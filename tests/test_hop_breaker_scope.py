"""Provider breakers must not park a WORKING provider on stale or per-model evidence.

MEASURED 2026-09-26 on the live hub:
  * aimlapi had 7 distinct 403'd models on record (threshold 8) and cloudflare 2,
    accumulated from sporadic per-model 403s over WEEKS -- the evidence only
    reset when the provider's own dead marker expired, and it was persisted.
  * dahl sat at 6 consecutive strikes: every timed-out/5xx hop throttled the
    WHOLE provider, doubling up to an hour, though only single models were slow.
  * quota-state kept strikes for throttles that had expired weeks before.
"""
import json
import os
import shutil
import tempfile
import time

import pytest
import requests

import app
import quota


@pytest.fixture
def breakers(monkeypatch):
    monkeypatch.setattr(app, "_provider_authfail", {})
    monkeypatch.setattr(app, "_provider_keyfail", {})
    monkeypatch.setattr(app, "_dead_providers", {})
    monkeypatch.setattr(app, "_hop_model_fail", {})


@pytest.fixture
def calls(monkeypatch):
    rec = {"provider": [], "model": []}
    monkeypatch.setattr(app.quota, "mark_throttled",
                        lambda pid, secs=None: rec["provider"].append((pid, secs)))
    monkeypatch.setattr(app.quota, "mark_model_throttled",
                        lambda pid, model, secs=None: rec["model"].append((pid, model, secs)))
    return rec


# --- 1. auth-fail evidence is a 24 h sliding window ------------------------------

def test_weeks_old_403s_do_not_count_toward_parking_the_provider(breakers):
    old = time.time() - 3 * 24 * 3600
    app._provider_authfail["aimlapi"] = {"m%d" % i: old for i in range(7)}
    app._mark_provider_authfail("aimlapi", "fresh-model", 403)
    assert not app._is_provider_dead("aimlapi")
    assert set(app._provider_authfail["aimlapi"]) == {"fresh-model"}


def test_eight_fresh_distinct_403s_still_park_the_provider(breakers):
    for i in range(app._PROVIDER_FORBIDDEN_THRESHOLD):
        app._mark_provider_authfail("aimlapi", "m%d" % i, 403)
    assert app._is_provider_dead("aimlapi")


def test_a_stale_401_no_longer_lowers_the_403_threshold(breakers):
    app._provider_keyfail["cloudflare"] = time.time() - 2 * 24 * 3600
    for i in range(app._PROVIDER_AUTHFAIL_THRESHOLD):
        app._mark_provider_authfail("cloudflare", "m%d" % i, 403)
    assert not app._is_provider_dead("cloudflare"), \
        "a 401 from days ago must not keep the strict 3-model threshold armed"
    assert "cloudflare" not in app._provider_keyfail


def test_a_fresh_401_still_uses_the_strict_threshold(breakers):
    for i in range(app._PROVIDER_AUTHFAIL_THRESHOLD):
        app._mark_provider_authfail("badkey", "m%d" % i, 401)
    assert app._is_provider_dead("badkey")


# --- persistence: backward compatible, window applied on load --------------------

def test_dump_keeps_legacy_shape_and_adds_timestamps(breakers):
    app._mark_provider_authfail("p", "m1", 401)
    blob = app._dead_state_dump()
    assert blob["provider_authfail"] == {"p": ["m1"]}
    assert blob["provider_keyfail"] == ["p"]
    assert set(blob["provider_authfail_ts"]["p"]) == {"m1"}
    assert "p" in blob["provider_keyfail_ts"]
    json.dumps(blob)  # must stay JSON-serialisable


def test_load_restores_fresh_timestamped_evidence_and_drops_old(breakers):
    now = time.time()
    app._dead_state_load({
        "provider_authfail": {"p": ["new", "old"]},
        "provider_keyfail": ["p", "q"],
        "provider_authfail_ts": {"p": {"new": now - 60, "old": now - 3 * 24 * 3600}},
        "provider_keyfail_ts": {"p": now - 60, "q": now - 3 * 24 * 3600},
    })
    assert set(app._provider_authfail["p"]) == {"new"}
    assert "p" in app._provider_keyfail and "q" not in app._provider_keyfail


def test_legacy_untimestamped_state_loads_without_error_but_is_not_revived(breakers):
    """Old files carry bare lists with no age -- the very weeks-old tally that
    was parking providers. Read cleanly, dropped deliberately."""
    app._dead_state_load({"provider_authfail": {"aimlapi": ["a", "b", "c", "d", "e", "f", "g"]},
                          "provider_keyfail": ["aimlapi"]})
    assert app._provider_authfail == {}
    assert app._provider_keyfail == {}


def test_prune_tolerates_a_legacy_set_value(breakers):
    app._provider_authfail["p"] = {"m1", "m2"}
    app._mark_provider_authfail("p", "m3", 403)
    assert set(app._provider_authfail["p"]) == {"m3"}


# --- 2. timeout / 5xx cooldown is per-model unless provider-wide ------------------

def test_one_failed_model_throttles_only_that_model(breakers, calls):
    app._throttle_failed_hop("dahl", "slow-model")
    assert calls["model"] == [("dahl", "slow-model", app._HOP_COOLDOWN_DEFAULT)]
    assert calls["provider"] == []


def test_the_same_model_failing_twice_does_not_escalate(breakers, calls):
    app._throttle_failed_hop("dahl", "slow-model")
    app._throttle_failed_hop("dahl", "slow-model")
    assert calls["provider"] == []


def test_two_distinct_models_within_the_window_escalate_to_the_provider(breakers, calls):
    app._throttle_failed_hop("dahl", "a")
    app._throttle_failed_hop("dahl", "b")
    assert calls["provider"] == [("dahl", app._HOP_COOLDOWN_DEFAULT)]


def test_a_failure_older_than_the_window_does_not_escalate(breakers, calls):
    app._hop_model_fail["dahl"] = {"a": time.time() - app._HOP_ESCALATE_WINDOW - 5}
    app._throttle_failed_hop("dahl", "b")
    assert calls["provider"] == []


def test_other_providers_failures_do_not_combine(breakers, calls):
    app._throttle_failed_hop("dahl", "a")
    app._throttle_failed_hop("groq", "b")
    assert calls["provider"] == []


@pytest.mark.parametrize("exc", [
    requests.exceptions.ConnectTimeout("connect timed out"),
    requests.exceptions.SSLError("certificate verify failed"),
    requests.exceptions.ConnectionError("[WinError 10061] target machine actively refused it"),
    requests.exceptions.ConnectionError("Failed to resolve 'api.example' (getaddrinfo failed)"),
])
def test_host_level_failures_throttle_the_whole_provider_at_once(breakers, calls, exc):
    app._throttle_failed_hop("dahl", "a", exc=exc)
    assert calls["provider"] == [("dahl", app._HOP_COOLDOWN_DEFAULT)]


def test_a_read_timeout_is_model_scoped(breakers, calls):
    app._throttle_failed_hop("dahl", "a", exc=requests.exceptions.ReadTimeout("read timed out"))
    assert calls["provider"] == []
    assert calls["model"] == [("dahl", "a", app._HOP_COOLDOWN_DEFAULT)]


def test_no_model_id_falls_back_to_the_provider(breakers, calls):
    app._throttle_failed_hop("dahl", None)
    assert calls["provider"] == [("dahl", app._HOP_COOLDOWN_DEFAULT)]


def test_throttle_helper_never_raises(breakers, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("quota exploded")
    monkeypatch.setattr(app.quota, "mark_model_throttled", boom)
    app._throttle_failed_hop("dahl", "a")  # must not raise into the hop loop


# --- 3. stale strikes are cleared on load ----------------------------------------

@pytest.fixture
def state_file(monkeypatch):
    d = tempfile.mkdtemp(prefix="hub-pytest-strikes-")
    monkeypatch.setattr(quota, "_STATE", {})
    try:
        yield os.path.join(d, "quota-state.json")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _write(path, blob):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(blob, f)


def test_strikes_from_a_throttle_that_expired_weeks_ago_are_cleared(state_file):
    old = time.time() - 14 * 24 * 3600
    _write(state_file, {"state": {"dahl": {"count": 0, "window_start": old,
                                           "throttled_until": old + 3600,
                                           "strikes": 6, "last_strike": old}}})
    quota._load_state(state_file)
    assert quota._STATE["dahl"]["strikes"] == 0
    assert quota._STATE["dahl"]["last_strike"] == 0.0


def test_recent_strikes_survive_a_restart(state_file):
    now = time.time()
    _write(state_file, {"state": {"groq": {"count": 0, "window_start": now,
                                           "throttled_until": now + 120,
                                           "strikes": 3, "last_strike": now - 10}}})
    quota._load_state(state_file)
    assert quota._STATE["groq"]["strikes"] == 3


def test_odd_strike_values_do_not_break_load(state_file):
    _write(state_file, {"state": {"x": {"throttled_until": "soon", "strikes": 2,
                                        "last_strike": None}}})
    quota._load_state(state_file)
    assert "x" in quota._STATE
