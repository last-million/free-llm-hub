"""Tests for GET /api/settings/export + POST /api/settings/import (app.py) --
a portable backup/restore of config.py's actual config.json state. See
app.py's own module comment above these routes for what's deliberately
excluded (hub_mode/runtime lifecycle state, control_token, conversation
history) and why.
"""
import pytest

import app
import config

_DASH = {"X-Free-LLM-Hub": "dashboard"}


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    path = tmp_path / "state" / "config.json"
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(path))
    return path


def _seed_rich_config():
    """Populate every exportable section with real, non-default values."""
    config.set_provider_config("groq", enabled=True, base_url=None)
    config.add_provider_key("groq", "key-one")
    config.add_provider_key("groq", "key-two")
    config.set_provider_config("cloudflare", enabled=True,
                               base_url="https://api.cloudflare.com/accounts/xyz/ai/v1")
    config.add_provider_key("cloudflare", "cf-token")
    config.set_flag("agentic_chat_enabled", True)
    config.set_flag("sub_claude_isolated", True)
    config.set_default("groq", "llama-3.3-70b-versatile")
    config.set_local_api_key("local-bearer-abc")
    state = config.get_media_state()
    config.update_media_state(state["revision"], lambda cur: {
        **cur, "priority_mode": "manual", "manual_priority": ["google/gemini-3.5-flash"]})
    state = config.get_images_state()
    config.update_images_state(state["revision"], lambda cur: {
        **cur, "priority_mode": "manual", "manual_priority": ["cloudflare/some-model"]})


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #

def test_export_default_includes_all_sections(isolated_config):
    _seed_rich_config()
    client = app.app.test_client()
    resp = client.get("/api/settings/export")
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body["sections"]) == {"api_keys", "flags", "default", "local_api_key",
                                     "media", "images"}
    assert body["api_keys"]["groq"]["api_keys"] == ["key-one", "key-two"]
    assert body["api_keys"]["groq"]["enabled"] is True
    assert body["api_keys"]["cloudflare"]["base_url"] == \
        "https://api.cloudflare.com/accounts/xyz/ai/v1"
    assert body["flags"]["agentic_chat_enabled"] is True
    assert body["flags"]["sub_claude_isolated"] is True
    assert body["default"] == {"provider": "groq", "model": "llama-3.3-70b-versatile"}
    assert body["local_api_key"] == "local-bearer-abc"
    assert body["media"] == {"priority_mode": "manual", "manual_priority": ["google/gemini-3.5-flash"]}
    assert body["images"] == {"priority_mode": "manual", "manual_priority": ["cloudflare/some-model"]}
    # Excluded runtime/lifecycle sections never leak into the export.
    assert "hub_mode" not in body
    assert "runtime" not in body
    assert "control_token" not in body


def test_export_explicit_all_matches_default(isolated_config):
    _seed_rich_config()
    client = app.app.test_client()
    default_body = client.get("/api/settings/export").get_json()
    all_body = client.get("/api/settings/export?sections=all").get_json()
    assert default_body["sections"] == all_body["sections"]


def test_export_section_subset(isolated_config):
    _seed_rich_config()
    client = app.app.test_client()
    resp = client.get("/api/settings/export?sections=flags,default")
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body["sections"]) == {"flags", "default"}
    assert "api_keys" not in body
    assert "local_api_key" not in body
    assert "media" not in body
    assert "images" not in body
    assert "flags" in body and "default" in body


@pytest.mark.parametrize("section", ["api_keys", "flags", "default", "local_api_key",
                                     "media", "images"])
def test_export_each_individual_section_selector(isolated_config, section):
    """Requesting exactly ONE section returns ONLY that section's key, with
    the real shape config.py actually stores (not a guessed/reshaped one)."""
    _seed_rich_config()
    client = app.app.test_client()
    resp = client.get("/api/settings/export?sections=" + section)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["sections"] == [section]
    assert section in body
    other_sections = set(app._SETTINGS_SECTIONS) - {section}
    assert not (other_sections & set(body.keys()))


def test_export_rejects_unknown_section(isolated_config):
    client = app.app.test_client()
    resp = client.get("/api/settings/export?sections=bogus")
    assert resp.status_code == 400


def test_export_never_includes_control_token_or_lifecycle_state(isolated_config):
    """Even asking for 'all' must not leak the per-install control-token secret
    or process-lifecycle state -- these are excluded on purpose, not merely
    absent from _seed_rich_config()."""
    token = config.ensure_control_token()
    client = app.app.test_client()
    # Once a control token exists, EVERY /api/* request (GET included) needs
    # it -- see _local_control_guard in app.py -- so supply it here; that
    # guard is not what this test is about.
    body = client.get("/api/settings/export?sections=all&token=" + token).get_json()
    assert "control_token" not in body
    assert "hub_mode" not in body
    assert "runtime" not in body
    assert "schema_version" in body  # top-level metadata, not a "section" -- fine to include


# --------------------------------------------------------------------------- #
# Import -- round trip
# --------------------------------------------------------------------------- #

def test_import_round_trips_full_export(isolated_config, tmp_path, monkeypatch):
    _seed_rich_config()
    client = app.app.test_client()
    exported = client.get("/api/settings/export?sections=all").get_json()

    # Switch to a completely FRESH config (a different machine).
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(tmp_path / "state2" / "config.json"))
    fresh_client = app.app.test_client()
    resp = fresh_client.post("/api/settings/import", json=exported, headers=_DASH)
    assert resp.status_code == 200
    assert set(resp.get_json()["imported"]) == {"api_keys", "flags", "default",
                                                 "local_api_key", "media", "images"}

    reimported = fresh_client.get("/api/settings/export?sections=all").get_json()
    assert reimported["api_keys"] == exported["api_keys"]
    assert reimported["flags"] == exported["flags"]
    assert reimported["default"] == exported["default"]
    assert reimported["local_api_key"] == exported["local_api_key"]
    assert reimported["media"] == exported["media"]
    assert reimported["images"] == exported["images"]


def test_import_clears_default_and_local_key_when_null(isolated_config, tmp_path, monkeypatch):
    """A source machine with NO default/local key configured must be able to
    overwrite a target machine's existing values back to 'unset'."""
    config.set_default("groq", "some-model")
    config.set_local_api_key("preexisting-key")
    client = app.app.test_client()
    resp = client.post("/api/settings/import",
                       json={"default": None, "local_api_key": None}, headers=_DASH)
    assert resp.status_code == 200
    assert config.get_default() is None
    assert config.get_local_api_key() is None


def test_import_explicit_null_base_url_clears_existing_one(isolated_config):
    """Distinct from 'base_url absent -> preserve': the source machine had NO
    custom base_url (exports it as null) and that must overwrite a target
    machine's existing custom base_url back to unset, not silently leave it."""
    config.set_provider_config("cloudflare", enabled=True,
                               base_url="https://stale.example/accounts/old/ai/v1")
    client = app.app.test_client()
    resp = client.post("/api/settings/import",
                       json={"api_keys": {"cloudflare": {"enabled": True, "base_url": None}}},
                       headers=_DASH)
    assert resp.status_code == 200
    assert config.get_provider_config("cloudflare")["base_url"] is None


def test_import_round_trips_no_key_provider_with_empty_key_pool(isolated_config, tmp_path, monkeypatch):
    """A Pollinations-style no-key provider: enabled, base_url unset, and a
    genuinely EMPTY api_keys list -- the shape _seed_rich_config() never
    exercises since both its providers carry real keys."""
    config.set_provider_config("groq", enabled=True, base_url=None)
    # api_keys deliberately left empty (no add_provider_key call).
    client = app.app.test_client()
    exported = client.get("/api/settings/export?sections=api_keys").get_json()
    assert exported["api_keys"]["groq"] == {"enabled": True, "base_url": None, "api_keys": []}

    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(tmp_path / "state3" / "config.json"))
    fresh_client = app.app.test_client()
    resp = fresh_client.post("/api/settings/import", json=exported, headers=_DASH)
    assert resp.status_code == 200
    reimported = fresh_client.get("/api/settings/export?sections=api_keys").get_json()
    assert reimported["api_keys"] == exported["api_keys"]


def test_import_partial_provider_row_preserves_untouched_fields(isolated_config):
    """A row that only carries 'enabled' must NOT wipe an existing key pool."""
    config.set_provider_config("groq", enabled=True)
    config.add_provider_key("groq", "existing-key")
    client = app.app.test_client()
    resp = client.post("/api/settings/import",
                       json={"api_keys": {"groq": {"enabled": False}}}, headers=_DASH)
    assert resp.status_code == 200
    row = config.get_provider_config("groq")
    assert row["enabled"] is False
    assert row["api_keys"] == ["existing-key"]  # untouched -- 'api_keys' key was absent


# --------------------------------------------------------------------------- #
# Import -- validation / all-or-nothing
# --------------------------------------------------------------------------- #

def test_import_rejects_non_object_body(isolated_config):
    client = app.app.test_client()
    resp = client.post("/api/settings/import", data="not json",
                       content_type="application/json", headers=_DASH)
    assert resp.status_code == 400


def test_import_rejects_body_with_no_recognized_sections(isolated_config):
    client = app.app.test_client()
    resp = client.post("/api/settings/import", json={"totally_unrelated": 1}, headers=_DASH)
    assert resp.status_code == 400


def test_import_rejects_malformed_section_without_writing_anything(isolated_config):
    config.set_flag("agentic_chat_enabled", True)
    config.set_default("groq", "some-model")
    client = app.app.test_client()
    # 'flags' is well-formed but 'default' is malformed (missing 'model') --
    # the WHOLE import must be rejected, including the well-formed section.
    resp = client.post("/api/settings/import",
                       json={"flags": {"use_local_subscriptions": True},
                             "default": {"provider": "groq"}},
                       headers=_DASH)
    assert resp.status_code == 400
    # Nothing was written -- pre-existing state is untouched.
    assert config.get_flag("agentic_chat_enabled", False) is True
    assert config.get_flag("use_local_subscriptions", False) is False
    assert config.get_default() == {"provider": "groq", "model": "some-model"}


def test_import_rejects_non_bool_flag_value(isolated_config):
    client = app.app.test_client()
    resp = client.post("/api/settings/import",
                       json={"flags": {"agentic_chat_enabled": "yes"}}, headers=_DASH)
    assert resp.status_code == 400


def test_import_rejects_bad_media_priority_mode(isolated_config):
    client = app.app.test_client()
    resp = client.post("/api/settings/import",
                       json={"media": {"priority_mode": "sideways"}}, headers=_DASH)
    assert resp.status_code == 400


def test_import_flags_cannot_clobber_structural_section(isolated_config):
    """A crafted 'flags' payload naming a reserved structural key must be
    silently dropped, never applied as if it were a real boolean flag."""
    client = app.app.test_client()
    resp = client.post("/api/settings/import",
                       json={"flags": {"providers": True, "agentic_chat_enabled": True}},
                       headers=_DASH)
    assert resp.status_code == 200
    assert resp.get_json()["imported"] == ["flags"]
    assert config.get_flag("agentic_chat_enabled", False) is True
    # 'providers' was never applied via set_flag -- the real providers dict
    # (a dict, not a bool) must be untouched.
    assert isinstance(config.load_config()["providers"], dict)


def test_import_requires_dashboard_header(isolated_config):
    client = app.app.test_client()
    resp = client.post("/api/settings/import", json={"flags": {"agentic_chat_enabled": True}})
    assert resp.status_code == 403  # local-control-guard header check
