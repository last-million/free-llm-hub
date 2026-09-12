r"""Multi sessions runs where the message was sent, not in another tab.

REPORTED: "when I click Multi sessions in /agent it shows a new window and
does not stay in the same place, the conversation -- wtf".

It was a button that switched to the Swarm tab: its own goal box, its own list
of runs, and a result the conversation never saw. Choosing it meant leaving
the conversation, retyping the message and reading the answer somewhere else.

Now it is the fourth quality tier. In "multi", the message IS the goal of a
swarm_windows run -- several real agent sessions, each its own CLI process and
context window, working phases of that message in the project folder -- and
the turn stays where it was sent: progress arrives as the same events an
ordinary turn emits, the combined report is the reply, and the reply is
recorded in this conversation's history from the run's own thread.
"""
import threading
import time

import pytest

import agentic_chat as AC
import agentic_history as AH
import app as A
import swarm_windows as SW


SRC = open("templates/index.html", encoding="utf-8").read()
APP = open("app.py", encoding="utf-8").read()


# --------------------------------------------------------------------------- #
# It is a tier, and every gate takes it
# --------------------------------------------------------------------------- #

def test_there_are_four_tiers_and_one_list_of_them():
    assert AH.QUALITIES == ("normal", "max", "swarm", "multi")
    assert AC.QUALITIES is AH.QUALITIES


def test_no_gate_keeps_its_own_copy_of_the_list():
    """Three copies of the tuple is how the fourth tier was silently rejected
    by one of them and came back as "normal" after a restart."""
    for src in (APP, open("agentic_chat.py", encoding="utf-8").read(),
                open("agentic_history.py", encoding="utf-8").read()):
        assert '("normal", "max", "swarm")' not in src


def test_a_session_can_be_started_in_it(tmp_path, monkeypatch):
    sess = AC._Session("opencode", str(tmp_path), quality="multi")
    assert sess.quality == "multi"


def test_the_conversation_store_keeps_it(tmp_path, monkeypatch):
    monkeypatch.setattr(AH, "_root", lambda: str(tmp_path))
    AH.record_turn("s-multi", "opencode", str(tmp_path), "user", "build it")
    assert AH.set_quality("s-multi", "multi") == "multi"
    assert AH.get_conversation("s-multi")["quality"] == "multi"


def test_a_parent_turn_in_it_is_routed_at_the_top_tier():
    """The parent rarely runs a turn itself in this tier; when it does, it is
    not a cheap one."""
    assert AC._hub_model_for("multi") == "best"
    assert AC._hub_model_for("multi", "coding") == "coding"


# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #

def test_it_is_a_radio_in_the_quality_row():
    row = SRC[SRC.index('id="agent-quality"'):]
    row = row[:row.index("</div>") + 6]
    assert 'name="agent-quality" value="multi"' in row


def test_nothing_in_the_row_switches_tabs_any_more():
    assert 'id="agent-quality-multi"' not in SRC
    assert "multiBtn" not in SRC
    assert ".q-go" not in SRC, "the dashed 'navigates' style went with the button"


def test_the_page_says_what_picking_it_does():
    i = SRC.index('value="multi"')
    around = SRC[i - 700:i + 100]
    assert "REAL agent sessions" in around
    assert "this conversation" in around


def test_the_swarm_tab_is_still_there_for_stopping_and_logs():
    assert "id=\"sw-runs\"" in SRC or "sw-runs" in SRC
    assert "viewSwarmBtn" in SRC


# --------------------------------------------------------------------------- #
# The turn itself
# --------------------------------------------------------------------------- #

class _FakeRun:
    def __init__(self, run_id, state, agents, error=None, owner="s1",
                 cli_id="codex", project_dir="C:/proj"):
        self.id = run_id
        self.state = state
        self.agents = agents
        self.error = error
        self.owner = owner
        self.cli_id = cli_id
        self.project_dir = project_dir


@pytest.fixture
def swarm(monkeypatch):
    """A scripted swarm_windows: start() records the call, status() plays a
    sequence of snapshots, and on_done fires from a thread like the real one."""
    calls = {"start": [], "stop": [], "frames": [], "on_done": None}

    def start(goal, project_dir, cli_id, spawn, run_turn, phases=None, planner=None,
              on_done=None, configure=None, modes=(), review=True, owner=None):
        calls["start"].append({"goal": goal, "project_dir": project_dir,
                               "cli": cli_id, "modes": tuple(modes), "owner": owner})
        calls["on_done"] = on_done
        return "swarm-test"

    def status(run_id, with_events=False):
        if not calls["frames"]:
            return None
        if len(calls["frames"]) > 1:
            return calls["frames"].pop(0)
        return calls["frames"][0]

    def stop(run_id):
        calls["stop"].append(run_id)
        return True

    monkeypatch.setattr(A.swarm_windows, "start", start)
    monkeypatch.setattr(A.swarm_windows, "status", status)
    monkeypatch.setattr(A.swarm_windows, "stop", stop)
    monkeypatch.setattr(A.swarm_windows, "format_result",
                        lambda rid: "Swarm run %s - done (2/2 phases done)" % rid)
    monkeypatch.setattr(A, "_mode_keys", lambda: ["coding", "fast"])
    monkeypatch.setattr(A, "_MULTI_POLL", 0.0)
    monkeypatch.setattr(A.memory, "note_turn", lambda sid: 2)
    monkeypatch.setattr(A.memory, "remember_recent", lambda *a, **k: None)
    monkeypatch.setattr(A.memory, "remember_fact", lambda *a, **k: None)
    A._MULTI_RUNS.clear()
    yield calls
    A._MULTI_RUNS.clear()


def _frame(state, agents, done=0, error=None):
    return {"run_id": "swarm-test", "state": state, "agents": agents,
            "total": len(agents), "done": done, "error": error}


def _agent(index, title, state, summary="", error=None, mode=None):
    return {"index": index, "title": title, "state": state, "summary": summary,
            "error": error, "mode": mode}


SESS = {"session_id": "s1", "cli": "codex", "project_dir": "C:/proj", "quality": "multi"}


def test_the_message_is_the_goal_in_this_folder_under_this_cli(swarm):
    swarm["frames"] = [_frame(SW.DONE, [_agent(1, "Build", SW.DONE, "built")], done=1)]
    list(A._multi_turn_events("s1", SESS, "make a landing page"))
    # "fast" is offered to the planner nowhere: a worker's output is files.
    assert swarm["start"] == [{"goal": "make a landing page", "project_dir": "C:/proj",
                               "cli": "codex", "modes": ("coding",), "owner": "s1"}]


def test_progress_arrives_as_ordinary_turn_events(swarm):
    swarm["frames"] = [
        _frame(SW.RUNNING, [_agent(1, "Build", SW.RUNNING, mode="coding"),
                            _agent(2, "Review and finish", SW.PENDING)]),
        _frame(SW.RUNNING, [_agent(1, "Build", SW.DONE, "Wrote index.html\nand css"),
                            _agent(2, "Review and finish", SW.RUNNING)], done=1),
        _frame(SW.DONE, [_agent(1, "Build", SW.DONE, "Wrote index.html"),
                         _agent(2, "Review and finish", SW.DONE, "Looks good")], done=2),
    ]
    evs = list(A._multi_turn_events("s1", SESS, "make it"))
    kinds = [e["event"] for e in evs]
    assert kinds[0] == "notice" and "2 phases" in evs[0]["text"]
    assert kinds.count("tool") == 2, "each phase starting is a tool line"
    assert "Phase 1/2 Build (coding)" in evs[1]["text"]
    outs = [e["text"] for e in evs if e["event"] == "output"]
    assert any(o.startswith("Phase 1/2 Build -- done: Wrote index.html") for o in outs)
    assert kinds[-2:] == ["message", "done"]
    assert "2/2 phases done" in evs[-1]["text"]


def test_the_reply_is_recorded_from_the_runs_own_thread(swarm, monkeypatch):
    """The reader of the stream may be gone by the time the run ends; the
    conversation must still get its reply."""
    recorded = []
    monkeypatch.setattr(A.agentic_history, "record_turn",
                        lambda *a, **k: recorded.append((a, k)))
    monkeypatch.setattr(A.agentic_chat, "get_session",
                        lambda sid: {"native_session_id": "thread-9"})
    swarm["frames"] = [_frame(SW.DONE, [_agent(1, "Build", SW.DONE, "ok")], done=1)]
    list(A._multi_turn_events("s1", SESS, "go"))
    assert not recorded, "the generator itself does not record"
    swarm["on_done"](_FakeRun("swarm-test", SW.DONE, []))
    (args, kw), = recorded
    assert args[:4] == ("s1", "codex", "C:/proj", "agent")
    assert "2/2 phases done" in args[4]
    assert kw["native_session_id"] == "thread-9"
    assert "s1" not in A._MULTI_RUNS


def test_a_second_message_while_one_runs_is_refused_not_doubled(swarm):
    swarm["frames"] = [_frame(SW.RUNNING, [_agent(1, "Build", SW.RUNNING)])]
    A._MULTI_RUNS["s1"] = "swarm-test"
    evs = list(A._multi_turn_events("s1", SESS, "and also this"))
    assert evs == [{"event": "error", "status": 409,
                    "detail": "A multi-session run is already working on this "
                              "conversation. Wait for it, or press Stop."}]
    assert swarm["start"] == []


def test_stop_reaches_the_run(swarm):
    swarm["frames"] = [_frame(SW.RUNNING, [_agent(1, "Build", SW.RUNNING)])]
    A._MULTI_RUNS["s1"] = "swarm-test"
    body = APP[APP.index("def api_agent_stop_session("):]
    body = body[:body.index("\n@app.route")]
    assert "_MULTI_RUNS" in body and "swarm_windows.stop(rid)" in body


def test_a_stopped_run_is_a_stopped_turn_with_what_it_had(swarm):
    swarm["frames"] = [_frame(SW.STOPPED, [_agent(1, "Build", SW.DONE, "half")], done=1)]
    evs = list(A._multi_turn_events("s1", SESS, "go"))
    kinds = [e["event"] for e in evs]
    assert "stopped" in kinds
    assert evs[-1]["event"] == "message"
    assert evs[-1]["text"].startswith(A._MULTI_STOPPED_NOTE)


def test_a_run_where_nothing_finished_is_an_error_not_an_empty_reply(swarm):
    swarm["frames"] = [_frame(SW.FAILED, [_agent(1, "Build", SW.FAILED, error="boom")],
                              error="every phase failed")]
    evs = list(A._multi_turn_events("s1", SESS, "go"))
    assert evs[-1]["event"] == "error" and evs[-1]["status"] == 502


def test_a_run_that_could_not_be_planned_says_so(swarm, monkeypatch):
    def start(*a, **k):
        raise SW.SwarmWindowsError("could not turn that into phases")
    monkeypatch.setattr(A.swarm_windows, "start", start)
    evs = list(A._multi_turn_events("s1", SESS, "go"))
    assert evs == [{"event": "error", "status": 400,
                    "detail": "could not turn that into phases"}]


def test_the_session_reads_as_running_while_the_run_is(swarm):
    swarm["frames"] = [_frame(SW.RUNNING, [_agent(1, "Build", SW.RUNNING)])]
    A._MULTI_RUNS["s1"] = "swarm-test"
    assert A._multi_run_for("s1")
    swarm["frames"] = [_frame(SW.DONE, [_agent(1, "Build", SW.DONE)], done=1)]
    assert A._multi_run_for("s1") is None
    assert "s1" not in A._MULTI_RUNS, "a finished run is forgotten on sight"


def test_the_blocking_shape_answers_like_send_message(swarm):
    swarm["frames"] = [_frame(SW.DONE, [_agent(1, "Build", SW.DONE, "ok")], done=1)]
    status, text, detail = A._multi_turn_blocking("s1", SESS, "go")
    assert status == 200 and "phases done" in text and detail is None


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #

def test_both_message_routes_branch_on_the_tier():
    stream = APP[APP.index("def api_agent_send_message_stream("):]
    stream = stream[:stream.index("\n@app.route")]
    assert 'sess_info.get("quality") == "multi"' in stream
    assert "_multi_turn_events(session_id, sess_info, text)" in stream
    plain = APP[APP.index("def api_agent_send_message("):]
    plain = plain[:plain.index("\n@app.route")]
    assert "_multi_turn_blocking(session_id, sess_info" in plain


def test_the_plain_route_does_not_record_the_reply_twice():
    plain = APP[APP.index("def api_agent_send_message("):]
    plain = plain[:plain.index("\n@app.route")]
    i = plain.index("_multi_turn_blocking(")
    j = plain.index("agentic_chat.send_message(session_id")
    assert i < j and "return jsonify" in plain[i:j], \
        "the multi branch returns before the ordinary recording"


def test_the_session_row_says_running_during_a_run():
    body = APP[APP.index("def api_agent_get_session("):]
    body = body[:body.index("\n@app.route")]
    assert "_multi_run_for(session_id)" in body
