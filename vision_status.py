"""Calvoun Free LLM Hub -- vision-model-gap detection, shared by app.py (the
GET /api/agent/vision-status route) and agentic_chat.py (the --append-
system-prompt vision-gap notice, see that module's _system_prompt_addition()).

Deliberately a NEW, standalone LEAF module (imports only `config` + `providers`,
both of which import nothing from this project) rather than a helper living in
app.py: app.py already imports agentic_chat.py, so agentic_chat.py must never
import app.py back (see agentic_chat.py's own module docstring on this exact
constraint for _agentic_env()/_secret_values()). Putting the shared logic here
lets BOTH callers import ONE real implementation instead of duplicating it.

"Available" means: at least one provider is currently enabled, has a usable
key (a non-empty api_keys pool, or no key needed at all -- see providers.py's
`no_key` flag), AND carries a non-empty, VERIFIED `vision_models` list in the
registry (providers.py) -- the same verified-list contract app.py's own
_vision_model_ids()/_vision_candidates() already rely on for image routing.
This check does NOT additionally require free quota to be unspent or the
model to currently answer (unlike app.py's _vision_candidates(), which also
quota- and dead-model-filters for actual image routing) -- this is a coarser
"is vision even configured at all" signal for the agentic-chat notice/badge,
not a routing decision.

status() always RECOMPUTES fresh on every call: the underlying check is pure
local state (config.json + the in-process provider registry, no network), so
there is no real staleness cost to paying it on every read. The heartbeat
thread's only job is to keep detecting the unavailable->available FLIP (and
stamp `vision_became_available_at`) even when nobody is polling the route for
a while -- see start_heartbeat(). This reuses the SAME "spawn one daemon
thread that loops with a fixed interval" pattern as app.py's existing
auto-update thread (_auto_update_loop/_start_auto_update), per the task spec.

NOT built in this pass (explicitly scoped down, per the task): auto-resuming
a specific paused conversation when vision becomes available. That needs
infra beyond this stage (which conversation, which session, is it still
open) -- the heartbeat only flips an in-memory flag the frontend/agent can
react to.
"""
from __future__ import annotations

import os
import threading
import time

import config
import providers as prov

# --------------------------------------------------------------------------- #
# Core check
# --------------------------------------------------------------------------- #

def _provider_qualifies(pid: str) -> bool:
    """True iff `pid` is enabled, usably keyed, and has >=1 verified vision model."""
    row = config.get_provider_config(pid)
    if not row.get("enabled"):
        return False
    p = prov.get_provider(pid) or {}
    needs_key = not p.get("no_key")
    if needs_key and not row.get("api_keys"):
        return False
    vision_models = [m for m in (p.get("vision_models") or []) if isinstance(m, str) and m]
    return bool(vision_models)


def qualifying_providers() -> list:
    """Enabled+keyed provider ids that carry a non-empty vision_models list."""
    return [p["id"] for p in prov.list_providers() if _provider_qualifies(p["id"])]


# --------------------------------------------------------------------------- #
# In-memory state + flip detection
# --------------------------------------------------------------------------- #

_STATE_LOCK = threading.Lock()
_STATE = {
    "available": False,
    "providers": [],
    "vision_became_available_at": None,  # epoch seconds of the CURRENT streak's start, or None
    "last_checked": None,
}


def _recompute() -> dict:
    pids = qualifying_providers()
    now = time.time()
    with _STATE_LOCK:
        was_available = _STATE["available"]
        _STATE["available"] = bool(pids)
        _STATE["providers"] = pids
        _STATE["last_checked"] = now
        if _STATE["available"] and not was_available:
            _STATE["vision_became_available_at"] = now
        elif not _STATE["available"]:
            _STATE["vision_became_available_at"] = None
        return dict(_STATE)


def status() -> dict:
    """{'available': bool, 'providers': [pid,...], 'vision_became_available_at':
    epoch|None, 'last_checked': epoch|None}. Never raises; a probe failure
    (should not happen -- pure local reads) surfaces as unavailable rather
    than crashing whatever is asking (a route, or an agentic-chat turn)."""
    try:
        return _recompute()
    except Exception:
        with _STATE_LOCK:
            return dict(_STATE)


# --------------------------------------------------------------------------- #
# Heartbeat -- same daemon-thread-with-fixed-interval shape as app.py's
# existing _auto_update_loop/_start_auto_update, reused rather than
# reinvented (see task spec).
# --------------------------------------------------------------------------- #

_HEARTBEAT_INTERVAL_MINUTES = float(
    os.environ.get("VISION_HEARTBEAT_INTERVAL_MINUTES", "45") or "45")
_heartbeat_thread = None
_heartbeat_start_lock = threading.Lock()


def _heartbeat_loop():
    interval = max(1.0, _HEARTBEAT_INTERVAL_MINUTES) * 60.0
    while True:
        time.sleep(interval)
        try:
            _recompute()
        except Exception:
            pass


def start_heartbeat():
    """Idempotent: only ever spawns one background thread per process."""
    global _heartbeat_thread
    with _heartbeat_start_lock:
        if _heartbeat_thread is not None:
            return
        _recompute()  # populate real state immediately rather than defaults
        _heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
        _heartbeat_thread.start()
