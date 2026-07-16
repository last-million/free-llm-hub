import base64
import json
import re
from pathlib import Path

import pytest

import app
import config


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    path = tmp_path / "state" / "config.json"
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(path))
    app._runtime_active[0] = 0
    app._runtime_shutdown_thread[0] = None
    app._runtime_server[0] = None
    return path


def _data_url(payload=b"small-image"):
    return "data:image/png;base64," + base64.b64encode(payload).decode("ascii")


def test_config_migrates_state_and_compare_and_swap(isolated_config):
    state = config.get_hub_mode_state()
    assert state["phase"] == "unmanaged"
    assert state["revision"] == 0

    updated = config.update_hub_mode_state(0, lambda row: {**row, "desired": "off"})
    assert updated["revision"] == 1
    assert updated["desired"] == "off"
    with pytest.raises(config.RevisionConflict) as exc:
        config.update_hub_mode_state(0, lambda row: row)
    assert exc.value.current_revision == 1


def test_strict_mutation_refuses_corrupt_config(isolated_config):
    isolated_config.parent.mkdir(parents=True)
    isolated_config.write_text("not-json", encoding="utf-8")
    with pytest.raises(config.ConfigCorruptError):
        config.set_flag("example", True)
    assert isolated_config.read_text(encoding="utf-8") == "not-json"


def test_all_protocols_preserve_images():
    url = _data_url()
    messages, count = app._normalize_openai_messages([
        {"role": "user", "content": [
            {"type": "text", "text": "read this"},
            {"type": "image_url", "image_url": url},
        ]}
    ])
    assert count == 1
    assert messages[0]["content"][1]["image_url"]["url"] == url

    responses = app._responses_to_chat({"input": [{
        "type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "read"},
            {"type": "input_image", "image_url": url},
        ]
    }]})
    assert responses[0]["content"][1]["type"] == "image_url"

    anthropic = app._anthropic_to_openai_messages({"messages": [{
        "role": "user", "content": [
            {"type": "text", "text": "read"},
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/png",
                "data": base64.b64encode(b"image").decode("ascii"),
            }},
        ]
    }]})
    assert anthropic[0]["content"][1]["type"] == "image_url"


def test_media_validation_is_fail_closed():
    with pytest.raises(ValueError, match="audio and video"):
        app._normalize_openai_messages([
            {"role": "user", "content": [{"type": "input_audio", "data": "x"}]}
        ])
    with pytest.raises(ValueError, match="supported image type"):
        app._normalize_image_url("data:image/svg+xml;base64,PHN2Zz4=")
    with pytest.raises(ValueError, match="https"):
        app._normalize_image_url("file:///etc/passwd")


def test_vision_priority_and_fallback_exclude_text_models(monkeypatch):
    monkeypatch.setattr(app, "_available_providers", lambda: ["google", "glm", "groq"])
    models = {
        "google": ["gemini-3.5-flash", "not-vision"],
        "glm": ["glm-4.6v-flash", "glm-4.7-flash"],
        "groq": ["llama-3.3-70b-versatile"],
    }
    monkeypatch.setattr(app, "provider_free_models", lambda pid, live=True: models[pid])
    monkeypatch.setattr(config, "get_media_state", lambda: {
        "priority_mode": "manual", "manual_priority": ["glm/glm-4.6v-flash"]
    })
    candidates = app._vision_candidates()
    assert candidates[0] == ("glm", "glm-4.6v-flash")
    chain = app._build_chain("glm", "glm-4.6v-flash", require_vision=True)
    assert ("google", "gemini-3.5-flash") in chain
    assert all(app._is_vision_model(pid, model) for pid, model in chain)


def test_snapshot_restore_and_conflict(isolated_config, tmp_path):
    target = tmp_path / "cli.json"
    target.write_bytes(b"original")
    entry = {"id": "fake", "write_path": str(target), "config_paths": [str(target)],
             "env_check": [], "autofix": "fake"}
    generation = config.new_generation_id()
    manifest = app._capture_cli_snapshot(generation, entry)
    target.write_bytes(b"managed")
    manifest["managed_sha256"] = config.sha256_bytes(b"managed")
    app._write_snapshot_manifest(generation, "fake", manifest)
    restored = app._restore_cli_snapshot(generation, "fake")
    assert restored["status"] == "off"
    assert target.read_bytes() == b"original"

    generation2 = config.new_generation_id()
    manifest2 = app._capture_cli_snapshot(generation2, entry)
    target.write_bytes(b"managed-2")
    manifest2["managed_sha256"] = config.sha256_bytes(b"managed-2")
    app._write_snapshot_manifest(generation2, "fake", manifest2)
    target.write_bytes(b"user-edited")
    conflict = app._restore_cli_snapshot(generation2, "fake")
    assert conflict["status"] == "conflict"
    assert target.read_bytes() == b"user-edited"


def test_startup_recovers_interrupted_enable(isolated_config, tmp_path):
    target = tmp_path / "cli.json"
    target.write_bytes(b"original")
    entry = {"id": "recover", "write_path": str(target), "config_paths": [str(target)],
             "env_check": [], "autofix": "fake"}
    generation = config.new_generation_id()
    manifest = app._capture_cli_snapshot(generation, entry)
    target.write_bytes(b"managed")
    manifest["managed_sha256"] = config.sha256_bytes(b"managed")
    app._write_snapshot_manifest(generation, "recover", manifest)
    config.update_hub_mode_state(0, lambda row: {
        **row, "desired": "on", "phase": "changing", "generation": generation,
    })
    app._recover_interrupted_hub_transition()
    assert target.read_bytes() == b"original"
    state = config.get_hub_mode_state()
    assert state["phase"] == "error"
    assert state["clients"]["recover"]["status"] == "off"


def test_bulk_hub_cycle_restores_exact_bytes(isolated_config, tmp_path, monkeypatch):
    target = tmp_path / "tool.conf"
    target.write_text("user-setting=true\n", encoding="utf-8")
    entry = {"id": "fake", "name": "Fake", "kind": "openai", "bins": ["fake"],
             "write_path": str(target), "config_paths": [str(target)], "env_check": [],
             "autofix": "fake", "default_method": "config"}
    monkeypatch.setattr(app, "CLI_REGISTRY", [entry])
    monkeypatch.setattr(app, "_CLI_BY_ID", {"fake": entry})
    monkeypatch.setattr(app, "_cli_installed", lambda _entry: (True, "fake"))
    monkeypatch.setattr(app, "_first_free_model_id", lambda: "google/gemini-3.5-flash")

    def fixer(_entry, _key, _root, _v1, _model):
        app._cli_write_text(str(target), "managed=true\n")
        return {"ok": True, "wrote_path": str(target), "restart_hint": "restart"}

    monkeypatch.setitem(app._AUTOFIXERS, "fake", fixer)
    on = app._bulk_hub_on(0)
    assert on["phase"] == "on"
    assert target.read_text(encoding="utf-8") == "managed=true\n"
    off = app._bulk_hub_off(on["revision"])
    assert off["phase"] == "off"
    assert target.read_text(encoding="utf-8") == "user-setting=true\n"


def test_media_api_uses_revision_cas(isolated_config, monkeypatch):
    monkeypatch.setattr(app, "_vision_candidates", lambda est=0: [])
    client = app.app.test_client()
    response = client.post("/api/media", json={
        "revision": 0, "priority_mode": "manual",
        "manual_priority": ["google/gemini-3.5-flash"],
    }, headers={"X-Free-LLM-Hub": "dashboard"})
    assert response.status_code == 200
    assert response.get_json()["state"]["revision"] == 1
    stale = client.post("/api/media", json={
        "revision": 0, "priority_mode": "auto", "manual_priority": [],
    }, headers={"X-Free-LLM-Hub": "dashboard"})
    assert stale.status_code == 409


def test_stop_api_sets_marker_without_running_worker(isolated_config, monkeypatch):
    class DummyThread:
        def __init__(self, *args, **kwargs):
            self.started = False

        def is_alive(self):
            return False

        def start(self):
            self.started = True

    monkeypatch.setattr(app.threading, "Thread", DummyThread)
    client = app.app.test_client()
    response = client.post("/api/runtime/stop", json={"revision": 0},
                           headers={"X-Free-LLM-Hub": "dashboard"})
    assert response.status_code == 202
    assert config.is_intentionally_stopped()
    assert config.get_runtime_state()["phase"] == "draining"


def test_local_control_guard_and_security_headers(isolated_config):
    client = app.app.test_client()
    hostile_host = client.get("/", headers={"Host": "attacker.example"})
    assert hostile_host.status_code == 403
    cross_site = client.post(
        "/api/media",
        json={"revision": 0, "priority_mode": "auto", "manual_priority": []},
        headers={"Origin": "https://attacker.example", "X-Free-LLM-Hub": "dashboard"},
    )
    assert cross_site.status_code == 403
    missing_header = client.post(
        "/api/media", json={"revision": 0, "priority_mode": "auto", "manual_priority": []}
    )
    assert missing_header.status_code == 403

    page = client.get("/")
    assert page.status_code == 200
    csp = page.headers["Content-Security-Policy"]
    nonce = re.search(r"script-src 'nonce-([^']+)'", csp).group(1)
    assert ('nonce="%s"' % nonce).encode() in page.data
    assert page.headers["X-Content-Type-Options"] == "nosniff"

    token = config.ensure_control_token()
    embedded = client.get("/")
    assert json.dumps(token).encode() in embedded.data
    no_header = client.get("/api/runtime")
    assert no_header.status_code == 401
    with_token = client.get("/api/runtime", headers={"X-Free-LLM-Hub-Token": token})
    assert with_token.status_code == 200


def test_chat_image_route_preserves_payload_and_requires_vision(
        isolated_config, monkeypatch):
    captured = {}
    monkeypatch.setattr(app, "_route_for_vision",
                        lambda messages, max_tokens=None, est=None:
                        ("google", "gemini-3.5-flash", "simple"))
    monkeypatch.setattr(app, "_check_provider_ready", lambda pid: None)

    def chain(pid, model, est=0, require_vision=False):
        captured["require_vision"] = require_vision
        return [(pid, model)]

    monkeypatch.setattr(app, "_build_chain", chain)

    class Response:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"role": "assistant", "content": "seen"}}]}

        def close(self):
            pass

    def dispatch(pid, payload, stream):
        captured["payload"] = payload
        return Response()

    monkeypatch.setattr(app, "_dispatch_chat", dispatch)
    client = app.app.test_client()
    response = client.post("/v1/chat/completions", json={
        "model": "auto", "stream": False,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "what is this?"},
            {"type": "image_url", "image_url": _data_url()},
        ]}],
    })
    assert response.status_code == 200
    assert captured["require_vision"] is True
    assert captured["payload"]["messages"][0]["content"][1]["type"] == "image_url"


def test_control_token_gates_api_routes(isolated_config, monkeypatch):
    monkeypatch.setattr(app, "_vision_candidates", lambda est=0: [])
    token = config.ensure_control_token()
    client = app.app.test_client()

    no_token = client.get("/api/runtime")
    assert no_token.status_code == 401
    assert no_token.get_json()["code"] == "token_required"

    wrong_token = client.get("/api/runtime", headers={"X-Free-LLM-Hub-Token": "nope"})
    assert wrong_token.status_code == 401

    ok = client.get("/api/runtime", headers={"X-Free-LLM-Hub-Token": token})
    assert ok.status_code == 200

    mutation_ok = client.post(
        "/api/media",
        json={"revision": 0, "priority_mode": "auto", "manual_priority": []},
        headers={"X-Free-LLM-Hub": "dashboard", "X-Free-LLM-Hub-Token": token},
    )
    assert mutation_ok.status_code == 200


def test_custom_base_url_validation():
    assert app._validate_custom_base_url("http://127.0.0.1:9000/v1") == \
        "http://127.0.0.1:9000/v1"
    assert app._validate_custom_base_url("https://api.example.com/v1/") == \
        "https://api.example.com/v1"
    with pytest.raises(ValueError, match="https"):
        app._validate_custom_base_url("http://api.example.com/v1")
    with pytest.raises(ValueError, match="credentials"):
        app._validate_custom_base_url("https://user:pass@api.example.com/v1")
