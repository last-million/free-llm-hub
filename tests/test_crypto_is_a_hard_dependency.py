"""The missing package that cost this install every API key.

DIAGNOSED 2026-09-06, after a whole session of looking somewhere else.

secretstore.py imports AESGCM inside a try/except and sets _HAVE_CRYPTO=False on
failure, so the hub keeps running without it. run.bat creates a venv and pip
installs requirements.txt into it -- and cryptography was not in that file. So a
hub started through the launcher had no AESGCM, secretstore.available() was
False, EVERY stored key was undecryptable at once, and before the ciphertext
carry-back existed the next save wrote the shorter list. All 41 keys, together.

That all-or-nothing shape was the clue nobody read: a file race loses one key at
a time, a missing MODULE loses all of them simultaneously. It is also why it
never reproduced under test -- the system interpreter has cryptography, the venv
did not, and every probe happened to use the system one.

MEASURED on this machine, same config file, same secret.key, two interpreters:

    C:/Python312/python.exe        available()=True   load_config -> 42 keys
    .venv/Scripts/python.exe       available()=False  load_config ->  0 keys

Three things keep it from happening again, and this file pins all three: the
package is a declared dependency, the ciphertext is never dropped, and the hub
says out loud why it cannot read a key instead of quietly running with none.
"""
import re

import pytest

import config
import secretstore


def test_cryptography_is_a_declared_dependency():
    """The actual fix. Everything else is damage control."""
    req = open("requirements.txt", encoding="utf-8").read()
    assert re.search(r"^cryptography==", req, re.M), \
        "a venv built from requirements.txt would have no AESGCM"


def test_it_is_pinned_like_everything_else():
    """This file pins exact versions on purpose -- see its own header."""
    req = open("requirements.txt", encoding="utf-8").read()
    m = re.search(r"^cryptography==([\d.]+)", req, re.M)
    assert m and len(m.group(1).split(".")) >= 2


def test_the_reason_is_recorded_next_to_it():
    """A future reader must not see a guarded import and 'tidy up' an unused
    pin -- which is how it came to be missing in the first place."""
    req = open("requirements.txt", encoding="utf-8").read()
    i = req.index("cryptography==")
    preamble = req[max(0, i - 1200):i].lower()
    assert "silent" in preamble or "key" in preamble


def test_this_interpreter_can_actually_encrypt():
    """Guards the install the tests themselves run under."""
    assert secretstore.available()


def test_a_round_trip_works(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    secretstore.reset_cache()
    try:
        blob = secretstore.encrypt("gsk-secret-value", str(cfg))
        assert blob.startswith(secretstore.PREFIX)
        assert secretstore.decrypt(blob, str(cfg)) == "gsk-secret-value"
    finally:
        secretstore.reset_cache()


# --------------------------------------------------------------------------- #
# It is no longer silent
# --------------------------------------------------------------------------- #

def test_unreadable_keys_are_reported(monkeypatch, caplog):
    """Running with zero usable keys used to look exactly like every provider
    being down. It has to name its own cause."""
    monkeypatch.setattr(config, "_warned_unreadable", [False])
    with caplog.at_level("ERROR"):
        config._warn_unreadable_keys(41)
    assert any("decrypt" in r.message.lower() for r in caplog.records)


def test_it_names_the_missing_package_when_that_is_the_cause(monkeypatch, caplog):
    """The actionable version: 'install cryptography', not 'keys are broken'."""
    monkeypatch.setattr(config, "_warned_unreadable", [False])
    monkeypatch.setattr(secretstore, "available", lambda: False)
    with caplog.at_level("ERROR"):
        config._warn_unreadable_keys(41)
    joined = " ".join(r.message for r in caplog.records).lower()
    assert "cryptography" in joined
    assert "not lost" in joined or "come back" in joined


def test_it_says_the_keys_are_not_lost(monkeypatch, caplog):
    """The first thing anyone needs to know, in both branches."""
    for available in (True, False):
        caplog.clear()
        monkeypatch.setattr(config, "_warned_unreadable", [False])
        monkeypatch.setattr(secretstore, "available", lambda: available)
        with caplog.at_level("ERROR"):
            config._warn_unreadable_keys(41)
        joined = " ".join(r.message for r in caplog.records).lower()
        assert "not deleted" in joined or "not lost" in joined, available


def test_it_only_says_it_once(monkeypatch, caplog):
    """load_config runs on nearly every request."""
    monkeypatch.setattr(config, "_warned_unreadable", [False])
    with caplog.at_level("ERROR"):
        for _ in range(5):
            config._warn_unreadable_keys(41)
    assert len(caplog.records) == 1


def test_the_warning_can_never_raise(monkeypatch):
    """It runs inside config loading; a broken warning must not break the hub."""
    monkeypatch.setattr(config, "_warned_unreadable", [False])
    monkeypatch.setattr(secretstore, "available",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    config._warn_unreadable_keys(1)          # must not raise


def test_the_warning_is_wired_into_the_load_path(monkeypatch, caplog, tmp_path):
    """Calling the warner directly proves nothing about whether anything calls
    it -- a mutation that deleted the call site from _decrypt_secrets left every
    other test in this file passing."""
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "_config_path", lambda: str(cfg))
    monkeypatch.setattr(config, "_warned_unreadable", [False])
    payload = {"providers": {"groq": {"api_keys": ["enc.v1:not-real-ciphertext"]}}}
    with caplog.at_level("ERROR"):
        out = config._decrypt_secrets(payload)
    # parked, not dropped...
    assert out["providers"]["groq"]["_unreadable_api_keys"] == ["enc.v1:not-real-ciphertext"]
    assert out["providers"]["groq"]["api_keys"] == []
    # ...and said so
    assert any("decrypt" in r.message.lower() for r in caplog.records), \
        "_decrypt_secrets parked a key without telling anyone"


def test_a_readable_config_says_nothing(monkeypatch, caplog, tmp_path):
    """No false alarm on the ordinary path."""
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "_config_path", lambda: str(cfg))
    monkeypatch.setattr(config, "_warned_unreadable", [False])
    secretstore.reset_cache()
    try:
        good = secretstore.encrypt("gsk-real", str(cfg))
        with caplog.at_level("ERROR"):
            out = config._decrypt_secrets({"providers": {"groq": {"api_keys": [good]}}})
        assert out["providers"]["groq"]["api_keys"] == ["gsk-real"]
        assert not caplog.records
    finally:
        secretstore.reset_cache()
