"""Restore a previous message: the conversation AND the code, then rerun.

ASKED 2026-08-31: "in /agent that he be able to have checkpoints buttons for
previous message to restaure conversation and code and rerun it".

The transcript half already existed as a bookmark. This is the whole flow:

  before every turn   snapshots.take() commits the project to a shadow git repo
                      and the commit id is recorded ON that turn
  Restore & rerun     snapshots.restore() puts the files back, then
                      agentic_history.truncate_to_turn() puts the conversation
                      back to the same point, then the UI re-sends the message

Both halves move together, always. Restoring only the conversation would leave
the transcript claiming a state the files no longer have, and restoring only
the files would leave the agent believing work it can no longer see.
"""
import os
import tempfile
import uuid

import pytest

import agentic_history as ah
import config
import snapshots

pytestmark = pytest.mark.skipif(not snapshots.git_available(),
                                reason="git is not installed")


def _client():
    import app
    app.app.config["TESTING"] = True
    return app.app.test_client()


def _hdrs():
    return {"X-Free-LLM-Hub-Token": config.ensure_control_token(),
            "X-Free-LLM-Hub": "dashboard"}


def _project(**files):
    d = tempfile.mkdtemp(prefix="rewind-")
    for name, body in files.items():
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(body)
    return d


def _read(d, name):
    with open(os.path.join(d, name), encoding="utf-8") as f:
        return f.read()


def _built_session():
    """A conversation two turns deep, each with its files snapshotted first --
    the shape a real /build session has by the time you want to rewind."""
    sid = "rw-" + uuid.uuid4().hex[:10]
    d = _project(**{"index.html": "<h1>v1</h1>"})

    snap1 = snapshots.take(sid, d, "before turn 1")
    ah.record_turn(sid, "codex", d, "user", "build the homepage", snapshot=snap1)
    with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
        f.write("<h1>v2 homepage</h1>")
    ah.record_turn(sid, "codex", d, "agent", "Homepage built.")

    snap2 = snapshots.take(sid, d, "before turn 2")
    ah.record_turn(sid, "codex", d, "user", "now restyle it", snapshot=snap2)
    with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
        f.write("<h1>v3 RUINED</h1>")
    with open(os.path.join(d, "junk.css"), "w", encoding="utf-8") as f:
        f.write("/* slop */")
    ah.record_turn(sid, "codex", d, "agent", "Restyled.")
    return sid, d


# --------------------------------------------------------------------------- #
# The whole point
# --------------------------------------------------------------------------- #

def test_rewinding_puts_the_code_back():
    sid, d = _built_session()
    r = _client().post("/api/agent/history/%s/rewind" % sid,
                       json={"index": 2}, headers=_hdrs())
    assert r.status_code == 200, r.get_json()
    assert _read(d, "index.html") == "<h1>v2 homepage</h1>"
    assert not os.path.exists(os.path.join(d, "junk.css"))


def test_rewinding_puts_the_conversation_back():
    sid, _d = _built_session()
    _client().post("/api/agent/history/%s/rewind" % sid,
                   json={"index": 2}, headers=_hdrs())
    turns = ah.get_conversation(sid)["turns"]
    assert len(turns) == 2
    assert turns[-1]["text"] == "Homepage built."


def test_it_hands_back_the_message_so_the_ui_can_rerun_it():
    """The "and rerun it" half: the UI sends this straight back through the
    ordinary send path."""
    sid, _d = _built_session()
    r = _client().post("/api/agent/history/%s/rewind" % sid,
                       json={"index": 2}, headers=_hdrs())
    assert r.get_json()["text"] == "now restyle it"
    assert r.get_json()["turns_removed"] == 2


def test_going_all_the_way_back_to_the_first_message():
    sid, d = _built_session()
    r = _client().post("/api/agent/history/%s/rewind" % sid,
                       json={"index": 0}, headers=_hdrs())
    assert r.status_code == 200
    assert _read(d, "index.html") == "<h1>v1</h1>"
    assert ah.get_conversation(sid)["turns"] == []


# --------------------------------------------------------------------------- #
# Refusals -- it never half-does it
# --------------------------------------------------------------------------- #

def test_a_turn_with_no_snapshot_is_refused():
    """Turns recorded before this shipped, or in a project too big to snapshot.
    Better an honest refusal than restoring a conversation whose files cannot
    follow it."""
    sid = "rw-nosnap-" + uuid.uuid4().hex[:8]
    d = _project(**{"a.txt": "a"})
    ah.record_turn(sid, "codex", d, "user", "do a thing")
    r = _client().post("/api/agent/history/%s/rewind" % sid,
                       json={"index": 0}, headers=_hdrs())
    assert r.status_code == 400
    assert r.get_json()["code"] == "no_snapshot"
    assert len(ah.get_conversation(sid)["turns"]) == 1, "nothing may be dropped"


def test_an_out_of_range_turn_is_refused():
    sid, _d = _built_session()
    for bad in (-1, 99):
        r = _client().post("/api/agent/history/%s/rewind" % sid,
                           json={"index": bad}, headers=_hdrs())
        assert r.status_code == 400, bad
    assert len(ah.get_conversation(sid)["turns"]) == 4


def test_a_missing_index_is_refused():
    sid, _d = _built_session()
    r = _client().post("/api/agent/history/%s/rewind" % sid,
                       json={}, headers=_hdrs())
    assert r.status_code == 400


def test_an_unknown_conversation_is_a_404():
    r = _client().post("/api/agent/history/nope-%s/rewind" % uuid.uuid4().hex[:6],
                       json={"index": 0}, headers=_hdrs())
    assert r.status_code == 404


def test_a_deleted_project_folder_is_refused_before_anything_is_dropped():
    sid, d = _built_session()
    import shutil
    shutil.rmtree(d, ignore_errors=True)
    r = _client().post("/api/agent/history/%s/rewind" % sid,
                       json={"index": 2}, headers=_hdrs())
    assert r.status_code == 400
    assert r.get_json()["code"] == "folder_gone"
    assert len(ah.get_conversation(sid)["turns"]) == 4, "transcript must survive"


def test_it_needs_the_agent_gate_like_every_other_agent_route():
    r = _client().post("/api/agent/history/whatever/rewind", json={"index": 0})
    assert r.status_code in (401, 403, 404)


# --------------------------------------------------------------------------- #
# The storage layer underneath
# --------------------------------------------------------------------------- #

def test_truncate_drops_checkpoints_past_the_new_end():
    sid, _d = _built_session()
    ah.create_checkpoint(sid, "late bookmark")
    assert ah.truncate_to_turn(sid, 1) == 3
    conv = ah.get_conversation(sid)
    assert len(conv["turns"]) == 1
    assert all((c.get("turn_index") or 0) <= 1 for c in conv["checkpoints"])


def test_truncate_is_safe_on_nonsense():
    assert ah.truncate_to_turn(None, 0) is None
    assert ah.truncate_to_turn("nope-" + uuid.uuid4().hex[:6], 0) is None
    sid, _d = _built_session()
    assert ah.truncate_to_turn(sid, -1) is None
    assert ah.truncate_to_turn(sid, 999) == 0


def test_a_turn_records_the_snapshot_it_was_given():
    sid = "rw-rec-" + uuid.uuid4().hex[:8]
    d = _project(**{"a.txt": "a"})
    commit = snapshots.take(sid, d)
    ah.record_turn(sid, "codex", d, "user", "hello", snapshot=commit)
    assert ah.get_conversation(sid)["turns"][0]["snapshot"] == commit


# --------------------------------------------------------------------------- #
# The button
# --------------------------------------------------------------------------- #

def _template():
    with open("templates/index.html", encoding="utf-8") as f:
        return f.read()


def test_the_button_is_rendered_for_user_turns_that_have_one():
    html = _template()
    assert "ah-rewind" in html
    assert "Restore &amp; rerun" in html
    assert "t.role === 'user' && t.snapshot" in html


def test_it_confirms_before_destroying_anything():
    html = _template()
    block = html.split("function wireRewindButtons", 1)[1][:2000]
    assert "confirm(" in block
    assert "cannot be undone" in block


def test_it_reruns_the_message_afterwards():
    html = _template()
    block = html.split("function wireRewindButtons", 1)[1][:2000]
    assert "sendAgentMessage(r.text)" in block
