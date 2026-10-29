"""A tool call typed as plain Anthropic markup is rescued, not leaked as prose.

MEASURED 2026-10-29 (opencode, an uncensored relay model): the model wrote its call as
text -- <invoke name="bash"><parameter name="command">...</parameter></invoke>, sometimes
wrapped in <function_calls>...</function_calls>, sometimes "antml:"-namespaced. tool_rescue
knew the fullwidth-bar DeepSeek (DSML) spelling of the same shape but not this plain one,
so the call reached the CLI as prose and nothing ran -- the turn "stopped". The tags are
built from pieces here so no literal tool-use tag sits in the test's own text.
"""
import json

import pytest

import app as A
import tool_rescue as tr

L, R, S = "<", ">", "/"


def inv(name, params, ns="", wrap=False):
    o = L + ns + 'invoke name="' + name + '"' + R
    body = ""
    for k, v in params:
        body += "\n" + L + ns + 'parameter name="' + k + '"' + R + v + L + S + ns + "parameter" + R
    out = o + body + "\n" + L + S + ns + "invoke" + R
    if wrap:
        out = L + ns + "function_calls" + R + out + L + S + ns + "function_calls" + R
    return out


TOOLS = [{"type": "function", "function": {"name": "bash", "parameters": {
    "type": "object", "properties": {"command": {"type": "string"},
                                     "timeout": {"type": "number"}}}}}]
NAMES = ["bash"]
SCHEMAS = tr.tool_schemas(TOOLS)


def _one(text):
    return tr.parse(text, allowed_names=NAMES, schemas=SCHEMAS)


# --------------------------------------------------------------------------- parse

def test_plain_invoke_is_rescued():
    calls = _one(inv("bash", [("command", "ls -la")]))
    assert len(calls) == 1 and calls[0]["name"] == "bash"
    assert json.loads(calls[0]["arguments"]) == {"command": "ls -la"}


def test_number_param_keeps_its_type_from_the_schema():
    calls = _one(inv("bash", [("command", "echo hi"), ("timeout", "180000")]))
    assert json.loads(calls[0]["arguments"]) == {"command": "echo hi", "timeout": 180000}


def test_antml_namespace_is_rescued():
    calls = _one(inv("bash", [("command", "id")], ns="antml:"))
    assert len(calls) == 1 and json.loads(calls[0]["arguments"]) == {"command": "id"}


def test_function_calls_wrapper_is_rescued():
    calls = _one(inv("bash", [("command", "whoami")], wrap=True))
    assert len(calls) == 1 and json.loads(calls[0]["arguments"]) == {"command": "whoami"}


def test_prose_before_the_call_is_rescued():
    calls = _one("Let me check that for you.\n" + inv("bash", [("command", "pwd")]))
    assert len(calls) == 1 and json.loads(calls[0]["arguments"]) == {"command": "pwd"}


def test_two_calls_in_one_reply():
    text = inv("bash", [("command", "ls")]) + "\n" + inv("bash", [("command", "pwd")])
    calls = _one(text)
    assert [json.loads(c["arguments"]) for c in calls] == [{"command": "ls"}, {"command": "pwd"}]


def test_a_name_not_offered_is_dropped():
    calls = _one(inv("rm_rf_everything", [("path", "/")]))
    assert calls == []


# --------------------------------------------------------------------------- guards

def test_a_fenced_mention_is_not_a_call():
    text = "Here is how a tool call looks:\n```\n" + inv("bash", [("command", "ls")]) + "\n```"
    assert tr.has_model_markup(text) is False
    assert _one(text) == []


def test_an_inline_mention_is_not_a_call():
    text = "write `" + inv("bash", [("command", "ls")]) + "` to call it"
    assert tr.has_model_markup(text) is False


def test_the_bare_word_invoke_is_not_a_call():
    assert tr.has_model_markup("I will invoke the bash tool and show you the parameter list.") is False


# --------------------------------------------------------------------------- strip

def test_strip_removes_the_block_keeps_the_prose():
    text = "Running it now. " + inv("bash", [("command", "ls")]) + " done."
    left = tr.strip_model_markup(text)
    assert "invoke" not in left and "parameter" not in left
    assert left.startswith("Running it now.") and left.endswith("done.")


# --------------------------------------------------------------------------- DSML still works

def test_dsml_still_parses_and_is_not_double_counted():
    bar = "｜"
    dsml = (L + bar + "DSML" + bar + "function_calls" + R + "\n"
            + L + bar + "DSML" + bar + 'invoke name="bash"' + R + "\n"
            + L + bar + "DSML" + bar + 'parameter name="command" string="true"' + R
            + "ls" + L + S + bar + "DSML" + bar + "parameter" + R + "\n"
            + L + S + bar + "DSML" + bar + "invoke" + R + "\n"
            + L + S + bar + "DSML" + bar + "function_calls" + R)
    calls = _one(dsml)
    assert len(calls) == 1 and json.loads(calls[0]["arguments"]) == {"command": "ls"}


# --------------------------------------------------------------------------- streaming

def _frame(txt):
    return ("data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": txt}}]})).encode()


def _finish():
    return [b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}', b"data: [DONE]"]


def _run_stream(pieces):
    frames = [_frame(p) for p in pieces] + _finish()
    out = b"".join(tr.rescue_stream(iter(frames), TOOLS, "lines")).decode()
    calls = []
    # "lines" framing glues frames with no separator (end=b""); split on data:.
    for seg in out.split("data:"):
        seg = seg.strip()
        if not seg or seg == "[DONE]":
            continue
        try:
            obj = json.loads(seg)
        except ValueError:
            continue
        for tc in ((obj.get("choices") or [{}])[0].get("delta") or {}).get("tool_calls") or []:
            calls.append(tc.get("function", {}))
    return out, calls


def test_streamed_markup_becomes_tool_calls():
    markup = inv("bash", [("command", "ls -la")])
    out, calls = _run_stream([markup])
    assert calls and calls[-1].get("name") == "bash"
    assert "invoke" not in out                       # never leaked as text


def test_streamed_token_by_token_is_held_and_rescued():
    markup = inv("bash", [("command", "ls")])
    _out, calls = _run_stream(list(markup))           # one character per delta
    assert calls and calls[-1].get("name") == "bash"


def test_streamed_prose_then_markup():
    out, calls = _run_stream(["Let me look. ", inv("bash", [("command", "pwd")])])
    assert "Let me look." in out and calls and calls[-1].get("name") == "bash"


# --------------------------------------------------------------------------- the app fail-over

def test_app_treats_a_complete_typed_call_as_a_real_answer_not_a_nonanswer():
    data = {"choices": [{"message": {"role": "assistant",
                                     "content": inv("bash", [("command", "ls")])}}]}
    assert A._chat_json_nonanswer(data, has_tools=True, tools=TOOLS) is False
    # ...and it was promoted in place to a real tool call
    assert data["choices"][0]["message"].get("tool_calls")


def test_app_fails_over_on_an_orphan_closing_invoke():
    # a truncated / reordered call: a </invoke> with no parseable opening invoke
    orphan = L + S + "invoke" + R
    assert A._looks_like_text_tool_call("some text " + orphan)
    data = {"choices": [{"message": {"role": "assistant", "content": "working " + orphan}}]}
    assert A._chat_json_nonanswer(data, has_tools=True, tools=TOOLS) is True


def test_app_fails_over_on_an_orphan_parameter():
    orphan = L + 'parameter name="command"' + R + "ls" + L + S + "parameter" + R
    assert A._looks_like_text_tool_call(orphan)


def test_app_does_not_fail_over_on_prose_about_invoking():
    assert A._looks_like_text_tool_call("I'll invoke the tool and pass a parameter to it.") is False
