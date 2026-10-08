"""One wall clock per request, and trivial turns that do not wait on slow hops.

MEASURED in a live sweep before this existed:

  * "What is N plus 1? Answer with only the number." timed out at the client's
    180s in the reasoning, seo and specialist category modes -- categories made
    almost entirely of slow reasoning models.
  * "Use the add tool to add 17 and 25" took 82-112s in auto/best/coding/fast,
    and 962s with coding-max: bytes kept trickling, so the client's 180s read
    timeout never fired and the hub held the turn for ~16 minutes.

Every timeout the hub had was PER something (per recv, per chunk gap, per hop's
first content), so they compounded without a ceiling. These tests drive the
three chain loops with fake providers -- tiny sleeps, no network -- and pin:
the request deadline (setting request_deadline_seconds), the tight hop budget
for trivial small turns, the cut of a committed stream that only trickles past
the deadline, the routing that prefers fast models for trivial asks, and the
outer bound on prose pipelines.
"""
import time

import pytest
import requests

import app as A


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class _Resp:
    """The subset of a requests.Response the hop loops touch."""

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


def _answer(text="42"):
    return {"choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}]}


@pytest.fixture
def quiet(monkeypatch):
    """No ledger writes, no activity rows, no provider readiness checks: the
    tests are about time, and the real bookkeeping touches the state dir."""
    for name in ("_record_chat_usage", "_record_outcome", "_save_perf_stats",
                 "_act_pick", "_note_ttft", "_record_stream_outcome",
                 "_note_provider_timeout", "_throttle_failed_hop"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)
    monkeypatch.setattr(A, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(A, "_model_block_reason", lambda pid, m: None)
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    yield


def _route_to(monkeypatch, pid, model, difficulty):
    monkeypatch.setattr(A, "_route_by_difficulty",
                        lambda *a, **k: (pid, model, difficulty))


# --------------------------------------------------------------------------- #
# The setting
# --------------------------------------------------------------------------- #

def test_the_default_deadline_is_under_a_typical_client_timeout(monkeypatch):
    monkeypatch.setattr(A.config, "get_setting", lambda k, d=None: d)
    secs = A._request_deadline_seconds()
    assert secs == 240
    # Same budget the streaming header guard already uses: the client must
    # see a status before its own ~300s header timeout.
    assert secs <= A._STREAM_HEADER_BUDGET


def test_zero_means_unbounded(monkeypatch):
    monkeypatch.setattr(A.config, "get_setting", lambda k, d=None: 0)
    assert A._request_deadline_seconds() is None


def test_a_broken_setting_keeps_the_bound(monkeypatch):
    monkeypatch.setattr(A.config, "get_setting", lambda k, d=None: "nonsense")
    assert A._request_deadline_seconds() == 240


def test_the_clock_starts_once_per_request(monkeypatch):
    """/v1/responses re-enters itself for its transient-storm retry; that retry
    is the same request to codex and must not get a fresh four minutes."""
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: 100)
    with A.app.test_request_context():
        first = A._begin_request_deadline()
        time.sleep(0.01)
        assert A._begin_request_deadline() == first


# --------------------------------------------------------------------------- #
# Primitives
# --------------------------------------------------------------------------- #

def test_the_wall_clock_returns_a_fast_answer():
    assert A._call_with_wall_clock(1.0, lambda: "ok") == "ok"


def test_the_wall_clock_reraises_what_the_call_raised():
    def boom():
        raise requests.exceptions.ConnectionError("refused")
    with pytest.raises(requests.exceptions.ConnectionError):
        A._call_with_wall_clock(1.0, boom)


def test_the_wall_clock_stops_waiting_and_closes_the_late_answer():
    late = _Resp()

    def slow():
        time.sleep(0.3)
        return late
    t0 = time.monotonic()
    with pytest.raises(A._HopBudgetExceeded):
        A._call_with_wall_clock(0.05, slow)
    assert time.monotonic() - t0 < 0.25
    time.sleep(0.5)
    assert late.closed, "an abandoned response must give its connection back"


def test_our_own_impatience_is_not_a_provider_timeout():
    """A requests Timeout throttles the hop and counts toward parking its
    provider; the hub walking away from a one-liner is not that evidence."""
    exc = A._HopBudgetExceeded("x")
    assert not isinstance(exc, requests.exceptions.Timeout)
    assert isinstance(exc, RuntimeError)       # the hop loops catch RuntimeError
    assert A._classify_hop_error(exc=exc) == "deadline"


def test_trivial_turn_detection():
    small = [{"role": "user", "content": "What is 5767 plus 1? Answer with only the number."}]
    assert A._is_trivial_turn(small, None, "simple", 400)
    # best/max lifts a simple ask to medium to reach a strong model; the ask
    # is no less trivial for it.
    assert A._is_trivial_turn(small, None, "medium", 400)
    assert not A._is_trivial_turn(small, None, "simple", 400, pinned=True)
    assert not A._is_trivial_turn(small, None, "simple", A.STREAM_BIG_REQUEST_TOKENS)
    assert not A._is_trivial_turn(small, None, "hard", 400)
    creation = [{"role": "user", "content": "build me a landing page"}]
    assert not A._is_trivial_turn(creation, None, "medium", 400)


def test_hop_budgets(monkeypatch):
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: None)
    with A.app.test_request_context():
        c = A._ChainClock(trivial=True)
        assert c._budget_for("groq", "llama-3.3-70b") == A._TRIVIAL_HOP_BUDGET
        assert c._budget_for("nvidia", "deepseek-r1") == A._TRIVIAL_SLOW_HOP_BUDGET
        # A local CLI relay cold-starts in tens of seconds whatever the ask.
        assert c._budget_for("sub-codex", "gpt-5") is None
        assert A._ChainClock(trivial=False)._budget_for("groq", "x") is None


def test_the_hop_budget_never_outlives_the_deadline(monkeypatch):
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: 5)
    with A.app.test_request_context():
        c = A._ChainClock(trivial=True)
        assert c._budget_for("groq", "llama") <= 5
        assert A._ChainClock(trivial=False)._budget_for("groq", "llama") <= 5


# --------------------------------------------------------------------------- #
# The committed-stream guard
# --------------------------------------------------------------------------- #

CONTENT = b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
REASONING = b'data: {"choices":[{"delta":{"reasoning_content":"hmm"}}]}\n\n'
KEEPALIVE = b": keepalive\n\n"
DONE = b"data: [DONE]\n\n"


def test_no_deadline_is_a_plain_passthrough():
    items = [CONTENT, REASONING, KEEPALIVE]
    assert list(A._deadline_guard(iter(items), None, b"T")) == items


def test_before_the_deadline_everything_passes():
    items = [CONTENT, REASONING, KEEPALIVE, CONTENT, DONE]
    far = time.monotonic() + 60
    assert list(A._deadline_guard(iter(items), far, b"T")) == items


def test_past_the_deadline_a_trickle_is_cut_but_real_content_is_not():
    """A model mid-way through writing keeps writing; a reasoning-only or
    keepalive trickle -- the 16-minute hostage -- ends the stream cleanly."""
    past = time.monotonic() - 1
    out = list(A._deadline_guard(iter([CONTENT, b"", CONTENT, REASONING, CONTENT]),
                                 past, b"T"))
    assert out == [CONTENT, b"", CONTENT, b"T"]


def test_past_the_deadline_the_finish_still_arrives():
    past = time.monotonic() - 1
    fin = b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    assert list(A._deadline_guard(iter([CONTENT, fin, DONE]), past, b"T")) \
        == [CONTENT, fin, DONE]


def test_past_the_deadline_a_trailing_usage_frame_is_not_a_cut():
    """include_usage sends usage as its own frame AFTER the real finish; it
    must pass, and no fake finish_reason "length" may follow the real one."""
    past = time.monotonic() - 1
    fin = b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n'
    usage = b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n'
    assert list(A._deadline_guard(iter([CONTENT, fin, usage, DONE]), past, b"T")) \
        == [CONTENT, fin, usage, DONE]
    # ...and even a keepalive after the finish is never answered with a terminator
    assert list(A._deadline_guard(iter([fin, REASONING, DONE]), past, b"T")) \
        == [fin, REASONING, DONE]


# Distinct sentences: one sentence x80 is itself a runaway loop, which the
# stream answer gate (_StreamAnswerGate) now trims before the deadline matters.
_PROSE = b"".join(b"More text %d here. " % i for i in range(80))


def _trickle_lines(first):
    def gen():
        yield first
        for _ in range(2000):            # ~100s if nothing cut it
            time.sleep(0.05)
            yield REASONING.strip()
    return gen


def test_a_deadline_cut_responses_stream_ends_incomplete(quiet, monkeypatch):
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: 0.5)
    monkeypatch.setattr(A, "_is_trivial_turn", lambda *a, **k: False)
    _route_to(monkeypatch, "p1", "m1", "hard")
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("p1", "m1")])
    first = (b'data: {"choices":[{"delta":{"content":"The answer is forty-two. '
             + _PROSE + b'"}}]}')
    monkeypatch.setattr(A, "_dispatch_chat",
                        lambda pid, payload, stream: _Resp(200, chunks=_trickle_lines(first)()))
    r = A.app.test_client().post("/v1/responses", json={
        "model": "auto", "stream": True, "input": "explain the design"})
    body = r.get_data()
    assert b"response.incomplete" in body
    assert b'"max_output_tokens"' in body
    assert b"response.completed" not in body


def test_a_deadline_cut_messages_stream_ends_max_tokens(quiet, monkeypatch):
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: 0.5)
    monkeypatch.setattr(A, "_is_trivial_turn", lambda *a, **k: False)
    _route_to(monkeypatch, "p1", "m1", "hard")
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("p1", "m1")])
    first = (b'data: {"choices":[{"delta":{"content":"The answer is forty-two. '
             + _PROSE + b'"}}]}')
    monkeypatch.setattr(A, "_dispatch_chat",
                        lambda pid, payload, stream: _Resp(200, chunks=_trickle_lines(first)()))
    r = A.app.test_client().post("/v1/messages", json={
        "model": "claude-sonnet-4", "max_tokens": 64, "stream": True,
        "messages": [{"role": "user", "content": "explain the design"}]})
    body = r.get_data()
    assert b'"stop_reason": "max_tokens"' in body or b'"stop_reason":"max_tokens"' in body
    assert b"end_turn" not in body


def test_past_the_deadline_silence_is_cut(monkeypatch):
    monkeypatch.setattr(A, "_POST_DEADLINE_IDLE", 0.2)

    def hangs():
        yield CONTENT
        time.sleep(3)
        yield CONTENT
    t0 = time.monotonic()
    out = list(A._deadline_guard(hangs(), time.monotonic() - 1, b"T"))
    assert out == [CONTENT, b"T"]
    assert time.monotonic() - t0 < 1.5


def test_a_blocked_read_cannot_carry_the_stream_past_the_deadline(monkeypatch):
    monkeypatch.setattr(A, "_POST_DEADLINE_IDLE", 0.1)

    def blocks():
        yield CONTENT
        time.sleep(5)
        yield CONTENT
    t0 = time.monotonic()
    out = list(A._deadline_guard(blocks(), time.monotonic() + 0.2, None))
    assert out == [CONTENT]
    assert time.monotonic() - t0 < 1.5


def test_the_upstream_error_still_propagates():
    def dies():
        yield CONTENT
        raise requests.exceptions.ChunkedEncodingError("reset")
    with pytest.raises(requests.exceptions.ChunkedEncodingError):
        list(A._deadline_guard(dies(), time.monotonic() + 60, b"T"))


# --------------------------------------------------------------------------- #
# End to end: the three chain loops
# --------------------------------------------------------------------------- #

def _hung(seconds=3.0, reply=None):
    def call(pid, payload, stream):
        time.sleep(seconds)
        return reply or _Resp(200, _answer("too late"))
    return call


def test_a_trivial_turn_walks_past_a_slow_hop_in_seconds(quiet, monkeypatch):
    """The 82-112s shape: hop one sits on its whole peek budget before hop
    two -- which answers at once -- is even tried."""
    monkeypatch.setattr(A, "_TRIVIAL_HOP_BUDGET", 0.3)
    monkeypatch.setattr(A, "_TRIVIAL_SLOW_HOP_BUDGET", 0.3)
    throttled = []
    monkeypatch.setattr(A.quota, "mark_model_throttled",
                        lambda *a, **k: throttled.append(a))
    _route_to(monkeypatch, "slowp", "deep-reasoning-model", "simple")
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [
        ("slowp", "deep-reasoning-model"), ("fastp", "quick-model")])
    hung = _hung()

    def dispatch(pid, payload, stream):
        if pid == "slowp":
            return hung(pid, payload, stream)
        return _Resp(200, _answer("5768"))
    monkeypatch.setattr(A, "_dispatch_chat", dispatch)
    t0 = time.monotonic()
    r = A.app.test_client().post("/v1/chat/completions", json={
        "model": "auto", "stream": False,
        "messages": [{"role": "user",
                      "content": "What is 5767 plus 1? Answer with only the number."}]})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["choices"][0]["message"]["content"] == "5768"
    assert time.monotonic() - t0 < 2.5
    assert r.headers.get("X-Free-LLM-Hub-Last-Error") == "deadline"
    assert not throttled, "the hub's impatience must not cool the model down"


def test_a_trivial_streamed_turn_gets_a_tight_first_content_peek(quiet, monkeypatch):
    """A 200 whose first content never comes costs the budget, not 35-60s."""
    monkeypatch.setattr(A, "_TRIVIAL_HOP_BUDGET", 0.3)
    monkeypatch.setattr(A, "_TRIVIAL_SLOW_HOP_BUDGET", 0.3)
    _route_to(monkeypatch, "slowp", "slow-model", "simple")
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [
        ("slowp", "slow-model"), ("fastp", "quick-model")])

    def silent():
        time.sleep(5)
        yield CONTENT
    answer = (b'data: {"choices":[{"delta":{"content":"' + b"5768 " * 130 + b'"}}]}\n\n'
              + b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n' + DONE)

    def dispatch(pid, payload, stream):
        if pid == "slowp":
            return _Resp(200, chunks=silent())
        return _Resp(200, chunks=[answer])
    monkeypatch.setattr(A, "_dispatch_chat", dispatch)
    t0 = time.monotonic()
    r = A.app.test_client().post("/v1/chat/completions", json={
        "model": "auto", "stream": True,
        "messages": [{"role": "user", "content": "What is 5767 plus 1?"}]})
    body = r.get_data()
    assert r.status_code == 200
    assert b"5768" in body
    assert time.monotonic() - t0 < 2.5


def test_the_request_deadline_ends_a_hop_chain_with_a_clean_504(quiet, monkeypatch):
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: 0.4)
    monkeypatch.setattr(A, "_is_trivial_turn", lambda *a, **k: False)
    _route_to(monkeypatch, "p1", "m1", "hard")
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [
        ("p1", "m1"), ("p2", "m2"), ("p3", "m3"), ("p4", "m4")])
    calls = []

    def dispatch(pid, payload, stream):
        calls.append(pid)
        time.sleep(3)
        return _Resp(200, _answer())
    monkeypatch.setattr(A, "_dispatch_chat", dispatch)
    t0 = time.monotonic()
    r = A.app.test_client().post("/v1/chat/completions", json={
        "model": "auto", "stream": False,
        "messages": [{"role": "user", "content": "refactor the parser module"}]})
    assert r.status_code == 504
    assert "deadline" in r.get_json()["error"]["message"]
    assert r.headers.get("X-Free-LLM-Hub-Last-Error") == "deadline"
    assert time.monotonic() - t0 < 2.0
    assert calls == ["p1"], "no hop may start once the deadline is spent"


def test_a_committed_stream_that_only_trickles_is_cut_at_the_deadline(quiet, monkeypatch):
    """The coding-max shape: content committed the stream, then the upstream
    kept the socket alive with reasoning deltas -- the client's read timeout
    never fires. Past the deadline the trickle is cut, and the client is told
    the answer was truncated rather than left waiting."""
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: 0.5)
    monkeypatch.setattr(A, "_is_trivial_turn", lambda *a, **k: False)
    _route_to(monkeypatch, "p1", "m1", "hard")
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("p1", "m1")])
    # Distinct sentences: 30 copies of ONE sentence is itself a runaway loop,
    # which the stream answer gate (_StreamAnswerGate) now trims.
    first = (b'data: {"choices":[{"delta":{"content":"'
             + b"".join(b"Point %d of the design holds. " % i for i in range(30))
             + b'"}}]}\n\n')

    def trickle():
        yield first
        for _ in range(2000):            # ~100s if nothing cut it
            time.sleep(0.05)
            yield REASONING
    monkeypatch.setattr(A, "_dispatch_chat",
                        lambda pid, payload, stream: _Resp(200, chunks=trickle()))
    t0 = time.monotonic()
    r = A.app.test_client().post("/v1/chat/completions", json={
        "model": "auto", "stream": True,
        "messages": [{"role": "user", "content": "explain the design"}]})
    body = r.get_data()
    assert time.monotonic() - t0 < 4.0
    assert body.startswith(first)
    assert b'"finish_reason":"length"' in body
    assert body.rstrip().endswith(b"data: [DONE]")


def test_responses_honours_the_deadline(quiet, monkeypatch):
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: 0.4)
    monkeypatch.setattr(A, "_is_trivial_turn", lambda *a, **k: False)
    _route_to(monkeypatch, "p1", "m1", "hard")
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("p1", "m1"), ("p2", "m2")])
    monkeypatch.setattr(A, "_dispatch_chat", _hung())
    t0 = time.monotonic()
    r = A.app.test_client().post("/v1/responses", json={
        "model": "auto", "stream": False, "input": "refactor the parser module"})
    assert r.status_code == 504
    assert r.headers.get("X-Free-LLM-Hub-Last-Error") == "deadline"
    assert time.monotonic() - t0 < 2.0


def test_messages_honours_the_deadline(quiet, monkeypatch):
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: 0.4)
    monkeypatch.setattr(A, "_is_trivial_turn", lambda *a, **k: False)
    _route_to(monkeypatch, "p1", "m1", "hard")
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("p1", "m1"), ("p2", "m2")])
    monkeypatch.setattr(A, "_dispatch_chat", _hung())
    t0 = time.monotonic()
    r = A.app.test_client().post("/v1/messages", json={
        "model": "claude-sonnet-4", "max_tokens": 64, "stream": False,
        "messages": [{"role": "user", "content": "refactor the parser module"}]})
    assert r.status_code == 504
    assert r.get_json()["type"] == "error"
    assert time.monotonic() - t0 < 2.0


def test_every_chain_loop_runs_on_the_clock():
    """A loop left on the bare _dispatch_chat would be the one CLI that still
    hangs -- and nobody would test it."""
    src = open("app.py", encoding="utf-8").read()
    assert src.count("_clock = _ChainClock(") == 3
    assert src.count("if _clock.spent():") == 6        # hop top + after the loop, x3
    assert src.count("resp = _dispatch_chat(hop_pid, payload, dispatch_stream)") == 0
    assert src.count("resp = _dispatch_chat(hop_pid, payload, stream)") == 0
    # pid rides along so measured TTFT can class the hop slow (_is_slow_model)
    assert src.count("_clock.peek_timeout(hop_model, est, pid=hop_pid)") == 3
    assert src.count("_clock.guard(_chain_buffered(") == 3


# --------------------------------------------------------------------------- #
# Routing: trivial asks prefer fast models
# --------------------------------------------------------------------------- #

@pytest.fixture
def fleet(monkeypatch):
    world = {"pa": ["pa-r1", "pa-chat"], "pb": ["pb-r1-mini", "pb-chat"],
             "pc": ["pc-r-fast"]}
    fast = {"pa-chat", "pb-chat", "pc-r-fast"}
    in_cat = {"pa-r1", "pb-r1-mini", "pc-r-fast"}
    latency = {"pa-r1": 90000.0, "pb-r1-mini": 12000.0}
    monkeypatch.setattr(A, "_available_providers", lambda *a, **k: list(world))
    monkeypatch.setattr(A, "_prefetch_auto_models", lambda pids: dict(world))
    monkeypatch.setattr(A, "_auto_models", lambda pid: list(world[pid]))
    monkeypatch.setattr(A, "_provider_capable", lambda pid, est: True)
    monkeypatch.setattr(A, "_supports_tools", lambda pid, m: True)
    monkeypatch.setattr(A, "_session_pin_get", lambda key: None)
    monkeypatch.setattr(A, "_session_pin_set", lambda *a, **k: None)
    monkeypatch.setattr(A, "_is_fast", lambda pid, m: m in fast)
    monkeypatch.setattr(A, "_measured_latency_ms", lambda pid, m: latency.get(m))
    monkeypatch.setattr(A.model_categories, "matches",
                        lambda key, p, m, i=None: m in in_cat)
    return world


SIMPLE = [{"role": "user", "content": "What is 5767 plus 1? Answer with only the number."}]


def test_a_simple_ask_in_an_all_slow_category_takes_its_quickest_model(fleet, monkeypatch):
    """reasoning/seo/specialist: nothing fast in the category. The cheapest
    qualified model used to win, whatever its speed."""
    scores = {"pa-r1": 60.0, "pb-r1-mini": 100.0}
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: scores.get(m, 100.0))
    monkeypatch.setattr(A.model_categories, "matches",
                        lambda key, p, m, i=None: m in scores)
    monkeypatch.setattr(A, "_active_mode", lambda: "reasoning")
    pid, model, diff = A._route_by_difficulty(SIMPLE, None, 400)
    assert diff == "simple"
    assert (pid, model) == ("pb", "pb-r1-mini")


def test_a_simple_chain_leaves_the_category_before_its_slow_models(fleet, monkeypatch):
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: 100.0)
    monkeypatch.setattr(A, "_active_mode", lambda: "reasoning")
    with A.app.test_request_context():
        A._mark_turn_shape("simple", 400)
        chain = A._build_chain("", "", 400, messages=SIMPLE)
    models = [m for _p, m in chain]
    assert models[0] == "pc-r-fast", models          # the category's own fast model
    assert {"pa-chat", "pb-chat"} <= set(models), models
    first_slow = min(models.index("pa-r1"), models.index("pb-r1-mini"))
    assert models.index("pa-chat") < first_slow and models.index("pb-chat") < first_slow
    # ...and the category's slow models quickest first.
    assert models.index("pb-r1-mini") < models.index("pa-r1")


def test_a_non_simple_chain_keeps_the_category(fleet, monkeypatch):
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: 100.0)
    monkeypatch.setattr(A, "_active_mode", lambda: "reasoning")
    with A.app.test_request_context():
        A._mark_turn_shape("hard", 400)
        chain = A._build_chain("", "", 400, messages=SIMPLE)
    # Every category model before any model outside it: the out-of-category
    # hops are only the fail-open tail (see _mode_fallback_tail).
    inside = [m in {"pa-r1", "pb-r1-mini", "pc-r-fast"} for _p, m in chain]
    assert inside[:3] == [True, True, True], chain
    assert inside == sorted(inside, reverse=True), chain


def test_a_trivial_tool_turn_prefers_a_fast_tool_model(fleet, monkeypatch):
    """The agentic pool skips the fast prefilter on purpose; for "add 17 and
    25" that handed the turn to a strong reasoning model."""
    scores = {"pa-r1": 140.0, "pb-r1-mini": 135.0, "pa-chat": 100.0, "pb-chat": 95.0,
              "pc-r-fast": 92.0}
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: scores[m])
    monkeypatch.setattr(A, "_active_mode", lambda: A.MODE_ALL)
    monkeypatch.setattr(A, "_may_lead_agentic", lambda s, m: True)
    monkeypatch.setattr(A, "_weighted_pick", lambda pool, *a, **k: max(pool))
    msgs = [{"role": "user", "content": "Use the add tool to add 17 and 25"}]
    pid, model, diff = A._route_by_difficulty(msgs, None, 600, require_tools=True)
    assert diff == "simple"
    assert model in {"pa-chat", "pb-chat", "pc-r-fast"}, model
    # A real (big) agent turn keeps the strength-first pick.
    _p, big, _d = A._route_by_difficulty(msgs, None, A.STREAM_BIG_REQUEST_TOKENS + 1,
                                        require_tools=True)
    assert big == "pa-r1"


def test_a_trivial_tool_chain_walks_fast_models_first(fleet, monkeypatch):
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: 140.0 if "r1" in m else 100.0)
    monkeypatch.setattr(A, "_active_mode", lambda: A.MODE_ALL)
    with A.app.test_request_context():
        A._mark_turn_shape("simple", 400)
        chain = A._build_chain("", "", 400, require_tools=True, messages=SIMPLE)
    models = [m for _p, m in chain]
    fast_idx = [models.index(m) for m in ("pa-chat", "pb-chat", "pc-r-fast")]
    slow_idx = [models.index(m) for m in ("pa-r1", "pb-r1-mini")]
    assert max(fast_idx) < min(slow_idx), models


def test_outside_a_request_the_chain_order_is_unchanged(fleet, monkeypatch):
    """Probes and pipelines build chains with no request behind them."""
    assert A._simple_turn() is False


# --------------------------------------------------------------------------- #
# Pipelines keep their own cap, inside an outer bound
# --------------------------------------------------------------------------- #

def test_the_pipeline_outer_bound(monkeypatch):
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: 240)
    assert A._pipeline_outer_bound(180) == 180 + A._PIPELINE_OUTER_GRACE
    assert A._pipeline_outer_bound(None) == 240 + A._PIPELINE_OUTER_GRACE
    assert A._pipeline_outer_bound(10 ** 6) == A._PIPELINE_OUTER_MAX


def _stage_fakes(monkeypatch, seen):
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("p1", "m1", "hard"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("p1", "m1"), ("p2", "m2")])

    def dispatch(pid, payload, deadline=None):
        seen.append(deadline)
        return _Resp(200, _answer("stage text")), None
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", dispatch)
    monkeypatch.setattr(A, "_record_chat_usage", lambda *a, **k: None)
    monkeypatch.setattr(A, "_act_pick", lambda *a, **k: None)


def test_a_spent_pipeline_starts_no_stage_hop(monkeypatch):
    seen = []
    _stage_fakes(monkeypatch, seen)
    tok = A._PIPELINE_DEADLINE.set(time.monotonic() - 1)
    try:
        assert A._swarm_dispatch([{"role": "user", "content": "x"}], 100) == ("", None)
    finally:
        A._PIPELINE_DEADLINE.reset(tok)
    assert seen == []


def test_a_stage_hop_is_cut_to_what_the_pipeline_has_left(monkeypatch):
    seen = []
    _stage_fakes(monkeypatch, seen)
    tok = A._PIPELINE_DEADLINE.set(time.monotonic() + 20)
    try:
        text, who = A._swarm_dispatch([{"role": "user", "content": "x"}], 100)
    finally:
        A._PIPELINE_DEADLINE.reset(tok)
    assert text == "stage text" and who == "p1/m1"
    assert seen and seen[0] <= 20


def test_outside_a_pipeline_a_stage_keeps_its_own_deadline(monkeypatch):
    seen = []
    _stage_fakes(monkeypatch, seen)
    A._swarm_dispatch([{"role": "user", "content": "x"}], 100)
    assert seen == [A._SWARM_HOP_DEADLINE]


def test_one_routed_pick_does_not_make_its_provider_a_hog():
    """Fairness reads a provider's share of recent picks; after ONE pick that
    share was 100% and the next turn left the best model for a weaker one."""
    with A._route_log_lock:
        A._ROUTE_LOG.clear()
    A._note_route_pick("pa")
    assert A._provider_recent_share("pa") == 0.0
    for _ in range(A._FAIR_MIN_PICKS):
        A._note_route_pick("pa")
    assert A._provider_recent_share("pa") == 1.0
    with A._route_log_lock:
        A._ROUTE_LOG.clear()


def test_a_loaded_host_never_hands_a_simple_turn_to_a_weaker_quick_model(fleet, monkeypatch):
    """The quick pick narrows by load only inside the top band."""
    scores = {"pa-r1": 140.0, "pb-r1-mini": 135.0, "pa-chat": 100.0, "pb-chat": 90.0,
              "pc-r-fast": 80.0}
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: scores[m])
    monkeypatch.setattr(A, "_active_mode", lambda: A.MODE_ALL)
    monkeypatch.setattr(A, "_may_lead_agentic", lambda s, m: True)
    with A._inflight_lock:
        A._PROVIDER_INFLIGHT["pa"] = 9          # pa far over the soft cap
    try:
        msgs = [{"role": "user", "content": "Use the add tool to add 17 and 25"}]
        _pid, model, diff = A._route_by_difficulty(msgs, None, 600, require_tools=True)
    finally:
        with A._inflight_lock:
            A._PROVIDER_INFLIGHT.clear()
    assert diff == "simple"
    assert model == "pa-chat", model
