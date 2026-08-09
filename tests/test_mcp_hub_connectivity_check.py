"""A CLI's own MCP registration for this hub must never look like a leftover
model-provider connection.

MEASURED LIVE 2026-08-09, TWICE, in two different config formats: a user
disconnected Codex (TOML), then separately OpenCode (JSON) -- both times the
UI reported "disconnected in config -- but it still reports as connected",
because the SAME config file also carries an MCP server registration for
this hub (a separate, deliberately-persistent feature -- see the comment in
app._disconnect_codex). The "still connected?" recheck did a blind
whole-file substring scan for the hub's origin, which still matched the MCP
entry's url even after the real provider wiring was correctly removed. A
first fix only stripped the TOML shape (fixed Codex, left OpenCode/JSON
broken) -- _strip_hub_mcp_table now covers every format mcp_manager.py
itself writes: JSON (claude's flat mcpServers, opencode's flat mcp,
openclaw's nested mcp.servers), TOML (codex/kimi), and YAML (hermes).
"""
import json
import os
import tempfile

import pytest

import app

MCP_ONLY_TOML = """
[mcp_servers.free-llm-hub]
command = "npx"
args = ["-y", "mcp-remote", "http://127.0.0.1:%d/mcp"]

[mcp_servers.context7]
command = "npx"
args = ["context7"]
""" % app.PORT

PROVIDER_TOML = """
model_provider = "freehub"
model = "auto"

[model_providers.freehub]
name = "Calvoun Free LLM Hub"
base_url = "http://127.0.0.1:%d/v1"
""" % app.PORT

BOTH_TOML = MCP_ONLY_TOML + PROVIDER_TOML


def test_strip_removes_only_the_hub_mcp_table():
    stripped = app._strip_hub_mcp_table(MCP_ONLY_TOML)
    assert "127.0.0.1:%d" % app.PORT not in stripped
    assert "context7" in stripped, "an unrelated MCP server must survive the strip"


def test_strip_leaves_real_provider_wiring_alone():
    """The strip must remove ONLY the MCP table -- real model-provider wiring
    in the same file must still be detected as a real connection."""
    stripped = app._strip_hub_mcp_table(BOTH_TOML)
    assert "127.0.0.1:%d" % app.PORT in stripped, (
        "the [model_providers.freehub] table is a REAL connection and must survive")


def test_strip_is_best_effort_never_raises():
    # _remove_toml_server normalizes trailing newlines even on a no-op pass --
    # what matters is content survives untouched, not byte-for-byte identity.
    assert app._strip_hub_mcp_table("not even toml { } [[[").strip() == "not even toml { } [[["
    assert app._strip_hub_mcp_table("") == ""


def test_file_points_at_hub_ignores_mcp_only_registration():
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False,
                                     encoding="utf-8") as f:
        f.write(MCP_ONLY_TOML)
        path = f.name
    try:
        assert app._file_points_at_hub(path) is False, (
            "an MCP-only registration must not read as a model-provider connection")
    finally:
        os.remove(path)


def test_file_points_at_hub_still_detects_a_real_connection():
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False,
                                     encoding="utf-8") as f:
        f.write(BOTH_TOML)
        path = f.name
    try:
        assert app._file_points_at_hub(path) is True
    finally:
        os.remove(path)


def test_cli_connected_false_positive_reproduced_and_fixed():
    """The exact live-reported scenario: Codex's config.toml carries ONLY the
    hub's MCP registration (provider wiring already disconnected). Must read
    as not-connected."""
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False,
                                     encoding="utf-8") as f:
        f.write(MCP_ONLY_TOML)
        path = f.name
    try:
        entry = {"env_check": [], "config_paths": [path], "default_method": "config"}
        connected, method, detail = app._cli_connected(entry)
        assert connected is False, (
            "an MCP-only registration falsely reported as 'still connected'")
        assert method == "config"
    finally:
        os.remove(path)


def test_cli_connected_still_detects_a_real_leftover_connection():
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False,
                                     encoding="utf-8") as f:
        f.write(BOTH_TOML)
        path = f.name
    try:
        entry = {"env_check": [], "config_paths": [path], "default_method": "config"}
        connected, method, detail = app._cli_connected(entry)
        assert connected is True
        assert method == "config"
    finally:
        os.remove(path)


# --------------------------------------------------------------------------- #
# The SAME false positive, in every OTHER config format mcp_manager.py writes
# -- the exact live-reported OpenCode (JSON) regression against a TOML-only fix.
# --------------------------------------------------------------------------- #

HUB_URL = "http://127.0.0.1:%d/mcp" % app.PORT

CLAUDE_MCP_ONLY_JSON = json.dumps({
    "mcpServers": {
        "free-llm-hub": {"type": "http", "url": HUB_URL},
        "context7": {"type": "http", "url": "https://ctx7.example"},
    }
})

OPENCODE_MCP_ONLY_JSON = json.dumps({
    "mcp": {
        "free-llm-hub": {"type": "remote", "url": HUB_URL},
        "context7": {"type": "remote", "url": "https://ctx7.example"},
    }
})

OPENCLAW_MCP_ONLY_JSON = json.dumps({
    "mcp": {"servers": {
        "free-llm-hub": {"url": HUB_URL},
        "context7": {"url": "https://ctx7.example"},
    }}
})

HERMES_MCP_ONLY_YAML = (
    "model:\n  provider: custom\n"
    "mcp_servers:\n"
    "  free-llm-hub:\n"
    '    url: "%s"\n'
    "  context7:\n"
    '    command: "npx"\n'
) % HUB_URL


@pytest.mark.parametrize("fmt,text", [
    ("claude-json", CLAUDE_MCP_ONLY_JSON),
    ("opencode-json", OPENCODE_MCP_ONLY_JSON),
    ("openclaw-json", OPENCLAW_MCP_ONLY_JSON),
    ("hermes-yaml", HERMES_MCP_ONLY_YAML),
])
def test_strip_handles_every_config_format_mcp_only(fmt, text):
    stripped = app._strip_hub_mcp_table(text)
    assert "127.0.0.1:%d" % app.PORT not in stripped, (
        "%s: hub MCP entry survived the strip" % fmt)
    assert "context7" in stripped, "%s: sibling MCP server must survive" % fmt


@pytest.mark.parametrize("fmt,text,suffix", [
    ("claude-json", CLAUDE_MCP_ONLY_JSON, ".json"),
    ("opencode-json", OPENCODE_MCP_ONLY_JSON, ".json"),
    ("openclaw-json", OPENCLAW_MCP_ONLY_JSON, ".json"),
    ("hermes-yaml", HERMES_MCP_ONLY_YAML, ".yaml"),
])
def test_cli_connected_false_positive_fixed_for_every_format(fmt, text, suffix):
    """The exact live-reported OpenCode regression: an MCP-only registration,
    in EVERY format the hub writes, must never read as 'still connected'."""
    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False,
                                     encoding="utf-8") as f:
        f.write(text)
        path = f.name
    try:
        entry = {"env_check": [], "config_paths": [path], "default_method": "config"}
        connected, method, detail = app._cli_connected(entry)
        assert connected is False, "%s: falsely reported as still connected" % fmt
    finally:
        os.remove(path)
