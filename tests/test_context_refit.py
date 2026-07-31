"""Context-window handling: learn the real limit, re-fit, and remember it.

MEASURED 2026-07-31 from a real Codex "build me a store" session: 10 of 13
requests were answered by llm7/gemini-3.1-flash-LITE — the weakest model in the
fleet — because the strong first hop 400'd every single time. Root cause:
cloudflare/@cf/qwen/qwen3-30b-a3b-fp8 has a 32,768-token window, but
_PROVIDER_TPM told the router cloudflare could take 120,000, so every large
turn was built ~3x too big and rejected.

Three separate bugs, one per section below.
"""
import pytest

import app


# --------------------------------------------------------------------------- #
# 1. The error bodies must be recognised — including the 413 phrasing
# --------------------------------------------------------------------------- #

CF_400 = ('{"errors":[{"message":"AiError: {\\"error\\":{\\"message\\":\\"This '
          "model's maximum context length is 32768 tokens. However, you requested "
          '64 output tokens\\"}}"}]}')
CF_413 = ('{"errors":[{"message":"AiError: Ai: The estimated number of input and '
          'maximum output tokens (42532) exceeded this model context window limit '
          '(32768)."}]}')


class _R:
    def __init__(self, text, status=400):
        self.text = text
        self.status_code = status

    def close(self):
        pass


@pytest.mark.parametrize("body", [CF_400, CF_413])
def test_both_cloudflare_phrasings_are_recognised_as_context_errors(body):
    """The 413 wording ('context window limit') was NOT matched before, so the
    real window was never learned from it and the re-fit had nothing to use."""
    assert app._SOFT_400_CONTEXT_RE.search(body)


@pytest.mark.parametrize("body", [CF_400, CF_413])
def test_the_real_window_is_extracted_not_the_requested_size(body):
    """CF_413 contains BOTH 42532 (what was sent) and 32768 (the limit). Learning
    42532 would be worse than useless — it is larger than the real window."""
    app._MODEL_MAX_INPUT.pop(("t", "m"), None)
    app._learn_context_limit("t", "m", _R(body))
    assert app._MODEL_MAX_INPUT.get(("t", "m")) == 32768


def test_an_unrelated_400_teaches_nothing():
    app._MODEL_MAX_INPUT.pop(("t", "m2"), None)
    app._learn_context_limit("t", "m2", _R('{"error":"invalid api key"}'))
    assert ("t", "m2") not in app._MODEL_MAX_INPUT


# --------------------------------------------------------------------------- #
# 2. Re-fit: apply the freshly-learned limit to THIS request, not just the next
# --------------------------------------------------------------------------- #

def _convo(turns):
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(turns):
        msgs.append({"role": "user", "content": ("x" * 400) + str(i)})
        msgs.append({"role": "assistant", "content": "ok " + str(i)})
    return msgs


def test_an_oversized_conversation_is_recompacted_to_the_learned_window():
    app._MODEL_MAX_INPUT[("t", "big")] = 8000
    payload = {"model": "big", "max_tokens": 4000, "messages": _convo(200)}
    assert app._est_tokens(payload["messages"]) > 8000
    refit = app._refit_payload_to_learned_ctx("t", payload)
    assert refit is not None
    assert app._est_tokens(refit["messages"]) <= 8000
    assert len(refit["messages"]) < len(payload["messages"])


def test_the_output_budget_is_capped_too():
    """max_tokens counts against the same window on most providers, so leaving a
    4000-token reply budget on an 8000-token window re-triggers the error."""
    app._MODEL_MAX_INPUT[("t", "big")] = 8000
    refit = app._refit_payload_to_learned_ctx(
        "t", {"model": "big", "max_tokens": 4000, "messages": _convo(200)})
    assert refit["max_tokens"] < 4000


def test_the_system_prompt_survives_recompaction():
    app._MODEL_MAX_INPUT[("t", "big")] = 8000
    refit = app._refit_payload_to_learned_ctx(
        "t", {"model": "big", "max_tokens": 100, "messages": _convo(200)})
    assert refit["messages"][0]["role"] == "system"


def test_no_refit_when_it_already_fits():
    """Then the 400 was about something else and recompacting would only lose
    context for no reason."""
    app._MODEL_MAX_INPUT[("t", "big")] = 500000
    assert app._refit_payload_to_learned_ctx(
        "t", {"model": "big", "max_tokens": 100, "messages": _convo(5)}) is None


def test_refit_never_raises_on_a_junk_payload():
    for bad in ({}, {"messages": None}, {"messages": []}, {"messages": "nope"}):
        assert app._refit_payload_to_learned_ctx("t", bad) is None


# --------------------------------------------------------------------------- #
# 3. Persistence — a context window is a fixed fact, not a per-boot discovery
# --------------------------------------------------------------------------- #

def test_learned_windows_survive_a_restart_cycle():
    """They were in-memory only, so every restart forgot them and re-learned
    each by burning a real failed request on the best hop."""
    app._MODEL_MAX_INPUT[("cloudflare", "@cf/qwen/qwen3-30b-a3b-fp8")] = 32768
    blob = app._dead_state_dump()
    assert blob["model_max_input"]["cloudflare|@cf/qwen/qwen3-30b-a3b-fp8"] == 32768
    app._MODEL_MAX_INPUT.clear()
    app._dead_state_load(blob)
    assert app._MODEL_MAX_INPUT[("cloudflare", "@cf/qwen/qwen3-30b-a3b-fp8")] == 32768


def test_reload_keeps_the_SMALLER_limit():
    """Two sources disagreeing means the smaller one is the safe truth."""
    app._MODEL_MAX_INPUT[("p", "m")] = 16000
    app._dead_state_load({"model_max_input": {"p|m": 32768}})
    assert app._MODEL_MAX_INPUT[("p", "m")] == 16000
    app._dead_state_load({"model_max_input": {"p|m": 8000}})
    assert app._MODEL_MAX_INPUT[("p", "m")] == 8000


def test_garbage_in_the_saved_blob_is_ignored():
    before = dict(app._MODEL_MAX_INPUT)
    app._dead_state_load({"model_max_input": {"nopipe": 9999, "p|m3": "big",
                                              "p|m4": 12}})   # 12 is absurdly small
    assert ("p", "m3") not in app._MODEL_MAX_INPUT
    assert ("p", "m4") not in app._MODEL_MAX_INPUT
    app._MODEL_MAX_INPUT.clear()
    app._MODEL_MAX_INPUT.update(before)
