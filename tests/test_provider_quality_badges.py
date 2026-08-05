"""'Recommended' used to be one hand-curated provider (puter) -- every other
provider showed as an ordinary card no matter how strong its actual models
were.

USER 2026-08-04: "recommend should be for the best providers that give best
models llms in quality... and highlight new for new providers."

_provider_quality_score reuses _benchmark_score (the same scorer that already
ranks every model for routing) instead of inventing a second one: a
provider's quality is the best score any of its own free models reaches.
"""
import os
import shutil
import tempfile

import pytest

import app


@pytest.fixture
def state_dir():
    d = tempfile.mkdtemp(prefix="hub-pytest-quality-")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def isolated_config(state_dir, monkeypatch):
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", os.path.join(state_dir, "state", "config.json"))


def test_quality_score_is_the_best_of_its_free_models():
    score = app._provider_quality_score("cerebras", ["gpt-oss-120b", "gemma-4-31b"])
    assert score == max(app._benchmark_score("cerebras", "gpt-oss-120b"),
                        app._benchmark_score("cerebras", "gemma-4-31b"))
    assert score > 0


def test_quality_score_is_zero_with_no_free_models():
    assert app._provider_quality_score("some-pid", []) == 0
    assert app._provider_quality_score("some-pid", None) == 0


def test_quality_score_never_raises_on_a_junk_model_id():
    # A scoring quirk on one model must not break the whole provider list --
    # _benchmark_score itself scores unrecognized junk low (a real default,
    # not an error), so the only real invariant here is "does not raise" and
    # "stays well under the recommended bar".
    score = app._provider_quality_score("pid", [None, "", 12345])
    assert score < app._RECOMMENDED_QUALITY_THRESHOLD


def test_a_provider_with_a_genuinely_strong_free_model_is_recommended(isolated_config):
    """opencode-zen's default free model is deepseek-v4-flash-free -- top
    tier per tonight's own deepseek ranking work. It was never in the old
    hand-curated recommended set (only puter was), so this only passes if
    quality is now actually being computed, not just read from a flag."""
    row = app._provider_row("opencode-zen", live_models=False)
    assert row["quality_score"] >= app._RECOMMENDED_QUALITY_THRESHOLD
    assert row["recommended"] is True


def test_the_static_hand_curated_flag_still_works_standalone(isolated_config):
    """puter's recommended=True in providers.py must keep working even for
    a provider whose free_models list doesn't independently clear the
    quality bar -- the OR must not have silently become an AND."""
    row = app._provider_row("puter", live_models=False)
    assert row["recommended"] is True


def test_tokenrouter_is_flagged_new(isolated_config):
    row = app._provider_row("tokenrouter", live_models=False)
    assert row["new"] is True


def test_an_old_established_provider_is_not_flagged_new(isolated_config):
    row = app._provider_row("groq", live_models=False)
    assert row["new"] is False
