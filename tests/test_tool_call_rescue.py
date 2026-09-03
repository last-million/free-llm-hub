"""A tool call the model TYPED is still a tool call.

Plenty of free models pick the right tool with the right arguments and then
write it into the message content instead of emitting it in `tool_calls` --
wrong fine-tune dialect, or a provider adapter that dropped the field. To the
client that is prose, so the CLI executes nothing and the build stops.

The hub already SPOTTED this (_looks_like_text_tool_call) and reacted by marking
the model dead for the TTL and retrying the whole turn elsewhere. Right when the
text is unusable; wasteful when it contains a complete call, because a working
model gets sidelined and a second inference is bought to produce an answer the
hub was already holding.

So it now parses first and discards only on failure.

The safety rule is the interesting half: a rescued call is emitted ONLY when its
name is one the client offered this request. A model that invents a tool name
has not produced a usable call -- returning it would make the agent loop fail on
an unknown tool, which is strictly worse than retrying elsewhere.
"""
import json

import tool_rescue as T


TOOLS = [{"type": "function", "function": {"name": "read_file"}},
         {"type": "function", "function": {"name": "write_file"}}]
NAMES = ["read_file", "write_file"]


def _args(call):
    return json.loads(call["arguments"])


# --------------------------------------------------------------------------- #
# The dialects
# --------------------------------------------------------------------------- #

def test_the_xml_json_dialect():
    calls = T.parse(
        '<tool_call>{"name": "read_file", "arguments": {"path": "a.txt"}}</tool_call>',
        NAMES)
    assert len(calls) == 1
    assert calls[0]["name"] == "read_file"
    assert _args(calls[0]) == {"path": "a.txt"}


def test_the_arg_key_arg_value_dialect():
    """GLM writes this one."""
    calls = T.parse("<tool_call>read_file\n"
                    "<arg_key>path</arg_key><arg_value>a.txt</arg_value>"
                    "</tool_call>", NAMES)
    assert calls[0]["name"] == "read_file"
    assert _args(calls[0]) == {"path": "a.txt"}


def test_a_fenced_json_call():
    calls = T.parse('Sure, I will read it.\n'
                    '```json\n{"name": "read_file", "arguments": {"path": "a.txt"}}\n```',
                    NAMES)
    assert _args(calls[0]) == {"path": "a.txt"}


def test_a_whole_tool_calls_field_written_into_content():
    calls = T.parse('{"tool_calls": [{"function": {"name": "read_file", '
                    '"arguments": "{\\"path\\": \\"a.txt\\"}"}}]}', NAMES)
    assert calls[0]["name"] == "read_file" and _args(calls[0]) == {"path": "a.txt"}


def test_the_legacy_function_call_field():
    calls = T.parse('{"function_call": {"name": "read_file", '
                    '"arguments": "{\\"path\\": \\"a.txt\\"}"}}', NAMES)
    assert calls[0]["name"] == "read_file"


def test_the_llama_function_tag():
    calls = T.parse('<function=read_file>{"path": "a.txt"}</function>', NAMES)
    assert calls[0]["name"] == "read_file" and _args(calls[0]) == {"path": "a.txt"}


def test_arguments_at_the_top_level_next_to_the_name():
    calls = T.parse('{"name": "read_file", "path": "a.txt"}', NAMES)
    assert _args(calls[0]) == {"path": "a.txt"}


def test_an_unclosed_block_is_still_rescued():
    """max_tokens truncation opens the tag, writes a complete object, and never
    closes it -- which is precisely when a retry is most expensive."""
    calls = T.parse('<tool_call>{"name": "read_file", "arguments": {"path": "a.txt"}}',
                    NAMES)
    assert calls[0]["name"] == "read_file"


def test_two_calls_in_one_turn():
    calls = T.parse('<tool_call>{"name":"read_file","arguments":{"path":"a"}}</tool_call>'
                    '<tool_call>{"name":"write_file","arguments":{"path":"b"}}</tool_call>',
                    NAMES)
    assert [c["name"] for c in calls] == ["read_file", "write_file"]


def test_the_same_call_matched_twice_is_emitted_once():
    """A fenced block that ALSO parses as bare JSON must not double-fire, or the
    agent runs the same command twice."""
    calls = T.parse('```json\n{"name": "read_file", "arguments": {"path": "a"}}\n```',
                    NAMES)
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# Parsing that has to be right
# --------------------------------------------------------------------------- #

def test_nested_argument_objects_survive():
    """A regex would truncate at the first inner brace, silently corrupting the
    arguments instead of failing."""
    calls = T.parse('<tool_call>{"name": "write_file", "arguments": '
                    '{"path": "a", "meta": {"deep": {"x": 1}}}}</tool_call>', NAMES)
    assert _args(calls[0])["meta"]["deep"]["x"] == 1


def test_a_brace_inside_a_string_does_not_end_the_object():
    calls = T.parse('<tool_call>{"name": "write_file", "arguments": '
                    '{"body": "a } brace"}}</tool_call>', NAMES)
    assert _args(calls[0])["body"] == "a } brace"


def test_an_escaped_quote_inside_a_string_is_handled():
    calls = T.parse('<tool_call>{"name": "write_file", "arguments": '
                    '{"body": "say \\"hi\\""}}</tool_call>', NAMES)
    assert _args(calls[0])["body"] == 'say "hi"'


def test_arguments_are_always_a_json_string():
    """OpenAI's field is a STRING. Models write an object about as often, and a
    client handed the wrong type sees a broken call."""
    for text in ('<tool_call>{"name":"read_file","arguments":{"path":"a"}}</tool_call>',
                 '<tool_call>{"name":"read_file","arguments":"{\\"path\\":\\"a\\"}"}</tool_call>'):
        c = T.parse(text, NAMES)[0]
        assert isinstance(c["arguments"], str)
        assert json.loads(c["arguments"]) == {"path": "a"}


# --------------------------------------------------------------------------- #
# The safety rule
# --------------------------------------------------------------------------- #

def test_a_tool_the_client_never_offered_is_not_rescued():
    """An invented name would make the agent loop fail on an unknown tool --
    worse than retrying on another model, which is what still happens."""
    assert T.parse('<tool_call>{"name": "rm_rf", "arguments": {}}</tool_call>',
                   NAMES) == []


def test_without_the_offered_names_nothing_is_validated_away():
    """parse() alone is dialect-level; the gate lives in rescue()."""
    assert T.parse('<tool_call>{"name": "anything", "arguments": {}}</tool_call>')


def test_rescue_does_nothing_when_the_request_offered_no_tools():
    data = {"choices": [{"message": {"content":
                                     '<tool_call>{"name":"read_file","arguments":{}}</tool_call>'}}]}
    assert T.rescue(data, []) is False
    assert "tool_calls" not in data["choices"][0]["message"]


def test_rescue_leaves_a_real_tool_call_alone():
    data = {"choices": [{"message": {"content": "x", "tool_calls": [{"id": "a"}]}}]}
    assert T.rescue(data, TOOLS) is False


def test_prose_about_tools_is_not_rescued():
    data = {"choices": [{"message": {"content":
                                     "You could call read_file with a path."}}]}
    assert T.rescue(data, TOOLS) is False


def test_junk_is_not_rescued():
    for text in ("", "just talking", "{not json", "<tool_call></tool_call>"):
        assert T.parse(text, NAMES) == [], text


# --------------------------------------------------------------------------- #
# What the client ends up with
# --------------------------------------------------------------------------- #

def _rescued(content):
    data = {"choices": [{"message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}]}
    ok = T.rescue(data, TOOLS)
    return ok, data["choices"][0]


def test_rescue_produces_a_well_formed_openai_tool_call():
    ok, choice = _rescued('<tool_call>{"name":"read_file","arguments":{"path":"a"}}</tool_call>')
    assert ok
    call = choice["message"]["tool_calls"][0]
    assert call["type"] == "function" and call["id"]
    assert call["function"]["name"] == "read_file"


def test_the_finish_reason_becomes_tool_calls():
    """Agent loops branch on it; leaving "stop" makes the CLI end the turn."""
    _ok, choice = _rescued('<tool_call>{"name":"read_file","arguments":{}}</tool_call>')
    assert choice["finish_reason"] == "tool_calls"


def test_the_typed_call_is_stripped_out_of_the_prose():
    """Otherwise the user is shown raw XML next to the real call."""
    _ok, choice = _rescued('Reading it now.\n'
                           '<tool_call>{"name":"read_file","arguments":{}}</tool_call>')
    assert choice["message"]["content"] == "Reading it now."


def test_content_is_none_when_nothing_but_the_call_was_written():
    """Empty string is not the same as absent; strict clients reject it."""
    _ok, choice = _rescued('<tool_call>{"name":"read_file","arguments":{}}</tool_call>')
    assert choice["message"]["content"] is None


def test_list_content_is_handled():
    data = {"choices": [{"message": {"content": [
        {"type": "text",
         "text": '<tool_call>{"name":"read_file","arguments":{}}</tool_call>'}]}}]}
    assert T.rescue(data, TOOLS) is True


def test_tool_names_reads_both_shapes():
    assert T.tool_names(TOOLS) == ["read_file", "write_file"]
    assert T.tool_names([{"name": "bare"}]) == ["bare"]


def test_rescue_never_raises_on_junk():
    for junk in (None, {}, {"choices": []}, {"choices": [None]}, "nope", []):
        assert T.rescue(junk, TOOLS) is False


# --------------------------------------------------------------------------- #
# ...and the hub actually calls it
# --------------------------------------------------------------------------- #

def _app_source():
    with open("app.py", encoding="utf-8") as f:
        return f.read()


def test_the_verdict_function_rescues_before_condemning():
    src = _app_source()
    body = src.split("def _chat_json_nonanswer(", 1)[1].split("\ndef ", 1)[0]
    assert "tool_rescue.rescue(data, tools)" in body
    # Past the docstring: it names _looks_like_text_tool_call in prose, which is
    # not the call being ordered against.
    code = body.split('"""', 2)[2]
    assert code.index("tool_rescue.rescue") < code.index("_looks_like_text_tool_call")


def test_every_call_site_hands_over_the_offered_tools():
    """Without the names the rescue is skipped, so a site that forgot them would
    silently keep the old behaviour."""
    src = _app_source()
    sites = [l for l in src.splitlines()
             if "_chat_json_nonanswer(data" in l and "def " not in l]
    assert len(sites) == 6
    for line in sites:
        assert "has_tools, " in line, line


def test_the_swarm_path_rescues_too():
    """It judges the message itself rather than through the verdict function, so
    it needs the rescue explicitly -- and it is the mode that has already paid
    for N inferences before reaching this point."""
    src = _app_source()
    i = src.index("Same rescue as the single-model path")
    assert "tool_rescue.rescue(data, body.get(\"tools\"))" in src[i:i + 500]
