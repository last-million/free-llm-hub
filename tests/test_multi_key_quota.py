"""A pool of N keys is N budgets, and a spent key gets skipped.

REPORTED 2026-09-05: "for example for openrouter he say no more quota and will
be available in 1 hour but i have multi api keys", then "should work for all
providers the multi api keys rotation when one dont have quota left".

Two separate defects behind that.

BUDGET. Every key's request was counted against ONE key's allowance:

    openrouter   4 keys, limit   50/day  -> stopped at   50 of a real 200
    sambanova    4 keys, limit   20/day  -> stopped at   20 of a real  80
    groq         3 keys, limit 1000/day  -> stopped at 1000 of a real 3000

Three quarters of the budget unusable -- and worse than unusable, because
is_exhausted then drops the provider out of routing entirely.

MEMORY. Rotation itself already worked: _upstream_chat advances to the next key
on 401/403/429. But it forgot. A key that ran out at 09:00 was still tried first
on every later request, burning one guaranteed-failed round trip each time --
on a pool of four, a quarter of all attempts spent on a key already known spent.

The assumption in the first fix is worth stating: it treats each key as carrying
its own allowance, which holds for keys from separate accounts (the reason
anyone collects several) and not for several keys on one account. If they do
share, the provider says 429, observe_headers revises the limit down from the
provider's own numbers and the cooldown sidelines it -- so being wrong costs a
few self-correcting 429s, against a certainty of leaving three quarters of the
budget unspent.
"""
import time
from unittest import mock

import pytest

import quota


@pytest.fixture(autouse=True)
def clean():
    with quota._LOCK:
        quota._KEY_COOLDOWN.clear()
    quota.set_key_counter(None)
    yield
    with quota._LOCK:
        quota._KEY_COOLDOWN.clear()
    quota.set_key_counter(None)


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #

def test_one_key_is_unchanged():
    """Single-key providers must behave exactly as before."""
    quota.set_key_counter(lambda pid: 1)
    assert quota._limit_for("openrouter")["limit"] == \
        quota.FREE_LIMITS["openrouter"]["limit"]


def test_four_keys_is_four_budgets():
    quota.set_key_counter(lambda pid: 4)
    base = quota.FREE_LIMITS["openrouter"]["limit"]
    assert quota._limit_for("openrouter")["limit"] == base * 4


def test_it_applies_to_every_provider_not_a_list():
    """Asked for explicitly: "should work for all providers"."""
    quota.set_key_counter(lambda pid: 3)
    for pid in ("openrouter", "groq", "sambanova"):
        base = quota.FREE_LIMITS[pid]["limit"]
        assert quota._limit_for(pid)["limit"] == base * 3, pid


def test_no_free_tier_stays_no_free_tier():
    """A documented limit of 0 times any number of keys is still 0."""
    quota.set_key_counter(lambda pid: 5)
    with mock.patch.dict(quota.FREE_LIMITS,
                         {"zerop": {"limit": 0, "window": "day"}}, clear=False):
        assert quota._limit_for("zerop")["limit"] == 0


def test_an_unknown_limit_is_not_invented():
    """DEFAULT_LIMIT carries no figure; multiplying None would fabricate one."""
    quota.set_key_counter(lambda pid: 4)
    lim = quota._limit_for("some-provider-nobody-researched")
    assert not isinstance(lim.get("limit"), int) or lim.get("limit") == \
        quota.DEFAULT_LIMIT.get("limit")


def test_without_the_hook_nothing_changes():
    """quota.py must be safe to import and use on its own."""
    quota.set_key_counter(None)
    assert quota.key_count("openrouter") == 1
    assert quota._limit_for("openrouter")["limit"] == \
        quota.FREE_LIMITS["openrouter"]["limit"]


def test_a_broken_hook_does_not_break_accounting():
    quota.set_key_counter(lambda pid: 1 / 0)
    assert quota.key_count("openrouter") == 1


def test_the_scaled_limit_reaches_the_status_the_router_reads():
    quota.set_key_counter(lambda pid: 4)
    base = quota.FREE_LIMITS["openrouter"]["limit"]
    assert quota.status("openrouter")["limit"] == base * 4


# --------------------------------------------------------------------------- #
# Skipping a spent key
# --------------------------------------------------------------------------- #

def test_a_fresh_key_is_available():
    assert quota.key_available("p", "key-a") is True


def test_an_exhausted_key_is_skipped():
    quota.mark_key_exhausted("p", "key-a", 60)
    assert quota.key_available("p", "key-a") is False


def test_only_that_key_is_skipped():
    """The provider is not out; one of its keys is."""
    quota.mark_key_exhausted("p", "key-a", 60)
    assert quota.key_available("p", "key-b") is True


def test_the_same_key_on_another_provider_is_untouched():
    quota.mark_key_exhausted("p", "key-a", 60)
    assert quota.key_available("other", "key-a") is True


def test_a_cooldown_expires():
    quota.mark_key_exhausted("p", "key-a", 0.01)
    time.sleep(0.05)
    assert quota.key_available("p", "key-a") is True


def test_usable_keys_puts_the_live_ones_first():
    quota.mark_key_exhausted("p", "spent", 60)
    assert quota.usable_keys("p", ["spent", "fresh"]) == ["fresh"]


def test_usable_keys_fails_open_when_all_are_spent():
    """A cooldown is an estimate; the provider is the only real authority, and
    refusing to try is worse than trying."""
    for k in ("a", "b"):
        quota.mark_key_exhausted("p", k, 60)
    assert quota.usable_keys("p", ["a", "b"]) == ["a", "b"]


def test_a_single_key_pool_is_returned_untouched():
    quota.mark_key_exhausted("p", "only", 60)
    assert quota.usable_keys("p", ["only"]) == ["only"]


def test_a_keyless_provider_is_handled():
    assert quota.key_available("p", None) is True
    assert quota.usable_keys("p", []) == []


def test_the_cooldown_defaults_to_the_window_reset():
    """A daily key should come back when the day does, not on a made-up timer."""
    quota.set_key_counter(lambda pid: 1)
    quota.mark_key_exhausted("openrouter", "key-a")      # no explicit seconds
    left = quota.key_cooldowns("openrouter")
    assert left and list(left.values())[0] > 60


def test_the_cooldowns_are_reportable():
    quota.mark_key_exhausted("p", "key-a", 120)
    assert list(quota.key_cooldowns("p").values())[0] > 0


# --------------------------------------------------------------------------- #
# Wired into both upstream paths
# --------------------------------------------------------------------------- #

def _app_src():
    with open("app.py", encoding="utf-8") as f:
        return f.read()


def test_the_key_counter_is_wired():
    src = _app_src()
    assert "quota.set_key_counter(_provider_key_count)" in src


def test_both_upstream_paths_skip_spent_keys():
    """/v1/chat/completions and the non-chat surfaces rotate the same pool."""
    assert _app_src().count("quota.usable_keys(pid, keys)") == 2


def test_both_paths_record_a_429_against_the_key():
    assert _app_src().count("quota.mark_key_exhausted(pid, key,") == 2


def test_a_429_is_attributed_to_the_key_not_the_provider():
    src = _app_src()
    i = src.index("quota.mark_key_exhausted(pid, key,")
    assert "THIS key is out, not the provider" in src[max(0, i - 300):i]
