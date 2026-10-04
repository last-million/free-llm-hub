"""Stalls are remembered (live trace 2026-10-04, OpenCode coding-multi).

17:02:43 nvidia/z-ai/glm-5.3 silent 45 s -> backup; 17:04:22 the SAME pair was
picked first again and cost another 45 s. 17:03:32 the owner's orchestrator pin
(openrouter space-bunny) answered 200 with an empty message turn after turn,
then glm-5.3 silent, backup 429 -> 180 s gone -> single-model fallback.

Covered here with fakes: a stalled actor is demoted for the next turn, the
backup delay comes from the fleet median, three attempts fit in the turn, an
empty-200 streak rests a pair (a pin too) and a good answer recovers it, a fast
loser is not penalised, a client that left files nothing.
"""
import time

import pytest
import requests

import app as A
import clientgone

TOOLS = [{"type": "function", "function": {"name": "read", "parameters": {}}}]
CHAIN = [("p1", "m1"), ("p2", "m2"), ("p3", "m3")]


def _body():
    return {"model": "swarm", "tools": TOOLS, "stream": False,
            "messages": [{"role": "user", "content": "read the parser module"}]}


class _Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload

    def close(self):
        pass


def _call(cid="c1"):
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": cid, "type": "function",
                        "function": {"name": "read", "arguments": '{"path": "x.py"}'}}]}}]}


def _text(text):
    return {"choices": [{"finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}]}


@pytest.fixture
def roles(monkeypatch):
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("p1", "m1", "hard"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: list(CHAIN))
    monkeypatch.setattr(A, "_record_outcome", lambda *a, **k: None)
    monkeypatch.setattr(A, "_record_chat_usage", lambda *a, **k: None)
    monkeypatch.setattr(A, "_note_nonanswer", lambda *a, **k: None)
    monkeypatch.setattr(A, "_est_tokens", lambda *a, **k: 1000)
    monkeypatch.setattr(A, "_classify_difficulty", lambda *a, **k: "hard")
    monkeypatch.setattr(A, "_swarm_member_sick", lambda *a, **k: None)
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, model: 130.0)
    monkeypatch.setattr(A.prov, "is_model_allowed", lambda m: True)
    monkeypatch.setattr(A, "_tool_turn_race_on", lambda: False)
    monkeypatch.setattr(A, "_team_notes_for_turn", lambda body, *a, **k: body)
    monkeypatch.setattr(A, "_role_verify_and_correct", lambda *a, **k: None)


def _dispatcher(script, calls):
    """script: {pid: (delay, payload, status)}; records (pid, model, t, deadline)."""
    t0 = time.monotonic()

    def go(pid, payload, deadline):
        calls.append((pid, payload.get("model"), time.monotonic() - t0, deadline))
        delay, out, status = script[pid]
        end = time.monotonic() + delay
        while time.monotonic() < end:
            if clientgone.cancelled():
                return None, None
            time.sleep(0.02)
        if out is None:
            return None, requests.RequestException("refused")
        return _Resp(out, status), None
    return go


def _fast_delay(monkeypatch, secs=0.5):
    monkeypatch.setattr(A, "_tool_hedge_delay", lambda *a, **k: secs)


# --------------------------------------------------------------------------- #
# 1. a stalled actor is demoted
# --------------------------------------------------------------------------- #

def test_actor_replaced_by_its_backup_is_remembered(roles, monkeypatch):
    _fast_delay(monkeypatch, 0.5)
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (4.0, _call(), 200), "p2": (0.0, _call("b"), 200),
        "p3": (0.0, _call(), 200)}, calls))
    data, _h = A._swarm_tool_result(_body())
    assert data["model"] == "p2/m2"
    # the ledger _build_chain's tool branch reads: a STALL, so it sits behind
    # the pairs that merely 429'd and behind every unflagged one
    assert A._recent_hop_stall("p1", "m1")
    assert A._recent_hop_failure("p2", "m2") is None
    samples = A._tool_ttft.get(("p1", "m1")) or []
    assert samples and samples[-1] >= 500.0, "a censored sample (>= the silence)"


def test_next_turn_does_not_open_on_the_stalled_pair(monkeypatch):
    """The real _build_chain: after the stall note, the pair is at the tail."""
    A._note_recent_hop_failure("p1", "m1", "timeout")
    assert A._recent_hop_failure("p1", "m1") == "timeout"
    assert A._recent_hop_stall("p1", "m1")
    A._clear_recent_hop_failure("p1", "m1")
    assert A._recent_hop_failure("p1", "m1") is None


def test_stalled_samples_make_the_pair_measured_slow(roles):
    for _ in range(3):
        A._record_speed_sample(A._tool_ttft, "px", "mx", 45000.0)
    assert A._tool_turn_slow("px", "mx")


def test_a_fast_loser_is_not_penalised(roles, monkeypatch):
    """Actor silent past the delay (backup fires) but then answers FIRST: the
    backup lost, ran for less than its own delay, and files nothing."""
    _fast_delay(monkeypatch, 0.3)
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.6, _call("a"), 200), "p2": (5.0, _call("b"), 200),
        "p3": (0.0, _call(), 200)}, calls))
    data, _h = A._swarm_tool_result(_body())
    assert data["model"] == "p1/m1"
    assert [c[0] for c in calls] == ["p1", "p2"]
    assert A._recent_hop_failure("p2", "m2") is None
    assert A._recent_hop_failure("p1", "m1") is None


def test_no_backup_no_penalty(roles, monkeypatch):
    _fast_delay(monkeypatch, 2.0)
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.2, _call(), 200), "p2": (0.0, _call(), 200),
        "p3": (0.0, _call(), 200)}, calls))
    A._swarm_tool_result(_body())
    assert [c[0] for c in calls] == ["p1"]
    assert A._recent_hop_failure("p1", "m1") is None


def test_client_gone_files_nothing(roles, monkeypatch):
    clock = A._ChainClock(tools=True, est=1000)
    monkeypatch.setattr(A, "_client_gone", lambda: True)
    A._note_actor_stall(clock, "pg", "mg", 45.0)
    assert A._recent_hop_failure("pg", "mg") is None
    assert not A._tool_ttft.get(("pg", "mg"))
    A._note_empty_200("pg", "mg")
    assert A._empty_streak("pg", "mg") == 0


# --------------------------------------------------------------------------- #
# 2. backup delay from the fleet median
# --------------------------------------------------------------------------- #

def _seed(pair, ms, n=5):
    with A._outcome_lock:
        A._tool_ttft[pair] = [float(ms)] * n


def test_unmeasured_delay_is_the_fleet_median_times_2_5_clamped():
    assert A._tool_hedge_delay("new", "pair") == A._TOOL_FLEET_CLAMP[1]   # no fleet data: 30
    _seed(("a", "a"), 8000)
    _seed(("b", "b"), 10000)
    _seed(("c", "c"), 12000)
    assert A._tool_hedge_delay("new", "pair") == pytest.approx(25.0)      # 2.5 x 10 s
    _seed(("a", "a"), 1000)
    _seed(("b", "b"), 2000)
    _seed(("c", "c"), 3000)
    assert A._tool_hedge_delay("new", "pair") == 12.0                     # clamp floor
    _seed(("a", "a"), 30000)
    _seed(("b", "b"), 40000)
    _seed(("c", "c"), 50000)
    assert A._tool_hedge_delay("new", "pair") == 30.0                     # clamp cap


def test_fleet_ignores_pairs_with_few_samples():
    _seed(("a", "a"), 10000, n=4)
    assert A._fleet_tool_p50_ms() is None
    _seed(("a", "a"), 10000, n=5)
    assert A._fleet_tool_p50_ms() == pytest.approx(10000.0)


def test_only_a_huge_request_waits_45s_unmeasured():
    _seed(("a", "a"), 10000)
    assert A._tool_hedge_delay("new", "pair", 50000) == pytest.approx(25.0)
    assert A._tool_hedge_delay("new", "pair", 100000) == A._TOOL_HEDGE_UNKNOWN == 45.0


def test_a_measured_pair_keeps_its_own_delay():
    _seed(("a", "a"), 4000, n=3)
    assert A._tool_hedge_delay("a", "a", 500000) == pytest.approx(14.0)   # 3.5 x 4 s


# --------------------------------------------------------------------------- #
# 3. three attempts fit
# --------------------------------------------------------------------------- #

def test_share_math_for_a_180s_turn():
    assert A._role_share(180.0, 3) == pytest.approx(60.0)
    assert A._role_share(75.0, 2) == pytest.approx(37.5)
    assert A._role_share(30.0, 3) == 20.0          # never under 20 s
    assert A._role_share(10.0, 3) == 10.0          # nor over what is left
    assert A._role_share(180.0, 1) == 180.0


def test_first_hop_deadline_is_a_third_of_the_turn(roles, monkeypatch):
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, {"e": 1}, 429), "p2": (0.0, _call(), 200),
        "p3": (0.0, _call(), 200)}, calls))
    clock = A._ChainClock(tools=True, est=1000)
    clock.plan_tool_hedge(CHAIN)
    rec = {"calls": 0, "sent_tokens": 0, "failed": [], "invalid": 0, "hedge": None}
    A._role_actor_hop(clock, _body(), "p1", "m1", 1000, "k", rec, [],
                      time.monotonic() + 180.0, attempts_left=3)
    assert calls[0][3] == pytest.approx(60.0, abs=1.5)


def test_three_attempts_fit_with_a_silent_first_hop(roles, monkeypatch):
    """Scaled 20x (turn 9 s, floor 1 s, delay 1.5 s): actor silent, backup
    silent, the NEXT hop still gets time and serves."""
    monkeypatch.setattr(A, "_SWARM_TOOL_HOP_DEADLINE", 9)
    monkeypatch.setattr(A, "_ROLE_MIN_HOP_SECONDS", 1.0)
    _fast_delay(monkeypatch, 1.5)
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (60.0, _call(), 200), "p2": (60.0, _call(), 200),
        "p3": (0.0, _call("third"), 200)}, calls))
    started = time.monotonic()
    data, hdrs = A._swarm_tool_result(_body())
    assert data["model"] == "p3/m3"
    assert [c[0] for c in calls] == ["p1", "p2", "p3"]
    assert time.monotonic() - started < 9.0
    assert calls[2][2] < 7.0, "the third attempt started at %.1fs" % calls[2][2]


# --------------------------------------------------------------------------- #
# 4. empty-200 streak
# --------------------------------------------------------------------------- #

def _judge_empty(pid="p1", model="m1"):
    return A._role_judge(pid, model, _Resp(_text("")), None,
                         {"tools": TOOLS, "messages": []}, {}, 1000, "k")


def _judge_ok(pid="p1", model="m1"):
    return A._role_judge(pid, model, _Resp(_call()), None,
                         {"tools": TOOLS, "messages": []}, {"stream": False}, 1000, "k")


def test_three_empties_rest_the_pair_and_a_good_answer_clears(roles):
    for _ in range(2):
        assert _judge_empty()["fail"] == "empty"
    assert not A._empty_resting("p1", "m1")
    _judge_empty()
    assert A._empty_resting("p1", "m1")
    assert _judge_ok()["ok"]
    assert not A._empty_resting("p1", "m1")


def test_empties_older_than_10_minutes_do_not_count(roles):
    old = time.time() - A._EMPTY_STREAK_TTL - 5
    with A._empty_200_lock:
        A._empty_200[("p1", "m1")] = [old, old, old]
    assert not A._empty_resting("p1", "m1")


def test_a_resting_pin_is_not_applied_and_applies_again(roles, monkeypatch, caplog):
    monkeypatch.setattr(A, "_available_providers", lambda: {"p1"})
    monkeypatch.setattr(A, "_model_block_reason", lambda p, m: None)
    monkeypatch.setattr(A, "_prefetch_free_models", lambda pids: {"p1": ["m1"]})
    monkeypatch.setattr(A, "_is_model_skipped", lambda p, m: False)
    monkeypatch.setattr(A.quota, "is_model_throttled", lambda p, m: False)
    monkeypatch.setattr(A, "_supports_tools", lambda p, m: True)
    monkeypatch.setattr(A, "_model_ctx_info", lambda p, m: (None, "default"))
    assert A._orch_unusable("p1", "m1", 1000, True) is None
    for _ in range(3):
        _judge_empty()
    with caplog.at_level("INFO"):
        why = A._orch_unusable("p1", "m1", 1000, True)
    assert why == "resting (3 empties)"
    assert "pinned p1/m1 resting (3 empties)" in caplog.text
    assert A._orch_unusable("p1", "m1", 1000, False) is None     # tool turns only
    assert _judge_ok()["ok"]
    assert A._orch_unusable("p1", "m1", 1000, True) is None


def test_a_resting_pair_walks_last(roles, monkeypatch):
    for _ in range(3):
        _judge_empty("p1", "m1")
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, _call(), 200), "p2": (0.0, _call(), 200),
        "p3": (0.0, _call(), 200)}, calls))
    data, _h = A._swarm_tool_result(_body())
    assert [c[0] for c in calls] == ["p2"]
    assert data["model"] == "p2/m2"


def test_a_resting_pair_is_kept_as_the_last_resort(roles, monkeypatch):
    for p, m in CHAIN[:2]:
        for _ in range(3):
            _judge_empty(p, m)
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, _call(), 200), "p2": (0.0, _call(), 200),
        "p3": (0.0, {"e": 1}, 503)}, calls))
    data, _h = A._swarm_tool_result(_body())
    assert [c[0] for c in calls] == ["p3", "p1"]          # rested pairs: tail, not dropped
    assert data["model"] == "p1/m1"


# --------------------------------------------------------------------------- #
# 5. verifier: no verdict -> one strict retry on the next verifier
# --------------------------------------------------------------------------- #

def test_unreadable_verifier_reply_is_retried_strictly_and_a_verdict_is_used(monkeypatch):
    import json
    import test_tool_turn_roles as T          # same fixtures, same dir
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("p1", "m1", "hard"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: list(CHAIN))
    monkeypatch.setattr(A, "_record_outcome", lambda *a, **k: None)
    monkeypatch.setattr(A, "_record_chat_usage", lambda *a, **k: None)
    monkeypatch.setattr(A, "_est_tokens", lambda *a, **k: 1000)
    monkeypatch.setattr(A, "_classify_difficulty", lambda *a, **k: "hard")
    monkeypatch.setattr(A, "_swarm_member_sick", lambda *a, **k: None)
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, model: 130.0)
    monkeypatch.setattr(A.prov, "is_model_allowed", lambda m: True)
    monkeypatch.setattr(A, "_tool_turn_race_on", lambda: False)
    monkeypatch.setattr(A, "_team_notes_for_turn", lambda body, *a, **k: body)
    calls = []
    verdict = json.dumps({"ok": True, "problems": [], "severity": "low"})
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, T._tool_call(cid="orig"), 200),
        "p2": (0.0, _text("I could not tell."), 200),
        "p3": (0.0, _text(verdict), 200)}, calls))
    data, hdrs = A._swarm_tool_result(T._body())
    assert [c[0] for c in calls] == ["p1", "p2", "p3"]
    assert data["model"] == "p1/m1"
    assert "verifier=ok" in hdrs["X-Free-LLM-Hub-Roles"]
