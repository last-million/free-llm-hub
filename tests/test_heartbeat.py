"""Heartbeats: schedule parsing, due computation, the skip rules, one beat at a
time, and a beat that starts a run with tasks + a budget.

Every side of the scheduler is injected, so these drive heartbeat.Scheduler.tick()
by hand with a fake clock, a fake task board and a fake start_run -- no thread,
no real Multi run, no settings file. Deterministic throughout.
"""
import time

import heartbeat as H


# --------------------------------------------------------------------------- #
# cron-lite parsing
# --------------------------------------------------------------------------- #
def test_every_minutes_and_hours():
    assert H.parse_when("every 30 min")["seconds"] == 1800
    assert H.parse_when("every 2 hours")["seconds"] == 7200
    assert H.parse_when("EVERY 5 Minutes")["seconds"] == 300
    assert H.parse_when("every 1 h")["seconds"] == 3600


def test_daily_and_weekly():
    assert H.parse_when("daily 09:00") == {"kind": "daily", "hh": 9, "mm": 0}
    assert H.parse_when("weekdays 18:30")["days"] == [0, 1, 2, 3, 4]
    assert H.parse_when("weekends 10:00")["days"] == [5, 6]
    assert H.parse_when("mon,wed,fri 07:15")["days"] == [0, 2, 4]


def test_unreadable_schedules_are_none():
    for bad in ("", "nonsense", "every 0 min", "daily 25:00", "daily 9", "xyz 09:00"):
        assert H.parse_when(bad) is None


# --------------------------------------------------------------------------- #
# due / next_due
# --------------------------------------------------------------------------- #
def test_every_due_from_last_beat():
    p = H.parse_when("every 10 min")
    assert H.due(p, 1000, None) is True            # never beaten = due
    assert H.due(p, 1000, 700) is False            # 300s < 600s
    assert H.due(p, 1000, 300) is True             # 700s >= 600s


def _noon(day_epoch):
    """An epoch at local noon, so HH:MM slots sit cleanly inside the day."""
    lt = time.localtime(day_epoch)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 12, 0, 0, 0, 0, -1))


def test_daily_due_only_after_its_time_and_once():
    p = H.parse_when("daily 09:00")
    noon = _noon(time.time())
    nine = noon - 3 * 3600                          # 09:00 the same day
    assert H.due(p, nine - 60, None) is False       # before 09:00
    assert H.due(p, noon, None) is True             # after, not yet beaten
    assert H.due(p, noon, nine + 10) is False       # already beaten today
    assert H.due(p, noon, nine - 86400) is True     # last beat was yesterday


def test_next_due_is_in_the_future_for_time_schedules():
    p = H.parse_when("daily 09:00")
    now = _noon(time.time())                         # it is noon, past 09:00
    nd = H.next_due(p, now, now - 3 * 3600 + 5)       # already beaten today
    assert nd is not None and nd > now               # tomorrow's 09:00


def test_quiet_hours_including_wrap_past_midnight():
    day = _noon(time.time())
    at_23 = day + 11 * 3600
    at_03 = day - 9 * 3600
    assert H.in_quiet_hours("22:00-07:00", at_23) is True
    assert H.in_quiet_hours("22:00-07:00", at_03) is True
    assert H.in_quiet_hours("22:00-07:00", day) is False      # noon
    assert H.in_quiet_hours(None, at_23) is False


# --------------------------------------------------------------------------- #
# schedule records
# --------------------------------------------------------------------------- #
def test_normalize_requires_when_and_a_target():
    assert H.normalize_schedule({"when": "every 5 min"}) is None       # no goal/project
    assert H.normalize_schedule({"goal_id": "g"}) is None              # no when
    s = H.normalize_schedule({"when": "every 5 min", "goal_id": "g", "max_tasks": 99})
    assert s["goal_id"] == "g" and s["max_tasks"] == 50 and s["id"]
    assert s["budget"]["seconds"] == H.DEFAULT_BUDGET["seconds"]


def test_add_update_remove():
    lst, s = H.add([], {"when": "daily 09:00", "project_dir": "/p"})
    assert s and len(lst) == 1
    lst, s2 = H.update(lst, s["id"], {"enabled": False, "max_tasks": 2})
    assert s2["enabled"] is False and s2["max_tasks"] == 2
    lst, removed = H.remove(lst, s["id"])
    assert removed and lst == []


# --------------------------------------------------------------------------- #
# the scheduler: fakes
# --------------------------------------------------------------------------- #
class FakeBoard:
    def __init__(self, tasks):
        self.tasks = list(tasks)
        self.updates = []
        self.goal_for_calls = []

    def next_batch(self, goal_id, n):
        return self.tasks[:n]

    def goal_for(self, project_dir):
        self.goal_for_calls.append(project_dir)
        return {"id": "goal-for-" + project_dir}

    def update(self, task_id, status=None, note=None, owner=None, run_id=None):
        self.updates.append({"task_id": task_id, "status": status,
                             "owner": owner, "run_id": run_id})


def _make(sched, *, now=1000.0, board=None, busy=False, ram=True, providers=True,
          enabled=True, started=None):
    schedules = [sched]
    state = {}
    runs = started if started is not None else []

    def start_run(s, goal_text, tasks, budget):
        rid = "run-%d" % (len(runs) + 1)
        runs.append({"sched": s, "goal": goal_text, "tasks": tasks,
                     "budget": budget, "run_id": rid})
        return rid

    sch = H.Scheduler(
        now=lambda: now,
        load=lambda: schedules,
        state_get=lambda: state,
        state_set=lambda s: state.update(s),
        enabled=lambda: enabled,
        busy=lambda s: busy,
        ram_ok=lambda: ram,
        providers_ok=lambda: providers,
        taskboard=lambda: board,
        start_run=start_run)
    return sch, state, runs


def _sched(**kw):
    base = {"id": "s1", "when": "every 10 min", "goal_id": "g",
            "enabled": True, "max_tasks": 3, "cli": "opencode", "budget": None}
    base.update(kw)
    return base


def test_a_due_beat_starts_one_run_with_tasks_and_a_budget():
    board = FakeBoard([{"id": "t1", "title": "fix a"}, {"id": "t2", "title": "fix b"}])
    sch, state, runs = _make(_sched(), board=board)
    out = sch.tick(now=1000)
    assert [k for k, _s, _i in out] == ["beat"]
    assert len(runs) == 1
    run = runs[0]
    assert run["tasks"] == board.tasks            # the batch, as the goal text's source
    assert "fix a" in run["goal"] and "fix b" in run["goal"]
    assert run["budget"]["seconds"] == H.DEFAULT_BUDGET["seconds"]   # always budgeted
    # tasks marked doing, owned by this heartbeat, tied to the run
    assert {u["task_id"] for u in board.updates} == {"t1", "t2"}
    assert all(u["status"] == "doing" and u["owner"] == "heartbeat:s1"
               and u["run_id"] == "run-1" for u in board.updates)
    assert state["s1"]["last_beat_at"] == 1000
    assert state["s1"]["last_run_id"] == "run-1"


def test_not_due_does_nothing():
    board = FakeBoard([{"id": "t1"}])
    sch, state, runs = _make(_sched(), board=board)
    sch.tick(now=1000)                             # first beat
    runs.clear()
    out = sch.tick(now=1000 + 300)                 # 5 min later, every-10-min: not due
    assert out == [] and runs == []


def test_disabled_schedule_and_kill_switch():
    board = FakeBoard([{"id": "t1"}])
    sch, _s, runs = _make(_sched(enabled=True), board=board, enabled=False)
    assert sch.tick(now=1000) == [] and runs == []        # kill switch off
    sch2, _s2, runs2 = _make(_sched(enabled=False), board=board)
    assert sch2.tick(now=1000) == [] and runs2 == []      # schedule off


def test_skip_when_owner_busy_ram_429_quiet():
    board = FakeBoard([{"id": "t1"}])
    for kw, reason in ((dict(busy=True), "owner busy"),
                       (dict(ram=False), "no RAM room"),
                       (dict(providers=False), "providers rate-limited")):
        sch, state, runs = _make(_sched(), board=board, **kw)
        out = sch.tick(now=1000)
        assert out == [("skip", "s1", reason)]
        assert runs == []
        assert "last_beat_at" not in state["s1"]          # stays due, retried next tick
        assert state["s1"]["last_skip"]["reason"] == reason
    # quiet hours
    sch, state, runs = _make(_sched(quiet="22:00-07:00"), board=board,
                             now=_noon(time.time()) + 11 * 3600)   # 23:00
    out = sch.tick()
    assert out[0][2] == "quiet hours" and runs == []


def test_skip_when_no_tasks():
    board = FakeBoard([])
    sch, state, runs = _make(_sched(), board=board)
    out = sch.tick(now=1000)
    assert out == [("skip", "s1", "no tasks")] and runs == []


def test_project_schedule_resolves_its_goal():
    board = FakeBoard([{"id": "t1", "title": "x"}])
    sch, _s, runs = _make(_sched(goal_id=None, project_dir="/proj"), board=board)
    sch.tick(now=1000)
    assert board.goal_for_calls == ["/proj"]
    assert len(runs) == 1


def test_one_beat_at_a_time():
    board = FakeBoard([{"id": "t1"}])
    sch, _s, runs = _make(_sched(), board=board)
    sch._inflight.add("s1")                         # a beat of s1 is already running
    assert sch.tick(now=1000) == [] and runs == []


def test_boot_starts_the_thread_but_no_immediate_beat():
    board = FakeBoard([{"id": "t1"}])
    sch, _s, runs = _make(_sched(), board=board)
    sch.tick_seconds = 30.0
    sch.start()
    try:
        time.sleep(0.05)
        assert runs == []                           # the loop waits a tick first
    finally:
        sch.stop()


def test_status_reports_schedule_and_last_skip():
    board = FakeBoard([{"id": "t1"}])
    sch, _s, _r = _make(_sched(), board=board, busy=True)
    sch.tick(now=1000)
    st = sch.status(now=1000)
    assert st["enabled"] is True
    row = st["schedules"][0]
    assert row["id"] == "s1" and row["valid"] is True
    assert row["next_due"] is not None
    assert row["last_skip"]["reason"] == "owner busy"
