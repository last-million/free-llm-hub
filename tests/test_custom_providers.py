"""Custom providers (user-added), Settings -> "Add a provider" (2026-10-24).

Owner request: add ANY OpenAI-compatible endpoint by hand (name + base URL +
where to get a key), have it survive `git pull` (providers.py's PROVIDERS
dict is a GIT-TRACKED constant, overwritten on every auto-update), and have
it ride export/import like a real provider's key.

Storage: the DEFINITION lives in config.json's new top-level
`custom_providers` block (outside `providers`, same reasoning as the GitHub
token -- see AGENTS.md); the KEY reuses the ORDINARY per-provider key
machinery (config.get_provider_config/set_provider_config/add_provider_key),
landing in the SAME providers[<pid>] row a real provider's key would.

Merge point: providers.set_custom_source(fn), consulted by
get_provider/list_providers/base_url_for/signup_url/is_known_provider/
is_free_model -- every real app.py `prov.get_provider(...)` call site needed
no changes at all.

Hermetic: FREE_LLM_HUB_CONFIG points at a fresh temp file per test (same
pattern as tests/test_provider_out_reason.py); no network.
"""
import json
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

import app
import config
import migrations
import providers as prov

_DASH = {"X-Free-LLM-Hub": "dashboard"}


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    # tempfile.mkdtemp, not tmp_path: pytest's temp root is permission-broken
    # on this machine (see AGENTS.md).
    path = Path(tempfile.mkdtemp()) / "state" / "config.json"
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(path))
    with app._model_cache_lock:
        app._model_cache.clear()
        app._paid_model_cache.clear()
    yield


def _auth():
    return dict(_DASH, **{"X-Free-LLM-Hub-Token": config.ensure_control_token()})


def _client():
    return app.app.test_client()


def _create(body, client=None):
    client = client or _client()
    return client.post("/api/custom-providers", json=body, headers=_auth())


def _basic_body(name="My Test Provider", base_url="https://api.example.com/v1", **extra):
    body = {"name": name, "base_url": base_url}
    body.update(extra)
    return body


# --------------------------------------------------------------------------- #
# Create / list / edit / delete
# --------------------------------------------------------------------------- #
def test_create_returns_a_full_provider_card():
    r = _create(_basic_body())
    assert r.status_code == 201
    row = r.get_json()
    assert row["id"] == "my-test-provider"
    assert row["name"] == "My Test Provider"
    assert row["custom"] is True
    assert row["paid"] is False
    assert row["has_key"] is False


def test_list_shows_the_definition_not_the_full_card():
    _create(_basic_body(signup_url="https://example.com/key", key_hint="sk-...",
                         notes="hello"))
    r = _client().get("/api/custom-providers", headers=_auth())
    assert r.status_code == 200
    body = r.get_json()
    assert body["enabled"] is True
    rows = {p["id"]: p for p in body["providers"]}
    assert rows["my-test-provider"]["base_url"] == "https://api.example.com/v1"
    assert rows["my-test-provider"]["signup_url"] == "https://example.com/key"
    assert rows["my-test-provider"]["key_hint"] == "sk-..."
    assert rows["my-test-provider"]["notes"] == "hello"
    assert rows["my-test-provider"]["has_key"] is False
    assert "created_at" in rows["my-test-provider"]


def test_edit_changes_fields_without_touching_the_id():
    _create(_basic_body())
    r = _client().put("/api/custom-providers/my-test-provider",
                      json={"notes": "updated note"}, headers=_auth())
    assert r.status_code == 200
    assert r.get_json()["id"] == "my-test-provider"
    assert r.get_json()["notes"] == "updated note"
    # base_url untouched (not present in the PUT body) -- _provider_row has no
    # base_url field, so read it from the merge point / definition list.
    assert prov.get_provider("my-test-provider")["base_url"] == "https://api.example.com/v1"
    assert prov.get_provider("my-test-provider")["notes"] == "updated note"


def test_edit_unknown_id_is_404():
    r = _client().put("/api/custom-providers/does-not-exist",
                      json={"notes": "x"}, headers=_auth())
    assert r.status_code == 404


def test_delete_needs_confirm():
    _create(_basic_body())
    r = _client().delete("/api/custom-providers/my-test-provider", headers=_auth())
    assert r.status_code == 400
    assert r.get_json()["code"] == "confirm_required"
    # still there
    assert prov.get_provider("my-test-provider") is not None


def test_delete_also_removes_its_saved_key():
    _create(_basic_body())
    config.add_provider_key("my-test-provider", "sk-delete-me")
    assert config.get_provider_config("my-test-provider")["api_keys"] == ["sk-delete-me"]

    r = _client().delete("/api/custom-providers/my-test-provider",
                         json={"confirm": True}, headers=_auth())
    assert r.status_code == 200
    assert r.get_json() == {"deleted": "my-test-provider"}

    assert prov.get_provider("my-test-provider") is None
    assert config.get_custom_provider("my-test-provider") is None
    # the key pool is gone, not just "disabled"
    assert config.get_provider_config("my-test-provider")["api_keys"] == []


def test_delete_unknown_id_is_404():
    r = _client().delete("/api/custom-providers/does-not-exist",
                         json={"confirm": True}, headers=_auth())
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Id validation: collisions, slug auto-generation + dedupe, reserved words
# --------------------------------------------------------------------------- #
def test_id_collision_with_a_static_provider_is_rejected():
    r = _create({"id": "groq", "name": "x", "base_url": "https://x.example.com"})
    assert r.status_code == 400
    assert "groq" in r.get_json()["error"]


def test_id_collision_with_an_existing_custom_provider_is_rejected():
    _create({"id": "my-pid", "name": "One", "base_url": "https://one.example.com"})
    r = _create({"id": "my-pid", "name": "Two", "base_url": "https://two.example.com"})
    assert r.status_code == 400
    assert r.get_json()["code"] == "id_exists"
    # the ORIGINAL definition is untouched
    assert prov.get_provider("my-pid")["name"] == "One"


@pytest.mark.parametrize("bad_id", ["-x", "x-", "x" * 41, "has spaces", "has_underscore",
                                    "Ünïcode"])
def test_malformed_ids_are_rejected(bad_id):
    r = _create({"id": bad_id, "name": "x", "base_url": "https://x.example.com"})
    assert r.status_code == 400


def test_id_is_case_normalized_not_rejected():
    r = _create({"id": "AU", "name": "x", "base_url": "https://x.example.com"})
    assert r.status_code == 201
    assert r.get_json()["id"] == "au"


def test_empty_string_id_in_the_create_body_means_auto_generate():
    """An explicit `''` id is treated the same as an omitted one (falsy ==
    'no preference') -- it is NOT the same path as import's bare dict key,
    which rejects an empty id outright (see the next test)."""
    r = _create({"id": "", "name": "Some Name", "base_url": "https://x.example.com"})
    assert r.status_code == 201
    assert r.get_json()["id"] == "some-name"


def test_empty_id_is_rejected_on_import():
    body = {"custom_providers": {"": {"name": "x", "base_url": "https://x.example.com"}}}
    r = _client().post("/api/settings/import", json=body, headers=_auth())
    assert r.status_code == 400


@pytest.mark.parametrize("reserved", ["auto", "best", "all", "swarm", "multi", "crew",
                                      "coding", "custom", "hub", "sub-codex"])
def test_reserved_hub_sentinel_ids_are_rejected(reserved):
    """A custom id that SHADOWS a hub-wide virtual model id (tier/category/
    crew/sub-* relay) would silently break _resolve_model's '<head>/<rest>'
    provider-pin parsing for anyone who sends that word."""
    r = _create({"id": reserved, "name": "x", "base_url": "https://x.example.com"})
    assert r.status_code == 400
    assert "reserved" in r.get_json()["error"] or "built-in" in r.get_json()["error"]


def test_id_auto_generated_from_name_when_omitted():
    r = _create(_basic_body(name="Acme Örg!!"))
    assert r.status_code == 201
    assert r.get_json()["id"] == "acme-rg"  # non-ascii dropped, punctuation collapsed


def test_auto_generated_id_dedupes_on_collision():
    r1 = _create(_basic_body(name="Same Name", base_url="https://a.example.com"))
    r2 = _create(_basic_body(name="Same Name", base_url="https://b.example.com"))
    assert r1.status_code == 201 and r2.status_code == 201
    id1, id2 = r1.get_json()["id"], r2.get_json()["id"]
    assert id1 != id2
    assert id1 == "same-name"
    assert id2 == "same-name-2"


def test_auto_generated_id_never_lands_on_a_reserved_word():
    r = _create(_basic_body(name="Auto"))
    assert r.status_code == 201
    assert r.get_json()["id"] != "auto"


# --------------------------------------------------------------------------- #
# base_url / models_url validation
# --------------------------------------------------------------------------- #
def test_invalid_base_url_rejected():
    r = _create({"name": "Bad", "base_url": "not-a-url"})
    assert r.status_code == 400


def test_missing_base_url_rejected_on_create():
    r = _create({"name": "Bad"})
    assert r.status_code == 400


def test_invalid_models_url_rejected():
    r = _create(_basic_body(models_url="javascript:alert(1)"))
    assert r.status_code == 400


def test_models_url_defaults_from_base_url_when_omitted():
    _create(_basic_body(base_url="https://api.example.com/v1"))
    row = prov.get_provider("my-test-provider")
    assert row["models_url"] == "https://api.example.com/v1/models"


def test_explicit_models_url_is_kept_as_given():
    _create(_basic_body(models_url="https://api.example.com/custom-catalog"))
    row = prov.get_provider("my-test-provider")
    assert row["models_url"] == "https://api.example.com/custom-catalog"


# --------------------------------------------------------------------------- #
# The merge point: get_provider/list_providers/base_url_for/is_known_provider/
# is_free_model see a user-added provider transparently.
# --------------------------------------------------------------------------- #
def test_get_provider_sees_the_custom_provider():
    _create(_basic_body())
    row = prov.get_provider("my-test-provider")
    assert row is not None
    assert row["base_url"] == "https://api.example.com/v1"
    assert row["free_filter"] == "all"
    assert row["paid"] is False
    assert row["default_free_models"] == []


def test_list_providers_includes_the_custom_provider():
    _create(_basic_body())
    ids = [p["id"] for p in prov.list_providers()]
    assert "my-test-provider" in ids


def test_is_known_provider_true_for_a_custom_id():
    _create(_basic_body())
    assert prov.is_known_provider("my-test-provider") is True
    assert prov.is_known_provider("not-a-real-id") is False


def test_a_static_provider_id_can_never_be_shadowed():
    """providers.PROVIDERS always wins over a custom row of the same id --
    not reachable through the create route (static-id collision is
    rejected), but the merge point itself must still prefer the static
    entry if one is ever force-written underneath it."""
    config.set_custom_provider("groq", {"name": "Fake Groq", "base_url": "https://fake.example.com"})
    row = prov.get_provider("groq")
    assert row["base_url"] == prov.PROVIDERS["groq"]["base_url"]
    assert "custom" not in row or row.get("custom") is not True


def test_is_free_model_true_for_a_custom_provider_model():
    """THE critical fix: is_free_model used to read PROVIDERS.get(pid)
    directly, so every model of a custom provider read as 'not free' and
    live discovery filtered all of them out."""
    _create(_basic_body())
    assert prov.is_free_model("my-test-provider", "whatever-model") is True


def test_base_url_for_resolves_the_definitions_base_url():
    _create(_basic_body())
    assert prov.base_url_for("my-test-provider") == "https://api.example.com/v1"
    # an explicit override still wins
    assert prov.base_url_for("my-test-provider", "https://override.example.com") == \
        "https://override.example.com"


def test_signup_url_reads_through_the_merge_point():
    _create(_basic_body(signup_url="https://example.com/get-a-key"))
    assert prov.signup_url("my-test-provider") == "https://example.com/get-a-key"


# --------------------------------------------------------------------------- #
# Key-save round trip through the EXISTING /api/providers/<id>/keys route --
# proves there is NO second key-storage path.
# --------------------------------------------------------------------------- #
def test_key_saved_through_the_ordinary_provider_route():
    _create(_basic_body())
    r = _client().post("/api/providers/my-test-provider/keys",
                       json={"api_key": "sk-shared-path-123"}, headers=_auth())
    assert r.status_code == 200
    body = r.get_json()
    assert body["has_key"] is True
    assert body["key_count"] == 1
    assert body["enabled"] is True   # saving a key auto-enables, same as any provider

    # The key landed in the SAME place a real provider's key would:
    # config["providers"]["my-test-provider"], not a custom-providers field.
    plain = config.get_provider_config("my-test-provider")
    assert plain["api_keys"] == ["sk-shared-path-123"]
    raw_path = config._config_path()
    with open(raw_path, encoding="utf-8") as f:
        raw = json.load(f)
    assert "api_keys" in raw["providers"]["my-test-provider"]
    assert "api_keys" not in raw.get("custom_providers", {}).get("my-test-provider", {})


def test_key_removed_through_the_ordinary_provider_route():
    _create(_basic_body())
    config.add_provider_key("my-test-provider", "sk-one")
    r = _client().delete("/api/providers/my-test-provider/keys/0", headers=_auth())
    assert r.status_code == 200
    assert config.get_provider_config("my-test-provider")["api_keys"] == []


def test_reveal_key_through_the_ordinary_provider_route():
    _create(_basic_body())
    config.add_provider_key("my-test-provider", "sk-reveal-me")
    r = _client().get("/api/providers/my-test-provider/keys/0/reveal", headers=_auth())
    assert r.status_code == 200
    assert r.get_json()["api_key"] == "sk-reveal-me"


def test_provider_update_route_works_unchanged_for_a_custom_id():
    """/api/providers/<pid> POST (the real-provider key/enabled/base_url
    route) 404s on an unknown pid -- it must now accept a custom one."""
    _create(_basic_body())
    r = _client().post("/api/providers/my-test-provider",
                       json={"enabled": False}, headers=_auth())
    assert r.status_code == 200
    assert r.get_json()["enabled"] is False


# --------------------------------------------------------------------------- #
# Routing / discovery / "Test" see a custom provider like a real one.
# --------------------------------------------------------------------------- #
def test_live_discovery_actually_lists_models():
    _create(_basic_body())
    config.add_provider_key("my-test-provider", "sk-disco-1")
    config.set_provider_config("my-test-provider", enabled=True)

    class FakeResp:
        status_code = 200
        def json(self):
            return {"data": [{"id": "disco-model-a"}, {"id": "disco-model-b"}]}

    with mock.patch.object(app.requests, "get", return_value=FakeResp()):
        models = app.provider_free_models("my-test-provider", live=True)
    assert models == ["disco-model-a", "disco-model-b"]


def test_enabled_keyed_includes_a_custom_provider():
    _create(_basic_body())
    config.add_provider_key("my-test-provider", "sk-1")
    config.set_provider_config("my-test-provider", enabled=True)
    assert "my-test-provider" in app._enabled_keyed()


def test_resolve_model_pins_to_a_custom_provider():
    """'<pid>/<model>' pinning (_resolve_model) must treat a custom provider
    id exactly like a real one."""
    _create(_basic_body())
    config.add_provider_key("my-test-provider", "sk-1")
    config.set_provider_config("my-test-provider", enabled=True)
    pid, model = app._resolve_model("my-test-provider/some-model")
    assert (pid, model) == ("my-test-provider", "some-model")


def test_provider_card_shows_in_the_providers_list_route():
    _create(_basic_body())
    r = _client().get("/api/providers", headers=_auth())
    rows = {row["id"]: row for row in r.get_json()}
    assert "my-test-provider" in rows
    assert rows["my-test-provider"]["custom"] is True
    # Every field renderGrid()'s zone filters read must be present and of the
    # expected JS-truthy type, so a custom row lands in a real zone (not
    # silently dropped) -- see the dashboard contract test below for the
    # filter logic itself.
    row = rows["my-test-provider"]
    assert row["paid"] is False
    assert row.get("trial") is False
    assert row.get("no_key") is False
    assert isinstance(row["recommended"], bool)


# --------------------------------------------------------------------------- #
# Export / import round trip on a fresh config (definition AND key).
# --------------------------------------------------------------------------- #
def test_export_includes_definition_and_key():
    _create(_basic_body(name="Round Trip Co", base_url="https://rt.example.com/v1"))
    config.add_provider_key("round-trip-co", "sk-roundtrip")

    r = _client().get("/api/settings/export", headers=_auth())
    assert r.status_code == 200
    export = r.get_json()
    assert "custom_providers" in export["sections"]
    assert export["custom_providers"]["round-trip-co"]["name"] == "Round Trip Co"
    assert export["custom_providers"]["round-trip-co"]["base_url"] == "https://rt.example.com/v1"
    # the key rides in the EXISTING api_keys section, same as a real provider's
    assert export["api_keys"]["round-trip-co"]["api_keys"] == ["sk-roundtrip"]


def test_export_then_wipe_then_import_restores_everything():
    _create(_basic_body(name="Round Trip Co", base_url="https://rt.example.com/v1",
                        signup_url="https://rt.example.com/signup", key_hint="rt-...",
                        notes="a note"))
    config.add_provider_key("round-trip-co", "sk-roundtrip")

    export = _client().get("/api/settings/export", headers=_auth()).get_json()

    # Wipe clean (simulating a fresh machine).
    config.delete_custom_provider("round-trip-co")
    assert prov.get_provider("round-trip-co") is None

    r = _client().post("/api/settings/import", json=export, headers=_auth())
    assert r.status_code == 200
    assert "custom_providers" in r.get_json()["imported"]
    assert "api_keys" in r.get_json()["imported"]

    row = prov.get_provider("round-trip-co")
    assert row is not None
    assert row["name"] == "Round Trip Co"
    assert row["base_url"] == "https://rt.example.com/v1"
    assert row["signup_url"] == "https://rt.example.com/signup"
    assert row["key_hint"] == "rt-..."
    assert row["notes"] == "a note"
    assert config.get_provider_config("round-trip-co")["api_keys"] == ["sk-roundtrip"]


def test_import_validates_like_create_rejects_reserved_id():
    body = {"custom_providers": {"auto": {"name": "x", "base_url": "https://x.example.com"}}}
    r = _client().post("/api/settings/import", json=body, headers=_auth())
    assert r.status_code == 400


def test_import_validates_like_create_rejects_bad_base_url():
    body = {"custom_providers": {"ok-id": {"name": "x", "base_url": "not-a-url"}}}
    r = _client().post("/api/settings/import", json=body, headers=_auth())
    assert r.status_code == 400


def test_import_custom_providers_section_alone_without_api_keys():
    """custom_providers can be imported on its own (no api_keys section) --
    the definition lands with no key, exactly as a fresh add would."""
    body = {"custom_providers": {"solo-id": {"name": "Solo", "base_url": "https://solo.example.com"}}}
    r = _client().post("/api/settings/import", json=body, headers=_auth())
    assert r.status_code == 200
    row = prov.get_provider("solo-id")
    assert row["name"] == "Solo"
    assert config.get_provider_config("solo-id")["api_keys"] == []


# --------------------------------------------------------------------------- #
# Migration-safety: a custom provider's key survives a schema migration
# exactly like a real provider's -- covered by construction (providers[pid]
# is walked generically, by whatever pids are present).
# --------------------------------------------------------------------------- #
def test_custom_provider_key_survives_the_migration_fingerprint():
    config.set_custom_provider("mig-test", {"name": "Mig", "base_url": "https://mig.example.com"})
    config.add_provider_key("mig-test", "sk-migration-secret")
    config.set_provider_config("mig-test", enabled=True)

    raw_path = config._config_path()
    with open(raw_path, encoding="utf-8") as f:
        raw = json.load(f)
    assert "mig-test" in raw.get("custom_providers", {})

    before_fp = migrations.key_fingerprint(raw)
    assert before_fp["count"] == 1

    new_raw, _applied = migrations.apply(raw)
    after_fp = migrations.key_fingerprint(new_raw)
    assert migrations._keys_preserved(before_fp, after_fp)
    assert before_fp == after_fp
    # the definition block passes through byte-for-byte: apply() never
    # touches custom_providers at all.
    assert new_raw.get("custom_providers") == raw.get("custom_providers")


def test_custom_provider_key_survives_run_migrations_end_to_end():
    config.set_custom_provider("mig-test", {"name": "Mig", "base_url": "https://mig.example.com"})
    config.add_provider_key("mig-test", "sk-migration-secret")

    report = migrations.run_migrations(config._config_path())
    assert report["ok"] is True

    assert config.get_provider_config("mig-test")["api_keys"] == ["sk-migration-secret"]
    assert config.get_custom_provider("mig-test")["name"] == "Mig"


# --------------------------------------------------------------------------- #
# Log / error masking -- a custom provider's key is scrubbed exactly like a
# real provider's (no second masking path).
# --------------------------------------------------------------------------- #
def test_custom_provider_key_is_masked_in_sanitize():
    _create(_basic_body())
    config.add_provider_key("my-test-provider", "sk-very-secret-value")
    assert "sk-very-secret-value" not in app._sanitize(
        "upstream error: key sk-very-secret-value was rejected")
    assert "***" in app._sanitize("upstream error: key sk-very-secret-value was rejected")


def test_custom_provider_key_masked_in_an_error_response():
    """A log/error-shaped string anywhere in the hub that runs through
    _sanitize() never leaks a custom provider's key -- the SAME guarantee a
    real provider's key already has, with no extra code."""
    _create(_basic_body())
    config.add_provider_key("my-test-provider", "sk-leak-check-999")
    msg = app._sanitize("connection to https://api.example.com failed, "
                        "bearer sk-leak-check-999 was used")
    assert "sk-leak-check-999" not in msg


# --------------------------------------------------------------------------- #
# Flag off: create/edit/delete refused; list stays harmless; the dashboard
# shows nothing new.
# --------------------------------------------------------------------------- #
def test_flag_off_disables_create():
    config.set_flag("custom_providers_manual", False)
    r = _create(_basic_body())
    assert r.status_code == 403
    assert r.get_json()["code"] == "disabled"


def test_flag_off_disables_edit_and_delete_but_list_stays_harmless():
    _create(_basic_body())           # created while the flag is still on
    config.set_flag("custom_providers_manual", False)

    r = _client().put("/api/custom-providers/my-test-provider",
                      json={"notes": "x"}, headers=_auth())
    assert r.status_code == 403

    r = _client().delete("/api/custom-providers/my-test-provider",
                         json={"confirm": True}, headers=_auth())
    assert r.status_code == 403

    # GET still works and still reports the existing provider.
    r = _client().get("/api/custom-providers", headers=_auth())
    assert r.status_code == 200
    assert r.get_json()["enabled"] is False
    assert any(p["id"] == "my-test-provider" for p in r.get_json()["providers"])
    # ...and routing/discovery for the already-created provider is untouched
    # by the flag (the merge point itself has no flag check -- only the
    # mutating routes do).
    assert prov.get_provider("my-test-provider") is not None


def test_flag_off_is_reported_so_the_dashboard_can_hide_the_form():
    config.set_flag("custom_providers_manual", False)
    r = _client().get("/api/custom-providers", headers=_auth())
    assert r.get_json()["enabled"] is False


# --------------------------------------------------------------------------- #
# Dashboard contract checks on the template -- static, no node required
# (same style as tests/test_dashboard_feature_rendering.py's "contract
# checks on the template" half).
# --------------------------------------------------------------------------- #
import re as _re
from pathlib import Path as _Path

_HTML_PATH = _Path(app.__file__).resolve().parent / "templates" / "index.html"


def _html():
    return _HTML_PATH.read_text(encoding="utf-8")


def test_template_has_the_custom_providers_group():
    html = _html()
    assert 'id="cp-group"' in html
    assert 'id="cp-form"' in html
    assert 'id="cp-list"' in html
    assert 'id="cp-name"' in html
    assert 'id="cp-base-url"' in html
    # reuses the Skills list/form CSS classes -- no new CSS was added for this
    assert "sk-list" in html.split('id="cp-group"', 1)[1].split("</div>", 1)[0] \
        or 'class="sk-list" id="cp-list"' in html


def test_template_wires_load_and_init_functions():
    html = _html()
    assert "function loadCustomProviders(" in html
    assert "function initCustomProviders(" in html
    assert "function renderCustomProviders(" in html
    assert "loadCustomProviders(silent)" in html
    assert "initCustomProviders();" in html


def test_template_export_pairs_custom_providers_with_api_keys():
    html = _html()
    m = _re.search(r"_EXPORT_SECTION_MAP\s*=\s*\{(.*?)\};", html, _re.S)
    assert m, "_EXPORT_SECTION_MAP not found"
    assert "custom_providers" in m.group(1)
    assert "api_keys" in m.group(1)


def test_template_provider_grid_zone_filters_never_drop_an_unflagged_row():
    """renderGrid()'s zone partition (recommended / keyless / trial /
    pure-paid / free-key) must be EXHAUSTIVE, so a custom provider row
    (paid=false, no trial, no no_key, not recommended) always lands
    somewhere -- the 'freeKeyG' catch-all. A regression here would silently
    hide every custom provider from the Providers page."""
    html = _html()
    assert "var freeKeyG = rest.filter(function(p){ return !p.trial && !isPurePaid(p); })" in html
    assert "var paidG = rest.filter(function(p){ return !p.trial && isPurePaid(p); })" in html
    assert "var trialG = rest.filter(function(p){ return p.trial; })" in html
    # KEYLESS_GATEWAYS / PURE_PAID are opt-IN allowlists (checked with ||/&&
    # against a specific id), never a deny-list that could exclude an
    # unknown custom id by omission.
    assert "function isKeyless(p){ return !!(KEYLESS_GATEWAYS[p.id] || p.no_key); }" in html
    assert "function isPurePaid(p){ return !!(p.paid && PURE_PAID[p.id]); }" in html
