"""One clickable file per platform, and it works on a bare machine.

Two things a newcomer hits before any code runs: which file do I start, and do
I have to install anything first. Both had the wrong answer -- the root shipped
`run.bat` AND `autostart.bat` side by side (a coin flip for anyone who did not
write them), and a machine with no Python got told to go install Python.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _root_files(ext):
    return sorted(f for f in os.listdir(ROOT)
                  if f.lower().endswith(ext) and os.path.isfile(os.path.join(ROOT, f)))


def test_the_project_root_holds_exactly_one_clickable_file_per_platform():
    assert _root_files(".bat") == ["run.bat"], (
        "two .bat files in the root is a coin flip for whoever downloads this")
    assert _root_files(".sh") == ["run.sh"], (
        "two .sh files in the root is a coin flip for whoever downloads this")


def test_the_second_launcher_still_exists_as_a_subcommand():
    """Moved, not deleted: autostart is real functionality, it just must not
    compete with the start button."""
    assert os.path.isfile(os.path.join(ROOT, "scripts", "autostart.bat"))
    assert os.path.isfile(os.path.join(ROOT, "scripts", "autostart.sh"))


def test_run_bat_forwards_autostart_to_the_moved_script():
    bat = open(os.path.join(ROOT, "run.bat"), encoding="utf-8", errors="replace").read()
    assert re.search(r'if /i "%~1"=="autostart"', bat), "no autostart subcommand"
    assert r"scripts\autostart.bat" in bat, "forwards nowhere"


def test_run_sh_forwards_autostart_to_the_moved_script():
    sh = open(os.path.join(ROOT, "run.sh"), encoding="utf-8", errors="replace").read()
    assert "autostart)" in sh, "no autostart subcommand"
    assert "./scripts/autostart.sh" in sh, "forwards nowhere"


def test_the_moved_scripts_climb_back_to_the_project_root():
    """Everything they install (logon launcher, unit file, plist) points at the
    ROOT. Living in scripts/ without climbing out would write paths one folder
    too deep -- installed, and broken."""
    bat = open(os.path.join(ROOT, "scripts", "autostart.bat"), encoding="utf-8",
               errors="replace").read()
    assert r'cd /d "%~dp0.."' in bat
    sh = open(os.path.join(ROOT, "scripts", "autostart.sh"), encoding="utf-8",
              errors="replace").read()
    assert 'cd "$(dirname "$0")/.."' in sh


def test_windows_installs_python_when_the_machine_has_none():
    bat = open(os.path.join(ROOT, "run.bat"), encoding="utf-8", errors="replace").read()
    assert ":install_python" in bat, "no install path: a bare machine just gets an error"
    assert "winget install" in bat, "winget is the cheapest route and ships with Windows"
    assert "python.org/ftp/python/" in bat, "no fallback when winget is absent"
    assert "InstallAllUsers=0" in bat, "a per-user install is what keeps this admin-free"
    assert "PrependPath=1" in bat, "installed but not on PATH is the same as not installed"


def test_windows_looks_past_PATH_after_installing():
    """A fresh install does not reach the PATH of the shell that ran it, so
    checking PATH alone makes a SUCCESSFUL install look like a failed one."""
    bat = open(os.path.join(ROOT, "run.bat"), encoding="utf-8", errors="replace").read()
    assert r"%LOCALAPPDATA%\Programs\Python\Python3*" in bat


def test_windows_rejects_the_microsoft_store_stub():
    """python.exe from the Store answers `where` and does nothing but open the
    Store. Finding it and calling it Python hangs the launcher on a shopfront."""
    bat = open(os.path.join(ROOT, "run.bat"), encoding="utf-8", errors="replace").read()
    assert 'python -c "import sys"' in bat


def test_unix_installs_python_with_whatever_package_manager_is_present():
    sh = open(os.path.join(ROOT, "run.sh"), encoding="utf-8", errors="replace").read()
    for mgr in ("apt-get", "dnf", "pacman", "zypper", "apk", "brew"):
        assert mgr in sh, "no install route for %s" % mgr
    assert "python3-venv" in sh, (
        "Debian ships venv separately: python3 exists while `python3 -m venv` fails")
    assert "brew install python" in sh and "$SUDO brew" not in sh, (
        "Homebrew refuses to run under sudo")


# --------------------------------------------------------------------------- #
# Auto-persist: a plain start survives closing the window, without the user
# ever having to know "autostart" exists.
#
# MEASURED 2026-08-08: a real deployment kept a cmd window open at all times --
# closing it silently killed the hub, and nobody watching it knew that was
# even the cause. autostart already solved this, but only for someone who
# already knew to run it. This makes a normal double-click self-heal too.
# --------------------------------------------------------------------------- #

def test_windows_auto_persists_on_first_successful_start():
    bat = open(os.path.join(ROOT, "run.bat"), encoding="utf-8", errors="replace").read()
    assert ":maybe_autopersist" in bat
    assert "call :maybe_autopersist" in bat
    assert r"scripts\autostart.bat" in bat.split(":maybe_autopersist", 1)[1]


def test_windows_auto_persist_fires_from_both_the_fresh_start_and_the_already_running_branch():
    """The most common case (re-run/double-click while the hub is already up)
    exits at the double-bind guard, before the python/venv checks -- without a
    call there too, that case would never auto-persist."""
    bat = open(os.path.join(ROOT, "run.bat"), encoding="utf-8", errors="replace").read()
    assert bat.count("call :maybe_autopersist") == 2


def test_windows_auto_persist_is_marker_gated_not_every_start():
    bat = open(os.path.join(ROOT, "run.bat"), encoding="utf-8", errors="replace").read()
    body = bat[bat.index(":maybe_autopersist"):]
    assert "AUTOSTART_MARKER" in body
    assert 'if not exist "%AUTOSTART_MARKER%"' in body


def test_unix_auto_persists_on_first_successful_start():
    sh = open(os.path.join(ROOT, "run.sh"), encoding="utf-8", errors="replace").read()
    assert "maybe_autopersist()" in sh
    assert sh.count("maybe_autopersist") >= 3, (
        "definition + two call sites (fresh start, already-running branch)")
    assert "./scripts/autostart.sh" in sh.split("maybe_autopersist()", 1)[1]


def test_unix_auto_persist_is_marker_gated_and_set_e_safe():
    sh = open(os.path.join(ROOT, "run.sh"), encoding="utf-8", errors="replace").read()
    body = sh[sh.index("maybe_autopersist()"):]
    assert "autostart-auto-installed" in body
    assert 'if [ ! -f "$marker" ]; then' in body
    # set -e is at the top of this file; a bare `./scripts/autostart.sh` that
    # fails (no systemd, unsupported platform) would take the WHOLE launcher
    # down without the `if ...; then` guard around it.
    assert "if ./scripts/autostart.sh" in body


def test_auto_persist_never_touches_saved_state():
    """The whole feature exists to protect a running hub -- it must never be
    able to delete or overwrite ~/.free-llm-hub/'s config, usage or quota
    data. Ban destructive commands from the subroutine bodies entirely."""
    bat = open(os.path.join(ROOT, "run.bat"), encoding="utf-8", errors="replace").read()
    bat_body = bat[bat.index(":maybe_autopersist"):bat.index(":usage")]
    for banned in ("del ", "rmdir", "Remove-Item"):
        assert banned not in bat_body

    sh = open(os.path.join(ROOT, "run.sh"), encoding="utf-8", errors="replace").read()
    sh_body = sh[sh.index("maybe_autopersist()"):sh.index("maybe_autopersist()") + 800]
    for banned in ("rm -rf", "rm -f $CONFIG", ">$CONFIG_FILE"):
        assert banned not in sh_body


# --------------------------------------------------------------------------- #
# Detached start: closing the launcher window must never kill the hub, not
# even for the ~5 minutes self-heal takes to notice.
#
# MEASURED 2026-08-09: closing the visible window running `python app.py` in
# the foreground killed the hub instantly. Fix: relaunch through
# run-hidden.vbs (the same mechanism the logon launcher and self-heal already
# use) and let the visible window exit.
# --------------------------------------------------------------------------- #

def test_windows_relaunches_detached_instead_of_running_in_the_foreground():
    bat = open(os.path.join(ROOT, "run.bat"), encoding="utf-8", errors="replace").read()
    assert 'if "%HUB_DETACHED%"=="1"' in bat, "no guard against re-detaching forever"
    assert 'start "" wscript.exe "%~dp0run-hidden.vbs"' in bat


def test_windows_detach_guard_is_set_before_relaunching():
    """HUB_DETACHED must be set BEFORE the relaunch, not after -- it travels
    to the child through Windows environment inheritance, so setting it too
    late (or not at all) would recurse forever instead of running python."""
    bat = open(os.path.join(ROOT, "run.bat"), encoding="utf-8", errors="replace").read()
    set_idx = bat.index('set "HUB_DETACHED=1"')
    relaunch_idx = bat.index('start "" wscript.exe "%~dp0run-hidden.vbs"')
    assert set_idx < relaunch_idx


def test_windows_the_detached_branch_actually_runs_python():
    """The guard exists to short-circuit into the real launch, not just to
    exist -- confirm `python app.py` sits inside the HUB_DETACHED=1 branch."""
    bat = open(os.path.join(ROOT, "run.bat"), encoding="utf-8", errors="replace").read()
    branch = bat[bat.index('if "%HUB_DETACHED%"=="1"'):bat.index('rem --- relaunch hidden')]
    assert "python app.py" in branch


def test_hidden_launcher_uses_a_full_path_not_a_bare_filename():
    """MEASURED 2026-08-09: a bare `call run.bat` here silently found nothing
    on a machine with NoDefaultCurrentDirectoryInExePath=1 set (a real
    Windows hardening setting) -- that excludes the current directory from
    cmd.exe's search for a bare executable name. wscript.exe's fire-and-forget
    Run() still exited 0 regardless, so this failure was invisible everywhere:
    not in the visible window (there isn't one), not in Task Scheduler's
    LastTaskResult (reports the launcher's own exit code, not the inner
    command's), not anywhere. A full path sidesteps cmd.exe's search
    behaviour entirely rather than depending on getting it right."""
    vbs = open(os.path.join(ROOT, "run-hidden.vbs"), encoding="utf-8", errors="replace").read()
    assert 'batPath = here & "\\run.bat"' in vbs
    run_lines = [ln for ln in vbs.splitlines()
                if ln.strip().startswith("sh.Run")]
    assert len(run_lines) == 2, "supervised and explicit-start branches, both"
    for ln in run_lines:
        assert "call run.bat" not in ln, "must never call it by bare name again: %r" % ln
        assert "batPath" in ln
