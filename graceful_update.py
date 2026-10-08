"""Graceful updates: drain, restart, continue (pure helpers).

An update used to wait for the work that was running and then restart, but a
hub that keeps receiving work could starve the restart forever, and the jobs a
restart cut did not continue by themselves. This module is the pure half of the
fix (stdlib only, no import from app/config/swarm_windows, every side injected,
never raises where a caller could not recover):

  * ``Drain`` -- the "draining for update" state. While it is active the hub
    refuses NEW work with a 503 + Retry-After and lets what is already running
    finish. It never decides what is busy; app.py hands it the snapshot.
  * the resume marker -- ``state_dir()/update-resume.json``, written
    atomically just before the re-exec and read back (once, if fresh) by the
    next boot, which then continues exactly the conversations it names.
  * ``ResumePlan`` -- what a boot does with a fresh marker.

app.py owns every decision about the live process (what is busy, when to
restart, which conversations still exist); this file only holds the shapes, so
they can be tested with a fake clock and a temp directory.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time

MARKER_NAME = "update-resume.json"
MARKER_VERSION = 1
# A marker older than this belongs to a restart nobody is waiting on any more.
MARKER_MAX_AGE = 15 * 60.0
# Longest the hub waits for running work before it restarts anyway (the resume
# marker is what makes that safe). Setting `update_drain_max_seconds`.
DEFAULT_DRAIN_MAX = 600.0
# What a refused caller is told to wait. Short enough that a client which
# honours it again and again reaches the new hub soon after it is back, long
# enough not to hammer a hub that is busy finishing.
RETRY_AFTER_MIN = 5
RETRY_AFTER_MAX = 30


def short_label(value, size: int = 7) -> str:
    """The short form of a commit hash / manifest fingerprint, for messages."""
    text = str(value or "").strip()
    return text[:size] if text else ""


# --------------------------------------------------------------------------- #
# The draining state
# --------------------------------------------------------------------------- #

class Drain:
    """Thread-safe "the hub is draining for an update" state.

    ``begin`` records which sessions and runs were busy when the drain started:
    a request that belongs to one of them (``admits``) is the running work
    itself asking for its next model call and must be served, everything else
    is new work and is refused. ``clock`` is injectable for tests."""

    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._state = None

    # -- lifecycle ---------------------------------------------------------
    def begin(self, to_label, from_label="", max_seconds=DEFAULT_DRAIN_MAX,
              sessions=(), runs=(), reason="update") -> bool:
        """Start draining. False (and nothing changes) when a drain is already
        running: the first drain's snapshot and deadline stay."""
        try:
            limit = float(max_seconds)
        except (TypeError, ValueError):
            limit = DEFAULT_DRAIN_MAX
        if limit <= 0:
            limit = DEFAULT_DRAIN_MAX
        now = self._clock()
        with self._lock:
            if self._state is not None:
                return False
            self._state = {
                "to": short_label(to_label), "from": short_label(from_label),
                "reason": str(reason or "update"), "started": now,
                "deadline": now + limit, "max_seconds": limit,
                "sessions": frozenset(str(s) for s in (sessions or ())),
                "runs": frozenset(str(r) for r in (runs or ())),
            }
        return True

    def end(self) -> None:
        with self._lock:
            self._state = None

    def active(self) -> bool:
        with self._lock:
            return self._state is not None

    # -- reads -------------------------------------------------------------
    def snapshot(self):
        with self._lock:
            return dict(self._state) if self._state is not None else None

    def seconds_left(self) -> float:
        with self._lock:
            st = self._state
        return max(0.0, st["deadline"] - self._clock()) if st else 0.0

    def expired(self) -> bool:
        with self._lock:
            st = self._state
        return bool(st) and self._clock() >= st["deadline"]

    def retry_after(self) -> int:
        """Whole seconds a refused caller should wait: what is left of the
        drain, kept inside [RETRY_AFTER_MIN, RETRY_AFTER_MAX]."""
        left = self.seconds_left()
        return int(min(RETRY_AFTER_MAX, max(RETRY_AFTER_MIN, math.ceil(left))))

    def admits(self, session_id, run_of=None) -> bool:
        """Whether a request from `session_id` is running work (served) rather
        than new work (refused). `run_of(sid)` maps a worker session to its run
        id so a worker started after the snapshot still counts as its run's."""
        with self._lock:
            st = self._state
        if not st or not session_id:
            return False
        sid = str(session_id)
        if sid in st["sessions"]:
            return True
        if run_of is not None:
            try:
                rid = run_of(sid)
            except Exception:                                    # noqa: BLE001
                rid = None
            if rid and str(rid) in st["runs"]:
                return True
        return False

    def info(self, busy=None):
        """The dashboard's view of the drain, or None when not draining."""
        st = self.snapshot()
        if st is None:
            return None
        return {"to": st["to"], "from": st["from"], "reason": st["reason"],
                "retry_after": self.retry_after(),
                "deadline_in": int(math.ceil(self.seconds_left())),
                "max_seconds": int(st["max_seconds"]),
                "busy": int(busy) if busy is not None else None}


def refusal_message(to_label, retry_after, reason="update") -> str:
    """The plain sentence every protocol's error carries."""
    n = int(retry_after)
    if reason == "update" and to_label:
        return "The hub is updating to %s; retry in %d s." % (to_label, n)
    return "The hub is restarting; retry in %d s." % n


# --------------------------------------------------------------------------- #
# The resume marker
# --------------------------------------------------------------------------- #

def marker_path(state_dir) -> str:
    return os.path.join(str(state_dir), MARKER_NAME)


def build_marker(from_label, to_label, now, state_dir, sessions=(), runs=(),
                 reason="update") -> dict:
    """The marker body. `runs` = [{"run_id", "owner"}] (owner may be None);
    `sessions` = ids of conversations whose single turn was running."""
    run_rows = []
    for r in runs or ():
        if isinstance(r, dict) and r.get("run_id"):
            run_rows.append({"run_id": str(r["run_id"]),
                             "owner": str(r["owner"]) if r.get("owner") else None})
    return {"v": MARKER_VERSION, "reason": str(reason or "update"),
            "from": short_label(from_label), "to": short_label(to_label),
            "written_at": float(now),
            "state_dir": os.path.abspath(str(state_dir)),
            "sessions": sorted({str(s) for s in (sessions or ()) if s}),
            "runs": run_rows}


def has_work(marker) -> bool:
    return bool(isinstance(marker, dict)
                and (marker.get("sessions") or marker.get("runs")))


def write_marker(state_dir, marker):
    """Atomically write the marker (temp file in the same folder, fsync,
    os.replace). Returns the path, or None when it could not be written -- a
    restart must never be blocked by its own bookkeeping."""
    try:
        os.makedirs(str(state_dir), exist_ok=True)
        path = marker_path(state_dir)
        fd, tmp = tempfile.mkstemp(prefix=".update-resume-", suffix=".tmp",
                                   dir=str(state_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(marker, fh)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
            os.replace(tmp, path)
        except Exception:                                        # noqa: BLE001
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return path
    except Exception:                                            # noqa: BLE001
        return None


def clear_marker(state_dir) -> None:
    try:
        os.unlink(marker_path(state_dir))
    except OSError:
        pass


def read_marker(state_dir, now, max_age=MARKER_MAX_AGE):
    """A FRESH, well-formed marker written for THIS state dir, else None.
    A stale, corrupt, foreign or empty marker is removed and ignored."""
    path = marker_path(state_dir)
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        clear_marker(state_dir)
        return None
    ok = isinstance(raw, dict) and raw.get("v") == MARKER_VERSION
    stamp = None
    if ok:
        try:
            stamp = float(raw.get("written_at"))
        except (TypeError, ValueError):
            ok = False
    if ok:
        age = float(now) - stamp
        ok = (age <= float(max_age)) and (age >= -60.0)
    if ok:
        ok = (os.path.normcase(os.path.abspath(str(raw.get("state_dir") or "")))
              == os.path.normcase(os.path.abspath(str(state_dir))))
    if ok:
        ok = isinstance(raw.get("sessions"), list) and isinstance(raw.get("runs"), list)
    if not ok or not has_work(raw):
        clear_marker(state_dir)
        return None
    return raw


class ResumePlan:
    """What one boot continues after an update: exactly the conversations and
    runs the marker names. ``finish(part)`` for each consumer ("turns",
    "runs"); the marker file goes away once all of them ran."""

    PARTS = ("turns", "runs")

    def __init__(self, marker, state_dir):
        self.state_dir = str(state_dir)
        self.reason = str(marker.get("reason") or "update")
        self.from_label = short_label(marker.get("from"))
        self.to_label = short_label(marker.get("to"))
        self.sessions = frozenset(str(s) for s in marker.get("sessions") or ())
        self.run_ids = frozenset(str(r.get("run_id")) for r in marker.get("runs") or ()
                                 if isinstance(r, dict) and r.get("run_id"))
        self.owners = frozenset(str(r.get("owner")) for r in marker.get("runs") or ()
                                if isinstance(r, dict) and r.get("owner"))
        self._left = set(self.PARTS)
        self._lock = threading.Lock()

    def wants_session(self, session_id) -> bool:
        return str(session_id) in self.sessions

    def wants_run(self, run_id, owner=None) -> bool:
        return (str(run_id) in self.run_ids
                or bool(owner and str(owner) in self.owners))

    def notice(self) -> str:
        if self.reason == "update" and self.to_label and self.to_label != self.from_label:
            return "Continued automatically after the update to %s." % self.to_label
        return "Continued automatically after the restart."

    def finish(self, part) -> bool:
        """Mark one consumer done; True when that was the last one (the marker
        file is removed then)."""
        with self._lock:
            self._left.discard(part)
            last = not self._left
        if last:
            clear_marker(self.state_dir)
        return last
