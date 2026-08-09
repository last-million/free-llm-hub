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

def _stub_successful_pull(monkeypatch, before="aaaaaaa1111", after="bbbbbbb2222",
                          deps_ok=True):
    monkeypatch.setattr(app, "_is_git_repo", lambda: True)
    monkeypatch.setattr(app, "_origin_is_trusted", lambda: True)
    monkeypatch.setattr(app, "_hub_mode_is_off", lambda: False)
    # Explicit, not left to the real requirements.txt/.venv/.deps-stamp on
    # whatever machine runs this test -- that would make these tests
    # nondeterministic (and, on a stale venv, actually spawn a real pip
    # install per test run).
    monkeypatch.setattr(app, "_sync_deps_after_pull", lambda: deps_ok)

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


# --------------------------------------------------------------------------- #
# _sync_deps_after_pull -- os.execv keeps the SAME already-imported
# interpreter/site-packages, so nothing else here ever re-ran pip. A commit
# that adds a dependency would pull clean, then crash the next import.
# --------------------------------------------------------------------------- #

@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("flask==3.0.0\n", encoding="utf-8")
    monkeypatch.setattr(app, "_REPO_DIR", str(tmp_path))
    return tmp_path


def test_deps_sync_is_a_noop_when_the_stamp_already_matches(fake_repo, monkeypatch):
    import hashlib
    h = hashlib.sha256((fake_repo / "requirements.txt").read_bytes()).hexdigest()
    (fake_repo / ".venv").mkdir()
    (fake_repo / ".venv" / ".deps-stamp").write_text(h, encoding="utf-8")
    calls = []
    monkeypatch.setattr(app.subprocess, "run", lambda *a, **k: calls.append(a) or None)

    assert app._sync_deps_after_pull() is True
    assert calls == [], "must not spawn pip when the hash already matches"


def test_deps_sync_installs_and_updates_the_stamp_when_requirements_changed(
        fake_repo, monkeypatch):
    class _Ok:
        returncode = 0
        stderr = ""
    calls = []
    monkeypatch.setattr(app.subprocess, "run", lambda *a, **k: calls.append(a) or _Ok())

    assert app._sync_deps_after_pull() is True
    assert len(calls) == 1
    cmd = calls[0][0]
    assert cmd[1:4] == ["-m", "pip", "install"]
    stamp = fake_repo / ".venv" / ".deps-stamp"
    assert stamp.exists()
    import hashlib
    assert stamp.read_text(encoding="utf-8") == hashlib.sha256(
        (fake_repo / "requirements.txt").read_bytes()).hexdigest()


def test_deps_sync_reports_failure_and_leaves_the_stamp_untouched(fake_repo, monkeypatch):
    class _Fail:
        returncode = 1
        stderr = "no matching distribution found"
    monkeypatch.setattr(app.subprocess, "run", lambda *a, **k: _Fail())

    assert app._sync_deps_after_pull() is False
    assert not (fake_repo / ".venv" / ".deps-stamp").exists(), (
        "a failed install must not be recorded as if it succeeded -- the next "
        "cycle needs to retry, not treat this requirements.txt as already handled")


def test_deps_sync_never_raises_when_requirements_txt_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "_REPO_DIR", str(tmp_path))  # no requirements.txt written
    assert app._sync_deps_after_pull() is False


# --------------------------------------------------------------------------- #
# _do_update_check wired to deps sync
# --------------------------------------------------------------------------- #

def test_update_check_defers_restart_when_dependency_install_fails(
        isolated_config, clean_registry, clean_runtime_active, monkeypatch):
    _stub_successful_pull(monkeypatch, deps_ok=False)
    calls = []
    monkeypatch.setattr(app, "_reexec_soon", lambda: calls.append("reexec_soon"))
    monkeypatch.setattr(app, "_reexec_when_idle", lambda busy: calls.append(("deferred", busy)))

    result = app._do_update_check()

    assert calls == [], "must never re-exec into an environment missing a new dependency"
    assert "dependency install failed" in result
    assert "retrying" in result


def test_deps_sync_is_retried_every_cycle_even_once_head_stops_moving(
        isolated_config, clean_registry, clean_runtime_active, monkeypatch):
    """git pull is a no-op the moment it has already succeeded once -- if a
    failed dependency install only got checked inside the before!=after
    branch, a hub stuck on stale deps would stop retrying forever the very
    next cycle, since HEAD would no longer appear to move."""
    monkeypatch.setattr(app, "_is_git_repo", lambda: True)
    monkeypatch.setattr(app, "_origin_is_trusted", lambda: True)
    monkeypatch.setattr(app, "_hub_mode_is_off", lambda: False)
    monkeypatch.setattr(app, "_git", lambda *a, **k: (
        (0, "", "") if a[:2] == ("status", "--porcelain") else
        (0, "same-sha-both-times", "") if a == ("rev-parse", "HEAD") else
        (0, "Already up to date.", "")))
    sync_calls = []
    monkeypatch.setattr(app, "_sync_deps_after_pull", lambda: sync_calls.append(1) or False)

    app._do_update_check()
    app._do_update_check()

    assert len(sync_calls) == 2, "must re-check every cycle, not just when HEAD moves"
