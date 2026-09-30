"""The Build page's task list follows a running Multi run.

MEASURED 2026-09-30, session 47a25faa: during a four-phase run the strip read
"9/9 done" from the previous turn's PROGRESS.md ("ALL GATES COMPLETE"), and
the run's workers were never asked to update that file. Owner: "he should
always update his todolist and progress".
"""
import os
import time

import app as A
import swarm_windows as SW


def _run(tmp_path, owner="conv-1"):
    run = SW._Run("fix the contours", str(tmp_path), "opencode", SW.clean_phases({"phases": [
        {"title": "Diagnose", "task": "t"}, {"title": "Fix", "task": "t"},
        {"title": "Review and finish", "task": "t"}]}), owner=owner)
    run.state = SW.RUNNING
    run.agents[0].state, run.agents[0].session_id = SW.DONE, "w1"
    run.agents[1].state, run.agents[1].session_id = SW.RUNNING, "w2"
    SW._remember(run)
    return run


def _forget(run):
    with SW._LOCK:
        SW._RUNS.pop(run.id, None)


def test_a_running_run_is_the_task_list(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_WORKER_MODEL", {"w1": "space-bunny-alpha", "w2": "glm-5.3"})
    (tmp_path / "PROGRESS.md").write_text("- [x] old task\nALL GATES COMPLETE\n")
    run = _run(tmp_path)
    try:
        plan = A._multi_run_plan("conv-1", str(tmp_path))
    finally:
        _forget(run)
    assert [t["text"] for t in plan["tasks"]] == [
        "Phase 1: Diagnose · space-bunny-alpha",
        "Phase 2: Fix · glm-5.3",
        "Phase 3: Review and finish"]
    assert [(t["done"], t["doing"]) for t in plan["tasks"]] == [
        (True, False), (False, True), (False, False)]
    assert plan["done"] == 1 and plan["source"].startswith("multi-session run")


def test_an_ended_run_gives_way_to_a_newer_progress_file(tmp_path):
    run = _run(tmp_path)
    run.state, run.ended_at = SW.DONE, time.time() - 60
    p = tmp_path / "PROGRESS.md"
    p.write_text("- [x] Phase 1\n")
    os.utime(p, (time.time(), time.time()))
    try:
        assert A._multi_run_plan("conv-1", str(tmp_path)) is None
        os.utime(p, (run.created_at - 600, run.created_at - 600))       # older than the run
        assert A._multi_run_plan("conv-1", str(tmp_path)) is not None
    finally:
        _forget(run)


def test_no_run_means_the_old_list(tmp_path):
    assert A._multi_run_plan("no-such-conversation", str(tmp_path)) is None


def test_the_plan_route_uses_it():
    src = open("app.py", encoding="utf-8").read()
    body = src[src.index("def api_agent_plan("):]
    body = body[:body.index("\n@app.route")]
    assert body.index("_multi_run_plan(") < body.index("update_tasks_from_project")


def test_every_worker_is_told_to_keep_progress_md_true(tmp_path):
    run = _run(tmp_path)
    try:
        prompt = SW._agent_prompt(run, run.agents[1])
    finally:
        _forget(run)
    assert "PROGRESS.md" in prompt
    assert "Phase 2: Fix" in prompt and run.id in prompt


# --------------------------------------------------------------------------- #
# The helpers panel (owner, 2026-09-30: "see the helpers in the same
# conversation, and open each one we want in a new window")
# --------------------------------------------------------------------------- #
def test_each_helper_row_carries_what_the_panel_shows(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_WORKER_MODEL", {"w2": "glm-5.3"})
    run = _run(tmp_path)
    run.agents[1].started_at = 1000.0
    run.agents[1].events.append({"event": "tool", "text": "edit app.py"})
    run.agents[1].events.append({"event": "usage", "text": "not an action"})
    try:
        plan = A._multi_run_plan("conv-1", str(tmp_path))
    finally:
        _forget(run)
    h = plan["tasks"][1]
    assert (h["index"], h["title"], h["model"]) == (2, "Fix", "glm-5.3")
    assert h["url"] == "/agent/w2" and h["session_id"] == "w2"
    assert h["last"] == "edit app.py" and h["started_at"] == 1000.0
    assert plan["current"] == 2 and plan["run_state"] == SW.RUNNING
    assert plan["tasks"][2]["url"] is None                     # not started yet


HTML = open("templates/index.html", encoding="utf-8").read()


def test_the_panel_opens_a_helper_in_a_new_window_safely():
    js = HTML[HTML.index("function helperRow("):]
    js = js[:js.index("function renderPlan(")]
    assert "a.target = '_blank'" in js and "a.rel = 'noopener'" in js
    assert "aria-label" in js                                   # says it opens a new window
    assert "HP_STATE[state]" in js                              # state in words, not colour only


def test_the_panel_css_uses_theme_tokens_and_respects_reduced_motion():
    css = [l for l in HTML.splitlines() if ".hp" in l and "{" in l]
    assert css, "helper styles missing"
    import re
    assert not [l for l in css if re.search(r"#[0-9a-fA-F]{3,8}\b", l)]
    assert "@media (prefers-reduced-motion:reduce){ .agent-plan-list li.hp.doing .pl-box{ animation:none } }" in HTML


def test_a_live_run_keeps_the_panel_fresh_without_a_busy_turn():
    assert "_planLive || (turnBusy" in HTML
