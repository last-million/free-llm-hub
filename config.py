"""
Free LLM Hub — local config store.

JSON config at ~/.free-llm-hub/config.json (override with env
FREE_LLM_HUB_CONFIG). Holds per-provider API keys / enabled flags /
custom base URLs, the default provider+model, and an optional local
gateway bearer key. No secrets ever live in code.

Shape:
{
  "providers": {
    "<pid>": {"api_key": "sk-...", "enabled": true, "base_url": null}
  },
  "default": {"provider": "groq", "model": "llama-3.3-70b-versatile"},  # or null
  "local_api_key": "..."   # or null (open on localhost)
}

Pure stdlib: json, os, secrets, stat, tempfile, threading, typing.
"""
from __future__ import annotations

import json
import os
import secrets
import stat
import tempfile
import threading
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
        os.replace(tmp_path, path)
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
    """Return {'api_key','enabled','base_url'} for `pid` with sane defaults."""
    with _LOCK:
        cfg = load_config()
    row = cfg["providers"].get(pid) or {}
    out = dict(_PROVIDER_DEFAULTS)
    if isinstance(row, dict):
        for k in out:
            if k in row and row[k] is not None:
                out[k] = row[k]
    out["enabled"] = bool(out["enabled"])
    return out


def set_provider_config(pid: str, *, api_key: Optional[str] = None,
                        enabled: Optional[bool] = None,
                        base_url: Optional[str] = None) -> None:
    """Partial update for one provider — merges into the existing row, persists.

    Only the keyword args explicitly passed (non-None) are changed; existing
    values are preserved. To CLEAR a value, pass an empty string ('') for
    api_key/base_url — it is stored as None.
    """
    with _LOCK:
        cfg = load_config()
        row = cfg["providers"].get(pid)
        if not isinstance(row, dict):
            row = dict(_PROVIDER_DEFAULTS)
        if api_key is not None:
            row["api_key"] = api_key.strip() or None if isinstance(api_key, str) else api_key
        if enabled is not None:
            row["enabled"] = bool(enabled)
        if base_url is not None:
            row["base_url"] = base_url.strip() or None if isinstance(base_url, str) else base_url
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
