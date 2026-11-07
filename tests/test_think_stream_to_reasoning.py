"""A leaked <think> block at the start of a STREAMED chat reply goes to reasoning_content.

MEASURED 2026-11-07, a real OpenCode run through the hub: every step's reply opened with
"<think>...</think>" in the CONTENT and OpenCode printed it as the assistant's text. The
non-stream path already moved such a block (_strip_leaked_think_msg); every CLI streams.
_think_to_reasoning_sse wraps the chat stream after _proxy_sse: the judge still reads the
reply as written, the client gets the thinking as reasoning and the answer as text.
"""
import json

import pytest

import app as A

_ORIG_ON = A._think_sse_on


def _frame(delta, finish=None, extra=None):
    obj = {"id": "c1", "object": "chat.completion.chunk", "model": "m",
           "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if extra:
        obj.update(extra)
    return b"data: " + json.dumps(obj).encode("utf-8") + b"\n\n"


DONE = b"data: [DONE]\n\n"


def _reply(text, piece=5, finish="stop", calls=False):
    out = [_frame({"role": "assistant", "content": ""})]
    for i in range(0, len(text), piece):
        out.append(_frame({"content": text[i:i + piece]}))
    if calls:
        out.append(_frame({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                           "function": {"name": "bash",
                                                        "arguments": "{\"command\": \"ls\"}"}}]}))
    out.append(_frame({}, finish="tool_calls" if calls else finish))
    out.append(DONE)
    return out


def _run(chunks, prompt=None):
    return b"".join(A._think_to_reasoning_sse(iter(chunks), prompt))


def _rechunk(chunks, size):
    raw = b"".join(chunks)
    return [raw[i:i + size] for i in range(0, len(raw), size)]


def _read(body):
    """(content, reasoning, tool names, finish reasons) of an SSE chat body."""
    text, reasoning, tools, fins = [], [], [], []
    for block in body.decode("utf-8").split("\n\n"):
        block = block.strip()
        if not block.startswith("data:") or block.endswith("[DONE]"):
            continue
        obj = json.loads(block[5:].strip())
        for ch in obj.get("choices") or []:
            d = ch.get("delta") or {}
            if d.get("content"):
                text.append(d["content"])
            if d.get("reasoning_content"):
                reasoning.append(d["reasoning_content"])
            for tc in d.get("tool_calls") or []:
                tools.append((tc.get("function") or {}).get("name"))
            if ch.get("finish_reason"):
                fins.append(ch["finish_reason"])
    return "".join(text), "".join(reasoning), tools, fins


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    monkeypatch.setattr(A, "_think_sse_on", lambda: True)


# --------------------------------------------------------------------------- #
# The move
# --------------------------------------------------------------------------- #

def test_the_measured_reply_shows_the_answer_and_keeps_the_thinking_as_reasoning():
    body = _run(_reply("<think>\nThe instruction says use the write tool. Let me do it.\n"
                       "</think>\n\nDone: the file prints 5050."))
    text, reasoning, _tools, fins = _read(body)
    assert text == "Done: the file prints 5050."
    assert "<think" not in body.decode() and "</think" not in body.decode()
    assert reasoning == "The instruction says use the write tool. Let me do it."
    assert fins == ["stop"] and body.endswith(DONE)


@pytest.mark.parametrize("size", [1, 3, 7, 40, 10000])
def test_any_byte_split_gives_the_same_result(size):
    chunks = _reply("<think>plan the edit</think>The edit is done.", piece=4)
    whole = _read(_run(chunks))
    assert _read(_run(_rechunk(chunks, size))) == whole
    assert whole[0] == "The edit is done." and whole[1] == "plan the edit"


def test_thinking_then_a_tool_call_keeps_the_call():
    text, reasoning, tools, fins = _read(_run(_reply("<think>run ls first</think>", calls=True)))
    assert text == "" and reasoning == "run ls first"
    assert tools == ["bash"] and fins == ["tool_calls"]


def test_a_tag_split_across_frames_is_still_read():
    chunks = [_frame({"role": "assistant", "content": ""}), _frame({"content": "\n<th"}),
              _frame({"content": "ink>a"}), _frame({"content": "b</thi"}),
              _frame({"content": "nk> answer"}), _frame({}, finish="stop"), DONE]
    text, reasoning, _t, _f = _read(_run(chunks))
    assert text == "answer" and reasoning == "ab"


@pytest.mark.parametrize("open_tag,close_tag", [("<thinking>", "</thinking>"),
                                                 ("<THINK>", "</THINK>"),
                                                 ("<Think>", "</think>")])
def test_other_spellings_of_the_tag(open_tag, close_tag):
    text, reasoning, _t, _f = _read(_run(_reply(open_tag + "x y" + close_tag + "the answer")))
    assert text == "the answer" and reasoning == "x y"


def test_characters_that_change_length_when_lowercased_do_not_shift_the_cut():
    text, reasoning, _t, _f = _read(_run(_reply("<think>İİİ plan</think>İstanbul is the answer")))
    assert reasoning == "İİİ plan" and text == "İstanbul is the answer"


def test_answer_text_after_the_block_streams_unchanged():
    chunks = _reply("<think>t</think>first part. " + "and more words " * 20, piece=6)
    body = _run(chunks)
    # Every frame after the one that closed the block is the upstream's own bytes.
    tail = chunks[-8:]
    assert body.endswith(b"".join(tail))


# --------------------------------------------------------------------------- #
# What is left exactly as it came
# --------------------------------------------------------------------------- #

def test_a_reply_without_the_tag_is_byte_identical():
    chunks = _reply("Here is the plan: first read the file.")
    assert _run(chunks) == b"".join(chunks)


def test_a_reply_that_starts_with_other_markup_is_byte_identical():
    chunks = _reply("<p>hello</p> and the rest")
    assert _run(chunks) == b"".join(chunks)


def test_a_reply_that_ends_inside_the_block_is_sent_as_it_came():
    chunks = _reply("<think>still thinking when it stopped")
    assert _run(chunks) == b"".join(chunks)


def test_a_block_with_nothing_after_it_is_sent_as_it_came():
    chunks = _reply("<think>only thinking</think>\n  ")
    assert _run(chunks) == b"".join(chunks)


def test_a_tool_call_inside_an_unclosed_block_sends_everything_as_it_came():
    chunks = _reply("<think>unclosed", calls=True)
    assert _run(chunks) == b"".join(chunks)


def test_a_prompt_about_think_tags_is_left_alone():
    chunks = _reply("<think>example</think> is how the tags look")
    assert _run(chunks, prompt="how do <think> tags work in qwen?") == b"".join(chunks)


def test_the_flag_off_is_byte_identical(monkeypatch):
    monkeypatch.setattr(A, "_think_sse_on", lambda: False)
    chunks = _reply("<think>t</think>answer")
    assert _run(chunks) == b"".join(chunks)


def test_a_flag_read_that_raises_counts_as_off(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("config")
    monkeypatch.setattr(A.config, "get_flag", boom)
    assert _ORIG_ON() is False


def test_a_block_longer_than_the_hold_is_sent_as_it_came(monkeypatch):
    monkeypatch.setattr(A, "_THINK_SSE_HOLD_MAX", 50)
    chunks = _reply("<think>" + "x" * 200 + "</think>answer", piece=20)
    assert _run(chunks) == b"".join(chunks)


def test_odd_frames_before_any_content_pass_through():
    chunks = [b": keepalive\n\n", _frame({"role": "assistant"}),
              _frame({"content": "plain"}), _frame({}, finish="stop"), DONE]
    assert _run(chunks) == b"".join(chunks)


def test_a_usage_frame_inside_the_block_sends_everything_as_it_came():
    chunks = [_frame({"content": "<think>a"}),
              b'data: {"choices": [], "usage": {"prompt_tokens": 3}}\n\n',
              _frame({"content": "</think>b"}), DONE]
    assert _run(chunks) == b"".join(chunks)


def test_closing_the_client_closes_the_stream_below():
    closed = []

    def upstream():
        try:
            for c in _reply("<think>a</think>b c d e f g"):
                yield c
        finally:
            closed.append(True)
    gen = A._think_to_reasoning_sse(upstream(), None)
    next(gen)
    gen.close()
    assert closed == [True]


# --------------------------------------------------------------------------- #
# End to end through the real chat handler (the path OpenCode uses)
# --------------------------------------------------------------------------- #

TOOLS = [{"type": "function", "function": {"name": "bash", "description": "run a command",
                                           "parameters": {"type": "object",
                                                          "properties": {"command": {"type": "string"}},
                                                          "required": ["command"]}}}]


class _Resp:
    status_code = 200
    text = ""

    def __init__(self, chunks):
        self.headers = {}
        self._chunks = chunks

    def json(self):
        return {}

    def close(self):
        pass

    def iter_content(self, chunk_size=None):
        return iter(list(self._chunks))

    def iter_lines(self, decode_unicode=False):
        return iter([c.rstrip(b"\n") for c in self._chunks])


@pytest.fixture
def served(monkeypatch):
    for name in ("_record_chat_usage", "_save_perf_stats", "_act_pick", "_note_ttft",
                 "_note_provider_timeout", "_throttle_failed_hop", "_v1_observe"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)
    seen = []
    monkeypatch.setattr(A, "_record_stream_outcome", lambda pid, model, text, **k: seen.append(text))
    monkeypatch.setattr(A, "_record_outcome", lambda *a, **k: None)
    monkeypatch.setattr(A, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(A, "_model_block_reason", lambda pid, m: None)
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    monkeypatch.setattr(A, "_is_trivial_turn", lambda *a, **k: False)
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: 30)
    monkeypatch.setattr(A, "_stream_peek_timeout", lambda *a, **k: 5.0)
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("nvidia", "z-ai/glm-5.3", "hard"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("nvidia", "z-ai/glm-5.3")])
    box = {}
    monkeypatch.setattr(A, "_dispatch_chat", lambda pid, payload, stream: _Resp(box["chunks"]))
    return box, seen


def _post(tools=True):
    body = {"model": "auto", "stream": True,
            "messages": [{"role": "user", "content": "write sum.js that prints the sum of 1..100 and run it"}]}
    if tools:
        body["tools"] = TOOLS
    return A.app.test_client().post("/v1/chat/completions", json=body)


def test_e2e_opencode_sees_the_answer_and_the_judge_still_sees_the_whole_reply(served):
    box, seen = served
    box["chunks"] = _reply("<think>\nThe sum of 1..100 is 100*101/2.\n</think>\n\nThe sum is 5050.",
                           piece=6)
    r = _post()
    body = r.get_data()
    text, reasoning, _t, fins = _read(body)
    assert r.status_code == 200
    assert text == "The sum is 5050." and "100*101/2" in reasoning
    assert b"<think>" not in body and fins == ["stop"]
    assert seen and "<think>" in seen[-1]        # judged as the model wrote it


def test_e2e_thinking_then_a_tool_call_runs_the_call(served):
    box, _seen = served
    box["chunks"] = _reply("<think>I will run ls</think>", calls=True)
    text, reasoning, tools, fins = _read(_post().get_data())
    assert tools == ["bash"] and reasoning == "I will run ls" and text == ""
    assert fins == ["tool_calls"]


def test_e2e_a_plain_chat_stream_is_unchanged(served):
    box, _seen = served
    box["chunks"] = _reply("A plain answer with no thinking in it at all.")
    text, reasoning, _t, _f = _read(_post(tools=False).get_data())
    assert text == "A plain answer with no thinking in it at all." and reasoning == ""
