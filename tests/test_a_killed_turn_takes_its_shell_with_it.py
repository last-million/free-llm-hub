r"""Stopping or restarting a turn kills the shells and servers it started.

MEASURED 2026-09-12: a review worker ran `python app.py > /dev/null 2>&1 &`
in its bash tool, the shell hung, the stall watchdog restarted the turn after
420s -- and the two bash.exe children were still running twelve minutes later
("I see a running shell"). _terminate called proc.terminate() FIRST and only
then `taskkill /T` on the same pid: the tree it was told to walk had no root
any more, and a soft taskkill never reaches a console process anyway.

Now the tree is walked children-first (psutil) while the CLI is still alive,
every pid is killed, and only then the parent. The workers and the brief are
also told not to park a server in the shell in the first place.
"""
import os
import subprocess
import sys
import time

import pytest

import agentic_chat as AC


psutil = pytest.importorskip("psutil")

# A stand-in for the CLI: a python process that spawns a grandchild sleeping
# for a minute, then sleeps itself -- the shape of `opencode -> bash -> python app.py`.
CHILD = (
    "import subprocess, sys, time\n"
    "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "print(p.pid, flush=True)\n"
    "time.sleep(60)\n"
)


def _alive(pid):
    try:
        return psutil.Process(pid).is_running() and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def test_the_grandchild_dies_with_the_turn():
    proc = subprocess.Popen([sys.executable, "-c", CHILD], stdout=subprocess.PIPE, text=True,
                            **AC._tree_popen_kwargs())
    grandchild = int(proc.stdout.readline().strip())
    assert _alive(proc.pid) and _alive(grandchild)
    AC._terminate(proc)
    deadline = time.time() + 10
    while time.time() < deadline and (_alive(proc.pid) or _alive(grandchild)):
        time.sleep(0.1)
    assert not _alive(proc.pid), "the CLI itself"
    assert not _alive(grandchild), "the shell/server it started"


def test_the_tree_is_listed_deepest_first():
    proc = subprocess.Popen([sys.executable, "-c", CHILD], stdout=subprocess.PIPE, text=True,
                            **AC._tree_popen_kwargs())
    grandchild = int(proc.stdout.readline().strip())
    try:
        pids = AC._tree_pids(proc.pid)
        assert pids[-1] == proc.pid and grandchild in pids
        assert pids.index(grandchild) < pids.index(proc.pid)
    finally:
        AC._terminate(proc)


def test_the_tree_is_signalled_before_the_parent():
    src = open("agentic_chat.py", encoding="utf-8").read()
    body = src[src.index("def _terminate(proc)"):]
    body = body[:body.index("\ndef ", 10)]
    lines = [ln.strip() for ln in body.splitlines() if not ln.strip().startswith("#")]
    assert lines.index("_signal_tree(proc.pid, hard=False)") < lines.index("proc.terminate()")


def test_workers_and_the_brief_say_not_to_park_a_server_in_the_shell():
    import swarm_windows as SW
    run = SW._Run("g", ".", "opencode", [{"title": "T", "task": "t", "needs": []}])
    assert "Do not start a server or any long-running process from the shell" \
        in SW._agent_prompt(run, run.agents[0])
    src = open("agentic_chat.py", encoding="utf-8").read()
    assert "Do not start a server or any long-running process from the shell" in src
