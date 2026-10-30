"""Heartbeats x the task board, three fixes:

(a) a CLOSED goal gives no next batch, so a heartbeat pinned to it stops;
(b) a todo task waiting on a need that can never finish (missing, itself,
    failed, blocked) is listed by Board.stalled() and named in the heartbeat's
    skip reason -- it is still never run early;
(c) a beat whose run did not start keeps its slot (last_beat_at untouched),
    records why, and is retried no sooner than START_RETRY_SECONDS later.

Hermetic: a real taskboard.Board (memory or tmp_path), a fake clock passed to
tick(), a fake start_run -- no thread, no Multi run, no settings file.
"""
import json
import time

import heartbeat as H
import taskboard


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _clocked_board(path=None):
    clock = [100.0]

    def tick_clock():
        clock[0] += 1.0
        return clock[0]

    return taskboard.Board(path=path, clock=tick_clock)


def _sched(**kw):
    base = {"id": "s1", "when": "every 10 min", "goal_id": "g",
            "enabled": True, "max_tasks": 3, "cli": "opencode", "budget": None}
    base.update(kw)
    return base


def _make(board, sched, outcomes=None):
    """A scheduler over `board`. Each start_run call takes the next outcome:
    "ok" (default) -> a run id, None -> nothing started, an exception -> raised."""
    schedules = [sched]
    state = {}
    calls = []
    outcomes = list(outcomes or [])

    def start_run(s, goal_text, tasks, budget):
        calls.append({"goal": goal_text, "tasks": tasks})
        nxt = outcomes.pop(0) if outcomes else "ok"
        if isinstance(nxt, BaseException):
            raise nxt
        if nxt == "ok":
            return "run-%d" % len(calls)
        return nxt

    sch = H.Scheduler(
        now=lambda: 0.0,
        load=lambda: schedules,
        state_get=lambda: state,
        state_set=lambda s: state.update(s),
        enabled=lambda: True,
        busy=lambda s: False,
        ram_ok=lambda: True,
        providers_ok=lambda: True,
        taskboard=lambda: board,
        start_run=start_run)
    return sch, state, calls


def _noon(day_epoch):
    lt = time.localtime(day_epoch)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 12, 0, 0, 0, 0, -1))


# --------------------------------------------------------------------------- #
# (a) a closed goal stops its scheduled runs
# --------------------------------------------------------------------------- #
def test_next_batch_is_empty_once_its_goal_is_closed(tmp_path):
    path = str(tmp_path / "tb.json")
    b = taskboard.Board(path=path)
    gid = b.add_goal("Ship it")
    tid = b.add_task(gid, "a todo task")
    assert [t["id"] for t in b.next_batch(gid, 5)] == [tid]
    assert b.close_goal(gid) is True
    assert b.next_batch(gid, 5) == []
    # The leftover task keeps its status; only the pick stops.
    assert b.tasks(goal_id=gid)[0]["status"] == "todo"
    # Still closed after a reload from the file.
    assert taskboard.Board(path=path).next_batch(gid, 5) == []
    # A task under a goal id the board does not know is still offered, as before.
    orphan = b.add_task("g-unknown", "orphan")
    assert [t["id"] for t in b.next_batch("g-unknown", 5)] == [orphan]


def test_a_heartbeat_pinned_to_a_closed_goal_starts_nothing():
    board = taskboard.Board()
    gid = board.add_goal("Old work")
    board.add_task(gid, "left over 1")
    board.add_task(gid, "left over 2")
    board.close_goal(gid)
    sch, state, calls = _make(board, _sched(goal_id=gid))
    out = sch.tick(now=1000)
    assert out == [("skip", "s1", "no tasks")]
    assert calls == []
    assert all(t["status"] == "todo" for t in board.tasks(goal_id=gid))
    assert sch.status(now=1000)["schedules"][0]["stalled"] == 0


# --------------------------------------------------------------------------- #
# (b) tasks that wait on a need that can never finish
# --------------------------------------------------------------------------- #
def _write_board(path):
    def task(tid, goal, status="todo", needs=(), created=0, priority=0, title=None):
        return {"id": tid, "goal_id": goal, "title": title or tid, "status": status,
                "needs": list(needs), "created_at": created, "priority": priority}

    data = {"version": 1, "goals": [
        {"id": "g1", "title": "One", "status": "open", "created_at": 1},
        {"id": "g2", "title": "Two", "status": "open", "created_at": 2},
    ], "tasks": [
        task("dep_failed", "g1", "failed", created=1),
        task("dep_blocked", "g1", "blocked", created=2),
        task("dep_doing", "g1", "doing", created=3),
        task("dep_todo", "g1", "todo", created=4),
        task("dep_done", "g1", "done", created=5),
        task("w_failed", "g1", needs=["dep_failed"], created=10),
        task("w_blocked", "g1", needs=["dep_blocked"], created=11),
        task("w_missing", "g1", needs=["t-gone"], created=12, priority=3),
        task("w_other_goal", "g1", needs=["other_goal_task"], created=13),
        task("w_self", "g1", needs=["w_self"], created=14),
        task("w_doing", "g1", needs=["dep_doing"], created=15),
        task("w_todo", "g1", needs=["dep_todo"], created=16),
        task("w_done", "g1", needs=["dep_done"], created=17),
        task("w_two", "g1", needs=["dep_done", "dep_failed"], created=18),
        task("running_missing", "g1", "doing", needs=["t-gone"], created=19),
        task("other_goal_task", "g2", "done", created=20),
    ]}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def test_stalled_names_each_task_whose_need_can_never_finish(tmp_path):
    path = tmp_path / "tb.json"
    _write_board(str(path))
    b = taskboard.Board(path=str(path))
    before = path.read_bytes()
    rows = b.stalled("g1")
    assert [(r["id"], r["need"], r["reason"]) for r in rows] == [
        ("w_missing", "t-gone", "missing"),          # priority 3 first
        ("w_failed", "dep_failed", "failed"),
        ("w_blocked", "dep_blocked", "blocked"),
        ("w_other_goal", "other_goal_task", "missing"),   # a task of another goal
        ("w_self", "w_self", "self"),
        ("w_two", "dep_failed", "failed"),
    ]
    assert rows[0]["title"] == "w_missing"
    assert set(r["reason"] for r in rows) <= set(taskboard.Board.STALL_REASONS)
    # The rule stands: nothing stalled is offered, nothing is changed.
    assert {t["id"] for t in b.next_batch("g1", 50)} == {"dep_todo", "w_done"}
    assert path.read_bytes() == before
    assert {t["id"]: t["status"] for t in b.tasks(goal_id="g1")}["w_failed"] == "todo"


def test_stalled_is_empty_for_a_closed_goal_and_never_raises():
    b = taskboard.Board()
    gid = b.add_goal("g")
    b.add_task(gid, "waits on nothing real", needs=["t-nope"])
    assert len(b.stalled(gid)) == 1
    b.close_goal(gid)
    assert b.stalled(gid) == []
    assert b.stalled("g-unknown") == []
    assert b.stalled(None) == []
    assert b.stalled(["not", "hashable"]) == []


def test_heartbeat_skip_names_tasks_that_wait_on_a_failed_or_missing_task():
    board = _clocked_board()
    gid = board.add_goal("Docs")
    long_title = "Write the onboarding guide " + "x" * 500
    board.add_task(gid, long_title, needs=["t-deleted"])
    dep = board.add_task(gid, "Build API")
    board.update(dep, status="failed")
    board.add_task(gid, "Write the API docs", needs=[dep])
    sch, state, calls = _make(board, _sched(goal_id=gid))
    out = sch.tick(now=1000)
    assert len(out) == 1 and out[0][:2] == ("skip", "s1")
    reason = out[0][2]
    assert reason.startswith("no ready tasks: 2 task(s) wait on a failed, blocked or missing task")
    assert "Write the onboarding guide" in reason and "(waits on a missing task)" in reason
    assert long_title not in reason and len(reason) <= H.REASON_CHARS
    assert calls == []
    assert state["s1"]["last_skip"]["reason"] == reason
    assert "last_beat_at" not in state["s1"]
    assert H.stalled_reason([]) == ""
    row = sch.status(now=1000)["schedules"][0]
    assert row["stalled"] == 2


def test_status_stalled_count_resolves_a_project_goal_and_fails_open(tmp_path):
    board = taskboard.Board()
    proj = str(tmp_path / "proj")
    gid = board.add_goal("P", project_dir=proj)
    dep = board.add_task(gid, "dep")
    board.update(dep, status="blocked")
    board.add_task(gid, "waits", needs=[dep])
    sch, _state, _calls = _make(board, _sched(goal_id=None, project_dir=proj))
    assert sch.status(now=1000)["schedules"][0]["stalled"] == 1
    out = sch.tick(now=1000)
    assert "waits (waits on a blocked task)" in out[0][2]

    class NoStalled:                       # a board without stalled(): unknown
        def next_batch(self, goal_id, n):
            return []

        def goal_for(self, project_dir):
            return None

    sch2, _s2, _c2 = _make(NoStalled(), _sched())
    assert sch2.status(now=1000)["schedules"][0]["stalled"] is None
    assert sch2.tick(now=1000) == [("skip", "s1", "no tasks")]

    class Broken(NoStalled):               # stalled() that raises: unknown, no crash
        def stalled(self, goal_id):
            raise RuntimeError("boom")

    sch3, _s3, _c3 = _make(Broken(), _sched())
    assert sch3.status(now=1000)["schedules"][0]["stalled"] is None
    assert sch3.tick(now=1000) == [("skip", "s1", "no tasks")]


# --------------------------------------------------------------------------- #
# (c) a run that did not start keeps its slot
# --------------------------------------------------------------------------- #
def test_a_run_that_did_not_start_keeps_its_slot_and_waits_before_retrying():
    board = taskboard.Board()
    gid = board.add_goal("g")
    tid = board.add_task(gid, "fix a")
    sch, state, calls = _make(board, _sched(goal_id=gid), outcomes=[None, None, "ok"])
    wait = H.START_RETRY_SECONDS

    out = sch.tick(now=1000)
    assert len(out) == 1 and out[0][:2] == ("skip", "s1")
    assert out[0][2].startswith("could not start the run")
    assert len(calls) == 1
    assert "last_beat_at" not in state["s1"]                # the slot is kept
    assert state["s1"]["start_failed_at"] == 1000
    assert state["s1"]["last_skip"]["reason"] == out[0][2]
    assert board.tasks(goal_id=gid)[0]["status"] == "todo"

    assert sch.tick(now=1030) == []                         # no tight loop
    assert sch.tick(now=1000 + wait - 1) == []
    assert len(calls) == 1

    out = sch.tick(now=1000 + wait)                         # retried, fails again
    assert out[0][0] == "skip" and len(calls) == 2
    assert state["s1"]["start_failed_at"] == 1000 + wait

    t3 = 1000 + 2 * wait
    out = sch.tick(now=t3)                                  # retried, starts
    assert out[0][0] == "beat" and len(calls) == 3
    beat = out[0][2]
    assert beat == {"at": t3, "tasks": [tid], "run_id": "run-3", "result": "started"}
    assert state["s1"]["last_beat_at"] == t3
    assert "start_failed_at" not in state["s1"] and "last_skip" not in state["s1"]
    assert board.tasks(goal_id=gid)[0]["status"] == "doing"


def test_a_failed_start_never_burns_a_daily_slot():
    board = taskboard.Board()
    gid = board.add_goal("g")
    board.add_task(gid, "daily job")
    sch, state, calls = _make(board, _sched(goal_id=gid, when="daily 09:00"),
                              outcomes=[None, "ok"])
    noon = _noon(time.time())
    assert sch.tick(now=noon)[0][0] == "skip"
    out = sch.tick(now=noon + H.START_RETRY_SECONDS)        # the same day: still due
    assert out[0][0] == "beat" and len(calls) == 2
    assert state["s1"]["last_beat_at"] == noon + H.START_RETRY_SECONDS


def test_a_start_that_raises_records_the_class_never_the_message():
    board = taskboard.Board()
    gid = board.add_goal("g")
    board.add_task(gid, "t")
    secret = RuntimeError("key sk-live-SECRET at https://user:pw@internal.example/x")
    sch, state, calls = _make(board, _sched(goal_id=gid), outcomes=[secret])
    out = sch.tick(now=1000)
    reason = out[0][2]
    assert out[0][0] == "skip" and "(RuntimeError)" in reason
    assert "SECRET" not in reason and "https" not in reason
    assert len(reason) <= H.REASON_CHARS
    assert "beats" not in state["s1"] and "last_run_id" not in state["s1"]
    assert "SECRET" not in json.dumps(state)


def test_status_shows_the_retry_time_and_a_clock_that_went_back_is_not_waited_out():
    board = taskboard.Board()
    gid = board.add_goal("g")
    board.add_task(gid, "t")
    sch, state, calls = _make(board, _sched(goal_id=gid), outcomes=[None])
    sch.tick(now=1000)
    row = sch.status(now=1010)["schedules"][0]
    assert row["next_due"] == 1000 + H.START_RETRY_SECONDS
    assert row["last_skip"]["reason"].startswith("could not start the run")
    later = 1000 + H.START_RETRY_SECONDS + 1
    assert sch.status(now=later)["schedules"][0]["next_due"] == later

    # A failure stamped in the future of the clock (it went back) is ignored.
    state["s1"] = {"start_failed_at": 50_000.0}
    assert H.start_retry_at(state["s1"], 1000) is None
    out = sch.tick(now=1000)
    assert out[0][0] == "beat" and len(calls) == 2
    assert "start_failed_at" not in state["s1"]
