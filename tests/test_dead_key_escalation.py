"""Only working keys: a key found dead again stays out longer.

REQUESTED 2026-09-29: "make sure it uses only working API keys". MEASURED:
zenmux keys #1/#3/#4/#5 answer 403 "no permission" on every Test, yet came
back every 6 hours and cost an attempt each time.
"""
import time

import pytest

import quota

PID = "zenmux"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(quota, "_KEY_DEAD", {})
    monkeypatch.setattr(quota, "_KEY_AUTH_STRIKES", {})
    monkeypatch.setattr(quota, "_persist_maybe", lambda: None)


def _left(key):
    rec = quota._KEY_DEAD[(PID, quota.key_fingerprint(key))]
    return rec["until"] - time.time(), rec["count"]


def _serve_time(key):
    rec = quota._KEY_DEAD[(PID, quota.key_fingerprint(key))]
    rec["until"] = time.time() - 1                    # its time is up


def test_each_repeat_doubles_the_time_out():
    quota.mark_key_dead(PID, "k1", why="Test: HTTP 403: no permission")
    left, n = _left("k1")
    assert n == 1 and left == pytest.approx(quota._KEY_DEAD_TEST_TTL, abs=5)
    _serve_time("k1")
    assert quota.key_dead(PID, "k1") is False          # usable again once served...
    quota.mark_key_dead(PID, "k1", why="Test: HTTP 403: no permission")
    left, n = _left("k1")                              # ...but found dead again
    assert n == 2 and left == pytest.approx(2 * quota._KEY_DEAD_TEST_TTL, abs=5)
    for _ in range(10):
        _serve_time("k1")
        quota.mark_key_dead(PID, "k1")
    left, _n = _left("k1")
    assert left == pytest.approx(quota._KEY_DEAD_MAX_TTL, abs=5)   # capped at 7 days


def test_a_success_clears_the_history():
    quota.mark_key_dead(PID, "k1")
    _serve_time("k1")
    quota.clear_key_dead(PID, "k1")
    quota.mark_key_dead(PID, "k1")
    assert _left("k1")[1] == 1


def test_an_old_mark_does_not_count():
    quota.mark_key_dead(PID, "k1")
    rec = quota._KEY_DEAD[(PID, quota.key_fingerprint("k1"))]
    rec["until"] = time.time() - quota._KEY_DEAD_REPEAT_WINDOW - 10
    quota.mark_key_dead(PID, "k1")
    assert _left("k1")[1] == 1


def test_a_provider_outage_never_escalates():
    for _ in range(4):
        quota.mark_key_dead(PID, "k1", quota._KEY_DEAD_LIVE_TTL, "Test: HTTP 503", escalate=False)
        _serve_time("k1")
    quota.mark_key_dead(PID, "k1", quota._KEY_DEAD_LIVE_TTL, "Test: HTTP 503", escalate=False)
    assert _left("k1")[0] == pytest.approx(quota._KEY_DEAD_LIVE_TTL, abs=5)


def test_the_test_verdict_escalates_key_failures_but_not_5xx(monkeypatch):
    import app
    seen = []
    monkeypatch.setattr(app.quota, "mark_key_dead",
                        lambda pid, key, ttl, why, source="test", escalate=True:
                        seen.append(escalate))
    app._note_key_test_verdict(PID, "k1", False, "HTTP 403: no permission")
    app._note_key_test_verdict(PID, "k1", False, "HTTP 503: no channel")
    assert seen == [True, False]
