"""Roles instead of racing: a swarm/crew*/multi TOOL turn gives each model a job.

MEASURED hub.log 2026-09-26..10-04: the old race (_swarm_tool_result, now behind flag tool_turn_race) sent one
tool turn to 3.72 models, 4.02 upstream calls per served answer, and 75% of
member calls served nothing. Now: ONE actor (walked fail-fast), a stall
backup only after measured silence, a cross-family verifier only on a risky
step, at most one corrector. The bandit (task-kind nudge) only breaks ties
inside _AUTO_TOP_BAND -- owner rule 2026-10-04.

`bandit` and `verify` are other agents' modules: faked here through
sys.modules, exactly as app.py's lazy accessors find them.
"""
import json
import os
import threading
import time
import types

import pytest
import requests

import app as A
import clientgone
import config


TOOLS = [{"type": "function", "function": {"name": "write_file", "parameters": {}}},
         {"type": "function", "function": {"name": "bash", "parameters": {}}}]
CHAIN = [("p1", "m1"), ("p2", "m2"), ("p3", "m3")]
SCORES = {"m1": 138.0, "m2": 137.0, "m3": 136.0}


def _body(stream=False):
    return {"model": "swarm", "tools": TOOLS, "stream": stream,
            "messages": [{"role": "user", "content": "build the parser module"}]}


class _Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload

    def close(self):
        pass


def _tool_call(name="write_file", args="{}", cid="c1"):
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": cid, "type": "function",
                        "function": {"name": name, "arguments": args}}]}}]}


# A READ-ONLY tool call. The real verify.is_risky does NOT verify a read on a
# hard turn, so these isolate the ACTOR walk: one upstream call, no verifier.
# (The risky write_file path -- where a verifier call is correct and expected
# -- is pinned by test_a_risky_write_fires_the_real_verifier below.)
RO_TOOLS = [{"type": "function", "function": {"name": "read", "parameters": {}}}]


def _ro_body(stream=False):
    return {"model": "swarm", "tools": RO_TOOLS, "stream": stream,
            "messages": [{"role": "user", "content": "read the parser module"}]}


def _ro_call(cid="c1"):
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": cid, "type": "function",
                        "function": {"name": "read", "arguments": '{"path": "x.py"}'}}]}}]}


def _text(text):
    return {"choices": [{"finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}]}


VERDICT_OK = json.dumps({"ok": True, "problems": [], "severity": "low"})
VERDICT_HIGH = json.dumps({"ok": False, "problems": ["deletes the repo"], "severity": "high"})
VERDICT_LOW = json.dumps({"ok": False, "problems": ["style"], "severity": "low"})


class FakeBandit:
    def __init__(self):
        self.nudges = {}
        self.rewards = []
        self.remembered = []
        self.credit_calls = []

    def nudge(self, kind, pid, model, base):
        return base + self.nudges.get((pid, model), 0.0)

    def reward(self, kind, pid, model, value):
        self.rewards.append((kind, pid, model, value))

    def remember_tool_calls(self, ids, pid, model, kind):
        self.remembered.append((list(ids), pid, model, kind))

    def credit_from_messages(self, messages, grade):
        self.credit_calls.append(messages)
        n = 0
        known = {i: (p, m, k) for ids, p, m, k in self.remembered for i in ids}
        for msg in messages or ():
            if isinstance(msg, dict) and msg.get("role") == "tool" \
                    and msg.get("tool_call_id") in known:
                p, m, k = known.pop(msg["tool_call_id"])
                self.reward(k, p, m, grade(msg))
                n += 1
        return n

    def stats(self, limit=10):
        return []


@pytest.fixture
def fake_bandit(monkeypatch):
    mod = types.ModuleType("bandit")
    mod.MAX_NUDGE = 1.0
    mod.task_kind = lambda category, difficulty, tools, est: "kind-%s-%s" % (
        difficulty, "tools" if tools else "text")
    mod.default = FakeBandit()
    mod.configure = lambda path: None
    monkeypatch.setitem(__import__("sys").modules, "bandit", mod)
    return mod.default


@pytest.fixture
def fake_verify(monkeypatch):
    state = {"risky": False, "picked": []}
    mod = types.ModuleType("verify")
    mod.VERIFY_MAX_TOKENS = 300
    mod.family = lambda model_id: str(model_id)[:1]

    def pick(producer, candidates):
        state["picked"].append(list(candidates))
        for c in candidates:
            if (c[0], c[1]) != tuple(producer[:2]):
                return (c[0], c[1])
        return None
    mod.pick_verifier = pick
    mod.is_risky = lambda first, difficulty, observed_pass=None: state["risky"]
    mod.digest = lambda messages, proposed: [{"role": "user", "content": "VERIFY THIS"}]
    mod.parse_verdict = lambda text: json.loads(text)
    mod.corrector_messages = lambda messages, proposed, verdict: (
        list(messages) + [{"role": "user", "content": "CORRECT IT"}])
    monkeypatch.setitem(__import__("sys").modules, "verify", mod)
    return state


@pytest.fixture
def roles(monkeypatch):
    """Pin the routing so only the role behaviour is under test."""
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("p1", "m1", "hard"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: list(CHAIN))
    monkeypatch.setattr(A, "_record_outcome", lambda *a, **k: None)
    monkeypatch.setattr(A, "_record_chat_usage", lambda *a, **k: None)
    monkeypatch.setattr(A, "_note_nonanswer", lambda *a, **k: None)
    monkeypatch.setattr(A, "_est_tokens", lambda *a, **k: 1000)
    monkeypatch.setattr(A, "_classify_difficulty", lambda *a, **k: "hard")
    monkeypatch.setattr(A, "_swarm_member_sick", lambda *a, **k: None)
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, model: SCORES.get(model, 100.0))
    monkeypatch.setattr(A.prov, "is_model_allowed", lambda m: True)
    monkeypatch.setattr(A, "_tool_turn_race_on", lambda: False)
    with A._outcome_lock:
        saved = dict(A._tool_ttft)
        A._tool_ttft.clear()
    yield
    with A._outcome_lock:
        A._tool_ttft.clear()
        A._tool_ttft.update(saved)


def _dispatcher(script, calls):
    """script: {pid: (delay, payload-or-callable, status)}; records each call
    as (pid, model, t, messages)."""
    t0 = time.monotonic()

    def go(pid, payload, deadline):
        calls.append((pid, payload.get("model"), time.monotonic() - t0,
                      payload.get("messages")))
        delay, out, status = script[pid]
        end = time.monotonic() + delay
        while time.monotonic() < end:
            if clientgone.cancelled():
                return None, None
            time.sleep(0.02)
        if out is None:
            return None, requests.RequestException("refused")
        if callable(out):
            out = out(payload)
        return _Resp(out, status), None
    return go


# --------------------------------------------------------------------------- #
# One actor
# --------------------------------------------------------------------------- #

def test_one_actor_serves_a_normal_tool_turn_with_one_call(roles, monkeypatch):
    # A non-risky (read-only) tool turn, so the real verify adds no call: the
    # one actor call is the whole turn. (A risky write is verified -- see
    # test_a_risky_write_fires_the_real_verifier.)
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, _ro_call(), 200), "p2": (0.0, _ro_call(), 200),
        "p3": (0.0, _ro_call(), 200)}, calls))
    data, hdrs = A._swarm_tool_result(_ro_body())
    assert [c[0] for c in calls] == ["p1"], "one upstream call, not a fan-out"
    assert data["model"] == "p1/m1"
    assert data["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "read"
    assert hdrs["X-Free-LLM-Hub-Attempts"] == "1"
    assert "calls=1" in hdrs["X-Free-LLM-Hub-Roles"]


def test_a_read_only_hard_turn_is_one_call_on_the_real_verify(roles, monkeypatch):
    """No fake_verify: the REAL verify.is_risky declines a read, so a hard
    read-only turn stays ONE upstream call and no verifier is named."""
    import verify as real_verify
    assert A._verify() is real_verify              # the real module is in play
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, _ro_call(), 200), "p2": (0.0, _ro_call(), 200),
        "p3": (0.0, _ro_call(), 200)}, calls))
    _data, hdrs = A._swarm_tool_result(_ro_body())
    assert [c[0] for c in calls] == ["p1"]
    assert "verifier=none" in hdrs["X-Free-LLM-Hub-Roles"]


def test_a_risky_write_fires_the_real_verifier(roles, monkeypatch):
    """A write_file step on a hard turn IS risky (real verify.is_risky): the
    actor's call plus one verifier call = 2, rec["verifier"] set, and a
    non-verdict reply fails open to the original answer."""
    import verify as real_verify
    assert A._verify() is real_verify
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, _tool_call(cid="orig"), 200),   # the risky write (actor)
        "p2": (0.0, _text("I could not tell."), 200),   # verifier: no JSON verdict
        "p3": (0.0, _text("I could not tell."), 200)}, calls))
    data, hdrs = A._swarm_tool_result(_body())
    # the actor call, one verifier call, and -- the reply being unreadable --
    # ONE strict-contract retry on the next verifier; neither is p1
    assert len(calls) == 3 and calls[0][0] == "p1"
    assert calls[1][0] != "p1" and calls[2][0] not in ("p1", calls[1][0])
    assert data["model"] == "p1/m1"                 # fail-open keeps the original
    assert data["choices"][0]["message"]["tool_calls"][0]["id"] == "orig"
    assert "verifier=no" in hdrs["X-Free-LLM-Hub-Roles"]   # "no verdict"


def test_the_verifier_is_never_the_model_that_just_stalled(roles, monkeypatch):
    """After a stall hedge the actor that stalled lost the race; it must not
    then be picked as the independent verifier (it would re-call a model that
    just timed out)."""
    import verify as real_verify
    assert A._verify() is real_verify
    _fast_hedge(monkeypatch)
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (3.0, _tool_call(cid="slow"), 200),   # actor stalls, then (unused) writes
        "p2": (0.0, _tool_call(cid="won"), 200),    # backup wins with a risky write
        "p3": (0.0, _text(VERDICT_OK), 200)}, calls))
    data, _h = A._swarm_tool_result(_body())
    assert data["model"] == "p2/m2"
    dispatched = [c[0] for c in calls]
    assert "p1" in dispatched and dispatched.count("p1") == 1, \
        "the stalled actor was re-called as the verifier"
    assert "p3" in dispatched, "a healthy third model should verify the backup"


def test_429_walks_on_to_the_next_actor(roles, monkeypatch, fake_bandit):
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, {"error": "rate"}, 429), "p2": (0.0, _ro_call(), 200),
        "p3": (0.0, _ro_call(), 200)}, calls))
    data, _h = A._swarm_tool_result(_ro_body())
    assert [c[0] for c in calls] == ["p1", "p2"]
    assert data["model"] == "p2/m2"
    # a 429 is the provider's quota, never a quality signal
    assert not [r for r in fake_bandit.rewards if r[1] == "p1"]


def test_empty_200_walks_on(roles, monkeypatch):
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, _text(""), 200), "p2": (0.0, _ro_call(), 200),
        "p3": (0.0, _ro_call(), 200)}, calls))
    data, _h = A._swarm_tool_result(_ro_body())
    assert [c[0] for c in calls] == ["p1", "p2"]
    assert data["model"] == "p2/m2"


def test_invalid_tool_call_is_a_failed_hop_and_rewards_zero(roles, monkeypatch, fake_bandit):
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, _tool_call(name="no_such_tool"), 200),
        "p2": (0.0, _tool_call(), 200), "p3": (0.0, _tool_call(), 200)}, calls))
    data, _h = A._swarm_tool_result(_body())
    assert data["model"] == "p2/m2"
    assert ("kind-hard-tools", "p1", "m1", 0.0) in fake_bandit.rewards


def test_prose_announcement_walks_on(roles, monkeypatch):
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, _text("I will now write the parser file."), 200),
        "p2": (0.0, _tool_call(), 200), "p3": (0.0, _tool_call(), 200)}, calls))
    data, _h = A._swarm_tool_result(_body())
    assert data["model"] == "p2/m2"


def test_no_actor_delivers_returns_none_for_the_fallback(roles, monkeypatch):
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, {"e": 1}, 503), "p2": (0.0, {"e": 1}, 503),
        "p3": (0.0, None, 200)}, calls))
    assert A._swarm_tool_result(_body()) is None
    assert len(calls) == 3


# --------------------------------------------------------------------------- #
# Stall backup
# --------------------------------------------------------------------------- #

def test_hedge_delay_is_max_floor_and_measured_p50():
    with A._outcome_lock:
        saved = A._tool_ttft.get(("hp", "hm"))
        A._tool_ttft[("hp", "hm")] = [4000.0, 4000.0, 4000.0]
    try:
        assert A._tool_hedge_delay("hp", "hm") == pytest.approx(14.0)   # 3.5 x 4 s
        with A._outcome_lock:
            A._tool_ttft[("hp", "hm")] = [500.0, 500.0, 500.0]
        assert A._tool_hedge_delay("hp", "hm") == A._TOOL_HEDGE_FLOOR    # 6 s floor
        assert A._TOOL_FLEET_CLAMP[0] <= A._tool_hedge_delay("never", "seen") <= A._TOOL_FLEET_CLAMP[1]
        assert A._tool_hedge_delay("never", "seen", 150000) == A._TOOL_HEDGE_UNKNOWN
    finally:
        with A._outcome_lock:
            A._tool_ttft.pop(("hp", "hm"), None)
            if saved is not None:
                A._tool_ttft[("hp", "hm")] = saved


def _fast_hedge(monkeypatch):
    monkeypatch.setattr(A, "_TOOL_HEDGE_FLOOR", 0.2)
    with A._outcome_lock:
        A._tool_ttft[("p1", "m1")] = [150.0, 150.0, 150.0]   # 3.5 x 0.15 = 0.525 s


def test_backup_starts_only_after_the_measured_silence(roles, monkeypatch):
    _fast_hedge(monkeypatch)
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (3.0, _ro_call(cid="slow"), 200),
        "p2": (0.0, _ro_call(cid="fast"), 200),
        "p3": (0.0, _ro_call(), 200)}, calls))
    started = time.monotonic()
    data, hdrs = A._swarm_tool_result(_ro_body())
    elapsed = time.monotonic() - started
    assert [c[0] for c in calls] == ["p1", "p2"]
    assert calls[1][2] >= 0.45, "backup started after %.2fs, before the silence" % calls[1][2]
    assert data["model"] == "p2/m2"
    assert elapsed < 2.0, "the stalled actor was waited out (%.1fs)" % elapsed
    assert "backup=1" in hdrs["X-Free-LLM-Hub-Roles"]


def test_no_backup_when_the_actor_answers_inside_the_silence(roles, monkeypatch):
    _fast_hedge(monkeypatch)
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.1, _ro_call(), 200), "p2": (0.0, _ro_call(), 200),
        "p3": (0.0, _ro_call(), 200)}, calls))
    data, _h = A._swarm_tool_result(_ro_body())
    assert [c[0] for c in calls] == ["p1"]
    assert data["model"] == "p1/m1"


def test_the_losing_actor_call_is_cut(roles, monkeypatch):
    _fast_hedge(monkeypatch)
    seen = {}

    def go(pid, payload, deadline):
        if pid == "p1":
            end = time.monotonic() + 5.0
            while time.monotonic() < end:
                if clientgone.cancelled():
                    seen["cut_at"] = time.monotonic()
                    return None, None
                time.sleep(0.02)
            return _Resp(_tool_call()), None
        return _Resp(_tool_call(cid="b")), None
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", go)
    data, _h = A._swarm_tool_result(_body())
    assert data["model"] == "p2/m2"
    deadline = time.monotonic() + 2.0
    while "cut_at" not in seen and time.monotonic() < deadline:
        time.sleep(0.02)
    assert "cut_at" in seen, "the losing actor's call was left running"


# --------------------------------------------------------------------------- #
# Verifier + corrector
# --------------------------------------------------------------------------- #

def _verify_script(verdict_text, corrector=None):
    def verifier(payload):
        return _text(verdict_text)
    return verifier


def test_verifier_runs_only_when_the_step_is_risky(roles, monkeypatch, fake_verify):
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, _tool_call(), 200), "p2": (0.0, _text(VERDICT_OK), 200),
        "p3": (0.0, _tool_call(), 200)}, calls))
    fake_verify["risky"] = False
    A._swarm_tool_result(_body())
    assert [c[0] for c in calls] == ["p1"]
    calls.clear()
    fake_verify["risky"] = True
    data, hdrs = A._swarm_tool_result(_body())
    assert [c[0] for c in calls] == ["p1", "p2"]
    assert calls[1][3] == [{"role": "user", "content": "VERIFY THIS"}]
    assert data["model"] == "p1/m1"
    assert "verifier=ok" in hdrs["X-Free-LLM-Hub-Roles"]
    # candidates handed to verify.pick_verifier are (pid, model, score)
    assert all(len(c) == 3 for c in fake_verify["picked"][-1])


def test_verifier_ok_rewards_the_actor_one(roles, monkeypatch, fake_verify, fake_bandit):
    fake_verify["risky"] = True
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, _tool_call(), 200), "p2": (0.0, _text(VERDICT_OK), 200),
        "p3": (0.0, _tool_call(), 200)}, []))
    A._swarm_tool_result(_body())
    assert ("kind-hard-tools", "p1", "m1", 1.0) in fake_bandit.rewards
    assert fake_bandit.remembered and fake_bandit.remembered[-1][1:3] == ("p1", "m1")


def test_revise_high_runs_one_corrector_whose_step_ships(roles, monkeypatch, fake_verify,
                                                         fake_bandit):
    fake_verify["risky"] = True
    calls = []

    def p2(payload):
        if payload["messages"][-1]["content"] == "VERIFY THIS":
            return _text(VERDICT_HIGH)
        return _tool_call(name="bash", args='{"command": "pytest -q"}', cid="fix")
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, _tool_call(), 200), "p2": (0.0, p2, 200),
        "p3": (0.0, _tool_call(), 200)}, calls))
    data, hdrs = A._swarm_tool_result(_body())
    assert [c[0] for c in calls] == ["p1", "p2", "p2"], "actor, verifier, ONE corrector"
    assert calls[2][3][-1]["content"] == "CORRECT IT"
    assert data["model"] == "p2/m2"
    assert data["choices"][0]["message"]["tool_calls"][0]["id"] == "fix"
    assert "corrected=1" in hdrs["X-Free-LLM-Hub-Roles"]
    assert ("kind-hard-tools", "p1", "m1", 0.5) in fake_bandit.rewards
    assert fake_bandit.remembered[-1][1:3] == ("p2", "m2")


def test_invalid_corrector_call_keeps_the_original(roles, monkeypatch, fake_verify):
    fake_verify["risky"] = True

    def p2(payload):
        if payload["messages"][-1]["content"] == "VERIFY THIS":
            return _text(VERDICT_HIGH)
        return _tool_call(name="rm_rf_everything", cid="bad")
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, _tool_call(cid="orig"), 200), "p2": (0.0, p2, 200),
        "p3": (0.0, _tool_call(), 200)}, []))
    data, hdrs = A._swarm_tool_result(_body())
    assert data["model"] == "p1/m1"
    assert data["choices"][0]["message"]["tool_calls"][0]["id"] == "orig"
    assert "corrected=0" in hdrs["X-Free-LLM-Hub-Roles"]


def test_low_severity_revise_does_not_correct(roles, monkeypatch, fake_verify):
    fake_verify["risky"] = True
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, _tool_call(), 200), "p2": (0.0, _text(VERDICT_LOW), 200),
        "p3": (0.0, _tool_call(), 200)}, calls))
    data, _h = A._swarm_tool_result(_body())
    assert len(calls) == 2 and data["model"] == "p1/m1"


@pytest.mark.parametrize("verifier_out,status", [
    ({"error": "boom"}, 500),
    (_text("this is not a verdict at all"), 200),
    (None, 200),                                   # the call raised
])
def test_verifier_failure_fails_open(roles, monkeypatch, fake_verify, fake_bandit,
                                     verifier_out, status):
    fake_verify["risky"] = True
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, _tool_call(cid="orig"), 200), "p2": (0.0, verifier_out, status),
        "p3": (0.0, _tool_call(), 200)}, []))
    data, _h = A._swarm_tool_result(_body())
    assert data["model"] == "p1/m1"
    assert data["choices"][0]["message"]["tool_calls"][0]["id"] == "orig"
    # no verdict = no quality reward either way
    assert not [r for r in fake_bandit.rewards if r[1] == "p1"]


# --------------------------------------------------------------------------- #
# Bandit credit on the next request
# --------------------------------------------------------------------------- #

def test_next_request_credits_the_served_call(roles, monkeypatch, fake_bandit):
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, _tool_call(name="bash", args='{"command": "python -m pytest -q"}',
                               cid="call_run"), 200),
        "p2": (0.0, _tool_call(), 200), "p3": (0.0, _tool_call(), 200)}, []))
    A._swarm_tool_result(_body())
    assert fake_bandit.remembered[-1][0] == ["call_run"]
    monkeypatch.setattr(A, "_chat_completions", lambda body: ({"ok": True}, 200))
    nxt = _body()["messages"] + [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_run", "type": "function", "function": {
                "name": "bash", "arguments": '{"command": "python -m pytest -q"}'}}]},
        {"role": "tool", "tool_call_id": "call_run",
         "content": "...\n===== 1 failed, 3 passed in 0.21s =====\nExit code: 1"}]
    client = A.app.test_client()
    r = client.post("/v1/chat/completions", json={"model": "auto", "messages": nxt},
                    headers={"Authorization": "Bearer x"})
    assert r.status_code in (200, 401, 403)
    if r.status_code != 200:
        A._bandit_credit(nxt)           # auth-gated install: same entry point
    assert fake_bandit.credit_calls, "the /v1 request never credited"
    assert ("kind-hard-tools", "p1", "m1", 0.5) in fake_bandit.rewards   # observed FAIL


def test_grade_reads_observed_results_then_error_markers():
    msgs = [{"role": "assistant", "content": "", "tool_calls": [
        {"id": "a", "type": "function", "function": {
            "name": "bash", "arguments": '{"command": "python -m pytest -q"}'}},
        {"id": "b", "type": "function", "function": {
            "name": "read", "arguments": '{"path": "x.py"}'}}]}]
    grade = A._make_bandit_grade(msgs)
    passed = {"role": "tool", "tool_call_id": "a",
              "content": "===== 4 passed in 0.10s =====\nExit code: 0"}
    failed = {"role": "tool", "tool_call_id": "a",
              "content": "===== 1 failed in 0.10s =====\nExit code: 1"}
    errored = {"role": "tool", "tool_call_id": "b",
               "content": "Error: no such file or directory"}
    clean = {"role": "tool", "tool_call_id": "b", "content": "def main():\n    pass"}
    assert grade(passed) == 1.0
    assert grade(failed) == 0.5
    assert grade(errored) == 0.5
    assert grade(clean) == 1.0


def test_observed_pass_follows_the_last_check():
    msgs = [{"role": "assistant", "content": "", "tool_calls": [
        {"id": "a", "type": "function", "function": {
            "name": "bash", "arguments": '{"command": "python -m pytest -q"}'}}]},
        {"role": "tool", "tool_call_id": "a",
         "content": "===== 4 passed in 0.10s =====\nExit code: 0"}]
    assert A._observed_pass(msgs) is True
    assert A._observed_pass([{"role": "user", "content": "hi"}]) is None


# --------------------------------------------------------------------------- #
# The flag, the activity row, the log, the client leaving
# --------------------------------------------------------------------------- #

def test_the_flag_reads_tool_turn_race_default_off(monkeypatch):
    real = config.get_flag
    assert A._tool_turn_race_on() is False                 # default: roles
    monkeypatch.setattr(config, "get_flag",
                        lambda name, default=False: True if name == "tool_turn_race"
                        else real(name, default))
    assert A._tool_turn_race_on() is True


def test_flag_tool_turn_race_restores_the_race(roles, monkeypatch):
    monkeypatch.setattr(A, "_tool_turn_race_on", lambda: True)
    monkeypatch.setattr(A, "_swarm_rank", lambda cands, difficulty=None: list(cands))
    monkeypatch.setattr(A, "_normalize_model_identity", lambda m: m)
    roles_called = []
    monkeypatch.setattr(A, "_tool_turn_roles", lambda body: roles_called.append(1))
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, _tool_call(), 200), "p2": (0.0, _tool_call(), 200),
        "p3": (0.0, _tool_call(), 200)}, calls))
    data, _h = A._swarm_tool_result(_body())
    assert not roles_called
    assert sorted(c[0] for c in calls) == ["p1", "p2", "p3"], "the race fans out"
    assert data["model"].startswith("p")


def test_activity_row_names_roles_not_race_labels(roles, monkeypatch, fake_verify):
    fake_verify["risky"] = True
    _fast_hedge(monkeypatch)

    def p3(payload):        # p1 429s, p2 acts, p3 verifies and corrects
        if payload["messages"][-1]["content"] == "VERIFY THIS":
            return _text(VERDICT_HIGH)
        return _tool_call(cid="fix")
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, {"e": 1}, 429), "p2": (0.0, _tool_call(), 200),
        "p3": (0.0, p3, 200)}, []))
    with A.app.test_request_context("/v1/chat/completions", method="POST"):
        A.g.act = {}
        A._swarm_tool_result(_body())
        act = A.g.act
    roles_seen = [r["role"] for r in act["pipeline"]]
    assert act["crew"] == "swarm (roles)"
    assert roles_seen[0].startswith("actor: HTTP 429")
    assert "actor" in roles_seen
    assert "verifier: revise" in roles_seen
    assert "corrector" in roles_seen
    assert not any(r in ("winner", "used a tool", "answered") for r in roles_seen)


def test_every_role_turn_is_logged(roles, monkeypatch):
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, _ro_call(), 200), "p2": (0.0, _ro_call(), 200),
        "p3": (0.0, _ro_call(), 200)}, []))
    path = os.path.join(config.state_dir(), A._ROLE_LOG_NAME)
    assert "free-llm-hub" not in path or "pytest" in path.lower() or "tmp" in path.lower() \
        or "sandbox" in path.lower(), "the test must not write the real state dir: " + path
    A._swarm_tool_result(_ro_body())
    with open(path, encoding="utf-8") as f:
        row = json.loads(f.read().strip().splitlines()[-1])
    for key in ("kind", "actor", "nudge", "hedge", "verifier", "verdict", "corrected",
                "input_tokens", "latency_s", "calls", "served"):
        assert key in row
    assert row["served"] == "p1/m1" and row["calls"] == 1


def test_client_gone_still_cancels(roles, monkeypatch, fake_bandit):
    calls, cut = [], {}

    def go(pid, payload, deadline):
        calls.append(pid)
        end = time.monotonic() + 5.0
        while time.monotonic() < end:
            if clientgone.cancelled():
                cut[pid] = True
                return None, None
            time.sleep(0.02)
        return _Resp(_tool_call()), None
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", go)
    tok = clientgone.Token(label="test", log=False)
    clientgone.set_current(tok)
    try:
        threading.Timer(0.3, lambda: tok.cancel("client left")).start()
        started = time.monotonic()
        out = A._swarm_tool_result(_body())
        assert out is None
        assert time.monotonic() - started < 2.0
    finally:
        clientgone.set_current(None)
    deadline = time.monotonic() + 2.0
    while not cut and time.monotonic() < deadline:
        time.sleep(0.02)
    assert calls == ["p1"] and cut.get("p1"), "the actor's call was not cut"
    assert not fake_bandit.rewards


# --------------------------------------------------------------------------- #
# Owner rule: benchmarks lead, the nudge only breaks ties inside the band
# --------------------------------------------------------------------------- #

def test_a_134_model_with_max_nudge_never_outranks_an_available_138(fake_bandit, monkeypatch):
    monkeypatch.setattr(A, "_agentic_score", lambda c, sustain_override=None: c[0])
    fake_bandit.nudges[("pb", "b-134")] = +50.0           # clamped to 1.0 anyway
    band = A._auto_top_band([(138.0, "pa", "a-138"), (134.0, "pb", "b-134")],
                            kind="k")
    assert [c[2] for c in band] == ["a-138"]
    ordered = A._nudge_in_band([("pa", "a-138"), ("pb", "b-134")], "k",
                               lambda e: {"a-138": 138.0, "b-134": 134.0}[e[1]])
    assert ordered[0] == ("pa", "a-138")


@pytest.mark.real_bandit
def test_138_and_137_7_can_swap_by_nudge(fake_bandit):
    score = {"a": 138.0, "b": 137.7}.get
    pairs = [("pa", "a"), ("pb", "b")]
    assert A._nudge_in_band(pairs, "k", lambda e: score(e[1])) == pairs
    fake_bandit.nudges[("pb", "b")] = +1.0
    assert A._nudge_in_band(pairs, "k", lambda e: score(e[1])) == pairs[::-1]
    # the nudge is capped at 1.0: a +6 on a model 1.5 under still swaps only
    # inside the band, and a model past the band never moves
    fake_bandit.nudges[("pb", "b")] = +6.0
    three = pairs + [("pc", "c")]
    s3 = {"a": 138.0, "b": 137.7, "c": 135.0}.get
    fake_bandit.nudges[("pc", "c")] = +6.0
    out = A._nudge_in_band(three, "k", lambda e: s3(e[1]))
    assert out[2] == ("pc", "c")


@pytest.mark.real_bandit
def test_swarm_rank_breaks_ties_only_inside_the_band(fake_bandit, monkeypatch):
    sc = {"a": 138.0, "b": 137.7, "c": 134.0}
    monkeypatch.setattr(A, "_benchmark_score", lambda p, m: sc[m])
    monkeypatch.setattr(A, "_agentic_score", lambda c, sustain_override=None: c[0])
    monkeypatch.setattr(A, "_swarm_reliability", lambda p, m: 0.9)
    monkeypatch.setattr(A, "_swarm_has_record", lambda p, m: True)
    monkeypatch.setattr(A, "_quota_headroom", lambda p: 1.0)
    monkeypatch.setattr(A, "_swarm_fanout", lambda cands, d=None: 3)
    monkeypatch.setattr(A, "_normalize_model_identity", lambda m: m)
    cands = [("pa", "a"), ("pb", "b"), ("pc", "c")]
    fake_bandit.nudges[("pc", "c")] = +1.0
    fake_bandit.nudges[("pb", "b")] = +1.0
    got = A._swarm_rank(list(cands), "hard")
    assert got[0] == ("pb", "b") and got[2] == ("pc", "c")


def test_negative_nudge_never_drops_a_band_member_under_an_outsider(fake_bandit):
    s = {"a": 138.0, "b": 136.0, "c": 135.9}.get
    fake_bandit.nudges[("pb", "b")] = -1.0
    scores, band = A._band_scores([("pa", "a"), ("pb", "b"), ("pc", "c")], "k",
                                  lambda e: s(e[1]))
    assert band == [0, 1]
    assert scores[1] >= 136.0 > scores[2]


def test_low_quality_family_is_never_nudged(fake_bandit):
    fake_bandit.nudges[("pz", "nemotron-ultra")] = +1.0
    assert A._bandit_delta("k", "pz", "nemotron-ultra", 138.0) == 0.0


def test_no_bandit_module_means_no_nudge(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "bandit", types.ModuleType("bandit"))  # not ours
    assert A._bandit() is None
    assert A._task_kind("hard", True, 10) is None
    pairs = [("pa", "a"), ("pb", "b")]
    assert A._nudge_in_band(pairs, None, lambda e: 1.0) == pairs


# --------------------------------------------------------------------------- #
# Max tier text turns
# --------------------------------------------------------------------------- #

def test_max_hard_text_answer_is_verified_and_corrected(monkeypatch, fake_verify, fake_bandit):
    monkeypatch.setattr(A, "_swarm_member_sick", lambda *a, **k: None)
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, model: SCORES.get(model, 100.0))
    monkeypatch.setattr(A, "_record_chat_usage", lambda *a, **k: None)
    monkeypatch.setattr(A, "_record_outcome", lambda *a, **k: None)
    calls = []

    def p2(payload):
        if payload["messages"][-1]["content"] == "VERIFY THIS":
            return _text(VERDICT_HIGH)
        return _text("The corrected, complete answer with the proof written out in full.")
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p2": (0.0, p2, 200), "p3": (0.0, _text("x"), 200)}, calls))
    msgs = [{"role": "user", "content": "prove the theorem rigorously"}]
    orig = _text("A first answer that the verifier will not accept as it stands.")
    out = A._max_text_review("max", "hard", False, msgs, "p1", "m1", orig, CHAIN, 100)
    assert out is not None and out[:2] == ("p2", "m2")
    assert "corrected" in out[2]["choices"][0]["message"]["content"]
    assert [c[0] for c in calls] == ["p2", "p2"]
    assert ("kind-hard-text", "p1", "m1", 0.5) in fake_bandit.rewards


@pytest.mark.parametrize("tier,diff,tools", [("auto", "hard", False),
                                             ("max", "medium", False),
                                             ("max", "hard", True)])
def test_text_review_only_on_max_hard_tool_free(monkeypatch, fake_verify, tier, diff, tools):
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({}, calls))
    out = A._max_text_review(tier, diff, tools, [{"role": "user", "content": "q"}],
                             "p1", "m1", _text("answer"), CHAIN, 10)
    assert out is None and calls == []


def test_streamed_turn_is_still_buffered_and_replayed(roles, monkeypatch):
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, _tool_call(), 200), "p2": (0.0, _tool_call(), 200),
        "p3": (0.0, _tool_call(), 200)}, []))
    with A.app.test_request_context("/v1/chat/completions", method="POST"):
        resp = A._swarm_tool_turn(_body(stream=True))
        text = "".join(c.decode() if isinstance(c, bytes) else c for c in resp.response)
    assert '"tool_calls"' in text and text.rstrip().endswith("data: [DONE]")


def test_an_unparsed_verdict_is_fail_open_and_not_a_pass(roles, monkeypatch, fake_verify,
                                                         fake_bandit):
    """verify.parse_verdict turns an unreadable reply into ACCEPT + unparsed."""
    fake_verify["risky"] = True
    monkeypatch.setitem(__import__("sys").modules["verify"].__dict__, "parse_verdict",
                        lambda text: {"ok": True, "problems": [], "severity": "low",
                                      "unparsed": True})
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, _tool_call(cid="orig"), 200), "p2": (0.0, _text("hmm"), 200),
        "p3": (0.0, _tool_call(), 200)}, []))
    data, hdrs = A._swarm_tool_result(_body())
    assert data["model"] == "p1/m1"
    assert "verifier=no" in hdrs["X-Free-LLM-Hub-Roles"]
    assert not [r for r in fake_bandit.rewards if r[1] == "p1"]


# --------------------------------------------------------------------------- #
# Pipeline helpers: avoid_families, _verify_family, _free_verdict
# --------------------------------------------------------------------------- #

FAMILY_CHAIN = [("p1", "g-1"), ("p2", "c-2"), ("p3", "g-3")]   # fake family = 1st char


def _stage_setup(monkeypatch, calls):
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("p1", "g-1", "hard"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: list(FAMILY_CHAIN))
    monkeypatch.setattr(A, "_record_outcome", lambda *a, **k: None)
    monkeypatch.setattr(A, "_record_chat_usage", lambda *a, **k: None)
    monkeypatch.setattr(A, "_swarm_member_sick", lambda *a, **k: None)
    monkeypatch.setattr(A, "_benchmark_score", lambda p, m: 130.0)

    def go(pid, payload, deadline):
        calls.append((pid, payload.get("messages")))
        return _Resp(_text("stage output written by %s, complete and on topic." % pid)), None
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", go)


def test_swarm_dispatch_puts_avoided_families_last(monkeypatch, fake_verify):
    calls = []
    _stage_setup(monkeypatch, calls)
    msgs = [{"role": "user", "content": "review the draft"}]
    with A.app.test_request_context("/v1/chat/completions", method="POST"):
        text, who = A._swarm_dispatch(msgs, 200, avoid_families=("g",))
    assert who == "p2/c-2" and calls[0][0] == "p2"
    # never excluded: avoided families are still the chain's tail
    assert A._avoid_families_last(FAMILY_CHAIN, ("g",)) == [
        ("p2", "c-2"), ("p1", "g-1"), ("p3", "g-3")]
    assert A._avoid_families_last(FAMILY_CHAIN, ("c", "g")) == FAMILY_CHAIN
    calls.clear()
    with A.app.test_request_context("/v1/chat/completions", method="POST"):
        _t, who = A._swarm_dispatch(msgs, 200)
    assert who == "p1/g-1"                               # no kwarg: unchanged
    calls.clear()
    with A.app.test_request_context("/v1/chat/completions", method="POST"):
        _t, who = A._swarm_fast_dispatch(msgs, 200, avoid_families=("g",))
    assert who == "p2/c-2"


def test_avoid_families_is_a_no_op_without_verify(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "verify", types.ModuleType("verify"))   # not ours
    assert A._verify_family() is None
    assert A._avoid_families_last(FAMILY_CHAIN, ("g",)) == FAMILY_CHAIN


def test_verify_family_exposes_verify_family(fake_verify):
    fam = A._verify_family()
    assert callable(fam) and fam("g-1") == "g"


def test_free_verdict_reads_a_dict_brief_and_picks_another_family(monkeypatch, fake_verify):
    calls = []
    _stage_setup(monkeypatch, calls)
    seen = {}

    def go(pid, payload, deadline):
        seen["pid"], seen["payload"] = pid, payload
        return _Resp(_text(VERDICT_HIGH)), None
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", go)
    sysmod = __import__("sys").modules["verify"]
    sysmod.VERIFIER_SYSTEM = "You are an independent reviewer."
    # pick a different FAMILY than the producer, as verify.pick_verifier does
    sysmod.pick_verifier = lambda producer, cands: next(
        ((c[0], c[1]) for c in cands if c[1][:1] != producer[1][:1]), None)
    out = A._free_verdict({"text": "PHASE 2 BRIEF: task + output", "producer": "p1/g-1"})
    assert out == {"ok": False, "problems": ["deletes the repo"], "severity": "high"}
    assert seen["pid"] == "p2"
    msgs = seen["payload"]["messages"]
    assert msgs[0]["role"] == "system" and msgs[-1]["content"] == "PHASE 2 BRIEF: task + output"
    assert seen["payload"]["max_tokens"] == 300


def test_role_eval_compares_race_log_and_roles_jsonl(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "role_eval", os.path.join(os.path.dirname(A.__file__), "scripts", "role_eval.py"))
    ev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ev)
    log = tmp_path / "hub.log"
    log.write_text(
        "2026-10-01 10:00:00,000 INFO [swarm-tools] 2/4 models answered, 1 used a tool -> g4f/x\n"
        "2026-10-01 10:01:00,000 WARNING [swarm-tools] 0/4 models answered in 30s -> a: b\n"
        "2026-10-01 10:02:00,000 INFO unrelated line\n", encoding="utf-8")
    roles = tmp_path / "turn-roles.jsonl"
    rows = [
        {"event": "turn", "turn": "tool", "calls": 1, "sent_tokens": 1000, "latency_s": 4.0,
         "served": "p/m", "invalid": 0},
        {"event": "turn", "turn": "tool", "calls": 3, "sent_tokens": 2500, "latency_s": 20.0,
         "served": "p/m", "invalid": 1, "hedge": "q/n", "verifier": "v/w",
         "verdict": "revise", "corrector": "c/d", "corrected": True},
        {"event": "turn", "turn": "tool", "calls": 2, "sent_tokens": 2000, "latency_s": 9.0,
         "served": None, "invalid": 0},
        {"event": "credit", "credited": 2},
    ]
    roles.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    out = ev.main(["--log", str(log), "--roles", str(roles), "--json"])
    assert out["race"]["turns"] == 2 and out["race"]["member_calls"] == 8
    assert out["race"]["calls_per_served_answer"] == 8.0
    assert out["race"]["share_that_served_nothing"] == 0.875
    r = out["roles"]
    assert r["turns"] == 3 and r["calls_per_turn"] == 2.0
    assert r["calls_per_served_answer"] == 3.0
    assert r["latency_p50_s"] == 9.0 and r["latency_p90_s"] == 20.0
    assert r["zero_answer_rate"] == round(1 / 3, 3)
    assert r["invalid_tool_calls"] == 1 and r["backup_fired"] == 1
    assert r["verifier_fixes"] == 1 and r["next_turn_credits"] == 2


@pytest.mark.parametrize("reply,status", [(_text("not json"), 200), ({"e": 1}, 503)])
def test_free_verdict_fails_open_to_none(monkeypatch, fake_verify, reply, status):
    _stage_setup(monkeypatch, [])
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline",
                        lambda pid, payload, deadline: (_Resp(reply, status), None))
    assert A._free_verdict({"text": "brief"}) is None
    assert A._free_verdict({"text": ""}) is None
    assert A._free_verdict(None) is None
