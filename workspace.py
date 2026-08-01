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
import shutil
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
# What this project needs installed, and where to get it
# --------------------------------------------------------------------------- #

# A beginner asked to install "Node" has to know what that is and which of the
# five buttons on the page to press. Naming the tool without a link is only
# half an answer, so every entry carries one.
TOOL_HINTS = {
    "node": ("Node.js", "https://nodejs.org/en/download",
             "runs JavaScript projects — Next.js, React, Vite, Express"),
    "npm":  ("npm (comes with Node.js)", "https://nodejs.org/en/download",
             "installs this project's packages"),
    "git":  ("Git", "https://git-scm.com/downloads",
             "clones repositories and tracks changes"),
    "python": ("Python", "https://www.python.org/downloads/",
               "runs Python projects"),
}


def missing_tools(project_dir):
    """Which tools this project needs that this machine does not have.

    Returns [{tool, name, url, why, needed_for}] -- empty when nothing is
    missing. Read-only: looks at the project's own files and at PATH, runs
    nothing, installs nothing.

    The point is the message, not the check: a project that needs Node on a
    machine without it fails with something like "[WinError 2] The system
    cannot find the file specified", which tells a beginner nothing at all."""
    project_dir = os.path.abspath(project_dir or "")
    need = []                                   # [(tool, needed_for)]

    pkg = _read_json(os.path.join(project_dir, "package.json"))
    if isinstance(pkg, dict):
        deps = {}
        for key in ("dependencies", "devDependencies"):
            if isinstance(pkg.get(key), dict):
                deps.update(pkg[key])
        # Name the framework, not just "a JavaScript project" -- someone who
        # typed "build me a Next.js site" recognises Next.js.
        for dep, label in (("next", "Next.js"), ("nuxt", "Nuxt"),
                           ("vite", "Vite"), ("react", "React"),
                           ("@angular/core", "Angular"), ("svelte", "Svelte")):
            if dep in deps:
                need.append(("node", label))
                break
        else:
            need.append(("node", "this project"))
        need.append(("npm", "installing its packages"))

    if os.path.isdir(os.path.join(project_dir, ".git")):
        need.append(("git", "this project's version history"))

    for entry in ("requirements.txt", "app.py", "main.py", "manage.py"):
        if os.path.isfile(os.path.join(project_dir, entry)):
            need.append(("python", "this project"))
            break

    out, seen = [], set()
    for tool, needed_for in need:
        if tool in seen or tool not in TOOL_HINTS:
            continue
        seen.add(tool)
        if _tool_present(tool):
            continue
        # npm ships INSIDE Node, so listing both sends someone to the same page
        # twice and reads like two separate problems.
        if tool == "npm" and any(m["tool"] == "node" for m in out):
            continue
        name, url, why = TOOL_HINTS[tool]
        out.append({"tool": tool, "name": name, "url": url,
                    "why": why, "needed_for": needed_for})
    return out


def _tool_present(tool):
    if tool == "python":
        # We ARE Python; a project's own venv is created from this interpreter.
        return True
    if tool == "npm":
        return bool(shutil.which("npm") or shutil.which("npm.cmd"))
    return bool(shutil.which(tool))


def missing_tools_message(project_dir):
    """The same thing as one plain-language block, or None.

    Printed into the conversation, so it is written to be read by someone who
    has never installed a toolchain: what is missing, what it is for, and the
    exact page to download it from."""
    missing = missing_tools(project_dir)
    if not missing:
        return None
    lines = ["This project needs something that is not installed on this computer yet:"]
    for m in missing:
        lines.append("  • %s — %s (needed for %s)" % (m["name"], m["why"], m["needed_for"]))
        lines.append("    Download: %s" % m["url"])
    lines.append("Install it, then reopen this project — nothing else here changes.")
    return "\n".join(lines)


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
    if not proc:
        # Nothing of ours — but there may be an ADOPTED server showing. Stop
        # displaying it rather than killing it: we did not start it, so its
        # lifetime belongs to whoever did (the agent, or the user's terminal).
        # Killing a process we do not own on a button labelled "Stop preview"
        # would be a much bigger action than the label promises.
        return forget(project_dir)
    if not proc.popen:
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


# --------------------------------------------------------------------------- #
# ADOPTING a server the AGENT started
#
# The whole point of this pane is "done means you SAW it run", and the SHIP brief
# tells the agent to actually start what it builds. So the common case is that by
# the time the user looks, the agent has ALREADY run `npm run dev` itself and the
# site is live on :3000 — while this module, which only knew about servers it
# spawned, reported "not running" and offered a Run button that would start a
# SECOND copy on a different port.
#
# Adopting means: point the preview at the server that already exists.
# --------------------------------------------------------------------------- #

# Ports a dev server actually lands on. Deliberately excludes PORT_RANGE (ours)
# and the hub's own port — adopting either would make the preview show the hub.
_DEV_PORTS = (3000, 3001, 3002, 5173, 5174, 4321, 4200, 8080, 8000, 8081,
              1420, 5000, 9000, 7777)

_adopted = {}      # project_dir -> {"url", "port", "since", "touched_at", "source"}


def _hub_port():
    try:
        return int(os.environ.get("PORT") or 8787)
    except (TypeError, ValueError):
        return 8787


def _http_ok(port, timeout=1.2):
    """Does something actually SERVE on this port, as opposed to merely holding
    it open? A socket connect alone would happily adopt a database or an SSH
    tunnel; a real response is the cheap way to tell them apart."""
    import urllib.error
    import urllib.request
    url = "http://127.0.0.1:%d/" % port
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 500
    except urllib.error.HTTPError as e:
        return 200 <= e.code < 500          # 404 still means a server is there
    except Exception:                                            # noqa: BLE001
        return False


def _port_of(url):
    m = re.search(r"^https?://[^/:]+:(\d+)", str(url or ""))
    return int(m.group(1)) if m else None


def adopt(project_dir, url, source="agent"):
    """Point the preview at a server we did NOT start.

    `url` normally comes from the agent's own output ("Local:
    http://localhost:3000/"), which is the authoritative signal — the agent said
    where it put it. Refuses our own port range and the hub's port, and refuses a
    URL nothing is answering on, so a stale line scrolled past in the transcript
    cannot point the pane at nothing."""
    project_dir = os.path.abspath(project_dir)
    port = _port_of(url)
    if not port:
        raise WorkspaceError("no port in %r" % (url,))
    if PORT_RANGE[0] <= port <= PORT_RANGE[1]:
        raise WorkspaceError("that is a port this hub hands out itself")
    if port == _hub_port():
        raise WorkspaceError("that is the hub's own port")
    if not _http_ok(port):
        raise WorkspaceError("nothing is answering on port %d" % port)
    # A port ANOTHER project is already previewing is not ours to take. The
    # agent prints URLs it did not necessarily start -- it repeats a port from
    # an earlier note, or the SHIP brief's example -- and adopting on the word
    # alone showed a finished project the PREVIOUS project's app (reported: a
    # pizza site appearing in an unrelated project's preview).
    with _lock:
        owner = next((d for d, a in _adopted.items()
                      if a["port"] == port and d != project_dir), None)
        if owner is None:
            owner = next((d for d, p in _procs.items()
                          if p.port == port and d != project_dir), None)
    if owner is not None:
        raise WorkspaceError("port %d is already the preview for %s"
                             % (port, os.path.basename(owner)))
    # Then ask the operating system WHO is on that port. A process running in
    # another project's folder is not this project's server, whatever the agent
    # printed. This is what caught the reported case: an Express app with no
    # <title> anywhere in it could not be told apart from the previous
    # project's site by content alone.
    owner_dir = _port_owner_dir(port)
    if owner_dir and not _dir_covers(owner_dir, project_dir):
        raise WorkspaceError("port %d is served from %s, not from this project"
                             % (port, owner_dir))
    # And when we can READ what that port serves, a different project's title
    # is decisive too. Unlike discover(), both checks fail OPEN -- the agent
    # naming a url IS evidence, so silence (an unreadable cwd, a missing title)
    # is not enough to override it; only positive contradiction is.
    if owner_dir is None:
        want = _fingerprint(project_dir)
        if want:
            served = _served_title(port)
            if served and want.strip().lower() != served.strip().lower():
                raise WorkspaceError(
                    "port %d is serving %r, not this project (%r)" % (port, served, want))
    now = time.time()
    with _lock:
        # A server WE started wins: it is the one whose logs and problems we can
        # actually read, so never shadow it with an adopted guess.
        if project_dir in _procs:
            return status(project_dir)
        _adopted[project_dir] = {"url": "http://127.0.0.1:%d" % port, "port": port,
                                 "since": now, "touched_at": now, "source": source}
    return status(project_dir)


def discover(project_dir):
    """Find a server the agent started, when we were not watching its output.

    Needed because the URL is parsed from a live stream: reload the dashboard and
    that knowledge is gone, while the server is still up. A scan cannot PROVE the
    port belongs to this project — nothing local ties a listening socket to a
    folder without extra dependencies — so it only fires when the user has no
    preview of their own, and the result is labelled as detected rather than
    presented as certain."""
    project_dir = os.path.abspath(project_dir)
    with _lock:
        if project_dir in _procs or project_dir in _adopted:
            return None
        # A port ANOTHER project already owns is not ours to take. Without this,
        # starting a fresh project while the previous one was still serving on
        # :3000 adopted that server and showed the OLD project's site in the new
        # project's preview -- reported, and exactly the ambiguity a port scan
        # invites.
        taken = {a["port"] for a in _adopted.values()}
        taken |= {p.port for p in _procs.values()}

    # An EMPTY project cannot be serving anything, so scanning on its behalf can
    # only ever find somebody else's server. Require something runnable first.
    if not _is_runnable(project_dir):
        return None

    want = _fingerprint(project_dir)
    for port in _DEV_PORTS:
        if port in taken:
            continue
        if not _port_open(port) or not _http_ok(port):
            continue
        # Ask the OS who is on the port first: a process whose working
        # directory IS this project is proof, and it is the only check that
        # works for a project shipping no HTML of its own (an Express app with
        # server-rendered views, an API). Content checks cannot see those.
        owner_dir = _port_owner_dir(port)
        if owner_dir is not None:
            if not _dir_covers(owner_dir, project_dir):
                continue
        # No readable owner: fall back to content. When the project has its own
        # index.html, ask the port what it is serving and require a match --
        # that turns "something is listening" into evidence of ownership. With
        # neither signal available a scan proves nothing, so it declines: the
        # cost of guessing wrong is showing someone else's site as yours.
        elif not want or not _serves_fingerprint(port, want):
            continue
        try:
            adopt(project_dir, "http://127.0.0.1:%d" % port, source="detected")
            return port
        except WorkspaceError:
            continue
    return None


def _fingerprint(project_dir):
    """A distinctive string from the project's own index.html, or None.

    The <title> is the cheapest thing that is both present and specific in
    almost every generated page, and it is what tells one project's dev server
    apart from another's."""
    for rel in ("index.html", os.path.join("public", "index.html"),
                os.path.join("dist", "index.html"), os.path.join("src", "index.html")):
        path = os.path.join(project_dir, rel)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                head = fh.read(8000)
        except OSError:
            continue
        m = re.search(r"<title[^>]*>([^<]{3,120})</title>", head, re.I)
        if m:
            return m.group(1).strip()
    return None


def _serves_fingerprint(port, want, timeout=1.5):
    """Does this port serve a page carrying `want`?

    Best-effort and FAIL-CLOSED: anything unreadable counts as "not a match", so
    an uncertain scan declines to adopt rather than showing the wrong project."""
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=timeout) as r:
            body = r.read(200000).decode("utf-8", "replace")
    except Exception:                                            # noqa: BLE001
        return False
    return want.lower() in body.lower()


def _port_owner_dir(port):
    """The folder the process listening on `port` is running IN, or None.

    This is the strongest ownership signal available without adding a
    dependency, and unlike a <title> it works for every stack: an Express app
    with server-rendered views, an API with no HTML at all, a Vite dev server.
    The project that hit this had no <title> in any file it shipped, so the
    title check could not tell it apart from the previous project's server
    still sitting on :3000.

    Falls back to the listener's children: a shell or npm wrapper can hold the
    socket while the real server runs underneath it."""
    try:
        import psutil
    except ImportError:
        return None
    try:
        conns = psutil.net_connections(kind="inet")
    except Exception:                                            # noqa: BLE001
        return None                       # needs privileges on some systems
    for c in conns:
        if c.status != psutil.CONN_LISTEN or not c.laddr or c.laddr.port != port or not c.pid:
            continue
        try:
            proc = psutil.Process(c.pid)
        except Exception:                                        # noqa: BLE001
            return None
        try:
            return os.path.abspath(proc.cwd())
        except Exception:                                        # noqa: BLE001
            pass
        try:
            for child in proc.children(recursive=True):
                try:
                    return os.path.abspath(child.cwd())
                except Exception:                                # noqa: BLE001
                    continue
        except Exception:                                        # noqa: BLE001
            pass
        return None
    return None


def _dir_covers(owner_dir, project_dir):
    """Is a server running in `owner_dir` plausibly serving `project_dir`?"""
    if not owner_dir:
        return False
    a = os.path.normcase(os.path.abspath(owner_dir))
    b = os.path.normcase(os.path.abspath(project_dir))
    return a == b or a.startswith(b + os.sep)


def _served_title(port, timeout=1.5):
    """The <title> the port is actually serving, or None if it cannot be read."""
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=timeout) as r:
            body = r.read(200000).decode("utf-8", "replace")
    except Exception:                                            # noqa: BLE001
        return None
    m = re.search(r"<title[^>]*>([^<]{1,200})</title>", body, re.I)
    return m.group(1).strip() if m else None


def shutdown(project_dir):
    """Stop the preview AND the app behind it, whoever started it.

    stop() deliberately spares an adopted server: on a button labelled "Stop
    preview", killing a process we did not start is a bigger action than the
    label promises. Ending a SESSION is a different promise -- the session is
    the thing that started the app, so leaving a dev server holding a port
    after its session is gone is a leak the user has to clean up by hand.

    Scoped to the port this project was previewing, and only when the server
    was the agent's own (adopted from its output, or found serving this
    project's pages). Ports the hub hands out and the hub's own port are never
    touched by the by-port path -- those are handled by stop()."""
    project_dir = os.path.abspath(project_dir)
    with _lock:
        adopted = _adopted.get(project_dir)
        port = adopted["port"] if adopted else None
        source = adopted["source"] if adopted else None
    ours = stop(project_dir)                     # our own child, if any
    forget(project_dir)
    if not port or source not in ("agent", "detected"):
        return ours
    if PORT_RANGE[0] <= port <= PORT_RANGE[1] or port == _hub_port():
        return ours
    return _kill_listener(port) or ours


def sweep_own_range():
    """Reclaim preview ports left behind by a previous run of the hub.

    Measured on this machine: 99 of the 100 ports in PORT_RANGE were held by
    orphaned `python -m http.server` processes -- every preview ever started
    that outlived the hub that spawned it (a crash, a kill, a restart while a
    project was running). The next start then fails with "no free port", and
    every one of those servers is still burning a port for a project nobody is
    looking at.

    Only PORT_RANGE is touched. That range exists solely for previews the hub
    hands out -- adopt() refuses it precisely because it is ours -- so anything
    listening there at startup is a leaked preview, not the user's own work.
    Returns how many were reclaimed."""
    freed = 0
    for port in range(PORT_RANGE[0], PORT_RANGE[1] + 1):
        if not _port_open(port):
            continue
        if _kill_listener(port):
            freed += 1
    return freed


def _kill_listener(port):
    """Kill whatever is listening on `port`, with its children.

    A dev server is normally a tree (npm -> node, python -m http.server under a
    shell), so killing the single listening PID leaves the port held. Returns
    whether anything was killed."""
    try:
        import psutil
    except ImportError:
        return False
    pids = set()
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port and c.pid:
                pids.add(c.pid)
    except Exception:                                            # noqa: BLE001
        return False
    killed = False
    for pid in pids:
        try:
            proc = psutil.Process(pid)
            if proc.pid == os.getpid():
                continue                          # never the hub itself
            for child in proc.children(recursive=True):
                try:
                    child.kill()
                except Exception:                                # noqa: BLE001
                    pass
            proc.kill()
            killed = True
        except Exception:                                        # noqa: BLE001
            continue
    return killed


def forget(project_dir):
    """Stop showing an adopted server. Deliberately does NOT kill it: we did not
    start it, so we do not own its lifetime — the agent, or the user's own
    terminal, does."""
    with _lock:
        return _adopted.pop(os.path.abspath(project_dir), None) is not None


def _adopted_status(project_dir, now=None):
    now = now or time.time()
    with _lock:
        a = _adopted.get(project_dir)
        if not a:
            return None
        a["touched_at"] = now
        port, url, since, source = a["port"], a["url"], a["since"], a["source"]
    if not _http_ok(port):
        with _lock:
            _adopted.pop(project_dir, None)
        return None                      # it died; report idle, not a dead link
    return {
        "running": True, "state": "running", "url": url, "port": port,
        "kind": "started by the agent" if source == "agent" else "detected on this port",
        "log": [], "problems": [], "error": None,
        "uptime": int(now - since),
        "external": True,
    }


def running():
    """Every live preview, for the "what is running" list."""
    now = time.time()
    with _lock:
        items = list(_procs.items())
        adopted = list(_adopted.items())
    out = [{"project_dir": d, "port": p.port, "kind": p.kind, "state": p.state,
            "url": "http://127.0.0.1:%d" % p.port if p.state == "running" else None,
            "uptime": int(now - p.started_at),
            "idle": int(now - p.touched_at),
            "idle_stops_in": max(0, int(IDLE_TIMEOUT - (now - p.touched_at))),
            "external": False}
           for d, p in items]
    # Adopted servers are listed so the user can SEE them, but never counted
    # against the idle reaper: we did not start them, so we must not stop them.
    out += [{"project_dir": d, "port": a["port"],
             "kind": "started by the agent" if a["source"] == "agent"
                     else "detected on this port",
             "state": "running", "url": a["url"],
             "uptime": int(now - a["since"]), "idle": int(now - a["touched_at"]),
             "idle_stops_in": None, "external": True}
            for d, a in adopted]
    return out


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
        # No server of ours — but the AGENT may have started one, which is the
        # normal case once it follows the SHIP brief and runs what it built.
        ext = _adopted_status(project_dir)
        if ext:
            return ext
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
