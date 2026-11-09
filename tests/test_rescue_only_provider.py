"""A rescue provider catches what would fail, and does nothing else.

Owner, 2026-11-07: "when I enable Mercury it uses it always, maybe because it is the
fastest." MEASURED in hub.log, 10-25 to 10-28: 168 picks "inception/mercury-3 (pool 1,
...)". It never won on merit: its hub score is 26. It came through four doors:

  1. The health filters run first, so one recent failure takes a strong model out of the
     candidates and the clean weak model is what is left.
  2. The stall backup ("glm-5.3 silent 30s -> backup inception/mercury-3"): a fast model
     beats a thinking model in a race, whatever the quality.
  3. The best/max fallback sorts by speed.
  4. The session watch returns a conversation to whoever acted, i.e. to it.

Rule: it never competes. The router behaves as if it were off, so the usual fail-opens
fire, and it gets ONE hop, the LAST, with the end of the clock kept for it, so a turn the
strong models lost is still answered.
"""
import time

import pytest

import app as A

SCORES = {"strong-a": 134.0, "strong-b": 133.0, "strong-c": 130.0, "mid": 100.0,
          "mercury-3": 26.0, "weak": 30.0}
WORLD = {"nvidia": ["strong-a", "strong-b"], "dahl": ["strong-c"], "other": ["mid", "weak"],
         "inception": ["mercury-3"]}
MSGS = [{"role": "user", "content": "fix the failing test in the scheduler and add a regression test"}]
RESCUE = ("inception", "mercury-3")


@pytest.fixture
def fleet(monkeypatch):
    world = {p: list(ms) for p, ms in WORLD.items()}
    monkeypatch.setattr(A, "_available_providers", lambda *a, **k: list(world))
    monkeypatch.setattr(A, "_prefetch_auto_models", lambda pids: {p: world[p] for p in pids})
    monkeypatch.setattr(A, "_auto_models", lambda pid: list(world[pid]))
    monkeypatch.setattr(A, "_provider_capable", lambda pid, est: True)
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: SCORES.get(m, 10.0))
    monkeypatch.setattr(A, "_supports_tools", lambda pid, m: True)
    monkeypatch.setattr(A, "_session_pin_get", lambda key: None)
    monkeypatch.setattr(A, "_session_pin_set", lambda *a, **k: None)
    monkeypatch.setattr(A, "_weighted_pick", lambda pool, *a, **k: max(pool))
    monkeypatch.setattr(A, "_is_relay_pid", lambda pid: False)
    monkeypatch.setattr(A, "_is_fast", lambda pid, m: pid == "inception")
    monkeypatch.setattr(A, "_rescue_pids", lambda: frozenset(("inception",)))
    yield world


def _route(tools=True, msgs=MSGS):
    return A._route_by_difficulty(msgs, None, 500, require_tools=tools)[:2]


def _all_out(monkeypatch, kind="timeout"):
    """Everything but the rescue provider failed in the last minutes."""
    monkeypatch.setattr(A, "_recent_hop_failure",
                        lambda pid, m: None if pid == "inception" else kind)
    monkeypatch.setattr(A, "_recent_hop_stall",
                        lambda pid, m: pid != "inception" and kind == "timeout")


# --------------------------------------------------------------------------- #
# Door 1: the opening pick
# --------------------------------------------------------------------------- #

def test_a_healthy_fleet_never_opens_on_it(fleet):
    assert _route()[0] != "inception"


def test_the_reported_case_every_strong_model_just_failed(fleet, monkeypatch):
    """This is the "pool 1" line: it used to open every turn here."""
    _all_out(monkeypatch)
    assert _route()[0] != "inception"


def test_the_case_reproduced_on_the_old_code(fleet, monkeypatch):
    """The rescue rule off = the old code: every strong model STALLED, so the weak clean
    model opens. (The 2026-11-09 rule `recent_fail_keeps_strong` does not change this case:
    it only keeps strong models that failed FAST ahead of weak ones; switched off anyway so
    this stays the old code whatever that rule becomes.)"""
    monkeypatch.setattr(A, "_rescue_pids", lambda: frozenset())
    monkeypatch.setattr(A, "_keep_strong_on_recent_failure_on", lambda: False)
    _all_out(monkeypatch)
    assert _route() == RESCUE


def test_without_the_rescue_rule_a_fast_failure_still_keeps_it_out(fleet, monkeypatch):
    """Every strong model 429'd (milliseconds to ask again): they open, not the weak one."""
    monkeypatch.setattr(A, "_rescue_pids", lambda: frozenset())
    _all_out(monkeypatch, kind="429")
    assert _route()[0] != "inception"


def test_resting_strong_models_do_not_hand_it_the_turn(fleet, monkeypatch):
    monkeypatch.setattr(A, "_tool_turn_sick", lambda pid, m: pid != "inception")
    assert _route()[0] != "inception"


def test_the_same_on_a_chat_turn(fleet, monkeypatch):
    _all_out(monkeypatch)
    assert _route(tools=False)[0] != "inception"


def test_a_simple_turn_does_not_go_to_it_for_speed(fleet, monkeypatch):
    msgs = [{"role": "user", "content": "use the add tool to add 17 and 25"}]
    monkeypatch.setattr(A, "_classify_difficulty", lambda *a, **k: "simple")
    assert _route(msgs=msgs)[0] != "inception"
    assert _route(tools=False, msgs=msgs)[0] != "inception"


def test_when_nothing_else_exists_it_serves(fleet):
    for p in list(fleet):
        if p != "inception":
            fleet[p] = []
    assert _route() == RESCUE


def _config(monkeypatch, tmp_path):
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(tmp_path / "state" / "config.json"))


def test_inception_is_the_default_rescue_provider(monkeypatch, tmp_path):
    _config(monkeypatch, tmp_path)
    assert A._rescue_pids() == frozenset(("inception",))


def test_flag_off_it_is_an_ordinary_provider_again(monkeypatch, tmp_path):
    _config(monkeypatch, tmp_path)
    A.config.set_flag("rescue_only_providers", False)
    assert A._rescue_pids() == frozenset()


def test_the_list_can_be_changed_in_settings(monkeypatch, tmp_path):
    _config(monkeypatch, tmp_path)
    real = A.config.get_setting
    monkeypatch.setattr(A.config, "get_setting",
                        lambda k, d=None: ["other"] if k == "rescue_only_provider_ids" else real(k, d))
    assert A._rescue_pids() == frozenset(("other",))


# --------------------------------------------------------------------------- #
# The chain: last, and always there
# --------------------------------------------------------------------------- #

def test_it_is_the_last_hop(fleet):
    chain = A._rescue_tail([RESCUE, ("nvidia", "strong-a"), ("dahl", "strong-c")])
    assert chain == [("nvidia", "strong-a"), ("dahl", "strong-c"), RESCUE]


def test_a_hop_cap_cannot_cut_it_off(fleet):
    chain = A._rescue_tail([("nvidia", "strong-a"), ("dahl", "strong-c")], est=100)
    assert chain[-1] == RESCUE


def test_only_one_hop(fleet):
    fleet["inception"].append("mercury-2")
    chain = A._rescue_tail([("nvidia", "strong-a"), ("inception", "mercury-2"), RESCUE])
    assert [e for e in chain if e[0] == "inception"] == [("inception", "mercury-2")]


def test_an_explicit_request_keeps_its_head(fleet):
    chain = A._rescue_tail([RESCUE, ("nvidia", "strong-a")], pinned=True)
    assert chain[0] == RESCUE


def test_a_pinned_other_model_keeps_its_place_and_the_rescue_follows(fleet):
    chain = A._rescue_tail([("dahl", "strong-c"), ("nvidia", "strong-a")], pinned=True)
    assert chain == [("dahl", "strong-c"), ("nvidia", "strong-a"), RESCUE]


def test_a_chain_of_nothing_else_is_left_alone(fleet):
    assert A._rescue_tail([RESCUE]) == [RESCUE]


def test_a_rejected_model_stays_out(fleet, monkeypatch):
    monkeypatch.setattr(A, "_normalize_model_identity", lambda m: m)
    assert A._rescue_tail([("nvidia", "strong-a")], veto={"mercury-3"}) == [("nvidia", "strong-a")]


def test_an_image_turn_is_not_handed_to_it(fleet):
    assert A._rescue_tail([("nvidia", "strong-a")], require_vision=True) == [("nvidia", "strong-a")]


def test_a_tool_turn_needs_a_tool_capable_model(fleet, monkeypatch):
    monkeypatch.setattr(A, "_supports_tools", lambda pid, m: pid != "inception")
    assert A._rescue_tail([("nvidia", "strong-a")], require_tools=True) == [("nvidia", "strong-a")]


def test_a_request_too_big_for_it_does_not_get_it(fleet, monkeypatch):
    monkeypatch.setattr(A, "_window_fits", lambda pid, m, est: False)
    assert A._rescue_tail([("nvidia", "strong-a")], est=9000) == [("nvidia", "strong-a")]


def test_a_broken_helper_leaves_the_chain_alone(fleet, monkeypatch):
    monkeypatch.setattr(A, "_rescue_pids", lambda: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        A._rescue_pids()
    assert A._rescue_tail([("nvidia", "strong-a")]) == [("nvidia", "strong-a")]


def test_the_real_chain_ends_on_it(fleet, monkeypatch):
    with A.app.test_request_context("/v1/chat/completions"):
        chain = A._build_chain("nvidia", "strong-a", est=100, require_tools=True)
    assert chain[0] == ("nvidia", "strong-a") and chain[-1] == RESCUE
    assert sum(1 for e in chain if e == RESCUE) == 1


# --------------------------------------------------------------------------- #
# The walk: the end of the clock is its own
# --------------------------------------------------------------------------- #

def _clock(left):
    c = A._ChainClock(tools=True, est=500)
    c.deadline_at = time.monotonic() + left
    c.limit = 240
    return c


def _rescue_on(monkeypatch):
    monkeypatch.setattr(A, "_rescue_pids", lambda: frozenset(("inception",)))


CHAIN = [("nvidia", "strong-a"), ("dahl", "strong-c"), ("other", "mid"), RESCUE]


def test_while_there_is_time_the_chain_runs_in_order(monkeypatch):
    _rescue_on(monkeypatch)
    with A.app.test_request_context("/v1/chat/completions"):
        got = next(_clock(200).walk(list(CHAIN)))
    assert got == CHAIN[0]


def test_when_only_its_reserve_is_left_it_goes_next(monkeypatch):
    _rescue_on(monkeypatch)
    with A.app.test_request_context("/v1/chat/completions"):
        got = next(_clock(A._RESCUE_RESERVE_S - 1).walk(list(CHAIN)))
    assert got == RESCUE


def test_no_hop_may_spend_its_reserve(monkeypatch):
    _rescue_on(monkeypatch)
    with A.app.test_request_context("/v1/chat/completions"):
        c = _clock(100)
        it = c.walk(list(CHAIN))
        next(it)
        budget = c._budget_for("dahl", "strong-c")
    assert budget <= 100 - A._RESCUE_RESERVE_S + 1


def test_its_own_hop_gets_all_that_is_left(monkeypatch):
    _rescue_on(monkeypatch)
    with A.app.test_request_context("/v1/chat/completions"):
        c = _clock(40)
        it = c.walk(list(CHAIN))
        assert next(it) == RESCUE
        budget = c._budget_for("inception", "mercury-3")
    assert budget > 30


def test_with_no_rescue_hop_nothing_is_held_back(monkeypatch):
    _rescue_on(monkeypatch)
    with A.app.test_request_context("/v1/chat/completions"):
        c = _clock(100)
        it = c.walk([("nvidia", "strong-a"), ("dahl", "strong-c")])
        next(it)
        budget = c._budget_for("dahl", "strong-c")
    assert budget > 90


def test_with_no_deadline_it_stays_last(monkeypatch):
    _rescue_on(monkeypatch)
    with A.app.test_request_context("/v1/chat/completions"):
        c = A._ChainClock(tools=True, est=500)
        c.deadline_at = None
        assert list(c.walk(list(CHAIN)))[-1] == RESCUE


def test_a_clock_built_without_init_does_not_break():
    c = A._ChainClock.__new__(A._ChainClock)
    c._rescue = frozenset(("inception",))
    assert c._rescue_jump([RESCUE]) is None
    assert c._rescue_waiting("nvidia") is False


# --------------------------------------------------------------------------- #
# Door 2: the stall backup. Door 3: the fallback. Door 4: the session watch.
# --------------------------------------------------------------------------- #

def test_it_is_never_the_stall_backup(fleet, monkeypatch):
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    monkeypatch.setattr(A, "_recent_hop_failure", lambda pid, m: None)
    monkeypatch.setattr(A, "_swarm_member_sick", lambda pid, m: False)
    with A.app.test_request_context("/v1/chat/completions"):
        c = A._ChainClock(tools=True, est=500)
        c.plan_tool_hedge([("nvidia", "strong-a"), RESCUE])
        assert c.tool_hedge_partner("nvidia", "strong-a") is None
        c = A._ChainClock(tools=True, est=500)
        c.plan_tool_hedge([("nvidia", "strong-a"), RESCUE, ("dahl", "strong-c")])
        assert c.tool_hedge_partner("nvidia", "strong-a") == ("dahl", "strong-c")


def test_the_fallback_does_not_pick_it_for_speed(fleet):
    with A.app.test_request_context("/v1/chat/completions"):
        got = A._quality_fallback_pick([RESCUE, ("dahl", "strong-c"), ("other", "mid")], tools=True)
    assert RESCUE not in got and got


def test_the_fallback_may_when_it_is_all_that_is_left(fleet):
    with A.app.test_request_context("/v1/chat/completions"):
        assert A._quality_fallback_pick([RESCUE], tools=True) == [RESCUE]


def test_the_session_watch_does_not_return_to_it(fleet, monkeypatch):
    monkeypatch.setitem(A._session_return, "k1", ("inception", "mercury-3", time.time() + 60))
    monkeypatch.setitem(A._session_return, "k2", ("nvidia", "strong-a", time.time() + 60))
    assert A._session_return_pair("k1") is None
    assert A._session_return_pair("k2") == ("nvidia", "strong-a")


def test_it_is_never_a_verifier_or_a_specialist(fleet, monkeypatch):
    monkeypatch.setattr(A, "_swarm_member_sick", lambda pid, m: False)
    monkeypatch.setattr(A, "_pair_resting", lambda pid, m: False)
    rows = A._role_candidates([("nvidia", "strong-a"), RESCUE, ("dahl", "strong-c")], ("x", "y"))
    assert "inception" not in {r[0] for r in rows} and rows


def test_a_pin_left_on_it_lets_go_as_soon_as_something_else_lives(fleet, monkeypatch):
    monkeypatch.setattr(A, "_session_pin_get", lambda key: RESCUE)
    monkeypatch.setattr(A, "_agent_pin_key", lambda m: "k")
    assert _route()[0] != "inception"
