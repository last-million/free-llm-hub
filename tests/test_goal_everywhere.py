"""The goal behind every task reaches the same three surfaces: a Multi phase
prompt, a Build session's brief file, and the opening turn of a terminal CLI
conversation -- never a mid-loop continuation or a compaction request. And a
Multi run's board tasks follow its phases.
"""
import os

import pytest

import app as A
import swarm_windows as SW
import taskboard
import agentic_chat
import ctxwin


@pytest.fixture(autouse=True)
def _clean_board():
    """A fresh in-memory board per test (configure(None) clears it)."""
    taskboard.configure(None)
    yield
    taskboard.configure(None)


# --------------------------------------------------------------------------- #
# (a) Multi: the goal brief is in the planner's view and every worker prompt
# --------------------------------------------------------------------------- #

def test_multi_worker_prompt_carries_the_goal_brief(tmp_path):
    run = SW._Run("ship the page", str(tmp_path), "opencode",
                  SW.clean_phases({"phases": [{"title": "Hero", "task": "build it"}]}),
                  goal_brief="GOAL: ship the landing page\nOPEN TASKS: Hero")
    prompt = SW._agent_prompt(run, run.agents[0])
    assert "GOAL: ship the landing page" in prompt
    assert "keep every step serving it" in prompt


def test_run_row_roundtrip_keeps_goal_brief(tmp_path):
    run = SW._Run("g", str(tmp_path), "opencode",
                  SW.clean_phases({"phases": [{"title": "P", "task": "t"}]}),
                  goal_brief="GOAL: remember me")
    back = SW._Run.from_row(run.row())
    assert back.goal_brief == "GOAL: remember me"


def test_multi_prompt_unchanged_without_a_goal(tmp_path):
    run = SW._Run("g", str(tmp_path), "opencode",
                  SW.clean_phases({"phases": [{"title": "P", "task": "t"}]}))
    assert "THE GOAL BEHIND THIS WORK" not in SW._agent_prompt(run, run.agents[0])


def test_start_feeds_the_planner_the_goal_brief(tmp_path, monkeypatch):
    monkeypatch.setattr(SW, "_walk", lambda *a, **k: None)   # no background work
    seen = {}

    def planner(system, user):
        seen["user"] = user
        return '{"phases":[{"title":"P1","task":"do it","done_when":"done"}]}'

    rid = SW.start("fix the thing", str(tmp_path), "opencode",
                   lambda *a, **k: None, lambda *a, **k: iter(()),
                   planner=planner, goal_brief="GOAL: fix the thing well",
                   review=False)
    assert "GOAL: fix the thing well" in seen.get("user", "")
    assert SW.get(rid).goal_brief == "GOAL: fix the thing well"


# --------------------------------------------------------------------------- #
# (b) Build session brief file carries it (agentic_chat goal-brief source)
# --------------------------------------------------------------------------- #

def test_build_brief_file_carries_the_goal(tmp_path):
    # app registered _goal_brief_for_project on agentic_chat at import; a board
    # goal for this folder is all it needs.
    taskboard.default.add_goal("Build the dashboard", project_dir=str(tmp_path))
    name = agentic_chat.write_task_brief(str(tmp_path), "add a chart")
    assert name
    text = (tmp_path / name).read_text(encoding="utf-8")
    assert "The goal behind this work" in text
    assert "GOAL: Build the dashboard" in text


def test_build_brief_file_unchanged_without_a_goal(tmp_path):
    name = agentic_chat.write_task_brief(str(tmp_path), "add a chart")
    assert name
    text = (tmp_path / name).read_text(encoding="utf-8")
    assert "The goal behind this work" not in text


# --------------------------------------------------------------------------- #
# (c) terminal CLI: one system note on the OPENING turn only
# --------------------------------------------------------------------------- #

def _env_msgs(project_dir, last_user=True):
    env = ("<environment_context>\n  Current working directory: %s\n"
           "  Is directory a git repo: No\n</environment_context>" % project_dir)
    msgs = [{"role": "system", "content": env},
            {"role": "user", "content": "add a booking page"}]
    if not last_user:
        msgs.append({"role": "assistant", "content": "",
                     "tool_calls": [{"id": "c1", "type": "function",
                                     "function": {"name": "sh", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": "c1", "content": "ok"})
    return msgs


def test_goal_note_on_opening_cli_turn(tmp_path):
    taskboard.default.add_goal("Finish the website", project_dir=str(tmp_path))
    out = A._apply_goal_note(_env_msgs(str(tmp_path)))
    joined = " ".join(m.get("content", "") for m in out if m.get("role") == "system")
    assert "GOAL: Finish the website" in joined
    assert len(out) == 3                       # one system note added


def test_goal_note_not_on_continuation(tmp_path):
    taskboard.default.add_goal("Finish the website", project_dir=str(tmp_path))
    msgs = _env_msgs(str(tmp_path), last_user=False)
    out = A._apply_goal_note(msgs)
    assert out is msgs                          # mid tool loop -> untouched


def test_goal_note_not_on_compaction(tmp_path, monkeypatch):
    taskboard.default.add_goal("Finish the website", project_dir=str(tmp_path))
    monkeypatch.setattr(ctxwin, "is_compaction_request", lambda m: True)
    msgs = _env_msgs(str(tmp_path))
    assert A._apply_goal_note(msgs) is msgs


def test_goal_note_absent_without_a_goal(tmp_path):
    msgs = _env_msgs(str(tmp_path))             # no goal on the board
    assert A._apply_goal_note(msgs) is msgs


def test_goal_note_off_when_flag_off(tmp_path, monkeypatch):
    taskboard.default.add_goal("Finish the website", project_dir=str(tmp_path))
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=True: False if k == "goal_brief" else d)
    msgs = _env_msgs(str(tmp_path))
    assert A._apply_goal_note(msgs) is msgs


# --------------------------------------------------------------------------- #
# (4) a Multi run's board tasks follow its phases
# --------------------------------------------------------------------------- #

def test_link_creates_a_task_per_phase(tmp_path):
    gid = taskboard.default.add_goal("the goal", project_dir=str(tmp_path))
    run = SW._Run("g", str(tmp_path), "opencode", SW.clean_phases({"phases": [
        {"title": "Alpha", "task": "t"}, {"title": "Beta", "task": "t"}]}))
    SW._remember(run)
    try:
        A._multi_link_tasks(run.id, str(tmp_path))
        titles = sorted(t["title"] for t in taskboard.default.tasks(goal_id=gid))
        assert titles == ["Alpha", "Beta"]
        with A._MULTI_LOCK:
            assert len(A._MULTI_TASKS.get(run.id) or {}) == 2
    finally:
        with SW._LOCK:
            SW._RUNS.pop(run.id, None)
        A._multi_forget_tasks(run.id)


def test_sync_moves_task_through_doing_done_failed(tmp_path):
    gid = taskboard.default.add_goal("the goal", project_dir=str(tmp_path))
    t1 = taskboard.default.add_task(gid, "P1")
    t2 = taskboard.default.add_task(gid, "P2")
    rid = "swarm-test"
    with A._MULTI_LOCK:
        A._MULTI_TASKS[rid] = {1: t1, 2: t2}
    try:
        A._multi_sync_task(rid, {"index": 1, "state": SW.RUNNING, "session_id": "w1"})
        done = {t["id"]: t for t in taskboard.default.tasks(goal_id=gid)}
        assert done[t1]["status"] == "doing" and done[t1]["owner"] == "w1"
        A._multi_sync_task(rid, {"index": 1, "state": SW.DONE, "verified": True})
        A._multi_sync_task(rid, {"index": 2, "state": SW.FAILED, "error": "boom"})
        done = {t["id"]: t for t in taskboard.default.tasks(goal_id=gid)}
        assert done[t1]["status"] == "done"
        assert done[t2]["status"] == "failed"
    finally:
        A._multi_forget_tasks(rid)


def test_progress_md_ticks_mark_tasks_done(tmp_path):
    gid = taskboard.default.add_goal("the goal", project_dir=str(tmp_path))
    t1 = taskboard.default.add_task(gid, "P1")
    rid = "swarm-prog"
    with A._MULTI_LOCK:
        A._MULTI_TASKS[rid] = {1: t1}
    (tmp_path / "PROGRESS.md").write_text(
        "## run\n- [x] Phase 1: build it\n- [ ] Phase 2: later\n", encoding="utf-8")
    try:
        A._multi_progress_sync(rid, str(tmp_path))
        t = taskboard.default.tasks(goal_id=gid)[0]
        assert t["status"] == "done"
    finally:
        A._multi_forget_tasks(rid)


# --------------------------------------------------------------------------- #
# (5) UI: the Tasks panel is present and theme-token styled
# --------------------------------------------------------------------------- #

def test_tasks_panel_in_the_template():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html = open(os.path.join(here, "templates", "index.html"), encoding="utf-8").read()
    for marker in ('id="agent-tasks"', 'id="agent-goal-form"', 'id="agent-task-form"',
                   'id="agent-tasks-list"', 'loadTasks(', "/api/goals", "/api/tasks"):
        assert marker in html, marker
    # theme tokens, not hard-coded page colours, for the panel's own rules
    block = html[html.index(".agent-tasks{"): html.index(".agent-plan{")]
    assert "var(--" in block
    assert "#000" not in block and "#fff" not in block.replace("color:#fff", "")
