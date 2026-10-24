# -*- coding: utf-8 -*-
"""The hub's LEARNED SLOWNESS / HEALTH state, persisted across restarts.

Owner 2026-10-24: "of course the hub should remember the slowness -- it should
be done with code." MEASURED the same day: a roles turn re-discovered that
nvidia GLM-5.3 is silent for 30 s by failing on it AGAIN, because what the hub
learns about slow first-content times, rested pairs and recent failures lived
in plain in-process dicts. This hub reboots on every auto-update and every
machine restart, so that learning never outlived an hour.

`perfstats.py` already persists the two numeric ledgers (_outcomes / _latencies)
that feed `_reliability`. This module is its sibling for the SHORT-LIVED health
signals the router reads every turn -- first-content latency on tool turns,
rested/benched pairs, empty-200 streaks, recent-failure marks, and the small
"this pair needs X" quirk marks (system-message-first, rejected params,
learned-thinking). Those all already SELF-EXPIRE on read (each has its own TTL /
`until` / window), so restoring them verbatim is safe: a mark that went stale
while the hub was off is ignored by its own consumer exactly as a live one
would be once its clock runs out.

This module is deliberately dumb: it stores and returns a JSON-native blob and
knows nothing about what the keys mean. app.py owns every pack/unpack (as it
does for perfstats), so the shape knowledge stays next to the dicts it mirrors.

Two limits, both inherited from perfstats:
  - NEUTRAL WHEN UNKNOWN. A missing / corrupt / stale file restores nothing, the
    exact state every boot started in before this file existed.
  - NEVER RAISES. Losing a save costs at most the last minute of learning; it
    must never take a request or a boot down with it.
"""
import json
import os
import threading
import time

import config

SAVE_INTERVAL = 60.0          # seconds between writes; the hot path never blocks on IO
MAX_AGE = 7 * 86400           # a whole-file guard: nothing here is worth more than a week

_FILE = "learned-state.json"
_SCHEMA = 1

_lock = threading.Lock()
_last_save = [0.0]


def _path():
    return os.path.join(config.state_dir(), _FILE)


def load():
    """Return the stored `{name: <json value>}` map, or {} on ANY problem.

    The whole file is dropped when it is older than MAX_AGE, the schema does
    not match, or it cannot be read/parsed -- in every case the hub boots with
    empty learned state, which is the neutral state it has always started in."""
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            blob = json.load(f)
        if not isinstance(blob, dict) or blob.get("schema") != _SCHEMA:
            return {}
        saved = float(blob.get("saved") or 0)
        if saved <= 0 or (time.time() - saved) > MAX_AGE:
            return {}
        dicts = blob.get("dicts")
        return dicts if isinstance(dicts, dict) else {}
    except Exception:                                            # noqa: BLE001
        return {}


def due(now=None):
    """True when SAVE_INTERVAL has passed since the last write -- lets the hot
    path skip building the (lock-taking) snapshot on the 99% of calls that
    would be throttled anyway."""
    now = time.time() if now is None else now
    with _lock:
        return (now - _last_save[0]) >= SAVE_INTERVAL


def save(dicts, force=False):
    """Persist `dicts` (a JSON-native map), throttled to SAVE_INTERVAL unless
    `force`. Temp file + os.replace, so a crash mid-write never leaves a
    half-written file for the next boot. Returns True if it wrote. Never
    raises."""
    now = time.time()
    with _lock:
        if not force and (now - _last_save[0]) < SAVE_INTERVAL:
            return False
        _last_save[0] = now
    try:
        if not isinstance(dicts, dict):
            return False
        path = _path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"schema": _SCHEMA, "saved": round(now, 3), "dicts": dicts}, f)
        os.replace(tmp, path)
        return True
    except Exception:                                            # noqa: BLE001
        return False
