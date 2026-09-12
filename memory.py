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

Nor is it a truth store. Reviewed from outside (2026-09-12): "useful
continuity memory, not semantic truth ... it does not know whether a code
claim is still true at the current revision". What it does since is the cheap
half of that: a fact is stamped with the project paths it names and the
project's revision, and recall marks references that have moved ("gone since",
"changed since", "now on disk") -- a freshness signal about what a fact refers
to, never a verdict on whether the claim itself holds. See REF_MAX_PER_FACT.

Pure stdlib, and no imports from app.py or agentic_chat: this is a leaf, for the
same reason model_categories is one.
"""
import hashlib
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

# THREE HORIZONS, NOT ONE.
#
# REQUESTED: "long, short, medium memory for each project and session, and for
# long and short and medium context window".
#
# The three are different kinds of thing, and collapsing them is why a long
# conversation both forgets what matters and pays for what does not:
#
#   SHORT   the last few exchanges, verbatim. The model already has these in
#           its window; what it loses is the ones compaction just dropped, so
#           this keeps a one-line trace of each recent turn to hand back.
#   MEDIUM  the running summary of everything older. One paragraph standing in
#           for a hundred turns.
#   LONG    the decisions and constraints that outlive the conversation
#           entirely -- and, for the first time, that can belong to the PROJECT
#           rather than the session, so tomorrow's conversation in the same
#           folder starts knowing what yesterday's established.
MAX_RECENT = 12               # short: how many turn traces are kept
MAX_RECENT_CHARS = 200        # ...and how much of each

# What each horizon may spend of the context budget when they compete. Short
# memory is cheapest and most perishable; long memory is the expensive thing to
# re-derive, so it is served first and trimmed last.
SHARE_LONG = 0.45
SHARE_MEDIUM = 0.35
SHARE_SHORT = 0.20
# How often an agent session is reminded of its standing instructions. Every
# turn would be nagging and would cost tokens on every request; never is what
# the hub did before.
RESTATE_EVERY = 8

# THE TASK LIST AND THE PLACE THE WORK STOPPED.
#
# REQUESTED: "make sure he always creates a todo list and tracks it and
# updates it while working, and if I stop the agent he should keep what the
# LLM was working on, and when I come back and ask it to continue he should
# continue from where he was working exactly".
#
# The agent is TOLD to keep a checklist (agentic_chat._PLANNING_SNIPPET and
# craft.PLAN_PHASES ask for PROGRESS.md); this is the hub's own copy of it,
# read off the project's PROGRESS.md / TODO.md and off the checklists in the
# agent's replies, so the page can show it and the next turn can be handed it
# even when the CLI's own thread has forgotten. And when a turn is stopped or
# dies, what it was doing -- the request, its last tool calls, the text it had
# written -- is filed here and put in front of the next turn, so "continue"
# means continue and not start over.
MAX_TASKS = 40
MAX_TASK_CHARS = 160
TASK_FILES = ("PROGRESS.md", "TODO.md", "PLAN.md", "TASKS.md")
TASK_FILE_MAX_BYTES = 64 * 1024
# "- [ ] text", "* [x] text", "1. [~] text"; ~ and / and > mark in progress.
_CHECK_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s*\[([ xX~/>])\]\s*(.+?)\s*$")
INTERRUPT_DOING = 8           # how many of the last tool calls are kept
INTERRUPT_PARTIAL_CHARS = 700

# IS IT STILL TRUE? A fact is remembered as text; the files it NAMES are what
# change under it. Reviewed from outside (2026-09-12): "this project's memory
# is useful continuity memory, not semantic truth ... it does not know whether
# a code claim is still true at the current revision". Right, and this is the
# cheap layer on top: each fact is stamped with the project paths it mentions
# (existence, size, mtime, a content hash for small files) and the project's
# git HEAD when it was learned; at recall the same paths are stat'd again and
# a fact whose references moved is marked -- "[gone since: src/pen.js]",
# "[changed since: app.py]", "[now on disk: hello.txt]" -- never dropped,
# never re-ordered, and never called "still true": an unchanged file is a
# freshness signal about the fact's references, not a proof of the claim.
# No LLM call, no subprocess, no tree walk: a few os.stat per recalled fact.
REF_MAX_PER_FACT = 3
REF_MAX_PER_INTERRUPT = 6
REF_HASH_MAX_BYTES = 512 * 1024
REF_SUFFIX_MAX_CHARS = 80
REF_PATH_SHOW_CHARS = 28
_REF_SPLIT_RE = re.compile(r"[\s\"'()\[\]{}<>,;`]+")
_REF_FILE_RE = re.compile(r"^[\w.-]+\.[A-Za-z][A-Za-z0-9]{0,4}$")

_LOCK = threading.RLock()


def _root():
    env = os.environ.get(_ROOT_ENV)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(os.path.expanduser("~"), ".free-llm-hub", "memory")


_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# WHO OWNS A MEMORY.
#
# REQUESTED: "each task working should have his memory alone, and each agent
# have his memory, and some can have shared memory in swarm, and global memory
# ... to not consume much tokens with useless tokens from context window and
# memory".
#
# Five scopes, narrowest first. A memory is written to exactly ONE of them, and
# a turn reads the ones that apply to it:
#
#   task     one unit of work inside a conversation. Dies with the task; this
#            is where "the file is at src/api/v2.ts" belongs, and exactly what
#            should NOT follow the conversation into the next job.
#   agent    one session. What this worker has established for itself.
#   run      SHARED between the agents of one swarm. The reason a swarm is a
#            crew and not five strangers: what agent 2 discovered about the
#            build, agent 4 does not have to discover again.
#   project  the folder. Outlives every conversation in it.
#   global   the install. Facts true of this machine and this user.
#
# A narrower scope wins a contradiction, because it is the more specific
# claim: a task that decided on Vite overrides a project note preferring
# webpack, for the length of that task.
GLOBAL_KEY = "global-memory"
SCOPE_ORDER = ("task", "agent", "run", "project", "global")


def global_key():
    return GLOBAL_KEY


def run_key(run_id):
    """The scope SHARED by every agent in one swarm run."""
    rid = str(run_id or "").strip()
    if not rid or not _SAFE_ID_RE.match(rid):
        return None
    return "run-" + rid[:48]


def task_key(session_id, task):
    """One unit of work inside a conversation.

    Hashed on the session AND the task text, so the same task description in
    two different conversations is two memories -- a task is work, not a
    topic, and two people doing "add auth" are not doing the same work."""
    sid = str(session_id or "").strip()
    txt = " ".join(str(task or "").split()).lower()
    if not sid or not txt:
        return None
    raw = (sid + "|" + txt).encode("utf-8", "replace")
    return "task-" + hashlib.sha256(raw).hexdigest()[:20]


def project_key(project_dir):
    """A stable, filesystem-safe id for one project folder, or None.

    A project is identified by its PATH, which is neither safe nor short as a
    filename, so it is hashed. Case- and separator-normalised first: the same
    folder reached as C:\\work\\site and c:/work/site is one project, and on
    Windows it routinely is both within a single session."""
    if not isinstance(project_dir, str) or not project_dir.strip():
        return None
    norm = os.path.normcase(os.path.abspath(project_dir.strip()))
    return "proj-" + hashlib.sha256(norm.encode("utf-8", "replace")).hexdigest()[:20]


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
            "summary": "", "facts": [], "recent": [],
            "turns": 0, "rules_restated_turn": 0, "updated_at": 0.0,
            "compactions": 0, "restate_due": False,
            "tasks": [], "tasks_source": "", "interrupted": None,
            "fact_meta": {}, "summary_rev": None, "summary_turn": 0}


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
        for field in ("facts", "recent", "tasks"):
            if not isinstance(base.get(field), list):
                base[field] = []
        if not isinstance(base.get("interrupted"), dict):
            base["interrupted"] = None
        if not isinstance(base.get("fact_meta"), dict):
            base["fact_meta"] = {}
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


# --------------------------------------------------------------------------- #
# What a fact refers to, and whether that is still there
# --------------------------------------------------------------------------- #

def _fact_key(text):
    return hashlib.sha1((text or "").encode("utf-8", "replace")).hexdigest()[:12]


def _project_rev(project_dir):
    """The project's git HEAD, short, read from the .git files -- or None.

    No subprocess: a project without git on PATH still answers, and a hung
    git can never stall a turn. Walks up at most eight levels for .git,
    follows a worktree's "gitdir:" file, reads HEAD and its ref (or
    packed-refs). Two to four tiny reads."""
    try:
        here = os.path.abspath(str(project_dir or ""))
        if not here or not os.path.isdir(here):
            return None
        git = None
        for _ in range(8):
            cand = os.path.join(here, ".git")
            if os.path.isdir(cand):
                git = cand
                break
            if os.path.isfile(cand):
                with open(cand, encoding="utf-8", errors="replace") as fh:
                    first = fh.read(400).strip()
                if first.startswith("gitdir:"):
                    git = os.path.normpath(os.path.join(here, first[7:].strip()))
                break
            up = os.path.dirname(here)
            if up == here:
                break
            here = up
        if not git or not os.path.isdir(git):
            return None
        with open(os.path.join(git, "HEAD"), encoding="utf-8", errors="replace") as fh:
            head = fh.read(200).strip()
        if re.match(r"^[0-9a-f]{40}$", head):
            return head[:7]
        if not head.startswith("ref:"):
            return None
        ref = head[4:].strip()
        common = git
        cd = os.path.join(git, "commondir")
        if os.path.isfile(cd):
            with open(cd, encoding="utf-8", errors="replace") as fh:
                common = os.path.normpath(os.path.join(git, fh.read(400).strip()))
        for base in (git, common):
            path = os.path.join(base, *ref.split("/"))
            if os.path.isfile(path):
                with open(path, encoding="utf-8", errors="replace") as fh:
                    sha = fh.read(80).strip()
                if re.match(r"^[0-9a-f]{40}$", sha):
                    return sha[:7]
        packed = os.path.join(common, "packed-refs")
        if os.path.isfile(packed):
            with open(packed, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    parts = line.strip().split()
                    if len(parts) == 2 and parts[1] == ref and re.match(r"^[0-9a-f]{40}$", parts[0]):
                        return parts[0][:7]
    except (OSError, ValueError):
        pass
    return None


def _fingerprint(project_dir, rel):
    """What one referenced path looks like right now."""
    ref = {"p": rel, "ex": False, "sz": None, "mt": None, "h": None}
    try:
        full = os.path.join(project_dir, rel)
        st = os.stat(full)
        ref["ex"] = True
        if os.path.isdir(full):
            return ref
        ref["sz"] = int(st.st_size)
        ref["mt"] = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
        if st.st_size <= REF_HASH_MAX_BYTES:
            ref["h"] = _content_hash(full)
    except (OSError, ValueError, TypeError):
        pass
    return ref


def _content_hash(path):
    try:
        h = hashlib.sha1()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()[:12]
    except (OSError, ValueError, TypeError):
        return None


# Prose that looks like a file name and is not one.
_REF_NOT_FILES = {"e.g", "i.e", "etc", "vs", "u.s", "node.js", "next.js", "vue.js",
                  "nuxt.js", "react.js", "express.js", "three.js", "d3.js"}
# How many trailing words to peel off a spelled-out absolute path before giving
# up on it: "Edit C:\proj\my file.py now" -> "my file.py".
_REF_PEEL = 4


def _inside(root, tok):
    """(full, rel) for a token that names something INSIDE root, else None.
    Lexically first, then by realpath, so a link the project contains cannot
    point the stat outside it."""
    try:
        if "\x00" in tok:
            return None
        full = os.path.abspath(tok) if os.path.isabs(tok) \
            else os.path.normpath(os.path.join(root, tok))
        root_nc = os.path.normcase(root)
        root_sep = root_nc if root_nc.endswith(os.sep) else root_nc + os.sep
        full_nc = os.path.normcase(full)
        if not full_nc.startswith(root_sep):
            return None
        rel = os.path.relpath(full, root).replace("\\", "/")
        if not rel or rel == "." or rel.startswith(".."):
            return None
        if os.path.lexists(full):
            real_root = os.path.normcase(os.path.realpath(root))
            real_root_sep = real_root if real_root.endswith(os.sep) else real_root + os.sep
            if not os.path.normcase(os.path.realpath(full)).startswith(real_root_sep):
                return None                              # a link pointing outside
        return full, rel
    except (OSError, ValueError, TypeError):
        return None


def _spelled_out_paths(text, root):
    r"""Absolute paths under the project written out in full -- with spaces,
    which tokenising would shred. This repo lives under "bureau 2024\ALL\python
    perso", and the tool lines a stopped turn is filed with are exactly such
    paths ("Edit C:\...\python perso\proj\src\pen.js")."""
    out = []
    low = text.lower()
    for spelling in {root.lower(), root.lower().replace("\\", "/")}:
        start = 0
        while True:
            i = low.find(spelling, start)
            if i < 0:
                break
            j = i + len(spelling)
            end = j
            while end < len(text) and text[end] not in "\"'\n\r<>()[]{},;`":
                end += 1
            cand = text[j:end].strip()
            cand = re.sub(r"(:\d+)+$", "", cand).rstrip(".,:!? ")
            cand = cand.lstrip("\\/")
            for _ in range(_REF_PEEL + 1):
                if cand and os.path.exists(os.path.join(root, cand)):
                    out.append((cand, i, j + len(cand)))
                    break
                if " " not in cand:
                    break
                cand = cand.rsplit(" ", 1)[0].rstrip(".,:!?")
            start = j
    return out


def _harvest_refs(text, project_dir, cap):
    """The project paths a piece of text names, fingerprinted. Only paths
    inside the project are kept -- nothing outside it is ever stat'd, and a
    link the project contains that points outside is refused too.

    Paths that exist come first: a fact that says "e.g. 12/09/2026 and
    src/a.py" must stamp src/a.py, not the date."""
    if not project_dir or not text:
        return []
    try:
        root = os.path.abspath(str(project_dir))
        text = str(text)
    except (OSError, ValueError, TypeError):
        return []
    cands = []
    spelled = _spelled_out_paths(text, root)
    for cand, _i, _j in spelled:
        cands.append(cand)
    # ...and blanked out, so the fragments after a space are not read as
    # relative paths of their own ("proj/src/pen.js" from "my proj\src\pen.js").
    for _cand, i, j in sorted(spelled, key=lambda t: -t[1]):
        text = text[:i] + " " + text[j:]
    for tok in _REF_SPLIT_RE.split(text):
        tok = tok.strip().rstrip(".,:!?")
        tok = re.sub(r"(:\d+)+$", "", tok)          # path:12 / path:12:5
        if not tok or "://" in tok or "\x00" in tok:
            continue
        segs = re.split(r"[\\/]+", tok)
        pathy = ("/" in tok or "\\" in tok) and len(segs) >= 2 \
            and not all(re.match(r"^\d*$", x) for x in segs)      # 12/09/2026, 24/7
        stem, _dot, ext = tok.rpartition(".")
        filey = bool(_REF_FILE_RE.match(tok)) and tok.lower() not in _REF_NOT_FILES \
            and (len(stem) >= 2 or len(ext) >= 2)                  # e.g / i.e, not a.py
        pathy = pathy and not (len(segs) == 2 and all(len(x) <= 3 for x in segs)
                               and "." not in tok)                 # and/or, a/b
        if pathy or filey:
            cands.append(tok)
    found, seen = [], set()
    for tok in cands:
        got = _inside(root, tok)
        if not got:
            continue
        full, rel = got
        key = rel.lower()
        if key in seen:
            continue
        seen.add(key)
        found.append((rel, os.path.lexists(full)))
        if len(found) >= cap * 4:
            break
    # existing first, text order within each half
    found.sort(key=lambda t: 0 if t[1] else 1)
    return [_fingerprint(root, rel) for rel, _ex in found[:cap]]


def _ref_status(ref, project_dir, cache=None):
    """"" when the reference looks as it did, else what happened to it.
    `cache` (rel -> status) makes a second look in the same turn free."""
    try:
        rel = ref.get("p")
        if not isinstance(rel, str) or not rel:
            return ""
        if cache is not None and rel in cache:
            return cache[rel]
        status = _ref_status_now(ref, project_dir, rel)
        if cache is not None:
            cache[rel] = status
        return status
    except (OSError, ValueError, TypeError, AttributeError):
        return ""


def _ref_status_now(ref, project_dir, rel):
    full = os.path.join(project_dir, rel)
    exists = os.path.lexists(full)
    was = bool(ref.get("ex"))
    if was and not exists:
        return "gone since"
    if not was and exists:
        return "now on disk"
    if not exists:
        return ""
    if os.path.isdir(full):
        # A file replaced by a folder of its name is not the file any more.
        return "" if ref.get("sz") is None else "changed since"
    st = os.stat(full)
    if ref.get("sz") is None:
        return "changed since"                     # was a folder, is a file
    if ref.get("sz") != int(st.st_size):
        return "changed since"
    mt = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
    if ref.get("mt") == mt:
        return ""
    if ref.get("h") and st.st_size <= REF_HASH_MAX_BYTES:
        return "" if _content_hash(full) == ref.get("h") else "changed since"
    return "changed since"


def _shown(path):
    """A path short enough to show, cut at a folder boundary so the file's own
    name is always whole."""
    path = str(path or "")
    if len(path) <= REF_PATH_SHOW_CHARS:
        return path
    parts = path.split("/")
    tail = parts[-1]
    for i in range(len(parts) - 2, -1, -1):
        cand = "/".join(parts[i:])
        if len(cand) + 4 > REF_PATH_SHOW_CHARS:
            break
        tail = cand
    return ".../" + tail


def _marks(refs, project_dir, cache=None):
    marks = []
    if not isinstance(refs, (list, tuple)):
        return marks
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        status = _ref_status(ref, project_dir, cache)
        if status:
            marks.append("%s: %s" % (status, _shown(ref.get("p"))))
    return marks


def _join_marks(marks, limit):
    """Whole marks only: a mark cut in the middle of a path is a file that
    does not exist, and a mark silently dropped is a file reported as
    unchanged. What does not fit is counted."""
    out = []
    for i, m in enumerate(marks):
        cand = "; ".join(out + [m])
        rest = len(marks) - i - 1
        if len(cand) + (len("; +%d more" % rest) if rest else 0) > limit and out:
            return "; ".join(out) + "; +%d more" % (len(marks) - i)
        if len(cand) > limit and not out:
            return m[:limit - 8].rstrip() + "...; +%d more" % rest if rest else m[:limit - 3].rstrip() + "..."
        out.append(m)
    return "; ".join(out)


def _annotate(text, meta, project_dir, cache=None):
    """The fact, plus a status suffix for references that moved. A suffix,
    never a prefix: _is_always reads the first characters, and a status is
    worded as what happened, not as an instruction."""
    try:
        if not meta or not project_dir or not isinstance(meta, dict):
            return text
        marks = _marks(meta.get("refs"), project_dir, cache)
        if not marks:
            return text
        return text + " [" + _join_marks(marks, REF_SUFFIX_MAX_CHARS - 3) + "]"
    except Exception:                                            # noqa: BLE001
        return text


# What the reader is told once, when any mark fired. Zero cost on a clean turn.
_MARKS_LEGEND = ("(a [gone since / changed since / now on disk: path] mark means that "
                 "file moved after the fact was learned -- check it before relying on the fact)")


def _refs_line(refs, project_dir, lead, cache=None):
    try:
        if not refs or not project_dir:
            return ""
        marks = _marks(refs, project_dir, cache)
        if not marks:
            return ""
        return "%s: %s. Re-check these before continuing." % (
            lead, _join_marks(marks, REF_SUFFIX_MAX_CHARS * 2))
    except Exception:                                            # noqa: BLE001
        return ""


def _stamp(text, project_dir, cap=REF_MAX_PER_FACT):
    """Never raises: a stamp that cannot be taken is an empty stamp, and the
    fact is still remembered."""
    try:
        return {"at": time.time(), "rev": _project_rev(project_dir),
                "refs": _harvest_refs(text, project_dir, cap)}
    except Exception:                                            # noqa: BLE001
        return {"at": time.time(), "rev": None, "refs": []}


def remember_summary(session_id, text, project_dir=None):
    """Keep the running recap of what has already happened.

    This is what _compact_to_budget produces when it drops old turns, and until
    now it only ever lived in a RAM cache. Written whole rather than appended:
    a recap is a replacement for the turns it describes, not another turn."""
    text = (text or "").strip()
    if not text:
        return False
    rev = _project_rev(project_dir) if project_dir else None     # outside the lock
    with _LOCK:
        mem = get(session_id)
        mem["summary"] = text[:MAX_SUMMARY_CHARS]
        # The project's revision the recap describes, so a later turn can be
        # told "as of rev X; now Y" when the tree has moved under it -- and
        # the turn it was written on, so that is only said once the recap is
        # actually old (an agent that commits every turn moves HEAD every turn).
        mem["summary_rev"] = rev
        mem["summary_turn"] = int(mem.get("turns") or 0)
        return _save(mem)


def remember_fact(session_id, text, project_dir=None):
    """A decision or constraint worth not re-deciding.

    Deduplicated and bounded: the failure mode of a fact store is that it fills
    with restatements of the same thing until it is the context problem it was
    meant to solve. Oldest out first.

    With `project_dir`, the fact is stamped with the project paths it names
    and the project's revision (see REF_MAX_PER_FACT), so recall can say
    whether those references still look the same. Facts stay plain strings;
    the stamps live beside them in fact_meta, keyed by the text."""
    text = " ".join((text or "").replace("\x00", " ").split())[:MAX_FACT_CHARS]
    if not text:
        return False
    # The stamp stats files; taken before the lock, so a slow folder (a
    # cloud-synced one) cannot hold every other conversation's memory up.
    stamp = _stamp(text, project_dir) if project_dir else None
    with _LOCK:
        mem = get(session_id)
        facts = [f for f in mem.get("facts") or [] if isinstance(f, str)]
        meta = mem.get("fact_meta") if isinstance(mem.get("fact_meta"), dict) else {}
        if text in facts:
            if stamp:
                # A restatement is a re-assertion at the current state.
                meta[_fact_key(text)] = stamp
                mem["fact_meta"] = meta
                _save(mem)
            return True                        # already known; not an error
        facts.append(text)
        mem["facts"] = facts[-MAX_FACTS:]
        if stamp:
            meta[_fact_key(text)] = stamp
        keep = {_fact_key(f) for f in mem["facts"]}
        mem["fact_meta"] = {k: v for k, v in meta.items() if k in keep}
        return _save(mem)


def remember_recent(session_id, text, role="user"):
    """SHORT memory: a one-line trace of a turn that just happened.

    Not a transcript -- the model still has the real turns in its window. This
    is what survives the moment compaction drops them, so a conversation that
    was just truncated can still say what it was doing thirty seconds ago."""
    text = " ".join((text or "").split())[:MAX_RECENT_CHARS]
    if not text:
        return False
    with _LOCK:
        mem = get(session_id)
        recent = [r for r in (mem.get("recent") or []) if isinstance(r, dict)]
        recent.append({"role": str(role or "user")[:16], "text": text,
                       "at": time.time()})
        mem["recent"] = recent[-MAX_RECENT:]
        return _save(mem)


# --------------------------------------------------------------------------- #
# The task list
# --------------------------------------------------------------------------- #

def parse_checklist(text):
    """The markdown checklist in `text`, as [{"text", "done", "doing"}].

    Only checkbox lines count: a plain bullet list is prose, and turning every
    bullet in a reply into a task would make the list say things nobody
    decided. In order, capped, de-duplicated on the text."""
    out, seen = [], set()
    for line in (text or "").splitlines():
        m = _CHECK_RE.match(line)
        if not m:
            continue
        mark, body = m.group(1), " ".join(m.group(2).split())[:MAX_TASK_CHARS]
        key = body.lower()
        if not body or key in seen:
            continue
        seen.add(key)
        out.append({"text": body, "done": mark.lower() == "x",
                    "doing": mark in "~/>"})
        if len(out) >= MAX_TASKS:
            break
    return out


def tasks(session_id):
    """This conversation's task list, as last read. Never raises."""
    mem = get(session_id)
    return [t for t in (mem.get("tasks") or []) if isinstance(t, dict) and t.get("text")]


def set_tasks(session_id, items, source=""):
    with _LOCK:
        mem = get(session_id)
        mem["tasks"] = [{"text": str(t.get("text", ""))[:MAX_TASK_CHARS],
                         "done": bool(t.get("done")), "doing": bool(t.get("doing"))}
                        for t in (items or []) if isinstance(t, dict) and t.get("text")][:MAX_TASKS]
        mem["tasks_source"] = str(source or "")[:80]
        return _save(mem)


def update_tasks_from_text(session_id, text, source="reply"):
    """A checklist in the agent's reply replaces the list -- when it IS a
    list. One checkbox line is a sentence with a box in it, not a plan."""
    items = parse_checklist(text)
    if len(items) < 2:
        return False
    return set_tasks(session_id, items, source)


def update_tasks_from_project(session_id, project_dir):
    """The project's own PROGRESS.md (or TODO/PLAN/TASKS.md) wins over the
    reply: it is the copy the agent was told to keep, and it outlives the
    reply. Reads the first one that holds a checklist."""
    if not project_dir:
        return False
    for name in TASK_FILES:
        path = os.path.join(str(project_dir), name)
        try:
            if not os.path.isfile(path) or os.path.getsize(path) > TASK_FILE_MAX_BYTES:
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                items = parse_checklist(fh.read())
        except OSError:
            continue
        if items:
            return set_tasks(session_id, items, name)
    return False


def update_tasks(session_id, reply_text="", project_dir=None):
    """Both sources, file first. Returns which one was used, or ""."""
    if update_tasks_from_project(session_id, project_dir):
        return "file"
    if update_tasks_from_text(session_id, reply_text):
        return "reply"
    return ""


# --------------------------------------------------------------------------- #
# Where the work stopped
# --------------------------------------------------------------------------- #

def note_interrupted(session_id, request="", doing=(), partial="", why="stopped",
                     project_dir=None):
    """A turn ended before it finished: keep what it was doing, for the next
    one. `doing` is the turn's last tool calls in order; `partial` the text it
    had written."""
    # Stat'd before the lock, like a fact's stamp.
    refs = _stamp(" ".join(str(d) for d in list(doing or [])) + " " + (request or ""),
                  project_dir, REF_MAX_PER_INTERRUPT) if project_dir else {"rev": None, "refs": []}
    with _LOCK:
        mem = get(session_id)
        mem["interrupted"] = {
            "turn": int(mem.get("turns") or 0),
            "at": time.time(),
            "why": str(why or "stopped")[:40],
            "request": " ".join((request or "").split())[:300],
            "doing": [" ".join(str(d).split())[:MAX_TASK_CHARS] for d in list(doing or [])[-INTERRUPT_DOING:]],
            "partial": " ".join((partial or "").split())[-INTERRUPT_PARTIAL_CHARS:],
            # The files it was touching, as they were: the next turn is told
            # which of them moved since (see _refs_line in resume_block).
            "rev": refs.get("rev"),
            "refs": refs.get("refs") or [],
        }
        return _save(mem)


def clear_interrupted(session_id):
    with _LOCK:
        mem = get(session_id)
        if not mem.get("interrupted"):
            return False
        mem["interrupted"] = None
        return _save(mem)


def interrupted(session_id):
    mem = get(session_id)
    return mem.get("interrupted") if isinstance(mem.get("interrupted"), dict) else None


def resume_block(session_id, budget_chars=900, project_dir=None, _cache=None):
    """What the next turn is told about a stopped one, and about the list.

    Pinned ahead of everything else the memory hands over: a turn that starts
    over is the failure this exists to prevent, and it costs more than any
    fact it might displace. "" when there is nothing to say."""
    mem = get(session_id)
    lines = []
    cut = mem.get("interrupted") if isinstance(mem.get("interrupted"), dict) else None
    if cut:
        why = cut.get("why") or "stopped"
        lines += ["YOUR PREVIOUS TURN WAS %s BEFORE IT FINISHED."
                  % ("STOPPED BY THE USER" if why == "stopped" else "CUT SHORT (%s)" % why)]
        if cut.get("request"):
            lines.append("It was working on: " + cut["request"])
        # Early, before the tool lines and the partial text: _clip trims from
        # the end, and this line matters most on exactly the long interrupts
        # that get trimmed.
        moved = _refs_line(cut.get("refs"), project_dir, "Since it stopped", _cache)
        if moved:
            lines.append(moved)
        lines.append("Continue from exactly there: check what is already on disk, "
                     "do not start over, and do not redo what is done.")
        if cut.get("doing"):
            lines.append("Its last actions, in order:")
            lines += ["  - " + d for d in cut["doing"]]
        if cut.get("partial"):
            lines.append("What it had written so far: " + cut["partial"])
    items = [t for t in (mem.get("tasks") or []) if isinstance(t, dict) and t.get("text")]
    if items:
        done = sum(1 for t in items if t.get("done"))
        if lines:
            lines.append("")
        src = mem.get("tasks_source") or ""
        if src in TASK_FILES and project_dir and not os.path.isfile(os.path.join(str(project_dir), src)):
            src += " (file now missing)"
        lines.append("Task list (%d/%d done%s):"
                     % (done, len(items), (", from " + src) if src else ""))
        for t in items:
            mark = "x" if t.get("done") else ("~" if t.get("doing") else " ")
            lines.append("- [%s] %s" % (mark, t["text"]))
        lines.append("Keep this list updated as you work; mark each item as you finish it.")
    if not lines:
        return ""
    return _clip(lines, max(0, int(budget_chars or 0)), heading_lines=1)


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


def note_compaction(session_id):
    """Record that this conversation just lost turns to compaction.

    COMPACTION WAS SILENT. _compact_to_budget returns whether it dropped
    anything and both call sites threw that away, so nothing in the hub knew a
    conversation had just had its history cut -- including the part of the hub
    whose whole job is deciding when to say the standing rules again.

    That is the worst possible moment to stay quiet: the message carrying the
    instructions is exactly the kind of old turn compaction drops first. So a
    compaction makes the next turn due, whatever the schedule says.

    REPORTED as: "sometimes it's like the session gets full and I should go out
    from conversation and reopen it again to continue"."""
    with _LOCK:
        mem = get(session_id)
        mem["compactions"] = int(mem.get("compactions") or 0) + 1
        mem["restate_due"] = True
        return _save(mem)


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
    if mem.get("restate_due"):
        return True                    # compaction just ate them; do not wait
    turns = int(mem.get("turns") or 0)
    last = int(mem.get("rules_restated_turn") or 0)
    return turns > 0 and (turns - last) >= every


def mark_rules_restated(session_id):
    with _LOCK:
        mem = get(session_id)
        mem["rules_restated_turn"] = int(mem.get("turns") or 0)
        mem["restate_due"] = False
        return _save(mem)


def remember_project_fact(project_dir, text):
    """LONG memory, one level up: something true of the PROJECT.

    A session's facts die with the session. "This repo uses pnpm, not npm" is
    true tomorrow too, and re-learning it every conversation is the cost this
    removes. Stored under a hash of the folder, so it follows the project and
    not the chat."""
    return remember_fact(project_key(project_dir), text, project_dir=project_dir)


def project_facts(project_dir):
    return get(project_key(project_dir)).get("facts") or []


def forget_project(project_dir):
    return forget(project_key(project_dir))


# WHEN A LONG MEMORY IS WORTH ITS TOKENS.
#
# "long memory or context can be used only when really needed." Injecting
# every remembered fact into every turn is how a memory system becomes the
# context problem it was built to solve -- forty facts at 300 characters is
# 12,000 characters spent on a turn that asked one question.
#
# So a fact earns its place by OVERLAPPING WITH THE TURN. No embeddings and no
# index: term overlap against the current message, which is stdlib, instant,
# deterministic, and explains itself when it is wrong. The two exceptions are
# deliberate -- the job itself is always in scope, and so is anything the user
# marked as a rule.
_STOP = frozenset("""
the a an and or but if then else for of to in on at by with from as is are was
were be been being do does did doing have has had having this that these those
it its i you he she we they me him her them my your our their what which who
whom how why when where all any both each few more most other some such no nor
not only own same so than too very can will just should now please make use
using used need needs want file files code line lines
""".split())

# A fact is pinned into every turn when it looks like a standing rule or the
# job itself. These are the two things a conversation must never be without.
_ALWAYS = ("the original request:", "must ", "must:", "never ", "always ",
           "do not ", "don't ")

# How much of a fact's own vocabulary has to appear in the turn before it is
# worth sending. Low on purpose: a false positive costs a line, a false
# negative costs the model the one thing it needed.
RELEVANCE_MIN = 0.15


def _terms(text):
    out = set()
    for word in re.split(r"[^A-Za-z0-9_.+-]+", (text or "").lower()):
        word = word.strip("._+-")
        if len(word) >= 3 and word not in _STOP:
            out.add(word)
    return out


def _is_always(fact):
    low = (fact or "").lower()
    return any(low.startswith(m) or m in low[:40] for m in _ALWAYS)


def _relevant(facts, query, keep_always=True):
    """`facts` narrowed to what this turn is actually about.

    Returns them in their original order -- a memory is a list of decisions and
    reordering it by score would make the oldest and newest read the same."""
    if not query:
        return list(facts)
    qt = _terms(query)
    if not qt:
        return list(facts)
    out = []
    for f in facts:
        if keep_always and _is_always(f):
            out.append(f)
            continue
        ft = _terms(f)
        if not ft:
            continue
        if len(ft & qt) / float(len(ft)) >= RELEVANCE_MIN:
            out.append(f)
    return out


def _clip(parts, budget, heading_lines=1):
    """As many of `parts` as fit in `budget`, as whole lines where possible.

    TWO RULES, both learned from getting it wrong:

      * trimming back to the last newline threw the whole content line away.
        A summary is ONE long line, so "cut at 800 then trim to the newline"
        left the heading and nothing else. The last line is truncated instead,
        because half a sentence of recap beats none of it.
      * a section title with nothing under it spends characters to say less
        than silence would, so a block whose only survivor is its heading
        comes back empty.
    """
    lines = [x for x in parts if x]
    out, used = [], 0
    for line in lines:
        sep = 1 if out else 0
        if used + sep + len(line) <= budget:
            out.append(line)
            used += sep + len(line)
            continue
        room = budget - used - sep
        # Below this a fragment is noise rather than information.
        if room >= 40:
            out.append(line[:room])
        break
    if len([x for x in out if x.strip()]) <= heading_lines:
        return ""
    return "\n".join(out)


def remember_in(scope, text, project_dir=None):
    """Write one fact into a named scope. `scope` is any *_key() result."""
    return remember_fact(scope, text, project_dir=project_dir)


def facts_in(scope):
    return [f for f in (get(scope).get("facts") or []) if isinstance(f, str)]


def recall(scopes, query="", budget_chars=1200, project_dir=None, _cache=None):
    """The decisions from `scopes` that this turn is actually about.

    `scopes` is an ordered list of scope ids, NARROWEST FIRST -- task, agent,
    run, project, global. A fact seen in a narrower scope suppresses the same
    fact in a wider one, so a task-level decision overrides the project note it
    contradicts for as long as the task lasts.

    Narrowed by relevance to `query` (the turn being sent), because a memory
    that ships everything it has on every turn is the context problem it was
    meant to solve. Pass query="" to get everything, which is what a restate
    turn wants."""
    picked, seen = [], set()
    for scope in scopes:
        if not scope:
            continue
        mem_s = get(scope)
        metas = mem_s.get("fact_meta") if isinstance(mem_s.get("fact_meta"), dict) else {}
        facts = [f for f in (mem_s.get("facts") or []) if isinstance(f, str)]
        for f in _relevant(facts, query):
            k = " ".join(f.lower().split())
            if k in seen:
                continue        # the narrower scope already said it
            seen.add(k)
            picked.append((f, metas.get(_fact_key(f))))
    if not picked:
        return ""
    # Annotated BEFORE clipping, so the budget handed in still holds exactly.
    lines = ["- " + _annotate(f, m, project_dir, _cache) for f, m in picked]
    head = ["Decisions already made (do not re-litigate these):"]
    if any(ln != "- " + f for ln, (f, _m) in zip(lines, picked)):
        head.append(_MARKS_LEGEND)          # only when a mark fired
    return _clip(head + lines, budget_chars, heading_lines=len(head))


def context_block(session_id, budget_chars=2000, project_dir=None,
                  short=True, medium=True, long=True, query="",
                  run_id=None, task=None):
    """What this conversation carries into its next turn, or "".

    THREE HORIZONS, SPENT IN ORDER OF WHAT IS EXPENSIVE TO LOSE:

      long    project facts, then session facts. Short, and the costliest to
              re-derive -- a decision re-litigated is a whole exchange spent
              arriving back where the conversation already was.
      medium  the running summary. One paragraph standing in for the turns
              that compaction dropped.
      short   the last few turn traces. Cheapest, most perishable, and the
              first thing cut when the budget is tight.

    Each horizon gets a share of the budget (SHARE_LONG / MEDIUM / SHORT), and
    whatever a horizon does not spend is handed to the next one down -- so a
    conversation with no facts yet gives its whole budget to the summary
    instead of padding.

    The three can be switched off individually: a first turn has no history
    worth restating, and a caller that only wants the standing decisions can
    ask for long alone."""
    mem = get(session_id)
    # Narrowest first: task, agent, run, project, global. recall() de-duplicates
    # across them and drops whatever this turn is not about.
    scopes = [task_key(session_id, task), session_id, run_key(run_id),
              project_key(project_dir) if project_dir else None, GLOBAL_KEY]
    summary = (mem.get("summary") or "").strip()
    recent = [r for r in (mem.get("recent") or []) if isinstance(r, dict)]

    budget = max(0, int(budget_chars or 0))
    out, spent = [], 0

    # WHERE THE WORK STOPPED, AND THE LIST -- before any fact. See
    # resume_block: a turn that starts over costs more than anything else the
    # memory could hand over, so this is spent first and never trimmed for
    # the others' sake. Up to half the budget; a list that long is itself
    # the plan.
    cache = {}                # rel -> status, so nothing is stat'd twice per turn
    lead = resume_block(session_id, budget_chars=max(300, int(budget * 0.5)),
                        project_dir=project_dir, _cache=cache)
    if lead:
        out.append(lead)
        spent += len(lead)
        budget = max(0, budget - spent)
        spent = 0

    if long:
        # Served FIRST and trimmed LAST, so the share is a ceiling against
        # starving the others -- not a cap when there is nothing to starve.
        block = recall(scopes, query, budget, project_dir=project_dir, _cache=cache)
        if block:
            room = min(budget, max(int(budget * SHARE_LONG),
                                   block.find(chr(10), block.find(chr(10)) + 1) + 1
                                   if chr(10) in block else len(block)))
            block = recall(scopes, query, room, project_dir=project_dir, _cache=cache)
        if block:
            out.append(block)
            spent += len(block)

    # MEDIUM IS NOT FREE EITHER. The running summary is worth its ~4000
    # characters when the conversation has actually lost turns to compaction,
    # or when the caller asked for everything (a restate turn). On an ordinary
    # turn in a conversation that has never been compacted, the model still
    # has the real history in its window and the summary is a duplicate.
    if medium and summary and (not query or int(mem.get("compactions") or 0) > 0):
        room = int(budget * SHARE_MEDIUM) + (int(budget * SHARE_LONG) - spent
                                             if spent < int(budget * SHARE_LONG) else 0)
        heading = "What has happened so far:"
        # Said only once the recap is actually old: an agent that commits
        # every turn moves HEAD on the very turn the recap was written.
        old_enough = int(mem.get("turns") or 0) - int(mem.get("summary_turn") or 0) >= 2
        if project_dir and mem.get("summary_rev") and old_enough:
            now_rev = _project_rev(project_dir)
            if now_rev and now_rev != mem["summary_rev"]:
                heading = "What has happened so far (as of rev %s; now %s):" % (mem["summary_rev"], now_rev)
        block = _clip(["", heading, summary], max(0, room))
        if block:
            out.append(block)
            spent += len(block)

    if short and recent:
        room = budget - spent
        if room > 80:
            lines = ["", "The last few turns:"]
            lines += ["- %s: %s" % (r.get("role", "?"), r.get("text", ""))
                      for r in recent]
            block = _clip(lines, room)
            if block:
                out.append(block)

    return "\n".join(out).strip()


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
