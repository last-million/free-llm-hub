"""The new surfaces, end to end through Flask.

The translation itself is tested in test_foreign_wire_formats.py. What is tested
here is the plumbing around it, which is where the genuinely dangerous mistakes
are:

  * the Ollama surface lives under /api/, which is otherwise the DASHBOARD
    control API behind a per-install token and an anti-CSRF header. Carving it
    out with a PREFIX instead of exact paths would have dragged
    "/api/chat/history" -- a real control endpoint -- out of the token gate
    along with "/api/chat". That is the bug this file exists to prevent.
  * every surface delegates to the one _chat_completions seam, so routing,
    failover, swarm escalation and quota accounting cannot drift apart.
  * Ollama streams by default and Gemini streams a JSON array unless ?alt=sse.
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


def _ctl():
    return {"X-Free-LLM-Hub-Token": config.ensure_control_token(),
            "X-Free-LLM-Hub": "dashboard"}


def _completion(content="hello", finish="stop"):
    return {"id": "x", "model": "auto",
            "choices": [{"message": {"role": "assistant", "content": content},
                         "finish_reason": finish}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}}


def _buffered(data=None, status=200):
    return lambda body: (A.jsonify(data if data is not None else _completion()), status)


def _sse(*chunks):
    """A streaming OpenAI response, exactly as /v1/chat/completions emits one."""
    def make(body):
        def gen():
            for c in chunks:
                yield "data: " + json.dumps(
                    {"choices": [{"delta": {"content": c}, "finish_reason": None}]}) + "\n\n"
            yield ("data: " + json.dumps(
                {"choices": [{"delta": {}, "finish_reason": "stop"}],
                 "usage": {"completion_tokens": len(chunks)}}) + "\n\n")
            yield "data: [DONE]\n\n"
        return Response(gen(), mimetype="text/event-stream")
    return make


@pytest.fixture
def ollama_on():
    with mock.patch.object(A.config, "get_flag",
                           side_effect=lambda k, d=None: True if k == "ollama_api" else d):
        yield


# --------------------------------------------------------------------------- #
# The gate. This is the part that must not regress.
# --------------------------------------------------------------------------- #

def test_the_ollama_surface_is_off_by_default(client):
    """An extra unauthenticated-by-default shape on the control port has to be
    switched on deliberately, not shipped on."""
    assert client.get("/api/tags").status_code == 404


def test_our_own_control_endpoint_keeps_its_token_gate(client, ollama_on):
    """"/api/chat" must not drag "/api/chat/history" out of the token gate. A
    prefix carve-out would have done exactly that, silently."""
    assert client.get("/api/chat/history").status_code == 401


def test_the_other_control_endpoints_are_untouched(client, ollama_on):
    for path in ("/api/status", "/api/providers", "/api/models"):
        assert client.get(path).status_code == 401, path


def test_an_enabled_ollama_path_needs_no_control_token(client, ollama_on):
    """No Ollama client can send one -- the protocol has no auth at all."""
    r = client.get("/api/tags")
    assert r.status_code == 200


def test_an_enabled_ollama_post_needs_no_csrf_header(client, ollama_on):
    r = client.post("/api/show", json={"model": "auto"})
    assert r.status_code == 200


def test_the_gateway_key_still_applies_when_one_is_set(client, ollama_on):
    with mock.patch.object(A.config, "get_local_api_key", return_value="secret"):
        assert client.get("/api/tags").status_code == 401
        ok = client.get("/api/tags", headers={"Authorization": "Bearer secret"})
        assert ok.status_code == 200


def test_gemini_clients_may_authenticate_googles_way(client):
    """google-genai and Gemini CLI send x-goog-api-key or ?key=, never a bearer."""
    with mock.patch.object(A.config, "get_local_api_key", return_value="secret"):
        assert client.get("/v1beta/models").status_code == 401
        assert client.get("/v1beta/models",
                          headers={"x-goog-api-key": "secret"}).status_code == 200
        assert client.get("/v1beta/models?key=secret").status_code == 200


def test_a_gemini_auth_failure_uses_googles_error_envelope(client):
    with mock.patch.object(A.config, "get_local_api_key", return_value="secret"):
        body = client.get("/v1beta/models").get_json()
    assert body["error"]["status"] == "UNAUTHENTICATED"


# --------------------------------------------------------------------------- #
# Ollama
# --------------------------------------------------------------------------- #

def test_tags_lists_auto_first(client, ollama_on):
    names = [m["name"] for m in client.get("/api/tags").get_json()["models"]]
    assert names[0] == "auto:latest"


def test_version_answers_an_ollama_client_with_an_ollama_version(client, ollama_on):
    """/api/version was ALREADY the hub's own version endpoint. Registering a
    second rule on it silently lost -- Flask keeps the first -- so the one route
    now answers whichever caller asked, told apart by the control token."""
    v = client.get("/api/version").get_json()["version"]
    assert tuple(int(x) for x in v.split(".")) >= (0, 5, 0)


def test_version_still_answers_the_dashboard_with_the_hub_version(client, ollama_on):
    body = client.get("/api/version", headers=_ctl()).get_json()
    assert body["release"] == A.HUB_RELEASE


def test_version_is_unchanged_while_the_emulation_is_off(client):
    body = client.get("/api/version", headers=_ctl()).get_json()
    assert body["release"] == A.HUB_RELEASE
    assert client.get("/api/version").status_code == 401


def test_ps_reports_nothing_resident(client, ollama_on):
    assert client.get("/api/ps").get_json() == {"models": []}


def test_chat_returns_the_ollama_shape(client, ollama_on):
    with mock.patch.object(A, "_chat_completions", side_effect=_buffered()):
        body = client.post("/api/chat", json={"model": "auto", "stream": False,
                                              "messages": [{"role": "user",
                                                            "content": "hi"}]}).get_json()
    assert body["message"] == {"role": "assistant", "content": "hello"}
    assert body["done"] is True


def test_chat_goes_through_the_shared_router(client, ollama_on):
    """Not a private copy of the routing loop -- that is the whole point."""
    with mock.patch.object(A, "_chat_completions", side_effect=_buffered()) as router:
        client.post("/api/chat", json={"model": "auto", "stream": False,
                                       "messages": [{"role": "user", "content": "hi"}]})
    sent = router.call_args[0][0]
    assert sent["messages"] == [{"role": "user", "content": "hi"}]


def test_chat_streams_by_default(client, ollama_on):
    """Absent "stream" means TRUE in Ollama, the opposite of OpenAI. Backwards,
    and every client hangs on first use."""
    with mock.patch.object(A, "_chat_completions", side_effect=_sse("he", "llo")):
        r = client.post("/api/chat", json={"model": "auto",
                                           "messages": [{"role": "user", "content": "hi"}]})
    assert r.mimetype == "application/x-ndjson"
    lines = [json.loads(x) for x in r.get_data(as_text=True).splitlines() if x.strip()]
    assert "".join(l["message"]["content"] for l in lines) == "hello"
    assert lines[-1]["done"] is True


def test_generate_streams_response_not_message(client, ollama_on):
    with mock.patch.object(A, "_chat_completions", side_effect=_sse("hi")):
        r = client.post("/api/generate", json={"model": "auto", "prompt": "x"})
    lines = [json.loads(x) for x in r.get_data(as_text=True).splitlines() if x.strip()]
    assert lines[0]["response"] == "hi"
    assert lines[-1]["done"] is True


def test_an_upstream_error_is_reported_in_ollamas_envelope(client, ollama_on):
    err = _buffered({"error": {"message": "no models available"}}, 503)
    with mock.patch.object(A, "_chat_completions", side_effect=err):
        r = client.post("/api/chat", json={"model": "auto", "stream": False,
                                           "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 503
    assert r.get_json()["error"] == "no models available"


# --------------------------------------------------------------------------- #
# Gemini
# --------------------------------------------------------------------------- #

def test_models_are_listed_with_the_models_prefix(client):
    names = [m["name"] for m in client.get("/v1beta/models").get_json()["models"]]
    assert names[0] == "models/auto"


def test_generate_content_returns_candidates(client):
    with mock.patch.object(A, "_chat_completions", side_effect=_buffered()):
        body = client.post("/v1beta/models/auto:generateContent", json={
            "contents": [{"role": "user", "parts": [{"text": "hi"}]}]}).get_json()
    assert body["candidates"][0]["content"]["parts"][0]["text"] == "hello"
    assert body["candidates"][0]["content"]["role"] == "model"


def test_a_provider_pinned_model_survives_the_slash_in_the_path(client):
    """"models/chutes/glm-5.3:generateContent" -- the id itself contains a slash,
    which is why this is one <path:> rule and not several."""
    with mock.patch.object(A, "_chat_completions", side_effect=_buffered()) as router:
        r = client.post("/v1beta/models/chutes/glm-5.3:generateContent",
                        json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]})
    assert r.status_code == 200
    assert router.call_args[0][0]["model"] == "chutes/glm-5.3"


def test_count_tokens_needs_no_upstream_call(client):
    with mock.patch.object(A, "_chat_completions") as router:
        body = client.post("/v1beta/models/auto:countTokens", json={
            "contents": [{"role": "user", "parts": [{"text": "hello there"}]}]}).get_json()
    assert body["totalTokens"] > 0
    assert not router.called


def test_count_tokens_does_not_report_the_routing_margin():
    """_est_tokens adds a fixed +400 so requests stay UNDER provider TPM and
    context limits -- over-estimating there is free, under-estimating costs a
    413. Reporting it to a CLIENT is a different matter: countTokens said 406
    tokens for a 24-character message, which is the margin, not the text, and
    would make a client think it was near a limit it is nowhere near."""
    msgs = [{"role": "user", "content": "hello world from the hub"}]
    assert A._est_tokens(msgs, None) > 400            # routing keeps its margin
    assert A._est_tokens(msgs, None, overhead=0) < 20  # the caller sees the text


def test_text_shorter_than_a_token_still_counts_as_one(client):
    """chars//4 floors, so "hi" counted as 0 -- and a client reading 0 tokens
    for real text concludes the message was empty."""
    body = client.post("/v1beta/models/auto:countTokens", json={
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}]}).get_json()
    assert body["totalTokens"] >= 1


def test_genuinely_empty_input_still_counts_as_nothing(client):
    body = client.post("/v1beta/models/auto:countTokens",
                       json={"contents": []}).get_json()
    assert body["totalTokens"] == 0


def test_count_tokens_reflects_the_actual_text(client):
    short = client.post("/v1beta/models/auto:countTokens", json={
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}]}).get_json()
    long = client.post("/v1beta/models/auto:countTokens", json={
        "contents": [{"role": "user", "parts": [{"text": "hi " * 500}]}]}).get_json()
    assert short["totalTokens"] < 20
    assert long["totalTokens"] > short["totalTokens"] * 10


def test_count_tokens_counts_the_tools_too(client):
    """Tools dominate an agentic request's size; ignoring them makes the number
    useless for exactly the clients that ask."""
    contents = [{"role": "user", "parts": [{"text": "hi"}]}]
    bare = client.post("/v1beta/models/auto:countTokens",
                       json={"contents": contents}).get_json()
    withtools = client.post("/v1beta/models/auto:countTokens", json={
        "contents": contents,
        "tools": [{"functionDeclarations": [
            {"name": "read_file", "description": "x" * 400,
             "parameters": {"type": "object"}}]}]}).get_json()
    assert withtools["totalTokens"] > bare["totalTokens"]


def test_stream_generate_content_defaults_to_a_json_array(client):
    """The documented shape without ?alt=sse. A client expecting it cannot parse
    SSE frames at all."""
    with mock.patch.object(A, "_chat_completions", side_effect=_sse("he", "llo")):
        r = client.post("/v1beta/models/auto:streamGenerateContent",
                        json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]})
    assert r.mimetype == "application/json"
    arr = json.loads(r.get_data(as_text=True))
    assert isinstance(arr, list)
    text = "".join(c["candidates"][0]["content"]["parts"][0].get("text", "") for c in arr)
    assert text == "hello"


def test_alt_sse_streams_sse_instead(client):
    with mock.patch.object(A, "_chat_completions", side_effect=_sse("hi")):
        r = client.post("/v1beta/models/auto:streamGenerateContent?alt=sse",
                        json={"contents": [{"role": "user", "parts": [{"text": "x"}]}]})
    assert r.mimetype == "text/event-stream"
    assert "data: {" in r.get_data(as_text=True)


def test_an_unknown_method_is_refused_clearly(client):
    r = client.post("/v1beta/models/auto:embedContent", json={"contents": []})
    assert r.status_code == 400
    assert "embedContent" in r.get_json()["error"]["message"]


def test_a_single_model_can_be_fetched(client):
    body = client.get("/v1beta/models/auto").get_json()
    assert body["name"] == "models/auto"


# --------------------------------------------------------------------------- #
# Legacy /v1/completions
# --------------------------------------------------------------------------- #

def test_completions_returns_text_not_a_message(client):
    with mock.patch.object(A, "_chat_completions", side_effect=_buffered()):
        body = client.post("/v1/completions",
                           json={"model": "auto", "prompt": "once"}).get_json()
    assert body["object"] == "text_completion"
    assert body["choices"][0]["text"] == "hello"


def test_the_prompt_becomes_a_user_message(client):
    with mock.patch.object(A, "_chat_completions", side_effect=_buffered()) as router:
        client.post("/v1/completions", json={"model": "auto", "prompt": "once"})
    assert router.call_args[0][0]["messages"] == [{"role": "user", "content": "once"}]


def test_a_string_array_prompt_is_joined(client):
    with mock.patch.object(A, "_chat_completions", side_effect=_buffered()) as router:
        client.post("/v1/completions", json={"prompt": ["a", "b"]})
    assert router.call_args[0][0]["messages"][0]["content"] == "ab"


def test_a_token_array_prompt_is_refused_rather_than_mangled(client):
    r = client.post("/v1/completions", json={"prompt": [[1, 2, 3]]})
    assert r.status_code == 400


def test_a_missing_prompt_is_refused(client):
    assert client.post("/v1/completions", json={"model": "auto"}).status_code == 400


def test_completions_streams_text_deltas(client):
    with mock.patch.object(A, "_chat_completions", side_effect=_sse("he", "llo")):
        r = client.post("/v1/completions",
                        json={"model": "auto", "prompt": "x", "stream": True})
    assert r.mimetype == "text/event-stream"
    body = r.get_data(as_text=True)
    texts = [json.loads(l[5:])["choices"][0]["text"]
             for l in body.splitlines() if l.startswith("data: ") and "[DONE]" not in l]
    assert "".join(texts) == "hello"
    assert body.rstrip().endswith("data: [DONE]")
