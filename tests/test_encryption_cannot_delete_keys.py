"""Encryption must never be able to destroy the thing it protects.

DATA LOSS, 2026-09-05. Every provider key on this install -- 64 of them across
35 providers -- was wiped from config.json, leaving `"api_keys": []` on all 45
provider rows and no recoverable copy.

The mechanism, whatever first caused the bad read, is this:

  _decrypt_secrets DROPPED any value it could not decrypt, so load_config
  returned a SHORTER list than the file held. The next save_config -- and that
  is triggered by anything at all: a flag toggle, a runtime-state write, a
  settings change -- then persisted the shorter list over the ciphertext.

So one unreadable decrypt became permanent, silent deletion. There was no error,
no warning, and nothing to restore from.

Hiding an unreadable key from callers is still right: handing "enc.v1:..." to a
provider produces an auth failure that re-entering the correct key would never
fix. But hiding it and DELETING it are different things, and the code did both.
"""
import json
import os

import pytest

import config
import secretstore


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(path))
    secretstore.reset_cache()
    yield path
    secretstore.reset_cache()


def _raw(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_key(value="sk-important", pid="groq"):
    cfg = config.load_config()
    cfg["providers"] = {pid: {"api_keys": [value], "enabled": True}}
    config.save_config(cfg)


def _break_the_master_key(path):
    """Exactly what a lost, replaced or wrong-directory secret.key looks like."""
    secretstore.reset_cache()
    with open(secretstore.key_path(str(path)), "wb") as f:
        f.write(os.urandom(32))


def test_an_unreadable_key_is_hidden_from_callers(store):
    """Unchanged: a value nothing can decrypt must not reach a provider."""
    _save_key()
    _break_the_master_key(store)
    assert config.get_provider_config("groq")["api_keys"] == []


def test_but_it_survives_on_disk(store):
    """THE FIX. It used to be dropped, and the next save wrote the drop back."""
    _save_key()
    _break_the_master_key(store)
    config.load_config()
    assert _raw(store)["providers"]["groq"]["api_keys"], "the ciphertext was lost"


def test_an_unrelated_save_does_not_delete_it(store):
    """The exact path that destroyed 64 keys: something writes a FLAG, and the
    provider keys disappear with it."""
    _save_key()
    _break_the_master_key(store)
    config.set_flag("some_unrelated_flag", True)
    stored = _raw(store)["providers"]["groq"]["api_keys"]
    assert stored and stored[0].startswith(secretstore.PREFIX)


def test_restoring_the_master_key_brings_them_back(store):
    """Which is the whole point of not deleting: a wrong secret.key should cost
    a restart, not the keys."""
    _save_key("sk-recoverable")
    good = open(secretstore.key_path(str(store)), "rb").read()
    _break_the_master_key(store)
    config.set_flag("noise", True)                  # a save while unreadable
    secretstore.reset_cache()
    with open(secretstore.key_path(str(store)), "wb") as f:
        f.write(good)                               # the real key comes back
    assert config.get_provider_config("groq")["api_keys"] == ["sk-recoverable"]


def test_readable_keys_are_untouched_by_the_carry(store):
    """A normal install must not grow phantom entries."""
    _save_key("sk-fine")
    config.set_flag("noise", True)
    assert config.get_provider_config("groq")["api_keys"] == ["sk-fine"]
    assert len(_raw(store)["providers"]["groq"]["api_keys"]) == 1


def test_a_mix_of_readable_and_unreadable_keeps_both(store):
    cfg = config.load_config()
    cfg["providers"] = {"groq": {"api_keys": ["sk-one"], "enabled": True}}
    config.save_config(cfg)
    stored = _raw(store)
    stored["providers"]["groq"]["api_keys"].append(
        secretstore.PREFIX + "bm90LWRlY3J5cHRhYmxl")     # junk ciphertext
    with open(store, "w", encoding="utf-8") as f:
        json.dump(stored, f)
    assert config.get_provider_config("groq")["api_keys"] == ["sk-one"]
    config.set_flag("noise", True)
    assert len(_raw(store)["providers"]["groq"]["api_keys"]) == 2


def test_the_carry_field_never_leaks_to_callers(store):
    """It is bookkeeping, not configuration."""
    _save_key()
    _break_the_master_key(store)
    assert "_unreadable_api_keys" not in _raw(store)["providers"]["groq"]
