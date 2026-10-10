"""Apps deploy by themselves, part 2: the machine probe at planning, the
run-end deploy check (one fix phase at most) and the remembered start.

Hermetic: the probe gets fake which/run/port_open (Windows, Linux and macOS
output shapes), runs use fake spawn/run_turn, the deploy check is a fake
callable or a faked workspace, and no real process, port or network is used.
"""
import json
import os
import time

import pytest

import app
import craft
import envprobe
import swarm_windows as SW
import workspace


# --------------------------------------------------------------------------- #
# probe fakes: three machines
# --------------------------------------------------------------------------- #

def _machine(paths, outputs, open_ports=()):
    """which/run/port_open fakes. `paths` exe -> path; `outputs` (path, first
    arg) -> (rc, text)."""
    calls = []

    def which(exe):
        return paths.get(exe)

    def run(argv, timeout):
        calls.append(list(argv))
        return outputs.get((argv[0], argv[1]))

    return which, run, (lambda p: p in open_ports), calls


WIN_PATHS = {
    "node": r"C:\Program Files\nodejs\node.exe",
    "npm": r"C:\Program Files\nodejs\npm.cmd",
    "python": r"C:\Users\u\AppData\Local\Microsoft\WindowsApps\python.exe",
    "py": r"C:\Windows\py.exe",
    "pip": r"C:\Python312\Scripts\pip.exe",
    "docker": r"C:\Program Files\Docker\Docker\resources\bin\docker.exe",
}
WIN_OUT = {
    (WIN_PATHS["node"], "--version"): (0, "v22.11.0\r\n"),
    (WIN_PATHS["npm"], "--version"): (0, "10.9.0\r\n"),
    (WIN_PATHS["python"], "--version"): (9009, "Python was not found; run without "
                                         "arguments to install from the Microsoft "
                                         "Store, or disable this shortcut from "
                                         "Settings > Manage App Execution Aliases.\r\n"),
    (WIN_PATHS["py"], "--version"): (0, "Python 3.12.6\r\n"),
    (WIN_PATHS["pip"], "--version"): (0, "pip 24.2 from C:\\Python312\\Lib\\site-packages"
                                      "\\pip (python 3.12)\r\n"),
    (WIN_PATHS["docker"], "--version"): (0, "Docker version 27.3.1, build ce12230\r\n"),
    (WIN_PATHS["docker"], "info"): (1, "error during connect: open "
                                    "//./pipe/dockerDesktopLinuxEngine: The system "
                                    "cannot find the file specified.\r\n"),
}

LINUX_PATHS = {"node": "/usr/bin/node", "npm": "/usr/bin/npm", "python3": "/usr/bin/python3",
               "pip3": "/usr/bin/pip3", "psql": "/usr/bin/psql",
               "redis-server": "/usr/bin/redis-server"}
LINUX_OUT = {
    ("/usr/bin/node", "--version"): (0, "v18.19.1\n"),
    ("/usr/bin/npm", "--version"): (0, "9.2.0\n"),
    ("/usr/bin/python3", "--version"): (0, "Python 3.11.2\n"),
    ("/usr/bin/pip3", "--version"): (0, "pip 23.0.1 from /usr/lib/python3/dist-packages/"
                                     "pip (python 3.11)\n"),
    ("/usr/bin/psql", "--version"): (0, "psql (PostgreSQL) 15.8 (Debian 15.8-0+deb12u1)\n"),
    ("/usr/bin/redis-server", "--version"): (0, "Redis server v=7.0.15 sha=00000000:0 "
                                             "malloc=jemalloc-5.3.0 bits=64 build=1\n"),
}

MAC_PATHS = {"node": "/opt/homebrew/bin/node", "npm": "/opt/homebrew/bin/npm",
             "bun": "/Users/u/.bun/bin/bun", "python3": "/usr/bin/python3",
             "mysql": "/opt/homebrew/bin/mysql", "psql": "/opt/homebrew/bin/psql"}
MAC_OUT = {
    ("/opt/homebrew/bin/node", "--version"): (0, "v20.17.0\n"),
    ("/opt/homebrew/bin/npm", "--version"): (0, "10.8.2\n"),
    ("/Users/u/.bun/bin/bun", "--version"): (0, "1.1.30\n"),
    ("/usr/bin/python3", "--version"): (0, "Python 3.9.6\n"),
    ("/opt/homebrew/bin/mysql", "--version"): (0, "mysql  Ver 9.0.1 for macos14.4 on arm64 "
                                               "(Homebrew)\n"),
    ("/opt/homebrew/bin/psql", "--version"): (0, "psql (PostgreSQL) 14.13 (Homebrew)\n"),
}


@pytest.mark.parametrize("tool,text,want", [
    ("node", "v22.11.0\r\n", "22.11.0"),
    ("npm", "10.9.0", "10.9.0"),
    ("bun", "1.1.30", "1.1.30"),
    ("python", "Python 3.12.6\r\n", "3.12.6"),
    ("python", "Python was not found; run without arguments to install", None),
    ("pip", "pip 24.2 from C:\\Python312\\Lib\\site-packages\\pip (python 3.12)", "24.2"),
    ("pip", "pip 23.0.1 from /usr/lib/python3/dist-packages/pip (python 3.11)", "23.0.1"),
    ("docker", "Docker version 27.3.1, build ce12230", "27.3.1"),
    ("psql", "psql (PostgreSQL) 16.4", "16.4"),
    ("psql", "psql (PostgreSQL) 14.13 (Homebrew)", "14.13"),
    ("mysql", "mysql  Ver 8.0.39 for Linux on x86_64 (MySQL Community Server - GPL)", "8.0.39"),
    ("mysql", "mysql  Ver 15.1 Distrib 10.11.8-MariaDB, for debian-linux-gnu", "10.11.8"),
    ("mysql", "mysql  Ver 9.0.1 for macos14.4 on arm64 (Homebrew)", "9.0.1"),
    ("redis", "Redis server v=7.2.5 sha=00000000:0 malloc=libc bits=64", "7.2.5"),
    ("redis", "redis-cli 7.2.5", "7.2.5"),
    ("npm", "npm ERR! something broke", None),
])
def test_version_shapes(tool, text, want):
    assert envprobe.parse_version(tool, text) == want


def test_probe_reads_a_windows_machine():
    which, run, port_open, calls = _machine(WIN_PATHS, WIN_OUT)
    snap = envprobe.probe(which=which, run=run, port_open=port_open, os_name="windows")
    t = snap["tools"]
    assert t["node"] == "22.11.0" and t["npm"] == "10.9.0"
    assert t["python"] == "3.12.6"               # the Store alias fell through to py
    assert t["pip"] == "24.2" and t["docker"] == "27.3.1"
    assert t["psql"] is None and t["redis"] is None
    assert snap["docker_daemon"] is False
    assert snap["services"] == {"postgres": False, "mysql": False, "redis": False}
    assert snap["busy_ports"] == []
    assert all(c[1] in ("--version", "info") for c in calls)   # nothing else is run


def test_probe_reads_a_linux_and_a_mac_machine():
    which, run, port_open, _ = _machine(LINUX_PATHS, LINUX_OUT, open_ports={5432})
    lin = envprobe.probe(which=which, run=run, port_open=port_open, os_name="linux")
    assert lin["tools"]["psql"] == "15.8" and lin["services"]["postgres"] is True
    assert lin["tools"]["redis"] == "7.0.15" and lin["services"]["redis"] is False
    assert lin["docker_daemon"] is None                        # no docker CLI at all
    which, run, port_open, _ = _machine(MAC_PATHS, MAC_OUT, open_ports={3000})
    mac = envprobe.probe(which=which, run=run, port_open=port_open, os_name="macos")
    assert mac["tools"]["bun"] == "1.1.30" and mac["tools"]["mysql"] == "9.0.1"
    assert mac["busy_ports"] == [3000]


def _win_snap():
    which, run, port_open, _ = _machine(WIN_PATHS, WIN_OUT)
    return envprobe.probe(which=which, run=run, port_open=port_open, os_name="windows")


def test_block_prefers_sqlite_when_postgres_is_missing():
    b = envprobe.block(_win_snap(), who="planner")
    assert b.startswith("THIS MACHINE: node 22, npm 10, python 3.12")
    assert "docker 27 (daemon not running)" in b
    assert "PostgreSQL: not installed (port 5432 closed)" in b
    assert "MySQL, Redis: not installed" in b
    assert "The app MUST run here with one command." in b
    assert "Prefer storage that needs no install (SQLite / a JSON file)" in b
    assert "add an explicit setup phase that says exactly what the user must install" in b
    assert "still provide a working local fallback" in b
    worker = envprobe.block(_win_snap(), who="worker")
    assert "setup phase" not in worker and "say exactly what the user must install" in worker
    assert len(b) < 600


def test_block_names_running_and_installed_services():
    which, run, port_open, _ = _machine(LINUX_PATHS, LINUX_OUT, open_ports={5432})
    b = envprobe.block(envprobe.probe(which=which, run=run, port_open=port_open))
    assert "PostgreSQL: running (port 5432)" in b
    assert "Redis 7: installed, not running (port 6379 closed)" in b
    snap = dict(_win_snap(), services={"postgres": True, "mysql": True, "redis": True})
    assert "asked for a database server." in envprobe.block(snap)
    assert envprobe.block(None) == ""


@pytest.mark.parametrize("text,want", [
    ("build a multi-role web app for customers, technicians and admins", True),
    ("crée une application de réservation pour des techniciens", True),
    ("build an online store and deploy it", True),
    ("create a REST API with a database", True),
    ("create a landing page for my saas", False),
    ("build me a restaurant website", False),
    ("fix the failing test in utils.py", False),
    ("what is an api?", False),
])
def test_only_app_builds_get_the_block(text, want):
    assert envprobe.wants_block(text) is want


def test_snapshot_is_cached_and_refreshed_in_the_background(monkeypatch):
    calls = []

    def fake_probe():
        calls.append(1)
        return {"tools": {}, "services": {}, "busy_ports": [], "n": len(calls)}

    monkeypatch.setattr(envprobe, "ENABLED", True)
    monkeypatch.setattr(envprobe, "probe", fake_probe)
    envprobe.reset()
    try:
        first = envprobe.snapshot(wait=5)
        assert first["n"] == 1
        assert envprobe.snapshot()["n"] == 1 and len(calls) == 1      # cached
        envprobe._STATE["at"] -= envprobe.TTL + 1                    # expire it
        assert envprobe.snapshot(wait=5)["n"] == 2
    finally:
        envprobe.reset()
    monkeypatch.setattr(envprobe, "ENABLED", False)
    assert envprobe.snapshot(wait=1) is None


# --------------------------------------------------------------------------- #
# the single-session brief: app builds only, inside the cost ceiling
# --------------------------------------------------------------------------- #

def test_the_brief_carries_the_block_for_app_builds_only(monkeypatch):
    block = envprobe.block(_win_snap(), who="session")
    monkeypatch.setattr(craft, "_ENV_SOURCE",
                        lambda t: block if envprobe.wants_block(t) else "")
    app_ask = craft.system_message("build an online store and deploy it")["content"]
    assert block in app_ask
    assert app_ask.index(block) < app_ask.index(craft.PLAN_PHASES)   # loop stays last
    for text in ("create a landing page for my saas", "fix my python bug"):
        with_source = craft.system_message(text)
        monkeypatch.setattr(craft, "_ENV_SOURCE", None)
        without = craft.system_message(text)
        monkeypatch.setattr(craft, "_ENV_SOURCE",
                            lambda t: block if envprobe.wants_block(t) else "")
        assert with_source == without
    assert block not in (craft.system_message("build an online store", tools=False)
                         or {}).get("content", "")


def test_the_brief_never_crosses_the_cost_ceiling(monkeypatch):
    assert craft.BRIEF_CEILING_CHARS == int(32768 * 0.135 * 4)
    block = envprobe.block(_win_snap(), who="session")
    monkeypatch.setattr(craft, "_ENV_SOURCE",
                        lambda t: block if envprobe.wants_block(t) else "")
    asks = ("build an online store and deploy it",
            "create a landing page for my saas",
            "build me a restaurant website",
            "build a saas web app with login, payments, a landing page and a database",
            "build an online store with seo, product images, a secure checkout and "
            "deploy it",
            "build a multi-role web app for customers, technicians and admins")
    for text in asks:
        assert len(craft.system_message(text)["content"]) <= craft.BRIEF_CEILING_CHARS, text
    # a block that would cross it is dropped, never trimmed into the loop
    monkeypatch.setattr(craft, "_ENV_SOURCE", lambda t: "X" * 20000)
    body = craft.system_message("build an online store and deploy it")["content"]
    assert "XXXX" not in body and len(body) <= craft.BRIEF_CEILING_CHARS


# --------------------------------------------------------------------------- #
# the Multi planner and workers
# --------------------------------------------------------------------------- #

def test_the_planner_and_workers_get_the_block(monkeypatch):
    block = envprobe.block(_win_snap(), who="planner")
    goal = "build a booking platform for repair technicians"
    monkeypatch.setattr(SW, "_ENV_SOURCE", [None])
    before = SW.plan_system(goal)
    monkeypatch.setattr(SW, "_ENV_SOURCE", [lambda g, who: block if who == "planner" else "W"])
    after = SW.plan_system(goal)
    assert after.startswith(before) and block in after
    monkeypatch.setattr(SW, "_ENV_SOURCE", [lambda g, who: ""])
    assert SW.plan_system(goal) == before                   # byte-identical without
    run = SW._Run(goal, ".", "codex", [{"title": "A", "task": "do a", "needs": []}],
                  env_block="THIS MACHINE: node 22.")
    assert "THIS MACHINE: node 22." in SW._agent_prompt(run, run.agents[0])
    plain = SW._Run(goal, ".", "codex", [{"title": "A", "task": "do a", "needs": []}])
    assert "THIS MACHINE" not in SW._agent_prompt(plain, plain.agents[0])


# --------------------------------------------------------------------------- #
# the run-end deploy check
# --------------------------------------------------------------------------- #

@pytest.fixture
def runs(tmp_path, monkeypatch):
    monkeypatch.setenv(SW._STORE_ENV, str(tmp_path / "runs"))
    monkeypatch.setattr(SW, "SPAWN_STAGGER", 0.0)
    monkeypatch.setattr(SW, "_ENV_SOURCE", [None])
    SW._RUNS.clear()
    yield
    for run in list(SW._RUNS.values()):
        run.stop_flag.set()
    SW._RUNS.clear()


_n = [0]


def _spawn(cli, project):
    _n[0] += 1
    return "sess-%d" % _n[0]


def _turns(fix_ok=True, seen=None):
    def run_turn(session_id, prompt):
        if seen is not None:
            seen.append(prompt)
        if "THE APP DOES NOT START" in prompt and not fix_ok:
            yield {"type": "done"}                 # the fix phase produces nothing
            return
        yield {"type": "message", "text": "did: " + prompt.splitlines()[0][:40]}
        yield {"type": "done"}
    return run_turn


def _wait(run_id, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        st = SW.status(run_id)
        if st and st["state"] in (SW.DONE, SW.FAILED, SW.STOPPED):
            return st
        time.sleep(0.02)
    return SW.status(run_id)


PHASES = [{"title": "Backend", "task": "build the api", "needs": []},
          {"title": "Frontend", "task": "build the ui", "needs": [1]}]
FAIL = {"ok": False, "error": "npm ERR! missing script: start",
        "log_tail": ["> fixli@1.0.0 start", "Error: Cannot find module 'express'"]}
OK = {"ok": True, "url": "http://127.0.0.1:5812", "port": 5812}


def _check(*results):
    calls = []

    def check(run):
        calls.append(run.id)
        return dict(results[min(len(calls), len(results)) - 1])
    check.calls = calls
    return check


def _fixes(run):
    return [a for a in run.agents if a.label == SW.DEPLOY_FIX_LABEL]


def test_a_passing_check_says_deployed_and_adds_nothing(runs):
    check = _check(OK)
    rid = SW.start("build an app", ".", "codex", _spawn, _turns(), phases=PHASES,
                   deploy_check=check)
    assert _wait(rid)["state"] == SW.DONE
    run = SW.get(rid)
    assert len(check.calls) == 1 and len(run.agents) == 3 and not _fixes(run)
    assert run.deploy["state"] == "ok" and run.deploy_fix_used is False
    assert "Deployed: http://127.0.0.1:5812" in SW.format_result(rid)


def test_a_failure_queues_exactly_one_fix_phase_then_checks_again(runs):
    seen = []
    check = _check(FAIL, OK)
    rid = SW.start("build an app", ".", "codex", _spawn, _turns(seen=seen),
                   phases=PHASES, deploy_check=check)
    _wait(rid)
    run = SW.get(rid)
    assert len(check.calls) == 2
    fixes = _fixes(run)
    assert len(fixes) == 1 and fixes[0].index == 4 and fixes[0].state == SW.DONE
    fix_prompt = [p for p in seen if "THE APP DOES NOT START" in p][0]
    assert "npm ERR! missing script: start" in fix_prompt
    assert "Cannot find module 'express'" in fix_prompt
    assert SW._is_review(run, run.agents[2])                  # the review stays the review
    assert "Deployed: http://127.0.0.1:5812 (after fix phase 4)" in SW.format_result(rid)


def test_a_second_failure_reports_the_error_and_never_adds_another_phase(runs):
    check = _check(FAIL, FAIL, FAIL)
    rid = SW.start("build an app", ".", "codex", _spawn, _turns(fix_ok=False),
                   phases=PHASES, deploy_check=check)
    _wait(rid)
    text = SW.format_result(rid)
    assert "Deploy failed: npm ERR! missing script: start (after fix phase 4)" in text
    assert "Cannot find module 'express'" in text
    # "continue" re-runs the failed fix phase and checks again -- still ONE fix phase
    assert SW.resume(rid, _spawn, _turns(fix_ok=False), deploy_check=check) == rid
    _wait(rid)
    run = SW.get(rid)
    assert len(_fixes(run)) == 1 and len(check.calls) == 3


def test_the_outcome_survives_a_restart(runs):
    check = _check(FAIL, FAIL)
    rid = SW.start("build an app", ".", "codex", _spawn, _turns(fix_ok=False),
                   phases=PHASES, deploy_check=check)
    _wait(rid)
    SW._RUNS.clear()
    SW.load()
    run = SW.get(rid)
    assert run is not None and run.restored
    assert run.deploy["state"] == "failed" and run.deploy_fix_used is True
    assert run.agents[-1].label == SW.DEPLOY_FIX_LABEL
    assert SW.deploy_view(run)["line"].startswith("Deploy failed:")


def test_no_check_injected_means_the_report_is_unchanged(runs):
    rid = SW.start("build an app", ".", "codex", _spawn, _turns(), phases=PHASES)
    _wait(rid)
    assert SW.get(rid).deploy is None and "Deploy" not in SW.format_result(rid)


def test_a_spent_budget_gets_no_fix_phase(runs, monkeypatch):
    check = _check(FAIL)
    run = SW._Run("build an app", ".", "codex", PHASES, deploy_check=check)
    for a in run.agents:
        a.state = SW.DONE
    monkeypatch.setattr(SW._Run, "budget_status", lambda self: (True, "calls", "spent"))
    SW._deploy_after_run(run, _spawn, _turns())
    assert len(run.agents) == 2 and not run.deploy_fix_used
    assert run.deploy["state"] == "failed"


# --------------------------------------------------------------------------- #
# app: the injected check
# --------------------------------------------------------------------------- #

class _Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _fake_workspace(monkeypatch, statuses, remembered=None):
    seq = list(statuses)
    monkeypatch.setattr(workspace, "start", lambda d: {"state": "installing"})
    monkeypatch.setattr(workspace, "status",
                        lambda d: seq.pop(0) if len(seq) > 1 else seq[0])
    monkeypatch.setattr(workspace, "remember_start",
                        lambda d: remembered.append(d) if remembered is not None else None)


def test_start_and_check_passes_and_remembers(tmp_path, monkeypatch):
    remembered = []
    _fake_workspace(monkeypatch, [{"state": "installing"},
                                  {"state": "running", "url": "http://127.0.0.1:5812",
                                   "port": 5812, "log": []}], remembered)
    c = _Clock()
    out = app._dp_start_and_check(str(tmp_path), clock=c.now, sleep=c.sleep,
                                  probe=lambda u: 200)
    assert out["ok"] is True and out["url"] == "http://127.0.0.1:5812"
    assert remembered == [str(tmp_path)]


def test_start_and_check_reports_the_error_and_last_lines(tmp_path, monkeypatch):
    _fake_workspace(monkeypatch, [{"state": "failed", "error": "npm ERR! missing script: start",
                                   "log": ["> app start", "Error: Cannot find module 'pg'"]}])
    c = _Clock()
    out = app._dp_start_and_check(str(tmp_path), clock=c.now, sleep=c.sleep)
    assert out["ok"] is False and "missing script" in out["error"]
    assert "Error: Cannot find module 'pg'" in out["log_tail"]


def test_start_and_check_is_bounded(tmp_path, monkeypatch):
    _fake_workspace(monkeypatch, [{"state": "starting", "log": []}])
    c = _Clock()
    out = app._dp_start_and_check(str(tmp_path), wait=120, clock=c.now, sleep=c.sleep)
    assert out["ok"] is False and out["error"] == "it did not start within 120 s"
    assert c.t <= 121


def test_start_and_check_needs_an_http_answer(tmp_path, monkeypatch):
    _fake_workspace(monkeypatch, [{"state": "running", "url": "http://127.0.0.1:5812",
                                   "log": ["listening"]}])

    def refused(url):
        raise ConnectionError("refused")

    c = _Clock()
    out = app._dp_start_and_check(str(tmp_path), clock=c.now, sleep=c.sleep, probe=refused)
    assert out["ok"] is False and "did not answer HTTP" in out["error"]


def test_start_and_check_with_nothing_to_start(tmp_path, monkeypatch):
    def boom(d):
        raise workspace.WorkspaceError("nothing runnable in this folder")
    monkeypatch.setattr(workspace, "start", boom)
    out = app._dp_start_and_check(str(tmp_path))
    assert out["ok"] is False and "nothing the preview can start" in out["error"]


def test_the_check_only_runs_for_app_builds(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "_dp_run_check_on", lambda: True)
    monkeypatch.setattr(app, "_dp_start_and_check", lambda d: {"ok": True, "url": "u"})
    docs = SW._Run("write the docs for the library", str(tmp_path), "codex",
                   [{"title": "A", "task": "a", "needs": []}])
    web = SW._Run("build a multi-role web app", str(tmp_path), "codex",
                  [{"title": "A", "task": "a", "needs": []}])
    assert app._dp_run_deploy_check(docs) is None
    assert app._dp_run_deploy_check(web) == {"ok": True, "url": "u"}
    monkeypatch.setattr(app, "_dp_run_check_on", lambda: False)
    assert app._dp_run_deploy_check(web) is None


def test_every_run_entry_point_gets_the_check(monkeypatch):
    monkeypatch.setattr(app, "_dp_run_check_on", lambda: True)
    assert app._multi_check_kwargs()["deploy_check"] is app._dp_run_deploy_check
    monkeypatch.setattr(app, "_dp_run_check_on", lambda: False)
    assert "deploy_check" not in app._multi_check_kwargs()


# --------------------------------------------------------------------------- #
# the remembered start
# --------------------------------------------------------------------------- #

def _pkg(path, scripts):
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "package.json"), "w", encoding="utf-8") as fh:
        json.dump({"name": os.path.basename(path), "scripts": scripts}, fh)


class _Running:
    def __init__(self, project, run_dir, kind, port):
        self.project_dir, self.run_dir, self.kind, self.port = project, run_dir, kind, port
        self.state = "running"


def _restart_and_start(monkeypatch, project):
    """Forget every live preview (a hub restart), then press Run."""
    workspace._procs.pop(project, None)
    seen = {}

    def fake_popen(argv, cwd=None, env=None, **k):
        seen.update(argv=list(argv), cwd=cwd, port=(env or {}).get("PORT"))
        raise FileNotFoundError("fake: nothing is started in a test")

    monkeypatch.setattr(workspace, "install", lambda run_dir, log: None)
    monkeypatch.setattr(workspace, "_port_open", lambda p: False)
    monkeypatch.setattr(workspace.subprocess, "Popen", fake_popen)
    workspace.start(project)
    for _ in range(200):
        if workspace.status(project)["state"] not in ("installing", "starting"):
            break
        time.sleep(0.02)
    workspace._procs.pop(project, None)
    return seen


def test_a_remembered_start_is_reused_after_a_restart(tmp_path, monkeypatch):
    project = str(tmp_path / "proj")
    _pkg(os.path.join(project, "site"), {"dev": "vite"})
    _pkg(os.path.join(project, "api-x"), {"start": "node server.js"})
    with pytest.raises(workspace.WorkspaceError):
        workspace.detect(project)                     # two undeclared folders: ambiguous
    api = os.path.join(project, "api-x")
    workspace._procs[project] = _Running(project, api, "npm:start", 5913)
    rec = workspace.remember_start(project)
    assert rec == {"kind": "npm:start", "run_dir": "api-x", "port": 5913, "at": rec["at"]}
    seen = _restart_and_start(monkeypatch, project)
    assert os.path.normcase(seen["cwd"]) == os.path.normcase(api)
    assert seen["port"] == "5913" and "start" in seen["argv"]


def test_a_record_that_no_longer_fits_is_dropped(tmp_path, monkeypatch):
    project = str(tmp_path / "proj")
    api = os.path.join(project, "api-x")
    _pkg(api, {"start": "node server.js"})
    workspace._procs[project] = _Running(project, api, "npm:start", 5914)
    workspace.remember_start(project)
    _pkg(api, {"dev": "node --watch server.js"})       # its start script changed
    seen = _restart_and_start(monkeypatch, project)    # one runnable folder: detect finds it
    assert workspace.canonical(project) is None
    with open(workspace.CANON_PATH, encoding="utf-8") as fh:
        assert json.load(fh) == {}
    assert "dev" in seen["argv"] and seen["port"] != "5914"


def _write_record(project, run_dir):
    data = {workspace._canon_key(project): {"kind": "npm:start", "run_dir": run_dir,
                                            "port": 5915, "at": 1}}
    with open(workspace.CANON_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def test_a_record_pointing_outside_or_through_a_link_is_never_used(tmp_path):
    project = str(tmp_path / "proj")
    os.makedirs(project)
    outside = str(tmp_path / "outside")
    _pkg(outside, {"start": "node evil.js"})
    _write_record(project, "../outside")
    assert workspace.canonical(project) is None
    link = os.path.join(project, "api")
    made = False
    if os.name == "nt":
        try:
            import _winapi
            _winapi.CreateJunction(outside, link)
            made = True
        except Exception:                                        # noqa: BLE001
            made = False
    if not made:
        try:
            os.symlink(outside, link, target_is_directory=True)
            made = True
        except (OSError, NotImplementedError):
            pytest.skip("this platform cannot create a directory link here")
    try:
        _write_record(project, "api")
        assert workspace.canonical(project) is None
    finally:
        try:
            os.rmdir(link) if os.name == "nt" else os.unlink(link)
        except OSError:
            pass
