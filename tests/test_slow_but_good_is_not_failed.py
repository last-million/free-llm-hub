"""A good but slow model is not a failed one.

MEASURED 2026-11-07: nvidia glm-5.3-flash's first token on tool turns is 48-130 s
(median ~90 s); the best/max fallback mark on a big request is 108 s, and a hop
that starts late gets only what is left of it. The hub's own cut was filed as the
model's failure and fed the failure streak (rests of 30 min -> 6 h), so the
strongest models were kept off the chain by the hub's impatience.

1. A hop cut EARLIER than the pair's own measured median first-content time keeps
   the short ordering mark but does not feed the persisted streak.
2. A pair with a real record may keep waiting to about its own p90 when that is
   longer than the room the mark leaves: once per walk, never for a trivial ask,
   never for a pair that is failing, and never past a bounded share of the deadline.

Fakes only; no network, no sleeping.
"""
import time

import pytest

import app as A

BIG = 145671
SLOW = ("nv", "glm-slow")          # answers in 80-110 s
OTHER = ("nv", "other-slow")
QUICK = ("fastp", "quick")
ASK = [{"role": "system", "content": "agent"},
       {"role": "user", "content": "build it"},
       {"role": "assistant", "content": "ok"},
       {"role": "user", "content": "continue a very long and detailed instruction "
                                   "that is certainly not a trivial one-liner at all, "
                                   "it asks for several parts of work to be done."}]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: 240)
    monkeypatch.setattr(A, "_is_fast", lambda p, m: m == "quick")
    monkeypatch.setattr(A, "_recent_hop_failure", lambda p, m: None)
    monkeypatch.setattr(A, "_chain_reliability_band", lambda p, m: 0)
    with A._outcome_lock:
        A._tool_ttft.clear()
        A._ttft.clear()
    yield
    with A._outcome_lock:
        A._tool_ttft.clear()
        A._ttft.clear()


def _measure(pair, ms_list, tool=True):
    with A._outcome_lock:
        (A._tool_ttft if tool else A._ttft)[pair] = list(ms_list)


SLOW_MS = [80000, 85000, 90000, 92000, 95000, 100000, 105000]


LATE_ROOM = 40.0     # seconds of the mark left when the hop starts (an earlier hop used the rest)


def _clock(tools=True, est=BIG, late=True):
    clock = A._ChainClock(est=est, tools=tools)
    clock.plan_quality_fallback("max->auto", ASK)
    clock._rest = [QUICK, OTHER]
    if late:
        clock._qf["at"] = time.monotonic() + LATE_ROOM
    return clock


def _room(clock):
    return clock._qf["at"] - time.monotonic()


# --- 1. a cut below the pair's own median is not a streak ------------------

def test_a_cut_below_the_measured_median_is_too_short():
    _measure(SLOW, SLOW_MS)
    assert A._qf_cut_too_short(*SLOW, 25.0, True) is True
    assert A._qf_cut_too_short(*SLOW, 89.0, True) is True


def test_a_cut_after_the_median_is_real_evidence():
    _measure(SLOW, SLOW_MS)
    assert A._qf_cut_too_short(*SLOW, 95.0, True) is False
    assert A._qf_cut_too_short(*SLOW, 200.0, True) is False


def test_an_unmeasured_or_thinly_measured_pair_is_judged_as_before():
    assert A._qf_cut_too_short(*SLOW, 5.0, True) is False
    _measure(SLOW, SLOW_MS[: A._QF_EXT_MIN_SAMPLES - 1])
    assert A._qf_cut_too_short(*SLOW, 5.0, True) is False


def test_an_unknown_wait_is_judged_as_before():
    _measure(SLOW, SLOW_MS)
    assert A._qf_cut_too_short(*SLOW, None, True) is False


def test_a_tool_free_turn_reads_the_streamed_samples():
    _measure(SLOW, SLOW_MS, tool=False)
    assert A._qf_cut_too_short(*SLOW, 25.0, False) is True
    assert A._qf_cut_too_short(*SLOW, 25.0, True) is True      # falls back to them


def test_flag_off_is_the_previous_behaviour(monkeypatch):
    _measure(SLOW, SLOW_MS)
    real = A.config.get_flag
    monkeypatch.setattr(A.config, "get_flag",
                        lambda n, d=None: False if n == "cut_not_a_streak" else real(n, d))
    assert A._qf_cut_too_short(*SLOW, 25.0, True) is False


def test_a_flag_read_that_raises_counts_as_off(monkeypatch):
    _measure(SLOW, SLOW_MS)

    def boom(n, d=None):
        raise RuntimeError("unreadable")
    monkeypatch.setattr(A.config, "get_flag", boom)
    assert A._qf_cut_too_short(*SLOW, 25.0, True) is False


def test_the_streak_is_skipped_but_the_ordering_mark_stays(monkeypatch):
    calls = []
    monkeypatch.setattr(A, "_note_pair_failure", lambda p, m, c: calls.append((p, m, c)))
    monkeypatch.setattr(A, "_save_learned_state", lambda *a, **k: None)
    with A._recent_fail_lock:
        A._recent_hop_fail.pop(("nv", "a"), None)
        A._recent_hop_fail.pop(("nv", "b"), None)
    A._note_recent_hop_failure("nv", "a", "deadline", streak=False)
    assert calls == []
    with A._recent_fail_lock:                    # the short ordering mark stays
        assert A._recent_hop_fail[("nv", "a")][1] == "deadline"
    A._note_recent_hop_failure("nv", "b", "deadline")
    assert calls == [("nv", "b", "deadline")]
    # a 429 never fed the streak either way
    A._note_recent_hop_failure("nv", "c", "429")
    assert len(calls) == 1


def test_a_non_streamed_cut_below_the_median_does_not_feed_the_streak(monkeypatch):
    _measure(SLOW, SLOW_MS)
    seen = []
    monkeypatch.setattr(A, "_note_recent_hop_failure",
                        lambda p, m, k, streak=True: seen.append((p, m, k, streak)))

    def cut(*a, **k):
        raise A._HopBudgetExceeded("no answer within 25s")
    monkeypatch.setattr(A, "_call_with_wall_clock", cut)
    with A.app.test_request_context("/v1/chat/completions"):
        clock = A._ChainClock(est=BIG, tools=True)
        clock._hop_budget = 25.0
        clock._hop_started = time.monotonic() - 25.0
        with pytest.raises(A._HopBudgetExceeded):
            clock._plain(*SLOW, {"model": SLOW[1]}, False)
    assert seen == [("nv", "glm-slow", "deadline", False)]


def test_a_non_streamed_cut_of_an_unmeasured_pair_feeds_the_streak_as_before(monkeypatch):
    seen = []
    monkeypatch.setattr(A, "_note_recent_hop_failure",
                        lambda p, m, k, streak=True: seen.append((p, m, k, streak)))

    def cut(*a, **k):
        raise A._HopBudgetExceeded("no answer within 25s")
    monkeypatch.setattr(A, "_call_with_wall_clock", cut)
    with A.app.test_request_context("/v1/chat/completions"):
        clock = A._ChainClock(est=BIG, tools=True)
        clock._hop_budget = 25.0
        clock._hop_started = time.monotonic() - 25.0
        with pytest.raises(A._HopBudgetExceeded):
            clock._plain("nv", "never-measured", {"model": "never-measured"}, False)
    assert seen == [("nv", "never-measured", "deadline", True)]


def test_a_silent_stream_cut_below_the_median_does_not_feed_the_streak(monkeypatch):
    _measure(SLOW, SLOW_MS)
    seen = []
    monkeypatch.setattr(A, "_note_recent_hop_failure",
                        lambda p, m, k, streak=True: seen.append((p, m, k, streak)))
    with A.app.test_request_context("/v1/chat/completions"):
        clock = A._ChainClock(est=BIG, tools=True)
        clock._hop_budget = 25.0
        clock._last_peek = 25.0
        clock._hop_started = time.monotonic() - 25.0
        clock.note_peek(*SLOW, "timeout")
    assert seen == [("nv", "glm-slow", "deadline", False)]


# --- 2. a pair with a record may finish -------------------------------------

def test_a_proven_slow_pair_may_run_past_the_mark():
    _measure(SLOW, SLOW_MS)
    with A.app.test_request_context("/v1/chat/completions"):
        clock = _clock()
        room = _room(clock)
        got = clock._budget_for(*SLOW)
        assert got > room + 30                   # well past the plain room
        p90 = A._percentile(SLOW_MS, 90) / 1000.0
        assert got == pytest.approx(min(A._QF_EXT_MULT * p90,
                                        room + A._QF_EXT_CAP_SHARE * clock.limit),
                                    abs=2.0)


def test_the_extension_is_bounded_so_the_fallback_keeps_time_to_serve():
    _measure(SLOW, [400000] * 8)                 # absurd: 400 s
    with A.app.test_request_context("/v1/chat/completions"):
        clock = _clock()
        cap = _room(clock) + A._QF_EXT_CAP_SHARE * clock.limit
        got = clock._budget_for(*SLOW)
        assert cap - 2.0 <= got <= cap + 1.0     # it WAS extended, and only to the cap
        assert got < clock.limit * 0.75


def test_only_one_pair_per_walk_gets_the_extension():
    _measure(SLOW, SLOW_MS)
    _measure(OTHER, SLOW_MS)
    with A.app.test_request_context("/v1/chat/completions"):
        clock = _clock()
        first = clock._budget_for(*SLOW)
        second = clock._budget_for(*OTHER)
        assert first > second + 30
        assert second <= _room(clock) + 1.0
        # the same pair asking again keeps its extension
        assert clock._budget_for(*SLOW) > second


def test_a_pair_that_failed_recently_gets_no_extension(monkeypatch):
    _measure(SLOW, SLOW_MS)
    monkeypatch.setattr(A, "_recent_hop_failure",
                        lambda p, m: "deadline" if (p, m) == SLOW else None)
    with A.app.test_request_context("/v1/chat/completions"):
        clock = _clock()
        assert clock._budget_for(*SLOW) <= _room(clock) + 1.0


def test_a_pair_measured_to_fail_gets_no_extension(monkeypatch):
    _measure(SLOW, SLOW_MS)
    monkeypatch.setattr(A, "_chain_reliability_band", lambda p, m: 2)
    with A.app.test_request_context("/v1/chat/completions"):
        clock = _clock()
        assert clock._budget_for(*SLOW) <= _room(clock) + 1.0


def test_an_unmeasured_pair_gets_no_extension():
    with A.app.test_request_context("/v1/chat/completions"):
        clock = _clock()
        assert clock._budget_for(*SLOW) <= _room(clock) + 1.0


def test_a_pair_that_answers_before_the_mark_is_unchanged():
    _measure(SLOW, [20000] * 8)
    with A.app.test_request_context("/v1/chat/completions"):
        clock = _clock()
        room = _room(clock)
        assert room < 41 and clock._budget_for(*SLOW) == pytest.approx(room, abs=1.0)


def test_a_trivial_ask_never_gets_an_extension(monkeypatch):
    _measure(SLOW, SLOW_MS)
    with A.app.test_request_context("/v1/chat/completions"):
        clock = A._ChainClock(est=2000, tools=True)
        monkeypatch.setattr(A, "_is_trivial_ask", lambda *a, **k: True)
        clock.plan_quality_fallback("max->auto", ASK)
        clock._rest = [QUICK, OTHER]
        clock._qf["at"] = time.monotonic() + LATE_ROOM
        assert clock._qf["trivial"] is True
        assert clock._budget_for(*SLOW) <= _room(clock) + 1.0


def test_flag_off_is_the_plain_cut(monkeypatch):
    _measure(SLOW, SLOW_MS)
    real = A.config.get_flag
    monkeypatch.setattr(A.config, "get_flag",
                        lambda n, d=None: False if n == "qf_let_proven_finish" else real(n, d))
    with A.app.test_request_context("/v1/chat/completions"):
        clock = _clock()
        assert clock._budget_for(*SLOW) <= _room(clock) + 1.0


def test_nothing_to_fall_back_to_means_no_cut_at_all():
    _measure(SLOW, SLOW_MS)
    with A.app.test_request_context("/v1/chat/completions"):
        clock = _clock()
        clock._rest = []
        got = clock._budget_for(*SLOW)
        assert got > LATE_ROOM + 60              # the old behaviour: no cut at all


def test_the_room_never_raises(monkeypatch):
    monkeypatch.setattr(A, "_qf_first_content_ms",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    with A.app.test_request_context("/v1/chat/completions"):
        clock = _clock()
        assert clock._budget_for(*SLOW) is not None
