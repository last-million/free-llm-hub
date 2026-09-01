"""The model's memory has to survive a turn that did not finish.

ASKED 2026-09-01: "so he should save the context window and the memory u
understand?" -- after a live conversation lost everything mid-build.

WHAT THE STORED CONVERSATION ACTUALLY LOOKED LIKE (measured, not supposed):

    cli            : codex
    turns saved    : 2
    conv native id : None
       [0] user   native=-  snapshot=-
       [1] user   native=-  snapshot=-

Two turns, BOTH role "user". No agent reply, no thread id, no file snapshot.

That explains every symptom at once. The CLI's own thread id is what
`codex exec resume <id>` / `claude --resume <id>` need to restore the MODEL's
context -- the transcript on disk only restores what is displayed. It was
written as a side effect of recording the agent's reply, so a turn that was
interrupted never wrote it, and /resume then had nothing to resume: the model
came back knowing only the files on disk. "when i refresh page it's like he will
start from beginning."

And the file snapshot was missing entirely, because it had only been added to
the NON-streaming route -- while every turn from /build streams. The whole
Restore & rerun feature was inert on the one path the dashboard uses.

Both are now written as early as they can be: the snapshot before the turn
touches anything, the thread id the moment it exists rather than when the turn
survives.
"""
import os
import tempfile
import uuid

import agentic_history as ah


def _sid(label):
    return "ctx-%s-%s" % (label, uuid.uuid4().hex[:8])


# --------------------------------------------------------------------------- #
# The thread id, saved on its own
# --------------------------------------------------------------------------- #

def test_the_thread_id_can_be_saved_without_an_agent_reply():
    """The whole point: an interrupted turn still leaves the memory recoverable."""
    sid = _sid("alone")
    ah.record_turn(sid, "codex", "/tmp/p", "user", "build the site")
    assert ah.set_native_session_id(sid, "01a0-thread") == "01a0-thread"
    assert ah.get_conversation(sid)["native_session_id"] == "01a0-thread"


def test_saving_the_same_id_again_does_not_rewrite_the_file():
    """It is called once per streamed event, so the unchanged case must be free."""
    from unittest import mock
    sid = _sid("noop")
    ah.record_turn(sid, "codex", "/tmp/p", "user", "hi")
    ah.set_native_session_id(sid, "t-1")
    with mock.patch.object(ah, "_save_conversation") as save:
        assert ah.set_native_session_id(sid, "t-1") == "t-1"
    assert not save.called


def test_a_newer_id_replaces_an_older_one():
    sid = _sid("replace")
    ah.record_turn(sid, "codex", "/tmp/p", "user", "hi")
    ah.set_native_session_id(sid, "t-1")
    ah.set_native_session_id(sid, "t-2")
    assert ah.get_conversation(sid)["native_session_id"] == "t-2"


def test_nonsense_is_safe():
    assert ah.set_native_session_id(None, "x") is None
    assert ah.set_native_session_id(_sid("nope"), "x") is None      # no conversation
    sid = _sid("empty")
    ah.record_turn(sid, "codex", "/tmp/p", "user", "hi")
    assert ah.set_native_session_id(sid, "") is None
    assert ah.set_native_session_id(sid, None) is None


def test_resume_can_then_restore_the_model_context():
    """What the id is FOR: /resume reports resumed_thread only when it has one,
    and that is the difference between the model remembering the conversation
    and only seeing the files."""
    sid = _sid("resume")
    ah.record_turn(sid, "codex", "/tmp/p", "user", "build it")
    conv = ah.get_conversation(sid)
    assert not conv.get("native_session_id")          # the broken state, reproduced
    ah.set_native_session_id(sid, "01a0-real-thread")
    assert ah.get_conversation(sid)["native_session_id"] == "01a0-real-thread"


# --------------------------------------------------------------------------- #
# The streaming route persists both
# --------------------------------------------------------------------------- #

def _app_source():
    with open("app.py", encoding="utf-8") as f:
        return f.read()


def _stream_route():
    src = _app_source()
    i = src.index("def api_agent_send_message_stream")
    # to the end of the route, not a fixed slice: the finally block sits past
    # 3000 characters and a short window silently skipped it
    j = src.index("@app.route", i)
    return src[i:j]


def test_the_streaming_route_snapshots_the_files():
    """It only ever did this on the non-streaming route, so Restore & rerun was
    inert on the path /build uses."""
    body = _stream_route()
    assert "snapshots.take(" in body
    assert "snapshot=snap" in body


def test_the_streaming_route_persists_the_thread_id_as_it_appears():
    body = _stream_route()
    assert "_keep_context()" in body
    assert "set_native_session_id" in body


def test_it_is_persisted_on_the_way_out_too():
    """The id may only appear as the turn ERRORS, which is exactly the case that
    was losing it."""
    body = _stream_route()
    tail = body.split("finally:", 1)[1][:400]
    assert "_keep_context()" in tail


def test_the_streaming_route_also_keeps_the_quality_in_step():
    body = _stream_route()
    assert "set_quality" in body
