"""Trivial questions answer in seconds when a fast capable model is alive.

MEASURED 2026-09-26, live, after the request-deadline work: "What is N plus
1?" took auto 36.7s, best 64.7s, max 56.9s, coding 31.9s (streaming 32-40s),
with X-Free-LLM-Hub-Last-Error 429 or deadline -- earlier hops were rate
limited or slow before one answered -- while other categories answered in 1-8s.

Pinned here, with fake providers and tiny sleeps (no network):
  * a (provider, model) that 429'd or ran out its time in the last ten minutes
    is ordered last in every chain and never opens the turn;
  * hop budgets follow the MEASURED p90 (max 6s, 3x p90) instead of a flat 25s;
  * a streamed hop already writing visible text is never cut by its budget;
  * hedging: a trivial, tool-free, small turn whose hop stays silent starts the
    next candidate in parallel and serves the first VALID answer -- at most one
    extra call, and before any byte reaches the client.
"""
import threading
import time

import pytest

import app as A


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class _Resp:
    def __init__(self, status=200, payload=None, chunks=None):
        self.status_code = status
        self._payload = payload or {}
        self._chunks = chunks
        self.headers = {}
        self.text = ""
        self.closed = False

    def json(self):
        return self._payload

    def close(self):
        self.closed = True

    def iter_content(self, chunk_size=None):
        return iter(self._chunks or ())

    def iter_lines(self, decode_unicode=False):
        return iter(self._chunks or ())


def _answer(text="5768"):
    return {"choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}]}


QUESTION = "What is 5767 plus 1? Answer with only the number."
SSE_ANSWER = (b'data: {"choices":[{"delta":{"content":"5768"}}]}\n\n'
              b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
              b'data: [DONE]\n\n')
LINE_ANSWER = [b'data: {"choices":[{"delta":{"content":"5768"}}]}',
               b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
               b'data: [DONE]']


@pytest.fixture
def quiet(monkeypatch):
    for name in ("_record_chat_usage", "_record_outcome", "_save_perf_stats",
                 "_act_pick", "_note_ttft", "_record_stream_outcome",
                 "_note_provider_timeout", "_throttle_failed_hop"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)
    monkeypatch.setattr(A, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(A, "_model_block_reason", lambda pid, m: None)
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=None: True if k == "hedge_simple_turns" else d)
    yield


@pytest.fixture
def hedge_fast(monkeypatch):
    """Budgets and hedge delay shrunk to test scale: 2s hop, 0.2s hedge."""
    monkeypatch.setattr(A, "_TRIVIAL_HOP_BUDGET", 2.0)
    monkeypatch.setattr(A, "_TRIVIAL_SLOW_HOP_BUDGET", 2.0)
    monkeypatch.setattr(A, "_HEDGE_DELAY_UNKNOWN", 0.2)
    monkeypatch.setattr(A, "_ADAPTIVE_HOP_FLOOR", 1.0)
    yield


def _route_to(monkeypatch, pid, model, difficulty="simple"):
    monkeypatch.setattr(A, "_route_by_difficulty",
                        lambda *a, **k: (pid, model, difficulty))


def _chain(monkeypatch, *hops):
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: list(hops))


# --------------------------------------------------------------------------- #
# The recent-failure ledger
# --------------------------------------------------------------------------- #

def test_the_ledger_remembers_then_forgets():
    assert A._recent_hop_failure("p", "m") is None
    A._note_recent_hop_failure("p", "m", "429")
    assert A._recent_hop_failure("p", "m") == "429"
    A._recent_hop_fail[("p", "m")] = (time.time() - A._RECENT_FAIL_TTL - 1, "429")
    assert A._recent_hop_failure("p", "m") is None, "ten minutes, not forever"
    A._note_recent_hop_failure("p", "m", "deadline")
    A._clear_recent_hop_failure("p", "m")
    assert A._recent_hop_failure("p", "m") is None


@pytest.fixture
def fleet(monkeypatch):
    world = {"pa": ["pa-1", "pa-2"], "pb": ["pb-1"], "pc": ["pc-1"]}
    scores = {"pa-1": 100.0, "pa-2": 90.0, "pb-1": 95.0, "pc-1": 80.0}
    monkeypatch.setattr(A, "_available_providers", lambda *a, **k: list(world))
    monkeypatch.setattr(A, "_prefetch_auto_models", lambda pids: dict(world))
    monkeypatch.setattr(A, "_auto_models", lambda pid: list(world[pid]))
    monkeypatch.setattr(A, "_provider_capable", lambda pid, est: True)
    monkeypatch.setattr(A, "_supports_tools", lambda pid, m: True)
    monkeypatch.setattr(A, "_session_pin_get", lambda key: None)
    monkeypatch.setattr(A, "_session_pin_set", lambda *a, **k: None)
    monkeypatch.setattr(A, "_is_fast", lambda pid, m: True)
    monkeypatch.setattr(A, "_measured_latency_ms", lambda pid, m: None)
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: scores[m])
    monkeypatch.setattr(A, "_active_mode", lambda: A.MODE_ALL)
    monkeypatch.setattr(A, "_is_model_dead", lambda pid, m: False)
    monkeypatch.setattr(A.quota, "is_model_throttled", lambda pid, m: False)
    monkeypatch.setattr(A.quota, "model_status", lambda pid, m: {"exhausted": False})
    monkeypatch.setattr(A, "_context_ok", lambda pid, m, est: True)
    monkeypatch.setattr(A, "_sub_available_providers", lambda: [])
    # Deterministic picks whatever earlier tests left in the process: shipped
    # flag defaults (route_always_best), no learned penalties.
    monkeypatch.setattr(A.config, "get_flag", lambda k, d=None: d)
    for name in ("_reliability_penalty", "_latency_penalty", "_answer_quality_penalty"):
        monkeypatch.setattr(A, name, lambda pid, m: 0.0)
    monkeypatch.setattr(A, "_chain_reliability_band", lambda pid, m: 0)
    return world


SIMPLE = [{"role": "user", "content": QUESTION}]
HARD = [{"role": "user", "content": "Explain in depth how a B-tree rebalances."}]


def test_a_recent_failure_goes_to_the_tail_of_every_chain(fleet):
    A._note_recent_hop_failure("pa", "pa-1", "429")
    for msgs, shape in ((SIMPLE, "simple"), (HARD, "hard")):
        with A.app.test_request_context():
            A._mark_turn_shape(shape, 400)
            chain = A._build_chain("", "", 400, messages=msgs)
        assert chain[-1] == ("pa", "pa-1"), chain       # last resort ...
        assert len(set(chain)) == len(chain)
        assert {("pb", "pb-1"), ("pc", "pc-1"), ("pa", "pa-2")} <= set(chain)


def test_a_recently_failed_primary_does_not_open_the_turn(fleet):
    A._note_recent_hop_failure("pa", "pa-1", "deadline")
    with A.app.test_request_context():
        chain = A._build_chain("pa", "pa-1", 400, messages=HARD)
    assert chain[0] != ("pa", "pa-1")
    assert ("pa", "pa-1") in chain, "demoted, never removed"
    # ...but a model the CALLER named still opens its own turn.
    with A.app.test_request_context():
        pinned = A._build_chain("pa", "pa-1", 400, messages=HARD, pinned=True)
    assert pinned[0] == ("pa", "pa-1")


def test_a_recent_failure_stays_past_the_hop_cap(fleet, monkeypatch):
    monkeypatch.setattr(A, "MAX_HOPS", 2)
    A._note_recent_hop_failure("pc", "pc-1", "429")
    with A.app.test_request_context():
        chain = A._build_chain("", "", 400, messages=HARD)
    assert ("pc", "pc-1") in chain


def test_the_router_skips_a_recent_failure_and_fails_open(fleet):
    A._note_recent_hop_failure("pa", "pa-1", "429")
    _p, model, _d = A._route_by_difficulty(HARD, None, 400)
    assert model != "pa-1"
    for pid, models in fleet.items():
        for m in models:
            A._note_recent_hop_failure(pid, m, "429")
    _p, model, _d = A._route_by_difficulty(HARD, None, 400)
    assert model is not None, "every candidate failed recently: still answer"


def test_best_max_keep_the_strong_pool_but_skip_recent_failures(fleet):
    A._note_recent_hop_failure("pa", "pa-1", "429")
    _p, model, diff = A._route_by_difficulty(SIMPLE, None, 400, quality_mode=True)
    assert diff == "medium"
    assert model == "pb-1", "strongest of what did not just fail"


def test_a_simple_pick_is_speed_first_on_evidence(fleet, monkeypatch):
    """Cheapest-that-clears-the-floor used to win whatever its measured speed."""
    slow = {"pc-1": 40000.0}
    monkeypatch.setattr(A, "_measured_latency_ms", lambda pid, m: slow.get(m))
    monkeypatch.setattr(A, "_DIFFICULTY_FLOOR", dict(A._DIFFICULTY_FLOOR, simple=0))
    _p, model, diff = A._route_by_difficulty(SIMPLE, None, 400)
    assert diff == "simple"
    assert model == "pa-2", model          # cheapest of the not-measured-slow
    monkeypatch.setattr(A, "_measured_latency_ms", lambda pid, m: None)
    _p, model, _d = A._route_by_difficulty(SIMPLE, None, 400)
    assert model == "pc-1", "unmeasured fleet: the cheapest pick is unchanged"


# --------------------------------------------------------------------------- #
# Adaptive hop budgets
# --------------------------------------------------------------------------- #

def _samples(monkeypatch, ttft=None, dur=None):
    monkeypatch.setattr(A, "_ttft", {("p", "m"): list(ttft or [])})
    monkeypatch.setattr(A, "_speed", {("p", "m"): list(dur or [])})


def test_an_unmeasured_hop_keeps_the_ceiling(monkeypatch):
    _samples(monkeypatch)
    assert A._adaptive_hop_budget("p", "m", 25) == 25
    assert A._hedge_delay("p", "m") == A._HEDGE_DELAY_UNKNOWN


def test_a_measured_fast_hop_gets_the_floor(monkeypatch):
    _samples(monkeypatch, ttft=[500, 700, 900, 1000])
    assert A._adaptive_hop_budget("p", "m", 25) == A._ADAPTIVE_HOP_FLOOR
    assert A._hedge_delay("p", "m") == A._HEDGE_DELAY_MIN


def test_a_measured_hop_gets_three_times_its_p90(monkeypatch):
    _samples(monkeypatch, ttft=[3000, 4000, 5000, 5000])
    assert A._adaptive_hop_budget("p", "m", 25) == pytest.approx(15.0)
    assert A._hedge_delay("p", "m") == pytest.approx(1.5 * 4.0)


def test_the_adaptive_budget_never_exceeds_the_old_ceiling(monkeypatch):
    _samples(monkeypatch, ttft=[20000, 30000, 30000])
    assert A._adaptive_hop_budget("p", "m", 25) == 25


def test_a_non_streamed_hop_is_judged_on_its_durations(monkeypatch):
    _samples(monkeypatch, ttft=[500, 500, 500], dur=[4000, 4000, 4000])
    assert A._adaptive_hop_budget("p", "m", 25, stream=False) == pytest.approx(12.0)
    assert A._adaptive_hop_budget("p", "m", 25, stream=True) == A._ADAPTIVE_HOP_FLOOR


def test_the_clock_uses_the_adaptive_budget(monkeypatch):
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: None)
    _samples(monkeypatch, ttft=[1000, 1000, 1000])
    with A.app.test_request_context():
        c = A._ChainClock(trivial=True)
        assert c._budget_for("p", "m") == A._ADAPTIVE_HOP_FLOOR
        assert A._ChainClock(trivial=False)._budget_for("p", "m") is None


# --------------------------------------------------------------------------- #
# The clock feeds the ledger
# --------------------------------------------------------------------------- #

def test_a_429_hop_is_remembered(monkeypatch):
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: None)
    monkeypatch.setattr(A, "_dispatch_chat", lambda pid, pl, s: _Resp(429))
    with A.app.test_request_context():
        A._ChainClock(trivial=False).dispatch("p", {"model": "m"}, False)
    assert A._recent_hop_failure("p", "m") == "429"


def test_a_hop_that_used_a_fair_budget_is_remembered(monkeypatch):
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: None)
    monkeypatch.setattr(A, "_ADAPTIVE_HOP_FLOOR", 0.1)
    monkeypatch.setattr(A, "_TRIVIAL_HOP_BUDGET", 0.15)

    def slow(pid, pl, s):
        time.sleep(1)
        return _Resp(200, _answer())
    monkeypatch.setattr(A, "_dispatch_chat", slow)
    with A.app.test_request_context():
        with pytest.raises(A._HopBudgetExceeded):
            A._ChainClock(trivial=True).dispatch("p", {"model": "m"}, False)
    assert A._recent_hop_failure("p", "m") == "deadline"


def test_a_hop_squeezed_by_the_request_deadline_is_not_blamed(monkeypatch):
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: 0.1)

    def slow(pid, pl, s):
        time.sleep(1)
        return _Resp(200, _answer())
    monkeypatch.setattr(A, "_dispatch_chat", slow)
    with A.app.test_request_context():
        with pytest.raises(A._HopBudgetExceeded):
            A._ChainClock(trivial=False).dispatch("p", {"model": "m"}, False)
    assert A._recent_hop_failure("p", "m") is None


def test_a_silent_stream_is_remembered_and_content_clears_it(monkeypatch):
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: None)
    with A.app.test_request_context():
        c = A._ChainClock(trivial=True)
        c._hop_budget = A._ADAPTIVE_HOP_FLOOR
        c.note_peek("p", "m", "timeout")
        assert A._recent_hop_failure("p", "m") == "deadline"
        c.note_peek("p", "m", "content")
        assert A._recent_hop_failure("p", "m") is None


# --------------------------------------------------------------------------- #
# A hop already writing visible text is never cut
# --------------------------------------------------------------------------- #

def test_a_short_answer_whose_done_arrives_alone_is_content():
    """ "5768", then finish, then [DONE] as separate reads -- every iter_lines
    stream -- was judged EMPTY (under _PEEK_JUDGE_CHARS, then a terminal), and
    the chain threw a correct trivial answer away and walked on."""
    status, buf = A._peek_until_content(iter(LINE_ANSWER), 2.0)
    assert status == "content"
    assert buf == LINE_ANSWER
    # A stream that ends with nothing said is still empty.
    assert A._peek_until_content(iter([b'data: {"choices":[{"delta":{"role":"assistant"}}]}',
                                       b"data: [DONE]"]), 2.0)[0] == "empty"


def test_the_peek_does_not_cut_a_hop_that_is_writing():
    def writing():
        yield b'data: {"choices":[{"delta":{"content":"The answer"}}]}\n\n'
        time.sleep(0.4)
        yield b'data: {"choices":[{"delta":{"content":" is 5768."}}]}\n\n'
        yield b"data: [DONE]\n\n"
    status, buf = A._peek_until_content(writing(), 0.1, content_grace=2.0)
    assert status == "content"
    assert len(buf) == 3
    # ...while a silent one still is.
    def silent():
        time.sleep(1)
        yield b"data: [DONE]\n\n"
    assert A._peek_until_content(silent(), 0.1, content_grace=2.0)[0] == "timeout"


# --------------------------------------------------------------------------- #
# Hedging, end to end through the chain loops
# --------------------------------------------------------------------------- #

def _slow_then_fast(calls, slow_s=3.0, slow_reply=None, fast_reply=None):
    def dispatch(pid, payload, stream):
        calls.append((pid, payload.get("model"), stream))
        if pid == "slowp":
            time.sleep(slow_s)
            return slow_reply() if slow_reply else _Resp(200, _answer("slow"))
        return fast_reply() if fast_reply else _Resp(200, _answer("5768"))
    return dispatch


def test_a_silent_hop_is_hedged_and_the_first_valid_answer_wins(quiet, hedge_fast, monkeypatch):
    _route_to(monkeypatch, "slowp", "slow-model")
    _chain(monkeypatch, ("slowp", "slow-model"), ("fastp", "quick-model"))
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat", _slow_then_fast(calls))
    t0 = time.monotonic()
    r = A.app.test_client().post("/v1/chat/completions", json={
        "model": "auto", "stream": False,
        "messages": [{"role": "user", "content": QUESTION}]})
    took = time.monotonic() - t0
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["choices"][0]["message"]["content"] == "5768"
    assert r.get_json()["model"] == "fastp/quick-model"
    assert took < 1.5, took
    assert [c[0] for c in calls] == ["slowp", "fastp"], "one extra call, no more"
    # Losing a race is not a failure: the slow hop is not demoted for it.
    assert A._recent_hop_failure("slowp", "slow-model") is None


def test_a_hedged_stream_serves_the_winner_before_any_byte(quiet, hedge_fast, monkeypatch):
    _route_to(monkeypatch, "slowp", "slow-model")
    _chain(monkeypatch, ("slowp", "slow-model"), ("fastp", "quick-model"))

    def silent():
        time.sleep(3)
        yield b'data: {"choices":[{"delta":{"content":"slow answer"}}]}\n\n'
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat", _slow_then_fast(
        calls, slow_s=0, slow_reply=lambda: _Resp(200, chunks=silent()),
        fast_reply=lambda: _Resp(200, chunks=[SSE_ANSWER])))
    t0 = time.monotonic()
    r = A.app.test_client().post("/v1/chat/completions", json={
        "model": "auto", "stream": True,
        "messages": [{"role": "user", "content": QUESTION}]})
    body = r.get_data()
    assert r.status_code == 200
    assert b"5768" in body and b"slow answer" not in body
    assert time.monotonic() - t0 < 1.5
    assert all(c[2] for c in calls), "a streaming request hedges with streams"


def test_an_invalid_first_answer_does_not_win_the_race(quiet, hedge_fast, monkeypatch):
    """First back is not enough: the answer must pass answer_check."""
    _route_to(monkeypatch, "slowp", "slow-model")
    _chain(monkeypatch, ("slowp", "slow-model"), ("fastp", "quick-model"))
    junk = "<|im_end|>" * 60
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat", _slow_then_fast(
        calls, slow_s=0.5, slow_reply=lambda: _Resp(200, _answer("5768")),
        fast_reply=lambda: _Resp(200, _answer(junk))))
    r = A.app.test_client().post("/v1/chat/completions", json={
        "model": "auto", "stream": False,
        "messages": [{"role": "user", "content": QUESTION}]})
    assert r.status_code == 200
    assert r.get_json()["choices"][0]["message"]["content"] == "5768"
    assert r.get_json()["model"] == "slowp/slow-model"


def test_at_most_one_extra_call_per_request(quiet, hedge_fast, monkeypatch):
    monkeypatch.setattr(A, "_TRIVIAL_HOP_BUDGET", 0.6)
    monkeypatch.setattr(A, "_TRIVIAL_SLOW_HOP_BUDGET", 0.6)
    _route_to(monkeypatch, "p1", "m1")
    _chain(monkeypatch, ("p1", "m1"), ("p2", "m2"), ("p3", "m3"), ("p4", "m4"))
    calls = []
    lock = threading.Lock()

    def dispatch(pid, payload, stream):
        with lock:
            calls.append(pid)
        if pid in ("p1", "p2"):
            time.sleep(3)
        if pid == "p3":
            time.sleep(0.4)            # past the hedge delay -- but no 2nd hedge
        return _Resp(200, _answer("5768"))
    monkeypatch.setattr(A, "_dispatch_chat", dispatch)
    r = A.app.test_client().post("/v1/chat/completions", json={
        "model": "auto", "stream": False,
        "messages": [{"role": "user", "content": QUESTION}]})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["model"] == "p3/m3"
    assert calls == ["p1", "p2", "p3"], calls


def test_a_fast_failure_goes_back_to_the_loop_untouched(quiet, hedge_fast, monkeypatch):
    """Leg 0 failing before the hedge fires is an ordinary hop failure: the
    loop moves on (and the 429 is remembered)."""
    _route_to(monkeypatch, "p1", "m1")
    _chain(monkeypatch, ("p1", "m1"), ("p2", "m2"))
    calls = []

    def dispatch(pid, payload, stream):
        calls.append(pid)
        return _Resp(429) if pid == "p1" else _Resp(200, _answer("5768"))
    monkeypatch.setattr(A, "_dispatch_chat", dispatch)
    r = A.app.test_client().post("/v1/chat/completions", json={
        "model": "auto", "stream": False,
        "messages": [{"role": "user", "content": QUESTION}]})
    assert r.status_code == 200
    assert calls == ["p1", "p2"]
    assert r.headers.get("X-Free-LLM-Hub-Last-Error") == "429"
    assert A._recent_hop_failure("p1", "m1") == "429"


def test_a_tool_turn_is_never_hedged(quiet, hedge_fast, monkeypatch):
    _route_to(monkeypatch, "slowp", "slow-model")
    _chain(monkeypatch, ("slowp", "slow-model"), ("fastp", "quick-model"))
    calls = []

    def dispatch(pid, payload, stream):
        calls.append(pid)
        time.sleep(0.5)
        return _Resp(200, {"choices": [{"index": 0, "finish_reason": "tool_calls",
                                        "message": {"role": "assistant", "content": None,
                                                    "tool_calls": [{
                                                        "id": "c1", "type": "function",
                                                        "function": {"name": "add",
                                                                     "arguments": "{}"}}]}}]})
    monkeypatch.setattr(A, "_dispatch_chat", dispatch)
    r = A.app.test_client().post("/v1/chat/completions", json={
        "model": "auto", "stream": False,
        "tools": [{"type": "function", "function": {"name": "add", "parameters": {}}}],
        "messages": [{"role": "user", "content": "Use the add tool to add 17 and 25"}]})
    assert r.status_code == 200
    assert calls == ["slowp"]


def test_the_hedge_switch_turns_it_off(quiet, hedge_fast, monkeypatch):
    monkeypatch.setattr(A.config, "get_flag", lambda k, d=None: False
                        if k == "hedge_simple_turns" else d)
    _route_to(monkeypatch, "slowp", "slow-model")
    _chain(monkeypatch, ("slowp", "slow-model"), ("fastp", "quick-model"))
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat", _slow_then_fast(calls, slow_s=0.5))
    r = A.app.test_client().post("/v1/chat/completions", json={
        "model": "auto", "stream": False,
        "messages": [{"role": "user", "content": QUESTION}]})
    assert r.get_json()["model"] == "slowp/slow-model"
    assert [c[0] for c in calls] == ["slowp"]


def test_responses_hedges_too(quiet, hedge_fast, monkeypatch):
    _route_to(monkeypatch, "slowp", "slow-model")
    _chain(monkeypatch, ("slowp", "slow-model"), ("fastp", "quick-model"))

    def silent():
        time.sleep(3)
        yield b'data: {"choices":[{"delta":{"content":"slow answer"}}]}'
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat", _slow_then_fast(
        calls, slow_s=0, slow_reply=lambda: _Resp(200, chunks=silent()),
        fast_reply=lambda: _Resp(200, chunks=list(LINE_ANSWER))))
    t0 = time.monotonic()
    r = A.app.test_client().post("/v1/responses", json={
        "model": "auto", "stream": True, "input": QUESTION})
    body = r.get_data()
    assert b"5768" in body and b"slow answer" not in body
    assert time.monotonic() - t0 < 1.5


def test_messages_hedges_too(quiet, hedge_fast, monkeypatch):
    _route_to(monkeypatch, "slowp", "slow-model")
    _chain(monkeypatch, ("slowp", "slow-model"), ("fastp", "quick-model"))
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat", _slow_then_fast(calls))
    t0 = time.monotonic()
    r = A.app.test_client().post("/v1/messages", json={
        "model": "claude-sonnet-4", "max_tokens": 64, "stream": False,
        "messages": [{"role": "user", "content": QUESTION}]})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert "5768" in r.get_data(as_text=True)
    assert time.monotonic() - t0 < 1.5


def test_every_chain_loop_can_hedge():
    src = open("app.py", encoding="utf-8").read()
    assert src.count("_clock.plan_hedge(_chain,") == 3
    assert src.count("_clock.served(hop_pid, hop_model, payload)") == 3
    assert src.count("if _clock.consumed(hop_pid, hop_model):") == 3
    assert src.count("_clock.note_peek(hop_pid, hop_model, status)") == 3
