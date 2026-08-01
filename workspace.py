"""Calvoun Free LLM Hub — run a generated project locally and watch it fail.

WHAT THIS IS FOR
----------------
The agent chat can already drive a CLI inside a project directory. What it could
not do is CLOSE THE LOOP: you asked for a site, the CLI wrote files, and nobody
ever ran them. The SHIP brief tells the model to run what it builds, but a model
running a server in its own sandbox is not the same as the user SEEING it.

So: start the project on its own port, show it in an iframe next to the chat, and
turn whatever it prints when it breaks into a clickable problem that becomes the
next prompt.

ISOLATION, HONESTLY
-------------------
Each project gets its OWN dependencies — a .venv for Python, the local
node_modules npm already creates — and its own port. That is DEPENDENCY
isolation: two projects can want different versions of the same package and
neither breaks the other.

It is NOT security isolation. The process runs as you, on your machine, with your
files. Anything the project's own start script does, it can do. Docker would give
the stronger guarantee and is the natural upgrade; this deliberately starts with
the version that needs nothing installed and starts in seconds.

NOTHING HERE RUNS A COMMAND THE USER TYPED
------------------------------------------
The start command is DERIVED from what is in the project directory (a package.json
script, a known framework entry point, a static index.html). There is no field
anywhere that feeds a string to a shell. That is the whole reason `detect()`
returns a fixed argv list and never a shell string.
"""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time

# Ports we hand out to previews. Above the usual dev-server range so a project
# that hardcodes 3000/5173 does not collide with one we assigned.
PORT_RANGE = (5800, 5899)
START_TIMEOUT = 90.0        # seconds to wait for the port to answer
LOG_LINES = 400             # per project ring buffer
MAX_PROBLEMS = 20
# Stop a preview nobody is watching. THIS is the process that actually costs
# something while idle — a dev server holds a port, a node/python process and
# often a file watcher, indefinitely. (The agent CLI does not: agentic_chat
# spawns exactly one subprocess per message and clears it when the turn ends,
# so between turns there is nothing running to close.)
# Reaped, not killed forever: pressing Run starts it again in seconds.
IDLE_TIMEOUT = 30 * 60      # seconds without anyone looking
_REAP_EVERY = 60.0

_procs = {}                 # project_dir -> _Proc
_lock = threading.RLock()


class WorkspaceError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

def _npm(name):
    """npm/npx are .cmd shims on Windows; argv must name the real file or
    CreateProcess raises FileNotFoundError."""
    return name + ".cmd" if os.name == "nt" else name


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:                                            # noqa: BLE001
        return None


def detect(project_dir):
    """How to start what is in `project_dir`: {"argv", "kind", "needs_install"}.

    Ordered the same way the SHIP brief tells models to do it — the project's own
    script first, because that is the one the author actually maintains, and a
    plain static server last, because it always works."""
    if not os.path.isdir(project_dir):
        raise WorkspaceError("not a directory: %s" % project_dir)

    pkg = _read_json(os.path.join(project_dir, "package.json"))
    if isinstance(pkg, dict):
        scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
        for script in ("dev", "start", "serve", "preview"):
            if scripts.get(script):
                return {"argv": [_npm("npm"), "run", script],
                        "kind": "npm:" + script,
                        "needs_install": not os.path.isdir(
                            os.path.join(project_dir, "node_modules"))}
        # A package.json with no runnable script is still a node project; vite
        # can serve it, and npx will fetch vite on demand.
        return {"argv": [_npm("npx"), "--yes", "vite", "--host", "127.0.0.1"],
                "kind": "vite", "needs_install": False}

    for entry in ("app.py", "main.py", "server.py", "manage.py"):
        if os.path.isfile(os.path.join(project_dir, entry)):
            return {"argv": [_venv_python(project_dir), entry],
                    "kind": "python:" + entry,
                    "needs_install": os.path.isfile(
                        os.path.join(project_dir, "requirements.txt"))}

    if os.path.isfile(os.path.join(project_dir, "index.html")):
        return {"argv": [sys.executable, "-m", "http.server", "--bind", "127.0.0.1"],
                "kind": "static", "needs_install": False}

    raise WorkspaceError(
        "nothing runnable in this folder — no package.json, no app.py/main.py, "
        "no index.html")


# --------------------------------------------------------------------------- #
# Per-project dependency isolation
# --------------------------------------------------------------------------- #

def _venv_dir(project_dir):
    return os.path.join(project_dir, ".venv")


def _venv_python(project_dir):
    """The project's OWN interpreter if it has one, else the hub's.

    Returning the hub's interpreter as the fallback is deliberate: a project with
    no dependencies should still start instantly instead of paying for a venv it
    does not need."""
    vd = _venv_dir(project_dir)
    exe = (os.path.join(vd, "Scripts", "python.exe") if os.name == "nt"
           else os.path.join(vd, "bin", "python"))
    return exe if os.path.isfile(exe) else sys.executable


def install(project_dir, log):
    """Create the project's own dependency environment. Blocking; the caller runs
    it on a worker thread. `log(line)` receives progress."""
    pkg = os.path.join(project_dir, "package.json")
    req = os.path.join(project_dir, "requirements.txt")
    if os.path.isfile(pkg) and not os.path.isdir(os.path.join(project_dir, "node_modules")):
        log("[hub] npm install (own node_modules for this project)")
        _run_blocking([_npm("npm"), "install"], project_dir, log)
    if os.path.isfile(req):
        vd = _venv_dir(project_dir)
        if not os.path.isdir(vd):
            log("[hub] creating .venv (own python deps for this project)")
            _run_blocking([sys.executable, "-m", "venv", vd], project_dir, log)
        log("[hub] pip install -r requirements.txt")
        _run_blocking([_venv_python(project_dir), "-m", "pip", "install",
                       "-q", "-r", "requirements.txt"], project_dir, log)


def _run_blocking(argv, cwd, log):
    try:
        p = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace")
    except (OSError, ValueError) as exc:
        log("[hub] could not run %s: %s" % (argv[0], exc))
        return
    for line in p.stdout:
        log(line.rstrip())
    p.wait()


# --------------------------------------------------------------------------- #
# Problems — what the user clicks on
# --------------------------------------------------------------------------- #

# Ordered most-specific first: the first pattern that matches a line wins, so a
# webpack/vite error is not also reported as a bare "error:".
_PROBLEM_PATTERNS = [
    ("build", re.compile(
        r"^\s*(?:ERROR|Error|error)\s+in\s+(.+)$")),
    ("syntax", re.compile(
        r"(SyntaxError|ParseError|Unexpected token|Unterminated string)[:\s].*", re.I)),
    ("module", re.compile(
        r"((?:Cannot find module|Module not found|ModuleNotFoundError|"
        r"ImportError|Failed to resolve import)[^\n]*)", re.I)),
    ("exception", re.compile(
        r"^\s*([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception))\s*:\s*(.+)$")),
    ("address", re.compile(
        r"((?:EADDRINUSE|address already in use)[^\n]*)", re.I)),
    ("generic", re.compile(
        r"^\s*(?:\[[^\]]+\]\s*)?(?:ERR!|error|ERROR)\s*[:\s]\s*(.{6,})$")),
]

# Lines that LOOK like errors but are normal dev-server chatter. Reporting these
# trains the user to ignore the panel, which defeats the point of having one.
_PROBLEM_NOISE = re.compile(
    r"0 error|no error|error-free|--error|errorformat|"
    r"npm warn|deprecated|browserslist|found 0 vulnerabilities", re.I)


def problems_from(lines):
    """Clickable problems parsed from process output, newest last, deduped."""
    out, seen = [], set()
    for raw in lines:
        line = (raw or "").strip()
        if not line or len(line) > 600 or _PROBLEM_NOISE.search(line):
            continue
        for kind, rx in _PROBLEM_PATTERNS:
            m = rx.search(line)
            if not m:
                continue
            text = " ".join(g for g in m.groups() if g).strip() or line
            key = (kind, text[:160])
            if key not in seen:
                seen.add(key)
                out.append({"kind": kind, "text": text[:400], "line": line[:600]})
            break
    return out[-MAX_PROBLEMS:]


# --------------------------------------------------------------------------- #
# Process lifecycle
# --------------------------------------------------------------------------- #

def _free_port():
    """A port free on the OS AND not already handed to another project.

    The second half is not optional. Probing with a throwaway bind and closing it
    is inherently TOCTOU: start() returns immediately and the real server binds
    later, on a worker thread, so two projects started back-to-back both saw
    5800 as free and the second one died with EADDRINUSE. Reserving against
    _procs closes the window between handing a port out and it being listened
    on."""
    with _lock:
        taken = {p.port for p in _procs.values()}
    for port in range(*PORT_RANGE):
        if port in taken:
            continue
        # Anything already answering means the port is in use, full stop. This
        # check has to come FIRST because the bind probe below cannot be trusted
        # on its own: on Windows SO_REUSEADDR behaves like SO_REUSEPORT and
        # happily binds a port another process is ALREADY listening on. With it
        # set, the probe called 5800 free while an orphaned server held it, the
        # real server failed to bind, _port_open saw the ORPHAN answering and
        # reported "ready" — and the preview served the wrong project.
        if _port_open(port):
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise WorkspaceError("no free port in %d-%d" % PORT_RANGE)


def _port_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


class _Proc:
    def __init__(self, project_dir, port, kind):
        self.project_dir = project_dir
        self.port = port
        self.kind = kind
        self.popen = None
        self.lines = []
        self.state = "installing"
        self.error = None
        self.started_at = time.time()
        self.touched_at = time.time()   # last time anyone asked about it
        self._lock = threading.Lock()

    def log(self, line):
        with self._lock:
            self.lines.append(line)
            if len(self.lines) > LOG_LINES:
                del self.lines[:len(self.lines) - LOG_LINES]

    def tail(self, n=80):
        with self._lock:
            return list(self.lines[-n:])

    def all_lines(self):
        with self._lock:
            return list(self.lines)


def _env_for(project_dir, port):
    env = dict(os.environ)
    # Every common dev server reads one of these. Setting all of them is how one
    # code path serves vite, next, CRA, flask and http.server without knowing
    # which one it is about to start.
    env["PORT"] = str(port)
    env["VITE_PORT"] = str(port)
    env["FLASK_RUN_PORT"] = str(port)
    env["HOST"] = "127.0.0.1"
    env["BROWSER"] = "none"            # do not steal focus with a browser tab
    env["CI"] = "1"                    # CRA/vite: no interactive prompts
    env["FORCE_COLOR"] = "0"           # ANSI codes would land in the problems panel
    env["NO_COLOR"] = "1"
    # A project's own venv must win over the hub's interpreter for subprocesses.
    vd = _venv_dir(project_dir)
    bindir = os.path.join(vd, "Scripts" if os.name == "nt" else "bin")
    if os.path.isdir(bindir):
        env["VIRTUAL_ENV"] = vd
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    return env


def _argv_with_port(argv, kind, port):
    """Ports that must be on the command line rather than in the environment."""
    if kind == "static":
        return argv + [str(port)]
    if kind == "vite" or kind.startswith("npm:"):
        return argv + ["--", "--port", str(port)] if kind.startswith("npm:") \
            else argv + ["--port", str(port)]
    return argv


def start(project_dir, on_done=None):
    """Install deps, launch the project, and wait for its port. Returns the
    status dict immediately; the work happens on a worker thread."""
    project_dir = os.path.abspath(project_dir)
    spec = detect(project_dir)
    stop(project_dir)
    port = _free_port()
    proc = _Proc(project_dir, port, spec["kind"])
    with _lock:
        _procs[project_dir] = proc
    proc.log("[hub] %s on port %d" % (spec["kind"], port))

    def worker():
        try:
            if spec["needs_install"]:
                install(project_dir, proc.log)
            proc.state = "starting"
            argv = _argv_with_port(spec["argv"], spec["kind"], port)
            proc.log("[hub] " + " ".join(argv))
            try:
                proc.popen = subprocess.Popen(
                    argv, cwd=project_dir, env=_env_for(project_dir, port),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    # Own group so stop() takes the whole tree: npm spawns the
                    # real server as a CHILD, and killing npm alone orphans it
                    # holding the port.
                    creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP
                                   if os.name == "nt" else 0),
                    start_new_session=(os.name != "nt"))
            except (OSError, ValueError) as exc:
                proc.state = "failed"
                proc.error = "could not start %s: %s" % (spec["argv"][0], exc)
                proc.log("[hub] " + proc.error)
                return
            threading.Thread(target=_pump, args=(proc,), daemon=True).start()
            deadline = time.time() + START_TIMEOUT
            while time.time() < deadline:
                if _port_open(port):
                    proc.state = "running"
                    proc.log("[hub] ready on http://127.0.0.1:%d" % port)
                    return
                if proc.popen.poll() is not None:
                    proc.state = "failed"
                    proc.error = "the project exited before it served anything"
                    return
                time.sleep(0.35)
            proc.state = "failed"
            proc.error = "no response on port %d after %ds" % (port, int(START_TIMEOUT))
        finally:
            if on_done:
                try:
                    on_done(proc)
                except Exception:                                # noqa: BLE001
                    pass

    threading.Thread(target=worker, daemon=True).start()
    return status(project_dir)


def _pump(proc):
    try:
        for line in proc.popen.stdout:
            proc.log(line.rstrip())
    except Exception:                                            # noqa: BLE001
        pass
    finally:
        if proc.state == "running" and proc.popen.poll() is not None:
            proc.state = "failed"
            proc.error = "the project stopped on its own"


def stop(project_dir):
    project_dir = os.path.abspath(project_dir)
    with _lock:
        proc = _procs.pop(project_dir, None)
    if not proc or not proc.popen:
        return False
    try:
        if os.name == "nt":
            # taskkill /T is what actually takes npm's grandchildren on Windows;
            # terminate() alone leaves the real server holding the port.
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.popen.pid)],
                           capture_output=True, timeout=15)
        else:
            os.killpg(os.getpgid(proc.popen.pid), 15)
    except Exception:                                            # noqa: BLE001
        try:
            proc.popen.kill()
        except Exception:                                        # noqa: BLE001
            pass
    proc.state = "stopped"
    return True


def stop_all():
    for d in list(_procs):
        stop(d)


ATTACH_DIR = ".hub-attachments"
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def save_attachment(project_dir, raw, ext, name=None):
    """Write a pasted/dropped image INTO the project and return its relative path.

    Why a file and not an inline image: the agent chat drives a real CLI, and
    Claude Code / Codex read images by PATH from the working directory. Handing
    them a base64 blob in the prompt text would just waste the context window on
    something they cannot decode. A path they can open is the thing that works.

    The filename is fully rewritten from a counter, never taken from the upload,
    so a name like `../../.ssh/id_rsa` cannot escape the folder. The result is
    re-checked against the project root anyway — belt and braces, because this
    writes to the user's disk."""
    project_dir = os.path.abspath(project_dir)
    out_dir = os.path.join(project_dir, ATTACH_DIR)
    os.makedirs(out_dir, exist_ok=True)
    ext = _SAFE_NAME_RE.sub("", (ext or "png").lower())[:5] or "png"
    stem = _SAFE_NAME_RE.sub("-", os.path.splitext(name or "")[0])[:40].strip("-")
    n = 1
    while True:
        fname = "%s-%d.%s" % (stem or "shot", n, ext)
        path = os.path.join(out_dir, fname)
        if not os.path.exists(path):
            break
        n += 1
    if os.path.commonpath([os.path.abspath(path), project_dir]) != project_dir:
        raise WorkspaceError("refusing to write outside the project")
    with open(path, "wb") as fh:
        fh.write(raw)
    return os.path.join(ATTACH_DIR, fname).replace("\\", "/")


def list_dirs(path=None):
    """Sub-directories of `path` (default: home), for the folder picker.

    A browser CANNOT give an absolute local path — neither a drop nor a file
    input exposes one, by design. Since the hub runs on the same machine, the
    honest way to let someone "browse for a folder" is to list directories
    server-side and let them click down.

    Read-only, never recursive, hidden and system folders skipped, and capped —
    node_modules alone can hold thousands of entries and would make the picker
    useless as well as slow."""
    root = os.path.abspath(os.path.expanduser(path or "~"))
    if not os.path.isdir(root):
        raise WorkspaceError("not a directory: %s" % root)
    out = []
    try:
        with os.scandir(root) as it:
            for e in it:
                if len(out) >= 500:
                    break
                name = e.name
                if name.startswith(".") or name.startswith("$"):
                    continue
                try:
                    if not e.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                out.append({"name": name, "path": os.path.join(root, name)})
    except PermissionError:
        raise WorkspaceError("permission denied: %s" % root)
    out.sort(key=lambda d: d["name"].lower())
    parent = os.path.dirname(root)
    return {"path": root,
            "parent": parent if parent and parent != root else None,
            "dirs": out,
            "runnable": _is_runnable(root)}


def _is_runnable(path):
    """Whether this folder already holds something the preview could start —
    shown in the picker so an empty folder is an informed choice, not a
    surprise."""
    try:
        detect(path)
        return True
    except Exception:                                            # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# Reading the project — the file tree and the code viewer
# --------------------------------------------------------------------------- #

MAX_FILE_BYTES = 512 * 1024      # a file bigger than this is not for reading
MAX_TREE_ENTRIES = 800

# Never worth showing, and node_modules/.git would drown everything else.
_TREE_SKIP = {"node_modules", ".git", ".venv", "venv", "__pycache__", ".next",
              "dist", "build", ".cache", ".idea", ".pytest_cache", ".mypy_cache",
              ATTACH_DIR}


def _inside(root, target):
    """True when `target` really is inside `root`.

    commonpath on the ABSOLUTE, symlink-resolved paths — a plain startswith is
    defeated by '..' and by a symlink pointing out of the project, and this
    decides which of the user's files a browser can read."""
    try:
        root = os.path.realpath(root)
        target = os.path.realpath(target)
        return os.path.commonpath([root, target]) == root
    except (ValueError, OSError):
        return False          # different drives on Windows -> ValueError


def _resolve_in(project_dir, rel):
    root = os.path.abspath(project_dir)
    target = os.path.abspath(os.path.join(root, rel or ""))
    if not _inside(root, target):
        raise WorkspaceError("path is outside the project")
    return root, target


def tree(project_dir, rel=None):
    """One level of the project: directories first, then files."""
    root, target = _resolve_in(project_dir, rel)
    if not os.path.isdir(target):
        raise WorkspaceError("not a directory")
    dirs, files = [], []
    try:
        with os.scandir(target) as it:
            for e in it:
                if len(dirs) + len(files) >= MAX_TREE_ENTRIES:
                    break
                if e.name in _TREE_SKIP or e.name.startswith("."):
                    continue
                r = os.path.relpath(os.path.join(target, e.name), root).replace("\\", "/")
                try:
                    if e.is_dir(follow_symlinks=False):
                        dirs.append({"name": e.name, "rel": r, "dir": True})
                    else:
                        files.append({"name": e.name, "rel": r, "dir": False,
                                      "size": e.stat().st_size})
                except OSError:
                    continue
    except PermissionError:
        raise WorkspaceError("permission denied")
    dirs.sort(key=lambda d: d["name"].lower())
    files.sort(key=lambda d: d["name"].lower())
    return {"rel": os.path.relpath(target, root).replace("\\", "/") if target != root else "",
            "entries": dirs + files}


def read_file(project_dir, rel):
    """A text file's contents, for the viewer. Binary and oversized files are
    reported as such rather than dumped into the browser."""
    _root, target = _resolve_in(project_dir, rel)
    if not os.path.isfile(target):
        raise WorkspaceError("not a file")
    size = os.path.getsize(target)
    if size > MAX_FILE_BYTES:
        return {"rel": rel, "size": size, "too_big": True, "text": None}
    with open(target, "rb") as fh:
        raw = fh.read()
    # bytes([0]), not a backslash-x-0-0 literal. Writing this file through
    # tooling turned that escape into the RAW byte once, which made the module
    # unimportable ("source code string cannot contain null bytes"). Spelling
    # it as an int is immune to however many quoting layers an edit passes
    # through.
    if bytes([0]) in raw[:8000]:
        return {"rel": rel, "size": size, "binary": True, "text": None}
    return {"rel": rel, "size": size,
            "text": raw.decode("utf-8", "replace"),
            "lang": os.path.splitext(target)[1].lstrip(".").lower()}


def running():
    """Every live preview, for the "what is running" list."""
    now = time.time()
    with _lock:
        items = list(_procs.items())
    return [{"project_dir": d, "port": p.port, "kind": p.kind, "state": p.state,
             "url": "http://127.0.0.1:%d" % p.port if p.state == "running" else None,
             "uptime": int(now - p.started_at),
             "idle": int(now - p.touched_at),
             "idle_stops_in": max(0, int(IDLE_TIMEOUT - (now - p.touched_at)))}
            for d, p in items]


def reap_idle(now=None):
    """Stop previews nobody has looked at for IDLE_TIMEOUT. Returns what it
    stopped.

    "Looked at" means a status() call, which the dashboard makes every couple of
    seconds while the preview pane is open — so this only ever fires once the
    user has genuinely moved on."""
    now = now or time.time()
    with _lock:
        stale = [d for d, p in _procs.items() if now - p.touched_at > IDLE_TIMEOUT]
    for d in stale:
        stop(d)
    return stale


def _reaper():
    while True:
        time.sleep(_REAP_EVERY)
        try:
            reap_idle()
        except Exception:                                        # noqa: BLE001
            pass


_reaper_thread = None


def start_reaper():
    """Idempotent — app.py calls this once at boot."""
    global _reaper_thread
    if _reaper_thread is not None:
        return
    _reaper_thread = threading.Thread(target=_reaper, daemon=True)
    _reaper_thread.start()


def status(project_dir):
    project_dir = os.path.abspath(project_dir)
    with _lock:
        proc = _procs.get(project_dir)
        if proc:
            proc.touched_at = time.time()   # someone is watching
    if not proc:
        return {"running": False, "state": "idle", "url": None, "port": None,
                "kind": None, "log": [], "problems": [], "error": None}
    return {
        "running": proc.state == "running",
        "state": proc.state,
        "url": "http://127.0.0.1:%d" % proc.port if proc.state == "running" else None,
        "port": proc.port,
        "kind": proc.kind,
        "log": proc.tail(),
        "problems": problems_from(proc.all_lines()),
        "error": proc.error,
        "uptime": int(time.time() - proc.started_at),
    }
