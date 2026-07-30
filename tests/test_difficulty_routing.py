"""Tests for the difficulty-aware routing ("caveman" mode) heuristics in app.py:
_classify_difficulty() tiers and _apply_reasoning_effort()'s per-difficulty
reasoning_effort mapping + output-budget cap. Pure heuristics, no network, no
monkeypatching needed.
"""
import app


def _msgs(text):
    return [{"role": "user", "content": text}]


# --------------------------------------------------------------------------- #
# _classify_difficulty
# --------------------------------------------------------------------------- #

def test_short_trivial_ask_is_simple():
    assert app._classify_difficulty(_msgs("hi")) == "simple"
    assert app._classify_difficulty(_msgs("what is a mutex?")) == "simple"


def test_code_plus_length_is_medium():
    text = (
        "Here is a function:\n```\ndef rotate(keys):\n"
        "    return keys[1:] + keys[:1]\n```\n"
        "Can you explain what it does and whether it handles empty input? "
        + "detail " * 60
    )
    assert app._classify_difficulty(_msgs(text)) == "medium"


def test_heavy_ask_is_hard():
    text = (
        "refactor the whole routing chain, then write code for comprehensive "
        "tests, debug any failures, and optimize performance " + "x" * 2000
    )
    assert app._classify_difficulty(_msgs(text)) == "hard"


def test_big_system_prompt_does_not_inflate_to_hard():
    """Judged on the latest user turn, not the whole conversation."""
    msgs = [
        {"role": "system", "content": "context " * 5000},
        {"role": "user", "content": "thanks"},
    ]
    assert app._classify_difficulty(msgs) == "simple"


def test_large_max_tokens_bumps_score():
    plain = _msgs("a" * 200)  # >180 chars so no 'trivial' discount, no hints -> score 0
    assert app._classify_difficulty(plain) == "simple"
    assert app._classify_difficulty(plain, max_tokens=4000) == "medium"
    # but a genuinely trivial short ask stays simple even with a big budget
    assert app._classify_difficulty(_msgs("hi"), max_tokens=4000) == "simple"


# --------------------------------------------------------------------------- #
# _apply_reasoning_effort
# --------------------------------------------------------------------------- #

def test_effort_follows_difficulty_on_reasoning_models():
    for difficulty, effort in (("simple", "low"), ("medium", "medium"), ("hard", "high")):
        payload = {"max_tokens": 8000}
        out = app._apply_reasoning_effort(payload, "gpt-oss-120b", difficulty)
        assert out["reasoning_effort"] == effort


def test_non_reasoning_model_untouched():
    payload = {"max_tokens": 8000}
    out = app._apply_reasoning_effort(payload, "llama-3.3-70b", "hard")
    assert "reasoning_effort" not in out


def test_effort_capped_by_output_budget():
    """'high' effort on a small budget must drop so thinking can't eat the
    whole allowance and return empty content."""
    out = app._apply_reasoning_effort({"max_tokens": 500}, "deepseek-r1", "hard")
    assert out["reasoning_effort"] == "low"
    out = app._apply_reasoning_effort({"max_tokens": 1500}, "deepseek-r1", "hard")
    assert out["reasoning_effort"] == "medium"


def test_missing_difficulty_is_noop():
    payload = {"max_tokens": 8000}
    out = app._apply_reasoning_effort(payload, "gpt-oss-120b", None)
    assert "reasoning_effort" not in out
