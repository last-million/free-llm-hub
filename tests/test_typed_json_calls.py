# -*- coding: utf-8 -*-
"""A tool call typed as a JSON object no longer ends a streamed turn.

MEASURED 2026-11-07, a real OpenCode run through the live hub: the last reply was prose
followed by {"name":"bash","arguments":"{\\"command\\":\\"node -e \\"...","workdir":"..."}},
so the check the model wanted to run never ran and the turn ended on that text.

Three faults, all reproduced:
  1. tool_rescue.rescue_stream only knew TAG dialects. A bare JSON call passed as text on
     every stream, valid or not, and every CLI streams.
  2. The inner quotes were unescaped, so `arguments` was not JSON and the call was thrown
     away although what it wanted to run was all there.
  3. The stream's end-of-reply check did not get the tools, so an unusable typed call read
     as a final answer instead of a failed hop.

Hermetic: no network. The command in the fixture is the one the run typed.
"""
import json

import pytest

import app as A
import tool_rescue as tr


def _tool(name, props=("command", "workdir"), required=("command",)):
    return {"type": "function", "function": {
        "name": name, "description": "x",
        "parameters": {"type": "object", "required": list(required),
                       "properties": {p: {"type": "string"} for p in props}}}}


TOOLS = [_tool("bash"), _tool("read", props=("path",), required=("path",))]
NAMES = tr.tool_names(TOOLS)
SCHEMAS = tr.tool_schemas(TOOLS)

# The exact shape: `arguments` is a string, its inner quotes unescaped; `workdir` is outside
# `arguments`; one stray closing brace at the end.
COMMAND = ("node -e \"const fs=require('fs');const h=fs.readFileSync('index.html','utf8');"
           "console.log('bytes',h.length);\"")
BROKEN = ('{"name":"bash","arguments":"{\\"command\\":\\"node -e \\"const fs=require(\'fs\');'
          "const h=fs.readFileSync('index.html','utf8');console.log('bytes',h.length);"
          '\\"","workdir":"C:\\\\p\\\\site"}}')
PROSE = "Verifying the page loads without JS errors via a non-server check."
VALID = '{"name":"bash","arguments":{"command":"ls -la"}}'


def _frame(delta, finish=None):
    return b"data: " + json.dumps({
        "id": "x", "object": "chat.completion.chunk", "created": 1, "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}).encode("utf-8")


def _frames(text, size=None, finish="stop"):
    size = size or len(text)
    out = [_frame({"role": "assistant", "content": ""})]
    out += [_frame({"content": text[i:i + size]}) for i in range(0, len(text), size)]
    out.append(_frame({}, finish))
    out.append(b"data: [DONE]")
    return out


def _run(text, size=None, tools=TOOLS):
    got = list(tr.rescue_stream(_frames(text, size), tools, "lines"))
    content, calls, errors = "", [], []
    for raw in got:
        body = raw.decode("utf-8", "replace")
        if not body.startswith("data: {"):
            continue
        obj = json.loads(body[6:])
        if obj.get("error"):
            errors.append(obj["error"])
            continue
        delta = obj["choices"][0].get("delta") or {}
        content += delta.get("content") or ""
        calls += delta.get("tool_calls") or []
    return content, calls, errors


# --------------------------------------------------------------------------- #
# The repair
# --------------------------------------------------------------------------- #

def test_the_reply_that_ended_the_run_becomes_the_call_it_meant():
    calls = tr.parse(BROKEN, NAMES, SCHEMAS)
    assert len(calls) == 1 and calls[0]["name"] == "bash"
    args = json.loads(calls[0]["arguments"])
    assert args == {"command": COMMAND, "workdir": "C:\\p\\site"}


def test_a_closing_quote_goes_only_when_the_quotes_do_not_balance_without_it():
    raw = '{"command":"echo "hi""}'
    assert tr._repair_args("bash", raw, {}, SCHEMAS) == {"command": 'echo "hi"'}


def test_two_keys_are_both_read():
    raw = '{"command":"echo "a"","workdir":"C:\\p"}'
    assert tr._repair_args("bash", raw, {}, SCHEMAS) == {"command": 'echo "a"', "workdir": "C:\\p"}


def test_valid_arguments_are_left_alone():
    assert tr._repair_args("bash", '{"command":"ls"}', {}, SCHEMAS) is None


def test_a_key_the_tool_does_not_declare_is_never_read():
    assert tr._repair_args("read", '{"command":"rm -rf "x""}', {}, SCHEMAS) is None


def test_a_missing_required_key_is_no_repair():
    assert tr._repair_args("bash", '{"workdir":"C:\\p" oops', {}, SCHEMAS) is None


def test_with_no_schema_nothing_is_guessed():
    assert tr._repair_args("bash", '{"command":"a"b"}', {}, {}) is None
    assert tr._repair_args("bash", '{"command":"a"b"}', {}, None) is None


def test_plain_text_arguments_are_not_a_repair():
    assert tr._repair_args("bash", "ls -la", {}, SCHEMAS) is None


def test_a_call_with_nothing_to_salvage_stays_unusable():
    text = '{"name":"bash","arguments":"not json at all"}'
    assert tr.parse(text, NAMES, SCHEMAS) == []
    assert tr.typed_call_unusable(text, TOOLS) is True


def test_a_stray_top_level_key_only_counts_if_the_tool_declares_it():
    text = ('{"name":"bash","arguments":"{\\"command\\":\\"echo \\"a\\"\\"","evil":"x",'
            '"workdir":"C:\\\\p"}')
    args = json.loads(tr.parse(text, NAMES, SCHEMAS)[0]["arguments"])
    assert "evil" not in args and args["workdir"] == "C:\\p"


# --------------------------------------------------------------------------- #
# The stream
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("size", [None, 1, 3, 7, 40], ids=["whole", "1", "3", "7", "40"])
def test_a_typed_call_on_a_stream_becomes_a_real_call(size):
    content, calls, errors = _run(VALID, size)
    assert not errors and content == ""
    assert calls[0]["function"]["name"] == "bash"
    assert json.loads(calls[0]["function"]["arguments"]) == {"command": "ls -la"}


@pytest.mark.parametrize("size", [None, 1, 3, 7, 40], ids=["whole", "1", "3", "7", "40"])
def test_the_real_runs_reply_runs_and_keeps_its_prose(size):
    content, calls, errors = _run(PROSE + "\n\n" + BROKEN, size)
    assert not errors
    assert content.strip() == PROSE            # no raw JSON, no stray brace
    assert json.loads(calls[0]["function"]["arguments"])["command"] == COMMAND


def test_the_finish_reason_says_tool_calls():
    got = b"".join(tr.rescue_stream(_frames(VALID), TOOLS, "lines"))
    assert b'"finish_reason": "tool_calls"' in got


def test_every_spelling_of_the_call_is_caught():
    for text in ('{"tool":"bash","arguments":{"command":"ls"}}',
                 '{"tool_name":"bash","parameters":{"command":"ls"}}',
                 '{"function":{"name":"bash","arguments":{"command":"ls"}}}',
                 '{"tool_calls":[{"function":{"name":"bash","arguments":{"command":"ls"}}}]}',
                 '{ "name" : "bash", "arguments" : {"command":"ls"} }'):
        _c, calls, errors = _run(text, 4)
        assert calls and not errors, text


def test_a_call_past_saving_fails_the_hop_instead_of_ending_the_turn():
    _content, calls, errors = _run('{"name":"bash","arguments":{"command":"a"b"}', 5)
    assert not calls and errors and errors[0]["code"] == "unparseable_tool_markup"


@pytest.mark.parametrize("text", [
    'The tool is {"name":"not_offered","arguments":{}} and it does not exist here.',
    'Here is the config: {"foo": 1, "bar": [2, 3]} and that is all.',
    "A set in Python looks like {1, 2, 3} and a dict like {'a': 1}.",
    "Use `{\"name\":\"bash\"}` as the payload, in backticks.",
    "```json\n{\"name\":\"bash\",\"arguments\":{\"command\":\"ls\"}}\n```",
    "function f() { return 1; }",
])
@pytest.mark.parametrize("size", [None, 1, 5], ids=["whole", "1", "5"])
def test_ordinary_text_passes_untouched(text, size):
    frames = _frames(text, size)
    got = [x for x in tr.rescue_stream(frames, TOOLS, "lines")]
    assert b"".join(got) == b"".join(frames)


def test_held_text_keeps_its_order():
    text = "before {\"na and then it was not a call after all"
    content, calls, _e = _run(text, 2)
    assert content == text and not calls


def test_a_hold_ends_at_the_end_of_the_stream():
    content, calls, _e = _run('text then {"na', 3)
    assert content == 'text then {"na' and not calls


def test_without_tools_nothing_is_touched():
    frames = _frames(VALID, 3)
    assert b"".join(tr.rescue_stream(frames, [], "lines")) == b"".join(frames)


def test_real_tool_calls_win_over_a_typed_copy():
    frames = [_frame({"tool_calls": [{"index": 0, "id": "c", "type": "function", "function": {
        "name": "bash", "arguments": "{\"command\":\"ls\"}"}}]}),
        _frame({"content": VALID}), _frame({}, "tool_calls"), b"data: [DONE]"]
    got = b"".join(tr.rescue_stream(frames, TOOLS, "lines")).decode("utf-8")
    assert got.count('"name": "bash"') == 1


def test_the_frames_framing_works_too():
    frames = [f + b"\n\n" for f in _frames(VALID, 4)]
    got = b"".join(tr.rescue_stream(frames, TOOLS, "frames"))
    assert b'"tool_calls"' in got and b'"name": "bash"' in got


def test_a_huge_reply_of_braces_does_not_hang():
    content, _calls, _errors = _run("{" * 20000, 50)
    assert content


# --------------------------------------------------------------------------- #
# The end-of-reply check
# --------------------------------------------------------------------------- #

def _sse(text):
    """What the peek collected: the frames of a stream that carried `text`."""
    return [f.decode("utf-8") for f in _frames(text, 12)]


def test_an_unusable_typed_call_is_a_failed_hop_on_a_whole_reply():
    text = '{"name":"bash","arguments":"not json at all"}'
    check = {"tools": TOOLS, "tools_offered": True}
    with A.app.test_request_context("/v1/chat/completions"):
        assert A._judge_peeked(_sse(text), check=check, raw_items=None, complete=True) == "nonanswer"


def test_a_partial_reply_is_not_judged_on_it():
    text = '{"name":"bash","arguments":"not json at all"}'
    check = {"tools": TOOLS, "tools_offered": True}
    with A.app.test_request_context("/v1/chat/completions"):
        assert A._judge_peeked(_sse(text), check=check, complete=False) != "nonanswer"


def test_a_real_answer_stays_an_answer():
    check = {"tools": TOOLS, "tools_offered": True}
    text = "The page is written and every check passed, so there is nothing left to do."
    with A.app.test_request_context("/v1/chat/completions"):
        assert A._judge_peeked(_sse(text), check=check, complete=True) != "nonanswer"


# --------------------------------------------------------------------------- #
# The real handlers: every CLI gets a call it can run
# --------------------------------------------------------------------------- #

class _Resp:
    status_code = 200
    text = ""

    def __init__(self):
        self.headers = {}
        self.closed = False

    def close(self):
        self.closed = True

    def iter_lines(self, decode_unicode=False):
        for f in _frames(PROSE + "\n\n" + BROKEN, 9):
            yield f
            yield b""

    def iter_content(self, chunk_size=None):
        for f in _frames(PROSE + "\n\n" + BROKEN, 9):
            yield f + b"\n\n"


@pytest.fixture
def world(monkeypatch):
    for name in ("_record_chat_usage", "_save_perf_stats", "_act_pick", "_note_ttft",
                 "_note_provider_timeout", "_throttle_failed_hop", "_record_outcome"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)
    monkeypatch.setattr(A, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(A, "_model_block_reason", lambda pid, m: None)
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    monkeypatch.setattr(A, "_is_trivial_turn", lambda *a, **k: False)
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: 20)
    monkeypatch.setattr(A, "_stream_peek_timeout", lambda *a, **k: 2.0)
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("p1", "m1", "hard"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("p1", "m1")])
    monkeypatch.setattr(A, "_dispatch_chat", lambda *a, **k: _Resp())


def test_opencodes_protocol_gets_the_call(world):
    body = {"model": "auto", "stream": True, "tools": TOOLS,
            "messages": [{"role": "user", "content": "check the page"}]}
    out = A.app.test_client().post("/v1/chat/completions", json=body).get_data(as_text=True)
    assert '"tool_calls"' in out and "bash" in out
    said = ""
    for line in out.splitlines():
        if line.startswith("data: {"):
            for ch in json.loads(line[6:]).get("choices") or ():
                said += (ch.get("delta") or {}).get("content") or ""
    assert PROSE in said and '"name"' not in said        # the JSON is not shown as text


def test_codexs_protocol_gets_the_call(world):
    body = {"model": "auto", "stream": True, "input": "check the page",
            "tools": [{"type": "function", "name": "bash", "description": "x",
                       "parameters": TOOLS[0]["function"]["parameters"]}]}
    out = A.app.test_client().post("/v1/responses", json=body).get_data(as_text=True)
    assert '"function_call"' in out and "response.completed" in out


def test_claude_codes_protocol_gets_the_call(world):
    body = {"model": "auto", "max_tokens": 100, "stream": True,
            "messages": [{"role": "user", "content": "check the page"}],
            "tools": [{"name": "bash", "description": "x",
                       "input_schema": TOOLS[0]["function"]["parameters"]}]}
    out = A.app.test_client().post("/v1/messages", json=body).get_data(as_text=True)
    assert '"tool_use"' in out and "bash" in out
