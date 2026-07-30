"""Kimi Code (kimi) CLI_REGISTRY entry — shape and detection logic.

Kimi Code is wired ONLY through ~/.kimi/config.toml ([providers.*] tables);
its documented credential priority is config api_key > [providers.*.env],
with NO shell-environment fallback — so the entry must detect connected state
from the config file alone and never trust OPENAI_* env vars (the codex
false-positive precedent). No network, no real filesystem: configs live in
tempfile.mkdtemp(prefix="hub-pytest-") dirs.
"""
import os
import shutil
import tempfile

import app

HUB_V1 = "http://127.0.0.1:%d/v1" % app.PORT

# The shape the manual_note tells the user to paste (official docs:
# api_key is REQUIRED — Kimi Code fails to start without one).
HUB_CONFIG = (
    'default_model = "auto"\n'
    '\n'
    '[providers.free-hub]\n'
    'type = "openai"\n'
    'base_url = "%s"\n'
    'api_key = "free-llm-hub"\n'
    '\n'
    '[models."auto"]\n'
    'provider = "free-hub"\n'
    'model = "auto"\n'
    'max_context_size = 128000\n'
) % HUB_V1

# This machine's real current config: the managed OAuth service, NOT the hub.
MANAGED_CONFIG = (
    'default_model = "kimi-code/kimi-for-coding"\n'
    '\n'
    '[providers."managed:kimi-code"]\n'
    'type = "kimi"\n'
    'base_url = "https://api.kimi.com/coding/v1"\n'
    'api_key = ""\n'
)


def _entry_with_config(path):
    """The real kimi registry entry, re-pointed at a temp config file."""
    entry = dict(app._get_cli_entry("kimi"))
    entry["config_paths"] = [path]
    return entry


def _write_config(text):
    d = tempfile.mkdtemp(prefix="hub-pytest-")
    path = os.path.join(d, "config.toml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def test_kimi_entry_registered_with_sane_fields():
    e = app._get_cli_entry("kimi")
    assert e is not None, "kimi missing from CLI_REGISTRY"
    assert e["name"] == "Kimi Code"
    assert e["kind"] == "openai"
    assert e["bins"] == ["kimi"]
    assert len(e["config_paths"]) == 1
    assert e["config_paths"][0].endswith(os.path.join(".kimi", "config.toml"))
    # TOML config surface, no shell-env wiring -> manual method, no env_check,
    # and listed among the CLIs that must NOT get an env-var block.
    assert not e.get("env_check")
    assert e.get("autofix") is None
    assert e["default_method"] == "manual"
    assert "kimi" in app._ENVLESS_CLIS


def test_kimi_manual_note_carries_the_official_toml_shape():
    note = app._get_cli_entry("kimi")["manual_note"]
    assert "[providers.free-hub]" in note
    assert 'type = "openai"' in note
    assert "127.0.0.1:%d/v1" % app.PORT in note
    # api_key is REQUIRED by Kimi Code (startup fails without it) — the note
    # must hand the user a placeholder, not omit the field.
    assert 'api_key = "free-llm-hub"' in note
    # The 'auto' alias -> the hub's difficulty-aware orchestration.
    assert '[models."auto"]' in note
    # The anthropic-style alternative (hub serves /v1/messages) is mentioned.
    assert "anthropic" in note and "/v1/messages" in note


def test_kimi_connected_via_config_pointed_at_hub():
    entry = _entry_with_config(_write_config(HUB_CONFIG))
    connected, method, detail = app._cli_connected(entry)
    assert connected is True
    assert method == "config"
    assert "config.toml" in detail


def test_kimi_not_connected_when_pointed_at_managed_service():
    entry = _entry_with_config(_write_config(MANAGED_CONFIG))
    connected, method, detail = app._cli_connected(entry)
    assert connected is False
    assert method == "manual"      # entry default
    assert detail is None


def test_kimi_missing_config_reads_as_not_connected():
    d = tempfile.mkdtemp(prefix="hub-pytest-")
    entry = _entry_with_config(os.path.join(d, "config.toml"))  # absent file
    connected, method, _ = app._cli_connected(entry)
    assert connected is False
    assert method == "manual"


def test_kimi_ignores_openai_env_vars(monkeypatch):
    # Shell OPENAI_BASE_URL pointing at the hub must NOT connect kimi — it has
    # no env_check because the CLI never reads shell env for custom providers
    # (same false-positive class as the codex OPENAI_BASE_URL bug).
    monkeypatch.setenv("OPENAI_BASE_URL", HUB_V1)
    monkeypatch.setenv("OPENAI_API_BASE", HUB_V1)
    d = tempfile.mkdtemp(prefix="hub-pytest-")
    entry = _entry_with_config(os.path.join(d, "config.toml"))
    connected, method, _ = app._cli_connected(entry)
    assert connected is False
    assert method == "manual"


def test_kimi_installed_detection(monkeypatch):
    entry = app._get_cli_entry("kimi")
    monkeypatch.setattr(shutil, "which", lambda b: "/usr/local/bin/kimi" if b == "kimi" else None)
    installed, path = app._cli_installed(entry)
    assert installed is True
    assert path == "/usr/local/bin/kimi"
    # No config_means_installed on this entry: without the bin, an existing
    # config alone does NOT count as installed (claude/aider/qwen precedent).
    monkeypatch.setattr(shutil, "which", lambda b: None)
    entry2 = _entry_with_config(_write_config(HUB_CONFIG))
    installed2, _ = app._cli_installed(entry2)
    assert installed2 is False
