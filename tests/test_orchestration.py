"""Orchestration glue: static_key no-key providers, quota persistence across
restarts, and routing-transparency response headers."""
import json
import shutil
import tempfile
import time

import pytest

import app
import config  # noqa: F401  (env isolation fixture touches its path convention)
import quota


@pytest.fixture
def state_dir():
    # NB: not pytest's tmp_path — this machine's default pytest basetemp is
    # permission-denied (the suite's known environmental errors), so tests here
    # allocate their own temp dirs like the hub-pytest-* runs did.
    d = tempfile.mkdtemp(prefix="hub-pytest-orch-")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def isolated_config(state_dir, monkeypatch):
    import os
    path = os.path.join(state_dir, "state", "config.json")
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", path)
    app._runtime_active[0] = 0
    app._runtime_shutdown_thread[0] = None
    app._runtime_server[0] = None
    return path


@pytest.fixture
def fresh_quota():
    """Run a test against EMPTY quota state with persistence detached, then put
    the module's real state back exactly as it was."""
    saved = (dict(quota._STATE), dict(quota._MODEL_STATE),
             dict(quota._MODEL_THROTTLE), dict(quota._DYNAMIC),
             quota._PERSIST_PATH, quota._persist_last)
    quota._STATE.clear()
    quota._MODEL_STATE.clear()
    quota._MODEL_THROTTLE.clear()
    quota._DYNAMIC.clear()
    quota._PERSIST_PATH = None
    quota._persist_last = 0.0
    try:
        yield
    finally:
        quota._STATE.clear()
        quota._STATE.update(saved[0])
        quota._MODEL_STATE.clear()
        quota._MODEL_STATE.update(saved[1])
        quota._MODEL_THROTTLE.clear()
        quota._MODEL_THROTTLE.update(saved[2])
        quota._DYNAMIC.clear()
        quota._DYNAMIC.update(saved[3])
        quota._PERSIST_PATH = saved[4]
        quota._persist_last = saved[5]


class FakeResponse:
    """Minimal stand-in for a requests.Response upstream answer."""

    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload

    def close(self):
        pass


def _chat_ok():
    return FakeResponse(200, {
        "id": "chatcmpl-fake",
        "choices": [{"message": {"role": "assistant", "content": "hello"}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    })


# --- static_key: no-key providers that carry a placeholder bearer -----------

def test_static_key_sent_for_uncloseai_shaped_provider(isolated_config, monkeypatch):
    captured = {}

    def post(url, json=None, headers=None, stream=None, timeout=None, **kw):
        captured["headers"] = headers
        return _chat_ok()

    monkeypatch.setattr(app.requests, "post", post)
    app._upstream_chat("uncloseai", {"model": "open", "messages": []}, False)
    assert captured["headers"]["Authorization"] == "Bearer uncloseai"


def test_no_authorization_for_pollinations_shaped_provider(isolated_config, monkeypatch):
    captured = {}

    def post(url, json=None, headers=None, stream=None, timeout=None, **kw):
        captured["headers"] = headers
        return _chat_ok()

    monkeypatch.setattr(app.requests, "post", post)
    app._upstream_chat("pollinations", {"model": "open", "messages": []}, False)
    assert "Authorization" not in captured["headers"]


def test_static_key_sent_on_models_discovery(isolated_config, monkeypatch):
    captured = {}
    with app._model_cache_lock:
        app._model_cache.pop("uncloseai", None)

    def get(url, headers=None, timeout=None):
        captured["headers"] = headers
        return FakeResponse(200, {"data": []})

    monkeypatch.setattr(app.requests, "get", get)
    app.provider_free_models("uncloseai", live=True)
    assert captured["headers"]["Authorization"] == "Bearer uncloseai"


def test_no_authorization_on_models_discovery_without_static_key(
        isolated_config, monkeypatch):
    captured = {}
    with app._model_cache_lock:
        app._model_cache.pop("pollinations", None)

    def get(url, headers=None, timeout=None):
        captured["headers"] = headers
        return FakeResponse(200, {"data": []})

    monkeypatch.setattr(app.requests, "get", get)
    app.provider_free_models("pollinations", live=True)
    assert captured["headers"] == {}


# --- quota persistence across restarts ---------------------------------------

def test_persistence_round_trip(fresh_quota, state_dir):
    import os
    path = os.path.join(state_dir, "quota-state.json")
    quota.init_persistence(path)
    quota.record("groq", "llama-3.3-70b-versatile", 3)
    quota.mark_throttled("groq", 60)
    quota.observe_headers("cerebras", {"x-ratelimit-remaining-requests": "41"})
    quota.save_state()

    # Simulate a restart: wipe the in-memory maps, load from the file alone.
    quota._STATE.clear()
    quota._MODEL_STATE.clear()
    quota._MODEL_THROTTLE.clear()
    quota._DYNAMIC.clear()
    quota.init_persistence(path)

    st = quota.status("groq")
    assert st["used"] == 3
    assert st["throttled"] is True
    assert quota.models("groq") == {"llama-3.3-70b-versatile": 3}
    assert quota._DYNAMIC["cerebras"]["remaining"] == 41


def test_persistence_corrupt_file_fails_open(fresh_quota, state_dir):
    import os
    path = os.path.join(state_dir, "quota-state.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not json at all")
    quota.init_persistence(path)  # must not raise
    assert quota._STATE == {}
    assert quota.status("groq")["used"] == 0


def test_persistence_drops_expired_entries(fresh_quota, state_dir):
    import os
    now = time.time()
    path = os.path.join(state_dir, "quota-state.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({
        "model_throttle": {
            "groq|expired-model": {"throttled_until": now - 5,
                                   "strikes": 2, "last_strike": now - 100},
            "groq|parked-model": {"throttled_until": now + 600,
                                  "strikes": 1, "last_strike": now},
        },
        # A dynamic reading older than _DYNAMIC_TTL is stale, not learned truth.
        "dynamic": {"groq": {"remaining": 3, "limit": 100, "reset_at": None,
                             "seen": now - quota._DYNAMIC_TTL - 10}},
    }))
    quota.init_persistence(path)
    assert quota.is_model_throttled("groq", "expired-model") is False
    assert quota.is_model_throttled("groq", "parked-model") is True
    assert "groq" not in quota._DYNAMIC


# --- routing-transparency headers --------------------------------------------

def _stub_chain(monkeypatch, hops):
    monkeypatch.setattr(app, "_route_by_difficulty",
                        lambda messages, max_tokens=None, est=0, require_tools=False:
                        (hops[0][0], hops[0][1], "simple"))
    monkeypatch.setattr(app, "_check_provider_ready", lambda pid: None)
    # **kw, not a fixed signature: this stub stands in for the real builder, and
    # pinning every parameter means any new routing option (exclude_identities,
    # prefer, whatever comes next) breaks five tests that are about response
    # headers rather than about the chain builder's arguments.
    monkeypatch.setattr(app, "_build_chain",
                        lambda pid, model, est=0, **kw: list(hops))


def test_transparency_headers_on_success(isolated_config, monkeypatch):
    _stub_chain(monkeypatch, [("groq", "llama-3.3-70b-versatile")])
    monkeypatch.setattr(app, "_dispatch_chat",
                        lambda pid, payload, stream: _chat_ok())
    client = app.app.test_client()
    resp = client.post("/v1/chat/completions", json={
        "model": "auto", "stream": False,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert resp.status_code == 200
    assert resp.headers["X-Free-LLM-Hub-Provider"] == "groq"
    assert resp.headers["X-Free-LLM-Hub-Model"] == "llama-3.3-70b-versatile"
    assert resp.headers["X-Free-LLM-Hub-Attempts"] == "1"
    # First hop just worked -> no hop failure to report.
    assert resp.headers["X-Free-LLM-Hub-Last-Error"] == "none"


def test_transparency_headers_on_chain_exhaustion(isolated_config, monkeypatch):
    _stub_chain(monkeypatch, [("groq", "llama-3.3-70b-versatile"),
                              ("cerebras", "gpt-oss-120b")])
    monkeypatch.setattr(app, "_dispatch_chat",
                        lambda pid, payload, stream:
                        FakeResponse(500, {"error": "boom"}))
    client = app.app.test_client()
    resp = client.post("/v1/chat/completions", json={
        "model": "auto", "stream": False,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert resp.status_code == 503
    assert resp.headers["X-Free-LLM-Hub-Attempts"] == "2"
    # The error relays the LAST hop the chain burned before giving up.
    assert resp.headers["X-Free-LLM-Hub-Provider"] == "cerebras"
    assert resp.headers["X-Free-LLM-Hub-Model"] == "gpt-oss-120b"
    assert resp.headers["X-Free-LLM-Hub-Last-Error"] == "http-500"


def test_last_error_header_surfaces_earlier_hop_failure(isolated_config, monkeypatch):
    """A 413 on hop 1 then success on hop 2: the header names the class that
    killed the earlier hop, so 'why did the chain degrade' is one curl away."""
    _stub_chain(monkeypatch, [("groq", "llama-3.3-70b-versatile"),
                              ("cerebras", "zai-glm-4.7")])
    monkeypatch.setattr(app, "_dispatch_chat",
                        lambda pid, payload, stream:
                        FakeResponse(413, {"error": "too large"}) if pid == "groq"
                        else _chat_ok())
    client = app.app.test_client()
    resp = client.post("/v1/chat/completions", json={
        "model": "auto", "stream": False,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert resp.status_code == 200
    assert resp.headers["X-Free-LLM-Hub-Provider"] == "cerebras"
    assert resp.headers["X-Free-LLM-Hub-Last-Error"] == "413"


def test_last_error_header_on_responses_exhaustion(isolated_config, monkeypatch):
    """Codex's path (/v1/responses) carried no routing headers at all — the
    last-error class is now surfaced on its chain-exhausted errors too."""
    monkeypatch.setattr(app, "_route_by_difficulty",
                        lambda messages, max_tokens=None, est=0, require_tools=False:
                        ("groq", "llama-3.3-70b-versatile", "hard"))
    monkeypatch.setattr(app, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(app, "_build_chain",
                        lambda pid, model, est=0, require_vision=False,
                               require_tools=False, messages=None:
                        [("groq", "llama-3.3-70b-versatile")])
    monkeypatch.setattr(app, "_dispatch_chat",
                        lambda pid, payload, stream:
                        FakeResponse(429, {"error": "slow down"}))
    client = app.app.test_client()
    resp = client.post("/v1/responses", json={
        "model": "auto", "stream": False, "input": "hi",
    })
    assert resp.status_code == 503
    assert resp.headers["X-Free-LLM-Hub-Last-Error"] == "429"


def test_transparency_headers_on_messages_success(isolated_config, monkeypatch):
    _stub_chain(monkeypatch, [("groq", "llama-3.3-70b-versatile")])
    monkeypatch.setattr(app, "_dispatch_chat",
                        lambda pid, payload, stream: _chat_ok())
    client = app.app.test_client()
    resp = client.post("/v1/messages", json={
        "model": "auto", "max_tokens": 16, "stream": False,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert resp.status_code == 200
    assert resp.headers["X-Free-LLM-Hub-Provider"] == "groq"
    assert resp.headers["X-Free-LLM-Hub-Model"] == "llama-3.3-70b-versatile"
    assert resp.headers["X-Free-LLM-Hub-Attempts"] == "1"
