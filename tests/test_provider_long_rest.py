"""A provider that keeps stalling rests for 30 minutes, with one test request at a time.

MEASURED live 2026-11-09 (evening): the provider `nvidia` timed out on ALL its models. The hub
learns that one model at a time, and only once each ~2 minute timeout has finished, so several
OpenCode sessions that opened a tool turn on nvidia in the same minute each waited 127-171 s
before a weak model served anyway. And every stalled model gets a new try when its ten-minute mark
(`_RECENT_FAIL_TTL`) runs out, so the burst came back about every ten minutes.

THE RULE (flag `provider_stall_long_rest`, default on; off = the behaviour before it):

* two DIFFERENT models of a direct provider stall inside ten minutes -> the PROVIDER rests for
  30 minutes; `_provider_stalling` stays true for that long whatever the per-pair marks do, so
  every consumer that already reads it follows unchanged;
* when the rest runs out the provider is on probation: the next hop really dispatched to it
  (`_dispatch_chat`) is its ONE test request, and while that is in flight everybody else still
  sees the provider as stalling;
* an answer from any of its models lifts everything; a stall on probation rests it again at once;
  a test request that never resolves frees its slot after 300 s;
* relays never; nothing for a client that left or a local network failure; reorder only, never a
  block; persisted with the learned state.

Hermetic: a fake wall clock, fake upstreams, fresh ledgers, no network, no sleeping.
"""
import json
import logging
import time
from unittest import mock

import pytest
import requests

import app as A
import config
import learnstate

REST = A._PROVIDER_STALL_REST_S
PROBE = A._PROVIDER_PROBE_S
TTL = A._RECENT_FAIL_TTL
REAL_SAVE = A._save_learned_state

GLM = "z-ai/glm-5.3"
DEEPSEEK = "deepseek-ai/deepseek-v4.1-flash"
KIMI = "moonshotai/kimi-k3"


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    for name in ("_recent_hop_fail", "_pair_rest", "_empty_200", "_chain_seen", "_outcomes",
                 "_tool_outcomes", "_latencies", "_provider_rest", "_psr_flag_cache"):
        monkeypatch.setattr(A, name, {})
    monkeypatch.setattr(A.perfstats, "save", lambda *a, **k: False)
    monkeypatch.setattr(A, "_save_learned_state", lambda *a, **k: False)


@pytest.fixture
def clock(monkeypatch):
    """The wall clock every ledger here reads (`time.time`), moved by hand."""
    c = {"t": time.time()}
    monkeypatch.setattr(A.time, "time", lambda: c["t"])
    return c


def _stall(pid, model, kind="timeout"):
    """What the chain walk files when a hop ran out its time."""
    A._note_recent_hop_failure(pid, model, kind)


def _rest_nvidia(clock):
    """Two different nvidia models stall: the provider-wide rest starts. Returns its end."""
    _stall("nvidia", GLM)
    _stall("nvidia", DEEPSEEK, "deadline")
    return clock["t"] + REST


def _to_probation(clock):
    """...and the rest has just run out. Returns the end of that rest."""
    until = _rest_nvidia(clock)
    clock["t"] += REST + 1
    return until


def _flags(monkeypatch, **over):
    real = config.get_flag
    monkeypatch.setattr(config, "get_flag",
                        lambda k, d=None: over[k] if k in over else real(k, d))


class _Resp:
    def __init__(self, status):
        self.status_code = status


# --------------------------------------------------------------------------- #
# 1. The rest
# --------------------------------------------------------------------------- #

def test_two_different_stalled_models_start_a_thirty_minute_rest(clock):
    _stall("nvidia", GLM)
    assert not A._provider_stalling("nvidia")          # one stall: only that pair's business
    assert A._provider_rest == {}
    _stall("nvidia", DEEPSEEK, "deadline")
    assert REST == 1800
    assert A._provider_rest["nvidia"] == {"until": clock["t"] + REST, "n": 1, "probe": None}
    assert A._provider_stalling("nvidia")
    assert not A._provider_stalling("groq")


def test_it_is_still_resting_after_the_ten_minute_marks_have_expired(clock):
    _rest_nvidia(clock)
    clock["t"] += TTL + 60                              # 11 min: every per-pair mark is gone
    assert A._provider_stalled_models("nvidia", clock["t"]) == set()
    assert A._provider_stalling("nvidia")
    # every consumer that already reads it follows, with no change of its own
    assert A._tool_turn_sick("nvidia", KIMI)
    assert A._recent_hop_stall("nvidia", GLM)           # its own mark expired; the rest answers
    assert A._recent_hop_failure("nvidia", "never/tried") == "timeout"
    assert not A._tool_turn_sick("groq", "qwen/qwen3.8-27b")
    assert A._recent_hop_failure("groq", "qwen/qwen3.8-27b") is None
    clock["t"] += REST - TTL - 61                       # one second before the 30 minutes are up
    assert A._provider_stalling("nvidia")
    clock["t"] += 2                                     # one second after: probation, slot free
    assert not A._provider_stalling("nvidia")
    assert not A._tool_turn_sick("nvidia", KIMI)


def test_one_model_stalling_twice_does_not_start_it(clock):
    _stall("nvidia", GLM)
    clock["t"] += 30
    _stall("nvidia", GLM, "reasoning_stall")
    assert A._provider_rest == {}
    assert not A._provider_stalling("nvidia")


@pytest.mark.parametrize("kind", ["429", "junk", "invalid", "announced", "content_filter"])
def test_only_stall_kinds_start_it(clock, kind):
    _stall("nvidia", "a", kind)
    _stall("nvidia", "b", kind)
    assert A._provider_rest == {}
    assert not A._provider_stalling("nvidia")


def test_two_stalls_more_than_ten_minutes_apart_do_not_start_it(clock):
    _stall("nvidia", "a")
    clock["t"] += TTL + 5
    _stall("nvidia", "b")
    assert A._provider_rest == {}
    assert not A._provider_stalling("nvidia")


def test_a_relay_never_rests_as_a_whole(clock):
    # g4f's models are separate backends; the relay layer has its own per-server logic.
    _stall("g4f", "srv_a:gpt-5")
    _stall("g4f", "srv_b:glm-5.3")
    assert A._is_relay_pid("g4f")
    assert A._provider_rest == {}
    assert not A._provider_stalling("g4f")
    A._provider_rest["g4f"] = {"until": clock["t"] + 600, "n": 1, "probe": None}   # by hand
    assert not A._provider_stalling("g4f")


def test_a_running_rest_runs_on_while_two_models_still_stall(clock):
    first = _rest_nvidia(clock)
    clock["t"] += 300
    _stall("nvidia", KIMI)                              # a third model, the marks still fresh
    assert A._provider_rest["nvidia"]["until"] == clock["t"] + REST > first
    assert A._provider_rest["nvidia"]["n"] == 1         # the same rest, not a new one
    clock["t"] += TTL + 60                              # every mark has expired
    until = A._provider_rest["nvidia"]["until"]
    _stall("nvidia", KIMI)                              # one lone stall during the rest
    assert A._provider_rest["nvidia"]["until"] == until


def test_a_pairs_own_record_still_wins_during_the_rest(clock):
    _rest_nvidia(clock)
    clock["t"] += TTL + 60
    A._note_recent_hop_failure("nvidia", "busy/model", "429")
    assert A._recent_hop_failure("nvidia", "busy/model") == "429"      # not rewritten to a stall
    assert A._recent_hop_failure("nvidia", "other/model") == "timeout"


def test_the_ten_minute_rule_itself_is_untouched(clock):
    # Marks written straight into the ledger (a restore, the older tests): the old rule answers
    # exactly as before, and a pure query never starts a rest by itself.
    now = clock["t"]
    A._recent_hop_fail[("nvidia", "a")] = (now - 30, "timeout")
    A._recent_hop_fail[("nvidia", "b")] = (now - 30, "deadline")
    assert A._provider_stalling("nvidia")
    assert A._provider_rest == {}
    clock["t"] += TTL
    assert not A._provider_stalling("nvidia")


# --------------------------------------------------------------------------- #
# 2. An answer lifts it
# --------------------------------------------------------------------------- #

def test_any_answer_from_the_provider_lifts_the_rest(clock):
    _rest_nvidia(clock)
    clock["t"] += TTL + 60
    assert A._provider_stalling("nvidia")
    A._record_outcome("nvidia", "some/other-model", True)       # ANY model: it answers again
    assert A._provider_rest == {}
    assert not A._provider_stalling("nvidia")
    assert not A._tool_turn_sick("nvidia", KIMI)


def test_a_failure_or_another_providers_answer_lifts_nothing(clock):
    until = _rest_nvidia(clock)
    clock["t"] += TTL + 60
    A._record_outcome("nvidia", GLM, False)
    A._record_outcome("nvidia", GLM, False, junk=True)
    A._record_outcome("groq", "qwen/qwen3.8-27b", True)
    assert A._provider_rest["nvidia"]["until"] == until
    assert A._provider_stalling("nvidia")


def test_a_passing_provider_test_lifts_the_rest(clock):
    # The owner presses Test on the card and a real generation succeeds: it answers.
    _rest_nvidia(clock)
    clock["t"] += TTL + 60
    A._unpark_after_verified_generation("nvidia", [KIMI])
    assert A._provider_rest == {}
    assert not A._provider_stalling("nvidia")


# --------------------------------------------------------------------------- #
# 3. Probation: one test request at a time
# --------------------------------------------------------------------------- #

def test_probation_lets_exactly_one_test_request_through(clock):
    _to_probation(clock)
    assert not A._provider_stalling("nvidia")
    token = A._psr_claim_probe("nvidia")
    assert token is not None
    # while it is in flight, everybody else still sees the provider as stalling
    assert A._provider_stalling("nvidia")
    assert A._tool_turn_sick("nvidia", KIMI)
    assert A._recent_hop_failure("nvidia", KIMI) == "timeout"
    assert A._psr_claim_probe("nvidia") is None
    assert A._psr_claim_probe("nvidia") is None
    assert A._provider_rest["nvidia"]["probe"] is token


def test_asking_is_not_claiming(clock):
    _to_probation(clock)
    for _ in range(5):
        assert not A._provider_stalling("nvidia")
        assert not A._psr_holds("nvidia")
        assert not A._tool_turn_sick("nvidia", KIMI)
        assert A._recent_hop_failure("nvidia", KIMI) is None
        assert A._psr_walk_pick([("nvidia", "a"), ("groq", "x")], 0) == 0
    assert A._provider_rest["nvidia"]["probe"] is None
    assert A._psr_claim_probe("nvidia") is not None             # the slot was still free


def test_there_is_no_test_request_while_the_rest_still_runs(clock):
    _rest_nvidia(clock)
    clock["t"] += REST - 5
    assert A._psr_claim_probe("nvidia") is None     # a last-resort hop, not a test request
    assert A._provider_rest["nvidia"]["probe"] is None
    assert A._psr_claim_probe("groq") is None       # nothing on record: an ordinary hop
    assert "groq" not in A._provider_rest


def test_a_stalled_test_request_starts_a_new_rest_at_once(clock):
    _to_probation(clock)
    assert A._psr_claim_probe("nvidia") is not None
    clock["t"] += 120
    _stall("nvidia", KIMI)                          # ONE model, and no other fresh mark
    assert len(A._provider_stalled_models("nvidia", clock["t"])) == 1
    assert A._provider_rest["nvidia"] == {"until": clock["t"] + REST, "n": 2, "probe": None,
                                          "said": clock["t"] - 120}
    assert A._provider_stalling("nvidia")
    assert A._psr_claim_probe("nvidia") is None     # resting again: no test request
    clock["t"] += REST + 1
    assert A._psr_claim_probe("nvidia") is not None             # ...until that rest is over too


def test_a_stall_on_probation_rests_it_again_even_with_no_test_request_in_flight(clock):
    # e.g. a last-resort hop sent during the rest that times out just after it ended
    _to_probation(clock)
    _stall("nvidia", KIMI)
    assert A._provider_rest["nvidia"]["until"] == clock["t"] + REST
    assert A._provider_rest["nvidia"]["n"] == 2


def test_a_test_request_that_answers_lifts_everything(clock):
    _to_probation(clock)
    assert A._psr_claim_probe("nvidia") is not None
    clock["t"] += 20
    A._record_outcome("nvidia", KIMI, True)
    assert A._provider_rest == {}
    assert not A._provider_stalling("nvidia")
    assert A._psr_claim_probe("nvidia") is None     # an ordinary provider again
    _stall("nvidia", "a")                           # ...and it takes two models again
    assert A._provider_rest == {}


def test_a_test_request_that_never_resolves_expires_by_itself(clock):
    _to_probation(clock)
    first = A._psr_claim_probe("nvidia")
    assert PROBE == 300
    clock["t"] += PROBE - 1
    assert A._provider_stalling("nvidia")
    assert A._psr_claim_probe("nvidia") is None
    clock["t"] += 2
    assert not A._provider_stalling("nvidia")
    second = A._psr_claim_probe("nvidia")
    assert second is not None and second is not first
    assert A._provider_stalling("nvidia")


def test_a_probation_nobody_resolved_is_forgotten(clock):
    _to_probation(clock)
    clock["t"] += A._PROVIDER_PROBATION_S
    assert not A._provider_stalling("nvidia")
    assert A._psr_claim_probe("nvidia") is None
    assert A._provider_rest == {}
    _stall("nvidia", "a")                           # one stall is that pair's business again
    assert A._provider_rest == {}


def test_a_refusal_frees_the_slot_at_once_and_a_200_or_a_timeout_keeps_it(clock):
    _to_probation(clock)
    t = A._psr_claim_probe("nvidia")
    A._psr_probe_done("nvidia", t, _Resp(200), None)            # the answer is judged later
    assert A._provider_rest["nvidia"]["probe"] is t
    A._psr_probe_done("nvidia", t, None, requests.exceptions.ReadTimeout())   # the caller files it
    assert A._provider_rest["nvidia"]["probe"] is t
    A._psr_probe_done("nvidia", t, _Resp(429), None)            # a refusal: no verdict
    assert A._provider_rest["nvidia"]["probe"] is None
    assert not A._provider_stalling("nvidia")
    t2 = A._psr_claim_probe("nvidia")
    A._psr_probe_done("nvidia", t2, None, requests.exceptions.ConnectionError())
    assert A._provider_rest["nvidia"]["probe"] is None
    t3 = A._psr_claim_probe("nvidia")
    A._psr_probe_done("nvidia", t3, None, None)                 # it raised before it was sent
    assert A._provider_rest["nvidia"]["probe"] is None
    assert A._provider_rest["nvidia"]["n"] == 1                 # none of that is a new rest


def test_an_old_token_never_frees_a_newer_test_request(clock):
    _to_probation(clock)
    old = A._psr_claim_probe("nvidia")
    clock["t"] += PROBE + 1
    new = A._psr_claim_probe("nvidia")
    A._psr_probe_done("nvidia", old, _Resp(500), None)
    assert A._provider_rest["nvidia"]["probe"] is new
    A._psr_probe_done("nvidia", None, _Resp(500), None)         # an ordinary hop: nothing
    assert A._provider_rest["nvidia"]["probe"] is new


# --------------------------------------------------------------------------- #
# 4. Where the slot is taken: _dispatch_chat, the point a hop is really sent
# --------------------------------------------------------------------------- #

def _dispatch(monkeypatch, upstream, pid="nvidia", model=KIMI, stream=False):
    monkeypatch.setattr(A, "_upstream_chat", upstream)
    monkeypatch.setattr(A, "_record_latency", lambda *a, **k: None)
    return A._dispatch_chat(pid, {"model": model, "messages": []}, stream)


def test_the_slot_is_taken_at_dispatch_and_nobody_is_ever_blocked(clock, monkeypatch):
    _to_probation(clock)
    sent = []

    def upstream(pid, payload, stream):
        sent.append((pid, payload["model"]))
        return _Resp(200)

    assert _dispatch(monkeypatch, upstream).status_code == 200
    held = A._provider_rest["nvidia"]["probe"]
    assert held is not None
    assert A._provider_stalling("nvidia")
    # A second request that still reaches the provider (it is its last resort, or the user
    # named provider/model) is SENT all the same -- and it is not a second test request.
    assert _dispatch(monkeypatch, upstream, model=GLM).status_code == 200
    assert A._provider_rest["nvidia"]["probe"] is held
    assert sent == [("nvidia", KIMI), ("nvidia", GLM)]


def test_a_streamed_hop_takes_the_slot_too(clock, monkeypatch):
    _to_probation(clock)
    resp = mock.Mock(status_code=200)
    assert _dispatch(monkeypatch, lambda *a: resp, stream=True) is resp
    assert A._provider_rest["nvidia"]["probe"] is not None      # headers are not a verdict
    A._record_outcome("nvidia", KIMI, True)                     # the stream delivered
    assert A._provider_rest == {}


def test_a_fast_refusal_at_dispatch_frees_the_slot(clock, monkeypatch):
    _to_probation(clock)
    assert _dispatch(monkeypatch, lambda *a: _Resp(429)).status_code == 429
    assert A._provider_rest["nvidia"]["probe"] is None
    assert not A._provider_stalling("nvidia")       # the next request may be the test


def test_a_read_timeout_at_dispatch_keeps_the_slot_until_the_stall_is_filed(clock, monkeypatch):
    _to_probation(clock)

    def upstream(*a):
        raise requests.exceptions.ReadTimeout("no answer")

    with pytest.raises(requests.exceptions.ReadTimeout):
        _dispatch(monkeypatch, upstream)
    assert A._provider_rest["nvidia"]["probe"] is not None
    _stall("nvidia", KIMI)                          # what _ChainClock._plain does next
    assert A._provider_rest["nvidia"]["n"] == 2
    assert A._provider_rest["nvidia"]["probe"] is None
    assert A._provider_rest["nvidia"]["until"] == clock["t"] + REST


def test_through_the_chain_clock_a_test_request_that_times_out_rests_the_provider_again(
        clock, monkeypatch):
    # The real path of a chain hop: _ChainClock.dispatch -> _dispatch_chat (the slot) ->
    # the read timeout -> _note_recent_hop_failure (the new rest). Nothing simulated by hand.
    _to_probation(clock)

    def upstream(*a):
        raise requests.exceptions.ReadTimeout("no answer")

    monkeypatch.setattr(A, "_upstream_chat", upstream)
    with A.app.test_request_context("/v1/chat/completions"):
        hop = A._ChainClock(tools=True, est=500)
        with pytest.raises(requests.exceptions.ReadTimeout):
            hop.dispatch("nvidia", {"model": KIMI, "messages": []}, False)
    assert A._provider_rest["nvidia"]["n"] == 2
    assert A._provider_rest["nvidia"]["probe"] is None
    assert A._provider_rest["nvidia"]["until"] == clock["t"] + REST
    assert A._provider_stalling("nvidia")


def test_a_hop_that_never_left_frees_the_slot(clock, monkeypatch):
    _to_probation(clock)

    def upstream(*a):
        raise RuntimeError("no api key for provider nvidia")

    with pytest.raises(RuntimeError):
        _dispatch(monkeypatch, upstream)
    assert A._provider_rest["nvidia"]["probe"] is None


def test_an_ordinary_dispatch_touches_nothing(clock, monkeypatch):
    _dispatch(monkeypatch, lambda *a: _Resp(200), pid="groq", model="qwen/qwen3.8-27b")
    assert A._provider_rest == {}
    # while the rest still runs, a hop that reaches the provider is a last resort, not a test
    _rest_nvidia(clock)
    assert _dispatch(monkeypatch, lambda *a: _Resp(200)).status_code == 200
    assert A._provider_rest["nvidia"]["probe"] is None


# --------------------------------------------------------------------------- #
# 5. The walk: a chain built before the rest / the test request began
# --------------------------------------------------------------------------- #

CHAIN = [("nvidia", "a"), ("groq", "x"), ("nvidia", "b"), ("dahl", "y")]
STEPPED = [("groq", "x"), ("dahl", "y"), ("nvidia", "a"), ("nvidia", "b")]


def _walk(chain, **kw):
    with A.app.test_request_context("/v1/chat/completions"):
        return list(A._ChainClock(**kw).walk(list(chain)))


def _resting(clock, pid="nvidia"):
    A._provider_rest[pid] = {"until": clock["t"] + 600, "n": 1, "probe": None}


@pytest.mark.parametrize("kw", [{"tools": True, "est": 500}, {"tools": False, "est": 6853}],
                         ids=["tool-turn", "tool-free"])
def test_a_chain_built_before_the_rest_began_steps_over_the_resting_provider(clock, kw):
    assert _walk(CHAIN, **kw) == CHAIN              # nothing rests: the chain as built
    _resting(clock)
    assert _walk(CHAIN, **kw) == STEPPED            # reordered, nothing dropped or added


def test_a_hop_steps_behind_while_another_request_holds_the_test_request(clock):
    _to_probation(clock)
    assert _walk(CHAIN, tools=True, est=500) == CHAIN           # slot free: this walk may test
    assert A._psr_claim_probe("nvidia") is not None             # another request got there first
    assert _walk(CHAIN, tools=True, est=500) == STEPPED
    clock["t"] += PROBE + 1                                     # that test never resolved
    assert _walk(CHAIN, tools=True, est=500) == CHAIN


def test_the_head_the_user_named_is_never_moved(clock):
    _resting(clock)
    got = _walk(CHAIN, tools=True, est=500, pinned=True)
    assert got == [("nvidia", "a"), ("groq", "x"), ("dahl", "y"), ("nvidia", "b")], got


def test_with_nothing_else_left_the_resting_provider_is_still_walked(clock):
    _resting(clock)
    only = [("nvidia", "a"), ("nvidia", "b")]
    assert _walk(only, tools=True, est=500) == only
    assert _walk([("nvidia", "a")], tools=True, est=500) == [("nvidia", "a")]


def test_the_rescue_hop_is_never_what_the_step_promotes(clock, monkeypatch):
    monkeypatch.setattr(A, "_rescue_pids", lambda: frozenset({"inception"}))
    _resting(clock)
    chain = [("nvidia", "a"), ("inception", "mercury-3")]
    assert _walk(chain, tools=True, est=500) == chain


def test_a_benched_pair_is_never_what_the_step_promotes(clock, monkeypatch):
    monkeypatch.setattr(A, "_is_pair_benched", lambda pid, m: (pid, m) == ("groq", "x"))
    _resting(clock)
    got = _walk([("nvidia", "a"), ("groq", "x"), ("dahl", "y")], tools=False, est=6853)
    assert got == [("dahl", "y"), ("nvidia", "a"), ("groq", "x")], got


# --------------------------------------------------------------------------- #
# 6. The chain a turn really walks, twenty minutes later
# --------------------------------------------------------------------------- #

HARD = [{"role": "user", "content": "Explain in depth how a B-tree rebalances."}]


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


def test_twenty_minutes_later_the_chain_still_tries_the_other_providers_first(fleet, clock):
    _stall("pa", "pa-2")
    _stall("pa", "pa-3")
    clock["t"] += 20 * 60                           # both ten-minute marks are long gone
    chain = _chain()
    assert chain[0][0] != "pa", chain
    pos = {hop: i for i, hop in enumerate(chain)}
    assert pos[("pb", "pb-1")] < pos[("pa", "pa-1")], chain
    assert pos[("pc", "pc-1")] < pos[("pa", "pa-1")], chain
    assert {("pa", "pa-1"), ("pa", "pa-2"), ("pa", "pa-3")} <= set(chain), "demoted, never removed"
    _p, model, _d = A._route_by_difficulty(HARD, None, 400)
    assert model in ("pb-1", "pc-1"), model
    # ...and once the provider answers again it leads again
    A._record_outcome("pa", "pa-3", True)
    assert _chain()[0] == ("pa", "pa-1")


def test_with_the_flag_off_the_stalled_provider_leads_again_after_ten_minutes(fleet, clock,
                                                                              monkeypatch):
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=None: False if k == "provider_stall_long_rest" else d)
    _stall("pa", "pa-2")
    _stall("pa", "pa-3")
    assert _chain()[0][0] != "pa"                   # the ten-minute rule, as before
    clock["t"] += 20 * 60
    assert _chain()[0] == ("pa", "pa-1")            # ...and the burst this change removes


# --------------------------------------------------------------------------- #
# 7. Survives a restart
# --------------------------------------------------------------------------- #

def test_the_rest_survives_a_restart(clock):
    until = _rest_nvidia(clock)
    saved = A._ls_pack(clock["t"])["provider_rest"]
    assert saved == [["provider", "nvidia", {"until": until, "n": 1}]]
    A._provider_rest.clear()
    A._recent_hop_fail.clear()
    assert not A._provider_stalling("nvidia")       # the old boot: it has forgotten
    clock["t"] += 700                               # the hub was off for a while
    assert A._ls_restore({"provider_rest": saved}, clock["t"]) == 1
    assert A._provider_rest["nvidia"] == {"until": until, "n": 1, "probe": None}
    assert A._provider_stalling("nvidia")
    assert A._tool_turn_sick("nvidia", KIMI)


def test_a_probation_comes_back_with_its_slot_free(clock):
    until = _to_probation(clock)
    assert A._psr_claim_probe("nvidia") is not None
    saved = A._ls_pack(clock["t"])["provider_rest"]
    assert saved == [["provider", "nvidia", {"until": until, "n": 1}]]      # no probe, no log stamp
    A._provider_rest.clear()
    assert A._ls_restore({"provider_rest": saved}, clock["t"]) == 1
    assert A._provider_rest["nvidia"]["probe"] is None
    assert not A._provider_stalling("nvidia")
    assert A._psr_claim_probe("nvidia") is not None  # the first hop after the restart is the test
    assert A._provider_stalling("nvidia")


def test_expired_rows_are_dropped_and_a_live_entry_is_never_overwritten(clock):
    now = clock["t"]
    gone = now - A._PROVIDER_PROBATION_S - 1
    A._provider_rest["gone"] = {"until": gone, "n": 4, "probe": None}
    assert A._ls_pack(now)["provider_rest"] == []               # dropped on save
    A._provider_rest.clear()
    A._provider_rest["live"] = {"until": now + 1700, "n": 5, "probe": None}
    saved = {"provider_rest": [
        ["provider", "gone", {"until": gone, "n": 4}],              # dropped on restore
        ["provider", "nvidia", {"until": now + 900, "n": 2}],
        ["provider", "g4f", {"until": now + 900, "n": 1}],          # a relay never rests
        ["provider", "far", {"until": now + 10 * 86400, "n": 1}],   # never longer than a fresh rest
        ["provider", "bad", {"until": "soon"}],
        ["provider", "live", {"until": now + 60, "n": 1}],
        None, 7, ["only", "two"],
    ]}
    assert A._ls_restore(saved, now) == 2
    assert set(A._provider_rest) == {"nvidia", "far", "live"}
    assert A._provider_rest["nvidia"] == {"until": now + 900, "n": 2, "probe": None}
    assert A._provider_rest["far"]["until"] == now + REST
    assert A._provider_rest["live"] == {"until": now + 1700, "n": 5, "probe": None}


def test_it_goes_through_the_learned_state_file(clock, monkeypatch, tmp_path):
    path = tmp_path / "learned-state.json"
    monkeypatch.setattr(learnstate, "_path", lambda: str(path))
    monkeypatch.setattr(learnstate, "_last_save", [0.0])
    monkeypatch.setattr(A, "_save_learned_state", REAL_SAVE)
    until = _rest_nvidia(clock)
    assert REAL_SAVE(force=True) is True
    data = json.loads(path.read_text())
    assert data["dicts"]["provider_rest"] == [["provider", "nvidia", {"until": until, "n": 1}]]
    A._provider_rest.clear()
    A._recent_hop_fail.clear()
    assert A._load_learned_state() >= 1
    assert A._provider_rest["nvidia"] == {"until": until, "n": 1, "probe": None}
    assert A._provider_stalling("nvidia")


# --------------------------------------------------------------------------- #
# 8. The switch, the guards, fail-open
# --------------------------------------------------------------------------- #

def test_the_flag_off_is_the_old_behaviour(clock, monkeypatch):
    _flags(monkeypatch, provider_stall_long_rest=False)
    _rest_nvidia(clock)
    assert A._provider_rest == {}                   # nothing is recorded
    assert A._provider_stalling("nvidia")           # the ten-minute rule still stands
    clock["t"] += TTL + 60
    assert not A._provider_stalling("nvidia")       # ...and ends with the marks, as before
    assert not A._tool_turn_sick("nvidia", KIMI)
    assert A._recent_hop_failure("nvidia", KIMI) is None


def test_switching_the_flag_off_lets_go_of_a_rest_already_running(clock, monkeypatch):
    _rest_nvidia(clock)
    clock["t"] += TTL + 60
    assert A._provider_stalling("nvidia")
    assert _walk(CHAIN, tools=True, est=500) == STEPPED
    _flags(monkeypatch, provider_stall_long_rest=False)
    assert not A._provider_stalling("nvidia")
    assert _walk(CHAIN, tools=True, est=500) == CHAIN
    clock["t"] += REST                              # past the rest: no test request either
    assert A._psr_claim_probe("nvidia") is None
    _stall("nvidia", KIMI)                          # ...and a stall does not renew it
    assert A._provider_rest["nvidia"]["n"] == 1


def test_the_older_switch_off_turns_this_off_too(clock, monkeypatch):
    _rest_nvidia(clock)
    clock["t"] += TTL + 60
    _flags(monkeypatch, provider_stall_rest=False)  # "per-pair only"
    assert not A._provider_stalling("nvidia")


def test_a_flag_read_that_raises_counts_as_off(clock, monkeypatch):
    _rest_nvidia(clock)
    clock["t"] += TTL + 60
    assert A._provider_stalling("nvidia")

    def boom(k, d=None):
        raise OSError("config unreadable")

    monkeypatch.setattr(config, "get_flag", boom)
    assert A._psr_on() is False
    assert not A._provider_stalling("nvidia")
    assert A._psr_claim_probe("nvidia") is None
    _stall("nvidia", "a")                           # never raises, files nothing new
    assert A._provider_rest["nvidia"]["n"] == 1


def test_the_flag_is_not_read_once_per_candidate(clock, monkeypatch):
    # config.get_flag re-reads config.json; a resting provider is asked about once per candidate.
    reads = []
    real = config.get_flag
    monkeypatch.setattr(config, "get_flag", lambda k, d=None: (reads.append(k), real(k, d))[1])
    _rest_nvidia(clock)
    clock["t"] += TTL + 60
    del reads[:]
    for _ in range(100):
        assert A._provider_stalling("nvidia")
    assert len(reads) <= 4, len(reads)              # at most a refresh or two, not 200


def test_nothing_is_filed_for_a_client_that_left_or_a_local_network_failure(clock, monkeypatch):
    monkeypatch.setattr(A, "_client_gone", lambda: True)
    _stall("nvidia", "a")
    _stall("nvidia", "b")
    assert A._provider_rest == {} and A._recent_hop_fail == {}
    monkeypatch.setattr(A, "_client_gone", lambda: False)
    monkeypatch.setattr(A, "_local_net_failed", lambda pid, model=None: True)
    _stall("nvidia", "a")
    _stall("nvidia", "b")
    assert A._provider_rest == {} and A._recent_hop_fail == {}


def test_a_client_that_left_during_the_test_request_does_not_rest_the_provider(clock, monkeypatch):
    until = _to_probation(clock)
    assert A._psr_claim_probe("nvidia") is not None
    monkeypatch.setattr(A, "_client_gone", lambda: True)
    _stall("nvidia", KIMI)
    assert A._provider_rest["nvidia"]["until"] == until
    assert A._provider_rest["nvidia"]["n"] == 1


def test_a_helper_that_raises_is_fail_open(clock, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")

    _rest_nvidia(clock)
    clock["t"] += TTL + 60
    assert A._provider_stalling("nvidia")
    with monkeypatch.context() as m:
        m.setattr(A, "_psr_holds", boom)            # the query: an error is "not stalling"
        assert A._provider_stalling("nvidia") is False
        assert A._psr_walk_pick(CHAIN, 0) == 0      # the walk: the chain as built
    with monkeypatch.context() as m:
        m.setattr(A, "_is_relay_pid", boom)
        assert A._provider_stalling("nvidia") is False
    with monkeypatch.context() as m:
        m.setattr(A, "_provider_stalled_models", boom)
        _stall("nvidia", "x")                       # filing: the mark is written, nothing raises
        _stall("nvidia", "y")
        assert ("nvidia", "x") in A._recent_hop_fail and ("nvidia", "y") in A._recent_hop_fail
        assert A._provider_stalling("nvidia") is False
    assert A._provider_stalling("nvidia")           # and nothing was lost meanwhile


def test_broken_records_and_garbage_never_raise(clock):
    A._provider_rest["nvidia"] = {"until": "soon"}
    assert A._provider_stalling("nvidia") is False
    assert A._psr_claim_probe("nvidia") is None
    A._psr_note_stall("nvidia")
    assert A._psr_pack(clock["t"]) == []
    A._psr_probe_done("nvidia", object(), None, None)
    A._psr_note_answer("nvidia")
    assert A._provider_rest == {}
    A._provider_rest["x"] = None
    for pid in (None, "", 7, ["unhashable"]):
        assert A._provider_stalling(pid) is False
        assert A._psr_holds(pid) is False
        assert A._psr_claim_probe(pid) is None
        A._psr_note_stall(pid)
        A._psr_note_answer(pid)
        A._psr_probe_done(pid, object(), None, None)
    assert A._psr_walk_pick([], 0) == 0
    assert A._psr_walk_pick([("nvidia", "a")], 3) == 3
    assert A._psr_walk_pick(None, 0) == 0


# --------------------------------------------------------------------------- #
# 9. The log
# --------------------------------------------------------------------------- #

def _lines(caplog):
    return [r.getMessage() for r in caplog.records if "[provider-rest]" in r.getMessage()]


def test_one_log_line_per_event_with_the_provider_and_counts_only(clock, caplog):
    with caplog.at_level(logging.INFO, logger="free-llm-hub"):
        _rest_nvidia(clock)                         # 1. the rest starts
        clock["t"] += REST + 1
        assert A._psr_claim_probe("nvidia")         # 2. the test request
        _stall("nvidia", KIMI)                      # 3. it stalled: another rest
        clock["t"] += REST + 1
        assert A._psr_claim_probe("nvidia")         # 4. the next test request
        A._record_outcome("nvidia", KIMI, True)     # 5. it answered
    lines = _lines(caplog)
    assert len(lines) == 5, lines
    assert "2 of its models stalled" in lines[0] and "rests 30 min" in lines[0]
    assert "one test request sent" in lines[1]
    assert "stalled again" in lines[2] and "rest 2 in a row" in lines[2]
    assert "one test request sent" in lines[3]
    assert "answered again" in lines[4] and "after 2 rest(s)" in lines[4]
    for line in lines:
        assert "nvidia" in line
        for model in ("glm", "deepseek", "kimi"):
            assert model not in line.lower(), line


def test_fast_refusals_do_not_flood_the_log(clock, caplog):
    _to_probation(clock)
    with caplog.at_level(logging.INFO, logger="free-llm-hub"):
        for _ in range(20):
            token = A._psr_claim_probe("nvidia")
            assert token is not None
            A._psr_probe_done("nvidia", token, _Resp(429), None)
            clock["t"] += 1
    assert len([x for x in _lines(caplog) if "one test request sent" in x]) == 1
