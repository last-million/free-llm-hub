r"""The 5-hourly update must not restart on top of work that is still running.

The hub pulls and re-execs itself every few hours. A re-exec kills every
in-flight connection outright -- no HTTP response, no SSE event -- so it has
always waited for agent turns that hold their session's turn_lock, and for any
request still in flight on /v1/*.

A SWARM IS WORK THE TURN LOCK CANNOT SEE. Its workers hold the lock only while
their own turn runs; a run spends real time between phases -- a wave finishing,
the next being scheduled, a worker being spawned -- and in those gaps no lock
is held anywhere. Restarting in one of them kills the whole run: the workers
are children of this process and the walk thread goes with it, so a
twenty-minute five-phase job dies two phases in and comes back marked
"interrupted by a hub restart".

REQUESTED: "make sure people who installed it get the git pull and restart
without interrupting the work."
"""
import time

import pytest

import app as A
import swarm_windows as SW


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setenv(SW._STORE_ENV, str(tmp_path / "runs"))
    SW._RUNS.clear()
    yield
    for run in list(SW._RUNS.values()):
        run.stop_flag.set()
    SW._RUNS.clear()


def _run(state):
    run = SW._Run("g", ".", "opencode", [{"title": "a", "task": "t", "needs": []}])
    run.state = state
    SW._RUNS[run.id] = run
    return run


# --------------------------------------------------------------------------- #
# What counts as busy
# --------------------------------------------------------------------------- #

def test_a_running_swarm_is_busy():
    run = _run(SW.RUNNING)
    assert run.id in A._swarm_busy_run_ids()


def test_a_swarm_that_has_not_started_yet_is_busy_too():
    """It is about to spawn workers; restarting into that is the same loss."""
    run = _run(SW.PENDING)
    assert run.id in A._swarm_busy_run_ids()


@pytest.mark.parametrize("state", [SW.DONE, SW.FAILED, SW.STOPPED])
def test_a_finished_swarm_is_not(state):
    run = _run(state)
    assert run.id not in A._swarm_busy_run_ids()


def test_a_broken_swarm_module_never_blocks_an_update(monkeypatch):
    """Fail-open: an update that cannot tell whether anything is running must
    still apply, or a bug here freezes every install on old code."""
    monkeypatch.setattr(SW, "list_runs",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert A._swarm_busy_run_ids() == set()


# --------------------------------------------------------------------------- #
# The wait
# --------------------------------------------------------------------------- #

def test_the_restart_waits_while_the_run_is_going(monkeypatch):
    run = _run(SW.RUNNING)
    monkeypatch.setattr(A, "_runtime_active", [0])
    assert A._still_running(set(), {run.id}) == 1


def test_it_stops_waiting_once_the_run_ends(monkeypatch):
    run = _run(SW.RUNNING)
    monkeypatch.setattr(A, "_runtime_active", [0])
    assert A._still_running(set(), {run.id}) == 1
    run.state = SW.DONE
    assert A._still_running(set(), {run.id}) == 0


def test_a_run_that_started_after_the_check_is_not_waited_for():
    """Same rule the session snapshot already follows: an always-busy hub would
    otherwise defer forever and the pulled code would never apply."""
    _run(SW.RUNNING)                       # started now, not in the snapshot
    assert A._still_running(set(), set()) == 0


def test_an_inflight_request_still_counts(monkeypatch):
    monkeypatch.setattr(A, "_runtime_active", [1])
    assert A._still_running(set(), set()) == 1


def test_there_is_a_ceiling_on_how_long_it_waits():
    """A swarm that hangs must not defer the update forever."""
    assert 0 < A._DEFER_RESTART_MAX <= 6 * 3600
    assert A._DEFER_RESTART_MAX >= SW.AGENT_TIMEOUT


# --------------------------------------------------------------------------- #
# The wiring
# --------------------------------------------------------------------------- #

def test_the_update_check_snapshots_swarm_runs():
    src = open("app.py", encoding="utf-8").read()
    i = src.index("_auto_update_state[\"updating\"] = True")
    body = src[i:i + 1500]
    assert "runs = _swarm_busy_run_ids()" in body
    assert "_reexec_when_idle(busy, runs)" in body


def test_the_deferred_message_counts_them():
    src = open("app.py", encoding="utf-8").read()
    i = src.index("restart deferred: %d task(s) still running")
    assert "len(runs)" in src[i:i + 400]


def test_the_waiter_re_checks_rather_than_trusting_the_snapshot():
    src = open("app.py", encoding="utf-8").read()
    body = src[src.index("def _reexec_when_idle("):]
    body = body[:body.index("\ndef _auto_update_loop(")]
    assert "_still_running(busy_snapshot, busy_runs)" in body
    assert "deadline" in body
