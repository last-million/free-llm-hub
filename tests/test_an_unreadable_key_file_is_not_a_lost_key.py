"""A momentary read failure must not look like a lost key.

DATA LOSS, 2026-09-05 -- the second on this install. All 41 provider keys went
missing while secret.key was intact, unchanged, and decrypted every one of them
on the first try once they were fed back through the loader. Three app.py
processes were running at the time.

Three defects lined up:

  1. secretstore.load_or_create_key returned None on the FIRST OSError. On
     Windows that error is normally a sharing violation from another process
     holding the file, and it clears in milliseconds -- but None means "no
     master key", so decrypt() returned None for EVERY stored secret at once.

  2. _decrypt_secrets parked all of them in _unreadable_api_keys and returned an
     empty api_keys, and _encrypt_secrets -- whose carry-back is the thing that
     stops a bad read becoming a deletion -- returned EARLY when encryption was
     unavailable, without doing it. The config went to disk with api_keys empty
     and the ciphertext stranded in a field nothing reads.

  3. Nothing ever read that field back, so the keys stayed invisible through
     every later load and every restart even though the file was intact.

Encryption must never be able to destroy the thing it protects.
"""
import json
import os
from unittest import mock

import pytest

import config as C
import secretstore


@pytest.fixture
def hub(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(C, "_config_path", lambda: str(cfg))
    secretstore.reset_cache()
    yield cfg
    secretstore.reset_cache()


def _enc(cfg, value):
    return secretstore.encrypt(value, str(cfg))


def _locking_open(only_fail_first=False):
    """An open() that fails on secret.key the way Windows does."""
    real = open
    calls = {"n": 0}

    def fake(path, *a, **k):
        if str(path).endswith("secret.key"):
            calls["n"] += 1
            if not only_fail_first or calls["n"] == 1:
                raise PermissionError(32, "being used by another process")
        return real(path, *a, **k)

    return fake, calls


# --------------------------------------------------------------------------- #
# 1. the read is retried
# --------------------------------------------------------------------------- #

def test_a_transient_read_failure_does_not_lose_the_master_key(hub):
    secretstore.load_or_create_key(str(hub))        # create it
    secretstore.reset_cache()
    fake, calls = _locking_open(only_fail_first=True)
    with mock.patch("builtins.open", fake):
        key = secretstore.load_or_create_key(str(hub))
    assert key is not None, "one sharing violation lost the master key"
    assert calls["n"] > 1, "the read was not retried"


def test_a_persistently_unreadable_key_still_reports_unavailable(hub):
    """The retry is a retry, not a promise. A key that never reads must still
    come back None rather than being regenerated over the top of the real one."""
    secretstore.load_or_create_key(str(hub))
    secretstore.reset_cache()
    fake, _calls = _locking_open()
    with mock.patch("builtins.open", fake):
        assert secretstore.load_or_create_key(str(hub)) is None


def test_the_real_key_file_is_never_overwritten_by_the_retry(hub):
    """The dangerous failure mode this must not become: give up on reading, then
    generate a fresh key over an intact one and make every stored secret
    permanently undecryptable."""
    secretstore.load_or_create_key(str(hub))
    path = secretstore.key_path(str(hub))
    before = open(path, "rb").read()
    secretstore.reset_cache()
    fake, _calls = _locking_open()
    with mock.patch("builtins.open", fake):
        secretstore.load_or_create_key(str(hub))
    assert open(path, "rb").read() == before


def test_a_missing_key_file_is_still_created(hub):
    """FileNotFoundError is not a transient -- a first run must still get a key
    rather than spending four retries deciding that."""
    assert secretstore.load_or_create_key(str(hub)) is not None
    assert os.path.exists(secretstore.key_path(str(hub)))


# --------------------------------------------------------------------------- #
# 2. the carry-back happens even when encryption is unavailable
# --------------------------------------------------------------------------- #

def test_the_ciphertext_reaches_disk_when_crypto_is_unavailable(hub):
    """The exact shape of the loss: api_keys emptied, everything parked, and the
    save skipping the one step that puts it back."""
    cfg = {"providers": {"groq": {"api_keys": [],
                                  "_unreadable_api_keys": ["enc.v1:abc", "enc.v1:def"]}}}
    with mock.patch.object(secretstore, "available", lambda: False):
        out = C._encrypt_secrets(cfg)
    assert out["providers"]["groq"]["api_keys"] == ["enc.v1:abc", "enc.v1:def"]
    assert "_unreadable_api_keys" not in out["providers"]["groq"]


def test_the_carry_back_still_happens_on_the_normal_path(hub):
    cfg = {"providers": {"groq": {"api_keys": ["plain-one"],
                                  "_unreadable_api_keys": ["enc.v1:abc"]}}}
    out = C._encrypt_secrets(cfg)
    assert "enc.v1:abc" in out["providers"]["groq"]["api_keys"]
    assert len(out["providers"]["groq"]["api_keys"]) == 2


def test_the_callers_own_dict_is_not_mutated(hub):
    """_encrypt_secrets works on a copy; the unavailable path must too, or live
    code is left holding ciphertext where it expects a key."""
    prov = {"api_keys": [], "_unreadable_api_keys": ["enc.v1:abc"]}
    cfg = {"providers": {"groq": prov}}
    with mock.patch.object(secretstore, "available", lambda: False):
        C._encrypt_secrets(cfg)
    assert prov["_unreadable_api_keys"] == ["enc.v1:abc"]
    assert prov["api_keys"] == []


def test_a_config_with_nothing_parked_is_unchanged(hub):
    cfg = {"providers": {"groq": {"api_keys": ["plain-one"]}}}
    with mock.patch.object(secretstore, "available", lambda: False):
        out = C._encrypt_secrets(cfg)
    assert out["providers"]["groq"]["api_keys"] == ["plain-one"]


# --------------------------------------------------------------------------- #
# 3. parked keys come back once the transient clears
# --------------------------------------------------------------------------- #

def test_parked_ciphertext_is_retried_and_restored(hub):
    """41 keys sat in _unreadable_api_keys through every later load and every
    restart, and every one decrypted the moment it was retried."""
    cfg = {"providers": {"groq": {
        "api_keys": [],
        "_unreadable_api_keys": [_enc(hub, "gsk-real-key")]}}}
    out = C._decrypt_secrets(cfg)
    assert out["providers"]["groq"]["api_keys"] == ["gsk-real-key"]
    assert "_unreadable_api_keys" not in out["providers"]["groq"]


def test_a_genuinely_dead_key_stays_parked(hub):
    """Self-healing is not amnesia: something that still will not decrypt must
    stay in the field that keeps it, not be dropped."""
    cfg = {"providers": {"groq": {"api_keys": [],
                                  "_unreadable_api_keys": ["enc.v1:not-real-ciphertext"]}}}
    out = C._decrypt_secrets(cfg)
    assert out["providers"]["groq"]["api_keys"] == []
    assert out["providers"]["groq"]["_unreadable_api_keys"] == ["enc.v1:not-real-ciphertext"]


def test_a_recovered_key_is_not_duplicated(hub):
    """It must leave api_keys exactly once, not once per load."""
    cfg = {"providers": {"groq": {
        "api_keys": [], "_unreadable_api_keys": [_enc(hub, "gsk-real-key")]}}}
    out = C._decrypt_secrets(cfg)
    assert out["providers"]["groq"]["api_keys"].count("gsk-real-key") == 1


def test_a_restored_key_survives_the_round_trip(hub):
    """Load then save: the recovered key goes back as ciphertext, once."""
    cfg = {"providers": {"groq": {
        "api_keys": [], "_unreadable_api_keys": [_enc(hub, "gsk-real-key")]}}}
    saved = C._encrypt_secrets(C._decrypt_secrets(cfg))
    keys = saved["providers"]["groq"]["api_keys"]
    assert len(keys) == 1
    assert secretstore.decrypt(keys[0], str(hub)) == "gsk-real-key"


def test_the_whole_failure_end_to_end(hub):
    """The reported incident in one test: a locked key file during a load, a
    save while it is still locked, then a normal load once it clears. The only
    copy of the key must survive all three."""
    secretstore.load_or_create_key(str(hub))
    stored = _enc(hub, "gsk-the-only-copy")
    cfg = {"providers": {"groq": {"api_keys": [stored]}}}

    secretstore.reset_cache()
    fake, _calls = _locking_open()
    with mock.patch("builtins.open", fake):
        loaded = C._decrypt_secrets(cfg)
        assert loaded["providers"]["groq"]["api_keys"] == []      # nothing usable
        with mock.patch.object(secretstore, "available", lambda: False):
            on_disk = C._encrypt_secrets(loaded)

    # the ciphertext reached the file rather than being deleted
    assert on_disk["providers"]["groq"]["api_keys"] == [stored]

    secretstore.reset_cache()
    back = C._decrypt_secrets(json.loads(json.dumps(on_disk)))
    assert back["providers"]["groq"]["api_keys"] == ["gsk-the-only-copy"]
