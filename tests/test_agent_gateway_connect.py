"""Connect / disconnect round-trips for the OpenClaw + Hermes agent gateways.

These are the two message-gateway agents wired in CLI_REGISTRY. The contract each
must uphold: Auto-fix adds ONLY our hub provider/model block (merge-safe), and
Disconnect returns the file byte-for-byte to what it was — never clobbering the
user's channels, plugins, allowlist, or other settings.
"""
import json
import os

import yaml
import pytest

import app

KEY = "sk-local-test"
ROOT = "http://127.0.0.1:8787"
V1 = ROOT + "/v1"


def _openclaw_entry(path):
    return {"id": "openclaw", "write_path": path, "config_paths": [path],
            "config_means_installed": True, "bins": ["openclaw"]}


def _hermes_entry(path):
    return {"id": "hermes", "write_path": path, "config_paths": [path],
            "bins": ["hermes"], "env_check": []}


@pytest.fixture
def _local_settings(monkeypatch):
    """Isolate config.get_setting/set_setting (used to remember OpenClaw's primary)
    so tests never read or write the real hub config store."""
    store = {}
    monkeypatch.setattr(app.config, "set_setting", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(app.config, "get_setting", lambda k, d=None: store.get(k, d))
    return store


# --------------------------------------------------------------------------- #
# OpenClaw
# --------------------------------------------------------------------------- #
def _openclaw_original():
    return {
        "models": {"mode": "merge"},
        "agents": {
            "defaults": {
                "model": {"primary": "openai/gpt-5.2", "fallbacks": ["google/gemini-3-pro"]},
                "models": {"openai/gpt-5.2": {"alias": "ChatGPT"}},
            },
            "list": [{"id": "main"}],
        },
        "channels": {"whatsapp": {"dmPolicy": "pairing"}},
        "plugins": {"entries": {"discord": {"enabled": True}}},
    }


def test_openclaw_connect_adds_provider_allowlist_and_primary(tmp_path, monkeypatch, _local_settings):
    cfg = tmp_path / "openclaw.json"
    cfg.write_text(json.dumps(_openclaw_original()), encoding="utf-8")
    monkeypatch.setenv("OPENCLAW_CONFIG", str(cfg))
    entry = _openclaw_entry(str(cfg))

    r = app._autofix_openclaw(entry, KEY, ROOT, V1, "some/model")
    assert r["ok"], r
    after = json.loads(cfg.read_text(encoding="utf-8"))

    prov = after["models"]["providers"]["freehub"]
    assert prov["api"] == "openai-completions"
    assert prov["baseUrl"] == V1
    assert prov["apiKey"] == KEY
    assert prov["models"][0]["id"] == "auto"        # the hub auto-router sentinel
    assert after["models"]["mode"] == "merge"        # built-in providers kept
    assert after["agents"]["defaults"]["models"]["freehub/auto"]  # allowlisted
    assert after["agents"]["defaults"]["model"]["primary"] == "freehub/auto"
    # untouched user config
    assert after["channels"] == _openclaw_original()["channels"]
    assert after["plugins"] == _openclaw_original()["plugins"]
    assert _local_settings["openclaw_prev_primary"] == "openai/gpt-5.2"


def test_openclaw_disconnect_is_lossless(tmp_path, monkeypatch, _local_settings):
    original = _openclaw_original()
    cfg = tmp_path / "openclaw.json"
    cfg.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setenv("OPENCLAW_CONFIG", str(cfg))
    entry = _openclaw_entry(str(cfg))

    app._autofix_openclaw(entry, KEY, ROOT, V1, "some/model")
    d = app._disconnect_openclaw(entry)
    assert d["changed"]
    restored = json.loads(cfg.read_text(encoding="utf-8"))
    assert restored == original  # byte-for-byte back to the start


def test_openclaw_json5_comments_abort_safely(tmp_path, monkeypatch, _local_settings):
    cfg = tmp_path / "openclaw.json"
    cfg.write_text("{ // json5 comment\n  mode: 'merge'\n}", encoding="utf-8")
    monkeypatch.setenv("OPENCLAW_CONFIG", str(cfg))
    before = cfg.read_text(encoding="utf-8")
    r = app._autofix_openclaw(_openclaw_entry(str(cfg)), KEY, ROOT, V1, "some/model")
    assert not r["ok"]
    assert cfg.read_text(encoding="utf-8") == before  # never overwritten


# --------------------------------------------------------------------------- #
# Hermes
# --------------------------------------------------------------------------- #
def test_hermes_connect_creates_and_disconnect_deletes(tmp_path, monkeypatch):
    hdir = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(hdir))
    cfg = hdir / "config.yaml"
    entry = _hermes_entry(str(cfg))

    r = app._autofix_hermes(entry, KEY, ROOT, V1, "some/model")
    assert r["ok"] and cfg.exists()
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["model"] == {"provider": "custom", "base_url": V1,
                             "default": "auto", "api_key": KEY}

    d = app._disconnect_hermes(entry)
    assert d["changed"] and d.get("deleted") and not cfg.exists()


def test_hermes_preserves_other_settings(tmp_path, monkeypatch):
    hdir = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(hdir))
    hdir.mkdir()
    cfg = hdir / "config.yaml"
    original = {"soul": "keep me", "memory": {"enabled": True}}
    cfg.write_text(yaml.safe_dump(original), encoding="utf-8")
    entry = _hermes_entry(str(cfg))

    app._autofix_hermes(entry, KEY, ROOT, V1, "some/model")
    mid = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert "model" in mid and mid["soul"] == "keep me"

    app._disconnect_hermes(entry)
    after = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert after == original


def test_hermes_disconnect_leaves_foreign_endpoint_alone(tmp_path, monkeypatch):
    hdir = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(hdir))
    hdir.mkdir()
    cfg = hdir / "config.yaml"
    original = {"model": {"provider": "custom", "base_url": "http://example.com/v1",
                          "default": "gpt"}}
    cfg.write_text(yaml.safe_dump(original), encoding="utf-8")
    entry = _hermes_entry(str(cfg))

    d = app._disconnect_hermes(entry)
    assert not d["changed"]
    assert yaml.safe_load(cfg.read_text(encoding="utf-8")) == original


# --------------------------------------------------------------------------- #
# config_means_installed
# --------------------------------------------------------------------------- #
def test_config_means_installed_flag(tmp_path):
    cfg = tmp_path / "openclaw.json"
    cfg.write_text("{}", encoding="utf-8")
    fake_bin = "definitely-not-a-real-bin-xyz-123"
    with_flag = {"bins": [fake_bin], "config_paths": [str(cfg)], "config_means_installed": True}
    installed, path = app._cli_installed(with_flag)
    assert installed and path == str(cfg)
    without_flag = {"bins": [fake_bin], "config_paths": [str(cfg)]}
    assert app._cli_installed(without_flag) == (False, None)
