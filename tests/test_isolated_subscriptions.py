"""Tests for the OPT-IN isolated Claude Code / Codex install profile layered
on top of the existing _SUB_PROVIDERS / _sub_bin / _sub_env / _sub_state
system. Never runs a real `npm install` or a real CLI subprocess -- every
subprocess boundary is monkeypatched.
"""
import json
import os

import pytest

import app
import config


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    path = tmp_path / "state" / "config.json"
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(path))
    # Redirect _home() so isolated-CLI dirs land under tmp_path, never the
    # real user home directory.
    monkeypatch.setattr(app, "_home", lambda: str(tmp_path))
    app._runtime_active[0] = 0
    app._runtime_shutdown_thread[0] = None
    app._runtime_server[0] = None
    return tmp_path


def _make_iso_bin(tmp_path, cli_id, bin_name):
    """Drop a dummy 'installed' binary where npm --prefix's POSIX layout would
    put it (<install>/bin/<name>); _isolated_bin_path searches this AND the
    Windows-style <install> root, so this one location covers both checks."""
    install_bin_dir = tmp_path / ".free-llm-hub" / "isolated-clis" / cli_id / "install" / "bin"
    install_bin_dir.mkdir(parents=True, exist_ok=True)
    name = bin_name + (".cmd" if os.name == "nt" else "")
    p = install_bin_dir / name
    p.write_text("echo stub\n", encoding="utf-8")
    if os.name != "nt":
        p.chmod(0o755)
    return str(p)


# --------------------------------------------------------------------------- #
# path helpers -- pure, no side effects except _ensure_isolated_dirs
# --------------------------------------------------------------------------- #

def test_isolated_dirs_scoped_under_free_llm_hub(isolated_config):
    d = app._isolated_cli_dir("codex")
    assert str(isolated_config) in d
    assert d.endswith(os.path.join(".free-llm-hub", "isolated-clis", "codex"))


def test_isolated_cli_dir_is_pure_no_mkdir(isolated_config):
    app._isolated_install_dir("codex")
    app._isolated_config_dir("codex")
    assert not os.path.exists(app._isolated_cli_dir("codex"))


def test_ensure_isolated_dirs_creates_install_and_config(isolated_config):
    app._ensure_isolated_dirs("codex")
    assert os.path.isdir(app._isolated_install_dir("codex"))
    assert os.path.isdir(app._isolated_config_dir("codex"))


def test_isolated_bin_path_finds_dummy_binary(isolated_config):
    bin_path = _make_iso_bin(isolated_config, "codex", "codex")
    found = app._isolated_bin_path("codex", "codex")
    assert found and os.path.samefile(found, bin_path)


def test_isolated_bin_path_none_when_not_installed(isolated_config):
    assert app._isolated_bin_path("codex", "codex") is None


# --------------------------------------------------------------------------- #
# _sub_isolated_on / _sub_bin -- default OFF, additive only
# --------------------------------------------------------------------------- #

def test_sub_isolated_on_default_false(isolated_config):
    assert app._sub_isolated_on("sub-claude") is False
    assert app._sub_isolated_on("sub-codex") is False


def test_sub_bin_default_behavior_unchanged(isolated_config, monkeypatch):
    """Isolated OFF -> _sub_bin must still be a plain shutil.which(name) call,
    with no `path=` kwarg -- byte-identical to the pre-isolation function."""
    calls = []

    def fake_which(name, path=None):
        calls.append((name, path))
        return "/usr/bin/" + name

    monkeypatch.setattr(app.shutil, "which", fake_which)
    result = app._sub_bin("sub-codex")
    assert result == "/usr/bin/codex"
    assert calls == [("codex", None)]


def test_sub_bin_isolated_scopes_to_isolated_dir_only(isolated_config):
    config.set_flag("sub_codex_isolated", True)
    assert app._sub_bin("sub-codex") is None   # not installed in isolation yet
    bin_path = _make_iso_bin(isolated_config, "codex", "codex")
    found = app._sub_bin("sub-codex")
    assert found and os.path.samefile(found, bin_path)


# --------------------------------------------------------------------------- #
# _codex_subscription_auth -- default path byte-identical, isolated scoped
# --------------------------------------------------------------------------- #

def test_codex_auth_default_messages_unchanged(isolated_config):
    ok, detail = app._codex_subscription_auth()
    assert ok is False
    assert detail == "Not signed in (no ~/.codex/auth.json). Run: codex login"


def test_codex_auth_isolated_reads_isolated_path(isolated_config):
    conf_dir = app._isolated_config_dir("codex")
    os.makedirs(conf_dir, exist_ok=True)
    with open(os.path.join(conf_dir, "auth.json"), "w", encoding="utf-8") as f:
        json.dump({"auth_mode": "chatgpt"}, f)
    ok, detail = app._codex_subscription_auth(conf_dir)
    assert ok is True
    assert "ChatGPT subscription" in detail


# --------------------------------------------------------------------------- #
# _sub_env -- default identical; isolated sets CODEX_HOME / CLAUDE_CONFIG_DIR
# --------------------------------------------------------------------------- #

def test_sub_env_default_strips_hub_pointing_vars_only(isolated_config, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:%d/v1" % app.PORT)
    env = app._sub_env()
    assert "ANTHROPIC_BASE_URL" not in env
    assert "CODEX_HOME" not in env
    assert "CLAUDE_CONFIG_DIR" not in env


def test_sub_env_pid_none_ignores_isolated_flag(isolated_config):
    config.set_flag("sub_codex_isolated", True)
    env = app._sub_env()   # no pid -> byte-identical to the legacy call
    assert "CODEX_HOME" not in env


def test_sub_env_isolated_sets_config_dir_var(isolated_config):
    config.set_flag("sub_codex_isolated", True)
    env = app._sub_env("sub-codex")
    assert env.get("CODEX_HOME") == app._isolated_config_dir("codex")
    assert os.path.isdir(env["CODEX_HOME"])


def test_sub_env_isolated_off_for_pid_no_env_var_set(isolated_config):
    env = app._sub_env("sub-codex")   # isolated flag still False
    assert "CODEX_HOME" not in env


# --------------------------------------------------------------------------- #
# _sub_state -- loop-guard fix: isolated profile must not be blocked by the
# SHARED CLI's connection status (the main reason to want isolation at all).
# --------------------------------------------------------------------------- #

def test_sub_state_isolated_bypasses_shared_loop_guard(isolated_config, monkeypatch):
    _make_iso_bin(isolated_config, "codex", "codex")
    conf_dir = app._isolated_config_dir("codex")
    os.makedirs(conf_dir, exist_ok=True)
    with open(os.path.join(conf_dir, "auth.json"), "w", encoding="utf-8") as f:
        json.dump({"auth_mode": "chatgpt"}, f)
    # Simulate the user's SHARED ~/.codex CLI being connected to this hub.
    monkeypatch.setattr(app, "_sub_loops_back",
                        lambda cli_id: (True, "shared CLI points at the hub"))

    config.set_flag("sub_codex_isolated", False)
    _enabled, _installed, authed, detail = app._sub_state("sub-codex")
    assert authed is False and "points at the hub" in (detail or "")

    config.set_flag("sub_codex_isolated", True)
    _enabled, _installed, authed, _detail = app._sub_state("sub-codex")
    assert authed is True   # isolated profile is NOT blocked by the shared loop-guard


def test_sub_state_isolated_not_installed_message(isolated_config):
    config.set_flag("sub_codex_isolated", True)
    _enabled, installed, _authed, detail = app._sub_state("sub-codex")
    assert installed is False
    assert "isolated copy" in (detail or "").lower()


# --------------------------------------------------------------------------- #
# _isolated_login_command
# --------------------------------------------------------------------------- #

def test_isolated_login_command_none_when_not_installed(isolated_config):
    cmd, note = app._isolated_login_command("sub-codex")
    assert cmd is None
    assert "install" in (note or "").lower()


def test_isolated_login_command_codex_includes_login_subcommand(isolated_config):
    _make_iso_bin(isolated_config, "codex", "codex")
    cmd, note = app._isolated_login_command("sub-codex")
    assert note is None
    assert "CODEX_HOME" in cmd
    assert cmd.rstrip().endswith("login")


def test_isolated_login_command_claude_has_no_login_subcommand(isolated_config):
    _make_iso_bin(isolated_config, "claude", "claude")
    cmd, note = app._isolated_login_command("sub-claude")
    assert note is None
    assert "CLAUDE_CONFIG_DIR" in cmd
    assert not cmd.rstrip().endswith("login")


# --------------------------------------------------------------------------- #
# /api/subscriptions -- payload shape + isolated toggle
# --------------------------------------------------------------------------- #

def test_api_subscriptions_get_includes_isolated_fields(isolated_config):
    client = app.app.test_client()
    resp = client.get("/api/subscriptions")
    assert resp.status_code == 200
    rows = {r["id"]: r for r in resp.get_json()["providers"]}
    for pid in ("sub-claude", "sub-codex"):
        assert rows[pid]["isolated"] is False
        assert rows[pid]["isolated_supported"] is True
        assert rows[pid]["isolated_installed"] is False
        assert rows[pid]["isolated_login_command"] is None


def test_api_subscriptions_post_toggles_isolated_flag(isolated_config):
    client = app.app.test_client()
    resp = client.post("/api/subscriptions",
                       json={"provider": "sub-codex", "isolated": True},
                       headers={"X-Free-LLM-Hub": "dashboard"})
    assert resp.status_code == 200
    assert config.get_flag("sub_codex_isolated", False) is True
    rows = {r["id"]: r for r in resp.get_json()["providers"]}
    assert rows["sub-codex"]["isolated"] is True


def test_api_subscriptions_post_enabled_flag_still_works(isolated_config):
    """The original {"provider", "enabled"} shape must still work unchanged."""
    client = app.app.test_client()
    resp = client.post("/api/subscriptions",
                       json={"provider": "sub-codex", "enabled": False},
                       headers={"X-Free-LLM-Hub": "dashboard"})
    assert resp.status_code == 200
    assert config.get_flag("sub_codex_enabled", True) is False


def test_api_subscriptions_post_requires_enabled_or_isolated(isolated_config):
    client = app.app.test_client()
    resp = client.post("/api/subscriptions", json={"provider": "sub-codex"},
                       headers={"X-Free-LLM-Hub": "dashboard"})
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# /api/subscriptions/<pid>/install-isolated -- NEVER a real npm install
# --------------------------------------------------------------------------- #

def test_api_install_isolated_unknown_provider(isolated_config):
    client = app.app.test_client()
    resp = client.post("/api/subscriptions/sub-bogus/install-isolated",
                       headers={"X-Free-LLM-Hub": "dashboard"})
    assert resp.status_code == 400


def test_api_install_isolated_npm_missing(isolated_config, monkeypatch):
    monkeypatch.setattr(app.shutil, "which",
                        lambda name, path=None: None if name == "npm" else "/usr/bin/" + name)
    client = app.app.test_client()
    resp = client.post("/api/subscriptions/sub-codex/install-isolated",
                       headers={"X-Free-LLM-Hub": "dashboard"})
    assert resp.status_code == 400
    assert "npm" in resp.get_json()["error"].lower()


def _patch_npm_which(monkeypatch):
    """Fake `npm` resolving on PATH while leaving real shutil.which() in
    charge of resolving the (also faked-installed) isolated binary."""
    real_which = app.shutil.which

    def fake_which(name, path=None):
        if name == "npm" and path is None:
            return "/usr/bin/npm"
        return real_which(name, path=path)

    monkeypatch.setattr(app.shutil, "which", fake_which)


def test_api_install_isolated_success_never_runs_real_npm(isolated_config, monkeypatch):
    _patch_npm_which(monkeypatch)

    class FakeProc:
        returncode = 0
        stdout = "+ @openai/codex@1.0.0"
        stderr = ""

    def fake_run(argv, **kwargs):
        assert "install" in argv and "-g" in argv and "@openai/codex" in argv
        # simulate npm having actually placed the binary
        _make_iso_bin(isolated_config, "codex", "codex")
        return FakeProc()

    monkeypatch.setattr(app.subprocess, "run", fake_run)

    client = app.app.test_client()
    resp = client.post("/api/subscriptions/sub-codex/install-isolated",
                       headers={"X-Free-LLM-Hub": "dashboard"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert "bin_path" in body


def test_api_install_isolated_nonzero_exit_surfaces_stderr(isolated_config, monkeypatch):
    _patch_npm_which(monkeypatch)

    class FakeProc:
        returncode = 1
        stdout = ""
        stderr = "npm ERR! network timeout"

    monkeypatch.setattr(app.subprocess, "run", lambda argv, **kwargs: FakeProc())

    client = app.app.test_client()
    resp = client.post("/api/subscriptions/sub-codex/install-isolated",
                       headers={"X-Free-LLM-Hub": "dashboard"})
    assert resp.status_code == 502
    assert "network timeout" in resp.get_json()["error"]


def test_api_install_isolated_timeout_surfaces_clearly(isolated_config, monkeypatch):
    _patch_npm_which(monkeypatch)

    def fake_run(argv, **kwargs):
        raise app.subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(app.subprocess, "run", fake_run)

    client = app.app.test_client()
    resp = client.post("/api/subscriptions/sub-codex/install-isolated",
                       headers={"X-Free-LLM-Hub": "dashboard"})
    assert resp.status_code == 504
    assert "timed out" in resp.get_json()["error"].lower()


def test_api_install_isolated_success_but_binary_missing(isolated_config, monkeypatch):
    """npm exits 0 but no binary is found afterward -- must be surfaced, not
    reported as ok."""
    _patch_npm_which(monkeypatch)

    class FakeProc:
        returncode = 0
        stdout = "installed"
        stderr = ""

    monkeypatch.setattr(app.subprocess, "run", lambda argv, **kwargs: FakeProc())

    client = app.app.test_client()
    resp = client.post("/api/subscriptions/sub-codex/install-isolated",
                       headers={"X-Free-LLM-Hub": "dashboard"})
    assert resp.status_code == 502
    assert resp.get_json()["ok"] is False
