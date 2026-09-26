"""The answer-canary quality penalty (two junk/wrong canary answers -> 15
points for 24 h per (provider, model)) used to reach ONLY _agentic_score, so
plain chat -- the hard/medium chat pick and the spread rotation -- kept
preferring a pair that answers "2826HAMSTER-2826…". It now applies there too.
"""
from unittest import mock

import pytest

import app


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.setattr(app, "_canary_state", {})
    monkeypatch.setattr(app.quota, "_persist_maybe", lambda: None)
    with app._outcome_lock:
        app._outcomes.clear()
    yield
    with app._outcome_lock:
        app._outcomes.clear()


def _demote(pid, model):
    app._record_canary_verdict(pid, model, "junk")
    app._record_canary_verdict(pid, model, "junk")
    assert app._answer_quality_penalty(pid, model) == app._CANARY_PENALTY


@pytest.fixture
def neutral():
    with mock.patch.object(app, "_quota_headroom", return_value=1.0), \
            mock.patch.object(app, "_sustain_penalty", return_value=0.0), \
            mock.patch.object(app, "_tool_dialect_penalty", return_value=0.0), \
            mock.patch.object(app, "_latency_penalty", return_value=0.0):
        yield


def test_chat_pick_key_pays_the_quality_penalty(neutral):
    _demote("llm7", "GLM-5.3-Flash")
    bad = app._chat_pick_key((138.0, "llm7", "GLM-5.3-Flash"))
    clean = app._chat_pick_key((138.0, "nvidia", "z-ai/glm-5.2"))
    assert clean[0] - bad[0] == pytest.approx(app._CANARY_PENALTY)


def test_chat_pick_prefers_a_clean_pair_over_a_stronger_junk_one(neutral):
    _demote("llm7", "GLM-5.3-Flash")
    pool = [(138.0, "llm7", "GLM-5.3-Flash"), (130.0, "nvidia", "z-ai/glm-5.2")]
    assert max(pool, key=app._chat_pick_key)[1] == "nvidia"


def test_chat_pick_key_is_unchanged_without_a_demotion(neutral):
    assert app._chat_pick_key((138.0, "llm7", "GLM-5.3-Flash"))[0] == pytest.approx(138.0)


def test_spread_pick_never_rotates_onto_a_demoted_pair(neutral, monkeypatch):
    _demote("llm7", "GLM-5.3-Flash")
    pool = [(140.0, "llm7", "GLM-5.3-Flash"), (132.0, "nvidia", "z-ai/glm-5.2"),
            (128.0, "groq", "llama-3.3-70b-versatile")]
    monkeypatch.setattr(app, "_orch_cursor", 0)
    picks = {app._spread_pick(pool)[1] for _ in range(12)}
    assert "llm7" not in picks
    assert picks == {"nvidia", "groq"}


def test_spread_pick_still_answers_when_every_member_is_demoted(neutral, monkeypatch):
    pool = [(140.0, "a", "m1"), (135.0, "b", "m2")]
    for _s, p, m in pool:
        _demote(p, m)
    monkeypatch.setattr(app, "_orch_cursor", 0)
    assert app._spread_pick(pool) is not None


def test_spread_band_membership_pays_the_penalty(neutral):
    # 112 is inside a 30-point band under 140; minus 15 it is not
    pool = [(140.0, "a", "m1"), (112.0, "llm7", "GLM-5.3-Flash")]
    assert {p[1] for p in app._spread_band(pool)} == {"a", "llm7"}
    _demote("llm7", "GLM-5.3-Flash")
    assert {p[1] for p in app._spread_band(pool)} == {"a"}


def test_spread_band_is_never_emptied_by_the_penalty(neutral):
    pool = [(140.0, "llm7", "GLM-5.3-Flash")]
    _demote("llm7", "GLM-5.3-Flash")
    assert app._spread_band(pool) == pool


def test_gate_failures_lower_reliability_for_that_pair_only():
    data = {"choices": [{"index": 0, "finish_reason": "length", "message": {
        "role": "assistant", "content": "2826HAMSTER-2826282628262826282628262826"}}]}
    payload = {"messages": [{"role": "user",
                             "content": "What is 2825 plus 1? Answer with only the number."}]}
    assert app._answer_gate(data, payload, False) == "salvaged"
    app._record_chat_usage("llm7", "GLM-5.3-Flash", data, 10, ok=False)
    assert app._reliability("llm7", "GLM-5.3-Flash") < 0.5
    assert app._reliability_penalty("llm7", "GLM-5.3-Flash") > 0
    assert app._reliability("nvidia", "GLM-5.3-Flash") == 0.5
