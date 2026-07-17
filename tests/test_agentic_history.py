"""Tests for agentic_history.py -- the persisted conversation-transcript +
rewind-checkpoint layer on top of agentic_chat.py's in-memory session
registry. Pure file-based; never spawns a subprocess, never touches
agentic_chat's own _REGISTRY.
"""
import os
import time

import pytest

import agentic_history


@pytest.fixture
def isolated_history(tmp_path, monkeypatch):
    path = tmp_path / "state" / "config.json"
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(path))
    return path


# --------------------------------------------------------------------------- #
# record_turn / get_conversation -- basic round trip
# --------------------------------------------------------------------------- #

def test_record_turn_creates_conversation_lazily(isolated_history):
    agentic_history.record_turn("sess-1", "claude", "/tmp/proj", "user", "hello")
    conv = agentic_history.get_conversation("sess-1")
    assert conv is not None
    assert conv["session_id"] == "sess-1"
    assert conv["cli_id"] == "claude"
    assert conv["project_dir"] == "/tmp/proj"
    assert len(conv["turns"]) == 1
    assert conv["turns"][0]["role"] == "user"
    assert conv["turns"][0]["text"] == "hello"
    assert conv["checkpoints"] == []


def test_record_turn_appends_in_order(isolated_history):
    agentic_history.record_turn("sess-1", "claude", "/tmp/proj", "user", "hi")
    agentic_history.record_turn("sess-1", "claude", "/tmp/proj", "agent", "hello back")
    agentic_history.record_turn("sess-1", "claude", "/tmp/proj", "user", "thanks")
    conv = agentic_history.get_conversation("sess-1")
    roles = [t["role"] for t in conv["turns"]]
    texts = [t["text"] for t in conv["turns"]]
    assert roles == ["user", "agent", "user"]
    assert texts == ["hi", "hello back", "thanks"]


def test_record_turn_stores_native_session_id(isolated_history):
    agentic_history.record_turn("sess-1", "claude", "/tmp/proj", "agent", "done",
                                native_session_id="native-abc")
    conv = agentic_history.get_conversation("sess-1")
    assert conv["turns"][0]["native_session_id"] == "native-abc"


def test_record_turn_ignores_invalid_role(isolated_history):
    agentic_history.record_turn("sess-1", "claude", "/tmp/proj", "system", "nope")
    assert agentic_history.get_conversation("sess-1") is None


def test_record_turn_ignores_missing_session_id(isolated_history):
    agentic_history.record_turn(None, "claude", "/tmp/proj", "user", "x")
    agentic_history.record_turn("", "claude", "/tmp/proj", "user", "x")
    assert agentic_history.list_conversations() == []


def test_record_turn_never_raises_on_non_string_text(isolated_history):
    agentic_history.record_turn("sess-1", "claude", "/tmp/proj", "agent", None)
    conv = agentic_history.get_conversation("sess-1")
    assert conv["turns"][0]["text"] == ""


def test_get_conversation_unknown_returns_none(isolated_history):
    assert agentic_history.get_conversation("does-not-exist") is None


def test_record_turn_updates_last_active_at(isolated_history):
    agentic_history.record_turn("sess-1", "claude", "/tmp/proj", "user", "hi")
    first = agentic_history.get_conversation("sess-1")["last_active_at"]
    time.sleep(0.01)
    agentic_history.record_turn("sess-1", "claude", "/tmp/proj", "agent", "hello")
    conv = agentic_history.get_conversation("sess-1")
    assert conv["last_active_at"] >= first
    assert conv["started_at"] <= conv["last_active_at"]


# --------------------------------------------------------------------------- #
# list_conversations -- metadata-only, newest-first
# --------------------------------------------------------------------------- #

def test_list_conversations_metadata_shape(isolated_history):
    agentic_history.record_turn("sess-1", "claude", "/tmp/proj", "user", "hi")
    agentic_history.record_turn("sess-1", "claude", "/tmp/proj", "agent", "hello")
    rows = agentic_history.list_conversations()
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == "sess-1"
    assert row["cli_id"] == "claude"
    assert row["project_dir"] == "/tmp/proj"
    assert row["turn_count"] == 2
    assert "started_at" in row and "last_active_at" in row
    assert "turns" not in row  # metadata only, no full transcript leak


def test_list_conversations_newest_first(isolated_history):
    agentic_history.record_turn("sess-1", "claude", "/tmp/a", "user", "1")
    time.sleep(0.01)
    agentic_history.record_turn("sess-2", "claude", "/tmp/b", "user", "2")
    rows = agentic_history.list_conversations()
    assert [r["session_id"] for r in rows] == ["sess-2", "sess-1"]


def test_list_conversations_touching_bumps_to_front(isolated_history):
    agentic_history.record_turn("sess-1", "claude", "/tmp/a", "user", "1")
    time.sleep(0.01)
    agentic_history.record_turn("sess-2", "claude", "/tmp/b", "user", "2")
    time.sleep(0.01)
    agentic_history.record_turn("sess-1", "claude", "/tmp/a", "agent", "reply")
    rows = agentic_history.list_conversations()
    assert [r["session_id"] for r in rows] == ["sess-1", "sess-2"]
    assert rows[0]["turn_count"] == 2


def test_list_conversations_respects_limit(isolated_history):
    for i in range(5):
        agentic_history.record_turn("sess-%d" % i, "claude", "/tmp", "user", "x")
    rows = agentic_history.list_conversations(limit=2)
    assert len(rows) == 2


def test_list_conversations_empty_when_none(isolated_history):
    assert agentic_history.list_conversations() == []


# --------------------------------------------------------------------------- #
# delete_conversation
# --------------------------------------------------------------------------- #

def test_delete_conversation_removes_row_and_file(isolated_history):
    agentic_history.record_turn("sess-1", "claude", "/tmp/proj", "user", "hi")
    path = agentic_history._conv_path("sess-1")
    assert os.path.exists(path)
    assert agentic_history.delete_conversation("sess-1") is True
    assert not os.path.exists(path)
    assert agentic_history.get_conversation("sess-1") is None
    assert agentic_history.list_conversations() == []


def test_delete_conversation_unknown_returns_false(isolated_history):
    assert agentic_history.delete_conversation("nope") is False


def test_delete_conversation_missing_session_id_returns_false(isolated_history):
    assert agentic_history.delete_conversation(None) is False
    assert agentic_history.delete_conversation("") is False


# --------------------------------------------------------------------------- #
# Checkpoints
# --------------------------------------------------------------------------- #

def test_create_checkpoint_records_turn_index(isolated_history):
    agentic_history.record_turn("sess-1", "claude", "/tmp/proj", "user", "hi")
    agentic_history.record_turn("sess-1", "claude", "/tmp/proj", "agent", "hello")
    checkpoint = agentic_history.create_checkpoint("sess-1", label="before refactor")
    assert checkpoint is not None
    assert checkpoint["turn_index"] == 2
    assert checkpoint["label"] == "before refactor"
    assert "created_at" in checkpoint
    assert "id" in checkpoint


def test_create_checkpoint_no_label_is_none(isolated_history):
    agentic_history.record_turn("sess-1", "claude", "/tmp/proj", "user", "hi")
    checkpoint = agentic_history.create_checkpoint("sess-1")
    assert checkpoint["label"] is None


def test_create_checkpoint_blank_label_is_none(isolated_history):
    agentic_history.record_turn("sess-1", "claude", "/tmp/proj", "user", "hi")
    checkpoint = agentic_history.create_checkpoint("sess-1", label="   ")
    assert checkpoint["label"] is None


def test_create_checkpoint_unknown_conversation_returns_none(isolated_history):
    assert agentic_history.create_checkpoint("does-not-exist") is None


def test_create_checkpoint_after_deletion_returns_none(isolated_history):
    agentic_history.record_turn("sess-1", "claude", "/tmp/proj", "user", "hi")
    agentic_history.delete_conversation("sess-1")
    assert agentic_history.create_checkpoint("sess-1") is None


def test_list_checkpoints_oldest_first(isolated_history):
    agentic_history.record_turn("sess-1", "claude", "/tmp/proj", "user", "hi")
    c1 = agentic_history.create_checkpoint("sess-1", label="first")
    agentic_history.record_turn("sess-1", "claude", "/tmp/proj", "agent", "reply")
    c2 = agentic_history.create_checkpoint("sess-1", label="second")
    checkpoints = agentic_history.list_checkpoints("sess-1")
    assert [c["label"] for c in checkpoints] == ["first", "second"]
    assert [c["id"] for c in checkpoints] == [c1["id"], c2["id"]]


def test_list_checkpoints_empty_for_unknown_conversation(isolated_history):
    assert agentic_history.list_checkpoints("does-not-exist") == []


def test_list_checkpoints_empty_when_none_created(isolated_history):
    agentic_history.record_turn("sess-1", "claude", "/tmp/proj", "user", "hi")
    assert agentic_history.list_checkpoints("sess-1") == []


def test_checkpoint_bumps_last_active_and_survives_prune_after_old_turn(isolated_history):
    """Regression: bookmarking a conversation must itself count as activity --
    otherwise RETENTION_DAYS measured from the last TURN (which can be long
    before the checkpoint) would silently prune something the user just
    deliberately marked "come back to this"."""
    agentic_history.record_turn("sess-1", "claude", "/tmp/proj", "user", "hi")
    conv = agentic_history.get_conversation("sess-1")
    old_ts = time.time() - (agentic_history.RETENTION_DAYS + 5) * 86400
    conv["last_active_at"] = old_ts
    agentic_history._save_conversation(conv)
    entries = agentic_history._load_index()
    for e in entries:
        if e["session_id"] == "sess-1":
            e["last_active_at"] = old_ts
    agentic_history._save_index(entries)

    # Checkpoint created "today" -- must refresh last_active_at back to now.
    agentic_history.create_checkpoint("sess-1", label="keep me")
    row = agentic_history.list_conversations()[0]
    assert row["last_active_at"] > old_ts

    # A prune triggered by unrelated activity must NOT drop this conversation.
    agentic_history.record_turn("sess-2", "claude", "/tmp", "user", "y")
    assert agentic_history.get_conversation("sess-1") is not None
    assert agentic_history.list_checkpoints("sess-1")[0]["label"] == "keep me"


def test_checkpoint_is_not_a_filesystem_snapshot(isolated_history, tmp_path):
    """Scope-limit regression: creating a checkpoint must never touch the
    project directory on disk -- it is a transcript bookmark only."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "original.txt").write_text("v1", encoding="utf-8")
    agentic_history.record_turn("sess-1", "claude", str(project_dir), "user", "hi")
    agentic_history.create_checkpoint("sess-1", label="snapshot?")
    (project_dir / "original.txt").write_text("v2 -- edited after checkpoint", encoding="utf-8")
    # The checkpoint holds no copy of the file; the live file was free to
    # change and nothing under agentic_history's own storage root reverted it.
    assert (project_dir / "original.txt").read_text(encoding="utf-8") == "v2 -- edited after checkpoint"
    checkpoint = agentic_history.list_checkpoints("sess-1")[0]
    assert set(checkpoint.keys()) == {"id", "turn_index", "created_at", "label"}


# --------------------------------------------------------------------------- #
# Retention / pruning
# --------------------------------------------------------------------------- #

def test_prune_drops_oldest_conversations_past_max_count(isolated_history, monkeypatch):
    monkeypatch.setattr(agentic_history, "MAX_CONVERSATIONS", 3)
    for i in range(5):
        agentic_history.record_turn("sess-%d" % i, "claude", "/tmp", "user", "x")
        time.sleep(0.005)
    rows = agentic_history.list_conversations(limit=100)
    assert len(rows) == 3
    survivors = {r["session_id"] for r in rows}
    assert survivors == {"sess-2", "sess-3", "sess-4"}
    # dropped conversations' files are actually deleted, not just unlisted
    assert agentic_history.get_conversation("sess-0") is None
    assert not os.path.exists(agentic_history._conv_path("sess-0"))
    assert not os.path.exists(agentic_history._conv_path("sess-1"))


def test_prune_drops_conversations_older_than_retention(isolated_history, monkeypatch):
    agentic_history.record_turn("old-sess", "claude", "/tmp", "user", "x")
    conv = agentic_history.get_conversation("old-sess")
    conv["last_active_at"] = time.time() - (agentic_history.RETENTION_DAYS + 5) * 86400
    agentic_history._save_conversation(conv)
    entries = agentic_history._load_index()
    for e in entries:
        if e["session_id"] == "old-sess":
            e["last_active_at"] = conv["last_active_at"]
    agentic_history._save_index(entries)

    # Any new record_turn() call re-prunes the whole index.
    agentic_history.record_turn("new-sess", "claude", "/tmp", "user", "y")

    assert agentic_history.get_conversation("old-sess") is None
    assert not os.path.exists(agentic_history._conv_path("old-sess"))
    assert agentic_history.get_conversation("new-sess") is not None


# --------------------------------------------------------------------------- #
# Path safety -- session_id is also a URL path segment (Flask route param),
# so it must never be trusted blindly as a filename.
# --------------------------------------------------------------------------- #

def test_safe_filename_handles_path_traversal_attempt(isolated_history):
    malicious = "../../evil"
    agentic_history.record_turn(malicious, "claude", "/tmp", "user", "hi")
    path = agentic_history._conv_path(malicious)
    root = os.path.abspath(agentic_history._root())
    assert os.path.commonpath([root, os.path.abspath(path)]) == root
    conv = agentic_history.get_conversation(malicious)
    assert conv is not None
    assert conv["turns"][0]["text"] == "hi"


def test_safe_filename_plain_uuid_used_as_is(isolated_history):
    sid = "abcdef0123456789"
    assert agentic_history._safe_filename(sid) == sid


# --------------------------------------------------------------------------- #
# Atomic-write safety -- simulate a crash mid-write (os.replace raises after
# the tmp file is fully written): the tmp file must be cleaned up and any
# pre-existing on-disk data must be left untouched. Mirrors the
# tmp-file-then-rename idiom shared with usage_history.py / image_history.py.
# --------------------------------------------------------------------------- #

def test_save_index_crash_mid_write_cleans_up_tmp_and_preserves_original(isolated_history, monkeypatch):
    agentic_history._save_index([{"session_id": "sess-1", "turn_count": 1}])
    root = agentic_history._root()

    def _boom(*a, **kw):
        raise OSError("simulated crash")

    monkeypatch.setattr(agentic_history.os, "replace", _boom)
    with pytest.raises(OSError):
        agentic_history._save_index([{"session_id": "sess-2", "turn_count": 1}])

    # tmp file was cleaned up -- nothing but the original index.json remains
    leftovers = [f for f in os.listdir(root) if f != "index.json"]
    assert leftovers == []
    # original content untouched (rename never happened)
    on_disk = agentic_history._load_index()
    assert on_disk == [{"session_id": "sess-1", "turn_count": 1}]


def test_save_conversation_crash_mid_write_cleans_up_tmp_and_preserves_original(isolated_history, monkeypatch):
    conv = {"session_id": "sess-1", "cli_id": "claude", "project_dir": "/tmp",
            "started_at": 1.0, "last_active_at": 1.0, "turns": [{"role": "user", "text": "hi"}],
            "checkpoints": []}
    agentic_history._save_conversation(conv)
    root = agentic_history._root()

    def _boom(*a, **kw):
        raise OSError("simulated crash")

    monkeypatch.setattr(agentic_history.os, "replace", _boom)
    corrupted = dict(conv)
    corrupted["turns"] = conv["turns"] + [{"role": "agent", "text": "should not persist"}]
    with pytest.raises(OSError):
        agentic_history._save_conversation(corrupted)

    leftovers = [f for f in os.listdir(root) if not f.endswith(".json") or f == "index.json"]
    assert leftovers == []  # no stray .tmp files
    on_disk = agentic_history._load_conversation("sess-1")
    assert len(on_disk["turns"]) == 1  # original, un-corrupted content survives


def test_load_index_recovers_from_malformed_json(isolated_history):
    root = agentic_history._root()
    os.makedirs(root, exist_ok=True)
    with open(agentic_history._index_path(), "w", encoding="utf-8") as f:
        f.write("{not valid json")
    assert agentic_history._load_index() == []


def test_load_conversation_recovers_from_malformed_json(isolated_history):
    root = agentic_history._root()
    os.makedirs(root, exist_ok=True)
    path = agentic_history._conv_path("sess-1")
    with open(path, "w", encoding="utf-8") as f:
        f.write("not json at all")
    assert agentic_history._load_conversation("sess-1") is None
