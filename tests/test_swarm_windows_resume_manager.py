r"""A run resumed after a hub restart keeps its subscription manager.

REPORTED: the manager is a callable, so it is not persisted -- and
resume_interrupted() used to walk the remaining phases with run.manager None.
A run the person started WITH a manager checking the work finished every
remaining phase unverified ("not verified by the manager" in the report).
resume_interrupted(manager=) re-attaches the hub's current manager, and the
hub passes it whenever _manager_enabled(). Everything is a fake.
"""
import json
import os
import time

import pytest

import app as A
import swarm_windows as SW


PHASES = [{"title": "A", "task": "do a", "needs": []},
          {"title": "B", "task": "do b", "needs": []},
          {"title": "C", "task": "do c", "needs": [1, 2]}]


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv(SW._STORE_ENV, str(tmp_path / "swarm"))
    monkeypatch.setattr(SW, "SPAWN_STAGGER", 0)
    monkeypatch.setattr(SW, "RETRY_BACKOFF", 0)
    with SW._LOCK:
        SW._RUNS.clear()
    yield
    with SW._LOCK:
        for run in SW._RUNS.values():
            run.stop_flag.set()
        SW._RUNS.clear()


def _spawn(cli_id, project_dir):
    return "sess-" + os.urandom(3).hex()


def _turn(session_id, prompt):
    yield {"event": "message", "text": "redone " + prompt.splitlines()[0]}


def _wait(rid, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        st = SW.status(rid)
        if st and st["state"] not in (SW.PENDING, SW.RUNNING):
            return st
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def _interrupted_run(managed=True):
    run = SW._Run("build it", ".", "opencode", PHASES,
                  manager=_Manager() if managed else None)
    run.agents[0].state = SW.DONE
    run.agents[0].summary = "a was done before"
    run.agents[1].state = SW.RUNNING
    run.agents[2].state = SW.PENDING
    run.state = SW.RUNNING
    if managed:
        run.manager_tokens, run.manager_calls = 500, 3     # spent before the restart
    SW._persist(run)
    with SW._LOCK:
        SW._RUNS.clear()
    assert SW.load() == 1
    back = SW.get(run.id)
    assert back.manager is None, "a callable never survives the file"
    return back


class _Manager:
    def __init__(self):
        self.calls = []

    def __call__(self, system, user, purpose, max_tokens):
        self.calls.append(purpose)
        return (json.dumps({"ok": True}), 40) if purpose == "verify" else ("", 0)


def test_resume_reattaches_the_manager_and_verifies_the_remaining_phases():
    run = _interrupted_run()
    mgr = _Manager()
    assert SW.resume_interrupted(_spawn, _turn, manager=mgr,
                                 modes=("coding",)) == [run.id]
    st = _wait(run.id)
    assert st["state"] == SW.DONE
    run = SW.get(run.id)
    assert run.manager is mgr and run.modes == ("coding",)
    # Only the two phases that ran again were checked -- A was done before.
    assert mgr.calls.count("verify") == 2
    # The manager agreed with their summaries: reviewed (verified needs an
    # observed passing test run).
    assert run.agents[1].reviewed is True and run.agents[2].reviewed is True
    # The cost keeps accumulating on the SAME run, on top of what was restored.
    assert st["manager_tokens"] == 500 + 2 * 40
    assert st["manager_calls"] == 3 + 2


def test_resume_without_a_manager_is_unchanged():
    run = _interrupted_run()
    assert SW.resume_interrupted(_spawn, _turn) == [run.id]
    st = _wait(run.id)
    assert st["state"] == SW.DONE
    assert SW.get(run.id).manager is None
    assert st["manager_tokens"] == 500 and st["manager_calls"] == 3


def test_the_hub_passes_its_manager_on_resume_only_when_enabled(monkeypatch):
    seen = []

    def fake_resume(spawn, run_turn, **kw):
        seen.append(kw)
        return []
    monkeypatch.setattr(A.swarm_windows, "resume_interrupted", fake_resume)
    monkeypatch.setattr(A, "_manager_enabled", lambda: True)
    A._resume_interrupted_swarms()
    monkeypatch.setattr(A, "_manager_enabled", lambda: False)
    A._resume_interrupted_swarms()
    assert seen[0]["manager"] is A._swarm_windows_manager
    assert "manager" not in seen[1]
    assert isinstance(seen[0]["modes"], tuple)


def test_resume_never_attaches_a_manager_to_a_run_started_without_one():
    run = _interrupted_run(managed=False)
    mgr = _Manager()
    assert SW.resume_interrupted(_spawn, _turn, manager=mgr) == [run.id]
    st = _wait(run.id)
    assert st["state"] == SW.DONE
    assert SW.get(run.id).manager is None and mgr.calls == []
    assert st["manager_tokens"] == 0 and st["managed"] is False


def test_a_row_from_before_managed_existed_reads_it_from_the_cost():
    row = SW._Run("g", ".", "opencode", PHASES).row()
    row.pop("managed")
    assert SW._Run.from_row(dict(row, manager_calls=2)).managed is True
    assert SW._Run.from_row(dict(row, manager_calls=0)).managed is False
