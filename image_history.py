"""Calvoun Free LLM Hub -- generated-image history (view + delete).

Persists every successfully generated image (via /v1/images/generations,
regardless of caller) to disk as an actual file plus a small JSON metadata
index, so the dashboard's Image generation section can show a gallery of
past results across restarts -- unlike the in-response-only result, which
disappears the moment the HTTP response is sent.

Retention is capped by both a max COUNT and a max AGE so this can't grow
without bound: images are real binary data, not small JSON rows, so the
count cap matters here far more than it does for usage_history.py.

Pure stdlib: json, os, tempfile, threading, time, uuid.
"""
import json
import os
import tempfile
import threading
import time
import uuid

_LOCK = threading.RLock()
MAX_ENTRIES = 200
RETENTION_DAYS = 30


def _root():
    env = os.environ.get("FREE_LLM_HUB_CONFIG")
    base = os.path.dirname(os.path.abspath(os.path.expanduser(env))) if env else os.path.join(
        os.path.expanduser("~"), ".free-llm-hub")
    return os.path.join(base, "generated_images")


def _index_path():
    return os.path.join(_root(), "index.json")


def _load_index():
    try:
        with open(_index_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_index(entries):
    root = _root()
    os.makedirs(root, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".index-", suffix=".tmp", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(entries, indent=2))
        os.replace(tmp, _index_path())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _prune(entries):
    """Drop entries past MAX_ENTRIES or RETENTION_DAYS, deleting their files
    too. `entries` is already newest-first (save() inserts at index 0)."""
    cutoff = time.time() - RETENTION_DAYS * 86400
    kept, dropped = [], []
    for e in entries:
        if e.get("created_at", 0) < cutoff:
            dropped.append(e)
        else:
            kept.append(e)
    if len(kept) > MAX_ENTRIES:
        dropped.extend(kept[MAX_ENTRIES:])
        kept = kept[:MAX_ENTRIES]
    root = _root()
    for e in dropped:
        try:
            os.unlink(os.path.join(root, e["filename"]))
        except OSError:
            pass
    return kept


def save(raw_bytes, prompt, provider, model, mime_type="image/png"):
    """Persist one generated image, return its id (or None on failure).
    Never raises -- a history-tracking bug must never break a real
    image-generation request."""
    try:
        with _LOCK:
            root = _root()
            os.makedirs(root, exist_ok=True)
            ext = "jpg" if "jpeg" in (mime_type or "") else "png"
            image_id = uuid.uuid4().hex
            filename = image_id + "." + ext
            with open(os.path.join(root, filename), "wb") as f:
                f.write(raw_bytes)
            entries = _load_index()
            entries.insert(0, {
                "id": image_id, "filename": filename, "mime_type": mime_type,
                "prompt": (prompt or "")[:500], "provider": provider, "model": model,
                "created_at": time.time(), "size_bytes": len(raw_bytes),
            })
            _save_index(_prune(entries))
            return image_id
    except Exception:
        return None


def list_entries(limit=100):
    """Metadata only (no image bytes) -- newest first."""
    with _LOCK:
        entries = _load_index()
    return [{k: v for k, v in e.items() if k != "filename"} for e in entries[:limit]]


def get_file(image_id):
    """(raw_bytes, mime_type) for one entry, or (None, None) if missing."""
    with _LOCK:
        entries = _load_index()
    entry = next((e for e in entries if e.get("id") == image_id), None)
    if not entry:
        return None, None
    try:
        with open(os.path.join(_root(), entry["filename"]), "rb") as f:
            return f.read(), entry.get("mime_type", "image/png")
    except OSError:
        return None, None


def delete(image_id):
    """Remove one entry + its file. Returns True if something was deleted."""
    with _LOCK:
        entries = _load_index()
        match = next((e for e in entries if e.get("id") == image_id), None)
        if not match:
            return False
        entries = [e for e in entries if e.get("id") != image_id]
        try:
            os.unlink(os.path.join(_root(), match["filename"]))
        except OSError:
            pass
        _save_index(entries)
        return True
