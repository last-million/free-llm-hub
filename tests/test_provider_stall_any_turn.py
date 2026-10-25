"""A provider that stalls on two models is slow for the THIRD one too, on every turn.

MEASURED 2026-10-25 (Activity rows + hub.log, codex `auto`): tool-free turns kept
504'ing on nvidia glm-5.3 -> glm-5.3-flash -> deepseek-v4.1-flash, each sibling a fresh
~120 s stall behind the same slow gateway, while `inception/mercury-3` answered in
seconds once reached -- every success was 198-231 s of a 240 s clock, i.e. the working
provider was only reached after the dead heads had burned the time.

`_provider_stalling` (two different models of a direct provider with a fresh stall mark)
only fed `_tool_turn_sick`, so a tool-FREE chain still treated the unmarked siblings as
fresh. `_recent_hop_failure` now reports such a sibling as recently stalled, which puts it
in the recent-failure tail of the chain and keeps it from opening the turn. Reorder only
(the tail keeps it), relays excluded, flag `provider_stall_any_turn`.
"""
import time

import pytest

import app as A

HARD = [{"role": "user", "content": "Explain in depth how a B-tree rebalances."}]


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(A, "_recent_hop_fail", {})
    monkeypatch.setattr(A, "_pair_rest", {})
    monkeypatch.setattr(A, "_chain_seen", {})
    monkeypatch.setattr(A.perfstats, "save", lambda *a, **k: False)
    monkeypatch.setattr(A, "_save_learned_state", lambda *a, **k: False)


def _mark(pid, model, kind="timeout", ago=30):
    A._recent_hop_fail[(pid, model)] = (time.time() - ago, kind)


def test_an_unmarked_sibling_reads_as_recently_stalled():
    _mark("nvidia", "z-ai/glm-5.3")
    assert A._recent_hop_failure("nvidia", "deepseek-ai/deepseek-v4.1-flash") is None
    _mark("nvidia", "z-ai/glm-5.3-flash", "deadline")
    assert A._recent_hop_stall("nvidia", "deepseek-ai/deepseek-v4.1-flash")
    # another provider is untouched
    assert A._recent_hop_failure("inception", "mercury-3") is None


def test_a_pairs_own_record_wins():
    _mark("nvidia", "a", "429")
    _mark("nvidia", "b")
    _mark("nvidia", "c")
    assert A._recent_hop_failure("nvidia", "a") == "429"      # not rewritten to a stall
    assert A._recent_hop_failure("nvidia", "d") == "timeout"


def test_a_relay_is_never_judged_by_its_siblings():
    _mark("g4f", "srv_a:gpt-5")
    _mark("g4f", "srv_b:glm-5.3")
    assert A._recent_hop_failure("g4f", "srv_c:claude-opus-5.5") is None


def test_the_sibling_mark_expires_with_the_stalls():
    _mark("nvidia", "a")
    _mark("nvidia", "b")
    assert A._recent_hop_failure("nvidia", "c")
    for key in list(A._recent_hop_fail):
        A._recent_hop_fail[key] = (time.time() - A._RECENT_FAIL_TTL - 5, "timeout")
    assert A._recent_hop_failure("nvidia", "c") is None


def test_the_flag_turns_it_off(monkeypatch):
    _mark("nvidia", "a")
    _mark("nvidia", "b")
    orig = A.config.get_flag
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=None: False if k == "provider_stall_any_turn" else orig(k, d))
    assert A._recent_hop_failure("nvidia", "c") is None


# --- the chain a tool-free turn actually walks ------------------------------------------

@pytest.fixture
def fleet(monkeypatch):
    world = {"pa": ["pa-1", "pa-2", "pa-3"], "pb": ["pb-1"], "pc": ["pc-1"]}
    scores = {"pa-1": 100.0, "pa-2": 95.0, "pa-3": 90.0, "pb-1": 80.0, "pc-1": 70.0}
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
    monkeypatch.setattr(A.config, "get_flag", lambda k, d=None: d)
    for name in ("_reliability_penalty", "_latency_penalty", "_answer_quality_penalty"):
        monkeypatch.setattr(A, name, lambda pid, m: 0.0)
    monkeypatch.setattr(A, "_chain_reliability_band", lambda pid, m: 0)
    return world


def _chain(msgs=HARD):
    with A.app.test_request_context():
        A._mark_turn_shape("hard", 400)
        return A._build_chain("", "", 400, messages=msgs)


def test_a_tool_free_chain_tries_the_other_providers_before_the_stalling_one(fleet):
    # The real shape: two models of one gateway stalled, the strongest sibling did
    # not -- and it used to open the turn (a fresh 120 s stall behind the same host).
    _mark("pa", "pa-2")
    _mark("pa", "pa-3")
    chain = _chain()
    assert chain[0][0] != "pa", chain
    pos = {hop: i for i, hop in enumerate(chain)}
    assert pos[("pb", "pb-1")] < pos[("pa", "pa-1")], chain
    assert pos[("pc", "pc-1")] < pos[("pa", "pa-1")], chain
    assert ("pa", "pa-1") in chain, "demoted, never removed"


def test_without_the_marks_the_strong_provider_still_leads(fleet):
    chain = _chain()
    assert chain[0] == ("pa", "pa-1"), chain


def test_one_stalled_model_does_not_rest_its_siblings(fleet):
    _mark("pa", "pa-2")
    chain = _chain()
    assert chain[0] == ("pa", "pa-1"), chain


def test_the_flag_off_is_the_old_per_pair_chain(fleet, monkeypatch):
    _mark("pa", "pa-2")
    _mark("pa", "pa-3")
    orig = A.config.get_flag
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=None: False if k == "provider_stall_any_turn" else orig(k, d))
    chain = _chain()
    assert chain[0] == ("pa", "pa-1"), chain


def test_the_router_does_not_open_a_turn_on_the_stalling_provider(fleet):
    _mark("pa", "pa-2")
    _mark("pa", "pa-3")
    _p, model, _d = A._route_by_difficulty(HARD, None, 400)
    assert model in ("pb-1", "pc-1"), model
    # every candidate stalled: still answers (fail-open)
    for pid, models in fleet.items():
        for m in models:
            _mark(pid, m)
    _p, model, _d = A._route_by_difficulty(HARD, None, 400)
    assert model is not None
