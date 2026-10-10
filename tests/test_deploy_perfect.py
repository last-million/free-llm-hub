"""Apps deploy by themselves: layout analysis, .env seeding, declared
sub-project installs (scripts off) + frontend build, an HTTP deploy check, and
the security limits around all of it -- hermetic.

No real npm / pip / docker / network / server runs here: every project is a
temp folder of fake package.json / .env.example / marker files, every
subprocess is a recording stub, and the HTTP probe and clock are injected.
Symlink cases skip where the platform refuses to create links; junctions are
made with _winapi.CreateJunction (no privilege needed) on Windows.
"""
import json
import os
import urllib.parse

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
    """Root Express+pg server (with deps), plus the conventional
    frontend/+backend/ pair; the Vite frontend is not installed, not built."""
    _pkg(root, scripts={"start": "node backend/server.js"},
         deps={"express": "^4", "pg": "^8"})
    _write(os.path.join(root, ".env.example"),
           "# app\nPORT=3000\nNODE_ENV=development\n"
           "DB_NAME=home_repair_db\nDB_PASSWORD=change_me\n"
           "JWT_SECRET=your_jwt_secret_here_min_32_chars\n"
           "# payments\nSTRIPE_SECRET_KEY=sk_test_PLACEHOLDER\n")
    _pkg(os.path.join(root, "backend"), scripts={"start": "node server.js"},
         deps={"express": "^4", "pg": "^8"})
    _pkg(os.path.join(root, "frontend"),
         scripts={"dev": "vite", "build": "vite build"},
         dev={"vite": "^6", "@vitejs/plugin-react": "^4"})
    return root


def _auth():
    import config
    return {"X-Free-LLM-Hub": "dashboard",
            "X-Free-LLM-Hub-Token": config.ensure_control_token()}


def _try_symlink(src, dst, is_dir=False):
    try:
        os.symlink(src, dst, target_is_directory=is_dir)
        return True
    except (OSError, NotImplementedError, AttributeError):
        return False


def _try_dir_link(src, dst):
    """A directory link: a junction on Windows (no privilege), else a symlink."""
    if os.name == "nt":
        try:
            import _winapi
            _winapi.CreateJunction(src, dst)
            return True
        except Exception:                                        # noqa: BLE001
            pass
    return _try_symlink(src, dst, is_dir=True)


def _unlink_dir_link(path):
    try:
        os.rmdir(path) if os.name == "nt" else os.unlink(path)
    except OSError:
        pass


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


def _kv(content):
    return dict(line.split("=", 1) for line in content.splitlines() if "=" in line)


# --------------------------------------------------------------------------- #
# env_seed_plan
# --------------------------------------------------------------------------- #

def test_env_seed_randomises_only_internal_secrets():
    body = ("PORT=3000\nDB_PASSWORD=change_me\n"
            "JWT_SECRET=your_jwt_secret_here\nSTRIPE_SECRET_KEY=sk_test_x\n"
            "# a comment\n\nALLOWED_ORIGINS=http://localhost:3000\n")
    plan = deploy_perfect.env_seed_plan(body, secret_factory=lambda: "R" * 40)
    out = _kv(plan["content"])
    assert out["JWT_SECRET"] == "R" * 40
    assert out["STRIPE_SECRET_KEY"] == "sk_test_x"
    assert out["PORT"] == "3000"
    assert out["ALLOWED_ORIGINS"] == "http://localhost:3000"
    assert "JWT_SECRET" in plan["secrets"]
    assert "STRIPE_SECRET_KEY" in plan["placeholders"]
    assert "SESSION" not in " ".join(plan["placeholders"])


def test_env_seed_copies_only_key_value_lines():
    body = ("# comment\n\nexport API_URL=http://localhost:3000\n"
            "not a kv line\n1BAD=x\nGOOD=1\nGOOD=2\n"
            "CTRL=a\x01b\n" + "LONG=" + "x" * 2000 + "\n")
    plan = deploy_perfect.env_seed_plan(body, secret_factory=lambda: "s")
    lines = plan["content"].splitlines()
    assert lines == ["API_URL=http://localhost:3000", "GOOD=1"]


def test_env_seed_defaults_a_bare_db_password():
    plan = deploy_perfect.env_seed_plan("DB_PASSWORD=\n", secret_factory=lambda: "x")
    assert "DB_PASSWORD=postgres" in plan["content"]


def test_env_seed_never_raises_on_garbage():
    assert deploy_perfect.env_seed_plan(None)["content"] == ""
    assert deploy_perfect.env_seed_plan(12345)["content"] == ""


# --------------------------------------------------------------------------- #
# analyze: which sub-projects, which steps
# --------------------------------------------------------------------------- #

def test_single_app_has_no_extra_steps(tmp_path):
    root = str(tmp_path)
    _pkg(root, scripts={"start": "node server.js"}, deps={"express": "^4"})
    plan = deploy_perfect.analyze(root)
    assert plan["layout"] == "single"
    assert plan["steps"] == []
    assert plan["env"]["needs_seed"] is False


def test_pair_installs_the_frontend_with_scripts_off_and_builds_it(tmp_path):
    plan = deploy_perfect.analyze(_monorepo(str(tmp_path)))
    assert plan["layout"] == "monorepo"
    assert [s["kind"] for s in plan["steps"]] == ["install", "build"]
    for s in plan["steps"]:
        assert s["dir"].replace("\\", "/").endswith("/frontend")
    # the backend is NOT sub-installed: the root (with deps) covers the server
    assert all("backend" not in s["dir"] for s in plan["steps"])
    assert plan["env"]["needs_seed"] is True
    assert any("separate frontend" in n for n in plan["notes"])


def test_a_bare_pair_installs_only_the_non_server_member(tmp_path):
    root = str(tmp_path)        # no root package.json: the backend is the server
    _pkg(os.path.join(root, "frontend"),
         scripts={"dev": "vite", "build": "vite build"}, dev={"vite": "^6"})
    _pkg(os.path.join(root, "backend"),
         scripts={"start": "node server.js"}, deps={"express": "^4"})
    plan = deploy_perfect.analyze(root)
    assert plan["server_dir"].replace("\\", "/").endswith("/backend")
    assert [s["kind"] for s in plan["steps"]] == ["install", "build"]
    assert all(s["dir"].replace("\\", "/").endswith("/frontend") for s in plan["steps"])


def test_scanned_subfolders_are_never_installed(tmp_path):
    root = str(tmp_path)
    _pkg(root, scripts={"start": "node server.js"})
    _pkg(os.path.join(root, "site"), scripts={"build": "vite build"}, dev={"vite": "^6"})
    _pkg(os.path.join(root, "tools"), scripts={"start": "node t.js"})
    plan = deploy_perfect.analyze(root)
    assert plan["layout"] == "single" and plan["steps"] == []


def test_workspaces_build_members_but_never_install_them(tmp_path):
    root = str(tmp_path)
    _pkg(root, scripts={"dev": "turbo dev"}, extra={"workspaces": ["apps/*"]})
    _pkg(os.path.join(root, "apps", "web"),
         scripts={"dev": "vite", "build": "vite build"}, dev={"vite": "^6"})
    _pkg(os.path.join(root, "apps", "api"), scripts={"start": "node a.js"})
    plan = deploy_perfect.analyze(root)
    assert plan["layout"] == "workspaces"
    assert [s["kind"] for s in plan["steps"]] == ["build"]
    assert plan["steps"][0]["dir"].replace("\\", "/").endswith("apps/web")


def test_pnpm_workspace_yaml_declares_members(tmp_path):
    root = str(tmp_path)
    _pkg(root, scripts={"dev": "pnpm -r dev"})
    _write(os.path.join(root, "pnpm-lock.yaml"), "lockfileVersion: 9\n")
    _write(os.path.join(root, "pnpm-workspace.yaml"),
           "packages:\n  - 'packages/*'   # ui lives here\n  - '!packages/skip'\n")
    _pkg(os.path.join(root, "packages", "ui"),
         scripts={"build": "vite build"}, dev={"vite": "^6"})
    plan = deploy_perfect.analyze(root)
    assert plan["layout"] == "workspaces"
    assert plan["members"] == ["packages/ui"]
    assert plan["steps"][0]["pm"] == "pnpm"


def test_workspace_globs_never_enter_skip_hidden_or_outside_dirs(tmp_path):
    root = str(tmp_path / "proj")
    _pkg(root, scripts={"dev": "x"}, extra={"workspaces": [
        "examples/*", "node_modules/*", ".hidden/*", "../outside", "apps/**",
        "fixtures/a", "vendor/*"]})
    for rel in ("examples/a", "node_modules/b", ".hidden/c", "fixtures/a", "vendor/v"):
        _pkg(os.path.join(root, *rel.split("/")),
             scripts={"build": "vite build"}, dev={"vite": "^6"})
    _pkg(str(tmp_path / "outside"), scripts={"build": "vite build"}, dev={"vite": "^6"})
    plan = deploy_perfect.analyze(root)
    assert plan["members"] == [] and plan["steps"] == []


def test_pm_args_always_turn_lifecycle_scripts_off():
    for pm in ("npm", "pnpm", "yarn", "bun"):
        assert "--ignore-scripts" in deploy_perfect.pm_args(pm, "install")
    assert deploy_perfect.pm_args("yarn-berry", "install") == ["install", "--mode=skip-build"]
    assert deploy_perfect.pm_args("npm", "build") == ["run", "build"]
    assert deploy_perfect.pm_args("nope", "install") is None


def test_monorepo_postgres_note_mentions_createdb(tmp_path):
    plan = deploy_perfect.analyze(_monorepo(str(tmp_path)))
    assert plan["db"]["engine"] == "postgres" and plan["db"]["required"] is True
    assert "createdb home_repair_db" in plan["db"]["note"]
    assert "Monorepo" in plan["summary"] and "DB" in plan["summary"]


def test_a_built_installed_frontend_needs_no_steps(tmp_path):
    root = _monorepo(str(tmp_path))
    os.makedirs(os.path.join(root, "frontend", "node_modules", "vite"), exist_ok=True)
    os.makedirs(os.path.join(root, "backend", "node_modules"), exist_ok=True)
    _write(os.path.join(root, "frontend", "dist", "index.html"), "<html></html>")
    assert deploy_perfect.analyze(root)["steps"] == []


def test_sqlite_needs_no_external_database(tmp_path):
    root = str(tmp_path)
    _pkg(root, scripts={"start": "node server.js"}, deps={"better-sqlite3": "^9"})
    db = deploy_perfect.analyze(root)["db"]
    assert db["engine"] == "sqlite" and db["required"] is False and db["local_ok"] is True


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
# path safety: links, realpath containment, caps
# --------------------------------------------------------------------------- #

def test_inside_is_component_wise(tmp_path):
    base = str(tmp_path / "proj")
    assert deploy_perfect.inside(os.path.join(base, "sub"), base)
    assert deploy_perfect.inside(base, base)
    assert not deploy_perfect.inside(base + "-evil", base)
    assert not deploy_perfect.inside(str(tmp_path), base)


def test_a_realpath_outside_the_project_is_refused_with_a_fake(tmp_path):
    root = str(tmp_path)
    outside = lambda p: os.path.join(os.path.dirname(root), "elsewhere", "x")  # noqa: E731
    assert deploy_perfect.safe_path(os.path.join(root, "a"), root,
                                    realpath=outside, is_link_fn=lambda p: False) is None
    text, why = deploy_perfect.safe_read_text(os.path.join(root, "a"), root, 100,
                                              realpath=outside, is_link_fn=lambda p: False)
    assert text is None and "outside" in why


def test_a_member_resolving_outside_is_never_entered_with_a_fake(tmp_path):
    root = _monorepo(str(tmp_path / "proj"))
    real = os.path.realpath

    def fake(p):
        parts = p.replace("\\", "/").split("/")
        return str(tmp_path / "elsewhere") if "frontend" in parts else real(p)

    plan = deploy_perfect.analyze(root, realpath=fake)
    assert plan["steps"] == [] and plan["frontends"] == []


def test_a_linked_sub_folder_is_never_entered(tmp_path):
    root = str(tmp_path / "proj")
    _pkg(root, scripts={"start": "node backend/server.js"})
    _pkg(os.path.join(root, "backend"), scripts={"start": "node s.js"})
    target = str(tmp_path / "outside-frontend")
    _pkg(target, scripts={"build": "vite build"}, dev={"vite": "^6"})
    link = os.path.join(root, "frontend")
    if not _try_dir_link(target, link):
        pytest.skip("this platform cannot create a directory link here")
    try:
        assert deploy_perfect.is_link(link)
        plan = deploy_perfect.analyze(root)
        assert plan["steps"] == [] and plan["layout"] == "single"
    finally:
        _unlink_dir_link(link)


def test_a_symlinked_env_example_is_never_read(tmp_path, monkeypatch):
    root = str(tmp_path / "proj")
    _pkg(root, scripts={"start": "node server.js"})
    secret = str(tmp_path / "secret.txt")
    _write(secret, "API_KEY=very-private\n")
    if not _try_symlink(secret, os.path.join(root, ".env.example")):
        pytest.skip("this platform cannot create a file symlink here")
    plan = deploy_perfect.analyze(root)
    assert plan["env"]["needs_seed"] is False
    monkeypatch.setattr(workspace, "_run_blocking", lambda *a, **k: None)
    workspace._deploy_prepare(root, root, _Proc())
    assert not os.path.lexists(os.path.join(root, ".env"))


def test_a_symlinked_env_is_never_written_through(tmp_path):
    root = str(tmp_path / "proj")
    os.makedirs(root)
    victim = str(tmp_path / "victim.txt")
    _write(victim, "ORIGINAL\n")
    env = os.path.join(root, ".env")
    if not _try_symlink(victim, env):
        pytest.skip("this platform cannot create a file symlink here")
    ok, why = workspace._create_exclusive(env, "EVIL=1\n")
    assert ok is False
    assert open(victim, encoding="utf-8").read() == "ORIGINAL\n"


def test_a_dangling_env_link_never_creates_its_target(tmp_path):
    """MEASURED on Windows: os.open(O_CREAT|O_EXCL) through a DANGLING
    symlink creates the link's target. The hub must refuse instead."""
    root = str(tmp_path / "proj")
    os.makedirs(root)
    target = str(tmp_path / "planted-by-the-link.cmd")
    env = os.path.join(root, ".env")
    if not _try_symlink(target, env):
        pytest.skip("this platform cannot create a file symlink here")
    ok, why = workspace._create_exclusive(env, "X=1 & calc\n")
    assert ok is False
    assert not os.path.exists(target)


def test_create_exclusive_never_overwrites(tmp_path):
    p = str(tmp_path / ".env")
    assert workspace._create_exclusive(p, "A=1\n") == (True, None)
    assert open(p, encoding="utf-8").read() == "A=1\n"
    ok, why = workspace._create_exclusive(p, "B=2\n")
    assert ok is False and why == "already exists"
    assert open(p, encoding="utf-8").read() == "A=1\n"


def test_an_oversized_env_example_is_refused(tmp_path, monkeypatch):
    root = str(tmp_path)
    _pkg(root, scripts={"start": "node server.js"})
    _write(os.path.join(root, ".env.example"),
           "A=1\n" * (deploy_perfect.ENV_EXAMPLE_MAX_BYTES // 4 + 10))
    monkeypatch.setattr(workspace, "_run_blocking", lambda *a, **k: None)
    proc = _Proc()
    workspace._deploy_prepare(root, root, proc)
    assert not os.path.exists(os.path.join(root, ".env"))
    assert any("larger than 64 KB" in m for m in proc.logs)


def test_safe_read_refuses_a_directory(tmp_path):
    d = str(tmp_path / "dir")
    os.makedirs(d)
    text, why = deploy_perfect.safe_read_text(d, str(tmp_path), 100)
    assert text is None


# --------------------------------------------------------------------------- #
# workspace wiring: .env seeding + declared steps
# --------------------------------------------------------------------------- #

def test_prepare_seeds_env_and_runs_each_step_with_scripts_off(tmp_path, monkeypatch):
    root = _monorepo(str(tmp_path))
    calls = []

    def fake_run(argv, cwd, log, timeout=None, label=None):
        calls.append((list(argv), label, cwd, timeout))
        os.makedirs(os.path.join(cwd, "node_modules"), exist_ok=True)
        if "build" in (label or ""):
            os.makedirs(os.path.join(cwd, "dist"), exist_ok=True)
        return None

    monkeypatch.setattr(workspace, "_run_blocking", fake_run)
    assert workspace._deploy_prepare(root, root, _Proc()) is None

    text = open(os.path.join(root, ".env"), encoding="utf-8").read()
    assert "sk_test_PLACEHOLDER" in text
    assert "your_jwt_secret_here_min_32_chars" not in text
    assert "#" not in text                                 # KEY=VALUE lines only

    installs = [c for c in calls if "install" in (c[1] or "")]
    builds = [c for c in calls if "build" in (c[1] or "")]
    assert len(installs) == 1 and len(builds) == 1
    assert "--ignore-scripts" in installs[0][0]
    assert installs[0][3] == workspace.INSTALL_TIMEOUT
    assert builds[0][3] == workspace.BUILD_TIMEOUT
    assert all(c[2].replace("\\", "/").endswith("/frontend") for c in calls)


def test_prepare_does_not_overwrite_an_existing_env(tmp_path, monkeypatch):
    root = _monorepo(str(tmp_path))
    _write(os.path.join(root, ".env"), "KEEP=me\n")
    monkeypatch.setattr(workspace, "_run_blocking", lambda *a, **k: None)
    workspace._deploy_prepare(root, root, _Proc())
    assert open(os.path.join(root, ".env"), encoding="utf-8").read() == "KEEP=me\n"


def test_prepare_removes_a_half_written_install_and_reports(tmp_path, monkeypatch):
    root = _monorepo(str(tmp_path))

    def fake_run(argv, cwd, log, timeout=None, label=None):
        os.makedirs(os.path.join(cwd, "node_modules", "half"), exist_ok=True)
        return "npm install did not finish in 600 s"

    monkeypatch.setattr(workspace, "_run_blocking", fake_run)
    assert workspace._deploy_prepare(root, root, _Proc()) == \
        "npm install did not finish in 600 s"
    assert not os.path.isdir(os.path.join(root, "frontend", "node_modules"))


def test_prepare_is_a_noop_for_a_plain_single_project(tmp_path, monkeypatch):
    root = str(tmp_path)
    _pkg(root, scripts={"start": "node server.js"})
    calls = []
    monkeypatch.setattr(workspace, "_run_blocking", lambda *a, **k: calls.append(a) or None)
    assert workspace._deploy_prepare(root, root, _Proc()) is None
    assert calls == [] and not os.path.isfile(os.path.join(root, ".env"))


def test_prepare_degrades_when_the_flag_is_off(tmp_path, monkeypatch):
    root = _monorepo(str(tmp_path))
    monkeypatch.setattr(workspace, "_deploy_enabled", lambda: False)
    calls = []
    monkeypatch.setattr(workspace, "_run_blocking", lambda *a, **k: calls.append(a) or None)
    assert workspace._deploy_prepare(root, root, _Proc()) is None
    assert calls == [] and not os.path.isfile(os.path.join(root, ".env"))


# --------------------------------------------------------------------------- #
# workspace.detect disambiguation
# --------------------------------------------------------------------------- #

def test_detect_launches_the_backend_of_a_bare_conventional_pair(tmp_path):
    root = str(tmp_path)
    _pkg(os.path.join(root, "frontend"),
         scripts={"dev": "vite", "build": "vite build"}, dev={"vite": "^6"})
    _pkg(os.path.join(root, "backend"), scripts={"start": "node server.js"},
         deps={"express": "^4"})
    spec = workspace.detect(root)
    assert spec["run_dir"].replace("\\", "/").endswith("/backend")
    assert spec["kind"] == "npm:start"


def test_detect_does_not_guess_between_undeclared_folders(tmp_path):
    root = str(tmp_path)
    _pkg(os.path.join(root, "site"), scripts={"dev": "vite"}, dev={"vite": "^6"})
    _pkg(os.path.join(root, "api-x"), scripts={"start": "node a.js"})
    with pytest.raises(workspace.WorkspaceError) as e:
        workspace.detect(root)
    assert "ambiguous" in str(e.value)


def test_detect_keeps_two_static_sites_ambiguous(tmp_path):
    root = str(tmp_path)
    for name in ("site-a", "site-b"):
        _write(os.path.join(root, name, "index.html"), "<h1>x</h1>")
    with pytest.raises(workspace.WorkspaceError) as e:
        workspace.detect(root)
    assert "ambiguous" in str(e.value)


# --------------------------------------------------------------------------- #
# deploy_check (pure) + the workspace wrapper
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
    assert out["ok"] is False and out["error"] == "ConnectionError"
    assert out["waited"] >= 3


def test_workspace_deploy_check_uses_the_live_url(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "status",
                        lambda d: {"running": True, "url": "http://127.0.0.1:3000"})
    out = workspace.deploy_check(str(tmp_path), probe=lambda u: 200)
    assert out["ok"] is True and out["status"] == 200


def test_workspace_deploy_check_none_when_nothing_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "status", lambda d: {"running": False, "url": None})
    assert workspace.deploy_check(str(tmp_path), probe=lambda u: 200) is None


# --------------------------------------------------------------------------- #
# app status route: report only
# --------------------------------------------------------------------------- #

def test_augment_reports_only_when_asked(tmp_path):
    root = _monorepo(str(tmp_path))
    app._dp_analysis_cache.clear()
    st = {"running": False, "state": "idle", "url": None, "error": None}
    assert app._dp_augment_status(root, st) is st                 # not asked
    out = app._dp_augment_status(root, st, report=True)
    assert "Monorepo" in out["deploy"]["summary"]
    assert out["deploy"]["db"]["engine"] == "postgres"
    assert "deploy" not in st


def test_augment_is_silent_for_a_trivial_folder(tmp_path):
    app._dp_analysis_cache.clear()
    st = {"running": False, "state": "idle", "url": None}
    assert "deploy" not in app._dp_augment_status(str(tmp_path), st, report=True)


def test_status_get_with_deploy_check_runs_nothing(tmp_path, monkeypatch):
    import subprocess
    import urllib.request
    root = _monorepo(str(tmp_path / "proj"))
    calls = []

    def boom(*a, **k):
        calls.append(a)
        raise AssertionError("a status read ran something")

    for mod, name in ((subprocess, "Popen"), (subprocess, "run"),
                      (workspace, "_run_blocking"), (workspace, "start"),
                      (workspace, "install"), (workspace, "deploy_check"),
                      (workspace, "_deploy_prepare"), (urllib.request, "urlopen")):
        monkeypatch.setattr(mod, name, boom)
    monkeypatch.setattr(workspace, "status", lambda d: {
        "running": False, "state": "failed", "url": None, "port": None,
        "kind": None, "log": [], "problems": [], "error": "port 3000 in use"})
    app._dp_analysis_cache.clear()
    with app.app.test_client() as c:
        r = c.get("/api/workspace/status?deploy_check=1&project_dir="
                  + urllib.parse.quote(root), headers=_auth())
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert "Monorepo" in body["deploy"]["summary"]
    assert body["deploy"]["last_error"] == "port 3000 in use"
    assert body["deploy"]["on_start"]
    assert calls == []
    assert not os.path.exists(os.path.join(root, ".env"))          # a read wrote nothing
