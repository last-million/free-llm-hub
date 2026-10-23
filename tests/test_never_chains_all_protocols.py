"""The "never chains" ledger is fed by EVERY way a CLI reaches the hub.

`test_never_chains.py` pins the ledger and the roles path. This file drives the
real chat, responses and messages handlers (what opencode / qwen / kimi, codex
and Claude Code speak), stream and non-stream, through the Flask test client
with a fake upstream, so a plain single-model tier (`auto`, `best`, a category)
feeds the same ledger the pipeline tiers do. Six paths, three facts each:

  * a text-only reply to a CONTINUATION (tools already ran this turn) counts;
    `_NEVER_CHAIN_MIN` of them in a row mark the pair;
  * a tool call on ANY turn -- an opening turn included -- clears the mark;
  * text on an OPENING turn never counts (it may just be a question).
"""
import json
import time

import pytest

import app as A

PAIR = ("p1", "m1")
TEXT = ("The command ran and printed AAA. Everything you asked for is finished, "
        "so there is nothing left to run right now.")
BASH_SCHEMA = {"type": "object", "properties": {"command": {"type": "string"}},
               "required": ["command"]}


# --------------------------------------------------------------------------- #
# Fake upstream: one pair that answers either TEXT or a native tool call
# --------------------------------------------------------------------------- #

def _frame(delta, finish=None):
    obj = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return b"data: " + json.dumps(obj).encode("utf-8")


class _Resp:
    status_code = 200
    text = ""

    def __init__(self, kind):
        self.kind = kind
        self.headers = {}
        self.closed = False

    def _message(self):
        if self.kind == "call":
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_77", "type": "function",
                "function": {"name": "bash", "arguments": "{\"command\": \"echo BBB\"}"}}]}
        return {"role": "assistant", "content": TEXT}

    def json(self):
        return {"id": "chatcmpl-x", "object": "chat.completion", "created": 1,
                "choices": [{"index": 0, "message": self._message(),
                             "finish_reason": "tool_calls" if self.kind == "call" else "stop"}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 12, "total_tokens": 52}}

    def close(self):
        self.closed = True

    def _frames(self, sep):
        if self.kind == "call":
            yield _frame({"role": "assistant", "tool_calls": [{
                "index": 0, "id": "call_77", "type": "function",
                "function": {"name": "bash", "arguments": "{\"command\": \"echo BBB\"}"}}]}) + sep
            yield _frame({}, "tool_calls") + sep
        else:
            yield _frame({"role": "assistant", "content": TEXT}) + sep
            yield _frame({}, "stop") + sep
        yield b"data: [DONE]" + sep

    def iter_lines(self, decode_unicode=False):
        return self._frames(b"")

    def iter_content(self, chunk_size=None):
        return self._frames(b"\n\n")


@pytest.fixture
def world(monkeypatch):
    """One pair, no disk ledgers; `reply["kind"]` picks what it answers."""
    for name in ("_record_chat_usage", "_save_perf_stats", "_act_pick", "_note_ttft",
                 "_note_provider_timeout", "_throttle_failed_hop"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)
    monkeypatch.setattr(A, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(A, "_model_block_reason", lambda pid, m: None)
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    monkeypatch.setattr(A, "_is_trivial_turn", lambda *a, **k: False)
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: 20)
    monkeypatch.setattr(A, "_stream_peek_timeout", lambda *a, **k: 1.0)
    monkeypatch.setattr(A, "_record_outcome", lambda *a, **k: None)
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("p1", "m1", "hard"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("p1", "m1")])
    reply = {"kind": "text"}
    monkeypatch.setattr(A, "_dispatch_chat", lambda pid, payload, stream: _Resp(reply["kind"]))
    A._chain_seen.clear()
    yield reply
    A._chain_seen.clear()


# --------------------------------------------------------------------------- #
# The three wire shapes. `continuation`: a tool call + its result are in history
# --------------------------------------------------------------------------- #

def _request(proto, stream, continuation):
    if proto == "chat":
        msgs = [{"role": "user", "content": "run echo twice, then tell me"}]
        if continuation:
            msgs += [{"role": "assistant", "content": None, "tool_calls": [{
                          "id": "c1", "type": "function",
                          "function": {"name": "bash", "arguments": "{\"command\": \"echo AAA\"}"}}]},
                     {"role": "tool", "tool_call_id": "c1", "content": "AAA"}]
        return "/v1/chat/completions", {
            "model": "auto", "stream": stream, "messages": msgs,
            "tools": [{"type": "function", "function": {
                "name": "bash", "description": "run", "parameters": BASH_SCHEMA}}]}
    if proto == "responses":
        items = [{"role": "user", "content": [{"type": "input_text",
                                                "text": "run echo twice, then tell me"}]}]
        if continuation:
            items += [{"type": "function_call", "call_id": "c1", "name": "bash",
                       "arguments": "{\"command\": \"echo AAA\"}"},
                      {"type": "function_call_output", "call_id": "c1", "output": "AAA"}]
        return "/v1/responses", {
            "model": "auto", "stream": stream, "input": items,
            "tools": [{"type": "function", "name": "bash", "description": "run",
                       "parameters": BASH_SCHEMA}]}
    msgs = [{"role": "user", "content": "run echo twice, then tell me"}]
    if continuation:
        msgs += [{"role": "assistant", "content": [{"type": "tool_use", "id": "c1",
                                                     "name": "bash",
                                                     "input": {"command": "echo AAA"}}]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1",
                                                "content": "AAA"}]}]
    return "/v1/messages", {
        "model": "auto", "max_tokens": 400, "stream": stream, "messages": msgs,
        "tools": [{"name": "bash", "description": "run", "input_schema": BASH_SCHEMA}]}


def _send(proto, stream, continuation):
    path, body = _request(proto, stream, continuation)
    r = A.app.test_client().post(path, json=body)
    r.get_data()                                   # drain a stream: its end files the outcome
    assert r.status_code == 200, (proto, stream, r.get_data()[:200])
    return r


def _text_seen():
    rec = A._chain_seen.get(PAIR)
    return rec["text"] if rec else 0


CASES = [(p, s) for p in ("chat", "responses", "messages") for s in (False, True)]
IDS = ["%s-%s" % (p, "stream" if s else "json") for p, s in CASES]


@pytest.mark.parametrize("proto,stream", CASES, ids=IDS)
def test_text_only_continuations_mark_the_pair(world, proto, stream):
    world["kind"] = "text"
    for i in range(A._NEVER_CHAIN_MIN - 1):
        _send(proto, stream, continuation=True)
    assert _text_seen() == A._NEVER_CHAIN_MIN - 1
    assert not A._never_chains(*PAIR)
    _send(proto, stream, continuation=True)
    assert A._never_chains(*PAIR)
    assert A._tool_turn_sick(*PAIR)


@pytest.mark.parametrize("proto,stream", CASES, ids=IDS)
def test_a_tool_call_on_an_opening_turn_clears_the_mark(world, proto, stream):
    A._chain_seen[PAIR] = {"text": A._NEVER_CHAIN_MIN + 2, "last": time.time()}
    assert A._never_chains(*PAIR)
    world["kind"] = "call"
    _send(proto, stream, continuation=False)         # an OPENING turn
    assert not A._never_chains(*PAIR)
    assert PAIR not in A._chain_seen


@pytest.mark.parametrize("proto,stream", CASES, ids=IDS)
def test_a_tool_call_on_a_continuation_clears_the_mark(world, proto, stream):
    A._chain_seen[PAIR] = {"text": A._NEVER_CHAIN_MIN + 2, "last": time.time()}
    world["kind"] = "call"
    _send(proto, stream, continuation=True)
    assert not A._never_chains(*PAIR)


@pytest.mark.parametrize("proto,stream", CASES, ids=IDS)
def test_text_on_an_opening_turn_never_counts(world, proto, stream):
    world["kind"] = "text"
    for _ in range(A._NEVER_CHAIN_MIN + 2):
        _send(proto, stream, continuation=False)
    assert _text_seen() == 0
    assert not A._never_chains(*PAIR)


def test_a_healthy_pair_doing_simple_call_then_answer_tasks_is_never_flagged(world):
    # call on the opening turn, text on the continuation, over and over: every
    # opening call clears the run, so a pair that DOES call is never marked.
    for _ in range(A._NEVER_CHAIN_MIN + 3):
        world["kind"] = "call"
        _send("chat", False, continuation=False)
        world["kind"] = "text"
        _send("chat", False, continuation=True)
    assert not A._never_chains(*PAIR)
    assert _text_seen() <= 1
