"""A project's setup commands always end (2026-10-04 audit).

workspace._run_blocking ran `npm install` / `python -m venv` / `pip install -r`
with NO time limit, so one hung install (a registry that stops answering, a
postinstall waiting on a prompt nobody sees) left the preview on "installing
dependencies..." forever. Now every step has a limit (INSTALL_TIMEOUT for the
downloads, SETUP_TIMEOUT for venv creation); past it the WHOLE process tree is
killed, what it printed so far stays in the log, and the preview shows
"<step> did not finish in N s" instead of hanging.

The kill test uses a real child that spawns a real grandchild sharing its
output pipe -- the exact shape of `npm.cmd -> node` -- because killing only the
top process leaves the grandchild running AND holding the pipe open, which is
the hang this fixes.
"""
import os
import shutil
import sys
import tempfile
import threading
import time

import pytest

import workspace

psutil = pytest.importorskip("psutil")


@pytest.fixture
def proj():
    """Own temp dir, not pytest's `tmp_path` (its factory raises
    PermissionError on this machine -- see tests/test_workspace.py)."""
    d = tempfile.mkdtemp(prefix="hubinst-")
    try:
        yield d
    finally:
        workspace.stop(d)
        shutil.rmtree(d, ignore_errors=True)


class _Log:
    def __init__(self):
        self.lines = []
        self._lock = threading.Lock()

    def __call__(self, line):
        with self._lock:
            self.lines.append(line)

    def snapshot(self):
        with self._lock:
            return list(self.lines)


# The grandchild prints its own pid and sleeps; the child prints its pid,
# starts the grandchild on the SAME stdout, and sleeps too. Neither ever exits
# by itself within the test.
_GRANDCHILD = ("import os, time; print('GRANDCHILD', os.getpid(), flush=True); "
               "time.sleep(120)")
_CHILD = ("import os, subprocess, sys, time; "
          "print('CHILD', os.getpid(), flush=True); "
          "subprocess.Popen([sys.executable, '-c', %r]); "
          "time.sleep(120)" % _GRANDCHILD)


def _pid_from(lines, tag):
    for line in lines:
        parts = line.split()
        if len(parts) == 2 and parts[0] == tag and parts[1].isdigit():
            return int(parts[1])
    return None


def _gone(pid, within=3.0):
    end = time.time() + within
    while time.time() < end:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.1)
    return False


def _force_kill(*pids):
    for pid in pids:
        if not pid:
            continue
        try:
            psutil.Process(pid).kill()
        except Exception:                                        # noqa: BLE001
            pass


def test_a_hung_install_is_killed_with_its_whole_tree(proj):
    log = _Log()
    timeout = 3.0
    t0 = time.monotonic()
    reason = workspace._run_blocking([sys.executable, "-c", _CHILD], proj, log,
                                     timeout=timeout, label="npm install")
    elapsed = time.monotonic() - t0
    lines = log.snapshot()
    child, grandchild = _pid_from(lines, "CHILD"), _pid_from(lines, "GRANDCHILD")
    try:
        assert elapsed < timeout + 3.0, "took %.1fs -- the kill did not end it" % elapsed
        assert reason == "npm install did not finish in 3 s"
        # What it printed BEFORE the kill is in the log (that is how we know
        # both pids), followed by the hub's own line saying what happened.
        assert child and grandchild, lines
        assert any("did not finish in 3 s" in l and l.startswith("[hub]") for l in lines)
        assert _gone(child), "the hung child is still running"
        assert _gone(grandchild), (
            "the grandchild survived: only the top process was killed, which "
            "is the npm.cmd -> node orphan")
    finally:
        _force_kill(child, grandchild)


def test_a_fast_command_still_succeeds_unchanged(proj):
    log = _Log()
    argv = [sys.executable, "-c", "print('one'); print('two')"]
    assert workspace._run_blocking(argv, proj, log, timeout=30,
                                   label="pip install -r requirements.txt") is None
    assert log.snapshot() == ["one", "two"], "no extra lines on a normal run"
    # The old signature (no timeout) still works the same way.
    log2 = _Log()
    assert workspace._run_blocking(argv, proj, log2) is None
    assert log2.snapshot() == ["one", "two"]


def test_a_command_that_fails_quickly_is_not_a_timeout(proj):
    """A non-zero exit keeps the old behaviour (None): the start that follows
    reports what is missing in the project's own words."""
    log = _Log()
    argv = [sys.executable, "-c", "import sys; print('nope'); sys.exit(3)"]
    assert workspace._run_blocking(argv, proj, log, timeout=30) is None
    assert log.snapshot() == ["nope"]


def test_the_limits_are_bounded_and_ordered():
    assert 0 < workspace.SETUP_TIMEOUT < workspace.INSTALL_TIMEOUT <= 900
    assert workspace.INSTALL_TIMEOUT == 600 and workspace.SETUP_TIMEOUT == 120


def test_install_gives_each_step_its_limit_and_reports_the_reason(proj, monkeypatch):
    with open(os.path.join(proj, "package.json"), "w", encoding="utf-8") as fh:
        fh.write('{"scripts": {"dev": "vite"}}')
    calls = []

    def fake_run(argv, cwd, log, timeout=None, label=None):
        calls.append((label, timeout))
        # A killed npm install leaves a half-written node_modules behind.
        os.makedirs(os.path.join(cwd, "node_modules", "half"), exist_ok=True)
        return "npm install did not finish in 600 s"

    monkeypatch.setattr(workspace, "_run_blocking", fake_run)
    reason = workspace.install(proj, _Log())
    assert reason == "npm install did not finish in 600 s"
    assert calls == [("npm install", workspace.INSTALL_TIMEOUT)]
    assert not os.path.isdir(os.path.join(proj, "node_modules")), (
        "a half-written node_modules makes the next Run skip the install")


def test_python_setup_steps_get_their_own_limits(proj, monkeypatch):
    with open(os.path.join(proj, "requirements.txt"), "w", encoding="utf-8") as fh:
        fh.write("flask\n")
    calls = []

    def fake_run(argv, cwd, log, timeout=None, label=None):
        calls.append((label, timeout))
        return None

    monkeypatch.setattr(workspace, "_run_blocking", fake_run)
    assert workspace.install(proj, _Log()) is None
    assert calls == [("creating the project's .venv", workspace.SETUP_TIMEOUT),
                     ("pip install -r requirements.txt", workspace.INSTALL_TIMEOUT)]


def test_a_venv_timeout_stops_before_pip(proj, monkeypatch):
    with open(os.path.join(proj, "requirements.txt"), "w", encoding="utf-8") as fh:
        fh.write("flask\n")
    calls = []

    def fake_run(argv, cwd, log, timeout=None, label=None):
        calls.append(label)
        os.makedirs(os.path.join(cwd, ".venv"), exist_ok=True)
        return "creating the project's .venv did not finish in 120 s"

    monkeypatch.setattr(workspace, "_run_blocking", fake_run)
    assert workspace.install(proj, _Log()).startswith("creating the project's .venv")
    assert calls == ["creating the project's .venv"], "pip ran on a half-made venv"
    assert not os.path.isdir(os.path.join(proj, ".venv"))


def test_the_preview_shows_the_reason_instead_of_hanging(proj, monkeypatch):
    spawned = []

    def fake_popen(*a, **k):
        spawned.append(a)
        raise AssertionError("started the project after its install timed out")

    monkeypatch.setattr(workspace, "install",
                        lambda run_dir, log: "npm install did not finish in 600 s")
    monkeypatch.setattr(workspace.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(workspace, "detect", lambda d: {
        "kind": "npm:dev", "argv": ["npm", "run", "dev"], "needs_install": True,
        "run_dir": d})

    workspace.start(proj)
    st = None
    for _ in range(100):
        st = workspace.status(proj)
        if st["state"] != "installing":
            break
        time.sleep(0.05)
    assert st["state"] == "failed", st
    assert st["error"].startswith("npm install did not finish in 600 s")
    assert "press Run" in st["error"]
    assert spawned == []
