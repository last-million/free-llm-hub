r"""Server safety in /agent turns, and where the brief is read from.

1. PID / PORT ONLY. Live 2026-09-28 (opencode log, run 1476d09d): an agent
   found "its" server with
       Get-Process | Where-Object {$_.ProcessName -like "*python*" -and
                                   $_.CommandLine -like "*app.py*"} | Select-Object Id
   and ran `Stop-Process -Id 1488` on the result. The hub is `python app.py`
   too, so that filter matches it. Every text the agent gets now says: record
   the PID when you start a server, stop it only by that PID or by its port,
   never by a name / command-line search. The resume prompt (early ~60 s and
   420 s stall alike -- both use resume_instruction) carries the guard but still
   names no kill command.

2. THE BRIEF'S ABSOLUTE PATH. Same run: opencode's instance directory, its
   session directory and PWD were all the session's temp project folder, yet
   the model read C:\Users\hamza\Desktop\Projects\opencode-evals\.calvoun-brief-
   <id>.md -- a folder that does not exist. The pointer named a bare file and
   "this folder"; it now names the absolute path in the project folder.

Fakes only: no CLI, no process, no network.
"""
import json
import os
import threading

import pytest

import agent_servers as S
import agentic_chat as AC
import config


HUB = [4242]


# --------------------------------------------------------------------------- #
# 1. What the agent is told about stopping servers
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("windows", [True, False])
def test_every_detached_spelling_records_the_pid(windows):
    for shell, ex in S.detach_examples("python app.py", windows=windows):
        assert S.PID_FILE in ex, (shell, ex)
        assert "server.log" in ex
    ex = dict(S.detach_examples("python app.py", windows=True))
    assert ex["bash"] == "nohup python app.py > server.log 2>&1 & echo $! > server.pid"
    assert "-PassThru | Select-Object -ExpandProperty Id | Set-Content server.pid" in ex["PowerShell"]
    assert ex["cmd"].startswith('powershell -NoProfile -Command "Start-Process')
    # still the launch measured to return: no -NoNewWindow / -Redirect*
    for e in ex.values():
        assert not S.leaky_launch(e), e


def test_stop_spellings_are_pid_or_port_only():
    joined = " ".join(e for _h, e in S.stop_examples(windows=True)).lower()
    assert "taskkill /pid" in joined and "/t /f" in joined
    assert "get-nettcpconnection -localport" in joined
    assert "netstat -ano | findstr listening | findstr :port" in joined
    for bad in ("/im", "-name", "findstr python", "app.py", "pkill", "get-process",
                "tasklist", "wmic", "commandline"):
        assert bad not in joined, bad


def test_the_brief_says_record_the_pid_and_stop_by_pid_or_port():
    brief = S.brief_section(pids=HUB, port=8787, windows=True)
    assert "RECORD THE PID of every server you start" in brief
    assert "by that recorded PID, or by the port you started it on" in brief
    assert "`taskkill /PID <pid from server.pid> /T /F`" in brief
    assert "Get-NetTCPConnection -LocalPort PORT -State Listen" in brief
    # measured: Stop-Process on the recorded (cmd.exe) PID leaves the server up
    assert "`Stop-Process -Id` on it leaves the server itself running" in brief
    # a bash $! is not a Windows PID
    assert "never pass it to `taskkill`" in brief
    assert "check the port is free" in brief
    assert "one of the hub's PIDs below, it is not your server" in brief


def test_the_brief_forbids_name_and_command_line_searches_and_says_why():
    brief = S.brief_section(pids=HUB, port=8787, windows=True)
    assert "NEVER find a process to stop by its name or its command line" in brief
    for shape in ("Get-Process | Where-Object ... app.py", "tasklist | findstr python",
                  "Get-CimInstance Win32_Process", "pkill -f app.py"):
        assert shape in brief, shape
    assert "is itself a `python app.py`, so every such search matches the hub too" in brief
    assert "PID 4242, listening on port 8787" in brief


def test_off_windows_the_brief_stops_by_pid_or_port_too():
    brief = S.brief_section(pids=HUB, port=8787, windows=False)
    assert "kill $(cat server.pid)" in brief
    assert "lsof -t -iTCP:PORT -sTCP:LISTEN" in brief
    assert "taskkill" not in brief.split("NEVER find")[0]
    assert "Get-NetTCPConnection" not in brief


@pytest.mark.parametrize("diag", [
    {"command": "python app.py", "ports": [5000], "pids": [13]},          # early probe
    {"command": "python app.py", "ports": [5000], "leaky": True},         # 420 s stall
    {"command": "npm run dev", "ports": [], "survivors": [77], "pids": [77]},
])
def test_the_resume_prompt_carries_the_guard_without_naming_a_kill_command(diag):
    note = S.resume_instruction(diag, 63, pids=HUB, port=8787, windows=True)
    assert "keep the PID it records in server.pid" in note
    assert "stop it ONLY by that PID or by that port" in note
    assert "never by searching processes by name or command line" in note
    assert "a search for python or app.py matches the hub too" in note
    assert "PID 4242, port 8787" in note
    # the relaunch it is told to do records the PID
    assert "& echo $! > server.pid" in note
    lowered = note.lower()
    for bad in ("taskkill", "stop-process", "pkill", "killall", "tasklist", "netstat",
                "get-process", "wmic"):
        assert bad not in lowered, bad


def test_swarm_workers_get_the_pid_rule():
    rules = S.worker_rules(pids=HUB, port=8787, windows=True)
    assert "PID recorded" in rules and "echo $! > server.pid" in rules
    assert "by that PID or its port" in rules
    assert "never by a name or command-line search" in rules
    assert "python app.py, PID 4242, port 8787" in rules


def test_the_written_brief_carries_the_rule(tmp_path):
    name = AC.write_task_brief(str(tmp_path), "build me a flask site")
    brief = (tmp_path / name).read_text(encoding="utf-8")
    assert "RECORD THE PID" in brief
    assert "NEVER find a process to stop by its name or its command line" in brief


# --------------------------------------------------------------------------- #
# 2. The pointer names the brief's absolute path in the project folder
# --------------------------------------------------------------------------- #

def test_the_pointer_names_the_absolute_path(tmp_path):
    name = ".calvoun-brief-abcdef123456.md"
    add = AC._system_prompt_addition("build a site", has_brief=name,
                                     project_dir=str(tmp_path))
    assert os.path.join(os.path.abspath(str(tmp_path)), name) in add
    assert "This folder contains" not in add
    assert "established" in add


def test_without_a_folder_the_pointer_says_working_directory():
    add = AC._system_prompt_addition("build a site", has_brief=True)
    assert AC.BRIEF_FILENAME + " in your working directory" in add


def _direct(monkeypatch):
    monkeypatch.setattr(AC, "_launcher", lambda b: [b])


def _via_cmd(monkeypatch):
    monkeypatch.setattr(AC, "_launcher", lambda b: ["cmd.exe", "/c", b])


@pytest.mark.parametrize("cli,build", [
    ("claude", AC._build_argv), ("codex", AC._build_argv_codex),
    ("opencode", AC._build_argv_opencode)])
def test_each_cli_gets_the_absolute_path_of_the_file_it_wrote(cli, build, tmp_path,
                                                             monkeypatch):
    _direct(monkeypatch)
    proj = tmp_path / "temp project"
    proj.mkdir()
    sess = AC._Session(cli, str(proj))
    sess.native_session_id = None
    argv = build(sess, "/fake/" + cli, "build me a landing page website")
    blob = "\n".join(argv)
    name = AC.brief_filename(sess.id)
    path = os.path.join(os.path.abspath(str(proj)), name)
    assert path in blob, blob[-600:]
    assert os.path.isfile(path)             # the file IS where the pointer says


def test_on_the_cmd_fallback_a_short_message_still_gets_the_absolute_path(tmp_path,
                                                                        monkeypatch):
    _via_cmd(monkeypatch)
    sess = AC._Session("opencode", str(tmp_path))
    sess.native_session_id = None
    argv = AC._build_argv_opencode(sess, r"C:\npm\opencode.cmd", "build a site")
    assert os.path.join(os.path.abspath(str(tmp_path)), AC.brief_filename(sess.id)) \
        in argv[-1]


def test_on_the_cmd_fallback_a_message_at_the_cap_keeps_the_old_budget(tmp_path,
                                                                     monkeypatch):
    """The cmd.exe command line has ~120 characters to spare at the cap; an
    absolute path can be 260. There the bare name ships, as before."""
    _via_cmd(monkeypatch)
    monkeypatch.setattr(AC.vision_status, "status",
                        lambda: {"available": False, "providers": []})
    monkeypatch.setattr(AC, "test_verification_enabled", lambda: True)
    deep = tmp_path / ("d" * 60) / ("e" * 60)
    deep.mkdir(parents=True)
    text = ("build me a landing page website " + "x" * AC._MAX_MESSAGE_CHARS
            )[:AC._MAX_MESSAGE_CHARS]
    long_bin = r"C:\Users\somewhat-long-username\AppData\Roaming\npm\claude.cmd"
    for cli, build in (("claude", AC._build_argv), ("codex", AC._build_argv_codex),
                       ("opencode", AC._build_argv_opencode)):
        sess = AC._Session(cli, str(deep))
        sess.native_session_id = None
        argv = build(sess, long_bin, text)
        cost = sum(len(a) + 3 for a in argv)
        assert cost < 8191, (cli, cost)
        assert "in your working directory" in "\n".join(argv)


# --------------------------------------------------------------------------- #
# 3. A whole opencode turn, faked: cwd, PWD and the pointer all agree
# --------------------------------------------------------------------------- #

class _Proc:
    def __init__(self, stdout):
        self.pid = 4243
        self.returncode = 0
        self._out = stdout
        self._done = threading.Event()
        self._done.set()

    def communicate(self, timeout=None):
        return self._out, ""

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


@pytest.fixture
def opencode_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(tmp_path / "state" / "config.json"))
    AC._REGISTRY.clear()
    AC._recent_projects.clear()
    monkeypatch.setattr(AC.subprocess, "run", lambda *a, **kw: None)
    monkeypatch.setattr(AC.shutil, "which", lambda name: "/usr/bin/" + name)
    _direct(monkeypatch)
    # the hub's own PWD is its repo (run.bat cd's there): a stale one is the
    # classic way opencode lands in the wrong project
    monkeypatch.setenv("PWD", r"C:\Users\hamza\Desktop\Projects\opencode-evals")
    config.set_flag("agentic_chat_enabled", True)
    yield tmp_path
    AC._REGISTRY.clear()
    AC._recent_projects.clear()


def test_an_opencode_turn_in_a_temp_folder_reads_its_brief_there(opencode_turn,
                                                                  monkeypatch):
    proj = opencode_turn / "live-opencode-001034"
    proj.mkdir()
    sid = AC.start_session("opencode", str(proj))
    seen = {}

    def fake_popen(argv, **kw):
        seen["argv"], seen["cwd"], seen["env"] = argv, kw.get("cwd"), kw.get("env") or {}
        out = "\n".join(json.dumps(e) for e in (
            {"type": "step_start", "sessionID": "ses_x", "part": {}},
            {"type": "text", "sessionID": "ses_x", "part": {"text": "Done."}}))
        return _Proc(out)

    monkeypatch.setattr(AC.subprocess, "Popen", fake_popen)
    status, text, _detail = AC.send_message(sid, "build me a landing page website")
    assert status == 200, (status, text, _detail)
    assert seen["cwd"] == str(proj)
    assert seen["env"].get("PWD") == str(proj)
    assert "opencode-evals" not in "\n".join(seen["argv"])
    brief = os.path.join(os.path.abspath(str(proj)), AC.brief_filename(sid))
    assert brief in seen["argv"][-1]
    assert os.path.isfile(brief)


def test_brief_gives_the_msys_taskkill_spelling():
    # Live 2026-09-28 (opencode, bash tool): `taskkill /PID 3248 /T /F` failed
    # twice -- MSYS turned /PID into a path -- and `//PID` worked on try three.
    import agent_servers
    text = agent_servers.brief_section(pids=[1], port=8787, windows=True)
    assert "taskkill //PID <pid> //T //F" in text
