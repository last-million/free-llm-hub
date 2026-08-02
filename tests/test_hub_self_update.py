"""Settings + sidebar "Update" button: git pull the hub's own repo.

The backend for this already existed (the 5-hourly auto-updater in
_do_update_check()/_reexec_soon() -- pull --ff-only, then os.execv to apply
it) and POST /api/auto-update {check:true} already triggered one cycle on
demand, so the button itself is thin: it just calls that existing route.

What did NOT exist: _reexec_soon() re-exec'd unconditionally, the instant a
pull brought new commits -- with zero regard for whether an agent chat
session was mid-turn or a /v1/* request was in flight. Killing the process
out from under either is exactly "stopped my work": an agentic turn can
legitimately run up to _TURN_TIMEOUT (1800s), and re-exec drops every
in-flight connection outright (no HTTP response, no final SSE event).

Fix: _agentic_busy_session_ids() snapshots which sessions are mid-turn AT
THE MOMENT the update is requested; if that snapshot (plus the /v1/* inflight
counter) isn't empty, _reexec_when_idle() waits for exactly that snapshotted
set to finish before restarting -- deliberately NOT any session that starts
afterward, so a hub in continuous use can't defer the restart forever.

This is the SAME code path the existing 5-hourly background auto-updater
already uses (_do_update_check() is shared), so the fix applies there too,
not just to the new manual button.
"""
import os
import tempfile
import threading
import time
import shutil

import pytest

import app


@pytest.fixture
def isolated_config(monkeypatch):
    d = tempfile.mkdtemp(prefix="hub-pytest-selfupdate-")
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", os.path.join(d, "state", "config.json"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def clean_registry():
    app.agentic_chat._REGISTRY.clear()
    yield app.agentic_chat._REGISTRY
    app.agentic_chat._REGISTRY.clear()


@pytest.fixture
def clean_runtime_active():
    saved = app._runtime_active[0]
    app._runtime_active[0] = 0
    yield
    app._runtime_active[0] = saved


class _FakeSession:
    def __init__(self, busy=False):
        self.turn_lock = threading.Lock()
        if busy:
            self.turn_lock.acquire()


def _register(registry, sid, busy=False):
    registry[sid] = _FakeSession(busy=busy)
    return sid


# --------------------------------------------------------------------------- #
# _agentic_busy_session_ids
# --------------------------------------------------------------------------- #

def test_busy_snapshot_includes_only_locked_sessions(clean_registry):
    _register(clean_registry, "idle-1", busy=False)
    _register(clean_registry, "busy-1", busy=True)
    _register(clean_registry, "idle-2", busy=False)
    assert app._agentic_busy_session_ids() == {"busy-1"}


def test_busy_snapshot_empty_when_no_sessions(clean_registry):
    assert app._agentic_busy_session_ids() == set()


def test_busy_snapshot_never_raises_on_a_broken_session_object(clean_registry):
    class _Weird:
        pass  # no .turn_lock at all
    clean_registry["broken"] = _Weird()
    _register(clean_registry, "busy-1", busy=True)
    assert app._agentic_busy_session_ids() == {"busy-1"}


# --------------------------------------------------------------------------- #
# _do_update_check -- the busy/inflight branch
# --------------------------------------------------------------------------- #

def _stub_successful_pull(monkeypatch, before="aaaaaaa1111", after="bbbbbbb2222"):
    monkeypatch.setattr(app, "_is_git_repo", lambda: True)
    monkeypatch.setattr(app, "_origin_is_trusted", lambda: True)
    monkeypatch.setattr(app, "_hub_mode_is_off", lambda: False)

    def fake_git(*args, **kw):
        if args[:2] == ("status", "--porcelain"):
            return 0, "", ""
        if args == ("rev-parse", "HEAD"):
            # called twice: before the pull, then after
            fake_git.calls += 1
            return 0, (before if fake_git.calls == 1 else after), ""
        if args[0] == "pull":
            return 0, "Updating...", ""
        return 0, "", ""
    fake_git.calls = 0
    monkeypatch.setattr(app, "_git", fake_git)


def test_restarts_immediately_when_nothing_is_busy(isolated_config, clean_registry,
                                                     clean_runtime_active, monkeypatch):
    _stub_successful_pull(monkeypatch)
    calls = []
    monkeypatch.setattr(app, "_reexec_soon", lambda: calls.append("reexec_soon"))
    monkeypatch.setattr(app, "_reexec_when_idle", lambda busy: calls.append(("deferred", busy)))

    result = app._do_update_check()

    assert calls == ["reexec_soon"]
    assert "restarting" in result
    assert "deferred" not in result


def test_defers_when_an_agent_session_is_mid_turn(isolated_config, clean_registry,
                                                    clean_runtime_active, monkeypatch):
    _stub_successful_pull(monkeypatch)
    _register(clean_registry, "busy-sess", busy=True)
    calls = []
    monkeypatch.setattr(app, "_reexec_soon", lambda: calls.append("reexec_soon"))
    monkeypatch.setattr(app, "_reexec_when_idle", lambda busy: calls.append(("deferred", busy)))

    result = app._do_update_check()

    assert calls == [("deferred", {"busy-sess"})]
    assert "reexec_soon" not in calls
    assert "deferred" in result
    assert "1 task" in result


def test_defers_when_a_v1_request_is_in_flight(isolated_config, clean_registry,
                                                 clean_runtime_active, monkeypatch):
    _stub_successful_pull(monkeypatch)
    app._runtime_active[0] = 1
    calls = []
    monkeypatch.setattr(app, "_reexec_soon", lambda: calls.append("reexec_soon"))
    monkeypatch.setattr(app, "_reexec_when_idle", lambda busy: calls.append(("deferred", busy)))

    result = app._do_update_check()

    assert calls and calls[0][0] == "deferred"
    assert "deferred" in result


def test_no_new_commits_never_touches_restart_logic_at_all(isolated_config, clean_registry,
                                                             clean_runtime_active, monkeypatch):
    """before == after -- must hit the plain "up to date" branch, not the
    busy-check machinery at all (regression: the new code must not fire on
    every cycle, only on an actual pulled change)."""
    _stub_successful_pull(monkeypatch, before="same7890", after="same7890")
    calls = []
    monkeypatch.setattr(app, "_reexec_soon", lambda: calls.append("reexec_soon"))
    monkeypatch.setattr(app, "_reexec_when_idle", lambda busy: calls.append("deferred"))

    result = app._do_update_check()

    assert calls == []
    assert "up to date" in result


# --------------------------------------------------------------------------- #
# _reexec_when_idle
# --------------------------------------------------------------------------- #

def test_reexec_when_idle_waits_for_the_snapshotted_session_then_restarts(
        clean_registry, clean_runtime_active, monkeypatch):
    real_sleep = time.sleep  # app.time IS the same singleton module -- patching
    monkeypatch.setattr(app.time, "sleep", lambda s: real_sleep(0.01))  # it via itself would recurse
    sess = _FakeSession(busy=True)
    clean_registry["watched"] = sess
    done = threading.Event()
    monkeypatch.setattr(app, "_reexec_soon", lambda: done.set())

    app._reexec_when_idle({"watched"})
    assert not done.wait(timeout=0.2), "must not restart while the snapshotted session is busy"
    sess.turn_lock.release()
    assert done.wait(timeout=2.0), "must restart once the snapshotted session finishes"


def test_reexec_when_idle_ignores_sessions_that_start_after_the_snapshot(
        clean_registry, clean_runtime_active, monkeypatch):
    """The advisor-flagged livelock risk: waiting for "everything currently
    busy" (re-scanned live) instead of a fixed snapshot could wait forever on
    a hub in continuous use. An EMPTY snapshot must restart promptly even
    while a brand-new session becomes busy at the same moment."""
    real_sleep = time.sleep  # app.time IS the same singleton module -- patching
    monkeypatch.setattr(app.time, "sleep", lambda s: real_sleep(0.01))  # it via itself would recurse
    done = threading.Event()
    monkeypatch.setattr(app, "_reexec_soon", lambda: done.set())

    app._reexec_when_idle(set())
    _register(clean_registry, "started-after-snapshot", busy=True)

    assert done.wait(timeout=2.0), "an empty snapshot must not wait on newly-started work"
