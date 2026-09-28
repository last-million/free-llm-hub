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

   Each spelling also RECORDS THE PID in server.pid (bash `& echo $! >
   server.pid`; PowerShell `-PassThru | Select-Object -ExpandProperty Id |
   Set-Content server.pid`, cmd the same through `powershell -Command`).
   MEASURED 2026-09-28, same harness, a listening python http server: bash
   0.1 s, pwsh 7.6 0.5 s, PowerShell 5.1 0.6 s, cmd 0.4 s, server listening
   afterwards in all four. Stopping by the recorded PID:
     PowerShell/cmd  the PID is cmd.exe's (the server is its child):
                     `Stop-Process -Id <it>` left the server LISTENING;
                     `taskkill /PID <it> /T /F` freed the port.
     bash            `$!` is an MSYS PID, not a Windows one; bash's own
                     `kill $(cat server.pid)` freed the port 13 times of 14
                     (hence "check the port afterwards, else stop by port").
     by port         `Get-NetTCPConnection -LocalPort N -State Listen` names
                     the python itself; `taskkill /PID <it> /F` freed it.
   Stopping by NAME or COMMAND LINE is forbidden in every text the agent gets:
   live 2026-09-28 (opencode log, run 1476d09d), an agent ran `Get-Process |
   Where-Object {$_.ProcessName -like "*python*" -and $_.CommandLine -like
   "*app.py*"} | Select-Object Id`, then `Stop-Process -Id 1488` on what it
   found -- the hub is a `python app.py` too, so that filter matches it.

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

   EARLY (early_server_diagnosis, ~60 s of silence instead of 420 s): the
   same question, asked strictly -- the shell tool is waiting on a server
   started after the CLI's last line and on nothing else -- and extended to
   ORPHANS (`start /B`, a bare `&`: the server outlives the shell that started
   it, so it is in no tree), recognised by the TURN_MARKER every turn's CLI
   carries in its environment. See the section above early_server_diagnosis.

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
only what the agent itself started -- by the PID it recorded or the port it
chose, never by name or command line -- the brief's stop spellings are only
PID/port based, and the resume prompt names no kill command at all.
"""
from __future__ import annotations

import os
import re
import time

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
# for the measurements behind each one. Each one also RECORDS THE PID in
# server.pid (PID_FILE): the only safe way to stop the server later is by
# that PID or by the port it listens on, never by searching processes by name
# or command line -- the hub itself is a `python app.py` and matches such a
# search (live 2026-09-28, see the module docstring).
PID_FILE = "server.pid"


def detach_examples(cmd="python app.py", windows=None):
    cmd = _one_line(cmd or "python app.py", 120).replace("'", "")
    if windows is None:
        windows = os.name == "nt"
    bash = "nohup %s > server.log 2>&1 & echo $! > %s" % (cmd, PID_FILE)
    if not windows:
        return [("bash", bash)]
    # -PassThru returns the started process; its Id is cmd.exe's (the server
    # is cmd's child), which is why the stop rule says taskkill /T.
    ps = ("Start-Process -WindowStyle Hidden -FilePath cmd -ArgumentList "
          "'/c','%s > server.log 2>&1' -PassThru | Select-Object -ExpandProperty Id "
          "| Set-Content %s" % (cmd, PID_FILE))
    cmd_exe = ('powershell -NoProfile -Command "Start-Process -WindowStyle Hidden '
               "-FilePath cmd -ArgumentList '/c','%s > server.log 2>&1' -PassThru "
               "| Select-Object -ExpandProperty Id | Set-Content %s\"" % (cmd, PID_FILE))
    return [("bash", bash), ("PowerShell", ps), ("cmd", cmd_exe)]


def stop_examples(port="PORT", windows=None):
    """How to stop a server the agent started: by the PID it recorded, or by
    the port it started it on. Only for the BRIEF -- the resume prompt never
    names a kill command (see the module docstring's last paragraph)."""
    if windows is None:
        windows = os.name == "nt"
    bash = "kill $(cat %s)" % PID_FILE
    if not windows:
        return [("bash, by PID", bash),
                ("by port", "lsof -t -iTCP:%s -sTCP:LISTEN" % port)]
    return [
        ("bash, by PID", bash),
        ("PowerShell or cmd, by PID", "taskkill /PID <pid from %s> /T /F" % PID_FILE),
        ("PowerShell, by port", "Get-NetTCPConnection -LocalPort %s -State Listen "
                                "| Select-Object -ExpandProperty OwningProcess" % port),
        ("cmd, by port", "netstat -ano | findstr LISTENING | findstr :%s" % port),
    ]


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
        "- RECORD THE PID of every server you start, at the moment you start it: "
        "the spellings above write it to `%s` (`cat %s`, in PowerShell "
        "`Get-Content %s`). Keep the port you started it on, too."
        % (PID_FILE, PID_FILE, PID_FILE),
        "- Stop only what YOU started in this session, and only in one of two "
        "ways: by that recorded PID, or by the port you started it on. Spellings:",
    ]
    for how, example in stop_examples(windows=windows):
        lines.append("  - %s: `%s`" % (how, example))
    if windows:
        lines.append(
            "  The PowerShell/cmd PID is the `cmd` that runs your server, so stop "
            "it with `/T` (its whole tree): `Stop-Process -Id` on it leaves the "
            "server itself running. A bash `$!` is a bash PID, not a Windows "
            "one: stop it with bash's own `kill`, never pass it to `taskkill` or "
            "`Stop-Process`. The by-port lines print the PID of the server "
            "itself; stop it with `taskkill /PID <that pid> /F`.")
    else:
        lines.append("  The by-port line prints the PID of the server itself; stop "
                     "it with `kill <that pid>`.")
    lines += [
        "  After stopping it, check the port is free (the same one request with "
        "a timeout); if it still answers, stop it by its port.",
        "  If a PID you are about to stop is one of the hub's PIDs below, it is "
        "not your server: do not stop it.",
        "- NEVER find a process to stop by its name or its command line: not "
        "`Get-Process | Where-Object ... app.py`, not `Get-CimInstance "
        "Win32_Process` / `wmic` filtered on a command line, not `tasklist | "
        "findstr python`, not `ps | grep python`, not `pkill -f app.py`, "
        "`taskkill /IM python.exe` or `Stop-Process -Name python`. The hub below "
        "is itself a `python app.py`, so every such search matches the hub too. "
        "Never kill processes by name, and never kill a PID you only found in a "
        "process list, or in netstat for any port other than the one you "
        "started your server on. If a port is taken by something you did not "
        "start, use another port: what holds it may be the hub's preview of "
        "this project or someone else's app.",
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
            "for it and the turn hangs. Start it DETACHED, log and PID recorded "
            "(%s), check it with one request with a timeout, or leave it to the "
            "hub's preview. Stop only what you started, by that PID or its port, "
            "never by a name or command-line search: python/app.py matches the "
            "hub (python app.py, PID %s, port %d), not yours."
            % (ex, _pid_text(pids), port))


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
            try:
                started = k.create_time()
            except Exception:                                    # noqa: BLE001
                started = None
            info[k.pid] = {"pid": k.pid, "ppid": k.ppid(), "name": name,
                           "cmd": cmd, "ports": ports, "started": started}
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
        out.append({"pid": pid, "ppid": p["ppid"], "name": p["name"], "cmd": p["cmd"],
                    "ports": sorted(p["ports"]), "via_shell": via,
                    "started": p["started"]})
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
                "leaky": bool(tool_cmd and leaky_launch(tool_cmd)),
                "pids": [s.get("pid") for s in servers + runners]}
    except Exception:                                            # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# Early detection: a server blocking the shell tool, long before the stall
# --------------------------------------------------------------------------- #
#
# diagnose_stall above runs only once a turn has been silent for the whole
# stall deadline (420 s), and for opencode -- which reports a command only
# after it returned -- the process tree is its only evidence. The live failure
# cost 2 x 420 s before anything was said. early_server_diagnosis answers the
# same question after ~60 s of silence, so it must be much stricter: it may
# only fire when the CLI's shell tool is waiting on a server and on NOTHING
# ELSE, so a long `pytest`, `npm install`, `cargo build` or a model thinking
# for minutes is never touched.
#
# ORPHANS. `start /B python app.py` (cmd), `python app.py &` (bash) and
# Start-Process hand the shell's pipe to the server and let the shell exit:
# the server is then nobody's child -- the tree walk from the CLI never sees
# it, the old tree kill never reached it, and it kept its port (live: the
# resumed model found the first attempt's server still on :5000 in netstat and
# killed it by PID). The hub stamps every turn's CLI with TURN_MARKER in its
# environment, which every process the agent starts inherits, so such an
# orphan is recognised by that marker -- and only a process carrying it is
# ever stopped.
#
# WHY IT IS STOPPED, NOT LEFT RUNNING. The shell tool stays blocked until
# every holder of its pipe's write end is gone, and that handle lives inside
# the server; the only other way out is to kill the CLI (the pipe's reader),
# and a server whose output pipe has no reader breaks. MEASURED here: with its
# stdout/stderr pipe reader closed, `python -m http.server` and a handler that
# print()s stayed alive but answered 3/3 requests with RemoteDisconnected (the
# request log write raises before the response); only a logging-based handler
# (werkzeug's shape) kept answering 200. So the server is stopped with the
# blocked shell and the model is told to restart it detached.

TURN_MARKER = "CALVOUN_AGENT_TURN"
# workspace._PREVIEW_MARKER: the hub's own preview of the project. Never the
# agent's, whatever else its environment carries.
PREVIEW_MARKER = "CALVOUN_PREVIEW"
# A server must have been up this long before it counts: the model may be
# about to curl it and stop it itself.
SERVER_MIN_AGE = 20.0

# The CLI's own helpers that happen to sit under a shell (an MCP server or a
# language server launched through a .cmd shim) -- never the agent's command.
_HELPER_RE = re.compile(r"mcp|--stdio\b|language-?server|langserver|\blsp\b", re.I)
# Pipeline filters and console hosts: they wait on whatever feeds them.
_NEUTRAL = frozenset({"conhost", "openconsole", "tee", "cat", "grep", "egrep", "fgrep",
                      "findstr", "head", "tail", "sed", "awk", "gawk", "cut", "tr",
                      "more", "less", "sort", "uniq", "wc"})
# Finite work that may LISTEN while it runs (a test's live server, a kernel, a
# download): a listening socket on one of these is not a server blocking the
# shell -- the shell is legitimately waiting for it to finish.
_FINITE_RE = re.compile(
    r"\b(?:py\.?test|unittest|nose2|tox|nox|jest|vitest|mocha|ava|karma|playwright|"
    r"cypress|puppeteer|selenium|go\s+(?:test|build)|cargo\s+(?:test|build|check)|"
    r"mvnw?|gradlew?|msbuild|dotnet\s+(?:test|build)|pip3?|pipx|"
    r"(?:npm|pnpm|yarn|bun)\s+(?:i|install|ci|add|test|t)|torchrun|accelerate|"
    r"deepspeed|ipykernel(?:_launcher)?|nbconvert|curl|wget)\b", re.I)
# What only starts something else: the shell waiting on it waits on its child.
_LAUNCHER_RE = re.compile(
    r"^(?:npm|npx|pnpm|pnpx|yarn|bun|bunx|uv|uvx|poetry|pipenv|pdm|hatch|rye|py|"
    r"nohup|env|timeout|cross-env|dotenv|concurrently|npm-run-all|run-p|run-s|"
    r"winpty)\b|^(?:cargo|go|dotnet)\s+(?:run|watch)\b", re.I)
# `node ...\npm\bin\npm-cli.js run dev` is `npm run dev`.
_NODE_PM_RE = re.compile(r"^node\s+.*?[\\/](npm|npx|pnpm|pnpx|yarn)(?:-cli)?\.[cm]?js\b",
                         re.I)
# Entry points that are batch scripts about as often as servers: a port is
# needed before one of these counts.
_WEAK_ENTRY_RE = re.compile(r"\b(?:main|run|api|index)\.(?:py|[cm]?js)\b|"
                            r"\bdotnet\s+run\b", re.I)


def _normal_cmd(cmd):
    return _NODE_PM_RE.sub(lambda m: m.group(1), str(cmd or "").strip(), count=1)


def server_by_command(cmd):
    """Does this command line alone say "server" (no port needed)?
    `python app.py`, `npm run dev`, `uvicorn m:app` yes; `python main.py`,
    `node index.js` -- as often a batch job -- only with a listening port."""
    cmd = _normal_cmd(cmd)
    return starts_long_running(cmd) and starts_long_running(_WEAK_ENTRY_RE.sub(" ", cmd))


def _kind(p, exclude_ports):
    """helper | shell | server | neutral | launcher | other, plus the ports."""
    name = str(p.get("name") or "").lower()
    cmd = _normal_cmd(p.get("cmd") or name)
    if _HELPER_RE.search(cmd):
        return "helper", []
    if name in _SHELLS:
        return "shell", []
    if _FINITE_RE.search(cmd):
        return "other", []
    ports = [x for x in (p.get("ports") or []) if x not in exclude_ports]
    if ports:
        return "server", ports
    if server_by_command(cmd):
        return "server", []
    if name in _NEUTRAL:
        return "neutral", []
    if _LAUNCHER_RE.match(cmd) or starts_long_running(cmd):
        return "launcher", []
    return "other", []


def _listening_ports(pids):
    """{pid: {port}} for the given pids, one system-wide table read."""
    pids = set(pids or ())
    out = {}
    if not pids:
        return out
    try:
        import psutil
    except ImportError:
        return out
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.pid in pids and c.status == psutil.CONN_LISTEN and c.laddr:
                out.setdefault(c.pid, set()).add(int(c.laddr.port))
        return out
    except Exception:                                            # noqa: BLE001
        pass
    for pid in pids:
        try:
            proc = psutil.Process(pid)
            get = getattr(proc, "net_connections", None) or proc.connections
            for c in get(kind="inet"):
                if c.status == psutil.CONN_LISTEN and c.laddr:
                    out.setdefault(pid, set()).add(int(c.laddr.port))
        except Exception:                                        # noqa: BLE001
            continue
    return out


def orphan_processes(marker, since, tree_pids=()):
    """Processes started after `since` (epoch seconds) whose environment
    carries this turn's `marker` but that are no longer under the CLI: what
    `start /B`, a bare `&` or Start-Process leave behind once the shell that
    launched them exited. Same dict shape as session_processes (plus
    "orphan": True). [] without psutil, a marker or a start time; never raises."""
    if not marker or since is None:
        return []
    try:
        import psutil
    except ImportError:
        return []
    skip = set(tree_pids or ()) | {os.getpid()}
    found = []
    try:
        procs = psutil.process_iter(["pid", "ppid", "name", "create_time"])
        for proc in procs:
            try:
                info = proc.info
                pid, started = info.get("pid"), info.get("create_time")
                if pid in skip or not started or started <= since:
                    continue
                env = proc.environ()
                if env.get(TURN_MARKER) != marker or env.get(PREVIEW_MARKER):
                    continue
                name = (info.get("name") or "").lower()
                if name.endswith(".exe"):
                    name = name[:-4]
                try:
                    cmd = _short_cmdline(proc.cmdline())
                except Exception:                                # noqa: BLE001
                    cmd = name
                found.append({"pid": pid, "ppid": info.get("ppid"), "name": name,
                              "cmd": cmd, "ports": [], "via_shell": True,
                              "started": started, "orphan": True})
            except Exception:                                    # noqa: BLE001
                continue
    except Exception:                                            # noqa: BLE001
        return found
    ports = _listening_ports(p["pid"] for p in found)
    for p in found:
        p["ports"] = sorted(ports.get(p["pid"], ()))
    return found


def early_server_diagnosis(root_pid, marker=None, since=None, now=None,
                           exclude_pids=None, exclude_ports=None, processes=None,
                           orphans=None, min_age=SERVER_MIN_AGE, last_tool=None):
    """After a short silence: is the CLI's shell tool blocked on a server --
    and on nothing else?

    `since` is the wall-clock time of the last line the CLI printed. Every
    CLI prints something once a command returns (opencode its tool_use,
    codex item.completed, claude the tool_result), so a server started AFTER
    that line belongs to a command that has not returned -- and one started
    before it (run detached, or Claude Code's run_in_background) is never a
    candidate, however long the model then thinks.

    Fires only when all of these hold:
      * a process the shell tool started (in the CLI's tree below a shell, or
        an orphan carrying `marker`) is a server: LISTENING on a port that is
        not the hub's, or running an unambiguous server/watcher command;
      * it started after `since` and has been up `min_age` seconds;
      * nothing else the shell tool runs is real work: every other process
        there is a shell, a launcher (npm/npx/uv/py...), a pipeline filter, a
        server or a server's own child. A test runner, installer or build --
        even one that started a server of its own -- means the shell is
        legitimately busy and the answer is None.
    The hub's PIDs and port are never candidates. Returns None or
    {"command", "ports", "pids" (what listens, best first), "stop" (every
    candidate, launchers included), "orphans", "source": "early", "leaky"}.
    Never raises."""
    try:
        now = time.time() if now is None else float(now)
        exclude_pids = set(hub_pids() if exclude_pids is None else exclude_pids)
        exclude_ports = set([hub_port()] if exclude_ports is None else exclude_ports)
        tree = session_processes(root_pid) if processes is None else processes
        tree = [p for p in (tree or []) if isinstance(p, dict)]
        if orphans is None:
            orphans = orphan_processes(marker, since,
                                       {p.get("pid") for p in tree} | {root_pid})
        pool = {}
        for p in tree:
            if p.get("via_shell") and p.get("pid") not in exclude_pids:
                pool[p.get("pid")] = dict(p, orphan=False)
        for p in orphans or []:
            if (isinstance(p, dict) and p.get("pid") not in pool
                    and p.get("pid") not in exclude_pids):
                pool[p.get("pid")] = dict(p, orphan=True)
        if not pool:
            return None
        kinds = {pid: _kind(p, exclude_ports) for pid, p in pool.items()}

        def under(pid, wanted):
            seen, up = set(), pool[pid].get("ppid")
            while up in pool and up not in seen:
                seen.add(up)
                if kinds[up][0] in wanted:
                    return up
                up = pool[up].get("ppid")
            return None

        for pid, (kind, _ports) in kinds.items():
            if kind == "other" and under(pid, ("server", "helper")) is None:
                return None                     # the shell is doing real work
        cands = []
        for pid, (kind, ports) in kinds.items():
            if kind != "server" or under(pid, ("helper",)) is not None:
                continue
            started = pool[pid].get("started")
            if started is None or now - float(started) < float(min_age):
                continue
            if since is not None and float(started) <= float(since):
                continue
            cands.append((pid, ports))
        if not cands:
            return None
        cands.sort(key=lambda c: (not c[1], float(pool[c[0]].get("started") or 0)))
        best = cands[0][0]
        # Quote what the model typed: the topmost launcher/server above the
        # process that listens (`npm run dev` rather than node ...\vite.js).
        shown, up = best, pool[best].get("ppid")
        while up in pool and kinds[up][0] in ("server", "launcher", "shell"):
            if server_by_command(pool[up].get("cmd")):
                shown = up
            up = pool[up].get("ppid")
        cmd = pool[shown].get("cmd") or pool[best].get("cmd")
        command = server_command(_normal_cmd(cmd)) or _one_line(cmd, 120)
        # Shown: what listens (not `npm run dev` above it). Stopped: all of it.
        listening = [pid for pid, ports in cands if ports]
        return {"command": command,
                "ports": sorted({x for _pid, ports in cands for x in ports}),
                "pids": listening or [pid for pid, _ports in cands],
                "stop": [pid for pid, _ports in cands],
                "orphans": [pid for pid, _ports in cands if pool[pid].get("orphan")],
                "source": "early",
                "leaky": bool(last_tool and leaky_launch(last_tool)
                              and starts_long_running(last_tool))}
    except Exception:                                            # noqa: BLE001
        return None


def stop_processes(pids, marker, exclude_pids=None, grace=3.0):
    """Stop the servers THIS TURN started, with their children, and return
    the PIDs still alive afterwards. A process is only touched when its
    environment carries `marker` (so a stale or reused PID, the hub's preview
    or anyone else's process never is) and it is not one of the hub's PIDs.
    Never raises."""
    pids = [p for p in (pids or []) if isinstance(p, int)]
    if not pids or not marker:
        return []
    try:
        import psutil
    except ImportError:
        return []
    protect = set(hub_pids() if exclude_pids is None else exclude_pids)

    def ours(proc):
        try:
            env = proc.environ()
            return (proc.pid not in protect and env.get(TURN_MARKER) == marker
                    and not env.get(PREVIEW_MARKER))
        except Exception:                                        # noqa: BLE001
            return False
    victims = {}
    for pid in pids:
        try:
            proc = psutil.Process(pid)
        except Exception:                                        # noqa: BLE001
            continue
        if not ours(proc):
            continue
        try:
            kids = proc.children(recursive=True)
        except Exception:                                        # noqa: BLE001
            kids = []
        for k in kids + [proc]:
            if k.pid not in victims and ours(k):
                victims[k.pid] = k
    for v in victims.values():
        try:
            v.kill()
        except Exception:                                        # noqa: BLE001
            pass
    try:
        _gone, alive = psutil.wait_procs(list(victims.values()), timeout=grace)
    except Exception:                                            # noqa: BLE001
        return []
    return sorted(v.pid for v in alive)


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
        head = "Server running on %s in the foreground (`%s`)%s" % (
            where, cmd, _pids_note(diag))
    else:
        head = "`%s` is a long-running process started in the foreground%s" % (
            cmd, _pids_note(diag))
    left = [p for p in (diag or {}).get("survivors") or [] if isinstance(p, int)]
    stopped = ("the hub stopped the blocked shell, but could not stop PID %s -- "
               "resuming with instructions to start it detached on another port"
               % ", ".join(str(p) for p in left)) if left else (
               "the hub stopped it with the blocked shell and is resuming with "
               "instructions to start it detached")
    return ("%s%s: the shell never returned, so nothing came for %ds. Not a "
            "wedge -- %s%s."
            % (head, " again" if again else "", int(stall_seconds), stopped,
               " (last retry)" if again else ""))


def _pids_note(diag):
    pids = [p for p in (diag or {}).get("pids") or [] if isinstance(p, int)]
    if not pids:
        return ""
    return ", PID %s" % ", ".join(str(p) for p in pids[:3])


def resume_instruction(diag, stall_seconds, pids=None, port=None, windows=None):
    """The prompt the resumed turn gets instead of a blind "continue"."""
    pids = hub_pids() if pids is None else list(pids)
    port = hub_port() if port is None else port
    cmd = (diag or {}).get("command") or "your server command"
    where = _ports_text((diag or {}).get("ports"))
    pid_list = [p for p in (diag or {}).get("pids") or [] if isinstance(p, int)][:3]
    pid_text = (" (PID %s)" % ", ".join(str(p) for p in pid_list)) if pid_list else ""
    left = [p for p in (diag or {}).get("survivors") or [] if isinstance(p, int)]
    if left:
        state = ("It was %s%s. The hub stopped your blocked shell but could NOT "
                 "stop PID %s, so %s may still be taken: start yours on another "
                 "port." % (("listening on " + where) if where else "running",
                            pid_text, ", ".join(str(p) for p in left),
                            where or "its port"))
    elif where:
        state = ("It was listening on %s%s, but in the foreground, so the hub had "
                 "to stop it together with your blocked shell: it is NOT running "
                 "now and there is nothing left to stop." % (where, pid_text))
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
            "your turn ends. When you start it again, keep the PID it records in "
            "%s and the port you gave it: if you must stop it later, stop it "
            "ONLY by that PID or by that port, never by searching processes by "
            "name or command line -- a search for python or app.py matches the "
            "hub too. Stop only processes you started yourself; do not "
            "kill python processes by name or by a PID from a process list -- "
            "the hub (python app.py, PID %s, port %d) runs this session."
            % (cmd, int(stall_seconds), state, leaky, ex, PID_FILE,
               _pid_text(pids), port))


def failure_detail(cli_id, diag):
    """The error when the model blocked its shell on a server every time."""
    where = _ports_text((diag or {}).get("ports"))
    return ("%s kept running `%s`%s in the foreground and its shell never "
            "returned (retried twice, with instructions to start it detached)."
            % (cli_id, (diag or {}).get("command") or "a server",
               (" (%s)" % where) if where else ""))
