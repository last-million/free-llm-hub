"""A resting model does not open a turn while a working one exists.

MEASURED live on 36f8a07 (2026-11-09 22:46:54 UTC, OpenCode `coding-max`): the router opened a
turn on nvidia/z-ai/glm-5.3-flash ("[spread] ... (pool 1 ...)") while learned-state.json had that
pair RESTING for five more hours (level 7, class "deadline"). The turn waited 174 s on it and
Mercury served. Twenty-four seconds earlier glm/glm-4.5-flash had answered the same session in
22 s.

WHY: the router drops tool-sick pairs with `[...] or agentic`, and `agentic` holds only the
candidates at or above the tool bar that the category mode kept. With every one of those sick, the
filter failed open onto a sick one; the working models under the bar, or outside the category, were
never looked at. The chain already put sick pairs last (`_fit + _sick`); the opening pick did not.

RULE: when every candidate left for the opening pick is tool-sick, the pick is made among the
working tool-capable candidates from before the mode filter: those at or above the tool bar if
any, else the rest. In the chain, in-mode sick hops go behind the working hops of the mode's
way-out tail. Reorder only.

Hermetic: fake providers, scores, category and sickness.
"""
import pytest

import app as A

SCORES = {
    "strong-a": 134.0,     # in the category, strong
    "strong-b": 133.0,     # in the category, strong
    "weak-out": 30.0,      # outside the category, under the tool bar (glm-4.7)
    "weak2-out": 10.0,     # outside the category, under the tool bar (glm-4.5)
    "strong-out": 120.0,   # outside the category, strong (not in every fleet below)
}
WORLD = {"nv": ["strong-a", "strong-b"], "zl": ["weak-out", "weak2-out"]}
MSGS = [{"role": "user", "content": "fix the failing test in the scheduler and add a regression test"}]


SICK = set()          # models the hub has measured to keep failing (resting ...), per test


def _in_mode(m):
    return m in ("strong-a", "strong-b")


@pytest.fixture
def fleet(monkeypatch):
    world = {p: list(ms) for p, ms in WORLD.items()}
    sick = SICK
    monkeypatch.setattr(A, "_available_providers", lambda *a, **k: list(world))
    monkeypatch.setattr(A, "_prefetch_auto_models", lambda pids: dict(world))
    monkeypatch.setattr(A, "_auto_models", lambda pid: list(world[pid]))
    monkeypatch.setattr(A, "_provider_capable", lambda pid, est: True)
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: SCORES.get(m, 10.0))
    monkeypatch.setattr(A, "_supports_tools", lambda pid, m: True)
    monkeypatch.setattr(A, "_session_pin_get", lambda key: None)
    monkeypatch.setattr(A, "_session_pin_set", lambda *a, **k: None)
    monkeypatch.setattr(A, "_weighted_pick", lambda pool, *a, **k: max(pool))
    monkeypatch.setattr(A, "_is_relay_pid", lambda pid: False)
    monkeypatch.setattr(A, "_rescue_pids", lambda: frozenset())
    monkeypatch.setattr(A.model_categories, "matches", lambda key, p, m, i=None: _in_mode(m))
    monkeypatch.setattr(A, "_active_mode", lambda: "coding")
    monkeypatch.setattr(A, "_simple_turn", lambda: False)
    monkeypatch.setattr(A, "_tool_turn_sick", lambda pid, m: m in sick)
    monkeypatch.setattr(A, "_pair_resting", lambda pid, m: m in sick)
    SICK.clear()
    yield world
    SICK.clear()


def _flag(monkeypatch, on):
    real = A.config.get_flag
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=None: on if k == "sick_yields_to_working" else real(k, d))


def _route(tools=True):
    return A._route_by_difficulty(MSGS, None, 500, require_tools=tools)[:2]


def _chain(primary, tools=True):
    return A._build_chain(primary[0], primary[1], 500, require_tools=tools, messages=MSGS)


def _names(chain):
    return [m for _p, m in chain]


# --------------------------------------------------------------------------- #
# The opening pick
# --------------------------------------------------------------------------- #

def test_the_live_case_every_strong_model_is_resting(fleet):
    """The 22:46:54 turn: it opened on a resting pair while a working model existed."""
    SICK.update({"strong-a", "strong-b"})
    assert _route() == ("zl", "weak-out")


def test_one_working_strong_model_still_opens(fleet):
    """Unchanged: the rule only speaks when EVERY candidate for the pick is sick."""
    SICK.add("strong-a")
    assert _route() == ("nv", "strong-b")


def test_a_healthy_fleet_is_unchanged(fleet):
    assert _route() == ("nv", "strong-a")


def test_a_working_strong_model_outside_the_category_goes_before_a_weak_one(fleet):
    fleet["zl"].append("strong-out")
    SICK.update({"strong-a", "strong-b"})
    assert _route() == ("zl", "strong-out")


def test_when_everything_is_sick_the_old_pick_stands(fleet):
    SICK.update({"strong-a", "strong-b", "weak-out", "weak2-out"})
    assert _route() == ("nv", "strong-a")


def test_a_model_that_cannot_use_tools_is_not_an_alternative(fleet, monkeypatch):
    SICK.update({"strong-a", "strong-b"})
    monkeypatch.setattr(A, "_supports_tools", lambda pid, m: m not in ("weak-out", "weak2-out"))
    assert _route()[1] in ("strong-a", "strong-b")


def test_a_vision_specialised_model_is_not_an_alternative(fleet, monkeypatch):
    SICK.update({"strong-a", "strong-b"})
    monkeypatch.setattr(A, "_is_vision_specialised", lambda m: m == "weak-out")
    assert _route() == ("zl", "weak2-out")


def test_the_flag_off_is_the_old_behaviour(fleet, monkeypatch):
    """Documents the bug: a resting strong model opens although a working one exists."""
    _flag(monkeypatch, False)
    SICK.update({"strong-a", "strong-b"})
    assert _route()[1] in ("strong-a", "strong-b")


def test_a_tool_free_turn_is_untouched(fleet):
    SICK.update({"strong-a", "strong-b"})
    assert _route(tools=False)[1] in ("strong-a", "strong-b")


def test_without_a_category_mode_the_rule_is_the_same(fleet, monkeypatch):
    monkeypatch.setattr(A, "_active_mode", lambda: A.MODE_ALL)
    SICK.update({"strong-a", "strong-b"})
    assert _route() == ("zl", "weak-out")


def test_the_pin_lets_go_of_a_resting_model(fleet, monkeypatch):
    """A session pinned to a pair that is now resting re-picks: the pin is checked against the
    list the rule produced."""
    SICK.update({"strong-a", "strong-b"})
    monkeypatch.setattr(A, "_session_pin_get", lambda key: ("nv", "strong-a"))
    monkeypatch.setattr(A, "_agent_pin_key", lambda messages: "sess-1")
    assert _route() == ("zl", "weak-out")


def test_the_pin_returns_to_a_strong_model_once_one_works(fleet, monkeypatch):
    """...and a session that was pinned to the weak model while everything rested goes back
    to a strong one as soon as one works (the weak model is no longer in the pick list)."""
    monkeypatch.setattr(A, "_session_pin_get", lambda key: ("zl", "weak-out"))
    monkeypatch.setattr(A, "_agent_pin_key", lambda messages: "sess-1")
    assert _route() == ("nv", "strong-a")


# --------------------------------------------------------------------------- #
# The helper
# --------------------------------------------------------------------------- #

def _c(m, pid="nv"):
    return (SCORES[m], pid, m)


def test_helper_passes_through_when_any_member_works(fleet):
    SICK.add("strong-a")
    ag = [_c("strong-a"), _c("strong-b")]
    assert A._sick_yields_to_working(ag, ag + [_c("weak-out", "zl")]) == ag


def test_helper_prefers_strong_working_alternatives(fleet):
    SICK.update({"strong-a", "strong-b"})
    ag = [_c("strong-a"), _c("strong-b")]
    before = ag + [_c("weak-out", "zl"), _c("strong-out", "zl")]
    assert A._sick_yields_to_working(ag, before) == [_c("strong-out", "zl")]


def test_helper_empty_and_error_are_fail_open(fleet, monkeypatch):
    assert A._sick_yields_to_working([], [_c("weak-out", "zl")]) == []
    ag = [_c("strong-a")]
    monkeypatch.setattr(A, "_tool_turn_sick", lambda p, m: (_ for _ in ()).throw(RuntimeError("x")))
    assert A._sick_yields_to_working(ag, [_c("weak-out", "zl")]) == ag


# --------------------------------------------------------------------------- #
# The chain behind the pick
# --------------------------------------------------------------------------- #

def test_tail_helper_moves_sick_in_mode_hops_behind_working_tail_hops(fleet):
    SICK.update({"strong-b", "weak2-out"})
    chain = [("zl", "weak-out"), ("nv", "strong-b"), ("nv", "strong-a")]
    tail = [("zl", "strong-out"), ("zl", "weak2-out")]
    got_chain, got_tail = A._sick_mode_hops_behind_tail(chain, tail, True)
    assert got_chain == [("zl", "weak-out"), ("nv", "strong-a")]
    assert got_tail == [("zl", "strong-out"), ("nv", "strong-b"), ("zl", "weak2-out")]
    assert sorted(got_chain + got_tail) == sorted(chain + tail)       # nothing lost, nothing added


def test_tail_helper_never_moves_the_first_hop(fleet):
    SICK.add("strong-a")
    chain = [("nv", "strong-a"), ("nv", "strong-b")]
    tail = [("zl", "weak-out")]
    assert A._sick_mode_hops_behind_tail(chain, tail, True) == (chain, tail)


def test_tail_helper_is_a_no_op_without_a_working_tail_or_tools_or_flag(fleet, monkeypatch):
    SICK.update({"strong-b", "weak-out"})
    chain = [("nv", "strong-a"), ("nv", "strong-b")]
    tail = [("zl", "weak-out")]
    assert A._sick_mode_hops_behind_tail(chain, tail, True) == (chain, tail)      # tail all sick
    SICK.discard("weak-out")
    assert A._sick_mode_hops_behind_tail(chain, tail, False) == (chain, tail)     # not a tool turn
    assert A._sick_mode_hops_behind_tail(chain, [], True) == (chain, [])
    _flag(monkeypatch, False)
    assert A._sick_mode_hops_behind_tail(chain, tail, True) == (chain, tail)


def test_the_whole_chain_asks_working_models_before_resting_ones(fleet):
    """Opening pick + chain together: the working model opens, the other working one comes
    next, and the resting strong pairs are the last resort (never dropped)."""
    SICK.update({"strong-a", "strong-b"})
    primary = _route()
    names = _names(_chain(primary))
    assert names[0] == "weak-out", names
    assert {"strong-a", "strong-b", "weak2-out"} <= set(names), names
    assert names.index("weak2-out") < min(names.index("strong-a"), names.index("strong-b")), names
    assert len(names) == len(set(names))


def test_the_chain_flag_off_keeps_the_resting_pairs_ahead_of_the_way_out(fleet, monkeypatch):
    _flag(monkeypatch, False)
    SICK.update({"strong-a", "strong-b"})
    names = _names(_chain(("zl", "weak-out")))
    assert names.index("weak2-out") > max(names.index("strong-a"), names.index("strong-b")), names


def test_a_healthy_chain_is_unchanged(fleet, monkeypatch):
    on = _names(_chain(("nv", "strong-a")))
    _flag(monkeypatch, False)
    assert _names(_chain(("nv", "strong-a"))) == on
