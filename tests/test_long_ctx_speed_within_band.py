"""Speed breaks ties on a big request; it does not buy a lead over better models.

MEASURED on the owner's hub (2026-10-27): a ~71K-token context-max tool request
went to inception/mercury-3 (hub score 26, ~2 s) as its ONLY hop, as did 24 of 29
OpenCode requests, while dahl's GLM-5.3-Flash (133.6, 3-15 s, healthy) and nvidia's
minimax-m3.1-flash (99) sat behind it. `_prefer_fast_long_context` sorted by
"measured fast at long context" BEFORE rank, and only Mercury had such samples:
no other model could ever earn one.

Pinned here:
  * a measured-fast hop jumps ahead only of hops it is as good as
    (within _AUTO_TOP_BAND), on the score the chain was ordered by;
  * measured-SLOW hops still go behind the unmeasured ones (the timeout
    protection the rule exists for), the low-quality tail stays the tail, a
    pinned head never moves;
  * the live fleet's numbers come out strongest-first, Mercury behind them;
  * flag long_ctx_speed_within_band off = the old ordering exactly.
"""
import pytest

import app as A

BIG = 200000

SCORES = {
    "glm-5.3": 134.0,            # nvidia: measured stalled at long context
    "deepseek-v4-flash": 133.9,  # dahl: unmeasured
    "glm-5.3-flash": 133.6,      # dahl: unmeasured
    "minimax-m3.1-flash": 99.1,  # nvidia: unmeasured
    "mercury-3": 26.0,           # inception: measured FAST
    "peer-fast": 133.0,          # as good as the head, measured fast
    "weak-unknown": 60.0,
}


@pytest.fixture(autouse=True)
def _ledger_and_scores(monkeypatch):
    A._long_ctx_speed.clear()
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: SCORES.get(m, 10.0))
    monkeypatch.setattr(A, "_learned_score", lambda c: c[0])
    monkeypatch.setattr(A, "_agentic_score", lambda c: c[0])
    monkeypatch.setattr(A, "_is_pair_benched", lambda pid, m: False)
    monkeypatch.setattr(A, "_is_low_quality", lambda m: False)
    monkeypatch.setattr(A, "_recent_hop_failure", lambda pid, m: None)
    monkeypatch.setattr(A, "_rr_fit", lambda est: est)
    yield
    A._long_ctx_speed.clear()


def _fast(pid, model, n=3):
    for _ in range(n):
        A._record_long_ctx_speed(pid, model, BIG, 3000.0)


def _slow(pid, model):
    A._record_long_ctx_speed(pid, model, BIG, 150000.0)


# The chain in the router's own rank order.
LIVE = [("nvidia", "glm-5.3"), ("dahl", "deepseek-v4-flash"), ("dahl", "glm-5.3-flash"),
        ("nvidia", "minimax-m3.1-flash"), ("inception", "mercury-3")]


def test_the_live_fleet_no_longer_opens_on_mercury():
    _slow("nvidia", "glm-5.3")
    _fast("inception", "mercury-3")
    out = A._prefer_fast_long_context(LIVE, BIG)
    assert out[0] == ("dahl", "deepseek-v4-flash"), out
    assert out.index(("dahl", "glm-5.3-flash")) < out.index(("inception", "mercury-3"))
    assert out.index(("nvidia", "minimax-m3.1-flash")) < out.index(("inception", "mercury-3"))
    assert out[-1] == ("nvidia", "glm-5.3"), "measured slow still goes last"
    assert sorted(out) == sorted(LIVE), "reordered, never dropped or added"


def test_with_the_flag_off_mercury_leads_as_before(monkeypatch):
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=None: False if k == "long_ctx_speed_within_band" else d)
    _slow("nvidia", "glm-5.3")
    _fast("inception", "mercury-3")
    out = A._prefer_fast_long_context(LIVE, BIG)
    assert out[0] == ("inception", "mercury-3")
    assert out[-1] == ("nvidia", "glm-5.3")


def test_an_equally_good_measured_fast_hop_still_goes_first():
    chain = [("a", "deepseek-v4-flash"), ("b", "peer-fast")]
    _fast("b", "peer-fast")
    assert A._prefer_fast_long_context(chain, BIG) == [("b", "peer-fast"), ("a", "deepseek-v4-flash")]


def test_a_fast_hop_two_points_below_is_not_equal_but_inside_the_band_is():
    SCORES["edge-in"], SCORES["edge-out"] = 131.95, 131.85
    try:
        for name, expect_first in (("edge-in", "edge-in"), ("edge-out", "deepseek-v4-flash")):
            A._long_ctx_speed.clear()
            chain = [("a", "deepseek-v4-flash"), ("b", name)]   # head 133.9, band 2.0
            _fast("b", name)
            assert A._prefer_fast_long_context(chain, BIG)[0][1] == expect_first, name
    finally:
        SCORES.pop("edge-in", None)
        SCORES.pop("edge-out", None)


def test_measured_slow_still_goes_behind_the_unmeasured_even_when_stronger():
    chain = [("a", "glm-5.3"), ("b", "weak-unknown")]
    _slow("a", "glm-5.3")
    assert A._prefer_fast_long_context(chain, BIG) == [("b", "weak-unknown"), ("a", "glm-5.3")]


def test_a_pinned_head_never_moves():
    _fast("inception", "mercury-3")
    out = A._prefer_fast_long_context(LIVE, BIG, keep_head=1)
    assert out[0] == ("nvidia", "glm-5.3")


def test_a_tool_turn_orders_by_the_agentic_score(monkeypatch):
    """The score the chain was ordered by: _agentic_score on a tool turn."""
    monkeypatch.setattr(A, "_agentic_score", lambda c: 200.0 if c[2] == "mercury-3" else c[0])
    _fast("inception", "mercury-3")
    chain = [("dahl", "deepseek-v4-flash"), ("inception", "mercury-3")]
    assert A._prefer_fast_long_context(chain, BIG, tools=True)[0] == ("inception", "mercury-3")
    assert A._prefer_fast_long_context(chain, BIG, tools=False)[0] == ("dahl", "deepseek-v4-flash")


def test_a_score_that_cannot_be_read_never_promotes(monkeypatch):
    def boom(c):
        raise RuntimeError("no score")
    monkeypatch.setattr(A, "_learned_score", boom)
    _fast("inception", "mercury-3")
    chain = [("dahl", "deepseek-v4-flash"), ("inception", "mercury-3")]
    assert A._prefer_fast_long_context(chain, BIG) == chain


def test_below_the_threshold_and_with_no_measurements_nothing_moves():
    _fast("inception", "mercury-3")
    assert A._prefer_fast_long_context(LIVE, 20000) == LIVE
    A._long_ctx_speed.clear()
    assert A._prefer_fast_long_context(LIVE, BIG) is LIVE


def test_the_low_quality_tail_stays_the_tail(monkeypatch):
    monkeypatch.setattr(A, "_is_low_quality", lambda m: m == "weak-unknown")
    _fast("x", "weak-unknown")
    chain = [("a", "deepseek-v4-flash"), ("x", "weak-unknown"), ("b", "glm-5.3-flash")]
    out = A._prefer_fast_long_context(chain, BIG)
    assert out[-1] == ("x", "weak-unknown")


def test_the_build_chain_passes_the_tool_flag():
    import inspect
    src = inspect.getsource(A._build_chain)
    assert "tools=bool(require_tools)" in src
