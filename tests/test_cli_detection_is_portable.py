r"""The hub finds a CLI the USER installed, not only one on its own PATH.

REPORTED 2026-09-04: "i opened opencode now in terminal and he is connected but
he dont work with our hub", then "should be dynamic in any computer working".

opencode was installed at %APPDATA%\npm\opencode.cmd -- present, working, and
on the user's interactive PATH. The hub could not see it, because shutil.which()
searches the PATH of THIS process, and a hub started from a shortcut, a
scheduled task, a service, or simply a different shell than the one npm
configured does not inherit what a package manager added to the interactive
profile.

The visible symptom was the worst kind: the card said "Not installed (looked
for: opencode)", so the Connect button never appeared, so the provider block was
never written -- while opencode itself looked "connected" because the hub's MCP
server WAS wired in its config. Tools connected, models not.

Detection now falls back to the standard global-install roots. Nothing is
guessed from a project directory; every entry is a per-user or system package
root.
"""
import os
from unittest import mock

import app as A


def test_path_is_still_preferred():
    with mock.patch.object(A.shutil, "which", return_value="/usr/bin/thing"):
        assert A._which_cli("thing") == "/usr/bin/thing"


def test_a_global_install_off_path_is_found(tmp_path):
    """The exact case: npm -g put it somewhere this process's PATH lacks."""
    binary = tmp_path / ("opencode" + (".cmd" if os.name == "nt" else ""))
    binary.write_text("", encoding="utf-8")
    with mock.patch.object(A.shutil, "which", return_value=None), \
         mock.patch.object(A, "_bin_search_dirs", return_value=[str(tmp_path)]):
        assert A._which_cli("opencode") == str(binary)


def test_something_genuinely_absent_is_still_absent(tmp_path):
    with mock.patch.object(A.shutil, "which", return_value=None), \
         mock.patch.object(A, "_bin_search_dirs", return_value=[str(tmp_path)]):
        assert A._which_cli("definitely-not-installed") is None


def test_only_directories_that_exist_are_searched():
    """A missing root must not raise, and must not be reported."""
    for d in A._bin_search_dirs():
        assert os.path.isdir(d), d


def test_windows_shims_are_recognised():
    """An npm-installed CLI on Windows is a .cmd shim, never a bare name."""
    if os.name == "nt":
        assert ".cmd" in A._BIN_EXTS
        assert A._BIN_EXTS[0] == "", "an extensionless match should still win first"
    else:
        assert A._BIN_EXTS == ("",)


def test_the_registry_detector_uses_it():
    """Otherwise the fix exists and nothing calls it."""
    src = open("app.py", encoding="utf-8").read()
    body = src.split("def _cli_installed(", 1)[1]
    body = body[:body.index("\ndef ")]
    assert "_which_cli(b)" in body
    assert "shutil.which(b)" not in body


def test_opencode_connect_offers_every_routing_mode():
    """The isolated /agent copy has always seeded the routing modes; a terminal
    opencode connected from the dashboard used to get only one.

    Asserts the SOURCE now, not a literal tuple. It pinned
    ("auto","best","swarm") -- and that list then fell behind again when the
    category modes arrived, because opencode's /model picker reads its own
    config file and never /v1/models, so an id missing there is invisible
    however well the hub serves it. There are TWO writers of that list -- this
    one and agentic_chat's seed -- and they were separate hardcoded copies.
    app imports agentic_chat and not the reverse, so the list lives there and
    this takes it from there: one direction, one definition."""
    src = open("app.py", encoding="utf-8").read()
    body = src.split("def _autofix_opencode(", 1)[1]
    body = body[:body.index("\ndef ")]
    assert "agentic_chat._opencode_hub_models()" in body
    assert '("auto", "best", "swarm")' not in body, "back to a hand-written list"
