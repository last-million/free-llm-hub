"""The task board: goals, tasks, dependencies, next_batch, the goal brief,
persistence and a corrupt file. Pure module, no app import."""
import json

import taskboard


def _board(tmp_path):
    return taskboard.Board(path=str(tmp_path / "taskboard.json"))


def test_add_goal_and_tasks_roundtrip(tmp_path):
    b = _board(tmp_path)
    gid = b.add_goal("Ship the landing page", project_dir=str(tmp_path / "proj"),
                     detail="conversion-focused")
    assert gid.startswith("g-")
    t1 = b.add_task(gid, "Build hero", files=["index.html"], priority=2)
    t2 = b.add_task(gid, "Write copy", needs=[t1])
    assert t1.startswith("t-") and t2.startswith("t-")
    tasks = b.tasks(goal_id=gid)
    assert [t["title"] for t in tasks] == ["Build hero", "Write copy"]
    assert tasks[0]["status"] == "todo"
    assert tasks[1]["needs"] == [t1]


def test_goals_filtered_by_project_and_open_state(tmp_path):
    b = _board(tmp_path)
    a = b.add_goal("A", project_dir=str(tmp_path / "a"))
    b.add_goal("B", project_dir=str(tmp_path / "b"))
    assert len(b.goals()) == 2
    assert [g["title"] for g in b.goals(project_dir=str(tmp_path / "a"))] == ["A"]
    assert b.close_goal(a) is True
    assert b.close_goal(a) is False          # already closed
    assert b.goals() and all(g["title"] == "B" for g in b.goals())
    assert len(b.goals(include_closed=True)) == 2


def test_goal_for_is_newest_open(tmp_path):
    b = _board(tmp_path)
    clock = [100.0]
    b2 = taskboard.Board(path=str(tmp_path / "b.json"), clock=lambda: clock[0])
    pd = str(tmp_path / "proj")
    b2.add_goal("old", project_dir=pd)
    clock[0] = 200.0
    newer = b2.add_goal("new", project_dir=pd)
    g = b2.goal_for(pd)
    assert g is not None and g["id"] == newer and g["title"] == "new"
    assert b2.goal_for(str(tmp_path / "nope")) is None


def test_next_batch_respects_deps_priority_and_age(tmp_path):
    b = _board(tmp_path)
    gid = b.add_goal("g")
    a = b.add_task(gid, "a", priority=0)
    hi = b.add_task(gid, "hi", priority=5)
    blocked = b.add_task(gid, "blocked", needs=[a])
    batch = b.next_batch(gid, 10)
    titles = [t["title"] for t in batch]
    # "hi" (priority 5) before "a"; "blocked" excluded (dep not done).
    assert titles == ["hi", "a"]
    b.update(a, status="done")
    titles2 = [t["title"] for t in b.next_batch(gid, 10)]
    assert "blocked" in titles2            # dependency satisfied
    assert b.next_batch(gid, 0) == []


def test_update_appends_history_and_returns_task(tmp_path):
    b = _board(tmp_path)
    gid = b.add_goal("g")
    tid = b.add_task(gid, "t")
    out = b.update(tid, status="doing", owner="sess-1", note="started")
    assert out["status"] == "doing" and out["owner"] == "sess-1"
    assert out["history"] and out["history"][-1]["note"] == "started"
    assert b.update("t-nope") is None
    # Unknown status ignored, note still recorded.
    out2 = b.update(tid, status="banana", note="n2")
    assert out2["status"] == "doing"
    assert out2["history"][-1]["note"] == "n2"


def test_tasks_status_filter_and_by_project(tmp_path):
    b = _board(tmp_path)
    pd = str(tmp_path / "proj")
    gid = b.add_goal("g", project_dir=pd)
    done = b.add_task(gid, "done one")
    b.add_task(gid, "todo one")
    b.update(done, status="done")
    assert [t["title"] for t in b.tasks(goal_id=gid, status="done")] == ["done one"]
    assert len(b.tasks(project_dir=pd)) == 2
    assert b.tasks(project_dir=str(tmp_path / "other")) == []


def test_goal_brief_shape_and_bound(tmp_path):
    b = _board(tmp_path)
    pd = str(tmp_path / "proj")
    gid = b.add_goal("Make it fast", project_dir=pd, detail="under 1s LCP")
    b.add_task(gid, "Lazy-load images")
    doing = b.add_task(gid, "Inline critical CSS")
    b.update(doing, status="doing")
    done = b.add_task(gid, "already shipped")
    b.update(done, status="done")
    brief = b.goal_brief(project_dir=pd)
    assert brief.startswith("GOAL: Make it fast")
    assert "under 1s LCP" in brief
    assert "Lazy-load images" in brief
    assert "Inline critical CSS [doing]" in brief
    assert "already shipped" not in brief          # done tasks are not "open"
    short = b.goal_brief(project_dir=pd, max_chars=20)
    assert len(short) <= 20
    assert b.goal_brief(project_dir=str(tmp_path / "none")) == ""


def test_goal_brief_resolution_order(tmp_path):
    b = _board(tmp_path)
    gid = b.add_goal("only goal")
    assert b.goal_brief(goal_id=gid).startswith("GOAL: only goal")
    # No goal_id, no project_dir -> newest open goal on the board.
    assert b.goal_brief().startswith("GOAL: only goal")


def test_persistence_across_instances(tmp_path):
    path = str(tmp_path / "tb.json")
    b = taskboard.Board(path=path)
    gid = b.add_goal("persist me", project_dir=str(tmp_path / "p"))
    tid = b.add_task(gid, "a task")
    b.update(tid, status="doing", note="go")
    b2 = taskboard.Board(path=path)
    assert [g["title"] for g in b2.goals()] == ["persist me"]
    t = b2.tasks(goal_id=gid)
    assert t and t[0]["status"] == "doing" and t[0]["history"]


def test_corrupt_file_leaves_empty_board(tmp_path):
    path = tmp_path / "tb.json"
    path.write_text("{not json at all", encoding="utf-8")
    b = taskboard.Board(path=str(path))
    assert b.goals() == []
    assert b.load() is False
    # And the board still works after a corrupt load.
    gid = b.add_goal("fresh")
    assert b.goals()[0]["id"] == gid


def test_add_task_records_run_and_phase(tmp_path):
    path = str(tmp_path / "tb.json")
    b = taskboard.Board(path=path)
    gid = b.add_goal("g")
    tid = b.add_task(gid, "P1", run_id="swarm-x", phase=1)
    t = b.tasks(goal_id=gid)[0]
    assert t["id"] == tid and t["run_id"] == "swarm-x" and t["phase"] == 1
    # A non-int phase is tolerated as None; persistence keeps run_id/phase.
    assert b.add_task(gid, "P?", phase="nope")
    b2 = taskboard.Board(path=path)
    kept = {x["title"]: x for x in b2.tasks(goal_id=gid)}
    assert kept["P1"]["run_id"] == "swarm-x" and kept["P1"]["phase"] == 1
    assert kept["P?"]["phase"] is None


def test_memory_only_board_never_writes(tmp_path):
    b = taskboard.Board(path=None)
    gid = b.add_goal("in memory")
    assert b.goals()[0]["id"] == gid
    assert not list(tmp_path.iterdir())    # nothing written


def test_configure_points_default_at_file(tmp_path):
    path = str(tmp_path / "default.json")
    try:
        taskboard.configure(path)
        gid = taskboard.default.add_goal("via default")
        assert taskboard.default.goal_brief(goal_id=gid).startswith("GOAL: via default")
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        assert data["goals"][0]["title"] == "via default"
    finally:
        taskboard.configure(None)          # back to memory for other tests
