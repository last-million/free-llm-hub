r"""The sub-* streaming branch shipped an empty turn and called it done.

Found while chasing "opencode and codex stop, specially when he sees the bash".
The relay defect (see test_a_tool_call_that_says_stop) was measured on an
upstream; this one the hub does to itself, and it is worse: not a mislabelled
tool call but a DELETED one.

THE CODE, verbatim, at both /v1/chat/completions and /v1/responses:

    msg = ((data.get("choices") or [{}])[0].get("message") or {})
    chunk = {... "delta": {"role": "assistant", "content": msg.get("content") or ""},
             "finish_reason": None}]}
    done = dict(chunk, choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}])

`msg["tool_calls"]` is never read, and the finish_reason is a literal. So a
`sub-claude` / `sub-codex` hop that answers a tools turn hands the CLI a
zero-length assistant message and tells it the turn ended normally.

AND IT IS WORSE THAN "a sub rarely calls a tool", because of what runs two
lines earlier. `_chat_json_nonanswer(data, has_tools, body["tools"])` calls
`tool_rescue.rescue(data, tools)`, which MUTATES `data` in place -- promoting a
prose-typed call into real `tool_calls`, setting finish_reason "tool_calls",
and setting `msg["content"] = left or None`. It then returns False, meaning
"that was an answer, carry on". So on exactly the turns where the rescue
succeeded, content is None and the repair is thrown away one line later:
`msg.get("content") or ""` is "".

The rescue exists because free models type tool calls as prose. Landing its
output in a branch that reads only `content` meant the better the rescue got,
the emptier the turn.

Both call sites now go through one helper, because they had drifted apart
already: the chat one at least carried `role`, the responses one did not.
"""
import app as A


def _data(content=None, tool_calls=None, finish="stop"):
    msg = {"role": "assistant"}
    if content is not None:
        msg["content"] = content
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"message": msg, "finish_reason": finish}]}


CALL = [{"id": "call_1", "type": "function",
         "function": {"name": "bash", "arguments": '{"command":"ls"}'}}]


# --------------------------------------------------------------------------- #
# What was being dropped
# --------------------------------------------------------------------------- #

def test_a_tool_call_survives_the_conversion():
    delta, _fin = A._sub_stream_message(_data(tool_calls=CALL))
    assert delta["tool_calls"][0]["function"]["name"] == "bash"


def test_it_arrives_with_an_index():
    delta, _fin = A._sub_stream_message(_data(tool_calls=CALL))
    assert delta["tool_calls"][0]["index"] == 0


def test_the_turn_is_not_reported_as_finished():
    """finish_reason 'stop' is what makes the CLI discard the call it just
    parsed -- the same silent stop the relay repair fixes for upstreams."""
    _delta, fin = A._sub_stream_message(_data(tool_calls=CALL))
    assert fin == "tool_calls"


def test_the_rescued_shape_is_the_one_that_used_to_vanish():
    """tool_rescue leaves content None and tool_calls set. That is precisely
    what `msg.get("content") or ""` turned into an empty message."""
    delta, fin = A._sub_stream_message(_data(content=None, tool_calls=CALL))
    assert delta["tool_calls"] and fin == "tool_calls"
    assert delta["content"] == ""       # no text, but the call is there


# --------------------------------------------------------------------------- #
# ...without changing a prose turn
# --------------------------------------------------------------------------- #

def test_a_prose_answer_is_unchanged():
    delta, fin = A._sub_stream_message(_data(content="hello"))
    assert delta == {"role": "assistant", "content": "hello"}
    assert fin == "stop"


def test_content_is_never_None_on_the_wire():
    """Some clients do `content.length` without a null check."""
    delta, _fin = A._sub_stream_message(_data(content=None))
    assert delta["content"] == ""


def test_an_upstream_finish_reason_is_respected():
    _delta, fin = A._sub_stream_message(_data(content="cut off", finish="length"))
    assert fin == "length"


def test_length_is_not_promoted_even_with_a_tool_call():
    """A truncated call must read as truncated, not as something to run."""
    _delta, fin = A._sub_stream_message(_data(tool_calls=CALL, finish="length"))
    assert fin == "length"


def test_a_missing_finish_reason_still_defaults_to_stop():
    _delta, fin = A._sub_stream_message({"choices": [{"message": {"content": "x"}}]})
    assert fin == "stop"


def test_an_empty_payload_does_not_raise():
    delta, fin = A._sub_stream_message({})
    assert delta["content"] == "" and fin == "stop"


def test_a_preexisting_index_is_kept():
    calls = [{"index": 3, "id": "c", "type": "function",
              "function": {"name": "bash", "arguments": "{}"}}]
    delta, _fin = A._sub_stream_message(_data(tool_calls=calls))
    assert delta["tool_calls"][0]["index"] == 3


# --------------------------------------------------------------------------- #
# Both protocols use it, and neither reads `content` on its own any more
# --------------------------------------------------------------------------- #

def test_both_streaming_sub_branches_use_the_helper():
    """There are two, they had already drifted, and a fix applied to one of them
    leaves the other CLI broken."""
    src = open("app.py", encoding="utf-8").read()
    calls = (src.count("_sub_stream_message(data)")
             - src.count("def _sub_stream_message(data)"))
    assert calls == 2, "expected the chat and the responses branch, got %d" % calls


def test_neither_branch_still_builds_a_content_only_delta():
    src = open("app.py", encoding="utf-8").read()
    assert '"delta": {"role": "assistant", "content": msg.get("content") or ""}' not in src
    assert '{"delta": {"content": msg.get("content") or ""}}' not in src
