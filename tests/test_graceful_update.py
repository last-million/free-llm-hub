"""Graceful updates: drain, restart, continue (2026-10-08).

Owner: "even after auto update the hub should let working jobs WAIT RETRYING
until the update finishes and they continue working after restarting with the
new system, if there are updates."

Hermetic: a fake clock for the drain, a fake pull (see test_hub_self_update),
a fake re-exec (tests/conftest.py makes app._do_reexec a tripwire; the tests
that need to see it swap in a recorder -- NOTHING here replaces the process).
"""
import json
import os
import shutil
import tempfile
import threading
import time

import pytest

import app
import config
import graceful_update as GU
import swarm_windows as SW

HTML = open("templates/index.html", encoding="utf-8").read()
AGENTS = open("AGENTS.md", encoding="utf-8").read()
REAL_SLEEP = time.sleep


# --------------------------------------------------------------------------- #
# fixtures and fakes
# --------------------------------------------------------------------------- #

@pytest.fixture
def state(monkeypatch):
    d = tempfile.mkdtemp(prefix="hub-pytest-graceful-")
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", os.path.join(d, "state", "config.json"))
    app._runtime_active[0] = 0
    monkeypatch.setitem(app._auto_update_state, "updating", False)
    try:
        yield os.path.join(d, "state")
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def registry():
    app.agentic_chat._REGISTRY.clear()
    yield app.agentic_chat._REGISTRY
    app.agentic_chat._REGISTRY.clear()


@pytest.fixture
def runs():
    made = []
    yield made
    for rid in made:
        SW._RUNS.pop(rid, None)


@pytest.fixture
def fast_sleep(monkeypatch):
    """Every sleep in the app returns at once (the waiters poll in a loop)."""
    monkeypatch.setattr(app.time, "sleep", lambda s: REAL_SLEEP(0.005))


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class _Sess:
    def __init__(self, busy=False, stopped=False):
        self.turn_lock = threading.Lock()
        self.last_interrupted = stopped
        self.stop_pending = False
        if busy:
            self.turn_lock.acquire()


def _sess(registry, sid, busy=True, stopped=False):
    registry[sid] = _Sess(busy=busy, stopped=stopped)
    return registry[sid]


def _run(runs, state_name, owner=None, session=None):
    run = SW._Run("g", ".", "opencode", [{"title": "a", "task": "t", "needs": []}],
                  owner=owner)
    run.state = state_name
    if session:
        run.agents[0].session_id = session
    SW._RUNS[run.id] = run
    runs.append(run.id)
    return run


def _stub_pull(monkeypatch, before="aaaaaaa1111", after="bbbbbbb2222", deps_ok=True):
    monkeypatch.setattr(app, "_is_git_repo", lambda: True)
    monkeypatch.setattr(app, "_origin_is_trusted", lambda: True)
    monkeypatch.setattr(app, "_hub_mode_is_off", lambda: False)
    monkeypatch.setattr(app, "_sync_deps_after_pull", lambda: deps_ok)

    def fake_git(*args, **kw):
        if args[:2] == ("status", "--porcelain"):
            return 0, "", ""
        if args == ("rev-parse", "HEAD"):
            fake_git.calls += 1
            return 0, (before if fake_git.calls == 1 else after), ""
        return 0, "", ""
    fake_git.calls = 0
    monkeypatch.setattr(app, "_git", fake_git)


def _client():
    return app.app.test_client()


DASH = {"X-Free-LLM-Hub": "dashboard"}


def _drain(monkeypatch=None, sessions=(), runs_=(), reason="update", clock=None, to="bbbbbbb2222",
           frm="aaaaaaa1111", max_seconds=600):
    d = GU.Drain(clock=clock or time.time)
    d.begin(to, frm, max_seconds=max_seconds, sessions=sessions, runs=runs_, reason=reason)
    if monkeypatch is not None:
        monkeypatch.setattr(app, "_UPDATE_DRAIN", d)
    return d


# --------------------------------------------------------------------------- #
# graceful_update.py -- the drain (pure, fake clock)
# --------------------------------------------------------------------------- #

def test_the_drain_counts_down_on_its_own_clock():
    clock = _Clock()
    d = GU.Drain(clock=clock)
    assert not d.active() and d.snapshot() is None and d.info() is None
    assert d.begin("bbbbbbb2222", "aaaaaaa1111", max_seconds=100)
    assert d.active() and d.seconds_left() == 100 and not d.expired()
    clock.t += 60
    assert d.seconds_left() == 40 and not d.expired()
    clock.t += 40
    assert d.expired() and d.seconds_left() == 0
    d.end()
    assert not d.active() and not d.expired()


def test_a_second_begin_keeps_the_first_snapshot_and_deadline():
    clock = _Clock()
    d = GU.Drain(clock=clock)
    assert d.begin("bbbbbbb", "aaaaaaa", max_seconds=100, sessions=("s1",))
    clock.t += 30
    assert d.begin("ccccccc", "bbbbbbb", max_seconds=999, sessions=("s2",)) is False
    snap = d.snapshot()
    assert snap["to"] == "bbbbbbb" and snap["sessions"] == frozenset({"s1"})
    assert d.seconds_left() == 70


def test_retry_after_is_whole_seconds_clamped_to_five_and_thirty():
    clock = _Clock()
    d = GU.Drain(clock=clock)
    d.begin("bbbbbbb", max_seconds=600)
    assert d.retry_after() == GU.RETRY_AFTER_MAX == 30
    clock.t += 590.4                   # 9.6 s left -> 10
    assert d.retry_after() == 10
    clock.t += 7                       # 2.6 s left -> floor of 5
    assert d.retry_after() == GU.RETRY_AFTER_MIN == 5
    clock.t += 100                     # expired: still a sane header
    assert d.retry_after() == 5


def test_running_work_is_admitted_new_work_is_not():
    d = GU.Drain()
    d.begin("bbbbbbb", sessions=("busy-sess",), runs=("run-1",))
    run_of = {"w-late": "run-1", "w-other": "run-9"}.get
    assert d.admits("busy-sess", run_of)
    assert d.admits("w-late", run_of)          # a worker started after the snapshot
    assert not d.admits("w-other", run_of)
    assert not d.admits("fresh", run_of)
    assert not d.admits(None, run_of)
    assert not d.admits("w-late", lambda sid: 1 / 0)   # a broken lookup never admits
    d.end()
    assert not d.admits("busy-sess", run_of)


def test_the_refusal_names_the_version_and_the_wait():
    assert GU.refusal_message("bbbbbbb", 12) == "The hub is updating to bbbbbbb; retry in 12 s."
    assert GU.refusal_message("bbbbbbb", 12, "restart") == "The hub is restarting; retry in 12 s."
    assert GU.refusal_message("", 7) == "The hub is restarting; retry in 7 s."


# --------------------------------------------------------------------------- #
# graceful_update.py -- the marker
# --------------------------------------------------------------------------- #

def _marker(state_dir, now, **kw):
    kw.setdefault("sessions", ["conv-a"])
    kw.setdefault("runs", [{"run_id": "r1", "owner": "conv-o"}])
    return GU.build_marker("aaaaaaa1111", "bbbbbbb2222", now, state_dir, **kw)


def test_the_marker_is_written_atomically(state, monkeypatch):
    now = 5000.0
    path = GU.write_marker(state, _marker(state, now))
    assert path == os.path.join(state, "update-resume.json")
    assert sorted(os.listdir(state)) == ["update-resume.json"]       # no temp file left
    on_disk = json.load(open(path, encoding="utf-8"))
    assert on_disk["from"] == "aaaaaaa" and on_disk["to"] == "bbbbbbb"
    assert on_disk["sessions"] == ["conv-a"]
    assert on_disk["runs"] == [{"run_id": "r1", "owner": "conv-o"}]
    assert on_disk["written_at"] == now

    # A write that dies half way leaves the PREVIOUS marker whole and no debris.
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(GU.json, "dump", boom)
    assert GU.write_marker(state, _marker(state, now + 1, sessions=["other"])) is None
    assert json.load(open(path, encoding="utf-8"))["sessions"] == ["conv-a"]
    assert sorted(os.listdir(state)) == ["update-resume.json"]


def test_a_fresh_marker_is_read_back(state):
    GU.write_marker(state, _marker(state, 1000.0))
    got = GU.read_marker(state, 1000.0 + 5 * 60)
    assert got and got["sessions"] == ["conv-a"]
    assert os.path.isfile(GU.marker_path(state))        # reading is not consuming


@pytest.mark.parametrize("age", [GU.MARKER_MAX_AGE + 1, 3600, 86400])
def test_a_stale_marker_is_ignored_and_removed(state, age):
    GU.write_marker(state, _marker(state, 1000.0))
    assert GU.read_marker(state, 1000.0 + age) is None
    assert not os.path.exists(GU.marker_path(state))


def test_a_corrupt_marker_is_ignored_and_removed(state):
    os.makedirs(state, exist_ok=True)
    for junk in ("{not json", "[]", "null", '{"v": 1}', json.dumps({"v": 99})):
        with open(GU.marker_path(state), "w", encoding="utf-8") as fh:
            fh.write(junk)
        assert GU.read_marker(state, 1000.0) is None
        assert not os.path.exists(GU.marker_path(state))
    assert GU.read_marker(state, 1000.0) is None          # missing file: quietly None


def test_a_marker_from_another_state_dir_or_the_future_or_with_no_work_is_ignored(state):
    other = _marker(os.path.join(state, "elsewhere"), 1000.0)
    GU.write_marker(state, other)
    assert GU.read_marker(state, 1000.0) is None
    GU.write_marker(state, _marker(state, 1000.0 + 3600))            # clock far ahead
    assert GU.read_marker(state, 1000.0) is None
    GU.write_marker(state, _marker(state, 1000.0, sessions=[], runs=[]))
    assert GU.read_marker(state, 1000.0) is None


def test_the_plan_knows_its_own_and_goes_away_when_both_consumers_ran(state):
    GU.write_marker(state, _marker(state, 1000.0))
    plan = GU.ResumePlan(GU.read_marker(state, 1000.0), state)
    assert plan.wants_session("conv-a") and not plan.wants_session("conv-b")
    assert plan.wants_run("r1") and plan.wants_run("zzz", owner="conv-o")
    assert not plan.wants_run("zzz", owner="conv-z") and not plan.wants_run("zzz")
    assert plan.notice() == "Continued automatically after the update to bbbbbbb."
    assert plan.finish("turns") is False and os.path.exists(GU.marker_path(state))
    assert plan.finish("runs") is True and not os.path.exists(GU.marker_path(state))


def test_a_plain_restart_says_restart_not_update(state):
    m = GU.build_marker("ccccccc", "ccccccc", 1000.0, state, sessions=["s"], reason="restart")
    assert GU.ResumePlan(m, state).notice() == "Continued automatically after the restart."


# --------------------------------------------------------------------------- #
# the drain starts when an update is pending AND the restart must wait
# --------------------------------------------------------------------------- #

def test_a_pulled_update_with_busy_work_starts_the_drain(state, registry, monkeypatch, fast_sleep):
    _stub_pull(monkeypatch)
    _sess(registry, "busy-sess")
    done = threading.Event()
    monkeypatch.setattr(app, "_reexec_soon", lambda: done.set())

    result = app._do_update_check()

    assert "deferred" in result and "1 task" in result
    snap = app._UPDATE_DRAIN.snapshot()
    assert snap and snap["to"] == "bbbbbbb" and snap["from"] == "aaaaaaa"
    assert snap["sessions"] == frozenset({"busy-sess"}) and snap["reason"] == "update"
    assert not done.wait(0.1), "must not restart while the snapshotted turn runs"
    registry["busy-sess"].turn_lock.release()
    assert done.wait(2.0), "the restart proceeds as soon as nothing is left"


def test_no_drain_when_nothing_is_pending_or_nothing_is_busy(state, registry, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_reexec_soon", lambda: calls.append("now"))
    monkeypatch.setattr(app, "_reexec_when_idle", lambda b, r=(): calls.append("wait"))
    _stub_pull(monkeypatch, before="same7890", after="same7890")       # no new commits
    _sess(registry, "busy-sess")
    assert "up to date" in app._do_update_check()
    assert not app._UPDATE_DRAIN.active() and calls == []

    registry.clear()                                                     # idle hub
    _stub_pull(monkeypatch)
    app._do_update_check()
    assert calls == ["now"] and not app._UPDATE_DRAIN.active()

    _sess(registry, "busy-sess")                                         # hub switched off
    calls.clear()
    monkeypatch.setattr(app, "_hub_mode_is_off", lambda: True)
    app._do_update_check()
    assert calls == [] and not app._UPDATE_DRAIN.active()


def test_a_failed_dependency_install_starts_no_drain(state, registry, monkeypatch):
    _stub_pull(monkeypatch, deps_ok=False)
    _sess(registry, "busy-sess")
    monkeypatch.setattr(app, "_reexec_when_idle", lambda b, r=(): None)
    app._do_update_check()
    assert not app._UPDATE_DRAIN.active()


# --------------------------------------------------------------------------- #
# new work gets 503 + Retry-After, in each protocol's own error shape
# --------------------------------------------------------------------------- #

def _check_503(resp):
    assert resp.status_code == 503
    assert GU.RETRY_AFTER_MIN <= int(resp.headers["Retry-After"]) <= GU.RETRY_AFTER_MAX
    assert resp.headers["x-should-retry"] == "true"
    return resp.get_json()


def test_chat_completions_and_the_other_openai_posts_get_the_openai_shape(state, monkeypatch):
    _drain(monkeypatch)
    for path in ("/v1/chat/completions", "/v1/responses", "/v1/completions",
                 "/v1/embeddings", "/v1/images/generations"):
        body = _check_503(_client().post(path, json={"model": "auto"}))
        assert body["error"]["type"] == "server_error" and body["error"]["code"] == 503, path
        assert "updating to bbbbbbb" in body["error"]["message"], path
        assert "retry in" in body["error"]["message"], path


def test_anthropic_messages_get_an_api_error_not_an_overload_count(state, monkeypatch):
    """Claude Code retries any 5xx with Retry-After, but counts overloaded_error
    bodies toward its 3-strikes "Repeated 529" stop (opus/fable/mythos ids)."""
    _drain(monkeypatch)
    body = _check_503(_client().post("/v1/messages", json={"model": "auto"}))
    assert body["type"] == "error" and body["error"]["type"] == "api_error"
    assert "The hub is updating to bbbbbbb; retry in " in body["error"]["message"]


def test_gemini_gets_its_own_unavailable_envelope(state, monkeypatch):
    _drain(monkeypatch)
    body = _check_503(_client().post("/v1beta/models/gemini-x:generateContent",
                                     json={"contents": []}))
    assert body["error"]["code"] == 503 and body["error"]["status"] == "UNAVAILABLE"
    assert "updating to bbbbbbb" in body["error"]["message"]


def test_ollama_gets_its_own_envelope(state, monkeypatch):
    config.set_flag("ollama_api", True)
    _drain(monkeypatch)
    body = _check_503(_client().post("/api/chat", json={"model": "auto"}))
    assert isinstance(body["error"], str) and "updating to bbbbbbb" in body["error"]


def test_a_plain_restart_drain_says_restarting(state, monkeypatch):
    _drain(monkeypatch, reason="restart", to="ccccccc", frm="ccccccc")
    body = _check_503(_client().post("/v1/chat/completions", json={"model": "auto"}))
    assert body["error"]["message"].startswith("The hub is restarting; retry in ")


def test_the_dashboard_doors_to_new_work_get_the_same_503(state, monkeypatch):
    _drain(monkeypatch)
    c = _client()
    for path in ("/api/agent/sessions/abc/message", "/api/agent/sessions/abc/message/stream"):
        body = _check_503(c.post(path, json={"text": "go"}, headers=DASH))
        assert body["code"] == "hub_updating" and body["status"] == 503 and body["text"] is None
        assert "updating to bbbbbbb" in body["detail"] and body["detail"] == body["error"]
        assert body["retry_after"] == int(c.post(path, json={"text": "go"}, headers=DASH)
                                          .headers["Retry-After"])
    body = _check_503(c.post("/api/swarm-windows", json={"goal": "x", "project_dir": "."},
                             headers=DASH))
    assert "updating to bbbbbbb" in body["error"]["message"]
    _check_503(c.post("/api/enhance-prompt", json={"text": "x"}, headers=DASH))
    for tool in ("crew_start", "crew_run", "swarm_windows_start"):
        body = _check_503(c.post("/mcp", json={"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                               "params": {"name": tool, "arguments": {}}}))
        assert body["jsonrpc"] == "2.0" and body["id"] == 7
        assert "updating to bbbbbbb" in body["error"]["message"]


def test_dashboard_pages_and_read_only_gets_stay_up(state, monkeypatch):
    _drain(monkeypatch)
    c = _client()
    assert c.get("/").status_code == 200              # (rendering it mints the control token)
    tok = {"X-Free-LLM-Hub-Token": config.get_control_token() or ""}
    assert c.get("/v1/models").status_code != 503
    assert c.get("/api/hub/stopped", headers=tok).status_code == 200
    assert c.get("/api/agent/settings", headers=tok).status_code == 200
    # token counting runs no model; MCP reads and a stop are not new work
    assert c.post("/v1/messages/count_tokens", json={"messages": []}).status_code != 503
    for body in ({"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "crew_result", "arguments": {"job_id": "nope"}}}):
        assert c.post("/mcp", json=body).status_code != 503
    runtime = c.get("/api/runtime", headers=tok).get_json()
    assert runtime["updating"]["to"] == "bbbbbbb"
    assert GU.RETRY_AFTER_MIN <= runtime["updating"]["retry_after"] <= GU.RETRY_AFTER_MAX
    ready = c.get("/ready")
    assert ready.status_code == 503 and ready.get_json()["reason"] == "draining"


def test_nothing_is_refused_when_no_drain_runs(state):
    c = _client()
    assert c.get("/api/runtime").get_json()["updating"] is None
    assert c.post("/api/agent/sessions/abc/message", json={"text": "go"},
                  headers=DASH).status_code != 503
    assert c.post("/v1/messages/count_tokens", json={"messages": []}).status_code != 503


def test_a_request_from_running_work_is_served_a_new_one_is_not(state, monkeypatch, runs):
    run = _run(runs, SW.RUNNING, session="w-late")
    _drain(monkeypatch, sessions=("running-sess",), runs_=(run.id,))

    def ask(sid):
        env = {"flh.build_session": sid} if sid else {}
        with app.app.test_request_context("/v1/chat/completions", method="POST",
                                          environ_overrides=env):
            return app._update_drain_refusal()

    assert ask("running-sess") is None          # the turn's own next model call
    assert ask("w-late") is None                # a worker of a snapshotted run
    assert ask("brand-new-sess")[1] == 503
    assert ask(None)[1] == 503                  # a terminal CLI waits and retries


def test_the_update_drain_refuses_nothing_for_a_get(state, monkeypatch):
    _drain(monkeypatch)
    with app.app.test_request_context("/v1/models", method="GET"):
        assert app._update_drain_refusal() is None


# --------------------------------------------------------------------------- #
# in-flight work finishes, then the restart proceeds AT ONCE; or the cap hits
# --------------------------------------------------------------------------- #

def test_in_flight_work_finishes_then_the_restart_proceeds_at_once(state, registry, monkeypatch,
                                                                   fast_sleep, runs):
    run = _run(runs, SW.RUNNING)
    _sess(registry, "busy-sess")
    app._runtime_active[0] = 1
    done = threading.Event()
    monkeypatch.setattr(app, "_reexec_soon", lambda: done.set())
    app._note_update_labels("aaaaaaa1111", "bbbbbbb2222")

    app._reexec_when_idle({"busy-sess"}, {run.id})

    assert app._UPDATE_DRAIN.active()
    assert not done.wait(0.15)
    registry["busy-sess"].turn_lock.release()
    assert not done.wait(0.15), "the run and the request are still going"
    run.state = SW.DONE
    assert not done.wait(0.15), "the in-flight request is still going"
    app._runtime_active[0] = 0
    assert done.wait(1.0)


def test_new_work_that_arrives_during_the_drain_does_not_starve_the_restart(
        state, registry, monkeypatch, fast_sleep):
    """The old wait counted EVERY /v1 request in flight, so a hub that kept
    receiving work never restarted. Now such work is refused at the door, so
    the counter can only fall."""
    _sess(registry, "busy-sess")
    done = threading.Event()
    monkeypatch.setattr(app, "_reexec_soon", lambda: done.set())
    app._reexec_when_idle({"busy-sess"})
    for _ in range(25):
        r = _client().post("/v1/chat/completions", json={"model": "auto"})
        assert r.status_code == 503
    assert app._runtime_active[0] == 0           # refused requests are never counted
    registry["busy-sess"].turn_lock.release()
    assert done.wait(1.0)


def test_the_cap_restarts_anyway_and_the_wait_is_the_setting(state, registry, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(app, "_UPDATE_DRAIN", GU.Drain(clock=clock))
    config.set_setting("update_drain_max_seconds", 50)
    assert app._update_drain_max() == 50.0
    _sess(registry, "never-ends")
    done = threading.Event()
    monkeypatch.setattr(app, "_reexec_soon", lambda: done.set())

    def tick(seconds):
        clock.t += seconds
        REAL_SLEEP(0.001)
    monkeypatch.setattr(app.time, "sleep", tick)

    app._reexec_when_idle({"never-ends"})

    assert done.wait(5.0), "after update_drain_max_seconds the restart goes ahead"
    assert 50 <= clock.t - 1000.0 <= 52               # one second per look
    assert registry["never-ends"].turn_lock.locked()  # nothing was waited out


def test_the_drain_cap_setting_is_clamped_and_defaults_to_ten_minutes(state):
    assert app._update_drain_max() == 600.0 == GU.DEFAULT_DRAIN_MAX
    for raw, want in ((30, 30.0), ("45", 45.0), ("abc", 600.0), (None, 600.0),
                      (0, 1.0), (-5, 1.0), (10 ** 9, 86400.0)):
        config.set_setting("update_drain_max_seconds", raw)
        assert app._update_drain_max() == want, raw


# --------------------------------------------------------------------------- #
# the resume marker, just before the re-exec
# --------------------------------------------------------------------------- #

def _restart_and_wait(monkeypatch):
    """Run the REAL _reexec_soon with a recording stand-in for the re-exec."""
    gone = threading.Event()
    monkeypatch.setattr(app, "_do_reexec", lambda: gone.set())
    monkeypatch.setattr(app.time, "sleep", lambda s: REAL_SLEEP(0.005))
    app._reexec_soon()
    assert gone.wait(3.0), "the (fake) re-exec never ran"


def test_the_marker_names_what_is_running_and_nothing_else(state, registry, runs, monkeypatch):
    known = {"conv-a", "conv-stop", "conv-o", "conv-w", "conv-x"}
    monkeypatch.setattr(app.agentic_history, "known_session_ids", lambda: set(known))
    _sess(registry, "conv-a")                                  # a plain turn: listed
    _sess(registry, "conv-stop", stopped=True)                 # Stop pressed: not listed
    _sess(registry, "conv-gone")                               # deleted conversation: not listed
    _sess(registry, "conv-idle", busy=False)                   # nothing running: not listed
    _sess(registry, "conv-o")                                  # owner of a listed run: via the run
    _sess(registry, "w-1")                                     # a Multi worker: via its run
    r1 = _run(runs, SW.RUNNING, owner="conv-o", session="w-1")
    r2 = _run(runs, SW.PENDING)                                # no conversation (Swarm tab)
    _run(runs, SW.STOPPED, owner="conv-x")                     # stopped: not listed
    _run(runs, SW.RUNNING, owner="conv-deleted")               # its conversation is gone
    app._note_update_labels("aaaaaaa1111", "bbbbbbb2222")

    _restart_and_wait(monkeypatch)

    got = GU.read_marker(config.state_dir(), time.time())
    assert got is not None
    assert got["from"] == "aaaaaaa" and got["to"] == "bbbbbbb" and got["reason"] == "update"
    assert got["sessions"] == ["conv-a"]
    assert sorted(got["runs"], key=lambda r: r["run_id"]) == sorted(
        [{"run_id": r1.id, "owner": "conv-o"}, {"run_id": r2.id, "owner": None}],
        key=lambda r: r["run_id"])
    assert os.path.abspath(got["state_dir"]) == os.path.abspath(config.state_dir())


def test_no_marker_when_nothing_was_running(state, registry, monkeypatch):
    _sess(registry, "idle", busy=False)
    _restart_and_wait(monkeypatch)
    assert not os.path.exists(GU.marker_path(config.state_dir()))


def test_the_setting_and_the_restart_request_can_switch_the_marker_off(state, registry, monkeypatch):
    monkeypatch.setattr(app.agentic_history, "known_session_ids", lambda: {"conv-a"})
    _sess(registry, "conv-a")
    config.set_setting("resume_after_update", False)
    _restart_and_wait(monkeypatch)
    assert not os.path.exists(GU.marker_path(config.state_dir()))
    config.set_setting("resume_after_update", True)
    app._UPDATE_RESUME_WANTED[0] = False
    _restart_and_wait(monkeypatch)
    assert not os.path.exists(GU.marker_path(config.state_dir()))


def test_a_user_stop_wins_over_a_pending_restart(state, registry, monkeypatch):
    monkeypatch.setattr(app.agentic_history, "known_session_ids", lambda: {"conv-a"})
    _sess(registry, "conv-a")
    _drain(monkeypatch)
    config.set_intentional_stop()
    gone = threading.Event()
    monkeypatch.setattr(app, "_do_reexec", lambda: gone.set())
    monkeypatch.setattr(app.time, "sleep", lambda s: REAL_SLEEP(0.005))
    app._auto_update_state["updating"] = True

    app._reexec_soon()

    assert not gone.wait(0.4), "a stopped hub must never be brought back by an update"
    assert not os.path.exists(GU.marker_path(config.state_dir()))
    assert not app._UPDATE_DRAIN.active() and app._auto_update_state["updating"] is False
    config.clear_intentional_stop()


def test_a_stop_in_the_runtime_state_also_wins(state, monkeypatch):
    monkeypatch.setattr(app.config, "get_runtime_state",
                        lambda: {"desired": "stopped", "phase": "draining"})
    assert app._restart_is_vetoed_by_stop() is True


def test_a_failed_reexec_ends_the_drain_and_clears_updating(state, monkeypatch):
    _drain(monkeypatch)
    app._auto_update_state["updating"] = True
    ran = threading.Event()

    def broken():
        ran.set()
        raise OSError("no such executable")
    monkeypatch.setattr(app, "_do_reexec", broken)
    monkeypatch.setattr(app.time, "sleep", lambda s: REAL_SLEEP(0.005))
    app._reexec_soon()
    assert ran.wait(3.0)
    for _ in range(100):
        if not app._auto_update_state["updating"]:
            break
        REAL_SLEEP(0.01)
    assert app._auto_update_state["updating"] is False and not app._UPDATE_DRAIN.active()


# --------------------------------------------------------------------------- #
# the next boot continues exactly what the marker names
# --------------------------------------------------------------------------- #

def _fresh_marker(sessions=("conv-a",), runs=(("r1", "conv-o"),), **kw):
    m = GU.build_marker("aaaaaaa1111", "bbbbbbb2222", time.time(), config.state_dir(),
                        sessions=list(sessions),
                        runs=[{"run_id": r, "owner": o} for r, o in runs], **kw)
    assert GU.write_marker(config.state_dir(), m)


class _R:
    def __init__(self, owner, rid="r1"):
        self.owner, self.id = owner, rid


def test_listed_multi_runs_resume_whether_or_not_the_box_is_ticked(state, monkeypatch):
    monkeypatch.setattr(app.agentic_history, "auto_resume", lambda sid: sid == "ticked")
    monkeypatch.setattr(app.agentic_history, "known_session_ids",
                        lambda: {"conv-o", "conv-n", "ticked"})
    _fresh_marker(runs=(("r1", "conv-o"),))
    assert app._multi_should_auto_resume(_R(None)) is True               # no conversation
    assert app._multi_should_auto_resume(_R("ticked", "rX")) is True     # the box, as before
    assert app._multi_should_auto_resume(_R("conv-o", "rY")) is True     # listed owner
    assert app._multi_should_auto_resume(_R("conv-n", "r1")) is True     # listed run id
    assert app._multi_should_auto_resume(_R("conv-n", "rZ")) is False    # not on the list


def test_without_a_marker_the_box_is_the_only_rule(state, monkeypatch):
    monkeypatch.setattr(app.agentic_history, "auto_resume", lambda sid: sid == "ticked")
    assert app._multi_should_auto_resume(_R("ticked")) is True
    assert app._multi_should_auto_resume(_R("conv-o")) is False


def test_a_deleted_conversation_is_never_resumed(state, monkeypatch):
    monkeypatch.setattr(app.agentic_history, "auto_resume", lambda sid: False)
    monkeypatch.setattr(app.agentic_history, "known_session_ids", lambda: set())
    _fresh_marker(sessions=("conv-a",), runs=(("r1", "conv-o"),))
    assert app._multi_should_auto_resume(_R("conv-o", "r1")) is False


@pytest.fixture
def store(tmp_path_factory, monkeypatch):
    """The real swarm_windows persistence on a temp folder (see
    test_a_restart_does_not_end_the_work): a run is saved, the process 'dies'
    (the registry is cleared), and load() finds it interrupted."""
    monkeypatch.setenv(SW._STORE_ENV, str(tmp_path_factory.mktemp("swarm")))
    monkeypatch.setattr(SW, "SPAWN_STAGGER", 0)
    with SW._LOCK:
        SW._RUNS.clear()
    yield
    with SW._LOCK:
        SW._RUNS.clear()


PHASES = [{"title": "A", "task": "do a", "needs": []},
          {"title": "B", "task": "do b", "needs": []}]


def _cut_run(owner, state=None):
    """A run as the next hub finds it: A done, B was running (or stopped)."""
    run = SW._Run("build it", ".", "opencode", PHASES, owner=owner)
    run.agents[0].state = SW.DONE
    run.agents[0].summary = "a was done before"
    run.agents[1].state = state or SW.RUNNING
    run.state = state or SW.RUNNING
    SW._persist(run)
    return run


def _wait_done(rid, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        st = SW.status(rid)
        if st and st["state"] not in (SW.PENDING, SW.RUNNING):
            return st
        REAL_SLEEP(0.05)
    raise AssertionError("run did not finish")


def _spawn(cli_id, project_dir):
    return "sess-" + os.urandom(3).hex()


def _turn(session_id, prompt):
    yield {"event": "message", "text": "redone"}


def test_a_restart_continues_exactly_the_listed_runs_whatever_the_box_says(
        state, store, monkeypatch):
    monkeypatch.setattr(app.agentic_history, "auto_resume", lambda sid: False)
    monkeypatch.setattr(app.agentic_history, "known_session_ids", lambda: {"conv-o", "conv-n"})
    listed = _cut_run("conv-o")
    unlisted = _cut_run("conv-n")
    with SW._LOCK:
        SW._RUNS.clear()
    assert SW.load() == 2
    _fresh_marker(sessions=(), runs=((listed.id, "conv-o"),))
    noted = []
    monkeypatch.setattr(app.memory, "note_interrupted",
                        lambda sid, **k: noted.append((sid, k.get("why"))))

    got = SW.resume_interrupted(_spawn, _turn, should_resume=app._multi_should_auto_resume)

    assert got == [listed.id]
    assert _wait_done(listed.id)["state"] == SW.DONE
    app._file_unresumed_runs()
    assert noted == [("conv-n", "hub restarted")], "the unlisted run still waits for Continue"
    assert SW.get(unlisted.id).interrupted is True


def test_a_run_the_owner_stopped_is_not_resumed_even_though_it_was_listed(
        state, store, monkeypatch):
    monkeypatch.setattr(app.agentic_history, "auto_resume", lambda sid: False)
    monkeypatch.setattr(app.agentic_history, "known_session_ids", lambda: {"conv-o"})
    run = _cut_run("conv-o", state=SW.STOPPED)          # Stop pressed before the exec
    with SW._LOCK:
        SW._RUNS.clear()
    SW.load()
    _fresh_marker(sessions=(), runs=((run.id, "conv-o"),))
    assert SW.resume_interrupted(_spawn, _turn,
                                 should_resume=app._multi_should_auto_resume) == []
    assert SW.get(run.id).state == SW.STOPPED


def test_stale_or_corrupt_markers_change_nothing_at_boot(state, monkeypatch):
    monkeypatch.setattr(app.agentic_history, "auto_resume", lambda sid: False)
    monkeypatch.setattr(app.agentic_history, "known_session_ids", lambda: {"conv-o"})
    m = GU.build_marker("a", "b", time.time() - 3600, config.state_dir(),
                        runs=[{"run_id": "r1", "owner": "conv-o"}])
    GU.write_marker(config.state_dir(), m)
    assert app._update_resume_plan() is None
    assert not os.path.exists(GU.marker_path(config.state_dir()))
    assert app._multi_should_auto_resume(_R("conv-o")) is False

    app._UPDATE_PLAN.update({"loaded": False, "plan": None})
    with open(GU.marker_path(config.state_dir()), "w", encoding="utf-8") as fh:
        fh.write("{oops")
    assert app._update_resume_plan() is None


def test_the_plan_is_read_once_per_boot(state):
    _fresh_marker()
    first = app._update_resume_plan()
    assert first is not None and app._update_resume_plan() is first


def test_listed_single_turns_continue_regardless_of_the_box(state, monkeypatch):
    monkeypatch.setattr(app.agentic_history, "auto_resume", lambda sid: sid == "ticked")
    monkeypatch.setattr(app.agentic_history, "known_session_ids",
                        lambda: {"conv-a", "ticked", "waits"})
    _fresh_marker(sessions=("conv-a", "conv-deleted"), runs=())
    plan = app._update_resume_plan()
    resumed, sent, notes = [], [], []
    monkeypatch.setattr(app, "api_agent_resume_session", lambda sid: resumed.append(sid))
    monkeypatch.setattr(app.agentic_chat, "send_message_stream_durable",
                        lambda sid, text: (sent.append((sid, text))
                                           or iter([{"event": "tool", "text": "x"}])))
    monkeypatch.setattr(app.agentic_chat, "live_notice", lambda sid, t: notes.append((sid, t)))

    app._auto_continue_turns(["conv-a", "ticked", "waits", "conv-deleted"], plan)

    assert resumed == ["conv-a", "ticked"]
    assert sent == [("conv-a", app._CONTINUE_TEXT), ("ticked", app._CONTINUE_TEXT)]
    # one notice line, for the conversation the UPDATE continued -- not for the
    # one that ticked its own box
    assert notes == [("conv-a", "Continued automatically after the update to bbbbbbb.")]


def test_an_error_first_event_gets_no_notice(state, monkeypatch):
    monkeypatch.setattr(app.agentic_history, "auto_resume", lambda sid: False)
    monkeypatch.setattr(app.agentic_history, "known_session_ids", lambda: {"conv-a"})
    _fresh_marker(sessions=("conv-a",), runs=())
    notes = []
    monkeypatch.setattr(app, "api_agent_resume_session", lambda sid: None)
    monkeypatch.setattr(app.agentic_chat, "send_message_stream_durable",
                        lambda sid, text: iter([{"event": "error", "status": 409}]))
    monkeypatch.setattr(app.agentic_chat, "live_notice", lambda sid, t: notes.append(sid))
    app._auto_continue_turns(["conv-a"], app._update_resume_plan())
    assert notes == []


def test_the_live_notice_lands_in_the_conversations_live_buffer():
    ac = app.agentic_chat
    ac._live_begin("conv-live")
    ac.live_notice("conv-live", "Continued automatically after the restart.")
    events = list(ac._LIVE["conv-live"].events)
    ac._LIVE.pop("conv-live", None)
    assert events == [{"event": "notice", "text": "Continued automatically after the restart."}]
    ac.live_notice("no-such-turn", "dropped quietly")           # no live turn: no error


def test_boot_recovery_hands_the_plan_to_the_turn_continuer_and_drops_the_marker(
        state, monkeypatch, fast_sleep):
    monkeypatch.setattr(app.agentic_history, "known_session_ids", lambda: {"conv-a"})
    _fresh_marker(sessions=("conv-a",), runs=(("r1", "conv-o"),))
    monkeypatch.setattr(app.memory, "recover_inflight", lambda: ["conv-a", "conv-b"])
    monkeypatch.setattr(app.memory, "prune_orphans", lambda *a, **k: [])
    seen, done = [], threading.Event()
    monkeypatch.setattr(app, "_auto_continue_turns",
                        lambda ids, plan=None: (seen.append((list(ids), plan)), done.set()))
    app._recover_memory_state()
    assert done.wait(3.0)
    ids, plan = seen[0]
    assert ids == ["conv-a", "conv-b"] and plan.wants_session("conv-a")
    assert os.path.exists(GU.marker_path(config.state_dir())), "the runs half has not run yet"

    monkeypatch.setattr(app.swarm_windows, "resume_interrupted", lambda *a, **k: [])
    monkeypatch.setattr(app, "_file_unresumed_runs", lambda: None)
    monkeypatch.setattr(app, "_worker_mode_keys", lambda: ())
    monkeypatch.setattr(app, "_swarm_windows_manager_kw", lambda: {})
    monkeypatch.setattr(app, "_multi_check_kwargs", lambda: {})
    for _ in range(200):                                  # the turns thread files its half
        if plan._left == {"runs"}:
            break
        REAL_SLEEP(0.01)
    app._resume_interrupted_swarms()
    assert not os.path.exists(GU.marker_path(config.state_dir())), "marker deleted after use"


def test_boot_with_no_cut_turns_still_finishes_the_turns_half(state, monkeypatch):
    _fresh_marker(sessions=("conv-a",), runs=())
    monkeypatch.setattr(app.memory, "recover_inflight", lambda: [])
    monkeypatch.setattr(app.memory, "prune_orphans", lambda *a, **k: [])
    plan = app._update_resume_plan()
    app._recover_memory_state()
    assert plan._left == {"runs"}


def test_a_resumed_conversation_run_gets_one_notice_line_first(state, monkeypatch):
    monkeypatch.setattr(app.agentic_history, "known_session_ids", lambda: {"conv-o"})
    _fresh_marker(sessions=(), runs=(("run-1", "conv-o"),))

    class _Resumed:
        owner, cli_id, id = "conv-o", "opencode", "run-1"
    monkeypatch.setattr(app.swarm_windows, "resume_interrupted", lambda *a, **k: ["run-1"])
    monkeypatch.setattr(app.swarm_windows, "get", lambda rid: _Resumed())
    monkeypatch.setattr(app, "_file_unresumed_runs", lambda: None)
    monkeypatch.setattr(app, "_worker_mode_keys", lambda: ())
    monkeypatch.setattr(app, "_swarm_windows_manager_kw", lambda: {})
    monkeypatch.setattr(app, "_multi_check_kwargs", lambda: {})
    monkeypatch.setattr(app, "_multi_follow_events",
                        lambda rid, cli: iter([{"event": "tool", "text": "Phase 1"}]))
    got, done = [], threading.Event()

    def fake_live_run(owner, events):
        got.append((owner, list(events)))
        done.set()
        return iter(())
    monkeypatch.setattr(app.agentic_chat, "live_run", fake_live_run)

    try:
        assert app._resume_interrupted_swarms() == ["run-1"]
        assert done.wait(3.0)
    finally:
        app._MULTI_RUNS.pop("conv-o", None)
    owner, events = got[0]
    assert owner == "conv-o"
    assert events[0] == {"event": "notice",
                         "text": "Continued automatically after the update to bbbbbbb."}
    assert events[1] == {"event": "tool", "text": "Phase 1"}


def test_a_run_the_box_resumed_gets_no_update_notice(state, monkeypatch):
    monkeypatch.setattr(app.agentic_history, "known_session_ids", lambda: {"conv-o", "other"})
    _fresh_marker(sessions=(), runs=(("run-1", "conv-o"),))

    class _Resumed:
        owner, cli_id, id = "other", "opencode", "run-2"
    monkeypatch.setattr(app.swarm_windows, "resume_interrupted", lambda *a, **k: ["run-2"])
    monkeypatch.setattr(app.swarm_windows, "get", lambda rid: _Resumed())
    for name, val in (("_file_unresumed_runs", lambda: None), ("_worker_mode_keys", lambda: ()),
                      ("_swarm_windows_manager_kw", lambda: {}),
                      ("_multi_check_kwargs", lambda: {})):
        monkeypatch.setattr(app, name, val)
    monkeypatch.setattr(app, "_multi_follow_events", lambda rid, cli: iter([{"event": "tool"}]))
    got, done = [], threading.Event()
    monkeypatch.setattr(app.agentic_chat, "live_run",
                        lambda owner, events: (got.append(list(events)), done.set(), iter(()))[2])
    try:
        app._resume_interrupted_swarms()
        assert done.wait(3.0)
    finally:
        app._MULTI_RUNS.pop("other", None)
    assert got[0] == [{"event": "tool"}]


# --------------------------------------------------------------------------- #
# the flag off = today's behaviour
# --------------------------------------------------------------------------- #

def test_flag_off_starts_no_drain_writes_no_marker_and_ignores_a_stop(state, registry, monkeypatch,
                                                                       fast_sleep):
    config.set_flag("graceful_update", False)
    monkeypatch.setattr(app.agentic_history, "known_session_ids", lambda: {"conv-a"})
    real_reexec_soon = app._reexec_soon
    _sess(registry, "conv-a")
    done = threading.Event()
    monkeypatch.setattr(app, "_reexec_soon", lambda: done.set())

    app._reexec_when_idle({"conv-a"})
    assert not app._UPDATE_DRAIN.active()
    assert _client().post("/v1/messages/count_tokens", json={}).status_code != 503
    assert not done.wait(0.1)
    registry["conv-a"].turn_lock.release()
    assert done.wait(2.0)

    monkeypatch.setattr(app, "_reexec_soon", real_reexec_soon)     # the real one, as before
    gone = threading.Event()
    monkeypatch.setattr(app, "_do_reexec", lambda: gone.set())
    monkeypatch.setattr(app.time, "sleep", lambda s: REAL_SLEEP(0.005))
    registry["conv-a"].turn_lock.acquire()
    config.set_intentional_stop()
    real_reexec_soon()
    assert gone.wait(3.0), "flag off keeps the old unconditional re-exec"
    assert not os.path.exists(GU.marker_path(config.state_dir()))
    config.clear_intentional_stop()


def test_flag_off_reads_no_marker_and_the_route_says_so(state):
    config.set_flag("graceful_update", False)
    _fresh_marker()
    assert app._update_resume_plan() is None
    r = _client().post("/api/hub/restart", json={}, headers=DASH)
    assert r.status_code == 409 and "switched off" in r.get_json()["error"]


def test_resume_after_update_off_reads_no_marker(state):
    config.set_setting("resume_after_update", False)
    _fresh_marker()
    assert app._update_resume_plan() is None


def test_the_flag_and_the_settings_default_the_way_the_owner_asked(state):
    assert app._graceful_update_on() is True
    assert app._resume_after_update_on() is True
    assert app._update_drain_max() == 600.0


# --------------------------------------------------------------------------- #
# POST /api/hub/restart -- the same path for an operator or an agent
# --------------------------------------------------------------------------- #

def test_the_restart_route_is_guarded_like_every_post(state):
    c = _client()
    assert c.post("/api/hub/restart", json={}).status_code == 403          # no dashboard header
    assert c.get("/api/hub/restart").status_code == 405


def test_the_restart_route_restarts_an_idle_hub_at_once(state, registry, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_reexec_soon", lambda: calls.append("now"))
    monkeypatch.setattr(app, "_reexec_when_idle", lambda b, r=(): calls.append("wait"))
    monkeypatch.setattr(app, "_current_version_label", lambda: "ccccccc")
    r = _client().post("/api/hub/restart", json={}, headers=DASH)
    assert r.status_code == 202
    body = r.get_json()
    assert body["ok"] and body["restarting"] and body["waiting_for"] == 0 and body["resume"] is True
    assert calls == ["now"]
    assert app._UPDATE_LABELS == {"from": "ccccccc", "to": "ccccccc", "reason": "restart"}


def test_the_restart_route_drains_busy_work_and_honours_resume_false(state, registry, runs,
                                                                     monkeypatch):
    _sess(registry, "conv-a")
    run = _run(runs, SW.RUNNING)
    calls = []
    monkeypatch.setattr(app, "_reexec_soon", lambda: calls.append("now"))
    monkeypatch.setattr(app, "_reexec_when_idle", lambda b, r=(): calls.append(("wait", set(b), set(r))))
    monkeypatch.setattr(app, "_current_version_label", lambda: "ccccccc")
    r = _client().post("/api/hub/restart", json={"resume": False}, headers=DASH)
    assert r.status_code == 202 and r.get_json()["waiting_for"] == 2
    assert r.get_json()["resume"] is False and app._UPDATE_RESUME_WANTED[0] is False
    assert calls == [("wait", {"conv-a"}, {run.id})]


def test_the_restart_route_can_skip_the_wait(state, registry, monkeypatch):
    _sess(registry, "conv-a")
    calls = []
    monkeypatch.setattr(app, "_reexec_soon", lambda: calls.append("now"))
    monkeypatch.setattr(app, "_reexec_when_idle", lambda b, r=(): calls.append("wait"))
    monkeypatch.setattr(app, "_current_version_label", lambda: "ccccccc")
    r = _client().post("/api/hub/restart", json={"drain": False}, headers=DASH)
    assert r.status_code == 202 and calls == ["now"]


def test_the_restart_route_refuses_a_stopped_hub_and_a_second_request(state, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_reexec_soon", lambda: calls.append("now"))
    monkeypatch.setattr(app, "_current_version_label", lambda: "ccccccc")
    config.set_intentional_stop()
    r = _client().post("/api/hub/restart", json={}, headers=DASH)
    assert r.status_code == 409 and calls == []
    config.clear_intentional_stop()

    assert _client().post("/api/hub/restart", json={}, headers=DASH).status_code == 202
    again = _client().post("/api/hub/restart", json={}, headers=DASH)
    assert again.status_code == 202 and again.get_json().get("already") is True
    assert calls == ["now"]


def test_the_restart_route_rejects_a_non_object_body(state):
    r = _client().post("/api/hub/restart", data="[1]", content_type="application/json",
                       headers=DASH)
    assert r.status_code == 400


def test_the_auto_update_state_shows_the_drain(state, monkeypatch):
    assert _client().get("/api/auto-update").get_json()["draining"] is None
    _drain(monkeypatch)
    d = _client().get("/api/auto-update").get_json()["draining"]
    assert d["to"] == "bbbbbbb" and d["max_seconds"] == 600


def test_heartbeats_start_nothing_while_draining(state, monkeypatch):
    monkeypatch.setattr(app.swarm_windows, "list_runs", lambda: [])
    monkeypatch.setattr(app, "_activity", [])
    assert app._hb_busy({}) is False
    _drain(monkeypatch)
    assert app._hb_busy({}) is True


# --------------------------------------------------------------------------- #
# the page: static checks only
# --------------------------------------------------------------------------- #

def test_the_page_says_it_in_plain_english_in_both_places():
    assert "The hub is updating. Running jobs finish or wait, then continue by themselves." in HTML
    assert 'id="update-banner"' in HTML and 'id="agent-update-banner"' in HTML
    for ident in ("update-banner", "agent-update-banner"):
        tag = HTML[HTML.index('id="%s"' % ident) - 120:HTML.index('id="%s"' % ident) + 160]
        assert 'role="status"' in tag and 'aria-live="polite"' in tag and "hidden" in tag


def test_the_banner_follows_the_runtime_state_and_the_lists_say_waiting():
    assert "function refreshUpdateStatus()" in HTML
    assert "api('/api/runtime')" in HTML and "r.updating" in HTML
    assert "window.hubUpdating = on;" in HTML
    assert HTML.count("waiting for update") >= 3
    assert "refreshUpdateStatus();\n" in HTML                       # on load and on resync


def test_a_refused_send_shows_the_hubs_own_sentence():
    body = HTML[HTML.index("function pumpSse(resp, handle)"):][:900]
    assert "resp.status === 503" in body and "j.detail" in body


def test_the_banner_css_uses_theme_tokens_only():
    css = HTML[HTML.index(".update-banner{"):][:700]
    css = css[:css.index(".agent-update-banner")] + css[css.index(".agent-update-banner"):][:160]
    assert "#" not in css.replace("#update", "") and "rgb" not in css
    for token in ("--warn-text", "--warn-soft", "--warn-border"):
        assert token in css


# --------------------------------------------------------------------------- #
# the docs
# --------------------------------------------------------------------------- #

def test_the_docs_section_is_the_last_one_and_covers_the_evidence():
    head = "## Graceful updates: drain, restart, continue (2026-10-08)"
    assert AGENTS.count(head) == 1
    assert AGENTS.index(head) > AGENTS.index("## Tests")
    tail = AGENTS[AGENTS.index(head) + len(head):]
    assert "\n## " not in tail                                  # nothing after it
    for needle in ("update-resume.json", "graceful_update", "update_drain_max_seconds",
                   "resume_after_update", "POST /api/hub/restart", "Retry-After"):
        assert needle in tail, needle


def test_the_readme_counts_the_new_route():
    import re
    src = open("app.py", encoding="utf-8").read()
    assert "/api/hub/restart" in src
    assert "%d routes in total" % len(re.findall(r"@app\.route\(", src)) in open(
        "README.md", encoding="utf-8").read()
