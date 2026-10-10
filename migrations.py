"""
Calvoun Free LLM Hub — automatic, versioned config migrations (2026-10-10).

Every install auto-updates (git pull + graceful restart), so a release that
renames or retires a persisted setting must carry old state forward BY ITSELF —
and must NEVER drop or corrupt a stored API key. This module is that step.

Design
------
* Pure core. ``apply(raw) -> (new_raw, applied_ids)``. Each migration is a pure,
  IDEMPOTENT function ``old_raw -> new_raw`` that detects the old shape itself,
  so re-running it is a no-op. The transforms run on the RAW, encrypted-at-rest
  config dict: a stored key string (ciphertext ``enc.v1:…`` or legacy plaintext)
  is MOVED VERBATIM, never decrypted, re-encrypted or rewritten.
* I/O wrapper. ``run_migrations(path=None)`` reads the raw JSON (no decryption)
  under the config lock. If nothing changes it writes nothing (idempotent). When
  a change is due it takes an atomic 0600 backup to
  ``state_dir()/backups/config-<UTC>-premigrate.json`` BEFORE the write, runs the
  KEY-SAFETY check, then atomically writes the new raw dict (0600). A change that
  would REDUCE the stored keys is ABORTED (live config untouched, backup kept).
  Any exception leaves the config untouched, logs one line, and boot continues.

Keys are never deleted. A retired provider's keys are kept under
``retired_providers`` (same bytes, still encrypted) so the owner still has them.

Pure stdlib. Imports ``config`` lazily inside functions (no import cycle: config
never imports this module). Never imports app.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import stat
import tempfile
import time
from typing import Optional

_log = logging.getLogger("migrations")

# The schema version this hub migrates UP TO. Bump when you add a step below.
LATEST = 3

# Providers removed from the registry whose rows (and any keys) are carried
# forward under ``retired_providers`` rather than left in the active map. Only
# genuinely-removed ids belong here (see AGENTS.md: agentrouter 2026-07-31,
# tokenrouter 2026-09-29). A future release that RE-ADDS one must also drop it
# from this tuple — the retire step is version-gated so it never fires on a
# config already at/after the version that retired it.
RETIRED_PROVIDERS = ("tokenrouter", "agentrouter")

# Remembered for the status route; set by the last run_migrations() of the
# process (boot). Never holds a key value — counts and a backup path only.
_LAST_REPORT: dict = {}


# --------------------------------------------------------------------------- #
# Pure migration helpers (old_raw -> new_raw), each idempotent.               #
# --------------------------------------------------------------------------- #
def _rename_cli_multi_approval(raw: dict) -> dict:
    """``cli_multi_confirm`` (bool) -> ``cli_multi_approval`` (str mode).

    The boolean guard became a three-way choice (app.py ``_cm_approval_mode``):
    "dashboard" (default, safe — the owner approves in the dashboard), "chat"
    (unauthenticated "go multi" reply) or "off" (direct start). Mapping:
      * true  (old: require consent)  -> "dashboard"  — never weaker than the
        owner's prior intent, and the hub's own safe default for anything
        unknown/unreadable.
      * false (old: direct start)     -> "off".
    The new key is only written when the owner has not already chosen a mode
    under the new name; the old key is always dropped.
    """
    if "cli_multi_confirm" not in raw:
        return raw
    raw = dict(raw)
    old = raw.pop("cli_multi_confirm", None)
    if "cli_multi_approval" not in raw:
        raw["cli_multi_approval"] = "off" if old is False else "dashboard"
    return raw


def _retire_removed_providers(raw: dict) -> dict:
    """Move rows of removed providers into ``retired_providers`` (keys kept).

    The whole row moves VERBATIM (every ``api_keys`` / ``api_key`` /
    ``_unreadable_api_keys`` string unchanged), so the owner keeps the keys of a
    provider the hub no longer offers. Idempotent: a pid already retired (or
    absent) is left alone; if both maps somehow hold it, the key lists are
    UNIONED (order-preserving, de-duplicated) so nothing is lost.
    """
    providers = raw.get("providers")
    if not isinstance(providers, dict):
        return raw
    present = [p for p in RETIRED_PROVIDERS if p in providers]
    if not present:
        return raw
    raw = dict(raw)
    raw["providers"] = dict(providers)
    retired = dict(raw.get("retired_providers") or {}) \
        if isinstance(raw.get("retired_providers"), dict) else {}
    for pid in present:
        row = raw["providers"].pop(pid)
        if pid in retired and isinstance(retired[pid], dict) and isinstance(row, dict):
            merged = dict(retired[pid])
            merged.setdefault("retired_at", _utc())
            existing_keys = list(merged.get("api_keys") or [])
            seen = set(existing_keys)
            for k in list(row.get("api_keys") or []):
                if k not in seen:
                    existing_keys.append(k)
                    seen.add(k)
            # carry a legacy single key and any parked ciphertext, de-duped
            for field in ("api_key",):
                v = row.get(field)
                if isinstance(v, str) and v and v not in seen:
                    existing_keys.append(v)
                    seen.add(v)
            unreadable = list(merged.get("_unreadable_api_keys") or []) \
                + [k for k in (row.get("_unreadable_api_keys") or [])
                   if k not in (merged.get("_unreadable_api_keys") or [])]
            merged["api_keys"] = existing_keys
            if unreadable:
                merged["_unreadable_api_keys"] = unreadable
            retired[pid] = merged
        else:
            if isinstance(row, dict):
                row = dict(row)
                row.setdefault("retired_at", _utc())
            retired[pid] = row
    raw["retired_providers"] = retired
    return raw


def _to_v3(raw: dict) -> dict:
    """Release 2026-10-10: the CLI-Multi rename and the retired-provider move."""
    raw = _rename_cli_multi_approval(raw)
    raw = _retire_removed_providers(raw)
    return raw


# Ordered (target_version, id, fn). A step runs when the config's effective
# start version is below target_version.
_STEPS = (
    (3, "v3:rename_cli_multi_approval+retire_providers", _to_v3),
)


def _effective_start(raw: dict) -> int:
    """The version the config has really COMPLETED.

    A premature ``schema_version`` stamp (a save between boot start and this
    step) must not make a pending rename look done, so a config still carrying
    the unambiguous pre-v3 key ``cli_multi_confirm`` is treated as < 3 whatever
    its stamp says. Provider presence is NOT used as a signal (a future release
    may legitimately re-add a name), so the retire step stays version-gated.
    """
    try:
        stamped = int(raw.get("schema_version"))
    except (TypeError, ValueError):
        stamped = 1
    if "cli_multi_confirm" in raw and stamped > 2:
        return 2
    return stamped


def apply(raw: dict):
    """Pure: return ``(new_raw, applied_ids)``. Never mutates the input."""
    if not isinstance(raw, dict):
        return raw, []
    new = copy.deepcopy(raw)
    start = _effective_start(new)
    applied = []
    for version, mid, fn in _STEPS:
        if version <= start:
            continue
        before = new
        new = fn(new)
        if new != before:
            applied.append(mid)
    cur = _as_int(new.get("schema_version"), 0)
    new["schema_version"] = cur if cur > LATEST else LATEST
    return new, applied


# --------------------------------------------------------------------------- #
# Key-safety fingerprint (counts + SHA-256, never the values).                #
# --------------------------------------------------------------------------- #
def _iter_key_strings(raw: dict):
    """Yield every stored API-key STRING anywhere in the config (ciphertext or
    plaintext), across active and retired providers and parked-unreadable
    pools. Order-independent; used only for counting and hashing."""
    if not isinstance(raw, dict):
        return
    for mapname in ("providers", "retired_providers"):
        provs = raw.get(mapname)
        if not isinstance(provs, dict):
            continue
        for row in provs.values():
            if not isinstance(row, dict):
                continue
            for field in ("api_keys", "_unreadable_api_keys"):
                v = row.get(field)
                if isinstance(v, list):
                    for k in v:
                        if isinstance(k, str) and k:
                            yield k
            legacy = row.get("api_key")
            if isinstance(legacy, str) and legacy:
                yield legacy


def key_fingerprint(raw: dict) -> dict:
    """``{"count": int, "digests": {sha256: multiplicity}}`` over stored keys.

    The digest is of the STORED string (ciphertext for an encrypted key), so it
    never exposes a secret and is invariant under MOVING a key between provider
    maps — which is exactly what a safe migration does."""
    digests: dict = {}
    count = 0
    for k in _iter_key_strings(raw):
        count += 1
        h = hashlib.sha256(k.encode("utf-8")).hexdigest()
        digests[h] = digests.get(h, 0) + 1
    return {"count": count, "digests": digests}


def _keys_preserved(before: dict, after: dict) -> bool:
    """True iff every key present BEFORE is still present AFTER (additions are
    allowed, losses are not). Multiset containment on the digests."""
    if after.get("count", 0) < before.get("count", 0):
        return False
    a = after.get("digests") or {}
    for h, n in (before.get("digests") or {}).items():
        if a.get(h, 0) < n:
            return False
    return True


# --------------------------------------------------------------------------- #
# I/O helpers.                                                                 #
# --------------------------------------------------------------------------- #
def _utc() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _as_int(v, default=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _read_raw(path: str) -> Optional[dict]:
    """The on-disk config as stored (NO decryption). None when missing/corrupt —
    a corrupt file is never migrated or overwritten."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _premigrate_backup(path: str) -> Optional[str]:
    """Copy the live config aside to backups/config-<UTC>-premigrate.json (0600),
    byte-for-byte, before any migrating write. Returns the path or None."""
    try:
        import config
        backup_dir = config._backup_dir()
    except Exception:                                            # noqa: BLE001
        backup_dir = os.path.join(os.path.dirname(os.path.abspath(path)), "backups")
    try:
        os.makedirs(backup_dir, exist_ok=True)
        dest = os.path.join(backup_dir, "config-%s-premigrate.json" % _utc())
        with open(path, "rb") as a:
            data = a.read()
        fd, tmp = tempfile.mkstemp(prefix=".premigrate-", dir=backup_dir)
        try:
            with os.fdopen(fd, "wb") as b:
                b.write(data)
                b.flush()
                os.fsync(b.fileno())
            if os.name == "posix":
                try:
                    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600
                except OSError:
                    pass
            os.replace(tmp, dest)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return dest
    except OSError:
        return None


def _atomic_write(path: str, obj: dict) -> None:
    """Atomically write the raw config (0600). Mirrors config.save_config's
    durability WITHOUT its encryption pass — the keys here are already stored
    exactly as they must land on disk, and encrypt() is idempotent anyway."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    data = json.dumps(obj, indent=2, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(prefix=".config-migrate-", suffix=".tmp",
                               dir=parent or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if os.name == "posix":
            try:
                os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600
            except OSError:
                pass
        replaced = False
        for _attempt in range(6):
            try:
                os.replace(tmp, path)
                replaced = True
                break
            except PermissionError:
                time.sleep(0.15)
            except OSError:
                break
        if not replaced:
            raise OSError("could not atomically replace config after retries")
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if os.name == "posix":
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError:
            pass


def _config_path(path: Optional[str]) -> Optional[str]:
    if path:
        return path
    try:
        import config
        return config._config_path()
    except Exception:                                            # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# The boot step.                                                              #
# --------------------------------------------------------------------------- #
def run_migrations(path: Optional[str] = None) -> dict:
    """Migrate the on-disk config in place, once, safely.

    Returns a report dict (also remembered for the status route). Writes only
    when a migration changes the config; takes a pre-write backup and refuses a
    write that would lose a stored key. Never raises: a failure keeps the live
    config untouched and boot carries on.
    """
    report = {
        "ok": True, "changed": False, "applied": [], "aborted": False,
        "schema_version": None, "keys_count": 0, "last_backup": None,
        "error": None,
    }
    cfg_path = _config_path(path)
    if not cfg_path:
        report["ok"] = False
        report["error"] = "no config path"
        _remember(report)
        return report
    lock = _lock()
    try:
        with lock:
            with _xlock():
                raw = _read_raw(cfg_path)
                if raw is None:
                    # No file yet (fresh install) or an unreadable one — nothing
                    # to migrate, nothing to risk.
                    report["schema_version"] = None if not os.path.exists(cfg_path) \
                        else "unreadable"
                    _remember(report)
                    return report
                report["schema_version"] = _as_int(raw.get("schema_version"), 1)
                report["keys_count"] = key_fingerprint(raw)["count"]
                new, applied = apply(raw)
                if new == raw:
                    _remember(report)                     # already migrated
                    return report
                before_fp = key_fingerprint(raw)
                after_fp = key_fingerprint(new)
                backup = _premigrate_backup(cfg_path)
                report["last_backup"] = backup
                if not _keys_preserved(before_fp, after_fp):
                    # A migration would drop a stored key — never write it.
                    report["ok"] = False
                    report["aborted"] = True
                    report["error"] = "key-safety check failed; migration aborted"
                    _log.error(
                        "[migrations] ABORTED: a migration would reduce stored "
                        "keys (%d -> %d). The config was NOT changed; a backup is "
                        "at %s.", before_fp["count"], after_fp["count"], backup)
                    _remember(report)
                    return report
                _atomic_write(cfg_path, new)
                try:
                    import config
                    config.invalidate_settings_cache()
                except Exception:                                # noqa: BLE001
                    pass
                report["changed"] = True
                report["applied"] = applied
                report["schema_version"] = _as_int(new.get("schema_version"), LATEST)
                report["keys_count"] = after_fp["count"]
                _log.info("[migrations] applied %s (schema_version -> %d, %d key(s) "
                          "preserved; backup %s)",
                          applied or ["schema_version"], report["schema_version"],
                          after_fp["count"], backup)
                _remember(report)
                return report
    except Exception as exc:                                     # noqa: BLE001
        report["ok"] = False
        report["error"] = repr(exc)
        try:
            _log.warning("[migrations] skipped (config left untouched): %s", exc)
        except Exception:                                        # noqa: BLE001
            pass
        _remember(report)
        return report


def status(path: Optional[str] = None) -> dict:
    """``{schema_version, applied, last_backup, keys_count}`` for the status
    route. Reads the live config (no decryption); ``applied`` / ``last_backup``
    come from this process's last run_migrations()."""
    cfg_path = _config_path(path)
    schema_version = None
    keys_count = 0
    if cfg_path:
        raw = _read_raw(cfg_path)
        if raw is not None:
            schema_version = _as_int(raw.get("schema_version"), 1)
            keys_count = key_fingerprint(raw)["count"]
    last = _LAST_REPORT
    return {
        "schema_version": schema_version if schema_version is not None
        else last.get("schema_version"),
        "applied": list(last.get("applied") or []),
        "last_backup": last.get("last_backup"),
        "keys_count": keys_count,
    }


def _remember(report: dict) -> None:
    global _LAST_REPORT
    _LAST_REPORT = dict(report)


def _lock():
    try:
        import config
        return config._LOCK
    except Exception:                                            # noqa: BLE001
        import threading
        return threading.RLock()


class _NullCtx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _xlock():
    """The cross-process config lock when available, else a no-op."""
    try:
        import config
        return config._cross_process_lock()
    except Exception:                                            # noqa: BLE001
        return _NullCtx()
