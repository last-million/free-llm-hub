"""
Free LLM Hub — local config store.

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
  "local_api_key": "..."   # or null (open on localhost)
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
from typing import Optional


def _default_config_path() -> str:
    env = os.environ.get("FREE_LLM_HUB_CONFIG")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(os.path.expanduser("~"), ".free-llm-hub", "config.json")


CONFIG_PATH: str = _default_config_path()

_LOCK = threading.RLock()

_EMPTY_CONFIG = {
    "providers": {},
    "default": None,
    "local_api_key": None,
}


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


def load_config() -> dict:
    """Load the config file; return a well-formed dict even if missing/corrupt."""
    path = _config_path()
    cfg: dict = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            cfg = loaded
    except (OSError, ValueError):
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
    return cfg


def save_config(cfg: dict) -> None:
    """Persist config atomically; chmod 0600 on POSIX (best-effort on Windows)."""
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
        if os.name == "posix":
            try:
                os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
            except OSError:
                pass
        # os.replace is atomic, but on Windows an antivirus scanner or a cloud
        # sync client (OneDrive/Dropbox) can briefly hold a handle on the
        # destination or temp file -> PermissionError [WinError 5]. Retry a few
        # times with tiny backoff; if it still fails, fall back to a direct
        # (non-atomic) write so a save NEVER 500s the app. Cross-platform safe.
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
            with open(path, "w", encoding="utf-8") as f:
                f.write(data)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
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
        cfg = load_config()
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
        cfg = load_config()
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
        cfg = load_config()
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
        cfg = load_config()
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
        cfg = load_config()
        cfg["default"] = {"provider": provider, "model": model}
        save_config(cfg)


def get_local_api_key() -> Optional[str]:
    """Optional bearer clients must present on /v1/*; None = open on localhost."""
    with _LOCK:
        cfg = load_config()
    key = cfg.get("local_api_key")
    return key if isinstance(key, str) and key else None


def ensure_local_api_key() -> str:
    """Return the local gateway key, generating + persisting one if absent."""
    with _LOCK:
        cfg = load_config()
        key = cfg.get("local_api_key")
        if isinstance(key, str) and key:
            return key
        key = secrets.token_urlsafe(24)
        cfg["local_api_key"] = key
        save_config(cfg)
        return key
