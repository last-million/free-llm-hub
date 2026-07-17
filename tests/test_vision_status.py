"""Unit tests for vision_status.py in isolation (no Flask app involved) --
the app-route + agentic-chat-system-prompt-injection integration is covered
in tests/test_agentic_chat.py instead, since both consumers live there.
"""
import pytest

import config
import providers as prov
import vision_status


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    path = tmp_path / "state" / "config.json"
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(path))
    return path


def test_qualifying_providers_empty_on_fresh_config(isolated_config):
    assert vision_status.qualifying_providers() == []
    result = vision_status.status()
    assert result["available"] is False
    assert result["providers"] == []
    assert result["vision_became_available_at"] is None
    assert isinstance(result["last_checked"], float)


def test_provider_needs_enabled_and_key_and_vision_models(isolated_config):
    # Registry has one -- confirm 'google' really carries a non-empty
    # vision_models list (a hardcoded assumption this whole test file rests on).
    google = prov.get_provider("google")
    assert google and google.get("vision_models")

    # Not enabled -> disqualified even with a key.
    config.set_provider_config("google", api_key="k", enabled=False)
    assert "google" not in vision_status.qualifying_providers()

    # Enabled but no key -> disqualified (google is not a no_key provider).
    config.set_provider_config("google", api_key="", enabled=True)
    assert "google" not in vision_status.qualifying_providers()

    # Enabled + keyed -> qualifies.
    config.set_provider_config("google", api_key="k", enabled=True)
    assert "google" in vision_status.qualifying_providers()


def test_provider_without_vision_models_never_qualifies(isolated_config):
    # 'groq' (a real registry id) does NOT carry a vision_models list.
    assert not (prov.get_provider("groq") or {}).get("vision_models")
    config.set_provider_config("groq", api_key="k", enabled=True)
    assert "groq" not in vision_status.qualifying_providers()


def test_no_key_provider_qualifies_without_any_key(isolated_config, monkeypatch):
    """A hypothetical no_key provider with vision_models should qualify purely
    from being enabled -- simulate via monkeypatching the registry lookup
    rather than requiring a REAL no-key+vision provider to exist today."""
    fake_row = {"no_key": True, "vision_models": ["fake-vision-model"]}
    # Capture the ORIGINAL (unpatched) functions first -- setattr below
    # reassigns the SAME module attribute these fixtures would otherwise
    # recurse into.
    orig_get_provider = prov.get_provider
    orig_list_providers = prov.list_providers
    monkeypatch.setattr(vision_status.prov, "get_provider",
                        lambda pid: fake_row if pid == "fake-nokey" else orig_get_provider(pid))
    monkeypatch.setattr(vision_status.prov, "list_providers",
                        lambda: orig_list_providers() + [{"id": "fake-nokey"}])
    config.set_provider_config("fake-nokey", enabled=True)
    assert "fake-nokey" in vision_status.qualifying_providers()


def test_status_flip_sets_and_clears_timestamp(isolated_config):
    assert vision_status.status()["vision_became_available_at"] is None
    config.set_provider_config("google", api_key="k", enabled=True)
    first = vision_status.status()
    assert first["available"] is True
    ts1 = first["vision_became_available_at"]
    assert ts1 is not None
    # Recomputing again while STILL available must not reset the streak start.
    second = vision_status.status()
    assert second["vision_became_available_at"] == ts1
    config.set_provider_config("google", enabled=False)
    assert vision_status.status()["vision_became_available_at"] is None


def test_start_heartbeat_spawns_exactly_one_daemon_thread(isolated_config, monkeypatch):
    monkeypatch.setattr(vision_status, "_heartbeat_thread", None)
    vision_status.start_heartbeat()
    thread = vision_status._heartbeat_thread
    assert thread is not None
    assert thread.daemon is True
    assert thread.is_alive()
    vision_status.start_heartbeat()  # second call must be a no-op
    assert vision_status._heartbeat_thread is thread
