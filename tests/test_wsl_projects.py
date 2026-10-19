"""Projects that live inside WSL work for any agent and any distro.

MEASURED 2026-10-19 on the owner's machine: a \\\\wsl.localhost\\Ubuntu\\...
project folder is readable and writable from Windows and the agent CLIs start
in it, but cmd.exe cannot work there (the preview's npm.cmd and the brief's
server recipe), and Windows npm/python would build Windows files into a Linux
project. A live probe of the wrapping: a server started through
`wsl.exe -d Ubuntu --cd /home/... -e bash -lic '...'` answered on Windows'
127.0.0.1 and `taskkill /T` on wsl.exe freed the port; with `--` instead of
`-e` the distro's shell expanded every `$` first. Hermetic: no WSL needed.
"""
import os
import sys

import pytest

import agent_servers
import workspace
import wslpath

UNC = "\\\\wsl.localhost\\Ubuntu\\home\\me\\app"
UNC_OLD = "\\\\wsl$\\Ubuntu-22.04\\srv\\site"


# --------------------------------------------------------------------------- #
# wslpath: any distro, any spelling
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("path, want", [
    (UNC, ("Ubuntu", "/home/me/app")),
    (UNC_OLD, ("Ubuntu-22.04", "/srv/site")),
    ("//wsl.localhost/Debian/home/x", ("Debian", "/home/x")),
    ("\\\\WSL.LOCALHOST\\Ubuntu\\", ("Ubuntu", "/")),
    ("\\\\wsl.localhost\\Ubuntu", ("Ubuntu", "/")),
])
def test_parse_reads_every_wsl_spelling(path, want):
    assert wslpath.parse(path) == want


@pytest.mark.parametrize("path", ["C:\\Users\\me\\app", "\\\\server\\share\\app",
                                  "/home/me/app", "", None])
def test_parse_leaves_other_folders_alone(path):
    assert wslpath.parse(path) is None


def test_npm_runs_in_the_distro_without_a_shell_reparse():
    argv = wslpath.wrap_argv(["C:\\nodejs\\npm.cmd", "install"], UNC)
    assert argv[:7] == ["wsl.exe", "-d", "Ubuntu", "--cd", "/home/me/app", "-e", "bash"]
    assert argv[7] == "-lic"
    assert argv[8] == "exec npm install"


def test_python_maps_to_the_projects_linux_venv_or_python3():
    venv_py = UNC + "\\.venv\\bin\\python"
    assert wslpath.wrap_argv([venv_py, "app.py"], UNC)[-1] == \
        "exec /home/me/app/.venv/bin/python app.py"
    assert wslpath.wrap_argv([UNC + "\\.venv\\Scripts\\python.exe", "-m", "pip"], UNC)[-1] == \
        "exec /home/me/app/.venv/bin/python -m pip"
    assert wslpath.wrap_argv([sys.executable, "-m", "venv", UNC + "\\.venv"], UNC)[-1] == \
        "exec python3 -m venv /home/me/app/.venv"


def test_the_preview_variables_and_venv_ride_along_quoted():
    line = wslpath.command_line(["npm", "run", "dev"], UNC,
                                env={"PORT": "5801", "HOST": "127.0.0.1", "SECRET": "x"},
                                venv=UNC + "\\.venv")
    assert "export PORT=5801" in line and "export HOST=127.0.0.1" in line
    assert "SECRET" not in line                    # only the preview's own keys
    assert "export VIRTUAL_ENV=/home/me/app/.venv" in line
    assert line.endswith("exec npm run dev")


def test_a_normal_folder_is_not_wrapped():
    assert wslpath.wrap_argv(["npm", "install"], "C:\\proj") is None


def test_the_agent_hint_needs_no_cd_argument_git_bash_would_rewrite():
    hint = wslpath.run_hint(UNC, "python3 -m pytest -q")
    assert hint == "wsl -d Ubuntu -e bash -lic 'cd /home/me/app && python3 -m pytest -q'"
    assert "--cd" not in hint


def test_distros_from_the_share_then_from_wsl_list():
    assert wslpath.distros(listdir=lambda p: ["Ubuntu", "docker-desktop", "Debian"],
                           windows=True) == ["Ubuntu", "Debian"]

    class R:
        stdout = "Ubuntu\r\nkali-linux\r\n".encode("utf-16-le")

    def no_share(p):
        raise OSError("no share")
    assert wslpath.distros(listdir=no_share, run=lambda *a, **k: R(), windows=True) == \
        ["Ubuntu", "kali-linux"]
    assert wslpath.distros(windows=False) == []


def test_picker_roots_point_at_each_distros_home():
    rows = wslpath.roots(listdir=lambda p: ["Ubuntu"], windows=True,
                         isdir=lambda p: p.endswith("\\home"))
    assert rows == [{"name": "WSL \u00b7 Ubuntu", "path": "\\\\wsl.localhost\\Ubuntu\\home"}]


# --------------------------------------------------------------------------- #
# workspace: the preview runs the project's tools in its distro
# --------------------------------------------------------------------------- #

def test_setup_commands_run_in_the_distro(monkeypatch):
    seen = {}

    def fake_popen(argv, cwd=None, **kw):
        seen.update(argv=argv, cwd=cwd)
        raise OSError("not really")
    monkeypatch.setattr(workspace.subprocess, "Popen", fake_popen)
    logs = []
    workspace._run_blocking([workspace._npm("npm"), "install"], UNC, logs.append)
    assert seen["argv"][0] == "wsl.exe" and seen["argv"][-1] == "exec npm install"
    assert seen["cwd"] is None                       # wsl --cd sets the folder
    assert any("in WSL" in line for line in logs)


def test_setup_commands_in_a_normal_folder_are_unchanged(monkeypatch, tmp_path):
    seen = {}

    def fake_popen(argv, cwd=None, **kw):
        seen.update(argv=argv, cwd=cwd)
        raise OSError("not really")
    monkeypatch.setattr(workspace.subprocess, "Popen", fake_popen)
    workspace._run_blocking(["npm", "install"], str(tmp_path), lambda l: None)
    assert seen["argv"] == ["npm", "install"] and seen["cwd"] == str(tmp_path)


def test_a_wsl_project_without_a_venv_uses_python3():
    assert workspace._venv_python(UNC) == "python3"


def test_windows_tool_checks_do_not_judge_a_wsl_project():
    assert workspace.missing_tools(UNC) == []


def test_the_picker_offers_wsl_shortcuts(monkeypatch, tmp_path):
    monkeypatch.setattr(workspace, "_wsl_roots",
                        lambda: [{"name": "WSL \u00b7 Ubuntu", "path": UNC}])
    assert workspace.list_dirs(str(tmp_path))["wsl"][0]["path"] == UNC


# --------------------------------------------------------------------------- #
# the agent brief and the helpers' rules
# --------------------------------------------------------------------------- #

def test_the_brief_tells_any_agent_to_work_inside_wsl():
    text = agent_servers.brief_section(pids=[1], port=8787, windows=True, project_dir=UNC)
    assert "This project lives in WSL (Ubuntu: `/home/me/app`)" in text
    assert "wsl -d Ubuntu -e bash -lic 'cd /home/me/app && python3 -m pytest -q'" in text
    assert "dubious" in text and "kill $(cat server.pid)" in text


def test_a_normal_project_brief_is_unchanged():
    a = agent_servers.brief_section(pids=[1], port=8787, windows=True)
    b = agent_servers.brief_section(pids=[1], port=8787, windows=True,
                                    project_dir="C:\\Users\\me\\app")
    assert a == b and "lives in WSL" not in a


def test_helpers_get_the_wsl_rule_too():
    assert agent_servers.worker_rules(pids=[1], port=8787, windows=True,
                                      project_dir=UNC).startswith("THIS PROJECT LIVES IN WSL")
    assert "WSL" not in agent_servers.worker_rules(pids=[1], port=8787, windows=True,
                                                   project_dir="C:\\x")


def test_the_picker_template_draws_the_shortcuts():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(here, "templates", "index.html"), encoding="utf-8").read()
    assert "(r.wsl || []).forEach" in src
