"""Model guides: short, evidence-backed instructions for weak and specific models.

The hub routes ~160 free models of very different strength. Two things help a
model that is not top-band, or one with a measured family quirk:

  * a GUIDE (text for its system prompt): `model_guides/<family>.md` for the
    families with real evidence (hub.log failures, measured notes in the code,
    or the official model card), plus `weak.md` for any model under
    WEAK_SCORE, or the minimal `strong.md` otherwise;
  * SCAFFOLD knobs for the caller (smaller steps, always verify, a smaller
    working context) -- `scaffold()`, weak models only.

A guide file is markdown: `<!-- evidence: ... -->` comments at the top back each
line (stripped before use), then optional `## any` / `## tools` / `## answer`
sections; text before the first header counts as `any`. `task_kind` picks the
sections (see _KIND_SECTIONS), so a plain chat answer is not told how to call
tools.

Pure: stdlib only, never imports app, never raises (every public function
falls back to "" / {} / the safe default). File reads are cached per mtime.
"""

import math
import os
import re
import threading

# WEAK_SCORE -- on the hub's score scale (app._benchmark_score):
#   * every owner floor is >= 133 (app._PREF_FLOORS), Arena newcomers are capped
#     at 134.5 (arena.HUB_CAP), and category membership by evidence starts at
#     130 (AGENTS.md "Categories by name AND by evidence") -- the strong band;
#   * below it sit the family-table tiers: S 100, A 84, B 56, llama 44, C 26,
#     unknown 10;
#   * arena.py's linear map puts 120 at an Arena rating of ~1445: minimax-m3's
#     own strength ("~120 by strength", rated 1440) and claude-haiku-4.5 (1414,
#     ~107) fall under it, deepseek-v4.1-flash / glm-5.3-flash (1474-1477) over.
# So 120 is the line under which a model gets the full scaffolding; nothing the
# hub routes hard work to (>= 130) does.
WEAK_SCORE = 120.0
GUIDE_MAX_CHARS = 1500          # whole guide_for() output
FILE_BODY_MAX_CHARS = 1200      # one guide file's text, comments stripped (tests)

GUIDES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_guides")
GENERIC = ("weak", "strong")
HEADER = "MODEL GUIDE"

# context_budget_tokens for a weak model. 12000 = app.STREAM_BIG_REQUEST_TOKENS,
# the hub's own "big request" line (also the trivial-turn and pipeline fast-path
# gate). Under the medium difficulty floor (app._DIFFICULTY_FLOOR["medium"] = 50)
# a model is simple-turn only and typically sits on an 8K-per-request host
# (groq, AGENTS.md "Tool-turn reliability"): 8000.
WEAK_CONTEXT_TOKENS = 12000
VERY_WEAK_SCORE = 50.0
VERY_WEAK_CONTEXT_TOKENS = 8000

# task_kind -> sections of a guide file to include. None / unknown = all.
_KIND_SECTIONS = {
    "tools": ("any", "tools"), "tool": ("any", "tools"), "agent": ("any", "tools"),
    "agentic": ("any", "tools"), "code": ("any", "tools"), "edit": ("any", "tools"),
    "answer": ("any", "answer"), "chat": ("any", "answer"), "text": ("any", "answer"),
}
_SECTIONS = ("any", "tools", "answer")

# Family names a caller's family() may return -> our guide file name.
_ALIASES = {
    "zai": "glm", "z-ai": "glm", "zai-org": "glm", "zhipu": "glm", "zhipuai": "glm",
    "chatglm": "glm", "moonshot": "kimi", "moonshotai": "kimi", "deepseek-ai": "deepseek",
    "minimaxai": "minimax",
}

# Fallback detection when no family() is given or it names nothing we have a
# guide for. Only the evidenced families; gemma must never read as gemini.
_BUILTIN_FAMILIES = (
    ("deepseek", re.compile(r"deepseek")),
    ("glm", re.compile(r"glm|(?:^|/)z-ai/|zai-org/")),
    ("kimi", re.compile(r"kimi|moonshot")),
    ("minimax", re.compile(r"minimax")),
    ("gemini", re.compile(r"gemini")),
)

# Model-card sampling (fetched 2026-10-04), scoped to the version each card
# covers -- another version gets {} rather than a guess. Not applied by this
# module; offered to the caller via sampling_for().
_SAMPLING = (
    # huggingface.co/deepseek-ai/DeepSeek-V4-Flash: "temperature = 1.0, top_p = 1.0"
    (re.compile(r"deepseek-v4"), {"temperature": 1.0, "top_p": 1.0}, None),
    # huggingface.co/zai-org/GLM-4.7-Flash: default 1.0 / 0.95;
    # Terminal Bench + SWE-bench Verified 0.7 / 1.0
    (re.compile(r"glm-4\.7"), {"temperature": 1.0, "top_p": 0.95},
     {"temperature": 0.7, "top_p": 1.0}),
    # huggingface.co/moonshotai/Kimi-K2.6: 1.0 thinking (default), 0.6 instant, top_p 0.95
    (re.compile(r"kimi-k2\.6"), {"temperature": 1.0, "top_p": 0.95}, None),
    # huggingface.co/MiniMaxAI/MiniMax-M2.7: temperature 1.0, top_p 0.95, top_k 40
    (re.compile(r"minimax-m2\.7"), {"temperature": 1.0, "top_p": 0.95, "top_k": 40}, None),
    # ai.google.dev Gemini 3: keep the default 1.0; lower "may lead to ... looping"
    (re.compile(r"gemini-3"), {"temperature": 1.0}, None),
    # huggingface.co/Qwen/Qwen3.8-27B: thinking mode (default) 1.0 / 0.95 / top_k 20
    (re.compile(r"qwen3\.8"), {"temperature": 1.0, "top_p": 0.95, "top_k": 20}, None),
)

_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_HEAD_RE = re.compile(r"^##\s*([a-z]+)\s*$", re.I)

_cache = {}
_lock = threading.Lock()


def _read(name):
    """{section: [lines]} for model_guides/<name>.md, cached per mtime; {} if absent."""
    try:
        path = os.path.join(GUIDES_DIR, name + ".md")
        mtime = os.path.getmtime(path)
    except (OSError, TypeError, ValueError):
        return {}
    with _lock:
        hit = _cache.get(name)
        if hit and hit[0] == mtime:
            return hit[1]
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except (OSError, UnicodeDecodeError):
        return {}
    sections, current = {}, "any"
    for line in _COMMENT_RE.sub("", raw).splitlines():
        m = _HEAD_RE.match(line.strip())
        if m:
            current = m.group(1).lower()
            continue
        if line.strip():
            sections.setdefault(current, []).append(line.rstrip())
    with _lock:
        _cache[name] = (mtime, sections)
    return sections


def guide_body(name, task_kind=None):
    """The model-facing text of one guide file for this task kind ("" if none)."""
    sections = _read(name)
    wanted = _KIND_SECTIONS.get(str(task_kind).lower(), _SECTIONS) if task_kind else _SECTIONS
    lines = []
    for sec in wanted:
        lines.extend(sections.get(sec, ()))
    return "\n".join(lines)


def available_families():
    """Family names that have a guide file (generic files excluded)."""
    try:
        names = os.listdir(GUIDES_DIR)
    except OSError:
        return []
    return sorted(n[:-3] for n in names if n.endswith(".md") and n[:-3] not in GENERIC)


def _norm(name):
    if not isinstance(name, str):
        return None
    low = name.strip().lower()
    return _ALIASES.get(low, low) or None


def _family_of(model_id, family):
    """Our guide family for model_id: the caller's family() first, then the
    builtin patterns. None when nothing we have a guide for matches."""
    have = set(available_families())
    named = None
    try:
        if callable(family):
            named = _norm(family(model_id))
        elif isinstance(family, str):
            named = _norm(family)
    except Exception:                                            # noqa: BLE001
        named = None
    if named in have:
        return named
    low = model_id.lower() if isinstance(model_id, str) else ""
    for fam, rx in _BUILTIN_FAMILIES:
        if rx.search(low) and fam in have:
            return fam
    return None


def _as_score(score):
    if isinstance(score, bool):
        return None
    try:
        val = float(score)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(val) else val


def is_weak(score):
    """True under WEAK_SCORE. An unknown / unreadable score counts as weak: the
    hub scores an unknown family 10, and a missing guide costs a failed turn
    while an extra one costs a few hundred tokens."""
    val = _as_score(score)
    return True if val is None else val < WEAK_SCORE


def _trim(lines, limit):
    """Whole lines, in order, while they fit in `limit` chars (joined by \\n)."""
    out, used = [], 0
    for line in lines:
        cost = len(line) + (1 if out else 0)
        if used + cost > limit:
            break
        out.append(line)
        used += cost
    return out


def guide_for(model_id, family, score, task_kind=None):
    """Guide text for one model: its family guide (if evidenced) + weak.md when
    weak, else at most the minimal strong.md. Trimmed to GUIDE_MAX_CHARS on line
    boundaries, family lines first. "" when nothing applies. Never raises."""
    try:
        parts = []
        fam = _family_of(model_id, family)
        if fam:
            parts.append(guide_body(fam, task_kind))
        parts.append(guide_body("weak" if is_weak(score) else "strong", task_kind))
        lines = [ln for part in parts if part for ln in part.split("\n")]
        if not lines:
            return ""
        return "\n".join(_trim([HEADER] + lines, GUIDE_MAX_CHARS))
    except Exception:                                            # noqa: BLE001
        return ""


_CHECKLIST_TOOLS = [
    "State the one goal of this step in one line.",
    "Read the file (or run the command) before changing anything.",
    "Change one file or one function, then stop and check.",
    "Run the cheapest check first (syntax or build, then tests) and read its real output.",
    "Claim done only with passing output; otherwise list what still fails.",
]
_CHECKLIST_ANSWER = [
    "Answer exactly what was asked, in the asked format.",
    "Put the answer first; no restating of the question.",
    "Stop when the answer is complete.",
]


def scaffold(score, task_kind=None):
    """Knobs for the caller when the model is weak; {} for a strong model.

    temperature is None on purpose: no hub evidence says a generic value helps,
    and the model cards disagree (Gemini 3: never below 1.0, it loops; Qwen3.8
    instruct 0.7; GLM-4.7 agentic 0.7). Use sampling_for() for card values."""
    try:
        if not is_weak(score):
            return {}
        val = _as_score(score)
        very_weak = val is None or val < VERY_WEAK_SCORE
        kind = str(task_kind).lower() if task_kind else ""
        sections = _KIND_SECTIONS.get(kind, _SECTIONS)
        if sections == ("any", "answer"):
            checklist = list(_CHECKLIST_ANSWER)
        elif sections == ("any", "tools"):
            checklist = list(_CHECKLIST_TOOLS)
        else:
            checklist = _CHECKLIST_TOOLS + _CHECKLIST_ANSWER[:1]
        return {
            "max_step_scope": "one file or one function",
            "always_verify": True,
            "context_budget_tokens": (VERY_WEAK_CONTEXT_TOKENS if very_weak
                                      else WEAK_CONTEXT_TOKENS),
            "temperature": None,
            "checklist": checklist,
        }
    except Exception:                                            # noqa: BLE001
        return {}


def sampling_for(model_id, task_kind=None):
    """The model card's recommended sampling for this exact model version, or {}.
    Agentic/coding kinds get the card's agentic setting when it names one."""
    try:
        low = model_id.lower() if isinstance(model_id, str) else ""
        agentic = _KIND_SECTIONS.get(str(task_kind).lower()) == ("any", "tools")
        for rx, default, agentic_values in _SAMPLING:
            if rx.search(low):
                return dict(agentic_values if (agentic and agentic_values) else default)
        return {}
    except Exception:                                            # noqa: BLE001
        return {}
