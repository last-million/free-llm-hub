"""Durable per-conversation memory: what survives a restart and a compaction.

WHY THIS EXISTS
---------------
The hub already had three kinds of memory and none of them lasted.

  * The running recap that _compact_to_budget writes when it drops old turns
    lives in `_summary_cache` -- 64 entries, in RAM, gone the moment the hub
    auto-updates. The 5-hourly `git pull` restart is therefore also a
    5-hourly amnesia.
  * The standing instructions an agent is given ship on turn 1 and never
    again. For codex and opencode the addition is literally `""` from turn 2
    (`if sess.native_session_id: addition = ""`), so a session that runs for
    forty turns is following rules it was told about once, before compaction
    had eaten them.
  * `_Session.turn_count` resets to 0 when a session is resumed, so nothing
    could even ask "how long has this been going".

REPORTED as: "utilise le memory manager ... memory management, and also context
window, and also persistence, and also the orchestrator, and also everything
like this nothing can escape, and all agents and conversations will always
follow the rules 100%".

WHAT IT IS NOT
--------------
Not a vector store, not an embedding index, not a second brain. It is a small
JSON file per conversation holding the few things that must outlive the context
window: a running summary, the decisions worth not re-deciding, and the
counters that let the rest of the hub ask "is it time to say that again".

Everything here is best-effort. A conversation whose memory file cannot be
written is a conversation that works exactly as it did before -- no call site
may ever fail because remembering failed.

Pure stdlib, and no imports from app.py or agentic_chat: this is a leaf, for the
same reason model_categories is one.
"""
import json
import os
import re
import tempfile
import threading
import time

# One file per conversation, beside the rest of the hub's state.
_ROOT_ENV = "FREE_LLM_HUB_MEMORY_DIR"

# Bounds. A memory that grows without limit is a context window problem wearing
# a different hat -- the whole point is that this stays small enough to inject.
MAX_SUMMARY_CHARS = 4000
MAX_FACTS = 40
MAX_FACT_CHARS = 300
# How often an agent session is reminded of its standing instructions. Every
# turn would be nagging and would cost tokens on every request; never is what
# the hub did before.
RESTATE_EVERY = 8

_LOCK = threading.RLock()


def _root():
    env = os.environ.get(_ROOT_ENV)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(os.path.expanduser("~"), ".free-llm-hub", "memory")


_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _path(session_id):
    """The file for one conversation, or None when the id is not one.

    Ids reach this from a URL segment and from CLI output, so the filename is
    never built from an unchecked string: a session id of '../config' must not
    be able to name a file outside this directory."""
    # `str(None)` is "None", which matches the pattern below and would quietly
    # give every caller that lost its session id the SAME memory file. Only a
    # real string is an id.
    if not isinstance(session_id, str):
        return None
    sid = session_id.strip()
    if not _SAFE_ID_RE.match(sid):
        return None
    return os.path.join(_root(), sid + ".json")


def _blank(session_id):
    # NOT str(session_id). _save re-derives the path from this field, and
    # stringifying turned None into the perfectly valid id "None" -- so every
    # caller that had lost its session id wrote to, and read from, one shared
    # file. Keeping it unusable is what makes _save refuse.
    return {"session_id": session_id if isinstance(session_id, str) else None,
            "summary": "", "facts": [],
            "turns": 0, "rules_restated_turn": 0, "updated_at": 0.0}


def get(session_id):
    """This conversation's memory. Always a dict, never raises."""
    path = _path(session_id)
    if not path:
        return _blank(session_id)
    try:
        with open(path, encoding="utf-8") as fh:
            got = json.load(fh)
        if not isinstance(got, dict):
            return _blank(session_id)
        base = _blank(session_id)
        base.update({k: v for k, v in got.items() if k in base})
        if not isinstance(base.get("facts"), list):
            base["facts"] = []
        return base
    except (OSError, ValueError):
        return _blank(session_id)


def _save(mem):
    """Atomic write. A half-written memory file would be read as a blank one on
    the next turn, which is a silent loss rather than a loud one."""
    path = _path(mem.get("session_id"))
    if not path:
        return False
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mem["updated_at"] = time.time()
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(mem, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except (OSError, ValueError, TypeError):
        return False


def remember_summary(session_id, text):
    """Keep the running recap of what has already happened.

    This is what _compact_to_budget produces when it drops old turns, and until
    now it only ever lived in a RAM cache. Written whole rather than appended:
    a recap is a replacement for the turns it describes, not another turn."""
    text = (text or "").strip()
    if not text:
        return False
    with _LOCK:
        mem = get(session_id)
        mem["summary"] = text[:MAX_SUMMARY_CHARS]
        return _save(mem)


def remember_fact(session_id, text):
    """A decision or constraint worth not re-deciding.

    Deduplicated and bounded: the failure mode of a fact store is that it fills
    with restatements of the same thing until it is the context problem it was
    meant to solve. Oldest out first."""
    text = " ".join((text or "").split())[:MAX_FACT_CHARS]
    if not text:
        return False
    with _LOCK:
        mem = get(session_id)
        facts = [f for f in mem.get("facts") or [] if isinstance(f, str)]
        if text in facts:
            return True                        # already known; not an error
        facts.append(text)
        mem["facts"] = facts[-MAX_FACTS:]
        return _save(mem)


def note_turn(session_id):
    """Count a turn, durably. Returns the new count.

    _Session.turn_count resets to 0 when a session is resumed, so it cannot
    answer "how long has this conversation been going" across the 5-hourly
    restart -- which is exactly the question 'should the rules be restated'
    depends on."""
    with _LOCK:
        mem = get(session_id)
        mem["turns"] = int(mem.get("turns") or 0) + 1
        _save(mem)
        return mem["turns"]


def should_restate_rules(session_id, every=RESTATE_EVERY):
    """Is it time to remind this session of its standing instructions?

    True on the first turn after every `every` turns since the last reminder.
    Deliberately not every turn: a repeated notice reads as a repeated user
    instruction (the codex failure recorded in agentic_chat), and it costs
    tokens on a request that did not need it."""
    try:
        every = max(1, int(every))
    except (TypeError, ValueError):
        every = RESTATE_EVERY
    mem = get(session_id)
    turns = int(mem.get("turns") or 0)
    last = int(mem.get("rules_restated_turn") or 0)
    return turns > 0 and (turns - last) >= every


def mark_rules_restated(session_id):
    with _LOCK:
        mem = get(session_id)
        mem["rules_restated_turn"] = int(mem.get("turns") or 0)
        return _save(mem)


def context_block(session_id, budget_chars=2000):
    """What this conversation should carry into its next turn, or "".

    Ordered so that truncation loses the least: the decisions first, because
    they are short and expensive to re-derive, then as much of the summary as
    fits. Returns plain text -- the caller decides whether that becomes a
    system message, a prompt prefix or a file."""
    mem = get(session_id)
    facts = [f for f in (mem.get("facts") or []) if isinstance(f, str) and f.strip()]
    summary = (mem.get("summary") or "").strip()
    if not facts and not summary:
        return ""
    parts = []
    if facts:
        parts.append("Decisions already made in this conversation "
                     "(do not re-litigate these):")
        parts.extend("- " + f for f in facts)
    if summary:
        parts.append("")
        parts.append("What has happened so far:")
        parts.append(summary)
    text = "\n".join(parts)
    if len(text) <= budget_chars:
        return text
    # Trim the SUMMARY, never the decisions: a half-remembered decision is
    # worse than a half-remembered narrative.
    head = "\n".join(parts[:len(facts) + 1]) if facts else ""
    room = max(0, budget_chars - len(head) - 40)
    if room <= 0:
        return head[:budget_chars]
    return head + "\n\nWhat has happened so far:\n" + summary[:room]


def forget(session_id):
    """Drop one conversation's memory. Used when a conversation is deleted."""
    path = _path(session_id)
    if not path:
        return False
    try:
        os.unlink(path)
        return True
    except OSError:
        return False


def stats():
    """How much is remembered, for the dashboard."""
    root = _root()
    try:
        names = [n for n in os.listdir(root) if n.endswith(".json")]
    except OSError:
        return {"conversations": 0, "bytes": 0}
    total = 0
    for n in names:
        try:
            total += os.path.getsize(os.path.join(root, n))
        except OSError:
            pass
    return {"conversations": len(names), "bytes": total}
