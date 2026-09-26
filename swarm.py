"""Calvoun Free LLM Hub — multi-model swarm for creation work.

WHY THIS IS A MODEL AND NOT AN AUTOMATIC MODE
---------------------------------------------
The obvious design — detect "this looks like a creation task" and secretly run a
multi-pass pipeline — breaks the hub's most important clients. Codex, Claude
Code, OpenClaw and Kimi already run their OWN agent loops: they send turns
carrying tool_calls, tool results and diffs, and expect exactly one model reply
per turn. Re-planning or multi-passing such a turn corrupts the loop, the same
way rewriting a tool-carrying prompt does (see _enhance_prompt's scoping).

So the swarm is a VIRTUAL MODEL: ask for `model: "swarm"` and you get the
pipeline; ask for anything else and nothing changes. Every client can reach it
by selecting one model id, and conversational traffic is untouched by
construction.

THE PIPELINE
------------
    supervisor plan  ->  subagent waves  ->  supervisor check  ->  review  ->  synthesis

1. PLAN       a SUPERVISOR model splits the work into phases and, crucially,
              declares which phases need the OUTPUT of which others. This is
              also what satisfies "always make a plan first".
2. SUBAGENTS  each phase is one subagent with its OWN context: a fresh two-
              message conversation, shown only the outputs of the phases it
              declared it needs. Phases that need nothing from each other RUN
              CONCURRENTLY, in dependency waves.
3. SUPERVISE  the supervisor compares what came back against the plan it set.
              Workers in a wave could not see each other, so this is where a
              genuine gap or a contradiction between them is caught and filled.
4. REVIEW     a DIFFERENT provider than the one that executed criticises the
              draft. Different-provider is the point: a model reviewing its own
              output agrees with itself, so correlated blind spots survive.
5. SYNTH      the strongest model folds the review into a final answer.

WHY THE OWN-CONTEXT PART IS THE WHOLE POINT
-------------------------------------------
This used to run phases sequentially, concatenating every earlier output into
every later prompt. That made the swarm's ceiling the SMALLEST context window it
routed to, and it ran out on exactly the large builds it existed for. Five
subagents on 32K windows have ~160K of usable context between them, and a
dependency handed to a worker is clipped (DEP_CONTEXT_CHARS) so no single worker
can be handed the whole project again.

Every stage degrades instead of failing: if the planner returns nothing usable
the work becomes a single phase carrying the WHOLE brief, if review fails the
draft is returned as-is, and one subagent dying does not take down its wave. A
swarm request must never end with an error the plain model would have answered.

This module owns NO transport. `dispatch(messages, max_tokens, exclude_pids)`
is injected by app.py, which keeps chain/fallback/quota/activity behaviour
identical to every other request.

PROFILES
--------
`run()` takes an optional `profile` dict that swaps the stage system prompts
and turns on ONE bounded revision pass (`max_revisions`). That is the whole
mechanism behind the "crew-*" virtual models (see crews.py): a crew is not a
second pipeline, it is this pipeline wearing a specialist persona — a code
crew gets a senior-engineer reviewer, a research crew gets a fact-hunter, and
so on. `profile=None` reproduces the generic behaviour byte-for-byte, so the
plain "swarm" model and its tests are untouched by construction.
"""
import json
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

try:
    # A leaf module (no app imports), so the no-app-imports rule for this file
    # holds. Optional: without it the per-phase check just skips that test.
    import answer_check
except ImportError:                                             # pragma: no cover
    answer_check = None

MAX_PHASES = 5           # bounds worst-case cost: 1 plan + 5 phases + 1 review + 1 synth
# The planner is asked for 2..MAX_PHASES. A plan with ONE phase means it did not
# follow the contract, and that is actively dangerous: observed live, a 3-part
# brief ("hero headline, about paragraph, 6 menu descriptions") came back as a
# single phase titled "Hero Headline", the one worker delivered exactly that,
# and the other two thirds of the request were silently dropped. Falling back to
# the whole brief as one phase answers ALL of it, so a bad plan costs a retry,
# never content.
MIN_PHASES = 2
MAX_REPAIRS = 2          # supervisor gap-fills; each is a whole extra model call
# Generous on purpose: the planner is routed to the STRONGEST model available,
# and the strongest free models are reasoning models whose thinking is billed
# against the same budget. At 1200 a real planner hit finish_reason=length after
# ~220 visible characters — the JSON never closed, so every plan was unusable and
# the swarm silently degraded to one model. Cheap insurance: this is one call.
PLAN_MAX_TOKENS = 3000
PHASE_MAX_TOKENS = 4000
REVIEW_MAX_TOKENS = 2500   # same reasoning-budget trap as PLAN_MAX_TOKENS
SUPERVISE_MAX_TOKENS = 2000  # ditto: a truncated verdict reads as 'no gaps'
SYNTH_MAX_TOKENS = 6000
# How much of a dependency's output a worker is shown. The point of giving each
# subagent its own context is that nobody carries the whole project; handing a
# worker an unbounded teammate output would put the ceiling straight back.
DEP_CONTEXT_CHARS = 6000

# ---- The optional MANAGER (a paid subscription model, see run(manager=)) ----
# The manager plans, checks and fixes; the free models do the work. Every cap
# below exists to keep its bill small: it is only ever shown CLIPPED summaries
# (never a full transcript) and asked for short answers. The one place it may
# write at length is fixing a phase two free models could not get right.
MANAGER_PLAN_TOKENS = 1500
MANAGER_SUPERVISE_TOKENS = 600
MANAGER_REVIEW_TOKENS = 800
MANAGER_VERDICT_TOKENS = 300
MANAGER_FIX_TOKENS = PHASE_MAX_TOKENS
MANAGER_BRIEF_CHARS = 6000       # the user's brief as the manager sees it
MANAGER_PHASE_CHARS = 1500       # per phase, in the supervise/review summaries
MANAGER_DRAFT_CHARS = 8000       # whole draft, in the review summary
VERDICT_OUTPUT_CHARS = 3000      # one worker's output, in a per-phase verdict


def _clip(text, limit):
    """Head + tail, so a truncated dependency keeps how it starts AND how it
    ends — the middle of a document is the safest part to lose."""
    text = text or ""
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    return text[:head] + "\n\n[... trimmed ...]\n\n" + text[-(limit - head):]

_PLAN_SYSTEM = (
    "You are the SUPERVISOR of a team of AI models that will build what the user "
    "asked for. You do not write the deliverable yourself — you decide how to "
    "split it, and who needs whose output.\n"
    "Reply with JSON ONLY — no prose, no markdown fence:\n"
    '{"goal": "<one sentence>", "phases": [{"title": "<short>", "task": "<what to '
    'produce, concretely>", "done_when": "<observable completion test>", '
    '"needs": [<numbers of the phases this one needs the OUTPUT of>], '
    '"inputs": "<facts/material from the brief this worker must use>", '
    '"constraints": ["<hard rule the output must respect>"], '
    '"output_format": "<exact shape: e.g. HTML file, JSON object, markdown list>", '
    '"acceptance": ["<checkable criterion, e.g. includes \\"Pricing\\" section>"]}]}\n'
    "Rules:\n"
    "- Each worker sees ONLY its own phase, never the user's message. Put "
    "everything it needs in task/inputs/constraints — names, numbers, tone, "
    "audience, language — so the brief is complete on its own.\n"
    "- 'acceptance' is how the work is checked: 1-4 short criteria anyone can "
    "verify by reading the output. Quote exact text that must appear.\n"
    "- Between 2 and %d phases. Fewer is better; do not invent work.\n"
    "- Each phase must produce a CONCRETE artefact (copy, code, a structure, a "
    "list) — never 'research', 'consider' or 'think about'.\n"
    "- Phases are numbered from 1 in the order you list them.\n"
    "- 'needs' is the most important field. Each worker runs in its OWN context "
    "and is shown ONLY the output of the phases it lists. List a phase ONLY if "
    "the work genuinely cannot be done without reading its output — an empty "
    "needs list means it can start immediately, and phases that need nothing "
    "from each other RUN AT THE SAME TIME. Over-listing serialises the team and "
    "wastes the context you were given.\n"
    "- A phase may only need LOWER-numbered phases.\n"
    "- Split so that parallel work is possible where it honestly is: separate "
    "concerns (copy vs layout vs data) usually can start together; a phase that "
    "assembles or depends on decisions made elsewhere cannot.\n"
    "- Phases must be about the user's actual request. Do not add scope they did "
    "not ask for.\n"
    "- No filler phases such as 'gather requirements' or 'final review' — review "
    "happens outside your plan."
) % MAX_PHASES

_SUPERVISE_SYSTEM = (
    "You are the supervisor checking your team's work against the plan you set. "
    "You can see each phase's output but NOT the workers' reasoning.\n"
    "Reply with JSON ONLY:\n"
    '{"missing": [{"title": "<short>", "task": "<the specific gap to fill>"}]}\n'
    "List ONLY work that was assigned and genuinely is not there, or that two "
    "workers produced incompatibly (they could not see each other). At most 2 "
    "items — this costs a whole extra round. If the phases together cover the "
    "goal, return an empty list. Do not list style preferences, do not ask for "
    "polish, and do not invent new scope: that is not what a supervisor is for."
)

_PHASE_SYSTEM = (
    "You are one specialist on a team. Do YOUR phase only, completely, and to the "
    "highest standard you are capable of.\n"
    "Output the actual artefact — the copy, the code, the structure. No preamble, "
    "no 'here is', no restating the task, no apologising for limitations.\n"
    "Never pad. Never invent facts, names, statistics, prices or testimonials: if "
    "something real is required and you were not given it, mark it clearly as "
    "[NEEDS INPUT: what].\n"
    "Do not write the other phases. Do not summarise what you did afterwards."
)

_REVIEW_SYSTEM = (
    "You are reviewing another model's work against the brief. Be specific and "
    "hard to please; you are the last check before this reaches the user.\n"
    "Reply with JSON ONLY:\n"
    '{"verdict": "ship" | "revise", "problems": ["<concrete, actionable>"]}\n'
    "Judge only: does it do what was asked, is anything factually invented, is "
    "anything missing, is any part generic filler that would fit any other "
    "project. Style preferences are not problems. If it is genuinely good, say "
    "ship with an empty problems list — do not manufacture criticism."
)

_SYNTH_SYSTEM = (
    "Assemble the final deliverable from the phase outputs, applying the reviewer's "
    "problems where they are valid.\n"
    "Output the finished work itself — no meta-commentary, no 'here is the final "
    "version', no description of what you changed.\n"
    "Keep every concrete detail the phases produced; preserve [NEEDS INPUT: ...] "
    "markers verbatim so the user can see what still needs their input.\n"
    "Cut anything that reads as generic AI filler."
)

# Per-phase verdict, asked of the MANAGER only (never a free model: a free
# checker is as likely wrong as the worker it judges, and the cheap mechanical
# checks run first anyway). Deliberately tiny in and out.
_VERDICT_SYSTEM = (
    "You check ONE team member's output against its task and acceptance "
    "criteria. You see a clipped excerpt; do not fault what may sit in the "
    "trimmed middle.\n"
    "Reply with JSON ONLY:\n"
    '{"ok": true | false, "problems": ["<concrete, fixable>"]}\n'
    "Fail it only for a missed criterion, a wrong or invented fact, missing "
    "required content, or the wrong format. Style is not a problem. At most 4 "
    "problems."
)

# The manager writing a phase itself, after two free attempts failed.
_FIX_SYSTEM = (
    "Two team members failed this phase. Write the phase's output yourself, "
    "complete and correct, meeting every acceptance criterion and fixing every "
    "listed problem. Output only the artefact — no preamble, no notes. Never "
    "invent facts; mark missing real-world data as [NEEDS INPUT: what]."
)


def _last_user_text(messages):
    for m in reversed(messages or []):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):        # multimodal turn -> text parts only
                return "\n".join(p.get("text", "") for p in c
                                 if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _parse_json(text):
    """Models wrap JSON in prose or a fence no matter how firmly you ask, and a
    weak or truncated one emits JSON that is nearly right. Returns None only
    when there is genuinely nothing usable.

    The repair pass exists because the plan is the linchpin: a single missing
    brace used to collapse the whole swarm to one phase, which is the difference
    between a team and one model. Observed for real from a small planner —
    objects opened and never closed, one after another."""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"```\s*$", "", s).strip()
    i = s.find("{")
    if i == -1:
        return None
    s = s[i:]
    j = s.rfind("}")
    for candidate in ([s[:j + 1]] if j > 0 else []) + [s]:
        try:
            out = json.loads(candidate)
        except ValueError:
            out = _repair_json(candidate)
        if isinstance(out, dict):
            return out
    return None


def _repair_json(s):
    """Best-effort fix for the two ways model JSON actually breaks: a trailing
    comma before a closer, and objects/arrays left open (truncation, or a model
    that simply forgot). Returns a dict or None — never raises."""
    fixed = re.sub(r",\s*([}\]])", r"\1", s)
    # Balance, ignoring braces inside strings.
    stack, in_str, esc = [], False, False
    for ch in fixed:
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in "{[":
            stack.append(ch)
        elif ch in "}]" and stack:
            stack.pop()
    if in_str:
        fixed += '"'
    fixed += "".join("}" if c == "{" else "]" for c in reversed(stack))
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
    try:
        out = json.loads(fixed)
    except ValueError:
        return None
    return out if isinstance(out, dict) else None


def _clean_phases(plan):
    """Validated phase list, or [] if the plan is unusable.

    `needs` is sanitised hard because a bad dependency graph is worse than none:
    a self-reference or a forward reference would deadlock the wave scheduler,
    and a supervisor that lists every phase as a dependency silently turns the
    swarm back into the sequential pipeline this replaced."""
    if not isinstance(plan, dict):
        return []
    out = []
    for p in (plan.get("phases") or [])[:MAX_PHASES]:
        if not isinstance(p, dict):
            continue
        task = str(p.get("task") or "").strip()
        if not task:
            continue
        idx = len(out) + 1
        needs = []
        raw = p.get("needs")
        if isinstance(raw, list):
            for n in raw:
                try:
                    n = int(n)
                except (TypeError, ValueError):
                    continue
                # Only strictly-earlier phases: anything else is a cycle or a
                # reference to work that does not exist yet.
                if 1 <= n < idx and n not in needs:
                    needs.append(n)
        cleaned = {
            "title": str(p.get("title") or "Phase %d" % idx).strip()[:80],
            "task": task[:2000],
            "done_when": str(p.get("done_when") or "").strip()[:300],
            "needs": needs,
        }
        cleaned.update(_brief_fields(p))
        out.append(cleaned)
    return out


def _str_list(value, n, width):
    """A list of short non-empty strings from a list OR a single string (a
    planner asked for a list often writes one sentence instead)."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = []
    for v in value:
        s = str(v or "").strip() if not isinstance(v, (dict, list)) else ""
        if s:
            out.append(s[:width])
    return out[:n]


def _brief_fields(p):
    """The optional worker-brief fields (inputs, constraints, output_format,
    acceptance). Only keys that carry something are returned, so a plan
    without them yields exactly the phase dicts it always did."""
    out = {}
    inputs = str(p.get("inputs") or "").strip() if isinstance(
        p.get("inputs"), (str, int, float)) else ""
    if inputs:
        out["inputs"] = inputs[:1200]
    fmt = str(p.get("output_format") or "").strip() if isinstance(
        p.get("output_format"), str) else ""
    if fmt:
        out["output_format"] = fmt[:300]
    for key in ("constraints", "acceptance"):
        items = _str_list(p.get(key), 6, 240)
        if items:
            out[key] = items
    return out


def _render_brief(ph):
    """The optional brief fields, rendered for the worker. "" when the phase
    has none, so the worker prompt is byte-identical to the plain pipeline's."""
    parts = []
    if ph.get("inputs"):
        parts.append("\n\nInputs to use:\n%s" % ph["inputs"])
    if ph.get("constraints"):
        parts.append("\n\nConstraints:\n- " + "\n- ".join(ph["constraints"]))
    if ph.get("output_format"):
        parts.append("\n\nOutput format: %s" % ph["output_format"])
    if ph.get("acceptance"):
        parts.append("\n\nAcceptance criteria (your output is checked against "
                     "each one):\n- " + "\n- ".join(ph["acceptance"]))
    return "".join(parts)


# ---- cheap, mechanical per-phase checks (no model call) -------------------
_QUOTED_RE = re.compile(r'["“]([^"”]{2,80})["”]')
_MUST_HAVE_RE = re.compile(r"\b(include|includes|including|contain|contains|"
                           r"mention|mentions|name|names|use|uses|show|shows|"
                           r"titled|heading)\b", re.I)
_MIN_WORDS_RE = re.compile(r"\bat\s+least\s+(\d{1,5})\s+words?\b", re.I)
_MAX_WORDS_RE = re.compile(r"\b(?:at\s+most|under|no\s+more\s+than|maximum(?:\s+of)?|"
                           r"max\.?|fewer\s+than)\s+(\d{1,5})\s+words?\b", re.I)


def _looks_like_json(text):
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"```\s*$", "", s).strip()
    try:
        json.loads(s)
        return True
    except ValueError:
        return False


def _mechanical_problems(ph, text):
    """Problems a string test can prove, most conservative reading only: a
    quoted literal an acceptance criterion says must appear, word-count bounds,
    and a JSON/HTML output format. A criterion this cannot read mechanically is
    left to the manager's verdict — never guessed at here."""
    problems = []
    low = (text or "").lower()
    for crit in ph.get("acceptance") or []:
        if _MUST_HAVE_RE.search(crit):
            for lit in _QUOTED_RE.findall(crit):
                if lit.strip().lower() not in low:
                    problems.append('missing required text "%s" (criterion: %s)'
                                    % (lit.strip(), crit))
        words = len(re.findall(r"\w+", text or ""))
        m = _MIN_WORDS_RE.search(crit)
        if m and words < int(m.group(1)):
            problems.append("only %d words; criterion: %s" % (words, crit))
        m = _MAX_WORDS_RE.search(crit)
        # 1.5x slack: a model's word count and ours differ (hyphens, markup).
        if m and words > int(int(m.group(1)) * 1.5):
            problems.append("%d words; criterion: %s" % (words, crit))
    fmt = (ph.get("output_format") or "").lower()
    if re.search(r"\bjson\b", fmt) and not re.search(r"\b(or|and)\b", fmt) \
            and not _looks_like_json(text):
        problems.append("output_format is JSON but the output is not valid JSON")
    if re.search(r"\bhtml\b", fmt) and not re.search(r"<[a-zA-Z][^>]*>", text or ""):
        problems.append("output_format is HTML but the output contains no HTML tags")
    return problems[:6]


_PHASE_SCAN_RE = re.compile(
    r'"title"\s*:\s*"([^"]{1,120})"\s*,\s*(?:\n\s*)?"task"\s*:\s*"([^"]{1,2000})"'
    r'(?:\s*,\s*"done_when"\s*:\s*"([^"]{0,300})")?', re.S)


def _phases_from_text(text):
    """Pull phases out of JSON too broken to repair.

    A planner that opens an object per phase and never closes any of them is not
    fixable by balancing braces — the structure is wrong, not truncated — but
    the CONTENT is all there and perfectly readable. Observed verbatim from a
    small planner, and it used to cost the entire team: one unparseable reply
    and the swarm silently became a single model.

    Dependencies are deliberately not scanned: a plan recovered this way is
    already suspect, and running every phase in one parallel wave is the safe
    reading — worst case each worker gets less context, which they all handle."""
    if not text:
        return []
    out = []
    for title, task, done in _PHASE_SCAN_RE.findall(text):
        if task.strip():
            out.append({"title": title.strip(), "task": task.strip(),
                        "done_when": (done or "").strip(), "needs": []})
    return out[:MAX_PHASES]


def _usable(phases):
    """A plan is only worth running as a TEAM if it actually splits the work.
    Fewer than MIN_PHASES means the planner ignored the contract, and a partial
    plan silently drops the rest of the user's request — see MIN_PHASES."""
    return phases if len(phases) >= MIN_PHASES else []


def _waves(phases):
    """Group phases into dependency waves. Everything in one wave is independent
    of everything else in it, so a wave runs CONCURRENTLY.

    Falls back to running the remainder as one wave if the graph is somehow
    still unsatisfiable — a swarm must never hang, and phases whose inputs are
    missing simply get less context, which every worker already tolerates."""
    remaining = list(range(1, len(phases) + 1))
    done = set()
    out = []
    while remaining:
        wave = [i for i in remaining if set(phases[i - 1]["needs"]) <= done]
        if not wave:
            wave = list(remaining)
        out.append(wave)
        done.update(wave)
        remaining = [i for i in remaining if i not in done]
    return out


def _gather(fn, items, timeout, need_one=False):
    """Run fn(item) for every item concurrently; {item: (text, who)} for the
    ones that finished within `timeout` seconds (None = wait for all).

    `need_one`: past the timeout, keep waiting until at least one result
    carries text -- used while the run has nothing at all to deliver yet.

    NOT a `with` block: ThreadPoolExecutor.__exit__ joins every worker, which
    would make the wall-clock cap in run() wait for the very stragglers it
    exists to stop waiting for. Abandoned workers are bounded by the
    dispatcher's own per-hop deadline. One worker raising must not kill the
    others."""
    out = {}

    def _have_text():
        return any(isinstance(r, tuple) and r and r[0] for r in out.values())

    pool = ThreadPoolExecutor(max_workers=max(1, len(items)))
    try:
        pending = {pool.submit(fn, it): it for it in items}
        end = None if timeout is None else time.monotonic() + timeout
        while pending:
            left = None if end is None else end - time.monotonic()
            if left is not None and left <= 0:
                if not need_one or _have_text():
                    break
                left = None          # past the cap, still owed a first answer
            done, _rest = wait(pending, timeout=left, return_when=FIRST_COMPLETED)
            for fut in done:
                item = pending.pop(fut)
                try:
                    out[item] = fut.result()
                except Exception:                                # noqa: BLE001
                    out[item] = ("", None)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return out


def run(messages, dispatch, profile=None, on_event=None, max_seconds=None,
        manager=None):
    """Run the pipeline. `dispatch(msgs, max_tokens, exclude_pids=()) ->
    (text, pid_model)`; it must never raise — an empty text means that call
    failed, and every stage below treats that as "carry on with what we have".

    `max_seconds` is an OVERALL wall clock for the run (None/0 = unbounded).
    MEASURED live: "coding-swarm" took 231 s to answer "What is 5767 plus 1" --
    thirteen sequential-and-parallel calls, each allowed its own hop deadline,
    add up with nothing bounding the sum. Past the cap no NEW stage starts
    (remaining waves, supervisor, review, revision are skipped) and a parallel
    wave stops waiting; the run then synthesises from whatever phases DID
    finish, and the result carries "timed_out": True.

    `profile` (crews.py builds these) overrides the stage system prompts
    ("plan_system"/"phase_system"/"review_system"/"synth_system"), appends
    extra text to the worker system prompt ("worker_extra" — the design crew's
    craft brief), and sets "max_revisions" (0 = a "revise" verdict is only
    folded into synthesis; >=1 = run ONE bounded revision pass first). None
    reproduces the generic pipeline exactly.

    `manager(msgs, max_tokens, purpose) -> (text, who[, tokens])` is an
    optional paid model that plans, supervises, reviews and fixes while the
    free models do the work (purpose is "plan"/"supervise"/"review"/"verify"/
    "fix"). It is only ever shown clipped summaries and asked for short
    replies; "" from it makes that stage use `dispatch` instead. With it, each
    worker's output is checked (cheap tests, then a short manager verdict),
    retried once on another free model, and written by the manager after two
    failures; the result gains "manager_tokens" (and "review_warning" when the
    reviewer's reply stayed unreadable). None = the pipeline exactly as before.

    Returns {"text", "plan", "phases", "review", "models"} — `text` is always a
    non-empty answer unless every single call failed."""
    profile = profile or {}
    plan_system = profile.get("plan_system") or _PLAN_SYSTEM
    phase_system = profile.get("phase_system") or _PHASE_SYSTEM
    review_system = profile.get("review_system") or _REVIEW_SYSTEM
    synth_system = profile.get("synth_system") or _SYNTH_SYSTEM
    worker_extra = str(profile.get("worker_extra") or "").strip()
    if worker_extra:
        phase_system = phase_system + "\n" + worker_extra
    try:
        max_revisions = max(0, int(profile.get("max_revisions") or 0))
    except (TypeError, ValueError):
        max_revisions = 0
    def emit(kind, detail):
        if on_event:
            try:
                on_event(kind, detail)
            except Exception:                                   # noqa: BLE001
                pass

    try:
        cap = float(max_seconds or 0)
    except (TypeError, ValueError):
        cap = 0.0
    stop_at = (time.monotonic() + cap) if cap > 0 else None
    timed_out = [False]

    def _left():
        """Seconds left on the wall clock (None = unbounded, 0 = spent)."""
        if stop_at is None:
            return None
        return max(0.0, stop_at - time.monotonic())

    def _over(stage):
        """True once the cap is spent; says so ONCE in the event trail."""
        if stop_at is None or time.monotonic() < stop_at:
            return False
        if not timed_out[0]:
            timed_out[0] = True
            emit("budget", "wall-clock cap (%ds) reached before %s — "
                           "synthesising from what finished" % (int(cap), stage))
        return True

    brief = _last_user_text(messages)
    models_used = []

    # ---- the optional manager ---------------------------------------------
    # manager(msgs, max_tokens, purpose) -> (text, who[, tokens]). It PLANS,
    # SUPERVISES, REVIEWS, judges each worker's output and fixes what two free
    # attempts could not; the free models still do the work (phases, gap
    # repairs, synthesis). "" from it (off, over budget, failed) makes that one
    # stage fall back to `dispatch`, so a manager can only ever add quality.
    mgr_spent = [0]
    mgr_lock = threading.Lock()
    extras = {}

    def _mgr(msgs, max_tokens, purpose):
        if manager is None:
            return "", None
        try:
            out = manager(msgs, max_tokens, purpose)
        except Exception:                                       # noqa: BLE001
            return "", None
        if not isinstance(out, (tuple, list)) or len(out) < 2:
            return "", None
        text = out[0] if isinstance(out[0], str) else ""
        tokens = out[2] if len(out) > 2 else None
        if tokens is None:
            # The caller did not report real usage: chars/4, like the hub.
            tokens = ((sum(len(str(m.get("content") or "")) for m in msgs)
                       + len(text)) // 4) if text else 0
        try:
            with mgr_lock:
                mgr_spent[0] += max(0, int(tokens))
        except (TypeError, ValueError):
            pass
        return (text, out[1] or "manager") if text.strip() else ("", None)

    def _staged(free_msgs, free_tokens, purpose, mgr_msgs=None, mgr_tokens=None,
                exclude=None):
        """One manager-eligible stage: the manager on a clipped view first,
        else the free dispatch with EXACTLY the call it always made."""
        if manager is not None:
            text, who = _mgr(mgr_msgs or free_msgs, mgr_tokens or free_tokens, purpose)
            if text:
                return text, who
            emit(purpose, "manager unavailable — free models")
        if exclude is None:
            return dispatch(free_msgs, free_tokens)
        return dispatch(free_msgs, free_tokens, exclude_pids=exclude)

    def _finish(result):
        if manager is not None:
            result["manager_tokens"] = mgr_spent[0]
            result.update(extras)
        return result

    def _spent():
        """Wall clock spent — checked from worker threads, so it never emits."""
        return stop_at is not None and time.monotonic() >= stop_at

    mgr_brief = _clip(brief, MANAGER_BRIEF_CHARS)

    # ---- 1. PLAN ----------------------------------------------------------
    emit("plan", "planning")
    plan_text, plan_model = _staged(
        [{"role": "system", "content": plan_system},
         {"role": "user", "content": brief}], PLAN_MAX_TOKENS, "plan",
        mgr_msgs=[{"role": "system", "content": plan_system},
                  {"role": "user", "content": mgr_brief}],
        mgr_tokens=MANAGER_PLAN_TOKENS)
    if plan_model:
        models_used.append(("plan", plan_model))
    plan = _parse_json(plan_text) or {}
    phases = _usable(_clean_phases(plan) or _phases_from_text(plan_text))
    if not phases:
        # ONE retry before giving up on having a team at all. The plan is the
        # linchpin — without it the swarm degrades to a single model, which is
        # not what the user selected — and the usual cause is a planner that
        # narrated instead of answering, or emitted JSON too broken to repair.
        # The retry says so bluntly and shows the exact shape.
        emit("plan", "plan unusable — retrying once")
        strict = ("\n\nOUTPUT JSON ONLY. Start your reply with { and end it "
                  'with }. No prose, no fence, no explanation. Shape:\n'
                  '{"goal":"...","phases":[{"title":"...","task":"...",'
                  '"done_when":"...","needs":[]}]}')
        plan_text, plan_model = _staged(
            [{"role": "system", "content": plan_system},
             {"role": "user", "content": brief + strict}], PLAN_MAX_TOKENS, "plan",
            mgr_msgs=[{"role": "system", "content": plan_system},
                      {"role": "user", "content": mgr_brief + strict}],
            mgr_tokens=MANAGER_PLAN_TOKENS)
        if plan_model:
            models_used.append(("plan:retry", plan_model))
        plan = _parse_json(plan_text) or {}
        phases = _usable(_clean_phases(plan) or _phases_from_text(plan_text))
    if not phases:
        # Planner failed or returned junk -> ONE phase that is the original ask.
        # Degrading to a normal answer beats erroring out.
        phases = [{"title": "Deliver", "task": brief, "done_when": "", "needs": []}]
        emit("plan", "planner unusable — running as a single phase")
    else:
        emit("plan", "%d phases" % len(phases))

    # ---- 2. PHASES — one subagent each, own context, waves run in parallel --
    #
    # Each worker is dispatched with a FRESH two-message conversation: the phase
    # system prompt and its own brief. It never sees the user's conversation, the
    # other workers' prompts, or any phase output it did not declare a need for.
    # That is what makes the swarm bigger than one model: five workers on 32K
    # windows have 160K of usable context between them, where the old sequential
    # pipeline concatenated every previous output into every later phase and ran
    # out on exactly the large builds it was meant for.
    goal = plan.get("goal") or brief
    outputs = {}                      # phase number -> output text
    titles = {}
    exec_pids = set()

    def _run_phase(idx):
        ph = phases[idx - 1]
        ctx = "".join(
            "\n\n### Output of phase %d (%s)\n%s"
            % (n, phases[n - 1]["title"], _clip(outputs[n], DEP_CONTEXT_CHARS))
            for n in ph["needs"] if outputs.get(n))
        user = ("OVERALL GOAL\n%s\n\nYOUR PHASE (%d of %d): %s\n%s%s%s"
                % (goal, idx, len(phases), ph["title"], ph["task"],
                   ("\n\nDone when: " + ph["done_when"]) if ph["done_when"] else "",
                   ("\n\nYou were given these teammates' outputs to build on. Do "
                    "not repeat them, do not rewrite them:" + ctx) if ctx else
                   "\n\nYou are working in parallel with the rest of the team and "
                   "cannot see their output. Produce your part only."))
        # Appended AFTER the context so a phase without brief fields sends the
        # exact prompt the plain pipeline always sent.
        user += _render_brief(ph)
        msgs = [{"role": "system", "content": phase_system},
                {"role": "user", "content": user}]
        first = dispatch(msgs, PHASE_MAX_TOKENS)
        if manager is None:
            return first
        return _verified(idx, ph, msgs, first)

    def _phase_problems(ph, text, trail):
        """[] when the output passes, else concrete problems. Cheap checks
        first (they cost nothing); the manager is asked only about an output
        that already passed them, and only on a clipped excerpt. An unreadable
        verdict passes — a checker must never be what loses a phase."""
        if not (text or "").strip():
            return ["the output was empty"]
        if answer_check is not None:
            try:
                v = answer_check.inspect(text, prompt_text=brief + "\n" + ph["task"])
            except Exception:                                   # noqa: BLE001
                v = {"ok": True}
            if not v.get("ok", True):
                return ["the output degenerated (%s) — produce clean, complete "
                        "output only" % ", ".join(v.get("reasons") or ["junk"])]
        mech = _mechanical_problems(ph, text)
        if mech:
            return mech
        crit = ph.get("acceptance") or ([ph["done_when"]] if ph.get("done_when") else [])
        v_text, v_who = _mgr(
            [{"role": "system", "content": _VERDICT_SYSTEM},
             {"role": "user", "content":
              "TASK: %s\n%s\n\nACCEPTANCE\n- %s%s\n\nOUTPUT (excerpt)\n%s"
              % (ph["title"], _clip(ph["task"], 1200),
                 "\n- ".join(crit) if crit else "(none stated — judge against the task)",
                 ("\n\nOUTPUT FORMAT: " + ph["output_format"]) if ph.get("output_format") else "",
                 _clip(text, VERDICT_OUTPUT_CHARS))}],
            MANAGER_VERDICT_TOKENS, "verify")
        if v_who:
            trail.append(("verify:%s" % ph["title"], v_who))
        verdict = _parse_json(v_text)
        if not isinstance(verdict, dict) or "ok" not in verdict:
            return []
        if verdict.get("ok") is True or str(verdict.get("ok")).lower() == "true":
            return []
        return _str_list(verdict.get("problems"), 4, 300) or \
            ["the manager rejected the output without detail — re-check every criterion"]

    def _verified(idx, ph, msgs, first):
        """Check one worker's output; on failure retry ONCE on a different free
        model with the problems as instructions; after two failures the manager
        writes the phase itself (the only call where it spends many tokens).
        Returns (text, who, trail) — trail is every (role, model) it used."""
        title = ph["title"]
        trail = []
        text, used = first
        if used:
            trail.append(("phase:%s" % title, used))
        failed = set()
        fallback = text or ""
        problems = []
        for attempt in (1, 2):
            problems = _phase_problems(ph, text, trail)
            if not problems:
                return text, used, trail
            emit("verify", "%s: %s" % (title[:30], problems[0][:44]))
            if used:
                failed.add(used.split("/", 1)[0])
            if (text or "").strip():
                fallback = text
            if attempt == 2 or _spent():
                break
            retry = [msgs[0], {"role": "user", "content":
                               msgs[1]["content"] + "\n\nA PREVIOUS ATTEMPT AT THIS "
                               "PHASE WAS REJECTED. Fix every one of these problems:\n- "
                               + "\n- ".join(problems)}]
            text, used = dispatch(retry, PHASE_MAX_TOKENS, exclude_pids=tuple(failed))
            if used:
                trail.append(("phase-retry:%s" % title, used))
        if not _spent():
            fix_text, fix_who = _mgr(
                [{"role": "system", "content": _FIX_SYSTEM},
                 {"role": "user", "content":
                  "OVERALL GOAL\n%s\n\nPHASE: %s\n%s%s%s\n\nPROBLEMS TO FIX\n- %s"
                  "\n\nLAST ATTEMPT (excerpt)\n%s"
                  # Clipped like every other manager input: when the planner
                  # falls back to one phase, its task IS the user's whole brief.
                  % (_clip(goal, 1500), title, _clip(ph["task"], MANAGER_BRIEF_CHARS),
                     ("\n\nDone when: " + _clip(ph["done_when"], MANAGER_PHASE_CHARS))
                     if ph.get("done_when") else "",
                     _render_brief(ph), "\n- ".join(problems),
                     _clip(fallback, VERDICT_OUTPUT_CHARS) or "(empty)")}],
                MANAGER_FIX_TOKENS, "fix")
            if fix_text:
                emit("verify", "%s: fixed by the manager" % title[:30])
                trail.append(("fix:%s" % title, fix_who))
                return fix_text, fix_who, trail
        # Nothing better: ship the last real attempt rather than drop the phase.
        return fallback, (used if fallback == text else None), trail

    for wave in _waves(phases):
        if outputs and _over("the next wave"):
            break            # at least one phase is in hand -- stop adding more
        names = ", ".join(phases[i - 1]["title"] for i in wave)
        emit("phase", ("%d in parallel: %s" % (len(wave), names)) if len(wave) > 1
             else "1/%d %s" % (len(phases), names))
        if len(wave) == 1 and stop_at is None:
            results = {wave[0]: _run_phase(wave[0])}
        else:
            # One worker dying must not kill the wave (_gather maps it to an
            # empty result). Under a cap, a wave stops waiting once the clock is
            # spent -- but, while nothing at all is in hand, not before one
            # phase has answered: a slow planner must not leave nothing to
            # deliver.
            results = _gather(_run_phase, list(wave), _left(),
                              need_one=not outputs)
            if len(results) < len(wave):
                _over("the rest of the wave")
        # Applied in phase order, not completion order, so the assembled draft
        # reads in the sequence the supervisor planned.
        for i in sorted(results):
            text, used = results[i][0], results[i][1]
            if len(results[i]) > 2:
                # Verified phase: its trail names every worker, retry, verdict
                # and fix. Only FREE workers count as executors -- the reviewer
                # must differ from them, not from the manager.
                for role, who in results[i][2]:
                    models_used.append((role, who))
                    if role.startswith("phase"):
                        exec_pids.add(who.split("/", 1)[0])
            elif used:
                models_used.append(("phase:%s" % phases[i - 1]["title"], used))
                exec_pids.add(used.split("/", 1)[0])
            if text:
                outputs[i] = text
                titles[i] = phases[i - 1]["title"]

    done = [{"title": titles[i], "output": outputs[i]} for i in sorted(outputs)]

    # ---- 2b. SUPERVISOR — did the team actually cover the plan? -------------
    # Workers that ran in parallel could not see each other, so this is where a
    # genuine gap or a contradiction between them gets caught. Skipped when only
    # one phase produced anything: there is no team to reconcile.
    if len(done) > 1 and not _over("the supervisor"):
        emit("supervise", "checking coverage")
        plan_lines = "\n".join("%d. %s — %s" % (i, p["title"], p["task"])
                               for i, p in enumerate(phases, 1))
        sup_text, sup_model = _staged(
            [{"role": "system", "content": _SUPERVISE_SYSTEM},
             {"role": "user", "content": "GOAL\n%s\n\nPLAN\n%s\n\nWHAT THE TEAM PRODUCED\n%s"
              % (goal, plan_lines,
                 "\n\n".join("## %s\n%s" % (d["title"], _clip(d["output"], DEP_CONTEXT_CHARS))
                             for d in done))}],
            SUPERVISE_MAX_TOKENS, "supervise",
            mgr_msgs=[{"role": "system", "content": _SUPERVISE_SYSTEM},
                      {"role": "user", "content":
                       "GOAL\n%s\n\nPLAN\n%s\n\nWHAT THE TEAM PRODUCED (excerpts)\n%s"
                       % (_clip(goal, 1500), _clip(plan_lines, 4000),
                          "\n\n".join("## %s\n%s" % (d["title"],
                                                     _clip(d["output"], MANAGER_PHASE_CHARS))
                                      for d in done))}],
            mgr_tokens=MANAGER_SUPERVISE_TOKENS)
        if sup_model:
            models_used.append(("supervisor", sup_model))
        gaps = []
        for g in ((_parse_json(sup_text) or {}).get("missing") or [])[:MAX_REPAIRS]:
            if isinstance(g, dict) and str(g.get("task") or "").strip():
                gaps.append({"title": str(g.get("title") or "Gap").strip()[:80],
                             "task": str(g["task"]).strip()[:1200]})
        if gaps and not _over("the gap repairs"):
            emit("supervise", "%d gap%s to fill" % (len(gaps), "" if len(gaps) == 1 else "s"))

            def _repair(k):
                g = gaps[k]
                return dispatch(
                    [{"role": "system", "content": phase_system},
                     {"role": "user", "content": "OVERALL GOAL\n%s\n\nYOUR TASK: %s\n%s"
                      % (goal, g["title"], g["task"])}],
                    PHASE_MAX_TOKENS)
            fixed = _gather(_repair, list(range(len(gaps))), _left())
            if len(fixed) < len(gaps):
                _over("the rest of the gap repairs")
            for k in sorted(fixed):
                text, used = fixed[k]
                if used:
                    models_used.append(("repair:%s" % gaps[k]["title"], used))
                if text:
                    done.append({"title": gaps[k]["title"], "output": text})

    if not done:
        return _finish({"text": "", "plan": plan, "phases": [], "review": None,
                        "models": models_used, "timed_out": timed_out[0]})

    draft = "\n\n".join("## %s\n%s" % (d["title"], d["output"]) for d in done) \
        if len(done) > 1 else done[0]["output"]

    # ---- 3. REVIEW (different provider on purpose) ------------------------
    # Skipped past the wall clock: an unreviewed answer now beats a reviewed
    # one after the client has given up.
    def _review(suffix=""):
        # The manager reviews excerpts; the full draft goes only to a free
        # reviewer, exactly as before.
        mgr_work = _clip("\n\n".join(
            "## %s\n%s" % (d["title"], _clip(d["output"], MANAGER_PHASE_CHARS))
            for d in done) if len(done) > 1 else done[0]["output"], MANAGER_DRAFT_CHARS)
        return _staged(
            [{"role": "system", "content": review_system},
             {"role": "user", "content": "BRIEF\n%s\n\nWORK\n%s%s" % (brief, draft, suffix)}],
            REVIEW_MAX_TOKENS, "review",
            mgr_msgs=[{"role": "system", "content": review_system},
                      {"role": "user", "content": "BRIEF\n%s\n\nWORK (excerpts)\n%s%s"
                       % (mgr_brief, mgr_work, suffix)}],
            mgr_tokens=MANAGER_REVIEW_TOKENS, exclude=tuple(exec_pids))

    reviewed = not _over("the review")
    if not reviewed:
        review_text, review_model = "", None
    else:
        emit("review", "reviewing")
        review_text, review_model = _review()
    if review_model:
        models_used.append(("review", review_model))
    review = _parse_json(review_text) or {}
    if manager is not None and reviewed and \
            str(review.get("verdict") or "").lower() not in ("ship", "revise"):
        # Without a manager an unreadable review silently counts as "ship" (the
        # plain pipeline's long-standing behaviour). With one, ask ONCE more,
        # then ship with a visible warning instead of pretending it passed.
        emit("review", "review unreadable — asking once more")
        review_text, review_model = _review(
            "\n\nREPLY WITH JSON ONLY: {\"verdict\": \"ship\" | \"revise\", "
            "\"problems\": [...]}")
        if review_model:
            models_used.append(("review:retry", review_model))
        review = _parse_json(review_text) or {}
        if str(review.get("verdict") or "").lower() not in ("ship", "revise"):
            extras["review_warning"] = ("the reviewer's reply was unreadable twice "
                                        "— shipped without a review")
            emit("review", "unreadable twice — shipping unreviewed")
    problems = [str(p).strip() for p in (review.get("problems") or []) if str(p).strip()]
    needs_work = (str(review.get("verdict") or "").lower() == "revise") and problems

    # ---- 3b. REVISION — one bounded pass, only when the profile asks --------
    # max_revisions 0 (the default, and the plain "swarm" model) keeps today's
    # behaviour: a "revise" verdict is only handed to synthesis. With >=1 a
    # worker is shown the draft plus the reviewer's problems and returns the
    # corrected work — the Claude Code style plan->do->review->fix loop. It is
    # capped at ONE pass on purpose: each loop is a full extra model call, and
    # a reviewer that will not say "ship" would otherwise loop forever.
    revised = False
    if needs_work and max_revisions >= 1 and not _over("the revision"):
        emit("revise", "fixing %d problem%s" % (len(problems), "" if len(problems) == 1 else "s"))
        def _rev_user(b):
            return ("BRIEF\n%s\n\nDRAFT\n%s\n\nREVIEWER PROBLEMS TO FIX\n- %s\n\n"
                    "Return the COMPLETE corrected work — the full draft with every "
                    "problem fixed, not a diff, not a list of changes."
                    % (b, _clip(draft, DEP_CONTEXT_CHARS), "\n- ".join(problems[:10])))
        # The final FIX is the manager's when there is one, capped at one
        # phase's worth of output -- the draft it is shown is already clipped.
        rev_text, rev_model = _staged(
            [{"role": "system", "content": phase_system},
             {"role": "user", "content": _rev_user(brief)}],
            SYNTH_MAX_TOKENS, "fix",
            mgr_msgs=[{"role": "system", "content": phase_system},
                      {"role": "user", "content": _rev_user(mgr_brief)}],
            mgr_tokens=MANAGER_FIX_TOKENS)
        if rev_model:
            models_used.append(("revision", rev_model))
        if rev_text:
            draft = rev_text
            revised = True

    # ---- 4. SYNTHESIS -----------------------------------------------------
    # Single phase that the reviewer passed -> the draft IS the answer; another
    # rewrite would only risk making it worse.
    if len(done) == 1 and not needs_work:
        emit("done", "single phase, review passed")
        return _finish({"text": draft, "plan": plan, "phases": done, "review": review,
                        "models": models_used, "timed_out": timed_out[0]})

    emit("synthesis", "assembling")
    synth_user = "BRIEF\n%s\n\nPHASE OUTPUTS\n%s" % (brief, draft)
    if problems and not revised:
        # A completed revision pass already fixed these; handing them to
        # synthesis again would ask it to fix problems that no longer exist.
        synth_user += "\n\nREVIEWER PROBLEMS TO FIX\n- " + "\n- ".join(problems[:10])
    final_text, synth_model = dispatch(
        [{"role": "system", "content": synth_system},
         {"role": "user", "content": synth_user}], SYNTH_MAX_TOKENS)
    if synth_model:
        models_used.append(("synthesis", synth_model))
    emit("done", "complete")
    return _finish({"text": final_text or draft, "plan": plan, "phases": done,
                    "review": review, "models": models_used, "timed_out": timed_out[0]})


def trailer_summary(result):
    """The format_answer trailer as ONE header-safe line, for callers that must
    receive only the deliverable (a CLI writes the answer into a file or its
    own transcript, where "**Models used**" is noise it then has to delete).
    Same facts: crew, plan steps, per-stage models, reviewer problem count,
    and whether the wall clock cut the run short. ASCII only -- HTTP header
    values are latin-1 and a phase title can be anything -- and bounded."""
    result = result or {}
    parts = []
    if result.get("crew"):
        parts.append("crew=%s" % result["crew"])
    phases = result.get("phases") or []
    if phases:
        parts.append("plan=" + " | ".join(str(p.get("title") or "") for p in phases))
    models = result.get("models") or []
    if models:
        parts.append("models=" + ", ".join("%s:%s" % (s, m) for s, m in models))
    review = result.get("review") or {}
    problems = [p for p in (review.get("problems") or []) if str(p).strip()]
    if problems:
        parts.append("reviewer_raised=%d" % len(problems))
    if result.get("timed_out"):
        parts.append("timed_out=1")
    if result.get("manager_tokens"):
        parts.append("manager_tokens=%d" % int(result["manager_tokens"]))
    if result.get("review_warning"):
        parts.append("review_unreadable=1")
    line = "; ".join(parts)
    line = line.encode("ascii", "replace").decode("ascii")
    line = re.sub(r"[\r\n\t]+", " ", line)
    return line[:1500]


def format_answer(result):
    """Final text plus a compact, honest trailer: the plan that was followed and
    which model did which stage. The user asked for a visible plan/todolist, and
    showing the real per-stage models is also the answer to 'is it really using
    the good models' — the thing that made the flash-lite fallback invisible."""
    text = (result.get("text") or "").strip()
    if not text:
        return ""
    lines = []
    phases = result.get("phases") or []
    if phases:
        lines.append("\n\n---\n**Plan followed**")
        goal = (result.get("plan") or {}).get("goal")
        if goal:
            lines.append("*%s*" % goal)
        for i, p in enumerate(phases, 1):
            lines.append("%d. [x] %s" % (i, p["title"]))
    models = result.get("models") or []
    if models:
        lines.append("\n**Models used**")
        lines.extend("- %s — `%s`" % (stage, mid) for stage, mid in models)
    review = result.get("review") or {}
    problems = [p for p in (review.get("problems") or []) if str(p).strip()]
    if problems:
        # NOT "applied above": the problems are handed to the synthesis stage,
        # but whether it actually fixed each one is not something this code can
        # verify — and claiming it did would be exactly the kind of unverifiable
        # assertion the reviewer exists to catch. Show them and let the user judge.
        lines.append("\n**Reviewer raised** (passed to the final pass — check them)")
        lines.extend("- %s" % p for p in problems[:5])
    if result.get("review_warning"):
        lines.append("\n**Warning:** %s" % result["review_warning"])
    if result.get("manager_tokens"):
        lines.append("\n*Manager tokens: %d*" % int(result["manager_tokens"]))
    return text + "\n".join(lines)
