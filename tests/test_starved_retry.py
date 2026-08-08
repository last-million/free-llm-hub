"""Budget starvation: a reasoning model that spent max_tokens thinking.

MEASURED 2026-07-31 across cerebras / openrouter / opencode-zen / kilocode /
g4f-gemini (~85 live calls, budgets 1..512): when a model is starved, the reply
is a 200 with empty content and finish_reason == "length" — 49 times out of 49.
The hub used to score that as a dead hop and fall through, throwing away a model
that was one bigger budget away from answering.

Two traps this pins, both found in that measurement:
  * finish_reason == "length" WITH content is ordinary truncation and must NOT
    retry, or every truncated answer costs a second call.
  * the reasoning text cannot be the discriminator: it arrives as `reasoning`,
    `reasoning_content` OR `reasoning_details`, and g4f-gemini starves with NO
    reasoning field at all (message is literally {"role": "assistant"}).
"""
import pytest

import app

# The exact shapes observed live.
G4F_GEMINI_STARVED = {"choices": [{"finish_reason": "length",
                                   "message": {"role": "assistant"}}]}          # no content key
OPENCODE_STARVED = {"choices": [{"finish_reason": "length",
                                 "message": {"role": "assistant", "content": "",
                                             "reasoning_content": "We need to..."}}]}
CEREBRAS_STARVED = {"choices": [{"finish_reason": "length",
                                 "message": {"role": "assistant",
                                             "reasoning": "The user wants..."}}]}
OPENROUTER_STARVED = {"choices": [{"finish_reason": "length",
                                   "message": {"role": "assistant", "content": None,
                                               "reasoning": "...",
                                               "reasoning_details": [{"text": "..."}]}}]}
TRUNCATED_WITH_CONTENT = {"choices": [{"finish_reason": "length",
                                       "message": {"role": "assistant", "content": "OKI"}}]}
EMPTY_BUT_STOPPED = {"choices": [{"finish_reason": "stop",
                                  "message": {"role": "assistant", "content": ""}}]}


@pytest.mark.parametrize("shape", [G4F_GEMINI_STARVED, OPENCODE_STARVED,
                                   CEREBRAS_STARVED, OPENROUTER_STARVED])
def test_every_measured_starvation_shape_is_detected(shape):
    """Absent key, "", and null are all real encodings of 'no content'."""
    assert app._chat_json_is_empty(shape) is True
    assert app._chat_json_starved(shape) is True


def test_plain_truncation_is_not_treated_as_starvation():
    """finish_reason == 'length' WITH content is a normal truncated answer.
    Retrying it would double the cost of every long reply."""
    assert app._chat_json_is_empty(TRUNCATED_WITH_CONTENT) is False


def test_empty_with_finish_stop_is_a_real_failure_not_starvation():
    """A model that stopped cleanly and still said nothing is broken — fall
    through to the next hop instead of paying for a retry."""
    assert app._chat_json_is_empty(EMPTY_BUT_STOPPED) is True
    assert app._chat_json_starved(EMPTY_BUT_STOPPED) is False


def test_starvation_detection_never_raises_on_junk():
    for junk in ({}, {"choices": []}, {"choices": [None]}, {"choices": [{}]}):
        assert app._chat_json_starved(junk) is False


# --------------------------------------------------------------------------- #
# Retry budget
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("requested,expected", [
    (None, 512), (0, 512), (8, 512), (24, 512),     # tiny budgets -> the floor
    (256, 1024), (400, 1600),                       # 4x in the middle
    (512, 2048), (900, 2048),                       # clamped to the cap
])
def test_retry_budget_scales_then_clamps(requested, expected):
    assert app._starved_retry_budget(requested) == expected


@pytest.mark.parametrize("plenty", [2048, 4000, 100000])
def test_no_retry_when_the_caller_already_asked_for_plenty(plenty):
    """Starving at 2048+ is the model's problem, not the budget's — a retry
    would just burn a second call for the same empty answer."""
    assert app._starved_retry_budget(plenty) is None


def test_retry_budget_survives_a_non_numeric_max_tokens():
    assert app._starved_retry_budget("lots") == 512


# --------------------------------------------------------------------------- #
# The retry itself, through the real endpoint
# --------------------------------------------------------------------------- #

def test_a_starved_hop_is_retried_bigger_and_its_answer_returned(monkeypatch):
    """The whole point: recover the hop instead of discarding it."""
    calls = []

    def fake_dispatch(pid, payload, stream):
        calls.append(payload.get("max_tokens"))
        if len(calls) == 1:
            return _Resp(200, CEREBRAS_STARVED)                 # starved
        return _Resp(200, {"choices": [{"finish_reason": "stop",
                                        "message": {"role": "assistant", "content": "OK"}}]})
    monkeypatch.setattr(app, "_dispatch_chat", fake_dispatch)
    monkeypatch.setattr(app, "_build_chain", lambda *a, **k: [("cerebras", "zai-glm-4.7")])
    monkeypatch.setattr(app, "_check_provider_ready", lambda pid: None)
    client = app.app.test_client()
    r = client.post("/v1/chat/completions", json={
        "model": "cerebras/zai-glm-4.7", "max_tokens": 24, "stream": False,
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    body = r.get_json()
    assert body["choices"][0]["message"]["content"] == "OK"
    assert calls == [24, 512], "expected one retry at the floor budget, got %s" % calls


def test_a_hop_still_empty_after_the_retry_falls_through(monkeypatch):
    """One retry, not a loop — then behave exactly as before."""
    calls = []

    def fake_dispatch(pid, payload, stream):
        calls.append((pid, payload.get("max_tokens")))
        if pid == "cerebras":
            return _Resp(200, CEREBRAS_STARVED)                 # starved both times
        return _Resp(200, {"choices": [{"finish_reason": "stop",
                                        "message": {"role": "assistant", "content": "from B"}}]})
    monkeypatch.setattr(app, "_dispatch_chat", fake_dispatch)
    monkeypatch.setattr(app, "_build_chain",
                        lambda *a, **k: [("cerebras", "zai-glm-4.7"), ("groq", "llama")])
    monkeypatch.setattr(app, "_check_provider_ready", lambda pid: None)
    # "auto" resolves via _best_free_pair() -> real enabled+keyed providers,
    # which an isolated test config has none of. _build_chain is already
    # mocked and ignores whatever _resolve_model returns, so any well-shaped
    # pair that doesn't itself error out is enough to clear the gate.
    monkeypatch.setattr(app, "_resolve_model", lambda m: ("cerebras", "zai-glm-4.7"))
    client = app.app.test_client()
    r = client.post("/v1/chat/completions", json={
        "model": "auto", "max_tokens": 24, "stream": False,
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert r.get_json()["choices"][0]["message"]["content"] == "from B"
    assert [c[0] for c in calls] == ["cerebras", "cerebras", "groq"], calls


class _Resp:
    """Minimal response stand-in matching what the hop loop touches."""

    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.headers = {}
        self.text = ""

    def json(self):
        return self._payload

    def close(self):
        pass

    def iter_lines(self, decode_unicode=False):
        return iter(())
