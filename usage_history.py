"""Calvoun Free LLM Hub -- per-day, per-model TOKEN usage history.

Separate from quota.py (which tracks REQUEST COUNTS against a provider's
CURRENT free-tier window, in-memory, resets on restart). This tracks actual
TOKEN usage (prompt+completion), keyed by calendar day (UTC) and by
"<provider>/<model>", persisted to disk so a multi-day history survives a
hub restart. Retention is capped so the file doesn't grow forever.

Pure stdlib: json, os, tempfile, threading, time.
"""
import json
import os
import tempfile
import threading
import time

_LOCK = threading.RLock()
RETENTION_DAYS = 90


def _path():
    env = os.environ.get("FREE_LLM_HUB_CONFIG")
    base = os.path.dirname(os.path.abspath(os.path.expanduser(env))) if env else os.path.join(
        os.path.expanduser("~"), ".free-llm-hub")
    return os.path.join(base, "usage_history.json")


def _load():
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data):
    path = _path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".usage-", suffix=".tmp", dir=parent or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def _prune(data):
    cutoff = time.time() - RETENTION_DAYS * 86400
    for day_key in list(data.keys()):
        try:
            day_epoch = time.mktime(time.strptime(day_key, "%Y-%m-%d"))
        except ValueError:
            data.pop(day_key, None)
            continue
        if day_epoch < cutoff:
            data.pop(day_key, None)


def record(pid, model, prompt_tokens=0, completion_tokens=0, estimated=False):
    """Add one request's usage to today's (UTC) running total for pid/model.
    Never raises -- a usage-tracking bug must never break a real request."""
    if not pid or not model:
        return
    key = pid + "/" + model
    try:
        with _LOCK:
            data = _load()
            day = data.setdefault(_today(), {})
            row = day.setdefault(key, {"prompt_tokens": 0, "completion_tokens": 0,
                                        "requests": 0, "estimated_requests": 0})
            row["prompt_tokens"] += max(0, int(prompt_tokens or 0))
            row["completion_tokens"] += max(0, int(completion_tokens or 0))
            row["requests"] += 1
            if estimated:
                row["estimated_requests"] += 1
            _prune(data)
            _save(data)
    except Exception:
        pass


def get_day(date_str=None):
    """{"date","total_tokens","models":[{...}]} for one UTC day (default today)."""
    date_str = date_str or _today()
    with _LOCK:
        data = _load()
    day = data.get(date_str) or {}
    models = []
    total = 0
    for key, row in day.items():
        pt = row.get("prompt_tokens", 0)
        ct = row.get("completion_tokens", 0)
        total += pt + ct
        pid, _, model = key.partition("/")
        models.append({"id": key, "provider": pid, "model": model,
                       "prompt_tokens": pt, "completion_tokens": ct,
                       "total_tokens": pt + ct, "requests": row.get("requests", 0),
                       "estimated_requests": row.get("estimated_requests", 0)})
    models.sort(key=lambda m: m["total_tokens"], reverse=True)
    return {"date": date_str, "total_tokens": total, "models": models}


def recent_days(limit=30):
    """Sorted (newest first) list of UTC date strings that have any data."""
    with _LOCK:
        data = _load()
    return sorted(data.keys(), reverse=True)[:limit]
