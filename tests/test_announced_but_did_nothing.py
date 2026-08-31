"""Announcing the work is not doing the work.

REPORTED 2026-08-31, from the user's own /build conversation. Two consecutive
turns, both after an explicit "continue", both ending as "Finished":

    "I'll now craft a premium editorial UI system -- refined typography, rich
     color tokens, and magazine-grade layout -- then rebuild the full
     architecture with strong topical authority."

    "I am now hard-coding the pillar architecture in the manifest and
     redesigning the CSS to an editorial premium standard."

118 and 177 characters. No tool calls at all. One of them burned 2m 27s to say
it. Nothing was written either time, and the hub recorded both as clean
successes, so it learned nothing and picked the same models again.

WHY ACT DID NOT CATCH IT. ACT's first bullet opens "If you already called a
tool this turn and your own last sentence names the exact next call...". Both
of these called NOTHING. The rule covered acting-then-announcing and had a hole
exactly where the model announces having done nothing at all -- the worse of
the two failures, because at least the first one made progress.

THE STRUCTURAL FIX, which does not depend on a model reading the rule: on a
turn that OFFERED TOOLS, a short reply with no tool calls that states an
intention to work, rather than reporting work or asking a question, did not
take the turn. It is treated like any other non-answer the hub already knows
about (a relay error page returned as content, a tool call typed out as prose):
the chain moves to the next model and the id is sidelined.

The tense is the discriminator, and it has to be. "Done -- all 12 pages built"
is a perfectly good short final answer with no tool calls; "I'll now build the
12 pages" is the same length and is a dead turn.
"""
import app


def _resp(content, tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"index": 0, "message": msg, "finish_reason": "stop"}]}


# The two real ones, verbatim from the reported conversation.
REAL_1 = ("\n\nI'll now craft a premium editorial UI system—refined typography, "
          "rich color tokens, and magazine-grade layout—then rebuild the full "
          "architecture with strong topical authority.")
REAL_2 = ("I am now hard-coding the pillar architecture in the manifest and "
          "redesigning the CSS to an editorial premium standard.")


# --------------------------------------------------------------------------- #
# The detector
# --------------------------------------------------------------------------- #

def test_both_reported_turns_are_caught():
    assert app._looks_like_announced_not_acted(REAL_1) is True
    assert app._looks_like_announced_not_acted(REAL_2) is True


def test_the_common_phrasings_are_caught():
    for text in ("I'll now rebuild the CSS.",
                 "I will create the pillar pages next.",
                 "Let me update the manifest.",
                 "Now I'll write the remaining templates.",
                 "I'm going to refactor the layout system.",
                 "Next, I'll add the FAQ schema."):
        assert app._looks_like_announced_not_acted(text) is True, text


def test_a_finished_report_is_not_caught():
    """Same length, no tool calls, and a perfectly good answer. Tense is what
    separates them."""
    for text in ("Done -- all 12 pages are built and validated.",
                 "Finished. The portal is running on port 8080.",
                 "I rebuilt the CSS and updated every template.",
                 "All done."):
        assert app._looks_like_announced_not_acted(text) is False, text


def test_a_question_is_never_a_dead_turn():
    """Stopping to ask is exactly what the agent is SUPPOSED to do for a real
    open decision -- ACT's last bullet says so."""
    for text in ("I'll need the API key before I can deploy. Can you provide it?",
                 "Should I use Postgres or SQLite here?",
                 "Let me know which font you prefer and I'll apply it."):
        assert app._looks_like_announced_not_acted(text) is False, text


def test_a_long_substantive_answer_is_not_caught():
    """A real deliverable that happens to open with "I'll". Length alone is not
    the test, but a full answer is not an announcement either."""
    text = "I'll walk through the architecture.\n\n" + ("Section detail. " * 60)
    assert app._looks_like_announced_not_acted(text) is False


def test_empty_and_broken_input_is_safe():
    for text in ("", None, "   ", 12345):
        assert app._looks_like_announced_not_acted(text) is False


# --------------------------------------------------------------------------- #
# Wired into the non-answer machinery the hub already has
# --------------------------------------------------------------------------- #

def test_it_counts_as_a_non_answer_on_a_tool_turn():
    assert app._chat_json_nonanswer(_resp(REAL_2), has_tools=True) is True


def test_a_real_tool_call_alongside_it_is_fine():
    """Announcing the NEXT step while actually calling a tool is normal."""
    calls = [{"id": "c1", "type": "function",
              "function": {"name": "shell", "arguments": "{}"}}]
    assert app._chat_json_nonanswer(_resp(REAL_2, tool_calls=calls),
                                    has_tools=True) is False


def test_plain_chat_may_say_what_it_is_about_to_do():
    """With no tools there is nothing it could have called instead, and
    describing an approach is a legitimate answer."""
    assert app._chat_json_nonanswer(_resp(REAL_2)) is False
    assert app._chat_json_nonanswer(_resp(REAL_2), has_tools=False) is False


def test_the_swarm_rejects_a_member_that_only_announced():
    """The reported turn was swarm mode: three models ran, none called a tool,
    and the best-ranked ANNOUNCEMENT won the slot -- so the CLI executed
    nothing and the build reported Finished."""
    from unittest import mock

    class _R:
        status_code = 200
        headers = {}

        def json(self):
            return _resp(REAL_1)

        def close(self):
            pass

    with mock.patch.object(app, "_route_by_difficulty",
                           return_value=("groq", "m", "hard")), \
            mock.patch.object(app, "_build_chain", return_value=[("groq", "m")]), \
            mock.patch.object(app, "_dispatch_chat_with_deadline",
                              return_value=(_R(), None)), \
            app.app.test_request_context("/v1/chat/completions"):
        out = app._swarm_tool_result({"messages": [{"role": "user", "content": "continue"}],
                                      "tools": [{"type": "function"}]})
    assert out is None, "an announcement must not win a swarm slot"


# --------------------------------------------------------------------------- #
# ...and the instruction itself no longer has the hole
# --------------------------------------------------------------------------- #

def test_act_covers_a_turn_that_called_nothing():
    """The wording only ever addressed acting-then-announcing."""
    import craft
    low = craft.ACT_RUN.lower()
    assert "no tool" in low or "called nothing" in low or "without calling" in low


def test_act_stays_inside_its_size_cap():
    import craft
    assert len(craft.ACT_RUN) <= 750, len(craft.ACT_RUN)
