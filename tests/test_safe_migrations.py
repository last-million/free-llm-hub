"""Automatic, versioned config migrations (2026-10-10).

Every install auto-updates, so a release that renamed/retired a setting must
migrate old state by itself and must NEVER drop or corrupt an API key. These
tests are hermetic: a temp config via FREE_LLM_HUB_CONFIG, fake "encrypted" key
strings (never real crypto), the real atomic I/O.

The one guarantee that must never break: the exact stored key strings (the
ciphertext) are byte-identical after a migration — including the keys of a
REMOVED provider, which are kept, not discarded.
"""
import collections
import json
import os
import stat

import pytest

import config
import migrations


# --------------------------------------------------------------------------- #
# fixtures / helpers                                                          #
# --------------------------------------------------------------------------- #
@pytest.fixture
def cfgfile(tmp_path, monkeypatch):
    path = str(tmp_path / "config.json")
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", path)
    config.invalidate_settings_cache()
    return path


def write_raw(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)


def read_raw(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def all_key_strings(obj):
    """Independent extractor: every stored key string, as a multiset."""
    bag = collections.Counter()
    for mapname in ("providers", "retired_providers"):
        for row in (obj.get(mapname) or {}).values():
            if not isinstance(row, dict):
                continue
            for field in ("api_keys", "_unreadable_api_keys"):
                for k in (row.get(field) or []):
                    bag[k] += 1
            if isinstance(row.get("api_key"), str) and row["api_key"]:
                bag[row["api_key"]] += 1
    return bag


def old_config(**extra):
    """A realistic pre-v3 config with fake encrypted keys."""
    cfg = {
        "schema_version": 2,
        "cli_multi_confirm": False,
        "providers": {
            "groq": {"api_keys": ["enc.v1:GROQ1", "enc.v1:GROQ2"], "enabled": True},
            "tokenrouter": {"api_keys": ["enc.v1:TOK1"], "enabled": True},
            "agentrouter": {"api_key": "enc.v1:AGENT_LEGACY", "enabled": False,
                            "_unreadable_api_keys": ["enc.v1:AGENT_PARKED"]},
        },
        "local_api_key": None,
    }
    cfg.update(extra)
    return cfg


# --------------------------------------------------------------------------- #
# pure apply(): the rename                                                     #
# --------------------------------------------------------------------------- #
def test_rename_false_maps_to_off():
    new, applied = migrations.apply({"schema_version": 2, "cli_multi_confirm": False})
    assert new["cli_multi_approval"] == "off"
    assert "cli_multi_confirm" not in new
    assert applied


def test_rename_true_maps_to_dashboard():
    new, _ = migrations.apply({"schema_version": 2, "cli_multi_confirm": True})
    assert new["cli_multi_approval"] == "dashboard"
    assert "cli_multi_confirm" not in new


def test_rename_nonbool_takes_the_safe_side():
    new, _ = migrations.apply({"schema_version": 2, "cli_multi_confirm": "weird"})
    assert new["cli_multi_approval"] == "dashboard"


def test_rename_never_clobbers_an_explicit_new_choice():
    new, _ = migrations.apply(
        {"schema_version": 2, "cli_multi_confirm": True, "cli_multi_approval": "chat"})
    assert new["cli_multi_approval"] == "chat"      # owner's new-name choice wins
    assert "cli_multi_confirm" not in new


# --------------------------------------------------------------------------- #
# pure apply(): retiring removed providers                                     #
# --------------------------------------------------------------------------- #
def test_retire_moves_rows_and_keeps_every_key():
    before = old_config()
    new, _ = migrations.apply(before)
    assert set(new["providers"]) == {"groq"}                    # dead rows gone
    assert set(new["retired_providers"]) == {"tokenrouter", "agentrouter"}
    # every stored key string survives, as the SAME string, nothing added/lost
    assert all_key_strings(new) == all_key_strings(before)
    # the removed provider's keys are specifically kept
    assert new["retired_providers"]["tokenrouter"]["api_keys"] == ["enc.v1:TOK1"]
    assert new["retired_providers"]["agentrouter"]["api_key"] == "enc.v1:AGENT_LEGACY"
    assert new["retired_providers"]["agentrouter"]["_unreadable_api_keys"] \
        == ["enc.v1:AGENT_PARKED"]


def test_apply_is_idempotent():
    new1, _ = migrations.apply(old_config())
    new2, applied2 = migrations.apply(new1)
    assert new2 == new1
    assert applied2 == []


def test_schema_version_is_bumped_to_latest():
    new, _ = migrations.apply(old_config())
    assert new["schema_version"] == migrations.LATEST == 3


def test_a_newer_config_is_not_downgraded():
    new, applied = migrations.apply({"schema_version": 99, "providers": {}})
    assert new["schema_version"] == 99
    assert applied == []


# --------------------------------------------------------------------------- #
# version gating vs premature stamping                                         #
# --------------------------------------------------------------------------- #
def test_readded_provider_at_a_later_version_is_not_retired():
    """The retire step is version-gated: a v3 config that legitimately carries a
    provider named like a once-removed one is left alone."""
    cfg = {"schema_version": 3,
           "providers": {"tokenrouter": {"api_keys": ["enc.v1:NEW"], "enabled": True}}}
    new, applied = migrations.apply(cfg)
    assert "tokenrouter" in new["providers"]
    assert "retired_providers" not in new
    assert applied == []


def test_a_premature_stamp_still_runs_the_rename():
    """A save between boot and the step can stamp v3 early; the unambiguous
    pre-v3 key forces the rename to still happen."""
    new, applied = migrations.apply({"schema_version": 3, "cli_multi_confirm": False})
    assert new["cli_multi_approval"] == "off"
    assert applied


# --------------------------------------------------------------------------- #
# key-safety fingerprint                                                       #
# --------------------------------------------------------------------------- #
def test_fingerprint_counts_without_exposing_values():
    fp = migrations.key_fingerprint(old_config())
    assert fp["count"] == 5                                      # 2 + 1 + 1 legacy + 1 parked
    assert all(len(h) == 64 for h in fp["digests"])             # sha-256 hex, not the key


def test_keys_preserved_detects_a_loss():
    before = migrations.key_fingerprint(old_config())
    fewer = {"schema_version": 2, "providers": {"groq": {"api_keys": ["enc.v1:GROQ1"]}}}
    assert not migrations._keys_preserved(before, migrations.key_fingerprint(fewer))


# --------------------------------------------------------------------------- #
# run_migrations(): the real boot step                                         #
# --------------------------------------------------------------------------- #
def test_run_writes_backs_up_first_and_keeps_keys(cfgfile):
    write_raw(cfgfile, old_config())
    original_bytes = open(cfgfile, "rb").read()
    before_keys = all_key_strings(read_raw(cfgfile))

    report = migrations.run_migrations()

    assert report["ok"] and report["changed"]
    assert report["schema_version"] == 3
    assert report["keys_count"] == 5
    # the pre-migration backup holds the ORIGINAL bytes -> it was taken first
    assert report["last_backup"] and os.path.isfile(report["last_backup"])
    assert open(report["last_backup"], "rb").read() == original_bytes
    assert "premigrate" in os.path.basename(report["last_backup"])
    # the migrated file: renamed, retired, keys byte-identical
    disk = read_raw(cfgfile)
    assert disk["cli_multi_approval"] == "off" and "cli_multi_confirm" not in disk
    assert set(disk["providers"]) == {"groq"}
    assert set(disk["retired_providers"]) == {"tokenrouter", "agentrouter"}
    assert all_key_strings(disk) == before_keys


def test_run_is_idempotent_and_takes_no_second_backup(cfgfile):
    write_raw(cfgfile, old_config())
    migrations.run_migrations()
    backups_after_first = sorted(os.listdir(os.path.join(os.path.dirname(cfgfile),
                                                          "backups")))
    bytes_after_first = open(cfgfile, "rb").read()

    report2 = migrations.run_migrations()

    assert report2["ok"] and not report2["changed"]
    assert report2["schema_version"] == 3
    assert open(cfgfile, "rb").read() == bytes_after_first      # not rewritten
    backups_after_second = sorted(os.listdir(os.path.join(os.path.dirname(cfgfile),
                                                          "backups")))
    assert backups_after_first == backups_after_second          # no new backup


def test_run_with_no_file_is_a_safe_noop(cfgfile):
    assert not os.path.exists(cfgfile)
    report = migrations.run_migrations()
    assert report["ok"] and not report["changed"]
    assert not os.path.exists(cfgfile)                          # nothing created


def test_run_on_a_corrupt_file_leaves_it_untouched(cfgfile):
    with open(cfgfile, "w", encoding="utf-8") as f:
        f.write("{ this is not json")
    before = open(cfgfile, "rb").read()
    report = migrations.run_migrations()
    assert not report["changed"]
    assert open(cfgfile, "rb").read() == before                 # never overwritten


def test_run_on_a_fresh_latest_config_does_nothing(cfgfile):
    write_raw(cfgfile, {"schema_version": 3, "providers": {}, "cli_multi_approval": "off"})
    before = open(cfgfile, "rb").read()
    report = migrations.run_migrations()
    assert report["ok"] and not report["changed"]
    assert open(cfgfile, "rb").read() == before


# --------------------------------------------------------------------------- #
# failure paths — the config is never left worse                               #
# --------------------------------------------------------------------------- #
def test_a_key_losing_migration_is_aborted_and_the_config_kept(cfgfile, monkeypatch):
    write_raw(cfgfile, old_config())
    original = open(cfgfile, "rb").read()

    def _drops_a_key(raw):
        raw = dict(raw)
        raw["providers"] = {"groq": {"api_keys": ["enc.v1:GROQ1"]}}   # loses keys
        return raw

    monkeypatch.setattr(migrations, "_STEPS", ((3, "buggy:drops_a_key", _drops_a_key),))
    report = migrations.run_migrations()

    assert not report["ok"] and report["aborted"]
    assert open(cfgfile, "rb").read() == original               # untouched
    assert report["last_backup"] and os.path.isfile(report["last_backup"])  # kept


def test_a_write_failure_leaves_the_config_untouched(cfgfile, monkeypatch):
    write_raw(cfgfile, old_config())
    original = open(cfgfile, "rb").read()

    def _boom(path, obj):
        raise OSError("disk full")

    monkeypatch.setattr(migrations, "_atomic_write", _boom)
    report = migrations.run_migrations()

    assert not report["ok"] and not report["changed"]
    assert open(cfgfile, "rb").read() == original               # never half-written


# --------------------------------------------------------------------------- #
# integration with config.py                                                   #
# --------------------------------------------------------------------------- #
def test_config_reads_the_migrated_values(cfgfile):
    write_raw(cfgfile, old_config())
    migrations.run_migrations()
    config.invalidate_settings_cache()
    assert config.get_setting("cli_multi_approval") == "off"
    cfg = config.load_config()                                  # loads cleanly
    assert "groq" in cfg["providers"]
    assert cfg["schema_version"] == 3
    assert set(cfg.get("retired_providers") or {}) == {"tokenrouter", "agentrouter"}


def test_ordinary_saves_preserve_the_retired_block(cfgfile):
    """A later flag toggle must not drop the kept keys of removed providers."""
    write_raw(cfgfile, old_config())
    migrations.run_migrations()
    before = all_key_strings(read_raw(cfgfile))
    config.set_flag("some_unrelated_flag", True)                # a normal save
    after = read_raw(cfgfile)
    assert set(after.get("retired_providers") or {}) == {"tokenrouter", "agentrouter"}
    assert all_key_strings(after) == before                     # keys still all there


def test_status_has_the_four_fields(cfgfile):
    write_raw(cfgfile, old_config())
    migrations.run_migrations()
    st = migrations.status()
    assert set(("schema_version", "applied", "last_backup", "keys_count")) <= set(st)
    assert st["schema_version"] == 3
    assert st["keys_count"] == 5


@pytest.mark.skipif(os.name != "posix", reason="POSIX file mode")
def test_backup_and_config_are_0600_on_posix(cfgfile):
    write_raw(cfgfile, old_config())
    report = migrations.run_migrations()
    for p in (cfgfile, report["last_backup"]):
        mode = stat.S_IMODE(os.stat(p).st_mode)
        assert mode == 0o600
