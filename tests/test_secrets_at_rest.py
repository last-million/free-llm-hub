"""Provider keys are encrypted in config.json.

Third of the three picked from the freellmapi comparison. Ours were plaintext
JSON, protected by a 0600 chmod -- which config.py applies only `if os.name ==
"posix"`. This hub runs on Windows, so in practice there was no file-permission
protection at all.

The honest scope, because the acronym oversells it: the master key lives in
secret.key beside config.json, readable by the same user. This is not protection
against someone who can already read your home directory as you; nothing stored
locally can be, short of a passphrase typed at every start, which for a
background hub means never starting unattended.

It protects against the ways these keys actually leak, all of which move
config.json somewhere its permissions do not follow: a backup, a sync folder, a
support bundle, a screenshot, a settings export, `grep -r "sk-" ~`. Someone who
copies BOTH files gets the keys; someone who copies the config, the common case,
does not.

The dependency is optional on purpose. A gateway that refuses to start because
an encryption library is missing has traded a small risk for a total outage.
"""
import json
import os

import pytest

import config
import secretstore


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A config directory of its own, so nothing here touches the real one."""
    path = tmp_path / "config.json"
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(path))
    secretstore.reset_cache()
    yield path
    secretstore.reset_cache()


def _raw(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _save_key(value="sk-secret-value", pid="groq"):
    cfg = config.load_config()
    cfg["providers"] = {pid: {"api_keys": [value], "enabled": True, "base_url": None}}
    config.save_config(cfg)


# --------------------------------------------------------------------------- #
# The primitive
# --------------------------------------------------------------------------- #

def test_encryption_is_available_here():
    """If this fails the rest degrades to plaintext by design, so say so loudly
    rather than letting the other tests quietly assert nothing."""
    assert secretstore.available(), "cryptography is not installed"


def test_a_secret_round_trips(store):
    blob = secretstore.encrypt("sk-abc", str(store))
    assert blob != "sk-abc"
    assert secretstore.decrypt(blob, str(store)) == "sk-abc"


def test_ciphertext_is_tagged_with_its_format(store):
    assert secretstore.encrypt("sk-abc", str(store)).startswith(secretstore.PREFIX)
    assert secretstore.is_encrypted(secretstore.encrypt("sk-abc", str(store)))


def test_plaintext_passes_through_untouched(store):
    """A hand-edited file, or one from before this existed."""
    assert secretstore.decrypt("sk-plain", str(store)) == "sk-plain"


def test_encrypting_twice_does_not_double_wrap(store):
    once = secretstore.encrypt("sk-abc", str(store))
    assert secretstore.encrypt(once, str(store)) == once


def test_the_same_secret_encrypts_differently_each_time(store):
    """A fresh nonce per value: equal ciphertexts would reveal which providers
    share a key."""
    a = secretstore.encrypt("sk-abc", str(store))
    b = secretstore.encrypt("sk-abc", str(store))
    assert a != b
    assert secretstore.decrypt(a, str(store)) == secretstore.decrypt(b, str(store))


def test_a_tampered_ciphertext_does_not_decrypt(store):
    """GCM authenticates; a flipped byte must fail rather than return garbage."""
    blob = secretstore.encrypt("sk-abc", str(store))
    broken = blob[:-4] + ("AAAA" if not blob.endswith("AAAA") else "BBBB")
    assert secretstore.decrypt(broken, str(store)) is None


def test_a_wrong_key_yields_none_not_ciphertext(store):
    """None, so the caller drops a dead key. Returning "enc.v1:..." would send
    it to a provider as if it were an API key, producing an auth failure that
    re-entering the correct key would never fix."""
    blob = secretstore.encrypt("sk-abc", str(store))
    secretstore.reset_cache()
    with open(secretstore.key_path(str(store)), "wb") as f:
        f.write(os.urandom(32))
    assert secretstore.decrypt(blob, str(store)) is None


def test_the_key_file_is_created_once_and_reused(store):
    secretstore.encrypt("a", str(store))
    key_path = secretstore.key_path(str(store))
    first = open(key_path, "rb").read()
    secretstore.reset_cache()
    secretstore.encrypt("b", str(store))
    assert open(key_path, "rb").read() == first


def test_a_truncated_key_file_is_not_silently_replaced(store):
    """Generating a new key over a damaged one destroys every stored secret."""
    with open(secretstore.key_path(str(store)), "wb") as f:
        f.write(b"short")
    secretstore.reset_cache()
    with pytest.raises(ValueError):
        secretstore.load_or_create_key(str(store))


# --------------------------------------------------------------------------- #
# Through the config store
# --------------------------------------------------------------------------- #

def test_the_key_is_not_in_the_file_in_plaintext(store):
    _save_key("sk-super-secret")
    assert "sk-super-secret" not in _raw(store)
    assert secretstore.PREFIX in _raw(store)


def test_every_caller_still_sees_the_real_key(store):
    """The whole design: encryption is a property of the FILE, so routing, the
    dashboard and the export are untouched."""
    _save_key("sk-super-secret")
    assert config.get_provider_config("groq")["api_keys"] == ["sk-super-secret"]


def test_a_key_survives_a_full_round_trip(store):
    _save_key("sk-round-trip")
    cfg = config.load_config()
    config.save_config(cfg)
    assert config.get_provider_config("groq")["api_keys"] == ["sk-round-trip"]


def test_several_keys_in_a_pool_are_all_encrypted(store):
    cfg = config.load_config()
    cfg["providers"] = {"groq": {"api_keys": ["sk-one", "sk-two"], "enabled": True}}
    config.save_config(cfg)
    raw = _raw(store)
    assert "sk-one" not in raw and "sk-two" not in raw
    assert config.get_provider_config("groq")["api_keys"] == ["sk-one", "sk-two"]


def test_saving_does_not_mutate_the_callers_dict(store):
    """Callers usually hold the dict they are still using; leaving ciphertext in
    it would hand live code an unusable key."""
    cfg = config.load_config()
    cfg["providers"] = {"groq": {"api_keys": ["sk-live"], "enabled": True}}
    config.save_config(cfg)
    assert cfg["providers"]["groq"]["api_keys"] == ["sk-live"]


def test_an_undecryptable_key_is_dropped_not_served(store):
    _save_key("sk-lost")
    secretstore.reset_cache()
    with open(secretstore.key_path(str(store)), "wb") as f:
        f.write(os.urandom(32))          # secret.key replaced: the key is gone
    assert config.get_provider_config("groq")["api_keys"] == []


# --------------------------------------------------------------------------- #
# Migration from plaintext
# --------------------------------------------------------------------------- #

def test_plaintext_keys_already_on_disk_still_work(store):
    """Backward compatible: an existing install must not lose its keys."""
    store.write_text(json.dumps(
        {"providers": {"groq": {"api_keys": ["sk-old-plain"], "enabled": True}}}),
        encoding="utf-8")
    assert config.get_provider_config("groq")["api_keys"] == ["sk-old-plain"]


def test_they_are_migrated_on_demand(store):
    store.write_text(json.dumps(
        {"providers": {"groq": {"api_keys": ["sk-old-plain"], "enabled": True}}}),
        encoding="utf-8")
    assert config.encrypt_existing_secrets() == 1
    assert "sk-old-plain" not in _raw(store)
    assert config.get_provider_config("groq")["api_keys"] == ["sk-old-plain"]


def test_migration_is_a_no_op_once_done(store):
    _save_key("sk-already")
    assert config.encrypt_existing_secrets() == 0


def test_a_mixed_file_migrates_only_what_needs_it(store):
    _save_key("sk-encrypted")
    cfg = json.loads(_raw(store))
    cfg["providers"]["other"] = {"api_keys": ["sk-plain"], "enabled": True}
    store.write_text(json.dumps(cfg), encoding="utf-8")
    assert config.encrypt_existing_secrets() == 1
    assert config.get_provider_config("other")["api_keys"] == ["sk-plain"]
    assert config.get_provider_config("groq")["api_keys"] == ["sk-encrypted"]


def test_the_status_reports_how_keys_are_stored(store):
    assert config.secrets_encrypted() is False
    _save_key()
    assert config.secrets_encrypted() is True


# --------------------------------------------------------------------------- #
# Degrading rather than failing
# --------------------------------------------------------------------------- #

def test_without_the_library_the_hub_still_saves_keys(store, monkeypatch):
    """A gateway that will not start because an encryption library is missing
    has traded a small risk for a total outage."""
    monkeypatch.setattr(secretstore, "_HAVE_CRYPTO", False)
    monkeypatch.setattr(secretstore, "available", lambda: False)
    _save_key("sk-no-crypto")
    assert config.get_provider_config("groq")["api_keys"] == ["sk-no-crypto"]
    assert "sk-no-crypto" in _raw(store)          # plaintext, and honest about it
    assert config.secrets_encrypted() is False


def test_the_hub_reports_both_facts():
    """"encrypted" and "can encrypt" are different questions, and a fresh
    install with no keys yet answers no to the first and yes to the second."""
    import app as A
    src = open("app.py", encoding="utf-8").read()
    assert '"keys_encrypted": config.secrets_encrypted()' in src
    assert '"encryption_available": secretstore.available()' in src


def test_startup_migrates_existing_installs():
    src = open("app.py", encoding="utf-8").read()
    assert "config.encrypt_existing_secrets()" in src
