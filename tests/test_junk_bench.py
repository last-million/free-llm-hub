"""The JUNK BENCH: a (provider, model) that answers garbage sits out.

REPORTED 2026-09-26: after the answer gate had recorded failures,
llm7/GLM-5.3-Flash still won "best" and coding, live, streaming and not. The
demotions that existed were too weak (_reliability_penalty caps at 9 points)
or too slow (the canary's 15 points need two canary runs, one per 6 h). Now
three junk/salvaged answers in an hour bench THAT PAIR for 6 h -- per pair,
so the same model on another provider stays usable -- and every pick path
honours it. No network: every provider-facing seam is faked.
"""
import shutil
import tempfile
from unittest import mock

import pytest

import app
import perfstats
import quota

BAD = ("llm7", "GLM-5.3-Flash")
TWIN = ("nvidia", "GLM-5.3-Flash")          # same weights, other deployment


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(app, "_canary_state", {})
    monkeypatch.setattr(app.quota, "_persist_maybe", lambda: None)
    monkeypatch.setattr(app, "_save_perf_stats", lambda force=False: None)
    with app._outcome_lock:
        app._outcomes.clear()
    with app._junk_lock:
        app._junk_events.clear()
        app._junk_bench.clear()
    yield
    with app._outcome_lock:
        app._outcomes.clear()
    with app._junk_lock:
        app._junk_events.clear()
        app._junk_bench.clear()


def _junk(pair, n=1):
    for _ in range(n):
        app._record_outcome(pair[0], pair[1], False, junk=True)


@pytest.fixture
def neutral():
    with mock.patch.object(app, "_quota_headroom", return_value=1.0), \
            mock.patch.object(app, "_sustain_penalty", return_value=0.0), \
            mock.patch.object(app, "_tool_dialect_penalty", return_value=0.0), \
            mock.patch.object(app, "_latency_penalty", return_value=0.0):
        yield


# --------------------------------------------------------------------------- #
# 1) the bench itself
# --------------------------------------------------------------------------- #

def test_three_junk_answers_in_an_hour_bench_the_pair():
    _junk(BAD, 2)
    assert not app._is_pair_benched(*BAD)
    _junk(BAD, 1)
    assert app._is_pair_benched(*BAD)


def test_the_bench_is_per_pair_not_per_identity():
    _junk(BAD, 3)
    assert app._is_pair_benched(*BAD)
    assert not app._is_pair_benched(*TWIN)
    assert app._answer_quality_penalty(*TWIN) == 0.0


def test_junk_spread_over_more_than_an_hour_does_not_bench(monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(app.time, "time", lambda: clock[0])
    _junk(BAD, 2)
    clock[0] += app._JUNK_BENCH_WINDOW + 1
    _junk(BAD, 1)
    assert not app._is_pair_benched(*BAD)


def test_the_bench_lasts_six_hours_then_lifts(monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(app.time, "time", lambda: clock[0])
    _junk(BAD, 3)
    clock[0] += app._JUNK_BENCH_TTL - 60
    assert app._is_pair_benched(*BAD)
    clock[0] += 120
    assert not app._is_pair_benched(*BAD)


def test_plain_http_failures_never_bench():
    for _ in range(10):
        app._record_outcome(BAD[0], BAD[1], False)
    assert not app._is_pair_benched(*BAD)


def test_salvaged_answer_is_a_strike():
    for _ in range(3):
        app._record_chat_usage(BAD[0], BAD[1], {}, 10, ok=False)
    assert app._is_pair_benched(*BAD)


def test_stream_gate_failure_is_a_strike(monkeypatch):
    monkeypatch.setattr(app.answer_check, "inspect", lambda *a, **k: {"ok": False})
    for _ in range(3):
        app._record_stream_outcome(BAD[0], BAD[1], "loop loop loop")
    assert app._is_pair_benched(*BAD)


def test_canary_junk_counts_and_a_correct_canary_lifts_early():
    app._record_canary_verdict(BAD[0], BAD[1], "junk")
    _junk(BAD, 2)
    assert app._is_pair_benched(*BAD)
    app._record_canary_verdict(BAD[0], BAD[1], "correct")
    assert not app._is_pair_benched(*BAD)
    # ...and the strikes went with it: one more junk does not re-bench.
    _junk(BAD, 1)
    assert not app._is_pair_benched(*BAD)


def test_benched_pair_is_rechecked_by_the_canary_sooner(monkeypatch):
    now = 2_000_000.0
    monkeypatch.setattr(app, "_ranked_free_pairs",
                        lambda limit=6: [(140.0, BAD[0], BAD[1]), (130.0, "a", "m")])
    monkeypatch.setattr(app, "_canary_provider_eligible", lambda pid: True)
    monkeypatch.setattr(app.quota, "is_model_throttled", lambda pid, m: False)
    probed = now - app._JUNK_BENCH_RECHECK - 5       # < 6 h ago, > 1 h ago
    app._canary_state[BAD] = {"last_probe": probed}
    app._canary_state[("a", "m")] = {"last_probe": probed}
    assert app._canary_due_pairs(now=now) == []
    with app._junk_lock:
        app._junk_bench[BAD] = {"until": 9e12, "count": 3}
    assert app._canary_due_pairs(now=now) == [BAD]


def test_bench_survives_a_restart():
    _junk(BAD, 3)
    blob = app._dead_state_dump()
    assert "llm7|GLM-5.3-Flash" in blob["junk_bench"]["bench"]
    with app._junk_lock:
        app._junk_bench.clear()
        app._junk_events.clear()
    app._dead_state_load(blob)
    assert app._is_pair_benched(*BAD)


def test_a_bench_longer_than_ttl_in_a_file_is_refused():
    app._junk_bench_load({"bench": {"x|y": {"until": 9e12, "count": 3}}})
    assert not app._is_pair_benched("x", "y")


def test_providers_card_names_the_bench():
    _junk(BAD, 3)
    rows = app._junk_bench_rows("llm7")
    assert rows and rows[0]["detail"] == "llm7 GLM-5.3-Flash benched 6 h: 3 junk answers"
    assert app._junk_bench_rows("nvidia") == []
    with mock.patch.object(app.quota, "status", return_value={}), \
            mock.patch.object(app.quota, "is_model_throttled", return_value=False):
        out = app._provider_out_status("llm7", {}, ["GLM-5.3-Flash", "other"])
    assert "llm7 GLM-5.3-Flash benched 6 h: 3 junk answers" in out["detail"]


# --------------------------------------------------------------------------- #
# 2) junk weighs more, and the last 24 h lead
# --------------------------------------------------------------------------- #

def test_a_junk_answer_counts_as_two_failures():
    app._record_outcome(BAD[0], BAD[1], False, junk=True)
    app._record_outcome("p", "m", False)
    assert app._outcomes[BAD]["fail"] == app._JUNK_FAIL_WEIGHT == 2
    assert app._outcomes[("p", "m")]["fail"] == 1
    assert app._reliability(*BAD) < app._reliability("p", "m")


def test_one_plain_failure_still_reads_a_third():
    app._record_outcome("p", "m", False)
    assert app._reliability("p", "m") == pytest.approx(1 / 3)


def test_recent_failures_outweigh_an_old_good_record():
    now = app.time.time()
    old_good = {"ok": 30, "fail": 6, "last": now}
    app._outcomes[("p", "m")] = dict(old_good)
    lifetime_only = app._reliability("p", "m")
    app._outcomes[("p", "m")] = dict(old_good, rok=0, rfail=6, rstart=now - 3600)
    assert app._reliability("p", "m") < lifetime_only - 0.3
    # ...and a recent bucket older than 24 h is ignored.
    app._outcomes[("p", "m")] = dict(old_good, rok=0, rfail=6,
                                     rstart=now - app._RECENT_WINDOW - 60)
    assert app._reliability("p", "m") == pytest.approx(lifetime_only)


def test_recent_bucket_persists_through_perfstats(monkeypatch):
    d = tempfile.mkdtemp(prefix="hub-pytest-junkbench-")
    try:
        monkeypatch.setattr(perfstats, "_path", lambda: d + "/perf.json")
        now = app.time.time()
        rec = {("p", "m"): {"ok": 3, "fail": 2, "last": now,
                            "rok": 1, "rfail": 2, "rstart": now - 10}}
        assert perfstats.save(rec, {}, force=True)
        outcomes, _lat = perfstats.load()
        assert outcomes[("p", "m")]["rfail"] == 2
        assert outcomes[("p", "m")]["rok"] == 1
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_recent_bucket_persists_through_the_quota_blob():
    app._record_outcome("p", "m", False)
    blob = app._dead_state_dump()
    with app._outcome_lock:
        app._outcomes.clear()
    app._dead_state_load(blob)
    assert app._outcomes[("p", "m")]["rfail"] == 1


# --------------------------------------------------------------------------- #
# 3) every pick path honours it
# --------------------------------------------------------------------------- #

def test_chat_pick_and_agentic_score_pay_the_bench(neutral):
    _junk(BAD, 3)
    with mock.patch.object(app, "_reliability_penalty", return_value=0.0):
        bad = app._chat_pick_key((138.0,) + BAD)[0]
        twin = app._chat_pick_key((138.0,) + TWIN)[0]
        assert twin - bad == pytest.approx(app._JUNK_BENCH_PENALTY)
        assert (app._agentic_score((138.0,) + TWIN)
                - app._agentic_score((138.0,) + BAD)) == pytest.approx(app._JUNK_BENCH_PENALTY)
    pool = [(138.0,) + BAD, (120.0, "groq", "llama")]
    assert max(pool, key=app._chat_pick_key)[1] == "groq"


def test_spread_never_rotates_onto_a_benched_pair(neutral, monkeypatch):
    _junk(BAD, 3)
    pool = [(140.0,) + BAD, (132.0, "nvidia", "z-ai/glm-5.2"), (128.0, "groq", "llama")]
    monkeypatch.setattr(app, "_orch_cursor", 0)
    assert "llm7" not in {app._spread_pick(pool)[1] for _ in range(12)}
    assert "llm7" not in {p[1] for p in app._spread_band(pool)}


def test_unbenched_and_bench_last_fail_open():
    _junk(BAD, 3)
    only = [(140.0,) + BAD]
    assert app._unbenched(only) == only
    mixed = [(140.0,) + BAD, (100.0, "a", "m"), (90.0, "b", "n")]
    assert app._unbenched(mixed) == mixed[1:]
    assert app._bench_last(mixed) == mixed[1:] + mixed[:1]
    assert app._unbenched([BAD, ("a", "m")], 0, 1) == [("a", "m")]


def test_swarm_fanout_drops_a_benched_member():
    _junk(BAD, 3)
    cands = [BAD, ("a", "m1"), ("b", "m2"), ("c", "m3")]
    with mock.patch.object(app, "_benchmark_score", return_value=100.0), \
            mock.patch.object(app, "_swarm_fanout", return_value=3):
        picks = app._swarm_rank(cands)
    assert BAD not in picks and len(picks) == 3


def test_swarm_fanout_still_runs_when_everything_is_benched():
    _junk(BAD, 3)
    with mock.patch.object(app, "_benchmark_score", return_value=100.0):
        assert app._swarm_rank([BAD]) == [BAD]


# --- router + chain, against a faked fleet --------------------------------- #

FLEET = {"llm7": ["GLM-5.3-Flash"], "nvidia": ["GLM-5.3-Flash", "z-ai/glm-5.2"],
         "groq": ["llama-3.3-70b-versatile"]}
SCORES = {BAD: 150.0, TWIN: 140.0, ("nvidia", "z-ai/glm-5.2"): 130.0,
          ("groq", "llama-3.3-70b-versatile"): 110.0}


@pytest.fixture
def fleet(monkeypatch):
    saved = (dict(quota._STATE), dict(quota._MODEL_STATE),
             dict(quota._MODEL_THROTTLE), dict(quota._DYNAMIC),
             quota._PERSIST_PATH)
    for d in (quota._STATE, quota._MODEL_STATE, quota._MODEL_THROTTLE, quota._DYNAMIC):
        d.clear()
    quota._PERSIST_PATH = None
    monkeypatch.setattr(app, "_available_providers", lambda: list(FLEET))
    monkeypatch.setattr(app, "_auto_models", lambda pid: list(FLEET.get(pid, ())))
    monkeypatch.setattr(app, "_prefetch_auto_models",
                        lambda pids: {p: list(FLEET.get(p, ())) for p in pids})
    monkeypatch.setattr(app, "_sub_available_providers", lambda: [])
    monkeypatch.setattr(app, "_is_model_dead", lambda pid, m: False)
    monkeypatch.setattr(app, "_context_ok", lambda pid, m, est: True)
    monkeypatch.setattr(app, "_is_fast", lambda pid, m: True)
    monkeypatch.setattr(app, "_supports_tools", lambda pid, m: True)
    monkeypatch.setattr(app.prov, "is_model_allowed", lambda m: True)
    monkeypatch.setattr(app, "_aa_scores", {})
    monkeypatch.setattr(app, "_benchmark_score",
                        lambda pid, m: SCORES.get((pid, m), 10.0))
    app._session_pins.clear()
    try:
        yield
    finally:
        app._session_pins.clear()
        for d, s in zip((quota._STATE, quota._MODEL_STATE, quota._MODEL_THROTTLE,
                         quota._DYNAMIC), saved[:4]):
            d.clear()
            d.update(s)
        quota._PERSIST_PATH = saved[4]


_HARD = [{"role": "user", "content": (
    "refactor the whole routing chain, then write code for comprehensive "
    "tests, debug any failures, and optimize performance " + "x" * 2000)}]


@pytest.fixture(params=[True, False], ids=["always-best", "spread"])
def best_mode(request, monkeypatch):
    real = app.config.get_flag
    monkeypatch.setattr(app.config, "get_flag",
                        lambda name, default=None: request.param
                        if name == "route_always_best" else real(name, default))
    monkeypatch.setattr(app, "_orch_cursor", 0)
    return request.param


def test_router_best_and_coding_skip_the_benched_pair(fleet, best_mode, monkeypatch):
    # No twin host here, so the control is unambiguous: BAD (150) wins clean.
    solo = {"llm7": ["GLM-5.3-Flash"], "nvidia": ["z-ai/glm-5.2"],
            "groq": ["llama-3.3-70b-versatile"]}
    monkeypatch.setattr(app, "_available_providers", lambda: list(solo))
    monkeypatch.setattr(app, "_prefetch_auto_models",
                        lambda pids: {p: list(solo.get(p, ())) for p in pids})
    if best_mode:
        assert app._route_by_difficulty(_HARD)[:2] == BAD       # control
    _junk(BAD, 3)
    for _ in range(4):          # the spread rotation must never land on it
        for kw in ({}, {"quality_mode": True}, {"require_tools": True}):
            app._session_pins.clear()
            assert app._route_by_difficulty(_HARD, **kw)[:2] != BAD, kw


def test_router_keeps_the_same_model_on_another_provider(fleet, best_mode):
    _junk(BAD, 3)
    picks = set()
    for _ in range(4):
        app._session_pins.clear()
        picks.add(app._route_by_difficulty(_HARD)[:2])
    assert BAD not in picks
    if best_mode:
        assert picks == {TWIN}


def test_router_fails_open_when_only_the_benched_pair_lives(fleet, monkeypatch):
    monkeypatch.setattr(app, "_available_providers", lambda: ["llm7"])
    _junk(BAD, 3)
    assert app._route_by_difficulty(_HARD)[:2] == BAD


def test_chain_puts_the_benched_pair_last(fleet):
    _junk(BAD, 3)
    chain = app._build_chain(TWIN[0], TWIN[1], est=100, messages=_HARD)
    assert BAD in chain
    # Among the fleet's own hops (the permissive last-resort hop the chain
    # always appends is a different rule), the benched pair is the last.
    fleet_hops = [p for p in chain if p[1] in FLEET.get(p[0], ())]
    assert fleet_hops[-1] == BAD, chain
