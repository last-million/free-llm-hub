"""A helper's own page shows its run, itself marked, and where it belongs.

Owner, 2026-09-30, on http://127.0.0.1:8787/agent/fe3a67bf...: "when I open
helpers in a new window I see nothing accurate, like the old plan from
PROGRESS.md, and I don't see what happens in the helper and its status live".
MEASURED: that helper (phase 2 of swarm-f1ad3285449e) showed the project's
shared PROGRESS.md (28 items) as its plan.
"""
import os
import time

import app as A
import swarm_windows as SW

HTML = open("templates/index.html", encoding="utf-8").read()


def _run(tmp_path):
    run = SW._Run("fix the editor", str(tmp_path), "opencode", SW.clean_phases({"phases": [
        {"title": "Fix zoom", "task": "t"}, {"title": "Fix pen tool dark mode", "task": "t"},
        {"title": "Review and finish", "task": "t", "needs": [1, 2]}]}), owner="conv-7")
    run.state = SW.RUNNING
    run.agents[0].state, run.agents[0].session_id = SW.RUNNING, "h-1"
    run.agents[1].state, run.agents[1].session_id = SW.RUNNING, "h-2"
    SW._remember(run)
    return run


def test_a_helper_page_shows_its_run_not_the_projects_progress_file(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_WORKER_MODEL", {})
    p = tmp_path / "PROGRESS.md"
    p.write_text("- [x] old one\n- [x] old two\n")
    os.utime(p, (time.time() + 60, time.time() + 60))          # newer than the run
    run = _run(tmp_path)
    try:
        plan = A._multi_run_plan("h-2", str(tmp_path))
    finally:
        with SW._LOCK:
            SW._RUNS.pop(run.id, None)
    assert [t["title"] for t in plan["tasks"]] == ["Fix zoom", "Fix pen tool dark mode",
                                                  "Review and finish"]
    assert [t["this"] for t in plan["tasks"]] == [False, True, False]
    assert plan["helper"]["index"] == 2 and plan["helper"]["owner"] == "conv-7"


def test_the_conversation_itself_is_not_marked_as_a_helper(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_WORKER_MODEL", {})
    run = _run(tmp_path)
    try:
        plan = A._multi_run_plan("conv-7", str(tmp_path))
    finally:
        with SW._LOCK:
            SW._RUNS.pop(run.id, None)
    assert plan["helper"] is None and not any(t["this"] for t in plan["tasks"])


def test_the_page_says_which_helper_and_links_its_conversation():
    assert 'id="agent-helper-bar"' in HTML
    assert HTML.index('id="agent-helper-bar"') < HTML.index('id="agent-plan"')
    bar = HTML[HTML.index("function renderHelperBar(h, total){"):]
    bar = bar[:bar.index("function renderResume(")]
    assert "'/agent/' + encodeURIComponent(h.owner)" in bar
    assert "a.target = '_blank'" in bar and "a.rel = 'noopener'" in bar
    assert "renderHelperBar(p.helper || null" in HTML
    row = HTML[HTML.index("function helperRow("):]
    row = row[:row.index("function renderPlan(")]
    assert "'this window'" in row and "hp-this" in row
