"""Agent CLIs and subscription CLIs never inherit the hub's own port.

MEASURED 2026-10-08: run.bat exports PORT=8787 for the hub; an agent's
`npm run dev` (vite.config `port: process.env.PORT`) bound [::]:8787 next to
the hub, and the next restart refused to start because the port was taken.
"""
import agentic_chat
import app


def test_agent_env_drops_the_hub_port(monkeypatch):
    monkeypatch.setenv("PORT", "8787")
    monkeypatch.setenv("VITE_PORT", "8787")
    monkeypatch.setenv("FLASK_RUN_PORT", "8787")
    env = agentic_chat._agentic_env()
    for k in ("PORT", "VITE_PORT", "FLASK_RUN_PORT"):
        assert k not in env, k


def test_a_users_own_port_passes_through(monkeypatch):
    monkeypatch.setenv("PORT", "8787")
    monkeypatch.setenv("VITE_PORT", "3000")
    env = agentic_chat._agentic_env()
    assert "PORT" not in env
    assert env["VITE_PORT"] == "3000"


def test_a_custom_hub_port_is_the_one_dropped(monkeypatch):
    monkeypatch.setenv("PORT", "9100")              # the hub runs on 9100
    monkeypatch.setenv("FLASK_RUN_PORT", "8787")    # not the hub: keep it
    env = agentic_chat._agentic_env()
    assert "PORT" not in env
    assert env["FLASK_RUN_PORT"] == "8787"


def test_the_hub_process_keeps_its_own_port(monkeypatch):
    monkeypatch.setenv("PORT", "8787")
    agentic_chat._agentic_env()
    import os
    assert os.environ["PORT"] == "8787"             # only the child's copy changes


def test_subscription_cli_env_drops_the_hub_port(monkeypatch):
    monkeypatch.setenv("PORT", "8787")
    env = app._sub_env()
    assert "PORT" not in env
