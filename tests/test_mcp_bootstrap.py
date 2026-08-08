"""Every agent should always have the hub's crews AND live documentation.

USER 2026-08-08: "give the local hub the mcp context7 ALWAYS so he can always
get last documentations and also repositories."

Doing that once by hand would drift: a CLI installed later, a reset config, or
a fresh machine silently loses it. _ensure_mcp_servers_once runs at every boot
so the claim stays true rather than being true once.

Two servers, for two different reasons:
  free-llm-hub  the hub's own crews (crew_run/crew_start/crew_result), so
                "use the crew agents" is a real tool in any CLI
  context7      live library docs + repo lookup, so an agent works from
                CURRENT documentation instead of its training cutoff
"""
import os
import shutil
import tempfile

import pytest

import app
import mcp_manager as m


@pytest.fixture
def home(monkeypatch):
    d = tempfile.mkdtemp(prefix="hub-pytest-mcpboot-")
    monkeypatch.setenv("MCP_MANAGER_HOME", d)
    monkeypatch.setenv("HERMES_HOME", os.path.join(d, "hermes"))
    for v in ("OPENCLAW_CONFIG_PATH", "OPENCLAW_CONFIG",
              "OPENCLAW_STATE_DIR", "OPENCLAW_HOME"):
        monkeypatch.delenv(v, raising=False)
    # The repo-local .opencode/opencode.json would otherwise win over the
    # temp home and let the test write into the real repo.
    monkeypatch.setattr(m, "_REPO_DIR", os.path.join(d, "repo"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_both_servers_are_registered_everywhere(home):
    app._ensure_mcp_servers_once()
    for cli in m.supported_clis():
        path = m._config_path(cli)
        assert os.path.isfile(path), "%s config not written" % cli
        text = open(path, encoding="utf-8").read()
        assert "free-llm-hub" in text, cli
        assert "context7" in text, cli
        assert "mcp.context7.com" in text, cli


def test_the_isolated_copies_get_them_too(home):
    """The /agent sessions run against the isolated configs, so a global-only
    registration would leave the hub's own agent chat without either tool."""
    app._ensure_mcp_servers_once()
    for cli in ("codex", "claude", "opencode", "kimi"):
        path = m._config_path(cli, isolated=True)
        assert os.path.isfile(path), "%s isolated config not written" % cli
        text = open(path, encoding="utf-8").read()
        assert "free-llm-hub" in text and "context7" in text, cli


def test_running_twice_changes_nothing(home):
    """Boot-time work must be idempotent: an existing entry reports 'exists'
    and is left alone, never duplicated."""
    app._ensure_mcp_servers_once()
    snapshot = {cli: open(m._config_path(cli), encoding="utf-8").read()
                for cli in m.supported_clis()}
    app._ensure_mcp_servers_once()
    for cli, before in snapshot.items():
        assert open(m._config_path(cli), encoding="utf-8").read() == before, cli


def test_a_users_own_servers_are_never_disturbed(home):
    """The bootstrap is additive -- an unrelated MCP server the user added by
    hand must survive it."""
    p = m._config_path("codex")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w", encoding="utf-8").write(
        'model = "auto"\n\n[mcp_servers.playwright]\ncommand = "npx"\n')
    app._ensure_mcp_servers_once()
    text = open(p, encoding="utf-8").read()
    assert "[mcp_servers.playwright]" in text
    assert 'model = "auto"' in text
    assert "free-llm-hub" in text and "context7" in text


def test_bootstrap_never_raises(home, monkeypatch):
    """It runs on the boot thread; an exception must not take startup with it."""
    def boom(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(m, "add_server", boom)
    app._ensure_mcp_servers_once()      # must simply return


def test_boot_order_installs_clis_before_registering_mcp(monkeypatch):
    """An isolated config dir does not exist until its CLI is installed, so
    registering first would silently skip the copies that matter most."""
    order = []
    monkeypatch.setattr(app, "_agent_cli_autoinstall_once",
                        lambda: order.append("install"))
    monkeypatch.setattr(app, "_ensure_mcp_servers_once",
                        lambda: order.append("mcp"))
    app._boot_agent_setup()
    assert order == ["install", "mcp"]


def test_boot_continues_to_mcp_even_if_cli_install_fails(monkeypatch):
    order = []

    def boom():
        raise RuntimeError("no npm")
    monkeypatch.setattr(app, "_agent_cli_autoinstall_once", boom)
    monkeypatch.setattr(app, "_ensure_mcp_servers_once",
                        lambda: order.append("mcp"))
    app._boot_agent_setup()
    assert order == ["mcp"], "a failed CLI install must not skip MCP setup"
