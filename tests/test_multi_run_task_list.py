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
