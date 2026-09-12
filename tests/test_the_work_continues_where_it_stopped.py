r"""The hub keeps the agent's task list, and a stopped turn is continued, not redone.

REQUESTED: "make sure he always creates a todo list and tracks it and updates
it while working, and if I stop the agent he should keep what the LLM was
working on, and when I come back and ask it to continue he should continue
from where he was working exactly -- also inside CLIs".

The agent was already TOLD to keep a checklist (PROGRESS.md); nothing read it
back. Now:
  * the hub keeps its own copy, refreshed after every turn from the project's
    PROGRESS.md / TODO.md and from the checklist in the reply, and shows it on
    the Build page;
  * a turn that is stopped or dies files what it was doing -- the request, its
    last tool calls, the text it had written -- and the next turn is handed
    that first, ahead of every other memory, with "continue from exactly
    there";
  * a CLI driven straight against the hub gets the same file instruction in
    its opening plan block, so it can be resumed the same way.
"""
import collections
import os

import pytest

import agentic_chat as AC
import app as A
import craft
import memory as M


APP = open("app.py", encoding="utf-8").read()
SRC = open("templates/index.html", encoding="utf-8").read()


@pytest.fixture(autouse=True)
def _mem(tmp_path, monkeypatch):
    monkeypatch.setenv(M._ROOT_ENV, str(tmp_path / "mem"))
    yield


# --------------------------------------------------------------------------- #
# The list
# --------------------------------------------------------------------------- #

def test_a_markdown_checklist_is_read_and_a_plain_list_is_not():
    items = M.parse_checklist("Plan:\n- [ ] build the page\n* [x] set up repo\n"
                              "1. [~] write tests\n- just a bullet\n2. [ ] deploy")
    assert [(t["text"], t["done"], t["doing"]) for t in items] == [
        ("build the page", False, False), ("set up repo", True, False),
        ("write tests", False, True), ("deploy", False, False)]


def test_a_reply_with_a_real_list_replaces_the_list():
    assert M.update_tasks_from_text("s1", "- [x] a\n- [ ] b") is True
    assert [t["text"] for t in M.tasks("s1")] == ["a", "b"]


def test_one_box_in_a_sentence_is_not_a_plan():
    M.set_tasks("s1", M.parse_checklist("- [ ] a\n- [ ] b"))
    assert M.update_tasks_from_text("s1", "Done. - [x] a") is False
    assert len(M.tasks("s1")) == 2


def test_the_projects_own_file_wins_over_the_reply(tmp_path):
    (tmp_path / "PROGRESS.md").write_text("# plan\n- [x] one\n- [~] two\n- [ ] three\n",
                                          encoding="utf-8")
    assert M.update_tasks("s1", "- [ ] something else\n- [ ] entirely", str(tmp_path)) == "file"
    got = M.tasks("s1")
    assert [t["text"] for t in got] == ["one", "two", "three"]
    assert got[1]["doing"] is True
    assert M.get("s1")["tasks_source"] == "PROGRESS.md"


def test_no_file_means_the_reply(tmp_path):
    assert M.update_tasks("s1", "- [ ] x\n- [ ] y", str(tmp_path)) == "reply"


def test_a_huge_file_is_left_alone(tmp_path):
    (tmp_path / "TODO.md").write_text("- [ ] x\n" * 20000, encoding="utf-8")
    assert M.update_tasks_from_project("s1", str(tmp_path)) is False


def test_the_list_is_capped():
    items = M.parse_checklist("\n".join("- [ ] item %d" % i for i in range(100)))
    assert len(items) == M.MAX_TASKS


# --------------------------------------------------------------------------- #
# Where it stopped
# --------------------------------------------------------------------------- #

def test_a_stopped_turn_is_filed_and_handed_to_the_next_one():
    M.note_turn("s1")
    M.note_interrupted("s1", request="build the landing page",
                       doing=["bash npm init", "write index.html", "write style.css"],
                       partial="Created index.html, now adding the hero section", why="stopped")
    block = M.context_block("s1", 2000, query="continue")
    assert block.startswith("YOUR PREVIOUS TURN WAS STOPPED BY THE USER")
    assert "build the landing page" in block
    assert "write style.css" in block
    assert "now adding the hero section" in block
    assert "do not start over" in block


def test_it_leads_even_when_the_budget_is_tight():
    for i in range(10):
        M.remember_fact("s1", "must keep decision %d in mind for ever" % i)
    M.note_interrupted("s1", request="r", doing=["a"], partial="p")
    block = M.context_block("s1", 400, query="anything")
    assert block.startswith("YOUR PREVIOUS TURN WAS")


def test_a_finished_turn_clears_it():
    M.note_interrupted("s1", request="r", doing=["a"], partial="p")
    assert M.clear_interrupted("s1") is True
    assert M.interrupted("s1") is None
    assert "PREVIOUS TURN" not in M.context_block("s1", 2000)


def test_a_turn_that_died_says_so_rather_than_blaming_the_user():
    M.note_interrupted("s1", request="r", doing=[], partial="", why="database is locked")
    assert "CUT SHORT (database is locked)" in M.resume_block("s1")


def test_the_list_rides_along_with_where_it_stopped():
    M.set_tasks("s1", M.parse_checklist("- [x] a\n- [ ] b"), "PROGRESS.md")
    block = M.resume_block("s1")
    assert "Task list (1/2 done, from PROGRESS.md):" in block
    assert "- [x] a" in block and "- [ ] b" in block


def test_nothing_to_say_is_an_empty_string():
    assert M.resume_block("fresh") == ""
    assert M.context_block("fresh", 2000) == ""


# --------------------------------------------------------------------------- #
# The turn files it
# --------------------------------------------------------------------------- #

def test_the_durable_turn_files_the_stopping_place():
    src = open("agentic_chat.py", encoding="utf-8").read()
    body = src[src.index("def send_message_stream_durable("):]
    body = body[:body.index("\ndef ", 10)]
    assert 'if kind == "tool" and ev.get("text"):' in body
    assert "memory.note_interrupted(session_id, request=text, doing=list(doing)" in body
    assert "memory.clear_interrupted(session_id)" in body
    assert "memory.update_tasks(session_id, final_reply," in body


def test_the_multi_turn_files_its_phases():
    body = APP[APP.index("def _multi_record("):]
    body = body[:body.index("\ndef ")]
    assert "memory.update_tasks(session_id, report, run.project_dir)" in body
    assert "memory.note_interrupted(session_id, request=run.goal, doing=doing" in body


# --------------------------------------------------------------------------- #
# The CLIs are told to keep the file
# --------------------------------------------------------------------------- #

def test_the_agent_page_prompt_names_the_file_and_the_format():
    assert "PROGRESS.md, as a markdown checklist (- [ ] todo, - [x] done, - [~] in progress)" \
        in AC._PLANNING_SNIPPET


def test_a_cli_through_the_gateway_gets_the_same():
    """PLAN_PHASES ships to every tool-carrying opening turn from any CLI."""
    assert "PROGRESS.md" in craft.PLAN_PHASES
    assert "read it first and continue from it" in craft.PLAN_PHASES
    assert "- [~] in progress" in craft.PLAN_PHASES
    msg = craft.system_message("build me a landing page", tools=True)
    assert "PROGRESS.md" in msg["content"]


# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #

def test_the_plan_route_reads_the_file_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_agent_gate", lambda: None)
    monkeypatch.setattr(A.agentic_chat, "get_session",
                        lambda sid: {"session_id": sid, "project_dir": str(tmp_path)})
    (tmp_path / "PROGRESS.md").write_text("- [x] a\n- [ ] b\n", encoding="utf-8")
    hdr = {"X-Free-LLM-Hub": "dashboard",
           "X-Free-LLM-Hub-Token": A.config.get_control_token() or ""}
    r = A.app.test_client().get("/api/agent/sessions/s9/plan", headers=hdr)
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    assert [t["text"] for t in d["tasks"]] == ["a", "b"] and d["done"] == 1
    assert d["source"] == "PROGRESS.md" and d["interrupted"] is None


def test_the_page_shows_it_and_offers_continue():
    assert 'id="agent-plan"' in SRC
    body = SRC[SRC.index("function renderPlan(p){"):]
    body = body[:body.index("function initPlanStrip(){")]
    assert "'Continue from there'" in body
    assert "Continue exactly where you stopped" in body
    assert "doSend();" in body
    assert "loadPlan();" in SRC[SRC.index("function refreshSessionInfo(){"):][:200]


# --------------------------------------------------------------------------- #
# The brief says where the files go
# --------------------------------------------------------------------------- #
# MEASURED 2026-09-12 in a normal session on Windows: the model ran `pwd` in
# the bash tool, got a POSIX spelling, and wrote index.html, style.css and
# PROGRESS.md under /workspace -- C:\workspace -- while the project stayed
# empty. A normal turn's own instruction rides in argv, which on the shell
# path has ~60 characters to spare, so the line lives in the brief the agent
# is told to read first.

def test_the_brief_opens_with_the_project_folder(tmp_path):
    name = AC.write_task_brief(str(tmp_path), "hello there", session_id="abcdef123456")
    assert name, "the brief is written even when no standard matches -- the folder always applies"
    body = (tmp_path / name).read_text(encoding="utf-8")
    assert "THE PROJECT FOLDER IS: " + os.path.abspath(str(tmp_path)) in body
    assert "never write to /, /tmp or /workspace" in body
    assert body.index("THE PROJECT FOLDER IS") < len(body) // 3, "first, before any standard"


def test_it_costs_the_command_line_nothing():
    """The folder rides in the file, not in argv: the shell path's turn-1
    command line stays where it was measured."""
    src = open("agentic_chat.py", encoding="utf-8").read()
    assert "THE PROJECT FOLDER IS" not in AC._PLANNING_SNIPPET
    assert "THE PROJECT FOLDER IS" in src[src.index("def write_task_brief("):src.index("def _claude_model_for(")]


def test_the_strip_follows_the_file_while_the_turn_runs():
    body = SRC[SRC.index("function initPlanStrip(){"):]
    body = body[:body.index("function refreshSessionInfo(){")]
    assert "if (sessionId && turnBusy) loadPlan();" in body
