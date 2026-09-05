"""What "uncensored" covers, and what it deliberately does not.

USER-REPORTED 2026-09-05: "in uncensored there is not just grok 4 but also
qwen 3.8 and deepseek v4 flash and glm 5.3".

Two of the three were already there. MEASURED against the live fleet that day:

    qwen3.8            7 live ids,  7 matched
    deepseek-v4-flash 12 live ids, 12 matched
    glm-5.3           14 live ids,  0 matched   <- the real gap
    grok-4             1 live id,   1 matched

So the report was half a bug and half a coincidence: grok-4-fast simply won the
one pick the user happened to watch, while qwen3.8 and deepseek were in the pool
all along. glm was genuinely missing, and adding it took the category from 34
live models to 60.

The pattern is "glm-5", not "glm". glm-4.6-thinking is a REASONING model and
belongs to that category: sweeping it in here would be claiming something about
its refusal behaviour on the strength of a shared prefix.
"""
import pytest

import model_categories as MC


def _m(model, pid="groq"):
    return MC.matches("uncensored", pid, model)


# --------------------------------------------------------------------------- #
# The three the user named
# --------------------------------------------------------------------------- #

def test_glm_5_is_uncensored():
    """The one that was actually missing."""
    assert _m("z-ai/glm-5.3-free")
    assert _m("z-ai/glm-5.2:free")
    assert _m("glm-5.3")


def test_qwen38_was_already_uncensored():
    assert _m("qwen/qwen3.8-27b")


def test_deepseek_v4_flash_was_already_uncensored():
    assert _m("deepseek-ai/DeepSeek-V4-Flash-0731")


def test_grok_is_still_uncensored():
    """It was never wrong -- it just happened to win the pick that was watched."""
    assert _m("tb/grok-4-fast")


# --------------------------------------------------------------------------- #
# ...and what stays out
# --------------------------------------------------------------------------- #

def test_the_thinking_glm_is_not_swept_in():
    """glm-4.6-thinking is a reasoning model. "uncensored" is a claim about how
    a model responds to a system prompt, and a shared prefix is not evidence
    for it."""
    assert not _m("glm-4.6-thinking")
    assert MC.matches("reasoning", "g4f", "glm-4.6-thinking")


def test_an_ordinary_model_is_not_uncensored():
    for m in ("gemini-3.7-flash", "llama-3.3-70b-instruct", "minimax-m3",
              "nemotron-3-ultra-550b-a55b"):
        assert not _m(m), m


def test_the_category_is_not_everything():
    """A filter that matches the whole fleet is not a filter. Guards against a
    future pattern like a bare "glm" or a single letter."""
    fleet = ["gemini-3.7-flash", "llama-3.3-70b", "minimax-m3", "kimi-k3",
             "nemotron-3-nano", "gemma-4-31b-it", "inkling", "lyria-3",
             "qwen/qwen3.8-27b", "z-ai/glm-5.3-free"]
    matched = [m for m in fleet if _m(m)]
    assert 0 < len(matched) < len(fleet), matched


def test_every_pattern_is_specific_enough_to_mean_something():
    """A one- or two-character pattern would match half the catalog by
    accident."""
    for p in MC._BY_KEY["uncensored"]:
        assert len(p) >= 3, p
