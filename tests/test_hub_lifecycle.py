"""Hub lifecycle tests: sticky stop flag, stopped-state query, desktop shortcut.

The stop flag itself is config.set_intentional_stop() -> state_dir()/
"intentional-stop". run.bat's flag logic (supervised runs refuse while the
flag exists, unsupervised runs clear it and start) is batch glue for Windows
autostart; it is exercised manually, not driven from pytest.

NOTE: no pytest tmp_path here — this machine's basetemp is permission-denied;
tempfile.mkdtemp(prefix="hub-pytest-") works.
"""

import os
import shutil
import tempfile

import pytest

import app
import config


@pytest.fixture
def isolated_config(monkeypatch):
    root = tempfile.mkdtemp(prefix="hub-pytest-")
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", os.path.join(root, "state", "config.json"))
    app._runtime_active[0] = 0
    app._runtime_shutdown_thread[0] = None
    app._runtime_server[0] = None
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def desktop_dir():
    path = tempfile.mkdtemp(prefix="hub-pytest-desktop-")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _client():
    return app.app.test_client()


def test_stop_endpoint_sets_flag(isolated_config, monkeypatch):
    class DummyThread:
        def __init__(self, *args, **kwargs):
            self.started = False

        def is_alive(self):
            return False

        def start(self):
            self.started = True

    monkeypatch.setattr(app.threading, "Thread", DummyThread)
    assert not config.is_intentionally_stopped()
    response = _client().post("/api/runtime/stop", json={"revision": 0},
                              headers={"X-Free-LLM-Hub": "dashboard"})
    assert response.status_code == 202
    assert config.is_intentionally_stopped()
    assert os.path.isfile(config.intentional_stop_path())


def test_stopped_state_endpoint_reflects_flag(isolated_config):
    client = _client()
    response = client.get("/api/hub/stopped")
    assert response.status_code == 200
    assert response.get_json() == {"stopped": False}
    config.set_intentional_stop()
    assert client.get("/api/hub/stopped").get_json() == {"stopped": True}
    config.clear_intentional_stop()
    assert client.get("/api/hub/stopped").get_json() == {"stopped": False}


def test_desktop_shortcut_requires_local_control_header(isolated_config):
    response = _client().post("/api/hub/desktop-shortcut", json={})
    assert response.status_code == 403


def test_desktop_shortcut_creates_lnk(isolated_config, desktop_dir, monkeypatch):
    monkeypatch.setattr(app, "_desktop_dir", lambda: desktop_dir)
    captured = {}

    def fake_powershell(command, timeout=20):
        captured["command"] = command
        # Emulate the WScript.Shell COM call: the .lnk lands on disk.
        with open(os.path.join(desktop_dir, "Calvoun Free LLM Hub.lnk"), "wb") as f:
            f.write(b"LNK")

    monkeypatch.setattr(app, "_run_hidden_powershell", fake_powershell)
    response = _client().post("/api/hub/desktop-shortcut", json={},
                              headers={"X-Free-LLM-Hub": "dashboard"})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["path"] == os.path.join(desktop_dir, "Calvoun Free LLM Hub.lnk")
    assert os.path.isfile(payload["path"])
    # The shortcut targets run-hidden.vbs (no 'supervised' -> clears the flag).
    assert "run-hidden.vbs" in captured["command"]


def test_desktop_shortcut_falls_back_to_bat(isolated_config, desktop_dir, monkeypatch):
    monkeypatch.setattr(app, "_desktop_dir", lambda: desktop_dir)

    def boom(command, timeout=20):
        raise OSError("powershell unavailable")

    monkeypatch.setattr(app, "_run_hidden_powershell", boom)
    response = _client().post("/api/hub/desktop-shortcut", json={},
                              headers={"X-Free-LLM-Hub": "dashboard"})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["path"].endswith(".bat")
    with open(payload["path"], encoding="utf-8") as f:
        body = f.read()
    assert "run-hidden.vbs" in body
