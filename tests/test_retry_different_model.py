"""Retry with a different model: the caller can veto models by identity.

A user who gets a bad or falsely-refused answer needs a second opinion from a
DIFFERENT model. The client names what it already tried in
X-Free-LLM-Hub-Exclude and the router skips it.

Deliberately user-driven and one model per press. This is not an automatic
"keep re-rolling until something answers" loop -- each retry is a person
deciding that a specific answer was wrong.

Matching is by model IDENTITY (leaf name), so vetoing gpt-oss-120b excludes it
on every host rather than sending the same weights back from the next provider.
"""
from unittest import mock

import app


def test_header_parses_into_identities():
    got = app._excluded_identities("groq/openai/gpt-oss-120b, cerebras/gemma-4-31b")
    assert got == {"gpt-oss-120b", "gemma-4-31b"}


def test_a_bare_model_id_works_too():
    assert app._excluded_identities("gpt-oss-120b") == {"gpt-oss-120b"}


def test_the_same_model_is_vetoed_across_every_host():
    """The whole point of matching on the leaf: cerebras' bare 'gpt-oss-120b',
    groq's 'openai/gpt-oss-120b' and cloudflare's '@cf/openai/gpt-oss-120b' are
    one model. Vetoing one must not hand back another spelling of it."""
    veto = app._excluded_identities("groq/openai/gpt-oss-120b")
    for spelling in ("gpt-oss-120b", "openai/gpt-oss-120b",
                     "@cf/openai/gpt-oss-120b", "openai/gpt-oss-120b:free"):
        assert app._normalize_model_identity(spelling) in veto, spelling


def test_empty_or_missing_header_vetoes_nothing():
    for raw in (None, "", "   ", ",,"):
        assert app._excluded_identities(raw) == set()


def _chain(primary, model, **kw):
    """Build a chain against a small fake catalog."""
    with mock.patch.object(app, "_available_providers", return_value=["groq", "cerebras"]), \
            mock.patch.object(app, "_provider_capable", return_value=True), \
            mock.patch.object(app, "_auto_models",
                              side_effect=lambda pid: {"groq": ["openai/gpt-oss-120b", "qwen/qwen3.8-27b"],
                                                       "cerebras": ["gpt-oss-120b", "gemma-4-31b"]}[pid]), \
            mock.patch.object(app, "_is_model_dead", return_value=False), \
            mock.patch.object(app, "_context_ok", return_value=True), \
            mock.patch.object(app.quota, "is_model_throttled", return_value=False), \
            mock.patch.object(app.quota, "model_status", return_value={"exhausted": False}):
        return app._build_chain(primary, model, **kw)


def test_a_vetoed_model_is_absent_from_the_whole_chain():
    veto = {"gpt-oss-120b"}
    chain = _chain("groq", "qwen/qwen3.8-27b", exclude_identities=veto)
    assert chain, "chain should not be empty"
    for pid, m in chain:
        assert app._normalize_model_identity(m) not in veto, (pid, m)
    # ...and it really was in there without the veto.
    plain = _chain("groq", "qwen/qwen3.8-27b")
    assert any(app._normalize_model_identity(m) == "gpt-oss-120b" for _p, m in plain)


def test_a_vetoed_PRIMARY_is_not_tried_first():
    """The bug this guards: the model the user just rejected was force-seeded as
    hop 1, so 'retry with a different model' answered from the same model."""
    chain = _chain("groq", "openai/gpt-oss-120b",
                   exclude_identities={"gpt-oss-120b"})
    assert chain, "a veto on the primary must not empty the chain"
    assert app._normalize_model_identity(chain[0][1]) != "gpt-oss-120b", chain[0]


def test_an_empty_primary_lets_the_chain_choose_one():
    """Used when Auto's pick is itself vetoed: hand the choice to the chain."""
    chain = _chain("", "", exclude_identities={"gpt-oss-120b"})
    assert chain
    assert ("", "") not in chain, "an empty primary must never be seeded as a hop"
    for _pid, m in chain:
        assert app._normalize_model_identity(m) != "gpt-oss-120b"


def test_no_veto_keeps_the_previous_behaviour_exactly():
    assert _chain("groq", "qwen/qwen3.8-27b") == \
        _chain("groq", "qwen/qwen3.8-27b", exclude_identities=None)
