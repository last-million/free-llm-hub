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
import sys
import threading
import time
import uuid

import requests
from flask import Flask, Response, jsonify, render_template, request, stream_with_context

try:
    from jinja2 import TemplateNotFound
except Exception:  # pragma: no cover - jinja2 always ships with flask
    class TemplateNotFound(Exception):
        pass

import config
import providers as prov

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
CHAT_READ_TIMEOUT = 300       # seconds (long generations)
MODELS_READ_TIMEOUT = 10      # seconds (model discovery / key tests)
MODEL_CACHE_TTL = 60          # seconds
MAX_HOPS = 3                  # primary + up to 2 fallback providers

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


def _resolve_model(model):
    """'<pid>/<model>' -> (pid, model); bare -> default provider.
    Returns (pid, model_id) or (None, error_message)."""
    model = model if isinstance(model, str) else ""
    model = model.strip()
    if "/" in model:
        head, rest = model.split("/", 1)
        if prov.get_provider(head):
            return head, rest
    default = config.get_default()
    if not default or not default.get("provider") or not default.get("model"):
        return None, ("No default provider/model configured. Set one on the "
                      "dashboard (or POST /api/default), or request a model as "
                      "'<provider>/<model>'.")
    pid = default["provider"]
    # Bare claude-* names (Claude Code's built-in defaults / small fast model)
    # route to the configured default model on the default provider.
    if not model or model.lower().startswith("claude"):
        return pid, default["model"]
    return pid, model


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


def _build_chain(primary_pid, model_id):
    """[(pid, model)] -- primary first, then fallback providers with a
    comparable free model. Capped at MAX_HOPS."""
    chain = [(primary_pid, model_id)]
    for pid in _enabled_keyed():
        if len(chain) >= MAX_HOPS:
            break
        if pid == primary_pid:
            continue
        free = provider_free_models(pid)
        if not free:
            continue
        alt = model_id if model_id in free else _comparable_model(model_id, free)
        if alt and prov.is_model_allowed(alt):
            chain.append((pid, alt))
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
                timeout=(CONNECT_TIMEOUT, CHAT_READ_TIMEOUT),
            )
        except requests.RequestException as exc:
            last_exc = exc
            if is_last:
                raise
            continue
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
    with _model_cache_lock:
        _model_cache.pop(pid, None)  # pool changed -> rediscover
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
    model = _suggested_model()
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
    return jsonify({
        "providers_enabled": len(_enabled_keyed()),
        "has_default": bool(default and default.get("provider") and default.get("model")),
        "local_api_key_set": bool(config.get_local_api_key()),
        "connect_snippets": _connect_snippets(),
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
        base = os.environ.get("LOCALAPPDATA") or os.path.join(_home(), "AppData", "Local")
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
        "config_paths": [os.path.join(_home(), ".codex", "config.toml")],
        "env_check": ["OPENAI_BASE_URL", "OPENAI_API_BASE"],
        "autofix": None,  # TOML — no safe stdlib writer/merger, so manual only
        "default_method": "config",
        "hint": "Installed. Needs a manual [model_providers] block in ~/.codex/config.toml (see instructions).",
        "manual_note": (
            "Codex uses ~/.codex/config.toml (TOML — not auto-edited here). Add:\n"
            "  [model_providers.freehub]\n"
            "  name = \"Free LLM Hub\"\n"
            "  base_url = \"http://127.0.0.1:%d/v1\"\n"
            "  env_key = \"OPENAI_API_KEY\"\n"
            "then set  model_provider = \"freehub\"  and  model = \"<provider>/<model>\"  at the top "
            "level, and export OPENAI_API_KEY (below)." % PORT
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
            "UNCERTAIN / likely incompatible: Google's Gemini CLI targets the native Gemini API, not "
            "an OpenAI- or Anthropic-shaped endpoint, so this hub can't serve it as-is. Even builds that "
            "honor GOOGLE_GEMINI_BASE_URL still expect Google's wire format. Use the Qwen Code CLI (an "
            "OpenAI-compatible Gemini-CLI fork) instead if you want a Gemini-CLI-style tool on this hub."
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


def _hub_fragments():
    """Substrings that, if present in a CLI's config/env, mean it points here.
    The PORT is the discriminator, so this matches both the bare origin and the
    /v1 form (http://127.0.0.1:<PORT>, http://127.0.0.1:<PORT>/v1, ...)."""
    return ["127.0.0.1:%d" % PORT, "localhost:%d" % PORT, "[::1]:%d" % PORT]


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
    """Shell one-liners to set env vars persistently (per-CLI, NOT a profile edit
    we make for the user — we only *print* these for them to run)."""
    win = "\n".join('setx %s "%s"' % (k, v) for k, v in env.items())
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


def _autofix_claude(entry, key, base_root, base_v1, model):
    path = _p_claude()
    data = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {"ok": False, "reason": "existing %s is not valid JSON — fix or remove it, then retry."
                    % _short(path)}
        if not isinstance(data, dict):
            return {"ok": False, "reason": "existing %s is not a JSON object; not overwriting." % _short(path)}
    backup = _backup_once(path)
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
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {"ok": False, "reason": ("existing %s is not valid JSON (jsonc comments aren't "
                    "auto-merged) — configure it by hand, then retry." % _short(path))}
        if not isinstance(data, dict):
            return {"ok": False, "reason": "existing %s is not a JSON object; not overwriting." % _short(path)}
    backup = _backup_once(path)
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
    _cli_write_text(path, _merge_dotenv(path, updates))
    return {
        "ok": True,
        "wrote_path": path,
        "backup_path": backup,
        "applied": {"OPENAI_API_BASE": base_v1, "OPENAI_BASE_URL": base_v1,
                    "OPENAI_API_KEY": _mask_key(key), "OPENAI_MODEL": model},
        "restart_hint": "Re-run `qwen` in a new terminal; it loads ~/.qwen/.env on startup.",
    }


_AUTOFIXERS = {
    "claude": _autofix_claude,
    "aider": _autofix_aider,
    "opencode": _autofix_opencode,
    "qwen": _autofix_qwen,
}


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
        # env-only / TOML / uncertain: never touch a global shell profile —
        # hand back copy-paste commands + a CLI-specific note instead.
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


@app.route("/api/clis/<cid>/instructions", methods=["GET"])
def api_cli_instructions(cid):
    entry = _get_cli_entry(cid)
    if not entry:
        return jsonify({"error": "unknown CLI '%s'" % cid}), 404
    key = config.get_local_api_key() or "free-llm-hub"
    model = _first_free_model_id() or _suggested_model()
    base_root = "http://127.0.0.1:%d" % PORT
    base_v1 = base_root + "/v1"
    env = _manual_env(entry, key, base_root, base_v1, model)
    snippets = _connect_snippets()
    steps = []
    if entry.get("autofix"):
        steps.append("Auto-fix (recommended): POST /api/clis/%s/autofix to write %s for you "
                     "(a .freehub-bak backup is made first)." % (entry["id"], _short(entry.get("write_path", "the CLI config"))))
    steps.append("Manual: set the environment variables in `env` below, then restart the CLI.")
    if entry.get("manual_note"):
        steps.append(entry["manual_note"])
    steps.append("Verify: run `%s` and confirm it answers via this hub using %s." % (entry["bins"][0], model))
    return jsonify({
        "steps": steps,
        "env": env,
        "snippet_openai": snippets["openai"],
        "snippet_anthropic": snippets["claude_code"],
    })


# ---------------------------------------------------------------------------
# OpenAI-compatible gateway
# ---------------------------------------------------------------------------

@app.route("/v1/models", methods=["GET"])
def v1_models():
    data = [{"id": m["id"], "object": "model", "created": 0, "owned_by": m["provider"]}
            for m in aggregated_models()]
    return jsonify({"object": "list", "data": data})


def _proxy_sse(resp):
    """Pass upstream SSE bytes through unchanged."""
    try:
        for chunk in resp.iter_content(chunk_size=None):
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
    for hop_pid, hop_model in _build_chain(pid, resolved):
        if not prov.is_model_allowed(hop_model):
            continue
        payload = dict(body)
        payload["model"] = hop_model
        try:
            resp = _upstream_chat(hop_pid, payload, stream)
        except (requests.RequestException, RuntimeError) as exc:
            errors.append("%s: %s" % (hop_pid, _sanitize(exc.__class__.__name__)))
            continue
        if _retryable(resp.status_code):
            errors.append("%s: HTTP %d" % (hop_pid, resp.status_code))
            resp.close()
            continue
        if stream and resp.status_code == 200:
            return Response(stream_with_context(_proxy_sse(resp)),
                            mimetype="text/event-stream", headers=_SSE_HEADERS)
        # Non-stream success, or a non-retryable upstream error: relay it.
        try:
            data = resp.json()
        except ValueError:
            return _openai_error("Upstream returned non-JSON (%s, HTTP %d): %s"
                                 % (hop_pid, resp.status_code, _sanitize(resp.text)),
                                 502, "upstream_error")
        if resp.status_code == 200 and isinstance(data, dict):
            data["model"] = hop_pid + "/" + hop_model
        return jsonify(data), resp.status_code
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


def _anthropic_stream(resp, model_str, input_tokens):
    """Translate an upstream OpenAI SSE stream into the Anthropic event
    sequence: message_start -> content_block_start -> content_block_delta* ->
    content_block_stop -> message_delta -> message_stop."""
    msg_id = "msg_" + uuid.uuid4().hex
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

        for raw in resp.iter_lines(decode_unicode=False):
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
    for hop_pid, hop_model in _build_chain(pid, resolved):
        if not prov.is_model_allowed(hop_model):
            continue
        payload = dict(base_payload)
        payload["model"] = hop_model
        payload["stream"] = stream
        try:
            resp = _upstream_chat(hop_pid, payload, stream)
        except (requests.RequestException, RuntimeError) as exc:
            errors.append("%s: %s" % (hop_pid, _sanitize(exc.__class__.__name__)))
            continue
        if _retryable(resp.status_code):
            errors.append("%s: HTTP %d" % (hop_pid, resp.status_code))
            resp.close()
            continue
        model_str = requested_model or (hop_pid + "/" + hop_model)
        if resp.status_code != 200:
            detail = _upstream_error_detail(resp)
            status = resp.status_code if 400 <= resp.status_code < 500 else 502
            return _anthropic_error("api_error",
                                    "Upstream %s error (HTTP %d): %s"
                                    % (hop_pid, resp.status_code, detail), status)
        if stream:
            return Response(stream_with_context(
                _anthropic_stream(resp, model_str, input_est)),
                mimetype="text/event-stream", headers=_SSE_HEADERS)
        try:
            data = resp.json()
        except ValueError:
            return _anthropic_error("api_error",
                                    "Upstream %s returned non-JSON." % hop_pid, 502)
        return jsonify(_openai_resp_to_anthropic(data, model_str))
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
    app.run(host=HOST, port=PORT, threaded=True, debug=False)
