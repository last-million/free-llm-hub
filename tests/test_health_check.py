"""One-click health check (POST/GET /api/health-check, Settings button).

Everything upstream is faked: the per-provider test (_health_test_provider),
the router behind /v1/chat/completions (_chat_completions) and the window
coverage. No network, no real settings.

NOTE: no pytest tmp_path here -- this machine's basetemp is permission-denied;
tempfile.mkdtemp(prefix="hub-pytest-") works.
"""

import json
import os
import re
import shutil
import tempfile
import threading

import pytest
from flask import Response

import app
import config


@pytest.fixture
def isolated_config(monkeypatch):
    root = tempfile.mkdtemp(prefix="hub-pytest-")
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", os.path.join(root, "state", "config.json"))
    with app._health_lock:
        app._health_state.update(running=False, phase="idle", done=0, total=0,
                                 current="", started_at=None, cancel_requested=False)
    app._health_cancel.clear()
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def no_network(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("health check test tried a real network call")
    monkeypatch.setattr(app.requests, "get", boom)
    monkeypatch.setattr(app.requests, "post", boom)
    monkeypatch.setattr(app.requests.Session, "request", boom)


PROVIDERS = [
    # (pid, name, has_key, needs_key)
    ("good", "Good AI", True, True),
    ("mixed", "Mixed AI", True, True),
    ("deadkey", "Dead Key AI", True, True),
    ("flaky", "Flaky AI", True, True),
    ("nokey", "No Key AI", False, True),
    ("spent", "Spent AI", True, True),
    ("open", "Open AI-less", False, False),
]

TEST_RESULTS = {
    "good": {"ok": True, "detail": "Key OK", "keys": [{"index": 0, "masked": "sk-a…1111", "ok": True}]},
    "mixed": {"ok": True, "detail": "1 of 2 keys work.",
              "keys": [{"index": 0, "masked": "sk-b…2222", "ok": True},
                       {"index": 1, "masked": "sk-c…3333", "ok": False,
                        "detail": "HTTP 401: invalid api key"}]},
    "deadkey": {"ok": False, "detail": "HTTP 401: Unauthorized", "keys": []},
    "flaky": {"ok": False, "detail": "ReadTimeout: timed out",
              "keys": [{"index": 0, "masked": "sk-d…4444", "ok": False,
                        "detail": "ReadTimeout: timed out"}]},
    "open": {"ok": True, "detail": "Key OK", "keys": []},
}


def _fake_chat(body):
    """Stand-in for the router: a plain answer, or a tool call when tools ride."""
    tools = bool(body.get("tools"))
    hdrs = {"X-Free-LLM-Hub-Provider": "good", "X-Free-LLM-Hub-Model": "m-1"}
    if body.get("stream"):
        if tools:
            chunks = [{"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "c1", "type": "function",
                 "function": {"name": app._HEALTH_TOOL_NAME, "arguments": "{}"}}]}}]}]
        else:
            chunks = [{"choices": [{"delta": {"content": "po"}}]},
                      {"choices": [{"delta": {"content": "ng"}}]}]

        def gen():
            for c in chunks:
                yield "data: %s\n\n" % json.dumps(c)
            yield "data: [DONE]\n\n"
        return Response(gen(), mimetype="text/event-stream", headers=hdrs)
    if tools:
        msg = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": app._HEALTH_TOOL_NAME, "arguments": "{}"}}]}
    else:
        msg = {"role": "assistant", "content": "pong"}
    return app.jsonify({"choices": [{"index": 0, "message": msg}]}), 200, hdrs


@pytest.fixture
def fakes(monkeypatch, isolated_config, no_network):
    tested, routed = [], []
    monkeypatch.setattr(app, "_health_enabled_providers", lambda: list(PROVIDERS))

    def out_status(pid, p, free_models=None):
        if pid == "spent":
            return {"status_reason": "exhausted", "until": None,
                    "detail": "used 100 of 100 this day"}
        return {"status_reason": "ok", "until": None, "detail": ""}
    monkeypatch.setattr(app, "_provider_out_status", out_status)

    def fake_test(pid):
        tested.append(pid)
        return dict(TEST_RESULTS[pid])
    monkeypatch.setattr(app, "_health_test_provider", fake_test)

    def fake_chat(body):
        routed.append(body)
        return _fake_chat(body)
    monkeypatch.setattr(app, "_chat_completions", fake_chat)
    monkeypatch.setattr(app, "_health_window_coverage", lambda: {
        "known": 8, "total": 10, "by_source": {"catalog": 8, "default": 2},
        "unknown_by_provider": {"good": 2}})
    return {"tested": tested, "routed": routed}


def test_run_tests_enabled_providers_sequentially_and_skips_with_reason(fakes):
    rep = app._health_run(threading.Event())
    assert rep["status"] == "done"
    # Skipped providers were never tested; the rest in order, one at a time.
    assert fakes["tested"] == ["good", "mixed", "deadkey", "flaky", "open"]
    rows = {r["id"]: r for r in rep["providers"]}
    assert rows["nokey"]["status"] == "skipped" and rows["nokey"]["reason"] == "no_key"
    assert rows["spent"]["status"] == "skipped" and rows["spent"]["reason"] == "exhausted"
    assert "100 of 100" in rows["spent"]["detail"]
    assert rows["open"]["status"] == "ok"      # keyless provider is tested, not skipped


def test_routing_check_runs_four_requests_through_the_hub_stack(fakes):
    rep = app._health_run(threading.Event())
    bodies = fakes["routed"]
    assert len(bodies) == 4
    assert [(bool(b.get("stream")), bool(b.get("tools"))) for b in bodies] == [
        (False, False), (True, False), (False, True), (True, True)]
    assert all(b["model"] == "auto" for b in bodies)
    assert all(r["ok"] for r in rep["routing"]), rep["routing"]
    assert rep["routing"][0]["provider"] == "good"
    assert rep["routing"][0]["model"] == "m-1"
    assert rep["routing"][1]["detail"].startswith("answered: pong")
    assert rep["routing"][3]["detail"] == "called %s" % app._HEALTH_TOOL_NAME


def test_routing_check_reports_failures(fakes, monkeypatch):
    def failing(body):
        if body.get("tools"):
            # A tool turn answered in plain text is NOT a pass.
            return app.jsonify({"choices": [{"message": {"content": "It is noon."}}]})
        return (app.jsonify({"error": {"message": "all providers failed"}}), 503,
                {"X-Free-LLM-Hub-Last-Error": "429"})
    monkeypatch.setattr(app, "_chat_completions", failing)
    rep = app._health_run(threading.Event())
    r = rep["routing"]
    assert r[0]["ok"] is False and "HTTP 503" in r[0]["detail"]
    assert "all providers failed" in r[0]["detail"] and "429" in r[0]["detail"]
    assert r[2]["ok"] is False and "without calling the tool" in r[2]["detail"]
    assert rep["summary"]["routing_ok"] == 0
    assert any(s.startswith("Routing check: 4 of 4") for s in rep["summary"]["recommendations"])


def test_summary_classifies_and_recommends(fakes):
    s = app._health_run(threading.Event())["summary"]
    assert s["working"] == ["good", "mixed", "open"]
    dead = {d["id"]: d for d in s["dead_keys"]}
    assert set(dead) == {"mixed", "deadkey"}
    assert dead["mixed"]["all_dead"] is False and [k["index"] for k in dead["mixed"]["keys"]] == [1]
    assert dead["deadkey"]["all_dead"] is True
    # A timeout is not a dead key.
    assert [f["id"] for f in s["failing"]] == ["flaky"]
    assert s["no_key"] == ["nokey"]
    assert [k["id"] for k in s["skipped"]] == ["spent"]
    recs = "\n".join(s["recommendations"])
    assert "Add an API key for No Key AI" in recs
    assert "Mixed AI: remove the dead key #2" in recs
    assert "Dead Key AI: its key failed the test (#1)" in recs
    assert "Flaky AI failed its test" in recs
    assert "2 of 10 usable models have no known context window" in recs


def test_report_is_persisted_in_state_dir(fakes):
    app._health_run(threading.Event())
    path = os.path.join(config.state_dir(), app.HEALTH_REPORT_NAME)
    assert os.path.isfile(path)
    with open(path, encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["status"] == "done" and saved["finished_at"] >= saved["started_at"]
    assert saved["windows"]["known"] == 8
    assert app._health_load_report()["summary"]["working"] == ["good", "mixed", "open"]


def test_run_never_changes_settings(fakes):
    config.set_provider_config("good", enabled=True)
    before = json.dumps(config.load_config(), sort_keys=True)
    app._health_run(threading.Event())
    assert json.dumps(config.load_config(), sort_keys=True) == before


def test_cancel_stops_between_steps_and_saves_partial_report(fakes, monkeypatch):
    cancel = threading.Event()

    def cancel_after_first(pid):
        fakes["tested"].append(pid)
        cancel.set()
        return dict(TEST_RESULTS[pid])
    monkeypatch.setattr(app, "_health_test_provider", cancel_after_first)
    rep = app._health_run(cancel)
    assert rep["status"] == "cancelled"
    assert fakes["tested"] == ["good"]
    assert [r["id"] for r in rep["providers"]] == ["good"]
    assert rep["routing"] == [] and fakes["routed"] == []
    assert app._health_load_report()["status"] == "cancelled"
    assert app._health_state["running"] is False


def _hdrs(token=True):
    h = {"X-Free-LLM-Hub": "dashboard"}
    if token:
        h["X-Free-LLM-Hub-Token"] = config.ensure_control_token()
    return h


def test_endpoints_are_control_token_gated(isolated_config):
    config.ensure_control_token()
    c = app.app.test_client()
    assert c.get("/api/health-check").status_code == 401
    assert c.post("/api/health-check", headers=_hdrs(token=False)).status_code == 401
    assert c.post("/api/health-check/cancel", headers=_hdrs(token=False)).status_code == 401
    r = c.get("/api/health-check", headers=_hdrs())
    assert r.status_code == 200
    j = r.get_json()
    assert j["running"] is False and j["report"] is None


def test_only_one_run_at_a_time_and_cancel_endpoint(isolated_config, monkeypatch):
    started, release = threading.Event(), threading.Event()
    seen = {}

    def fake_run(cancel):
        started.set()
        release.wait(5)
        seen["cancelled"] = cancel.is_set()
        with app._health_lock:
            app._health_state.update(running=False, phase="idle", cancel_requested=False)
    monkeypatch.setattr(app, "_health_run", fake_run)
    c = app.app.test_client()
    r1 = c.post("/api/health-check", headers=_hdrs(), json={})
    assert r1.status_code == 202 and r1.get_json()["running"] is True
    assert started.wait(5)
    r2 = c.post("/api/health-check", headers=_hdrs(), json={})
    assert r2.status_code == 409 and r2.get_json()["code"] == "already_running"
    rc = c.post("/api/health-check/cancel", headers=_hdrs(), json={})
    assert rc.get_json() == {"ok": True, "running": True}
    st = c.get("/api/health-check", headers=_hdrs()).get_json()
    assert st["running"] is True and st["progress"]["cancel_requested"] is True
    release.set()
    for _ in range(100):
        if not app._health_state["running"]:
            break
        threading.Event().wait(0.05)
    assert seen.get("cancelled") is True
    assert c.get("/api/health-check", headers=_hdrs()).get_json()["running"] is False


def test_skip_reason_uses_local_state(isolated_config, monkeypatch):
    monkeypatch.setattr(app, "_provider_out_status", lambda pid, p, free_models=None: {
        "status_reason": "no_free_tier", "detail": "no free tier"})
    assert app._health_skip_reason("x", True, True) == ("no_free_tier", "no free tier")
    assert app._health_skip_reason("x", False, True) == ("no_key", "no API key saved")
    monkeypatch.setattr(app, "_provider_out_status", lambda pid, p, free_models=None: {
        "status_reason": "parked", "detail": "parked"})
    # Parked is exactly what a re-test is for: not skipped.
    assert app._health_skip_reason("x", True, True) is None


def test_provider_test_reuses_the_test_button_view(isolated_config, no_network):
    # The real wrapper runs api_test_provider in-process; an unknown id comes
    # back as the view's own 404 verdict, with no network call.
    res = app._health_test_provider("definitely-not-a-provider")
    assert res["ok"] is False and res["detail"] == "unknown provider"


def test_window_coverage_reuses_model_windows(isolated_config, no_network, monkeypatch):
    monkeypatch.setattr(app, "_prefetch_auto_models", lambda pids: {"fakep": ["m1", "m2"]})
    monkeypatch.setattr(app, "_window_info",
                        lambda pid, m: (128000, "catalog") if m == "m1" else (32768, "default"))
    monkeypatch.setattr(app, "_model_ctx_info", lambda pid, m: (32768, "default"))
    monkeypatch.setattr(app, "_is_provider_dead", lambda pid: False)
    cov = app._health_window_coverage()
    assert cov["known"] == 1 and cov["total"] == 2
    assert cov["unknown_by_provider"] == {"fakep": 1}


def test_settings_ui_has_button_progress_and_escaped_report():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "templates", "index.html")
    with open(path, encoding="utf-8") as f:
        html = f.read()
    for needle in ('id="health-check-run"', 'id="health-check-cancel"',
                   'id="health-check-progress"', 'id="health-check-report"',
                   "initHealthCheck();", "api('/api/health-check'",
                   "api('/api/health-check/cancel'"):
        assert needle in html, needle
    start = html.index("function hcList")
    body = html[start:html.index("function initHealthCheck")]
    # Every server-provided string goes through esc(): list items and headers.
    assert "'<li>' + esc(t) + '</li>'" in body
    assert "esc(head)" in body
    assert "Last run: " in body          # the report carries its date
    # No raw interpolation of report fields into markup.
    assert not re.search(r"'>' \+ (r|d|f|k|p|s|rep)\.", body)
