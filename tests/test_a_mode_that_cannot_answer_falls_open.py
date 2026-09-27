"""A category mode whose models are all failing must still answer -- on the
FIRST request, not only on the retry.

MEASURED 2026-09-27, live, global mode "coding":

    POST /v1/chat/completions  model=auto  stream=false  tools=[add]
    "Use the add tool to add 17 and 25."
    -> 503 after 122.7s
       tokenrouter/moonshotai/kimi-k3-free  ! HTTP 503 (model_not_found)
       dahl/zai-org/GLM-5.3-Flash           ! HTTP 503
       dahl/deepseek-ai/DeepSeek-V4-Flash   ! HTTP 429
       nvidia/z-ai/glm-5.3                  ! _HopBudgetExceeded
       nvidia/moonshotai/kimi-k3            ! _HopBudgetExceeded
       nvidia/z-ai/glm-5.3-flash            ! _HopBudgetExceeded
       nvidia/deepseek-ai/deepseek-v4.1-flash ! _HopBudgetExceeded

The same request a second later (stream=true) answered in 1.1s from
groq/qwen/qwen3.8-27b. Not a stream difference: _build_chain filtered the whole
chain to the coding category (groq's qwen3.8-27b is outside it), and
_apply_mode only fails open when that leaves NOTHING -- so a mode whose few
models were all listed-but-broken walked them to the end and 503'd. The router
fails open on RECENT failures instead, which is why whatever request came next
(stream or not) was routed straight out of the mode and answered.

Now the chain keeps every in-mode hop first and ends with a short tail of the
best healthy models OUTSIDE the mode, one per provider -- the same fail-open
contract _apply_mode states, applied at the level where it matters. On a tool
turn the walk already demotes a provider that stalled, so nvidia's second,
third and fourth siblings no longer burn 25s each before the tail is reached.
"""
import json

import pytest
import requests

import app as A

PIDS = ["pa", "pb", "pc", "pd"]
# "-coder" is in the mode, "-lyria" is not.
WORLD = {
    "pa": ["pa-coder", "pa-coder2", "pa-coder3"],
    "pb": ["pb-coder"],
    "pc": ["pc-lyria"],
    "pd": ["pd-lyria", "pd-lyria2"],
}
ASK = "Use the add tool to add 17 and 25."
MSGS = [{"role": "user", "content": ASK}]
PARAMS = {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
          "required": ["a", "b"]}
TOOLS = [{"type": "function", "function": {"name": "add", "description": "Add",
                                           "parameters": PARAMS}}]


def _in_mode(m):
    return "-coder" in m


@pytest.fixture
def fleet(monkeypatch):
    monkeypatch.setattr(A, "_available_providers", lambda *a, **k: list(PIDS))
    monkeypatch.setattr(A, "_prefetch_auto_models", lambda pids: dict(WORLD))
    monkeypatch.setattr(A, "_auto_models", lambda pid: list(WORLD[pid]))
    monkeypatch.setattr(A, "_provider_capable", lambda pid, est: True)
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: 134.0)
    monkeypatch.setattr(A, "_supports_tools", lambda pid, m: True)
    monkeypatch.setattr(A, "_session_pin_get", lambda key: None)
    monkeypatch.setattr(A, "_session_pin_set", lambda *a, **k: None)
    monkeypatch.setattr(A, "_weighted_pick", lambda pool, *a, **k: max(pool))
    monkeypatch.setattr(A, "_is_relay_pid", lambda pid: False)
    monkeypatch.setattr(A.model_categories, "matches",
                        lambda key, p, m, i=None: _in_mode(m))
    monkeypatch.setattr(A, "_active_mode", lambda: "coding")
    yield


def _chain(**kw):
    kw.setdefault("require_tools", True)
    return A._build_chain("pa", "pa-coder", 500, messages=MSGS, **kw)


# --------------------------------------------------------------------------- #
# The chain
# --------------------------------------------------------------------------- #

def test_the_chain_reaches_outside_the_mode_once_the_mode_is_walked(fleet):
    """The live failure in one assertion: no out-of-mode hop existed at all."""
    chain = _chain()
    outside = [e for e in chain if not _in_mode(e[1])]
    assert outside, chain


def test_every_in_mode_hop_comes_first(fleet):
    """The mode still decides the first retry and every one after it, until
    the mode itself is exhausted (test_model_mode's reason for the filter)."""
    chain = _chain()
    flags = [_in_mode(m) for _p, m in chain]
    assert flags == sorted(flags, reverse=True), chain
    assert {m for _p, m in chain if _in_mode(m)} == {
        m for ms in WORLD.values() for m in ms if _in_mode(m)}


def test_the_tail_is_short_and_spread_across_providers(fleet):
    chain = _chain()
    outside = [e for e in chain if not _in_mode(e[1])]
    assert len(outside) <= A._MODE_FALLBACK_TAIL
    assert len({p for p, _m in outside}) == len(outside), outside


def test_a_tool_turn_tail_holds_only_tool_capable_models(fleet, monkeypatch):
    monkeypatch.setattr(A, "_supports_tools", lambda pid, m: m != "pc-lyria")
    chain = _chain()
    assert ("pc", "pc-lyria") not in chain
    assert any(p == "pd" for p, _m in chain), chain


def test_the_same_holds_for_a_chat_turn(fleet):
    chain = _chain(require_tools=False)
    flags = [_in_mode(m) for _p, m in chain]
    assert False in flags and flags == sorted(flags, reverse=True), chain


def test_no_tail_and_no_duplicate_under_all(fleet, monkeypatch):
    monkeypatch.setattr(A, "_active_mode", lambda: A.MODE_ALL)
    chain = _chain()
    assert len(chain) == len(set(chain)), chain


def test_a_best_max_fallback_prefers_the_mode(fleet):
    """The best/max fallback picks from the chain not walked yet; with the tail
    in it, it must still pick in-mode hops while any remain."""
    picks = A._quality_fallback_pick([("pb", "pb-coder"), ("pc", "pc-lyria")])
    assert picks and all(_in_mode(m) for _p, m in picks), picks


# --------------------------------------------------------------------------- #
# End to end: stream and non-stream walk the same chain and both answer
# --------------------------------------------------------------------------- #

class _Resp:
    def __init__(self, status=200, payload=None, chunks=None):
        self.status_code = status
        self._payload = payload
        self._chunks = chunks
        self.headers = {}
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def close(self):
        pass

    def iter_content(self, chunk_size=None):
        return iter(self._chunks or ())

    def iter_lines(self, decode_unicode=False):
        return iter(self._chunks or ())


CALL = {"id": "call_1", "type": "function",
        "function": {"name": "add", "arguments": '{"a": 17, "b": 25}'}}


def _tool_answer(stream):
    if stream:
        units = [("data: " + json.dumps({"choices": [{"index": 0, "delta": {
            "role": "assistant", "tool_calls": [dict(CALL, index=0)]}}]})).encode(),
                 b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}',
                 b"data: [DONE]"]
        return _Resp(chunks=[u + b"\n\n" for u in units])
    return _Resp(payload={"id": "x", "object": "chat.completion", "created": 1, "model": "m",
                          "choices": [{"index": 0, "finish_reason": "tool_calls",
                                       "message": {"role": "assistant", "content": None,
                                                   "tool_calls": [CALL]}}]})


@pytest.fixture
def hub(fleet, monkeypatch):
    for name in ("_record_chat_usage", "_save_perf_stats", "_act_pick", "_note_ttft",
                 "_record_stream_outcome", "_note_provider_timeout", "_record_outcome",
                 "_throttle_failed_hop", "_mark_model_dead"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)
    monkeypatch.setattr(A, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(A, "_model_block_reason", lambda pid, m: None)
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    calls = []

    def dispatch(pid, payload, stream):
        model = payload.get("model")
        calls.append((pid, model))
        if pid == "pa":             # the stalling gateway (nvidia, live)
            raise requests.exceptions.ReadTimeout("stalled")
        if pid == "pb":             # the dead channel (tokenrouter, live)
            return _Resp(status=503, payload={"error": {"code": "model_not_found"}})
        return _tool_answer(stream)
    monkeypatch.setattr(A, "_dispatch_chat", dispatch)
    client = A.app.test_client()
    client.calls = calls
    return client


def _ask(hub, stream):
    del hub.calls[:]
    r = hub.post("/v1/chat/completions", json={
        "model": "auto", "stream": stream, "max_tokens": 200, "tools": TOOLS,
        "messages": MSGS})
    body = r.get_data(as_text=True)     # drain a stream before the next request
    r.close()
    return r, body, list(hub.calls)


@pytest.mark.parametrize("stream", [False, True])
def test_a_failing_mode_still_answers_the_first_request(hub, stream):
    r, body, calls = _ask(hub, stream)
    assert r.status_code == 200, body[:400]
    assert '"add"' in body or "add" in body
    assert calls[-1][0] in ("pc", "pd"), calls


@pytest.mark.parametrize("stream", [False, True])
def test_a_stalled_gateway_does_not_walk_its_siblings_first(hub, stream):
    """pa stalled once: its two in-mode siblings go behind the healthy tail
    (see _ChainClock.walk), instead of 25s each before anything answers."""
    _r, _b, calls = _ask(hub, stream)
    assert sum(1 for p, _m in calls if p == "pa") == 1, calls


def test_stream_and_non_stream_walk_the_same_hops(hub):
    # _recent_hop_fail is process-wide (see conftest): the first request's
    # stall would reorder the second one's chain.
    A._recent_hop_fail.clear()
    _r1, _b1, plain = _ask(hub, False)
    A._recent_hop_fail.clear()
    _r2, _b2, streamed = _ask(hub, True)
    assert plain == streamed, (plain, streamed)
