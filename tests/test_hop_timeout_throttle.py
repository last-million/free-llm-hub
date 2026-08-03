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
import requests

import app


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
    client = app.app.test_client()
    r = client.post("/v1/chat/completions", json={
        "model": "auto", "max_tokens": 24, "stream": False,
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert r.get_json()["choices"][0]["message"]["content"] == "OK"
    assert ("nvidia", 60) in throttled, \
        "a timed-out hop must get the same short cooldown a 429 already gets: %r" % throttled


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
    client = app.app.test_client()
    r = client.post("/v1/chat/completions", json={
        "model": "auto", "max_tokens": 24, "stream": False,
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert not throttled, "a non-timeout RequestException must not throttle: %r" % throttled
