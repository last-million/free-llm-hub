"""
Free LLM Hub — per-provider free-quota tracking.

Counts upstream requests per provider inside that provider's free-tier window
(minute / day / month), reports how many remain and when the window resets, and
records a hard throttle when a provider returns HTTP 429. Consumed by app.py to
(a) skip exhausted providers during orchestration and (b) drive the red
"no free quota left" dashboard banner + per-provider reset countdowns.

Best-effort + approximate: the limits below are conservative public free-tier
figures (July 2026), NOT billing-accurate. Unknown providers get a generous
default so the tracker never blocks a provider we have no data for. In-memory
only (resets when the hub restarts) — a single local user doesn't need more.

Pure stdlib: calendar, threading, time.
"""
from __future__ import annotations

import calendar
import threading
import time

# provider id -> {"limit": int, "window": "minute"|"day"|"month"}
# Conservative public free-tier figures. Tune freely; unknowns use DEFAULT_LIMIT.
FREE_LIMITS = {
    "groq":          {"limit": 1000,  "window": "day"},
    "cerebras":      {"limit": 14400, "window": "day"},
    "nvidia":        {"limit": 40,    "window": "minute"},
    "google":        {"limit": 200,   "window": "day"},
    "openrouter":    {"limit": 50,    "window": "day"},
    "mistral":       {"limit": 500,   "window": "day"},
    "sambanova":     {"limit": 300,   "window": "day"},
    "morph":         {"limit": 200,   "window": "month"},
    "github-models": {"limit": 150,   "window": "day"},
    "huggingface":   {"limit": 300,   "window": "day"},
    "cohere":        {"limit": 1000,  "window": "month"},
    "deepseek":      {"limit": 500,   "window": "day"},
    "chutes":        {"limit": 200,   "window": "day"},
    "scaleway":      {"limit": 300,   "window": "day"},
    "ovh":           {"limit": 300,   "window": "day"},
}
DEFAULT_LIMIT = {"limit": 200, "window": "day"}

_LOCK = threading.RLock()
# pid -> {"count": int, "window_start": float, "throttled_until": float}
_STATE: dict = {}
# pid -> {"window_start": float, "models": {model_id: count}}  (per-model usage)
_MODEL_STATE: dict = {}


def _limit_for(pid: str) -> dict:
    return FREE_LIMITS.get(pid, DEFAULT_LIMIT)


def _window_bounds(window: str, now: float):
    """(start_epoch, reset_epoch) for the window CONTAINING `now`, in UTC."""
    if window == "minute":
        start = now - (now % 60)
        return start, start + 60
    tm = time.gmtime(now)
    if window == "month":
        start = calendar.timegm((tm.tm_year, tm.tm_mon, 1, 0, 0, 0, 0, 0, 0))
        if tm.tm_mon == 12:
            reset = calendar.timegm((tm.tm_year + 1, 1, 1, 0, 0, 0, 0, 0, 0))
        else:
            reset = calendar.timegm((tm.tm_year, tm.tm_mon + 1, 1, 0, 0, 0, 0, 0, 0))
        return start, reset
    # default: day (UTC midnight -> next UTC midnight)
    start = calendar.timegm((tm.tm_year, tm.tm_mon, tm.tm_mday, 0, 0, 0, 0, 0, 0))
    return start, start + 86400


def record(pid: str, model: str = None, n: int = 1) -> None:
    """Count `n` upstream requests against pid's current window (auto rolls over).
    If `model` is given, ALSO count it per-model so the dashboard can show usage
    per provider AND per model."""
    lim = _limit_for(pid)
    now = time.time()
    start, _reset = _window_bounds(lim["window"], now)
    with _LOCK:
        st = _STATE.get(pid)
        if not st or st.get("window_start") != start:
            st = {"count": 0, "window_start": start,
                  "throttled_until": (st.get("throttled_until", 0) if st else 0)}
        st["count"] = st.get("count", 0) + n
        _STATE[pid] = st
        if isinstance(model, str) and model:
            ms = _MODEL_STATE.get(pid)
            if not ms or ms.get("window_start") != start:
                ms = {"window_start": start, "models": {}}
            ms["models"][model] = ms["models"].get(model, 0) + n
            _MODEL_STATE[pid] = ms


def models(pid: str) -> dict:
    """{model_id: used_count} for pid's CURRENT window (only models actually hit).
    Empty once the window rolls over."""
    lim = _limit_for(pid)
    now = time.time()
    start, _reset = _window_bounds(lim["window"], now)
    with _LOCK:
        ms = _MODEL_STATE.get(pid)
        if not ms or ms.get("window_start") != start:
            return {}
        return dict(ms.get("models") or {})


def mark_throttled(pid: str, seconds: float = None) -> None:
    """Provider returned 429 -> treat as exhausted until the window resets (or for
    `seconds` if the provider sent a Retry-After). Also pegs `used` to the limit so
    `remaining` reads 0 immediately."""
    lim = _limit_for(pid)
    now = time.time()
    start, reset = _window_bounds(lim["window"], now)
    until = now + seconds if seconds else reset
    with _LOCK:
        st = _STATE.get(pid)
        if not st or st.get("window_start") != start:
            st = {"count": lim["limit"], "window_start": start, "throttled_until": 0}
        else:
            st["count"] = max(st.get("count", 0), lim["limit"])
        st["throttled_until"] = max(st.get("throttled_until", 0), until)
        _STATE[pid] = st


def status(pid: str) -> dict:
    """{used, limit, remaining, window, resets_in, resets_at, throttled, exhausted}."""
    lim = _limit_for(pid)
    now = time.time()
    start, reset = _window_bounds(lim["window"], now)
    with _LOCK:
        st = _STATE.get(pid)
        used = st["count"] if st and st.get("window_start") == start else 0
        throttled_until = (st.get("throttled_until", 0) if st else 0)
    throttled = throttled_until > now
    remaining = max(0, lim["limit"] - used)
    exhausted = throttled or remaining <= 0
    reset_at = max(reset, throttled_until) if throttled else reset
    return {
        "used": used, "limit": lim["limit"], "remaining": remaining,
        "window": lim["window"], "resets_in": max(0, int(reset_at - now)),
        "resets_at": int(reset_at), "throttled": throttled, "exhausted": exhausted,
    }


def is_exhausted(pid: str) -> bool:
    return status(pid)["exhausted"]
