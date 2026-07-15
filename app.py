#!/usr/bin/env python3
"""Free LLM Hub -- local gateway that serves FREE LLM providers to any tool.

Surfaces:
  GET  /                        dashboard (templates/index.html)
  /api/*                        config API (localhost-open, no auth)
  GET  /v1/models               OpenAI-compatible model list
  POST /v1/chat/completions     OpenAI-compatible chat (streaming passthrough)
  POST /v1/messages             Anthropic Messages API (translated to OpenAI
                                upstream, both directions, incl. streaming) --
                                this is what lets Claude Code use free models.
  POST /v1/messages/count_tokens  rough token estimate (Claude Code compat)

Auth: if a local API key is configured (config.get_local_api_key()), all /v1/*
routes require it as 'Authorization: Bearer <key>' or 'x-api-key: <key>'.
Dashboard and /api/* stay open (the server only binds 127.0.0.1).

Run:  python app.py    (PORT env overrides default 8787)
"""

import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

import requests
from flask import Flask, Response, g, jsonify, render_template, request, stream_with_context

try:
    from jinja2 import TemplateNotFound
except Exception:  # pragma: no cover - jinja2 always ships with flask
    class TemplateNotFound(Exception):
        pass

import config
import providers as prov
import quota

import logging
import traceback as _traceback

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
_log = logging.getLogger("free-llm-hub")

app = Flask(__name__)


@app.errorhandler(Exception)
def _json_error(exc):
    """Safety net: any unhandled exception becomes a clean JSON 500 (never a
    bare HTML 500), with the real cause logged locally. Keeps the dashboard
    usable and gives an actionable message instead of 'Save failed: 500'."""
    from werkzeug.exceptions import HTTPException
    if isinstance(exc, HTTPException):
        return exc  # preserve intended 4xx/redirects
    _log.error("Unhandled error on %s:\n%s", request.path, _traceback.format_exc())
    return jsonify({"error": _sanitize(str(exc)) or "internal error"}), 500


PORT = int(os.environ.get("PORT", "8787") or "8787")
HOST = "127.0.0.1"

CONNECT_TIMEOUT = 10          # seconds
CHAT_READ_TIMEOUT = 300       # seconds (long NON-streaming generations)
# Streaming (stream=True) timeouts. A hung/slow 200 must not stall the client for
# the full CHAT_READ_TIMEOUT with no fallback:
#   STREAM_FIRST_BYTE_TIMEOUT — max wait for the FIRST streamed byte before we give
#     up on this provider and fall through to the next hop in the chain.
#   STREAM_IDLE_TIMEOUT — the requests read timeout for streaming; bounds the gap
#     between chunks once the stream is committed (a mid-stream stall fails in ~90s
#     instead of 300s).
STREAM_FIRST_BYTE_TIMEOUT = 25   # seconds
STREAM_IDLE_TIMEOUT = 90         # seconds
MODELS_READ_TIMEOUT = 10      # seconds (model discovery / key tests)
MODEL_CACHE_TTL = 60          # seconds
MAX_HOPS = 6                  # primary + up to 5 fallback models (across providers)

_model_cache = {}             # pid -> (timestamp, [model ids])
_model_cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers: secrets hygiene
# ---------------------------------------------------------------------------

def _secret_values():
    """Every secret we know about, for scrubbing error strings."""
    vals = []
    try:
        cfg = config.load_config()
        for pcfg in (cfg.get("providers") or {}).values():
            if not isinstance(pcfg, dict):
                continue
            for key in (pcfg.get("api_keys") or []):
                if key:
                    vals.append(key)
            legacy = pcfg.get("api_key")  # defensive: normally migrated away on load
            if legacy:
                vals.append(legacy)
        local = cfg.get("local_api_key")
        if local:
            vals.append(local)
    except Exception:
        pass
    return vals


def _sanitize(text, limit=400):
    """Never let a provider key (or the local key) leak into an error/log."""
    s = str(text if text is not None else "")
    for secret in _secret_values():
        if secret and secret in s:
            s = s.replace(secret, "***")
    return s[:limit]


# ---------------------------------------------------------------------------
# Helpers: providers / models
# ---------------------------------------------------------------------------

def _enabled_keyed():
    """Provider ids that are enabled AND have an API key saved."""
    out = []
    for p in prov.list_providers():
        pid = p["id"]
        pcfg = config.get_provider_config(pid)
        if pcfg.get("enabled") and pcfg.get("api_key"):
            out.append(pid)
    return out


def _available_providers():
    """Enabled+keyed providers that still have free quota (not exhausted/throttled).
    Falls back to ALL enabled+keyed when every one is exhausted, so the gateway
    still tries (and the dashboard's red banner tells the user why it may fail)."""
    keyed = _enabled_keyed()
    live = [pid for pid in keyed if not quota.is_exhausted(pid)]
    return live or keyed


def _models_url_for(pid, pcfg):
    p = prov.get_provider(pid) or {}
    custom = pcfg.get("base_url")
    if custom:
        return custom.rstrip("/") + "/models"
    return p.get("models_url")


def _parse_model_ids(payload):
    """Accept OpenAI ({'data':[{'id':..}]}) and common variants."""
    items = []
    if isinstance(payload, dict):
        for key in ("data", "models"):
            val = payload.get(key)
            if isinstance(val, list):
                items = val
                break
    elif isinstance(payload, list):
        items = payload
    ids = []
    for item in items:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict):
            mid = item.get("id") or item.get("name") or item.get("model")
            if isinstance(mid, str) and mid:
                ids.append(mid)
    return ids


def provider_free_models(pid, live=True):
    """Free models for a provider: live discovery if keyed (60s cache),
    else the registry's default_free_models. Always safety-filtered."""
    p = prov.get_provider(pid)
    if not p:
        return []
    defaults = [m for m in (p.get("default_free_models") or []) if prov.is_model_allowed(m)]
    pcfg = config.get_provider_config(pid)
    if not live or not pcfg.get("api_key"):
        return defaults

    now = time.time()
    with _model_cache_lock:
        hit = _model_cache.get(pid)
        if hit and (now - hit[0]) < MODEL_CACHE_TTL:
            return list(hit[1])

    models = defaults
    url = _models_url_for(pid, pcfg)
    if url:
        try:
            resp = requests.get(
                url,
                headers={"Authorization": "Bearer " + pcfg["api_key"]},
                timeout=(CONNECT_TIMEOUT, MODELS_READ_TIMEOUT),
            )
            if resp.status_code == 200:
                ids = _parse_model_ids(resp.json())
                # filter_models drops blocked (uncensored) AND non-chat ids
                # (whisper/tts/embed/guard) — per the providers.py contract.
                live_free = prov.filter_models(
                    [m for m in ids if prov.is_free_model(pid, m)]
                )
                if live_free:
                    models = live_free
        except Exception:
            pass  # network/parse failure -> defaults

    with _model_cache_lock:
        _model_cache[pid] = (now, list(models))
    return models


def aggregated_models():
    """[{id:'<pid>/<model>', provider, model}] across enabled+keyed providers."""
    out = []
    for pid in _enabled_keyed():
        for m in provider_free_models(pid):
            out.append({"id": pid + "/" + m, "provider": pid, "model": m})
    return out


# --------------------------------------------------------------------------- #
# Benchmark heuristic — rank free models best-first WITHOUT any live network or
# hard-coded model list, so it keeps working as providers rotate their catalogs.
# Score = capability-family tokens + parameter size + version recency. Higher is
# stronger. Used to (a) auto-pick the orchestration default and (b) order the
# cross-provider fallback chain best-first.
# --------------------------------------------------------------------------- #
# Capability families, strongest first. Matched case-insensitively as substrings.
_BENCH_FAMILY = [
    (("deepseek-v4", "deepseek-r2", "grok-4", "gpt-5", "claude-opus", "claude-sonnet-5",
      "gemini-3", "llama-4-maverick", "qwen3.5", "qwen3-max"), 100),
    (("deepseek-v3", "deepseek-r1", "gpt-oss-120b", "llama-4", "qwen3", "gemini-2.5-pro",
      "mixtral-8x22", "command-r-plus", "minimax", "glm-5", "glm52", "kimi"), 82),
    (("llama-3.3-70b", "llama-3.1-405", "qwen2.5-72", "gemini-2.5-flash", "gemma-3-27",
      "mistral-large", "nemotron-70", "command-r", "gpt-4o"), 68),
    (("70b", "72b", "gemini-2.0", "gpt-4o-mini", "mistral-small", "codestral", "gemma-2-27"), 52),
    (("32b", "27b", "gemma-3", "phi-4", "qwen2.5-coder"), 40),
    (("8b", "9b", "7b", "flash-lite", "mini", "small", "nemo"), 24),
]


def _benchmark_score(pid, model_id):
    """Heuristic strength score for a '<model>' on provider `pid` (higher=better).
    Pure string heuristic — no network, future-proof against catalog churn."""
    low = (model_id or "").lower()
    score = 10  # base so an unknown model still ranks above nothing
    for names, pts in _BENCH_FAMILY:
        if any(n in low for n in names):
            score = max(score, pts)
            break
    # Explicit parameter size nudges within a family (…-70b > …-8b).
    m = re.search(r"(\d{1,4})\s*b\b", low)
    if m:
        try:
            score += min(int(m.group(1)), 500) / 25.0
        except ValueError:
            pass
    # Prefer instruct/chat tunes over raw/base for a chat gateway.
    if any(t in low for t in ("instruct", "chat", "-it")):
        score += 3
    # A tiny provider bias breaks ties toward fast, reliable free hosts.
    score += {"cerebras": 2.0, "groq": 1.8, "nvidia": 1.2, "google": 1.0}.get(pid, 0.0)
    return score


def _best_free_pair():
    """Scan every AVAILABLE (enabled+keyed+quota-left) provider's free models and
    return the single highest-benchmark (pid, model) pair, or (None, None)."""
    best, best_pid, best_score = None, None, -1.0
    for pid in _available_providers():
        for m in provider_free_models(pid):
            s = _benchmark_score(pid, m)
            if s > best_score:
                best, best_pid, best_score = m, pid, s
    return (best_pid, best) if best else (None, None)


# --------------------------------------------------------------------------- #
# Difficulty-aware routing ("caveman" mode) — don't waste a strong model (or its
# scarce free quota) on an easy task, and don't hand a hard task to a weak model.
# Classify the request, then pick the CHEAPEST model that still clears the bar
# for simple/medium tasks, and the STRONGEST available for hard ones.
# --------------------------------------------------------------------------- #
_HARD_HINTS = (
    "refactor", "debug", "stack trace", "traceback", "algorithm", "architecture",
    "optimize", "optimise", "prove", "derive", "analyze", "analyse", "reason",
    "step by step", "step-by-step", "complex", "design a", "implement", "write code",
    "full code", "entire", "compile", "regex", "sql", "concurrency", "async",
    "benchmark", "vulnerab", "exploit", "math", "theorem",
)
_SIMPLE_HINTS = (
    "translate", "summarize", "summarise", "tl;dr", "rephrase", "reword",
    "spell", "grammar", "fix typo", "capitalize", "lowercase", "uppercase",
    "yes or no", "one word", "one line", "define ", "what is ", "who is ",
    "list ", "hello", "hi ", "thanks", "thank you",
)
# Minimum benchmark score a model needs to be trusted with each tier.
_DIFFICULTY_FLOOR = {"simple": 20, "medium": 50, "hard": 78}


def _messages_text(messages):
    parts = []
    for m in messages or []:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and isinstance(b.get("text"), str):
                    parts.append(b["text"])
                elif isinstance(b, str):
                    parts.append(b)
    return "\n".join(parts)


def _classify_difficulty(messages, max_tokens=None):
    """'simple' | 'medium' | 'hard' from prompt length, task hints, code, and the
    requested output size. Pure heuristic (no network)."""
    text = _messages_text(messages)
    low = text.lower()
    length = len(text)
    score = 0
    if "```" in text or re.search(r"\bdef \w+\(|\bclass \w+|function \w+\(|;\s*$", text):
        score += 2
    score += sum(1 for h in _HARD_HINTS if h in low)
    score -= sum(1 for h in _SIMPLE_HINTS if h in low)
    if length > 4000:
        score += 2
    elif length > 1500:
        score += 1
    elif length < 180:
        score -= 1
    if len(messages or []) > 8:
        score += 1
    try:
        if max_tokens and int(max_tokens) > 1500:
            score += 1
    except (TypeError, ValueError):
        pass
    if score >= 3:
        return "hard"
    if score <= 0:
        return "simple"
    return "medium"


# Approx FREE-tier tokens-per-minute per provider. A single request whose tokens
# exceed this gets a 413 "Payload Too Large" (Groq free = 6k TPM is the classic
# one that rejects an agentic CLI like Codex, whose requests are ~13k tokens of
# system prompt + tool schemas). Used to keep big requests off small providers.
_PROVIDER_TPM = {
    "groq": 6000, "github-models": 8000, "huggingface": 20000, "mistral": 30000,
    "morph": 30000, "sambanova": 50000, "cerebras": 60000, "deepseek": 60000,
    "openrouter": 100000, "cohere": 100000, "nvidia": 200000, "google": 250000,
}
_DEFAULT_TPM = 20000


def _provider_tpm(pid):
    return _PROVIDER_TPM.get(pid, _DEFAULT_TPM)


def _est_tokens(messages, tools=None):
    """Rough token estimate of a request (~4 chars/token). Counts message text,
    tool-call arguments, AND tool schemas — tools dominate an agentic CLI's size."""
    chars = 0
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and isinstance(b.get("text"), str):
                    chars += len(b["text"])
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            chars += len(str(fn.get("arguments") or "")) + len(str(fn.get("name") or ""))
    if tools:
        try:
            chars += len(json.dumps(tools))
        except Exception:
            pass
    return chars // 4 + 400  # + overhead for roles/formatting


def _provider_capable(pid, est):
    """Can this provider's free tier take a single `est`-token request? (margin
    for the model's own reply added.)"""
    return est <= 0 or _provider_tpm(pid) >= int(est * 1.15) + 512


# --------------------------------------------------------------------------- #
# SPEED tier — the dispatcher prefers FAST, good models and pushes slow reasoning
# models to the back (used only once the fast ones are rate-limited/exhausted).
# Speed = provider throughput minus a big penalty for reasoning models (they emit
# a long hidden 'thinking' pass -> slow to a useful answer) and huge params.
# --------------------------------------------------------------------------- #
_PROVIDER_SPEED = {
    "cerebras": 100, "groq": 92, "sambanova": 78, "morph": 70, "deepseek": 66,
    "mistral": 66, "google": 62, "agentrouter": 60, "nvidia": 54, "openrouter": 52,
    "huggingface": 46, "github-models": 44, "cohere": 60,
}
_DEFAULT_SPEED = 55
# Reasoning / "thinking" model families — slow to first useful token.
_SLOW_MODEL_RE = re.compile(
    r"(reasoning|thinking|\bqwq\b|deepseek[-_]?r\d|[-/]r1\b|\bo1\b|\bo3\b|magistral|"
    r"nemotron[-_](ultra|super)|gpt[-_]?oss|[-_]think\b|deepthink)", re.I)


def _speed_score(pid, model):
    """0-ish..100, higher = faster to a useful answer. Pure heuristic (no network)."""
    s = _PROVIDER_SPEED.get(pid, _DEFAULT_SPEED)
    low = (model or "").lower()
    if _SLOW_MODEL_RE.search(low):
        s -= 45                          # reasoning models: big latency hit
    m = re.search(r"(\d{2,4})\s*b\b", low)
    if m:
        try:
            n = int(m.group(1))
            s -= 22 if n >= 400 else 14 if n >= 200 else 7 if n >= 100 else 0
        except ValueError:
            pass
    return s


def _is_fast(pid, model):
    """A model quick enough for interactive use (non-reasoning on a decent host)."""
    return _speed_score(pid, model) >= 55


# --------------------------------------------------------------------------- #
# DEAD-MODEL tracker — "only route to models that actually work".
#
# A provider's catalog lies: it lists models the key has no access to (403), that
# no longer exist (404), or that reject chat (400 on a non-chat id). A live bulk
# test found 78 of 150 listed free models unusable — e.g. every github-models id
# returns 403 "No access to model" when the token lacks the models:read scope.
#
# Rather than hard-code today's results (they rot as catalogs change), LEARN: the
# first time a model answers with a hard MODEL-level error, sideline it and route
# around it. Self-healing — entries expire, so a fixed token/restored model comes
# back on its own.
#
# Deliberately NOT tracked here: 429 (quota — that's quota.mark_throttled's job,
# the model is fine) and 5xx (transient upstream). Only 403/404 are treated as
# "this exact model is unusable with this key", because they are unambiguous.
# 400 is NOT auto-sidelined: it is just as often a bad payload as a bad model,
# and blocklisting a good model off one malformed request would be worse.
# --------------------------------------------------------------------------- #
_DEAD_MODEL_TTL = 6 * 3600         # 6h, then re-probe (token fixed? model back?)
_dead_models = {}                  # (pid, model) -> expiry epoch
_dead_lock = threading.Lock()
_DEAD_STATUSES = (403, 404)


def _mark_model_dead(pid, model, status):
    if not model or status not in _DEAD_STATUSES:
        return
    with _dead_lock:
        _dead_models[(pid, str(model))] = time.time() + _DEAD_MODEL_TTL


def _is_model_dead(pid, model):
    key = (pid, str(model))
    with _dead_lock:
        exp = _dead_models.get(key)
        if not exp:
            return False
        if exp <= time.time():
            _dead_models.pop(key, None)   # TTL expired -> give it another chance
            return False
        return True


def _dead_model_rows():
    """[(pid, model, seconds_left)] for the dashboard / diagnostics."""
    now = time.time()
    with _dead_lock:
        return [(p, m, int(exp - now)) for (p, m), exp in _dead_models.items() if exp > now]


# Reasoning EFFORT the manager assigns per task difficulty. A simple question gets
# minimal thinking (fast); a hard task gets more. Applied ONLY to reasoning models
# (non-reasoning models ignore it). This is what makes "the manager decides the
# effort by the question" real — and it also overrides whatever a client (e.g.
# Codex) hard-coded, since the hub, not the CLI, knows the task.
_DIFFICULTY_EFFORT = {"simple": "low", "medium": "medium", "hard": "high"}


def _apply_reasoning_effort(payload, model, difficulty):
    """For a reasoning model, set reasoning_effort from the task difficulty so easy
    questions answer fast and hard ones think more. No-op for non-reasoning models."""
    if difficulty and _SLOW_MODEL_RE.search((model or "").lower()):
        payload["reasoning_effort"] = _DIFFICULTY_EFFORT.get(difficulty, "medium")
    return payload


def _route_by_difficulty(messages, max_tokens=None, est=None):
    """Pick (pid, model) by task difficulty across AVAILABLE providers that can
    also HANDLE the request size (skip small-TPM providers for big requests).
    - hard  -> strongest capable model.
    - simple/medium -> cheapest capable model clearing the tier floor.
    Returns (None, None, difficulty) if nothing is ready (caller falls back)."""
    difficulty = _classify_difficulty(messages, max_tokens)
    if est is None:
        est = _est_tokens(messages)
    providers = [p for p in _available_providers() if _provider_capable(p, est)]
    if not providers:  # request too big for every free tier -> try the biggest anyway
        providers = sorted(_available_providers(), key=_provider_tpm, reverse=True)
    cands = []  # (score, pid, model)
    for pid in providers:
        for m in provider_free_models(pid):
            # skip ids this key provably can't use (403/404 learned at runtime)
            if prov.is_model_allowed(m) and not _is_model_dead(pid, m):
                cands.append((_benchmark_score(pid, m), pid, m))
    if not cands:
        return None, None, difficulty
    # Prefer FAST models — the primary should be a good model the user won't wait
    # on. Slow reasoning models are used only if NO fast model is available (and
    # they still appear later in _build_chain as a last-resort fallback).
    pool = [c for c in cands if _is_fast(c[1], c[2])] or cands
    if difficulty == "hard":
        # best QUALITY among the fast models (good + fast, not the slow giant).
        _s, pid, model = max(pool, key=lambda t: t[0])
        return pid, model, difficulty
    floor = _DIFFICULTY_FLOOR[difficulty]
    qualified = [c for c in pool if c[0] >= floor]
    if qualified:
        # cheapest fast model that still clears the bar -> saves strong quota
        _s, pid, model = min(qualified, key=lambda t: t[0])
    else:
        _s, pid, model = max(pool, key=lambda t: t[0])
    return pid, model, difficulty


def _is_orchestrate(model):
    """True when the caller wants the manager to choose (Auto / empty / claude-*)."""
    model = (model or "").strip().lower()
    if "/" in model:
        return False
    return (not model) or model in ("auto", "orchestrate", "default") \
        or model.startswith("claude")


def _autoselect_default_if_unset():
    """If no orchestration default is configured yet, auto-pick the best free
    model across the newly-ready providers and persist it. Never overrides a
    default the user already chose. Best-effort (never raises to the caller)."""
    try:
        if config.get_default():
            return
        pid, model = _best_free_pair()
        if pid and model and prov.is_model_allowed(model):
            config.set_default(pid, model)
    except Exception:
        pass


def _resolve_model(model):
    """'<pid>/<model>' -> (pid, model). 'auto'/empty/claude-* -> ORCHESTRATE:
    the free-LLM manager picks the primary itself (configured default, else the
    single highest-benchmark free model across all enabled+keyed providers) and
    the caller's _build_chain adds cross-provider fallback + key rotation on top.
    Returns (pid, model_id) or (None, error_message)."""
    model = model if isinstance(model, str) else ""
    model = model.strip()
    if "/" in model:
        head, rest = model.split("/", 1)
        if prov.get_provider(head):
            return head, rest

    default = config.get_default()
    # 'auto' (dashboard Auto mode), empty, or Claude Code's built-in claude-*
    # names all mean "let the manager choose + orchestrate".
    is_auto = (not model) or model.lower() in ("auto", "orchestrate", "default") \
        or model.lower().startswith("claude")
    if is_auto:
        if default and default.get("provider") and default.get("model"):
            return default["provider"], default["model"]
        # No default set yet -> orchestrate: pick the single highest-benchmark
        # free model across every enabled+keyed provider (not just the first).
        pid, best = _best_free_pair()
        if pid and best:
            return pid, best
        return None, ("No enabled provider with a saved key yet. Add a key and "
                      "enable a provider on the dashboard, then try again.")

    # Explicit bare model name -> run it on the default provider if one is set.
    if default and default.get("provider"):
        return default["provider"], model
    return None, ("Pick 'Auto' or a '<provider>/<model>', or set a default on "
                  "the dashboard.")


def _check_provider_ready(pid):
    """None if usable, else a human error message."""
    if not prov.get_provider(pid):
        return "Unknown provider '%s'." % pid
    pcfg = config.get_provider_config(pid)
    if not pcfg.get("api_key"):
        return "Provider '%s' has no API key saved. Add one on the dashboard." % pid
    if not pcfg.get("enabled"):
        return "Provider '%s' is disabled. Enable it on the dashboard." % pid
    return None


def _comparable_model(model_id, candidates):
    """Pick the candidate sharing the most family tokens with model_id."""
    if not candidates:
        return None
    base = model_id.split("/")[-1].lower()
    tokens = [t for t in re.split(r"[-_.:@ ]", base) if len(t) >= 3 and not t.isdigit()]
    best, best_score = None, 0
    for cand in candidates:
        low = cand.lower()
        score = sum(1 for t in tokens if t in low)
        if score > best_score:
            best, best_score = cand, score
    return best or candidates[0]


def _build_chain(primary_pid, model_id, est=0):
    """Priority-ordered [(pid, model)] fallback chain. Primary first, then the
    next-best MODELS across every AVAILABLE, size-capable provider, INTERLEAVED
    across providers (best model of each provider, then each provider's 2nd, ...).
    So if the chosen model is rate-limited (429) the gateway auto-switches to the
    next model in priority: a different PROVIDER first (handles per-account limits
    like NVIDIA), while later rounds still try other models of the same provider
    (handles per-model limits like Groq). Size-incapable providers are skipped so
    a big request never falls onto one that will 413. Capped at MAX_HOPS."""
    chain = [(primary_pid, model_id)]
    seen = {(primary_pid, model_id)}
    # Split every available, size-capable candidate into FAST and SLOW tiers.
    # FAST models are tried first (best-first); SLOW reasoning models are the LAST
    # resort — only reached once the fast+good ones are exhausted/rate-limited.
    fast, slow = [], []
    for pid in _available_providers():
        if not _provider_capable(pid, est):
            continue
        for m in provider_free_models(pid):
            if (pid, m) in seen or not prov.is_model_allowed(m) or _is_model_dead(pid, m):
                continue
            entry = (_benchmark_score(pid, m), pid, m)
            (fast if _is_fast(pid, m) else slow).append(entry)
    fast.sort(key=lambda t: t[0], reverse=True)   # best fast model first
    slow.sort(key=lambda t: t[0], reverse=True)   # then best slow model
    for _score, pid, m in fast + slow:
        if len(chain) >= MAX_HOPS:
            break
        if (pid, m) not in seen:
            chain.append((pid, m))
            seen.add((pid, m))
    return chain


# Key-pool rotation: statuses that mean "this key is bad/throttled, try the
# next key for the SAME provider before falling back to another provider".
_KEY_ROTATE_STATUSES = (401, 403, 429)
_provider_key_cursor = {}          # pid -> next round-robin start offset
_key_cursor_lock = threading.Lock()


def _next_key_start(pid, n):
    """Round-robin starting index for provider `pid`, advanced per request so
    load spreads across the pool instead of always hammering key[0]."""
    if n <= 1:
        return 0
    with _key_cursor_lock:
        start = _provider_key_cursor.get(pid, 0) % n
        _provider_key_cursor[pid] = (start + 1) % n
    return start


def _upstream_chat(pid, payload, stream):
    """POST {base_url}/chat/completions for provider pid, rotating across the
    provider's api_keys pool. Tries a round-robin start key; on 401/403/429 it
    advances to the next key for the SAME provider. Returns the first non-
    rotatable response (or the last response/exception once keys are exhausted,
    so the caller's provider-level fallback still kicks in). May raise
    requests.RequestException or RuntimeError. Never logs a key."""
    pcfg = config.get_provider_config(pid)
    base = prov.base_url_for(pid, pcfg.get("base_url"))
    if not base:
        raise RuntimeError("no base_url for provider " + pid)
    keys = pcfg.get("api_keys") or []
    if not keys:
        raise RuntimeError("no api key for provider " + pid)
    url = base.rstrip("/") + "/chat/completions"
    n = len(keys)
    start = _next_key_start(pid, n)
    last_exc = None
    for i in range(n):
        is_last = (i == n - 1)
        key = keys[(start + i) % n]
        try:
            resp = requests.post(
                url,
                json=payload,
                headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                stream=stream,
                # Streaming: bound the inter-chunk (idle) read at STREAM_IDLE_TIMEOUT
                # so a stalled stream fails in ~90s not 300s (the handler's first-byte
                # peek falls through even sooner, at ~25s). Non-streaming keeps the
                # long CHAT_READ_TIMEOUT for slow one-shot generations.
                timeout=(CONNECT_TIMEOUT, STREAM_IDLE_TIMEOUT if stream else CHAT_READ_TIMEOUT),
            )
        except requests.RequestException as exc:
            last_exc = exc
            if is_last:
                raise
            continue
        quota.record(pid, payload.get("model"))  # counts against free quota (per provider + model)
        # A 429 on a SINGLE key just rotates to the next key below. Only when the
        # LAST key also 429s (every key for this provider is rate-limited) do we
        # sideline the whole provider. And when there's no numeric Retry-After,
        # cool down for a SHORT 60s (assume a per-minute burst) instead of pegging
        # it exhausted until the day/month window resets — `secs or 60` keeps the
        # provider usable ~1 min later; a real Retry-After is honored as-is.
        if resp.status_code == 429 and is_last:
            retry_after = resp.headers.get("Retry-After")
            secs = None
            try:
                secs = float(retry_after) if retry_after else None
            except ValueError:
                secs = None
            quota.mark_throttled(pid, secs or 60)
        # 403 (no access to this model with this key) / 404 (model gone) are about
        # the MODEL, not the key or the quota: sideline just that id so routing
        # stops picking it. Only on the last key — an earlier key's 403 may just
        # mean THAT key lacks access, and rotation below still gets a chance.
        if resp.status_code in _DEAD_STATUSES and is_last:
            _mark_model_dead(pid, payload.get("model"), resp.status_code)
        # Auth/rate-limit on this key -> try the next key before this provider
        # is given up on. On the last key, return it so the caller can react
        # (429/5xx -> provider fallback; 401/403 -> surfaced as an error).
        if resp.status_code in _KEY_ROTATE_STATUSES and not is_last:
            resp.close()
            continue
        return resp
    if last_exc is not None:  # only reachable if the pool was somehow empty
        raise last_exc
    raise RuntimeError("no api key for provider " + pid)


def _retryable(status):
    return status == 429 or status >= 500


def _upstream_error_detail(resp):
    try:
        data = resp.json()
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict) and err.get("message"):
                return _sanitize(err["message"])
            if isinstance(err, str) and err:
                return _sanitize(err)
            if data.get("message"):
                return _sanitize(data["message"])
    except ValueError:
        pass
    return _sanitize(resp.text or ("HTTP %d" % resp.status_code))


# ---------------------------------------------------------------------------
# Helpers: error shapes
# ---------------------------------------------------------------------------

def _openai_error(message, status, err_type="invalid_request_error"):
    return jsonify({"error": {"message": message, "type": err_type, "code": status}}), status


def _anthropic_error(err_type, message, status):
    return jsonify({"type": "error", "error": {"type": err_type, "message": message}}), status


# ---------------------------------------------------------------------------
# Auth guard: /v1/* only (dashboard + /api/* stay localhost-open)
# ---------------------------------------------------------------------------

@app.before_request
def _guard_v1():
    if not request.path.startswith("/v1"):
        return None
    local_key = config.get_local_api_key()
    if not local_key:
        return None  # open on localhost
    supplied = None
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()
    if not supplied:
        supplied = request.headers.get("x-api-key")
    if supplied and hmac.compare_digest(str(supplied), str(local_key)):
        return None
    msg = ("Missing or invalid local API key. Send it as "
           "'Authorization: Bearer <key>' or 'x-api-key: <key>'.")
    if request.path.startswith("/v1/messages"):
        return _anthropic_error("authentication_error", msg, 401)
    return _openai_error(msg, 401, "authentication_error")


# ---------------------------------------------------------------------------
# Live activity feed — a small ring buffer of recent gateway calls so the
# dashboard can show, per CLI/tool, what request is in flight, which model it
# landed on, and whether it succeeded. In-memory only; localhost, single user.
# ---------------------------------------------------------------------------
import collections  # noqa: E402  (local, stdlib)

_ACTIVITY_MAX = 40
_activity = collections.deque(maxlen=_ACTIVITY_MAX)
_activity_lock = threading.Lock()
_activity_seq = [0]
_INFERENCE_PATHS = {
    "/v1/chat/completions": "openai",
    "/v1/responses": "responses",
    "/v1/messages": "anthropic",
}
# Map a client's User-Agent to a friendly CLI/tool label (best-effort).
_UA_CLI = (
    ("codex", "Codex"), ("claude-cli", "Claude Code"), ("claude", "Claude Code"),
    ("aider", "Aider"), ("opencode", "OpenCode"), ("cursor", "Cursor"),
    ("qwen", "Qwen Code"), ("llm/", "llm"), ("openai", "OpenAI SDK"),
    ("anthropic", "Anthropic SDK"), ("python-requests", "script"),
    ("node", "node"), ("curl", "curl"),
)


def _guess_cli():
    ua = (request.headers.get("User-Agent") or "").lower()
    for sub, name in _UA_CLI:
        if sub in ua:
            return name
    return (ua.split("/")[0][:24] or "unknown") if ua else "unknown"


def _act_pick(pid, model):
    """Record the provider/model the orchestrator actually landed on."""
    act = getattr(g, "act", None)
    if act is not None:
        with _activity_lock:
            act["provider"] = pid
            act["model"] = model


def _activity_done(act, status, http=None):
    with _activity_lock:
        if act.get("finished") is None:
            act["status"] = status
            act["http"] = http
            act["finished"] = time.time()


@app.before_request
def _activity_before():
    if request.method != "POST":
        return None
    proto = _INFERENCE_PATHS.get(request.path)
    if not proto:
        return None
    body = request.get_json(force=True, silent=True) if request.is_json or True else None
    model_req = body.get("model") if isinstance(body, dict) else None
    with _activity_lock:
        _activity_seq[0] += 1
        act = {
            "id": _activity_seq[0], "protocol": proto, "cli": _guess_cli(),
            "model_req": model_req if isinstance(model_req, str) else None,
            "provider": None, "model": None, "status": "in_progress",
            "http": None, "stream": False,
            "started": time.time(), "finished": None,
        }
        _activity.appendleft(act)
    g.act = act
    return None


@app.after_request
def _activity_after(response):
    act = getattr(g, "act", None)
    if act is None:
        return response
    if response.mimetype == "text/event-stream" and 200 <= response.status_code < 300:
        with _activity_lock:
            act["stream"] = True
            act["status"] = "streaming"
            act["http"] = response.status_code
        response.call_on_close(lambda: _activity_done(act, "ok", response.status_code))
    else:
        ok = 200 <= response.status_code < 300
        _activity_done(act, "ok" if ok else "error", response.status_code)
    return response


@app.route("/api/dead-models", methods=["GET"])
def api_dead_models():
    """Models sidelined at runtime because this key provably can't use them
    (403 no-access / 404 gone). Self-healing: each entry expires and is re-probed."""
    rows = [{"provider": p, "model": m, "expires_in": s} for p, m, s in _dead_model_rows()]
    rows.sort(key=lambda r: (r["provider"], r["model"]))
    return jsonify({"dead": rows, "count": len(rows), "ttl_seconds": _DEAD_MODEL_TTL})


@app.route("/api/activity", methods=["GET"])
def api_activity():
    now = time.time()
    with _activity_lock:
        rows = list(_activity)
    out = []
    for a in rows:
        end = a["finished"] if a["finished"] else now
        out.append({**a, "duration_ms": int((end - a["started"]) * 1000)})
    return jsonify({"activity": out})


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    try:
        return render_template("index.html")
    except TemplateNotFound:
        return (
            "<h1>Free LLM Hub</h1>"
            "<p>Gateway is running, but <code>templates/index.html</code> is "
            "missing. The API surface is live: <code>/api/status</code>, "
            "<code>/api/providers</code>, <code>/v1/models</code>, "
            "<code>/v1/chat/completions</code>, <code>/v1/messages</code>.</p>"
        )


# ---------------------------------------------------------------------------
# Config API
# ---------------------------------------------------------------------------

def _mask_key(k):
    """Safe display form of a key: first4 + '…' + last4 (or '••••' if <9 chars).
    NEVER returns the full key — the reveal route is the only full-key surface."""
    s = k if isinstance(k, str) else str(k or "")
    if len(s) < 9:
        return "••••"
    return s[:4] + "…" + s[-4:]


def _provider_row(pid, live_models=False):
    p = prov.get_provider(pid) or {}
    pcfg = config.get_provider_config(pid)
    keys = pcfg.get("api_keys") or []
    # Provider rows never trigger a network model-discovery call by default:
    # a save/list must be instant and can't fail on a provider's flaky /models
    # endpoint. The live model list is served separately by GET /api/models.
    return {
        "id": pid,
        "name": p.get("name") or pid,
        "enabled": bool(pcfg.get("enabled")),
        "has_key": bool(keys),
        "key_count": len(keys),
        "keys": [{"masked": _mask_key(k), "index": i} for i, k in enumerate(keys)],
        "signup_url": prov.signup_url(pid),
        "key_hint": p.get("key_hint") or "",
        "notes": p.get("notes") or "",
        "paid": bool(p.get("paid")),
        "trial": bool(p.get("trial")),
        "free_models": provider_free_models(pid, live=live_models),
    }


@app.route("/api/providers", methods=["GET"])
def api_providers():
    return jsonify([_provider_row(p["id"]) for p in prov.list_providers()])


@app.route("/api/providers/<pid>", methods=["POST"])
def api_provider_update(pid):
    if not prov.get_provider(pid):
        return jsonify({"error": "unknown provider '%s'" % pid}), 404
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "invalid JSON body"}), 400
    kwargs = {}
    if "api_key" in body:
        val = body["api_key"]
        kwargs["api_key"] = val.strip() if isinstance(val, str) else val
        # Saving a NON-EMPTY key auto-enables the provider (so it starts working
        # immediately) — unless the caller explicitly set `enabled` in the same
        # request. The user can still turn it off manually afterwards.
        if kwargs["api_key"] and "enabled" not in body:
            kwargs["enabled"] = True
    if "enabled" in body:
        kwargs["enabled"] = bool(body["enabled"])
    if "base_url" in body:
        val = body["base_url"]
        # config.set_provider_config treats None as "leave untouched" and ''
        # as "clear" — so an empty/null base_url must be passed as '' here.
        kwargs["base_url"] = val.strip() if isinstance(val, str) else ""
    if kwargs:
        config.set_provider_config(pid, **kwargs)
        with _model_cache_lock:
            _model_cache.pop(pid, None)  # key/base changed -> rediscover
        _autoselect_default_if_unset()  # first keyed provider -> best default
    return jsonify(_provider_row(pid))


@app.route("/api/providers/<pid>/keys", methods=["POST"])
def api_provider_add_key(pid):
    """Add ONE key to the provider's rotation pool (dedupes)."""
    if not prov.get_provider(pid):
        return jsonify({"error": "unknown provider '%s'" % pid}), 404
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "invalid JSON body"}), 400
    val = body.get("api_key")
    key = val.strip() if isinstance(val, str) else ""
    if not key:
        return jsonify({"error": "api_key is required"}), 400
    config.add_provider_key(pid, key)
    # Adding a key signals intent to use this provider -> auto-enable it (the
    # user can still toggle it off). Idempotent: only flips a disabled row.
    if not config.get_provider_config(pid).get("enabled"):
        config.set_provider_config(pid, enabled=True)
    with _model_cache_lock:
        _model_cache.pop(pid, None)  # pool changed -> rediscover
    _autoselect_default_if_unset()  # first keyed provider -> pick a best default
    return jsonify(_provider_row(pid))


@app.route("/api/providers/<pid>/keys/<int:idx>", methods=["DELETE"])
def api_provider_remove_key(pid, idx):
    """Remove the key at `idx` from the provider's rotation pool."""
    if not prov.get_provider(pid):
        return jsonify({"error": "unknown provider '%s'" % pid}), 404
    if not config.remove_provider_key(pid, idx):
        return jsonify({"error": "no key at index %d" % idx}), 404
    with _model_cache_lock:
        _model_cache.pop(pid, None)  # pool changed -> rediscover
    return jsonify(_provider_row(pid))


@app.route("/api/providers/<pid>/keys/<int:idx>/reveal", methods=["GET"])
def api_provider_reveal_key(pid, idx):
    """Return the FULL key at `idx` (localhost-only, single-user, in-threat-model
    per the plaintext local store) so the dashboard eye-toggle can show it."""
    if not prov.get_provider(pid):
        return jsonify({"error": "unknown provider '%s'" % pid}), 404
    keys = config.list_provider_keys(pid)
    if idx < 0 or idx >= len(keys):
        return jsonify({"error": "no key at index %d" % idx}), 404
    return jsonify({"api_key": keys[idx]})


@app.route("/api/test/<pid>", methods=["POST"])
def api_test_provider(pid):
    p = prov.get_provider(pid)
    if not p:
        return jsonify({"ok": False, "detail": "unknown provider", "sample_models": []}), 404
    pcfg = config.get_provider_config(pid)
    key = pcfg.get("api_key")
    if not key:
        return jsonify({"ok": False, "detail": "No API key saved for this provider.",
                        "sample_models": []})
    headers = {"Authorization": "Bearer " + key}
    models_url = _models_url_for(pid, pcfg)
    if models_url:
        try:
            resp = requests.get(models_url, headers=headers,
                                timeout=(CONNECT_TIMEOUT, MODELS_READ_TIMEOUT))
        except requests.RequestException as exc:
            return jsonify({"ok": False,
                            "detail": _sanitize("%s: %s" % (exc.__class__.__name__, exc)),
                            "sample_models": []})
        if resp.status_code == 200:
            try:
                ids = _parse_model_ids(resp.json())
            except ValueError:
                ids = []
            return jsonify({"ok": True,
                            "detail": "Key OK (HTTP 200, %d models listed)." % len(ids),
                            "sample_models": ids[:5]})
        return jsonify({"ok": False,
                        "detail": "HTTP %d: %s" % (resp.status_code, _upstream_error_detail(resp)),
                        "sample_models": []})
    # No models_url -> 1-token chat probe
    model = None
    for m in (p.get("default_free_models") or []):
        if prov.is_model_allowed(m):
            model = m
            break
    if not model:
        return jsonify({"ok": False,
                        "detail": "Provider has no models_url and no default model to test with.",
                        "sample_models": []})
    try:
        resp = _upstream_chat(pid, {"model": model,
                                    "messages": [{"role": "user", "content": "hi"}],
                                    "max_tokens": 1}, stream=False)
    except (requests.RequestException, RuntimeError) as exc:
        return jsonify({"ok": False,
                        "detail": _sanitize("%s: %s" % (exc.__class__.__name__, exc)),
                        "sample_models": []})
    if resp.status_code == 200:
        return jsonify({"ok": True, "detail": "Key OK (1-token chat succeeded on %s)." % model,
                        "sample_models": [model]})
    return jsonify({"ok": False,
                    "detail": "HTTP %d: %s" % (resp.status_code, _upstream_error_detail(resp)),
                    "sample_models": []})


@app.route("/api/models", methods=["GET"])
def api_models():
    return jsonify(aggregated_models())


@app.route("/api/default", methods=["GET", "POST"])
def api_default():
    if request.method == "GET":
        return app.response_class(json.dumps(config.get_default()),
                                  mimetype="application/json")
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "invalid JSON body"}), 400
    provider = body.get("provider")
    model = body.get("model")
    if not provider or not model:
        return jsonify({"error": "both 'provider' and 'model' are required"}), 400
    if not prov.get_provider(provider):
        return jsonify({"error": "unknown provider '%s'" % provider}), 404
    if not prov.is_model_allowed(model):
        return jsonify({"error": "model '%s' is blocked by the safety filter" % model}), 403
    config.set_default(provider, model)
    return jsonify({"ok": True, "default": config.get_default()})


def _suggested_model():
    default = config.get_default()
    if default and default.get("provider") and default.get("model"):
        return default["provider"] + "/" + default["model"]
    for pid in _enabled_keyed():
        models = provider_free_models(pid, live=False)
        if models:
            return pid + "/" + models[0]
    for p in prov.list_providers():
        models = [m for m in (p.get("default_free_models") or []) if prov.is_model_allowed(m)]
        if models:
            return p["id"] + "/" + models[0]
    return "<provider>/<model>"


def _connect_snippets():
    key = config.get_local_api_key()
    shown_key = key or "free-llm-hub"
    # Use the SAME model id the CLI auto-fixers write (first aggregated free model),
    # falling back to the suggestion only when nothing is keyed yet — so the shown
    # snippet and the written env block never disagree.
    model = _first_free_model_id() or _suggested_model()
    claude = ("export ANTHROPIC_BASE_URL=http://localhost:%d\n"
              "export ANTHROPIC_AUTH_TOKEN=%s\n"
              "export ANTHROPIC_MODEL=%s\n"
              "claude" % (PORT, shown_key, model))
    openai = ("export OPENAI_BASE_URL=http://localhost:%d/v1\n"
              "export OPENAI_API_KEY=%s" % (PORT, shown_key))
    return {"claude_code": claude, "openai": openai}


@app.route("/api/status", methods=["GET"])
def api_status():
    default = config.get_default()
    keyed = _enabled_keyed()
    # Per-provider free-quota snapshot (used, remaining, reset countdown, throttled).
    q = {}
    exhausted = 0
    for pid in keyed:
        p = prov.get_provider(pid) or {}
        # A PAID provider has no free tier, so it has no free quota to report.
        # It IS "exhausted" by quota.status() (limit 0), but surfacing that would
        # make the banner cry "out of free quota - resets in 389h" about providers
        # that never had a free allowance. Report free quota for free providers only.
        if p.get("paid"):
            continue
        s = quota.status(pid)
        s["name"] = p.get("name", pid)
        s["models"] = quota.models(pid)  # {model_id: used_count} this window
        q[pid] = s
        if s["exhausted"]:
            exhausted += 1
    # Exhaustion is about the FREE fleet only: `q` holds just the free providers
    # (paid ones were skipped above). Comparing against len(keyed) would include
    # paid providers in the denominator, so all_exhausted could never be true
    # while any paid provider was keyed.
    free_count = len(q)
    return jsonify({
        "providers_enabled": len(keyed),
        "free_providers": free_count,
        "has_default": bool(default and default.get("provider") and default.get("model")),
        "local_api_key_set": bool(config.get_local_api_key()),
        "connect_snippets": _connect_snippets(),
        "quota": q,
        "all_exhausted": free_count > 0 and exhausted == free_count,
        "any_exhausted": exhausted > 0,
    })


# ---------------------------------------------------------------------------
# Local CLI detection / connection status / auto-fix
# ---------------------------------------------------------------------------
# Detect known local AI CLIs, report whether each one is already pointed at
# THIS hub, and (safely, additively) rewrite the CLI's OWN config file to use
# a free model served here. All /api/clis/* routes are localhost-open like the
# rest of /api/*. Everything fails open — a missing/garbled config never
# crashes a row, it just reads as connected:false. Provider API keys are never
# written into a response (masked); the local gateway key is treated the same
# way _connect_snippets() already does (shown so the user can paste it, never
# logged).


def _home():
    return os.path.expanduser("~")


def _xdg_config():
    return os.environ.get("XDG_CONFIG_HOME") or os.path.join(_home(), ".config")


def _llm_user_dir():
    """Best-effort user dir for Simon Willison's `llm` (click app dir)."""
    override = os.environ.get("LLM_USER_PATH")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    if sys.platform == "darwin":
        return os.path.join(_home(), "Library", "Application Support", "io.datasette.llm")
    if os.name == "nt":
        # `llm` (click, app_dir with roaming=True) uses %APPDATA%\Roaming, NOT
        # %LOCALAPPDATA% — match it so we find/edit the same config file.
        base = os.environ.get("APPDATA") or os.path.join(_home(), "AppData", "Roaming")
        return os.path.join(base, "io.datasette.llm")
    return os.path.join(_xdg_config(), "io.datasette.llm")


def _short(path):
    """Display a path with ~ for the home dir (cosmetic only)."""
    try:
        home = _home()
        if path == home:
            return "~"
        if path.startswith(home + os.sep):
            return "~" + path[len(home):]
    except Exception:
        pass
    return path


def _p_claude():
    return os.path.join(_home(), ".claude", "settings.json")


def _p_opencode():
    return os.path.join(_xdg_config(), "opencode", "opencode.json")


def _p_aider():
    return os.path.join(_home(), ".aider.conf.yml")


def _p_qwen_env():
    return os.path.join(_home(), ".qwen", ".env")


def _p_codex():
    return os.path.join(_home(), ".codex", "config.toml")


# The CLI registry: known local AI CLIs and how each connects to a custom
# OpenAI/Anthropic endpoint. `autofix` names a safe writer strategy (JSON/
# YAML/dotenv merge) or is None for CLIs we won't touch automatically (TOML,
# protocol-incompatible, or uncertain — handled via a manual instructions
# payload). Paths are resolved at import; env path overrides (XDG_CONFIG_HOME,
# LLM_USER_PATH) are read live inside the path helpers above.
CLI_REGISTRY = [
    {
        "id": "claude",
        "name": "Claude Code",
        "kind": "anthropic",
        "bins": ["claude"],
        "config_paths": [_p_claude(), os.path.join(_home(), ".claude", "settings.local.json")],
        "env_check": ["ANTHROPIC_BASE_URL"],
        "autofix": "claude",
        "write_path": _p_claude(),
        "default_method": "config",
        "hint": ("Installed. Set ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN/ANTHROPIC_MODEL, "
                 "or run Auto-fix to write the 'env' block of ~/.claude/settings.json."),
    },
    {
        "id": "aider",
        "name": "Aider",
        "kind": "openai",
        "bins": ["aider"],
        "config_paths": [_p_aider()],
        "env_check": ["OPENAI_API_BASE", "OPENAI_BASE_URL"],
        "autofix": "aider",
        "write_path": _p_aider(),
        "default_method": "config",
        "hint": ("Installed. Set OPENAI_API_BASE + OPENAI_API_KEY, or run Auto-fix to write "
                 "openai-api-base/openai-api-key/model into ~/.aider.conf.yml."),
    },
    {
        "id": "opencode",
        "name": "OpenCode",
        "kind": "openai",
        "bins": ["opencode"],
        "config_paths": [_p_opencode(), os.path.join(_xdg_config(), "opencode", "opencode.jsonc")],
        "env_check": ["OPENAI_BASE_URL", "OPENAI_API_BASE"],
        "autofix": "opencode",
        "write_path": _p_opencode(),
        "default_method": "config",
        "hint": ("Installed. Run Auto-fix to add a 'free-llm-hub' openai-compatible provider to "
                 "~/.config/opencode/opencode.json (provider schema can vary by opencode version)."),
    },
    {
        "id": "codex",
        "name": "OpenAI Codex CLI",
        "kind": "openai",
        "bins": ["codex"],
        "config_paths": [_p_codex()],
        "env_check": ["OPENAI_BASE_URL", "OPENAI_API_BASE"],
        "autofix": "codex",  # TOML edited ADDITIVELY (top keys) + one [table], reversible
        "write_path": _p_codex(),
        "default_method": "config",
        "hint": ("Installed. Run Auto-fix to add a [model_providers.freehub] block "
                 "(wire_api = \"responses\") to ~/.codex/config.toml — the localhost hub needs "
                 "NO auth, so there's no API key or env var to set. Just restart Codex afterwards."),
        "manual_note": (
            "Codex (2026+) speaks ONLY the OpenAI Responses API (wire_api = \"responses\") and is "
            "wired through ~/.codex/config.toml, NOT environment variables — the "
            "OPENAI_API_BASE/OPENAI_BASE_URL/OPENAI_API_KEY/OPENAI_MODEL chat vars do NOT connect "
            "it. Auto-fix adds:\n"
            "  [model_providers.freehub]\n"
            "  name = \"Free LLM Hub\"\n"
            "  base_url = \"http://127.0.0.1:%d/v1\"\n"
            "  wire_api = \"responses\"\n"
            "and sets  model_provider = \"freehub\"  +  model = \"auto\"  in the top (pre-table) "
            "section. The localhost hub needs NO auth, so there is no API key or environment "
            "variable to set (if the hub has a local key, Auto-fix embeds it in config.toml for "
            "you). The only manual step: if Codex was already open, restart it (or run /model) so "
            "it re-reads config.toml." % PORT
        ),
    },
    {
        "id": "gemini",
        "name": "Gemini CLI",
        "kind": "openai",  # nearest allowed kind; see manual_note — not natively OpenAI
        "bins": ["gemini"],
        "config_paths": [os.path.join(_home(), ".gemini", "settings.json")],
        "env_check": ["GOOGLE_GEMINI_BASE_URL", "GEMINI_API_BASE_URL"],
        "autofix": None,  # protocol mismatch — uncertain/unsupported, do not auto-config
        "default_method": "manual",
        "hint": ("Installed, but Gemini CLI speaks Google's native API — this OpenAI/Anthropic hub "
                 "cannot serve it directly (uncertain)."),
        "manual_note": (
            "INCOMPATIBLE: Google's Gemini CLI reads GEMINI_API_BASE_URL / GOOGLE_GEMINI_BASE_URL "
            "and speaks Google's native wire format — it is NOT OpenAI-shaped, so this hub cannot "
            "serve it and the usual OPENAI_* env vars do nothing for it. Use Qwen Code (qwen) "
            "instead — it's an OpenAI-compatible Gemini-CLI fork that this hub fully supports "
            "(Auto-fix wires it into ~/.qwen/.env)."
        ),
    },
    {
        "id": "qwen",
        "name": "Qwen Code",
        "kind": "openai",
        "bins": ["qwen"],
        "config_paths": [os.path.join(_home(), ".qwen", "settings.json"), _p_qwen_env()],
        "env_check": ["OPENAI_API_BASE", "OPENAI_BASE_URL"],
        "autofix": "qwen",
        "write_path": _p_qwen_env(),
        "default_method": "config",
        "hint": ("Installed. Set OPENAI_API_BASE/OPENAI_API_KEY/OPENAI_MODEL, or run Auto-fix to write "
                 "them into ~/.qwen/.env (the CLI's own dotenv, not a global shell profile)."),
    },
    {
        "id": "llm",
        "name": "llm (Simon Willison)",
        "kind": "openai",
        "bins": ["llm"],
        "config_paths": [os.path.join(_llm_user_dir(), "extra-openai-models.yaml")],
        "env_check": ["OPENAI_BASE_URL", "OPENAI_API_BASE"],
        "autofix": None,  # needs a YAML *list* entry + `llm keys set`; safer to guide manually
        "default_method": "config",
        "hint": "Installed. Add an OpenAI-compatible model via extra-openai-models.yaml + `llm keys set` (see instructions).",
        "manual_note": (
            "`llm` needs an OpenAI-compatible model registered in %s :\n"
            "  - model_id: freehub\n"
            "    model_name: <provider>/<model>\n"
            "    api_base: http://127.0.0.1:%d/v1\n"
            "    api_key_name: freehub\n"
            "then run  llm keys set freehub  (paste the local key), and use  llm -m freehub ...  ."
            % (_short(os.path.join(_llm_user_dir(), "extra-openai-models.yaml")), PORT)
        ),
    },
    {
        "id": "cursor-agent",
        "name": "Cursor Agent CLI",
        "kind": "openai",
        "bins": ["cursor-agent"],
        "config_paths": [os.path.join(_home(), ".cursor", "cli-config.json"),
                         os.path.join(_home(), ".cursor", "config.json")],
        "env_check": ["OPENAI_BASE_URL", "ANTHROPIC_BASE_URL", "OPENAI_API_BASE"],
        "autofix": None,  # uncertain: custom-endpoint support is unofficial
        "default_method": "manual",
        "hint": "Installed. Custom-endpoint support is uncertain/unofficial (see instructions).",
        "manual_note": (
            "UNCERTAIN: cursor-agent authenticates to Cursor's own backend; pointing it at a custom "
            "OpenAI/Anthropic endpoint is not an officially documented flow. Only try the env vars below "
            "if your cursor-agent version explicitly supports a base-URL override."
        ),
    },
]

_CLI_BY_ID = {e["id"]: e for e in CLI_REGISTRY}


def _get_cli_entry(cid):
    return _CLI_BY_ID.get(cid)


# CLIs that are NOT wired through OPENAI_*/ANTHROPIC_* environment variables, so
# handing out an env-var block or unset commands for them is misleading:
#   codex  -> Responses API via ~/.codex/config.toml (Auto-fix writes it; no auth)
#   gemini -> Google-native wire format; this OpenAI/Anthropic hub can't serve it
#   llm    -> extra-openai-models.yaml + `llm keys` (not env vars)
_ENVLESS_CLIS = {"codex", "gemini", "llm"}


def _hub_fragments():
    """Substrings that, if present in a CLI's config/env, mean it points here.
    The PORT is the discriminator, so this matches both the bare origin and the
    /v1 form (http://127.0.0.1:<PORT>, http://127.0.0.1:<PORT>/v1, ...)."""
    return ["127.0.0.1:%d" % PORT, "localhost:%d" % PORT, "[::1]:%d" % PORT]


def _points_at_hub(val):
    """True if a config value (string) targets THIS hub's origin/port."""
    return isinstance(val, str) and any(fr in val for fr in _hub_fragments())


def _file_points_at_hub(path):
    """True if the file's raw text contains a hub origin substring. Fail-open:
    an unreadable file reads as 'not pointing here' (never raises)."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            txt = f.read()
    except OSError:
        return False
    return any(fr in txt for fr in _hub_fragments())


def _cli_connected(entry):
    """(connected, method, detail) — best-effort, never raises.
    method is 'env' or 'config' when connected, else the entry default."""
    frags = _hub_fragments()
    for ev in entry.get("env_check", []):
        val = os.environ.get(ev)
        if val and any(fr in val for fr in frags):
            return True, "env", "Connected via the %s environment variable." % ev
    for path in entry.get("config_paths", []):
        try:
            if not os.path.isfile(path):
                continue
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                txt = f.read()
        except OSError:
            continue  # fail open
        if any(fr in txt for fr in frags):
            return True, "config", "Connected via %s." % _short(path)
    return False, entry.get("default_method", "manual"), None


def _cli_installed(entry):
    for b in entry.get("bins", []):
        p = shutil.which(b)
        if p:
            return True, p
    return False, None


def _cli_row(entry):
    installed, path = _cli_installed(entry)
    connected, method, cdetail = _cli_connected(entry)
    if not installed:
        connected = False  # can't be "connected" if the binary isn't on PATH
        detail = "Not installed (looked for: %s)." % ", ".join(entry.get("bins", []))
    elif connected:
        detail = cdetail
    else:
        detail = entry.get("hint") or "Installed. Not pointed at this hub yet."
    return {
        "id": entry["id"],
        "name": entry["name"],
        "kind": entry["kind"],
        "installed": installed,
        "path": path,
        "connected": connected,
        "connect_method": method if connected else entry.get("default_method", "manual"),
        "detail": detail,
    }


def _first_free_model_id():
    """First aggregated (enabled+keyed) free model id '<pid>/<model>', or None."""
    models = aggregated_models()
    return models[0]["id"] if models else None


def _manual_env(entry, key, base_root, base_v1, model):
    """The env vars this CLI would need, resolved with the live port/key/model."""
    if entry["kind"] == "anthropic":
        return {"ANTHROPIC_BASE_URL": base_root,
                "ANTHROPIC_AUTH_TOKEN": key,
                "ANTHROPIC_MODEL": model}
    return {"OPENAI_API_BASE": base_v1,
            "OPENAI_BASE_URL": base_v1,
            "OPENAI_API_KEY": key,
            "OPENAI_MODEL": model}


def _env_commands(env):
    """Shell one-liners to set env vars (per-CLI, NOT a profile edit we make for
    the user — we only *print* these for them to run).
    Windows: emit BOTH `set "VAR=VALUE"` (takes effect in the CURRENT shell so the
    CLI works right now) AND `setx VAR "VALUE"` (persists for FUTURE shells) per
    var — setx alone never touches the live session. One command per line so the
    whole block stays copy-pasteable."""
    win_lines = []
    for k, v in env.items():
        win_lines.append('set "%s=%s"' % (k, v))   # current shell (this session)
        win_lines.append('setx %s "%s"' % (k, v))  # persist for future shells
    win = "\n".join(win_lines)
    unix = "\n".join("export %s='%s'" % (k, v) for k, v in env.items())
    return {"windows": win, "unix": unix}


def _backup_once(path):
    """Copy path -> path.freehub-bak exactly once (never clobber an existing
    backup). Returns the backup path if one exists, else None."""
    bak = path + ".freehub-bak"
    try:
        if os.path.isfile(path) and not os.path.exists(bak):
            shutil.copy2(path, bak)
        return bak if os.path.exists(bak) else None
    except OSError:
        return None


def _abort_if_backup_failed(path, backup):
    """Guard for the auto-fixers: if `path` is a NON-EMPTY existing file but
    `_backup_once` returned None (the backup genuinely failed), return an abort
    dict so we refuse to overwrite the user's only copy. Returns None when it's
    safe to proceed (no file, empty file, or a backup exists). Never raises."""
    try:
        if backup is None and os.path.isfile(path) and os.path.getsize(path) > 0:
            return {"ok": False,
                    "reason": "could not back up your existing config — refusing to overwrite it"}
    except OSError:
        # Can't even stat it -> be conservative and refuse rather than risk a loss.
        return {"ok": False,
                "reason": "could not back up your existing config — refusing to overwrite it"}
    return None


def _cli_write_text(path, text):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _yaml_dq(v):
    """Double-quote a scalar for a flat YAML value."""
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return '"%s"' % s


def _merge_flat_yaml(path, updates):
    """Additively set top-level `key: value` pairs in a flat YAML file,
    preserving every other line. Only rewrites lines whose top-level key matches
    (no indentation), appends the rest — safe for aider's flat conf."""
    lines = []
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.read().splitlines()
        except OSError:
            lines = []
    remaining = dict(updates)
    out = []
    for ln in lines:
        replaced = False
        for k in list(remaining):
            if ln.startswith(k + ":"):
                out.append("%s: %s" % (k, _yaml_dq(remaining[k])))
                del remaining[k]
                replaced = True
                break
        if not replaced:
            out.append(ln)
    for k, v in updates.items():
        if k in remaining:
            out.append("%s: %s" % (k, _yaml_dq(v)))
    return "\n".join(out).rstrip("\n") + "\n"


def _merge_dotenv(path, updates):
    """Additively set KEY=VALUE lines in a dotenv file, preserving other lines.
    Matches `KEY=` and `export KEY=` at line start."""
    lines = []
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.read().splitlines()
        except OSError:
            lines = []
    remaining = dict(updates)
    out = []
    for ln in lines:
        s = ln.strip()
        replaced = False
        for k in list(remaining):
            if s.startswith(k + "=") or s.startswith("export " + k + "="):
                out.append("%s=%s" % (k, remaining[k]))
                del remaining[k]
                replaced = True
                break
        if not replaced:
            out.append(ln)
    for k, v in updates.items():
        if k in remaining:
            out.append("%s=%s" % (k, v))
    return "\n".join(out).rstrip("\n") + "\n"


def _strip_lines(path, keys, matches):
    """Drop lines whose top-level identity is in `keys`, preserving every other
    line. `matches(stripped_line, key)` decides whether a line belongs to `key`.
    Returns (new_text, removed_count). new_text is '' when nothing is left."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.read().splitlines()
    except OSError:
        return None, 0
    out, removed = [], 0
    for ln in lines:
        s = ln.strip()
        if any(matches(s, k) for k in keys):
            removed += 1
            continue
        out.append(ln)
    text = "\n".join(out).rstrip("\n")
    return (text + "\n" if text else ""), removed


def _remove_flat_yaml_keys(path, keys):
    """Remove top-level `key: ...` lines (aider's flat conf)."""
    return _strip_lines(path, keys, lambda s, k: s.startswith(k + ":"))


def _remove_dotenv_keys(path, keys):
    """Remove `KEY=...` / `export KEY=...` lines (qwen's dotenv)."""
    return _strip_lines(
        path, keys,
        lambda s, k: s.startswith(k + "=") or s.startswith("export " + k + "="))


def _autofix_claude(entry, key, base_root, base_v1, model):
    path = _p_claude()
    data = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:  # tolerate a UTF-8 BOM
                data = json.load(f)
        except (OSError, ValueError):
            return {"ok": False, "reason": "existing %s is not valid JSON — fix or remove it, then retry."
                    % _short(path)}
        if not isinstance(data, dict):
            return {"ok": False, "reason": "existing %s is not a JSON object; not overwriting." % _short(path)}
    backup = _backup_once(path)
    abort = _abort_if_backup_failed(path, backup)
    if abort:
        return abort
    env = data.get("env")
    if not isinstance(env, dict):
        env = {}
    env["ANTHROPIC_BASE_URL"] = base_root
    env["ANTHROPIC_AUTH_TOKEN"] = key
    env["ANTHROPIC_MODEL"] = model
    data["env"] = env
    _cli_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return {
        "ok": True,
        "wrote_path": path,
        "backup_path": backup,
        "applied": {"file_key": "env", "ANTHROPIC_BASE_URL": base_root,
                    "ANTHROPIC_AUTH_TOKEN": _mask_key(key), "ANTHROPIC_MODEL": model},
        "restart_hint": "Restart Claude Code (open a new terminal) so it re-reads ~/.claude/settings.json.",
    }


def _autofix_aider(entry, key, base_root, base_v1, model):
    path = _p_aider()
    updates = {"openai-api-base": base_v1, "openai-api-key": key, "model": "openai/" + model}
    backup = _backup_once(path)
    abort = _abort_if_backup_failed(path, backup)  # scalar-replace: never overwrite an un-backed-up conf
    if abort:
        return abort
    _cli_write_text(path, _merge_flat_yaml(path, updates))
    return {
        "ok": True,
        "wrote_path": path,
        "backup_path": backup,
        "applied": {"openai-api-base": base_v1, "openai-api-key": _mask_key(key),
                    "model": "openai/" + model},
        "restart_hint": "Re-run aider in a new session; it reads ~/.aider.conf.yml on startup.",
    }


def _autofix_opencode(entry, key, base_root, base_v1, model):
    path = _p_opencode()
    data = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:  # tolerate a UTF-8 BOM
                data = json.load(f)
        except (OSError, ValueError):
            return {"ok": False, "reason": ("existing %s is not valid JSON (jsonc comments aren't "
                    "auto-merged) — configure it by hand, then retry." % _short(path))}
        if not isinstance(data, dict):
            return {"ok": False, "reason": "existing %s is not a JSON object; not overwriting." % _short(path)}
    backup = _backup_once(path)
    abort = _abort_if_backup_failed(path, backup)
    if abort:
        return abort
    data.setdefault("$schema", "https://opencode.ai/config.json")
    providers = data.get("provider")
    if not isinstance(providers, dict):
        providers = {}
    providers["free-llm-hub"] = {
        "npm": "@ai-sdk/openai-compatible",
        "name": "Free LLM Hub",
        "options": {"baseURL": base_v1, "apiKey": key},
        "models": {model: {"name": model}},
    }
    data["provider"] = providers
    data["model"] = "free-llm-hub/" + model
    _cli_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return {
        "ok": True,
        "wrote_path": path,
        "backup_path": backup,
        "applied": {"provider": "free-llm-hub", "baseURL": base_v1, "apiKey": _mask_key(key),
                    "model": "free-llm-hub/" + model},
        "restart_hint": ("Restart opencode. If it complains about the provider, run its install/auth "
                         "step for @ai-sdk/openai-compatible (schema varies by version)."),
    }


def _autofix_qwen(entry, key, base_root, base_v1, model):
    path = _p_qwen_env()
    updates = {"OPENAI_API_BASE": base_v1, "OPENAI_BASE_URL": base_v1,
               "OPENAI_API_KEY": key, "OPENAI_MODEL": model}
    backup = _backup_once(path)
    abort = _abort_if_backup_failed(path, backup)  # scalar-replace: never overwrite an un-backed-up .env
    if abort:
        return abort
    _cli_write_text(path, _merge_dotenv(path, updates))
    return {
        "ok": True,
        "wrote_path": path,
        "backup_path": backup,
        "applied": {"OPENAI_API_BASE": base_v1, "OPENAI_BASE_URL": base_v1,
                    "OPENAI_API_KEY": _mask_key(key), "OPENAI_MODEL": model},
        "restart_hint": "Re-run `qwen` in a new terminal; it loads ~/.qwen/.env on startup.",
    }


_CODEX_TABLE_RE = re.compile(r"^\s*\[")


def _codex_apply_text(text, base_v1, bearer=None):
    """Pure transform for ~/.codex/config.toml (no IO). ADDITIVELY + REVERSIBLY:
      1. In the TOP section (every line before the first '[table]' header — the
         only place bare keys are valid TOML) replace an existing model_provider=
         line with model_provider = "freehub" (else prepend it), and likewise
         force model = "auto".
      2. Append a [model_providers.freehub] table once (wire_api = "responses",
         env_key = FREE_LLM_HUB_KEY) if the file doesn't already declare it.
    Every other line (model_reasoning_effort, [mcp_servers.*], other providers,
    comments) is preserved verbatim. Returns the new file text."""
    lines = text.splitlines()
    top, rest, in_rest = [], [], False
    for ln in lines:
        if not in_rest and _CODEX_TABLE_RE.match(ln):
            in_rest = True
        (rest if in_rest else top).append(ln)

    def _set_top_key(key_name, value_line):
        pat = re.compile(r"^\s*%s\s*=" % re.escape(key_name))
        for i, ln in enumerate(top):
            if pat.match(ln):
                top[i] = value_line
                return
        top.insert(0, value_line)

    _set_top_key("model_provider", 'model_provider = "freehub"')
    _set_top_key("model", 'model = "auto"')

    # Drop any pre-existing [model_providers.freehub] table so we always rewrite it
    # clean (e.g. strip a stale env_key from an earlier autofix). Skip from that
    # header to the next '[table]' header (or EOF).
    cleaned, skip = [], False
    for ln in rest:
        if _CODEX_TABLE_RE.match(ln):
            skip = (ln.strip() == "[model_providers.freehub]")
        if not skip:
            cleaned.append(ln)
    rest = cleaned

    block = [
        "[model_providers.freehub]",
        'name = "Free LLM Hub"',
        'base_url = "%s"' % base_v1,
        'wire_api = "responses"',
    ]
    if bearer:
        # Hub requires a local key -> embed it directly (works in every terminal,
        # no env var). It is the user's own key in their own local config file.
        block.append('experimental_bearer_token = "%s"' % bearer)
    # else: NO auth field at all -> Codex connects to the localhost hub
    # unauthenticated (the hub is open on 127.0.0.1). Zero env-var setup.

    new_text = "\n".join(top + rest).rstrip("\n")
    new_text = (new_text + "\n\n" if new_text else "") + "\n".join(block) + "\n"
    return new_text


def _autofix_codex(entry, key, base_root, base_v1, model):
    """Point the OpenAI Codex CLI at this hub. Codex only supports
    wire_api="responses" (served by POST /v1/responses). Edits config.toml
    additively/reversibly; a .freehub-bak backup is made first. NO auth is
    written when the hub is open on localhost (Codex connects unauthenticated —
    zero env-var setup); if the hub has a local key it is embedded directly as
    experimental_bearer_token. Only real caveat: restart an already-open Codex
    session so it re-reads config.toml."""
    path = _p_codex()
    backup = _backup_once(path)
    abort = _abort_if_backup_failed(path, backup)
    if abort:
        return abort
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        else:
            text = ""
    except OSError as exc:
        return {"ok": False, "reason": _sanitize("could not read %s: %s" % (_short(path), exc))}
    bearer = config.get_local_api_key()  # None -> write NO auth (cleanest); set -> embed token
    _cli_write_text(path, _codex_apply_text(text, base_v1, bearer))
    if bearer:
        note = ("Connected. The hub key is written straight into Codex's config "
                "(no environment variable needed) — works in any terminal. If Codex "
                "was already open, restart it (or run /model).")
    else:
        note = ("Connected. Codex now talks to the hub with NO auth required — works in "
                "any terminal immediately, nothing else to set. If Codex was already "
                "open, restart it (or run /model) to pick up the new config.")
    return {
        "ok": True,
        "wrote_path": path,
        "backup_path": backup,
        "applied": {"file_top": {"model_provider": "freehub", "model": "auto"},
                    "table": "[model_providers.freehub]", "base_url": base_v1,
                    "wire_api": "responses",
                    "auth": ("experimental_bearer_token" if bearer else "none (open localhost)")},
        "note": note,
        "restart_hint": "Restart Codex if it was already running (config is re-read on start).",
    }


_AUTOFIXERS = {
    "claude": _autofix_claude,
    "aider": _autofix_aider,
    "opencode": _autofix_opencode,
    "qwen": _autofix_qwen,
    "codex": _autofix_codex,
}


# --- Disconnect / revert: turn an auto-fixed CLI back to its NORMAL config ----
# Each reverter restores the pre-autofix state of the CLI's OWN config file:
#   1. if a <write_path>.freehub-bak backup exists (autofix made one), copy it
#      back verbatim and delete the backup — this is the user's 'normal case';
#   2. otherwise (autofix created a fresh file) strip ONLY the hub-specific keys
#      we added, leaving every unrelated user setting untouched.
# Never raises for expected IO/parse issues; the route wraps OSErrors. Returns a
# dict with restored_from_backup / wrote_path / restart_hint (no secrets).

def _restore_backup(path):
    """Copy <path>.freehub-bak back over path and delete the backup.
    Returns True if a backup existed and was restored."""
    bak = path + ".freehub-bak"
    if not os.path.isfile(bak):
        return False
    shutil.copy2(bak, path)
    try:
        os.remove(bak)
    except OSError:
        pass  # restore succeeded; a lingering backup is harmless
    return True


def _discard_backup(path):
    """Delete a <path>.freehub-bak backup if present (best-effort, never raises).
    Called after a successful NON-destructive strip revert so the frozen backup —
    which was captured at FIRST connect and is now stale — can never later shadow
    or overwrite config the user added after connecting."""
    bak = path + ".freehub-bak"
    try:
        if os.path.isfile(bak):
            os.remove(bak)
    except OSError:
        pass


def _disconnect_claude(entry):
    path = entry["write_path"]
    hint = "Restart Claude Code (open a new terminal) so it re-reads ~/.claude/settings.json."
    # #2 STRUCTURED-CONFIG revert: prefer the NON-DESTRUCTIVE strip path over
    # restoring the .freehub-bak backup. The backup is frozen at FIRST connect, so
    # restoring it would silently wipe any MCP servers / settings the user added to
    # settings.json AFTER connecting. As long as the live file still parses as JSON
    # we strip ONLY our three env keys and keep everything else, then drop the stale
    # backup. The backup restore is a last resort for a file that no longer parses.
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:  # tolerate a UTF-8 BOM
                data = json.load(f)
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            changed = False
            env = data.get("env")
            # Only touch keys we set, and only when the base URL is ours.
            if isinstance(env, dict) and _points_at_hub(env.get("ANTHROPIC_BASE_URL")):
                for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL"):
                    if env.pop(k, None) is not None:
                        changed = True
                if not env:
                    data.pop("env", None)
            if changed:
                _cli_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
            _discard_backup(path)  # strip succeeded -> stale backup no longer needed
            return {"restored_from_backup": False, "wrote_path": path,
                    "changed": changed, "restart_hint": hint}
    # Live file missing or no longer valid JSON -> fall back to the frozen backup.
    if _restore_backup(path):
        return {"restored_from_backup": True, "wrote_path": path, "restart_hint": hint}
    return {"restored_from_backup": False, "wrote_path": path,
            "changed": False, "restart_hint": hint}


def _disconnect_aider(entry):
    path = entry["write_path"]
    hint = "Re-run aider in a new session; it reads ~/.aider.conf.yml on startup."
    if _restore_backup(path):
        return {"restored_from_backup": True, "wrote_path": path, "restart_hint": hint}
    changed = False
    if os.path.isfile(path) and _file_points_at_hub(path):
        text, removed = _remove_flat_yaml_keys(
            path, ["openai-api-base", "openai-api-key", "model"])
        if text is not None and removed:
            _cli_write_text(path, text)
            changed = True
    return {"restored_from_backup": False, "wrote_path": path,
            "changed": changed, "restart_hint": hint}


def _disconnect_opencode(entry):
    path = entry["write_path"]
    hint = "Restart opencode so it re-reads ~/.config/opencode/opencode.json."
    # #2 STRUCTURED-CONFIG revert: strip ONLY our provider + model entries so any
    # provider/agent/setting the user added after connecting survives — never blind-
    # restore the stale first-connect backup. Backup restore is the last resort for
    # a file that no longer parses as JSON.
    changed = False
    deleted = False
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:  # tolerate a UTF-8 BOM
                data = json.load(f)
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            providers = data.get("provider")
            if isinstance(providers, dict) and providers.pop("free-llm-hub", None) is not None:
                changed = True
                if not providers:
                    data.pop("provider", None)
            m = data.get("model")
            if isinstance(m, str) and m.startswith("free-llm-hub/"):
                data.pop("model", None)
                changed = True
            if changed:
                # A lone "$schema" (or nothing) left means the file holds nothing of
                # the user's — remove it for a clean revert instead of leaving a
                # {"$schema": ...} shell. ANY other remaining key means the user
                # added real content -> keep it (this is the invariant #2 protects).
                remaining = set(data.keys())
                if not remaining or remaining == {"$schema"}:
                    try:
                        os.remove(path)
                        deleted = True
                    except OSError:
                        _cli_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
                else:
                    _cli_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
            _discard_backup(path)  # strip succeeded -> stale backup no longer needed
            out = {"restored_from_backup": False, "wrote_path": path,
                   "changed": changed, "restart_hint": hint}
            if deleted:
                out["deleted"] = True
            return out
    # Live file missing or no longer valid JSON -> fall back to the frozen backup.
    if _restore_backup(path):
        return {"restored_from_backup": True, "wrote_path": path, "restart_hint": hint}
    return {"restored_from_backup": False, "wrote_path": path,
            "changed": False, "restart_hint": hint}


def _disconnect_qwen(entry):
    path = entry["write_path"]
    hint = "Re-run `qwen` in a new terminal; it reloads ~/.qwen/.env on startup."
    if _restore_backup(path):
        return {"restored_from_backup": True, "wrote_path": path, "restart_hint": hint}
    changed = False
    deleted = False
    if os.path.isfile(path) and _file_points_at_hub(path):
        text, removed = _remove_dotenv_keys(
            path, ["OPENAI_API_BASE", "OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL"])
        if text is not None and removed:
            # No backup existed -> Auto-fix CREATED this .env. If removing our 4 keys
            # leaves it empty, delete it for a clean revert rather than leaving an
            # empty file lying around.
            if text.strip() == "":
                try:
                    os.remove(path)
                    deleted = True
                except OSError:
                    _cli_write_text(path, text)
            else:
                _cli_write_text(path, text)
            changed = True
    out = {"restored_from_backup": False, "wrote_path": path,
           "changed": changed, "restart_hint": hint}
    if deleted:
        out["deleted"] = True
    return out


def _remove_toml_table(text, table_name):
    """Remove a '[<table_name>]' block (its header line through the line before
    the next '[table]' header, or EOF). Returns (new_text, removed_bool)."""
    lines = text.splitlines()
    target = re.compile(r"^\s*\[\s*%s\s*\]\s*$" % re.escape(table_name))
    out, removed, i, n = [], False, 0, len(lines)
    while i < n:
        if target.match(lines[i]):
            removed = True
            i += 1
            while i < n and not _CODEX_TABLE_RE.match(lines[i]):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    new_text = "\n".join(out).rstrip("\n")
    return (new_text + "\n" if new_text else ""), removed


def _strip_codex_top_keys(text):
    """Remove our exact 'model_provider = "freehub"' / 'model = "auto"' lines from
    the TOP (pre-table) section only. Returns (new_text, removed_bool)."""
    lines = text.splitlines()
    mp = re.compile(r'^\s*model_provider\s*=\s*"freehub"\s*$')
    md = re.compile(r'^\s*model\s*=\s*"auto"\s*$')
    out, removed, in_rest = [], False, False
    for ln in lines:
        if not in_rest and _CODEX_TABLE_RE.match(ln):
            in_rest = True
        if not in_rest and (mp.match(ln) or md.match(ln)):
            removed = True
            continue
        out.append(ln)
    new_text = "\n".join(out).rstrip("\n")
    return (new_text + "\n" if new_text else ""), removed


def _disconnect_codex(entry):
    path = entry.get("write_path") or _p_codex()
    hint = "Restart Codex (open a new terminal) so it re-reads ~/.codex/config.toml."
    # #2 STRUCTURED-CONFIG revert: strip ONLY our additions (the
    # [model_providers.freehub] table + the two top keys we set) so any provider /
    # [mcp_servers.*] / setting the user added to config.toml after connecting
    # survives — never blind-restore the stale first-connect backup. Restore the
    # frozen backup only if the file can't be read at all.
    # Trade-off (documented): if the user had their OWN model/model_provider before
    # connecting, autofix overwrote those scalars and the strip removes them rather
    # than restoring the originals — the same limitation as the line-based CLIs.
    # Preserving newly-added MCP servers outweighs restoring a trivially-reset scalar.
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except OSError:
            text = None
        if text is not None:
            text2, tbl_removed = _remove_toml_table(text, "model_providers.freehub")
            text3, top_removed = _strip_codex_top_keys(text2)
            changed = False
            if tbl_removed or top_removed:
                _cli_write_text(path, text3)
                changed = True
            _discard_backup(path)  # strip succeeded -> stale backup no longer needed
            return {"restored_from_backup": False, "wrote_path": path,
                    "changed": changed, "restart_hint": hint}
    # Unreadable / missing -> fall back to the frozen backup.
    if _restore_backup(path):
        return {"restored_from_backup": True, "wrote_path": path, "restart_hint": hint}
    return {"restored_from_backup": False, "wrote_path": path,
            "changed": False, "restart_hint": hint}


_LLM_ITEM_RE = re.compile(r"^\s*-\s+\S")
_LLM_FREEHUB_RE = re.compile(r"^\s*-?\s*model_id\s*:\s*[\"']?freehub[\"']?\s*$")


def _remove_llm_freehub_entry(text):
    """Strip the `model_id: freehub` block item from an llm extra-openai-models.yaml
    (a YAML block *list* of model dicts), preserving every other item plus any
    leading comments/preamble. Line-based (no PyYAML dependency): split the file
    into list items on `- ` header lines, drop the item whose block declares
    `model_id: freehub`, keep the rest. Returns (new_text, removed_bool)."""
    lines = text.splitlines()
    preamble, items, cur = [], [], None
    for ln in lines:
        if _LLM_ITEM_RE.match(ln):
            if cur is not None:
                items.append(cur)
            cur = [ln]
        elif cur is None:
            preamble.append(ln)
        else:
            cur.append(ln)
    if cur is not None:
        items.append(cur)
    kept, removed = [], False
    for block in items:
        if any(_LLM_FREEHUB_RE.match(l) for l in block):
            removed = True
            continue
        kept.append(block)
    if not removed:
        return text, False
    out_lines = list(preamble)
    for block in kept:
        out_lines.extend(block)
    new_text = "\n".join(out_lines).rstrip("\n")
    # Nothing but our entry existed -> leave a valid (empty) YAML file.
    return (new_text + "\n" if new_text.strip() else ""), True


def _disconnect_llm(entry):
    """Revert Simon Willison's `llm`: remove the `model_id: freehub` entry from
    extra-openai-models.yaml (its REAL connection surface — `llm` never used env
    vars here), preserving every other registered model. The saved key lives in
    llm's own encrypted keys store, which we never touch — the restart hint tells
    the user to run `llm keys remove freehub` to drop it. Never raises for
    expected IO."""
    path = (entry.get("config_paths") or [None])[0] \
        or os.path.join(_llm_user_dir(), "extra-openai-models.yaml")
    hint = ("Removed the freehub model from extra-openai-models.yaml. Also run  "
            "llm keys remove freehub  to delete the stored key, then re-run llm.")
    changed = False
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except OSError:
            text = None
        if text is not None:
            new_text, removed = _remove_llm_freehub_entry(text)
            if removed:
                _cli_write_text(path, new_text)
                changed = True
    return {"restored_from_backup": False, "wrote_path": path,
            "changed": changed, "restart_hint": hint,
            "note": "Also run  llm keys remove freehub  to remove the saved key."}


_DISCONNECTERS = {
    "claude": _disconnect_claude,
    "aider": _disconnect_aider,
    "opencode": _disconnect_opencode,
    "qwen": _disconnect_qwen,
    "codex": _disconnect_codex,
    "llm": _disconnect_llm,   # id-keyed: `llm` has no autofix strategy string
}


def _env_unset_commands(entry):
    """Copy-paste commands to REMOVE the hub env vars a manual CLI would use.
    Names only — never a value, so no secret can leak."""
    if entry.get("kind") == "anthropic":
        names = ["ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL"]
    else:
        names = ["OPENAI_API_BASE", "OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL"]
    # Windows: `setx VAR ""` stores an EMPTY value, it does NOT delete the var, so
    # the CLI still sees a (blank) override. Actually remove it: `reg delete` drops
    # the persisted user var (future shells) and `set "VAR="` clears it in the
    # CURRENT shell. Unix `unset` already removes it outright.
    win_lines = []
    for n in names:
        win_lines.append('set "%s="' % n)                                # current shell
        win_lines.append('reg delete "HKCU\\Environment" /F /V %s' % n)   # future shells
    win = "\n".join(win_lines)
    unix = "\n".join("unset %s" % n for n in names)
    return {"windows": win, "unix": unix}


def _hub_serves_now():
    """In-process: route + call one free provider with a 1-token prompt. Returns
    (served_label, reply_snippet) or (None, None). Proves the hub pipeline works."""
    pid, model, _diff = _route_by_difficulty([{"role": "user", "content": "hi"}], 8)
    if not pid:
        return None, None
    for hop_pid, hop_model in _build_chain(pid, model):
        payload = {"model": hop_model, "max_tokens": 8, "stream": False,
                   "messages": [{"role": "user", "content": "Reply with the single word: OK"}]}
        try:
            resp = _upstream_chat(hop_pid, payload, False)
        except Exception:
            continue
        if resp.status_code == 200:
            try:
                j = resp.json()
                reply = ((j.get("choices") or [{}])[0].get("message") or {}).get("content", "")
            except Exception:
                reply = ""
            resp.close()
            return hop_pid + "/" + hop_model, (reply or "").strip()[:120]
        resp.close()
    return None, None


def _cli_test(entry):
    """REALLY test a CLI's connection, reliably (never hangs, ~2-5s):
      1. is the CLI installed?
      2. is its config actually pointed at THIS hub? (parsed from its own file)
      3. does the hub serve that CLI's protocol RIGHT NOW? (live 1-token call)
    Passing 2+3 means the CLI is wired to a hub that is answering — the practical
    definition of 'connected and working'. Never raises."""
    cid = entry["id"]
    row = _cli_row(entry)
    name = entry.get("name", cid)
    if not row.get("installed"):
        return {"ok": False, "stage": "install", "installed": False,
                "detail": "%s is not installed on this machine." % name}
    connected = bool(row.get("connected"))
    if not connected:
        return {"ok": False, "stage": "config", "installed": True, "connected": False,
                "detail": "%s is installed but its config is NOT pointed at the hub. "
                          "Click Connect first." % name}
    try:
        served, reply = _hub_serves_now()
    except Exception as exc:
        return {"ok": False, "stage": "hub", "connected": True,
                "detail": _sanitize("%s: %s" % (exc.__class__.__name__, exc))}
    if served:
        return {"ok": True, "stage": "done", "installed": True, "connected": True,
                "model": served, "reply": reply,
                "detail": "%s is wired to the hub, and the hub answered a live test — %s said: %s"
                          % (name, served, reply or "OK")}
    return {"ok": False, "stage": "hub", "connected": True,
            "detail": "%s points at the hub, but the hub got no reply from any free provider "
                      "(check provider keys / quota)." % name}


@app.route("/api/clis/<cid>/test", methods=["POST"])
def api_cli_test(cid):
    entry = _get_cli_entry(cid)
    if not entry:
        return jsonify({"error": "unknown CLI '%s'" % cid}), 404
    return jsonify(_cli_test(entry))


@app.route("/api/clis", methods=["GET"])
def api_clis():
    return jsonify([_cli_row(e) for e in CLI_REGISTRY])


@app.route("/api/clis/<cid>/autofix", methods=["POST"])
def api_cli_autofix(cid):
    entry = _get_cli_entry(cid)
    if not entry:
        return jsonify({"error": "unknown CLI '%s'" % cid}), 404
    key = config.get_local_api_key() or "free-llm-hub"
    base_root = "http://127.0.0.1:%d" % PORT
    base_v1 = base_root + "/v1"
    strategy = entry.get("autofix")
    if not strategy:
        # No autofix strategy. For CLIs that AREN'T wired via env vars (gemini is
        # protocol-incompatible; llm uses extra-openai-models.yaml + `llm keys`),
        # handing back OPENAI_* commands is misleading -> return the note only.
        if entry["id"] in _ENVLESS_CLIS:
            return jsonify({
                "ok": False, "manual": True,
                "note": entry.get("manual_note", "See this CLI's setup details."),
            })
        # env-based but uncertain (e.g. cursor-agent): never touch a global shell
        # profile — hand back copy-paste commands + a CLI-specific note instead.
        model = _first_free_model_id() or _suggested_model()
        env = _manual_env(entry, key, base_root, base_v1, model)
        return jsonify({
            "ok": False, "manual": True,
            "commands": _env_commands(env),
            "note": entry.get("manual_note", "Configure this CLI manually with the env vars above."),
        })
    model = _first_free_model_id()
    if not model:
        return jsonify({"ok": False, "reason": "no free model configured yet — add a provider key first"})
    fixer = _AUTOFIXERS.get(strategy)
    if not fixer:
        return jsonify({"ok": False, "reason": "no autofix strategy '%s'" % strategy})
    try:
        result = fixer(entry, key, base_root, base_v1, model)
    except OSError as exc:
        return jsonify({"ok": False, "reason": _sanitize("could not write config: %s" % exc)})
    return jsonify(result)


@app.route("/api/clis/<cid>/disconnect", methods=["POST"])
def api_cli_disconnect(cid):
    """Revert a CLI to its NORMAL (non-hub) config. For an auto-fixable CLI this
    restores the .freehub-bak backup (the user's original file) or, if autofix
    created a fresh file, strips only the hub keys we added. Manual-only CLIs get
    copy-paste unset commands instead. Never 500s; never logs a secret."""
    entry = _get_cli_entry(cid)
    if not entry:
        return jsonify({"error": "unknown CLI '%s'" % cid}), 404
    strategy = entry.get("autofix")
    # Reverters are resolved by autofix strategy first, then by CLI id — so a CLI
    # with no autofix strategy but a real config surface (e.g. `llm`'s YAML) can
    # still register a disconnecter under its id.
    reverter = (_DISCONNECTERS.get(strategy) if strategy else None) \
        or _DISCONNECTERS.get(entry["id"])
    if not reverter:
        # Manual-only (protocol-incompatible / uncertain): we never wrote this
        # CLI's config, so we can't safely revert it — guide the user.
        if entry["id"] in _ENVLESS_CLIS:
            # Never wired via OPENAI_*/ANTHROPIC_* env vars -> no bogus unset block.
            return jsonify({
                "ok": False, "manual": True,
                "note": (entry.get("manual_note")
                         or "This CLI isn't wired through environment variables; nothing to unset."),
            })
        return jsonify({
            "ok": False, "manual": True,
            "note": ("This CLI was configured manually; remove the hub env vars/config "
                     "yourself"),
            "commands": _env_unset_commands(entry),
        })
    try:
        result = reverter(entry)
    except OSError as exc:
        return jsonify({"ok": False, "reason": _sanitize("could not restore config: %s" % exc)})
    # Recompute freshly from disk so 'connected' reflects the revert immediately.
    connected = _cli_row(entry).get("connected")
    out = {
        "ok": True,
        "restored_from_backup": bool(result.get("restored_from_backup")),
        "wrote_path": result.get("wrote_path"),
        "restart_hint": result.get("restart_hint"),
        "connected": bool(connected),
    }
    if "changed" in result:
        out["changed"] = bool(result["changed"])
    return jsonify(out)


@app.route("/api/clis/<cid>/instructions", methods=["GET"])
def api_cli_instructions(cid):
    entry = _get_cli_entry(cid)
    if not entry:
        return jsonify({"error": "unknown CLI '%s'" % cid}), 404
    key = config.get_local_api_key() or "free-llm-hub"
    model = _first_free_model_id() or _suggested_model()
    base_root = "http://127.0.0.1:%d" % PORT
    base_v1 = base_root + "/v1"
    snippets = _connect_snippets()
    # Some CLIs are NOT wired via OPENAI_*/ANTHROPIC_* env vars (codex -> config.toml,
    # gemini -> incompatible, llm -> extra-openai-models.yaml). For those, an env
    # block / OpenAI snippet is misleading — show the note + config path only.
    env_based = entry["id"] not in _ENVLESS_CLIS
    steps = []
    if entry.get("autofix"):
        steps.append("Auto-fix (recommended): POST /api/clis/%s/autofix to write %s for you "
                     "(a .freehub-bak backup is made first)." % (entry["id"], _short(entry.get("write_path", "the CLI config"))))
    if env_based:
        steps.append("Manual: set the environment variables in `env` below, then restart the CLI.")
    else:
        steps.append("Manual: follow the note below — this CLI is not wired through "
                     "OPENAI_*/ANTHROPIC_* environment variables.")
    if entry.get("manual_note"):
        steps.append(entry["manual_note"])
    steps.append("Verify: run `%s` and confirm it answers via this hub using %s." % (entry["bins"][0], model))
    # Env block is resolved with the SAME model id the auto-fixers write, and the
    # cross-platform commands (setx AND export) are emitted here too so a Windows
    # user on the manual path isn't handed a unix-only `export`.
    env = _manual_env(entry, key, base_root, base_v1, model) if env_based else {}
    out = {
        "steps": steps,
        "env": env,
        "snippet_openai": snippets["openai"] if env_based else None,
        "snippet_anthropic": snippets["claude_code"] if env_based else None,
    }
    if env_based:
        out["commands"] = _env_commands(env)
    return jsonify(out)


# ---------------------------------------------------------------------------
# OpenAI-compatible gateway
# ---------------------------------------------------------------------------

@app.route("/v1/models", methods=["GET"])
def v1_models():
    agg = aggregated_models()
    data = [{"id": m["id"], "object": "model", "created": 0, "owned_by": m["provider"]}
            for m in agg]
    # Also expose a `models` array (id + slug). Some clients (e.g. Codex's model
    # manager) expect that field and log a decode error against the OpenAI-only
    # {data:[...]} shape. Additive — OpenAI clients keep reading `data`.
    models = [{"id": m["id"], "slug": m["id"], "object": "model",
               "owned_by": m["provider"]} for m in agg]
    return jsonify({"object": "list", "data": data, "models": models})


_MISSING = object()  # sentinel: "no pre-read first item" for the peeked streamers


def _chain_first(first, iterator):
    """Yield `first` (unless it's the _MISSING sentinel) then the rest of
    `iterator` — used to prepend a first item pulled during the first-byte peek
    back onto the stream so no chunk is lost."""
    if first is not _MISSING:
        yield first
    for item in iterator:
        yield item


def _peek_first_chunk(iterator, deadline_s):
    """First-byte peek for streaming fallback (#4). Pull the FIRST item from an
    already-created `iterator` (resp.iter_content(...) for raw SSE, or
    resp.iter_lines(...) for the translating parsers) in a daemon worker thread
    bounded to ~deadline_s. Returns (ok, first):
      ok=True  -> `first` is the first item; the upstream is responsive. The worker
                  has finished (join returned), so the caller keeps iterating the
                  SAME iterator sequentially — no concurrency, no lost/duplicated
                  item.
      ok=False -> no usable first byte: the read timed out (slow/hung provider), the
                  stream ended immediately (StopIteration), or the read errored. The
                  caller should resp.close() and fall through to the next provider.
    A worker still blocked past the deadline is abandoned (daemon); the caller's
    resp.close() unblocks/ends its read. requests' STREAM_IDLE_TIMEOUT read timeout
    is the hard backstop if the tighter join deadline is ever exceeded."""
    box = {}

    def _worker():
        try:
            box["v"] = next(iterator)
            box["ok"] = True
        except StopIteration:
            box["ok"] = False
        except Exception:  # requests read timeout / connection reset / etc.
            box["ok"] = False

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(deadline_s)
    if t.is_alive() or not box.get("ok"):
        return False, None
    return True, box.get("v")


def _proxy_sse(resp, iterator=None, first=_MISSING):
    """Pass upstream SSE bytes through unchanged. When `iterator`/`first` are
    supplied (the first-byte peek already pulled the first chunk from this exact
    iterator), yield that chunk first, then continue the SAME iterator — so the
    fast-path byte stream is byte-for-byte identical to before. Empty chunks are
    still filtered exactly as before."""
    try:
        if iterator is None:
            iterator = resp.iter_content(chunk_size=None)
        for chunk in _chain_first(first, iterator):
            if chunk:
                yield chunk
    finally:
        resp.close()


_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@app.route("/v1/chat/completions", methods=["POST"])
def v1_chat_completions():
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return _openai_error("Invalid JSON body.", 400)
    # Orchestrate (Auto): route by task difficulty AND request size so weak/small
    # providers take easy work and big requests avoid small-TPM providers (413).
    # Explicit '<pid>/<model>' bypasses model choice (chain still size-filters).
    est = _est_tokens(body.get("messages"), body.get("tools"))
    diff = None
    if _is_orchestrate(body.get("model")):
        pid, resolved, diff = _route_by_difficulty(body.get("messages"),
                                                    body.get("max_tokens"), est)
        if pid is None:
            pid, resolved = _resolve_model(body.get("model"))  # default/best or error
    else:
        pid, resolved = _resolve_model(body.get("model"))
    if pid is None:
        return _openai_error(resolved, 400)
    if not prov.is_model_allowed(resolved):
        return _openai_error("Model '%s' is blocked by the safety filter." % resolved, 403,
                             "permission_error")
    not_ready = _check_provider_ready(pid)
    if not_ready:
        return _openai_error(not_ready, 400)

    stream = bool(body.get("stream"))
    errors = []
    last_hard = None  # last hard (non-retryable) upstream error, relayed if the chain is exhausted
    for hop_pid, hop_model in _build_chain(pid, resolved, est):
        if not prov.is_model_allowed(hop_model):
            continue
        payload = dict(body)
        payload["model"] = hop_model
        _apply_reasoning_effort(payload, hop_model, diff)
        try:
            _act_pick(hop_pid, hop_model)
            resp = _upstream_chat(hop_pid, payload, stream)
        except (requests.RequestException, RuntimeError) as exc:
            errors.append("%s: %s" % (hop_pid, _sanitize(exc.__class__.__name__)))
            continue
        if resp.status_code == 200:
            if stream:
                # #4: peek the first byte BEFORE committing the 200. A hung/slow
                # stream (no first byte within STREAM_FIRST_BYTE_TIMEOUT) falls
                # through to the next provider instead of stalling the client.
                it = resp.iter_content(chunk_size=None)
                ok, first = _peek_first_chunk(it, STREAM_FIRST_BYTE_TIMEOUT)
                if not ok:
                    errors.append("%s: no first byte within %ds"
                                  % (hop_pid, STREAM_FIRST_BYTE_TIMEOUT))
                    resp.close()
                    continue
                return Response(stream_with_context(_proxy_sse(resp, it, first)),
                                mimetype="text/event-stream", headers=_SSE_HEADERS)
            try:
                data = resp.json()
            except ValueError:
                return _openai_error("Upstream returned non-JSON (%s, HTTP 200): %s"
                                     % (hop_pid, _sanitize(resp.text)), 502, "upstream_error")
            if isinstance(data, dict):
                data["model"] = hop_pid + "/" + hop_model
            return jsonify(data), 200
        # Non-2xx. Retryable (429/5xx) and HARD errors (404/400/model-not-found)
        # both advance to the NEXT provider — each chain hop is a DIFFERENT
        # provider, so a broken model/provider should fall through before we give
        # up. Key rotation for the SAME provider already happened in _upstream_chat.
        errors.append("%s: HTTP %d" % (hop_pid, resp.status_code))
        if not _retryable(resp.status_code):
            # Capture the body once so the last hard error can be relayed verbatim
            # after the chain is exhausted (retryable errors stay generic 502).
            try:
                body_json = resp.json()
                body_text = None
            except ValueError:
                body_json = None
                body_text = _sanitize(resp.text)
            last_hard = {"pid": hop_pid, "status": resp.status_code,
                         "json": body_json, "text": body_text}
        resp.close()
        continue
    if last_hard is not None:
        if last_hard["json"] is not None:
            return jsonify(last_hard["json"]), last_hard["status"]
        return _openai_error("Upstream returned non-JSON (%s, HTTP %d): %s"
                             % (last_hard["pid"], last_hard["status"], last_hard["text"]),
                             502, "upstream_error")
    return _openai_error("All providers failed: " + ("; ".join(errors) or "none available"),
                         502, "upstream_error")


# ---------------------------------------------------------------------------
# OpenAI Responses API gateway (OpenAI Codex CLI support)
# ---------------------------------------------------------------------------
# Codex (2026+) speaks ONLY the Responses API (wire_api="responses"), never chat
# completions. Strategy: translate the Responses request DOWN to OpenAI chat
# messages, reuse the SAME difficulty routing + provider chain + key rotation +
# fallback as /v1/chat/completions, then translate the chat result (JSON or SSE)
# back UP into Responses objects/events. No new orchestration is introduced.


def _responses_tools_to_chat(tools):
    """Responses tools ({"type":"function","name","description","parameters"},
    sometimes nested under a "function" key) -> OpenAI chat tools."""
    out = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        inner = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = inner.get("name")
        if not name:
            continue
        out.append({"type": "function", "function": {
            "name": name,
            "description": inner.get("description") or "",
            "parameters": inner.get("parameters") or {"type": "object", "properties": {}},
        }})
    return out


# Responses/Codex roles -> roles every OpenAI-chat model template accepts. The
# big one: Codex sends its system prompt with role "developer", which most open
# model chat templates reject ("Unexpected message role"). Map it to system.
_RESP_ROLE_MAP = {"system": "system", "developer": "system", "user": "user",
                  "assistant": "assistant", "tool": "tool"}


def _norm_role(role):
    return _RESP_ROLE_MAP.get(str(role or "user").lower(), "user")


def _responses_to_chat(body):
    """Translate a Responses request body into OpenAI chat-completions messages.
    Handles `instructions` (-> leading system), a STRING or LIST `input`, and the
    message / function_call / function_call_output item types (unknown item types,
    e.g. reasoning, are skipped). Roles are normalized (developer -> system) so
    open model chat templates don't reject the request."""
    messages = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})

    inp = body.get("input")
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
        return messages
    for item in inp or []:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "function_call":
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": item.get("call_id") or item.get("id"),
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": item.get("arguments") or "{}",
                    },
                }],
            })
        elif itype == "function_call_output":
            output = item.get("output")
            content = output if isinstance(output, str) else json.dumps(output)
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id"),
                "content": content,
            })
        elif itype == "message" or itype is None:
            role = _norm_role(item.get("role"))
            content = item.get("content")
            if isinstance(content, str):
                text = content
            else:
                parts = []
                for part in content or []:
                    if isinstance(part, str):
                        parts.append(part)
                    elif isinstance(part, dict) and part.get("type") in (
                            "input_text", "output_text", "text"):
                        parts.append(part.get("text") or "")
                text = "".join(parts)
            messages.append({"role": role, "content": text})
        # else: unknown item type (reasoning, etc.) -> skip
    return messages


def _chat_to_responses(chat_json, model_label):
    """Non-streaming OpenAI chat-completions JSON -> a Responses `response` object."""
    choice = (chat_json.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    output = []
    content = msg.get("content")
    if content:
        output.append({
            "type": "message",
            "id": "msg_" + uuid.uuid4().hex,
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": content, "annotations": []}],
        })
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        output.append({
            "type": "function_call",
            "id": "fc_" + uuid.uuid4().hex,
            "call_id": tc.get("id"),
            "name": fn.get("name") or "",
            "arguments": fn.get("arguments") or "",
            "status": "completed",
        })
    usage = chat_json.get("usage") or {}
    pt = int(usage.get("prompt_tokens") or 0)
    ct = int(usage.get("completion_tokens") or 0)
    return {
        "id": "resp_" + uuid.uuid4().hex,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": model_label,
        "output": output,
        "usage": {"input_tokens": pt, "output_tokens": ct, "total_tokens": pt + ct},
    }


def _responses_stream(resp, model_label, line_iter=None, first=_MISSING):
    """Consume an upstream OpenAI chat SSE stream and re-emit it as Responses API
    events for Codex. When `line_iter`/`first` are supplied (the first-byte peek
    already pulled the first line from this exact iterator) the pre-read line is
    processed first, then the rest of the SAME iterator — so fast-path output is
    identical to before. Event order:
      response.created
      [text]  output_item.added -> content_part.added -> output_text.delta* ->
              output_text.done -> content_part.done -> output_item.done
      [tools] output_item.added -> function_call_arguments.delta* ->
              function_call_arguments.done -> output_item.done
      response.completed
    The assistant message (if any) is output_index 0; each tool call takes the
    next index. Defensive: unparseable chunks are skipped, and a mid-stream
    failure still emits a terminal response.completed so Codex never hangs."""
    resp_id = "resp_" + uuid.uuid4().hex
    created = int(time.time())

    def _obj(status, output_items, usage=None):
        o = {"id": resp_id, "object": "response", "created_at": created,
             "status": status, "model": model_label, "output": output_items}
        if usage is not None:
            o["usage"] = usage
        return o

    done_items = []          # [(output_index, item)] assembled so far
    next_index = 0
    text_started = False
    text_item_id = text_index = None
    text_buf = []
    tools = {}               # oai tool index -> {out_index,item_id,call_id,name,args[]}
    usage = None
    if line_iter is None:
        line_iter = resp.iter_lines(decode_unicode=False)
    try:
        yield _sse_event("response.created",
                         {"type": "response.created", "response": _obj("in_progress", [])})

        for raw in _chain_first(first, line_iter):
            if not raw or not raw.startswith(b"data:"):
                continue
            data = raw[5:].strip()
            if data == b"[DONE]":
                break
            try:
                chunk = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            u = chunk.get("usage")
            if isinstance(u, dict) and (u.get("prompt_tokens") is not None
                                        or u.get("completion_tokens") is not None):
                usage = u
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = (choices[0] or {}).get("delta") or {}

            dtext = delta.get("content")
            if dtext:
                if not text_started:
                    text_started = True
                    text_item_id = "msg_" + uuid.uuid4().hex
                    text_index = next_index
                    next_index += 1
                    yield _sse_event("response.output_item.added", {
                        "type": "response.output_item.added",
                        "output_index": text_index,
                        "item": {"type": "message", "id": text_item_id,
                                 "status": "in_progress", "role": "assistant",
                                 "content": []}})
                    yield _sse_event("response.content_part.added", {
                        "type": "response.content_part.added",
                        "item_id": text_item_id, "output_index": text_index,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": []}})
                text_buf.append(dtext)
                yield _sse_event("response.output_text.delta", {
                    "type": "response.output_text.delta",
                    "item_id": text_item_id, "output_index": text_index,
                    "content_index": 0, "delta": dtext})

            for tcd in delta.get("tool_calls") or []:
                if not isinstance(tcd, dict):
                    continue
                oai_idx = tcd.get("index", 0)
                fn = tcd.get("function") or {}
                st = tools.get(oai_idx)
                if st is None:
                    st = {"out_index": next_index,
                          "item_id": "fc_" + uuid.uuid4().hex,
                          "call_id": tcd.get("id") or ("call_" + uuid.uuid4().hex[:24]),
                          "name": fn.get("name") or "", "args": []}
                    next_index += 1
                    tools[oai_idx] = st
                    yield _sse_event("response.output_item.added", {
                        "type": "response.output_item.added",
                        "output_index": st["out_index"],
                        "item": {"type": "function_call", "id": st["item_id"],
                                 "call_id": st["call_id"], "name": st["name"],
                                 "arguments": "", "status": "in_progress"}})
                else:
                    if tcd.get("id"):
                        st["call_id"] = tcd["id"]
                    if fn.get("name"):
                        st["name"] = fn["name"]
                args = fn.get("arguments")
                if args:
                    st["args"].append(args)
                    yield _sse_event("response.function_call_arguments.delta", {
                        "type": "response.function_call_arguments.delta",
                        "item_id": st["item_id"], "output_index": st["out_index"],
                        "delta": args})

        if text_started:
            full = "".join(text_buf)
            yield _sse_event("response.output_text.done", {
                "type": "response.output_text.done",
                "item_id": text_item_id, "output_index": text_index,
                "content_index": 0, "text": full})
            yield _sse_event("response.content_part.done", {
                "type": "response.content_part.done",
                "item_id": text_item_id, "output_index": text_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": full, "annotations": []}})
            item = {"type": "message", "id": text_item_id, "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": full, "annotations": []}]}
            yield _sse_event("response.output_item.done", {
                "type": "response.output_item.done",
                "output_index": text_index, "item": item})
            done_items.append((text_index, item))

        for _oai_idx, st in sorted(tools.items(), key=lambda kv: kv[1]["out_index"]):
            full_args = "".join(st["args"])
            yield _sse_event("response.function_call_arguments.done", {
                "type": "response.function_call_arguments.done",
                "item_id": st["item_id"], "output_index": st["out_index"],
                "arguments": full_args})
            item = {"type": "function_call", "id": st["item_id"],
                    "call_id": st["call_id"], "name": st["name"],
                    "arguments": full_args, "status": "completed"}
            yield _sse_event("response.output_item.done", {
                "type": "response.output_item.done",
                "output_index": st["out_index"], "item": item})
            done_items.append((st["out_index"], item))

        final_usage = None
        if usage is not None:
            pt = int(usage.get("prompt_tokens") or 0)
            ct = int(usage.get("completion_tokens") or 0)
            final_usage = {"input_tokens": pt, "output_tokens": ct, "total_tokens": pt + ct}
        final_output = [it for _i, it in sorted(done_items, key=lambda t: t[0])]
        yield _sse_event("response.completed", {
            "type": "response.completed",
            "response": _obj("completed", final_output, final_usage)})
    except Exception as exc:  # never leave Codex hanging on a mid-stream failure
        _log.error("Responses stream error: %s", _sanitize(str(exc)))
        partial = [it for _i, it in sorted(done_items, key=lambda t: t[0])]
        try:
            yield _sse_event("response.completed", {
                "type": "response.completed",
                "response": _obj("completed", partial)})
        except Exception:
            pass
    finally:
        resp.close()


@app.route("/v1/responses", methods=["POST"])
def v1_responses():
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return _openai_error("Invalid JSON body.", 400)
    try:
        messages = _responses_to_chat(body)
    except Exception as exc:
        return _openai_error("Could not translate request: " + _sanitize(str(exc)), 400)
    if not messages:
        return _openai_error("No input to send.", 400)

    # Tools + size estimate up front (Codex sends huge tool schemas — they must
    # count toward routing so a big request doesn't land on a small-TPM provider).
    tools = _responses_tools_to_chat(body.get("tools"))
    est = _est_tokens(messages, tools)

    # Same routing as /v1/chat/completions: Auto/empty/claude-* -> difficulty
    # route across available, SIZE-CAPABLE providers; explicit '<pid>/<model>' bypasses.
    diff = None
    if _is_orchestrate(body.get("model")):
        pid, resolved, diff = _route_by_difficulty(messages, body.get("max_output_tokens"), est)
        if pid is None:
            pid, resolved = _resolve_model(body.get("model"))
    else:
        pid, resolved = _resolve_model(body.get("model"))
    if pid is None:
        return _openai_error(resolved, 400)
    if not prov.is_model_allowed(resolved):
        return _openai_error("Model '%s' is blocked by the safety filter." % resolved, 403,
                             "permission_error")
    not_ready = _check_provider_ready(pid)
    if not_ready:
        return _openai_error(not_ready, 400)

    base_payload = {"messages": messages}
    if body.get("max_output_tokens"):
        try:
            base_payload["max_tokens"] = int(body["max_output_tokens"])
        except (TypeError, ValueError):
            pass
    if body.get("temperature") is not None:
        base_payload["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        base_payload["top_p"] = body["top_p"]
    if tools:
        base_payload["tools"] = tools
        if body.get("tool_choice") is not None:
            base_payload["tool_choice"] = body["tool_choice"]

    stream = bool(body.get("stream"))
    errors = []
    last_hard = None  # last hard (non-retryable) upstream error, relayed if chain is exhausted
    for hop_pid, hop_model in _build_chain(pid, resolved, est):
        if not prov.is_model_allowed(hop_model):
            continue
        payload = dict(base_payload)
        payload["model"] = hop_model
        _apply_reasoning_effort(payload, hop_model, diff)
        payload["stream"] = stream
        try:
            _act_pick(hop_pid, hop_model)
            resp = _upstream_chat(hop_pid, payload, stream)
        except (requests.RequestException, RuntimeError) as exc:
            errors.append("%s: %s" % (hop_pid, _sanitize(exc.__class__.__name__)))
            continue
        if resp.status_code == 200:
            model_label = hop_pid + "/" + hop_model
            if stream:
                # #4: peek the first line BEFORE committing the 200 SSE stream so a
                # hung/slow provider falls through to the next hop instead of stalling.
                line_it = resp.iter_lines(decode_unicode=False)
                ok, first = _peek_first_chunk(line_it, STREAM_FIRST_BYTE_TIMEOUT)
                if not ok:
                    errors.append("%s: no first byte within %ds"
                                  % (hop_pid, STREAM_FIRST_BYTE_TIMEOUT))
                    resp.close()
                    continue
                return Response(stream_with_context(
                    _responses_stream(resp, model_label, line_it, first)),
                    mimetype="text/event-stream", headers=_SSE_HEADERS)
            try:
                data = resp.json()
            except ValueError:
                return _openai_error("Upstream returned non-JSON (%s, HTTP 200): %s"
                                     % (hop_pid, _sanitize(resp.text)), 502, "upstream_error")
            return jsonify(_chat_to_responses(data, model_label)), 200
        errors.append("%s: HTTP %d" % (hop_pid, resp.status_code))
        if not _retryable(resp.status_code):
            try:
                body_json = resp.json()
                body_text = None
            except ValueError:
                body_json = None
                body_text = _sanitize(resp.text)
            last_hard = {"pid": hop_pid, "status": resp.status_code,
                         "json": body_json, "text": body_text}
        resp.close()
        continue
    # No provider yielded a 200. We have NOT emitted any SSE yet, so return a
    # normal non-200 JSON OpenAI-style error (Codex checks the HTTP status before
    # opening the event stream and surfaces this cleanly) rather than a fake 200
    # SSE stream carrying an error.
    if last_hard is not None:
        if last_hard["json"] is not None:
            return jsonify(last_hard["json"]), last_hard["status"]
        return _openai_error("Upstream returned non-JSON (%s, HTTP %d): %s"
                             % (last_hard["pid"], last_hard["status"], last_hard["text"]),
                             502, "upstream_error")
    return _openai_error("All providers failed: " + ("; ".join(errors) or "none available"),
                         502, "upstream_error")


# ---------------------------------------------------------------------------
# Anthropic-compatible gateway (Claude Code support)
# ---------------------------------------------------------------------------

def _blocks_to_text(content):
    """Anthropic content (str | [blocks]) -> plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                btype = block.get("type")
                if btype == "text":
                    parts.append(block.get("text") or "")
                elif btype == "tool_result":
                    parts.append(_blocks_to_text(block.get("content")))
                elif btype == "image":
                    parts.append("[image omitted]")
        return "\n".join(p for p in parts if p)
    return ""


def _anthropic_to_openai_messages(body):
    """Anthropic system+messages -> OpenAI messages (tools included)."""
    out = []
    system = body.get("system")
    if system:
        text = system if isinstance(system, str) else _blocks_to_text(system)
        if text:
            out.append({"role": "system", "content": text})
    for msg in body.get("messages") or []:
        role = msg.get("role")
        content = msg.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        blocks = content if isinstance(content, list) else []
        if role == "assistant":
            text_parts, tool_calls = [], []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text_parts.append(block.get("text") or "")
                elif btype == "tool_use":
                    tool_calls.append({
                        "id": block.get("id") or ("call_" + uuid.uuid4().hex[:12]),
                        "type": "function",
                        "function": {
                            "name": block.get("name") or "",
                            "arguments": json.dumps(block.get("input") or {}),
                        },
                    })
            entry = {"role": "assistant",
                     "content": "\n".join(p for p in text_parts if p) or None}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            out.append(entry)
        else:  # user
            tool_results = [b for b in blocks
                            if isinstance(b, dict) and b.get("type") == "tool_result"]
            for tr in tool_results:
                out.append({"role": "tool",
                            "tool_call_id": tr.get("tool_use_id") or "",
                            "content": _blocks_to_text(tr.get("content")) or ""})
            rest = [b for b in blocks
                    if not (isinstance(b, dict) and b.get("type") == "tool_result")]
            text = _blocks_to_text(rest)
            if text or not tool_results:
                out.append({"role": "user", "content": text})
    return out


def _anthropic_tools_to_openai(tools):
    out = []
    for tool in tools or []:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        out.append({"type": "function", "function": {
            "name": tool["name"],
            "description": tool.get("description") or "",
            "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
        }})
    return out


def _anthropic_tool_choice_to_openai(tc):
    if not isinstance(tc, dict):
        return None
    ttype = tc.get("type")
    if ttype == "auto":
        return "auto"
    if ttype == "any":
        return "required"
    if ttype == "tool" and tc.get("name"):
        return {"type": "function", "function": {"name": tc["name"]}}
    return None


def _map_stop_reason(finish_reason):
    return {"stop": "end_turn", "length": "max_tokens",
            "tool_calls": "tool_use", "function_call": "tool_use",
            "content_filter": "end_turn"}.get(finish_reason or "stop", "end_turn")


def _estimate_input_tokens(body):
    total = 0
    system = body.get("system")
    if system:
        total += len(_blocks_to_text(system))
    for msg in body.get("messages") or []:
        total += len(_blocks_to_text(msg.get("content")))
    return max(1, total // 4)


def _openai_resp_to_anthropic(data, model_str):
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = []
    text = msg.get("content")
    if text:
        content.append({"type": "text", "text": text})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        content.append({"type": "tool_use",
                        "id": tc.get("id") or ("toolu_" + uuid.uuid4().hex[:16]),
                        "name": fn.get("name") or "",
                        "input": args})
    if not content:
        content = [{"type": "text", "text": ""}]
    usage = data.get("usage") or {}
    return {
        "id": "msg_" + str(data.get("id") or uuid.uuid4().hex),
        "type": "message",
        "role": "assistant",
        "model": model_str,
        "content": content,
        "stop_reason": _map_stop_reason(choice.get("finish_reason")),
        "stop_sequence": None,
        "usage": {"input_tokens": int(usage.get("prompt_tokens") or 0),
                  "output_tokens": int(usage.get("completion_tokens") or 0)},
    }


def _sse_event(name, obj):
    return ("event: %s\ndata: %s\n\n" % (name, json.dumps(obj, ensure_ascii=False))).encode("utf-8")


def _anthropic_stream(resp, model_str, input_tokens, line_iter=None, first=_MISSING):
    """Translate an upstream OpenAI SSE stream into the Anthropic event
    sequence: message_start -> content_block_start -> content_block_delta* ->
    content_block_stop -> message_delta -> message_stop. When `line_iter`/`first`
    are supplied (the first-byte peek already pulled the first line from this exact
    iterator) the pre-read line is processed first, then the rest of the SAME
    iterator — fast-path output is identical to before."""
    msg_id = "msg_" + uuid.uuid4().hex
    if line_iter is None:
        line_iter = resp.iter_lines(decode_unicode=False)
    try:
        yield _sse_event("message_start", {"type": "message_start", "message": {
            "id": msg_id, "type": "message", "role": "assistant", "model": model_str,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": 0}}})
        yield _sse_event("ping", {"type": "ping"})

        block_index = -1        # index of the currently open anthropic block
        block_kind = None       # None | 'text' | 'tool'
        tool_blocks = {}        # openai tool_call index -> anthropic block index
        finish_reason = None
        out_tokens = None
        text_chars = 0

        for raw in _chain_first(first, line_iter):
            if not raw or not raw.startswith(b"data:"):
                continue
            data = raw[5:].strip()
            if data == b"[DONE]":
                break
            try:
                chunk = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            usage = chunk.get("usage")
            if isinstance(usage, dict) and usage.get("completion_tokens") is not None:
                out_tokens = usage.get("completion_tokens")
            choices = chunk.get("choices") or []
            if not choices:
                continue
            choice = choices[0] or {}
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}

            dtext = delta.get("content")
            if dtext:
                if block_kind != "text":
                    if block_kind is not None:
                        yield _sse_event("content_block_stop",
                                         {"type": "content_block_stop", "index": block_index})
                    block_index += 1
                    block_kind = "text"
                    yield _sse_event("content_block_start", {
                        "type": "content_block_start", "index": block_index,
                        "content_block": {"type": "text", "text": ""}})
                text_chars += len(dtext)
                yield _sse_event("content_block_delta", {
                    "type": "content_block_delta", "index": block_index,
                    "delta": {"type": "text_delta", "text": dtext}})

            for tcd in delta.get("tool_calls") or []:
                if not isinstance(tcd, dict):
                    continue
                oai_idx = tcd.get("index", 0)
                fn = tcd.get("function") or {}
                if oai_idx not in tool_blocks:
                    if block_kind is not None:
                        yield _sse_event("content_block_stop",
                                         {"type": "content_block_stop", "index": block_index})
                    block_index += 1
                    block_kind = "tool"
                    tool_blocks[oai_idx] = block_index
                    yield _sse_event("content_block_start", {
                        "type": "content_block_start", "index": block_index,
                        "content_block": {
                            "type": "tool_use",
                            "id": tcd.get("id") or ("toolu_" + uuid.uuid4().hex[:16]),
                            "name": fn.get("name") or "",
                            "input": {}}})
                args = fn.get("arguments")
                if args:
                    yield _sse_event("content_block_delta", {
                        "type": "content_block_delta", "index": tool_blocks[oai_idx],
                        "delta": {"type": "input_json_delta", "partial_json": args}})

        if block_index < 0:  # upstream produced nothing: still emit a valid shape
            block_index = 0
            yield _sse_event("content_block_start", {
                "type": "content_block_start", "index": 0,
                "content_block": {"type": "text", "text": ""}})
        yield _sse_event("content_block_stop",
                         {"type": "content_block_stop", "index": block_index})
        if out_tokens is None:
            out_tokens = max(1, text_chars // 4)
        yield _sse_event("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": _map_stop_reason(finish_reason), "stop_sequence": None},
            "usage": {"output_tokens": int(out_tokens)}})
        yield _sse_event("message_stop", {"type": "message_stop"})
    finally:
        resp.close()


@app.route("/v1/messages", methods=["POST"])
def v1_messages():
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return _anthropic_error("invalid_request_error", "Invalid JSON body.", 400)
    # Claude Code sends model 'claude-*' + a big system/tools payload -> orchestrate
    # by difficulty AND request size (skip small-TPM providers for large requests).
    est = _estimate_input_tokens(body)
    try:
        if body.get("tools"):
            est += len(json.dumps(body.get("tools"))) // 4
    except Exception:
        pass
    diff = None
    if _is_orchestrate(body.get("model")):
        pid, resolved, diff = _route_by_difficulty(body.get("messages"),
                                                    body.get("max_tokens"), est)
        if pid is None:
            pid, resolved = _resolve_model(body.get("model"))
    else:
        pid, resolved = _resolve_model(body.get("model"))
    if pid is None:
        return _anthropic_error("invalid_request_error", resolved, 400)
    if not prov.is_model_allowed(resolved):
        return _anthropic_error("permission_error",
                                "Model '%s' is blocked by the safety filter." % resolved, 403)
    not_ready = _check_provider_ready(pid)
    if not_ready:
        return _anthropic_error("invalid_request_error", not_ready, 400)

    try:
        oai_messages = _anthropic_to_openai_messages(body)
    except Exception as exc:
        return _anthropic_error("invalid_request_error",
                                "Could not translate request: " + _sanitize(exc), 400)
    if not oai_messages:
        return _anthropic_error("invalid_request_error", "No messages to send.", 400)

    base_payload = {"messages": oai_messages}
    if body.get("max_tokens"):
        try:
            base_payload["max_tokens"] = int(body["max_tokens"])
        except (TypeError, ValueError):
            pass
    if body.get("temperature") is not None:
        base_payload["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        base_payload["top_p"] = body["top_p"]
    if body.get("stop_sequences"):
        base_payload["stop"] = body["stop_sequences"]
    tools = _anthropic_tools_to_openai(body.get("tools"))
    if tools:
        base_payload["tools"] = tools
        tc = _anthropic_tool_choice_to_openai(body.get("tool_choice"))
        if tc:
            base_payload["tool_choice"] = tc

    stream = bool(body.get("stream"))
    requested_model = body.get("model") if isinstance(body.get("model"), str) else None
    input_est = _estimate_input_tokens(body)

    errors = []
    last_hard = None  # last hard (non-retryable) upstream error, relayed if the chain is exhausted
    for hop_pid, hop_model in _build_chain(pid, resolved, est):
        if not prov.is_model_allowed(hop_model):
            continue
        payload = dict(base_payload)
        payload["model"] = hop_model
        _apply_reasoning_effort(payload, hop_model, diff)
        payload["stream"] = stream
        try:
            _act_pick(hop_pid, hop_model)
            resp = _upstream_chat(hop_pid, payload, stream)
        except (requests.RequestException, RuntimeError) as exc:
            errors.append("%s: %s" % (hop_pid, _sanitize(exc.__class__.__name__)))
            continue
        if resp.status_code == 200:
            model_str = requested_model or (hop_pid + "/" + hop_model)
            if stream:
                # #4: peek the first line BEFORE committing the 200 SSE stream so a
                # hung/slow provider falls through to the next hop instead of stalling.
                line_it = resp.iter_lines(decode_unicode=False)
                ok, first = _peek_first_chunk(line_it, STREAM_FIRST_BYTE_TIMEOUT)
                if not ok:
                    errors.append("%s: no first byte within %ds"
                                  % (hop_pid, STREAM_FIRST_BYTE_TIMEOUT))
                    resp.close()
                    continue
                return Response(stream_with_context(
                    _anthropic_stream(resp, model_str, input_est, line_it, first)),
                    mimetype="text/event-stream", headers=_SSE_HEADERS)
            try:
                data = resp.json()
            except ValueError:
                return _anthropic_error("api_error",
                                        "Upstream %s returned non-JSON." % hop_pid, 502)
            return jsonify(_openai_resp_to_anthropic(data, model_str))
        # Non-2xx. Retryable (429/5xx) AND hard errors (404/400/model-not-found)
        # both advance to the NEXT provider (a different provider/model) before we
        # surface an error; within-provider key rotation already ran upstream.
        errors.append("%s: HTTP %d" % (hop_pid, resp.status_code))
        if not _retryable(resp.status_code):
            # Capture the last hard error's detail to relay once the chain is done.
            detail = _upstream_error_detail(resp)
            status = resp.status_code if 400 <= resp.status_code < 500 else 502
            last_hard = {"pid": hop_pid, "http": resp.status_code,
                         "status": status, "detail": detail}
        resp.close()
        continue
    if last_hard is not None:
        return _anthropic_error("api_error",
                                "Upstream %s error (HTTP %d): %s"
                                % (last_hard["pid"], last_hard["http"], last_hard["detail"]),
                                last_hard["status"])
    return _anthropic_error("api_error",
                            "All providers failed: " + ("; ".join(errors) or "none available"),
                            502)


@app.route("/v1/messages/count_tokens", methods=["POST"])
def v1_count_tokens():
    """Rough estimate (chars/4) so Anthropic clients that pre-count don't 404."""
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return _anthropic_error("invalid_request_error", "Invalid JSON body.", 400)
    return jsonify({"input_tokens": _estimate_input_tokens(body)})


# ---------------------------------------------------------------------------
# Auto-update: git-pull every N hours and self-restart when the repo owner ships
# new commits. Opt-in (on by default), skipped if the working tree is dirty so a
# user's local edits are never clobbered. Never touches ~/.free-llm-hub config
# (that lives outside the repo), so keys/enabled flags survive every update.
# ---------------------------------------------------------------------------
_REPO_DIR = os.path.dirname(os.path.abspath(__file__))
_AUTO_UPDATE_INTERVAL_H = float(os.environ.get("AUTO_UPDATE_INTERVAL_HOURS", "5") or "5")
_auto_update_state = {
    "enabled": None,          # resolved at boot from env + config
    "interval_hours": _AUTO_UPDATE_INTERVAL_H,
    "last_check": 0,          # epoch of last pull attempt
    "last_result": "not run yet",
    "updating": False,
}
_auto_update_thread = None
_auto_update_lock = threading.Lock()


def _git(*args, timeout=120):
    """Run a git command in the repo dir; return (rc, stdout, stderr). Never raises."""
    try:
        r = subprocess.run(["git", "-C", _REPO_DIR, *args],
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except Exception as exc:
        return 1, "", "%s: %s" % (exc.__class__.__name__, exc)


def _is_git_repo():
    rc, out, _ = _git("rev-parse", "--is-inside-work-tree")
    return rc == 0 and out == "true"


def _auto_update_enabled():
    """Env AUTO_UPDATE overrides; else the config flag (default ON)."""
    env = os.environ.get("AUTO_UPDATE")
    if env is not None:
        return env.strip().lower() not in ("0", "false", "no", "off", "")
    return config.get_flag("auto_update", True)


def _do_update_check():
    """One pull cycle: skip if dirty, pull --ff-only, re-exec if HEAD moved.
    Returns a short human status string (also stored in _auto_update_state)."""
    with _auto_update_lock:
        _auto_update_state["last_check"] = int(time.time())
        if not _is_git_repo():
            _auto_update_state["last_result"] = "not a git repo — auto-update off"
            return _auto_update_state["last_result"]
        rc, dirty, _ = _git("status", "--porcelain")
        if rc == 0 and dirty:
            _auto_update_state["last_result"] = "skipped: local uncommitted changes"
            return _auto_update_state["last_result"]
        rc, before, _ = _git("rev-parse", "HEAD")
        rc2, _out, err = _git("pull", "--ff-only")
        if rc2 != 0:
            _auto_update_state["last_result"] = "pull failed: " + _sanitize(err)[:160]
            return _auto_update_state["last_result"]
        _rc, after, _ = _git("rev-parse", "HEAD")
        if before and after and before != after:
            _auto_update_state["last_result"] = "updated %s->%s — restarting" % (before[:7], after[:7])
            _auto_update_state["updating"] = True
            _log.info("Auto-update: new commits pulled (%s -> %s), re-executing.",
                     before[:7], after[:7])
            _reexec_soon()
            return _auto_update_state["last_result"]
        _auto_update_state["last_result"] = "up to date (%s)" % (after[:7] if after else "?")
        return _auto_update_state["last_result"]


def _reexec_soon():
    """Replace this process with a fresh one (applies pulled code). Env (incl.
    PORT) is inherited across execv, so the gateway comes back on the same port."""
    def _go():
        time.sleep(1.0)
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as exc:
            _log.error("Auto-update re-exec failed: %s", exc)
            _auto_update_state["updating"] = False
    threading.Thread(target=_go, daemon=True).start()


def _auto_update_loop():
    interval = max(0.25, _auto_update_state["interval_hours"]) * 3600.0
    # A short initial delay lets the server finish booting before the first check.
    time.sleep(min(interval, 60))
    while True:
        if _auto_update_enabled():
            try:
                _do_update_check()
            except Exception as exc:
                _log.error("Auto-update cycle error: %s", exc)
        # Sleep in small slices so a disabled->enabled flip is honored promptly.
        slept = 0.0
        while slept < interval:
            time.sleep(min(30.0, interval - slept))
            slept += 30.0


def _start_auto_update():
    global _auto_update_thread
    _auto_update_state["enabled"] = _auto_update_enabled()
    if _auto_update_thread is not None:
        return
    _auto_update_thread = threading.Thread(target=_auto_update_loop, daemon=True)
    _auto_update_thread.start()


@app.route("/api/auto-update", methods=["GET", "POST"])
def api_auto_update():
    """GET -> current state. POST {enabled:bool} -> toggle. POST {check:true} ->
    run one update cycle now (may restart the server if new commits are found)."""
    if request.method == "POST":
        body = request.get_json(force=True, silent=True) or {}
        if "enabled" in body:
            config.set_flag("auto_update", bool(body["enabled"]))
        if body.get("check"):
            threading.Thread(target=_do_update_check, daemon=True).start()
    st = dict(_auto_update_state)
    st["enabled"] = _auto_update_enabled()
    st["is_git_repo"] = _is_git_repo()
    return jsonify(st)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def _print_banner():
    key = config.get_local_api_key()
    snippets = _connect_snippets()
    line = "=" * 74
    print(line)
    print("  Free LLM Hub -- local gateway for free LLM providers")
    print(line)
    print("  Dashboard:   http://%s:%d/" % (HOST, PORT))
    print("  OpenAI API:  http://%s:%d/v1  (chat/completions, models)" % (HOST, PORT))
    print("  Anthropic:   http://%s:%d/v1/messages  (Claude Code compatible)" % (HOST, PORT))
    if key:
        print("  Local key:   SET (required on /v1/* as Bearer or x-api-key)")
    else:
        print("  Local key:   not set -- /v1/* is open on localhost")
    print(line)
    print("  Connect Claude Code:")
    for ln in snippets["claude_code"].splitlines():
        print("    " + ln)
    print("  Connect OpenAI-compatible CLIs (aider, opencode, ...):")
    for ln in snippets["openai"].splitlines():
        print("    " + ln)
    print(line)


if __name__ == "__main__":
    _print_banner()
    _start_auto_update()
    app.run(host=HOST, port=PORT, threaded=True, debug=False)
