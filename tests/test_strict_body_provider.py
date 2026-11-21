"""A strict OpenAI-compatible API that takes only model / messages / stream still works.

REPORTED 2026-11-21: a user's own local bridge was added as a custom provider and the
hub marked its only key dead: "Test: HTTP 400: Unsupported request options." The bridge
accepts ONLY model, messages and stream (messages: role + content only) and answers any
other key with that message -- which names no field, so the hub's drop-the-named-field
retry found nothing to drop, and max_tokens (sent by every Test) is never droppable.

FIX: such a refusal is retried ONCE with the body cut to those keys; the provider is
remembered only when that retry succeeds.
"""
import os
import shutil
import tempfile

import pytest

import app

STRICT_400 = '{"error": {"message": "Unsupported request options.", "type": "invalid_request_error"}}'


@pytest.fixture
def isolated(monkeypatch):
    d = tempfile.mkdtemp(prefix="hub-pytest-strictbody-")
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", os.path.join(d, "state", "config.json"))
    monkeypatch.setattr(app, "_PARAM_REJECTED", {})
    monkeypatch.setattr(app, "_MINIMAL_BODY", set())
    monkeypatch.setattr(app, "_strict_body_on", lambda: True)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


class _Resp:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text
        self.headers = {}
        self.closed = False

    def json(self):
        return {"choices": [{"finish_reason": "stop",
                             "message": {"role": "assistant", "content": "OK"}}]}

    def close(self):
        self.closed = True

    def iter_lines(self, decode_unicode=False):
        return iter(())


def _payload(**extra):
    p = {"model": "gemini-web", "messages": [{"role": "user", "content": "Reply OK"}]}
    p.update(extra)
    return p


def _strict_api(sent, refuse_text=STRICT_400):
    """The bridge: model / messages / stream only, messages role + content only."""
    def post(*a, **kw):
        body = kw.get("json") or {}
        sent.append(body)
        if set(body) - {"model", "messages", "stream"}:
            return _Resp(400, refuse_text)
        if any(set(m) - {"role", "content"} for m in body.get("messages", [])):
            return _Resp(400, refuse_text)
        return _Resp(200)
    return post


# --------------------------------------------------------------------------- #
# The cut
# --------------------------------------------------------------------------- #

def test_the_minimal_body_keeps_only_the_three_keys():
    got = app._minimal_body(_payload(max_tokens=512, temperature=0.2, stream=False,
                                     stream_options={"include_usage": True}, user="u"))
    assert got == {"model": "gemini-web", "stream": False,
                   "messages": [{"role": "user", "content": "Reply OK"}]}


def test_messages_are_cut_to_role_and_content():
    p = _payload(messages=[{"role": "user", "content": "hi", "name": "bob"}], max_tokens=5)
    assert app._minimal_body(p)["messages"] == [{"role": "user", "content": "hi"}]


def test_text_parts_are_kept():
    parts = [{"type": "text", "text": "hi"}]
    got = app._minimal_body(_payload(messages=[{"role": "user", "content": parts}], max_tokens=5))
    assert got["messages"] == [{"role": "user", "content": parts}]


@pytest.mark.parametrize("payload", [
    _payload(max_tokens=5, tools=[{"type": "function", "function": {"name": "bash"}}]),
    _payload(max_tokens=5, tool_choice="auto"),
    _payload(max_tokens=5, functions=[{"name": "f"}]),
    _payload(max_tokens=5, messages=[
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": "out"},
        {"role": "user", "content": "go"}]),
    _payload(max_tokens=5, messages=[{"role": "user", "content": [
        {"type": "text", "text": "see"}, {"type": "image_url", "image_url": {"url": "data:x"}}]}]),
    _payload(max_tokens=5, messages=[{"role": "user", "content": None}]),
    _payload(),                                   # nothing to cut
])
def test_requests_that_would_change_meaning_are_never_cut(payload):
    assert app._minimal_body(payload) is None


# --------------------------------------------------------------------------- #
# The retry
# --------------------------------------------------------------------------- #

def test_a_refusal_naming_no_field_is_retried_with_the_minimal_body(isolated, monkeypatch):
    sent = []
    monkeypatch.setattr(app.requests, "post", _strict_api(sent))
    r = app._upstream_chat("uncloseai", _payload(max_tokens=512, temperature=0.3), False)
    assert r.status_code == 200
    assert len(sent) == 2
    assert "max_tokens" in sent[0] and set(sent[1]) == {"model", "messages"}
    assert "uncloseai" in app._MINIMAL_BODY


def test_once_learned_the_extra_fields_are_never_sent_again(isolated, monkeypatch):
    sent = []
    monkeypatch.setattr(app.requests, "post", _strict_api(sent))
    app._upstream_chat("uncloseai", _payload(max_tokens=512), False)
    sent.clear()
    r = app._upstream_chat("uncloseai", _payload(max_tokens=9, temperature=1, top_p=0.5), False)
    assert r.status_code == 200
    assert len(sent) == 1 and set(sent[0]) == {"model", "messages"}


def test_a_streamed_request_keeps_stream_true(isolated, monkeypatch):
    sent = []
    monkeypatch.setattr(app.requests, "post", _strict_api(sent))
    r = app._upstream_chat("uncloseai", _payload(stream=True, max_tokens=64,
                                                 stream_options={"include_usage": True}), True)
    assert r.status_code == 200
    assert set(sent[-1]) == {"model", "messages", "stream"} and sent[-1]["stream"] is True


def test_only_the_provider_that_refused_is_changed(isolated, monkeypatch):
    sent = []
    monkeypatch.setattr(app.requests, "post", _strict_api(sent))
    app._upstream_chat("uncloseai", _payload(max_tokens=512), False)
    sent.clear()
    monkeypatch.setattr(app.requests, "post",
                        lambda *a, **kw: sent.append(dict(kw.get("json") or {})) or _Resp(200))
    app._upstream_chat("llm7", _payload(max_tokens=512), False)
    assert "max_tokens" in sent[0]


def test_a_400_about_something_else_teaches_nothing(isolated, monkeypatch):
    """The cut body fails too (an unknown model, say): the original 400 is returned
    and the provider is NOT remembered as strict."""
    sent = []

    def post(*a, **kw):
        sent.append(dict(kw.get("json") or {}))
        return _Resp(400, '{"error": {"message": "Unsupported model: gemini-web"}}')

    monkeypatch.setattr(app.requests, "post", post)
    r = app._upstream_chat("uncloseai", _payload(max_tokens=512), False)
    assert r.status_code == 400
    assert len(sent) == 2                       # the one retry, no more
    assert "uncloseai" not in app._MINIMAL_BODY


def test_a_400_that_is_not_a_refusal_is_not_retried(isolated, monkeypatch):
    sent = []
    monkeypatch.setattr(app.requests, "post", lambda *a, **kw: sent.append(1) or _Resp(
        400, '{"error": {"message": "Your API key has no quota left."}}'))
    r = app._upstream_chat("uncloseai", _payload(max_tokens=512), False)
    assert r.status_code == 400 and len(sent) == 1


def test_a_tool_request_is_never_cut(isolated, monkeypatch):
    sent = []
    monkeypatch.setattr(app.requests, "post", _strict_api(sent))
    tools = [{"type": "function", "function": {"name": "bash", "parameters": {"type": "object"}}}]
    r = app._upstream_chat("uncloseai", _payload(max_tokens=512, tools=tools), False)
    assert r.status_code == 400
    assert len(sent) == 1
    assert "uncloseai" not in app._MINIMAL_BODY


def test_a_learned_provider_still_gets_its_tools_when_asked(isolated, monkeypatch):
    app._MINIMAL_BODY.add("uncloseai")
    sent = []
    monkeypatch.setattr(app.requests, "post",
                        lambda *a, **kw: sent.append(dict(kw.get("json") or {})) or _Resp(200))
    tools = [{"type": "function", "function": {"name": "bash", "parameters": {"type": "object"}}}]
    app._upstream_chat("uncloseai", _payload(tools=tools), False)
    assert "tools" in sent[0]


def test_the_flag_turns_it_off(isolated, monkeypatch):
    monkeypatch.setattr(app, "_strict_body_on", lambda: False)
    sent = []
    monkeypatch.setattr(app.requests, "post", _strict_api(sent))
    r = app._upstream_chat("uncloseai", _payload(max_tokens=512), False)
    assert r.status_code == 400 and len(sent) == 1


def test_the_flag_defaults_on_and_a_broken_read_means_off(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(app.config, "get_flag", lambda name, default=False: default)
    assert app._strict_body_on() is True

    def boom(*a, **k):
        raise OSError("unreadable")
    monkeypatch.setattr(app.config, "get_flag", boom)
    assert app._strict_body_on() is False
