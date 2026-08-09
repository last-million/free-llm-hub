"""Zip-install auto-update: the same 5-hour self-heal a git clone gets, for
someone who used GitHub's "Download ZIP" instead of `git clone`.

git's _do_update_check() skips a dirty tree so local edits are never
clobbered -- a zip install has no git to ask, so _do_zip_update_check()
builds the same guarantee from a hash manifest of the files ITS OWN last
update wrote: before applying a new update, every one of those files must
still hash the same, or the whole cycle is skipped (never a partial,
corrupting overwrite).
"""
import hashlib
import io
import json
import os
import shutil
import tempfile
import zipfile

import pytest

import app


def _zip_bytes(files, top="free-llm-hub-main"):
    """Build an in-memory zip matching GitHub's archive layout: everything
    nested one folder deep under '<repo>-<branch>/'."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for rel, content in files.items():
            zf.writestr("%s/%s" % (top, rel), content)
    return buf.getvalue()


@pytest.fixture
def repo_dir(monkeypatch):
    d = tempfile.mkdtemp(prefix="hub-pytest-zipupdate-")
    monkeypatch.setattr(app, "_REPO_DIR", d)
    monkeypatch.setattr(app, "_ZIP_MANIFEST_PATH",
                        os.path.join(d, ".free-llm-hub-update-manifest.json"))
    monkeypatch.setattr(app, "_sync_deps_after_pull", lambda: True)
    monkeypatch.setattr(app, "_hub_mode_is_off", lambda: False)
    monkeypatch.setattr(app, "_agentic_busy_session_ids", lambda: set())
    with app._runtime_condition:
        app._runtime_active[0] = 0
    reexec_calls = []
    monkeypatch.setattr(app, "_reexec_soon", lambda: reexec_calls.append(1))
    app._auto_update_state["last_result"] = "not run yet"
    app._auto_update_state["updating"] = False
    try:
        yield d, reexec_calls
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _write(d, rel, content):
    full = os.path.join(d, *rel.split("/"))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)


def _mock_zip_response(monkeypatch, files):
    zbytes = _zip_bytes(files)

    class _Resp:
        content = zbytes
        def raise_for_status(self):
            pass

    monkeypatch.setattr(app.requests, "get", lambda *a, **k: _Resp())


# --------------------------------------------------------------------------- #
# _zip_manifest_of / _zip_apply_needed / _zip_tree_is_dirty
# --------------------------------------------------------------------------- #

def test_manifest_hashes_real_files(repo_dir):
    d, _ = repo_dir
    _write(d, "app.py", "print(1)")
    _write(d, "sub/mod.py", "x = 1")
    m = app._zip_manifest_of(d)
    assert set(m) == {"app.py", "sub/mod.py"}
    assert m["app.py"] == hashlib.sha256(b"print(1)").hexdigest()


def test_manifest_skips_runtime_and_ignore_dirs(repo_dir):
    d, _ = repo_dir
    _write(d, "app.py", "x")
    _write(d, ".venv/lib/foo.py", "y")
    _write(d, "__pycache__/app.cpython-312.pyc", "z")
    _write(d, ".free-llm-hub-update-manifest.json", "{}")
    m = app._zip_manifest_of(d)
    assert set(m) == {"app.py"}


def test_apply_needed_false_when_disk_already_matches(repo_dir):
    d, _ = repo_dir
    _write(d, "app.py", "same")
    new_manifest = {"app.py": hashlib.sha256(b"same").hexdigest()}
    assert app._zip_apply_needed(new_manifest) is False


def test_apply_needed_true_when_content_differs(repo_dir):
    d, _ = repo_dir
    _write(d, "app.py", "old")
    new_manifest = {"app.py": hashlib.sha256(b"new").hexdigest()}
    assert app._zip_apply_needed(new_manifest) is True


def test_apply_needed_true_when_file_missing(repo_dir):
    new_manifest = {"app.py": hashlib.sha256(b"new").hexdigest()}
    assert app._zip_apply_needed(new_manifest) is True


def test_tree_not_dirty_when_disk_matches_old_manifest(repo_dir):
    d, _ = repo_dir
    _write(d, "app.py", "v1")
    old_manifest = {"app.py": hashlib.sha256(b"v1").hexdigest()}
    assert app._zip_tree_is_dirty(old_manifest) is False


def test_tree_dirty_when_a_tracked_file_was_hand_edited(repo_dir):
    d, _ = repo_dir
    _write(d, "app.py", "hand-edited")
    old_manifest = {"app.py": hashlib.sha256(b"v1").hexdigest()}
    assert app._zip_tree_is_dirty(old_manifest) is True


def test_tree_dirty_when_a_tracked_file_was_deleted(repo_dir):
    old_manifest = {"app.py": hashlib.sha256(b"v1").hexdigest()}
    assert app._zip_tree_is_dirty(old_manifest) is True


# --------------------------------------------------------------------------- #
# _do_zip_update_check -- full cycle
# --------------------------------------------------------------------------- #

def test_first_run_identical_to_upstream_records_baseline_no_restart(repo_dir, monkeypatch):
    """A fresh zip install checking for the first time, already on the latest
    code (just downloaded it) -- must NOT force a pointless restart."""
    d, reexec_calls = repo_dir
    _write(d, "app.py", "print(1)")
    _mock_zip_response(monkeypatch, {"app.py": "print(1)"})
    result = app._do_zip_update_check()
    assert "up to date" in result
    assert reexec_calls == []
    assert os.path.isfile(app._ZIP_MANIFEST_PATH), "baseline must be recorded for next cycle"


def test_first_run_behind_upstream_applies_and_restarts(repo_dir, monkeypatch):
    d, reexec_calls = repo_dir
    _write(d, "app.py", "print('old')")
    _mock_zip_response(monkeypatch, {"app.py": "print('new')"})
    result = app._do_zip_update_check()
    assert "restarting" in result
    assert reexec_calls == [1]
    with open(os.path.join(d, "app.py"), encoding="utf-8") as f:
        assert f.read() == "print('new')"


def test_second_run_no_upstream_change_is_a_noop(repo_dir, monkeypatch):
    d, reexec_calls = repo_dir
    _write(d, "app.py", "print(1)")
    _mock_zip_response(monkeypatch, {"app.py": "print(1)"})
    app._do_zip_update_check()  # builds the baseline
    result = app._do_zip_update_check()  # second cycle, nothing changed upstream
    assert "up to date" in result
    assert reexec_calls == []


def test_hand_edited_file_blocks_the_update_like_a_dirty_git_tree(repo_dir, monkeypatch):
    d, reexec_calls = repo_dir
    _write(d, "app.py", "print(1)")
    _mock_zip_response(monkeypatch, {"app.py": "print(1)"})
    app._do_zip_update_check()  # builds baseline at print(1)
    _write(d, "app.py", "print('user edit')")  # user hand-edits after the baseline
    _mock_zip_response(monkeypatch, {"app.py": "print('newer upstream')"})
    result = app._do_zip_update_check()
    assert "skipped" in result
    assert reexec_calls == []
    with open(os.path.join(d, "app.py"), encoding="utf-8") as f:
        assert f.read() == "print('user edit')", "a dirty tree must never be overwritten"


def test_a_new_file_upstream_is_added_without_touching_others(repo_dir, monkeypatch):
    d, reexec_calls = repo_dir
    _write(d, "app.py", "print(1)")
    _mock_zip_response(monkeypatch, {"app.py": "print(1)"})
    app._do_zip_update_check()
    _mock_zip_response(monkeypatch, {"app.py": "print(1)", "new_module.py": "print(2)"})
    result = app._do_zip_update_check()
    assert "restarting" in result
    assert os.path.isfile(os.path.join(d, "new_module.py"))


def test_a_removed_upstream_file_is_left_in_place_not_deleted(repo_dir, monkeypatch):
    """Additive/overwrite only -- deleting a file the update no longer ships
    is strictly more destructive than leaving a stale one, and git's own
    ff-only pull gives no equivalent guarantee either way."""
    d, reexec_calls = repo_dir
    _write(d, "app.py", "print(1)")
    _write(d, "old_module.py", "print('will not be deleted')")
    old_manifest = app._zip_manifest_of(d)
    app._save_zip_manifest(old_manifest)
    _mock_zip_response(monkeypatch, {"app.py": "print(2)"})  # old_module.py no longer shipped
    result = app._do_zip_update_check()
    assert "restarting" in result
    assert os.path.isfile(os.path.join(d, "old_module.py"))


def test_network_failure_is_reported_and_never_raises(repo_dir, monkeypatch):
    d, reexec_calls = repo_dir
    def _boom(*a, **k):
        raise app.requests.exceptions.ConnectionError("no network")
    monkeypatch.setattr(app.requests, "get", _boom)
    result = app._do_zip_update_check()
    assert "failed" in result
    assert reexec_calls == []


def test_gitignored_local_files_never_block_or_force_an_update(repo_dir, monkeypatch):
    """A live install also holds config.json, .hublog.*, etc. -- files never
    shipped in the zip. Their mere presence must not make an identical
    tracked-file set look 'different' and force a pointless restart."""
    d, reexec_calls = repo_dir
    _write(d, "app.py", "print(1)")
    _write(d, "config.json", '{"local": "secret"}')  # gitignored, zip-install-local
    _mock_zip_response(monkeypatch, {"app.py": "print(1)"})
    result = app._do_zip_update_check()
    assert "up to date" in result
    assert reexec_calls == []


# --------------------------------------------------------------------------- #
# _do_update_check dispatch
# --------------------------------------------------------------------------- #

def test_dispatch_uses_zip_path_when_not_a_git_repo(repo_dir, monkeypatch):
    d, reexec_calls = repo_dir
    monkeypatch.setattr(app, "_is_git_repo", lambda: False)
    calls = []
    monkeypatch.setattr(app, "_do_zip_update_check", lambda: calls.append(1) or "ok")
    monkeypatch.setattr(app, "_do_git_update_check", lambda: calls.append("WRONG"))
    app._do_update_check()
    assert calls == [1]


def test_dispatch_uses_git_path_when_it_is_a_git_repo(repo_dir, monkeypatch):
    d, reexec_calls = repo_dir
    monkeypatch.setattr(app, "_is_git_repo", lambda: True)
    calls = []
    monkeypatch.setattr(app, "_do_git_update_check", lambda: calls.append(1) or "ok")
    monkeypatch.setattr(app, "_do_zip_update_check", lambda: calls.append("WRONG"))
    app._do_update_check()
    assert calls == [1]
