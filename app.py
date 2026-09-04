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

import hashlib
import hmac
import base64
import binascii
import io
import calendar
import concurrent.futures
import copy
import ipaddress
import json
import math
import mimetypes
import os
import platform
import random
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile
from urllib.parse import quote, urlsplit

import requests
from flask import (Flask, Response, g, jsonify, make_response, render_template,
                   request, send_file, stream_with_context)

try:
    from jinja2 import TemplateNotFound
except Exception:  # pragma: no cover - jinja2 always ships with flask
    class TemplateNotFound(Exception):
        pass

import agentic_chat
import agentic_history
import antigravity
import secretstore
import respcache
import tool_rescue
import wire_gemini
import wire_ollama
import model_categories
import snapshots
import quick_history
import config
import image_history
import craft
import perfstats
import providers as prov
import quota
import workspace
import swarm
import crews
import hub_mcp
import mcp_manager
import usage_history

# The hub is also an MCP server (POST /mcp, JSON-RPC 2.0) so any MCP-capable
# agent CLI can call the crews as native tools. The runner goes through the
# exact same pipeline as the /v1/* crew model ids. _swarm_dispatch is defined
# far below in this file, which is fine -- the lambda only resolves it at CALL
# time, not at import time.
hub_mcp.init(lambda messages, crew_name: crews.format_answer(
    crews.run(messages, _swarm_dispatch, crew_name)))
import vision_status

import logging
import traceback as _traceback

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
_log = logging.getLogger("free-llm-hub")

# Windows' registry has no .webp entry, so Flask served the logo/favicon as
# application/octet-stream -- and because we also send X-Content-Type-Options:
# nosniff (see _security_headers), the browser REFUSES to render an image with
# that type. Register it before the app is created.
mimetypes.add_type("image/webp", ".webp")

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
#     between chunks. Only guards against a genuinely dead connection: the decision
#     to abandon a SLOW-but-alive provider in favor of a fallback hop happens
#     separately and much sooner, via STREAM_CONTENT_PEEK_TIMEOUT below (an
#     independent thread-join, not this socket timeout) — once that peek has
#     already committed the 200 to the client there is no fallback left to gain by
#     cutting a still-working heavy reasoning model off early, so this is set close
#     to CHAT_READ_TIMEOUT rather than far below it (a heavy reasoning model can go
#     genuinely idle for well over a minute between the plan text and its first tool
#     call on a large Codex-style prompt — confirmed via hub.err.log 'Read timed
#     out' entries that lined up exactly with real builds stopping after the plan
#     with zero tool calls, at the old 90s ceiling).
STREAM_FIRST_BYTE_TIMEOUT = 25   # seconds
# Bound for the "peek until real content" look-ahead that tells a genuine answer
# from an empty 200 (some free providers return 200 then stream only a role delta +
# [DONE]). An empty stream reaches its terminal FAST (well under this), and a normal
# model emits content within a few seconds; this ceiling only bites a stream that
# goes idle without ever producing content, which then falls through to the next model.
STREAM_CONTENT_PEEK_TIMEOUT = 35  # seconds
# ADAPTIVE first-content budget (see _stream_peek_timeout): the flat 35s peek
# killed HEALTHY hops — a big reasoning model on a loaded free host can
# legitimately think longer than 35s before its first SSE byte on a Codex-sized
# prompt, and abandoning it degraded the chain onto the last-resort families.
# Slow/reasoning models (_SLOW_MODEL_RE) and large requests get a longer budget;
# fast models on small requests keep the snappy ceiling above.
STREAM_SLOW_PEEK_TIMEOUT = 60       # seconds — slow model OR big request
STREAM_SLOW_BIG_PEEK_TIMEOUT = 90   # seconds — slow model AND big request
STREAM_BIG_REQUEST_TOKENS = 12000   # est tokens at which a request is "big"
STREAM_IDLE_TIMEOUT = 280        # seconds
MODELS_READ_TIMEOUT = 10      # seconds (model discovery / key tests)
MODEL_CACHE_TTL = 60          # seconds

# Default cooldown for a 429/timeout with no provider-given Retry-After. USER-
# REPORTED live 2026-08-03: the same just-429'd g4f-gemini model kept getting
# retried attempt after attempt -- the previous 60s default was shorter than a
# real full chain-exhaustion pass (measured 150-430s that same session, mostly
# from OTHER slow/timing-out hops earlier in the same chain), so by the time
# the next attempt's chain was built, the 60s memory of "this just failed" had
# already expired and the model looked fresh again. A cooldown that expires
# before the next real retry even happens is not a cooldown. 180s comfortably
# outlasts the short end of what was measured and meaningfully cuts the
# repeat-hit rate on the long end too; still short enough that a genuinely
# recovered model isn't excluded for long. Reasoned from that one session's
# measurements, not a vendor-confirmed number -- self-correcting either way,
# since this only ever sidelines a model temporarily.
_HOP_COOLDOWN_DEFAULT = 180    # seconds
MAX_HOPS = 6                  # primary + up to 5 fallback models (across providers)
# Agentic (tool-calling) requests — Codex / Claude Code / hermes / openclaw loops —
# hammer the gateway far harder than a one-shot chat, so the small tool-capable pool
# throttles together in bursts. Give that path a DEEPER fallback so a transient
# multi-provider throttle window reaches the still-fresh models instead of 503-ing.
TOOLS_MAX_HOPS = 10
# Providers that enforce their free rate limit PER MODEL (not per account): one
# model's per-minute 429 must NOT bench the whole provider — its sibling models each
# keep their own budget. Google is 15 RPM *per model* (measured, see memory). Benching
# all of Google on one gemini's burst is what thinned the agentic pool into a 503.
_PER_MODEL_RATE_LIMIT_PROVIDERS = {"google"}

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


# Field names that state a model's INPUT context window, verified live against
# real catalogs 2026-07-31: openrouter -> context_length (1048576), groq ->
# context_window AND context_length (4000 on its smallest model), puter ->
# context (131072). Deliberately EXCLUDES max_completion_tokens /
# max_output_length / max_tokens: those bound the REPLY, and treating an output
# cap as the input window would over-compact every conversation.
_CTX_FIELDS = ("context_length", "context_window", "context",
               "max_context_length", "max_input_tokens", "context_size")
_CTX_SANE_MIN, _CTX_SANE_MAX = 1000, 5_000_000


def _learn_ctx_from_catalog(payload_pid, payload):
    """Record every context window a provider's /models catalog publishes.

    This is the PROACTIVE half of context handling: _learn_context_limit only
    fires after a request has already failed, which costs a real hop on the best
    model in the chain (measured: a Codex session lost its top hop on every turn
    to a 32k model the router believed was 120k). A catalog states the same fact
    for free, before anything is sent. Never raises."""
    try:
        items = payload
        if isinstance(payload, dict):
            for k in ("data", "models", "result"):
                v = payload.get(k)
                if isinstance(v, dict):
                    v = v.get("models")
                if isinstance(v, list):
                    items = v
                    break
        if not isinstance(items, list):
            return
        found = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            mid = it.get("id") or it.get("name") or it.get("model")
            if not isinstance(mid, str) or not mid:
                continue
            ctx = None
            for f in _CTX_FIELDS:
                v = it.get(f)
                # openrouter nests a second copy under top_provider
                if v is None and f == "context_length":
                    v = (it.get("top_provider") or {}).get("context_length") \
                        if isinstance(it.get("top_provider"), dict) else None
                if isinstance(v, bool):
                    continue
                if isinstance(v, (int, float)) and _CTX_SANE_MIN <= v <= _CTX_SANE_MAX:
                    ctx = int(v)
                    break
            if ctx is None:
                continue
            with _model_max_input_lock:
                cur = _MODEL_MAX_INPUT.get((payload_pid, mid))
                # Smaller wins: a limit learned from a real rejection is
                # authoritative over an optimistic catalog number.
                _MODEL_MAX_INPUT[(payload_pid, mid)] = min(cur, ctx) if cur else ctx
            found += 1
        if found:
            _log.debug("[ctx] %s: learned %d context windows from its catalog",
                       payload_pid, found)
    except Exception:                                                # noqa: BLE001
        _log.debug("[ctx] catalog harvest failed for %s", payload_pid, exc_info=True)


_PRICE_FIELDS = ("prompt", "completion", "input", "output",
                 "input_cost_per_token", "output_cost_per_token",
                 "prompt_price", "completion_price")


def _zero_priced_ids(payload):
    """Ids in a catalog whose published price is ZERO, for 'pricing_zero' providers.

    Without this, is_free_model() fails closed for that filter (it cannot prove
    free-ness with no pricing to look at), live_free comes back empty on every
    fetch, and those providers are pinned to their hand-written
    default_free_models forever — so a newly launched free model never appears
    until someone edits providers.py by hand.

    Prices arrive as strings ("0", "0.0000001") or numbers, nested under
    `pricing` (OpenRouter) or flat on the row. A row with NO recognisable price
    field is left out: unknown must not read as free, or a paid model gets
    routed as if it cost nothing."""
    out = []
    for item in (payload.get("data") or payload.get("models") or []) \
            if isinstance(payload, dict) else (payload or []):
        if not isinstance(item, dict):
            continue
        mid = item.get("id") or item.get("name") or item.get("model")
        if not isinstance(mid, str) or not mid:
            continue
        src = item.get("pricing") if isinstance(item.get("pricing"), dict) else item
        seen = False
        for f in _PRICE_FIELDS:
            v = src.get(f)
            if v is None:
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            seen = True
            if v > 0:
                break
        else:
            if seen:
                out.append(mid)
    return out


def _free_tier_ids(payload, free_tiers):
    """Ids a catalog itself stamps with a FREE tier, for 'free_tier' providers.

    Same job as _zero_priced_ids, different signal. Some gateways publish no
    price at all but DO label every row with the tier it belongs to (llm7:
    'turbo' = free on the anonymous key, 'pro' = token-priced). Reading that
    label keeps such a provider genuinely LIVE: a newly launched free model
    shows up on its own instead of waiting for someone to hand-edit
    providers.py, which is exactly how llm7's pinned list went stale.

    `usage_based_only` is an independent paid flag — a row can sit in the free
    tier and still bill per token — so it is excluded whatever its tier says. A
    row with no tier field is left out: unknown must not read as free."""
    wanted = {str(t).lower() for t in (free_tiers or ())}
    if not wanted:
        return []
    out = []
    for item in (payload.get("data") or payload.get("models") or []) \
            if isinstance(payload, dict) else (payload or []):
        if not isinstance(item, dict):
            continue
        mid = item.get("id") or item.get("name") or item.get("model")
        if not isinstance(mid, str) or not mid:
            continue
        tier = item.get("tier")
        if not isinstance(tier, str) or tier.lower() not in wanted:
            continue
        if item.get("usage_based_only"):
            continue
        out.append(mid)
    return out


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
                # KeyError); their /models is public. One carrying a static_key
                # (uncloseai, llm7) sends that placeholder bearer instead.
                headers=({"Authorization": "Bearer " + pcfg["api_key"]}
                         if pcfg.get("api_key") else
                         ({"Authorization": "Bearer " + p["static_key"]}
                          if p.get("static_key") else {})),
                timeout=(CONNECT_TIMEOUT, MODELS_READ_TIMEOUT),
            )
            if resp.status_code == 200:
                # Harvest each model's PUBLISHED context window while we are
                # already holding the catalog. Learning it from a failed request
                # works but costs a real hop; the catalog states it for free.
                _learn_ctx_from_catalog(pid, resp.json())
                ids = _parse_model_ids(resp.json())
                # A 'pricing_zero' provider can only be judged against the prices
                # in the payload we are already holding; hand it that list, or it
                # fails closed on every id and stays frozen on its static list.
                known_free = None
                if p.get("free_filter") == "pricing_zero":
                    known_free = _zero_priced_ids(resp.json())
                # Same idea for a catalog that labels tiers instead of prices.
                elif p.get("free_filter") == "free_tier":
                    known_free = _free_tier_ids(resp.json(), p.get("free_tiers"))
                # filter_models drops blocked (uncensored) AND non-chat ids
                # (whisper/tts/embed/guard) — per the providers.py contract.
                _free_ids = [m for m in ids
                             if prov.is_free_model(pid, m, known_free=known_free)]
                # Embeddings are dropped by that filter and were therefore
                # invisible to the whole hub. They are a different SURFACE, not
                # junk -- /v1/embeddings is what codebase indexing and RAG call
                # -- so keep them here, where the catalog is already in hand and
                # a second discovery pass would cost another round trip.
                _remember_embedding_models(
                    pid, [m for m in _free_ids
                          if prov.is_embedding_model(m) and prov.is_model_allowed(m)])
                live_free = prov.filter_models(_free_ids)
                if live_free:
                    models = live_free
                    # Discovery is the FIRST place a brand-new model id is ever
                    # seen. If nothing can score it, ask the benchmark source
                    # now rather than leaving it dead last until the next
                    # scheduled refresh (debounced + off-thread inside).
                    _maybe_recheck_aa_for_unknown(live_free)
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


def _prefetch_auto_models(providers):
    """{pid: [models]} for every provider in `providers`, fetched CONCURRENTLY
    instead of one at a time -- see the comment at the call site in
    _route_by_difficulty for the measured cost this replaces. Each fetch is
    independent (provider_free_models's 60s cache is keyed per-pid), so there
    is nothing to race, only wall-clock time to save. Never raises: a
    provider whose fetch fails contributes an empty list, exactly what
    calling _auto_models(pid) directly would give on the same failure."""
    if not providers:
        return {}
    out = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(providers), 16)) as ex:
        futures = {ex.submit(_auto_models, pid): pid for pid in providers}
        for fut in concurrent.futures.as_completed(futures):
            pid = futures[fut]
            try:
                out[pid] = fut.result()
            except Exception:                                    # noqa: BLE001
                out[pid] = []
    return out


# Embedding models, harvested during the ordinary chat discovery pass (see
# provider_free_models) because they are filtered out of the chat catalog and
# would otherwise be invisible. Same TTL as that cache; no separate refresh.
_embed_cache = {}


def _remember_embedding_models(pid, models):
    with _model_cache_lock:
        _embed_cache[pid] = (time.time(), list(models or []))


def provider_embedding_models(pid):
    """Cached embedding ids for one provider, or the registry's static list.

    Never triggers discovery of its own: if the chat catalog has not been
    fetched yet this is empty, and the next chat listing fills it."""
    with _model_cache_lock:
        hit = _embed_cache.get(pid)
    if hit and (time.time() - hit[0]) < MODEL_CACHE_TTL:
        return list(hit[1])
    p = prov.get_provider(pid) or {}
    return [m for m in (p.get("default_free_models") or [])
            if prov.is_embedding_model(m) and prov.is_model_allowed(m)]


def embedding_models():
    """[{id:'<pid>/<model>', provider, model}] across enabled+keyed providers."""
    out = []
    for pid in _enabled_keyed():
        for m in provider_embedding_models(pid):
            if _is_model_dead(pid, m):
                continue
            out.append({"id": pid + "/" + m, "provider": pid, "model": m})
    return out


def aggregated_models():
    """[{id:'<pid>/<model>', provider, model}] across enabled+keyed providers,
    plus any usable local-subscription relay (sub-*) -- so a relay hop shows up
    as a pickable option in the dashboard's Chat/Test playground, not just
    reachable via an explicit pin from an external client."""
    out = []
    for pid in _enabled_keyed():
        for m in provider_free_models(pid):
            out.append({"id": pid + "/" + m, "provider": pid, "model": m})
    for pid in _sub_available_providers():
        for m in _sub_models(pid):
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
    # 2026-07-30: glm-4.7/4.6, gemini-2.5-pro and Xiaomi MiMo promoted in (user
    # top-tier list); nemotron/gpt-oss/gemma DEMOTED out to Tier C below.
    (("grok-4", "gpt-5", "claude-opus", "claude-sonnet-5", "claude-fable",
      "gemini-3-pro", "gemini-3.5-pro", "gemini-3-ultra", "gemini-3.5-ultra",
      "gemini-3.5-flash", "gemini-3-flash", "gemini-2.5-pro",
      "deepseek-v4", "deepseek-r2", "glm-5", "glm5", "glm-6", "glm-4.7", "glm-4.6",
      "kimi-k2.6", "kimi-k2.7", "kimi-k2-thinking",
      "hy3", "hunyuan-3", "tencent-hy",
      "minimax-m3", "qwen3.5", "qwen3-max", "mimo"), 100),
    # Tier A — strong open workhorses (production SEO + coding drivers).
    # 2026-07-30: bare qwen3, llama-4 / llama-3.3-70b, mistral-large, gpt-4o and
    # gemini-2.5-flash promoted (user top-tier list); nemotron-3/gpt-oss demoted out.
    (("deepseek-v3", "deepseek-r1", "deepseek-chat",
      "qwen3-235b", "qwen3-next", "qwen3-coder", "qwen3-32b", "qwen3",
      "kimi-k2", "minimax-m2", "gemini-2.5-flash",
      "llama-4", "llama4", "llama-3.3-70",
      "mistral-large", "gpt-4o",
      "hunyuan-a13", "hunyuan-turbos", "command-a"), 84),  # hy3 promoted to Tier S above
    # Tier B — capable mid (routine content, not hard reasoning).
    (("mistral-medium", "phi-4", "solar-pro", "nova-2-pro", "granite-4",
      "command-r-plus"), 56),
    # Tier C-hi — older mid / mid-small usable.
    (("qwen2.5-72", "mistral-small", "command-r"), 40),
    # Tier C — legacy / superseded / specialized (avoid for heavy).
    # 2026-07-30: nemotron, gpt-oss and the whole gemma family land here — the
    # user ranks them clearly BELOW the strong families, not above them.
    (("qwen2.5", "qwen2", "llama-3", "llama-2",
      "gemma-4", "gemma4", "gemma-3", "gemma3", "gemma-2",
      "nemotron", "gpt-oss",
      "mixtral", "moonshot-v1", "qwq", "distill", "codestral", "devstral",
      "mercury", "sonar", "ernie", "hermes", "gemini-2.0",
      "gpt-4o-mini"), 26),
    # Tier D — tiny / lite / mini / flash-lite / nano (the CAP also enforces this).
    (("flash-lite", "-lite", "-mini", "nano", "small", "mistral-nemo", "tiny",
      "ministral", "instant", "1b", "2b", "3b", "4b", "7b", "8b", "9b"), 18),
]

# NEW-VERSION HEURISTIC — a known-strong family ROOT at a version >= its pinned
# CURRENT strong version scores in the TOP band, so a brand-new release
# (deepseek-v5, glm-6, kimi-k3, qwen4, minimax-m4, gemini-4) auto-ranks strong the
# moment a free provider lists it — no table edit needed. Numbered families only
# (clean version parse); flat-named strong families (command-a, hunyuan hy3) are
# covered by _BENCH_FAMILY above.
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


# User-preference floors applied by _benchmark_score: (hy3, kimi-k3, puter
# gpt-5.6-sol class, puter gpt-5.6-terra/gpt-5.5-pro class, kimi-k2.6/k2.7,
# claude, gpt-5.5+, glm-5.x, gpt-5.x, deepseek-v4, minimax-m3).
# They are
# deliberate thumb-on-the-scale values, NOT measured strength, so any code that
# reasons about the SHAPE of the score distribution (the spread band) must exclude
# them. Kept as one tuple so the floor sites and _spread_pick can never drift apart.
#                 hy3    k3     sol  terra k2.6  claude gpt5.5+ glm5.x gpt5.x  dsv4   mm3
_PREF_FLOORS = (134.5, 134.8, 136, 135, 0,    138,   136,    134,   135,   134,   133)
# kimi-k2.6/k2.7 are CAPPED, not floored: the user ranks them below qwen3.5/
# 3.6 (108-109) and below mimo-2.5 (100), so the ceiling sits just under mimo.
_KIMI_K2_CEILING = 98

# How much a newer version gains INSIDE the top band. Deliberately tiny: this
# orders a family's releases against each other without lifting one family out
# of the band and above everything else.
#
# ASKED 2026-08-31: "glm 5.3 is good man but of course if new verison of it
# should be used more of ocurse and have higher priority". Below the band the
# hub already did this (qwen 3.8 -> 134.08, 4.0 -> 134.10; kimi k3 -> 134.8, k4
# -> 134.9); the band itself was flat, so glm-5.3, 5.4, 6 and 7 all scored
# exactly 138.00 and a new release ranked no higher than the one it replaced.
_BAND_MAJOR_STEP = 0.10
_BAND_MINOR_STEP = 0.01
_BAND_MAX_BUMP = 0.9            # stays strictly inside the band


def _band_version_bump(major, minor, base_major, base_minor):
    """Points above a top-band floor for a version newer than the one that
    earned it. Never negative, never enough to leave the band."""
    steps = (major - base_major) * _BAND_MAJOR_STEP + (minor - base_minor) * _BAND_MINOR_STEP
    return max(0.0, min(steps, _BAND_MAX_BUMP))

# Providers that RELAY someone else's models rather than hosting their own, so a
# model id there is a claim, not a guarantee. Subtracted after the preference
# floors (see the end of _benchmark_score) — a bias added earlier would be wiped
# by max(score, floor), which is exactly why g4f's relayed 'claude-sonnet-4'
# inherited claude's full 138 and owned the top slot.
_RELAY_DISCOUNT = {"g4f": 4.0}

# REVISED 2026-07-31 (2nd pass, user correction):
#   "kimi k3 is the best one AFTER gpt models from 5 up and claude models"
#     -> k3 moves from 140 (above everything) to 134.8: under every gpt-5.x
#        (135+) and under claude (138), but still above hy3/glm and the field.
#   "kimi 2.7 is NOT better than qwen 3.5/3.6 or even mimo 2.5"
#     -> the k2.6/k2.7 floor is REMOVED (index 4 = 0, i.e. no lift). It was
#        133, which ranked it above qwen3.6 (109) and mimo (100). A demotion
#        below mimo is applied at the bottom of _benchmark_score instead.
# Index 8 added 2026-07-31: "GPT-5 versions are better than GLM 5.2." Only
# gpt-5.5-and-up was floored (136), so gpt-5.0 through 5.4 carried NO floor and
# GLM 5.2 outranked them. The GPT-5 family now sits at 135 and glm-5.x drops to
# 134, which puts every GPT-5 above it while keeping the rest of the stated
# order intact: kimi-k3 140 > claude 138 > gpt-5.5+ 136 > gpt-5.0-5.4 / hy3 135
# > glm-5.x 134 > kimi-k2.6/2.7 133 > gemini (unfloored).
# Index 5/6 added 2026-07-31. USER RANKING, stated explicitly, best first:
#   kimi-k3 (140)  >  claude (138)  >  gpt-5.5 and up (136)  >  gemini
# Gemini deliberately gets NO floor: a floor only ever LIFTS a model, so the way
# to rank gemini last is to leave it on its natural score while the three above
# it are floored. hy3 (135) keeps the floor the user gave it earlier, now
# sitting just under the newly-named three rather than above them.

# Any provider's id shape for a real Claude model: 'claude-opus-5',
# 'anthropic/claude-fable-5', 'claude-3-7-sonnet'. Anchored on the word so
# 'claude-code' style CLI-relay ids (subscription hops, scored elsewhere) and
# stray substrings don't collect the floor.
_CLAUDE_FAMILY_RE = re.compile(r"(?:^|[/:._-])claude[-.]?(?:opus|sonnet|haiku|fable|instant|v\d|\d)")
# Captures the GPT version so the floor can SCALE with it — "all GPT-5 versions
# are good, and a higher version means it's better", which two flat tiers could
# not express. gpt-5 -> (5, None), gpt-5.6-sol -> (5, 6), gpt-6.2-codex ->
# (6, 2). gpt-4.1 matches as (4, 1) and is rejected by the major>=5 check.
_GPT_VER_RE = re.compile(r"\bgpt-(\d+)(?:\.(\d+))?")
# GLM 5.x (Zhipu) — glm-5, glm-5.2, zhipu/glm-5.2, THUDM/glm-5. Not glm-4.x.
_GLM5_RE = re.compile(r"glm-?5(?:\.\d+)?\b")
# Moonshot Kimi, version captured: kimi-k3, kimi-k3.5, kimik4, .../kimi-k5.
_KIMI_VERSION_RE = re.compile(r"kimi-?k-?(\d+)")
# Tencent HunYuan, with the version CAPTURED. hy3 was matched as a literal
# substring, so a future hy4 matched nothing at all and scored 10.0 -- dead
# last out of ~1300 listings. A version number is required immediately after
# the family name, which keeps unrelated ids like "hypernova-3" out.
_HY_VERSION_RE = re.compile(r"\b(?:hy|hunyuan)-?(\d+)\b")
# Same family, but with the version CAPTURED, so glm-5.3-and-up can be told
# apart from 5.2. The trailing word boundary keeps glm-4.6v-flash out: its
# "6" is followed by a word character, so the optional minor group
# backtracks away and the match reads (4, 0).
_GLM_VERSION_RE = re.compile(r"glm-?(\d+)(?:\.(\d+))?\b")
# The LATEST qwen line only: qwen3.5 and up (3.5, 3.6, 3.8, 3.9...). Captures the
# minor so a newer release outranks an older one instead of tying with it.
# qwen3 / qwen2.5 deliberately do NOT match — the preference was about the
# latest qwen, and floating the whole family would lift the small old ones too.
# Qwen, MAJOR and minor captured. The old pattern was r"qwen-?3\.([5-9])"
# -- 3.5 through 3.9 and nothing else -- so qwen4 matched nothing and scored
# 100 while qwen3.9 scored 134.09: the newer model ranked WORSE. See
# tests/test_newer_never_ranks_lower.py, which now guards every family.
_QWEN_LATEST_RE = re.compile(r"qwen-?(\d+)(?:\.(\d+))?\b")
# DeepSeek V4 (and later) — deepseek-v4, deepseek-v4-flash, deepseek/deepseek-v4,
# and morph's 'dsv4flash' once _canon_model_id has expanded it. V3 and R1 keep
# their measured Tier A/S scores; only the v4+ generation gets the floor.
_DSV4_RE = re.compile(r"deepseek[-_/]?v([4-9])(?:\.\d+)?")
# MiniMax M3 (and later) — minimax-m3, MiniMaxAI/MiniMax-M3, and morph's
# 'minimax3' after canonicalisation. M2/M2.7 stay on their measured score.
_MINIMAX3_RE = re.compile(r"minimax-m([3-9])(?:\.\d+)?")
# Gemini 3.1+ (Google) — gemini-3.1, gemini-3.5-flash, models/gemini-3.6-flash,
# google/gemini-4. Version-scaled like gpt-5.x because the user's rule is the
# same one: a higher version number means a better model. 3.0 and 2.x are NOT
# included — the user named 3.1 as the floor of the good ones.
_GEMINI_VER_RE = re.compile(r"gemini-(\d+)(?:\.(\d+))?")
# 2026-07-30: the qwen -45 demotion (_PREF_QWEN_DEMOTION) is REMOVED — qwen3 is a
# strong family again and ranks with the top tier via Tier A + _STRONG_ROOTS.


# --------------------------------------------------------------------------- #
# ARTIFICIAL ANALYSIS — real, independently-measured Intelligence Index scores
# (artificialanalysis.ai), replacing the hand-typed _BENCH_FAMILY guess with
# actual data WHEN a confident match exists. Everything else in _benchmark_score
# (size nudge, instruct bonus, provider bias, coding boost, hy3/kimi-k3
# PREFERENCE floors, mistral penalty, speed cap) still applies
# on top unchanged — those are deliberate user decisions, not attempts to
# measure capability, so real benchmark data must not silently overrule them.
# Free tier: 100 requests/day (resets 00:00 UTC) — refreshed on a long interval
# (not per-request) so routing never makes a network call in the hot path.
# --------------------------------------------------------------------------- #
AA_API_BASE = "https://artificialanalysis.ai/api/v2"
AA_SCORE_CACHE_PATH = os.path.join(os.path.expanduser("~"), ".free-llm-hub", "aa_scores.json")
AA_REFRESH_INTERVAL = 6 * 3600  # seconds
_aa_cache_lock = threading.Lock()
_aa_scores = {}          # normalized slug -> calibrated hub-scale score (in-memory, hot path reads this)
_aa_last_refresh = 0.0   # monotonic-ish wall clock of the last successful fetch


def _load_aa_cache():
    try:
        with open(AA_SCORE_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_aa_cache(cache):
    """Same atomic-write discipline as _save_test_cache / config.save_config."""
    try:
        parent = os.path.dirname(AA_SCORE_CACHE_PATH)
        os.makedirs(parent, exist_ok=True)
        data = json.dumps(cache, indent=2, ensure_ascii=False)
        fd, tmp_path = tempfile.mkstemp(prefix=".aa_scores-", suffix=".tmp", dir=parent)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        for _attempt in range(6):
            try:
                os.replace(tmp_path, AA_SCORE_CACHE_PATH)
                return
            except PermissionError:
                time.sleep(0.15)
            except OSError:
                break
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    except Exception:
        _log.debug("[aa] cache save failed", exc_info=True)


_AA_SLUG_STRIP_RE = re.compile(
    r"^(?:@cf/|nvidia/|z-ai/|zai-org/|moonshotai/|meta/|meta-llama/|google/|models/|"
    r"deepseek-ai/|minimaxai/|openai/|nousresearch/|inclusionai/|poolside/|cohere/)+")
_AA_SLUG_SUFFIX_RE = re.compile(r"(?::free|:beta|:extended|:nitro|:floor|:online)+$", re.IGNORECASE)
_AA_SLUG_PUNCT_RE = re.compile(r"[^a-z0-9]+")


def _normalize_aa_slug(text):
    """Collapse a hub model id OR an Artificial Analysis slug/name onto a bare
    alnum core for fuzzy matching — strips vendor path prefixes (nvidia's own
    ids nest 'vendor/model', openrouter mirrors that plus a ':free' suffix,
    cloudflare uses '@cf/vendor/model'), then any non-alphanumeric separator,
    so 'nvidia/nemotron-3-ultra-550b-a55b' and AA's 'nemotron-3-ultra-550b'
    compare as the same core string regardless of punctuation drift."""
    t = (text or "").strip().lower()
    t = _AA_SLUG_SUFFIX_RE.sub("", t)
    for _ in range(3):  # a couple of ids double-nest (nvidia's own catalog: 'nvidia/nvidia/...')
        stripped = _AA_SLUG_STRIP_RE.sub("", t)
        if stripped == t:
            break
        t = stripped
    return _AA_SLUG_PUNCT_RE.sub("", t)


_OPENROUTER_CATALOG_URL = "https://openrouter.ai/api/v1/models"

# On-demand AA re-check when discovery turns up a model nothing can score.
_AA_UNKNOWN_MIN_GAP = 900        # 15 min between out-of-band re-checks
_aa_unknown_last_check = [0.0]
_aa_unknown_lock = threading.Lock()


def _model_is_unscoreable(model_id):
    """True when NOTHING can put a real number on this id: no Artificial
    Analysis entry, and no known-strong family root to inherit a tier from."""
    if _aa_score_for(model_id) is not None:
        return False
    norm = _normalize_aa_slug(model_id)
    if _static_benchmark_score(norm) is not None:
        return False
    return not _strong_new_version_score((model_id or "").lower())


def _maybe_recheck_aa_for_unknown(model_ids):
    """A brand-new model id just showed up in a provider's catalog and nothing
    here can score it -- go ask the benchmark source about it NOW instead of
    waiting out the refresh interval.

    MEASURED 2026-08-30: an unknown id scores ~11.8 against a 358-model pool,
    i.e. dead last, so it is never routed to and never gets a chance to prove
    itself. Discovery notices a new model within MODEL_CACHE_TTL (60s) but the
    scores behind it only moved every AA_REFRESH_INTERVAL (6h) -- so a genuinely
    new flagship could sit at the bottom of the chain for most of a day.

    Debounced to _AA_UNKNOWN_MIN_GAP and run off-thread: this is reached from
    the discovery path, and one unrecognised id must never add a network round
    trip to a user's request. Cheap regardless -- the keyless catalog is a
    single unauthenticated GET, and an unknown id is rare once the cache is
    warm."""
    now = time.time()
    with _aa_unknown_lock:
        if now - _aa_unknown_last_check[0] < _AA_UNKNOWN_MIN_GAP:
            return False
        if not any(_model_is_unscoreable(m) for m in (model_ids or ())):
            return False
        _aa_unknown_last_check[0] = now
    _log.info("[aa] unrecognised model in a provider catalog -- re-checking benchmarks")
    threading.Thread(target=_aa_refresh_once, daemon=True).start()
    return True


def _fetch_aa_scores_keyless():
    """Artificial Analysis's Intelligence Index WITHOUT an AA API key.

    OpenRouter's public catalog carries AA's own numbers per model under
    benchmarks.artificial_analysis, and that endpoint needs no key, no account
    and no card. VERIFIED 2026-08-30: HTTP 200 unauthenticated, 396 models, 151
    of them AA-scored, newest scored entry created four days earlier -- so this
    is the same data the paid API sells, at the same freshness.

    Returns {normalized_slug: raw_aa_index}, i.e. the SAME shape the keyed path
    builds, so it feeds the existing _calibrate_aa_scores() fit and needs no
    separate scale. {} on any failure -- fail-open, exactly like the keyed path:
    callers fall back to the static tier table.

    Coverage is partial and that is fine. Measured against the live routable
    pool: 103 of 357 entries (29%). The models it does cover are the mainstream
    ones that dominate routing; the private provider aliases it cannot know
    (morph-kimik3, llama-3.3-70b-versatile) keep their static tier score. Real
    measurement where it exists, an honest guess elsewhere."""
    raw = {}
    try:
        resp = requests.get(_OPENROUTER_CATALOG_URL,
                            timeout=(CONNECT_TIMEOUT, MODELS_READ_TIMEOUT))
        if resp.status_code != 200:
            _log.warning("[aa] keyless catalog HTTP %d", resp.status_code)
            return {}
        for row in (resp.json().get("data") or []):
            if not isinstance(row, dict):
                continue
            bench = row.get("benchmarks")
            aa = (bench or {}).get("artificial_analysis") if isinstance(bench, dict) else None
            idx = (aa or {}).get("intelligence_index") if isinstance(aa, dict) else None
            if idx is None:
                continue
            # `id` FIRST, not canonical_slug: the canonical form carries a
            # release DATE ('moonshotai/kimi-k3-20260715' -> 'kimik320260715')
            # which can never match a hub id, so keying on it silently matched
            # almost nothing -- kimi-k3, qwen3.8 and glm-5.2 all missed until
            # this was fixed. Both forms are registered anyway: the extra key
            # costs nothing and catches a provider that does carry the date.
            for slug in (row.get("id"), row.get("canonical_slug")):
                if not slug:
                    continue
                norm = _normalize_aa_slug(slug)
                if norm:
                    raw[norm] = max(raw.get(norm, 0.0), float(idx))
    except (requests.RequestException, ValueError, TypeError, KeyError):
        _log.debug("[aa] keyless fetch failed", exc_info=True)
        return {}
    if raw:
        _log.info("[aa] %d model scores from OpenRouter's public catalog (no key)",
                  len(raw))
    return raw


def _fetch_aa_scores():
    """One full paginated fetch of Artificial Analysis's LLM Intelligence Index,
    calibrated onto the hub's existing ~0-110 scoring scale. Returns
    {normalized_slug: hub_scale_score} or {} on any failure (missing key,
    network error, empty/malformed response) — always fail-open, callers keep
    using whatever was cached before (or the static table if nothing ever
    succeeded)."""
    key = config.get_aa_api_key()
    if not key:
        # NO KEY IS NO LONGER THE END. OpenRouter's public catalog embeds the
        # very same Artificial Analysis numbers, keyless -- so the 6-hourly
        # refresh below now does real work on an install that has never had an
        # AA key, which until 2026-08-30 was every install: aa_scores.json had
        # never been written here and 100% of ranking came from a hand-typed
        # table nobody re-dates.
        raw = _fetch_aa_scores_keyless()
        return _calibrate_aa_scores(raw) if raw else {}
    raw = {}   # normalized_slug -> aa_intelligence_index
    page = 1
    try:
        while True:
            resp = requests.get(
                AA_API_BASE + "/language/models",
                headers={"x-api-key": key},
                params={"page": page, "page_size": 200},
                timeout=(CONNECT_TIMEOUT, MODELS_READ_TIMEOUT))
            if resp.status_code != 200:
                _log.warning("[aa] fetch HTTP %d on page %d", resp.status_code, page)
                break
            body = resp.json()
            for row in (body.get("data") or []):
                idx = row.get("artificial_analysis_intelligence_index")
                slug = row.get("slug") or row.get("name")
                if idx is None or not slug:
                    continue
                norm = _normalize_aa_slug(slug)
                if norm:
                    raw[norm] = max(raw.get(norm, 0.0), float(idx))
            pagination = body.get("pagination") or {}
            if not pagination.get("has_more") or page > 10:  # hard stop, never loop forever
                break
            page += 1
    except (requests.RequestException, ValueError, TypeError, KeyError):
        _log.debug("[aa] fetch failed", exc_info=True)
        return {}
    if not raw:
        return {}
    return _calibrate_aa_scores(raw)


def _calibrate_aa_scores(raw):
    """Fit AA's raw Intelligence Index values onto the hub's existing ~0-110
    heuristic scale via ordinary least squares, using every model this hub
    ALREADY has an opinion on (from _BENCH_FAMILY et al, computed via the
    static-only path) as training pairs. This anchors AA's real data to the
    same scale every other constant in this file already depends on
    (_TOOLS_MIN_SCORE, _ORCH_BAND, _sustain_penalty's point deductions) rather
    than requiring a full, riskier rescale of the whole routing system.
    Falls back to returning `raw` unscaled (better than nothing) if fewer than
    5 training pairs are found — not enough points for a trustworthy fit."""
    xs, ys = [], []
    for norm, aa_score in raw.items():
        existing = _static_benchmark_score(norm)
        if existing is not None:
            xs.append(aa_score)
            ys.append(existing)
    n = len(xs)
    if n < 5:
        _log.debug("[aa] only %d calibration pairs found, using raw AA scores unscaled", n)
        return dict(raw)
    sx, sy = sum(xs), sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-9:
        return dict(raw)
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    return {norm: slope * v + intercept for norm, v in raw.items()}


def _static_benchmark_score(normalized_model_text):
    """The OLD hand-typed _BENCH_FAMILY tier lookup only (no size/instruct/
    provider/preference adjustments) — used solely as calibration training
    data for _calibrate_aa_scores. Takes an already-normalized string (no
    provider context, so no provider-bias term) since this exists only to
    anchor AA's scale, not to route anything itself."""
    for names, pts in _BENCH_FAMILY:
        if any(_AA_SLUG_PUNCT_RE.sub("", n) in normalized_model_text for n in names):
            return float(pts)
    return None


def _aa_score_for(model_id):
    """Look up a confident Artificial Analysis score for a hub model id, or
    None if AA has never been fetched / has no matching model. Exact match on
    the normalized core string only — no loose substring fallback here, unlike
    _static_benchmark_score's family-tier matching, because a wrong AA match
    would silently misinform routing with a specific, confident-looking wrong
    number rather than an admittedly-generic tier guess."""
    if not _aa_scores:
        return None
    return _aa_scores.get(_normalize_aa_slug(model_id))


def _aa_refresh_once():
    global _aa_scores, _aa_last_refresh
    scores = _fetch_aa_scores()
    if scores:
        with _aa_cache_lock:
            _aa_scores = scores
            _aa_last_refresh = time.time()
            _save_aa_cache({"fetched_at": _aa_last_refresh, "scores": scores})
        _log.info("[aa] refreshed %d model scores from Artificial Analysis", len(scores))


def _aa_refresh_loop():
    # Load whatever was cached from a previous run immediately (no network wait).
    cached = _load_aa_cache()
    if isinstance(cached.get("scores"), dict):
        global _aa_scores, _aa_last_refresh
        _aa_scores = cached["scores"]
        _aa_last_refresh = cached.get("fetched_at") or 0.0
    time.sleep(5)  # let the server finish booting before the first live fetch
    while True:
        if config.get_aa_api_key():
            try:
                _aa_refresh_once()
            except Exception:
                _log.debug("[aa] refresh cycle error", exc_info=True)
        slept = 0.0
        while slept < AA_REFRESH_INTERVAL:
            time.sleep(min(60.0, AA_REFRESH_INTERVAL - slept))
            slept += 60.0


_aa_refresh_thread = None


def _start_aa_refresh():
    global _aa_refresh_thread
    if _aa_refresh_thread is not None:
        return
    _aa_refresh_thread = threading.Thread(target=_aa_refresh_loop, daemon=True)
    _aa_refresh_thread.start()


# --------------------------------------------------------------------------- #
# Compressed vendor ids -> canonical spelling.
#
# Family matching is substring-based ("glm-5" in the id), and every preference
# floor is a regex written against the canonical name. Relays that squeeze the
# separators out of an id therefore fall through BOTH and get ranked as unknown
# models. Morph is the clearest case — its whole catalog is spelled this way:
#
#   morph-glm52-744b     -> matched 'glm5' but _GLM5_RE needs a word boundary
#                           after the 5, so the 134 floor never applied  (128)
#   morph-qwen35-397b    -> only reached 'qwen3' (Tier A), then out-ranked
#                           qwen3.6 purely on parameter count             (123.9)
#   morph-minimax3-428b  -> 'minimax-m3' never matched at all              (30)
#   morph-dsv4flash      -> same model as deepseek/deepseek-v4-flash (108)  (10)
#
# So a strong model sat at the BOTTOM of the chain and was never picked first.
#
# These rules are deliberately narrow: each requires a known vendor prefix, and
# the two-digit rule requires exactly two digits NOT followed by another digit or
# a dot — so already-canonical ids ("qwen3-30b", "glm-4.6", "llama-3.3-70b") are
# left untouched. Anything unrecognised passes through unchanged.
# '-mini' as a real suffix ('gpt-4o-mini', 'gemini-3-mini'), NOT as the head of a
# longer word ('-minimax', '-ministral', '-miniature').
_MINI_SUFFIX_RE = re.compile(r"-mini(?![a-z])")

_CANON_TWO_DIGIT = re.compile(
    r"\b(glm|qwen|gemma|llama|mistral|hunyuan|ernie|granite|phi|yi)(\d)(\d)(?![\d.])")
_CANON_MINIMAX = re.compile(r"\bminimax-?m?(\d)(?:\.?(\d))?(?![\d.])")
_CANON_KIMI = re.compile(r"\bkimi-?k(\d)(?:\.?(\d))?(?![\d.])")
_CANON_DEEPSEEK = re.compile(r"\bds-?v(\d)")


def _canon_model_id(low):
    """Canonical spelling of a compressed model id, for FAMILY MATCHING ONLY.

    Never shown to a user and never sent upstream — the real id is always what
    gets called. This only decides how strong we think the model is."""
    out = _CANON_TWO_DIGIT.sub(lambda m: "%s%s.%s" % (m.group(1), m.group(2), m.group(3)), low)
    out = _CANON_MINIMAX.sub(
        lambda m: "minimax-m%s" % (m.group(1) + ("." + m.group(2) if m.group(2) else "")), out)
    out = _CANON_KIMI.sub(
        lambda m: "kimi-k%s" % (m.group(1) + ("." + m.group(2) if m.group(2) else "")), out)
    out = _CANON_DEEPSEEK.sub(lambda m: "deepseek-v%s" % m.group(1), out)
    return out


def _benchmark_score(pid, model_id):
    """Strength score for a '<model>' on provider `pid` (higher=better). Base
    tier comes from a REAL Artificial Analysis Intelligence Index match when
    one exists (calibrated onto this same scale — see _aa_score_for), else the
    hand-typed _BENCH_FAMILY guess as before. Every adjustment below (size,
    instruct bonus, provider bias, coding boost, hy3/kimi-k3/puter preference
    floors, mistral penalty, speed cap) still applies on top either
    way — those encode deliberate product decisions, not a capability
    estimate, so real data augments them rather than replacing them."""
    low = _canon_model_id((model_id or "").lower())
    aa = _aa_score_for(model_id)
    if aa is not None:
        score = aa
    else:
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
                              "claude", "gpt-5", "minimax-m", "hy3",
                              "hunyuan", "starcoder")):
        score += 8
    # PREFERENCE FLOORS (not measured scores). These lift a favourite model above
    # every natural score so it wins the top slot — but because they are ARTIFICIAL
    # they must never define the top of the load-spreading band (a 135 floor on a
    # model whose real score is 108 drags the band cutoff from 82.8 up to 105 and
    # starves the rotation down to a couple of ids). _spread_pick therefore computes
    # the band from NATURAL scores only; see _PREF_FLOORS use there.
    # USER PREFERENCE: hy3 (Tencent HunYuan) is the #1 pick for coding + heavy tasks,
    # then latest kimi / qwen / deepseek (all already Tier S). Floor it above every
    # other model's realistic max (~133: tier 100 + coder 8 + size 20 + instruct 3 +
    # provider 2) so hy3 wins the top slot whenever it's available/keyed — second
    # only to the puter gpt-5.6-sol floor below. Only affects
    # the max-based (hard/tools/agentic) routing — light tasks still pick cheapest.
    if "hy3" in low or "hunyuan-3" in low or "tencent-hy" in low:
        score = max(score, _PREF_FLOORS[0])
    # USER PREFERENCE 2026-08-31: "check if hy4 available if yes he should be
    # also used from top best models cause he is gooooooooooood".
    #
    # Tencent open-sourced Hy4 preview on 2026-08-28 (Apache 2.0, 770B MoE, 49B
    # active, 1M context): Terminal Bench 2.1 85.4, past DeepSeek V4 Pro;
    # DeepSWE 28.0 -> 64.3; and 2.99/4.00 in Tencent's own 163-expert blind
    # eval, slightly ahead of GLM-5.3 (2.92) and Kimi K3 (2.94). So hy4-and-up
    # joins the top band. hy3 keeps its own, lower floor above, exactly where
    # the earlier preference put it.
    #
    # NOT AVAILABLE ANYWHERE YET, measured: all 416 discovered ids searched for
    # tencent/hunyuan/hy-<n>/770b returned three entries, every one of them hy3.
    # This is written now because of what the substring test above did to a
    # version it had never heard of: hy4 scored 10.0, dead last, so the
    # strongest model of the family would have been the worst-ranked thing in
    # the hub on the day it arrived -- and silently, since a model that is never
    # picked never fails either. A floor is only a preference among models that
    # exist; if hy4 turns up dead, the measured machinery demotes it like
    # anything else (_note_nonanswer, the reliability ledger, the swarm prior).
    _hyv = _HY_VERSION_RE.search(low)
    if _hyv:
        try:
            _hmaj = int(_hyv.group(1))
            if _hmaj >= 4:
                score = max(score, _PREF_FLOORS[5] + _band_version_bump(_hmaj, 0, 4, 0))
        except ValueError:
            pass
    # USER PREFERENCE: Kimi K3 (Moonshot) — top pick for the heaviest tasks, right
    # behind hy3 and above every other model. Auto-applies when a provider serves a
    # kimi-k3 id (nothing lists it yet). Matches kimi-k3 / kimi-k3.x / .../kimi-k3.
    # k3 AND UP. This was the literal string "kimi-k3", so kimi-k4 matched
    # nothing and scored 108 against k3's 134.8 -- the newer model ranking below
    # the one it replaces, the same defect found in glm, hunyuan and qwen. The
    # k2.x ceiling below is unaffected: it is a separate, explicit demotion the
    # user asked for, and 2 < 3 never reaches this floor.
    _km = _KIMI_VERSION_RE.search(low)
    if _km:
        try:
            if int(_km.group(1)) >= 3:
                score = max(score, _PREF_FLOORS[1] + min(int(_km.group(1)) - 3, 9) * 0.1)
        except ValueError:
            pass
    # USER CORRECTION 2026-07-31: "kimi 2.7 is not better than qwen 3.5 or 3.6,
    # or even mimo 2.5." It used to be FLOORED at 133, which ranked it above
    # qwen3.6 (109) and mimo (100) and made it win turns it should not have.
    # The floor is gone; a demotion puts it just under mimo instead, so it stays
    # a usable fallback without ever outranking those three. K3 is unaffected —
    # it is a different generation and keeps its own floor above.
    # Substring match covers every provider id shape: plain 'kimi-k2.6',
    # cloudflare '@cf/moonshotai/kimi-k2.7-code', g4f-nvidia 'moonshotai/kimi-k2.6'.
    if ("kimi-k2.6" in low or "kimi-k2.7" in low
            or "kimik2.6" in low or "kimik2.7" in low):
        score = min(score, _KIMI_K2_CEILING)
    # USER PREFERENCE: Puter's newest GPT flagship — the gpt-5.6-sol(-pro)
    # class — ranks FIRST among equals (user-requested top priority for the
    # puter provider, 2026-07-30): floored one point above hy3 so a keyed
    # puter account wins the top slot whenever it serves the id. The
    # gpt-5.6-terra / gpt-5.5-pro class sits level with hy3. Id-keyed like
    # the other floors — any provider serving these ids gets the lift.
    if "gpt-5.6-sol" in low:
        score = max(score, _PREF_FLOORS[2])
    if "gpt-5.6-terra" in low or "gpt-5.5-pro" in low:
        score = max(score, _PREF_FLOORS[3])
    # USER RANKING 2026-07-31: claude > gpt-5.5+ > gemini, with kimi-k3 above all
    # three (its floor is set above). Matches every provider id shape a Claude
    # model arrives as -- 'claude-opus-5', 'anthropic/claude-fable-5',
    # 'claude-3-7-sonnet' -- but NOT the hub's own 'claude' CLI relay ids, which
    # are subscription hops scored elsewhere.
    if _CLAUDE_FAMILY_RE.search(low):
        score = max(score, _PREF_FLOORS[5])
    # "gpt-5.5 and up": 5.5, 5.6 and any later 5.x/6+. Written as a regex so
    # gpt-5.4 and older do NOT qualify and no floor is handed to a weaker id.
    # GPT-5+ scales WITH THE VERSION: "all GPT-5 versions are good, and a higher
    # version means it's better." Two flat tiers could not express that (gpt-5.0
    # and gpt-5.4 tied), so the floor is computed from the number itself:
    #   5.0 -> 135.0   5.2 -> 135.4   5.4 -> 135.8   5.5 -> 136.0   5.6 -> 136.2
    #   6.0 -> 137.0   6.2 -> 137.4
    # Capped below claude's 138 so a future version bump can't silently overtake
    # a ranking the user set deliberately. gpt-4.x never matches.
    _gm = _GPT_VER_RE.search(low)
    if _gm:
        _major = int(_gm.group(1))
        _minor = int(_gm.group(2) or 0)
        if _major >= 5:
            score = max(score, min(135.0 + (_major - 5) * 2 + _minor * 0.2,
                                   _PREF_FLOORS[5] - 0.5))
    # USER PREFERENCE 2026-07-31: "GLM 5.2 is also good — if available it should
    # be used." Floored level with hy3, i.e. just under the named top three, so
    # a live glm-5.x is reached for ahead of the ordinary field. glm-4.x and the
    # -flash/-air variants are NOT included (the speed cap below still applies).
    if _GLM5_RE.search(low):
        score = max(score, _PREF_FLOORS[7])
    # USER PREFERENCE 2026-08-30: "ox alpha ... it's the best one now" -- and
    # "Ox Alpha" was the stealth name GLM-5.3-Flash shipped under (listed
    # unnamed and free on OpenRouter 2026-08-20, revealed 2026-08-26). The
    # coding numbers published under that name beat what already sits at the
    # top of this table: DeepSWE 80% against Claude Fable 5's 65% and GPT-5.6
    # Sol's 52%. So 5.3-and-up joins the top band instead of sharing 5.2's
    # floor; 5.2 itself stays exactly where the 2026-07-31 preference put it
    # ("GPT-5 versions are better than GLM 5.2"), which is why this is a
    # separate version-gated floor rather than a bump to _PREF_FLOORS[7].
    #
    # VERIFIED REACHABLE FIRST. The only "ox-alpha" id in the entire catalog
    # (g4f/...:pegalink/ox-alpha-agent) answered HTTP 400 on 6 of 6 attempts
    # across three spellings; tokenrouter/z-ai/glm-5.3-free answered as itself.
    # A floor on a model that cannot serve just puts a dead hop at the head of
    # every chain -- which is the whole reason this was tested before promoted.
    _glmv = _GLM_VERSION_RE.search(low)
    if _glmv:
        try:
            _gmaj, _gmin = int(_glmv.group(1)), int(_glmv.group(2) or 0)
            if (_gmaj, _gmin) >= (5, 3):
                score = max(score, _PREF_FLOORS[5] + _band_version_bump(_gmaj, _gmin, 5, 3))
        except ValueError:
            pass
    # USER PREFERENCE 2026-08-01: "DeepSeek V4 should have priority almost as
    # much as GLM 5.2, and MiniMax M3 is also good, almost like DeepSeek 4."
    # UPDATED 2026-08-01, same day: "deepseek v4 seems good so it should be the
    # SAME level as glm 5.2" — so deepseek-v4 is no longer just under glm-5.x,
    # it is level with it at 134. MiniMax M3 keeps its "almost like DeepSeek 4"
    # place immediately below. In that order, which
    # puts both above the whole qwen/mimo field without displacing the named top
    # four (claude 138 / gpt-5.x 135-137 / kimi-k3 134.8 / hy3 134.5).
    # mimo-2.5 needs no rule: it sits at 100 and is already under all of them.
    #
    # USER CORRECTION 2026-08-03: "deepseek v4 flash is better than the pro --
    # the pro is LESS than the flash." Unlike Gemini (pro > flash, see below),
    # DeepSeek V4 is the other way round. Flash (and any unlabeled v4 id) keeps
    # the family's established level -- same as glm-5.2, per the instruction
    # above, unretracted -- and -pro drops one notch below it, still a hair
    # above minimax-m3's "almost like deepseek 4" floor.
    if _DSV4_RE.search(low):
        score = max(score, _PREF_FLOORS[9] - (0.5 if "pro" in low else 0))
    # USER PREFERENCE, stated as "latest kimi / qwen / deepseek" being the top
    # picks together — but only kimi, glm and deepseek ever got a floor, so qwen
    # was left on its natural score. MEASURED 2026-08-30: qwen3.8-27b scored
    # 110.9 against glm-5.2's 134.0, i.e. 24 points below the peers it was named
    # beside, and it never won a slot. Worse, qwen3.8 and qwen3.6 scored
    # IDENTICALLY (110.9 each) — nothing in the table knew 3.8 was newer, so
    # every future release would flatline the same way.
    #
    # Floored level with glm-5.x/deepseek-v4 and scaled by the minor version so a
    # newer qwen always beats an older one (3.5 -> 134.05, 3.6 -> 134.06,
    # 3.8 -> 134.08). Deliberately 3.5+: qwen3 and 2.5 stay on their natural
    # scores, since the preference was about the LATEST qwen.
    _qm = _QWEN_LATEST_RE.search(low)
    if _qm:
        try:
            _qmaj = int(_qm.group(1))
            _qmin = int(_qm.group(2) or 0)
        except ValueError:
            _qmaj = _qmin = 0
        # 3.5+ is where the preference starts; 4+ has no minor to wait for.
        if (_qmaj, _qmin) >= (3, 5):
            # Scaled so a newer qwen always beats an older one, and a whole
            # major version is worth more than a minor: 3.5 -> 134.05,
            # 3.8 -> 134.08, 4.0 -> 134.10, 5.0 -> 134.20.
            _bump = min(_qmaj - 3, 9) * 0.1 + min(_qmin, 9) * 0.01
            score = max(score, _PREF_FLOORS[7] + _bump)
    if _MINIMAX3_RE.search(low):
        score = max(score, _PREF_FLOORS[10])
    # USER PREFERENCE 2026-08-01: "gemini flash or pro 3.1, 3.5, 3.6 and up are
    # better than deepseek 4 / v4 — but gemini pro is better than gemini flash,
    # of course."
    #
    # These bands moved UP on 2026-08-01 (later the same day) and the move was
    # forced, not chosen: the user then said DeepSeek V4 should sit at the SAME
    # level as glm-5.2, which took deepseek-v4 from 133.5 to 134. Gemini had to
    # stay above deepseek — that is the instruction above, unretracted — so it
    # now sits above glm-5.2 too. Nobody ever said "glm beats gemini"; that was
    # an inference from deepseek being below glm, and it stopped holding the
    # moment deepseek drew level. Two bands between glm/deepseek (134) and
    # hy3-large (134.5):
    #   pro    134.250 .. 134.440
    #   flash  134.050 .. 134.240
    # Both scale off the FULL version number so a newer gemini always outranks
    # an older one (scaling on the minor alone put gemini-4.0 below gemini-3.6),
    # and the bands cannot overlap, so pro always beats flash of ANY version.
    # The ceiling keeps the family under glm-5.2, which the user ranked earlier
    # and did not revisit. The bands are narrow only because they must fit
    # between two ranks that are already set — only the ORDER inside matters.
    #
    # NOT included: gemini-3.0 and 2.x (the user named 3.1 as the floor), and
    # -flash-lite, which is Google's cheapest tier and neither "flash" nor
    # "pro" — it stays in the capped small tier where it already was.
    _gem = _GEMINI_VER_RE.search(low)
    _gem_floored = False
    if _gem and "flash-lite" not in low:
        _gver = int(_gem.group(1)) + int(_gem.group(2) or 0) / 10.0
        if _gver >= 3.1:
            _gbase = 134.25 if "pro" in low else 134.05
            score = max(score, _gbase + min(_gver - 3.1, 7.6) * 0.025)
            _gem_floored = True
    # 2026-07-30: the 2026-07-25 qwen -45 demotion is REMOVED — qwen3 is a strong
    # family per the user and ranks with the top tier (Tier A + _STRONG_ROOTS).
    if (("mistral" in low or "mixtral" in low or "ministral" in low)
            and not any(k in low for k in ("mistral-large", "mistral-medium"))):
        score -= 14
    # SPEED/TINY CAP (last word): flash/lite/mini/nano/distill variants and any model
    # < 14B params are latency/edge tier — cap them out of the heavy band even when
    # their family root is a flagship (glm-4.7-FLASH, gemini-3.1-flash-lite,
    # deepseek-r1-DISTILL). gemini-3(.5)-flash / gemini-4 are the kept exceptions.
    flash_ok = ("gemini-3.5-flash", "gemini-3-flash", "gemini-4")
    speed = ("flash-lite", "-lite", "nano", "distill", "-air",
             "instant", "-tiny", "-edge", "mixtral", "moonshot-v1",
             "ernie-speed", "ernie-lite", "mistral-nemo", "qwq")
    capped = any(s in low for s in speed)
    # '-mini' needs a boundary, unlike the rest: as a bare substring it also hits
    # '-minimax', so EVERY relay that prefixes the vendor ('morph-minimax3-428b',
    # any 'x-minimax-m3') had a 428B flagship capped to the 30-point tiny tier and
    # buried at the bottom of the chain, while the unprefixed 'minimax-m3' scored
    # 108. '-ministral' is a genuinely small Mistral and stays capped via its own
    # entry in the tuple above.
    if _MINI_SUFFIX_RE.search(low):
        capped = True
    # 'flash' is ambiguous: weak on gemini-3.1/glm-4.x, but STRONG on
    # deepseek-v4-flash / gemini-3.5-flash. Don't cap a model the version
    # heuristic already flagged as a strong new release (sv > 0).
    if "flash" in low and not any(ok in low for ok in flash_ok) and not sv:
        capped = True
    if params_b is not None and params_b < 14:
        capped = True
    # USER DIRECTIVE 2026-07-31: "ALL available Claude models should be in top."
    # The speed cap runs LAST and overrode the floor for the ids whose names
    # happen to contain a capped word — claude-instant matched "instant" and
    # landed at 30 despite being floored to 138. Claude is exempt so the floor
    # is what actually decides, which is the whole point of setting one.
    if capped and _CLAUDE_FAMILY_RE.search(low):
        capped = False
    # Same reasoning for gemini 3.1+: nearly every id in that family is a
    # '-flash' or '-flash-lite', so the cap would undo the floor on all of them
    # and the user's "gemini 3.1/3.5/3.6+ beat deepseek 4" would silently not
    # hold. Only the versions that EARNED the floor are exempt — gemini-3.0 and
    # 2.x flash variants stay capped.
    if capped and _gem_floored:
        capped = False
    if capped:
        score = min(score, 30)
    score -= _shared_budget_penalty(pid, low)
    # RELAY DISCOUNT, applied LAST so it survives the preference floors above
    # (those use max(score, floor), so a bias added earlier would be erased).
    #
    # MEASURED 2026-08-30: 15 of the top 20 eligible models were g4f entries,
    # and the whole top 8 was. g4f is a pool of community-donated relays that
    # advertise ids like 'srv_msjkstt...:claude-sonnet-4' — the NAME earns
    # claude's 138 floor, but nothing verifies the relay serves the model it
    # claims, and a live smoke test put the pool at 110 ok / 112 fail. So the
    # least reliable provider owned every top slot while first-party hosts of
    # genuinely strong models (nvidia's kimi-k3, opencode-zen's hy3, wandb's
    # glm-5.2) sat just underneath, unused.
    #
    # A discount, not a ban: g4f stays in the chain and still answers when the
    # first-party hosts are rate-limited — it just stops outranking them on a
    # claimed name alone. Sized so a relayed frontier id lands beside the
    # first-party field (138 -> 134) rather than below it.
    score -= _RELAY_DISCOUNT.get(pid, 0.0)
    return score


# Some providers meter every model against ONE shared daily allowance, and the
# per-model price varies enormously — so an expensive id doesn't just cost more,
# it eats the budget its cheaper siblings would have used. Cloudflare Workers AI
# is the concrete case: 10,000 free neurons/day, all models sharing it
# (developers.cloudflare.com/workers-ai/platform/pricing). Per 1M tokens:
#   @cf/openai/gpt-oss-120b        31,818 in /  68,182 out  -> ~150 calls/day
#   @cf/qwen/qwen3-30b-a3b-fp8      4,625 in /  30,475 out  -> ~500 calls/day
#   @cf/moonshotai/kimi-k2.6       86,364 in / 363,636 out  -> ~37 calls/day
#   @cf/moonshotai/kimi-k2.7-code  86,364 in / 363,636 out  -> ~37 calls/day
# The kimi ids are ~5x the output cost for no better coding score than the qwen /
# gpt-oss siblings, so preferring them spends the whole day's allowance in a few
# turns. The penalty demotes them WITHIN Cloudflare (they stay available, just
# last) and is scoped by provider — kimi via any other provider is untouched.
_SHARED_BUDGET_PENALTY = {
    "cloudflare": ((("kimi-k2.6", "kimi-k2.7"), 12),),
}


def _shared_budget_penalty(pid, low):
    for subs, points in _SHARED_BUDGET_PENALTY.get(pid, ()):
        if any(s in low for s in subs):
            return points
    return 0


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


def _repair_tool_arguments(s):
    """A tool-call `arguments` field must be exactly ONE JSON value. Some upstreams
    stream cumulative or duplicated argument deltas which the gateway concatenated into
    'X{...}' — two JSON values back-to-back. Strict providers (google/groq/cerebras)
    then reject the WHOLE request with 400 'invalid character { after top-level value',
    and because the malformed tool_call sits in the conversation it re-poisons EVERY
    later turn — one bad tool_call 503s the CLI forever. Return a single valid JSON
    string: unchanged when already valid, the FIRST complete JSON value when there is
    trailing junk, else '{}' (empty args beats a hard 400 every turn)."""
    if not isinstance(s, str):
        return s
    t = s.strip()
    if not t:
        return s
    try:
        json.loads(t)
        return s                      # already a single valid JSON value — leave as-is
    except ValueError:
        pass
    try:
        obj, _end = json.JSONDecoder().raw_decode(t)  # first value; ignore trailing junk
        return json.dumps(obj)
    except ValueError:
        return "{}"


# Git-diff preamble lines a strict CLI's apply_patch parser does not expect. Codex's
# parser (and most agentic apply_patch implementations) accept EITHER its own V4A
# envelope ("*** Begin Patch" / "*** Update File: ...") OR a bare unified diff
# ("--- a/f\n+++ b/f\n@@ ...") — but NOT git's full `diff --git` header block. A model
# trained mostly on `git diff` output naturally includes that header even when asked
# for a plain patch, and the payload is otherwise perfectly correct: the SAME hunk body
# that works from other models is sitting right after these lines.
# MEASURED 2026-07-27: deepseek-v4-pro (nvidia) emitted exactly this shape —
#   'diff --git a/hello.txt b/hello.txt\nnew file mode 100644\nindex 0000000..45b983b\n
#    --- /dev/null\n+++ b/hello.txt\n@@ -0,0 +1 @@\n+hi'
# — and codex_core::tools::router rejected the WHOLE turn with "Fatal error: tool
# apply_patch invoked with incompatible payload", producing zero files despite the
# patch itself being valid. Earlier fix (_TOOL_DIALECT_MISMATCH) demoted such models
# out of agentic routing entirely — correct as a stopgap, wrong as the end state: it
# throws away a model's real capability over a fixable formatting quirk. Normalizing
# the payload is strictly better than excluding the model.
_GIT_DIFF_PREAMBLE_RE = re.compile(
    r"^(?:diff --git .*|index [0-9a-fA-F]{4,40}\.\.[0-9a-fA-F]{4,40}(?: \d+)?|"
    r"new file mode \d+|deleted file mode \d+|old mode \d+|new mode \d+|"
    r"similarity index \d+%|dissimilarity index \d+%|"
    r"rename (?:from|to) .*|copy (?:from|to) .*)\n",
    re.MULTILINE,
)


def _normalize_apply_patch_diff(name, arguments):
    """Strip git's `diff --git` header block from an apply_patch tool call's
    `input`, leaving the bare unified-diff hunk a CLI's parser actually expects.
    Only touches the apply_patch tool (name check) and only when the payload
    parses as JSON with a string `input` field containing the git preamble —
    anything else (a different tool, an already-clean patch, a malformed
    payload _repair_tool_arguments will handle separately) passes through
    byte-for-byte unchanged. Never raises."""
    if name != "apply_patch" or not isinstance(arguments, str) or "diff --git" not in arguments:
        return arguments
    try:
        obj = json.loads(arguments)
    except ValueError:
        return arguments
    if not isinstance(obj, dict) or not isinstance(obj.get("input"), str):
        return arguments
    cleaned = _GIT_DIFF_PREAMBLE_RE.sub("", obj["input"])
    if cleaned == obj["input"]:
        return arguments  # nothing matched — leave untouched rather than re-serialize for no reason
    obj["input"] = cleaned
    try:
        return json.dumps(obj)
    except (TypeError, ValueError):
        return arguments


def _repair_message_tool_calls(row):
    """Heal malformed tool-call arguments on one message (in place). Shared by all 3
    CLI endpoints via _normalize_openai_messages so a poisoned agentic history self-
    heals on the next turn instead of 503-ing forever. See _repair_tool_arguments."""
    tcs = row.get("tool_calls")
    if not isinstance(tcs, list):
        return
    for tc in tcs:
        if isinstance(tc, dict):
            fn = tc.get("function")
            if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                fn["arguments"] = _repair_tool_arguments(fn["arguments"])


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
        _repair_message_tool_calls(row)  # un-poison doubled tool_call arguments (all CLIs)
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
    "web design", "checkout", "cart", "mobile app", "script that", "scraper",
    "crawler", "pipeline",
    # Debugging asks are heavy even when phrased casually. "fix this bug in my
    # code" measured SIMPLE -> a 7B model, because the list had "debug" but not
    # "bug", and not a single word for "it is broken".
    "bug", "not working", "doesn't work", "does not work", "broken", "crash",
    "error", "exception", "fails", "failing", "stuck", "hangs",
)

# WHAT is being asked for, weighted double, because naming a whole deliverable
# IS the heaviness -- and it is the part that survives typos.
#
# Measured 2026-08-01 in the dashboard chat: "crerat ebst store website web
# deisng ... for restaurant in fez" classified SIMPLE and routed to a 7B model.
# The verb was misspelled ("crerat"), so a list of verbs matched nothing, while
# the thing being asked for ("store", "website") was spelled correctly. Typos
# are normal in a chat box; the noun is the reliable signal.
#
# Word boundaries, not substrings: "app" inside "happen", "site" inside
# "opposite", "game" inside "gamely". The rest of the hint lists can live with
# substring matching because their terms are long enough not to collide.
_ARTIFACT_RE = re.compile(
    r"\b(?:web ?sites?|sites?|landing pages?|home ?pages?|stores?|shops?|"
    r"e-?commerce|dashboards?|apps?|applications?|games?|blogs?|portfolios?|"
    r"platforms?|saas|chat ?bots?|apis?|clones?)\b")
_ARTIFACT_WEIGHT = 2
_ARTIFACT_MAX_HITS = 2          # "a store website app" is one job, not three
# CREATION INTENT. USER DIRECTIVE 2026-08-30: "when it's about website creation
# or any creation thing, of course he should always use best models, no matter
# our rules... even motion design creation, or anything that will be created
# with our hub."
#
# The classifier judges SHAPE, not intent, so a creation ask phrased shortly
# fell to the cheap tier: MEASURED, "Write a python function to reverse a
# string" classified `simple` and routed 40/40 to deepinfra/Qwen2.5-72B, score
# 45.9 out of a 358-model pool -- an old weak model writing code while kimi-k3
# (134.8) sat idle. Building something is exactly when model quality shows, and
# the cheap tier exists for one-word replies and the hub's own probes, not for
# work the user will keep.
#
# Lifts to `hard` rather than `medium`: medium still applies the fast-only
# prefilter, which drops several of the strongest sustainable builders.
_CREATION_VERBS = ("build", "create", "make", "write", "implement",
                   "design", "generate", "develop", "animate", "compose",
                   "draft", "produce", "scaffold", "port", "refactor",
                   "rewrite", "redesign", "rebuild", "code")
_CREATION_INTENT_RE = re.compile(
    r"\b(?:" + "|".join(_CREATION_VERBS) + r")\b", re.IGNORECASE)
_SIMPLE_HINTS = (
    "translate", "summarize", "summarise", "tl;dr", "rephrase", "reword",
    "spell", "grammar", "fix typo", "capitalize", "lowercase", "uppercase",
    "yes or no", "one word", "one line", "define ", "what is ", "who is ",
    "list ", "hello", "hi ", "thanks", "thank you",
)
# Minimum benchmark score a model needs to be trusted with each tier.
#
# `simple` was 20, which permits a 7B model. That is the right answer for "hi"
# and the wrong answer for everything a classifier gets wrong -- and a free-text
# chat box guarantees it will sometimes be wrong (typos, terse asks, a language
# the hints are not written in). 45 keeps the tier cheap while putting a floor
# under the damage: the worst case becomes a small-but-real model instead of a
# 7B answering a request to build a website. Trivial asks still route here, so
# quota on the strong models is still preserved for work that needs them.
_DIFFICULTY_FLOOR = {"simple": 45, "medium": 50, "hard": 78}


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
    artifacts = min(len(_ARTIFACT_RE.findall(low)), _ARTIFACT_MAX_HITS)
    hard_hits += artifacts          # also suppresses the short-ask penalty below
    score += hard_hits + artifacts * (_ARTIFACT_WEIGHT - 1)
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
    # groq free is a HARD 8000 tokens/minute — a single 30-42k agentic request 413s
    # on it 100% of the time, so it must be prefiltered OFF large requests (it stays
    # usable for the small turns it can actually hold). cerebras gpt-oss-120b is a
    # real 128k context on a 14,400/day budget (the sustainable large-agentic
    # workhorse) — 60000 wrongly dropped it once a growing conversation passed ~52k,
    # exactly when it was needed most.
    "groq": 8000, "github-models": 60000, "huggingface": 30000, "mistral": 120000,
    "morph": 30000, "sambanova": 120000, "cerebras": 128000, "deepseek": 120000,
    "openrouter": 128000, "cohere": 100000, "nvidia": 250000, "google": 900000,
    "cloudflare": 120000, "nararouter": 120000, "kimi": 128000, "glm": 128000,
    "aiand": 120000, "xiaomi": 60000, "minimax": 120000,
}
_DEFAULT_TPM = 100000


def _provider_tpm(pid):
    return _PROVIDER_TPM.get(pid, _DEFAULT_TPM)


def _est_tokens(messages, tools=None, overhead=400):
    """Rough token estimate of a request (~4 chars/token). Counts message text,
    tool-call arguments, AND tool schemas — tools dominate an agentic CLI's size.

    `overhead` is a deliberate safety margin for roles and wire formatting, and
    exists because this number is used to keep requests UNDER provider TPM and
    context limits: over-estimating there costs nothing, under-estimating costs
    a 413. It has to be dropped for anything the CLIENT reads as a token count,
    though -- Gemini's countTokens reported 406 tokens for a 24-character
    message, which is the margin, not the text, and would make a client believe
    it was near a limit it is nowhere near."""
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
    est = chars // 4 + overhead
    # Text shorter than one whole token still costs one. Only matters once the
    # routing margin is dropped (countTokens), where "hi" floored to 0 -- and a
    # client reading 0 tokens for real text concludes the message was empty.
    return 1 if (chars and not est) else est


# --------------------------------------------------------------------------- #
# OUTCOME-LEARNED RELIABILITY
#
# Every other free-tier gateway routes on AVAILABILITY (is it up, does it have
# quota left). None of them can route on "did this hop actually deliver",
# because a pure proxy never sees the request through to a usable answer. This
# hub does, so it can learn from what really happened instead of a hand-typed
# list going stale (see _TOOL_PROVEN, four hardcoded strings).
#
# THE PROBLEM THIS FIXES, measured live: g4f-nvidia/mistral-medium-3.5-128b
# ReadTimeout'd ~7 minutes per attempt, four attempts running (2026-08-03),
# then hit ConnectionError + HTTP 524 on consecutive requests (2026-08-05).
# Every cooldown we added is a SHORT-TERM sideline that expires; nothing
# remembered "this hop has a bad track record" across the whole session, so it
# kept winning chain slots on raw benchmark strength alone.
#
# DELIBERATELY PENALTY-ONLY. A perfect record earns NO bonus -- promoting a
# mediocre-but-reliable model over a genuinely stronger one is a real
# regression risk, and this is a reliability signal, not a quality one. An
# unknown (pid, model) scores exactly as before, so this can only ever demote
# something that has actually, repeatedly failed here.
#
# SCOPE LIMIT, stated honestly: "ok" means the hop returned a usable answer,
# NOT that the coding CLI went on to complete its task. True task-completion
# learning needs a signal the CLI itself does not report back. This is learned
# RELIABILITY, which is still strictly better than the hardcoded guess it
# supplements.
# --------------------------------------------------------------------------- #
_OUTCOME_TTL = 7 * 86400        # forget a record untouched for a week
_OUTCOME_CAP = 40               # cap each counter; halving on overflow keeps the
                                # RATIO but lets a fixed provider climb back out
_OUTCOME_WEIGHT = 9.0           # max points an all-failure record can cost
_outcomes = {}                  # (pid, model) -> {"ok": int, "fail": int, "last": float}
_latencies = {}                 # (pid, model) -> {"ms": float, "n": int, "last": float}
_outcome_lock = threading.Lock()


def _load_perf_stats():
    """Seed the learned dicts from disk at startup.

    Both dicts used to be born empty on every boot, and this hub reboots on
    every auto-update (5h) plus every machine restart -- so a signal designed
    to accumulate over a week of real traffic never got past a few hours, and
    ranking leaned on the hand-typed capability table almost exclusively.
    Nothing else changes: an empty or unreadable file leaves both dicts empty,
    which is the exact state this code has always started in."""
    try:
        outcomes, latency = perfstats.load()
        with _outcome_lock:
            _outcomes.update(outcomes)
            _latencies.update(latency)
        if outcomes or latency:
            _log.info("[perf] restored %d outcome and %d latency records",
                      len(outcomes), len(latency))
    except Exception:                                            # noqa: BLE001
        pass


def _save_perf_stats(force=False):
    """Persist both learned dicts. perfstats.save throttles internally, so this
    is cheap enough to call from the request path."""
    try:
        with _outcome_lock:
            outcomes = {k: dict(v) for k, v in _outcomes.items()}
            latency = {k: dict(v) for k, v in _latencies.items()}
        perfstats.save(outcomes, latency, force=force)
    except Exception:                                            # noqa: BLE001
        pass


def _record_outcome(pid, model, ok):
    """One real delivery result for this (pid, model). Never raises."""
    if not (pid and model):
        return
    try:
        now = time.time()
        with _outcome_lock:
            rec = _outcomes.get((pid, model))
            if not rec or now - rec.get("last", 0) > _OUTCOME_TTL:
                rec = {"ok": 0, "fail": 0, "last": now}
            rec["ok" if ok else "fail"] += 1
            rec["last"] = now
            if rec["ok"] + rec["fail"] > _OUTCOME_CAP:
                # Halve BOTH so the ratio survives while old evidence decays --
                # a provider that was broken for an hour must be able to earn
                # its slot back once it starts answering again.
                rec["ok"] //= 2
                rec["fail"] //= 2
            _outcomes[(pid, model)] = rec
    except Exception:                                            # noqa: BLE001
        pass
    _save_perf_stats()


# Measured latency. Deliberately narrow: only NON-STREAMING hops are timed, so
# every sample means the same thing (full answer produced). Timing a streaming
# hop the same way would measure time-to-headers and quietly average two
# different quantities into one number.
_LATENCY_ALPHA = 0.3            # EWMA weight on the newest sample
_LATENCY_FREE_MS = 20000.0      # at or under this, no penalty at all
_LATENCY_MAX_MS = 90000.0       # at or over this, the full penalty
_LATENCY_WEIGHT = 6.0           # max points a reliably-slow hop can lose
_LATENCY_MIN_SAMPLES = 3        # never judge a hop on one unlucky request


def _record_latency(pid, model, ms):
    """One measured end-to-end duration (ms) for a hop that DELIVERED.

    Exponentially weighted so a provider that gets faster (or slower) is
    reflected within a few requests instead of being anchored to its first
    ever sample. Failures are not timed: a hop that 429s in 200ms is not
    'fast', and _reliability already covers not-delivering."""
    if not (pid and model) or not ms or ms <= 0:
        return
    try:
        now = time.time()
        with _outcome_lock:
            rec = _latencies.get((pid, model))
            if not rec or now - rec.get("last", 0) > _OUTCOME_TTL:
                rec = {"ms": float(ms), "n": 0, "last": now}
            else:
                rec["ms"] = (1 - _LATENCY_ALPHA) * rec["ms"] + _LATENCY_ALPHA * float(ms)
            rec["n"] = min(rec.get("n", 0) + 1, _OUTCOME_CAP)
            rec["last"] = now
            _latencies[(pid, model)] = rec
    except Exception:                                            # noqa: BLE001
        pass
    # ...and keep the raw sample, so the same measurement can also be reported
    # as a distribution rather than only as a mean.
    _record_speed_sample(_speed, pid, model, ms)


# RAW SAMPLES, alongside the EWMA above.
#
# The EWMA is what ROUTING uses, and it is the right shape for that: one number,
# cheap, and it forgets old behaviour. It is the wrong shape for a person asking
# "is this model actually fast", because a mean hides the thing that makes a
# model painful to use -- a p50 of four seconds with a p95 of ninety is a very
# different model from a steady twenty, and both average out the same.
#
# So the last _SPEED_SAMPLES durations are kept per (pid, model) and reported as
# percentiles. In memory only: unlike the EWMA these are not routing inputs, and
# a distribution that rebuilds within a few requests is not worth a file format.
_SPEED_SAMPLES = 64
_speed = {}          # (pid, model) -> [ms, ...]   total duration, non-streaming
_ttft = {}           # (pid, model) -> [ms, ...]   time to FIRST CONTENT, streaming


def _record_speed_sample(bucket, pid, model, ms):
    if not (pid and model) or not ms or ms <= 0:
        return
    try:
        with _outcome_lock:
            row = bucket.setdefault((pid, model), [])
            row.append(float(ms))
            if len(row) > _SPEED_SAMPLES:
                del row[:-_SPEED_SAMPLES]
    except Exception:                                            # noqa: BLE001
        pass


def _record_ttft(pid, model, ms):
    """Time to the first CONTENT token of a streamed answer.

    Not time to first byte: providers send a role delta, keep-alives and
    sometimes a whole reasoning block before any content, so first-byte says
    nothing about when the user sees words appear. _peek_until_content already
    reads exactly that far to tell a real answer from an empty 200, so the
    measurement is free -- it is the moment that check succeeds.

    This is the number the old code explicitly refused to record, because
    folding it into the same average as a non-streaming total would have mixed
    two different quantities. Kept apart, both are worth having."""
    _record_speed_sample(_ttft, pid, model, ms)


def _note_ttft(resp, pid, model):
    """Record TTFT for a streaming hop that just produced its first content.

    The clock started in _dispatch_chat, before the request went out, and is
    carried on the response so the peek sites do not each have to thread a
    start time through."""
    started = getattr(resp, "_hub_started", None)
    if started is None:
        return
    try:
        _record_ttft(pid, model, (time.perf_counter() - started) * 1000.0)
    except Exception:                                            # noqa: BLE001
        pass


def _percentile(values, pct):
    """Nearest-rank percentile. No numpy, and no interpolation: with at most 64
    samples, interpolating between two real measurements invents a number that
    was never observed."""
    if not values:
        return None
    ordered = sorted(values)
    k = int(round((pct / 100.0) * len(ordered) + 0.5)) - 1
    return ordered[max(0, min(k, len(ordered) - 1))]


def _speed_profile(pid, model):
    """p50/p95 of total duration and of time-to-first-token, or Nones."""
    with _outcome_lock:
        durations = list(_speed.get((pid, model)) or [])
        ttfts = list(_ttft.get((pid, model)) or [])
    return {
        "p50_ms": _percentile(durations, 50),
        "p95_ms": _percentile(durations, 95),
        "ttft_p50_ms": _percentile(ttfts, 50),
        "ttft_p95_ms": _percentile(ttfts, 95),
        "samples": len(durations),
        "ttft_samples": len(ttfts),
    }


def _measured_latency_ms(pid, model):
    """The learned EWMA duration, or None while the evidence is too thin."""
    with _outcome_lock:
        rec = _latencies.get((pid, model))
        if not rec or time.time() - rec.get("last", 0) > _OUTCOME_TTL:
            return None
        if rec.get("n", 0) < _LATENCY_MIN_SAMPLES:
            return None
        return rec.get("ms")


def _latency_penalty(pid, model):
    """0 for an unknown, thinly-measured or reasonably quick hop; up to
    _LATENCY_WEIGHT for one measured as consistently slow.

    Penalty-only and neutral-when-unknown, exactly like _reliability_penalty:
    being fast earns nothing (speed is not quality, and a stronger model is
    usually worth a wait), while a hop that really does take a minute and a
    half every time stops outranking a comparable one that answers in five
    seconds."""
    ms = _measured_latency_ms(pid, model)
    if ms is None or ms <= _LATENCY_FREE_MS:
        return 0.0
    span = _LATENCY_MAX_MS - _LATENCY_FREE_MS
    return min(1.0, (ms - _LATENCY_FREE_MS) / span) * _LATENCY_WEIGHT


def _reliability(pid, model):
    """Laplace-smoothed delivery rate in [0,1]. Exactly 0.5 (neutral) when
    unknown or stale, so a model with no history is never judged -- and one
    single failure cannot condemn a model outright ((0+1)/(1+2) = 0.33, not 0)."""
    with _outcome_lock:
        rec = _outcomes.get((pid, model))
        if not rec or time.time() - rec.get("last", 0) > _OUTCOME_TTL:
            return 0.5
        ok, fail = rec.get("ok", 0), rec.get("fail", 0)
    return (ok + 1.0) / (ok + fail + 2.0)


def _reliability_penalty(pid, model):
    """0 for an unknown or healthy hop; up to _OUTCOME_WEIGHT for one that has
    really, repeatedly failed to deliver here. Never negative (see the
    penalty-only note above)."""
    return max(0.0, (0.5 - _reliability(pid, model))) * 2.0 * _OUTCOME_WEIGHT


def _record_chat_usage(hop_pid, hop_model, data, prompt_est):
    """Record usage from a completed OpenAI-shaped chat response `data` (the
    raw upstream JSON -- all three protocol handlers dispatch through the
    same OpenAI-shaped upstream call, so this is one shared hook point).
    Uses the REAL usage object when the provider returned one; otherwise
    falls back to the same char/4 estimate this file already uses elsewhere
    (_est_tokens). Never raises -- usage_history.record() already swallows
    its own errors, but guard the data-parsing here too.

    ALSO the single success hook for outcome-learned reliability: this is
    called from all six accepted-answer sites across the three endpoints
    (chat/responses/messages, streaming and not), and only ever once a hop's
    answer was actually accepted -- which is exactly the "it delivered"
    signal _reliability_penalty needs."""
    _record_outcome(hop_pid, hop_model, True)
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
    "mistral": 66, "google": 62, "nvidia": 54, "openrouter": 52,
    "huggingface": 46, "github-models": 44, "cohere": 60,
}
_DEFAULT_SPEED = 55
# Reasoning / "thinking" model families — slow to first useful token.
# USER 2026-08-04: "models that need reasoning maximum effort to be pushed
# for that even if will take time it should just work... we want the hub to
# output always best quality possible." deepseek-v4(+) is a real reasoning
# model (catalog data confirms "capabilities":{"reasoning":true}) but was
# invisible to this pattern -- deepseek[-_]?r\d only matches the R1/R2
# reasoning-labeled line, not V4's own reasoning mode, so _apply_reasoning_
# effort never set an effort for it at all and it never dropped into the
# slow/last-resort tier where a reasoning model belongs. deepseek[-9] mirrors
# the v4+ cutoff _DSV4_RE already uses elsewhere for the same family.
_SLOW_MODEL_RE = re.compile(
    r"(reasoning|thinking|\bqwq\b|deepseek[-_]?r\d|deepseek[-_/]?v[4-9]|[-/]r1\b|\bo1\b|\bo3\b|"
    r"magistral|nemotron[-_](ultra|super)|gpt[-_]?oss|[-_]think\b|deepthink)", re.I)

# LAST-RESORT families — nemotron (ANY variant), gpt-oss, gemma. Demoted to
# Tier C in _BENCH_FAMILY, but a real Artificial Analysis score (_aa_score_for)
# OVERRIDES the tier guess and re-inflates them (measured: nemotron-3-ultra
# ~104, gpt-oss-120b ~99), and _TOOL_PROVEN still lists them from the
# 2026-07-25 dialect evidence — so SCORES alone cannot keep them behind the
# strong families, and the fallback chain kept landing on them while
# glm-4.7 / kimi-k2.6 / kimi-k2.7-code sat alive (RESPONSES-503 logs
# 2026-07-27: chain opened nvidia/nemotron-3-ultra -> cloudflare/gpt-oss-120b
# -> openrouter/nemotron-3-super). This is the chain-ORDERING rule: candidates
# matching it are partitioned to the TAIL of every fallback chain — after every
# other alive candidate for the request's constraints (tools/context), and for
# tool requests after every tool-proven normal candidate too. They may serve a
# PRIMARY only for difficulty=='simple' or when nothing else is alive. Ordered
# last, never deleted — they stay the final safety net.
# Below this many DISTINCT providers, the agentic pool is treated as collapsed
# rather than merely narrowed, and routing widens back out (see the funnel
# measurement at the use site in _route_by_difficulty). 4 is deliberately low:
# the point is to break a single-family monopoly, not to force diversity for
# its own sake -- a genuinely small live fleet still routes normally.
_MIN_AGENTIC_PROVIDERS = 4

_LOW_QUALITY_RE = re.compile(r"nemotron|gpt[-_]?oss|gemma", re.I)


def _is_low_quality(model_id):
    """True for the demoted last-resort families (see _LOW_QUALITY_RE)."""
    return bool(_LOW_QUALITY_RE.search(model_id or ""))


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
# the model is fine) and 5xx (transient upstream). 402/403/404/410 are treated as
# "this exact model is unusable with this key", because they are unambiguous:
#   404 = model doesn't exist on this provider (e.g. aiand/glm-5.2),
#   410 = model existed and is now permanently gone (e.g. nvidia/qwen3-next-
#         80b-a3b-instruct: still listed in /models, 410s on every generation) --
#         a STRONGER signal than 404, not a lesser one,
#   403 = this key has no access to it,
#   402 = payment required / insufficient credits — a trial provider with no free
#         balance (e.g. aiand/deepseek-v4) that would 402 EVERY turn otherwise.
# 400 is NOT auto-sidelined: it is just as often a bad payload as a bad model,
# and blocklisting a good model off one malformed request would be worse.
# All self-heal: entries expire after the TTL, so a topped-up/restored model returns.
#
# THE BUG a missing 410 caused (found live): a dead model sitting in a hop chain
# doesn't just waste that one hop -- _retryable() correctly refuses to retry a
# 410, which sets last_hard for the WHOLE request, which defeats the bonus
# whole-chain retry a few lines down (it only fires when NOTHING hard failed).
# So one never-excluded dead model turned "every other hop was a survivable
# 429" into a hard 503 on every single request that happened to draw it.
# --------------------------------------------------------------------------- #
_DEAD_MODEL_TTL = 6 * 3600         # 6h, then re-probe (token fixed? model back?)
_dead_models = {}                  # (pid, model) -> expiry epoch
_dead_lock = threading.Lock()
_DEAD_STATUSES = (402, 403, 404, 410)


# --------------------------------------------------------------------------- #
# Persisted test-result cache. The dashboard used to re-test every keyed
# provider on EVERY page load (nothing survived past the in-memory JS variable),
# which meant a real generation request per provider on every single visit —
# real quota spent just to redraw a page. Persist each /api/test/<pid> outcome
# to disk so the dashboard can hydrate instantly from the LAST known result and
# only re-test what's actually stale, while still detecting genuinely NEW free
# models a provider adds over time (routing already discovers those live via
# provider_free_models(); this is what makes the TEST/verification side of the
# dashboard aware of them too, instead of only the invisible routing layer).
# --------------------------------------------------------------------------- #
TEST_CACHE_PATH = os.path.join(os.path.expanduser("~"), ".free-llm-hub", "test_cache.json")
_test_cache_lock = threading.Lock()


def _load_test_cache():
    try:
        with open(TEST_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_test_cache(cache):
    """Same atomic-write discipline as config.save_config: temp file + os.replace,
    with a short retry loop for a Windows AV/cloud-sync lock. Best-effort — a
    failure here must never break the test response the user is waiting on."""
    try:
        parent = os.path.dirname(TEST_CACHE_PATH)
        os.makedirs(parent, exist_ok=True)
        data = json.dumps(cache, indent=2, ensure_ascii=False)
        fd, tmp_path = tempfile.mkstemp(prefix=".test_cache-", suffix=".tmp", dir=parent)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        for _attempt in range(6):
            try:
                os.replace(tmp_path, TEST_CACHE_PATH)
                return
            except PermissionError:
                time.sleep(0.15)
            except OSError:
                break
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    except Exception:
        _log.debug("[test-cache] save failed", exc_info=True)


def _record_test_result(pid, ok, detail, sample_models, attempted=None):
    """Persist one /api/test/<pid> outcome. Detects two things a provider can do
    between visits, in EITHER direction:
      new_models   — an id never seen in this provider's catalog before (a union
                     across every test ever recorded, not just the last one, so
                     a model added while the dashboard was closed still shows up).
      stale_models — an id that was VERIFIED WORKING by an earlier real generation
                     and is now either delisted (missing from the current catalog)
                     or still listed but failing generation THIS run (silently
                     deprecated — exactly the openrouter/nvidia/google cases found
                     by hand tonight: still in /models, 404/410/429 on generation).
    `attempted` is every (model, ok) pair this call actually ran a generation
    against — normally just the ones tried before the loop found a winner or gave
    up, NOT the whole candidate list, so this is coverage that accumulates over
    repeated tests rather than a single exhaustive pass (keeps quota cost the
    same as before; a full per-model audit is a separate, opt-in feature).
    Returns (new_models, stale_models); both empty on the first recorded test for
    a provider, so day-one never floods everything as new/stale."""
    sample_models = [m for m in (sample_models or []) if isinstance(m, str)]
    attempted = [(m, bool(a_ok)) for m, a_ok in (attempted or []) if isinstance(m, str)]
    with _test_cache_lock:
        cache = _load_test_cache()
        prev = cache.get(pid) or {}
        known_before = set(prev.get("known_models") or [])
        verified_before = dict(prev.get("verified_models") or {})
        new_models = [m for m in sample_models if m not in known_before] if prev else []

        verified_now = dict(verified_before)
        stale_models = []
        if prev:
            # (a) previously verified-working, now missing from the catalog entirely.
            # Mark it ok=False in verified_now IMMEDIATELY (not just append to the
            # report list) — otherwise the next run reads the same stale
            # verified_before entry and re-flags the same model forever. This makes
            # stale_models a one-time TRANSITION notice, not a recurring nag.
            for m, info in verified_before.items():
                if info.get("ok") and m not in sample_models:
                    stale_models.append(m)
                    verified_now[m] = {"ok": False, "at": time.time()}
            # (b) previously verified-working, still listed, but THIS attempt failed.
            for m, a_ok in attempted:
                if not a_ok and verified_before.get(m, {}).get("ok") and m not in stale_models:
                    stale_models.append(m)

        # This run's actual attempts are the most current signal — apply them last
        # so they win over the delisted-mark above if a model is somehow both.
        for m, a_ok in attempted:
            verified_now[m] = {"ok": a_ok, "at": time.time()}
        # Bound growth: a provider's model list is at most a few hundred ids; drop
        # the oldest once past a generous cap so this file can't grow unbounded.
        if len(verified_now) > 300:
            for m in sorted(verified_now, key=lambda k: verified_now[k].get("at", 0))[:len(verified_now) - 300]:
                verified_now.pop(m, None)

        cache[pid] = {
            "ok": bool(ok),
            "detail": detail or "",
            "sample_models": sample_models[:8],
            "known_models": sorted(known_before | set(sample_models))[:200],
            "verified_models": verified_now,
            "tested_at": time.time(),
        }
        _save_test_cache(cache)
    return new_models, stale_models


def _mark_model_dead(pid, model, status):
    if not model or status not in _DEAD_STATUSES:
        return
    with _dead_lock:
        _dead_models[(pid, str(model))] = time.time() + _DEAD_MODEL_TTL


def _is_model_dead(pid, model):
    # A model the USER switched off is treated exactly like one upstream has
    # withdrawn: unavailable for routing. Done HERE rather than at each filter
    # because all six of them -- the candidate pool, _build_chain, the model
    # lists and the probes -- already call this with (pid, model), so one seam
    # covers them all and none can be forgotten later. /api/dead-models reads
    # _dead_model_rows() instead, so a user-blocked model never shows up there
    # claiming the upstream killed it.
    if _is_model_blocked_by_user(pid, model):
        return True
    return _is_model_dead_upstream(pid, model)


def _is_model_dead_upstream(pid, model):
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
_PROVIDER_AUTHFAIL_THRESHOLD = 3       # distinct models 401/402 -> the KEY is bad
_PROVIDER_FORBIDDEN_THRESHOLD = 8      # 403-only: usually per-model, not a bad key
_PROVIDER_NOCREDIT_THRESHOLD = 2       # 402 on 2 distinct models = the ACCOUNT is broke
_AUTH_FAIL_STATUSES = (401, 402, 403)
_dead_providers = {}                   # pid -> expiry epoch
_provider_authfail = {}                # pid -> set(models that auth-failed this window)
_provider_keyfail = set()              # pids that saw a real 401/402 this window
_provider_dead_lock = threading.Lock()

# CONSECUTIVE-HARD-FAILURE breaker — a status-agnostic safety net ON TOP OF
# _mark_provider_authfail. The distinct-model rule above misses two real cases
# seen live 2026-07-24: (1) a provider that MASKS an empty wallet as 404
# model_not_found (aiand: /balance says 402 "Insufficient credits" but chat calls
# 404) never trips the 401/402/403 counter at all; (2) a provider whose ONE hot
# model repeatedly fails (nararouter/hy3 402) never reaches "2 DISTINCT models".
# Both let a dead provider keep winning a chain slot on every request. This net
# counts consecutive HARD failures (4xx that mean "produced no usable answer")
# with NO 2xx in between; a single success resets it, so a provider that 404s on
# a retired id but serves others (nvidia) never accumulates. Ignores 429/5xx
# (transient / handled by mark_throttled) so a rate-limit blip can't park a
# healthy provider. Same 30-min TTL + auto-re-probe as the rest.
_PROVIDER_CONSEC_FAIL_THRESHOLD = 4
_HARD_FAIL_STATUSES = (401, 402, 403, 404)
_provider_consec_fail = {}             # pid -> consecutive hard-fails since last 2xx


# A 429 whose body says the DAILY allowance (not a per-minute burst) is spent.
# Cloudflare Workers AI is the concrete case: 10,000 neurons/day, reset 00:00 UTC,
# error "you have used up your daily free allocation of 10,000 neurons" (code 3036,
# also seen as 4006). Google's free-tier RPD message is matched too. Deliberately
# NARROW — a generic 429 must keep the short 60s cooldown, since most are bursts.
_DAILY_EXHAUSTED_RE = re.compile(
    r"daily (?:free )?(?:allocation|quota|limit)|used up your daily|"
    r"free_tier_requests|requests per day|\bRPD\b|neurons", re.I)


def _daily_exhaustion_secs(pid, resp, cap=6 * 3600):
    """Seconds until pid's quota window resets, when a 429 body says the DAILY
    budget is gone. Returns None for an ordinary burst 429 (caller keeps its 60s).
    Capped so a mis-read never sidelines a provider for a whole day."""
    try:
        body = (resp.text or "")[:2000]
    except Exception:
        return None
    if not _DAILY_EXHAUSTED_RE.search(body):
        return None
    try:
        secs = quota.status(pid).get("resets_in")
    except Exception:
        return None
    if not secs or secs <= 0:
        return None
    return min(float(secs), cap)


def _mark_provider_authfail(pid, model, status):
    """Record an auth/credit failure; once enough DISTINCT models of a provider fail
    this way, the key is bad — sideline the whole provider (not just each model).

    403 is graded SEPARATELY and far more leniently than 401/402. 401 (bad token)
    and 402 (no credit) are properties of the KEY, so three models is already proof.
    403 usually is not: free gateways return it per-MODEL (gated/preview/regional
    ids) while the key works fine elsewhere. Sidelining a whole provider on three
    such 403s silently deleted most of the candidate pool, which funnelled every
    request onto whatever single model was left — and left a real 403 with nowhere
    to fall through to. Per-model 403s are already handled by _mark_model_dead."""
    if status not in _AUTH_FAIL_STATUSES:
        return
    with _provider_dead_lock:
        s = _provider_authfail.setdefault(pid, set())
        if model:
            s.add(str(model))
        if status in (401, 402):
            _provider_keyfail.add(pid)      # this window saw a KEY-level failure
        if status == 402 and len(s) >= _PROVIDER_NOCREDIT_THRESHOLD:
            # 402 is USUALLY an account fact ("balance is 0, top up to continue") and a
            # broke provider that keeps winning the primary slot burns a hop on every
            # request. But it is not ALWAYS account-wide: some aggregators serve their
            # free models fine and 402 only on the premium ids (verified live on aiand,
            # which answers on qwen3.6 while other ids 402). So confirm on a SECOND
            # distinct model before sidelining the whole provider — one 402 only kills
            # that model (_mark_model_dead already did), never its working siblings.
            _dead_providers[pid] = time.time() + _PROVIDER_DEAD_TTL
            return
        # A key-level failure anywhere in the window keeps the strict threshold; a
        # 403-only window needs far more distinct models before we blame the key
        # (a token 403ing on EVERY model still trips it, just a little later).
        threshold = (_PROVIDER_AUTHFAIL_THRESHOLD if pid in _provider_keyfail
                     else _PROVIDER_FORBIDDEN_THRESHOLD)
        if len(s) >= threshold:
            _dead_providers[pid] = time.time() + _PROVIDER_DEAD_TTL


def _note_provider_result(pid, ok, hard_fail=False):
    """Consecutive-hard-failure provider breaker (see _provider_consec_fail).
    ok=True (any 2xx) clears the streak; hard_fail=True (a 4xx that produced no
    usable answer, INCLUDING a 404-masked no-credit) increments it, and at the
    threshold the whole provider is parked with the standard TTL + auto-re-probe.
    Neither flag set (429/5xx) leaves the streak untouched."""
    if not pid:
        return
    with _provider_dead_lock:
        if ok:
            _provider_consec_fail.pop(pid, None)
            return
        if not hard_fail:
            return
        n = _provider_consec_fail.get(pid, 0) + 1
        _provider_consec_fail[pid] = n
        if n >= _PROVIDER_CONSEC_FAIL_THRESHOLD:
            _dead_providers[pid] = time.time() + _PROVIDER_DEAD_TTL
            _provider_consec_fail.pop(pid, None)


def _is_provider_dead(pid):
    with _provider_dead_lock:
        exp = _dead_providers.get(pid)
        if not exp:
            return False
        if exp <= time.time():
            _dead_providers.pop(pid, None)
            _provider_authfail.pop(pid, None)   # reset counter -> a clean re-probe
            _provider_keyfail.discard(pid)
            _provider_consec_fail.pop(pid, None)
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


# Bridge for quota.init_persistence(): the dead-model/provider maps ride along in
# the same state file as the quota blob (the "app" key). Expired entries are
# dropped on BOTH dump and load — a sideline that would already have lifted is
# worse than none after a restart.

def _dead_state_dump():
    now = time.time()
    with _dead_lock:
        dead_models = {"%s|%s" % (p, m): exp
                       for (p, m), exp in _dead_models.items() if exp > now}
    with _provider_dead_lock:
        out = {
            "dead_models": dead_models,
            "dead_providers": {p: exp for p, exp in _dead_providers.items() if exp > now},
            "provider_authfail": {p: sorted(ms) for p, ms in _provider_authfail.items()},
            "provider_keyfail": sorted(_provider_keyfail),
            "provider_consec_fail": dict(_provider_consec_fail),
        }
    # Learned context windows ride along. Without this they were in-memory only,
    # so EVERY restart forgot them and had to re-learn each one by burning a real
    # 400 -- which is exactly how a Codex session kept losing its best hop and
    # falling through to a flash-lite. A model's context window is a fixed fact,
    # so it is safe to remember indefinitely (no TTL).
    with _model_max_input_lock:
        out["model_max_input"] = {"%s|%s" % (p, m): v
                                  for (p, m), v in _MODEL_MAX_INPUT.items()}
    # Learned delivery reliability rides along too (see _record_outcome). It is
    # earned one real request at a time, so losing it on every restart would
    # mean re-learning a bad hop by burning real chain slots on it again --
    # the same argument that put model_max_input here. TTL-expired records are
    # dropped rather than saved.
    with _outcome_lock:
        out["outcomes"] = {"%s|%s" % (p, m): [r.get("ok", 0), r.get("fail", 0), r.get("last", 0)]
                           for (p, m), r in _outcomes.items()
                           if now - r.get("last", 0) <= _OUTCOME_TTL}
    return out


def _dead_state_load(blob):
    now = time.time()
    with _dead_lock:
        for key, exp in (blob.get("dead_models") or {}).items():
            if isinstance(key, str) and "|" in key and exp > now:
                p, m = key.split("|", 1)
                _dead_models[(p, m)] = exp
    with _provider_dead_lock:
        for p, exp in (blob.get("dead_providers") or {}).items():
            if isinstance(p, str) and exp > now:
                _dead_providers[p] = exp
        for p, ms in (blob.get("provider_authfail") or {}).items():
            if isinstance(p, str) and isinstance(ms, list):
                _provider_authfail[p] = set(m for m in ms if isinstance(m, str))
        for p in (blob.get("provider_keyfail") or []):
            if isinstance(p, str):
                _provider_keyfail.add(p)
        for p, n in (blob.get("provider_consec_fail") or {}).items():
            if isinstance(p, str) and isinstance(n, int):
                _provider_consec_fail[p] = n
    with _model_max_input_lock:
        for key, v in (blob.get("model_max_input") or {}).items():
            if isinstance(key, str) and "|" in key and isinstance(v, int) and v >= 1000:
                p, m = key.split("|", 1)
                cur = _MODEL_MAX_INPUT.get((p, m))
                _MODEL_MAX_INPUT[(p, m)] = min(cur, v) if cur else v
    with _outcome_lock:
        for key, row in (blob.get("outcomes") or {}).items():
            if not (isinstance(key, str) and "|" in key
                    and isinstance(row, list) and len(row) == 3):
                continue
            ok, fail, last = row
            if not all(isinstance(v, (int, float)) for v in (ok, fail, last)):
                continue
            if now - last > _OUTCOME_TTL:
                continue                  # stale evidence -- start that hop clean
            p, m = key.split("|", 1)
            _outcomes[(p, m)] = {"ok": int(ok), "fail": int(fail), "last": float(last)}


def _init_quota_persistence():
    """Persist quota + dead-model/provider state next to the config so a restart
    doesn't wipe a provider's 429 sideline or daily spend. Best-effort: a bad
    path or file must never block startup."""
    try:
        quota.init_persistence(os.path.join(config.state_dir(), "quota-state.json"),
                               extra_load=_dead_state_load, extra_dump=_dead_state_dump)
    except Exception:
        pass


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
    # NOTE: a third "sub-agentrouter" relay lived here until 2026-07-31 —
    # removed at explicit user request together with the `agentrouter`
    # provider entry (see providers.py). It shelled out to a DEDICATED
    # isolated Codex/Claude install to get around agentrouter.org's WAF (which
    # 401s "unauthorized client detected" at every generic HTTP client — still
    # true when re-probed on removal day). Its own test probe hung for 240s.
    # The `agentrouter_relay` flag it carried is gone with it, so no provider
    # sets it any more.
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
_ISOLATED_NPM_PACKAGE = {"claude": "@anthropic-ai/claude-code", "codex": "@openai/codex",
                         "opencode": "opencode-ai"}

_ISOLATED_INSTALL_TIMEOUT = 300   # npm install can be slow; this is an admin click, not a hop


def _isolated_root():
    """~/.free-llm-hub/isolated-clis — separate from wherever the user's own
    interactive install lives. Path only; no filesystem side effects."""
    return os.path.join(_home(), ".free-llm-hub", "isolated-clis")


# --------------------------------------------------------------------------- #
# Node bootstrap — the agent CLIs are npm packages, so no Node means no CLIs
# --------------------------------------------------------------------------- #

_NODE_FALLBACK_VERSION = "v22.14.0"     # used only if nodejs.org's index is unreachable
_NODE_DOWNLOAD_TIMEOUT = 600            # ~50MB over a slow line


def _hub_node_dir():
    return os.path.join(_home(), ".free-llm-hub", "node")


def _npm_in(node_dir):
    """The npm entry point inside an extracted official Node build, or None."""
    if not node_dir or not os.path.isdir(node_dir):
        return None
    for rel in ("npm.cmd", "npm", os.path.join("bin", "npm")):
        cand = os.path.join(node_dir, rel)
        if os.path.isfile(cand):
            return cand
    # The archive unpacks into node-vX.Y.Z-<platform>/ — accept one level down.
    try:
        for name in sorted(os.listdir(node_dir)):
            inner = os.path.join(node_dir, name)
            if os.path.isdir(inner) and name.startswith("node-"):
                found = _npm_in(inner)
                if found:
                    return found
    except OSError:
        pass
    return None


def _node_archive_name(version):
    """Official build name for THIS machine, or None if there is no build."""
    machine = (platform.machine() or "").lower()
    arm = machine in ("arm64", "aarch64")
    if sys.platform.startswith("win"):
        return "node-%s-win-%s.zip" % (version, "arm64" if arm else "x64")
    if sys.platform == "darwin":
        return "node-%s-darwin-%s.tar.gz" % (version, "arm64" if arm else "x64")
    if sys.platform.startswith("linux"):
        return "node-%s-linux-%s.tar.xz" % (version, "arm64" if arm else "x64")
    return None


def _latest_node_lts():
    """Ask nodejs.org which LTS is current, so this does not rot into a pinned
    version that stops existing. Falls back to a known-good one."""
    try:
        with urllib.request.urlopen("https://nodejs.org/dist/index.json", timeout=20) as r:
            for entry in json.loads(r.read().decode("utf-8", "replace")):
                if entry.get("lts"):
                    return entry["version"]
    except Exception:                                            # noqa: BLE001
        pass
    return _NODE_FALLBACK_VERSION


def _install_hub_node():
    """Download an official Node build into ~/.free-llm-hub/node and return its
    npm path, or None.

    Deliberately NOT a package-manager install. On Windows the Node MSI writes
    to Program Files and raises a UAC prompt, and this runs in a background
    thread at boot where nothing can answer it. On Linux `sudo apt-get` would
    block on a password nobody is there to type. An official archive unpacked
    into the hub's own folder needs no administrator, prompts nothing, changes
    no system PATH, and is deleted with the hub's config directory."""
    version = _latest_node_lts()
    name = _node_archive_name(version)
    if not name:
        _log.info("[node] no official Node build for %s/%s",
                  sys.platform, platform.machine())
        return None
    url = "https://nodejs.org/dist/%s/%s" % (version, name)
    dest = _hub_node_dir()
    os.makedirs(dest, exist_ok=True)
    archive = os.path.join(dest, name)
    _log.info("[node] downloading %s", url)
    try:
        with urllib.request.urlopen(url, timeout=_NODE_DOWNLOAD_TIMEOUT) as r, \
                open(archive, "wb") as fh:
            shutil.copyfileobj(r, fh)
        if name.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(dest)
        else:
            # tarfile handles .gz and .xz; filter="data" refuses absolute paths
            # and traversal entries on Python 3.12+, and is ignored below it.
            kwargs = {"filter": "data"} if sys.version_info >= (3, 12) else {}
            with tarfile.open(archive) as tf:
                tf.extractall(dest, **kwargs)
    except Exception as exc:                                     # noqa: BLE001
        _log.warning("[node] could not install Node: %s", exc)
        return None
    finally:
        try:
            os.remove(archive)
        except OSError:
            pass
    npm = _npm_in(dest)
    if npm:
        _log.info("[node] Node %s ready at %s", version, os.path.dirname(npm))
    return npm


def _ensure_npm(install=True):
    """npm for the hub's own installs: the user's if they have one, otherwise a
    private copy. Returns a path or None; never raises, never prompts."""
    found = shutil.which("npm")
    if found:
        return found
    found = _npm_in(_hub_node_dir())
    if found or not install:
        return found
    return _install_hub_node()


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
    """The model id(s) a sub provider exposes. sub-claude/sub-codex use the
    single "model" key (one each, by design -- the whole point is "your
    logged-in session", not model choice). A provider may instead carry a
    "models" list, in which case each entry becomes its own addressable
    '<pid>/<model>' fallback hop (_build_chain already iterates every entry
    this returns, so returning >1 here is all it takes)."""
    cfg = _SUB_PROVIDERS.get(pid)
    if not cfg:
        return []
    if "models" in cfg:
        return list(cfg["models"])
    return [cfg["model"]] if cfg.get("model") else []


def _sub_master_on():
    """The master opt-in. DEFAULT FALSE — with it off, nothing below ever runs."""
    return bool(config.get_flag(_SUB_MASTER_FLAG, False))


def _sub_bin(pid, model=None):
    """Absolute path to the CLI binary, or None. Never raises.

    When the isolated profile is ON for this provider, resolves ONLY inside its
    isolated install dir — it deliberately does NOT fall back to the shared
    PATH copy, since silently mixing the two would defeat the point of
    isolation (a "not installed" isolated provider must show as not installed,
    even if the user's regular `claude`/`codex` is right there on PATH).

    `model` is accepted (and ignored) so callers that route per-model don't
    have to special-case this."""
    cfg = _SUB_PROVIDERS.get(pid)
    if not cfg:
        return None
    bin_name, cli_id = cfg["bin"], cfg["cli_id"]
    try:
        if _sub_isolated_on(pid):
            return _isolated_bin_path(cli_id, bin_name)
        return shutil.which(bin_name)
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
    for pid in _SUB_PROVIDERS:
        enabled, _installed, authed, _detail = _sub_state(pid)
        # Fail-open across a multi-model pid: available if AT LEAST ONE of its
        # models isn't currently dead (one model 403'd out of quota doesn't
        # have to take the whole relay down -- _build_chain still only offers
        # the live ones, via _sub_models further down).
        if enabled and authed and any(not _is_model_dead(pid, m) for m in _sub_models(pid)):
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


def _sub_env(pid=None, model=None):
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
    ~/.claude or ~/.codex at all.

    `model` is accepted (and ignored) so per-model callers don't have to
    special-case this."""
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
                 "authentication_error", "session expired", "oauth",
                 # A relay's own separate quota pool (distinct from the main
                 # wallet) -- MEASURED 2026-07-29: real error is "Failed to
                 # authenticate. API Error: 403 token quota is not enough,
                 # token remain quota: $X, need quota: $Y". Same bucket as the
                 # auth failures above on purpose: both mean "not usable right
                 # now", so this reuses the existing dead-for-6h skip instead of
                 # burning a hop retrying it every request.
                 "quota is not enough", "insufficient quota")


def _sub_run(pid, prompt, model=None):
    """Run the local CLI ONCE, non-interactively. NEVER raises.

    Returns (status, text, detail) where `status` mirrors an HTTP code the chain
    loops already understand:
      200 -> `text` is the assistant's reply
      403 -> unusable (off / not installed / not signed in / loops back) -> DEAD
      413 -> prompt over _SUB_MAX_PROMPT_CHARS (request-specific, NOT dead)
      504 -> timed out       502 -> ran but failed / produced nothing

    `model` selects WHICH model for a multi-model pid; ignored for
    sub-claude/sub-codex, which only ever expose "your logged-in session".

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
    bin_name = cfg["bin"]
    path = _sub_bin(pid, model)
    if not path:
        return 403, "", "'%s' is no longer on PATH." % bin_name
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
                                          # MEASURED 2026-07-28: a fresh/isolated CODEX_HOME
                                          # tries to git-clone its plugin marketplace on
                                          # every single `exec` call -- 3 stale
                                          # plugins-clone-* dirs + a sync lock found sitting
                                          # in this isolated profile, and turning both off
                                          # cut a call that failed after ~110s down to
                                          # near-instant. Plugins are a marketplace/extension
                                          # system (`codex plugin`), unrelated to the core
                                          # file/shell/patch tools this relay actually needs,
                                          # and nothing is installed in a fresh isolated
                                          # profile for either flag to remove.
                                          "--disable", "plugins", "--disable", "remote_plugin"]
            argv += ["-o", tmp_out, "-"]
        else:
            argv = _sub_launcher(path) + ["-p", "--output-format", "text"]
        try:
            proc = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=_SUB_TIMEOUT, env=_sub_env(pid, model),
                                  cwd=tempfile.gettempdir())
        except subprocess.TimeoutExpired:
            return 504, "", "%s timed out after %ds." % (bin_name, _SUB_TIMEOUT)
        except (OSError, ValueError) as exc:
            return 502, "", "%s failed to start: %s" % (bin_name, exc.__class__.__name__)
        text = (proc.stdout or "").strip()
        if pid == "sub-codex":
            last = _read_text(tmp_out).strip()
            text = last or _codex_strip_noise(proc.stdout)
        # Claude Code can print a hard failure straight to stdout with exit 0
        # (MEASURED 2026-07-29: a relay's quota-exhausted error --
        # "Failed to authenticate. API Error: 403 token quota is not
        # enough...") rather than stderr/non-zero exit, so a naive
        # non-empty-stdout=success check would return that error text as if
        # it were a real reply. Same classification list as the exit!=0 path
        # below, just checked against stdout too when this is the claude
        # backend.
        if pid == "sub-claude" and text and any(s in text.lower() for s in _SUB_AUTH_ERR):
            return 403, "", _sanitize(text, 300)
        if not text:
            raw_err = (proc.stderr or "").strip()
            if proc.returncode != 0:
                # Classify against the FULL stderr, not a 300-char slice --
                # codex's fixed banner (workdir/model/session id/prompt echo)
                # alone regularly runs past 300 chars, so truncating BEFORE
                # classifying cut the real error off every time (MEASURED
                # 2026-07-29: a genuine AgentRouter "401 Unauthorized ...
                # quota" failure was misclassified as a generic 502 because
                # "quota"/"401" only showed up past character 300) -- silently
                # bypassing the existing dead-for-6h skip and re-running a
                # known-exhausted quota call every single time. The banner is
                # fixed-size boilerplate and the real reason always comes
                # after it, so the LAST 300 chars (not the first) is also
                # what's actually worth showing on the dashboard.
                low = raw_err.lower()
                status = 403 if any(s in low for s in _SUB_AUTH_ERR) else 502
                shown = _sanitize(raw_err[-300:] if len(raw_err) > 300 else raw_err, 300)
                return status, "", "%s exited %d: %s" % (bin_name, proc.returncode, shown or "no detail")
            return 502, "", "%s produced no output. %s" % (bin_name, _sanitize(raw_err, 300))
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
    default_model = cfg.get("model") or ((cfg.get("models") or [None])[0])
    model = payload.get("model") or default_model or "cli"
    prompt = _sub_flatten(payload.get("messages"))
    status, text, detail = _sub_run(pid, prompt, model=model)
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


# --------------------------------------------------------------------------- #
# Puter: DRIVER-based AI, not OpenAI-compatible for the tokens we can obtain.
#
# PROBED LIVE 2026-07-31 with a real popup token:
#   POST https://api.puter.com/puterai/openai/v1/chat/completions
#     -> 403 {"code":"forbidden","message":"This endpoint is only available to
#        user sessions"}   <- that surface wants a browser SESSION, and the
#        popup hands out an APP token, so it can never work from this hub.
#   POST https://api.puter.com/drivers/call
#     {"interface":"puter-chat-completion","method":"complete",
#      "args":{"messages":[...],"model":"gpt-4.1"}}
#     -> 200 {"success":true,"result":{index,message,finish_reason,usage}}
# The driver path is what puter.js itself calls for every AI feature, so it is
# the one this hub uses. With "stream":true the SAME endpoint answers
# application/x-ndjson: one {"type":"text","text":"..."} object per delta, then
# a final {"type":"usage",...}. Everything downstream in this hub speaks OpenAI
# (SSE for streams), so both directions are translated here and nowhere else.
# --------------------------------------------------------------------------- #
_PUTER_DRIVERS_URL = "https://api.puter.com/drivers/call"
_PUTER_CHAT_IFACE = "puter-chat-completion"
_PUTER_IMAGE_IFACE = "puter-image-generation"
_PUTER_IMAGE_DRIVER = "ai-image"      # the default puter.js's txt2img passes
# Sentinel image "model": Puter's image driver picks the model server-side and
# publishes no catalog, so the registry row carries the DRIVER name and this
# means "send no model arg at all".
_PUTER_IMAGE_DEFAULT_ID = "ai-image"


def _is_puter_driver(pid):
    return (prov.get_provider(pid) or {}).get("driver_api") == "puter"


def _puter_post(api_key, body, stream=False, read_timeout=None):
    """One POST to Puter's driver endpoint. Raises requests.RequestException
    exactly like the plain-HTTP path, so callers need no special handling."""
    return requests.post(
        _PUTER_DRIVERS_URL,
        json=body,
        headers={"Authorization": "Bearer " + api_key,
                 "Content-Type": "application/json"},
        stream=stream,
        timeout=(CONNECT_TIMEOUT,
                 read_timeout or (STREAM_IDLE_TIMEOUT if stream else CHAT_READ_TIMEOUT)),
    )


# --------------------------------------------------------------------------- #
# PUTER ALLOWANCE — the number the provider does not publish anywhere.
#
# MEASURED 2026-07-31 against GET https://api.puter.com/metering/usage, which
# returns {"usage": {...per-model...}, "appTotals": {...},
#          "allowanceInfo": {"remaining": N, "monthUsageAllowance": N}}.
#
# Unit calibration (this is the part no doc states): the catalog prices
# gemini-2.5-flash-image at 3.9 US cents per 1024x1024 image, and metering
# recorded exactly 3,870,000 units for one such image -> ~992,308 units per US
# cent, i.e. the units are MICRO-CENTS.
#
# Which makes the free tier: 25,000,000 units = ~25 US cents PER MONTH, shared
# across chat AND images. Not per day -- it does not come back tomorrow. For
# scale, ONE gemini image is 15% of the entire month. This is why puter must
# never be treated as an ordinary free provider: it is a trickle, and the hub
# now shows exactly how much of it is left instead of discovering it via 402s.
# --------------------------------------------------------------------------- #
_PUTER_USAGE_URL = "https://api.puter.com/metering/usage"
_PUTER_UNITS_PER_CENT = 992308.0
_PUTER_ALLOWANCE_TTL = 600          # seconds; this is a dashboard number, not a hot path
_puter_allowance_cache = {"at": 0.0, "data": None}
_puter_allowance_lock = threading.Lock()


_PUTER_WHOAMI_URL = "https://api.puter.com/whoami"


def _next_month_start_utc():
    """Epoch seconds of 00:00 UTC on the 1st of next month."""
    now = time.gmtime()
    year, month = now.tm_year, now.tm_mon + 1
    if month > 12:
        year, month = year + 1, 1
    return calendar.timegm((year, month, 1, 0, 0, 0, 0, 0, 0))


def _puter_account_uuid(key):
    """The Puter account a token belongs to, or None. Identity, not the token:
    Puter mints a FRESH token on every sign-in, so string-dedupe cannot tell a
    re-connect of the same account from a genuinely new one."""
    try:
        resp = requests.get(_PUTER_WHOAMI_URL,
                            headers={"Authorization": "Bearer " + key},
                            timeout=(CONNECT_TIMEOUT, 15))
        if resp.status_code != 200:
            return None
        uuid_ = (resp.json() or {}).get("uuid")
    except (requests.RequestException, ValueError, TypeError):
        return None
    return uuid_ if isinstance(uuid_, str) and uuid_ else None


def _puter_replace_same_account(new_key):
    """Drop any saved token belonging to the SAME account as `new_key`, so a
    re-connect REFRESHES that account instead of stacking a duplicate.

    MEASURED 2026-07-31: clicking Connect twice (with a live puter.com session,
    which makes it a one-click "continue as ...") produced two DIFFERENT tokens
    for one identical account — same uuid, both at 0¢. It looked like a pool of
    two and was one empty wallet with two cards. Returns True if it replaced.

    Fail-open: if identity can't be read, the token is simply appended — never
    block a working key over a failed lookup."""
    new_uuid = _puter_account_uuid(new_key)
    if not new_uuid:
        return False
    existing = config.list_provider_keys("puter") or []
    replaced = False
    for idx in range(len(existing) - 1, -1, -1):    # reverse: indexes stay valid
        old = existing[idx]
        if old == new_key:
            continue                                # plain dedupe already handles this
        if _puter_account_uuid(old) == new_uuid:
            config.remove_provider_key("puter", idx)
            replaced = True
    return replaced


def _puter_allowance_one(key):
    """(remaining, allowance) for ONE account token, or None."""
    try:
        resp = requests.get(_PUTER_USAGE_URL,
                            headers={"Authorization": "Bearer " + key},
                            timeout=(CONNECT_TIMEOUT, 15))
        if resp.status_code != 200:
            return None
        info = (resp.json() or {}).get("allowanceInfo") or {}
        allowance = float(info.get("monthUsageAllowance") or 0)
        remaining = float(info.get("remaining") or 0)
    except (requests.RequestException, ValueError, TypeError):
        return None
    return (remaining, allowance) if allowance > 0 else None


def _puter_allowance(force=False):
    """Pooled allowance across EVERY saved Puter account.

    Puter's documented model is user-pays: "each user will cover their own usage
    costs", so the ~25c monthly allowance belongs to the ACCOUNT, not the app.
    Several accounts therefore carry several allowances, and the hub already
    rotates a provider's key pool -- so this sums them and, critically, only
    sidelines puter when EVERY account is empty. Reading just keys[0] (as the
    first version did) would have parked a whole pool because its first token
    happened to be spent.

    Returns totals plus per-account rows, or None when nothing is readable."""
    now = time.time()
    with _puter_allowance_lock:
        hit = _puter_allowance_cache
        if not force and hit["data"] is not None and (now - hit["at"]) < _PUTER_ALLOWANCE_TTL:
            return hit["data"]
    keys = (config.get_provider_config("puter") or {}).get("api_keys") or []
    if not keys:
        return None
    accounts, remaining, allowance = [], 0.0, 0.0
    for idx, key in enumerate(keys):
        one = _puter_allowance_one(key)
        if one is None:
            continue
        r, a = one
        remaining += r
        allowance += a
        accounts.append({"index": idx, "remaining_cents": round(r / _PUTER_UNITS_PER_CENT, 2),
                         "spent": r <= 0})
    if allowance <= 0:
        return None
    data = {
        "remaining": remaining,
        "allowance": allowance,
        "remaining_cents": round(remaining / _PUTER_UNITS_PER_CENT, 2),
        "allowance_cents": round(allowance / _PUTER_UNITS_PER_CENT, 2),
        "used_pct": round(100.0 * (1.0 - remaining / allowance), 1),
        "period": "month",
        "accounts": len(accounts),
        "accounts_spent": sum(1 for a in accounts if a["spent"]),
        # Per-key rows so the card can mark WHICH saved account is empty --
        # otherwise a pooled provider just says "some of it is gone".
        "per_key": accounts,
        # Puter reports monthUsageAllowance but no reset DATE anywhere, so this
        # is the calendar-month boundary in UTC. Labelled an estimate in the UI
        # for exactly that reason: it is an inference from "month", not a
        # documented timestamp.
        "resets_at": _next_month_start_utc(),
        "resets_in": max(0, int(_next_month_start_utc() - time.time())),
        "resets_estimated": True,
    }
    with _puter_allowance_lock:
        _puter_allowance_cache["at"] = now
        _puter_allowance_cache["data"] = data
    # Spent -> sideline BEFORE the request rather than after a 402. Reuses the
    # normal dead-provider path, so the chain (and the mid-request re-check)
    # skip it exactly like any other sidelined provider.
    if remaining <= 0:
        with _provider_dead_lock:
            _dead_providers["puter"] = time.time() + _PROVIDER_DEAD_TTL
    return data


def _puter_usage(u):
    """Driver usage -> OpenAI usage. The driver reports prompt/completion but no
    total_tokens (and adds usd_cents, which is metering, not token accounting)."""
    if not isinstance(u, dict):
        return None
    try:
        pt = int(u.get("prompt_tokens") or 0)
        ct = int(u.get("completion_tokens") or 0)
    except (TypeError, ValueError):
        return None
    return {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}


def _puter_err(status, detail):
    return {"error": {"message": "Puter: %s" % (detail or "driver call failed"),
                      "type": "upstream_error", "code": status}}


class _PuterStreamResponse:
    """Response look-alike whose iter_lines() yields OpenAI SSE lines built from
    Puter's NDJSON driver stream. Same minimal surface as _SubResponse (which
    the chain loops already accept), plus real streaming.

    `deltas` is an iterable of OpenAI `delta` dicts; the buffered constructor
    passes a one-element list so a tools request (whose streamed tool-call shape
    on this driver is NOT verified) still reaches the client as one complete
    chunk instead of risking silently-dropped tool_calls."""

    def __init__(self, upstream, model, deltas, finish_reason="stop", usage=None):
        self._upstream = upstream          # None for the buffered path
        self.status_code = 200
        self.headers = {"Content-Type": "text/event-stream"}
        self.text = ""
        self._model = model
        self._deltas = deltas
        self._finish = finish_reason
        self._usage = usage

    @classmethod
    def buffered(cls, model, message, finish_reason="stop", usage=None):
        delta = {"role": "assistant"}
        if message.get("content"):
            delta["content"] = message["content"]
        if message.get("tool_calls"):
            delta["tool_calls"] = message["tool_calls"]
        return cls(None, model, [delta], finish_reason or "stop", usage)

    @classmethod
    def from_ndjson(cls, upstream, model):
        return cls(upstream, model, None)

    def _ndjson_deltas(self):
        """Translate the live NDJSON stream. Unknown event types are skipped
        rather than guessed at; a usage event is captured for the final chunk."""
        first = True
        for raw in self._upstream.iter_lines(decode_unicode=False):
            if not raw:
                continue
            try:
                ev = json.loads(raw.decode("utf-8", "replace"))
            except (ValueError, AttributeError):
                continue
            if not isinstance(ev, dict):
                continue
            kind = ev.get("type")
            if kind == "text":
                delta = {"content": ev.get("text") or ""}
                if first:
                    delta["role"] = "assistant"
                    first = False
                yield delta
            elif kind == "usage":
                self._usage = _puter_usage(ev.get("usage"))
            elif kind == "error":
                self._finish = "stop"

    def iter_lines(self, decode_unicode=False):
        cid = "chatcmpl-puter-" + uuid.uuid4().hex
        created = int(time.time())

        def _chunk(delta, finish=None):
            body = {"id": cid, "object": "chat.completion.chunk", "created": created,
                    "model": self._model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            return b"data: " + json.dumps(body).encode("utf-8")

        deltas = self._deltas if self._deltas is not None else self._ndjson_deltas()
        for delta in deltas:
            yield _chunk(delta)
        final = {"id": cid, "object": "chat.completion.chunk", "created": created,
                 "model": self._model,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": self._finish}]}
        if self._usage:
            final["usage"] = self._usage
        yield b"data: " + json.dumps(final).encode("utf-8")
        yield b"data: [DONE]"

    def iter_content(self, chunk_size=None):
        return iter(())

    def json(self):
        return {}

    def close(self):
        try:
            if self._upstream is not None:
                self._upstream.close()
        except Exception:                                    # noqa: BLE001
            pass


def _puter_chat(pid, payload, stream):
    """_upstream_chat's Puter twin: same contract (returns a response-like with
    .status_code/.json()/.text/.close(), raises RuntimeError with no key), but
    speaking the driver protocol. Rotates the key pool on the same statuses as
    the HTTP path. Never logs a token."""
    pcfg = config.get_provider_config(pid)
    keys = pcfg.get("api_keys") or []
    if not keys:
        raise RuntimeError("no api key for provider " + pid)
    model = payload.get("model")
    args = {"messages": payload.get("messages") or []}
    if model:
        args["model"] = model
    # Forwarded as-is when present; the driver ignores what it doesn't know.
    for k in ("tools", "tool_choice", "temperature", "max_tokens", "top_p"):
        if payload.get(k) is not None:
            args[k] = payload[k]
    # Tool-calls are NOT streamed here: the driver's NDJSON tool-call event
    # shape is unverified, and guessing it would silently drop tool_calls
    # mid-agentic-turn. Buffer instead and replay as one chunk (the same
    # tradeoff the sub-* hops already make).
    live_stream = bool(stream) and not payload.get("tools")
    if live_stream:
        args["stream"] = True
    body = {"interface": _PUTER_CHAT_IFACE, "method": "complete", "args": args}

    n = len(keys)
    start = _next_key_start(pid, n)
    last = None
    for i in range(n):
        is_last = (i == n - 1)
        key = keys[(start + i) % n]
        try:
            resp = _puter_post(key, body, stream=live_stream)
        except requests.RequestException:
            if is_last:
                raise
            continue
        quota.record(pid, model)
        if resp.status_code in _KEY_ROTATE_STATUSES and not is_last:
            resp.close()
            continue
        if resp.status_code != 200:
            detail = _upstream_error_detail(resp)
            resp.close()
            last = _SubResponse(resp.status_code, _puter_err(resp.status_code, detail))
            if is_last:
                return last
            continue
        if live_stream:
            return _PuterStreamResponse.from_ndjson(resp, model or "puter")
        try:
            data = resp.json() or {}
        except ValueError:
            resp.close()
            return _SubResponse(502, _puter_err(502, "non-JSON driver response"))
        resp.close()
        if not data.get("success"):
            detail = data.get("message") or data.get("error") or "driver reported failure"
            return _SubResponse(502, _puter_err(502, _sanitize(str(detail))[:300]))
        result = data.get("result")
        if not isinstance(result, dict):
            return _SubResponse(502, _puter_err(502, "driver returned no result"))
        # `result` IS an OpenAI choice (index/message/finish_reason) with usage
        # nested inside it — lift usage to the top level where OpenAI puts it.
        message = result.get("message") or {"role": "assistant", "content": ""}
        openai = {
            "id": "chatcmpl-puter-" + uuid.uuid4().hex,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model or "puter",
            "choices": [{"index": 0, "message": message,
                         "finish_reason": result.get("finish_reason") or "stop"}],
        }
        usage = _puter_usage(result.get("usage"))
        if usage:
            openai["usage"] = usage
        if stream:
            # Streaming was requested but buffered (tools present) — replay the
            # complete answer as a single SSE chunk rather than skipping the hop.
            return _PuterStreamResponse.buffered(
                model or "puter", message, result.get("finish_reason"), usage)
        return _SubResponse(200, openai)
    return last or _SubResponse(502, _puter_err(502, "no usable key"))


def _puter_image_b64(result):
    """Normalise Puter's image result to base64 payload bytes.

    The driver answers in TWO shapes depending on which upstream served it
    (MEASURED 2026-07-31): OpenAI/Gemini-backed models return a
    `data:image/...;base64,<payload>` URI, while Replicate/Together-backed ones
    return a bare https URL to the rendered file (e.g. replicate.delivery/...
    /out-0.webp). Treating the second as base64 yielded 31 bytes of garbage, so
    a URL is downloaded here — SSRF-checked exactly like _openai_generate_image.
    Returns (b64, error) with exactly one of them set."""
    if isinstance(result, dict):
        result = result.get("url") or result.get("image") or result.get("data")
    if not isinstance(result, str) or not result.strip():
        return None, "Puter returned no image data"
    result = result.strip()
    if "base64," in result:
        payload = result.split("base64,", 1)[1].strip()
        return (payload or None), (None if payload else "Puter returned an empty data URI")
    if result.startswith("http://") or result.startswith("https://"):
        if not _is_safe_external_url(result):
            return None, "Puter returned an image URL that failed the safety check"
        try:
            img = requests.get(result, timeout=(CONNECT_TIMEOUT, 60))
        except requests.RequestException as exc:
            return None, "could not download the image: %s" % exc.__class__.__name__
        if img.status_code != 200 or not img.content:
            return None, "image download failed (HTTP %d)" % img.status_code
        return _b64_bytes(img.content), None
    # Not a URI and not a URL -> assume it already IS base64.
    return result, None


def _puter_generate_image(pcfg, model, prompt, size="1024x1024", steps=4):
    """Puter text-to-image through the same driver endpoint as chat.
    `size`/`steps` are accepted for signature parity and ignored: the driver
    takes neither (verified against puter.js's txt2img binding, whose only
    positional arg is `prompt`), and it answers 1024x1024."""
    keys = pcfg.get("api_keys") or []
    if not keys:
        return 400, None, "no api key for provider puter"
    args = {"prompt": (prompt or "")[:32000]}
    body = {"interface": _PUTER_IMAGE_IFACE, "method": "generate", "args": args}
    if model and model != _PUTER_IMAGE_DEFAULT_ID:
        # Naming a driver makes `model` MANDATORY (400 "Missing `model`"), and
        # an unknown one is rejected with 400 "Model not found: X". Omitting
        # BOTH — the sentinel path — is what actually returned a PNG when
        # probed live, so the default route sends neither key.
        args["model"] = model
        body["driver"] = _PUTER_IMAGE_DRIVER
    try:
        resp = _puter_post(keys[0], body, read_timeout=CHAT_READ_TIMEOUT)
    except requests.RequestException as exc:
        return 502, None, exc.__class__.__name__
    if resp.status_code != 200:
        return resp.status_code, None, _upstream_error_detail(resp)
    try:
        data = resp.json() or {}
    except ValueError:
        return 502, None, "Puter returned a non-JSON image response"
    if not data.get("success"):
        detail = data.get("message") or data.get("error") or "image generation failed"
        return 502, None, _sanitize(str(detail))[:200]
    b64, err = _puter_image_b64(data.get("result"))
    if not b64:
        return 502, None, err or "Puter returned no image data"
    return 200, b64, None


def _dispatch_chat(pid, payload, stream):
    """Single entry point for the chain loops: a local subscription CLI for a
    sub-* hop, the HTTP upstream for everything else. Keeps the loops
    provider-agnostic and the HTTP path byte-identical to before.

    Also the one place every hop passes through, so it is where measured
    latency is taken (see _record_latency). Only NON-STREAMING hops are timed:
    for a stream this call returns once headers are in, which would measure
    time-to-first-byte and average two different quantities into one number.
    A non-2xx is not timed either -- failing fast is not being fast, and
    _reliability already covers not delivering."""
    if _is_sub(pid):
        return _subscription_chat(pid, payload)
    if stream:
        # A streaming call returns once headers are in, so there is no total
        # duration to record here -- but the clock is still worth starting:
        # _note_ttft reads it back off the response when the first real content
        # arrives, which is the number that actually describes a stream.
        started = time.perf_counter()
        resp = _upstream_chat(pid, payload, stream)
        try:
            resp._hub_started = started
        except Exception:                                        # noqa: BLE001
            pass
        return resp
    # perf_counter, not time(): this is a DURATION, so it must be monotonic (an
    # NTP step or DST jump mid-request must not record a negative or wildly
    # inflated one) AND high-resolution. time.monotonic() ticks at ~15.6ms on
    # Windows, which floors any faster hop to exactly 0.0 -- and _record_latency
    # drops a 0, so those samples would vanish and bias the average slow.
    started = time.perf_counter()
    resp = _upstream_chat(pid, payload, stream)
    try:
        if resp is not None and getattr(resp, "status_code", None) == 200:
            _record_latency(pid, (payload or {}).get("model"),
                            (time.perf_counter() - started) * 1000.0)
    except Exception:                                            # noqa: BLE001
        pass
    return resp




# Reasoning EFFORT the manager assigns per task difficulty. A simple question gets
# minimal thinking (fast); a hard task gets more. Applied ONLY to reasoning models
# (non-reasoning models ignore it). This is what makes "the manager decides the
# effort by the question" real — and it also overrides whatever a client (e.g.
# Codex) hard-coded, since the hub, not the CLI, knows the task.
_DIFFICULTY_EFFORT = {"simple": "low", "medium": "medium", "hard": "high"}


# (output-token budget, HIGHEST effort still allowed below it), strictest last.
_EFFORT_MIN_BUDGET = ((2000, "medium"), (800, "low"))


def _apply_reasoning_effort(payload, model, difficulty):
    """For a reasoning model, set reasoning_effort from the task difficulty so easy
    questions answer fast and hard ones think more. No-op for non-reasoning models.

    The effort is CAPPED by the output budget. Thinking is spent from the same
    max_tokens as the answer, so 'high' effort on a small budget makes the model
    burn the whole allowance reasoning and return finish_reason='length' with EMPTY
    content — which the chain then discards as a dead hop. That silently knocked the
    deep-quota reasoning workhorses (gpt-oss-120b, glm-4.7) out of every heavy turn
    and funnelled the work onto whichever non-reasoning model was left."""
    if not (difficulty and _SLOW_MODEL_RE.search((model or "").lower())):
        return payload
    effort = _DIFFICULTY_EFFORT.get(difficulty, "medium")
    try:
        budget = int(payload.get("max_tokens") or 0)
    except (TypeError, ValueError):
        budget = 0
    if budget > 0:
        order = {"low": 0, "medium": 1, "high": 2}
        for need, cap in _EFFORT_MIN_BUDGET:
            if budget < need and order.get(effort, 1) > order[cap]:
                effort = cap
    payload["reasoning_effort"] = effort
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
_ORCH_CHEAP_BAND = 10.0   # tight band for light sub-tasks: rotate only among the
                          # near-equal-cost strong coders, so the scarce top models
                          # stay saved for the heavy turns.
_orch_cursor = 0
_orch_cheap_cursor = 0
_orch_lock = threading.Lock()


def _spread_band(pool):
    """The top-tier band of `pool`, best first. The band top is computed from
    NATURAL scores only: a preference floor (hy3 135 / kimi-k3 134 / puter
    gpt-5.6-sol 136 / kimi-k2.6-k2.7 133) is a thumb on
    the scale, not a measurement, and letting it define the top drags the cutoff up
    ~27 points and collapses the band to one or two ids — which is exactly how a
    single model ends up serving a whole project."""
    if not pool:
        return []
    scores = sorted({p[0] for p in pool}, reverse=True)
    natural = [s for s in scores if s not in _PREF_FLOORS]
    top = natural[0] if natural else scores[0]
    band = [p for p in pool if p[0] >= top - _ORCH_BAND]
    # Quality first; among EQUAL-benchmark models prefer the one with more free
    # budget left (headroom never outranks quality — see _quota_headroom).
    band.sort(key=lambda t: (-_agentic_score(t), -round(_quota_headroom(t[1]), 1), t[1], t[2]))
    return band


def _spread_pick(pool):
    """pool = [(score, pid, model)] of candidates. Return one top-tier entry,
    rotating across the band so consecutive agentic turns land on DIFFERENT strong
    providers — one project then draws on the strength of MANY good models instead
    of draining the single best one. Exhausted models are filtered out upstream."""
    global _orch_cursor
    band = _spread_band(pool)
    if not band:
        return None
    lead = band[0]
    # The lead keeps DOUBLE weight only while it still has budget. Once it is
    # nearly drained it falls back to a normal single slot — never dropped (quality
    # still wins), it just stops being consumed at twice everyone else's rate.
    ring = ([lead] + band) if _quota_headroom(lead[1]) >= 0.25 else band
    with _orch_lock:
        pick = ring[_orch_cursor % len(ring)]
        _orch_cursor += 1
    return pick


def _spread_pick_cheap(pool):
    """Rotate across the CHEAP band — the near-equal-cost strong coders just above
    the agentic floor — for lighter sub-tasks. Deliberately tight (_ORCH_CHEAP_BAND)
    so the scarce top-tier models stay reserved for the heavy turns, and on its own
    cursor so it can't phase-shift the hard-turn rotation."""
    global _orch_cheap_cursor
    if not pool:
        return None
    base = min(p[0] for p in pool)
    band = [p for p in pool if p[0] <= base + _ORCH_CHEAP_BAND]
    # cheapest first, then most free budget among equals
    band.sort(key=lambda t: (t[0], -round(_quota_headroom(t[1]), 1), t[1], t[2]))
    with _orch_lock:
        pick = band[_orch_cheap_cursor % len(band)]
        _orch_cheap_cursor += 1
    return pick


# Session affinity state: conversation-key -> (pid, model, expires_at).
_SESSION_PIN_TTL = 4 * 3600     # a coding session comfortably outlives this
_session_pins = {}
_session_pin_lock = threading.Lock()


def _session_key(messages):
    """Stable id for a CONVERSATION, derived from the parts that do not change
    as it grows: the system prompt plus the FIRST user turn. Later turns append
    history, so hashing the whole thing would mint a new key every turn (which
    is exactly the per-turn re-routing we're fixing). Returns None when there is
    nothing stable to key on — then no pinning happens and behaviour is as before."""
    try:
        system_txt = ""
        first_user = ""
        for m in messages or []:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            content = m.get("content")
            if isinstance(content, list):      # content-parts -> join their text
                content = "".join(p.get("text") or "" for p in content
                                  if isinstance(p, dict))
            if not isinstance(content, str):
                continue
            if role == "system" and not system_txt:
                system_txt = content
            elif role == "user" and not first_user:
                first_user = content
            if system_txt and first_user:
                break
        basis = (system_txt[:4000] + "\x00" + first_user[:4000]).strip()
        if len(basis) < 16:                    # too little to identify a session
            return None
        return hashlib.sha256(basis.encode("utf-8", "ignore")).hexdigest()[:20]
    except Exception:
        return None


_SAME_HOST_EPS = 0.02   # headroom fractions this close count as tied
# How far a host may score BELOW the best host of the same model and still take
# an equal share of it. Sized deliberately between the two things that separate
# hosts of identical weights:
#   provider bias      0.0 .. 2.0  (cerebras 2.0, groq 1.8, nvidia 1.2, ...)
#   _RELAY_DISCOUNT    4.0         (a scraped relay's claimed id)
# So first-party hosts a point or two apart still SHARE -- which is the whole
# point of this function, and what stopped one provider's daily cap being
# drained while an identical copy sat idle -- while a relay four points down
# stops taking an equal cut of a model a real host also serves.
_SAME_HOST_BAND = 3.0


def _pick_same_model_host(pool, pinned):
    """Which provider should serve the pinned MODEL on this turn.

    `pinned` is the (pid, model) this conversation started on. Returns a
    (pid, model) from `pool` serving the same underlying model — preferring the
    host with the most free budget LEFT, and choosing at random among hosts that
    are effectively tied so the load genuinely alternates instead of always
    landing on whichever provider happens to sort first.

    Returns None when the model is no longer a live candidate at all, which is
    the caller's signal to re-pick and re-pin from scratch.

    Only same-identity hosts are ever considered, so this can never change WHICH
    model answers — the conversation stays on identical weights, and every
    candidate here has already passed the availability, tool-capability and
    context-size filters that built `pool`."""
    ident = _normalize_model_identity(pinned[1])
    hosts = [(c[1], c[2]) for c in pool
             if _normalize_model_identity(c[2]) == ident]
    if not hosts:
        return None
    if len(hosts) == 1:
        return hosts[0]
    best = max(_quota_headroom(p) for p, _m in hosts)
    tied = [h for h in hosts if _quota_headroom(h[0]) >= best - _SAME_HOST_EPS]
    # Headroom alone treats every host of a model as interchangeable, and
    # _quota_headroom returns 1.0 for every provider whose limit is unknown --
    # which is most of them -- so nearly all hosts tie and the choice was a
    # coin flip weighted by how many times a provider happens to LIST the
    # model. MEASURED 2026-08-30: a creation ask sent 24 of 30 turns to g4f
    # relays of kimi-k3 (130.8) rather than nvidia's first-party copy (134.8),
    # purely because g4f lists it four times and nvidia once. That silently
    # defeated the relay discount, whose entire job is to stop a scraped proxy
    # outranking a real host of the same weights.
    #
    # So: among headroom-tied hosts, prefer the better-SCORING one, and only
    # roll the dice between hosts tied on both. Spreading still happens where
    # the hosts really are equivalent, which is what it was for.
    if len(tied) > 1:
        top = max(_benchmark_score(p, m) for p, m in tied)
        tied = [h for h in tied
                if _benchmark_score(h[0], h[1]) >= top - _SAME_HOST_BAND] or tied
    return random.choice(tied) if len(tied) > 1 else tied[0]


def _session_pin_get(key):
    """(pid, model) pinned to this conversation, or None. Expired pins are dropped."""
    if not key:
        return None
    now = time.time()
    with _session_pin_lock:
        row = _session_pins.get(key)
        if not row:
            return None
        pid, model, exp = row
        if exp <= now:
            _session_pins.pop(key, None)
            return None
        return (pid, model)


def _session_pin_set(key, pid, model):
    """Pin this conversation to (pid, model). Also opportunistically evicts
    expired rows so the dict cannot grow without bound in a long-lived process."""
    if not (key and pid and model):
        return
    now = time.time()
    with _session_pin_lock:
        if len(_session_pins) > 512:
            for k, (_p, _m, exp) in list(_session_pins.items()):
                if exp <= now:
                    _session_pins.pop(k, None)
        _session_pins[key] = (pid, model, now + _SESSION_PIN_TTL)


_AGENTIC_PICK_TEMPERATURE = 5.0  # score points at which weight roughly e-folds


def _weighted_pick(pool, sustain_override=None):
    """Pick one (score, pid, model) from `pool` with OpenRouter's own approach to
    provider selection, not a strict argmax: OpenRouter's documented default
    routing weights stable providers by the INVERSE SQUARE of price (a provider
    3x cheaper gets 9x the traffic, not 100%) rather than always sending every
    request to the single cheapest one. This is that idea applied to quality
    instead of price — softmax over _agentic_score at a temperature where a
    close competitor (a few points back) wins a real, meaningful minority of
    picks, while one 20+ points back is picked only rarely.
    Deterministic max() (tried first this session) starved every provider but
    the single highest scorer, every NEW session, forever — confirmed live via
    /api/activity showing nvidia picked as the sole primary across every fresh
    request. Wide-band rotation (tried before that) mixed genuinely different-
    strength models turn to turn on the SAME build and produced incoherent
    output — this sits between the two. Session pinning (the caller, unchanged)
    still keeps one session on whichever model this returns.

    `sustain_override` — see _model_identity_min_penalty — lets a scarce-quota
    copy of a model that also exists uncapped elsewhere in `pool` compete on
    the uncapped copy's penalty instead of its own."""
    if len(pool) <= 1:
        return pool[0]
    scores = [_agentic_score(c, sustain_override) for c in pool]
    best = max(scores)
    weights = [math.exp((s - best) / _AGENTIC_PICK_TEMPERATURE)
               * (0.5 + 0.5 * _quota_headroom(c[1]))
               for c, s in zip(pool, scores)]
    total = sum(weights)
    if total <= 0:
        return max(pool, key=lambda t: (_agentic_score(t, sustain_override), _quota_headroom(t[1])))
    r = random.random() * total
    acc = 0.0
    for c, w in zip(pool, weights):
        acc += w
        if r <= acc:
            return c
    return pool[-1]  # float-rounding fallback


def _route_by_difficulty(messages, max_tokens=None, est=None, require_tools=False,
                         force_difficulty=None, quality_mode=False):
    """Pick (pid, model) by task difficulty across AVAILABLE providers that can
    also HANDLE the request size (skip small-TPM providers for big requests).
    - hard/medium -> strongest capable model.
    - simple      -> cheapest capable model clearing the tier floor.
    Returns (None, None, difficulty) if nothing is ready (caller falls back).

    `force_difficulty` overrides the classifier for callers who KNOW the tier
    the text alone can't reveal — prompt enhancement is a short instruction-
    following job that classifies `simple` yet needs a model strong enough to
    obey "rewrite, don't answer" (a weak one replies to the prompt instead,
    replacing the user's question with an answer)."""
    difficulty = force_difficulty or _classify_difficulty(messages, max_tokens)
    # MAX-QUALITY MODE. A CLI session started in "max quality" is launched with
    # ANTHROPIC_MODEL=best (see agentic_chat._agentic_env), so every turn it
    # sends carries model="best": route exactly like `auto`, but never drop to
    # the cheap tier -- not even on the small steps an agent takes between the
    # big ones, where a weak model quietly costs a retry.
    #
    # This is what the CLI question offers INSTEAD of a swarm choice. The swarm
    # cannot serve a tool-calling loop at all: it emits finished prose, never
    # tool calls, and _swarm_completion refuses those turns outright -- so a
    # "swarm mode" CLI session would write no files and do nothing. Forcing the
    # top tier is the working version of "use the best models".
    if quality_mode and difficulty == "simple":
        difficulty = "medium"
    # CREATION ALWAYS GETS THE BEST MODELS (see _CREATION_INTENT_RE). Skipped
    # when force_difficulty is set: that caller already knows the tier it needs,
    # and the hub's own internal probes use it precisely to stay off the strong
    # providers -- lifting those would drain top-tier quota on housekeeping.
    if not force_difficulty and difficulty != "hard":
        if _CREATION_INTENT_RE.search(_latest_user_text(messages) or ""):
            difficulty = "hard"
    if est is None:
        est = _est_tokens(messages)
    providers = [p for p in _available_providers() if _provider_capable(p, est)]
    if not providers:  # request too big for every free tier -> try the biggest anyway
        providers = sorted(_available_providers(), key=_provider_tpm, reverse=True)
    providers = _exclude_google_for_foreign_tool_history(providers, require_tools, messages)
    # Each _auto_models(pid) can be a live, network-bound provider /models
    # fetch on a cold or expired cache entry. MEASURED, chasing a report of a
    # 12-minute agentic turn that came back with nothing: 20 providers,
    # sequential, ~1s each -- a genuine ~20-30s stall on the FIRST routing
    # decision after a restart, or any call more than the 60s cache TTL after
    # the last one. Independent per provider (provider_free_models's cache is
    # keyed per-pid), so there is no shared state to race by fetching them
    # concurrently instead of one at a time -- only wall-clock time to save.
    models_by_pid = _prefetch_auto_models(providers)
    cands = []  # (score, pid, model)
    for pid in providers:
        for m in models_by_pid.get(pid, ()):
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
    # NOTE: the fast-only prefilter is skipped for AGENTIC (tools) turns. A coding
    # agent waits on quality, not on first-token latency, and the speed heuristic
    # scores several of the strongest sustainable coders (openrouter's hy3,
    # gpt-oss-120b) as "slow" — filtering them here removed them from the agentic
    # pool ENTIRELY, before the strength floor below ever saw them, which is a big
    # part of why one model ended up serving everything.
    # ...and skipped for HARD chat turns too, for the same reason. Asked why the
    # dashboard chat does not use the best models "as in CLI mode", this is the
    # other half of the answer (the first half was classification): a CLI turn
    # sets require_tools and sees every candidate, while a chat turn asking for
    # a whole website saw only the ones the speed heuristic likes. Someone who
    # just asked for an e-commerce site is waiting on the ANSWER, not on the
    # first token. Simple and medium keep the fast preference, where the
    # strength difference is small and latency is what you actually feel.
    _fast_only = not require_tools and difficulty != "hard"
    pool = ([c for c in cands if _is_fast(c[1], c[2])] or cands) if _fast_only else cands
    if require_tools:
        # CODING/AGENTIC: the primary is ALWAYS a STRONG model (>= _TOOLS_MIN_SCORE) —
        # a weak model plans then under-builds, and mistral (56) only ever appears
        # later in the fallback CHAIN once these are exhausted, never as the primary.
        # Fail-open: if nothing clears the bar (all strong keys weak/exhausted), keep
        # the full pool rather than fail.
        agentic = [c for c in pool if c[0] >= _TOOLS_MIN_SCORE] or pool
        # ════════════════════════════════════════════════════════════════════
        # SESSION AFFINITY — one model per TASK, rotation only between tasks.
        # Rotating per-TURN spread the load nicely but wrecked the OUTPUT: a
        # coding agent builds a landing page over many turns, and each turn was
        # answered by a different model with no idea what the previous one chose.
        # Turn 1 writes one CSS approach, turn 2 restructures it another way,
        # turn 3 changes it again -> incoherent, "fucked" result even though
        # every individual model was strong and every request returned 200.
        # So: pin the first model a conversation lands on and keep using it for
        # that conversation. Quota spreading still happens — across SESSIONS,
        # which is where it belongs — and if the pinned model becomes
        # unavailable (throttled/dead/too small) it simply is not in `agentic`
        # any more and we re-pick + re-pin. Nothing is ever forced.
        # ════════════════════════════════════════════════════════════════════
        _skey = _session_key(messages)
        _pinned = _session_pin_get(_skey)
        if _pinned:
            # Keep an in-progress task on the model it started on — but pin the
            # MODEL, not the provider. Two providers serving the SAME model id
            # (nvidia and g4f-nvidia both list z-ai/glm-5.2; groq, nvidia,
            # g4f-groq and g4f-nvidia all list openai/gpt-oss-120b — 94 such
            # models are live right now) return the same weights and the same
            # behaviour, so hopping between them costs NOTHING in coherence,
            # which is the only reason the pin exists. Pinning the pair instead
            # meant one long conversation drained a single provider's daily
            # quota while an identical copy sat unused: 18 of 23 turns went to
            # nvidia/z-ai/glm-5.2 while g4f-nvidia served the same model.
            # Now every same-model host shares the load, so the effective daily
            # budget for that model is the SUM of their quotas, not one of them.
            _host = _pick_same_model_host(agentic, _pinned)
            if _host:
                if _host != _pinned:
                    _session_pin_set(_skey, _host[0], _host[1])
                return _host[0], _host[1], difficulty
        # NOTE: an "AGENTROUTER FIRST" block sat here until 2026-07-31 — it
        # tried the AgentRouter relay's paid models BEFORE the free tier on
        # every fresh coding task. It went with the relay itself (removed at
        # user request); agentic tasks now go straight to the weighted
        # free-tier pick below, as they did before that block existed.
        # WEIGHTED pick, not strict argmax — see _weighted_pick. `agentic` has
        # ALREADY been filtered to models that are available, not throttled, not
        # exhausted, tool-capable and big enough for this request — so this picks
        # from "the models that can actually serve this request right now",
        # strongly favoring the best one without starving every close competitor.
        # Scarce tiers (openrouter 50/day) and de-prioritised families (qwen) stay
        # demoted through _agentic_score, so the weighting still respects those.
        # Prefer models PROVEN to drive a CLI to completion. Strength only counts
        # if the CLI can actually execute what the model emits — a brilliant model
        # whose tool payloads codex rejects builds nothing at all (measured: three
        # separate runs, three different silent failures, zero files each time).
        # Fail-OPEN: if none of the proven models can serve this request right now,
        # fall back to the full agentic pool rather than refusing to answer.
        _proven = [c for c in agentic if _may_lead_agentic(c[0], c[2])]
        _pool = _proven or agentic
        # LOW-QUALITY TAIL (see _LOW_QUALITY_RE): the proven list still names
        # nemotron/gpt-oss from the 2026-07-25 dialect evidence, so proven-first
        # alone made a demoted family the agentic PRIMARY whenever it was alive —
        # exactly the cascade the user reported. They may only serve a tool
        # request once every stronger alive candidate has been tried: pick from
        # the normal candidates only, widening back to the full agentic pool
        # when the proven subset held nothing but last-resort families, and
        # failing open to the low-quality pool only when nothing else lives.
        _normal = [c for c in _pool if not _is_low_quality(c[2])]
        if not _normal:
            _normal = [c for c in agentic if not _is_low_quality(c[2])]
        # ...but "not empty" was too weak a bar, and the pool COLLAPSED instead
        # of narrowing. MEASURED 2026-08-07, one funnel over the live fleet:
        #     alive + tool-capable        660 models / 21 providers
        #     ∩ _TOOL_PROVEN              132 models / 13 providers
        #     ∩ not low-quality            31 models /  3 providers
        # _TOOL_PROVEN names gpt-oss and nemotron, _LOW_QUALITY_RE demotes
        # exactly those, so the intersection is essentially "gemini-3" -- and
        # 14 sampled routes across 14 DISTINCT conversations returned just two
        # ids, both google/gemini-3.x. The fail-open above never fired because
        # 31 models is not zero. A one-family monopoly is not what proven-first
        # was for, and it wastes 20 other providers' quota.
        #
        # So widen on PROVIDER DIVERSITY, not just emptiness. Safe to widen now
        # in a way it would not have been in July: _TOOL_PROVEN is a hand-typed
        # allowlist that stood in for a feedback signal we did not have, and we
        # now measure the real thing -- a model that fails here earns a lasting
        # reliability penalty (see _record_outcome) and a 6h dead-model sideline
        # on 402/403/404/410, both of which _agentic_score already folds in. So
        # a listing a provider cannot actually serve costs ONE hop and then
        # sinks itself, instead of being pre-banned by a stale list.
        if len({c[1] for c in _normal}) < _MIN_AGENTIC_PROVIDERS:
            _wider = [c for c in agentic if not _is_low_quality(c[2])]
            if len({c[1] for c in _wider}) > len({c[1] for c in _normal}):
                _normal = _wider
        _pool = _normal or _pool
        picked = _weighted_pick(_pool, _model_identity_min_penalty(_pool))
        _s, pid, model = picked
        _session_pin_set(_skey, pid, model)
        return pid, model, difficulty
    # ════════════════════════════════════════════════════════════════════════
    # CONVERSATION PIN FOR PLAIN CHAT. Agentic turns have had this since the
    # coding agent produced incoherent work by answering each turn with a
    # different model; a chat conversation has the same problem in a smaller
    # way. The history is always forwarded, so nothing is FORGOTTEN when the
    # model changes -- but the voice, the format and the opinions do change
    # mid-conversation, which reads as the assistant contradicting itself.
    #
    # Pinned across every later turn regardless of tier: once a conversation is
    # under way, a one-word follow-up is still part of THAT conversation, and
    # dropping to a cheap model for it is exactly the switch this prevents. The
    # pin is never forced -- a model that is throttled, dead or too small for
    # the request is not in `pool`, so it simply re-picks and re-pins.
    _ckey = _session_key(messages)
    if not require_tools and _ckey:
        _cpin = _session_pin_get(_ckey)
        if _cpin:
            _host = _pick_same_model_host(pool, _cpin)
            if _host:
                return _host[0], _host[1], difficulty
    if difficulty != "simple":
        # LOW-QUALITY TAIL (see _LOW_QUALITY_RE): nemotron/gpt-oss/gemma are the
        # last resort for medium/hard too — only a SIMPLE ask may route to them
        # while a stronger candidate is alive. Fail-open when they are all that
        # lives (nothing else keyed/fresh) so the request still gets served.
        pool = [c for c in pool if not _is_low_quality(c[2])] or pool
    if difficulty in ("hard", "medium"):
        # BEST-EXCEPT-TRIVIAL (user choice 2026-07-31): medium joins hard on the
        # strongest-model path instead of taking the old "cheapest model that
        # clears _DIFFICULTY_FLOOR" route. Rationale: the floor bought quota
        # savings by deliberately answering ordinary questions with a weaker
        # model, which is the opposite of what this hub is for.
        # `simple` deliberately KEEPS the cheap path: that tier is one-word
        # replies, classification, and the hub's OWN internal probes
        # (difficulty classification, prompt enhancement, health checks) —
        # spending top-tier quota there buys no quality and drains the strong
        # providers before real work reaches them.
        # ALWAYS-BEST vs SPREAD (user choice 2026-07-31, default always-best).
        # _spread_pick rotates across the top BAND so consecutive turns land on
        # different strong providers — one project then draws on many good models
        # instead of draining the single best one. That trades a little quality
        # per turn for more total capacity, and it is why the strongest available
        # model was often NOT the one that answered (measured: glm-5.2 at 135 sat
        # in the pool while a 121 served the turn).
        # The user asked repeatedly for the best model every time, so that is the
        # default; the flag restores the spreading behaviour for anyone who would
        # rather stretch their quota further.
        if config.get_flag("route_always_best", True):
            picked = max(pool, key=lambda t: (t[0], _quota_headroom(t[1])))
        else:
            picked = _spread_pick(pool) or max(pool, key=lambda t: (t[0], _quota_headroom(t[1])))
        _s, pid, model = picked
        # Remember it for the rest of THIS conversation (see the pin block
        # above). Only real work pins: a conversation that opens with "hi"
        # must not spend the rest of its life on whatever answered that.
        if not require_tools:
            _session_pin_set(_ckey, pid, model)
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
    return (not model) or model in ("auto", "orchestrate", "default", "best") \
        or model.startswith("claude") \
        or model in _CLAUDE_MODEL_ALIASES


# Claude Code's OWN short model names -- what `--model opus` on the real CLI
# actually sends, and what "opus"/"sonnet"/"haiku" mean to any Anthropic-API
# client, not just this hub's own isolated sessions. MEASURED, and the reason
# this exists: with no special case, "opus" is neither "auto" nor
# "claude"-prefixed, so _resolve_model("opus") fell through to a literal
# passthrough lookup -- ('groq', 'opus') -- a model that does not exist on
# Groq or anywhere else. The isolated claude fallback (agentic_chat.py) sends
# this UNCONDITIONALLY on every turn (Anthropic's own CLI reference: --model
# is not remembered across --resume, so it is resent every time), and the
# request against a nonexistent literal model did not fail fast -- it hung,
# with nothing ever coming back, for the full length of a 12-minute real
# session before the caller gave up with no reply at all.
_CLAUDE_MODEL_ALIASES = ("opus", "sonnet", "haiku", "opusplan")


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


def _rotate_band(ordered):
    """Rotate the TOP BAND of a strength-ordered candidate list so consecutive
    requests don't all fall back onto the same model. Everything below the band
    keeps its exact order — only which of the equally-strong models gets the first
    fallback slot varies. Uses the shared spread cursor WITHOUT advancing it, so it
    tracks the primary rotation instead of fighting it."""
    if len(ordered) < 3:
        return ordered
    band = _spread_band(ordered)
    if len(band) < 2:
        return ordered
    keys = {(e[1], e[2]) for e in band}
    rest = [e for e in ordered if (e[1], e[2]) not in keys]
    with _orch_lock:
        off = _orch_cursor % len(band)
    return band[off:] + band[:off] + rest


def _interleave_by_provider(ordered):
    """Re-arrange a strength-sorted [(score, pid, model), ...] list so
    consecutive entries favor DIFFERENT providers: round 1 = each provider's
    best model (providers visited in the order their own best entry ranks —
    already the case since `ordered` arrives strength-sorted and this only
    reorders INTER-provider adjacency), round 2 = each provider's 2nd-best,
    etc. Each provider's own internal strength order is untouched.
    MEASURED 2026-07-27: a real agentic chain had 3 straight nvidia hops
    (nemotron-3-ultra timeout -> kimi-k2.6 404 -> glm-5.2) while 13 other
    available, fresh providers (cerebras 14,400/day, groq 1000/day, ...) sat
    untried, because nvidia's catalog alone had 3+ models that outranked every
    other provider's best — the flat sort this replaces had no cross-provider
    diversity despite _build_chain's own docstring claiming it did. Without
    this, one provider's account-level outage/429 can eat several consecutive
    fallback hops before a healthy provider is ever reached."""
    buckets, order_by_pid = {}, []
    for e in ordered:
        pid = e[1]
        if pid not in buckets:
            buckets[pid] = []
            order_by_pid.append(pid)
        buckets[pid].append(e)
    result = []
    round_idx = 0
    while len(result) < len(ordered):
        for pid in order_by_pid:
            b = buckets[pid]
            if round_idx < len(b):
                result.append(b[round_idx])
        round_idx += 1
    return result


def _history_has_tool_calls(messages):
    """True if any prior assistant turn in this conversation already made a tool
    call. Google's Gemini API rejects a request outright (400: "Function call is
    missing a thought_signature") whenever a tool_calls message in history lacks
    Gemini's own signing token — which is guaranteed for a tool call that came
    from a DIFFERENT model (glm/deepseek/nvidia/... fallback hop, or a session
    that re-picked away from Gemini and back). The hub doesn't track per-message
    provenance, so any prior tool call at all is treated as a foreign one and
    Google is excluded from the candidate pool for that request — a deterministic
    hard-fail, not a soft dialect mismatch like _TOOL_DIALECT_MISMATCH, so this is
    a hard exclusion rather than a ranking penalty. MEASURED live 2026-07-27."""
    for m in messages or []:
        if isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls"):
            return True
    return False


def _exclude_google_for_foreign_tool_history(pids, require_tools, messages):
    """Drop 'google' from a candidate provider list when this is a tool-calling
    continuation (see _history_has_tool_calls) — fail-open if that would empty
    the pool (google is the only option left)."""
    if not require_tools or "google" not in pids or not _history_has_tool_calls(messages):
        return pids
    filtered = [p for p in pids if p != "google"]
    return filtered or pids


def _excluded_identities(header_value):
    """Parse an X-Free-LLM-Hub-Exclude header into a set of model IDENTITIES.

    Accepts a comma-separated list of either "<pid>/<model>" or a bare model id.
    Matching is by _normalize_model_identity, i.e. the LEAF name, so excluding
    "groq/openai/gpt-oss-120b" also excludes cerebras' bare "gpt-oss-120b" and
    cloudflare's "@cf/openai/gpt-oss-120b" -- the same weights under three
    spellings. Anyone asking for a different MODEL means a different model, not
    the same one from another host."""
    out = set()
    for raw in str(header_value or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        # A "<pid>/<model>" prefix is stripped by the leaf rule anyway; this
        # just means both spellings a caller might send are accepted.
        ident = _normalize_model_identity(raw)
        if ident:
            out.add(ident)
    return out


# NAMED CHAINS -- an ordered list of models saved under a name and selected as a
# model id ("coding", "cheap", "uncensored").
#
# The hub already had category BUTTONS, which are a different thing: a category
# is a SET used to filter what routing may consider, and the order inside it is
# still the benchmark's. A chain is an ORDER the user asserts -- try this exact
# model, then that one -- which is the only way to express "I know this pairing
# works for my project" or "this one first, but never leave me stuck".
#
# It is a preference, not a cage: the ordinary chain is appended after the named
# entries, so a chain whose models are all rate-limited degrades to normal
# routing instead of failing. A chain that could dead-end would be worse than no
# chain, because it fails exactly when everything is busiest.
_CHAINS_KEY = "chains"


def _named_chains():
    raw = config.get_json(_CHAINS_KEY, {}) or {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for name, ids in raw.items():
        if isinstance(name, str) and name and isinstance(ids, list):
            out[name] = [str(i) for i in ids if isinstance(i, str) and i]
    return out


def _is_chain_name(model):
    return bool(model) and str(model).strip().lower() in _named_chains()


def _chain_entries(name):
    """[(pid, model)] for a named chain, dropping ids that are not available.

    Filtered against the live catalog rather than trusted: a chain saved months
    ago naming a model a provider has since withdrawn must not put a dead hop at
    the front of every request."""
    ids = _named_chains().get(str(name).strip().lower()) or []
    live = {m["id"] for m in aggregated_models()}
    out = []
    for ident in ids:
        if ident not in live or "/" not in ident:
            continue
        pid, model = ident.split("/", 1)
        if _is_model_dead(pid, model):
            continue
        out.append((pid, model))
    return out


def _build_chain(primary_pid, model_id, est=0, require_vision=False, require_tools=False,
                  messages=None, exclude_identities=None, prefer=None):
    """Priority-ordered [(pid, model)] fallback chain. Primary first, then the
    next-best MODELS across every AVAILABLE, size-capable provider, INTERLEAVED
    across providers (best model of each provider, then each provider's 2nd, ...).
    So if the chosen model is rate-limited (429) the gateway auto-switches to the
    next model in priority: a different PROVIDER first (handles per-account limits
    like NVIDIA), while later rounds still try other models of the same provider
    (handles per-model limits like Groq). Size-incapable providers are skipped so
    a big request never falls onto one that will 413. Capped at MAX_HOPS."""
    # A NAMED CHAIN's entries go in front of everything, in the order the user
    # wrote them -- that order IS the feature. They still pass through the same
    # veto and de-duplication below, so "retry with a different model" keeps
    # working against a named chain, and the ordinary chain still follows so the
    # request cannot dead-end.
    _preferred = [tuple(x) for x in (prefer or []) if x]
    # Caller-vetoed models ("retry with a different model"): drop them before the
    # primary is even seeded, or the one model the user just rejected would be
    # tried FIRST on the retry. Never widened silently -- an empty result falls
    # through to the normal "none available" path rather than quietly serving
    # the excluded model anyway.
    _veto = set(exclude_identities or ())
    if (not primary_pid or not model_id
            or (_veto and _normalize_model_identity(model_id) in _veto)):
        # No primary to seed: either the caller has none to offer (it wants the
        # chain to choose one), or the one it had is exactly what the user just
        # rejected. Either way the ranked candidates below become the chain.
        chain, seen = [], set()
    else:
        chain = [(primary_pid, model_id)]
        seen = {(primary_pid, model_id)}
    # Split every available, size-capable candidate into FAST and SLOW tiers.
    # FAST models are tried first (best-first); SLOW reasoning models are the LAST
    # resort — only reached once the fast+good ones are exhausted/rate-limited.
    fast, slow = [], []
    _cand_pids = _exclude_google_for_foreign_tool_history(
        _available_providers(), require_tools, messages)
    for pid in _cand_pids:
        if not _provider_capable(pid, est):
            continue
        for m in _auto_models(pid):
            if (pid, m) in seen or not prov.is_model_allowed(m) or _is_model_dead(pid, m):
                continue
            if _veto and _normalize_model_identity(m) in _veto:
                continue          # caller asked for a DIFFERENT model than this
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
        _sustain_map = _model_identity_min_penalty(fast + slow)
        ordered = sorted(fast + slow,
                         key=lambda t: (_agentic_score(t, _sustain_map), _quota_headroom(t[1])),
                         reverse=True)
        ordered = [e for e in ordered if _supports_tools(e[1], e[2])] or ordered
        # INTERLEAVE by provider — see _interleave_by_provider. _rotate_band used to
        # run here too ("spread which model gets the first fallback slot"), but its
        # _ORCH_BAND=30 is wide enough to sweep in MOST of a 100+ candidate pool (not
        # the small near-tied handful it was designed for), so rotating it after
        # interleaving reshuffled almost the whole list on every call — MEASURED
        # 2026-07-27: openrouter's nemotron-3-ultra survived every step up through
        # interleaving, then vanished from the final 10-hop chain entirely, purely
        # because that rotation's offset happened to push it past the hop cap.
        # _rotate_band's original job (don't let the same model own hop 2 forever,
        # vary it across separate requests) is now handled more precisely by THIS
        # interleaving (guarantees a different provider, not just a different roll of
        # the dice) plus _weighted_pick on the primary — removed here, not narrowed,
        # since a narrower band would still fight interleaving's ordering somewhat.
        ordered = _interleave_by_provider(ordered)
        # PROVEN-first, same fail-open allowlist _route_by_difficulty already
        # applies to the primary pick — but until now ONLY to the primary. The
        # fallback chain built above ranks by raw strength alone, so an unproven
        # model that happens to benchmark high (glm-5.2 / kimi-k2.6, both above
        # nemotron-3-ultra) won every early fallback slot whenever the primary or
        # an earlier hop failed — MEASURED live: glm-5.2 hit 2 real "apply_patch
        # invoked with incompatible payload" fatal errors in tonight's own build
        # test, yet kept winning fallback hops in a real user session afterward,
        # because nothing here ever checked _is_tool_proven. Unproven models are
        # NOT dropped — only demoted to a last-resort tail — so a request still
        # gets served if every proven option is exhausted/throttled.
        _proven_ordered = [e for e in ordered if _is_tool_proven(e[2])]
        if _proven_ordered:
            ordered = _proven_ordered + [e for e in ordered if not _is_tool_proven(e[2])]
        # LOW-QUALITY TAIL, AFTER proven-first (see _LOW_QUALITY_RE): the proven
        # allowlist still names nemotron/gpt-oss, so proven-first alone walked the
        # chain straight onto the demoted families while glm-4.7/kimi-k2.6 sat
        # alive — MEASURED in the 2026-07-27 RESPONSES-503 'tried=' logs. A
        # tool request must exhaust every normal candidate (proven first, then
        # unproven) before a last-resort family is even offered. Fail-open: when
        # only last-resort families live, the order is unchanged.
        _lq_tail = [e for e in ordered if _is_low_quality(e[2])]
        if _lq_tail and len(_lq_tail) < len(ordered):
            ordered = [e for e in ordered if not _is_low_quality(e[2])] + _lq_tail
    else:
        ordered = _interleave_by_provider(fast + slow)
        # Same last-resort partition for a non-tool chain: a demoted family is
        # only ever the TAIL, behind every other alive candidate that fits the
        # request (fail-open when nothing else lives).
        _lq_tail = [e for e in ordered if _is_low_quality(e[2])]
        if _lq_tail and len(_lq_tail) < len(ordered):
            ordered = [e for e in ordered if not _is_low_quality(e[2])] + _lq_tail
    # Agentic loops burn through the tool-capable pool in bursts, so give them a
    # deeper chain (reaches the still-fresh sibling models when the top providers are
    # momentarily throttled) than a one-shot chat needs.
    hop_cap = TOOLS_MAX_HOPS if require_tools else MAX_HOPS
    for _score, pid, m in ordered:
        if len(chain) >= hop_cap:
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
    # Keep somewhere to go when a model DECLINES the task rather than failing.
    # A refusal is now a non-answer (see _looks_like_refusal), so the chain moves
    # on -- but only if it still holds a model that will take the work. Appended,
    # never promoted: the ordinary ranking still decides who answers first.
    # The named chain's entries, in the user's order, ahead of everything the
    # builder produced -- minus anything vetoed or already present, so a chain
    # can neither introduce a duplicate hop nor resurrect a model the caller
    # just rejected.
    if _preferred:
        head = []
        for entry in _preferred:
            pid, m = entry[0], entry[1]
            if exclude_identities and _normalize_model_identity(m) in exclude_identities:
                continue
            if (pid, m) in head:
                continue
            head.append((pid, m))
        chain = head + [e for e in chain if e not in head]
    return _ensure_permissive_hop(chain)


# Key-pool rotation: statuses that mean "this key is bad/throttled, try the
# next key for the SAME provider before falling back to another provider".
# 402 added 2026-07-31: a key whose CREDIT is spent is the textbook case for
# trying the next key in the pool. Without it, one exhausted account took the
# whole provider down even when a second, funded key sat right behind it —
# which is exactly the situation when several Puter accounts are pooled (each
# account carries its own allowance; see _puter_allowance).
_KEY_ROTATE_STATUSES = (401, 402, 403, 429)

# A hop failure that is worth waiting out rather than giving up on. Used to
# decide whether an exhausted chain should back off and retry once (see the
# transient-storm retry in the responses path).
#
# MEASURED LIVE 2026-08-09: every hop in a real chain failed with a raw
# requests.exceptions.ConnectionError (a brief local network blip -- the
# VERY NEXT request seconds later succeeded normally on the same providers),
# yet the storm-retry never fired and the chain surfaced a hard 503 instead.
# Root cause: errors.append(exc.__class__.__name__) records the bare
# exception class name ("ConnectionError"), which contains none of
# "timeout"/"timed out"/HTTP 429/500/502/503/504 -- a pure connection
# failure (no HTTP response at all) is arguably the MOST classic transient
# condition and was the one class this regex did not cover.
_TRANSIENT_ERR_RE = re.compile(
    r"HTTP (?:429|500|502|503|504)|timeout|timed out|connectionerror", re.I)
_CHAIN_RETRY_DELAY = 6.0   # seconds; the free relays meter per MINUTE

_provider_key_cursor = {}          # pid -> next round-robin start offset
_key_cursor_lock = threading.Lock()

# Sentinel for "no key pinned" in _upstream_chat. Cannot be None: None is a
# real, meaningful key value there (a keyless provider sends no auth header).
_NO_KEY_PIN = object()


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


def _refit_payload_to_learned_ctx(pid, payload):
    """A payload recompacted to the window we JUST learned, or None when that
    would change nothing.

    Called after a 400/413 whose body revealed the model's real context. The
    request that triggered it was built against an optimistic budget (the
    provider-wide _PROVIDER_TPM), so it is usually several times too big; this
    rebuilds it against the authoritative per-model limit. The caller retries
    at most once, so a second failure falls through to the next hop."""
    try:
        msgs = payload.get("messages")
        if not isinstance(msgs, list) or not msgs:
            return None
        budget = _model_ctx_budget(pid, payload.get("model"))
        if not budget or budget <= 0:
            return None
        # SAFETY FACTOR. This was 0.55, on the belief that _est_tokens (chars/4)
        # is optimistic for code. RE-MEASURED 2026-08-01 against real
        # `usage.prompt_tokens` from a live provider, and the belief was wrong —
        # the estimator OVER-counts on every shape of traffic:
        #
        #     prose        est=886   real=402    est/real 2.20
        #     code         est=1013  real=801    est/real 1.26
        #     code+tools   est=1623  real=1423   est/real 1.14
        #     tools only   est=1014  real=889    est/real 1.14
        #
        # (The earlier measurement that said otherwise was taken through the
        # normal chat path, which had injected craft briefs into the payload —
        # those tokens were real, but they were not the ones being counted.)
        #
        # So 0.55 on top of an estimator already 1.14x conservative meant an
        # agentic request used under HALF of the window it was entitled to. At
        # 0.80 the worst realistic case (tools-heavy, est/real 1.14) still lands
        # near 70% of the true window, leaving genuine headroom for the response
        # — output tokens share the window on most providers, which is what the
        # max_tokens cap below handles — and for a model whose ADVERTISED window
        # is simply a lie (cloudflare claimed 120,000 and delivered 32,768).
        budget = int(budget * 0.80)
        compacted, did = _compact_to_budget(msgs, payload.get("tools"), budget)
        if not did:
            return None                       # already fits; the 400 was something else
        out = dict(payload)
        out["messages"] = _sanitize_tool_messages(compacted)
        # Output tokens count against the same window on most providers, so an
        # oversized max_tokens can re-trigger the very error we just handled.
        mt = out.get("max_tokens")
        cap = max(256, int(budget * 0.12))
        if isinstance(mt, int) and mt > cap:
            out["max_tokens"] = cap
        return out
    except Exception:                                                # noqa: BLE001
        _log.debug("[refit] could not recompact for %s", pid, exc_info=True)
        return None


# --------------------------------------------------------------------------- #
# Output budget — a provider's OWN default can cut an answer in half
# --------------------------------------------------------------------------- #

# When a caller sends no max_tokens, each provider applies its own default, and
# some are tiny. MEASURED 2026-08-01: cloudflare stops at exactly 256 completion
# tokens with finish_reason "length" -- an answer listing twelve months died at
# "10. October:". The same model and the same question with max_tokens=2048
# finished cleanly in 432 tokens. Nothing in the hub was wrong except that it
# forwarded no budget at all and let the provider pick one.
#
# Only providers whose low default has been MEASURED are listed. A blind global
# default risks sending a value above some model's real ceiling, which comes
# back as a 400 and costs a hop -- worse than the problem.
_PROVIDER_OUTPUT_DEFAULT = {
    "cloudflare": 2048,      # measured default 256
    "g4f-cloudflare": 2048,  # same upstream
}
# Providers proven to truncate at run time (see _note_default_truncation) join
# the table above for the rest of the process's life.
_LEARNED_OUTPUT_DEFAULT = {}
_LEARNED_OUTPUT_TOKENS = 2048


def _apply_output_budget(payload, pid):
    """Give the hop an explicit output budget when the CALLER gave none.

    Never overrides a max_tokens the client asked for -- a caller that wants a
    20-token answer still gets one."""
    if payload.get("max_tokens") is not None:
        return
    budget = _PROVIDER_OUTPUT_DEFAULT.get(pid) or _LEARNED_OUTPUT_DEFAULT.get(pid)
    if budget:
        payload["max_tokens"] = budget


def _note_default_truncation(pid, client_max_tokens, finish_reason):
    """Remember a provider that truncated an answer we set no budget for.

    This is the same self-healing shape as _learn_context_limit: rather than
    guess every provider's default up front, notice the one case that proves it
    (finish_reason "length" on a request where the client asked for no limit)
    and send an explicit budget to that provider from then on."""
    if client_max_tokens is not None or finish_reason != "length":
        return
    if pid in _PROVIDER_OUTPUT_DEFAULT or pid in _LEARNED_OUTPUT_DEFAULT:
        return
    _LEARNED_OUTPUT_DEFAULT[pid] = _LEARNED_OUTPUT_TOKENS
    _log.info("[output] %s truncated an unbounded answer at its own default; "
              "sending max_tokens=%d from now on", pid, _LEARNED_OUTPUT_TOKENS)


def _model_ctx_budget(pid, model):
    """Best estimate of a model's usable INPUT context: the limit LEARNED from a real
    400 (authoritative) if we have one, else the provider's context-sized _PROVIDER_TPM."""
    lim = _MODEL_MAX_INPUT.get((pid, model))
    if isinstance(lim, int) and lim > 0:
        return lim
    return _provider_tpm(pid)


_SUMMARY_SYSTEM = (
    "You compress the dropped part of a coding conversation so the next model can "
    "carry on without re-reading it. Reply with the RECAP ONLY — no preamble.\n"
    "Cover, in this order, only what is present:\n"
    "1. GOAL — what is being built, in one line.\n"
    "2. STATE — what already exists: files created/edited and what each does.\n"
    "3. DECISIONS — choices already made and why (stack, schema, naming, layout).\n"
    "4. OPEN — what was still in progress or unresolved.\n"
    "Be specific: real file paths, real names, real values. No filler, no advice, "
    "no restating these instructions. Under 250 words. Facts only — never invent a "
    "detail that was not in the text."
)
_SUMMARY_MAX_TOKENS = 500
_SUMMARY_CACHE_MAX = 64
_SUMMARY_MAX_INFLIGHT = 3              # background recaps must not swamp the free fleet
_summary_cache = {}                    # sha256(dropped) -> recap
_summary_inflight = set()
_summary_lock = threading.Lock()


# Reasoning models emit their scratchpad inline. MEASURED: a recap came back
# starting "<think>Here's a thinking process: 1. Analyze User Input..." — feeding
# that into the next model's context is pure noise, and worse, it reads as
# instructions. An UNCLOSED opening tag means the reply was cut off mid-thought,
# so everything after it is scratchpad too.
_THINK_BLOCK_RE = re.compile(
    r"<(think|thinking|reasoning|scratchpad)>.*?</\1>", re.S | re.I)
_THINK_OPEN_RE = re.compile(r"<(think|thinking|reasoning|scratchpad)>.*", re.S | re.I)


def _strip_thinking(text):
    """Model output with any chain-of-thought block removed."""
    if not isinstance(text, str):
        return ""
    out = _THINK_BLOCK_RE.sub("", text)
    out = _THINK_OPEN_RE.sub("", out)
    return out.strip()


def _summary_key(dropped):
    """(key, text) for the turns being dropped, or (None, None) if not worth it."""
    text = "\n\n".join(
        "%s: %s" % (m.get("role", "?"), m["content"][:4000])
        for m in dropped
        if isinstance(m, dict) and isinstance(m.get("content"), str) and m["content"].strip())
    if len(text) < 800:                # too little dropped to be worth a call
        return None, None
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest(), text


def _summarize_worker(key, text):
    """Compute one recap and cache it. Runs OFF the request path."""
    try:
        msgs = [{"role": "system", "content": _SUMMARY_SYSTEM},
                {"role": "user", "content": text[-60000:]}]
        # medium, not hard: compression is not the user's actual task and must
        # not take the strongest hop the real request wants.
        pid, model, _d = _route_by_difficulty(msgs, _SUMMARY_MAX_TOKENS,
                                              require_tools=False,
                                              force_difficulty="medium")
        if not pid:
            return
        for hop_pid, hop_model in _build_chain(pid, model):
            if _is_sub(hop_pid):
                continue               # never spend a paid subscription on a recap
            try:
                resp = _dispatch_chat(hop_pid, {"model": hop_model, "stream": False,
                                                "max_tokens": _SUMMARY_MAX_TOKENS,
                                                "messages": msgs}, False)
                data = resp.json() if resp.status_code == 200 else None
                resp.close()
            except (requests.RequestException, RuntimeError, ValueError):
                continue
            out = _strip_thinking(
                (((data or {}).get("choices") or [{}])[0].get("message") or {})
                .get("content") or "")
            if not out:
                continue
            with _summary_lock:
                if len(_summary_cache) >= _SUMMARY_CACHE_MAX:
                    _summary_cache.clear()   # cheap bound; recaps are re-derivable
                _summary_cache[key] = out
            return
    except Exception:                                            # noqa: BLE001
        _log.debug("[summary] worker failed", exc_info=True)
    finally:
        with _summary_lock:
            _summary_inflight.discard(key)


def _summarize_dropped(dropped):
    """A recap of the discarded turns if one is READY, else None — and start
    computing it in the background for next time.

    Deliberately never blocks. Compaction runs on EVERY hop of every request, and
    a recap call is a full model round-trip: MEASURED at over two minutes on the
    free fleet, which would have made large requests unusable. So the first
    compaction of a conversation ships the structural notice (brief + file list)
    and the recap lands from the following turn onward — which is exactly when it
    matters, since the continuity problem shows up on the FOLLOW-UP ("make it
    better"), not on the turn that filled the window."""
    if not dropped:
        return None
    try:
        key, text = _summary_key(dropped)
        if not key:
            return None
        with _summary_lock:
            hit = _summary_cache.get(key)
            if hit:
                return hit
            if key in _summary_inflight or len(_summary_inflight) >= _SUMMARY_MAX_INFLIGHT:
                return None                # already being computed, or too many at once
            _summary_inflight.add(key)
        threading.Thread(target=_summarize_worker, args=(key, text),
                         daemon=True, name="summarize").start()
    except Exception:                                            # noqa: BLE001
        _log.debug("[summary] could not schedule", exc_info=True)
    return None


# Tells an agent CLI (Codex / Claude Code / hermes / openclaw — any tool-calling
# client) that the hub's crew pipeline exists and how to call it, so the AGENT
# itself can decide a big creation subtask is worth delegating (user request
# 2026-08-06: "the agent should take the decision depending on the task").
# Opening-turn-only like every injection here: mid-loop instructions corrupt
# agent loops. ~110 tokens, injected only for tool-carrying requests.
_CREW_AGENT_HINT = (
    "HUB CREW DELEGATION (optional — judge per task): this endpoint is a "
    "multi-model hub. For a LARGE, self-contained creation subtask (a whole "
    "page, a full module, a research brief) you may delegate it instead of "
    "writing it inline: an ordinary chat/completions POST to this same "
    "endpoint with NO tools field and model \"crew\" (auto-picks the "
    "specialist) or \"crew-code\" / \"crew-research\" / \"crew-write\" / "
    "\"crew-design\". The pipeline (planner -> specialist workers -> "
    "cross-provider reviewer -> fix pass) takes 5-20 minutes and returns the "
    "finished artefact as one message. Use it for big deliverables only — "
    "never for quick questions, never for a turn that needs YOUR tools."
)


def _awaiting_new_instruction(messages):
    """True when the model is about to answer a FRESH user instruction rather
    than continue a tool cycle -- i.e. the last non-system message is a user
    message with actual text.

    This is the distinction _apply_craft_brief used to miss. Its old `mid_loop`
    test counted any conversation past the first user message as a running
    loop, which is far too broad: a tool loop is the model MID-CYCLE (a call
    issued, a result pending), while a user typing "now add the booking page"
    after the previous turn finished is a new task that deserves a plan exactly
    as much as the first one did. Reported as "i want also that work if i
    continue eg projects".

    A tool result is role "tool" in the OpenAI shape every caller is converted
    to before this runs, so it can never be mistaken for an instruction; a
    pending call shows as tool_calls on the assistant message."""
    for m in reversed(messages or []):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "system":
            continue
        if role != "user":
            return False                      # assistant / tool -> mid-cycle
        return bool(_message_text(m).strip())
    return False


def _message_text(m):
    """The text of a message whose content may be a string or a multimodal
    list of parts."""
    content = m.get("content")
    if isinstance(content, list):
        return " ".join(p.get("text") or "" for p in content
                        if isinstance(p, dict))
    return content or ""


def _apply_craft_brief(messages, agentic=False):
    """Prepend a domain craft brief when the OPENING turn calls for one —
    plus, for tool-carrying (agentic) requests, the crew-delegation hint.

    Deliberately opening-turn-only. By the second turn a tool loop may be in
    flight, and injecting instructions into a running agent turn is the failure
    mode that made the prompt enhancer dashboard-only. On the first turn there is
    no loop yet, so this is additive and safe.

    It never touches the user's own text — it adds system messages ahead of
    the conversation. Returns the original list when nothing matches, so the
    common case allocates nothing."""
    try:
        if not config.get_flag("craft_briefs", True):
            return messages
        if not isinstance(messages, list) or not messages:
            return messages
        users = [m for m in messages
                 if isinstance(m, dict) and m.get("role") == "user"]
        # A NEW instruction -- turn 1 or turn 50 -- gets the brief. Only a live
        # tool cycle is refused it (see _awaiting_new_instruction).
        if not users or not _awaiting_new_instruction(messages):
            # NOT the opening turn. The domain brief stays out -- it is about the
            # TASK, and re-sending it into a running loop is the noise this
            # opening-turn-only rule exists to prevent.
            #
            # ACT is different: it is about how to END a turn, so it is needed on
            # exactly the turns the opening brief never sees. MEASURED 2026-08-30
            # on a real codex session -- 13 of 14 agent turns ended with "Let me
            # <do the next thing>." and then stopped, the precise shape ACT's
            # first bullet forbids, and every one was turn 2 or later. The brief
            # had only ever been injected on turn 1, so the instruction was never
            # present when it was needed and the user typed "continue" 13 times.
            #
            # Additive and ~150 tokens: a system message, never a rewrite of the
            # user's text or of the tool history.
            if agentic and config.get_flag("act_every_turn", True):
                act = craft.act_message()
                i = 0
                while i < len(messages) and isinstance(messages[i], dict)                         and messages[i].get("role") == "system":
                    i += 1
                return messages[:i] + [act] + messages[i:]
            return messages
        # The LATEST instruction, not the first. It used to read users[0], so a
        # session that opened with "hi" and later asked for a landing page
        # matched the brief against "hi". On turn 1 these are the same message.
        text = _message_text(users[-1])
        # ...falling back to the instruction that OPENED the project when the
        # latest one names no domain of its own. A follow-up is usually short
        # ("now add the booking page") and says nothing about what is being
        # built, but the project has not changed underneath it: the design
        # rules that applied to the homepage apply to the booking page. Without
        # this, every step after the first is built with no brief at all --
        # which is the slop the user is asking to be rid of. Costs a brief on
        # some small follow-ups too; that trade is deliberate, and the cheaper
        # side of it is the one that produces the bad output.
        if len(users) > 1 and not craft.match(text or ""):
            first = _message_text(users[0])
            if craft.match(first):
                text = first + "\n" + (text or "")
        # `agentic` is bool(payload["tools"]) -- the only honest signal for
        # whether this caller can actually RUN a check. It picks which VERIFY
        # block ships, so a tool-less client is never told to run anything.
        brief = craft.system_message(text or "", tools=bool(agentic))
        extra = [brief] if brief else []
        if agentic and config.get_flag("crew_agent_hint", True):
            extra.append({"role": "system", "content": _CREW_AGENT_HINT})
        if not extra:
            return messages
        # After any leading system messages, so the caller's own instructions
        # (Codex's agent prompt) still come first and win on conflict.
        i = 0
        while i < len(messages) and isinstance(messages[i], dict) \
                and messages[i].get("role") == "system":
            i += 1
        return messages[:i] + extra + messages[i:]
    except Exception:                                            # noqa: BLE001
        _log.debug("[craft] brief injection skipped", exc_info=True)
        return messages


def _compact_to_budget(messages, tools, budget, summarizer=None):
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
    # PIN THE ORIGINAL BRIEF. Keeping only the newest turns loses the message
    # that says WHAT IS BEING BUILT, which is the one thing a follow-up depends
    # on. Symptom: finish a project, say "make it better", and the model starts a
    # NEW one because every turn describing the old one had been dropped. The
    # first user turn is the task statement, so it is pinned (capped, so a huge
    # opening paste can't eat the window it is meant to protect).
    brief = None
    # Only when there is a CONVERSATION to lose. With a single turn the "brief"
    # IS the whole payload, and pinning it would cap it to its first 4000 chars —
    # throwing away the end of a pasted file, which _trim_largest_message below
    # keeps deliberately (head AND tail).
    if len(rest) > 2:
        for m in rest:
            if isinstance(m, dict) and m.get("role") == "user":
                brief = m
                break
    if brief is not None and isinstance(brief.get("content"), str) \
            and len(brief["content"]) > _BRIEF_PIN_CHARS:
        brief = dict(brief)
        brief["content"] = (brief["content"][:_BRIEF_PIN_CHARS] +
                            "\n[... original request truncated ...]")
    base = _est_tokens(lead_sys + ([brief] if brief else []), tools)
    kept, running = [], base
    for m in reversed(rest):                       # keep newest-first until full
        if brief is not None and m is rest[0]:
            continue                               # already pinned above
        c = _est_tokens([m])
        if kept and running + c > target:
            break
        kept.append(m)
        running += c
    kept.reverse()
    if len(kept) >= len(rest):
        # Dropping whole turns achieved nothing — the overflow is INSIDE a single
        # message (a pasted file, a huge tool result). Previously this returned
        # "no change" and the request went out oversized and was rejected, losing
        # the hop entirely. Trim within that message instead: keep its head AND
        # tail, which is where the question and the recent output live, and mark
        # the cut so the model knows material is missing rather than silently
        # reasoning over a truncated file.
        trimmed, did_trim = _trim_largest_message(lead_sys + rest, tools, target)
        return (trimmed, True) if did_trim else (messages, False)
    # Name the artefacts that were dropped. "Earlier conversation was truncated"
    # tells the model nothing actionable; a list of the files already created
    # tells it the project EXISTS and should be edited, not started again.
    dropped = [m for m in rest if m not in kept and m is not brief]
    files = _mentioned_paths(dropped)
    note = ("[Note: earlier turns of THIS SAME conversation were dropped to fit this "
            "model's context window. The work already exists — continue and EDIT it, "
            "do not start a new project.")
    if files:
        note += " Files created/edited earlier: " + ", ".join(files) + "."
    note += " Read a file before changing it, and ask the user for anything else you need.]"
    # A model-written recap of what was dropped, when a summarizer is wired in.
    # Structural facts (brief + file list) survive either way; the recap adds the
    # part they cannot carry — the DECISIONS and the reasoning behind them.
    recap = summarizer(dropped) if summarizer else None
    if recap:
        note += "\n\n[Recap of the dropped turns]\n" + recap
    notice = {"role": "system", "content": note}
    head = lead_sys + [notice] + ([brief] if brief else [])
    return head + kept, True


# Longest opening request we will pin verbatim. Past this it is truncated: the
# brief exists to say what is being built, not to carry a whole pasted codebase.
_BRIEF_PIN_CHARS = 4000

# Paths in prose/tool output: src/app.py, ./index.html, components/Cart.tsx.
# Deliberately narrow — a real extension and no spaces — so ordinary sentences
# don't get mined for junk.
_PATH_RE = re.compile(
    r"(?<![\w/.])(?:\./)?(?:[\w.-]+/){0,6}[\w.-]+\."
    r"(?:py|js|jsx|ts|tsx|html|css|scss|json|md|yml|yaml|toml|sql|sh|rb|go|rs|java|php|vue|svelte)"
    r"(?![\w/])")


def _mentioned_paths(messages, limit=25):
    """File paths mentioned across messages, most recent first, de-duplicated."""
    seen, out = set(), []
    for m in reversed(messages or []):
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, list):          # content-parts
            c = " ".join(p.get("text") or "" for p in c if isinstance(p, dict))
        if not isinstance(c, str):
            continue
        for p in _PATH_RE.findall(c):
            p = p.lstrip("./")
            if p and p not in seen:
                seen.add(p)
                out.append(p)
                if len(out) >= limit:
                    return out
    return out


def _trim_largest_message(messages, tools, target):
    """Shrink the single biggest text message so the whole payload fits `target`.

    Head+tail rather than a plain cut: the start of a pasted file (imports,
    signatures) and its end (the part just being edited) both carry more signal
    than the middle. Returns (messages, changed)."""
    try:
        over = _est_tokens(messages, tools) - target
        if over <= 0:
            return messages, False
        idx, biggest = None, 0
        for i, m in enumerate(messages):
            if not isinstance(m, dict) or not isinstance(m.get("content"), str):
                continue
            n = len(m["content"])
            if n > biggest:
                idx, biggest = i, n
        if idx is None:
            return messages, False
        # ~4 chars per token, plus slack for the marker itself.
        cut = min(biggest - 400, int(over * 4) + 400)
        if cut <= 0 or biggest - cut < 400:
            return messages, False
        text = messages[idx]["content"]
        keep = biggest - cut
        head = text[: int(keep * 0.6)]
        tail = text[-int(keep * 0.4):] if int(keep * 0.4) else ""
        out = list(messages)
        out[idx] = dict(messages[idx])
        out[idx]["content"] = (
            head + "\n\n[... %d characters omitted to fit this model's context "
                   "window; ask for the missing part if you need it ...]\n\n" % cut + tail)
        return out, True
    except Exception:                                                # noqa: BLE001
        return messages, False


# How many providers an embedding request is allowed to fall through before
# giving up. Lower than the chat chain on purpose: embeddings are called in
# tight loops while indexing a corpus, so a long per-call fallback turns one bad
# provider into a stall of thousands of slow requests rather than a fast error.
_EMBED_MAX_HOPS = 4


# OUTBOUND PROXY.
#
# requests already honours HTTP_PROXY/HTTPS_PROXY through the environment, but
# only because `trust_env` defaults to True -- which also means it silently
# picks up whatever a corporate machine, a VPN client or a leftover shell export
# happens to have set. The failure that produces is miserable to diagnose: every
# provider times out at once, with no indication the traffic is being sent
# somewhere else entirely.
#
# So it is stated explicitly instead: a proxy configured here is used, nothing
# is inherited by accident, and /api/status can report which it is. Empty means
# direct.
_PROXY_KEY = "outbound_proxy"


def _proxies():
    """{'http': url, 'https': url} for upstream calls, or None for direct."""
    url = (config.get_value(_PROXY_KEY, "") or "").strip()
    if not url:
        return None
    return {"http": url, "https": url}


def _upstream_post(pid, path, payload):
    """POST {base_url}/{path} for provider pid, rotating across its key pool.

    _upstream_chat's small sibling for the NON-chat surfaces. It deliberately
    shares the parts that are about the provider -- base-URL resolution
    (including Cloudflare's {account_id} fill), the no-key/static-key rule, key
    rotation on 401/403/429, and quota accounting -- and none of the parts that
    are about chat, since context compaction, output budgets, craft briefs and
    the 400-refit-retry are all meaningless for an embedding request.

    Returns the first non-rotatable response, or raises like _upstream_chat."""
    pcfg = config.get_provider_config(pid)
    base = _resolve_base_url(pid, pcfg)
    if not base:
        raise RuntimeError("no base_url for provider " + pid)
    if "{account_id}" in base:
        raise RuntimeError("could not resolve the Cloudflare account id from this token")
    keys = pcfg.get("api_keys") or []
    if not keys:
        if _needs_key(pid):
            raise RuntimeError("no api key for provider " + pid)
        keys = [(prov.get_provider(pid) or {}).get("static_key") or None]

    url = base.rstrip("/") + "/" + path.lstrip("/")
    n = len(keys)
    start = _next_key_start(pid, n)
    for i in range(n):
        is_last = (i == n - 1)
        key = keys[(start + i) % n]
        try:
            resp = requests.post(
                url, json=payload,
                headers=({"Content-Type": "application/json"} if key is None else
                         {"Authorization": "Bearer " + key,
                          "Content-Type": "application/json"}),
                timeout=(CONNECT_TIMEOUT, CHAT_READ_TIMEOUT),
                proxies=_proxies(),
            )
        except requests.RequestException:
            if is_last:
                raise
            continue
        quota.record(pid, payload.get("model"))
        quota.record_key(pid, key, payload.get("model"))
        quota.note_key_outcome(pid, key, resp.status_code not in (401, 403, 429))
        quota.observe_headers(pid, resp.headers)
        # Same rotation rule as chat: these three statuses are about THIS KEY,
        # so the next key in the pool deserves a turn before the provider is
        # written off. Anything else is about the request or the provider and
        # rotating would just burn the whole pool on it.
        if resp.status_code in (401, 403, 429) and not is_last:
            resp.close()
            continue
        return resp
    raise RuntimeError("no keys tried for provider " + pid)


def _upstream_chat(pid, payload, stream, only_key=_NO_KEY_PIN):
    """POST {base_url}/chat/completions for provider pid, rotating across the
    provider's api_keys pool. Tries a round-robin start key; on 401/403/429 it
    advances to the next key for the SAME provider. Returns the first non-
    rotatable response (or the last response/exception once keys are exhausted,
    so the caller's provider-level fallback still kicks in). May raise
    requests.RequestException or RuntimeError. Never logs a key.

    `only_key` pins the call to a single key instead of the pool (None means
    "send no Authorization header"). _NO_KEY_PIN, not None, is the "not pinned"
    default precisely because None is itself a meaningful key value here."""
    if isinstance(payload, dict) and isinstance(payload.get("messages"), list):
        # (1) AUTO-COMPACT the history to THIS model's context window (per-model
        # memory management — a small-context model gets recent turns only), then
        # (2) repair tool-pairing so a strict upstream can't 400 the agentic turn
        # ('tool_call_ids did not have response messages') — compaction may itself
        # orphan a tool msg / dangle a tool_call, so sanitize runs AFTER compaction.
        # SWARM-INTERNAL calls opt out. Every swarm stage already carries a
        # precise system prompt, and a craft brief on top of one does real harm:
        # the planner is told "reply with JSON ONLY", the WEB_DESIGN/SEO/IMAGES
        # briefs told it to build a website, and it obeyed the brief — returning
        # publication-ready copy instead of a plan, so the swarm fell back to a
        # single model on every creation task, which is the one task it exists
        # for. Skipping them is also ~1.8k tokens back per stage.
        msgs = (payload["messages"] if payload.get("_no_craft")
                else _apply_craft_brief(payload["messages"],
                                        agentic=bool(payload.get("tools"))))
        payload = dict(payload)
        payload.pop("_no_craft", None)      # never goes upstream
        payload["messages"] = msgs
        # Summarised compaction: a model-written recap of the dropped turns, so
        # the DECISIONS survive and not just the file names. Cached per content,
        # fail-open to the structural notice, and skippable via the setting for
        # anyone who would rather not spend a call on it.
        _summarizer = (_summarize_dropped
                       if config.get_flag("compact_summary", True) else None)
        compacted, did = _compact_to_budget(msgs, payload.get("tools"),
                                            _model_ctx_budget(pid, payload.get("model")),
                                            summarizer=_summarizer)
        fixed = _sanitize_tool_messages(compacted)
        if did or fixed is not msgs:
            payload = dict(payload)
            payload["messages"] = fixed
    # Perplexity rejects max_tokens < 16 ("max_tokens must be at least 16"). Clamp up
    # harmlessly so a small-output request (classification, a probe) doesn't 400.
    if pid == "perplexity" and isinstance(payload, dict):
        mt = payload.get("max_tokens")
        if isinstance(mt, int) and mt < 16:
            payload = dict(payload)
            payload["max_tokens"] = 16
    # Puter speaks a DRIVER protocol, not OpenAI-over-HTTP — its
    # /chat/completions surface 403s the app tokens this hub can obtain. The
    # branch lives HERE, not only in _dispatch_chat, so every caller (the key
    # test, the model probe, the chain loops) goes the working way.
    if _is_puter_driver(pid):
        return _puter_chat(pid, payload, stream)
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
    if only_key is not _NO_KEY_PIN:
        # Pin the pool to ONE key. Used by the per-key Test button: rotation is
        # exactly what makes "which of my keys is broken?" unanswerable, since a
        # pool call can silently succeed on key 2 and report the provider green
        # while key 1 is dead. Everything else in this function (base-URL
        # resolution, Cloudflare account-id fill, the Puter driver branch,
        # Perplexity's max_tokens floor, context re-fit) still applies, which is
        # why the test pins this call instead of hand-rolling its own request.
        keys = [only_key]
    elif not keys:
        if _needs_key(pid):
            raise RuntimeError("no api key for provider " + pid)
        # No-key provider (e.g. Pollinations' anonymous tier): run exactly one
        # "key-less" pass. None is the sentinel -> no Authorization header below.
        # A no_key provider whose registry entry carries a static_key (uncloseai,
        # llm7) sends that documented placeholder as its bearer instead.
        keys = [(prov.get_provider(pid) or {}).get("static_key") or None]
    url = base.rstrip("/") + "/chat/completions"
    n = len(keys)
    start = _next_key_start(pid, n)
    last_exc = None
    refit_done = False        # one context re-fit per request, never a loop
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
                proxies=_proxies(),          # explicit; never inherited by accident
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
        # ...and against THIS key. The pool exists so a provider survives one key
        # running out, which worked -- and made "which of my keys is dead?"
        # unanswerable, because every counter was per provider.
        quota.record_key(pid, key, payload.get("model"))
        quota.note_key_outcome(pid, key, resp.status_code not in (401, 403, 429))
        quota.observe_headers(pid, resp.headers)  # ADAPT to the provider's real quota
        if resp.status_code == 400:               # learn a small context window from the error
            _learn_context_limit(pid, payload.get("model"), resp)
            _maybe_mark_missing_model(pid, payload.get("model"), resp)  # gone/renamed id -> sideline
        if resp.status_code == 413:               # 'too large for this model's TPM' -> learn the cap
            _learn_tpm_limit(pid, payload.get("model"), resp)
            # A 413 is not always about TPM. Cloudflare reports a CONTEXT
            # overflow with this status too ('exceeded this model context window
            # limit (32768)'), so the context learner gets a look as well —
            # otherwise the real window is never recorded and the re-fit below
            # has nothing smaller to compact to.
            _learn_context_limit(pid, payload.get("model"), resp)
        # RE-FIT AND RETRY. Learning the real window only helped the NEXT request;
        # this one still died and fell through to a weaker model. MEASURED: a
        # Codex session sent ~100k-token turns to cloudflare/@cf/qwen/qwen3-30b
        # (real window 32768, but _PROVIDER_TPM claimed 120000), so every request
        # 400'd on its best hop and was answered by llm7/gemini-3.1-flash-LITE.
        # Now the freshly-learned limit is applied immediately: recompact to the
        # REAL window and try the same hop once more, so a big conversation is
        # served by the strong model instead of being handed to a weak one.
        if resp.status_code in (400, 413) and not refit_done:
            refit_done = True                 # at most one re-fit per key attempt
            refit = _refit_payload_to_learned_ctx(pid, payload)
            if refit is not None:
                resp.close()
                try:
                    resp = requests.post(
                        url, json=refit,
                        headers=({"Content-Type": "application/json"} if key is None else
                                 {"Authorization": "Bearer " + key,
                                  "Content-Type": "application/json"}),
                        stream=stream,
                        timeout=(CONNECT_TIMEOUT,
                                 STREAM_IDLE_TIMEOUT if stream else CHAT_READ_TIMEOUT))
                except requests.RequestException as exc:
                    last_exc = exc
                    if is_last:
                        raise
                    continue
        if 200 <= resp.status_code < 300:
            quota.note_success(pid)  # provider answered -> clear its 429-backoff streak
            quota.note_model_success(pid, payload.get("model"))  # and THIS model's streak
            _note_provider_result(pid, ok=True)  # clear the consecutive-hard-fail streak
        # A 429 on a SINGLE key just rotates to the next key below. Only when the
        # LAST key also 429s (every key for this provider is rate-limited) do we
        # sideline the whole provider. And when there's no numeric Retry-After,
        # cool down for _HOP_COOLDOWN_DEFAULT (assume a per-minute burst) instead
        # of pegging it exhausted until the day/month window resets; a real
        # Retry-After is honored as-is.
        if resp.status_code == 429 and is_last:
            retry_after = resp.headers.get("Retry-After")
            secs = None
            try:
                secs = float(retry_after) if retry_after else None
            except ValueError:
                secs = None
            daily_secs = None
            if secs is None:
                # A DAILY allowance that is spent will not come back in a short
                # cooldown, and retrying it every round burns a chain hop on every
                # request for the rest of the day (Cloudflare: "you have used up
                # your daily free allocation of 10,000 neurons"). Park it until
                # the window actually resets instead — the same self-healing
                # path, just with an honest ETA.
                daily_secs = _daily_exhaustion_secs(pid, resp)
                secs = daily_secs
            # Per-model-limited providers (Google: 15 RPM PER MODEL): a per-minute
            # burst 429 on ONE model must not bench the whole fleet — the sibling
            # models each still have budget and are exactly the capacity that keeps an
            # agentic loop off a 503. Park only the offending model for the short burst;
            # only bench the whole provider when the DAILY window is truly spent.
            per_model_only = pid in _PER_MODEL_RATE_LIMIT_PROVIDERS and daily_secs is None
            if not per_model_only:
                quota.mark_throttled(pid, secs or _HOP_COOLDOWN_DEFAULT)
            # ALSO park just this model: it survives provider note_success(), so when
            # a sibling model revives the provider, the id that actually 429'd stays
            # sidelined instead of being re-picked and 429'ing again.
            quota.mark_model_throttled(pid, payload.get("model"), secs or _HOP_COOLDOWN_DEFAULT)
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
        # Consecutive-hard-failure safety net: increments on any 401/402/403/404
        # (incl. a 404-masked empty wallet) with no 2xx in between, parks the whole
        # provider at the threshold. 429/5xx leave the streak untouched.
        if is_last and not (200 <= resp.status_code < 300):
            _note_provider_result(
                pid, ok=False,
                hard_fail=resp.status_code in _HARD_FAIL_STATUSES)
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
    r"too many (?:input )?tokens|exceeds? the (?:model'?s )?maximum|"
    # Cloudflare Workers AI, MEASURED 2026-07-31 (arrives as 413, not 400):
    # 'The estimated number of input and maximum output tokens (42532) exceeded
    #  this model context window limit (32768).'
    r"context window limit|"
    # google/gemini-3.6-flash via g4f, MEASURED 2026-08-08: 'You have reached the
    # maximum prompt length limit. Please consider shortening your prompt and try
    # again. Thank you.' -- says PROMPT length, not context length, and carries no
    # digit at all, so it teaches _learn_context_limit nothing, but it must still
    # reroute silently instead of surfacing as a hard, un-retryable 400.
    r"maximum prompt length", re.I)
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
    # cerebras/zai-glm-4.7: 'Please reduce the length of the messages or completion.
    # Current length is 23633 while limit is 8192'. Nothing below matched that
    # phrasing, so its real 8192 cap was never learned and routing kept offering it
    # for 30k+ agentic turns — a guaranteed 400 + wasted hop on every request.
    # Anchored on 'limit is' so it takes the CAP (8192), never the length (23633).
    re.compile(r"limit is (\d{3,7})", re.I),
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


# A per-MINUTE token cap phrased as a size rejection: groq free is
# 'Request too large ... on tokens per minute (TPM): Limit 8000, Requested 36430'.
# Grab the LIMIT (8000), never the Requested figure — anchored on 'Limit' right
# after the TPM/token-per-minute phrase so a big 'Requested N' can't be mismatched.
_TPM_LIMIT_RE = re.compile(
    r"(?:TPM|tokens[\s_-]*per[\s_-]*minute)[^0-9]{0,40}?Limit\s+(\d{3,7})", re.I)


def _learn_tpm_limit(pid, model, resp):
    """A 413 'request too large for the per-minute token budget' names a cap this
    model can NEVER exceed in one request (groq free = 8000 TPM). Treat it as an
    effective max-input so routing stops sending oversized agentic requests here —
    they 413 100% of the time otherwise, burning a hop every turn. Best-effort."""
    if not model:
        return
    try:
        text = resp.text or ""
    except Exception:
        return
    m = _TPM_LIMIT_RE.search(text)
    if not m:
        return
    limit = int(m.group(1))
    if limit < 1000:
        return
    with _model_max_input_lock:
        cur = _MODEL_MAX_INPUT.get((pid, model))
        _MODEL_MAX_INPUT[(pid, model)] = min(cur, limit) if cur else limit


def _sustain_penalty(pid):
    """Score demotion (points) for a SCARCE daily budget, applied only in agentic
    ordering. A coding CLI fires hundreds of turns; a 50/day tier (openrouter free)
    drains in an hour and then 503s, so it must NOT out-rank the sustainable large
    providers (cerebras 14,400/day, google 200/day, or any uncapped one) just because
    its benchmark score is a couple points higher. 0 for uncapped / >=150-day budgets;
    grows as the daily allowance shrinks below that.

    Divisor 5.0 (was 15.0): the first cut demoted openrouter by only ~6.7 points
    while the spread band is _ORCH_BAND (30) wide, so a 50/day tier stayed well
    inside the band and still took ~half of all agentic picks — draining the
    SCARCEST provider first while cerebras sat on 14,400/day. At /5 a 50/day tier
    loses 20 points (nemotron 104 -> 84), which puts it below cerebras (98.8) and
    google (101) instead of beside them. It stays in the fallback chain."""
    try:
        s = quota.status(pid)
    except Exception:
        return 0.0
    if not s.get("limit_known"):
        return 0.0
    lim = s.get("limit") or 0
    if lim <= 0 or lim >= 150:
        return 0.0
    return (150 - lim) / 5.0


_MODEL_ID_SUFFIX_RE = re.compile(r"(?::free|:beta|:extended|:nitro|:floor|:online)+$", re.IGNORECASE)


_FREE_TIER_SUFFIX_RE = re.compile(r"-free$")


def _normalize_model_identity(model_id):
    """Strip provider-added suffixes (openrouter's ':free', ':beta', etc.) AND
    the vendor namespace, then lowercase, so the SAME underlying model hosted by
    two different providers compares equal — e.g. nvidia's
    'nvidia/nemotron-3-ultra-550b-a55b' and openrouter's
    'nvidia/nemotron-3-ultra-550b-a55b:free' are one model, not two.

    MEASURED 2026-08-29: suffix-stripping alone was not enough, because hosts
    disagree about the NAMESPACE as well as the suffix. gpt-oss-120b ships as
    'openai/gpt-oss-120b' (groq, nvidia), bare 'gpt-oss-120b' (cerebras,
    sambanova) and '@cf/openai/gpt-oss-120b' (cloudflare) — three spellings of
    identical weights, which compared as three unrelated models, so none of the
    same-identity machinery below (penalty sharing, same-host alternation) ever
    fired across them. Keeping only the leaf fixes that: hosts rename the
    namespace, never the model itself."""
    base = _MODEL_ID_SUFFIX_RE.sub("", (model_id or "").strip().lower())
    # ...and the OTHER free-tier spelling. openrouter marks its free tier
    # ':free' (handled above); tokenrouter appends '-free' instead, so
    # 'z-ai/glm-5.3-free' and 'z-ai/glm-5.3' compared as two unrelated models
    # and none of the same-identity machinery below fired across them. Anchored
    # at the END so a model whose NAME contains the word ('free-willy-7b',
    # 'freeform-2') is untouched.
    base = _FREE_TIER_SUFFIX_RE.sub("", base)
    # Relay BACKEND prefixes, which g4f puts in front of the real id:
    # 'srv_mkom688d...:openai/gpt-oss-120b', 'pa:657cce02:auto',
    # 'nvidia:moonshotai/kimi-k3'. FOUND 2026-08-30 while testing the swarm
    # fan-out: without this, three g4f listings of ONE model read as three
    # different models, so the same opinion was bought three times -- and the
    # relay discount and same-model load sharing never saw them as one model
    # either. Everything after the LAST colon is the real id; the ':free'-style
    # suffixes were already removed above, so no genuine suffix is at risk.
    if ":" in base:
        base = base.rsplit(":", 1)[-1]
    return base.rsplit("/", 1)[-1]


def _model_identity_min_penalty(pool):
    """For a candidate pool [(score, pid, model), ...], map each (pid, model) to
    the LOWEST _sustain_penalty found among every candidate offering the exact
    same underlying model (by _normalize_model_identity), including itself.

    MEASURED 2026-07-27: nvidia's nemotron-3-ultra-550b (uncapped, penalty 0)
    and openrouter's identical model (50/day, penalty 20) scored 21 points
    apart for being the SAME model — the scarcity penalty exists to stop the
    router over-relying on a genuinely limited resource, which doesn't apply
    when an uncapped copy of the exact same model is sitting right there in
    the same pool. This lets the scarce copy inherit its sibling's penalty
    instead of eating its own, so _weighted_pick can give it a fair, non-
    trivial share instead of ~0%. A model with no same-identity sibling (the
    normal case) is completely unaffected — it only ever sees its own
    penalty, min() over a single-element list."""
    by_identity = {}
    for _score, pid, model in pool:
        by_identity.setdefault(_normalize_model_identity(model), []).append(pid)
    effective = {}
    for _score, pid, model in pool:
        sibling_pids = by_identity.get(_normalize_model_identity(model), [pid])
        effective[(pid, model)] = min(_sustain_penalty(p) for p in sibling_pids)
    return effective


# ── TOOL-PROTOCOL COMPATIBILITY ────────────────────────────────────────────
# A benchmark score measures how SMART a model is. It says nothing about whether
# the model speaks the tool dialect the calling CLI can actually consume — and a
# mismatch there is fatal, silently: the hub returns a clean 200, the tool_call
# arguments are valid JSON, and the CLI then dies locally on the payload.
# MEASURED 2026-07-25 with Codex's own apply_patch schema:
#   deepseek-v4-pro -> '{"input":"diff --git a/hello.txt b/hello.txt\nnew file
#                        mode 100644\nindex 0000000..45b983b\n--- /dev/null..."}'
#                      i.e. a GIT-style unified diff -> codex_core::tools::router
#                      "Fatal error: tool apply_patch invoked with incompatible
#                      payload" -> whole run produced ZERO files.
#   gpt-oss-120b    -> '{"input":"--- /dev/null\n+++ b/hello.txt\n@@\n+hi\n"}'
#                      i.e. the plain patch form codex accepts -> run succeeded.
# So the strongest model on paper was the one that could not build anything.
# Demote the dialects proven to break agentic CLIs; this is a COMPATIBILITY
# penalty, not a quality judgement, and it only applies to tool/agentic routing
# (plain chat is unaffected — these models are excellent there).
#   minimax-m2.7    -> emits a well-formed apply_patch call BUT also leaks the same
#                      JSON into the TEXT channel ('{\"input\":...' as output_text),
#                      so codex sees a malformed turn and exits silently: exit 0,
#                      no error, no files, ONE request. Hardest failure to spot —
#                      the hub logs a clean 200 and nothing looks wrong.
# RE-MEASURED 2026-07-27 (6 direct apply_patch repros, raw deltas captured before
# any hub-side normalization): deepseek-v4-pro and zai-glm-4.7 BOTH invent a
# DIFFERENT JSON shape almost every call for the exact same tool/prompt — seen:
# {"input":"diff --git..."} (git-header pollution, the one _normalize_apply_patch_diff
# targets), {"lines":[{"type":"new_file",...}]}, {"operation":{"create_file":{...}}},
# a STRINGIFIED nested JSON blob under a random key ("d43bd"), a "_scratchpad"
# wrapper, bare {"patch": "..."} (sometimes valid, sometimes not), {"content",
# "filepath"}, and even a bare "{}". This is not one fixable pattern — a
# normalizer can only ever chase the shapes already seen. zai-glm-4.7 additionally
# produced a run with ZERO files after 2 fatal attempts and no shell fallback
# (session just gave up). deepseek-v4-pro was already excluded below; zai-glm-4.7
# is added on this evidence rather than promoted to _TOOL_PROVEN as originally
# considered.
# PROVEN GOOD (each completed a full multi-file Codex build end-to-end tonight):
#   cerebras/gpt-oss-120b, nvidia+openrouter nemotron-3-*, google gemini-3.x,
#   mistral-medium. Those built the pest-control site and the SVG dashboard.
_TOOL_DIALECT_PENALTY = 25.0
_TOOL_DIALECT_MISMATCH = ("deepseek-v4", "minimax-m2.7", "minimax-m3", "glm-4.7")

# A blacklist of broken dialects is whack-a-mole: three runs found three DIFFERENT
# model-specific ways to fail, each invisible to the hub (clean 200 every time).
# So agentic routing prefers an ALLOWLIST instead — models that have actually driven
# a coding CLI to completion. New/unknown models are then "unproven" (fallback)
# rather than "assumed good", which is the safe default for a protocol this strict.
# EVIDENCE (2026-07-25, real Codex runs, output browser-verified):
#   gpt-oss-120b     built the single-file SVG dashboard (chart, sortable table,
#                    theme toggle) — all interactions worked.
#   nemotron-3-*     built the 3-file French pest-control site — nav toggle, FAQ
#                    accordion, form validation all verified in a browser.
#   gemini-3.x       served turns in both successful builds.
#   mistral-medium   served turns in the first successful build.
# Add to this list ONLY after a model completes a real multi-file build.
_TOOL_PROVEN = ("gpt-oss", "nemotron", "gemini-3", "mistral-medium")


def _may_lead_agentic(score, model_id):
    """True when this model is allowed to LEAD an agentic build.

    Was `_is_tool_proven` alone, i.e. the four families in _TOOL_PROVEN, and
    since the pool is `_proven or agentic` that meant: whenever ANY allowlisted
    model was available, nothing else could lead at all. MEASURED 2026-08-31, a
    hard build routed to qwen3.8 at 134.1 while glm-5.3 at 138 -- the top of the
    whole ranking, and the model the user had just asked for by name -- was not
    in the chain.

    The allowlist was right when it was written: three runs, three different
    silent tool-dialect failures, zero files each time, and nothing downstream
    to catch them. The hub has since learned to reject a tool call typed out as
    prose, reject a turn that only announced work, mark a non-answering id dead,
    and demote by measured delivery -- so that failure is now caught after the
    fact instead of only being prevented beforehand.

    So the gate is WIDENED, not removed: allowlisted, or in the top band. An
    unproven mid-tier model still may not lead a build."""
    return _is_tool_proven(model_id) or score >= _PREF_FLOORS[5]


def _is_tool_proven(model_id):
    """True for a model empirically shown to complete an agentic CLI build."""
    low = (model_id or "").lower()
    return any(f in low for f in _TOOL_PROVEN)


def _tool_dialect_penalty(model_id):
    """Points deducted from an AGENTIC pick for a model whose tool-call payloads
    a CLI cannot consume. Extend _TOOL_DIALECT_MISMATCH only with EVIDENCE from a
    real failed run — never on suspicion."""
    low = (model_id or "").lower()
    return _TOOL_DIALECT_PENALTY if any(f in low for f in _TOOL_DIALECT_MISMATCH) else 0.0


def _agentic_score(entry, sustain_override=None):
    """Ordering key for agentic (tool) routing: raw strength, minus the scarce-budget
    penalty (so sustainable providers lead), minus the tool-dialect penalty (so a model
    that a CLI cannot actually execute never wins the slot no matter how strong it is).
    A model that writes brilliant code the CLI throws away is worth nothing here.

    `sustain_override` (from _model_identity_min_penalty, computed once per pool by
    the caller) lets a scarce-quota copy of a model inherit a non-scarce sibling's
    penalty instead of its own — omit it (default None) to get the plain per-
    provider penalty, exactly as before this parameter existed.

    ...and minus the LEARNED reliability penalty (see _reliability_penalty): a
    hop with a real track record of not delivering here gets demoted no matter
    how well it benchmarks. Penalty-only and neutral-when-unknown, so a model
    the hub has never routed to scores exactly as it always did."""
    penalty = (sustain_override.get((entry[1], entry[2])) if sustain_override is not None
               else None)
    if penalty is None:
        penalty = _sustain_penalty(entry[1])
    return (entry[0] - penalty - _tool_dialect_penalty(entry[2])
            - _reliability_penalty(entry[1], entry[2])
            - _latency_penalty(entry[1], entry[2]))


def _context_ok(pid, model, est):
    """False once we've LEARNED this (pid, model) can't hold an est-token request
    (5% headroom for estimate error). True when unknown — never blocks on a guess."""
    if not est:
        return True
    lim = _MODEL_MAX_INPUT.get((pid, model))
    return lim is None or est <= lim * 0.95


_MISSING_MODEL_RE = re.compile(
    r"model_not_found|model not found|no such model|does not exist|"
    r"unknown model|invalid model|unsupported model|not supported|"
    r"model .* not (?:found|available)", re.I)


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

# Harmless read-only GET endpoints exempt from the control token: they expose
# a single non-sensitive value and exist so local agent tooling (which has no
# token) can read them. Writes to the same paths keep full protection.
_CONTROL_TOKEN_EXEMPT_GETS = ("/api/web-search-policy",)


def _has_control_token():
    """Whether THIS request carries the per-install control token, i.e. whether
    it came from the dashboard. Same comparison the gate makes."""
    token = config.get_control_token()
    if not token:
        return True                    # no token configured: everything is local
    supplied = (request.headers.get("X-Free-LLM-Hub-Token")
                or request.args.get("token"))
    return bool(supplied and hmac.compare_digest(str(supplied), str(token)))


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
    if request.path.startswith("/api/") and not _skips_control_gate(request.path):
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
        if request.method == "GET" and request.path in _CONTROL_TOKEN_EXEMPT_GETS:
            return None
        token = config.get_control_token()
        supplied = request.headers.get("X-Free-LLM-Hub-Token") or request.args.get("token")
        if token and not (supplied and hmac.compare_digest(str(supplied), token)):
            return jsonify({"error": "missing or invalid control token",
                            "code": "token_required"}), 401
    return None


@app.after_request
def _security_headers(response):
    # An artifact document sets its own CSP (see serve_artifact) — setdefault
    # below would leave it alone anyway, but X-Frame-Options: DENY would not,
    # and that would stop the preview panel embedding it at all.
    if request.path.startswith("/artifact/"):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response
    nonce = getattr(g, "csp_nonce", "")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'none'; script-src 'nonce-%s'; "
        "style-src 'unsafe-inline'; img-src 'self' data:; "
        # <video>/<audio> fall back to default-src 'none' without this, so a
        # project's own video in the file preview is refused before it loads.
        # Same-origin only: the bytes come from /api/workspace/raw, which is
        # token-gated and path-confined to the project directory.
        "media-src 'self'; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; "
        # The workspace preview frames the user's OWN project, which runs on a
        # loopback port we allocate at run time (PORT_RANGE). Without an explicit
        # frame-src this falls back to default-src 'none' and the iframe is
        # refused outright — measured: "Refused to frame 'http://127.0.0.1:5801/'".
        # Scoped to loopback only, so this still cannot frame anything remote --
        # plus the one named exception below for the Tutorial AR page's single
        # embedded YouTube video.
        "frame-src http://127.0.0.1:* http://localhost:* "
        "https://www.youtube.com; "
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
    if not (request.path.startswith("/v1") or _is_ollama_path(request.path)):
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

# The Ollama surface lives under /api/, which is otherwise the DASHBOARD control
# API and is gated by the per-install control token plus an anti-CSRF header. No
# Ollama client can send either -- the protocol has no auth at all -- so these
# exact paths are carved out of that gate and put behind the gateway key instead,
# exactly like /v1. Exact paths, never a prefix: "/api/chat" must not drag
# "/api/chat/history" (a real control endpoint) out of the token gate with it.
# Paths that exist ONLY to serve Ollama. Nothing else answers them, so they are
# carved out of the control gate unconditionally and the views themselves refuse
# while the emulation is off -- which turns "enable the ollama_api flag" into the
# answer an Ollama client actually receives, instead of a 401 about a dashboard
# token it has never heard of and cannot send.
_OLLAMA_ONLY_PATHS = frozenset({
    "/api/tags", "/api/chat", "/api/generate", "/api/show", "/api/ps",
    "/api/embed", "/api/embeddings",
})

# /api/version is SHARED: it was already the hub's own version endpoint long
# before Ollama emulation existed, and Ollama clients probe the same path to
# decide whether they are talking to an Ollama server at all. It therefore stays
# token-gated as before and only opens up while the emulation is on -- see
# api_version, which answers whichever caller asked.
_OLLAMA_SHARED_PATHS = frozenset({"/api/version"})

_OLLAMA_PATHS = _OLLAMA_ONLY_PATHS | _OLLAMA_SHARED_PATHS


def _ollama_enabled():
    """Off by default: an extra unauthenticated-by-default shape on the control
    port should exist because someone asked for it, not because the hub shipped
    it switched on."""
    return config.get_flag("ollama_api", False)


def _is_ollama_path(path):
    return path in _OLLAMA_PATHS and _ollama_enabled()


def _skips_control_gate(path):
    return path in _OLLAMA_ONLY_PATHS or (path in _OLLAMA_SHARED_PATHS
                                          and _ollama_enabled())


@app.before_request
def _guard_v1():
    if not (request.path.startswith("/v1") or _is_ollama_path(request.path)):
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
    if not supplied:
        # Gemini clients authenticate with Google's own header or ?key=, never
        # a bearer token -- google-genai and Gemini CLI have no way to send one.
        supplied = (request.headers.get("x-goog-api-key")
                    or request.args.get("key"))
    if supplied and hmac.compare_digest(str(supplied), str(local_key)):
        return None
    msg = ("Missing or invalid local API key. Send it as "
           "'Authorization: Bearer <key>' or 'x-api-key: <key>'.")
    if request.path.startswith("/v1/messages"):
        return _anthropic_error("authentication_error", msg, 401)
    if request.path.startswith("/v1beta"):
        return jsonify(wire_gemini.error_payload(msg, 401, "UNAUTHENTICATED")), 401
    if _is_ollama_path(request.path):
        return jsonify(wire_ollama.error_payload(msg)), 401
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
# WHERE A REQUEST CAME FROM. A hub-launched agent session is pointed at
# <hub>/build/<session_id> (agentic_chat._hub_base_url), so that prefix rides
# along on every call the CLI makes. The prefix is stripped in WSGI before
# routing, so every endpoint gets this for free and not one route is
# duplicated; the session id is left on the environ for the activity row.
#
# The path is the only channel available. An agent CLI is an ordinary API
# client that forwards none of its environment, so nothing else on the request
# distinguishes a session the dashboard started from one you started yourself
# in a terminal -- both are just "Codex" with the same User-Agent.
_BUILD_PREFIX_RE = re.compile(r"^/build/([A-Za-z0-9_-]{1,64})(/.*)$")


class _BuildPrefix:
    """Strip /build/<session_id> and remember it for the request."""

    def __init__(self, wsgi):
        self._wsgi = wsgi

    def __call__(self, environ, start_response):
        m = _BUILD_PREFIX_RE.match(environ.get("PATH_INFO") or "")
        if m:
            environ["flh.build_session"] = m.group(1)
            environ["PATH_INFO"] = m.group(2)
        return self._wsgi(environ, start_response)


app.wsgi_app = _BuildPrefix(app.wsgi_app)
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
            # First pick = what the ROUTER chose; later ones are fallback hops.
            hops = act.setdefault("hops", [])
            if len(hops) < 12:
                hops.append("%s/%s" % (pid, model))
            act["routed"] = act.get("routed") or ("%s/%s" % (pid, model))


def _act_pipeline_watcher():
    """on_event sink for swarm.run/crews.run, so the activity feed can show WHAT
    a multi-model pipeline is doing rather than one opaque row.

    A swarm request is ONE Flask request, so every stage's _act_pick lands on
    the SAME activity row: `hops` already accumulated all ~10 stage models, but
    with no way to tell the planner from a worker from the reviewer. swarm.py
    has emitted these events all along (12 emit() call sites) and app.py simply
    never passed an on_event -- the plumbing existed unused.

    Binds the activity dict directly instead of reading flask.g inside the
    callback: swarm runs independent phases in concurrent waves, and `g` is not
    shared with worker threads."""
    act = getattr(g, "act", None)
    if act is None:
        return None

    def _on_event(kind, detail):
        try:
            with _activity_lock:
                stages = act.setdefault("stages", [])
                if len(stages) < 40:          # a bounded trail, like `hops`
                    stages.append({"kind": str(kind)[:24],
                                   "detail": str(detail)[:80],
                                   "at": time.time()})
        except Exception:                                        # noqa: BLE001
            pass
    return _on_event


def _act_pipeline_result(result):
    """Stamp the finished pipeline's crew and its per-ROLE model list onto the
    activity row. swarm.py already returns result["models"] as (role, model)
    pairs -- 'plan', 'phase:<title>', 'supervisor', 'review', 'repair:<title>',
    'synthesis' -- which is exactly the "which agent used which model" view the
    feed was missing; it was only ever rendered into the answer's text trailer."""
    act = getattr(g, "act", None)
    if act is None or not isinstance(result, dict):
        return
    try:
        pairs = [(str(r)[:48], str(m)[:64])
                 for r, m in (result.get("models") or []) if m]
        with _activity_lock:
            if result.get("crew"):
                act["crew"] = str(result["crew"])[:24]
            if pairs:
                act["pipeline"] = [{"role": r, "model": m} for r, m in pairs]
    except Exception:                                            # noqa: BLE001
        pass


def _act_hop_failed(reason):
    """Annotate the hop currently being attempted with why it fell through, so the
    activity feed shows the REAL story ('router picked A, A refused, answered by B')
    instead of only the model that happened to answer."""
    act = getattr(g, "act", None)
    if act is None:
        return
    with _activity_lock:
        hops = act.get("hops")
        if hops and "!" not in hops[-1]:
            hops[-1] = "%s ! %s" % (hops[-1], str(reason)[:40])


class _HopErrors(list):
    """The per-request fallback-error list, with one addition: every append also
    marks WHY the hop being attempted fell through, on the activity trail. Plain
    list semantics otherwise ('; '.join(errors), truthiness, len — all unchanged)."""

    def append(self, item):
        list.append(self, item)
        try:
            _act_hop_failed(item)
        except Exception:
            pass    # diagnostics must never break a request


def _activity_done(act, status, http=None):
    with _activity_lock:
        if act.get("finished") is None:
            act["status"] = status
            act["http"] = http
            act["finished"] = time.time()


def _build_sid():
    """The agent session id this request arrived under, or None."""
    try:
        return request.environ.get("flh.build_session")
    except Exception:                                            # noqa: BLE001
        return None


def _build_project():
    """Folder name of the project a /build request belongs to, or None.

    The basename, not the full path: the activity row is a narrow column and
    'project-20260830-024030' identifies it, while
    'C:/Users/.../calvoun-projects/project-20260830-024030' just pushes every
    other field off the screen."""
    sid = _build_sid()
    if not sid:
        return None
    try:
        sess = agentic_chat.get_session(sid) or {}
        d = sess.get("project_dir")
        return os.path.basename(str(d).rstrip("/\\")) if d else None
    except Exception:                                            # noqa: BLE001
        return None


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
            # "build"  = a CLI session the dashboard's /build page started
            # "cli"    = anything else pointed at the hub (your own terminal,
            #            a script, another tool)
            # "build" = a session the dashboard's /build page started (and we
            # know WHICH project, because the session id came in on the path);
            # "cli"   = anything else pointed at the hub -- your own terminal,
            # a script, another tool.
            "source": "build" if _build_sid() else "cli",
            "project": _build_project(),
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
# Peek-safe variant: only an error that actually CARRIES a value (an object or a
# string). The shared _STREAM_ERROR_RE also matches `"error": null` / `"error": {}`,
# which several OpenAI-compatible gateways put in every envelope — harmless in
# _finalizing_body (real content later in the stream wins) but fatal in the peek,
# which returns on the FIRST match and would drop a perfectly healthy stream.
_STREAM_ERROR_VALUE_RE = re.compile(rb'event:\s*error\b|"error"\s*:\s*(?:"|\{(?!\s*\}))')
# Reasoning/thinking deltas. NOT final content (so they can't satisfy the content
# gate on their own) but they ARE proof the upstream is alive and working, which is
# what lets a slow reasoning model keep its turn instead of being judged empty.
_STREAM_REASONING_RE = re.compile(
    rb'"reasoning_content"\s*:\s*"(?:\\|[^"])|"(?:thinking|reasoning)"\s*:\s*"(?:\\|[^"])'
    rb'|thinking_delta|reasoning_summary_text\.delta')


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
    # Whole providers sidelined by repeated key-level failures. These silently
    # remove EVERY model of a provider from routing, so they must be visible —
    # an invisible sideline looks exactly like "the router keeps choosing one model".
    pr = [{"provider": p, "expires_in": s,
           "reason": "key (401/402)" if p in _provider_keyfail else "forbidden (403)"}
          for p, s in _dead_provider_rows()]
    pr.sort(key=lambda r: r["provider"])
    return jsonify({"dead": rows, "count": len(rows), "ttl_seconds": _DEAD_MODEL_TTL,
                    "dead_providers": pr, "provider_count": len(pr),
                    "provider_ttl_seconds": _PROVIDER_DEAD_TTL})


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
    # What routing has LEARNED (see _record_outcome), alongside what it did --
    # a demotion that cannot be inspected is indistinguishable from a bug, and
    # the activity feed is already the routing-diagnostics surface. Only hops
    # carrying a real penalty are listed, worst first.
    with _outcome_lock:
        ledger = [(p, m, r.get("ok", 0), r.get("fail", 0)) for (p, m), r in _outcomes.items()]
    learned = []
    for pid, model, ok, fail in ledger:
        penalty = _reliability_penalty(pid, model)
        if penalty > 0:
            learned.append({"provider": pid, "model": model, "ok": ok, "failed": fail,
                            "reliability": round(_reliability(pid, model), 3),
                            "score_penalty": round(penalty, 2)})
    learned.sort(key=lambda r: r["score_penalty"], reverse=True)
    return jsonify({"activity": out, "learned_unreliable": learned[:15]})


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

# Each dashboard view has its own real URL, so it can be opened in a new tab,
# bookmarked or shared. The frontend holds the same map (VIEW_PATHS in
# index.html) and decides WHICH view to show from the path; the server's only
# job is to serve the app for these paths instead of 404ing on a hard refresh.
# An explicit list, not a catch-all: a typo must still 404 rather than silently
# render the dashboard, and /api, /v1 and /artifact keep their own handlers.
_VIEW_SLUGS = ("hub", "activity", "chat", "agent", "images", "providers",
               "image-providers", "subscriptions", "routing", "quota",
               "usage", "tracking", "tutorial-ar", "settings")


@app.route("/<any(" + ", ".join("'%s'" % s for s in _VIEW_SLUGS) + "):slug>")
@app.route("/<any(" + ", ".join("'%s'" % s for s in _VIEW_SLUGS) + "):slug>/<path:rest>")
def view_page(slug, rest=None):
    """Serve the dashboard for a view URL.

    `rest` carries a session id (/agent/<id>) and is deliberately not validated
    here -- the page reads it from the address bar itself and every API it then
    calls validates it. Accepting it just means a refresh returns to the same
    conversation instead of losing it."""
    return index()


@app.route("/")
def index():
    try:
        page = make_response(
            render_template("index.html", csp_nonce=getattr(g, "csp_nonce", ""),
                            control_token=config.ensure_control_token()))
        # The dashboard is one file: markup, styles and ALL of its JavaScript.
        # Flask sends it with no freshness headers, so a browser is free to keep
        # serving the copy it already has -- and a kept copy keeps running the
        # code that shipped with it. That produced console errors from code
        # paths which no longer exist in the file on disk (a srcdoc fallback
        # removed hours earlier still throwing CSP violations), which is a
        # miserable thing to debug: the source says one thing, the browser does
        # another. Restarting the hub must mean the next load runs the current
        # build. It is a single local request, so there is nothing to save by
        # caching it.
        page.headers["Cache-Control"] = "no-store, must-revalidate"
        return page
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

def _key_rows(pid, keys):
    """One row per key: how to display it, and how it has actually behaved.

    Usage is per key rather than per provider because that is the only level at
    which "this one is exhausted" is a statement you can act on -- with a pool,
    the provider total says nothing about which member is carrying it or which
    is being rotated onto and rejected every time."""
    used = quota.keys(pid)
    rows = []
    for i, k in enumerate(keys):
        stat = used.get(quota.key_fingerprint(k)) or {}
        rows.append({
            "masked": _mask_key(k),
            "index": i,
            "requests": stat.get("count", 0),
            "ok": stat.get("ok", 0),
            "failed": stat.get("fail", 0),
            "last_used": stat.get("last", 0) or None,
        })
    return rows


def _mask_key(k):
    """Safe display form of a key: first4 + '…' + last4 (or '••••' if <9 chars).
    NEVER returns the full key — the reveal route is the only full-key surface."""
    s = k if isinstance(k, str) else str(k or "")
    if len(s) < 9:
        return "••••"
    return s[:4] + "…" + s[-4:]


# Providers that ONLY actually work through a local-subscription CLI relay
# (a _SUB_PROVIDERS pid) rather than direct HTTP -- lets the provider card link
# to and show the live status of the thing that actually works, instead of just
# failing its own Test button silently. EMPTY since 2026-07-31: its only entry
# was {"agentrouter": "sub-agentrouter"}, and both halves were removed at user
# request. The machinery below is generic and stays for the next such provider;
# with an empty map every branch that consults it is simply skipped.
_PROVIDER_RELAY_SUB_PID = {}


def _provider_quota_row(pid, p):
    """{used, limit, remaining, window, exhausted, resets_in, resets_at,
    limit_known} for the card, or None when there is no free budget to report.

    A PAID provider has no free tier, so quota.status() reports it as exhausted
    with limit 0 — surfacing that would paint a perfectly healthy paid card red
    about an allowance it never had (the same reasoning the quota banner already
    uses). Those cards report their real allowance separately (see
    _puter_allowance) or nothing at all."""
    if p.get("paid"):
        return None
    try:
        s = quota.status(pid)
    except Exception:                                                # noqa: BLE001
        return None
    return {k: s.get(k) for k in ("used", "limit", "remaining", "window",
                                  "exhausted", "throttled", "resets_in",
                                  "resets_at", "limit_known")}


# USER 2026-08-04: "put recommended... for the best providers that give
# best models llms in quality... and highlight new for new providers." The
# 'recommended' flag was a single hand-curated provider (puter) -- everyone
# else showed as an ordinary card no matter how strong its actual models
# were. Reuses _benchmark_score (the same scorer that already ranks every
# model for routing) instead of a second scoring system: a provider's
# quality is the best score any of its own free models can reach. 95 sits
# just above the _STRONG_ROOTS auto-detected "new flagship release" tier
# (100 base -> ~95 after the small per-provider speed/coding adjustments
# _benchmark_score applies), so a provider only needs ONE genuinely strong
# free model to qualify -- weak/tiny-tier models top out far below this
# (see the Tier D comment: 18).
_RECOMMENDED_QUALITY_THRESHOLD = 95
# Providers added in the last few days of work on this hub (git log
# providers.py, 2026-08-04) -- not derived at request time (git isn't
# available/fast enough to shell out to on every /api/providers call), so
# this is a snapshot list, same tradeoff the static 'recommended' flag
# already makes. Update when a real new batch lands.
_NEW_PROVIDER_IDS = frozenset((
    "tokenrouter",
    "deepinfra", "together", "hyperbolic", "nebius", "cohere",
    "scaleway", "stepfun", "aion", "sealion", "requesty",
    "dahl",
    # Not a new provider, but newly NEEDING SETUP: g4f.space ended anonymous
    # access on 2026-08-06 (keyless chat now 402s "No cake credits"), so it
    # went from working-out-of-the-box to needing a free key from
    # g4f.dev/members.html. Badged so the change is visible on the card
    # instead of silently reading as "connected" until a request fails.
    # (Also the date the three per-upstream g4f-* cards merged into this one.)
    "g4f",
))


def _provider_quality_score(pid, free_models):
    """Best _benchmark_score any of this provider's own free models reaches.
    0 if it has no free models to score (paid-only, or discovery came back
    empty) -- never raises, a scoring quirk on one model must not break the
    whole provider list."""
    best = 0.0
    for m in free_models or ():
        try:
            s = _benchmark_score(pid, m)
        except Exception:
            continue
        if s > best:
            best = s
    return best


def _provider_row(pid, live_models=False):
    p = prov.get_provider(pid) or {}
    pcfg = config.get_provider_config(pid)
    keys = pcfg.get("api_keys") or []
    # Puter publishes no quota anywhere, so the hub reads its real remaining
    # allowance instead of leaving the card blank (see _puter_allowance).
    allowance = _puter_allowance() if pid == "puter" and keys else None
    relay_pid = _PROVIDER_RELAY_SUB_PID.get(pid)
    relay = None
    if relay_pid and relay_pid in _SUB_PROVIDERS:
        r_enabled, r_installed, r_authed, r_detail = _sub_state(relay_pid)
        relay = {
            "sub_pid": relay_pid,
            "name": _SUB_PROVIDERS[relay_pid]["name"],
            "usable": bool(r_enabled and r_installed and r_authed),
            "detail": r_detail,
        }
    # Provider rows never trigger a network model-discovery call by default:
    # a save/list must be instant and can't fail on a provider's flaky /models
    # endpoint. The live model list is served separately by GET /api/models.
    return {
        "id": pid,
        "name": p.get("name") or pid,
        "enabled": bool(pcfg.get("enabled")),
        "has_key": bool(keys),
        "key_count": len(keys),
        "keys": _key_rows(pid, keys),
        "signup_url": prov.signup_url(pid),
        "key_hint": p.get("key_hint") or "",
        "notes": p.get("notes") or "",
        "paid": bool(p.get("paid")),
        "trial": bool(p.get("trial")),
        "no_key": bool(p.get("no_key")),   # open gateway: usable with NO api key
        # vendor documents anonymous instant keys -> the card can offer one click
        "can_mint_key": bool(p.get("key_mint_url")),
        "free_models": (lambda fm: fm)(provider_free_models(pid, live=live_models)),
        "image_free_count": sum(1 for r in _image_model_rows(pid) if r.get("free", True)),
        "image_paid_count": sum(1 for r in _image_model_rows(pid) if not r.get("free", True)),
        "relay": relay,
        "allowance": allowance,     # puter only; None everywhere else
        # Free-quota state so the card can go red and count down to the reset
        # instead of the user discovering exhaustion through failed requests.
        # Cheap: quota.status reads local counters, no network.
        "quota": _provider_quota_row(pid, p),
        # Hand-curated OR earns it on real model quality -- see
        # _RECOMMENDED_QUALITY_THRESHOLD's comment for why 95.
        "recommended": bool(p.get("recommended")) or
                       _provider_quality_score(pid, p.get("default_free_models")) >=
                       _RECOMMENDED_QUALITY_THRESHOLD,
        "quality_score": _provider_quality_score(pid, p.get("default_free_models")),
        "new": pid in _NEW_PROVIDER_IDS,
    }


@app.route("/api/providers", methods=["GET"])
def api_providers():
    # include_custom stays the default (False): the generic "Custom
    # (OpenAI-compatible)" card was briefly surfaced here so a not-yet-
    # registered provider (AIAND) could be configured through it, but AIAND
    # now has its own proper registry row with a confirmed base_url -- the
    # generic card is redundant again and the user asked for it hidden.
    return jsonify([_provider_row(p["id"]) for p in prov.list_providers()])


# Providers whose signup needs a real human step no script should try to do for
# you. Surfaced so the guided setup can WARN and let you skip, instead of
# sending you to a page that dead-ends five minutes in. Sourced from each
# provider's own `notes` in providers.py (kept here rather than there because
# this is an onboarding-UI concern, not a protocol fact the router uses).
#
# The reason this list exists at all: the obvious "just automate the signups"
# shortcut is a ToS violation on essentially every provider, risks the Google
# account driving it, and breaks on CAPTCHA/phone/KYC anyway. So the honest
# design is to make the HUMAN path fast, and be upfront about which ones cost
# real effort.
_SIGNUP_FRICTION = {
    "nararouter":  "Needs a Telegram join and a credit card on file.",
    "mistral":     "Needs phone verification (and a card for some regions).",
    "glm":         "Needs phone verification.",
    "siliconflow": "Needs Chinese real-name verification (实名认证) for the full quota.",
    "modelscope":  "Needs KYC / a Chinese account to finish signup.",
    "baidu":       "Needs KYC and phone verification.",
    "tencent":     "Needs KYC to finish signup.",
}


@app.route("/api/onboarding", methods=["GET"])
def api_onboarding():
    """The guided key-setup list: the best providers NOT yet connected, in the
    order worth doing them.

    Deliberately just an ORDERED LIST OF LINKS. It opens each provider's own
    signup/key page for YOU to click through; nothing here automates account
    creation, and it never touches a provider's site itself. Automating the
    signups was considered and rejected: it breaks nearly every provider's
    terms, gets keys revoked, and endangers the email account used to do it.

    Sorted friction-free first, then by real model quality (_provider_quality_
    score, the same _benchmark_score that ranks models for routing) -- so the
    quick wins come first and the phone/KYC ones are last and clearly labelled.
    """
    steps = []
    for p in prov.list_providers():
        pid = p["id"]
        if p.get("no_key"):
            continue                       # keyless: nothing to set up
        if not p.get("signup_url"):
            continue                       # nowhere to send them
        cfg = config.get_provider_config(pid)
        if cfg.get("api_keys"):
            continue                       # already connected
        friction = _SIGNUP_FRICTION.get(pid)
        steps.append({
            "id": pid,
            "name": p.get("name") or pid,
            "signup_url": p.get("signup_url"),
            "key_hint": p.get("key_hint") or "",
            "quality_score": _provider_quality_score(pid, p.get("default_free_models")),
            "recommended": bool(p.get("recommended")),
            "new": pid in _NEW_PROVIDER_IDS,
            "friction": friction,
            "free_models": (p.get("default_free_models") or [])[:3],
        })
    steps.sort(key=lambda s: (s["friction"] is not None, -s["quality_score"]))
    return jsonify({"steps": steps, "remaining": len(steps)})


def _sync_relay_enable(pid):
    """When a provider that only actually works through a local-subscription
    relay (see _PROVIDER_RELAY_SUB_PID -- EMPTY since 2026-07-31, so this is a
    no-op today) gets enabled from its OWN card, also turn on the
    master local-subscriptions switch and that relay's per-provider flag.
    Without this, "Enabled" on the provider card silently does nothing --
    the thing that actually works lives in a completely separate opt-in
    system (off by default) the user would otherwise have to discover and
    flip on themselves in a different section of the dashboard."""
    relay_pid = _PROVIDER_RELAY_SUB_PID.get(pid)
    if not relay_pid or relay_pid not in _SUB_PROVIDERS:
        return
    if not _sub_master_on():
        config.set_flag(_SUB_MASTER_FLAG, True)
    relay_flag = _SUB_PROVIDERS[relay_pid]["flag"]
    if not config.get_flag(relay_flag, True):
        config.set_flag(relay_flag, True)


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
        if kwargs.get("enabled"):
            _sync_relay_enable(pid)
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
    replaced = _puter_replace_same_account(key) if _is_puter_driver(pid) else False
    config.add_provider_key(pid, key)
    # Adding a key signals intent to use this provider -> auto-enable it (the
    # user can still toggle it off). Idempotent: only flips a disabled row.
    if not config.get_provider_config(pid).get("enabled"):
        config.set_provider_config(pid, enabled=True)
        _sync_relay_enable(pid)
    with _model_cache_lock:
        _model_cache.pop(pid, None)  # pool changed -> rediscover
    _autoselect_default_if_unset()  # first keyed provider -> pick a best default
    row = _provider_row(pid)
    if replaced:
        row["note"] = ("Refreshed the token for this Puter account — it was already "
                       "connected, so this replaced the old one instead of adding a "
                       "second. To pool a SECOND allowance, sign in with a different "
                       "Puter account (each account has its own ~25¢/month).")
    return jsonify(row)


@app.route("/api/providers/<pid>/mint-key", methods=["POST"])
def api_provider_mint_key(pid):
    """One-click free key, for providers whose vendor documents anonymous minting.

    Only 'dahl' qualifies today: its docs say verbatim "Keys are free and each
    includes 100 million tokens. There is no payment UI yet — create another key
    when a key's allowance is spent." An unauthenticated POST to /tokens returns
    201 with a fresh key. No email, no card, no captcha.

    DELIBERATELY NOT AUTOMATIC. This fires only when the user clicks the button.
    Minting on every 402 would turn a documented 100M-token allowance into an
    unlimited one by rotating identities — which is the thing the vendor's cap
    exists to prevent, and is not ours to route around. `key_mint_url` is read
    from OUR registry, never from the request, so this cannot be pointed at an
    arbitrary host."""
    p = prov.get_provider(pid)
    if not p:
        return jsonify({"error": "unknown provider '%s'" % pid}), 404
    url = p.get("key_mint_url")
    if not url:
        return jsonify({"error": "%s does not offer instant free keys" % pid}), 400
    try:
        r = requests.post(url, timeout=(CONNECT_TIMEOUT, 20))
    except requests.RequestException as exc:
        return jsonify({"error": "could not reach %s: %s" % (url, exc)}), 502
    if r.status_code not in (200, 201):
        return jsonify({"error": "HTTP %s from %s: %s"
                        % (r.status_code, url, (r.text or "")[:200])}), 502
    try:
        data = r.json()
    except ValueError:
        return jsonify({"error": "%s did not return JSON" % url}), 502
    key = (data.get("token") or data.get("api_key") or data.get("key") or "").strip()
    if not key:
        return jsonify({"error": "no key field in the response from %s" % url}), 502
    config.add_provider_key(pid, key)
    if not config.get_provider_config(pid).get("enabled"):
        config.set_provider_config(pid, enabled=True)
        _sync_relay_enable(pid)
    with _model_cache_lock:
        _model_cache.pop(pid, None)
    _autoselect_default_if_unset()
    row = _provider_row(pid)
    allowance = data.get("available_tokens")
    if isinstance(allowance, int):
        row["note"] = "Free key created — %s tokens on it." % f"{allowance:,}"
    else:
        row["note"] = "Free key created."
    return jsonify(row)


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


def _run_relay_model_test(pid):
    """Run one real, minimal generation PER MODEL a sub-* relay exposes.
    Shared by /api/subscriptions/<pid>/test and, for any regular provider
    mapped in _PROVIDER_RELAY_SUB_PID, /api/test/<pid> -- same relay, same
    one-real-call-per-model discipline, one implementation.

    Runs every model on its OWN thread, concurrently -- measured live
    tonight: 6 models run sequentially took several minutes (each real CLI
    call runs up to ~45s), unusable behind a single click. Each call is a
    genuinely separate subprocess with its own argv/env (isolated
    CODEX_HOME/CLAUDE_CONFIG_DIR per backend, model passed as a plain
    argument) -- nothing mutable is shared between them to race on, so this
    turns the wall-clock cost into "as slow as the single slowest model"
    instead of "the sum of all of them".

    Skips a model already marked dead (_is_model_dead) -- no point spending a
    real call (and, for AgentRouter, real shared quota) reconfirming a
    failure the hub already knows about; it retries automatically once the
    6h dead-TTL expires.

    Returns (any_ok, results); results is [{model, ok, response/error,
    skipped?}, ...], in the same order _sub_models(pid) returns."""
    models = _sub_models(pid)
    results = [None] * len(models)

    def _one(i, m):
        if _is_model_dead(pid, m):
            results[i] = {"model": m, "ok": False, "skipped": True,
                          "error": "Skipped -- already marked unavailable; retries automatically later."}
            return
        status, text, detail = _sub_run(pid, "Reply with just the word OK, nothing else.", model=m)
        ok = status == 200
        results[i] = {"model": m, "ok": ok,
                      "response": text if ok else None,
                      "error": None if ok else (detail or ("HTTP %d" % status))}

    threads = [threading.Thread(target=_one, args=(i, m), daemon=True) for i, m in enumerate(models)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    any_ok = any(r["ok"] for r in results)
    return any_ok, results


def _image_only_provider_test(pid, pcfg):
    """/api/test verdict for an IMAGE-ONLY provider (AI Horde today): it has no
    chat endpoint at all, so the chat-completions probe in api_test_provider is
    a guaranteed false negative ("no models_url and no default model"). Test the
    provider's ACTUAL capability instead — cheaply: a real anonymous AI Horde
    image generation queues ~24min (measured 2026-07-27), untestable behind a
    dashboard click, so this probes the public /status/models?type=image feed.
    ok=True therefore means "image gateway healthy, workers online right now"
    and the detail says exactly that — never "generation verified". A saved
    personal key is additionally verified against /find_user (the documented
    whoami), because THAT is what real generations would authenticate with.
    Returns (ok, detail, sample_models)."""
    if pid != "aihorde":
        # No known cheap health probe — fall through to the chat path's honest
        # "nothing to test with" error rather than inventing a verdict.
        return None
    headers = {"Client-Agent": "free-llm-hub:1.0:https://github.com/last-million/free-llm-hub"}
    keys = pcfg.get("api_keys") or []
    if keys:
        try:
            ur = requests.get("https://aihorde.net/api/v2/find_user",
                              headers=dict(headers, apikey=keys[0]),
                              timeout=(CONNECT_TIMEOUT, MODELS_READ_TIMEOUT))
        except requests.RequestException as exc:
            return False, _sanitize("Image-only provider: AI Horde key check failed — %s: %s"
                                    % (exc.__class__.__name__, exc)), []
        if ur.status_code in (401, 403):
            return False, ("Image-only provider: the saved AI Horde key is INVALID "
                           "(find_user HTTP %d) — generations would 403. Remove it to run "
                           "on the anonymous key, or paste a valid one." % ur.status_code), []
    try:
        resp = requests.get("https://aihorde.net/api/v2/status/models?type=image",
                            headers=headers,
                            timeout=(CONNECT_TIMEOUT, MODELS_READ_TIMEOUT))
    except requests.RequestException as exc:
        return False, _sanitize("Image-only provider: AI Horde status probe failed — %s: %s"
                                % (exc.__class__.__name__, exc)), []
    if resp.status_code != 200:
        return False, ("Image-only provider: AI Horde status probe returned HTTP %d — "
                       "gateway not healthy right now." % resp.status_code), []
    try:
        models = resp.json()
    except ValueError:
        models = []
    online = [m for m in models if isinstance(m, dict) and (m.get("count") or 0) > 0]
    if not online:
        return False, ("Image-only provider: AI Horde reachable but NO workers online "
                       "right now — a generation would queue indefinitely."), []
    workers = sum(m.get("count") or 0 for m in online)
    top = sorted(online, key=lambda m: -(m.get("count") or 0))
    sample = [m["name"] for m in top[:5] if m.get("name")]
    who = "saved key verified via find_user" if keys else "anonymous key"
    return True, ("Image-only provider (no chat): AI Horde healthy — %d image models "
                  "served by %d workers online right now (%s, status/models probe). Not a "
                  "generation test: a real anonymous render queues ~24min, so this verifies "
                  "live capacity, not one image." % (len(online), workers, who)), sample


@app.route("/api/test/<pid>", methods=["POST"])
def api_test_provider(pid):
    """Test a saved key. ok=True means ONLY ONE THING: a real 1-token generation
    just succeeded — this key can actually produce free output right now.
    (Sole exception: IMAGE-ONLY providers like AI Horde, where no chat endpoint
    exists — there ok=True means "image gateway healthy via status probe" and the
    detail says so explicitly; see _image_only_provider_test.)

    Used to stop at a clean /models listing and call that "Key OK". That is a
    false positive for a spent trial/wallet: a $0-balance account still lists its
    full catalog with HTTP 200 (proved live on aimlapi — 909 models listed, 200 OK,
    while every one of 4 keys 403'd on generation with "You've run out of funds").
    A green card that can't generate is worse than a red one — it hides the
    problem instead of surfacing it. So /models (when available) is now used ONLY
    to pick a real model id to test with and to enrich the response's
    sample_models; the PASS/FAIL verdict always comes from an actual generation.
    """
    p = prov.get_provider(pid)
    if not p:
        return jsonify({"ok": False, "detail": "unknown provider", "sample_models": []}), 404
    pcfg = config.get_provider_config(pid)
    key = pcfg.get("api_key")
    attempted = []  # (model, ok) for every REAL generation this call actually ran

    def _finish(ok, detail, sample_models, cache=True, keys=None):
        """Single exit point for every REAL attempt (everything past the
        precondition checks below). Persists the outcome and folds in any
        newly-discovered or newly-broken free model ids so the dashboard can
        show '🆕 <model>' / '⚠ <model> no longer works' without a separate
        polling mechanism. cache=False is only for preconditions (unknown
        provider, no key at all) that say nothing new about the provider's
        live state and would just churn tested_at.

        `keys` is the PER-KEY breakdown ([{index, masked, ok, detail}]) so a
        provider holding several keys can show which individual ones work.
        Absent (or empty) on the paths that never test a specific key."""
        payload = {"ok": bool(ok), "detail": detail, "sample_models": sample_models or [],
                   "keys": keys or []}
        if cache:
            try:
                new_models, stale_models = _record_test_result(
                    pid, ok, detail, sample_models, attempted=attempted)
                payload["new_models"] = new_models
                payload["stale_models"] = stale_models
            except Exception:
                _log.debug("[test-cache] record failed for %s", pid, exc_info=True)
                payload["new_models"] = []
                payload["stale_models"] = []
        return jsonify(payload)

    # A provider mapped in _PROVIDER_RELAY_SUB_PID (empty since 2026-07-31)
    # only ever reaches its backend through the isolated CLI relay — a raw
    # HTTP call from here would eat that gateway's generic-client block (e.g.
    # HTTP 401 "unauthorized client detected"), a false negative about the
    # provider, not a real signal. Route the whole test through the SAME
    # relay Subscriptions uses instead, one real call per model, then feed
    # the result into the same _finish()/_record_test_result() cache path
    # used below — "already tested" / staleness / new-model detection all
    # keep working exactly like every other provider, for free.
    relay_pid = _PROVIDER_RELAY_SUB_PID.get(pid)
    if relay_pid and relay_pid in _SUB_PROVIDERS:
        r_enabled, r_installed, r_authed, r_detail = _sub_state(relay_pid)
        if not (r_enabled and r_installed and r_authed):
            return _finish(False, "Only works through its isolated CLI relay, which isn't "
                                  "ready yet: %s" % r_detail, [], cache=False)
        any_ok, results = _run_relay_model_test(relay_pid)
        ok_models = [r["model"] for r in results if r["ok"]]
        bad = ["%s (%s)" % (r["model"], r.get("error") or "unavailable")
              for r in results if not r["ok"] and not r.get("skipped")]
        skipped = [r["model"] for r in results if r.get("skipped")]
        if ok_models:
            detail = "Relay OK — %d/%d models verified working right now via isolated CLI: %s." % (
                len(ok_models), len(results), ", ".join(ok_models))
        else:
            detail = "Relay reachable but no model could generate right now."
        if bad:
            detail += " Not available right now: %s." % "; ".join(bad)
        if skipped:
            detail += " Skipped (already known unavailable): %s." % ", ".join(skipped)
        attempted.extend((r["model"], r["ok"]) for r in results if not r.get("skipped"))
        return _finish(any_ok, detail, ok_models)

    # IMAGE-ONLY provider (registry image_models, no chat surface: no models_url,
    # no default chat model): the chat-completions probe below can NEVER succeed
    # — there is no chat endpoint — so route the test at the provider's real
    # capability instead (AI Horde: cheap status/models health probe, see the
    # helper). ok=True means "image gateway healthy", truthfully labelled.
    if p.get("image_models") and not p.get("default_free_models") \
            and not _models_url_for(pid, pcfg):
        verdict = _image_only_provider_test(pid, pcfg)
        if verdict is not None:
            return _finish(*verdict)

    # Pollinations-style anonymous tiers need no key at all — the ORIGINAL test
    # required one unconditionally, so a genuinely-working keyless provider always
    # read "No API key saved", the same kind of lie this rewrite exists to remove.
    if not key and _needs_key(pid):
        return _finish(False, "No API key saved for this provider.", [], cache=False)
    models_url = _models_url_for(pid, pcfg)
    sample_models = []
    models_list_note = None
    listing_failed = False   # multi-key only: the PRIMARY key could not list
    if models_url and key:  # an anonymous tier has nothing to authenticate the GET with
        try:
            resp = requests.get(models_url, headers={"Authorization": "Bearer " + key},
                                timeout=(CONNECT_TIMEOUT, MODELS_READ_TIMEOUT))
        except requests.RequestException as exc:
            return _finish(False, _sanitize("%s: %s" % (exc.__class__.__name__, exc)), [])
        if resp.status_code == 200:
            try:
                all_ids = _parse_model_ids(resp.json())
            except ValueError:
                all_ids = []
            # FREE-filter before this goes anywhere near a user: the raw catalog
            # includes every PAID model too (a live bug — a user testing openrouter
            # saw "claude", "opus", "fable 5" listed as if recognized/free, when
            # they were just unfiltered paid entries from the same /models response
            # provider_free_models() already filters correctly elsewhere; this path
            # never did). "% listed" and sample_models must both reflect only what
            # is_free_model() actually confirms is free for this provider.
            sample_models = prov.filter_models(
                [m for m in all_ids if prov.is_free_model(pid, m)])
            models_list_note = "%d free models listed (%d total in catalog)" % (
                len(sample_models), len(all_ids))
        elif len(pcfg.get("api_keys") or []) > 1:
            # MULTI-KEY: this GET only ever used the PRIMARY key, so returning
            # here declared the whole provider dead on the strength of one key
            # and never tried the others -- the exact case that makes "which of
            # my keys is broken?" unanswerable. Fall through to the per-key
            # generation loop instead; candidates come from the registry pins.
            # Deliberately NOT models_list_note: that one reads "key
            # authenticates and lists models", which would be a flat lie here.
            listing_failed = True
        else:
            # Single key: can't even list models -> that key is bad. No point
            # spending a generation attempt to learn the same thing twice.
            return _finish(False, "HTTP %d: %s" % (resp.status_code, _upstream_error_detail(resp)), [])

    # ALWAYS verify with a REAL generation — see docstring. Try every registry-
    # pinned default (not just the first): a provider often lists several free
    # ids, and one going stale (a rotated-out ':free' slug, a renamed model)
    # must not paint the whole provider red when its siblings still work —
    # exactly what happened testing this rewrite: openrouter's FIRST pinned id
    # 404'd "unavailable for free, use this slug instead: ..." while its other
    # 6 pinned ids were fine. /models results are tried too, deduped, in order.
    candidates, seen = [], set()
    for m in (p.get("default_free_models") or []) + sample_models:
        if m and m not in seen and prov.is_model_allowed(m):
            seen.add(m)
            candidates.append(m)
    # A provider with NO free models still has a key worth testing. Puter is the
    # live case: it is metered (~25c/month per account), so it deliberately
    # declares zero free models — which left Test with nothing to probe and made
    # it report "Key authenticates (0 free models listed) but no allowed model to
    # verify generation with" for a key that works perfectly. The question Test
    # answers is "does this key work", not "is this model free", so fall back to
    # the provider's own catalog. Capped at a few ids: on a metered provider each
    # attempt spends real allowance.
    metered_probe = False
    if not candidates:
        for m in (_provider_paid_models(pid) or [])[:3]:
            if m and m not in seen and prov.is_model_allowed(m):
                seen.add(m)
                candidates.append(m)
        metered_probe = bool(candidates)
    if not candidates:
        if models_list_note:
            return _finish(False, "Key authenticates (%s) but no allowed model to verify "
                                  "generation with." % models_list_note, sample_models[:5])
        return _finish(False, "Provider has no models_url and no default model to test with.", [])

    # Transient statuses (a live rate-limit blip, momentary overload) get ONE
    # retry after a short pause before moving to the next candidate — without
    # this, testing this very rewrite painted glm/huggingface red on a passing
    # 429/503 that a re-click a second later cleared on its own. 402/403/404 are
    # NOT retried: those mean "will never work", not "try again".
    _TRANSIENT = (429, 500, 502, 503, 504)

    def _probe(key_pin):
        """Real 1-token generation across the candidate models, optionally
        PINNED to one key. Returns (ok, model_that_worked, failure_reason)."""
        reason = None
        for model in candidates[:5]:  # cap attempts — this call is user-interactive
            resp = None
            for attempt in range(2):
                # Pass only_key ONLY when actually pinning, so the unpinned path
                # keeps the exact call shape it always had (a test double, or any
                # other stand-in for _upstream_chat, should not have to know about
                # a parameter this path never uses).
                pin = {} if key_pin is _NO_KEY_PIN else {"only_key": key_pin}
                try:
                    resp = _upstream_chat(pid, {"model": model,
                                                "messages": [{"role": "user", "content": "hi"}],
                                                "max_tokens": 16},  # 16 = Perplexity's floor
                                          stream=False, **pin)
                except (requests.RequestException, RuntimeError) as exc:
                    reason = _sanitize("%s: %s" % (exc.__class__.__name__, exc))
                    resp = None
                    break
                if resp.status_code == 200:
                    attempted.append((model, True))
                    return True, model, None
                if resp.status_code in _TRANSIENT and attempt == 0:
                    time.sleep(2)
                    continue
                reason = "HTTP %d: %s" % (resp.status_code, _upstream_error_detail(resp))
                break
            attempted.append((model, False))  # candidate didn't pan out — try the next
            if resp is not None and resp.status_code == 401:
                # 401 is about the CREDENTIAL, not the model, so no sibling
                # model can rescue it -- walking the rest of the candidates just
                # spends four more requests to be told the same thing. Matters
                # more now that a pool is tested key-by-key: 3 dead keys x 5
                # candidates was 15 pointless calls per click.
                break
        return False, None, reason

    def _verdict(ok, model, reason):
        """Same wording the single-key path has always produced."""
        if ok:
            # Don't claim "FREE" when the probe ran on a metered catalog id —
            # that would be the one thing a user reading this most needs to
            # be true.
            return (("Key OK — verified generation works (1-token chat "
                     "succeeded on %s). This provider has no free tier, "
                     "so the probe spent a little of its allowance."
                     if metered_probe else
                     "Key OK — verified FREE generation works "
                     "(1-token chat succeeded on %s).") % model)
        # Every candidate authenticated but none could actually generate — the
        # spent-wallet case this whole rewrite exists to catch. Plain language,
        # not a bare HTTP status, so the verdict answers "will this work".
        if models_list_note:
            return ("Key authenticates and lists models (%s), but generation FAILS on "
                    "every candidate tried — this will NOT work for free usage: %s"
                    % (models_list_note, reason))
        if listing_failed:
            # Never claim it "authenticates" here: the listing call is exactly
            # what it failed.
            return "This key is rejected by the provider: %s" % (reason or "generation failed")
        return reason or "generation failed"

    pool = list(pcfg.get("api_keys") or [])
    # ONE key (or none): leave the pool logic exactly as it was, so a keyless
    # provider's static_key/no-auth pass is untouched.
    if len(pool) <= 1:
        ok, model, reason = _probe(_NO_KEY_PIN)
        payload_extra = ([{"index": 0, "masked": _mask_key(pool[0]), "ok": ok,
                           "detail": _verdict(ok, model, reason)}] if pool else [])
        return _finish(ok, _verdict(ok, model, reason),
                       (sample_models[:5] or ([model] if model else [])),
                       keys=payload_extra)

    # SEVERAL keys: test each one SEPARATELY. Without this the pool rotates, so
    # one good key makes the provider look healthy while a dead one beside it
    # keeps burning a routing hop on every request -- and nothing in the UI
    # could tell you which was which.
    per_key, first_ok_model = [], None
    for i, k in enumerate(pool):
        k_ok, k_model, k_reason = _probe(k)
        per_key.append({"index": i, "masked": _mask_key(k), "ok": k_ok,
                        "detail": _verdict(k_ok, k_model, k_reason)})
        if k_ok and first_ok_model is None:
            first_ok_model = k_model
    good = [r["index"] + 1 for r in per_key if r["ok"]]
    bad = [r["index"] + 1 for r in per_key if not r["ok"]]
    if good and bad:
        detail = ("%d of %d keys work. Working: #%s. NOT working: #%s — "
                  "each dead key still costs a wasted attempt when routing "
                  "rotates onto it, so remove them."
                  % (len(good), len(pool), ", #".join(map(str, good)),
                     ", #".join(map(str, bad))))
    elif good:
        detail = "All %d keys work." % len(pool)
    else:
        detail = ("None of the %d keys work. %s"
                  % (len(pool), per_key[0]["detail"] if per_key else ""))
    return _finish(bool(good), detail,
                   (sample_models[:5] or ([first_ok_model] if first_ok_model else [])),
                   keys=per_key)


@app.route("/api/test-cache", methods=["GET"])
def api_test_cache():
    """Persisted results from every /api/test/<pid> call ever made, so the
    dashboard can hydrate instantly on load instead of re-testing (spending a
    real generation request) on every single page visit. {pid: {ok, detail,
    sample_models, tested_at, ...}}. Read-only, makes no upstream calls."""
    with _test_cache_lock:
        return jsonify(_load_test_cache())


@app.route("/api/aa-benchmarks", methods=["GET", "POST"])
def api_aa_benchmarks():
    """GET -> status of the Artificial Analysis benchmark integration (has_key,
    how many models are currently scored from real data, when it last
    refreshed successfully). Never returns the key itself.
    POST {api_key: "..."} -> save a new key (or {api_key: null} to clear one)
    and kick off an immediate refresh in the background so the dashboard
    doesn't have to wait out the normal 6h interval to see it take effect."""
    if request.method == "POST":
        body = request.get_json(force=True, silent=True) or {}
        if "api_key" in body:
            val = body.get("api_key")
            config.set_aa_api_key(val.strip() if isinstance(val, str) and val.strip() else None)
            threading.Thread(target=_aa_refresh_once, daemon=True).start()
    return jsonify({
        "has_key": bool(config.get_aa_api_key()),
        "score_count": len(_aa_scores),
        "last_refresh": _aa_last_refresh or None,
        "refresh_interval_hours": AA_REFRESH_INTERVAL / 3600,
    })


@app.route("/api/models", methods=["GET"])
def api_models():
    return jsonify(aggregated_models())


@app.route("/api/model-categories", methods=["GET", "POST"])
def api_model_categories():
    """What each model is good at, and a one-click way to use only those.

    GET  -> {"categories":[{"key","label","help","count","ids":[...]}], "blocked":[...]}
    POST {"key": "swarm"}  -> switch ON every model in that category and OFF
                              everything else, then return the same shape.
    POST {"key": "all"}    -> switch everything back on.

    The category table lives in model_categories.py precisely so this route and
    the router cannot disagree about what a category means. The switch itself
    reuses the per-model allowlist, which routing already honours at one seam --
    so choosing a category takes effect in orchestration, the fallback chain and
    the swarm at once, and "always the best, else the next best" still comes for
    free from the ordinary ranking inside whatever set is enabled."""
    live = []
    for pid in _available_providers():
        try:
            for m in provider_free_models(pid) or []:
                live.append((pid, m, "%s/%s" % (pid, m),
                             _normalize_model_identity(m)))
        except Exception:                                        # noqa: BLE001
            continue

    if request.method == "POST":
        body = request.get_json(force=True, silent=True) or {}
        key = str(body.get("key") or "").strip()
        if key == "all":
            config.set_setting(_BLOCKED_SETTING, [])
        elif key in model_categories.CATEGORY_KEYS:
            keep = {mid for _p, _m, mid, ident in live
                    if model_categories.matches(key, _p, _m, ident)}
            if not keep:
                return jsonify({"error": "No available model is in that category "
                                         "right now."}), 400
            config.set_setting(_BLOCKED_SETTING,
                               sorted({mid for _p, _m, mid, _i in live} - keep))
        else:
            return jsonify({"error": "Unknown category."}), 400

    blocked = _blocked_models()
    out = []
    for key, label, helptext in model_categories.labels():
        ids = sorted(mid for _p, _m, mid, ident in live
                     if model_categories.matches(key, _p, _m, ident))
        out.append({"key": key, "label": label, "help": helptext,
                    "count": len(ids), "ids": ids,
                    "enabled": sum(1 for i in ids if i not in blocked)})
    return jsonify({"categories": out, "blocked": sorted(blocked),
                    "total": len(live)})


@app.route("/api/model-allowlist", methods=["GET", "POST"])
def api_model_allowlist():
    """Which models the user has switched off.

    GET  -> {"blocked": ["pid/model", ...]}
    POST {"provider","model","enabled":bool} -> the same, after the change.

    The benchmark figures the Settings table shows next to these come from
    /api/tracking, which already returns every model with its score and state --
    no second scorer, so the table and the router can never disagree."""
    # No explicit gate: the global before_request control-token check already
    # covers every /api/* route, which is what POST /api/providers/<pid> relies
    # on too.
    if request.method == "GET":
        return jsonify({"blocked": sorted(_blocked_models())})
    body = request.get_json(force=True, silent=True) or {}
    pid = str(body.get("provider") or "").strip()
    model = str(body.get("model") or "").strip()
    if not pid or not model:
        return jsonify({"error": "provider and model are required."}), 400
    blocked = _set_model_blocked(pid, model, not bool(body.get("enabled", True)))
    return jsonify({"blocked": sorted(blocked)})


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
    # Enabled+keyed providers that surfaced NO free model — not routed in Free mode
    # because they're paid/trial-credit (no free tier). Surfaced so they're never
    # silently 'excluded': the user sees them and why (switch Auto-uses to Mix/Paid
    # to actually use them with their key).
    shown = {r["provider"] for r in out}
    keyed_no_free = sorted(pid for pid in _enabled_keyed() if pid not in shown)
    return jsonify({"models": out, "total": len(out), "by_state": by_state,
                    "providers": sorted({r["provider"] for r in out}),
                    "usable": sum(1 for r in out if r["state"] == "ok"),
                    "keyed_no_free": keyed_no_free})


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
    payload = {"model": model, "max_tokens": 16, "stream": False,  # 16 = Perplexity's floor
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
    blocked = _model_block_reason(provider, model)
    if blocked:
        return jsonify({"error": blocked}), 403
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
    # Advertise 'auto' — the SAME value the Claude auto-fixer writes, so the shown
    # snippet and the written env block never disagree. It must NOT be a concrete
    # '<pid>/<model>': _is_orchestrate() bails on any id containing '/', which pins
    # every Claude Code request to one provider and skips difficulty/vision routing
    # (that pin is how a single model ended up serving an entire project).
    claude = ("export ANTHROPIC_BASE_URL=http://localhost:%d\n"
              "export ANTHROPIC_AUTH_TOKEN=%s\n"
              "export ANTHROPIC_MODEL=auto\n"
              "claude" % (PORT, shown_key))
    openai = ("export OPENAI_BASE_URL=http://localhost:%d/v1\n"
              "export OPENAI_API_KEY=%s" % (PORT, shown_key))
    return {"claude_code": claude, "openai": openai}


# The RELEASE name, set by hand. _detect_hub_version() below is the git HEAD,
# which changes on every commit and is what the "what's new" popup compares --
# useful for that, useless as something to tell someone you are running.
HUB_RELEASE = "LLM Calvoun V2.8"


def _detect_hub_version():
    """Running-version stamp for the dashboard's "what's new" popup: the short
    git HEAD of this checkout, resolved once at startup (an update = a git
    pull = a new commit = a new version string). Falls back to a stable
    constant when the repo or git is unavailable."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        version = (out.stdout or "").strip()
        if out.returncode == 0 and version:
            return version
    except Exception:
        pass
    return "unknown"


_HUB_VERSION = _detect_hub_version()


@app.route("/api/version", methods=["GET"])
def api_version():
    # Control-token gated like every other /api/* read; the dashboard always
    # holds the token, so no _CONTROL_TOKEN_EXEMPT_GETS entry is needed.
    #
    # ...except that Ollama clients probe this exact path to decide whether they
    # are talking to an Ollama server, and they cannot send the token. While the
    # emulation is on the gate lets them through, so the caller is identified by
    # what it presented: the dashboard always holds a valid token, an Ollama
    # client never does. Both then get the payload they can actually parse --
    # they disagree about what `version` means, so there is no single answer.
    if _ollama_enabled() and not _has_control_token():
        return jsonify({"version": wire_ollama.OLLAMA_VERSION})
    return jsonify({"version": _HUB_VERSION, "release": HUB_RELEASE})


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
        # So the dashboard can state how keys are stored rather than leaving it
        # to be assumed either way.
        "keys_encrypted": config.secrets_encrypted(),
        "outbound_proxy": (config.get_value(_PROXY_KEY, "") or "") or None,
        "encryption_available": secretstore.available(),
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
# Hub lifecycle extras: stopped-state query + desktop relaunch shortcut
# ---------------------------------------------------------------------------

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@app.route("/api/hub/stopped", methods=["GET"])
def api_hub_stopped():
    """Whether the hub was intentionally stopped from the dashboard. Sticky:
    stays true until an explicit user relaunch (desktop shortcut, plain
    run.bat, or `python app.py`) clears the intentional-stop flag."""
    return jsonify({"stopped": config.is_intentionally_stopped()})


def _desktop_dir():
    """Current user's Desktop, OneDrive-redirect aware: ask Windows first,
    fall back to ~/Desktop."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "[Environment]::GetFolderPath('Desktop')"],
            capture_output=True, text=True, timeout=10,
            creationflags=_CREATE_NO_WINDOW)
        path = (out.stdout or "").strip()
        if out.returncode == 0 and path and os.path.isdir(path):
            return path
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Desktop")


def _run_hidden_powershell(command, timeout=20):
    """Run a PowerShell command with no window; raises on non-zero exit."""
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
         "Bypass", "-Command", command],
        capture_output=True, text=True, timeout=timeout,
        creationflags=_CREATE_NO_WINDOW, check=True)


def _create_desktop_shortcut():
    """Create a Desktop shortcut that relaunches the hub HIDDEN.

    Prefers a real .lnk via the WScript.Shell COM object; falls back to a
    small .bat. Both point at run-hidden.vbs WITHOUT the 'supervised'
    argument, so run.bat clears the intentional-stop flag before starting —
    an explicit click always revives a dashboard-stopped hub.
    Returns the created file path."""
    desktop = _desktop_dir()
    os.makedirs(desktop, exist_ok=True)
    here = os.path.dirname(os.path.abspath(__file__))
    vbs = os.path.join(here, "run-hidden.vbs")
    lnk = os.path.join(desktop, "Calvoun Free LLM Hub.lnk")
    icon = os.path.join(here, "static", "calvoun.ico")
    # IconLocation is a separate CreateShortcut property, never inferred from
    # TargetPath -- without it, the shortcut just shows wscript.exe's generic
    # scroll icon, indistinguishable from any other .vbs shortcut on the
    # desktop. Only set it when the file is actually there (a shallow/partial
    # checkout missing static/ should still produce A shortcut, just with
    # Windows' default icon, not none at all).
    icon_line = ("$sc.IconLocation = '%s,0'; " % icon.replace("'", "''")
                 if os.path.isfile(icon) else "")
    # icon_line is inserted via its OWN %s slot (not string-concatenated
    # in) so it stays inside the single format() call below -- % binds
    # tighter than +, so a bare `"..." + icon_line + "..." % tuple(...)`
    # would only format the LAST literal and leave the earlier %s's (lnk/
    # vbs/here) untouched, raising TypeError on every call.
    ps = ("$ws = New-Object -ComObject WScript.Shell; "
          "$sc = $ws.CreateShortcut('{lnk}'); "
          "$sc.TargetPath = 'wscript.exe'; "
          "$sc.Arguments = '\"{vbs}\"'; "
          "$sc.WorkingDirectory = '{here}'; "
          "$sc.WindowStyle = 7; "
          "{icon_line}"
          "$sc.Description = 'Relaunch the Calvoun Free LLM Hub (hidden, no console window)'; "
          "$sc.Save()").format(
              lnk=lnk.replace("'", "''"), vbs=vbs.replace("'", "''"),
              here=here.replace("'", "''"), icon_line=icon_line)
    try:
        _run_hidden_powershell(ps)
        if os.path.isfile(lnk):
            return lnk
    except Exception as exc:
        _log.warning("Desktop .lnk creation failed, falling back to .bat: %s",
                     _sanitize(str(exc)))
    bat = os.path.join(desktop, "Calvoun Free LLM Hub.bat")
    with open(bat, "w", encoding="utf-8", newline="") as f:
        f.write("@echo off\r\nrem Relaunch the Calvoun Free LLM Hub, hidden.\r\n")
        f.write('wscript.exe "%s"\r\n' % vbs)
    return bat


_DESKTOP_SHORTCUT_MARKER_NAME = "desktop-shortcut-auto-created"


def _maybe_auto_create_desktop_shortcut():
    """First SUCCESSFUL boot: create the desktop shortcut automatically,
    same "once, marker only on success" contract as run.bat's own
    maybe_autopersist -- a user who never finds the Stop-hub modal's
    shortcut checkbox still gets a one-click way back in, and a transient
    failure (Desktop dir not ready, PowerShell hiccup) retries on the next
    boot instead of being silently given up on forever. Windows only --
    .lnk/.ico/wscript.exe are meaningless on run.sh platforms. Gated on a
    marker file, not the shortcut's mere existence, so a user who deletes
    the shortcut on purpose does not get it silently recreated under them."""
    if os.name != "nt":
        return
    marker = os.path.join(config.state_dir(), _DESKTOP_SHORTCUT_MARKER_NAME)
    if os.path.exists(marker):
        return
    try:
        _create_desktop_shortcut()
    except Exception as exc:
        _log.warning("Auto desktop-shortcut creation failed, will retry next boot: %s",
                     _sanitize(str(exc)))
        return
    try:
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as f:
            f.write("created automatically on first successful boot -- delete this "
                    "file to let the hub try again, or just create your own shortcut\n")
    except OSError:
        pass


@app.route("/api/hub/desktop-shortcut", methods=["POST"])
def api_hub_desktop_shortcut():
    try:
        path = _create_desktop_shortcut()
    except OSError as exc:
        return jsonify({"ok": False, "error": _sanitize(str(exc))}), 500
    return jsonify({"ok": True, "path": path})


# ---------------------------------------------------------------------------
# MCP server endpoint + per-CLI MCP server management
# ---------------------------------------------------------------------------
# POST /mcp speaks JSON-RPC 2.0 (tools crew_run / crew_start / crew_result) so
# any MCP-capable agent CLI can call the hub's crews as native tools. It is NOT
# control-token gated — CLI agents on localhost must reach it like /v1/* — but
# the loopback Host/Origin guard in _local_control_guard applies to every path,
# so it is exposed exactly like the rest of the hub. The /api/mcp* routes manage
# MCP server entries in the kimi/codex/claude/opencode configs; they live under
# /api/ so the dashboard header + control token gate them like every other
# control-plane route.


def _hub_mcp_url():
    return "http://127.0.0.1:%d/mcp" % PORT


@app.route("/mcp", methods=["POST"])
def mcp_rpc():
    body = request.get_json(force=True, silent=True)
    result, status = hub_mcp.handle_rpc(body)
    if result is None:
        return "", 204   # JSON-RPC notification — no response body
    # The client MAY send Accept: text/event-stream (MCP streamable HTTP), but
    # a single JSON-RPC response is still legal as plain application/json.
    return jsonify(result), status


@app.route("/api/mcp", methods=["GET"])
def api_mcp_list():
    payload = mcp_manager.list_servers()
    payload["hub_mcp"] = {"url": _hub_mcp_url(), "name": "free-llm-hub"}
    return jsonify(payload)


@app.route("/api/mcp", methods=["POST"])
def api_mcp_add():
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "message": "invalid JSON body"}), 400
    ok, msg = mcp_manager.add_server(body.get("cli"), body.get("name"),
                                     body.get("spec"))
    return jsonify({"ok": ok, "message": msg}), 200 if ok else 400


@app.route("/api/mcp/delete", methods=["POST"])
def api_mcp_delete():
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "message": "invalid JSON body"}), 400
    ok, msg = mcp_manager.remove_server(body.get("cli"), body.get("name"))
    return jsonify({"ok": ok, "message": msg}), 200 if ok else 400


@app.route("/api/mcp/install-hub", methods=["POST"])
def api_mcp_install_hub():
    """One-click 'enable hub crews in this CLI': register the hub's own MCP
    endpoint under the well-known name 'free-llm-hub'."""
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "message": "invalid JSON body"}), 400
    cli = body.get("cli")
    spec = {"url": _hub_mcp_url()}

    def _install(isolated):
        ok, msg = mcp_manager.add_server(cli, "free-llm-hub", spec,
                                         isolated=isolated)
        if not ok and str(msg).lower() == "exists":
            # Already registered — retry once with force:true (consumed by the
            # manager, never stored) so the button also REPAIRS a stale/wrong
            # entry instead of failing.
            ok, msg = mcp_manager.add_server(cli, "free-llm-hub",
                                             dict(spec, force=True),
                                             isolated=isolated)
        return ok, msg

    ok, msg = _install(False)
    # ALSO register in the hub's OWN isolated copy of the CLI. agentic_chat.py
    # runs every /agent session with CODEX_HOME / CLAUDE_CONFIG_DIR /
    # XDG_CONFIG_HOME pointed at ~/.free-llm-hub/isolated-clis/<cli>/config, so
    # a global-only entry is invisible to the hub's own agent chat -- which is
    # exactly where the crew tools matter most. Best-effort: the isolated copy
    # may not be installed, and that must never fail the global install.
    iso_ok, iso_msg = _install(True)
    if ok and iso_ok:
        msg = "%s (and the hub's own isolated copy)" % msg
    elif ok:
        msg = "%s — isolated copy skipped: %s" % (msg, iso_msg)
    return jsonify({"ok": ok, "message": msg, "isolated_ok": iso_ok}), 200 if ok else 400


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
        models = _sub_models(pid)
        row = {
            "id": pid,
            "name": cfg["name"],
            "model": pid + "/" + (models[0] if models else "cli"),   # primary, for legacy display
            "models": [pid + "/" + m for m in models],   # every addressable model, for a multi-model relay
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
            "recommended": bool(cfg.get("recommended")),
        }
        rows.append(row)
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
    ok, result = _install_isolated_cli(cfg["cli_id"], cfg["bin"])
    if not ok:
        return jsonify(result), result.pop("_status", 502)
    return jsonify(result)


_AGENT_AUTOINSTALL_FLAG = "agent_cli_autoinstall"      # config flag, default ON


def _agent_cli_autoinstall_once():
    """Install the agent CLIs into the hub's OWN isolated namespace if they are
    not there yet, in the background, at boot.

    WHY AT ALL: the agent chat is unusable without one, and asking someone to go
    install a CLI by hand is exactly the friction this hub exists to remove.

    WHY ISOLATED ONLY: this never touches a global install and never upgrades
    anything the user already has. If a CLI is already resolvable — isolated or
    on PATH — it is left completely alone. So the only case where this does
    anything is "the hub needs a CLI and there is none", which is the case where
    doing nothing leaves a broken feature.

    Opt-out via the config flag, and it is one attempt per boot: a machine with
    no npm, or offline, must not retry in a loop."""
    if not config.get_flag(_AGENT_AUTOINSTALL_FLAG, True):
        return
    npm = _ensure_npm()
    if not npm:
        _log.info("[agent-cli] no npm and Node could not be installed — skipping")
        return
    for cli_id, bin_name in (("codex", "codex"), ("opencode", "opencode"),
                             ("claude", "claude")):
        try:
            # 1. ON THE MACHINE. The isolated copy only ever serves the hub's
            #    own agent chat; it is not on PATH, so the user's terminal
            #    still has no `codex` and no `opencode`. Asked for explicitly:
            #    install them properly too, not just in the private namespace.
            #    npm's global prefix is per-user on Windows (%APPDATA%\npm) and
            #    on any sane nvm/fnm/volta setup, so this needs no privileges.
            #    Where it does (a root-owned /usr/lib/node_modules), npm fails
            #    with EACCES, we log it, and the isolated copy below still
            #    makes the feature work.
            if not shutil.which(bin_name):
                _log.info("[agent-cli] installing %s on this machine", cli_id)
                ok, res = _install_global_cli(cli_id, npm=npm)
                _log.info("[agent-cli] %s (global): %s", cli_id,
                          "installed" if ok else _sanitize(str(res.get("error"))))
            # 2. FOR THE HUB. Isolated so a version the hub drives can never
            #    disturb the one the user's own terminal depends on.
            #
            #    This used to check _resolve_bin(), which answers "is there a
            #    binary I could run" -- and it resolves the GLOBAL install too.
            #    So on any machine that already had claude or opencode, the
            #    isolated copy was never built, and every agent session ran the
            #    user's own install against the user's own credentials. Exactly
            #    what isolation is for. Check for the ISOLATED copy specifically.
            if agentic_chat._isolated_bin(cli_id):
                _ensure_hyperframes_skill(cli_id)
                continue                       # already isolated; leave it alone
            _log.info("[agent-cli] installing %s into the hub's isolated namespace", cli_id)
            ok, res = _install_isolated_cli(cli_id, bin_name)
            _log.info("[agent-cli] %s: %s", cli_id,
                      "installed" if ok else _sanitize(str(res.get("error"))))
            if ok:
                _ensure_hyperframes_skill(cli_id)
        except Exception:                                        # noqa: BLE001
            _log.debug("[agent-cli] auto-install failed for %s", cli_id, exc_info=True)


_HYPERFRAMES_SKILL_REPO = "heygen-com/hyperframes"
_HYPERFRAMES_INSTALL_TIMEOUT = 300


def _ensure_hyperframes_skill(cli_id):
    """Best-effort, idempotent: give claude/codex's isolated copy the
    hyperframes-animation (claude) / gsap (codex) skill so agentic web builds
    get real GSAP/Three.js/CSS-keyframe technique reference by default (user
    2026-08-05: "give him the hyperframe skills... use it by default in all
    web page generation"). opencode is skipped -- not in hyperframes' own
    agent list and its isolated config has no skills/ convention at all.

    Claude Code's own package cleanly names and installs a single
    "hyperframes-animation" skill. Codex's package is a "plugin" bundling
    several skills (video composition, CLI, registry, GSAP) with no clean
    animation-only equivalent -- MEASURED 2026-08-05: `npx skills add`
    installed it into `<CODEX_HOME>/.tmp/plugins/...` without ever
    registering it in codex's own flat skills/<name>/ convention, and the
    closest clean fit is the bundle's own "gsap" skill (a pure technique
    reference, not video-composition-flavored) -- so codex gets that one
    specifically, promoted into place by hand since the installer never
    finishes the job for it on its own.

    Never raises, never blocks CLI availability: a failure here just leaves
    the skill absent, exactly as if this had never been attempted."""
    if cli_id not in ("claude", "codex"):
        return
    config_dir = _isolated_config_dir(cli_id)
    marker = os.path.join(config_dir, "skills",
                          "hyperframes-animation" if cli_id == "claude" else "gsap")
    if os.path.isdir(marker):
        return                              # already installed
    npx = shutil.which("npx")
    if not npx:
        return
    _ensure_isolated_dirs(cli_id)
    env = dict(os.environ)
    env[{"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}[cli_id]] = config_dir
    argv = _sub_launcher(npx) + ["--yes", "skills", "add", _HYPERFRAMES_SKILL_REPO,
                                 "--full-depth", "-s", "hyperframes-animation", "-y", "-g"]
    try:
        subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env, timeout=_HYPERFRAMES_INSTALL_TIMEOUT)
    except Exception:                                            # noqa: BLE001
        return
    if cli_id == "codex":
        # The installer stages the whole plugin bundle under .tmp and never
        # finishes registering it for codex -- finish it: promote just the
        # gsap skill to codex's real flat skills/<name>/ layout (matches the
        # .system skills already living there) and drop the staging clone.
        staged = os.path.join(config_dir, ".tmp", "plugins", "plugins",
                              "hyperframes", "skills", "gsap")
        target = os.path.join(config_dir, "skills", "gsap")
        try:
            if os.path.isdir(staged) and not os.path.isdir(target):
                shutil.copytree(staged, target)
            tmp_plugins = os.path.join(config_dir, ".tmp", "plugins")
            if os.path.isdir(tmp_plugins):
                shutil.rmtree(tmp_plugins, ignore_errors=True)
        except Exception:                                        # noqa: BLE001
            pass


def _install_global_cli(cli_id, npm=None):
    """`npm install -g <pkg>` into the machine's own global prefix.

    The counterpart to _install_isolated_cli: that one gives the HUB a private
    copy, this one gives the USER a `codex` / `opencode` they can type in their
    own terminal. Both are wanted -- the private copy keeps the hub's driving
    from disturbing the user's setup, and the global one is what makes the tool
    actually present on the machine."""
    pkg = _ISOLATED_NPM_PACKAGE.get(cli_id)
    if not pkg:
        return False, {"ok": False, "error": "No known npm package for '%s'." % cli_id}
    npm = npm or _ensure_npm()
    if not npm:
        return False, {"ok": False, "error": "npm is not available and Node could not be installed."}
    argv = _sub_launcher(npm) + ["install", "-g", pkg]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=_ISOLATED_INSTALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, {"ok": False, "error": "npm install timed out after %ds."
                                             % _ISOLATED_INSTALL_TIMEOUT}
    except Exception as exc:                                     # noqa: BLE001
        return False, {"ok": False, "error": "could not run npm: %s" % exc}
    if proc.returncode != 0:
        tail = _sanitize((proc.stderr or proc.stdout or "").strip()[-400:], 400)
        return False, {"ok": False, "error": "npm install -g %s failed: %s" % (pkg, tail)}
    return True, {"ok": True, "package": pkg, "bin": shutil.which(_CLI_BIN_NAME.get(cli_id, cli_id))}


_CLI_BIN_NAME = {"claude": "claude", "codex": "codex", "opencode": "opencode"}


_agent_autoinstall_thread = None


# MCP servers every agent should always have, registered at boot.
#
#   free-llm-hub  the hub's own crews (crew_run / crew_start / crew_result),
#                 so "use the crew agents" is a real tool in any CLI.
#   context7      live library documentation + repository lookup, so an agent
#                 works from CURRENT docs instead of its training cutoff.
#
# USER 2026-08-08: "give the local hub the mcp context7 ALWAYS so he can always
# get last documentations and also repositories". Doing this only once by hand
# would drift: a CLI installed later, a reset config, or a fresh machine would
# silently lose it. Running it at every boot keeps it true instead.
#   playwright    a real browser, so an agent can OPEN what it just built and
#                 check it instead of declaring it done unseen.
#
# playwright is the odd one out and worth knowing about: it is STDIO, not a
# URL. Each agent spawns its own `npx @playwright/mcp@latest` subprocess, which
# needs Node on PATH and downloads browser binaries on first use. That makes it
# heavier than the two HTTP servers -- but "verify the page you generated"
# is exactly the step agents skip, so it earns the cost.
_HUB_URL_SENTINEL = "<hub>"     # replaced with _hub_mcp_url() at call time

_ALWAYS_MCP = (
    ("free-llm-hub", {"url": _HUB_URL_SENTINEL}),
    ("context7", {"url": "https://mcp.context7.com/mcp"}),
    ("playwright", {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]}),
)


def _ensure_mcp_servers_once():
    """Register the always-on MCP servers in every supported CLI, and in the
    hub's own isolated copies. Idempotent (an existing entry reports 'exists'
    and is left alone) and entirely best-effort -- a CLI that is not installed,
    or a config we refuse to touch, must never affect hub startup."""
    for name, spec in _ALWAYS_MCP:
        spec = dict(spec)
        # A stdio server is a command we are asking every CLI to SPAWN. If it
        # is not on PATH, registering it anyway writes an entry into six
        # configs that then silently fails to start in each one -- the exact
        # looks-like-success failure this module exists to avoid. Node is
        # normally present because _agent_cli_autoinstall_once installs it for
        # the CLIs themselves, but that can fail (offline, no npm).
        cmd = spec.get("command")
        if cmd and not shutil.which(cmd):
            _log.info("[mcp] skipping %s: %r is not on PATH", name, cmd)
            continue
        # Test the SENTINEL, not `is None`: a stdio spec has no "url" key at
        # all, so `spec.get("url") is None` was also true for playwright and
        # bolted the hub's URL onto it -- registering a browser subprocess as
        # an HTTP server pointing at the hub, in every CLI.
        if spec.get("url") == _HUB_URL_SENTINEL:
            spec["url"] = _hub_mcp_url()
        for cli in mcp_manager.supported_clis():
            for isolated in (False, True):
                try:
                    ok, msg = mcp_manager.add_server(cli, name, dict(spec),
                                                     isolated=isolated)
                except Exception:                                # noqa: BLE001
                    continue
                if ok:
                    _log.info("[mcp] registered %s in %s%s", name, cli,
                              " (isolated)" if isolated else "")
                elif str(msg).lower() not in ("exists",):
                    _log.debug("[mcp] %s/%s%s: %s", name, cli,
                               " (isolated)" if isolated else "", msg)


def _boot_agent_setup():
    """CLI install first, MCP registration second — in that order on purpose:
    an isolated config dir does not exist until its CLI has been installed, so
    registering first would silently skip the copies that matter most."""
    try:
        _agent_cli_autoinstall_once()
    except Exception:                                            # noqa: BLE001
        _log.debug("[agent-cli] auto-install pass failed", exc_info=True)
    try:
        _ensure_mcp_servers_once()
    except Exception:                                            # noqa: BLE001
        _log.debug("[mcp] bootstrap pass failed", exc_info=True)


def _start_agent_cli_autoinstall():
    """Idempotent, daemon, off the request path — an npm install takes tens of
    seconds and must never delay the hub coming up."""
    global _agent_autoinstall_thread
    if _agent_autoinstall_thread is not None:
        return
    _agent_autoinstall_thread = threading.Thread(
        target=_boot_agent_setup, daemon=True)
    _agent_autoinstall_thread.start()


def _install_isolated_cli(cli_id, bin_name):
    """`npm install -g <pkg> --prefix <isolated dir>` for one isolated CLI
    namespace. Returns (ok, result_dict) -- result_dict is always a plain
    JSON-able dict; on failure it also carries "_status" (popped by the
    caller before jsonify) so each failure mode keeps its own HTTP code."""
    pkg = _ISOLATED_NPM_PACKAGE.get(cli_id)
    if not pkg:
        return False, {"ok": False, "error": "No known npm package for '%s'." % cli_id, "_status": 400}
    npm = shutil.which("npm")
    if not npm:
        return False, {"ok": False, "error":
                       "npm is not on PATH. Install Node.js first (nodejs.org), then retry.",
                       "_status": 400}
    _ensure_isolated_dirs(cli_id)
    install_dir = _isolated_install_dir(cli_id)
    argv = _sub_launcher(npm) + ["install", "-g", pkg, "--prefix", install_dir]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=_ISOLATED_INSTALL_TIMEOUT,
                              cwd=tempfile.gettempdir())
    except subprocess.TimeoutExpired:
        return False, {"ok": False, "error": "npm install timed out after %ds."
                                             % _ISOLATED_INSTALL_TIMEOUT, "_status": 504}
    except (OSError, ValueError) as exc:
        return False, {"ok": False, "error": "npm failed to start: %s"
                                             % exc.__class__.__name__, "_status": 502}
    if proc.returncode != 0:
        err = _sanitize(((proc.stderr or "") + "\n" + (proc.stdout or "")).strip(), 2000)
        return False, {"ok": False, "error": "npm install exited %d: %s"
                                             % (proc.returncode, err or "no detail"), "_status": 502}
    bin_path = _isolated_bin_path(cli_id, bin_name)
    if not bin_path:
        return False, {"ok": False, "error":
                       ("npm reported success but no '%s' binary was found under %s."
                        % (bin_name, _short(install_dir))), "_status": 502}
    return True, {"ok": True, "bin_path": _short(bin_path), "install_dir": _short(install_dir)}


@app.route("/api/subscriptions/<pid>/test", methods=["POST"])
def api_subscriptions_test(pid):
    """Run ONE real, minimal generation PER MODEL this sub-* hop exposes, and
    report per-model results -- same "one real call, no false green"
    discipline api_test_provider already applies to regular HTTP providers,
    extended to every model instead of just the first: the hub can fall back
    across all of them (_build_chain iterates every _sub_models() entry), so
    knowing which ones currently work is the actual point, not just whether
    the first one does.

    Skips a model already marked dead (_is_model_dead) -- no point spending a
    real call (and, for AgentRouter, real shared quota) reconfirming a
    failure the hub already knows about; it retries automatically once the
    6h dead-TTL expires.

    _sub_run() is synchronous per call (subprocess.run blocks until the
    child exits or the timeout kills it) -- nothing is left running in the
    background after this returns, by construction, no extra cleanup needed."""
    if pid not in _SUB_PROVIDERS:
        return jsonify({"ok": False, "error": "Unknown subscription provider '%s'."
                                              % _sanitize(str(pid), 40)}), 400
    if not _sub_models(pid):
        return jsonify({"ok": False, "error": "No models registered for this provider."}), 200
    any_ok, results = _run_relay_model_test(pid)
    return jsonify({"ok": any_ok, "results": results})


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
    if not isinstance(body, dict):
        return jsonify({"error": "Pass {\"enabled\": bool} and/or "
                                 "{\"default_cli\": str}."}), 400
    # Picking a CLI in the dashboard saves it here, on the click. There is no
    # Save button to forget: a picker that quietly reverts on the next reload
    # is worse than one that never remembered.
    if "default_cli" in body:
        try:
            agentic_chat.set_default_cli(body["default_cli"])
        except agentic_chat.AgenticError as exc:
            return jsonify({"error": str(exc)}), 400
    elif not isinstance(body.get("enabled"), bool):
        return jsonify({"error": "Pass {\"enabled\": bool} and/or "
                                 "{\"default_cli\": str}."}), 400
    if isinstance(body.get("enabled"), bool):
        agentic_chat.set_master_enabled(body["enabled"])
    return jsonify({"enabled": agentic_chat.master_enabled(), "clis": agentic_chat.cli_support(),
                    "default_cli": agentic_chat.default_cli()})


def _no_candidates_hint():
    """Reserved for extra context on an exhausted-chain 503. Empty today: the
    uncensored-only mode that used to explain itself here is gone."""
    return ""


# Models the USER has switched off, as "pid/model" ids, kept in config.json
# through the generic setting helpers so they survive a restart.
#
# ASKED 2026-08-31: "i want in settings to see benchmark of available models live
# and user can edit wich one to work with". Everything that could take a model out
# of rotation before this was automatic and temporary -- _mark_model_dead on a
# 402/403/404 with a 6h TTL, or providers.is_model_allowed's hardcoded safety
# regex. Nothing let a person say "not this one".
_BLOCKED_SETTING = "blocked_models"


def _blocked_models():
    """The user's off-list, as a set of 'pid/model' ids."""
    try:
        raw = config.get_setting(_BLOCKED_SETTING, []) or []
        return {str(x) for x in raw if x}
    except Exception:                                            # noqa: BLE001
        return set()


def _is_model_blocked_by_user(pid, model):
    return ("%s/%s" % (pid, model)) in _blocked_models()


def _set_model_blocked(pid, model, blocked):
    """Switch one model off or back on. Returns the new blocked set."""
    mid = "%s/%s" % (pid, model)
    cur = _blocked_models()
    cur.add(mid) if blocked else cur.discard(mid)
    try:
        config.set_setting(_BLOCKED_SETTING, sorted(cur))
    except Exception:                                            # noqa: BLE001
        pass
    return cur


def _model_block_reason(pid, model):
    """None when this model may run, else the reason it may not.

    Kept as ONE helper rather than four inline copies of the same check, so a
    future gate has a single place to add its own wording. The removed
    uncensored-only mode is why that matters: it shared this message, so "you
    left a toggle on" reached the user as "Model 'claude' is blocked by the
    safety filter" -- a 403 accusing their own Claude Code subscription of
    being unsafe, with nothing pointing at the setting responsible."""
    if not prov.is_model_allowed(model):
        return "Model '%s' is blocked by the safety filter." % model
    if _is_model_blocked_by_user(pid, model):
        return "Model '%s/%s' is switched off in Settings." % (pid, model)
    return None


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


@app.route("/api/web-search-policy", methods=["GET"])
def api_web_search_policy():
    """Read by the last30days agent skill before using X/social sources.
    GET is exempt from the control token (see _CONTROL_TOKEN_EXEMPT_GETS in
    _local_control_guard) so token-less local agents can read the single
    non-sensitive boolean; POST below keeps full protection."""
    return jsonify({"social_search": config.get_social_web_search()})


@app.route("/api/web-search-policy", methods=["POST"])
def api_web_search_policy_update():
    body = request.get_json(force=True, silent=True)
    value = body.get("social_search") if isinstance(body, dict) else None
    if not isinstance(value, bool):
        return jsonify({"error": "social_search must be a boolean."}), 400
    config.set_social_web_search(value)
    return jsonify({"social_search": config.get_social_web_search()})


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


# --------------------------------------------------------------------------- #
# WORKSPACE — run the project the agent just wrote, next to the chat.
#
# The agent chat could already drive a CLI inside a project directory; what it
# could not do was CLOSE THE LOOP. Files got written and nobody ever ran them.
# These three routes start the project on its own port, report what it prints,
# and turn a crash into something the user can click.
#
# The start command is DERIVED from the directory (a package.json script, a known
# entry point, an index.html) — never taken from the request. There is no field
# here that reaches a shell.
# --------------------------------------------------------------------------- #

def _workspace_dir_from(body):
    """The validated project directory, or (None, error_response)."""
    d = (body or {}).get("project_dir")
    if not isinstance(d, str) or not d.strip():
        return None, (jsonify({"error": "project_dir is required."}), 400)
    d = os.path.abspath(os.path.expanduser(d.strip()))
    if not os.path.isdir(d):
        return None, (jsonify({"error": "not a directory: %s" % _sanitize(d)}), 400)
    return d, None


@app.route("/api/workspace/start", methods=["POST"])
def api_workspace_start():
    gate = _agent_gate()
    if gate:
        return gate
    d, err = _workspace_dir_from(request.get_json(force=True, silent=True))
    if err:
        return err
    try:
        return jsonify(workspace.start(d))
    except workspace.WorkspaceError as exc:
        return jsonify({"error": _sanitize(str(exc)), "code": "not_runnable"}), 400


@app.route("/api/workspace/stop", methods=["POST"])
def api_workspace_stop():
    # DELIBERATELY NOT gated on _agent_gate(), following the rule that gate's own
    # docstring sets for the agent stop/end routes: a kill switch must still be
    # able to kill. Turning the master flag off while a preview server is running
    # must not strand that process with no way to stop it from the UI. Transport
    # auth still applies — every /api route needs the control token.
    d, err = _workspace_dir_from(request.get_json(force=True, silent=True))
    if err:
        return err
    workspace.stop(d)
    return jsonify(workspace.status(d))


@app.route("/api/workspace/attach", methods=["POST"])
def api_workspace_attach():
    """Save a pasted/dropped screenshot into the project so the CLI can READ it.

    The agent chat drives a real CLI, and Claude Code / Codex open images by path
    from the working directory — an inline base64 blob in the prompt would just
    burn context on something they cannot decode."""
    gate = _agent_gate()
    if gate:
        return gate
    body = request.get_json(force=True, silent=True) or {}
    d, err = _workspace_dir_from(body)
    if err:
        return err
    url = body.get("data_url")
    if not isinstance(url, str) or not url.startswith("data:"):
        return jsonify({"error": "data_url must be a data: image URL."}), 400
    m = re.match(r"^data:([^;,]+);base64,(.*)$", url, re.I | re.S)
    if not m:
        return jsonify({"error": "only base64 data URLs are accepted."}), 400
    mime = m.group(1).lower()
    if mime not in _IMAGE_MIMES:
        return jsonify({"error": "unsupported image type '%s'" % _sanitize(mime)}), 400
    try:
        raw = base64.b64decode(re.sub(r"\s+", "", m.group(2)), validate=True)
    except (binascii.Error, ValueError):
        return jsonify({"error": "image data is not valid base64."}), 400
    if len(raw) > MAX_IMAGE_BYTES:
        return jsonify({"error": "image is larger than %d MB."
                        % (MAX_IMAGE_BYTES // (1024 * 1024))}), 400
    try:
        rel = workspace.save_attachment(
            d, raw, mime.split("/")[-1], body.get("name"))
    except (workspace.WorkspaceError, OSError) as exc:
        return jsonify({"error": _sanitize(str(exc))}), 400
    return jsonify({"path": rel, "bytes": len(raw)})


_ARTIFACTS = {}                 # id -> html, newest last
_ARTIFACT_KEEP = 8              # a preview panel, not a store


@app.route("/api/artifact", methods=["POST"])
def api_artifact_put():
    """Stash agent-produced HTML so it can be served as its OWN document.

    It used to go straight into a sandboxed iframe via `srcdoc`, which is safe
    but has one consequence that made the dashboard console unusable: a srcdoc
    iframe INHERITS the embedder's Content-Security-Policy. Ours is
    `default-src 'none'`, so every webfont, stylesheet and image a generated
    page references was refused, and one page produced dozens of CSP errors
    plus hundreds of failed image requests in the parent's console.

    Served from its own URL it is a separate document with its own policy, so a
    generated page renders as the browser would really render it -- which is the
    entire point of previewing it -- while the DASHBOARD keeps its strict CSP."""
    gate = _agent_gate()
    if gate:
        return gate
    body = request.get_json(force=True, silent=True) or {}
    html = body.get("html")
    if not isinstance(html, str) or not html.strip():
        return jsonify({"error": "html is required."}), 400
    if len(html) > 2 * 1024 * 1024:
        return jsonify({"error": "artifact is too large to preview."}), 400
    aid = uuid.uuid4().hex
    _ARTIFACTS[aid] = html
    for old in list(_ARTIFACTS)[:-_ARTIFACT_KEEP]:
        _ARTIFACTS.pop(old, None)
    return jsonify({"id": aid, "url": "/artifact/" + aid})


@app.route("/artifact/<aid>", methods=["GET"])
def serve_artifact(aid):
    """The stashed page, with a policy of ITS own.

    Deliberately permissive about what the page may LOAD (a preview that cannot
    fetch its own fonts is not a preview) and deliberately strict about what it
    may REACH: no framing of anything else, no form submission, and the iframe
    that embeds it is still sandboxed without allow-same-origin, so it has an
    opaque origin and cannot touch the dashboard or its storage."""
    html = _ARTIFACTS.get(aid)
    if html is None:
        return "Not found", 404
    resp = make_response(html)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self' data: blob: https:; "
        "img-src * data: blob:; font-src * data:; "
        "style-src * 'unsafe-inline'; script-src 'unsafe-inline' 'unsafe-eval' https:; "
        "form-action 'none'; frame-ancestors 'self'")
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    return resp


@app.route("/api/workspace/adopt", methods=["POST"])
def api_workspace_adopt():
    """Point the preview at a server the AGENT started.

    The SHIP brief tells the agent to actually run what it builds, so by the time
    the user looks the site is usually already live on its own port — while the
    preview, which only knew about servers it spawned, said "not running" and
    offered a Run button that would have started a SECOND copy elsewhere."""
    gate = _agent_gate()
    if gate:
        return gate
    body = request.get_json(force=True, silent=True) or {}
    d, err = _workspace_dir_from(body)
    if err:
        return err
    try:
        return jsonify(workspace.adopt(d, body.get("url"),
                                       source=body.get("source") or "agent"))
    except workspace.WorkspaceError as exc:
        return jsonify({"error": _sanitize(str(exc))}), 400


# What /api/workspace/raw will serve with a real media type. Everything else is
# handed back as a download, never as something a browser will render: this is
# same-origin, so letting a project's own .html or .svg be rendered here would
# run it inside the dashboard's own origin. X-Content-Type-Options: nosniff is
# set globally, and is what stops a browser second-guessing these.
_RAW_MEDIA_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".ico": "image/x-icon", ".avif": "image/avif",
    ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
    ".m4v": "video/mp4", ".ogv": "video/ogg",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".m4a": "audio/mp4", ".flac": "audio/flac",
}
# SVG is deliberately ABSENT: it is XML that can carry script, and serving it
# inline from this origin would execute that script as the dashboard. It stays
# a text file in the code view, where it is shown as source.
_RAW_MAX_BYTES = 64 * 1024 * 1024


@app.route("/api/workspace/raw", methods=["GET"])
def api_workspace_raw():
    """The RAW BYTES of one project file, for <img>/<video> in the preview.

    /api/workspace/file returns JSON and cannot carry an image: it decodes
    utf-8 and refuses anything with a NUL byte, so a .png came back as
    {"binary": true} and the preview printed "Binary file — not shown."

    Same guards as every other workspace route -- the agent gate, the control
    token (accepted as ?token= so an <img src> can carry it), and
    workspace._resolve_in, which resolves through symlinks and rejects anything
    outside the project. Only known image/video/audio types get a real media
    type; anything else is sent as an attachment so this can never become a way
    to render a project's HTML inside the dashboard's origin."""
    gate = _agent_gate()
    if gate:
        return gate
    d, err = _workspace_dir_from({"project_dir": request.args.get("project_dir")})
    if err:
        return err
    rel = request.args.get("path") or ""
    try:
        # _resolve_in returns (root, target), not just the path.
        _root, target = workspace._resolve_in(d, rel)
    except workspace.WorkspaceError as exc:
        return jsonify({"error": _sanitize(str(exc))}), 400
    if not os.path.isfile(target):
        return jsonify({"error": "not a file"}), 404
    try:
        if os.path.getsize(target) > _RAW_MAX_BYTES:
            return jsonify({"error": "file too large to preview"}), 413
    except OSError as exc:
        return jsonify({"error": _sanitize(str(exc))}), 400
    ext = os.path.splitext(target)[1].lower()
    mime = _RAW_MEDIA_TYPES.get(ext)
    resp = send_file(target, mimetype=mime or "application/octet-stream",
                     as_attachment=not mime,
                     download_name=os.path.basename(target),
                     conditional=True)      # Range requests, so video can seek
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/workspace/tree", methods=["GET"])
def api_workspace_tree():
    """One level of the project, for the file browser beside the preview."""
    gate = _agent_gate()
    if gate:
        return gate
    d, err = _workspace_dir_from({"project_dir": request.args.get("project_dir")})
    if err:
        return err
    try:
        return jsonify(workspace.tree(d, request.args.get("path")))
    except (workspace.WorkspaceError, OSError) as exc:
        return jsonify({"error": _sanitize(str(exc))}), 400


@app.route("/api/workspace/file", methods=["GET"])
def api_workspace_file():
    """A file's text, for the code viewer.

    Confined to the project by workspace._resolve_in, which compares realpaths —
    a plain prefix check is beaten by '..' and by a symlink pointing out of the
    tree, and this decides which of the user's files a browser can read."""
    gate = _agent_gate()
    if gate:
        return gate
    d, err = _workspace_dir_from({"project_dir": request.args.get("project_dir")})
    if err:
        return err
    rel = request.args.get("path")
    if not isinstance(rel, str) or not rel:
        return jsonify({"error": "path is required."}), 400
    try:
        return jsonify(workspace.read_file(d, rel))
    except (workspace.WorkspaceError, OSError) as exc:
        return jsonify({"error": _sanitize(str(exc))}), 400


@app.route("/api/workspace/running", methods=["GET"])
def api_workspace_running():
    """Every live preview server, so the user can see and stop what is using the
    machine. Not gated on the master switch, same reasoning as /stop: you must
    be able to find and kill a process even after turning the feature off."""
    return jsonify({"previews": workspace.running(),
                    "idle_timeout": workspace.IDLE_TIMEOUT})


@app.route("/api/workspace/browse", methods=["GET"])
def api_workspace_browse():
    """Directory listing for the folder picker.

    A browser cannot hand us an absolute local path — not from a drop, not from
    a file input — so browsing has to happen here. Read-only, one level, and
    behind the same gate + control token as the rest: it is still a filesystem
    listing endpoint, which is exactly the kind of thing that should not be
    reachable just because the port is open."""
    gate = _agent_gate()
    if gate:
        return gate
    try:
        return jsonify(workspace.list_dirs(request.args.get("path")))
    except workspace.WorkspaceError as exc:
        return jsonify({"error": _sanitize(str(exc))}), 400
    except OSError as exc:
        return jsonify({"error": _sanitize(str(exc))}), 400


@app.route("/api/workspace/status", methods=["GET"])
def api_workspace_status():
    # Gated, unlike stop: this one only READS, and its 400-on-missing-directory
    # is an existence oracle for arbitrary paths. It gets the same gate as start
    # rather than remaining a filesystem probe that outlives the master switch.
    gate = _agent_gate()
    if gate:
        return gate
    d, err = _workspace_dir_from({"project_dir": request.args.get("project_dir")})
    if err:
        return err
    # `discover=1` asks us to look for a server the agent started but that we
    # never saw announced — needed because the URL is normally parsed from a LIVE
    # stream, so reloading the dashboard loses it while the server stays up.
    # Opt-in per request, never on the 2s poll: it probes a dozen ports.
    if request.args.get("discover") == "1":
        try:
            workspace.discover(d)
        except Exception:                                        # noqa: BLE001
            _log.debug("[workspace] discovery failed for %s", d, exc_info=True)
    return jsonify(workspace.status(d))


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
    # "normal" | "max" -- asked once, when the session starts. Anything else is
    # treated as "normal" rather than rejected: an older dashboard that does not
    # send the field must keep working exactly as it did.
    quality = body.get("quality")
    quality = quality if quality in ("normal", "max", "swarm") else "normal"
    try:
        session_id = agentic_chat.start_session(body.get("cli"), body.get("project_dir"),
                                                 create_new=create_new, quality=quality)
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


@app.route("/api/agent/sessions/<session_id>/resume", methods=["POST"])
def api_agent_resume_session(session_id):
    """Continue a conversation from the history list.

    Sessions live in memory, so a hub restart drops them — but the CLI's own
    thread does not: `codex exec resume <id>` / `claude --resume <id>` pick it
    back up with the model's full context. The transcript on disk now carries
    that id, so this rebuilds a live session pointed at the real thread instead
    of quietly starting a fresh one that has forgotten everything.

    Reuses the ORIGINAL session id, so the transcript keeps accumulating into
    the same conversation rather than forking a second copy of it."""
    gate = _agent_gate()
    if gate:
        return gate
    conv = agentic_history.get_conversation(session_id)
    if not conv:
        return jsonify({"error": "No stored conversation with that id.",
                        "code": "no_history"}), 404
    # The id is written on the agent's turns, so take the most recent one that
    # has it — earlier turns predate the thread existing.
    native = conv.get("native_session_id")
    for turn in reversed(conv.get("turns") or []):
        if turn.get("native_session_id"):
            native = turn["native_session_id"]
            break
    project_dir = conv.get("project_dir")
    if not project_dir or not os.path.isdir(project_dir):
        return jsonify({"error": "That project folder no longer exists: %s"
                        % _sanitize(str(project_dir)), "code": "folder_gone"}), 400
    try:
        sid = agentic_chat.resume_session(conv.get("cli_id"), project_dir,
                                          native, session_id=session_id)
    except agentic_chat.AgenticError as exc:
        payload = {"error": _sanitize(str(exc))}
        if exc.code:
            payload["code"] = exc.code
        payload.update(exc.extra)
        return jsonify(payload), exc.status
    # Put the rebuilt session back in the mode this conversation was running in.
    # Without this, resuming a Swarm build after a hub restart silently dropped
    # it to Normal -- the live session is new, so it starts at the default.
    saved_quality = conv.get("quality") or "normal"
    if saved_quality != "normal":
        agentic_chat.set_quality(sid, saved_quality)
    row = agentic_chat.get_session(sid) or {}
    # turn_count on a just-rebuilt session is always 0 -- it's a fresh Session
    # object with no turns played through it yet. The real count is what's
    # already sitting in the conversation this route just loaded from disk.
    row["turn_count"] = len(conv.get("turns") or [])
    # Honest about which kind of continue this is: with a thread id the model
    # still has the conversation; without one it only has the files on disk.
    row["resumed_thread"] = bool(native)
    return jsonify(row)


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
        # The files as they are RIGHT NOW, before this turn touches anything --
        # so restoring this turn's snapshot puts the project back to how it
        # looked when the message was sent. Fails open (returns None) rather
        # than ever holding up the turn; see snapshots.py.
        snap = None
        if config.get_flag("turn_snapshots", True):
            snap = snapshots.take(session_id, sess_info["project_dir"],
                                  "before: " + (body["text"] or "")[:60])
        agentic_history.record_turn(session_id, sess_info["cli"], sess_info["project_dir"],
                                    "user", body["text"], snapshot=snap)
        # Keep the saved conversation's mode in step with the live session. Done
        # here rather than at session start because the conversation record does
        # not exist until its first turn. set_quality returns without writing
        # when it already matches, so this is a dict lookup on every later turn.
        agentic_history.set_quality(session_id, sess_info.get("quality") or "normal")
    status, text, detail = agentic_chat.send_message(session_id, body["text"])
    if sess_info and status == 200 and text:
        # The CLI's OWN thread id, captured with the reply. Without it a
        # conversation cannot be continued after a hub restart -- sessions are
        # in memory, but `codex exec resume <id>` / `claude --resume <id>` pick
        # the real thread back up with the model's full context. Read AFTER the
        # turn: turn 1 is what creates it.
        _after = agentic_chat.get_session(session_id) or {}
        agentic_history.record_turn(session_id, sess_info["cli"], sess_info["project_dir"],
                                    "agent", text,
                                    native_session_id=_after.get("native_session_id"))
    return jsonify({"status": status, "text": text, "detail": detail}), status


@app.route("/api/agent/sessions/<session_id>/message/stream", methods=["POST"])
def api_agent_send_message_stream(session_id):
    """Live version of the message route: relays send_message_stream_durable's
    normalized progress events over SSE so the dashboard shows the agent working
    in real time. Records the user turn up front; the agent's reply is recorded
    by the durable wrapper's own background thread once the turn actually ends,
    whether or not this connection is still around to see it."""
    gate = _agent_gate()
    if gate:
        return gate
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("text"), str):
        return jsonify({"error": "Pass {\"text\": string}."}), 400
    text = body["text"]
    sess_info = agentic_chat.get_session(session_id)
    if sess_info:
        # Snapshot the files BEFORE the turn touches them, exactly as the
        # non-streaming route does. It was missing here, which made the whole
        # Restore & rerun feature inert on the path /build actually uses --
        # every turn from the dashboard streams.
        snap = None
        if config.get_flag("turn_snapshots", True):
            snap = snapshots.take(session_id, sess_info["project_dir"],
                                  "before: " + (text or "")[:60])
        agentic_history.record_turn(session_id, sess_info["cli"], sess_info["project_dir"],
                                    "user", text, snapshot=snap)
        agentic_history.set_quality(session_id, sess_info.get("quality") or "normal")

    def gen():
        # THE MODEL'S MEMORY. The CLI's own thread id is what `codex exec resume`
        # / `claude --resume` need to bring back the full context, and it used to
        # reach disk only when a turn COMPLETED -- so an interrupted turn lost it
        # and the next one started from nothing but the files. Persist it the
        # moment it exists instead. set_native_session_id does not write when it
        # is already stored, so this costs a dict lookup per event.
        saved_native = [False]

        def _keep_context():
            if saved_native[0]:
                return
            live = agentic_chat.get_session(session_id) or {}
            native = live.get("native_session_id")
            if native and agentic_history.set_native_session_id(session_id, native):
                saved_native[0] = True

        # The agent's reply is persisted from INSIDE send_message_stream_durable's
        # own background thread now, not here -- this loop merely relays events
        # to whoever is still connected. See its docstring: a plain generator
        # driven only by this route would silently lose the reply if the client
        # disconnected before the turn finished, even though the turn itself
        # completed for real.
        try:
            for ev in agentic_chat.send_message_stream_durable(session_id, text):
                _keep_context()
                yield "data: " + json.dumps(ev) + "\n\n"
        except Exception as exc:  # never leak a traceback into the stream
            yield "data: " + json.dumps({"event": "error", "status": 500,
                                         "detail": _sanitize(str(exc), 300)}) + "\n\n"
        finally:
            # Last chance, and the one that matters when the turn ERRORED: the
            # id may only have appeared on the way out.
            try:
                _keep_context()
            except Exception:                                    # noqa: BLE001
                pass
            yield "event: end\ndata: {}\n\n"

    return Response(stream_with_context(gen()), mimetype="text/event-stream", headers=_SSE_HEADERS)


@app.route("/api/agent/sessions/<session_id>/quality", methods=["POST"])
def api_agent_set_quality(session_id):
    """Switch a live session between normal and max quality.

    Works mid-conversation because the CLI subprocess is re-spawned per turn,
    so its ANTHROPIC_MODEL is rebuilt each time. A turn already running keeps
    the mode it started with -- its child is already launched."""
    gate = _agent_gate()
    if gate:
        return gate
    body = request.get_json(force=True, silent=True)
    q = body.get("quality") if isinstance(body, dict) else None
    if q not in ("normal", "max", "swarm"):
        return jsonify({"error": "Pass {\"quality\": \"normal\"|\"max\"|\"swarm\"}."}), 400
    out = agentic_chat.set_quality(session_id, q)
    if out is None:
        return jsonify({"error": "No such agentic session."}), 404
    # Mirror it into the saved conversation. The live session holds the mode but
    # does not survive a hub restart (and the hub restarts every 5h to update),
    # so without this, Continue tomorrow silently resumes on Normal.
    agentic_history.set_quality(session_id, out)
    _log.info("[agent] session quality -> %s", out)
    return jsonify({"session_id": session_id, "quality": out})


@app.route("/api/agent/sessions/<session_id>/stop", methods=["POST"])
def api_agent_stop_session(session_id):
    stopped = agentic_chat.stop_session(session_id)
    return jsonify({"stopped": stopped})


@app.route("/api/agent/sessions/<session_id>", methods=["DELETE"])
def api_agent_end_session(session_id):
    # Take the folder BEFORE ending: end_session drops the registry entry, and
    # after that there is nothing left to say which project this session owned.
    sess = agentic_chat.get_session(session_id)
    project_dir = getattr(sess, "project_dir", None) if sess else None
    ended = agentic_chat.end_session(session_id)
    # Ending a session ends the app it started. A dev server left holding :3000
    # after its session is gone is a leak the user has to clear by hand -- and
    # worse, the next project then finds that port busy or, until the ownership
    # guards landed, showed its site in the new project's preview.
    closed = False
    if ended and project_dir:
        try:
            closed = workspace.shutdown(project_dir)
        except Exception as exc:                                 # noqa: BLE001
            _log.warning("could not stop the preview for %s: %s", project_dir, exc)
    return jsonify({"ended": ended, "app_closed": bool(closed)})


# --------------------------------------------------------------------------- #
# Quick-chat history — the plain chat kept nothing across a reload
# --------------------------------------------------------------------------- #

@app.route("/api/chat/history", methods=["GET"])
def api_chat_history_list():
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
    except (TypeError, ValueError):
        limit = 50
    return jsonify({"conversations": quick_history.list_conversations(limit)})


@app.route("/api/chat/history/<cid>", methods=["GET"])
def api_chat_history_get(cid):
    conv = quick_history.load_conversation(cid)
    if not conv:
        return jsonify({"error": "No such conversation."}), 404
    return jsonify(conv)


@app.route("/api/chat/history/<cid>", methods=["DELETE"])
def api_chat_history_delete(cid):
    return jsonify({"deleted": quick_history.delete_conversation(cid)})


@app.route("/api/chat/history/<cid>/turn", methods=["POST"])
def api_chat_history_save_turn(cid):
    """Record one exchange.

    Written by the browser AFTER a reply completes, rather than by the chat
    endpoint itself: the quick chat calls /v1/chat/completions directly, the
    same endpoint every external CLI uses, and that endpoint has no idea which
    dashboard conversation a request belongs to. Teaching it would mean adding
    a hub-specific field to a public OpenAI-compatible API. The browser already
    knows, so it says."""
    body = request.get_json(silent=True) or {}
    user_text = body.get("user")
    assistant_text = body.get("assistant")
    if not isinstance(user_text, str) or not isinstance(assistant_text, str):
        return jsonify({"error": "Pass {user: str, assistant: str}."}), 400
    quick_history.save_turn(cid, user_text, assistant_text,
                            model=body.get("model"), provider=body.get("provider"))
    return jsonify({"ok": True})


@app.route("/api/agent/clis/<cli_id>/install", methods=["POST"])
def api_agent_cli_install(cli_id):
    """Install one agent CLI on demand, from the picker.

    Exists because the old path went through /api/subscriptions/<sub-*>/
    install-isolated, and that only covers CLIs that ARE subscription providers
    -- claude and codex. opencode is not one (it brings its own provider), so
    it had no install button at all. This works for every CLI the agent chat
    can drive, and does both halves: the machine's own copy, and the hub's
    isolated one."""
    gate = _agent_gate()
    if gate:
        return gate
    if cli_id not in _ISOLATED_NPM_PACKAGE:
        return jsonify({"error": "Unknown CLI '%s'." % _sanitize(str(cli_id), 40)}), 400
    bin_name = _CLI_BIN_NAME.get(cli_id, cli_id)
    out = {"cli": cli_id}
    if not shutil.which(bin_name):
        ok, res = _install_global_cli(cli_id)
        out["global"] = {"ok": ok, "error": res.get("error")}
    ok, res = _install_isolated_cli(cli_id, bin_name)
    res.pop("_status", None)
    out["isolated"] = {"ok": ok, "error": res.get("error")}
    if not ok and not out.get("global", {}).get("ok"):
        return jsonify({"error": res.get("error") or "install failed", **out}), 502
    return jsonify({"ok": True, **out})


@app.route("/api/agent/clis/<cli_id>/login", methods=["POST"])
def api_agent_cli_login(cli_id):
    """One-click sign-in for the hub's isolated copy of a CLI.

    Isolation on purpose means a SEPARATE, initially-empty credential store
    from the CLI the user already uses by hand -- asked for explicitly, and
    the reason the auth error names it. That separation still needs signing
    in once; this is what makes doing so one click in the dashboard rather
    than "copy this PowerShell line into a terminal yourself". Opens a real,
    visible window for the login flow itself and returns immediately -- the
    hub does not, and should not, see credentials as they are typed."""
    gate = _agent_gate()
    if gate:
        return gate
    ok, detail = agentic_chat.launch_isolated_login(cli_id)
    if not ok:
        return jsonify({"error": detail}), 400
    return jsonify({"ok": True})


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


@app.route("/api/agent/history/<session_id>/title", methods=["POST"])
def api_agent_history_set_title(session_id):
    """Rename a conversation. The auto-title is a guess from the first message,
    so correcting it has to be possible."""
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("title"), str):
        return jsonify({"error": "Pass {\"title\": str}."}), 400
    title = agentic_history.set_title(session_id, body["title"])
    if title is None:
        return jsonify({"error": "No such conversation."}), 404
    return jsonify({"session_id": session_id, "title": title})


@app.route("/api/agent/history/<session_id>", methods=["DELETE"])
def api_agent_history_delete(session_id):
    deleted = agentic_history.delete_conversation(session_id)
    if not deleted:
        return jsonify({"error": "No such conversation."}), 404
    return jsonify({"deleted": True})


@app.route("/api/agent/history/<session_id>/rewind", methods=["POST"])
def api_agent_history_rewind(session_id):
    """Go back to how things were before turn `index`: FILES and transcript.

    Asked for directly -- "checkpoints buttons for previous message to restaure
    conversation and code and rerun it". Restoring only the conversation would
    be worse than useless, because the transcript would then claim a state the
    files no longer have; both halves move together or neither does.

    DESTRUCTIVE, and deliberately not idempotent-by-accident: work done after
    that point is discarded. The UI confirms first, and this refuses outright
    unless the turn actually carries a snapshot."""
    gate = _agent_gate()
    if gate:
        return gate
    body = request.get_json(force=True, silent=True) or {}
    try:
        index = int(body.get("index"))
    except (TypeError, ValueError):
        return jsonify({"error": "index is required."}), 400
    conv = agentic_history.get_conversation(session_id)
    if not conv:
        return jsonify({"error": "No stored conversation with that id.",
                        "code": "no_history"}), 404
    turns = conv.get("turns") or []
    if not 0 <= index < len(turns):
        return jsonify({"error": "No such turn."}), 400
    snap = (turns[index] or {}).get("snapshot")
    if not snap:
        return jsonify({"error": "That message has no file snapshot, so the code "
                                 "cannot be restored to it.",
                        "code": "no_snapshot"}), 400
    project_dir = conv.get("project_dir")
    if not project_dir or not os.path.isdir(project_dir):
        return jsonify({"error": "That project folder no longer exists: %s"
                        % _sanitize(str(project_dir)), "code": "folder_gone"}), 400
    if not snapshots.restore(session_id, project_dir, snap):
        return jsonify({"error": "Could not restore the files for that message.",
                        "code": "restore_failed"}), 500
    removed = agentic_history.truncate_to_turn(session_id, index)
    return jsonify({"session_id": session_id, "index": index,
                    "turns_removed": removed or 0,
                    "text": (turns[index] or {}).get("text") or "",
                    "project_dir": project_dir})


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
    "artificial_analysis_api_key",
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


def _p_kimi():
    """Kimi Code's config.toml (~/.kimi/config.toml on every platform)."""
    return os.path.join(_home(), ".kimi", "config.toml")


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
    {
        "id": "kimi",
        "name": "Kimi Code",
        "kind": "openai",
        "bins": ["kimi"],
        "config_paths": [_p_kimi()],
        # NO env_check: Kimi Code wires custom providers ONLY through
        # ~/.kimi/config.toml ([providers.*] tables). Its documented credential
        # priority is config api_key > [providers.*.env] sub-table — there is NO
        # shell-env fallback, so checking OPENAI_BASE_URL/OPENAI_API_KEY here
        # would false-positive exactly like it did for codex.
        # ONE-CLICK since 2026-07-31 (was manual-only): the same additive TOML
        # rewrite Codex already gets. The one risky part — clobbering the live
        # default_model (normally Kimi's managed 'kimi-code' OAuth service) —
        # is handled by remembering it in the `kimi_prev_default_model` setting
        # and restoring it on Disconnect. Manual TOML below stays as a fallback.
        "autofix": "kimi",
        "write_path": _p_kimi(),
        "default_method": "config",
        "hint": ("Installed. Connect writes a [providers.free-hub] block + an 'auto' model "
                 "alias into ~/.kimi/config.toml and switches default_model to it; "
                 "Disconnect strips them and restores your previous default_model. "
                 "Restart kimi afterwards."),
        "manual_note": (
            "One click does all of this — Connect writes it for you (and Disconnect reverses it, "
            "restoring your previous default_model); the manual steps below are only a fallback.\n\n"
            "Kimi Code is wired through ~/.kimi/config.toml ([providers.*] tables), NOT shell "
            "environment variables. Per the official docs, api_key is a REQUIRED field — startup "
            "fails without one — so give it a placeholder; the localhost hub accepts any bearer "
            "when no local API key is set (if you DID set a hub key, paste that instead). Add:\n"
            "  [providers.free-hub]\n"
            "  type = \"openai\"\n"
            "  base_url = \"http://127.0.0.1:%d/v1\"\n"
            "  api_key = \"free-llm-hub\"\n"
            "plus a model alias:\n"
            "  [models.\"auto\"]\n"
            "  provider = \"free-hub\"\n"
            "  model = \"auto\"\n"
            "  max_context_size = 128000\n"
            "then set  default_model = \"auto\"  in the top section (or pick it via /model in the "
            "TUI) and restart Kimi Code. The 'auto' model routes every request through the hub's "
            "difficulty-aware orchestration. ALTERNATIVE: type = \"anthropic\" with "
            "base_url = \"http://127.0.0.1:%d\" (no /v1 — the Anthropic SDK appends it) also "
            "works; the hub serves /v1/messages." % (PORT, PORT)
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
#   kimi   -> [providers.*] tables in ~/.kimi/config.toml (no shell-env fallback)
_ENVLESS_CLIS = {"codex", "gemini", "llm", "kimi"}


def _hub_fragments():
    """Substrings that, if present in a CLI's config/env, mean it points here.
    The PORT is the discriminator, so this matches both the bare origin and the
    /v1 form (http://127.0.0.1:<PORT>, http://127.0.0.1:<PORT>/v1, ...)."""
    return ["127.0.0.1:%d" % PORT, "localhost:%d" % PORT, "[::1]:%d" % PORT]


def _points_at_hub(val):
    """True if a config value (string) targets THIS hub's origin/port."""
    return isinstance(val, str) and any(fr in val for fr in _hub_fragments())


# Every JSON container key mcp_manager.py's CLIs write a hub MCP entry under.
# Deliberately NOT keyed by CLI id here: _file_points_at_hub only has a path,
# not which CLI it belongs to, and trying every known shape is both simpler
# and safer than threading CLI identity through every caller — an unrelated
# app coincidentally using the SAME container key name is not a realistic risk.
_HUB_MCP_JSON_KEYS = ("mcpServers", "mcp")  # claude / opencode (+ generic default)


def _strip_hub_mcp_table(text):
    """Drop the hub's OWN MCP server registration from a COPY of `text`
    before any "does this still point at the hub" scan — format-agnostic:
    tries JSON (claude's flat "mcpServers", opencode's flat "mcp", openclaw's
    nested "mcp.servers"), then TOML ([mcp_servers.free-llm-hub]), then YAML
    (hermes' "mcp_servers:" block). Whichever one actually matches wins;
    the rest are silent no-ops.

    MEASURED LIVE 2026-08-09, TWICE, in two different config formats: a user
    disconnected Codex (TOML) and separately OpenCode (JSON) — in both cases
    the model-provider wiring was correctly stripped, yet the UI still
    reported "disconnected in config — but it still reports as connected".
    Root cause: the SAME config file also carries an MCP server registration
    for this hub (a completely separate, INTENTIONALLY-persistent feature —
    see the comment in _disconnect_codex: "[mcp_servers.*] ... the user added
    ... survives"), and that entry's url still contains 127.0.0.1:<PORT>. The
    blind whole-file substring scan has no way to tell "still wired as the
    model provider" from "still registered as an MCP tool server" apart, so
    it reported a false leftover connection. An initial fix only stripped the
    TOML shape, which fixed Codex but left OpenCode (JSON) broken — this
    covers every format mcp_manager.py itself writes, not just one.

    Best-effort/no-raise throughout: on any parse hiccup for a given format,
    that format is skipped (not treated as a match) so a genuine leftover
    connection in a DIFFERENT format is never hidden by a broken strip."""
    # JSON (claude, opencode, openclaw).
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            changed = False
            for key in _HUB_MCP_JSON_KEYS:
                holder = data.get(key)
                if isinstance(holder, dict) and holder.pop("free-llm-hub", None) is not None:
                    changed = True
            mcp_holder = data.get("mcp")
            if isinstance(mcp_holder, dict):
                servers = mcp_holder.get("servers")
                if isinstance(servers, dict) and servers.pop("free-llm-hub", None) is not None:
                    changed = True
            if changed:
                return json.dumps(data)
            return text  # valid JSON, nothing to strip -- do not fall through to TOML/YAML
    except (ValueError, TypeError):
        pass
    # TOML (codex, kimi).
    try:
        stripped, removed = mcp_manager._remove_toml_server(text, "free-llm-hub")
        if removed:
            return stripped
    except Exception:
        pass
    # YAML (hermes).
    try:
        block, start, end = mcp_manager._yaml_block(text)
        if block is not None:
            cleaned = mcp_manager._yaml_remove_entry(block, "free-llm-hub")
            if cleaned != block:
                return text[:start] + cleaned + text[end:]
    except Exception:
        pass
    return text


def _file_points_at_hub(path):
    """True if the file's raw text contains a hub origin substring. Fail-open:
    an unreadable file reads as 'not pointing here' (never raises)."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            txt = f.read()
    except OSError:
        return False
    return any(fr in _strip_hub_mcp_table(txt) for fr in _hub_fragments())


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
        # See _strip_hub_mcp_table: an MCP server registration for this hub is
        # a separate, deliberately-persistent feature and must not count as
        # "still wired as the model provider".
        if any(fr in _strip_hub_mcp_table(txt) for fr in frags):
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
        # 'auto', never a '<pid>/<model>' pin — see _autofix_claude / _connect_snippets.
        return {"ANTHROPIC_BASE_URL": base_root,
                "ANTHROPIC_AUTH_TOKEN": key,
                "ANTHROPIC_MODEL": "auto"}
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
    # ALWAYS 'auto' (never the passed-in '<pid>/<model>'): _is_orchestrate() returns
    # False for any id containing '/', so a pinned id sends every Claude Code request
    # to ONE provider and skips difficulty/vision routing + load spreading entirely.
    # Assigned rather than skipped so a stale pin from an earlier connect is cleared.
    env["ANTHROPIC_MODEL"] = "auto"
    data["env"] = env
    _cli_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return {
        "ok": True,
        "wrote_path": path,
        "backup_path": backup,
        "applied": {"file_key": "env", "ANTHROPIC_BASE_URL": base_root,
                    "ANTHROPIC_AUTH_TOKEN": _mask_key(key), "ANTHROPIC_MODEL": "auto"},
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
    # 'auto' (not a '<pid>/<model>' pin) so every request is orchestrated — same
    # reasoning as _autofix_claude / _autofix_codex, and 'auto' is advertised by
    # /v1/models so a model-validating client still accepts it.
    updates = {"OPENAI_API_BASE": base_v1, "OPENAI_BASE_URL": base_v1,
               "OPENAI_API_KEY": key, "OPENAI_MODEL": "auto"}
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
                    "OPENAI_API_KEY": _mask_key(key), "OPENAI_MODEL": "auto"},
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


_KIMI_DEFAULT_MODEL_RE = re.compile(r"""^\s*default_model\s*=\s*["']([^"']*)["']\s*$""")


def _kimi_prev_default_model(text):
    """The `default_model` currently declared in the TOP (pre-table) section of
    ~/.kimi/config.toml, or None. Stops at the first '[table]' header — a bare
    key is only valid TOML above it, so anything below belongs to a table."""
    for ln in text.splitlines():
        if _CODEX_TABLE_RE.match(ln):
            break
        m = _KIMI_DEFAULT_MODEL_RE.match(ln)
        if m:
            return m.group(1)
    return None


def _kimi_apply_text(text, base_v1, key):
    """Pure transform for ~/.kimi/config.toml (no IO). ADDITIVELY + REVERSIBLY:
      1. drop any previous [providers.free-hub] / [models."auto"] tables so a
         re-connect always rewrites them clean (new port, new hub key, ...);
      2. in the TOP section set default_model = "auto" (replacing an existing
         default_model line in place, else prepending it);
      3. append the two tables that wire Kimi Code to this hub.
    Every other line (other [providers.*], [models.*], MCP blocks, comments)
    survives verbatim. Returns the new file text."""
    body, _ = _remove_toml_table(text, "providers.free-hub")
    body, _ = _remove_toml_table(body, 'models."auto"')
    top, rest, in_rest = [], [], False
    for ln in body.splitlines():
        if not in_rest and _CODEX_TABLE_RE.match(ln):
            in_rest = True
        (rest if in_rest else top).append(ln)
    pat = re.compile(r"^\s*default_model\s*=")
    for i, ln in enumerate(top):
        if pat.match(ln):
            top[i] = 'default_model = "auto"'
            break
    else:
        top.insert(0, 'default_model = "auto"')
    block = [
        "[providers.free-hub]",
        'type = "openai"',
        'base_url = "%s"' % base_v1,
        # api_key is a REQUIRED field per Kimi's docs (startup fails without
        # one). The localhost hub accepts any bearer when no local key is set.
        'api_key = "%s"' % key,
        "",
        '[models."auto"]',
        'provider = "free-hub"',
        'model = "auto"',
        "max_context_size = 128000",
    ]
    new_text = "\n".join(top + rest).rstrip("\n")
    return (new_text + "\n\n" if new_text else "") + "\n".join(block) + "\n"


def _autofix_kimi(entry, key, base_root, base_v1, model):
    """Point Kimi Code at this hub in ONE click. Kimi is wired ONLY through
    ~/.kimi/config.toml ([providers.*] tables) — it has no shell-env fallback —
    so this writes that file additively/reversibly (a .freehub-bak backup is
    taken first) instead of handing out manual TOML to paste. The previous
    default_model (normally Kimi's managed 'kimi-code' OAuth service) is
    remembered so Disconnect puts it back exactly."""
    path = _p_kimi()
    try:
        if os.path.isfile(path):
            # utf-8-sig: strip a leading BOM so it can't land mid-file after we
            # prepend default_model (a mid-file BOM breaks TOML parsing).
            with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
                text = f.read()
        else:
            text = ""
    except OSError as exc:
        return {"ok": False, "reason": _sanitize("could not read %s: %s" % (_short(path), exc))}
    backup = _backup_once(path)
    abort = _abort_if_backup_failed(path, backup)
    if abort:
        return abort
    prev = _kimi_prev_default_model(text)
    if prev and prev != "auto":
        config.set_setting("kimi_prev_default_model", prev)  # remember for Disconnect
    _cli_write_text(path, _kimi_apply_text(text, base_v1, key))
    return {
        "ok": True,
        "wrote_path": path,
        "backup_path": backup,
        "applied": {"provider": "free-hub", "type": "openai", "base_url": base_v1,
                    "api_key": _mask_key(key), "model_alias": "auto",
                    "default_model": "auto"},
        "note": ("Connected. Kimi Code now routes every request through the hub's "
                 "difficulty-aware orchestration (the 'auto' model)."
                 + (" Your previous default_model (%s) is remembered and restored "
                    "on Disconnect." % prev if prev and prev != "auto" else "")),
        "restart_hint": ("Restart Kimi Code — it reads ~/.kimi/config.toml on startup. "
                         "In an already-open session, pick 'auto' via /model."),
    }


_AUTOFIXERS = {
    "claude": _autofix_claude,
    "aider": _autofix_aider,
    "opencode": _autofix_opencode,
    "qwen": _autofix_qwen,
    "codex": _autofix_codex,
    "openclaw": _autofix_openclaw,
    "hermes": _autofix_hermes,
    "kimi": _autofix_kimi,
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


def _kimi_restore_default_model(text):
    """Undo our `default_model = "auto"` in the TOP (pre-table) section only:
    put back the value remembered at connect time, or drop the line entirely if
    there was none. A default_model the user has since changed to something
    else is left alone (the line no longer matches). Returns
    (new_text, changed_bool)."""
    prev = config.get_setting("kimi_prev_default_model")
    ours = re.compile(r'^\s*default_model\s*=\s*"auto"\s*$')
    out, changed, in_rest = [], False, False
    for ln in text.splitlines():
        if not in_rest and _CODEX_TABLE_RE.match(ln):
            in_rest = True
        if not in_rest and ours.match(ln):
            changed = True
            if isinstance(prev, str) and prev:
                out.append('default_model = "%s"' % prev)
            continue
        out.append(ln)
    if changed and isinstance(prev, str) and prev:
        config.set_setting("kimi_prev_default_model", "")  # consumed
    new_text = "\n".join(out).rstrip("\n")
    return (new_text + "\n" if new_text else ""), changed


def _disconnect_kimi(entry):
    """Revert Kimi Code: strip ONLY the two tables we added plus our
    default_model line (restoring the remembered one), so any provider / model
    alias / setting added to config.toml after connecting survives. Restoring
    the frozen first-connect backup is the last resort, for a file we can no
    longer read at all."""
    path = entry.get("write_path") or _p_kimi()
    hint = ("Restart Kimi Code so it re-reads ~/.kimi/config.toml "
            "(an open session keeps the old model until then).")
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
                text = f.read()
        except OSError:
            text = None
        if text is not None:
            text2, prov_removed = _remove_toml_table(text, "providers.free-hub")
            text3, alias_removed = _remove_toml_table(text2, 'models."auto"')
            text4, top_changed = _kimi_restore_default_model(text3)
            changed = bool(prov_removed or alias_removed or top_changed)
            if changed:
                _cli_write_text(path, text4)
            _discard_backup(path)  # strip succeeded -> stale backup no longer needed
            return {"restored_from_backup": False, "wrote_path": path,
                    "changed": changed, "restart_hint": hint}
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
    "kimi": _disconnect_kimi,
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


# --------------------------------------------------------------------------- #
# Prompt enhancement (dashboard only).
#
# Rewrites the user's OPENING prompt into a sharper one before it is sent, so a
# two-word ask still gets the model's best work. Deliberately scoped:
#   * only the FIRST user turn of a conversation — rewriting every turn fights
#     the user mid-conversation, where they are correcting and steering;
#   * only prompts typed IN THE DASHBOARD. Traffic on /v1/* is never touched:
#     rewriting a turn that carries tool_calls or a diff breaks the agent loop.
# Fail-open at every step: any error, timeout, refusal or empty result returns
# the ORIGINAL text. Enhancement must never block or corrupt a generation.
#
# ANTI-SLOP is the point, not a nicety. The failure mode of every naive prompt
# enhancer is padding: image prompts become "masterpiece, 8k, ultra-detailed,
# award-winning, trending on artstation", and chat prompts sprout "provide a
# comprehensive overview". That is worse than the original, so both system
# prompts below ban it explicitly and cap the output length.
# --------------------------------------------------------------------------- #
_ENHANCE_MAX_INPUT = 4000       # longer than this is already a considered prompt
_ENHANCE_MAX_TOKENS = 400

# MEASURED 2026-07-31: routed as `simple` (which the text alone classifies it
# as), enhancement landed on groq/allam-2-7b, which ANSWERED the prompt instead
# of rewriting it -- "fix my python bug" came back as "Please provide the
# specific bug in your Python code...". That would have replaced the user's
# question with an assistant reply. Rewriting is a short but real
# instruction-following job, so it is routed as `medium` (= strongest model).
_ENHANCE_DIFFICULTY = "medium"

# Overall deadline for ONE enhance hop. Enhancement is a nice-to-have rewrite
# the user is WAITING on with a dead-looking composer (reported 2026-08-06:
# "Just answer" click, then 150s+ of silence — the hop list was walking
# degraded providers at CHAT_READ_TIMEOUT=300s apiece). 45s is generous for a
# 400-token rewrite; past it the next hop tries, and the client-side timeout
# (20s, enhanceOpening in index.html) is the outer guard that keeps the UI
# alive even if every hop stalls.
_ENHANCE_HOP_DEADLINE = 45

# Second line of defence for the same failure: a model that answers instead of
# rewriting almost always opens with one of these. Cheap, and a false positive
# only costs us the (optional) enhancement.
_ENHANCE_ANSWERED_RE = re.compile(
    r"^\s*(sure|certainly|of course|absolutely|okay|ok)\b"
    r"|^\s*(please\s+(provide|share|paste|send)|could you (please )?(provide|share))"
    r"|^\s*(i('| a)?m |i can |i'd be |i will |i'll |let me |as an ai|as a language model)"
    r"|^\s*(here('s| is)|to (fix|solve|debug|answer)\b)"
    r"|^\s*(great|good) (question|point)\b",
    re.I)

_ENHANCE_SYSTEM = {
    "image": (
        "You clarify a user's image prompt. Reply with the rewritten prompt ONLY — no "
        "preamble, no quotes, no explanation, no options.\n"
        "THE USER DIRECTS THE IMAGE, NOT YOU. Never invent a style, art movement, medium, "
        "mood, lighting setup, colour palette, camera angle, lens, or setting that the "
        "user did not state. If they wrote 'a fox', the image is a fox — do not decide it "
        "is whimsical, golden-hour, or shot on 85mm. A prompt with no style stays a prompt "
        "with no style; the model's own default is the correct look.\n"
        "What you MAY do: fix grammar and ambiguity, resolve contradictions, make a "
        "pronoun or vague reference concrete, and spell out detail the user already "
        "implied. Keep every word of their direction that is already there, and sharpen it "
        "if it is fuzzy.\n"
        "BANNED — never emit these, they are noise that degrades modern image models: "
        "'masterpiece', '8k', '4k', 'ultra-detailed', 'hyper-realistic', 'award-winning', "
        "'trending on artstation', 'best quality', 'highly detailed', 'stunning', "
        "'breathtaking', stacked style tags, or a trailing pile of comma-separated "
        "adjectives.\n"
        "Never invent a real person, brand, logo or trademark that the user did not name.\n"
        "Stay under 60 words. Returning the prompt UNCHANGED is the right answer whenever "
        "it is already clear — that is the common case, not a failure."
    ),
    "chat": (
        "You rewrite a user's question into a sharper version of the SAME question. Reply "
        "with the rewritten prompt ONLY — no preamble, no commentary, no answer to it.\n"
        "Preserve the user's intent, language, and every concrete detail they gave. Make "
        "implicit requirements explicit, and state the output shape they clearly want "
        "(e.g. working code, a direct comparison, a short answer).\n"
        "Add this constraint when it fits: answer directly, no restating the question, no "
        "filler openings, no padded lists, no hedging.\n"
        "BANNED — never add: 'act as a world-class expert', 'think step by step', 'provide "
        "a comprehensive overview', role-play framing, flattery, or invented requirements "
        "the user never asked for.\n"
        "Never answer the question. Stay under 120 words. If the prompt is already precise, "
        "return it unchanged."
    ),
}


def _enhance_prompt(text, kind="chat"):
    """(enhanced_text, model_label_or_None). Never raises; returns the ORIGINAL
    text whenever anything at all goes wrong, so a failed enhancement is
    invisible rather than destructive."""
    original = (text or "").strip()
    if not original or len(original) > _ENHANCE_MAX_INPUT:
        return original, None
    system = _ENHANCE_SYSTEM.get(kind) or _ENHANCE_SYSTEM["chat"]
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": original}]
    try:
        # require_tools=False and a tiny budget: this is a helper call, it must
        # not consume a strong tool-capable hop that real work is waiting for.
        pid, model, _diff = _route_by_difficulty(messages, _ENHANCE_MAX_TOKENS,
                                                 require_tools=False,
                                                 force_difficulty=_ENHANCE_DIFFICULTY)
        if not pid:
            return original, None
        for hop_pid, hop_model in _build_chain(pid, model):
            if _is_sub(hop_pid):
                continue        # never spend the user's paid subscription on a rewrite
            payload = {"model": hop_model, "stream": False,
                       "max_tokens": _ENHANCE_MAX_TOKENS, "messages": messages}
            resp, _hop_exc = _dispatch_chat_with_deadline(hop_pid, payload,
                                                          _ENHANCE_HOP_DEADLINE)
            if resp is None:         # hung or raised hop — try the next one
                continue
            try:
                if resp.status_code != 200:
                    continue
                data = resp.json() or {}
            except ValueError:
                continue
            finally:
                try:
                    resp.close()
                except Exception:                                    # noqa: BLE001
                    pass
            out = (((data.get("choices") or [{}])[0].get("message") or {})
                   .get("content") or "").strip()
            # A model that ignored "prompt only" and answered the question
            # instead would silently replace the user's ask with an answer.
            # Guards: non-empty, not absurdly longer than the original, and not
            # opening like a reply (see _ENHANCE_ANSWERED_RE). Any miss falls
            # through to the next hop, and finally to the original text.
            if not out or len(out) > max(600, len(original) * 6):
                continue
            out = out.strip().strip('"').strip()
            if _ENHANCE_ANSWERED_RE.search(out):
                _log.debug("[enhance] hop answered instead of rewriting; skipping")
                continue
            return (out or original), (hop_pid + "/" + hop_model)
    except Exception:                                                # noqa: BLE001
        _log.debug("[enhance] failed, returning the original prompt", exc_info=True)
    return original, None


@app.route("/api/prompt-enhance", methods=["GET", "POST"])
def api_prompt_enhance_flag():
    """The dashboard's Enhance-opening-prompts switch. Default ON: the feature
    exists because the user asked for every prompt to get the model's best
    work, so opting OUT is the deliberate act, not opting in."""
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        config.set_flag("prompt_enhance", bool(body.get("enabled")))
    return jsonify({"enabled": config.get_flag("prompt_enhance", True)})


@app.route("/api/enhance-prompt", methods=["POST"])
def api_enhance_prompt():
    """{prompt, kind:'chat'|'image'} -> {original, enhanced, changed, model}.
    Dashboard-only helper; /v1/* traffic is never enhanced."""
    body = request.get_json(silent=True) or {}
    original = str(body.get("prompt") or "")
    kind = str(body.get("kind") or "chat").lower()
    if kind not in _ENHANCE_SYSTEM:
        kind = "chat"
    if not original.strip():
        return jsonify({"error": "prompt is required"}), 400
    if not config.get_flag("prompt_enhance", True):
        return jsonify({"original": original, "enhanced": original,
                        "changed": False, "model": None, "disabled": True})
    enhanced, model = _enhance_prompt(original, kind)
    return jsonify({"original": original, "enhanced": enhanced,
                    "changed": enhanced.strip() != original.strip(),
                    "model": model})


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
        payload = {"model": hop_model, "max_tokens": 16, "stream": False,  # 16 = Perplexity's floor
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


@app.route("/api/chains", methods=["GET", "POST", "DELETE"])
def api_chains():
    """Named fallback chains: an ORDER the user asserts.

    Distinct from the category buttons, which are a SET used to filter what
    routing may consider while leaving the benchmark to order it. A chain says
    "try this exact model, then that one", which is the only way to express "I
    know this pairing works for my project".

    It is a preference, not a cage: the ordinary chain still follows, so a chain
    whose models are all rate-limited degrades to normal routing instead of
    failing. One that could dead-end would be worse than none, because it would
    fail exactly when everything is busiest."""
    if request.method == "GET":
        chains = _named_chains()
        return jsonify({"chains": [
            {"name": n,
             "models": ids,
             # Which entries are actually usable right now: a chain saved months
             # ago can name models a provider has since withdrawn.
             "live": [p + "/" + m for p, m in _chain_entries(n)]}
            for n, ids in sorted(chains.items())]})

    body = request.get_json(force=True, silent=True) or {}
    name = str(body.get("name") or "").strip().lower()
    if not name:
        return jsonify({"error": "'name' is required."}), 400
    if "/" in name or name in ("auto", "best", "swarm", "crew", "team", "plan"):
        # Those already mean something as a model id; a chain shadowing one
        # would silently change what an existing client asks for.
        return jsonify({"error": "'%s' is already a model id." % name}), 400
    chains = _named_chains()
    if request.method == "DELETE":
        chains.pop(name, None)
        config.set_json(_CHAINS_KEY, chains)
        return jsonify({"ok": True, "removed": name})
    models = body.get("models")
    if not isinstance(models, list) or not models:
        return jsonify({"error": "'models' must be a non-empty list of '<provider>/<model>' ids."}), 400
    ids = [str(m).strip() for m in models if isinstance(m, str) and m.strip()]
    if not ids:
        return jsonify({"error": "'models' held no usable ids."}), 400
    chains[name] = ids
    config.set_json(_CHAINS_KEY, chains)
    return jsonify({"ok": True, "name": name, "models": ids,
                    "live": [p + "/" + m for p, m in _chain_entries(name)]})


@app.route("/api/model-speed", methods=["GET"])
def api_model_speed():
    """Measured speed per model: p50/p95 total, and p50/p95 time-to-first-token.

    Separate from /api/models because it answers a different question. The
    catalog says what exists and how it is ranked; this says how it has actually
    behaved on this machine, which is the only place that evidence lives.

    Models with no samples yet are omitted rather than reported as zero -- an
    unmeasured model is not a fast one."""
    rows = []
    with _outcome_lock:
        keys = set(_speed) | set(_ttft)
    for pid, model in sorted(keys):
        prof = _speed_profile(pid, model)
        if not prof["samples"] and not prof["ttft_samples"]:
            continue
        prof.update({"id": pid + "/" + model, "provider": pid, "model": model})
        rows.append(prof)
    # Slowest first: the reason to look at this page is to find what is hurting.
    rows.sort(key=lambda r: (r["p95_ms"] or r["ttft_p95_ms"] or 0), reverse=True)
    return jsonify({"models": rows, "sample_cap": _SPEED_SAMPLES,
                    "note": "measured since the hub last started"})


@app.route("/api/response-cache", methods=["GET", "POST"])
def api_response_cache():
    """The response cache's switch and its counters.

    Same reasoning as /api/ollama: off by default is a real decision, but
    reaching it should not mean editing a JSON file."""
    if request.method == "POST":
        body = request.get_json(force=True, silent=True) or {}
        if body.get("clear"):
            respcache.clear()
        want = body.get("enabled")
        if want is not None:
            if not isinstance(want, bool):
                return jsonify({"error": "'enabled' must be true or false."}), 400
            config.set_flag("response_cache", want)
        ttl = body.get("ttl")
        if ttl is not None:
            try:
                config.set_value("response_cache_ttl", str(max(30, int(ttl))))
            except (TypeError, ValueError):
                return jsonify({"error": "'ttl' must be a number of seconds."}), 400
    st = respcache.stats()
    st["enabled"] = config.get_flag("response_cache", False)
    st["ttl"] = _cache_ttl()
    return jsonify(st)


@app.route("/api/ollama", methods=["GET", "POST"])
def api_ollama():
    """The Ollama surface's on/off switch, as a control endpoint.

    It exists because "set the ollama_api flag" is not an answer anyone should
    have to act on. The surface is off by default for a real reason -- it is an
    extra, auth-less-by-default shape on the control port -- but "off by
    default" and "hidden behind a config file" are different things, and only
    the first one was intended."""
    if request.method == "POST":
        body = request.get_json(force=True, silent=True) or {}
        want = body.get("enabled")
        if not isinstance(want, bool):
            return jsonify({"error": "'enabled' must be true or false."}), 400
        config.set_flag("ollama_api", want)
    enabled = _ollama_enabled()
    key = config.get_local_api_key()
    return jsonify({
        "enabled": enabled,
        # Ollama clients ask for a HOST, not a /v1 base: they append /api/... on
        # their own. Handing them the /v1 URL is the single most common way to
        # get "connection refused" out of an otherwise correct setup.
        "base_url": "http://127.0.0.1:%d" % PORT,
        "openai_base_url": "http://127.0.0.1:%d/v1" % PORT,
        "api_key": key or "",
        "key_required": bool(key),
        "paths": sorted(_OLLAMA_ONLY_PATHS | _OLLAMA_SHARED_PATHS),
        "models": ["auto", "best", "swarm"],
    })


@app.route("/api/antigravity", methods=["GET"])
def api_antigravity():
    """Antigravity is NOT in CLI_REGISTRY on purpose.

    Every card there offers Connect / Disconnect / Test, which all assume the
    hub can write the tool's config and then prove the round trip. For
    Antigravity that would be a lie in both directions: its launcher has no
    headless exec mode to test through, and its built-in agent has no provider
    setting to write (see antigravity.py for what was actually measured). So it
    gets its own card that offers the two things that ARE real -- detection and
    a genuine one-click extension install -- and states the manual step instead
    of pretending it away."""
    info = antigravity.detect()
    key = config.get_local_api_key() or "free-llm-hub"
    info["connect"] = {
        "base_url": "http://127.0.0.1:%d/v1" % PORT,
        "api_key": key,
        "model": "auto",
        "models": ["auto", "best", "swarm"],
    }
    return jsonify(info)


@app.route("/api/antigravity/install", methods=["POST"])
def api_antigravity_install():
    body = request.get_json(force=True, silent=True) or {}
    ok, message = antigravity.install_extension(body.get("extension"))
    payload = {"ok": ok, "message": message}
    payload.update(antigravity.detect())
    return jsonify(payload), (200 if ok else 400)


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
    # 'auto' is a REAL, selectable id here (it is what _is_orchestrate accepts) and
    # is listed FIRST so a CLI that validates its configured model — or just takes
    # the first row as its default — lands on the orchestrator instead of being
    # pinned to one provider's model for every request.
    auto = {"id": "auto", "object": "model", "created": 0, "owned_by": "free-llm-hub",
            "display_name": "auto (orchestrated — best free model per task)"}
    # Virtual pipeline models (the SWARM/CREWS section below): selectable ids that
    # run the multi-phase pipeline instead of a single model.
    virtual = [{"id": mid, "object": "model", "created": 0,
                "owned_by": "free-llm-hub",
                "display_name": mid + " (multi-model pipeline)"}
               for mid in _SWARM_IDS + tuple(crews.CREW_IDS)]
    rows = [_codex_model_entry(m) for m in agg]
    return jsonify({"object": "list",
                    "data": [auto] + virtual + rows,
                    "models": [dict(auto, slug="auto")]
                              + [dict(v, slug=v["id"]) for v in virtual]
                              + [dict(_codex_model_entry(m), slug=m["id"]) for m in agg]})


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


def _stream_peek_timeout(model, est):
    """Adaptive deadline for _peek_until_content: how long to wait for a stream's
    first REAL content before falling through to the next hop. A fast model on a
    small request answers in seconds, so a >35s silence means a hung provider —
    but a slow/reasoning model (_SLOW_MODEL_RE) or a big agentic prompt (Codex's
    15-40K tokens + tool schemas) legitimately needs longer before the first
    byte, and killing that hop abandoned HEALTHY strong models mid-chain."""
    slow = bool(_SLOW_MODEL_RE.search((model or "").lower()))
    big = bool(est) and est >= STREAM_BIG_REQUEST_TOKENS
    if slow and big:
        return STREAM_SLOW_BIG_PEEK_TIMEOUT
    if slow or big:
        return STREAM_SLOW_PEEK_TIMEOUT
    return STREAM_CONTENT_PEEK_TIMEOUT


# How much of a streaming answer to collect before judging it. Small on
# purpose: the failures being caught are short (an announcement is a sentence, a
# refusal opens with one), and a turn that is really working emits a tool call,
# which commits the stream immediately without waiting for any of this.
_PEEK_JUDGE_CHARS = 600
# A tool-call delta in an SSE frame, in any of the three protocols' shapes.
_STREAM_TOOLCALL_RE = re.compile(
    rb'"(?:tool_calls|function_call|tool_use|function_call_arguments)"', re.I)


def _judge_peeked(chunks):
    """"content" or "nonanswer" for the text collected during the peek.

    Runs the same three detectors the non-streaming path has always had. They
    were unreachable on a stream, which is the path Codex and Claude Code
    actually use -- so a turn that typed its tool call, announced work it never
    did, or declined outright was relayed to the CLI as a finished answer."""
    try:
        text = _peeked_text("".join(chunks))
        if not text:
            return "content"
        if (_looks_like_text_tool_call(text)
                or _looks_like_announced_not_acted(text)
                or _looks_like_refusal(text)):
            return "nonanswer"
    except Exception:                                            # noqa: BLE001
        pass
    return "content"


# A JSON string value for one of the keys an SSE delta puts visible text
# under. Non-greedy up to the first unescaped quote.
_PEEK_TEXT_RE = re.compile(r'"(?:content|text)"\s*:\s*"(.*?)(?<!\\)"', re.S)


def _peeked_text(raw):
    """The assistant's visible text, pulled out of raw SSE frames.

    Deliberately regex rather than a JSON parse: these are partial frames from
    three different protocols, and a strict parse would throw away the very
    turns worth judging (the measured one was malformed JSON -- one '[' and no
    ']')."""
    out = []
    for m in _PEEK_TEXT_RE.finditer(raw or ""):
        try:
            out.append(json.loads('"%s"' % m.group(1)))
        except Exception:                                        # noqa: BLE001
            out.append(m.group(1))
    return "".join(out).strip()


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
      'error'   -> the 200 body carries an upstream error frame (a 403/quota refusal
                   delivered inside the stream); caller falls through to next model.
      'timeout' -> nothing usable arrived in time; caller falls through.
    Same daemon-worker discipline as _peek_first_chunk: the buffer/iterator are only
    used when the worker FINISHED (status set) so there's never concurrent iteration."""
    box = {"buf": [], "status": None}

    def _worker():
        buf = box["buf"]
        saw_reasoning = False
        seen_content = []
        try:
            for _ in range(max_lines):
                item = next(iterator)
                buf.append(item)
                if not item:
                    continue
                b = item if isinstance(item, (bytes, bytearray)) \
                    else str(item).encode("utf-8", "ignore")
                # A real tool call means the model is DOING the work. Commit at
                # once: nothing below can improve on that, and every extra frame
                # here is latency on the turns that are going well.
                if _STREAM_TOOLCALL_RE.search(b):
                    box["status"] = "content"
                    return
                if _STREAM_CONTENT_RE.search(b):
                    # LIVE-VERIFIED 2026-08-07: the api.airforce backend behind
                    # g4f ships its ENTIRE error in ONE content delta, so a
                    # per-frame check catches it before the stream is committed.
                    # This is the path Codex and Claude Code actually use, and
                    # the frame really is content -- _STREAM_ERROR_VALUE_RE below
                    # never fires because there is no "error" key anywhere.
                    if len(b) <= 1024 and _NONANSWER_RE.search(
                            b.decode("utf-8", "ignore")):
                        box["status"] = "nonanswer"
                        return
                    # KEEP READING instead of committing on the first delta.
                    #
                    # MEASURED 2026-08-31: every dead-turn detector the hub has
                    # -- a tool call typed out as prose, a turn that only
                    # announces work, a refusal -- hangs off _chat_json_nonanswer,
                    # which runs ONLY on non-streaming bodies. Codex and Claude
                    # Code both stream, so on the path that matters none of them
                    # ever ran. Committing here on the first content delta is why:
                    # at that moment there is no text to judge.
                    #
                    # So accumulate a little of the answer first. The failures
                    # being caught are short by nature (an announcement is one
                    # sentence; a refusal opens with one), while a turn that is
                    # really working reaches for a tool -- and that exits above
                    # without waiting at all. Bounded by _PEEK_JUDGE_CHARS and by
                    # the deadline this whole peek already runs under.
                    seen_content.append(b.decode("utf-8", "ignore"))
                    if sum(len(x) for x in seen_content) < _PEEK_JUDGE_CHARS:
                        continue
                    box["status"] = _judge_peeked(seen_content)
                    return
                # An error INSIDE a 200 stream (403/429/quota reported as an SSE
                # error frame instead of an HTTP status) — the single most common
                # way a provider dead-ends a turn. Report it so the caller drops
                # this hop and tries the next model instead of committing a stream
                # that will never produce an answer.
                if _STREAM_ERROR_VALUE_RE.search(b):
                    box["status"] = "error"
                    return
                if _STREAM_TERMINAL_RE.search(b):
                    box["status"] = "empty"
                    return
                if _STREAM_REASONING_RE.search(b):
                    saw_reasoning = True
            # Budget exhausted with no content marker. Only treat that as a real
            # stream if we saw the model THINKING (reasoning deltas) — otherwise it
            # was 400 lines of keepalives/role deltas and committing it hands the
            # CLI a stream that never answers.
            if seen_content:
                box["status"] = _judge_peeked(seen_content)
            else:
                box["status"] = "content" if saw_reasoning else "empty"
        except StopIteration:
            # The stream ENDED inside the peek window, so everything the model
            # was ever going to say is in hand -- the best possible moment to
            # judge it, and the shape a dead turn usually has.
            box["status"] = _judge_peeked(seen_content) if seen_content else "empty"
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


# A 200 whose "answer" IS the relay backend's error page.
#
# MEASURED LIVE 2026-08-07, reproduced twice with the hub's own g4f key:
#   model=srv_mp3lmkuad07322459f47:claude-opus-4-7  ->  HTTP 200,
#   finish_reason "stop", usage {"total_tokens": 399}, content =
#   "The model does not exist in https://api.airforce\ndiscord.gg/airforce"
#   -- character for character what the user reported, four separate times.
#
# g4f fronts ~42 donated backends; api.airforce answers an unknown model id
# with 200 + error-as-content instead of a 4xx. That is why FOUR previous
# fixes changed nothing: _DEAD_STATUSES, _maybe_mark_missing_model, the
# non-2xx _record_outcome(False) and the last_hard relay ALL live in the
# non-2xx branch, and this request never enters it -- the chain does not
# exhaust, it SUCCEEDS on hop 1 and returns. Worse, _record_chat_usage files
# every delivery as _record_outcome(..., True), so the reliability signal was
# PROMOTING the id while it kept its 138 claude-family score floor.
#
# Content is the only signal that exists here. Requiring the WHOLE message to
# be short is what makes it safe: a real model quoting "model does not exist"
# inside a genuine reply is never a <300-char complete answer, and a false
# positive costs only a fall-through to the next hop -- exactly what an empty
# 200 already pays.
_NONANSWER_MAX_CHARS = 300
_NONANSWER_RE = re.compile(
    r"model (?:'[^']*' )?does\s?n[o']?t exist"
    r"|requires an active subscription"
    r"|rate limit exceeded"
    r"|insufficient (?:credits|balance|quota)"
    r"|no cake credits"
    r"|discord\.gg/"
    # google/gemini-3.6-flash via g4f, MEASURED 2026-08-09: a Codex session
    # ships this AS THE STREAMED CONTENT (HTTP 200, no error status at all --
    # see _SOFT_400_CONTEXT_RE for the 400-status twin of this same phrasing),
    # so only the content-level nonanswer check (_peek_until_content) can
    # catch it; the soft-400 regex never runs because there is no 400 here.
    # Without this the hub committed the stream and relayed the provider's
    # own error text to Codex as if it were the real answer.
    r"|maximum prompt length", re.I)


def _is_upstream_nonanswer(text):
    """True if `text` is a COMPLETE, SHORT message that is an upstream service's
    error/branding rather than an answer."""
    s = (text or "").strip()
    return bool(s) and len(s) <= _NONANSWER_MAX_CHARS and bool(_NONANSWER_RE.search(s))


# A tool call TYPED OUT as prose instead of emitted as a tool_calls array.
# MEASURED 2026-08-30 from a reported build that "stopped and did not finish" --
# the last two agent turns of that conversation both ended like this:
#
#     I'll now check all the generated pages...
#     ```shell
#     shell_command
#     <arg_key>command</arg_key>
#     <arg_value>Get-Content robots.txt -Raw</arg_value>
#     </tool_call>
#
# No tool_calls array, so the CLI saw an ordinary prose message, had nothing to
# execute and ended the turn. Nothing ran, nothing was written, and the hub
# recorded a clean success -- so it learned nothing and picked the same model
# again next turn.
#
# Detected from the RESPONSE rather than from a per-model blacklist on purpose:
# _TOOL_DIALECT_MISMATCH's own comment already concedes a blacklist is
# whack-a-mole ("three runs found three DIFFERENT model-specific ways to
# fail"), and this catches models nobody has met yet.
# MEASURED 2026-08-31: the live failures had moved dialect. The last agent turn
# of the most recent session typed a fenced ```json block containing
# {"tool_calls": [{"name": "shell_command", "arguments": {...}}]} -- valid-
# looking JSON (actually malformed: one [ and no ]) with no XML anywhere, so all
# three XML patterns missed it. An earlier turn used an OPENING <tool_call> that
# was never closed, which the closing-tag pattern also missed.
_TEXT_TOOL_CALL_RES = (
    re.compile(r"</tool_call>", re.I),
    re.compile(r"<tool_call\b", re.I),
    re.compile(r"<arg_key>.*?</arg_key>", re.I | re.S),
    re.compile(r"<arg_value>.*?</arg_value>", re.I | re.S),
    # the JSON dialect, fenced or bare: a tool_calls / function_call structure
    # written into the CONTENT instead of emitted as a tool call
    re.compile(r'"tool_calls"\s*:\s*\[', re.I),
    re.compile(r'"function_call"\s*:\s*\{', re.I),
    re.compile(r'```(?:json|tool_code|python)?\s*\{\s*"(?:name|tool|command)"\s*:', re.I),
)


# A turn that ANNOUNCED work instead of doing it.
#
# REPORTED 2026-08-31 from a real /build conversation -- two turns in a row,
# both straight after an explicit "continue", both ending as "Finished":
#   "I'll now craft a premium editorial UI system..."      (177 chars)
#   "I am now hard-coding the pillar architecture..."      (118 chars)
# No tool calls at all. One burned 2m 27s to say it. Nothing was written, and
# the hub scored both as clean successes -- so it learned nothing and picked
# the same models again next turn.
#
# ACT's first bullet did not cover it: it opens "If you already called a tool
# this turn", and these called nothing. That hole is closed in craft.ACT_RUN
# too, but a prompt rule only works when the model reads it; this is the
# structural half.
#
# TENSE is the discriminator, and it has to be. "Done -- all 12 pages built" is
# a perfectly good short final answer with no tool calls, and "I'll now build
# the 12 pages" is the same length and a dead turn. So: a statement of INTENT
# to do work, with no question in it, on a turn that offered tools.
# MEASURED 2026-08-31 across the three most recent sessions: this caught 2 of 13
# agent turns. It was ^-anchored, so "Core helpers are ready. Now I'll write the
# content generator." -- a turn that ended right there -- did not match, because
# the intent phrase was not at position 0. It also only knew the ASCII
# apostrophe, so "I'm locking the architecture" with a curly U+2019 missed, and
# it had no gerund form, so "Now writing the complete landing page." (the whole
# 38-character turn) missed.
#
# Now: the intent phrase may open ANY sentence in a short message, both
# apostrophes count, and the bare gerund is included.
_INTENT_RE = re.compile(
    r"(?:^|(?<=[.!?])\s|(?<=\n))\W*"
    r"(?:i\s*['’]?\s*(?:ll|m)\b|i will|i am (?:now )?|i\s*['’]?m (?:now )?going to"
    r"|i\s*['’]?m (?:now )?about to|let me|now i\s*['’]?ll|next,? i\s*['’]?ll"
    r"|next,? i will|now (?:writing|creating|building|generating|adding|updating)"
    r"|proceeding to|moving (?:on )?to|starting (?:on|with))", re.I)
_WORK_VERB_RE = re.compile(
    r"\b(?:build\w*|rebuild\w*|creat\w*|writ\w*|add\w*|updat\w*|fix\w*|redesign\w*|design\w*"
    r"|implement\w*|generat\w*|craft\w*|hard-?cod\w*|refactor\w*|instal\w*|run|test\w*"
    r"|deploy\w*|styl\w*|check\w*|verif\w*|deliver\w*|produc\w*|set up|wire\w*"
    # measured misses: "I'm locking the architecture", "moving forward with"
    r"|lock\w*|scaffold\w*|assembl\w*|final(?:is|iz)\w*|batch\w*|wrap\w*"
    r"|mov\w+ forward|start\w*|continu\w*|proceed\w*)\b", re.I)
# An announcement is SHORT by nature -- it is a sentence about what comes next,
# not the work. A long answer that happens to open with "I'll walk through..."
# is a real deliverable and must not be touched.
_ANNOUNCE_MAX_CHARS = 400


# A model DECLINING the task. The fourth way a clean 200 turns out not to be an
# answer, after a relay's error page as content, a tool call typed out as prose,
# and a turn that only announced work.
#
# ASKED 2026-08-31: "if task can't be done with a model then he try with other
# model usually hy3 and deepseek v4 flash and qwen 3.8 flash they can do things
# that other models dont accept to do". A refusal was invisible to the hub: real
# text, HTTP 200, so the chain accepted it, the turn ended, and the ledger
# recorded a SUCCESS -- which meant the next turn picked the same model and got
# refused again.
#
# FALSE POSITIVES are the whole risk, so this is narrow by construction: a SHORT
# reply, no tool calls, that is essentially nothing but the refusal. "I can't
# find config.yml, so I created one" is a real answer and must survive.
_REFUSAL_RE = re.compile(
    r"\b(?:i\s*(?:'|’)?\s*(?:m\s+sorry[, ]*)?(?:can\s*(?:not|'?t)|cannot|will\s+not|won'?t|"
    r"am\s+(?:not\s+able|unable)|(?:'|’)?m\s+(?:not\s+able|unable))"
    r"\s+(?:be\s+able\s+)?(?:to\s+)?(?:help|assist|comply|creat\w*|provide|generat\w*"
    r"|do|write|make|continue|fulfil\w*|support|engage|produce|proceed)"
    r"|i\s+must\s+decline|i\s+have\s+to\s+decline"
    r"|as\s+an\s+ai(?:\s+language)?\s+model[, ].{0,40}\b(?:cannot|can'?t|unable))",
    re.I)
# MEASURED 2026-08-31 against three real refusals in a live session: they were
# 865, 4585 and 1404 characters. A refusal in practice is the refusal PLUS a
# paragraph explaining why and offering alternatives, so a short-message cap
# caught none of them. What is actually characteristic is WHERE it appears: a
# model that declines says so immediately, in its opening sentence. So the test
# is applied to the OPENING of the message, not the whole of it.
_REFUSAL_HEAD_CHARS = 400


def _looks_like_refusal(text):
    """True when `text` OPENS by declining the task rather than doing it."""
    if not text or not isinstance(text, str):
        return False
    head = text.strip()[:_REFUSAL_HEAD_CHARS]
    if not head:
        return False
    # The FIRST SENTENCE only. Scanning the whole head was tried and produced a
    # real false positive: "Built all 12 pages and wired the nav. I can't reach
    # the CDN, so the fonts are self-hosted." is a turn that did the work and
    # then named a limit, and the refusal phrase sat well inside 400 characters.
    # A model that is declining says so before it says anything else.
    first = re.split(r"(?<=[.!?])\s", head, 1)[0]
    if "?" in first:
        return False                  # asking is what it SHOULD do
    return bool(_REFUSAL_RE.search(first))


# The models the user named as accepting work others decline. Matched on the
# normalised identity, so every provider's spelling of them counts.
_PERMISSIVE_MODELS = ("hy3", "deepseek-v4-flash", "qwen3.8")


def _is_permissive(model_id):
    """True for a model known to take on work other models refuse."""
    ident = _normalize_model_identity(model_id)
    low = (model_id or "").lower()
    return any(p in ident or p in low for p in _PERMISSIVE_MODELS)


def _permissive_candidates():
    """Live (pid, model) pairs for the permissive models, best first."""
    out = []
    for pid in _available_providers():
        try:
            for m in provider_free_models(pid) or []:
                if _is_permissive(m) and not _is_model_dead(pid, m):
                    out.append((_benchmark_score(pid, m), pid, m))
        except Exception:                                        # noqa: BLE001
            continue
    out.sort(reverse=True)
    return [(pid, m) for _s, pid, m in out]


def _ensure_permissive_hop(chain):
    """Guarantee the fallback chain contains a model that accepts work others
    decline, so a refusal always has somewhere to go.

    Appended, never promoted: the ordinary ranking still decides who answers
    first, and this only matters once the models ahead of it have refused."""
    if not chain:
        return chain
    if any(_is_permissive(m) for _p, m in chain):
        return chain
    try:
        for pid, model in _permissive_candidates():
            if (pid, model) in chain:
                continue
            out = list(chain)
            # BEFORE the low-quality tail, not at the very end. _build_chain
            # deliberately parks demoted families (kimi-k2.x, nemotron, gpt-oss)
            # last as a last resort; appending past them would rank a normal
            # model behind a demoted one, which test_last_resort_routing exists
            # to prevent -- and it caught exactly that.
            at = len(out)
            for i, (_p, m) in enumerate(out):
                if _is_low_quality(m):
                    at = i
                    break
            out.insert(at, (pid, model))
            return out
    except Exception:                                            # noqa: BLE001
        pass
    return chain


def _looks_like_announced_not_acted(text):
    """True when `text` states an intention to do work rather than reporting
    work done or asking a question. See the note above."""
    if not text or not isinstance(text, str):
        return False
    body = text.strip()
    if not body or len(body) > _ANNOUNCE_MAX_CHARS:
        return False
    if "?" in body:
        return False                      # asking is what it SHOULD do
    return bool(_INTENT_RE.search(body) and _WORK_VERB_RE.search(body))


def _looks_like_text_tool_call(text):
    """True when `text` contains a tool call the model wrote out instead of
    emitting. Requires a real closing/paired marker, not a passing mention, so
    prose that merely talks about tools does not trip it."""
    if not text or not isinstance(text, str):
        return False
    return any(rx.search(text) for rx in _TEXT_TOOL_CALL_RES)


def _chat_json_nonanswer(data, has_tools=False, tools=None):
    """_chat_json_is_empty's sibling: a 200 that is not really an answer.

    Two shapes. The original one is a relay returning its own error page as
    content. The second is a tool call typed out as prose (see
    _looks_like_text_tool_call) -- only ever checked when the request actually
    offered tools, because in plain chat, explaining what a tool call looks
    like is a perfectly good answer.

    ...except that "typed it out" is not always a failed turn. When the text
    contains a COMPLETE call to a tool the client actually offered, the model
    did the work and only got the envelope wrong, so tool_rescue promotes it
    into real tool_calls and this stops being a non-answer at all. That saves
    the whole retry: before, such a turn marked a working model dead for the
    TTL and paid for a second inference to get an answer the hub was already
    holding. Only unparseable text, or a call to a tool that was never offered,
    still falls through to the old path -- an invented tool name would just make
    the agent loop fail further downstream.

    `tools` is the client's tools array, needed to check the name. Without it
    the rescue is skipped rather than guessed at."""
    try:
        msg = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
        if msg.get("tool_calls"):
            return False                      # a real tool call is a real answer
        if tools and tool_rescue.rescue(data, tools):
            return False                      # it WAS an answer, in the wrong envelope
        content = msg.get("content")
        if isinstance(content, list):
            content = "".join((p.get("text") or "") for p in content
                              if isinstance(p, dict))
        if has_tools and (_looks_like_text_tool_call(content)
                          or _looks_like_announced_not_acted(content)):
            return True
        # A refusal counts with or without tools: it is no more useful in plain
        # chat, and the hub has other models that will answer.
        if _looks_like_refusal(content):
            return True
        return _is_upstream_nonanswer(content)
    except Exception:
        return False


def _note_nonanswer(pid, model):
    """Both consequences in one hook: file the delivery failure the 200 branch
    never filed, and sideline this (pid, model) for _DEAD_MODEL_TTL. Dead-marking
    is the ONLY mechanism here that actually REMOVES an id from the chain
    (_build_chain checks _is_model_dead) rather than merely re-ordering it --
    which is why a reliability penalty alone could never have stopped this."""
    _record_outcome(pid, model, False)
    _mark_model_dead(pid, model, 404)         # 404 is in _DEAD_STATUSES


# --------------------------------------------------------------------------- #
# BUDGET STARVATION — a reasoning model that spent the whole max_tokens thinking
# and had nothing left to answer with.
#
# MEASURED 2026-07-31 across cerebras / openrouter / opencode-zen / kilocode /
# g4f-gemini, ~85 live calls at budgets 1..512:
#   * empty content + finish_reason == "length" occurred 49/49 times when the
#     model was starved. Never "stop", never null. That pair IS the signal.
#   * the CONVERSE is false: finish_reason == "length" WITH real content is
#     common (a plain truncation). So both halves must be checked -- gating on
#     finish_reason alone would retry ordinary truncated answers forever.
#   * the reasoning text is NOT a usable discriminator. It arrives under three
#     different names (reasoning / reasoning_content / reasoning_details) and
#     g4f-gemini starves with NO reasoning field at all -- its message is
#     literally {"role":"assistant"} -- so keying on "has reasoning" would
#     misclassify exactly the provider that most needs the retry.
# Raising the budget fixed every case on a normal prompt; it is not a universal
# cure (cerebras stayed empty at 512 on a hard word problem), so this retries
# ONCE and then falls through to the next hop as before.
# --------------------------------------------------------------------------- #
_STARVED_RETRY_CAP = 2048        # never ask for more than this on the retry
_STARVED_RETRY_FLOOR = 512       # ...and never less than this, or it starves again


def _chat_json_starved(data):
    """True when a 200 carried no content ONLY because the token budget ran out
    mid-reasoning. Caller must already know the body is empty."""
    try:
        return (((data.get("choices") or [{}])[0] or {}).get("finish_reason") or "") == "length"
    except Exception:                                                # noqa: BLE001
        return False


def _starved_retry_budget(requested):
    """A budget likely to leave room for an answer after the thinking, or None
    when the caller already asked for plenty (then starvation is the model's
    problem, not the budget's, and a retry would just burn a second call)."""
    try:
        cur = int(requested) if requested else 0
    except (TypeError, ValueError):
        cur = 0
    if cur >= _STARVED_RETRY_CAP:
        return None
    return max(_STARVED_RETRY_FLOOR, min(_STARVED_RETRY_CAP, cur * 4 or _STARVED_RETRY_FLOOR))


# A provider can dodge STREAM_IDLE_TIMEOUT forever by sending blank/keepalive
# SSE lines without ever delivering real content -- observed LIVE, same
# provider+model, twice: 2026-08-06 held a swarm hop hostage 24+ minutes (see
# _SWARM_HOP_DEADLINE, the fix for that non-streaming case); 2026-08-08 held a
# live Codex /v1/responses stream 600+s, ending only when the ACTIVITY-FEED
# janitor relabelled the row 'stalled' for the dashboard (_ACTIVITY_STALL_SECS)
# -- which never touches the real connection, so Codex just kept waiting the
# whole time. Bounds time since the LAST real chunk, not total stream
# duration, so a genuinely slow-but-progressing generation (a long tool-call
# argument assembling, a reasoning model thinking) is never cut off -- only a
# stream that has gone quiet except for keepalives is.
# Same value and meaning as _SWARM_HOP_DEADLINE below ("this hop stopped
# delivering") -- not a direct reference, since that constant is defined
# later in the file and this one is used by _proxy_sse right below.
_STREAM_PROGRESS_DEADLINE = 150
_SSE_BLANK_DATA_RE = re.compile(rb"^data:\s*(?:\[DONE\])?\s*$")


def _sse_chunk_is_progress(raw):
    """True if `raw` -- one line from iter_lines(), or one arbitrary chunk from
    iter_content() that may itself contain several newline-joined lines --
    carries real upstream data, not just a blank/keepalive SSE line or a bare
    [DONE]. See _STREAM_PROGRESS_DEADLINE."""
    if not raw:
        return False
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "ignore")
    for line in raw.split(b"\n"):
        line = line.strip()
        if line.startswith(b"data:") and not _SSE_BLANK_DATA_RE.match(line):
            return True
    return False


def _proxy_sse(resp, iterator=None, first=_MISSING, hop_pid=None, hop_model=None):
    """Pass upstream SSE bytes through unchanged. When `iterator`/`first` are
    supplied (the first-byte peek already pulled the first chunk from this exact
    iterator), yield that chunk first, then continue the SAME iterator — so the
    fast-path byte stream is byte-for-byte identical to before. Empty chunks are
    still filtered exactly as before.

    Also enforces _STREAM_PROGRESS_DEADLINE: a provider that goes quiet except
    for keepalives is cut off, recorded as a delivery failure (so it stops
    winning top slot), and the client gets a clean [DONE] instead of an SSE
    body that never ends."""
    saw_done = False
    last_progress = time.time()
    try:
        if iterator is None:
            iterator = resp.iter_content(chunk_size=None)
        for chunk in _chain_first(first, iterator):
            now = time.time()
            if _sse_chunk_is_progress(chunk):
                last_progress = now
            elif now - last_progress > _STREAM_PROGRESS_DEADLINE:
                _log.warning("[stream-stall] %s/%s: no real content for %ds, cutting off",
                            hop_pid, hop_model, _STREAM_PROGRESS_DEADLINE)
                _record_outcome(hop_pid, hop_model, False)
                if not saw_done:
                    yield b"data: [DONE]\n\n"
                    saw_done = True
                break
            if chunk:
                if not saw_done and _STREAM_TERMINAL_RE.search(
                        chunk if isinstance(chunk, (bytes, bytearray))
                        else str(chunk).encode("utf-8", "ignore")):
                    saw_done = True
                yield chunk
    except Exception as exc:
        # Upstream died mid-stream (reset / ChunkedEncodingError / read timeout).
        # Without a terminator the client sits on a half-open SSE body waiting for
        # a [DONE] that will never come. Byte passthrough is unchanged on the happy
        # path; this only appends the terminator the upstream failed to send.
        _log.error("SSE passthrough error: %s", _sanitize(str(exc)))
        if not saw_done:
            try:
                yield b"data: [DONE]\n\n"
            except Exception:
                pass
    finally:
        resp.close()


_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _classify_hop_error(exc=None, status=None, peek=None):
    """One-token class of why a chain hop failed, for the X-Free-LLM-Hub-Last-Error
    transparency header (timeout / conn / 413 / 429 / http-<n> / empty / error /
    non-json) — so 'why did the chain degrade' is one curl -i away instead of a
    log dig."""
    if exc is not None:
        if isinstance(exc, requests.Timeout):
            return "timeout"
        if isinstance(exc, requests.RequestException):
            return "conn"
        return "error"      # RuntimeError (no key/base_url, sub relay, ...)
    if status is not None:
        if status in (413, 429):
            return str(status)
        return "http-%d" % status
    return peek or "error"


def _routing_headers(pid, model, attempts, last_error=None):
    """Routing-transparency headers: which provider/model actually served the
    response (or was last tried, on a chain-exhausted error), how many
    upstream hops the chain burned, and the class of the LAST hop failure seen
    before this response ('none' when the first hop just worked — see
    _classify_hop_error). Debugging aid only — bodies stay untouched."""
    h = {"X-Free-LLM-Hub-Attempts": str(attempts),
         "X-Free-LLM-Hub-Last-Error": last_error or "none"}
    if pid:
        h["X-Free-LLM-Hub-Provider"] = str(pid)
    if model:
        h["X-Free-LLM-Hub-Model"] = str(model)
    return h


def _with_headers(resp_tuple, headers):
    """Attach extra headers to an (response, status) tuple. Best-effort; returns
    the input untouched on error."""
    try:
        resp, status = resp_tuple
        for k, v in headers.items():
            resp.headers[k] = v
        return resp, status
    except Exception:
        return resp_tuple


# --------------------------------------------------------------------------- #
# SWARM — the "several strong models build it together" virtual model.
# Selected explicitly (model: "swarm"); see swarm.py for why it is not automatic.
# CREWS — the same pipeline with specialised stage prompts (model: "crew-code",
# "crew-research", ...; bare "crew" auto-detects); see crews.py.
# --------------------------------------------------------------------------- #
_SWARM_IDS = ("swarm", "team", "plan")

# Total deadline for ONE swarm/crew stage hop. Swarm stages dispatch
# non-streaming, so they get neither the streaming first-byte peek (~25-90s)
# nor the inter-chunk idle timeout — only requests' per-recv read timeout
# (CHAT_READ_TIMEOUT). A provider that trickles keepalive bytes resets that on
# every byte and can hold a hop forever: OBSERVED LIVE 2026-08-06,
# tokenrouter/moonshotai/kimi-k3-free kept a stage hostage 24+ minutes (never
# answering stream:false), so a whole crew run produced nothing. CHAT_READ_TIMEOUT
# stays as the per-recv guard; this is the overall one. The abandoned thread
# is a daemon and its socket dies with the provider's connection — bounded by
# chain length, so the leak is small and self-cleaning.
_SWARM_HOP_DEADLINE = 150

# The TOOL fan-out gets its own, longer deadline. The 150s above was measured
# against prose pipeline STAGES -- short, self-contained, and many per run, so a
# tight per-stage bound is what keeps a whole crew from stalling. A CLI agent
# turn is a different workload wearing the same clothes: the full system prompt,
# every tool schema the CLI declares, and the whole conversation so far, all
# dispatched NON-STREAMING so the model must finish generating before a single
# byte comes back. On free-tier models that routinely passes 150s.
#
# MEASURED 2026-09-04: five consecutive opencode build turns, 152/156/157/162/
# 168 seconds, every one of them 0-of-5 members answering -- durations clustered
# just above the deadline, which is what a deadline looks like when it is the
# thing doing the killing.
#
# Raised, not removed: an unbounded fan-out can hold a turn open for as long as
# the slowest provider feels like trickling keepalives (the 24-minute hostage in
# the note above). And it is now safe to be generous, because missing it no
# longer fails the turn -- it falls through to single-model routing.
_SWARM_TOOL_HOP_DEADLINE = 300

# ...and how long to keep waiting for stragglers AFTER a member has produced a
# usable tool call.
#
# The fan-out used to wait for every member, so one that never answers cost the
# full deadline even when the turn was already answerable. MEASURED 2026-09-04
# on a real tool turn: three of five answered, at 5s, 78s and 111s -- and the
# request took 297 SECONDS, because the two that never answered burned the whole
# budget. The user waited three extra minutes for an answer that had been
# sitting there since 111s.
#
# The grace only starts once something has actually CALLED A TOOL, which is what
# an agent turn needs. A prose-only answer does not start it: waiting longer is
# exactly right while the only thing on the table is an answer the CLI cannot
# execute. Best-of-N still gets its N whenever N models are willing to be quick.
_SWARM_STRAGGLER_GRACE = 25


def _dispatch_chat_with_deadline(pid, payload, deadline=None):
    """_dispatch_chat(..., stream=False) bounded by an OVERALL deadline.
    Returns (resp, exc): resp is None on deadline or failure, exc is the
    raised exception (None on deadline). Never raises."""
    if deadline is None:
        deadline = _SWARM_HOP_DEADLINE
    box = {}

    def _call():
        try:
            box["resp"] = _dispatch_chat(pid, payload, False)
        except (requests.RequestException, RuntimeError) as exc:
            box["exc"] = exc

    t = threading.Thread(target=_call, daemon=True)
    t.start()
    t.join(deadline)
    if t.is_alive():
        return None, None            # hung hop — abandon and walk the chain on
    return box.get("resp"), box.get("exc")


def _crew_name_for(model):
    """None for a plain swarm id; otherwise the crew the id selects —
    bare "crew" -> "auto" (crews.detect_crew picks from the request text),
    "crew-code" / "crew/code" -> "code"."""
    m = (model or "").strip().lower()
    if m == "crew":
        return "auto"
    if m.startswith("crew-"):
        return m[len("crew-"):]
    if m.startswith("crew/"):
        return m[len("crew/"):]
    return None


def _is_swarm_model(model):
    m = (model or "").strip().lower()
    if m in _SWARM_IDS or m.startswith("swarm/"):
        return True
    if m in crews.CREW_IDS:
        return True
    return m.startswith("crew/") and ("crew-" + m[len("crew/"):]) in crews.CREW_IDS


def _swarm_dispatch(messages, max_tokens, exclude_pids=()):
    """One stage of the pipeline, routed and executed through the SAME chain
    every other request uses (so fallback, key rotation, quota accounting and
    the activity trail all behave identically). Returns (text, 'pid/model');
    never raises — an empty text tells swarm.py that stage failed."""
    try:
        est = _est_tokens(messages)
        # force_difficulty="hard": every swarm stage is creation work and must
        # take the strongest model, whatever the text alone would classify as.
        pid, model, _d = _route_by_difficulty(messages, max_tokens, est,
                                              require_tools=False,
                                              force_difficulty="hard")
        if not pid:
            return "", None
        best_partial, best_partial_who = "", None
        for hop_pid, hop_model in _build_chain(pid, model, est):
            if exclude_pids and hop_pid in exclude_pids:
                continue     # reviewer must not be the provider that wrote it
            payload = {"model": hop_model, "stream": False,
                       "max_tokens": max_tokens, "messages": messages,
                       "_no_craft": True}   # stripped in _upstream_chat
            resp, hop_exc = _dispatch_chat_with_deadline(hop_pid, payload,
                                                         _SWARM_HOP_DEADLINE)
            if resp is None:         # hung hop (deadline) or a failed one
                _record_outcome(hop_pid, hop_model, False)
                continue
            try:
                if resp.status_code != 200:
                    _record_outcome(hop_pid, hop_model, False)
                    continue
                data = resp.json() or {}
            except ValueError:
                continue
            finally:
                try:
                    resp.close()
                except Exception:                                # noqa: BLE001
                    pass
            choice = (data.get("choices") or [{}])[0]
            text = ((choice.get("message") or {}).get("content") or "").strip()
            if text and choice.get("finish_reason") == "length":
                # Truncated by the PROVIDER's completion cap (observed live:
                # kilocode/hy3 cut a synthesis mid-attribute, shipping broken
                # HTML). Walking the chain costs one hop; a cut-off artefact is
                # what the user receives otherwise. Keep the longest truncation
                # as the stage result if EVERY hop truncates.
                if len(text) > len(best_partial):
                    best_partial = text
                    best_partial_who = "%s/%s" % (hop_pid, hop_model)
                continue
            if text and _is_upstream_nonanswer(text):
                # A relay's error page delivered as a 200 (see
                # _chat_json_nonanswer). Without this a swarm stage happily
                # synthesises on top of "The model does not exist in
                # https://api.airforce" as if it were real stage output.
                _note_nonanswer(hop_pid, hop_model)
                continue
            if text:
                # Same success hook as every other endpoint: usage accounting
                # AND the reliability record, so a stage's healthy hop is what
                # the next stage's chain prefers (and a hung hop sinks).
                _record_chat_usage(hop_pid, hop_model, data, est)
                _act_pick(hop_pid, hop_model)
                return text, "%s/%s" % (hop_pid, hop_model)
        if best_partial:
            return best_partial, best_partial_who
    except Exception:                                            # noqa: BLE001
        _log.debug("[swarm] stage failed", exc_info=True)
    return "", None


# How many distinct models attempt a tool-carrying swarm turn in parallel.
# Each one is a full model call, so this multiplies the cost of every turn --
# 3 is enough for a real best-of, and small enough that a free tier survives a
# long agent run.
# ASKED 2026-08-31: "he can use more then 3 agents in the swarm". Five by
# default, settable per install. What makes a bigger swarm safe rather than
# reckless is the budget check in _swarm_rank -- more slots on a drained
# provider would just finish it off faster.
_SWARM_TOOL_FANOUT = 5
_SWARM_FANOUT_MAX = 8           # past this the quota cost outruns the benefit


def _swarm_fanout():
    """How many models answer one swarm turn. Clamped: a nonsense setting must
    not spend eight times the quota by accident, nor drop the swarm to zero."""
    try:
        n = int(config.get_setting("swarm_fanout", _SWARM_TOOL_FANOUT))
    except (TypeError, ValueError):
        n = _SWARM_TOOL_FANOUT
    return max(1, min(n, _SWARM_FANOUT_MAX))
# How deep into the chain to look before ranking. Bigger than the fan-out on
# purpose: with only three candidates there is nothing to choose BETWEEN, which
# is exactly how the swarm ended up filling half its slots with models that
# never answer (see _swarm_rank).
_SWARM_TOOL_CANDIDATES = 12
# Below this measured delivery rate a model is demoted out of a swarm slot.
# _reliability is Laplace smoothed and returns a flat 0.5 when unknown, so this
# only ever catches a REAL record: 0 of 3 answered is (0+1)/(3+2) = 0.2, while
# a single unlucky failure is (0+1)/(1+2) = 0.33 -- deliberately above a lone
# bad hop's reach once one success exists ((1+1)/(2+2) = 0.5).
_SWARM_MIN_RELIABILITY = 0.4
# Below this fraction of a provider's daily budget, it keeps its remaining calls
# for the single-model path rather than spending them on one slot of a fan-out.
_SWARM_MIN_HEADROOM = 0.10


# A provider needs a real record before it says anything about a model nobody
# has tried. Two failures is a bad afternoon; this many is a pattern.
_PROVIDER_PRIOR_MIN_SAMPLES = 6


def _provider_outcome_totals(pid):
    """Every recorded outcome for one provider, summed. None when it has no
    fresh record at all."""
    ok = fail = 0
    found = False
    now = time.time()
    with _outcome_lock:
        for (p, _m), rec in _outcomes.items():
            if p != pid or now - rec.get("last", 0) > _OUTCOME_TTL:
                continue
            found = True
            ok += rec.get("ok", 0)
            fail += rec.get("fail", 0)
    return {"ok": ok, "fail": fail} if found else None


def _swarm_reliability(pid, model):
    """Delivery rate for a swarm slot, with a PROVIDER-level prior behind it.

    MEASURED 2026-08-30, after ranking by _reliability alone shipped: a swarm
    turn came back 2-of-3 dead anyway, and one dead member was
    'g4f/RelayRouter:gemini-3.7-flash-free' -- an id the ledger had never seen,
    on a provider it had already measured at 0 successes in 16 tries under four
    OTHER ids. A relay fronts the same model once per backend
    ('srv_msjk...:gemini-3.6-flash', 'AnyProvider:gemini-3.6-flash',
    'RelayRouter:gemini-3.7-flash-free', ...), so every listing is a separate
    key and the hub relearns the same lesson forever, one id at a time.

    So: this exact model's own record when it has one, otherwise the provider's
    -- and a flat neutral 0.5 when neither has enough evidence to say anything.
    Deliberately SWARM-ONLY. Ordinary routing keeps judging a model purely on
    its own history, because there a bad hop costs one retry, while here it
    costs a whole slot in a fan-out that only has three."""
    own = _reliability(pid, model)
    if own != 0.5:
        return own                      # it has its own record; use it
    totals = _provider_outcome_totals(pid)
    if not totals:
        return 0.5
    ok, fail = totals["ok"], totals["fail"]
    if ok + fail < _PROVIDER_PRIOR_MIN_SAMPLES:
        return 0.5
    return (ok + 1.0) / (ok + fail + 2.0)


def _swarm_rank(cands):
    """Order swarm candidates by what actually DELIVERS, and push the ones with
    a real record of not answering to the back.

    _build_chain hands its entries back in raw-benchmark order. That is right
    for a FALLBACK chain -- try the best, drop to the next when it fails -- and
    wrong for a swarm, where every member runs at once and one that will not
    answer is pure waste. Hops 2 and 3 of a fallback chain are, by construction,
    the entries the hub already ranks lower and trusts less.

    MEASURED 2026-08-30 over 24 real swarm slots: 12 came back "no answer", and
    not evenly -- g4f went 0-for-3 and nvidia 4-for-11 while google and glm
    answered 8 of 9. The hub was already recording that (_record_outcome on
    every hop, read back by _reliability); the swarm just never asked.

    Two orderings, in this order:
      1. models with a real record of failing go last (never dropped outright --
         a smaller swarm is worse than a member that might not answer, and the
         fan-out is the whole point);
      2. within that, _agentic_score decides -- the same key agentic routing
         already uses, so strength still wins between two healthy models.
    Then providers are spread across the slots, because one provider having a
    bad minute must not take the entire swarm down with it."""
    if not cands:
        return []
    fanout = _swarm_fanout()
    ranked = sorted(cands, reverse=True,
                    key=lambda pm: _agentic_score((_benchmark_score(pm[0], pm[1]),
                                                   pm[0], pm[1])))
    healthy = [pm for pm in ranked
               if _swarm_reliability(pm[0], pm[1]) >= _SWARM_MIN_RELIABILITY]
    weak = [pm for pm in ranked if pm not in healthy]
    ordered = healthy + weak
    # BUDGET. Asked for directly -- "always in the range of best models to dont
    # exaust good ones quickly". A five-slot swarm aimed at one nearly-drained
    # provider finishes it off in a single turn, and it is precisely the
    # providers carrying the best models that are worth protecting. Same signal
    # the single-model router already uses (_quota_headroom); applied only when
    # there is somewhere else to go, because refusing to answer is worse than
    # spending the last of a budget.
    # DEMOTED, not dropped: a drained provider goes to the back of the queue and
    # is used only if there are not enough others to fill the slots. An
    # all-or-nothing filter was tried first and was worse -- with four healthy
    # candidates and a fan-out of five it gave up and let the drained provider
    # take TWO slots, which is the exact outcome this exists to prevent.
    afford = [pm for pm in ordered if _quota_headroom(pm[0]) > _SWARM_MIN_HEADROOM]
    drained = [pm for pm in ordered if pm not in afford]
    ordered = afford + drained

    picks, used = [], set()
    for pm in ordered:                      # one pass preferring a fresh provider
        if pm[0] not in used:
            picks.append(pm)
            used.add(pm[0])
            if len(picks) >= fanout:
                return picks
    for pm in ordered:                      # then top up, repeats allowed
        if pm not in picks:
            picks.append(pm)
            if len(picks) >= fanout:
                break
    return picks


def _swarm_tool_result(body):
    """Swarm for a TOOL-CALLING turn: run the same request on several strong
    models AT ONCE and return the best single response.

    The multi-phase pipeline (planner -> workers -> reviewer) cannot serve a CLI
    agent: it emits finished prose, never tool calls, so a coding agent driven
    by it would write no files at all. This is the shape of "several models work
    on it together" that actually survives an agent loop -- every candidate
    answers the real request with the real tools, so whatever wins is a normal,
    complete response the CLI can execute.

    Returns (data, headers) -- `data` is an ordinary OpenAI chat completion --
    or None when nothing usable came back, so the caller can fall through to
    ordinary single-model routing. Never raises.

    PROTOCOL-INDEPENDENT on purpose. This used to return a finished Flask
    response, which welded the whole fan-out to /v1/chat/completions -- and
    that is not the endpoint the CLIs use (codex speaks /v1/responses, claude
    /v1/messages), so the mode 404'd on a model literally named "swarm" for two
    of the three. Every protocol now runs this same race and translates the
    winner into its own shape.
    """
    messages = body.get("messages") or []
    # OPENING turn of a swarm session: ask for the phased plan up front. The
    # prose pipeline plans this way on its own (swarm._waves runs independent
    # phases concurrently), but a CLI drives its own loop, so it has to be
    # asked. Opening turn only -- re-sending it mid-task invites replanning
    # work that is already done. Done HERE because this is the last place the
    # REQUESTED model is still "swarm"; by the time the brief injector runs,
    # payload["model"] is the hop model and the mode is no longer visible.
    users = [m for m in messages if isinstance(m, dict) and m.get("role") == "user"]
    mid_loop = (len(users) != 1
                or any(isinstance(m, dict)
                       and (m.get("role") == "tool" or m.get("tool_calls"))
                       for m in messages))
    # The phased plan used to be injected HERE, and only here -- which meant it
    # existed in swarm mode alone while Normal and Max got none. It now ships
    # from craft.system_message() for every tool-carrying opening turn, and a
    # swarm tool turn goes through that same injector (it carries `tools` and
    # never sets _no_craft), so adding it again here would just buy a second
    # copy in the one mode that pays for its context three times over.
    est = _est_tokens(messages, body.get("tools"))
    pid, resolved, _d = _route_by_difficulty(messages, body.get("max_tokens"), est,
                                             require_tools=True,
                                             force_difficulty="hard")
    if not pid:
        return None
    # Distinct MODELS, not distinct listings: three copies of one model relayed
    # by one provider is not a second opinion, it is the same opinion three
    # times at three times the cost.
    cands, seen = [], set()
    for hop_pid, hop_model in _build_chain(pid, resolved, est, require_tools=True,
                                           messages=messages):
        ident = _normalize_model_identity(hop_model)
        if ident in seen:
            continue
        seen.add(ident)
        cands.append((hop_pid, hop_model))
        if len(cands) >= _SWARM_TOOL_CANDIDATES:
            break
    # Rank by delivery, don't just take the top of the chain -- see _swarm_rank.
    picks = _swarm_rank(cands)
    if not picks:
        return None

    def _run(pair):
        hop_pid, hop_model = pair
        payload = dict(body)
        payload["model"] = hop_model
        payload["stream"] = False        # fan-out cannot stream; re-emitted below
        resp, _exc = _dispatch_chat_with_deadline(hop_pid, payload,
                                                  _SWARM_TOOL_HOP_DEADLINE)
        if resp is None:
            _record_outcome(hop_pid, hop_model, False)
            return None
        try:
            if resp.status_code != 200:
                _record_outcome(hop_pid, hop_model, False)
                return None
            data = resp.json() or {}
        except (ValueError, AttributeError):
            return None
        finally:
            try:
                resp.close()
            except Exception:                                    # noqa: BLE001
                pass
        msg = ((data.get("choices") or [{}])[0].get("message") or {})
        if not (msg.get("tool_calls") or (msg.get("content") or "").strip()):
            _record_outcome(hop_pid, hop_model, False)
            return None
        # A member that TYPED its tool call instead of emitting one looks like
        # ordinary prose, so without this it competes as a candidate answer --
        # and if it wins, the CLI executes nothing and the build stops. That is
        # exactly the reported failure; see _looks_like_text_tool_call.
        # Same rescue as the single-model path: a member that typed a complete,
        # offered call did the work, and discarding it here would throw away a
        # usable answer in a mode that already paid for N inferences.
        if not msg.get("tool_calls") and body.get("tools"):
            if tool_rescue.rescue(data, body.get("tools")):
                msg = ((data.get("choices") or [{}])[0].get("message") or {})
        # A REFUSAL is not an answer either, and this is how the reported build
        # actually died. MEASURED 2026-09-04, 02:51:34: "1/5 models answered, 0
        # used a tool" -- the one member that replied wrote "Blocked. Every tool
        # call needs approval ... run `/permissions` and allow Write, Edit,
        # Bash(python:*)". The session was OPENCODE, which has no /permissions
        # command, no Edit tool and no Bash(...) syntax: the model invented a
        # Claude Code refusal wholesale. Nothing rejected it, so it won the slot
        # and became the whole turn's answer, and the user read it as the hub
        # denying permissions.
        #
        # _chat_json_nonanswer has treated a refusal as a non-answer on the
        # single-model path for a while; the fan-out simply never asked.
        if not msg.get("tool_calls") and _looks_like_refusal(msg.get("content")):
            _note_nonanswer(hop_pid, hop_model)
            return None
        if not msg.get("tool_calls") and (
                _looks_like_text_tool_call(msg.get("content"))
                or _looks_like_announced_not_acted(msg.get("content"))):
            # MEASURED: the reported dead turn was swarm mode -- three models
            # ran, none called a tool, and the best-ranked ANNOUNCEMENT won the
            # slot, so the CLI executed nothing and the build said Finished.
            _note_nonanswer(hop_pid, hop_model)
            return None
        _record_chat_usage(hop_pid, hop_model, data, est)
        return (hop_pid, hop_model, data, msg)

    # `ex.map` waited for EVERY member, which is why a turn cost the slowest one
    # even after the answer was in hand. Collect as they finish instead, and
    # once a tool call has arrived give the rest a short grace before moving on.
    #
    # NOT a `with` block: ThreadPoolExecutor.__exit__ calls shutdown(wait=True),
    # which would join the very threads this is trying to stop waiting for and
    # undo the whole change. Abandoned members are bounded by their own hop
    # deadline and their sockets die with the provider's connection -- the same
    # trade-off _dispatch_chat_with_deadline already documents.
    results = []
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=len(picks))
    try:
        pending = {ex.submit(_run, pm) for pm in picks}
        deadline = time.monotonic() + _SWARM_TOOL_HOP_DEADLINE
        cutoff = deadline
        while pending:
            remaining = cutoff - time.monotonic()
            if remaining <= 0:
                break
            done, pending = concurrent.futures.wait(
                pending, timeout=remaining,
                return_when=concurrent.futures.FIRST_COMPLETED)
            if not done:
                break                      # nothing landed before the cutoff
            for fut in done:
                try:
                    r = fut.result()
                except Exception:                                # noqa: BLE001
                    r = None
                if r:
                    results.append(r)
            if cutoff == deadline and any((r[3] or {}).get("tool_calls")
                                          for r in results):
                cutoff = min(deadline, time.monotonic() + _SWARM_STRAGGLER_GRACE)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    if not results:
        # LOG IT. The success line below sits after this early return, so a run
        # where NOTHING answered used to leave no trace at all -- five failing
        # turns in a row produced an empty log and a 503 with no model recorded,
        # which is the hardest possible shape to diagnose. The failing case is
        # the one worth a line.
        _log.warning("[swarm-tools] 0/%d models answered in %ds -> %s",
                     len(picks), _SWARM_TOOL_HOP_DEADLINE,
                     ", ".join(p + "/" + m for p, m in picks))
        return None

    # WINNER. A turn that needs an action is served by a model that took one:
    # prefer a response carrying tool_calls, because a model answering in prose
    # where the others reached for a tool has done strictly less of the job.
    # Within either group, the better-ranked model wins.
    acted = [r for r in results if (r[3].get("tool_calls"))]
    pool = acted or results
    hop_pid, hop_model, data, _msg = max(
        pool, key=lambda r: _benchmark_score(r[0], r[1]))
    _log.info("[swarm-tools] %d/%d models answered, %d used a tool -> %s/%s",
              len(results), len(picks), len(acted), hop_pid, hop_model)
    # Show the whole race in the activity feed, not just the survivor. The
    # existing pipeline chips already render (role, model) pairs for the prose
    # swarm, so reuse them: every model that was asked appears, labelled with
    # what it did, and the one that was actually served is marked. Without this
    # a swarm turn looks identical to an ordinary single-model turn -- three
    # models' worth of quota spent, one model's worth of evidence.
    try:
        answered = {(r[0], r[1]) for r in results}
        acted_set = {(r[0], r[1]) for r in acted}
        rows = []
        for p_id, m_id in picks:
            if (p_id, m_id) == (hop_pid, hop_model):
                role = "winner"
            elif (p_id, m_id) in acted_set:
                role = "used a tool"
            elif (p_id, m_id) in answered:
                role = "answered"
            else:
                role = "no answer"
            rows.append({"role": role, "model": p_id + "/" + m_id})
        act = getattr(g, "act", None)
        if act is not None:
            with _activity_lock:
                act["crew"] = "swarm (parallel)"
                act["pipeline"] = rows
    except Exception:                                            # noqa: BLE001
        pass
    data["model"] = hop_pid + "/" + hop_model
    hdrs = _routing_headers(hop_pid, hop_model, len(picks), None)
    return data, hdrs


def _swarm_stream_chunks(data):
    """The finished answer as the chat.completion.chunk objects a stream would
    have carried: one content/tool_calls delta, then the finish_reason. The
    fan-out has already completed by the time anything streams, so there is
    nothing to interleave -- but a CLI still expects stream shape, and a
    tool_calls delta that goes missing here is a turn that writes no files."""
    out_msg = ((data.get("choices") or [{}])[0].get("message") or {})
    delta = {"role": "assistant"}
    if out_msg.get("content"):
        delta["content"] = out_msg["content"]
    if out_msg.get("tool_calls"):
        delta["tool_calls"] = out_msg["tool_calls"]
    base = {"id": data.get("id") or ("chatcmpl-swarm-" + uuid.uuid4().hex),
            "object": "chat.completion.chunk",
            "created": data.get("created") or int(time.time()),
            "model": data.get("model") or "swarm"}
    yield dict(base, choices=[{"index": 0, "delta": delta, "finish_reason": None}])
    fin = (data.get("choices") or [{}])[0].get("finish_reason") or "stop"
    yield dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": fin}])


def _swarm_sse_lines(data):
    """The same chunks as the RAW BYTE LINES an upstream OpenAI SSE stream would
    have produced. _responses_stream and _anthropic_stream both already accept a
    line_iter (it exists for the first-byte peek), and both use `resp` for
    nothing else -- so replaying the swarm's answer through them gives codex and
    claude a correctly-shaped stream without a second translator per protocol."""
    for chunk in _swarm_stream_chunks(data):
        yield b"data: " + json.dumps(chunk).encode("utf-8")
        yield b""
    yield b"data: [DONE]"


def _swarm_tool_turn(body):
    """The /v1/chat/completions wrapper around _swarm_tool_result."""
    out = _swarm_tool_result(body)
    if out is None:
        return None
    data, hdrs = out
    if not body.get("stream"):
        return (jsonify(data), 200, hdrs)

    def _one_shot():
        for chunk in _swarm_stream_chunks(data):
            yield "data: %s\n\n" % json.dumps(chunk)
        yield "data: [DONE]\n\n"
    return Response(_one_shot(), mimetype="text/event-stream", headers=hdrs)


class _ReplayUpstream:
    """Stands in for the upstream HTTP response when replaying an ALREADY
    FINISHED answer -- the swarm's -- through one of the per-protocol SSE
    translators. They read raw byte lines and, at the very end, close() the
    upstream; there is no connection here to close, and passing None instead
    crashed the stream after every event had already been emitted."""

    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self, decode_unicode=False):
        return iter(self._lines)

    def close(self):
        pass


def _swarm_as_responses(body, messages, tools, est):
    """The swarm's answer in the Responses shape codex speaks, or None.

    Streaming replays the finished answer through _responses_stream, the same
    translator a real upstream stream goes through -- it takes a line_iter (it
    exists for the first-byte peek) and uses `resp` for nothing else, so there
    is no second translator to keep in step."""
    out = _swarm_for(body, messages, tools, body.get("max_output_tokens"))
    if out is None:
        return None
    data, hdrs = out
    label = (body.get("model") or "").strip() or data.get("model") or "swarm"
    if not body.get("stream"):
        return jsonify(_chat_to_responses(data, label)), 200, hdrs
    return Response(stream_with_context(
        _responses_stream(_ReplayUpstream(_swarm_sse_lines(data)), label)),
        mimetype="text/event-stream", headers=dict(_SSE_HEADERS, **hdrs))


def _swarm_as_anthropic(body, oai_messages, tools):
    """The swarm's answer in the Anthropic shape claude speaks, or None."""
    out = _swarm_for(body, oai_messages, tools, body.get("max_tokens"))
    if out is None:
        return None
    data, hdrs = out
    label = (body.get("model") or "").strip() or data.get("model") or "swarm"
    if not body.get("stream"):
        return jsonify(_openai_resp_to_anthropic(data, label)), 200, hdrs
    return Response(stream_with_context(
        _anthropic_stream(_ReplayUpstream(_swarm_sse_lines(data)), label,
                          _est_tokens(oai_messages, tools))),
        mimetype="text/event-stream", headers=dict(_SSE_HEADERS, **hdrs))


def _swarm_for(body, messages, tools, max_tokens):
    """Run the tool-turn fan-out for a NON-chat-completions endpoint.

    Returns (data, headers) exactly as _swarm_tool_result does, or None when
    nothing usable came back. The caller translates `data` into its own wire
    shape -- for a stream, by feeding _swarm_sse_lines(data) to the SSE
    translator it already has."""
    return _swarm_tool_result({"messages": messages, "tools": tools,
                               "model": body.get("model"),
                               "max_tokens": max_tokens,
                               "stream": bool(body.get("stream"))})


def _quality_route_kwargs(model, has_images):
    """quality_mode=True for the 'best' id, the same test /v1/chat/completions
    makes. Vision routing has its own model pool and no quality tier, so it is
    left alone there, matching the chat endpoint exactly."""
    if not has_images and (model or "").strip().lower() == "best":
        return {"quality_mode": True}
    return {}


def _swarm_completion(body):
    """Run the swarm (or a crew) and return ONE ordinary chat-completions response."""
    if body.get("tools"):
        # A tool-carrying turn cannot use the prose pipeline (it emits no tool
        # calls, so an agent driven by it writes nothing). Run the parallel
        # best-of-N form of "several models work on it together" instead, which
        # every CLI can actually execute. Falling through to ordinary routing
        # when nothing usable comes back is deliberate: a swarm that cannot
        # answer must not take the turn down with it.
        out = _swarm_tool_turn(body)
        if out is not None:
            return out
        # ...and this is what "falling through" has to mean, because it did not.
        #
        # REPORTED 2026-09-04: five consecutive /agent build turns from opencode
        # came back 503, each after ~155s, with no model recorded. The comment
        # directly above already promised the fall-through, and
        # _swarm_tool_result's docstring promises it twice ("so the caller can
        # fall through to ordinary single-model routing") -- but the code
        # returned a hard 503 instead, so choosing Swarm made a turn STRICTLY
        # more likely to die than choosing Normal. The better mode was the
        # riskier one.
        #
        # 'best', not 'auto': asking for Swarm is asking for the strongest
        # models available, and that intent should survive the fan-out failing.
        _log.info("[swarm-tools] nothing usable from the fan-out -> "
                  "falling back to single-model routing")
        fallback = dict(body)
        fallback["model"] = "best"
        return _chat_completions_uncached(fallback)
    messages = body.get("messages") or []
    if not messages:
        return _openai_error("messages is required.", 400)
    asked = (body.get("model") or "").strip()
    crew = _crew_name_for(asked)
    # Feed live stage progress + the per-role model list to the activity row.
    _watch = _act_pipeline_watcher()
    if crew is not None:
        result = crews.run(messages, _swarm_dispatch, crew, on_event=_watch)
        text = crews.format_answer(result)
    else:
        result = swarm.run(messages, _swarm_dispatch, on_event=_watch)
        text = swarm.format_answer(result)
    _act_pipeline_result(result)
    if not text:
        # Same reasoning as the tool path above: a pipeline that produced
        # nothing must not be the reason the user gets no answer at all. One
        # more single-model attempt costs far less than the fan-out that just
        # failed, and it is the difference between a reply and a 503.
        _log.info("[swarm] pipeline produced no text -> falling back to "
                  "single-model routing")
        fallback = dict(body)
        fallback["model"] = "best"
        return _chat_completions_uncached(fallback)
    out = {
        "id": "chatcmpl-swarm-" + uuid.uuid4().hex,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": asked,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": text}}],
    }
    if body.get("stream"):
        # A pipeline cannot stream token-by-token; emit the finished answer as
        # one chunk rather than refusing the request (same trade-off the sub-*
        # relay hops already make).
        def _one_shot():
            chunk = {"id": out["id"], "object": "chat.completion.chunk",
                     "created": out["created"], "model": out["model"],
                     "choices": [{"index": 0, "delta": {"role": "assistant",
                                                        "content": text},
                                  "finish_reason": None}]}
            yield "data: %s\n\n" % json.dumps(chunk)
            done = {"id": out["id"], "object": "chat.completion.chunk",
                    "created": out["created"], "model": out["model"],
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            yield "data: %s\n\n" % json.dumps(done)
            yield "data: [DONE]\n\n"
        return Response(_one_shot(), mimetype="text/event-stream")
    return jsonify(out)


# ---------------------------------------------------------------------------
# Foreign wire formats.
#
# Gemini CLI, the google-genai SDKs, Open WebUI, Enchanted, Continue's ollama
# provider, editor ghost-text autocomplete -- none of them can be pointed at an
# OpenAI endpoint, so without these the hub simply does not exist for any of
# them. Each one translates its request into an OpenAI body, calls the shared
# _chat_completions seam, and translates the answer back, so all of them inherit
# difficulty routing, the fallback chain, swarm/crew escalation, quota
# accounting and auto-compaction for free -- and keep inheriting improvements
# made after they were written.
# ---------------------------------------------------------------------------

def _call_router(openai_body):
    """Run a translated request through the real router.

    Returns (response, data, status). `data` is the parsed JSON for a buffered
    answer and None for a streamed one, in which case the caller iterates
    `response` itself."""
    rv = _chat_completions(openai_body)
    resp = app.make_response(rv)
    if resp.mimetype == "text/event-stream":
        return resp, None, resp.status_code
    try:
        return resp, resp.get_json(), resp.status_code
    except Exception:                                            # noqa: BLE001
        return resp, None, resp.status_code


def _sse_deltas(resp):
    """Yield (text, tool_calls, finish_reason, usage) from an OpenAI SSE stream.

    Every foreign streaming surface needs exactly this and nothing more, so the
    frame parsing lives here once rather than three times."""
    buf = ""
    for raw in resp.response:
        buf += raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        while "\n\n" in buf:
            frame, buf = buf.split("\n\n", 1)
            for line in frame.splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    obj = json.loads(payload)
                except ValueError:
                    continue
                choice = (obj.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                yield (delta.get("content") or "", delta.get("tool_calls"),
                       choice.get("finish_reason"), obj.get("usage"))


def _error_text(data, fallback):
    """Pull a human message out of whatever error envelope came back."""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(err, str):
            return err
    return fallback


# --------------------------------------------------------------------------- #
# Legacy text completions -- what editor ghost-text autocomplete still speaks
# --------------------------------------------------------------------------- #

@app.route("/v1/completions", methods=["POST"])
def v1_completions():
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return _openai_error("Invalid JSON body.", 400)
    prompt = body.get("prompt")
    if isinstance(prompt, list):
        # The spec allows an array (and token arrays, which no free provider
        # accepts); join the string case and refuse the rest rather than
        # silently completing the wrong text.
        if not all(isinstance(x, str) for x in prompt):
            return _openai_error("Only string prompts are supported.", 400)
        prompt = "".join(prompt)
    if not isinstance(prompt, str) or not prompt:
        return _openai_error("'prompt' is required.", 400)

    openai_body = {"model": body.get("model") or "auto",
                   "messages": [{"role": "user", "content": prompt}]}
    for k in ("temperature", "top_p", "max_tokens", "stop", "seed", "stream", "user"):
        if body.get(k) is not None:
            openai_body[k] = body[k]

    if body.get("stream"):
        resp, _data, _status = _call_router(openai_body)
        if resp.mimetype != "text/event-stream":
            return resp                                   # an error, already shaped
        model = body.get("model") or "auto"
        cid = "cmpl-" + uuid.uuid4().hex

        def gen():
            for text, _tc, finish, _u in _sse_deltas(resp):
                if not text and not finish:
                    continue
                yield "data: " + json.dumps({
                    "id": cid, "object": "text_completion",
                    "created": int(time.time()), "model": model,
                    "choices": [{"text": text, "index": 0,
                                 "finish_reason": finish, "logprobs": None}],
                }) + "\n\n"
            yield "data: [DONE]\n\n"
        return Response(stream_with_context(gen()), mimetype="text/event-stream",
                        headers=_SSE_HEADERS)

    resp, data, status = _call_router(openai_body)
    if status != 200 or not isinstance(data, dict):
        return resp
    choice = (data.get("choices") or [{}])[0]
    return jsonify({
        "id": "cmpl-" + uuid.uuid4().hex, "object": "text_completion",
        "created": int(time.time()), "model": data.get("model") or openai_body["model"],
        "choices": [{"text": (choice.get("message") or {}).get("content") or "",
                     "index": 0, "finish_reason": choice.get("finish_reason"),
                     "logprobs": None}],
        "usage": data.get("usage") or {},
    })


# --------------------------------------------------------------------------- #
# Embeddings
#
# The chat catalog deliberately drops these (routing must never pick an
# embedding model to generate text), which also left the hub unable to serve
# them at all. They are a different SURFACE, not junk: /v1/embeddings is what
# codebase indexing, RAG and semantic search call, and it is the one endpoint
# Continue, Open WebUI and every vector-store integration need before they can
# use this hub for anything but chat.
#
# Routing is deliberately simpler than the chat chain: there is no difficulty to
# assess and no swarm to escalate to, just "try the best available provider,
# fall through on failure". Vectors from DIFFERENT models are not comparable, so
# a silent fallback mid-corpus would poison an index -- which is why the model
# that actually served is always reported back in the response.
# --------------------------------------------------------------------------- #

def _embedding_chain(requested):
    """(pid, model) hops to try, best-first.

    An explicit '<pid>/<model>' pins to that one provider; anything else (or
    'auto') fans out across every provider that has an embedding model."""
    rows = embedding_models()
    if requested and requested not in ("auto", "best"):
        want = str(requested)
        exact = [(r["provider"], r["model"]) for r in rows if r["id"] == want]
        if exact:
            return exact
        # a bare model name, served by however many providers carry it
        by_name = [(r["provider"], r["model"]) for r in rows if r["model"] == want]
        if by_name:
            return by_name
        return []
    return [(r["provider"], r["model"]) for r in rows]


def _embed_upstream(pid, model, inputs, extra=None):
    """POST {base_url}/embeddings for one provider, rotating its key pool.

    Reuses the chat path's key rotation and quota accounting rather than opening
    a second, differently-behaved way of talking to a provider."""
    payload = {"model": model, "input": inputs}
    if isinstance(extra, dict):
        for k in ("encoding_format", "dimensions", "user"):
            if extra.get(k) is not None:
                payload[k] = extra[k]
    return _upstream_post(pid, "embeddings", payload)


@app.route("/v1/embeddings", methods=["POST"])
def v1_embeddings():
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return _openai_error("Invalid JSON body.", 400)
    raw = body.get("input")
    if raw is None or raw == "" or raw == []:
        return _openai_error("'input' is required.", 400)
    inputs = raw if isinstance(raw, list) else [raw]
    if not all(isinstance(x, (str, int, list)) for x in inputs):
        return _openai_error("'input' must be a string or an array of strings.", 400)

    chain = _embedding_chain(body.get("model"))
    if not chain:
        return _openai_error(
            "No embedding model is available. Enable a provider that serves one, "
            "or open the dashboard once so the model catalog is discovered.", 503)

    errors = []
    for pid, model in chain[:_EMBED_MAX_HOPS]:
        try:
            resp = _embed_upstream(pid, model, inputs, body)
        except (requests.RequestException, RuntimeError) as exc:
            errors.append("%s: %s" % (pid, _sanitize(exc.__class__.__name__)))
            _record_outcome(pid, model, False)
            continue
        if resp.status_code != 200:
            errors.append("%s: HTTP %d" % (pid, resp.status_code))
            _record_outcome(pid, model, False)
            if resp.status_code in _DEAD_STATUSES:
                _mark_model_dead(pid, model, resp.status_code)
            continue
        try:
            data = resp.json()
        except ValueError:
            errors.append("%s: bad JSON" % pid)
            _record_outcome(pid, model, False)
            continue
        if not isinstance(data, dict) or not data.get("data"):
            errors.append("%s: empty" % pid)
            _record_outcome(pid, model, False)
            continue
        _record_outcome(pid, model, True)
        # Say which model actually produced these. Vectors from two models are
        # not comparable, so a caller indexing a corpus must be able to tell
        # that a hop changed underneath it rather than discovering it later as
        # unexplained retrieval nonsense.
        data["model"] = pid + "/" + model
        return jsonify(data), 200, {"X-Free-LLM-Hub-Model": pid + "/" + model}

    return _openai_error("All embedding providers failed: " + "; ".join(errors[:5]), 502)


@app.route("/api/embed", methods=["POST"])
@app.route("/api/embeddings", methods=["POST"])
def ollama_embed():
    """Ollama has TWO embedding endpoints with different shapes: the current
    /api/embed (input, embeddings[][]) and the deprecated /api/embeddings
    (prompt, embedding[]). Clients in the wild still use both."""
    if not _is_ollama_path(request.path):
        return _ollama_off()
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify(wire_ollama.error_payload("Invalid JSON body.")), 400
    legacy = request.path == "/api/embeddings"
    raw = body.get("prompt") if legacy else body.get("input")
    if raw is None or raw == "":
        return jsonify(wire_ollama.error_payload(
            "'%s' is required." % ("prompt" if legacy else "input"))), 400
    inputs = raw if isinstance(raw, list) else [raw]

    chain = _embedding_chain(body.get("model"))
    if not chain:
        return jsonify(wire_ollama.error_payload(
            "No embedding model is available on this hub.")), 503
    pid, model = chain[0]
    try:
        resp = _embed_upstream(pid, model, inputs)
        data = resp.json() if resp.status_code == 200 else None
    except (requests.RequestException, RuntimeError, ValueError):
        data = None
    if not isinstance(data, dict) or not data.get("data"):
        _record_outcome(pid, model, False)
        return jsonify(wire_ollama.error_payload("Embedding provider failed.")), 502
    _record_outcome(pid, model, True)
    vectors = [row.get("embedding") or [] for row in data["data"]]
    if legacy:
        return jsonify({"embedding": vectors[0] if vectors else []})
    return jsonify({"model": wire_ollama.tag_name(pid + "/" + model),
                    "embeddings": vectors})


# --------------------------------------------------------------------------- #
# Ollama emulation
# --------------------------------------------------------------------------- #

def _ollama_off():
    return jsonify(wire_ollama.error_payload(
        "Ollama emulation is off. Enable the 'ollama_api' flag on the hub.")), 404


@app.route("/api/tags", methods=["GET"])
def ollama_tags():
    if not _is_ollama_path("/api/tags"):
        return _ollama_off()
    # 'auto' first: it is the id that actually routes, and Ollama clients pick
    # whatever is at the top of the list by default.
    rows = [{"id": "auto", "provider": "free-llm-hub"}]
    rows += [{"id": m, "provider": "free-llm-hub"} for m in (_SWARM_IDS + tuple(crews.CREW_IDS))]
    rows += [{"id": m["id"], "provider": m.get("provider")} for m in aggregated_models()]
    return jsonify(wire_ollama.tags_payload(rows))


@app.route("/api/ps", methods=["GET"])
def ollama_ps():
    if not _is_ollama_path("/api/ps"):
        return _ollama_off()
    return jsonify({"models": []})       # nothing is resident: every model is remote


@app.route("/api/show", methods=["POST"])
def ollama_show():
    if not _is_ollama_path("/api/show"):
        return _ollama_off()
    body = request.get_json(force=True, silent=True) or {}
    name = body.get("model") or body.get("name") or "auto"
    return jsonify(wire_ollama.show_payload(name))


def _ollama_chat_like(kind):
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify(wire_ollama.error_payload("Invalid JSON body.")), 400
    model = body.get("model") or "auto"
    openai_body = (wire_ollama.chat_to_openai(body) if kind == "chat"
                   else wire_ollama.generate_to_openai(body))
    # Ollama streams by DEFAULT -- an absent "stream" means true, the opposite
    # of OpenAI. Getting this backwards makes every client hang on first use.
    streaming = body.get("stream", True) is not False
    openai_body["stream"] = streaming
    started = time.time()

    if not streaming:
        resp, data, status = _call_router(openai_body)
        if status != 200 or not isinstance(data, dict):
            return jsonify(wire_ollama.error_payload(
                _error_text(data, "Upstream error."))), status
        ns = int((time.time() - started) * 1_000_000_000)
        out = (wire_ollama.chat_response(data, model, ns) if kind == "chat"
               else wire_ollama.generate_response(data, model, ns))
        return jsonify(out)

    resp, _data, status = _call_router(openai_body)
    if resp.mimetype != "text/event-stream":
        try:
            payload = resp.get_json()
        except Exception:                                        # noqa: BLE001
            payload = None
        return jsonify(wire_ollama.error_payload(
            _error_text(payload, "Upstream error."))), status

    def gen():
        usage, calls = None, None
        for text, tool_calls, _finish, u in _sse_deltas(resp):
            if u:
                usage = u
            if tool_calls:
                calls = tool_calls
            if not text:
                continue
            yield wire_ollama.ndjson(
                wire_ollama.chat_chunk(model, text) if kind == "chat"
                else wire_ollama.generate_chunk(model, text))
        if calls and kind == "chat":
            yield wire_ollama.ndjson(wire_ollama.chat_chunk(model, "", calls))
        ns = int((time.time() - started) * 1_000_000_000)
        # The done:true line is what stops the client spinning. Without it the
        # text all arrives and the UI still looks stuck forever.
        yield wire_ollama.ndjson(wire_ollama.final_chunk(model, kind, ns, usage))

    return Response(stream_with_context(gen()),
                    mimetype="application/x-ndjson",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/chat", methods=["POST"])
def ollama_chat():
    if not _is_ollama_path("/api/chat"):
        return _ollama_off()
    return _ollama_chat_like("chat")


@app.route("/api/generate", methods=["POST"])
def ollama_generate():
    if not _is_ollama_path("/api/generate"):
        return _ollama_off()
    return _ollama_chat_like("generate")


# --------------------------------------------------------------------------- #
# Gemini (generateContent)
# --------------------------------------------------------------------------- #

def _gemini_models():
    rows = [{"id": "auto", "context_window": None}]
    rows += [{"id": m, "context_window": None} for m in (_SWARM_IDS + tuple(crews.CREW_IDS))]
    rows += [{"id": m["id"], "context_window": m.get("context_window")}
             for m in aggregated_models()]
    return rows


@app.route("/v1beta/models", methods=["GET"])
@app.route("/v1beta/openai/models", methods=["GET"])
def gemini_models():
    return jsonify(wire_gemini.models_payload(_gemini_models()))


@app.route("/v1beta/models/<path:spec>", methods=["GET", "POST"])
def gemini_generate(spec):
    """One route for every method, because a model id here can contain slashes
    ("openrouter/glm-5.3") and the method is a ":suffix" on the last segment --
    neither of which Flask can express as separate rules."""
    method = None
    if ":" in spec:
        spec, method = spec.rsplit(":", 1)
    model = wire_gemini.strip_model_prefix(spec)

    if request.method == "GET" and not method:
        return jsonify(wire_gemini.model_entry(model))

    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify(wire_gemini.error_payload("Invalid JSON body.", 400)), 400

    if method == "countTokens":
        msgs = wire_gemini.contents_to_messages(body)
        # No routing margin: this number is shown to the caller, not used to
        # keep a request under a provider limit.
        return jsonify(wire_gemini.count_tokens_payload(
            _est_tokens(msgs, body.get("tools"), overhead=0)))

    if method not in ("generateContent", "streamGenerateContent"):
        return jsonify(wire_gemini.error_payload(
            "Unsupported method '%s'." % (method or ""), 400)), 400

    openai_body = wire_gemini.to_openai(body, model)
    streaming = method == "streamGenerateContent"
    openai_body["stream"] = streaming

    if not streaming:
        resp, data, status = _call_router(openai_body)
        if status != 200 or not isinstance(data, dict):
            return jsonify(wire_gemini.error_payload(
                _error_text(data, "Upstream error."), status)), status
        return jsonify(wire_gemini.from_openai(data, model))

    resp, _data, status = _call_router(openai_body)
    if resp.mimetype != "text/event-stream":
        try:
            payload = resp.get_json()
        except Exception:                                        # noqa: BLE001
            payload = None
        return jsonify(wire_gemini.error_payload(
            _error_text(payload, "Upstream error."), status)), status

    # ?alt=sse is what Gemini CLI and the SDKs send. Without it the documented
    # shape is a JSON ARRAY delivered incrementally, and a client expecting that
    # cannot parse SSE frames at all -- so both are emitted properly.
    as_sse = (request.args.get("alt") or "").lower() == "sse"

    def gen():
        usage, first = None, True
        for text, tool_calls, finish, u in _sse_deltas(resp):
            if u:
                usage = u
            if not text and not tool_calls and not finish:
                continue
            chunk = wire_gemini.stream_chunk(text, model, finish, tool_calls)
            if as_sse:
                yield "data: " + json.dumps(chunk) + "\n\n"
            else:
                yield ("[" if first else ",") + json.dumps(chunk)
                first = False
        tail = wire_gemini.stream_chunk("", model, "stop", None, usage)
        if as_sse:
            yield "data: " + json.dumps(tail) + "\n\n"
        else:
            yield ("[" if first else ",") + json.dumps(tail) + "]"

    if as_sse:
        return Response(stream_with_context(gen()), mimetype="text/event-stream",
                        headers=_SSE_HEADERS)
    return Response(stream_with_context(gen()), mimetype="application/json",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/v1/chat/completions", methods=["POST"])
def v1_chat_completions():
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return _openai_error("Invalid JSON body.", 400)
    return _chat_completions(body)


def _chat_completions(body):
    """The router, with the response cache in front of it.

    Wrapping the seam rather than each route means every surface -- OpenAI,
    Gemini, Ollama, /v1/completions -- is cached by the same rules, and a
    surface added later cannot forget to be.

    Off unless the `response_cache` flag is on. The scarce resource here is
    other people's free tiers, not latency: a hit is a request that never comes
    off a daily allowance. But a cache also turns "ask again" into "the same
    answer", which is right for a retry after a dropped stream and wrong for
    someone pressing regenerate hoping for better -- and nothing in the request
    distinguishes those, so the choice belongs to whoever runs the hub.

    A client can bypass it for one request with `X-Free-LLM-Hub-Cache: bypass`,
    which still STORES the fresh answer."""
    # IDEMPOTENCY. A client that retries a POST it never saw the answer to --
    # a dropped connection, a proxy timeout, an SDK's own retry -- spends the
    # free quota twice for one question. `Idempotency-Key` says "this is that
    # same request", so the first answer is returned instead of a second one
    # being generated. Independent of the response cache: this is about a
    # RETRY of one request, not about two people asking the same thing, so it
    # applies even to tool-carrying turns the cache refuses.
    idem = (request.headers.get("Idempotency-Key") or "").strip()[:200]
    if idem:
        held = _idem_get(idem)
        if held is not None:
            return jsonify(held), 200, {"X-Free-LLM-Hub-Idempotent": "replayed"}

    if not config.get_flag("response_cache", False):
        return _idem_store(idem, _chat_completions_uncached(body))

    bypass = (request.headers.get("X-Free-LLM-Hub-Cache") or "").lower() == "bypass"
    if not bypass:
        hit = respcache.get(body, ttl=_cache_ttl())
        if hit is not None:
            return _cached_response(body, hit)
    return _idem_store(idem, _remember_completion(body, _chat_completions_uncached(body)))


# Answers held against an Idempotency-Key. Small and short-lived: this exists to
# cover a retry of a request in flight, not to remember yesterday.
_IDEM_TTL = 600
_IDEM_MAX = 128
_idem = {}                      # key -> (stored_at, data)
_idem_lock = threading.Lock()


def _idem_get(key):
    if not key:
        return None
    now = time.time()
    with _idem_lock:
        hit = _idem.get(key)
        if not hit:
            return None
        stored_at, data = hit
        if now - stored_at > _IDEM_TTL:
            _idem.pop(key, None)
            return None
        return data


def _idem_store(key, rv):
    """Remember a successful answer under its idempotency key.

    Streamed answers are deliberately NOT held: replaying one would mean
    buffering it here, and a client streaming a turn is watching it arrive --
    the retry case this covers is a client that got no answer at all."""
    if not key:
        return rv
    try:
        resp = app.make_response(rv)
    except Exception:                                            # noqa: BLE001
        return rv
    if resp.status_code != 200 or resp.mimetype == "text/event-stream":
        return resp
    try:
        data = resp.get_json()
    except Exception:                                            # noqa: BLE001
        return resp
    if not isinstance(data, dict):
        return resp
    with _idem_lock:
        if len(_idem) >= _IDEM_MAX:
            oldest = next(iter(_idem), None)
            if oldest is not None:
                _idem.pop(oldest, None)
        _idem[key] = (time.time(), data)
    return resp


def _cache_ttl():
    try:
        return max(30, int(config.get_value("response_cache_ttl", "")
                           or respcache.DEFAULT_TTL))
    except (TypeError, ValueError):
        return respcache.DEFAULT_TTL


def _cached_response(body, data):
    """Serve a stored completion in whichever shape THIS request asked for.

    A cached answer has to be able to come back as a stream: the commonest hit
    by far is the re-run of a turn whose stream dropped, and that client is
    still a streaming client. It arrives as a single chunk, which is honest --
    the tokens were not generated now."""
    headers = {"X-Free-LLM-Hub-Cache": "hit"}
    if not body.get("stream"):
        return jsonify(data), 200, headers
    text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    model = data.get("model") or body.get("model") or "auto"
    cid = data.get("id") or ("chatcmpl-" + uuid.uuid4().hex)

    def gen():
        yield "data: " + json.dumps({
            "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                         "finish_reason": None}]}) + "\n\n"
        yield "data: " + json.dumps({
            "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": data.get("usage") or {}}) + "\n\n"
        yield "data: [DONE]\n\n"

    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers=dict(_SSE_HEADERS, **headers))


def _remember_completion(body, rv):
    """Store a fresh answer on its way out, buffered or streamed."""
    try:
        resp = app.make_response(rv)
    except Exception:                                            # noqa: BLE001
        return rv
    if resp.status_code != 200:
        return resp
    if resp.mimetype != "text/event-stream":
        try:
            respcache.put(body, resp.get_json())
        except Exception:                                        # noqa: BLE001
            pass
        return resp
    if not respcache.cacheable(body):
        return resp
    # A streamed answer is reassembled AS IT PASSES THROUGH, because the turns
    # worth caching are the expensive ones and those stream. Buffering the whole
    # thing first would hold the answer back from the client to serve a cache
    # that only helps the NEXT request.
    return Response(stream_with_context(_tee_and_store(resp.response, body)),
                    mimetype="text/event-stream", headers=dict(resp.headers))


def _tee_and_store(upstream, body):
    """Forward an SSE stream untouched while reassembling it for the cache."""
    parts, usage, model, cid = [], None, None, None
    buf = ""
    for raw in upstream:
        yield raw                                  # the client sees it unchanged
        buf += raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        while "\n\n" in buf:
            frame, buf = buf.split("\n\n", 1)
            for line in frame.splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    obj = json.loads(payload)
                except ValueError:
                    continue
                cid = cid or obj.get("id")
                model = model or obj.get("model")
                if obj.get("usage"):
                    usage = obj["usage"]
                delta = ((obj.get("choices") or [{}])[0] or {}).get("delta") or {}
                if delta.get("content"):
                    parts.append(delta["content"])
    text = "".join(parts)
    if not text.strip():
        return                       # a stream that produced nothing is not an answer
    try:
        respcache.put(body, {
            "id": cid or ("chatcmpl-" + uuid.uuid4().hex),
            "object": "chat.completion", "created": int(time.time()),
            "model": model or body.get("model") or "auto",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": usage or {}})
    except Exception:                                            # noqa: BLE001
        pass


def _chat_completions_uncached(body):
    """The whole router, reachable WITHOUT an HTTP request of its own.

    Every wire format this hub speaks -- OpenAI, Anthropic, Gemini, Ollama, the
    legacy /v1/completions -- wants the same thing behind it: difficulty
    routing, the fallback chain, swarm/crew escalation, quota and reliability
    accounting, auto-compaction. /v1/messages predates this seam and duplicates
    that loop; doing the same for two more formats would have meant four copies
    of the one piece of logic where a divergence is silent and expensive.

    So the foreign surfaces translate their request into an OpenAI body, call
    this, and translate the answer back. They inherit every routing improvement
    automatically, including ones added after they were written.

    Still runs inside a real request context (it reads request.headers for
    X-Free-LLM-Hub-Exclude), it just no longer re-reads the JSON body."""
    try:
        body["messages"], image_count = _normalize_openai_messages(body.get("messages"))
    except ValueError as exc:
        return _openai_error(str(exc), 400)
    has_images = image_count > 0
    # SWARM: an explicitly-selected virtual model, never an automatic mode — a
    # multi-pass pipeline applied behind a client's back would corrupt the agent
    # loops Codex/Claude Code run (see swarm.py's header). Tool-carrying turns
    # are refused outright for the same reason.
    if _is_swarm_model(body.get("model")):
        return _swarm_completion(body)
    # AUTO-ESCALATION to the crew pipeline. The dashboard project gate asks a
    # HUMAN "crew or plain?" before sending; an API client (hermes, openclaw,
    # a script) has no human to ask, so the hub decides by the same heuristic:
    # a tool-free, image-free, OPENING-turn 'auto' request that reads as a full
    # project gets the crew pipeline instead of one model. Tool-carrying turns
    # (agent loops) and explicit '<pid>/<model>' choices are never touched —
    # both are already a decision. Flag: crew_auto_escalate (default on).
    if (_is_orchestrate(body.get("model"))
            and config.get_flag("crew_auto_escalate", True)
            and not body.get("tools") and not has_images
            and len([m for m in (body.get("messages") or [])
                     if isinstance(m, dict) and m.get("role") == "user"]) == 1
            and crews.looks_like_full_project(
                swarm._last_user_text(body.get("messages")))):
        esc = dict(body)
        esc["model"] = "crew"
        return _swarm_completion(esc)
    # Orchestrate (Auto): route by task difficulty AND request size so weak/small
    # providers take easy work and big requests avoid small-TPM providers (413).
    # Explicit '<pid>/<model>' bypasses model choice (chain still size-filters).
    # A NAMED CHAIN used as the model: resolve it to its first live entry and
    # keep the rest as the preferred head of the fallback chain, so the order
    # the user wrote is the order that runs. Resolved here rather than inside
    # the router because everything below reads body["model"], and a name that
    # reached _route_by_difficulty would simply score as an unknown id.
    chain_prefer = None
    if _is_chain_name(body.get("model")):
        chain_prefer = _chain_entries(body.get("model"))
        if chain_prefer:
            body = dict(body)
            body["model"] = chain_prefer[0][0] + "/" + chain_prefer[0][1]
        else:
            # Every model in the chain is gone or dead. Fall back to Auto rather
            # than failing: a saved preference must never be able to break a
            # request outright.
            body = dict(body)
            body["model"] = "auto"
    est = _est_tokens(body.get("messages"), body.get("tools"))
    has_tools = bool(body.get("tools"))
    # "Retry with a different model": the caller names the model(s) it does NOT
    # want this time. Honoured for the Auto pick as well as the fallback chain --
    # vetoing it only in the chain would let Auto re-pick the very model the user
    # just rejected and answer from hop 1.
    veto = _excluded_identities(request.headers.get("X-Free-LLM-Hub-Exclude"))
    diff = None
    if _is_orchestrate(body.get("model")):
        router = _route_for_vision if has_images else _route_by_difficulty
        _rkw = {}
        if (body.get("model") or "").strip().lower() == "best" and not has_images:
            _rkw["quality_mode"] = True
        pid, resolved, diff = router(body.get("messages"), body.get("max_tokens"), est,
                                     require_tools=has_tools, **_rkw)
        if veto and pid is not None and _normalize_model_identity(resolved) in veto:
            # Auto landed on exactly the model the user just rejected. Hand the
            # choice to _build_chain with an empty primary: it applies the same
            # veto, so its first entry is the best model that is NOT vetoed.
            pid, resolved = None, None
            for _p, _m in _build_chain("", "", est, require_vision=has_images,
                                       require_tools=has_tools,
                                       messages=body.get("messages"),
                                       exclude_identities=veto):
                pid, resolved = _p, _m
                break
            if pid is None:
                return _openai_error(
                    "No other model is available to retry with right now.", 503,
                    "upstream_error")
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
    _blocked = _model_block_reason(pid, resolved)
    if _blocked:
        return _openai_error(_blocked, 403, "permission_error")
    if has_images and not _is_vision_model(pid, resolved):
        return _openai_error(
            "Model '%s/%s' is not a verified vision model." % (pid, resolved), 400)
    not_ready = _check_provider_ready(pid)
    if not_ready:
        return _openai_error(not_ready, 400)

    stream = bool(body.get("stream"))
    errors = _HopErrors()
    attempts = 0          # upstream hops actually tried (transparency header)
    last_hop = (None, None)
    last_hard = None  # last hard (non-retryable) upstream error, relayed if the chain is exhausted
    last_error = None  # class of the LAST failed hop (transparency header)
    # Pass exclude_identities ONLY when something is actually vetoed, so the
    # ordinary path keeps the exact call shape it always had (a stand-in for
    # _build_chain should not have to know about a parameter it never sees).
    _veto_kw = {"exclude_identities": veto} if veto else {}
    for hop_pid, hop_model in _build_chain(pid, resolved, est, require_vision=has_images,
                                          prefer=chain_prefer,
                                           require_tools=has_tools,
                                           messages=body.get("messages"),
                                           **_veto_kw):
        if not prov.is_model_allowed(hop_model):
            continue
        # MID-REQUEST re-check. _build_chain is computed ONCE, up front, so a
        # provider sidelined DURING this request (e.g. its 2nd distinct 402 ->
        # _mark_provider_authfail decides the account is broke) still has all its
        # remaining hops queued. MEASURED 2026-07-31: one Codex request burned
        # SEVEN consecutive puter/402 hops for exactly this reason before
        # reaching a model that could answer. Re-checking here drops the rest of
        # a provider's hops the moment it is proven dead.
        if _is_provider_dead(hop_pid):
            errors.append("%s: sidelined mid-request" % hop_pid)
            continue
        # See the matching comment in /v1/responses -- same fix, same reason:
        # dispatch a sub-* (local CLI relay) hop non-streaming even when the
        # client wants a stream, instead of skipping it outright, then emit
        # its complete answer as one synthetic SSE chunk below.
        is_sub_hop = _is_sub(hop_pid)
        payload = dict(body)
        payload["model"] = hop_model
        _apply_output_budget(payload, hop_pid)
        _apply_reasoning_effort(payload, hop_model, diff)
        dispatch_stream = stream and not is_sub_hop
        payload["stream"] = dispatch_stream
        try:
            _act_pick(hop_pid, hop_model)
            attempts += 1
            last_hop = (hop_pid, hop_model)
            resp = _dispatch_chat(hop_pid, payload, dispatch_stream)
        except (requests.RequestException, RuntimeError) as exc:
            errors.append("%s: %s" % (hop_pid, _sanitize(exc.__class__.__name__)))
            last_error = _classify_hop_error(exc=exc)
            if isinstance(exc, requests.exceptions.Timeout):
                # A hop that TIMES OUT (unlike a fast-fail 429/400) can burn
                # minutes doing it -- and an immediate retry of the same
                # request (codex's own client-level retry on 503, or this
                # chain's bonus whole-chain retry a bit further down) hits the
                # SAME slow hop again, compounding into a long stall with zero
                # progress. MEASURED 2026-08-03: nvidia/mistral-medium-3.5-128b
                # ReadTimeout'd 4 times running, ~7 minutes each, while every
                # other hop in the chain failed fast. Same short cooldown a
                # 429 already gets, so the NEXT chain build skips it long
                # enough for a retry to reach a hop that actually responds.
                # A plain ConnectionError deliberately stays OUT of this branch
                # (see test_a_fast_failing_hop_is_not_throttled_only_a_real_
                # timeout_is): a real connection-refused fails just as fast on
                # a retry, so there's nothing costly to protect against. The
                # SLOW failure mode that error class was missing turned out to
                # be a raw 5xx status (524 etc.) that never raises at all --
                # handled separately below, in the non-2xx branch.
                quota.mark_throttled(hop_pid, _HOP_COOLDOWN_DEFAULT)
            continue
        if resp.status_code == 200:
            if stream and is_sub_hop:
                try:
                    data = resp.json()
                except (ValueError, requests.RequestException):
                    errors.append("%s: non-JSON 200 body" % hop_pid)
                    last_error = "non-json"
                    resp.close()
                    continue
                if _chat_json_nonanswer(data, has_tools, body.get("tools")):
                    # A 200 whose content IS the relay backend's error page (see
                    # _chat_json_nonanswer). Falls through to the next hop exactly
                    # like an empty 200, and sidelines the id so it stops winning.
                    errors.append("%s: upstream error returned as content" % hop_pid)
                    last_error = "empty"
                    _note_nonanswer(hop_pid, hop_model)
                    resp.close()
                    continue
                if _chat_json_is_empty(data):
                    errors.append("%s: empty (200 but no content)" % hop_pid)
                    last_error = "empty"
                    resp.close()
                    continue
                _record_chat_usage(hop_pid, hop_model, data, est)
                msg = ((data.get("choices") or [{}])[0].get("message") or {})
                chunk = {"id": data.get("id", "chatcmpl-sub"), "object": "chat.completion.chunk",
                        "created": data.get("created", int(time.time())),
                        "model": hop_pid + "/" + hop_model,
                        "choices": [{"index": 0,
                                    "delta": {"role": "assistant", "content": msg.get("content") or ""},
                                    "finish_reason": None}]}
                done = dict(chunk, choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}])
                body_bytes = (b"data: " + json.dumps(chunk).encode("utf-8") + b"\n\n" +
                             b"data: " + json.dumps(done).encode("utf-8") + b"\n\n" +
                             b"data: [DONE]\n\n")
                return Response(stream_with_context(_proxy_sse(resp, iter([body_bytes]))),
                                mimetype="text/event-stream",
                                headers=dict(_SSE_HEADERS, **_routing_headers(
                                    hop_pid, hop_model, attempts, last_error)))
            if stream:
                # #4: peek the first byte BEFORE committing the 200. A hung/slow
                # stream (no first byte within STREAM_FIRST_BYTE_TIMEOUT) falls
                # through to the next provider instead of stalling the client.
                it = resp.iter_content(chunk_size=None)
                # Peek until REAL content: a 200 that streams no content must fall
                # through to the next model, not be handed to the client as empty.
                status, buffered = _peek_until_content(
                    it, _stream_peek_timeout(hop_model, est))
                if status != "content":
                    errors.append("%s: %s (200 but no content)" % (hop_pid, status))
                    last_error = _classify_hop_error(peek=status)
                    if status == "nonanswer":
                        # The relay streamed its own error page as the answer --
                        # sideline the id so it stops winning hop 1 (see
                        # _note_nonanswer; ranking alone never removed it).
                        _note_nonanswer(hop_pid, hop_model)
                    resp.close()
                    continue
                _note_ttft(resp, hop_pid, hop_model)
                chained = _chain_buffered(buffered, it)
                return Response(stream_with_context(
                    _proxy_sse(resp, chained, hop_pid=hop_pid, hop_model=hop_model)),
                                mimetype="text/event-stream",
                                headers=dict(_SSE_HEADERS, **_routing_headers(
                                    hop_pid, hop_model, attempts, last_error)))
            try:
                data = resp.json()
            except (ValueError, requests.RequestException):
                # Non-JSON / broken 200 body -> don't dead-end, try the next model.
                errors.append("%s: non-JSON 200 body" % hop_pid)
                last_error = "non-json"
                resp.close()
                continue
            if _chat_json_nonanswer(data, has_tools, body.get("tools")):
                # A 200 whose content IS the relay backend's error page (see
                # _chat_json_nonanswer). Falls through to the next hop exactly
                # like an empty 200, and sidelines the id so it stops winning.
                errors.append("%s: upstream error returned as content" % hop_pid)
                last_error = "empty"
                _note_nonanswer(hop_pid, hop_model)
                resp.close()
                continue
            if _chat_json_is_empty(data):
                # Starved by the token budget rather than broken? Give this SAME
                # hop one more go with room to answer, instead of throwing away a
                # model that was about to work. See _chat_json_starved.
                bigger = _starved_retry_budget(payload.get("max_tokens")) \
                    if _chat_json_starved(data) else None
                if bigger:
                    resp.close()
                    retry = dict(payload)
                    retry["max_tokens"] = bigger
                    try:
                        resp2 = _dispatch_chat(hop_pid, retry, False)
                        data2 = resp2.json() if resp2.status_code == 200 else None
                        resp2.close()
                    except (requests.RequestException, RuntimeError, ValueError):
                        data2 = None
                    if (data2 and not _chat_json_is_empty(data2)
                            and not _chat_json_nonanswer(data2, has_tools, body.get("tools"))):
                        _record_chat_usage(hop_pid, hop_model, data2, est)
                        data2["model"] = hop_pid + "/" + hop_model
                        return (jsonify(data2), 200,
                                _routing_headers(hop_pid, hop_model, attempts, last_error))
                    errors.append("%s: empty even at max_tokens=%d" % (hop_pid, bigger))
                    last_error = "empty"
                    continue
                errors.append("%s: empty (200 but no content)" % hop_pid)
                last_error = "empty"
                resp.close()
                continue
            _record_chat_usage(hop_pid, hop_model, data, est)
            if isinstance(data, dict):
                data["model"] = hop_pid + "/" + hop_model
                # An answer cut off at the provider's OWN default budget is a
                # bug the caller cannot see or fix -- learn from it so the next
                # request to this provider carries an explicit budget.
                try:
                    _note_default_truncation(
                        hop_pid, body.get("max_tokens"),
                        ((data.get("choices") or [{}])[0] or {}).get("finish_reason"))
                except Exception:                                # noqa: BLE001
                    pass
            return jsonify(data), 200, _routing_headers(hop_pid, hop_model, attempts, last_error)
        # Non-2xx. Retryable (429/5xx) and HARD errors (404/400/model-not-found)
        # both advance to the NEXT provider — each chain hop is a DIFFERENT
        # provider, so a broken model/provider should fall through before we give
        # up. Key rotation for the SAME provider already happened in _upstream_chat.
        # A network error while INSPECTING the error body (stream=True leaves the body
        # unread) must also just advance, never escape the loop into a 500.
        try:
            errors.append("%s: HTTP %d" % (hop_pid, resp.status_code))
            last_error = _classify_hop_error(status=resp.status_code)
            # EVERY non-2xx hop is a delivery failure for the reliability
            # ledger, not just 5xx. MEASURED 2026-08-07, chasing an
            # api.airforce error the user reported THREE times: g4f fronts ~42
            # backends, one of which is api.airforce with a GLOBAL 1 req/sec
            # cap shared by every g4f user, so its models answer
            #   429 "Global rate limit exceeded ... upgrade at api.airforce"
            #   402 "requires an active subscription"
            # 429 is deliberately NOT in _DEAD_STATUSES (a burst limit really
            # is temporary), so those ids returned after every cooldown, for
            # ever. Recording only timeouts and 5xx meant a hop that ONLY ever
            # 429s never accrued a penalty and kept winning slots. Laplace
            # smoothing keeps this fair: an occasional 429 among successes
            # barely moves the ratio, while a model that never delivers sinks.
            _record_outcome(hop_pid, hop_model, False)
            if resp.status_code >= 500:
                # 429 already gets throttled inside _upstream_chat once its key
                # pool is exhausted; a raw 5xx (502/503/524...) reaching here
                # never did -- so a hop that is actually down (not just rate
                # limited) got retried on every single request with zero
                # cooldown. MEASURED 2026-08-05: g4f-nvidia's mistral hop hit
                # HTTP 524 (Cloudflare gateway timeout) right after a
                # ConnectionError on the SAME hop moments earlier in a
                # different request -- nothing had cooled it down between them.
                quota.mark_throttled(hop_pid, _HOP_COOLDOWN_DEFAULT)
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
    try:  # DIAG (temporary): record WHY the chat chain exhausted (any CLI's 503).
        _log.warning("CHAT-503 stream=%s tools=%s images=%s est=%d errors=[%s] last_hard=%s",
                     stream, has_tools, has_images, est, "; ".join(errors) or "none",
                     (str(last_hard.get("status")) + "/" + str(last_hard.get("pid"))) if last_hard else "none")
    except Exception:
        pass
    hdrs = _routing_headers(last_hop[0], last_hop[1], attempts, last_error)
    if last_hard is not None:
        if last_hard["json"] is not None:
            return _with_headers(_with_retry_after(
                (jsonify(last_hard["json"]), _retryable_relay_status(last_hard["status"])), eta), hdrs)
        return _with_headers(_with_retry_after(_openai_error(
            "Upstream returned non-JSON (%s, HTTP %d): %s"
            % (last_hard["pid"], last_hard["status"], last_hard["text"]), 503, "upstream_error"), eta), hdrs)
    return _with_headers(_with_retry_after(_openai_error(
        "All providers failed: " + ("; ".join(errors) or "none available") + _no_candidates_hint(), 503, "upstream_error"), eta), hdrs)


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
            "arguments": _normalize_apply_patch_diff(
                fn.get("name") or "", _repair_tool_arguments(fn.get("arguments") or "")),
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


def _responses_stream(resp, model_label, line_iter=None, first=_MISSING, prompt_est=0,
                      hop_pid=None, hop_model=None):
    """Consume an upstream OpenAI chat SSE stream and re-emit it as Responses API
    events for Codex. When `line_iter`/`first` are supplied (the first-byte peek
    already pulled the first line from this exact iterator) the pre-read line is
    processed first, then the rest of the SAME iterator — so fast-path output is
    identical to before. `hop_pid`/`hop_model` are used only to record the
    outcome if _STREAM_PROGRESS_DEADLINE fires. Event order:
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

    def _finalize_open_items():
        """Emit done-events for whatever text/tool item is still in_progress and
        append it to done_items. Called once, either after a clean finish or from
        the except handler on a mid-stream failure (e.g. an upstream idle-read
        timeout) — without this, a caught exception left done_items empty even
        though real text had already been streamed to the client via delta events,
        so response.completed reported output: [] and the caller saw a clean
        'nothing happened' turn instead of the partial answer it actually got."""
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
            # Repair before emitting so we never hand the CLI a doubled-JSON tool_call
            # that it will replay and 503 on every later turn (see _repair_tool_arguments),
            # then strip a git-diff header block apply_patch parsers reject even though
            # the underlying hunk is valid (see _normalize_apply_patch_diff).
            full_args = _normalize_apply_patch_diff(
                st["name"], _repair_tool_arguments("".join(st["args"])))
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

    last_progress = time.time()
    try:
        yield _sse_event("response.created",
                         {"type": "response.created", "response": _obj("in_progress", [])})

        for raw in _chain_first(first, line_iter):
            now = time.time()
            if _sse_chunk_is_progress(raw):
                last_progress = now
            elif now - last_progress > _STREAM_PROGRESS_DEADLINE:
                _log.warning("[stream-stall] %s/%s: no real content for %ds, cutting off",
                            hop_pid, hop_model, _STREAM_PROGRESS_DEADLINE)
                _record_outcome(hop_pid, hop_model, False)
                break
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

            # Reasoning-phase keepalive: a thinking model sends reasoning deltas for
            # up to a minute before its first visible token. Forward an SSE COMMENT
            # (ignored by every compliant parser, invents no event type codex would
            # have to understand) so the connection is visibly alive meanwhile.
            if delta.get("reasoning_content") or delta.get("reasoning"):
                yield ": keepalive\n\n"

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

        yield from _finalize_open_items()

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
        try:
            yield from _finalize_open_items()
        except Exception:
            pass
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
def v1_responses(_retry_pass=False):
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
    # SWARM on codex's own protocol. This dispatch used to exist only in
    # /v1/chat/completions -- which codex never calls -- so "swarm" arrived here
    # as an unknown bare id and _resolve_model turned it into a literal model on
    # the default provider. Measured live: 'groq/swarm ! groq: HTTP 404'.
    if _is_swarm_model(body.get("model")):
        served = _swarm_as_responses(body, messages, tools, est)
        if served is not None:
            return served
        # No model could serve the fan-out. Still a request for maximum effort,
        # so continue as 'best' -- never as a model named "swarm".
        body = dict(body, model="best")
    if _is_orchestrate(body.get("model")):
        router = _route_for_vision if has_images else _route_by_difficulty
        pid, resolved, diff = router(messages, body.get("max_output_tokens"), est,
                                     require_tools=has_tools,
                                     **_quality_route_kwargs(body.get("model"), has_images))
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
    _blocked = _model_block_reason(pid, resolved)
    if _blocked:
        return _openai_error(_blocked, 403, "permission_error")
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
    errors = _HopErrors()
    last_hard = None  # last hard (non-retryable) upstream error, relayed if chain is exhausted
    last_error = None  # class of the LAST failed hop (transparency header)
    _tried = []  # DIAG: every hop the chain actually offered (root-cause the 503s)
    _err_bodies = {}  # DIAG: first raw error body per (pid:status) — reveals soft-400 reasons
    for hop_pid, hop_model in _build_chain(pid, resolved, est, require_vision=has_images,
                                           require_tools=has_tools, messages=messages):
        _tried.append(hop_pid + "/" + hop_model)
        if not prov.is_model_allowed(hop_model):
            continue
        # MID-REQUEST re-check — see the twin in /v1/chat/completions. The chain
        # is built once, so without this a provider proven dead on hop 2 keeps
        # every one of its later hops (measured: 7 consecutive puter/402s).
        if _is_provider_dead(hop_pid):
            errors.append("%s: sidelined mid-request" % hop_pid)
            continue
        # A local-CLI relay hop (sub-*) is a subprocess that runs to completion —
        # it cannot stream token-by-token. User-approved tradeoff 2026-07-29:
        # rather than skip it outright whenever the client wants a stream
        # (which silently skipped every relay hop for interactive/streaming
        # codex sessions — their actual normal usage),
        # dispatch it NON-streaming regardless of what the client asked for,
        # then emit its complete answer as ONE synthetic chunk through the
        # SAME _responses_stream() a real stream uses below. The client sees
        # nothing for the call's duration, then the full answer arrives at
        # once — not real streaming, but no longer silently skipped either.
        is_sub_hop = _is_sub(hop_pid)
        payload = dict(base_payload)
        payload["model"] = hop_model
        _apply_reasoning_effort(payload, hop_model, diff)
        dispatch_stream = stream and not is_sub_hop
        payload["stream"] = dispatch_stream
        try:
            _act_pick(hop_pid, hop_model)
            resp = _dispatch_chat(hop_pid, payload, dispatch_stream)
        except (requests.RequestException, RuntimeError) as exc:
            errors.append("%s: %s" % (hop_pid, _sanitize(exc.__class__.__name__)))
            last_error = _classify_hop_error(exc=exc)
            if isinstance(exc, requests.exceptions.Timeout):
                # A hop that TIMES OUT (unlike a fast-fail 429/400) can burn
                # minutes doing it -- and an immediate retry of the same
                # request (codex's own client-level retry on 503, or this
                # chain's bonus whole-chain retry a bit further down) hits the
                # SAME slow hop again, compounding into a long stall with zero
                # progress. MEASURED 2026-08-03: nvidia/mistral-medium-3.5-128b
                # ReadTimeout'd 4 times running, ~7 minutes each, while every
                # other hop in the chain failed fast. Same short cooldown a
                # 429 already gets, so the NEXT chain build skips it long
                # enough for a retry to reach a hop that actually responds.
                # A plain ConnectionError deliberately stays OUT of this branch
                # (see test_a_fast_failing_hop_is_not_throttled_only_a_real_
                # timeout_is): a real connection-refused fails just as fast on
                # a retry, so there's nothing costly to protect against. The
                # SLOW failure mode that error class was missing turned out to
                # be a raw 5xx status (524 etc.) that never raises at all --
                # handled separately below, in the non-2xx branch.
                quota.mark_throttled(hop_pid, _HOP_COOLDOWN_DEFAULT)
            continue
        if resp.status_code == 200:
            # Echo back the id the client ASKED for (codex sends "auto", which now has
            # config metadata) instead of the resolved "pid/model" — otherwise codex
            # re-warns "Model metadata for `pid/model` not found" on every response
            # (issue #21070). The real model that answered is still shown on the hub
            # dashboard + activity feed, so no transparency is lost.
            model_label = (body.get("model") or "").strip() or (hop_pid + "/" + hop_model)
            if stream and is_sub_hop:
                try:
                    data = resp.json()
                except (ValueError, requests.RequestException):
                    errors.append("%s: non-JSON 200 body" % hop_pid)
                    last_error = "non-json"
                    resp.close()
                    continue
                if _chat_json_nonanswer(data, has_tools, tools):
                    # A 200 whose content IS the relay backend's error page (see
                    # _chat_json_nonanswer). Falls through to the next hop exactly
                    # like an empty 200, and sidelines the id so it stops winning.
                    errors.append("%s: upstream error returned as content" % hop_pid)
                    last_error = "empty"
                    _note_nonanswer(hop_pid, hop_model)
                    resp.close()
                    continue
                if _chat_json_is_empty(data):
                    errors.append("%s: empty (200 but no content)" % hop_pid)
                    last_error = "empty"
                    resp.close()
                    continue
                _record_chat_usage(hop_pid, hop_model, data, est)
                msg = ((data.get("choices") or [{}])[0].get("message") or {})
                synth = json.dumps({"choices": [{"delta": {"content": msg.get("content") or ""}}]}).encode("utf-8")
                line_iter = iter([b"data: " + synth, b"data: [DONE]"])
                return Response(stream_with_context(
                    _responses_stream(resp, model_label, line_iter=line_iter, prompt_est=est)),
                    mimetype="text/event-stream", headers=_SSE_HEADERS)
            if stream:
                # #4: peek the first line BEFORE committing the 200 SSE stream so a
                # hung/slow provider falls through to the next hop instead of stalling.
                line_it = resp.iter_lines(decode_unicode=False)
                # Peek until REAL content (not just the first byte): an empty 200
                # (role delta + [DONE], no content) must fall through to the next
                # model instead of being streamed to codex as a dead-end answer.
                status, buffered = _peek_until_content(
                    line_it, _stream_peek_timeout(hop_model, est))
                if status != "content":
                    errors.append("%s: %s (200 but no content)" % (hop_pid, status))
                    last_error = _classify_hop_error(peek=status)
                    if status == "nonanswer":
                        # The relay streamed its own error page as the answer --
                        # sideline the id so it stops winning hop 1 (see
                        # _note_nonanswer; ranking alone never removed it).
                        _note_nonanswer(hop_pid, hop_model)
                    resp.close()
                    continue
                _note_ttft(resp, hop_pid, hop_model)
                chained = _chain_buffered(buffered, line_it)
                return Response(stream_with_context(
                    _responses_stream(resp, model_label, line_iter=chained, prompt_est=est,
                                      hop_pid=hop_pid, hop_model=hop_model)),
                    mimetype="text/event-stream", headers=_SSE_HEADERS)
            try:
                data = resp.json()
            except (ValueError, requests.RequestException):
                errors.append("%s: non-JSON 200 body" % hop_pid)
                last_error = "non-json"
                resp.close()
                continue
            if _chat_json_nonanswer(data, has_tools, tools):
                # A 200 whose content IS the relay backend's error page (see
                # _chat_json_nonanswer). Falls through to the next hop exactly
                # like an empty 200, and sidelines the id so it stops winning.
                errors.append("%s: upstream error returned as content" % hop_pid)
                last_error = "empty"
                _note_nonanswer(hop_pid, hop_model)
                resp.close()
                continue
            if _chat_json_is_empty(data):
                errors.append("%s: empty (200 but no content)" % hop_pid)
                last_error = "empty"
                resp.close()
                continue
            _record_chat_usage(hop_pid, hop_model, data, est)
            return jsonify(_chat_to_responses(data, model_label)), 200
        try:
            errors.append("%s: HTTP %d" % (hop_pid, resp.status_code))
            last_error = _classify_hop_error(status=resp.status_code)
            # EVERY non-2xx hop is a delivery failure for the reliability
            # ledger, not just 5xx. MEASURED 2026-08-07, chasing an
            # api.airforce error the user reported THREE times: g4f fronts ~42
            # backends, one of which is api.airforce with a GLOBAL 1 req/sec
            # cap shared by every g4f user, so its models answer
            #   429 "Global rate limit exceeded ... upgrade at api.airforce"
            #   402 "requires an active subscription"
            # 429 is deliberately NOT in _DEAD_STATUSES (a burst limit really
            # is temporary), so those ids returned after every cooldown, for
            # ever. Recording only timeouts and 5xx meant a hop that ONLY ever
            # 429s never accrued a penalty and kept winning slots. Laplace
            # smoothing keeps this fair: an occasional 429 among successes
            # barely moves the ratio, while a model that never delivers sinks.
            _record_outcome(hop_pid, hop_model, False)
            if resp.status_code >= 500:
                # See the matching comment in /v1/chat/completions: a raw 5xx
                # (unlike 429, already handled inside _upstream_chat) never got
                # a cooldown here, so a genuinely down hop was retried on every
                # request. MEASURED 2026-08-05: g4f-nvidia/mistral-medium-3.5.
                quota.mark_throttled(hop_pid, _HOP_COOLDOWN_DEFAULT)
            _ekey = "%s:%d" % (hop_pid, resp.status_code)  # DIAG: capture first raw body
            if _ekey not in _err_bodies:
                try:
                    _err_bodies[_ekey] = _sanitize(resp.text)[:200]
                except Exception:
                    _err_bodies[_ekey] = "?"
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
    # TRANSIENT-STORM RETRY. An agentic CLI fires many requests in quick
    # succession, and the free relays are metered PER MINUTE (g4f ~5/min,
    # llm7 20/min). A burst therefore 429s every hop in the chain at once and
    # Codex receives a hard 503 — which reads to the user as "it ignored me and
    # stopped", when the window would have refilled seconds later. MEASURED:
    # three Codex turns 503'd with every hop 429, while the same providers all
    # read healthy moments afterwards.
    # So when EVERY failure was transient, wait once and run the chain again.
    # Bounded to a single retry, and only when nothing hard failed — a real 400
    # or 404 still surfaces immediately, because retrying that just wastes time.
    if (not last_hard and errors and not _retry_pass
            and all(_TRANSIENT_ERR_RE.search(e or "") for e in errors)):
        _log.info("[chain] all %d hops transient (429/5xx) — backing off %.1fs and retrying",
                  len(errors), _CHAIN_RETRY_DELAY)
        time.sleep(_CHAIN_RETRY_DELAY)
        return v1_responses(_retry_pass=True)
    eta = _capacity_eta()
    # DIAG (temporary): the access log only shows "503" — record WHY the responses
    # chain (Codex's path) exhausted so the real root cause is visible, not guessed.
    try:
        _lh_body = "none"
        if last_hard:
            try:
                _lh_body = (json.dumps(last_hard.get("json"))
                            if last_hard.get("json") is not None
                            else str(last_hard.get("text")))[:700]
            except Exception:
                _lh_body = "?"
        _log.warning(
            "RESPONSES-503 stream=%s tools=%s images=%s est=%d hops=%d tried=[%s] errors=[%s] last_hard=%s body=%s bodies=%s",
            stream, has_tools, has_images, est, len(_tried), ", ".join(_tried),
            "; ".join(errors) or "none",
            (str(last_hard.get("status")) + "/" + str(last_hard.get("pid"))) if last_hard else "none",
            _lh_body, json.dumps(_err_bodies)[:1400])
    except Exception:
        pass
    # Codex's path carried no routing headers at all — surface at least the last
    # hop-failure class so 'why did the chain degrade' is one curl -i away.
    hdrs = {"X-Free-LLM-Hub-Last-Error": last_error or "none"}
    if last_hard is not None:
        if last_hard["json"] is not None:
            return _with_headers(_with_retry_after(
                (jsonify(last_hard["json"]), _retryable_relay_status(last_hard["status"])), eta), hdrs)
        return _with_headers(_with_retry_after(_openai_error(
            "Upstream returned non-JSON (%s, HTTP %d): %s"
            % (last_hard["pid"], last_hard["status"], last_hard["text"]), 503, "upstream_error"), eta), hdrs)
    return _with_headers(_with_retry_after(_openai_error(
        "All providers failed: " + ("; ".join(errors) or "none available") + _no_candidates_hint(), 503, "upstream_error"), eta), hdrs)


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
    last_progress = time.time()
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
            now = time.time()
            if _sse_chunk_is_progress(raw):
                last_progress = now
            elif now - last_progress > _STREAM_PROGRESS_DEADLINE:
                _log.warning("[stream-stall] %s/%s: no real content for %ds, cutting off",
                            hop_pid, hop_model, _STREAM_PROGRESS_DEADLINE)
                _record_outcome(hop_pid, hop_model, False)
                break
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

            # A reasoning model can think for a minute before its first visible
            # token. Upstream is sending reasoning deltas the whole time, but we
            # forward nothing — so the CLI sees a dead connection and may give up.
            # A ping is the protocol's own keepalive: no content, no shape change.
            if delta.get("reasoning_content") or delta.get("reasoning"):
                yield _sse_event("ping", {"type": "ping"})

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
    except Exception as exc:
        # A mid-stream upstream failure (connection reset, ChunkedEncodingError,
        # read timeout) used to propagate out of this generator and truncate the
        # SSE body with NO terminal event, which leaves Claude Code waiting forever
        # on a turn that is already dead. Always close the message properly instead:
        # the client gets a well-formed (if short) turn and can continue.
        # NOTE: `except Exception` deliberately does not catch GeneratorExit, so a
        # normal client disconnect still tears the generator down as before.
        _log.error("Anthropic stream error: %s", _sanitize(str(exc)))
        try:
            if block_index < 0:
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
                "delta": {"stop_reason": _map_stop_reason(finish_reason),
                          "stop_sequence": None},
                "usage": {"output_tokens": int(out_tokens)}})
            yield _sse_event("message_stop", {"type": "message_stop"})
        except Exception:
            pass    # client socket already gone / state not yet bound
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
    # Same two modes, on claude's protocol. See the note in /v1/responses.
    if _is_swarm_model(body.get("model")):
        served = _swarm_as_anthropic(body, oai_messages, tools)
        if served is not None:
            return served
        body = dict(body, model="best")
    if _is_orchestrate(body.get("model")):
        router = _route_for_vision if has_images else _route_by_difficulty
        pid, resolved, diff = router(oai_messages, body.get("max_tokens"), est,
                                     require_tools=has_tools,
                                     **_quality_route_kwargs(body.get("model"), has_images))
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
    _blocked = _model_block_reason(pid, resolved)
    if _blocked:
        return _anthropic_error("permission_error", _blocked, 403)
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

    errors = _HopErrors()
    attempts = 0          # upstream hops actually tried (transparency header)
    last_hop = (None, None)
    last_hard = None  # last hard (non-retryable) upstream error, relayed if the chain is exhausted
    last_error = None  # class of the LAST failed hop (transparency header)
    for hop_pid, hop_model in _build_chain(pid, resolved, est, require_vision=has_images,
                                           require_tools=has_tools, messages=oai_messages):
        if not prov.is_model_allowed(hop_model):
            continue
        if stream and _is_sub(hop_pid):
            errors.append("%s: skipped (a local CLI cannot stream)" % hop_pid)
            last_error = "skipped"
            continue
        payload = dict(base_payload)
        payload["model"] = hop_model
        _apply_reasoning_effort(payload, hop_model, diff)
        payload["stream"] = stream
        try:
            _act_pick(hop_pid, hop_model)
            attempts += 1
            last_hop = (hop_pid, hop_model)
            resp = _dispatch_chat(hop_pid, payload, stream)
        except (requests.RequestException, RuntimeError) as exc:
            errors.append("%s: %s" % (hop_pid, _sanitize(exc.__class__.__name__)))
            last_error = _classify_hop_error(exc=exc)
            if isinstance(exc, requests.exceptions.Timeout):
                # A hop that TIMES OUT (unlike a fast-fail 429/400) can burn
                # minutes doing it -- and an immediate retry of the same
                # request (codex's own client-level retry on 503, or this
                # chain's bonus whole-chain retry a bit further down) hits the
                # SAME slow hop again, compounding into a long stall with zero
                # progress. MEASURED 2026-08-03: nvidia/mistral-medium-3.5-128b
                # ReadTimeout'd 4 times running, ~7 minutes each, while every
                # other hop in the chain failed fast. Same short cooldown a
                # 429 already gets, so the NEXT chain build skips it long
                # enough for a retry to reach a hop that actually responds.
                # A plain ConnectionError deliberately stays OUT of this branch
                # (see test_a_fast_failing_hop_is_not_throttled_only_a_real_
                # timeout_is): a real connection-refused fails just as fast on
                # a retry, so there's nothing costly to protect against. The
                # SLOW failure mode that error class was missing turned out to
                # be a raw 5xx status (524 etc.) that never raises at all --
                # handled separately below, in the non-2xx branch.
                quota.mark_throttled(hop_pid, _HOP_COOLDOWN_DEFAULT)
            continue
        if resp.status_code == 200:
            model_str = requested_model or (hop_pid + "/" + hop_model)
            if stream:
                # #4: peek the first line BEFORE committing the 200 SSE stream so a
                # hung/slow provider falls through to the next hop instead of stalling.
                line_it = resp.iter_lines(decode_unicode=False)
                # Peek until REAL content so an empty 200 falls through to the next
                # model instead of being handed to the client as a dead-end answer.
                status, buffered = _peek_until_content(
                    line_it, _stream_peek_timeout(hop_model, est))
                if status != "content":
                    errors.append("%s: %s (200 but no content)" % (hop_pid, status))
                    last_error = _classify_hop_error(peek=status)
                    if status == "nonanswer":
                        # The relay streamed its own error page as the answer --
                        # sideline the id so it stops winning hop 1 (see
                        # _note_nonanswer; ranking alone never removed it).
                        _note_nonanswer(hop_pid, hop_model)
                    resp.close()
                    continue
                _note_ttft(resp, hop_pid, hop_model)
                chained = _chain_buffered(buffered, line_it)
                return Response(stream_with_context(
                    _anthropic_stream(resp, model_str, input_est, line_iter=chained,
                                     hop_pid=hop_pid, hop_model=hop_model)),
                    mimetype="text/event-stream",
                    headers=dict(_SSE_HEADERS, **_routing_headers(
                        hop_pid, hop_model, attempts, last_error)))
            try:
                data = resp.json()
            except (ValueError, requests.RequestException):
                errors.append("%s: non-JSON 200 body" % hop_pid)
                last_error = "non-json"
                resp.close()
                continue
            if _chat_json_nonanswer(data, has_tools, tools):
                # A 200 whose content IS the relay backend's error page (see
                # _chat_json_nonanswer). Falls through to the next hop exactly
                # like an empty 200, and sidelines the id so it stops winning.
                errors.append("%s: upstream error returned as content" % hop_pid)
                last_error = "empty"
                _note_nonanswer(hop_pid, hop_model)
                resp.close()
                continue
            if _chat_json_is_empty(data):
                errors.append("%s: empty (200 but no content)" % hop_pid)
                last_error = "empty"
                resp.close()
                continue
            _record_chat_usage(hop_pid, hop_model, data, est)
            return jsonify(_openai_resp_to_anthropic(data, model_str)), 200, \
                _routing_headers(hop_pid, hop_model, attempts, last_error)
        # Non-2xx. Retryable (429/5xx) AND hard errors (404/400/model-not-found)
        # both advance to the NEXT provider (a different provider/model) before we
        # surface an error; within-provider key rotation already ran upstream. A
        # network error reading the (unread, stream=True) error body must also just
        # advance, never escape the loop into a 500.
        try:
            errors.append("%s: HTTP %d" % (hop_pid, resp.status_code))
            last_error = _classify_hop_error(status=resp.status_code)
            # EVERY non-2xx hop is a delivery failure for the reliability
            # ledger, not just 5xx. MEASURED 2026-08-07, chasing an
            # api.airforce error the user reported THREE times: g4f fronts ~42
            # backends, one of which is api.airforce with a GLOBAL 1 req/sec
            # cap shared by every g4f user, so its models answer
            #   429 "Global rate limit exceeded ... upgrade at api.airforce"
            #   402 "requires an active subscription"
            # 429 is deliberately NOT in _DEAD_STATUSES (a burst limit really
            # is temporary), so those ids returned after every cooldown, for
            # ever. Recording only timeouts and 5xx meant a hop that ONLY ever
            # 429s never accrued a penalty and kept winning slots. Laplace
            # smoothing keeps this fair: an occasional 429 among successes
            # barely moves the ratio, while a model that never delivers sinks.
            _record_outcome(hop_pid, hop_model, False)
            if resp.status_code >= 500:
                # See the matching comment in /v1/chat/completions: a raw 5xx
                # (unlike 429, already handled inside _upstream_chat) never got
                # a cooldown here, so a genuinely down hop was retried on every
                # request. MEASURED 2026-08-05: g4f-nvidia/mistral-medium-3.5.
                quota.mark_throttled(hop_pid, _HOP_COOLDOWN_DEFAULT)
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
    try:  # DIAG (temporary): record WHY the messages chain exhausted (Claude Code's 503).
        _log.warning("MESSAGES-503 stream=%s tools=%s images=%s est=%d errors=[%s] last_hard=%s",
                     stream, has_tools, has_images, est, "; ".join(errors) or "none",
                     (str(last_hard.get("status")) + "/" + str(last_hard.get("pid"))) if last_hard else "none")
    except Exception:
        pass
    hdrs = _routing_headers(last_hop[0], last_hop[1], attempts, last_error)
    if last_hard is not None:
        return _with_headers(_with_retry_after(_anthropic_error("api_error",
                                "Upstream %s error (HTTP %d): %s"
                                % (last_hard["pid"], last_hard["http"], last_hard["detail"]),
                                last_hard["status"]), eta), hdrs)
    return _with_headers(_with_retry_after(_anthropic_error("api_error",
                            "All providers failed: " + ("; ".join(errors) or "none available") + _no_candidates_hint(),
                            503), eta), hdrs)


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
# Puter is deliberately ABSENT: every one of its 59 image models bills the
# account (0.3-17c per image, verified against its own catalog 2026-07-31), so
# none of them are free-rotation material. They stay one click away in the
# picker as explicit pins — see providers.py's puter image_models.
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


_AIHORDE_POLL_DEADLINE = 300      # anonymous queue can genuinely take minutes
_AIHORDE_ANON_KEY = "0000000000"  # AI Horde's documented public anonymous key


def _aihorde_generate_image(pcfg, model, prompt, size=1024, steps=4):
    """AI Horde (aihorde.net, formerly Stable Horde) -- a community-run,
    GPU-donated distributed inference network. VERIFIED live 2026-07-27:
    submitted a real anonymous job (apikey "0000000000"), watched the queue
    position advance, downloaded and hex-verified an actual WebP image --
    zero payment, zero signup. A personal free account/key jumps the
    (otherwise lowest-priority) anonymous queue but isn't required. Async
    submit-then-poll, same shape as this file's other async generators
    (r2=false requests base64 inline, skipping the extra download hop)."""
    keys = pcfg.get("api_keys") or []
    api_key = keys[0] if keys else _AIHORDE_ANON_KEY
    headers = {"apikey": api_key, "Content-Type": "application/json",
               "Client-Agent": "free-llm-hub:1.0:https://github.com/last-million/free-llm-hub"}
    width, height = _parse_wh(size)
    if not keys:
        # MEASURED 2026-07-27: the anonymous key 0000000000 gets a real 403
        # ("the client needs to already have the required kudos") for any
        # request over 661x661px -- a real anti-abuse limit, not a fluke; the
        # hub's own default size (1024x1024) blew straight past it. A
        # personal key (real kudos balance) isn't capped the same way, so
        # only clamp when actually running anonymous.
        width, height = min(width, 640), min(height, 640)
    body = {"prompt": (prompt or "")[:2000],
            "params": {"width": width, "height": height, "steps": 20,
                       "sampler_name": "k_euler", "cfg_scale": 7.5},
            "nsfw": False, "models": [model], "r2": False}
    try:
        resp = requests.post("https://aihorde.net/api/v2/generate/async",
                             headers=headers, json=body,
                             timeout=(CONNECT_TIMEOUT, CHAT_READ_TIMEOUT))
    except requests.RequestException as exc:
        return 502, None, exc.__class__.__name__
    if resp.status_code not in (200, 202):
        return resp.status_code, None, _upstream_error_detail(resp)
    job_id = (resp.json() or {}).get("id")
    if not job_id:
        return 502, None, "AI Horde returned no job id"
    check_url = "https://aihorde.net/api/v2/generate/check/" + job_id
    status_url = "https://aihorde.net/api/v2/generate/status/" + job_id
    deadline = time.time() + _AIHORDE_POLL_DEADLINE
    done = False
    while time.time() < deadline:
        time.sleep(4)
        try:
            cr = requests.get(check_url, timeout=(CONNECT_TIMEOUT, MODELS_READ_TIMEOUT))
        except requests.RequestException:
            continue
        cd = cr.json() if cr.status_code == 200 else {}
        if cd.get("faulted"):
            return 502, None, "AI Horde job faulted"
        if cd.get("done"):
            done = True
            break
    if not done:
        return 504, None, "AI Horde timed out waiting for a free worker"
    try:
        sr = requests.get(status_url, timeout=(CONNECT_TIMEOUT, MODELS_READ_TIMEOUT))
    except requests.RequestException as exc:
        return 502, None, exc.__class__.__name__
    if sr.status_code != 200:
        return sr.status_code, None, _upstream_error_detail(sr)
    gens = (sr.json() or {}).get("generations") or []
    if not gens or not gens[0].get("img"):
        return 502, None, "AI Horde returned no image data"
    return 200, gens[0]["img"], None


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
    "aihorde": _aihorde_generate_image,
    "openai": _openai_generate_image,
    "google": _google_generate_image,
    "openrouter": _openrouter_generate_image,
    "higgsfield": _higgsfield_generate_image,
    # Driver-based, not an images REST endpoint — see _puter_generate_image.
    "puter": _puter_generate_image,
}


_IMAGE_TEXT_QA_PROMPT = (
    "Look closely at this image. Does it contain any text (words, letters, "
    "numbers, a sign, a label, a logo wordmark) that is garbled, misspelled, "
    "malformed, duplicated, or otherwise clearly wrong? AI image generators "
    "frequently render text incorrectly even when everything else looks "
    "right. Reply with exactly one word first, YES or NO. If YES, add one "
    "short sentence naming what is wrong. If the image has no text at all, "
    "reply NO."
)


def _image_text_qa_flagged(b64_raw):
    """User 2026-08-05: "verify with vision each image to see if there is
    TEXT issue... sometimes image generators do mistakes in text." Ask a
    vision-capable model whether a just-generated image has a text-rendering
    defect. `b64_raw` is whatever format the generator returned (png/jpeg/
    webp vary by provider) -- re-encoded here via _to_webp_b64 so the data
    URL always carries a mime type that actually matches its bytes. True if
    flagged, False if clean, None if no vision model could be reached at all
    -- the caller fails OPEN on None (keeps the image rather than blocking
    generation on an unrelated vision-provider outage). Tries up to 2 vision
    candidates; never raises."""
    try:
        candidates = _vision_candidates()[:2]
    except Exception:                                              # noqa: BLE001
        return None
    if not candidates:
        return None
    b64, mime = _to_webp_b64(b64_raw)
    for pid, model in candidates:
        payload = {
            "model": model, "max_tokens": 120, "stream": False,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": _IMAGE_TEXT_QA_PROMPT},
                    {"type": "image_url",
                     "image_url": {"url": "data:%s;base64,%s" % (mime, b64)}},
                ],
            }],
        }
        try:
            resp = _dispatch_chat(pid, payload, False)
        except (requests.RequestException, RuntimeError):
            continue
        if resp.status_code != 200:
            continue
        try:
            data = resp.json()
            text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        except (ValueError, requests.RequestException):
            continue
        return text.strip().upper().startswith("YES")
    return None


def _call_image_generator(pid, generator, pcfg, model, prompt, size, steps):
    """Same key-pool-rotation policy as the chat path's _upstream_chat
    (_KEY_ROTATE_STATUSES / _next_key_start, app.py ~3012-3027) -- MEASURED
    2026-07-27: every one of the 7 generators above only ever reads
    api_keys[0], so a provider with a 3-key pool where key[0] is
    revoked/exhausted had the whole provider abandoned for image generation
    instead of trying key[1]/key[2], unlike text chat which already recovers
    within a hop. Generators are left untouched -- each attempt gets a pcfg
    view scoped to exactly ONE key, so their existing single-key logic
    (async polling, SSRF-checked downloads, composite Higgsfield
    KEY_ID:KEY_SECRET credentials) runs exactly as before; rotation only
    decides which key populates that view on the next attempt."""
    keys = pcfg.get("api_keys") or []
    if len(keys) <= 1:
        return generator(pcfg, model, prompt, size=size, steps=steps)
    n = len(keys)
    start = _next_key_start(pid, n)
    result = (400, None, "no api key for provider %s" % pid)
    for i in range(n):
        is_last = (i == n - 1)
        one_key_cfg = dict(pcfg)
        one_key_cfg["api_keys"] = [keys[(start + i) % n]]
        result = generator(one_key_cfg, model, prompt, size=size, steps=steps)
        if result[0] in _KEY_ROTATE_STATUSES and not is_last:
            continue
        return result
    return result


def _save_generated_image(b64, prompt, pid, model):
    """Best-effort persist of one successful generation for the history
    gallery. Never raises -- a history-tracking bug must never break a real
    image-generation response."""
    try:
        image_history.save(base64.b64decode(b64), prompt, pid, model)
    except Exception:
        pass


_WEBP_QUALITY = 82      # visually lossless for photos, ~30% of the JPEG bytes


def _to_webp_b64(b64):
    """Re-encode a generated image to WebP. Returns (b64, mime).

    WHY IN THE HUB AND NOT IN THE BRIEF: "convert the images to WebP" written
    as an instruction is a step a model can forget, and every provider returns
    something different (cloudflare/flux answers JPEG, pollinations PNG). Doing
    it here means every client — every CLI, the dashboard, a raw curl — gets
    WebP without knowing anything about it, and the page it builds is already
    carrying the smaller file.

    Falls back to the original bytes if Pillow is missing or the re-encode
    fails: a slightly larger image is a much better outcome than a 500."""
    try:
        raw = base64.b64decode(b64)
    except (binascii.Error, ValueError):
        return b64, "image/png"
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw))
        # WebP has no palette mode and only keeps alpha in RGBA — normalise
        # first or Pillow raises on P/CMYK images from some generators.
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=_WEBP_QUALITY, method=4)
        return base64.b64encode(buf.getvalue()).decode("ascii"), "image/webp"
    except Exception:
        # Unknown/broken format, or Pillow built without WebP. Report the mime
        # HONESTLY rather than claiming webp for bytes that are not.
        if raw[:8] == b"\x89PNG\r\n\x1a\n":
            return b64, "image/png"
        if raw[:3] == b"\xff\xd8\xff":
            return b64, "image/jpeg"
        return b64, "image/png"


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
    # Fallback if EVERY hop's image gets text-flagged: a real (if imperfect)
    # image beats a 502 over a soft quality heuristic -- see below.
    text_flagged_fallback = None
    for hop_pid, hop_model in chain[:MAX_IMAGE_HOPS]:
        generator = _IMAGE_GENERATORS.get(hop_pid)
        if not generator:
            continue
        pcfg = config.get_provider_config(hop_pid)
        try:
            status, b64, detail = _call_image_generator(
                hop_pid, generator, pcfg, hop_model, prompt, size, steps)
        except (requests.RequestException, RuntimeError) as exc:
            errors.append("%s: %s" % (hop_pid, _sanitize(exc.__class__.__name__)))
            continue
        quota.record(hop_pid, hop_model)
        if status == 200 and b64:
            # User 2026-08-05: "verify with vision each image... ask to
            # regenerate it to fix the text." A flagged image is treated as a
            # soft failure -- fall through to the NEXT hop (a different
            # provider/model, a real chance at different output) rather than
            # accepting it. None (no vision model reachable) fails OPEN and
            # accepts immediately, same as a clean result.
            if _image_text_qa_flagged(b64):
                errors.append("%s: vision flagged text issue, retrying next hop" % hop_pid)
                if text_flagged_fallback is None:
                    text_flagged_fallback = (b64, hop_pid, hop_model)
                continue
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
    if not images_b64 and text_flagged_fallback is not None:
        # Every hop that produced an image got text-flagged -- a real image
        # with a possible text defect still beats failing the whole request.
        b64, landed_pid, landed_model = text_flagged_fallback
        images_b64.append(b64)
        _save_generated_image(b64, prompt, landed_pid, landed_model)
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
            status, b64, _detail = _call_image_generator(
                landed_pid, generator, pcfg, landed_model, prompt, size, steps)
        except (requests.RequestException, RuntimeError):
            break
        quota.record(landed_pid, landed_model)
        if status == 200 and b64 and not _image_text_qa_flagged(b64):
            images_b64.append(b64)
        else:
            break

    # WebP on the way out, so every consumer gets the smaller file without
    # having to ask for it. `mime` is an extra field the OpenAI shape does not
    # define — clients that don't know it ignore it, and ours uses it instead of
    # assuming image/png (which was wrong for every JPEG we have ever returned).
    converted = [_to_webp_b64(b) for b in images_b64]
    return jsonify({
        "created": int(time.time()),
        "model": landed_pid + "/" + landed_model,
        "data": [{"b64_json": b, "mime": m} for b, m in converted],
    }), 200


def _images_payload():
    state = config.get_images_state()
    models = []
    for p in prov.list_providers():
        pid = p["id"]
        pcfg = config.get_provider_config(pid)
        rows = _image_model_rows(pid)
        # Quota state per PROVIDER, resolved once and stamped on each of its
        # model rows: picking an image model is pointless if the provider it
        # belongs to has nothing left, and finding that out from a failed
        # generation is the worst way to learn it.
        q = _provider_quota_row(pid, p) if rows else None
        allow = _puter_allowance() if (rows and _is_puter_driver(pid) and
                                       pcfg.get("api_keys")) else None
        if allow:
            exhausted = allow["remaining"] <= 0
            quota_label = ("no credit left" if exhausted
                           else "%.2f¢ left" % allow["remaining_cents"])
        elif q:
            exhausted = bool(q.get("exhausted"))
            quota_label = "out of free quota" if exhausted else None
        else:
            exhausted, quota_label = False, None
        for row in rows:
            model = row["id"]
            models.append({
                "exhausted": exhausted,
                "quota_label": quota_label,
                "id": pid + "/" + model,
                "provider": pid,
                "model": model,
                "provider_name": p.get("name") or pid,
                "label": row.get("label") or model,
                "text_in_image": row.get("text_in_image"),
                # Carries the per-image price for metered models, which is what
                # the picker turns into its cost tag ("~0.5c per 1024x1024")
                # instead of a vague "costs credit".
                "notes": row.get("notes") or "",
                "free": row.get("free", True),
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


# --------------------------------------------------------------------------- #
# Zip-install auto-update: the same 5-hour self-heal as a git clone, for
# someone who downloaded "Download ZIP" from GitHub instead of cloning.
#
# git's dirty-tree skip ("don't clobber local edits") has no free equivalent
# without git, so this builds one: a manifest of {relpath: sha256} for every
# file that was part of the LAST update this code itself applied. Before
# applying a new one, every manifested file's CURRENT on-disk hash must still
# match — any mismatch means the user (or something else) touched a file
# since our last update, and the whole cycle is skipped, exactly like git
# skipping a dirty tree rather than force-pulling over it.
#
# On the very first check for a given zip install (no manifest yet) there is
# nothing to compare against — same trusted-baseline assumption a fresh git
# clone gets on day one: presumed unmodified until proven otherwise.
# --------------------------------------------------------------------------- #
_ZIP_UPDATE_URL = "https://github.com/last-million/free-llm-hub/archive/refs/heads/main.zip"
_ZIP_MANIFEST_PATH = os.path.join(_REPO_DIR, ".free-llm-hub-update-manifest.json")
# Never let a runtime/user artifact block the dirty-check or get shipped into
# the manifest — these are exactly what a git install's .gitignore also keeps
# out of the tree that `git status --porcelain` watches.
_ZIP_UPDATE_IGNORE_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules"}
_ZIP_UPDATE_IGNORE_FILES = {".free-llm-hub-update-manifest.json", ".calvoun-brief.md"}


def _zip_manifest_of(root_dir):
    """{relpath (forward-slash, sorted) -> sha256} for every real file under
    root_dir, skipping the runtime/ignore set above. Never raises — an
    unreadable file is simply left out (worst case it re-downloads next time)."""
    manifest = {}
    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames[:] = [d for d in dirnames if d not in _ZIP_UPDATE_IGNORE_DIRS
                       and not d.endswith(".pyc")]
        for fn in filenames:
            if fn in _ZIP_UPDATE_IGNORE_FILES or fn.endswith((".pyc", ".pyo")):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root_dir).replace(os.sep, "/")
            try:
                with open(full, "rb") as f:
                    manifest[rel] = hashlib.sha256(f.read()).hexdigest()
            except OSError:
                continue
    return manifest


def _zip_apply_needed(new_manifest):
    """True if copying new_manifest's files over _REPO_DIR would actually
    change anything. Deliberately checks ONLY new_manifest's own keys against
    disk (not a full-tree walk) — the live install also holds gitignored/
    local-only files (config.json, .hublog.*, ...) that are never part of the
    shipped zip and must never make this comparison look 'different' when the
    tracked source files are already identical."""
    for rel, expected_hash in new_manifest.items():
        full = os.path.join(_REPO_DIR, *rel.split("/"))
        try:
            with open(full, "rb") as f:
                actual_hash = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            return True
        if actual_hash != expected_hash:
            return True
    return False


def _zip_tree_is_dirty(old_manifest):
    """True if any file the LAST zip-update wrote has since changed on disk —
    the zip-install equivalent of `git status --porcelain` reporting dirty."""
    for rel, expected_hash in old_manifest.items():
        full = os.path.join(_REPO_DIR, *rel.split("/"))
        try:
            with open(full, "rb") as f:
                actual_hash = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            return True  # a file our own last update wrote is now missing/unreadable
        if actual_hash != expected_hash:
            return True
    return False


def _load_zip_manifest():
    try:
        with open(_ZIP_MANIFEST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _save_zip_manifest(manifest):
    with open(_ZIP_MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f)


def _manifest_fingerprint(manifest):
    """Short, stable label for a manifest -- the zip-update analogue of a git
    short SHA, used only for the human-readable status string/log lines."""
    blob = json.dumps(sorted(manifest.items())).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:7]


def _do_zip_update_check():
    """One zip-update cycle: download the latest source zip, skip if the local
    tree has diverged from what we last applied, otherwise overwrite and
    re-exec via the same _finish_update_apply tail the git path uses. Caller
    (_do_update_check) already holds _auto_update_lock."""
    old_manifest = _load_zip_manifest()
    if old_manifest and _zip_tree_is_dirty(old_manifest):
        _auto_update_state["last_result"] = (
            "skipped: local files differ from the last applied update")
        _log.warning("Zip auto-update: local tree diverged from the last applied "
                    "update — skipping, same as git's dirty-tree skip.")
        return _auto_update_state["last_result"]
    tmp_dir = None
    try:
        zip_path = os.path.join(tempfile.gettempdir(),
                                "free-llm-hub-update-%d.zip" % os.getpid())
        r = requests.get(_ZIP_UPDATE_URL, timeout=(10, 60))
        r.raise_for_status()
        with open(zip_path, "wb") as f:
            f.write(r.content)
        tmp_dir = tempfile.mkdtemp(prefix="free-llm-hub-update-")
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmp_dir)
        os.remove(zip_path)
        # GitHub's archive zip wraps everything in one '<repo>-<branch>/' folder.
        entries = [d for d in os.listdir(tmp_dir) if os.path.isdir(os.path.join(tmp_dir, d))]
        if len(entries) != 1:
            raise RuntimeError("unexpected archive layout: %r" % entries)
        src_root = os.path.join(tmp_dir, entries[0])
        new_manifest = _zip_manifest_of(src_root)
        if not new_manifest:
            raise RuntimeError("downloaded archive had no files")
        before_label = _manifest_fingerprint(old_manifest) if old_manifest else "initial"
        after_label = _manifest_fingerprint(new_manifest)
        # Checked against what's ACTUALLY on disk right now, not just the saved
        # manifest — on a fresh zip install's very first check there IS no saved
        # manifest yet, and treating that as "always different" would force an
        # unnecessary restart even when the download is byte-identical to what
        # is already installed.
        if not _zip_apply_needed(new_manifest):
            _save_zip_manifest(new_manifest)  # still record the baseline
            _auto_update_state["last_result"] = "up to date (%s)" % after_label
            return _auto_update_state["last_result"]
        # New/changed code. Copy every file over the live install — additive/
        # overwrite only, a file the new zip no longer ships is left in place
        # rather than deleted, the safer side of the same "never destroy more
        # than the update itself changed" principle git's ff-only pull already
        # gives for free.
        for rel in new_manifest:
            src = os.path.join(src_root, *rel.split("/"))
            dst = os.path.join(_REPO_DIR, *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
        _save_zip_manifest(new_manifest)
    except Exception as exc:
        _auto_update_state["last_result"] = "zip update failed: " + _sanitize(str(exc))[:160]
        _log.warning("Zip auto-update: failed: %s", exc)
        return _auto_update_state["last_result"]
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    deps_ok = _sync_deps_after_pull()
    return _finish_update_apply(before_label, after_label, deps_ok)


def _do_update_check():
    """One update cycle. Dispatches on install type — a git clone pulls via git,
    a plain zip/folder download self-updates via _do_zip_update_check — so BOTH
    install methods get the same 5-hour auto-heal, not just git users. Returns a
    short human status string (also stored in _auto_update_state)."""
    with _auto_update_lock:
        _auto_update_state["last_check"] = int(time.time())
        if _is_git_repo():
            return _do_git_update_check()
        return _do_zip_update_check()


def _do_git_update_check():
    """One pull cycle: skip if dirty, pull --ff-only, re-exec if HEAD moved.
    Caller (_do_update_check) already holds _auto_update_lock."""
    if not _origin_is_trusted():
        _auto_update_state["last_result"] = (
            "skipped: 'origin' is not the trusted last-million/free-llm-hub repo")
        _log.warning("Auto-update: refusing to pull — origin remote is untrusted.")
        return _auto_update_state["last_result"]
    # --untracked-files=no: only a modification to a TRACKED file can make
    # `pull --ff-only` clobber real work. Counting untracked files as "dirty"
    # meant one stray note, editor swapfile or stray log in the working tree
    # silently disabled auto-update FOREVER -- found live on 2026-08-30, where a
    # single untracked .md had been parking every 5-hourly check at
    # "skipped: local uncommitted changes" with nothing surfacing that the hub
    # had stopped updating itself. The narrow case this gives up (an incoming
    # commit adding a path that already exists untracked) still fails safely:
    # git refuses the merge and the rc2 != 0 branch below reports it.
    rc, dirty, _ = _git("status", "--porcelain", "--untracked-files=no")
    if rc == 0 and dirty:
        _auto_update_state["last_result"] = "skipped: local uncommitted changes"
        return _auto_update_state["last_result"]
    rc, before, _ = _git("rev-parse", "HEAD")
    # Name the remote and branch explicitly rather than relying on the branch's
    # upstream tracking. MEASURED 2026-08-30: a git-filter-repo run removes the
    # 'origin' remote by design, and re-adding it does NOT restore tracking --
    # so a bare `git pull --ff-only` died with "There is no tracking information
    # for the current branch" and auto-update was silently off. Nothing about
    # this repo's identity depends on that config: _origin_is_trusted() has
    # already checked the remote, and the branch is the one we are on.
    _rcb, _branch, _ = _git("rev-parse", "--abbrev-ref", "HEAD")
    _branch = (_branch or "").strip() or "main"
    rc2, _out, err = _git("pull", "--ff-only", "origin", _branch)
    if rc2 != 0:
        _auto_update_state["last_result"] = "pull failed: " + _sanitize(err)[:160]
        return _auto_update_state["last_result"]
    _rc, after, _ = _git("rev-parse", "HEAD")
    # Run every cycle, not only when before != after: a dependency install
    # that failed on a PRIOR cycle must keep retrying even once HEAD stops
    # moving (git pull is a no-op the moment it succeeded once), or a hub
    # stuck on stale deps would silently stop trying forever. Cheap when
    # nothing changed -- one hash compare against the stamp file.
    deps_ok = _sync_deps_after_pull()
    if not (before and after and before != after):
        _auto_update_state["last_result"] = "up to date (%s)" % (after[:7] if after else "?")
        return _auto_update_state["last_result"]
    return _finish_update_apply(before[:7], after[:7], deps_ok)


def _finish_update_apply(before_label, after_label, deps_ok):
    """Shared tail for BOTH install types once new code has actually landed on
    disk: handle a failed dependency sync, a user-stood-down hub, busy
    sessions, or the clear-to-restart case. Caller already holds
    _auto_update_lock."""
    if not deps_ok:
        _auto_update_state["last_result"] = (
            "updated %s->%s — restart deferred: dependency install failed, "
            "retrying next check" % (before_label, after_label))
        _log.warning("Auto-update: updated %s->%s but dependency install failed; "
                    "NOT re-executing into a broken environment.",
                    before_label, after_label)
        return _auto_update_state["last_result"]
    # New code landed. Normally we re-exec to apply it — but if the user has
    # deliberately switched the hub OFF as the default (hub-mode off), respect
    # that "leave me stood down" intent and do NOT respawn the process; the
    # update applies on the next manual restart instead.
    if _hub_mode_is_off():
        _auto_update_state["last_result"] = (
            "updated %s->%s — restart deferred (hub switched off as default)"
            % (before_label, after_label))
        _log.info("Auto-update: updated %s->%s but hub-mode is off; deferring re-exec.",
                 before_label, after_label)
        return _auto_update_state["last_result"]
    _auto_update_state["updating"] = True
    busy = _agentic_busy_session_ids()
    with _runtime_condition:
        inflight = _runtime_active[0]
    if not busy and not inflight:
        _auto_update_state["last_result"] = "updated %s->%s — restarting" % (before_label, after_label)
        _log.info("Auto-update: new code applied (%s -> %s), re-executing.",
                 before_label, after_label)
        _reexec_soon()
    else:
        _auto_update_state["last_result"] = (
            "updated %s->%s — restart deferred: %d task(s) still running"
            % (before_label, after_label, len(busy) + (1 if inflight else 0)))
        _log.info("Auto-update: updated %s->%s but %d session(s)/%d inflight request(s) "
                 "busy; deferring restart until they finish.",
                 before_label, after_label, len(busy), inflight)
        _reexec_when_idle(busy)
    return _auto_update_state["last_result"]


def _sync_deps_after_pull():
    """Best-effort `pip install -r requirements.txt`, run ONLY when the pulled
    tree's hash differs from the last install (same stamp file and hash
    convention run.bat/run.sh already use for a fast plain start, so a normal
    pull with no dependency change costs one hash compare, not a pip round
    trip). Returns True if it is now safe to re-exec (deps match what HEAD
    needs), False if a required install failed.

    WHY THIS EXISTS: _do_update_check() pulls new code and os.execv's into it,
    but execv keeps the SAME already-imported interpreter/site-packages --
    nothing else in this file ever re-ran pip. A commit that adds a new
    dependency would pull clean, then crash the very next line on import,
    taking down an auto-updating hub that was working fine before the pull."""
    try:
        req_path = os.path.join(_REPO_DIR, "requirements.txt")
        stamp_path = os.path.join(_REPO_DIR, ".venv", ".deps-stamp")
        with open(req_path, "rb") as f:
            h = hashlib.sha256(f.read()).hexdigest()
        try:
            with open(stamp_path, "r", encoding="utf-8") as f:
                current = f.read().strip()
        except OSError:
            current = None
        if current == h:
            return True
        _log.info("Auto-update: requirements.txt changed, installing before restarting.")
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", req_path],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            _log.error("Auto-update: pip install failed, deferring restart: %s",
                      _sanitize((r.stderr or "")[:300]))
            return False
        os.makedirs(os.path.dirname(stamp_path), exist_ok=True)
        with open(stamp_path, "w", encoding="utf-8") as f:
            f.write(h)
        return True
    except Exception as exc:
        _log.error("Auto-update: dependency sync failed, deferring restart: %s", exc)
        return False


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


def _agentic_busy_session_ids():
    """Agent chat session ids currently mid-turn (turn_lock held), snapshotted
    right now. A re-exec kills every in-flight connection outright (no HTTP
    response, no SSE event) -- an active agentic turn can legitimately run up
    to _TURN_TIMEOUT (1800s), so restarting out from under one is exactly the
    "stopped my work" a user would notice and be annoyed by."""
    ids = set()
    for sid, sess in list(agentic_chat._REGISTRY.items()):
        try:
            if sess.turn_lock.locked():
                ids.add(sid)
        except Exception:                                          # noqa: BLE001
            continue
    return ids


def _reexec_when_idle(busy_snapshot):
    """Restart once every session busy AT UPDATE-CHECK TIME (plus any request
    already in flight on /v1/*) has finished -- and not a moment later, even
    if the hub stays continuously busy. Deliberately does NOT wait for
    sessions that START after the snapshot: an always-on hub could otherwise
    defer forever and the pulled code would never actually apply."""
    def _go():
        while True:
            still_busy = {sid for sid in busy_snapshot
                          if sid in agentic_chat._REGISTRY
                          and agentic_chat._REGISTRY[sid].turn_lock.locked()}
            with _runtime_condition:
                inflight = _runtime_active[0]
            if not still_busy and not inflight:
                break
            _auto_update_state["last_result"] = (
                "update pulled — restart deferred: %d task(s) still running"
                % (len(still_busy) + (1 if inflight else 0)))
            time.sleep(3.0)
        _log.info("Auto-update: deferred restart proceeding — snapshotted tasks are done.")
        _reexec_soon()
    threading.Thread(target=_go, daemon=True, name="freehub-update-wait").start()


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
    # Say it out loud when routing is NOT picking the best model. This flag has
    # no UI: it sat False in a real config with no way for anyone to have set it
    # deliberately, and the only symptom was "why does chat not use the good
    # models" -- a question that took a routing trace to answer. A line in the
    # banner turns that into something visible.
    if not config.get_flag("route_always_best", True):
        print("  Routing:     SPREADING across the top band, not always-best.")
        print("               Every medium/hard turn may land on a weaker model")
        print("               to stretch quota. Undo: route_always_best=true in")
        print("               ~/.free-llm-hub/config.json")
        print("=" * 74)
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
    _init_quota_persistence()      # restore quota/dead-model state from the last run
    # Encrypt any provider keys still stored in plaintext. A no-op once done, so
    # an ordinary start does not rewrite the config file for nothing.
    try:
        _migrated = config.encrypt_existing_secrets()
        if _migrated:
            _log.info("encrypted %d provider key(s) at rest", _migrated)
    except Exception as _exc:                                    # noqa: BLE001
        _log.warning("could not encrypt stored keys: %s", _exc)
    _print_banner()
    _start_auto_update()
    _start_aa_refresh()
    # Previews that outlived the hub that started them (crash, kill, restart
    # while a project was running) keep holding their ports forever. Found 99
    # of the 100 held on this machine, which makes the next preview fail with
    # "no free port". Only the hub's own range is swept.
    try:
        freed = workspace.sweep_own_range()
        if freed:
            _log.info("reclaimed %d preview port(s) left by a previous run", freed)
    except Exception as exc:                                     # noqa: BLE001
        _log.warning("could not sweep preview ports: %s", exc)
    workspace.start_reaper()   # stop previews nobody is watching
    _load_perf_stats()         # measured reliability/latency from previous runs
    _maybe_auto_create_desktop_shortcut()
    _start_agent_cli_autoinstall()
    vision_status.start_heartbeat()
    server = make_server(HOST, PORT, app, threaded=True)
    _runtime_server[0] = server
    try:
        server.serve_forever()
    finally:
        _runtime_server[0] = None
        server.server_close()
        # Flush what this run learned. The throttled saves during the run keep
        # a crash cheap; this makes a clean stop (or an auto-update re-exec)
        # lose nothing at all.
        _save_perf_stats(force=True)
