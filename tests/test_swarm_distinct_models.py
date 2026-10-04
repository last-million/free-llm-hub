"""Swarm / crew workers of ONE run open on different models, and a code/format
bound phase gets one free cross-family verdict.

MEASURED 2026-10-04 (turn-roles.jsonl + app.py audit): prose swarm/crews
workers run in parallel (swarm._waves) but all went through _swarm_dispatch
with force_difficulty "hard" and no per-run rotation, so they clustered on the
top one or two models. Fixes under test:

  1. swarm.RunLedger + app._distinct_first: a worker prefers a model identity
     (then family, then provider) no other worker of the run holds, inside the
     top band (_AUTO_TOP_BAND, widened to 4 points only when needed); never
     excludes anything, never promotes a last-resort family, flag
     `swarm_distinct_models`.
  2. swarm.run(free_verdict=): one free verdict per code/format-bound phase
     whose free checks pass; not-ok + HIGH -> the existing single retry;
     default None unchanged; flag `swarm_phase_verdict`.

No network: every model call is a stub.
"""
import json
import re
import threading

import pytest

import app as A
import config
import crews
import swarm


# --------------------------------------------------------------------------- #
# 1. distinct identities
# --------------------------------------------------------------------------- #

SCORES = {"m1": 138.0, "m2": 137.5, "m3": 137.0, "m4": 136.5, "m5": 136.0,
          "m6": 135.5, "far": 120.0, "mid": 133.8}


@pytest.fixture
def scored(monkeypatch):
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, model: SCORES.get(model, 100.0))
    monkeypatch.setattr(A, "_is_low_quality", lambda m: m.startswith("weak"))
    monkeypatch.setattr(A, "_verify_family", lambda: None)   # family = identity


def _chain(*names):
    return [("p%d" % (i + 1), n) for i, n in enumerate(names)]


def test_six_workers_get_six_distinct_identities(scored):
    chain = _chain("m1", "m2", "m3", "m4", "m5", "m6")
    ledger = swarm.RunLedger()
    got = []
    for _ in range(6):
        c, reserved = A._distinct_first(chain, ledger)
        got.append(c[0])
        assert reserved == c[0]
    assert len({m for _p, m in got}) == 6
    assert got[0] == ("p1", "m1"), "the best model is still first when it is free"
    assert len(ledger.used()) == 6


def test_the_same_identity_on_two_providers_counts_once(scored):
    chain = [("a", "m1"), ("b", "m1"), ("c", "m2")]
    ledger = swarm.RunLedger()
    first, _ = A._distinct_first(chain, ledger)
    second, _ = A._distinct_first(chain, ledger)
    assert first[0] == ("a", "m1")
    assert second[0] == ("c", "m2"), "m1 on provider b is the same weights"


def test_the_band_rule_only_best_models_are_promoted(scored):
    chain = _chain("m1", "m2", "far")
    ledger = swarm.RunLedger()
    picks = [A._distinct_first(chain, ledger)[0][0] for _ in range(3)]
    assert [p[1] for p in picks[:2]] == ["m1", "m2"]
    # the third worker: nothing unused within 4 points -> the chain as built
    assert picks[2] == ("p1", "m1"), "a model 18 points back is never promoted"


def test_the_band_widens_to_four_points_only_when_needed(scored):
    chain = _chain("m1", "m2", "mid")        # mid is 4.2 behind the best? no: 138-133.8
    SCORES["m2"] = 137.9
    try:
        ledger = swarm.RunLedger()
        got = [A._distinct_first(chain, ledger)[0][0][1] for _ in range(2)]
        assert got == ["m1", "m2"]
        third = A._distinct_first(chain, ledger)[0][0][1]
        assert third == "m1", "mid is 4.2 points behind: outside even the widened band"
        SCORES["mid"] = 134.5                 # 3.5 behind: inside 4.0, outside 2.0
        ledger2 = swarm.RunLedger()
        got2 = [A._distinct_first(chain, ledger2)[0][0][1] for _ in range(3)]
        assert got2 == ["m1", "m2", "mid"]
    finally:
        SCORES["m2"], SCORES["mid"] = 137.5, 133.8


def test_last_resort_families_and_excluded_providers_are_not_promoted(scored):
    chain = [("p1", "m1"), ("p2", "weak-1"), ("p3", "m2")]
    SCORES["weak-1"] = 137.9
    try:
        ledger = swarm.RunLedger()
        A._distinct_first(chain, ledger)
        second, _ = A._distinct_first(chain, ledger)
        assert second[0] == ("p3", "m2")
        # a retry that excludes p3 (it failed the phase) must not open on p3
        third, _ = A._distinct_first(chain, swarm.RunLedger(), exclude_pids=("p1",))
        assert third[0][0] != "p1" and third[0] == ("p3", "m2")
    finally:
        SCORES.pop("weak-1", None)


def test_an_unused_family_beats_an_unused_provider(scored, monkeypatch):
    fam = {"m1": "k", "m2": "k", "m3": "q"}
    monkeypatch.setattr(A, "_verify_family", lambda: (lambda m: fam.get(m, m)))
    chain = [("a", "m1"), ("a", "m2"), ("b", "m3")]
    ledger = swarm.RunLedger()
    A._distinct_first(chain, ledger)
    second, _ = A._distinct_first(chain, ledger)
    assert second[0] == ("b", "m3"), "a new family comes before a new identity of the same family"


def test_nothing_is_dropped_only_reordered(scored):
    chain = _chain("m1", "m2", "m3", "far")
    c, _ = A._distinct_first(chain, swarm.RunLedger())
    assert sorted(c) == sorted(chain)


def test_concurrent_workers_never_reserve_the_same_identity(scored):
    chain = _chain("m1", "m2", "m3", "m4", "m5", "m6")
    ledger = swarm.RunLedger()
    out, lock, gate = [], threading.Lock(), threading.Barrier(6)

    def worker():
        gate.wait()
        c, _ = A._distinct_first(chain, ledger)
        with lock:
            out.append(c[0][1])
    ts = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(out) == ["m1", "m2", "m3", "m4", "m5", "m6"]


def test_ledger_release_gives_a_reservation_back():
    led = swarm.RunLedger()
    led.reserve("p", "m")
    led.reserve("p", "n")
    led.release("p", "m")
    assert led.used() == ["p/n"]
    led.release("p", "never")                     # harmless
    assert led.used() == ["p/n"]


# ---- through _swarm_dispatch ------------------------------------------------

class _Resp:
    status_code = 200

    def __init__(self, text):
        self._t = text

    def json(self):
        return {"choices": [{"finish_reason": "stop",
                             "message": {"role": "assistant", "content": self._t}}]}

    def close(self):
        pass


@pytest.fixture
def stage(monkeypatch, scored):
    chain = _chain("m1", "m2", "m3")
    seen = []
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("p1", "m1", "hard"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: list(chain))
    monkeypatch.setattr(A, "_est_tokens", lambda *a, **k: 100)
    monkeypatch.setattr(A, "_record_outcome", lambda *a, **k: None)
    monkeypatch.setattr(A, "_record_chat_usage", lambda *a, **k: None)
    monkeypatch.setattr(A, "_act_pick", lambda *a, **k: None)
    monkeypatch.setattr(A, "_pipeline_time_left", lambda: None)
    monkeypatch.setattr(A, "_answer_gate", lambda *a, **k: "ok")

    def fake(pid, payload, deadline):
        seen.append((pid, payload["model"]))
        return _Resp("a worker answer that is long enough"), None
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", fake)
    return seen


def test_swarm_dispatch_without_a_ledger_is_the_chain_as_built(stage):
    for _ in range(3):
        text, who = A._swarm_dispatch([{"role": "user", "content": "x"}], 100)
        assert who == "p1/m1"
    assert stage == [("p1", "m1")] * 3


def test_swarm_dispatch_with_a_ledger_spreads_workers(stage):
    ledger = swarm.RunLedger()
    whos = [A._swarm_dispatch([{"role": "user", "content": "x"}], 100, ledger=ledger)[1]
            for _ in range(3)]
    assert whos == ["p1/m1", "p2/m2", "p3/m3"]
    assert ledger.used() == ["p1/m1", "p2/m2", "p3/m3"]


def test_the_flag_turns_distinct_models_off(stage, monkeypatch):
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=None: False if k == "swarm_distinct_models" else d)
    ledger = swarm.RunLedger()
    whos = [A._swarm_dispatch([{"role": "user", "content": "x"}], 100, ledger=ledger)[1]
            for _ in range(3)]
    assert whos == ["p1/m1"] * 3 and ledger.used() == []


def test_a_failed_hop_gives_its_reservation_back(stage, monkeypatch):
    def fake(pid, payload, deadline):
        stage.append((pid, payload["model"]))
        return (None, RuntimeError("down")) if pid == "p1" else (_Resp("fine long answer ok"), None)
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", fake)
    ledger = swarm.RunLedger()
    _t, who = A._swarm_dispatch([{"role": "user", "content": "x"}], 100, ledger=ledger)
    assert who == "p2/m2"
    assert ledger.used() == ["p2/m2"], "the dead p1 hop is not held against the next worker"


def test_the_pipeline_kwargs_follow_the_flags(monkeypatch):
    got = A._pipeline_check_kwargs()
    assert got.get("ledger") is True
    assert got.get("free_verdict") is A._free_verdict
    monkeypatch.setattr(A.config, "get_flag", lambda k, d=None: False)
    off = A._pipeline_check_kwargs()
    assert "ledger" not in off and "free_verdict" not in off and "search" not in off


# --------------------------------------------------------------------------- #
# swarm.run: ledger plumbing + workers_models
# --------------------------------------------------------------------------- #

_PHASE_RE = re.compile(r"YOUR PHASE \(\d+ of \d+\): (.+)")
PLAN = json.dumps({"goal": "Build the parser", "phases": [
    {"title": "Alpha", "task": "write the python parser module"},
    {"title": "Beta", "task": "write the usage notes"}]})
GOOD_A = "def parse(path):\n    return [dict(r) for r in rows(path)]  # reads every row\n"
GOOD_B = "The usage notes explain how to call the parser and what it returns to you."
ASK = [{"role": "user", "content": "build me a CSV parser with usage notes"}]


def _stage(messages):
    user = messages[-1]["content"]
    m = _PHASE_RE.search(user)
    if m:
        return "phase", m.group(1).strip()
    if "WHAT THE TEAM PRODUCED" in user:
        return "supervise", ""
    if "PHASE OUTPUTS" in user:
        return "synth", ""
    if user.startswith("BRIEF\n"):
        return "review", ""
    return "plan", ""


def _dispatcher(phase_text=None, verdict_log=None):
    calls = []
    lock = threading.Lock()

    def dispatch(messages, max_tokens, **kw):
        st, title = _stage(messages)
        with lock:
            calls.append({"stage": st, "title": title, "kw": dict(kw), "user": messages[-1]["content"]})
            n = len([c for c in calls if c["stage"] == st and c["title"] == title])
        if st == "plan":
            return PLAN, "planner/glm-5"
        if st == "phase":
            text = (phase_text or {}).get(title)
            if callable(text):
                text = text(n)
            return (text or (GOOD_A if title == "Alpha" else GOOD_B)), \
                "w%d/model-%d" % (len(calls), len(calls))
        if st == "supervise":
            return json.dumps({"missing": []}), "sup/qwen3"
        if st == "review":
            return json.dumps({"verdict": "ship", "problems": []}), "rev/llama-4"
        if st == "synth":
            return "FINAL", "syn/deepseek-v4"
        return "", None
    dispatch.calls = calls
    return dispatch


def test_ledger_reaches_only_the_worker_calls():
    d = _dispatcher()
    out = swarm.run(ASK, d, ledger=True)
    phase = [c for c in d.calls if c["stage"] == "phase"]
    assert phase and all(isinstance(c["kw"].get("ledger"), swarm.RunLedger) for c in phase)
    assert len({id(c["kw"]["ledger"]) for c in phase}) == 1, "one ledger for the whole run"
    others = [c for c in d.calls if c["stage"] != "phase"]
    assert others and all("ledger" not in c["kw"] for c in others)
    assert out["workers_models"] == [c for c in out["workers_models"]] and len(out["workers_models"]) == 2


def test_no_ledger_is_the_old_call_shape():
    d = _dispatcher()
    out = swarm.run(ASK, d)
    assert all("ledger" not in c["kw"] for c in d.calls)
    assert len(out["workers_models"]) == 2          # the result lists them either way


def test_crews_forward_ledger_and_free_verdict_only_when_given(monkeypatch):
    seen = {}

    def fake_run(messages, dispatch, **kw):
        seen.update(kw)
        return {"text": "x"}
    monkeypatch.setattr(crews.swarm, "run", fake_run)
    crews.run(ASK, lambda *a, **k: ("", None), "code")
    assert "ledger" not in seen and "free_verdict" not in seen
    fv = lambda b: None                                          # noqa: E731
    crews.run(ASK, lambda *a, **k: ("", None), "code", ledger=True, free_verdict=fv)
    assert seen["ledger"] is True and seen["free_verdict"] is fv


# --------------------------------------------------------------------------- #
# 2. per-phase free verdict
# --------------------------------------------------------------------------- #

def test_default_none_is_unchanged():
    d = _dispatcher()
    out = swarm.run(ASK, d)
    assert "phase_verdicts" not in out
    assert len([c for c in d.calls if c["stage"] == "phase"]) == 2


def test_verdict_bound_phases():
    assert swarm.verdict_bound({"title": "Parser", "task": "write the python parser", "done_when": ""})
    assert swarm.verdict_bound({"title": "x", "task": "plain", "output_format": "JSON object"})
    assert swarm.verdict_bound({"title": "Schema", "task": "design the SQL schema"})
    assert not swarm.verdict_bound({"title": "Usage notes", "task": "write the usage notes for users"})


def test_an_ok_verdict_changes_nothing_and_only_bound_phases_are_asked():
    d = _dispatcher()
    asked = []

    def fv(brief):
        asked.append(brief)
        return {"ok": True, "problems": [], "severity": "low"}
    out = swarm.run(ASK, d, free_verdict=fv)
    assert len(asked) == 1 and "Alpha" in asked[0]["text"], "Beta is prose: no verdict"
    assert asked[0]["producer"].startswith("w") and asked[0]["avoid_families"]
    assert len([c for c in d.calls if c["stage"] == "phase"]) == 2
    assert out["phase_verdicts"] == [{"phase": 1, "verdict": "ok", "severity": "low"}]


def test_not_ok_high_earns_the_one_retry_on_another_provider():
    d = _dispatcher(phase_text={"Alpha": lambda n: GOOD_A if n == 1 else
                                "def parse(path):\n    return fixed(path)  # corrected version\n"})
    calls = []

    def fv(brief):
        calls.append(brief)
        return {"ok": False, "problems": ["parse ignores the header row"], "severity": "high"}
    out = swarm.run(ASK, d, free_verdict=fv)
    alpha = [c for c in d.calls if c["stage"] == "phase" and c["title"] == "Alpha"]
    assert len(alpha) == 2, "exactly ONE retry"
    assert "parse ignores the header row" in alpha[1]["user"]
    assert alpha[1]["kw"]["exclude_pids"] == ("w2",) or alpha[1]["kw"]["exclude_pids"][0].startswith("w")
    assert len(calls) == 1, "at most one verdict per phase"
    rec = out["phase_verdicts"][0]
    assert rec["verdict"] == "revise" and rec["retried"] is True


def test_not_ok_below_high_changes_nothing():
    d = _dispatcher()
    out = swarm.run(ASK, d, free_verdict=lambda b: {"ok": False, "problems": ["style"],
                                                     "severity": "low"})
    assert len([c for c in d.calls if c["stage"] == "phase" and c["title"] == "Alpha"]) == 1
    assert out["phase_verdicts"][0].get("retried") is None


@pytest.mark.parametrize("reply", [None, "garbage", {"nope": 1}])
def test_a_missing_or_unreadable_verdict_changes_nothing(reply):
    d = _dispatcher()
    out = swarm.run(ASK, d, free_verdict=lambda b: reply)
    assert len([c for c in d.calls if c["stage"] == "phase" and c["title"] == "Alpha"]) == 1
    assert out["phase_verdicts"][0]["verdict"] == "none"


def test_a_verdict_function_that_raises_costs_nothing():
    def boom(brief):
        raise RuntimeError("verifier down")
    d = _dispatcher()
    out = swarm.run(ASK, d, free_verdict=boom)
    assert out["text"]


def test_a_phase_the_free_checks_reject_is_not_sent_to_the_verdict():
    d = _dispatcher(phase_text={"Alpha": "   "})
    asked = []
    swarm.run(ASK, d, free_verdict=lambda b: asked.append(b))
    assert not asked
