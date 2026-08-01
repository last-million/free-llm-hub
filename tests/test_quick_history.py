"""The quick chat remembering what was said.

Before this, the plain #sec-chat kept its conversation in the page only: a
reload wiped it. These cover the three promises the history makes -- a
conversation comes back exactly as it was said, the list is in the order a
human expects, and deleting really deletes -- plus the two ways a file-backed
store like this fails in the wild: an id that is not really an id, and a file
that is no longer valid JSON.

No network, no Flask, no app import. Its own temp dir rather than pytest's
`tmp_path` factory, which raises PermissionError on this machine (see
tests/test_workspace.py's `proj` fixture).
"""
import json
import os
import shutil
import tempfile
import time

import pytest

import quick_history


@pytest.fixture(autouse=True)
def store(monkeypatch):
    """Point the module at a throwaway folder.

    Autouse on purpose: no test in this file may be able to reach -- or
    delete from -- the real ~/.free-llm-hub history. `base` is the folder the
    store lives INSIDE, so a test can assert nothing was written next to it.
    """
    base = tempfile.mkdtemp(prefix="hubqh-")
    root = os.path.join(base, "state", "quick_history")
    monkeypatch.setattr(quick_history, "_root", lambda: root)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def files_under(path):
    """Every file below `path`, relative and slash-normalised."""
    found = set()
    for dirpath, _dirs, names in os.walk(path):
        for name in names:
            rel = os.path.relpath(os.path.join(dirpath, name), path)
            found.add(rel.replace("\\", "/"))
    return found


# --------------------------------------------------------------------------- #
# Saying something and getting it back
# --------------------------------------------------------------------------- #

def test_a_saved_exchange_comes_back_word_for_word():
    quick_history.save_turn("abc123", "how do I sort a list?",
                            "Use sorted().", model="llama-3.3-70b", provider="groq")
    conv = quick_history.load_conversation("abc123")
    assert [t["role"] for t in conv["turns"]] == ["user", "assistant"]
    assert conv["turns"][0]["content"] == "how do I sort a list?"
    assert conv["turns"][1]["content"] == "Use sorted()."
    assert conv["turns"][1]["model"] == "llama-3.3-70b"
    assert conv["turns"][1]["provider"] == "groq"


def test_a_saved_conversation_is_in_the_list():
    quick_history.save_turn("abc123", "hello", "hi there", model="gpt-oss-120b")
    rows = quick_history.list_conversations()
    assert len(rows) == 1
    assert rows[0]["id"] == "abc123"
    assert rows[0]["turns"] == 2, "one exchange is two messages"
    assert rows[0]["model"] == "gpt-oss-120b"
    assert rows[0]["updated"] > 0
    assert not any(isinstance(v, list) for v in rows[0].values()), \
        "the list must stay metadata -- no transcript in a listing"


def test_the_list_shows_a_title_taken_from_the_first_question():
    quick_history.save_turn("abc123", "  what is a  monad?  ", "A burrito.")
    quick_history.save_turn("abc123", "explain again", "Still a burrito.")
    assert quick_history.list_conversations()[0]["title"] == "what is a monad?"


def test_a_very_long_first_question_becomes_a_short_title():
    quick_history.save_turn("abc123", "word " * 200, "ok")
    title = quick_history.list_conversations()[0]["title"]
    assert len(title) <= quick_history.MAX_TITLE_CHARS + 1
    assert title.endswith("\u2026")


def test_a_conversation_with_nothing_to_name_it_still_has_a_title():
    """An empty row in the sidebar is worse than a placeholder."""
    quick_history.save_turn("abc123", "", "I need a question first.")
    assert quick_history.list_conversations()[0]["title"] == quick_history.DEFAULT_TITLE
    # ...and the next real message gets to name it.
    quick_history.save_turn("abc123", "now a real question", "an answer")
    assert quick_history.list_conversations()[0]["title"] == "now a real question"


def test_continuing_a_conversation_appends_instead_of_starting_over():
    quick_history.save_turn("abc123", "first", "1")
    quick_history.save_turn("abc123", "second", "2")
    conv = quick_history.load_conversation("abc123")
    assert [t["content"] for t in conv["turns"]] == ["first", "1", "second", "2"]
    assert len(quick_history.list_conversations()) == 1, "one conversation, not two"
    assert conv["created"] <= conv["updated"]


def test_accents_and_newlines_survive_the_round_trip():
    said = "Explique-moi les décorateurs\n\n- avec un exemple"
    quick_history.save_turn("abc123", said, "Voilà :\n\n```py\n@cache\n```")
    conv = quick_history.load_conversation("abc123")
    assert conv["turns"][0]["content"] == said
    assert "```py" in conv["turns"][1]["content"]


def test_opening_a_conversation_that_does_not_exist_gives_nothing():
    assert quick_history.load_conversation("neverSaved") is None


def test_a_fresh_id_is_one_the_store_accepts():
    cid = quick_history.new_conversation_id()
    quick_history.save_turn(cid, "hi", "hello")
    assert quick_history.load_conversation(cid) is not None
    assert quick_history.new_conversation_id() != cid


# --------------------------------------------------------------------------- #
# The order of the list
# --------------------------------------------------------------------------- #

def test_the_newest_conversation_is_first():
    quick_history.save_turn("older", "1", "a")
    quick_history.save_turn("newer", "2", "b")
    assert [r["id"] for r in quick_history.list_conversations()] == ["newer", "older"]


def test_replying_in_an_old_conversation_moves_it_back_to_the_top():
    quick_history.save_turn("older", "1", "a")
    quick_history.save_turn("newer", "2", "b")
    quick_history.save_turn("older", "3", "c")
    rows = quick_history.list_conversations()
    assert [r["id"] for r in rows] == ["older", "newer"]
    assert rows[0]["turns"] == 4


def test_the_list_can_be_kept_short():
    for i in range(5):
        quick_history.save_turn("c%d" % i, "q", "a")
    assert len(quick_history.list_conversations(limit=2)) == 2


def test_no_history_is_an_empty_list_not_an_error():
    assert quick_history.list_conversations() == []


# --------------------------------------------------------------------------- #
# Deleting
# --------------------------------------------------------------------------- #

def test_deleting_a_conversation_really_removes_it():
    quick_history.save_turn("abc123", "hi", "hello")
    path = quick_history._conv_path("abc123")
    assert os.path.exists(path)
    assert quick_history.delete_conversation("abc123") is True
    assert not os.path.exists(path), "the transcript file is still on disk"
    assert quick_history.load_conversation("abc123") is None
    assert quick_history.list_conversations() == []


def test_deleting_the_same_conversation_twice_reports_nothing_to_delete():
    quick_history.save_turn("abc123", "hi", "hello")
    assert quick_history.delete_conversation("abc123") is True
    assert quick_history.delete_conversation("abc123") is False


def test_deleting_leaves_the_other_conversations_alone():
    quick_history.save_turn("keep", "1", "a")
    quick_history.save_turn("drop", "2", "b")
    quick_history.delete_conversation("drop")
    assert [r["id"] for r in quick_history.list_conversations()] == ["keep"]
    assert quick_history.load_conversation("keep")["turns"][0]["content"] == "1"


def test_deleting_something_that_was_never_there_is_not_an_error():
    assert quick_history.delete_conversation("neverSaved") is False
    assert quick_history.delete_conversation(None) is False
    assert quick_history.delete_conversation("") is False


# --------------------------------------------------------------------------- #
# The id is not really an id.
#
# It arrives from the browser, so it reaches this module as a URL segment or a
# JSON field. Anything outside the alphabet new_conversation_id() mints is
# refused outright -- it must never become a path.
# --------------------------------------------------------------------------- #

BAD_IDS = [
    "../../evil",
    "..",
    "sub/../../outside",
    "a/b",
    "a\\b",
    "/etc/passwd",
    "C:\\Windows\\System32\\evil",
    "\\\\server\\share\\evil",
    "abc123\nmore",
    "abc 123",
    "abc.123",
    "",
    None,
    123,
    "x" * 200,
]


@pytest.mark.parametrize("bad", BAD_IDS)
def test_an_id_that_is_not_an_id_is_refused(bad, store):
    before = files_under(store)
    quick_history.save_turn(bad, "hi", "hello")
    assert files_under(store) == before, "a refused id wrote something to disk"
    assert quick_history.load_conversation(bad) is None
    assert quick_history.list_conversations() == []
    assert quick_history.delete_conversation(bad) is False


def test_a_traversal_id_cannot_reach_outside_the_store(store):
    """The whole point: not "it lands somewhere harmless", but "no file is
    created anywhere on this machine"."""
    open(os.path.join(store, "precious.txt"), "w").write("do not touch")
    quick_history.save_turn("../../evil", "hi", "hello")
    quick_history.save_turn("../" * 8 + "evil", "hi", "hello")
    quick_history.delete_conversation("../../precious.txt")
    assert files_under(store) == {"precious.txt"}
    assert open(os.path.join(store, "precious.txt")).read() == "do not touch"
    assert quick_history._conv_path("../../evil") is None


def test_a_real_conversation_only_ever_writes_inside_the_store(store):
    quick_history.save_turn("abc123", "hi", "hello")
    written = files_under(store)
    assert written == {"state/quick_history/index.json",
                       "state/quick_history/abc123.json"}


def test_a_deleted_conversation_leaves_no_stray_files(store):
    quick_history.save_turn("abc123", "hi", "hello")
    quick_history.delete_conversation("abc123")
    assert files_under(store) == {"state/quick_history/index.json"}


# --------------------------------------------------------------------------- #
# Files that are no longer valid.
#
# Half-written JSON is the normal end state of a machine losing power mid
# write. A dashboard section must come up empty, never 500.
# --------------------------------------------------------------------------- #

def test_a_corrupt_index_shows_an_empty_history_instead_of_failing():
    quick_history.save_turn("abc123", "hi", "hello")
    with open(quick_history._index_path(), "w", encoding="utf-8") as f:
        f.write("{not valid json")
    assert quick_history.list_conversations() == []


def test_a_corrupt_transcript_opens_as_nothing_instead_of_failing():
    quick_history.save_turn("abc123", "hi", "hello")
    with open(quick_history._conv_path("abc123"), "w", encoding="utf-8") as f:
        f.write("]]] not json at all")
    assert quick_history.load_conversation("abc123") is None


def test_a_corrupt_transcript_does_not_block_the_next_message():
    quick_history.save_turn("abc123", "hi", "hello")
    with open(quick_history._conv_path("abc123"), "w", encoding="utf-8") as f:
        f.write("]]] not json at all")
    quick_history.save_turn("abc123", "still here?", "yes")
    conv = quick_history.load_conversation("abc123")
    assert [t["content"] for t in conv["turns"]] == ["still here?", "yes"]


def test_an_index_holding_the_wrong_shape_is_ignored():
    """Hand-edited, or written by something else entirely."""
    os.makedirs(quick_history._root(), exist_ok=True)
    with open(quick_history._index_path(), "w", encoding="utf-8") as f:
        json.dump({"not": "a list"}, f)
    assert quick_history.list_conversations() == []
    quick_history.save_turn("abc123", "hi", "hello")
    assert [r["id"] for r in quick_history.list_conversations()] == ["abc123"]


def test_a_missing_store_is_an_empty_history():
    assert not os.path.exists(quick_history._root())
    assert quick_history.list_conversations() == []
    assert quick_history.load_conversation("abc123") is None
    assert quick_history.delete_conversation("abc123") is False


def test_a_reply_is_still_saved_when_the_disk_refuses(monkeypatch):
    """save_turn() runs on the response path of a real chat request. A broken
    history must cost the user their history, never their answer."""
    def _boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(quick_history.os, "replace", _boom)
    quick_history.save_turn("abc123", "hi", "hello")   # must not raise
    assert quick_history.list_conversations() == []


# --------------------------------------------------------------------------- #
# Caps -- this folder must not grow forever
# --------------------------------------------------------------------------- #

def test_the_oldest_conversations_fall_off_the_end(monkeypatch):
    monkeypatch.setattr(quick_history, "MAX_CONVERSATIONS", 3)
    for i in range(5):
        quick_history.save_turn("c%d" % i, "q%d" % i, "a")
    rows = quick_history.list_conversations(limit=100)
    assert [r["id"] for r in rows] == ["c4", "c3", "c2"]
    assert quick_history.load_conversation("c0") is None
    assert not os.path.exists(quick_history._conv_path("c0")), \
        "pruned conversations must free their bytes, not just go unlisted"
    assert not os.path.exists(quick_history._conv_path("c1"))


def test_one_endless_conversation_stops_growing(monkeypatch):
    monkeypatch.setattr(quick_history, "MAX_TURNS", 4)
    for i in range(5):
        quick_history.save_turn("abc123", "q%d" % i, "a%d" % i)
    conv = quick_history.load_conversation("abc123")
    assert [t["content"] for t in conv["turns"]] == ["q3", "a3", "q4", "a4"], \
        "the oldest messages go first, and a reply never outlives its question"
    assert quick_history.list_conversations()[0]["turns"] == 4


def test_the_turn_cap_keeps_questions_and_answers_paired():
    assert quick_history.MAX_TURNS % 2 == 0


def test_a_conversation_nobody_touched_for_a_month_is_dropped():
    quick_history.save_turn("stale", "hi", "hello")
    conv = quick_history._load_conv("stale")
    old = time.time() - (quick_history.RETENTION_DAYS + 5) * 86400
    conv["updated"] = old
    quick_history._save_conv(conv)
    entries = quick_history._load_index()
    for e in entries:
        if e["id"] == "stale":
            e["updated"] = old
    quick_history._save_index(entries)

    quick_history.save_turn("fresh", "hi", "hello")   # any save re-prunes

    assert quick_history.load_conversation("stale") is None
    assert not os.path.exists(quick_history._conv_path("stale"))
    assert quick_history.load_conversation("fresh") is not None


# --------------------------------------------------------------------------- #
# Where it lives
# --------------------------------------------------------------------------- #

def test_the_store_sits_in_the_hubs_own_state_folder(monkeypatch):
    monkeypatch.undo()          # drop the fixture's redirect for this one test
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG",
                       os.path.join(tempfile.gettempdir(), "hubqh-cfg", "config.json"))
    root = quick_history._root()
    assert os.path.dirname(root) == os.path.join(tempfile.gettempdir(), "hubqh-cfg")
    assert os.path.basename(root) == "quick_history"
    assert not os.path.exists(root), "reading the path must not create anything"
