# -*- coding: utf-8 -*-
"""Measured per-(provider, model) performance that SURVIVES A RESTART.

app.py already learns which hops actually deliver (_record_outcome /
_reliability, see the long note above them) and demotes the ones that
repeatedly do not. That learning lived in a plain in-process dict, so every
restart threw it away -- and this hub restarts on every auto-update (5h) and
every reboot. The signal therefore never accumulated past a few hours of
evidence, and ranking fell back almost entirely on a hand-typed capability
table that nobody re-dates.

This module gives that learning somewhere to live, and adds the second half of
"rank on what actually happened": measured LATENCY.

Two deliberate limits, both inherited from the reliability design they extend:

  - PENALTY-ONLY. A good record earns no bonus. Promoting a mediocre-but-quick
    model over a genuinely stronger one is a real regression, and speed is not
    quality. These signals can only demote a hop that has actually misbehaved.
  - NEUTRAL WHEN UNKNOWN. A (pid, model) with no measurements scores exactly as
    it did before this file existed, so adding it cannot reorder anything the
    hub has never actually run.

Pure stdlib, and it never raises: a corrupt, missing or unwritable file
degrades to "no measurements", which is precisely the neutral state every
caller already handles.
"""
import json
import os
import threading
import time

import config

# Match the reliability semantics in app.py so the two halves decay together.
TTL = 7 * 86400            # forget a pair untouched for a week
SAVE_INTERVAL = 60.0       # seconds between writes; the hot path never blocks on IO

_FILE = "perf-stats.json"
_SCHEMA = 1

_lock = threading.Lock()
_last_save = [0.0]


def _path():
    return os.path.join(config.state_dir(), _FILE)


def _fresh(row, now):
    return (now - float(row.get("last") or 0)) <= TTL


def load():
    """(outcomes, latency) read back from disk, stale pairs already dropped.

    outcomes: {(pid, model): {"ok": int, "fail": int, "last": float}}
              -- the exact shape app.py's _outcomes already uses, so it can be
              loaded straight into it with no translation at the call site.
    latency:  {(pid, model): {"ms": float, "n": int, "last": float}}

    Returns two empty dicts on ANY problem. A hub that starts with no history
    behaves exactly as it did before this file existed, which is the whole
    point of the neutral-when-unknown rule."""
    outcomes, latency = {}, {}
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            blob = json.load(f)
        if not isinstance(blob, dict) or blob.get("schema") != _SCHEMA:
            return {}, {}
        now = time.time()
        for row in blob.get("pairs") or []:
            if not isinstance(row, dict):
                continue
            pid, model = row.get("pid"), row.get("model")
            if not (isinstance(pid, str) and isinstance(model, str) and pid and model):
                continue
            if not _fresh(row, now):
                continue
            key = (pid, model)
            ok, fail = int(row.get("ok") or 0), int(row.get("fail") or 0)
            last = float(row.get("last") or 0)
            if ok or fail:
                outcomes[key] = {"ok": ok, "fail": fail, "last": last}
            ms, n = float(row.get("ms") or 0), int(row.get("n") or 0)
            if ms > 0 and n > 0:
                latency[key] = {"ms": ms, "n": n, "last": last}
    except Exception:                                            # noqa: BLE001
        return {}, {}
    return outcomes, latency


def save(outcomes, latency, force=False):
    """Persist both halves, throttled to SAVE_INTERVAL unless `force`.

    Written to a temp file then os.replace()d, so a crash mid-write can never
    leave a half-written file behind for the next boot to choke on. Returns
    True if it actually wrote. Never raises -- losing a save costs at most the
    last minute of learning, which must never take a request down with it."""
    now = time.time()
    with _lock:
        if not force and (now - _last_save[0]) < SAVE_INTERVAL:
            return False
        _last_save[0] = now
    try:
        keys = set(outcomes or {}) | set(latency or {})
        pairs = []
        for pid, model in keys:
            o = (outcomes or {}).get((pid, model)) or {}
            lat = (latency or {}).get((pid, model)) or {}
            last = max(float(o.get("last") or 0), float(lat.get("last") or 0))
            if (now - last) > TTL:
                continue
            row = {"pid": pid, "model": model, "last": round(last, 3)}
            if o.get("ok") or o.get("fail"):
                row["ok"] = int(o.get("ok") or 0)
                row["fail"] = int(o.get("fail") or 0)
            if lat.get("n"):
                row["ms"] = round(float(lat.get("ms") or 0), 1)
                row["n"] = int(lat.get("n") or 0)
            pairs.append(row)
        path = _path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"schema": _SCHEMA, "saved": round(now, 3), "pairs": pairs}, f)
        os.replace(tmp, path)
        return True
    except Exception:                                            # noqa: BLE001
        return False
