#!/usr/bin/env python3
"""Calvoun Free LLM Hub -- local gateway that serves FREE LLM providers to any tool.

Surfaces:
  GET  /                        dashboard (templates/index.html)
  /api/*                        localhost control API (dashboard control header for writes)
  GET  /v1/models               OpenAI-compatible model list
  POST /v1/chat/completions     OpenAI-compatible chat (streaming passthrough)
  POST /v1/messages             Anthropic Messages API (translated to OpenAI
                                upstream, both directions, incl. streaming) --
                                this is what lets Claude Code use free models.
  POST /v1/messages/count_tokens  rough token estimate (Claude Code compat)

Auth: if a local API key is configured (config.get_local_api_key()), all /v1/*
routes require it as 'Authorization: Bearer <key>' or 'x-api-key: <key>'.
The dashboard/control API is loopback-only; browser writes also require a
non-simple local-control header to prevent cross-site localhost requests.

Run:  python app.py    (PORT env overrides default 8787)
"""

import hmac
import base64
import binascii
import copy
import ipaddress
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from urllib.parse import quote, urlsplit

import requests
from flask import Flask, Response, g, jsonify, render_template, request, stream_with_context

try:
    from jinja2 import TemplateNotFound
except Exception:  # pragma: no cover - jinja2 always ships with flask
    class TemplateNotFound(Exception):
        pass

import agentic_chat
import agentic_history
import config
import image_history
import providers as prov
import quota
import usage_history
import vision_status

import logging
import traceback as _traceback

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
_log = logging.getLogger("free-llm-hub")

app = Flask(__name__)
# Bound JSON/image requests before Flask buffers them. Eight 1 MiB images plus
# JSON/base64 overhead fit; accidental multi-hundred-megabyte data URLs do not.
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024
# Without this, Flask caches the compiled template on first render and a
# dashboard edit (like this footer link) won't appear until the process is
# restarted, not just on browser refresh.
app.config["TEMPLATES_AUTO_RELOAD"] = True


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
# Bound for the "peek until real content" look-ahead that tells a genuine answer
# from an empty 200 (some free providers return 200 then stream only a role delta +
# [DONE]). An empty stream reaches its terminal FAST (well under this), and a normal
# model emits content within a few seconds; this ceiling only bites a stream that
# goes idle without ever producing content, which then falls through to the next model.
STREAM_CONTENT_PEEK_TIMEOUT = 35  # seconds
STREAM_IDLE_TIMEOUT = 90         # seconds
MODELS_READ_TIMEOUT = 10      # seconds (model discovery / key tests)
MODEL_CACHE_TTL = 60          # seconds
MAX_HOPS = 6                  # primary + up to 5 fallback models (across providers)

MAX_IMAGE_COUNT = 8
MAX_IMAGE_BYTES = 8 * 1024 * 1024
_IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}

_model_cache = {}             # pid -> (timestamp, [model ids])
_model_cache_lock = threading.Lock()
_cf_account_cache = {}        # cloudflare api token -> account id (see _cf_account_id)


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

def _needs_key(pid):
    """False for providers that are usable with NO api key at all (e.g.
    Pollinations' anonymous tier: no key, no signup, no card). Everything in the
    key path has to honor this or such a provider is registered but unreachable."""
    p = prov.get_provider(pid) or {}
    return not p.get("no_key")


def _bootstrap_no_key_providers():
    """Enable each no-key provider ONCE, on first sight.

    A keyed provider is enabled implicitly by the act of saving a key. A no-key
    provider has nothing to save, so without this it would sit disabled forever
    and be registered-but-unusable — which is exactly what happened: Pollinations
    answered fine on its own, but the hub refused it with "is disabled".

    Only ever writes when the user has NOT expressed an opinion (no `enabled` key
    stored). Disable it in the dashboard and that decision sticks — the row then
    carries enabled=False and this never touches it again. Best-effort."""
    try:
        rows = (config.load_config() or {}).get("providers") or {}
        for p in prov.list_providers():
            pid = p["id"]
            if not (prov.get_provider(pid) or {}).get("no_key"):
                continue
            row = rows.get(pid)
            if isinstance(row, dict) and row.get("enabled") is not None:
                continue  # user already chose — respect it
            config.set_provider_config(pid, enabled=True)
    except Exception:
        pass  # never block startup over a convenience default


def _enabled_keyed():
    """Provider ids that are enabled AND have an API key saved — plus enabled
    no-key providers, which are usable precisely because they need no key."""
    out = []
    for p in prov.list_providers():
        pid = p["id"]
        pcfg = config.get_provider_config(pid)
        if not pcfg.get("enabled"):
            continue
        if pcfg.get("api_key") or not _needs_key(pid):
            out.append(pid)
    return out


def _available_providers():
    """Enabled+keyed providers that still have free quota (not exhausted/throttled).
    Falls back to ALL enabled+keyed when every one is exhausted, so the gateway
    still tries (and the dashboard's red banner tells the user why it may fail)."""
    keyed = _enabled_keyed()
    # Skip providers with a bad key (auth/credit-sidelined) AND those out of quota.
    live = [pid for pid in keyed
            if not quota.is_exhausted(pid) and not _is_provider_dead(pid)]
    if live:
        return live
    # Nothing fully available: prefer providers that are merely quota-exhausted (they
    # recover on reset) over auth-dead ones (bad key), and only fall back to ALL keyed
    # as the last resort so the gateway still tries something rather than nothing.
    not_broken = [pid for pid in keyed if not _is_provider_dead(pid)]
    return not_broken or keyed


def _cf_account_id(api_key):
    """Resolve the Cloudflare account id from the API token itself.

    Cloudflare's base URL is account-scoped
    (.../accounts/{account_id}/ai/v1), which is why it can't just be a registry
    row. But the token can tell us: GET /client/v4/accounts returns the accounts
    it can see, so the user pastes ONLY a token and the hub fills in the rest.
    Cached; returns None on any failure (caller falls back to the custom base)."""
    if not api_key:
        return None
    hit = _cf_account_cache.get(api_key)
    if hit:
        return hit
    try:
        r = requests.get("https://api.cloudflare.com/client/v4/accounts",
                         headers={"Authorization": "Bearer " + api_key},
                         timeout=(CONNECT_TIMEOUT, MODELS_READ_TIMEOUT))
        if r.status_code == 200:
            res = (r.json() or {}).get("result") or []
            if res and isinstance(res[0], dict) and res[0].get("id"):
                _cf_account_cache[api_key] = res[0]["id"]
                return res[0]["id"]
    except Exception:
        pass
    return None


def _validate_custom_base_url(value):
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ValueError("custom base URL is invalid") from exc
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        raise ValueError("custom base URL must use http:// or https://")
    if parsed.username or parsed.password:
        raise ValueError("custom base URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("custom base URL must not contain a query or fragment")
    if parsed.scheme == "http" and host not in _LOOPBACK_HOSTS:
        raise ValueError("non-loopback custom base URLs must use https://")
    return value.rstrip("/")


def _resolve_base_url(pid, pcfg):
    """Provider base URL, with Cloudflare's {account_id} filled in from the token.

    A user-set custom base still wins (base_url_for); this only rescues the case
    where the registry base carries a template and the user pasted just a token."""
    custom = pcfg.get("base_url")
    base = _validate_custom_base_url(custom) if custom else prov.base_url_for(pid, None)
    if base and "{account_id}" in base:
        acct = _cf_account_id(pcfg.get("api_key"))
        if acct:
            return base.replace("{account_id}", acct)
    return base


def _models_url_for(pid, pcfg):
    p = prov.get_provider(pid) or {}
    custom = pcfg.get("base_url")
    if custom:
        return _validate_custom_base_url(custom) + "/models"
    murl = p.get("models_url")
    if murl and "{account_id}" in murl:
        acct = _cf_account_id(pcfg.get("api_key"))
        return murl.replace("{account_id}", acct) if acct else None
    return murl


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
    # A no-key provider can still be discovered live (its /models needs no auth);
    # everything else without a key has nothing to authenticate with.
    if not live or (not pcfg.get("api_key") and _needs_key(pid)):
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
                # no_key providers have no key to send (and pcfg["api_key"] would
                # KeyError); their /models is public.
                headers=({"Authorization": "Bearer " + pcfg["api_key"]}
                         if pcfg.get("api_key") else {}),
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


# --- Auto provider mode: free (default) / paid / mix -------------------------
_paid_model_cache = {}  # pid -> (timestamp, [paid model ids]); separate from _model_cache


def _auto_provider_mode():
    """Which models AUTO routing may use: 'free' (default), 'paid', or 'mix'.
    Persisted as a top-level config string; anything unexpected falls back to 'free'."""
    m = config.get_setting("auto_provider_mode", "free")
    return m if m in ("free", "paid", "mix") else "free"


def _provider_paid_models(pid):
    """A provider's NON-free (paid) models via live /models discovery, safety- and
    non-chat-filtered. Empty unless the provider is enabled+keyed with a models_url.
    Cached 60s separately from the free cache."""
    p = prov.get_provider(pid)
    if not p:
        return []
    pcfg = config.get_provider_config(pid)
    if not pcfg.get("api_key") and _needs_key(pid):
        return []
    now = time.time()
    with _model_cache_lock:
        hit = _paid_model_cache.get(pid)
        if hit and (now - hit[0]) < MODEL_CACHE_TTL:
            return list(hit[1])
    out = []
    url = _models_url_for(pid, pcfg)
    if url:
        try:
            resp = requests.get(
                url,
                headers=({"Authorization": "Bearer " + pcfg["api_key"]}
                         if pcfg.get("api_key") else {}),
                timeout=(CONNECT_TIMEOUT, MODELS_READ_TIMEOUT),
            )
            if resp.status_code == 200:
                ids = _parse_model_ids(resp.json())
                out = prov.filter_models([m for m in ids if not prov.is_free_model(pid, m)])
        except Exception:
            pass
    with _model_cache_lock:
        _paid_model_cache[pid] = (now, list(out))
    return out


def _auto_models(pid):
    """Models a provider contributes to AUTO routing, honoring _auto_provider_mode():
    'free' -> free only (provider_free_models); 'paid' -> paid only; 'mix' -> both.
    Display code keeps calling provider_free_models directly (always free-only)."""
    mode = _auto_provider_mode()
    free = provider_free_models(pid)
    if mode == "free":
        return free
    paid = _provider_paid_models(pid)
    if mode == "paid":
        return paid
    seen = set(free)
    return free + [m for m in paid if m not in seen]


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
# First matching tier wins (break). Order S→A→B→C→D so a specific strong id beats
# a generic weak token. Scores refreshed to Jul-2026 AA-II / SWE-bench tiers.
# NOTE: strong flagships live here so their CURRENT pins score top even without a
# parsable version bump; the new-version heuristic below auto-covers FUTURE bumps.
# Flash/flash-lite/mini/<14B variants are demoted by the CAP in _benchmark_score,
# never by inheriting a flagship family's tier (the historical "gemini-3" /
# bare-"mini" scoring bugs).
_BENCH_FAMILY = [
    # Tier S — frontier proprietary (pinned) + strongest free/open 2026 flagships.
    (("grok-4", "gpt-5", "claude-opus", "claude-sonnet-5", "claude-fable",
      "gemini-3-pro", "gemini-3.5-pro", "gemini-3-ultra", "gemini-3.5-ultra",
      "gemini-3.5-flash", "gemini-3-flash",
      "deepseek-v4", "deepseek-r2", "glm-5", "glm5", "glm-6",
      "kimi-k2.6", "kimi-k2.7", "kimi-k2-thinking",
      "hy3", "hunyuan-3", "tencent-hy",
      "minimax-m3", "qwen3.5", "qwen3-max"), 100),
    # Tier A — strong open workhorses (production SEO + coding drivers).
    (("deepseek-v3", "deepseek-r1", "deepseek-chat",
      "qwen3-235b", "qwen3-next", "qwen3-coder", "qwen3-32b",
      "glm-4.7", "glm-4.6", "kimi-k2", "minimax-m2",
      "gpt-oss-120b", "nemotron-3-ultra", "nemotron-3-super",
      "hunyuan-a13", "hunyuan-turbos", "command-a"), 84),  # hy3 promoted to Tier S above
    # Tier B — capable mid (routine content, not hard reasoning).
    (("qwen3", "gemma-4", "gemma4", "mistral-medium", "mistral-large",
      "nemotron-3-120b", "phi-4", "solar-pro", "nova-2-pro", "granite-4",
      "command-r-plus", "gemini-2.5-pro"), 56),
    # Tier C-hi — older mid / mid-small usable.
    (("llama-4", "llama4", "gemma-3-27", "gemma3-27", "qwen2.5-72",
      "llama-3.3-70", "mistral-small", "command-r", "gpt-4o", "gpt-oss-20b",
      "nemotron-70"), 40),
    # Tier C — legacy / superseded / specialized (avoid for heavy).
    (("qwen2.5", "qwen2", "llama-3", "llama-2", "gemma-3", "gemma3", "gemma-2",
      "mixtral", "moonshot-v1", "qwq", "distill", "codestral", "devstral",
      "mercury", "sonar", "ernie", "hermes", "gemini-2.0", "gemini-2.5-flash",
      "gpt-4o-mini"), 26),
    # Tier D — tiny / lite / mini / flash-lite / nano (the CAP also enforces this).
    (("flash-lite", "-lite", "-mini", "nano", "small", "mistral-nemo", "tiny",
      "ministral", "instant", "1b", "2b", "3b", "4b", "7b", "8b", "9b"), 18),
]

# NEW-VERSION HEURISTIC — a known-strong family ROOT at a version >= its pinned
# CURRENT strong version scores in the TOP band, so a brand-new release
# (deepseek-v5, glm-6, kimi-k3, qwen4, minimax-m4, gemini-4) auto-ranks strong the
# moment a free provider lists it — no table edit needed. Numbered families only
# (clean version parse); flat-named strong families (gpt-oss-120b, command-a,
# hunyuan hy3, nemotron-3-ultra) are covered by _BENCH_FAMILY above.
_STRONG_ROOTS = (
    # (root_substring, pinned_version, top_score)
    ("deepseek-v", 3.1, 100),   # v3.1/v3.2/v4/v5…  (bare v3 orig -> 3.0 < 3.1)
    ("glm",        5.0, 100),   # glm-5/5.1/5.2/6…  (glm-4.x -> <5, stays weak)
    ("qwen",       3.0, 100),   # qwen3/qwen3.5/qwen4…  (qwen2.5 -> 2.5 < 3)
    ("kimi-k",     2.0, 100),   # kimi-k2/k2.6/k3…
    ("minimax-m",  2.0, 100),   # minimax-m2/m2.5/m3/m4…
    ("gemini",     3.0, 100),   # gemini-3/3.5/4…  (gemini-2.x -> <3; flash-lite CAPed)
    ("llama",      5.0, 100),   # llama-5+ only (Llama-4 flopped -> stays mid)
)
_VER_AFTER_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _strong_new_version_score(low):
    """New-version heuristic (fail-safe): the highest top-score whose known-strong
    family ROOT appears with a version >= its pin, else 0."""
    best = 0
    for root, pin, pts in _STRONG_ROOTS:
        idx = low.find(root)
        if idx < 0:
            continue
        m = _VER_AFTER_RE.search(low[idx + len(root):])
        if not m:
            continue
        try:
            if float(m.group(1)) >= pin:
                best = max(best, pts)
        except ValueError:
            pass
    return best


def _benchmark_score(pid, model_id):
    """Heuristic strength score for a '<model>' on provider `pid` (higher=better).
    Pure string heuristic — no network, future-proof against catalog churn."""
    low = (model_id or "").lower()
    score = 10  # base so an unknown model still ranks above nothing
    for names, pts in _BENCH_FAMILY:
        if any(n in low for n in names):
            score = max(score, pts)
            break
    # NEW-VERSION HEURISTIC: auto-rank a newer release of a known-strong family.
    sv = _strong_new_version_score(low)
    score = max(score, sv)
    # Explicit parameter size nudges within a family (…-70b > …-8b).
    params_b = None
    m = re.search(r"(\d{1,4})\s*b\b", low)
    if m:
        try:
            params_b = int(m.group(1))
            score += min(params_b, 500) / 25.0
        except ValueError:
            pass
    # Prefer instruct/chat tunes over raw/base for a chat gateway.
    if any(t in low for t in ("instruct", "chat", "-it")):
        score += 3
    # A tiny provider bias breaks ties toward fast, reliable free hosts.
    score += {"cerebras": 2.0, "groq": 1.8, "nvidia": 1.2, "google": 1.0}.get(pid, 0.0)
    # Coding-strength adjustment: this hub is coding-heavy, and raw strength != code
    # ability. Boost known-strong 2026 coders; penalize the weak Mistral CHAT family
    # (mistral-large/medium exempt — they are the capable big ones).
    if any(c in low for c in ("deepseek", "qwen3-coder", "qwen2.5-coder", "qwen3",
                              "kimi", "glm-5", "glm5", "glm-6", "glm-4.7", "glm-4.6",
                              "gpt-oss", "claude", "gpt-5", "minimax-m", "hy3",
                              "hunyuan", "starcoder")):
        score += 8
    # USER PREFERENCE: hy3 (Tencent HunYuan) is the #1 pick for coding + heavy tasks,
    # then latest kimi / qwen / deepseek (all already Tier S). Floor it above every
    # other model's realistic max (~133: tier 100 + coder 8 + size 20 + instruct 3 +
    # provider 2) so hy3 wins the top slot whenever it's available/keyed. Only affects
    # the max-based (hard/tools/agentic) routing — light tasks still pick cheapest.
    if "hy3" in low or "hunyuan-3" in low or "tencent-hy" in low:
        score = max(score, 135)
    # USER PREFERENCE: Kimi K3 (Moonshot) — top pick for the heaviest tasks, right
    # behind hy3 and above every other model. Auto-applies when a provider serves a
    # kimi-k3 id (nothing lists it yet). Matches kimi-k3 / kimi-k3.x / .../kimi-k3.
    if "kimi-k3" in low or "kimik3" in low:
        score = max(score, 134)
    if (("mistral" in low or "mixtral" in low or "ministral" in low)
            and not any(k in low for k in ("mistral-large", "mistral-medium"))):
        score -= 14
    # SPEED/TINY CAP (last word): flash/lite/mini/nano/distill variants and any model
    # < 14B params are latency/edge tier — cap them out of the heavy band even when
    # their family root is a flagship (glm-4.7-FLASH, gemini-3.1-flash-lite,
    # deepseek-r1-DISTILL). gemini-3(.5)-flash / gemini-4 are the kept exceptions.
    flash_ok = ("gemini-3.5-flash", "gemini-3-flash", "gemini-4")
    speed = ("flash-lite", "-lite", "-mini", "nano", "distill", "-air",
             "instant", "-tiny", "-edge", "mixtral", "moonshot-v1",
             "ernie-speed", "ernie-lite", "mistral-nemo", "qwq")
    capped = any(s in low for s in speed)
    # 'flash' is ambiguous: weak on gemini-3.1/glm-4.x, but STRONG on
    # deepseek-v4-flash / gemini-3.5-flash. Don't cap a model the version
    # heuristic already flagged as a strong new release (sv > 0).
    if "flash" in low and not any(ok in low for ok in flash_ok) and not sv:
        capped = True
    if params_b is not None and params_b < 14:
        capped = True
    if capped:
        score = min(score, 30)
    return score


def _best_free_pair(working_only=True):
    """Scan every AVAILABLE (enabled+keyed+quota-left) provider's free models and
    return the single highest-benchmark (pid, model) pair, or (None, None).

    `working_only` (default) skips models we KNOW are unusable — blocked by the
    safety filter, or sidelined by the dead-model tracker after a real 403/404.
    That check is why this exists: without it the picker happily returned
    github-models/llama-4-maverick as "best", an id that 403s on EVERY call
    (the token lacks the models:read scope), and it got saved as the default.
    "Best" must mean best AMONG MODELS THAT ANSWER."""
    best, best_pid, best_score = None, None, -1.0
    for pid in _available_providers():
        for m in _auto_models(pid):
            if working_only and (not prov.is_model_allowed(m) or _is_model_dead(pid, m)):
                continue
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
def _vision_model_ids(pid):
    """Verified image-capable model ids for one provider (exact matches only)."""
    p = prov.get_provider(pid) or {}
    return [m for m in (p.get("vision_models") or []) if isinstance(m, str) and m]


def _is_vision_model(pid, model):
    needle = str(model or "").lower()
    return any(needle == m.lower() for m in _vision_model_ids(pid))


def _data_image_bytes(url):
    """Validate an image data URL and return its decoded byte length."""
    match = re.match(r"^data:([^;,]+);base64,(.*)$", url, re.I | re.S)
    if not match:
        raise ValueError("image data URLs must use data:<image-type>;base64,...")
    mime = match.group(1).lower()
    if mime not in _IMAGE_MIMES:
        raise ValueError("unsupported image type '%s'" % mime)
    encoded = re.sub(r"\s+", "", match.group(2))
    if len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
        raise ValueError("image payload exceeds the %d MiB limit" % (MAX_IMAGE_BYTES // 1048576))
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("image data is not valid base64") from exc
    return len(raw)


def _normalize_image_url(value):
    """Return OpenAI's canonical {url, detail?} image_url object."""
    if isinstance(value, str):
        obj = {"url": value}
    elif isinstance(value, dict):
        obj = {"url": value.get("url")}
        if value.get("detail") in ("auto", "low", "high"):
            obj["detail"] = value["detail"]
    else:
        raise ValueError("image_url must be a URL string or object")
    url = obj.get("url")
    if not isinstance(url, str) or not url:
        raise ValueError("image_url.url is required")
    if url.lower().startswith("data:"):
        _data_image_bytes(url)
    elif not re.match(r"^https?://", url, re.I):
        raise ValueError("image URLs must use https://, http://, or a supported data URL")
    elif len(url) > 8192:
        raise ValueError("image URL is too long")
    return obj


def _normalize_openai_messages(messages):
    """Validate/canonicalize message content and return (messages, image_count).

    The hub never fetches image URLs itself. Known audio/video blocks fail
    explicitly; silently flattening them would answer a different question.
    """
    if not isinstance(messages, list):
        raise ValueError("messages must be an array")
    out, images, image_bytes = [], 0, 0
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("each message must be an object")
        row = copy.deepcopy(message)
        content = row.get("content")
        if not isinstance(content, list):
            out.append(row)
            continue
        blocks = []
        for block in content:
            if not isinstance(block, dict):
                blocks.append(block)
                continue
            btype = str(block.get("type") or "").lower()
            if btype in ("image_url", "input_image"):
                value = block.get("image_url")
                if value is None and block.get("url") is not None:
                    value = block.get("url")
                normalized = _normalize_image_url(value)
                images += 1
                if normalized["url"].lower().startswith("data:"):
                    image_bytes += _data_image_bytes(normalized["url"])
                blocks.append({"type": "image_url", "image_url": normalized})
            elif btype in ("input_audio", "audio", "video", "input_video"):
                raise ValueError("audio and video inputs are not supported by this hub")
            else:
                blocks.append(copy.deepcopy(block))
        row["content"] = blocks
        out.append(row)
    if images > MAX_IMAGE_COUNT:
        raise ValueError("at most %d images are allowed per request" % MAX_IMAGE_COUNT)
    if image_bytes > MAX_IMAGE_BYTES:
        raise ValueError("combined image payload exceeds the %d MiB limit"
                         % (MAX_IMAGE_BYTES // 1048576))
    return out, images


def _vision_candidates(est=0):
    """Available verified vision pairs in the persisted priority order."""
    available = []
    for pid in _available_providers():
        if not _provider_capable(pid, est):
            continue
        free = {m.lower(): m for m in provider_free_models(pid)}
        for verified in _vision_model_ids(pid):
            model = free.get(verified.lower())
            if model and prov.is_model_allowed(model) and not _is_model_dead(pid, model):
                available.append((pid, model))

    state = config.get_media_state()
    manual = state.get("manual_priority") if state.get("priority_mode") == "manual" else []
    by_id = {pid + "/" + model: (pid, model) for pid, model in available}
    ordered = []
    for item in manual or []:
        pair = by_id.pop(str(item), None)
        if pair:
            ordered.append(pair)
    # The automatic tail keeps manual mode resilient if a preferred model fails.
    tail = list(by_id.values())
    tail.sort(key=lambda pair: (_benchmark_score(pair[0], pair[1]),
                                _speed_score(pair[0], pair[1])), reverse=True)
    return ordered + tail


def _route_for_vision(messages, max_tokens=None, est=None, require_tools=False):
    if est is None:
        est = _est_tokens(messages)
    candidates = _vision_candidates(est)  # [(pid, model), ...]
    difficulty = _classify_difficulty(messages, max_tokens)
    if require_tools and candidates:
        # vision + tools: prefer a vision model that also calls tools; fail-open.
        candidates = [c for c in candidates if _supports_tools(c[0], c[1])] or candidates
    if not candidates:
        return None, None, difficulty
    pid, model = candidates[0]
    return pid, model, difficulty


_HARD_HINTS = (
    "refactor", "debug", "stack trace", "traceback", "algorithm", "architecture",
    "optimize", "optimise", "prove", "derive", "analyze", "analyse", "reason",
    "step by step", "step-by-step", "complex", "design a", "implement", "write code",
    "full code", "entire", "compile", "regex", "sql", "concurrency", "async",
    "benchmark", "vulnerab", "exploit", "math", "theorem",
    # coding-build heaviness (short asks like "build the auth module" are HEAVY even
    # though they're brief) -> route to the strongest coder, never a small model:
    "build", "create", "rewrite", "scaffold", "generate", "module", "backend",
    "frontend", "endpoint", "database", "schema", "migration", "component", "feature",
    "the whole", "the full", "a full", "complete ", "integrate", "wire ", "add auth",
    "authentication", "payment", "deploy", "dockerfile", "test suite", "unit test",
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


def _latest_user_text(messages):
    """Text of the LAST user message — the ACTUAL current ask. Heaviness is judged on
    THIS, not the whole request, so a fixed multi-KB system prompt (codex ships ~13K)
    doesn't make every agentic turn look 'hard' and hog the strongest models on a
    trivial sub-task ('run ls', 'read config')."""
    try:
        for m in reversed(messages or []):
            if isinstance(m, dict) and m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, list):
                    return " ".join((p.get("text") or "") for p in c if isinstance(p, dict))
                return str(c or "")
    except Exception:
        pass
    return ""


def _classify_difficulty(messages, max_tokens=None):
    """'simple' | 'medium' | 'hard' from the CURRENT ask's length, task hints, code,
    and the requested output size. Pure heuristic (no network). Judged on the latest
    user turn so a big fixed system prompt doesn't inflate every turn to 'hard'."""
    recent = _latest_user_text(messages) or _messages_text(messages)
    low = recent.lower()
    length = len(recent)
    score = 0
    if "```" in recent or re.search(r"\bdef \w+\(|\bclass \w+|function \w+\(|;\s*$", recent):
        score += 2
    hard_hits = sum(1 for h in _HARD_HINTS if h in low)
    score += hard_hits
    score -= sum(1 for h in _SIMPLE_HINTS if h in low)
    if length > 4000:
        score += 2
    elif length > 1500:
        score += 1
    elif length < 180 and hard_hits == 0:
        score -= 1   # short AND no heavy signal -> trivial; a short heavy ask is NOT
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
# Effective SINGLE-REQUEST token budget per provider. This is the pre-filter that
# keeps an oversized request off a provider that can't hold it. It must reflect the
# provider's CONTEXT window, NOT its per-minute rate: a real per-minute 429 is now
# handled by fall-through + throttle, and a genuine over-context 400 self-heals via
# _learn_context_limit. Sizing these to the per-minute rate (old groq=6000,
# default=20000) pre-filtered a typical agentic request (system prompt + tools +
# apply_patch + history ≈ 15-40K) OFF the strong large-context models
# (hy3/kimi/qwen3, all >=128K ctx) and onto the exhausted high-quota ones -> 503 storm.
_PROVIDER_TPM = {
    "groq": 120000, "github-models": 60000, "huggingface": 30000, "mistral": 120000,
    "morph": 30000, "sambanova": 120000, "cerebras": 60000, "deepseek": 120000,
    "openrouter": 128000, "cohere": 100000, "nvidia": 250000, "google": 900000,
    "cloudflare": 120000, "nararouter": 120000, "kimi": 128000, "glm": 128000,
    "aiand": 120000, "xiaomi": 60000, "minimax": 120000,
}
_DEFAULT_TPM = 100000


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
                elif isinstance(b, dict) and b.get("type") in ("image_url", "input_image"):
                    # Provider tokenization varies with resolution/detail. A
                    # conservative fixed allowance is enough for TPM routing.
                    chars += 4000
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            chars += len(str(fn.get("arguments") or "")) + len(str(fn.get("name") or ""))
    if tools:
        try:
            chars += len(json.dumps(tools))
        except Exception:
            pass
    return chars // 4 + 400  # + overhead for roles/formatting


def _record_chat_usage(hop_pid, hop_model, data, prompt_est):
    """Record usage from a completed OpenAI-shaped chat response `data` (the
    raw upstream JSON -- all three protocol handlers dispatch through the
    same OpenAI-shaped upstream call, so this is one shared hook point).
    Uses the REAL usage object when the provider returned one; otherwise
    falls back to the same char/4 estimate this file already uses elsewhere
    (_est_tokens). Never raises -- usage_history.record() already swallows
    its own errors, but guard the data-parsing here too."""
    try:
        usage = data.get("usage") if isinstance(data, dict) else None
        if isinstance(usage, dict) and (usage.get("prompt_tokens") or usage.get("completion_tokens")):
            usage_history.record(hop_pid, hop_model,
                                 usage.get("prompt_tokens") or 0,
                                 usage.get("completion_tokens") or 0, estimated=False)
            return
        content = ""
        choice = (data.get("choices") or [{}])[0] if isinstance(data, dict) else {}
        msg = choice.get("message") or {}
        if isinstance(msg.get("content"), str):
            content = msg["content"]
        usage_history.record(hop_pid, hop_model, prompt_est, len(content) // 4, estimated=True)
    except Exception:
        pass


def _provider_capable(pid, est):
    """Can this provider's free tier take a single `est`-token request? (margin
    for the model's own reply added.)"""
    if _is_sub(pid):
        # A local subscription CLI has no free-tier TPM ceiling to bust: it is the
        # user's own paid session, sized by the model's real context window. The
        # only size guard that applies is _SUB_MAX_PROMPT_CHARS, enforced at run
        # time in _sub_run(). Never let the free-tier filter drop it.
        return True
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
# the model is fine) and 5xx (transient upstream). 402/403/404 are treated as
# "this exact model is unusable with this key", because they are unambiguous:
#   404 = model doesn't exist on this provider (e.g. aiand/glm-5.2),
#   403 = this key has no access to it,
#   402 = payment required / insufficient credits — a trial provider with no free
#         balance (e.g. aiand/deepseek-v4) that would 402 EVERY turn otherwise.
# 400 is NOT auto-sidelined: it is just as often a bad payload as a bad model,
# and blocklisting a good model off one malformed request would be worse.
# All self-heal: entries expire after the TTL, so a topped-up/restored model returns.
# --------------------------------------------------------------------------- #
_DEAD_MODEL_TTL = 6 * 3600         # 6h, then re-probe (token fixed? model back?)
_dead_models = {}                  # (pid, model) -> expiry epoch
_dead_lock = threading.Lock()
_DEAD_STATUSES = (402, 403, 404)


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


# PROVIDER-level sideline. When a provider fails AUTH/credit (401/402/403) across
# several DIFFERENT models, the KEY itself is the problem (wrong token / no model
# access / out of credits — e.g. a github-models token with 403 on everything, or
# nararouter/aiand at 402 "balance 0"), so trying its other 20+ models every request
# just burns hops. Sideline the WHOLE provider for a while, then re-probe (a fixed
# token / topped-up balance revives it on its own).
_PROVIDER_DEAD_TTL = 30 * 60           # 30 min, then re-probe the provider
_PROVIDER_AUTHFAIL_THRESHOLD = 3       # distinct models failing auth -> kill provider
_AUTH_FAIL_STATUSES = (401, 402, 403)
_dead_providers = {}                   # pid -> expiry epoch
_provider_authfail = {}                # pid -> set(models that auth-failed this window)
_provider_dead_lock = threading.Lock()


def _mark_provider_authfail(pid, model, status):
    """Record an auth/credit failure; once enough DISTINCT models of a provider fail
    this way, the key is bad — sideline the whole provider (not just each model)."""
    if status not in _AUTH_FAIL_STATUSES:
        return
    with _provider_dead_lock:
        s = _provider_authfail.setdefault(pid, set())
        if model:
            s.add(str(model))
        if len(s) >= _PROVIDER_AUTHFAIL_THRESHOLD:
            _dead_providers[pid] = time.time() + _PROVIDER_DEAD_TTL


def _is_provider_dead(pid):
    with _provider_dead_lock:
        exp = _dead_providers.get(pid)
        if not exp:
            return False
        if exp <= time.time():
            _dead_providers.pop(pid, None)
            _provider_authfail.pop(pid, None)   # reset counter -> a clean re-probe
            return False
        return True


def _dead_provider_rows():
    now = time.time()
    with _provider_dead_lock:
        return [(pid, int(exp - now)) for pid, exp in _dead_providers.items() if exp > now]


def _dead_model_rows():
    """[(pid, model, seconds_left)] for the dashboard / diagnostics."""
    now = time.time()
    with _dead_lock:
        return [(p, m, int(exp - now)) for (p, m), exp in _dead_models.items() if exp > now]


# --------------------------------------------------------------------------- #
# LOCAL SUBSCRIPTION providers — OPT-IN, DEFAULT OFF ("sub-*").
#
# The user already pays for Claude Code and ChatGPT/Codex, and both CLIs are
# already signed in locally against those subscriptions. These two VIRTUAL
# providers let the hub use that PAID capacity as extra models alongside the free
# fleet, while keeping every bit of its orchestration (difficulty routing, chain
# fallback, dead-model tracking, quota accounting).
#
# They are deliberately NOT in providers.py: that module is the registry of HTTP
# api-key providers, and these have no base_url, no key and no /v1/models. Each
# one is a LOCAL SUBPROCESS driven through its CLI's documented non-interactive
# mode, as a plain text completion (no tool access, no permission bypass).
#
# HARD RULES — a sub hop spends the user's real money, so:
#   * THREE gates must all pass or the provider does not exist at all (master
#     flag + per-provider flag + installed & authenticated). The master flag is
#     OFF by default => zero behavior delta vs. the free-only hub.
#   * LAST RESORT ONLY: appended after BOTH free tiers in _build_chain, and
#     _route_by_difficulty may pick one as primary ONLY when no free candidate
#     exists at all.
#   * NEVER on streaming requests (a one-shot CLI cannot emit a token stream).
#   * NEVER when the CLI is currently pointed at this hub (that would be a
#     hub -> CLI -> hub loop). See _sub_loops_back().
# --------------------------------------------------------------------------- #
_SUB_MASTER_FLAG = "use_local_subscriptions"     # config flag, default False
_SUB_PROVIDERS = {
    "sub-claude": {
        "name": "Claude subscription (local)",
        "bin": "claude",             # resolved via shutil.which() at call time
        "model": "claude",           # exposed as 'sub-claude/claude'
        "cli_id": "claude",          # CLI_REGISTRY row (loop-guard reuse)
        "flag": "sub_claude_enabled",
        "isolated_flag": "sub_claude_isolated",   # opt-in, default OFF (see below)
    },
    "sub-codex": {
        "name": "Codex subscription (local)",
        "bin": "codex",
        "model": "codex",            # exposed as 'sub-codex/codex'
        "cli_id": "codex",
        "flag": "sub_codex_enabled",
        "isolated_flag": "sub_codex_isolated",
    },
}
_SUB_TIMEOUT = 120        # seconds for one run (CLI cold start + generation)
# `claude -p --output-format json` is known to HANG on very large prompts (~148KB
# observed). We use --output-format text, but keep a hard ceiling far below that:
# a sub hop is a last resort, not a bulk-context path. Over the cap the hop
# returns 413 and the chain moves on instead of freezing for the full timeout.
_SUB_MAX_PROMPT_CHARS = 100000


# --------------------------------------------------------------------------- #
# ISOLATED installs — OPT-IN, per-provider, default OFF.
#
# By default a sub-* hop runs the SAME `claude`/`codex` binary and config the
# user's own interactive terminal uses (whatever `shutil.which()` finds, reading
# ~/.claude or ~/.codex like normal). Some users want the hub's hop to be a
# COMPLETELY SEPARATE copy — signed into the SAME subscription, but never
# sharing config/credentials/session state with their own terminal. This block
# is that: a private npm --prefix install + the CLI's own OFFICIAL config-dir
# override env var, both scoped under ~/.free-llm-hub/isolated-clis/<cli_id>/.
#
# Env vars (verified against OFFICIAL docs, not guessed — see comments below):
#   codex  -> CODEX_HOME          confirmed: developers.openai.com/codex/environment-variables
#             ("Sets the root for Codex state... If you set it, the directory
#             must already exist" — Codex will NOT create it for you).
#   claude -> CLAUDE_CONFIG_DIR   confirmed: code.claude.com/docs/en/authentication
#             ("If you've set the CLAUDE_CONFIG_DIR environment variable on
#             Linux or Windows, the .credentials.json file lives under that
#             directory instead" — stated for Linux/Windows; macOS always uses
#             the system Keychain regardless of this var, which is fine here
#             since this hub only ever spawns a LOCAL subprocess, not macOS
#             Keychain-mediated auth).
#
# Both are confirmed, so isolation is fully implemented for BOTH providers —
# no guessed env var, no silent no-op.
#
# Login itself (OAuth/subscription sign-in) is NOT scriptable headlessly for
# either CLI — both vendors' docs require a human to complete a browser step
# (or, for Codex, enter a device code into a browser on any device) — see
# _isolated_login_command(). This hub can create the isolated dir, install the
# isolated binary, and hand the user the exact command to run themselves; it
# cannot click through OAuth consent for them.
# --------------------------------------------------------------------------- #
_ISOLATED_ENV_VAR = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
_ISOLATED_NPM_PACKAGE = {"claude": "@anthropic-ai/claude-code", "codex": "@openai/codex"}
_ISOLATED_INSTALL_TIMEOUT = 300   # npm install can be slow; this is an admin click, not a hop


def _isolated_root():
    """~/.free-llm-hub/isolated-clis — separate from wherever the user's own
    interactive install lives. Path only; no filesystem side effects."""
    return os.path.join(_home(), ".free-llm-hub", "isolated-clis")


def _isolated_cli_dir(cli_id):
    """~/.free-llm-hub/isolated-clis/<claude|codex>. Path only — see
    _ensure_isolated_dirs() for the actual mkdir."""
    return os.path.join(_isolated_root(), cli_id)


def _isolated_install_dir(cli_id):
    """`npm install -g <pkg> --prefix <this>` target for the isolated copy."""
    return os.path.join(_isolated_cli_dir(cli_id), "install")


def _isolated_config_dir(cli_id):
    """Value handed to CODEX_HOME / CLAUDE_CONFIG_DIR for the isolated copy."""
    return os.path.join(_isolated_cli_dir(cli_id), "config")


def _ensure_isolated_dirs(cli_id):
    """Create the isolated install+config dirs if missing. Never raises
    (best-effort — a failure here just means the caller's own next filesystem/
    subprocess call fails with its own clear error instead).

    Codex's docs are explicit it will NOT create CODEX_HOME itself ("the
    directory must already exist"), so this always runs BEFORE the isolated
    npm install and before any isolated subprocess env is built — for both
    providers, for consistency, even though only Codex is documented to need
    it pre-created."""
    for d in (_isolated_install_dir(cli_id), _isolated_config_dir(cli_id)):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass


def _isolated_bin_path(cli_id, bin_name):
    """Resolve the isolated binary, or None. Never raises. Pure read — does NOT
    create any directory (safe to call from a GET / dashboard render).

    npm's global-install layout under --prefix differs by OS: POSIX puts the
    launcher at <prefix>/bin/<name>; Windows puts the .cmd/.ps1 shim directly in
    <prefix> itself. shutil.which(..., path=...) already does PATHEXT-aware
    resolution (.cmd/.exe/etc on Windows, no extension on POSIX), so searching
    both candidate directories through it covers both layouts without
    hand-rolling an extension guess."""
    install_dir = _isolated_install_dir(cli_id)
    search = os.pathsep.join([install_dir, os.path.join(install_dir, "bin")])
    try:
        return shutil.which(bin_name, path=search)
    except Exception:
        return None


def _sub_isolated_on(pid):
    """The per-provider isolated-profile opt-in. DEFAULT FALSE — with it off,
    _sub_bin/_sub_env/_sub_state behave EXACTLY as they did before this feature
    (shared install, shared ~/.claude or ~/.codex)."""
    cfg = _SUB_PROVIDERS.get(pid)
    flag = cfg.get("isolated_flag") if cfg else None
    return bool(flag and config.get_flag(flag, False))


def _isolated_login_command(pid):
    """(command:str|None, note:str|None) — the EXACT command the user runs
    THEMSELVES in their own terminal to sign the isolated copy in.

    Neither CLI's subscription/OAuth login can be scripted headlessly — both
    vendors' docs require a human to complete a browser step (Claude: a login
    URL/code; Codex: the default browser callback OR `--device-auth`, which
    prints a code to enter into a browser on ANY device). So this hands back a
    ready-to-paste command with the isolated env var pre-set, never an attempt
    to drive the login itself. Returns (None, reason) when there's no isolated
    binary yet — install it first."""
    cfg = _SUB_PROVIDERS.get(pid)
    if not cfg:
        return None, "Unknown subscription provider."
    cli_id = cfg["cli_id"]
    bin_path = _isolated_bin_path(cli_id, cfg["bin"])
    if not bin_path:
        return None, "Install the isolated copy first."
    conf_dir = _isolated_config_dir(cli_id)
    var = _ISOLATED_ENV_VAR[cli_id]
    login_arg = " login" if cli_id == "codex" else ""   # `claude` itself walks first-launch login
    if os.name == "nt":
        cmd = "$env:%s = '%s'; & '%s'%s" % (var, conf_dir, bin_path, login_arg)
    else:
        cmd = "%s='%s' '%s'%s" % (var, conf_dir, bin_path, login_arg)
    return cmd, None


def _is_sub(pid):
    """True for a local-subscription virtual provider id ('sub-claude'/'sub-codex')."""
    return pid in _SUB_PROVIDERS


def _sub_models(pid):
    """The model id(s) a sub provider exposes (one each, by design)."""
    cfg = _SUB_PROVIDERS.get(pid)
    return [cfg["model"]] if cfg else []


def _sub_master_on():
    """The master opt-in. DEFAULT FALSE — with it off, nothing below ever runs."""
    return bool(config.get_flag(_SUB_MASTER_FLAG, False))


def _sub_bin(pid):
    """Absolute path to the CLI binary, or None. Never raises.

    When the isolated profile is ON for this provider, resolves ONLY inside its
    isolated install dir — it deliberately does NOT fall back to the shared
    PATH copy, since silently mixing the two would defeat the point of
    isolation (a "not installed" isolated provider must show as not installed,
    even if the user's regular `claude`/`codex` is right there on PATH)."""
    cfg = _SUB_PROVIDERS.get(pid)
    if not cfg:
        return None
    try:
        if _sub_isolated_on(pid):
            return _isolated_bin_path(cfg["cli_id"], cfg["bin"])
        return shutil.which(cfg["bin"])
    except Exception:
        return None


def _codex_subscription_auth(codex_home=None):
    """(ok, detail) for <codex_home>/auth.json — is Codex signed in with a ChatGPT
    SUBSCRIPTION (not an API key)? Reads the file's shape only; no token is ever
    returned, logged or copied. Never raises.

    codex_home=None (default) checks the shared ~/.codex — byte-identical to
    this function's original behavior. Pass the isolated config dir instead to
    check an isolated profile's own auth.json (same file shape, different
    CODEX_HOME) — the message text switches to the actual path in that case.

    auth_mode == 'chatgpt' (or an OAuth token pair) == subscription. An
    API-key-only auth.json is deliberately REJECTED: that bills per token, which
    is not what this feature offers."""
    base = codex_home or os.path.join(_home(), ".codex")
    path = os.path.join(base, "auth.json")
    label = "~/.codex/auth.json" if codex_home is None else _short(path)
    if not os.path.isfile(path):
        return False, "Not signed in (no %s). Run: codex login" % label
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False, "%s is unreadable or not valid JSON." % label
    if not isinstance(data, dict):
        return False, "%s has an unexpected shape." % label
    mode = str(data.get("auth_mode") or "").lower()
    tokens = data.get("tokens")
    has_tokens = isinstance(tokens, dict) and bool(
        tokens.get("access_token") or tokens.get("refresh_token"))
    if mode == "chatgpt":
        return True, "Signed in with a ChatGPT subscription (auth_mode=chatgpt)."
    if has_tokens:
        return True, "Signed in (OAuth session present in %s)." % label
    return False, ("%s holds no ChatGPT subscription session "
                   "(API-key mode). Run: codex login" % label)


def _sub_loops_back(cli_id):
    """(loops, detail) — True when that CLI is CURRENTLY POINTED AT THIS HUB.

    This hub's own Auto-fix writes ANTHROPIC_BASE_URL / ~/.codex/config.toml to
    point a CLI here. Spawning such a CLI from inside the hub would make the hub
    call ITSELF (hub -> CLI -> hub -> ...) until something times out. So a
    connected CLI is withheld as a subscription provider. Reuses the existing
    connection detector, so it stays true to whatever Auto-fix/Disconnect did."""
    entry = _get_cli_entry(cli_id)
    if not entry:
        return False, None
    try:
        connected, _method, detail = _cli_connected(entry)
    except Exception:
        return False, None       # fail open: detection problems don't block the user
    if connected:
        return True, ("%s is currently connected to this hub (%s). Using it as a "
                      "subscription provider would make the hub call itself — "
                      "disconnect it first."
                      % (entry.get("name", cli_id), detail or "config/env"))
    return False, None


def _sub_state(pid):
    """(enabled, installed, authenticated, detail) for one sub provider.

    Pure inspection: never runs the CLI, never touches the filesystem beyond
    reads (so it costs nothing and is safe on every dashboard poll), never
    raises."""
    cfg = _SUB_PROVIDERS.get(pid)
    if not cfg:
        return False, False, False, "Unknown subscription provider."
    enabled = bool(config.get_flag(cfg["flag"], True))   # per-provider default ON
    isolated = _sub_isolated_on(pid)
    path = _sub_bin(pid)
    if not path:
        if isolated:
            return enabled, False, False, ("Isolated copy not installed yet (looked under %s). "
                                           "Click \"Install isolated copy\"."
                                           % _short(_isolated_install_dir(cfg["cli_id"])))
        return enabled, False, False, "Not installed (no '%s' on PATH)." % cfg["bin"]
    if isolated:
        # An isolated install reads ONLY its own CODEX_HOME/CLAUDE_CONFIG_DIR — a
        # directory Auto-fix never writes to — so it can NEVER loop back into this
        # hub. Checking the SHARED CLI entry's connection status here would wrongly
        # block isolation for exactly the user who ALSO has their main CLI
        # connected via Auto-fix (arguably the main reason to want isolation in the
        # first place), so the loop-guard is skipped for an isolated profile.
        loops, loop_detail = False, None
    else:
        loops, loop_detail = _sub_loops_back(cfg["cli_id"])
    if loops:
        return enabled, True, False, loop_detail
    if pid == "sub-codex":
        codex_home = _isolated_config_dir("codex") if isolated else None
        ok, detail = _codex_subscription_auth(codex_home)
        return enabled, True, ok, detail
    # sub-claude: do NOT try to parse Claude Code's credentials. They live across
    # an OS keychain / OAuth store / managed settings depending on the install, so
    # any check here would be a guess that wrongly hides a working CLI. Installed
    # == usable; a failed run marks the model dead and routing skips it for 6h.
    where = "an isolated profile" if isolated else "the local Claude Code session"
    return enabled, True, True, ("Installed (%s). Uses %s; "
                                 "a failed run sidelines it automatically." % (_short(path), where))


def _sub_available_providers():
    """Sub provider ids usable RIGHT NOW (master flag + per-provider flag +
    installed + authenticated + no hub loop + not dead).

    Returns [] whenever the master flag is off — which is the default, so every
    caller below is a no-op on a stock hub. NOTE: deliberately NOT merged into
    _available_providers(): that function feeds _best_free_pair() /
    aggregated_models() / the FREE quota banner, and a paid subscription must
    never leak into "best FREE model" or be auto-persisted as the default."""
    if not _sub_master_on():
        return []
    out = []
    for pid, cfg in _SUB_PROVIDERS.items():
        enabled, _installed, authed, _detail = _sub_state(pid)
        if enabled and authed and not _is_model_dead(pid, cfg["model"]):
            out.append(pid)
    return out


# Chat roles -> readable labels for a CLI that only takes plain text.
_SUB_ROLE_LABEL = {"system": "System", "developer": "System", "user": "User",
                   "assistant": "Assistant", "tool": "Tool result"}


def _sub_flatten(messages):
    """OpenAI chat messages -> ONE readable prompt string.

    Content blocks ([{type:'text',text:..}]) are flattened; non-text parts
    (images) are dropped — a sub hop is a text completion. A single lone user
    message is passed through verbatim (the common case: no labels added)."""
    msgs = [m for m in (messages or []) if isinstance(m, dict)]
    if len(msgs) == 1 and isinstance(msgs[0].get("content"), str) \
            and str(msgs[0].get("role") or "user").lower() == "user":
        return msgs[0]["content"].strip()
    parts = []
    for m in msgs:
        role = _SUB_ROLE_LABEL.get(str(m.get("role") or "user").lower(), "User")
        c = m.get("content")
        text = ""
        if isinstance(c, str):
            text = c
        elif isinstance(c, list):
            text = "\n".join(b["text"] for b in c
                             if isinstance(b, dict) and isinstance(b.get("text"), str)
                             and b.get("text"))
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") if isinstance(tc, dict) else None
            if isinstance(fn, dict):
                text += "\n[tool call] %s(%s)" % (fn.get("name") or "",
                                                  fn.get("arguments") or "")
        text = text.strip()
        if text:
            parts.append("%s: %s" % (role, text))
    return "\n\n".join(parts)


def _sub_launcher(path):
    """argv prefix that can actually execute `path`.

    On Windows an npm-installed CLI is a .cmd/.bat shim (codex -> codex.CMD):
    CreateProcess cannot run a batch file directly, so it must go through the
    command interpreter. A native .exe (and every POSIX binary) runs directly.
    Only the interpreter + the resolved path are passed here — the prompt goes in
    on stdin, so no untrusted text is ever handed to cmd.exe for parsing."""
    if os.name == "nt" and os.path.splitext(path)[1].lower() in (".cmd", ".bat"):
        return [os.environ.get("COMSPEC") or "cmd.exe", "/c", path]
    return [path]


def _sub_env(pid=None):
    """Child env with every HUB-POINTING override stripped, so the CLI talks to
    its own subscription backend and can't be redirected back into this hub.
    Defense in depth — _sub_loops_back() already refuses a CLI configured to
    point here; this also covers a hub process that merely inherited such a var.
    Everything else (PATH, HOME, the user's own settings) is passed through.

    pid=None (default) is byte-identical to this function's original behavior.
    When `pid` is given AND its isolated profile is on, this ALSO points that
    CLI's own config-dir override env var (CODEX_HOME / CLAUDE_CONFIG_DIR) at
    its isolated config dir (creating it first — Codex refuses to use a
    CODEX_HOME that doesn't already exist), so the subprocess never touches
    ~/.claude or ~/.codex at all."""
    env = dict(os.environ)
    for k in list(env.keys()):
        if _points_at_hub(env.get(k)):
            env.pop(k, None)
    if pid and _sub_isolated_on(pid):
        cfg = _SUB_PROVIDERS.get(pid) or {}
        cli_id = cfg.get("cli_id")
        var = _ISOLATED_ENV_VAR.get(cli_id)
        if var and cli_id:
            _ensure_isolated_dirs(cli_id)
            env[var] = _isolated_config_dir(cli_id)
    return env


# Codex prints a banner + event log on stdout. `-o/--output-last-message` gives us
# the final message exactly, so this stripper is only a FALLBACK for when that
# file comes back empty. Best-effort by design: drop the known banner/meta lines
# and keep the rest.
_CODEX_NOISE_RE = re.compile(
    r"^\s*(-{3,}|_{3,}|\[?\d{4}-\d{2}-\d{2}T?[\d:.]*\]?\s|>_|OpenAI Codex|codex\b|"
    r"(workdir|model|provider|approval|sandbox|reasoning( effort| summaries)?|"
    r"session|version|tokens used|user instructions?)\s*:)", re.I)


def _codex_strip_noise(out):
    """Best-effort: strip Codex's banner/meta lines from stdout. Fallback only."""
    lines = [ln for ln in (out or "").splitlines() if not _CODEX_NOISE_RE.match(ln)]
    return "\n".join(lines).strip()


def _read_text(path):
    """Read a file, '' on any problem. Never raises."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


# Substrings that mean "this CLI is not usable with this session" -> mark dead so
# routing stops picking it for 6h (instead of retrying a broken login every hop).
_SUB_AUTH_ERR = ("not logged in", "not authenticated", "unauthorized", "401",
                 "please run /login", "please login", "run `codex login`",
                 "run codex login", "invalid api key", "no credentials",
                 "authentication_error", "session expired", "oauth")


def _sub_run(pid, prompt):
    """Run the local CLI ONCE, non-interactively. NEVER raises.

    Returns (status, text, detail) where `status` mirrors an HTTP code the chain
    loops already understand:
      200 -> `text` is the assistant's reply
      403 -> unusable (off / not installed / not signed in / loops back) -> DEAD
      413 -> prompt over _SUB_MAX_PROMPT_CHARS (request-specific, NOT dead)
      504 -> timed out       502 -> ran but failed / produced nothing

    Invocation (flags verified against `claude --help` / `codex exec --help`):
      claude -> `claude -p --output-format text`, prompt on STDIN (print mode
        reads a piped stdin as the prompt). NO --dangerously-skip-permissions and
        no tool flags: a plain text completion, nothing else. NOT `--bare` either
        — that mode refuses to read the OAuth session and demands an API key,
        i.e. the exact opposite of "use my subscription".
      codex  -> `codex exec --skip-git-repo-check --color never --sandbox
        read-only -o <tmp> -`. The trailing '-' reads the prompt from STDIN, and
        -o writes ONLY the final assistant message to <tmp>, so Codex's banner /
        event noise on stdout never has to be parsed at all.

    The prompt always travels on STDIN, never in argv: a Windows command line
    caps around 8k chars, and this can carry a whole conversation.
    cwd is a temp dir so neither CLI picks up THIS repo as project context."""
    cfg = _SUB_PROVIDERS.get(pid)
    if not cfg:
        return 403, "", "Unknown subscription provider '%s'." % pid
    if not _sub_master_on():
        return 403, "", "Local subscription providers are turned off."
    enabled, installed, authed, detail = _sub_state(pid)
    if not enabled:
        return 403, "", "%s is switched off." % cfg["name"]
    if not (installed and authed):
        return 403, "", detail or "%s is not usable." % cfg["name"]
    if not prompt:
        return 502, "", "Nothing to send (empty prompt)."
    if len(prompt) > _SUB_MAX_PROMPT_CHARS:
        return 413, "", ("Prompt is %d chars; the local %s CLI is capped at %d here "
                         "(a CLI hangs on very large prompts)."
                         % (len(prompt), cfg["bin"], _SUB_MAX_PROMPT_CHARS))
    path = _sub_bin(pid)
    if not path:
        return 403, "", "'%s' is no longer on PATH." % cfg["bin"]
    tmp_out = None
    try:
        if pid == "sub-codex":
            try:
                fd, tmp_out = tempfile.mkstemp(prefix="hub-sub-", suffix=".txt")
                os.close(fd)
            except OSError as exc:
                return 502, "", "Could not create a temp file: %s" % exc.__class__.__name__
            argv = _sub_launcher(path) + ["exec", "--skip-git-repo-check",
                                          "--color", "never", "--sandbox", "read-only",
                                          "-o", tmp_out, "-"]
        else:
            argv = _sub_launcher(path) + ["-p", "--output-format", "text"]
        try:
            proc = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=_SUB_TIMEOUT, env=_sub_env(pid),
                                  cwd=tempfile.gettempdir())
        except subprocess.TimeoutExpired:
            return 504, "", "%s timed out after %ds." % (cfg["bin"], _SUB_TIMEOUT)
        except (OSError, ValueError) as exc:
            return 502, "", "%s failed to start: %s" % (cfg["bin"], exc.__class__.__name__)
        text = (proc.stdout or "").strip()
        if pid == "sub-codex":
            last = _read_text(tmp_out).strip()
            text = last or _codex_strip_noise(proc.stdout)
        if not text:
            err = _sanitize((proc.stderr or "").strip(), 300)
            if proc.returncode != 0:
                low = (err or "").lower()
                status = 403 if any(s in low for s in _SUB_AUTH_ERR) else 502
                return status, "", ("%s exited %d: %s"
                                    % (cfg["bin"], proc.returncode, err or "no detail"))
            return 502, "", "%s produced no output. %s" % (cfg["bin"], err or "")
        return 200, text, None
    finally:
        if tmp_out:
            try:
                os.unlink(tmp_out)
            except OSError:
                pass


class _SubResponse:
    """A minimal `requests.Response` look-alike — EXACTLY the surface the chain
    loops touch (.status_code / .json() / .text / .close()), so a sub-* hop flows
    through the same loop as an HTTP provider with no special-casing.

    iter_content/iter_lines exist only so that a hypothetical streaming caller
    degrades to the loops' "no first byte" fall-through instead of raising
    AttributeError. The loops already skip sub hops when stream is requested."""

    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)
        self.headers = {}

    def json(self):
        return self._payload

    def close(self):
        return None

    def iter_content(self, chunk_size=None):
        return iter(())

    def iter_lines(self, decode_unicode=False):
        return iter(())


def _subscription_chat(pid, payload):
    """The sub-* twin of _upstream_chat(): run the user's local, already-signed-in
    CLI and return an OpenAI chat-completions response shim. Never raises, never
    streams. Shape matches _upstream_chat's contract so every downstream
    translator (_chat_to_responses / _openai_resp_to_anthropic) just works."""
    cfg = _SUB_PROVIDERS.get(pid) or {}
    model = payload.get("model") or cfg.get("model") or "cli"
    prompt = _sub_flatten(payload.get("messages"))
    status, text, detail = _sub_run(pid, prompt)
    # Count usage like any other provider so the dashboard shows it. sub-* has no
    # researched row in quota.FREE_LIMITS, so it inherits DEFAULT_LIMIT
    # (limit: None) -> reported as UNKNOWN and NEVER as exhausted, which is right:
    # a subscription's remaining budget is not something this hub can know.
    quota.record(pid, model)
    if status in _DEAD_STATUSES:
        _mark_model_dead(pid, model, status)
    if status != 200:
        return _SubResponse(status, {"error": {
            "message": "%s: %s" % (cfg.get("name", pid), detail or "run failed"),
            "type": "upstream_error", "code": status}})
    # A CLI reports no token accounting, so usage is ESTIMATED (chars/4) — same
    # heuristic the rest of the hub uses for sizing.
    pt = max(1, len(prompt) // 4)
    ct = max(1, len(text) // 4)
    return _SubResponse(200, {
        "id": "chatcmpl-sub-" + uuid.uuid4().hex,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
    })


def _dispatch_chat(pid, payload, stream):
    """Single entry point for the chain loops: a local subscription CLI for a
    sub-* hop, the HTTP upstream for everything else. Keeps the loops
    provider-agnostic and the HTTP path byte-identical to before."""
    if _is_sub(pid):
        return _subscription_chat(pid, payload)
    return _upstream_chat(pid, payload, stream)


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


# --------------------------------------------------------------------------- #
# Tool/function-calling capability. A request carrying a non-empty tools schema
# (Codex over /v1/responses incl. spawn_agent; Claude Code over /v1/messages;
# any /v1/* with tools) must route to a model that can DO function calling —
# never a completion-only model (codestral, tiny/base models). Lowercase
# substrings; NOT_TOOL_CAPABLE (exclusions) always wins. FAIL-OPEN: an id in
# NEITHER list is assumed capable, so a new/unknown model is never dropped.
# --------------------------------------------------------------------------- #
NOT_TOOL_CAPABLE = (
    "codestral", "codestral-mamba", "mamba-codestral",
    "-fim", "/fim", "fim-", "fill-in", "text-completion",
    "qwen2.5-coder", "qwen2-5-coder", "qwen2.5coder",
    "deepseek-coder-v2", "deepseek-coder-6.7", "deepseek-coder-33",
    "-1b", "-1.5b", "-1.1b", "-2b", "-3b", "-0.5b",
    "llama-3.2-1b", "llama-3.2-3b", "llama3.2:1b", "llama3.2:3b",
    "tinyllama", "gemma-2b", "gemma-2-2b", "qwen1.5-0.5b", "qwen2-0.5b",
    "mixtral", "mistral-7b-instruct-v0.1", "mistral-7b-instruct-v0.2",
    "open-mistral-7b", "open-mixtral",
    "gemma-2-", "gemma-3-", "gemma:2", "gemma:3",
    "embed", "rerank", "-mt", "hunyuan-mt", "whisper", "-tts", "-asr",
    "-ocr", "guard", "llama-guard", "moderation",
    "nova-canvas", "nova-reel", "flux", "stable-diffusion", "sdxl",
    "-vl-", "-vision-only",
)
TOOL_CAPABLE = (
    "deepseek-chat", "deepseek-v3", "deepseek-r1", "deepseek-reasoner",
    "qwen2.5-", "qwen-2.5", "qwen3", "qwen-3", "qwen-max", "qwen-plus", "qwen-turbo", "qwq",
    "kimi", "moonshot", "-k2",
    "glm-4.5", "glm-4.6", "glm-4.7", "glm-5", "zai", "z-ai",
    "llama-3.1", "llama3.1", "llama-3.3", "llama3.3", "llama-4", "llama4", "scout", "maverick",
    "gpt-oss", "gpt-4", "gpt-5", "claude", "command-r", "nemotron",
    "hunyuan-large", "hunyuan-turbo", "hunyuan-a13b", "hunyuan-3", "hy3",
    "mistral-large", "mistral-medium", "mistral-small", "ministral", "devstral", "magistral",
    "pixtral-large", "voxtral-small",
    "gemini", "gemma-4", "functiongemma",
    "nova-micro", "nova-lite", "nova-pro", "nova-premier",
    "minimax", "abab6.5",
)


def _supports_tools(pid, model):
    """True if (pid, model) can do OpenAI function/tool calling. FAIL-OPEN: an id
    matching NEITHER list is assumed capable, so a new/unknown model is never
    silently dropped from a tools request. An explicit NOT_TOOL_CAPABLE hit
    (codestral, base/completion-only models) always wins over TOOL_CAPABLE."""
    low = (model or "").lower()
    if any(n in low for n in NOT_TOOL_CAPABLE):
        return False
    return True  # fail-open (TOOL_CAPABLE documents known-good; unknown -> allow)


def _quota_headroom(pid: str) -> float:
    """Fraction of a provider's free budget still available: 1.0 = fresh (or an
    unknown/uncapped ceiling), 0.0 = spent. Used ONLY as a TIEBREAKER among
    equal-benchmark models so the router keeps using a provider that still has
    quota instead of re-picking a nearly-drained one — it NEVER overrides model
    quality (a weaker-but-fresh model can't jump a stronger one). Fully-exhausted
    providers are already dropped upstream by _available_providers()."""
    try:
        s = quota.status(pid)
    except Exception:
        return 1.0
    if not s.get("limit_known"):      # no researched ceiling -> treat as fresh
        return 1.0
    lim = s.get("limit") or 0
    if lim <= 0:                      # documented no-free-tier (already excluded)
        return 0.0
    rem = s.get("remaining")
    if rem is None:
        return 1.0
    return max(0.0, min(1.0, rem / lim))


# Orchestrator load-spreading. On agentic/hard turns the strict "always the single
# best model" rule made codex hammer ONE provider+model for a whole project, so that
# provider's quota drained while every other strong model sat idle. Instead ROTATE
# across the TOP-TIER BAND (models within _ORCH_BAND points of the best) turn by
# turn: same quality, but consumption spreads across providers and a single account's
# budget lasts far longer. The current best id keeps DOUBLE weight, so hy3 (or a
# future kimi-k3) still leads the rotation — "prioritize hy3" AND "mix the best ones"
# both hold. Rotation only runs in Auto/orchestrate mode; an explicit '<pid>/<model>'
# request bypasses the router entirely and is untouched.
_ORCH_BAND = 30.0
# Agentic/coding (require_tools) floor: only models scoring at/above this (S-tier on
# the _benchmark_score scale — hy3/qwen3-coder/deepseek-v4/kimi-k2/glm-5.2/
# gpt-oss-120b class) may serve a coding agent. Below it a model plans then
# under-builds. Fail-open when nothing clears it (weak/exhausted pool).
_TOOLS_MIN_SCORE = 90.0   # include the deep-quota strong coders (gpt-oss-120b 99 /
                          # glm-4.7 94 / deepseek-v3 92 on cerebras+groq+github) in the
                          # agentic pool — they're the sustainable workhorses when the
                          # shallow top-tier (openrouter/google) burns out. Was 100,
                          # which excluded them and forced the cascade onto weak mistral.
_orch_cursor = 0
_orch_lock = threading.Lock()


def _spread_pick(pool):
    """pool = [(score, pid, model)] of fast candidates. Return one top-tier entry,
    rotating across the band so consecutive agentic turns land on DIFFERENT strong
    providers. The top-scored model gets double weight (stays the lead). Exhausted
    models are already filtered out of `pool` upstream."""
    global _orch_cursor
    if not pool:
        return None
    top = max(p[0] for p in pool)
    band = [p for p in pool if p[0] >= top - _ORCH_BAND]
    band.sort(key=lambda t: (-t[0], t[1], t[2]))    # deterministic, best first
    ring = [band[0]] + band                          # best id twice -> ~2x weight
    with _orch_lock:
        pick = ring[_orch_cursor % len(ring)]
        _orch_cursor += 1
    return pick


def _route_by_difficulty(messages, max_tokens=None, est=None, require_tools=False):
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
        for m in _auto_models(pid):
            # skip ids this key provably can't use (403/404 learned at runtime) and
            # ids individually rate-limited / over their per-model sub-cap.
            if (prov.is_model_allowed(m) and not _is_model_dead(pid, m)
                    and not quota.is_model_throttled(pid, m)
                    and not quota.model_status(pid, m)["exhausted"]
                    and _context_ok(pid, m, est)):
                cands.append((_benchmark_score(pid, m), pid, m))
    if require_tools:
        # A tools request must never land on a completion-only model. FAIL-OPEN:
        # keep the unfiltered list if NO candidate is known tool-capable.
        cands = [c for c in cands if _supports_tools(c[1], c[2])] or cands
    if not cands:
        # No FREE model exists at all (nothing keyed/enabled, or every candidate is
        # dead/exhausted). ONLY here may an opt-in local subscription become the
        # primary — it costs the user real money, so it never competes with a free
        # model for that slot. With the master flag off this list is empty and the
        # function returns exactly what it always did.
        for sub_pid in _sub_available_providers():
            for m in _sub_models(sub_pid):
                return sub_pid, m, difficulty
        return None, None, difficulty
    # Prefer FAST models — the primary should be a good model the user won't wait
    # on. Slow reasoning models are used only if NO fast model is available (and
    # they still appear later in _build_chain as a last-resort fallback).
    pool = [c for c in cands if _is_fast(c[1], c[2])] or cands
    if require_tools:
        # CODING/AGENTIC: the primary is ALWAYS a STRONG model (>= _TOOLS_MIN_SCORE) —
        # a weak model plans then under-builds, and mistral (56) only ever appears
        # later in the fallback CHAIN once these are exhausted, never as the primary.
        # Fail-open: if nothing clears the bar (all strong keys weak/exhausted), keep
        # the full pool rather than fail.
        agentic = [c for c in pool if c[0] >= _TOOLS_MIN_SCORE] or pool
        if difficulty == "hard":
            # HEAVY coding -> the STRONGEST, SPREAD across the top band (hy3 leads, then
            # kimi/qwen/deepseek) so consecutive heavy turns + codex sub-agents mix the
            # best models instead of pinning one and draining its quota.
            picked = _spread_pick(agentic) or max(
                agentic, key=lambda t: (t[0], _quota_headroom(t[1])))
        else:
            # LIGHTER coding (simple/medium sub-task) -> the CHEAPEST model that is STILL
            # STRONG, tie to most free quota. This leans on the deep-quota strong coders
            # (gpt-oss-120b on cerebras 14400/day, glm-4.7, deepseek-v3) and SAVES the
            # scarce top models (hy3 openrouter 50/day) for the heavy turns — "best by
            # heaviness", never a weak model for coding.
            picked = min(agentic, key=lambda t: (t[0], -_quota_headroom(t[1])))
        _s, pid, model = picked
        return pid, model, difficulty
    if difficulty == "hard":
        # Non-tool HARD -> strongest fast model (spread keeps variety across turns).
        picked = _spread_pick(pool) or max(pool, key=lambda t: (t[0], _quota_headroom(t[1])))
        _s, pid, model = picked
        return pid, model, difficulty
    floor = _DIFFICULTY_FLOOR[difficulty]
    qualified = [c for c in pool if c[0] >= floor]
    if qualified:
        # cheapest fast model that still clears the bar -> saves strong quota;
        # tie among equal-cheap models -> the one with the MOST free quota left
        # (-headroom so min() picks lowest score THEN highest remaining budget).
        _s, pid, model = min(qualified, key=lambda t: (t[0], -_quota_headroom(t[1])))
    else:
        _s, pid, model = max(pool, key=lambda t: (t[0], _quota_headroom(t[1])))
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
        # Explicit local-subscription pick ('sub-codex/codex'). Honored ONLY while
        # that provider is actually enabled+usable, and answered with an honest
        # error otherwise — never silently downgraded onto the default provider
        # (which would send a nonsense 'sub-codex/codex' model id upstream), and
        # never able to spend the subscription while the feature is switched off.
        if _is_sub(head):
            if head in _sub_available_providers():
                return head, (rest or _SUB_PROVIDERS[head]["model"])
            if not _sub_master_on():
                return None, ("Local subscription providers are off. Turn them on "
                              "first — they spend your PAID Claude/ChatGPT plan.")
            _e, _i, _a, detail = _sub_state(head)
            return None, ("%s is not available: %s"
                          % (_SUB_PROVIDERS[head]["name"], detail or "disabled"))

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
    if _is_sub(pid):
        # A local subscription provider has no key and no base_url: its gates are
        # the two flags, the binary, and the CLI's own local session.
        if not _sub_master_on():
            return ("Local subscription providers are off. Turn them on first — "
                    "they spend your PAID Claude/ChatGPT plan.")
        enabled, _installed, authed, detail = _sub_state(pid)
        if not enabled:
            return "%s is switched off." % _SUB_PROVIDERS[pid]["name"]
        if not authed:
            return detail or ("%s is not usable." % _SUB_PROVIDERS[pid]["name"])
        return None
    if not prov.get_provider(pid):
        return "Unknown provider '%s'." % pid
    pcfg = config.get_provider_config(pid)
    if not pcfg.get("api_key") and _needs_key(pid):
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


def _build_chain(primary_pid, model_id, est=0, require_vision=False, require_tools=False):
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
        for m in _auto_models(pid):
            if (pid, m) in seen or not prov.is_model_allowed(m) or _is_model_dead(pid, m):
                continue
            # skip a model that's individually rate-limited or over its per-model cap
            if quota.is_model_throttled(pid, m) or quota.model_status(pid, m)["exhausted"]:
                continue
            if not _context_ok(pid, m, est):   # learned too-small context for this request
                continue
            if require_vision and not _is_vision_model(pid, m):
                continue
            entry = (_benchmark_score(pid, m), pid, m)
            (fast if _is_fast(pid, m) else slow).append(entry)
    # best model first; tie among equal-score models -> most free quota left, so
    # the fallback chain keeps using providers that still have budget.
    fast.sort(key=lambda t: (t[0], _quota_headroom(t[1])), reverse=True)
    slow.sort(key=lambda t: (t[0], _quota_headroom(t[1])), reverse=True)
    if require_tools:
        # AGENTIC/coding: order by STRENGTH, not speed. A strong deep-quota model
        # (gpt-oss-120b / glm-4.7 / deepseek on cerebras+groq — flagged 'slow' for
        # their size but actually fast on those providers, and cerebras has 14400/day)
        # MUST be tried BEFORE a fast-but-weak model (mistral, score 56). Otherwise
        # the fast/slow split buries the strong deep-quota models behind mistral and
        # codex cascades onto mistral while they sit unused. FAIL-OPEN on tool-capable.
        ordered = sorted(fast + slow, key=lambda t: (t[0], _quota_headroom(t[1])), reverse=True)
        ordered = [e for e in ordered if _supports_tools(e[1], e[2])] or ordered
    else:
        ordered = fast + slow
    for _score, pid, m in ordered:
        if len(chain) >= MAX_HOPS:
            break
        if (pid, m) not in seen:
            chain.append((pid, m))
            seen.add((pid, m))
    # LAST RESORT — the user's PAID local subscriptions, opt-in and OFF by
    # default (so this loop normally adds NOTHING and the chain is identical to
    # before). Appended after BOTH free tiers: a sub hop must only ever run once
    # every free model has been tried and failed.
    # Deliberately allowed past MAX_HOPS: that cap bounds free-provider fan-out,
    # and the explicit last-resort fallback the user opted into must not be
    # crowded out by the very free models that just failed.
    for pid in ([] if require_vision else _sub_available_providers()):
        if not _provider_capable(pid, est):   # always True today; keeps the rule honest
            continue
        for m in _sub_models(pid):
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


def _sanitize_tool_messages(messages):
    """Rebuild a chat history into the CANONICAL tool-calling shape strict upstreams
    require — every assistant `tool_calls` message IMMEDIATELY followed by exactly one
    tool message per tool_call_id, in order. Fixes the 400 'An assistant message with
    tool_calls must be followed by tool messages ... did not have response messages':
      * DANGLING tool_call (no result — the turn failed/was cut off) -> stub result;
      * OUT-OF-ORDER result (assistant -> user -> tool) -> moved to right after its
        call (a plain presence check misses this — it's why codex kept 400ing);
      * ORPHAN tool message (no matching call) -> dropped.
    Returns the SAME list untouched when there are NO tool_calls/tool messages at all
    (the vast majority of requests) so nothing is copied needlessly."""
    if not isinstance(messages, list) or not messages:
        return messages
    has_tools = False
    tool_by_id = {}        # tool_call_id -> its tool message (first occurrence wins)
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "tool":
            has_tools = True
            tcid = m.get("tool_call_id")
            if tcid is not None:
                tool_by_id.setdefault(tcid, m)
        elif role == "assistant" and isinstance(m.get("tool_calls"), list):
            if any(isinstance(tc, dict) and tc.get("id") for tc in m["tool_calls"]):
                has_tools = True
    if not has_tools:
        return messages
    out = []
    for m in messages:
        if not isinstance(m, dict):
            out.append(m)
            continue
        if m.get("role") == "tool":
            continue  # every tool message is (re)emitted next to its call, below
        out.append(m)
        if m.get("role") == "assistant" and isinstance(m.get("tool_calls"), list):
            for tc in m["tool_calls"]:
                if not (isinstance(tc, dict) and tc.get("id")):
                    continue
                tid = tc["id"]
                out.append(tool_by_id.get(tid) or {
                    "role": "tool", "tool_call_id": tid,
                    "content": "(tool result unavailable)"})
    return out


def _model_ctx_budget(pid, model):
    """Best estimate of a model's usable INPUT context: the limit LEARNED from a real
    400 (authoritative) if we have one, else the provider's context-sized _PROVIDER_TPM."""
    lim = _MODEL_MAX_INPUT.get((pid, model))
    if isinstance(lim, int) and lim > 0:
        return lim
    return _provider_tpm(pid)


def _compact_to_budget(messages, tools, budget):
    """AUTO-COMPACT: if a conversation is bigger than a model's context budget, drop
    the OLDEST turns (keeping ALL leading system messages + the most RECENT turns that
    fit + tool-call/result pairing, which _sanitize_tool_messages then repairs) and
    insert a truncation marker. This is what lets a SMALL-context model still serve a
    long agentic conversation (recent context only) instead of 400ing — per-model
    memory management. Returns (messages, compacted_bool). No-op when it already fits
    or the budget is unknown/zero."""
    if not isinstance(messages, list) or not messages or not budget or budget <= 0:
        return messages, False
    target = int(budget * 0.85)   # leave ~15% headroom for the model's own reply
    if _est_tokens(messages, tools) <= target:
        return messages, False
    lead_sys, rest = [], []
    for m in messages:
        if not rest and isinstance(m, dict) and m.get("role") == "system":
            lead_sys.append(m)
        else:
            rest.append(m)
    base = _est_tokens(lead_sys, tools)
    kept, running = [], base
    for m in reversed(rest):                       # keep newest-first until full
        c = _est_tokens([m])
        if kept and running + c > target:
            break
        kept.append(m)
        running += c
    kept.reverse()
    if len(kept) >= len(rest):
        return messages, False                     # nothing actually dropped
    notice = {"role": "system",
              "content": "[Note: earlier conversation was truncated to fit this model's "
                         "context window. Ask the user to re-share anything you need.]"}
    return lead_sys + [notice] + kept, True


def _upstream_chat(pid, payload, stream):
    """POST {base_url}/chat/completions for provider pid, rotating across the
    provider's api_keys pool. Tries a round-robin start key; on 401/403/429 it
    advances to the next key for the SAME provider. Returns the first non-
    rotatable response (or the last response/exception once keys are exhausted,
    so the caller's provider-level fallback still kicks in). May raise
    requests.RequestException or RuntimeError. Never logs a key."""
    if isinstance(payload, dict) and isinstance(payload.get("messages"), list):
        # (1) AUTO-COMPACT the history to THIS model's context window (per-model
        # memory management — a small-context model gets recent turns only), then
        # (2) repair tool-pairing so a strict upstream can't 400 the agentic turn
        # ('tool_call_ids did not have response messages') — compaction may itself
        # orphan a tool msg / dangle a tool_call, so sanitize runs AFTER compaction.
        msgs = payload["messages"]
        compacted, did = _compact_to_budget(msgs, payload.get("tools"),
                                            _model_ctx_budget(pid, payload.get("model")))
        fixed = _sanitize_tool_messages(compacted)
        if did or fixed is not msgs:
            payload = dict(payload)
            payload["messages"] = fixed
    pcfg = config.get_provider_config(pid)
    # _resolve_base_url, not base_url_for: it also fills Cloudflare's
    # {account_id} from the token so the user only pastes a key.
    base = _resolve_base_url(pid, pcfg)
    if not base:
        raise RuntimeError("no base_url for provider " + pid)
    if "{account_id}" in base:
        raise RuntimeError(
            "could not resolve the Cloudflare account id from this token — paste your "
            "account-scoped base URL into 'Advanced: custom base URL' on the card")
    keys = pcfg.get("api_keys") or []
    if not keys:
        if _needs_key(pid):
            raise RuntimeError("no api key for provider " + pid)
        # No-key provider (e.g. Pollinations' anonymous tier): run exactly one
        # "key-less" pass. None is the sentinel -> no Authorization header below.
        keys = [None]
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
                headers=({"Content-Type": "application/json"} if key is None else
                         {"Authorization": "Bearer " + key,
                          "Content-Type": "application/json"}),
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
        quota.observe_headers(pid, resp.headers)  # ADAPT to the provider's real quota
        if resp.status_code == 400:               # learn a small context window from the error
            _learn_context_limit(pid, payload.get("model"), resp)
            _maybe_mark_missing_model(pid, payload.get("model"), resp)  # gone/renamed id -> sideline
        if 200 <= resp.status_code < 300:
            quota.note_success(pid)  # provider answered -> clear its 429-backoff streak
            quota.note_model_success(pid, payload.get("model"))  # and THIS model's streak
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
            # ALSO park just this model: it survives provider note_success(), so when
            # a sibling model revives the provider, the id that actually 429'd stays
            # sidelined instead of being re-picked and 429'ing again.
            quota.mark_model_throttled(pid, payload.get("model"), secs or 60)
        # 403 (no access to this model with this key) / 404 (model gone) are about
        # the MODEL, not the key or the quota: sideline just that id so routing
        # stops picking it. Only on the last key — an earlier key's 403 may just
        # mean THAT key lacks access, and rotation below still gets a chance.
        if resp.status_code in _DEAD_STATUSES and is_last:
            _mark_model_dead(pid, payload.get("model"), resp.status_code)
        # Track auth/credit failures per provider: once enough distinct models fail
        # this way the KEY is bad (no access / no credits) -> sideline the whole
        # provider so routing stops trying its other 20+ models every request.
        if resp.status_code in _AUTH_FAIL_STATUSES and is_last:
            _mark_provider_authfail(pid, payload.get("model"), resp.status_code)
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


def _capacity_eta(cap=60):
    """Seconds until the SOONEST enabled+keyed provider becomes usable again — the min
    of every provider's throttle/quota-reset countdown — capped at `cap`. Used as a
    Retry-After hint on a chain-EXHAUSTED response so the CLIENT waits out a short
    throttle and AUTO-CONTINUES once a model frees (a per-minute cap like nararouter's
    10/min resets in <=60s), instead of surfacing the failure. `cap` bounds it so the
    client never sleeps for a far-off daily reset — it just re-attempts every `cap`s
    within its own retry budget. Returns `cap` when nothing's close/known."""
    best = None
    try:
        for pid in _enabled_keyed():
            try:
                s = quota.status(pid)
            except Exception:
                continue
            if not s.get("exhausted"):
                return 1                       # something is usable right now
            r = s.get("resets_in")
            if isinstance(r, int) and r > 0:
                best = r if best is None else min(best, r)
    except Exception:
        return cap
    return max(1, min(int(best), cap)) if best is not None else cap


def _with_retry_after(resp_tuple, seconds):
    """Attach a Retry-After header to an (response, status) error tuple so the client
    waits `seconds` then auto-retries the turn. Best-effort; returns input on error."""
    try:
        resp, status = resp_tuple
        resp.headers["Retry-After"] = str(max(1, int(seconds)))
        return resp, status
    except Exception:
        return resp_tuple


def _retryable_relay_status(status):
    """Chain-EXHAUSTED relay only: turn a non-retryable hard 4xx (400/401/403/404/422
    — the last provider's incidental error) into a client-retryable 503, so a CLI SDK
    (codex/Claude Code retry on 408/409/429/>=500) RE-ATTEMPTS the whole agentic turn
    instead of hard-stopping. By the time we relay, a throttle window may have reset or
    a sidelined model revived, so the retry often succeeds. 409/429/5xx pass through.
    The original status + body are preserved in the relayed payload for diagnostics."""
    try:
        return status if (status in (408, 409, 429) or status >= 500) else 503
    except Exception:
        return 503


# --------------------------------------------------------------------------- #
# SOFT 400s — errors that _retryable() correctly treats as "hard" (never
# auto-retried) but that are actually just "this exact model/provider can't
# serve THIS request", not "everything is broken". Two observed in the wild:
#   - a small-context model rejecting a request that's too big for its window
#     ("context_length_exceeded" / "reduce the length of the messages")
#   - Gemini's 400 "missing thought_signature in functionCall parts" on
#     multi-turn tool use — a protocol quirk of GEMINI'S OWN tool-calling
#     continuity that a stateless proxy cannot repair by editing the payload
#     (the signature must come from a prior Gemini turn the hub never saw).
#     The fix is routing around it, not patching the payload.
# Both must fall through to the next chain hop SILENTLY instead of being
# replayed to the CLI as `last_hard` once the chain is exhausted — surfacing
# either one just breaks the CLI's turn for a cause it can't act on, when a
# different free model would likely have answered fine.
# --------------------------------------------------------------------------- #
_SOFT_400_CONTEXT_RE = re.compile(
    r"context_length_exceeded|reduce the length of the (?:messages|prompt)|"
    r"maximum context length|prompt is too long|"
    # smaller-context providers phrase it differently — all mean "route to a bigger
    # model": 'Max_len exceeded: Input is 16685 tokens but this model only supports 16384'
    r"max_?len exceeded|input is \d+ tokens|only supports \d+|"
    r"too many (?:input )?tokens|exceeds? the (?:model'?s )?maximum", re.I)
_SOFT_400_TOOL_RE = re.compile(r"thought_signature", re.I)
# Some providers return a VAGUE 400 ("we could not process your request / please
# check your input / invalid_request_error") for a request THIS model can't serve
# (usually an oversized context or an unsupported field) WITHOUT the tell-tale
# 'context_length' text. Route around it to the next (often larger-context) model
# instead of hard-failing the turn — codex's 'continue' on a big conversation hit
# exactly this and errored every time instead of trying another free model.
_SOFT_400_GENERIC_RE = re.compile(
    r"could not process your request|please check your input|unable to process", re.I)


def _classify_soft_400(resp):
    """True for a response ALREADY KNOWN to be HTTP 400 that matches a known
    SHOULD-NEVER-REACH-THE-CLIENT signature — don't treat it as a hard/
    relayable error, just move on to the next hop. False for a genuine hard
    error that should still be relayed if the whole chain is exhausted.

    Deliberately does NOT try to parse a "required token count" out of the
    error body to pre-emptively skip smaller-context hops: an adversarial
    review found that a blind digit-scan over the whole JSON body can pick up
    an unrelated large number (a request/trace id) and inflate the learned
    size into the billions, which then fails EVERY remaining hop's capacity
    check and collapses the fallback chain to nothing — worse than the
    original bug. The safe fix is simpler: just don't surface these two
    signatures raw, and let the existing hop loop try the next candidate."""
    try:
        text = json.dumps(resp.json())
    except ValueError:
        text = resp.text or ""
    return bool(_SOFT_400_TOOL_RE.search(text) or _SOFT_400_CONTEXT_RE.search(text)
                or _SOFT_400_GENERIC_RE.search(text))


# Learned per-(pid, model) max INPUT tokens, populated when a provider 400s with a
# context-size error that reveals its real limit ('... only supports 16384'). Lets
# routing STOP sending a growing agentic context to a small-context model instead of
# 400ing + falling through on every single turn.
_MODEL_MAX_INPUT = {}
_model_max_input_lock = threading.Lock()
# Priority-ordered: match the phrasing that names the LIMIT, never the request size.
# 'Max_len exceeded: Input is 16685 tokens but this model only supports 16384' must
# learn 16384 (the cap), NOT 16685 (the input) — so 'only supports N' wins first and
# a generic 'maximum ... N tokens' (which would grab 'Max_len ... 16685') is last.
_CTX_LIMIT_PATS = (
    re.compile(r"only supports (\d{3,7})", re.I),
    re.compile(r"supports (\d{3,7})\s*tokens", re.I),
    re.compile(r"context (?:window|length)[^0-9]{0,20}?(\d{4,7})", re.I),
    re.compile(r"maximum(?: context)?(?: length| window)?[^0-9]{0,20}?(\d{4,7})", re.I),
)


def _learn_context_limit(pid, model, resp):
    """Remember a model's real max input when a 400 reveals it. Best-effort, no raise."""
    if not model:
        return
    try:
        text = resp.text or ""
    except Exception:
        return
    if not _SOFT_400_CONTEXT_RE.search(text):
        return
    limit = None
    for pat in _CTX_LIMIT_PATS:
        m = pat.search(text)
        if m:
            limit = int(m.group(1))
            break
    if not limit or limit < 1000:
        return
    with _model_max_input_lock:
        cur = _MODEL_MAX_INPUT.get((pid, model))
        _MODEL_MAX_INPUT[(pid, model)] = min(cur, limit) if cur else limit


def _context_ok(pid, model, est):
    """False once we've LEARNED this (pid, model) can't hold an est-token request
    (5% headroom for estimate error). True when unknown — never blocks on a guess."""
    if not est:
        return True
    lim = _MODEL_MAX_INPUT.get((pid, model))
    return lim is None or est <= lim * 0.95


_MISSING_MODEL_RE = re.compile(
    r"model_not_found|model not found|no such model|does not exist|"
    r"unknown model|invalid model|model .* not (?:found|available)", re.I)


def _maybe_mark_missing_model(pid, model, resp):
    """A 400 that says the MODEL doesn't exist (some providers 400 instead of 404 for
    a gone/renamed id) -> sideline it like a 404 so routing stops picking it. Only on
    an unambiguous 'model missing' signature; a generic bad-request 400 is untouched."""
    if not model:
        return
    try:
        text = resp.text or ""
    except Exception:
        return
    if _MISSING_MODEL_RE.search(text):
        _mark_model_dead(pid, model, 404)


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

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _hostname(value, origin=False):
    try:
        parsed = urlsplit(value if origin else "//" + value)
        return (parsed.hostname or "").lower()
    except ValueError:
        return ""


@app.before_request
def _local_control_guard():
    """Block DNS rebinding and cross-site writes to the localhost control API."""
    g.csp_nonce = base64.b64encode(os.urandom(18)).decode("ascii")
    if _hostname(request.host) not in _LOOPBACK_HOSTS:
        return jsonify({"error": "this service accepts loopback Host headers only"}), 403
    origin = request.headers.get("Origin")
    if origin and _hostname(origin, origin=True) not in _LOOPBACK_HOSTS:
        return jsonify({"error": "cross-origin requests are not allowed"}), 403
    if request.path.startswith("/api/"):
        if (request.method in ("POST", "PUT", "PATCH", "DELETE") and
                request.headers.get("X-Free-LLM-Hub") != "dashboard"):
            # A custom header forces a browser CORS preflight. This app emits no
            # CORS permission, so an arbitrary website cannot reconfigure/stop the
            # user's localhost hub with a "simple" text/plain request.
            return jsonify({"error": "missing local control header"}), 403
        # The loopback port itself is not user-isolated: on a shared machine a
        # DIFFERENT local OS account can also connect to 127.0.0.1:PORT. This
        # per-install token (0600 config file, printed once at startup, never
        # rendered into the HTML) is what actually gates control of the hub —
        # Host/Origin only stop a browser-borne cross-site request.
        token = config.get_control_token()
        supplied = request.headers.get("X-Free-LLM-Hub-Token") or request.args.get("token")
        if token and not (supplied and hmac.compare_digest(str(supplied), token)):
            return jsonify({"error": "missing or invalid control token",
                            "code": "token_required"}), 401
    return None


@app.after_request
def _security_headers(response):
    nonce = getattr(g, "csp_nonce", "")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'none'; script-src 'nonce-%s'; "
        "style-src 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'; object-src 'none'" % nonce)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    if request.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response

_runtime_condition = threading.Condition()
_runtime_active = [0]
_runtime_server = [None]
_runtime_shutdown_thread = [None]


def _runtime_error():
    message = "The hub is draining and is not accepting new inference requests."
    if request.path.startswith("/v1/messages"):
        return _anthropic_error("overloaded_error", message, 503)
    return _openai_error(message, 503, "server_error")


@app.before_request
def _runtime_before():
    if not request.path.startswith("/v1"):
        return None
    state = config.get_runtime_state()
    if state.get("desired") == "stopped" or state.get("phase") in ("draining", "stopped"):
        return _runtime_error()
    with _runtime_condition:
        _runtime_active[0] += 1
    g.runtime_counted = True
    return None


def _runtime_request_done():
    with _runtime_condition:
        if _runtime_active[0] > 0:
            _runtime_active[0] -= 1
        _runtime_condition.notify_all()


@app.after_request
def _runtime_after(response):
    if not getattr(g, "runtime_counted", False):
        return response
    g.runtime_counted = False
    if response.is_streamed:
        response.call_on_close(_runtime_request_done)
    else:
        _runtime_request_done()
    return response

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
# A streaming/in-progress request still unfinished after this many seconds is
# treated as dead (client hung, or an upstream that never closed the stream) and
# shown as 'stalled' — instead of "streaming" forever with a timer that climbs
# without bound. CLI-agnostic: protects the feed from ANY client that abandons a
# stream, not just codex.
_ACTIVITY_STALL_SECS = 600
_activity = collections.deque(maxlen=_ACTIVITY_MAX)
_activity_lock = threading.Lock()
_activity_seq = [0]
_INFERENCE_PATHS = {
    "/v1/chat/completions": "openai",
    "/v1/responses": "responses",
    "/v1/messages": "anthropic",
    "/v1/images/generations": "images",
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


# Terminal-success / content / error markers scanned in the streamed SSE body to
# tell the activity feed whether an ANSWER was actually delivered. Bytes-level,
# protocol-agnostic (marker strings don't collide across openai/responses/
# anthropic). Terminal: [DONE] (chat), response.completed (responses),
# message_stop (messages). Content: a real text/tool delta in any dialect.
_STREAM_TERMINAL_RE = re.compile(rb"\[DONE\]|response\.completed|message_stop")
_STREAM_CONTENT_RE = re.compile(
    rb'output_text\.delta|function_call_arguments\.delta|content_block_delta'
    rb'|"tool_calls"|"content"\s*:\s*"(?:\\|[^"])')
_STREAM_ERROR_RE = re.compile(rb'event:\s*error|"error"\s*:')


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
        code = response.status_code
        # Finalize when the streamed BODY is exhausted (the generator's finally
        # runs on the terminal next()), NOT only when the connection closes.
        # Codex keeps the HTTP connection alive across its interactive session,
        # so response.call_on_close() alone would not fire until the socket is
        # torn down (or the 600s stall-guard trips) — leaving the row "streaming"
        # with a climbing timer long after response.completed already shipped.
        _body = response.response

        def _finalizing_body(src=_body, a=act, http=code):
            saw_content = saw_terminal = saw_error = False
            try:
                for chunk in src:
                    b = chunk if isinstance(chunk, (bytes, bytearray)) \
                        else str(chunk).encode("utf-8", "replace")
                    if not saw_content and _STREAM_CONTENT_RE.search(b):
                        saw_content = True
                    if not saw_terminal and _STREAM_TERMINAL_RE.search(b):
                        saw_terminal = True
                    if not saw_error and _STREAM_ERROR_RE.search(b):
                        saw_error = True
                    yield chunk
            finally:
                # 'ok'   -> a real answer (text or tool call) was delivered
                # 'empty'-> stream finished cleanly but produced nothing
                # 'error'-> an error event, or the stream cut off before any output
                if saw_content:
                    status = "ok"
                elif saw_error:
                    status = "error"
                elif saw_terminal:
                    status = "empty"
                else:
                    status = "error"
                _activity_done(a, status, http)

        response.response = _finalizing_body()
        # Backstop: if the client disconnects before the body is fully consumed,
        # connection-close still finalizes (no-op if already done).
        response.call_on_close(lambda: _activity_done(act, "ok", code))
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
        # Self-heal abandoned streams: anything still unfinished past the stall
        # window is finalized as 'stalled' (fixed end time) so it stops showing
        # "streaming" and its timer stops climbing.
        for a in _activity:
            if a.get("finished") is None and (now - a["started"]) > _ACTIVITY_STALL_SECS:
                a["status"] = "stalled"
                a["finished"] = now
        rows = list(_activity)
    out = []
    for a in rows:
        end = a["finished"] if a["finished"] else now
        out.append({**a,
                    "duration_ms": int((end - a["started"]) * 1000),
                    "started_ms": int(a["started"] * 1000)})
    return jsonify({"activity": out})


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    try:
        return render_template("index.html", csp_nonce=getattr(g, "csp_nonce", ""),
                                control_token=config.ensure_control_token())
    except TemplateNotFound:
        return (
            "<h1>Calvoun Free LLM Hub</h1>"
            "<p>Gateway is running, but <code>templates/index.html</code> is "
            "missing. The API surface is live: <code>/api/status</code>, "
            "<code>/api/providers</code>, <code>/v1/models</code>, "
            "<code>/v1/chat/completions</code>, <code>/v1/messages</code>.</p>"
        )


@app.route("/favicon.ico")
def favicon():
    """Avoid a noisy 404 when no branded favicon asset is installed."""
    return Response(status=204)


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
        "no_key": bool(p.get("no_key")),   # open gateway: usable with NO api key
        "free_models": provider_free_models(pid, live=live_models),
    }


@app.route("/api/providers", methods=["GET"])
def api_providers():
    # include_custom stays the default (False): the generic "Custom
    # (OpenAI-compatible)" card was briefly surfaced here so a not-yet-
    # registered provider (AIAND) could be configured through it, but AIAND
    # now has its own proper registry row with a confirmed base_url -- the
    # generic card is redundant again and the user asked for it hidden.
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
        try:
            kwargs["base_url"] = _validate_custom_base_url(val) if val else ""
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
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


@app.route("/api/tracking", methods=["GET"])
def api_tracking():
    """LIVE tracking of every EXISTING (provider, model) the hub knows: benchmark
    score, tool-capability, speed, and the full runtime state it self-tracks —
    dead (402/403/404, with re-probe countdown), per-model throttle, provider
    quota (used/limit/remaining/resets), and any LEARNED context limit. Read-only:
    reflects exactly what routing currently sees; makes NO upstream calls."""
    now = time.time()
    with _dead_lock:
        dead = dict(_dead_models)
    with _model_max_input_lock:
        learned = dict(_MODEL_MAX_INPUT)
    prov_status, out = {}, []
    for pid in _enabled_keyed():
        if pid not in prov_status:
            try:
                prov_status[pid] = quota.status(pid)
            except Exception:
                prov_status[pid] = {}
        qs = prov_status[pid]
        try:
            models = _auto_models(pid)
        except Exception:
            models = []
        for m in models:
            key = (pid, str(m))
            dexp = dead.get(key)
            is_dead = bool(dexp and dexp > now)
            try:
                thr = quota.is_model_throttled(pid, m)
            except Exception:
                thr = False
            try:
                allowed = prov.is_model_allowed(m)
            except Exception:
                allowed = True
            try:
                score = round(_benchmark_score(pid, m), 1)
            except Exception:
                score = 0.0
            state = ("provider-dead" if _is_provider_dead(pid) else
                     "dead" if is_dead else
                     "blocked" if not allowed else
                     "provider-exhausted" if qs.get("exhausted") else
                     "throttled" if thr else "ok")
            out.append({
                "id": pid + "/" + m, "provider": pid, "model": m,
                "score": score, "tool_capable": _supports_tools(pid, m),
                "fast": _is_fast(pid, m), "state": state,
                "dead_expires_in": int(dexp - now) if is_dead else None,
                "throttled": thr, "learned_ctx": learned.get(key),
                "quota": {k: qs.get(k) for k in
                          ("used", "limit", "remaining", "exhausted", "resets_in", "window")},
            })
    out.sort(key=lambda r: (-r["score"], r["provider"], r["model"]))
    by_state = {}
    for r in out:
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
    return jsonify({"models": out, "total": len(out), "by_state": by_state,
                    "providers": sorted({r["provider"] for r in out}),
                    "usable": sum(1 for r in out if r["state"] == "ok")})


@app.route("/api/probe-all", methods=["POST"])
def api_probe_all():
    """ACTIVE health check: send ONE tiny real request to each enabled+keyed
    (provider, model) and record whether it answers — marking 402/403/404 ids dead so
    routing stops picking them. Uses a little free quota (opt-in, POST). Skips ids
    already dead/exhausted so it doesn't waste calls. Returns the per-model verdict."""
    results = []
    for pid in _available_providers():
        try:
            models = _auto_models(pid)
        except Exception:
            models = []
        for m in models:
            if not prov.is_model_allowed(m) or _is_model_dead(pid, m):
                continue
            ok, detail = _probe_pair(pid, m)
            results.append({"id": pid + "/" + m, "provider": pid, "model": m,
                            "ok": bool(ok), "detail": detail})
    results.sort(key=lambda r: (not r["ok"], r["provider"]))
    return jsonify({"results": results, "total": len(results),
                    "working": sum(1 for r in results if r["ok"]),
                    "failed": sum(1 for r in results if not r["ok"])})


def _ranked_free_pairs(limit=6):
    """[(score, pid, model)] best-first across available providers, skipping
    safety-blocked and known-dead ids."""
    cands = []
    for pid in _available_providers():
        for m in _auto_models(pid):
            if not prov.is_model_allowed(m) or _is_model_dead(pid, m):
                continue
            cands.append((_benchmark_score(pid, m), pid, m))
    cands.sort(key=lambda t: t[0], reverse=True)
    return cands[:limit]


def _probe_pair(pid, model, timeout_s=25):
    """Send ONE tiny real request to (pid, model). Returns (ok, detail).
    Marks the model dead on a 403/404 so the rest of the hub routes around it."""
    payload = {"model": model, "max_tokens": 4, "stream": False,
               "messages": [{"role": "user", "content": "hi"}]}
    try:
        r = _upstream_chat(pid, payload, False)
    except Exception as exc:
        return False, "%s: %s" % (exc.__class__.__name__, _sanitize(str(exc))[:60])
    try:
        if r.status_code == 200:
            return True, "answered"
        # _upstream_chat already marks 403/404 dead on the last key
        try:
            b = r.json()
            e = b.get("error")
            msg = e.get("message") if isinstance(e, dict) else str(e)
        except Exception:
            msg = (r.text or "")[:60]
        return False, "HTTP %d: %s" % (r.status_code, _sanitize(str(msg))[:60])
    finally:
        try:
            r.close()
        except Exception:
            pass


@app.route("/api/default/auto", methods=["POST"])
def api_default_auto():
    """Auto-pick the best ORCHESTRATOR from models that ACTUALLY WORK, then save it.

    This PROBES before committing, on purpose. Ranking alone is not enough: the
    picker's honest favourite here is github-models/llama-4-maverick, which 403s
    on EVERY call (the user's token lacks the models:read scope) — and it really
    did get saved as the default that way. The dead-model tracker only learns
    after a live failure and is in-memory, so a fresh process would re-pick the
    same broken id. So: walk the ranked list, send ONE 4-token probe per
    candidate, save the first that answers (each failure marks itself dead via
    _upstream_chat, so the whole hub routes around it afterwards).

    Costs at most a few tiny requests, only when the user explicitly asks."""
    ranked = _ranked_free_pairs()
    if not ranked:
        return jsonify({
            "ok": False,
            "reason": ("No working free model available. Add a provider key, or "
                       "everything keyed is exhausted/sidelined (see the quota "
                       "panel and /api/dead-models)."),
        }), 409
    tried = []
    for _score, pid, model in ranked:
        ok, detail = _probe_pair(pid, model)
        tried.append({"model": "%s/%s" % (pid, model), "ok": ok, "detail": detail})
        if ok:
            config.set_default(pid, model)
            return jsonify({
                "ok": True,
                "provider": pid, "model": model, "label": "%s/%s" % (pid, model),
                "score": round(_benchmark_score(pid, model), 1),
                "fast": _is_fast(pid, model),
                "tried": tried,
                "note": ("Verified live: this is the highest-benchmark model that "
                         "actually answered. Rejected candidates were marked dead "
                         "so routing avoids them too."),
            })
    return jsonify({
        "ok": False,
        "tried": tried,
        "reason": ("Every top candidate failed a live probe — none of them answer "
                   "right now. See 'tried' for why (e.g. 403 = the provider's key "
                   "lacks permission)."),
    }), 409


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


@app.route("/api/usage", methods=["GET"])
def api_usage():
    date_str = request.args.get("date")
    if date_str and not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400
    payload = usage_history.get_day(date_str)
    payload["available_days"] = usage_history.recent_days()
    return jsonify(payload)


def _media_payload():
    state = config.get_media_state()
    models = []
    for p in prov.list_providers():
        pid = p["id"]
        pcfg = config.get_provider_config(pid)
        defaults = {m.lower() for m in provider_free_models(pid, live=False)}
        for model in _vision_model_ids(pid):
            models.append({
                "id": pid + "/" + model,
                "provider": pid,
                "model": model,
                "provider_name": p.get("name") or pid,
                "configured": bool(pcfg.get("enabled") and
                                   (pcfg.get("api_key") or not _needs_key(pid))),
                "listed": model.lower() in defaults,
                "dead": _is_model_dead(pid, model),
            })
    available_order = [pid + "/" + model for pid, model in _vision_candidates()]
    return {"state": state, "models": models, "effective_priority": available_order,
            "limits": {"max_images": MAX_IMAGE_COUNT,
                       "max_image_bytes": MAX_IMAGE_BYTES,
                       "supported_types": sorted(_IMAGE_MIMES)}}


@app.route("/api/media", methods=["GET", "POST"])
@app.route("/api/multimodal", methods=["GET", "POST"])
def api_media():
    if request.method == "GET":
        return jsonify(_media_payload())
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "invalid JSON body"}), 400
    if "revision" not in body:
        return jsonify({"error": "revision is required"}), 400
    mode = body.get("priority_mode")
    if mode not in ("auto", "manual"):
        return jsonify({"error": "priority_mode must be 'auto' or 'manual'"}), 400
    manual = body.get("manual_priority", [])
    if not isinstance(manual, list) or any(not isinstance(v, str) for v in manual):
        return jsonify({"error": "manual_priority must be an array of model ids"}), 400
    valid = {p["id"] + "/" + model for p in prov.list_providers()
             for model in _vision_model_ids(p["id"])}
    unknown = [value for value in manual if value not in valid]
    if unknown:
        return jsonify({"error": "unknown vision model(s): " + ", ".join(unknown)}), 400
    deduped = []
    for value in manual:
        if value not in deduped:
            deduped.append(value)

    def _update(state):
        state["priority_mode"] = mode
        state["manual_priority"] = deduped if mode == "manual" else []
        return state

    try:
        config.update_media_state(body["revision"], _update)
    except config.RevisionConflict as exc:
        return jsonify({"error": "media state changed; reload and retry",
                        "current_revision": exc.current_revision,
                        "state": config.get_media_state()}), 409
    return jsonify(_media_payload())


def _set_runtime_phase(phase, last_error=None):
    for _attempt in range(3):
        state = config.get_runtime_state()

        def _update(value):
            value["phase"] = phase
            value["last_error"] = last_error
            return value

        try:
            return config.update_runtime_state(state["revision"], _update)
        except config.RevisionConflict:
            continue
    return config.get_runtime_state()


def _graceful_shutdown_worker(timeout=30):
    # Give the HTTP handler enough time to flush its accepted response.
    time.sleep(0.2)
    deadline = time.time() + timeout
    with _runtime_condition:
        while _runtime_active[0] > 0 and time.time() < deadline:
            _runtime_condition.wait(timeout=min(0.5, max(0, deadline - time.time())))
    _set_runtime_phase("stopped")
    server = _runtime_server[0]
    if server is not None:
        try:
            server.shutdown()
        except Exception as exc:
            _set_runtime_phase("error", _sanitize(str(exc)))


@app.route("/api/runtime", methods=["GET"])
def api_runtime():
    with _runtime_condition:
        active = _runtime_active[0]
    return jsonify({"state": config.get_runtime_state(), "active_requests": active,
                    "intentional_stop": config.is_intentionally_stopped()})


@app.route("/api/runtime/stop", methods=["POST"])
@app.route("/api/lifecycle/stop", methods=["POST"])
def api_runtime_stop():
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "invalid JSON body"}), 400
    if "revision" not in body:
        return jsonify({"error": "revision is required"}), 400

    def _drain(state):
        state.update({"desired": "stopped", "phase": "draining",
                      "shutdown_requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "last_error": None})
        return state

    try:
        state = config.update_runtime_state(body["revision"], _drain)
    except config.RevisionConflict as exc:
        return jsonify({"error": "runtime state changed; reload and retry",
                        "current_revision": exc.current_revision,
                        "state": config.get_runtime_state()}), 409
    try:
        config.set_intentional_stop()
    except OSError as exc:
        for _attempt in range(3):
            current = config.get_runtime_state()
            try:
                config.update_runtime_state(current["revision"], lambda value: {
                    **value, "desired": "running", "phase": "error",
                    "last_error": _sanitize(str(exc))})
                break
            except config.RevisionConflict:
                continue
        return jsonify({"error": "could not create the intentional-stop marker",
                        "state": config.get_runtime_state()}), 500
    thread = _runtime_shutdown_thread[0]
    if thread is None or not thread.is_alive():
        thread = threading.Thread(target=_graceful_shutdown_worker,
                                  name="freehub-shutdown", daemon=True)
        _runtime_shutdown_thread[0] = thread
        thread.start()
    return jsonify({"ok": True, "state": state, "active_requests": _runtime_active[0],
                    "message": "Shutdown accepted; draining active inference requests."}), 202


# ---------------------------------------------------------------------------
# Local subscription providers API (opt-in, default OFF)
# ---------------------------------------------------------------------------
# Localhost-open like the rest of /api/*. Read the state, flip the master switch,
# flip one provider. Flags persist through config.get_flag/set_flag.

_SUB_WARNING = (
    "These providers spend your PAID Claude Code / ChatGPT subscriptions — they are "
    "NOT free, and this hub cannot see how much of your plan is left. Each request "
    "starts a local CLI process, so they are noticeably slower than the free HTTP "
    "models, and they cannot stream. The hub only uses them as a LAST RESORT (after "
    "every free model has failed) or when you pick one explicitly. They run the CLIs "
    "as your local user with your own logged-in session: keep the hub bound to "
    "127.0.0.1 and never expose it to a network."
)


def _sub_provider_rows():
    """One row per sub provider for the dashboard. Inspection only — never runs a
    CLI or writes to disk, so opening the page costs nothing."""
    rows = []
    for pid, cfg in _SUB_PROVIDERS.items():
        enabled, installed, authed, detail = _sub_state(pid)
        cli_id = cfg["cli_id"]
        isolated = _sub_isolated_on(pid)
        iso_bin = _isolated_bin_path(cli_id, cfg["bin"])
        login_cmd, login_note = _isolated_login_command(pid)
        rows.append({
            "id": pid,
            "name": cfg["name"],
            "model": pid + "/" + cfg["model"],
            "bin": cfg["bin"],
            "installed": installed,
            "authenticated": authed,
            "enabled": enabled,
            "usable": bool(_sub_master_on() and enabled and authed),
            "detail": detail,
            # Isolated-install profile (opt-in, default off — see _sub_isolated_on).
            "isolated": isolated,
            "isolated_supported": True,   # both CODEX_HOME and CLAUDE_CONFIG_DIR are
                                          # CONFIRMED official env vars (see comments
                                          # above _ISOLATED_ENV_VAR) -- no guessed gap.
            "isolated_installed": bool(iso_bin),
            "isolated_install_dir": _short(_isolated_install_dir(cli_id)),
            "isolated_config_dir": _short(_isolated_config_dir(cli_id)),
            "isolated_env_var": _ISOLATED_ENV_VAR.get(cli_id),
            "isolated_login_command": login_cmd,
            "isolated_login_note": login_note,
        })
    return rows


def _sub_payload():
    return {"enabled": _sub_master_on(), "providers": _sub_provider_rows(),
            "warning": _SUB_WARNING}


@app.route("/api/subscriptions", methods=["GET"])
def api_subscriptions():
    """{enabled, providers:[{id,name,installed,authenticated,enabled,detail,...}],
    warning}."""
    return jsonify(_sub_payload())


@app.route("/api/subscriptions", methods=["POST"])
def api_subscriptions_update():
    """Toggle the master switch, or ONE provider's enabled/isolated flags, then
    return the same shape.

      {"enabled": bool}                             -> master switch
      {"provider": "sub-codex", "enabled": bool}     -> that provider's enabled flag
      {"provider": "sub-codex", "isolated": bool}    -> that provider's isolated-profile flag
      (the last two keys may be combined in one body; each is applied independently)

    When 'provider' is present, 'enabled'/'isolated' apply to THAT provider (the
    master switch is only touched by a body without 'provider') — so one call can
    never silently mean both."""
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Invalid JSON body."}), 400
    pid = body.get("provider")
    if pid is not None:
        if pid not in _SUB_PROVIDERS:
            return jsonify({"error": "Unknown subscription provider '%s'."
                                     % _sanitize(str(pid), 40)}), 400
        touched = False
        if isinstance(body.get("enabled"), bool):
            config.set_flag(_SUB_PROVIDERS[pid]["flag"], bool(body["enabled"]))
            touched = True
        if isinstance(body.get("isolated"), bool):
            config.set_flag(_SUB_PROVIDERS[pid]["isolated_flag"], bool(body["isolated"]))
            touched = True
        if not touched:
            return jsonify({"error": "Pass 'enabled' and/or 'isolated' (bool) with 'provider'."}), 400
    elif isinstance(body.get("enabled"), bool):
        config.set_flag(_SUB_MASTER_FLAG, bool(body["enabled"]))
    else:
        return jsonify({"error": "Pass {enabled: bool} and/or "
                                 "{provider: 'sub-codex', enabled: bool, isolated: bool}."}), 400
    return jsonify(_sub_payload())


@app.route("/api/subscriptions/<pid>/install-isolated", methods=["POST"])
def api_subscriptions_install_isolated(pid):
    """Install an ISOLATED copy of a sub provider's CLI via `npm install -g
    <pkg> --prefix <isolated dir>`, so it never touches the shared ~/.claude or
    ~/.codex the user's own terminal session uses.

    Does NOT require the master/per-provider enable flags — installing spends
    no money and never touches the shared CLI; those flags still gate actually
    USING the result as a sub-* hop (_sub_state / _check_provider_ready,
    unchanged). This IS a real subprocess call the user authorized by clicking
    the dashboard button — every failure mode (npm missing, network,
    permissions, timeout, non-zero exit) is surfaced in the response, never
    swallowed."""
    if pid not in _SUB_PROVIDERS:
        return jsonify({"error": "Unknown subscription provider '%s'."
                                 % _sanitize(str(pid), 40)}), 400
    cfg = _SUB_PROVIDERS[pid]
    cli_id = cfg["cli_id"]
    pkg = _ISOLATED_NPM_PACKAGE.get(cli_id)
    if not pkg:
        return jsonify({"ok": False, "error": "No known npm package for '%s'." % cli_id}), 400
    npm = shutil.which("npm")
    if not npm:
        return jsonify({"ok": False, "error":
                        "npm is not on PATH. Install Node.js first (nodejs.org), then retry."}), 400
    _ensure_isolated_dirs(cli_id)
    install_dir = _isolated_install_dir(cli_id)
    argv = _sub_launcher(npm) + ["install", "-g", pkg, "--prefix", install_dir]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=_ISOLATED_INSTALL_TIMEOUT,
                              cwd=tempfile.gettempdir())
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "npm install timed out after %ds."
                                              % _ISOLATED_INSTALL_TIMEOUT}), 504
    except (OSError, ValueError) as exc:
        return jsonify({"ok": False, "error": "npm failed to start: %s"
                                              % exc.__class__.__name__}), 502
    if proc.returncode != 0:
        err = _sanitize(((proc.stderr or "") + "\n" + (proc.stdout or "")).strip(), 2000)
        return jsonify({"ok": False, "error": "npm install exited %d: %s"
                                              % (proc.returncode, err or "no detail")}), 502
    bin_path = _isolated_bin_path(cli_id, cfg["bin"])
    if not bin_path:
        return jsonify({"ok": False, "error":
                        ("npm reported success but no '%s' binary was found under %s."
                         % (cfg["bin"], _short(install_dir)))}), 502
    return jsonify({"ok": True, "bin_path": _short(bin_path), "install_dir": _short(install_dir)})


# ---------------------------------------------------------------------------
# Agentic chat -- opt-in, full-tool-access coding-agent mode (project-scoped).
# ADDITIVE to the _SUB_PROVIDERS/_sub_run/_subscription_chat system above; that
# one-shot, no-tool-access orchestration fallback is completely untouched by
# this. See agentic_chat.py for the session registry + subprocess handling.
# ---------------------------------------------------------------------------

def _agent_gate():
    """None when agentic chat is enabled; otherwise a (response, status) pair
    the route should return immediately. NOT applied to /api/agent/settings
    (that route is how the flag gets turned on/off in the first place) nor to
    stop/end (a kill switch must still be able to kill/clean up a session even
    after the master flag is flipped off)."""
    if not agentic_chat.master_enabled():
        return jsonify({"error": "Agentic chat is turned off. Enable it via "
                                 "POST /api/agent/settings {\"enabled\": true}.",
                        "code": "agentic_chat_disabled"}), 403
    return None


@app.route("/api/agent/settings", methods=["GET"])
def api_agent_settings():
    return jsonify({"enabled": agentic_chat.master_enabled(), "clis": agentic_chat.cli_support(),
                    "default_cli": agentic_chat.default_cli()})


@app.route("/api/agent/settings", methods=["POST"])
def api_agent_settings_update():
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
        return jsonify({"error": "Pass {\"enabled\": bool}."}), 400
    agentic_chat.set_master_enabled(body["enabled"])
    return jsonify({"enabled": agentic_chat.master_enabled(), "clis": agentic_chat.cli_support(),
                    "default_cli": agentic_chat.default_cli()})


@app.route("/api/agent/test-verification", methods=["GET"])
def api_agent_test_verification():
    """Master, GLOBAL (not per-session) toggle for the test-verification
    system-prompt notice -- mirrors /api/agent/settings' shape exactly. Not
    gated by _agent_gate(): same reasoning as /api/agent/settings itself,
    this IS the route that configures the behavior in the first place."""
    return jsonify({"enabled": agentic_chat.test_verification_enabled()})


@app.route("/api/agent/test-verification", methods=["POST"])
def api_agent_test_verification_update():
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
        return jsonify({"error": "Pass {\"enabled\": bool}."}), 400
    agentic_chat.set_test_verification_enabled(body["enabled"])
    return jsonify({"enabled": agentic_chat.test_verification_enabled()})


@app.route("/api/auto/provider-mode", methods=["GET"])
def api_auto_provider_mode():
    return jsonify({"mode": _auto_provider_mode()})


@app.route("/api/auto/provider-mode", methods=["POST"])
def api_auto_provider_mode_update():
    body = request.get_json(force=True, silent=True)
    mode = body.get("mode") if isinstance(body, dict) else None
    if mode not in ("free", "paid", "mix"):
        return jsonify({"error": "mode must be 'free', 'paid', or 'mix'."}), 400
    config.set_setting("auto_provider_mode", mode)
    return jsonify({"mode": _auto_provider_mode()})


@app.route("/api/agent/vision-status", methods=["GET"])
def api_agent_vision_status():
    """Read-only capability probe: is at least one enabled+keyed provider
    carrying a verified vision model? See vision_status.py. Deliberately NOT
    gated by _agent_gate() (contrast with /api/agent/recent-projects, which
    IS gated): this is a general hub capability signal a settings/status
    panel should be able to show even before agentic chat itself is turned
    on, not an agentic-session-scoped resource -- same "informational, no
    live CLI subprocess touched" reasoning as the history routes below."""
    return jsonify(vision_status.status())


@app.route("/api/agent/sessions", methods=["POST"])
def api_agent_start_session():
    gate = _agent_gate()
    if gate:
        return gate
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Invalid JSON body."}), 400
    create_new = body.get("create_new", False)
    if not isinstance(create_new, bool):
        return jsonify({"error": "create_new must be a boolean."}), 400
    try:
        session_id = agentic_chat.start_session(body.get("cli"), body.get("project_dir"),
                                                 create_new=create_new)
    except agentic_chat.AgenticError as exc:
        # exc.code/.extra carry the DISTINCT "not installed, but installable"
        # shape (code="cli_not_installed", extra={"install_provider": "sub-..."})
        # so the frontend can offer a one-click Install button that calls the
        # EXISTING /api/subscriptions/<pid>/install-isolated route, instead of
        # just failing. Plain validation errors have no .code and pass through
        # as a generic {"error": ...} exactly as before.
        payload = {"error": _sanitize(str(exc))}
        if exc.code:
            payload["code"] = exc.code
        payload.update(exc.extra)
        return jsonify(payload), exc.status
    return jsonify(agentic_chat.get_session(session_id))


@app.route("/api/agent/sessions", methods=["GET"])
def api_agent_list_sessions():
    gate = _agent_gate()
    if gate:
        return gate
    return jsonify({"sessions": agentic_chat.list_sessions()})


@app.route("/api/agent/recent-projects", methods=["GET"])
def api_agent_recent_projects():
    """Recently-used project_dir values (this process lifetime) -- lets the
    workspace folder picker show a list instead of a blank text box."""
    gate = _agent_gate()
    if gate:
        return gate
    return jsonify({"recent_projects": agentic_chat.get_recent_projects()})


@app.route("/api/agent/sessions/<session_id>", methods=["GET"])
def api_agent_get_session(session_id):
    gate = _agent_gate()
    if gate:
        return gate
    sess = agentic_chat.get_session(session_id)
    if sess is None:
        return jsonify({"error": "No such agentic session."}), 404
    return jsonify(sess)


@app.route("/api/agent/sessions/<session_id>/message", methods=["POST"])
def api_agent_send_message(session_id):
    gate = _agent_gate()
    if gate:
        return gate
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("text"), str):
        return jsonify({"error": "Pass {\"text\": string}."}), 400
    # Persist the user's side BEFORE the (possibly long-running, up to
    # _TURN_TIMEOUT) subprocess call -- so a hub restart mid-turn never loses
    # the outgoing message. Only persist the agent's reply if a turn actually
    # produced one (status 200); a 4xx/409/499/5xx has no reply text to save.
    sess_info = agentic_chat.get_session(session_id)
    if sess_info:
        agentic_history.record_turn(session_id, sess_info["cli"], sess_info["project_dir"],
                                    "user", body["text"])
    status, text, detail = agentic_chat.send_message(session_id, body["text"])
    if sess_info and status == 200 and text:
        agentic_history.record_turn(session_id, sess_info["cli"], sess_info["project_dir"],
                                    "agent", text)
    return jsonify({"status": status, "text": text, "detail": detail}), status


@app.route("/api/agent/sessions/<session_id>/message/stream", methods=["POST"])
def api_agent_send_message_stream(session_id):
    """Live version of the message route: relays agentic_chat.send_message_stream's
    normalized progress events over SSE so the dashboard shows the agent working in
    real time. Records the user turn up front and the agent's final reply on done."""
    gate = _agent_gate()
    if gate:
        return gate
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("text"), str):
        return jsonify({"error": "Pass {\"text\": string}."}), 400
    text = body["text"]
    sess_info = agentic_chat.get_session(session_id)
    if sess_info:
        agentic_history.record_turn(session_id, sess_info["cli"], sess_info["project_dir"],
                                    "user", text)

    def gen():
        final_reply = None
        try:
            for ev in agentic_chat.send_message_stream(session_id, text):
                if ev.get("event") == "done":
                    final_reply = ev.get("text")
                yield "data: " + json.dumps(ev) + "\n\n"
        except Exception as exc:  # never leak a traceback into the stream
            yield "data: " + json.dumps({"event": "error", "status": 500,
                                         "detail": _sanitize(str(exc), 300)}) + "\n\n"
        finally:
            if sess_info and final_reply:
                try:
                    agentic_history.record_turn(session_id, sess_info["cli"],
                                                sess_info["project_dir"], "agent", final_reply)
                except Exception:
                    pass
            yield "event: end\ndata: {}\n\n"

    return Response(stream_with_context(gen()), mimetype="text/event-stream", headers=_SSE_HEADERS)


@app.route("/api/agent/sessions/<session_id>/stop", methods=["POST"])
def api_agent_stop_session(session_id):
    stopped = agentic_chat.stop_session(session_id)
    return jsonify({"stopped": stopped})


@app.route("/api/agent/sessions/<session_id>", methods=["DELETE"])
def api_agent_end_session(session_id):
    ended = agentic_chat.end_session(session_id)
    return jsonify({"ended": ended})


@app.route("/api/agent/new-project", methods=["POST"])
def api_agent_new_project():
    """One-click 'Create new project': auto-create a fresh uniquely-named folder
    under ~/calvoun-projects and return its path, so the dashboard can fill it in
    and the user just clicks Start session."""
    gate = _agent_gate()
    if gate:
        return gate
    try:
        path = agentic_chat.new_project_dir()
    except OSError as exc:
        return jsonify({"error": "Could not create a new project folder: %s" % exc.__class__.__name__}), 500
    return jsonify({"path": path})


# ---------------------------------------------------------------------------
# Agentic chat -- persisted conversation history + rewind checkpoints.
# None of these five routes call _agent_gate(): they never touch a live CLI
# subprocess, only the locally-persisted transcript (agentic_history.py), so
# gating them behind agentic_chat_enabled would only block the user from
# browsing/managing their OWN past conversations after turning the live
# feature off -- that isn't what the master flag is for (same reasoning the
# pre-existing stop/end routes above already use, and the same precedent as
# /api/images/history's routes, which carry no image-generation-flag gate
# either). They still go through the normal global request guard in
# _local_control_guard() (loopback host/origin + dashboard header + control
# token for any POST/PUT/PATCH/DELETE under /api/).
#
# Checkpoint scope reminder (see agentic_history.py docstring): a checkpoint
# is a TRANSCRIPT BOOKMARK (turn index + timestamp + optional label), never a
# filesystem snapshot/undo -- this hub has no sandboxing/versioning of the
# project folder's actual files.
# ---------------------------------------------------------------------------

@app.route("/api/agent/history", methods=["GET"])
def api_agent_history_list():
    limit = request.args.get("limit", "50")
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 50
    return jsonify({"conversations": agentic_history.list_conversations(limit=limit)})


@app.route("/api/agent/history/<session_id>", methods=["GET"])
def api_agent_history_get(session_id):
    conv = agentic_history.get_conversation(session_id)
    if conv is None:
        return jsonify({"error": "No such conversation."}), 404
    return jsonify(conv)


@app.route("/api/agent/history/<session_id>", methods=["DELETE"])
def api_agent_history_delete(session_id):
    deleted = agentic_history.delete_conversation(session_id)
    if not deleted:
        return jsonify({"error": "No such conversation."}), 404
    return jsonify({"deleted": True})


@app.route("/api/agent/history/<session_id>/checkpoints", methods=["POST"])
def api_agent_history_create_checkpoint(session_id):
    body = request.get_json(force=True, silent=True)
    label = body.get("label") if isinstance(body, dict) else None
    if label is not None and not isinstance(label, str):
        return jsonify({"error": "label must be a string."}), 400
    checkpoint = agentic_history.create_checkpoint(session_id, label=label)
    if checkpoint is None:
        return jsonify({"error": "No such conversation."}), 404
    return jsonify(checkpoint)


@app.route("/api/agent/history/<session_id>/checkpoints", methods=["GET"])
def api_agent_history_list_checkpoints(session_id):
    return jsonify({"checkpoints": agentic_history.list_checkpoints(session_id)})


# ---------------------------------------------------------------------------
# Settings export/import -- a portable backup/restore of config.py's ACTUAL
# persisted state (config.json: providers/keys, boolean flags, the default
# model, the /v1/* local bearer key, and media/images priority preferences).
#
# Deliberately OUT OF SCOPE (not guessed at, not silently included):
#   - hub_mode / runtime: process LIFECYCLE state -- current phase (running/
#     stopped/draining/changing/conflict/error), a CAS revision counter, a
#     "generation" id + per-client snapshot dirs that are real paths on THIS
#     machine's filesystem, shutdown_requested_at, last_error. Restoring an
#     old "phase: stopped/changing" snapshot onto a freshly-booted process,
#     or a generation id whose snapshot dir doesn't exist on the target
#     machine, would corrupt the target's own lifecycle rather than restore
#     anything a user actually wants preserved -- a local-only runtime
#     artifact, not a portable setting.
#   - control_token: the per-install control-plane secret (see its own
#     docstring in config.py). Every install should mint its own; shipping it
#     in a portable file would hand control-plane access to whoever later
#     reads that file.
#   - schema_version: stamped by config.py itself on every load, not a
#     user-set value.
#   - conversation history (agentic_history.py/usage_history.py/
#     image_history.py): each lives in its OWN JSON store OUTSIDE
#     config.py entirely -- a separate concern (large, potentially sensitive
#     transcript content, its own backup/restore story) left for a future
#     pass rather than folded in here without being asked.
# ---------------------------------------------------------------------------

# Every section this pair understands. "all" (the sections=... shortcut, and
# the default when the param is omitted) expands to exactly this tuple.
_SETTINGS_SECTIONS = ("api_keys", "flags", "default", "local_api_key", "media", "images")

# cfg top-level keys that are NEVER auto-detected as a "flag" (see
# _export_settings's "flags" branch below) -- each already has its own named
# section above, or is one of the excluded runtime/lifecycle keys documented
# above. Also used defensively on IMPORT so a crafted `flags` payload can
# never clobber a structural section by reusing its key name.
_SETTINGS_RESERVED_KEYS = {
    "schema_version", "providers", "default", "local_api_key",
    "hub_mode", "runtime", "media", "images", "control_token",
}


def _settings_flags(cfg):
    """Every top-level boolean flag in cfg (config.py's set_flag()/get_flag()
    store arbitrary top-level bool keys with no fixed registry of names, so
    this is a generic scan, not a hardcoded list -- future flags are picked
    up automatically). Verified against the actual current flag names in
    this codebase (agentic_chat_enabled, agentic_test_verification_enabled,
    use_local_subscriptions, sub_claude_enabled, sub_claude_isolated,
    sub_codex_enabled, sub_codex_isolated) -- none of config.py's structural
    fields (providers/default/local_api_key/hub_mode/runtime/media/images/
    schema_version/control_token) are ever booleans, so this can't
    accidentally swallow one of those."""
    return {k: v for k, v in cfg.items()
            if k not in _SETTINGS_RESERVED_KEYS and isinstance(v, bool)}


def _export_settings(sections):
    cfg = config.load_config()
    out = {}
    if "api_keys" in sections:
        out["api_keys"] = copy.deepcopy(cfg.get("providers") or {})
    if "flags" in sections:
        out["flags"] = _settings_flags(cfg)
    if "default" in sections:
        out["default"] = copy.deepcopy(cfg.get("default"))
    if "local_api_key" in sections:
        out["local_api_key"] = cfg.get("local_api_key")
    if "media" in sections:
        m = cfg.get("media") or {}
        out["media"] = {"priority_mode": m.get("priority_mode"),
                        "manual_priority": list(m.get("manual_priority") or [])}
    if "images" in sections:
        i = cfg.get("images") or {}
        out["images"] = {"priority_mode": i.get("priority_mode"),
                         "manual_priority": list(i.get("manual_priority") or [])}
    return out


def _parse_sections_param(raw):
    """'all' (or omitted/blank) -> every section. Otherwise a comma-separated
    subset of _SETTINGS_SECTIONS. Returns (sections_tuple, error_or_None)."""
    if raw is None or not str(raw).strip() or str(raw).strip().lower() == "all":
        return _SETTINGS_SECTIONS, None
    requested = [s.strip() for s in str(raw).split(",") if s.strip()]
    unknown = [s for s in requested if s not in _SETTINGS_SECTIONS]
    if unknown:
        return None, ("unknown section(s): %s -- valid: %s, or 'all'"
                      % (", ".join(unknown), ", ".join(_SETTINGS_SECTIONS)))
    return tuple(requested), None


@app.route("/api/settings/export", methods=["GET"])
def api_settings_export():
    raw = request.args.get("sections")
    if raw is None:
        body = request.get_json(force=True, silent=True)
        if isinstance(body, dict):
            raw = body.get("sections")
    sections, err = _parse_sections_param(raw)
    if err:
        return jsonify({"error": err}), 400
    payload = {
        "schema_version": config.SCHEMA_VERSION,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sections": list(sections),
    }
    payload.update(_export_settings(sections))
    return jsonify(payload)


def _validate_settings_import(body):
    """Structural validation ONLY -- no side effects, nothing written yet.
    Returns (normalized_sections_dict, error_or_None). Only keys present in
    `body` are validated/returned (auto-detect, per the task spec); unknown
    top-level keys are silently ignored (forward-compat with a newer export
    format) rather than rejected. Any structural violation in ANY present
    section rejects the WHOLE import before this function returns -- the
    caller must not apply a partial result."""
    if not isinstance(body, dict):
        return None, "request body must be a JSON object."
    present = [s for s in _SETTINGS_SECTIONS if s in body]
    if not present:
        return None, ("no recognized settings section found in the uploaded JSON -- "
                      "expected one or more of: %s" % ", ".join(_SETTINGS_SECTIONS))
    out = {}
    if "api_keys" in present:
        raw = body["api_keys"]
        if not isinstance(raw, dict):
            return None, "'api_keys' must be an object of {provider_id: {...}}."
        rows = {}
        for pid, row in raw.items():
            if not isinstance(pid, str) or not pid:
                return None, "'api_keys' keys must be non-empty provider id strings."
            if not isinstance(row, dict):
                return None, "'api_keys.%s' must be an object." % pid
            norm = {}
            if "enabled" in row:
                if not isinstance(row["enabled"], bool):
                    return None, "'api_keys.%s.enabled' must be a boolean." % pid
                norm["enabled"] = row["enabled"]
            if "base_url" in row:
                bu = row["base_url"]
                if bu is not None and not isinstance(bu, str):
                    return None, "'api_keys.%s.base_url' must be a string or null." % pid
                norm["base_url"] = bu
            if "api_keys" in row:
                keys = row["api_keys"]
                if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
                    return None, "'api_keys.%s.api_keys' must be an array of strings." % pid
                norm["api_keys"] = keys
            rows[pid] = norm
        out["api_keys"] = rows
    if "flags" in present:
        raw = body["flags"]
        if not isinstance(raw, dict):
            return None, "'flags' must be an object of {name: bool}."
        flags = {}
        for k, v in raw.items():
            if not isinstance(k, str) or not k:
                return None, "'flags' keys must be non-empty strings."
            if not isinstance(v, bool):
                return None, "'flags.%s' must be a boolean." % k
            if k in _SETTINGS_RESERVED_KEYS:
                continue  # never let a flags payload clobber a structural section
            flags[k] = v
        out["flags"] = flags
    if "default" in present:
        raw = body["default"]
        if raw is not None:
            if (not isinstance(raw, dict) or not isinstance(raw.get("provider"), str)
                    or not raw.get("provider") or not isinstance(raw.get("model"), str)
                    or not raw.get("model")):
                return None, "'default' must be null or {\"provider\": str, \"model\": str}."
        out["default"] = raw
    if "local_api_key" in present:
        raw = body["local_api_key"]
        if raw is not None and not isinstance(raw, str):
            return None, "'local_api_key' must be a string or null."
        out["local_api_key"] = raw
    for section in ("media", "images"):
        if section in present:
            raw = body[section]
            if not isinstance(raw, dict):
                return None, "'%s' must be an object." % section
            norm = {}
            if "priority_mode" in raw:
                if raw["priority_mode"] not in ("auto", "manual"):
                    return None, "'%s.priority_mode' must be 'auto' or 'manual'." % section
                norm["priority_mode"] = raw["priority_mode"]
            if "manual_priority" in raw:
                mp = raw["manual_priority"]
                if not isinstance(mp, list) or not all(isinstance(x, str) for x in mp):
                    return None, "'%s.manual_priority' must be an array of strings." % section
                norm["manual_priority"] = mp
            out[section] = norm
    return out, None


def _apply_media_like(getter, updater_call, section_data):
    """CAS-merge helper for media/images -- retries on RevisionConflict (same
    read-revision/retry shape as _mark_runtime_started() below). A no-op when
    the imported section carried neither field (e.g. an empty {})."""
    if not section_data:
        return
    for _attempt in range(5):
        current = getter()
        rev = current.get("revision", 0)

        def _upd(cur, section_data=section_data):
            if "priority_mode" in section_data:
                cur["priority_mode"] = section_data["priority_mode"]
            if "manual_priority" in section_data:
                cur["manual_priority"] = list(section_data["manual_priority"])
            return cur

        try:
            updater_call(rev, _upd)
            return
        except config.RevisionConflict:
            continue
    # 5 concurrent writers on a local single-user desktop app is not a
    # realistic scenario -- give up silently rather than fail the whole
    # import over an already-vanishingly-unlikely race.


def _apply_settings_import(sections):
    """Merge each present, already-VALIDATED section back into the live
    config via config.py's existing setters (add_provider_key/
    clear_provider_keys/set_provider_config/set_flag/set_default/
    clear_default/set_local_api_key/update_media_state/update_images_state)
    -- every one of these already does its own atomic save_config() write
    (see config.py), so no new persistence mechanism is introduced here."""
    if "api_keys" in sections:
        for pid, row in sections["api_keys"].items():
            if "api_keys" in row:
                config.clear_provider_keys(pid)
                for key in row["api_keys"]:
                    config.add_provider_key(pid, key)
            base_url_arg = None
            if "base_url" in row:
                bu = row["base_url"]
                base_url_arg = bu if isinstance(bu, str) and bu.strip() else ""
            config.set_provider_config(pid, enabled=row.get("enabled"), base_url=base_url_arg)
    if "flags" in sections:
        for name, value in sections["flags"].items():
            config.set_flag(name, value)
    if "default" in sections:
        d = sections["default"]
        if d is None:
            config.clear_default()
        else:
            config.set_default(d["provider"], d["model"])
    if "local_api_key" in sections:
        config.set_local_api_key(sections["local_api_key"])
    if "media" in sections:
        _apply_media_like(config.get_media_state, config.update_media_state, sections["media"])
    if "images" in sections:
        _apply_media_like(config.get_images_state, config.update_images_state, sections["images"])


@app.route("/api/settings/import", methods=["POST"])
def api_settings_import():
    body = request.get_json(force=True, silent=True)
    sections, err = _validate_settings_import(body)
    if err:
        return jsonify({"error": err}), 400
    # Every present section is FULLY structurally validated above BEFORE this
    # point -- a malformed/partial upload is rejected wholesale, nothing is
    # written. That validate-first pass is what "all-or-nothing" buys here;
    # it does NOT make the several setter calls below into one filesystem
    # transaction (each is its own already-atomic save_config() call, same as
    # every other multi-field settings change in this app).
    _apply_settings_import(sections)
    return jsonify({"imported": sorted(sections.keys())})


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


def _p_openclaw():
    """OpenClaw's config file. Default ~/.openclaw/openclaw.json, but a --config /
    OPENCLAW_CONFIG override wins (this machine uses ~/openclaw-config/openclaw.json).
    Prefer the env override, then the first candidate that exists, else the default."""
    env = os.environ.get("OPENCLAW_CONFIG")
    if env and env.strip():
        return env if env.lower().endswith(".json") else os.path.join(env, "openclaw.json")
    candidates = [
        os.path.join(_home(), "openclaw-config", "openclaw.json"),
        os.path.join(_home(), ".openclaw", "openclaw.json"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return candidates[1]


def _p_hermes():
    """Hermes Agent's config.yaml. HERMES_HOME wins; else %LOCALAPPDATA%\\hermes on
    Windows, ~/.hermes elsewhere."""
    env = os.environ.get("HERMES_HOME")
    if env and env.strip():
        return os.path.join(env, "config.yaml")
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(_home(), "AppData", "Local")
        return os.path.join(base, "hermes", "config.yaml")
    return os.path.join(_home(), ".hermes", "config.yaml")


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
        # NO env_check on purpose. Codex is wired ONLY by ~/.codex/config.toml
        # (model_provider + [model_providers.*] with wire_api="responses"); it
        # never reads OPENAI_BASE_URL/OPENAI_API_BASE. Checking them here was a
        # real bug: _cli_connected tests env FIRST and short-circuits, so a stale
        # OPENAI_BASE_URL left over from another tool's manual setup made Codex
        # report "Connected via the OPENAI_BASE_URL environment variable" even
        # right after Disconnect had correctly cleaned config.toml — the popup
        # said disconnected, the badge said connected, and the badge was wrong.
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
            "  name = \"Calvoun Free LLM Hub\"\n"
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
    {
        "id": "openclaw",
        "name": "OpenClaw",
        "kind": "openai",
        "bins": ["openclaw"],
        "config_paths": [_p_openclaw()],
        # OpenClaw is set up via openclaw.json even when its bin/daemon isn't on this
        # shell's PATH, so the presence of that file counts as "installed".
        "config_means_installed": True,
        # NO env_check: OpenClaw ignores OPENAI_BASE_URL for custom endpoints; it is
        # wired ONLY by a models.providers.<id> block inside openclaw.json.
        "autofix": "openclaw",
        "write_path": _p_openclaw(),
        "default_method": "config",
        "hint": ("Configured. Run Auto-fix to add a 'freehub' openai-compatible provider to "
                 "openclaw.json (models.providers.freehub + the agents allowlist + "
                 "primary = freehub/auto). OpenClaw hot-reloads — no restart."),
        "manual_note": (
            "OpenClaw is wired by openclaw.json, NOT environment variables. Auto-fix (merge-safe) adds:\n"
            "  models.providers.freehub = { baseUrl: \"http://127.0.0.1:%d/v1\", apiKey: \"<local key>\",\n"
            "    api: \"openai-completions\", models: [{ id: \"auto\", name: \"Calvoun Free LLM Hub\" }] }\n"
            "then allowlists \"freehub/auto\" in agents.defaults.models and sets\n"
            "  agents.defaults.model.primary = \"freehub/auto\"  (your previous primary is remembered and\n"
            "restored on Disconnect). The localhost hub needs no real key; a dummy string is fine. OpenClaw\n"
            "watches the file and hot-reloads, so no restart is needed." % PORT
        ),
    },
    {
        "id": "hermes",
        "name": "Hermes Agent",
        "kind": "openai",
        "bins": ["hermes"],
        "config_paths": [_p_hermes()],
        "env_check": ["OPENAI_BASE_URL", "OPENAI_API_BASE"],
        "autofix": "hermes",
        "write_path": _p_hermes(),
        "default_method": "config",
        "hint": ("Installed. Run Auto-fix to set model.provider = custom + base_url in Hermes' "
                 "config.yaml. Restart the Hermes session afterwards."),
        "manual_note": (
            "Hermes (Nous Research) is wired by config.yaml (%%LOCALAPPDATA%%\\hermes on Windows, "
            "~/.hermes elsewhere), NOT environment variables for a custom endpoint. Auto-fix "
            "(merge-safe) sets:\n"
            "  model:\n"
            "    provider: custom\n"
            "    base_url: http://127.0.0.1:%d/v1   # end at /v1; Hermes appends /chat/completions\n"
            "    default: auto\n"
            "    api_key: <local key>               # optional for a keyless local server\n"
            "Changing base_url needs a RESTART of Hermes. (Alternatively set model.provider: openai-api "
            "and put OPENAI_BASE_URL + OPENAI_API_KEY in the sibling .env.)" % PORT
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
    # Some tools (e.g. OpenClaw) run as a daemon / via npx and aren't on the
    # current shell's PATH, yet an existing config file proves they're set up.
    if entry.get("config_means_installed"):
        for cp in entry.get("config_paths", []):
            if cp and os.path.isfile(cp):
                return True, cp
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
    fd, tmp = tempfile.mkstemp(prefix=".freehub-write-", dir=parent or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
        "name": "Calvoun Free LLM Hub",
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
        'name = "Calvoun Free LLM Hub"',
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
            # utf-8-sig: strip a leading UTF-8 BOM so it never lands mid-file after
            # we prepend model_provider/model above it (a mid-file BOM makes Codex
            # reject config.toml with "invalid unquoted key" at that line).
            with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
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


def _autofix_openclaw(entry, key, base_root, base_v1, model):
    """Merge a 'freehub' OpenAI-compatible provider into openclaw.json: register it
    under models.providers, allowlist freehub/auto in agents.defaults.models, and set
    agents.defaults.model.primary = freehub/auto (remembering the previous primary so
    Disconnect can restore it). OpenClaw hot-reloads the file — no restart."""
    path = _p_openclaw()
    data = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:  # tolerate a UTF-8 BOM
                data = json.load(f)
        except (OSError, ValueError):
            return {"ok": False, "reason": ("existing %s isn't plain JSON (OpenClaw allows JSON5 "
                    "comments, which aren't auto-merged) — add the freehub provider by hand, then retry."
                    % _short(path))}
        if not isinstance(data, dict):
            return {"ok": False, "reason": "existing %s is not a JSON object; not overwriting." % _short(path)}
    backup = _backup_once(path)
    abort = _abort_if_backup_failed(path, backup)
    if abort:
        return abort
    models = data.get("models")
    if not isinstance(models, dict):
        models = {}
    models.setdefault("mode", "merge")  # merge = keep OpenClaw's built-in providers
    providers = models.get("providers")
    if not isinstance(providers, dict):
        providers = {}
    providers["freehub"] = {
        "baseUrl": base_v1,
        "apiKey": key,
        "api": "openai-completions",
        "timeoutSeconds": 300,
        "models": [{
            "id": "auto",
            "name": "Calvoun Free LLM Hub (auto)",
            "reasoning": False,
            "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 200000,
            "maxTokens": 8192,
        }],
    }
    models["providers"] = providers
    data["models"] = models
    agents = data.get("agents")
    if not isinstance(agents, dict):
        agents = {}
    defaults = agents.get("defaults")
    if not isinstance(defaults, dict):
        defaults = {}
    amodels = defaults.get("models")
    if not isinstance(amodels, dict):
        amodels = {}
    amodels["freehub/auto"] = {"alias": "Free LLM Hub"}  # allowlist (else "model not allowed")
    defaults["models"] = amodels
    mdl = defaults.get("model")
    if not isinstance(mdl, dict):
        mdl = {}
    prev = mdl.get("primary")
    if isinstance(prev, str) and prev and not prev.startswith("freehub/"):
        config.set_setting("openclaw_prev_primary", prev)  # remember for Disconnect
    mdl["primary"] = "freehub/auto"
    defaults["model"] = mdl
    agents["defaults"] = defaults
    data["agents"] = agents
    _cli_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return {
        "ok": True,
        "wrote_path": path,
        "backup_path": backup,
        "applied": {"provider": "freehub", "baseURL": base_v1, "apiKey": _mask_key(key),
                    "primary": "freehub/auto"},
        "restart_hint": "OpenClaw watches openclaw.json and hot-reloads — no restart needed.",
    }


def _autofix_hermes(entry, key, base_root, base_v1, model):
    """Set model.{provider,base_url,default,api_key} in Hermes' config.yaml (merge-safe
    via PyYAML). Hermes needs a restart to re-read it."""
    path = _p_hermes()
    try:
        import yaml
    except ImportError:
        return {"ok": False, "reason": ("PyYAML isn't available to safely edit Hermes' config.yaml — "
                "add the model block by hand (see the instructions).")}
    data = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:  # tolerate a UTF-8 BOM
                loaded = yaml.safe_load(f)
            if loaded is not None:
                data = loaded
        except (OSError, yaml.YAMLError):
            return {"ok": False, "reason": "existing %s is not valid YAML — fix or remove it, then retry."
                    % _short(path)}
        if not isinstance(data, dict):
            return {"ok": False, "reason": "existing %s is not a YAML mapping; not overwriting." % _short(path)}
    backup = _backup_once(path)
    abort = _abort_if_backup_failed(path, backup)
    if abort:
        return abort
    mdl = data.get("model")
    if not isinstance(mdl, dict):
        mdl = {}
    mdl["provider"] = "custom"
    mdl["base_url"] = base_v1   # end at /v1; Hermes appends /chat/completions
    mdl["default"] = "auto"
    mdl["api_key"] = key
    data["model"] = mdl
    _cli_write_text(path, yaml.safe_dump(data, default_flow_style=False, sort_keys=False,
                                         allow_unicode=True))
    return {
        "ok": True,
        "wrote_path": path,
        "backup_path": backup,
        "applied": {"model.provider": "custom", "model.base_url": base_v1,
                    "model.default": "auto", "model.api_key": _mask_key(key)},
        "restart_hint": "Restart the Hermes CLI/session so it re-reads config.yaml.",
    }


_AUTOFIXERS = {
    "claude": _autofix_claude,
    "aider": _autofix_aider,
    "opencode": _autofix_opencode,
    "qwen": _autofix_qwen,
    "codex": _autofix_codex,
    "openclaw": _autofix_openclaw,
    "hermes": _autofix_hermes,
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
            # utf-8-sig: tolerate/strip a leading UTF-8 BOM on the way back out too.
            with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
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


def _disconnect_openclaw(entry):
    path = entry["write_path"]
    hint = "OpenClaw watches openclaw.json and hot-reloads — no restart needed."
    # STRUCTURED-CONFIG revert: strip ONLY the freehub provider + allowlist entry we
    # added and restore the previous primary, so any provider/channel/plugin the user
    # added after connecting survives. Backup restore is the last resort for a file
    # that no longer parses as JSON.
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:  # tolerate a UTF-8 BOM
                data = json.load(f)
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            changed = False
            models = data.get("models")
            if isinstance(models, dict):
                providers = models.get("providers")
                if isinstance(providers, dict) and providers.pop("freehub", None) is not None:
                    changed = True
                    if not providers:
                        models.pop("providers", None)
            agents = data.get("agents")
            defaults = agents.get("defaults") if isinstance(agents, dict) else None
            if isinstance(defaults, dict):
                amodels = defaults.get("models")
                if isinstance(amodels, dict) and amodels.pop("freehub/auto", None) is not None:
                    changed = True
                mdl = defaults.get("model")
                if isinstance(mdl, dict) and mdl.get("primary") == "freehub/auto":
                    prev = config.get_setting("openclaw_prev_primary")
                    if prev:
                        mdl["primary"] = prev      # restore what they had before connecting
                    else:
                        mdl.pop("primary", None)   # no memory -> let OpenClaw use its own default
                    changed = True
            if changed:
                config.set_setting("openclaw_prev_primary", "")  # clear the stash
                _cli_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
            _discard_backup(path)  # strip succeeded -> stale backup no longer needed
            return {"restored_from_backup": False, "wrote_path": path,
                    "changed": changed, "restart_hint": hint}
    # Live file missing or no longer valid JSON -> fall back to the frozen backup.
    if _restore_backup(path):
        return {"restored_from_backup": True, "wrote_path": path, "restart_hint": hint}
    return {"restored_from_backup": False, "wrote_path": path,
            "changed": False, "restart_hint": hint}


def _disconnect_hermes(entry):
    path = entry["write_path"]
    hint = "Restart the Hermes CLI/session so it re-reads config.yaml."
    # STRUCTURED-CONFIG revert: strip ONLY our model.* keys (and only when base_url
    # points HERE), so any other Hermes setting in config.yaml survives.
    if os.path.isfile(path):
        try:
            import yaml
            with open(path, "r", encoding="utf-8-sig") as f:  # tolerate a UTF-8 BOM
                data = yaml.safe_load(f)
        except Exception:
            data = None
        if isinstance(data, dict):
            changed = False
            mdl = data.get("model")
            if isinstance(mdl, dict) and _points_at_hub(mdl.get("base_url")):
                for k in ("provider", "base_url", "default", "api_key"):
                    if mdl.pop(k, None) is not None:
                        changed = True
                if not mdl:
                    data.pop("model", None)
            deleted = False
            if changed:
                if not data:
                    # Auto-fix CREATED this file (Hermes had no config.yaml) -> remove
                    # the now-empty shell for a clean revert.
                    try:
                        os.remove(path)
                        deleted = True
                    except OSError:
                        _cli_write_text(path, yaml.safe_dump(data, default_flow_style=False,
                                                             sort_keys=False, allow_unicode=True))
                else:
                    _cli_write_text(path, yaml.safe_dump(data, default_flow_style=False,
                                                         sort_keys=False, allow_unicode=True))
            _discard_backup(path)
            out = {"restored_from_backup": False, "wrote_path": path,
                   "changed": changed, "restart_hint": hint}
            if deleted:
                out["deleted"] = True
            return out
    if _restore_backup(path):
        return {"restored_from_backup": True, "wrote_path": path, "restart_hint": hint}
    return {"restored_from_backup": False, "wrote_path": path,
            "changed": False, "restart_hint": hint}


_DISCONNECTERS = {
    "claude": _disconnect_claude,
    "aider": _disconnect_aider,
    "opencode": _disconnect_opencode,
    "qwen": _disconnect_qwen,
    "codex": _disconnect_codex,
    "llm": _disconnect_llm,   # id-keyed: `llm` has no autofix strategy string
    "openclaw": _disconnect_openclaw,
    "hermes": _disconnect_hermes,
}


# ---------------------------------------------------------------------------
# Transactional hub mode (bulk connect/disconnect)
# ---------------------------------------------------------------------------
_hub_switch_lock = threading.Lock()


def _read_optional_bytes(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
        return True, data
    except FileNotFoundError:
        return False, b""


def _atomic_write_bytes(path, data, mode=None):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".freehub-restore-", dir=parent or ".")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if os.name == "posix":
            os.chmod(tmp, int(mode) & 0o777 if mode is not None else 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _snapshot_manifest_path(generation, cid):
    return os.path.join(config.snapshot_dir(generation, cid), "manifest.json")


def _write_snapshot_manifest(generation, cid, manifest):
    directory = config.snapshot_dir(generation, cid)
    os.makedirs(directory, exist_ok=True)
    if os.name == "posix":
        os.chmod(directory, 0o700)
    _atomic_write_bytes(_snapshot_manifest_path(generation, cid),
                        (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
                        0o600)


def _load_snapshot_manifest(generation, cid):
    try:
        with open(_snapshot_manifest_path(generation, cid), "r", encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _capture_cli_snapshot(generation, entry):
    """Capture the pre-hub bytes before a bulk connect touches a CLI file.

    If an older one-click connect already made a .freehub-bak, that backup is
    the real pre-hub state and is adopted. This makes the first master-switch
    cycle backward-compatible with existing installs.
    """
    cid = entry["id"]
    path = entry.get("write_path")
    if not path:
        raise ValueError("CLI has no managed write path")
    source = path
    connected, _method, _detail = _cli_connected(entry)
    has_prior_backup = connected and os.path.isfile(path + ".freehub-bak")
    if has_prior_backup:
        source = path + ".freehub-bak"
    existed, original = _read_optional_bytes(source)
    try:
        mode = os.stat(source).st_mode & 0o777 if existed else None
    except OSError:
        mode = None
    directory = config.snapshot_dir(generation, cid)
    os.makedirs(directory, exist_ok=True)
    if existed:
        _atomic_write_bytes(os.path.join(directory, "original.bin"), original, 0o600)
    manifest = {
        "version": 1,
        "cli_id": cid,
        "path": path,
        "original_exists": existed,
        "original_sha256": config.sha256_bytes(original) if existed else None,
        "original_mode": mode,
        "managed_sha256": None,
        "restore_strategy": "snapshot" if (not connected or has_prior_backup) else "semantic",
    }
    _write_snapshot_manifest(generation, cid, manifest)
    return manifest


def _current_file_sha(path):
    exists, data = _read_optional_bytes(path)
    return exists, (config.sha256_bytes(data) if exists else None)


def _restore_cli_snapshot(generation, cid):
    """Restore only when the live file still equals our managed output."""
    manifest = _load_snapshot_manifest(generation, cid)
    if not manifest:
        return {"status": "conflict", "detail": "snapshot manifest is missing or corrupt"}
    path = manifest.get("path")
    if not isinstance(path, str) or not path:
        return {"status": "conflict", "detail": "snapshot path is invalid"}
    exists, current_sha = _current_file_sha(path)
    managed_sha = manifest.get("managed_sha256")
    original_sha = manifest.get("original_sha256")
    if (manifest.get("restore_strategy") != "semantic" and
            exists == bool(manifest.get("original_exists")) and current_sha == original_sha):
        return {"status": "off", "path": path, "changed": False}
    if not managed_sha or not exists or current_sha != managed_sha:
        return {"status": "conflict", "path": path,
                "detail": "CLI config changed after hub mode enabled; left untouched"}
    if manifest.get("restore_strategy") == "semantic":
        entry = _get_cli_entry(cid)
        reverter = _DISCONNECTERS.get(entry.get("autofix")) if entry else None
        if not reverter:
            return {"status": "conflict", "path": path,
                    "detail": "no safe semantic disconnect strategy is available"}
        _discard_backup(path)  # a re-connect backup would contain the hub config
        result = reverter(entry)
        if _cli_connected(entry)[0]:
            return {"status": "conflict", "path": path,
                    "detail": "CLI still points at the hub after safe disconnect"}
        return {"status": "off", "path": path,
                "changed": bool(result.get("changed", True))}
    if manifest.get("original_exists"):
        try:
            with open(os.path.join(config.snapshot_dir(generation, cid), "original.bin"), "rb") as f:
                original = f.read()
        except OSError:
            return {"status": "conflict", "path": path,
                    "detail": "snapshot bytes are missing; live config left untouched"}
        if config.sha256_bytes(original) != original_sha:
            return {"status": "conflict", "path": path,
                    "detail": "snapshot checksum failed; live config left untouched"}
        _atomic_write_bytes(path, original, manifest.get("original_mode"))
    else:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    _discard_backup(path)
    return {"status": "off", "path": path, "changed": True}


def _hub_mode_payload():
    return {"state": config.get_hub_mode_state(),
            "clients": [_cli_row(entry) for entry in CLI_REGISTRY]}


def _finalize_hub_state(revision, phase, clients):
    def _update(state):
        state["phase"] = phase
        state["clients"] = clients
        return state
    return config.update_hub_mode_state(revision, _update)


def _bulk_hub_on(expected_revision):
    generation = config.new_generation_id()

    def _begin(state):
        state.update({"desired": "on", "phase": "changing",
                      "generation": generation, "clients": {}})
        return state

    changing = config.update_hub_mode_state(expected_revision, _begin)
    clients = {}
    failures = False
    changed_ids = []
    key = config.get_local_api_key() or "free-llm-hub"
    base_root = "http://127.0.0.1:%d" % PORT
    base_v1 = base_root + "/v1"
    model = _first_free_model_id()
    if not model:
        clients["_hub"] = {"status": "error", "detail": "no free model configured"}
        return _finalize_hub_state(changing["revision"], "error", clients)
    for entry in CLI_REGISTRY:
        cid = entry["id"]
        manifest = None
        installed, _binary = _cli_installed(entry)
        fixer = _AUTOFIXERS.get(entry.get("autofix"))
        if not installed or not fixer:
            clients[cid] = {"status": "skipped",
                            "detail": "not installed" if not installed else "manual-only CLI"}
            continue
        try:
            manifest = _capture_cli_snapshot(generation, entry)
            result = fixer(entry, key, base_root, base_v1, model)
            if not result.get("ok"):
                failures = True
                clients[cid] = {"status": "error", "detail": result.get("reason") or "connect failed"}
                exists, current_sha = _current_file_sha(manifest["path"])
                if exists and current_sha != manifest.get("original_sha256"):
                    manifest["managed_sha256"] = current_sha
                    _write_snapshot_manifest(generation, cid, manifest)
                    rollback = _restore_cli_snapshot(generation, cid)
                    clients[cid]["rollback"] = rollback["status"]
                continue
            exists, managed_sha = _current_file_sha(manifest["path"])
            if not exists:
                raise OSError("CLI config was not created")
            manifest["managed_sha256"] = managed_sha
            _write_snapshot_manifest(generation, cid, manifest)
            changed_ids.append(cid)
            clients[cid] = {"status": "on", "path": _short(manifest["path"]),
                            "original_sha256": manifest["original_sha256"],
                            "managed_sha256": managed_sha,
                            "restart_hint": result.get("restart_hint")}
        except Exception as exc:
            failures = True
            clients[cid] = {"status": "error", "detail": _sanitize(str(exc))}
            # A writer may have changed the file and then raised (disk/fsync
            # errors are the classic case). If so, checksum that exact output
            # and use the already-captured original to roll it back safely.
            try:
                if manifest and manifest.get("cli_id") == cid:
                    exists, current_sha = _current_file_sha(manifest["path"])
                    if exists and current_sha != manifest.get("original_sha256"):
                        manifest["managed_sha256"] = current_sha
                        _write_snapshot_manifest(generation, cid, manifest)
                        rollback = _restore_cli_snapshot(generation, cid)
                        clients[cid]["rollback"] = rollback["status"]
            except Exception as rollback_exc:
                clients[cid]["rollback"] = "conflict"
                clients[cid]["rollback_detail"] = _sanitize(str(rollback_exc))
    if failures:
        # All-or-nothing on enable: clean managed files are rolled back. A file
        # concurrently edited by the user becomes a conflict and is untouched.
        for cid in changed_ids:
            try:
                rollback = _restore_cli_snapshot(generation, cid)
                clients[cid]["rollback"] = rollback["status"]
                if rollback["status"] == "conflict":
                    clients[cid]["detail"] = rollback.get("detail")
            except Exception as exc:
                clients[cid]["rollback"] = "conflict"
                clients[cid]["detail"] = _sanitize(str(exc))
        return _finalize_hub_state(changing["revision"], "error", clients)
    return _finalize_hub_state(changing["revision"], "on", clients)


def _bulk_hub_off(expected_revision):
    previous = config.get_hub_mode_state()

    def _begin(state):
        state.update({"desired": "off", "phase": "changing"})
        return state

    changing = config.update_hub_mode_state(expected_revision, _begin)
    generation = previous.get("generation")
    clients = {}
    conflicts = False
    if generation:
        managed_ids = [cid for cid, row in (previous.get("clients") or {}).items()
                       if isinstance(row, dict) and row.get("status") in ("on", "conflict")]
        for cid in managed_ids:
            try:
                result = _restore_cli_snapshot(generation, cid)
            except Exception as exc:
                result = {"status": "conflict", "detail": _sanitize(str(exc))}
            clients[cid] = result
            conflicts = conflicts or result.get("status") == "conflict"
    else:
        # Migration path for installations connected before master mode existed.
        for entry in CLI_REGISTRY:
            connected, _method, _detail = _cli_connected(entry)
            reverter = _DISCONNECTERS.get(entry.get("autofix"))
            if not connected or not reverter:
                clients[entry["id"]] = {"status": "skipped"}
                continue
            try:
                result = reverter(entry)
                still_connected = _cli_connected(entry)[0]
                clients[entry["id"]] = {"status": "conflict" if still_connected else "off",
                                        "path": result.get("wrote_path")}
                conflicts = conflicts or still_connected
            except Exception as exc:
                clients[entry["id"]] = {"status": "conflict", "detail": _sanitize(str(exc))}
                conflicts = True
    return _finalize_hub_state(changing["revision"], "conflict" if conflicts else "off", clients)


def _mark_hub_mode_unmanaged():
    """An individual CLI edit intentionally exits bulk-managed mode."""
    for _attempt in range(2):
        state = config.get_hub_mode_state()
        if state.get("phase") in ("unmanaged", "changing"):
            return
        try:
            config.update_hub_mode_state(state["revision"], lambda value: {
                **value, "phase": "unmanaged", "generation": None, "clients": {}})
            return
        except config.RevisionConflict:
            continue


def _recover_interrupted_hub_transition():
    """Finish a crashed disable, or roll back a crashed enable at startup."""
    state = config.get_hub_mode_state()
    if state.get("phase") != "changing":
        return
    if not _hub_switch_lock.acquire(blocking=False):
        return
    try:
        state = config.get_hub_mode_state()
        if state.get("phase") != "changing":
            return
        if state.get("desired") == "off":
            _bulk_hub_off(state["revision"])
            return
        generation = state.get("generation")
        clients = {}
        conflicts = False
        if generation:
            try:
                root = os.path.join(config.snapshots_dir(), config.re_safe_component(generation))
                cids = os.listdir(root)
            except (OSError, ValueError):
                cids = []
            for cid in cids:
                try:
                    safe_cid = config.re_safe_component(cid)
                    manifest = _load_snapshot_manifest(generation, safe_cid)
                    if not manifest:
                        raise ValueError("snapshot manifest is missing or corrupt")
                    exists, current_sha = _current_file_sha(manifest["path"])
                    original_same = (exists == bool(manifest.get("original_exists")) and
                                     current_sha == manifest.get("original_sha256"))
                    if original_same and not manifest.get("managed_sha256"):
                        result = {"status": "off", "changed": False,
                                  "detail": "enable interrupted before this CLI was changed"}
                    else:
                        result = _restore_cli_snapshot(generation, safe_cid)
                    clients[safe_cid] = result
                    conflicts = conflicts or result.get("status") == "conflict"
                except Exception as exc:
                    clients[str(cid)] = {"status": "conflict", "detail": _sanitize(str(exc))}
                    conflicts = True
        clients["_recovery"] = {
            "status": "error",
            "detail": "an interrupted enable was rolled back at startup; retry Hub mode",
        }
        _finalize_hub_state(state["revision"], "conflict" if conflicts else "error", clients)
    finally:
        _hub_switch_lock.release()


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
        if _is_sub(hop_pid):
            # A connectivity probe must NEVER spend the user's paid subscription:
            # this proves the FREE pipeline works, and a sub hop is not part of it.
            continue
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


@app.route("/api/hub-mode", methods=["GET", "POST"])
@app.route("/api/lifecycle/hub", methods=["GET", "POST"])
def api_hub_mode():
    if request.method == "GET":
        return jsonify(_hub_mode_payload())
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "invalid JSON body"}), 400
    desired = body.get("desired")
    if desired is None and isinstance(body.get("enabled"), bool):
        desired = "on" if body["enabled"] else "off"
    if desired not in ("on", "off"):
        return jsonify({"error": "desired must be 'on' or 'off'"}), 400
    if "revision" not in body:
        return jsonify({"error": "revision is required"}), 400
    if not _hub_switch_lock.acquire(blocking=False):
        return jsonify({"error": "a hub mode transition is already running",
                        "state": config.get_hub_mode_state()}), 409
    try:
        current = config.get_hub_mode_state()
        try:
            expected = int(body["revision"])
        except (TypeError, ValueError):
            return jsonify({"error": "revision must be an integer"}), 400
        if expected != current["revision"]:
            return jsonify({"error": "hub state changed; reload and retry",
                            "current_revision": current["revision"], "state": current}), 409
        if current.get("desired") == desired and current.get("phase") == desired:
            return jsonify(_hub_mode_payload())
        try:
            if desired == "on":
                _bulk_hub_on(expected)
            else:
                _bulk_hub_off(expected)
        except config.RevisionConflict as exc:
            return jsonify({"error": "hub state changed during transition",
                            "current_revision": exc.current_revision,
                            "state": config.get_hub_mode_state()}), 409
        except Exception as exc:
            _log.error("Hub mode transition failed: %s", _sanitize(str(exc)))
            return jsonify({"error": _sanitize(str(exc)) or "hub transition failed",
                            "state": config.get_hub_mode_state()}), 500
        return jsonify(_hub_mode_payload())
    finally:
        _hub_switch_lock.release()


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
    if result.get("ok"):
        _mark_hub_mode_unmanaged()
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
    _mark_hub_mode_unmanaged()
    # VERIFY THE REVERT. Recompute freshly from disk/env so 'connected' reflects
    # reality, and if the CLI is STILL wired to the hub, say so instead of
    # reporting a clean success. This is the honest answer to "I clicked
    # Disconnect, it said done, but the CLI is still connected": the config
    # revert worked, but a hub-pointing env var (OPENAI_BASE_URL / ANTHROPIC_
    # BASE_URL, often left over from an older manual setup) still overrides it,
    # and we do NOT silently unset a user's environment. Hand back the exact
    # commands instead so the popup can show them.
    row = _cli_row(entry)
    connected = bool(row.get("connected"))
    out = {
        "ok": True,
        "restored_from_backup": bool(result.get("restored_from_backup")),
        "wrote_path": result.get("wrote_path"),
        "restart_hint": result.get("restart_hint"),
        "connected": connected,
    }
    if "changed" in result:
        out["changed"] = bool(result["changed"])
    if connected:
        method = row.get("connect_method")
        out["still_connected"] = True
        out["still_connected_via"] = method
        out["still_connected_detail"] = row.get("detail")
        if method == "env":
            out["note"] = (
                "Config reverted, but %s is STILL pointed at the hub by an environment "
                "variable (%s). Environment variables override the config file, so run "
                "the commands below to finish disconnecting, then open a NEW terminal."
                % (entry.get("name", entry["id"]), row.get("detail") or "an env var"))
            out["commands"] = _env_unset_commands(entry)
        else:
            out["note"] = (
                "Config reverted, but %s still reports as connected (%s). Nothing else "
                "was changed — check that path manually."
                % (entry.get("name", entry["id"]), row.get("detail") or "unknown source"))
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
    return jsonify({"object": "list",
                    "data": [_codex_model_entry(m) for m in agg],
                    "models": [dict(_codex_model_entry(m), slug=m["id"]) for m in agg]})


# Model row for /v1/models. `display_name` is additive and harmless for every
# client. We deliberately do NOT try to mirror Codex's full, strict model-manager
# schema (reasoning-effort presets, capability structs, ...): it changes across
# Codex versions, so chasing it here would break on every Codex update. Codex is
# therefore best pointed at its NATIVE subscription for the agentic Chat CLI (the
# hub's free models remain great for the Chat/Test playground + general CLI use,
# which use /v1/chat/completions and don't hit this strict schema).
def _codex_model_entry(m):
    return {
        "id": m["id"],
        "object": "model",
        "created": 0,
        "owned_by": m["provider"],
        "display_name": m["id"],
    }


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


def _peek_until_content(iterator, deadline_s, max_lines=400):
    """Look ahead on a streaming 200 to tell a REAL answer from an EMPTY one before
    committing it to the client. Reads SSE items (bytes) until one carries actual
    content / a tool call (the provider is really answering), or the stream
    TERMINATES with none (an empty 200 — some free providers 200 then stream only a
    role delta + [DONE], which left codex with nothing and no retry), or the stream
    goes idle past `deadline_s` (hung). Buffers everything it reads so the caller can
    replay it losslessly. Returns (status, buffered):
      'content' -> commit; stream `buffered` then the rest of the SAME iterator.
      'empty'   -> carried no content; caller closes resp + falls through to next model.
      'timeout' -> nothing usable arrived in time; caller falls through.
    Same daemon-worker discipline as _peek_first_chunk: the buffer/iterator are only
    used when the worker FINISHED (status set) so there's never concurrent iteration."""
    box = {"buf": [], "status": None}

    def _worker():
        buf = box["buf"]
        try:
            for _ in range(max_lines):
                item = next(iterator)
                buf.append(item)
                if not item:
                    continue
                b = item if isinstance(item, (bytes, bytearray)) \
                    else str(item).encode("utf-8", "ignore")
                if _STREAM_CONTENT_RE.search(b):
                    box["status"] = "content"
                    return
                if _STREAM_TERMINAL_RE.search(b):
                    box["status"] = "empty"
                    return
            box["status"] = "content"   # lots of data, no terminal yet -> real stream
        except StopIteration:
            box["status"] = "empty"     # ended before any content
        except Exception:
            box["status"] = "empty"     # read error/timeout -> unusable, fall through

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(deadline_s)
    if t.is_alive():
        return "timeout", []
    return box.get("status") or "empty", list(box["buf"])


def _chain_buffered(buffered, iterator):
    """Yield each already-read item in `buffered`, then the rest of `iterator` — the
    multi-item version of _chain_first, used to replay a look-ahead losslessly."""
    for item in buffered or ():
        yield item
    for item in iterator:
        yield item


def _chat_json_is_empty(data):
    """True if an OpenAI chat-completions JSON carried NO assistant content AND no
    tool calls — a non-streamed 'empty 200' that should fall through to the next
    model. Conservative: returns False on anything unexpected so a real answer is
    never discarded."""
    try:
        msg = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):  # content-parts -> join their text
            content = "".join((p.get("text") or "") for p in content
                              if isinstance(p, dict))
        has_text = bool(content and str(content).strip())
        # A tool_call counts as content ONLY if it has a usable function name — a
        # nameless/blank tool_call is unusable and should fall through to a real model.
        has_tools = any(
            isinstance(tc, dict) and ((tc.get("function") or {}).get("name") or "").strip()
            for tc in (msg.get("tool_calls") or []))
        return not (has_text or has_tools)
    except Exception:
        return False


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
    try:
        body["messages"], image_count = _normalize_openai_messages(body.get("messages"))
    except ValueError as exc:
        return _openai_error(str(exc), 400)
    has_images = image_count > 0
    # Orchestrate (Auto): route by task difficulty AND request size so weak/small
    # providers take easy work and big requests avoid small-TPM providers (413).
    # Explicit '<pid>/<model>' bypasses model choice (chain still size-filters).
    est = _est_tokens(body.get("messages"), body.get("tools"))
    has_tools = bool(body.get("tools"))
    diff = None
    if _is_orchestrate(body.get("model")):
        router = _route_for_vision if has_images else _route_by_difficulty
        pid, resolved, diff = router(body.get("messages"), body.get("max_tokens"), est,
                                     require_tools=has_tools)
        if pid is None:
            if has_images:
                return _openai_error(
                    "No enabled verified vision model is available. Enable Google, "
                    "Cloudflare, or Z.AI with a usable vision model.", 400)
            pid, resolved = _resolve_model(body.get("model"))  # default/best or error
    else:
        pid, resolved = _resolve_model(body.get("model"))
    if pid is None:
        return _openai_error(resolved, 400)
    if not prov.is_model_allowed(resolved):
        return _openai_error("Model '%s' is blocked by the safety filter." % resolved, 403,
                             "permission_error")
    if has_images and not _is_vision_model(pid, resolved):
        return _openai_error(
            "Model '%s/%s' is not a verified vision model." % (pid, resolved), 400)
    not_ready = _check_provider_ready(pid)
    if not_ready:
        return _openai_error(not_ready, 400)

    stream = bool(body.get("stream"))
    errors = []
    last_hard = None  # last hard (non-retryable) upstream error, relayed if the chain is exhausted
    for hop_pid, hop_model in _build_chain(pid, resolved, est, require_vision=has_images,
                                           require_tools=has_tools):
        if not prov.is_model_allowed(hop_model):
            continue
        if stream and _is_sub(hop_pid):
            errors.append("%s: skipped (a local CLI cannot stream)" % hop_pid)
            continue
        payload = dict(body)
        payload["model"] = hop_model
        _apply_reasoning_effort(payload, hop_model, diff)
        try:
            _act_pick(hop_pid, hop_model)
            resp = _dispatch_chat(hop_pid, payload, stream)
        except (requests.RequestException, RuntimeError) as exc:
            errors.append("%s: %s" % (hop_pid, _sanitize(exc.__class__.__name__)))
            continue
        if resp.status_code == 200:
            if stream:
                # #4: peek the first byte BEFORE committing the 200. A hung/slow
                # stream (no first byte within STREAM_FIRST_BYTE_TIMEOUT) falls
                # through to the next provider instead of stalling the client.
                it = resp.iter_content(chunk_size=None)
                # Peek until REAL content: a 200 that streams no content must fall
                # through to the next model, not be handed to the client as empty.
                status, buffered = _peek_until_content(it, STREAM_CONTENT_PEEK_TIMEOUT)
                if status != "content":
                    errors.append("%s: %s (200 but no content)" % (hop_pid, status))
                    resp.close()
                    continue
                chained = _chain_buffered(buffered, it)
                return Response(stream_with_context(_proxy_sse(resp, chained)),
                                mimetype="text/event-stream", headers=_SSE_HEADERS)
            try:
                data = resp.json()
            except (ValueError, requests.RequestException):
                # Non-JSON / broken 200 body -> don't dead-end, try the next model.
                errors.append("%s: non-JSON 200 body" % hop_pid)
                resp.close()
                continue
            if _chat_json_is_empty(data):
                errors.append("%s: empty (200 but no content)" % hop_pid)
                resp.close()
                continue
            _record_chat_usage(hop_pid, hop_model, data, est)
            if isinstance(data, dict):
                data["model"] = hop_pid + "/" + hop_model
            return jsonify(data), 200
        # Non-2xx. Retryable (429/5xx) and HARD errors (404/400/model-not-found)
        # both advance to the NEXT provider — each chain hop is a DIFFERENT
        # provider, so a broken model/provider should fall through before we give
        # up. Key rotation for the SAME provider already happened in _upstream_chat.
        # A network error while INSPECTING the error body (stream=True leaves the body
        # unread) must also just advance, never escape the loop into a 500.
        try:
            errors.append("%s: HTTP %d" % (hop_pid, resp.status_code))
            if resp.status_code == 400 and _classify_soft_400(resp):
                resp.close()
                continue
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
        except requests.RequestException as exc:
            errors.append("%s: %s reading error body" % (hop_pid, _sanitize(exc.__class__.__name__)))
        resp.close()
        continue
    # Chain exhausted. Tell the client HOW LONG until a model frees (Retry-After) so
    # its SDK waits out a short throttle and auto-continues once capacity returns.
    eta = _capacity_eta()
    if last_hard is not None:
        if last_hard["json"] is not None:
            return _with_retry_after(
                (jsonify(last_hard["json"]), _retryable_relay_status(last_hard["status"])), eta)
        return _with_retry_after(_openai_error(
            "Upstream returned non-JSON (%s, HTTP %d): %s"
            % (last_hard["pid"], last_hard["status"], last_hard["text"]), 503, "upstream_error"), eta)
    return _with_retry_after(_openai_error(
        "All providers failed: " + ("; ".join(errors) or "none available"), 503, "upstream_error"), eta)


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
                "tool_call_id": item.get("call_id") or item.get("id"),
                "content": content,
            })
        elif itype == "message" or itype is None:
            role = _norm_role(item.get("role"))
            content = item.get("content")
            if isinstance(content, str):
                text = content
            else:
                parts = []
                multimodal = False
                for part in content or []:
                    if isinstance(part, str):
                        parts.append(part)
                    elif isinstance(part, dict):
                        ptype = part.get("type")
                        if ptype in ("input_text", "output_text", "text"):
                            parts.append(part.get("text") or "")
                        elif ptype in ("input_image", "image_url"):
                            if part.get("file_id") and not part.get("image_url"):
                                raise ValueError("Responses file_id images cannot be resolved by this hub")
                            image = _normalize_image_url(part.get("image_url") or part.get("url"))
                            parts.append({"type": "image_url", "image_url": image})
                            multimodal = True
                        elif ptype in ("input_audio", "audio", "input_video", "video"):
                            raise ValueError("audio and video inputs are not supported by this hub")
                if multimodal:
                    text = [({"type": "text", "text": p} if isinstance(p, str) else p)
                            for p in parts]
                else:
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
    if isinstance(content, list):   # content-parts -> join their text (never relay a list)
        content = "".join((p.get("text") or "") for p in content if isinstance(p, dict))
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


def _responses_stream(resp, model_label, line_iter=None, first=_MISSING, prompt_est=0):
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
            if isinstance(chunk, dict) and chunk.get("error"):
                break  # provider streamed an error object on a 200 -> stop cleanly,
                       # emit the terminal below (never relay the error as content)
            u = chunk.get("usage")
            if isinstance(u, dict) and (u.get("prompt_tokens") is not None
                                        or u.get("completion_tokens") is not None):
                usage = u
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = (choices[0] or {}).get("delta") or {}

            dtext = delta.get("content")
            if dtext is not None and not isinstance(dtext, str):
                # Some upstreams stream a non-string content delta (an int, or a
                # content-parts list). Coerce to str so `"".join(text_buf)` below
                # can't crash ("sequence item N: expected str instance, int found").
                dtext = "".join(
                    (p.get("text") or "") for p in dtext
                    if isinstance(p, dict)) if isinstance(dtext, list) else str(dtext)
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
                if args is not None and not isinstance(args, str):
                    args = str(args)  # tool-call args must be str for the join()
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
        try:
            hop_pid, _sep, hop_model = model_label.partition("/")
            if usage is not None:
                pt = int(usage.get("prompt_tokens") or 0)
                ct = int(usage.get("completion_tokens") or 0)
                usage_history.record(hop_pid, hop_model, pt, ct, estimated=False)
            else:
                usage_history.record(hop_pid, hop_model, prompt_est,
                                     len("".join(text_buf)) // 4, estimated=True)
        except Exception:
            pass
        resp.close()


@app.route("/v1/responses", methods=["POST"])
def v1_responses():
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return _openai_error("Invalid JSON body.", 400)
    try:
        messages = _responses_to_chat(body)
        messages, image_count = _normalize_openai_messages(messages)
    except Exception as exc:
        return _openai_error("Could not translate request: " + _sanitize(str(exc)), 400)
    if not messages:
        return _openai_error("No input to send.", 400)
    has_images = image_count > 0

    # Tools + size estimate up front (Codex sends huge tool schemas — they must
    # count toward routing so a big request doesn't land on a small-TPM provider).
    tools = _responses_tools_to_chat(body.get("tools"))
    est = _est_tokens(messages, tools)

    # Same routing as /v1/chat/completions: Auto/empty/claude-* -> difficulty
    # route across available, SIZE-CAPABLE providers; explicit '<pid>/<model>' bypasses.
    has_tools = bool(tools)
    diff = None
    if _is_orchestrate(body.get("model")):
        router = _route_for_vision if has_images else _route_by_difficulty
        pid, resolved, diff = router(messages, body.get("max_output_tokens"), est,
                                     require_tools=has_tools)
        if pid is None:
            if has_images:
                return _openai_error(
                    "No enabled verified vision model is available. Enable Google, "
                    "Cloudflare, or Z.AI with a usable vision model.", 400)
            pid, resolved = _resolve_model(body.get("model"))
    else:
        pid, resolved = _resolve_model(body.get("model"))
    if pid is None:
        return _openai_error(resolved, 400)
    if not prov.is_model_allowed(resolved):
        return _openai_error("Model '%s' is blocked by the safety filter." % resolved, 403,
                             "permission_error")
    if has_images and not _is_vision_model(pid, resolved):
        return _openai_error(
            "Model '%s/%s' is not a verified vision model." % (pid, resolved), 400)
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
    for hop_pid, hop_model in _build_chain(pid, resolved, est, require_vision=has_images,
                                           require_tools=has_tools):
        if not prov.is_model_allowed(hop_model):
            continue
        if stream and _is_sub(hop_pid):
            errors.append("%s: skipped (a local CLI cannot stream)" % hop_pid)
            continue
        payload = dict(base_payload)
        payload["model"] = hop_model
        _apply_reasoning_effort(payload, hop_model, diff)
        payload["stream"] = stream
        try:
            _act_pick(hop_pid, hop_model)
            resp = _dispatch_chat(hop_pid, payload, stream)
        except (requests.RequestException, RuntimeError) as exc:
            errors.append("%s: %s" % (hop_pid, _sanitize(exc.__class__.__name__)))
            continue
        if resp.status_code == 200:
            # Echo back the id the client ASKED for (codex sends "auto", which now has
            # config metadata) instead of the resolved "pid/model" — otherwise codex
            # re-warns "Model metadata for `pid/model` not found" on every response
            # (issue #21070). The real model that answered is still shown on the hub
            # dashboard + activity feed, so no transparency is lost.
            model_label = (body.get("model") or "").strip() or (hop_pid + "/" + hop_model)
            if stream:
                # #4: peek the first line BEFORE committing the 200 SSE stream so a
                # hung/slow provider falls through to the next hop instead of stalling.
                line_it = resp.iter_lines(decode_unicode=False)
                # Peek until REAL content (not just the first byte): an empty 200
                # (role delta + [DONE], no content) must fall through to the next
                # model instead of being streamed to codex as a dead-end answer.
                status, buffered = _peek_until_content(line_it, STREAM_CONTENT_PEEK_TIMEOUT)
                if status != "content":
                    errors.append("%s: %s (200 but no content)" % (hop_pid, status))
                    resp.close()
                    continue
                chained = _chain_buffered(buffered, line_it)
                return Response(stream_with_context(
                    _responses_stream(resp, model_label, line_iter=chained, prompt_est=est)),
                    mimetype="text/event-stream", headers=_SSE_HEADERS)
            try:
                data = resp.json()
            except (ValueError, requests.RequestException):
                errors.append("%s: non-JSON 200 body" % hop_pid)
                resp.close()
                continue
            if _chat_json_is_empty(data):
                errors.append("%s: empty (200 but no content)" % hop_pid)
                resp.close()
                continue
            _record_chat_usage(hop_pid, hop_model, data, est)
            return jsonify(_chat_to_responses(data, model_label)), 200
        try:
            errors.append("%s: HTTP %d" % (hop_pid, resp.status_code))
            if resp.status_code == 400 and _classify_soft_400(resp):
                resp.close()
                continue
            if not _retryable(resp.status_code):
                try:
                    body_json = resp.json()
                    body_text = None
                except ValueError:
                    body_json = None
                    body_text = _sanitize(resp.text)
                last_hard = {"pid": hop_pid, "status": resp.status_code,
                             "json": body_json, "text": body_text}
        except requests.RequestException as exc:
            errors.append("%s: %s reading error body" % (hop_pid, _sanitize(exc.__class__.__name__)))
        resp.close()
        continue
    # No provider yielded a 200. We have NOT emitted any SSE yet, so return a
    # normal non-200 JSON OpenAI-style error (Codex checks the HTTP status before
    # opening the event stream and surfaces this cleanly) rather than a fake 200
    # SSE stream carrying an error.
    # Chain exhausted. Tell the client HOW LONG until a model frees (Retry-After) so
    # its SDK waits out a short throttle and auto-continues once capacity returns.
    eta = _capacity_eta()
    if last_hard is not None:
        if last_hard["json"] is not None:
            return _with_retry_after(
                (jsonify(last_hard["json"]), _retryable_relay_status(last_hard["status"])), eta)
        return _with_retry_after(_openai_error(
            "Upstream returned non-JSON (%s, HTTP %d): %s"
            % (last_hard["pid"], last_hard["status"], last_hard["text"]), 503, "upstream_error"), eta)
    return _with_retry_after(_openai_error(
        "All providers failed: " + ("; ".join(errors) or "none available"), 503, "upstream_error"), eta)


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


def _anthropic_image_to_openai(block):
    source = block.get("source") if isinstance(block, dict) else None
    if not isinstance(source, dict):
        raise ValueError("Anthropic image.source must be an object")
    stype = source.get("type")
    if stype == "base64":
        mime = str(source.get("media_type") or "").lower()
        data = source.get("data")
        if mime not in _IMAGE_MIMES or not isinstance(data, str):
            raise ValueError("Anthropic base64 images need a supported media_type and data")
        value = "data:%s;base64,%s" % (mime, data)
    elif stype == "url":
        value = source.get("url")
    else:
        raise ValueError("unsupported Anthropic image source type '%s'" % stype)
    return {"type": "image_url", "image_url": _normalize_image_url(value)}


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
            content_parts = []
            has_image = False
            for block in rest:
                if isinstance(block, str):
                    content_parts.append({"type": "text", "text": block})
                elif isinstance(block, dict) and block.get("type") == "text":
                    content_parts.append({"type": "text", "text": block.get("text") or ""})
                elif isinstance(block, dict) and block.get("type") == "image":
                    content_parts.append(_anthropic_image_to_openai(block))
                    has_image = True
            text = "".join(p.get("text", "") for p in content_parts
                           if p.get("type") == "text")
            if content_parts or not tool_results:
                out.append({"role": "user", "content": content_parts if has_image else text})
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
    images = 0
    system = body.get("system")
    if system:
        total += len(_blocks_to_text(system))
    for msg in body.get("messages") or []:
        total += len(_blocks_to_text(msg.get("content")))
        content = msg.get("content")
        if isinstance(content, list):
            images += sum(1 for block in content
                          if isinstance(block, dict) and block.get("type") == "image")
    return max(1, total // 4 + images * 1000)


def _openai_resp_to_anthropic(data, model_str):
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = []
    text = msg.get("content")
    if isinstance(text, list):   # content-parts -> join their text (never relay a list)
        text = "".join((p.get("text") or "") for p in text if isinstance(p, dict))
    if text:
        content.append({"type": "text", "text": text})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        if not ((fn.get("name") or "").strip()):
            continue   # drop a nameless tool_call — never emit a blank-name tool_use
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        content.append({"type": "tool_use",
                        "id": tc.get("id") or ("toolu_" + uuid.uuid4().hex[:16]),
                        "name": fn.get("name"),
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


def _anthropic_stream(resp, model_str, input_tokens, line_iter=None, first=_MISSING,
                      hop_pid=None, hop_model=None):
    """Translate an upstream OpenAI SSE stream into the Anthropic event
    sequence: message_start -> content_block_start -> content_block_delta* ->
    content_block_stop -> message_delta -> message_stop. When `line_iter`/`first`
    are supplied (the first-byte peek already pulled the first line from this exact
    iterator) the pre-read line is processed first, then the rest of the SAME
    iterator — fast-path output is identical to before.

    `hop_pid`/`hop_model` (the REAL resolved provider/model, not the client-facing
    `model_str` -- Claude Code sends its own requested model string, which is not
    necessarily "pid/model") are used only to key usage_history recording."""
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
        real_out_tokens = None   # usage_history: only set from a REAL upstream usage object
        real_in_tokens = None
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
            if isinstance(chunk, dict) and chunk.get("error"):
                break  # error object on a 200 stream -> stop cleanly, emit terminal below
            usage = chunk.get("usage")
            if isinstance(usage, dict) and usage.get("completion_tokens") is not None:
                out_tokens = usage.get("completion_tokens")
                real_out_tokens = out_tokens
                if usage.get("prompt_tokens") is not None:
                    real_in_tokens = usage.get("prompt_tokens")
            choices = chunk.get("choices") or []
            if not choices:
                continue
            choice = choices[0] or {}
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}

            dtext = delta.get("content")
            if dtext is not None and not isinstance(dtext, str):
                # Coerce a non-string content delta (int / content-parts list) so
                # len(dtext) and the text_delta below can't crash — same upstream
                # quirk that broke the /v1/responses stream.
                dtext = "".join(
                    (p.get("text") or "") for p in dtext
                    if isinstance(p, dict)) if isinstance(dtext, list) else str(dtext)
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
                if args is not None and not isinstance(args, str):
                    args = str(args)
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
        try:
            if hop_pid and hop_model:
                if real_out_tokens is not None:
                    pt = real_in_tokens if real_in_tokens is not None else int(input_tokens or 0)
                    usage_history.record(hop_pid, hop_model, pt, int(real_out_tokens),
                                         estimated=False)
                else:
                    usage_history.record(hop_pid, hop_model, int(input_tokens or 0),
                                         int(out_tokens or (text_chars // 4)), estimated=True)
        except Exception:
            pass
        resp.close()


@app.route("/v1/messages", methods=["POST"])
def v1_messages():
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return _anthropic_error("invalid_request_error", "Invalid JSON body.", 400)
    try:
        oai_messages = _anthropic_to_openai_messages(body)
        oai_messages, image_count = _normalize_openai_messages(oai_messages)
    except Exception as exc:
        return _anthropic_error("invalid_request_error",
                                "Could not translate request: " + _sanitize(exc), 400)
    if not oai_messages:
        return _anthropic_error("invalid_request_error", "No messages to send.", 400)
    has_images = image_count > 0
    # Claude Code sends model 'claude-*' + a big system/tools payload -> orchestrate
    # by difficulty AND request size (skip small-TPM providers for large requests).
    tools = _anthropic_tools_to_openai(body.get("tools"))
    est = _est_tokens(oai_messages, tools)
    has_tools = bool(tools)
    diff = None
    if _is_orchestrate(body.get("model")):
        router = _route_for_vision if has_images else _route_by_difficulty
        pid, resolved, diff = router(oai_messages, body.get("max_tokens"), est,
                                     require_tools=has_tools)
        if pid is None:
            if has_images:
                return _anthropic_error(
                    "invalid_request_error",
                    "No enabled verified vision model is available. Enable Google, "
                    "Cloudflare, or Z.AI with a usable vision model.", 400)
            pid, resolved = _resolve_model(body.get("model"))
    else:
        pid, resolved = _resolve_model(body.get("model"))
    if pid is None:
        return _anthropic_error("invalid_request_error", resolved, 400)
    if not prov.is_model_allowed(resolved):
        return _anthropic_error("permission_error",
                                "Model '%s' is blocked by the safety filter." % resolved, 403)
    if has_images and not _is_vision_model(pid, resolved):
        return _anthropic_error(
            "invalid_request_error",
            "Model '%s/%s' is not a verified vision model." % (pid, resolved), 400)
    not_ready = _check_provider_ready(pid)
    if not_ready:
        return _anthropic_error("invalid_request_error", not_ready, 400)

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
    for hop_pid, hop_model in _build_chain(pid, resolved, est, require_vision=has_images,
                                           require_tools=has_tools):
        if not prov.is_model_allowed(hop_model):
            continue
        if stream and _is_sub(hop_pid):
            errors.append("%s: skipped (a local CLI cannot stream)" % hop_pid)
            continue
        payload = dict(base_payload)
        payload["model"] = hop_model
        _apply_reasoning_effort(payload, hop_model, diff)
        payload["stream"] = stream
        try:
            _act_pick(hop_pid, hop_model)
            resp = _dispatch_chat(hop_pid, payload, stream)
        except (requests.RequestException, RuntimeError) as exc:
            errors.append("%s: %s" % (hop_pid, _sanitize(exc.__class__.__name__)))
            continue
        if resp.status_code == 200:
            model_str = requested_model or (hop_pid + "/" + hop_model)
            if stream:
                # #4: peek the first line BEFORE committing the 200 SSE stream so a
                # hung/slow provider falls through to the next hop instead of stalling.
                line_it = resp.iter_lines(decode_unicode=False)
                # Peek until REAL content so an empty 200 falls through to the next
                # model instead of being handed to the client as a dead-end answer.
                status, buffered = _peek_until_content(line_it, STREAM_CONTENT_PEEK_TIMEOUT)
                if status != "content":
                    errors.append("%s: %s (200 but no content)" % (hop_pid, status))
                    resp.close()
                    continue
                chained = _chain_buffered(buffered, line_it)
                return Response(stream_with_context(
                    _anthropic_stream(resp, model_str, input_est, line_iter=chained,
                                     hop_pid=hop_pid, hop_model=hop_model)),
                    mimetype="text/event-stream", headers=_SSE_HEADERS)
            try:
                data = resp.json()
            except (ValueError, requests.RequestException):
                errors.append("%s: non-JSON 200 body" % hop_pid)
                resp.close()
                continue
            if _chat_json_is_empty(data):
                errors.append("%s: empty (200 but no content)" % hop_pid)
                resp.close()
                continue
            _record_chat_usage(hop_pid, hop_model, data, est)
            return jsonify(_openai_resp_to_anthropic(data, model_str))
        # Non-2xx. Retryable (429/5xx) AND hard errors (404/400/model-not-found)
        # both advance to the NEXT provider (a different provider/model) before we
        # surface an error; within-provider key rotation already ran upstream. A
        # network error reading the (unread, stream=True) error body must also just
        # advance, never escape the loop into a 500.
        try:
            errors.append("%s: HTTP %d" % (hop_pid, resp.status_code))
            if resp.status_code == 400 and _classify_soft_400(resp):
                resp.close()
                continue
            if not _retryable(resp.status_code):
                # Capture the last hard error's detail to relay once the chain is done.
                detail = _upstream_error_detail(resp)
                last_hard = {"pid": hop_pid, "http": resp.status_code,
                             "status": _retryable_relay_status(resp.status_code),
                             "detail": detail}
        except requests.RequestException as exc:
            errors.append("%s: %s reading error body" % (hop_pid, _sanitize(exc.__class__.__name__)))
        resp.close()
        continue
    # Chain exhausted -> Retry-After so the client waits out a short throttle + auto-continues.
    eta = _capacity_eta()
    if last_hard is not None:
        return _with_retry_after(_anthropic_error("api_error",
                                "Upstream %s error (HTTP %d): %s"
                                % (last_hard["pid"], last_hard["http"], last_hard["detail"]),
                                last_hard["status"]), eta)
    return _with_retry_after(_anthropic_error("api_error",
                            "All providers failed: " + ("; ".join(errors) or "none available"),
                            503), eta)


@app.route("/v1/messages/count_tokens", methods=["POST"])
def v1_count_tokens():
    """Rough estimate (chars/4) so Anthropic clients that pre-count don't 404."""
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return _anthropic_error("invalid_request_error", "Invalid JSON body.", 400)
    return jsonify({"input_tokens": _estimate_input_tokens(body)})


# ---------------------------------------------------------------------------
# Image generation (Text-to-Image) — a few free providers offer a genuinely
# free image-gen endpoint alongside their free chat models. NONE of these are
# OpenAI /chat/completions-compatible (each is a bespoke shape), so they get
# their own dispatch functions and their own /v1/images/generations route,
# separate from the chat gateway above. Deliberately NO Pillow/webp dependency
# added — the hub stays "Flask + requests only" (README's stated contract);
# images pass through as whatever bytes the provider returns, base64-encoded
# for the JSON response. `response_format` is always answered as `b64_json`
# regardless of what the caller asks for — there is no hosting here to hand
# back a real fetchable `url`, and a `data:` URI in the `url` field would
# break any client (including the real OpenAI SDK) that tries to GET it.
# ---------------------------------------------------------------------------
_IMAGE_PROVIDER_ORDER = ("cloudflare", "modelscope", "pollinations")
MAX_IMAGE_HOPS = 4              # bound worst-case latency (ModelScope polls up to 60s/hop)
_MODELSCOPE_POLL_DEADLINE = 60  # seconds


def _image_model_rows(pid):
    """Registry image-model rows ({'id','label',...}) for one provider."""
    p = prov.get_provider(pid) or {}
    return [m for m in (p.get("image_models") or []) if isinstance(m, dict) and m.get("id")]


def _image_candidates():
    """Available (pid, model_id) image-generation pairs, in priority order —
    same manual/auto shape as _vision_candidates(), backed by config's
    separate `images` CAS state (independent priority from vision/chat).

    The auto tail is INTERLEAVED across providers (best model of provider A,
    best of B, best of C, then each provider's 2nd, ...) rather than grouped
    by provider. MAX_IMAGE_HOPS bounds how many candidates a single request
    will try, and Cloudflare alone lists 4 image models — grouping would let
    every hop be spent on one broken/exhausted provider before a working,
    entirely different provider is ever reached.

    PAID image models (row["free"] is False) are deliberately EXCLUDED here —
    they never appear in auto/manual rotation and are reachable only via an
    explicit '<provider>/<model>' pin through _resolve_image_model, exactly
    like every paid CHAT provider already works in this hub. This is
    per-MODEL, not per-provider, because a provider can mix free and paid
    image models in the same row (e.g. google has both)."""
    by_provider = {}
    for pid in _available_providers():
        for row in _image_model_rows(pid):
            if not row.get("free", True):
                continue
            model = row["id"]
            if prov.is_model_allowed(model) and not _is_model_dead(pid, model):
                by_provider.setdefault(pid, []).append((pid, model))

    state = config.get_images_state()
    manual = state.get("manual_priority") if state.get("priority_mode") == "manual" else []
    ordered = []
    for item in manual or []:
        if "/" not in str(item):
            continue
        head, rest = str(item).split("/", 1)
        pair = (head, rest)
        lst = by_provider.get(head) or []
        if pair in lst:
            lst.remove(pair)
            ordered.append(pair)

    provider_order = sorted(by_provider.keys(),
                            key=lambda pid: _IMAGE_PROVIDER_ORDER.index(pid)
                            if pid in _IMAGE_PROVIDER_ORDER else 99)
    tail = []
    max_len = max((len(v) for v in by_provider.values()), default=0)
    for round_i in range(max_len):
        for pid in provider_order:
            models = by_provider.get(pid) or []
            if round_i < len(models):
                tail.append(models[round_i])
    return ordered + tail


def _resolve_image_model(model):
    """'<pid>/<model>' -> (pid, model). 'auto'/empty -> top _image_candidates()
    pick. Returns (pid, model) or (None, error_message)."""
    model = model.strip() if isinstance(model, str) else ""
    if "/" in model:
        head, rest = model.split("/", 1)
        if prov.get_provider(head) and any(row["id"] == rest for row in _image_model_rows(head)):
            return head, rest
        return None, "Unknown image model '%s'." % model
    candidates = _image_candidates()
    if not candidates:
        return None, ("No enabled provider offers free image generation yet. Enable "
                      "Cloudflare Workers AI, ModelScope, or Pollinations on the dashboard.")
    return candidates[0]


def _b64_bytes(raw_bytes):
    return base64.b64encode(raw_bytes).decode("ascii")


def _cf_account_id_for_image(pcfg, api_key):
    """Resolve the Cloudflare account id for the IMAGE endpoint (.../ai/run/
    {model}), honoring a user-pasted custom base URL FIRST — the same
    fallback _resolve_base_url already gives the chat-completions path when a
    narrowly-scoped token can't self-resolve via _cf_account_id. Without this,
    the documented workaround ("paste your account-scoped base URL") had zero
    effect here even though the error message told the user to do exactly
    that (found in review: image generation stayed permanently broken for
    such tokens while chat on the same token worked fine)."""
    custom = pcfg.get("base_url")
    if custom:
        match = re.search(r"/accounts/([^/]+)/ai", custom)
        if match:
            return match.group(1)
    return _cf_account_id(api_key)


def _cf_generate_image(pcfg, model, prompt, size=1024, steps=4):
    """Cloudflare Workers AI's NATIVE image endpoint (NOT /chat/completions):
    POST .../accounts/{account_id}/ai/run/{model}. flux-1-schnell returns JSON
    {"result":{"image": "<base64 png>"}}; SD-family models return raw PNG
    bytes directly. Returns (status, b64_or_None, error_detail_or_None)."""
    keys = pcfg.get("api_keys") or []
    if not keys:
        return 400, None, "no api key for provider cloudflare"
    api_key = keys[0]
    account_id = _cf_account_id_for_image(pcfg, api_key)
    if not account_id:
        return 400, None, ("could not resolve the Cloudflare account id from this token — "
                           "paste your account-scoped base URL into 'Advanced: custom base "
                           "URL' on the Cloudflare card")
    url = "https://api.cloudflare.com/client/v4/accounts/%s/ai/run/%s" % (account_id, model)
    try:
        resp = requests.post(
            url, headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
            json={"prompt": (prompt or "")[:2048], "steps": int(steps or 4)},
            timeout=(CONNECT_TIMEOUT, CHAT_READ_TIMEOUT))
    except requests.RequestException as exc:
        return 502, None, exc.__class__.__name__
    if resp.status_code != 200:
        return resp.status_code, None, _upstream_error_detail(resp)
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "application/json" in ctype:
        data = resp.json()
        if not data.get("success", True):
            return 502, None, _sanitize(str(data.get("errors")))
        b64 = (data.get("result") or {}).get("image")
        if not b64:
            return 502, None, "Cloudflare Workers AI returned no image data"
        return 200, b64, None
    if not resp.content:
        return 502, None, "Cloudflare Workers AI returned an empty body"
    return 200, _b64_bytes(resp.content), None


def _is_safe_external_url(url):
    """Lightweight SSRF guard for a URL the hub is about to fetch SERVER-SIDE
    on the strength of an upstream provider's own response (not user input
    directly) — e.g. ModelScope's task-result image URL. Review found the
    prior code fetched such a URL with no validation at all despite a comment
    claiming "no SSRF surface"; that claim only holds if the URL is actually
    checked. Blocks non-https schemes, embedded credentials, and any hostname
    that resolves to a private/loopback/link-local/reserved/multicast address
    (defends a misbehaving or compromised upstream, not just a malicious one)."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local \
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return True


def _modelscope_generate_image(pcfg, model, prompt, size=1024, steps=4):
    """ModelScope's async task API (NOT /chat/completions): POST
    /v1/images/generations with X-ModelScope-Async-Mode:true returns a
    task_id; poll /v1/tasks/{id} (X-ModelScope-Task-Type: image_generation)
    until SUCCEED, then download output_images[0]. The hub called ModelScope
    itself, but the returned URL still comes from that response's JSON body,
    so a misbehaving/compromised upstream could point it somewhere else —
    validated via _is_safe_external_url() before the download, same as this
    codebase already requires for a comparable provider-returned-URL fetch
    elsewhere. Returns (status, b64_or_None, error_detail)."""
    keys = pcfg.get("api_keys") or []
    if not keys:
        return 400, None, "no api key for provider modelscope"
    api_key = keys[0]
    base = "https://api-inference.modelscope.cn"
    try:
        resp = requests.post(
            base + "/v1/images/generations",
            headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json",
                     "X-ModelScope-Async-Mode": "true"},
            json={"model": model, "prompt": (prompt or "")[:2000]},
            timeout=(CONNECT_TIMEOUT, MODELS_READ_TIMEOUT))
    except requests.RequestException as exc:
        return 502, None, exc.__class__.__name__
    if resp.status_code not in (200, 201):
        return resp.status_code, None, _upstream_error_detail(resp)
    task_id = (resp.json() or {}).get("task_id")
    if not task_id:
        return 502, None, "ModelScope returned no task_id"
    deadline = time.time() + _MODELSCOPE_POLL_DEADLINE
    img_url = None
    while time.time() < deadline:
        time.sleep(2)
        try:
            pr = requests.get(base + "/v1/tasks/" + task_id,
                              headers={"Authorization": "Bearer " + api_key,
                                       "X-ModelScope-Task-Type": "image_generation"},
                              timeout=(CONNECT_TIMEOUT, MODELS_READ_TIMEOUT))
        except requests.RequestException:
            continue
        pd = pr.json() if pr.status_code == 200 else {}
        status = str(pd.get("task_status") or "").upper()
        if status in ("SUCCEED", "SUCCEEDED"):
            outs = pd.get("output_images") or (pd.get("output") or {}).get("images") or []
            img_url = outs[0] if outs else None
            break
        if status in ("FAILED", "FAIL", "ERROR"):
            return 502, None, "ModelScope task failed: %s" % _sanitize(str(pd), 200)
    if not img_url:
        return 504, None, "ModelScope task timed out or returned no image"
    if not _is_safe_external_url(img_url):
        return 502, None, "ModelScope returned an unsafe image URL"
    try:
        img_resp = requests.get(img_url, timeout=(CONNECT_TIMEOUT, 30))
    except requests.RequestException as exc:
        return 502, None, "Could not download the generated image: %s" % exc.__class__.__name__
    if img_resp.status_code != 200 or not img_resp.content:
        return 502, None, "Could not download the generated image (HTTP %d)" % img_resp.status_code
    return 200, _b64_bytes(img_resp.content), None


def _parse_wh(size, default=1024):
    """'WIDTHxHEIGHT' -> (width, height), each clamped to [256, 1536].
    Falls back to a square `default` for anything unparseable. A single
    scalar in, squared for both dimensions, was the prior bug here — a
    'portrait' pick silently became a square image."""
    width = height = default
    if isinstance(size, str) and "x" in size.lower():
        parts = size.lower().split("x")
        try:
            width = max(256, min(1536, int(parts[0])))
            height = max(256, min(1536, int(parts[1]))) if len(parts) > 1 else width
        except (ValueError, IndexError):
            width = height = default
    return width, height


def _pollinations_generate_image(pcfg, model, prompt, size=1024, steps=4):
    """Pollinations' anonymous GET-URL image API (NOT /chat/completions): GET
    image.pollinations.ai/prompt/{prompt}?... -> raw image bytes. No key
    required; an optional saved key/token just lifts limits."""
    keys = pcfg.get("api_keys") or []
    api_key = keys[0] if keys else None
    width, height = _parse_wh(size)
    query = "width=%d&height=%d&model=%s&nologo=true&seed=%d" % (
        width, height, quote(model or "flux", safe=""), random.randint(1, 1_000_000))
    url = "https://image.pollinations.ai/prompt/%s?%s" % (
        quote((prompt or "")[:1500], safe=""), query)
    headers = {"Authorization": "Bearer " + api_key} if api_key else {}
    try:
        resp = requests.get(url, headers=headers, timeout=(CONNECT_TIMEOUT, 90))
    except requests.RequestException as exc:
        return 502, None, exc.__class__.__name__
    if resp.status_code != 200 or not resp.content:
        status = resp.status_code if resp.status_code != 200 else 502
        return status, None, _sanitize(resp.text or "empty body", 300)
    return 200, _b64_bytes(resp.content), None


# --------------------------------------------------------------------------- #
# PAID image generators — OpenAI, Google (Gemini image), OpenRouter, Higgsfield.
# Every model these dispatch is registered with "free": False in providers.py,
# so _image_candidates() (per-model "free" filter) never auto/manual-routes to
# them — reachable ONLY via an explicit "<provider>/<model>" pin through
# _resolve_image_model, same as this hub's existing paid CHAT providers
# (deepseek/kimi/minimax): enabled+keyed but excluded from auto, usable only
# when explicitly named. Same (status, b64_or_None, error_detail_or_None)
# return convention as the free generators above.
# --------------------------------------------------------------------------- #

def _openai_generate_image(pcfg, model, prompt, size="1024x1024", steps=4):
    """OpenAI's real Images API: POST /v1/images/generations. Standard,
    stable, well-documented REST shape (model, prompt, n, size) ->
    {"data":[{"b64_json":...} or {"url":...}]}."""
    keys = pcfg.get("api_keys") or []
    if not keys:
        return 400, None, "no api key for provider openai"
    api_key = keys[0]
    valid_sizes = {"1024x1024", "1536x1024", "1024x1536"}
    body = {"model": model, "prompt": (prompt or "")[:32000], "n": 1,
            "size": size if size in valid_sizes else "1024x1024"}
    try:
        resp = requests.post(
            "https://api.openai.com/v1/images/generations",
            headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
            json=body, timeout=(CONNECT_TIMEOUT, CHAT_READ_TIMEOUT))
    except requests.RequestException as exc:
        return 502, None, exc.__class__.__name__
    if resp.status_code != 200:
        return resp.status_code, None, _upstream_error_detail(resp)
    data = (resp.json() or {}).get("data") or []
    if not data:
        return 502, None, "OpenAI returned no image data"
    item = data[0]
    b64 = item.get("b64_json")
    if b64:
        return 200, b64, None
    url = item.get("url")
    if url and _is_safe_external_url(url):
        try:
            img = requests.get(url, timeout=(CONNECT_TIMEOUT, 30))
        except requests.RequestException as exc:
            return 502, None, "could not download image: %s" % exc.__class__.__name__
        if img.status_code == 200 and img.content:
            return 200, _b64_bytes(img.content), None
    return 502, None, "OpenAI returned neither b64_json nor a fetchable url"


def _google_generate_image(pcfg, model, prompt, size="1024x1024", steps=4):
    """Gemini's generateContent REST endpoint with responseModalities:["IMAGE"]
    -- the standard, stable Gemini REST shape (verified against
    ai.google.dev/api/generate-content): response image bytes appear at
    candidates[].content.parts[].inlineData.data."""
    keys = pcfg.get("api_keys") or []
    if not keys:
        return 400, None, "no api key for provider google"
    api_key = keys[0]
    url = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % model
    body = {"contents": [{"parts": [{"text": (prompt or "")[:8000]}]}],
            "generationConfig": {"responseModalities": ["IMAGE"]}}
    try:
        resp = requests.post(url, params={"key": api_key},
                             headers={"Content-Type": "application/json"}, json=body,
                             timeout=(CONNECT_TIMEOUT, CHAT_READ_TIMEOUT))
    except requests.RequestException as exc:
        return 502, None, exc.__class__.__name__
    if resp.status_code != 200:
        return resp.status_code, None, _upstream_error_detail(resp)
    data = resp.json() or {}
    for cand in data.get("candidates") or []:
        for part in (cand.get("content") or {}).get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                return 200, inline["data"], None
    return 502, None, "Google returned no image data"


def _openrouter_generate_image(pcfg, model, prompt, size="1024x1024", steps=4):
    """OpenRouter's image-capable chat/completions: modalities:["image"] on
    a normal chat request; the image comes back in
    choices[0].message.images[].image_url.url, either a data: URI or a real
    fetchable URL depending on the model."""
    keys = pcfg.get("api_keys") or []
    if not keys:
        return 400, None, "no api key for provider openrouter"
    api_key = keys[0]
    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
            json={"model": model, "modalities": ["image"],
                  "messages": [{"role": "user", "content": prompt or ""}]},
            timeout=(CONNECT_TIMEOUT, CHAT_READ_TIMEOUT))
    except requests.RequestException as exc:
        return 502, None, exc.__class__.__name__
    if resp.status_code != 200:
        return resp.status_code, None, _upstream_error_detail(resp)
    data = resp.json() or {}
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    for img in (msg.get("images") or []):
        url = (img.get("image_url") or {}).get("url") if isinstance(img, dict) else None
        if isinstance(url, str) and url.startswith("data:"):
            try:
                return 200, url.split(",", 1)[1], None
            except IndexError:
                continue
        if url and _is_safe_external_url(url):
            try:
                r2 = requests.get(url, timeout=(CONNECT_TIMEOUT, 30))
            except requests.RequestException:
                continue
            if r2.status_code == 200 and r2.content:
                return 200, _b64_bytes(r2.content), None
    return 502, None, "OpenRouter returned no image data"


def _higgsfield_generate_image(pcfg, model, prompt, size="1024x1024", steps=4):
    """Higgsfield's bespoke API (NOT OpenAI-compatible): composite credential
    "Authorization: Key <id>:<secret>", async submit (model-specific endpoint
    path) -> request_id -> poll /requests/{id}/status until completed, then
    fetch images[].url. The composite credential is stored as a single
    "KEY_ID:KEY_SECRET" string in the existing api_keys slot -- split it here."""
    keys = pcfg.get("api_keys") or []
    if not keys or ":" not in keys[0]:
        return 400, None, "Higgsfield needs a KEY_ID:KEY_SECRET credential (paste both, colon-separated)"
    key_id, key_secret = keys[0].split(":", 1)
    endpoints = {
        "higgsfield/text2image/soul": "/v1/text2image/soul",
        "flux-pro/kontext/max/text-to-image": "/v1/flux-pro/kontext/max/text-to-image",
        "bytedance/seedream/v4/text-to-image": "/v1/bytedance/seedream/v4/text-to-image",
        "higgsfield/nano-banana-pro": "/v1/text2image/nano-banana-pro",
    }
    endpoint = endpoints.get(model, "/v1/text2image/soul")
    base = "https://platform.higgsfield.ai"
    headers = {"Authorization": "Key %s:%s" % (key_id, key_secret), "Content-Type": "application/json"}
    width, height = _parse_wh(size)
    body = {"prompt": (prompt or "")[:2000], "width_and_height": "%dx%d" % (width, height), "batch_size": 1}
    try:
        resp = requests.post(base + endpoint, headers=headers, json=body,
                             timeout=(CONNECT_TIMEOUT, 60))
    except requests.RequestException as exc:
        return 502, None, exc.__class__.__name__
    if resp.status_code not in (200, 201, 202):
        return resp.status_code, None, _upstream_error_detail(resp)
    sj = resp.json() if resp.content else {}
    rid = sj.get("request_id") or sj.get("id") or (sj.get("data") or {}).get("id")
    if not rid:
        return 502, None, "Higgsfield returned no request id"
    deadline = time.time() + 90
    img_url = None
    while time.time() < deadline:
        time.sleep(2)
        try:
            pr = requests.get(base + "/requests/" + rid + "/status", headers=headers,
                              timeout=(CONNECT_TIMEOUT, 30))
        except requests.RequestException:
            continue
        pd = pr.json() if pr.status_code == 200 else {}
        st = str(pd.get("status") or "").lower()
        if st in ("completed", "succeeded", "success"):
            imgs = pd.get("images") or (pd.get("result") or {}).get("images") or []
            if imgs:
                first = imgs[0]
                img_url = first.get("url") if isinstance(first, dict) else first
            break
        if st in ("failed", "nsfw", "cancelled", "error"):
            return 502, None, "Higgsfield task %s" % st
    if not img_url:
        return 504, None, "Higgsfield timed out or returned no image"
    if not _is_safe_external_url(img_url):
        return 502, None, "Higgsfield returned an unsafe image URL"
    try:
        img = requests.get(img_url, timeout=(CONNECT_TIMEOUT, 60))
    except requests.RequestException as exc:
        return 502, None, "could not download image: %s" % exc.__class__.__name__
    if img.status_code != 200 or not img.content:
        return 502, None, "could not download image (HTTP %d)" % img.status_code
    return 200, _b64_bytes(img.content), None


_IMAGE_GENERATORS = {
    "cloudflare": _cf_generate_image,
    "modelscope": _modelscope_generate_image,
    "pollinations": _pollinations_generate_image,
    "openai": _openai_generate_image,
    "google": _google_generate_image,
    "openrouter": _openrouter_generate_image,
    "higgsfield": _higgsfield_generate_image,
}


def _save_generated_image(b64, prompt, pid, model):
    """Best-effort persist of one successful generation for the history
    gallery. Never raises -- a history-tracking bug must never break a real
    image-generation response."""
    try:
        image_history.save(base64.b64decode(b64), prompt, pid, model)
    except Exception:
        pass


@app.route("/v1/images/generations", methods=["POST"])
def v1_images_generations():
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return _openai_error("Invalid JSON body.", 400)
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _openai_error("'prompt' is required.", 400)
    try:
        n = max(1, min(4, int(body.get("n") or 1)))
    except (TypeError, ValueError):
        n = 1
    # Kept as the original "WxH" string (not pre-parsed into one scalar) so
    # each generator decides for itself what to do with it: Cloudflare/
    # ModelScope don't accept a size param at all and ignore it; Pollinations
    # (the one provider that does) parses width/height independently via
    # _parse_wh() so a non-square pick isn't silently squared.
    size = body.get("size") if isinstance(body.get("size"), str) else "1024x1024"
    try:
        steps = max(1, min(8, int(body.get("steps") or 4)))
    except (TypeError, ValueError):
        steps = 4

    requested = body.get("model") if isinstance(body.get("model"), str) else "auto"
    pid, model = _resolve_image_model(requested)
    if pid is None:
        return _openai_error(model, 400)
    not_ready = _check_provider_ready(pid)
    if not_ready:
        return _openai_error(not_ready, 400)

    tried = {(pid, model)}
    chain = [(pid, model)] + [c for c in _image_candidates() if c not in tried]
    errors = []
    images_b64 = []
    landed_pid = landed_model = None
    for hop_pid, hop_model in chain[:MAX_IMAGE_HOPS]:
        generator = _IMAGE_GENERATORS.get(hop_pid)
        if not generator:
            continue
        pcfg = config.get_provider_config(hop_pid)
        try:
            status, b64, detail = generator(pcfg, hop_model, prompt, size=size, steps=steps)
        except (requests.RequestException, RuntimeError) as exc:
            errors.append("%s: %s" % (hop_pid, _sanitize(exc.__class__.__name__)))
            continue
        quota.record(hop_pid, hop_model)
        if status == 200 and b64:
            images_b64.append(b64)
            landed_pid, landed_model = hop_pid, hop_model
            _save_generated_image(b64, prompt, hop_pid, hop_model)
            break
        if status == 429:
            # Unlike _upstream_chat, these generators don't surface a
            # Retry-After header through their (status, b64, detail) return
            # shape, so assume the same short per-minute-burst cooldown the
            # chat path uses when no Retry-After is present. Without this the
            # rate-limited provider stayed top-ranked and got retried again on
            # the very next request instead of cooling down.
            quota.mark_throttled(hop_pid, 60)
        if status in _DEAD_STATUSES:
            _mark_model_dead(hop_pid, hop_model, status)
        errors.append("%s: HTTP %s %s" % (hop_pid, status, _sanitize(detail or "")))
    if not images_b64:
        return _openai_error(
            "All image providers failed: " + ("; ".join(errors) or "none available"),
            502, "upstream_error")

    # n > 1: reuse the SAME confirmed-working hop for the rest — no need to
    # re-run the fallback chain once we know this hop actually answers.
    generator = _IMAGE_GENERATORS[landed_pid]
    pcfg = config.get_provider_config(landed_pid)
    for _extra in range(n - 1):
        try:
            status, b64, _detail = generator(pcfg, landed_model, prompt, size=size, steps=steps)
        except (requests.RequestException, RuntimeError):
            break
        quota.record(landed_pid, landed_model)
        if status == 200 and b64:
            images_b64.append(b64)
        else:
            break

    return jsonify({
        "created": int(time.time()),
        "model": landed_pid + "/" + landed_model,
        "data": [{"b64_json": b} for b in images_b64],
    }), 200


def _images_payload():
    state = config.get_images_state()
    models = []
    for p in prov.list_providers():
        pid = p["id"]
        pcfg = config.get_provider_config(pid)
        for row in _image_model_rows(pid):
            model = row["id"]
            models.append({
                "id": pid + "/" + model,
                "provider": pid,
                "model": model,
                "provider_name": p.get("name") or pid,
                "label": row.get("label") or model,
                "text_in_image": row.get("text_in_image"),
                "configured": bool(pcfg.get("enabled") and
                                   (pcfg.get("api_key") or not _needs_key(pid))),
                "dead": _is_model_dead(pid, model),
            })
    available_order = [pid + "/" + model for pid, model in _image_candidates()]
    return {"state": state, "models": models, "effective_priority": available_order}


@app.route("/api/images", methods=["GET", "POST"])
def api_images():
    if request.method == "GET":
        return jsonify(_images_payload())
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "invalid JSON body"}), 400
    if "revision" not in body:
        return jsonify({"error": "revision is required"}), 400
    mode = body.get("priority_mode")
    if mode not in ("auto", "manual"):
        return jsonify({"error": "priority_mode must be 'auto' or 'manual'"}), 400
    manual = body.get("manual_priority", [])
    if not isinstance(manual, list) or any(not isinstance(v, str) for v in manual):
        return jsonify({"error": "manual_priority must be an array of model ids"}), 400
    valid = {p["id"] + "/" + row["id"] for p in prov.list_providers()
             for row in _image_model_rows(p["id"])}
    unknown = [value for value in manual if value not in valid]
    if unknown:
        return jsonify({"error": "unknown image model(s): " + ", ".join(unknown)}), 400
    deduped = []
    for value in manual:
        if value not in deduped:
            deduped.append(value)

    def _update(state):
        state["priority_mode"] = mode
        state["manual_priority"] = deduped if mode == "manual" else []
        return state

    try:
        config.update_images_state(body["revision"], _update)
    except config.RevisionConflict as exc:
        return jsonify({"error": "images state changed; reload and retry",
                        "current_revision": exc.current_revision,
                        "state": config.get_images_state()}), 409
    return jsonify(_images_payload())


@app.route("/api/images/history", methods=["GET"])
def api_images_history():
    """Metadata for previously generated images (newest first), no image
    bytes -- the dashboard gallery fetches each thumbnail separately via
    GET /api/images/history/<id>, so listing many entries here stays cheap."""
    return jsonify({"images": image_history.list_entries()})


@app.route("/api/images/history/<image_id>", methods=["GET"])
def api_images_history_file(image_id):
    """Raw bytes of one generated image, for <img src> use. Auth still goes
    through the normal /api/* control-token guard -- an <img> tag can't set a
    custom header, so this relies on the guard's existing '?token=' query-
    param fallback (already used elsewhere in this file for the same reason)."""
    raw, mime_type = image_history.get_file(image_id)
    if raw is None:
        return jsonify({"error": "not found"}), 404
    return Response(raw, mimetype=mime_type or "image/png")


@app.route("/api/images/history/<image_id>", methods=["DELETE"])
def api_images_history_delete(image_id):
    if not image_history.delete(image_id):
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


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


# Auto-update pulls and then os.execv's freshly fetched code, so an `origin`
# repointed to an untrusted fork (accidentally or by a compromised dependency/
# setup step) would otherwise run arbitrary code as this user. This does not
# defend against the upstream repo itself being compromised — only against
# a wrong/hostile remote — the accepted residual risk is documented in
# security_best_practices_report.md (SEC-002).
_TRUSTED_REMOTE_RE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:)last-million/free-llm-hub(?:\.git)?/?$",
    re.IGNORECASE)


def _origin_is_trusted():
    rc, url, _ = _git("remote", "get-url", "origin")
    return rc == 0 and bool(_TRUSTED_REMOTE_RE.match(url.strip()))


def _hub_mode_is_off():
    """True only when the user has DELIBERATELY stood the hub down as the machine
    default (a completed hub-mode 'off' transition: desired=='off' AND phase=='off').
    A fresh/never-managed config defaults to desired='on'/phase='unmanaged', so this
    stays False there and leaves always-on auto-update unaffected. Used so auto-update
    won't silently re-exec (respawn) a hub the user just switched off."""
    try:
        st = config.get_hub_mode_state() or {}
    except Exception:
        return False
    return (str(st.get("desired") or "").lower() == "off"
            and str(st.get("phase") or "").lower() == "off")


def _auto_update_enabled():
    """ALWAYS ON. Auto-update is not a user-facing option: the hub keeps itself
    current (git pull every AUTO_UPDATE_INTERVAL_HOURS, default 5) and restarts
    only when the pull actually brought new commits. It is inherently safe —
    it skips a dirty tree, is a no-op outside a git checkout, and never touches
    ~/.free-llm-hub (keys/config live outside the repo), so there is nothing for
    a user to opt out of.

    `AUTO_UPDATE=0` remains ONLY as a developer escape hatch (used by the test
    harness to keep a pinned checkout from restarting mid-run). The old
    `auto_update` config flag is deliberately ignored — it is no longer written
    or read by the dashboard."""
    env = os.environ.get("AUTO_UPDATE")
    if env is not None:
        return env.strip().lower() not in ("0", "false", "no", "off", "")
    return True


def _do_update_check():
    """One pull cycle: skip if dirty, pull --ff-only, re-exec if HEAD moved.
    Returns a short human status string (also stored in _auto_update_state)."""
    with _auto_update_lock:
        _auto_update_state["last_check"] = int(time.time())
        if not _is_git_repo():
            _auto_update_state["last_result"] = "not a git repo — auto-update off"
            return _auto_update_state["last_result"]
        if not _origin_is_trusted():
            _auto_update_state["last_result"] = (
                "skipped: 'origin' is not the trusted last-million/free-llm-hub repo")
            _log.warning("Auto-update: refusing to pull — origin remote is untrusted.")
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
            # New commits pulled. Normally we re-exec to apply them — but if the user
            # has deliberately switched the hub OFF as the default (hub-mode off), respect
            # that "leave me stood down" intent and do NOT respawn the process; the update
            # applies on the next manual restart instead.
            if _hub_mode_is_off():
                _auto_update_state["last_result"] = (
                    "updated %s->%s — restart deferred (hub switched off as default)"
                    % (before[:7], after[:7]))
                _log.info("Auto-update: pulled %s->%s but hub-mode is off; deferring re-exec.",
                         before[:7], after[:7])
                return _auto_update_state["last_result"]
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
    """GET -> current state (diagnostics only; the dashboard no longer shows it).
    POST {check:true} -> run one update cycle now (may restart on new commits).

    There is NO enable/disable: auto-update is always on (see
    _auto_update_enabled). A POST carrying {enabled:...} is accepted but ignored
    so an older cached dashboard can't silently turn it off; the response's
    `enabled` always reflects the truth."""
    if request.method == "POST":
        body = request.get_json(force=True, silent=True) or {}
        if body.get("check"):
            threading.Thread(target=_do_update_check, daemon=True).start()
    st = dict(_auto_update_state)
    st["enabled"] = _auto_update_enabled()   # always True (unless the dev env escape)
    st["always_on"] = True
    st["is_git_repo"] = _is_git_repo()
    return jsonify(st)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def _print_banner():
    key = config.get_local_api_key()
    control_token = config.ensure_control_token()
    snippets = _connect_snippets()
    line = "=" * 74
    print(line)
    print("  Calvoun Free LLM Hub -- local gateway for free LLM providers")
    print(line)
    print("  Dashboard:   http://%s:%d/" % (HOST, PORT))
    print("  OpenAI API:  http://%s:%d/v1  (chat/completions, models)" % (HOST, PORT))
    print("  Anthropic:   http://%s:%d/v1/messages  (Claude Code compatible)" % (HOST, PORT))
    if key:
        print("  Local key:   SET (required on /v1/* as Bearer or x-api-key)")
    else:
        print("  Local key:   not set -- /v1/* is open on localhost")
    print(line)
    print("  Control token (paste into the dashboard once, first load):")
    print("    " + control_token)
    print("  This gates /api/* (dashboard config, hub mode, shutdown). It is")
    print("  stored in config.json (0600) and never sent to any provider.")
    print(line)
    print("  Connect Claude Code:")
    for ln in snippets["claude_code"].splitlines():
        print("    " + ln)
    print("  Connect OpenAI-compatible CLIs (aider, opencode, ...):")
    for ln in snippets["openai"].splitlines():
        print("    " + ln)
    print(line)


def _mark_runtime_started():
    config.clear_intentional_stop()
    for _attempt in range(3):
        state = config.get_runtime_state()

        def _running(value):
            value.update({"desired": "running", "phase": "running",
                          "shutdown_requested_at": None, "last_error": None})
            return value

        try:
            config.update_runtime_state(state["revision"], _running)
            return
        except config.RevisionConflict:
            continue


if __name__ == "__main__":
    from werkzeug.serving import make_server

    _recover_interrupted_hub_transition()
    _mark_runtime_started()
    _bootstrap_no_key_providers()  # no-key providers have nothing to configure -> on
    _print_banner()
    _start_auto_update()
    vision_status.start_heartbeat()
    server = make_server(HOST, PORT, app, threaded=True)
    _runtime_server[0] = server
    try:
        server.serve_forever()
    finally:
        _runtime_server[0] = None
        server.server_close()
