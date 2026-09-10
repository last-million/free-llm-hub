r"""cmd.exe treats a newline as a command separator, so half of every
multi-line prompt never reached the agent.

FOUND 2026-09-10, by reading what two swarm workers actually said. Both were
marked done and both had spent their whole turn asking a question:

    "Your message got cut off after 'two public functions:' -- the function
     names/signatures never arrived"
    "Your message cuts off after the colon -- which two files should I create?"

Their stored tasks were complete. The truncation happened on delivery: an
npm-installed CLI on Windows is a .cmd shim, CreateProcess cannot run a batch
file, so the launcher went through `cmd.exe /c shim.cmd <prompt>` -- and
cmd.exe ends the command at a newline, whether or not it sits inside quotes.

MEASURED through that exact path, with a real .cmd shim and a child that dumps
its argv: 111 characters in, 59 out. Everything after the first line was gone.

What it silently broke:
  * a swarm phase task -- the planner writes numbered lists;
  * any /agent message typed with a line break;
  * opencode's standing instruction, appended after a blank line, so it never
    arrived at all;
  * claude's --append-system-prompt, whose parts are joined with blank lines,
    so only the first snippet was ever delivered.

The shim is a two-line batch file whose only job is to run a real program. Run
that program directly and there is no shell left to lose anything.
"""
import json
import os
import subprocess
import sys

import pytest

import agentic_chat as AC


WINDOWS = os.name == "nt"

SHIM_EXE = (
    "@ECHO off\r\n"
    "GOTO start\r\n"
    ":find_dp0\r\n"
    "SET dp0=%~dp0\r\n"
    "EXIT /b\r\n"
    ":start\r\n"
    "SETLOCAL\r\n"
    "CALL :find_dp0\r\n"
    '"%dp0%\\node_modules\\thing\\bin\\thing.exe"   %*\r\n'
)

SHIM_NODE = (
    "@ECHO off\r\n"
    'endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  '
    '"%dp0%\\node_modules\\@scope\\pkg\\bin\\cli.js" %*\r\n'
)

CHILD = ("import sys, json, io\n"
         "io.open(sys.argv[1], 'w', encoding='utf-8')"
         ".write(json.dumps(sys.argv[2:]))\n")

MULTILINE = ("Create a file. It must define exactly two public functions:\n\n"
             "1. slugify(text)\n2. truncate(text, n)\nEnd of task.")


def _shim(tmp_path, body, target_rel):
    """A shim plus the file it points at, so the resolver has something real."""
    shim = tmp_path / "tool.cmd"
    shim.write_text(body, encoding="utf-8")
    target = tmp_path / target_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x", encoding="utf-8")
    AC._SHIM_TARGET_CACHE.clear()
    return str(shim), str(target)


# --------------------------------------------------------------------------- #
# The measurement that started it
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not WINDOWS, reason="cmd.exe is a Windows problem")
def test_cmd_exe_really_does_eat_the_rest(tmp_path):
    """The premise, run rather than asserted. If this ever stops being true,
    the fix below is no longer needed -- and this test will say so."""
    child = tmp_path / "child.py"
    child.write_text(CHILD, encoding="utf-8")
    shim = tmp_path / "shim.cmd"
    shim.write_text('@echo off\r\n"%s" "%%~dp0child.py" %%*\r\n' % sys.executable,
                    encoding="utf-8")
    out = tmp_path / "got.json"
    subprocess.run([os.environ.get("COMSPEC") or "cmd.exe", "/c", str(shim),
                    str(out), MULTILINE], capture_output=True)
    got = json.loads(out.read_text(encoding="utf-8"))
    assert got == [MULTILINE[:MULTILINE.index("\n")]], got


@pytest.mark.skipif(not WINDOWS, reason="cmd.exe is a Windows problem")
def test_running_the_program_directly_keeps_all_of_it(tmp_path):
    child = tmp_path / "child.py"
    child.write_text(CHILD, encoding="utf-8")
    out = tmp_path / "got.json"
    subprocess.run([sys.executable, str(child), str(out), MULTILINE],
                   capture_output=True)
    assert json.loads(out.read_text(encoding="utf-8")) == [MULTILINE]


# --------------------------------------------------------------------------- #
# The resolver
# --------------------------------------------------------------------------- #

def test_it_finds_the_exe_behind_the_shim(tmp_path):
    shim, target = _shim(tmp_path, SHIM_EXE, "node_modules/thing/bin/thing.exe")
    assert AC._resolve_shim(shim) == [os.path.normpath(target)]


def test_it_finds_node_and_the_script(tmp_path, monkeypatch):
    monkeypatch.setattr(AC.shutil, "which",
                        lambda name: "C:\\node\\node.exe" if name == "node" else None)
    shim, target = _shim(tmp_path, SHIM_NODE, "node_modules/@scope/pkg/bin/cli.js")
    assert AC._resolve_shim(shim) == ["C:\\node\\node.exe", os.path.normpath(target)]


def test_a_shim_pointing_at_nothing_falls_back(tmp_path):
    """An unreadable shim must keep working the way it did, not fail the turn."""
    shim = tmp_path / "tool.cmd"
    shim.write_text(SHIM_EXE, encoding="utf-8")     # target never created
    AC._SHIM_TARGET_CACHE.clear()
    assert AC._resolve_shim(str(shim)) is None


def test_a_shim_with_no_forwarding_line_falls_back(tmp_path):
    shim = tmp_path / "tool.cmd"
    shim.write_text("@echo off\r\necho nothing here\r\n", encoding="utf-8")
    AC._SHIM_TARGET_CACHE.clear()
    assert AC._resolve_shim(str(shim)) is None


def test_a_variable_we_cannot_expand_falls_back(tmp_path):
    shim = tmp_path / "tool.cmd"
    shim.write_text('@echo off\r\n"%SOMETHING_ELSE%\\x.exe" %*\r\n', encoding="utf-8")
    AC._SHIM_TARGET_CACHE.clear()
    assert AC._resolve_shim(str(shim)) is None


def test_a_missing_file_is_not_an_error(tmp_path):
    AC._SHIM_TARGET_CACHE.clear()
    assert AC._resolve_shim(str(tmp_path / "does-not-exist.cmd")) is None


def test_the_answer_is_cached(tmp_path):
    """_launcher runs on every turn; re-reading and re-stat-ing the shim every
    time is filesystem work for an answer that cannot change."""
    shim, _ = _shim(tmp_path, SHIM_EXE, "node_modules/thing/bin/thing.exe")
    AC._resolve_shim(shim)
    assert os.path.abspath(shim) in AC._SHIM_TARGET_CACHE


# --------------------------------------------------------------------------- #
# The launcher
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not WINDOWS, reason="only .cmd shims go through a shell")
def test_the_launcher_uses_the_program_not_the_shell(tmp_path):
    shim, target = _shim(tmp_path, SHIM_EXE, "node_modules/thing/bin/thing.exe")
    argv = AC._launcher(shim)
    assert argv == [os.path.normpath(target)]
    assert not any("cmd.exe" in a.lower() for a in argv)


@pytest.mark.skipif(not WINDOWS, reason="only .cmd shims go through a shell")
def test_an_unresolvable_shim_still_runs(tmp_path):
    shim = tmp_path / "tool.cmd"
    shim.write_text(SHIM_EXE, encoding="utf-8")
    AC._SHIM_TARGET_CACHE.clear()
    argv = AC._launcher(str(shim))
    assert argv[0].lower().endswith("cmd.exe") and argv[1] == "/c"


def test_a_real_binary_is_untouched(tmp_path):
    exe = tmp_path / "tool.exe"
    exe.write_text("x", encoding="utf-8")
    assert AC._launcher(str(exe)) == [str(exe)]


def test_the_caller_cannot_mutate_the_cache(tmp_path):
    """_launcher hands its list straight to argv builders that append to it."""
    shim, _ = _shim(tmp_path, SHIM_EXE, "node_modules/thing/bin/thing.exe")
    first = AC._launcher(shim)
    first.append("--poison")
    assert "--poison" not in AC._launcher(shim)


# --------------------------------------------------------------------------- #
# What it was breaking
# --------------------------------------------------------------------------- #

def test_the_standing_instruction_is_multi_line():
    """opencode gets it inlined after a blank line, so on the old path it was
    always the part that disappeared."""
    add = AC._system_prompt_addition("build a landing page", has_brief=True)
    assert "\n" in add


def test_a_swarm_task_is_multi_line():
    import swarm_windows as SW
    run = SW._Run("g", ".", "opencode",
                  [{"title": "a", "task": "do it", "needs": []},
                   {"title": "b", "task": "and this", "needs": []}])
    assert "\n" in SW._agent_prompt(run, run.agents[0])
