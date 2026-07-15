"""
Free LLM Hub — per-provider free-quota tracking.

Counts upstream requests per provider inside that provider's free-tier window
(minute / day / month), reports how many remain and when the window resets, and
records a hard throttle when a provider returns HTTP 429. Consumed by app.py to
(a) skip exhausted providers during orchestration and (b) drive the red
"no free quota left" dashboard banner + per-provider reset countdowns.

Not billing-accurate, but no longer guesswork: every figure below was researched
against the provider's own docs / live catalog (July 2026) and carries the
research confidence inline. A limit of 0 means "NO FREE TIER" — a documented
zero, not an unknown. A provider we have no figure for is tracked as UNKNOWN
(see DEFAULT_LIMIT) and is never assumed to have a free budget. In-memory only
(resets when the hub restarts) — a single local user doesn't need more.

Pure stdlib: calendar, threading, time.
"""
from __future__ import annotations

import calendar
import threading
import time

# provider id -> {"limit": int, "window": "minute"|"day"|"month"}
#
# RESEARCHED 2026-07-15 (one agent per provider, official docs + live catalogs).
# Confidence per row:
#   high   — read verbatim from the provider's own documentation
#   medium — official but derived, single-sourced, or internally odd
#   low    — NO official figure exists; kept only where a legacy number is
#            already in place, and marked UNVERIFIED so it isn't mistaken for fact
#
# limit: 0 == NO FREE TIER (documented). Those providers bill (or 402) on every
# call, so they get zero free budget and status() reports them exhausted.
# NEVER delete a 0 row to "clean up": a provider absent from this dict falls back
# to DEFAULT_LIMIT, which is the exact bug the explicit 0 exists to prevent.
FREE_LIMITS = {
    # ── genuinely free tiers (researched) ───────────────────────────────────
    "groq":          {"limit": 1000,  "window": "day"},     # high: RPD floor across free chat models (llama-3.1-8b-instant is 14.4k/day)
    "cerebras":      {"limit": 14400, "window": "day"},     # high: 30 req/min, 900/hour, 14,400 req/day, 1M tok/day (per-model, gpt-oss-120b & Llama-3.1-8B). CORRECTED BACK from a wrong 5/minute: a research pass claimed no daily cap existed and that 14400 was "stale Groq cross-contamination" — it is not. Cerebras really is 14,400/day; Groq's llama-3.1-8b sharing that exact number is the coincidence that caused the mistake. Cross-checked against cheahjs/free-llm-api-resources (MIT). The 30/min burst limit is handled by the 429 -> 60s cooldown path, not by this daily budget.
    "openrouter":    {"limit": 50,    "window": "day"},     # high: 50/day TOTAL across all ':free' models (1000/day after a one-time $10 top-up)
    "github-models": {"limit": 150,   "window": "day"},     # high: Copilot Free low-tier RPD (high-tier ids are 50/day, deepseek-r1 8/day — separate buckets)
    "sambanova":     {"limit": 20,    "window": "day"},     # medium: official Free Tier table = 20 RPM / 20 RPD / 200k TPD (was 300/day = 15x too high)
    "modelscope":    {"limit": 2000,  "window": "day"},     # high: 2,000 calls/day per account, sub-cap 500/model/day (was absent -> self-throttled at 10% of real capacity)
    "siliconflow":   {"limit": 100,   "window": "day"},     # medium: cap for accounts without Chinese real-name verification (实名认证), which most hub users cannot complete
    "nararouter":    {"limit": 10,    "window": "minute"},  # high: Free plan = 10 req/min (its own pricing page). The real budget is 6M TOKENS/day (resets 07:00 WIB) which a request counter can't express, so we track the documented REQUEST rate — the limit a caller actually trips first.
    "google":        {"limit": 200,   "window": "day"},     # UNVERIFIED (low): Google no longer publishes a free-tier RPD table; best third-party figure is ~250/day for 2.5-flash and sources conflict. 200 kept as the conservative legacy value.
    "mistral":       {"limit": 500,   "window": "day"},     # UNVERIFIED (low): Mistral deliberately publishes NO free-tier figure (per-org, Admin Console only). Real shape is req/SEC + tok/min + tok/month — there is no documented req/day cap. Legacy number; do not cite it as fact.

    # ── NO FREE TIER — documented zeros. Every call costs money (or 402s). ───
    # Kept as explicit rows (not deleted) so they can never inherit DEFAULT_LIMIT.
    "kimi":          {"limit": 0,     "window": "day"},     # high: docs verbatim "There is no free tier... recharge at least $1 to start using."
    "minimax":       {"limit": 0,     "window": "day"},     # high: no free tier in any official doc; pay-per-token or subscription only. FAILS UNSAFELY (200 OK + a bill).
    "together":      {"limit": 0,     "window": "day"},     # high: all four '-Free' serverless endpoints were removed during 2025 (last 2025-12-23)
    "chutes":        {"limit": 0,     "window": "day"},     # high: free tier fully retired 2026-03-15 (the old 200/day Early Access always required a $5 deposit). FAILS UNSAFELY.
    "huggingface":   {"limit": 0,     "window": "month"},   # high: allowance is $0.10/MONTH of credits at full rates (~17 req on GLM-5.2), not a request count. is_free:true matches 0 of 102 router models. FAILS UNSAFELY.
    "scaleway":      {"limit": 0,     "window": "day"},     # high: card mandatory before the first call; one-time 1M-token allowance then silent billing. The old "300/day" was the PAID 300/MINUTE figure with the window swapped. FAILS UNSAFELY.
    "deepseek":      {"limit": 0,     "window": "day"},     # high: 2 models, both paid. The old 500/day was mis-derived from the v4-pro CONCURRENCY limit; no RPD/RPM is published anywhere.
    "nebius":        {"limit": 0,     "window": "day"},     # high: $1 trial credit, 30 days, bank card required at onboarding; no $0 models
    "xiaomi":        {"limit": 0,     "window": "day"},     # high: chat/LLM is pay-as-you-go or paid Token Plans; the only $0 models are TTS (non-chat, "limited time")
    "nvidia":        {"limit": 0,     "window": "day"},     # medium: TRIAL, not a tier — a LIFETIME 1,000-credit budget (max 5,000, 90-day expiry) returning 402 "Cloud credits expired". The old 40/minute was a real rate baseline but never the binding constraint; a window counter cannot express a finite consumable balance.
    "morph":         {"limit": 0,     "window": "month"},   # medium: the official "200 req free every month" headline meters TOKENS (250K/mo = $2.50); a coding CLI's 20-50K-token turns make the real allowance ~5-12 req/month, so 200 reported quota long after Morph starts rejecting.
    "agentrouter":   {"limit": 0,     "window": "day"},     # medium: consumable $100/$200 signup credits; publishes no rate limits anywhere and /v1/models 401s without a key
    "qwen":          {"limit": 0,     "window": "day"},     # medium: consumable 1M-tokens-PER-MODEL / 90-day trial, then AllocationQuota.FreeTierOnly on every call. (Documented rate is 600 RPM with no per-day cap.)

    # ── legacy rows, NOT researched, and both are DEAD KEYS ─────────────────
    # Neither id exists in providers.PROVIDERS, so neither row has ever applied:
    # there is no "cohere" provider, and OVHcloud's provider id is "ovhcloud".
    # Left as-is rather than silently activating an unverified figure on
    # ovhcloud — it now tracks as UNKNOWN via DEFAULT_LIMIT instead.
    "cohere":        {"limit": 1000,  "window": "month"},
    "ovh":           {"limit": 300,   "window": "day"},
}

# UNKNOWN provider — deliberately NOT a free budget.
#
# Was {"limit": 200, "window": "day"}: every provider missing from FREE_LIMITS
# silently inherited a fabricated 200 free requests/day that nobody researched,
# and the dashboard reported it as fact. That is exactly backwards for
# trial-credit providers, where those 200 "free" calls are billable.
#
# `limit: None` means "no figure": usage is still counted (so the dashboard can
# show what was actually spent), but status() never claims a `remaining` count
# and never self-throttles on an invented number. Such a provider is sidelined
# ONLY by a real upstream 429 (mark_throttled) — so an unlisted-but-genuinely-
# free provider keeps working instead of being disabled by a made-up ceiling.
# status() exposes `limit_known: False` so callers can render "unknown" rather
# than a number. Add a researched row above to give a provider a real budget.
DEFAULT_LIMIT = {"limit": None, "window": "day", "unknown": True}

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
    """Provider returned 429.

    - `seconds` given (a Retry-After value OR the hub's short default cooldown):
      sideline the provider for JUST that long and do NOT peg `used`. A per-minute
      burst 429 must not read as 'daily/monthly budget spent' for the rest of the
      window — once the short throttle lifts the provider is usable again.
    - `seconds` is None: treat it as full-window exhaustion — peg `used` to the
      limit (so `remaining` reads 0 immediately) and sideline until the window
      resets. This is the legacy behavior, preserved for callers that mean it."""
    lim = _limit_for(pid)
    now = time.time()
    start, reset = _window_bounds(lim["window"], now)
    until = now + seconds if seconds else reset
    with _LOCK:
        st = _STATE.get(pid)
        if not st or st.get("window_start") != start:
            st = {"count": 0, "window_start": start, "throttled_until": 0}
        if not seconds:
            # Full-window throttle: peg usage so `remaining` reads 0 immediately.
            # No-op for UNKNOWN-limit providers — there is no budget to peg, so
            # they stay sidelined by `throttled_until` alone (until the window
            # resets), which is the honest signal we actually have.
            if isinstance(lim.get("limit"), int):
                st["count"] = max(st.get("count", 0), lim["limit"])
        st["throttled_until"] = max(st.get("throttled_until", 0), until)
        _STATE[pid] = st


def status(pid: str) -> dict:
    """{used, limit, limit_known, remaining, window, resets_in, resets_at,
    throttled, exhausted}.

    `limit`/`remaining` are None when the provider has no researched figure
    (DEFAULT_LIMIT) — `limit_known: False` says so explicitly. An unknown
    provider is NEVER reported as quota-exhausted: we don't know its budget, so
    we don't invent one and we don't disable it. Only a real 429 sidelines it.

    A provider with a documented `limit: 0` (no free tier) IS reported
    exhausted — that's a researched zero, not an unknown, and keeping it out of
    free routing is the point."""
    lim = _limit_for(pid)
    limit = lim.get("limit")
    limit_known = isinstance(limit, int)
    now = time.time()
    start, reset = _window_bounds(lim["window"], now)
    with _LOCK:
        st = _STATE.get(pid)
        used = st["count"] if st and st.get("window_start") == start else 0
        throttled_until = (st.get("throttled_until", 0) if st else 0)
    throttled = throttled_until > now
    remaining = max(0, limit - used) if limit_known else None
    quota_exhausted = bool(limit_known and remaining <= 0)
    exhausted = throttled or quota_exhausted
    # Countdown = when the provider becomes usable AGAIN:
    #   - budget genuinely spent -> wait for the window reset (or a later throttle);
    #   - only throttled (short 429 cooldown, budget left) -> wait out the throttle,
    #     NOT the far-off window reset — otherwise a 1-minute burst limit would show
    #     an end-of-day countdown and the provider would look dead all day;
    #   - neither -> next window (informational).
    if quota_exhausted:
        reset_at = max(reset, throttled_until)
    elif throttled:
        reset_at = throttled_until
    else:
        reset_at = reset
    return {
        "used": used, "limit": limit, "limit_known": limit_known,
        "remaining": remaining,
        "window": lim["window"], "resets_in": max(0, int(reset_at - now)),
        "resets_at": int(reset_at), "throttled": throttled, "exhausted": exhausted,
    }


def is_exhausted(pid: str) -> bool:
    return status(pid)["exhausted"]
