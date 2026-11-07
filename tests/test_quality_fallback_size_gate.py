"""The best/max fallback's 30 s "trivial" mark is for SMALL requests only.

MEASURED 2026-11-07 (owner's OpenCode `coding-max`, hub.log): a 145,671-token
request ended CHAT-503 with hop budgets of 19 / 45 / 6 / 25 / 25 s -- the
trivial-turn budgets -- under a `[quality-fallback] max->auto: nothing served
after 30s` line. The walk had judged the turn trivial because its LAST message
was a bare "continue" (`_is_trivial_ask` reads the latest instruction alone),
jumped to the fast picks at 30 s and flipped the clock to trivial. Nothing
prefills 145K tokens in 25 s, so every strong hop was cut by the hub, filed as
a deadline failure and rested, and the weak fast models served.

The clock's own `trivial` flag already refuses a big request
(`_is_trivial_turn`: est >= STREAM_BIG_REQUEST_TOKENS); the fallback's did not.
Fakes only, no network, no sleeping.
"""
import time

import pytest

import app as A

ASK = [{"role": "system", "content": "you are a coding agent"},
       {"role": "user", "content": "build the booking page"},
       {"role": "assistant", "content": "done, anything else?"},
       {"role": "user", "content": "continue"}]
STRONG = [("nv", "strong-a"), ("nv", "strong-b")]
QUICK = ("fastp", "quick")


@pytest.fixture(autouse=True)
def _deadline(monkeypatch):
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: 240)
    monkeypatch.setattr(A, "_is_fast", lambda p, m: m == "quick")


def _plan(est, messages=ASK, tools=True):
    clock = A._ChainClock(est=est, tools=tools)
    clock.plan_quality_fallback("max->auto", messages)
    return clock


def _mark(clock):
    return clock._qf["at"] - clock._qf["start"]


# --- the real classifier says "continue" is trivial: that is the trap ---------

def test_the_real_classifier_calls_a_bare_continue_trivial():
    assert A._is_trivial_ask(ASK) is True


# --- the mark -----------------------------------------------------------------

def test_a_small_request_keeps_the_30_second_mark():
    with A.app.test_request_context("/v1/chat/completions"):
        clock = _plan(est=2000)
        assert clock._qf["trivial"] is True
        assert _mark(clock) == A._QUALITY_FALLBACK_TRIVIAL_SECONDS


def test_an_unknown_size_keeps_the_old_behaviour():
    with A.app.test_request_context("/v1/chat/completions"):
        clock = _plan(est=0)
        assert clock._qf["trivial"] is True
        assert _mark(clock) == A._QUALITY_FALLBACK_TRIVIAL_SECONDS


def test_a_big_request_is_not_trivial_whatever_its_last_message_says():
    with A.app.test_request_context("/v1/chat/completions"):
        clock = _plan(est=145671)
        assert clock._qf["trivial"] is False
        assert _mark(clock) >= A._QUALITY_FALLBACK_MIN_SECONDS
        assert _mark(clock) > A._QUALITY_FALLBACK_TRIVIAL_SECONDS
        # the normal mark: a bounded share of the deadline, never past half
        assert _mark(clock) <= 0.5 * clock.limit


def test_the_line_is_the_size_the_clock_already_uses_for_trivial_turns():
    big = A.STREAM_BIG_REQUEST_TOKENS
    with A.app.test_request_context("/v1/chat/completions"):
        assert _plan(est=big - 1)._qf["trivial"] is True
        assert _plan(est=big)._qf["trivial"] is False


@pytest.mark.parametrize("est", [None, 0, "x", -5, 1.5])
def test_the_helper_never_raises(est):
    assert A._qf_big_request(est) in (True, False)


# --- firing -------------------------------------------------------------------

def test_firing_on_a_big_request_does_not_flip_the_clock_to_trivial():
    with A.app.test_request_context("/v1/chat/completions"):
        clock = _plan(est=145671)
        rest = [QUICK] + STRONG
        clock._fire_quality_fallback(rest)
        assert clock.trivial is False


def test_firing_on_a_small_trivial_request_still_flips_it():
    with A.app.test_request_context("/v1/chat/completions"):
        clock = _plan(est=2000)
        rest = [QUICK] + STRONG
        clock._fire_quality_fallback(rest)
        assert clock.trivial is True


def test_a_strong_hop_of_a_big_request_keeps_a_budget_no_trivial_cap_applies():
    """Before the fix the picks of this walk got the 25 s / 45 s trivial
    budget; the strong pool needs a minute or more to prefill 145K tokens."""
    with A.app.test_request_context("/v1/chat/completions"):
        clock = _plan(est=145671)
        clock._fire_quality_fallback([QUICK] + STRONG)
        budget = clock._budget_for("nv", "strong-a")
        assert budget is None or budget > A._TRIVIAL_SLOW_HOP_BUDGET + 30


# --- the switch ---------------------------------------------------------------

def test_flag_off_is_the_previous_behaviour_byte_for_byte(monkeypatch):
    real = A.config.get_flag
    monkeypatch.setattr(
        A.config, "get_flag",
        lambda name, default=None: False if name == "qf_size_gate" else real(name, default))
    with A.app.test_request_context("/v1/chat/completions"):
        clock = _plan(est=145671)
        assert clock._qf["trivial"] is True
        assert _mark(clock) == A._QUALITY_FALLBACK_TRIVIAL_SECONDS
        clock._fire_quality_fallback([QUICK] + STRONG)
        assert clock.trivial is True


def test_a_flag_read_that_raises_counts_as_off(monkeypatch):
    def boom(name, default=None):
        raise RuntimeError("config unreadable")
    monkeypatch.setattr(A.config, "get_flag", boom)
    assert A._qf_big_request(145671) is False


# --- the other readers of the same ask are not affected ------------------------

def test_a_long_message_is_never_trivial_so_the_gate_changes_nothing_there():
    msgs = ASK[:-1] + [{"role": "user", "content": "Refactor the whole billing module "
                        "to use the new ledger, update every caller and add tests."}]
    with A.app.test_request_context("/v1/chat/completions"):
        clock = _plan(est=145671, messages=msgs)
        assert clock._qf["trivial"] is False
        assert time.monotonic() < clock._qf["at"]
