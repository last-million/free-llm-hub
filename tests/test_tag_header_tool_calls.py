"""A tool call typed as a "<task 1>" header + 'key: "value"' lines is rescued (2026-11-11).

MEASURED in a real OpenCode turn (coding-max, served by openrouter recursive/flint-v1.0:free):
the model typed three sub-agent calls in this shape, then "Stop. No more content after the
third task call. End of turn. Done.", and no dialect knew it, so the reply reached the CLI as
text and the turn ended. The replies here are synthetic; only the SHAPE is the measured one.
"""
import json

import pytest

import app
import tool_rescue as tr

TASK_PROPS = {"description": {"type": "string"}, "prompt": {"type": "string"},
              "subagent_type": {"type": "string"}, "task_id": {"type": "string"},
              "command": {"type": "string"}}
TOOLS = [
    {"type": "function", "function": {"name": "task", "parameters": {
        "type": "object", "properties": TASK_PROPS,
        "required": ["description", "prompt", "subagent_type"]}}},
    {"type": "function", "function": {"name": "bash", "parameters": {
        "type": "object", "properties": {"command": {"type": "string"},
                                         "timeout": {"type": "integer"}},
        "required": ["command"]}}},
]

STOP = "Stop. No more content after the third task call. End of turn. Done."


def _block(i, desc, prompt, extra=""):
    return ('<task %d>\ndescription: "%s"\nprompt: "%s"\nsubagent_type: "general"\n'
            'task_id: ""%s\n' % (i, desc, prompt, extra))


REPLY = ("I will split the work across three helpers.\n\n"
         + _block(1, "Map the API", "List every route in src/api and its handler.") + "\n"
         + _block(2, "Check the tests", "Run the unit tests and report failures.") + "\n"
         + _block(3, "Read the docs", "Summarise docs/setup.md in five lines.") + "\n"
         + STOP)


def _msg(text):
    return {"choices": [{"message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}]}


def _frames(text, size):
    for i in range(0, len(text), size):
        yield b"data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": text[i:i + size]},
                                                   "finish_reason": None}]}).encode()
    yield b"data: " + json.dumps({"choices": [{"index": 0, "delta": {},
                                               "finish_reason": "stop"}]}).encode()
    yield b"data: [DONE]"


def _read_stream(units):
    calls, text, fin, errors = [], "", None, 0
    for u in units:
        s = u.decode()[6:] if isinstance(u, (bytes, bytearray)) else str(u)[6:]
        if s.strip() == "[DONE]" or not s.strip():
            continue
        o = json.loads(s)
        if "error" in o:
            errors += 1
            continue
        ch = o["choices"][0]
        d = ch.get("delta") or {}
        text += d.get("content") or ""
        for tc in d.get("tool_calls") or []:
            calls.append((tc["function"]["name"], json.loads(tc["function"]["arguments"])))
        fin = ch.get("finish_reason") or fin
    return calls, text, fin, errors


# --------------------------------------------------------------------------- non-stream

def test_the_measured_shape_becomes_three_real_task_calls():
    data = _msg(REPLY)
    assert tr.rescue(data, TOOLS) is True
    msg = data["choices"][0]["message"]
    calls = [(c["function"]["name"], json.loads(c["function"]["arguments"])) for c in msg["tool_calls"]]
    assert [n for n, _a in calls] == ["task", "task", "task"]
    assert calls[1][1] == {"description": "Check the tests",
                           "prompt": "Run the unit tests and report failures.",
                           "subagent_type": "general"}
    assert data["choices"][0]["finish_reason"] == "tool_calls"
    # The prose before the calls stays; the typed calls and the stop note are gone.
    assert msg["content"] == "I will split the work across three helpers."


def test_an_empty_optional_value_is_dropped_but_a_required_one_is_kept():
    text = '<task 1>\ndescription: ""\nprompt: "do it"\nsubagent_type: "general"\ntask_id: ""\n'
    [call] = tr.parse(text, allowed_names=["task"], schemas=tr.tool_schemas(TOOLS))
    args = json.loads(call["arguments"])
    assert args == {"description": "", "prompt": "do it", "subagent_type": "general"}


def test_values_are_typed_by_the_schema_and_a_closing_tag_ends_the_block():
    text = "<bash>\ncommand: ls -la\ntimeout: 30\n</bash>\nAfter."
    data = _msg(text)
    assert tr.rescue(data, TOOLS)
    msg = data["choices"][0]["message"]
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"command": "ls -la", "timeout": 30}
    assert msg["content"] == "After."


def test_a_quoted_value_that_runs_over_several_lines_is_read_whole():
    text = '<task 1>\ndescription: "Two lines"\nprompt: "first line\nsecond line"\nsubagent_type: "general"\n'
    [call] = tr.parse(text, allowed_names=["task"], schemas=tr.tool_schemas(TOOLS))
    assert json.loads(call["arguments"])["prompt"] == "first line\nsecond line"


@pytest.mark.parametrize("text", [
    # a tool that was never offered
    '<agent 1>\ndescription: "x"\nprompt: "y"\n',
    # a header no parameter line confirms
    "<task 1>\nThis is the first step of the plan.",
    "<task 1> is the first step of the plan.",
    # a line after the header that names an undeclared key
    '<task 1>\ncolour: "blue"\n',
    # inside a fence or inline code: an explanation, not a call
    '```\n<task 1>\ndescription: "x"\nprompt: "y"\nsubagent_type: "general"\n```',
    # not at the start of a line
    'Write it as <task 1>\ndescription: "x"\nprompt: "y"\nsubagent_type: "general"\n'.replace("\n", " ", 1),
    # ordinary HTML
    '<div>\nclass: "hero"\n</div>',
])
def test_what_is_not_a_typed_call_is_left_alone(text):
    data = _msg(text)
    assert tr.rescue(data, TOOLS) is False
    assert data["choices"][0]["message"]["content"] == text


def test_an_unrescuable_typed_call_to_an_unoffered_tool_hands_the_turn_on():
    text = 'I will ask a helper.\n<agent 1>\ndescription: "x"\nprompt: "y"\n'
    assert app._looks_like_text_tool_call(text, TOOLS)
    assert not app._looks_like_text_tool_call("A plain answer with no typed call.", TOOLS)


# --------------------------------------------------------------------------- stream

@pytest.mark.parametrize("size", [1, 3, 7, 10000])
def test_the_streamed_shape_becomes_real_calls_at_any_chunk_size(size):
    calls, text, fin, errors = _read_stream(tr.rescue_stream(_frames(REPLY, size), TOOLS))
    assert errors == 0
    assert [n for n, _a in calls] == ["task", "task", "task"]
    assert calls[2][1]["prompt"] == "Summarise docs/setup.md in five lines."
    assert fin == "tool_calls"
    assert "<task" not in text and "subagent_type" not in text and "End of turn" not in text
    assert text.strip() == "I will split the work across three helpers."


@pytest.mark.parametrize("size", [1, 4, 10000])
@pytest.mark.parametrize("text", [
    "Step one.\n<task 1> is the first step of the plan.\nThat is all.",
    "Step one.\n<task 1>\nThis is plain prose after a header-looking line.",
    "Use a <task 1>\nheader? no.",
    "A <tag>\nkey: \"value\" inside prose with no offered tool.",
])
def test_streamed_text_that_only_looks_like_a_header_arrives_unchanged(text, size):
    calls, out, fin, errors = _read_stream(tr.rescue_stream(_frames(text, size), TOOLS))
    assert errors == 0 and calls == []
    assert out == text
    assert fin == "stop"


def test_a_header_in_the_middle_of_a_line_across_deltas_is_not_a_call():
    text = 'Say <task 1>\ndescription: "x"\nprompt: "y"\nsubagent_type: "general"\n'
    units = [b"data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": c},
                                                  "finish_reason": None}]}).encode()
             for c in ("Say ", '<task 1>\ndescription: "x"\nprompt: "y"\nsubagent_type: "general"\n')]
    units.append(b"data: " + json.dumps({"choices": [{"index": 0, "delta": {},
                                                      "finish_reason": "stop"}]}).encode())
    calls, out, fin, errors = _read_stream(tr.rescue_stream(iter(units), TOOLS))
    assert calls == [] and errors == 0 and out == text


# --------------------------------------------------------------------------- the stop note

@pytest.mark.parametrize("text,kept", [
    ("Plan ready.\n\n" + STOP, "Plan ready."),
    ("Plan ready.\n\nEnd of turn.", "Plan ready."),
    ("Plan ready.\n\nDone.", "Plan ready.\n\nDone."),
    ("We are done. There is no more content to add.", "We are done. There is no more content to add."),
])
def test_only_a_whole_stop_note_paragraph_is_dropped(text, kept):
    assert tr._drop_stop_note(text) == kept
