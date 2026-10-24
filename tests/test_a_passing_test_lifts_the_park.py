"""A Test that makes a real generation succeed lifts the provider's park.

REPORTED 2026-10-24 (ZenMux): the card read "Parked - back in 25m - parked 30 min after 4
consecutive failures (4xx)" while its Test said "Key OK". Measured the same minute: all four
saved keys answered HTTP 200 on both free models. The park had been put there earlier, when
the old pay-as-you-go keys were still saved and 403'd, and a passing Test only RECORDED its
result -- nothing ever lifted the park, so a healthy provider stayed out of routing for the
rest of the 30 minutes and its card stayed red.

A verified generation is the strongest evidence the hub gets that a provider works, so it now
clears the park, the failure counters that led to it and the dead marks of the models that
just answered. A Test that FAILS lifts nothing, and live traffic re-parks after four more
hard failures, so nothing is hidden.
"""
import time
from unittest import mock

import pytest

import app
import config

PID = "groq"


def _client():
    app.app.config["TESTING"] = True
    return app.app.test_client()


def _hdrs():
    return {"X-Free-LLM-Hub-Token": config.ensure_control_token(),
            "X-Free-LLM-Hub": "dashboard"}


def _resp(status, body=None):
    r = mock.Mock(status_code=status)
    r.json.return_value = body if body is not None else {
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}
    r.headers = {}
    r.text = "{}"
    r.close = mock.Mock()
    return r


def _run_test(fake_chat):
    with mock.patch.object(config, "get_provider_config",
                           return_value={"api_key": "K", "api_keys": ["K"], "enabled": True}), \
            mock.patch.object(app, "_models_url_for", return_value=None), \
            mock.patch.object(app, "_upstream_chat", side_effect=fake_chat), \
            mock.patch.object(app, "_record_test_result", return_value=([], [])):
        return _client().post("/api/test/" + PID, headers=_hdrs()).get_json()


def _park():
    with app._provider_dead_lock:
        app._dead_providers[PID] = time.time() + 1800
        app._dead_provider_why[PID] = "4 consecutive failures (4xx)"
        app._provider_consec_fail[PID] = 3
        app._provider_authfail[PID] = {"m1": time.time()}
        app._provider_timeout_fail[PID] = 2


@pytest.fixture(autouse=True)
def _clean():
    def wipe():
        with app._provider_dead_lock:
            for d in (app._dead_providers, app._dead_provider_why, app._provider_consec_fail,
                      app._provider_authfail, app._provider_timeout_fail):
                d.pop(PID, None)
    wipe()
    yield
    wipe()


def test_a_passing_test_lifts_the_park_and_the_counters():
    _park()
    assert app._is_provider_dead(PID)
    body = _run_test(lambda pid, payload, stream, **kw: _resp(200))
    assert body["ok"] is True
    assert not app._is_provider_dead(PID)
    assert app._provider_out_status(PID, app.prov.get_provider(PID))["status_reason"] != "parked"
    with app._provider_dead_lock:
        assert PID not in app._dead_provider_why
        assert PID not in app._provider_consec_fail
        assert PID not in app._provider_authfail
        assert PID not in app._provider_timeout_fail


def test_a_passing_test_clears_the_dead_mark_of_the_model_that_answered():
    _park()
    answered = []

    def fake(pid, payload, stream, **kw):
        answered.append(payload.get("model"))
        return _resp(200)

    # pre-mark every default model dead, then let the Test answer on whichever it tries
    for m in app.prov.get_provider(PID).get("default_free_models") or []:
        with app._dead_lock:
            app._dead_models[(PID, m)] = time.time() + 3600
    body = _run_test(fake)
    assert body["ok"] is True and answered
    for m in answered:
        assert not app._is_model_dead_upstream(PID, m), m


def test_a_failing_test_lifts_nothing():
    _park()
    body = _run_test(lambda pid, payload, stream, **kw: _resp(403))
    assert body["ok"] is False
    assert app._is_provider_dead(PID)                       # still parked
    with app._provider_dead_lock:
        assert app._provider_consec_fail.get(PID) == 3      # counters untouched


def test_the_switch_off_keeps_the_old_behaviour(monkeypatch):
    _park()
    orig = config.get_flag
    monkeypatch.setattr(config, "get_flag",
                        lambda k, d=None: False if k == "test_unparks_provider" else orig(k, d))
    body = _run_test(lambda pid, payload, stream, **kw: _resp(200))
    assert body["ok"] is True
    assert app._is_provider_dead(PID)                       # a pass no longer lifts the park


def test_the_helper_never_raises_on_garbage():
    app._unpark_after_verified_generation(None, None)
    app._unpark_after_verified_generation("no-such-provider", [None, 7, "x"])
