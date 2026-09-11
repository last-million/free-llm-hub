r"""Picking a mode excluded the best model for it.

MEASURED 2026-09-11 against the live fleet: 67 of 156 answering models matched
no mode at all -- including qwen3.6-27b, the highest-scoring model on the whole
fleet and, per the September 2026 community ranking, the open-weight model
people actually put on agentic coding. Choosing "Coding" therefore routed
around the best coding model available.

The patterns had drifted: they named deepseek-v4-pro, kimi-k3 and gpt-5.6-sol
while the fleet had moved on to qwen3.6, kimi-k2.6, claude-fable and
gpt-5.6-luna. Refreshed against what is actually live, informed by what the
community currently ranks.

Two things fell out of the refresh:

  * "mini" is a substring of geMINI. Every Gemini model on the fleet --
    gemini-3.1-PRO included -- was being sold as "fast / cheap". Found by
    asking why a flagship was excluded from a category that subtracts the
    cheap tier.
  * a category can now SUBTRACT with a leading "!", because "seo" is a claim
    about behaviour that a whole family does not share: "glm-5.3" cannot help
    also matching glm-5.3-flash, and the request was explicitly for the models
    that think hardest.
"""
import model_categories as MC
import pytest


def cats(identity):
    return [k for k in MC.CATEGORY_KEYS if MC.matches(k, "prov", identity, identity)]


# --------------------------------------------------------------------------- #
# The bug that started it
# --------------------------------------------------------------------------- #

def test_gemini_is_not_a_mini():
    """A bare "mini" matched geMINI, so the whole family read as cheap."""
    assert "fast" not in cats("gemini-3.1-pro-preview")
    assert "fast" not in cats("gemini-2.5-pro")


def test_an_actual_mini_still_reads_as_fast():
    for ident in ("gpt-4o-mini", "o4-mini"):
        assert "fast" in cats(ident), ident


def test_a_flash_is_still_fast():
    assert "fast" in cats("gemini-3.6-flash")
    assert "fast" in cats("glm-5.3-flash")


# --------------------------------------------------------------------------- #
# The models the fleet actually serves
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("ident", [
    "qwen3.6-27b",          # top-scoring model on the fleet; matched nothing
    "kimi-k2.6",
    "claude-fable-5.1",
    "gpt-5.6-luna",
    "codellama-70b",
    "mimo-v2.5",
])
def test_a_live_coding_model_is_in_coding(ident):
    assert "coding" in cats(ident), ident


def test_the_long_context_models_are_in_context():
    """qwen3.6 carries the longest context in the open-weight class."""
    assert "context" in cats("qwen3.6-27b")
    assert "context" in cats("llama-4-maverick-17b-128e-instruct")


def test_a_multimodal_model_without_vl_in_its_name_is_still_vision():
    """"-vl" caught the Qwen-VL spellings and missed everything else."""
    for ident in ("llama-4-maverick-17b-128e-instruct", "gemma-4-31b-it"):
        assert "vision" in cats(ident), ident


def test_the_vl_families_are_still_vision():
    for ident in ("qwen3-vl-235b-a22b-instruct", "internvl3-78b",
                  "llama-3.2-90b-vision-instruct"):
        assert "vision" in cats(ident), ident


# --------------------------------------------------------------------------- #
# SEO: follows a brief exactly, and thinks first
# --------------------------------------------------------------------------- #

def test_seo_is_a_mode_people_can_pick():
    assert "seo" in MC.CATEGORY_KEYS
    labels = dict((k, lab) for k, lab, _h in MC.labels())
    assert "SEO" in labels["seo"]


def test_seo_takes_the_models_that_follow_a_brief():
    """Independent testing through 2026 puts Claude first for adhering to a
    detailed brief, with GPT-5 close behind."""
    for ident in ("claude-sonnet-4-5", "claude-fable-5", "gpt-5.6-luna",
                  "gpt-5.2", "glm-5.3", "kimi-k3", "qwen3.6-27b"):
        assert "seo" in cats(ident), ident


def test_seo_takes_the_thinking_variants():
    """"max thinking for best results" was half the request."""
    assert "seo" in cats("glm-5.2-thinking")
    assert "seo" in cats("deepseek-v4-pro-0813")


def test_seo_refuses_the_cheap_tier():
    """The other half: a flash/lite/mini variant is the one thing "max
    thinking" rules out."""
    for ident in ("glm-5.3-flash", "gemini-3.6-flash", "qwen3.8-flash",
                  "gpt-4o-mini", "granite-4.0-h-micro"):
        assert "seo" not in cats(ident), ident


def test_seo_still_wants_the_full_size_gemini():
    """The exclusion must not take the family it wants most: "!mini" would
    have subtracted every Gemini."""
    assert "seo" in cats("gemini-3.1-pro-preview")


# --------------------------------------------------------------------------- #
# The subtraction mechanism itself
# --------------------------------------------------------------------------- #

def test_a_bang_pattern_subtracts():
    assert MC.matches("seo", "p", "glm-5.3", "glm-5.3")
    assert not MC.matches("seo", "p", "glm-5.3-flash", "glm-5.3-flash")


def test_a_category_with_no_bang_patterns_is_unchanged():
    """Every other category keeps plain substring behaviour."""
    assert MC.matches("uncensored", "p", "dolphin-2.9", "dolphin-2.9")
    assert MC.matches("fast", "p", "something-lite", "something-lite")


def test_subtraction_beats_a_positive_hit():
    """Order must not matter: the exclusion wins wherever it sits."""
    assert not MC.matches("seo", "p", "claude-sonnet-4-5-flash",
                          "claude-sonnet-4-5-flash")


def test_an_unknown_category_matches_nothing():
    assert not MC.matches("no-such-mode", "p", "anything", "anything")


# --------------------------------------------------------------------------- #
# Guard rails
# --------------------------------------------------------------------------- #

def test_swarm_is_still_not_a_mode():
    """It is the fan-out PIPELINE's id; reading it as a mode turns the feature
    off. (app._mode_keys enforces this; this pins the premise.)"""
    assert "swarm" in MC.CATEGORY_KEYS


def test_every_category_has_a_label_and_help():
    for key, label, helptext in MC.labels():
        assert label and helptext, key


def test_no_category_is_only_exclusions():
    """A category with nothing but "!" patterns matches nothing at all, which
    is a silently empty mode rather than an error."""
    for key, _label, _help, pats in MC.CATEGORIES:
        assert any(not p.startswith("!") for p in pats), key


# --------------------------------------------------------------------------- #
# Every generation of a family, not just the one that was current
# --------------------------------------------------------------------------- #

def test_an_older_gemini_pro_is_not_left_out():
    """The patterns named "gemini-3", so gemini-2.5-pro -- multimodal, long
    context, and live on this fleet -- matched nothing at all."""
    got = cats("gemini-2.5-pro")
    for key in ("context", "vision", "seo"):
        assert key in got, key
    assert "fast" not in got


def test_hy3_is_used_where_it_belongs():
    """REQUESTED: "make sure using hy3 too if available". It is live and
    scores 130, and it was in coding/uncensored but not in the long-context or
    SEO modes it also qualifies for."""
    got = cats("hy3")
    for key in ("coding", "uncensored", "context", "seo"):
        assert key in got, key


def test_widening_gemini_did_not_make_a_flash_slow_down_seo():
    """"gemini-" now matches every generation, so the SEO subtraction is what
    keeps the cheap tier out of a mode that asked for maximum thinking."""
    assert "seo" not in cats("gemini-3.6-flash")
    assert "vision" in cats("gemini-3.6-flash")
