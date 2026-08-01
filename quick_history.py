"""Calvoun Free LLM Hub -- persistent QUICK-CHAT conversation history.

The dashboard's quick chat (#sec-chat) has always been amnesiac: every
message lived in the page's JavaScript only, so a browser reload -- never
mind a hub restart -- threw the whole conversation away. This module is the
disk layer that fixes that, and only that: list past conversations, reopen
one and keep talking, delete the ones no longer wanted.

Deliberately a SIBLING of agentic_history.py rather than a branch of it.
That module persists AGENT (CLI subprocess) transcripts keyed by an
agentic_chat session id and carries rewind checkpoints; this one persists
plain provider chat exchanges keyed by an id the browser holds. The two
stores have different lifetimes, different shapes and different callers, so
they get different folders -- but identical CONVENTIONS, copied on purpose:

  * index.json for cheap listing + one JSON file per conversation. A
    transcript grows unboundedly turn by turn, so rewriting one giant
    day-keyed blob (usage_history.py's shape) on every single message of
    every conversation would be wasteful.
  * a module-level threading.RLock, because Flask serves requests
    concurrently and two tabs can save into the same index.
  * every write atomic: temp file in the destination folder, then
    os.replace. A crash mid-write leaves the previous file intact.
  * retention capped by both count (MAX_CONVERSATIONS) and age
    (RETENTION_DAYS), plus a per-conversation message cap (MAX_TURNS) the
    agentic store does not need -- a quick chat is cheap to spam and one
    runaway conversation should not be able to grow without bound.
  * nothing here ever raises at the caller. Losing a history row must never
    break the actual chat reply the user is waiting on, exactly the tradeoff
    agentic_history.record_turn() / usage_history.record() already make. A
    corrupt or missing file degrades to "empty", not to a 500.

Id policy -- STRICTER THAN THE SIBLING, ON PURPOSE. agentic_history hashes an
id that is not filename-safe, so a weird-but-genuine session id still gets
stored. Here a conversation id is only ever minted by new_conversation_id()
and echoed back by the browser, so a value outside that alphabet is a bug or
an attack, never real data worth keeping. Such ids are REFUSED outright --
save_turn() is a no-op, load returns None, delete returns False -- so no
caller-supplied string ever reaches the filesystem as a path component and
nothing is written anywhere, inside the folder or outside it. Same alphabet
as agentic_history accepts ([A-Za-z0-9_-]), anchored with \\Z rather than $
so a trailing newline cannot sneak through.

Pure stdlib (json, os, re, tempfile, threading, time, uuid) + config.py for
the hub's state directory. Never imports app.py.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import uuid

import config

_LOCK = threading.RLock()

# Conversation-level retention, matching agentic_history.py's count + age
# pair (a conversation is one open-ended blob per entry, so a day-only cap
# like usage_history.py's would not bound the store).
MAX_CONVERSATIONS = 200
RETENTION_DAYS = 30

# Per-conversation cap, counted in MESSAGES. save_turn() appends two (the
# user's and the assistant's), so this is 200 exchanges; keeping it even
# means trimming from the front can never orphan a reply from its question.
MAX_TURNS = 400

MAX_TITLE_CHARS = 60
DEFAULT_TITLE = "New conversation"

# Same alphabet agentic_history accepts, plus a length bound: an id becomes a
# filename, and 64 chars is already twice a uuid4 hex.
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")

_ROW_KEYS = ("id", "title", "updated", "turns", "model")


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

def _root():
    """Storage folder: <hub state dir>/quick_history/.

    config.state_dir() is the dirname of the resolved config path, so this
    follows FREE_LLM_HUB_CONFIG and lands next to config.json, agentic_history
    and generated_images. Re-resolved on every call so a late env change (or a
    test) is honored, exactly like config._config_path() does."""
    return os.path.join(config.state_dir(), "quick_history")


def _index_path():
    return os.path.join(_root(), "index.json")


def _safe_id(conversation_id):
    """The id, iff it is safe to use as a bare filename component -- else
    None, which every public function treats as "there is no such
    conversation". Returning None instead of a sanitized/hashed fallback is
    what makes traversal impossible rather than merely unlikely: '../../evil',
    'C:\\Windows\\x', 'a/b' and 'a\\b' never become a path at all."""
    if isinstance(conversation_id, str) and _SAFE_ID_RE.match(conversation_id):
        return conversation_id
    return None


def _conv_path(conversation_id):
    """Absolute path of one conversation's file, or None if the id is
    refused. Callers must check for None before touching the filesystem."""
    safe = _safe_id(conversation_id)
    if safe is None:
        return None
    return os.path.join(_root(), safe + ".json")


# --------------------------------------------------------------------------- #
# Low-level load/save -- atomic tmp-file-then-rename, the same idiom as
# agentic_history._save_index() / image_history._save_index(). Callers hold
# _LOCK. ensure_ascii=False keeps accented chat text readable on disk.
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
    fd, tmp = tempfile.mkstemp(prefix=".quick-index-", suffix=".tmp", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(entries, indent=2, ensure_ascii=False))
        os.replace(tmp, _index_path())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_conv(conversation_id):
    path = _conv_path(conversation_id)
    if path is None:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _save_conv(conv):
    path = _conv_path(conv.get("id"))
    if path is None:
        return
    root = _root()
    os.makedirs(root, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".quick-conv-", suffix=".tmp", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(conv, indent=2, ensure_ascii=False))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# Shaping
# --------------------------------------------------------------------------- #

def _text(value):
    """Chat text arrives from JSON, so it can be anything. Coerce rather than
    reject: a history row is not worth failing a real reply over."""
    if isinstance(value, str):
        return value
    return "" if value is None else str(value)


def _title_from(text):
    """A short, whitespace-collapsed label derived from the first user
    message -- what the conversation list shows instead of a raw id."""
    words = " ".join(_text(text).split())
    if not words:
        return DEFAULT_TITLE
    if len(words) <= MAX_TITLE_CHARS:
        return words
    return words[:MAX_TITLE_CHARS].rstrip() + "\u2026"


def _message(role, content, model=None, provider=None, ts=None):
    return {
        "role": role,
        "content": _text(content),
        "model": model,
        "provider": provider,
        "ts": time.time() if ts is None else ts,
    }


def _row(conv):
    """The lightweight index row -- metadata only, never the transcript, so
    listing 200 conversations does not mean loading 200 transcripts."""
    return {
        "id": conv.get("id"),
        "title": conv.get("title") or DEFAULT_TITLE,
        "updated": conv.get("updated"),
        "turns": len(conv.get("turns") or []),
        "model": conv.get("model"),
    }


def _prune(entries):
    """Drop rows past MAX_CONVERSATIONS or RETENTION_DAYS (by `updated`),
    deleting each dropped conversation's own file too, so a pruned
    conversation frees its bytes instead of just going unlisted. `entries`
    is already newest-first. Mirrors agentic_history._prune()."""
    cutoff = time.time() - RETENTION_DAYS * 86400
    kept, dropped = [], []
    for e in entries:
        if not isinstance(e, dict):
            continue
        if (e.get("updated") or 0) < cutoff:
            dropped.append(e)
        else:
            kept.append(e)
    if len(kept) > MAX_CONVERSATIONS:
        dropped.extend(kept[MAX_CONVERSATIONS:])
        kept = kept[:MAX_CONVERSATIONS]
    for e in dropped:
        path = _conv_path(e.get("id"))
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
    return kept


def _upsert_row(conv):
    """Replace (or insert, newest-first) this conversation's row, prune, save
    -- one load + one save, called while holding _LOCK."""
    entries = [e for e in _load_index()
               if isinstance(e, dict) and e.get("id") != conv.get("id")]
    entries.insert(0, _row(conv))
    _save_index(_prune(entries))


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def new_conversation_id():
    """A fresh id for a conversation the user is about to start. uuid4 hex,
    i.e. always inside the alphabet _safe_id() accepts -- an id produced here
    can never be the one that gets refused."""
    return uuid.uuid4().hex


def save_turn(conversation_id, user_text, assistant_text, model=None, provider=None):
    """Append ONE exchange -- the user's message and the assistant's reply --
    to conversation_id, creating the conversation on the first call.

    Records `created`/`updated` timestamps and, on creation, a short title
    derived from the first user message. A conversation that never had usable
    first text keeps the DEFAULT_TITLE placeholder until a later turn can
    replace it, so the list never renders a blank row.

    Returns None and never raises: this is called on the response path of a
    real chat request, and a history failure must not become a failed reply.
    An unusable conversation id is silently ignored (nothing is written)."""
    try:
        with _LOCK:
            if _safe_id(conversation_id) is None:
                return
            now = time.time()
            user_text = _text(user_text)
            conv = _load_conv(conversation_id)
            if conv is None:
                conv = {
                    "id": conversation_id,
                    "title": _title_from(user_text),
                    "created": now,
                    "updated": now,
                    "model": model,
                    "provider": provider,
                    "turns": [],
                }
            elif not conv.get("title") or conv.get("title") == DEFAULT_TITLE:
                conv["title"] = _title_from(user_text)
            turns = conv.get("turns")
            if not isinstance(turns, list):
                turns = []
            turns.append(_message("user", user_text, ts=now))
            turns.append(_message("assistant", assistant_text, model, provider, ts=now))
            # Oldest messages fall off the front; MAX_TURNS is even and we
            # always append in pairs, so a reply is never left without its
            # question.
            conv["turns"] = turns[-MAX_TURNS:]
            conv["id"] = conversation_id
            conv["updated"] = now
            conv.setdefault("created", now)
            if model:
                conv["model"] = model
            if provider:
                conv["provider"] = provider
            _save_conv(conv)
            _upsert_row(conv)
    except Exception:
        pass


def list_conversations(limit=50):
    """Newest-first metadata rows for the history list: {"id", "title",
    "updated", "turns", "model"}, where `turns` is the number of stored
    MESSAGES (one save_turn() adds two). Metadata only -- the transcripts stay
    on disk until something is actually opened. [] if there is no history, if
    the index is unreadable, or if it is corrupt. Never raises."""
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 50
    try:
        with _LOCK:
            entries = _load_index()
        rows = []
        for e in entries[:limit]:
            if isinstance(e, dict):
                rows.append({k: e.get(k) for k in _ROW_KEYS})
        return rows
    except Exception:
        return []


def load_conversation(conversation_id):
    """One full conversation -- {"id", "title", "created", "updated",
    "turns": [{"role", "content", "model", "provider", "ts"}]} -- ready to be
    replayed into the chat pane, or None if there is no such conversation, the
    id is refused, or its file is unreadable/corrupt. Never raises."""
    try:
        with _LOCK:
            conv = _load_conv(conversation_id)
        if conv is None:
            return None
        turns = []
        for t in conv.get("turns") or []:
            if not isinstance(t, dict):
                continue
            turns.append(_message(t.get("role"), t.get("content"), t.get("model"),
                                  t.get("provider"), t.get("ts")))
        return {
            "id": conv.get("id") or conversation_id,
            "title": conv.get("title") or DEFAULT_TITLE,
            "created": conv.get("created"),
            "updated": conv.get("updated"),
            "turns": turns,
        }
    except Exception:
        return None


def delete_conversation(conversation_id):
    """Remove one conversation's file and its index row. True if there was
    something to delete, False if there was not (so a second delete, an
    unknown id, or a refused id all report False). Never raises."""
    try:
        with _LOCK:
            path = _conv_path(conversation_id)
            if path is None:
                return False
            entries = _load_index()
            had_row = any(isinstance(e, dict) and e.get("id") == conversation_id
                          for e in entries)
            had_file = os.path.exists(path)
            if not had_row and not had_file:
                return False
            if had_row:
                _save_index([e for e in entries
                             if isinstance(e, dict) and e.get("id") != conversation_id])
            try:
                os.unlink(path)
            except OSError:
                pass
            return True
    except Exception:
        return False
