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
    plan  ->  phases  ->  review  ->  synthesis

1. PLAN     the strongest available model writes a phased plan + todolist as
            JSON. This is also what satisfies "always make a plan first".
2. PHASES   each phase is executed in order, every one seeing the outputs of the
            phases before it. Phases are capped (MAX_PHASES) so a runaway plan
            cannot spend an unbounded number of calls.
3. REVIEW   a DIFFERENT provider than the one that executed criticises the draft.
            Different-provider is the point: a model reviewing its own output
            agrees with itself, so correlated blind spots survive.
4. SYNTH    the strongest model folds the review into a final answer.

Every stage degrades instead of failing: if the planner returns nothing usable
the work becomes a single phase, if review fails the draft is returned as-is. A
swarm request must never end with an error the plain model would have answered.

This module owns NO transport. `dispatch(messages, max_tokens, exclude_pids)`
is injected by app.py, which keeps chain/fallback/quota/activity behaviour
identical to every other request.
"""
import json
import re

MAX_PHASES = 5           # bounds worst-case cost: 1 plan + 5 phases + 1 review + 1 synth
PLAN_MAX_TOKENS = 1200
PHASE_MAX_TOKENS = 4000
REVIEW_MAX_TOKENS = 1500
SYNTH_MAX_TOKENS = 6000

_PLAN_SYSTEM = (
    "You are the planner for a team of AI models that will build what the user asked for.\n"
    "Reply with JSON ONLY — no prose, no markdown fence:\n"
    '{"goal": "<one sentence>", "phases": [{"title": "<short>", "task": "<what to '
    'produce, concretely>", "done_when": "<observable completion test>"}]}\n'
    "Rules:\n"
    "- Between 2 and %d phases. Fewer is better; do not invent work.\n"
    "- Each phase must produce a CONCRETE artefact (copy, code, a structure, a "
    "list) — never 'research', 'consider' or 'think about'.\n"
    "- Order them so each phase can use the previous phases' output.\n"
    "- Phases must be about the user's actual request. Do not add scope they did "
    "not ask for.\n"
    "- No filler phases such as 'gather requirements' or 'final review' — review "
    "happens outside your plan."
) % MAX_PHASES

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
    """Models wrap JSON in prose or a fence no matter how firmly you ask.
    Take the outermost {...} and parse that. Returns None on failure."""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"```\s*$", "", s).strip()
    i, j = s.find("{"), s.rfind("}")
    if i == -1 or j <= i:
        return None
    try:
        out = json.loads(s[i:j + 1])
    except ValueError:
        return None
    return out if isinstance(out, dict) else None


def _clean_phases(plan):
    """Validated phase list, or [] if the plan is unusable."""
    if not isinstance(plan, dict):
        return []
    out = []
    for p in (plan.get("phases") or [])[:MAX_PHASES]:
        if not isinstance(p, dict):
            continue
        task = str(p.get("task") or "").strip()
        if not task:
            continue
        out.append({
            "title": str(p.get("title") or "Phase %d" % (len(out) + 1)).strip()[:80],
            "task": task[:2000],
            "done_when": str(p.get("done_when") or "").strip()[:300],
        })
    return out


def run(messages, dispatch, on_event=None):
    """Run the pipeline. `dispatch(msgs, max_tokens, exclude_pids=()) ->
    (text, pid_model)`; it must never raise — an empty text means that call
    failed, and every stage below treats that as "carry on with what we have".

    Returns {"text", "plan", "phases", "review", "models"} — `text` is always a
    non-empty answer unless every single call failed."""
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
        [{"role": "system", "content": _PLAN_SYSTEM},
         {"role": "user", "content": brief}], PLAN_MAX_TOKENS)
    if plan_model:
        models_used.append(("plan", plan_model))
    plan = _parse_json(plan_text) or {}
    phases = _clean_phases(plan)
    if not phases:
        # Planner failed or returned junk -> ONE phase that is the original ask.
        # Degrading to a normal answer beats erroring out.
        phases = [{"title": "Deliver", "task": brief, "done_when": ""}]
        emit("plan", "planner unusable — running as a single phase")
    else:
        emit("plan", "%d phases" % len(phases))

    # ---- 2. PHASES --------------------------------------------------------
    done = []
    exec_pids = set()
    for idx, ph in enumerate(phases, 1):
        emit("phase", "%d/%d %s" % (idx, len(phases), ph["title"]))
        context = "".join(
            "\n\n### Completed phase: %s\n%s" % (d["title"], d["output"]) for d in done)
        user = ("OVERALL GOAL\n%s\n\nYOUR PHASE (%d of %d): %s\n%s%s%s"
                % (plan.get("goal") or brief, idx, len(phases), ph["title"], ph["task"],
                   ("\n\nDone when: " + ph["done_when"]) if ph["done_when"] else "",
                   ("\n\nWork already completed by the team — build on it, do not "
                    "repeat it:" + context) if context else ""))
        text, used = dispatch(
            [{"role": "system", "content": _PHASE_SYSTEM},
             {"role": "user", "content": user}], PHASE_MAX_TOKENS)
        if used:
            models_used.append(("phase:%s" % ph["title"], used))
            exec_pids.add(used.split("/", 1)[0])
        if text:
            done.append({"title": ph["title"], "output": text})

    if not done:
        return {"text": "", "plan": plan, "phases": [], "review": None,
                "models": models_used}

    draft = "\n\n".join("## %s\n%s" % (d["title"], d["output"]) for d in done) \
        if len(done) > 1 else done[0]["output"]

    # ---- 3. REVIEW (different provider on purpose) ------------------------
    emit("review", "reviewing")
    review_text, review_model = dispatch(
        [{"role": "system", "content": _REVIEW_SYSTEM},
         {"role": "user", "content": "BRIEF\n%s\n\nWORK\n%s" % (brief, draft)}],
        REVIEW_MAX_TOKENS, exclude_pids=tuple(exec_pids))
    if review_model:
        models_used.append(("review", review_model))
    review = _parse_json(review_text) or {}
    problems = [str(p).strip() for p in (review.get("problems") or []) if str(p).strip()]
    needs_work = (str(review.get("verdict") or "").lower() == "revise") and problems

    # ---- 4. SYNTHESIS -----------------------------------------------------
    # Single phase that the reviewer passed -> the draft IS the answer; another
    # rewrite would only risk making it worse.
    if len(done) == 1 and not needs_work:
        emit("done", "single phase, review passed")
        return {"text": draft, "plan": plan, "phases": done, "review": review,
                "models": models_used}

    emit("synthesis", "assembling")
    synth_user = "BRIEF\n%s\n\nPHASE OUTPUTS\n%s" % (brief, draft)
    if problems:
        synth_user += "\n\nREVIEWER PROBLEMS TO FIX\n- " + "\n- ".join(problems[:10])
    final_text, synth_model = dispatch(
        [{"role": "system", "content": _SYNTH_SYSTEM},
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
