r"""A server parked in the agent's shell is not a wedge, and the hub is not
the agent's to kill.

LIVE, 2026-09-27 (the owner's /agent session with OpenCode, project folder
project-20260907-031142, "Failed" after 26m 28s). Tool events, in order:
    bash Get-Process -Id 15316 ...
    bash tasklist | grep -i python
    [hub] Nothing for 420s — looks wedged, resuming.
    bash netstat -ano | grep ":5000"
    bash taskkill /F /PID 24656 && timeout /t 2 /nobreak >nul && start /B python app.py
    bash taskkill //F //PID 24656
    bash start /B python app.py
    [error] opencode produced nothing for 420s twice (retried once).
The model started its server in the FOREGROUND, the shell tool waited for it
forever, the watchdog resumed blindly with the user's own words, and the model
-- told nothing -- went hunting python processes (the hub is one) and blocked
the same way again.

Fixed in agent_servers + agentic_chat:
  1. the brief (every CLI, every tier) and each swarm worker's prompt carry
     the detached spellings MEASURED to return, the preview, "stop only what
     you started", and the hub's own PID and port;
  2. at a stall the process tree under the CLI (and, for CLIs that announce a
     command before running it, the last tool event) is inspected; a server
     block resumes with an explicit instruction, and the first one does not
     spend the wedge retry;
  3. the live view says "Server running on port N", not "looks wedged".
"""
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

import agent_servers as S
import agentic_chat as AC


# --------------------------------------------------------------------------- #
# 1. What the agent is told
# --------------------------------------------------------------------------- #

def test_the_brief_names_the_hub_pid_and_port(tmp_path, monkeypatch):
    monkeypatch.setenv("PORT", "8787")
    name = AC.write_task_brief(str(tmp_path), "build me a flask site")
    brief = (tmp_path / name).read_text(encoding="utf-8")
    assert "PID %d" % os.getpid() in brief
    assert "port 8787" in brief
    assert "THE HUB IS NOT YOURS" in brief
    # the old line contradicted craft.SHIP's "start it in the BACKGROUND"
    assert "(no `&`, `nohup`, `start`" not in brief


def test_the_brief_follows_the_hubs_real_port(tmp_path, monkeypatch):
    monkeypatch.setenv("PORT", "9123")
    name = AC.write_task_brief(str(tmp_path), "x")
    assert "listening on port 9123" in (tmp_path / name).read_text(encoding="utf-8")


def test_the_brief_gives_the_measured_detached_spellings_for_each_shell():
    brief = S.brief_section(pids=[4242], port=8787, windows=True)
    assert "nohup python app.py > server.log 2>&1 &" in brief
    assert ("Start-Process -WindowStyle Hidden -FilePath cmd -ArgumentList "
            "'/c','python app.py > server.log 2>&1'") in brief
    assert 'powershell -NoProfile -Command "Start-Process' in brief
    # the spellings measured to HANG a pipe-reading shell tool are named as such
    assert "Not `start /B`, not `Start-Process -NoNewWindow`" in brief
    # a check that cannot hang either
    assert "curl -s -m 5" in brief


def test_the_brief_sends_web_apps_to_the_preview():
    brief = S.brief_section(pids=[1], port=8787, windows=True)
    assert "preview panel starts this project by itself" in brief
    assert "PORT environment variable" in brief
    assert "http://127.0.0.1:PORT" in brief


def test_the_brief_forbids_killing_what_the_agent_did_not_start():
    brief = S.brief_section(pids=[1], port=8787, windows=True)
    assert "Stop only what YOU started" in brief
    assert "never kill a PID you only found in a" in brief
    assert "Never kill processes by name" in brief


def test_the_brief_lists_the_venv_launcher_too():
    """.venv\\Scripts\\python.exe is a launcher running the real interpreter
    as its child; `taskkill /T` on the launcher kills the hub as well."""
    brief = S.brief_section(pids=[200, 199], port=8787, windows=True)
    assert "PID 200 (its launcher: 199)" in brief


def test_off_windows_the_brief_does_not_offer_windows_spellings():
    brief = S.brief_section(pids=[1], port=8787, windows=False)
    assert "nohup python app.py > server.log 2>&1 &" in brief
    assert "Start-Process" not in brief


def test_swarm_workers_get_the_rules_with_the_hub_pid():
    import swarm_windows as SW
    run = SW._Run("g", ".", "opencode", [{"title": "T", "task": "t", "needs": []}])
    prompt = SW._agent_prompt(run, run.agents[0])
    assert "Never run a server or watcher in the foreground" in prompt
    assert "PID %d" % os.getpid() in prompt
    assert "port %d" % S.hub_port() in prompt
    assert "nohup CMD > server.log 2>&1 &" in prompt


def test_worker_rules_stay_short_enough_for_argv():
    """A swarm worker's prompt rides in argv; the rule must not eat it."""
    assert len(S.worker_rules(pids=[123456, 123455], port=8787, windows=True)) < 650


# --------------------------------------------------------------------------- #
# 2. Recognising a server command -- the live commands first
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("cmd", [
    "Get-Process -Id 15316 | Select-Object Id,ProcessName",
    "tasklist | grep -i python",
    'netstat -ano | grep ":5000"',
    "taskkill //F //PID 24656",
    "pip install uvicorn", "npm i -g http-server", "npx vite build", "vite build",
    "npm run build", "python test_app.py", "python -m pytest tests",
    "docker compose up -d", "curl -s -m 5 http://127.0.0.1:5000/",
])
def test_ordinary_commands_are_not_servers(cmd):
    assert not S.starts_long_running(cmd)


@pytest.mark.parametrize("cmd,server", [
    ("taskkill /F /PID 24656 && timeout /t 2 /nobreak >nul && start /B python app.py",
     "python app.py"),
    ("start /B python app.py", "python app.py"),
    ("python app.py", "python app.py"),
    ("Bash: python -u server.py", "python -u server.py"),
    ("cd web && npm start", "npm start"),
    ("uvicorn main:app --reload", "uvicorn main:app --reload"),
    ("python -m http.server 8000", "python -m http.server 8000"),
    ('"C:\\WINDOWS\\powershell.exe" -Command \'python app.py\'', "python app.py"),
    ('start "" /B cmd /c "python app.py > server.log 2>&1"', "python app.py"),
    ("Start-Process -NoNewWindow -FilePath python -ArgumentList app.py", "python app.py"),
    ("nohup python app.py > server.log 2>&1 &", "python app.py"),
    ("npx next dev", "npx next dev"), ("tsc -w", "tsc -w"),
])
def test_server_commands_are_recognised_and_extracted(cmd, server):
    assert S.starts_long_running(cmd)
    assert S.server_command(cmd) == server


def test_the_launchers_measured_to_hang_are_flagged():
    assert S.leaky_launch("start /B python app.py")
    assert S.leaky_launch('start "" /B cmd /c "python app.py > l 2>&1"')
    assert S.leaky_launch("Start-Process -NoNewWindow -FilePath python")
    assert not S.leaky_launch("nohup python app.py > server.log 2>&1 &")


# --------------------------------------------------------------------------- #
# 3. The stall diagnosis, on fake process trees
# --------------------------------------------------------------------------- #

def _server_tree(port=5000, cmd="python app.py"):
    return [
        {"pid": 11, "name": "opencode", "cmd": "opencode run --format json 'python app.py'",
         "ports": [4096], "via_shell": False},            # the CLI itself
        {"pid": 12, "name": "bash", "cmd": "bash -c 'python app.py'", "ports": [],
         "via_shell": False},                             # its shell tool
        {"pid": 13, "name": "python", "cmd": cmd, "ports": [port], "via_shell": True},
    ]


def test_a_listening_child_of_the_shell_is_the_server():
    d = S.diagnose_stall(10, cli_id="opencode", last_tool="bash tasklist | grep -i python",
                         tool_was_last=True, exclude_pids=[999], exclude_ports=[8787],
                         processes=_server_tree())
    assert d == {"command": "python app.py", "ports": [5000], "source": "process",
                 "leaky": False, "pids": [13]}


def test_the_cli_itself_never_counts():
    """Its command line carries the user's prompt, and opencode's `run`
    listens on a port of its own: neither is the agent's server."""
    tree = _server_tree()[:2]
    assert S.diagnose_stall(10, cli_id="opencode", processes=tree,
                            exclude_pids=[], exclude_ports=[8787]) is None


def test_the_hub_port_and_pids_are_never_the_agents_server():
    tree = [{"pid": 13, "name": "python", "cmd": "python helper.py", "ports": [8787],
             "via_shell": True}]
    assert S.diagnose_stall(10, processes=tree, exclude_pids=[],
                            exclude_ports=[8787]) is None
    tree = [{"pid": 999, "name": "python", "cmd": "python app.py", "ports": [5000],
             "via_shell": True}]
    assert S.diagnose_stall(10, processes=tree, exclude_pids=[999],
                            exclude_ports=[8787]) is None


def test_an_mcp_server_under_a_cmd_shim_is_not_the_agents_server():
    tree = [{"pid": 13, "name": "node", "cmd": "node C:/x/mcp-server.js", "ports": [3845],
             "via_shell": True}]
    assert S.diagnose_stall(10, processes=tree, exclude_pids=[],
                            exclude_ports=[8787]) is None


def test_a_watcher_that_does_not_listen_still_counts():
    tree = [{"pid": 13, "name": "node", "cmd": "npm run dev", "ports": [],
             "via_shell": True}]
    d = S.diagnose_stall(10, processes=tree, exclude_pids=[], exclude_ports=[8787])
    assert d["command"] == "npm run dev" and d["ports"] == []


def test_codex_and_claude_announce_the_command_before_running_it():
    for cli, text in (("codex", "python app.py"), ("claude", "Bash: npm run dev")):
        d = S.diagnose_stall(10, cli_id=cli, last_tool=text, tool_was_last=True,
                             processes=[], exclude_pids=[], exclude_ports=[8787])
        assert d and d["source"] == "tool"


def test_a_tool_event_that_is_not_the_last_line_proves_nothing():
    assert S.diagnose_stall(10, cli_id="codex", last_tool="python app.py",
                            tool_was_last=False, processes=[], exclude_pids=[],
                            exclude_ports=[8787]) is None


def test_opencode_tool_events_arrive_after_the_command_so_they_prove_nothing():
    """opencode's `run --format json` emits tool_use only once a tool part is
    completed or errored: `start /B python app.py` in its stream had already
    returned. Only the process tree can say its shell is blocked."""
    assert "opencode" not in S.TOOL_EVENT_AT_START
    assert S.diagnose_stall(10, cli_id="opencode", last_tool="bash start /B python app.py",
                            tool_was_last=True, processes=[], exclude_pids=[],
                            exclude_ports=[8787]) is None


def test_the_diagnosis_never_raises():
    """It runs on the watchdog thread, which must still kill the process."""
    for procs in ([{"pid": "x", "ports": 5, "via_shell": True}], [None], "garbage"):
        d = S.diagnose_stall(None, processes=procs, exclude_pids=[], exclude_ports=[])
        assert d is None or isinstance(d, dict)
    assert S.diagnose_stall(-1, exclude_pids=[], exclude_ports=[]) is None


# --------------------------------------------------------------------------- #
# 4. The words: live view and resume prompt
# --------------------------------------------------------------------------- #

def test_the_live_view_says_server_running_not_wedged():
    note = S.stall_notice({"command": "python app.py", "ports": [5000]}, 420)
    assert note.startswith("Server running on port 5000")
    assert "wedged" not in note.replace("Not a wedge", "")
    # no URL: the dashboard adopts any local URL it sees, once per port,
    # and this server is already stopped
    assert "http://" not in note


def test_the_resume_prompt_explains_and_never_suggests_killing():
    note = S.resume_instruction({"command": "python app.py", "ports": [5000],
                                 "leaky": True}, 420, pids=[4242], port=8787,
                                windows=True)
    assert "`python app.py` starts a server that never returns" in note
    assert "It was listening on port 5000" in note
    assert "it is NOT running now" in note
    assert "nohup python app.py > server.log 2>&1 &" in note
    assert "`start /B` and `Start-Process -NoNewWindow` still hold" in note
    assert "continue the task where you left off" in note
    assert "PID 4242, port 8787" in note
    lowered = note.lower()
    for bad in ("taskkill", "stop-process", "pkill", "killall", "tasklist", "netstat"):
        assert bad not in lowered
    assert "do not kill python processes" in lowered


def test_the_resume_prompt_says_when_nothing_was_listening():
    note = S.resume_instruction({"command": "npm run dev", "ports": []}, 420,
                                pids=[1], port=8787, windows=False)
    assert "not listening on any port yet" in note
    assert "nohup npm run dev > server.log 2>&1 &" in note


# --------------------------------------------------------------------------- #
# 5. The whole turn, replaying the live sequence with fakes
# --------------------------------------------------------------------------- #

class _FakeSession:
    def __init__(self, cli_id, project_dir):
        self.cli_id = cli_id
        self.project_dir = project_dir
        self.native_session_id = None
        self.turn_count = 0
        self.proc = None
        self.proc_lock = threading.Lock()
        self.turn_lock = threading.Lock()
        self.last_interrupted = False
        self.tools_notified = True


def _oc(etype, **part):
    import json
    ev = {"type": etype, "sessionID": "ses_live"}
    if part:
        ev["part"] = part
    return json.dumps(ev) + "\n"


def _oc_tool(cmd):
    return _oc("tool_use", type="tool", tool="bash",
               state={"status": "completed", "input": {"command": cmd}})


# Attempt 1: what the live turn printed before it went silent.
LIVE_1 = [_oc("step_start"),
          _oc_tool("Get-Process -Id 15316 | Select-Object Id,ProcessName"),
          _oc_tool("tasklist | grep -i python")]
# Attempt 2: what it printed after the blind resume, then silent again.
LIVE_2 = [_oc("step_start"),
          _oc_tool('netstat -ano | grep ":5000"'),
          _oc_tool("taskkill /F /PID 24656 && timeout /t 2 /nobreak >nul && "
                   "start /B python app.py"),
          _oc_tool("taskkill //F //PID 24656"),
          _oc_tool("start /B python app.py")]
DONE = [_oc("step_start"),
        _oc_tool("nohup python app.py > server.log 2>&1 &"),
        _oc("text", type="text", text="Server started detached on port 5000; done.")]


class _ScriptedProc:
    """Prints its lines; then, when `hang`, blocks like a CLI whose shell tool
    waits on a foreground server -- until the watchdog's _terminate."""
    _next_pid = 70000

    def __init__(self, lines, hang):
        self._lines = iter(lines)
        self._hang = hang
        self._killed = threading.Event()
        self.stderr = iter(())
        self.returncode = None
        _ScriptedProc._next_pid += 1
        self.pid = _ScriptedProc._next_pid

    @property
    def stdout(self):
        return self

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self._lines)
        except StopIteration:
            if self._hang:
                self._killed.wait(timeout=10)
            self.returncode = -9 if self._hang else 0
            raise StopIteration

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def kill_now(self):
        self._killed.set()


@pytest.fixture
def turn(monkeypatch, tmp_path):
    """Run one streamed opencode turn against scripted attempts. Each attempt
    is (lines, hang, process_tree_at_stall)."""
    monkeypatch.setattr(AC, "master_enabled", lambda: True)
    monkeypatch.setattr(AC, "_should_check_binary_identity", lambda s: False)
    monkeypatch.setattr(AC, "_resolve_bin", lambda cli: "/fake/" + cli)
    monkeypatch.setattr(AC.workspace, "missing_tools_message", lambda d: None)
    monkeypatch.setattr(AC, "_STALL_TIMEOUT", 0.3)
    monkeypatch.setattr(AC, "_TURN_TIMEOUT", 600)
    monkeypatch.setattr(AC, "_terminate", lambda p: p.kill_now())
    monkeypatch.setattr(S, "_HUB_PIDS", [4242])
    monkeypatch.setenv("PORT", "8787")

    def run(attempts, text="build my flask app and run it"):
        sess = _FakeSession("opencode", str(tmp_path))
        sid = "servers-" + str(id(sess))
        AC._REGISTRY[sid] = sess
        prompts, procs, trees = [], [], {}

        def fake_popen(argv, **kw):
            i = len(procs)
            lines, hang, tree = attempts[min(i, len(attempts) - 1)]
            prompts.append(argv[-1])
            p = _ScriptedProc(lines, hang)
            trees[p.pid] = tree
            procs.append(p)
            return p

        monkeypatch.setattr(AC.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(S, "session_processes", lambda pid: trees.get(pid) or [])
        try:
            events = list(AC.send_message_stream(sid, text))
        finally:
            AC._REGISTRY.pop(sid, None)
        return events, prompts, sess
    return run


def _notices(events):
    return [e["text"] for e in events if e.get("event") == "notice"]


def test_the_live_sequence_now_resumes_with_the_reason_and_finishes(turn):
    events, prompts, sess = turn([(LIVE_1, True, _server_tree()),
                                  (LIVE_2, True, _server_tree()),
                                  (DONE, False, [])])
    notices = _notices(events)
    assert notices[0].startswith("Server running on port 5000 in the foreground "
                                 "(`python app.py`)")
    assert not any("looks wedged" in n for n in notices)
    assert "again" in notices[1] and "(last retry)" in notices[1]
    assert events[-1]["event"] == "done"
    assert "detached" in events[-1]["text"]
    # attempts 2 and 3 RESUME the same thread with the instruction, not the
    # user's words again (the blind resume is what left the model guessing)
    assert len(prompts) == 3
    for p in prompts[1:]:
        assert "starts a server that never returns" in p
        assert "It was listening on port 5000" in p
        assert "PID 4242, port 8787" in p
        assert "build my flask app" not in p
        assert "taskkill" not in p.lower()
    assert sess.native_session_id == "ses_live"


def test_the_first_server_stall_does_not_spend_the_wedge_retry(turn):
    """Server stall, then an ordinary wedge, then success: before, the second
    silence was the end of the turn."""
    events, prompts, _ = turn([(LIVE_1, True, _server_tree()),
                               ([_oc("step_start")], True, []),
                               (DONE, False, [])])
    notices = _notices(events)
    assert notices[0].startswith("Server running on port 5000")
    assert "looks wedged" in notices[1]
    assert events[-1]["event"] == "done"
    # the generic resume sends the request itself, not the server note
    assert prompts[2].startswith("build my flask app")
    assert "starts a server" not in prompts[2]


def test_a_model_that_keeps_blocking_its_shell_gets_a_clear_error(turn):
    events, prompts, _ = turn([(LIVE_1, True, _server_tree())] * 4)
    assert len(prompts) == 3, "one free server resume + the wedge retry, then stop"
    err = events[-1]
    assert err["event"] == "error" and err["status"] == 504
    assert "kept running `python app.py` (port 5000) in the foreground" in err["detail"]


def test_an_ordinary_wedge_behaves_exactly_as_before(turn):
    events, prompts, _ = turn([([_oc("step_start")], True, [])] * 3)
    assert any("looks wedged" in n for n in _notices(events))
    assert len(prompts) == 2
    assert events[-1]["event"] == "error"
    assert "produced nothing for" in events[-1]["detail"]
    assert "retried once" in events[-1]["detail"]
    assert prompts[1].startswith("build my flask app")


def test_the_diagnosis_runs_before_the_kill(monkeypatch, turn):
    """The tree is only walkable while the CLI is alive to be its root."""
    order = []
    real = S.diagnose_stall

    def spy(*a, **kw):
        order.append("diagnose")
        return real(*a, **kw)
    monkeypatch.setattr(S, "diagnose_stall", spy)
    monkeypatch.setattr(AC, "_terminate", lambda p: (order.append("kill"), p.kill_now()))
    turn([(LIVE_1, True, _server_tree()), (DONE, False, [])])
    assert order[:2] == ["diagnose", "kill"]


# --------------------------------------------------------------------------- #
# 6. The process scan against a REAL tree: CLI stand-in -> shell -> server
# --------------------------------------------------------------------------- #

psutil = pytest.importorskip("psutil")

_SERVER = (
    "import socket, sys, time\n"
    "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(1)\n"
    "open(sys.argv[1], 'w').write(str(s.getsockname()[1]))\n"
    "time.sleep(60)\n"
)


def test_the_scan_finds_a_server_started_through_a_shell(tmp_path):
    (tmp_path / "app.py").write_text(_SERVER, encoding="utf-8")
    port_file = tmp_path / "port.txt"
    # The CLI stand-in runs its "shell tool": a real shell that runs the
    # server in the foreground -- the live shape, cli -> shell -> server.
    if os.name == "nt":
        shell = ["cmd", "/c", sys.executable, "app.py", "port.txt"]
    else:
        shell = ["sh", "-c", '"$0" app.py port.txt', sys.executable]
    cli = ("import subprocess, time\n"
           "subprocess.Popen(%r, cwd=%r)\n"
           "time.sleep(60)\n" % (shell, str(tmp_path)))
    proc = subprocess.Popen([sys.executable, "-c", cli], **AC._tree_popen_kwargs())
    try:
        deadline = time.time() + 20
        port = None
        while time.time() < deadline:
            if port_file.exists() and port_file.read_text().strip():
                port = int(port_file.read_text().strip())
                found = [p for p in S.session_processes(proc.pid) if port in p["ports"]]
                if found:
                    break
            time.sleep(0.2)
        assert port, "the stand-in server never bound"
        found = [p for p in S.session_processes(proc.pid) if port in p["ports"]]
        assert found and found[0]["via_shell"], S.session_processes(proc.pid)
        d = S.diagnose_stall(proc.pid, cli_id="opencode", exclude_pids=[os.getpid()],
                             exclude_ports=[8787])
        assert d and port in d["ports"] and d["source"] == "process"
        assert "app.py" in d["command"]
    finally:
        AC._terminate(proc)
