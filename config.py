"""
Calvoun Free LLM Hub — local config store.

JSON config at ~/.free-llm-hub/config.json (override with env
FREE_LLM_HUB_CONFIG). Holds per-provider API keys / enabled flags /
custom base URLs, the default provider+model, and an optional local
gateway bearer key. No secrets ever live in code.

Shape:
{
  "providers": {
    "<pid>": {"api_keys": ["sk-...", "sk-..."], "enabled": true, "base_url": null}
  },
  "default": {"provider": "groq", "model": "llama-3.3-70b-versatile"},  # or null
  "local_api_key": "...",  # or null (open on localhost, gates /v1/*)
  "control_token": "..."   # or null (generated on first use, gates /api/*)
}

Each provider now holds a POOL of keys ("api_keys": list[str]) that the gateway
rotates across for load spreading + failover. Legacy rows that still carry a
single "api_key" string are migrated to api_keys=[that] on load and the old
field is dropped on the next save (fully backward-compatible).

Pure stdlib: json, os, secrets, stat, tempfile, threading, typing.
"""
from __future__ import annotations

import json
import os
import secrets
import stat
import tempfile
import threading
import time
import copy
import contextlib
import datetime
import hashlib
import uuid
from typing import Optional


def _default_config_path() -> str:
    env = os.environ.get("FREE_LLM_HUB_CONFIG")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(os.path.expanduser("~"), ".free-llm-hub", "config.json")


CONFIG_PATH: str = _default_config_path()

_LOCK = threading.RLock()

SCHEMA_VERSION = 2


def _new_hub_mode() -> dict:
    # Existing installs already behave as "on".  Defaulting a migrated config to
    # off would make the new switch lie while leaving CLI files connected.
    return {
        "desired": "on",
        "phase": "unmanaged",
        "revision": 0,
        "generation": None,
        "updated_at": None,
        "clients": {},
    }


def _new_runtime() -> dict:
    return {
        "desired": "running",
        "phase": "running",
        "revision": 0,
        "shutdown_requested_at": None,
        "last_error": None,
    }


def _new_media() -> dict:
    return {
        "revision": 0,
        "priority_mode": "auto",
        "manual_priority": [],
    }


def _new_images() -> dict:
    return {
        "revision": 0,
        "priority_mode": "auto",
        "manual_priority": [],
    }


_EMPTY_CONFIG = {
    "schema_version": SCHEMA_VERSION,
    "providers": {},
    "default": None,
    "local_api_key": None,
    "hub_mode": _new_hub_mode(),
    "runtime": _new_runtime(),
    "media": _new_media(),
    "images": _new_images(),
}


class ConfigCorruptError(RuntimeError):
    """The on-disk config exists but is not a JSON object.

    Read-only callers still receive a safe normalized view.  Every mutating
    caller uses ``strict=True`` and refuses to destroy the recoverable file.
    """


class RevisionConflict(RuntimeError):
    def __init__(self, current_revision: int):
        super().__init__("state revision changed")
        self.current_revision = int(current_revision)


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def _normalize_state(raw, defaults):
    out = copy.deepcopy(defaults)
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key in out:
                out[key] = value
    try:
        out["revision"] = max(0, int(out.get("revision") or 0))
    except (TypeError, ValueError):
        out["revision"] = 0
    return out


@contextlib.contextmanager
def _cross_process_lock():
    """Serialize lifecycle/media compare-and-swap across hub processes."""
    path = _config_path() + ".lock"
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    f = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            if os.path.getsize(path) == 0:
                f.seek(0)
                f.write(b"0")
                f.flush()
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()


def _config_path() -> str:
    # Re-resolve every call so tests / late env changes are honored.
    return _default_config_path()


def _blank_row() -> dict:
    """A fresh, well-formed provider row (new multi-key shape, no legacy field)."""
    return {"enabled": False, "base_url": None, "api_keys": []}


def _normalize_provider_row(row):
    """Return a normalized COPY of one provider row.

    - 'api_keys' becomes a de-duplicated, stripped list[str].
    - A legacy single 'api_key' (str) is migrated into api_keys IFF no api_keys
      list is already present, then the legacy field is dropped (so it vanishes
      from the file on the next save — one-time forward migration).
    Non-dict rows are returned untouched.
    """
    if not isinstance(row, dict):
        return row
    row = dict(row)
    clean = []
    raw_keys = row.get("api_keys")
    if isinstance(raw_keys, list):
        for k in raw_keys:
            if isinstance(k, str):
                s = k.strip()
                if s and s not in clean:
                    clean.append(s)
    if not clean:
        legacy = row.get("api_key")
        if isinstance(legacy, str) and legacy.strip():
            clean = [legacy.strip()]
    row["api_keys"] = clean
    row.pop("api_key", None)  # drop legacy field on next save
    return row


def load_config(strict: bool = False) -> dict:
    """Load and normalize config.

    Read-only callers get a safe view if the file is corrupt.  Mutators pass
    ``strict=True`` so malformed user data is never silently truncated.
    """
    path = _config_path()
    cfg: dict = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            cfg = loaded
        elif strict:
            raise ConfigCorruptError("config root is not a JSON object")
    except FileNotFoundError:
        cfg = {}
    except OSError:
        if strict:
            raise
        cfg = {}
    except ValueError as exc:
        if strict:
            raise ConfigCorruptError("config is not valid JSON") from exc
        cfg = {}
    # Normalize shape (never let callers see a missing key)
    if not isinstance(cfg.get("providers"), dict):
        cfg["providers"] = {}
    else:
        # Migrate every row to the multi-key shape (legacy api_key -> api_keys).
        cfg["providers"] = {
            pid: _normalize_provider_row(row)
            for pid, row in cfg["providers"].items()
        }
    if not isinstance(cfg.get("default"), dict):
        cfg["default"] = None
    if not isinstance(cfg.get("local_api_key"), str) or not cfg.get("local_api_key"):
        cfg["local_api_key"] = cfg.get("local_api_key") if isinstance(cfg.get("local_api_key"), str) and cfg.get("local_api_key") else None
    cfg["schema_version"] = SCHEMA_VERSION
    cfg["hub_mode"] = _normalize_state(cfg.get("hub_mode"), _new_hub_mode())
    if not isinstance(cfg["hub_mode"].get("clients"), dict):
        cfg["hub_mode"]["clients"] = {}
    if cfg["hub_mode"].get("desired") not in ("on", "off"):
        cfg["hub_mode"]["desired"] = "on"
    if cfg["hub_mode"].get("phase") not in (
            "unmanaged", "on", "off", "changing", "conflict", "error"):
        cfg["hub_mode"]["phase"] = "unmanaged"
    cfg["runtime"] = _normalize_state(cfg.get("runtime"), _new_runtime())
    if cfg["runtime"].get("desired") not in ("running", "stopped"):
        cfg["runtime"]["desired"] = "running"
    if cfg["runtime"].get("phase") not in ("running", "draining", "stopped", "error"):
        cfg["runtime"]["phase"] = (
            "stopped" if cfg["runtime"].get("desired") == "stopped" else "running")
    cfg["media"] = _normalize_state(cfg.get("media"), _new_media())
    if cfg["media"].get("priority_mode") not in ("auto", "manual"):
        cfg["media"]["priority_mode"] = "auto"
    if not isinstance(cfg["media"].get("manual_priority"), list):
        cfg["media"]["manual_priority"] = []
    cfg["images"] = _normalize_state(cfg.get("images"), _new_images())
    if cfg["images"].get("priority_mode") not in ("auto", "manual"):
        cfg["images"]["priority_mode"] = "auto"
    if not isinstance(cfg["images"].get("manual_priority"), list):
        cfg["images"]["manual_priority"] = []
    return cfg


def save_config(cfg: dict) -> None:
    """Persist atomically; never fall back to truncating the live file."""
    path = _config_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    data = json.dumps(cfg, indent=2, ensure_ascii=False)
    # Atomic write: temp file in the same dir, then replace.
    fd, tmp_path = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=parent or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if os.name == "posix":
            try:
                os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
            except OSError:
                pass
        # Antivirus/cloud-sync may briefly hold the destination on Windows.
        # Retry the atomic replace, but never truncate the only known-good file.
        replaced = False
        for _attempt in range(6):
            try:
                os.replace(tmp_path, path)
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
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    if os.name == "posix":
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError:
            pass
        try:
            dfd = os.open(parent or ".", os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass


_PROVIDER_DEFAULTS = {"api_key": None, "enabled": False, "base_url": None}


def get_provider_config(pid: str) -> dict:
    """Return {'api_key','api_keys','enabled','base_url'} for `pid`.

    Back-compat: 'api_key' is the FIRST key in the pool (or None). 'api_keys' is
    the full rotation pool (list[str], possibly empty).
    """
    with _LOCK:
        cfg = load_config()
    row = cfg["providers"].get(pid) or {}
    out = dict(_PROVIDER_DEFAULTS)
    keys = []
    if isinstance(row, dict):
        raw_keys = row.get("api_keys")
        if isinstance(raw_keys, list):
            keys = [k for k in raw_keys if isinstance(k, str) and k]
        if row.get("enabled") is not None:
            out["enabled"] = row.get("enabled")
        if row.get("base_url") is not None:
            out["base_url"] = row.get("base_url")
    out["api_keys"] = list(keys)
    out["api_key"] = keys[0] if keys else None
    out["enabled"] = bool(out["enabled"])
    return out


def set_provider_config(pid: str, *, api_key: Optional[str] = None,
                        enabled: Optional[bool] = None,
                        base_url: Optional[str] = None) -> None:
    """Partial update for one provider — merges into the existing row, persists.

    Only the keyword args explicitly passed (non-None) are changed; existing
    values are preserved. Back-compat for the single-Save UI:
      - api_key = '<non-empty>'  -> REPLACE the whole pool with [that key]
      - api_key = ''             -> CLEAR the whole pool
    To multi-key without clobbering, use add_provider_key/remove_provider_key.
    To CLEAR base_url, pass an empty string ('') — it is stored as None.
    """
    with _LOCK:
        cfg = load_config(strict=True)
        row = cfg["providers"].get(pid)
        if not isinstance(row, dict):
            row = _blank_row()
        row.pop("api_key", None)
        if not isinstance(row.get("api_keys"), list):
            row["api_keys"] = []
        if api_key is not None:
            if isinstance(api_key, str):
                s = api_key.strip()
                row["api_keys"] = [s] if s else []
            else:
                row["api_keys"] = [api_key] if api_key else []
        if enabled is not None:
            row["enabled"] = bool(enabled)
        if base_url is not None:
            row["base_url"] = base_url.strip() or None if isinstance(base_url, str) else base_url
        cfg["providers"][pid] = row
        save_config(cfg)


def list_provider_keys(pid: str) -> list:
    """Return the provider's key rotation pool as a list[str] (may be empty)."""
    with _LOCK:
        cfg = load_config()
    row = cfg["providers"].get(pid) or {}
    keys = row.get("api_keys") if isinstance(row, dict) else None
    return [k for k in keys if isinstance(k, str) and k] if isinstance(keys, list) else []


def add_provider_key(pid: str, key: str) -> None:
    """Append one key to the pool: strip, ignore empty, DEDUPE, persist."""
    if not isinstance(key, str):
        return
    key = key.strip()
    if not key:
        return
    with _LOCK:
        cfg = load_config(strict=True)
        row = cfg["providers"].get(pid)
        if not isinstance(row, dict):
            row = _blank_row()
        row.pop("api_key", None)
        keys = row.get("api_keys")
        if not isinstance(keys, list):
            keys = []
        if key not in keys:
            keys.append(key)
        row["api_keys"] = keys
        cfg["providers"][pid] = row
        save_config(cfg)


def remove_provider_key(pid: str, index: int) -> bool:
    """Remove the key at `index` from the pool. Return True if removed + persisted."""
    if not isinstance(index, int) or isinstance(index, bool):
        return False
    with _LOCK:
        cfg = load_config(strict=True)
        row = cfg["providers"].get(pid)
        if not isinstance(row, dict):
            return False
        keys = row.get("api_keys")
        if not isinstance(keys, list) or index < 0 or index >= len(keys):
            return False
        keys.pop(index)
        row["api_keys"] = keys
        row.pop("api_key", None)
        cfg["providers"][pid] = row
        save_config(cfg)
        return True


def clear_provider_keys(pid: str) -> None:
    """Empty the provider's key pool (keeps enabled/base_url), persist."""
    with _LOCK:
        cfg = load_config(strict=True)
        row = cfg["providers"].get(pid)
        if not isinstance(row, dict):
            return
        row["api_keys"] = []
        row.pop("api_key", None)
        cfg["providers"][pid] = row
        save_config(cfg)


def get_default() -> Optional[dict]:
    """Return {'provider': pid, 'model': str} or None."""
    with _LOCK:
        cfg = load_config()
    d = cfg.get("default")
    if isinstance(d, dict) and d.get("provider") and d.get("model"):
        return {"provider": d["provider"], "model": d["model"]}
    return None


def set_default(provider: str, model: str) -> None:
    with _LOCK:
        cfg = load_config(strict=True)
        cfg["default"] = {"provider": provider, "model": model}
        save_config(cfg)


def clear_default() -> None:
    """Explicitly clear the default provider/model (distinct from never having
    set one). Added for settings import/export round-tripping: without this,
    an exported {"default": null} (source machine had none configured) had no
    way to overwrite a target machine's existing default back to 'unset'."""
    with _LOCK:
        cfg = load_config(strict=True)
        cfg["default"] = None
        save_config(cfg)


def get_flag(name: str, default: bool = False) -> bool:
    """Read a top-level boolean flag from the config (survives round-trips)."""
    with _LOCK:
        cfg = load_config()
    v = cfg.get(name)
    return v if isinstance(v, bool) else default


def set_flag(name: str, value: bool) -> None:
    """Persist a top-level boolean flag."""
    with _LOCK:
        cfg = load_config(strict=True)
        cfg[name] = bool(value)
        save_config(cfg)


def get_local_api_key() -> Optional[str]:
    """Optional bearer clients must present on /v1/*; None = open on localhost."""
    with _LOCK:
        cfg = load_config()
    key = cfg.get("local_api_key")
    return key if isinstance(key, str) and key else None


def set_local_api_key(value: Optional[str]) -> None:
    """Explicitly set (or, with None/'', clear) the /v1/* bearer key. Added for
    settings import/export round-tripping (see clear_default() for the same
    reasoning): ensure_local_api_key() only ever GENERATES a fresh key when
    none is set, it cannot restore/clear a SPECIFIC value, which a settings
    restore onto a fresh or differently-configured machine needs to do."""
    with _LOCK:
        cfg = load_config(strict=True)
        cfg["local_api_key"] = value.strip() if isinstance(value, str) and value.strip() else None
        save_config(cfg)


def ensure_local_api_key() -> str:
    """Return the local gateway key, generating + persisting one if absent."""
    with _LOCK:
        cfg = load_config(strict=True)
        key = cfg.get("local_api_key")
        if isinstance(key, str) and key:
            return key
        key = secrets.token_urlsafe(24)
        cfg["local_api_key"] = key
        save_config(cfg)
        return key


def get_control_token() -> Optional[str]:
    """The dashboard/control-plane bearer token, if one has been generated."""
    with _LOCK:
        cfg = load_config()
    token = cfg.get("control_token")
    return token if isinstance(token, str) and token else None


def ensure_control_token() -> str:
    """Return the per-install control-plane token, generating + persisting one
    if absent. Required on every /api/* request (see app.py's
    _local_control_guard). This is what stops a DIFFERENT local OS user who can
    reach the loopback port from driving the control plane: the token lives in
    this 0600 config file, so only the owning account can read it, and it is
    never rendered into the dashboard HTML (any user's browser can load the
    page, only the owning user's shell sees the token printed at startup)."""
    with _LOCK:
        cfg = load_config(strict=True)
        token = cfg.get("control_token")
        if isinstance(token, str) and token:
            return token
        token = secrets.token_urlsafe(32)
        cfg["control_token"] = token
        save_config(cfg)
        return token


def _cas_update(section: str, expected_revision: int, updater) -> dict:
    """Locked compare-and-swap for lifecycle/runtime/media state."""
    with _LOCK:
        with _cross_process_lock():
            cfg = load_config(strict=True)
            current = copy.deepcopy(cfg[section])
            revision = int(current.get("revision") or 0)
            try:
                expected = int(expected_revision)
            except (TypeError, ValueError) as exc:
                raise RevisionConflict(revision) from exc
            if expected != revision:
                raise RevisionConflict(revision)
            replacement = updater(copy.deepcopy(current))
            if replacement is not None:
                current = replacement
            if not isinstance(current, dict):
                raise ValueError("state updater must return a dict or None")
            current["revision"] = revision + 1
            if section == "hub_mode":
                current["updated_at"] = _utc_now()
            cfg[section] = current
            cfg["schema_version"] = SCHEMA_VERSION
            save_config(cfg)
            return copy.deepcopy(current)


def get_hub_mode_state() -> dict:
    with _LOCK:
        return copy.deepcopy(load_config().get("hub_mode") or _new_hub_mode())


def update_hub_mode_state(expected_revision: int, updater) -> dict:
    return _cas_update("hub_mode", expected_revision, updater)


def get_runtime_state() -> dict:
    with _LOCK:
        return copy.deepcopy(load_config().get("runtime") or _new_runtime())


def update_runtime_state(expected_revision: int, updater) -> dict:
    return _cas_update("runtime", expected_revision, updater)


def get_media_state() -> dict:
    with _LOCK:
        return copy.deepcopy(load_config().get("media") or _new_media())


def update_media_state(expected_revision: int, updater) -> dict:
    return _cas_update("media", expected_revision, updater)


def get_images_state() -> dict:
    with _LOCK:
        return copy.deepcopy(load_config().get("images") or _new_images())


def update_images_state(expected_revision: int, updater) -> dict:
    return _cas_update("images", expected_revision, updater)


def new_generation_id() -> str:
    return "%s-%s" % (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), uuid.uuid4().hex[:12])


def state_dir() -> str:
    return os.path.dirname(_config_path())


def snapshots_dir() -> str:
    return os.path.join(state_dir(), "snapshots")


def snapshot_dir(generation: str, cli_id: str) -> str:
    safe_generation = re_safe_component(generation)
    safe_cli = re_safe_component(cli_id)
    return os.path.join(snapshots_dir(), safe_generation, safe_cli)


def re_safe_component(value: str) -> str:
    value = str(value or "")
    safe = "".join(ch for ch in value if ch.isalnum() or ch in ("-", "_", "."))
    if not safe or safe in (".", ".."):
        raise ValueError("invalid state path component")
    return safe


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def intentional_stop_path() -> str:
    return os.path.join(state_dir(), "intentional-stop")


def is_intentionally_stopped() -> bool:
    return os.path.isfile(intentional_stop_path())


def set_intentional_stop() -> str:
    """Atomically create the supervisor-visible intentional-stop marker."""
    path = intentional_stop_path()
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".intentional-stop-", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps({"requested_at": _utc_now()}) + "\n")
            f.flush()
            os.fsync(f.fileno())
        if os.name == "posix":
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def clear_intentional_stop() -> bool:
    try:
        os.remove(intentional_stop_path())
        return True
    except FileNotFoundError:
        return False
