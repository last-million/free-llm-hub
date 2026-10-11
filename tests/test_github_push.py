r"""Push a Build project to GitHub (ghpush.py + the /api/github routes + the UI).

REQUESTED 2026-10-10: from the Build page, create a repo on the owner's OWN
account (private by default) the first time, then Push / Sync on a click. Never
automatic. HARDENED 2026-10-11 after two security reviews: a project's own
.git/config, hooks and .gitattributes are written by AI agents that can be
prompt-injected, and ~/.gitconfig is writable by the same user, so the git
process that carries the token never reads either. This file pins:

  * the token is validated against GET /user, stored ENCRYPTED (the provider-key
    mechanism), never returned by status, never written anywhere git keeps
    config, and never counted/corrupted by the config migrations' key
    fingerprint;
  * every git call runs on a HUB-OWNED BARE MIRROR (never the project's .git)
    with a sanitized environment (inherited GIT_* dropped, an empty hub-owned
    global config, HOME pointed at a hub dir), an EMPTY private hooks dir and
    the safety -c set; push/fetch go to the EXPLICIT stored URL;
  * a hostile project config (url-specific sslVerify/proxy/insteadOf/pushurl/
    credential helpers/filters + .gitattributes/gpg.program/fsmonitor/hooks)
    and a hostile global config / GIT_* environment are PROVEN ignored: no
    marker file is ever written and the push lands in the stored-URL repo;
  * the askpass answers ONLY git's exact prompts for https://github.com;
  * create is private by default, public needs public_ok, a taken name is
    repo_exists, the secret scan runs BEFORE the repo is created, a linked
    project cannot be re-linked by a request, a stored URL that does not belong
    to the connected account is refused;
  * push commits only on changes, never force-pushes, reports behind_remote;
    sync fast-forwards the work tree only when it is clean, else local_changes /
    diverged with nothing touched;
  * the hub repo, a too-broad folder, the hub's own state dir, a linked folder
    and a folder that is not a Build project are refused; a .gitignore that is a
    link is refused and its target is never written;
  * the flag off answers status but refuses the rest; the UI exists and uses
    theme tokens / textContent only.

HERMETIC: GitHub HTTP is a fake; git is REAL but runs in tmp_path against
LOCAL BARE repos that stand in for GitHub (an injected transport_url maps the
validated stored https URL to one). A fence fails loudly if anything reaches the
real api.github.com.
"""
import hashlib
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
STORED = "https://github.com/octocat/myrepo.git"


def _git_ok():
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=20)
        return True
    except Exception:                                            # noqa: BLE001
        return False


requires_git = pytest.mark.skipif(not _git_ok(), reason="git is not available")


class DictStore:
    """An in-memory token+projects store (no config / secretstore)."""

    def __init__(self, token=TOKEN, login="octocat", user_id=42, client_id=None):
        self._token, self._login, self._uid = token, login, user_id
        self.projects = {}
        self.client_id = client_id

    def get_client_id(self):
        return self.client_id

    def set_client_id(self, value):
        self.client_id = value

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
                "name": "myrepo", "full_name": "octocat/myrepo",
                "html_url": "https://github.com/octocat/myrepo",
                "private": True, "default_branch": "main",
                "owner": {"login": "octocat"}}),
        }
        self.calls = []
        self.seq = {}

    def set(self, method, path, status, data):
        self.routes[(method, path)] = (status, data)

    def queue(self, method, path, *answers):
        """Answers returned one per call (then the last one repeats)."""
        self.seq[(method, path)] = list(answers)

    def __call__(self, method, url, headers, body):
        path = url[len(ghpush._API):] if url.startswith(ghpush._API) else url
        self.calls.append({"method": method, "path": path,
                           "headers": dict(headers), "body": body})
        q = self.seq.get((method, path))
        if q:
            status, data = q.pop(0) if len(q) > 1 else q[0]
        else:
            status, data = self.routes.get((method, path), (404, {"message": "not found"}))
        return status, {}, json.dumps(data).encode()


def _clean_env():
    """The test's own git setup never inherits a hostile GIT_* / HOME a test
    planted."""
    return {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}


def _raw_git(cwd, *args, check=True, env=None):
    """Run git for test SETUP (identity + no signing pinned so it works on any
    machine)."""
    full = ["git", "-c", "user.name=Tester", "-c", "user.email=t@example.com",
            "-c", "commit.gpgsign=false", "-c", "protocol.file.allow=always"] + list(args)
    p = subprocess.run(full, cwd=cwd, capture_output=True, text=True, timeout=60,
                       env=env if env is not None else _clean_env())
    if check and p.returncode != 0:
        raise AssertionError("git %s failed: %s" % (args, p.stderr))
    return p


def _mk_bare(tmp_path, name="remote.git"):
    bare = tmp_path / name
    _raw_git(str(tmp_path), "init", "--bare", "-b", "main", str(bare))
    return bare


def _refs(bare):
    return _raw_git(str(bare), "for-each-ref", check=False).stdout.strip()


def _bare_show(bare, path):
    return _raw_git(str(bare), "show", "main:" + path, check=False)


def _bare_count(bare):
    p = _raw_git(str(bare), "rev-list", "--count", "main", check=False)
    return int(p.stdout.strip() or "0") if p.returncode == 0 else 0


def _spy_run_git(calls):
    def run_git(args, cwd=None, env=None, **kw):
        env = dict(env or {})
        rec = {"args": list(args), "cwd": cwd, "env": env}
        ap = env.get("GIT_ASKPASS")
        if ap:
            rec["askpass_exists"] = os.path.exists(ap)
            try:
                rec["askpass_body"] = io.open(ap, encoding="ascii").read()
            except Exception:                                    # noqa: BLE001
                rec["askpass_body"] = ""
        hooks = [a.split("=", 1)[1] for a in args if a.startswith("core.hooksPath=")]
        if hooks:
            rec["hooks_dir"] = hooks[-1]
            rec["hooks_empty"] = os.path.isdir(hooks[-1]) and not os.listdir(hooks[-1])
        calls.append(rec)
        return ghpush._real_git(args, cwd=cwd, env=env, **kw)
    return run_git


def _gh(tmp_path, http=None, store=None, bare=None, calls=None, routes=None, **kw):
    """A GitHub client on a tmp mirror root whose transport maps the STORED
    canonical URL to a local bare repo (and nothing else)."""
    bare = bare or _mk_bare(tmp_path)
    calls = calls if calls is not None else []
    table = {STORED: str(bare)}
    table.update(routes or {})
    kw.setdefault("allowed_protocols", ("https", "file"))
    kw.setdefault("mirror_root", str(tmp_path / "hub" / "github-mirrors"))
    kw.setdefault("transport_url",
                  lambda url, _t=table: _t.get(url, os.path.join(str(tmp_path), "nowhere.git")))
    return ghpush.GitHub(http=http or FakeHTTP(), run_git=_spy_run_git(calls),
                         store=store or DictStore(), **kw), calls, bare


def _mk_project(tmp_path, name="proj", files=None):
    d = tmp_path / name
    d.mkdir()
    for fn, body in (files or {"README.md": "hello\n"}).items():
        p = d / fn
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return d


def _connected(tmp_path, http=None, files=None):
    """A project with a GitHub repo created + an initial snapshot pushed to the
    stand-in bare remote. Returns (gh, project, bare, calls, http)."""
    http = http or FakeHTTP()
    gh, calls, bare = _gh(tmp_path, http=http)
    proj = _mk_project(tmp_path, files=files)
    gh.create_repo(str(proj), "myrepo", private=True)
    return gh, proj, bare, calls, http


def _advance_remote(tmp_path, bare, name="fromremote.txt"):
    clone = tmp_path / ("clone-" + name.replace(".", "-"))
    _raw_git(str(tmp_path), "clone", "-q", str(bare), str(clone))
    (clone / name).write_text("remote change\n", encoding="utf-8")
    _raw_git(str(clone), "add", "-A")
    _raw_git(str(clone), "commit", "-q", "-m", "remote")
    _raw_git(str(clone), "push", "-q", "origin", "HEAD:main")


def _marker_script(tmp_path, markers, name):
    """A program that writes markers/<name> when anything runs it (git runs
    it through Git's own sh on Windows)."""
    p = tmp_path / ("run-" + name + ".sh")
    p.write_text('#!/bin/sh\necho ran > "%s"\ncat >/dev/null 2>&1\nexit 0\n'
                 % (markers / name).as_posix(), encoding="utf-8", newline="\n")
    os.chmod(str(p), 0o755)
    return p.as_posix()


def _plant_hooks(dirpath, markers):
    os.makedirs(dirpath, exist_ok=True)
    for name in ("pre-push", "reference-transaction", "post-merge", "pre-commit",
                 "post-commit", "post-checkout", "pre-auto-gc"):
        p = os.path.join(dirpath, name)
        with open(p, "w", newline="\n") as f:
            f.write('#!/bin/sh\necho "ran:$GH_ASKPASS_TOKEN" > "%s"\nexit 0\n'
                    % (markers / ("hook-" + name)).as_posix())
        os.chmod(p, 0o755)


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
    raw_text = (tmp_path / "state" / "config.json").read_text(encoding="utf-8")
    stored = json.loads(raw_text)["github"]["token"]
    if secretstore.available():
        assert secretstore.is_encrypted(stored), "the token must be stored as ciphertext"
        assert TOKEN not in raw_text, "the plaintext token must not be on disk"
    assert real_store.get_token() == TOKEN
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
    assert gh.validate_token(TOKEN) == {"login": "octocat", "id": 42}
    assert http.calls[0]["method"] == "GET" and http.calls[0]["path"] == "/user"
    assert http.calls[0]["headers"]["Authorization"] == "Bearer " + TOKEN
    http.set("GET", "/user", 401, {"message": "Bad credentials"})
    with pytest.raises(ghpush.GhError) as e:
        gh.validate_token(TOKEN)
    assert e.value.code == "bad_token"
    assert TOKEN not in e.value.message


def test_connect_stores_and_returns_only_login_and_hint(tmp_path):
    store = DictStore(token=None)
    gh, _c, _b = _gh(tmp_path, store=store)
    out = gh.connect(TOKEN)
    assert out["login"] == "octocat"
    assert TOKEN not in json.dumps(out)
    assert store.get_token() == TOKEN


# --------------------------------------------------------------------------- #
# Create.                                                                      #
# --------------------------------------------------------------------------- #
@requires_git
def test_create_defaults_to_private_and_stores_the_canonical_url(tmp_path):
    http = FakeHTTP()
    gh, _calls, bare = _gh(tmp_path, http=http)
    proj = _mk_project(tmp_path)
    st = gh.create_repo(str(proj), "myrepo")
    post = [c for c in http.calls if c["method"] == "POST" and c["path"] == "/user/repos"]
    assert json.loads(post[0]["body"].decode()) == \
        {"name": "myrepo", "private": True, "auto_init": False}
    row = gh.store.get_project(gh._key(str(proj)))
    assert row["url"] == STORED
    assert st["project"]["repo"]["html_url"] == "https://github.com/octocat/myrepo"
    assert _bare_show(bare, "README.md").stdout == "hello\n"


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
    with pytest.raises(ghpush.GhError) as e:
        gh.create_repo(str(_mk_project(tmp_path)), "myrepo")
    assert e.value.code == "repo_exists"


@requires_git
def test_bad_scope_on_create(tmp_path):
    http = FakeHTTP()
    http.set("POST", "/user/repos", 403, {"message": "Resource not accessible"})
    gh, _c, _b = _gh(tmp_path, http=http)
    with pytest.raises(ghpush.GhError) as e:
        gh.create_repo(str(_mk_project(tmp_path)), "myrepo")
    assert e.value.code == "bad_scope"


@requires_git
def test_create_refused_before_post_when_secrets_exist(tmp_path):
    http = FakeHTTP()
    gh, _c, bare = _gh(tmp_path, http=http)
    proj = _mk_project(tmp_path, files={"README.md": "hi\n", ".env": "API_KEY=x\n"})
    with pytest.raises(ghpush.GhError) as e:
        gh.create_repo(str(proj), "myrepo")
    assert e.value.code == "secrets_found"
    assert ".env" in [f["path"] for f in e.value.extra["findings"]]
    assert not [c for c in http.calls if c["method"] == "POST"], \
        "nothing may be created on GitHub when the scan finds a secret"
    assert _refs(bare) == ""


@requires_git
def test_a_linked_project_cannot_be_relinked_by_a_request(tmp_path):
    gh, proj, _bare, _calls, http = _connected(tmp_path)
    n = len(http.calls)
    with pytest.raises(ghpush.GhError) as e:
        gh.create_repo(str(proj), "other")
    assert e.value.code == "already_linked"
    assert len(http.calls) == n, "no second repo is created"
    assert gh.store.get_project(gh._key(str(proj)))["url"] == STORED


@requires_git
def test_github_answer_for_another_account_is_not_linked(tmp_path):
    http = FakeHTTP()
    http.set("POST", "/user/repos", 201, {"name": "myrepo", "full_name": "mallory/myrepo",
                                          "private": True, "owner": {"login": "mallory"}})
    gh, _c, _b = _gh(tmp_path, http=http)
    proj = _mk_project(tmp_path)
    with pytest.raises(ghpush.GhError) as e:
        gh.create_repo(str(proj), "myrepo")
    assert e.value.code == "github_error"
    assert gh.store.get_project(gh._key(str(proj))) is None


def test_sanitize_repo_name():
    assert ghpush.sanitize_repo_name("My App!! v2 ") == "My-App-v2"
    assert ghpush.sanitize_repo_name("...") == ""
    assert ghpush.sanitize_repo_name("ok_name.ok-1") == "ok_name.ok-1"


def test_valid_url_accepts_only_the_connected_accounts_github_https():
    ok = "https://github.com/octocat/my.repo-1.git"
    assert ghpush.valid_url(ok, "octocat")
    assert ghpush.valid_url("https://github.com/OctoCat/r.git", "octocat")
    for bad in ("https://github.com/mallory/r.git", "http://github.com/octocat/r.git",
                "https://github.com.evil.example/octocat/r.git",
                "https://evil.example/octocat/r.git", "https://github.com/octocat/r",
                "https://github.com/octocat/../r.git", "https://x@github.com/octocat/r.git",
                "https://github.com/octocat/r.git/", "ext::sh -c x", "/tmp/r.git",
                "https://github.com/octocat/...git"):
        assert not ghpush.valid_url(bad, "octocat"), bad


# --------------------------------------------------------------------------- #
# The hub-owned mirror: the project's .git is never written.                  #
# --------------------------------------------------------------------------- #
@requires_git
def test_create_uses_a_hub_mirror_and_never_creates_a_project_git(tmp_path):
    gh, proj, bare, _calls, _http = _connected(tmp_path)
    assert not (proj / ".git").exists(), "the project gets no .git from this feature"
    m = gh.mirror_path(str(proj))
    digest = hashlib.sha256(os.path.normcase(os.path.realpath(str(proj))).encode()).hexdigest()[:16]
    assert os.path.basename(m) == digest + ".git"
    assert os.path.dirname(m) == str(tmp_path / "hub" / "github-mirrors")
    assert _raw_git(m, "rev-parse", "--is-bare-repository").stdout.strip() == "true"
    assert _bare_count(bare) == 1


@requires_git
def test_existing_history_is_imported_once_and_the_project_git_is_untouched(tmp_path):
    proj = _mk_project(tmp_path)
    _raw_git(str(proj), "init", "-q", "-b", "main")
    _raw_git(str(proj), "add", "-A")
    _raw_git(str(proj), "commit", "-q", "-m", "original")
    _raw_git(str(proj), "remote", "add", "origin", "https://example.com/other.git")
    orig = _raw_git(str(proj), "rev-parse", "HEAD").stdout.strip()
    before = {p: (proj / ".git" / p).read_bytes() for p in ("config", "HEAD")}
    gh, _calls, bare = _gh(tmp_path)
    (proj / "new.txt").write_text("n\n", encoding="utf-8")
    gh.create_repo(str(proj), "myrepo")
    gh.push(str(proj))
    after = {p: (proj / ".git" / p).read_bytes() for p in ("config", "HEAD")}
    assert before == after, "the project's own .git must never be written"
    assert _raw_git(str(proj), "rev-parse", "HEAD").stdout.strip() == orig
    assert _raw_git(str(bare), "merge-base", "--is-ancestor", orig, "main",
                    check=False).returncode == 0, "the project's history is kept"
    assert _bare_show(bare, "new.txt").returncode == 0


# --------------------------------------------------------------------------- #
# Secret scan + .gitignore.                                                    #
# --------------------------------------------------------------------------- #
@requires_git
def test_secret_file_blocks_push(tmp_path):
    gh, proj, bare, _calls, _http = _connected(tmp_path)
    (proj / ".env").write_text("API_KEY=sekret\n", encoding="utf-8")
    with pytest.raises(ghpush.GhError) as e:
        gh.push(str(proj))
    assert e.value.code == "secrets_found"
    assert ".env" in [f["path"] for f in e.value.extra["findings"]]
    assert _bare_show(bare, ".env").returncode != 0


@requires_git
def test_secret_content_blocks_push(tmp_path):
    gh, proj, _bare, _calls, _http = _connected(tmp_path)
    (proj / "leak.js").write_text("const k = 'sk-" + "b" * 24 + "';\n", encoding="utf-8")
    with pytest.raises(ghpush.GhError) as e:
        gh.push(str(proj))
    assert e.value.code == "secrets_found"
    assert any(f["kind"] == "content" for f in e.value.extra["findings"])


def test_env_example_is_not_a_secret():
    assert not ghpush.is_secret_name(".env.example")
    assert ghpush.is_secret_name(".env.local")
    assert ghpush.scan_blob(".env.example", b"KEY=\n") == []
    assert [f["kind"] for f in ghpush.scan_blob("a/.env.local", b"")] == ["file"]


@pytest.mark.parametrize("name", [
    "id_ed25519", "id_ed25519.pub", "id_ecdsa", "id_dsa", "id_rsa", "cert.pfx",
    "release.jks", "app.keystore", ".npmrc", ".pypirc", ".netrc", ".git-credentials",
    "prod.tfstate", "prod.tfstate.backup", "server.pem", "tls.key", "store.p12",
    "credentials.json", ".env"])
def test_each_secret_file_name_is_found(name):
    assert ghpush.is_secret_name(name), name
    assert any(f["kind"] == "file" for f in ghpush.scan_blob("sub/" + name, b"x"))


@pytest.mark.parametrize("name", ["README.md", "keyboard.js", "npmrc.txt", "tfstate.md",
                                  "env.py", ".env.example"])
def test_ordinary_file_names_are_not_secrets(name):
    assert not ghpush.is_secret_name(name), name


@pytest.mark.parametrize("data", [
    b"STRIPE=sk_live_" + b"a1B2c3D4e5F6g7H8i9J0k1L2",
    b"r = 'rk_live_" + b"Z9y8X7w6V5u4T3s2R1q0'",
    b"token: glpat-" + b"abcdefghij0123456789",
    b"HF=hf_" + b"A" * 34,
    b"key xai-" + b"q" * 48,
    b'{"type": "service_account", "private_key": "-----BEGIN PRIVATE KEY-----\\nMII"}',
    b"-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n",
    b"ghp_" + b"c" * 36,
    b"\x00\xff\x10binary AKIA" + b"ABCDEFGHIJKLMNOP" + b"\x00\xfe",
])
def test_each_secret_content_pattern_is_found(data):
    found = ghpush.scan_blob("f.bin", data)
    assert any(f["kind"] == "content" for f in found), data[:40]


@pytest.mark.parametrize("data", [b"sk_test_abcdefghijklmnopqrstuv", b"hf_short",
                                  b"no secrets here\n", b"\x00\x01\x02"])
def test_ordinary_content_is_not_flagged(data):
    assert ghpush.scan_blob("f.txt", data) == []


@requires_git
def test_gitignore_add_unblocks_push(tmp_path):
    gh, proj, _bare, _calls, _http = _connected(tmp_path)
    (proj / ".env").write_text("API_KEY=sekret\n", encoding="utf-8")
    assert ".env" in gh.add_gitignore(str(proj), add=[".env"])["written"]
    assert gh.preview(str(proj))["findings"] == []
    assert gh.push(str(proj))["project"]["last_pushed"] is not None


@requires_git
def test_default_gitignore_has_the_expected_lines(tmp_path):
    gh, _c, _b = _gh(tmp_path)
    proj = _mk_project(tmp_path)
    gh.add_gitignore(str(proj), default=True)
    body = (proj / ".gitignore").read_text(encoding="utf-8")
    for line in ("node_modules/", ".env", "!.env.example", "__pycache__/"):
        assert line in body
    assert "dist/" not in body and "build/" not in body   # build output is the owner's call


def test_gitignore_entries_are_plain_paths(tmp_path):
    gh, _c, _b = _gh(tmp_path)
    proj = _mk_project(tmp_path)
    for bad in ("!.env", "# x", "a\nb", "a\rb"):
        with pytest.raises(ghpush.GhError) as e:
            gh.add_gitignore(str(proj), add=[bad])
        assert e.value.code == "bad_request"
    assert not (proj / ".gitignore").exists()


def test_a_gitignore_link_is_refused_and_its_target_untouched(tmp_path):
    gh, _c, _b = _gh(tmp_path)
    proj = _mk_project(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("precious\n", encoding="utf-8")
    try:
        os.symlink(str(outside), str(proj / ".gitignore"))
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("this OS / account cannot create symlinks")
    with pytest.raises(ghpush.GhError) as e:
        gh.add_gitignore(str(proj), add=["secret.txt"])
    assert e.value.code == "refused"
    assert outside.read_text(encoding="utf-8") == "precious\n"
    assert os.path.islink(str(proj / ".gitignore"))


def test_gitignore_write_replaces_the_file_atomically(tmp_path):
    gh, _c, _b = _gh(tmp_path)
    proj = _mk_project(tmp_path)
    (proj / ".gitignore").write_text("keep-me\n", encoding="utf-8")
    gh.add_gitignore(str(proj), add=["x.log"])
    body = (proj / ".gitignore").read_text(encoding="utf-8")
    assert body.startswith("keep-me\n") and "x.log" in body
    assert not [p for p in os.listdir(str(proj)) if p.endswith(".tmp")]


# --------------------------------------------------------------------------- #
# The scan reads EXACTLY the staged blobs and fails CLOSED (2026-10-11).        #
# --------------------------------------------------------------------------- #
def _staged_in_mirror(gh, proj):
    """What the mirror's index holds beyond main (empty = nothing staged)."""
    m = gh.mirror_path(str(proj))
    return _raw_git(str(proj), "--git-dir=" + m, "--work-tree=" + str(proj),
                    "diff", "--cached", "--name-only").stdout.strip()


def _wrapped(inner, before=None, after=None):
    """A run_git around the spy: `before(args)` runs first, `after(args, result)`
    may rewrite the result."""
    def run_git(args, cwd=None, env=None, **kw):
        if before:
            before(args)
        res = inner(args, cwd=cwd, env=env, **kw)
        return after(args, res) if after else res
    return run_git


@requires_git
def test_a_file_over_the_cap_blocks_until_ignored(tmp_path):
    gh, _calls, bare = _gh(tmp_path, scan_cap=1000)
    proj = _mk_project(tmp_path)
    gh.create_repo(str(proj), "myrepo")
    n = _bare_count(bare)
    (proj / "big.bin").write_bytes(b"x" * 2000)
    with pytest.raises(ghpush.GhError) as e:
        gh.push(str(proj))
    assert e.value.code == "secrets_found"
    assert [f["kind"] for f in e.value.extra["findings"] if f["path"] == "big.bin"] == ["too_large"]
    assert _bare_count(bare) == n and _staged_in_mirror(gh, proj) == ""
    gh.add_gitignore(str(proj), add=["big.bin"])          # the "add these" flow
    gh.push(str(proj))
    assert _bare_show(bare, "big.bin").returncode != 0


@requires_git
def test_an_unreadable_blob_blocks(tmp_path):
    proj = _mk_project(tmp_path)
    (proj / "odd.txt").write_bytes(b"odd\n")      # bytes: no CRLF for autocrlf to rewrite
    target = _raw_git(str(proj), "hash-object", "--no-filters", "odd.txt").stdout.strip().encode()

    def hide(args, res):
        rc, out, err = res
        if "cat-file" in args and "--batch-check" in args:
            out = out.replace(target + b" blob", target + b" missing")
        return rc, out, err
    calls = []
    gh, _c, bare = _gh(tmp_path, calls=calls)
    gh.run_git = _wrapped(_spy_run_git(calls), after=hide)
    with pytest.raises(ghpush.GhError) as e:
        gh.create_repo(str(proj), "myrepo")
    assert e.value.code == "secrets_found"
    assert {"path": "odd.txt", "kind": "unchecked"} in [
        {"path": f["path"], "kind": f["kind"]} for f in e.value.extra["findings"]]
    assert not [c for c in gh.http.calls if c["method"] == "POST"]
    assert _staged_in_mirror(gh, proj) == ""


@requires_git
def test_a_scan_error_raises_and_never_passes(tmp_path):
    gh, proj, bare, calls, _http = _connected(tmp_path)
    n = _bare_count(bare)

    def broken(args, res):
        if "cat-file" in args and "--batch" in args:
            return 1, b"", "fatal: simulated failure"
        return res
    gh.run_git = _wrapped(_spy_run_git(calls), after=broken)
    (proj / "new.txt").write_text("n\n", encoding="utf-8")
    with pytest.raises(ghpush.GhError) as e:
        gh.push(str(proj))
    assert e.value.code == "scan_failed"
    assert _bare_count(bare) == n
    assert _staged_in_mirror(gh, proj) == ""


@requires_git
def test_a_secret_past_512_kb_is_found(tmp_path):
    gh, proj, bare, _calls, _http = _connected(tmp_path)
    (proj / "bundle.js").write_bytes(b"a" * (600 * 1024) + b"\nconst t='ghp_" + b"d" * 36 + b"';\n")
    with pytest.raises(ghpush.GhError) as e:
        gh.push(str(proj))
    assert e.value.code == "secrets_found"
    assert any(f["path"] == "bundle.js" and f["kind"] == "content"
               for f in e.value.extra["findings"])
    assert _bare_show(bare, "bundle.js").returncode != 0


@requires_git
def test_a_file_rewritten_just_before_add_is_caught(tmp_path):
    """The old scan read the work tree, then `add -A` ran: a secret written in
    between was committed unscanned. The scan now reads the STAGED blob."""
    gh, proj, bare, calls, _http = _connected(tmp_path)
    n = _bare_count(bare)
    (proj / "late.txt").write_text("harmless\n", encoding="utf-8")
    fired = []

    def plant(args):
        if "add" in args and "-A" in args and not fired:
            fired.append(1)
            (proj / "late.txt").write_text("k = 'ghp_" + "e" * 36 + "'\n", encoding="utf-8")
    gh.run_git = _wrapped(_spy_run_git(calls), before=plant)
    with pytest.raises(ghpush.GhError) as e:
        gh.push(str(proj))
    assert fired
    assert e.value.code == "secrets_found"
    assert any(f["path"] == "late.txt" and f["kind"] == "content"
               for f in e.value.extra["findings"])
    assert _bare_count(bare) == n and _bare_show(bare, "late.txt").returncode != 0
    assert _staged_in_mirror(gh, proj) == "", "the index is reset after a block"


@requires_git
def test_preview_matches_the_push_scan_and_leaves_nothing_staged(tmp_path):
    gh, proj, _bare, _calls, _http = _connected(tmp_path)
    (proj / "deploy.pfx").write_bytes(b"\x30\x82")
    (proj / "ok.txt").write_text("ok\n", encoding="utf-8")
    pv = gh.preview(str(proj))
    assert set(pv["files"]) == {"deploy.pfx", "ok.txt"}
    assert [f["path"] for f in pv["findings"]] == ["deploy.pfx"]
    assert _staged_in_mirror(gh, proj) == ""


# --------------------------------------------------------------------------- #
# Push.                                                                        #
# --------------------------------------------------------------------------- #
@requires_git
def test_push_commits_only_on_changes(tmp_path):
    gh, proj, bare, _calls, _http = _connected(tmp_path)
    n1 = _bare_count(bare)
    gh.push(str(proj))
    assert _bare_count(bare) == n1, "an unchanged push must not create a commit"
    (proj / "new.txt").write_text("x\n", encoding="utf-8")
    gh.push(str(proj))
    assert _bare_count(bare) == n1 + 1


@requires_git
def test_push_never_forces(tmp_path):
    gh, proj, _bare, calls, _http = _connected(tmp_path)
    (proj / "a.txt").write_text("1\n", encoding="utf-8")
    gh.push(str(proj))
    pushes = [r["args"] for r in calls if "push" in r["args"]]
    assert pushes
    for a in pushes:
        assert "--force" not in a and "-f" not in a and "--force-with-lease" not in a
        assert not any(x.startswith("+") for x in a)


@requires_git
def test_behind_remote(tmp_path):
    gh, proj, bare, _calls, _http = _connected(tmp_path)
    _advance_remote(tmp_path, bare)
    (proj / "local.txt").write_text("local change\n", encoding="utf-8")
    with pytest.raises(ghpush.GhError) as e:
        gh.push(str(proj))
    assert e.value.code == "behind_remote"


@requires_git
def test_nothing_to_commit_on_an_empty_project(tmp_path):
    gh, _calls, _bare = _gh(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ghpush.GhError) as e:
        gh.create_repo(str(empty), "myrepo")
    assert e.value.code == "nothing_to_commit"


@requires_git
def test_a_stored_url_of_another_account_is_refused(tmp_path):
    gh, proj, _bare, _calls, _http = _connected(tmp_path)
    key = gh._key(str(proj))
    for tampered in ("https://github.com/mallory/myrepo.git", "https://evil.example/x.git",
                     str(tmp_path / "remote.git")):
        row = dict(gh.store.get_project(key))
        row["url"] = tampered
        gh.store.set_project(key, row)
        (proj / "t.txt").write_text(tampered, encoding="utf-8")
        with pytest.raises(ghpush.GhError) as e:
            gh.push(str(proj))
        assert e.value.code == "bad_link", tampered


@requires_git
def test_a_reconnected_different_account_cannot_push_to_the_old_link(tmp_path):
    gh, proj, _bare, _calls, _http = _connected(tmp_path)
    gh.store.set_account(TOKEN, "someoneelse", 7)
    with pytest.raises(ghpush.GhError) as e:
        gh.push(str(proj))
    assert e.value.code == "bad_link"


@requires_git
def test_the_hub_allows_only_https_transport(tmp_path):
    """https only in the hub: a stored URL can only ever be https, and even a
    transport that hands git a local path is refused (here "file")."""
    http = FakeHTTP()
    gh, _c, bare = _gh(tmp_path, http=http, allowed_protocols=("https",))
    with pytest.raises(ghpush.GhError) as e:
        gh.create_repo(str(_mk_project(tmp_path)), "myrepo")
    assert e.value.code == "push_failed"
    assert _refs(bare) == "", "nothing may reach a non-https remote"


# --------------------------------------------------------------------------- #
# Sync.                                                                        #
# --------------------------------------------------------------------------- #
@requires_git
def test_sync_fast_forwards_a_clean_work_tree(tmp_path):
    gh, proj, bare, _calls, _http = _connected(tmp_path)
    _advance_remote(tmp_path, bare)
    st = gh.sync(str(proj))
    assert (proj / "fromremote.txt").read_text(encoding="utf-8") == "remote change\n"
    assert st["project"]["behind"] == 0 and st["project"]["ahead"] == 0
    assert not (proj / ".git").exists()


@requires_git
def test_sync_with_local_changes_touches_nothing(tmp_path):
    gh, proj, bare, _calls, _http = _connected(tmp_path)
    _advance_remote(tmp_path, bare)
    (proj / "README.md").write_text("edited here\n", encoding="utf-8")
    with pytest.raises(ghpush.GhError) as e:
        gh.sync(str(proj))
    assert e.value.code == "local_changes"
    assert (proj / "README.md").read_text(encoding="utf-8") == "edited here\n"
    assert not (proj / "fromremote.txt").exists()


@requires_git
def test_sync_reports_diverged(tmp_path):
    gh, proj, bare, _calls, _http = _connected(tmp_path)
    _advance_remote(tmp_path, bare)
    (proj / "l.txt").write_text("l\n", encoding="utf-8")
    with pytest.raises(ghpush.GhError):
        gh.push(str(proj))                 # behind_remote, but the mirror committed l.txt
    with pytest.raises(ghpush.GhError) as e:
        gh.sync(str(proj))
    assert e.value.code == "diverged"
    assert not (proj / "fromremote.txt").exists()


# --------------------------------------------------------------------------- #
# Askpass: answers ONLY for https://github.com.                                #
# --------------------------------------------------------------------------- #
def _cred_fill(askpass, lines):
    env = dict(_clean_env(), GIT_ASKPASS=askpass, GH_ASKPASS_TOKEN=TOKEN,
               GIT_TERMINAL_PROMPT="0")
    p = subprocess.run(["git", "-c", "credential.helper=", "credential", "fill"],
                       input="".join(l + "\n" for l in lines) + "\n",
                       capture_output=True, text=True, env=env, timeout=60)
    return p.returncode, p.stdout


def _sh():
    import shutil
    found = shutil.which("sh")
    if found:
        return found
    try:
        execp = subprocess.run(["git", "--exec-path"], capture_output=True,
                               text=True, timeout=20).stdout.strip()
    except Exception:                                            # noqa: BLE001
        return None
    for up in (2, 3):
        root = execp
        for _ in range(up):
            root = os.path.dirname(root)
        cand = os.path.join(root, "usr", "bin", "sh.exe")
        if os.path.isfile(cand):
            return cand
    return None


@requires_git
def test_askpass_answers_only_for_github_com(tmp_path):
    script = ghpush.GitHub._write_askpass(str(tmp_path))
    body = io.open(script, encoding="ascii").read()
    assert TOKEN not in body and "GH_ASKPASS_TOKEN" in body
    rc, out = _cred_fill(script, ["protocol=https", "host=github.com"])
    assert rc == 0
    assert "username=x-access-token" in out and ("password=" + TOKEN) in out
    for evil in (["protocol=https", "host=evil.example"],
                 ["protocol=https", "host=github.com.evil.example"],
                 ["protocol=https", "host=evil.example", "username=x-access-token"],
                 ["protocol=https", "host=github.com:8443"],
                 ["protocol=http", "host=github.com"]):
        rc, out = _cred_fill(script, evil)
        assert TOKEN not in out, evil
        assert rc != 0, evil


@requires_git
def test_askpass_script_executed_directly(tmp_path):
    sh = _sh()
    if not sh:
        pytest.skip("no POSIX sh to run the script directly")
    script = ghpush.GitHub._write_askpass(str(tmp_path))
    env = dict(_clean_env(), GH_ASKPASS_TOKEN=TOKEN)

    def ask(prompt):
        p = subprocess.run([sh, script, prompt], capture_output=True, text=True,
                           env=env, timeout=30)
        return p.stdout.strip()
    assert ask("Username for 'https://github.com': ") == "x-access-token"
    assert ask("Password for 'https://x-access-token@github.com': ") == TOKEN
    assert ask("Username for 'https://evil.example': ") == ""
    assert ask("Password for 'https://x-access-token@evil.example': ") == ""
    assert ask("Password for 'https://x-access-token@github.com.evil.example': ") == ""
    assert ask("Password for 'http://x-access-token@github.com': ") == ""


# --------------------------------------------------------------------------- #
# Every git call: mirror GIT_DIR, sanitized env, empty hooks, safety -c set.   #
# --------------------------------------------------------------------------- #
@requires_git
def test_every_git_call_is_sanitized_and_never_uses_the_project_git(tmp_path, monkeypatch):
    gh, proj, bare, calls, _http = _connected(tmp_path)
    (proj / "h.txt").write_text("h\n", encoding="utf-8")
    monkeypatch.setenv("GIT_TRACE", "1")
    monkeypatch.setenv("GIT_SSL_NO_VERIFY", "1")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.fsmonitor'='x'")
    monkeypatch.setenv("CURL_HOME", str(tmp_path))
    calls.clear()
    gh.push(str(proj))
    gh.sync(str(proj))
    assert calls
    mirror = gh.mirror_path(str(proj))
    hub_home = os.path.join(str(tmp_path / "hub" / "github-mirrors"), "_home")
    for rec in calls:
        a, env = rec["args"], rec["env"]
        assert "core.fsmonitor=false" in a and "credential.helper=" in a, a
        assert rec.get("hooks_empty") is True, "hooksPath must be an EMPTY private dir"
        assert not os.path.exists(rec["hooks_dir"]), "the private dir is removed after"
        assert not [k for k in env if k.upper().startswith("GIT_") and k not in (
            "GIT_CONFIG_GLOBAL", "GIT_TERMINAL_PROMPT", "GIT_ALLOW_PROTOCOL",
            "GIT_ASKPASS")], env.keys()
        assert "CURL_HOME" not in env
        assert env["HOME"] == hub_home and env["XDG_CONFIG_HOME"] == hub_home
        g = env["GIT_CONFIG_GLOBAL"]
        assert os.path.isfile(g) and os.path.getsize(g) == 0
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        gitdirs = [x.split("=", 1)[1] for x in a if x.startswith("--git-dir=")]
        assert gitdirs == [mirror], a
        assert not any(".git" + os.sep in x or x.endswith(os.sep + ".git")
                       for x in a if str(proj) in x), a
    push = [r for r in calls if "push" in r["args"]][-1]
    fetch = [r for r in calls if "fetch" in r["args"]][-1]
    for r in (push, fetch):
        assert "http.sslVerify=true" in r["args"] and "push.gpgSign=false" in r["args"]
        assert STORED not in r["args"] and str(bare) in r["args"]   # the explicit URL
        assert r.get("askpass_exists") is True
        assert TOKEN not in " ".join(r["args"]) and TOKEN not in r.get("askpass_body", "")
        assert r["env"]["GH_ASKPASS_TOKEN"] == TOKEN
    assert "--no-verify" in push["args"]
    tokenless = [r for r in calls if r not in (push, fetch)]
    assert all("GH_ASKPASS_TOKEN" not in r["env"] for r in tokenless)


@requires_git
def test_the_token_is_never_written_to_disk_by_git(tmp_path):
    gh, proj, _bare, _calls, _http = _connected(tmp_path)
    (proj / "y.txt").write_text("y\n", encoding="utf-8")
    gh.push(str(proj))
    gh.sync(str(proj))
    for root, _dirs, files in os.walk(str(tmp_path / "hub")):
        for f in files:
            with open(os.path.join(root, f), "rb") as fh:
                assert TOKEN.encode() not in fh.read(), os.path.join(root, f)


@requires_git
def test_commit_identity_is_explicit(tmp_path):
    gh, proj, _bare, calls, _http = _connected(tmp_path)
    (proj / "z.txt").write_text("z\n", encoding="utf-8")
    calls.clear()
    gh.push(str(proj))
    commit = [c["args"] for c in calls if "commit" in c["args"]]
    assert commit
    assert "user.name=octocat" in commit[0]
    assert "user.email=42+octocat@users.noreply.github.com" in commit[0]


# --------------------------------------------------------------------------- #
# Hostile configs are PROVEN ignored.                                          #
# --------------------------------------------------------------------------- #
@requires_git
def test_a_hostile_project_config_is_never_read(tmp_path):
    good = _mk_bare(tmp_path, "good.git")
    evil = _mk_bare(tmp_path, "evil.git")
    control = _mk_bare(tmp_path, "control.git")
    markers = tmp_path / "markers"
    markers.mkdir()
    proj = _mk_project(tmp_path, files={"README.md": "hi\n", ".gitattributes": "* filter=evil\n"})
    _raw_git(str(proj), "init", "-q", "-b", "main")
    _raw_git(str(proj), "add", "-A")
    _raw_git(str(proj), "commit", "-q", "-m", "orig")
    hooks = tmp_path / "evilhooks"
    _plant_hooks(str(hooks), markers)
    _plant_hooks(str(proj / ".git" / "hooks"), markers)
    run = lambda name: _marker_script(tmp_path, markers, name)          # noqa: E731
    for key, value in (
            ("remote.github.url", evil.as_posix()),
            ("remote.github.pushurl", evil.as_posix()),
            ("url.%s.insteadOf" % evil.as_posix(), good.as_posix()),
            ("url.%s.pushInsteadOf" % evil.as_posix(), good.as_posix()),
            ("http.https://github.com/.sslVerify", "false"),
            ("http.https://github.com/.proxy", "http://127.0.0.1:9"),
            ("http.sslCAInfo", str(tmp_path / "evil-ca.pem")),
            ("http.curloptResolve", "github.com:443:127.0.0.1"),
            ("credential.helper", "!" + run("cred")),
            ("credential.https://github.com.helper", "!" + run("cred-url")),
            ("filter.evil.clean", run("clean")),
            ("filter.evil.smudge", run("smudge")),
            ("filter.evil.required", "true"),
            ("commit.gpgSign", "true"),
            ("gpg.program", run("gpg")),
            ("core.fsmonitor", run("fsmonitor")),
            ("core.hooksPath", hooks.as_posix()),
            ("core.sshCommand", run("ssh"))):
        _raw_git(str(proj), "config", key, value)
    # Control: plain git in this project DOES run the planted programs.
    (proj / "c.txt").write_text("c\n", encoding="utf-8")
    _raw_git(str(proj), "add", "-A", check=False)
    _raw_git(str(proj), "push", "-q", control.as_posix(), "HEAD:main", check=False)
    if not ((markers / "clean").exists() and (markers / "hook-pre-push").exists()):
        pytest.skip("filters/hooks cannot run on this machine; nothing to prove")
    for m in markers.iterdir():
        m.unlink()
    config_before = (proj / ".git" / "config").read_bytes()
    gh, _calls, _b = _gh(tmp_path, bare=good)
    gh.create_repo(str(proj), "myrepo")
    (proj / "d.txt").write_text("d\n", encoding="utf-8")
    gh.push(str(proj))
    _advance_remote(tmp_path, good, "r.txt")
    gh.sync(str(proj))
    assert (proj / "r.txt").exists()
    assert sorted(p.name for p in markers.iterdir()) == [], \
        "a planted program ran during the hub's create/push/sync"
    assert _refs(evil) == "", "the push must land in the stored-URL repo only"
    assert _bare_show(good, "d.txt").returncode == 0
    assert (proj / ".git" / "config").read_bytes() == config_before


@requires_git
def test_a_hostile_global_config_and_git_environment_are_ignored(tmp_path, monkeypatch):
    good = _mk_bare(tmp_path, "good.git")
    evil = _mk_bare(tmp_path, "evil.git")
    markers = tmp_path / "markers"
    markers.mkdir()
    proj = _mk_project(tmp_path, files={"README.md": "hi\n", ".gitattributes": "* filter=evil\n"})
    hooks = tmp_path / "evilhooks"
    _plant_hooks(str(hooks), markers)
    template = tmp_path / "eviltemplate"
    _plant_hooks(str(template / "hooks"), markers)
    run = lambda name: _marker_script(tmp_path, markers, name)          # noqa: E731
    hostile = (
        '[url "%s"]\n\tinsteadOf = %s\n\tpushInsteadOf = %s\n'
        '[core]\n\thooksPath = %s\n\tfsmonitor = %s\n'
        '[filter "evil"]\n\tclean = %s\n\tsmudge = %s\n\trequired = true\n'
        '[commit]\n\tgpgSign = true\n[gpg]\n\tprogram = %s\n'
        '[credential]\n\thelper = !%s\n[init]\n\ttemplateDir = %s\n'
        '[http]\n\tsslVerify = false\n'
    ) % (evil.as_posix(), good.as_posix(), good.as_posix(), hooks.as_posix(),
         run("fsmonitor"), run("clean"), run("smudge"), run("gpg"), run("cred"),
         template.as_posix())
    home = tmp_path / "evilhome"
    (home / ".config" / "git").mkdir(parents=True)
    (home / ".gitconfig").write_text(hostile, encoding="utf-8")
    (home / ".config" / "git" / "config").write_text(hostile, encoding="utf-8")
    gfile = tmp_path / "hostile.gitconfig"
    gfile.write_text(hostile, encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gfile))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", hooks.as_posix())
    monkeypatch.setenv("GIT_TEMPLATE_DIR", template.as_posix())
    # Control: git in this environment DOES read the hostile config.
    seen = subprocess.run(["git", "config", "--get-regexp", "^url\\."],
                          capture_output=True, text=True, timeout=30).stdout
    assert evil.as_posix() in seen
    gh, _calls, _b = _gh(tmp_path, bare=good)
    gh.create_repo(str(proj), "myrepo")
    (proj / "d.txt").write_text("d\n", encoding="utf-8")
    gh.push(str(proj))
    gh.sync(str(proj))
    assert sorted(p.name for p in markers.iterdir()) == []
    assert _refs(evil) == ""
    assert _bare_show(good, "d.txt").returncode == 0


# --------------------------------------------------------------------------- #
# Refusals / authorization.                                                    #
# --------------------------------------------------------------------------- #
def test_refuses_hub_repo(tmp_path):
    gh, _c, _b = _gh(tmp_path, is_hub_repo=lambda p: True)
    with pytest.raises(ghpush.GhError) as e:
        gh.add_gitignore(str(_mk_project(tmp_path)), default=True)
    assert e.value.code == "refused"


def test_refuses_too_broad_folder(tmp_path):
    gh, _c, _b = _gh(tmp_path, too_broad=lambda p: True)
    with pytest.raises(ghpush.GhError) as e:
        gh.push(str(_mk_project(tmp_path)))
    assert e.value.code == "refused"


def test_refuses_a_folder_that_is_not_a_build_project(tmp_path):
    gh, _c, _b = _gh(tmp_path, is_known_project=lambda p: False)
    with pytest.raises(ghpush.GhError) as e:
        gh.create_repo(str(_mk_project(tmp_path)), "myrepo")
    assert e.value.code == "unknown_project"
    assert not gh.http.calls


def test_refuses_the_hub_state_dir_and_anything_around_it(tmp_path):
    gh, _c, _b = _gh(tmp_path)
    state = tmp_path / "hub"
    (state / "sub").mkdir(parents=True)
    for folder in (state, state / "sub", tmp_path):
        with pytest.raises(ghpush.GhError) as e:
            gh.add_gitignore(str(folder), default=True)
        assert e.value.code == "refused", folder


def test_refuses_a_linked_folder(tmp_path, monkeypatch):
    proj = _mk_project(tmp_path)
    real_islink = os.path.islink
    monkeypatch.setattr(
        ghpush.os.path, "islink",
        lambda p, _r=real_islink, t=str(proj):
        True if os.path.abspath(p) == os.path.abspath(t) else _r(p))
    gh, _c, _b = _gh(tmp_path)
    with pytest.raises(ghpush.GhError) as e:
        gh.preview(str(proj))
    assert e.value.code == "refused"


def test_status_reports_a_refusal_without_raising(tmp_path):
    gh, _c, _b = _gh(tmp_path, is_hub_repo=lambda p: True)
    st = gh.status(str(_mk_project(tmp_path)))
    assert st["project"]["refused"]
    assert st["project"]["code"] == "refused"


def test_preview_needs_a_linked_project(tmp_path):
    gh, _c, _b = _gh(tmp_path)
    with pytest.raises(ghpush.GhError) as e:
        gh.preview(str(_mk_project(tmp_path)))
    assert e.value.code == "not_connected"


def test_app_known_project_guard(tmp_path, monkeypatch):
    import app as A
    known = _mk_project(tmp_path, "known")
    other = _mk_project(tmp_path, "other")
    monkeypatch.setattr(A.agentic_chat, "list_sessions",
                        lambda: [{"project_dir": str(known)}])
    monkeypatch.setattr(A.agentic_history, "list_conversations", lambda limit=50: [])
    assert A._gh_known_project(str(known)) is True
    assert A._gh_known_project(str(other)) is False
    assert A.ghpush.default.is_known_project is A._gh_known_project


# --------------------------------------------------------------------------- #
# One-click sign-in (1): GitHub's OAuth DEVICE flow (a Client ID is set).        #
# --------------------------------------------------------------------------- #
CID = "Iv1.abcdef1234567890"
DEVICE_CODE = "3584d83530557fdd1f46af8289938c8ef79f9dc5"


def _oauth_http(**over):
    http = FakeHTTP()
    http.set("POST", ghpush._DEVICE_CODE_URL, 200, dict({
        "device_code": DEVICE_CODE, "user_code": "WDJB-MJHT",
        "verification_uri": "https://github.com/login/device",
        "expires_in": 900, "interval": 5}, **over))
    http.queue("POST", ghpush._DEVICE_TOKEN_URL, (200, {"error": "authorization_pending"}))
    return http


def _oauth_gh(tmp_path, http=None, store=None, now=None, **kw):
    now = now if now is not None else [1000.0]
    gh = ghpush.GitHub(http=http or _oauth_http(), store=store or DictStore(token=None, client_id=CID),
                       clock=lambda: now[0], spawn=lambda fn: None,
                       mirror_root=str(tmp_path / "hub" / "github-mirrors"), **kw)
    return gh, now


def test_device_flow_happy_path(tmp_path):
    http = _oauth_http()
    gh, _now = _oauth_gh(tmp_path, http=http)
    out = gh.start_device()
    assert out["user_code"] == "WDJB-MJHT"
    assert out["verification_uri"] == "https://github.com/login/device"
    assert DEVICE_CODE not in json.dumps(out)
    req = http.calls[0]
    assert req["headers"]["Accept"] == "application/json"
    assert b"client_id=" + CID.encode() in req["body"] and b"scope=repo" in req["body"]
    assert gh.device_poll_once() is True                       # authorization_pending
    st = gh.device_status()
    assert st["state"] == "pending" and st["user_code"] == "WDJB-MJHT"
    assert DEVICE_CODE not in json.dumps(st)
    http.queue("POST", ghpush._DEVICE_TOKEN_URL, (200, {"access_token": TOKEN, "scope": "repo"}))
    assert gh.device_poll_once() is False
    st = gh.device_status()
    assert st == {"state": "connected", "login": "octocat"}
    assert gh.store.get_token() == TOKEN
    token_req = [c for c in http.calls if c["path"] == ghpush._DEVICE_TOKEN_URL][-1]
    assert b"grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Adevice_code" in token_req["body"]
    assert any(c["path"] == "/user" and c["headers"]["Authorization"] == "Bearer " + TOKEN
               for c in http.calls)


def test_device_flow_slow_down_expired_denied(tmp_path):
    http = _oauth_http()
    gh, now = _oauth_gh(tmp_path, http=http)
    gh.start_device()
    http.queue("POST", ghpush._DEVICE_TOKEN_URL, (200, {"error": "slow_down"}))
    assert gh.device_poll_once() is True and gh._device["interval"] == 10
    http.queue("POST", ghpush._DEVICE_TOKEN_URL, (200, {"error": "slow_down", "interval": 30}))
    gh.device_poll_once()
    assert gh._device["interval"] == 30
    http.queue("POST", ghpush._DEVICE_TOKEN_URL, (200, {"error": "expired_token"}))
    assert gh.device_poll_once() is False and gh.device_status()["state"] == "expired"
    gh.start_device()
    http.queue("POST", ghpush._DEVICE_TOKEN_URL, (200, {"error": "access_denied"}))
    gh.device_poll_once()
    assert gh.device_status()["state"] == "denied"
    gh.start_device()
    n = len(http.calls)
    now[0] += 901                                              # our own clock says expired
    assert gh.device_poll_once() is False
    assert gh.device_status()["state"] == "expired" and len(http.calls) == n
    assert gh.store.get_token() is None


@pytest.mark.parametrize("over", [
    {"verification_uri": "https://evil.example/login/device"},
    {"verification_uri": "https://github.com/login/device/"},
    {"verification_uri": "http://github.com/login/device"},
    {"user_code": "wdjb-mjht"}, {"user_code": "WDJB-MJHT-X"}, {"device_code": ""}])
def test_device_flow_refuses_an_unexpected_answer(tmp_path, over):
    gh, _now = _oauth_gh(tmp_path, http=_oauth_http(**over))
    with pytest.raises(ghpush.GhError) as e:
        gh.start_device()
    assert e.value.code == "github_error"
    assert gh.device_status() == {"state": "idle"}


def test_client_id_is_validated_and_resolved(tmp_path, monkeypatch):
    store = DictStore(token=None)
    gh, _now = _oauth_gh(tmp_path, store=store, gh_platform=("FreeBSD", "riscv"),
                         find_gh=lambda: None)
    for bad in ("short", "has space in it!!", "x" * 41, "a/b/c/d/e/f/g"):
        with pytest.raises(ghpush.GhError) as e:
            gh.set_client_id(bad)
        assert e.value.code == "bad_client_id"
    monkeypatch.delenv(ghpush.CLIENT_ID_ENV, raising=False)
    monkeypatch.setattr(ghpush, "DEFAULT_OAUTH_CLIENT_ID", "")
    assert gh.oauth_client_id() is None and gh.one_click_available() is False
    with pytest.raises(ghpush.GhError) as e:
        gh.start_device()
    assert e.value.code == "no_client_id"
    monkeypatch.setattr(ghpush, "DEFAULT_OAUTH_CLIENT_ID", "Ov23liDEFAULT0000")
    assert gh.oauth_client_id() == "Ov23liDEFAULT0000"
    monkeypatch.setenv(ghpush.CLIENT_ID_ENV, "Ov23liFROMENV00000")
    assert gh.oauth_client_id() == "Ov23liFROMENV00000"
    assert gh.set_client_id(CID) == {"oauth_available": True}
    assert gh.oauth_client_id() == CID                          # the setting wins
    gh.set_client_id("")
    assert store.client_id is None


def test_a_second_start_cancels_the_first(tmp_path):
    http = _oauth_http()
    gh, _now = _oauth_gh(tmp_path, http=http)
    gh.start_device()
    first = gh._device
    http.set("POST", ghpush._DEVICE_CODE_URL, 200, {
        "device_code": "second-device-code", "user_code": "ABCD-2345",
        "verification_uri": "https://github.com/login/device", "expires_in": 900, "interval": 5})
    gh.start_device()
    assert first["cancel"].is_set() and first["device_code"] is None
    assert gh.device_poll_once(first) is False
    assert gh.device_status()["user_code"] == "ABCD-2345"
    assert gh.cancel_device() == {"state": "idle"} and gh.device_status() == {"state": "idle"}


def test_device_flow_real_thread_finishes(tmp_path):
    http = _oauth_http()
    http.queue("POST", ghpush._DEVICE_TOKEN_URL, (200, {"error": "authorization_pending"}),
               (200, {"error": "authorization_pending"}), (200, {"access_token": TOKEN}))
    gh = ghpush.GitHub(http=http, store=DictStore(token=None, client_id=CID),
                       wait=lambda ev, s: ev.is_set(),
                       mirror_root=str(tmp_path / "hub" / "github-mirrors"))
    gh.start_device()
    t = gh._device["thread"]
    t.join(10)
    assert not t.is_alive(), "the poller thread must end"
    assert gh.device_status()["state"] == "connected"


def test_device_token_is_stored_encrypted_via_the_paste_path(real_store, tmp_path):
    import secretstore
    real_store.set_client_id(CID)
    http = _oauth_http()
    gh = ghpush.GitHub(http=http, store=real_store, spawn=lambda fn: None,
                       mirror_root=str(tmp_path / "hub" / "github-mirrors"))
    gh.start_device()
    http.queue("POST", ghpush._DEVICE_TOKEN_URL, (200, {"access_token": TOKEN}))
    gh.device_poll_once()
    raw = (tmp_path / "state" / "config.json").read_text(encoding="utf-8")
    if secretstore.available():
        assert TOKEN not in raw and DEVICE_CODE not in raw
        assert secretstore.is_encrypted(json.loads(raw)["github"]["token"])
    assert real_store.get_token() == TOKEN


def test_device_routes_never_leak_the_device_code_or_token(tmp_path, monkeypatch, caplog):
    import logging
    c, A = _client()
    http = _oauth_http()
    gh, _now = _oauth_gh(tmp_path, http=http)
    monkeypatch.setattr(A.config, "get_flag", lambda name, default=False: True)
    monkeypatch.setattr(A.ghpush, "default", gh)
    caplog.set_level(logging.INFO)
    texts = []
    r = c.post("/api/github/device/start", json={}, headers=c._hdr)
    assert r.status_code == 200
    assert set(r.get_json()) >= {"user_code", "verification_uri", "expires_in", "interval"}
    texts.append(r.get_data(as_text=True))
    gh.device_poll_once()
    texts.append(c.get("/api/github/device", headers=c._hdr).get_data(as_text=True))
    http.queue("POST", ghpush._DEVICE_TOKEN_URL, (200, {"access_token": TOKEN}))
    gh.device_poll_once()
    r = c.get("/api/github/device", headers=c._hdr)
    assert r.get_json() == {"state": "connected", "login": "octocat"}
    texts.append(r.get_data(as_text=True))
    texts.append(c.get("/api/github", headers=c._hdr).get_data(as_text=True))
    r = c.post("/api/github/device/cancel", json={}, headers=c._hdr)
    assert r.get_json() == {"ok": True}
    for t in texts + [caplog.text]:
        assert DEVICE_CODE not in t and TOKEN not in t


def test_device_start_refused_when_flag_is_off(monkeypatch):
    c, A = _client()
    monkeypatch.setattr(A.config, "get_flag",
                        lambda name, default=False: False if name == "github_push" else default)
    for path in ("/api/github/device/start", "/api/github/client-id", "/api/github/link",
                 "/api/github/visibility", "/api/github/unlink"):
        r = c.post(path, json={"project_dir": "x", "confirm": True, "name": "n",
                               "private": True, "client_id": CID}, headers=c._hdr)
        assert r.status_code == 403 and r.get_json()["code"] == "disabled", path
    assert c.get("/api/github/repos", headers=c._hdr).status_code == 403


def test_client_id_route(monkeypatch, tmp_path):
    c, A = _client()
    gh, _now = _oauth_gh(tmp_path, store=DictStore(token=None))
    monkeypatch.setattr(A.config, "get_flag", lambda name, default=False: True)
    monkeypatch.setattr(A.ghpush, "default", gh)
    r = c.post("/api/github/client-id", json={"client_id": "nope!"}, headers=c._hdr)
    assert r.status_code == 400 and r.get_json()["code"] == "bad_client_id"
    r = c.post("/api/github/client-id", json={"client_id": CID}, headers=c._hdr)
    assert r.get_json() == {"ok": True, "oauth_available": True}
    assert c.get("/api/github", headers=c._hdr).get_json()["oauth_available"] is True


# --------------------------------------------------------------------------- #
# One-click sign-in (2): the official GitHub CLI (no OAuth app).                 #
# --------------------------------------------------------------------------- #
FAKE_GH = r'''
import json, os, sys, time
args = sys.argv[1:]
d = os.environ["FAKEGH_DIR"]
mode = os.environ.get("FAKEGH_MODE", "ok")
cfg = os.environ.get("GH_CONFIG_DIR", "")
if args[:2] == ["auth", "token"]:
    try:
        print(open(os.path.join(cfg, "hosts.yml")).read().strip())
        sys.exit(0)
    except OSError:
        sys.exit(1)
if args[:2] == ["auth", "login"]:
    seen = {k: os.environ.get(k) for k in (
        "GH_CONFIG_DIR", "GH_NO_UPDATE_NOTIFIER", "GIT_CONFIG_GLOBAL", "GH_TOKEN",
        "GITHUB_TOKEN", "GIT_DIR", "BROWSER", "GH_BROWSER", "GH_PROMPT_DISABLED")}
    seen["argv"] = args
    # gh is non-interactive when stdout is not a terminal (here: a pipe). Not
    # stdin: Python reports Windows' NUL device as a tty, gh does not.
    seen["tty"] = bool(sys.stdout and sys.stdout.isatty())
    with open(os.path.join(d, "seen.json"), "w") as f:
        json.dump(seen, f)
    url = "https://github.com/login/device/evil" if mode == "badurl" else "https://github.com/login/device"
    print("\x1b[33m!\x1b[0m First copy your one-time code: \x1b[1mWXYZ-1234\x1b[0m", flush=True)
    print("Open this URL to continue in your web browser: " + url, flush=True)
    if mode == "deny":
        time.sleep(0.2)
        print("error: access_denied: the user has denied your application access", flush=True)
        sys.exit(1)
    end = time.time() + 60
    while time.time() < end:
        if os.path.exists(os.path.join(d, "approve")):
            with open(os.path.join(cfg, "hosts.yml"), "w") as f:
                f.write(os.environ["FAKEGH_TOKEN"])
            print("Authentication complete.", flush=True)
            sys.exit(0)
        time.sleep(0.05)
    sys.exit(3)
sys.exit(2)
'''


def _gh_cli(tmp_path, monkeypatch, mode="ok", now=None, **kw):
    import sys
    script = tmp_path / "fake_gh.py"
    script.write_text(FAKE_GH, encoding="utf-8")
    d = tmp_path / "ghstate"
    d.mkdir(exist_ok=True)
    monkeypatch.setenv("FAKEGH_DIR", str(d))
    monkeypatch.setenv("FAKEGH_MODE", mode)
    monkeypatch.setenv("FAKEGH_TOKEN", TOKEN)
    monkeypatch.setenv("GH_TOKEN", "leaked-env-token")
    monkeypatch.setenv("GITHUB_TOKEN", "leaked-env-token")
    monkeypatch.delenv(ghpush.CLIENT_ID_ENV, raising=False)
    monkeypatch.setattr(ghpush, "DEFAULT_OAUTH_CLIENT_ID", "")
    kw.setdefault("find_gh", lambda: "fake-gh")
    store = DictStore(token=None)
    clock = (lambda: now[0]) if now is not None else None
    gh = ghpush.GitHub(http=FakeHTTP(), store=store, clock=clock,
                       mirror_root=str(tmp_path / "hub" / "github-mirrors"),
                       gh_argv=lambda path: [sys.executable, str(script)],
                       gh_code_wait=30, gh_poll_step=0.05, **kw)
    return gh, store, d


def _finish(gh, flow):
    t = flow.get("thread")
    if t is not None:
        t.join(30)
        assert not t.is_alive(), "the GitHub CLI worker must end"


def test_gh_login_happy_path(tmp_path, monkeypatch):
    gh, store, d = _gh_cli(tmp_path, monkeypatch)
    out = gh.start_device()
    flow = gh._device
    assert out["user_code"] == "WXYZ-1234" and out["state"] == "pending"
    assert out["verification_uri"] == "https://github.com/login/device"
    seen = json.loads((d / "seen.json").read_text(encoding="utf-8"))
    tmp = os.path.dirname(seen["GH_CONFIG_DIR"])
    assert os.path.basename(tmp).startswith("ghpush-gh-")
    assert seen["GIT_CONFIG_GLOBAL"] == os.path.join(tmp, "gitconfig")
    assert seen["GH_TOKEN"] is None and seen["GITHUB_TOKEN"] is None and seen["GIT_DIR"] is None
    assert seen["GH_NO_UPDATE_NOTIFIER"] == "1" and seen["BROWSER"] and seen["GH_BROWSER"]
    assert seen["tty"] is False
    for flag in ("--web", "--insecure-storage", "--skip-ssh-key", "--hostname"):
        assert flag in seen["argv"]
    assert seen["argv"][seen["argv"].index("--git-protocol") + 1] == "ssh"
    assert gh.device_status()["user_code"] == "WXYZ-1234"
    (d / "approve").write_text("", encoding="utf-8")
    _finish(gh, flow)
    assert gh.device_status() == {"state": "connected", "login": "octocat"}
    assert store.get_token() == TOKEN
    assert not os.path.exists(tmp), "gh's private config dir (it held the token) is deleted"


def test_gh_login_denied_and_bad_url(tmp_path, monkeypatch):
    gh, store, d = _gh_cli(tmp_path, monkeypatch, mode="deny")
    gh.start_device()
    flow = gh._device
    _finish(gh, flow)
    assert gh.device_status()["state"] == "denied"
    assert not os.path.exists(flow["tmp"])
    monkeypatch.setenv("FAKEGH_MODE", "badurl")
    gh.start_device()
    flow = gh._device
    _finish(gh, flow)
    st = gh.device_status()
    assert st["state"] == "error" and "unexpected sign-in page" in st["error"]
    assert flow["proc"].poll() is not None and not os.path.exists(flow["tmp"])
    assert store.get_token() is None


def test_gh_login_cancel_and_timeout_kill_gh(tmp_path, monkeypatch):
    now = [5000.0]
    gh, store, d = _gh_cli(tmp_path, monkeypatch, now=now)
    gh.start_device()
    flow = gh._device
    assert gh.cancel_device() == {"state": "idle"}
    _finish(gh, flow)
    assert flow["proc"].poll() is not None, "cancel kills the gh process"
    assert not os.path.exists(flow["tmp"])
    gh.start_device()
    flow = gh._device
    now[0] += 901                                              # the 15-minute limit
    _finish(gh, flow)
    assert gh.device_status()["state"] == "expired"
    assert flow["proc"].poll() is not None and not os.path.exists(flow["tmp"])
    assert store.get_token() is None


def test_gh_second_start_kills_the_first(tmp_path, monkeypatch):
    gh, _store, _d = _gh_cli(tmp_path, monkeypatch)
    gh.start_device()
    first = gh._device
    gh.start_device()
    second = gh._device
    _finish(gh, first)
    assert first["cancel"].is_set() and first["proc"].poll() is not None
    assert gh.device_status()["state"] == "pending"
    gh.cancel_device()
    _finish(gh, second)


def _fake_release(tmp_path, ver="2.60.1", plat="windows_amd64", ext=".zip", binary=b"FAKEGH",
                  member=None, digest=None, sums_hash=None, url_host="github.com"):
    import tarfile
    import zipfile
    name = "gh_%s_%s%s" % (ver, plat, ext)
    exe = "gh.exe" if plat.startswith("windows") else "gh"
    member = member or "gh_%s_%s/bin/%s" % (ver, plat, exe)
    buf = io.BytesIO()
    if ext == ".zip":
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("gh_%s_%s/LICENSE" % (ver, plat), "MIT")
            zf.writestr(member, binary)
    else:
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo(member)
            info.size = len(binary)
            tf.addfile(info, io.BytesIO(binary))
    archive = buf.getvalue()
    real = hashlib.sha256(archive).hexdigest()
    sums = "%s  %s\n%s  gh_%s_checksums.txt.sig\n" % (sums_hash or real, name, "0" * 64, ver)
    base = "https://%s/cli/cli/releases/download/v%s/" % (url_host, ver)
    meta = {"tag_name": "v" + ver, "assets": [
        {"name": name, "browser_download_url": base + name,
         **({"digest": "sha256:" + digest} if digest else {})},
        {"name": "gh_%s_checksums.txt" % ver,
         "browser_download_url": "https://github.com/cli/cli/releases/download/v%s/"
                                 "gh_%s_checksums.txt" % (ver, ver)}]}
    table = {ghpush._GH_RELEASE_API: json.dumps(meta).encode(),
             base + name: archive,
             "https://github.com/cli/cli/releases/download/v%s/gh_%s_checksums.txt" % (ver, ver):
                 sums.encode()}
    calls = []

    def fetch(url, sink, max_bytes, on_progress, hosts):
        calls.append((url, frozenset(hosts)))
        assert url in table, url
        sink(table[url])
        if on_progress:
            on_progress(len(table[url]), len(table[url]))
        return len(table[url])
    return fetch, calls


def _install_flow(gh):
    import threading
    flow = {"mode": "gh", "cancel": threading.Event()}
    gh._device = flow
    return flow


@pytest.mark.parametrize("plat,ext,system,machine", [
    ("windows_amd64", ".zip", "Windows", "AMD64"),
    ("linux_arm64", ".tar.gz", "Linux", "aarch64"),
    ("macOS_arm64", ".zip", "Darwin", "arm64")])
def test_gh_install_verifies_and_extracts_only_the_binary(tmp_path, plat, ext, system, machine):
    fetch, calls = _fake_release(tmp_path, plat=plat, ext=ext)
    gh = ghpush.GitHub(store=DictStore(), gh_fetch=fetch, gh_platform=(system, machine),
                       gh_bin_dir=str(tmp_path / "bin"), mirror_root=str(tmp_path / "hub" / "m"))
    final = gh._install_gh(_install_flow(gh))
    assert open(final, "rb").read() == b"FAKEGH"
    assert os.path.basename(final) == ("gh.exe" if plat.startswith("windows") else "gh")
    assert sorted(os.listdir(str(tmp_path / "bin"))) == [os.path.basename(final)]
    assert calls[0][1] == ghpush._GH_META_HOSTS
    assert all(h == ghpush._GH_DOWNLOAD_HOSTS for _u, h in calls[1:])


@pytest.mark.parametrize("kw", [
    {"sums_hash": "f" * 64},                                # checksum mismatch
    {"digest": "e" * 64},                                   # two different checksums
    {"member": "../../evil/bin/gh.exe"},                    # unsafe archive path
    {"member": "gh_2.60.1_windows_amd64/README.md"},        # no gh binary
    {"url_host": "evil.example"}])                          # not the official host
def test_gh_install_refusals_install_nothing(tmp_path, kw):
    fetch, _calls = _fake_release(tmp_path, **kw)
    gh = ghpush.GitHub(store=DictStore(), gh_fetch=fetch, gh_platform=("Windows", "AMD64"),
                       gh_bin_dir=str(tmp_path / "bin"), mirror_root=str(tmp_path / "hub" / "m"))
    with pytest.raises(ghpush.GhError):
        gh._install_gh(_install_flow(gh))
    assert not os.path.exists(str(tmp_path / "bin" / "gh.exe"))


def test_gh_mode_installs_then_signs_in(tmp_path, monkeypatch):
    import threading
    fetch, _calls = _fake_release(tmp_path)
    release = threading.Event()

    def slow_fetch(url, sink, max_bytes, on_progress, hosts):
        if url.endswith(".zip"):
            release.wait(30)
        return fetch(url, sink, max_bytes, on_progress, hosts)
    bin_dir = tmp_path / "bin"
    gh, store, d = _gh_cli(
        tmp_path, monkeypatch, gh_fetch=slow_fetch, gh_platform=("Windows", "AMD64"),
        gh_bin_dir=str(bin_dir),
        find_gh=lambda: str(bin_dir / "gh.exe") if (bin_dir / "gh.exe").exists() else None)
    assert gh.one_click_available() is True                  # installable here
    out = gh.start_device()
    flow = gh._device
    assert out["state"] == "installing" and out["user_code"] is None
    for _ in range(200):                      # the worker reaches the archive download
        st = gh.device_status()
        if st.get("message") == "Downloading the GitHub CLI":
            break
        threading.Event().wait(0.02)
    assert st["state"] == "installing"
    assert st["message"] == "Downloading the GitHub CLI" and 0 <= st["progress"] <= 1
    release.set()
    assert flow["code_seen"].wait(30)
    assert gh.device_status()["user_code"] == "WXYZ-1234"
    (d / "approve").write_text("", encoding="utf-8")
    _finish(gh, flow)
    assert gh.device_status()["state"] == "connected" and store.get_token() == TOKEN


def test_one_click_is_unavailable_without_client_id_or_gh(tmp_path, monkeypatch):
    c, A = _client()
    gh, _store, _d = _gh_cli(tmp_path, monkeypatch, find_gh=lambda: None,
                             gh_platform=("FreeBSD", "riscv"))
    assert gh.one_click_available() is False
    monkeypatch.setattr(A.config, "get_flag", lambda name, default=False: True)
    monkeypatch.setattr(A.ghpush, "default", gh)
    r = c.post("/api/github/device/start", json={}, headers=c._hdr)
    assert r.status_code == 409 and r.get_json()["code"] == "no_client_id"
    assert c.get("/api/github", headers=c._hdr).get_json()["oauth_available"] is False


# --------------------------------------------------------------------------- #
# Existing repositories, visibility, unlink.                                   #
# --------------------------------------------------------------------------- #
EXISTING = "https://github.com/octocat/existing.git"


def _repo_row(name="existing", owner="octocat", push=True, branch="main", private=True):
    return {"name": name, "owner": {"login": owner}, "permissions": {"push": push},
            "default_branch": branch, "private": private}


def _linkable(tmp_path, branch="main", nonempty=False, **row):
    """A GitHub fake with one existing repo backed by a local bare repo."""
    existing = tmp_path / "existing.git"
    _raw_git(str(tmp_path), "init", "--bare", "-q", "-b", branch, str(existing))
    if nonempty:
        clone = tmp_path / "theirs"
        _raw_git(str(tmp_path), "clone", "-q", str(existing), str(clone))
        (clone / "theirs.txt").write_text("their file\n", encoding="utf-8")
        _raw_git(str(clone), "add", "-A")
        _raw_git(str(clone), "commit", "-q", "-m", "their commit")
        _raw_git(str(clone), "push", "-q", "origin", "HEAD:" + branch)
    http = FakeHTTP()
    http.set("GET", "/repos/octocat/existing", 200, _repo_row(branch=branch, **row))
    gh, calls, bare = _gh(tmp_path, http=http, routes={EXISTING: str(existing)})
    return gh, existing, http, calls


def test_list_repos_keeps_only_the_owners_repos_newest_first(tmp_path):
    http = FakeHTTP()
    http.set("GET", "/user/repos?affiliation=owner&sort=updated&per_page=100", 200, [
        {"name": "old", "owner": {"login": "octocat"}, "private": False, "size": 10,
         "updated_at": "2024-01-01T00:00:00Z", "description": "line1\nline2"},
        {"name": "new", "owner": {"login": "OctoCat"}, "private": True, "size": 0,
         "updated_at": "2026-01-01T00:00:00Z", "html_url": "https://evil.example/x"},
        {"name": "theirs", "owner": {"login": "mallory"}, "private": False, "size": 5},
        {"name": "../bad", "owner": {"login": "octocat"}}, "junk"])
    gh, _c, _b = _gh(tmp_path, http=http)
    repos = gh.list_repos()["repos"]
    assert [r["name"] for r in repos] == ["new", "old"]
    assert repos[0] == {"name": "new", "private": True, "empty": True,
                        "updated_at": "2026-01-01T00:00:00Z",
                        "html_url": "https://github.com/octocat/new", "description": ""}
    assert repos[1]["description"] == "line1 line2" and repos[1]["empty"] is False


@requires_git
def test_link_an_empty_existing_repo(tmp_path):
    gh, existing, http, _calls = _linkable(tmp_path)
    proj = _mk_project(tmp_path)
    st = gh.link_repo(str(proj), "existing")
    assert st["project"]["repo"]["full_name"] == "octocat/existing"
    assert gh.store.get_project(gh._key(str(proj)))["url"] == EXISTING
    assert _bare_show(existing, "README.md").stdout == "hello\n"
    assert not [c for c in http.calls if c["method"] == "POST"], "linking creates nothing"


@pytest.mark.parametrize("row,code", [
    ({"owner": "mallory"}, "repo_not_owned"), ({"push": False}, "no_push_permission")])
def test_link_refuses_other_owners_and_no_push(tmp_path, row, code):
    gh, existing, _http, _c = _linkable(tmp_path, **row)
    proj = _mk_project(tmp_path)
    with pytest.raises(ghpush.GhError) as e:
        gh.link_repo(str(proj), "existing")
    assert e.value.code == code
    assert gh.store.get_project(gh._key(str(proj))) is None and _refs(existing) == ""


def test_link_unknown_repo_is_not_found(tmp_path):
    gh, _c, _b = _gh(tmp_path)
    with pytest.raises(ghpush.GhError) as e:
        gh.link_repo(str(_mk_project(tmp_path)), "nope")
    assert e.value.code == "not_found"


@requires_git
@pytest.mark.parametrize("branch", ["main", "master"])
def test_link_a_non_empty_repo_commits_on_top(tmp_path, branch):
    gh, existing, _http, calls = _linkable(tmp_path, branch=branch, nonempty=True)
    proj = _mk_project(tmp_path)
    theirs = _raw_git(str(existing), "rev-parse", branch).stdout.strip()
    with pytest.raises(ghpush.GhError) as e:
        gh.link_repo(str(proj), "existing")
    assert e.value.code == "needs_overwrite_ok"
    assert gh.store.get_project(gh._key(str(proj))) is None
    assert _raw_git(str(existing), "rev-parse", branch).stdout.strip() == theirs
    gh.link_repo(str(proj), "existing", overwrite_ok=True)
    head = _raw_git(str(existing), "rev-parse", branch).stdout.strip()
    assert head != theirs
    assert _raw_git(str(existing), "merge-base", "--is-ancestor", theirs, head,
                    check=False).returncode == 0, "their history is kept (never a force)"
    assert _raw_git(str(existing), "show", "%s:README.md" % branch).stdout == "hello\n"
    assert _raw_git(str(existing), "show", "%s:theirs.txt" % branch, check=False).returncode != 0
    pushes = [r["args"] for r in calls if "push" in r["args"]]
    assert all("refs/heads/main:refs/heads/%s" % branch in a for a in pushes)
    assert not any(x.startswith("+") or x in ("-f", "--force") for a in pushes for x in a)
    assert gh.status(str(proj))["project"]["repo"]["default_branch"] == branch


@requires_git
def test_link_with_secrets_puts_the_mirror_back(tmp_path):
    gh, existing, _http, _c = _linkable(tmp_path, nonempty=True)
    proj = _mk_project(tmp_path, files={"README.md": "hi\n", ".env": "K=v\n"})
    with pytest.raises(ghpush.GhError) as e:
        gh.link_repo(str(proj), "existing", overwrite_ok=True)
    assert e.value.code == "secrets_found"
    m = gh.mirror_path(str(proj))
    assert _raw_git(m, "rev-parse", "--verify", "--quiet", "refs/heads/main",
                    check=False).returncode != 0, "the mirror's main is restored (none before)"
    assert gh.store.get_project(gh._key(str(proj))) is None


@requires_git
def test_link_refuses_an_already_linked_project(tmp_path):
    gh, proj, _bare, _calls, http = _connected(tmp_path)
    http.set("GET", "/repos/octocat/existing", 200, _repo_row())
    with pytest.raises(ghpush.GhError) as e:
        gh.link_repo(str(proj), "existing")
    assert e.value.code == "already_linked"


@requires_git
def test_visibility_change_needs_the_public_tick(tmp_path):
    gh, proj, _bare, _calls, http = _connected(tmp_path)
    with pytest.raises(ghpush.GhError) as e:
        gh.set_visibility(str(proj), False)
    assert e.value.code == "public_ok_required"
    assert not [c for c in http.calls if c["method"] == "PATCH"]
    http.set("PATCH", "/repos/octocat/myrepo", 200, {"private": False})
    assert gh.set_visibility(str(proj), False, public_ok=True) == {"ok": True, "private": False}
    patch = [c for c in http.calls if c["method"] == "PATCH"][-1]
    assert json.loads(patch["body"].decode()) == {"private": False}
    assert gh.status(str(proj))["project"]["repo"]["private"] is False
    http.set("PATCH", "/repos/octocat/myrepo", 200, {"private": True})
    assert gh.set_visibility(str(proj), True) == {"ok": True, "private": True}
    http.set("PATCH", "/repos/octocat/myrepo", 403, {"message": "no"})
    with pytest.raises(ghpush.GhError) as e:
        gh.set_visibility(str(proj), True)
    assert e.value.code == "bad_scope"


@requires_git
def test_unlink_forgets_the_link_and_the_mirror_then_relinks(tmp_path):
    gh, proj, bare, _calls, http = _connected(tmp_path)
    m = gh.mirror_path(str(proj))
    refs_before = _refs(bare)
    files_before = sorted(os.listdir(str(proj)))
    assert gh.unlink(str(proj)) == {"ok": True}
    assert gh.store.get_project(gh._key(str(proj))) is None
    assert not os.path.exists(m), "the hub's mirror is deleted"
    assert _refs(bare) == refs_before, "GitHub is never touched"
    assert sorted(os.listdir(str(proj))) == files_before, "the project is never touched"
    http.set("GET", "/repos/octocat/myrepo", 200, _repo_row(name="myrepo"))
    with pytest.raises(ghpush.GhError) as e:
        gh.link_repo(str(proj), "myrepo")
    assert e.value.code == "needs_overwrite_ok"
    gh.link_repo(str(proj), "myrepo", overwrite_ok=True)
    assert gh.store.get_project(gh._key(str(proj)))["url"] == STORED


def test_link_visibility_unlink_routes_check_their_input(monkeypatch):
    c, A = _client()
    monkeypatch.setattr(A.config, "get_flag", lambda name, default=False: True)
    r = c.post("/api/github/link", json={"project_dir": "x", "name": "n"}, headers=c._hdr)
    assert r.status_code == 400 and r.get_json()["code"] == "confirm_required"
    r = c.post("/api/github/unlink", json={"project_dir": "x"}, headers=c._hdr)
    assert r.status_code == 400 and r.get_json()["code"] == "confirm_required"
    r = c.post("/api/github/visibility", json={"project_dir": "x", "private": "yes"},
               headers=c._hdr)
    assert r.status_code == 400 and r.get_json()["code"] == "bad_request"


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
    assert new["github"]["token"] == "enc.v1:GITHUBTOKEN"
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


def test_route_mutations_need_the_control_token():
    c, _A = _client()
    for path in ("/api/github/push", "/api/github/create", "/api/github/sync",
                 "/api/github/token", "/api/github/gitignore"):
        r = c.post(path, json={"project_dir": "x", "confirm": True},
                   headers={"X-Free-LLM-Hub": "dashboard"})
        assert r.status_code in (401, 403), path


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
    assert fake.last == "ghp_secret_value_123"


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


def test_the_findings_box_is_outside_the_linked_section():
    """Create & push is refused before anything is made on GitHub, so its
    findings must be visible in the create state too."""
    linked = _between('<div id="github-linked"', "<!-- Secret-scan findings")
    assert 'id="github-findings"' not in linked


def test_the_token_field_is_a_password_input():
    dlg = _between('<dialog class="publish-dialog github-dialog"', "</dialog>")
    assert re.search(r'<input type="password" id="github-token"', dlg)


def test_the_token_help_link_is_safe_and_correct():
    dlg = _between('<dialog class="publish-dialog github-dialog"', "</dialog>")
    assert "https://github.com/settings/personal-access-tokens/new" in dlg
    assert 'target="_blank" rel="noopener noreferrer"' in dlg


def test_the_github_css_block_uses_theme_tokens_only():
    css = re.sub(r"/\*.*?\*/", "", _between("/* github-css:start */", "/* github-css:end */"),
                 flags=re.S)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css), "no raw hex colours in the github CSS"
    assert not re.search(r"\brgba?\(", css)
    assert not re.search(r"\bhsla?\(", css)
    assert "var(--" in css


def test_github_js_never_injects_server_strings_as_html():
    js = _between("/* github-js:start */", "/* github-js:end */")
    for m in re.finditer(r"\.innerHTML\s*=\s*([^;]+);", js):
        assert m.group(1).strip() in ("''", '""'), "innerHTML must only ever be cleared"
    assert "textContent" in js
    assert "ghSafeUrl" in js
    for code in ("unknown_project", "already_linked", "bad_link", "local_changes"):
        assert code in js, code
