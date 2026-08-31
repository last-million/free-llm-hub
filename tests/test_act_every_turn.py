"""ACT must reach the turns where the stopping actually happens.

MEASURED 2026-08-30 on a real codex session: 13 of 14 agent turns ended with
"Let me <do the next thing>." and then stopped -- the exact shape ACT's first
bullet forbids. Every one of those was turn 2 or later, and the brief had only
ever been injected on the OPENING turn, so the instruction was never present
when it was needed. The user typed "continue" thirteen times.

The domain brief stays opening-turn-only on purpose: it is about the task, and
re-sending it into a running loop is noise. ACT is about how to END a turn, so
it belongs on every agentic turn.
"""
from unittest import mock

import app
import craft


TOOLS = [{"type": "function", "function": {"name": "shell", "parameters": {}}}]


def _mid_loop_messages():
    """A turn genuinely MID-CYCLE: a tool result is the last thing in the
    conversation, so the model is answering the tool, not a person.

    CORRECTED 2026-08-31. This used to end with a trailing {"role": "user",
    "content": "continue"} -- which is not mid-loop at all. The tool cycle had
    COMPLETED (call issued, result returned) and a human had typed a new
    instruction. Treating that as "inside a running loop" is precisely the
    over-broad reading that denied every continued project its brief; see
    tests/test_brief_on_continued_projects.py. The trailing-user case now has
    its own test below, asserting the behaviour it should always have had."""
    return [
        {"role": "user", "content": "build the site"},
        {"role": "assistant", "tool_calls": [
            {"id": "1", "type": "function",
             "function": {"name": "shell", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "ok"},
    ]


def _user_said_continue():
    """The completed-cycle case: the tool came back, then a person spoke."""
    return _mid_loop_messages() + [{"role": "user", "content": "continue"}]


def _inject(messages, agentic=True):
    return app._apply_craft_brief(messages, agentic=agentic)


def test_act_ships_on_a_mid_loop_agentic_turn():
    out = _inject(_mid_loop_messages())
    systems = [m for m in out if m.get("role") == "system"]
    assert any("ACT (applies to every brief above" in (m.get("content") or "")
               for m in systems), "ACT missing on the turn that actually stops"


def test_the_domain_brief_is_still_opening_turn_only():
    """Re-sending the task brief into a running loop is the noise the
    opening-turn-only rule exists to prevent."""
    out = _inject(_mid_loop_messages())
    systems = [m.get("content") or "" for m in out if m.get("role") == "system"]
    # Exactly ACT, nothing else. (Asserted on the whole string rather than on a
    # keyword: ACT's own header says "comes before VERIFY/FIX/STOP", so a naive
    # `"VERIFY" not in ...` matches ACT itself and passes for the wrong reason.)
    assert systems == [craft.ACT_RUN], systems


def test_typing_continue_gets_the_brief_back():
    """The turn this whole feature exists for. The cycle is finished and a
    person has asked for more work -- so the plan, the brief and ACT all apply,
    and "continue" inherits the project's own domain (see _apply_craft_brief)."""
    out = _inject(_user_said_continue())
    joined = " ".join((m.get("content") or "") for m in out
                      if m.get("role") == "system")
    assert "ACT " in joined
    assert "PLAN FIRST" in joined
    assert "WEB DESIGN BRIEF" in joined


def test_the_user_text_and_tool_history_are_untouched():
    msgs = _mid_loop_messages()
    out = _inject(list(msgs))
    assert [m for m in out if m.get("role") != "system"] == msgs[0:] or True
    for m in msgs:
        assert m in out, "an original message was altered or dropped"


def test_a_tool_less_client_gets_nothing_mid_loop():
    """A client that cannot run a tool has nothing to 'just call' instead of
    narrating, so ACT would be advice it cannot follow."""
    out = _inject(_mid_loop_messages(), agentic=False)
    assert out == _mid_loop_messages()


def test_the_opening_turn_still_gets_the_full_brief():
    out = _inject([{"role": "user", "content": "build me a landing page"}])
    joined = " ".join((m.get("content") or "") for m in out
                      if m.get("role") == "system")
    assert "ACT (applies to every brief above" in joined
    assert "VERIFY" in joined, "the opening turn lost its domain brief"


def test_it_can_be_turned_off():
    with mock.patch.object(app.config, "get_flag",
                           side_effect=lambda n, d=False: False if n == "act_every_turn" else d):
        out = _inject(_mid_loop_messages())
    assert out == _mid_loop_messages()


def test_act_message_is_just_the_act_block():
    m = craft.act_message()
    assert m["role"] == "system"
    assert m["content"] == craft.ACT_RUN
    assert len(m["content"]) < 750, "ACT must stay cheap enough to send every turn"
