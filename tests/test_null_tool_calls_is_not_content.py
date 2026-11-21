"""An empty completion that carries `"tool_calls": null` is EMPTY, not an answer (2026-11-21).

MEASURED on the owner's OpenCode session (coding-max): three turns in a row, each sent as
"yes please ..." and each ended with 0 output tokens, no text and no tool call. The hub's
Activity row said ok / 200 every time: dahl answered HTTP 429, then the next hop,
g4f/Perplexity:claude3opus, was served. Probed directly (3 keys, a trivial "reply ok" prompt):
that relay answers HTTP 200 with ONE chunk

    {"choices":[{"delta":{"role":"assistant","content":"","reasoning":null,"tool_calls":null},
                 "finish_reason":"stop"}],"usage":{"completion_tokens":0,...},"conversation":{...}}

then [DONE]. _STREAM_CONTENT_RE listed a bare `"tool_calls"` as content, so the peek took the
frame for an answer; the frame is also long (the relay's metadata), so it passed the
_PEEK_JUDGE_CHARS gate at once, was judged "content" with no text to read and was COMMITTED.
The client got an empty stream, ended its turn, and nothing was walked on to another model.
The fix: a null / empty tool_calls (the same guard _STREAM_TOOLCALL_RE already had) is not content.
"""
import json

import app as A

TOOLS = [{"type": "function", "function": {"name": "bash", "parameters": {"type": "object"}}}]
META = {"conversation": {"frontend_uid": "u" * 36, "frontend_context_uuid": "c" * 36,
                         "visitor_id": "v" * 36, "extra": "x" * 700}}


def _empty_frame():
    d = {"id": "chatcmpl-1", "object": "chat.completion.chunk", "model": "claude3opus",
         "provider": "Perplexity",
         "choices": [{"index": 0, "delta": {"role": "assistant", "content": "", "reasoning": None,
                                            "tool_calls": None}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 12, "completion_tokens": 0, "total_tokens": 12}}
    d.update(META)
    return ("data: " + json.dumps(d)).encode()


def _check(tools=True):
    return {"tools_offered": tools, "tools": TOOLS if tools else None,
            "last_prompt": "yes please", "prompt_text": "yes please", "relay": True}


def _peek(frames, check=None):
    status, _buf = A._peek_until_content(iter(frames), 5, check=check if check is not None else _check())
    return status


def test_the_relays_empty_completion_is_empty_on_a_tool_turn():
    assert _peek([_empty_frame(), b"data: [DONE]"]) == "empty"


def test_and_on_a_tool_free_turn():
    assert _peek([_empty_frame(), b"data: [DONE]"], check=_check(False)) == "empty"


def test_and_without_any_check_context():
    status, _b = A._peek_until_content(iter([_empty_frame(), b"data: [DONE]"]), 5)
    assert status == "empty"


def test_a_real_answer_after_the_empty_preamble_is_still_an_answer():
    real = ("data: " + json.dumps({"choices": [{"index": 0, "delta": {
        "content": "Both tests pass now. I fixed the loop bound in parser.py so the last token "
                   "is kept, and nothing else needed to change."}}]})).encode()
    stop = b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}'
    pre = ("data: " + json.dumps({"choices": [{"index": 0, "delta": {
        "role": "assistant", "content": "", "tool_calls": None}}]})).encode()
    assert _peek([pre, real, stop, b"data: [DONE]"]) == "content"


def test_a_real_tool_call_still_commits():
    call = (b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"c1","function":'
            b'{"name":"bash","arguments":"{}"}}]}}]}')
    assert _peek([call, b"data: [DONE]"]) == "content"


def test_the_content_regex_ignores_null_and_empty_tool_calls_but_not_real_ones():
    rx = A._STREAM_CONTENT_RE
    for dead in (b'"tool_calls":null', b'"tool_calls": null', b'"tool_calls":[]', b'"tool_calls": [ ]',
                 b'"tool_calls":""', b'"tool_calls":{}', b'"content":""'):
        assert not rx.search(dead), dead
    for live in (b'"tool_calls":[{"index":0}]', b'"tool_calls": [{"id":"c1"}]', b'"content":"hi"',
                 b'output_text.delta', b'content_block_delta'):
        assert rx.search(live), live


def test_a_bare_empty_frame_is_not_something_the_client_can_use():
    # the deadline guard's "is this stream still delivering?" read the same bare tool_calls
    bare = b'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"","tool_calls":null}}]}'
    assert not A._stream_item_delivers(bare)
    real = (b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"c1",'
            b'"function":{"name":"bash","arguments":"{}"}}]}}]}')
    assert A._stream_item_delivers(real)
