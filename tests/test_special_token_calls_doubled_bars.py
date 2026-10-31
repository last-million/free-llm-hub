"""A tool call typed with DeepSeek-style special tokens, spelled with DOUBLED bars, is rescued.

MEASURED 2026-10-31 (opencode, g4f relay copies of DeepSeek): a call written into the reply
text used the template tokens with TWO bars on each side and a slash on the closing ones --
the same tokens tool_rescue already knows, spelled one bar wider. Only the one-bar spelling
parsed, so has_model_markup() was False, the call was served as prose, nothing ran and the
turn ended. Four of the seven replies that stopped that session ended this way.

Every token here is built from chr() pieces, so this file holds no literal template token.
"""
import json

import pytest

import app as A
import tool_rescue as tr

BAR = chr(0xFF5C)
SP = chr(0x2581)


def tok(words, bars=2, slash=False):
    b = BAR * bars
    return "<" + ("/" if slash else "") + b + SP.join(words) + b + ">"


TOOLS = [{"type": "function", "function": {"name": "bash", "parameters": {
    "type": "object", "properties": {"command": {"type": "string"},
                                     "timeout": {"type": "number"}}}}}]
NAMES = ["bash"]
SCHEMAS = tr.tool_schemas(TOOLS)

SPELLINGS = [(1, False), (1, True), (2, False), (2, True), (3, True)]


def v31(name, args, bars=2, slash=True):
    """NAME<sep>{json} -- the V3.1 / typed-by-a-relay shape."""
    return (tok(["tool", "calls", "begin"], bars)
            + tok(["tool", "call", "begin"], bars) + name
            + tok(["tool", "sep"], bars) + json.dumps(args)
            + tok(["tool", "call", "end"], bars, slash)
            + tok(["tool", "calls", "end"], bars, slash))


def v3(name, args, bars=2, slash=True):
    """function<sep>NAME + a fenced json block -- the V3 / R1 shape."""
    return (tok(["tool", "calls", "begin"], bars)
            + tok(["tool", "call", "begin"], bars) + "function"
            + tok(["tool", "sep"], bars) + name + "\n```json\n" + json.dumps(args) + "\n```"
            + tok(["tool", "call", "end"], bars, slash)
            + tok(["tool", "calls", "end"], bars, slash))


def _one(text):
    return tr.parse(text, allowed_names=NAMES, schemas=SCHEMAS)


# --------------------------------------------------------------------------- parse

@pytest.mark.parametrize("bars,slash", SPELLINGS)
def test_the_call_is_rescued_in_every_spelling(bars, slash):
    calls = _one("Checking.\n" + v31("bash", {"command": "echo hi"}, bars, slash))
    assert len(calls) == 1 and calls[0]["name"] == "bash"
    assert json.loads(calls[0]["arguments"]) == {"command": "echo hi"}


@pytest.mark.parametrize("bars,slash", SPELLINGS)
def test_the_v3_shape_is_rescued_in_every_spelling(bars, slash):
    calls = _one(v3("bash", {"command": "ls -la"}, bars, slash))
    assert len(calls) == 1 and json.loads(calls[0]["arguments"]) == {"command": "ls -la"}


def test_two_calls_in_one_block():
    text = (tok(["tool", "calls", "begin"])
            + tok(["tool", "call", "begin"]) + "bash" + tok(["tool", "sep"])
            + '{"command": "ls"}' + tok(["tool", "call", "end"], 2, True)
            + tok(["tool", "call", "begin"]) + "bash" + tok(["tool", "sep"])
            + '{"command": "pwd"}' + tok(["tool", "call", "end"], 2, True)
            + tok(["tool", "calls", "end"], 2, True))
    assert [json.loads(c["arguments"]) for c in _one(text)] == [{"command": "ls"},
                                                                 {"command": "pwd"}]


def test_a_name_not_offered_is_dropped():
    assert _one(v31("rm_rf_everything", {"path": "/"})) == []


def test_a_call_with_a_json_argument_keeps_its_type():
    calls = _one(v31("bash", {"command": "sleep 1", "timeout": 5000}))
    assert json.loads(calls[0]["arguments"]) == {"command": "sleep 1", "timeout": 5000}


# --------------------------------------------------------------------------- markup detection

@pytest.mark.parametrize("bars,slash", SPELLINGS)
def test_the_opener_is_seen(bars, slash):
    assert tr.has_model_markup("Let me run it.\n" + v31("bash", {"command": "ls"}, bars, slash))


def test_a_fenced_or_inline_mention_is_not_a_call():
    opener = tok(["tool", "calls", "begin"])
    assert tr.has_model_markup("The token looks like `" + opener + "` in the template.") is False
    assert tr.has_model_markup("```\n" + opener + "\n```") is False


def test_an_ordinary_double_bar_is_not_markup():
    assert tr.has_model_markup("a || b and c " + BAR * 2 + " d") is False
    assert tr.has_model_markup("<" + BAR * 2 + "im_end" + BAR * 2 + ">") is False


@pytest.mark.parametrize("bars,slash", SPELLINGS)
def test_strip_leaves_no_token_residue(bars, slash):
    text = "Running it now. " + v31("bash", {"command": "ls"}, bars, slash) + " done."
    left = tr.strip_model_markup(text)
    assert BAR not in left and SP not in left
    assert left.startswith("Running it now.") and left.endswith("done.")


def test_a_closing_token_with_a_slash_is_part_of_the_markup():
    # the one-bar spelling used to leave "></|tool_calls_end|>" behind
    left = tr.strip_model_markup("ok " + v31("bash", {"command": "ls"}, 1, True))
    assert left.strip() == "ok"


# --------------------------------------------------------------------------- unfinished calls

def test_a_truncated_call_is_markup_with_no_call():
    cut = (tok(["tool", "calls", "begin"]) + tok(["tool", "call", "begin"]) + "bash"
           + tok(["tool", "sep"]) + '{"command": "ec')
    assert tr.has_model_markup(cut) is True
    assert _one(cut) == []


def _chat(content):
    return {"choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}]}


def test_the_non_stream_path_promotes_a_complete_call():
    data = _chat("Doing it.\n" + v31("bash", {"command": "ls"}))
    assert A._chat_json_nonanswer(data, True, TOOLS) is False
    msg = data["choices"][0]["message"]
    assert msg["tool_calls"][0]["function"]["name"] == "bash"
    assert BAR not in (msg.get("content") or "")


def test_the_non_stream_path_fails_over_on_a_truncated_call():
    cut = (tok(["tool", "calls", "begin"]) + tok(["tool", "call", "begin"]) + "bash"
           + tok(["tool", "sep"]) + '{"command": "ec')
    assert A._chat_json_nonanswer(_chat(cut), True, TOOLS) is True


def test_plain_chat_may_talk_about_the_tokens():
    # tools were not offered: explaining a template is a fine answer
    text = "The template writes " + tok(["tool", "calls", "begin"], 1) + " first."
    assert A._chat_json_nonanswer(_chat("`" + text + "`"), False, None) is False


# --------------------------------------------------------------------------- streaming

def _frame(txt):
    return ("data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": txt}}]})).encode()


def _finish():
    return [b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}', b"data: [DONE]"]


def _run_stream(pieces):
    frames = [_frame(p) for p in pieces] + _finish()
    out = b"".join(tr.rescue_stream(iter(frames), TOOLS, "lines")).decode()
    calls, text = [], ""
    for seg in out.split("data:"):
        seg = seg.strip()
        if not seg or seg == "[DONE]":
            continue
        try:
            obj = json.loads(seg)
        except ValueError:
            continue
        delta = (obj.get("choices") or [{}])[0].get("delta") or {}
        text += delta.get("content") or ""
        for tc in delta.get("tool_calls") or []:
            calls.append(tc.get("function", {}))
    return out, calls, text


def _split(text, n):
    return [text[i:i + n] for i in range(0, len(text), n)]


@pytest.mark.parametrize("n", [1, 2, 3, 7, 1000])
def test_a_streamed_call_becomes_a_tool_call_whatever_the_chunking(n):
    reply = "Looking.\n" + v31("bash", {"command": "ls -la"})
    out, calls, text = _run_stream(_split(reply, n))
    assert len(calls) == 1 and calls[0]["name"] == "bash"
    assert json.loads(calls[0]["arguments"]) == {"command": "ls -la"}
    assert BAR not in text and SP not in text, "no token may leak into the visible text"
    assert text.strip() == "Looking."


def test_a_streamed_truncated_call_ends_in_the_retryable_error_not_in_text():
    cut = (tok(["tool", "calls", "begin"]) + tok(["tool", "call", "begin"]) + "bash"
           + tok(["tool", "sep"]) + '{"command": "ec')
    out, calls, text = _run_stream(_split(cut, 3))
    assert calls == []
    assert BAR not in text
    assert "unparseable_tool_markup" in out


@pytest.mark.parametrize("partial", [
    "<" + BAR,
    "<" + BAR * 2,
    "<" + BAR * 2 + "tool" + SP + "calls" + SP + "be",
    "<" + BAR * 2 + "tool" + SP + "call" + SP + "begin" + BAR,
])
def test_a_half_typed_opener_is_held_not_emitted(partial):
    assert tr._may_open_marker("Sure. " + partial) is True


def test_ordinary_text_is_not_held():
    assert tr._may_open_marker("if a < b then continue") is False
    assert tr._may_open_marker("done") is False
