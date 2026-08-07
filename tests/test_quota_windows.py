"""Quota windows: token buckets, honoured Retry-After, and reuse after reset.

THE BUG THESE PIN DOWN
----------------------
The dashboard showed a g4f provider as "1 minute remaining", the minute elapsed,
and the provider still did not work — for ~22 hours. Two independent causes, both
of which had to be fixed for the countdown to mean anything:

1. `observe_headers` read only the REQUEST bucket. g4f.space answers a 429 with
   `x-ratelimit-remaining-requests: 94` (healthy!) next to
   `x-ratelimit-remaining-tokens: -65038` (the actual reason). So `status()`
   reported the provider as having budget left, `exhausted` stayed False, and the
   router put it straight back in the chain to 429 again on the next request.

2. `mark_throttled` clamped the sideline to the end of our STATIC window. g4f is
   listed as a per-minute window but its real budget is 500k tokens per DAY, so
   an explicit `Retry-After: 81486` (22h38m) was cut to the next :00 boundary.

Together the UI countdown and the real block were off by ~1360x.
"""
import time

import pytest

import quota


@pytest.fixture
def fresh_quota():
    saved = (dict(quota._STATE), dict(quota._MODEL_STATE),
             dict(quota._MODEL_THROTTLE), dict(quota._DYNAMIC),
             quota._PERSIST_PATH, quota._persist_last)
    quota._STATE.clear()
    quota._MODEL_STATE.clear()
    quota._MODEL_THROTTLE.clear()
    quota._DYNAMIC.clear()
    quota._PERSIST_PATH = None
    quota._persist_last = 0.0
    try:
        yield
    finally:
        quota._STATE.clear()
        quota._STATE.update(saved[0])
        quota._MODEL_STATE.clear()
        quota._MODEL_STATE.update(saved[1])
        quota._MODEL_THROTTLE.clear()
        quota._MODEL_THROTTLE.update(saved[2])
        quota._DYNAMIC.clear()
        quota._DYNAMIC.update(saved[3])
        quota._PERSIST_PATH = saved[4]
        quota._persist_last = saved[5]


# The exact headers g4f.space returned on a 429, captured live.
G4F_429_HEADERS = {
    "retry-after": "81486",
    "x-ratelimit-limit-requests": "200",
    "x-ratelimit-remaining-requests": "94",
    "x-ratelimit-limit-tokens": "500000",
    "x-ratelimit-remaining-tokens": "-65038",
}


def test_blown_token_bucket_reads_as_exhausted(fresh_quota):
    """The headline case: requests left, tokens gone -> provider IS exhausted."""
    quota.observe_headers("g4f", G4F_429_HEADERS)
    st = quota.status("g4f")
    assert st["remaining"] == 0
    assert st["exhausted"] is True, (
        "94 requests remaining must NOT read as healthy when the token bucket is "
        "at -65038 — that is exactly what made the hub re-pick a dead provider"
    )


def test_countdown_matches_the_real_block_not_the_static_window(fresh_quota):
    """The UI number and the real block must be the SAME number."""
    quota.observe_headers("g4f", G4F_429_HEADERS)
    resets_in = quota.status("g4f")["resets_in"]
    assert resets_in > 80000, (
        f"countdown is {resets_in}s but upstream said 81486s; showing ~60s here is "
        "the '1 minute remaining that never recovers' bug"
    )


def test_negative_remaining_counts_as_spent(fresh_quota):
    """`<= 0`, never `== 0` — the token bucket goes NEGATIVE when overspent."""
    quota.observe_headers("p", {"x-ratelimit-remaining-tokens": "-1"})
    assert quota.status("p")["exhausted"] is True


def test_healthy_token_bucket_does_not_sideline(fresh_quota):
    quota.observe_headers("p", {"x-ratelimit-remaining-requests": "50",
                                "x-ratelimit-remaining-tokens": "400000"})
    assert quota.status("p")["exhausted"] is False


def test_token_only_provider_is_still_learned(fresh_quota):
    """A provider that sends ONLY a token bucket used to be dropped entirely."""
    quota.observe_headers("p", {"x-ratelimit-remaining-tokens": "1200",
                                "x-ratelimit-limit-tokens": "500000"})
    assert quota._DYNAMIC["p"]["remaining"] == 1200


def test_tpm_blip_parks_for_seconds_not_for_the_retry_after(fresh_quota):
    """Guard on groq/cerebras: a momentary TPM spike sends a spent token bucket
    WITH its own short reset. That must win over a long Retry-After, or a 6-second
    blip would park a healthy provider for hours."""
    now = time.time()
    quota.observe_headers("groq", {
        "retry-after": "7200",             # long, and wrong for a TPM blip
        "x-ratelimit-remaining-tokens": "0",
        "x-ratelimit-reset-tokens": "6",   # the token bucket's OWN reset
    })
    assert quota._DYNAMIC["groq"]["reset_at"] - now < 60


def test_explicit_retry_after_survives_a_wrong_static_window(fresh_quota):
    """g4f is configured as a per-MINUTE window; upstream said 22h38m. Honour it."""
    quota.mark_throttled("g4f", 81486.0)
    st = quota.status("g4f")
    assert st["exhausted"] is True
    assert st["resets_in"] > 80000, (
        f"parked for only {st['resets_in']}s — the static minute window must not "
        "shrink a Retry-After the provider gave us explicitly"
    )


def test_retry_after_is_capped(fresh_quota):
    """An absurd header must not park a provider for a week."""
    quota.mark_throttled("g4f", 999_999_999.0)
    assert quota.status("g4f")["resets_in"] <= quota._RETRY_AFTER_CAP


def test_short_burst_429_still_recovers_fast(fresh_quota):
    """No regression: the hub's default 60s cooldown stays a 60s cooldown."""
    quota.mark_throttled("g4f", 60.0)
    assert quota.status("g4f")["resets_in"] <= 120


def test_provider_is_reused_once_the_window_passes(fresh_quota):
    """The other half of the ask: come BACK when the window really has reset."""
    quota.mark_throttled("g4f", 81486.0)
    assert quota.status("g4f")["exhausted"] is True
    quota._STATE["g4f"]["throttled_until"] = time.time() - 1   # window elapsed
    quota._DYNAMIC.clear()
    assert quota.status("g4f")["exhausted"] is False


@pytest.mark.parametrize("window", ["minute", "hour", "day", "month"])
def test_every_window_granularity_resolves(fresh_quota, window):
    """Per-minute / hour / day / month must all produce a real forward reset."""
    now = time.time()
    start, reset = quota._window_bounds(window, now, "p")
    assert start <= now < reset, f"{window} window does not bracket now"
    assert reset > now
