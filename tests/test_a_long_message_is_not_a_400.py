r"""A message over the cap was refused with a 400 and the turn never ran.

REPORTED: "some requests got http 400, it's like no answer." That is exactly
what it looks like from the outside: paste a long prompt, a stack trace or a
file into the agent and the hub answers 400 instead of doing the work.

The cap was 5600 characters, and every bit of that arithmetic was against
cmd.exe's ~8191-character command line. cmd.exe is no longer in the way: the
launcher resolves the .cmd shim to the real program and runs it directly, and
CreateProcess allows 32,767 characters - four times what the shim did.

MEASURED at the new cap, worst case (turn 1, every optional block live, a full
memory in play):

    claude   26462 of 32767   headroom 6305
    codex    26467 of 32767   headroom 6300
    opencode 26382 of 32767   headroom 6385

The old number stays for the fallback path: a shim this hub cannot read still
goes through cmd.exe, and 5600 is still the true cap there.
"""
import os
import tempfile

import pytest

import agentic_chat as AC
import memory


WINDOWS = os.name == "nt"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(memory._ROOT_ENV, str(tmp_path / "mem"))
    yield


# --------------------------------------------------------------------------- #
# The cap follows the launcher
# --------------------------------------------------------------------------- #

def test_there_are_two_caps_and_the_direct_one_is_bigger():
    assert AC._MAX_MESSAGE_CHARS_DIRECT > AC._MAX_MESSAGE_CHARS


def test_the_direct_cap_leaves_real_headroom():
    """CreateProcess allows 32,767. Everything else on the command line -- the
    binary, the flags, the resume id, the system-prompt addition -- measured
    around 2,500 characters."""
    assert AC._MAX_MESSAGE_CHARS_DIRECT + 3000 < 32767


# The two shim shapes npm writes, copied from the hub's real isolated installs
# (opencode/claude forward straight to a bundled .exe; codex runs node on a .js).
_NPM_PREAMBLE = ("@ECHO off\r\nGOTO start\r\n:find_dp0\r\nSET dp0=%~dp0\r\nEXIT /b\r\n"
                 ":start\r\nSETLOCAL\r\nCALL :find_dp0\r\n")
_NPM_SHIMS = {
    "opencode": ("node_modules/opencode-ai/bin/opencode.exe",
                 '"%dp0%\\node_modules\\opencode-ai\\bin\\opencode.exe"   %*\r\n'),
    "claude": ("node_modules/@anthropic-ai/claude-code/bin/claude.exe",
               '"%dp0%\\node_modules\\@anthropic-ai\\claude-code\\bin\\claude.exe"   %*\r\n'),
    "codex": ("node_modules/@openai/codex/bin/codex.js",
              'IF EXIST "%dp0%\\node.exe" (\r\n  SET "_prog=%dp0%\\node.exe"\r\n) ELSE (\r\n'
              '  SET "_prog=node"\r\n  SET PATHEXT=%PATHEXT:;.JS;=;%\r\n)\r\n\r\n'
              'endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  '
              '"%dp0%\\node_modules\\@openai\\codex\\bin\\codex.js" %*\r\n'),
}


@pytest.mark.skipif(not WINDOWS, reason="the shell path is a Windows problem")
def test_an_installed_cli_gets_the_bigger_cap(tmp_path, monkeypatch):
    """All three resolve to a real .exe (or node plus a script), so none of
    them goes through cmd.exe any more.

    Built from npm-shaped installs in tmp_path rather than whatever this
    machine has on PATH: a real lookup finds the hub's isolated copy on one
    machine, a user's own wrapper .cmd (which is NOT an npm shim, and rightly
    keeps the shell cap) on another, and nothing at all on a third -- where the
    old `if _resolve_bin(cli)` guard made this test assert nothing."""
    install = tmp_path / "install"
    shims = {}
    for cli, (target, forward) in _NPM_SHIMS.items():
        real = install.joinpath(*target.split("/"))
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_bytes(b"")
        shim = install / (cli + ".cmd")
        shim.write_text(_NPM_PREAMBLE + forward, encoding="utf-8", newline="")
        shims[cli] = str(shim)
    # codex's shim runs `node`: give it one, first on PATH, owned by the test.
    node_dir = tmp_path / "nodejs"
    node_dir.mkdir()
    (node_dir / "node.exe").write_bytes(b"")
    monkeypatch.setenv("PATH", str(node_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setattr(AC, "_SHIM_TARGET_CACHE", {})
    monkeypatch.setattr(AC, "_resolve_bin", lambda cli: shims.get(cli))

    for cli in ("opencode", "codex", "claude"):
        argv = AC._launcher(shims[cli])
        assert os.path.basename(argv[0]).lower() not in ("cmd.exe", "cmd"), (cli, argv)
        assert AC.max_message_chars(cli) == AC._MAX_MESSAGE_CHARS_DIRECT, cli


def test_an_unknown_cli_keeps_the_safe_number():
    """Not knowing how something will be launched is not a reason to send it
    four times as much."""
    assert AC.max_message_chars("no-such-cli") == AC._MAX_MESSAGE_CHARS
    assert AC.max_message_chars(None) == AC._MAX_MESSAGE_CHARS


@pytest.mark.skipif(not WINDOWS, reason="the shell path is a Windows problem")
def test_a_shim_that_cannot_be_read_keeps_the_shell_cap(tmp_path, monkeypatch):
    """The fallback still goes through cmd.exe, where 5600 is still true."""
    shim = tmp_path / "tool.cmd"
    shim.write_text("@echo off\r\necho nothing here\r\n", encoding="utf-8")
    AC._SHIM_TARGET_CACHE.clear()
    monkeypatch.setattr(AC, "_resolve_bin", lambda cli: str(shim))
    assert AC.max_message_chars("whatever") == AC._MAX_MESSAGE_CHARS


def test_a_broken_lookup_never_raises(monkeypatch):
    monkeypatch.setattr(AC, "_resolve_bin",
                        lambda cli: (_ for _ in ()).throw(OSError("boom")))
    assert AC.max_message_chars("opencode") == AC._MAX_MESSAGE_CHARS


# --------------------------------------------------------------------------- #
# The command line still fits
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not WINDOWS, reason="the limit under test is Windows'")
def test_a_message_at_the_new_cap_still_fits_the_command_line(monkeypatch, tmp_path):
    """The whole point of raising it: every argv builder, at the new cap, with
    every optional block live and a full memory, under 32,767."""
    monkeypatch.setattr(AC.vision_status, "status",
                        lambda: {"available": False, "providers": []})
    monkeypatch.setattr(AC, "test_verification_enabled", lambda: True)
    long_bin = r"C:\Users\somewhat-long-username\AppData\Roaming\npm\claude.cmd"
    cap = AC._MAX_MESSAGE_CHARS_DIRECT
    text = ("build me a landing page website " + "x" * cap)[:cap]
    for cli, build in (("claude", AC._build_argv),
                       ("codex", AC._build_argv_codex),
                       ("opencode", AC._build_argv_opencode)):
        sess = AC._Session(cli, str(tmp_path))
        sess.native_session_id = None
        memory.remember_summary(sess.id, "y" * memory.MAX_SUMMARY_CHARS)
        for i in range(memory.MAX_FACTS):
            memory.remember_fact(sess.id, "decision %d " % i + "z" * 200)
        cost = sum(len(a) + 3 for a in build(sess, long_bin, text))
        assert cost < 32767, "%s argv is %d chars at the new cap" % (cli, cost)


# --------------------------------------------------------------------------- #
# The refusal itself
# --------------------------------------------------------------------------- #

def test_both_send_paths_use_the_same_cap():
    src = open("agentic_chat.py", encoding="utf-8").read()
    assert src.count("max_message_chars(") >= 3   # the definition plus both uses
    assert "if len(text) > _MAX_MESSAGE_CHARS:" not in src, \
        "a hardcoded cap is a path that does not follow the launcher"


def test_the_refusal_says_the_real_number():
    """A message rejected against a number the hub no longer uses is a message
    rejected for no reason the user can act on."""
    src = open("agentic_chat.py", encoding="utf-8").read()
    i = src.index("Message is %d chars; capped at %d per turn here")
    assert "_cap" in src[i - 200:i + 300]
