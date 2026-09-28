"""The orchestrator: which model answers a conversation.

REQUESTED 2026-09-28: "make sure selecting the orchestrator is possible ... a
specific orchestrator model for each conversation", "I think set orchestrator
doesn't work", and "inside CLIs I want a new command called /orchestrator to
select the preferred LLM or select auto best one".

WHY "SET ORCHESTRATOR" DID NOTHING. The dashboard's button saved
config.set_default(), which only resolves a BARE or unknown model name. Every
CLI sends `auto`, `best` or a category, and those always went through the
difficulty router, which never read the default -- so the choice had no
visible effect. (The saved default on this install had been AUTO-picked long
ago -- groq/llama-3.3-70b -- so wiring that value into `auto` would have
silently replaced good routing with a weak model. The preference below is
only ever set by an explicit user action.)

TWO LEVELS, most specific first:
  * one conversation  -- the /agent picker, or `/orchestrator <model>` typed in
    that conversation (any CLI: the hub answers the command itself);
  * every conversation -- Settings / the Orchestrator page, or
    `/orchestrator all <model>`.
A choice is "auto" (the hub picks the best model per task) or "<pid>/<model>".
It is a PREFERENCE, not a cage: the chosen model opens the turn and the
ordinary fallback chain stays behind it, and a model that is off, blocked,
dead, rate-limited, too small for the conversation or unable to call the tools
the turn needs is skipped for that turn with the reason logged.

This module is the pure part (parsing, matching, the per-conversation store);
app.py owns routing and the replies.
"""
import json
import os
import re
import threading
import time

COMMAND = "/orchestrator"
GLOBAL_SETTING = "orchestrator_preferred"      # "<pid>/<model>" or absent = auto
AUTO = "auto"
STORE_NAME = "orchestrators.json"
STORE_MAX = 1000                               # conversations remembered
STORE_TTL = 60 * 86400                         # a conversation untouched this long forgets

# The command, as the WHOLE message: one line, optionally tagged by the CLI
# template ("[free-llm-hub] /orchestrator ..."). A sentence that merely
# mentions the command is never taken for it.
_CMD_RE = re.compile(r"^\s*(?:\[free-llm-hub\]\s*)?/orchestrator(?:[ \t]+([^\r\n]*))?\s*$", re.I)
_AUTO_WORDS = {"auto", "best", "automatic", "default-auto"}
_INHERIT_WORDS = {"reset", "clear", "inherit", "default", "none", "off"}
_ALL_WORDS = {"all", "global", "everywhere", "--all", "-a"}

# The CLI definitions the hub installs so the command shows up in each CLI's
# own command list (opencode: `command` in its config; see app.py).
OPENCODE_COMMAND = {
    "template": "[free-llm-hub] /orchestrator $ARGUMENTS",
    "description": "Calvoun hub: choose the model for this conversation "
                   "(a name, 'auto', or 'all <name>' for every conversation)",
}


def parse_command(text):
    """None, or {"action": "show"} / {"action": "set", "target": str|None,
    "scope": "conversation"|"all"}. target None = inherit the global choice
    (conversation scope) or auto (all)."""
    if not isinstance(text, str):
        return None
    m = _CMD_RE.match(text)
    if not m:
        return None
    words = (m.group(1) or "").split()
    if not words or words[0].lower() in ("?", "help", "status", "list", "show"):
        return {"action": "show"}
    scope = "conversation"
    if words[0].lower() in _ALL_WORDS:
        scope, words = "all", words[1:]
    elif words[-1].lower() in _ALL_WORDS:
        scope, words = "all", words[:-1]
    query = " ".join(words).strip()
    if not query:
        return {"action": "show"}
    low = query.lower()
    if low in _AUTO_WORDS:
        return {"action": "set", "target": AUTO, "scope": scope}
    if low in _INHERIT_WORDS:
        return {"action": "set", "target": None, "scope": scope}
    return {"action": "set", "target": query, "scope": scope}


def is_command(text):
    return parse_command(text) is not None


def split_choice(choice):
    """("pid", "model") for "pid/model", else None."""
    if not isinstance(choice, str) or "/" not in choice:
        return None
    pid, _sep, model = choice.partition("/")
    pid, model = pid.strip(), model.strip()
    return (pid, model) if pid and model else None


def _norm(s):
    return re.sub(r"[\s_]+", "-", str(s or "").strip().lower())


def match_model(query, live):
    """The (pid, model) `query` names among `live` [(pid, model, tools_ok,
    score)], or (None, candidates) when nothing or several equally good
    things match. Exact "pid/model" first, then the model id or its last
    segment, then every word of the query contained in "pid/model"; among
    equals, one that can call tools, then the higher score."""
    q = _norm(query)
    if not q:
        return None, []
    rows = [(p, m, bool(t), float(s or 0)) for p, m, t, s in live or ()]

    def best(cands):
        cands.sort(key=lambda r: (not r[2], -r[3], r[0], r[1]))
        return cands

    exact = [r for r in rows if _norm(r[0] + "/" + r[1]) == q]
    if exact:
        return (exact[0][0], exact[0][1]), []
    leaf = [r for r in rows if _norm(r[1]) == q or _norm(r[1].rsplit("/", 1)[-1]) == q
            or _norm(r[1].rsplit(":", 1)[-1]) == q]
    if leaf:
        leaf = best(leaf)
        return (leaf[0][0], leaf[0][1]), []
    words = [w for w in re.split(r"[\s/]+", q) if w]
    part = [r for r in rows if all(w in _norm(r[0] + "/" + r[1]) for w in words)]
    if not part:
        return None, []
    part = best(part)
    return (part[0][0], part[0][1]), [p + "/" + m for p, m, _t, _s in part[1:6]]


class ConversationStore:
    """{conversation key: {"choice": "auto"|"pid/model", "at": ts}} on disk,
    newest last, bounded (STORE_MAX, STORE_TTL). Thread-safe; never raises."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._data = None

    def _load(self):
        if self._data is None:
            try:
                with open(self.path, encoding="utf-8") as fh:
                    raw = json.load(fh)
                self._data = raw if isinstance(raw, dict) else {}
            except (OSError, ValueError):
                self._data = {}
        return self._data

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def get(self, key):
        if not key:
            return None
        with self._lock:
            row = self._load().get(key)
            if not isinstance(row, dict):
                return None
            if time.time() - float(row.get("at") or 0) > STORE_TTL:
                self._data.pop(key, None)
                return None
            return row.get("choice")

    def set(self, key, choice):
        """choice None forgets the conversation's own pick (inherit)."""
        if not key:
            return
        with self._lock:
            data = self._load()
            data.pop(key, None)
            if choice:
                data[key] = {"choice": choice, "at": time.time()}
            while len(data) > STORE_MAX:
                data.pop(next(iter(data)))
            self._save()

    def count(self):
        with self._lock:
            return len(self._load())
