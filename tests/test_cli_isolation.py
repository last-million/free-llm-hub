"""Every CLI the agent chat drives runs the hub's OWN copy, with its OWN config.

"in all cli's that will work there in should be isolated please" -- and it was
only half true. Measured before the fix:

    claude    isolated=False   -> ran the user's global install
    codex     isolated=True
    opencode  isolated=False   -> ran the user's global install

and, worse, NO session ever set a config-directory variable, so even the
isolated codex binary read and wrote the same ~/.codex the user's own terminal
depends on. Isolating the binary but not its credentials isolates nothing that
matters.
"""
import json
import os
import shutil
import tempfile

import pytest

import agentic_chat
import app


DRIVABLE = ("claude", "codex", "opencode")


# --------------------------------------------------------------------------- #
# The copy that runs
# --------------------------------------------------------------------------- #

def test_every_drivable_cli_can_be_isolated():
    for cli in DRIVABLE:
        assert cli in app._ISOLATED_NPM_PACKAGE, "%s has no isolated install" % cli
        assert cli in agentic_chat._ISOLATED_CONFIG_ENV, "%s has no config isolation" % cli


def test_the_isolated_copy_is_preferred_over_the_users_own(monkeypatch):
    monkeypatch.setattr(agentic_chat, "_isolated_bin", lambda c: "/hub/copy/" + c)
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda n: "/usr/bin/" + n)
    assert agentic_chat._resolve_bin("opencode") == "/hub/copy/opencode"


def test_the_users_own_install_is_still_the_fallback(monkeypatch):
    """Nobody has to install anything twice, and a machine where the isolated
    install failed still works."""
    monkeypatch.setattr(agentic_chat, "_isolated_bin", lambda c: None)
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda n: "/usr/bin/" + n)
    assert agentic_chat._resolve_bin("codex") == "/usr/bin/codex"


def test_autoinstall_builds_the_isolated_copy_even_when_a_global_one_exists(monkeypatch):
    """The bug: it asked _resolve_bin ("is there a binary I could run"), which
    answers yes for the GLOBAL install -- so on any machine that already had claude
    or opencode, the isolated copy was never built."""
    built = []
    monkeypatch.setattr(app.config, "get_flag", lambda *a, **k: True)
    monkeypatch.setattr(app, "_ensure_npm", lambda *a, **k: "npm")
    monkeypatch.setattr(app.shutil, "which", lambda n: "/usr/bin/" + str(n))   # global present
    monkeypatch.setattr(app.agentic_chat, "_isolated_bin", lambda c: None)     # isolated absent
    monkeypatch.setattr(app, "_install_global_cli", lambda c, npm=None: (True, {}))
    monkeypatch.setattr(app, "_install_isolated_cli",
                        lambda c, b: (built.append(c), (True, {}))[1])
    app._agent_cli_autoinstall_once()
    for cli in DRIVABLE:
        assert cli in built, "%s never got an isolated copy" % cli


# --------------------------------------------------------------------------- #
# The config it reads
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("cli,var", [("claude", "CLAUDE_CONFIG_DIR"),
                                     ("codex", "CODEX_HOME"),
                                     ("opencode", "XDG_CONFIG_HOME")])
def test_a_session_points_the_cli_at_the_hubs_own_config(monkeypatch, cli, var):
    """opencode's variable was verified live -- it logged
    "loading path=<XDG_CONFIG_HOME>\\opencode\\opencode.json"."""
    monkeypatch.setattr(agentic_chat, "_isolated_bin", lambda c: "/hub/copy/" + c)
    env = agentic_chat._agentic_env(cli)
    assert var in env
    assert env[var].endswith(os.path.join("isolated-clis", cli, "config"))


def test_the_users_config_is_left_alone_when_running_their_own_install(monkeypatch):
    """Moving the config dir of a GLOBAL install would hide the login it
    already has, and the session would fail telling the user to authenticate
    something that is authenticated."""
    monkeypatch.setattr(agentic_chat, "_isolated_bin", lambda c: None)
    env = agentic_chat._agentic_env("claude")
    assert "CLAUDE_CONFIG_DIR" not in env


def test_hub_pointing_variables_are_still_stripped(monkeypatch):
    """The reason this function exists: an agent session must run the user's
    own subscription, not loop back into this hub."""
    monkeypatch.setattr(agentic_chat, "_isolated_bin", lambda c: None)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:8787")
    assert "ANTHROPIC_BASE_URL" not in agentic_chat._agentic_env("claude")


def test_an_unwritable_config_dir_degrades_instead_of_failing(monkeypatch):
    monkeypatch.setattr(agentic_chat, "_isolated_bin", lambda c: "/hub/copy/codex")
    monkeypatch.setattr(agentic_chat.os, "makedirs",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    env = agentic_chat._agentic_env("codex")
    assert "CODEX_HOME" not in env, "a broken isolation must not break the session"


# --------------------------------------------------------------------------- #
# Being signed in is now a separate thing, so say so
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("cli", DRIVABLE)
def test_an_auth_error_says_which_copy_needs_signing_in(cli):
    """"not logged in" about a CLI the user can see IS logged in is true and
    impossible to act on."""
    help_text = agentic_chat._auth_help(cli)
    assert "isolated copy" in help_text
    assert os.path.join("isolated-clis", cli, "config") in help_text
    assert agentic_chat._ISOLATED_CONFIG_ENV[cli] in help_text


# --------------------------------------------------------------------------- #
# opencode brings no provider of its own
# --------------------------------------------------------------------------- #

@pytest.fixture
def cfg_home():
    d = tempfile.mkdtemp(prefix="hubiso-")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_the_isolated_opencode_gets_a_working_provider(cfg_home):
    """claude and codex ARE subscriptions -- isolating them means signing in.
    opencode brings no provider at all, so an empty isolated config means every
    turn dies with ProviderAuthError before any work happens."""
    agentic_chat._seed_opencode_config(cfg_home)
    written = os.path.join(cfg_home, "opencode", "opencode.json")
    assert os.path.isfile(written)
    data = json.load(open(written, encoding="utf-8"))
    assert data["model"].startswith("free-llm-hub/")
    assert "127.0.0.1" in data["provider"]["free-llm-hub"]["options"]["baseURL"]


def test_an_existing_opencode_config_is_never_overwritten(cfg_home):
    """It is the user's file the moment they touch it."""
    target = os.path.join(cfg_home, "opencode", "opencode.json")
    os.makedirs(os.path.dirname(target))
    open(target, "w", encoding="utf-8").write('{"model": "mine/own-model"}')
    agentic_chat._seed_opencode_config(cfg_home)
    assert json.load(open(target, encoding="utf-8"))["model"] == "mine/own-model"


def test_only_opencode_is_seeded(monkeypatch):
    """Seeding claude or codex would point a paid subscription CLI at the hub,
    which is the opposite of what an agent session is for."""
    seeded = []
    monkeypatch.setattr(agentic_chat, "_isolated_bin", lambda c: "/hub/copy/" + c)
    monkeypatch.setattr(agentic_chat, "_seed_opencode_config", lambda p: seeded.append(p))
    agentic_chat._agentic_env("claude")
    agentic_chat._agentic_env("codex")
    assert seeded == []
    agentic_chat._agentic_env("opencode")
    assert len(seeded) == 1
