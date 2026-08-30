"""Max and Swarm have to work on the protocol each CLI actually speaks.

MEASURED 2026-08-30, live, after the CLI argv finally carried the mode: a real
`codex exec --model swarm` turn reached the hub as model_req="swarm" -- and the
hub's own activity row for it read

    ['groq/swarm ! groq: HTTP 404', 'google/models/gemini-3.7-flash']

It tried to send a model LITERALLY NAMED "swarm" to Groq, 404'd, and answered
from the fallback chain. The swarm never ran.

Root cause: the swarm dispatch and the `best` quality_mode both lived only in
/v1/chat/completions. But that is not the endpoint the CLIs use --

    codex     -> /v1/responses   (wire_api = "responses")
    claude    -> /v1/messages    (Anthropic)
    opencode  -> /v1/chat/completions

-- so the one CLI the modes worked for was the one nobody was using them from.
"swarm" fell through to _resolve_model(), which treats an unknown bare id as a
literal model name on the default provider.

Both endpoints now recognise both modes. The swarm fan-out itself is protocol
independent (it returns an ordinary chat completion), so each endpoint runs the
same fan-out and translates the winner into its own shape -- streaming included,
by replaying the finished answer through the SSE translator each endpoint
already has.
"""
import json
from unittest import mock

import app


def _client():
    app.app.config["TESTING"] = True
    return app.app.test_client()


def _winner(text="done", tool=False):
    """What the fan-out hands back: an ordinary OpenAI chat completion."""
    msg = {"role": "assistant", "content": text}
    if tool:
        msg["tool_calls"] = [{"id": "c1", "type": "function",
                              "function": {"name": "shell", "arguments": '{"cmd":"ls"}'}}]
    data = {"id": "chatcmpl-x", "object": "chat.completion", "created": 1,
            "model": "nvidia/moonshotai/kimi-k3",
            "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}]}
    return data, {"X-Free-LLM-Hub-Provider": "nvidia"}


# --------------------------------------------------------------------------- #
# The fan-out is reusable, not welded to one endpoint
# --------------------------------------------------------------------------- #

def test_the_fanout_is_separable_from_the_chat_endpoint():
    """_swarm_tool_result returns the winning completion; _swarm_tool_turn is
    only the chat-completions wrapper around it. Without this split the other
    two protocols would each need their own copy of the race."""
    assert callable(app._swarm_tool_result)
    with mock.patch.object(app, "_swarm_tool_result", return_value=_winner()),             app.app.app_context():          # jsonify needs one
        out = app._swarm_tool_turn({"messages": [{"role": "user", "content": "hi"}],
                                    "tools": [{"type": "function"}]})
    assert out is not None


def test_a_finished_answer_replays_as_the_sse_lines_a_stream_would_have_sent():
    """This is what lets the existing per-protocol SSE translators consume a
    swarm result: they read raw `data: {...}` byte lines from an upstream."""
    data, _ = _winner("hello")
    lines = list(app._swarm_sse_lines(data))
    assert all(isinstance(b, bytes) for b in lines)
    payloads = [json.loads(b[len(b"data:"):]) for b in lines
                if b.startswith(b"data:") and b"[DONE]" not in b]
    assert payloads, lines
    assert "hello" in json.dumps(payloads)
    assert any(b"[DONE]" in b for b in lines), "no terminator"


# --------------------------------------------------------------------------- #
# /v1/responses  (codex)
# --------------------------------------------------------------------------- #

def test_responses_runs_the_swarm_instead_of_looking_for_a_model_called_swarm():
    """The exact live failure: 'groq/swarm ! groq: HTTP 404'."""
    with mock.patch.object(app, "_swarm_tool_result", return_value=_winner("built it")) as fan, \
            mock.patch.object(app, "_dispatch_chat",
                              side_effect=AssertionError("routed instead of swarming")):
        r = _client().post("/v1/responses", json={
            "model": "swarm", "stream": False, "input": "build it"})
    assert r.status_code == 200, r.get_data(as_text=True)[:400]
    assert fan.called
    assert "built it" in r.get_data(as_text=True)


def test_responses_swarm_answer_is_in_the_responses_shape():
    with mock.patch.object(app, "_swarm_tool_result", return_value=_winner("ok")):
        r = _client().post("/v1/responses", json={
            "model": "swarm", "stream": False, "input": "hi"})
    body = r.get_json()
    assert body.get("object") == "response"
    assert isinstance(body.get("output"), list) and body["output"]


def test_responses_swarm_streams_as_responses_events():
    with mock.patch.object(app, "_swarm_tool_result", return_value=_winner("streamed")):
        r = _client().post("/v1/responses", json={
            "model": "swarm", "stream": True, "input": "hi"})
    text = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "response.created" in text and "response.completed" in text
    assert "streamed" in text


def test_responses_swarm_keeps_tool_calls():
    """A CLI turn that loses its tool call writes no files -- the whole reason
    the prose pipeline cannot serve an agent."""
    with mock.patch.object(app, "_swarm_tool_result", return_value=_winner("", tool=True)):
        r = _client().post("/v1/responses", json={
            "model": "swarm", "stream": True, "input": "hi"})
    assert "function_call" in r.get_data(as_text=True)


def test_responses_max_quality_routes_at_the_top_tier():
    seen = {}

    def _router(messages, max_tokens=None, est=0, require_tools=False, **kw):
        seen.update(kw)
        return ("groq", "llama-3.3-70b-versatile", "hard")

    with mock.patch.object(app, "_route_by_difficulty", _router), \
            mock.patch.object(app, "_check_provider_ready", lambda pid: None), \
            mock.patch.object(app, "_build_chain",
                              lambda *a, **k: [("groq", "llama-3.3-70b-versatile")]), \
            mock.patch.object(app, "_dispatch_chat",
                              lambda pid, payload, stream: _FakeOK()):
        _client().post("/v1/responses", json={
            "model": "best", "stream": False, "input": "hi"})
    assert seen.get("quality_mode") is True, seen


def test_responses_falls_back_to_best_when_the_swarm_cannot_answer():
    """Never back to a literal model named 'swarm'. A swarm request that no
    model could serve is still a request for maximum effort."""
    seen = {}

    def _router(messages, max_tokens=None, est=0, require_tools=False, **kw):
        seen.update(kw)
        return ("groq", "llama-3.3-70b-versatile", "hard")

    with mock.patch.object(app, "_swarm_tool_result", return_value=None), \
            mock.patch.object(app, "_route_by_difficulty", _router), \
            mock.patch.object(app, "_check_provider_ready", lambda pid: None), \
            mock.patch.object(app, "_build_chain",
                              lambda *a, **k: [("groq", "llama-3.3-70b-versatile")]), \
            mock.patch.object(app, "_dispatch_chat",
                              lambda pid, payload, stream: _FakeOK()):
        r = _client().post("/v1/responses", json={
            "model": "swarm", "stream": False, "input": "hi"})
    assert r.status_code == 200
    assert seen.get("quality_mode") is True, seen


# --------------------------------------------------------------------------- #
# /v1/messages  (claude)
# --------------------------------------------------------------------------- #

def test_messages_runs_the_swarm():
    with mock.patch.object(app, "_swarm_tool_result", return_value=_winner("claude built it")) as fan, \
            mock.patch.object(app, "_dispatch_chat",
                              side_effect=AssertionError("routed instead of swarming")):
        r = _client().post("/v1/messages", json={
            "model": "swarm", "max_tokens": 64, "stream": False,
            "messages": [{"role": "user", "content": "build it"}]})
    assert r.status_code == 200, r.get_data(as_text=True)[:400]
    assert fan.called
    assert "claude built it" in r.get_data(as_text=True)


def test_messages_swarm_answer_is_in_the_anthropic_shape():
    with mock.patch.object(app, "_swarm_tool_result", return_value=_winner("ok")):
        r = _client().post("/v1/messages", json={
            "model": "swarm", "max_tokens": 64, "stream": False,
            "messages": [{"role": "user", "content": "hi"}]})
    body = r.get_json()
    assert body.get("type") == "message"
    assert body.get("role") == "assistant"
    assert isinstance(body.get("content"), list) and body["content"]


def test_messages_swarm_streams_as_anthropic_events():
    with mock.patch.object(app, "_swarm_tool_result", return_value=_winner("streamed")):
        r = _client().post("/v1/messages", json={
            "model": "swarm", "max_tokens": 64, "stream": True,
            "messages": [{"role": "user", "content": "hi"}]})
    text = r.get_data(as_text=True)
    assert "message_start" in text and "message_stop" in text
    assert "streamed" in text


def test_messages_max_quality_routes_at_the_top_tier():
    seen = {}

    def _router(messages, max_tokens=None, est=0, require_tools=False, **kw):
        seen.update(kw)
        return ("groq", "llama-3.3-70b-versatile", "hard")

    with mock.patch.object(app, "_route_by_difficulty", _router), \
            mock.patch.object(app, "_check_provider_ready", lambda pid: None), \
            mock.patch.object(app, "_build_chain",
                              lambda *a, **k: [("groq", "llama-3.3-70b-versatile")]), \
            mock.patch.object(app, "_dispatch_chat",
                              lambda pid, payload, stream: _FakeOK()):
        _client().post("/v1/messages", json={
            "model": "best", "max_tokens": 64, "stream": False,
            "messages": [{"role": "user", "content": "hi"}]})
    assert seen.get("quality_mode") is True, seen


# --------------------------------------------------------------------------- #
# Nothing about the ordinary path moves
# --------------------------------------------------------------------------- #

def test_auto_is_untouched_on_both_endpoints():
    seen = []

    def _router(messages, max_tokens=None, est=0, require_tools=False, **kw):
        seen.append(kw)
        return ("groq", "llama-3.3-70b-versatile", "medium")

    with mock.patch.object(app, "_route_by_difficulty", _router), \
            mock.patch.object(app, "_check_provider_ready", lambda pid: None), \
            mock.patch.object(app, "_build_chain",
                              lambda *a, **k: [("groq", "llama-3.3-70b-versatile")]), \
            mock.patch.object(app, "_dispatch_chat",
                              lambda pid, payload, stream: _FakeOK()):
        c = _client()
        c.post("/v1/responses", json={"model": "auto", "stream": False, "input": "hi"})
        c.post("/v1/messages", json={"model": "auto", "max_tokens": 8, "stream": False,
                                     "messages": [{"role": "user", "content": "hi"}]})
    assert seen and all(not kw.get("quality_mode") for kw in seen), seen


class _FakeOK:
    """Minimal stand-in for a successful upstream non-streaming reply."""
    status_code = 200
    headers = {}

    def json(self):
        return {"id": "x", "object": "chat.completion", "created": 1,
                "model": "llama-3.3-70b-versatile",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "hi there"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    def close(self):
        pass
