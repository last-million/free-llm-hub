"""Tool fan-out: who takes a slot, and how fast the race settles.

MEASURED 2026-09-27 on the owner's OpenCode /agent session (quality swarm, tool
turns, e021e26): every turn ended 200 on an nvidia member's tool call and took
~137-145 s. Four of the five members per turn came from providers already known
to be failing -- g4f glm-5.3 HTTP 524, g4f kimi-k3 524/504, dahl
DeepSeek-V4-Flash 429, a gemini-3.8-flash listing 400 -- and the race kept
waiting for them under a 150 s grace after the winning tool call was in hand.

Fake providers only; nothing here reaches the network.
"""
import time

import pytest
import requests

import app as A


TOOLS = [{"type": "function", "function": {"name": "write_file"}}]
BODY = {"model": "swarm", "tools": TOOLS,
        "messages": [{"role": "user", "content": "build it"}]}


class _Resp:
    def __init__(self, payload=None, status=200):
        self.status_code = status
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload

    def close(self):
        pass


def _tool_call(name="write_file", args="{}"):
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": name, "arguments": args}}]}}]}


def _ident(m):
    return m.split(":")[-1].split("/")[-1].lower()


@pytest.fixture
def isolated(monkeypatch):
    """No persistent quota writes from fake pids, and a stable identity."""
    throttled = []
    monkeypatch.setattr(A, "_throttle_failed_hop",
                        lambda pid, model, exc=None, secs=None: throttled.append((pid, model)))
    monkeypatch.setattr(A, "_normalize_model_identity", _ident)
    # Quota state is process-wide and persisted; another test's throttle on a
    # pid must not decide this file's selections. Tests override as needed.
    monkeypatch.setattr(A.quota, "is_exhausted", lambda pid: False)
    monkeypatch.setattr(A.quota, "is_model_exhausted", lambda pid, model: False)
    yield throttled


# --------------------------------------------------------------------------- #
# 1. Candidate selection
# --------------------------------------------------------------------------- #

# The owner's chain shape: a failing relay listing of a model BEFORE the
# healthy listing of the same model on another provider.
CHAIN = [("g4f", "srv_a:z-ai/glm-5.3"),
         ("nv", "z-ai/glm-5.3"),
         ("dh", "deepseek-ai/DeepSeek-V4-Flash-0731"),
         ("nv", "deepseek-ai/deepseek-v4.1-flash"),
         ("g4f", "srv_b:models/gemini-3.8-flash"),
         ("nv", "moonshotai/kimi-k3")]


def _pids(cands):
    return [p for p, _m in cands]


def test_throttled_provider_takes_no_slot_when_healthy_ones_exist(isolated, monkeypatch):
    real = A.quota.is_exhausted
    monkeypatch.setattr(A.quota, "is_exhausted",
                        lambda pid: pid in ("dh", "g4f") or real(pid))
    cands, skipped = A._swarm_tool_candidates(CHAIN)
    assert "dh" not in _pids(cands) and "g4f" not in _pids(cands)
    assert {(p, m) for p, m, _w in skipped} >= {
        ("dh", "deepseek-ai/DeepSeek-V4-Flash-0731"), ("g4f", "srv_a:z-ai/glm-5.3")}


def test_the_healthy_listing_of_a_model_replaces_the_sick_one(isolated, monkeypatch):
    """The identity de-duplication kept the FIRST listing: a 524'ing g4f
    glm-5.3 took the slot and nvidia's glm-5.3 was dropped as a duplicate."""
    A._note_swarm_member_fail("g4f", "srv_a:z-ai/glm-5.3", "HTTP 524")
    cands, _skipped = A._swarm_tool_candidates(CHAIN)
    assert ("nv", "z-ai/glm-5.3") in cands
    assert ("g4f", "srv_a:z-ai/glm-5.3") not in cands


@pytest.mark.parametrize("how", ["parked", "model_throttled", "not_offered", "dead",
                                 "benched", "recent_429", "recent_stall",
                                 "relay_struck", "fanout_400"])
def test_every_known_sickness_excludes_the_pair(isolated, monkeypatch, how):
    pid, model = "g4f", "srv_b:models/gemini-3.8-flash"
    if how == "parked":
        monkeypatch.setitem(A._dead_providers, pid, time.time() + 60)
    elif how == "model_throttled":
        real = A.quota.is_model_exhausted
        monkeypatch.setattr(A.quota, "is_model_exhausted",
                            lambda p, m: (p, m) == (pid, model) or real(p, m))
    elif how == "not_offered":
        monkeypatch.setitem(A._not_offered, (pid, model), time.time() + 60)
    elif how == "dead":
        monkeypatch.setitem(A._dead_models, (pid, model), time.time() + 60)
    elif how == "benched":
        monkeypatch.setitem(A._junk_bench, (pid, model),
                            {"until": time.time() + 60, "count": 3})
    elif how == "recent_429":
        A._note_recent_hop_failure(pid, model, "429")
    elif how == "recent_stall":
        A._note_recent_hop_failure(pid, model, "deadline")
    elif how == "relay_struck":
        A._note_relay_tool_fail(pid, model)
        A._note_relay_tool_fail(pid, model)
    elif how == "fanout_400":
        A._note_swarm_member_fail(pid, model, "HTTP 400")
    assert A._swarm_member_sick(pid, model), how
    cands, skipped = A._swarm_tool_candidates(CHAIN)
    assert (pid, model) not in cands
    assert any((p, m) == (pid, model) for p, m, _w in skipped)


def test_fewer_than_two_healthy_keeps_the_old_selection(isolated, monkeypatch):
    """A thin swarm of doubtful members beats no swarm: below
    _SWARM_FANOUT_MIN healthy candidates the old selection runs unchanged."""
    chain = [("sub-claude", "sonnet")] + CHAIN
    real = A.quota.is_exhausted
    monkeypatch.setattr(A.quota, "is_exhausted",
                        lambda pid: pid != "dh" or real(pid))   # only dh healthy
    cands, skipped = A._swarm_tool_candidates(chain)
    old, seen = [], set()
    for p, m in chain:                       # the pre-change loop, verbatim
        if A._is_sub(p):
            continue
        if _ident(m) in seen:
            continue
        seen.add(_ident(m))
        old.append((p, m))
    assert cands == old[:A._SWARM_TOOL_CANDIDATES]
    assert skipped == []


# --------------------------------------------------------------------------- #
# 2. Settling the race
# --------------------------------------------------------------------------- #

@pytest.fixture
def race(isolated, monkeypatch):
    """Pin route + chain; ranking passes the candidates through untouched."""
    state = {"chain": []}
    recorded = []
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("p1", "a", "hard"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: list(state["chain"]))
    monkeypatch.setattr(A, "_swarm_rank", lambda cands, difficulty=None: list(cands))
    monkeypatch.setattr(A, "_record_outcome",
                        lambda p, m, ok, junk=False: recorded.append((p, m, ok)))
    monkeypatch.setattr(A, "_record_chat_usage", lambda *a, **k: None)
    monkeypatch.setattr(A, "_note_nonanswer", lambda *a, **k: None)
    monkeypatch.setattr(A, "_est_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(A, "_SWARM_TOOL_HOP_DEADLINE", 30)
    monkeypatch.setattr(A, "_SWARM_STRAGGLER_GRACE", 30)     # the old long grace
    monkeypatch.setattr(A, "_SWARM_TOOL_SETTLE_MIN", 0.5)
    monkeypatch.setattr(A, "_SWARM_TOOL_SETTLE_MAX", 1.0)
    state["recorded"] = recorded
    state["throttled"] = isolated
    yield state


def _dispatcher(plan, seen=None):
    """plan: {(pid, model): (delay, payload | status int | exception | None)}."""
    def go(pid, payload, deadline):
        delay, out = plan[(pid, payload["model"])]
        if seen is not None:
            seen.append((pid, payload["model"]))
        time.sleep(delay)
        if isinstance(out, BaseException):
            return None, out
        if isinstance(out, int):
            return _Resp(status=out), None
        if out is None:
            return None, None
        return _Resp(out), None
    return go


def test_a_valid_tool_call_ends_the_race_within_the_short_grace(race, monkeypatch):
    race["chain"] = [("p1", "a"), ("p2", "b")]
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        ("p1", "a"): (0.1, _tool_call()),
        ("p2", "b"): (10.0, _tool_call()),          # healthy, just slow
    }))
    t0 = time.monotonic()
    data, _hdrs = A._swarm_tool_result(dict(BODY))
    elapsed = time.monotonic() - t0
    assert elapsed < 3, "waited %.1fs past a valid tool call" % elapsed
    assert data["model"] == "p1/a"
    # The straggler was abandoned by us, not failed by the provider.
    assert not [r for r in race["recorded"] if r[:2] == ("p2", "b") and r[2] is False]


def test_the_short_grace_follows_the_winners_latency():
    assert A._swarm_tool_grace(4) == A._SWARM_TOOL_SETTLE_MIN          # fast: 15 s
    assert A._swarm_tool_grace(40) == 20                               # 0.5 x 40
    assert A._swarm_tool_grace(300) == A._SWARM_TOOL_SETTLE_MAX        # slow: 25 s
    assert A._swarm_tool_grace(300) <= A._SWARM_STRAGGLER_GRACE
    assert 15 <= A._SWARM_TOOL_SETTLE_MIN <= A._SWARM_TOOL_SETTLE_MAX <= 25


def test_an_invalid_tool_call_does_not_cut_the_race_short(race, monkeypatch):
    """An unknown tool name is not something the CLI can run: it keeps the
    long grace, and a valid call landing later wins over it."""
    race["chain"] = [("p1", "a"), ("p2", "b")]
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        ("p1", "a"): (0.1, _tool_call(name="not_offered")),
        ("p2", "b"): (2.0, _tool_call()),           # past the 0.5-1 s short grace
    }))
    data, _hdrs = A._swarm_tool_result(dict(BODY))
    assert data["model"] == "p2/b"


def test_valid_tool_call_check():
    ok = {"tool_calls": [{"function": {"name": "write_file", "arguments": '{"a": 1}'}}]}
    assert A._swarm_tool_calls_valid(ok, TOOLS)
    assert A._swarm_tool_calls_valid(
        {"tool_calls": [{"function": {"name": "write_file", "arguments": ""}}]}, TOOLS)
    for bad in ({"tool_calls": [{"function": {"name": "rm_rf", "arguments": "{}"}}]},
                {"tool_calls": [{"function": {"name": "", "arguments": "{}"}}]},
                {"tool_calls": [{"function": {"name": "write_file", "arguments": "{oops"}}]},
                {"tool_calls": [{"function": {"name": "write_file", "arguments": "[1]"}}]},
                {"content": "prose"}):
        assert not A._swarm_tool_calls_valid(bad, TOOLS), bad


@pytest.mark.parametrize("status", [429, 524, 504, 503])
def test_members_of_a_provider_that_failed_this_race_are_not_waited_on(race, monkeypatch,
                                                                      status):
    """The owner's two g4f members: one 524'd, the other would have too."""
    race["chain"] = [("p1", "a"), ("p2", "b"), ("p2", "c")]
    monkeypatch.setattr(A, "_SWARM_TOOL_SETTLE_MIN", 8)      # only the new rule
    monkeypatch.setattr(A, "_SWARM_TOOL_SETTLE_MAX", 8)      # can end it early
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        ("p1", "a"): (0.2, _tool_call()),
        ("p2", "b"): (0.05, status),
        ("p2", "c"): (10.0, None),
    }))
    t0 = time.monotonic()
    data, _hdrs = A._swarm_tool_result(dict(BODY))
    assert time.monotonic() - t0 < 3
    assert data["model"] == "p1/a"
    # ...and the failure is filed the way the chain loops file it, so the
    # NEXT turn's fan-out leaves the pair out.
    assert A._swarm_member_sick("p2", "b")
    if status == 429:
        assert A._recent_hop_failure("p2", "b") == "429"
    else:
        assert ("p2", "b") in race["throttled"]


def test_with_no_answer_yet_a_failed_providers_member_is_still_waited_on(race, monkeypatch):
    """Falling back while it still runs would re-dispatch to the same
    providers; only an answer in hand ends the wait early."""
    race["chain"] = [("p1", "a"), ("p2", "b"), ("p2", "c")]
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        ("p1", "a"): (0.0, requests.ConnectionError("refused")),
        ("p2", "b"): (0.05, 429),
        ("p2", "c"): (0.8, _tool_call()),
    }))
    data, _hdrs = A._swarm_tool_result(dict(BODY))
    assert data["model"] == "p2/c"


def test_a_sick_pair_is_never_dispatched_end_to_end(race, monkeypatch):
    """The whole path: a pair that 429'd in the last race is not called
    again while two healthy candidates exist."""
    race["chain"] = [("dh", "ds"), ("p1", "a"), ("p2", "b")]
    A._note_recent_hop_failure("dh", "ds", "429")
    seen = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        ("dh", "ds"): (0.0, 429),
        ("p1", "a"): (0.05, _tool_call()),
        ("p2", "b"): (0.1, _tool_call()),
    }, seen))
    data, _hdrs = A._swarm_tool_result(dict(BODY))
    assert ("dh", "ds") not in seen
    assert data["model"] in ("p1/a", "p2/b")
