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


# --------------------------------------------------------------------------- #
# list_servers must survive both new formats and the no-isolated-copy case.
# Both of these were real bugs: the yaml branch was missing (hermes entries
# were written but listed as empty), and a guard written for add/remove was
# pasted into list_servers -- which returns a DICT, so it returned a tuple AND
# truncated the listing at the first CLI without an isolated copy.
# --------------------------------------------------------------------------- #

def test_list_servers_reads_the_yaml_backend(home):
    app._ensure_mcp_servers_once()
    listed = m.list_servers()
    assert isinstance(listed, dict)
    names = [e["name"] for e in listed["hermes"]]
    assert "context7" in names and "free-llm-hub" in names, \
        "hermes entries are written but not listed -- the yaml branch is missing"


def test_list_servers_isolated_returns_a_dict_for_every_cli(home):
    app._ensure_mcp_servers_once()
    listed = m.list_servers(isolated=True)
    assert isinstance(listed, dict), "must never return add_server's (ok, msg) tuple"
    for cli in m.supported_clis():
        assert cli in listed, "%s missing -- the listing was truncated early" % cli
    for cli in ("codex", "claude", "opencode", "kimi"):
        assert [e["name"] for e in listed[cli]], cli
    for cli in ("openclaw", "hermes"):
        assert listed[cli] == [], "no isolated copy -> empty list, not an error"


# --------------------------------------------------------------------------- #
# playwright: the always-on set's only STDIO server. Three real bugs came out
# of adding it, each of which produced a config that looked fine.
# --------------------------------------------------------------------------- #

def test_playwright_is_registered_as_stdio_not_as_a_url(home):
    """THE BUG: the hub-url sentinel was {"url": None}, and the fill-in tested
    `spec.get("url") is None` -- which is ALSO true for a stdio spec that has
    no "url" key at all. Playwright was getting the hub's URL bolted on and
    registered in every CLI as an HTTP server pointing at the hub."""
    app._ensure_mcp_servers_once()
    for cli in m.supported_clis():
        entry = next(e for e in m.list_servers()[cli] if e["name"] == "playwright")
        assert entry.get("transport") != "http", \
            "%s registered a browser subprocess as an HTTP server" % cli
        assert not entry.get("url"), cli
        text = open(m._config_path(cli), encoding="utf-8").read()
        assert "npx" in text, cli
        # The hub URL must appear ONLY for free-llm-hub, never on playwright.
        assert text.count("127.0.0.1:8787") == 1, \
            "%s: the hub url leaked onto another server" % cli


def test_openclaw_stdio_omits_transport_and_uses_a_string_command(home):
    """Verified against OpenClaw's zod schema: stdio is detected by the
    presence of `command` (a STRING, not opencode's array), and `transport`
    is omitted -- the sse fallback applies only to url-bearing servers. The
    schema ends in .catchall(z.unknown()), so a wrong key would pass
    validation silently instead of erroring."""
    app._ensure_mcp_servers_once()
    import json
    doc = json.load(open(m._config_path("openclaw"), encoding="utf-8"))
    entry = doc["mcp"]["servers"]["playwright"]
    assert entry["command"] == "npx", "must be a string, not an array"
    assert entry["args"] == ["-y", "@playwright/mcp@latest"]
    assert "transport" not in entry, "stdio is detected by `command`; sse fallback is url-only"
    assert "type" not in entry, "'type' is claude's dialect and would pass silently"
    # the remote servers in the same file DO carry a transport
    assert doc["mcp"]["servers"]["context7"]["transport"] == "streamable-http"


def test_a_nested_yaml_key_is_not_parsed_as_a_server(home):
    """THE BUG: the entry-name regex allowed a space in the name class, so
    "^  " matched the first two spaces and the class swallowed the rest of a
    deeper indent -- playwright's own `args:` line parsed as a SECOND server
    called "args"."""
    app._ensure_mcp_servers_once()
    names = {e["name"] for e in m.list_servers()["hermes"]}
    assert names == {"free-llm-hub", "context7", "playwright"}, names
    assert "args" not in names and "command" not in names
