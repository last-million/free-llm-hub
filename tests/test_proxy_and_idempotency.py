"""The last two gaps: an explicit outbound proxy, and idempotency keys.

Gaps 12 and 13 from the freellmapi comparison. Both small, both closing a way to
lose a request or a diagnosis.

PROXY. requests already honours HTTP_PROXY/HTTPS_PROXY, but only because
`trust_env` defaults to True -- which also means it silently picks up whatever a
corporate machine, a VPN client or a leftover shell export happens to have set.
That failure is miserable to diagnose: every provider times out at once, with
nothing to suggest the traffic is going somewhere else entirely. Stating it
explicitly makes the setting visible in /api/status and stops it being inherited
by accident.

IDEMPOTENCY. A client that retries a POST it never saw the answer to -- a
dropped connection, a proxy timeout, an SDK's own retry -- spends the free quota
twice for one question. This is deliberately independent of the response cache:
that one is about two people asking the same thing and refuses tool-carrying
turns, while this is about ONE request being sent twice, which is exactly what
agent SDKs do on a network blip.
"""
import json
from unittest import mock

import pytest
from flask import Response

import app as A
import config


@pytest.fixture
def client():
    return A.app.test_client()


@pytest.fixture(autouse=True)
def clean():
    with A._idem_lock:
        A._idem.clear()
    yield
    with A._idem_lock:
        A._idem.clear()


def _body(text="hi", **kw):
    b = {"model": "auto", "messages": [{"role": "user", "content": text}]}
    b.update(kw)
    return b


def _completion(content="hello"):
    return {"id": "chatcmpl-1", "object": "chat.completion", "model": "auto",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}]}


def _buffered(data=None, status=200):
    return lambda body: (A.jsonify(data if data is not None else _completion()), status)


def _sse(*chunks):
    def make(body):
        def gen():
            for c in chunks:
                yield ("data: " + json.dumps(
                    {"choices": [{"delta": {"content": c}, "finish_reason": None}]}) + "\n\n")
            yield "data: [DONE]\n\n"
        return Response(gen(), mimetype="text/event-stream")
    return make


# --------------------------------------------------------------------------- #
# Proxy
# --------------------------------------------------------------------------- #

def test_no_proxy_configured_means_direct():
    with mock.patch.object(A.config, "get_value", return_value=""):
        assert A._proxies() is None


def test_a_configured_proxy_is_used_for_both_schemes():
    with mock.patch.object(A.config, "get_value", return_value="http://127.0.0.1:8080"):
        assert A._proxies() == {"http": "http://127.0.0.1:8080",
                                "https": "http://127.0.0.1:8080"}


def test_whitespace_only_is_treated_as_unset():
    with mock.patch.object(A.config, "get_value", return_value="   "):
        assert A._proxies() is None


def test_the_chat_path_passes_it_explicitly():
    """Explicit, so a stray HTTPS_PROXY in the environment cannot silently
    redirect every provider call."""
    src = open("app.py", encoding="utf-8").read()
    body = src.split("def _upstream_chat(", 1)[1]
    body = body[:body.index("\ndef ")]      # the whole function, not a guessed window
    assert "proxies=_proxies()" in body


def test_the_non_chat_path_passes_it_too():
    src = open("app.py", encoding="utf-8").read()
    body = src.split("def _upstream_post(", 1)[1]
    body = body[:body.index("\ndef ")]
    assert "proxies=_proxies()" in body


def test_the_status_reports_it():
    """Every provider timing out at once is the symptom; the proxy being set is
    the cause, and it has to be visible to connect the two."""
    src = open("app.py", encoding="utf-8").read()
    assert '"outbound_proxy"' in src


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #

def test_a_repeated_key_replays_the_first_answer(client):
    h = {"Idempotency-Key": "abc-123"}
    with mock.patch.object(A, "_chat_completions_uncached", side_effect=_buffered()) as up:
        first = client.post("/v1/chat/completions", json=_body(), headers=h)
        second = client.post("/v1/chat/completions", json=_body(), headers=h)
    assert up.call_count == 1
    assert second.headers.get("X-Free-LLM-Hub-Idempotent") == "replayed"
    assert first.get_json() == second.get_json()


def test_a_different_key_is_a_different_request(client):
    with mock.patch.object(A, "_chat_completions_uncached", side_effect=_buffered()) as up:
        client.post("/v1/chat/completions", json=_body(), headers={"Idempotency-Key": "a"})
        client.post("/v1/chat/completions", json=_body(), headers={"Idempotency-Key": "b"})
    assert up.call_count == 2


def test_without_a_key_nothing_changes(client):
    with mock.patch.object(A, "_chat_completions_uncached", side_effect=_buffered()) as up:
        for _ in range(2):
            client.post("/v1/chat/completions", json=_body())
    assert up.call_count == 2


def test_it_covers_tool_turns_that_the_cache_refuses(client):
    """The distinction between the two features: an agent SDK retrying a
    tool-carrying turn on a network blip is exactly the case this is for."""
    h = {"Idempotency-Key": "tool-1"}
    tools = [{"type": "function", "function": {"name": "f"}}]
    with mock.patch.object(A, "_chat_completions_uncached", side_effect=_buffered()) as up:
        client.post("/v1/chat/completions", json=_body(tools=tools), headers=h)
        client.post("/v1/chat/completions", json=_body(tools=tools), headers=h)
    assert up.call_count == 1


def test_a_failed_turn_is_not_held(client):
    """Retrying after an error must actually retry."""
    h = {"Idempotency-Key": "err-1"}
    with mock.patch.object(A, "_chat_completions_uncached",
                           side_effect=_buffered({"error": {"message": "no"}}, 503)) as up:
        client.post("/v1/chat/completions", json=_body(), headers=h)
        client.post("/v1/chat/completions", json=_body(), headers=h)
    assert up.call_count == 2


def test_a_streamed_answer_is_not_held(client):
    """Replaying one would mean buffering it here, and a client streaming a turn
    is watching it arrive -- the case this covers is a client that got nothing."""
    h = {"Idempotency-Key": "stream-1"}
    with mock.patch.object(A, "_chat_completions_uncached", side_effect=_sse("hi")) as up:
        client.post("/v1/chat/completions", json=_body(stream=True), headers=h).get_data()
        client.post("/v1/chat/completions", json=_body(stream=True), headers=h).get_data()
    assert up.call_count == 2


def test_entries_expire():
    A._idem["k"] = (A.time.time() - A._IDEM_TTL - 1, {"x": 1})
    assert A._idem_get("k") is None


def test_the_store_is_bounded():
    with A.app.test_request_context():          # jsonify needs an app context
        for i in range(A._IDEM_MAX * 2):
            A._idem_store("k%d" % i, (A.jsonify(_completion()), 200))
    with A._idem_lock:
        assert len(A._idem) <= A._IDEM_MAX


def test_an_absurdly_long_key_is_truncated_not_rejected(client):
    """It comes off the wire; it must not be able to bloat the store."""
    src = open("app.py", encoding="utf-8").read()
    assert '(request.headers.get("Idempotency-Key") or "").strip()[:200]' in src


def test_it_works_whether_or_not_the_cache_is_on(client):
    """Two independent features on the same seam; neither may depend on the
    other being enabled."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index('idem = (request.headers.get("Idempotency-Key")')
    j = src.index('config.get_flag("response_cache", False)', i)
    assert i < j, "the idempotency check must run before the cache flag is read"
