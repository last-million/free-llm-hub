r"""The user's PERSISTENT environment -- the one place the hub reads or removes it.

On Windows that is HKCU\Environment: what `setx VAR "VALUE"` writes and what
every NEW terminal inherits. The hub never writes it itself; the only thing
that ever put a hub URL there is the user pasting the `setx` block the
manual-setup instructions hand out (app._env_commands). What the hub DOES now
do is remove such a var -- on Disconnect when no other connected tool still
relies on it, or when the user explicitly asks (app.api_env_remove) -- and it
only ever removes a value that points at the hub itself.

REPORTED 2026-09-27: after Disconnect, OpenCode's card said "an env var still
points OpenCode at the hub". HKCU\Environment held OPENAI_BASE_URL (and
LLM_BASE_URL) = http://127.0.0.1:8787, shared by every OpenAI-shaped tool on
the machine, while OpenCode's own provider block was correctly gone.

Everything goes through a swappable backend so tests can never touch the real
registry: the root conftest.py installs MemoryBackend for the whole run. No
child process anywhere here -- winreg + one ctypes broadcast, so no console
window can ever flash (see tests/test_no_console_windows.py).

Off Windows there is no single persistent store we could safely edit (shell
profiles are the user's own files), so the null backend reports nothing and
removes nothing; callers fall back to printed `unset` commands.
"""
import os
import re
import threading

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")

# Never removable through the hub, whatever their value happens to contain.
_PROTECTED = frozenset({
    "PATH", "PATHEXT", "PSMODULEPATH", "TEMP", "TMP", "HOME", "USERPROFILE",
    "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH", "COMSPEC", "SYSTEMROOT",
    "WINDIR", "ONEDRIVE", "ONEDRIVECONSUMER", "ONEDRIVECOMMERCIAL",
})


def valid_name(name):
    """A plain environment variable name (letters, digits, underscore)."""
    return isinstance(name, str) and bool(_NAME_RE.match(name))


def is_protected(name):
    return isinstance(name, str) and name.upper() in _PROTECTED


class _NullBackend:
    """No persistent store we manage (non-Windows)."""
    available = False

    def get(self, name):
        return None

    def names(self):
        return []

    def delete(self, name):
        return False


class _WinRegBackend:
    """HKCU\\Environment via winreg. Reads never raise; delete raises OSError."""
    available = True
    _SUBKEY = "Environment"

    def _key(self, write=False):
        import winreg
        access = winreg.KEY_READ | (winreg.KEY_SET_VALUE if write else 0)
        return winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._SUBKEY, 0, access)

    def get(self, name):
        try:
            import winreg
            with self._key() as k:
                val, _typ = winreg.QueryValueEx(k, name)
        except (OSError, ImportError):
            return None
        return val if isinstance(val, str) else None

    def names(self):
        out = []
        try:
            import winreg
            with self._key() as k:
                i = 0
                while True:
                    try:
                        name, _val, _typ = winreg.EnumValue(k, i)
                    except OSError:
                        break
                    out.append(name)
                    i += 1
        except (OSError, ImportError):
            return []
        return out

    def delete(self, name):
        import winreg
        with self._key(write=True) as k:
            winreg.DeleteValue(k, name)
        _broadcast_env_change()
        return True


def _broadcast_env_change():
    """Tell Explorer the environment changed (what setx does after writing), so
    terminals opened from the Start menu / taskbar from now on stop inheriting
    the removed var. Already-open terminals keep their copy -- nothing can
    change another process's environment. Best-effort, never raises."""
    try:
        import ctypes
        from ctypes import wintypes
        result = wintypes.DWORD(0)
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF,            # HWND_BROADCAST
            0x001A,            # WM_SETTINGCHANGE
            0, "Environment",
            0x0002,            # SMTO_ABORTIFHUNG
            3000, ctypes.byref(result))
    except Exception:                                    # noqa: BLE001
        pass


class MemoryBackend:
    """In-memory stand-in for tests. Names are case-insensitive, as on Windows."""
    available = True

    def __init__(self, values=None):
        self._vals = {}
        self.deleted = []
        for k, v in (values or {}).items():
            self.set(k, v)

    def set(self, name, value):
        self._vals[name.upper()] = (name, value)

    def get(self, name):
        row = self._vals.get(str(name).upper())
        return row[1] if row else None

    def names(self):
        return [n for n, _v in self._vals.values()]

    def delete(self, name):
        if self._vals.pop(str(name).upper(), None) is None:
            raise FileNotFoundError(name)
        self.deleted.append(name)
        return True

    def clear(self):
        self._vals.clear()
        self.deleted = []


_lock = threading.Lock()
_backend = _WinRegBackend() if os.name == "nt" else _NullBackend()


def set_backend(backend):
    """Swap the backend (tests). Returns the previous one."""
    global _backend
    with _lock:
        prev, _backend = _backend, backend
    return prev


def backend():
    return _backend


def available():
    """True when a persistent user environment can be read and edited here."""
    return bool(getattr(_backend, "available", False))


def get(name):
    """The persistent value of `name`, or None. Never raises."""
    if not valid_name(name):
        return None
    try:
        return _backend.get(name)
    except Exception:                                    # noqa: BLE001
        return None


def names():
    try:
        return list(_backend.names())
    except Exception:                                    # noqa: BLE001
        return []


def remove(name):
    """Delete `name` from the persistent user environment. True if it was
    removed; False if it was not there, the name is invalid/protected, or the
    store is not available. Never raises."""
    if not valid_name(name) or is_protected(name) or not available():
        return False
    try:
        with _lock:
            return bool(_backend.delete(name))
    except Exception:                                    # noqa: BLE001
        return False
