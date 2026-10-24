"""A provider that stalls on two DIFFERENT models is slow, not unlucky (2026-10-24).

MEASURED (hub.log): 01:40 nvidia/z-ai/glm-5.3 "silent 30.0s -> backup", then 01:44
nvidia/z-ai/glm-5.3-flash "silent 30.0s -> backup" -- two roles turns that each burned
~160 s, because the stall mark is per (provider, model) and the sibling model on the same
host was tried first again four minutes later. `_provider_stalling` puts the whole
provider behind the others for tool turns while two of its models carry a fresh stall
mark. Reorder only (the "sick" group is still the last resort), relays excluded, derived
from `_recent_hop_fail`, flag `provider_stall_rest`.

Also pinned here: a roles hop's HTTP 4xx explanation is LOGGED (keys masked) so a bare
"HTTP 400" can be read. MEASURED the same day: inception/mercury-3 400'd twice and the
only record said "HTTP 400".
"""
import logging
import time

import pytest

import app as A
import config


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(A, "_recent_hop_fail", {})
    monkeypatch.setattr(A, "_pair_rest", {})
    monkeypatch.setattr(A, "_chain_seen", {})
    monkeypatch.setattr(A.perfstats, "save", lambda *a, **k: False)
    monkeypatch.setattr(A, "_save_learned_state", lambda *a, **k: False)


def _mark(pid, model, kind="timeout", ago=30):
    A._recent_hop_fail[(pid, model)] = (time.time() - ago, kind)


def test_two_stalled_models_of_one_provider_rest_the_whole_provider():
    _mark("nvidia", "z-ai/glm-5.3")
    assert not A._provider_stalling("nvidia")          # one stall: only that pair's business
    _mark("nvidia", "z-ai/glm-5.3-flash", "deadline")
    assert A._provider_stalling("nvidia")
    # the sibling that has NOT stalled is now behind the others too (the 01:44 case) ...
    assert A._tool_turn_sick("nvidia", "moonshotai/kimi-k3")
    # ... and another provider is untouched
    assert not A._tool_turn_sick("groq", "qwen/qwen3.8-27b")


def test_the_same_model_stalling_twice_is_still_one_model():
    _mark("nvidia", "z-ai/glm-5.3")
    A._recent_hop_fail[("nvidia", "z-ai/glm-5.3")] = (time.time() - 5, "reasoning_stall")
    assert not A._provider_stalling("nvidia")


@pytest.mark.parametrize("kind", ["429", "junk", "invalid", "fail"])
def test_only_stall_kinds_count(kind):
    _mark("nvidia", "a", kind)
    _mark("nvidia", "b", kind)
    assert not A._provider_stalling("nvidia")


def test_a_stall_older_than_the_recent_ttl_no_longer_counts():
    _mark("nvidia", "a")
    _mark("nvidia", "b", ago=A._RECENT_FAIL_TTL + 5)
    assert not A._provider_stalling("nvidia")


def test_reasoning_stall_counts_as_a_stall():
    _mark("nvidia", "a", "reasoning_stall")
    _mark("nvidia", "b", "timeout")
    assert A._provider_stalling("nvidia")


def test_a_relay_is_never_rested_as_a_whole(monkeypatch):
    # g4f's models are separate backends; the relay layer has its own per-server logic.
    _mark("g4f", "srv_a:gpt-5")
    _mark("g4f", "srv_b:glm-5.3")
    assert A._is_relay_pid("g4f")
    assert not A._provider_stalling("g4f")


def test_the_switch_off_is_the_per_pair_behaviour(monkeypatch):
    _mark("nvidia", "a")
    _mark("nvidia", "b")
    orig = config.get_flag
    monkeypatch.setattr(config, "get_flag",
                        lambda k, d=None: False if k == "provider_stall_rest" else orig(k, d))
    assert not A._provider_stalling("nvidia")
    assert not A._tool_turn_sick("nvidia", "c")


@pytest.mark.parametrize("pid", [None, "", 7])
def test_garbage_never_raises(pid):
    assert A._provider_stalling(pid) is False


def test_it_clears_by_itself_when_a_model_answers_again():
    _mark("nvidia", "a")
    _mark("nvidia", "b")
    assert A._provider_stalling("nvidia")
    A._clear_recent_hop_failure("nvidia", "b")          # what a served answer does
    assert not A._provider_stalling("nvidia")


# --- the 4xx explanation is logged (keys masked) -----------------------------------------

class _R:
    def __init__(self, status, text):
        self.status_code = status
        self.text = text

    def json(self):
        return {"error": self.text}


def test_a_400s_explanation_reaches_the_log_with_keys_masked(caplog, monkeypatch):
    monkeypatch.setattr(A, "_secret_values", lambda: ["sk-live-SECRET-123"])
    body = '{"error":{"message":"max_tokens too large for sk-live-SECRET-123","code":400}}'
    with caplog.at_level(logging.WARNING, logger="free-llm-hub"):
        A._role_log_http_body("inception", "mercury-3", _R(400, body))
    out = caplog.text
    assert "inception/mercury-3 HTTP 400" in out
    assert "max_tokens too large" in out
    assert "sk-live-SECRET-123" not in out and "***" in out


@pytest.mark.parametrize("status", [200, 204, 429, 399])
def test_ok_and_429_are_not_logged(caplog, status):
    with caplog.at_level(logging.WARNING, logger="free-llm-hub"):
        A._role_log_http_body("p", "m", _R(status, "x"))
    assert "HTTP" not in caplog.text


def test_a_body_that_is_not_text_falls_back_to_json_and_never_raises(caplog):
    class NoText:
        status_code = 500

        def json(self):
            return {"error": "boom"}
    with caplog.at_level(logging.WARNING, logger="free-llm-hub"):
        A._role_log_http_body("p", "m", NoText())
        A._role_log_http_body("p", "m", object())
        A._role_log_http_body("p", "m", None)
    assert "HTTP 500" in caplog.text and "boom" in caplog.text


def test_the_body_is_clipped():
    class Big:
        status_code = 400
        text = "x" * 5000
    records = []

    class H(logging.Handler):
        def emit(self, rec):
            records.append(rec.getMessage())
    h = H()
    A._log.addHandler(h)
    try:
        A._role_log_http_body("p", "m", Big())
    finally:
        A._log.removeHandler(h)
    assert records and len(records[0]) < 400
