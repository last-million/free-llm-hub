""""Create new project" with a bare typed name silently wrote INSIDE the
hub's own source tree -- caught live, by accident, while frontend-verifying
an unrelated fix: typed "frontend-verify-opus-fix" into the project-folder
box, and the session log showed the project at
...\\free-llm-hub\\frontend-verify-opus-fix instead of anywhere near
~/calvoun-projects.

Root cause: start_session()'s create_new path did
    abs_dir = os.path.abspath(os.path.expanduser(project_dir))
os.path.abspath resolves a relative string against the CALLING PROCESS's cwd
-- the hub SERVER's cwd, which is this repo's root when launched the normal
way (`python app.py` from the repo directory). The one-click "Create new
project" button never hits this path (it calls new_project_dir(), which
already anchors under ~/calvoun-projects and hands back an absolute path) --
only a user who types their OWN project name instead of accepting the
suggestion does. That is the ordinary, expected way to use the field.

Same bug CLASS as the opencode PWD incident (see test_pwd_env_matches_cwd.py):
server-process state (there, PWD; here, cwd) leaking into a user-chosen path
instead of being isolated from it.

Fix: a create_new project_dir that is not already absolute is anchored under
~/calvoun-projects (the same root new_project_dir() uses) before resolution,
so a bare name can never land outside that root or inside the hub's own repo.

NB: this file uses tempfile.mkdtemp() instead of pytest's tmp_path -- this
machine's default pytest basetemp is permission-denied (a known, pre-existing,
unrelated environmental issue -- see test_orchestration.py's state_dir
fixture for the same workaround).
"""
import os
import shutil
import tempfile

import pytest

import agentic_chat
import config


@pytest.fixture
def agent_state(monkeypatch):
    d = tempfile.mkdtemp(prefix="hub-pytest-newproj-")
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", os.path.join(d, "state", "config.json"))
    monkeypatch.setattr(agentic_chat.subprocess, "run",
                        lambda *a, **kw: type("R", (), {"stdout": "2.1.212 (Claude Code)",
                                                          "stderr": "", "returncode": 0})())
    monkeypatch.setattr(agentic_chat.os, "killpg", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(agentic_chat.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda name: "/usr/bin/" + name)
    agentic_chat._REGISTRY.clear()
    agentic_chat._recent_projects.clear()
    config.set_flag("agentic_chat_enabled", True)
    try:
        yield d
    finally:
        agentic_chat._REGISTRY.clear()
        agentic_chat._recent_projects.clear()
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def fake_home(agent_state, monkeypatch):
    home = os.path.join(agent_state, "fake-home")
    os.makedirs(home, exist_ok=True)
    monkeypatch.setenv("USERPROFILE", home)  # what Windows expanduser("~") reads
    monkeypatch.setenv("HOME", home)          # POSIX equivalent, harmless here
    return home


def test_bare_name_anchors_under_calvoun_projects_not_server_cwd(fake_home):
    sid = agentic_chat.start_session("claude", "my-new-app", create_new=True)
    got = agentic_chat.get_session(sid)["project_dir"]
    expected = os.path.join(fake_home, "calvoun-projects", "my-new-app")
    assert got == expected
    assert os.path.isdir(expected)


def test_bare_name_never_lands_under_the_real_process_cwd(fake_home):
    """The actual bug, asserted directly: the resolved dir must never sit
    under wherever THIS TEST PROCESS happens to be running from."""
    sid = agentic_chat.start_session("claude", "another-app", create_new=True)
    got = agentic_chat.get_session(sid)["project_dir"]
    assert not got.startswith(os.getcwd())


def test_absolute_path_bypasses_anchoring_entirely(agent_state):
    """Regression: Browse-for-folder and the auto-suggested ~/calvoun-projects
    path (from new_project_dir()) are already absolute -- must pass through
    byte-for-byte unchanged, same as before this fix."""
    new_dir = os.path.join(agent_state, "brand-new-project")
    sid = agentic_chat.start_session("claude", new_dir, create_new=True)
    assert agentic_chat.get_session(sid)["project_dir"] == os.path.abspath(new_dir)


def test_existing_folder_mode_is_not_anchored(fake_home):
    """create_new=False ("Use existing folder") is untouched by the anchoring
    fix -- it already requires the directory to exist first, which self-
    limits the blast radius; only the WRITING (create_new) path needed a
    guard against the server's own cwd."""
    with pytest.raises(agentic_chat.AgenticError) as exc:
        agentic_chat.start_session("claude", "bare-relative-name", create_new=False)
    assert "does not exist" in str(exc.value)


def test_new_project_dir_helper_and_the_anchoring_fix_agree_on_the_root():
    """The pre-existing one-click helper and the new typed-name fallback must
    both land under the exact same root, or "the folder I typed" and "the
    folder the button suggested" would silently diverge."""
    import inspect
    assert '"calvoun-projects"' in inspect.getsource(agentic_chat.new_project_dir)
    assert '"calvoun-projects"' in inspect.getsource(agentic_chat.start_session)
