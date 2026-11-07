"""The rescue provider steps in sooner once the strong models have failed in THIS walk.

MEASURED 2026-11-07 in a real OpenCode run: two steps were rescued by inception/mercury-3
at ~200 s each, because the strong hops ran the clock down to the 45 s reserve. Rule now:
after a hop of the walk has failed, the rescue goes next when (a) a provider stalled and
half the clock is gone, or (b) nothing healthy is left ahead of it. Never on a first hop,
never in the first half while a healthy strong hop waits. Flag `rescue_early_jump`.
"""
import time

import pytest

import app as A

_ORIG_ON = A._rescue_early_on
RESCUE = ("inception", "mercury-3")
CHAIN = [("nvidia", "strong-a"), ("dahl", "strong-c"), ("other", "mid"), RESCUE]


@pytest.fixture(autouse=True)
def world(monkeypatch):
    monkeypatch.setattr(A, "_rescue_pids", lambda: frozenset(("inception",)))
    monkeypatch.setattr(A, "_rescue_early_on", lambda: True)
    sick = set()
    monkeypatch.setattr(A, "_recent_hop_failure",
                        lambda pid, m: "timeout" if (pid, m) in sick else None)
    monkeypatch.setattr(A, "_is_pair_benched", lambda pid, m: False)
    monkeypatch.setattr(A, "_tool_turn_sick", lambda pid, m: False)
    monkeypatch.setattr(A, "_local_net_since", lambda *a, **k: None)
    return sick


def _walk(tools=True, left=200.0):
    c = A._ChainClock(tools=tools, est=500)
    c.deadline_at = time.monotonic() + left
    c.limit = 240
    return c, c.walk(list(CHAIN))


def _fail(c, hop, gone=None, stall=False, left=None):
    """The hop just walked failed (the walk resumes); optionally a stall, and the clock
    moved so that `gone` seconds passed and `left` remain."""
    c._cur = (hop[0], hop[1], time.monotonic())
    if stall:
        c._note_stall(hop[0])
    now = time.monotonic()
    if gone is not None:
        c._walk_t0 = now - gone
    if left is not None:
        c.deadline_at = now + left


def test_the_first_hop_is_never_the_rescue_even_on_a_sick_fleet(world):
    world.update({("nvidia", "strong-a"), ("dahl", "strong-c"), ("other", "mid")})
    with A.app.test_request_context("/v1/chat/completions"):
        c, it = _walk()
        assert next(it) == CHAIN[0]


def test_after_one_fast_failure_a_healthy_strong_hop_still_goes_next():
    with A.app.test_request_context("/v1/chat/completions"):
        c, it = _walk()
        hop = next(it)
        _fail(c, hop, gone=5, left=230)
        assert next(it) == ("dahl", "strong-c")


def test_a_stall_in_the_first_half_still_tries_the_next_strong_hop():
    with A.app.test_request_context("/v1/chat/completions"):
        c, it = _walk()
        hop = next(it)
        _fail(c, hop, gone=60, left=170, stall=True)
        assert next(it) == ("dahl", "strong-c")


def test_a_stall_once_half_the_clock_is_gone_hands_it_to_the_rescue():
    with A.app.test_request_context("/v1/chat/completions"):
        c, it = _walk()
        hop = next(it)
        _fail(c, hop, gone=130, left=100, stall=True)
        assert next(it) == RESCUE


def test_half_the_clock_gone_without_any_stall_is_not_enough():
    with A.app.test_request_context("/v1/chat/completions"):
        c, it = _walk()
        hop = next(it)
        _fail(c, hop, gone=130, left=100, stall=False)
        assert next(it) == ("dahl", "strong-c")


def test_nothing_healthy_left_after_a_failure_hands_it_to_the_rescue(world):
    world.update({("dahl", "strong-c"), ("other", "mid")})
    with A.app.test_request_context("/v1/chat/completions"):
        c, it = _walk()
        hop = next(it)
        _fail(c, hop, gone=10, left=220)
        assert next(it) == RESCUE


def test_one_healthy_hop_left_keeps_the_rescue_waiting(world):
    # The walk keeps the chain's own order (the chain builder already put fresh
    # failures behind); while one healthy hop is left, the rescue does not jump.
    world.update({("dahl", "strong-c")})
    with A.app.test_request_context("/v1/chat/completions"):
        c, it = _walk()
        hop = next(it)
        _fail(c, hop, gone=10, left=220)
        assert next(it) == ("dahl", "strong-c")
        _fail(c, ("dahl", "strong-c"), gone=20, left=210)
        assert next(it) == ("other", "mid")


def test_a_stalled_provider_counts_as_not_healthy(world):
    world.update({("other", "mid")})
    chain = [("nvidia", "a"), ("nvidia", "b"), ("other", "mid"), RESCUE]
    with A.app.test_request_context("/v1/chat/completions"):
        c = A._ChainClock(tools=True, est=500)
        c.deadline_at = time.monotonic() + 200
        it = c.walk(list(chain))
        hop = next(it)
        _fail(c, hop, gone=10, left=220, stall=True)
        assert next(it) == RESCUE


def test_tool_sickness_counts_on_a_tool_turn_only(monkeypatch, world):
    world.update({("other", "mid")})
    monkeypatch.setattr(A, "_tool_turn_sick", lambda pid, m: pid == "dahl")
    with A.app.test_request_context("/v1/chat/completions"):
        c, it = _walk(tools=True)
        _fail(c, next(it), gone=10, left=220)
        assert next(it) == RESCUE
    with A.app.test_request_context("/v1/chat/completions"):
        c, it = _walk(tools=False)
        _fail(c, next(it), gone=10, left=220)
        assert next(it) == ("dahl", "strong-c")


def test_the_flag_off_keeps_only_the_reserve(monkeypatch, world):
    monkeypatch.setattr(A, "_rescue_early_on", lambda: False)
    world.update({("dahl", "strong-c"), ("other", "mid")})
    with A.app.test_request_context("/v1/chat/completions"):
        c, it = _walk()
        hop = next(it)
        _fail(c, hop, gone=130, left=100, stall=True)
        assert next(it) == ("dahl", "strong-c")


def test_the_reserve_rule_still_holds_with_the_flag_off(monkeypatch):
    monkeypatch.setattr(A, "_rescue_early_on", lambda: False)
    with A.app.test_request_context("/v1/chat/completions"):
        c, it = _walk()
        hop = next(it)
        _fail(c, hop, left=A._RESCUE_RESERVE_S - 1)
        assert next(it) == RESCUE


def test_a_flag_read_that_raises_counts_as_off(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("config")
    monkeypatch.setattr(A.config, "get_flag", boom)
    assert _ORIG_ON() is False


def test_a_health_check_that_raises_never_jumps(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("ledger")
    monkeypatch.setattr(A, "_recent_hop_failure", boom)
    with A.app.test_request_context("/v1/chat/completions"):
        c, it = _walk()
        _fail(c, next(it), gone=10, left=220)
        assert next(it) == ("dahl", "strong-c")


def test_a_local_network_failure_does_not_count(monkeypatch, world):
    world.update({("dahl", "strong-c"), ("other", "mid")})
    monkeypatch.setattr(A, "_local_net_since", lambda *a, **k: "api.example.com")
    with A.app.test_request_context("/v1/chat/completions"):
        c, it = _walk()
        _fail(c, next(it), gone=10, left=220)
        assert next(it) == ("dahl", "strong-c")


def test_with_no_rescue_hop_in_the_chain_nothing_changes(world):
    world.update({("dahl", "strong-c")})
    chain = [("nvidia", "strong-a"), ("dahl", "strong-c")]
    with A.app.test_request_context("/v1/chat/completions"):
        c = A._ChainClock(tools=True, est=500)
        c.deadline_at = time.monotonic() + 200
        it = c.walk(list(chain))
        _fail(c, next(it), gone=150, left=80, stall=True)
        assert next(it) == ("dahl", "strong-c")


def test_it_is_still_only_one_hop_and_nothing_is_dropped(world):
    world.update({("dahl", "strong-c"), ("other", "mid")})
    with A.app.test_request_context("/v1/chat/completions"):
        c, it = _walk()
        _fail(c, next(it), gone=10, left=220)
        assert next(it) == RESCUE
        _fail(c, RESCUE, gone=20, left=210)
        rest = list(it)
    assert rest == [("dahl", "strong-c"), ("other", "mid")]
