"""Independent verifier + corrector -- the pure half.

Sakana's Trinity loop, applied to one agent step: a PRODUCER model proposes
the next assistant message; a VERIFIER from a DIFFERENT model family reads a
short digest (the user's instruction, the latest tool results, the proposal)
and answers ACCEPT or REVISE as a small JSON verdict; on REVISE a CORRECTOR is
shown the conversation, the draft and the verifier's problems and writes the
corrected next message.

A different FAMILY, not just a different host: two hosts serving the same
weights (or two sizes of one family trained on the same data) share the same
blind spots, so a check by one of them is mostly a self-check.

This module makes no model call and imports nothing from app.py (only the pure
ctxwin helpers): app.py decides when to verify, dispatches the verifier with
`digest()` / VERIFY_MAX_TOKENS, reads the reply with `parse_verdict()` and, on
REVISE, dispatches `corrector_messages()`.

Fail-open everywhere: an unreadable verdict is an ACCEPT (marked "unparsed"),
so a broken verifier can only cost a call, never block a turn.
"""
import ast
import json
import math
import re

import ctxwin

VERIFY_MAX_TOKENS = 300
DIGEST_MAX_CHARS = 20000            # ~5K tokens

# --------------------------------------------------------------------------- #
# Model identity and family
# --------------------------------------------------------------------------- #

# Mirrors app._normalize_model_identity (same regexes, same order) so a model
# is ONE identity here and in routing; tests/test_verify.py compares the two
# over every spelling it checks. Read that function's comments for why a colon
# is a host on the left and an Ollama tag on the right.
_MODEL_ID_SUFFIX_RE = re.compile(r"(?::free|:beta|:extended|:nitro|:floor|:online)+$", re.IGNORECASE)
_FREE_TIER_SUFFIX_RE = re.compile(r"-free$")
_OLLAMA_TAG_RE = re.compile(
    r"^(?:latest|instruct|chat|code|text|base|"
    r"v?\d+(?:\.\d+)*|"
    r"\d+(?:\.\d+)?[bkm](?:-[a-z0-9._\-]+)?|"
    r"[qf]p?\d+(?:_[a-z0-9]+)*|bf16|fp16)$",
    re.IGNORECASE)


def _path(model_id):
    """The id with provider suffixes, Ollama tags and relay/host prefixes
    removed, namespace kept: 'srv_x:anthropic/claude-sonnet-4:free' ->
    'anthropic/claude-sonnet-4'."""
    base = model_id if isinstance(model_id, str) else ""
    base = _MODEL_ID_SUFFIX_RE.sub("", base.strip().lower())
    base = _FREE_TIER_SUFFIX_RE.sub("", base)
    while ":" in base:
        head, tail = base.rsplit(":", 1)
        if not head or not _OLLAMA_TAG_RE.match(tail):
            break
        base = head
    while ":" in base:
        head, tail = base.split(":", 1)
        if not tail:
            break
        base = tail
    return base


def identity(model_id):
    """The hub's model identity (the leaf of `_path`), equal to
    app._normalize_model_identity(model_id)."""
    return _path(model_id).rsplit("/", 1)[-1]


# Checked IN ORDER against the identity; the first hit wins. The order is the
# point where a name carries two vendors:
#   nemotron before llama/qwen/mistral  NVIDIA post-trains other bases
#                                       ('llama-3.3-nemotron-super-49b');
#   gpt-oss before gpt                  open weights, a different model line;
#   gemma before gemini                 separate families by design;
#   claude before gemini                g4f spells 'gemini-claude-opus-4-6-thinking'
#                                       for a Claude model;
#   deepseek before qwen/llama          'deepseek-r1-distill-llama-70b' answers
#                                       in DeepSeek's trained style.
_FAMILY_PATTERNS = tuple((name, re.compile(rx)) for name, rx in (
    ("nemotron", r"nemotron"),
    ("gpt-oss", r"gpt[-_.]?oss|^openai-fast$"),        # pollinations openai-fast = gpt-oss-20b
    ("gemma", r"gemma"),
    ("claude", r"claude"),
    ("kimi", r"kimi|moonshot"),
    ("glm", r"(?<![a-z])(?:chat)?glm|^zai-org-"),
    ("minimax", r"minimax|(?<![a-z])abab\d"),
    ("mimo", r"(?<![a-z])mimo(?![a-z])"),
    ("deepseek", r"deepseek|(?<![a-z])dsv\d|(?<![a-z])ds-r1(?![0-9])"),
    ("qwen", r"qwen|(?<![a-z])qwq|(?<![a-z])qvq"),
    ("gemini", r"gemini"),
    ("llama", r"llama"),
    ("mistral", r"mistral|mixtral|codestral|devstral|magistral|ministral|pixtral|voxtral"),
    ("grok", r"(?<![a-z])grok"),
    ("cohere", r"cohere|c4ai|(?<![a-z])command-(?:r|a|light|nightly)(?![a-z])|(?<![a-z])aya-"),
    ("phi", r"(?<![a-z])phi-?\d"),
    ("gpt", r"(?<![a-z])(?:chat)?gpt|^o\d(?:$|-)|^openai(?:$|-)"),
))

# A vendor namespace names the family when the leaf alone does not
# ('openai/o3' is caught by the leaf; 'moonshotai/moonlight-16b' is not).
_NAMESPACE_FAMILY = {
    "moonshotai": "kimi", "moonshot": "kimi",
    "z-ai": "glm", "zai-org": "glm", "zai": "glm", "thudm": "glm", "zhipuai": "glm",
    "deepseek-ai": "deepseek", "deepseek": "deepseek",
    "qwen": "qwen", "alibaba": "qwen",
    "google": "gemini",
    "meta-llama": "llama", "meta": "llama",
    "mistralai": "mistral", "mistral": "mistral",
    "anthropic": "claude",
    "openai": "gpt",
    "minimax": "minimax", "minimaxai": "minimax",
    "xiaomi": "mimo", "xiaomimimo": "mimo",
    "x-ai": "grok", "xai": "grok",
    "cohere": "cohere", "coherelabs": "cohere", "cohereforai": "cohere",
}


def family(model_id):
    """Vendor family of a model id in any spelling the hub sees (relay and host
    prefixes, ':free' / '-free' suffixes, Ollama tags, case): one of kimi, glm,
    qwen, deepseek, gemini, gemma, llama, mistral, claude, gpt, gpt-oss,
    minimax, mimo, nemotron, grok, cohere, phi -- else
    'unknown:<identity>' (each unknown model is its own family)."""
    path = _path(model_id)
    leaf = path.rsplit("/", 1)[-1]
    for name, rx in _FAMILY_PATTERNS:
        if rx.search(leaf):
            return name
    for ns in reversed(path.split("/")[:-1]):
        fam = _NAMESPACE_FAMILY.get(ns.lstrip("@"))
        if fam:
            return fam
    return "unknown:" + leaf


# --------------------------------------------------------------------------- #
# Choosing the verifier
# --------------------------------------------------------------------------- #

def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return v if math.isfinite(v) else 0.0


def pick_verifier(producer, candidates):
    """(pid, model) of the verifier for `producer` = (pid, model), or None.

    `candidates` = [(pid, model, score), ...], already filtered to usable by
    the caller. Preference, in order: a different family; a different model
    (same weights on another host is a self-check); a different provider;
    the higher score. The producer itself (same pid, same identity) is never
    picked, so a pool holding only the producer gives None. Ties keep the
    caller's order."""
    try:
        ppid, pmodel = producer[0], producer[1]
    except (TypeError, IndexError, KeyError):
        ppid, pmodel = None, None
    pfam, pident = family(pmodel), identity(pmodel)
    best, best_key = None, None
    for c in candidates or ():
        try:
            pid, model = c[0], c[1]
        except (TypeError, IndexError, KeyError):
            continue
        score = c[2] if len(c) > 2 else 0
        ident = identity(model)
        if pid == ppid and ident == pident:
            continue
        key = (family(model) != pfam, ident != pident, pid != ppid, _num(score))
        if best_key is None or key > best_key:
            best, best_key = (pid, model), key
    return best


# --------------------------------------------------------------------------- #
# Which steps are worth a check
# --------------------------------------------------------------------------- #

# Name words of a tool that changes something. Names are split on case and
# punctuation first: 'MultiEdit' -> multi, edit; 'apply_patch' -> apply, patch;
# 'mcp__fs__write_file' -> mcp, fs, write, file.
_RISKY_NAME_TOKENS = frozenset((
    "write", "edit", "patch", "apply", "exec", "execute", "run", "bash", "shell", "sh",
    "zsh", "powershell", "pwsh", "cmd", "terminal", "command", "delete", "del",
    "remove", "rm", "rmdir", "unlink", "move", "mv", "rename", "create", "insert",
    "replace", "overwrite", "append", "save", "mkdir", "kill", "install",
    "uninstall", "commit", "push", "deploy", "drop", "truncate", "multiedit",
))
# Bookkeeping tools whose name happens to hold a risky word ('TodoWrite',
# Claude Code's 'BashOutput' which only READS a background shell).
_SAFE_NAMES = frozenset(("bashoutput", "bash_output", "todowrite", "todo_write", "todoread",
                         "todo_read", "write_todos", "update_plan"))
_SAFE_NAME_TOKENS = frozenset(("todo", "todos"))

# Argument keys that carry new file content, an edit or code to run.
# Compared with '_' / '-' removed and lowercased, so opencode's camelCase
# (`oldString`, `newString`) matches too.
_WRITE_ARG_KEYS = frozenset((
    "content", "contents", "filetext", "newstring", "newstr", "oldstring", "oldstr",
    "patch", "diff", "edits", "newsource", "inserttext", "codeedit", "newcontent",
    "newtext", "replacement", "code"))
_ACTION_ARG_KEYS = ("action", "operation", "op", "mode", "method")
_DESTRUCTIVE_ACTIONS = frozenset((
    "delete", "remove", "rm", "write", "create", "overwrite", "append", "move",
    "rename", "edit", "patch", "exec", "execute", "run", "kill", "drop", "truncate",
))
_COMMAND_ARG_KEYS = ("command", "cmd", "script")
_PATCH_TEXT_RE = re.compile(
    r"^\*\*\* (?:Begin Patch|Add File:|Update File:|Delete File:)|"
    r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", re.M)

# A shell command made only of these (and no output redirect, no command
# substitution) changes nothing. Editor tools' read verbs ('view') too: the
# Anthropic text editor takes {"command": "view", "path": ...}.
_READ_ONLY_CMDS = frozenset((
    "ls", "dir", "cat", "type", "head", "tail", "less", "more", "grep", "rg", "ag",
    "egrep", "fgrep", "find", "fd", "tree", "pwd", "wc", "echo", "printf", "which",
    "where", "whoami", "stat", "file", "du", "df", "printenv", "date", "uname",
    "sort", "uniq", "cut", "diff", "cmp", "nl", "realpath", "basename", "dirname",
    "jq", "sed", "git", "cd", "pushd", "popd", "true", "get-content", "gc",
    "get-childitem", "gci", "select-string", "sls", "get-item", "test-path",
    "get-location", "resolve-path", "measure-object", "set-location", "select-object",
    "where-object", "sort-object", "format-table", "format-list", "out-string",
    "write-output",
    "view", "read", "list", "get", "show", "search", "status",
))
_READ_ONLY_GIT = frozenset(("status", "diff", "log", "show", "rev-parse", "ls-files", "blame",
                            "grep", "describe", "shortlog", "remote", "branch"))
# `git branch` / `git remote` only read when every further word is one of these
# (`git branch feature` creates a branch, `git remote add` adds a remote).
_READ_ONLY_GIT_LISTING = frozenset(("-a", "-r", "-v", "-vv", "--all", "--remotes", "--list",
                                    "--show-current", "--verbose", "show", "get-url"))
_SHELLS = frozenset(("bash", "sh", "zsh", "dash", "pwsh", "powershell", "cmd"))
_HARMLESS_REDIRECT_RE = re.compile(r"\d?>&\d|\d?>>?\s*(?:/dev/null|\$null|nul)\b", re.I)
_SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||[;|\n]")
_SHELL_WRAP_RE = re.compile(
    r"^\s*(?:bash|sh|zsh)\s+-l?c\s+(['\"])(.*)\1\s*$|"
    r"^\s*(?:pwsh|powershell)(?:\.exe)?\s+(?:-\w+\s+)*-c(?:ommand)?\s+(['\"])(.*)\3\s*$",
    re.S | re.I)
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")


def _norm_key(key):
    return re.sub(r"[_\-]", "", str(key)).lower()


def _name_tokens(name):
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name or "")
    return [t for t in re.split(r"[^a-z0-9]+", s.lower()) if t]


def _parse_args(raw):
    if isinstance(raw, str):
        s = raw.strip()
        if s[:1] in ("{", "["):
            try:
                return json.loads(s)
            except ValueError:
                return raw
    return raw


def _call_name_args(call):
    """(name, args) of a tool call in the OpenAI chat shape, the Anthropic
    tool_use shape or the Responses function_call / custom_tool_call /
    local_shell_call shapes. args is a dict / list when it parses, else the
    raw value."""
    if not isinstance(call, dict):
        return "", None
    if call.get("type") == "local_shell_call":
        action = call.get("action") or {}
        return "local_shell", {"command": action.get("command") if isinstance(action, dict) else action}
    fn = call.get("function") if isinstance(call.get("function"), dict) else None
    src = fn if fn is not None else call
    name = src.get("name") if isinstance(src.get("name"), str) else ""
    for key in ("arguments", "input", "args", "parameters"):
        if key in src:
            return name, _parse_args(src.get(key))
    return name, None


def _command_text(value):
    """A shell command as one string; a bash -lc / powershell -Command wrapper
    is unwrapped to the script it runs."""
    if isinstance(value, (list, tuple)):
        parts = [str(p) for p in value]
        if len(parts) >= 2:
            exe = re.split(r"[\\/]", parts[0])[-1].lower()
            exe = exe[:-4] if exe.endswith(".exe") else exe
            if exe in _SHELLS:
                return parts[-1]
        return " ".join(parts)
    if not isinstance(value, str):
        return ""
    m = _SHELL_WRAP_RE.match(value)
    if m:
        return m.group(2) if m.group(2) is not None else (m.group(4) or "")
    return value


def _command_is_read_only(cmd):
    """True when every part of a shell command only reads."""
    cmd = _command_text(cmd).strip()
    if not cmd:
        return True
    if "`" in cmd or "$(" in cmd:
        return False
    if ">" in _HARMLESS_REDIRECT_RE.sub("", cmd):
        return False
    for seg in _SEGMENT_SPLIT_RE.split(cmd):
        words = seg.split()
        while words and _ENV_ASSIGN_RE.match(words[0]):
            words = words[1:]
        if not words:
            continue
        exe = re.split(r"[\\/]", words[0].strip("'\""))[-1].lower()
        exe = exe[:-4] if exe.endswith(".exe") else exe
        if exe not in _READ_ONLY_CMDS:
            return False
        rest = [w.lower() for w in words[1:]]
        if exe == "git":
            sub = next((w for w in rest if not w.startswith("-")), "")
            if sub not in _READ_ONLY_GIT:
                return False
            if sub in ("branch", "remote"):
                after = rest[rest.index(sub) + 1:]
                if sub == "remote" and after[:1] == ["show"]:
                    after = after[2:]                 # `git remote show origin`
                if any(w not in _READ_ONLY_GIT_LISTING for w in after):
                    return False
        elif exe == "find":
            if any(w in ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf")
                   for w in rest):
                return False
        elif exe == "sed":
            if any(w == "-i" or w.startswith("-i") or w.startswith("--in-place") for w in rest):
                return False
    return True


def _string_values(obj, depth=0):
    if depth > 3:
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _string_values(v, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _string_values(v, depth + 1)


def _call_is_risky(call):
    """True for a tool call that writes, edits, patches, deletes or runs
    something; False for read-only tools (read, grep, glob, list, a plainly
    read-only shell command, an editor 'view')."""
    name, args = _call_name_args(call)
    toks = set(_name_tokens(name))
    if name.lower() in _SAFE_NAMES or toks & _SAFE_NAME_TOKENS:
        return False
    if any(isinstance(s, str) and _PATCH_TEXT_RE.search(s) for s in _string_values(args)):
        return True
    command = None
    if isinstance(args, dict):
        for key, value in args.items():
            if _norm_key(key) in _WRITE_ARG_KEYS and value not in (None, "", [], {}):
                return True
        for key in _ACTION_ARG_KEYS:
            v = args.get(key)
            if isinstance(v, str) and v.strip().lower() in _DESTRUCTIVE_ACTIONS:
                return True
        for key in _COMMAND_ARG_KEYS:
            if args.get(key) not in (None, "", []):
                command = args.get(key)
                break
    if command is not None:
        return not _command_is_read_only(command)
    return bool(toks & _RISKY_NAME_TOKENS)


# A final text claiming the work is finished / verified.
_CLAIM_RE = re.compile(
    r"\b(?:all\s+(?:the\s+|of\s+the\s+)?)?(?:\d+\s+)?(?:unit\s+)?tests?\s+(?:suite\s+)?"
    r"(?:now\s+|are\s+(?:now\s+)?|have\s+(?:now\s+)?|all\s+)?"
    r"(?:pass(?:es|ed|ing)?|succeed(?:s|ed)?|green)\b"
    r"|\b\d+\s+passed\b"
    r"|\b(?:the\s+)?(?:build|compilation|lint(?:er|ing)?|ci|type[- ]?check)\s+(?:now\s+)?"
    r"(?:pass(?:es|ed)?|succeed(?:s|ed)?|is\s+(?:now\s+)?green|is\s+clean)\b"
    r"|\bI(?:'ve|\s+have)\s+(?:now\s+)?(?:successfully\s+)?"
    r"(?:fixed|implemented|completed|finished|resolved|verified|added|updated|created|refactored)\b"
    r"|\bsuccessfully\s+(?:implemented|fixed|completed|added|updated|created|resolved|refactored)\b"
    r"|\b(?:bug|issue|problem|error|task|feature|fix|change)s?\s+(?:is|are|has\s+been|have\s+been)\s+"
    r"(?:now\s+)?(?:fixed|resolved|completed?|done|implemented|working)\b"
    r"|\b(?:everything|it|this|that)\s+(?:now\s+)?works\b"
    r"|\b(?:is|are)\s+now\s+(?:working|fixed|done|complete)\b"
    r"|^\s*(?:all\s+)?(?:done|fixed|finished|completed?)\s*[.!]*\s*$"
    r"|\b(?:tous\s+les\s+|les\s+)tests\s+passent\b"
    r"|\b(?:c'est|est|sont)\s+(?:maintenant\s+)?(?:corrig|termin|r[ée]solu)\w*"
    r"|\bfonctionne\s+(?:maintenant|d[ée]sormais)\b",
    re.I | re.M)
# A claim word inside a plan or a condition is not a claim ("let me make sure
# the tests pass", "until all tests pass", "not all tests pass").
_CLAIM_HEDGE_RE = re.compile(
    r"(?:\b(?:not|never|no|whether|if|until|once|ensure|sure|check|verify|confirm|see|"
    r"should|could|would|might|may|make|can|to|will|when|unless|que|si)|n't)"
    r"[ \t]+(?:[\w'-]+[ \t]+){0,3}$",
    re.I)
_FAILURES_REPORTED_RE = re.compile(r"\b[1-9]\d*\s+(?:failed|failing|errors?|failures?)\b", re.I)


def _claims_done(text):
    if not isinstance(text, str) or not text.strip():
        return False
    failures = bool(_FAILURES_REPORTED_RE.search(text))
    for m in _CLAIM_RE.finditer(text):
        if failures and re.match(r"\d+\s+passed", m.group(0), re.I):
            continue
        before = text[max(0, m.start() - 40):m.start()]
        if _CLAIM_HEDGE_RE.search(before):
            continue
        return True
    return False


def _content_text_and_calls(content):
    if isinstance(content, str):
        return content, []
    text, calls = [], []
    if isinstance(content, list):
        for p in content:
            if not isinstance(p, dict):
                continue
            if p.get("type") in ("tool_use", "function_call", "custom_tool_call", "local_shell_call"):
                calls.append(p)
            elif isinstance(p.get("text"), str):
                text.append(p["text"])
    return "\n".join(text), calls


def _calls_and_text(proposed):
    """(tool_calls, text) of a proposal given as an assistant message dict, a
    list of tool calls (or content parts), a single tool call, or a string."""
    if proposed is None:
        return [], ""
    if isinstance(proposed, str):
        return [], proposed
    if isinstance(proposed, dict):
        if "tool_calls" in proposed or "content" in proposed or proposed.get("role"):
            text, part_calls = _content_text_and_calls(proposed.get("content"))
            calls = [c for c in (proposed.get("tool_calls") or []) if isinstance(c, dict)]
            return calls + part_calls, text
        return [proposed], ""
    if isinstance(proposed, (list, tuple)):
        text = [p["text"] for p in proposed
                if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)]
        calls = [p for p in proposed if isinstance(p, dict) and p.get("type") != "text"]
        return calls, "\n".join(text)
    return [], ""


def is_risky(tool_calls, difficulty, observed_pass=None):
    """Is this proposed step worth an independent check?

    `tool_calls` is the list of tool calls, or the whole proposed assistant
    message (dict with content + tool_calls), or its final text (str).

    True when
      * difficulty is "hard" and a call writes / edits / patches / deletes /
        runs something (by function name AND by argument shape -- a generic
        tool carrying `content`, `new_string`, a patch, or a command that is
        not plainly read-only); read-only tools (read, grep, glob, list, a
        `ls` / `cat` / `git status` command, an editor `view`) never count;
      * or the text claims the work is done / fixed / tests pass while
        `observed_pass` is not True (the hub did not see the passing run).
        Applies to every difficulty except "simple" (one-word replies and the
        hub's own probes are not verified)."""
    calls, text = _calls_and_text(tool_calls)
    if difficulty == "hard" and any(_call_is_risky(c) for c in calls):
        return True
    if difficulty != "simple" and observed_pass is not True and _claims_done(text):
        return True
    return False


# --------------------------------------------------------------------------- #
# What the verifier reads
# --------------------------------------------------------------------------- #

VERIFIER_SYSTEM = (
    "You are an independent reviewer. A different AI model proposed the NEXT assistant "
    "message in a conversation with a user; nothing in it has been executed or shown to "
    "the user yet. You see the user's latest instruction, the most recent tool results "
    "and the proposal.\n"
    "Check only whether the proposal is correct and safe for that instruction: a wrong, "
    "incomplete or broken edit; a command that would damage files or data; doing something "
    "that was not asked; ignoring the instruction; or claiming success (done, fixed, tests "
    "pass) that the tool results do not show. Do not judge style or wording and do not "
    "rewrite the proposal. When what you are shown is not enough to tell, accept it.\n\n"
    "Reply in EXACTLY this format, starting with the VERDICT line (no preamble, no "
    "thinking out loud before it):\n"
    "VERDICT: ACCEPT\n"
    "or\n"
    "VERDICT: REVISE\n"
    "PROBLEMS:\n"
    "- one short, concrete sentence (at most 5 lines)\n"
    "SEVERITY: high\n\n"
    "REVISE when the proposal must be changed. SEVERITY is high when following the "
    "proposal would break code or data, ignore the instruction or report a success that "
    "did not happen; low otherwise. An ACCEPT reply is the single line VERDICT: ACCEPT."
)
# The retry contract for a verifier whose first reply could not be read: one
# line, nothing else a weak model can get wrong.
STRICT_VERIFIER_SYSTEM = (
    "You are an independent reviewer of a proposed next AI message. Answer with ONE "
    "line and nothing else:\n"
    "VERDICT: ACCEPT\n"
    "or, only if it is wrong, broken or unsafe for the user's instruction:\n"
    "VERDICT: REVISE - <the main problem in one sentence>\n"
    "When what you see is not enough to tell, answer VERDICT: ACCEPT."
)
_NO_INSTRUCTION = "(no user instruction found)"


def _clip_middle(text, limit, head_share=0.6):
    """`text` cut to at most `limit` chars, keeping its head and tail with a
    marker naming how much of the middle was left out."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    probe = "\n[... %d chars omitted ...]\n" % len(text)
    room = limit - len(probe)
    if room < 40:
        return text[:limit]
    head = int(room * head_share)
    tail = room - head
    marker = "\n[... %d chars omitted ...]\n" % (len(text) - head - tail)
    return text[:head] + marker + (text[-tail:] if tail > 0 else "")


def _allocate(lengths, weights, budget):
    """Weighted water-filling: every part gets what it needs if it fits its
    share; what short parts leave over goes to the long ones. Sum <= budget."""
    alloc = [0] * len(lengths)
    active = [i for i, n in enumerate(lengths) if n > 0]
    remaining = max(0, budget)
    while active and remaining > 0:
        wsum = float(sum(weights[i] for i in active))
        fits = [i for i in active if lengths[i] <= remaining * weights[i] / wsum]
        if fits:
            for i in fits:
                alloc[i] = lengths[i]
                remaining -= lengths[i]
                active.remove(i)
            continue
        for i in active:
            alloc[i] = int(remaining * weights[i] / wsum)
        break
    return alloc


def _last_instruction(messages):
    last_user = ""
    for m in reversed(messages or []):
        if ctxwin.is_real_instruction(m):
            return ctxwin.instruction_text(ctxwin.message_text(m))
        if not last_user and isinstance(m, dict) and m.get("role") == "user":
            last_user = ctxwin.message_text(m).strip()
    return last_user


def _tool_names_by_id(messages):
    names = {}
    for m in messages or []:
        if isinstance(m, dict) and m.get("role") == "assistant":
            for c in m.get("tool_calls") or []:
                if isinstance(c, dict) and c.get("id"):
                    names[c["id"]] = _call_name_args(c)[0]
    return names


def _recent_tool_results(messages, n=2):
    """[(tool name, text)] of the last `n` tool results, oldest first: role
    "tool" / "function" messages and Anthropic tool_result content parts."""
    names = _tool_names_by_id(messages)
    found = []
    for m in reversed(messages or []):
        if len(found) >= n:
            break
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role in ("tool", "function"):
            name = m.get("name") or names.get(m.get("tool_call_id")) or "tool"
            found.append((name, ctxwin.message_text(m)))
        elif role == "user" and isinstance(m.get("content"), list):
            for p in reversed(m["content"]):
                if len(found) >= n:
                    break
                if isinstance(p, dict) and p.get("type") == "tool_result":
                    c = p.get("content")
                    text = c if isinstance(c, str) else ctxwin.message_text({"content": c})
                    found.append((names.get(p.get("tool_use_id")) or "tool", text))
    return list(reversed(found))


def _args_text(call):
    fn = call.get("function") if isinstance(call.get("function"), dict) else call
    raw = None
    for key in ("arguments", "input", "args", "parameters"):
        if key in fn:
            raw = fn.get(key)
            break
    if call.get("type") == "local_shell_call":
        raw = call.get("action")
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    try:
        return json.dumps(raw, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(raw)


def _render_proposal(proposed):
    calls, text = _calls_and_text(proposed)
    parts = []
    if text.strip():
        parts.append("TEXT:\n" + text.strip())
    for i, c in enumerate(calls, 1):
        name = _call_name_args(c)[0] or "(unnamed)"
        parts.append("TOOL CALL %d: %s\nARGUMENTS: %s" % (i, name, _args_text(c)))
    return "\n\n".join(parts) or "(empty message)"


def digest(messages, proposed, strict=False):
    """OpenAI chat messages for the verifier: the system contract plus ONE
    user message holding the last real user instruction (CLI wrapper blocks
    removed), the last 2 tool results and the proposed next assistant message
    (text and/or tool calls), each cut head+tail so the whole digest stays
    within DIGEST_MAX_CHARS. Dispatch with max_tokens=VERIFY_MAX_TOKENS.
    `strict`: the one-line retry contract (STRICT_VERIFIER_SYSTEM)."""
    system = STRICT_VERIFIER_SYSTEM if strict else VERIFIER_SYSTEM
    instruction = _last_instruction(messages) or _NO_INSTRUCTION
    results = _recent_tool_results(messages, 2)
    proposal = _render_proposal(proposed)

    headers = ["USER INSTRUCTION (latest):\n"]
    bodies = [instruction]
    weights = [2.0]
    shares = [0.6]
    if results:
        headers.append("\n\nRECENT TOOL RESULTS (oldest first):")
        bodies.append("")
        weights.append(0.0)
        shares.append(0.6)
        for i, (name, text) in enumerate(results, 1):
            headers.append("\n[%d] %s:\n" % (i, name))
            bodies.append(text or "(empty)")
            weights.append(1.0)
            shares.append(0.4)            # errors and summaries sit at the end
    else:
        headers.append("\n\nRECENT TOOL RESULTS: none")
        bodies.append("")
        weights.append(0.0)
        shares.append(0.6)
    headers.append("\n\nPROPOSED NEXT ASSISTANT MESSAGE (not executed, not shown to the user):\n")
    bodies.append(proposal)
    weights.append(3.0)
    shares.append(0.6)

    overhead = len(system) + sum(len(h) for h in headers)
    alloc = _allocate([len(b) for b in bodies], weights, DIGEST_MAX_CHARS - overhead)
    user = "".join(h + _clip_middle(b, n, s) for h, b, n, s in zip(headers, bodies, alloc, shares))
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


# --------------------------------------------------------------------------- #
# Reading the verdict
# --------------------------------------------------------------------------- #

_THINK_RE = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.S | re.I)
_THINK_OPEN_END_RE = re.compile(r"^.*?</(?:think|thinking|reasoning)>", re.S | re.I)
_FENCE_RE = re.compile(r"```[ \t]*(?:json|JSON|js|javascript)?[ \t]*\n?(.*?)```", re.S)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")
_KEYWORD_VERDICT_RE = re.compile(
    r"^\s*(?:\**\s*verdict\s*\**\s*[:=]\s*)?\**\s*(ACCEPT(?:ED)?|REVISE|REJECT(?:ED)?)\b\**"
    r"[\s:.\-]*(.*)$", re.S | re.I)
_ACCEPT_WORDS = frozenset(("accept", "accepted", "ok", "okay", "pass", "passed", "approve",
                           "approved", "yes", "true", "good", "correct", "lgtm", "1"))
_REVISE_WORDS = frozenset(("revise", "reject", "rejected", "fail", "failed", "no", "false",
                           "bad", "incorrect", "wrong", "needs_revision", "0"))
_HIGH_WORDS = frozenset(("high", "critical", "major", "severe", "blocker", "blocking"))
_OK_KEYS = ("ok", "accept", "accepted", "approved", "pass", "passed", "valid", "correct")
_VERDICT_KEYS = ("verdict", "decision", "status", "result")
_PROBLEM_KEYS = ("problems", "issues", "errors", "problem", "issue", "reasons", "reason",
                 "feedback", "concerns")
_MAX_PROBLEMS = 8
_MAX_PROBLEM_CHARS = 400


def _unparsed():
    return {"ok": True, "problems": [], "severity": "low", "unparsed": True}


def _truthy(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        w = v.strip().lower().strip(".!")
        if w in _ACCEPT_WORDS:
            return True
        if w in _REVISE_WORDS:
            return False
    return None


def _brace_end(s, start):
    """Index just past the brace group opening at s[start], honouring quoted
    strings of either quote; -1 when it never closes."""
    depth, quote, esc = 0, None, False
    for i in range(start, len(s)):
        ch = s[i]
        if quote:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def _loads_lenient(chunk):
    for attempt in (chunk, _TRAILING_COMMA_RE.sub(r"\1", chunk)):
        try:
            obj = json.loads(attempt)
        except ValueError:
            pass
        else:
            if isinstance(obj, dict):
                return obj
    # Python-literal dicts: single quotes, True/False/None.
    try:
        obj = ast.literal_eval(_TRAILING_COMMA_RE.sub(r"\1", chunk))
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return None
    if isinstance(obj, dict):
        return obj
    return None


def _objects_in(s, limit=24):
    out = []
    pos = 0
    while len(out) < limit:
        i = s.find("{", pos)
        if i < 0:
            break
        end = _brace_end(s, i)
        if end < 0:
            break
        obj = _loads_lenient(s[i:end])
        if obj is not None:
            out.append(obj)
            pos = end
        else:
            pos = i + 1
    return out


def _has_contract_keys(obj):
    keys = {str(k).lower() for k in obj}
    return bool(keys & set(_OK_KEYS + _VERDICT_KEYS + _PROBLEM_KEYS + ("severity",)))


def _problem_list(value):
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    out = []
    for it in items:
        if isinstance(it, dict):
            txt = next((it[k] for k in ("problem", "description", "issue", "text", "message", "detail")
                        if isinstance(it.get(k), str) and it.get(k).strip()), None)
            if txt is None:
                try:
                    txt = json.dumps(it, ensure_ascii=False)
                except (TypeError, ValueError):
                    txt = str(it)
        else:
            txt = it if isinstance(it, str) else str(it)
        txt = " ".join(txt.split())
        if txt:
            out.append(txt[:_MAX_PROBLEM_CHARS])
        if len(out) >= _MAX_PROBLEMS:
            break
    return out


def _normalize_verdict(obj, depth=0):
    low = {str(k).lower(): v for k, v in obj.items()}
    if depth < 2:                       # {"verdict": {"ok": false, ...}}
        for key in _VERDICT_KEYS:
            inner = low.get(key)
            if isinstance(inner, dict) and _has_contract_keys(inner):
                return _normalize_verdict(inner, depth + 1)
    ok = None
    for key in _OK_KEYS:
        if key in low:
            ok = _truthy(low[key])
            if ok is not None:
                break
    if ok is None:
        for key in _VERDICT_KEYS:
            if key in low:
                ok = _truthy(low[key])
                if ok is not None:
                    break
    problems = []
    for key in _PROBLEM_KEYS:
        if key in low and low[key] not in (None, "", []):
            problems = _problem_list(low[key])
            break
    if ok is None:
        ok = not problems
    sev = low.get("severity")
    severity = "high" if isinstance(sev, str) and sev.strip().lower() in _HIGH_WORDS else "low"
    return {"ok": bool(ok), "problems": problems, "severity": severity}


def _keyword_verdict(text):
    """Trinity-style bare reply: 'ACCEPT' or 'REVISE: <problems>'."""
    m = _KEYWORD_VERDICT_RE.match(text)
    if not m:
        return None
    ok = m.group(1).upper().startswith("ACCEPT")
    problems = []
    if not ok:
        for line in m.group(2).splitlines():
            line = re.sub(r"^\s*(?:[-*\u2022]|\d+[.)])\s*", "", line).strip()
            if line:
                problems.append(line[:_MAX_PROBLEM_CHARS])
            if len(problems) >= _MAX_PROBLEMS:
                break
    return {"ok": ok, "problems": problems, "severity": "low"}


_LINE_VERDICT_RE = re.compile(
    r"(?im)(?:^|(?<=[.!?]\s))[ \t>#*_`\-]*(?:final\s+|my\s+)?verdict\s*\**\s*[:=\-]\s*[*_`\"']*\s*"
    r"(accept(?:ed)?|approved?|pass(?:ed)?|ok(?:ay)?|lgtm|revise[d]?|reject(?:ed)?|fail(?:ed)?)\b"
    r"[*_`\"']*[ \t]*[:.\-\u2013\u2014]*[ \t]*(.*)$")
_PROBLEMS_HEAD_RE = re.compile(
    r"(?im)^[ \t>#*_`]*(?:problems?|issues?|reasons?)\s*\**\s*:\s*\**\s*(.*)$")
_SEVERITY_LINE_RE = re.compile(
    r"(?im)^[ \t>#*_`\-]*severity\s*\**\s*[:=]\s*[*_`\"']*\s*([A-Za-z]+)")
_BULLET_RE = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s*")
_PROSE_ACCEPT_RE = re.compile(
    r"\b(?:looks?|seems?|appears?|is|are|was)\s+(?:to\s+be\s+)?(?:fully\s+|entirely\s+|completely\s+|all\s+)?"
    r"(?:correct|good|fine|right|accurate|valid|acceptable|safe|sound|appropriate)\b"
    r"|\bno\s+(?:issues?|problems?|errors?|concerns?|bugs?)\b"
    r"|\blgtm\b|\bi\s+(?:would\s+)?(?:accept|approve)\b|\bshould\s+be\s+accepted\b", re.I)
_PROSE_REVISE_RE = re.compile(
    r"\b(?:is|are|looks?|seems?)\s+(?:not\s+correct|incorrect|wrong|broken|buggy|invalid|unsafe|incomplete)\b"
    r"|\b(?:does|do|did)\s+not\s+(?:work|satisfy|meet|match|follow|address|fulfil+)\b"
    r"|\b(?:isn't|aren't|wasn't)\s+(?:correct|right|valid|safe|complete)\b"
    r"|\bshould\s+be\s+(?:revised|rejected|changed|fixed|rewritten)\b"
    r"|\bneeds?\s+(?:to\s+be\s+)?(?:revis|fix|chang|correct|rewrit)\w*", re.I)


def _line_verdict(text):
    """'VERDICT: ACCEPT' / 'VERDICT: REVISE' on a line anywhere in the reply
    (bold, quoted, after a short preamble), with optional 'PROBLEMS:' bullet
    lines and a 'SEVERITY:' line. Also reads 'VERDICT: REVISE - <problem>'."""
    m = _LINE_VERDICT_RE.search(text)
    if not m:
        return None
    word = m.group(1).lower()
    ok = word.startswith(("accept", "approve", "pass", "ok", "lgtm"))
    problems = []
    rest = (m.group(2) or "").strip()
    if not ok and rest and not _PROBLEMS_HEAD_RE.match(rest):
        problems.append(" ".join(rest.split())[:_MAX_PROBLEM_CHARS])
    after = text[m.end():]
    head = _PROBLEMS_HEAD_RE.search(after)
    if head is not None and not ok:
        inline = (head.group(1) or "").strip()
        if inline:
            problems.append(" ".join(inline.split())[:_MAX_PROBLEM_CHARS])
        for line in after[head.end():].splitlines():
            if _SEVERITY_LINE_RE.match(line):
                break
            if not line.strip():
                continue
            problems.append(_BULLET_RE.sub("", line).strip()[:_MAX_PROBLEM_CHARS])
            if len(problems) >= _MAX_PROBLEMS:
                break
    sev = _SEVERITY_LINE_RE.search(text)
    severity = "high" if (not ok and sev and sev.group(1).lower() in _HIGH_WORDS) else "low"
    return {"ok": bool(ok), "problems": [p for p in problems if p][:_MAX_PROBLEMS],
            "severity": severity}


def _prose_verdict(text):
    """A verdict stated in plain sentences ('The answer looks correct.', 'This
    should be revised because ...'). Only when ONE side speaks: a reply that
    both approves and objects is left unparsed. Severity stays low -- a
    sentence is never evidence enough to trigger a corrector."""
    t = " ".join(text.split())
    if not t or len(t) > 3000:
        return None
    acc = _PROSE_ACCEPT_RE.search(t)
    rev = _PROSE_REVISE_RE.search(t)
    if bool(acc) == bool(rev):
        return None
    if acc:
        return {"ok": True, "problems": [], "severity": "low", "inferred": True}
    start = max(0, t.rfind(". ", 0, rev.start()) + 1)
    end = t.find(". ", rev.end())
    sentence = t[start:end + 1 if end >= 0 else len(t)].strip()
    return {"ok": False, "problems": [sentence[:_MAX_PROBLEM_CHARS]], "severity": "low",
            "inferred": True}


def parse_verdict(text):
    """{"ok": bool, "problems": [str], "severity": "low"|"high"} from the
    verifier's reply. Tolerant: reasoning blocks dropped, the JSON may sit in a
    code fence or in prose, trailing commas / Python literals are accepted, a
    "verdict": "ACCEPT"/"REVISE" field or a bare ACCEPT / REVISE reply count.
    Missing fields default (ok inferred from problems, severity "low").
    Unparseable -> ACCEPT with "unparsed": True (fail open)."""
    if not isinstance(text, str) or not text.strip():
        return _unparsed()
    t = _THINK_RE.sub("", text[:DIGEST_MAX_CHARS])
    if re.search(r"</(?:think|thinking|reasoning)>", t, re.I):
        t = _THINK_OPEN_END_RE.sub("", t)
    for chunk in [m.group(1) for m in _FENCE_RE.finditer(t)] + [t]:
        objs = _objects_in(chunk)
        hit = next((o for o in objs if _has_contract_keys(o)), None)
        if hit is not None:
            return _normalize_verdict(hit)
    return _line_verdict(t) or _keyword_verdict(t) or _prose_verdict(t) or _unparsed()


# --------------------------------------------------------------------------- #
# The corrector
# --------------------------------------------------------------------------- #

NOT_EXECUTED_TEXT = ("Not executed: this call was held back by an independent review "
                     "(see the review that follows).")
CORRECTOR_NOTE_HEADER = "INDEPENDENT REVIEW OF YOUR DRAFT"


def _openai_call(call, i):
    """A tool call in the OpenAI chat shape, with an id."""
    if isinstance(call.get("function"), dict):
        out = dict(call)
        out["function"] = dict(call["function"])
        out.setdefault("type", "function")
        if not out.get("id"):
            out["id"] = "call_verify_%d" % i
        args = out["function"].get("arguments")
        if args is None:
            out["function"]["arguments"] = "{}"
        elif not isinstance(args, str):
            out["function"]["arguments"] = json.dumps(args, ensure_ascii=False)
        return out
    name = _call_name_args(call)[0] or "tool"
    args = _args_text(call) or "{}"
    return {"id": call.get("id") or call.get("call_id") or "call_verify_%d" % i,
            "type": "function", "function": {"name": name, "arguments": args}}


def corrector_messages(messages, proposed, verdict):
    """Messages for the corrector: the ORIGINAL conversation, the proposed
    assistant message, then a user note listing the verifier's problems and
    asking for the corrected NEXT assistant message. Dispatch with the same
    tools as the original request.

    A proposal with tool calls is kept as a native assistant tool_calls
    message, each call answered by a tool message saying it was NOT executed
    -- every OpenAI-compatible provider rejects tool calls left without
    results -- so the corrector can re-issue a fixed call in the native shape."""
    out = [dict(m) if isinstance(m, dict) else m for m in (messages or [])]
    calls, text = _calls_and_text(proposed)
    if calls:
        oa_calls = [_openai_call(c, i) for i, c in enumerate(calls, 1)]
        out.append({"role": "assistant", "content": text or None, "tool_calls": oa_calls})
        for c in oa_calls:
            out.append({"role": "tool", "tool_call_id": c["id"], "content": NOT_EXECUTED_TEXT})
    else:
        out.append({"role": "assistant", "content": text})

    v = verdict if isinstance(verdict, dict) else {}
    problems = [p for p in (v.get("problems") or []) if isinstance(p, str) and p.strip()]
    lines = ["%s (your draft above was %s):" % (
        CORRECTOR_NOTE_HEADER,
        "not executed and not shown to the user" if calls else "not shown to the user")]
    if problems:
        lines.extend("- " + p.strip() for p in problems)
    else:
        lines.append("- The reviewer rejected the draft without details; re-check it against "
                     "the request and the tool results.")
    if v.get("severity") == "high":
        lines.append("The reviewer rated these problems HIGH severity.")
    lines.append(
        "Write the corrected next assistant message now. Fix every problem listed and keep "
        "what was right. If the task still needs an action, make the tool call(s) again "
        "through the tool interface (never write a call out as text). Do not mention this "
        "review.")
    out.append({"role": "user", "content": "\n".join(lines)})
    return out


# --------------------------------------------------------------------------- #
# Web slop in finished work (slopcheck, zero model cost)
# --------------------------------------------------------------------------- #
# One wrapper every caller shares: Multi's phase check, the prose swarm / crews'
# final text, and (later) the app's verifier / corrector on a web turn. Pure;
# slopcheck is imported lazily and any failure of it means "no findings".

WEB_FILE_EXTS = (".html", ".htm", ".css", ".jsx", ".tsx", ".vue", ".svelte")
SLOP_MAX_FILES = 40
SLOP_MAX_BYTES = 1_000_000
_FENCE_RE = re.compile(r"```[ \t]*([\w+-]*)[^\n]*\n(.*?)```", re.S)
_WEB_FENCE_LANGS = {"html", "htm", "css", "jsx", "tsx", "vue", "svelte", "svg"}
_MARKUP_RE = re.compile(r"<(?:!doctype|html|body|div|section|main)\b", re.I)


def is_web_file(path):
    return str(path or "").lower().split("?")[0].endswith(WEB_FILE_EXTS)


def html_blocks(text):
    """[(name, text)] for the web code fences in a prose answer (a fence with
    no language counts when it looks like markup). At most SLOP_MAX_FILES."""
    out = []
    for m in _FENCE_RE.finditer(text or ""):
        lang, body = m.group(1).lower(), m.group(2)
        if lang in _WEB_FENCE_LANGS or (not lang and _MARKUP_RE.search(body)):
            if len(body) <= SLOP_MAX_BYTES:
                ext = lang if lang in ("css", "jsx", "tsx", "vue", "svelte") else "html"
                out.append(("block-%d.%s" % (len(out) + 1, ext), body))
        if len(out) >= SLOP_MAX_FILES:
            break
    return out


def read_web_files(folder, paths):
    """[(relative path, text)] for the web files among `paths` (relative to
    `folder`): at most SLOP_MAX_FILES, each at most SLOP_MAX_BYTES."""
    import os
    out = []
    for p in paths or ():
        if len(out) >= SLOP_MAX_FILES:
            break
        if not is_web_file(p):
            continue
        full = str(p) if os.path.isabs(str(p)) else os.path.join(folder or "", str(p))
        try:
            if not os.path.isfile(full) or os.path.getsize(full) > SLOP_MAX_BYTES:
                continue
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                out.append((str(p).replace("\\", "/"), fh.read()))
        except OSError:
            continue
    return out


def slop_report(files_or_texts):
    """{"findings", "high": [problem text], "warnings": [str], "counts",
    "line"} for built web output; an empty report when there is nothing to
    check or slopcheck is unavailable. Never raises."""
    empty = {"findings": [], "high": [], "warnings": [],
             "counts": {"high": 0, "medium": 0, "low": 0}, "line": ""}
    try:
        import slopcheck
        findings = slopcheck.check_files(files_or_texts)
        if not findings:
            return empty
        score = slopcheck.summary(findings).get("score")
    except Exception:                                            # noqa: BLE001
        return empty
    high, warn = [], []
    for f in findings:
        text = "%s (%s): %s -- fix: %s" % (f.get("where") or "?", f.get("rule"),
                                           f.get("why"), f.get("fix"))
        if f.get("severity") == "high":
            high.append("AI-slop: " + text)
        else:
            warn.append("%s %s" % (f.get("severity"), text))
    counts = {s: sum(1 for f in findings if f.get("severity") == s)
              for s in ("high", "medium", "low")}
    line = "Slop check: %d high, %d medium" % (counts["high"], counts["medium"]) \
        + (", %d low" % counts["low"] if counts["low"] else "")
    return {"findings": findings, "high": high[:8], "warnings": warn[:12],
            "counts": counts, "line": line, "score": score}


def slop_problems(files_or_texts):
    """The HIGH slop findings of built web output as problem strings (what a
    verifier / corrector is told to fix). [] when clean."""
    return list(slop_report(files_or_texts)["high"])
