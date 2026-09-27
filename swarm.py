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
import ast
import difflib
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
# write at length is fixing a SHORT phase two free models could not get right
# (a long one, and the final revision, get directed fixes -- see below).
# The hub cuts a manager reply at max_tokens*4 chars AFTER the CLI produced
# (and billed) it, so a cap below a real plan only ever destroys the JSON: a
# 3-phase plan with inputs/constraints/acceptance runs 4-7K characters.
MANAGER_PLAN_TOKENS = 2500
MANAGER_SUPERVISE_TOKENS = 600
# ONE batched verdict per wave (+ the supervisor's coverage question on the
# last wave): per-phase entries, so more room than one verdict, far less than
# one call per phase (MEASURED: 100-150 s per subscription CLI call).
MANAGER_CHECK_TOKENS = 1200
CHECK_OUTPUT_CHARS = 6000        # all judged outputs together, in a batched verdict
CHECK_EARLIER_CHARS = 2400       # already-checked phases, shown for consistency only
# Each manager call has its OWN deadline (and never outlives the run's wall
# clock): past it the stage takes its free fallback and the run moves on.
MANAGER_DEADLINES = {"plan": 150, "verify": 120, "supervise": 120, "review": 150,
                     "fix": 150}
MANAGER_MIN_SECONDS = 20         # less left on the clock than this: no manager call
MANAGER_REVIEW_TOKENS = 800
MANAGER_VERDICT_TOKENS = 300
MANAGER_FIX_TOKENS = PHASE_MAX_TOKENS
MANAGER_BRIEF_CHARS = 6000       # the user's brief as the manager sees it
MANAGER_PHASE_CHARS = 1500       # per phase, in the supervise/review summaries
MANAGER_DRAFT_CHARS = 8000       # whole draft, in the review summary
VERDICT_OUTPUT_CHARS = 3000      # one worker's output, in a per-phase verdict
# DIRECTED FIXES. The manager used to REWRITE the final draft (and a failed
# phase) itself from a view clipped to a few thousand characters -- so on a
# long draft the "fixed" version silently lost everything in the trimmed
# middle, and the manager was paid to re-type text it could not even see. Now
# it writes short FIX INSTRUCTIONS from compact inputs, a FREE model applies
# them to the FULL text, and the manager only confirms with a short verdict
# on what changed.
MANAGER_INSTRUCT_TOKENS = 900
FIX_EXCERPT_CHARS = 4000         # the work, as the manager sees it to instruct
CHANGES_CHARS = 3000             # the diff, as the manager sees it to confirm
APPLY_MAX_TOKENS = 8000          # a free apply's output ceiling (big drafts)
# A free apply that returns less than this share of the work dropped content
# (output cap, or it "summarised"): rejected before any verdict is paid for.
APPLY_MIN_KEEP = 0.7
# Output chars one token buys, generously: a work longer than
# apply-ceiling * this / APPLY_MIN_KEEP cannot come back whole from one apply.
APPLY_CHARS_PER_TOKEN = 4

# ---- The conversation, not just its last message --------------------------
# REPORTED: the brief was ONLY the last user message, so a follow-up such as
# "make it better" reached the planner as three words with no idea what "it"
# was, and the team rebuilt something unrelated. A follow-up's brief now
# carries a BOUNDED digest of the conversation: what the caller knows about it
# (the rolling compaction recap, or an /agent session's memory block), the
# previous answer when the request refers back to it, and the earlier
# requests. The request itself is never clipped; only the added context is.
BRIEF_CONTEXT_CHARS = 6000       # everything added to the request, in total
CALLER_CONTEXT_CHARS = 2500      # recap / memory handed in by the caller
PREV_ANSWER_CHARS = 3000         # the previous deliverable, when referred to
EARLIER_REQUEST_CHARS = 400      # each earlier request
MANAGER_CONTEXT_CHARS = 1500     # the context, as the (paid) manager sees it
# Every worker also sees the user's request itself (bounded): a planner's task
# text is capped and paraphrased, and the details it dropped were the ones
# the review then flagged as missing.
WORKER_BRIEF_CHARS = 6000
PHASE_TASK_CHARS = 4000
# Synthesis output ceiling scales with what it has to assemble (chars/3 plus
# headroom), bounded here; the hub lowers it further to a model's learned
# output cap per hop.
SYNTH_MAX_CAP = 16000
# The free "directed fix" when no manager is configured: how much of the
# draft the instructing model sees (it is free, so more than the manager).
FREE_INSTRUCT_CHARS = 12000
FREE_INSTRUCT_TOKENS = 1500
_CONTEXT_HEADING = ("CONVERSATION CONTEXT (earlier in this conversation -- use it "
                    "to resolve what the request above refers to; do what the "
                    "request asks now, do not redo finished work unless asked)")
# A follow-up that points back at earlier work. Short messages nearly always
# do ("make it blue"); a long one only when it says so.
_REFERS_BACK_RE = re.compile(
    r"\b(previous|above|earlier|again|better|improve|continue|same|instead|"
    r"redo|rewrite|revise|polish|tweak|shorter|longer|expand|extend|your "
    r"(answer|code|version|draft|page|output)|the (code|page|draft|version|"
    r"answer|output|file|site|app|design))\b", re.I)
_REFER_SHORT_CHARS = 400


def _clip(text, limit):
    """Head + tail, so a truncated dependency keeps how it starts AND how it
    ends — the middle of a document is the safest part to lose."""
    text = text or ""
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    return text[:head] + "\n\n[... trimmed ...]\n\n" + text[-(limit - head):]


def _changes(before, after, limit=CHANGES_CHARS):
    """What an apply changed, as a clipped unified diff ("" = nothing). The
    manager confirms a directed fix from THIS, not from a head+tail excerpt
    that could miss every edited line in the middle."""
    diff = difflib.unified_diff((before or "").splitlines(),
                                (after or "").splitlines(),
                                "before", "after", n=1, lineterm="")
    return _clip("\n".join(list(diff)[2:]), limit)


def _apply_tokens(work, floor):
    """Output ceiling for a free apply: enough to return the WHOLE work
    (chars/4 plus headroom), never below the stage's usual cap."""
    return max(floor, min(APPLY_MAX_TOKENS, len(work or "") // 3 + 500))

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

# The same verdict for SEVERAL workers in one manager call (one per wave).
_BATCH_VERDICT_SYSTEM = (
    "You check SEVERAL team members' outputs, each against its own task and "
    "acceptance criteria. You see clipped excerpts; do not fault what may sit "
    "in the trimmed middle.\n"
    "Reply with JSON ONLY:\n"
    '{"phases": [{"n": <phase number>, "ok": true | false, "problems": '
    '["<concrete, fixable>"]}]}\n'
    "One entry for every phase under JUDGE THESE. Fail a phase only for a "
    "missed criterion, a wrong or invented fact, missing required content, the "
    "wrong format, or an inconsistency with the work it builds on (wrong names, "
    "signatures, flags or files). Style is not a problem. At most 4 problems "
    "per phase."
)

# Added on the LAST wave: the supervisor's coverage question rides on the
# same call instead of costing another one.
_CHECK_COVERAGE_SYSTEM = (
    "\nYou are also the supervisor: against the PLAN, add to the same JSON\n"
    '"missing": [{"title": "<short>", "task": "<the specific gap to fill>"}]\n'
    "-- ONLY work that was assigned and genuinely is not there anywhere, or that "
    "two workers produced incompatibly; at most 2 items, [] when the team "
    "covered the goal. No polish, no new scope."
)
_CHECK_PARTS_SYSTEM = (
    '\nAlso add "parts_missing": [<numbers of the REQUIRED PARTS genuinely '
    "absent from ALL the work shown>] -- never one that may sit in a trimmed "
    "middle."
)

# The manager writing a phase itself, after two free attempts failed.
_FIX_SYSTEM = (
    "Two team members failed this phase. Write the phase's output yourself, "
    "complete and correct, meeting every acceptance criterion and fixing every "
    "listed problem. Output only the artefact — no preamble, no notes. Never "
    "invent facts; mark missing real-world data as [NEEDS INPUT: what]."
)

# The manager directing a fix it cannot see in full (see MANAGER_INSTRUCT_TOKENS).
_INSTRUCT_SYSTEM = (
    "You direct an editor who will apply your instructions to the FULL work. "
    "You see only excerpts of it; the editor sees all of it.\n"
    "Write numbered, concrete edit instructions that fix every listed problem: "
    "where (section, heading or quoted anchor text), what to change, and the "
    "exact replacement text when it is short. Only what the problems require — "
    "no rewrite of untouched parts, no new scope, no commentary. Never invent "
    "facts; ask for [NEEDS INPUT: what] markers where real data is missing. "
    "Plain text, at most 15 instructions."
)

# The FREE model applying those instructions to the full text.
_APPLY_SYSTEM = (
    "You apply an editor's fix instructions to a piece of work. Return the "
    "COMPLETE work with every instruction applied. Everything the instructions "
    "do not touch stays exactly as it is — do not shorten, summarise or "
    "restructure it. Not a diff, not a list of changes: no preamble, no notes. "
    "Preserve [NEEDS INPUT: ...] markers verbatim."
)

# The manager's short confirmation of an applied fix, judged from the diff.
_CONFIRM_SYSTEM = (
    "You confirm that an editor applied your fix instructions. You see the "
    "problems, your instructions and a diff of what changed — everything not "
    "in the diff is unchanged.\n"
    "Reply with JSON ONLY:\n"
    '{"ok": true | false, "problems": ["<what is still wrong>"]}\n'
    "Fail it only for a listed problem left unfixed, a change that broke or "
    "dropped content, or an invented fact. Style is not a problem. At most 4 "
    "problems."
)


def _text_of(m):
    c = (m or {}).get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):                # multimodal turn -> text parts only
        return "\n".join(p.get("text", "") for p in c
                         if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _last_user_text(messages):
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user":
            return _text_of(m)
    return ""


def _refers_back(request):
    r = (request or "").strip()
    return len(r) <= _REFER_SHORT_CHARS or bool(_REFERS_BACK_RE.search(r))


def conversation_brief(messages, context=""):
    """(brief, context_block) for a pipeline run.

    `brief` is the last user message, unclipped, followed -- only when there
    IS something -- by a bounded context block (<= BRIEF_CONTEXT_CHARS):
    the caller's `context` (recap / memory), the previous assistant answer
    when the request refers back to it, and the earlier user requests.
    A single-message conversation with no context returns the message
    itself byte-for-byte, so the opening turn is exactly what it was."""
    msgs = [m for m in (messages or []) if isinstance(m, dict)]
    last_idx = None
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") == "user":
            last_idx = i
            break
    request = _text_of(msgs[last_idx]) if last_idx is not None else ""
    prior = msgs[:last_idx] if last_idx is not None else msgs
    prior = [m for m in prior if m.get("role") in ("user", "assistant")]
    context = str(context or "").strip()
    if not prior and not context:
        return request, ""
    budget = BRIEF_CONTEXT_CHARS
    sections = []
    if context:
        c = _clip(context, min(CALLER_CONTEXT_CHARS, budget))
        sections.append("What the conversation established so far:\n" + c)
        budget -= len(sections[-1])
    prev = ""
    for m in reversed(prior):
        if m.get("role") == "assistant" and _text_of(m).strip():
            prev = _text_of(m).strip()
            break
    if prev and budget > 200 and _refers_back(request):
        room = min(PREV_ANSWER_CHARS, budget - 120)
        sections.append("The previous answer -- the work this request refers to "
                        "(%d characters%s):\n%s"
                        % (len(prev), ", excerpt" if len(prev) > room else "",
                           _clip(prev, room)))
        budget -= len(sections[-1])
    earlier = [" ".join(_text_of(m).split()) for m in prior if m.get("role") == "user"]
    earlier = [e for e in earlier if e]
    if earlier and budget > 120:
        lines, used = [], len("Earlier requests (oldest first):")
        for e in reversed(earlier):
            line = "- " + (e if len(e) <= EARLIER_REQUEST_CHARS
                           else e[:EARLIER_REQUEST_CHARS - 3] + "...")
            if used + len(line) + 1 > budget:
                break
            lines.insert(0, line)
            used += len(line) + 1
        if lines:
            sections.append("Earlier requests (oldest first):\n" + "\n".join(lines))
    block = "\n\n".join(sections).strip()
    if not block:
        return request, ""
    block = _clip(block, BRIEF_CONTEXT_CHARS)
    return "%s\n\n---\n%s\n%s" % (request, _CONTEXT_HEADING, block), block


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
    best = None
    for candidate in ([s[:j + 1]] if j > 0 else []) + [s]:
        try:
            out = json.loads(candidate)
            if isinstance(out, dict):
                return out                # complete, valid JSON: nothing to weigh
        except ValueError:
            out = _repair_json(candidate)
        # Repaired readings: keep the one that recovered MORE. A reply cut off
        # mid-object has its last "}" at the end of an EARLIER phase, and the
        # text up to it silently drops every later (complete) field.
        if isinstance(out, dict) and (best is None or
                                      len(json.dumps(out)) > len(json.dumps(best))):
            best = out
    return best


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
        out = _salvage_truncated(s)
    return out if isinstance(out, dict) else None


def _salvage_truncated(s):
    """JSON cut off mid-value (a reply clipped at its token cap: `"inputs": `
    or `"acceptance": ["inclu`): walk back to the last separator outside a
    string, drop the half-written member, close what is open. Everything
    complete before the cut survives. None when nothing does."""
    cuts, in_str, esc = [], False, False
    for i, ch in enumerate(s):
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if not in_str and ch in ",{[":
            cuts.append(i)
    for pos in reversed(cuts[-60:]):
        cand = s[:pos] if s[pos] == "," else s[:pos + 1]
        stack, in_str, esc = [], False, False
        for ch in cand:
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
        cand = re.sub(r",\s*$", "", cand.rstrip())
        cand += "".join("}" if c == "{" else "]" for c in reversed(stack))
        try:
            out = json.loads(re.sub(r",\s*([}\]])", r"\1", cand))
        except ValueError:
            continue
        if isinstance(out, dict):
            return out
    return None


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
            "task": task[:PHASE_TASK_CHARS],
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


# ---- code phases: a Python file, its tests, its docs ------------------------
# MEASURED 2026-09-27 (manager sub-claude/sonnet): "tally.py + test_tally.py +
# README" ran 772 s and shipped WITHOUT the tests. Four sequential manager calls
# ate the wall clock before the tests wave finished -- and nothing tied the
# tests to the code: a tests worker planned in parallel (the planner is told to
# maximise parallelism) never sees tally.py and invents an API; one that does
# see it gets the file clipped, with no list of the names it may import. These
# helpers wire tests/docs to the code they describe, hand them its interface,
# and prove the cheap things (syntax, test count, imports that exist) without
# a paid verdict.
_PY_FILE_RE = re.compile(r"\b([\w\-]+\.py)\b", re.I)
_TEST_FILE_RE = re.compile(r"^(?:test_[\w\-]*|[\w\-]*_test)\.py$", re.I)
_TEST_PHASE_RE = re.compile(r"\b(?:pytest|unit\s*tests?|test\s+suite|tests?\s+file|"
                            r"test_[\w\-]+\.py|[\w\-]+_test\.py)\b", re.I)
_DOCS_PHASE_RE = re.compile(r"\b(?:readme|documentation|docs?\s+section|usage\s+section)\b",
                            re.I)
# A phase whose OUTPUT is documentation even when its title names the .py
# file it documents ("README.md for tally.py").
_DOCS_OUTPUT_RE = re.compile(r"\breadme\b|\b[\w\-]+\.md\b|\bmarkdown\b", re.I)
_DOCS_NEEDS_CODE_RE = re.compile(r"\b(?:flags?|options?|usage|arguments?|cli|command[- ]line|"
                                 r"examples?|api)\b", re.I)
_FENCED_RE = re.compile(r"```[ \t]*([\w+\-.]*)[^\n]*\n(.*?)(?:```|\Z)", re.S)
_PY_LOOK_RE = re.compile(r"^\s*(?:def |class |import |from [\w.]+ import |@\w|if __name__)",
                         re.M)
_ADD_ARG_RE = re.compile(r"add_argument\(\s*((?:[\"'][^\"']+[\"']\s*,?\s*)+)")
_BACKTICK_RE = re.compile(r"`([^`\n]{1,80})`")
_DEFINES_RE = re.compile(r"\b(?:define[sd]?|expose[sd]?|export[s]?|provide[sd]?|"
                         r"call(?:s|ed)?|named)\b", re.I)
# Words a mechanical check fully accounts for: a criterion left with nothing
# else after its literals / counts are removed is PROVEN by the check.
_CHECK_WORDS = frozenset((
    "mention mentions mentioned name named show shows titled heading headings "
    "text exact exactly literal string phrase appear appears word words "
    "test tests pytest function functions table tables markdown json valid "
    "define defines defined expose exposes export exports call calls called "
    "provide provides output result present verbatim least most fewer under "
    "maximum minimum use uses using include includes contain contains has "
    "named name").split())



def _py_target(ph):
    """The Python file this phase produces (lower-case), or "". Title first,
    then output_format, then the task; a file only MENTIONED as input is not
    a target unless nothing else names one."""
    for field in ("title", "output_format", "task"):
        m = _PY_FILE_RE.search(str(ph.get(field) or ""))
        if m:
            return m.group(1).lower()
    return ""


_TEST_TASK_START_RE = re.compile(r"\s*(?:write|create|add|produce|build|implement)?\s*"
                                 r"(?:a\s+|the\s+)?(?:pytest\b|unit\s*tests?\b|tests?\b)",
                                 re.I)


def _is_test_phase(ph):
    """Conservative: the title / output format decide; the task only when
    they name nothing (a code phase whose task says "easy to unit test" is
    NOT a tests phase)."""
    title = str(ph.get("title") or "")
    fmt = str(ph.get("output_format") or "")
    for field in (title, fmt):
        m = _PY_FILE_RE.search(field)
        if m:
            return bool(_TEST_FILE_RE.match(m.group(1)))
    # "Tests" / "Unit tests" -- not a bare "test" ("A/B test copy").
    if _TEST_PHASE_RE.search(title + " " + fmt) or re.search(r"\btests\b", title, re.I):
        return True
    task = str(ph.get("task") or "")
    m = _PY_FILE_RE.search(task)
    return bool((m and _TEST_FILE_RE.match(m.group(1))) or _TEST_TASK_START_RE.match(task))


def _is_docs_phase(ph):
    """A README / documentation phase (title or output format say so, or,
    when they name nothing, the task opens with it). Never a tests phase."""
    title = str(ph.get("title") or "")
    fmt = str(ph.get("output_format") or "")
    if _is_test_phase(ph):
        return False
    if _DOCS_OUTPUT_RE.search(title + " " + fmt) or _DOCS_PHASE_RE.match(title.strip()):
        return True
    if _PY_FILE_RE.search(title) or _PY_FILE_RE.search(fmt):
        return False
    if _DOCS_PHASE_RE.search(title + " " + fmt):
        return True
    return bool(_DOCS_PHASE_RE.search(str(ph.get("task") or "")[:80]))


def _code_file(ph):
    """The NON-test Python file a phase produces, or ""."""
    t = _py_target(ph)
    return "" if (not t or _TEST_FILE_RE.match(t) or _is_test_phase(ph)
                  or _is_docs_phase(ph)) else t


def _py_sources(text):
    """The Python code in a worker's output: every fenced block tagged
    python/py (or untagged but plainly Python). With no fence at all, the
    whole text when it starts like code. [] when there is none."""
    t = text or ""
    blocks = []
    for lang, body in _FENCED_RE.findall(t):
        lang = (lang or "").lower()
        if lang in ("python", "py", "python3") or (not lang and _PY_LOOK_RE.search(body)):
            blocks.append(body)
    if blocks or "```" in t:
        return blocks
    first = next((ln for ln in t.splitlines() if ln.strip()), "")
    if _PY_LOOK_RE.match(first) or first.startswith(("#!", '"""', "# ")):
        return [t]
    return []


def _syntax_error(code):
    """"line N: msg" when `code` is not valid Python, else ""."""
    try:
        ast.parse(code)
        return ""
    except SyntaxError as exc:
        return "line %s: %s" % (exc.lineno, exc.msg)
    except (ValueError, RecursionError, MemoryError):
        return ""


def _top_names(code, imports=True):
    """Names a module defines at top level (functions, classes, assignments,
    and -- unless imports=False -- the names it imports, which a test may
    legally import from it too). None when it does not parse."""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for tgt in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                for n in ast.walk(tgt):
                    if isinstance(n, ast.Name):
                        names.add(n.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)) and imports:
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.If, ast.Try)):
            for sub in ast.walk(node):
                if isinstance(sub, (ast.FunctionDef, ast.ClassDef)):
                    names.add(sub.name)
    return names


def _interface(code, limit=1500):
    """A short, exact interface of a Python module: top-level signatures,
    classes with their public methods, and the command-line flags it
    registers with argparse. "" when it does not parse."""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return ""
    lines = []

    def _sig(fn):
        try:
            args = ast.unparse(fn.args)
        except Exception:                                       # noqa: BLE001
            args = "..."
        ret = ""
        if getattr(fn, "returns", None) is not None:
            try:
                ret = " -> " + ast.unparse(fn.returns)
            except Exception:                                   # noqa: BLE001
                ret = ""
        return "def %s(%s)%s" % (fn.name, args, ret)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lines.append(_sig(node))
        elif isinstance(node, ast.ClassDef):
            lines.append("class %s" % node.name)
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                        (not sub.name.startswith("_") or sub.name == "__init__"):
                    lines.append("    " + _sig(sub))
    flags = []
    for m in _ADD_ARG_RE.finditer(code):
        for f in re.findall(r"[\"']([^\"']+)[\"']", m.group(1)):
            if f not in flags:
                flags.append(f)
    if flags:
        lines.append("command-line arguments (argparse): " + ", ".join(flags))
    if re.search(r"^if\s+__name__\s*==\s*[\"']__main__[\"']", code, re.M):
        lines.append("runs as a script (if __name__ == \"__main__\")")
    out = "\n".join(lines)
    return out if len(out) <= limit else out[:limit - 3].rstrip() + "..."


def _test_count_wanted(ph, request=""):
    """N from "at least N tests" in the phase (or the user's part it serves)."""
    for src in (_phase_text(ph), request or ""):
        m = _AT_LEAST_TESTS_RE.search(src or "")
        if m:
            return int(m.group(1))
    return 1


def _tested_stem(ph, deps):
    """The module a tests phase targets, among `deps` ({stem: code}): the one
    its test file is named after (test_<stem>.py / <stem>_test.py), else the
    first one its text mentions as <stem>.py, else the first dependency."""
    if not deps:
        return ""
    m = re.match(r"^(?:test_([\w\-]+)|([\w\-]+)_test)\.py$", _py_target(ph) or "", re.I)
    if m:
        want = (m.group(1) or m.group(2)).lower()
        for stem in deps:
            if stem.lower() == want:
                return stem
    text = _phase_text(ph).lower()
    for stem in deps:
        if ("%s.py" % stem.lower()) in text:
            return stem
    return next(iter(deps))


def _code_problems(ph, text, deps=None, request=""):
    """Problems a parser can PROVE in a code phase's output, never a guess:
    Python that does not parse, a tests file with too few test functions,
    tests that import names the module they test does not define, or that
    never touch it at all. `deps` = {module stem: its code} for the Python
    files this phase was built on."""
    testy = _is_test_phase(ph)
    target = _py_target(ph) if testy else _code_file(ph)
    if (not target and not testy) or _is_docs_phase(ph):
        return []
    if testy and not target and not deps and not re.search(
            r"pytest|\.py\b|python|unittest", _phase_text(ph), re.I):
        return []                   # tests of something that is not Python code
    srcs = _py_sources(text)
    if not srcs:
        if target or testy:
            return ["no Python code found -- return the complete %s in one fenced "
                    "```python block" % (target or "test file")]
        return []
    problems = []
    for code in srcs:
        err = _syntax_error(code)
        if err:
            problems.append("%s is not valid Python (%s) -- return the complete, "
                            "runnable file" % (target or "the code", err))
            break
    if not testy:
        return problems[:4]
    joined = "\n".join(srcs)
    want = _test_count_wanted(ph, request)
    have = len(re.findall(r"^\s*(?:async\s+)?def\s+test_\w+", joined, re.M))
    if have < want:
        problems.append("only %d test function%s (def test_...); at least %d required"
                        % (have, "" if have == 1 else "s", want))
    deps = deps or {}

    def _uses(stem):
        return bool(re.search(r"^\s*(?:from\s+%s\s+import|import\s+%s\b)"
                              % (re.escape(stem), re.escape(stem)), joined, re.M)
                    or ("%s.py" % stem) in joined)
    # A tests file may target ONE of several dependencies (test_core.py
    # needing core.py and cli.py): only importing NONE of them is proof.
    if deps and not any(_uses(st) for st in deps):
        stem = _tested_stem(ph, deps)
        problems.append("the tests never import %s (the module under test, "
                        "%s.py) -- test the real code, do not re-implement or stub "
                        "it" % (stem, stem))
    for stem, code in deps.items():
        names = _top_names(code)
        if names is None or not _uses(stem):
            continue
        bad = []
        for m in re.finditer(r"^\s*from\s+%s\s+import\s+\(?([^)\n]+)" % re.escape(stem),
                             joined, re.M):
            for nm in re.split(r"\s*,\s*", m.group(1)):
                nm = nm.strip().split(" as ")[0].strip(" ()\\")
                if nm and nm != "*" and nm not in names and nm not in bad:
                    bad.append(nm)
        if re.search(r"^\s*import\s+%s\b" % re.escape(stem), joined, re.M):
            # Only CALLS through the module (`tally.count(`): a bare
            # "tally.txt" in a string is a file name, not an attribute.
            for nm in re.findall(r"(?<![\w.\"'/\\])%s\.([A-Za-z_]\w*)\s*\(" % re.escape(stem),
                                 joined):
                if nm not in names and nm not in bad:
                    bad.append(nm)
        if bad:
            real = sorted(n for n in (_top_names(code, imports=False) or names)
                          if not n.startswith("_"))
            problems.append("the tests use %s from %s, which %s.py does not define "
                            "(it defines: %s) -- test the real API"
                            % (", ".join(bad[:6]), stem, stem, ", ".join(real[:15]) or "nothing"))
    return problems[:4]


def _criterion_proven(crit, text):
    """True when string tests DECIDE acceptance criterion `crit` and it holds
    (every quoted/backticked literal it requires is present, word/test counts,
    a markdown table, valid JSON) and nothing in it is left unchecked. None =
    a string test cannot decide it (the manager judges); False = it fails."""
    low = (text or "").lower()
    rest = " %s " % crit.lower()
    decided = False
    lits = _QUOTED_RE.findall(crit) + _BACKTICK_RE.findall(crit)
    if lits and (_MUST_HAVE_RE.search(crit) or _DEFINES_RE.search(crit)):
        for lit in lits:
            key = lit.strip().lower()
            key = key[:-2] if key.endswith("()") else key
            if key and key not in low:
                return False
            rest = rest.replace(lit.lower(), " ")
        decided = True
    words = len(re.findall(r"\w+", text or ""))
    for rx, ok in ((_MIN_WORDS_RE, lambda n: words >= n),
                   (_MAX_WORDS_RE, lambda n: words <= int(n * 1.5))):
        m = rx.search(crit)
        if m:
            if not ok(int(m.group(1))):
                return False
            rest = rest.replace(m.group(0).lower(), " ")
            decided = True
    m = _AT_LEAST_TESTS_RE.search(crit)
    if m:
        if len(_TEST_FN_RE.findall(text or "")) < int(m.group(1)):
            return False
        rest = rest.replace(m.group(0).lower(), " ")
        decided = True
    if re.search(r"\bmarkdown\s+table\b", rest):
        if not _MD_TABLE_RE.search(text or ""):
            return False
        decided = True
    if re.search(r"\bvalid\s+json\b", rest):
        if not _looks_like_json(text):
            return False
        decided = True
    if not decided:
        return None
    left = [w for w in _part_keywords(rest)
            if w not in _CHECK_WORDS and _norm_word(w) not in _CHECK_WORDS]
    return True if not left else None


def _verdict_problems(v):
    """[] for a passing (or unreadable) verdict, else its problems."""
    if not isinstance(v, dict) or "ok" not in v:
        return []
    if v.get("ok") is True or str(v.get("ok")).lower() == "true":
        return []
    return _str_list(v.get("problems"), 4, 300) or \
        ["the manager rejected the output without detail — re-check every criterion"]


def _proven(ph, text):
    """Every acceptance criterion of `ph` decided and passed by string tests
    (see _criterion_proven) -- the manager's verdict would add nothing. False
    when there are no criteria: nothing to prove, the manager judges."""
    crit = ph.get("acceptance") or []
    return bool(crit) and all(_criterion_proven(c, text) is True for c in crit)


def _reorder(phases, order):
    """`phases` in `order` (a permutation of 1-based indexes), `needs`
    renumbered; a need that would now point forward is dropped (the scheduler
    only ever runs lower-numbered dependencies first)."""
    new_of = {old: new for new, old in enumerate(order, 1)}
    out = []
    for new, old in enumerate(order, 1):
        ph = dict(phases[old - 1])
        ph["needs"] = [new_of[n] for n in ph["needs"] if new_of.get(n, new) < new]
        out.append(ph)
    return out


def _requires(phases, a, b):
    """True when phase `a` (transitively) needs phase `b`."""
    seen, stack = set(), list(phases[a - 1]["needs"])
    while stack:
        n = stack.pop()
        if n == b:
            return True
        if n not in seen and 1 <= n <= len(phases):
            seen.add(n)
            stack.extend(phases[n - 1]["needs"])
    return False


def wire_code_deps(phases):
    """Tests and docs are built ON the code they describe. A tests phase (or
    a README/docs phase that documents flags, usage or an API) that the plan
    left independent of the phase producing that Python file gets it as a
    dependency -- moved after it when the planner listed it first. Returns
    (phases, notes); `notes` names each wiring made (for the event trail)."""
    try:
        code = {}
        for i, ph in enumerate(phases, 1):
            f = _code_file(ph)
            if f and f not in code:
                code[f] = i
        if not code:
            return phases, []
        notes = []
        k = 1
        while k <= len(phases):
            ph = phases[k - 1]
            testy, docs = _is_test_phase(ph), _is_docs_phase(ph)
            if not (testy or docs):
                k += 1
                continue
            text = _phase_text(ph).lower()
            want = [f for f in code if f in text or ("test_" + f) in text
                    or (f[:-3] + "_test.py") in text]
            if not want and len(code) == 1 and (testy or _DOCS_NEEDS_CODE_RE.search(text)):
                want = list(code)
            moved = False
            for f in want:
                j = code[f]
                if j == k or j in ph["needs"]:
                    continue
                if j < k:
                    ph["needs"] = sorted(set(ph["needs"]) | {j})
                    notes.append("%s builds on %s" % (_short(ph["title"], 30), f))
                    continue
                if _requires(phases, j, k):
                    continue            # the code needs this phase: leave it
                order = [n for n in range(1, len(phases) + 1) if n != k]
                order.insert(order.index(j) + 1, k)
                phases = _reorder(phases, order)
                code = {cf: (ci - 1 if k < ci <= j else ci) for cf, ci in code.items()}
                newk = code[f] + 1
                phases[newk - 1]["needs"] = sorted(set(phases[newk - 1]["needs"]) | {code[f]})
                notes.append("%s moved after %s" % (_short(ph["title"], 30), f))
                moved = True
                break
            if not moved:
                k += 1
        return phases, notes
    except Exception:                                           # noqa: BLE001
        return phases, []


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


def _best_phases(plan, text):
    """The parsed plan's phases, unless scanning the raw text recovers MORE
    of them (a salvaged, truncated or unbalanced reply parses to a stub)."""
    parsed = _clean_phases(plan)
    scanned = _phases_from_text(text) if len(parsed) < MAX_PHASES else []
    return scanned if len(scanned) > len(parsed) else parsed


def _usable(phases):
    """A plan is only worth running as a TEAM if it actually splits the work.
    Fewer than MIN_PHASES means the planner ignored the contract, and a partial
    plan silently drops the rest of the user's request — see MIN_PHASES."""
    return phases if len(phases) >= MIN_PHASES else []


# ---- ENUMERATED DELIVERABLES ------------------------------------------------
# MEASURED 2026-09-27 (manager sub-claude/sonnet, model "swarm"): "1) the full
# tally.py using argparse ... 2) a pytest file test_tally.py with at least 5
# tests ... 3) a README section with a description, a usage block, a markdown
# table of the 3 flags and one example ... Every part must be consistent."
# came back after 359 s as tally.py ALONE -- no tests, no README table -- with
# only "timed_out=1" in a header to say so. Nothing checked that the parts the
# user NUMBERED were all planned, all produced, and all still there after
# synthesis. They are now: the plan must cover each one (MAX_PHASES may be
# exceeded by MAX_PART_PHASES to do it), the draft is checked for each one
# before review, and the final text again after synthesis; a part still
# missing is named in a "Not finished" note, never dropped silently.
MAX_PARTS = 8
MAX_PART_PHASES = 3              # phases added for uncovered parts, beyond MAX_PHASES
PART_CHARS = 300
_NUM_ITEM_RE = re.compile(r"(?:^|(?<=[\s:;]))\(?(\d{1,2})[.)](?=\s)", re.M)
_BULLET_RE = re.compile(r"^\s*[-*•]\s+(\S.*)$")
_PARTS_HEAD_RE = re.compile(
    r"\b(?:deliver(?:ables?)?|include[sd]?|including|required(?:\s+parts)?|"
    r"requirements?|must\s+(?:have|include|contain)|the\s+following|parts?|"
    r"sections?|provide|produce|i\s+need|output)\s*:", re.I)
# A trailing sentence that talks about the parts as a whole ("Every part must
# be consistent.") is an instruction for all of them, not more of the last one.
_WHOLE_SENTENCE_RE = re.compile(
    r"(?<=[.!?])\s+(?=[A-Z][^.!?]*\b(?:every|each|all|both|consistent|together)\b)")
_PART_STOP = frozenset((
    "a an the and or of to for with in on at by from into as is are be this that "
    "these those it its your you we our their must should will can each every all "
    "any one two three four five six seven eight nine ten part parts file files "
    "using use make write create build produce provide give include including "
    "includes contain containing following least most exactly only also then than "
    "more less some such other which what when where how full complete short long "
    "new plus about there them they has have had not but so if do does per via "
    "section sections").split())
_FILE_TOKEN_RE = re.compile(r"^[\w\-]+\.[a-z]{1,5}$")
_CODE_EXT_RE = re.compile(r"\b[\w\-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|rb|sh|c|cc|cpp|h|"
                          r"cs|php|kt|swift|html|css|sql)\b", re.I)
_MD_TABLE_RE = re.compile(r"^\s*\|.*\|\s*$\n^\s*\|?\s*:?-{3,}", re.M)
_MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S", re.M)
_FENCE_RE = re.compile(r"```")
_CODE_LINE_RE = re.compile(r"^\s*(?:def |class |import |from \w|function |const |let |"
                           r"#include|package |fn |public |<\w)", re.M)
_TEST_FN_RE = re.compile(r"\bdef\s+test_\w+|\b(?:it|test)\(\s*[\"']")
_AT_LEAST_TESTS_RE = re.compile(r"\bat\s+least\s+(\d{1,2})\s+(?:\w+\s+)?tests?\b", re.I)


def _norm_word(w):
    w = w.strip(".-_")
    return w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w


def _part_keywords(text):
    """Distinctive lowercase tokens of `text` (file names kept whole)."""
    out = []
    for tok in re.findall(r"[a-z0-9_][a-z0-9_.\-]*", (text or "").lower()):
        tok = tok.strip(".-")
        if _FILE_TOKEN_RE.match(tok) or "_" in tok:
            out.append(tok)
            continue
        w = _norm_word(tok)
        if len(w) >= 3 and w not in _PART_STOP and not w.isdigit():
            out.append(w)
    return out


def _split_inline(s):
    """'a, b (x, y), and c' -> ['a', 'b (x, y)', 'c'] (commas inside brackets
    kept). One item left -> split on ' and '."""
    items, depth, cur = [], 0, []
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        if ch in ",;" and depth == 0:
            items.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    items.append("".join(cur))
    items = [re.sub(r"^\s*(?:and|or)\s+", "", i).strip() for i in items]
    items = [i for i in items if i]
    if len(items) == 1:
        items = [i.strip() for i in re.split(r"\s+and\s+", items[0]) if i.strip()]
    return items


def required_parts(request):
    """The deliverables the user ENUMERATED in `request`, or []: a numbered
    list ("1) ... 2) ..." / "1. ..." / "(1) ..."), a bullet list under a line
    ending in ":", or an inline list after "Deliver:" / "Include:" and the
    like. Conservative on purpose -- prose that merely mentions several things
    is not a checklist. At most MAX_PARTS, each clipped. Never raises."""
    try:
        text = str(request or "")
        # 1. numbered items, the longest run 1, 2, 3, ...
        marks = [(m.start(), m.end(), int(m.group(1))) for m in _NUM_ITEM_RE.finditer(text)]
        best = []
        for i, (s, e, n) in enumerate(marks):
            if n != 1:
                continue
            run, want = [(s, e)], 2
            for s2, e2, n2 in marks[i + 1:]:
                if n2 == want:
                    run.append((s2, e2))
                    want += 1
            if len(run) >= 2 and len(run) > len(best):
                best = run
        items = []
        if best:
            for k, (_s, e) in enumerate(best):
                end = best[k + 1][0] if k + 1 < len(best) else len(text)
                items.append(text[e:end])
            last = items[-1]
            para = re.search(r"\n\s*\n", last)
            if para:
                last = last[:para.start()]
            whole = _WHOLE_SENTENCE_RE.search(last)
            items[-1] = last[:whole.start()] if whole else last
        if not items:
            # 2. a bullet list under a heading line that ends with ":"
            lines = text.splitlines()
            for i, line in enumerate(lines):
                if not line.rstrip().endswith(":"):
                    continue
                run = []
                for nxt in lines[i + 1:]:
                    m = _BULLET_RE.match(nxt)
                    if not m:
                        if nxt.strip():
                            break
                        continue
                    run.append(m.group(1))
                if len(run) >= 2:
                    items = run
                    break
        if not items:
            # 3. inline, on the same line as "Deliver:" / "Include:" ...
            m = _PARTS_HEAD_RE.search(text)
            if m:
                rest = text[m.end():]
                stop = re.search(r"(?<=[^\s.]{2})\.(?:\s|$)|\n", rest)
                seg = rest[:stop.start()] if stop else rest
                cand = _split_inline(seg)
                if len(cand) >= 2:
                    items = cand
        out = []
        for it in items:
            it = re.sub(r"\s+", " ", it).strip(" \t;,")
            if len(it) >= 3 and re.search(r"[A-Za-z]", it):
                out.append(it[:PART_CHARS])
        return out[:MAX_PARTS]
    except Exception:                                           # noqa: BLE001
        return []


def _phase_text(ph):
    """Everything a plan says about one phase, for the coverage test."""
    return " ".join([str(ph.get("title") or ""), str(ph.get("task") or ""),
                     str(ph.get("done_when") or ""), str(ph.get("inputs") or ""),
                     str(ph.get("output_format") or "")]
                    + [str(x) for x in (ph.get("acceptance") or [])]
                    + [str(x) for x in (ph.get("constraints") or [])])


def _covers(part, text):
    """True when `text` (one phase's plan text) addresses `part`: at least 40%
    of the part's distinctive words (and at least one) appear in it."""
    keys = list(dict.fromkeys(_part_keywords(part)))
    if not keys:
        return True
    have = set(_part_keywords(text))
    low = (text or "").lower()
    hits = sum(1 for k in keys if k in have or (_FILE_TOKEN_RE.match(k) and k in low))
    return hits >= max(1, -(-len(keys) * 2 // 5))


def _uncovered(parts, phases):
    """Indexes (1-based) of the parts no single phase addresses."""
    return [k for k, p in enumerate(parts, 1)
            if not any(_covers(p, _phase_text(ph)) for ph in phases)]


def _part_present(part, text):
    """True / False / None (cannot tell) for whether `text` contains `part`,
    by the markers a string test can prove: a markdown table, a heading for a
    README, a fenced block for a usage/code block, N test functions, code for
    a code file, and quoted literals. Every marker is a NECESSARY condition,
    so False is only ever said on hard evidence; no marker -> None."""
    p = (part or "").lower()
    t = text or ""
    checks = []
    if re.search(r"\btables?\b", p):
        checks.append(bool(_MD_TABLE_RE.search(t)) or "<table" in t.lower())
    if "readme" in p:
        checks.append(bool(_MD_HEADING_RE.search(t)))
    if re.search(r"\b(?:usage|code)\s+block\b", p):
        checks.append(bool(_FENCE_RE.search(t)))
    if re.search(r"\b(?:pytest|unit\s*tests?|tests?\s+file|test\s+suite|\d+\s+tests)\b", p):
        n = len(_TEST_FN_RE.findall(t))
        m = _AT_LEAST_TESTS_RE.search(part or "")
        checks.append(n >= (int(m.group(1)) if m else 1))
    if "argparse" in p:
        checks.append("argparse" in t)
    if _CODE_EXT_RE.search(p) and not re.search(r"\b(?:readme|pytest|tests?)\b", p):
        checks.append(bool(_FENCE_RE.search(t) or _CODE_LINE_RE.search(t)))
    for lit in _QUOTED_RE.findall(part or ""):
        checks.append(lit.strip().lower() in t.lower())
    if not checks:
        return None
    return all(checks)


def _short(text, n):
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s if len(s) <= n else s[:n - 3].rstrip() + "..."


def _part_label(k, part):
    return "part %d (%s)" % (k, _short(part, 70))


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


def _gather(fn, items, timeout, need_one=False, leftovers=None):
    """Run fn(item) for every item concurrently; {item: (text, who)} for the
    ones that finished within `timeout` seconds (None = wait for all).

    `need_one`: past the timeout, keep waiting until at least one result
    carries text -- used while the run has nothing at all to deliver yet.

    NOT a `with` block: ThreadPoolExecutor.__exit__ joins every worker, which
    would make the wall-clock cap in run() wait for the very stragglers it
    exists to stop waiting for. Abandoned workers are bounded by the
    dispatcher's own per-hop deadline. One worker raising must not kill the
    others.

    `leftovers` (a dict): receives {item: future} for the calls still running
    when it stopped waiting, so a later stage can still take their answer."""
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
        if leftovers is not None:
            for fut, item in pending.items():
                if not fut.cancelled():
                    leftovers[item] = fut
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return out


def run(messages, dispatch, profile=None, on_event=None, max_seconds=None,
        manager=None, context=None, seconds_per_phase=0, max_seconds_ceiling=None,
        grace_seconds=0, fast_dispatch=None):
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
    failures -- or, when that output is too long for the manager's clipped
    view, fixed by a free model following the manager's instructions (the
    final revision always works that way: instructions, free apply on the
    FULL draft, short manager confirmation). The result gains
    "manager_tokens" (and "review_warning" when the reviewer's reply stayed
    unreadable). None = the pipeline exactly as before.

    `context` is what the caller knows about the conversation beyond
    `messages` (its rolling recap, an /agent session's memory block). It and
    the earlier turns in `messages` are folded into the brief by
    conversation_brief(), bounded; the manager sees an excerpt of it
    (MANAGER_CONTEXT_CHARS) in its plan and review prompts.

    THE BUDGET SCALES WITH THE PLAN. `max_seconds` is the BASE cap; once the
    plan is known it grows by `seconds_per_phase` per phase beyond MIN_PHASES
    and, with a manager, by its MEASURED call latency for every manager stage
    on the critical path (the plan, one verdict per wave, supervise, review) --
    a subscription CLI takes 30-150 s per call, and a flat cap spent on three
    of them left no time for the work. Never above `max_seconds_ceiling`.
    Past the cap, phases that produced nothing are finished IN PARALLEL by
    `fast_dispatch` (default: dispatch) within `grace_seconds`; what is still
    missing is named in a "Not finished" note and in result["unfinished"].

    ENUMERATED PARTS (required_parts): each must be covered by a phase (one
    re-ask of a free planner, then phases are added), present in the draft
    before review (mechanical markers, then a manager-or-free verdict; gaps
    are produced by a repair worker) and present after synthesis.

    Returns {"text", "plan", "phases", "review", "models", "planned"[,
    "unfinished", "cap_seconds"]} — `text` is always a non-empty answer unless
    every single call failed."""
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
    t_start = time.monotonic()
    # [stop_at, effective cap]: mutable, the cap is re-sized once the plan is in.
    clock = [(t_start + cap) if cap > 0 else None, cap]
    timed_out = [False]
    grace_until = [None]

    def _left():
        """Seconds left on the wall clock (None = unbounded, 0 = spent)."""
        if clock[0] is None:
            return None
        return max(0.0, clock[0] - time.monotonic())

    def _over(stage):
        """True once the cap is spent; says so ONCE in the event trail."""
        if clock[0] is None or time.monotonic() < clock[0]:
            return False
        if not timed_out[0]:
            timed_out[0] = True
            emit("budget", "wall-clock cap (%ds) reached before %s — "
                           "synthesising from what finished" % (int(clock[1]), stage))
        return True

    def _grace_secs():
        try:
            return max(0.0, float(grace_seconds or 0))
        except (TypeError, ValueError):
            return 0.0

    def _grace_left():
        """Seconds left of the ONE post-cap grace window (opened on first
        use, shared by phase finishing and part repairs); 0 = none."""
        g = _grace_secs()
        if g <= 0:
            return 0.0
        if grace_until[0] is None:
            grace_until[0] = time.monotonic() + g
        return max(0.0, grace_until[0] - time.monotonic())

    fast = fast_dispatch or dispatch

    brief, ctx_block = conversation_brief(messages, context)
    request = _last_user_text(messages)
    models_used = []

    # ---- the optional manager ---------------------------------------------
    # manager(msgs, max_tokens, purpose) -> (text, who[, tokens]). It PLANS,
    # SUPERVISES, REVIEWS, judges each worker's output and fixes what two free
    # attempts could not; the free models still do the work (phases, gap
    # repairs, synthesis). "" from it (off, over budget, failed) makes that one
    # stage fall back to `dispatch`, so a manager can only ever add quality.
    mgr_spent = [0]
    mgr_secs = []                    # measured duration of each manager call
    mgr_calls = []                   # the purpose of each manager call made
    mgr_lock = threading.Lock()
    staged_by_manager = [False]      # did the last _staged() call get the manager's answer
    extras = {}

    def _mgr(msgs, max_tokens, purpose):
        if manager is None:
            return "", None
        # Its OWN deadline, never past the wall clock: a subscription CLI that
        # is still thinking when the stage is due is abandoned (its thread and
        # CLI finish in the background, bounded by the hub's CLI timeout) and
        # the stage takes its free fallback -- the run never waits on it.
        limit = float(MANAGER_DEADLINES.get(purpose, 150))
        left = _left()
        if left is not None:
            if left < min(MANAGER_MIN_SECONDS, 0.1 * clock[1]):
                emit(purpose, "%ds left on the clock — no manager call" % int(left))
                return "", None
            limit = min(limit, left)
        box = {}

        def _call():
            try:
                out = manager(msgs, max_tokens, purpose)
            except Exception:                                   # noqa: BLE001
                out = None
            box["out"] = out
            # Accounted HERE, on the manager's own thread, so a call abandoned
            # at its deadline still counts once it finishes (it is billed on
            # the daily budget either way). A call that finishes after the run
            # returned is not in that run's figures. A call that never ran
            # (refused: budget, dead model) is neither a call nor tokens.
            if not isinstance(out, (tuple, list)) or len(out) < 2:
                return
            text = out[0] if isinstance(out[0], str) else ""
            tokens = out[2] if len(out) > 2 else None
            if tokens is None:
                # The caller did not report real usage: chars/4, like the hub.
                tokens = ((sum(len(str(m.get("content") or "")) for m in msgs)
                           + len(text)) // 4) if text else 0
            try:
                tokens = max(0, int(tokens))
            except (TypeError, ValueError):
                tokens = 0
            if tokens or text.strip():
                with mgr_lock:
                    mgr_spent[0] += tokens
                    mgr_calls.append(purpose)
        t0 = time.monotonic()
        th = threading.Thread(target=_call, daemon=True, name="swarm-manager")
        th.start()
        th.join(limit)
        with mgr_lock:
            mgr_secs.append(time.monotonic() - t0)
        if th.is_alive():
            emit(purpose, "manager gave no answer within %ds — free models" % int(limit))
            return "", None
        out = box.get("out")
        if not isinstance(out, (tuple, list)) or len(out) < 2:
            return "", None
        text = out[0] if isinstance(out[0], str) else ""
        return (text, out[1] or "manager") if text.strip() else ("", None)

    def _staged(free_msgs, free_tokens, purpose, mgr_msgs=None, mgr_tokens=None,
                exclude=None):
        """One manager-eligible stage: the manager on a clipped view first,
        else the free dispatch with EXACTLY the call it always made."""
        staged_by_manager[0] = False
        if manager is not None:
            text, who = _mgr(mgr_msgs or free_msgs, mgr_tokens or free_tokens, purpose)
            if text:
                staged_by_manager[0] = True
                return text, who
            emit(purpose, "manager unavailable — free models")
        if exclude is None:
            return dispatch(free_msgs, free_tokens)
        return dispatch(free_msgs, free_tokens, exclude_pids=exclude)

    def _finish(result):
        if manager is not None:
            with mgr_lock:
                result["manager_tokens"] = mgr_spent[0]
                result["manager_calls"] = len(mgr_calls)
            result.update(extras)
        return result

    def _spent():
        """Wall clock spent — checked from worker threads, so it never emits."""
        return clock[0] is not None and time.monotonic() >= clock[0]

    # The manager's view: the request (clipped as always) plus a SHORT excerpt
    # of the conversation context, so its plan and review know what "it" is.
    mgr_brief = _clip(brief, MANAGER_BRIEF_CHARS) if not ctx_block else (
        "%s\n\n---\n%s (excerpt)\n%s"
        % (_clip(request, MANAGER_BRIEF_CHARS), _CONTEXT_HEADING,
           _clip(ctx_block, MANAGER_CONTEXT_CHARS)))

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
    phases = _usable(_best_phases(plan, plan_text))
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
        # The retry goes to the FREE planner even with a manager: a second
        # subscription call is another 30-150 s on the critical path (MEASURED:
        # the tally run paid two plan calls), a free planner answers in a few
        # seconds, and the coverage check below still holds it to every part.
        plan_text, plan_model = dispatch(
            [{"role": "system", "content": plan_system},
             {"role": "user", "content": brief + strict}], PLAN_MAX_TOKENS)
        if plan_model:
            models_used.append(("plan:retry", plan_model))
        plan = _parse_json(plan_text) or {}
        phases = _usable(_best_phases(plan, plan_text))
    single = not phases
    if not phases:
        # Planner failed or returned junk -> ONE phase that is the original ask.
        # Degrading to a normal answer beats erroring out.
        phases = [{"title": "Deliver", "task": brief, "done_when": "", "needs": []}]
        emit("plan", "planner unusable — running as a single phase")
    else:
        emit("plan", "%d phases" % len(phases))

    # ---- 1b. COVERAGE — every part the user enumerated has a phase ----------
    # The single-phase fallback carries the WHOLE brief, so it covers all.
    parts = required_parts(request)
    if parts and not single:
        missing = _uncovered(parts, phases)
        if missing and manager is None and not _spent():
            # ONE re-ask of the (free, fast) planner, naming what it left out.
            # With a paid manager the phases are added directly below instead:
            # a second subscription call is 30-150 s the run cannot spare.
            emit("plan", "plan misses %d required part%s — asking once more"
                 % (len(missing), "" if len(missing) == 1 else "s"))
            ask = (brief + "\n\nREQUIRED PARTS (the request enumerates these; EVERY "
                   "one must be produced by at least one phase):\n"
                   + "\n".join("%d. %s" % (k, p) for k, p in enumerate(parts, 1))
                   + "\n\nYour previous plan left out: "
                   + "; ".join(_part_label(k, parts[k - 1]) for k in missing))
            t2, m2 = dispatch([{"role": "system", "content": plan_system},
                               {"role": "user", "content": ask}], PLAN_MAX_TOKENS)
            if m2:
                models_used.append(("plan:coverage", m2))
            p2 = _parse_json(t2) or {}
            ph2 = _usable(_best_phases(p2, t2))
            if ph2 and len(_uncovered(parts, ph2)) < len(missing):
                plan, phases = p2, ph2
                missing = _uncovered(parts, phases)
        if missing:
            # Still uncovered: a phase per part (bounded; the rest share one).
            # It builds on the phases that start the plan, so it sees the work
            # it must stay consistent with ("every part must be consistent").
            roots = [i for i, ph in enumerate(phases, 1) if not ph["needs"]][:2]
            room = max(1, MAX_PHASES + MAX_PART_PHASES - len(phases))
            groups = ([[k] for k in missing] if len(missing) <= room else
                      [[k] for k in missing[:room - 1]] + [missing[room - 1:]])
            for grp in groups:
                text = "\n".join("- " + parts[k - 1] for k in grp)
                phases.append({
                    "title": ("Part %d: %s" % (grp[0], _short(parts[grp[0] - 1], 48))
                              if len(grp) == 1 else "Remaining required parts"),
                    "task": "Produce %s of the user's request, complete and ready "
                            "to use:\n%s" % ("this required part" if len(grp) == 1
                                              else "these required parts", text),
                    "done_when": "", "needs": list(roots)})
            emit("plan", "added %d phase%s for required parts the plan missed"
                 % (len(groups), "" if len(groups) == 1 else "s"))

    # ---- 1b'. CODE DEPENDENCIES — tests and docs see the code they describe --
    if not single:
        phases, wired = wire_code_deps(phases)
        if wired:
            emit("plan", "wired: " + "; ".join(wired))

    # ---- 1c. BUDGET — sized to the plan, not a flat number --------------------
    if clock[0] is not None:
        try:
            extra = max(0.0, float(seconds_per_phase or 0)) * max(0, len(phases) - MIN_PHASES)
            if mgr_secs:
                # plan (spent from the base) + one verdict per wave + supervise
                # + review (+ the parts verdict): each is a manager call at its
                # measured latency.
                extra += max(mgr_secs) * (len(_waves(phases)) + (4 if parts else 3))
            eff = cap + extra
            if max_seconds_ceiling:
                eff = min(eff, max(cap, float(max_seconds_ceiling)))
            if eff > clock[1]:
                clock[0], clock[1] = t_start + eff, eff
                emit("budget", "wall clock %ds for %d phase%s%s"
                     % (int(eff), len(phases), "" if len(phases) == 1 else "s",
                        " (manager ~%ds/call)" % int(max(mgr_secs)) if mgr_secs else ""))
        except (TypeError, ValueError):
            pass

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

    def _worker_brief(task):
        """The user's own request (and its conversation context), bounded,
        for a worker. "" when its task already IS the whole brief (the
        single-phase fallback) -- no need to say it twice."""
        if not brief.strip() or (task or "").strip() == brief.strip():
            return ""
        return ("\n\nTHE USER'S REQUEST (for reference: honour every detail it "
                "gives that concerns your phase; do ONLY your phase)\n"
                + _clip(brief, WORKER_BRIEF_CHARS))

    def _run_phase(idx):
        """One worker's first attempt. With a manager, the checks run per
        WAVE afterwards (_verify_set), not here, so one verdict covers them all."""
        return dispatch(_phase_msgs(idx), PHASE_MAX_TOKENS)

    def _dep_code(idx):
        """{module stem: its Python source} for the code files phase `idx`
        builds on (its `needs` that produce a .py file and produced code)."""
        out = {}
        for n in phases[idx - 1]["needs"]:
            f = _code_file(phases[n - 1])
            if f and outputs.get(n):
                src = "\n".join(_py_sources(outputs[n]))
                if src.strip():
                    out[f[:-3]] = src
        return out

    def _code_brief(idx):
        """The exact interface of every code file this phase builds on, and --
        for a tests phase -- how to test it. "" without code dependencies, so
        every other worker prompt is byte-identical to before."""
        deps = _dep_code(idx)
        if not deps:
            return ""
        ph = phases[idx - 1]
        out = []
        for stem, src in deps.items():
            iface = _interface(src)
            if iface:
                out.append("\n\nINTERFACE OF %s.py (module `%s`) -- use exactly these "
                           "names, signatures and flags; never rename, re-implement "
                           "or stub them:\n%s" % (stem, stem, iface))
        if _is_test_phase(ph):
            stem = _tested_stem(ph, deps)
            out.append(
                "\n\nHOW TO WRITE THESE TESTS\n"
                "- Import the real module: `import %s` or `from %s import ...` (%s.py "
                "sits next to the test file). Never paste, re-implement or mock the "
                "module under test.\n"
                "- Call only names from the interface above. For the command line use "
                "main(argv) when main takes an argv parameter, else subprocess.run("
                "[sys.executable, str(Path(__file__).with_name(\"%s.py\")), ...], "
                "capture_output=True, text=True).\n"
                "- pytest style: tmp_path for files, capsys for printed output, plain "
                "assert statements.\n"
                "- At least %d test functions named test_*, each one passing against "
                "the code shown above.\n"
                "- Output ONLY the complete test file, in one ```python block."
                % (stem, stem, stem, stem, _test_count_wanted(ph, request)))
        return "".join(out)

    def _phase_msgs(idx):
        """The worker conversation for phase `idx` (its own fresh context)."""
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
        user += _code_brief(idx)
        user += _worker_brief(ph["task"])
        return [{"role": "system", "content": phase_system},
                {"role": "user", "content": user}]

    def _cheap_problems(idx, text):
        """Problems the free checks PROVE (they cost nothing): empty output,
        degenerate output (answer_check), quoted literals / word counts /
        JSON-HTML format, and -- for code -- syntax, test count and imports
        that do not exist in the module under test."""
        ph = phases[idx - 1]
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
        return (_mechanical_problems(ph, text)
                + _code_problems(ph, text, _dep_code(idx), request))[:6]

    def _acceptance(ph):
        crit = ph.get("acceptance") or ([ph["done_when"]] if ph.get("done_when") else [])
        return "\n- ".join(crit) if crit else "(none stated — judge against the task)"

    def _batch_verdict(idxs, texts, final, current=None):
        """ONE manager verdict for the phases `idxs` -> ({idx: problems},
        info, who). A phase a readable reply leaves out passes, and an
        unreadable reply passes them all: a checker must never be what loses
        a phase. `final` (the last wave of a team) also asks the supervisor's
        coverage question -- and which enumerated parts no marker can judge
        are absent -- in the SAME call; `info` then carries {"missing",
        "parts_missing"} (None when the reply was unreadable)."""
        combined = final and len(phases) > 1
        if len(idxs) == 1 and not combined:
            i = idxs[0]
            ph = phases[i - 1]
            v_text, who = _mgr(
                [{"role": "system", "content": _VERDICT_SYSTEM},
                 {"role": "user", "content":
                  "TASK: %s\n%s\n\nACCEPTANCE\n- %s%s\n\nOUTPUT (excerpt)\n%s"
                  % (ph["title"], _clip(ph["task"], 1200), _acceptance(ph),
                     ("\n\nOUTPUT FORMAT: " + ph["output_format"]) if ph.get("output_format") else "",
                     _clip(texts[i], VERDICT_OUTPUT_CHARS))}],
                MANAGER_VERDICT_TOKENS, "verify")
            v = _parse_json(v_text)
            if not isinstance(v, dict) or "ok" not in v:
                return None, None, who
            return {i: _verdict_problems(v)}, None, who
        per =max(1000, min(VERDICT_OUTPUT_CHARS, CHECK_OUTPUT_CHARS // max(1, len(idxs))))
        blocks = []
        for i in idxs:
            ph = phases[i - 1]
            dep_titles = [phases[n - 1]["title"] for n in ph["needs"]]
            blocks.append(
                "### PHASE %d: %s\nTASK: %s\nACCEPTANCE\n- %s%s%s\nOUTPUT (excerpt)\n%s"
                % (i, ph["title"], _clip(ph["task"], 700), _acceptance(ph),
                   ("\nOUTPUT FORMAT: " + ph["output_format"]) if ph.get("output_format") else "",
                   ("\nBUILDS ON: " + ", ".join(dep_titles)) if dep_titles else "",
                   _clip(texts[i], per)))
        system = _BATCH_VERDICT_SYSTEM
        user = "GOAL\n%s\n\n" % _clip(goal, 1500)
        unknown_parts = []
        if combined:
            plan_lines = "\n".join("%d. %s — %s" % (k, p["title"], _clip(p["task"], 300))
                                   for k, p in enumerate(phases, 1))
            user += "PLAN\n%s\n\n" % _clip(plan_lines, 3000)
            # The rest of the work as it stands NOW (`current` = this wave's
            # latest texts, retries included), not as it first came back.
            now = dict(outputs)
            now.update({k: v for k, v in (current or {}).items() if (v or "").strip()})
            earlier = [k for k in sorted(now) if k not in idxs]
            if earlier:
                each = max(400, CHECK_EARLIER_CHARS // len(earlier))
                user += ("OTHER WORK (already checked, or settled by the free checks -- "
                         "for coverage and consistency only)\n%s\n\n" % "\n\n".join(
                             "## %d. %s\n%s" % (k, phases[k - 1]["title"], _clip(now[k], each))
                             for k in earlier))
            system += _CHECK_COVERAGE_SYSTEM
            # A part without markers is undecidable whatever the text says, so
            # this is exactly the set the parts check would ask about later.
            unknown_parts = [k for k, p in enumerate(parts, 1) if _part_present(p, "") is None]
            if unknown_parts:
                user += "REQUIRED PARTS (the user enumerated these)\n%s\n\n" % "\n".join(
                    "%d. %s" % (k, parts[k - 1]) for k in unknown_parts)
                system += _CHECK_PARTS_SYSTEM
        user += "JUDGE THESE\n" + "\n\n".join(blocks)
        if combined:
            # It may be the LAST manager look at the work (the review is skipped
            # when it passes everything), so it sees the brief as the review
            # would: the request and a short excerpt of the conversation.
            user += "\n\nTHE USER'S BRIEF\n%s" % _clip(mgr_brief, 3500)
        v_text, who = _mgr([{"role": "system", "content": system},
                            {"role": "user", "content": user}],
                           MANAGER_CHECK_TOKENS, "verify")
        v = _parse_json(v_text)
        res = {i: [] for i in idxs}
        info = None
        if not isinstance(v, dict) or not (isinstance(v.get("phases"), list) or "ok" in v):
            res = None                    # unreadable: every phase passes, unjudged
        if isinstance(v, dict):
            entries = v.get("phases")
            if isinstance(entries, list):
                for e in entries:
                    if not isinstance(e, dict):
                        continue
                    try:
                        n = int(e.get("n", e.get("phase")))
                    except (TypeError, ValueError):
                        continue
                    if n in res:
                        res[n] = _verdict_problems(e)
            elif "ok" in v:
                # One verdict for the lot (a reply in the single-phase shape).
                p = _verdict_problems(v)
                res = {i: list(p) for i in idxs}
            if combined:
                miss, pm = v.get("missing"), v.get("parts_missing")
                info = {"missing": miss if isinstance(miss, list) else None,
                        "parts_missing": (pm if isinstance(pm, list) else None)
                        if unknown_parts else []}
        return res, info, who

    def _apply_problems(work, text, extra_check):
        """Free checks on an applied fix, before any verdict is paid for."""
        if not (text or "").strip():
            return ["the editor returned nothing"]
        if text.strip() == (work or "").strip():
            return ["the editor changed nothing — apply every instruction"]
        if len(text) < APPLY_MIN_KEEP * len(work or ""):
            return ["the editor dropped content (returned %d of %d characters) — "
                    "return the COMPLETE work with the fixes applied"
                    % (len(text), len(work))]
        if answer_check is not None:
            try:
                v = answer_check.inspect(text, prompt_text=brief)
            except Exception:                                   # noqa: BLE001
                v = {"ok": True}
            if not v.get("ok", True):
                return ["the output degenerated (%s) — return clean, complete "
                        "work only" % ", ".join(v.get("reasons") or ["junk"])]
        if extra_check is not None:
            try:
                return list(extra_check(text) or [])
            except Exception:                                   # noqa: BLE001
                return []
        return []

    def _directed_fix(label, apply_role, mgr_ctx, free_ctx, work, problems, floor,
                      exclude=(), extra_check=None, need_instructions=True,
                      instructions=None):
        """The manager DIRECTS a fix it cannot see in full: it writes short
        instructions from a clipped excerpt, a FREE model applies them to the
        FULL work, and the manager confirms from the diff. Up to two applies
        (the second on another provider, told why the first was rejected).
        Returns (text, who, trail); text "" = nothing accepted, the caller
        keeps what it had. Without instructions from the manager the reviewer's
        problems are applied as-is -- unless `need_instructions`."""
        trail = []
        if _spent():
            return "", None, trail
        if len(work or "") * APPLY_MIN_KEEP > _apply_tokens(work, floor) * APPLY_CHARS_PER_TOKEN:
            # No apply can return enough of this work to pass the keep check
            # (its output ceiling is too small), so do not pay the manager for
            # instructions -- or two free applies -- that can never be accepted.
            emit("fix", "%s: too long for one apply — kept as is" % label[:30])
            return "", None, trail
        probs = [str(p) for p in problems if str(p).strip()][:10]
        if instructions:
            # Already written (the free instructing call of a manager-less
            # revision): do not ask the manager again.
            instr, iwho = instructions, None
        else:
            instr, iwho = _mgr(
                [{"role": "system", "content": _INSTRUCT_SYSTEM},
                 {"role": "user", "content":
                  "%s\n\nPROBLEMS TO FIX\n- %s\n\nTHE WORK (excerpt of %d characters; "
                  "the editor has all of it)\n%s"
                  % (mgr_ctx, "\n- ".join(probs) or "(none stated)", len(work or ""),
                     _clip(work, FIX_EXCERPT_CHARS))}],
                MANAGER_INSTRUCT_TOKENS, "fix")
        if iwho:
            trail.append(("fix-plan:%s" % label, iwho))
        if not instr:
            if need_instructions or not probs:
                return "", None, trail
            emit("fix", "%s: manager unavailable — free models" % label[:30])
            instr = "\n".join("%d. Fix: %s" % (i, p) for i, p in enumerate(probs, 1))
        failed = set(exclude or ())
        rejected = []
        for _attempt in (1, 2):
            if _spent():
                break
            again = ("\n\nA PREVIOUS ATTEMPT WAS REJECTED. Also fix:\n- "
                     + "\n- ".join(rejected)) if rejected else ""
            text, who = dispatch(
                [{"role": "system", "content": _APPLY_SYSTEM},
                 {"role": "user", "content":
                  "%s\n\nTHE WORK\n%s\n\nFIX INSTRUCTIONS\n%s%s\n\nReturn the COMPLETE "
                  "work with every instruction applied — not a diff, not a list "
                  "of changes." % (free_ctx, work, instr, again)}],
                _apply_tokens(work, floor), exclude_pids=tuple(failed))
            if who:
                trail.append((apply_role, who))
                failed.add(who.split("/", 1)[0])
            rejected = _apply_problems(work, text, extra_check)
            if not rejected:
                v_text, v_who = _mgr(
                    [{"role": "system", "content": _CONFIRM_SYSTEM},
                     {"role": "user", "content":
                      "PROBLEMS THAT HAD TO BE FIXED\n- %s\n\nYOUR INSTRUCTIONS\n%s"
                      "\n\nWHAT CHANGED (diff)\n%s"
                      % ("\n- ".join(probs) or "(none stated)", _clip(instr, 2000),
                         _changes(work, text))}],
                    MANAGER_VERDICT_TOKENS, "verify")
                if v_who:
                    trail.append(("confirm:%s" % label, v_who))
                verdict = _parse_json(v_text)
                # Unreadable passes, like every other verdict here: a checker
                # must never be what throws away a fix that passed the free tests.
                if not isinstance(verdict, dict) or "ok" not in verdict or \
                        verdict.get("ok") is True or str(verdict.get("ok")).lower() == "true":
                    return text, who, trail
                rejected = _str_list(verdict.get("problems"), 4, 300) or \
                    ["the manager rejected the fix without detail — apply every instruction"]
            emit("fix", "%s: %s" % (label[:30], rejected[0][:44]))
        return "", None, trail

    def _retry_msgs(idx, problems, prev):
        """The worker conversation again, plus the problems to fix -- and the
        rejected attempt itself (clipped) when it is worth repairing rather
        than regenerating: a test file with one wrong import is fixed in
        place, not rewritten from nothing. Never a degenerate/empty one."""
        msgs = _phase_msgs(idx)
        extra = ("\n\nA PREVIOUS ATTEMPT AT THIS PHASE WAS REJECTED. Fix every one "
                 "of these problems:\n- " + "\n- ".join(problems))
        broken = any(str(p).startswith(("the output degenerated", "the output was empty"))
                     for p in problems)
        if (prev or "").strip() and not broken:
            extra += ("\n\nTHE REJECTED ATTEMPT (keep what is right, fix the problems "
                      "above, return the COMPLETE corrected output -- not a diff):\n"
                      + _clip(prev, DEP_CONTEXT_CHARS))
        return [msgs[0], {"role": "user", "content": msgs[1]["content"] + extra}]

    def _fix_one(idx, fallback, problems, failed):
        """After two rejected free attempts: the manager fixes the phase (it
        writes a SHORT one itself, DIRECTS the fix of a long one), and when
        it cannot -- unavailable, past its deadline, fix rejected -- ONE free
        repair pass with the problems and the rejected attempt, on a provider
        that has not failed it yet, kept only if the free checks pass.
        Returns (text, who, trail); "" = keep the last real attempt."""
        ph = phases[idx - 1]
        title = ph["title"]
        trail = []
        if _spent():
            return "", None, trail
        if len(fallback) > VERDICT_OUTPUT_CHARS:
            # LONG output: the manager would rewrite it from an excerpt and
            # drop the trimmed middle. It directs the fix instead; a free model
            # (not one of the two that failed) applies it to the full text.
            fix_text, fix_who, fix_trail = _directed_fix(
                title, "phase-fix:%s" % title,
                "OVERALL GOAL\n%s\n\nPHASE: %s\n%s%s"
                % (_clip(goal, 1500), title, _clip(ph["task"], MANAGER_BRIEF_CHARS),
                   ("\n\nDone when: " + _clip(ph["done_when"], MANAGER_PHASE_CHARS))
                   if ph.get("done_when") else ""),
                "OVERALL GOAL\n%s\n\nPHASE: %s\n%s%s%s"
                % (goal, title, ph["task"],
                   ("\n\nDone when: " + ph["done_when"]) if ph.get("done_when") else "",
                   _render_brief(ph) + _code_brief(idx)),
                fallback, problems, PHASE_MAX_TOKENS, exclude=tuple(failed),
                extra_check=lambda t: (_mechanical_problems(ph, t)
                                       + _code_problems(ph, t, _dep_code(idx), request)))
            trail.extend(fix_trail)
            if fix_text:
                emit("verify", "%s: fixed as the manager directed" % title[:30])
                return fix_text, fix_who, trail
        else:
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
                     _render_brief(ph) + _clip(_code_brief(idx), 2500),
                     "\n- ".join(problems),
                     _clip(fallback, VERDICT_OUTPUT_CHARS) or "(empty)")}],
                MANAGER_FIX_TOKENS, "fix")
            if fix_text:
                emit("verify", "%s: fixed by the manager" % title[:30])
                trail.append(("fix:%s" % title, fix_who))
                return fix_text, fix_who, trail
        if _spent() or not (fallback or "").strip():
            return "", None, trail
        text, who = dispatch(_retry_msgs(idx, problems, fallback), PHASE_MAX_TOKENS,
                             exclude_pids=tuple(failed))
        if who:
            trail.append(("phase-repair:%s" % title, who))
        if text and not _cheap_problems(idx, text):
            emit("verify", "%s: repaired by a free model" % title[:30])
            return text, who, trail
        return "", None, trail

    def _verify_set(idxs, first, final):
        """Check a WAVE's outputs together: free checks first, then ONE
        manager verdict for every output they could not settle (none for an
        output they PROVE meets its acceptance), free retries in parallel for
        the rejected ones, one more batched verdict, and a fix only for what
        still fails. MEASURED before: one verdict per phase, each 100-150 s.

        `first` = {idx: (text, who)}. Returns ({idx: (text, who, trail,
        changed, clean)}, batch_trail) -- `clean` = what ships passed its
        checks (first attempt or free retry; no fix, no fallback). On the last
        wave (`final`) the verdict also answers the supervisor's coverage
        question (check_info)."""
        st = {}
        for i in idxs:
            text, who = first[i]
            st[i] = {"text": text or "", "who": who, "trail": [], "failed": set(),
                     "fallback": text or "", "clean": True}
        batch_trail = []
        retried = set()
        failing = {}

        def _rejected(probs):
            """Note a rejection: its provider excluded next time, its text
            kept as the fallback when it has any."""
            for i, p in sorted(probs.items()):
                emit("verify", "%s: %s" % (phases[i - 1]["title"][:30], p[0][:44]))
                if st[i]["who"]:
                    st[i]["failed"].add(st[i]["who"].split("/", 1)[0])
                if st[i]["text"].strip():
                    st[i]["fallback"] = st[i]["text"]

        def _retry(probs):
            """The rejected ones retry IN PARALLEL, each on a provider that has
            not failed it, told exactly what was wrong (and shown its attempt)."""
            got = _gather(lambda i: dispatch(_retry_msgs(i, probs[i], st[i]["text"]),
                                             PHASE_MAX_TOKENS,
                                             exclude_pids=tuple(st[i]["failed"])),
                          sorted(probs), _left())
            for i in sorted(probs):
                text, who = (got.get(i) or ("", None))[:2]
                if who:
                    st[i]["trail"].append(("phase-retry:%s" % phases[i - 1]["title"], who))
                st[i]["text"], st[i]["who"] = text or "", who
                retried.add(i)

        def _judge(ids, combined):
            """ONE verdict for `ids` -> {idx: problems} of the rejected ones."""
            if not ids:
                return {}
            if _spent():
                for i in ids:                # the clock ran out before a verdict
                    st[i]["clean"] = False
                return {}
            res, info, who = _batch_verdict(
                ids, {i: st[i]["text"] for i in ids}, combined,
                {i: st[i]["text"] for i in idxs})
            if who:
                batch_trail.append(("verify:%s" % _short(" + ".join(
                    phases[i - 1]["title"] for i in ids), 80), who))
            if not who or res is None:
                # No usable verdict (manager unavailable, past its deadline,
                # reply unreadable): the phase passes, as it always did -- but
                # it was never judged, so it is not "clean" and the final
                # review still runs.
                for i in ids:
                    st[i]["clean"] = False
                res = res or {}
            if combined and info is not None:
                check_info.update(info)
            return {i: p for i, p in res.items() if p}

        def _unsettled(ids):
            """(cheap problems, the ids that still need a verdict)."""
            probs, judge = {}, []
            for i in ids:
                p = _cheap_problems(i, st[i]["text"])
                if p:
                    probs[i] = p
                elif not _proven(phases[i - 1], st[i]["text"]):
                    judge.append(i)
            return probs, judge

        # A. The free checks' rejections retry for free FIRST, so the one paid
        #    verdict below sees the repaired work, not the broken one.
        cheap, judge = _unsettled(idxs)
        if cheap:
            _rejected(cheap)
            if not _spent():
                _retry(cheap)
                cheap2, judge2 = _unsettled(sorted(cheap))
                judge = sorted(set(judge) | set(judge2))
                failing.update(cheap2)
                _rejected(cheap2)
            else:
                failing.update(cheap)
        # B. ONE verdict for everything the free checks could not settle (on
        #    the last wave it also answers the supervisor's coverage question).
        rejected = _judge(judge, final)
        if rejected:
            _rejected(rejected)
            again = {i: p for i, p in rejected.items() if i not in retried}
            failing.update({i: p for i, p in rejected.items() if i in retried})
            # C. First-time rejections get their free retry, then ONE more
            #    batched verdict; a second rejection goes to the fix below.
            if again and not _spent():
                _retry(again)
                cheap3, judge3 = _unsettled(sorted(again))
                failing.update(cheap3)
                _rejected(cheap3)
                late = _judge(judge3, False)
                failing.update(late)
                _rejected(late)
            elif again:
                failing.update(again)
        if failing and not _spent():
            fixed = _gather(lambda i: _fix_one(i, st[i]["fallback"], failing[i],
                                               st[i]["failed"]),
                            sorted(failing), _left())
            for i in sorted(failing):
                text, who, trail = (fixed.get(i) or ("", None, []))[:3]
                st[i]["trail"].extend(trail)
                if text:
                    st[i]["text"], st[i]["who"] = text, who
                    st[i]["fallback"] = text
        out = {}
        for i in idxs:
            s = st[i]
            if i in failing and s["text"] != s["fallback"]:
                # Nothing better: ship the last real attempt, never drop it.
                s["text"], s["who"] = s["fallback"], None
            text = s["text"] or s["fallback"]
            # "Clean" = what ships PASSED its checks (proven, or a readable
            # verdict said ok) -- on the first attempt or after its free retry.
            # A manager-written fix or a fallback was never checked: not clean.
            out[i] = (text, s["who"], s["trail"], text != (first[i][0] or ""),
                      s["clean"] and i not in failing)
        return out, batch_trail

    def _verify_safe(idxs, first, final):
        """_verify_set that can never take the run down: on any error the
        wave's first attempts stand, unjudged (so not "clean")."""
        try:
            return _verify_set(idxs, first, final)
        except Exception:                                       # noqa: BLE001
            emit("verify", "check failed — keeping the first attempts")
            return ({i: (first[i][0], first[i][1], [], False, False) for i in idxs}, [])

    def _apply_verified(vres, batch_trail):
        """Fold one wave's verification into the run; the phases whose
        output it changed (their dependents built on the old one)."""
        changed = set()
        for i in sorted(vres):
            text, _who, trail, ch, ok = vres[i]
            for role, w in trail:
                models_used.append((role, w))
                if role.startswith("phase") and w:
                    exec_pids.add(w.split("/", 1)[0])
            clean[i] = ok
            if text and ch:
                outputs[i] = text
                titles[i] = phases[i - 1]["title"]
                changed.add(i)
        models_used.extend(batch_trail)
        return changed

    running = {}        # phase -> its first attempt, still running when the wave stopped waiting
    check_info = {}     # the last wave's verdict: {"missing": [...], "parts_missing": [...]}
    clean = {}          # phase -> what ships passed its checks (no fix, no fallback)

    if manager is None:
        for wave in _waves(phases):
            if outputs and _over("the next wave"):
                break            # at least one phase is in hand -- stop adding more
            names = ", ".join(phases[i - 1]["title"] for i in wave)
            emit("phase", ("%d in parallel: %s" % (len(wave), names)) if len(wave) > 1
                 else "1/%d %s" % (len(phases), names))
            if len(wave) == 1 and clock[0] is None:
                results = {wave[0]: _run_phase(wave[0])}
            else:
                # One worker dying must not kill the wave (_gather maps it to an
                # empty result). Under a cap, a wave stops waiting once the clock is
                # spent -- but, while nothing at all is in hand, not before one
                # phase has answered: a slow planner must not leave nothing to
                # deliver.
                results = _gather(_run_phase, list(wave), _left(),
                                  need_one=not outputs, leftovers=running)
                if len(results) < len(wave):
                    _over("the rest of the wave")
            # Applied in phase order, not completion order, so the assembled draft
            # reads in the sequence the supervisor planned.
            for i in sorted(results):
                text, used = results[i][0], results[i][1]
                if used:
                    models_used.append(("phase:%s" % phases[i - 1]["title"], used))
                    exec_pids.add(used.split("/", 1)[0])
                if text:
                    outputs[i] = text
                    titles[i] = phases[i - 1]["title"]
    else:
        # WITH A MANAGER the waves are PIPELINED: wave k's verification (a
        # paid call, 30-150 s) runs WHILE wave k+1's free workers build on
        # wave k's output. When the check changes an output, the phases built
        # on the old one are rebuilt on the fixed one (free, parallel); in the
        # common case -- it passed -- nothing waited for it. The last wave is
        # checked in one call that also answers the supervisor's question.
        all_waves = _waves(phases)
        vpool = ThreadPoolExecutor(max_workers=1)
        pending_v = None
        try:
            for wi, wave in enumerate(all_waves):
                if outputs and _over("the next wave"):
                    break
                names = ", ".join(phases[i - 1]["title"] for i in wave)
                emit("phase", ("%d in parallel: %s" % (len(wave), names)) if len(wave) > 1
                     else "1/%d %s" % (len(phases), names))
                got = _gather(_run_phase, list(wave), _left(), need_one=not outputs,
                              leftovers=running)
                if len(got) < len(wave):
                    _over("the rest of the wave")
                rebuilt = set()
                if pending_v is not None:
                    changed = _apply_verified(*pending_v.result())
                    pending_v = None
                    redo = [i for i in sorted(got) if set(phases[i - 1]["needs"]) & changed]
                    if redo and not _spent():
                        emit("phase", "%d rebuilt on work the check changed: %s"
                             % (len(redo), ", ".join(phases[i - 1]["title"] for i in redo)))
                        again = _gather(_run_phase, redo, _left())
                        for i in sorted(again):
                            if again[i][0]:
                                if got[i][1]:           # the discarded first build
                                    models_used.append(("phase:%s" % phases[i - 1]["title"],
                                                        got[i][1]))
                                    exec_pids.add(got[i][1].split("/", 1)[0])
                                got[i] = again[i]
                                rebuilt.add(i)
                            elif again[i][1]:
                                models_used.append(("phase-rebuild:%s" % phases[i - 1]["title"],
                                                    again[i][1]))
                firsts = {}
                for i in sorted(got):
                    text, used = got[i][0], got[i][1]
                    if used:
                        models_used.append(("%s:%s" % ("phase-rebuild" if i in rebuilt
                                                       else "phase", phases[i - 1]["title"]),
                                            used))
                        exec_pids.add(used.split("/", 1)[0])
                    if text:
                        outputs[i] = text
                        titles[i] = phases[i - 1]["title"]
                        firsts[i] = (text, used)
                if not firsts:
                    continue
                last = wi == len(all_waves) - 1
                if last or _spent():
                    _apply_verified(*_verify_safe(sorted(firsts), firsts, last))
                else:
                    pending_v = vpool.submit(_verify_safe, sorted(firsts), firsts, False)
            if pending_v is not None:
                _apply_verified(*pending_v.result())
        finally:
            vpool.shutdown(wait=False)

    # ---- 2a. FINISH WHAT IS MISSING — never a silently partial deliverable ---
    # Phases the cap skipped (a later wave), cut off (still running when it
    # hit) or that failed: finished IN PARALLEL by the fastest capable free
    # models, inside one short grace window. Each gets its dependencies'
    # outputs when they exist, and one free sanity check.
    unfinished_idx = [i for i in range(1, len(phases) + 1) if i not in outputs]
    if unfinished_idx and outputs and (_spent() or _grace_secs() > 0):
        g_left = _grace_left() if _spent() else (_left() or _grace_secs())
        if g_left:
            emit("budget", "%d phase%s unfinished — finishing with fast models (%ds)"
                 % (len(unfinished_idx), "" if len(unfinished_idx) == 1 else "s",
                    int(g_left)))

            use = fast if _spent() else dispatch     # time left: full strength

            def _sane(r):
                text, who = (r if isinstance(r, tuple) and len(r) >= 2 else ("", None))[:2]
                if text and answer_check is not None:
                    try:
                        if not answer_check.inspect(text, prompt_text=brief).get("ok", True):
                            return "", who
                    except Exception:                           # noqa: BLE001
                        pass
                return text, who

            def _fast_phase(idx):
                orig = running.get(idx)
                if orig is None:
                    return _sane(use(_phase_msgs(idx), PHASE_MAX_TOKENS))
                # The phase's FIRST attempt is still running (the cap cut the
                # wave, not the call): it races the fast re-run and the first
                # sane answer wins -- a call that may be seconds from done is
                # not thrown away for one that starts from nothing.
                inner = ThreadPoolExecutor(max_workers=1)
                try:
                    fresh = inner.submit(lambda: _sane(use(_phase_msgs(idx), PHASE_MAX_TOKENS)))
                    waiting, best = {orig, fresh}, ("", None)
                    while waiting:
                        fin, waiting = wait(waiting, return_when=FIRST_COMPLETED)
                        for f in fin:
                            try:
                                r = _sane(f.result())
                            except Exception:                   # noqa: BLE001
                                continue
                            if r[0]:
                                return (r[0], r[1], "late") if f is orig else r
                            if r[1] and not best[1]:
                                best = r
                    return best
                finally:
                    inner.shutdown(wait=False)
            got = _gather(_fast_phase, unfinished_idx, g_left)
            for i in sorted(got):
                text, who = got[i][0], got[i][1]
                if who:
                    models_used.append(("%s:%s" % ("phase" if len(got[i]) > 2 else "phase-finish",
                                                   phases[i - 1]["title"]), who))
                if text:
                    outputs[i] = text
                    titles[i] = phases[i - 1]["title"]

    done = [{"title": titles[i], "output": outputs[i]} for i in sorted(outputs)]

    # ---- 2b. SUPERVISOR — did the team actually cover the plan? -------------
    # Workers that ran in parallel could not see each other, so this is where a
    # genuine gap or a contradiction between them gets caught. Skipped when only
    # one phase produced anything: there is no team to reconcile.
    gaps = []
    coverage_by_manager = False
    sup_text = ""
    if len(done) > 1 and isinstance(check_info.get("missing"), list):
        # The last wave's batched verdict already answered this question
        # (same excerpts, same plan): no second paid call for it.
        emit("supervise", "coverage checked with the last wave's verdict")
        coverage_by_manager = True
        sup_text = json.dumps({"missing": check_info["missing"]})
    elif len(done) > 1 and not _over("the supervisor"):
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
        coverage_by_manager = staged_by_manager[0]
    if sup_text:
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
                     {"role": "user", "content": "OVERALL GOAL\n%s\n\nYOUR TASK: %s\n%s%s"
                      % (goal, g["title"], g["task"], _worker_brief(g["task"]))}],
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

    def _assemble():
        return "\n\n".join("## %s\n%s" % (d["title"], d["output"]) for d in done) \
            if len(done) > 1 else done[0]["output"]

    draft = _assemble()

    # ---- 2c. PARTS CHECK — is every enumerated part in the draft? -----------
    # Mechanical markers first (free, and only ever "missing" on hard
    # evidence); the parts they cannot judge go to ONE short verdict -- the
    # manager's when there is one, else a free model's -- while there is time.
    # Every gap goes back to a repair worker that sees the draft, so the part
    # it writes matches the rest (same names, flags, files).
    gap_parts = []
    if parts:
        verdicts = [_part_present(p, draft) for p in parts]
        unknown = [k for k, v in enumerate(verdicts, 1) if v is None]
        said_by_check = check_info.get("parts_missing")
        if unknown and isinstance(said_by_check, list):
            # The last wave's verdict was already asked exactly these parts
            # (a part without markers stays undecidable whatever is added).
            for n in said_by_check:
                try:
                    n = int(n)
                except (TypeError, ValueError):
                    continue
                if n in unknown:
                    verdicts[n - 1] = False
        elif unknown and not _spent():
            listing = "\n".join("%d. %s" % (k, parts[k - 1]) for k in unknown)
            v_sys = ("You check a draft against REQUIRED PARTS the user enumerated. "
                     "You may see an excerpt; never report as missing what may sit in "
                     "the trimmed middle.\nReply with JSON ONLY: {\"missing\": "
                     "[<numbers of the parts that are genuinely absent>]}")
            v_text, v_who = _staged(
                [{"role": "system", "content": v_sys},
                 {"role": "user", "content": "REQUIRED PARTS\n%s\n\nDRAFT (%d characters)\n%s"
                  % (listing, len(draft), _clip(draft, FREE_INSTRUCT_CHARS))}],
                SUPERVISE_MAX_TOKENS, "verify",
                mgr_msgs=[{"role": "system", "content": v_sys},
                          {"role": "user", "content": "REQUIRED PARTS\n%s\n\nDRAFT (excerpt of "
                           "%d characters)\n%s" % (listing, len(draft),
                                                   _clip(draft, MANAGER_DRAFT_CHARS))}],
                mgr_tokens=MANAGER_VERDICT_TOKENS)
            if v_who:
                models_used.append(("verify:parts", v_who))
            said = (_parse_json(v_text) or {}).get("missing")
            for n in (said if isinstance(said, list) else []):
                try:
                    n = int(n)
                except (TypeError, ValueError):
                    continue
                if n in unknown:
                    verdicts[n - 1] = False
        gap_parts = [k for k, v in enumerate(verdicts, 1) if v is False]
        p_left = _grace_left() if _spent() else _left()
        if gap_parts and (p_left is None or p_left > 0):
            emit("verify", "%d required part%s missing from the draft — producing %s"
                 % (len(gap_parts), "" if len(gap_parts) == 1 else "s",
                    "it" if len(gap_parts) == 1 else "them"))
            use_p = fast if _spent() else dispatch

            def _part_repair(k):
                part = parts[k - 1]
                return use_p(
                    [{"role": "system", "content": phase_system},
                     {"role": "user", "content":
                      "OVERALL GOAL\n%s\n\nYOUR TASK: produce this REQUIRED PART of the "
                      "deliverable -- the work so far does not contain it:\n%s\n\nIt "
                      "must be consistent with the work below: same names, flags, "
                      "files and facts. Output ONLY this part.%s\n\nTHE WORK SO FAR "
                      "(excerpt)\n%s" % (goal, part, _worker_brief(part),
                                         _clip(draft, DEP_CONTEXT_CHARS))}],
                    PHASE_MAX_TOKENS)
            got = _gather(_part_repair, gap_parts, p_left)
            for k in sorted(got):
                text, who = got[k][0], got[k][1]
                if who:
                    models_used.append(("repair:part %d" % k, who))
                if text and _part_present(parts[k - 1], text) is not False:
                    done.append({"title": "Part %d: %s" % (k, _short(parts[k - 1], 48)),
                                 "output": text})
            draft = _assemble()

    def _deliver(text, review):
        """The result, after the LAST parts check: synthesis (or a revision)
        can drop a part the phases had -- it is restored from the phase that
        had it. Whatever is still missing -- an enumerated part, or a phase
        that never produced anything and maps to no part -- is NAMED in a
        "Not finished" note and in result["unfinished"]: a partial answer
        may ship, a silently partial one may not."""
        text = text or ""
        unfinished = []
        if parts and text.strip():
            for k, part in enumerate(parts, 1):
                if _part_present(part, text) is not False:
                    continue
                src = next((d for d in done
                            if _part_present(part, d["output"]) is True), None)
                if src is not None:
                    text = text.rstrip() + "\n\n## %s\n%s" % (src["title"], src["output"])
                    emit("verify", "part %d restored — the final pass dropped it" % k)
                    if _part_present(part, text) is not False:
                        continue
                unfinished.append(_part_label(k, part))
        for i, ph in enumerate(phases, 1):
            if i in outputs:
                continue
            if parts and any(_covers(p, _phase_text(ph)) for p in parts):
                continue        # judged through the part(s) it was planned for
            unfinished.append(ph["title"])
        out = {"text": text, "plan": plan, "phases": done, "review": review,
               "models": models_used, "timed_out": timed_out[0],
               "planned": [ph["title"] for ph in phases]}
        if clock[0] is not None:
            out["cap_seconds"] = round(clock[1], 1)
        if unfinished and text.strip():
            out["unfinished"] = unfinished
            out["text"] = (text.rstrip() + "\n\n---\n**Not finished:** "
                           + "; ".join(unfinished)
                           + " -- the run ended before %s produced (%s). Ask again "
                             "to complete %s."
                           % ("this was" if len(unfinished) == 1 else "these were",
                              "time cap reached" if timed_out[0] else "the models failed",
                              "it" if len(unfinished) == 1 else "them"))
            emit("budget", "not finished: %s" % "; ".join(unfinished)[:120])
        return _finish(out)

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

    def _synthesis(text_in, probs):
        synth_user = "BRIEF\n%s\n\nPHASE OUTPUTS\n%s" % (brief, text_in)
        if probs:
            synth_user += "\n\nREVIEWER PROBLEMS TO FIX\n- " + "\n- ".join(probs[:10])
        # Scaled to what it has to assemble: a fixed 6000-token ceiling cut a big
        # multi-phase build off mid-file (the hub lowers it per hop to a model's
        # learned output cap).
        synth_tokens = max(SYNTH_MAX_TOKENS, min(SYNTH_MAX_CAP, len(text_in) // 3 + 1000))
        return dispatch([{"role": "system", "content": synth_system},
                         {"role": "user", "content": synth_user}], synth_tokens)

    # With a manager, a SECOND look at the same excerpts is redundant when every
    # phase passed its checks on the first attempt and the manager itself
    # confirmed coverage (no gap, no missing part): the review would re-judge
    # what was just judged, for another 30-150 s subscription call on the
    # critical path. Anything that needed a retry, fix, gap or part repair
    # still gets the review.
    # A crew that asks for its own reviewer (a custom review prompt, or a
    # review -> revise loop) always gets it: that review is the point of it.
    wants_review = bool(profile.get("review_system")) or max_revisions >= 1
    all_clean = (manager is not None and not single and not wants_review
                 and coverage_by_manager
                 and not gaps and not gap_parts
                 and all(clean.get(i) is True for i in range(1, len(phases) + 1)))
    reviewed = not all_clean and not _over("the review")
    spec = None
    spec_pool = None
    if all_clean:
        emit("review", "skipped — the manager passed every phase and the coverage")
        review_text, review_model = '{"verdict": "ship", "problems": []}', None
    elif not reviewed:
        review_text, review_model = "", None
    else:
        if manager is not None and len(done) > 1:
            # Synthesis starts NOW, alongside the paid review: when the review
            # says ship (the common case) it is already done; on "revise" it is
            # discarded and synthesis runs again with the problems.
            spec_pool = ThreadPoolExecutor(max_workers=1)
            spec = spec_pool.submit(_synthesis, draft, [])
            spec_pool.shutdown(wait=False)
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
    def _free_revision(probs):
        """The manager-less revision: (text, who, trail); text "" = keep the
        draft. Never replaces the full draft with a clipped rewrite."""
        trail = []
        probs = [str(p) for p in probs if str(p).strip()][:10]
        instr = ""
        if not _spent():
            instr, iwho = dispatch(
                [{"role": "system", "content": _INSTRUCT_SYSTEM},
                 {"role": "user", "content":
                  "USER BRIEF\n%s\n\nPROBLEMS TO FIX\n- %s\n\nTHE WORK (%s%d "
                  "characters; the editor has all of it)\n%s"
                  % (_clip(brief, MANAGER_BRIEF_CHARS), "\n- ".join(probs),
                     "excerpt of " if len(draft) > FREE_INSTRUCT_CHARS else "",
                     len(draft), _clip(draft, FREE_INSTRUCT_CHARS))}],
                FREE_INSTRUCT_TOKENS)
            if iwho:
                trail.append(("fix-plan:revision", iwho))
        # No instructions came back: the reviewer's problems ARE the
        # instructions, exactly as the manager path does it.
        instr = (instr or "").strip() or "\n".join(
            "%d. Fix: %s" % (i, p) for i, p in enumerate(probs, 1))
        whole_fits = (len(draft) * APPLY_MIN_KEEP
                      <= _apply_tokens(draft, SYNTH_MAX_TOKENS) * APPLY_CHARS_PER_TOKEN)
        if whole_fits or len(done) < 2:
            text, who, t2 = _directed_fix(
                "revision", "revision", "", "USER BRIEF\n%s" % brief, draft, probs,
                SYNTH_MAX_TOKENS, need_instructions=False, instructions=instr)
            trail.extend(t2)
            return text, who, trail
        # Too long to come back whole from one apply: revise PER PHASE, each
        # part edited in full, the parts no instruction touches left alone.
        emit("revise", "draft too long for one pass — revising per phase")

        def _one(k):
            work = done[k]["output"]
            if _spent() or len(work) * APPLY_MIN_KEEP > \
                    _apply_tokens(work, PHASE_MAX_TOKENS) * APPLY_CHARS_PER_TOKEN:
                return "", None
            return dispatch(
                [{"role": "system", "content": _APPLY_SYSTEM},
                 {"role": "user", "content":
                  "USER BRIEF\n%s\n\nTHIS IS PART %d OF %d (%s). Apply ONLY the "
                  "instructions that concern this part; if none do, return it "
                  "unchanged.\n\nTHE WORK\n%s\n\nFIX INSTRUCTIONS\n%s\n\nReturn "
                  "the COMPLETE part with the relevant instructions applied — "
                  "not a diff, not a list of changes."
                  % (_clip(brief, MANAGER_BRIEF_CHARS), k + 1, len(done),
                     done[k]["title"], work, instr)}],
                _apply_tokens(work, PHASE_MAX_TOKENS))
        got = _gather(_one, list(range(len(done))), _left())
        parts, changed = [dict(d) for d in done], False
        for k in sorted(got):
            text, who = got[k][0], got[k][1]
            if who:
                trail.append(("revision:%s" % done[k]["title"], who))
            before = done[k]["output"]
            if not (text or "").strip() or text.strip() == before.strip():
                continue                  # untouched part, or a failed apply
            if _apply_problems(before, text, None):
                continue                  # lossy or junk: keep the original part
            parts[k]["output"] = text
            changed = True
        if not changed:
            return "", None, trail
        return ("\n\n".join("## %s\n%s" % (d["title"], d["output"]) for d in parts),
                "per-phase", trail)

    revised = False
    if needs_work and max_revisions >= 1 and not _over("the revision"):
        emit("revise", "fixing %d problem%s" % (len(problems), "" if len(problems) == 1 else "s"))
        if manager is None:
            # DIRECTED here too. The plain pipeline used to hand ONE free
            # model the draft clipped to DEP_CONTEXT_CHARS and let its rewrite
            # REPLACE the draft -- so on a long draft the "revised" answer
            # silently lost its trimmed middle. Now a free call writes fix
            # instructions from the problems, a free apply edits the FULL
            # draft (when it can come back whole), else each phase is revised
            # on its own; a rejected apply keeps the draft as it was.
            rev_text, rev_model, rev_trail = _free_revision(problems)
            models_used.extend(rev_trail)
        else:
            # With a manager the final fix is DIRECTED (see _directed_fix): the
            # manager used to rewrite a draft clipped to DEP_CONTEXT_CHARS, so a
            # long draft lost its middle in the "fixed" version. A rejected
            # apply leaves `revised` False, and synthesis still gets the
            # reviewer's problems -- nothing is worse than without a revision.
            rev_text, rev_model, rev_trail = _directed_fix(
                "revision", "revision", "BRIEF\n%s" % mgr_brief, "BRIEF\n%s" % brief,
                draft, problems, SYNTH_MAX_TOKENS, need_instructions=False)
            models_used.extend(rev_trail)
        if rev_text:
            draft = rev_text
            revised = True

    # ---- 4. SYNTHESIS -----------------------------------------------------
    # Single phase that the reviewer passed -> the draft IS the answer; another
    # rewrite would only risk making it worse.
    if len(done) == 1 and not needs_work:
        emit("done", "single phase, review passed")
        return _deliver(draft, review)

    emit("synthesis", "assembling")
    # A completed revision pass already fixed the problems; handing them to
    # synthesis again would ask it to fix problems that no longer exist.
    probs = problems if (problems and not revised) else []
    final_text, synth_model = "", None
    if spec is not None and not probs and not revised:
        try:
            final_text, synth_model = spec.result()      # started with the review
        except Exception:                                       # noqa: BLE001
            final_text, synth_model = "", None
    if not final_text:
        final_text, synth_model = _synthesis(draft, probs)
    if synth_model:
        models_used.append(("synthesis", synth_model))
    emit("done", "complete")
    return _deliver(final_text or draft, review)


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
    # plan= is the PLAN (every phase planned), not just the phases that
    # finished: listing only the finished ones made a 3-phase plan cut short
    # after phase 1 read as a 1-phase plan. done= says how many finished.
    planned = [str(t or "") for t in (result.get("planned") or [])]
    if planned:
        parts.append("plan=" + " | ".join(planned))
        parts.append("done=%d/%d" % (min(len(phases), len(planned)), len(planned)))
    elif phases:
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
    if result.get("cap_seconds"):
        parts.append("cap=%ds" % int(result["cap_seconds"]))
    unfinished = [str(u) for u in (result.get("unfinished") or [])]
    if unfinished:
        # Early in the line: a long models= list must not push it past the cap.
        parts.insert(0, "unfinished=%d: %s" % (len(unfinished), " | ".join(unfinished)))
    if result.get("manager_tokens"):
        parts.append("manager_tokens=%d" % int(result["manager_tokens"]))
    if result.get("manager_calls"):
        parts.append("manager_calls=%d" % int(result["manager_calls"]))
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
        planned = result.get("planned") or []
        finished = {p["title"] for p in phases}
        if planned:
            # Every planned phase, ticked only when it actually produced.
            for i, t in enumerate(planned, 1):
                lines.append("%d. [%s] %s" % (i, "x" if t in finished else " ", t))
        else:
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
