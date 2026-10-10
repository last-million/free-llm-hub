"""Apps deploy by themselves: layout analysis, .env seeding, monorepo install +
frontend build, and an HTTP deploy check -- all hermetic.

No real npm / pip / docker / network / server runs here: every project is a
temp folder of fake package.json / .env.example / marker files, every
subprocess is a recording stub, and the HTTP probe and clock are injected.
"""
import json
import os
import types

import pytest

import app
import deploy_perfect
import workspace


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _pkg(path, scripts=None, deps=None, dev=None, extra=None):
    body = {"name": os.path.basename(path)}
    if scripts is not None:
        body["scripts"] = scripts
    if deps is not None:
        body["dependencies"] = deps
    if dev is not None:
        body["devDependencies"] = dev
    if extra:
        body.update(extra)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "package.json"), "w", encoding="utf-8") as fh:
        json.dump(body, fh)


def _write(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _monorepo(root):
    """Root Express+pg server, separate Vite frontend (not installed, not built)."""
    _pkg(root, scripts={"start": "node backend/server.js"},
         deps={"express": "^4", "pg": "^8"})
    _write(os.path.join(root, ".env.example"),
           "PORT=3000\nNODE_ENV=development\n"
           "DB_NAME=home_repair_db\nDB_PASSWORD=change_me\n"
           "JWT_SECRET=your_jwt_secret_here_min_32_chars\n"
           "# payments\nSTRIPE_SECRET_KEY=sk_test_PLACEHOLDER\n")
    _pkg(os.path.join(root, "backend"), scripts={"start": "node server.js"},
         deps={"express": "^4", "pg": "^8"})
    _pkg(os.path.join(root, "frontend"),
         scripts={"dev": "vite", "build": "vite build"},
         dev={"vite": "^6", "@vitejs/plugin-react": "^4"})
    return root


class _Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class _Proc:
    def __init__(self):
        self.logs = []
        self.stopping = False
        self.state = "installing"
        self.error = None

    def log(self, m):
        self.logs.append(m)


# --------------------------------------------------------------------------- #
# env_seed_plan
# --------------------------------------------------------------------------- #

def test_env_seed_randomises_only_internal_secrets():
    body = ("PORT=3000\nDB_PASSWORD=change_me\n"
            "JWT_SECRET=your_jwt_secret_here\nSTRIPE_SECRET_KEY=sk_test_x\n"
            "# a comment\n\nALLOWED_ORIGINS=http://localhost:3000\n")
    plan = deploy_perfect.env_seed_plan(body, secret_factory=lambda: "R" * 40)
    out = dict(line.split("=", 1) for line in plan["content"].splitlines()
               if "=" in line and not line.strip().startswith("#"))
    assert out["JWT_SECRET"] == "R" * 40            # internal secret -> random
    assert out["STRIPE_SECRET_KEY"] == "sk_test_x"  # external -> placeholder kept
    assert out["PORT"] == "3000"                    # kept verbatim
    assert out["ALLOWED_ORIGINS"] == "http://localhost:3000"
    assert "JWT_SECRET" in plan["secrets"]
    assert "STRIPE_SECRET_KEY" in plan["placeholders"]
    # comments and blank lines survive
    assert "# a comment" in plan["content"]


def test_env_seed_defaults_a_bare_db_password():
    plan = deploy_perfect.env_seed_plan("DB_PASSWORD=\n", secret_factory=lambda: "x")
    assert "DB_PASSWORD=postgres" in plan["content"]


def test_env_seed_never_raises_on_garbage():
    assert deploy_perfect.env_seed_plan(None)["content"] == ""
    assert deploy_perfect.env_seed_plan(12345)["content"] == ""


# --------------------------------------------------------------------------- #
# analyze
# --------------------------------------------------------------------------- #

def test_single_app_has_no_extra_steps(tmp_path):
    root = str(tmp_path)
    _pkg(root, scripts={"start": "node server.js"}, deps={"express": "^4"})
    plan = deploy_perfect.analyze(root)
    assert plan["layout"] == "single"
    assert plan["steps"] == []
    assert plan["server_dir"] == os.path.abspath(root)
    assert plan["env"]["needs_seed"] is False


def test_monorepo_installs_both_and_builds_frontend(tmp_path):
    plan = deploy_perfect.analyze(_monorepo(str(tmp_path)))
    assert plan["layout"] == "monorepo"
    kinds = [s["kind"] for s in plan["steps"]]
    assert kinds.count("npm-install") == 2          # frontend + backend
    assert kinds.count("npm-build") == 1
    build = [s for s in plan["steps"] if s["kind"] == "npm-build"][0]
    assert build["dir"].replace("\\", "/").endswith("/frontend")
    assert build["timeout"] == "build"
    assert "frontend" in plan["frontends"]
    assert plan["env"]["needs_seed"] is True
    assert plan["serves_frontend"] is False
    assert any("separate frontend" in n for n in plan["notes"])


def test_monorepo_postgres_note_mentions_createdb(tmp_path):
    plan = deploy_perfect.analyze(_monorepo(str(tmp_path)))
    assert plan["db"]["engine"] == "postgres"
    assert plan["db"]["required"] is True
    assert "createdb home_repair_db" in plan["db"]["note"]
    assert "Monorepo" in plan["summary"] and "DB" in plan["summary"]


def test_a_built_installed_frontend_needs_no_steps(tmp_path):
    root = _monorepo(str(tmp_path))
    os.makedirs(os.path.join(root, "frontend", "node_modules", "vite"), exist_ok=True)
    os.makedirs(os.path.join(root, "backend", "node_modules"), exist_ok=True)
    _write(os.path.join(root, "frontend", "dist", "index.html"), "<html></html>")
    plan = deploy_perfect.analyze(root)
    assert plan["steps"] == []                       # nothing left to install/build


def test_sqlite_needs_no_external_database(tmp_path):
    root = str(tmp_path)
    _pkg(root, scripts={"start": "node server.js"}, deps={"better-sqlite3": "^9"})
    plan = deploy_perfect.analyze(root)
    assert plan["db"]["engine"] == "sqlite"
    assert plan["db"]["required"] is False
    assert plan["db"]["local_ok"] is True


def test_docker_compose_is_flagged(tmp_path):
    root = str(tmp_path)
    _pkg(root, scripts={"start": "node server.js"})
    _write(os.path.join(root, "docker-compose.yml"), "services: {}\n")
    plan = deploy_perfect.analyze(root)
    assert plan["layout"] == "compose"
    assert any("docker compose" in n.lower() for n in plan["notes"])


def test_analyze_never_raises_on_a_missing_dir():
    plan = deploy_perfect.analyze("/no/such/dir/anywhere")
    assert plan["layout"] == "single" and plan["steps"] == []


# --------------------------------------------------------------------------- #
# deploy_check
# --------------------------------------------------------------------------- #

def test_deploy_check_ok_when_it_answers():
    c = _Clock()
    out = deploy_perfect.deploy_check("http://x", probe=lambda u: 200,
                                      now=c.now, sleep=c.sleep)
    assert out["ok"] is True and out["status"] == 200


def test_deploy_check_any_status_counts_after_a_refusal():
    c = _Clock()
    calls = [0]

    def probe(url):
        calls[0] += 1
        if calls[0] == 1:
            raise ConnectionError("refused")
        return 404

    out = deploy_perfect.deploy_check("http://x", probe=probe, now=c.now,
                                      sleep=c.sleep, deadline=10, interval=1)
    assert out["ok"] is True and out["status"] == 404


def test_deploy_check_times_out_with_the_last_error():
    c = _Clock()

    def probe(url):
        raise ConnectionError("refused")

    out = deploy_perfect.deploy_check("http://x", probe=probe, now=c.now,
                                      sleep=c.sleep, deadline=3, interval=1)
    assert out["ok"] is False
    assert out["error"] == "ConnectionError"
    assert out["waited"] >= 3


# --------------------------------------------------------------------------- #
# workspace wiring: .env seeding + monorepo steps
# --------------------------------------------------------------------------- #

def test_prepare_seeds_env_and_runs_each_step(tmp_path, monkeypatch):
    root = _monorepo(str(tmp_path))
    calls = []

    def fake_run(argv, cwd, log, timeout=None, label=None):
        calls.append((label, cwd, timeout))
        os.makedirs(os.path.join(cwd, "node_modules"), exist_ok=True)   # install done
        if "build" in (label or ""):
            os.makedirs(os.path.join(cwd, "dist"), exist_ok=True)
        return None

    monkeypatch.setattr(workspace, "_run_blocking", fake_run)
    proc = _Proc()
    assert workspace._deploy_prepare(root, root, proc) is None

    env = os.path.join(root, ".env")
    assert os.path.isfile(env)
    text = open(env, encoding="utf-8").read()
    assert "sk_test_PLACEHOLDER" in text                   # external key kept
    assert "your_jwt_secret_here_min_32_chars" not in text  # internal secret replaced
    assert "PORT=3000" in text

    labels = [c[0] for c in calls]
    assert sum("npm install" in l for l in labels) == 2
    assert sum("npm run build" in l for l in labels) == 1
    timeouts = {c[0]: c[2] for c in calls}
    assert any(t == workspace.BUILD_TIMEOUT for t in timeouts.values())
    assert any(t == workspace.INSTALL_TIMEOUT for t in timeouts.values())


def test_prepare_does_not_overwrite_an_existing_env(tmp_path, monkeypatch):
    root = _monorepo(str(tmp_path))
    _write(os.path.join(root, ".env"), "KEEP=me\n")
    monkeypatch.setattr(workspace, "_run_blocking",
                        lambda *a, **k: None)
    workspace._deploy_prepare(root, root, _Proc())
    assert open(os.path.join(root, ".env"), encoding="utf-8").read() == "KEEP=me\n"


def test_prepare_removes_a_half_written_install_and_reports(tmp_path, monkeypatch):
    root = _monorepo(str(tmp_path))

    def fake_run(argv, cwd, log, timeout=None, label=None):
        os.makedirs(os.path.join(cwd, "node_modules", "half"), exist_ok=True)
        return "npm install did not finish in 600 s"

    monkeypatch.setattr(workspace, "_run_blocking", fake_run)
    reason = workspace._deploy_prepare(root, root, _Proc())
    assert reason == "npm install did not finish in 600 s"
    # the first sub-app's half-written node_modules is cleaned so the next Run retries
    assert not os.path.isdir(os.path.join(root, "frontend", "node_modules"))


def test_prepare_is_a_noop_for_a_plain_single_project(tmp_path, monkeypatch):
    root = str(tmp_path)
    _pkg(root, scripts={"start": "node server.js"})
    calls = []
    monkeypatch.setattr(workspace, "_run_blocking",
                        lambda *a, **k: calls.append(a) or None)
    assert workspace._deploy_prepare(root, root, _Proc()) is None
    assert calls == []
    assert not os.path.isfile(os.path.join(root, ".env"))


def test_prepare_degrades_when_the_module_is_off(tmp_path, monkeypatch):
    root = _monorepo(str(tmp_path))
    monkeypatch.setattr(workspace, "_deploy_enabled", lambda: False)
    calls = []
    monkeypatch.setattr(workspace, "_run_blocking",
                        lambda *a, **k: calls.append(a) or None)
    assert workspace._deploy_prepare(root, root, _Proc()) is None
    assert calls == [] and not os.path.isfile(os.path.join(root, ".env"))


# --------------------------------------------------------------------------- #
# workspace.detect disambiguation for a frontend+backend monorepo
# --------------------------------------------------------------------------- #

def test_detect_launches_the_backend_of_a_bare_monorepo(tmp_path):
    root = str(tmp_path)   # NO root package.json
    _pkg(os.path.join(root, "frontend"),
         scripts={"dev": "vite", "build": "vite build"}, dev={"vite": "^6"})
    _pkg(os.path.join(root, "backend"),
         scripts={"start": "node server.js"}, deps={"express": "^4"})
    spec = workspace.detect(root)
    assert spec["run_dir"].replace("\\", "/").endswith("/backend")
    assert spec["kind"] == "npm:start"


def test_detect_keeps_two_static_sites_ambiguous(tmp_path):
    root = str(tmp_path)
    for name in ("site-a", "site-b"):
        _write(os.path.join(root, name, "index.html"), "<h1>x</h1>")
    with pytest.raises(workspace.WorkspaceError) as e:
        workspace.detect(root)
    assert "ambiguous" in str(e.value)


# --------------------------------------------------------------------------- #
# workspace.deploy_check wrapper
# --------------------------------------------------------------------------- #

def test_workspace_deploy_check_uses_the_live_url(tmp_path, monkeypatch):
    root = str(tmp_path)
    monkeypatch.setattr(workspace, "status",
                        lambda d: {"running": True, "url": "http://127.0.0.1:3000"})
    out = workspace.deploy_check(root, probe=lambda u: 200)
    assert out["ok"] is True and out["status"] == 200


def test_workspace_deploy_check_none_when_nothing_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "status",
                        lambda d: {"running": False, "url": None})
    assert workspace.deploy_check(str(tmp_path), probe=lambda u: 200) is None


# --------------------------------------------------------------------------- #
# app status-route augmentation (the Build-page surface)
# --------------------------------------------------------------------------- #

def test_augment_adds_a_deploy_block_for_a_monorepo(tmp_path):
    root = _monorepo(str(tmp_path))
    st = {"running": False, "state": "idle", "url": None, "error": None}
    out = app._dp_augment_status(root, st)
    assert "deploy" in out
    assert "Monorepo" in out["deploy"]["summary"]
    assert out["deploy"]["db"]["engine"] == "postgres"
    assert out is not st                               # additive copy, original intact
    assert "deploy" not in st


def test_augment_is_silent_for_a_trivial_folder(tmp_path):
    st = {"running": False, "state": "idle", "url": None}
    out = app._dp_augment_status(str(tmp_path), st)
    assert "deploy" not in out


def test_augment_reports_the_http_check(tmp_path, monkeypatch):
    root = _monorepo(str(tmp_path))
    monkeypatch.setattr(workspace, "deploy_check",
                        lambda d, **k: {"ok": True, "status": 200,
                                        "url": "http://127.0.0.1:3000",
                                        "waited": 0.1, "error": None})
    st = {"running": True, "state": "running", "url": "http://127.0.0.1:3000"}
    out = app._dp_augment_status(root, st, http_check=True)
    assert out["deploy"]["http_ok"] is True
    assert out["deploy"]["http_status"] == 200
