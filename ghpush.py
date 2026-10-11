"""GitHub push for Build projects (2026-10-10, mirror design 2026-10-11).

Push the current Build project to GitHub from the dashboard: create a repo on
the owner's OWN account (private by default), then Push / Sync whenever they
click. Nothing is ever automatic — every create/push is an explicit button.

THE DESIGN (2026-10-11, structural, after two security reviews)
---------------------------------------------------------------
A Build project's own ``.git/config``, hooks and ``.gitattributes`` are written
by AI agents that can be prompt-injected, and the user's ``~/.gitconfig`` is
writable by the same user. So the git process that carries the token never
reads either:

  * Every project gets a HUB-OWNED BARE MIRROR at
    ``state_dir()/github-mirrors/<sha256(normcase realpath)[:16]>.git``, created
    with ``git init --bare`` from an empty template; its config is written only
    by the hub. All history the feature makes lives there.
  * Every git call runs with a SANITIZED environment: every inherited ``GIT_*``
    variable dropped, ``GIT_CONFIG_GLOBAL`` = an empty hub-owned file (and HOME
    / XDG_CONFIG_HOME = a hub-owned dir, so no ~/.gitconfig, XDG config or
    ~/.netrc), system config kept (admin-owned; Git for Windows keeps its TLS
    settings there), ``GIT_TERMINAL_PROMPT=0``, ``GIT_ALLOW_PROTOCOL`` limited
    to what the call needs. Plus ``-c`` overrides: an EMPTY private
    ``core.hooksPath``, ``core.fsmonitor=false``, credential helpers reset,
    no submodule recursion, no signing.
  * No git call ever uses the project's ``.git`` as its GIT_DIR. A snapshot is
    ``git --git-dir=<mirror> --work-tree=<project> add -A`` + a commit with an
    explicit identity: the project's ``.gitignore`` / ``.gitattributes`` are
    work-tree files and still apply, but filter drivers are only defined in
    config and the mirror has none. A project that already has its own history
    is imported ONCE (``fetch <project path> HEAD:refs/heads/main``, no token);
    the project's own ``.git`` is never written by this feature.
  * The secret scan reads EXACTLY what a commit records: ``add -A`` into the
    mirror index first (no token), then every staged blob is read from the
    mirror (``cat-file --batch``) and scanned whole. It fails CLOSED: an
    unreadable blob or one over ``scan_cap`` (20 MB) is a blocking finding, a
    scan error raises; a block resets the index, so nothing is committed.
  * Push / fetch (the only calls with the token) go from the MIRROR to the
    EXPLICIT stored URL ``https://github.com/<login>/<repo>.git`` — never a
    remote name — validated against the connected account's login before every
    use. The URL is written only when the hub itself creates the repo through
    the API for that account; no request can set or change it.
  * The token reaches git only through a GIT_ASKPASS helper (a private 0700
    temp dir, deleted after the call) that answers ONLY git's exact prompts for
    ``https://github.com`` (username ``x-access-token``, password from an env
    var). ``http.sslVerify=true``; never a force push.

Shape (same as publish.py / deploy_perfect.py): every side effect injectable —
``http``, ``run_git``, ``clock``, ``store``, ``mirror_root`` and, for tests
only, ``transport_url`` (maps the validated stored URL to the URL git uses;
identity in the hub, a local bare repo in tests). Raises only ``GhError``.
Pure stdlib (+ deploy_perfect's stdlib-only link/read helpers). ``config`` /
``secretstore`` are imported lazily.

RESIDUAL RISK: an agent running as the same OS user can read ``config.json`` +
``secret.key`` (and write the hub's state dir) and so decrypt or use the token
itself. Nothing here can defend against that.
"""
from __future__ import annotations

import atexit
import hashlib
import io
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import deploy_perfect

# --------------------------------------------------------------------------- #
# Error type.                                                                  #
# --------------------------------------------------------------------------- #


class GhError(Exception):
    """The only exception that leaves this module. ``code`` is a stable machine
    string the route maps to an HTTP status and the UI maps to plain English;
    ``extra`` carries structured detail (e.g. secret-scan findings)."""

    def __init__(self, code: str, message: str, extra: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra or {}


# --------------------------------------------------------------------------- #
# Secret scan.                                                                 #
# --------------------------------------------------------------------------- #

# Sensitive FILE names (by basename). .env.example is explicitly allowed.
_SECRET_FILE_RE = re.compile(
    r"""^(
        \.env(\.[^/\\]+)?        # .env, .env.local, .env.production ...
        | .*\.pem
        | .*\.key
        | id_rsa.* | id_ed25519.* | id_ecdsa.* | id_dsa.*   # SSH keys (+ .pub)
        | .*\.p12 | .*\.pfx
        | .*\.jks | .*\.keystore
        | credentials\.json
        | \.npmrc | \.pypirc | \.netrc | \.git-credentials
        | .*\.tfstate(\.[^/\\]+)?    # terraform state (+ .backup)
    )$""",
    re.IGNORECASE | re.VERBOSE,
)

# Secret-shaped CONTENT, matched on the WHOLE staged blob (bytes read as
# latin-1, so binary files are scanned too). Each is a named finding.
_SECRET_CONTENT = [
    ("github_token", re.compile(r"\bghp_[A-Za-z0-9]{36,}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("stripe_live_key", re.compile(r"\b[sr]k_live_[A-Za-z0-9]{10,}\b")),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b")),
    ("huggingface_token", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("xai_key", re.compile(r"\bxai-[A-Za-z0-9]{40,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("openssh_private_key", re.compile(r"-----BEGIN OPENSSH PRIVATE KEY-----")),
    ("private_key_block", re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")),
    ("service_account_key", re.compile(r'"private_key"\s*:\s*"-----BEGIN')),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
]

SCAN_CAP = 20 * 1024 * 1024           # a staged blob bigger than this BLOCKS (not checked)
_BATCH_BYTES = 64 * 1024 * 1024       # blob bytes read per `cat-file --batch` call
_GITIGNORE_MAX = 256 * 1024           # a .gitignore bigger than this is refused


def is_secret_name(basename):
    """A file NAME that must never be published (.env.example is allowed)."""
    b = (basename or "").strip()
    return b.lower() != ".env.example" and bool(_SECRET_FILE_RE.match(b))


def _finding(path, kind, detail):
    return {"path": path, "kind": kind, "detail": detail}


def scan_blob(path, data):
    """Findings for ONE staged blob: its name, then its whole content (bytes,
    read as latin-1 so every byte maps to one character and binary files are
    scanned too; a symlink blob holds the link path and is scanned the same).
    Pure; never raises."""
    out = []
    if is_secret_name(path.rsplit("/", 1)[-1]):
        out.append(_finding(path, "file",
                            "This looks like a secret file and should not be committed."))
    try:
        text = bytes(data or b"").decode("latin-1")
    except Exception:                                            # noqa: BLE001
        out.append(_finding(path, "unchecked",
                            "This file could not be checked; add it to .gitignore."))
        return out
    for name, rx in _SECRET_CONTENT:
        if rx.search(text):
            out.append(_finding(path, "content",
                                "A %s appears in this file." % name.replace("_", " ")))
            break
    return out


# The default .gitignore. Build output (dist/, build/) is DELIBERATELY NOT
# ignored: a project may commit it on purpose (e.g. GitHub Pages). The user can
# add it themselves via the same "Add to .gitignore" button.
DEFAULT_GITIGNORE = [
    "node_modules/",
    ".venv/",
    "venv/",
    "__pycache__/",
    "*.pyc",
    ".env",
    ".env.*",
    "!.env.example",
    "*.log",
    ".DS_Store",
]


def sanitize_repo_name(name):
    """A GitHub-legal repository name, or "" when nothing usable is left. GitHub
    maps anything outside ``[A-Za-z0-9._-]`` to ``-``; we also collapse runs,
    trim leading/trailing separators and cap the length."""
    name = (name or "").strip()
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name)
    name = re.sub(r"-{2,}", "-", name)
    name = name.strip("-._ ")
    if not name or name in (".", ".."):
        return ""
    return name[:100]


def _mask(token):
    """A low-risk hint so the owner can tell which token is stored — the type
    prefix and the last four, never the middle. Never the whole token."""
    t = (token or "").strip()
    if len(t) < 12:
        return "token on file"
    head = t.split("_")[0][:11] if "_" in t[:12] else t[:4]
    return "%s…%s" % (head, t[-4:])


# --------------------------------------------------------------------------- #
# One-click sign-in: GitHub's OAuth DEVICE flow (what `gh auth login` does --   #
# no client secret, no redirect). The Client ID is public.                     #
# --------------------------------------------------------------------------- #
# Filled once the owner registers the "Calvoun Hub" OAuth App (Device Flow on).
DEFAULT_OAUTH_CLIENT_ID = ""
CLIENT_ID_SETTING = "github_oauth_client_id"
CLIENT_ID_ENV = "FREE_LLM_HUB_GITHUB_CLIENT_ID"
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{10,40}$")
_USER_CODE_RE = re.compile(r"^[A-Z0-9]{4}-[A-Z0-9]{4}$")
_DEVICE_CODE_URL = "https://github.com/login/device/code"
_DEVICE_TOKEN_URL = "https://github.com/login/oauth/access_token"
DEVICE_VERIFY_URI = "https://github.com/login/device"
_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
_DEVICE_SCOPE = "repo"
_DEVICE_NET_ERRORS = 5          # consecutive network failures before the flow gives up


def valid_client_id(cid):
    return isinstance(cid, str) and bool(_CLIENT_ID_RE.match(cid))


def _spawn_daemon(target):
    t = threading.Thread(target=target, daemon=True, name="github-device-flow")
    t.start()
    return t


def _event_wait(event, seconds):
    """Sleep until `seconds` pass or `event` is set; True when it was set."""
    return event.wait(seconds)


# --- the official GitHub CLI (gh) for one-click sign-in without an OAuth app ---
_GH_RELEASE_API = "https://api.github.com/repos/cli/cli/releases/latest"
_GH_META_HOSTS = frozenset({"api.github.com"})
_GH_DOWNLOAD_HOSTS = frozenset({"github.com", "objects.githubusercontent.com",
                                "release-assets.githubusercontent.com"})
_GH_DOWNLOAD_PREFIX = "/cli/cli/releases/download/"
_GH_MAX_DOWNLOAD = 120 * 1024 * 1024
_GH_MAX_BINARY = 200 * 1024 * 1024
_GH_CODE_RE = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4})\b")
_GH_TOKENISH_RE = re.compile(r"\bgh[opsur]_[A-Za-z0-9_]+")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_GH_PLATFORMS = {
    ("windows", "amd64"): ("windows_amd64", ".zip"),
    ("windows", "arm64"): ("windows_arm64", ".zip"),
    ("linux", "amd64"): ("linux_amd64", ".tar.gz"),
    ("linux", "arm64"): ("linux_arm64", ".tar.gz"),
    ("darwin", "amd64"): ("macOS_amd64", ".zip"),
    ("darwin", "arm64"): ("macOS_arm64", ".zip"),
}


def gh_asset_key(system, machine):
    """(platform part, extension) of the official gh release asset for this
    computer, or None when GitHub publishes none."""
    s = str(system or "").lower()
    m = str(machine or "").lower()
    s = "windows" if s.startswith("win") else s
    m = {"x86_64": "amd64", "amd64": "amd64", "x64": "amd64",
         "aarch64": "arm64", "arm64": "arm64"}.get(m, m)
    return _GH_PLATFORMS.get((s, m))


def _gh_asset_url(row):
    """The asset's download URL: https, an allowlisted host, and -- on
    github.com -- under /cli/cli/releases/download/."""
    url = row.get("browser_download_url") if isinstance(row, dict) else None
    try:
        u = urllib.parse.urlsplit(str(url or ""))
        host, port = (u.hostname or "").lower(), u.port
    except ValueError:
        raise GhError("github_error", "The GitHub CLI download address is not valid.")
    if (u.scheme != "https" or host not in _GH_DOWNLOAD_HOSTS or u.username or u.password
            or port not in (None, 443)
            or (host == "github.com" and not u.path.startswith(_GH_DOWNLOAD_PREFIX))):
        raise GhError("github_error", "Refusing to download the GitHub CLI from an address "
                                      "that is not its official release.")
    return url


def _gh_safe_member(name):
    parts = name.replace("\\", "/").split("/")
    return not (name.startswith(("/", "\\")) or "\\" in name or ".." in parts
                or re.match(r"^[A-Za-z]:", name))


def _gh_extract(data, ext):
    """ONLY the gh binary (…/bin/gh or …/bin/gh.exe) out of the archive, with
    no extract()/extractall(): any unsafe member name refuses the whole archive."""
    if ext == ".zip":
        import zipfile
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
            if not all(_gh_safe_member(i.filename) for i in infos):
                raise GhError("github_error", "The GitHub CLI archive has an unsafe path; refused.")
            wanted = [i for i in infos if not i.is_dir()
                      and i.filename.split("/")[-2:] in (["bin", "gh.exe"], ["bin", "gh"])]
            if len(wanted) != 1 or not 0 < wanted[0].file_size <= _GH_MAX_BINARY:
                raise GhError("github_error", "The GitHub CLI archive does not hold one gh binary.")
            with zf.open(wanted[0]) as src:
                out = src.read(_GH_MAX_BINARY + 1)
    else:
        import tarfile
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            members = tf.getmembers()
            if not all(_gh_safe_member(m.name) for m in members):
                raise GhError("github_error", "The GitHub CLI archive has an unsafe path; refused.")
            wanted = [m for m in members if m.isfile() and m.name.split("/")[-2:] == ["bin", "gh"]]
            if len(wanted) != 1 or not 0 < wanted[0].size <= _GH_MAX_BINARY:
                raise GhError("github_error", "The GitHub CLI archive does not hold one gh binary.")
            out = tf.extractfile(wanted[0]).read(_GH_MAX_BINARY + 1)
    if not out or len(out) > _GH_MAX_BINARY:
        raise GhError("github_error", "The gh binary has a bad size.")
    return out


def _gh_spawn(argv, env):
    """gh with no console window, no TTY (stdin closed), its own process group
    (so killing the tree can never reach the hub), output merged and piped."""
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
              stderr=subprocess.STDOUT, env=env)
    if os.name == "nt":
        kw["creationflags"] = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                               | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(argv, **kw)


def _remove_private_dir(path):
    """Delete a ghpush-private temp dir (it may hold gh's hosts.yml)."""
    if path and os.path.isdir(path) and os.path.basename(path).startswith("ghpush-"):
        shutil.rmtree(path, ignore_errors=True)


_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


def canonical_url(login, repo):
    return "https://github.com/%s/%s.git" % (login, repo)


def valid_url(url, login):
    """The ONLY shape a stored push URL may have:
    ``https://github.com/<connected login>/<repo>.git``. Login compared without
    case (GitHub logins are case-insensitive); host and scheme exact."""
    if not isinstance(url, str) or not isinstance(login, str) or not _LOGIN_RE.match(login):
        return False
    m = re.match(r"^https://github\.com/(?i:%s)/([A-Za-z0-9._-]+)\.git$" % re.escape(login), url)
    return bool(m) and m.group(1) not in (".", "..")


# --------------------------------------------------------------------------- #
# Storage: token encrypted in config, per-project state in a state file.       #
# --------------------------------------------------------------------------- #


class ConfigStore:
    """Token + identity under ``config["github"]`` (token AES-256-GCM encrypted
    via secretstore — never counted by migrations' key fingerprint, never
    touched by its providers-only encrypt/decrypt pass); per-project state in
    ``state_dir()/github-projects.json`` (atomic write)."""

    CONFIG_KEY = "github"
    PROJECTS_FILE = "github-projects.json"

    def __init__(self, config_module=None, secretstore_module=None, projects_path=None):
        self._config = config_module
        self._secret = secretstore_module
        self._projects_path = projects_path

    # -- lazy module handles (no import cycle; a fake store needs neither) --
    def _cfg(self):
        if self._config is None:
            import config
            self._config = config
        return self._config

    def _ss(self):
        if self._secret is None:
            import secretstore
            self._secret = secretstore
        return self._secret

    def _config_path(self):
        return self._cfg()._config_path()

    def _projects_file(self):
        if self._projects_path:
            return self._projects_path
        return os.path.join(self._cfg().state_dir(), self.PROJECTS_FILE)

    # -- token / account --
    def get_token(self):
        """The DECRYPTED token, or None. None when the stored ciphertext cannot
        be read (lost secret.key) — never the raw ``enc.v1:…`` string."""
        gh = self._cfg().load_config().get(self.CONFIG_KEY)
        if not isinstance(gh, dict):
            return None
        tok = gh.get("token")
        if not isinstance(tok, str) or not tok:
            return None
        return self._ss().decrypt(tok, self._config_path())

    def get_account(self):
        """``{login, user_id, masked_hint}`` when a token is stored, else None.
        NEVER includes the token value."""
        gh = self._cfg().load_config().get(self.CONFIG_KEY)
        if not isinstance(gh, dict) or not gh.get("token"):
            return None
        return {"login": gh.get("login"), "user_id": gh.get("user_id"),
                "masked_hint": gh.get("hint")}

    def set_account(self, token, login, user_id):
        cfg = self._cfg().load_config(strict=True)
        enc = self._ss().encrypt(token, self._config_path())
        cfg[self.CONFIG_KEY] = {"token": enc, "login": login,
                                "user_id": user_id, "hint": _mask(token)}
        self._cfg().save_config(cfg)

    def clear_account(self):
        cfg = self._cfg().load_config(strict=True)
        cfg.pop(self.CONFIG_KEY, None)
        self._cfg().save_config(cfg)

    # -- the OAuth App's public Client ID (one-click sign-in) --
    def get_client_id(self):
        v = self._cfg().load_config().get(CLIENT_ID_SETTING)
        return v if isinstance(v, str) and v else None

    def set_client_id(self, value):
        cfg = self._cfg().load_config(strict=True)
        if value:
            cfg[CLIENT_ID_SETTING] = value
        else:
            cfg.pop(CLIENT_ID_SETTING, None)
        self._cfg().save_config(cfg)

    # -- per-project state --
    def get_projects(self):
        try:
            with open(self._projects_file(), "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get_project(self, key):
        row = self.get_projects().get(key)
        return row if isinstance(row, dict) else None

    def set_project(self, key, data):
        projects = self.get_projects()
        if data is None:
            projects.pop(key, None)
        else:
            projects[key] = data
        _atomic_write(self._projects_file(), json.dumps(projects, indent=2, ensure_ascii=False))


def _atomic_write(path, text):
    """Write ``text`` to ``path`` through a temp file in the same folder and
    ``os.replace``: a link sitting at ``path`` is REPLACED, its target is never
    written."""
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".%s." % os.path.basename(path), suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# Default side effects.                                                        #
# --------------------------------------------------------------------------- #


def _urllib_http(method, url, headers, body):
    """The real GitHub HTTP call. Raises GhError('network', …) on a transport
    failure; an HTTP error status is RETURNED (not raised) so callers can read
    the body (422 'name already exists' etc.)."""
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.getcode(), dict(resp.headers or {}), resp.read()
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read() or b""
        except Exception:                                            # noqa: BLE001
            raw = b""
        return exc.code, dict(exc.headers or {}), raw
    except urllib.error.URLError as exc:
        raise GhError("network", "Could not reach GitHub (%s)." %
                      getattr(exc, "reason", "network error"))
    except Exception as exc:                                         # noqa: BLE001
        raise GhError("network", "Could not reach GitHub (%s)." % type(exc).__name__)


def _real_git(args, cwd=None, env=None, git_path="git", stdin=None, binary=False):
    """Run git. ``env`` is the COMPLETE environment (the caller sanitized it);
    nothing is merged in from os.environ here. ``stdin`` (bytes) is fed in;
    ``binary`` returns stdout as bytes (blob contents). Text is UTF-8 with
    replacement, so a path or message in another encoding never crashes."""
    try:
        p = subprocess.run(
            [git_path] + list(args), cwd=cwd,
            env=dict(env) if env is not None else None,
            input=stdin, capture_output=True, timeout=300,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = p.stdout or b""
        err = (p.stderr or b"").decode("utf-8", "replace")
        return p.returncode, (out if binary else out.decode("utf-8", "replace")), err
    except FileNotFoundError:
        raise GhError("no_git", "git is not installed or not on PATH.")
    except subprocess.TimeoutExpired:
        raise GhError("git_timeout", "A git command took too long and was stopped.")


# --------------------------------------------------------------------------- #
# The client.                                                                  #
# --------------------------------------------------------------------------- #

_API = "https://api.github.com"
_COMMIT_MESSAGE = "Update from Calvoun Build"
_MAIN = "refs/heads/main"
_TRACK = "refs/remotes/github/main"

# On EVERY git call (command-line -c is the highest-precedence config).
_SAFE_CONFIG = (
    "credential.helper=",            # empty resets every helper: no credential manager
    "credential.useHttpPath=false",  # keep askpass prompts in the exact host-only form
    "core.fsmonitor=false",
    "submodule.recurse=false",
    "commit.gpgSign=false",
    "tag.gpgSign=false",
)
# On push/fetch only (the calls that carry the token in the environment).
_AUTH_CONFIG = (
    "http.sslVerify=true",
    "push.gpgSign=false",
    "fetch.recurseSubmodules=false",
    "push.recurseSubmodules=no",
)
# Dropped from the inherited environment besides every GIT_* variable.
_DROP_ENV = ("CURL_HOME",)

# The askpass helper. Exact prompt strings only; anything else prints nothing
# and fails, so git never sends the token to another host or over http://.
# ONE POSIX sh script on every platform: Git for Windows runs #!/bin/sh askpass
# scripts itself (verified 2026-10-11 with `git credential fill`). A .bat is
# NOT used: cmd.exe parses the prompt argument before the script runs, and git
# URL-decodes a remote's username into that prompt.
_ASKPASS_SH = (
    "#!/bin/sh\n"
    "# Calvoun ghpush askpass: answers ONLY for https://github.com.\n"
    "case \"$1\" in\n"
    "\"Username for 'https://github.com': \")\n"
    "  printf '%s\\n' 'x-access-token' ;;\n"
    "\"Password for 'https://x-access-token@github.com': \")\n"
    "  printf '%s\\n' \"$GH_ASKPASS_TOKEN\" ;;\n"
    "*)\n"
    "  exit 1 ;;\n"
    "esac\n"
)

_MIN_GIT = (2, 32)        # GIT_CONFIG_GLOBAL


def _is_link(path):
    """A symlink, a Windows junction / mount point, or a reparse point."""
    try:
        return bool(deploy_perfect.is_link(path) or os.path.islink(path))
    except Exception:                                            # noqa: BLE001
        return False


def _real(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(str(path))))


class GitHub:
    def __init__(self, http=None, run_git=None, clock=None, store=None,
                 transport_url=None, git_path="git",
                 is_hub_repo=None, too_broad=None, is_known_project=None,
                 allowed_protocols=("https",), mirror_root=None, scan_cap=SCAN_CAP,
                 spawn=None, wait=None, find_gh=None, gh_bin_dir=None, gh_argv=None,
                 gh_spawn=None, gh_fetch=None, gh_platform=None, gh_timeout=900,
                 gh_code_wait=20.0, gh_poll_step=0.5):
        # GitHub CLI sign-in (no OAuth app): every effect injectable for tests.
        self.find_gh = find_gh
        self.gh_bin_dir = gh_bin_dir
        self.gh_argv = gh_argv or (lambda path: [path])
        self.gh_spawn = gh_spawn or _gh_spawn
        self.gh_fetch = gh_fetch
        self.gh_platform = tuple(gh_platform) if gh_platform else (platform.system(),
                                                                    platform.machine())
        self.gh_timeout = float(gh_timeout)
        self.gh_code_wait = float(gh_code_wait)
        self.gh_poll_step = float(gh_poll_step)
        # Transports a push/fetch may use (GIT_ALLOW_PROTOCOL). https only in
        # the hub; a test adds "file" for its local bare remote.
        self.allowed_protocols = tuple(allowed_protocols)
        self.scan_cap = int(scan_cap)
        self._locks = {}
        self._locks_guard = threading.Lock()
        # Device flow: ONE flow at a time; spawn/wait injectable (a test drives
        # device_poll_once itself, or a real thread with an instant wait).
        self.spawn = spawn or _spawn_daemon
        self.wait = wait or _event_wait
        self._device = None
        self._dev_lock = threading.Lock()
        self.http = http or _urllib_http
        self.git_path = git_path
        self.run_git = run_git or (lambda args, cwd=None, env=None, **kw:
                                   _real_git(args, cwd=cwd, env=env,
                                             git_path=self.git_path, **kw))
        self.clock = clock or time.time
        self.store = store if store is not None else ConfigStore()
        # TEST SEAM: maps the validated stored https URL to what git is given.
        self.transport_url = transport_url or (lambda url: url)
        self._mirror_root = mirror_root
        self.is_hub_repo = is_hub_repo
        self.too_broad = too_broad
        self.is_known_project = is_known_project
        self._git_ok = False

    def set_guards(self, is_hub_repo, too_broad, is_known_project=None):
        """Wire the hub's own folder guards (app._cm_is_hub_repo /
        app._publish_folder_too_broad / app._gh_known_project). Kept injectable
        so a pure test can define its own."""
        self.is_hub_repo = is_hub_repo
        self.too_broad = too_broad
        self.is_known_project = is_known_project

    # ---- hub-owned locations ----------------------------------------------
    def mirror_root(self):
        if self._mirror_root:
            return os.path.abspath(self._mirror_root)
        import config
        return os.path.join(config.state_dir(), "github-mirrors")

    def _hub_dir(self, name):
        d = os.path.join(self.mirror_root(), "_" + name)
        if _is_link(d):
            raise GhError("refused", "A hub folder was replaced by a link; refusing to use it.")
        os.makedirs(d, exist_ok=True)
        return d

    def _empty_global(self):
        """The hub-owned, EMPTY global git config every call uses."""
        path = os.path.join(self.mirror_root(), "_empty-gitconfig")
        try:
            st = os.lstat(path)
            if stat.S_ISREG(st.st_mode) and st.st_size == 0 and not _is_link(path):
                return path
        except OSError:
            pass
        _atomic_write(path, "")
        return path

    # ---- folder guard -----------------------------------------------------
    def _guard_folder(self, project_dir):
        if not isinstance(project_dir, str) or not project_dir.strip():
            raise GhError("bad_request", "A project folder is required.")
        p = os.path.abspath(project_dir)
        if _is_link(p):
            raise GhError("refused", "That folder is a link; pick the real project folder.")
        if not os.path.isdir(p):
            raise GhError("not_a_dir", "That folder does not exist.")
        if self.is_hub_repo and self.is_hub_repo(p):
            raise GhError("refused", "The hub's own repository cannot be pushed from here.")
        if self.too_broad and self.too_broad(p):
            raise GhError("refused", "That folder is too broad to be a project.")
        state = _real(os.path.dirname(self.mirror_root()))
        real = _real(p)
        if deploy_perfect.inside(real, state) or deploy_perfect.inside(state, real):
            raise GhError("refused", "The hub's own data folder cannot be pushed.")
        if self.is_known_project is not None and not self.is_known_project(p):
            raise GhError("unknown_project",
                          "Only a Build project's folder can be pushed to GitHub.")
        return p

    # ---- GitHub REST ------------------------------------------------------
    def _api(self, method, path, token, body=None):
        headers = {
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "free-llm-hub",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        status, _hdrs, raw = self.http(method, _API + path, headers, data)
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:                                            # noqa: BLE001
            parsed = {}
        return status, parsed

    def validate_token(self, token):
        """GET /user -> ``{login, id}``; GhError('bad_token') when GitHub says
        no. Never stores anything."""
        if not isinstance(token, str) or not token.strip():
            raise GhError("bad_token", "Paste a GitHub token first.")
        token = token.strip()
        status, data = self._api("GET", "/user", token)
        if status == 200 and isinstance(data, dict) and data.get("login"):
            if not _LOGIN_RE.match(str(data["login"])):
                raise GhError("github_error", "GitHub returned an unusable account name.")
            return {"login": data["login"], "id": data.get("id")}
        if status in (401, 403):
            raise GhError("bad_token",
                          "GitHub did not accept this token. Check it has not "
                          "expired and has Contents + Administration (repo) access.")
        raise GhError("github_error",
                      "GitHub returned an unexpected response (HTTP %s)." % status)

    def connect(self, token):
        """Validate and store (encrypted). Returns ``{login, masked_hint}`` —
        never the token."""
        token = (token or "").strip()
        info = self.validate_token(token)
        self.store.set_account(token, info["login"], info.get("id"))
        return {"login": info["login"], "masked_hint": _mask(token)}

    def disconnect(self):
        self.store.clear_account()
        return {"ok": True}

    def _require_token(self):
        token = self.store.get_token()
        if not token:
            raise GhError("no_token", "Connect your GitHub account first.")
        return token

    # ---- one-click sign-in: OAuth device flow -------------------------------
    #
    # start_device() asks github.com for a user code; ONE background poller
    # then asks for the token every `interval` s until the owner approves on
    # github.com/login/device, the code expires, they deny it, or the flow is
    # cancelled / replaced by a new start. The token is stored through
    # connect() -- the same validated, encrypted path as a pasted token. The
    # device_code and the token never leave this object (no response, no log).

    def oauth_client_id(self):
        """The public Client ID: the saved setting, else the env var, else the
        built-in constant. None when none is valid."""
        cands = []
        getter = getattr(self.store, "get_client_id", None)
        if getter is not None:
            try:
                cands.append(getter())
            except Exception:                                    # noqa: BLE001
                pass
        cands += [os.environ.get(CLIENT_ID_ENV), DEFAULT_OAUTH_CLIENT_ID]
        for c in cands:
            if isinstance(c, str) and valid_client_id(c.strip()):
                return c.strip()
        return None

    def set_client_id(self, value):
        value = (value or "").strip() if isinstance(value, str) or value is None else None
        if value is None or (value and not valid_client_id(value)):
            raise GhError("bad_client_id", "That does not look like a GitHub app Client ID.")
        setter = getattr(self.store, "set_client_id", None)
        if setter is None:
            raise GhError("bad_request", "The Client ID cannot be saved here.")
        setter(value or None)
        return {"oauth_available": bool(self.oauth_client_id())}

    def _form(self, url, fields):
        body = urllib.parse.urlencode(fields).encode("ascii")
        headers = {"Accept": "application/json",
                   "Content-Type": "application/x-www-form-urlencoded",
                   "User-Agent": "free-llm-hub"}
        status, _h, raw = self.http("POST", url, headers, body)
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:                                        # noqa: BLE001
            data = {}
        return status, (data if isinstance(data, dict) else {})

    def one_click_available(self):
        """One-click connect works here: an OAuth Client ID is configured, or
        the official GitHub CLI is installed or installable on this platform."""
        try:
            return bool(self.oauth_client_id() or self._find_gh()
                        or gh_asset_key(*self.gh_platform) is not None)
        except Exception:                                        # noqa: BLE001
            return False

    def start_device(self):
        """Begin a one-click sign-in. With an OAuth Client ID: GitHub's device
        flow, polled by the hub. Without one: the official GitHub CLI's own
        device login (installed first when missing). Returns ``{user_code,
        verification_uri, expires_in, interval, state}`` -- never the
        device_code. A new start cancels the old one."""
        cid = self.oauth_client_id()
        if cid:
            return self._start_oauth(cid)
        if self._find_gh() or gh_asset_key(*self.gh_platform) is not None:
            return self._start_gh()
        raise GhError("no_client_id",
                      "One-click connect is not available on this computer: there is "
                      "no GitHub app Client ID and the GitHub CLI cannot be installed here.")

    def _start_oauth(self, cid):
        status, data = self._form(_DEVICE_CODE_URL, {"client_id": cid, "scope": _DEVICE_SCOPE})
        err = data.get("error")
        if status != 200 or err:
            raise GhError("github_error", "GitHub did not start the sign-in (%s)."
                          % _short(err or "HTTP %s" % status))
        dc, uc, uri = data.get("device_code"), data.get("user_code"), data.get("verification_uri")
        if uri != DEVICE_VERIFY_URI:
            raise GhError("github_error", "GitHub answered with an unexpected sign-in page; "
                                          "nothing was started.")
        if not isinstance(uc, str) or not _USER_CODE_RE.match(uc):
            raise GhError("github_error", "GitHub answered with an unexpected code; nothing "
                                          "was started.")
        if not isinstance(dc, str) or not dc or len(dc) > 200 or not dc.isprintable():
            raise GhError("github_error", "GitHub answered with an unusable sign-in; nothing "
                                          "was started.")
        expires_in = _clamp_int(data.get("expires_in"), 900, 30, 3600)
        flow = {"mode": "oauth", "client_id": cid, "device_code": dc, "user_code": uc,
                "expires_at": self.clock() + expires_in,
                "interval": _clamp_int(data.get("interval"), 5, 1, 120),
                "state": "pending", "login": None, "error": None,
                "cancel": threading.Event(), "net_errors": 0, "thread": None}
        self._replace_flow(flow)
        flow["thread"] = self.spawn(lambda: self._device_loop(flow))
        return {"user_code": uc, "verification_uri": DEVICE_VERIFY_URI,
                "expires_in": expires_in, "interval": flow["interval"], "state": "pending"}

    def _replace_flow(self, flow):
        """Make `flow` the one active sign-in; the old one is cancelled (its gh
        process killed and its private dir removed)."""
        with self._dev_lock:
            old, self._device = self._device, flow
        if old is not None:
            self._stop_flow(old)

    def _stop_flow(self, flow):
        flow["cancel"].set()
        flow["device_code"] = None
        if flow.get("mode") == "gh":
            self._gh_kill(flow)
            _remove_private_dir(flow.get("tmp"))

    def _device_end(self, flow, state, error=None, login=None):
        """Caller holds _dev_lock. Terminal: the device_code is forgotten."""
        flow["state"] = state
        flow["error"] = error
        flow["login"] = login
        flow["device_code"] = None

    def _device_loop(self, flow):
        try:
            while True:
                if self.wait(flow["cancel"], flow["interval"]):
                    return
                if not self.device_poll_once(flow):
                    return
        except Exception:                                        # noqa: BLE001
            with self._dev_lock:
                if flow["state"] == "pending":
                    self._device_end(flow, "error", "The sign-in stopped unexpectedly.")

    def device_poll_once(self, flow=None):
        """One token request. True = keep polling."""
        with self._dev_lock:
            flow = flow or self._device
            if (flow is None or flow is not self._device or flow.get("mode") != "oauth"
                    or flow["state"] != "pending" or flow["cancel"].is_set()
                    or not flow["device_code"]):
                return False
            if self.clock() >= flow["expires_at"]:
                self._device_end(flow, "expired")
                return False
            fields = {"client_id": flow["client_id"], "device_code": flow["device_code"],
                      "grant_type": _DEVICE_GRANT}
        try:
            _status, data = self._form(_DEVICE_TOKEN_URL, fields)
        except GhError:
            with self._dev_lock:
                flow["net_errors"] += 1
                if flow["net_errors"] >= _DEVICE_NET_ERRORS and flow["state"] == "pending":
                    self._device_end(flow, "error", "Could not reach GitHub.")
                    return False
            return True
        err = data.get("error")
        with self._dev_lock:
            flow["net_errors"] = 0
            if flow is not self._device or flow["cancel"].is_set() or flow["state"] != "pending":
                return False
            if err == "authorization_pending":
                return True
            if err == "slow_down":
                flow["interval"] = max(flow["interval"] + 5,
                                       _clamp_int(data.get("interval"), 0, 0, 120))
                return True
            if err == "expired_token":
                self._device_end(flow, "expired")
                return False
            if err == "access_denied":
                self._device_end(flow, "denied")
                return False
            if err:
                self._device_end(flow, "error", "GitHub stopped the sign-in (%s)." % _short(err))
                return False
            tok = data.get("access_token")
            if not isinstance(tok, str) or not tok.strip():
                self._device_end(flow, "error", "GitHub sent no token.")
                return False
        try:
            info = self.connect(tok)          # validate (GET /user) + store ENCRYPTED
        except GhError as exc:
            with self._dev_lock:
                if flow["state"] == "pending":
                    self._device_end(flow, "error", exc.message)
            return False
        with self._dev_lock:
            self._device_end(flow, "connected", login=info.get("login"))
        return False

    def device_status(self):
        """``{state: idle|pending|connected|expired|denied|error, ...}`` --
        never the device_code or the token."""
        with self._dev_lock:
            flow = self._device
            if flow is None:
                return {"state": "idle"}
            if flow["state"] == "pending" and self.clock() >= flow["expires_at"]:
                self._device_end(flow, "expired")
            out = {"state": flow["state"]}
            if flow["state"] == "installing":
                out["message"] = flow.get("message") or "Installing the GitHub CLI"
                if isinstance(flow.get("progress"), (int, float)):
                    out["progress"] = max(0.0, min(1.0, float(flow["progress"])))
            if flow["state"] == "pending":
                if flow.get("user_code"):
                    out["user_code"] = flow["user_code"]
                out.update({"verification_uri": DEVICE_VERIFY_URI,
                            "expires_in": max(0, int(flow["expires_at"] - self.clock())),
                            "interval": flow["interval"]})
            if flow["login"]:
                out["login"] = flow["login"]
            if flow["error"]:
                out["error"] = flow["error"]
            return out

    def cancel_device(self):
        with self._dev_lock:
            flow, self._device = self._device, None
        if flow is not None:
            self._stop_flow(flow)
        return {"state": "idle"}

    # ---- one-click sign-in WITHOUT an OAuth app: the official GitHub CLI -------
    #
    # `gh auth login --web` is GitHub's own CLI doing GitHub's own device login,
    # so the token it gets is a real "GitHub CLI" OAuth token (scopes repo,
    # read:org, gist). It runs with a FRESH private GH_CONFIG_DIR (0700 temp
    # dir; --insecure-storage keeps the token in that dir, never the keyring),
    # its own empty GIT_CONFIG_GLOBAL, every inherited GH_* / GITHUB_* / GIT_*
    # variable dropped (so a GH_TOKEN in the environment is never used), no
    # TTY, and a no-op BROWSER (the dashboard opens the page). The one-time
    # code and URL are read from its output; on success `gh auth token` reads
    # the token from that private dir, it is validated + stored through
    # connect(), and the dir is deleted. The user's real gh login and global
    # git config are never read or written. gh output is never logged.

    def _gh_bin_dir(self):
        if self.gh_bin_dir:
            return self.gh_bin_dir
        import config
        return os.path.join(config.state_dir(), "bin")

    def _find_gh(self):
        if self.find_gh is not None:
            return self.find_gh()
        exe = "gh.exe" if os.name == "nt" else "gh"
        found = shutil.which("gh")
        if found:
            return found
        try:
            cand = os.path.join(self._gh_bin_dir(), exe)
        except Exception:                                        # noqa: BLE001
            return None
        return cand if os.path.isfile(cand) and not _is_link(cand) else None

    def _start_gh(self):
        flow = {"mode": "gh", "device_code": None, "user_code": None,
                "expires_at": self.clock() + self.gh_timeout, "interval": 5,
                "state": "pending", "login": None, "error": None,
                "cancel": threading.Event(), "code_seen": threading.Event(),
                "proc": None, "tmp": None, "thread": None}
        if not self._find_gh():
            flow["state"] = "installing"
        self._replace_flow(flow)
        flow["thread"] = self.spawn(lambda: self._gh_worker(flow))
        if flow["state"] != "installing":
            flow["code_seen"].wait(self.gh_code_wait)
        with self._dev_lock:
            return {"user_code": flow["user_code"], "verification_uri": DEVICE_VERIFY_URI,
                    "expires_in": max(0, int(flow["expires_at"] - self.clock())),
                    "interval": flow["interval"], "state": flow["state"]}

    def _gh_active(self, flow):
        return self._device is flow and not flow["cancel"].is_set()

    def _gh_env(self, cfg_dir, gitcfg):
        env = {k: v for k, v in os.environ.items()
               if not k.upper().startswith(("GIT_", "GH_", "GITHUB_"))}
        noop = "cmd.exe /c exit 0" if os.name == "nt" else "true"
        env.update({"GH_CONFIG_DIR": cfg_dir, "GH_NO_UPDATE_NOTIFIER": "1",
                    "GH_NO_EXTENSION_UPDATE_NOTIFIER": "1", "GH_PROMPT_DISABLED": "1",
                    "GH_SPINNER_DISABLED": "1", "NO_COLOR": "1",
                    "GIT_CONFIG_GLOBAL": gitcfg, "GIT_TERMINAL_PROMPT": "0",
                    "BROWSER": noop, "GH_BROWSER": noop})
        return env

    def _gh_worker(self, flow):
        try:
            path = self._find_gh()
            if not path:
                path = self._install_gh(flow)
            if not self._gh_active(flow):
                return
            tmp = tempfile.mkdtemp(prefix="ghpush-gh-")
            flow["tmp"] = tmp
            try:
                os.chmod(tmp, stat.S_IRWXU)
            except OSError:
                pass
            cfg_dir = os.path.join(tmp, "config")
            os.mkdir(cfg_dir)
            gitcfg = os.path.join(tmp, "gitconfig")
            open(gitcfg, "w").close()
            env = self._gh_env(cfg_dir, gitcfg)
            argv = self.gh_argv(path) + ["auth", "login", "--hostname", "github.com", "--web",
                                         "--git-protocol", "ssh", "--skip-ssh-key",
                                         "--insecure-storage"]
            with self._dev_lock:
                if not self._gh_active(flow):
                    return
                flow["state"] = "pending"
                flow["expires_at"] = self.clock() + self.gh_timeout
            flow["proc"] = self.gh_spawn(argv, env)
            if not self._gh_active(flow):           # cancelled while it started
                self._gh_kill(flow)
                return
            reader = threading.Thread(target=self._gh_read, args=(flow,), daemon=True,
                                      name="github-cli-output")
            reader.start()
            proc = flow["proc"]
            while proc.poll() is None:
                if flow["cancel"].wait(self.gh_poll_step):
                    self._gh_kill(flow)
                    return
                if self.clock() >= flow["expires_at"]:
                    self._gh_kill(flow)
                    with self._dev_lock:
                        if self._gh_active(flow):
                            self._device_end(flow, "expired")
                    return
            reader.join(5)
            if not self._gh_active(flow):
                return
            if flow.get("bad_url"):
                return
            if proc.returncode != 0:
                text = " ".join(flow.get("tail") or []).lower()
                with self._dev_lock:
                    if self._gh_active(flow):
                        if "denied" in text:
                            self._device_end(flow, "denied")
                        elif "expired" in text:
                            self._device_end(flow, "expired")
                        else:
                            self._device_end(flow, "error", "The GitHub CLI sign-in did not "
                                                            "finish (exit %s)." % proc.returncode)
                return
            token = self._gh_token(path, env)
            if not self._gh_active(flow):
                return
            info = self.connect(token)               # validate (GET /user) + store ENCRYPTED
            with self._dev_lock:
                if self._device is flow:
                    self._device_end(flow, "connected", login=info.get("login"))
        except GhError as exc:
            with self._dev_lock:
                if self._gh_active(flow):
                    self._device_end(flow, "error", exc.message)
        except Exception as exc:                                 # noqa: BLE001
            with self._dev_lock:
                if self._gh_active(flow):
                    self._device_end(flow, "error", "The GitHub CLI sign-in stopped (%s)."
                                     % type(exc).__name__)
        finally:
            self._gh_kill(flow)
            _remove_private_dir(flow.get("tmp"))

    def _gh_read(self, flow):
        """Read gh's merged output: the one-time code and the sign-in URL. Never
        logged; only the last few lines are kept (for denied / expired)."""
        proc = flow.get("proc")
        tail = flow.setdefault("tail", [])
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = _ANSI_RE.sub("", raw.decode("utf-8", "replace")).strip()
                if not line:
                    continue
                tail.append(_GH_TOKENISH_RE.sub("[redacted]", line)[:200])
                del tail[:-8]
                for url in re.findall(r"https://\S+", line):
                    url = url.rstrip(".,;)\"'")
                    if "login/device" in url and url != DEVICE_VERIFY_URI:
                        flow["bad_url"] = True
                        with self._dev_lock:
                            if self._gh_active(flow):
                                self._device_end(flow, "error", "The GitHub CLI pointed at an "
                                                 "unexpected sign-in page; stopped.")
                        self._gh_kill(flow)
                        return
                m = _GH_CODE_RE.search(line)
                if m and "code" in line.lower():
                    with self._dev_lock:
                        if self._gh_active(flow) and flow["state"] == "pending":
                            flow["user_code"] = m.group(1)
                    flow["code_seen"].set()
        except Exception:                                        # noqa: BLE001
            pass
        finally:
            try:
                proc.stdout.close()
            except Exception:                                    # noqa: BLE001
                pass

    def _gh_token(self, path, env):
        """`gh auth token` from the private config dir (never the user's)."""
        try:
            p = subprocess.run(self.gh_argv(path) + ["auth", "token", "--hostname", "github.com"],
                               env=env, stdin=subprocess.DEVNULL, capture_output=True,
                               timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:                                        # noqa: BLE001
            raise GhError("github_error", "The GitHub CLI did not hand over the sign-in.")
        tok = (p.stdout or b"").decode("utf-8", "replace").strip()
        if p.returncode != 0 or not tok or len(tok) > 255 or not tok.isprintable() or " " in tok:
            raise GhError("github_error", "The GitHub CLI did not hand over the sign-in.")
        return tok

    def _gh_kill(self, flow):
        proc = flow.get("proc")
        if proc is None or proc.poll() is not None:
            return
        try:
            import publish
            publish._kill_tree(proc)
        except Exception:                                        # noqa: BLE001
            try:
                proc.kill()
            except Exception:                                    # noqa: BLE001
                pass

    # ---- installing the official GitHub CLI -----------------------------------
    def _install_gh(self, flow):
        """Download the official gh for this platform from github.com/cli/cli's
        latest release (https + host allowlist on every redirect), verify it
        against the release's gh_<ver>_checksums.txt (and the asset digest when
        GitHub lists one), and extract ONLY the gh binary into the hub's bin
        dir. A mismatch installs nothing."""
        key = gh_asset_key(*self.gh_platform)
        if key is None:
            raise GhError("github_error", "The GitHub CLI has no official build for this "
                                          "computer; install it yourself or add a Client ID.")
        plat, ext = key
        fetch = self.gh_fetch
        if fetch is None:
            import publish
            fetch = publish._https_fetch

        def stage(message, progress):
            with self._dev_lock:
                flow["message"] = message
                flow["progress"] = progress

        def get(url, max_bytes, hosts, on_progress=None):
            buf = bytearray()
            try:
                fetch(url, buf.extend, max_bytes, on_progress, hosts)
            except GhError:
                raise
            except Exception as exc:                             # noqa: BLE001
                raise GhError("github_error", "Could not download the GitHub CLI (%s)."
                              % (getattr(exc, "message", None) or type(exc).__name__))
            return bytes(buf)

        stage("Looking up the latest GitHub CLI release", 0.02)
        try:
            meta = json.loads(get(_GH_RELEASE_API, 4 * 1024 * 1024, _GH_META_HOSTS).decode("utf-8"))
        except GhError:
            raise
        except Exception:                                        # noqa: BLE001
            raise GhError("github_error", "The GitHub CLI release information could not be read.")
        tag = str(meta.get("tag_name") or "")
        ver = tag[1:] if tag.startswith("v") else tag
        if not re.match(r"^\d+\.\d+\.\d+$", ver):
            raise GhError("github_error", "The GitHub CLI release has an unexpected version.")
        name = "gh_%s_%s%s" % (ver, plat, ext)
        sums_name = "gh_%s_checksums.txt" % ver
        assets = {a.get("name"): a for a in (meta.get("assets") or []) if isinstance(a, dict)}
        row, sums_row = assets.get(name), assets.get(sums_name)
        if row is None or sums_row is None:
            raise GhError("github_error", "The GitHub CLI release has no %s." % name)
        url, sums_url = _gh_asset_url(row), _gh_asset_url(sums_row)
        sums = get(sums_url, 1024 * 1024, _GH_DOWNLOAD_HOSTS).decode("utf-8", "replace")
        expected = [ln.split()[0].lower() for ln in sums.splitlines()
                    if len(ln.split()) == 2 and ln.split()[1].lstrip("*") == name
                    and re.fullmatch(r"[0-9a-fA-F]{64}", ln.split()[0])]
        if len(expected) != 1:
            raise GhError("github_error", "The GitHub CLI release lists no checksum for %s; "
                                          "nothing was installed." % name)
        digest = row.get("digest")
        if isinstance(digest, str) and digest.startswith("sha256:") \
                and digest[7:].lower() != expected[0]:
            raise GhError("github_error", "The GitHub CLI release lists two different "
                                          "checksums; nothing was installed.")
        if not self._gh_active(flow):
            raise GhError("github_error", "Cancelled.")
        stage("Downloading the GitHub CLI", 0.05)

        def on_progress(done, total):
            if total:
                stage("Downloading the GitHub CLI", round(0.05 + 0.85 * min(1.0, done / total), 3))
        data = get(url, _GH_MAX_DOWNLOAD, _GH_DOWNLOAD_HOSTS, on_progress)
        stage("Checking the download", 0.92)
        if hashlib.sha256(data).hexdigest() != expected[0]:
            raise GhError("github_error", "The GitHub CLI download does not match its published "
                                          "checksum; nothing was installed.")
        binary = _gh_extract(data, ext)
        stage("Installing the GitHub CLI", 0.97)
        bin_dir = self._gh_bin_dir()
        os.makedirs(bin_dir, exist_ok=True)
        final = os.path.join(bin_dir, "gh.exe" if plat.startswith("windows") else "gh")
        if _is_link(final):
            raise GhError("github_error", "The hub's gh location is a link; nothing was installed.")
        fd, staged = tempfile.mkstemp(prefix=".gh-", suffix=".bin", dir=bin_dir)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(binary)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(staged, 0o755)
            os.replace(staged, final)
            staged = None
        finally:
            if staged and os.path.exists(staged):
                try:
                    os.remove(staged)
                except OSError:
                    pass
        return final

    def _identity(self):
        acct = self.store.get_account() or {}
        login = acct.get("login") or "calvoun-build"
        uid = acct.get("user_id")
        email = ("%s+%s@users.noreply.github.com" % (uid, login)) if uid \
            else ("%s@users.noreply.github.com" % login)
        return login, email

    # ---- git plumbing -----------------------------------------------------
    def _env(self, protocols, token=None, askpass=None):
        """The COMPLETE environment of a hub git call: the inherited one minus
        every GIT_* variable (GIT_DIR, GIT_CONFIG_*, GIT_SSH_COMMAND, GIT_TRACE*,
        GIT_SSL_NO_VERIFY ...), HOME / XDG_CONFIG_HOME pointed at a hub-owned
        dir, the empty hub-owned global config, no terminal prompt and only the
        transports this call needs. System config is kept (admin-owned)."""
        env = {}
        for k, v in os.environ.items():
            ku = k.upper()
            if ku.startswith("GIT_") or ku in _DROP_ENV:
                continue
            env[k] = v
        home = self._hub_dir("home")
        env["HOME"] = home
        env["XDG_CONFIG_HOME"] = home
        env["GIT_CONFIG_GLOBAL"] = self._empty_global()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ALLOW_PROTOCOL"] = ":".join(protocols)
        if token is not None:
            env["GIT_ASKPASS"] = askpass
            env["GH_ASKPASS_TOKEN"] = token
        return env

    def _git(self, args, git_dir=None, work_tree=None, auth=False,
             identity=False, protocols=None, stdin=None, binary=False):
        """Run one hub git call: sanitized env, an EMPTY private hooks dir, the
        safety ``-c`` set, explicit --git-dir / --work-tree. ``auth`` adds the
        askpass token path and is only ever used with the mirror as GIT_DIR.
        Returns (rc, stdout, stderr)."""
        tmp = tempfile.mkdtemp(prefix="ghpush-")
        try:
            try:
                os.chmod(tmp, stat.S_IRWXU)       # 0700
            except OSError:
                pass
            hooks = os.path.join(tmp, "hooks")    # stays EMPTY
            os.mkdir(hooks)
            prefix = []
            for kv in _SAFE_CONFIG:
                prefix += ["-c", kv]
            prefix += ["-c", "core.hooksPath=" + hooks]
            if identity:
                login, email = self._identity()
                prefix += ["-c", "user.name=" + login, "-c", "user.email=" + email]
            token = askpass = None
            if auth:
                for kv in _AUTH_CONFIG:
                    prefix += ["-c", kv]
                token = self._require_token()
                askpass = self._write_askpass(tmp)
            if git_dir:
                prefix.append("--git-dir=" + git_dir)
            if work_tree:
                prefix.append("--work-tree=" + work_tree)
            env = self._env(protocols or self.allowed_protocols, token, askpass)
            cwd = work_tree or git_dir or self.mirror_root()
            extra = {}
            if stdin is not None:
                extra["stdin"] = stdin
            if binary:
                extra["binary"] = True
            return self.run_git(prefix + list(args), cwd=cwd, env=env, **extra)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @staticmethod
    def _write_askpass(folder):
        """The GIT_ASKPASS helper (see _ASKPASS_SH) in the call's private 0700
        temp dir. The token is never in the script body."""
        script = os.path.join(folder, "askpass.sh")
        with open(script, "w", encoding="ascii", newline="\n") as f:
            f.write(_ASKPASS_SH)
        try:
            os.chmod(script, stat.S_IRWXU)   # 0700
        except OSError:
            pass
        return script

    def _check_git(self):
        if self._git_ok:
            return
        rc, out, _ = self._git(["--version"])
        m = re.search(r"(\d+)\.(\d+)", out or "")
        if rc != 0 or not m:
            raise GhError("no_git", "git is not installed or not on PATH.")
        if (int(m.group(1)), int(m.group(2))) < _MIN_GIT:
            raise GhError("git_too_old", "GitHub push needs git %d.%d or newer." % _MIN_GIT)
        self._git_ok = True

    # ---- the hub-owned mirror ---------------------------------------------
    def _key(self, folder):
        return _real(folder)

    def mirror_path(self, folder):
        digest = hashlib.sha256(_real(folder).encode("utf-8")).hexdigest()[:16]
        return os.path.join(self.mirror_root(), digest + ".git")

    def _mirror_ready(self, m):
        return os.path.isfile(os.path.join(m, "HEAD"))

    def _has_main(self, m):
        rc, _o, _e = self._git(["rev-parse", "--verify", "--quiet", _MAIN], git_dir=m)
        return rc == 0

    def _ensure_mirror(self, folder):
        """Create the hub-owned bare mirror (once) and import the project's own
        history ONCE when it has some. The project's .git is only READ, by that
        one tokenless local fetch."""
        m = self.mirror_path(folder)
        if _is_link(m):
            raise GhError("refused", "The hub's copy of this project was replaced by a link.")
        if not self._mirror_ready(m):
            template = self._hub_dir("template")          # empty: no sample hooks
            rc, _o, err = self._git(["init", "--bare", "--quiet", "-b", "main",
                                     "--template=" + template, m])
            if rc != 0:
                raise GhError("git_error", "Could not prepare the hub's copy of this "
                                           "project. %s" % _tail(err))
            for key, value in (("core.hooksPath", self._hub_dir("nohooks")),
                               ("core.fsmonitor", "false"),
                               ("gc.autoDetach", "false")):
                self._git(["config", key, value], git_dir=m)
            if os.path.lexists(os.path.join(folder, ".git")):
                # Tokenless, local transport only. A failure (no commits yet,
                # not a repo, another owner) just means no history to import.
                self._git(["fetch", "--quiet", "--no-tags", "--no-recurse-submodules",
                           folder, "HEAD:" + _MAIN], git_dir=m, protocols=("file",))
        if not os.path.exists(os.path.join(m, "index")) and self._has_main(m):
            # Start the index from main (tracked-but-ignored files stay tracked).
            self._git(["read-tree", _MAIN], git_dir=m, work_tree=folder)
        return m

    def _would_commit(self, folder, m):
        """Relative paths the next snapshot would add or change (the mirror's
        index vs the project's work tree; .gitignore respected). Deletions are
        left out. Fails CLOSED: an unreadable status is an error, never "no
        files"."""
        rc, out, err = self._git(["status", "--porcelain", "-z", "--untracked-files=all"],
                                 git_dir=m, work_tree=folder)
        if rc != 0:
            raise GhError("git_error", "Could not read the project's files. %s" % _tail(err))
        entries = out.split("\x00")
        paths = []
        i = 0
        while i < len(entries):
            e = entries[i]
            if not e:
                i += 1
                continue
            xy, path = e[:2], e[3:]
            if xy[:1] in ("R", "C"):
                i += 1                       # the rename/copy source is the next field
            if xy in ("D ", " D", "DD"):
                i += 1
                continue
            if path:
                paths.append(path)
            i += 1
        return paths

    # ---- the secret scan: EXACTLY the staged blobs ------------------------
    #
    # The scan reads what `git add -A` put into the mirror's index -- the very
    # blobs a commit would record -- not the work tree, so a file rewritten
    # after the scan cannot slip through (the old work-tree scan had that gap).
    # It fails CLOSED: a blob that cannot be read, or is bigger than scan_cap,
    # is itself a blocking finding, and any scan error raises. The index is put
    # back to HEAD whenever it is not about to be committed, so between
    # operations the mirror's index always equals its main.

    def _mirror_lock(self, m):
        with self._locks_guard:
            lk = self._locks.get(m)
            if lk is None:
                lk = self._locks[m] = threading.RLock()
            return lk

    def _reset_index(self, folder, m):
        """Put the mirror's index back to main (or empty when there is none).
        Index only: the work tree is never touched."""
        args = ["read-tree", _MAIN] if self._has_main(m) else ["read-tree", "--empty"]
        rc, _o, err = self._git(args, git_dir=m, work_tree=folder)
        if rc != 0:
            raise GhError("git_error", "Could not reset the hub's index. %s" % _tail(err))

    def _staged_entries(self, folder, m):
        """``[(path, mode, sha)]`` of every staged addition / modification /
        type change (deletions carry no content). Raises on any git error."""
        rc, out, err = self._git(
            ["diff", "--cached", "--raw", "-z", "--no-abbrev", "--no-renames",
             "--no-ext-diff", "--no-textconv", "--diff-filter=ACMRT"],
            git_dir=m, work_tree=folder)
        if rc != 0:
            raise GhError("scan_failed", "Could not list the files to check. %s" % _tail(err))
        toks = out.split("\x00")
        entries = []
        i = 0
        while i < len(toks):
            meta = toks[i]
            if not meta.startswith(":"):
                i += 1
                continue
            if i + 1 >= len(toks):
                raise GhError("scan_failed", "Could not read the list of files to check.")
            parts = meta[1:].split()
            if len(parts) < 5:
                raise GhError("scan_failed", "Could not read the list of files to check.")
            entries.append((toks[i + 1], parts[1], parts[3]))
            i += 2
        return entries

    def _cat(self, m, shas, mode):
        """``cat-file --batch-check`` (mode "check") or ``--batch`` (mode
        "content") over object ids fed on stdin (ids only: no path ever reaches
        the batch input). Returns {sha: (type, size)} or {sha: bytes|None}."""
        stdin = "".join(s + "\n" for s in shas).encode("ascii")
        flag = "--batch-check" if mode == "check" else "--batch"
        rc, out, err = self._git(["cat-file", flag], git_dir=m, stdin=stdin, binary=True)
        if rc != 0:
            raise GhError("scan_failed", "Could not read the files to check. %s" % _tail(err))
        result, pos = {}, 0
        for sha in shas:
            nl = out.find(b"\n", pos)
            if nl < 0:
                raise GhError("scan_failed", "Could not read the files to check.")
            head = out[pos:nl].split()
            pos = nl + 1
            if len(head) < 3 or head[1] in (b"missing", b"ambiguous"):
                result[sha] = None
                continue
            size = int(head[2])
            if mode == "check":
                result[sha] = (head[1].decode("ascii", "replace"), size)
            else:
                if pos + size > len(out):
                    raise GhError("scan_failed", "A file to check was cut short.")
                result[sha] = out[pos:pos + size]
                pos += size + 1                    # the blob, then a newline
        return result

    def _scan_staged(self, folder, m):
        """Stage everything into the mirror index (no token in the environment)
        and scan the staged blobs. Returns ``(paths, findings)`` with the index
        STAGED; on any error the index is reset and the error raised."""
        rc, _o, err = self._git(["add", "-A"], git_dir=m, work_tree=folder)
        if rc != 0:
            self._reset_index(folder, m)
            raise GhError("git_error", "Could not stage the files. %s" % _tail(err))
        try:
            entries = self._staged_entries(folder, m)
            findings, readable = [], []
            blobs = [e for e in entries if e[1] != "160000"]   # a gitlink is a pointer, no content
            info = self._cat(m, sorted({e[2] for e in blobs}), "check") if blobs else {}
            for path, _mode, sha in blobs:
                t = info.get(sha)
                if not t or t[0] != "blob":
                    findings.append(_finding(path, "unchecked",
                                             "This file could not be checked; add it to .gitignore."))
                    if is_secret_name(path.rsplit("/", 1)[-1]):
                        findings.append(_finding(path, "file", "This looks like a secret "
                                                 "file and should not be committed."))
                elif t[1] > self.scan_cap:
                    findings.append(_finding(path, "too_large",
                                             "Too large to check (over %d MB); add it to "
                                             ".gitignore." % (self.scan_cap // (1024 * 1024) or 1)))
                    if is_secret_name(path.rsplit("/", 1)[-1]):
                        findings.append(_finding(path, "file", "This looks like a secret "
                                                 "file and should not be committed."))
                else:
                    readable.append((path, sha, t[1]))
            batch, size = [], 0
            for item in readable + [None]:
                if item is not None and (not batch or size + item[2] <= _BATCH_BYTES):
                    batch.append(item)
                    size += item[2]
                    continue
                if batch:
                    data = self._cat(m, sorted({b[1] for b in batch}), "content")
                    for path, sha, _n in batch:
                        blob = data.get(sha)
                        if blob is None:
                            findings.append(_finding(path, "unchecked", "This file could not "
                                                     "be checked; add it to .gitignore."))
                        else:
                            findings.extend(scan_blob(path, blob))
                if item is not None:
                    batch, size = [item], item[2]
                else:
                    batch = []
            return [e[0] for e in entries], findings
        except GhError:
            self._reset_index(folder, m)
            raise
        except Exception as exc:                                 # noqa: BLE001
            self._reset_index(folder, m)
            raise GhError("scan_failed", "The secret check failed (%s); nothing was "
                                         "pushed." % type(exc).__name__)

    def _secrets_error(self, folder, findings):
        return GhError("secrets_found",
                       "Found something that should not be published. "
                       "Add these to .gitignore, or remove them, then try again.",
                       extra={"findings": findings,
                              "has_gitignore": os.path.isfile(os.path.join(folder, ".gitignore"))})

    def _check_only(self, folder, m):
        """Scan what a snapshot WOULD commit, then put the index back. Returns
        ``(paths, findings)``."""
        try:
            return self._scan_staged(folder, m)
        finally:
            self._reset_index(folder, m)

    def _snapshot(self, folder, m):
        """``add -A`` into the MIRROR, scan exactly the staged blobs, then
        commit THAT index (no token in the environment). A finding resets the
        index and raises; nothing is committed. Commits only when something
        changed."""
        _paths, findings = self._scan_staged(folder, m)
        if findings:
            self._reset_index(folder, m)
            raise self._secrets_error(folder, findings)
        has_main = self._has_main(m)
        drc, _o2, _e2 = self._git(["diff", "--cached", "--quiet"], git_dir=m, work_tree=folder)
        if drc == 0:                              # nothing staged
            if not has_main:
                raise GhError("nothing_to_commit", "This project has no files to commit yet.")
            return False
        crc, _oc, cerr = self._git(["commit", "--quiet", "-m", _COMMIT_MESSAGE],
                                   git_dir=m, work_tree=folder, identity=True)
        if crc != 0:
            self._reset_index(folder, m)
            raise GhError("git_error", "Could not commit. %s" % _tail(cerr))
        return True

    def _linked_url(self, folder):
        """The stored push URL, validated against the CONNECTED account."""
        row = self.store.get_project(self._key(folder)) or {}
        url = row.get("url")
        if not url:
            raise GhError("not_connected", "Create a GitHub repository for this project first.")
        acct = self.store.get_account() or {}
        if not valid_url(url, acct.get("login")):
            raise GhError("bad_link",
                          "This project's GitHub link does not belong to the connected "
                          "account, so nothing was pushed.")
        return url

    def _ahead_behind(self, m):
        rc, out, _ = self._git(["rev-list", "--left-right", "--count",
                                "%s...%s" % (_MAIN, _TRACK)], git_dir=m)
        if rc != 0:
            return None, None
        try:
            left, right = (out or "").split()[:2]
            return int(left), int(right)
        except (ValueError, IndexError):
            return None, None

    # ---- public operations ------------------------------------------------
    def preview(self, project_dir):
        """Files the next push would commit (capped list + total) and the secret
        findings that would block it -- the same staged-blob scan a push runs,
        then the index is put back. Only for a linked project."""
        folder = self._guard_folder(project_dir)
        self._check_git()
        self._linked_url(folder)
        m = self._ensure_mirror(folder)
        with self._mirror_lock(m):
            paths, findings = self._check_only(folder, m)
        return {
            "files": paths[:200],
            "total_files": len(paths),
            "findings": findings,
            "has_gitignore": os.path.isfile(os.path.join(folder, ".gitignore")),
        }

    def add_gitignore(self, project_dir, add=None, default=False):
        """Append lines to the project's .gitignore (creating it). ``default``
        writes the hub's default set; ``add`` is a list of plain paths (no
        control characters, no "!" negation, no comment). Only lines not yet
        present are added. A .gitignore that is a link or not a regular file is
        refused; the write replaces the file itself, never a link target.
        Returns ``{written:[...]}``."""
        folder = self._guard_folder(project_dir)
        want = list(DEFAULT_GITIGNORE) if default else []
        for p in (add or []):
            if not isinstance(p, str):
                continue
            p = p.strip().replace("\\", "/")
            if (not p or len(p) > 300 or p.startswith(("!", "#"))
                    or any(ord(c) < 32 or ord(c) == 127 for c in p)):
                raise GhError("bad_request", "That is not a path that can go into .gitignore.")
            want.append(p)
        if not want:
            raise GhError("bad_request", "Nothing to add to .gitignore.")
        path = os.path.join(folder, ".gitignore")
        existing = ""
        if os.path.lexists(path):
            if _is_link(path):
                raise GhError("refused", "This project's .gitignore is a link; the hub will not write through it.")
            text, why = deploy_perfect.safe_read_text(path, os.path.realpath(folder), _GITIGNORE_MAX)
            if text is None:
                raise GhError("refused", "This project's .gitignore cannot be read safely (%s)." % why)
            existing = text
        present = {ln.strip() for ln in existing.splitlines() if ln.strip()}
        written = []
        for line in want:
            if line not in present:
                written.append(line)
                present.add(line)
        if not written:
            return {"written": []}
        out = existing
        if out and not out.endswith("\n"):
            out += "\n"
        if not out:
            out = "# Added by Calvoun Build\n"
        elif "Added by Calvoun Build" not in out:
            out += "\n# Added by Calvoun Build\n"
        out += "\n".join(written) + "\n"
        try:
            _atomic_write(path, out)
        except OSError as exc:
            raise GhError("git_error", "Could not write .gitignore (%s)." % exc)
        return {"written": written}

    def create_repo(self, project_dir, name, private=True, public_ok=False, description=None):
        """Create a repo on the authenticated user's account (auto_init:false),
        store its URL and push the first snapshot. ``private`` defaults True; a
        public repo additionally needs ``public_ok``. The secret scan runs
        BEFORE anything is created on GitHub."""
        folder = self._guard_folder(project_dir)
        self._check_git()
        token = self._require_token()
        private = bool(private)
        if not private and not public_ok:
            raise GhError("public_ok_required",
                          "A public repo is visible to everyone. Confirm to make it public.")
        repo = sanitize_repo_name(name)
        if not repo:
            raise GhError("bad_name", "That repository name is not usable on GitHub.")
        login = self._login()
        desc = _clean_text(description, 350)
        with self._mirror_lock(self.mirror_path(folder)):
            return self._create_locked(project_dir, folder, token, login, repo, private, desc)

    def _create_locked(self, project_dir, folder, token, login, repo, private, desc):
        self._refuse_if_linked(folder)
        m = self._ensure_mirror(folder)
        # The staged-blob scan BEFORE anything is created on GitHub.
        _paths, findings = self._check_only(folder, m)
        if findings:
            raise self._secrets_error(folder, findings)
        body = {"name": repo, "private": private, "auto_init": False}
        if desc:
            body["description"] = desc
        status, data = self._api("POST", "/user/repos", token, body)
        if status == 201 and isinstance(data, dict) and data.get("full_name"):
            info = data
        elif status == 422 and _says_exists(data):
            raise GhError("repo_exists",
                          "You already have a repository named '%s'." % repo)
        elif status in (401, 403):
            raise GhError("bad_scope",
                          "This token cannot create a repository. It needs "
                          "Administration (or classic 'repo') access.")
        else:
            raise GhError("github_error",
                          "GitHub could not create the repository (HTTP %s)." % status)
        owner = str((info.get("owner") or {}).get("login") or login)
        made = str(info.get("name") or str(info.get("full_name")).split("/")[-1] or repo)
        url = canonical_url(login, made)
        if owner.lower() != str(login).lower() or not _REPO_NAME_RE.match(made) \
                or not valid_url(url, login):
            raise GhError("github_error", "GitHub answered with a repository that is not "
                                          "on your account; nothing was linked.")
        self._store_link(folder, url, login, made, bool(info.get("private", private)), "main")
        self._snapshot(folder, m)
        self._push_mirror(folder, m, url)
        return self.status(project_dir)

    # ---- link an EXISTING repository of the owner's ---------------------------
    def list_repos(self):
        """The connected account's OWN repositories, newest first:
        ``{repos: [{name, private, empty, updated_at, html_url, description}]}``.
        ``empty`` is GitHub's size == 0 (display only; linking checks for real).
        First 100 only."""
        token = self._require_token()
        login = self._login()
        status, data = self._api(
            "GET", "/user/repos?affiliation=owner&sort=updated&per_page=100", token)
        if status in (401, 403):
            raise GhError("bad_scope", "This token cannot list your repositories.")
        if status != 200 or not isinstance(data, list):
            raise GhError("github_error", "GitHub did not list your repositories (HTTP %s)." % status)
        out = []
        for r in data:
            if not isinstance(r, dict):
                continue
            name = r.get("name")
            owner = str((r.get("owner") or {}).get("login") or "")
            if (not isinstance(name, str) or not _REPO_NAME_RE.match(name)
                    or name in (".", "..") or owner.lower() != login.lower()):
                continue
            updated = r.get("updated_at") if isinstance(r.get("updated_at"), str) else None
            out.append({"name": name, "private": bool(r.get("private")),
                        "empty": r.get("size") == 0, "updated_at": updated,
                        "html_url": "https://github.com/%s/%s" % (login, name),
                        "description": _clean_text(r.get("description"), 200)})
        out.sort(key=lambda r: r["updated_at"] or "", reverse=True)
        return {"repos": out}

    def link_repo(self, project_dir, name, overwrite_ok=False):
        """Link this project to one of the owner's EXISTING repositories. Only
        after GET /repos/<login>/<name> confirms the connected account owns it
        and may push. An empty repo is pushed as today; a non-empty one needs
        ``overwrite_ok`` and the snapshot is committed ON TOP of its default
        branch (its history kept; never a force push)."""
        folder = self._guard_folder(project_dir)
        self._check_git()
        token = self._require_token()
        login = self._login()
        name = name.strip() if isinstance(name, str) else ""
        if not _REPO_NAME_RE.match(name) or name in (".", ".."):
            raise GhError("bad_name", "That repository name is not usable on GitHub.")
        with self._mirror_lock(self.mirror_path(folder)):
            return self._link_locked(project_dir, folder, token, login, name, bool(overwrite_ok))

    def _link_locked(self, project_dir, folder, token, login, name, overwrite_ok):
        self._refuse_if_linked(folder)
        status, data = self._api("GET", "/repos/%s/%s" % (login, name), token)
        if status == 404:
            raise GhError("not_found", "There is no repository named '%s' on your account." % name)
        if status in (401, 403):
            raise GhError("bad_scope", "This token cannot read that repository.")
        if status != 200 or not isinstance(data, dict):
            raise GhError("github_error", "GitHub did not describe that repository (HTTP %s)." % status)
        owner = str((data.get("owner") or {}).get("login") or "")
        if owner.lower() != login.lower():
            raise GhError("repo_not_owned", "That repository belongs to another account.")
        if not (data.get("permissions") or {}).get("push"):
            raise GhError("no_push_permission", "This token cannot push to that repository.")
        real = str(data.get("name") or name)
        url = canonical_url(login, real)
        branch = str(data.get("default_branch") or "main")
        if not _REPO_NAME_RE.match(real) or not valid_url(url, login) or not _valid_branch(branch):
            raise GhError("github_error", "GitHub described that repository in an unexpected way; "
                                          "nothing was linked.")
        m = self._ensure_mirror(folder)
        frc, _o, ferr = self._git(
            ["fetch", "--quiet", "--no-tags", "--no-recurse-submodules",
             self.transport_url(url), "+refs/heads/%s:%s" % (branch, _TRACK)],
            git_dir=m, auth=True)
        if frc == 0:
            nonempty = True
        elif "couldn't find remote ref" in (ferr or "").lower():
            nonempty = False
        else:
            raise GhError("fetch_failed", "Could not read that repository. %s" % _tail(ferr))
        if nonempty and not overwrite_ok:
            raise GhError("needs_overwrite_ok",
                          "This repository already has files. Pushing will make its latest "
                          "version match this project (older versions stay in the history).",
                          extra={"nonempty": True})
        old_main = self._rev(m, _MAIN)
        if nonempty:
            if old_main:          # keep what the mirror had; it simply leaves main
                self._git(["update-ref", "refs/hub/before-link-%d" % int(self.clock()),
                           old_main], git_dir=m)
            self._git(["update-ref", _MAIN, _TRACK], git_dir=m)
            self._reset_index(folder, m)            # index = GitHub's tree; work tree untouched
        try:
            _paths, findings = self._check_only(folder, m)
            if findings:
                raise self._secrets_error(folder, findings)
        except GhError:
            if nonempty:          # put the mirror back as it was
                if old_main:
                    self._git(["update-ref", _MAIN, old_main], git_dir=m)
                else:
                    self._git(["update-ref", "-d", _MAIN], git_dir=m)
                self._reset_index(folder, m)
            raise
        self._store_link(folder, url, login, real, bool(data.get("private")), branch)
        self._snapshot(folder, m)                   # on top of GitHub's branch
        self._push_mirror(folder, m, url)           # a fast-forward: never forced
        return self.status(project_dir)

    def set_visibility(self, project_dir, private, public_ok=False):
        """Make the linked repository private or public (PATCH /repos/...).
        Public needs ``public_ok``. Returns ``{ok, private}``."""
        folder = self._guard_folder(project_dir)
        token = self._require_token()
        if not isinstance(private, bool):
            raise GhError("bad_request", "Say private: true or false.")
        if not private and not public_ok:
            raise GhError("public_ok_required",
                          "A public repo is visible to everyone. Confirm to make it public.")
        url = self._linked_url(folder)
        login = self._login()
        name = url[len("https://github.com/"):-len(".git")].split("/", 1)[1]
        status, data = self._api("PATCH", "/repos/%s/%s" % (login, name), token,
                                 {"private": private})
        if status == 200 and isinstance(data, dict):
            now = bool(data.get("private", private))
        elif status in (401, 403):
            raise GhError("bad_scope", "This token cannot change that repository's visibility.")
        elif status == 404:
            raise GhError("not_found", "That repository is no longer on your account.")
        else:
            raise GhError("github_error", "GitHub did not change the visibility (HTTP %s)." % status)
        key = self._key(folder)
        row = self.store.get_project(key) or {}
        row["private"] = now
        self.store.set_project(key, row)
        return {"ok": True, "private": now}

    def unlink(self, project_dir):
        """Forget this project's GitHub link and delete the hub's mirror of it.
        Never touches GitHub or the project's own files, so a deleted or moved
        repository can be linked again."""
        folder = self._guard_folder(project_dir)
        m = self.mirror_path(folder)
        with self._mirror_lock(m):
            self.store.set_project(self._key(folder), None)
            if os.path.lexists(m):
                if _is_link(m):
                    try:
                        os.unlink(m)                # the link itself, never its target
                    except OSError:
                        os.rmdir(m)
                elif os.path.isdir(m) and deploy_perfect.inside(_real(m), _real(self.mirror_root())):
                    _rmtree(m)
        return {"ok": True}

    # ---- shared helpers ---------------------------------------------------------
    def _login(self):
        login = (self.store.get_account() or {}).get("login")
        if not login or not _LOGIN_RE.match(str(login)):
            raise GhError("no_token", "Connect your GitHub account first.")
        return str(login)

    def _refuse_if_linked(self, folder):
        if (self.store.get_project(self._key(folder)) or {}).get("url"):
            raise GhError("already_linked", "This project is already linked to a GitHub repository.")

    def _store_link(self, folder, url, login, name, private, branch):
        self.store.set_project(self._key(folder), {
            "url": url, "full_name": "%s/%s" % (login, name), "private": private,
            "branch": branch, "created_at": self.clock(),
            "last_pushed": None, "ahead": None, "behind": None,
        })

    def _rev(self, m, ref):
        rc, out, _ = self._git(["rev-parse", "--verify", "--quiet", ref], git_dir=m)
        return out.strip() if rc == 0 and out.strip() else None

    def _branch(self, folder):
        """The linked repository's branch the hub pushes to (validated)."""
        b = (self.store.get_project(self._key(folder)) or {}).get("branch") or "main"
        if not _valid_branch(b):
            raise GhError("bad_link", "This project's GitHub link names an unusable branch.")
        return b

    def push(self, project_dir):
        """Snapshot the work tree into the mirror, then push the mirror's main
        to the stored URL. Never a force push."""
        folder = self._guard_folder(project_dir)
        self._check_git()
        self._require_token()
        url = self._linked_url(folder)
        with self._mirror_lock(self.mirror_path(folder)):
            m = self._ensure_mirror(folder)
            self._snapshot(folder, m)
            self._push_mirror(folder, m, url)
        return self.status(project_dir)

    def _push_mirror(self, folder, m, url):
        url = self._linked_url(folder) if url is None else url
        branch = self._branch(folder)
        prc, _op, perr = self._git(
            ["push", "--quiet", "--no-verify", "--no-recurse-submodules",
             self.transport_url(url), "%s:refs/heads/%s" % (_MAIN, branch)],
            git_dir=m, auth=True)
        if prc != 0:
            if _is_non_fast_forward(perr):
                raise GhError("behind_remote",
                              "GitHub has changes this copy doesn't. Press Sync first, "
                              "then push again.")
            raise GhError("push_failed", "The push to GitHub failed. %s" % _tail(perr))
        self._git(["update-ref", _TRACK, _MAIN], git_dir=m)
        key = self._key(folder)
        row = self.store.get_project(key) or {}
        row.update({"last_pushed": self.clock(), "ahead": 0, "behind": 0})
        self.store.set_project(key, row)

    def sync(self, project_dir):
        """Fetch GitHub's branch into the mirror. When GitHub is strictly ahead and
        the project's work tree is unchanged since the mirror's main, the work
        tree is fast-forwarded from the mirror; otherwise nothing is touched."""
        folder = self._guard_folder(project_dir)
        self._check_git()
        self._require_token()
        url = self._linked_url(folder)
        with self._mirror_lock(self.mirror_path(folder)):
            return self._sync_locked(project_dir, folder, url)

    def _sync_locked(self, project_dir, folder, url):
        m = self._ensure_mirror(folder)
        branch = self._branch(folder)
        frc, _o, ferr = self._git(
            ["fetch", "--quiet", "--no-tags", "--no-recurse-submodules",
             self.transport_url(url), "+refs/heads/%s:%s" % (branch, _TRACK)],
            git_dir=m, auth=True)
        if frc != 0:
            if "couldn't find remote ref" in (ferr or "").lower():
                self._record_counts(folder, None, 0)         # GitHub is still empty
                return self.status(project_dir)
            raise GhError("fetch_failed", "Could not fetch from GitHub. %s" % _tail(ferr))
        has_main = self._has_main(m)
        if has_main:
            ahead, behind = self._ahead_behind(m)
        else:
            rc, out, _ = self._git(["rev-list", "--count", _TRACK], git_dir=m)
            ahead, behind = 0, (int(out.strip()) if rc == 0 and out.strip().isdigit() else 0)
        if ahead and behind:
            raise GhError("diverged",
                          "This copy and GitHub have each changed since they last "
                          "matched (%d here, %d on GitHub). The hub will not merge "
                          "them; merge them by hand." % (ahead, behind))
        if behind:
            if self._would_commit(folder, m):
                raise GhError("local_changes",
                              "GitHub has newer changes, and this project also has "
                              "changes that are not on GitHub yet. Nothing was "
                              "touched: set your changes aside, Sync, then re-apply "
                              "them.")
            trees = [_MAIN, _TRACK] if has_main else [_TRACK]
            rrc, _or, rerr = self._git(["read-tree", "-u", "-m"] + trees,
                                       git_dir=m, work_tree=folder)
            if rrc != 0:
                raise GhError("local_changes",
                              "The project's files could not be updated safely. %s" % _tail(rerr))
            old = [self._rev(m, _MAIN)] if has_main and self._rev(m, _MAIN) else []
            self._git(["update-ref", _MAIN, _TRACK] + old, git_dir=m)
            ahead, behind = self._ahead_behind(m)
        self._record_counts(folder, ahead, behind)
        return self.status(project_dir)

    def _record_counts(self, folder, ahead, behind):
        key = self._key(folder)
        row = self.store.get_project(key)
        if row is None:
            return
        row["ahead"] = ahead
        row["behind"] = behind
        row["last_fetched"] = self.clock()
        self.store.set_project(key, row)

    def status(self, project_dir=None):
        """Connection + (optionally) one project's state. Never returns a token.
        Runs no git and no network: the stored row and the mirror's presence."""
        acct = self.store.get_account()
        out = {
            "connected": bool(acct),
            "login": acct.get("login") if acct else None,
            "masked_hint": acct.get("masked_hint") if acct else None,
            "project": None,
        }
        if not project_dir:
            return out
        try:
            folder = self._guard_folder(project_dir)
        except GhError as exc:
            out["project"] = {"dir": project_dir, "refused": exc.message,
                              "code": exc.code}
            return out
        row = self.store.get_project(self._key(folder)) or {}
        url = row.get("url")
        branch = row.get("branch") if _valid_branch(row.get("branch") or "") else "main"
        repo = None
        if url:
            full = url[len("https://github.com/"):-len(".git")]
            repo = {"full_name": full, "html_url": url[:-len(".git")],
                    "private": row.get("private"), "default_branch": branch,
                    "account_matches": valid_url(url, (acct or {}).get("login"))}
        out["project"] = {
            "dir": folder,
            "is_repo": self._mirror_ready(self.mirror_path(folder)),
            "has_remote": bool(url),
            "repo": repo,
            "branch": branch,
            "ahead": row.get("ahead"),
            "behind": row.get("behind"),
            "last_pushed": row.get("last_pushed"),
            "last_fetched": row.get("last_fetched"),
        }
        return out


# --------------------------------------------------------------------------- #
# Small pure helpers.                                                          #
# --------------------------------------------------------------------------- #


def _tail(text, n=300):
    s = (text or "").strip()
    return s[-n:] if len(s) > n else s


_BRANCH_RE = re.compile(r"^[A-Za-z0-9._-][A-Za-z0-9._/-]{0,99}$")


def _valid_branch(name):
    """A plain branch name the hub may put in a refspec: no "..", "//", "@{",
    leading "-" or "/", trailing "/" or ".lock"."""
    return (isinstance(name, str) and bool(_BRANCH_RE.match(name)) and ".." not in name
            and "//" not in name and not name.endswith(("/", ".lock", "."))
            and "@{" not in name)


def _clean_text(text, n):
    """One line of printable text, at most n characters ("" for anything else)."""
    if not isinstance(text, str):
        return ""
    s = " ".join("".join(c if c.isprintable() else " " for c in text).split())
    return s[:n]


def _short(text, n=60):
    """A short, single-line, printable rendering of a GitHub error code."""
    s = "".join(c for c in str(text or "") if c.isprintable())
    return s[:n]


def _clamp_int(value, default, lo, hi):
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


def _rmtree(path):
    """Remove a hub-owned tree; git marks pack files read-only on Windows."""
    def onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onerror=onerror)


def _says_exists(data):
    """A 422 whose errors say the name is already taken."""
    try:
        blob = json.dumps(data).lower()
    except Exception:                                            # noqa: BLE001
        blob = str(data).lower()
    return "already exists" in blob or "name already exists" in blob


def _is_non_fast_forward(stderr):
    s = (stderr or "").lower()
    return ("non-fast-forward" in s or "fetch first" in s
            or "updates were rejected" in s or "[rejected]" in s
            or "failed to push some refs" in s)


# The module-level singleton app.py uses. Its guards are wired at import.
default = GitHub()


def _shutdown_default():
    """Hub shutdown: a running GitHub CLI sign-in is killed and its private
    config dir deleted."""
    try:
        default.cancel_device()
    except Exception:                                            # noqa: BLE001
        pass


atexit.register(_shutdown_default)
