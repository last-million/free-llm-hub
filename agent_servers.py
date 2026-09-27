r"""Servers and other never-ending processes inside /agent turns.

LIVE, 2026-09-27 (opencode, project-20260907-031142, "Failed" after 26m 28s):
the model started its project's server in the FOREGROUND. The CLI's shell tool
waits for the command to return, a server never does, the stream went silent,
and the stall watchdog resumed the turn with a generic "Nothing for 420s --
looks wedged, resuming". The model did not know why it had been stopped, so it
listed python processes (`tasklist | grep -i python`), killed a PID it found
in netstat, ran `start /B python app.py` again and blocked again: "opencode
produced nothing for 420s twice (retried once)". The hub itself is a
`python app.py` -- one of the processes that listing showed it.

Three things live here, all pure or psutil-only (no app / agentic_chat import,
so swarm_windows can use it too):

1. THE RULES the agent is given (brief_section for the per-session brief file,
   worker_rules for a swarm worker's prompt): a server is started DETACHED
   with its output in a log file, the hub's own preview is preferred for a web
   app, only processes the agent started itself may be stopped, and the hub's
   PID and port are named so it knows which python is not its to touch.

   The detached spellings are MEASURED on this machine (Windows 11, Git Bash,
   Windows PowerShell 5.1, pwsh 7.6, cmd) with the caller holding pipes on
   stdin/stdout/stderr and waiting for EOF -- the strictest thing a shell tool
   can do. A 20-second python child; "hung" = still no EOF after 12 s:
     bash   `python x.py &` (no redirect)                          HUNG
     bash   `nohup python x.py > log 2>&1 &`                       0.1 s
     PS     `Start-Process -NoNewWindow ... -RedirectStandardOutput` HUNG (5.1 and 7.6)
     PS     `Start-Process -WindowStyle Hidden ... -RedirectStandard*` HUNG
     PS     `Start-Process -WindowStyle Hidden -FilePath cmd
            -ArgumentList '/c','python x.py > log 2>&1'`             0.5 s
     cmd    `start "" /B cmd /c "python x.py > log 2>&1"`           HUNG
     cmd    same with `<nul >nul 2>&1` on the start                  HUNG
     cmd    `powershell -NoProfile -Command "Start-Process ...
            cmd -ArgumentList '/c',..."`                             0.6 s
   The hanging ones hand the caller's pipe handles down to the server
   (CreateProcess with handle inheritance); ShellExecute (Start-Process without
   -NoNewWindow/-Redirect*) and a bash redirect do not. In the three that
   return, the server was still alive afterwards and its log was in the cwd.

2. THE STALL DIAGNOSIS (diagnose_stall): when a turn has been silent for the
   stall deadline, is the CLI's shell blocked on a server it started? Two
   kinds of evidence, either suffices:
     * the process tree under the CLI -- a descendant started through a
       shell (the shell tool's work, not the CLI's own helpers) that is
       LISTENING, or whose command is a server / watcher. CLI-agnostic, and
       the only evidence for opencode:
       its `run --format json` emits `tool_use` only once a tool part is
       "completed" or "error" (read in the installed opencode binary), so the
       command that is still running never appears in its stream;
     * the last tool event, when it was the very last line the CLI printed
       and the CLI announces a command BEFORE running it (codex item.started,
       claude's assistant tool_use).

3. THE WORDS for the live view (stall_notice) and the resume prompt
   (resume_instruction): "your last command `X` starts a server that never
   returns ... it was / was not listening on port N ... start it detached with
   output to a log and continue" instead of a blind resume.

WHAT THE HUB CANNOT DO: stop an agent from killing the hub. The agent's shell
runs as the same Windows user as the hub, and `taskkill /F /PID <hub>` needs no
privilege the hub could withhold; a watcher that restarted the hub would be the
extra process this design rules out, and the hub's own events arrive too late
to intervene -- opencode reports a command only after it ran. So the defence is
prevention: the brief names the hub's PID(s) and port, every rule says to stop
only what the agent itself started, and nothing the hub writes to the agent
ever suggests killing a python process.
"""
from __future__ import annotations

import os
import re

# The hub's port when PORT is unset -- the same default app.py, agentic_chat
# and workspace use.
HUB_PORT_DEFAULT = 8787

# CLIs whose stream announces a shell command BEFORE it runs, so a tool event
# that is the last line printed means "this command has not returned".
# opencode is deliberately absent: it emits tool_use only on completion/error.
TOOL_EVENT_AT_START = frozenset({"codex", "claude"})

_SHELLS = frozenset({"bash", "sh", "dash", "zsh", "fish", "cmd", "powershell",
                     "pwsh", "git-bash", "busybox"})

# Commands that start something that does not exit on its own. Searched (not
# matched) in the tool text, which carries wrappers such as "Bash: ..." or
# codex's `"...powershell.exe" -Command '...'`. A bare tool name only counts
# in COMMAND position (start of the text, or after ; & | ( : or a quote), so
# `pip install uvicorn` or `npm i -g http-server` is not a server.
_CMDPOS = r"(?:^|[;&|'\"(:]\s*)"
_BUILD = r"(?!\s+(?:build|export|generate|lint|check|info|--version|-v)\b)"
_LONG_RUNNING = [
    # python entry points that are servers far more often than not
    r"\bpy(?:thon)?(?:[\d.]*)(?:\.exe)?\s+(?:-[a-zA-Z]+\s+)*[\w./\\:~-]*?"
    r"\b(?:app|server|serve|main|run|wsgi|asgi|api|bot)\.py\b(?!\s+(?:--help|-h)\b)",
    r"\bmanage\.py\s+runserver\b",
    r"-m\s+(?:http\.server|SimpleHTTPServer|flask\s+run|uvicorn|gunicorn|hypercorn|"
    r"streamlit\s+run|django\s+runserver|aiohttp\.web|livereload)\b",
    r"\bflask\s+run\b", r"\bstreamlit\s+run\b", r"\bfastapi\s+(?:dev|run)\b",
    _CMDPOS + r"(?:uvicorn|gunicorn|hypercorn|waitress-serve|daphne)\b",
    # node / js toolchains
    r"\b(?:npm|pnpm|yarn|bun)(?:\.cmd)?\s+(?:run\s+)?(?:dev|start|serve|preview|watch)\b",
    r"\bnpx(?:\.cmd)?\s+(?:(?:--yes|-y)\s+)?(?:(?:vite|next|astro|nuxi?|parcel)\b" + _BUILD
    + r"|(?:serve|http-server|live-server|nodemon|webpack-dev-server|json-server|"
    r"browser-sync)\b)",
    _CMDPOS + r"(?:nodemon|http-server|live-server|webpack-dev-server|json-server|"
    r"browser-sync)\b",
    _CMDPOS + r"vite\b" + _BUILD,
    r"\bnext\s+(?:dev|start)\b", r"\bng\s+serve\b", r"\bastro\s+dev\b",
    r"\bwebpack\s+serve\b",
    r"\bnode(?:\.exe)?\s+(?:--\S+\s+)*[\w./\\:~-]*?\b(?:server|app|index|main)\.[cm]?js\b",
    # everything else that serves or watches
    r"\bphp\s+-S\b", r"\brails\s+s(?:erver)?\b", r"\bdotnet\s+(?:run|watch)\b",
    r"\bhugo\s+server\b", r"\bjekyll\s+serve\b", r"\bcargo\s+watch\b",
    r"\bdocker(?:-compose|\s+compose)\s+up\b(?![^\n]*\s(?:-d|--detach)\b)",
    r"(?:^|\s)--watch\b", r"\btsc(?:\.cmd)?\s+(?:[^\n]*\s)?-w\b",
    r"\btail\s+-f\b", r"\bping\s+(?:[^\n]*\s)?-t\b",
]
_LONG_RUNNING_RE = re.compile("|".join("(?:%s)" % p for p in _LONG_RUNNING), re.I)

# Launch spellings MEASURED to keep a pipe-reading shell tool waiting even
# though they look detached (see the module docstring). Only meaningful next to
# a long-running command, so they are not a pattern of their own.
_LEAKY_LAUNCH_RE = re.compile(
    r"\bstart\s+(?:\"[^\"]*\"\s+)?/b\b|start-process\b[^\n]*-(?:nonewwindow|redirectstandard)",
    re.I)


def hub_port():
    try:
        return int(os.environ.get("PORT") or HUB_PORT_DEFAULT)
    except (TypeError, ValueError):
        return HUB_PORT_DEFAULT


_HUB_PIDS = []


def hub_pids():
    """This process, plus its parent when that parent is a python too.

    A Windows venv's Scripts\\python.exe is a LAUNCHER that runs the real
    interpreter as its child, so a hub started as `.venv\\Scripts\\python
    app.py` is two python processes -- and `taskkill /T` on the launcher takes
    the hub with it. Computed once: neither changes while the hub runs."""
    if _HUB_PIDS:
        return list(_HUB_PIDS)
    pids = [os.getpid()]
    try:
        import psutil
        parent = psutil.Process(pids[0]).parent()
        if parent is not None and (parent.name() or "").lower().startswith("python"):
            pids.append(parent.pid)
    except Exception:                                            # noqa: BLE001
        pass
    _HUB_PIDS[:] = pids
    return list(pids)


def _pid_text(pids):
    pids = list(pids or [])
    if not pids:
        return "unknown"
    if len(pids) == 1:
        return str(pids[0])
    return "%d (its launcher: %s)" % (pids[0], ", ".join(str(p) for p in pids[1:]))


def starts_long_running(command):
    """Does this shell command start something that does not exit by itself?"""
    return bool(command) and bool(_LONG_RUNNING_RE.search(str(command)))


def leaky_launch(command):
    """Is it spelled with a launcher measured to keep the shell tool waiting?"""
    return bool(command) and bool(_LEAKY_LAUNCH_RE.search(str(command)))


# Where one command of a command line ends: && || ; | and a lone & that is not
# part of a redirection (2>&1, &>).
_SEGMENT_SEP_RE = re.compile(r"&&|\|\||;|\|(?!\|)|(?<![>&\d])&(?![&>])|[\r\n]")
_REDIRECT_RE = re.compile(r"\s*\d?>>?\s*(?:&\d|\"[^\"]*\"|\S+)|\s*<\s*\S+")
_WRAPPER_RE = re.compile(
    r"^\s*(?:(?:Bash|Shell|PowerShell):\s*|[^\n]*?\s-Command\s+|"
    r"(?:bash|sh)(?:\.exe)?\s+(?:-l?c\s+)?|cmd(?:\.exe)?\s+/[ck]\s+|"
    r"start\s+(?:\"[^\"]*\"\s+)?/b\s+|nohup\s+|exec\s+)+", re.I)
_START_PROCESS_RE = re.compile(
    r"start-process\b.*?-FilePath\s+['\"]?([^\s'\"]+)['\"]?"
    r"(?:.*?-ArgumentList\s+((?:'[^']*'|\"[^\"]*\"|[^\s-][^\s]*)"
    r"(?:\s*,\s*(?:'[^']*'|\"[^\"]*\"|[^\s,]+))*))?", re.I)


def server_command(command, limit=120):
    """The one command of `command` that starts the server, without wrappers
    or redirections ("python app.py", "npm run dev", "uvicorn main:app"), for
    quoting back to the model and for its detached spellings; "" when none."""
    text = str(command or "")
    sp = _START_PROCESS_RE.search(text)
    if sp:
        # Start-Process -FilePath python -ArgumentList app.py -> python app.py
        args = re.sub(r"['\"]", "", sp.group(2) or "").replace(",", " ")
        text = (sp.group(1) + " " + args).strip()
    m = _LONG_RUNNING_RE.search(text)
    if not m:
        return ""
    start, end = 0, len(text)
    for sep in _SEGMENT_SEP_RE.finditer(text):
        if sep.end() <= m.start():
            start = sep.end()
        elif sep.start() >= m.end():
            end = sep.start()
            break
    seg = text[start:end]
    seg = _WRAPPER_RE.sub("", seg).strip().strip("'\"").strip()
    seg = _WRAPPER_RE.sub("", seg)
    seg = _REDIRECT_RE.sub("", seg).strip().strip("'\"&( ").strip()
    if not starts_long_running(seg):
        seg = m.group(0).strip(" \t;&|'\"(:")
    return _one_line(seg, limit)


def _one_line(text, limit=160):
    s = re.sub(r"\s+", " ", str(text or "")).strip().replace("`", "'")
    return s if len(s) <= limit else s[:limit - 3].rstrip() + "..."


# The three spellings, with `{cmd}` for the server command. See the docstring
# for the measurements behind each one.
def detach_examples(cmd="python app.py", windows=None):
    cmd = _one_line(cmd or "python app.py", 120).replace("'", "")
    if windows is None:
        windows = os.name == "nt"
    bash = "nohup %s > server.log 2>&1 &" % cmd
    if not windows:
        return [("bash", bash)]
    ps = ("Start-Process -WindowStyle Hidden -FilePath cmd -ArgumentList "
          "'/c','%s > server.log 2>&1'" % cmd)
    cmd_exe = ('powershell -NoProfile -Command "Start-Process -WindowStyle Hidden '
               "-FilePath cmd -ArgumentList '/c','%s > server.log 2>&1'\"" % cmd)
    return [("bash", bash), ("PowerShell", ps), ("cmd", cmd_exe)]


def brief_section(pids=None, port=None, windows=None):
    """The brief file's section on servers. Markdown; dynamic hub PID/port."""
    pids = hub_pids() if pids is None else list(pids)
    port = hub_port() if port is None else port
    if windows is None:
        windows = os.name == "nt"
    nl = chr(10)
    lines = [
        "## Servers, watchers and other commands that never exit",
        "",
        "A dev server or a watcher run in the FOREGROUND never returns: your shell "
        "tool waits for it forever, your turn goes silent, and the hub has to stop "
        "the shell (and the server with it). That means commands like "
        "`python app.py`, `flask run`, `uvicorn main:app`, `npm run dev`, "
        "`npm start`, `vite`, `node server.js`, `python -m http.server`, or "
        "anything with `--watch`.",
        "",
        "- A web app does not need you to keep it running. When your turn ends, "
        "the hub's preview panel starts this project by itself (its package.json "
        "dev/start/serve/preview script, else app.py/main.py/server.py/manage.py, "
        "else index.html) on a port it chooses and passes in the PORT environment "
        "variable, and it picks up any `http://127.0.0.1:PORT` address you print. "
        "So make the server honour PORT when it is set, and end your turn with the "
        "URL instead of leaving a server running.",
        "- To test it yourself, start it DETACHED with its output in a log file, so "
        "the command returns at once. Use the spelling for the shell your tool runs:",
    ]
    for shell, example in detach_examples(windows=windows):
        lines.append("  - %s: `%s`" % (shell, example))
    if windows:
        lines.append(
            "  Not `start /B`, not `Start-Process -NoNewWindow` or "
            "`-RedirectStandardOutput`, not a bare `&`: they keep your shell tool's "
            "output pipe open and it hangs exactly as if the server ran in the "
            "foreground. A background option of your own shell tool (Claude Code's "
            "run_in_background) is fine too.")
    lines += [
        "  Then check it with ONE request that has a timeout (`curl -s -m 5 "
        "http://127.0.0.1:PORT/`; in PowerShell `curl.exe`) or read server.log. "
        "Never wait on it with a command that has no timeout.",
        "- Stop only what YOU started in this session, by the PID you started. "
        "Never kill processes by name (`taskkill /IM python.exe`, `pkill python`, "
        "`Stop-Process -Name python`) and never kill a PID you only found in a "
        "process list or in netstat. If a port is taken, use another port: what "
        "holds it may be the hub's preview of this project or someone else's app.",
        "- THE HUB IS NOT YOURS. Calvoun Free LLM Hub is the `python app.py` with "
        "PID %s, listening on port %d; it is what runs this session. Never stop, "
        "restart or signal it, and never bind port %d."
        % (_pid_text(pids), port, port),
    ]
    return nl.join(lines)


def worker_rules(pids=None, port=None, windows=None):
    """The same rules, short enough for a swarm worker's argv-borne prompt."""
    pids = hub_pids() if pids is None else list(pids)
    port = hub_port() if port is None else port
    ex = "; ".join("%s: `%s`" % (s, e) for s, e in
                   detach_examples("CMD", windows=windows)[:2])
    return ("Never run a server or watcher in the foreground: the shell tool waits "
            "for it and the turn hangs. Start it DETACHED with its output in a log "
            "(%s), check it with one request that has a timeout, or leave it to "
            "the hub's preview. Stop only processes you started yourself; the hub "
            "(python app.py, PID %s, port %d) is not yours -- never kill python "
            "processes by name." % (ex, _pid_text(pids), port))


# --------------------------------------------------------------------------- #
# What is running under a silent CLI
# --------------------------------------------------------------------------- #

def _short_cmdline(argv):
    if not argv:
        return ""
    head = os.path.basename(str(argv[0]))
    if head.lower().endswith(".exe"):
        head = head[:-4]
    return _one_line(" ".join([head] + [str(a) for a in argv[1:]]), 200)


def session_processes(root_pid):
    """Every descendant of `root_pid` (not the root itself), as dicts:
    {pid, name, cmd, ports (listening), via_shell}. [] without psutil or on
    any error. `via_shell`: a shell sits BELOW the root and above the process
    -- what a shell TOOL's child looks like. The CLI itself never has it (the
    root is often cmd.exe running the CLI's .cmd shim, and the walk stops at
    the root), which matters: the CLI's own command line carries the user's
    prompt, and opencode's `run` listens on a port of its own."""
    try:
        import psutil
        root = psutil.Process(root_pid)
        kids = root.children(recursive=True)
    except Exception:                                            # noqa: BLE001
        return []
    info = {}
    for k in kids:
        try:
            name = (k.name() or "").lower()
            if name.endswith(".exe"):
                name = name[:-4]
            try:
                cmd = _short_cmdline(k.cmdline())
            except Exception:                                    # noqa: BLE001
                cmd = name
            ports = set()
            try:
                get = getattr(k, "net_connections", None) or k.connections
                for c in get(kind="inet"):
                    if c.status == psutil.CONN_LISTEN and c.laddr:
                        ports.add(int(c.laddr.port))
            except Exception:                                    # noqa: BLE001
                pass
            info[k.pid] = {"pid": k.pid, "ppid": k.ppid(), "name": name,
                           "cmd": cmd, "ports": ports}
        except Exception:                                        # noqa: BLE001
            continue
    out = []
    for pid, p in info.items():
        via, seen, up = False, set(), p["ppid"]
        while up in info and up not in seen and up != root_pid:
            seen.add(up)
            if info[up]["name"] in _SHELLS:
                via = True
                break
            up = info[up]["ppid"]
        out.append({"pid": pid, "name": p["name"], "cmd": p["cmd"],
                    "ports": sorted(p["ports"]), "via_shell": via})
    return out


def diagnose_stall(root_pid, cli_id=None, last_tool=None, tool_was_last=False,
                   exclude_pids=None, exclude_ports=None, processes=None):
    """Is a silent turn's shell blocked on a server it started?

    Returns None (no evidence: an ordinary wedge) or
    {"command": str, "ports": [int], "source": "tool"|"process"}.
    Never raises."""
    try:
        exclude_pids = set(hub_pids() if exclude_pids is None else exclude_pids)
        exclude_ports = set([hub_port()] if exclude_ports is None else exclude_ports)
        procs = session_processes(root_pid) if processes is None else processes
        servers, runners = [], []
        for p in procs or []:
            # Only what a SHELL started: that is the shell tool's work. An MCP
            # server launched through a .cmd shim also sits under cmd.exe, and
            # a listening one would read as the agent's server.
            if (p.get("pid") in exclude_pids or not p.get("via_shell")
                    or "mcp" in str(p.get("cmd") or "").lower()):
                continue
            ports = [x for x in (p.get("ports") or []) if x not in exclude_ports]
            if ports:
                servers.append(dict(p, ports=ports))
            elif starts_long_running(p.get("cmd")):
                runners.append(p)
        tool_cmd = None
        if (last_tool and tool_was_last and cli_id in TOOL_EVENT_AT_START
                and starts_long_running(last_tool)):
            tool_cmd = last_tool
        if not (servers or runners or tool_cmd):
            return None
        ports = sorted({x for s in servers for x in s["ports"]})
        if tool_cmd:
            command = server_command(tool_cmd) or _one_line(tool_cmd, 120)
            source = "tool"
        else:
            best = (servers or runners)[0]
            command = server_command(best["cmd"]) or _one_line(best["cmd"], 120)
            source = "process"
        return {"command": command, "ports": ports, "source": source,
                "leaky": bool(tool_cmd and leaky_launch(tool_cmd))}
    except Exception:                                            # noqa: BLE001
        return None


def _ports_text(ports):
    ports = list(ports or [])
    if not ports:
        return ""
    if len(ports) == 1:
        return "port %d" % ports[0]
    return "ports " + ", ".join(str(p) for p in ports)


def stall_notice(diag, stall_seconds, again=False):
    """One line for the live view. Deliberately WITHOUT an http:// URL: the
    dashboard adopts any local URL it sees into the preview, once per port,
    and this server is about to be stopped -- adopting it now would burn that
    port's one try before the agent restarts it properly."""
    cmd = (diag or {}).get("command") or "a server"
    where = _ports_text((diag or {}).get("ports"))
    if where:
        head = "Server running on %s in the foreground (`%s`)" % (where, cmd)
    else:
        head = "`%s` is a long-running process started in the foreground" % cmd
    return ("%s%s: the shell never returned, so nothing came for %ds. Not a "
            "wedge -- the hub stopped it with the blocked shell and is resuming "
            "with instructions to start it detached%s."
            % (head, " again" if again else "", int(stall_seconds),
               " (last retry)" if again else ""))


def resume_instruction(diag, stall_seconds, pids=None, port=None, windows=None):
    """The prompt the resumed turn gets instead of a blind "continue"."""
    pids = hub_pids() if pids is None else list(pids)
    port = hub_port() if port is None else port
    cmd = (diag or {}).get("command") or "your server command"
    where = _ports_text((diag or {}).get("ports"))
    if where:
        state = ("It was listening on %s, but in the foreground, so the hub had "
                 "to stop it together with your blocked shell: it is NOT running "
                 "now." % where)
    else:
        state = ("It was not listening on any port yet; the hub stopped it "
                 "together with your blocked shell, so it is not running now.")
    leaky = ""
    if (diag or {}).get("leaky"):
        leaky = (" `start /B` and `Start-Process -NoNewWindow` still hold your "
                 "shell's output pipe, so they block the same way.")
    ex = " | ".join("%s: `%s`" % (s, e)
                    for s, e in detach_examples(cmd, windows=windows))
    return ("[hub] Your last shell command `%s` starts a server that never "
            "returns, so your shell tool was blocked on it and nothing happened "
            "for %ds. %s%s Do not run it in the foreground again. Start it "
            "DETACHED with its output in a log file so the command returns at "
            "once -- %s -- then check it with one request that has a timeout "
            "(curl -s -m 5 http://127.0.0.1:PORT/) or read server.log, and "
            "continue the task where you left off. For a web app you may also "
            "skip running it: the hub's preview starts the project itself when "
            "your turn ends. Stop only processes you started yourself; do not "
            "kill python processes by name or by a PID from a process list -- "
            "the hub (python app.py, PID %s, port %d) runs this session."
            % (cmd, int(stall_seconds), state, leaky, ex, _pid_text(pids), port))


def failure_detail(cli_id, diag):
    """The error when the model blocked its shell on a server every time."""
    where = _ports_text((diag or {}).get("ports"))
    return ("%s kept running `%s`%s in the foreground and its shell never "
            "returned (retried twice, with instructions to start it detached)."
            % (cli_id, (diag or {}).get("command") or "a server",
               (" (%s)" % where) if where else ""))
