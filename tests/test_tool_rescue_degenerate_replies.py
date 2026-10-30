"""The tool-call rescue is fast on degenerate replies and emits only calls a
client can run, each with its own id.

1. A looping model's reply of unbalanced braces ('{"a": ' repeated), bare '{',
   '<invoke ' openers with no '>' or bare '<tool_call>' openers made the rescue
   rescan to the end of the text from every opener: seconds to minutes of CPU on
   the request thread before the hop could fail over.
2. A rescued call's arguments could be `42`, `["ls"]`, a quoted string, or the
   {"input": text} stand-in for non-JSON text on a tool that has no "input"
   parameter. The client got a call it cannot run instead of a failover.
   Qwen3-Coder's <function=NAME><parameter=KEY>...</parameter> form is now read
   as the object it is.
3. Every non-stream rescued call was "call_rescued_0": the bandit keys its
   delayed tool-call credit on the id, so ids repeating across turns and
   conversations lost or misplaced that credit.

Pure module tests, no network.
"""
import json
import time

import pytest

import tool_rescue as T


TOOLS = [{"type": "function", "function": {"name": "bash", "parameters": {
    "type": "object", "properties": {"command": {"type": "string"},
                                     "timeout": {"type": "integer"}},
    "required": ["command"]}}}]
NAMES = ["bash"]
SCHEMAS = T.tool_schemas(TOOLS)


def _timed(fn, *args):
    t = time.perf_counter()
    fn(*args)
    return time.perf_counter() - t


# --------------------------------------------------------------------------- #
# 1. Degenerate replies cost linear time
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text", [
    '{"a": ' * 8000,                         # a max_tokens loop of open objects
    "{" * 20000,
    "<invoke " * 8000,                       # openers with no '>'
    '<invoke name="bash">' + "<invoke " * 8000,
    "<tool_call>" * 8000,
    "<tool_call></tool_call>" + "<tool_call>" * 8000,
], ids=["open-objects", "braces", "invoke", "invoke-after-call", "tool-call",
        "tool-call-after-close"])
def test_a_looping_reply_is_parsed_quickly(text):
    # Each of these took 5-30+ s before; linear now (well under a second).
    # Generous bound: a busy test machine must not make it flaky.
    best = min(_timed(T.parse, text, NAMES, SCHEMAS) for _ in range(2))
    assert best < 3.0, best
    best = min(_timed(T.has_model_markup, text) for _ in range(2))
    assert best < 3.0, best


def test_the_rescue_of_a_looping_reply_is_quick_and_finds_nothing():
    data = {"choices": [{"message": {"content": '{"a": ' * 8000}}]}
    t = time.perf_counter()
    assert T.rescue(data, TOOLS) is False
    assert time.perf_counter() - t < 3.0


def test_objects_after_unbalanced_braces_are_still_found():
    text = "{ { { " + '{"name": "bash", "arguments": {"command": "ls"}}' + " {"
    calls = T.parse(text, NAMES, SCHEMAS)
    assert [json.loads(c["arguments"]) for c in calls] == [{"command": "ls"}]


def test_the_brace_scan_still_reads_strings_and_nesting_the_same_way():
    text = ('prose with a "quote then {"name": "bash", "arguments": '
            '{"command": "echo \\"}\\" {x}"}} and {not json} {"k": {"n": 1}}')
    got = [(obj, s, e) for obj, s, e in T._json_objects(text)]
    assert got[0][0] == {"name": "bash", "arguments": {"command": 'echo "}" {x}'}}
    assert got[-1][0] == {"k": {"n": 1}}
    assert [g[0] for g in got] == [got[0][0], {"k": {"n": 1}}]
    # each object's span is exactly its text
    for obj, s, e in got:
        assert json.loads(text[s:e]) == obj


def test_a_brace_inside_a_string_of_an_unclosed_object_is_a_fresh_start():
    # The outer object never closes; the '{' inside its string is a fresh start
    # (outside any string) and closes on its own, exactly as before.
    text = '{"a": "x {"k": 1}'
    assert [o for o, _s, _e in T._json_objects(text)] == [{"k": 1}]


def _old_json_objects(text):
    """The previous (quadratic) scan, kept here as the reference."""
    i, n = 0, len(text or "")
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth, j, in_str, esc = 0, i, False, False
        while j < n:
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if j < n and depth == 0:
            try:
                yield json.loads(text[i:j + 1]), i, j + 1
            except (ValueError, RecursionError):
                pass
            i = j + 1
        else:
            i += 1


def test_the_new_scan_finds_exactly_what_the_old_one_did():
    import random
    rng = random.Random(20261030)
    tokens = ['{', '}', '"', '\\', '"a"', ':', '1', ',', ' ', 'x', '[1]',
              '{}', '{"k":1}', '{"k":{"n":"}"}}', '"{"', '\\"']
    found = 0
    for _ in range(6000):
        text = "".join(rng.choice(tokens) for _ in range(rng.randint(0, 30)))
        new = list(T._json_objects(text))
        assert new == list(_old_json_objects(text)), text
        found += len(new)
    assert found > 1000                       # the comparison saw real objects


def test_deep_nesting_does_not_raise():
    # json.loads itself gives up on this with RecursionError (not ValueError).
    text = '{"a":' * 5000 + "1" + "}" * 5000
    assert isinstance(list(T._json_objects(text)), list)
    assert T.parse(text, NAMES, SCHEMAS) == []


# --------------------------------------------------------------------------- #
# 2. Only calls a client can run
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text", [
    "<function=bash>42</function>",
    "<function=bash>null</function>",
    '<function=bash>"ls -la"</function>',
    '<tool_call>{"name": "bash", "arguments": "[\\"ls\\"]"}</tool_call>',
    '<tool_call>{"name": "bash", "arguments": 7}</tool_call>',
])
def test_arguments_that_are_not_an_object_are_not_a_call(text):
    assert T.parse(text, NAMES, SCHEMAS) == []
    assert T.parse(text, NAMES) == []
    data = {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}
    assert T.rescue(data, TOOLS) is False
    assert "tool_calls" not in data["choices"][0]["message"]


def test_plain_text_for_a_tool_without_an_input_parameter_is_not_a_call():
    text = "<function=bash>ls -la</function>"
    assert T.parse(text, NAMES, SCHEMAS) == []
    assert T.rescue({"choices": [{"message": {"content": text}}]}, TOOLS) is False


def test_plain_text_for_a_tool_that_declares_input_is_still_rescued():
    tools = [{"type": "function", "function": {"name": "run", "parameters": {
        "type": "object", "properties": {"input": {"type": "string"}}}}}]
    calls = T.parse("<function=run>ls -la</function>", ["run"], T.tool_schemas(tools))
    assert [json.loads(c["arguments"]) for c in calls] == [{"input": "ls -la"}]


def test_without_a_schema_plain_text_keeps_the_old_shape():
    # parse() alone (no schema) cannot know the parameters: unchanged.
    calls = T.parse("<function=bash>ls -la</function>", NAMES)
    assert [json.loads(c["arguments"]) for c in calls] == [{"input": "ls -la"}]


@pytest.mark.parametrize("text", [
    "<function=bash><parameter=command>ls -la</parameter></function>",
    "<function=bash>\n<parameter=command>\nls -la\n</parameter>\n</function>",
    "<tool_call>\n<function=bash>\n<parameter=command>\nls -la\n</parameter>\n"
    "</function>\n</tool_call>",
    '<function=bash><parameter name="command">ls -la</parameter></function>',
])
def test_the_qwen3_coder_parameter_form_is_read_as_an_object(text):
    calls = T.parse(text, NAMES, SCHEMAS)
    assert [c["name"] for c in calls] == ["bash"]
    assert json.loads(calls[0]["arguments"]) == {"command": "ls -la"}
    data = {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}
    assert T.rescue(data, TOOLS) is True
    msg = data["choices"][0]["message"]
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"command": "ls -la"}
    assert msg["content"] is None


def test_qwen_parameters_are_typed_by_the_schema():
    text = ("<function=bash><parameter=command>sleep 1</parameter>"
            "<parameter=timeout>30</parameter></function>")
    calls = T.parse(text, NAMES, SCHEMAS)
    assert json.loads(calls[0]["arguments"]) == {"command": "sleep 1", "timeout": 30}


def test_a_json_body_mentioning_a_parameter_tag_is_still_read_as_json():
    text = ('<function=bash>{"command": "echo <parameter=x>y</parameter>"}</function>')
    calls = T.parse(text, NAMES, SCHEMAS)
    assert json.loads(calls[0]["arguments"]) == {"command": "echo <parameter=x>y</parameter>"}


def test_a_parameter_value_holding_json_is_not_taken_for_the_arguments():
    text = ('<function=bash><parameter=command>echo {"a": 1}</parameter></function>')
    calls = T.parse(text, NAMES, SCHEMAS)
    assert json.loads(calls[0]["arguments"]) == {"command": 'echo {"a": 1}'}


@pytest.mark.parametrize("text", [
    "<function=bash>42</function>",
    "<function=bash>ls -la</function>",
    '{"name": "bash", "arguments": 7}',
])
def test_a_refused_typed_call_is_still_a_non_answer_not_prose_to_serve(text):
    # It used to be emitted as a broken call; refused, it must go to the next
    # model -- never be served as the final prose answer.
    import app as A
    assert T.typed_call_unusable(text, TOOLS) is True
    data = {"choices": [{"message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}]}
    assert A._chat_json_nonanswer(data, True, TOOLS) is True
    assert "tool_calls" not in data["choices"][0]["message"]


@pytest.mark.parametrize("text", [
    '{"name": "my-app", "version": "1.0.0"}',      # a package.json, not a call
    "Done: the tests pass and the build is green.",
    '<tool_call>{"name": "bash", "arguments": {"command": "ls"}}</tool_call>',
])
def test_ordinary_replies_and_usable_calls_are_not_refused_calls(text):
    assert T.typed_call_unusable(text, TOOLS) is False


def test_a_json_body_is_still_rescued():
    calls = T.parse('<function=bash>{"command": "ls"}</function>', NAMES, SCHEMAS)
    assert json.loads(calls[0]["arguments"]) == {"command": "ls"}


# --------------------------------------------------------------------------- #
# 3. Each rescued call has its own id
# --------------------------------------------------------------------------- #

def test_rescued_ids_are_unique_across_responses_and_calls():
    ids = []
    for _ in range(3):
        data = {"choices": [{"message": {"content":
                '<tool_call>{"name":"bash","arguments":{"command":"a"}}</tool_call>'
                '<tool_call>{"name":"bash","arguments":{"command":"b"}}</tool_call>'}}]}
        assert T.rescue(data, TOOLS) is True
        ids += [tc["id"] for tc in data["choices"][0]["message"]["tool_calls"]]
    assert len(ids) == 6 and len(set(ids)) == 6
    assert all(i.startswith("call_rescued_") for i in ids)
