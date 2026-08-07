"""Tests for the IMAGE-ONLY branch of /api/test/<pid> (AI Horde).

AI Horde has no chat endpoint at all, so the chat-completions probe used for
every other provider is a guaranteed false negative ("Provider has no
models_url and no default model to test with." — the live bug this covers).
The test must instead probe the provider's REAL capability: the public
status/models?type=image feed (a real anonymous render queues ~24min —
untestable behind a dashboard click). Network is faked throughout.
"""
import os
import shutil
import tempfile

import pytest

import app
import config

_DASH = {"X-Free-LLM-Hub": "dashboard"}


@pytest.fixture
def hub_config(monkeypatch):
    # NOTE: tempfile.mkdtemp, NOT pytest's tmp_path — on this machine the
    # pytest tmp-dir factory dies with a PermissionError (pre-existing
    # environmental issue; see AGENTS.md "Tests").
    d = tempfile.mkdtemp(prefix="hub-test-")
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", os.path.join(d, "state", "config.json"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


class _FakeResp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload
        self.headers = {}

    def json(self):
        return self._payload


def _fake_get(status_payload, probe_status=200, find_user_status=200):
    def _get(url, **kw):
        if "find_user" in url:
            return _FakeResp(find_user_status, {"username": "tester"})
        if "status/models" in url:
            return _FakeResp(probe_status, status_payload)
        raise AssertionError("unexpected GET " + url)
    return _get


def _forbid_chat(monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("chat probe must never run for an image-only provider")
    monkeypatch.setattr(app, "_upstream_chat", _boom)
    monkeypatch.setattr(app.requests, "post", _boom)


def test_aihorde_healthy_reports_image_only_ok(hub_config, monkeypatch):
    _forbid_chat(monkeypatch)
    monkeypatch.setattr(app.requests, "get", _fake_get([
        {"name": "stable_diffusion", "count": 12, "queued": 0.0},
        {"name": "Flux-Schnell", "count": 3, "queued": 5.0},
        {"name": "idle-model", "count": 0, "queued": 0.0},
    ]))
    client = app.app.test_client()
    resp = client.post("/api/test/aihorde", headers=_DASH)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert "Image-only" in body["detail"]
    assert "status/models" in body["detail"] or "status probe" in body["detail"]
    assert body["sample_models"]  # top image models by worker count
    assert "stable_diffusion" in body["sample_models"]


def test_aihorde_no_workers_is_truthful_failure(hub_config, monkeypatch):
    _forbid_chat(monkeypatch)
    monkeypatch.setattr(app.requests, "get", _fake_get([
        {"name": "stable_diffusion", "count": 0, "queued": 0.0},
    ]))
    client = app.app.test_client()
    body = client.post("/api/test/aihorde", headers=_DASH).get_json()
    assert body["ok"] is False
    assert "NO workers" in body["detail"]


def test_aihorde_probe_http_error_is_truthful_failure(hub_config, monkeypatch):
    _forbid_chat(monkeypatch)
    monkeypatch.setattr(app.requests, "get", _fake_get(None, probe_status=500))
    client = app.app.test_client()
    body = client.post("/api/test/aihorde", headers=_DASH).get_json()
    assert body["ok"] is False
    assert "HTTP 500" in body["detail"]


def test_aihorde_invalid_saved_key_is_truthful_failure(hub_config, monkeypatch):
    _forbid_chat(monkeypatch)
    config.set_provider_config("aihorde", api_key="bad-key")
    monkeypatch.setattr(app.requests, "get", _fake_get(
        [{"name": "stable_diffusion", "count": 12}], find_user_status=401))
    client = app.app.test_client()
    body = client.post("/api/test/aihorde", headers=_DASH).get_json()
    assert body["ok"] is False
    assert "INVALID" in body["detail"]


def test_aihorde_probe_network_error_is_truthful_failure(hub_config, monkeypatch):
    _forbid_chat(monkeypatch)

    def _down(url, **kw):
        raise app.requests.ConnectionError("connection refused")
    monkeypatch.setattr(app.requests, "get", _down)
    client = app.app.test_client()
    body = client.post("/api/test/aihorde", headers=_DASH).get_json()
    assert body["ok"] is False
    assert "ConnectionError" in body["detail"]


def test_chat_providers_unaffected_by_image_only_branch(hub_config, monkeypatch):
    """A provider WITH a chat surface (models_url + default models) must still
    take the normal 1-token chat probe — the image-only branch is gated on
    having NO chat surface at all.

    Uses llm7 (keyless, has models_url + default models). This was g4f-gemini
    until 2026-08-06, when g4f.space ended anonymous access and it became a
    keyed provider — the probe then short-circuits on "no key" before ever
    reaching the branch under test, which has nothing to do with this test's
    subject."""
    monkeypatch.setattr(app.requests, "get", _fake_get([]))
    seen = {}

    class _ChatResp:
        status_code = 200
        headers = {}

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    def _chat(pid, payload, stream):
        seen["called"] = True
        return _ChatResp()
    monkeypatch.setattr(app, "_upstream_chat", _chat)
    client = app.app.test_client()
    body = client.post("/api/test/llm7", headers=_DASH).get_json()
    assert seen.get("called") is True
    assert body["ok"] is True
    assert "chat succeeded" in body["detail"]
