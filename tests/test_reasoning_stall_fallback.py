"""A model that only reasons hands over (2026-10-11).

MEASURED (hub.log): Multi helper phases ended "opencode produced no reply"
after `[deadline] <pid>/<model>: cutting the committed stream (keepalive/
reasoning only past the deadline)`. The peek committed a stream on 400
reasoning frames, so the chain could never hand the turn to another model.

These tests drive the three real chain loops through the test client with
fake upstreams (tiny sleeps, no network): a hop that only reasons is abandoned
and the NEXT model serves inside the same request, a thinker that answers in
time keeps its turn with its reasoning intact, the stalled pair is filed, and
the flag off is the old behaviour.
"""
import json
import time

import pytest

import app as A


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

def _frame(delta, mode, finish=None):
    obj = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    line = b"data: " + json.dumps(obj).encode("utf-8")
    return line if mode == "lines" else line + b"\n\n"


def _done(mode):
    return b"data: [DONE]" if mode == "lines" else b"data: [DONE]\n\n"


class _Resp:
    """A streamed 200 whose body is produced by `factory(resp, mode)`; the
    body generator sees `closed` so an abandoned hop stops at once."""

    def __init__(self, factory):
        self.status_code = 200
        self.headers = {}
        self.text = ""
        self.closed = False
        self._factory = factory

    def json(self):
        return {}

    def close(self):
        self.closed = True

    def iter_content(self, chunk_size=None):
        return self._factory(self, "frames")

    def iter_lines(self, decode_unicode=False):
        return self._factory(self, "lines")


def _reasoning_forever(resp, mode):
    i = 0
    while not resp.closed and i < 200000:
        yield _frame({"reasoning_content": "thinking-one %d " % i}, mode)
        i += 1
        time.sleep(0.002)


def _answers(text):
    def factory(resp, mode):
        yield _frame({"role": "assistant", "content": text}, mode)
        yield _frame({}, mode, finish="stop")
        yield _done(mode)
    return factory


def _thinks_then_answers(n, text):
    def factory(resp, mode):
        for i in range(n):
            yield _frame({"reasoning_content": "r%d;" % i}, mode)
            time.sleep(0.001)
        yield _frame({"content": text}, mode)
        yield _frame({}, mode, finish="stop")
        yield _done(mode)
    return factory


def _reasons_then_ends(n):
    def factory(resp, mode):
        for i in range(n):
            yield _frame({"reasoning_content": "r%d;" % i}, mode)
        yield _done(mode)
    return factory


ANSWER = "Hello from model two: the design keeps one wall clock per request."


@pytest.fixture
def quiet(monkeypatch):
    """No disk ledgers, no activity bookkeeping: the tests are about which hop
    serves and what gets filed in memory."""
    for name in ("_record_chat_usage", "_save_perf_stats", "_act_pick", "_note_ttft",
                 "_record_stream_outcome", "_note_provider_timeout",
                 "_throttle_failed_hop"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)
    monkeypatch.setattr(A, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(A, "_model_block_reason", lambda pid, m: None)
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    monkeypatch.setattr(A, "_is_trivial_turn", lambda *a, **k: False)
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: 20)
    monkeypatch.setattr(A, "_stream_peek_timeout", lambda *a, **k: 0.3)
    monkeypatch.setattr(A, "_REASONING_STALL_SECONDS", 0.8)
    outcomes = []
    monkeypatch.setattr(A, "_record_outcome",
                        lambda pid, model, ok, **k: outcomes.append((pid, model, ok)))
    yield outcomes


def _fleet(monkeypatch, first, second):
    """p1/m1 opens the chain with `first`, p2/m2 is next with `second`."""
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("p1", "m1", "hard"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("p1", "m1"), ("p2", "m2")])
    calls = []

    def dispatch(pid, payload, stream):
        calls.append(pid)
        return _Resp(first if pid == "p1" else second)
    monkeypatch.setattr(A, "_dispatch_chat", dispatch)
    return calls


def _flag(monkeypatch, on):
    orig = A.config.get_flag

    def get_flag(name, default=False):
        if name == "reasoning_stall_fallback":
            return on
        return orig(name, default)
    monkeypatch.setattr(A.config, "get_flag", get_flag)


def _filed(outcomes):
    assert A._recent_hop_failure("p1", "m1") == "reasoning_stall"
    assert A._recent_hop_stall("p1", "m1")
    classes = [A._streak_event(e)[1] for e in A._empty_200.get(("p1", "m1"), ())]
    assert classes == ["reasoning_stall"]
    assert ("p1", "m1", False) in outcomes


# --------------------------------------------------------------------------- #
# End to end: a reasoning-only hop hands over, on all three protocols
# --------------------------------------------------------------------------- #

def test_chat_a_reasoning_only_hop_hands_over_in_the_same_request(quiet, monkeypatch):
    calls = _fleet(monkeypatch, _reasoning_forever, _answers(ANSWER))
    t0 = time.monotonic()
    r = A.app.test_client().post("/v1/chat/completions", json={
        "model": "auto", "stream": True,
        "messages": [{"role": "user", "content": "explain the design"}]})
    body = r.get_data()
    took = time.monotonic() - t0
    assert r.status_code == 200
    assert r.headers.get("X-Free-LLM-Hub-Provider") == "p2"
    assert b"model two" in body
    assert b"thinking-one" not in body      # the held reasoning never reached the client
    assert calls == ["p1", "p2"]
    assert took < 8.0                       # not the whole request clock
    _filed(quiet)


def test_responses_a_reasoning_only_hop_hands_over(quiet, monkeypatch):
    calls = _fleet(monkeypatch, _reasoning_forever, _answers(ANSWER))
    r = A.app.test_client().post("/v1/responses", json={
        "model": "auto", "stream": True, "input": "explain the design"})
    body = r.get_data()
    assert r.status_code == 200
    assert r.headers.get("X-Free-LLM-Hub-Provider") == "p2"
    assert b"response.output_text.delta" in body and b"model two" in body
    assert b"response.completed" in body
    assert calls == ["p1", "p2"]
    _filed(quiet)


def test_messages_a_reasoning_only_hop_hands_over(quiet, monkeypatch):
    calls = _fleet(monkeypatch, _reasoning_forever, _answers(ANSWER))
    r = A.app.test_client().post("/v1/messages", json={
        "model": "claude-sonnet-4", "max_tokens": 256, "stream": True,
        "messages": [{"role": "user", "content": "explain the design"}]})
    body = r.get_data()
    assert r.status_code == 200
    assert r.headers.get("X-Free-LLM-Hub-Provider") == "p2"
    assert b"text_delta" in body and b"model two" in body
    assert calls == ["p1", "p2"]
    _filed(quiet)


# --------------------------------------------------------------------------- #
# A thinker that answers inside its budget keeps the turn, reasoning intact
# --------------------------------------------------------------------------- #

def test_a_thinker_that_answers_in_time_is_not_cut_and_keeps_its_reasoning(quiet, monkeypatch):
    monkeypatch.setattr(A, "_REASONING_STALL_SECONDS", 6.0)
    # 500 reasoning frames: past the old 400-frame commit point AND past the
    # 0.3 s peek window, then visible text.
    calls = _fleet(monkeypatch, _thinks_then_answers(500, ANSWER), _answers("never"))
    r = A.app.test_client().post("/v1/chat/completions", json={
        "model": "auto", "stream": True,
        "messages": [{"role": "user", "content": "explain the design"}]})
    body = r.get_data()
    assert r.headers.get("X-Free-LLM-Hub-Provider") == "p1"
    assert calls == ["p1"]
    # Every held reasoning frame is replayed, once, in order, before the answer.
    text = body.decode("utf-8")
    pos = [text.find('"r%d;"' % i) for i in range(500)]
    assert all(p >= 0 for p in pos)
    assert pos == sorted(pos)
    assert text.count('"reasoning_content"') == 500
    assert text.find("model two") > pos[-1]
    assert A._recent_hop_failure("p1", "m1") is None
    assert ("p1", "m1", False) not in quiet


# --------------------------------------------------------------------------- #
# The flag
# --------------------------------------------------------------------------- #

def test_flag_off_keeps_the_old_commit_on_reasoning(quiet, monkeypatch):
    _flag(monkeypatch, False)
    # 450 reasoning frames, then the stream ends with nothing visible: the old
    # rule commits it at frame 400 (served by p1, the client gets nothing).
    calls = _fleet(monkeypatch, _reasons_then_ends(450), _answers(ANSWER))
    r = A.app.test_client().post("/v1/chat/completions", json={
        "model": "auto", "stream": True,
        "messages": [{"role": "user", "content": "explain the design"}]})
    r.get_data()
    assert r.headers.get("X-Free-LLM-Hub-Provider") == "p1"
    assert calls == ["p1"]
    with A.app.test_request_context():
        assert A._ChainClock().reasoning_budget() is None


def test_flag_on_the_same_stream_walks_on_to_the_next_model(quiet, monkeypatch):
    calls = _fleet(monkeypatch, _reasons_then_ends(450), _answers(ANSWER))
    r = A.app.test_client().post("/v1/chat/completions", json={
        "model": "auto", "stream": True,
        "messages": [{"role": "user", "content": "explain the design"}]})
    body = r.get_data()
    assert r.headers.get("X-Free-LLM-Hub-Provider") == "p2"
    assert b"model two" in body
    assert calls == ["p1", "p2"]


# --------------------------------------------------------------------------- #
# The peek itself
# --------------------------------------------------------------------------- #

def _frames(n):
    return [_frame({"reasoning_content": "r%d;" % i}, "frames") for i in range(n)]


def test_peek_old_rule_commits_on_400_reasoning_frames():
    status, buf = A._peek_until_content(iter(_frames(450) + [_done("frames")]), 5.0)
    assert status == "content"


def test_peek_hold_never_commits_on_reasoning_alone():
    status, _buf = A._peek_until_content(iter(_frames(450) + [_done("frames")]), 5.0,
                                         reasoning_budget=5.0)
    assert status == "empty"


def test_peek_hold_ends_a_reasoning_only_stream_at_the_budget():
    r = _Resp(_reasoning_forever)
    t0 = time.monotonic()
    status, buf = A._peek_until_content(r.iter_content(), 0.2, reasoning_budget=0.6)
    took = time.monotonic() - t0
    r.close()
    assert status == "reasoning_stall" and buf == []
    assert 0.55 <= took < 2.5


def test_peek_hold_never_extends_a_silent_hop():
    def silent():
        while True:
            yield b": keepalive\n\n"
            time.sleep(0.05)
    t0 = time.monotonic()
    status, _ = A._peek_until_content(silent(), 0.3, reasoning_budget=5.0)
    assert status == "timeout"
    assert time.monotonic() - t0 < 1.5


def test_peek_hold_is_bounded_in_memory(monkeypatch):
    monkeypatch.setattr(A, "_REASONING_HOLD_MAX_BYTES", 4000)
    r = _Resp(_reasoning_forever)
    t0 = time.monotonic()
    status, _ = A._peek_until_content(r.iter_content(), 0.2, reasoning_budget=30.0)
    r.close()
    assert status == "reasoning_stall"
    assert time.monotonic() - t0 < 5.0


# --------------------------------------------------------------------------- #
# The budget
# --------------------------------------------------------------------------- #

def _clock(monkeypatch, deadline, peek):
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: deadline)
    c = A._ChainClock()
    c._last_peek = peek
    return c


def test_budget_is_150s_capped_at_60_percent_of_the_clock(monkeypatch):
    with A.app.test_request_context():
        b = _clock(monkeypatch, 240, 60).reasoning_budget()
        assert 143.0 < b <= 144.0           # 0.6 x 240 leaves 96 s for the next model
    with A.app.test_request_context():
        assert _clock(monkeypatch, 600, 60).reasoning_budget() == pytest.approx(150.0)


def test_budget_never_below_the_peek_floor_and_never_past_the_clock(monkeypatch):
    with A.app.test_request_context():
        assert _clock(monkeypatch, 100, 90).reasoning_budget() == pytest.approx(90.0, abs=0.5)
    with A.app.test_request_context():
        b = _clock(monkeypatch, 20, 30).reasoning_budget()
        assert b <= 20.0


def test_budget_respects_the_hop_budget(monkeypatch):
    with A.app.test_request_context():
        c = _clock(monkeypatch, 240, 10)
        c._hop_budget, c._hop_started = 25.0, time.monotonic()
        assert 24.0 < c.reasoning_budget() <= 25.0


# --------------------------------------------------------------------------- #
# Filing, gates, hedges, non-stream
# --------------------------------------------------------------------------- #

def test_three_stalls_rest_the_pair_for_tool_turns(monkeypatch):
    monkeypatch.setattr(A, "_record_outcome", lambda *a, **k: None)
    for _ in range(3):
        A._rs_note_stall("pz", "mz", 150)
    assert A._pair_resting("pz", "mz")
    assert A._tool_turn_sick("pz", "mz")


def test_a_client_that_left_or_a_local_network_failure_files_nothing(monkeypatch):
    seen = []
    monkeypatch.setattr(A, "_record_outcome", lambda *a, **k: seen.append(a))
    monkeypatch.setattr(A, "_client_gone", lambda: True)
    A._rs_note_stall("pg", "mg", 10)
    assert A._recent_hop_failure("pg", "mg") is None and not seen
    monkeypatch.setattr(A, "_client_gone", lambda: False)
    monkeypatch.setattr(A, "_local_net_failed", lambda pid, model: True)
    A._rs_note_stall("pg", "mg", 10)
    assert A._recent_hop_failure("pg", "mg") is None and not seen


def test_a_hedge_leg_that_only_reasons_never_wins(monkeypatch):
    assert A._rs_leg_budget(3.0) == 3.0
    item = (1, "peek", _Resp(_answers("x")), ("reasoning_stall", [], None))
    assert A._hedge_leg_verdict(item, {}) == "reasoning_stall"
    _flag(monkeypatch, False)
    assert A._rs_leg_budget(3.0) is None


def test_a_buffered_reply_that_only_reasoned_is_empty():
    data = {"choices": [{"index": 0, "finish_reason": "stop", "message": {
        "role": "assistant", "content": "", "reasoning_content": "long thoughts"}}]}
    assert A._chat_json_is_empty(data)


# --------------------------------------------------------------------------- #
# Security review of 488d4fb: the hold cap counts EVERY frame. Checked only on
# reasoning frames, ONE reasoning delta followed by endless empty or keep-alive
# frames grew the held buffer without bound for the whole hold budget.
# --------------------------------------------------------------------------- #

def _one_reasoning_then(filler):
    def gen():
        yield _frame({"reasoning_content": "thinking"}, "frames")
        while True:
            yield filler
    return gen()


@pytest.mark.parametrize("filler", [b"", b": keepalive\n\n",
                                    b'data: {"choices":[{"delta":{}}]}\n\n'])
def test_hold_cap_counts_every_frame_not_only_reasoning(monkeypatch, filler):
    monkeypatch.setattr(A, "_REASONING_HOLD_MAX_BYTES", 4000)
    t0 = time.monotonic()
    status, buf = A._peek_until_content(_one_reasoning_then(filler), 5.0,
                                        reasoning_budget=30.0)
    assert status == "reasoning_stall"
    # bounded by the cap (each frame costs at least the fixed overhead), not by
    # the 30 s budget
    assert len(buf) <= 4000 // A._REASONING_HOLD_FRAME_OVERHEAD + 2
    assert time.monotonic() - t0 < 5.0


def test_hold_cap_without_any_reasoning_is_an_empty_hop(monkeypatch):
    monkeypatch.setattr(A, "_REASONING_HOLD_MAX_BYTES", 2000)

    def junk():
        while True:
            yield b"x" * 500
    status, _buf = A._peek_until_content(junk(), 5.0, reasoning_budget=30.0)
    assert status == "empty"
