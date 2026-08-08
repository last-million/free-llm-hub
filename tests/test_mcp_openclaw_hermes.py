"""Register the hub's crew tools in OpenClaw and Hermes too.

USER 2026-08-08: "do it please 100% in all CLIs and also inside openclaw and
hermes agent and buzz agent too."

Two of the three were implementable. Researched and adversarially reviewed
before writing a single byte, because this module WRITES THE USER'S REAL
CONFIG FILES and a wrong schema corrupts a tool they depend on.

OPENCLAW -- container is NESTED ("mcp": {"servers": {...}}), unlike every
other supported CLI's flat top-level key, and the transport goes under
"transport": "streamable-http". NOT claude's "type": "http"; omitting it makes
OpenClaw silently fall back to sse, which the hub does not serve.

  The config PATH deliberately differs from app.py's _p_openclaw(), which
  resolves ~/openclaw-config/openclaw.json. On this machine that directory is
  a DEPLOY-STAGING dir whose deploy.py SSHes the file into a remote VPS
  container -- nothing local reads it. Writing there would return ok=True and
  change nothing: the silent-success failure this module exists to prevent.

HERMES -- YAML, a format the module could not write at all before this. Key is
snake_case `mcp_servers` (NOT Claude Desktop's `mcpServers`), and there is no
"type" field: hermes_cli/mcp_config.py picks the transport by field presence,
so a bare `url:` means Streamable HTTP.

BUZZ -- deliberately NOT supported; see test_buzz_is_not_supported.
"""
import json
import os
import shutil
import tempfile

import pytest

import mcp_manager as m

HUB = {"url": "http://127.0.0.1:8787/mcp"}


@pytest.fixture
def home(monkeypatch):
    d = tempfile.mkdtemp(prefix="hub-pytest-mcpx-")
    monkeypatch.setenv("MCP_MANAGER_HOME", d)
    monkeypatch.setenv("HERMES_HOME", os.path.join(d, "hermes"))
    for v in ("OPENCLAW_CONFIG_PATH", "OPENCLAW_CONFIG",
              "OPENCLAW_STATE_DIR", "OPENCLAW_HOME"):
        monkeypatch.delenv(v, raising=False)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------- #
# OpenClaw
# --------------------------------------------------------------------------- #

def test_openclaw_writes_the_nested_container_and_transport(home):
    ok, msg = m.add_server("openclaw", "free-llm-hub", dict(HUB))
    assert ok, msg
    doc = json.load(open(m._config_path("openclaw"), encoding="utf-8"))
    entry = doc["mcp"]["servers"]["free-llm-hub"]
    assert entry["url"] == HUB["url"]
    assert entry["transport"] == "streamable-http", \
        "omitting transport makes OpenClaw fall back to sse, which we do not serve"
    assert "type" not in entry, "'type' is claude's dialect, not OpenClaw's"


def test_openclaw_never_targets_the_remote_deploy_staging_dir(home):
    """~/openclaw-config/ is a deploy-staging dir that ships to a remote VPS.
    Writing there succeeds and changes nothing locally."""
    assert "openclaw-config" not in m._config_path("openclaw")
    assert m._config_path("openclaw").endswith(
        os.path.join(".openclaw", "openclaw.json"))


def test_openclaw_env_overrides_win_in_documented_order(home, monkeypatch):
    monkeypatch.setenv("OPENCLAW_STATE_DIR", os.path.join(home, "state"))
    assert m._config_path("openclaw").endswith(
        os.path.join("state", "openclaw.json"))
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", os.path.join(home, "x", "custom.json"))
    assert m._config_path("openclaw").endswith("custom.json"), \
        "OPENCLAW_CONFIG_PATH must outrank OPENCLAW_STATE_DIR"


def test_openclaw_keeps_every_unrelated_key(home):
    p = m._config_path("openclaw")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"models": {"providers": {"freehub": {"baseUrl": "http://x/v1"}}},
                   "agents": {"defaults": {"model": {"primary": "freehub/auto"}}},
                   "mcp": {"servers": {"other": {"url": "http://other/mcp"}}}}, f)
    ok, msg = m.add_server("openclaw", "free-llm-hub", dict(HUB))
    assert ok, msg
    doc = json.load(open(p, encoding="utf-8"))
    assert doc["models"]["providers"]["freehub"]["baseUrl"] == "http://x/v1"
    assert doc["agents"]["defaults"]["model"]["primary"] == "freehub/auto"
    assert set(doc["mcp"]["servers"]) == {"other", "free-llm-hub"}
    assert os.path.exists(p + m._BACKUP_SUFFIX), "a one-time backup must exist"


# --------------------------------------------------------------------------- #
# Hermes (YAML)
# --------------------------------------------------------------------------- #

def test_hermes_creates_a_fresh_config_when_absent(home):
    """Hermes not being installed is the NORMAL case, not an error."""
    ok, msg = m.add_server("hermes", "free-llm-hub", dict(HUB))
    assert ok, msg
    text = open(m._config_path("hermes"), encoding="utf-8").read()
    assert text.startswith("mcp_servers:")
    assert 'url: "http://127.0.0.1:8787/mcp"' in text
    assert "mcpServers" not in text, "snake_case key; camelCase is Claude Desktop's"
    assert "type:" not in text, "hermes picks transport by field presence, not a type"


def test_hermes_merges_without_disturbing_sibling_keys(home):
    p = m._config_path("hermes")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w", encoding="utf-8").write(
        'model:\n  provider: custom\n  base_url: "http://x/v1"\n\n'
        'mcp_servers:\n  other:\n    url: "http://other/mcp"\n\n'
        'logging:\n  level: info\n')
    ok, msg = m.add_server("hermes", "free-llm-hub", dict(HUB))
    assert ok, msg
    text = open(p, encoding="utf-8").read()
    for keep in ("provider: custom", 'base_url: "http://x/v1"',
                 "other:", "level: info"):
        assert keep in text, keep
    assert "free-llm-hub:" in text
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(text)
    assert set(doc) == {"model", "mcp_servers", "logging"}
    assert set(doc["mcp_servers"]) == {"other", "free-llm-hub"}
    assert doc["mcp_servers"]["free-llm-hub"]["url"] == HUB["url"]


def test_hermes_duplicate_needs_force(home):
    assert m.add_server("hermes", "free-llm-hub", dict(HUB))[0]
    ok, msg = m.add_server("hermes", "free-llm-hub", dict(HUB))
    assert not ok and msg == "exists"
    ok, _ = m.add_server("hermes", "free-llm-hub", dict(HUB, force=True))
    assert ok
    text = open(m._config_path("hermes"), encoding="utf-8").read()
    assert text.count("free-llm-hub:") == 1, "force must REPLACE, not duplicate"


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #

def test_buzz_is_not_supported():
    """block/buzz is real, but its persona layer parses stdio only:
    parse_mcp_server_config starts at config.get("command") and returns None
    without it, so an http entry registers ZERO servers while reporting
    success -- the worst possible outcome for this module. Upstream
    block/buzz#2899 also reports mcp_servers is never plumbed to the runtime.
    And Buzz drives claude/codex over ACP, both already supported."""
    assert "buzz" not in m.supported_clis()
    ok, msg = m.add_server("buzz", "free-llm-hub", dict(HUB))
    assert not ok and "unsupported" in msg


def test_openclaw_and_hermes_have_no_isolated_copy(home):
    """The hub only installs private copies of claude/codex/opencode/kimi;
    an isolated write for the others would target a path nothing reads."""
    for cli in ("openclaw", "hermes"):
        assert m._config_path(cli, isolated=True) is None
        ok, msg = m.add_server(cli, "free-llm-hub", dict(HUB), isolated=True)
        assert not ok, "an isolated write must fail loudly, not silently no-op"


def test_both_are_listed_as_supported():
    for cli in ("openclaw", "hermes"):
        assert cli in m.supported_clis()
