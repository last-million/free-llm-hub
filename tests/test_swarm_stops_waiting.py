"""The fan-out stops waiting once the turn is answerable — measured, not asserted.

The sibling file checks the SHAPE of the change. This one runs it: fake members
that answer at controlled times, and an assertion on the wall clock.

MEASURED 2026-09-04 on a real tool turn: three of five members answered, at 5s,
78s and 111s, and the request took 297 SECONDS -- the two that never answered
burned the whole 300s budget while the winning answer had been in hand since
111s. `ex.map` waits for the slowest member; that is three extra minutes of a
build turn spent waiting for models that were never going to reply.
"""
import time
from unittest import mock

import pytest

import app as A


TOOLS = [{"type": "function", "function": {"name": "write_file"}}]
BODY = {"model": "swarm", "tools": TOOLS,
        "messages": [{"role": "user", "content": "build it"}]}

PICKS = [("fast", "m1"), ("slow", "m2"), ("never", "m3")]


class _Resp:
    """The bit of requests.Response the fan-out touches."""

    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload

    def close(self):
        pass


def _with_tool_call():
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "write_file", "arguments": "{}"}}]}}]}


def _prose(text="here is some prose about the task"):
    return {"choices": [{"finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}]}


@pytest.fixture
def fanout(monkeypatch):
    """Pin the candidate list so only the waiting behaviour is under test."""
    monkeypatch.setattr(A, "_route_by_difficulty",
                        lambda *a, **k: ("fast", "m1", "hard"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: list(PICKS))
    monkeypatch.setattr(A, "_swarm_rank", lambda cands: list(PICKS))
    monkeypatch.setattr(A, "_record_outcome", lambda *a, **k: None)
    monkeypatch.setattr(A, "_record_chat_usage", lambda *a, **k: None)
    monkeypatch.setattr(A, "_note_nonanswer", lambda *a, **k: None)
    monkeypatch.setattr(A, "_normalize_model_identity", lambda m: m)
    monkeypatch.setattr(A, "_est_tokens", lambda *a, **k: 10)
    # a short grace so the test is quick, and a deadline long enough that only
    # the grace can end the wait
    monkeypatch.setattr(A, "_SWARM_STRAGGLER_GRACE", 1)
    monkeypatch.setattr(A, "_SWARM_TOOL_HOP_DEADLINE", 20)
    yield


def _dispatcher(timings):
    """timings: {provider: (seconds, payload_or_None)}."""
    def go(pid, payload, deadline):
        delay, out = timings[pid]
        time.sleep(delay)
        return (_Resp(out) if out is not None else None), None
    return go


def test_a_dead_member_no_longer_costs_the_whole_budget(fanout, monkeypatch):
    """The reported 297s turn, in miniature: one member answers immediately with
    a tool call, one never answers at all."""
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "fast": (0.0, _with_tool_call()),
        "slow": (0.2, _with_tool_call()),
        "never": (15.0, None),          # would blow the whole deadline
    }))
    started = time.monotonic()
    out = A._swarm_tool_result(dict(BODY))
    elapsed = time.monotonic() - started
    assert out is not None
    assert elapsed < 6, "waited %.1fs; the dead member should not be waited on" % elapsed


def test_the_answer_is_still_a_real_tool_call(fanout, monkeypatch):
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "fast": (0.0, _with_tool_call()),
        "slow": (0.2, _with_tool_call()),
        "never": (15.0, None),
    }))
    data, _hdrs = A._swarm_tool_result(dict(BODY))
    msg = data["choices"][0]["message"]
    assert msg["tool_calls"][0]["function"]["name"] == "write_file"


def test_a_close_second_still_gets_in(fanout, monkeypatch):
    """Best-of-N must still get its N when the others are merely a little
    slower -- the grace exists for exactly that."""
    seen = []
    inner = _dispatcher({
        "fast": (0.0, _with_tool_call()),
        "slow": (0.4, _with_tool_call()),      # inside the 1s grace
        "never": (15.0, None),
    })

    def spy(pid, payload, deadline):
        r = inner(pid, payload, deadline)
        seen.append(pid)
        return r

    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", spy)
    A._swarm_tool_result(dict(BODY))
    assert "slow" in seen, "a member answering inside the grace was dropped"


def test_prose_alone_does_not_start_the_grace(fanout, monkeypatch):
    """While the only answer on the table is something the CLI cannot execute,
    waiting longer is the right call -- a tool-caller may still be coming."""
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "fast": (0.0, _prose()),               # prose: must NOT start the grace
        "slow": (3.0, _with_tool_call()),      # well past a 1s grace
        "never": (15.0, None),
    }))
    data, _hdrs = A._swarm_tool_result(dict(BODY))
    msg = data["choices"][0]["message"]
    assert msg.get("tool_calls"), "the later tool call was cut off by the grace"


def test_everything_failing_still_returns_none(fanout, monkeypatch):
    """Which is what lets the caller fall back to single-model routing."""
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "fast": (0.0, None), "slow": (0.0, None), "never": (0.0, None),
    }))
    assert A._swarm_tool_result(dict(BODY)) is None


def test_a_member_that_raises_does_not_take_the_others_with_it(fanout, monkeypatch):
    def go(pid, payload, deadline):
        if pid == "fast":
            raise RuntimeError("boom")
        return _Resp(_with_tool_call()), None

    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", go)
    out = A._swarm_tool_result(dict(BODY))
    assert out is not None
