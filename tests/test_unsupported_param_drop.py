"""An optional client parameter a provider refuses is dropped, not fatal.

MEASURED live 2026-09-27: a client sending `prompt_cache_key` on
/v1/chat/completions got nvidia 400 "Validation: Unsupported parameter(s):
`prompt_cache_key`" on every nvidia hop -- 17 CHAT-503s in a row.
"""
import os
import shutil
import tempfile

import pytest

import app

NV_400 = ('{"message": "Validation: Unsupported parameter(s): `prompt_cache_key`", '
          '"type": "Bad Request", "code": 400}')


@pytest.fixture
def isolated(monkeypatch):
    d = tempfile.mkdtemp(prefix="hub-pytest-params-")
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", os.path.join(d, "state", "config.json"))
    monkeypatch.setattr(app, "_PARAM_REJECTED", {})
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


class _Resp:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text
        self.headers = {}

    def json(self):
        return {"choices": [{"finish_reason": "stop",
                             "message": {"role": "assistant", "content": "OK"}}]}

    def close(self):
        pass

    def iter_lines(self, decode_unicode=False):
        return iter(())


def _payload(**extra):
    p = {"model": "open", "messages": [{"role": "user", "content": "hi"}]}
    p.update(extra)
    return p


@pytest.mark.parametrize("text", [
    NV_400,
    '{"error":{"message":"Unrecognized request argument supplied: prompt_cache_key"}}',
    '{"detail":[{"type":"extra_forbidden","loc":["body","prompt_cache_key"],'
    '"msg":"Extra inputs are not permitted"}]}',
    "{\"error\":{\"message\":\"property 'prompt_cache_key' is unsupported\"}}",
])
def test_the_common_refusal_shapes_name_the_key(text):
    assert app._rejected_optional_params(_payload(prompt_cache_key="s1"), text) == {
        "prompt_cache_key"}


def test_what_the_request_is_is_never_dropped():
    text = "Unsupported parameter(s): `tools`, `messages`, `model`, `stream`"
    assert app._rejected_optional_params(_payload(tools=[], stream=True), text) == set()


def test_an_ordinary_400_drops_nothing():
    text = ("This model's maximum context length is 32768 tokens. However, your "
            "messages resulted in 40000 tokens.")
    assert app._rejected_optional_params(_payload(prompt_cache_key="s"), text) == set()
    assert app._rejected_optional_params(_payload(), None) == set()


def test_the_hop_is_retried_without_it_and_it_is_remembered(isolated, monkeypatch):
    sent = []

    def fake_post(*a, **kw):
        body = kw.get("json") or {}
        sent.append(set(body))
        if "prompt_cache_key" in body:
            return _Resp(400, NV_400)
        return _Resp(200)

    monkeypatch.setattr(app.requests, "post", fake_post)
    r = app._upstream_chat("uncloseai", _payload(prompt_cache_key="sess-1"), False)
    assert r.status_code == 200
    assert "prompt_cache_key" in sent[0] and "prompt_cache_key" not in sent[-1]
    # Next request: never sent at all.
    sent.clear()
    r = app._upstream_chat("uncloseai", _payload(prompt_cache_key="sess-2"), False)
    assert r.status_code == 200
    assert len(sent) == 1 and "prompt_cache_key" not in sent[0]


def test_another_provider_still_gets_it(isolated, monkeypatch):
    app._PARAM_REJECTED["nvidia"] = {"prompt_cache_key"}
    sent = []
    monkeypatch.setattr(app.requests, "post",
                        lambda *a, **kw: sent.append(set(kw.get("json") or {})) or _Resp(200))
    app._upstream_chat("uncloseai", _payload(prompt_cache_key="s"), False)
    assert "prompt_cache_key" in sent[0]
