r"""A server blocking the agent's shell is caught after ~60 s, not 420 s.

LIVE, 2026-09-27: an opencode /agent turn ran its server in the foreground,
the shell tool never returned, and the stall watchdog waited the full 420 s
twice (26 min) before saying anything. opencode reports a command only once
it returned, so its stream never shows what is blocking; the process tree
(and, for `start /B` / a bare `&`, the ORPHAN the shell left behind) is the
only evidence.

agent_servers.early_server_diagnosis + the watchdog probe in agentic_chat:
after AGENTIC_CHAT_SERVER_PROBE (60 s) of silence, every 10 s, the watchdog
asks whether the shell tool is waiting on a server started after the CLI's
last line and on nothing else; a yes resumes at once with the server
instruction. These tests pin: detection at ~60 s instead of 420 s, no effect
on long thinking or long tool runs, the hub's own PID/port never touched,
and orphans recognised only by this turn's environment marker.
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import types

import pytest

import agent_servers as S
import agentic_chat as AC

HUB_PID = 4242
NOW = 1_700_000_000.0


# --------------------------------------------------------------------------- #
# 1. The diagnosis on fake process trees
# --------------------------------------------------------------------------- #

def _p(pid, name, cmd, ppid, ports=(), via=True, started=NOW - 40, **kw):
    d = {"pid": pid, "ppid": ppid, "name": name, "cmd": cmd, "ports": list(ports),
         "via_shell": via, "started": started}
    d.update(kw)
    return d


def _cli_and_shell():
    return [_p(11, "opencode", "opencode run --format json 'start the app'", 10,
               ports=[4096], via=False, started=NOW - 300),
            _p(12, "bash", "bash -c 'python app.py'", 11, via=False, started=NOW - 45)]


def _early(procs, orphans=(), since=NOW - 50, now=NOW, **kw):
    kw.setdefault("exclude_pids", [HUB_PID])
    kw.setdefault("exclude_ports", [8787])
    return S.early_server_diagnosis(10, since=since, now=now, processes=procs,
                                    orphans=list(orphans), **kw)


def test_a_foreground_server_listening_is_caught():
    tree = _cli_and_shell() + [_p(13, "python", "python app.py", 12, ports=[5000])]
    d = _early(tree)
    assert d["command"] == "python app.py"
    assert d["ports"] == [5000] and d["pids"] == [13] and d["orphans"] == []
    assert d["source"] == "early"


def test_a_server_command_counts_before_it_listens():
    tree = _cli_and_shell() + [_p(13, "node", "npm run dev", 12)]
    d = _early(tree)
    assert d and d["command"] == "npm run dev" and d["ports"] == []


def test_the_orphan_start_b_leaves_behind_is_caught():
    """`start /B python app.py`: cmd exits, the server is in no tree."""
    tree = _cli_and_shell()[:1]                       # the shell is gone too
    orphan = _p(77, "python", "python app.py", 555, ports=[5000], orphan=True)
    d = _early(tree, orphans=[orphan])
    assert d["pids"] == [77] and d["orphans"] == [77] and d["ports"] == [5000]


def test_too_young_a_server_is_left_alone():
    """The model may be about to curl it and stop it itself."""
    tree = _cli_and_shell() + [_p(13, "python", "python app.py", 12, ports=[5000],
                                  started=NOW - 5)]
    assert _early(tree, since=NOW - 50) is None


def test_a_server_started_before_the_last_line_is_not_blocking_anything():
    """Started detached (or Claude Code's run_in_background): the CLI printed
    the command's completion AFTER the server started, so the shell returned
    -- however long the model then thinks."""
    tree = _cli_and_shell() + [_p(13, "python", "python app.py", 12, ports=[5000],
                                  started=NOW - 200)]
    assert _early(tree, since=NOW - 150) is None
    orphan = _p(77, "python", "python app.py", 555, ports=[5000], started=NOW - 200)
    assert _early(_cli_and_shell()[:1], orphans=[orphan], since=NOW - 150) is None


@pytest.mark.parametrize("runner", [
    "python -m pytest tests -q",
    "node C:/p/node_modules/@playwright/test/cli.js test",
    "npx vitest run",
    "cargo build --release",
])
def test_a_test_run_that_starts_its_own_server_is_real_work(runner):
    tree = _cli_and_shell() + [
        _p(13, "python" if "pytest" in runner else "node", runner, 12),
        _p(14, "python", "python app.py", 13, ports=[5000]),
    ]
    assert _early(tree) is None


def test_a_listening_test_runner_itself_is_not_a_server():
    tree = _cli_and_shell() + [_p(13, "python", "python -m pytest -x", 12, ports=[61234])]
    assert _early(tree) is None


def test_a_batch_script_is_not_a_server_until_it_listens():
    batch = _cli_and_shell() + [_p(13, "python", "python main.py --epochs 3", 12)]
    assert _early(batch) is None
    serving = _cli_and_shell() + [_p(13, "python", "python main.py", 12, ports=[8000])]
    assert _early(serving)["ports"] == [8000]


def test_other_work_next_to_the_server_means_the_shell_is_busy():
    tree = _cli_and_shell() + [_p(13, "python", "python app.py", 12, ports=[5000]),
                               _p(14, "python", "python migrate.py", 12)]
    assert _early(tree) is None


def test_launchers_filters_and_the_servers_own_children_do_not_count_as_work():
    tree = _cli_and_shell() + [
        _p(13, "cmd", "cmd /c npm run dev", 12),
        _p(14, "node", r"node C:\Program Files\nodejs\node_modules\npm\bin\npm-cli.js run dev", 13),
        _p(15, "cmd", "cmd /d /s /c vite", 14),
        _p(16, "node", r"node C:\p\node_modules\vite\bin\vite.js", 15, ports=[5173]),
        _p(17, "esbuild", "esbuild --service=0.21.5 --ping", 16),       # vite's own
        _p(18, "conhost", "conhost 0xffffffff -ForceV1", 16),
        _p(19, "tee", "tee server.log", 12),
    ]
    d = _early(tree)
    assert d["ports"] == [5173] and d["pids"] == [16]
    assert sorted(d["stop"]) == [14, 16]          # npm above it goes too
    assert d["command"] == "npm run dev"


def test_the_clis_own_helpers_are_neither_work_nor_servers():
    tree = _cli_and_shell() + [
        _p(20, "cmd", "cmd /c pyright-langserver.cmd --stdio", 11),
        _p(21, "node", r"node C:\x\pyright\langserver.index.js --stdio", 20),
        _p(22, "node", "node C:/x/playwright-mcp/cli.js", 20, ports=[3845]),
    ]
    assert _early(tree) is None
    d = _early(tree + [_p(13, "python", "python app.py", 12, ports=[5000])])
    assert d["pids"] == [13]


def test_the_hub_pid_and_port_are_never_the_agents_server():
    on_hub_port = _cli_and_shell() + [_p(13, "python", "python helper.py", 12,
                                         ports=[8787])]
    assert _early(on_hub_port) is None
    hub = _cli_and_shell() + [_p(HUB_PID, "python", "python app.py", 12, ports=[5000])]
    assert _early(hub) is None
    orphan_hub = _p(HUB_PID, "python", "python app.py", 1, ports=[8787], orphan=True)
    assert _early(_cli_and_shell()[:1], orphans=[orphan_hub]) is None


def test_the_early_diagnosis_never_raises():
    for procs in ([{"pid": "x", "ports": 5, "via_shell": True, "started": "?"}],
                  [None], "garbage", [{"via_shell": True}]):
        d = S.early_server_diagnosis(None, since=0, now=NOW, processes=procs,
                                     orphans=[], exclude_pids=[], exclude_ports=[])
        assert d is None or isinstance(d, dict)


def test_the_notice_and_prompt_name_the_port_and_pid():
    diag = {"command": "python app.py", "ports": [5000], "pids": [13]}
    note = S.stall_notice(diag, 63)
    assert note.startswith("Server running on port 5000 in the foreground "
                           "(`python app.py`), PID 13")
    assert "nothing came for 63s" in note and "wedged" not in note.replace("Not a wedge", "")
    prompt = S.resume_instruction(diag, 63, pids=[HUB_PID], port=8787, windows=True)
    assert "It was listening on port 5000 (PID 13)" in prompt
    assert "nothing left to stop" in prompt
    assert "for 63s" in prompt
    lowered = prompt.lower()
    for bad in ("taskkill", "stop-process", "pkill", "tasklist", "netstat"):
        assert bad not in lowered


def test_a_server_the_hub_could_not_stop_is_reported_honestly():
    diag = {"command": "python app.py", "ports": [5000], "pids": [77], "survivors": [77]}
    prompt = S.resume_instruction(diag, 60, pids=[HUB_PID], port=8787, windows=True)
    assert "could NOT stop PID 77" in prompt and "another port" in prompt
    assert "NOT running now" not in prompt
    assert "could not stop PID 77" in S.stall_notice(diag, 60)


# --------------------------------------------------------------------------- #
# 2. Orphan scan and stop, against a fake psutil
# --------------------------------------------------------------------------- #

class _FakePsProc:
    def __init__(self, pid, env, started, name="python.exe", cmd=("python", "app.py"),
                 kids=()):
        self.pid, self._env, self._started = pid, env, started
        self.info = {"pid": pid, "ppid": 1, "name": name, "create_time": started}
        self._cmd, self._kids = list(cmd), list(kids)
        self.killed = False

    def environ(self):
        if self._env is None:
            raise PermissionError("access denied")
        return dict(self._env)

    def cmdline(self):
        return self._cmd

    def children(self, recursive=False):
        return list(self._kids)

    def kill(self):
        self.killed = True


def _fake_psutil(monkeypatch, procs):
    table = {p.pid: p for p in procs}

    class NoSuchProcess(Exception):
        pass

    def Process(pid):
        if pid not in table:
            raise NoSuchProcess(pid)
        return table[pid]

    fake = types.SimpleNamespace(
        process_iter=lambda attrs=None: list(procs), Process=Process,
        NoSuchProcess=NoSuchProcess, CONN_LISTEN="LISTEN",
        net_connections=lambda kind="inet": [types.SimpleNamespace(
            pid=77, status="LISTEN", laddr=types.SimpleNamespace(port=5000))],
        wait_procs=lambda ps, timeout=None: ([p for p in ps if p.killed],
                                             [p for p in ps if not p.killed]))
    monkeypatch.setitem(sys.modules, "psutil", fake)
    return table


def test_orphans_are_recognised_only_by_this_turns_marker(monkeypatch):
    mk = {S.TURN_MARKER: "sess/1/abc"}
    _fake_psutil(monkeypatch, [
        _FakePsProc(77, mk, NOW - 30),                                   # ours
        _FakePsProc(78, {S.TURN_MARKER: "sess/1/OTHER"}, NOW - 30),      # other turn
        _FakePsProc(79, {}, NOW - 30),                                   # someone's
        _FakePsProc(80, dict(mk, **{S.PREVIEW_MARKER: "C:/p"}), NOW - 30),  # preview
        _FakePsProc(81, mk, NOW - 500),                                  # before the line
        _FakePsProc(82, None, NOW - 30),                                 # access denied
        _FakePsProc(83, mk, NOW - 30),                                   # in the tree
    ])
    found = S.orphan_processes("sess/1/abc", NOW - 60, tree_pids={83})
    assert [p["pid"] for p in found] == [77]
    assert found[0]["ports"] == [5000] and found[0]["cmd"] == "python app.py"
    assert found[0]["orphan"] and found[0]["via_shell"]
    assert S.orphan_processes(None, NOW - 60) == []
    assert S.orphan_processes("sess/1/abc", None) == []


def test_stop_touches_only_this_turns_processes_and_never_the_hub(monkeypatch):
    mk = {S.TURN_MARKER: "sess/1/abc"}
    child = _FakePsProc(91, mk, NOW - 20)
    ours = _FakePsProc(77, mk, NOW - 30, kids=[child])
    stranger = _FakePsProc(79, {}, NOW - 30)
    hub = _FakePsProc(HUB_PID, mk, NOW - 9000)          # even carrying the marker
    _fake_psutil(monkeypatch, [ours, stranger, hub, child])
    left = S.stop_processes([77, 79, HUB_PID, 12345], "sess/1/abc",
                            exclude_pids=[HUB_PID], grace=0.1)
    assert ours.killed and child.killed
    assert not stranger.killed and not hub.killed
    assert left == []
    assert S.stop_processes([77], None) == []


# --------------------------------------------------------------------------- #
# 3. The whole turn on a fake clock: ~60 s, not 420 s
# --------------------------------------------------------------------------- #

class _Clock:
    """monotonic()/time() the watchdog reads; the scripted CLI advances it."""
    def __init__(self):
        self.t = 5000.0

    def monotonic(self):
        return self.t

    def time(self):
        return NOW + self.t

    def sleep(self, s):
        time.sleep(min(s, 0.005))

    def __getattr__(self, name):
        return getattr(time, name)


class _FakeSession:
    def __init__(self, cli_id, project_dir):
        self.id = "early-" + cli_id
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
    ev = {"type": etype, "sessionID": "ses_early"}
    if part:
        ev["part"] = part
    return json.dumps(ev) + "\n"


def _oc_tool(cmd):
    return _oc("tool_use", type="tool", tool="bash",
               state={"status": "completed", "input": {"command": cmd}})


HANG = ("pause", float("inf"))


class _ClockProc:
    """Prints its script; ("pause", s) is s seconds of silence on the fake
    clock (HANG: until the watchdog kills it)."""
    _next_pid = 81000

    def __init__(self, script, clock):
        self._it = iter(script)
        self._clock = clock
        self._killed = threading.Event()
        self.stderr = iter(())
        self.returncode = None
        self.pause_wall = None
        self.killed_at = None
        _ClockProc._next_pid += 1
        self.pid = _ClockProc._next_pid

    @property
    def stdout(self):
        return self

    def __iter__(self):
        return self

    def __next__(self):
        while True:
            try:
                item = next(self._it)
            except StopIteration:
                self.returncode = 0
                raise
            if not isinstance(item, tuple):
                return item
            start = self._clock.t
            self.pause_wall = self._clock.time()
            while self._clock.t - start < item[1]:
                if self._killed.is_set():
                    self.returncode = -9
                    raise StopIteration
                self._clock.t += 0.5
                time.sleep(0.0005)
            if self._killed.is_set():
                self.returncode = -9
                raise StopIteration

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def kill_now(self):
        if self.killed_at is None:
            self.killed_at = self._clock.t
        self._killed.set()


@pytest.fixture
def clock_turn(monkeypatch, tmp_path):
    clock = _Clock()
    monkeypatch.setattr(AC, "time", clock)
    monkeypatch.setattr(AC, "master_enabled", lambda: True)
    monkeypatch.setattr(AC, "_should_check_binary_identity", lambda s: False)
    monkeypatch.setattr(AC, "_resolve_bin", lambda cli: "/fake/" + cli)
    monkeypatch.setattr(AC.workspace, "missing_tools_message", lambda d: None)
    monkeypatch.setattr(AC, "_STALL_TIMEOUT", 420)
    monkeypatch.setattr(AC, "_TURN_TIMEOUT", 3600)
    monkeypatch.setattr(AC, "_SERVER_PROBE_AFTER", 60.0)
    monkeypatch.setattr(AC, "_SERVER_PROBE_EVERY", 10.0)
    monkeypatch.setattr(AC, "_WATCH_TICK_MAX", 0.002)
    monkeypatch.setattr(AC, "_terminate", lambda p: p.kill_now())
    monkeypatch.setattr(S, "_HUB_PIDS", [HUB_PID])
    monkeypatch.setenv("PORT", "8787")
    stops = []
    monkeypatch.setattr(S, "stop_processes",
                        lambda pids, marker, **kw: stops.append((list(pids), marker)) or [])

    def run(attempts, cli_id="opencode", text="Start the app server and tell me its port"):
        """attempts: [(script, tree_fn(proc) -> processes, orphans_fn(proc) -> list)]"""
        sess = _FakeSession(cli_id, str(tmp_path))
        AC._REGISTRY[sess.id] = sess
        rec = {"prompts": [], "procs": [], "envs": [], "stops": stops,
               "probes": [], "orphan_calls": []}
        by_pid = {}

        def fake_popen(argv, **kw):
            i = len(rec["procs"])
            script, tree_fn, orphans_fn = attempts[min(i, len(attempts) - 1)]
            p = _ClockProc(script, clock)
            by_pid[p.pid] = (p, tree_fn, orphans_fn)
            rec["prompts"].append(argv[-1])
            rec["envs"].append(dict(kw.get("env") or {}))
            rec["procs"].append(p)
            return p

        def tree(pid):
            p, tree_fn, _o = by_pid.get(pid, (None, None, None))
            rec["probes"].append(clock.t)
            return tree_fn(p) if (p and tree_fn) else []

        def orphans(marker, since, tree_pids=()):
            rec["orphan_calls"].append(marker)
            for p, _t, orphans_fn in by_pid.values():
                if orphans_fn and not p._killed.is_set():
                    return orphans_fn(p)
            return []

        monkeypatch.setattr(AC.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(S, "session_processes", tree)
        monkeypatch.setattr(S, "orphan_processes", orphans)
        try:
            events = list(AC.send_message_stream(sess.id, text))
        finally:
            AC._REGISTRY.pop(sess.id, None)
        return events, rec, sess
    return run


def _blocked_tree(port=5000, delay=2.0):
    """cli -> bash -> python app.py, started `delay` s into the silence."""
    def tree(proc):
        base = proc.pause_wall if proc.pause_wall is not None else NOW
        return [
            {"pid": 11, "ppid": proc.pid, "name": "opencode", "cmd": "opencode run",
             "ports": [4096], "via_shell": False, "started": base - 100},
            {"pid": 12, "ppid": 11, "name": "bash", "cmd": "bash -c 'python app.py'",
             "ports": [], "via_shell": False, "started": base + delay - 0.5},
            {"pid": 13, "ppid": 12, "name": "python", "cmd": "python app.py",
             "ports": [port], "via_shell": True, "started": base + delay},
        ]
    return tree


START = [_oc("step_start"), _oc_tool("ls")]
DONE = [_oc("step_start"),
        _oc_tool("nohup python app.py > server.log 2>&1 &"),
        _oc("text", type="text", text="Started detached; it listens on port 5000.")]


def _notices(events):
    return [e["text"] for e in events if e.get("event") == "notice"]


def test_a_blocked_server_is_resumed_after_about_60s_not_420(clock_turn):
    events, rec, sess = clock_turn([(START + [HANG], _blocked_tree(), None),
                                    (DONE, None, None)])
    first = rec["procs"][0]
    silent_for = first.killed_at - (first.pause_wall - NOW)
    assert 60 <= silent_for < 80, silent_for
    notices = _notices(events)
    assert len(notices) == 1
    assert notices[0].startswith("Server running on port 5000 in the foreground "
                                 "(`python app.py`), PID 13")
    assert "looks wedged" not in notices[0]
    shown = int(notices[0].split("nothing came for ")[1].split("s.")[0])
    assert 60 <= shown < 80
    assert events[-1]["event"] == "done"
    # resumed on the same thread with the instruction, port and pid
    assert len(rec["prompts"]) == 2
    assert "It was listening on port 5000 (PID 13)" in rec["prompts"][1]
    assert "Start the app server" not in rec["prompts"][1]
    # the diagnosed server is stopped -- by this attempt's own marker
    marker = rec["envs"][0][S.TURN_MARKER]
    assert rec["stops"] == [([13], marker)]
    assert rec["envs"][1][S.TURN_MARKER] != marker        # one per attempt


def test_without_the_early_probe_it_is_the_old_420s(clock_turn, monkeypatch):
    monkeypatch.setattr(AC, "_SERVER_PROBE_AFTER", 0.0)
    events, rec, _ = clock_turn([(START + [HANG], _blocked_tree(), None),
                                 (DONE, None, None)])
    first = rec["procs"][0]
    assert first.killed_at - (first.pause_wall - NOW) > 420
    assert "nothing came for 420s" in _notices(events)[0]


def test_the_orphan_is_found_and_stopped_early(clock_turn):
    def gone_shell(proc):
        return [{"pid": 11, "ppid": proc.pid, "name": "opencode", "cmd": "opencode run",
                 "ports": [], "via_shell": False, "started": NOW - 100}]

    def orphan(proc):
        return [{"pid": 77, "ppid": 5555, "name": "python", "cmd": "python app.py",
                 "ports": [5050], "via_shell": True, "orphan": True,
                 "started": proc.pause_wall + 1}]
    events, rec, _ = clock_turn([(START + [HANG], gone_shell, orphan),
                                 (DONE, None, None)])
    first = rec["procs"][0]
    assert 60 <= first.killed_at - (first.pause_wall - NOW) < 80
    assert _notices(events)[0].startswith("Server running on port 5050")
    assert rec["stops"][0][0] == [77]
    assert rec["stops"][0][1] == rec["envs"][0][S.TURN_MARKER]
    assert events[-1]["event"] == "done"


def _long_turn_unaffected(clock_turn, tree_fn, seconds=300):
    script = START + [("pause", seconds)] + [
        _oc_tool("python -m pytest -q"),
        _oc("text", type="text", text="All 212 tests pass.")]
    events, rec, _ = clock_turn([(script, tree_fn, None)])
    assert _notices(events) == []
    assert events[-1]["event"] == "done" and "212 tests" in events[-1]["text"]
    assert len(rec["procs"]) == 1 and rec["procs"][0].killed_at is None
    assert rec["stops"] == []
    return rec


def test_a_model_thinking_for_five_minutes_is_not_touched(clock_turn):
    rec = _long_turn_unaffected(clock_turn, lambda proc: [])
    assert rec["probes"], "the probe did run -- and found nothing"


def test_a_long_test_run_that_starts_its_own_server_is_not_touched(clock_turn):
    def tree(proc):
        base = proc.pause_wall
        return [
            {"pid": 12, "ppid": proc.pid, "name": "bash", "cmd": "bash -c 'pytest'",
             "ports": [], "via_shell": False, "started": base + 1},
            {"pid": 13, "ppid": 12, "name": "python", "cmd": "python -m pytest -q",
             "ports": [], "via_shell": True, "started": base + 1},
            {"pid": 14, "ppid": 13, "name": "python", "cmd": "python app.py",
             "ports": [5000], "via_shell": True, "started": base + 5},
        ]
    _long_turn_unaffected(clock_turn, tree)


def test_a_server_started_detached_before_a_long_think_is_not_touched(clock_turn):
    """Started (and reported) before the silence: Claude Code's
    run_in_background shape, or a correct nohup."""
    def tree(proc):
        return [
            {"pid": 12, "ppid": proc.pid, "name": "bash", "cmd": "bash",
             "ports": [], "via_shell": False, "started": proc.pause_wall - 5},
            {"pid": 13, "ppid": 12, "name": "python", "cmd": "python app.py",
             "ports": [5000], "via_shell": True, "started": proc.pause_wall - 4},
        ]
    _long_turn_unaffected(clock_turn, tree)


def test_the_hub_process_is_never_diagnosed_or_stopped(clock_turn):
    def tree(proc):
        base = proc.pause_wall
        return [
            {"pid": 12, "ppid": proc.pid, "name": "bash", "cmd": "bash", "ports": [],
             "via_shell": False, "started": base + 1},
            {"pid": HUB_PID, "ppid": 12, "name": "python", "cmd": "python app.py",
             "ports": [8787], "via_shell": True, "started": base + 2},
            {"pid": 13, "ppid": 12, "name": "python", "cmd": "python probe.py",
             "ports": [8787], "via_shell": True, "started": base + 2},
        ]
    _long_turn_unaffected(clock_turn, tree)


# --------------------------------------------------------------------------- #
# 4. A REAL orphan: `start /B` leaves the server outside the CLI's tree
# --------------------------------------------------------------------------- #

psutil = pytest.importorskip("psutil")

_SERVER = (
    "import socket, sys, time\n"
    "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(1)\n"
    "open(sys.argv[1], 'w').write(str(s.getsockname()[1]))\n"
    "time.sleep(60)\n"
)


@pytest.mark.skipif(os.name != "nt", reason="`start /B` is cmd.exe's")
def test_a_real_start_b_orphan_is_found_by_its_marker_and_stopped(tmp_path):
    (tmp_path / "app.py").write_text(_SERVER, encoding="utf-8")
    port_file = tmp_path / "port.txt"
    marker = "test/1/%d" % os.getpid()
    env = dict(os.environ, **{S.TURN_MARKER: marker})
    since = time.time() - 1
    # CLI stand-in -> cmd `start "" /B python app.py` -> cmd exits at once,
    # the server keeps running with nobody as its parent.
    cli = ("import subprocess, sys, time\n"
           "subprocess.call(['cmd', '/c', 'start', '', '/B', sys.executable, "
           "'app.py', 'port.txt'], cwd=%r)\n"
           "time.sleep(60)\n" % str(tmp_path))
    proc = subprocess.Popen([sys.executable, "-c", cli], env=env,
                            **AC._tree_popen_kwargs())
    server_pid = None
    try:
        deadline = time.time() + 20
        while time.time() < deadline and not (
                port_file.exists() and port_file.read_text().strip()):
            time.sleep(0.2)
        port = int(port_file.read_text().strip())
        tree = S.session_processes(proc.pid)
        assert not any(port in p["ports"] for p in tree), \
            "the orphan was expected OUTSIDE the tree: %r" % tree
        orphans = []
        while time.time() < deadline:
            orphans = S.orphan_processes(marker, since, {p["pid"] for p in tree})
            if any(port in o["ports"] for o in orphans):
                break
            time.sleep(0.3)
        server = [o for o in orphans if port in o["ports"]]
        assert server, orphans
        server_pid = server[0]["pid"]
        d = S.early_server_diagnosis(proc.pid, marker=marker, since=since,
                                     min_age=0, exclude_pids=[os.getpid()],
                                     exclude_ports=[8787])
        assert d and server_pid in d["orphans"] and port in d["ports"], d
        assert "app.py" in d["command"]
        # someone else's marker never stops it; this turn's does
        assert S.stop_processes([server_pid], "other/1/x", grace=0.5) == []
        assert psutil.pid_exists(server_pid)
        assert S.stop_processes([server_pid], marker, grace=5) == []
        assert not psutil.pid_exists(server_pid) or \
            psutil.Process(server_pid).status() == psutil.STATUS_ZOMBIE
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
    finally:
        AC._terminate(proc)
        if server_pid:
            try:
                psutil.Process(server_pid).kill()
            except Exception:
                pass
