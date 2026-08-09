"""A CLI's own MCP registration for this hub must never look like a leftover
model-provider connection.

MEASURED LIVE 2026-08-09: a user disconnected Codex from the hub (the model
provider wiring). The UI reported "disconnected in config -- but it still
reports as connected" anyway, because the SAME config.toml also carries an
MCP server registration for this hub (a separate, deliberately-persistent
feature -- see the comment in app._disconnect_codex). The "still connected?"
recheck did a blind whole-file substring scan for the hub's origin, which
still matched the MCP entry's url even after the real provider wiring was
correctly removed.
"""
import os
import tempfile

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
