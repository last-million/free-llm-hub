"""What THIS machine can run -- probed cheaply, cached ~10 minutes, for
planning (2026-10-10).

THE INCIDENT BEHIND IT: a Multi run planned a Postgres + Stripe app on a
machine with no PostgreSQL, so the app could never run here, and five runs in a
row tried to "deploy" it by hand. A planner that KNOWS what is installed picks
storage that needs no install (SQLite / a JSON file) unless the user asked for
a database server -- and plans an explicit setup phase when they did.

CHEAP: `shutil.which` + ONE `--version` call per tool under a short timeout,
all at once on worker threads; one `docker info` only when the docker CLI
exists; a loopback connect per port (never a listen, never a kill). The result
is cached for TTL seconds and refreshed in the background, so a request path
never waits on it (`snapshot(wait=0)` returns what is cached).

Every side (which / run / port_open / clock) is injectable, and the module-wide
switch ENABLED lets the test suite keep every real command out
(tests/conftest.py sets it False; the probe's own tests pass fakes).
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

ENABLED = True
TTL = 600.0
CMD_TIMEOUT = 4.0
DOCKER_TIMEOUT = 6.0
PORT_TIMEOUT = 0.3
PROBE_DEADLINE = 12.0
COMMON_PORTS = (3000, 5173, 8000, 8080, 5000)
SERVICE_PORTS = (("postgres", 5432), ("mysql", 3306), ("redis", 6379))

# Executables tried in order for each tool. python3 first (POSIX); on Windows
# `python` can be the Microsoft Store alias, which prints "Python was not
# found" -- that parses as no version and `py` (the launcher) is tried next.
_CANDIDATES = {
    "node": ("node",), "npm": ("npm",), "pnpm": ("pnpm",), "yarn": ("yarn",),
    "bun": ("bun",), "python": ("python3", "python", "py"), "pip": ("pip3", "pip"),
    "docker": ("docker",), "psql": ("psql",), "mysql": ("mysql",),
    "redis": ("redis-server", "redis-cli"),
}

_BARE = re.compile(r"^\s*v?(\d+\.\d+(?:\.\d+)?)(?:[-+][\w.]+)?\s*$", re.M)
_VERSION_RES = {
    "node": _BARE, "npm": _BARE, "pnpm": _BARE, "yarn": _BARE, "bun": _BARE,
    "python": re.compile(r"^\s*Python (\d+\.\d+(?:\.\d+)?)", re.M),
    "pip": re.compile(r"^\s*pip (\d+\.\d+(?:\.\d+)?)", re.M),
    "docker": re.compile(r"Docker version (\d+\.\d+(?:\.\d+)?)", re.I),
    "psql": re.compile(r"\(PostgreSQL\)\s*(\d+(?:\.\d+)*)", re.I),
    # MariaDB's client says "Ver 15.1 Distrib 10.11.8-MariaDB": the server
    # version is the Distrib one -- tried FIRST, because "Ver 15.1" comes
    # earlier in the line and a single alternation would match it.
    "mysql": (re.compile(r"Distrib (\d+\.\d+(?:\.\d+)?)-MariaDB", re.I),
              re.compile(r"\bVer (\d+\.\d+(?:\.\d+)?)", re.I)),
    "redis": re.compile(r"(?:\bv=|redis-cli )(\d+\.\d+(?:\.\d+)?)", re.I),
}


def parse_version(tool, text):
    """The version a tool's `--version` output names, or None (no match, an
    error message, a Store alias stub). Accepts CRLF and stderr text."""
    rxs = _VERSION_RES.get(tool)
    if rxs is None or not isinstance(text, str) or not text.strip():
        return None
    clean = text.replace("\r", "")
    for rx in (rxs if isinstance(rxs, tuple) else (rxs,)):
        m = rx.search(clean)
        if m:
            for g in m.groups():
                if g:
                    return g
    return None


def _default_run(argv, timeout):
    """(returncode, stdout+stderr text) or None. No console window, no stdin."""
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
              "stderr": subprocess.PIPE, "timeout": timeout}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        cp = subprocess.run(list(argv), **kwargs)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    out = (cp.stdout or b"") + b"\n" + (cp.stderr or b"")
    return cp.returncode, out.decode("utf-8", errors="replace")


def _default_port_open(port):
    for host in ("127.0.0.1", "::1"):
        try:
            with socket.create_connection((host, port), timeout=PORT_TIMEOUT):
                return True
        except OSError:
            continue
    return False


def _os_name():
    if os.name == "nt":
        return "windows"
    return "macos" if sys.platform == "darwin" else ("linux" if sys.platform.startswith("linux")
                                                     else sys.platform)


def probe(*, which=None, run=None, port_open=None, os_name=None):
    """One fresh look at this machine -> a snapshot dict. Never raises."""
    which = which or shutil.which
    run = run or _default_run
    port_open = port_open or _default_port_open
    tools, flags, ports = {}, {}, {}

    def version_of(key):
        for exe in _CANDIDATES[key]:
            try:
                path = which(exe)
            except Exception:                                    # noqa: BLE001
                path = None
            if not path:
                continue
            got = run([path, "--version"], CMD_TIMEOUT)
            if not got or got[0] != 0:
                continue
            v = parse_version(key, got[1])
            if v:
                return v
        return None

    def tool(key):
        try:
            tools[key] = version_of(key)
        except Exception:                                        # noqa: BLE001
            tools[key] = None

    def daemon():
        try:
            path = which("docker")
        except Exception:                                        # noqa: BLE001
            path = None
        if not path:
            flags["docker_daemon"] = None
            return
        got = run([path, "info", "--format", "{{.ServerVersion}}"], DOCKER_TIMEOUT)
        flags["docker_daemon"] = bool(got and got[0] == 0
                                      and re.search(r"\d+\.\d+", got[1] or ""))

    def port(p):
        try:
            ports[p] = bool(port_open(p))
        except Exception:                                        # noqa: BLE001
            ports[p] = False

    jobs = [threading.Thread(target=tool, args=(k,), daemon=True) for k in _CANDIDATES]
    jobs.append(threading.Thread(target=daemon, daemon=True))
    for p in [p for _n, p in SERVICE_PORTS] + list(COMMON_PORTS):
        jobs.append(threading.Thread(target=port, args=(p,), daemon=True))
    for j in jobs:
        j.start()
    end = time.monotonic() + PROBE_DEADLINE
    for j in jobs:
        j.join(max(0.0, end - time.monotonic()))
    return {
        "at": time.time(),
        "os": os_name or _os_name(),
        "tools": {k: tools.get(k) for k in _CANDIDATES},
        "docker_daemon": flags.get("docker_daemon"),
        "services": {name: bool(ports.get(p)) for name, p in SERVICE_PORTS},
        "busy_ports": sorted(p for p in COMMON_PORTS if ports.get(p)),
    }


# --------------------------------------------------------------------------- #
# Cache: refreshed in the background, never waited on by a request path
# --------------------------------------------------------------------------- #

_LOCK = threading.Lock()
_STATE = {"snap": None, "at": 0.0, "running": False, "done": threading.Event()}


def reset():
    with _LOCK:
        _STATE.update(snap=None, at=0.0, running=False, done=threading.Event())


def _refresh(ev):
    try:
        snap = probe()
    except Exception:                                            # noqa: BLE001
        snap = None
    with _LOCK:
        if snap is not None:
            _STATE["snap"], _STATE["at"] = snap, time.monotonic()
        _STATE["running"] = False
    ev.set()


def snapshot(wait=0.0):
    """The cached snapshot (refreshing it in the background when older than
    TTL), or None. `wait` > 0 waits up to that many seconds for a refresh in
    progress -- for the planner, never for a request path. Never raises."""
    if not ENABLED:
        return None
    try:
        with _LOCK:
            snap, at = _STATE["snap"], _STATE["at"]
            fresh = snap is not None and time.monotonic() - at < TTL
            if not fresh and not _STATE["running"]:
                _STATE["running"] = True
                _STATE["done"] = threading.Event()
                threading.Thread(target=_refresh, args=(_STATE["done"],), daemon=True,
                                 name="envprobe").start()
            ev = _STATE["done"]
        if fresh:
            return snap
        if wait and wait > 0:
            ev.wait(wait)
        with _LOCK:
            return _STATE["snap"]
    except Exception:                                            # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# The ONE short block the planner, the workers and a single session read
# --------------------------------------------------------------------------- #


def _major(v):
    return str(v).split(".")[0] if v else None


def block(snap, who="planner"):
    """THIS MACHINE facts + the storage rule, or "" without a snapshot.

    `who`: "planner" (may add phases: an explicit setup phase), anything else
    (a worker or a single session: says what to install)."""
    if not isinstance(snap, dict):
        return ""
    try:
        return _block(snap, who)
    except Exception:                                            # noqa: BLE001
        return ""


def _block(snap, who):
    t = snap.get("tools") or {}
    facts = []
    facts.append("node %s" % _major(t["node"]) if t.get("node") else "node: not installed")
    if t.get("npm"):
        facts.append("npm %s" % _major(t["npm"]))
    for pm in ("pnpm", "yarn", "bun"):
        if t.get(pm):
            facts.append("%s %s" % (pm, _major(t[pm])))
    py = t.get("python")
    facts.append("python %s" % ".".join(str(py).split(".")[:2]) if py else "python: not installed")
    if not t.get("docker"):
        facts.append("docker: no")
    elif snap.get("docker_daemon") is False:
        facts.append("docker %s (daemon not running)" % _major(t["docker"]))
    else:
        facts.append("docker %s" % _major(t["docker"]))
    svc = snap.get("services") or {}
    missing = []
    for name, key, tool in (("PostgreSQL", "postgres", "psql"), ("MySQL", "mysql", "mysql"),
                            ("Redis", "redis", "redis")):
        port = dict(SERVICE_PORTS)[key]
        if svc.get(key):
            facts.append("%s: running (port %d)" % (name, port))
        elif t.get(tool):
            facts.append("%s %s: installed, not running (port %d closed)"
                         % (name, _major(t[tool]), port))
        elif key == "postgres":
            facts.append("PostgreSQL: not installed (port %d closed)" % port)
        else:
            missing.append(name)
    if missing:
        facts.append("%s: not installed" % ", ".join(missing))
    busy = [str(p) for p in snap.get("busy_ports") or ()]
    line = "THIS MACHINE: " + ", ".join(facts) + (
        "; busy ports: " + ", ".join(busy) if busy else "") + "."
    all_up = all(svc.get(k) for k, _p in SERVICE_PORTS)
    rule = ("The app MUST run here with one command. Prefer storage that needs no "
            "install (SQLite / a JSON file) unless the user ")
    if all_up:
        rule += "asked for a database server."
    else:
        rule += ("explicitly asked for PostgreSQL/MySQL/Redis; if they did and it is "
                 "not installed, " +
                 ("add an explicit setup phase that says exactly what the user must "
                  "install" if who == "planner" else
                  "say exactly what the user must install") +
                 ", and still provide a working local fallback.")
    return line + "\n" + rule


# --------------------------------------------------------------------------- #
# Which requests get it: building an APP (runtime and storage matter), not a
# static page or a question. Several languages; the owner writes French.
# --------------------------------------------------------------------------- #

_BUILD_VERB_RE = re.compile(
    r"\b(build|create|make|develop|implement|code|write|set ?up|scaffold|deploy|ship|"
    r"cr[ée]{1,2}[rz]?|construi[rst]e?z?|d[ée]velopp?e[rz]?|r[ée]alise[rz]?|"
    r"d[ée]ploie[rz]?|d[ée]ployer|fais|faire|crear|construir|desarrollar|criar|"
    r"desenvolver|erstell\w*|entwickl\w*)\b", re.I)
_APP_NOUN_RE = re.compile(
    r"\b(app|apps|application|applications|appli|webapp|web app|api|apis|backend|"
    r"back-end|server|platform|plateforme|marketplace|store|shop|e-?commerce|"
    r"boutique|dashboard|tableau de bord|crm|erp|booking|bookings|r[ée]servations?|"
    r"portal|portail|full[- ]?stack|database|base de donn[ée]es|aplicaci[oó]n|"
    r"plataforma|anwendung)\b", re.I)
_SIZEABLE_CHARS = 280


def wants_block(text):
    """True for a request to BUILD an app (a server, an API, a store, a
    platform...) -- the cases where what is installed decides whether the
    result runs here. A static page, a question or a fix is not."""
    if not isinstance(text, str) or not text.strip():
        return False
    if not _APP_NOUN_RE.search(text):
        return False
    return bool(_BUILD_VERB_RE.search(text)) or len(text) >= _SIZEABLE_CHARS
