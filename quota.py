"""
Calvoun Free LLM Hub — per-provider free-quota tracking.

Counts upstream requests per provider inside that provider's free-tier window
(minute / day / month), reports how many remain and when the window resets, and
records a hard throttle when a provider returns HTTP 429. Consumed by app.py to
(a) skip exhausted providers during orchestration and (b) drive the red
"no free quota left" dashboard banner + per-provider reset countdowns.

Not billing-accurate, but no longer guesswork: every figure below was researched
against the provider's own docs / live catalog (July 2026) and carries the
research confidence inline. A limit of 0 means "NO FREE TIER" — a documented
zero, not an unknown. A provider we have no figure for is tracked as UNKNOWN
(see DEFAULT_LIMIT) and is never assumed to have a free budget.

State persists to a small JSON file when init_persistence() is called (app.py
wires it to a file next to the config on startup); without that call everything
stays in-memory and nothing touches the disk.

Pure stdlib: atexit, calendar, json, os, tempfile, threading, time.
"""
from __future__ import annotations

import atexit
import calendar
import json
import os
import tempfile
import threading
import hashlib
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
    "llm7":          {"limit": 20,    "window": "minute"},  # medium: docs state ~20 req/min AND 100 req/HOUR — the hourly cap has no matching window here, so we track the documented per-minute rate (same convention as nararouter) and let real 429s sideline it once the hourly budget trips.
    "navy":          {"limit": 20,    "window": "minute"},  # medium: free shared pool is ~150K TOKENS/day at ~20 RPM — a request counter can't express a token budget, so we track the documented request rate; real 429s retire it when the daily pool is spent.
    "routeway":      {"limit": 200,   "window": "day"},     # medium: free tier = ~200 req/day at ~5 RPM on ':free' ids; the 5/min burst limit is handled by the 429 -> 60s cooldown path, not by this daily budget (same shape as cerebras).
    "g4f":           {"limit": 5,     "window": "minute"},  # low: community-observed ~5 req/min on the g4f.space relay — no official doc exists for a volunteer proxy, so treat as a soft hint; real 429s still sideline it (same shape as llm7). ONE row since 2026-08-06: the old g4f-groq/g4f-gemini/g4f-nvidia split was three paths on ONE service sharing ONE account, so three separate 5/min budgets overstated the real allowance ~3x. They merged into the unified https://g4f.space/v1 endpoint.
    # uncloseai / api-airforce / kilocode / puter: genuinely free but with NO
    # published request figure — deliberately ABSENT here so they track as
    # UNKNOWN via DEFAULT_LIMIT instead of inheriting a fabricated budget (same
    # convention as pollinations/aihorde). kilocode's anonymous tier documents
    # no rate limit at all. puter is a "user-pays" gateway under a fair-use
    # policy with no published numbers (verified 2026-07-30). Real 429s still
    # throttle them.

    # ── NO FREE TIER — documented zeros. Every call costs money (or 402s). ───
    # Kept as explicit rows (not deleted) so they can never inherit DEFAULT_LIMIT.
    "kimi":          {"limit": 0,     "window": "day"},     # high: docs verbatim "There is no free tier... recharge at least $1 to start using."
    "minimax":       {"limit": 0,     "window": "day"},     # high: no free tier in any official doc; pay-per-token or subscription only. FAILS UNSAFELY (200 OK + a bill).
    "chutes":        {"limit": 0,     "window": "day"},     # high: free tier fully retired 2026-03-15 (the old 200/day Early Access always required a $5 deposit). FAILS UNSAFELY.
    "huggingface":   {"limit": 0,     "window": "month"},   # high: allowance is $0.10/MONTH of credits at full rates (~17 req on GLM-5.2), not a request count. is_free:true matches 0 of 102 router models. FAILS UNSAFELY.
    "scaleway":      {"limit": 0,     "window": "day"},     # high: card mandatory before the first call; one-time 1M-token allowance then silent billing. The old "300/day" was the PAID 300/MINUTE figure with the window swapped. FAILS UNSAFELY.
    "deepseek":      {"limit": 0,     "window": "day"},     # high: 2 models, both paid. The old 500/day was mis-derived from the v4-pro CONCURRENCY limit; no RPD/RPM is published anywhere.
    "nebius":        {"limit": 0,     "window": "day"},     # high: $1 trial credit, 30 days, bank card required at onboarding; no $0 models
    "xiaomi":        {"limit": 0,     "window": "day"},     # high: chat/LLM is pay-as-you-go or paid Token Plans; the only $0 models are TTS (non-chat, "limited time")
    # nvidia: was {"limit": 0} — but 0 means "researched: NO free tier", and that is
    # not what nvidia is. Probed live 2026-07-25: it answers 200 with 88 usable
    # models (deepseek-v4-pro, minimax-m3, glm-5.2, kimi-k2.6, nemotron-3-ultra;
    # 250k ctx) and holds ranks 3-13 of the whole fleet. The documented zero was
    # silently benching the best models we actually have — is_exhausted() returns
    # True unconditionally for limit 0, so routing never picked it even once.
    # The reasoning behind the 0 is still correct — it IS a finite lifetime credit
    # balance (1,000 credits, 90-day expiry) and a per-window counter genuinely
    # cannot express that — so the honest value is UNKNOWN, not zero: don't claim a
    # budget we can't measure, and let real 402s retire it. When the credits do run
    # out, "Cloud credits expired" trips _mark_provider_authfail + the
    # consecutive-hard-failure breaker, which parks nvidia for 30min and re-probes,
    # so it retires itself automatically instead of being permanently pre-banned.
    "nvidia":        {"limit": None,  "window": "day", "unknown": True},
    "morph":         {"limit": 0,     "window": "month"},   # medium: the official "200 req free every month" headline meters TOKENS (250K/mo = $2.50); a coding CLI's 20-50K-token turns make the real allowance ~5-12 req/month, so 200 reported quota long after Morph starts rejecting.
    "qwen":          {"limit": 0,     "window": "day"},     # medium: consumable 1M-tokens-PER-MODEL / 90-day trial, then AllocationQuota.FreeTierOnly on every call. (Documented rate is 600 RPM with no per-day cap.)

    # ── legacy rows, NOT researched, and both are DEAD KEYS ─────────────────
    # Neither id exists in providers.PROVIDERS, so neither row has ever applied:
    # there is no "cohere" provider, and OVHcloud's provider id is "ovhcloud".
    # Left as-is rather than silently activating an unverified figure on
    # ovhcloud — it now tracks as UNKNOWN via DEFAULT_LIMIT instead.
    "cohere":        {"limit": 1000,  "window": "month"},
    "ovh":           {"limit": 300,   "window": "day"},
}

# PER-MODEL sub-caps: providers that meter EACH model on its own budget INSIDE the
# provider's window, so one model can exhaust while its siblings keep serving. Only
# documented figures — a provider absent here has no per-model cap (its models share
# the provider-level FREE_LIMITS budget). The window is inherited from FREE_LIMITS.
PER_MODEL_LIMITS = {
    "modelscope":    500,   # 2,000 calls/day account-wide, but max 500/day PER MODEL
    # NB: github-models is deliberately NOT here — its per-id cap is per-TIER
    # (low-tier ids share 150/day, high-tier are 50/day each, deepseek-r1 8/day), so
    # a single flat number would wrongly throttle the low tier. Those are handled by
    # the real-429 per-model throttle below, which needs no fabricated figure.
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
# pid -> {"count": int, "window_start": float, "throttled_until": float,
#         "strikes": int, "last_strike": float}
_STATE: dict = {}
# pid -> {"window_start": float, "models": {model_id: count}}  (per-model usage)
_MODEL_STATE: dict = {}
# Per-KEY counters, same shape and same window as _MODEL_STATE. Keys are
# identified by a truncated SHA-256 -- never the credential itself, which
# must not end up in a state file that is not encrypted.
_KEY_STATE = {}
# (pid, model) -> {"throttled_until": float, "strikes": int, "last_strike": float}
# Per-MODEL 429 sideline: when ONE model rate-limits (not the whole provider), only
# that id is parked, so the provider's other models keep serving. Independent of the
# provider-level _STATE throttle and NOT cleared by provider note_success().
_MODEL_THROTTLE: dict = {}

# pid -> {"remaining": int, "limit": int|None, "reset_at": float|None, "seen": float}
# DYNAMIC quota learned from the provider's OWN rate-limit response headers. This is
# what makes the hub adapt when a real quota differs from the static FREE_LIMITS
# guess — a top-up that RAISES the limit, or a tightening that LOWERS it — with zero
# probe waste: status() trusts a fresh header reading over the static table. Absent
# for providers that send no such headers (they keep using the static budget).
_DYNAMIC: dict = {}
_DYNAMIC_TTL = 3600.0   # a reading older than this is ignored (window likely rolled)

# Rate-limit header conventions, widest-support first. "-requests" variants are the
# request buckets (not token buckets) most free providers expose; the bare names
# cover OpenRouter/OpenAI-style; the un-prefixed names cover the IETF draft.
_RL_REMAINING = ("x-ratelimit-remaining-requests", "x-ratelimit-remaining", "ratelimit-remaining")
_RL_LIMIT     = ("x-ratelimit-limit-requests", "x-ratelimit-limit", "ratelimit-limit")
_RL_RESET     = ("x-ratelimit-reset-requests", "x-ratelimit-reset", "ratelimit-reset", "retry-after")

# TOKEN buckets. A provider can have plenty of REQUESTS left and still refuse every
# call because its token budget is spent — g4f.space does exactly this:
#     x-ratelimit-remaining-requests: 94      <- looks perfectly healthy
#     x-ratelimit-remaining-tokens: -65038    <- the real reason it 429s
#     "Token limit (500,000 per day) exceeded. Used: 565,038 tokens."
# Reading only the request bucket made status() report the provider as fine, so the
# hub un-sidelined it after the ~60s throttle and re-picked it every single request
# for the next 22 hours, burning a chain hop on a guaranteed 429 each time. Note the
# remaining count goes NEGATIVE, so this must test `<= 0`, never `== 0`.
_RL_REMAINING_TOK = ("x-ratelimit-remaining-tokens",)
_RL_LIMIT_TOK     = ("x-ratelimit-limit-tokens",)
_RL_RESET_TOK     = ("x-ratelimit-reset-tokens",)

# Consecutive-429 backoff. Fixes "provider quota is spent but the hub keeps calling
# it every 60s": each recurring 429 within _STRIKE_TTL of the last DOUBLES the short
# cooldown, so a provider whose window budget is actually spent is retried
# exponentially less often (60s -> 2m -> 4m -> ... capped), never past the window
# reset — while a genuine 1-minute burst still recovers on its next success
# (note_success clears the streak).
_STRIKE_TTL = 1800.0     # seconds: 429s farther apart than this restart the streak
_MAX_BACKOFF = 3600.0    # seconds: cap one sideline at 1h (the window reset caps it too)
_RETRY_AFTER_CAP = 86400.0  # seconds: ceiling on an explicit Retry-After we honour


# HOW MANY KEYS a provider has, supplied by app.py at startup.
#
# quota.py deliberately does not import config -- it is the accounting layer and
# knows nothing about where credentials live -- so the count arrives through a
# hook instead.
_key_counter = None


def set_key_counter(fn) -> None:
    """Register `fn(pid) -> int`, the size of pid's key pool."""
    global _key_counter
    _key_counter = fn


def key_count(pid: str) -> int:
    """Keys in pid's pool; 1 when unknown, so nothing changes without a hook."""
    if _key_counter is None:
        return 1
    try:
        return max(1, int(_key_counter(pid) or 1))
    except Exception:                                            # noqa: BLE001
        return 1


def _limit_for(pid: str) -> dict:
    """The provider's free-tier budget, SCALED BY THE SIZE OF ITS KEY POOL.

    REPORTED 2026-09-05: "for example for openrouter he say no more quota and
    will be available in 1 hour but i have multi api keys". He does -- four of
    them -- and the hub was counting all four against ONE key's allowance:

        openrouter   4 keys, limit 50/day   -> stopped at 50 of a real 200
        sambanova    4 keys, limit 20/day   -> stopped at 20 of a real 80
        groq         3 keys, limit 1000/day -> stopped at 1000 of a real 3000

    Three quarters of the budget unusable, and the provider then dropped out of
    routing entirely because is_exhausted gates on this number.

    Key ROTATION already worked -- _upstream_chat advances to the next key on
    401/403/429 -- so the pool was being used; only the accounting stopped early.

    THE ASSUMPTION, stated because it can be wrong: this treats each key as
    carrying its own upstream allowance, which holds when the keys are separate
    accounts (the reason anyone collects several) and not when they share one.
    If they do share, the provider answers 429, and that path is already handled
    -- observe_headers revises the limit downward from the provider's own
    figures, and the 429 cooldown sidelines it. So the cost of being wrong is a
    few wasted 429s that self-correct, against a certainty of leaving three
    quarters of the budget unspent. A documented `limit: 0` stays 0 -- no free
    tier times any number of keys is still no free tier."""
    lim = FREE_LIMITS.get(pid, DEFAULT_LIMIT)
    n = key_count(pid)
    limit = lim.get("limit")
    if n <= 1 or not isinstance(limit, int) or limit <= 0:
        return lim
    scaled = dict(lim)
    scaled["limit"] = limit * n
    scaled["keys"] = n
    scaled["per_key_limit"] = limit
    return scaled


# Providers whose DAILY quota does not roll over at UTC midnight. Google states it
# explicitly: "Requests per day (RPD) quotas reset at midnight Pacific time"
# (https://ai.google.dev/gemini-api/docs/rate-limits) — 7-8h off UTC depending on
# DST, so a UTC assumption either keeps hammering a still-exhausted key for hours or
# writes the provider off long after it recovered. Named zones (not fixed offsets)
# so DST is handled for us. Everything absent from this map stays UTC.
DAY_RESET_TZ = {"google": "America/Los_Angeles"}


def _day_bounds_tz(zone: str, now: float):
    """(start, reset) of the LOCAL day containing `now` in `zone`, as epochs.
    Falls back to UTC if the tz database is unavailable (bare containers)."""
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime, timedelta
        tz = ZoneInfo(zone)
        local = datetime.fromtimestamp(now, tz)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        # Re-resolve the NEXT midnight from the calendar date (not +86400): across a
        # DST switch the local day is 23 or 25 hours long.
        nxt = (midnight + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                                     microsecond=0)
        return midnight.timestamp(), nxt.timestamp()
    except Exception:
        return None


def _window_bounds(window: str, now: float, pid: str = None):
    """(start_epoch, reset_epoch) for the window CONTAINING `now`. UTC unless the
    provider is listed in DAY_RESET_TZ (day window only)."""
    if window == "minute":
        start = now - (now % 60)
        return start, start + 60
    if window == "day" and pid in DAY_RESET_TZ:
        bounds = _day_bounds_tz(DAY_RESET_TZ[pid], now)
        if bounds:
            return bounds
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
    start, _reset = _window_bounds(lim["window"], now, pid)
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
    _persist_maybe()


def key_fingerprint(key) -> str:
    """A stable, non-reversible id for one API key.

    The key itself is never stored here. A fingerprint is enough to say "this
    one is the exhausted one" and cannot leak a credential into a state file
    that, unlike config.json, is not encrypted."""
    if not isinstance(key, str) or not key:
        return ""
    return hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:16]


def record_key(pid: str, key, model: str = None, n: int = 1) -> None:
    """Count requests against ONE key of pid's pool.

    The pool exists so a provider survives one key running out, and rotation
    made that work -- while making "which of my keys is dead?" unanswerable,
    because every counter was per provider. With dozens of keys across dozens
    of providers that is the difference between "something is throttled" and
    "this key is".

    Shares the provider's window, so the numbers add up to the provider total
    rather than drifting against it."""
    fp = key_fingerprint(key)
    if not fp:
        return                     # a no-key/static-key provider has none to track
    lim = _limit_for(pid)
    now = time.time()
    start, _reset = _window_bounds(lim["window"], now, pid)
    with _LOCK:
        ks = _KEY_STATE.get(pid)
        if not ks or ks.get("window_start") != start:
            ks = {"window_start": start, "keys": {}}
        row = ks["keys"].get(fp) or {"count": 0, "ok": 0, "fail": 0, "last": 0}
        row["count"] = row.get("count", 0) + n
        row["last"] = now
        ks["keys"][fp] = row
        _KEY_STATE[pid] = ks
    _persist_maybe()


def note_key_outcome(pid: str, key, ok: bool) -> None:
    """Whether a key's request actually worked.

    A count alone cannot distinguish a key doing all the work from one being
    rotated onto and rejected every time -- which is exactly the state a pool
    is supposed to make survivable and therefore invisible."""
    fp = key_fingerprint(key)
    if not fp:
        return
    with _LOCK:
        ks = _KEY_STATE.get(pid)
        if not ks:
            return
        row = ks["keys"].get(fp)
        if not row:
            return
        row["ok" if ok else "fail"] = row.get("ok" if ok else "fail", 0) + 1


# KEYS THAT ARE OUT OF QUOTA, so rotation can skip them instead of rediscovering
# the same 429 on every request.
#
# ASKED 2026-09-05: "should work for all providers the multi api keys rotation
# when one dont have quota left". Rotation itself already worked -- _upstream_chat
# advances to the next key on 401/403/429 -- but it had no MEMORY. A key that ran
# out at 09:00 was still tried first on every request after it, burning one
# guaranteed-failed round trip each time, and on a pool of four that is a quarter
# of all attempts spent on a key already known to be spent.
_KEY_COOLDOWN = {}          # (pid, fingerprint) -> epoch when it may be used again
_KEY_COOLDOWN_DEFAULT = 900


def mark_key_exhausted(pid: str, key, seconds: float = None) -> None:
    """This key has no quota left. Skip it until `seconds` have passed.

    Defaults to the provider's own window reset when it has one, so a daily key
    comes back when the day does rather than on an arbitrary timer."""
    fp = key_fingerprint(key)
    if not fp:
        return
    if seconds is None:
        try:
            _start, reset = _window_bounds(_limit_for(pid)["window"], time.time(), pid)
            seconds = max(60, reset - time.time())
        except Exception:                                        # noqa: BLE001
            seconds = _KEY_COOLDOWN_DEFAULT
    with _LOCK:
        _KEY_COOLDOWN[(pid, fp)] = time.time() + float(seconds)
    _persist_maybe()


def key_available(pid: str, key) -> bool:
    """False only while a key is in its post-429 cooldown."""
    fp = key_fingerprint(key)
    if not fp:
        return True
    with _LOCK:
        until = _KEY_COOLDOWN.get((pid, fp), 0)
    if until and until > time.time():
        return False
    if until:
        with _LOCK:
            _KEY_COOLDOWN.pop((pid, fp), None)     # expired: let it back in
    return True


def usable_keys(pid: str, key_list):
    """`key_list` reordered so keys with quota come first.

    FAIL-OPEN: when every key is cooling down the original list is returned
    unchanged. Refusing to try is worse than trying -- a cooldown is an estimate,
    and the provider is the only real authority on whether a key still works."""
    if not key_list or len(key_list) == 1:
        return list(key_list or [])
    fresh = [k for k in key_list if key_available(pid, k)]
    return fresh if fresh else list(key_list)


def key_cooldowns(pid: str) -> dict:
    """{fingerprint: seconds remaining} — for the dashboard."""
    now = time.time()
    with _LOCK:
        items = list(_KEY_COOLDOWN.items())
    return {fp: round(until - now) for (p, fp), until in items
            if p == pid and until > now}


def keys(pid: str) -> dict:
    """{fingerprint: {count, ok, fail, last}} for pid's current window."""
    with _LOCK:
        ks = _KEY_STATE.get(pid)
        if not ks:
            return {}
        lim = _limit_for(pid)
        start, _reset = _window_bounds(lim["window"], time.time(), pid)
        if ks.get("window_start") != start:
            return {}                        # the window rolled: last one is stale
        return {k: dict(v) for k, v in (ks.get("keys") or {}).items()}


def models(pid: str) -> dict:
    """{model_id: used_count} for pid's CURRENT window (only models actually hit).
    Empty once the window rolls over."""
    lim = _limit_for(pid)
    now = time.time()
    start, _reset = _window_bounds(lim["window"], now, pid)
    with _LOCK:
        ms = _MODEL_STATE.get(pid)
        if not ms or ms.get("window_start") != start:
            return {}
        return dict(ms.get("models") or {})


def _hdr(headers, names):
    for n in names:
        try:
            v = headers.get(n)
        except Exception:
            v = None
        if v not in (None, ""):
            return v
    return None


def _parse_int(v):
    if v is None:
        return None
    try:
        return int(float(str(v).strip()))
    except Exception:
        import re as _re
        m = _re.search(r"-?\d+", str(v))
        return int(m.group()) if m else None


def _parse_reset(v, now):
    """A rate-limit reset header -> absolute epoch. Handles seconds-from-now, epoch
    seconds, epoch millis, and duration strings ('60s', '1m30s', '500ms'). None if
    unparseable (caller then falls back to the static window reset)."""
    if v is None:
        return None
    import re as _re
    s = str(v).strip().lower()
    if _re.search(r"[a-z]", s):                       # duration like '1m30s'
        total, matched = 0.0, False
        for num, unit in _re.findall(r"(\d+(?:\.\d+)?)\s*(ms|s|m|h)", s):
            matched = True
            total += float(num) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
        return now + total if matched else None
    n = _parse_int(s)
    if n is None:
        return None
    if n > 1e12:                                       # epoch millis
        return n / 1000.0
    if n > 1e9:                                        # epoch seconds
        return float(n)
    return now + n                                     # seconds-from-now


def observe_headers(pid: str, headers) -> None:
    """Learn a provider's REAL request quota from its rate-limit response headers,
    so the hub adapts to any quota change (raised by a top-up, or lowered) with no
    probe waste. Best-effort: no usable 'remaining' header -> no-op (static budget
    stays in force). Never raises."""
    if not headers:
        return
    now = time.time()
    rem = _parse_int(_hdr(headers, _RL_REMAINING))
    tok_rem = _parse_int(_hdr(headers, _RL_REMAINING_TOK))
    if rem is None and tok_rem is None:
        return
    lim = _parse_int(_hdr(headers, _RL_LIMIT))
    reset_at = _parse_reset(_hdr(headers, _RL_RESET), now)

    # A spent TOKEN bucket means the provider is unusable even with requests left,
    # so it must win over an optimistic request count. Take the token bucket's OWN
    # reset when it gave one: a per-minute TPM blip then parks the provider for its
    # own few seconds instead of for whatever far-off Retry-After the response
    # carried — which is what keeps this from mis-parking groq/cerebras for hours
    # on a momentary token spike.
    if tok_rem is not None and tok_rem <= 0:
        rem = 0
        tok_reset = _parse_reset(_hdr(headers, _RL_RESET_TOK), now)
        if tok_reset:
            reset_at = tok_reset
    elif rem is None:
        # Only a token bucket was sent and it still has room: record the tokens as
        # the remaining budget rather than dropping the reading entirely.
        rem = tok_rem
        lim = _parse_int(_hdr(headers, _RL_LIMIT_TOK)) if lim is None else lim
        reset_at = reset_at or _parse_reset(_hdr(headers, _RL_RESET_TOK), now)

    with _LOCK:
        _DYNAMIC[pid] = {"remaining": max(0, rem), "limit": lim,
                         "reset_at": reset_at, "seen": now}
    _persist_maybe()


def _dynamic(pid: str, now: float):
    """Fresh dynamic reading for pid, or None if absent/stale/window-rolled."""
    d = _DYNAMIC.get(pid)
    if not d:
        return None
    if now - d.get("seen", 0) > _DYNAMIC_TTL:
        return None
    if d.get("reset_at") and d["reset_at"] <= now:     # its window already reset
        return None
    return d


def mark_model_throttled(pid: str, model: str, seconds: float = 60) -> None:
    """A SINGLE model returned 429 — sideline just this id (its siblings on the same
    provider keep serving). Consecutive-429 backoff mirrors the provider path: a 429
    recurring within _STRIKE_TTL doubles the cooldown (capped at _MAX_BACKOFF, never
    past the window reset), so an individually-spent model is retried exponentially
    less often instead of every 60s. No-op without a model id."""
    if not (isinstance(model, str) and model):
        return
    lim = _limit_for(pid)
    now = time.time()
    _start, reset = _window_bounds(lim["window"], now, pid)
    key = (pid, model)
    with _LOCK:
        mt = _MODEL_THROTTLE.get(key) or {"throttled_until": 0, "strikes": 0, "last_strike": 0.0}
        if now - mt.get("last_strike", 0.0) <= _STRIKE_TTL:
            mt["strikes"] = min(mt.get("strikes", 0) + 1, 16)
        else:
            mt["strikes"] = 1
        mt["last_strike"] = now
        backoff = min((seconds or 60) * (2 ** (mt["strikes"] - 1)), _MAX_BACKOFF)
        mt["throttled_until"] = max(mt.get("throttled_until", 0), min(now + backoff, reset))
        _MODEL_THROTTLE[key] = mt
    _persist_maybe()


def note_model_success(pid: str, model: str) -> None:
    """This exact model answered 2xx: clear ITS 429-backoff streak/sideline (a real
    burst recovers here). Leaves other models and the provider state untouched."""
    if not (isinstance(model, str) and model):
        return
    with _LOCK:
        mt = _MODEL_THROTTLE.get((pid, model))
        if mt:
            mt["strikes"] = 0
            mt["last_strike"] = 0.0
            mt["throttled_until"] = 0
    _persist_maybe()


def is_model_throttled(pid: str, model: str) -> bool:
    """True while this specific id is inside its per-model 429 cooldown."""
    with _LOCK:
        mt = _MODEL_THROTTLE.get((pid, model))
    return bool(mt and mt.get("throttled_until", 0) > time.time())


def model_status(pid: str, model: str) -> dict:
    """Per-model view: {used, limit, remaining, limit_known, throttled, exhausted}.
    `limit` is the PER_MODEL_LIMITS sub-cap (None if the provider has none — then the
    model shares the provider budget and only a real 429 sidelines it). `exhausted`
    is per-model only (sub-cap hit OR per-model 429 cooldown); it does NOT fold in
    provider-level exhaustion — use is_model_exhausted() for the full picture."""
    per = PER_MODEL_LIMITS.get(pid)
    used = models(pid).get(model, 0)
    limit_known = isinstance(per, int)
    remaining = max(0, per - used) if limit_known else None
    throttled = is_model_throttled(pid, model)
    exhausted = throttled or bool(limit_known and remaining <= 0)
    return {"used": used, "limit": per, "limit_known": limit_known,
            "remaining": remaining, "throttled": throttled, "exhausted": exhausted}


def is_model_exhausted(pid: str, model: str) -> bool:
    """Full per-(provider, model) gate for routing: True if the PROVIDER is spent OR
    this individual model hit its sub-cap / is in its own 429 cooldown."""
    return is_exhausted(pid) or model_status(pid, model)["exhausted"]


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
    start, reset = _window_bounds(lim["window"], now, pid)
    with _LOCK:
        st = _STATE.get(pid)
        if not st or st.get("window_start") != start:
            st = {"count": 0, "window_start": start, "throttled_until": 0,
                  "strikes": 0, "last_strike": 0.0}
        if not seconds:
            # Full-window throttle: peg usage so `remaining` reads 0 immediately.
            # No-op for UNKNOWN-limit providers — there is no budget to peg, so
            # they stay sidelined by `throttled_until` alone (until the window
            # resets), which is the honest signal we actually have.
            if isinstance(lim.get("limit"), int):
                st["count"] = max(st.get("count", 0), lim["limit"])
            until = reset
        else:
            # CONSECUTIVE-429 BACKOFF: a 429 that recurs within _STRIKE_TTL of the
            # last means the short cooldown didn't clear it — the window budget is
            # spent, not a 1-minute burst — so double the cooldown per strike
            # (capped at _MAX_BACKOFF, never past the window reset). Stops the hub
            # re-calling an exhausted provider every 60s forever.
            if now - st.get("last_strike", 0.0) <= _STRIKE_TTL:
                st["strikes"] = min(st.get("strikes", 0) + 1, 16)
            else:
                st["strikes"] = 1
            st["last_strike"] = now
            backoff = min(seconds * (2 ** (st["strikes"] - 1)), _MAX_BACKOFF)
            until = min(now + backoff, reset)
            # ...but never BELOW what the provider explicitly asked for. `reset`
            # comes from our static FREE_LIMITS window, which is a guess and can be
            # wildly wrong: g4f is listed as a per-MINUTE window while its real
            # budget is 500k tokens per DAY, so a Retry-After of 81486s (22h38m)
            # was being clamped to the next :00 boundary — the hub came back 60s
            # later, 429'd, and repeated that ~1360 times. When upstream names a
            # number it knows better than our table, so honour it as a FLOOR.
            # Capped at _RETRY_AFTER_CAP so one absurd header cannot park a
            # provider for a week.
            until = max(until, now + min(seconds, _RETRY_AFTER_CAP))
        st["window_start"] = start
        st["throttled_until"] = max(st.get("throttled_until", 0), until)
        _STATE[pid] = st
    _persist_maybe()


def note_success(pid: str) -> None:
    """A successful (2xx) upstream response: the provider is alive, so reset its
    consecutive-429 backoff streak (a genuine short burst recovers here). Usage
    counts are untouched."""
    with _LOCK:
        st = _STATE.get(pid)
        if st and (st.get("strikes") or st.get("last_strike") or st.get("throttled_until")):
            st["strikes"] = 0
            st["last_strike"] = 0.0
            # A live provider isn't throttled — drop any residual sideline so it's
            # eligible again (a later 429 re-throttles from the 60s base). Does NOT
            # un-exhaust a KNOWN-limit provider whose count already hit its budget.
            st["throttled_until"] = 0
    _persist_maybe()


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
    keys_n = lim.get("keys", 1)
    per_key = lim.get("per_key_limit")
    now = time.time()
    start, reset = _window_bounds(lim["window"], now, pid)
    with _LOCK:
        st = _STATE.get(pid)
        used = st["count"] if st and st.get("window_start") == start else 0
        throttled_until = (st.get("throttled_until", 0) if st else 0)
    throttled = throttled_until > now
    remaining = max(0, limit - used) if limit_known else None
    quota_exhausted = bool(limit_known and remaining <= 0)
    # ADAPT: a fresh reading from the provider's own rate-limit headers overrides the
    # static guess (both ways — a raised limit un-exhausts, a lowered one exhausts),
    # so quota changes are tracked with zero probe waste. `reset` moves to the header's
    # own reset when it gave one, so the countdown is the provider's real one.
    dyn = _dynamic(pid, now)
    if dyn is not None:
        remaining = dyn["remaining"]
        if isinstance(dyn.get("limit"), int):
            limit, limit_known = dyn["limit"], True
        else:
            limit_known = True                     # we at least know real remaining
        quota_exhausted = remaining <= 0
        if dyn.get("reset_at"):
            reset = dyn["reset_at"]
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


# ---------------------------------------------------------------------------
# Persistence — survive hub restarts.
#
# Opt-in: nothing is read or written until init_persistence(path) is called
# (app.py does that on startup with a file next to the config). One JSON file
# holds the usage counters, both throttle maps and the learned dynamic limits;
# an opaque "app" blob lets app.py piggyback its own dead-model/provider maps
# through the same file via the extra_load/extra_dump callables. Saves are
# debounced (_PERSIST_DEBOUNCE) from the mutators, crash-safe (tmp file +
# os.replace), and fire once more at clean shutdown (atexit). A missing or
# corrupt file fails OPEN to empty state — quota is a hint, never a gate, so a
# bad file must never block startup. Entries past their TTL are dropped on
# load (an expired throttle or a stale dynamic reading is worse than none).
# ---------------------------------------------------------------------------
_PERSIST_PATH = None
_PERSIST_DEBOUNCE = 30.0     # seconds: save at most this often from mutators
_persist_last = 0.0
_extra_load = None           # callable(dict) — applies the "app" blob (app.py bridge)
_extra_dump = None           # callable() -> dict — produces the "app" blob


def init_persistence(path: str, extra_load=None, extra_dump=None) -> None:
    """Load persisted state from `path`, then keep it saved from now on.
    `extra_load`/`extra_dump` let the caller round-trip its own state blob
    through the same file (app.py's dead-model/provider maps). Best-effort:
    a missing/corrupt file just starts empty."""
    global _PERSIST_PATH, _extra_load, _extra_dump
    _PERSIST_PATH = path
    _extra_load, _extra_dump = extra_load, extra_dump
    _load_state(path)
    atexit.register(save_state)


def _throttle_key(pid: str, model: str) -> str:
    # Flat string key for the JSON file; a pid never contains "|", a model id may.
    return pid + "|" + model


def save_state() -> None:
    """Write the current state to _PERSIST_PATH (tmp file + os.replace).
    No-op when persistence was never initialized. Never raises."""
    path = _PERSIST_PATH
    if not path:
        return
    global _persist_last
    try:
        with _LOCK:
            blob = {
                "state": _STATE,
                "model_state": _MODEL_STATE,
                "model_throttle": {_throttle_key(pid, m): mt
                                   for (pid, m), mt in _MODEL_THROTTLE.items()},
                "dynamic": _DYNAMIC,
            }
        if _extra_dump is not None:
            try:
                blob["app"] = _extra_dump()
            except Exception:
                pass  # a broken bridge must not lose the quota state
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or None,
                                   prefix=".quota-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(blob, f)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        _persist_last = time.time()
    except Exception:
        pass  # persistence is a convenience, never a failure mode


def _persist_maybe() -> None:
    """Debounced save from the mutators — at most one write per _PERSIST_DEBOUNCE."""
    if _PERSIST_PATH and time.time() - _persist_last >= _PERSIST_DEBOUNCE:
        save_state()


def _load_state(path: str) -> None:
    """Apply a previously saved state file. Fails open: any problem -> empty
    state. Entries past their TTL (expired throttles, stale dynamic readings)
    are dropped rather than revived."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            blob = json.load(f)
    except Exception:
        return  # missing or corrupt -> start empty
    if not isinstance(blob, dict):
        return
    now = time.time()
    with _LOCK:
        state = blob.get("state")
        if isinstance(state, dict):
            for pid, st in state.items():
                if isinstance(pid, str) and isinstance(st, dict):
                    _STATE[pid] = st
        model_state = blob.get("model_state")
        if isinstance(model_state, dict):
            for pid, ms in model_state.items():
                if isinstance(pid, str) and isinstance(ms, dict):
                    _MODEL_STATE[pid] = ms
        throttle = blob.get("model_throttle")
        if isinstance(throttle, dict):
            for key, mt in throttle.items():
                if not (isinstance(key, str) and "|" in key and isinstance(mt, dict)):
                    continue
                if mt.get("throttled_until", 0) <= now:
                    continue  # expired sideline — the model gets a fresh chance
                pid, model = key.split("|", 1)
                _MODEL_THROTTLE[(pid, model)] = mt
        dynamic = blob.get("dynamic")
        if isinstance(dynamic, dict):
            for pid, d in dynamic.items():
                if isinstance(pid, str) and isinstance(d, dict):
                    _DYNAMIC[pid] = d
            # Reuse the normal freshness gate: TTL-expired or window-rolled
            # readings are dropped, not trusted after a restart.
            for pid in [pid for pid in _DYNAMIC if _dynamic(pid, now) is None]:
                _DYNAMIC.pop(pid, None)
    app_blob = blob.get("app")
    if _extra_load is not None and isinstance(app_blob, dict):
        try:
            _extra_load(app_blob)
        except Exception:
            pass
