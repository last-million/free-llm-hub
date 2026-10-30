"""Heartbeats: agents that wake themselves up.

Owner, 2026-10-07 (inspired by Paperclip's heartbeats + budgets): the owner
sets a schedule, and at the times it names the hub takes the next open tasks of
a goal and starts ONE Multi run to work on them -- without the owner having to
be there. A beat that cannot start cleanly (the owner is busy, the machine is
short on RAM, the providers are rate-limited, or it is inside the owner's quiet
hours) is SKIPPED with the reason recorded, and tried again on the next tick --
never in a tight retry loop, never two beats of one schedule at once. A beat
whose run then fails to start keeps its slot too, and is tried again no sooner
than START_RETRY_SECONDS later. When no task is ready because todo tasks wait
on a task that failed, is blocked or is missing, the skip reason says so.

This module is pure and testable: the clock, the settings store, the "is the
owner busy / is there RAM / are the providers ok" checks, the task board and
the "start a Multi run" action are all INJECTED. app.py wires the real ones in
its "Heartbeats and budgets" section and starts the one daemon thread at boot;
tests inject fakes and drive `tick(now=...)` by hand.

Nothing here imports app, swarm_windows or config at module load; the taskboard
is imported lazily (agent T ships it in parallel), and a missing/implausible
board is treated as "no tasks", i.e. a skip, never a crash.
"""

import logging
import re
import threading
import time
import uuid

log = logging.getLogger("heartbeat")

# A heartbeat run ALWAYS carries a budget -- an unattended run that never stops
# is exactly what budgets exist to prevent. The schedule's own budget wins;
# this is the safe default when it names none.
DEFAULT_BUDGET = {"seconds": 45 * 60, "tokens": 2_000_000, "calls": 400}
DEFAULT_MAX_TASKS = 3
KEEP_BEATS = 20            # how many past beats per schedule we remember
TICK_SECONDS = 30.0        # how often the thread looks at the schedules
# A beat whose run did not start keeps its slot (it is retried), but not
# sooner than this after the failed attempt, so a permanent failure never
# becomes a tight loop. Measured with the scheduler's own (injectable) clock.
START_RETRY_SECONDS = 300
REASON_CHARS = 240         # a recorded skip reason never grows past this
_TITLE_CHARS = 80

# stalled() reasons (taskboard.Board.STALL_REASONS) in plain words.
_STALL_WHY = {"missing": "a missing task", "self": "itself",
              "failed": "a failed task", "blocked": "a blocked task"}

_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_WEEKDAYS = [0, 1, 2, 3, 4]
_WEEKENDS = [5, 6]

_EVERY_RE = re.compile(r"^every\s+(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours)$")
_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


# --------------------------------------------------------------------------- #
# cron-lite: "every N min" | "every N hours" | "daily HH:MM" | "<days> HH:MM"
# --------------------------------------------------------------------------- #
def parse_when(when):
    """A schedule string -> a normalized dict, or None when it cannot be read.

    Forms (case/space-insensitive):
      "every 30 min" / "every 2 hours"      -> {"kind":"every","seconds":N}
      "daily 09:00"                         -> {"kind":"daily","hh":9,"mm":0}
      "weekdays 18:30"                      -> {"kind":"weekly","days":[0-4],...}
      "weekends 10:00"                      -> {"kind":"weekly","days":[5,6],...}
      "mon,wed,fri 07:15"                   -> {"kind":"weekly","days":[0,2,4],...}
    """
    if not isinstance(when, str):
        return None
    s = " ".join(when.strip().lower().split())
    if not s:
        return None
    m = _EVERY_RE.match(s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        secs = n * (3600 if unit.startswith("h") else 60)
        if secs <= 0:
            return None
        return {"kind": "every", "seconds": secs}
    head, _, tail = s.rpartition(" ")
    tm = _HHMM_RE.match(tail)
    if not head or not tm:
        return None
    hh, mm = int(tm.group(1)), int(tm.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    if head == "daily" or head == "every day":
        return {"kind": "daily", "hh": hh, "mm": mm}
    if head in ("weekday", "weekdays"):
        return {"kind": "weekly", "days": list(_WEEKDAYS), "hh": hh, "mm": mm}
    if head in ("weekend", "weekends"):
        return {"kind": "weekly", "days": list(_WEEKENDS), "hh": hh, "mm": mm}
    days = []
    for part in head.replace(" ", "").split(","):
        if part in _DAYS and _DAYS[part] not in days:
            days.append(_DAYS[part])
    if not days:
        return None
    return {"kind": "weekly", "days": sorted(days), "hh": hh, "mm": mm}


def _epoch_on(parsed, now, day_offset):
    """The epoch of HH:MM on the local day `day_offset` days from `now`."""
    base = time.localtime(now + day_offset * 86400)
    tup = (base.tm_year, base.tm_mon, base.tm_mday, parsed["hh"], parsed["mm"],
           0, 0, 0, -1)
    return time.mktime(time.struct_time(tup))


def due(parsed, now, last_beat_at):
    """Should this schedule fire at `now`? (parsed from parse_when.)"""
    if not parsed:
        return False
    if parsed["kind"] == "every":
        if last_beat_at is None:
            return True
        return (now - last_beat_at) >= parsed["seconds"]
    # daily / weekly: today's HH:MM slot, if it has come and not yet fired.
    slot = _epoch_on(parsed, now, 0)
    if now < slot:
        return False
    if parsed["kind"] == "weekly":
        if time.localtime(slot).tm_wday not in parsed["days"]:
            return False
    return last_beat_at is None or last_beat_at < slot


def next_due(parsed, now, last_beat_at):
    """The next time this schedule will fire (for display), or None."""
    if not parsed:
        return None
    if parsed["kind"] == "every":
        if last_beat_at is None:
            return now
        return last_beat_at + parsed["seconds"]
    for d in range(0, 8):
        slot = _epoch_on(parsed, now, d)
        if parsed["kind"] == "weekly" and \
                time.localtime(slot).tm_wday not in parsed["days"]:
            continue
        if slot <= now:
            if last_beat_at is None or last_beat_at < slot:
                return slot            # due now
            continue
        return slot
    return None


def in_quiet_hours(quiet, now):
    """True when `now` (local) falls inside the owner's quiet window.

    `quiet` is "HH:MM-HH:MM" or {"start":"HH:MM","end":"HH:MM"}; None/empty =
    never quiet. A window that wraps midnight (22:00-07:00) is handled."""
    if not quiet:
        return False
    try:
        if isinstance(quiet, dict):
            start, end = quiet.get("start"), quiet.get("end")
        else:
            start, end = str(quiet).split("-", 1)
        sm = _HHMM_RE.match((start or "").strip())
        em = _HHMM_RE.match((end or "").strip())
        if not sm or not em:
            return False
        s = int(sm.group(1)) * 60 + int(sm.group(2))
        e = int(em.group(1)) * 60 + int(em.group(2))
        lt = time.localtime(now)
        cur = lt.tm_hour * 60 + lt.tm_min
        if s == e:
            return False
        if s < e:
            return s <= cur < e
        return cur >= s or cur < e      # wraps midnight
    except Exception:                                            # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# schedule records (pure list transforms; the routes load/save the list)
# --------------------------------------------------------------------------- #
def normalize_schedule(spec, keep_id=None):
    """A user-supplied schedule dict -> a clean stored one, or None when it has
    neither a readable `when` nor a goal/project to act on."""
    if not isinstance(spec, dict):
        return None
    if parse_when(spec.get("when")) is None:
        return None
    goal_id = spec.get("goal_id") or None
    project = spec.get("project_dir") or spec.get("project") or None
    if not goal_id and not project:
        return None
    try:
        max_tasks = int(spec.get("max_tasks") or DEFAULT_MAX_TASKS)
    except (TypeError, ValueError):
        max_tasks = DEFAULT_MAX_TASKS
    max_tasks = max(1, min(50, max_tasks))
    out = {
        "id": str(keep_id or spec.get("id") or uuid.uuid4().hex[:12]),
        "when": " ".join(str(spec.get("when")).strip().lower().split()),
        "goal_id": str(goal_id) if goal_id else None,
        "project_dir": str(project) if project else None,
        "enabled": bool(spec.get("enabled", True)),
        "max_tasks": max_tasks,
        "cli": str(spec.get("cli") or "opencode"),
        "mode": (str(spec.get("mode")).strip() or None) if spec.get("mode") else None,
        "budget": merge_budget(spec.get("budget")),
    }
    quiet = spec.get("quiet")
    if quiet:
        out["quiet"] = quiet
    return out


def merge_budget(budget):
    """A schedule's budget merged over DEFAULT_BUDGET (heartbeat runs always
    carry one). A key explicitly set to None stays unlimited; an absent key
    takes the default."""
    out = dict(DEFAULT_BUDGET)
    if isinstance(budget, dict):
        for k in ("tokens", "seconds", "calls"):
            if k not in budget:
                continue
            v = budget[k]
            if v is None:
                out[k] = None
            else:
                try:
                    out[k] = max(0, int(v))
                except (TypeError, ValueError):
                    pass
    return out


def add(schedules, spec):
    """Append a normalized schedule; returns (new_list, schedule) or
    (list, None) when the spec was unusable."""
    sched = normalize_schedule(spec)
    if sched is None:
        return list(schedules or []), None
    return list(schedules or []) + [sched], sched


def update(schedules, sched_id, patch):
    """Merge `patch` into the schedule with `sched_id`; returns (list, sched)."""
    out, found = [], None
    for s in schedules or []:
        if isinstance(s, dict) and s.get("id") == sched_id:
            merged = dict(s)
            merged.update(patch or {})
            found = normalize_schedule(merged, keep_id=sched_id)
            out.append(found if found is not None else s)
        else:
            out.append(s)
    return out, found


def remove(schedules, sched_id):
    """Drop the schedule with `sched_id`; returns (list, removed?)."""
    out = [s for s in (schedules or [])
           if not (isinstance(s, dict) and s.get("id") == sched_id)]
    return out, len(out) != len(schedules or [])


def _task_id(task):
    if isinstance(task, dict):
        return task.get("id") or task.get("task_id")
    return task


def _task_text(task):
    if isinstance(task, dict):
        return (task.get("title") or task.get("text") or task.get("note")
                or str(task.get("id") or "")).strip()
    return str(task or "").strip()


def tasks_to_goal(tasks, sched=None):
    """The goal text a Multi run is started with from a batch of tasks."""
    lines = [t for t in (_task_text(x) for x in tasks or ()) if t]
    if not lines:
        return ""
    head = "Continue these open tasks:" if len(lines) > 1 else "Continue this open task:"
    body = "\n".join("- " + l for l in lines)
    return head + "\n" + body


def _short(text, limit):
    """One line, at most `limit` characters ("..." when cut)."""
    text = " ".join(str(text or "").split())
    if len(text) > limit:
        text = text[: max(0, limit - 3)].rstrip() + "..."
    return text


def stalled_reason(rows):
    """The skip reason for a beat that found no ready task while some todo
    tasks wait on a need that can never finish (taskboard.Board.stalled()).
    Names how many and the first one; bounded to REASON_CHARS. "" for none."""
    rows = [r for r in (rows or ()) if isinstance(r, dict)]
    if not rows:
        return ""
    first = rows[0]
    title = _short(first.get("title"), _TITLE_CHARS) \
        or _short(first.get("id"), 40) or "a task"
    why = _STALL_WHY.get(first.get("reason"), "a task that cannot finish")
    text = ("no ready tasks: %d task(s) wait on a failed, blocked or missing "
            "task, first: %s (waits on %s)" % (len(rows), title, why))
    return _short(text, REASON_CHARS)


def _retry_words(seconds):
    seconds = max(0, int(seconds))
    if seconds >= 60:
        return "%d min" % (seconds // 60)
    return "%d s" % seconds


def start_failed_reason(error=None):
    """The skip reason for a beat whose run did not start. `error` is at most
    an exception CLASS name (never its message: it can carry paths, URLs or
    keys). Bounded to REASON_CHARS."""
    name = re.sub(r"[^A-Za-z0-9_.]", "", str(error or ""))[:60]
    text = "could not start the run"
    if name:
        text += " (%s)" % name
    text += "; trying again in %s" % _retry_words(START_RETRY_SECONDS)
    return _short(text, REASON_CHARS)


def start_retry_at(st, now):
    """When a schedule whose last run failed to start may be tried again, or
    None when it is not waiting. A failure stamped AFTER `now` (the clock
    went back) is not waited out -- the next attempt re-stamps it."""
    failed = (st or {}).get("start_failed_at")
    if isinstance(failed, bool) or not isinstance(failed, (int, float)):
        return None
    if not (failed <= now < failed + START_RETRY_SECONDS):
        return None
    return failed + START_RETRY_SECONDS


# --------------------------------------------------------------------------- #
# the scheduler
# --------------------------------------------------------------------------- #
class Scheduler:
    """One daemon thread that, every TICK_SECONDS, fires the schedules whose
    time has come. Every side of it is injected so it is fully testable.

    Injected:
      now()               -> epoch seconds (the clock)
      load()              -> list of schedule dicts
      state_get()         -> the runtime-state dict {sched_id: {...}}
      state_set(dict)     -> persist it
      enabled()           -> the master kill switch (heartbeats_enabled)
      busy(sched)         -> True when the owner is working (skip)
      ram_ok()            -> False when the machine has no room (skip)
      providers_ok()      -> False when the providers are rate-limited (skip)
      taskboard()         -> taskboard.default (or a fake)
      start_run(sched, goal_text, tasks, budget) -> run_id | None
    """

    KEEP_BEATS = KEEP_BEATS

    def __init__(self, *, now=time.time, load=None, state_get=None, state_set=None,
                 enabled=None, busy=None, ram_ok=None, providers_ok=None,
                 taskboard=None, start_run=None, tick_seconds=TICK_SECONDS,
                 logger=log):
        self._now = now
        self._load = load or (lambda: [])
        self._state_get = state_get or (lambda: {})
        self._state_set = state_set or (lambda _s: None)
        self._enabled = enabled or (lambda: False)
        self._busy = busy or (lambda _s: False)
        self._ram_ok = ram_ok or (lambda: True)
        self._providers_ok = providers_ok or (lambda: True)
        self._taskboard = taskboard or (lambda: _lazy_taskboard())
        self._start_run = start_run or (lambda *a, **k: None)
        self.tick_seconds = tick_seconds
        self.log = logger
        self._inflight = set()
        self._stop = threading.Event()
        self._thread = None

    # -- lifecycle ---------------------------------------------------------- #
    def start(self):
        """Start the daemon thread (idempotent). NO immediate beat -- boot only
        starts the clock; the first beat waits for a schedule to come due."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="heartbeat-scheduler")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        # Wait FIRST, then tick: boot starts the clock, it does not force a beat
        # the instant the hub comes up (owner: "start the thread only").
        while not self._stop.is_set():
            self._stop.wait(self.tick_seconds)
            if self._stop.is_set():
                break
            try:
                self.tick()
            except Exception as exc:                             # noqa: BLE001
                self.log.warning("[heartbeat] tick failed: %s", exc)

    # -- one look at the schedules ------------------------------------------ #
    def tick(self, now=None):
        """Fire every schedule whose time has come. Returns a list of
        ("beat"|"skip", sched_id, info) for the caller/tests."""
        now = self._now() if now is None else now
        out = []
        if not self._enabled():
            return out
        schedules = self._load() or []
        state = self._state_get() or {}
        changed = False
        for sched in schedules:
            if not isinstance(sched, dict):
                continue
            sid = sched.get("id")
            if not sid or not sched.get("enabled", True):
                continue
            parsed = parse_when(sched.get("when"))
            if parsed is None:
                continue
            st = state.setdefault(sid, {})
            if not due(parsed, now, st.get("last_beat_at")):
                continue
            if sid in self._inflight:                 # a beat of it is running
                continue
            if start_retry_at(st, now) is not None:   # its run just failed to start
                continue
            reason = self._skip_reason(sched, now)
            tasks = None
            if reason is None:
                tasks, stalled = self._read_board(sched)
                if not tasks:
                    reason = stalled_reason(stalled) or "no tasks"
            if reason is not None:
                st["last_skip"] = {"at": now, "reason": reason}
                out.append(("skip", sid, reason))
                changed = True
                continue
            beat = self._do_beat(sched, tasks, now)
            if not beat.get("run_id"):
                # The run did not start: keep the slot (last_beat_at untouched,
                # so it stays due) and wait START_RETRY_SECONDS before trying.
                reason = start_failed_reason(beat.get("error"))
                st["start_failed_at"] = now
                st["last_skip"] = {"at": now, "reason": reason}
                out.append(("skip", sid, reason))
                changed = True
                continue
            st.pop("start_failed_at", None)
            st["last_beat_at"] = now
            st["last_run_id"] = beat.get("run_id")
            st["last_result"] = beat.get("result")
            st["beats"] = (st.get("beats") or [])[-(self.KEEP_BEATS - 1):] + [beat]
            st.pop("last_skip", None)
            out.append(("beat", sid, beat))
            changed = True
        if changed:
            try:
                self._state_set(state)
            except Exception as exc:                             # noqa: BLE001
                self.log.warning("[heartbeat] could not persist state: %s", exc)
        return out

    def _skip_reason(self, sched, now):
        try:
            if in_quiet_hours(sched.get("quiet"), now):
                return "quiet hours"
            if self._busy(sched):
                return "owner busy"
            if not self._ram_ok():
                return "no RAM room"
            if not self._providers_ok():
                return "providers rate-limited"
        except Exception as exc:                                 # noqa: BLE001
            self.log.warning("[heartbeat] skip check failed: %s", exc)
            return "check failed"
        return None

    @staticmethod
    def _goal_id(tb, sched):
        goal_id = sched.get("goal_id")
        if not goal_id and sched.get("project_dir"):
            goal = tb.goal_for(sched["project_dir"])
            goal_id = goal.get("id") if isinstance(goal, dict) else goal
        return goal_id or None

    def _collect_tasks(self, sched):
        return self._read_board(sched)[0]

    def _read_board(self, sched):
        """(ready tasks, stalled rows). The stalled rows -- todo tasks waiting
        on a need that can never finish (Board.stalled) -- are read only when
        no task is ready; [] when the board has no stalled() or it fails."""
        try:
            tb = self._taskboard()
            if tb is None:
                return [], []
            goal_id = self._goal_id(tb, sched)
            if not goal_id:
                return [], []
            batch = tb.next_batch(goal_id, sched.get("max_tasks") or DEFAULT_MAX_TASKS)
            batch = list(batch or [])
        except Exception as exc:                                 # noqa: BLE001
            self.log.warning("[heartbeat] could not read tasks: %s", exc)
            return [], []
        if batch:
            return batch, []
        return batch, self._board_stalled(tb, goal_id) or []

    def _board_stalled(self, tb, goal_id):
        """Board.stalled(goal_id) as a list of dicts, or None when the board
        has no such method or it fails. Never raises."""
        try:
            fn = getattr(tb, "stalled", None)
            if not callable(fn):
                return None
            return [r for r in (fn(goal_id) or ()) if isinstance(r, dict)]
        except Exception as exc:                                 # noqa: BLE001
            self.log.warning("[heartbeat] could not read stalled tasks: %s", exc)
            return None

    def _stalled_count(self, sched):
        """How many todo tasks of the schedule's goal wait on a need that can
        never finish, for the status rows; None when that cannot be told."""
        try:
            tb = self._taskboard()
            if tb is None or not callable(getattr(tb, "stalled", None)):
                return None
            goal_id = self._goal_id(tb, sched)
            if not goal_id:
                return 0
            rows = self._board_stalled(tb, goal_id)
            return None if rows is None else len(rows)
        except Exception:                                        # noqa: BLE001
            return None

    def _do_beat(self, sched, tasks, now):
        sid = sched.get("id")
        budget = merge_budget(sched.get("budget"))
        goal_text = tasks_to_goal(tasks, sched)
        self._inflight.add(sid)
        run_id = None
        error = None
        try:
            run_id = self._start_run(sched, goal_text, tasks, budget)
        except Exception as exc:                                 # noqa: BLE001
            error = type(exc).__name__
            self.log.warning("[heartbeat] beat of %s could not start: %s", sid, exc)
        finally:
            self._inflight.discard(sid)
        ids = [i for i in (_task_id(t) for t in tasks) if i is not None]
        if run_id:
            self._mark_tasks(tasks, run_id, sid)
            result = "started"
        else:
            result = "could not start"
        self.log.info("[heartbeat] beat %s: %s (%d task%s) -> %s",
                      sid, result, len(ids), "" if len(ids) == 1 else "s", run_id)
        beat = {"at": now, "tasks": ids, "run_id": run_id, "result": result}
        if error:                       # only a failed start carries it
            beat["error"] = error
        return beat

    def _mark_tasks(self, tasks, run_id, sid):
        try:
            tb = self._taskboard()
            if tb is None:
                return
            owner = "heartbeat:%s" % sid
            for t in tasks:
                tid = _task_id(t)
                if tid is None:
                    continue
                try:
                    tb.update(tid, status="doing", owner=owner, run_id=run_id)
                except Exception:                                # noqa: BLE001
                    pass
        except Exception as exc:                                 # noqa: BLE001
            self.log.warning("[heartbeat] could not mark tasks doing: %s", exc)

    # -- status for the route ---------------------------------------------- #
    def status(self, now=None):
        """One row per schedule: its when, enabled, the next time it is due,
        its last beat and its last skip reason."""
        now = self._now() if now is None else now
        state = self._state_get() or {}
        rows = []
        for sched in self._load() or []:
            if not isinstance(sched, dict):
                continue
            sid = sched.get("id")
            st = state.get(sid) or {}
            parsed = parse_when(sched.get("when"))
            nd = next_due(parsed, now, st.get("last_beat_at"))
            retry = start_retry_at(st, now)
            if nd is not None and retry is not None and retry > nd:
                nd = retry                    # waiting after a run that did not start
            rows.append({
                "id": sid,
                "when": sched.get("when"),
                "enabled": bool(sched.get("enabled", True)),
                "goal_id": sched.get("goal_id"),
                "project_dir": sched.get("project_dir"),
                "max_tasks": sched.get("max_tasks"),
                "budget": merge_budget(sched.get("budget")),
                "valid": parsed is not None,
                "next_due": nd,
                "last_beat_at": st.get("last_beat_at"),
                "last_run_id": st.get("last_run_id"),
                "last_result": st.get("last_result"),
                "last_skip": st.get("last_skip"),
                "beats": list(st.get("beats") or [])[-5:],
                # todo tasks of its goal waiting on a need that can never
                # finish (None = the board cannot tell)
                "stalled": self._stalled_count(sched),
            })
        return {"enabled": bool(self._enabled()), "schedules": rows}


def _lazy_taskboard():
    """taskboard.default, imported only when a beat needs it (agent T ships
    taskboard.py in parallel). None when it is not available yet."""
    try:
        import taskboard
        return taskboard.default
    except Exception:                                            # noqa: BLE001
        return None
