"""An upstream stream that dies mid-answer is never handed to a CLI as a
finished answer (BROKEN UPSTREAM STREAMS in app.py).

MEASURED 2026-10-03 (hub.log, Multi worker, opencode session fe4bbd96): after
7 minutes of real work the provider's stream died ("SSE passthrough error:
Response ended prematurely"). The passthrough appended `data: [DONE]` -- to
opencode that is a COMPLETE answer: the turn ended with no message and the
phase was written off as "opencode produced no reply". Nothing was filed
against the pair, so the next request could pick it again.

Each protocol now ends a broken stream with the signal its clients retry on:
  chat      -> one `data: {"error": {..., "code": "ECONNRESET"}}` frame, no
               [DONE] (opencode fromError/retry.ts, qwen-code transport class)
  responses -> `response.failed` with an unknown (retryable) code, no
               response.completed and no output_item.done (codex
               CodexErr::Stream, stream_max_retries)
  messages  -> the body ends cleanly with NO further event (Claude Code's
               documented "dropped connection" retry, even after text started)
and the hop is filed failed: _record_outcome(False) + the recent-failure
ledger (_recent_hop_failure), which _build_chain reads to put the pair at the
tail of the next chain. An upstream that sent its finish_reason before dying
delivered a complete answer and keeps the clean end.
"""
import json
import re

import pytest
import requests
from flask import Response

import app as A


PREMATURE = "Response ended prematurely"


def _chunk(delta, fin=None):
    return ("data: " + json.dumps({
        "id": "chatcmpl-x", "object": "chat.completion.chunk", "created": 1727400000,
        "model": "m", "choices": [{"index": 0, "delta": delta, "finish_reason": fin}]})
    ).encode()


ANSWER = ("A hash map stores each key in a bucket chosen by its hash, and when two "
          "keys land in the same bucket it either chains them in a small list or "
          "probes for the next free slot, so lookups stay close to constant time "
          "while the table is kept below its load factor by resizing it.")


def _words(text=ANSWER):
    return [_chunk({"content": t}) for t in re.findall(r"\S+\s*", text)]


def _tool_frame():
    return _chunk({"role": "assistant", "tool_calls": [{
        "index": 0, "id": "call_1", "type": "function",
        "function": {"name": "bash", "arguments": '{"command": "ls"}'}}]})


def _dies(frames, exc=None, sep=b""):
    """The upstream: `frames`, then the connection breaks."""
    for f in frames:
        yield f + sep
    raise exc if exc is not None else requests.exceptions.ChunkedEncodingError(PREMATURE)


class _Resp:
    def __init__(self, frames=(), exc=None):
        self.status_code = 200
        self.headers = {}
        self.text = ""
        self._frames = list(frames)
        self._exc = exc
        self.closed = False

    def close(self):
        self.closed = True

    def json(self):
        return {}

    def iter_content(self, chunk_size=None):
        return _dies(self._frames, self._exc, sep=b"\n\n")

    def iter_lines(self, decode_unicode=False):
        return _dies(self._frames, self._exc)


def _data(body):
    out = []
    for ln in body.splitlines():
        if ln.startswith("data:"):
            try:
                out.append(json.loads(ln[5:]))
            except ValueError:
                pass
    return out


def _types(body):
    return [e.get("type") for e in _data(body)]


@pytest.fixture
def filed(monkeypatch):
    """Captures _record_outcome / _record_stream_outcome; the recent-failure
    ledger stays REAL (in memory, cleared per test by conftest)."""
    seen = {"outcome": [], "stream": []}
    monkeypatch.setattr(A, "_record_outcome",
                        lambda p, m, ok, junk=False, **k: seen["outcome"].append((p, m, ok)))
    monkeypatch.setattr(A, "_record_stream_outcome",
                        lambda p, m, text, **k: seen["stream"].append((p, m, k.get("finish_reason"))))
    monkeypatch.setattr(A.usage_history, "record", lambda *a, **k: None)
    monkeypatch.setattr(A, "_save_perf_stats", lambda *a, **k: None)
    return seen


def _assert_filed_broken(filed, pid="p1", model="m1", kind="conn"):
    assert (pid, model, False) in filed["outcome"]
    assert filed["outcome"].count((pid, model, False)) == 1, "filed exactly once"
    assert filed["stream"] == [], "a broken stream is not judged as a finished answer"
    assert A._recent_hop_failure(pid, model) == kind, \
        "the NEXT chain must put this pair at the tail (_RECENT_FAIL_TTL)"


def _assert_not_filed_broken(filed, pid="p1", model="m1"):
    assert (pid, model, False) not in filed["outcome"]
    assert A._recent_hop_failure(pid, model) is None


# --------------------------------------------------------------------------- #
# /v1/chat/completions passthrough (_proxy_sse)
# --------------------------------------------------------------------------- #

def _relay(it, **kw):
    return b"".join(A._proxy_sse(_Resp(), it, hop_pid="p1", hop_model="m1", **kw)).decode()


def test_chat_death_mid_answer_ends_with_a_retryable_error_not_done(filed):
    body = _relay(_dies(_words()[:6], sep=b"\n\n"))
    assert "[DONE]" not in body, "[DONE] tells opencode the answer is complete"
    last = _data(body)[-1]
    assert "choices" not in last, "the AI SDK reads a frame with choices as a chunk"
    assert last["error"]["code"] == "ECONNRESET"            # opencode: isRetryable
    assert isinstance(last["error"]["message"], str) and last["error"]["message"]
    assert PREMATURE in last["error"]["message"]
    assert re.search(r"unavailable|connection lost|server_error",
                     json.dumps(last).lower())               # retry.ts patterns too
    assert body.endswith("\n\n")
    _assert_filed_broken(filed)


def test_chat_death_after_the_finish_reason_is_still_a_clean_completion(filed):
    frames = _words()[:3] + [_chunk({}, "stop")]
    body = _relay(_dies(frames, sep=b"\n\n"))
    assert body.rstrip().endswith("data: [DONE]")
    assert not any("error" in e for e in _data(body))
    assert filed["stream"] == [("p1", "m1", "stop")], "judged like any finished stream"
    _assert_not_filed_broken(filed)


def test_chat_upstream_done_then_death_is_not_broken(filed):
    body = _relay(_dies(_words()[:3] + [b"data: [DONE]"], sep=b"\n\n"))
    assert body.count("[DONE]") == 1
    assert not any("error" in e for e in _data(body))
    _assert_not_filed_broken(filed)


def test_chat_death_mid_line_closes_the_partial_line_first(filed):
    body = _relay(_dies([_words()[0] + b"\n\n", b'data: {"choices":[{"delta":{"con']))
    assert '\n\ndata: {"error"' in body, "the error frame must parse on its own"
    assert "[DONE]" not in body
    _assert_filed_broken(filed)


def test_chat_read_timeout_is_filed_as_a_stall(filed):
    exc = requests.exceptions.ConnectionError(
        "HTTPSConnectionPool(host='x', port=443): Read timed out.")
    body = _relay(_dies(_words()[:4], exc=exc, sep=b"\n\n"))
    assert "[DONE]" not in body
    _assert_filed_broken(filed, kind="timeout")


def test_chat_gate_cut_is_not_filed_twice(filed):
    class _Cut:
        cut = True
    _relay(_dies(_words()[:4], sep=b"\n\n"), answer_gate=_Cut())
    assert filed["outcome"] == [], "the gate filed its own failure already"


# --------------------------------------------------------------------------- #
# /v1/responses (_responses_stream)
# --------------------------------------------------------------------------- #

def _responses(lines, **kw):
    return b"".join(A._responses_stream(
        _Resp(), "auto", line_iter=lines, hop_pid="p1", hop_model="m1", **kw)).decode()


def test_responses_death_mid_answer_is_response_failed_not_completed(filed):
    body = _responses(_dies(_words()[:6]))
    types = _types(body)
    assert "response.completed" not in types and "response.incomplete" not in types
    assert types[-1] == "response.failed"
    assert "response.output_item.done" not in types, \
        "codex records an output_item.done into history: the retry would duplicate it"
    assert "response.output_text.delta" in types          # what streamed stays streamed
    failed = _data(body)[-1]["response"]
    assert failed["status"] == "failed"
    code = failed["error"]["code"]
    assert code not in ("context_length_exceeded", "insufficient_quota", "usage_not_included",
                        "invalid_prompt", "server_is_overloaded", "rate_limit_exceeded")
    assert PREMATURE in failed["error"]["message"]
    _assert_filed_broken(filed)


def test_responses_death_with_a_tool_call_half_built_is_failed(filed):
    body = _responses(_dies([_tool_frame()]), tools_offered=True)
    types = _types(body)
    assert types[-1] == "response.failed"
    assert "response.function_call_arguments.done" not in types
    assert "response.output_item.done" not in types
    _assert_filed_broken(filed)


def test_responses_death_after_the_finish_reason_still_completes(filed):
    body = _responses(_dies(_words()[:3] + [_chunk({}, "stop")]))
    types = _types(body)
    assert types[-1] == "response.completed"
    assert "response.failed" not in types
    assert "response.output_item.done" in types
    assert filed["stream"] == [("p1", "m1", "stop")]
    _assert_not_filed_broken(filed)


def test_responses_error_object_before_the_finish_is_a_broken_stream(filed):
    err = b'data: {"error": {"message": "upstream overloaded", "code": 503}}'
    body = _responses(iter(_words()[:3] + [err]))
    assert _types(body)[-1] == "response.failed"
    assert "upstream overloaded" in _data(body)[-1]["response"]["error"]["message"]
    _assert_filed_broken(filed)


def test_responses_error_object_after_the_finish_keeps_the_answer(filed):
    err = b'data: {"error": {"message": "late noise"}}'
    body = _responses(iter(_words()[:3] + [_chunk({}, "stop"), err]))
    assert _types(body)[-1] == "response.completed"
    assert A._recent_hop_failure("p1", "m1") is None


# --------------------------------------------------------------------------- #
# /v1/messages (_anthropic_stream)
# --------------------------------------------------------------------------- #

def _messages(lines, **kw):
    return b"".join(A._anthropic_stream(
        _Resp(), "claude-x", 10, line_iter=lines, hop_pid="p1", hop_model="m1", **kw)).decode()


def test_messages_death_mid_answer_ends_the_body_with_no_further_event(filed):
    body = _messages(_dies(_words()[:6]))
    types = _types(body)
    assert "content_block_delta" in types
    for never in ("message_stop", "message_delta", "content_block_stop", "error"):
        assert never not in types, (never, types)
    assert types[-1] == "content_block_delta"
    _assert_filed_broken(filed)


def test_messages_death_with_an_open_tool_block_never_completes_it(filed):
    body = _messages(_dies(_words()[:2] + [_tool_frame()]), tools_offered=True)
    evs = _data(body)
    starts = {e["index"]: e["content_block"]["type"] for e in evs
              if e.get("type") == "content_block_start"}
    stops = [e["index"] for e in evs if e.get("type") == "content_block_stop"]
    tool_idx = [i for i, t in starts.items() if t == "tool_use"]
    assert tool_idx and not set(tool_idx) & set(stops), \
        "a stopped tool block is a COMPLETED tool call Claude Code would run"
    assert len(stops) == len(set(stops)), "never stopped twice"
    assert "message_stop" not in _types(body) and "message_delta" not in _types(body)
    _assert_filed_broken(filed)


def test_messages_death_after_the_finish_reason_still_ends_with_message_stop(filed):
    body = _messages(_dies(_words()[:3] + [_chunk({}, "stop")]))
    types = _types(body)
    assert types[-1] == "message_stop" and types.count("message_delta") == 1
    assert filed["stream"] == [("p1", "m1", "stop")]
    _assert_not_filed_broken(filed)


def test_messages_error_object_before_the_finish_is_a_broken_stream(filed):
    err = b'data: {"error": {"message": "upstream overloaded"}}'
    body = _messages(iter(_words()[:3] + [err]))
    assert "message_stop" not in _types(body)
    _assert_filed_broken(filed)


# --------------------------------------------------------------------------- #
# Through the real routes (peek, gate, tool wrappers, usage rewrite)
# --------------------------------------------------------------------------- #

@pytest.fixture
def hub(monkeypatch, filed):
    for name in ("_record_chat_usage", "_act_pick", "_note_ttft", "_note_provider_timeout"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)
    monkeypatch.setattr(A, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(A, "_model_block_reason", lambda pid, m: None)
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("p1", "m1", "hard"))
    monkeypatch.setattr(A, "_resolve_model", lambda m: ("p1", "m1"))
    monkeypatch.setattr(A, "_is_trivial_turn", lambda *a, **k: False)
    monkeypatch.setattr(A, "_throttle_failed_hop", lambda *a, **k: None)
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("p1", "m1"), ("p2", "m2")])
    state = {"frames": [], "calls": []}

    def dispatch(pid, payload, stream):
        state["calls"].append(pid)
        return _Resp(state["frames"])
    monkeypatch.setattr(A, "_dispatch_chat", dispatch)
    client = A.app.test_client()
    client.state = state
    return client


TOOLS_OAI = [{"type": "function", "function": {"name": "bash", "parameters": {
    "type": "object", "properties": {"command": {"type": "string"}}}}}]


def _post(client, proto, tools):
    ask = "Explain how a hash map resolves collisions."
    if proto == "messages":
        body = {"model": "auto", "max_tokens": 4096, "stream": True,
                "messages": [{"role": "user", "content": ask}]}
        if tools:
            body["tools"] = [{"name": "bash", "input_schema": TOOLS_OAI[0]["function"]["parameters"]}]
        return client.post("/v1/messages", json=body)
    if proto == "responses":
        body = {"model": "auto", "stream": True, "input": ask}
        if tools:
            body["tools"] = [{"type": "function", "name": "bash",
                              "parameters": TOOLS_OAI[0]["function"]["parameters"]}]
        return client.post("/v1/responses", json=body)
    body = {"model": "auto", "stream": True, "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": ask}]}
    if tools:
        body["tools"] = TOOLS_OAI
    return client.post("/v1/chat/completions", json=body)


@pytest.mark.parametrize("tools", [False, True])
@pytest.mark.parametrize("proto", ["chat", "responses", "messages"])
def test_a_committed_stream_that_dies_never_reads_as_complete(hub, filed, proto, tools):
    hub.state["frames"] = ([_tool_frame()] if tools else []) + _words()
    body = _post(hub, proto, tools).get_data(as_text=True)
    assert hub.state["calls"] == ["p1"], "committed: the stream is the client's now"
    types = _types(body)
    if proto == "chat":
        assert "[DONE]" not in body
        assert _data(body)[-1]["error"]["code"] == "ECONNRESET"
        assert not any(e.get("usage") for e in _data(body)), "no completion usage frame"
    elif proto == "responses":
        assert types[-1] == "response.failed"
        assert "response.completed" not in types and "response.output_item.done" not in types
    else:
        assert "message_stop" not in types and "message_delta" not in types
        assert "error" not in types
    _assert_filed_broken(filed)


@pytest.mark.parametrize("proto", ["chat", "responses", "messages"])
def test_a_finished_stream_that_dies_after_its_finish_is_complete(hub, filed, proto):
    hub.state["frames"] = _words() + [_chunk({}, "stop")]
    body = _post(hub, proto, False).get_data(as_text=True)
    types = _types(body)
    if proto == "chat":
        assert body.rstrip().endswith("data: [DONE]")
        assert not any("error" in e for e in _data(body))
    elif proto == "responses":
        assert types[-1] == "response.completed"
    else:
        assert types[-1] == "message_stop"
    _assert_not_filed_broken(filed)


# --------------------------------------------------------------------------- #
# Surfaces built on the chat stream, and Puter
# --------------------------------------------------------------------------- #

def test_sse_deltas_raises_on_the_broken_stream_frame():
    exc = requests.exceptions.ChunkedEncodingError(PREMATURE)
    stream = Response([_words()[0] + b"\n\n", A._chat_broken_frame(exc)],
                      mimetype="text/event-stream")
    got = []
    with pytest.raises(A._UpstreamStreamError):
        for item in A._sse_deltas(stream):
            got.append(item)
    assert got and got[0][0]


def test_sse_deltas_keeps_an_answer_that_finished_before_an_error_frame():
    late = b'data: {"error": {"message": "late noise"}}\n\n'
    stream = Response([_words()[0] + b"\n\n", _chunk({}, "stop") + b"\n\n", late],
                      mimetype="text/event-stream")
    got = list(A._sse_deltas(stream))
    assert [g[2] for g in got] == [None, "stop"]


def test_legacy_completions_stream_ends_with_the_error_not_done(monkeypatch):
    exc = requests.exceptions.ChunkedEncodingError(PREMATURE)
    stream = Response([_words()[0] + b"\n\n", A._chat_broken_frame(exc)],
                      mimetype="text/event-stream")
    monkeypatch.setattr(A, "_call_router", lambda body: (stream, None, 200))
    body = A.app.test_client().post("/v1/completions", json={
        "model": "auto", "prompt": "hi", "stream": True}).get_data(as_text=True)
    assert "[DONE]" not in body
    assert _data(body)[-1]["error"]["code"] == "ECONNRESET"


class _Ndjson:
    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self, decode_unicode=False):
        return iter(self._lines)

    def close(self):
        pass


def test_a_puter_error_event_is_a_broken_stream_not_a_stop(filed):
    up = _Ndjson([json.dumps({"type": "text", "text": ANSWER}).encode(),
                  json.dumps({"type": "error", "error": {"message": "driver boom"}}).encode()])
    resp = A._PuterStreamResponse.from_ndjson(up, "m1")
    body = b"".join(A._responses_stream(resp, "auto", hop_pid="p1",
                                        hop_model="m1")).decode()
    assert _types(body)[-1] == "response.failed"
    assert "driver boom" in _data(body)[-1]["response"]["error"]["message"]
    _assert_filed_broken(filed)
