"""Per-turn file snapshots: restoring a checkpoint restores the CODE too.

ASKED 2026-08-31: "in /agent that he be able to have checkpoints buttons for
previous message to restaure conversation and code and rerun it".

agentic_history's checkpoints were transcript bookmarks, and its own docstring
said so in capitals -- they "do NOT snapshot the project folder's files on disk
in any way". Restoring only the conversation is worse than useless: the
transcript would then claim a state the files no longer have.

This is the file half. A shadow git repo per session, kept in the hub's state
directory and never inside the user's project, so the project keeps no .git,
no config and no ignore file of its own.

Storage isolation comes from the root conftest (FREE_LLM_HUB_CONFIG points at a
tmp state dir), which is also where the shadow repos land.
"""
import os
import tempfile

import pytest

import snapshots

pytestmark = pytest.mark.skipif(not snapshots.git_available(),
                                reason="git is not installed")


def _project(**files):
    d = tempfile.mkdtemp(prefix="snaptest-")
    for name, body in files.items():
        path = os.path.join(d, name.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
    return d


def _read(d, name):
    with open(os.path.join(d, name.replace("/", os.sep)), encoding="utf-8") as f:
        return f.read()


def _sid(name):
    import uuid
    return "snap-%s-%s" % (name, uuid.uuid4().hex[:8])


# --------------------------------------------------------------------------- #
# The three things a restore has to get right
# --------------------------------------------------------------------------- #

def test_an_edited_file_goes_back():
    sid, d = _sid("edit"), _project(**{"index.html": "v1"})
    commit = snapshots.take(sid, d, "before turn 1")
    assert commit
    with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
        f.write("v2 -- the agent's work")
    assert snapshots.restore(sid, d, commit) is True
    assert _read(d, "index.html") == "v1"


def test_a_deleted_file_comes_back():
    sid, d = _sid("del"), _project(**{"a.txt": "keep me", "b.txt": "b"})
    commit = snapshots.take(sid, d)
    os.remove(os.path.join(d, "a.txt"))
    assert snapshots.restore(sid, d, commit) is True
    assert _read(d, "a.txt") == "keep me"


def test_a_file_created_afterwards_is_removed():
    """Otherwise a restore leaves debris the transcript knows nothing about."""
    sid, d = _sid("new"), _project(**{"a.txt": "a"})
    commit = snapshots.take(sid, d)
    with open(os.path.join(d, "junk.js"), "w", encoding="utf-8") as f:
        f.write("x")
    assert snapshots.restore(sid, d, commit) is True
    assert not os.path.exists(os.path.join(d, "junk.js"))


def test_nested_directories_are_handled():
    sid, d = _sid("nest"), _project(**{"src/pages/home.html": "home",
                                       "src/css/main.css": "body{}"})
    commit = snapshots.take(sid, d)
    with open(os.path.join(d, "src", "pages", "home.html"), "w", encoding="utf-8") as f:
        f.write("wrecked")
    os.remove(os.path.join(d, "src", "css", "main.css"))
    assert snapshots.restore(sid, d, commit) is True
    assert _read(d, "src/pages/home.html") == "home"
    assert _read(d, "src/css/main.css") == "body{}"


# --------------------------------------------------------------------------- #
# Several turns
# --------------------------------------------------------------------------- #

def test_you_can_go_back_several_turns():
    sid, d = _sid("multi"), _project(**{"f.txt": "turn0"})
    c0 = snapshots.take(sid, d, "before turn 1")
    with open(os.path.join(d, "f.txt"), "w", encoding="utf-8") as f:
        f.write("turn1")
    c1 = snapshots.take(sid, d, "before turn 2")
    with open(os.path.join(d, "f.txt"), "w", encoding="utf-8") as f:
        f.write("turn2")
    assert c0 != c1
    assert snapshots.restore(sid, d, c1) is True
    assert _read(d, "f.txt") == "turn1"
    assert snapshots.restore(sid, d, c0) is True
    assert _read(d, "f.txt") == "turn0"


def test_snapshots_keep_working_after_a_restore():
    """A restore must not leave the shadow repo in a state where the next turn
    cannot be snapshotted -- that would silently end checkpointing."""
    sid, d = _sid("after"), _project(**{"f.txt": "a"})
    c0 = snapshots.take(sid, d)
    with open(os.path.join(d, "f.txt"), "w", encoding="utf-8") as f:
        f.write("b")
    snapshots.restore(sid, d, c0)
    c2 = snapshots.take(sid, d)
    assert c2 and c2 != c0


def test_a_turn_that_changed_nothing_still_gets_a_point():
    """Snapshot ids line up with turns; skipping the empty ones would break
    that alignment."""
    sid, d = _sid("empty"), _project(**{"f.txt": "a"})
    c0 = snapshots.take(sid, d)
    c1 = snapshots.take(sid, d)
    assert c0 and c1 and c0 != c1


# --------------------------------------------------------------------------- #
# The user's project is left alone
# --------------------------------------------------------------------------- #

def test_the_project_never_gets_a_git_directory():
    """The reason for a separate --git-dir: their repo, their history, untouched."""
    sid, d = _sid("nogit"), _project(**{"f.txt": "a"})
    snapshots.take(sid, d)
    assert not os.path.exists(os.path.join(d, ".git"))
    assert not os.path.exists(os.path.join(d, ".gitignore"))


def test_a_nested_repo_survives_a_restore():
    """`git clean -fd` (never -ff) refuses to delete a nested git repository, so
    a checkout the user made inside their project is not destroyed by a
    restore."""
    sid, d = _sid("nested"), _project(**{"f.txt": "a"})
    commit = snapshots.take(sid, d)
    inner = os.path.join(d, "libs", "theirs", ".git")
    os.makedirs(inner)
    with open(os.path.join(inner, "HEAD"), "w", encoding="utf-8") as f:
        f.write("ref: refs/heads/main\n")
    assert snapshots.restore(sid, d, commit) is True
    assert os.path.isdir(inner), "a nested repo must survive"


def test_build_output_is_not_snapshotted():
    """node_modules is not source, it is the fastest way to make this unusably
    slow, and it is reproducible from the manifest that IS snapshotted."""
    sid, d = _sid("heavy"), _project(**{"package.json": "{}",
                                        "node_modules/left-pad/index.js": "x"})
    commit = snapshots.take(sid, d)
    os.remove(os.path.join(d, "package.json"))
    snapshots.restore(sid, d, commit)
    assert _read(d, "package.json") == "{}"          # source came back
    # ...and the dependency tree was never in the snapshot to begin with
    assert os.path.exists(os.path.join(d, "node_modules", "left-pad", "index.js"))


# --------------------------------------------------------------------------- #
# Fails open, never takes a turn down
# --------------------------------------------------------------------------- #

def test_a_missing_project_is_safe():
    assert snapshots.take(_sid("gone"), "/nope/not/here") is None
    assert snapshots.restore(_sid("gone"), "/nope/not/here", "deadbeef") is False


def test_missing_arguments_are_safe():
    assert snapshots.take("", "") is None
    assert snapshots.take(None, None) is None
    assert snapshots.restore(None, None, None) is False


def test_an_unknown_commit_changes_nothing():
    sid, d = _sid("bad"), _project(**{"f.txt": "original"})
    snapshots.take(sid, d)
    assert snapshots.restore(sid, d, "0" * 40) is False
    assert _read(d, "f.txt") == "original"


def test_restoring_a_session_with_no_snapshots_is_refused():
    d = _project(**{"f.txt": "a"})
    assert snapshots.restore(_sid("never"), d, "0" * 40) is False


def test_a_hostile_session_id_cannot_escape_the_snapshot_directory():
    """The id reaches here from a URL path segment."""
    for bad in ("../../etc", "..\\..\\windows", "a/b", "a\\b"):
        gd = snapshots._git_dir(bad)
        assert os.path.dirname(os.path.abspath(gd)) == os.path.abspath(snapshots._root())


# --------------------------------------------------------------------------- #
# Housekeeping
# --------------------------------------------------------------------------- #

def test_exists_reports_what_is_there():
    sid, d = _sid("ex"), _project(**{"f.txt": "a"})
    commit = snapshots.take(sid, d)
    assert snapshots.exists(sid, commit) is True
    assert snapshots.exists(sid, "0" * 40) is False
    assert snapshots.exists(_sid("other"), commit) is False


def test_discard_removes_them():
    sid, d = _sid("dis"), _project(**{"f.txt": "a"})
    snapshots.take(sid, d)
    assert snapshots.size_bytes(sid) > 0
    assert snapshots.discard(sid) is True
    assert snapshots.size_bytes(sid) == 0
    assert snapshots.discard(sid) is False        # already gone
