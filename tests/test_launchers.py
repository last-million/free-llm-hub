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
