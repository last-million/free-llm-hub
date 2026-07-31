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
    # TOML config surface, no shell-env wiring -> config method, no env_check,
    # and still listed among the CLIs that must NOT get an env-var block.
    assert not e.get("env_check")
    assert e["default_method"] == "config"
    assert "kimi" in app._ENVLESS_CLIS
    # One-click since 2026-07-31: a real autofix strategy + reverter, both
    # writing the SAME file the entry advertises.
    assert e.get("autofix") == "kimi"
    assert e["write_path"].endswith(os.path.join(".kimi", "config.toml"))
    assert app._AUTOFIXERS["kimi"] is app._autofix_kimi
    assert app._DISCONNECTERS["kimi"] is app._disconnect_kimi


def test_kimi_manual_note_still_carries_the_official_toml_shape():
    """The one-click path did NOT delete the manual fallback instructions."""
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
    assert method == "config"      # entry default      # entry default
    assert detail is None


def test_kimi_missing_config_reads_as_not_connected():
    d = tempfile.mkdtemp(prefix="hub-pytest-")
    entry = _entry_with_config(os.path.join(d, "config.toml"))  # absent file
    connected, method, _ = app._cli_connected(entry)
    assert connected is False
    assert method == "config"      # entry default


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
    assert method == "config"      # entry default


# --------------------------------------------------------------------------
# One-click Connect / Disconnect (2026-07-31). Every write goes to a temp
# config.toml and the two config.py settings calls are stubbed, so a test run
# can never touch the real ~/.kimi/config.toml or the hub's own config.
# --------------------------------------------------------------------------

def _patch_kimi_paths(monkeypatch, path):
    """Re-point _p_kimi() at a temp file and stub the setting store.
    Returns the dict standing in for the persisted settings."""
    store = {}
    monkeypatch.setattr(app, "_p_kimi", lambda: path)
    monkeypatch.setattr(app.config, "set_setting",
                        lambda name, value: store.__setitem__(name, value))
    monkeypatch.setattr(app.config, "get_setting",
                        lambda name, default=None: store.get(name, default))
    return store


def _kimi_entry_at(path):
    entry = dict(app._get_cli_entry("kimi"))
    entry["config_paths"] = [path]
    entry["write_path"] = path
    return entry


def _connect(entry):
    return app._autofix_kimi(entry, "free-llm-hub",
                             "http://127.0.0.1:%d" % app.PORT, HUB_V1, "auto")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_kimi_autofix_writes_valid_toml_and_connects(monkeypatch):
    path = _write_config(MANAGED_CONFIG)
    store = _patch_kimi_paths(monkeypatch, path)
    res = _connect(_kimi_entry_at(path))
    assert res["ok"] is True
    text = _read(path)
    # Parses as real TOML with exactly the documented shape.
    import tomllib
    data = tomllib.loads(text)
    assert data["default_model"] == "auto"
    assert data["providers"]["free-hub"] == {
        "type": "openai", "base_url": HUB_V1, "api_key": "free-llm-hub"}
    assert data["models"]["auto"] == {
        "provider": "free-hub", "model": "auto", "max_context_size": 128000}
    # The user's pre-existing managed provider survives untouched...
    assert data["providers"]["managed:kimi-code"]["base_url"] == "https://api.kimi.com/coding/v1"
    # ...and the clobbered default_model is remembered for Disconnect.
    assert store["kimi_prev_default_model"] == "kimi-code/kimi-for-coding"
    connected, method, _ = app._cli_connected(_kimi_entry_at(path))
    assert connected is True and method == "config"


def test_kimi_disconnect_restores_previous_default_model(monkeypatch):
    path = _write_config(MANAGED_CONFIG)
    _patch_kimi_paths(monkeypatch, path)
    entry = _kimi_entry_at(path)
    _connect(entry)
    out = app._disconnect_kimi(entry)
    assert out["changed"] is True
    text = _read(path)
    import tomllib
    data = tomllib.loads(text)
    assert data["default_model"] == "kimi-code/kimi-for-coding"   # exactly as before
    assert "free-hub" not in data.get("providers", {})
    assert "auto" not in data.get("models", {})
    assert data["providers"]["managed:kimi-code"]["type"] == "kimi"
    assert "127.0.0.1:%d" % app.PORT not in text
    connected, _, _ = app._cli_connected(_kimi_entry_at(path))
    assert connected is False


def test_kimi_connect_is_idempotent_and_keeps_later_user_tables(monkeypatch):
    path = _write_config(MANAGED_CONFIG)
    _patch_kimi_paths(monkeypatch, path)
    entry = _kimi_entry_at(path)
    _connect(entry)
    # The user adds their own provider AFTER connecting.
    with open(path, "a", encoding="utf-8") as f:
        f.write('\n[providers.mine]\ntype = "openai"\nbase_url = "https://x.example/v1"\napi_key = "k"\n')
    _connect(entry)                      # re-connect (e.g. after a port change)
    import tomllib
    data = tomllib.loads(_read(path))
    assert _read(path).count("[providers.free-hub]") == 1   # rewritten, not duplicated
    assert data["providers"]["mine"]["base_url"] == "https://x.example/v1"
    app._disconnect_kimi(entry)
    data2 = tomllib.loads(_read(path))
    assert data2["providers"]["mine"]["base_url"] == "https://x.example/v1"  # survives revert
    assert "free-hub" not in data2.get("providers", {})


def test_kimi_connect_from_no_config_file_at_all(monkeypatch):
    d = tempfile.mkdtemp(prefix="hub-pytest-")
    path = os.path.join(d, "config.toml")          # does not exist yet
    store = _patch_kimi_paths(monkeypatch, path)
    res = _connect(_kimi_entry_at(path))
    assert res["ok"] is True
    import tomllib
    data = tomllib.loads(_read(path))
    assert data["default_model"] == "auto"
    assert data["providers"]["free-hub"]["base_url"] == HUB_V1
    # Nothing was clobbered -> nothing to remember.
    assert "kimi_prev_default_model" not in store
    # Disconnect drops our default_model line entirely rather than inventing one.
    app._disconnect_kimi(_kimi_entry_at(path))
    assert "default_model" not in tomllib.loads(_read(path))


def test_kimi_autofix_never_echoes_the_key(monkeypatch):
    path = _write_config(MANAGED_CONFIG)
    _patch_kimi_paths(monkeypatch, path)
    res = app._autofix_kimi(_kimi_entry_at(path), "super-secret-hub-key",
                            "http://127.0.0.1:%d" % app.PORT, HUB_V1, "auto")
    assert "super-secret-hub-key" not in repr(res)          # masked in the response...
    assert 'api_key = "super-secret-hub-key"' in _read(path)  # ...but written to the config


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
