"""The config is copied aside whenever the stored keys change.

2026-09-05: every provider key on this install was wiped and there was NOTHING
to restore from. The encryption fix in test_encryption_cannot_delete_keys.py
stops that particular mechanism; this is the belt to its braces, because the
next way to lose them will not be the last one.

Backups hold the same ciphertext the live file does, so a copy is no more
sensitive than the original and is useless without secret.key.

Taken only when the KEY SET changes, not on every save: the hub writes config on
flag toggles and runtime-state changes hundreds of times a session, and backing
up on each would push the last good copy out of the window exactly when it is
needed.
"""
import json
import os

import pytest

import config


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(tmp_path / "config.json"))
    yield tmp_path


def _set_keys(keys, pid="groq"):
    cfg = config.load_config()
    cfg["providers"] = {pid: {"api_keys": list(keys), "enabled": True}}
    config.save_config(cfg)


def test_adding_a_key_takes_a_backup(store):
    _set_keys(["sk-1"])
    _set_keys(["sk-1", "sk-2"])
    assert config.list_backups(), "no backup taken when a key was added"


def test_losing_every_key_takes_a_backup(store):
    """THE case. A wipe is a key-set change like any other, and the copy taken
    just before it is the one that would have saved 64 keys."""
    _set_keys(["sk-1", "sk-2"])
    _set_keys([])                        # the wipe
    backups = config.list_backups()
    assert backups and max(b["keys"] for b in backups) == 2


def test_the_backup_still_holds_the_keys(store):
    _set_keys(["sk-recoverable"])
    _set_keys([])
    newest_with_keys = [b for b in config.list_backups() if b["keys"]][0]
    with open(newest_with_keys["path"], encoding="utf-8") as f:
        raw = json.load(f)
    assert raw["providers"]["groq"]["api_keys"], "the backup is empty too"


def test_an_ordinary_save_takes_no_backup(store):
    """Hundreds of flag writes must not push the last good copy out."""
    _set_keys(["sk-1"])
    before = len(config.list_backups())
    for i in range(5):
        config.set_flag("noise_%d" % i, True)
    assert len(config.list_backups()) == before


def test_backups_are_bounded(store):
    for i in range(config._BACKUP_KEEP + 8):
        _set_keys(["sk-%d" % i])
    assert len(config.list_backups()) <= config._BACKUP_KEEP


def test_they_are_listed_newest_first(store):
    _set_keys(["sk-1"])
    _set_keys(["sk-1", "sk-2"])
    _set_keys(["sk-1", "sk-2", "sk-3"])
    whens = [b["when"] for b in config.list_backups()]
    assert whens == sorted(whens, reverse=True)


def test_the_listing_says_how_many_keys_each_holds(store):
    """So a person restoring can pick the one from before the loss."""
    _set_keys(["sk-1", "sk-2", "sk-3"])
    _set_keys([])
    assert any(b["keys"] == 3 for b in config.list_backups())


def test_a_backup_can_be_taken_on_demand(store):
    _set_keys(["sk-1"])
    p = config.backup_config_now("manual")
    assert p and os.path.isfile(p)


def test_backing_up_a_missing_config_is_not_an_error(store):
    assert config.backup_config_now("manual") is None


def test_a_broken_backup_dir_never_blocks_a_save(store, monkeypatch):
    """Saving config must not fail because a backup could not be written."""
    monkeypatch.setattr(config, "_backup_dir", lambda: "\0not-a-path")
    _set_keys(["sk-still-saved"])
    assert config.get_provider_config("groq")["api_keys"] == ["sk-still-saved"]


def test_the_newest_backup_is_never_behind_the_live_config(store):
    """Backups are taken BEFORE a change, so the key just added sat in no backup
    at all until the next edit -- measured right after the user added keys: live
    41, newest backup 40. A wipe in that window would have lost the newest key.
    Adding a key now also copies the file afterwards."""
    _set_keys(["sk-1"])
    _set_keys(["sk-1", "sk-2"])
    assert config.list_backups()[0]["keys"] == 2


def test_removing_a_key_still_keeps_the_richer_copy(store):
    """The pre-save copy is the one that matters for a loss; a shrink must not
    add a post-save copy that pushes it down the list."""
    _set_keys(["sk-1", "sk-2", "sk-3"])
    _set_keys(["sk-1"])
    assert max(b["keys"] for b in config.list_backups()) == 3


def test_same_second_copies_stay_in_order(store):
    """A pre- and post-save copy can share a filename timestamp."""
    _set_keys(["sk-1"])
    _set_keys(["sk-1", "sk-2"])
    _set_keys(["sk-1", "sk-2", "sk-3"])
    whens = [b["when"] for b in config.list_backups()]
    assert whens == sorted(whens, reverse=True)
