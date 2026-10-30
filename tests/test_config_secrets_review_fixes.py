"""Review fixes for the config / secret.key / migration code (2026-10-30).

Each test reproduces a defect found by a read-only review:

  * get_setting cached the defaults view after one failed read, so the
    blocklists and the subscription scope stayed reverted until the next write;
  * encrypt_existing_secrets saved a NON-strict reload, so one failed read
    wrote an empty config over the real one;
  * a wrong-size secret.key raised ValueError out of every config read and save;
  * the retire migrations moved a user's own custom provider (and its key);
  * the control-token docstring promised the token is never in the HTML.

Hermetic: every test points FREE_LLM_HUB_CONFIG at tmp_path.
"""
import builtins
import json
import os
import re

import pytest

import config
import migrations
import secretstore

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(path))
    secretstore.reset_cache()
    config.invalidate_settings_cache()
    yield path
    secretstore.reset_cache()
    config.invalidate_settings_cache()


def _write(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)


def _failing_open(target, times):
    """builtins.open that raises PermissionError for `target` (read mode) the
    first `times` calls, like a sharing violation on Windows."""
    real_open = builtins.open
    state = {"left": times}

    def fake(file, mode="r", *a, **kw):
        if (os.path.abspath(str(file)) == os.path.abspath(str(target))
                and "r" in mode and "+" not in mode and state["left"] > 0):
            state["left"] -= 1
            raise PermissionError(13, "sharing violation", str(file))
        return real_open(file, mode, *a, **kw)
    return fake, state


# --------------------------------------------------------------------------- #
# get_setting never caches a failed read
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("failures", [1, 2])
def test_a_failed_read_is_not_cached_by_get_setting(cfg_path, failures):
    _write(cfg_path, {"subscription_scope": "manager_only",
                      "blocked_models": ["foo/bar"]})
    assert config.get_setting("subscription_scope") == "manager_only"
    config.invalidate_settings_cache()          # any write in this process does this

    fake, _ = _failing_open(cfg_path, failures)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(builtins, "open", fake)
        config.get_setting("subscription_scope", "all")   # the failing read

    # The file is readable again and unchanged: the real values must come back
    # (the old cache served the defaults view until the next write).
    assert config.get_setting("subscription_scope") == "manager_only"
    assert config.get_setting("blocked_models") == ["foo/bar"]


def test_get_setting_still_caches_a_good_read(cfg_path, monkeypatch):
    _write(cfg_path, {"subscription_scope": "manager_only"})
    assert config.get_setting("subscription_scope") == "manager_only"
    calls = {"n": 0}
    real = config.load_config

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)
    monkeypatch.setattr(config, "load_config", counting)
    for _ in range(5):
        assert config.get_setting("subscription_scope") == "manager_only"
    assert calls["n"] == 0


def test_a_corrupt_file_still_reads_as_defaults(cfg_path):
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write("{not json")
    assert config.get_setting("subscription_scope", "all") == "all"


# --------------------------------------------------------------------------- #
# encrypt_existing_secrets never saves an empty reload
# --------------------------------------------------------------------------- #

def test_a_failed_second_read_does_not_wipe_the_config(cfg_path):
    if not secretstore.available():
        pytest.skip("cryptography is not installed")
    _write(cfg_path, {"providers": {"groq": {"api_keys": ["gsk_plaintext"],
                                             "enabled": True}},
                      "local_api_key": "lk-secret", "control_token": "ct",
                      "subscription_scope": "manager_only"})
    real_open = builtins.open
    state = {"reads": 0}

    def fake(file, mode="r", *a, **kw):
        if os.path.abspath(str(file)) == os.path.abspath(str(cfg_path)) and mode == "r":
            state["reads"] += 1
            if state["reads"] == 2:        # the reload after counting
                raise PermissionError(13, "sharing violation", str(file))
        return real_open(file, mode, *a, **kw)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(builtins, "open", fake)
        migrated = config.encrypt_existing_secrets()

    assert migrated == 0
    with open(cfg_path, encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk["providers"]["groq"]["api_keys"] == ["gsk_plaintext"]
    assert on_disk["local_api_key"] == "lk-secret"
    assert on_disk["control_token"] == "ct"
    assert on_disk["subscription_scope"] == "manager_only"


def test_encrypt_existing_secrets_still_encrypts(cfg_path):
    if not secretstore.available():
        pytest.skip("cryptography is not installed")
    _write(cfg_path, {"providers": {"groq": {"api_keys": ["gsk_plaintext"],
                                             "enabled": True}}})
    assert config.encrypt_existing_secrets() == 1
    with open(cfg_path, encoding="utf-8") as f:
        raw = f.read()
    assert "gsk_plaintext" not in raw and secretstore.PREFIX in raw
    assert config.load_config()["providers"]["groq"]["api_keys"] == ["gsk_plaintext"]


# --------------------------------------------------------------------------- #
# a wrong-size secret.key is "no key", not an exception
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("content", [b"", b"short", b"\x07" * 40],
                         ids=["empty", "short", "padded"])
def test_a_wrong_size_key_file_does_not_break_config_reads(cfg_path, content):
    if not secretstore.available():
        pytest.skip("cryptography is not installed")
    _write(cfg_path, {"providers": {"groq": {"api_keys": ["enc.v1:QUJD"],
                                             "enabled": True}},
                      "some_flag": True})
    key_file = secretstore.key_path(str(cfg_path))
    with open(key_file, "wb") as f:
        f.write(content)
    secretstore.reset_cache()

    assert secretstore.load_or_create_key(str(cfg_path)) is None
    cfg = config.load_config()                     # used to raise ValueError
    assert config.get_flag("some_flag") is True
    assert cfg["providers"]["groq"]["api_keys"] == []
    # A NEW key is never stored in plain text: the fallback key encrypts it, and
    # the damaged key file is left exactly as it was.
    enc = secretstore.encrypt("sk-x", str(cfg_path))
    assert secretstore.is_encrypted(enc)
    assert secretstore.decrypt(enc, str(cfg_path)) == "sk-x"
    with open(key_file, "rb") as f:
        assert f.read() == content
    config.set_flag("other_flag", True)            # a save works too
    with open(cfg_path, encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk["providers"]["groq"]["api_keys"] == ["enc.v1:QUJD"]   # parked, not lost
    with open(key_file, "rb") as f:
        assert f.read() == content                 # never replaced


def test_a_failed_key_write_leaves_no_empty_key_file(cfg_path):
    if not secretstore.available():
        pytest.skip("cryptography is not installed")
    key_file = secretstore.key_path(str(cfg_path))

    def boom(fd):
        raise OSError(28, "No space left on device")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(secretstore.os, "fsync", boom)
        assert secretstore.load_or_create_key(str(cfg_path)) is None
    assert not os.path.exists(key_file)
    secretstore.reset_cache()
    key = secretstore.load_or_create_key(str(cfg_path))   # a later start works
    assert key is not None and len(key) == 32


# --------------------------------------------------------------------------- #
# the retire migrations leave a user's custom provider alone
# --------------------------------------------------------------------------- #

def test_a_custom_provider_with_a_retired_id_keeps_its_key():
    raw = {"schema_version": 3,
           "providers": {"hyperbolic": {"api_keys": ["enc.v1:MINE"], "enabled": True},
                         "github-models": {"api_keys": ["enc.v1:OLD"], "enabled": True}},
           "custom_providers": {"hyperbolic": {"name": "Hyperbolic",
                                               "base_url": "https://api.hyperbolic.ai/v1"}}}
    new, _applied = migrations.apply(raw)
    assert new["providers"]["hyperbolic"]["api_keys"] == ["enc.v1:MINE"]
    assert "hyperbolic" not in (new.get("retired_providers") or {})
    # A registry provider that was really removed still retires.
    assert "github-models" not in new["providers"]
    assert new["retired_providers"]["github-models"]["api_keys"] == ["enc.v1:OLD"]
    assert migrations._keys_preserved(raw, new)


# --------------------------------------------------------------------------- #
# the control-token docstring tells the truth
# --------------------------------------------------------------------------- #

def test_the_control_token_docs_do_not_promise_it_stays_out_of_the_html():
    with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
        assert "HUB_CONTROL_TOKEN = {{ control_token" in f.read()   # it IS embedded
    doc = config.ensure_control_token.__doc__ or ""
    assert "never rendered" not in doc
    assert "GET /" in doc
    with open(os.path.join(ROOT, "app.py"), encoding="utf-8") as f:
        src = f.read()
    start = src.index("def _local_control_guard")
    guard = src[start:src.index("\n@app.after_request", start)]
    assert "never rendered" not in re.sub(r"[\s#]+", " ", guard)


def test_restoring_the_damaged_key_keeps_both_old_and_new_secrets_readable(cfg_path):
    if not secretstore.available():
        pytest.skip("cryptography is not installed")
    good = secretstore.load_or_create_key(str(cfg_path))
    old_secret = secretstore.encrypt("sk-old", str(cfg_path))
    key_file = secretstore.key_path(str(cfg_path))
    with open(key_file, "wb") as f:
        f.write(good[:31])                          # damaged
    secretstore.reset_cache()
    assert secretstore.decrypt(old_secret, str(cfg_path)) is None
    new_secret = secretstore.encrypt("sk-new", str(cfg_path))
    assert secretstore.is_encrypted(new_secret)
    assert os.path.exists(secretstore.fallback_path(str(cfg_path)))
    with open(key_file, "wb") as f:                 # the owner restores the real key
        f.write(good)
    secretstore.reset_cache()
    assert secretstore.decrypt(old_secret, str(cfg_path)) == "sk-old"
    assert secretstore.decrypt(new_secret, str(cfg_path)) == "sk-new"


def test_no_fallback_key_is_made_while_the_key_file_is_fine(cfg_path):
    if not secretstore.available():
        pytest.skip("cryptography is not installed")
    enc = secretstore.encrypt("sk-x", str(cfg_path))
    assert secretstore.decrypt(enc, str(cfg_path)) == "sk-x"
    assert not os.path.exists(secretstore.fallback_path(str(cfg_path)))

