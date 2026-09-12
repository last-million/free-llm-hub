r"""A swarm run the hub died under is finished by the next hub, not filed as failed.

MEASURED 2026-09-12, twice in one night: a four-phase build killed at 00:49 and
another at 02:27 by hub restarts, each the person's real work, each shown as
"interrupted by a hub restart" on every unfinished phase, no review, and -- for
a run that was a conversation's turn -- no reply ever. Asked for in as many
words: "work should always be finished till the end".

Phases that were done keep their summaries; the ones that were running or
waiting run again in fresh sessions in the same folder; then the review; then
the conversation gets its reply, exactly as if nothing had happened.
"""
import os
import threading
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
    with SW._LOCK:
        SW._RUNS.clear()
    monkeypatch.setattr(SW, "SPAWN_STAGGER", 0)
    yield
    with SW._LOCK:
        SW._RUNS.clear()


def _spawn(cli_id, project_dir):
    return "sess-" + os.urandom(3).hex()


def _turn(text):
    def run_turn(session_id, prompt):
        yield {"event": "message", "text": text}
    return run_turn


def _wait(rid, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        st = SW.status(rid)
        if st and st["state"] not in (SW.PENDING, SW.RUNNING):
            return st
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def _interrupted_run(owner=None):
    """A run as the next hub finds it: A done, B and C never finished."""
    run = SW._Run("build it", ".", "opencode", PHASES, owner=owner)
    run.agents[0].state = SW.DONE
    run.agents[0].summary = "a was done before"
    run.agents[1].state = SW.RUNNING
    run.agents[2].state = SW.PENDING
    run.state = SW.RUNNING
    SW._persist(run)
    with SW._LOCK:
        SW._RUNS.clear()
    assert SW.load() == 1
    return SW.get(run.id)


# --------------------------------------------------------------------------- #
# What load() says, and what resume does about it
# --------------------------------------------------------------------------- #

def test_load_still_files_it_honestly_first():
    run = _interrupted_run()
    assert run.state == SW.FAILED and run.interrupted
    assert run.agents[1].error == SW.INTERRUPTED_ERROR
    assert run.agents[2].error == SW.INTERRUPTED_ERROR
    assert run.agents[0].state == SW.DONE, "what finished stays finished"


def test_the_unfinished_phases_run_again_and_the_done_one_does_not():
    run = _interrupted_run()
    ran = []

    def run_turn(session_id, prompt):
        ran.append(prompt.splitlines()[0])
        yield {"event": "message", "text": "redone"}
    resumed = SW.resume_interrupted(_spawn, run_turn)
    assert resumed == [run.id]
    st = _wait(run.id)
    assert st["state"] == SW.DONE
    assert sorted(ran) == ["do b", "do c"], "A was not run twice"
    assert [a["summary"] for a in st["agents"]] == ["a was done before", "redone", "redone"]
    assert not st["restored"]


def test_on_done_fires_for_the_resumed_run():
    run = _interrupted_run(owner="conv-1")
    seen = []
    SW.resume_interrupted(_spawn, _turn("ok"), on_done=lambda r: seen.append((r.id, r.owner)))
    _wait(run.id)
    for _ in range(50):
        if seen:
            break
        time.sleep(0.05)
    assert seen == [(run.id, "conv-1")]


def test_a_run_from_long_ago_is_left_alone():
    run = _interrupted_run()
    run.created_at = time.time() - SW.RESUME_MAX_AGE - 60
    assert SW.resume_interrupted(_spawn, _turn("ok")) == []
    assert run.state == SW.FAILED


def test_a_run_that_really_failed_is_not_retried():
    """Only phases the RESTART interrupted. A phase that failed on its own
    merits (a model that refused, a CLI that crashed) stays failed."""
    run = SW._Run("g", ".", "opencode", PHASES)
    for a in run.agents:
        a.state = SW.FAILED
        a.error = "the model refused"
    run.state = SW.FAILED
    SW._persist(run)
    with SW._LOCK:
        SW._RUNS.clear()
    SW.load()
    assert SW.resume_interrupted(_spawn, _turn("ok")) == []


def test_resuming_twice_does_not_run_it_twice():
    run = _interrupted_run()
    assert SW.resume_interrupted(_spawn, _turn("ok")) == [run.id]
    assert SW.resume_interrupted(_spawn, _turn("ok")) == []
    _wait(run.id)


def test_the_owner_survives_the_file():
    run = SW._Run("g", ".", "opencode", PHASES, owner="conv-9")
    assert SW._Run.from_row(run.row()).owner == "conv-9"
    assert SW._Run.from_row(SW._Run("g", ".", "opencode", PHASES).row()).owner is None


def test_a_wave_skips_phases_already_done():
    run = SW._Run("g", ".", "opencode", PHASES)
    run.agents[0].state = SW.DONE
    ran = []

    def run_turn(session_id, prompt):
        ran.append(prompt.splitlines()[0])
        yield {"event": "message", "text": "x"}
    SW._run_wave(run, [1, 2], _spawn, run_turn)
    assert ran == ["do b"]


# --------------------------------------------------------------------------- #
# The hub side: a conversation's run comes back to its conversation
# --------------------------------------------------------------------------- #

def test_a_run_started_as_a_turn_carries_its_conversation():
    src = open("app.py", encoding="utf-8").read()
    body = src[src.index("def _multi_turn_events("):]
    body = body[:body.index("\ndef _multi_record(")]
    assert "owner=session_id" in body
    assert "on_done=_multi_owner_record" in body


def test_the_recorder_reads_the_conversation_off_the_run(monkeypatch):
    recorded = []
    monkeypatch.setattr(A.agentic_history, "record_turn", lambda *a, **k: recorded.append(a))
    monkeypatch.setattr(A.memory, "remember_recent", lambda *a, **k: None)
    monkeypatch.setattr(A.swarm_windows, "format_result", lambda rid: "the report")
    monkeypatch.setattr(A.agentic_chat, "get_session", lambda sid: {})

    class R:
        id = "swarm-x"; state = SW.DONE; owner = "conv-2"; cli_id = "opencode"; project_dir = "C:/p"
    A._multi_owner_record(R())
    assert recorded and recorded[0][:4] == ("conv-2", "opencode", "C:/p", "agent")
    assert recorded[0][4] == "the report"

    class NoOwner(R):
        owner = None
    recorded.clear()
    A._multi_owner_record(NoOwner())
    assert recorded == [], "a run from the Swarm tab answers nobody"


def test_at_boot_the_hub_finishes_them_and_the_page_can_follow(monkeypatch):
    run = _interrupted_run(owner="conv-3")
    monkeypatch.setattr(A, "_swarm_windows_spawn", _spawn)
    monkeypatch.setattr(A, "_swarm_windows_turn", lambda sid, text: _turn("done again")(sid, text))
    monkeypatch.setattr(A, "_swarm_windows_configure", lambda sid, mode: True)
    recorded = []
    monkeypatch.setattr(A.agentic_history, "record_turn", lambda *a, **k: recorded.append(a))
    monkeypatch.setattr(A.memory, "remember_recent", lambda *a, **k: None)
    monkeypatch.setattr(A.agentic_chat, "get_session", lambda sid: {})
    monkeypatch.setattr(A, "_MULTI_POLL", 0.02)
    A._MULTI_RUNS.clear()
    with A.agentic_chat._LIVE_LOCK:
        A.agentic_chat._LIVE.clear()

    assert A._resume_interrupted_swarms() == [run.id]
    assert A._MULTI_RUNS.get("conv-3") == run.id, "registered as that conversation's run"
    st = _wait(run.id)
    assert st["state"] == SW.DONE
    for _ in range(100):
        if recorded and "conv-3" not in A._MULTI_RUNS:
            break
        time.sleep(0.05)
    assert recorded[0][0] == "conv-3" and "3/3 phases done" in recorded[0][4]
    # the page's reload path: the live buffer carried the resumed run's events
    followed = list(A.agentic_chat.follow_turn("conv-3"))
    kinds = [e["event"] for e in followed]
    assert kinds[0] == "notice" and kinds[-1] == "done"
    A._MULTI_RUNS.clear()


def test_it_is_wired_into_boot():
    src = open("app.py", encoding="utf-8").read()
    i = src.index("back = swarm_windows.load()")
    assert "_resume_interrupted_swarms" in src[i:i + 800]
