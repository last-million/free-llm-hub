"""A hop that TIMES OUT can burn minutes doing it -- and an immediate retry of
the same request (codex's own client-level retry on 503, or the chain's own
bonus whole-chain retry) hits the SAME slow hop again, compounding into a long
stall with zero progress.

MEASURED 2026-08-03: nvidia/mistral-medium-3.5-128b ReadTimeout'd 4 times
running, ~7 minutes each, while every other hop in the chain failed fast
(429/400). Codex kept retrying the whole turn on the resulting 503, and each
retry re-selected the same still-slow hop -- 2.5 hours, zero site files
written. A 429 already gets a short quota.mark_throttled cooldown so the NEXT
chain build skips a just-rate-limited provider; a hop that times out now gets
the same treatment.
"""
import os
import shutil
import tempfile

import pytest
import requests

import app


@pytest.fixture
def state_dir():
    d = tempfile.mkdtemp(prefix="hub-pytest-hopcooldown-")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def isolated_config(state_dir, monkeypatch):
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", os.path.join(state_dir, "state", "config.json"))


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


def test_a_hop_that_times_out_gets_throttled_so_a_retry_skips_it(monkeypatch):
    throttled = []
    monkeypatch.setattr(app.quota, "mark_throttled",
                        lambda pid, secs=None: throttled.append((pid, secs)))

    def fake_dispatch(pid, payload, stream):
        if pid == "nvidia":
            raise requests.exceptions.ReadTimeout("timed out")
        return _Resp(200, {"choices": [{"finish_reason": "stop",
                                        "message": {"role": "assistant", "content": "OK"}}]})
    monkeypatch.setattr(app, "_dispatch_chat", fake_dispatch)
    monkeypatch.setattr(app, "_build_chain",
                        lambda *a, **k: [("nvidia", "mistral-medium-3.5-128b"),
                                        ("groq", "llama")])
    monkeypatch.setattr(app, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(app, "_resolve_model", lambda m: ("nvidia", "mistral-medium-3.5-128b"))
    client = app.app.test_client()
    r = client.post("/v1/chat/completions", json={
        "model": "auto", "max_tokens": 24, "stream": False,
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert r.get_json()["choices"][0]["message"]["content"] == "OK"
    assert ("nvidia", app._HOP_COOLDOWN_DEFAULT) in throttled, \
        "a timed-out hop must get the same cooldown a 429 already gets: %r" % throttled


def test_a_fast_failing_hop_is_not_throttled_only_a_real_timeout_is(monkeypatch):
    """A 400/connection-refused is a different failure class -- it isn't slow,
    so there's nothing costly about a retry reaching it again. Only a genuine
    Timeout should cost the provider its next-chain slot."""
    throttled = []
    monkeypatch.setattr(app.quota, "mark_throttled",
                        lambda pid, secs=None: throttled.append((pid, secs)))

    def fake_dispatch(pid, payload, stream):
        if pid == "flaky":
            raise requests.exceptions.ConnectionError("refused")
        return _Resp(200, {"choices": [{"finish_reason": "stop",
                                        "message": {"role": "assistant", "content": "OK"}}]})
    monkeypatch.setattr(app, "_dispatch_chat", fake_dispatch)
    monkeypatch.setattr(app, "_build_chain",
                        lambda *a, **k: [("flaky", "some-model"), ("groq", "llama")])
    monkeypatch.setattr(app, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(app, "_resolve_model", lambda m: ("flaky", "some-model"))
    client = app.app.test_client()
    r = client.post("/v1/chat/completions", json={
        "model": "auto", "max_tokens": 24, "stream": False,
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert not throttled, "a non-timeout RequestException must not throttle: %r" % throttled


# --------------------------------------------------------------------------- #
# The 429 path (inside _upstream_chat) shares the same cooldown constant --
# THE BUG (user-reported live): the same just-429'd g4f-gemini model kept
# getting retried attempt after attempt. The old 60s default was shorter than
# a real chain-exhaustion pass (measured 150-430s that session), so by the
# time the next attempt's chain was built, the previous 429's 60s memory had
# already expired and the model looked fresh again -- a cooldown that expires
# before the next real retry even happens protects nothing.
# --------------------------------------------------------------------------- #

def test_the_cooldown_constant_is_longer_than_a_realistic_chain_attempt():
    """Pins the reasoning, not just the number: 180s must comfortably clear the
    150-430s range actually measured, so a regression back toward 60s (or any
    value under the observed range) fails loudly here instead of silently."""
    assert app._HOP_COOLDOWN_DEFAULT >= 150


def test_a_429_with_no_retry_after_uses_the_full_cooldown_default(isolated_config, monkeypatch):
    monkeypatch.setattr(app.requests, "post",
                        lambda *a, **kw: _Resp(429, {}))
    model_calls = []
    monkeypatch.setattr(app.quota, "mark_model_throttled",
                        lambda pid, model, secs=None: model_calls.append((pid, model, secs)))
    app._upstream_chat("uncloseai", {"model": "open", "messages": []}, False)
    assert ("uncloseai", "open", app._HOP_COOLDOWN_DEFAULT) in model_calls, model_calls


# --------------------------------------------------------------------------- #
# A raw 5xx status (not a raised exception) never got a cooldown at all -- so a
# hop that answers with an HTTP error, rather than failing to connect, was
# retried on every single request with zero memory of the prior failure.
#
# MEASURED 2026-08-05 (live activity log): g4f-nvidia's mistral-medium-3.5-128b
# hop returned ConnectionError in one request, then HTTP 524 (Cloudflare
# gateway timeout -- the origin really did hang, just behind a proxy that
# turned the hang into a response instead of a dropped socket) in the very
# next request seconds later. Nothing had throttled it in between.
# --------------------------------------------------------------------------- #

def test_a_5xx_response_gets_throttled_so_a_retry_skips_it(monkeypatch):
    throttled = []
    monkeypatch.setattr(app.quota, "mark_throttled",
                        lambda pid, secs=None: throttled.append((pid, secs)))

    def fake_dispatch(pid, payload, stream):
        if pid == "g4f-nvidia":
            return _Resp(524, {})
        return _Resp(200, {"choices": [{"finish_reason": "stop",
                                        "message": {"role": "assistant", "content": "OK"}}]})
    monkeypatch.setattr(app, "_dispatch_chat", fake_dispatch)
    monkeypatch.setattr(app, "_build_chain",
                        lambda *a, **k: [("g4f-nvidia", "mistralai/mistral-medium-3.5-128b"),
                                        ("groq", "llama")])
    monkeypatch.setattr(app, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(app, "_resolve_model",
                        lambda m: ("g4f-nvidia", "mistralai/mistral-medium-3.5-128b"))
    client = app.app.test_client()
    r = client.post("/v1/chat/completions", json={
        "model": "auto", "max_tokens": 24, "stream": False,
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert ("g4f-nvidia", app._HOP_COOLDOWN_DEFAULT) in throttled, \
        "a raw 5xx response must get the same cooldown a Timeout already gets: %r" % throttled


def test_a_429_response_is_not_double_throttled_by_the_5xx_branch(monkeypatch):
    """429 is already handled inside _upstream_chat's own key-rotation/backoff
    once its key pool is exhausted -- the outer non-2xx branch must not ALSO
    fire a second, redundant mark_throttled for it (the new check is strictly
    >= 500, so 429 never enters it)."""
    throttled = []
    monkeypatch.setattr(app.quota, "mark_throttled",
                        lambda pid, secs=None: throttled.append((pid, secs)))

    def fake_dispatch(pid, payload, stream):
        if pid == "openrouter":
            return _Resp(429, {})
        return _Resp(200, {"choices": [{"finish_reason": "stop",
                                        "message": {"role": "assistant", "content": "OK"}}]})
    monkeypatch.setattr(app, "_dispatch_chat", fake_dispatch)
    monkeypatch.setattr(app, "_build_chain",
                        lambda *a, **k: [("openrouter", "some-model"), ("groq", "llama")])
    monkeypatch.setattr(app, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(app, "_resolve_model", lambda m: ("openrouter", "some-model"))
    client = app.app.test_client()
    r = client.post("/v1/chat/completions", json={
        "model": "auto", "max_tokens": 24, "stream": False,
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert not throttled, "429 must stay owned by _upstream_chat, not double-throttled here: %r" % throttled


def test_a_429_with_a_real_retry_after_still_honors_it_exactly(isolated_config, monkeypatch):
    """The reasoned default must only fill in when the provider gives nothing
    to go on -- an explicit Retry-After is real information and must win."""
    resp = _Resp(429, {})
    resp.headers = {"Retry-After": "17"}
    monkeypatch.setattr(app.requests, "post", lambda *a, **kw: resp)
    model_calls = []
    monkeypatch.setattr(app.quota, "mark_model_throttled",
                        lambda pid, model, secs=None: model_calls.append((pid, model, secs)))
    app._upstream_chat("uncloseai", {"model": "open", "messages": []}, False)
    assert ("uncloseai", "open", 17.0) in model_calls, model_calls
