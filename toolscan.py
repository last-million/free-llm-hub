"""Which developer CLIs are installed here -- natively AND inside WSL -- and,
where an official status command exists, whether the user is logged in
(2026-10-29).

WHY: the owner wants the hub to work with ANY developer CLI (Shopify, Vercel,
gh, Stripe, Supabase, Netlify, Firebase, Wrangler, AWS, gcloud, Docker,
Terraform ...): a model that knows `vercel` is installed but not logged in asks
the user to run `vercel login` instead of hanging on an interactive prompt, and
a model that knows `gh` is logged in uses it instead of guessing.

CHEAP AND SAFE:
  * `shutil.which` + ONE time-boxed version call per installed tool, all at
    once on worker threads; only the parsed version NUMBER is kept.
  * LOGIN STATE only where the CLI's own docs name a non-interactive status
    command whose EXIT CODE says logged in / not logged in. The command's
    output is never read: natively it goes to DEVNULL, inside WSL to
    /dev/null. Only a boolean is kept (None = unknown: no such command, a
    timeout, a crash). Such output can carry e-mail addresses and tokens.
  * WSL: only distributions that are ALREADY RUNNING are looked into (a scan
    must never boot a WSL VM every ten minutes). The last WSL result is kept
    while the distro is stopped.
  * Cached TTL seconds and refreshed in the background: a request path never
    waits (`snapshot(wait=0)` returns what is cached, possibly None).

Every side (which / run / os name / clock) is injectable. The module switch
ENABLED lets the test suite keep every real command out (tests/conftest.py).
Pure stdlib, never raises.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import threading
import time

ENABLED = True
TTL = 600.0
VERSION_TIMEOUT = 6.0
LOGIN_TIMEOUT = 10.0
SCAN_DEADLINE = 30.0
WSL_LIST_TIMEOUT = 6.0
WSL_TIMEOUT = 45.0
MAX_WSL_DISTROS = 2
BLOCK_MAX = 350
MARKER = "CLI TOOLS HERE:"

# Environment for every scan call: no colours, no prompts, no update notices.
_SCAN_ENV = {"NO_COLOR": "1", "CI": "1", "GH_PROMPT_DISABLED": "1",
             "NO_UPDATE_NOTIFIER": "1", "AWS_PAGER": "", "TERM": "dumb"}

# --------------------------------------------------------------------------- #
# THE TOOL LIST (data-driven). Per tool:
#   exe      executables tried in order (Windows resolves .cmd/.exe via which)
#   version  argv after the executable that prints the version
#   login    argv of the official non-interactive status command whose exit
#            code is 0 when logged in, non-zero when not -- or None. Only
#            commands verified in the vendor's docs on 2026-10-29 (see the
#            `src` comment on each); everything else stays None = "unknown".
#   login_cmd  what the USER runs to log in (we never run it).
#   hint     one line: how to run it non-interactively (from the vendor's docs).
# --------------------------------------------------------------------------- #

TOOLS = (
    # src: cli.github.com/manual/gh_auth_status -- authentication issues exit 1
    # (with --json it always exits 0, so it is NOT used); gh_help_environment.
    {"name": "gh", "exe": ("gh",), "version": ("--version",),
     "login": ("auth", "status"), "login_cmd": "gh auth login",
     "hint": "gh: --json <fields> for data; GH_PROMPT_DISABLED=1, GH_TOKEN for CI"},
    # src: vercel.com/docs/cli/whoami + vercel/vercel whoami/index.ts -- no token
    # returns 1; vercel.com/docs/cli/global-options.
    {"name": "vercel", "exe": ("vercel",), "version": ("--version",),
     "login": ("whoami",), "login_cmd": "vercel login",
     "hint": "vercel: --non-interactive, --token or VERCEL_TOKEN, --no-color"},
    # src: cli.netlify.com/commands/status + netlify/cli status.ts -- with --json
    # not logged in exits 1 (plain `netlify status` exits 0: NOT used).
    {"name": "netlify", "exe": ("netlify",), "version": ("--version",),
     "login": ("status", "--json"), "login_cmd": "netlify login",
     "hint": "netlify: --json; --auth <token> or NETLIFY_AUTH_TOKEN"},
    # src: shopify.dev/docs/api/shopify-cli -- `auth login`/`auth logout`, no
    # status command.
    {"name": "shopify", "exe": ("shopify",), "version": ("version",),
     "login": None, "login_cmd": "shopify auth login",
     "hint": "shopify: give every value as a flag; no global non-interactive flag"},
    # src: docs.stripe.com/cli/login/list -- exit code not documented: unknown.
    {"name": "stripe", "exe": ("stripe",), "version": ("--version",),
     "login": None, "login_cmd": "stripe login",
     "hint": "stripe: --api-key, --color off; never print secret keys"},
    # src: supabase.com/docs/reference/cli -- `projects list` exit code not
    # confirmed: unknown.
    {"name": "supabase", "exe": ("supabase",), "version": ("--version",),
     "login": None, "login_cmd": "supabase login",
     "hint": "supabase: --yes; SUPABASE_ACCESS_TOKEN for CI"},
    # src: firebase-tools login-list.ts -- exits 0 with no account: NOT usable.
    {"name": "firebase", "exe": ("firebase",), "version": ("--version",),
     "login": None, "login_cmd": "firebase login",
     "hint": "firebase: read `firebase <command> --help` for its flags"},
    # src: developers.cloudflare.com/workers/wrangler/commands/general --
    # `whoami --json` "exits with a non-zero status if not authenticated".
    {"name": "wrangler", "exe": ("wrangler",), "version": ("--version",),
     "login": ("whoami", "--json"), "login_cmd": "wrangler login",
     "hint": "wrangler: --json where offered"},
    # src: docs.aws.amazon.com/cli/latest/reference/sts/get-caller-identity.html
    # + userguide/cli-usage-returncodes.html -- missing credentials exit 253.
    {"name": "aws", "exe": ("aws",), "version": ("--version",),
     "login": ("sts", "get-caller-identity"), "login_cmd": "aws configure sso",
     # 254 / 255: a service or network error, not "not logged in".
     "unknown_rc": (254, 255),
     "hint": "aws: --output json --no-cli-pager"},
    # src: docs.cloud.google.com/sdk/gcloud/reference/auth/list -- no-account
    # exit code not documented: unknown.
    {"name": "gcloud", "exe": ("gcloud",), "version": ("--version",),
     "login": None, "login_cmd": "gcloud auth login",
     "hint": "gcloud: --quiet --format=json"},
    # src: learn.microsoft.com/cli/azure/account#az-account-show + azure-cli
    # _profile.py -- no cached subscription exits 1 (local cache only).
    {"name": "az", "exe": ("az",), "version": ("version",),
     "login": ("account", "show"), "login_cmd": "az login",
     "hint": "az: --output json --only-show-errors"},
    # src: heroku/cli auth/whoami.ts -- not logged in exits 100.
    {"name": "heroku", "exe": ("heroku",), "version": ("--version",),
     "login": ("auth:whoami",), "login_cmd": "heroku login",
     "hint": "heroku: -j / --json on most commands"},
    # src: docs.railway.com/cli/whoami + railwayapp/cli main.rs -- no token exits 1.
    {"name": "railway", "exe": ("railway",), "version": ("--version",),
     "login": ("whoami",), "login_cmd": "railway login",
     "hint": "railway: --json; RAILWAY_TOKEN for CI"},
    # src: docs.fly.io/flyctl/auth-whoami + superfly/flyctl whoami.go -- with
    # --json it never prompts and returns ErrNoAuthToken (an error exit).
    {"name": "flyctl", "exe": ("flyctl", "fly"), "version": ("version",),
     "login": ("auth", "whoami", "--json"), "login_cmd": "fly auth login",
     "hint": "fly: -j / --json; -t / --access-token"},
    # No login concept the hub can read (docker keeps credentials in a store).
    {"name": "docker", "exe": ("docker",), "version": ("--version",),
     "login": None, "login_cmd": "",
     "hint": "docker: --format '{{json .}}'; never -it"},
    {"name": "kubectl", "exe": ("kubectl",), "version": ("version", "--client"),
     "login": None, "login_cmd": "", "hint": "kubectl: -o json"},
    {"name": "terraform", "exe": ("terraform",), "version": ("version", "-json"),
     "login": None, "login_cmd": "",
     "hint": "terraform: -json output; apply only after the user's yes"},
    {"name": "pnpm", "exe": ("pnpm",), "version": ("--version",),
     "login": None, "login_cmd": "", "hint": "pnpm: --reporter=append-only"},
    {"name": "bun", "exe": ("bun",), "version": ("--version",),
     "login": None, "login_cmd": "", "hint": ""},
    {"name": "deno", "exe": ("deno",), "version": ("--version",),
     "login": None, "login_cmd": "", "hint": ""},
    {"name": "uv", "exe": ("uv",), "version": ("--version",),
     "login": None, "login_cmd": "", "hint": "uv: --no-progress, -q"},
)

_NAMES = tuple(t["name"] for t in TOOLS)
_BY_NAME = {t["name"]: t for t in TOOLS}
_VERSION_RE = re.compile(r"(?<![\w.])v?(\d+\.\d+(?:\.\d+)?)")


def parse_version(text):
    """The first dotted version number in `text`, or None. Never raises."""
    if not isinstance(text, str) or not text.strip():
        return None
    m = _VERSION_RE.search(text.replace("\r", ""))
    return m.group(1) if m else None


def _scan_env():
    env = dict(os.environ)
    env.update(_SCAN_ENV)
    return env


def _default_run(argv, timeout, output=True):
    """(returncode, text) or None. output=False: stdout and stderr go to
    DEVNULL and text is "" (the login checks -- their output is never read).
    No console window, no stdin, no shell."""
    kwargs = {"stdin": subprocess.DEVNULL, "timeout": timeout, "env": _scan_env()}
    if output:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    else:
        kwargs.update(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        cp = subprocess.run(list(argv), **kwargs)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not output:
        return cp.returncode, ""
    out = (cp.stdout or b"") + b"\n" + (cp.stderr or b"")
    return cp.returncode, out.decode("utf-8", errors="replace")


def _default_which(exe):
    return shutil.which(exe)


def _login_state(run, argv, timeout, unknown_rc=()):
    """True / False from the exit code alone, None when unknown (no answer, or
    an exit code the tool's docs give to an error that is not about login).
    The output is NEVER requested (output=False)."""
    try:
        got = run(list(argv), timeout, output=False)
    except Exception:                                            # noqa: BLE001
        return None
    if not got or not isinstance(got, tuple):
        return None
    rc = got[0]
    if not isinstance(rc, int) or rc in unknown_rc:
        return None
    return rc == 0


def _scan_native(which, run):
    found = {}

    def one(tool):
        try:
            path = None
            for exe in tool["exe"]:
                try:
                    path = which(exe)
                except Exception:                                # noqa: BLE001
                    path = None
                if path:
                    break
            if not path:
                return
            version = None
            got = run([path] + list(tool["version"]), VERSION_TIMEOUT)
            if got and isinstance(got, tuple):
                version = parse_version(got[1])
            row = {"version": version, "login": None}
            if tool.get("login"):
                row["login"] = _login_state(run, [path] + list(tool["login"]),
                                            LOGIN_TIMEOUT, tool.get("unknown_rc") or ())
            found[tool["name"]] = row
        except Exception:                                        # noqa: BLE001
            return

    jobs = [threading.Thread(target=one, args=(t,), daemon=True) for t in TOOLS]
    for j in jobs:
        j.start()
    end = time.monotonic() + SCAN_DEADLINE
    for j in jobs:
        j.join(max(0.0, end - time.monotonic()))
    return {n: dict(found[n]) for n in _NAMES if n in found}


# --------------------------------------------------------------------------- #
# WSL: one bash script per RUNNING distro. Each installed tool prints
#   T|<name>|<first line of its version output>
# and, when it has a status command, that command runs with its output sent to
# /dev/null and only its exit code is printed:
#   L|<name>|<exit code>
# --------------------------------------------------------------------------- #

def wsl_script():
    exports = " ".join("%s=%s" % (k, shlex.quote(v)) for k, v in sorted(_SCAN_ENV.items()))
    lines = ["export %s" % exports]
    for t in TOOLS:
        name = t["name"]
        exes = " ".join(shlex.quote(e) for e in t["exe"])
        ver = " ".join(shlex.quote(a) for a in t["version"])
        body = ['v=$(timeout %d "$p" %s </dev/null 2>&1 | head -n 1 | tr -d "\\r|")'
                % (int(VERSION_TIMEOUT), ver),
                'echo "T|%s|$v"' % name]
        if t.get("login"):
            la = " ".join(shlex.quote(a) for a in t["login"])
            body += ['timeout %d "$p" %s </dev/null >/dev/null 2>&1; echo "L|%s|$?"'
                     % (int(LOGIN_TIMEOUT), la, name)]
        lines.append("p=; for e in %s; do p=$(command -v \"$e\" 2>/dev/null) && break; done; "
                     "if [ -n \"$p\" ]; then %s; fi" % (exes, "; ".join(body)))
    return "\n".join(lines) + "\n"


def parse_wsl_output(text):
    """{name: {"version", "login"}} from the script's T|/L| lines. Exit codes
    124 (timeout) and 127 (not found) are unknown, not 'not logged in'."""
    out = {}
    if not isinstance(text, str):
        return out
    for raw in text.replace("\r", "").splitlines():
        parts = raw.strip().split("|", 2)
        if len(parts) != 3 or parts[1] not in _BY_NAME:
            continue
        kind, name, val = parts
        if kind == "T":
            out.setdefault(name, {"version": None, "login": None})
            out[name]["version"] = parse_version(val)
        elif kind == "L" and name in out and _BY_NAME[name].get("login"):
            try:
                rc = int(val.strip())
            except ValueError:
                continue
            unknown = (124, 125, 126, 127, 137) + tuple(_BY_NAME[name].get("unknown_rc") or ())
            out[name]["login"] = None if rc in unknown else rc == 0
    return {n: out[n] for n in _NAMES if n in out}


def running_distros(run):
    """Names of the WSL distributions running right now ([] when none, or
    when WSL is absent). Never starts one."""
    got = None
    try:
        got = run(["wsl.exe", "-l", "--running", "-q"], WSL_LIST_TIMEOUT, output=True)
    except Exception:                                            # noqa: BLE001
        got = None
    if not got or not isinstance(got, tuple) or got[0] != 0:
        return []
    text = got[1] or ""
    if isinstance(text, bytes):
        text = text.decode("utf-16-le" if b"\x00" in text else "utf-8", "replace")
    names = []
    for n in text.replace("\x00", "").splitlines():
        n = n.strip()
        if n and not n.lower().startswith("docker-desktop") and n not in names \
                and re.match(r"^[\w.\-]+$", n):
            names.append(n)
    return names[:MAX_WSL_DISTROS]


def _scan_wsl(run):
    found = {}
    for distro in running_distros(run):
        try:
            # Login + interactive bash, like wslpath.wrap_argv: nvm and
            # ~/.local/bin tools are only on PATH that way (measured there).
            got = run(["wsl.exe", "-d", distro, "-e", "bash", "-lic", wsl_script()],
                      WSL_TIMEOUT, output=True)
        except Exception:                                        # noqa: BLE001
            got = None
        if got and isinstance(got, tuple):
            rows = parse_wsl_output(got[1])
            if rows:
                found[distro] = rows
    return found


def _os_is_windows():
    return os.name == "nt"


def scan(*, which=None, run=None, windows=None, previous=None):
    """One fresh look -> {"at", "native": {name: row}, "wsl": {distro: {name: row}}}.
    A row is {"version": str|None, "login": True|False|None}. Only INSTALLED
    tools appear. `previous` keeps a WSL distro's last result while it is not
    running now. Never raises."""
    which = which or _default_which
    run = run or _default_run
    if windows is None:
        windows = _os_is_windows()
    try:
        native = _scan_native(which, run)
    except Exception:                                            # noqa: BLE001
        native = {}
    wsl = {}
    if windows:
        try:
            wsl = _scan_wsl(run)
        except Exception:                                        # noqa: BLE001
            wsl = {}
        prev = (previous or {}).get("wsl") if isinstance(previous, dict) else None
        if isinstance(prev, dict):
            for d, rows in prev.items():
                if d not in wsl and isinstance(rows, dict):
                    wsl[d] = rows
    return {"at": time.time(), "native": native, "wsl": wsl}


# --------------------------------------------------------------------------- #
# Cache: refreshed in the background, never waited on by a request path
# --------------------------------------------------------------------------- #

_LOCK = threading.Lock()
_STATE = {"snap": None, "at": 0.0, "running": False, "done": threading.Event()}


def reset():
    with _LOCK:
        _STATE.update(snap=None, at=0.0, running=False, done=threading.Event())


def _refresh(ev):
    try:
        with _LOCK:
            prev = _STATE["snap"]
        snap = scan(previous=prev)
    except Exception:                                            # noqa: BLE001
        snap = None
    with _LOCK:
        if snap is not None:
            _STATE["snap"], _STATE["at"] = snap, time.monotonic()
        _STATE["running"] = False
    ev.set()


def _start_refresh_locked():
    _STATE["running"] = True
    _STATE["done"] = threading.Event()
    threading.Thread(target=_refresh, args=(_STATE["done"],), daemon=True,
                     name="toolscan").start()


def snapshot(wait=0.0):
    """The cached scan (refreshing it in the background when older than TTL),
    or None. `wait` > 0 waits up to that many seconds for a refresh in
    progress -- never on a request path. Never raises."""
    if not ENABLED:
        return None
    try:
        with _LOCK:
            snap, at = _STATE["snap"], _STATE["at"]
            fresh = snap is not None and time.monotonic() - at < TTL
            if not fresh and not _STATE["running"]:
                _start_refresh_locked()
            ev = _STATE["done"]
        if fresh:
            return snap
        if wait and wait > 0:
            ev.wait(wait)
        with _LOCK:
            return _STATE["snap"]
    except Exception:                                            # noqa: BLE001
        return None


def refresh():
    """Ask for a fresh scan now (background). Returns True when one started."""
    if not ENABLED:
        return False
    try:
        with _LOCK:
            if _STATE["running"]:
                return False
            _start_refresh_locked()
            return True
    except Exception:                                            # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# The ONE short block a model reads, and the full rows the dashboard shows
# --------------------------------------------------------------------------- #

def _entry(name, row, short=False):
    login = row.get("login") if isinstance(row, dict) else None
    tool = _BY_NAME.get(name) or {}
    if login is True:
        return "%s (logged in)" % name
    if login is False:
        cmd = tool.get("login_cmd")
        if cmd and not short:
            return "%s (not logged in: ask the user to run `%s`)" % (name, cmd)
        return "%s (not logged in)" % name
    return name


_TAIL = (". Run them non-interactively (JSON output, --yes / --non-interactive "
         "flags); never run an interactive login yourself, ask the user.")
_SHORT_TAIL = ". Never run an interactive login yourself; ask the user."


def block(snap, max_chars=BLOCK_MAX):
    """'CLI TOOLS HERE: gh (logged in), vercel (not logged in: ask the user to
    run `vercel login`), docker; in WSL Ubuntu: supabase. Run them ...' -- or
    "" without a snapshot or tools. Deterministic and never longer than
    max_chars: long login asks are shortened first, then tools are dropped from
    the end with "+N more". Never raises."""
    try:
        return _block(snap, int(max_chars))
    except Exception:                                            # noqa: BLE001
        return ""


def _groups(snap):
    native = snap.get("native") if isinstance(snap, dict) else None
    native = native if isinstance(native, dict) else {}
    groups = [("", [(n, native[n]) for n in _NAMES if n in native])]
    wsl = snap.get("wsl") if isinstance(snap, dict) else None
    if isinstance(wsl, dict):
        for distro in sorted(wsl):
            rows = wsl[distro] if isinstance(wsl[distro], dict) else {}
            items = [(n, rows[n]) for n in _NAMES if n in rows]
            if items:
                groups.append((distro, items))
    return [(g, items) for g, items in groups if items]


def _render(groups, short, keep, extra, tail=_TAIL):
    parts, seen = [], 0
    for distro, items in groups:
        texts = []
        for n, row in items:
            if seen >= keep:
                break
            texts.append(_entry(n, row, short))
            seen += 1
        if not texts:
            continue
        prefix = ("in WSL %s: " % distro) if distro else ""
        parts.append(prefix + ", ".join(texts))
    body = MARKER + " " + "; ".join(parts)
    if extra:
        body += " (+%d more)" % extra
    return body + tail


def _block(snap, max_chars):
    groups = _groups(snap)
    total = sum(len(items) for _g, items in groups)
    if not total or max_chars <= 0:
        return ""
    # Every tool first: the long login asks and the long tail give way before
    # any tool is dropped.
    for tail, short in ((_TAIL, False), (_TAIL, True), (_SHORT_TAIL, False),
                        (_SHORT_TAIL, True)):
        text = _render(groups, short, total, 0, tail)
        if len(text) <= max_chars:
            return text
    for keep in range(total - 1, 0, -1):
        text = _render(groups, True, keep, total - keep, _SHORT_TAIL)
        if len(text) <= max_chars:
            return text
    return ""


def rows(snap):
    """Every installed tool for the dashboard: [{name, where, version, login,
    login_cmd, hint, login_checked}]. Never raises."""
    out = []
    try:
        for distro, items in _groups(snap or {}):
            for n, row in items:
                tool = _BY_NAME.get(n) or {}
                out.append({"name": n, "where": ("WSL " + distro) if distro else "native",
                            "version": row.get("version"), "login": row.get("login"),
                            "login_checked": bool(tool.get("login")),
                            "login_cmd": tool.get("login_cmd") or "",
                            "hint": tool.get("hint") or ""})
    except Exception:                                            # noqa: BLE001
        return []
    return out
