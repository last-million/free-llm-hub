"""A tool call typed out as prose is not an answer -- it is a dead turn.

REPORTED 2026-08-30: "i still in my project i see he stop and he dont finish
the work". Read straight out of that conversation's own stored transcript, the
last thing the agent "said" was:

    I'll now check all the generated pages to ensure they work correctly...
    ```shell
    shell_command
    <arg_key>command</arg_key>
    <arg_value>Get-Content robots.txt -Raw</arg_value>
    </tool_call>

That is a tool call TYPED OUT as text. The response carried no tool_calls
array at all, so codex saw a plain prose message, had nothing to execute, and
ended the turn. Nothing ran, no file was touched, and the work stopped -- and
because it looked like a perfectly ordinary answer, the hub recorded it as a
success and learned nothing. Two turns in a row ended exactly this way.

The hub already has the right machinery for "a 200 that is not really an
answer": _chat_json_nonanswer marks it, the chain falls through to the next
hop, and _note_nonanswer sidelines the id so it stops winning. It only knew
about one shape -- a relay's error page returned as content. This teaches it
the other shape.

Deliberately NOT another entry in _TOOL_DIALECT_MISMATCH. That list is a
per-model blacklist, and its own comment already says a blacklist is
whack-a-mole ("three runs found three DIFFERENT model-specific ways to fail").
This is detected from the RESPONSE, so it works for every model, including ones
nobody has met yet.

Gated on the request actually offering tools, so ordinary chat -- where talking
ABOUT tool-call syntax is perfectly legitimate -- is untouched.
"""
import app


def _resp(content, tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"index": 0, "message": msg, "finish_reason": "stop"}]}


# The exact text from the reported conversation.
REAL = """
I'll now check all the generated pages to ensure they work correctly and fix any issues.
```shell
shell_command
<arg_key>command</arg_key>
<arg_value>Get-Content robots.txt -Raw</arg_value>
</tool_call>
"""


def test_the_reported_dead_turn_is_caught():
    assert app._chat_json_nonanswer(_resp(REAL), has_tools=True) is True


def test_a_closing_tool_call_tag_alone_is_enough():
    assert app._chat_json_nonanswer(
        _resp("Let me look.\n</tool_call>"), has_tools=True) is True


def test_the_arg_key_arg_value_pair_is_enough():
    """Some emit the body without ever closing the tag."""
    assert app._chat_json_nonanswer(
        _resp("<arg_key>path</arg_key><arg_value>x.txt</arg_value>"),
        has_tools=True) is True


def test_a_real_tool_call_is_always_an_answer():
    """Even if the prose alongside it happens to mention the syntax."""
    calls = [{"id": "c1", "type": "function",
              "function": {"name": "shell", "arguments": '{"cmd":"ls"}'}}]
    assert app._chat_json_nonanswer(_resp(REAL, tool_calls=calls),
                                    has_tools=True) is False


def test_an_ordinary_answer_is_untouched():
    assert app._chat_json_nonanswer(
        _resp("Done -- created robots.txt and sitemap.xml."), has_tools=True) is False


def test_plain_chat_may_talk_about_tool_calls():
    """No tools offered means no agent loop to break, and explaining the
    syntax is a legitimate answer. This is why the flag exists."""
    assert app._chat_json_nonanswer(_resp(REAL)) is False
    assert app._chat_json_nonanswer(_resp(REAL), has_tools=False) is False


def test_the_old_shape_still_works():
    """The relay-error-as-content case this function was written for."""
    import unittest.mock as mock
    with mock.patch.object(app, "_is_upstream_nonanswer", return_value=True):
        assert app._chat_json_nonanswer(_resp("<html>429</html>")) is True


def test_empty_and_broken_payloads_are_safe():
    assert app._chat_json_nonanswer({}, has_tools=True) is False
    assert app._chat_json_nonanswer({"choices": []}, has_tools=True) is False
    assert app._chat_json_nonanswer(_resp(None), has_tools=True) is False


def test_list_shaped_content_is_read_too():
    data = {"choices": [{"message": {"role": "assistant",
                                     "content": [{"type": "text", "text": REAL}]}}]}
    assert app._chat_json_nonanswer(data, has_tools=True) is True


def test_the_detector_is_available_on_its_own():
    """Used by the swarm too -- a member that typed its tool call did not
    answer, and must not be allowed to win the slot."""
    assert app._looks_like_text_tool_call(REAL) is True
    assert app._looks_like_text_tool_call("all done, files written") is False
    assert app._looks_like_text_tool_call("") is False
    assert app._looks_like_text_tool_call(None) is False


def test_the_swarm_rejects_a_member_that_typed_its_tool_call():
    """It looks like prose, so without this it would be a candidate answer --
    and if it won, the CLI would execute nothing and the build would stop,
    which is precisely the reported symptom."""
    from unittest import mock

    class _R:
        status_code = 200
        headers = {}

        def json(self):
            return _resp(REAL)

        def close(self):
            pass

    with mock.patch.object(app, "_route_by_difficulty",
                           return_value=("groq", "m", "hard")), \
            mock.patch.object(app, "_build_chain", return_value=[("groq", "m")]), \
            mock.patch.object(app, "_dispatch_chat_with_deadline",
                              return_value=(_R(), None)), \
            app.app.test_request_context("/v1/chat/completions"):
        out = app._swarm_tool_result({"messages": [{"role": "user", "content": "go"}],
                                      "tools": [{"type": "function"}]})
    assert out is None, "a typed-out tool call must not win a swarm slot"
