"""Low-resource mode (lowres.py): detection, the multi-session worker cap,
the API and the Settings control. No network, isolated config.

NOTE: no pytest tmp_path here -- this machine's basetemp is permission-denied.
"""
import os
import shutil
import tempfile
import threading
import time

import pytest

import app
import config
import lowres
import swarm_windows

H = {"X-Free-LLM-Hub": "dashboard"}
BIG = {"total_gb": 40.0, "free_gb": 20.0, "cores": 8}
WEAK = {"total_gb": 4.0, "free_gb": 2.0, "cores": 4}
MID = {"total_gb": 7.5, "free_gb": 3.0, "cores": 8}
BUSY = {"total_gb": 40.0, "free_gb": 1.0, "cores": 8}


@pytest.fixture
def isolated_config(monkeypatch):
    root = tempfile.mkdtemp(prefix="hub-pytest-")
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", os.path.join(root, "state", "config.json"))
    lowres._CACHE.update(at=0.0, value=None)
    try:
        yield root
    finally:
        lowres._CACHE.update(at=0.0, value=None)
        shutil.rmtree(root, ignore_errors=True)


def _machine(monkeypatch, m):
    monkeypatch.setattr(lowres, "machine", lambda: dict(m))


def test_detection_thresholds():
    assert not lowres.is_weak(BIG)
    assert lowres.is_weak(WEAK)                      # < 8 GB
    assert lowres.is_weak({"total_gb": 16.0, "cores": 2})   # < 4 cores
    assert not lowres.is_weak({"total_gb": None, "cores": None})  # unreadable = old behaviour


def test_workers_by_mode(isolated_config):
    assert lowres.mode() == "auto"
    assert lowres.workers(4, BIG) == 4
    assert lowres.workers(4, WEAK) == 1              # active, under 6 GB
    assert lowres.workers(4, MID) == 2               # active, 6-8 GB
    assert lowres.workers(4, BUSY) == 1              # big machine, free RAM short
    config.set_setting("low_resource_mode", "on")
    assert lowres.workers(4, BIG) == 2
    config.set_setting("low_resource_mode", "off")
    assert lowres.workers(4, WEAK) == 4
    assert lowres.workers(4, BUSY) == 4              # off = full speed, no guard
    config.set_setting("low_resource_mode", "bogus")
    assert lowres.mode() == "auto"


def test_machine_reading_is_cached(isolated_config, monkeypatch):
    calls = []
    monkeypatch.setattr(lowres, "_read_machine", lambda: calls.append(1) or dict(BIG))
    for _ in range(50):
        lowres.machine()
    assert len(calls) == 1


def test_multi_session_wave_respects_the_cap(isolated_config, monkeypatch):
    _machine(monkeypatch, WEAK)
    monkeypatch.setattr(swarm_windows, "SPAWN_STAGGER", 0)
    live, peak, lock = [0], [0], threading.Lock()

    def fake_run_agent(run, agent, spawn, run_turn, configure):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.15)
        with lock:
            live[0] -= 1
        agent.state = swarm_windows.DONE
    monkeypatch.setattr(swarm_windows, "_run_agent", fake_run_agent)

    class A:
        def __init__(self):
            self.state, self.started_at, self.last_event_at = "pending", None, None

    class Run:
        id = "t"
        agents = [A() for _ in range(4)]
        stop_flag = threading.Event()
    swarm_windows._run_wave(Run(), [1, 2, 3, 4], None, None)
    assert peak[0] == 1
    assert all(a.state == swarm_windows.DONE for a in Run.agents)


def test_concurrency_falls_back_to_the_constant(monkeypatch):
    def boom(n):
        raise RuntimeError("psutil gone")
    monkeypatch.setattr(lowres, "workers", boom)
    assert swarm_windows._concurrency() == swarm_windows.MAX_CONCURRENT


def test_api_roundtrip(isolated_config, monkeypatch):
    _machine(monkeypatch, WEAK)
    c = app.app.test_client()
    v = c.get("/api/low-resource").get_json()
    assert v["mode"] == "auto" and v["active"] is True and v["workers"] == 1
    assert v["machine"]["total_gb"] == 4.0
    v = c.post("/api/low-resource", json={"mode": "off"}, headers=H).get_json()
    assert v["mode"] == "off" and v["active"] is False and v["workers"] == 4
    assert config.get_setting("low_resource_mode") == "off"
    assert c.post("/api/low-resource", json={"mode": "max"}, headers=H).status_code == 400


def test_api_is_control_gated(isolated_config, monkeypatch):
    monkeypatch.setattr(config, "get_control_token", lambda: "secret-token")
    c = app.app.test_client()
    assert c.get("/api/low-resource").status_code == 401
    assert c.post("/api/low-resource", json={"mode": "on"}).status_code == 403


def test_settings_control():
    src = open(os.path.join(os.path.dirname(app.__file__), "templates", "index.html"),
               encoding="utf-8").read()
    for needle in ('id="lowres-mode"', 'value="auto"', 'value="on"', 'value="off"',
                   'id="lowres-status"', "/api/low-resource", "initLowres();",
                   "loadLowres(silent)"):
        assert needle in src, needle
    js = src[src.index("function renderLowres"):src.index("function loadLowres")]
    assert "textContent" in js and "innerHTML" not in js
