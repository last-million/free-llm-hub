"""One spent key must not bench a whole pool of separate accounts.

MEASURED 2026-09-09, the four g4f keys hit one at a time against the real
endpoint:

    key 1  200  limit=500 remaining=432
    key 2  429  "Token limit (1,000,000 per day) exceeded for new tier"
    key 3  429  same
    key 4  429  same

Three accounts spent, one with 432 requests left -- and the hub had g4f marked
exhausted PROVIDER-WIDE, so it would not touch the working key.

observe_headers stored a rate-limit reading under the provider id. That reading
describes the ACCOUNT that just answered, and a provider's keys are usually
separate accounts -- which is the reason anyone collects several. So whichever
key replied last decided the fate of all of them, and because is_exhausted
gates _available_providers, the provider left routing entirely and the rotation
that would have found the good key never ran.

Readings are per key now and summed. The pool is out of budget only when every
key we have heard from is spent AND we have heard from as many keys as the pool
holds -- a key we know nothing about is assumed to have budget, the same
fail-open rule usable_keys already follows.
"""
import pytest

import quota


PID = "pk-test"


def _hdr(limit, remaining, reset=3600):
    return {"x-ratelimit-limit-requests": str(limit),
            "x-ratelimit-remaining-requests": str(remaining),
            "x-ratelimit-reset-requests": "%ds" % reset}


@pytest.fixture(autouse=True)
def clean():
    saved = dict(quota._DYNAMIC)
    quota._DYNAMIC.clear()
    quota.set_key_counter(lambda pid: 4)
    yield
    quota._DYNAMIC.clear()
    quota._DYNAMIC.update(saved)
    quota.set_key_counter(None)


def test_one_working_key_keeps_the_provider_alive():
    """The measured g4f shape, exactly."""
    quota.observe_headers(PID, _hdr(500, 432), key="KEY-1")
    for k in ("KEY-2", "KEY-3", "KEY-4"):
        quota.observe_headers(PID, _hdr(250, 0), key=k)
    st = quota.status(PID)
    assert st["exhausted"] is False
    assert st["remaining"] == 432


def test_every_key_spent_does_exhaust_the_provider():
    """The fix must not make a genuinely spent pool look usable."""
    for k in ("KEY-1", "KEY-2", "KEY-3", "KEY-4"):
        quota.observe_headers(PID, _hdr(250, 0), key=k)
    assert quota.status(PID)["exhausted"] is True


def test_an_unheard_from_key_is_assumed_to_have_budget():
    """Only one key has ever answered, and it is spent. The other three are
    unknown, not empty -- refusing to try is worse than trying."""
    quota.observe_headers(PID, _hdr(250, 0), key="KEY-2")
    assert quota.status(PID)["exhausted"] is False


def test_the_limits_sum_across_accounts():
    quota.observe_headers(PID, _hdr(500, 400), key="KEY-1")
    quota.observe_headers(PID, _hdr(250, 200), key="KEY-2")
    st = quota.status(PID)
    assert st["limit"] == 750
    assert st["remaining"] == 600


def test_a_keyless_provider_still_works():
    """Nothing to attribute the reading to -- the provider-wide slot stays."""
    quota.observe_headers(PID, _hdr(100, 7))
    st = quota.status(PID)
    assert st["limit"] == 100 and st["remaining"] == 7


def test_a_per_key_reading_wins_over_the_shared_one():
    quota.observe_headers(PID, _hdr(100, 0))                   # old-style, spent
    quota.observe_headers(PID, _hdr(500, 432), key="KEY-1")    # this key is fine
    assert quota.status(PID)["exhausted"] is False


def test_the_soonest_key_back_sets_the_countdown():
    quota.observe_headers(PID, _hdr(250, 0, reset=3000), key="KEY-1")
    quota.observe_headers(PID, _hdr(250, 0, reset=600), key="KEY-2")
    quota.observe_headers(PID, _hdr(250, 0, reset=1800), key="KEY-3")
    quota.observe_headers(PID, _hdr(250, 0, reset=2400), key="KEY-4")
    st = quota.status(PID)
    assert st["exhausted"] is True
    assert st["resets_in"] <= 700, st["resets_in"]


def test_the_slots_are_json_serialisable():
    """_DYNAMIC is written to quota-state.json, whose object keys must be
    strings -- a tuple slot would break every save."""
    import json
    quota.observe_headers(PID, _hdr(500, 432), key="KEY-1")
    assert all(isinstance(k, str) for k in quota._DYNAMIC)
    json.dumps(quota._DYNAMIC)


def test_a_stale_reading_is_ignored():
    quota.observe_headers(PID, _hdr(250, 0), key="KEY-1")
    for v in quota._DYNAMIC.values():
        v["seen"] = 0                       # older than the TTL
    assert quota.status(PID)["exhausted"] is False


def test_one_key_pool_is_unaffected():
    """The common case must behave exactly as before."""
    quota.set_key_counter(lambda pid: 1)
    quota.observe_headers(PID, _hdr(100, 0), key="ONLY")
    assert quota.status(PID)["exhausted"] is True
