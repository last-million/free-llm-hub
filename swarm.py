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
from concurrent.futures import ThreadPoolExecutor, as_completed

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
    '"needs": [<numbers of the phases this one needs the OUTPUT of>]}]}\n'
    "Rules:\n"
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
        out.append({
            "title": str(p.get("title") or "Phase %d" % idx).strip()[:80],
            "task": task[:2000],
            "done_when": str(p.get("done_when") or "").strip()[:300],
            "needs": needs,
        })
    return out


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


def run(messages, dispatch, profile=None, on_event=None):
    """Run the pipeline. `dispatch(msgs, max_tokens, exclude_pids=()) ->
    (text, pid_model)`; it must never raise — an empty text means that call
    failed, and every stage below treats that as "carry on with what we have".

    `profile` (crews.py builds these) overrides the stage system prompts
    ("plan_system"/"phase_system"/"review_system"/"synth_system"), appends
    extra text to the worker system prompt ("worker_extra" — the design crew's
    craft brief), and sets "max_revisions" (0 = a "revise" verdict is only
    folded into synthesis; >=1 = run ONE bounded revision pass first). None
    reproduces the generic pipeline exactly.

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

    brief = _last_user_text(messages)
    models_used = []

    # ---- 1. PLAN ----------------------------------------------------------
    emit("plan", "planning")
    plan_text, plan_model = dispatch(
        [{"role": "system", "content": plan_system},
         {"role": "user", "content": brief}], PLAN_MAX_TOKENS)
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
        plan_text, plan_model = dispatch(
            [{"role": "system", "content": plan_system},
             {"role": "user", "content":
              brief + "\n\nOUTPUT JSON ONLY. Start your reply with { and end it "
              'with }. No prose, no fence, no explanation. Shape:\n'
              '{"goal":"...","phases":[{"title":"...","task":"...",'
              '"done_when":"...","needs":[]}]}'}], PLAN_MAX_TOKENS)
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
        return dispatch(
            [{"role": "system", "content": phase_system},
             {"role": "user", "content": user}], PHASE_MAX_TOKENS)

    for wave in _waves(phases):
        names = ", ".join(phases[i - 1]["title"] for i in wave)
        emit("phase", ("%d in parallel: %s" % (len(wave), names)) if len(wave) > 1
             else "1/%d %s" % (len(phases), names))
        if len(wave) == 1:
            results = {wave[0]: _run_phase(wave[0])}
        else:
            with ThreadPoolExecutor(max_workers=len(wave)) as pool:
                futures = {pool.submit(_run_phase, i): i for i in wave}
                results = {}
                for fut in as_completed(futures):
                    i = futures[fut]
                    try:
                        results[i] = fut.result()
                    except Exception:                            # noqa: BLE001
                        results[i] = ("", None)                  # one worker dying
                                                                 # must not kill the wave
        # Applied in phase order, not completion order, so the assembled draft
        # reads in the sequence the supervisor planned.
        for i in sorted(results):
            text, used = results[i]
            if used:
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
    if len(done) > 1:
        emit("supervise", "checking coverage")
        sup_text, sup_model = dispatch(
            [{"role": "system", "content": _SUPERVISE_SYSTEM},
             {"role": "user", "content": "GOAL\n%s\n\nPLAN\n%s\n\nWHAT THE TEAM PRODUCED\n%s"
              % (goal,
                 "\n".join("%d. %s — %s" % (i, p["title"], p["task"])
                           for i, p in enumerate(phases, 1)),
                 "\n\n".join("## %s\n%s" % (d["title"], _clip(d["output"], DEP_CONTEXT_CHARS))
                             for d in done))}],
            SUPERVISE_MAX_TOKENS)
        if sup_model:
            models_used.append(("supervisor", sup_model))
        gaps = []
        for g in ((_parse_json(sup_text) or {}).get("missing") or [])[:MAX_REPAIRS]:
            if isinstance(g, dict) and str(g.get("task") or "").strip():
                gaps.append({"title": str(g.get("title") or "Gap").strip()[:80],
                             "task": str(g["task"]).strip()[:1200]})
        if gaps:
            emit("supervise", "%d gap%s to fill" % (len(gaps), "" if len(gaps) == 1 else "s"))
            with ThreadPoolExecutor(max_workers=len(gaps)) as pool:
                futures = {pool.submit(
                    dispatch,
                    [{"role": "system", "content": phase_system},
                     {"role": "user", "content": "OVERALL GOAL\n%s\n\nYOUR TASK: %s\n%s"
                      % (goal, g["title"], g["task"])}],
                    PHASE_MAX_TOKENS): g for g in gaps}
                for fut in as_completed(futures):
                    g = futures[fut]
                    try:
                        text, used = fut.result()
                    except Exception:                            # noqa: BLE001
                        continue
                    if used:
                        models_used.append(("repair:%s" % g["title"], used))
                    if text:
                        done.append({"title": g["title"], "output": text})

    if not done:
        return {"text": "", "plan": plan, "phases": [], "review": None,
                "models": models_used}

    draft = "\n\n".join("## %s\n%s" % (d["title"], d["output"]) for d in done) \
        if len(done) > 1 else done[0]["output"]

    # ---- 3. REVIEW (different provider on purpose) ------------------------
    emit("review", "reviewing")
    review_text, review_model = dispatch(
        [{"role": "system", "content": review_system},
         {"role": "user", "content": "BRIEF\n%s\n\nWORK\n%s" % (brief, draft)}],
        REVIEW_MAX_TOKENS, exclude_pids=tuple(exec_pids))
    if review_model:
        models_used.append(("review", review_model))
    review = _parse_json(review_text) or {}
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
    if needs_work and max_revisions >= 1:
        emit("revise", "fixing %d problem%s" % (len(problems), "" if len(problems) == 1 else "s"))
        rev_text, rev_model = dispatch(
            [{"role": "system", "content": phase_system},
             {"role": "user", "content":
              "BRIEF\n%s\n\nDRAFT\n%s\n\nREVIEWER PROBLEMS TO FIX\n- %s\n\n"
              "Return the COMPLETE corrected work — the full draft with every "
              "problem fixed, not a diff, not a list of changes."
              % (brief, _clip(draft, DEP_CONTEXT_CHARS),
                 "\n- ".join(problems[:10]))}],
            SYNTH_MAX_TOKENS)
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
        return {"text": draft, "plan": plan, "phases": done, "review": review,
                "models": models_used}

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
    return {"text": final_text or draft, "plan": plan, "phases": done,
            "review": review, "models": models_used}


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
    return text + "\n".join(lines)
