"""Liveness / readiness probes (/health, /healthz, /ready, /readyz).

Idea from PR #4. The probes must answer without the control token, carry no
version / release / provider data (the hub shows those to token holders only),
keep the loopback Host guard, and report readiness with the same runtime test
/v1 traffic gets.
"""
import time

import pytest

import app
import config


def _client():
    return app.app.test_client()


@pytest.fixture
def token_set(monkeypatch):
    monkeypatch.setattr(config, "get_control_token", lambda: "probe-test-token")


@pytest.fixture
def running(monkeypatch):
    monkeypatch.setattr(config, "get_runtime_state",
                        lambda: {"desired": "running", "phase": "running"})


def _providers(monkeypatch, rows):
    """rows: {pid: paid?} for the enabled, usable providers."""
    monkeypatch.setattr(app, "_enabled_keyed", lambda: list(rows))
    monkeypatch.setattr(app.prov, "get_provider",
                        lambda pid: {"id": pid, "paid": rows[pid]} if pid in rows else None)


@pytest.mark.parametrize("path", ["/health", "/healthz"])
def test_health_needs_no_token_and_says_only_ok(path, token_set):
    with _client() as c:
        assert c.get("/api/version").status_code == 401   # the gate is on
        r = c.get(path)
    assert r.status_code == 200
    body = r.get_json()
    assert set(body) == {"status", "uptime_seconds"}
    assert body["status"] == "ok"
    assert r.headers["Cache-Control"] == "no-store"


def test_health_answers_head(token_set):
    with _client() as c:
        assert c.head("/health").status_code == 200
        assert c.head("/ready").status_code in (200, 503)


def test_uptime_is_monotonic(monkeypatch):
    monkeypatch.setattr(app, "_HUB_STARTED_MONO", time.monotonic() - 42)
    with _client() as c:
        assert c.get("/health").get_json()["uptime_seconds"] >= 42


def test_health_stays_up_while_stopping(monkeypatch):
    monkeypatch.setattr(config, "get_runtime_state",
                        lambda: {"desired": "stopped", "phase": "draining"})
    with _client() as c:
        assert c.get("/health").status_code == 200
        r = c.get("/ready")
    assert r.status_code == 503
    assert r.get_json() == {"status": "not_ready", "reason": "draining"}


@pytest.mark.parametrize("path", ["/ready", "/readyz"])
def test_ready_with_a_free_provider(path, running, token_set, monkeypatch):
    _providers(monkeypatch, {"metered": True, "groq": False})
    with _client() as c:
        r = c.get(path)
    assert r.status_code == 200
    assert r.get_json() == {"status": "ready"}
    assert r.headers["Cache-Control"] == "no-store"


def test_not_ready_with_only_paid_or_no_providers(running, monkeypatch):
    for rows in ({"metered": True}, {}):
        _providers(monkeypatch, rows)
        with _client() as c:
            r = c.get("/ready")
        assert r.status_code == 503
        assert r.get_json() == {"status": "not_ready", "reason": "no_provider"}


def test_not_ready_when_stopped(monkeypatch):
    _providers(monkeypatch, {"groq": False})
    monkeypatch.setattr(config, "get_runtime_state",
                        lambda: {"desired": "stopped", "phase": "stopped"})
    with _client() as c:
        r = c.get("/ready")
    assert r.status_code == 503
    assert r.get_json()["reason"] == "stopped"


def test_ready_never_names_a_provider(running, monkeypatch):
    _providers(monkeypatch, {"groq": False})
    with _client() as c:
        raw = c.get("/ready").get_data(as_text=True)
    assert "groq" not in raw


def test_readiness_failure_is_a_503_not_a_500(running, monkeypatch):
    def boom():
        raise RuntimeError("config unreadable")
    monkeypatch.setattr(app, "_enabled_keyed", boom)
    with _client() as c:
        r = c.get("/ready")
    assert r.status_code == 503
    assert r.get_json() == {"status": "not_ready", "reason": "error"}


def test_probes_keep_the_loopback_host_guard():
    with _client() as c:
        for path in ("/health", "/ready"):
            r = c.get(path, headers={"Host": "probe.example.com"})
            assert r.status_code == 403
