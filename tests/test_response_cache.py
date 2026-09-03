"""An opt-in cache of completions, so a repeat costs no free quota.

Fifth gap from the freellmapi comparison ("opt-in response cache"). The scarce
resource on this hub is not latency, it is other people's free tiers: every
answer served from memory is a request that never comes off a daily allowance.
And the things that repeat are the expensive ones -- a /build turn re-run after
a stream dropped, an agent retrying after a tool error, the same prompt tried
against auto and then best, a page reloaded mid-generation.

OFF BY DEFAULT, deliberately. A cache turns "ask again" into "the same answer",
which is right for a retry and wrong for someone pressing regenerate hoping for
better. Nothing in the request distinguishes those two, so the choice belongs to
whoever runs the hub rather than to a heuristic.

The refusals are the interesting part, and they live in one place so that no
surface can forget them -- especially tool-carrying turns: an agent loop that
repeats a request byte for byte is already stuck, and replaying the identical
tool call would make the hub a participant in the loop instead of a witness.
"""
import json
from unittest import mock

import pytest
from flask import Response

import app as A
import respcache


@pytest.fixture
def client():
    return A.app.test_client()


@pytest.fixture(autouse=True)
def clean():
    respcache.clear()
    yield
    respcache.clear()


@pytest.fixture
def cache_on():
    with mock.patch.object(A.config, "get_flag",
                           side_effect=lambda k, d=None: True if k == "response_cache" else d):
        yield


def _body(text="hi", **kw):
    b = {"model": "auto", "messages": [{"role": "user", "content": text}]}
    b.update(kw)
    return b


def _completion(content="hello"):
    return {"id": "chatcmpl-1", "object": "chat.completion", "model": "auto",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3}}


def _buffered(data=None):
    return lambda body: (A.jsonify(data if data is not None else _completion()), 200)


def _sse(*chunks):
    def make(body):
        def gen():
            for c in chunks:
                yield ("data: " + json.dumps(
                    {"id": "chatcmpl-s", "model": "auto",
                     "choices": [{"delta": {"content": c}, "finish_reason": None}]})
                    + "\n\n")
            yield ("data: " + json.dumps(
                {"choices": [{"delta": {}, "finish_reason": "stop"}],
                 "usage": {"completion_tokens": len(chunks)}}) + "\n\n")
            yield "data: [DONE]\n\n"
        return Response(gen(), mimetype="text/event-stream")
    return make


# --------------------------------------------------------------------------- #
# What may be cached
# --------------------------------------------------------------------------- #

def test_an_ordinary_request_is_cacheable():
    assert respcache.cacheable(_body())


def test_a_tool_carrying_turn_is_never_cached():
    """An agent loop that repeats a request byte for byte is already stuck.
    Replaying the identical tool call would make the hub part of the loop."""
    assert not respcache.cacheable(_body(tools=[{"type": "function"}]))


def test_an_explicit_seed_or_n_is_never_cached():
    """Both are statements about sampling; answering from memory ignores them."""
    assert not respcache.cacheable(_body(seed=7))
    assert not respcache.cacheable(_body(n=3))


def test_the_pipeline_models_are_never_cached():
    """swarm/crew/team/plan are a PIPELINE whose value is that it ran."""
    for m in ("swarm", "crew", "team", "plan"):
        assert not respcache.cacheable(_body(model=m)), m


def test_an_empty_request_is_not_cached():
    assert not respcache.cacheable({"model": "auto", "messages": []})


# --------------------------------------------------------------------------- #
# The key
# --------------------------------------------------------------------------- #

def test_the_same_question_hashes_the_same():
    assert respcache.key_for(_body()) == respcache.key_for(_body())


def test_different_text_hashes_differently():
    assert respcache.key_for(_body("a")) != respcache.key_for(_body("b"))


def test_streaming_does_not_change_the_key():
    """A streamed and a buffered request for the same thing deserve the same
    answer -- and the commonest hit of all is a re-run of a dropped stream."""
    assert respcache.key_for(_body()) == respcache.key_for(_body(stream=True))


def test_the_model_is_part_of_the_key():
    """'auto' and a pinned id are different questions."""
    assert respcache.key_for(_body()) != respcache.key_for(_body(model="chutes/glm"))


def test_sampling_settings_are_part_of_the_key():
    assert respcache.key_for(_body()) != respcache.key_for(_body(temperature=0.9))
    assert respcache.key_for(_body()) != respcache.key_for(_body(max_tokens=10))


# --------------------------------------------------------------------------- #
# Storing and expiry
# --------------------------------------------------------------------------- #

def test_a_stored_answer_comes_back():
    assert respcache.put(_body(), _completion())
    assert respcache.get(_body())["choices"][0]["message"]["content"] == "hello"


def test_an_error_is_never_stored():
    assert not respcache.put(_body(), {"error": {"message": "nope"}})


def test_an_empty_answer_is_never_stored():
    assert not respcache.put(_body(), _completion(""))


def test_an_answer_carrying_tool_calls_is_never_stored():
    data = _completion("")
    data["choices"][0]["message"]["tool_calls"] = [{"id": "a"}]
    assert not respcache.put(_body(), data)


def test_entries_expire():
    respcache.put(_body(), _completion())
    assert respcache.get(_body(), ttl=3600) is not None
    assert respcache.get(_body(), ttl=0) is None


def test_the_cache_is_bounded():
    for i in range(30):
        respcache.put(_body("q%d" % i), _completion(), max_entries=10)
    assert respcache.stats()["entries"] <= 10


def test_a_caller_cannot_mutate_what_is_stored():
    """get() hands out a copy; the surfaces rewrite `model` on the way out."""
    respcache.put(_body(), _completion())
    got = respcache.get(_body())
    got["choices"][0]["message"]["content"] = "tampered"
    assert respcache.get(_body())["choices"][0]["message"]["content"] == "hello"


# --------------------------------------------------------------------------- #
# Through the hub
# --------------------------------------------------------------------------- #

def test_the_cache_is_off_unless_asked_for(client):
    with mock.patch.object(A, "_chat_completions_uncached", side_effect=_buffered()) as up:
        for _ in range(2):
            client.post("/v1/chat/completions", json=_body())
    assert up.call_count == 2
    assert respcache.stats()["entries"] == 0


def test_a_repeat_is_served_from_memory(client, cache_on):
    with mock.patch.object(A, "_chat_completions_uncached", side_effect=_buffered()) as up:
        first = client.post("/v1/chat/completions", json=_body())
        second = client.post("/v1/chat/completions", json=_body())
    assert up.call_count == 1                       # the model ran once
    assert second.headers.get("X-Free-LLM-Hub-Cache") == "hit"
    assert first.get_json()["choices"][0]["message"]["content"] == \
        second.get_json()["choices"][0]["message"]["content"]


def test_a_different_question_still_reaches_the_model(client, cache_on):
    with mock.patch.object(A, "_chat_completions_uncached", side_effect=_buffered()) as up:
        client.post("/v1/chat/completions", json=_body("a"))
        client.post("/v1/chat/completions", json=_body("b"))
    assert up.call_count == 2


def test_a_client_can_bypass_it_for_one_request(client, cache_on):
    with mock.patch.object(A, "_chat_completions_uncached", side_effect=_buffered()) as up:
        client.post("/v1/chat/completions", json=_body())
        client.post("/v1/chat/completions", json=_body(),
                    headers={"X-Free-LLM-Hub-Cache": "bypass"})
    assert up.call_count == 2


def test_a_bypassed_request_still_refreshes_the_stored_answer(client, cache_on):
    with mock.patch.object(A, "_chat_completions_uncached",
                           side_effect=_buffered(_completion("fresh"))):
        client.post("/v1/chat/completions", json=_body(),
                    headers={"X-Free-LLM-Hub-Cache": "bypass"})
    assert respcache.get(_body())["choices"][0]["message"]["content"] == "fresh"


def test_a_failed_turn_is_not_remembered(client, cache_on):
    err = lambda body: (A.jsonify({"error": {"message": "no models"}}), 503)
    with mock.patch.object(A, "_chat_completions_uncached", side_effect=err) as up:
        client.post("/v1/chat/completions", json=_body())
        client.post("/v1/chat/completions", json=_body())
    assert up.call_count == 2


def test_a_tool_turn_is_not_cached_through_the_hub(client, cache_on):
    with mock.patch.object(A, "_chat_completions_uncached", side_effect=_buffered()) as up:
        for _ in range(2):
            client.post("/v1/chat/completions",
                        json=_body(tools=[{"type": "function",
                                           "function": {"name": "f"}}]))
    assert up.call_count == 2


# --------------------------------------------------------------------------- #
# Streaming, which is what actually needs caching
# --------------------------------------------------------------------------- #

def test_a_streamed_answer_reaches_the_client_unchanged_and_is_stored(client, cache_on):
    with mock.patch.object(A, "_chat_completions_uncached", side_effect=_sse("he", "llo")):
        r = client.post("/v1/chat/completions", json=_body(stream=True))
    body = r.get_data(as_text=True)
    assert "[DONE]" in body
    texts = [json.loads(l[5:])["choices"][0]["delta"].get("content", "")
             for l in body.splitlines() if l.startswith("data: ") and "[DONE]" not in l]
    assert "".join(texts) == "hello"
    assert respcache.get(_body())["choices"][0]["message"]["content"] == "hello"


def test_a_cached_answer_can_come_back_as_a_stream(client, cache_on):
    """The commonest hit of all is the re-run of a turn whose stream dropped,
    and that client is still a streaming client."""
    with mock.patch.object(A, "_chat_completions_uncached", side_effect=_sse("hi")):
        # The body must be READ: the answer is reassembled as it passes through
        # to the client, so a stream nobody consumed is never stored -- which is
        # the behaviour we want when a client disconnects mid-turn.
        client.post("/v1/chat/completions", json=_body(stream=True)).get_data()
    with mock.patch.object(A, "_chat_completions_uncached", side_effect=_sse("SHOULD NOT RUN")) as up:
        r = client.post("/v1/chat/completions", json=_body(stream=True))
    assert not up.called
    assert r.mimetype == "text/event-stream"
    assert r.headers.get("X-Free-LLM-Hub-Cache") == "hit"
    assert "hi" in r.get_data(as_text=True)
    assert r.get_data(as_text=True).rstrip().endswith("data: [DONE]")


def test_a_stream_that_produced_nothing_is_not_stored(client, cache_on):
    with mock.patch.object(A, "_chat_completions_uncached", side_effect=_sse()):
        client.post("/v1/chat/completions", json=_body(stream=True)).get_data()
    assert respcache.get(_body()) is None


def test_a_stream_the_client_abandoned_is_not_stored(client, cache_on):
    """It is reassembled as it passes through, so half an answer is never
    mistaken for a whole one."""
    with mock.patch.object(A, "_chat_completions_uncached", side_effect=_sse("he", "llo")):
        client.post("/v1/chat/completions", json=_body(stream=True))  # body unread
    assert respcache.get(_body()) is None


def test_a_buffered_hit_can_answer_a_streamed_repeat(client, cache_on):
    with mock.patch.object(A, "_chat_completions_uncached", side_effect=_buffered()):
        client.post("/v1/chat/completions", json=_body())
    with mock.patch.object(A, "_chat_completions_uncached", side_effect=_buffered()) as up:
        r = client.post("/v1/chat/completions", json=_body(stream=True))
    assert not up.called
    assert r.mimetype == "text/event-stream"
    assert "hello" in r.get_data(as_text=True)


# --------------------------------------------------------------------------- #
# ...and every other surface inherits it
# --------------------------------------------------------------------------- #

def test_the_gemini_surface_is_cached_too(client, cache_on):
    """The cache wraps the shared seam, so a surface cannot forget to use it."""
    payload = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}
    with mock.patch.object(A, "_chat_completions_uncached", side_effect=_buffered()) as up:
        for _ in range(2):
            client.post("/v1beta/models/auto:generateContent", json=payload)
    assert up.call_count == 1


# --------------------------------------------------------------------------- #
# Its switch
# --------------------------------------------------------------------------- #

def _ctl():
    import config as C
    return {"X-Free-LLM-Hub-Token": C.ensure_control_token(),
            "X-Free-LLM-Hub": "dashboard"}


@pytest.fixture
def flag():
    import config as C
    before = C.get_flag("response_cache", False)
    yield
    C.set_flag("response_cache", before)


def test_the_switch_reports_and_changes_the_state(client, flag):
    import config as C
    off = client.post("/api/response-cache", headers=_ctl(),
                      json={"enabled": False}).get_json()
    assert off["enabled"] is False and C.get_flag("response_cache") is False
    on = client.post("/api/response-cache", headers=_ctl(),
                     json={"enabled": True}).get_json()
    assert on["enabled"] is True and C.get_flag("response_cache") is True


def test_the_switch_reports_the_counters(client, flag):
    respcache.put(_body(), _completion())
    st = client.get("/api/response-cache", headers=_ctl()).get_json()
    assert st["entries"] == 1 and "hits" in st and "ttl" in st


def test_the_cache_can_be_emptied(client, flag):
    respcache.put(_body(), _completion())
    st = client.post("/api/response-cache", headers=_ctl(),
                     json={"clear": True}).get_json()
    assert st["entries"] == 0


def test_the_ttl_is_configurable_and_floored(client, flag):
    """A one-second TTL is indistinguishable from no cache and would just add a
    lookup to every request."""
    st = client.post("/api/response-cache", headers=_ctl(), json={"ttl": 1}).get_json()
    assert st["ttl"] >= 30


def test_a_bad_value_is_refused(client, flag):
    assert client.post("/api/response-cache", headers=_ctl(),
                       json={"enabled": "yes"}).status_code == 400
    assert client.post("/api/response-cache", headers=_ctl(),
                       json={"ttl": "soon"}).status_code == 400


def test_the_switch_is_control_gated(client):
    assert client.get("/api/response-cache").status_code == 401
