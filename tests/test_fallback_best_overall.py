"""The way out of a failing turn is ordered by quality first, not by "who has not failed".

MEASURED 2026-11-09 on the owner's hub: every `coding-max` tool turn ended on the best/max
fallback and was served by glm/glm-4.5-flash (hub score 10) or inception/mercury-3, after
~110-150 s of strong hops that failed (dahl 429, nvidia stalls). A check of every tool-capable
model scoring 45+ against the live state found exactly ONE at or above the tool bar that could
serve (nvidia/minimax-m3.1-flash, 99.1): the rest were resting, recently failed, on the owner's
blocklist, too small for the request, or on an exhausted provider. GLM 4.x are NOT in the
`coding` category (the first reproduction wrongly forced them in). What kept the 99.1 model
behind score-10 / 30 ones was the ordering: both `_quality_fallback_pick` and `_mode_fallback_tail`
sorted "recent failure / sick" BEFORE strength, so a healthy weak model outranked a strong one
that had stalled ten minutes ago (or whose provider's other models had).

Rule (flag `fallback_best_overall`, tool turns only, reorder only): a candidate at or above
_TOOLS_MIN_SCORE whose OWN record is clean leads; a resting / never-chaining / band-2 / benched
strong pair keeps its old place behind healthy ones. Fakes only, no network.
"""
import pytest

import app as A

IN_MODE = {"strong-a", "strong-b", "strong-rest", "strong-bad"}      # the coding category
SC = {
    "strong-a": 134.0, "strong-b": 133.6,           # in category, will be walked / failing
    "strong-rest": 134.1,                           # strong, resting after repeated failures
    "strong-bad": 133.0,                            # strong, measured to fail (band 2)
    "mid-recent": 99.1,                             # out of category, recently failed only
    "glm-4.5-flash": 10.0, "glm-4.7-flash": 30.0,   # healthy but weak, fast
    "crouton": 10.0,
}
REST = [("p1", "strong-rest"), ("p2", "strong-bad"), ("nv", "mid-recent"),
        ("glm", "glm-4.5-flash"), ("glm", "glm-4.7-flash"), ("or", "crouton")]
RECENT = {("nv", "mid-recent")}


@pytest.fixture(autouse=True)
def world(monkeypatch):
    monkeypatch.setattr(A, "_benchmark_score", lambda p, m: SC.get(m, 10.0))
    monkeypatch.setattr(A, "_agentic_score", lambda e, s=None: e[0])
    monkeypatch.setattr(A, "_model_identity_min_penalty", lambda pool: None)
    monkeypatch.setattr(A, "_active_mode", lambda: "coding")
    monkeypatch.setattr(A.model_categories, "matches", lambda key, p, m, i=None: m in IN_MODE)
    monkeypatch.setattr(A, "_window_fits", lambda p, m, e: True)
    monkeypatch.setattr(A, "_is_fast", lambda p, m: m.startswith("glm") or m == "crouton")
    monkeypatch.setattr(A, "_supports_tools", lambda p, m: True)
    monkeypatch.setattr(A, "_rescue_pids", lambda: set())
    monkeypatch.setattr(A, "_is_low_quality", lambda m: False)
    monkeypatch.setattr(A, "_is_vision_specialised", lambda m: False)
    monkeypatch.setattr(A, "_relay_tool_sick", lambda p, m: False)
    monkeypatch.setattr(A, "_is_pair_benched", lambda p, m: False)
    monkeypatch.setattr(A, "_simple_turn", lambda: False)
    monkeypatch.setattr(A, "_recent_hop_failure", lambda p, m: "timeout" if (p, m) in RECENT else None)
    monkeypatch.setattr(A, "_recent_hop_stall", lambda p, m: (p, m) in RECENT)
    monkeypatch.setattr(A, "_pair_resting", lambda p, m: m == "strong-rest")
    monkeypatch.setattr(A, "_never_chains", lambda p, m: False)
    monkeypatch.setattr(A, "_quiet_in_session", lambda p, m: False)
    monkeypatch.setattr(A, "_tool_turn_sick",
                        lambda p, m: m in ("strong-rest", "mid-recent", "strong-bad"))
    monkeypatch.setattr(A, "_chain_reliability_band", lambda p, m: 2 if m == "strong-bad" else 0)


def _flag(monkeypatch, on):
    real = A.config.get_flag
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=None: on if k == "fallback_best_overall" else real(k, d))


def _entries():
    return [(SC.get(m, 10.0), p, m) for p, m in REST]


# --- the premise: the old GLMs are not in the category, as measured -----------

def test_the_old_glms_are_not_in_the_coding_category():
    assert A._mode_allows("coding", "glm", "glm-4.5-flash") is False
    assert A._mode_allows("coding", "p1", "strong-a") is True


# --- _quality_fallback_pick ---------------------------------------------------

def test_the_fallback_prefers_the_strong_model_over_healthy_weak_ones(monkeypatch):
    _flag(monkeypatch, True)
    picks = A._quality_fallback_pick(REST, tools=True, stalled=(), est=72000)
    assert picks[0] == ("nv", "mid-recent")


def test_flag_off_is_the_previous_order(monkeypatch):
    _flag(monkeypatch, False)
    picks = A._quality_fallback_pick(REST, tools=True, stalled=(), est=72000)
    # the old behaviour: the category's own (resting / failing) strong pairs are kept
    # and the strong model outside it is not even considered
    assert picks[0] != ("nv", "mid-recent")
    assert ("nv", "mid-recent") not in picks


def test_a_tool_free_turn_is_untouched(monkeypatch):
    _flag(monkeypatch, True)
    a = A._quality_fallback_pick(REST, tools=False, stalled=(), est=72000)
    _flag(monkeypatch, False)
    b = A._quality_fallback_pick(REST, tools=False, stalled=(), est=72000)
    assert a == b


def test_a_resting_or_measured_to_fail_strong_pair_is_not_promoted(monkeypatch):
    """With no strong model that has a clean record anywhere, the pick is exactly the old
    one: the rule never promotes a pair that is resting or measured to fail."""
    rest = [("p1", "strong-rest"), ("p2", "strong-bad"), ("glm", "glm-4.7-flash")]
    _flag(monkeypatch, True)
    on = A._quality_fallback_pick(rest, tools=True, stalled=(), est=72000)
    _flag(monkeypatch, False)
    assert on == A._quality_fallback_pick(rest, tools=True, stalled=(), est=72000)


def test_nothing_clears_the_bar_means_the_old_pick(monkeypatch):
    _flag(monkeypatch, True)
    rest = [("glm", "glm-4.5-flash"), ("glm", "glm-4.7-flash"), ("or", "crouton")]
    a = A._quality_fallback_pick(rest, tools=True, stalled=(), est=72000)
    _flag(monkeypatch, False)
    assert a == A._quality_fallback_pick(rest, tools=True, stalled=(), est=72000)


def test_a_window_that_cannot_hold_the_request_still_drops_a_strong_model(monkeypatch):
    _flag(monkeypatch, True)
    monkeypatch.setattr(A, "_window_fits", lambda p, m, e: m != "mid-recent")
    picks = A._quality_fallback_pick(REST, tools=True, stalled=(), est=72000)
    assert ("nv", "mid-recent") not in picks


def test_every_pick_is_still_a_chain_entry_and_nothing_is_added(monkeypatch):
    _flag(monkeypatch, True)
    picks = A._quality_fallback_pick(REST, tools=True, stalled=(), est=72000)
    assert set(picks) <= set(REST) and len(picks) <= A._QUALITY_FALLBACK_PAIRS


# --- _mode_fallback_tail (the chain's way out of a category) ------------------

def test_the_tail_leads_with_the_strong_model(monkeypatch):
    _flag(monkeypatch, True)
    tail = A._mode_fallback_tail(_entries(), seen=set(), require_tools=True)
    assert tail and tail[0] == ("nv", "mid-recent")


def test_the_tail_flag_off_keeps_the_healthy_weak_first(monkeypatch):
    _flag(monkeypatch, False)
    tail = A._mode_fallback_tail(_entries(), seen=set(), require_tools=True)
    assert tail[0] != ("nv", "mid-recent")


def test_the_tail_never_promotes_a_resting_strong_pair(monkeypatch):
    _flag(monkeypatch, True)
    only = [(134.1, "p1", "strong-rest"), (30.0, "glm", "glm-4.7-flash")]
    tail = A._mode_fallback_tail(only, seen=set(), require_tools=True)
    assert tail[0] == ("glm", "glm-4.7-flash")


def test_a_tool_free_tail_is_untouched(monkeypatch):
    _flag(monkeypatch, True)
    a = A._mode_fallback_tail(_entries(), seen=set(), require_tools=False)
    _flag(monkeypatch, False)
    assert a == A._mode_fallback_tail(_entries(), seen=set(), require_tools=False)


# --- the helper ----------------------------------------------------------------

def test_the_strong_ok_helper():
    assert A._fb_strong_ok(99.1, "nv", "mid-recent") is True         # recent failure is fine
    assert A._fb_strong_ok(134.1, "p1", "strong-rest") is False      # resting
    assert A._fb_strong_ok(133.0, "p2", "strong-bad") is False       # band 2
    assert A._fb_strong_ok(89.9, "x", "y") is False                  # under the bar
    assert A._fb_strong_ok(None, "x", "y") is False


def test_the_helper_and_the_flag_reader_never_raise(monkeypatch):
    monkeypatch.setattr(A, "_pair_resting", lambda p, m: (_ for _ in ()).throw(RuntimeError("x")))
    assert A._fb_strong_ok(120.0, "x", "y") is False

    def boom(k, d=None):
        raise RuntimeError("unreadable")
    monkeypatch.setattr(A.config, "get_flag", boom)
    assert A._fb_floor_on() is False


def test_a_strong_model_outside_the_category_leads_when_the_category_only_has_failing_ones(monkeypatch):
    """The live shape: the category's survivors are resting strong pairs; the one strong
    model with a clean record sits outside the category."""
    _flag(monkeypatch, True)
    rest = [("p1", "strong-rest"), ("nv", "mid-recent"), ("glm", "glm-4.7-flash")]
    assert A._quality_fallback_pick(rest, tools=True, stalled=(), est=72000)[0] == ("nv", "mid-recent")


def test_a_strong_in_category_model_with_a_clean_record_still_leads_its_category(monkeypatch):
    _flag(monkeypatch, True)
    monkeypatch.setattr(A, "_pair_resting", lambda p, m: False)
    monkeypatch.setattr(A, "_chain_reliability_band", lambda p, m: 0)
    rest = [("p1", "strong-rest"), ("nv", "mid-recent"), ("glm", "glm-4.7-flash")]
    assert A._quality_fallback_pick(rest, tools=True, stalled=(), est=72000)[0] == ("p1", "strong-rest")
