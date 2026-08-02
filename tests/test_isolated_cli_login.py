"""One-click sign-in for an isolated CLI, instead of "copy this PowerShell
line into a terminal yourself" -- asked for directly: "it should work in
http://127.0.0.1:8787/agent/", isolation kept.

Isolation on purpose means a SEPARATE, initially-empty credential store from
the CLI the user already uses by hand. That is not a bug -- confirmed on disk
in the reported case: the user's own real codex has a working auth.json, the
isolated copy has none anywhere in its tree. It still needs signing in once;
these changes make that one click instead of a copy-pasted command.
"""
import os
import shutil
import tempfile

import pytest

import agentic_chat as ac
import app


@pytest.fixture
def client():
    app.app.config["TESTING"] = True
    with app.app.test_client() as c:
        yield c


def _dash(client, method, url, **kw):
    headers = {"X-Free-LLM-Hub": "dashboard",
               "X-Free-LLM-Hub-Token": app.config.ensure_control_token()}
    return getattr(client, method)(url, headers=headers, **kw)


# --------------------------------------------------------------------------- #
# Knowing whether the isolated copy is actually signed in
# --------------------------------------------------------------------------- #

def test_the_real_credential_filenames_are_correct():
    """Measured against the user's OWN real, working installs: claude keeps
    .credentials.json, codex keeps auth.json."""
    assert ac._ISOLATED_CREDENTIAL_FILE["claude"] == ".credentials.json"
    assert ac._ISOLATED_CREDENTIAL_FILE["codex"] == "auth.json"


def test_signed_in_is_true_when_the_credential_file_exists(monkeypatch):
    d = tempfile.mkdtemp(prefix="hublogin-")
    try:
        monkeypatch.setattr(ac, "_isolated_config_dir", lambda cli: d)
        assert ac._isolated_signed_in("codex") is False
        open(os.path.join(d, "auth.json"), "w").write("{}")
        assert ac._isolated_signed_in("codex") is True
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_opencode_never_needs_signing_in():
    """It is never a subscription for the hub's purposes -- its isolated copy
    is seeded with the hub's own free models (see _seed_opencode_config)."""
    assert ac._isolated_signed_in("opencode") is True


def test_cli_support_reports_signed_in_per_cli(monkeypatch):
    monkeypatch.setattr(ac, "_isolated_bin", lambda cli: "/hub/copy/" + cli)
    monkeypatch.setattr(ac, "_isolated_signed_in", lambda cli: cli == "opencode")
    info = ac.cli_support()
    assert info["opencode"]["signed_in"] is True
    assert info["claude"]["signed_in"] is False
    assert info["codex"]["signed_in"] is False


def test_a_non_isolated_session_is_never_reported_as_not_signed_in(monkeypatch):
    """Falling back to the user's OWN global install (isolated copy missing or
    failed) must not show a false "not signed in" warning about a CLI that is
    already working."""
    monkeypatch.setattr(ac, "_isolated_bin", lambda cli: None)
    monkeypatch.setattr(ac.shutil, "which", lambda name: "/usr/bin/" + str(name))
    info = ac.cli_support()
    assert info["claude"]["signed_in"] is True
    assert info["codex"]["signed_in"] is True


# --------------------------------------------------------------------------- #
# Launching the login flow
# --------------------------------------------------------------------------- #

def test_login_args_match_each_clis_own_login_flow():
    """claude has no login-only flag (confirmed against --help) -- a bare
    launch is what already walks a fresh profile through login."""
    assert ac._LOGIN_ARGS["claude"] == []
    assert ac._LOGIN_ARGS["codex"] == ["login"]
    assert ac._LOGIN_ARGS["opencode"] == ["auth", "login"]


def test_launch_refuses_an_uninstalled_cli(monkeypatch):
    monkeypatch.setattr(ac, "_isolated_bin", lambda cli: None)
    ok, detail = ac.launch_isolated_login("codex")
    assert ok is False
    assert "not installed" in detail


def test_launch_refuses_an_unknown_cli():
    ok, detail = ac.launch_isolated_login("not-a-real-cli")
    assert ok is False


def test_launch_opens_a_real_visible_window_on_windows(monkeypatch):
    """CREATE_NO_WINDOW is used everywhere else this hub shells out -- the
    opposite of what a login prompt (an OAuth tab, a device code, a paste-
    your-key prompt) needs. This is the one path that must NOT hide it."""
    calls = []
    monkeypatch.setattr(ac, "_isolated_bin", lambda cli: "/hub/copy/codex")
    monkeypatch.setattr(ac.os, "name", "nt")
    monkeypatch.setattr(ac.subprocess, "CREATE_NEW_CONSOLE", 0x10, raising=False)
    monkeypatch.setattr(ac.subprocess, "Popen",
                        lambda argv, **kw: calls.append((argv, kw)))
    ok, detail = ac.launch_isolated_login("codex")
    assert ok is True
    argv, kw = calls[0]
    assert argv[-1] == "login"
    assert kw.get("creationflags") == 0x10


def test_the_hub_never_captures_login_output():
    """The credentials are typed INTO that window, not relayed through the
    hub -- confirmed by construction: no stdout=PIPE/stderr=PIPE anywhere in
    the launch path."""
    import inspect
    src = inspect.getsource(ac.launch_isolated_login)
    assert "PIPE" not in src


def test_a_spawn_failure_is_reported_not_swallowed(monkeypatch):
    monkeypatch.setattr(ac, "_isolated_bin", lambda cli: "/hub/copy/codex")
    monkeypatch.setattr(ac.os, "name", "nt")

    def boom(*a, **k):
        raise OSError("no such file")
    monkeypatch.setattr(ac.subprocess, "Popen", boom)
    ok, detail = ac.launch_isolated_login("codex")
    assert ok is False and "no such file" in detail


# --------------------------------------------------------------------------- #
# The route
# --------------------------------------------------------------------------- #

def test_the_login_route_exists_and_is_gated(client):
    was = ac.master_enabled()
    try:
        ac.set_master_enabled(False)
        r = _dash(client, "post", "/api/agent/clis/codex/login")
        assert r.status_code == 403
    finally:
        ac.set_master_enabled(was)


def test_the_login_route_calls_launch_isolated_login(client, monkeypatch):
    was = ac.master_enabled()
    seen = []
    try:
        ac.set_master_enabled(True)
        monkeypatch.setattr(app.agentic_chat, "launch_isolated_login",
                            lambda cli: (seen.append(cli), (True, None))[1])
        r = _dash(client, "post", "/api/agent/clis/codex/login")
        assert r.status_code == 200
        assert r.get_json()["ok"] is True
        assert seen == ["codex"]
    finally:
        ac.set_master_enabled(was)


def test_the_login_route_reports_a_launch_failure(client, monkeypatch):
    was = ac.master_enabled()
    try:
        ac.set_master_enabled(True)
        monkeypatch.setattr(app.agentic_chat, "launch_isolated_login",
                            lambda cli: (False, "boom"))
        r = _dash(client, "post", "/api/agent/clis/codex/login")
        assert r.status_code == 400
        assert r.get_json()["error"] == "boom"
    finally:
        ac.set_master_enabled(was)


# --------------------------------------------------------------------------- #
# The frontend offers it in both places it is needed
# --------------------------------------------------------------------------- #

def test_the_picker_note_offers_sign_in_when_isolated_and_not_signed_in():
    html = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "templates", "index.html"), encoding="utf-8").read()
    assert "info.isolated && info.signed_in === false" in html
    assert "doAgentCliLogin" in html
    assert "/login'" in html or '/login"' in html


def test_the_live_chat_error_bubble_also_offers_sign_in():
    """The picker note is easy to miss once a session is already open -- the
    SAME button belongs right where the failure was actually seen."""
    html = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "templates", "index.html"), encoding="utf-8").read()
    assert "cli_not_signed_in" in html
    block = html[html.index("ev.code === 'cli_not_signed_in'"):]
    block = block[:block.index("}\n        }")]
    assert "doAgentCliLogin" in block
