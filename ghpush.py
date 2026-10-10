"""GitHub push for Build projects (2026-10-10).

Push the current Build project to GitHub from the dashboard: create a repo on
the owner's OWN account (private by default), then Push / Sync whenever they
click. Nothing is ever automatic — every create/push is an explicit button.

Shape (same as publish.py / deploy_perfect.py): a small, mostly-pure module
whose every side effect is INJECTABLE, so the whole thing runs hermetically in a
test with a fake GitHub HTTP and real git against a local bare repo.

  * ``http(method, url, headers, body) -> (status, headers, body_bytes)`` — the
    GitHub REST call (urllib by default).
  * ``run_git(args, cwd=, env=) -> (returncode, stdout, stderr)`` — git, argv
    list, no shell (subprocess by default).
  * ``clock() -> float`` — the wall clock (time.time by default).
  * ``store`` — where the token (ENCRYPTED, same AES-256-GCM as the provider
    keys) and the per-project state live (ConfigStore by default).
  * ``remote_url(login, repo) -> url`` — the git remote URL (the https GitHub
    URL by default; a test points it at a local bare repo).

Safety, stated plainly:
  * The token is validated against GET /user, stored ENCRYPTED (secretstore,
    the mechanism the provider keys use), NEVER logged, NEVER returned (status
    shows only the login and a masked hint), and NEVER written into .git/config
    or a remote URL.
  * git auth for push/fetch goes through a GIT_ASKPASS helper in a private 0700
    temp dir that is deleted right after the command; the token reaches it only
    through an env var, never a command line. Every git call runs with
    ``-c credential.helper=`` (empty) and ``GIT_TERMINAL_PROMPT=0`` so NO
    credential manager is ever read or written — this machine's Windows
    Credential Manager holds a different account and must never be touched.
  * Before any push, a secret scan of what WOULD be committed blocks the push
    when it finds a secret file or a secret-shaped string.
  * The hub's own repo, a too-broad folder (a drive root / home / above it) and
    a symlinked/junction folder are refused.

This module raises only ``GhError(code, message)`` to callers. Pure stdlib.
``config`` / ``secretstore`` are imported lazily inside ConfigStore, so there is
no import cycle and a test can use a fake store with neither present.
"""
from __future__ import annotations

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

_CONTENT_READ_CAP = 512 * 1024      # bytes read per file for the content scan
_CONTENT_SIZE_SKIP = 8 * 1024 * 1024  # never read a file larger than this


def scan_secrets(root, status_entries):
    """Findings for the files in ``status_entries`` (relative paths of what would
    be committed). Each finding: ``{path, kind: "file"|"content", detail}``.
    Reading a file never raises out of here."""
    findings = []
    for rel in status_entries:
        base = os.path.basename(rel)
        if base.lower() != ".env.example" and _SECRET_FILE_RE.match(base):
            findings.append({"path": rel, "kind": "file",
                             "detail": "This looks like a secret file and should not be committed."})
            # A secret file is reported by name; also scan its content below only
            # if it is small, but the name finding already blocks the push.
        full = os.path.join(root, rel)
        try:
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            if os.path.getsize(full) > _CONTENT_SIZE_SKIP:
                continue
            with open(full, "rb") as f:
                chunk = f.read(_CONTENT_READ_CAP)
        except OSError:
            continue
        try:
            text = chunk.decode("utf-8", "replace")
        except Exception:                                            # noqa: BLE001
            continue
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
        self._atomic_write_projects(projects)

    def _atomic_write_projects(self, projects):
        path = self._projects_file()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".github-projects-", suffix=".tmp",
                                   dir=parent or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(projects, f, indent=2, ensure_ascii=False)
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
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    try:
        p = subprocess.run(
            [git_path] + list(args), cwd=cwd, env=full_env,
            capture_output=True, text=True, timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode, (p.stdout or ""), (p.stderr or "")
    except FileNotFoundError:
        raise GhError("no_git", "git is not installed or not on PATH.")
    except subprocess.TimeoutExpired:
        raise GhError("git_timeout", "A git command took too long and was stopped.")


def _default_remote_url(login, repo):
    return "https://github.com/%s/%s.git" % (login, repo)


# --------------------------------------------------------------------------- #
# The client.                                                                  #
# --------------------------------------------------------------------------- #

_API = "https://api.github.com"
_COMMIT_MESSAGE = "Update from Calvoun Build"


def _is_link(path):
    """A symlink, a Windows junction / mount point, or a reparse point."""
    try:
        if os.path.islink(path):
            return True
        if hasattr(os.path, "isjunction") and os.path.isjunction(path):
            return True
        st = os.lstat(path)
        return bool(getattr(st, "st_reparse_tag", 0))
    except OSError:
        return False


class GitHub:
    def __init__(self, http=None, run_git=None, clock=None, store=None,
                 remote_url=None, git_path="git",
                 is_hub_repo=None, too_broad=None):
        self.http = http or _urllib_http
        self.git_path = git_path
        self.run_git = run_git or (lambda args, cwd=None, env=None:
                                   _real_git(args, cwd=cwd, env=env, git_path=self.git_path))
        self.clock = clock or time.time
        self.store = store if store is not None else ConfigStore()
        self.remote_url = remote_url or _default_remote_url
        self.is_hub_repo = is_hub_repo
        self.too_broad = too_broad

    def set_guards(self, is_hub_repo, too_broad):
        """Wire the hub's own folder guards (app._cm_is_hub_repo /
        app._publish_folder_too_broad). Kept injectable so a pure test can
        define its own."""
        self.is_hub_repo = is_hub_repo
        self.too_broad = too_broad

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

    # ---- git plumbing -----------------------------------------------------
    def _git(self, args, cwd, auth=False, identity=False):
        """Run git with the hub's fixed safety flags. ``auth`` adds the ASKPASS
        token path (push/fetch); ``identity`` adds a per-command commit identity
        when the repo has none configured. Returns (rc, out, err)."""
        prefix = ["-c", "credential.helper="]
        if identity and not self._has_identity(cwd):
            acct = self.store.get_account() or {}
            login = acct.get("login") or "calvoun-build"
            uid = acct.get("user_id")
            email = ("%s+%s@users.noreply.github.com" % (uid, login)) if uid \
                else ("%s@users.noreply.github.com" % login)
            prefix += ["-c", "user.name=%s" % login, "-c", "user.email=%s" % email]
        full = prefix + list(args)
        env, cleanup = ({}, None)
        if auth:
            env, cleanup = self._askpass_env(self._require_token())
        try:
            return self.run_git(full, cwd=cwd, env=env)
        finally:
            if cleanup:
                cleanup()

    def _askpass_env(self, token):
        """A private 0700 temp dir holding a GIT_ASKPASS helper that echoes the
        token from an env var — the token is never on a command line and never
        in the script body. Returns (env, cleanup)."""
        d = tempfile.mkdtemp(prefix="ghpush-askpass-")
        try:
            os.chmod(d, stat.S_IRWXU)       # 0700
        except OSError:
            pass
        if os.name == "nt":
            script = os.path.join(d, "askpass.bat")
            body = "@echo off\r\necho %GH_ASKPASS_TOKEN%\r\n"
        else:
            script = os.path.join(d, "askpass.sh")
            body = "#!/bin/sh\nprintf '%s' \"$GH_ASKPASS_TOKEN\"\n"
        with open(script, "w", encoding="ascii", newline="") as f:
            f.write(body)
        if os.name != "nt":
            try:
                os.chmod(script, stat.S_IRWXU)   # 0700
            except OSError:
                pass
        env = {
            "GIT_ASKPASS": script,
            "GH_ASKPASS_TOKEN": token,
            "GIT_TERMINAL_PROMPT": "0",
            # Belt and braces: no helper is read even if one is configured.
            "GIT_CONFIG_NOSYSTEM": "1",
        }

        def cleanup():
            shutil.rmtree(d, ignore_errors=True)

        return env, cleanup

    def _has_identity(self, cwd):
        rc, out, _ = self._git(["config", "user.email"], cwd=cwd)
        if rc == 0 and out.strip():
            rc2, out2, _ = self._git(["config", "user.name"], cwd=cwd)
            return rc2 == 0 and bool(out2.strip())
        return False

    def _is_repo(self, folder):
        return os.path.isdir(os.path.join(folder, ".git"))

    def _ensure_repo(self, folder):
        if self._is_repo(folder):
            return
        rc, _out, err = self._git(["init", "-b", "main"], cwd=folder)
        if rc != 0:
            # Older git without -b: init, then name the unborn branch main.
            rc2, _o2, err2 = self._git(["init"], cwd=folder)
            if rc2 != 0:
                raise GhError("git_error", "Could not create a git repository here. %s"
                              % _tail(err2 or err))
            self._git(["symbolic-ref", "HEAD", "refs/heads/main"], cwd=folder)

    def _current_branch(self, folder):
        rc, out, _ = self._git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=folder)
        b = (out or "").strip()
        return b if rc == 0 and b and b != "HEAD" else "main"

    def _has_commit(self, folder):
        rc, _out, _err = self._git(["rev-parse", "--verify", "HEAD"], cwd=folder)
        return rc == 0

    def _remotes(self, folder):
        rc, out, _ = self._git(["remote"], cwd=folder)
        return [r.strip() for r in (out or "").splitlines() if r.strip()] if rc == 0 else []

    def _github_remote_url(self, folder):
        rc, out, _ = self._git(["remote", "get-url", "github"], cwd=folder)
        return (out or "").strip() if rc == 0 else None

    def _wire_remote(self, folder, url):
        """Point the ``github`` remote at ``url`` (add it, or update ONLY the
        github remote — origin and any other remote are never touched)."""
        if "github" in self._remotes(folder):
            self._git(["remote", "set-url", "github", url], cwd=folder)
        else:
            self._git(["remote", "add", "github", url], cwd=folder)

    # ---- what would be committed -----------------------------------------
    def _staged_paths(self, folder):
        """Relative paths that would go into the next commit: everything git
        status shows as added/modified/untracked (ignored files are excluded by
        git). Deletions are left out."""
        rc, out, _ = self._git(
            ["status", "--porcelain", "-z", "--untracked-files=all"], cwd=folder)
        if rc != 0:
            return []
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
            if "D" in xy and path:
                # a pure deletion contributes no file to scan
                if xy in ("D ", " D", "DD"):
                    i += 1
                    continue
            if path:
                paths.append(path)
            i += 1
        return paths

    # ---- public operations ------------------------------------------------
    def preview(self, project_dir):
        """Files that would be committed (capped list + total) and the secret
        findings that would block a push."""
        folder = self._guard_folder(project_dir)
        self._ensure_repo(folder)
        paths = self._staged_paths(folder)
        findings = scan_secrets(folder, paths)
        return {
            "files": paths[:200],
            "total_files": len(paths),
            "findings": findings,
            "has_gitignore": os.path.isfile(os.path.join(folder, ".gitignore")),
        }

    def add_gitignore(self, project_dir, add=None, default=False):
        """Append lines to .gitignore (creating it). ``default`` writes the
        hub's default set; ``add`` is an explicit list of paths. Only ever
        appends lines not already present. Returns ``{written:[...]}``."""
        folder = self._guard_folder(project_dir)
        want = []
        if default:
            want.extend(DEFAULT_GITIGNORE)
        if add:
            for p in add:
                if isinstance(p, str) and p.strip():
                    want.append(p.strip())
        if not want:
            raise GhError("bad_request", "Nothing to add to .gitignore.")
        path = os.path.join(folder, ".gitignore")
        existing = ""
        present = set()
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    existing = f.read()
            except OSError:
                existing = ""
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
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(out)
        except OSError as exc:
            raise GhError("git_error", "Could not write .gitignore (%s)." % exc)
        return {"written": written}

    def create_repo(self, project_dir, name, private=True, public_ok=False):
        """Create a repo on the authenticated user's account (auto_init:false),
        wire the ``github`` remote and push. ``private`` defaults True; a public
        repo additionally needs ``public_ok`` true."""
        folder = self._guard_folder(project_dir)
        token = self._require_token()
        private = bool(private)
        if not private and not public_ok:
            raise GhError("public_ok_required",
                          "A public repo is visible to everyone. Confirm to make it public.")
        repo = sanitize_repo_name(name)
        if not repo:
            raise GhError("bad_name", "That repository name is not usable on GitHub.")
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
        self._ensure_repo(folder)
        acct = self.store.get_account() or {}
        login = info.get("owner", {}).get("login") or acct.get("login")
        self._wire_remote(folder, self.remote_url(login, repo))
        self._record(folder, info)
        # Push the first commit. A secrets block propagates (the repo now exists,
        # so a later Push after fixing them works).
        self.push(project_dir)
        return self.status(project_dir)

    def push(self, project_dir):
        """Secret-scan, stage (git add -A), commit only when there are changes,
        then ``git push github HEAD:main``. Never force-pushes."""
        folder = self._guard_folder(project_dir)
        self._require_token()
        self._ensure_repo(folder)
        if not self._github_remote_url(folder):
            raise GhError("not_connected", "Create a GitHub repository first.")
        paths = self._staged_paths(folder)
        findings = scan_secrets(folder, paths)
        if findings:
            raise GhError("secrets_found",
                          "Found something that should not be published. "
                          "Add these to .gitignore, or remove them, then push again.",
                          extra={"findings": findings,
                                 "has_gitignore": os.path.isfile(
                                     os.path.join(folder, ".gitignore"))})
        rc, _o, err = self._git(["add", "-A"], cwd=folder)
        if rc != 0:
            raise GhError("git_error", "Could not stage the files. %s" % _tail(err))
        staged_rc, _o2, _e2 = self._git(["diff", "--cached", "--quiet"], cwd=folder)
        has_commit = self._has_commit(folder)
        if staged_rc != 0:                       # there ARE staged changes
            crc, _oc, cerr = self._git(["commit", "-m", _COMMIT_MESSAGE],
                                       cwd=folder, identity=True)
            if crc != 0:
                raise GhError("git_error", "Could not commit. %s" % _tail(cerr))
        elif not has_commit:
            raise GhError("nothing_to_commit",
                          "This project has no files to commit yet.")
        branch = self._current_branch(folder)
        prc, _op, perr = self._git(["push", "github", "HEAD:main"], cwd=folder, auth=True)
        if prc != 0:
            if _is_non_fast_forward(perr):
                raise GhError("behind_remote",
                              "GitHub has changes this copy doesn't. Press Sync first, "
                              "then push again.")
            raise GhError("push_failed", "The push to GitHub failed. %s" % _tail(perr))
        self._touch_pushed(folder, branch)
        return self.status(project_dir)

    def sync(self, project_dir):
        """``git fetch github`` then a fast-forward-only merge. A diverged
        history is reported, never auto-merged or rebased."""
        folder = self._guard_folder(project_dir)
        self._require_token()
        if not self._is_repo(folder) or not self._github_remote_url(folder):
            raise GhError("not_connected", "Connect this project to GitHub first.")
        frc, _o, ferr = self._git(["fetch", "github"], cwd=folder, auth=True)
        if frc != 0:
            raise GhError("fetch_failed", "Could not fetch from GitHub. %s" % _tail(ferr))
        ahead, behind = self._ahead_behind(folder)
        if behind and ahead:
            raise GhError("diverged",
                          "This copy and GitHub have each changed since they last "
                          "matched (%d local, %d on GitHub). Open the project and "
                          "merge them by hand — the hub will not merge automatically."
                          % (ahead, behind))
        if behind:
            mrc, _om, merr = self._git(["merge", "--ff-only", "github/main"], cwd=folder)
            if mrc != 0:
                raise GhError("diverged",
                              "GitHub's changes could not be fast-forwarded in. "
                              "Merge them by hand. %s" % _tail(merr))
        ahead, behind = self._ahead_behind(folder)
        self._record_counts(folder, ahead, behind)
        return self.status(project_dir)

    def _ahead_behind(self, folder):
        """(ahead, behind) of HEAD vs github/main, or (None, None) when the
        remote branch is unknown."""
        rc, out, _ = self._git(
            ["rev-list", "--left-right", "--count", "HEAD...github/main"], cwd=folder)
        if rc != 0:
            return None, None
        try:
            left, right = (out or "").split()[:2]
            return int(left), int(right)
        except (ValueError, IndexError):
            return None, None

    # ---- project state ----------------------------------------------------
    def _key(self, folder):
        return os.path.normcase(os.path.realpath(os.path.abspath(folder)))

    def _record(self, folder, info):
        self.store.set_project(self._key(folder), {
            "full_name": info.get("full_name"),
            "html_url": info.get("html_url"),
            "private": bool(info.get("private")),
            "default_branch": info.get("default_branch") or "main",
            "created_at": self.clock(),
            "last_pushed": None,
            "ahead": None, "behind": None,
        })

    def _touch_pushed(self, folder, branch):
        key = self._key(folder)
        row = self.store.get_project(key) or {}
        row["last_pushed"] = self.clock()
        row["branch"] = branch
        row["ahead"] = 0
        self.store.set_project(key, row)

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
        Reads only local git and the remembered project row — no network."""
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
        is_repo = self._is_repo(folder)
        remote = self._github_remote_url(folder) if is_repo else None
        row = self.store.get_project(self._key(folder)) or {}
        repo = None
        if row.get("full_name"):
            repo = {"full_name": row.get("full_name"), "html_url": row.get("html_url"),
                    "private": row.get("private"), "default_branch": row.get("default_branch")}
        out["project"] = {
            "dir": folder,
            "is_repo": is_repo,
            "has_remote": bool(remote),
            "repo": repo,
            "branch": self._current_branch(folder) if is_repo else None,
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


# The module-level singleton app.py uses. Its guards are wired at boot.
default = GitHub()
