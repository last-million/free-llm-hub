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


def test_desktop_shortcut_sets_the_calvoun_icon(isolated_config, desktop_dir, monkeypatch):
    """Without IconLocation the shortcut just shows wscript.exe's generic
    scroll icon -- indistinguishable from any other .vbs shortcut."""
    monkeypatch.setattr(app, "_desktop_dir", lambda: desktop_dir)
    captured = {}

    def fake_powershell(command, timeout=20):
        captured["command"] = command
        with open(os.path.join(desktop_dir, "Calvoun Free LLM Hub.lnk"), "wb") as f:
            f.write(b"LNK")

    monkeypatch.setattr(app, "_run_hidden_powershell", fake_powershell)
    response = _client().post("/api/hub/desktop-shortcut", json={},
                              headers={"X-Free-LLM-Hub": "dashboard"})
    assert response.status_code == 200
    assert "IconLocation" in captured["command"]
    assert "calvoun.ico" in captured["command"]


# --------------------------------------------------------------------------- #
# Auto-create-once (2026-08-12): a user who never finds the Stop-hub modal's
# checkbox should still get a desktop shortcut, same "once, marker only on
# success" shape as run.bat's own maybe_autopersist for autostart.
# --------------------------------------------------------------------------- #

def test_auto_shortcut_skips_on_non_windows(isolated_config, monkeypatch):
    monkeypatch.setattr(app.os, "name", "posix")

    def boom():
        raise AssertionError("must not be called on a non-Windows platform")

    monkeypatch.setattr(app, "_create_desktop_shortcut", boom)
    app._maybe_auto_create_desktop_shortcut()
    marker = os.path.join(config.state_dir(), app._DESKTOP_SHORTCUT_MARKER_NAME)
    assert not os.path.exists(marker)


def test_auto_shortcut_creates_once_then_skips(isolated_config, monkeypatch):
    monkeypatch.setattr(app.os, "name", "nt")
    calls = []
    monkeypatch.setattr(app, "_create_desktop_shortcut", lambda: calls.append(1))

    app._maybe_auto_create_desktop_shortcut()
    marker = os.path.join(config.state_dir(), app._DESKTOP_SHORTCUT_MARKER_NAME)
    assert os.path.isfile(marker)
    assert len(calls) == 1

    app._maybe_auto_create_desktop_shortcut()
    assert len(calls) == 1, "marker exists -- must not create a second shortcut"


def test_auto_shortcut_retries_after_a_failure(isolated_config, monkeypatch):
    """A transient failure (Desktop dir not ready, PowerShell hiccup) must
    retry on the next boot, not be silently given up on forever."""
    monkeypatch.setattr(app.os, "name", "nt")
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("transient")

    monkeypatch.setattr(app, "_create_desktop_shortcut", flaky)
    marker = os.path.join(config.state_dir(), app._DESKTOP_SHORTCUT_MARKER_NAME)

    app._maybe_auto_create_desktop_shortcut()
    assert not os.path.exists(marker), "must not mark success after a failed attempt"
    assert len(calls) == 1

    app._maybe_auto_create_desktop_shortcut()
    assert os.path.isfile(marker)
    assert len(calls) == 2, "must retry since the first attempt failed"
