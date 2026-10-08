"""netresolve -- keep the hub working through a short resolver failure.

WHY THIS EXISTS. 2026-10-08 02:48:27 UTC, hub.log: three unrelated provider
hosts (integrate.api.nvidia.com, api.z.ai, zenmux.ai) failed to RESOLVE in the
same second (urllib3 NameResolutionError / getaddrinfo), so a whole chain was
burned in milliseconds and the CLI received a 503. It was seen one second after
the hub finished uploading a ~2 MB request body on a slow uplink; the cause is
NOT established (the resolver may have been starved by that upload, or it was
the local network or the router). A clean 72-lookup test minutes later had no
failure, so the failure is short-lived. No frequency is claimed anywhere.

WHAT IT DOES. `install()` wraps `socket.getaddrinfo` for the whole process:

  * a lookup that SUCCEEDS is returned at once (no delay, no lock held across
    the OS call) and remembered in a small bounded LRU with a timestamp;
  * a lookup that raises `socket.gaierror` (any errno: EAI_AGAIN, EAI_NONAME,
    Windows WSA codes ...) is retried ONCE after a short pause; if it still
    fails and the same lookup succeeded within MAX_STALE (7 days), the last
    known address list is returned instead, and one line is logged (at most
    once per host per 5 minutes);
  * otherwise the ORIGINAL error is re-raised unchanged. A failure is never
    cached, and a name that never resolved never gets a stale answer.

For HTTPS URLs, serving an old address is safe for the same reason it is
useful: TLS still verifies the certificate against the HOSTNAME of the URL
(urllib3 passes `server_hostname` from the URL, not from the address), so a
stale IP can only reach a server that holds a valid certificate for that name.
It cannot make the hub talk to an impostor, and when the address really moved
the connect/TLS step fails exactly as it would have without this module. This
module cannot see the URL scheme: for a plain-http base URL there is no
certificate check, with or without it (a stale address there is no worse than
the DNS answer it replaces, and only ever an address that name resolved to).

NOT TOUCHED: IP literals, `localhost` / `*.localhost`, loopback, bare names
without a dot, `host=None`, and bind (`AI_PASSIVE`) calls are handed to the
original resolver untouched. The machine's DNS/network settings are never read
or changed; this is pure standard library (Python 3.9+), no thread, no file, no
network of its own -- it behaves the same on Windows, macOS, Linux, ARM and
Termux.

Switches: the `dns_stale_cache` setting flag (config.get_flag, default ON; read
only when a lookup has already failed, so a success never pays for a config
read); `install_at_boot()` also honours the environment variable
FREE_LLM_HUB_NO_DNS_CACHE=1 and the module attribute BOOT_INSTALL (the test
suite turns that off so importing the app never patches the process).
"""
import collections
import ipaddress
import logging
import os
import socket
import threading
import time

MAX_ENTRIES = 256            # bounded LRU
MAX_STALE = 7 * 24 * 3600    # a cached success older than this is never served
RETRY_DELAY = 0.25           # one retry, this long after the first failure
LOG_EVERY = 300              # at most one log line per host per 5 minutes

_log = logging.getLogger("free-llm-hub")

# The resolver we wrap. Captured at import; install() re-captures only when
# somebody ELSE replaced socket.getaddrinfo in between. Always looked up through
# the module global at call time, so a test can monkeypatch it.
_import_time = socket.getaddrinfo
_orig = socket.getaddrinfo
_prev = None                 # what socket.getaddrinfo was when install() ran

BOOT_INSTALL = True          # tests/conftest.py turns this off
_sleep = time.sleep          # overridable: tests never really sleep
_now = time.time             # wall clock: monotonic() does not count suspend

_install_lock = threading.Lock()
_lock = threading.Lock()     # guards _cache / _stats / _last_log, never an OS call
_cache = collections.OrderedDict()   # key -> (result list, stored_at)
_last_log = {}               # host -> when the stale line was last logged
_stats = {"lookups": 0, "stored": 0, "failures": 0, "retried": 0, "recovered": 0,
          "stale_served": 0, "stale_refused": 0, "reraised": 0,
          "last_stale_host": None, "last_stale_age": None}


# --------------------------------------------------------------------------- #
# Which calls are ours
# --------------------------------------------------------------------------- #

def _plain_host(host):
    """The host as text, or None when this call is not ours to touch."""
    if host is None:
        return None
    if isinstance(host, (bytes, bytearray)):
        try:
            host = bytes(host).decode("ascii")
        except UnicodeDecodeError:
            return None
    if not isinstance(host, str):
        return None
    host = host.strip()
    return host or None


def _is_ip_literal(host):
    """True for IPv4/IPv6 literals (incl. a %scope suffix) and the old numeric
    spellings ("127.1", "0x7f.1") that getaddrinfo parses without any DNS."""
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        return True
    except ValueError:
        pass
    try:
        socket.inet_aton(host)
        return True
    except (OSError, ValueError, UnicodeError):
        return False


def _eligible(host, flags):
    """The lowercased host when this lookup may be cached/retried, else None."""
    h = _plain_host(host)
    if h is None:
        return None
    try:
        if flags & socket.AI_PASSIVE:
            return None                       # a bind, not a connect
    except TypeError:
        return None
    h = h.lower()
    if "." not in h:
        return None                           # bare name: localhost, a LAN alias
    if h == "localhost" or h.endswith(".localhost"):
        return None
    if _is_ip_literal(h):
        return None
    return h


def _flag_on():
    """The dns_stale_cache flag (default ON). Called only after a failure."""
    try:
        import config
        return bool(config.get_flag("dns_stale_cache", True))
    except Exception:                                            # noqa: BLE001
        return True


# --------------------------------------------------------------------------- #
# The wrapper
# --------------------------------------------------------------------------- #

def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """Drop-in for socket.getaddrinfo (see the module docstring)."""
    args = (host, port, family, type, proto, flags)
    name = _eligible(host, flags)
    if name is None:
        return _orig(*args)
    key = (name, port, family, type, proto, flags)
    try:
        hash(key)
    except TypeError:
        return _orig(*args)
    try:
        result = _orig(*args)
    except socket.gaierror:
        recovered = _recover(key, name, args)
        if recovered is None:
            raise                              # the ORIGINAL error, unchanged
        return recovered
    _store(key, result)
    return result


def _store(key, result):
    try:
        if not result:
            return
        entry = (list(result), _now())
        with _lock:
            _stats["lookups"] += 1
            _stats["stored"] += 1
            _cache[key] = entry
            _cache.move_to_end(key)
            while len(_cache) > MAX_ENTRIES:
                _cache.popitem(last=False)
    except Exception:                                            # noqa: BLE001
        pass


def _recover(key, name, args):
    """After a gaierror: one retry, then the last known good answer. Returns a
    result list, or None to make the caller re-raise the original error."""
    with _lock:
        _stats["lookups"] += 1
        _stats["failures"] += 1
    if not _flag_on():
        with _lock:
            _stats["reraised"] += 1
        return None
    with _lock:
        _stats["retried"] += 1
    try:
        _sleep(RETRY_DELAY)
    except Exception:                                            # noqa: BLE001
        pass
    try:
        result = _orig(*args)
    except socket.gaierror as second:
        reason = second
    else:
        with _lock:
            _stats["recovered"] += 1
        _store(key, result)
        return result
    stale = None
    age = 0.0
    now = _now()
    with _lock:
        entry = _cache.get(key)
        if entry is not None:
            age = max(0.0, now - entry[1])    # a clock that stepped back is age 0
            if age <= MAX_STALE:
                stale = list(entry[0])
                _cache.move_to_end(key)
                _stats["stale_served"] += 1
                _stats["last_stale_host"] = name
                _stats["last_stale_age"] = int(age)
            else:
                del _cache[key]
                _stats["stale_refused"] += 1
        if stale is None:
            _stats["reraised"] += 1
    if stale is not None:
        _log_stale(name, reason, age, now)
    return stale


def _log_stale(name, reason, age, now):
    try:
        with _lock:
            last = _last_log.get(name)
            if last is not None and 0 <= now - last < LOG_EVERY:
                return
            _last_log[name] = now
            if len(_last_log) > 2 * MAX_ENTRIES:
                for host in sorted(_last_log, key=_last_log.get)[:MAX_ENTRIES]:
                    del _last_log[host]
        why = getattr(reason, "strerror", None) or str(reason) or type(reason).__name__
        _log.warning("[dns] resolver failed for %s (%s); using the last known "
                     "address from %ds ago", name, str(why)[:120], int(age))
    except Exception:                                            # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# Install / uninstall / state
# --------------------------------------------------------------------------- #

def install():
    """Wrap socket.getaddrinfo. Idempotent; returns True."""
    global _orig, _prev
    with _install_lock:
        current = socket.getaddrinfo
        if current is getaddrinfo:
            return True
        if current is not _import_time:
            _orig = current                    # somebody else's resolver: wrap it
        _prev = current
        socket.getaddrinfo = getaddrinfo
    return True


def uninstall():
    """Put back what install() found. Only when socket.getaddrinfo is still our
    wrapper (never undo somebody else's later patch). Returns True if removed."""
    global _prev
    with _install_lock:
        if socket.getaddrinfo is not getaddrinfo:
            return False
        socket.getaddrinfo = _prev if _prev is not None else _import_time
        _prev = None
    return True


def installed():
    return socket.getaddrinfo is getaddrinfo


def install_at_boot():
    """The app's single startup line: install unless switched off. Never raises."""
    try:
        if not BOOT_INSTALL:
            return False
        if os.environ.get("FREE_LLM_HUB_NO_DNS_CACHE", "").strip().lower() in (
                "1", "true", "yes", "on"):
            return False
        return install()
    except Exception:                                            # noqa: BLE001
        return False


def stats():
    """A snapshot: counters plus the number of remembered names."""
    with _lock:
        out = dict(_stats)
        out["entries"] = len(_cache)
    out["installed"] = installed()
    out["max_entries"] = MAX_ENTRIES
    out["max_stale_seconds"] = MAX_STALE
    return out


def reset():
    """Forget the cache, the counters and the log throttle (not the install)."""
    with _lock:
        _cache.clear()
        _last_log.clear()
        for k in _stats:
            _stats[k] = None if k.startswith("last_") else 0
