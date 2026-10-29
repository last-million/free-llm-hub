"""A provider + model pair that never answers is blocked for 24 hours.

Owner, 2026-10-29: "auto blacklist the ones that never answer ... if a model doesn't answer
more than 3 times in 24 hours, blacklist it for 24 hours". Measured the same day: every g4f
copy of Claude Opus, gpt-6.1-sol, every Kimi K3 relay copy and nvidia kimi-k2.6 had never
answered yet stayed in the chain. Hermetic: fake clock values, in-memory config.
"""
import json
import time

import pytest

import app as A
import perfstats

P, M = "g4f", "srv_x:anthropic/claude-opus-5.5"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(A, "_outcomes", {})
    monkeypatch.setattr(A, "_dead_models", {})
    monkeypatch.setattr(A, "_auto_blocked", {})
    monkeypatch.setattr(A, "_save_perf_stats", lambda *a, **k: None)
    monkeypatch.setattr(A, "_client_gone", lambda: False)
    monkeypatch.setattr(A, "_local_net_failed", lambda p, m: False)
    monkeypatch.setattr(A, "_take_capacity_mark", lambda p, m: False)
    monkeypatch.setattr(A, "_is_model_blocked_by_user", lambda p, m: False)
    settings = {}
    monkeypatch.setattr(A.config, "get_setting", lambda n, d=None: settings.get(n, d))
    monkeypatch.setattr(A.config, "set_setting", lambda n, v: settings.__setitem__(n, v))
    real_flag = A.config.get_flag
    monkeypatch.setattr(A.config, "get_flag",
                        lambda n, d=False: True if n == "auto_block_no_answer" else real_flag(n, d))
    return settings


def _fail(n, pid=P, model=M):
    for _ in range(n):
        A._record_outcome(pid, model, False)


# --------------------------------------------------------------------------- the rule

def test_three_failures_are_not_enough():
    _fail(3)
    assert not A._is_model_dead(P, M)


def test_more_than_three_failures_with_no_answer_block_the_pair_for_a_day():
    _fail(4)
    assert A._is_model_dead(P, M) and A._is_model_skipped(P, M)
    left = A._dead_models[(P, M)] - time.time()
    assert 23.9 * 3600 < left <= 24 * 3600
    assert "no answer in the last 24 h" in A._auto_block_why(P, M)


def test_an_answer_today_protects_the_pair_from_a_short_outage():
    A._record_outcome(P, M, True)
    rec = A._outcomes[(P, M)]
    rec["rok"], rec["rstart"] = 0, time.time() - 60      # a fresh bucket, answer still < 24 h ago
    _fail(6)
    assert not A._is_model_dead(P, M), "answered within 24 h: not 'never answers'"


def test_an_answer_older_than_a_day_does_not_protect_it():
    A._record_outcome(P, M, True)
    rec = A._outcomes[(P, M)]
    rec["lok"] = time.time() - 25 * 3600
    rec["rok"], rec["rfail"], rec["rstart"] = 0, 0, time.time() - 60
    _fail(4)
    assert A._is_model_dead(P, M)


def test_a_real_answer_lifts_the_block_at_once():
    _fail(4)
    assert A._is_model_dead(P, M)
    A._record_outcome(P, M, True)
    assert not A._is_model_dead(P, M)
    assert (P, M) not in A._auto_blocked


def test_the_block_is_per_pair_not_per_model_name():
    _fail(4)
    other = ("nvidia", "anthropic/claude-opus-5.5")
    assert A._is_model_dead(P, M)
    assert not A._is_model_dead(*other), "a working copy elsewhere is untouched"


def test_the_block_expires_on_its_own():
    _fail(4)
    A._dead_models[(P, M)] = time.time() - 1
    assert not A._is_model_dead(P, M)


def test_a_client_that_left_is_not_a_failure(monkeypatch):
    monkeypatch.setattr(A, "_client_gone", lambda: True)
    _fail(8)
    assert not A._is_model_dead(P, M)


def test_a_capacity_429_is_not_a_failure(monkeypatch):
    monkeypatch.setattr(A, "_take_capacity_mark", lambda p, m: True)
    _fail(8)
    assert not A._is_model_dead(P, M)


def test_flag_off_blocks_nothing(monkeypatch):
    monkeypatch.setattr(A, "_auto_block_on", lambda: False)
    _fail(10)
    assert not A._is_model_dead(P, M)


def test_a_403_dead_mark_never_shortens_the_day():
    _fail(4)
    until = A._dead_models[(P, M)]
    A._mark_model_dead(P, M, 403)
    assert A._dead_models[(P, M)] >= until


def test_a_403_dead_mark_is_not_lifted_by_the_auto_unblock():
    A._mark_model_dead(P, M, 404)
    A._record_outcome(P, M, True)
    assert A._is_model_dead(P, M), "only a block this feature made is lifted by an answer"


# --------------------------------------------------------------------------- the first sweep

def _rec(ok, fail, age_h=1.0):
    return {"ok": ok, "fail": fail, "last": time.time() - age_h * 3600}


def test_the_first_sweep_blocks_the_pairs_that_never_answered(clean):
    A._outcomes.update({
        (P, M): _rec(0, 29, age_h=24.1),                      # Opus 5.5: never, last try a day ago
        ("g4f", "srv_x:openai/gpt-6.1-sol"): _rec(0, 22),
        ("nvidia", "moonshotai/kimi-k2.6"): _rec(0, 32),
        ("g4f", "srv_y:openai/gpt-6-sol"): _rec(1, 10),          # answered once: not "never"
        ("nvidia", "z-ai/glm-5.3-flash"): _rec(31, 2),
        ("g4f", "srv_z:x/three-fails"): _rec(0, 3),              # not more than 3
        ("g4f", "srv_old:x/ancient"): _rec(0, 40, age_h=8 * 24), # past the 7-day record
    })
    n = A._auto_block_sweep()
    assert n == 3
    assert A._is_model_dead(P, M)
    assert A._is_model_dead("g4f", "srv_x:openai/gpt-6.1-sol")
    assert A._is_model_dead("nvidia", "moonshotai/kimi-k2.6")
    for pair in (("g4f", "srv_y:openai/gpt-6-sol"), ("nvidia", "z-ai/glm-5.3-flash"),
                 ("g4f", "srv_z:x/three-fails"), ("g4f", "srv_old:x/ancient")):
        assert not A._is_model_dead(*pair), pair
    assert "never answered" in A._auto_block_why(P, M)


def test_the_first_sweep_runs_once(clean):
    A._outcomes[(P, M)] = _rec(0, 29)
    assert A._auto_block_sweep() == 1
    A._dead_models.clear(); A._auto_blocked.clear()
    assert A._auto_block_sweep() == 0, "a restart must not re-block for another day"
    assert not A._is_model_dead(P, M)


def test_the_first_sweep_skips_models_the_user_already_switched_off(monkeypatch):
    monkeypatch.setattr(A, "_is_model_blocked_by_user", lambda p, m: True)
    A._outcomes[(P, M)] = _rec(0, 29)
    assert A._auto_block_sweep() == 0


# --------------------------------------------------------------------------- persistence + display

def test_the_block_and_its_reason_survive_a_restart():
    _fail(4)
    blob = json.loads(json.dumps(A._dead_state_dump()))
    A._dead_models.clear(); A._auto_blocked.clear()
    A._dead_state_load(blob)
    assert A._is_model_dead(P, M)
    assert "no answer" in A._auto_block_why(P, M)


def test_the_last_answer_time_survives_perfstats(tmp_path, monkeypatch):
    monkeypatch.setattr(perfstats, "_path", lambda: str(tmp_path / "perf-stats.json"))
    now = time.time()
    rec = {"ok": 3, "fail": 1, "last": now, "rok": 3, "rfail": 1, "rstart": now - 10, "lok": now - 5}
    assert perfstats.save({(P, M): rec}, {}, force=True)
    out, _ = perfstats.load()
    assert abs(out[(P, M)]["lok"] - (now - 5)) < 0.01


def test_an_older_record_gets_its_latest_possible_answer_time():
    A._outcomes[(P, M)] = {"ok": 5, "fail": 2, "last": 1000.0}
    A._outcomes[("g4f", "never")] = {"ok": 0, "fail": 9, "last": 1000.0}
    A._seed_last_answer()
    assert A._outcomes[(P, M)]["lok"] == 1000.0
    assert "lok" not in A._outcomes[("g4f", "never")]


def test_the_dashboard_says_why(monkeypatch):
    _fail(4)
    A._mark_model_dead("nvidia", "gone-model", 404)
    with A.app.test_client() as c:
        monkeypatch.setattr(A.config, "get_control_token", lambda: None)
        r = c.get("/api/dead-models")
    rows = {(x["provider"], x["model"]): x for x in r.get_json()["dead"]}
    assert "no answer" in rows[(P, M)]["reason"]
    assert "refused" in rows[("nvidia", "gone-model")]["reason"]
