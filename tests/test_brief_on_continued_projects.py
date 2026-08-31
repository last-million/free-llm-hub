"""Continuing a project gets the plan and the brief too, not just turn one.

REPORTED 2026-08-31: "i want also that work if i continue eg projects u
udenrsant D?" -- straight after the phased todo list was made to ship in every
mode. It did ship, but only on the FIRST message of a session.

Two things caused that, both in _apply_craft_brief:

  1. `mid_loop` treated ANY conversation past the first user message as a
     running tool loop, so every later turn was refused the brief. That is far
     too broad. A tool loop is the model mid-cycle -- a tool call issued, a
     tool result pending. A user typing a NEW instruction after the previous
     turn finished is not that; it is a fresh task that deserves a plan exactly
     as much as the first one did.

  2. The brief was matched against `users[0]` -- the FIRST thing ever said in
     the conversation. So even where a brief did ship, a session that opened
     with "hi" and later asked for a landing page matched on "hi".

What this means in practice: open a project, build the homepage, then come back
tomorrow, hit Continue and say "now add the booking page" -- and the agent got
no phased plan, no web-design brief, no anti-slop list, no component guidance.
Exactly the case the user is describing.

The opening-turn-only rule is KEPT where it was actually earned: a turn whose
last message is a tool result, or that carries pending tool_calls, still gets
nothing but ACT. Injecting into a live tool cycle is the failure mode the rule
exists for, and none of this touches it.
"""
import app


TOOLS = [{"type": "function", "function": {"name": "shell"}}]


def _sys(messages, agentic=True):
    out = app._apply_craft_brief(messages, agentic=agentic)
    return "\n".join(m.get("content") or "" for m in out
                     if isinstance(m, dict) and m.get("role") == "system")


def _u(text):
    return {"role": "user", "content": text}


def _a(text):
    return {"role": "assistant", "content": text}


# --------------------------------------------------------------------------- #
# The reported case
# --------------------------------------------------------------------------- #

def test_a_follow_up_instruction_gets_the_plan():
    """"now add the booking page" is a new task, not a continuation of a tool
    cycle."""
    body = _sys([_u("build me a restaurant website"),
                 _a("Done -- homepage is up."),
                 _u("now add the booking page")])
    assert "PLAN FIRST" in body


def test_a_follow_up_gets_the_domain_brief_too():
    body = _sys([_u("build me a restaurant website"),
                 _a("Done."),
                 _u("now add the booking page")])
    assert "WEB DESIGN BRIEF" in body
    assert "ANTI" in body


def test_the_brief_matches_what_was_JUST_asked():
    """It matched users[0] -- the first thing ever said. A session that opened
    with "hi" and later asked for a website matched on "hi"."""
    body = _sys([_u("hi"), _a("Hello!"), _u("build me a landing page")])
    assert "WEB DESIGN BRIEF" in body


def test_a_follow_up_inherits_the_projects_domain():
    """A short follow-up names no domain of its own, but the project has not
    changed underneath it -- the design rules that applied to the homepage
    apply to the booking page.

    This assertion was written the other way round first (assert the brief does
    NOT ship, to save tokens on a small step) and then reversed on purpose. The
    two costs are not symmetric: paying a brief on a typo turn wastes some
    tokens, while missing it on a real step produces exactly the slop this work
    exists to stop. Every request in this session has been about quality, never
    about token cost."""
    body = _sys([_u("build me a restaurant website"),
                 _a("Done."),
                 _u("fix the footer spacing")])
    assert "WEB DESIGN BRIEF" in body
    assert "ACT " in body


def test_a_project_with_no_domain_still_pays_nothing():
    """The fallback only carries a domain the project actually had."""
    body = _sys([_u("help me understand this codebase"),
                 _a("Sure."),
                 _u("what does line 40 do")])
    assert "WEB DESIGN BRIEF" not in body


def test_resuming_a_long_conversation_still_plans():
    """What Continue actually sends: a whole history, complete tool cycles and
    all, ending in the new instruction."""
    history = [_u("build the site"),
               {"role": "assistant", "content": None,
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "shell", "arguments": "{}"}}]},
               {"role": "tool", "tool_call_id": "c1", "content": "ok"},
               _a("Homepage done."),
               _u("now build the menu page and the booking form")]
    body = _sys(history)
    assert "PLAN FIRST" in body
    assert "WEB DESIGN BRIEF" in body


# --------------------------------------------------------------------------- #
# The rule that is KEPT: never inject into a live tool cycle
# --------------------------------------------------------------------------- #

def test_a_pending_tool_result_still_gets_only_act():
    """The model is mid-cycle. This is the failure the opening-turn-only rule
    was written for, and it is untouched."""
    body = _sys([_u("build me a restaurant website"),
                 {"role": "assistant", "content": None,
                  "tool_calls": [{"id": "c1", "type": "function",
                                  "function": {"name": "shell", "arguments": "{}"}}]},
                 {"role": "tool", "tool_call_id": "c1", "content": "ok"}])
    assert "PLAN FIRST" not in body
    assert "WEB DESIGN BRIEF" not in body
    assert "ACT " in body


def test_a_turn_ending_in_pending_tool_calls_gets_only_act():
    body = _sys([_u("build me a restaurant website"),
                 {"role": "assistant", "content": None,
                  "tool_calls": [{"id": "c1", "type": "function",
                                  "function": {"name": "shell", "arguments": "{}"}}]}])
    assert "PLAN FIRST" not in body
    assert "ACT " in body


def test_an_assistant_message_last_is_not_a_new_instruction():
    """Nothing was asked; there is no instruction to brief against."""
    body = _sys([_u("build the site"), _a("Working on it.")])
    assert "WEB DESIGN BRIEF" not in body


# --------------------------------------------------------------------------- #
# Nothing about turn one changes
# --------------------------------------------------------------------------- #

def test_the_opening_turn_is_exactly_as_before():
    body = _sys([_u("build me a restaurant website")])
    assert "PLAN FIRST" in body
    assert "WEB DESIGN BRIEF" in body


def test_a_tool_less_caller_is_still_never_told_to_run_anything():
    body = _sys([_u("hi"), _a("hello"), _u("build me a landing page")], agentic=False)
    assert "PLAN FIRST" not in body


def test_multimodal_content_still_reads_its_text():
    body = _sys([_u("hi"), _a("hello"),
                 {"role": "user", "content": [
                     {"type": "text", "text": "build me a landing page"},
                     {"type": "image_url", "image_url": {"url": "data:,"}}]}])
    assert "WEB DESIGN BRIEF" in body
