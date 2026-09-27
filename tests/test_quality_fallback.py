"""best/max degrade gracefully instead of hanging until the client gives up.

MEASURED 2026-09-27 on 3c10c24: Claude Code `claude -p "What is N plus 1?
Reply with only the number." --model max` (/v1/messages, tools, stream) had no
answer after 421 s while `--model multi` answered in 7 s. Upstream was
degraded (quota exhausted, 429s, a stalling provider) and best/max -- which
lift a simple ask to medium and so skip every quick-turn rule -- walked the
strength-ordered chain one stall at a time until the deadline.

Now: past a bounded share of the deadline (sooner for a trivial ask) with
nothing served, the walk jumps to the best available fast capable model, cuts
a hop still waiting at that mark, and marks the answer
(X-Free-LLM-Hub-Fallback: max->auto, activity row `fallback`). A healthy
strong pool is untouched. Fakes only, tiny sleeps, no network.
"""
import json
import time

import pytest

import app as A


class _Resp:
    def __init__(self, status=200, payload=None, chunks=None):
        self.status_code = status
        self._payload = payload or {}
        self._chunks = chunks
        self.headers = {}
        self.text = ""

    def json(self):
        return self._payload

    def close(self):
        pass

    def iter_content(self, chunk_size=None):
        return iter(self._chunks or ())

    def iter_lines(self, decode_unicode=False):
        return iter(self._chunks or ())


def _answer(text):
    return {"choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}]}


def _sse(text):
    return [("data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": text}}]})
             ).encode(),
            b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
            b"data: [DONE]"]


def _silent(seconds=3.0):
    time.sleep(seconds)
    yield b'data: {"choices":[{"index":0,"delta":{"content":"too late"}}]}'


STRONG = [("nv", "strong-a"), ("nv", "strong-b"), ("dahl", "strong-c")]
QUICK = ("fastp", "quick")


@pytest.fixture
def hub(monkeypatch):
    for name in ("_record_chat_usage", "_record_outcome", "_save_perf_stats",
                 "_act_pick", "_note_ttft", "_record_stream_outcome",
                 "_note_provider_timeout", "_throttle_failed_hop"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)
    monkeypatch.setattr(A, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(A, "_model_block_reason", lambda pid, m: None)
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("nv", "strong-a", "medium"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: STRONG + [QUICK])
    monkeypatch.setattr(A, "_is_fast", lambda p, m: m == "quick")
    monkeypatch.setattr(A, "_is_trivial_turn", lambda *a, **k: False)
    monkeypatch.setattr(A, "_is_trivial_ask", lambda *a, **k: False)
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: 30)
    monkeypatch.setattr(A, "_ADAPTIVE_HOP_FLOOR", 0.05)
    monkeypatch.setattr(A, "_QUALITY_FALLBACK_SHARE", 0.0)
    monkeypatch.setattr(A, "_QUALITY_FALLBACK_MIN_SECONDS", 0.6)
    monkeypatch.setattr(A, "_QUALITY_FALLBACK_TRIVIAL_SECONDS", 0.3)
    state = {"strong": "stall", "seen": []}

    def dispatch(pid, payload, stream):
        state["seen"].append((pid, payload.get("model")))
        if payload.get("model", "").startswith("strong"):
            if state["strong"] == "healthy":
                return _Resp(200, chunks=_sse("41")) if stream else _Resp(200, _answer("41"))
            if stream:
                return _Resp(200, chunks=_silent())
            time.sleep(3.0)
            return _Resp(200, _answer("too late"))
        return _Resp(200, chunks=_sse("42")) if stream else _Resp(200, _answer("42"))
    monkeypatch.setattr(A, "_dispatch_chat", dispatch)
    client = A.app.test_client()
    client.state = state
    return client


ASK = "Explain how a hash map resolves collisions."


def test_chat_max_falls_back_when_the_strong_pool_stalls(hub):
    t0 = time.monotonic()
    r = hub.post("/v1/chat/completions", json={
        "model": "max", "stream": False, "messages": [{"role": "user", "content": ASK}]})
    took = time.monotonic() - t0
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["choices"][0]["message"]["content"] == "42"
    assert r.headers.get("X-Free-LLM-Hub-Fallback") == "max->auto"
    assert r.headers.get("X-Free-LLM-Hub-Provider") == "fastp"
    # one strong hop cut at the mark, then the fallback -- not three 3 s stalls
    assert took < 2.5
    assert hub.state["seen"][:2] == [("nv", "strong-a"), QUICK]


def test_claude_code_shape_messages_stream_with_tools(hub):
    """The live case: /v1/messages, streaming, tools offered, model max."""
    t0 = time.monotonic()
    r = hub.post("/v1/messages", json={
        "model": "max", "max_tokens": 256, "stream": True,
        "tools": [{"name": "bash", "input_schema": {"type": "object", "properties": {}}}],
        "messages": [{"role": "user", "content": ASK}]})
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "42" in body and "too late" not in body
    assert r.headers.get("X-Free-LLM-Hub-Fallback") == "max->auto"
    assert time.monotonic() - t0 < 2.5


def test_responses_best_is_marked_best(hub):
    r = hub.post("/v1/responses", json={"model": "best", "stream": False, "input": ASK})
    assert r.status_code == 200
    assert r.headers.get("X-Free-LLM-Hub-Fallback") == "best->auto"
    assert "42" in r.get_data(as_text=True)


def test_a_trivial_ask_falls_back_sooner(hub, monkeypatch):
    monkeypatch.setattr(A, "_is_trivial_ask", lambda *a, **k: True)
    monkeypatch.setattr(A, "_QUALITY_FALLBACK_MIN_SECONDS", 20.0)
    t0 = time.monotonic()
    r = hub.post("/v1/chat/completions", json={
        "model": "max", "stream": True,
        "messages": [{"role": "user", "content": "What is 41 plus 1? Reply with only the number."}]})
    assert b"42" in r.get_data()
    assert r.headers.get("X-Free-LLM-Hub-Fallback") == "max->auto"
    assert time.monotonic() - t0 < 2.0


def test_a_healthy_strong_pool_is_unchanged(hub):
    hub.state["strong"] = "healthy"
    for stream in (False, True):
        r = hub.post("/v1/chat/completions", json={
            "model": "max", "stream": stream, "messages": [{"role": "user", "content": ASK}]})
        assert "41" in r.get_data(as_text=True)
        assert "X-Free-LLM-Hub-Fallback" not in r.headers
        assert r.headers.get("X-Free-LLM-Hub-Provider") == "nv"


def test_auto_is_never_touched(hub):
    """Only best/max arm it: auto keeps walking its own chain (here the strong
    hops stall 3 s each, so it is slow -- and unmarked)."""
    hub.state["strong"] = "healthy"
    r = hub.post("/v1/chat/completions", json={
        "model": "auto", "stream": False, "messages": [{"role": "user", "content": ASK}]})
    assert "X-Free-LLM-Hub-Fallback" not in r.headers


# --------------------------------------------------------------------------- #
# The pieces
# --------------------------------------------------------------------------- #

def test_the_pick_prefers_healthy_quick_strong(monkeypatch):
    scores = {"a": 120, "b": 100, "c": 90, "d": 130, "e": 140}
    monkeypatch.setattr(A, "_benchmark_score", lambda p, m: scores[m])
    monkeypatch.setattr(A, "_is_fast", lambda p, m: m != "e")        # e: slow
    monkeypatch.setattr(A, "_recent_hop_stall", lambda p, m: m == "d")  # d: just stalled
    monkeypatch.setattr(A, "_is_pair_benched", lambda p, m: False)
    monkeypatch.setattr(A, "_chain_reliability_band", lambda p, m: 0)
    monkeypatch.setattr(A, "_recent_hop_failure", lambda p, m: False)
    monkeypatch.setattr(A, "_simple_speed_rank", lambda p, m: (False, False))
    entries = [("p1", "a"), ("stalledp", "b"), ("p3", "c"), ("p4", "d"), ("p5", "e")]
    picks = A._quality_fallback_pick(entries, stalled={"stalledp"})
    assert picks == [("p1", "a"), ("p3", "c")]


def test_the_pick_fails_open_and_never_raises(monkeypatch):
    monkeypatch.setattr(A, "_is_fast", lambda p, m: False)
    assert A._quality_fallback_pick([("p1", "m1")]) == [("p1", "m1")]
    assert A._quality_fallback_pick(None) == []


def test_firing_marks_the_request_and_the_activity_row(monkeypatch):
    monkeypatch.setattr(A, "_is_fast", lambda p, m: m == "quick")
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: 30)
    with A.app.test_request_context("/v1/chat/completions"):
        A.g.act = {}
        clock = A._ChainClock()
        clock.plan_quality_fallback("max->auto")
        clock._qf["at"] = time.monotonic() - 1          # already due
        walked = list(clock.walk(STRONG + [QUICK]))
        assert walked[0] == QUICK
        assert A.g.act["fallback"] == "max->auto"
        assert A._quality_fallback_header(*QUICK) == "max->auto"
        assert A._quality_fallback_header("nv", "strong-a") is None
        assert A._routing_headers(*QUICK, 2).get("X-Free-LLM-Hub-Fallback") == "max->auto"


def test_the_mark_caps_a_waiting_hop_only_while_something_is_left(monkeypatch):
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: 240)
    with A.app.test_request_context("/v1/chat/completions"):
        clock = A._ChainClock()
        clock.plan_quality_fallback("max->auto")
        mark = clock._qf["at"] - clock._qf["start"]
        clock._rest = [QUICK]
        assert clock._budget_for("nv", "strong-a") <= mark
        clock._rest = []                     # nothing to fall back to: no cut
        assert clock._budget_for("nv", "strong-a") > mark + 60


def test_the_mark_is_a_bounded_share_of_the_deadline(monkeypatch):
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: 240)
    with A.app.test_request_context("/v1/chat/completions"):
        clock = A._ChainClock()
        clock.plan_quality_fallback("max->auto")
        wait = clock._qf["at"] - clock._qf["start"]
        assert A._QUALITY_FALLBACK_MIN_SECONDS <= wait <= 0.5 * 240
        clock2 = A._ChainClock()
        monkeypatch.setattr(A, "_is_trivial_ask", lambda *a, **k: True)
        clock2.plan_quality_fallback("max->auto", [{"role": "user", "content": "2+2?"}])
        assert clock2._qf["at"] - clock2._qf["start"] == A._QUALITY_FALLBACK_TRIVIAL_SECONDS
