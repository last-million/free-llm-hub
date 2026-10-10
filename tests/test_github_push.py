r"""Push a Build project to GitHub (ghpush.py + the /api/github routes + the UI).

REQUESTED 2026-10-10: from the Build page, create a repo on the owner's OWN
account (private by default) the first time, then Push / Sync on a click. Never
automatic. This file pins the things that are easy to get dangerously wrong:

  * the token is validated against GET /user, stored ENCRYPTED (the provider-key
    mechanism), never returned by status, never written into .git/config, and
    never counted/corrupted by the config migrations' key fingerprint;
  * git auth goes through a GIT_ASKPASS helper that echoes an ENV VAR (the token
    is never on a command line and never in the script body), with
    `-c credential.helper=` empty and GIT_TERMINAL_PROMPT=0 so NO credential
    manager is read or written;
  * create is private by default, a public repo needs public_ok, a taken name is
    repo_exists; an existing repo keeps its remotes (a separate `github` remote
    is added);
  * a secret scan blocks the push and offers a .gitignore;
  * push commits only when there are changes, never force-pushes, reports
    behind_remote; sync is fast-forward only and reports a diverged history;
  * the hub repo, a too-broad folder and a linked folder are refused;
  * the flag off answers status but refuses the rest; the UI exists and uses
    theme tokens / textContent only.

HERMETIC: GitHub HTTP is a fake; git is REAL but runs in tmp_path against a
LOCAL BARE repo that stands in for the GitHub remote (an injected remote-URL
resolver). A fence fails loudly if anything reaches the real api.github.com.
"""
import io
import json
import os
import re
import subprocess

import pytest

import ghpush


# --------------------------------------------------------------------------- #
# Fence: no test may reach the real GitHub.                                    #
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _no_real_github(monkeypatch):
    def boom(*a, **k):
        raise AssertionError(
            "a test tried to reach the real api.github.com; inject a fake http")
    monkeypatch.setattr(ghpush, "_urllib_http", boom)
    try:
        monkeypatch.setattr(ghpush.default, "http", boom, raising=False)
    except Exception:                                            # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# Helpers.                                                                     #
# --------------------------------------------------------------------------- #
TOKEN = "ghp_" + "a" * 36


def _git_ok():
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=20)
        return True
    except Exception:                                            # noqa: BLE001
        return False


requires_git = pytest.mark.skipif(not _git_ok(), reason="git is not available")


class DictStore:
    """An in-memory token+projects store (no config / secretstore)."""

    def __init__(self, token=TOKEN, login="octocat", user_id=42):
        self._token, self._login, self._uid = token, login, user_id
        self.projects = {}

    def get_token(self):
        return self._token

    def get_account(self):
        if not self._token:
            return None
        return {"login": self._login, "user_id": self._uid,
                "masked_hint": ghpush._mask(self._token)}

    def set_account(self, token, login, user_id):
        self._token, self._login, self._uid = token, login, user_id

    def clear_account(self):
        self._token = None

    def get_projects(self):
        return dict(self.projects)

    def get_project(self, key):
        return self.projects.get(key)

    def set_project(self, key, data):
        if data is None:
            self.projects.pop(key, None)
        else:
            self.projects[key] = data


class FakeHTTP:
    """Routes (method, path) -> (status, dict). Records every call."""

    def __init__(self):
        self.routes = {
            ("GET", "/user"): (200, {"login": "octocat", "id": 42}),
            ("POST", "/user/repos"): (201, {
                "full_name": "octocat/myrepo",
                "html_url": "https://github.com/octocat/myrepo",
                "private": True, "default_branch": "main",
                "owner": {"login": "octocat"}}),
        }
        self.calls = []

    def set(self, method, path, status, data):
        self.routes[(method, path)] = (status, data)

    def __call__(self, method, url, headers, body):
        path = url[len(ghpush._API):] if url.startswith(ghpush._API) else url
        self.calls.append({"method": method, "path": path,
                           "headers": dict(headers), "body": body})
        status, data = self.routes.get((method, path), (404, {"message": "not found"}))
        return status, {}, json.dumps(data).encode()


def _raw_git(cwd, *args, check=True):
    """Run git for test SETUP (identity + no signing pinned so it works on any
    machine)."""
    full = ["git", "-c", "user.name=Tester", "-c", "user.email=t@example.com",
            "-c", "commit.gpgsign=false", "-c", "protocol.file.allow=always"] + list(args)
    p = subprocess.run(full, cwd=cwd, capture_output=True, text=True, timeout=60)
    if check and p.returncode != 0:
        raise AssertionError("git %s failed: %s" % (args, p.stderr))
    return p


def _mk_bare(tmp_path, name="remote.git"):
    bare = tmp_path / name
    _raw_git(str(tmp_path), "init", "--bare", "-b", "main", str(bare))
    return bare


def _spy_run_git(calls):
    def run_git(args, cwd=None, env=None):
        env = dict(env or {})
        rec = {"args": list(args), "cwd": cwd, "env": env}
        ap = env.get("GIT_ASKPASS")
        if ap:
            rec["askpass_exists"] = os.path.exists(ap)
            try:
                rec["askpass_body"] = io.open(ap, encoding="ascii").read()
            except Exception:                                    # noqa: BLE001
                rec["askpass_body"] = ""
        calls.append(rec)
        return ghpush._real_git(args, cwd=cwd, env=env)
    return run_git


def _gh(tmp_path, http=None, store=None, bare=None, calls=None, **kw):
    bare = bare or _mk_bare(tmp_path)
    calls = calls if calls is not None else []
    return ghpush.GitHub(
        http=http, run_git=_spy_run_git(calls),
        store=store or DictStore(),
        remote_url=lambda login, repo, _b=bare: str(_b),
        **kw), calls, bare


def _mk_project(tmp_path, name="proj", files=None):
    d = tmp_path / name
    d.mkdir()
    for fn, body in (files or {"README.md": "hello\n"}).items():
        p = d / fn
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return d


def _connected(tmp_path, http=None, files=None):
    """A project with a GitHub repo created + an initial commit pushed to the
    bare remote. Returns (gh, project, bare, calls, http)."""
    http = http or FakeHTTP()
    gh, calls, bare = _gh(tmp_path, http=http)
    proj = _mk_project(tmp_path, files=files)
    gh.create_repo(str(proj), "myrepo", private=True)
    return gh, proj, bare, calls, http


def _commits(cwd):
    p = _raw_git(cwd, "rev-list", "--count", "HEAD", check=False)
    try:
        return int((p.stdout or "0").strip())
    except ValueError:
        return 0


# --------------------------------------------------------------------------- #
# Token: validate / store encrypted / never returned.                         #
# --------------------------------------------------------------------------- #
@pytest.fixture
def real_store(tmp_path, monkeypatch):
    import config
    import secretstore
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(tmp_path / "state" / "config.json"))
    secretstore.reset_cache()
    config.invalidate_settings_cache()
    yield ghpush.ConfigStore()
    secretstore.reset_cache()
    config.invalidate_settings_cache()


def test_token_is_stored_encrypted_and_never_returned(real_store, tmp_path):
    import secretstore
    real_store.set_account(TOKEN, "octocat", 42)
    cfg_file = tmp_path / "state" / "config.json"
    raw_text = cfg_file.read_text(encoding="utf-8")
    raw = json.loads(raw_text)
    stored = raw["github"]["token"]
    if secretstore.available():
        assert secretstore.is_encrypted(stored), "the token must be stored as ciphertext"
        assert TOKEN not in raw_text, "the plaintext token must not be on disk"
    # Round-trips back to the real token for git auth.
    assert real_store.get_token() == TOKEN
    # The account view never carries the token value.
    acct = real_store.get_account()
    assert acct["login"] == "octocat"
    assert TOKEN not in json.dumps(acct)
    assert acct["masked_hint"] and TOKEN not in acct["masked_hint"]


def test_disconnect_clears_the_token(real_store):
    real_store.set_account(TOKEN, "octocat", 42)
    real_store.clear_account()
    assert real_store.get_token() is None
    assert real_store.get_account() is None


def test_validate_token_hits_user_endpoint_and_maps_errors(tmp_path):
    http = FakeHTTP()
    gh, _calls, _bare = _gh(tmp_path, http=http, store=DictStore(token=None))
    info = gh.validate_token(TOKEN)
    assert info == {"login": "octocat", "id": 42}
    assert http.calls[0]["method"] == "GET" and http.calls[0]["path"] == "/user"
    assert http.calls[0]["headers"]["Authorization"] == "Bearer " + TOKEN
    http.set("GET", "/user", 401, {"message": "Bad credentials"})
    with pytest.raises(ghpush.GhError) as e:
        gh.validate_token(TOKEN)
    assert e.value.code == "bad_token"
    assert TOKEN not in e.value.message


def test_connect_stores_and_returns_only_login_and_hint(tmp_path):
    http = FakeHTTP()
    store = DictStore(token=None)
    gh, _c, _b = _gh(tmp_path, http=http, store=store)
    out = gh.connect(TOKEN)
    assert out["login"] == "octocat"
    assert TOKEN not in json.dumps(out)
    assert store.get_token() == TOKEN


# --------------------------------------------------------------------------- #
# Create: private default, public needs public_ok, repo_exists.               #
# --------------------------------------------------------------------------- #
@requires_git
def test_create_defaults_to_private(tmp_path):
    http = FakeHTTP()
    gh, _calls, _bare = _gh(tmp_path, http=http)
    proj = _mk_project(tmp_path)
    gh.create_repo(str(proj), "myrepo")   # private defaults True
    post = [c for c in http.calls if c["method"] == "POST" and c["path"] == "/user/repos"]
    assert post, "a repo must be created"
    sent = json.loads(post[0]["body"].decode())
    assert sent == {"name": "myrepo", "private": True, "auto_init": False}


@requires_git
def test_public_requires_public_ok(tmp_path):
    http = FakeHTTP()
    gh, _c, _b = _gh(tmp_path, http=http)
    proj = _mk_project(tmp_path)
    with pytest.raises(ghpush.GhError) as e:
        gh.create_repo(str(proj), "myrepo", private=False)
    assert e.value.code == "public_ok_required"
    assert not http.calls, "no repo may be created without the public confirmation"
    gh.create_repo(str(proj), "myrepo", private=False, public_ok=True)
    sent = json.loads([c for c in http.calls if c["method"] == "POST"][0]["body"].decode())
    assert sent["private"] is False


@requires_git
def test_repo_exists_is_reported(tmp_path):
    http = FakeHTTP()
    http.set("POST", "/user/repos", 422,
             {"message": "Validation Failed",
              "errors": [{"message": "name already exists on this account"}]})
    gh, _c, _b = _gh(tmp_path, http=http)
    proj = _mk_project(tmp_path)
    with pytest.raises(ghpush.GhError) as e:
        gh.create_repo(str(proj), "myrepo")
    assert e.value.code == "repo_exists"


@requires_git
def test_bad_scope_on_create(tmp_path):
    http = FakeHTTP()
    http.set("POST", "/user/repos", 403, {"message": "Resource not accessible"})
    gh, _c, _b = _gh(tmp_path, http=http)
    proj = _mk_project(tmp_path)
    with pytest.raises(ghpush.GhError) as e:
        gh.create_repo(str(proj), "myrepo")
    assert e.value.code == "bad_scope"


def test_sanitize_repo_name():
    assert ghpush.sanitize_repo_name("My App!! v2 ") == "My-App-v2"
    assert ghpush.sanitize_repo_name("...") == ""
    assert ghpush.sanitize_repo_name("ok_name.ok-1") == "ok_name.ok-1"


# --------------------------------------------------------------------------- #
# init vs existing repo + a SEPARATE github remote.                           #
# --------------------------------------------------------------------------- #
@requires_git
def test_init_when_no_git(tmp_path):
    http = FakeHTTP()
    gh, _c, _b = _gh(tmp_path, http=http)
    proj = _mk_project(tmp_path)
    assert not (proj / ".git").exists()
    gh.create_repo(str(proj), "myrepo")
    assert (proj / ".git").is_dir()
    remotes = _raw_git(str(proj), "remote").stdout.split()
    assert "github" in remotes


@requires_git
def test_existing_repo_keeps_its_remotes(tmp_path):
    http = FakeHTTP()
    gh, _c, _b = _gh(tmp_path, http=http)
    proj = _mk_project(tmp_path)
    _raw_git(str(proj), "init", "-b", "main")
    _raw_git(str(proj), "remote", "add", "origin", "https://example.com/other.git")
    gh.create_repo(str(proj), "myrepo")
    remotes = set(_raw_git(str(proj), "remote").stdout.split())
    assert {"origin", "github"} <= remotes
    origin = _raw_git(str(proj), "remote", "get-url", "origin").stdout.strip()
    assert origin == "https://example.com/other.git"   # untouched


# --------------------------------------------------------------------------- #
# Secret scan + .gitignore.                                                    #
# --------------------------------------------------------------------------- #
@requires_git
def test_secret_file_blocks_push(tmp_path):
    gh, proj, _bare, _calls, _http = _connected(tmp_path)   # README pushed clean
    (proj / ".env").write_text("API_KEY=sekret\n", encoding="utf-8")
    with pytest.raises(ghpush.GhError) as e:
        gh.push(str(proj))
    assert e.value.code == "secrets_found"
    paths = [f["path"] for f in e.value.extra["findings"]]
    assert ".env" in paths


@requires_git
def test_secret_content_blocks_push(tmp_path):
    gh, proj, _bare, _calls, _http = _connected(tmp_path)
    (proj / "leak.js").write_text("const k = 'sk-" + "b" * 24 + "';\n", encoding="utf-8")
    with pytest.raises(ghpush.GhError) as e:
        gh.push(str(proj))
    assert e.value.code == "secrets_found"
    assert any(f["kind"] == "content" for f in e.value.extra["findings"])


def test_env_example_is_not_a_secret():
    findings = ghpush.scan_secrets(".", [])
    assert findings == []
    # .env.example alone is allowed; .env.local is not.
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, ".env.example"), "w").close()
        open(os.path.join(d, ".env.local"), "w").close()
        got = {f["path"]: f["kind"] for f in
               ghpush.scan_secrets(d, [".env.example", ".env.local"])}
    assert ".env.example" not in got
    assert got.get(".env.local") == "file"


@requires_git
def test_gitignore_add_unblocks_push(tmp_path):
    gh, proj, _bare, _calls, _http = _connected(tmp_path)   # README pushed clean
    (proj / ".env").write_text("API_KEY=sekret\n", encoding="utf-8")
    result = gh.add_gitignore(str(proj), add=[".env"], default=False)
    assert ".env" in result["written"]
    assert (proj / ".gitignore").read_text(encoding="utf-8").count(".env") >= 1
    # .env is now ignored -> no finding, and the push goes through.
    assert gh.preview(str(proj))["findings"] == []
    st = gh.push(str(proj))
    assert st["project"]["last_pushed"] is not None


@requires_git
def test_default_gitignore_has_the_expected_lines(tmp_path):
    gh, _c, _b = _gh(tmp_path, http=FakeHTTP())
    proj = _mk_project(tmp_path)
    gh.add_gitignore(str(proj), default=True)
    body = (proj / ".gitignore").read_text(encoding="utf-8")
    for line in ("node_modules/", ".env", "!.env.example", "__pycache__/"):
        assert line in body
    # Build output is deliberately NOT ignored by default.
    assert "dist/" not in body and "build/" not in body


# --------------------------------------------------------------------------- #
# Push: commits only on changes, never --force, behind_remote.                #
# --------------------------------------------------------------------------- #
@requires_git
def test_push_commits_only_on_changes(tmp_path):
    gh, proj, _bare, calls, _http = _connected(tmp_path)
    n1 = _commits(str(proj))
    gh.push(str(proj))                       # no change since create's push
    assert _commits(str(proj)) == n1, "an unchanged push must not create a commit"
    (proj / "new.txt").write_text("x\n", encoding="utf-8")
    gh.push(str(proj))
    assert _commits(str(proj)) == n1 + 1


@requires_git
def test_push_never_forces(tmp_path):
    gh, proj, _bare, calls, _http = _connected(tmp_path)
    (proj / "a.txt").write_text("1\n", encoding="utf-8")
    gh.push(str(proj))
    for rec in calls:
        if rec["args"] and "push" in rec["args"]:
            assert "--force" not in rec["args"] and "-f" not in rec["args"]
            assert not any(a.startswith("+") for a in rec["args"])


@requires_git
def test_behind_remote(tmp_path):
    gh, proj, bare, _calls, _http = _connected(tmp_path)
    # another clone advances the remote main
    clone = tmp_path / "clone"
    _raw_git(str(tmp_path), "clone", str(bare), str(clone))
    (clone / "other.txt").write_text("remote change\n", encoding="utf-8")
    _raw_git(str(clone), "add", "-A")
    _raw_git(str(clone), "commit", "-m", "remote")
    _raw_git(str(clone), "push", "origin", "HEAD:main")
    # local makes its own new commit, then push -> non-fast-forward
    (proj / "local.txt").write_text("local change\n", encoding="utf-8")
    with pytest.raises(ghpush.GhError) as e:
        gh.push(str(proj))
    assert e.value.code == "behind_remote"


@requires_git
def test_nothing_to_commit_on_an_empty_repo(tmp_path):
    http = FakeHTTP()
    gh, _calls, bare = _gh(tmp_path, http=http)
    empty = tmp_path / "empty"
    empty.mkdir()
    gh._ensure_repo(str(empty))
    gh._wire_remote(str(empty), str(bare))
    with pytest.raises(ghpush.GhError) as e:
        gh.push(str(empty))
    assert e.value.code == "nothing_to_commit"


# --------------------------------------------------------------------------- #
# Sync: fast-forward only, diverged reported.                                 #
# --------------------------------------------------------------------------- #
@requires_git
def test_sync_fast_forwards(tmp_path):
    gh, proj, bare, _calls, _http = _connected(tmp_path)
    clone = tmp_path / "clone"
    _raw_git(str(tmp_path), "clone", str(bare), str(clone))
    (clone / "fromremote.txt").write_text("r\n", encoding="utf-8")
    _raw_git(str(clone), "add", "-A")
    _raw_git(str(clone), "commit", "-m", "remote")
    _raw_git(str(clone), "push", "origin", "HEAD:main")
    st = gh.sync(str(proj))
    assert (proj / "fromremote.txt").exists(), "a fast-forward must bring the file in"
    assert st["project"]["behind"] == 0


@requires_git
def test_sync_reports_diverged(tmp_path):
    gh, proj, bare, _calls, _http = _connected(tmp_path)
    clone = tmp_path / "clone"
    _raw_git(str(tmp_path), "clone", str(bare), str(clone))
    (clone / "r.txt").write_text("r\n", encoding="utf-8")
    _raw_git(str(clone), "add", "-A")
    _raw_git(str(clone), "commit", "-m", "remote")
    _raw_git(str(clone), "push", "origin", "HEAD:main")
    # local diverges with its own commit
    (proj / "l.txt").write_text("l\n", encoding="utf-8")
    _raw_git(str(proj), "add", "-A")
    _raw_git(str(proj), "commit", "-m", "local")
    with pytest.raises(ghpush.GhError) as e:
        gh.sync(str(proj))
    assert e.value.code == "diverged"


# --------------------------------------------------------------------------- #
# git auth: askpass + empty credential.helper + no token on the command line.  #
# --------------------------------------------------------------------------- #
@requires_git
def test_askpass_and_empty_credential_helper(tmp_path):
    gh, proj, _bare, calls, _http = _connected(tmp_path)
    (proj / "x.txt").write_text("x\n", encoding="utf-8")
    gh.push(str(proj))
    push_calls = [c for c in calls if c["args"] and "push" in c["args"]]
    assert push_calls, "the push must have run"
    rec = push_calls[-1]
    env = rec["env"]
    assert env.get("GIT_TERMINAL_PROMPT") == "0"
    assert env.get("GH_ASKPASS_TOKEN") == TOKEN
    assert "GIT_ASKPASS" in env and rec.get("askpass_exists") is True
    # the helper reads the env var, it does not carry the token itself
    assert TOKEN not in rec.get("askpass_body", "")
    assert "GH_ASKPASS_TOKEN" in rec.get("askpass_body", "")
    # the token is never on the command line; a helper is disabled
    assert not any(TOKEN in a for a in rec["args"])
    assert "-c" in rec["args"] and "credential.helper=" in rec["args"]
    # and the temp askpass dir is cleaned up after the command
    assert not os.path.exists(env["GIT_ASKPASS"])


@requires_git
def test_token_never_written_into_git_config(tmp_path):
    gh, proj, _bare, _calls, _http = _connected(tmp_path)
    (proj / "y.txt").write_text("y\n", encoding="utf-8")
    gh.push(str(proj))
    cfg = (proj / ".git" / "config").read_text(encoding="utf-8")
    assert TOKEN not in cfg
    assert "github.com" not in cfg or "@github.com" not in cfg  # no token@host either


@requires_git
def test_commit_identity_injected_when_the_repo_has_none(tmp_path, monkeypatch):
    gh, proj, _bare, calls, _http = _connected(tmp_path)
    monkeypatch.setattr(gh, "_has_identity", lambda cwd: False)
    (proj / "z.txt").write_text("z\n", encoding="utf-8")
    calls.clear()
    gh.push(str(proj))
    commit = [c for c in calls if c["args"] and "commit" in c["args"]]
    assert commit, "a commit must have run"
    args = commit[0]["args"]
    assert "user.name=octocat" in args
    assert "user.email=42+octocat@users.noreply.github.com" in args


# --------------------------------------------------------------------------- #
# Refusals.                                                                     #
# --------------------------------------------------------------------------- #
def test_refuses_hub_repo(tmp_path):
    gh, _c, _b = _gh(tmp_path, http=FakeHTTP(), is_hub_repo=lambda p: True)
    proj = _mk_project(tmp_path)
    with pytest.raises(ghpush.GhError) as e:
        gh.preview(str(proj))
    assert e.value.code == "refused"


def test_refuses_too_broad_folder(tmp_path):
    gh, _c, _b = _gh(tmp_path, http=FakeHTTP(), too_broad=lambda p: True)
    proj = _mk_project(tmp_path)
    with pytest.raises(ghpush.GhError) as e:
        gh.push(str(proj))
    assert e.value.code == "refused"


def test_refuses_a_linked_folder(tmp_path, monkeypatch):
    proj = _mk_project(tmp_path)
    real_islink = os.path.islink
    monkeypatch.setattr(
        ghpush.os.path, "islink",
        lambda p, _r=real_islink, t=str(proj):
        True if os.path.abspath(p) == os.path.abspath(t) else _r(p))
    gh, _c, _b = _gh(tmp_path, http=FakeHTTP())
    with pytest.raises(ghpush.GhError) as e:
        gh.preview(str(proj))
    assert e.value.code == "refused"


def test_status_reports_a_refusal_without_raising(tmp_path):
    gh, _c, _b = _gh(tmp_path, http=FakeHTTP(), is_hub_repo=lambda p: True)
    proj = _mk_project(tmp_path)
    st = gh.status(str(proj))
    assert st["project"]["refused"]
    assert st["project"]["code"] == "refused"


# --------------------------------------------------------------------------- #
# Migrations never count or corrupt the GitHub token.                          #
# --------------------------------------------------------------------------- #
def test_github_token_is_invisible_to_migrations():
    import migrations
    raw = {"schema_version": 3,
           "providers": {"groq": {"api_keys": ["enc.v1:PROVIDERKEY"]}},
           "github": {"token": "enc.v1:GITHUBTOKEN", "login": "octocat", "user_id": 1}}
    before = migrations.key_fingerprint(raw)
    new, _applied = migrations.apply(raw)
    assert before["count"] == 1, "only the provider key is counted, never the github token"
    assert new["github"]["token"] == "enc.v1:GITHUBTOKEN"   # carried verbatim
    assert migrations.key_fingerprint(new)["count"] == 1


# --------------------------------------------------------------------------- #
# Routes (flag, confirm, status shape, token not echoed).                      #
# --------------------------------------------------------------------------- #
def _client():
    import app as A
    c = A.app.test_client()
    hdr = {"X-Free-LLM-Hub": "dashboard"}
    token = A.config.get_control_token()
    if token:
        hdr["X-Free-LLM-Hub-Token"] = token
    c._hdr = hdr
    return c, A


class _FakeDefault:
    def __init__(self):
        self.last = None

    def status(self, project_dir=None):
        return {"connected": True, "login": "octocat",
                "masked_hint": "ghp…aaaa", "project": None}

    def connect(self, token):
        self.last = token
        return {"login": "octocat", "masked_hint": "ghp…aaaa"}


def test_route_status_answers_even_when_flag_is_off(monkeypatch):
    c, A = _client()
    monkeypatch.setattr(A.config, "get_flag",
                        lambda name, default=False: False if name == "github_push" else default)
    monkeypatch.setattr(A.ghpush, "default", _FakeDefault())
    r = c.get("/api/github", headers=c._hdr)
    assert r.status_code == 200
    assert r.get_json()["enabled"] is False


def test_route_push_refused_when_flag_is_off(monkeypatch):
    c, A = _client()
    monkeypatch.setattr(A.config, "get_flag",
                        lambda name, default=False: False if name == "github_push" else default)
    r = c.post("/api/github/push", json={"project_dir": "x", "confirm": True}, headers=c._hdr)
    assert r.status_code == 403
    assert r.get_json()["code"] == "disabled"


def test_route_create_needs_confirm(monkeypatch):
    c, A = _client()
    monkeypatch.setattr(A.config, "get_flag", lambda name, default=False: True)
    r = c.post("/api/github/create", json={"project_dir": "x", "name": "y"}, headers=c._hdr)
    assert r.status_code == 400
    assert r.get_json()["code"] == "confirm_required"


def test_route_token_does_not_echo_the_token(monkeypatch):
    c, A = _client()
    fake = _FakeDefault()
    monkeypatch.setattr(A.config, "get_flag", lambda name, default=False: True)
    monkeypatch.setattr(A.ghpush, "default", fake)
    r = c.post("/api/github/token", json={"token": "ghp_secret_value_123"}, headers=c._hdr)
    assert r.status_code == 200
    j = r.get_json()
    assert j["login"] == "octocat"
    assert "ghp_secret_value_123" not in r.get_data(as_text=True)
    assert "token" not in j
    assert fake.last == "ghp_secret_value_123"   # it DID reach ghpush


# --------------------------------------------------------------------------- #
# The dashboard route count claim stays true.                                  #
# --------------------------------------------------------------------------- #
APP_SRC = io.open("app.py", encoding="utf-8").read()
README = io.open("README.md", encoding="utf-8").read()


def test_the_eight_github_routes_exist():
    for route in ("/api/github", "/api/github/token", "/api/github/token/delete",
                  "/api/github/preview", "/api/github/gitignore", "/api/github/create",
                  "/api/github/push", "/api/github/sync"):
        assert ('@app.route("%s"' % route) in APP_SRC, route


def test_route_count_claim_matches():
    assert str(len(re.findall(r"@app\.route\(", APP_SRC))) in README


# --------------------------------------------------------------------------- #
# UI static checks.                                                            #
# --------------------------------------------------------------------------- #
HTML = io.open("templates/index.html", encoding="utf-8").read()


def _between(start, end, src=HTML):
    i = src.index(start)
    return src[i:src.index(end, i)]


def test_the_github_button_and_dialog_exist():
    bar = _between('<div class="preview-bar" id="preview-bar">', 'id="preview-state"')
    assert 'id="preview-github"' in bar
    assert 'aria-controls="github-dialog"' in bar
    dlg = _between('<dialog class="publish-dialog github-dialog"', "</dialog>")
    for anid in ("github-token", "github-name", "github-vis-private", "github-vis-public",
                 "github-public-ok", "github-create-btn", "github-save", "github-push",
                 "github-sync", "github-disconnect", "github-findings", "github-gitignore"):
        assert ('id="%s"' % anid) in dlg, anid
    assert 'aria-labelledby="github-title"' in dlg
    assert 'aria-describedby="github-sub"' in dlg


def test_the_token_field_is_a_password_input():
    dlg = _between('<dialog class="publish-dialog github-dialog"', "</dialog>")
    assert re.search(r'<input type="password" id="github-token"', dlg)


def test_the_token_help_link_is_safe_and_correct():
    dlg = _between('<dialog class="publish-dialog github-dialog"', "</dialog>")
    assert "https://github.com/settings/personal-access-tokens/new" in dlg
    assert 'target="_blank" rel="noopener noreferrer"' in dlg


def test_the_github_css_block_uses_theme_tokens_only():
    css = re.sub(r"/\*.*?\*/", "", _between("/* github-css:start */", "/* github-css:end */"), flags=re.S)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css), "no raw hex colours in the github CSS"
    assert not re.search(r"\brgba?\(", css)
    assert not re.search(r"\bhsla?\(", css)
    assert "var(--" in css


def test_github_js_never_injects_server_strings_as_html():
    js = _between("/* github-js:start */", "/* github-js:end */")
    # every innerHTML assignment clears only ('' ); backend text goes via textContent
    for m in re.finditer(r"\.innerHTML\s*=\s*([^;]+);", js):
        assert m.group(1).strip() in ("''", '""'), "innerHTML must only ever be cleared"
    assert "textContent" in js
    assert "ghSafeUrl" in js   # a repo link is only ever validated https
