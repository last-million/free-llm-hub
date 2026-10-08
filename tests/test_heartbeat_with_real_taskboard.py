"""The heartbeat scheduler against the REAL task board (its own tests use a
fake): a due schedule picks the goal's ready tasks, starts one run with them,
and marks them doing under that run; a blocked task waits for its need."""
import os
import tempfile

import heartbeat
import taskboard


def _board():
    root = tempfile.mkdtemp(prefix="hb-board-")
    return taskboard.Board(path=os.path.join(root, "taskboard.json"))


def test_due_schedule_runs_the_ready_tasks_of_the_real_board():
    board = _board()
    project = os.path.join(tempfile.gettempdir(), "hb-project")
    gid = board.add_goal("Ship the invoice exporter", project_dir=project)
    first = board.add_task(gid, "Write the CSV writer")
    second = board.add_task(gid, "Add tests for the writer", needs=(first,))

    sched = heartbeat.normalize_schedule(
        {"when": "every 30 min", "project_dir": project})
    started = []
    state = {}

    def start_run(s, goal_text, tasks, budget):
        started.append((goal_text, [heartbeat._task_id(t) for t in tasks], budget))
        return "swarm-hbtest0001"

    sch = heartbeat.Scheduler(
        now=lambda: 10_000_000.0, load=lambda: [sched],
        state_get=lambda: dict(state), state_set=state.update,
        enabled=lambda: True, taskboard=lambda: board, start_run=start_run)
    sch.tick(now=10_000_000.0)

    assert len(started) == 1
    goal_text, ids, budget = started[0]
    assert ids == [first]                      # the blocked task waits
    assert "Write the CSV writer" in goal_text
    assert budget                              # a heartbeat run always has one
    row = [t for t in board.tasks(goal_id=gid) if t["id"] == first][0]
    assert row["status"] == "doing"
    assert row.get("run_id") == "swarm-hbtest0001"
    other = [t for t in board.tasks(goal_id=gid) if t["id"] == second][0]
    assert other["status"] == "todo"


def test_no_open_goal_means_no_run():
    board = _board()
    sched = heartbeat.normalize_schedule(
        {"when": "every 30 min", "project_dir": "C:/nowhere"})
    started = []
    sch = heartbeat.Scheduler(
        now=lambda: 10_000_000.0, load=lambda: [sched],
        state_get=lambda: {}, state_set=lambda _s: None,
        enabled=lambda: True, taskboard=lambda: board,
        start_run=lambda *a, **k: started.append(a) or "swarm-x")
    sch.tick(now=10_000_000.0)
    assert started == []
