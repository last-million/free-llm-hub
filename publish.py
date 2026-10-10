"""Publish a project's running preview on the internet through a FREE
Cloudflare Quick Tunnel (`cloudflared tunnel --url`, no account).

Pure stdlib, no Flask import. app.py exposes it under /api/publish*; the Build
page, the CLIs and the MCP tools all go through the one `default` Manager.

THE SHAPE
- `Manager.start()` records a tunnel as "starting", spawns cloudflared OUTSIDE
  the lock and returns at once. A daemon reader thread per process watches its
  output; the first valid `https://<label>.trycloudflare.com` flips the tunnel
  to "live". Everything time-driven (the 25 s wait for an address, the retry
  over HTTP/2, the expiry, a process that died) lives in ONE method, `tick()`,
  driven by ONE daemon timer thread -- never a thread per tunnel. Tests call
  `tick()` by hand with a fake clock.
- Expiry is hub-enforced: at the earlier of the monotonic deadline and the
  wall-clock `expires_at` (monotonic time does not advance through suspend on
  Linux/macOS, and this runs on laptops) the whole cloudflared process tree
  is killed and the row stays visible as "expired" so the UI can offer a new
  link. The countdown starts when the address goes live, not when Publish is
  clicked.
- The public URL is a capability: anyone holding it can open the app. It is
  returned ONLY through the token-gated API and is never logged, never put in
  an error message and never kept in the output tail (every line is scrubbed).

SAFETY
- Only 127.0.0.1:<port> is ever exposed. The hub's own port, anything outside
  1024-65535 and a list of well-known service ports are refused; the port must
  answer a plain HTTP request.
- cloudflared is stamped with CALVOUN_TUNNEL=<id>, so a leaked one is
  RECOGNISED at the next boot (`sweep_leftovers`) and nothing else is touched.
- The installer (the Install button, or by itself about a minute after boot:
  `AutoInstaller`, flag `cloudflared_auto_install`) downloads the official
  release over HTTPS from an allowlist of hosts, on every redirect hop, and
  refuses anything without a matching published SHA-256. It never runs what
  it downloaded. The automatic path calls the very same `Manager.install()`.
"""
import atexit
import hashlib
import http.client
import json
import logging
import math
import os
import platform as _platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid

_log = logging.getLogger("free-llm-hub")    # same logger as app.py / workspace

MARKER = "CALVOUN_TUNNEL"
# Which hub owns a tunnel process: the normalised state directory. Two hubs on
# one machine (the owner's real one and a sandboxed test hub on another port)
# must never sweep each other's tunnels; a process without the stamp is
# treated as ours (a tunnel from before the stamp existed).
MARKER_HOME = "CALVOUN_TUNNEL_HOME"
FLAG = "publish_enabled"

DEFAULT_TTL_MINUTES = 60
TTL_CHOICES = (15, 60, 240, 720, 1440)
MAX_TUNNELS = 3
URL_WAIT_SECONDS = 25.0          # no address by then -> retry once over HTTP/2
TICK_SECONDS = 1.0
KEEP_DEAD_ROWS = 10              # expired / stopped / failed rows kept visible

MIN_PORT, MAX_PORT = 1024, 65535
# Well-known service ports that must never be put on the internet by a click:
# databases, caches, brokers, container/cluster control planes, remote desktop
# and file sharing. (22/445/139 are below 1024 and refused by the range rule
# too; they are listed so the message says what the port is.)
DENY_PORTS = frozenset({
    22, 139, 445, 1433, 1521, 2181, 2375, 2376, 2379, 2380, 3306, 3389, 5432,
    5672, 5900, 5901, 5902, 5903, 5984, 5985, 5986, 6379, 6443, 9092, 9200,
    9300, 10250, 11211, 15672, 27017, 27018,
})

# --- the installer ---------------------------------------------------------
RELEASE_API = "https://api.github.com/repos/cloudflare/cloudflared/releases/latest"
METADATA_HOSTS = frozenset({"api.github.com"})
DOWNLOAD_HOSTS = frozenset({"github.com", "objects.githubusercontent.com",
                            "release-assets.githubusercontent.com"})
DOWNLOAD_PATH_PREFIX = "/cloudflare/cloudflared/releases/download/"
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 150 * 1024 * 1024
MAX_BINARY_BYTES = 200 * 1024 * 1024

_ASSETS = {
    ("windows", "amd64"): "cloudflared-windows-amd64.exe",
    ("linux", "amd64"): "cloudflared-linux-amd64",
    ("linux", "arm64"): "cloudflared-linux-arm64",
    ("darwin", "amd64"): "cloudflared-darwin-amd64.tgz",
    ("darwin", "arm64"): "cloudflared-darwin-arm64.tgz",
}
_MANUAL = {
    "windows": "winget install --id Cloudflare.cloudflared",
    "darwin": "brew install cloudflared",
    "linux": "your package manager's 'cloudflared' package "
             "(https://pkg.cloudflare.com/)",
}
_MANUAL_ANY = ("see https://developers.cloudflare.com/cloudflare-one/"
               "connections/connect-networks/downloads/")


class PublishError(Exception):
    """A refusal with a stable machine-readable `.code` and a plain message."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

# The trailing lookahead is the boundary that matters: `https://x.trycloudflare
# .com.evil.net/` and `https://x.trycloudflare.com@evil.net/` both START with a
# valid-looking prefix but the real host is somebody else's.
_URL_RE = re.compile(
    r"https://([a-z0-9][a-z0-9-]{0,62})\.trycloudflare\.com"
    r"(?![A-Za-z0-9@:_%~-]|\.[A-Za-z0-9-])", re.I)
# cloudflared prints its own control endpoint (https://api.trycloudflare.com/
# tunnel) in error messages; it is not a tunnel.
_RESERVED_LABELS = frozenset({"api", "www"})
_SCRUB_RE = re.compile(
    r"(?:https?://)?[A-Za-z0-9][A-Za-z0-9.-]*trycloudflare\.com[^\s\"'|)>]*", re.I)


def parse_tunnel_url(text):
    """The first valid public address in `text`, normalised to
    `https://<label>.trycloudflare.com`, else None. The host is re-validated
    with urllib after the regex: https only, exactly `<label>.trycloudflare.com`,
    no userinfo/port, never a reserved label."""
    for m in _URL_RE.finditer(str(text or "")):
        host = ("%s.trycloudflare.com" % m.group(1)).lower()
        if m.group(1).lower() in _RESERVED_LABELS:
            continue
        try:
            u = urllib.parse.urlsplit("https://" + host)
        except ValueError:
            continue
        if (u.scheme == "https" and u.hostname == host and u.port is None
                and not u.username and not u.password
                and host.endswith(".trycloudflare.com")):
            return "https://" + host
    return None


def scrub(text):
    """Remove every trycloudflare address from `text` (logs, tails, errors)."""
    return _SCRUB_RE.sub("<link>", str(text or ""))


def _norm_dir(path):
    return os.path.normcase(os.path.abspath(str(path)))


def _norm_machine(machine):
    m = (machine or "").lower()
    if m in ("x86_64", "amd64", "x64"):
        return "amd64"
    if m in ("aarch64", "arm64", "armv8", "armv8l"):
        return "arm64"
    return m


def platform_key(system=None, machine=None):
    s = (system if system is not None else _platform.system() or "").lower()
    m = _norm_machine(machine if machine is not None else _platform.machine())
    return s, m


def asset_for(system=None, machine=None):
    """The official release asset for this OS/arch, or None."""
    return _ASSETS.get(platform_key(system, machine))


def manual_install_hint(system=None):
    s = platform_key(system)[0]
    return _MANUAL.get(s, _MANUAL_ANY)


def _no_download_text(system, machine):
    """The one sentence for a platform with no official build to download."""
    return ("This computer (%s/%s) has no automatic download. Install cloudflared "
            "yourself with: %s, then reload this page."
            % (system or "?", machine or "?", manual_install_hint(system)))


def check_port(port, hub_ports=()):
    """Raise forbidden_port unless `port` may be put on the internet."""
    if isinstance(port, bool) or not isinstance(port, int):
        raise PublishError("forbidden_port",
                           "The port must be a whole number between %d and %d."
                           % (MIN_PORT, MAX_PORT))
    if port in set(hub_ports or ()):
        raise PublishError("forbidden_port",
                           "That is the port of the hub itself; publishing it "
                           "would put the control panel on the internet.")
    if port in DENY_PORTS:
        raise PublishError("forbidden_port",
                           "Port %d belongs to a database, remote-access or "
                           "infrastructure service and is never published." % port)
    if port < MIN_PORT or port > MAX_PORT:
        raise PublishError("forbidden_port",
                           "Only ports %d-%d can be published." % (MIN_PORT, MAX_PORT))
    return port


def check_ttl(ttl_minutes):
    """Minutes -> validated int (None = the default). Strict: a string or a
    bool is not a TTL."""
    if ttl_minutes is None:
        return DEFAULT_TTL_MINUTES
    if (isinstance(ttl_minutes, bool) or not isinstance(ttl_minutes, int)
            or ttl_minutes not in TTL_CHOICES):
        raise PublishError("bad_ttl", "The link can last %s minutes."
                           % ", ".join(str(c) for c in TTL_CHOICES))
    return ttl_minutes


def looks_like_cloudflared(name, cmdline=()):
    """The boot sweep's process test: the executable is cloudflared."""
    for cand in [name] + list(cmdline or ())[:1]:
        base = os.path.basename(str(cand or "")).lower()
        if base.endswith(".exe"):
            base = base[:-4]
        if base == "cloudflared" or base.startswith("cloudflared-"):
            return True
    return False


# --------------------------------------------------------------------------- #
# Real-world effects (module level so the test suite can stub every one)
# --------------------------------------------------------------------------- #

def _bin_dir():
    import config
    return os.path.join(config.state_dir(), "bin")


def _exe_name():
    return "cloudflared.exe" if os.name == "nt" else "cloudflared"


def _find_cloudflared(bin_dir=None):
    """PATH first, then the hub's own bin directory."""
    found = shutil.which("cloudflared")
    if found:
        return found
    cand = os.path.join(bin_dir or _bin_dir(), _exe_name())
    if os.path.isfile(cand) and os.access(cand, os.X_OK):
        return cand
    return None


def _home():
    """This hub's identity for the sweep: its state directory, normalised."""
    try:
        import config
        return os.path.normcase(os.path.abspath(config.state_dir()))
    except Exception:                                            # noqa: BLE001
        return ""


def _child_env(tunnel_id, home=""):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("TUNNEL_")}
    try:
        import agentic_chat
        agentic_chat.strip_hub_port_vars(env)
    except Exception:                                            # noqa: BLE001
        pass
    env[MARKER] = tunnel_id
    if home:
        env[MARKER_HOME] = home
    return env


def _spawn(argv, env):
    """Start cloudflared with no console window, its own session/process group
    (so killing the tree can never reach the hub), stderr merged into stdout."""
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
              stderr=subprocess.STDOUT, env=env, bufsize=1,
              universal_newlines=True, encoding="utf-8", errors="replace")
    if os.name == "nt":
        kw["creationflags"] = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                               | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(argv, **kw)


def _kill_tree(popen):
    """Kill `popen` and everything it started. Windows: taskkill /T walks the
    tree. POSIX: it was started in its own session, so its group is its pid --
    and if that is somehow OUR group, only the pid is killed. A process that
    already exited is NOT killed by pid on Windows: its pid may belong to
    somebody else by now (measured: a late reader-thread cleanup killed the next
    tunnel's launcher). Never raises."""
    if popen is None:
        return
    exited = popen.poll() is not None
    try:
        if os.name == "nt":
            if not exited:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(popen.pid)],
                               capture_output=True, timeout=15,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            import signal
            pgid = popen.pid        # its own session: the group id is the pid
            if not exited:
                try:
                    pgid = os.getpgid(popen.pid)
                except OSError:
                    pgid = popen.pid
            if pgid != os.getpgrp():
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif not exited:
                popen.kill()
    except Exception:                                            # noqa: BLE001
        try:
            popen.kill()
        except Exception:                                        # noqa: BLE001
            pass
    try:
        popen.wait(timeout=3)
    except Exception:                                            # noqa: BLE001
        pass
    # The pipe is NOT closed here: the reader thread may be blocked in a read
    # on it, and closing a buffered stream from another thread waits for that
    # read to return (measured: a 46-58 s hang). The reader closes its own
    # stream when the pipe ends.


def _http_probe(port, timeout=2.0):
    """Does something on 127.0.0.1:port speak HTTP? Any status line counts --
    a dev server that is compiling answers 5xx and is still an HTTP server."""
    conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=timeout)
    try:
        conn.request("GET", "/", headers={
            "Host": "127.0.0.1:%d" % int(port), "Connection": "close",
            "Accept": "*/*", "User-Agent": "calvoun-publish-probe"})
        resp = conn.getresponse()
        return 100 <= resp.status <= 599
    except Exception:                                            # noqa: BLE001
        return False
    finally:
        try:
            conn.close()
        except Exception:                                        # noqa: BLE001
            pass


def _preview_port(project_dir):
    """The port of the project's running preview, or None."""
    import workspace
    st = workspace.status(project_dir) or {}
    port = st.get("port")
    if st.get("running") and isinstance(port, int) and not isinstance(port, bool):
        return port
    return None


def _hub_ports():
    ports = {8787}
    try:
        ports.add(int(os.environ.get("PORT") or 8787))
    except (TypeError, ValueError):
        pass
    try:
        import agentic_chat
        ports.add(int(agentic_chat._port()))
    except Exception:                                            # noqa: BLE001
        pass
    return ports


def _flag_on():
    try:
        import config
        return config.get_flag(FLAG, True)
    except Exception:                                            # noqa: BLE001
        return True


def _iter_processes():
    import psutil
    return psutil.process_iter(["pid", "name"])


def _hub_pids():
    try:
        import agent_servers
        return set(agent_servers.hub_pids())
    except Exception:                                            # noqa: BLE001
        return set()


def _check_url(url, hosts):
    """https, an allowlisted host, no credentials, default port."""
    try:
        u = urllib.parse.urlsplit(str(url))
        host = (u.hostname or "").lower()
        port = u.port
    except ValueError:
        raise PublishError("install_failed", "The download address is not valid.")
    if (u.scheme != "https" or host not in hosts or u.username or u.password
            or port not in (None, 443)):
        raise PublishError("install_failed",
                           "Refusing to download from an address that is not an "
                           "official GitHub release host.")
    return u


class _AllowlistRedirect(urllib.request.HTTPRedirectHandler):
    """Every redirect hop is checked, not only the first URL."""

    def __init__(self, hosts):
        super().__init__()
        self.hosts = frozenset(hosts)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl, self.hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _https_fetch(url, sink, max_bytes, on_progress=None, hosts=DOWNLOAD_HOSTS,
                 timeout=30):
    """Stream `url` into `sink(chunk)`. HTTPS + allowlist on every hop, a hard
    size cap. Returns the byte count."""
    _check_url(url, hosts)
    opener = urllib.request.build_opener(_AllowlistRedirect(hosts))
    req = urllib.request.Request(url, headers={
        "User-Agent": "calvoun-hub-publish", "Accept": "application/octet-stream, "
        "application/vnd.github+json, */*"})
    done = 0
    with opener.open(req, timeout=timeout) as resp:
        try:
            total = int(resp.headers.get("Content-Length") or 0)
        except ValueError:
            total = 0
        if total and total > max_bytes:
            raise PublishError("install_failed", "The download is larger than expected.")
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            done += len(chunk)
            if done > max_bytes:
                raise PublishError("install_failed", "The download is larger than expected.")
            sink(chunk)
            if on_progress:
                on_progress(done, total)
    return done


def checksum_from_body(body, name):
    """A sha256 listed next to exactly `name` on one line of the release notes
    (lenient about the layout: `hash  name`, `name: hash`, table rows). Two
    different hashes on that line, or none -> None (refuse)."""
    name_re = re.compile(r"(?<![\w.-])%s(?![\w.-])" % re.escape(name))
    found = set()
    for line in str(body or "").splitlines():
        if name_re.search(line):
            found.update(h.lower() for h in re.findall(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])", line))
    return found.pop() if len(found) == 1 else None


def _extract_cloudflared(tgz_path, out_path):
    """Pull the single `cloudflared` file out of the macOS archive WITHOUT
    extract()/extractall(): refuse the whole archive if any member name is
    absolute, has a '..' part or a backslash, then stream only that member."""
    with tarfile.open(tgz_path, "r:gz") as tf:
        members = tf.getmembers()
        for m in members:
            n = m.name
            parts = n.replace("\\", "/").split("/")
            if (n.startswith(("/", "\\")) or "\\" in n or ".." in parts
                    or re.match(r"^[A-Za-z]:", n)):
                raise PublishError("install_failed",
                                   "The downloaded archive has an unsafe path; refused.")
        wanted = [m for m in members
                  if m.isfile() and re.sub(r"^(\./)+", "", m.name) == "cloudflared"]
        if len(wanted) != 1:
            raise PublishError("install_failed",
                               "The downloaded archive does not contain cloudflared.")
        member = wanted[0]
        if member.size <= 0 or member.size > MAX_BINARY_BYTES:
            raise PublishError("install_failed", "The cloudflared file has a bad size.")
        src = tf.extractfile(member)
        written = 0
        with open(out_path, "wb") as dst:
            while True:
                chunk = src.read(256 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > member.size or written > MAX_BINARY_BYTES:
                    raise PublishError("install_failed", "The archive lied about its size.")
                dst.write(chunk)


# --------------------------------------------------------------------------- #
# The tunnel record
# --------------------------------------------------------------------------- #

class _Tunnel:
    def __init__(self, tid, project_dir, port, source, ttl_seconds, now_wall):
        self.id = tid
        self.project_dir = project_dir
        self.port = port
        self.source = source
        self.ttl_seconds = ttl_seconds
        self.state = "starting"
        self.error = None
        self.url = None
        self.started_at = now_wall
        self.expires_wall = now_wall + ttl_seconds     # provisional until live
        self.deadline = None                            # monotonic, set when live
        self.url_deadline = None                        # monotonic, per attempt
        self.attempt = 1
        self.popen = None
        self.ended = None                               # monotonic, when it died
        self.tail = []                                  # scrubbed output lines

    def active(self):
        return self.state in ("starting", "live")

    def name(self):
        return os.path.basename(self.project_dir.rstrip("\\/")) or self.project_dir


class Manager:
    """All the state of the published tunnels. Every outside effect is a
    constructor argument (default: the real thing) so the tests can run it on a
    fake cloudflared, a fake clock and a fake downloader."""

    def __init__(self, *, locate=None, launcher=None, clock=None, wall=None,
                 spawn=None, kill=None, probe=None, preview_port=None,
                 hub_ports=None, bin_dir=None, flag=None, fetch=None,
                 system=None, version_of=None, procs=None, proc_match=None,
                 home=None, timer=True, url_timeout=URL_WAIT_SECONDS, tick_seconds=TICK_SECONDS):
        self._locate_fn = locate
        self._launcher = launcher or (lambda path: [path])
        self._clock = clock or time.monotonic
        self._wall = wall or time.time
        self._spawn_fn = spawn
        self._kill_fn = kill
        self._probe_fn = probe
        self._preview_fn = preview_port
        self._hub_ports_fn = hub_ports
        self._bin_dir_fn = bin_dir
        self._flag_fn = flag
        self._fetch_fn = fetch
        self._system = system          # () -> (system, machine)
        self._version_fn = version_of
        self._procs_fn = procs
        self._proc_match = proc_match or looks_like_cloudflared
        self._home_fn = home          # () -> this hub's identity (state dir)
        self._use_timer = timer
        self._url_timeout = float(url_timeout)
        self._tick_seconds = float(tick_seconds)
        self._lock = threading.RLock()
        self._tunnels = {}
        self._timer = None
        self._stop_evt = threading.Event()
        self._version_cache = {}
        self._install = {"running": False, "error": None, "stage": None,
                         "progress": None, "thread": None, "on_done": None}
        self._auto = None             # the AutoInstaller whose fields status() shows

    def attach_auto(self, auto):
        """status()["cloudflared"] gains `auto.view(...)`'s auto_* fields."""
        self._auto = auto

    # ----------------------------------------------------------- plumbing --
    def _home(self):
        return self._home_fn() if self._home_fn else _home()

    def _bin(self):
        return self._bin_dir_fn() if self._bin_dir_fn else _bin_dir()

    def _find(self):
        if self._locate_fn:
            return self._locate_fn()
        return _find_cloudflared(self._bin())

    def _enabled(self):
        return bool(self._flag_fn() if self._flag_fn else _flag_on())

    def _require_enabled(self):
        if not self._enabled():
            raise PublishError("disabled", "Publishing is switched off in the settings.")

    def _sys(self):
        return platform_key(*self._system()) if self._system else platform_key()

    def _spawn_proc(self, argv, env):
        return (self._spawn_fn or _spawn)(argv, env)

    def _kill(self, popen):
        if popen is None:
            return
        if self._kill_fn:
            self._kill_fn(popen)
        else:
            _kill_tree(popen)

    def _probe(self, port):
        return (self._probe_fn or _http_probe)(port)

    def _ports_of_hub(self):
        return set(self._hub_ports_fn() if self._hub_ports_fn else _hub_ports())

    def _preview(self, project_dir):
        return (self._preview_fn or _preview_port)(project_dir)

    # --------------------------------------------------------------- views --
    def _view(self, t, now_m=None, now_w=None):
        now_m = self._clock() if now_m is None else now_m
        now_w = self._wall() if now_w is None else now_w
        remaining = 0
        expires = t.expires_wall
        if t.state == "live" and t.deadline is not None:
            left = max(0.0, min(t.deadline - now_m, t.expires_wall - now_w))
            remaining = int(math.ceil(left))
            expires = now_w + left
        return {"id": t.id, "project_dir": t.project_dir, "port": t.port,
                "url": t.url if t.state == "live" else None,
                "state": t.state, "error": t.error, "source": t.source,
                "started_at": t.started_at, "expires_at": expires,
                "ttl_seconds": t.ttl_seconds, "remaining_seconds": remaining}

    def status(self, project_dir=None):
        now_m, now_w = self._clock(), self._wall()
        want = _norm_dir(project_dir) if project_dir else None
        with self._lock:
            rows = [self._view(t, now_m, now_w) for t in self._tunnels.values()
                    if want is None or _norm_dir(t.project_dir) == want]
        rows.sort(key=lambda r: r["started_at"], reverse=True)
        return {"server_time": now_w, "enabled": self._enabled(),
                "cloudflared": self._cloudflared_info(), "tunnels": rows,
                "limits": {"default_ttl_minutes": DEFAULT_TTL_MINUTES,
                           "ttl_choices": list(TTL_CHOICES),
                           "max_tunnels": MAX_TUNNELS}}

    def _cloudflared_info(self):
        path = self._find()
        system, machine = self._sys()
        asset = _ASSETS.get((system, machine))
        with self._lock:
            inst = dict(self._install)
        error = inst["error"]
        if path:
            error = None
        elif asset is None and not error:
            error = _no_download_text(system, machine)
        enabled = self._enabled()
        info = {"available": bool(path), "path": path,
                "version": self._version(path) if path else None,
                "platform": "%s/%s" % (system, machine),
                "installable": asset is not None and enabled,
                "installing": bool(inst["running"]), "install_error": error,
                "install_stage": inst["stage"], "install_progress": inst["progress"]}
        auto = self._auto
        if auto is not None:            # lock-free: never waits on the AutoInstaller
            try:
                info.update(auto.view(bool(path), asset is not None, enabled, error))
            except Exception:                                    # noqa: BLE001
                pass
        return info

    def _version(self, path):
        """Never spawned per poll. An installed binary's version is the tag
        recorded at install time (nothing downloaded is run during install);
        any other binary is asked once (`--version`) and the answer cached."""
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = None
        key = (path, mtime)
        if key in self._version_cache:
            return self._version_cache[key]
        ver = None
        side = os.path.join(os.path.dirname(path), "cloudflared.version")
        try:
            if os.path.normcase(os.path.dirname(os.path.abspath(path))) == \
                    os.path.normcase(os.path.abspath(self._bin())) and os.path.isfile(side):
                with open(side, encoding="utf-8") as fh:
                    ver = fh.read().strip()[:40] or None
            elif self._version_fn:
                ver = self._version_fn(path)
            else:
                out = subprocess.run(self._launcher(path) + ["--version"],
                                     capture_output=True, text=True, timeout=5,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                m = re.search(r"version\s+(\S+)", out.stdout or "")
                ver = m.group(1) if m else None
        except Exception:                                        # noqa: BLE001
            ver = None
        self._version_cache[key] = ver
        return ver

    # --------------------------------------------------------------- start --
    def _find_active(self, project_dir, port, skip_id=None):
        want = _norm_dir(project_dir)
        for t in self._tunnels.values():
            if (t.active() and t.id != skip_id and t.port == port
                    and _norm_dir(t.project_dir) == want):
                return t
        return None

    def _count_active(self, skip_id=None):
        return sum(1 for t in self._tunnels.values() if t.active() and t.id != skip_id)

    def _plan(self, project_dir, port, ttl_minutes, source, replace_id=None):
        """Validate everything and reserve a "starting" row. Returns
        (tunnel, created). Nothing is spawned here."""
        self._require_enabled()
        if not isinstance(project_dir, str) or not project_dir.strip():
            raise PublishError("no_preview", "No project was given.")
        project_dir = os.path.abspath(project_dir)
        ttl_min = check_ttl(ttl_minutes)
        if port is None:
            port = self._preview(project_dir)
            if port is None:
                raise PublishError("no_preview",
                                   "Nothing is running for this project. Press Run "
                                   "first, then publish.")
        check_port(port, self._ports_of_hub())
        with self._lock:
            existing = self._find_active(project_dir, port, skip_id=replace_id)
            if existing:
                return existing, False
            if self._count_active(skip_id=replace_id) >= MAX_TUNNELS:
                raise PublishError("too_many", "%d links are already online; stop one "
                                   "first." % MAX_TUNNELS)
        path = self._find()
        if not path:
            raise PublishError("no_cloudflared",
                               "cloudflared is not installed. Use Install cloudflared "
                               "first (a free download from Cloudflare).")
        if not self._probe(port):
            raise PublishError("not_http", "Nothing on port %d answers a web request, "
                               "so there is nothing to publish." % port)
        t = _Tunnel(uuid.uuid4().hex[:12], project_dir, port,
                    "agent" if source == "agent" else "build", ttl_min * 60,
                    self._wall())
        with self._lock:
            existing = self._find_active(project_dir, port, skip_id=replace_id)
            if existing:
                return existing, False
            if self._count_active(skip_id=replace_id) >= MAX_TUNNELS:
                raise PublishError("too_many", "%d links are already online; stop one "
                                   "first." % MAX_TUNNELS)
            t.url_deadline = self._clock() + self._url_timeout
            # A new link for the same project/port supersedes its dead rows.
            for old in [o for o in self._tunnels.values()
                        if not o.active() and o.port == port
                        and _norm_dir(o.project_dir) == _norm_dir(project_dir)]:
                del self._tunnels[old.id]
            self._tunnels[t.id] = t
            self._prune()
        t._path = path
        return t, True

    def _argv(self, t, path):
        base = list(self._launcher(path)) + [
            "tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:%d" % t.port,
            "--http-host-header", "127.0.0.1:%d" % t.port]
        if t.attempt > 1:
            base += ["--protocol", "http2"]
        return base

    def _launch(self, t, path):
        """Spawn (outside the lock) and attach the process to its row."""
        try:
            popen = self._spawn_proc(self._argv(t, path), _child_env(t.id, self._home()))
        except Exception as exc:                                 # noqa: BLE001
            with self._lock:
                self._tunnels.pop(t.id, None)
            if not isinstance(exc, (OSError, ValueError)):
                raise
            raise PublishError("no_cloudflared",
                               "cloudflared could not be started (%s)."
                               % (getattr(exc, "strerror", None) or type(exc).__name__))
        with self._lock:
            keep = t.state == "starting" and t.popen is None
            if keep:
                t.popen = popen
        if not keep:
            self._kill(popen)
            return
        threading.Thread(target=self._pump, args=(t, popen), daemon=True,
                         name="publish-reader-%s" % t.id).start()

    def start(self, project_dir, port=None, ttl_minutes=None, source="build"):
        t, created = self._plan(project_dir, port, ttl_minutes, source)
        if created:
            _log.info("[publish] starting a tunnel for %s (id %s)", t.name(), t.id)
            self._launch(t, t._path)
            self._ensure_timer()
        return self._snapshot(t)

    def _snapshot(self, t):
        with self._lock:
            return self._view(t)

    def renew(self, tunnel_id, ttl_minutes=None):
        """Stop the tunnel and start a NEW one for the same project and port
        with a fresh TTL -- a new random address. The old row is replaced."""
        self._require_enabled()
        with self._lock:
            old = self._tunnels.get(tunnel_id)
            if old is None:
                raise PublishError("not_found", "No such link.")
            project_dir, port, source = old.project_dir, old.port, old.source
            if ttl_minutes is None:
                mins = old.ttl_seconds // 60
                ttl_minutes = mins if mins in TTL_CHOICES else DEFAULT_TTL_MINUTES
        t, created = self._plan(project_dir, port, ttl_minutes, source,
                                replace_id=tunnel_id)
        with self._lock:
            gone = self._tunnels.pop(tunnel_id, None)
            popen = None
            if gone is not None:
                if gone.active():
                    gone.state, gone.error = "stopped", None
                    gone.url = None
                popen, gone.popen = gone.popen, None
        self._kill(popen)
        if created:
            _log.info("[publish] new link for %s (id %s replaces %s)", t.name(), t.id, tunnel_id)
            self._launch(t, t._path)
            self._ensure_timer()
        return self._snapshot(t)

    def stop(self, tunnel_id):
        """A starting/live tunnel is killed and its row stays as "stopped".
        Stopping a row that is already finished dismisses it from the list."""
        with self._lock:
            t = self._tunnels.get(tunnel_id)
            if t is None:
                raise PublishError("not_found", "No such link.")
            if not t.active():
                del self._tunnels[tunnel_id]
                return self._view(t)
            self._finish(t, "stopped", None)
            popen, t.popen = t.popen, None
            view = self._view(t)
        self._kill(popen)
        _log.info("[publish] tunnel %s for %s stopped", t.id, t.name())
        return view

    def project_stopped(self, project_dir):
        """The preview of `project_dir` stopped: its tunnels go with it."""
        want = _norm_dir(project_dir)
        victims = []
        with self._lock:
            for t in list(self._tunnels.values()):
                if t.active() and _norm_dir(t.project_dir) == want:
                    self._finish(t, "stopped", "The preview was stopped.")
                    victims.append((t.id, t.name(), t.popen))
                    t.popen = None
        for tid, name, popen in victims:
            self._kill(popen)
            _log.info("[publish] tunnel %s for %s stopped with its preview", tid, name)
        return [v[0] for v in victims]

    def has_active(self, project_dir):
        want = _norm_dir(project_dir)
        with self._lock:
            return any(t.active() and _norm_dir(t.project_dir) == want
                       for t in self._tunnels.values())

    # ------------------------------------------------- state transitions ----
    def _finish(self, t, state, error):
        """Under the lock: move to a terminal state. The public URL is dropped
        the moment it is dead."""
        t.state, t.error, t.url = state, error, None
        t.ended = self._clock()
        t.url_deadline = None

    def _prune(self):
        dead = sorted((t for t in self._tunnels.values() if not t.active()),
                      key=lambda t: t.ended or 0)
        for t in dead[:max(0, len(dead) - KEEP_DEAD_ROWS)]:
            self._tunnels.pop(t.id, None)

    # ----------------------------------------------------------- the pipe --
    def _pump(self, t, popen):
        try:
            for line in popen.stdout:
                self._on_line(t, popen, line)
        except Exception as exc:                                 # noqa: BLE001
            _log.warning("[publish] tunnel %s: reading cloudflared's output failed (%s: %s)",
                         t.id, type(exc).__name__, scrub(str(exc))[:120])
        finally:
            try:
                self._on_exit(t, popen)
            finally:
                try:
                    popen.stdout.close()
                except Exception:                                # noqa: BLE001
                    pass

    def _on_line(self, t, popen, line):
        safe = scrub(line).rstrip()[:200]
        url = parse_tunnel_url(line)
        with self._lock:
            if t.popen is not popen:
                return
            if safe:
                t.tail.append(safe)
                del t.tail[:-12]
            if url and t.state == "starting":
                t.url = url
                t.state = "live"
                t.url_deadline = None
                t.deadline = self._clock() + t.ttl_seconds
                t.expires_wall = self._wall() + t.ttl_seconds
                name = t.name()
            else:
                return
        _log.info("[publish] tunnel %s for %s is live", t.id, name)

    def _on_exit(self, t, popen):
        """The process ended (EOF on its pipe, or tick saw poll() set)."""
        try:
            code = popen.wait(timeout=2)
        except Exception:                                        # noqa: BLE001
            code = popen.poll()
        if code is None:
            return          # closed its output but still runs: tick() watches poll()
        retry = False
        with self._lock:
            if t.popen is not popen or not t.active():
                return
            _log.info("[publish] tunnel %s: cloudflared ended (exit %s, %s, attempt %d)",
                      t.id, code, t.state, t.attempt)
            if t.state == "starting" and t.attempt == 1:
                retry = self._begin_retry(t)
            elif t.state == "starting":
                self._finish(t, "failed", self._no_address_text(code))
                t.popen = None
            else:
                self._finish(t, "failed", "cloudflared stopped unexpectedly (exit %s). "
                             "Press New link to publish again." % code)
                t.popen = None
            failed = t.state == "failed"
        self._kill(popen)       # leftovers of its tree, and the pipe
        if retry:
            self._respawn(t)
        elif failed:
            _log.warning("[publish] tunnel %s for %s failed (%s)", t.id, t.name(), t.error)

    def _begin_retry(self, t):
        """Under the lock: switch a starting tunnel to its second (HTTP/2)
        attempt. The row has no process until `_respawn` attaches one."""
        t.attempt = 2
        t.popen = None
        t.url_deadline = self._clock() + self._url_timeout
        return True

    def _respawn(self, t):
        try:
            path = self._find()
            if not path:
                raise OSError("cloudflared disappeared")
            popen = self._spawn_proc(self._argv(t, path), _child_env(t.id, self._home()))
        except Exception as exc:                                 # noqa: BLE001
            with self._lock:
                if t.state == "starting":
                    self._finish(t, "failed", "cloudflared could not be restarted.")
            _log.warning("[publish] tunnel %s could not retry: %s", t.id, scrub(str(exc)))
            return
        with self._lock:
            keep = t.state == "starting" and t.attempt == 2 and t.popen is None
            if keep:
                t.popen = popen
        if not keep:
            self._kill(popen)
            return
        threading.Thread(target=self._pump, args=(t, popen), daemon=True,
                         name="publish-reader-%s" % t.id).start()

    def _no_address_text(self, code=None):
        return ("cloudflared did not get a public address, even after retrying over "
                "HTTP/2. A network or firewall that blocks Cloudflare's tunnel "
                "(outbound port 7844) is the usual cause.")

    # --------------------------------------------------------------- timer --
    def tick(self, now=None):
        """Everything time-driven. Called by the one timer thread; tests call it
        by hand with a fake clock."""
        now = self._clock() if now is None else now
        wall = self._wall()
        kills, retries, exits = [], [], []
        with self._lock:
            for t in list(self._tunnels.values()):
                if not t.active():
                    continue
                popen = t.popen
                if t.state == "live":
                    if now >= t.deadline or wall >= t.expires_wall:
                        self._finish(t, "expired", None)
                        t.popen = None
                        kills.append((t.id, t.name(), popen, "expired"))
                        continue
                if popen is not None and popen.poll() is not None:
                    exits.append((t, popen))
                    continue
                if (t.state == "starting" and popen is not None
                        and t.url_deadline is not None and now >= t.url_deadline):
                    if t.attempt == 1:
                        self._begin_retry(t)
                        retries.append((t, popen))
                    else:
                        self._finish(t, "failed", self._no_address_text())
                        t.popen = None
                        kills.append((t.id, t.name(), popen, "failed"))
            self._prune()
        for tid, name, popen, state in kills:
            self._kill(popen)
            _log.info("[publish] tunnel %s for %s ended (%s)", tid, name, state)
        for t, popen in exits:
            self._on_exit(t, popen)
        for t, popen in retries:
            self._kill(popen)
            self._respawn(t)

    def _ensure_timer(self):
        if not self._use_timer:
            return
        with self._lock:
            if self._timer is not None and self._timer.is_alive():
                return
            self._stop_evt.clear()
            self._timer = threading.Thread(target=self._timer_loop, daemon=True,
                                           name="publish-timer")
            self._timer.start()

    def _timer_loop(self):
        while not self._stop_evt.wait(self._tick_seconds):
            try:
                self.tick()
            except Exception as exc:                             # noqa: BLE001
                _log.warning("[publish] timer tick failed: %s", scrub(str(exc)))

    # ------------------------------------------------------ boot / exit -----
    def sweep_leftovers(self):
        """Boot: stop cloudflared processes carrying CALVOUN_TUNNEL that no
        live Manager row owns and that belong to THIS hub (same state dir stamp,
        or no stamp). Any other process -- another hub's tunnels included -- is
        never touched. [{pid, name}]"""
        try:
            procs = list(self._procs_fn() if self._procs_fn else _iter_processes())
        except Exception:                                        # noqa: BLE001
            return []
        with self._lock:
            owned = {t.popen.pid for t in self._tunnels.values() if t.popen is not None}
        try:                # ... and everything those processes started
            import psutil
            for pid in list(owned):
                owned.update(c.pid for c in psutil.Process(pid).children(recursive=True))
        except Exception:                                        # noqa: BLE001
            pass
        skip = owned | _hub_pids() | {os.getpid()}

        home = self._home()

        def ours(p):
            try:
                env = p.environ()
                if not env.get(MARKER):
                    return False
                stamp = env.get(MARKER_HOME)
                return not (stamp and home and stamp != home)   # another hub's tunnel
            except Exception:                                    # noqa: BLE001
                return False
        victims, stopped = {}, []
        for p in procs:
            try:
                pid = p.pid
                if pid in skip or not ours(p):
                    continue
                if not self._proc_match(p.name(), p.cmdline()):
                    continue
                kids = []
                try:
                    kids = [k for k in p.children(recursive=True) if k.pid not in skip and ours(k)]
                except Exception:                                # noqa: BLE001
                    pass
                for q in kids + [p]:
                    victims[q.pid] = q
                stopped.append({"pid": pid, "name": p.name()})
            except Exception:                                    # noqa: BLE001
                continue
        for q in victims.values():
            try:
                q.terminate()
            except Exception:                                    # noqa: BLE001
                pass
        deadline = time.monotonic() + 3.0
        while victims and time.monotonic() < deadline:
            time.sleep(0.05)
            alive = []
            for q in victims.values():
                try:
                    if q.is_running():
                        alive.append(q)
                except Exception:                                # noqa: BLE001
                    pass
            if not alive:
                break
        for q in victims.values():
            try:
                if q.is_running():
                    q.kill()
            except Exception:                                    # noqa: BLE001
                pass
        if stopped:
            _log.info("[publish] stopped %d leftover tunnel process(es)", len(stopped))
        return stopped

    def shutdown(self):
        """Hub exit: every live tunnel is killed, the timer thread stops."""
        with self._lock:
            victims = []
            for t in self._tunnels.values():
                if t.active():
                    self._finish(t, "stopped", "The hub stopped.")
                    victims.append(t.popen)
                    t.popen = None
            timer = self._timer
        self._stop_evt.set()
        for popen in victims:
            self._kill(popen)
        if timer is not None and timer is not threading.current_thread():
            timer.join(timeout=2)
        with self._lock:
            if self._timer is timer and (timer is None or not timer.is_alive()):
                self._timer = None

    # ------------------------------------------------------------ install --
    def install(self, on_done=None):
        """Download the official cloudflared in the background: the Install
        button, or the automatic install (AutoInstaller passes `on_done`, called
        once with the error text or None when the download THIS call started
        ends). Never two at once: an install already running is left alone.
        Progress and failure show in status()["cloudflared"]."""
        self._require_enabled()
        if self._find():
            return self.status()
        with self._lock:
            busy = self._install["running"]
        if busy:
            return self.status()
        system, machine = self._sys()
        asset = _ASSETS.get((system, machine))
        if asset is None:
            raise PublishError("install_failed", _no_download_text(system, machine))
        th = None
        with self._lock:
            if not self._install["running"]:        # re-checked: the button and the timer race
                self._install.update(running=True, error=None, stage="starting",
                                     progress=None, on_done=on_done)
                th = threading.Thread(target=self._install_worker, args=(asset,),
                                      daemon=True, name="publish-install")
                self._install["thread"] = th
        if th is not None:
            th.start()
        return self.status()

    def install_started_by(self, on_done):
        """True when the running (or last) download was started with this exact
        `on_done` -- how the AutoInstaller tells its own attempt from the button's."""
        with self._lock:
            return on_done is not None and self._install.get("on_done") is on_done

    def _stage(self, stage, progress=None):
        with self._lock:
            self._install["stage"] = stage
            self._install["progress"] = progress

    def _install_worker(self, asset):
        error = None
        try:
            self._do_install(asset)
            self._stage("done", 100)
        except PublishError as exc:
            error = exc.message
        except Exception as exc:                                 # noqa: BLE001
            error = "The download failed (%s)." % type(exc).__name__
        with self._lock:
            self._install["running"] = False
            self._install["error"] = error
            if error:
                self._install["stage"] = "failed"
            on_done = self._install.get("on_done")
        if error:
            _log.warning("[publish] cloudflared install failed: %s", scrub(error))
        else:
            _log.info("[publish] cloudflared installed")
        if on_done is not None:                 # outside the lock: it takes its own
            try:
                on_done(error)
            except Exception as exc:                             # noqa: BLE001
                _log.warning("[publish] install callback failed: %s", type(exc).__name__)

    def _download(self, url, sink, max_bytes, hosts, on_progress=None):
        fetch = self._fetch_fn or _https_fetch
        return fetch(url, sink, max_bytes, on_progress, hosts)

    def _do_install(self, asset):
        self._stage("looking up the latest release")
        buf = bytearray()
        self._download(RELEASE_API, buf.extend, MAX_METADATA_BYTES, METADATA_HOSTS)
        try:
            import json
            meta = json.loads(bytes(buf).decode("utf-8"))
            assets = meta.get("assets") or []
        except Exception:                                        # noqa: BLE001
            raise PublishError("install_failed", "The release information could not be read.")
        row = next((a for a in assets if isinstance(a, dict) and a.get("name") == asset), None)
        if row is None:
            raise PublishError("install_failed", "The latest release has no %s." % asset)
        url = row.get("browser_download_url")
        u = _check_url(url, DOWNLOAD_HOSTS)
        if (u.hostname or "").lower() == "github.com" and not u.path.startswith(DOWNLOAD_PATH_PREFIX):
            raise PublishError("install_failed", "Unexpected download address; refused.")
        expected = self._expected_digest(row, meta.get("body"), asset)
        bin_dir = self._bin()
        os.makedirs(bin_dir, exist_ok=True)
        fd, part = tempfile.mkstemp(prefix=".cloudflared-", suffix=".part", dir=bin_dir)
        os.close(fd)
        staged = None
        try:
            h = hashlib.sha256()
            with open(part, "wb") as fh:
                def sink(chunk):
                    h.update(chunk)
                    fh.write(chunk)

                def progress(done, total):
                    self._stage("downloading", int(done * 90 / total) if total else None)
                self._stage("downloading", 0)
                self._download(url, sink, MAX_DOWNLOAD_BYTES, DOWNLOAD_HOSTS, progress)
            self._stage("verifying", 92)
            if h.hexdigest() != expected:
                raise PublishError("install_failed",
                                   "The download does not match Cloudflare's published "
                                   "checksum; it was deleted and nothing was installed.")
            binary = part
            if asset.endswith(".tgz"):
                self._stage("unpacking", 95)
                fd2, staged = tempfile.mkstemp(prefix=".cloudflared-", suffix=".bin", dir=bin_dir)
                os.close(fd2)
                _extract_cloudflared(part, staged)
                binary = staged
            self._stage("installing", 98)
            os.chmod(binary, 0o755)
            final = os.path.join(bin_dir, _exe_name())
            try:
                os.replace(binary, final)
            except OSError as exc:
                raise PublishError("install_failed", "cloudflared could not be put in place "
                                   "(%s). Stop any link that is online and try again."
                                   % (exc.strerror or type(exc).__name__))
            if binary == staged:
                staged = None
            else:
                part = None
            self._write_version(bin_dir, meta.get("tag_name"))
        finally:
            for leftover in (part, staged):
                if leftover and os.path.exists(leftover):
                    try:
                        os.remove(leftover)
                    except OSError:
                        pass

    @staticmethod
    def _expected_digest(row, body, asset):
        listed = None
        d = row.get("digest")
        if isinstance(d, str):
            m = re.fullmatch(r"sha256:([0-9a-fA-F]{64})", d.strip())
            if m:
                listed = m.group(1).lower()
        from_body = checksum_from_body(body, asset)
        if listed and from_body and listed != from_body:
            raise PublishError("install_failed", "The release lists two different checksums; "
                               "nothing was installed.")
        digest = listed or from_body
        if not digest:
            raise PublishError("install_failed", "Cloudflare published no checksum for this "
                               "download, so it cannot be verified; nothing was installed.")
        return digest

    @staticmethod
    def _write_version(bin_dir, tag):
        tag = re.sub(r"[^A-Za-z0-9._-]", "", str(tag or ""))[:40]
        if not tag:
            return
        tmp = os.path.join(bin_dir, ".cloudflared.version.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(tag)
            os.replace(tmp, os.path.join(bin_dir, "cloudflared.version"))
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# cloudflared installs itself (owner request 2026-10-10)
# --------------------------------------------------------------------------- #

AUTO_FLAG = "cloudflared_auto_install"     # config flag, default on
AUTO_DELAY_SECONDS = 60.0                  # boot -> the first automatic look
AUTO_RETRY_SECONDS = 24 * 3600.0           # a failed automatic attempt -> the next one
AUTO_RECHECK_SECONDS = 60.0                # a drain or a Stop is on -> look again then
AUTO_REARM_SECONDS = 5.0                   # the panel's box was just ticked
AUTO_STATE_FILE = "cloudflared-auto.json"  # in state_dir()
_AUTO_RESULTS = frozenset({"installing", "installed", "failed", "unsupported", "interrupted"})
_AUTO_CUT_TWICE = ("The automatic install was cut short twice in a row (the hub stopped "
                   "during the download). Press Install cloudflared to try now.")


def _auto_timer(delay, fn):
    """The real timer: one daemon thread that waits `delay` seconds, then calls
    `fn`. tests/conftest.py makes it fail loudly; tests inject their own."""
    t = threading.Timer(max(0.0, float(delay)), fn)
    t.daemon = True
    t.name = "cloudflared-auto"
    t.start()
    return t


def _auto_flag_on():
    try:
        import config
        return config.get_flag(AUTO_FLAG, True)
    except Exception:                                            # noqa: BLE001
        return False          # the owner's choice cannot be read: download nothing


def _auto_set_flag(value):
    import config
    config.set_flag(AUTO_FLAG, bool(value))


def _auto_state_path():
    import config
    return os.path.join(config.state_dir(), AUTO_STATE_FILE)


def _finite(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


class AutoInstaller:
    """cloudflared installs itself. About a minute after boot, when the flag
    `cloudflared_auto_install` (default on) and `publish_enabled` are on and no
    cloudflared is found, the Manager's OWN `install()` runs in the background:
    the same release lookup, host allowlist and SHA-256 check as the Install
    button. Nothing here downloads, verifies or writes a binary itself.

    - Never at import (the constructor only stores its settings), never on the
      boot thread (`start()` arms one daemon timer and returns), never while
      `blocked()` names a reason (a graceful-update drain, a dashboard Stop):
      it looks again AUTO_RECHECK_SECONDS later instead.
    - At most one automatic attempt per AUTO_RETRY_SECONDS after a failure,
      across restarts: the last attempt and its result live in
      state_dir()/cloudflared-auto.json (temp file + fsync + os.replace). An
      attempt the hub stopped in the middle of is tried once more at the next
      boot; a second one in a row counts as a failure.
    - A platform with no official build is recorded `unsupported` and never
      tried automatically.
    - Never two downloads at once: the Manager's own `installing` guard; a
      download the button started is left alone (and not recorded here).
    Locks: `_lock` (timer + attempt) may be held while calling the Manager
    (auto -> manager, never the reverse); `view()` takes neither it nor any
    Manager lock, so a status read never waits on an install decision; `_io`
    (the state file) is a leaf lock."""

    def __init__(self, manager, *, flag=None, set_flag=None, wall=None, timer=None,
                 path=None, delay=AUTO_DELAY_SECONDS, retry_seconds=AUTO_RETRY_SECONDS,
                 recheck_seconds=AUTO_RECHECK_SECONDS, rearm_seconds=AUTO_REARM_SECONDS):
        self._m = manager
        self._flag_fn = flag
        self._set_flag_fn = set_flag
        self._wall = wall or time.time
        self._timer_fn = timer             # (delay, fn) -> handle with .cancel()
        self._path_fn = path
        self._delay = float(delay)
        self._retry = float(retry_seconds)
        self._recheck = float(recheck_seconds)
        self._rearm = float(rearm_seconds)
        self._lock = threading.RLock()
        self._io = threading.Lock()
        self._blocked = None               # () -> why an install must wait now, or None
        self._started = False              # start() ran: the hub booted
        self._handle = self._gen = self._due = None
        self._mine = None                  # token of the download THIS object started
        self._record = None                # the state file, read once (None = not yet)
        manager.attach_auto(self)

    # ------------------------------------------------------------ settings --
    def enabled(self):
        try:
            return bool(self._flag_fn() if self._flag_fn else _auto_flag_on())
        except Exception:                                        # noqa: BLE001
            return False

    def set_enabled(self, on):
        """The Publish panel's "Install cloudflared automatically" box: save the
        flag, then look again in a few seconds (on) or cancel the pending look
        (off). Downloads nothing in this call; a download already running ends
        by itself."""
        on = bool(on)
        (self._set_flag_fn or _auto_set_flag)(on)
        with self._lock:
            if not on:
                self._cancel()
            elif self._started and self._mine is None:
                self._arm(self._rearm)

    # ------------------------------------------------------ the state file --
    def _path(self):
        return self._path_fn() if self._path_fn else _auto_state_path()

    @staticmethod
    def _clean(rec):
        result = rec.get("result")
        err = rec.get("error")
        plat = rec.get("platform")
        n = rec.get("interrupted")
        return {"v": 1, "result": result if result in _AUTO_RESULTS else None,
                "last_attempt": _finite(rec.get("last_attempt")),
                "updated_at": _finite(rec.get("updated_at")),
                "error": scrub(err)[:400] if isinstance(err, str) and err else None,
                "platform": plat[:40] if isinstance(plat, str) else None,
                "interrupted": min(n, 9) if isinstance(n, int) and not isinstance(n, bool)
                and n > 0 else 0}

    def _load(self):
        """The last automatic attempt, read once. An attempt the previous hub
        stopped in the middle of reads `interrupted`; a second one in a row,
        `failed`."""
        rec = self._record
        if rec is not None:
            return rec
        with self._io:
            if self._record is not None:
                return self._record
            try:
                with open(self._path(), encoding="utf-8") as fh:
                    got = json.load(fh)
            except Exception:                                    # noqa: BLE001
                got = {}
            rec = self._clean(got if isinstance(got, dict) else {})
            if rec["result"] == "installing":
                n = rec["interrupted"] + 1
                if n >= 2:
                    rec.update(result="failed", error=_AUTO_CUT_TWICE, interrupted=0)
                else:
                    rec.update(result="interrupted", error=None, interrupted=n)
            self._record = rec
            return rec

    def _save(self, **changes):
        rec = dict(self._load())
        rec.update(changes)
        rec["updated_at"] = self._wall()
        rec = self._clean(rec)
        with self._io:
            self._record = rec
            tmp = None
            try:
                path = os.path.abspath(self._path())
                folder = os.path.dirname(path)
                os.makedirs(folder, exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix=".cloudflared-auto-", suffix=".tmp", dir=folder)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(rec, fh)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
                tmp = None
            except Exception as exc:                             # noqa: BLE001
                _log.warning("[publish] could not save %s: %s", AUTO_STATE_FILE,
                             type(exc).__name__)
            finally:
                if tmp is not None:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
        return rec

    # ----------------------------------------------------------- the timer --
    def start(self, blocked=None):
        """Boot (app.py, next to the tunnel sweep): arm ONE look
        AUTO_DELAY_SECONDS from now and return. `blocked()` names why an install
        must wait (a graceful-update drain, a dashboard Stop). With the flag off
        nothing is armed: the Install button stays the only way, as before."""
        with self._lock:
            if blocked is not None:
                self._blocked = blocked
            self._started = True
            if self.enabled():
                self._arm(self._delay)

    def stop(self):
        """Hub exit: the pending look is cancelled."""
        with self._lock:
            self._started = False
            self._cancel()

    def _arm(self, delay):
        self._cancel()
        delay = max(0.0, float(delay))
        gen = object()
        self._gen = gen
        try:
            handle = (self._timer_fn or _auto_timer)(delay, lambda: self._fire(gen))
        except BaseException:
            self._gen = None
            raise
        self._handle, self._due = handle, self._wall() + delay

    def _cancel(self):
        handle = self._handle
        self._handle = self._gen = self._due = None
        if handle is not None:
            try:
                handle.cancel()
            except Exception:                                    # noqa: BLE001
                pass

    def _fire(self, gen):
        with self._lock:
            if gen is not self._gen:
                return                      # cancelled or re-armed since
            self._handle = self._gen = self._due = None
        try:
            self.run_once()
        except Exception as exc:                                 # noqa: BLE001
            _log.warning("[publish] automatic cloudflared install: %s", type(exc).__name__)

    # -------------------------------------------------------- one decision --
    def _why_blocked(self):
        fn = self._blocked
        if fn is None:
            return None
        try:
            why = fn()
        except Exception:                                        # noqa: BLE001
            return "unknown"                # cannot tell: wait, never guess
        return str(why) if why else None

    def _unsupported(self, plat, message):
        rec = self._load()
        if rec["result"] != "unsupported" or rec["platform"] != plat:
            self._save(result="unsupported", platform=plat, error=message)
            _log.info("[publish] no official cloudflared build for %s; it is not "
                      "installed automatically", plat)
        return "unsupported"

    def run_once(self):
        """Install now, look again later, or do nothing. Returns why, in one
        word: off, disabled, available, unsupported, the blocked() reason,
        waiting, busy or started (else an engine refusal code)."""
        with self._lock:
            if self._mine is not None:
                return "busy"
            if not self.enabled():
                return "off"
            if not self._m._enabled():
                return "disabled"
            if self._m._find():
                return "available"
            system, machine = self._m._sys()
            plat = "%s/%s" % (system, machine)
            if _ASSETS.get((system, machine)) is None:
                return self._unsupported(plat, _no_download_text(system, machine))
            why = self._why_blocked()
            if why:
                self._arm(self._recheck)
                return why
            rec = self._load()
            now = self._wall()
            if rec["result"] == "failed" and rec["last_attempt"] is not None:
                wait = min(rec["last_attempt"] + self._retry - now, self._retry)
                if wait > 0:
                    self._arm(wait)
                    return "waiting"
            token = object()

            def done(error, _token=token):
                self._done(_token, error)
            try:
                self._m.install(on_done=done)
            except PublishError as exc:
                if exc.code == "install_failed":       # no official build after all
                    return self._unsupported(plat, exc.message)
                return exc.code
            if not self._m.install_started_by(done):
                return "busy"               # the button's download is running: left alone
            # Recorded before the worker's done() can run: it waits for _lock.
            self._mine = token
            self._save(result="installing", last_attempt=now, error=None, platform=plat,
                       interrupted=rec["interrupted"] if rec["result"] == "interrupted" else 0)
        _log.info("[publish] installing cloudflared automatically (the official release, "
                  "SHA-256 checked); the %s flag turns this off", AUTO_FLAG)
        return "started"

    def _done(self, token, error):
        """The download THIS object started ended (called on the engine's
        worker thread, outside the Manager's lock)."""
        with self._lock:
            if token is not self._mine:
                return
            self._mine = None
            if error:
                self._save(result="failed", error=str(error), interrupted=0)
                _log.info("[publish] the automatic cloudflared install failed; the next "
                          "automatic try is in %d h (Install cloudflared works any time)",
                          int(self._retry // 3600))
                if self._started and self.enabled():
                    self._arm(self._retry)
            else:
                self._save(result="installed", error=None, interrupted=0)

    # ---------------------------------------------------------- the status --
    def view(self, available, supported, enabled, install_error=None):
        """The auto_* fields of GET /api/publish -> cloudflared."""
        on = self.enabled()
        rec = self._load()
        due = self._due
        if not (on and enabled):
            state = "off"
        elif self._mine is not None:
            state = "installing"
        elif available:
            state = "installed" if rec["result"] == "installed" else "idle"
        elif not supported:
            state = "unsupported"
        elif rec["result"] == "failed":
            state = "failed"
        elif due is not None:
            state = "scheduled"
        else:
            state = "idle"
        error = None
        if state == "failed":
            error = rec["error"]
        elif state == "unsupported":
            error = rec["error"] or install_error
        nxt = None
        if due is not None and state in ("scheduled", "failed"):
            nxt = due
            if state == "failed" and rec["last_attempt"] is not None:
                nxt = max(due, rec["last_attempt"] + self._retry)
        return {"auto_install": on, "auto_state": state,
                "auto_last_attempt": rec["last_attempt"], "auto_error": error,
                "auto_next_attempt": nxt}


default = Manager()
auto = AutoInstaller(default)      # armed only by app.py's boot (start()), never here


def _shutdown_default():
    for fn in (auto.stop, default.shutdown):
        try:
            fn()
        except Exception:                                        # noqa: BLE001
            pass


atexit.register(_shutdown_default)
