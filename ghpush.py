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

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.error
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
        | id_rsa.*               # id_rsa, id_rsa.pub, id_rsa_old
        | .*\.p12
        | credentials\.json
    )$""",
    re.IGNORECASE | re.VERBOSE,
)

# Secret-shaped CONTENT. Each is a named finding so the UI can say what it is.
_SECRET_CONTENT = [
    ("github_token", re.compile(r"\bghp_[A-Za-z0-9]{36,}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key_block", re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
]

_CONTENT_READ_CAP = 512 * 1024        # characters scanned per file
_CONTENT_SIZE_SKIP = 8 * 1024 * 1024  # never read a file larger than this
_GITIGNORE_MAX = 256 * 1024           # a .gitignore bigger than this is refused


def scan_secrets(root, status_entries):
    """Findings for the files in ``status_entries`` (relative paths of what would
    be committed). Each finding: ``{path, kind: "file"|"content", detail}``.
    Files are read only through deploy_perfect.safe_read_text: a link, a path
    resolving outside the project or a non-regular file is never read. Never
    raises."""
    findings = []
    try:
        root_real = os.path.realpath(root)
    except Exception:                                            # noqa: BLE001
        return findings
    for rel in status_entries:
        base = os.path.basename(rel)
        if base.lower() != ".env.example" and _SECRET_FILE_RE.match(base):
            findings.append({"path": rel, "kind": "file",
                             "detail": "This looks like a secret file and should not be committed."})
        text, _why = deploy_perfect.safe_read_text(
            os.path.join(root, rel), root_real, _CONTENT_SIZE_SKIP)
        if not text:
            continue
        text = text[:_CONTENT_READ_CAP]
        for name, rx in _SECRET_CONTENT:
            if rx.search(text):
                findings.append({"path": rel, "kind": "content",
                                 "detail": "A %s appears in this file." % name.replace("_", " ")})
                break
    return findings


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


def _real_git(args, cwd=None, env=None, git_path="git"):
    """Run git. ``env`` is the COMPLETE environment (the caller sanitized it);
    nothing is merged in from os.environ here."""
    try:
        p = subprocess.run(
            [git_path] + list(args), cwd=cwd,
            env=dict(env) if env is not None else None,
            capture_output=True, text=True, timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode, (p.stdout or ""), (p.stderr or "")
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
                 allowed_protocols=("https",), mirror_root=None):
        # Transports a push/fetch may use (GIT_ALLOW_PROTOCOL). https only in
        # the hub; a test adds "file" for its local bare remote.
        self.allowed_protocols = tuple(allowed_protocols)
        self.http = http or _urllib_http
        self.git_path = git_path
        self.run_git = run_git or (lambda args, cwd=None, env=None:
                                   _real_git(args, cwd=cwd, env=env, git_path=self.git_path))
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
             identity=False, protocols=None):
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
            return self.run_git(prefix + list(args), cwd=cwd, env=env)
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

    def _raise_if_secrets(self, folder, m):
        findings = scan_secrets(folder, self._would_commit(folder, m))
        if findings:
            raise GhError("secrets_found",
                          "Found something that should not be published. "
                          "Add these to .gitignore, or remove them, then try again.",
                          extra={"findings": findings,
                                 "has_gitignore": os.path.isfile(
                                     os.path.join(folder, ".gitignore"))})

    def _snapshot(self, folder, m):
        """Secret-scan, then ``add -A`` + commit into the MIRROR (no token in
        the environment). Commits only when something changed."""
        self._raise_if_secrets(folder, m)
        rc, _o, err = self._git(["add", "-A"], git_dir=m, work_tree=folder)
        if rc != 0:
            raise GhError("git_error", "Could not stage the files. %s" % _tail(err))
        has_main = self._has_main(m)
        drc, _o2, _e2 = self._git(["diff", "--cached", "--quiet"], git_dir=m, work_tree=folder)
        if drc == 0:                              # nothing staged
            if not has_main:
                raise GhError("nothing_to_commit", "This project has no files to commit yet.")
            return False
        crc, _oc, cerr = self._git(["commit", "--quiet", "-m", _COMMIT_MESSAGE],
                                   git_dir=m, work_tree=folder, identity=True)
        if crc != 0:
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
        findings that would block it. Only for a linked project."""
        folder = self._guard_folder(project_dir)
        self._check_git()
        self._linked_url(folder)
        m = self._ensure_mirror(folder)
        paths = self._would_commit(folder, m)
        return {
            "files": paths[:200],
            "total_files": len(paths),
            "findings": scan_secrets(folder, paths),
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

    def create_repo(self, project_dir, name, private=True, public_ok=False):
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
        if (self.store.get_project(self._key(folder)) or {}).get("url"):
            raise GhError("already_linked", "This project is already linked to a GitHub repository.")
        login = (self.store.get_account() or {}).get("login")
        if not login or not _LOGIN_RE.match(str(login)):
            raise GhError("no_token", "Connect your GitHub account first.")
        m = self._ensure_mirror(folder)
        self._raise_if_secrets(folder, m)
        status, data = self._api("POST", "/user/repos", token,
                                 {"name": repo, "private": private, "auto_init": False})
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
        self.store.set_project(self._key(folder), {
            "url": url,
            "full_name": "%s/%s" % (login, made),
            "private": bool(info.get("private", private)),
            "created_at": self.clock(),
            "last_pushed": None, "ahead": None, "behind": None,
        })
        self._snapshot(folder, m)
        self._push_mirror(folder, m, url)
        return self.status(project_dir)

    def push(self, project_dir):
        """Snapshot the work tree into the mirror, then push the mirror's main
        to the stored URL. Never a force push."""
        folder = self._guard_folder(project_dir)
        self._check_git()
        self._require_token()
        url = self._linked_url(folder)
        m = self._ensure_mirror(folder)
        self._snapshot(folder, m)
        self._push_mirror(folder, m, url)
        return self.status(project_dir)

    def _push_mirror(self, folder, m, url):
        url = self._linked_url(folder) if url is None else url
        prc, _op, perr = self._git(
            ["push", "--quiet", "--no-verify", "--no-recurse-submodules",
             self.transport_url(url), "%s:%s" % (_MAIN, _MAIN)],
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
        """Fetch GitHub's main into the mirror. When GitHub is strictly ahead and
        the project's work tree is unchanged since the mirror's main, the work
        tree is fast-forwarded from the mirror; otherwise nothing is touched."""
        folder = self._guard_folder(project_dir)
        self._check_git()
        self._require_token()
        url = self._linked_url(folder)
        m = self._ensure_mirror(folder)
        frc, _o, ferr = self._git(
            ["fetch", "--quiet", "--no-tags", "--no-recurse-submodules",
             self.transport_url(url), "+%s:%s" % (_MAIN, _TRACK)],
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
            old = []
            if has_main:
                orc, oout, _ = self._git(["rev-parse", _MAIN], git_dir=m)
                old = [oout.strip()] if orc == 0 and oout.strip() else []
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
        repo = None
        if url:
            full = url[len("https://github.com/"):-len(".git")]
            repo = {"full_name": full, "html_url": url[:-len(".git")],
                    "private": row.get("private"), "default_branch": "main",
                    "account_matches": valid_url(url, (acct or {}).get("login"))}
        out["project"] = {
            "dir": folder,
            "is_repo": self._mirror_ready(self.mirror_path(folder)),
            "has_remote": bool(url),
            "repo": repo,
            "branch": "main",
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
