"""At boot, agent CLIs the previous hub launched are stopped.

MEASURED 2026-09-30: a restart kills the hub but not its children on Windows;
five opencode workers of an earlier run kept calling the new hub, spending
quota, holding the top models (a new run's helper was spread onto a weaker
one) and editing the same project as the new run's helper.
"""
import types

import agent_servers as S


class _P:
    def __init__(self, pid, name, env, cmd):
        self.info = {"pid": pid, "name": name}
        self.pid = pid
        self._env, self._cmd = env, cmd

    def environ(self):
        return self._env

    def cmdline(self):
        return self._cmd


def _fake_psutil(monkeypatch, procs):
    fake = types.SimpleNamespace(process_iter=lambda attrs=None: iter(procs))
    monkeypatch.setitem(__import__("sys").modules, "psutil", fake)


def test_only_marked_agent_clis_count(monkeypatch):
    M = {S.TURN_MARKER: "sid/1/abc"}
    _fake_psutil(monkeypatch, [
        _P(1, "opencode.exe", M, ["opencode", "run", "--auto"]),
        _P(2, "node.exe", M, ["node", "C:/npm/codex/bin/codex.js", "exec"]),
        _P(3, "python.exe", M, ["python", "app.py"]),                 # the owner's app an agent started
        _P(4, "opencode.exe", {}, ["opencode"]),                        # the owner's own terminal CLI
        _P(5, "node.exe", dict(M, **{S.PREVIEW_MARKER: "1"}), ["node", "claude"]),  # a preview
        _P(6, "claude.exe", M, ["claude", "-p"]),
    ])
    got = [p["pid"] for p in S.stale_agent_clis(exclude_pids=[])]
    assert got == [1, 2, 6]


def test_the_hub_itself_is_never_a_victim(monkeypatch):
    M = {S.TURN_MARKER: "x"}
    _fake_psutil(monkeypatch, [_P(7, "opencode.exe", M, ["opencode"])])
    assert S.stale_agent_clis(exclude_pids=[7]) == []


def test_the_boot_calls_it_before_any_turn():
    src = open("app.py", encoding="utf-8").read()
    boot = src[src.index("    _mark_runtime_started()\n"):][:900]
    assert "agent_servers.stop_stale_agent_clis()" in boot


def test_the_boot_can_reach_it():
    # MEASURED 2026-10-03, hub.log: "[boot] could not check for leftover
    # agent CLIs: name 'agent_servers' is not defined" -- the call above sat in
    # app.py with no import of the module, so the boot pass never ran.
    import app
    assert app.agent_servers.stop_stale_agent_clis is S.stop_stale_agent_clis
