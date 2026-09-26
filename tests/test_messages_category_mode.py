"""/v1/messages honours a bare category id, like /v1/chat/completions does.

REPORTED: ANTHROPIC_MODEL=coding routed Claude Code over every category --
v1_messages never set g.model_mode from the model id, while the chat path did.
"""
from unittest import mock

import app


class _FakeOK:
    status_code = 200
    headers = {}

    def json(self):
        return {"id": "x", "object": "chat.completion", "created": 1,
                "model": "llama-3.3-70b-versatile",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    def close(self):
        pass


def _post_messages(model):
    seen = []

    def _router(messages, max_tokens=None, est=0, require_tools=False, **kw):
        seen.append(app._active_mode())
        return ("groq", "llama-3.3-70b-versatile", "medium")

    def _chain(*a, **k):
        seen.append(app._active_mode())
        return [("groq", "llama-3.3-70b-versatile")]

    app.app.config["TESTING"] = True
    with mock.patch.object(app, "_route_by_difficulty", _router), \
            mock.patch.object(app, "_global_mode", lambda: app.MODE_ALL), \
            mock.patch.object(app, "_check_provider_ready", lambda pid: None), \
            mock.patch.object(app, "_build_chain", _chain), \
            mock.patch.object(app, "_dispatch_chat",
                              lambda pid, payload, stream: _FakeOK()):
        r = app.app.test_client().post("/v1/messages", json={
            "model": model, "max_tokens": 8, "stream": False,
            "messages": [{"role": "user", "content": "hi"}]})
    return r, seen


def test_bare_category_restricts_routing_to_that_category():
    r, seen = _post_messages("coding")
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    assert seen and all(m == "coding" for m in seen), seen


def test_category_is_case_and_space_insensitive():
    _, seen = _post_messages("  Coding ")
    assert seen and all(m == "coding" for m in seen), seen


def test_claude_model_id_leaves_the_mode_alone():
    _, seen = _post_messages("claude-sonnet-4-5")
    assert seen and "coding" not in seen, seen
