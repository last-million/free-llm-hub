"""Every "out" provider card says why, until when, and who used it.

REPORTED 2026-09-26: the Providers page showed dahl/tokenrouter throttled and
four more parked with no reason and no countdown, and the user read that as a
bug ("I did not use them for 2 days") -- but the hub's own swarm stages, agent
sessions and probes spend quota too. /api/providers now carries, per provider:

  status_reason  ok | throttled | parked | exhausted | no_free_tier | models_dead
  until          epoch the provider is usable again (or None)
  detail         one short human line
  used_by        [{source, count}] for the current quota window

All local state, no network: the Flask test client plus direct pokes at the
in-memory maps (monkeypatched so nothing leaks between tests).
"""
import json
import tempfile
import threading
import time
from pathlib import Path

import pytest

import app
import config
import quota

_DASH = {"X-Free-LLM-Hub": "dashboard"}
PID = "groq"          # a free, non-paid provider with a researched catalog


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    # tempfile.mkdtemp, not tmp_path: pytest's temp root is permission-broken
    # on this machine (see AGENTS.md).
    path = Path(tempfile.mkdtemp()) / "state" / "config.json"
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(path))
    monkeypatch.setattr(quota, "_PERSIST_PATH", None)
    monkeypatch.setattr(quota, "_STATE", {})
    monkeypatch.setattr(quota, "_MODEL_STATE", {})
    monkeypatch.setattr(quota, "_MODEL_THROTTLE", {})
    monkeypatch.setattr(quota, "_DYNAMIC", {})
    monkeypatch.setattr(quota, "_SOURCE_STATE", {})
    monkeypatch.setattr(app, "_dead_providers", {})
    monkeypatch.setattr(app, "_dead_provider_why", {})
    monkeypatch.setattr(app, "_dead_models", {})
    monkeypatch.setattr(app, "_provider_consec_fail", {})
    monkeypatch.setattr(app, "_provider_timeout_fail", {})
    monkeypatch.setattr(app, "_provider_authfail", {})
    monkeypatch.setattr(app, "_provider_keyfail", set())
    # No live catalog discovery: the registry defaults are the model list.
    monkeypatch.setattr(app, "provider_free_models",
                        lambda pid, live=False: ["m-a", "m-b", "m-c"])
    yield


def _rows():
    headers = dict(_DASH, **{"X-Free-LLM-Hub-Token": config.ensure_control_token()})
    resp = app.app.test_client().get("/api/providers", headers=headers)
    assert resp.status_code == 200
    return {r["id"]: r for r in resp.get_json()}


def _row(pid=PID):
    return _rows()[pid]


# --------------------------------------------------------------------------- #
# status_reason / until / detail
# --------------------------------------------------------------------------- #

def test_every_row_carries_the_new_fields():
    for r in _rows().values():
        assert r["status_reason"] in ("ok", "throttled", "parked", "exhausted",
                                      "no_free_tier", "models_dead")
        assert "until" in r and "detail" in r
        assert isinstance(r["used_by"], list)


def test_healthy_provider_is_ok_with_no_until():
    r = _row()
    assert r["status_reason"] == "ok"
    assert r["until"] is None
    assert r["detail"] == ""


def test_parked_after_consecutive_failures_says_why_and_until():
    for _ in range(app._PROVIDER_CONSEC_FAIL_THRESHOLD):
        app._note_provider_result(PID, False, hard_fail=True)
    r = _row()
    assert r["status_reason"] == "parked"
    assert r["until"] and r["until"] > time.time()
    assert "parked 30 min after 4 consecutive failures" in r["detail"]


def test_parked_without_a_recorded_reason_still_explains():
    app._dead_providers[PID] = time.time() + 600
    r = _row()
    assert r["status_reason"] == "parked"
    assert "repeated hard failures" in r["detail"]


def test_expired_park_drops_its_reason():
    app._dead_providers[PID] = time.time() - 1
    app._dead_provider_why[PID] = "stale"
    assert app._is_provider_dead(PID) is False
    assert PID not in app._dead_provider_why
    assert _row()["status_reason"] == "ok"


def test_throttled_shows_countdown_until():
    quota.mark_throttled(PID, 120)
    r = _row()
    assert r["status_reason"] == "throttled"
    assert r["until"] >= int(time.time()) + 100
    assert "429" in r["detail"]


def test_morph_is_no_free_tier_not_an_outage():
    r = _row("morph")
    assert r["status_reason"] == "no_free_tier"
    assert r["until"] is None
    assert "no free tier" in r["detail"]


def test_some_dead_models_is_a_note_all_dead_is_models_dead():
    exp = time.time() + 3600
    app._dead_models[(PID, "m-a")] = exp
    r = _row()
    assert r["status_reason"] == "ok"
    assert "1 of 3 models dead" in r["detail"]
    app._dead_models[(PID, "m-b")] = exp
    app._dead_models[(PID, "m-c")] = exp - 100
    r = _row()
    assert r["status_reason"] == "models_dead"
    assert r["until"] == int(exp - 100)       # the first one back
    assert "all 3 models dead" in r["detail"]


def test_breaker_counters_may_be_any_sized_shape():
    # Another change may turn the authfail sets into dicts: the card only
    # needs a count, so every shape must read the same.
    app._provider_authfail[PID] = {"m-a": 2, "m-b": 1}
    app._provider_consec_fail[PID] = 2
    d = _row()["detail"]
    assert "auth failed on 2 models" in d
    assert "2 consecutive failures (parks at 4)" in d
    assert app._sized({"a", "b"}) == 2 and app._sized(3) == 3
    assert app._sized(None) == 0


def test_park_reason_survives_a_restart():
    app._dead_providers[PID] = time.time() + 600
    app._dead_provider_why[PID] = "3 requests in a row timed out"
    blob = json.loads(json.dumps(app._dead_state_dump()))
    app._dead_providers.clear()
    app._dead_provider_why.clear()
    app._dead_state_load(blob)
    assert app._dead_provider_why[PID] == "3 requests in a row timed out"


# --------------------------------------------------------------------------- #
# used_by: per-window attribution by source
# --------------------------------------------------------------------------- #

def test_used_by_counts_hub_work_by_source():
    with app._usage_source_as("swarm"):
        quota.record(PID, "m-a")
        quota.record(PID, "m-b")
    with app._usage_source_as("probe"):
        quota.record(PID, "m-a")
    quota.record(PID, "m-a")                  # a daemon thread, no request
    assert _row()["used_by"] == [
        {"source": "swarm", "count": 2},
        {"source": "background", "count": 1},
        {"source": "probe", "count": 1},
    ]


def test_source_labels_from_the_request():
    with app.app.test_request_context(
            "/v1/chat/completions", method="POST",
            headers={"User-Agent": "opencode/1.2.3"}):
        assert app._usage_source() == "OpenCode"
    with app.app.test_request_context(
            "/v1/responses", method="POST",
            environ_base={"flh.build_session": "abc123"}):
        assert app._usage_source() == "agent"
    with app.app.test_request_context("/api/providers"):
        assert app._usage_source() == "dashboard"
        with app._usage_source_as("swarm"):   # hub-internal work wins
            assert app._usage_source() == "swarm"
        assert app._usage_source() == "dashboard"
    assert app._usage_source() == "background"


def test_label_follows_the_work_into_a_worker_thread():
    seen = []
    with app._usage_source_as("swarm"):
        fn = app._carry_usage_source(lambda: seen.append(app._usage_source()))
    t = threading.Thread(target=fn)
    t.start()
    t.join()
    assert seen == ["swarm"]


def test_resolver_failure_never_breaks_recording(monkeypatch):
    def boom():
        raise RuntimeError("no")
    monkeypatch.setattr(quota, "_source_resolver", boom)
    quota.record(PID, "m-a")
    assert quota.sources(PID) == {"other": 1}
    assert quota.status(PID)["used"] == 1


def test_sources_persist_with_quota_state(monkeypatch):
    path = Path(tempfile.mkdtemp()) / "quota-state.json"
    monkeypatch.setattr(quota, "_PERSIST_PATH", str(path))
    monkeypatch.setattr(quota, "_extra_dump", None)
    with app._usage_source_as("swarm"):
        quota.record(PID, "m-a", n=3)
    quota.save_state()
    quota._SOURCE_STATE.clear()
    quota._load_state(str(path))
    assert quota.sources(PID) == {"swarm": 3}


# --------------------------------------------------------------------------- #
# Frontend contract: the card renders the reason, countdown and used-by line
# --------------------------------------------------------------------------- #

def test_card_renderer_uses_the_new_fields():
    html = (Path(app.__file__).resolve().parent / "templates" / "index.html"
            ).read_text(encoding="utf-8")
    for needle in ("function outReasonLine", "function usedByLine",
                   "p.status_reason", "data-until", "used by: ",
                   "back in ", "models_dead"):
        assert needle in html, needle
