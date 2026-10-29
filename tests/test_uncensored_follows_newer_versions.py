"""The uncensored / steerable category follows a family to its newer release.

The category names `deepseek-v4-flash`. DeepSeek V4.1 Flash is spelled `deepseek-v4.1-flash`
(or `deepseek-v4-1-flash` on some relays), which does not contain that text, so on 2026-10-29
the newer model sat outside the category while its predecessor was in (7 live ids).
"""
import pytest

import model_categories as mc

IN = [
    ("nvidia", "deepseek-ai/deepseek-v4.1-flash"),
    ("freemodel", "deepseek-v4.1-flash"),
    ("g4f", "poe/DeepSeek-V4.1-Flash-T"),
    ("g4f", "srv_mrdypihj16e8b1776409:deepseek/deepseek-v4.1-flash"),
    ("g4f", "srv_x:deepseek/deepseek-v4-1-flash"),
    # what was already in must stay in
    ("dahl", "deepseek-ai/DeepSeek-V4-Flash-0731"),
    ("dahl", "zai-org/GLM-5.3-Flash"),
    ("freemodel", "qwen3.8-27b"),
]

OUT = [
    # the v4.1 PRO / reasoning cuts are a different claim about behaviour
    ("nvidia", "deepseek-ai/deepseek-v4.1-pro"),
    ("g4f", "srv_x:deepseek/deepseek-v4-pro"),
    # nothing here says a flagship Claude / GPT / Gemini is steerable
    ("g4f", "srv_x:anthropic/claude-opus-5.5"),
    ("g4f", "srv_x:openai/gpt-6.1-sol"),
    ("g4f", "srv_x:google/gemini-4-argon"),
]


@pytest.mark.parametrize("pid,model", IN)
def test_member(pid, model):
    assert mc.matches("uncensored", pid, model)


@pytest.mark.parametrize("pid,model", OUT)
def test_not_a_member(pid, model):
    assert not mc.matches("uncensored", pid, model)
