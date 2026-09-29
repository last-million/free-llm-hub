"""Categories: a model belongs to as many as fit, by name AND by evidence.

RECHECKED 2026-09-29 against the live fleet: 34 of 117 alive models were in no
category, among them the #2 model overall (stealth/space-bunny-alpha, 137.7),
qwen3.8-27b (134.1) was only "uncensored", and MiniMax-M2.7 was in none -- so
choosing "coding" shut out the models the ranking rates best.
"""
import pytest

import app
import model_categories as MC


def test_the_missed_models_are_named():
    assert MC.categories_for("openrouter", "stealth/space-bunny-alpha", "space-bunny-alpha") == \
        ["swarm", "coding", "context", "vision", "seo"]
    assert {"swarm", "coding"} <= set(MC.categories_for("groq", "qwen/qwen3.8-27b", "qwen3.8-27b"))
    assert {"swarm", "coding"} <= set(MC.categories_for("dahl", "MiniMaxAI/MiniMax-M2.7", "minimax-m2.7"))


def test_one_model_many_categories():
    cats = MC.categories_for("nvidia", "z-ai/glm-5.3", "glm-5.3")
    assert len(cats) >= 4 and {"coding", "swarm", "context"} <= set(cats)


def test_exclusions_are_readable_on_their_own():
    assert MC.excluded("seo", "google", "models/gemini-3.8-flash", "gemini-3.8-flash")
    assert not MC.excluded("seo", "nvidia", "z-ai/glm-5.3", "glm-5.3")
    assert not MC.excluded("coding", "google", "models/gemini-3.8-flash")   # no "!" in coding


@pytest.fixture
def facts(monkeypatch):
    """A made-up model 'acme/nova-9' whose facts each test sets."""
    f = {"score": 135.0, "tools": True, "window": (1000000, "catalog"), "thinks": True}
    monkeypatch.setattr(app, "_benchmark_score", lambda p, m: f["score"])
    monkeypatch.setattr(app, "_supports_tools", lambda p, m: f["tools"])
    monkeypatch.setattr(app, "_model_ctx_info", lambda p, m: f["window"])
    monkeypatch.setattr(app, "_thinks_by_default", lambda p, m: f["thinks"])
    return f


def test_a_new_strong_model_lands_in_the_right_categories(facts):
    got = [k for k in MC.CATEGORY_KEYS if app._category_matches(k, "acme", "acme/nova-9")]
    assert got == ["swarm", "coding", "reasoning", "context", "seo"]
    # behavioural categories are never inferred
    for k in ("uncensored", "specialist", "fast", "vision"):
        assert not app._category_matches(k, "acme", "acme/nova-9")


def test_evidence_needs_tools_and_the_top_band(facts):
    facts["score"] = 120.0
    assert not app._category_matches("coding", "acme", "acme/nova-9")
    facts["score"], facts["tools"] = 135.0, False
    assert not app._category_matches("swarm", "acme", "acme/nova-9")


def test_long_context_needs_a_known_window(facts):
    facts["window"] = (1000000, "default")          # a guess never counts
    assert not app._category_matches("context", "acme", "acme/nova-9")
    facts["window"] = (262144, "catalog")
    assert not app._category_matches("context", "acme", "acme/nova-9")
    facts["window"] = (400000, "learned")
    assert app._category_matches("context", "acme", "acme/nova-9")


def test_reasoning_needs_a_thinker_and_seo_respects_its_exclusions(facts):
    facts["thinks"] = False
    assert not app._category_matches("reasoning", "acme", "acme/nova-9")
    assert app._category_matches("seo", "acme", "acme/nova-9")
    assert not app._category_matches("seo", "acme", "acme/nova-9-flash")   # "!flash"


def test_routing_uses_the_same_test(facts, monkeypatch):
    monkeypatch.setattr(app, "_request_category_overrides", lambda: {})
    monkeypatch.setattr(app, "_category_overrides", lambda: {})
    assert app._mode_allows("coding", "acme", "acme/nova-9", session_overrides={})
    facts["score"] = 50.0
    assert not app._mode_allows("coding", "acme", "acme/nova-9", session_overrides={})
    # a user's own removal still wins over evidence
    facts["score"] = 135.0
    monkeypatch.setattr(app, "_category_overrides",
                        lambda: {"coding": {"remove": {app._normalize_model_identity("acme/nova-9")}}})
    assert not app._mode_allows("coding", "acme", "acme/nova-9", session_overrides={})
