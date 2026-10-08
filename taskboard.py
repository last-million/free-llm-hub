"""Task board: the GOAL behind every task, and a persistent list of tasks
agents pick work from -- the same board in any terminal CLI and in the Build
page.

Owner goal (2026-10-07, inspired by Paperclip's goal alignment + task board):
"goal behind every task" across runs and sessions, and a PERSISTENT task board
agents pick work from.

A GOAL is one durable outcome for a project folder (newest open goal wins).
A TASK belongs to a goal, carries dependencies (`needs`, other task ids),
owned files, a priority and a status in STATUSES. The board is a flat JSON
file under the hub's own state folder -- one lock, atomic replace, and every
read fails open to "nothing" rather than raising, because a corrupt board must
never cost a turn.

Pure: stdlib only, no import from app/swarm/config. `configure(path)` points
the module-level `default` board at a file; until then it lives in memory.
"""
import json
import os
import threading
import time
import uuid

STATUSES = ("todo", "doing", "done", "blocked", "failed")
# A goal is open until it is closed; its tasks carry the STATUSES above.
_GOAL_OPEN = "open"
_GOAL_CLOSED = "closed"

SCHEMA = 1


def _norm_dir(path):
    """A project folder in a form two spellings of the same folder share
    (absolute, case-folded on Windows). None/"" -> None. Never raises."""
    if not path:
        return None
    try:
        return os.path.normcase(os.path.abspath(str(path)))
    except Exception:                                            # noqa: BLE001
        return str(path)


def _clip(text, limit):
    text = " ".join(str(text or "").split())
    if limit and len(text) > limit:
        return text[: max(0, limit - 1)].rstrip() + "…"
    return text


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        return [str(value)]
    try:
        return [str(x) for x in value if str(x).strip()]
    except TypeError:
        return [str(value)]


class Board:
    """A goal/task board backed by one JSON file (or memory when path is
    None). Thread-safe (one reentrant lock); every public method is total --
    an unknown id returns None/False/[] rather than raising."""

    def __init__(self, path=None, clock=time.time):
        self.path = path or None
        self._clock = clock if callable(clock) else time.time
        self._lock = threading.RLock()
        self._goals = {}        # goal_id -> dict
        self._tasks = {}        # task_id -> dict
        if self.path:
            self.load()

    # ------------------------------------------------------------------ #
    # goals
    # ------------------------------------------------------------------ #

    def add_goal(self, title, project_dir=None, detail="") -> str:
        """Create a goal and return its id. Empty title is tolerated (the
        board never raises); it is stored as "(untitled goal)"."""
        gid = "g-" + uuid.uuid4().hex[:12]
        now = self._now()
        with self._lock:
            self._goals[gid] = {
                "id": gid,
                "title": _clip(title, 200) or "(untitled goal)",
                "detail": _clip(detail, 600),
                "project_dir": str(project_dir) if project_dir else None,
                "status": _GOAL_OPEN,
                "created_at": now,
                "closed_at": None,
            }
            self._save_locked()
        return gid

    def goals(self, project_dir=None, include_closed=False) -> list:
        """Goals, newest first. Filtered to one project folder when given,
        and to open goals unless `include_closed`."""
        want = _norm_dir(project_dir)
        with self._lock:
            out = []
            for g in self._goals.values():
                if not include_closed and g.get("status") != _GOAL_OPEN:
                    continue
                if want is not None and _norm_dir(g.get("project_dir")) != want:
                    continue
                out.append(dict(g))
        out.sort(key=lambda g: g.get("created_at") or 0, reverse=True)
        return out

    def close_goal(self, goal_id) -> bool:
        with self._lock:
            g = self._goals.get(goal_id)
            if not g or g.get("status") == _GOAL_CLOSED:
                return False
            g["status"] = _GOAL_CLOSED
            g["closed_at"] = self._now()
            self._save_locked()
        return True

    def goal_for(self, project_dir):
        """The active (newest open) goal of a project folder, or None."""
        gs = self.goals(project_dir=project_dir, include_closed=False)
        return gs[0] if gs else None

    # ------------------------------------------------------------------ #
    # tasks
    # ------------------------------------------------------------------ #

    def add_task(self, goal_id, title, detail="", needs=(), files=(),
                 priority=0, run_id=None, phase=None) -> str:
        """Create a task under a goal and return its id. A task under an
        unknown goal is still created (goal_id kept as given) so a caller is
        never left without an id; callers that care check goals() first.

        `run_id` / `phase` (optional) record which Multi run phase the task
        was made for, so a resumed run finds the SAME task again."""
        tid = "t-" + uuid.uuid4().hex[:12]
        now = self._now()
        try:
            prio = int(priority)
        except (TypeError, ValueError):
            prio = 0
        try:
            phase = int(phase) if phase is not None else None
        except (TypeError, ValueError):
            phase = None
        with self._lock:
            self._tasks[tid] = {
                "id": tid,
                "goal_id": goal_id,
                "title": _clip(title, 200) or "(untitled task)",
                "detail": _clip(detail, 600),
                "needs": _as_list(needs),
                "files": _as_list(files),
                "priority": prio,
                "status": "todo",
                "owner": None,
                "run_id": run_id or None,
                "phase": phase,
                "created_at": now,
                "updated_at": now,
                "history": [],
            }
            self._save_locked()
        return tid

    def update(self, task_id, status=None, note=None, owner=None,
               run_id=None):
        """Change a task's status/owner/run_id and append a note to its
        history. Returns the updated task (a copy) or None for an unknown id.
        An unknown status is ignored (the rest still applies)."""
        with self._lock:
            t = self._tasks.get(task_id)
            if not t:
                return None
            now = self._now()
            changed = False
            if status is not None and status in STATUSES:
                if t.get("status") != status:
                    changed = True
                t["status"] = status
            if owner is not None:
                if t.get("owner") != owner:
                    changed = True
                t["owner"] = owner
            if run_id is not None:
                if t.get("run_id") != run_id:
                    changed = True
                t["run_id"] = run_id
            note = _clip(note, 400) if note else ""
            if changed or note:
                t["updated_at"] = now
                t["history"].append({
                    "at": now,
                    "status": t.get("status"),
                    "owner": t.get("owner"),
                    "run_id": t.get("run_id"),
                    "note": note,
                })
                # Bound the history so a long-running task cannot grow the file
                # without limit.
                if len(t["history"]) > 100:
                    t["history"] = t["history"][-100:]
                self._save_locked()
            return dict(t)

    def tasks(self, goal_id=None, project_dir=None, status=None) -> list:
        """Tasks for one goal, or for every open/closed goal of a project
        folder, filtered by status when given. Oldest first (plan order)."""
        goal_ids = None
        if goal_id is not None:
            goal_ids = {goal_id}
        elif project_dir is not None:
            want = _norm_dir(project_dir)
            with self._lock:
                goal_ids = {g["id"] for g in self._goals.values()
                            if _norm_dir(g.get("project_dir")) == want}
        with self._lock:
            out = []
            for t in self._tasks.values():
                if goal_ids is not None and t.get("goal_id") not in goal_ids:
                    continue
                if status is not None and t.get("status") != status:
                    continue
                out.append(dict(t))
        out.sort(key=lambda t: t.get("created_at") or 0)
        return out

    def next_batch(self, goal_id, n) -> list:
        """The todo tasks of a goal whose dependencies are all done, by
        priority (highest first) then age (oldest first), capped at n."""
        try:
            n = int(n)
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            return []
        with self._lock:
            mine = [t for t in self._tasks.values()
                    if t.get("goal_id") == goal_id]
            done = {t["id"] for t in mine if t.get("status") == "done"}
            ready = []
            for t in mine:
                if t.get("status") != "todo":
                    continue
                needs = t.get("needs") or ()
                if all(dep in done for dep in needs):
                    ready.append(dict(t))
        ready.sort(key=lambda t: (-(t.get("priority") or 0),
                                  t.get("created_at") or 0))
        return ready[:n]

    # ------------------------------------------------------------------ #
    # the goal brief every prompt carries
    # ------------------------------------------------------------------ #

    def goal_brief(self, goal_id=None, project_dir=None, max_chars=600) -> str:
        """A bounded block naming the goal behind the work and its still-open
        tasks: "GOAL: ...\\nOPEN TASKS: ...". "" when there is no goal.

        Resolution: an explicit goal_id, else the active goal of project_dir,
        else the newest open goal on the board."""
        g = None
        if goal_id is not None:
            with self._lock:
                g = self._goals.get(goal_id)
                g = dict(g) if g else None
        if g is None and project_dir is not None:
            g = self.goal_for(project_dir)
        if g is None and goal_id is None and project_dir is None:
            allg = self.goals(include_closed=False)
            g = allg[0] if allg else None
        if not g:
            return ""
        try:
            max_chars = int(max_chars)
        except (TypeError, ValueError):
            max_chars = 600
        head = "GOAL: " + (g.get("title") or "")
        if g.get("detail"):
            head += " — " + g["detail"]
        open_tasks = [t for t in self.tasks(goal_id=g["id"])
                      if t.get("status") in ("todo", "doing", "blocked")]
        lines = [head]
        if open_tasks:
            parts = []
            for t in open_tasks:
                tag = "" if t["status"] == "todo" else " [%s]" % t["status"]
                parts.append(t["title"] + tag)
            lines.append("OPEN TASKS: " + "; ".join(parts))
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[: max(0, max_chars - 1)].rstrip() + "…"
        return text

    # ------------------------------------------------------------------ #
    # persistence (atomic; never raises)
    # ------------------------------------------------------------------ #

    def _now(self):
        try:
            return float(self._clock())
        except Exception:                                        # noqa: BLE001
            return time.time()

    def _save_locked(self):
        if not self.path:
            return False
        data = {
            "version": SCHEMA,
            "saved_at": self._now(),
            "goals": list(self._goals.values()),
            "tasks": list(self._tasks.values()),
        }
        path = self.path
        tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
        try:
            folder = os.path.dirname(os.path.abspath(path))
            if folder:
                os.makedirs(folder, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, separators=(",", ":"))
            os.replace(tmp, path)
            return True
        except Exception:                                        # noqa: BLE001
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False

    def load(self) -> bool:
        """Replace the board with the file at `path`. A missing, unreadable or
        corrupt file leaves an empty board; bad rows are skipped. Returns
        whether the file was read; never raises."""
        goals, tasks, ok = {}, {}, False
        if self.path:
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    for g in data.get("goals") or ():
                        if isinstance(g, dict) and g.get("id"):
                            goals[g["id"]] = self._clean_goal(g)
                    for t in data.get("tasks") or ():
                        if isinstance(t, dict) and t.get("id"):
                            tasks[t["id"]] = self._clean_task(t)
                    ok = True
            except Exception:                                    # noqa: BLE001
                goals, tasks, ok = {}, {}, False
        with self._lock:
            self._goals = goals
            self._tasks = tasks
        return ok

    @staticmethod
    def _clean_goal(g):
        return {
            "id": g["id"],
            "title": str(g.get("title") or "(untitled goal)"),
            "detail": str(g.get("detail") or ""),
            "project_dir": g.get("project_dir") or None,
            "status": _GOAL_CLOSED if g.get("status") == _GOAL_CLOSED else _GOAL_OPEN,
            "created_at": g.get("created_at") or 0,
            "closed_at": g.get("closed_at"),
        }

    @staticmethod
    def _clean_task(t):
        status = t.get("status")
        return {
            "id": t["id"],
            "goal_id": t.get("goal_id"),
            "title": str(t.get("title") or "(untitled task)"),
            "detail": str(t.get("detail") or ""),
            "needs": _as_list(t.get("needs")),
            "files": _as_list(t.get("files")),
            "priority": int(t.get("priority") or 0) if str(t.get("priority") or 0).lstrip("-").isdigit() else 0,
            "status": status if status in STATUSES else "todo",
            "owner": t.get("owner"),
            "run_id": t.get("run_id"),
            "phase": t.get("phase") if isinstance(t.get("phase"), int) else None,
            "created_at": t.get("created_at") or 0,
            "updated_at": t.get("updated_at") or t.get("created_at") or 0,
            "history": list(t.get("history") or []),
        }


default = Board()


def configure(path) -> None:
    """Point `default` at a json path and load it (None = memory only)."""
    with default._lock:
        default.path = path or None
        default.load()
