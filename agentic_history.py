"""Calvoun Free LLM Hub -- persistent agentic-chat conversation history +
rewind checkpoints.

SEPARATE, disk-persisted layer on top of agentic_chat.py's in-memory-only
session registry. That registry deliberately holds a live subprocess handle
(makes no sense to survive a restart -- see agentic_chat.py's own module
docstring); THIS module holds the actual conversation TRANSCRIPT (user
messages + agent replies, plain text, never a process handle), so a hub
restart never loses either side of a conversation.

Mirrors usage_history.py / image_history.py's existing conventions:
threading.RLock, atomic tmp-file-then-rename writes, a retention/pruning cap.
Storage shape is closer to image_history.py's (index.json for cheap listing +
one file per heavy entry) than usage_history.py's (one flat day-keyed blob):
a conversation transcript grows unboundedly turn-by-turn like a generated
image grows unboundedly in bytes, so rewriting one giant JSON file for every
single turn of every conversation would be wasteful -- each conversation gets
its own JSON file, and index.json holds only lightweight metadata rows for
the history-browser list.

Retention: capped by both MAX_CONVERSATIONS (count) and RETENTION_DAYS (age),
exactly like image_history.py -- picked over usage_history.py's day-only cap
because, like images, a conversation is one open-ended blob per entry rather
than a bounded day x model aggregate.

Checkpoint semantics -- IMPORTANT SCOPE LIMIT: a checkpoint is a TRANSCRIPT
BOOKMARK, not a filesystem snapshot/undo. create_checkpoint() records only an
index into that session's turn list (how many turns had happened) plus a
timestamp and optional label. It does NOT snapshot the project folder's files
on disk in any way -- this hub has no sandboxing or versioning of the
project directory the agentic session is pointed at. "Rewinding" to a
checkpoint means the user can see where in the conversation this checkpoint
was and start a NEW session that resumes context from around that point; it
never reverts any file the agent actually edited. Any UI built on top of this
must not imply otherwise.

Pure stdlib: hashlib, json, os, re, tempfile, threading, time, uuid.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
import uuid

_LOCK = threading.RLock()

# Conversation-level retention -- both a count cap and an age cap, mirroring
# image_history.py (an open-ended, per-entry-file store) rather than
# usage_history.py (a bounded day x model aggregate).
MAX_CONVERSATIONS = 200
RETENTION_DAYS = 30

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

def _root():
    env = os.environ.get("FREE_LLM_HUB_CONFIG")
    base = os.path.dirname(os.path.abspath(os.path.expanduser(env))) if env else os.path.join(
        os.path.expanduser("~"), ".free-llm-hub")
    return os.path.join(base, "agentic_history")


def _index_path():
    return os.path.join(_root(), "index.json")


def _safe_filename(session_id):
    """session_id normally comes from agentic_chat's own uuid4().hex, but it
    also arrives here via a URL path segment (Flask route <session_id>), so
    never trust it blindly as a filename component -- fall back to a hash for
    anything that isn't plain alnum/-/_ (defense in depth against path
    traversal, e.g. '../../x')."""
    if isinstance(session_id, str) and _SAFE_ID_RE.match(session_id):
        return session_id
    return hashlib.sha256((session_id or "").encode("utf-8", "replace")).hexdigest()


def _conv_path(session_id):
    return os.path.join(_root(), _safe_filename(session_id) + ".json")


# --------------------------------------------------------------------------- #
# Low-level load/save -- atomic tmp-file-then-rename, identical idiom to
# usage_history._save() / image_history._save_index(). Callers hold _LOCK.
# --------------------------------------------------------------------------- #

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
    fd, tmp = tempfile.mkstemp(prefix=".history-index-", suffix=".tmp", dir=root)
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


def _load_conversation(session_id):
    try:
        with open(_conv_path(session_id), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _save_conversation(conv):
    root = _root()
    os.makedirs(root, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".history-conv-", suffix=".tmp", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(conv, indent=2))
        os.replace(tmp, _conv_path(conv["session_id"]))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _metadata_row(conv):
    return {
        "session_id": conv.get("session_id"),
        "cli_id": conv.get("cli_id"),
        "project_dir": conv.get("project_dir"),
        "started_at": conv.get("started_at"),
        "last_active_at": conv.get("last_active_at"),
        "turn_count": len(conv.get("turns") or []),
    }


def _prune(entries):
    """Drop index rows past MAX_CONVERSATIONS or RETENTION_DAYS (by
    last_active_at), deleting each dropped conversation's own file too.
    `entries` is already newest-first. Mirrors image_history._prune()."""
    cutoff = time.time() - RETENTION_DAYS * 86400
    kept, dropped = [], []
    for e in entries:
        if (e.get("last_active_at") or 0) < cutoff:
            dropped.append(e)
        else:
            kept.append(e)
    if len(kept) > MAX_CONVERSATIONS:
        dropped.extend(kept[MAX_CONVERSATIONS:])
        kept = kept[:MAX_CONVERSATIONS]
    for e in dropped:
        try:
            os.unlink(_conv_path(e.get("session_id")))
        except OSError:
            pass
    return kept


def _upsert_index_row(conv):
    """Replace (or insert, newest-first) this conversation's metadata row,
    then prune, then save -- one load + one save, called while holding _LOCK."""
    entries = _load_index()
    entries = [e for e in entries if e.get("session_id") != conv.get("session_id")]
    entries.insert(0, _metadata_row(conv))
    _save_index(_prune(entries))


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

_VALID_ROLES = ("user", "agent")


def record_turn(session_id, cli_id, project_dir, role, text, native_session_id=None):
    """Append one turn to session_id's persisted transcript, creating the
    conversation record on its first call. Never raises -- a history bug must
    never break a real agentic turn (the cost of a failure here is that one
    turn isn't persisted, same tradeoff usage_history.record()/image_history
    .save() make)."""
    if not session_id or role not in _VALID_ROLES:
        return
    try:
        with _LOCK:
            now = time.time()
            conv = _load_conversation(session_id)
            if conv is None:
                conv = {
                    "session_id": session_id,
                    "cli_id": cli_id,
                    "project_dir": project_dir,
                    "started_at": now,
                    "last_active_at": now,
                    "turns": [],
                    "checkpoints": [],
                }
            if cli_id:
                conv["cli_id"] = cli_id
            if project_dir:
                conv["project_dir"] = project_dir
            conv["last_active_at"] = now
            conv.setdefault("turns", []).append({
                "role": role,
                "text": text if isinstance(text, str) else str(text if text is not None else ""),
                "native_session_id": native_session_id,
                "ts": now,
            })
            _save_conversation(conv)
            _upsert_index_row(conv)
    except Exception:
        pass


def list_conversations(limit=50):
    """Metadata only (session_id/cli_id/project_dir/started_at/
    last_active_at/turn_count), newest-first -- for a history browser list.
    Never raises."""
    try:
        with _LOCK:
            entries = _load_index()
        return [dict(e) for e in entries[:limit]]
    except Exception:
        return []


def get_conversation(session_id):
    """Full turn-by-turn transcript (+ checkpoints) for one conversation, or
    None if it doesn't exist. Never raises."""
    if not session_id:
        return None
    try:
        with _LOCK:
            return _load_conversation(session_id)
    except Exception:
        return None


def delete_conversation(session_id):
    """Remove one conversation's file + index row. Returns whether anything
    existed to delete. Never raises."""
    if not session_id:
        return False
    try:
        with _LOCK:
            entries = _load_index()
            existed_row = any(e.get("session_id") == session_id for e in entries)
            path = _conv_path(session_id)
            existed_file = os.path.exists(path)
            if not existed_row and not existed_file:
                return False
            entries = [e for e in entries if e.get("session_id") != session_id]
            _save_index(entries)
            try:
                os.unlink(path)
            except OSError:
                pass
            return True
    except Exception:
        return False


def create_checkpoint(session_id, label=None):
    """Record a bookmark at the CURRENT turn count for session_id. Returns the
    checkpoint dict, or None if no such conversation exists yet. Never raises.

    SCOPE LIMIT (see module docstring): this is a conversation-transcript
    bookmark ONLY -- an index + timestamp + optional label. It is NOT a
    filesystem snapshot; nothing on disk in the project folder is captured or
    reverted. "Rewinding" means starting a new session informed by where this
    checkpoint sat in the transcript, not undoing file edits."""
    if not session_id:
        return None
    try:
        with _LOCK:
            conv = _load_conversation(session_id)
            if conv is None:
                return None
            checkpoint = {
                "id": uuid.uuid4().hex,
                "turn_index": len(conv.get("turns") or []),
                "created_at": time.time(),
                "label": label.strip() if isinstance(label, str) and label.strip() else None,
            }
            conv.setdefault("checkpoints", []).append(checkpoint)
            # Bookmarking a conversation is itself activity on it -- bump
            # last_active_at and refresh the index row so a conversation the
            # user just deliberately marked "come back to this" doesn't get
            # silently pruned on RETENTION_DAYS measured from its last TURN
            # (which could be long before this checkpoint).
            conv["last_active_at"] = checkpoint["created_at"]
            _save_conversation(conv)
            _upsert_index_row(conv)
            return checkpoint
    except Exception:
        return None


def list_checkpoints(session_id):
    """Checkpoints for one conversation, oldest-first. [] if the conversation
    doesn't exist or has none. Never raises."""
    if not session_id:
        return []
    try:
        with _LOCK:
            conv = _load_conversation(session_id)
        return list(conv.get("checkpoints") or []) if conv else []
    except Exception:
        return []
