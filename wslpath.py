"""Projects that live inside WSL, for any agent and any project.

A Build / Multi project folder may be a WSL path seen from Windows:
`\\\\wsl.localhost\\<distro>\\home\\me\\app` (or the older `\\\\wsl$\\<distro>\\...`).
Windows programs can read and write it, and the agent CLIs start in it, but
cmd.exe cannot use it as a working folder (it falls back to C:\\Windows), and
a Windows `npm.cmd` / `python.exe` would build Windows artefacts in a Linux
project. So anything the HUB runs in such a folder is run INSIDE the distro:

    wsl.exe -d <distro> --cd <linux path> -- bash -lic '<command>'

MEASURED 2026-10-19 on the owner's machine: `os.path.isdir`, `realpath` and
`listdir` keep the UNC form; agent CLIs launch directly (no cmd.exe) in it.

Pure stdlib, never raises; every outside effect is a parameter.
"""
import os
import re
import shlex
import subprocess

_UNC_RE = re.compile(r"^[\\/]{2}(wsl\.localhost|wsl\$)[\\/]+([^\\/]+)(?:[\\/]+(.*))?$",
                     re.IGNORECASE)
# What the preview exports to the dev server (workspace._env_for). Nothing
# else of the hub's Windows environment is passed into the distro.
ENV_KEYS = ("PORT", "VITE_PORT", "FLASK_RUN_PORT", "HOST", "BROWSER", "CI",
            "FORCE_COLOR", "NO_COLOR")
_TOOLS = {"npm", "npx", "pnpm", "yarn", "bun", "node", "pip", "pip3", "uv",
          "git", "make", "cargo", "go", "deno"}


def parse(path):
    """(distro, linux_path) for a path inside a WSL distro, else None."""
    try:
        m = _UNC_RE.match(str(path or "").strip())
    except Exception:                                            # noqa: BLE001
        return None
    if not m:
        return None
    distro = m.group(2)
    rest = (m.group(3) or "").replace("\\", "/").strip("/")
    return distro, "/" + rest if rest else "/"


def is_wsl(path):
    return parse(path) is not None


def _same_distro_linux(path, distro):
    p = parse(path)
    if p and p[0].lower() == distro.lower():
        return p[1]
    return None


def _base(token):
    t = re.split(r"[\\/]", str(token or ""))[-1].lower()
    for ext in (".exe", ".cmd", ".bat", ".ps1"):
        if t.endswith(ext):
            return t[: -len(ext)]
    return t


def _map_program(arg, distro):
    """The Linux program for a Windows argv[0]."""
    linux = _same_distro_linux(arg, distro)
    if linux:
        # A venv interpreter inside the project: Scripts/python.exe -> bin/python.
        linux = re.sub(r"/Scripts/python(?:w)?(?:\.exe)?$", "/bin/python", linux,
                       flags=re.IGNORECASE)
        return linux
    b = _base(arg)
    if b in _TOOLS:
        return b
    if re.match(r"^(?:python|pythonw|py)(?:\d+(?:\.\d+)?)?$", b):
        return "python3"
    return arg


def command_line(argv, cwd, env=None, env_keys=ENV_KEYS, venv=None):
    """The bash command (a string) that runs `argv` inside the distro of `cwd`:
    the Windows program mapped to its Linux name, project paths made Linux
    paths, the preview's variables exported, the project's venv on PATH."""
    p = parse(cwd)
    if not p or not argv:
        return None
    distro = p[0]
    words = [_map_program(argv[0], distro)]
    for a in argv[1:]:
        words.append(_same_distro_linux(a, distro) or str(a))
    exports = []
    for k in env_keys:
        if env and env.get(k) not in (None, ""):
            exports.append("export %s=%s" % (k, shlex.quote(str(env[k]))))
    if venv:
        lv = _same_distro_linux(venv, distro)
        if lv:
            exports.append('export VIRTUAL_ENV=%s PATH=%s/bin:"$PATH"'
                           % (shlex.quote(lv), shlex.quote(lv)))
    return "; ".join(exports + ["exec " + shlex.join(words)])


def wrap_argv(argv, cwd, env=None, env_keys=ENV_KEYS, venv=None):
    """argv that runs `argv` INSIDE the WSL distro of `cwd`, in its Linux
    folder. A login + interactive bash so tools installed for the user (nvm,
    pyenv, ~/.local/bin) are on PATH as in the user's own terminal. Returns
    None when `cwd` is not a WSL path."""
    p = parse(cwd)
    if not p:
        return None
    line = command_line(argv, cwd, env=env, env_keys=env_keys, venv=venv)
    if line is None:
        return None
    # -e: no Linux shell re-parses the line (with `--` the default shell does,
    # and expands every `$` before bash -lic ever sees it).
    return ["wsl.exe", "-d", p[0], "--cd", p[1], "-e", "bash", "-lic", line]


def run_hint(path, command="<command>"):
    """How an agent runs one command in this WSL project, as one line a bash
    or PowerShell tool accepts (wsl.exe is a real program). `-e` so no Linux
    shell re-parses it, and the folder inside the quoted command, not as a
    `--cd /home/...` argument: Git Bash rewrites a bare /home/... argument into
    a Windows path before wsl.exe sees it."""
    p = parse(path)
    if not p:
        return None
    return "wsl -d %s -e bash -lic %s" % (
        p[0], shlex.quote("cd %s && %s" % (shlex.quote(p[1]), command)))


def distros(listdir=os.listdir, run=subprocess.run, windows=None):
    """Names of the installed WSL distributions ([] off Windows or when WSL is
    absent). Reads the \\\\wsl.localhost share first, then `wsl -l -q`."""
    if windows is None:
        windows = os.name == "nt"
    if not windows:
        return []
    names = []
    try:
        names = [n for n in listdir("\\\\wsl.localhost\\") if n and not n.startswith(".")]
    except Exception:                                            # noqa: BLE001
        names = []
    if not names:
        try:
            r = run(["wsl.exe", "-l", "-q"], capture_output=True, timeout=8,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            raw = r.stdout or b""
            text = raw.decode("utf-16-le", "replace") if b"\x00" in raw else raw.decode("utf-8", "replace")
            names = [n.strip().strip("\x00") for n in text.splitlines()]
            names = [n for n in names if n]
        except Exception:                                        # noqa: BLE001
            names = []
    seen, out = set(), []
    for n in names:
        if n.lower() not in seen and not n.lower().startswith("docker-desktop"):
            seen.add(n.lower())
            out.append(n)
    return out


def roots(listdir=os.listdir, run=subprocess.run, windows=None, isdir=os.path.isdir):
    """Folder-picker shortcuts: one per distro, at its /home when it has one."""
    out = []
    for d in distros(listdir=listdir, run=run, windows=windows):
        base = "\\\\wsl.localhost\\%s" % d
        home = base + "\\home"
        try:
            path = home if isdir(home) else base + "\\"
        except Exception:                                        # noqa: BLE001
            path = base + "\\"
        out.append({"name": "WSL \u00b7 %s" % d, "path": path})
    return out
