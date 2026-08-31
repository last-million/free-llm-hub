"""A model declining the task is a non-answer, not an answer.

ASKED 2026-08-31: "he should auto detect that if task can't be done with a model
then he try with other model usually hy3 and deepseek v4 flash and qwen 3.8
flash they can do things that other models dont accept to do".

The hub already knew three ways a 200 can fail to be an answer -- a relay's
error page returned as content, a tool call typed out as prose, and a turn that
only announced work. A refusal is the fourth, and it was invisible: "I can't
help with that" is a clean 200 with real text, so the chain accepted it, the
turn ended, and the reliability ledger recorded a success. The next turn then
picked the same model, which refused again.

It now counts as a non-answer: the chain moves to the next model and the
refusing id is sidelined for a while, so the retry lands somewhere else.

Which somewhere else matters. The user named three models that accept work
others decline -- hy3, deepseek-v4-flash, qwen3.8 -- so the chain is guaranteed
to contain at least one of them when it can, rather than leaving it to chance
whether the fallback order happens to include one.

FALSE POSITIVES are the whole risk here, so the detector is deliberately narrow:
a SHORT reply, no tool calls, that is essentially nothing but the refusal. A
long answer that happens to contain "I can't find the file" is a real answer and
is left alone.
"""
import app


def _resp(content, tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"index": 0, "message": msg, "finish_reason": "stop"}]}


REFUSALS = [
    "I can't help with that.",
    "I cannot help with this request.",
    "I'm sorry, but I can't assist with that.",
    "Sorry, I am unable to assist with this request.",
    "I won't be able to help with that request.",
    "I'm not able to help with that.",
    "As an AI language model, I cannot create that content.",
    "I must decline this request.",
]


# --------------------------------------------------------------------------- #
# The detector
# --------------------------------------------------------------------------- #

def test_the_common_refusals_are_caught():
    for text in REFUSALS:
        assert app._looks_like_refusal(text) is True, text


def test_a_real_answer_mentioning_a_limit_is_not_a_refusal():
    """The false positive that matters: work got done, and the model said what
    it could not reach while doing it."""
    for text in ("I can't find config.yml, so I created one with sane defaults "
                 "and wired it into the loader. All 12 pages build cleanly now.",
                 "Done. Note I couldn't reach the CDN, so fonts are self-hosted.",
                 "Built the page. I cannot verify the API key without your account, "
                 "so the request is stubbed behind a flag you can flip."):
        assert app._looks_like_refusal(text) is False, text


def test_a_question_is_not_a_refusal():
    assert app._looks_like_refusal("I can't tell which database you want -- "
                                   "Postgres or SQLite?") is False


def test_a_long_refusal_with_alternatives_is_still_a_refusal():
    """CORRECTED 2026-08-31 after measuring three real ones: they were 865,
    4585 and 1404 characters. A refusal in practice is the refusal PLUS a
    paragraph explaining why and offering alternatives, so the short-message cap
    this test originally asserted caught none of them. What is characteristic is
    WHERE it appears -- a model that declines says so in its opening sentence."""
    text = "I cannot fulfill this request. " + ("Here is an alternative approach. " * 60)
    assert app._looks_like_refusal(text) is True


def test_work_that_merely_mentions_a_limit_later_is_not_a_refusal():
    """The false positive the head-only test protects against: the turn did the
    work, and said what it could not reach while doing it."""
    text = ("Built all 12 pages and wired the nav. " * 3) +            "I can't reach the CDN from here, so the fonts are self-hosted."
    assert app._looks_like_refusal(text) is False


def test_empty_and_broken_input_is_safe():
    for text in ("", None, "   ", 42):
        assert app._looks_like_refusal(text) is False


# --------------------------------------------------------------------------- #
# Wired into the machinery the hub already has
# --------------------------------------------------------------------------- #

def test_a_refusal_counts_as_a_non_answer():
    assert app._chat_json_nonanswer(_resp(REFUSALS[0]), has_tools=True) is True


def test_it_counts_even_without_tools():
    """Unlike the announcement and typed-tool-call detectors, this one applies
    to plain chat too: a refusal is no more useful there, and the hub has other
    models that will answer."""
    assert app._chat_json_nonanswer(_resp(REFUSALS[0])) is True


def test_a_real_tool_call_is_never_a_refusal():
    calls = [{"id": "c1", "type": "function",
              "function": {"name": "shell", "arguments": "{}"}}]
    assert app._chat_json_nonanswer(_resp(REFUSALS[0], tool_calls=calls),
                                    has_tools=True) is False


def test_an_ordinary_answer_still_passes():
    assert app._chat_json_nonanswer(_resp("Done -- all 12 pages built."),
                                    has_tools=True) is False


# --------------------------------------------------------------------------- #
# ...and the retry lands somewhere that will actually do the work
# --------------------------------------------------------------------------- #

def test_the_named_models_are_recorded_as_permissive():
    """hy3, deepseek-v4-flash and qwen3.8 -- named by the user as the ones that
    accept work others decline."""
    joined = " ".join(app._PERMISSIVE_MODELS).lower()
    assert "hy3" in joined
    assert "deepseek-v4-flash" in joined or "deepseek-v4" in joined
    assert "qwen3.8" in joined or "qwen-3.8" in joined


def test_a_permissive_model_is_recognised():
    assert app._is_permissive("tencent/hy3:free") is True
    assert app._is_permissive("deepseek-ai/deepseek-v4-flash-0731") is True
    assert app._is_permissive("qwen/qwen3.8-27b") is True
    assert app._is_permissive("openai/gpt-oss-120b") is False


def test_the_chain_carries_one_of_them():
    """So a refusal always has somewhere to go, instead of it being luck
    whether the fallback order happened to include one."""
    from unittest import mock
    chain = [("google", "models/gemini-3.7-flash"), ("groq", "z-ai/glm-5.3")]
    with mock.patch.object(app, "_permissive_candidates",
                           return_value=[("kilocode", "tencent/hy3:free")]):
        out = app._ensure_permissive_hop(chain)
    assert any(app._is_permissive(m) for _p, m in out), out
    assert out[:2] == chain, "ordinary models keep their order and lead"


def test_it_goes_ahead_of_the_last_resort_tail():
    """CORRECTED after test_last_resort_routing caught it: appending at the very
    END put a normal model behind the families _build_chain deliberately parks
    last (kimi-k2.x, nemotron, gpt-oss). It goes in front of those instead."""
    from unittest import mock
    chain = [("google", "models/gemini-3.7-flash"), ("groq", "openai/gpt-oss-120b")]
    with mock.patch.object(app, "_permissive_candidates",
                           return_value=[("kilocode", "tencent/hy3:free")]):
        out = app._ensure_permissive_hop(chain)
    ids = [m for _p, m in out]
    assert ids.index("tencent/hy3:free") < ids.index("openai/gpt-oss-120b")
    assert ids[0] == "models/gemini-3.7-flash", "a real model still leads"


def test_it_does_not_duplicate_one_already_there():
    from unittest import mock
    chain = [("kilocode", "tencent/hy3:free"), ("groq", "openai/gpt-oss-120b")]
    with mock.patch.object(app, "_permissive_candidates",
                           return_value=[("other", "tencent/hy3:free")]):
        assert app._ensure_permissive_hop(chain) == chain


def test_an_empty_chain_is_safe():
    assert app._ensure_permissive_hop([]) == []
