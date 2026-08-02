"""The agent CLIs install themselves, on a machine that has nothing.

Three gaps this covers, all of them "the feature is dead and the user has to go
fix it by hand":

  * opencode was never installed at all -- only claude and codex were.
  * The installs were ISOLATED only, so the hub could drive a CLI the user
    could not type in their own terminal.
  * A machine with no Node was simply skipped, and the agent chat is an npm
    package away from working. Node now installs too -- from an official
    archive into the hub's own folder, because every package-manager route
    prompts (UAC on Windows, sudo on Linux) and nothing can answer a prompt
    raised by a background thread at boot.
"""
import os
import shutil
import sys
import tempfile

import pytest

import app


# --------------------------------------------------------------------------- #
# What gets installed
# --------------------------------------------------------------------------- #

def test_opencode_is_a_cli_the_hub_can_install():
    assert app._ISOLATED_NPM_PACKAGE.get("opencode") == "opencode-ai", (
        "opencode ships as opencode-ai on npm (bin: opencode)")


def test_every_installable_cli_has_a_binary_name():
    for cli_id in app._ISOLATED_NPM_PACKAGE:
        assert cli_id in app._CLI_BIN_NAME, "%s has no binary name to look for" % cli_id


def test_autoinstall_covers_codex_and_opencode(monkeypatch):
    """Both were asked for by name. claude too -- it was already there."""
    seen = {"global": [], "isolated": []}
    monkeypatch.setattr(app.config, "get_flag", lambda *a, **k: True)
    monkeypatch.setattr(app, "_ensure_npm", lambda *a, **k: "npm")
    monkeypatch.setattr(app.shutil, "which", lambda name: None)
    monkeypatch.setattr(app.agentic_chat, "_resolve_bin", lambda cli: None)
    monkeypatch.setattr(app, "_install_global_cli",
                        lambda cli, npm=None: (seen["global"].append(cli), (True, {}))[1])
    monkeypatch.setattr(app, "_install_isolated_cli",
                        lambda cli, b: (seen["isolated"].append(cli), (True, {}))[1])

    app._agent_cli_autoinstall_once()

    for cli in ("codex", "opencode", "claude"):
        assert cli in seen["global"], "%s never installed on the machine" % cli
        assert cli in seen["isolated"], "%s never installed for the hub" % cli


def test_a_cli_already_on_the_machine_is_left_alone(monkeypatch):
    """Installing over someone's existing CLI is not ours to do -- it can move
    them to a version their own work does not expect.

    "Already there" means BOTH copies exist: the user's own, and the hub's
    isolated one. This test used to pass with only the global install present,
    which is precisely the hole that left claude and opencode running the
    user's own credentials -- see test_cli_isolation.py."""
    installed = []
    monkeypatch.setattr(app.config, "get_flag", lambda *a, **k: True)
    monkeypatch.setattr(app, "_ensure_npm", lambda *a, **k: "npm")
    monkeypatch.setattr(app.shutil, "which", lambda name: "/usr/bin/" + str(name))
    monkeypatch.setattr(app.agentic_chat, "_isolated_bin", lambda cli: "/hub/copy/" + cli)
    monkeypatch.setattr(app, "_install_global_cli",
                        lambda cli, npm=None: (installed.append(cli), (True, {}))[1])
    monkeypatch.setattr(app, "_install_isolated_cli",
                        lambda cli, b: (installed.append(cli), (True, {}))[1])

    app._agent_cli_autoinstall_once()
    assert installed == [], "reinstalled a CLI that was already there"


def test_the_isolated_copy_is_still_installed_even_when_the_global_one_fails(monkeypatch):
    """A root-owned global prefix makes `npm install -g` fail with EACCES. That
    must not take the agent chat down with it: the private copy is what the hub
    actually drives."""
    isolated = []
    monkeypatch.setattr(app.config, "get_flag", lambda *a, **k: True)
    monkeypatch.setattr(app, "_ensure_npm", lambda *a, **k: "npm")
    monkeypatch.setattr(app.shutil, "which", lambda name: None)
    monkeypatch.setattr(app.agentic_chat, "_resolve_bin", lambda cli: None)
    monkeypatch.setattr(app, "_install_global_cli",
                        lambda cli, npm=None: (False, {"error": "EACCES"}))
    monkeypatch.setattr(app, "_install_isolated_cli",
                        lambda cli, b: (isolated.append(cli), (True, {}))[1])

    app._agent_cli_autoinstall_once()
    assert "opencode" in isolated and "codex" in isolated


# --------------------------------------------------------------------------- #
# Node, on a machine that has none
# --------------------------------------------------------------------------- #

def test_no_npm_and_no_node_means_node_gets_installed(monkeypatch):
    called = []
    monkeypatch.setattr(app.shutil, "which", lambda name: None)
    monkeypatch.setattr(app, "_npm_in", lambda d: None)
    monkeypatch.setattr(app, "_install_hub_node", lambda: called.append(1) or "npm")
    assert app._ensure_npm() == "npm"
    assert called, "a machine with no Node was just skipped"


def test_the_users_own_npm_always_wins(monkeypatch):
    monkeypatch.setattr(app.shutil, "which", lambda name: "/usr/bin/npm")
    monkeypatch.setattr(app, "_install_hub_node",
                        lambda: pytest.fail("downloaded Node while npm was right there"))
    assert app._ensure_npm() == "/usr/bin/npm"


def test_ensure_npm_can_look_without_installing(monkeypatch):
    """The status/probe path must never trigger a 50MB download as a side
    effect of someone opening a page."""
    monkeypatch.setattr(app.shutil, "which", lambda name: None)
    monkeypatch.setattr(app, "_npm_in", lambda d: None)
    monkeypatch.setattr(app, "_install_hub_node",
                        lambda: pytest.fail("install=False still downloaded Node"))
    assert app._ensure_npm(install=False) is None


@pytest.mark.parametrize("plat,machine,expect", [
    ("win32",  "AMD64",   "node-v22.14.0-win-x64.zip"),
    ("win32",  "ARM64",   "node-v22.14.0-win-arm64.zip"),
    ("darwin", "arm64",   "node-v22.14.0-darwin-arm64.tar.gz"),
    ("darwin", "x86_64",  "node-v22.14.0-darwin-x64.tar.gz"),
    ("linux",  "x86_64",  "node-v22.14.0-linux-x64.tar.xz"),
    ("linux",  "aarch64", "node-v22.14.0-linux-arm64.tar.xz"),
])
def test_the_right_node_build_is_picked_per_platform(monkeypatch, plat, machine, expect):
    monkeypatch.setattr(app.sys, "platform", plat)
    monkeypatch.setattr(app.platform, "machine", lambda: machine)
    assert app._node_archive_name("v22.14.0") == expect


def test_an_unknown_platform_declines_instead_of_guessing(monkeypatch):
    monkeypatch.setattr(app.sys, "platform", "sunos5")
    monkeypatch.setattr(app.platform, "machine", lambda: "sparc")
    assert app._node_archive_name("v22.14.0") is None


def test_node_lands_in_the_hubs_own_folder():
    """Never a system location: no administrator, no PATH surgery, and it goes
    away with the hub's config directory."""
    assert app._hub_node_dir().endswith(os.path.join(".free-llm-hub", "node"))


def test_npm_is_found_inside_an_extracted_node_archive():
    """The archive unpacks into node-vX.Y.Z-<platform>/, one level deeper than
    where we extracted it -- looking only at the top level finds nothing and
    reports a successful install as a failure.

    Own temp dir rather than pytest's tmp_path: its factory raises
    PermissionError on this machine, which is the cause of the suite's known
    errors, and this test needs to actually run."""
    root = tempfile.mkdtemp(prefix="hubnode-")
    try:
        inner = os.path.join(root, "node-v22.14.0-linux-x64", "bin")
        os.makedirs(inner)
        npm = os.path.join(inner, "npm")
        open(npm, "w").write("#!/bin/sh\n")
        assert app._npm_in(root) == npm
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_the_node_version_is_not_hardcoded():
    """A pinned version rots: nodejs.org eventually drops it and the bootstrap
    starts 404ing. The pin is only the fallback."""
    src = open(app.__file__, encoding="utf-8", errors="replace").read()
    assert "nodejs.org/dist/index.json" in src, "no lookup of the current LTS"
    assert "_NODE_FALLBACK_VERSION" in src, "no fallback when the index is unreachable"


@pytest.mark.skipif(sys.version_info < (3, 12), reason="tarfile filter= is 3.12+")
def test_tar_extraction_refuses_traversal_entries():
    """An archive is untrusted input even from a good host; filter='data'
    rejects absolute paths and ../ entries instead of writing them."""
    src = open(app.__file__, encoding="utf-8", errors="replace").read()
    assert '"filter": "data"' in src
