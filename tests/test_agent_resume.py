"""Continuing a conversation after the hub restarts.

THE BUG: "why can't I continue previous conversations, even from history?"

Sessions live in memory, so a hub restart drops them. The CLI's own thread does
NOT -- `codex exec resume <id>` and `claude --resume <id>` both pick it back up
with the model's full context. agentic_history.record_turn() has always accepted
a native_session_id and stored it per turn... and app.py never passed one. Every
stored turn had None, so there was nothing to resume from and "continue" could
only ever offer to start over.
"""
import os
import shutil
import tempfile

import pytest

import agentic_chat as ac
import agentic_history as ah
import app


@pytest.fixture
def proj():
    d = tempfile.mkdtemp(prefix="hubres-")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def client():
    app.app.config["TESTING"] = True
    with app.app.test_client() as c:
        yield c


def _auth():
    import config
    return {"X-Free-LLM-Hub": "dashboard",
            "X-Free-LLM-Hub-Token": config.ensure_control_token()}


# --------------------------------------------------------------------------- #
# The missing link: the id has to be RECORDED before it can be resumed
# --------------------------------------------------------------------------- #

def test_get_session_exposes_the_native_thread_id(proj):
    """app.py cannot record what get_session does not return. It used to expose
    only a has_native_session boolean, which is unusable as a resume handle."""
    sid = ac.resume_session("codex", proj, "THREAD-1")
    try:
        row = ac.get_session(sid)
        assert row["native_session_id"] == "THREAD-1"
        assert row["has_native_session"] is True
    finally:
        ac.end_session(sid)


def test_history_keeps_the_native_id_per_turn(proj):
    ah.record_turn("sess-native-1", "codex", proj, "user", "hi")
    ah.record_turn("sess-native-1", "codex", proj, "agent", "done",
                   native_session_id="THREAD-9")
    try:
        conv = ah.get_conversation("sess-native-1")
        ids = [t.get("native_session_id") for t in conv["turns"]]
        assert "THREAD-9" in ids
    finally:
        ah.delete_conversation("sess-native-1")


# --------------------------------------------------------------------------- #
# Rebuilding a live session pointed at the real thread
# --------------------------------------------------------------------------- #

def test_resume_session_seeds_the_thread(proj):
    sid = ac.resume_session("codex", proj, "THREAD-ABC")
    try:
        assert ac.get_session(sid)["native_session_id"] == "THREAD-ABC"
    finally:
        ac.end_session(sid)


def test_resume_session_reuses_the_original_id(proj):
    """So the transcript keeps accumulating into the SAME conversation instead
    of forking a second copy of it."""
    sid = ac.resume_session("codex", proj, "T", session_id="keep-this-id")
    try:
        assert sid == "keep-this-id"
        assert ac.get_session("keep-this-id") is not None
    finally:
        ac.end_session(sid)


def test_a_hostile_session_id_is_not_reused(proj):
    """The id becomes a FILENAME in agentic_history, so anything outside the
    whitelist must fall back to the generated one rather than be trusted."""
    sid = ac.resume_session("codex", proj, "T", session_id="../../etc/passwd")
    try:
        assert sid != "../../etc/passwd"
        assert "/" not in sid and "\\" not in sid
    finally:
        ac.end_session(sid)


def test_a_resumed_turn_does_not_repeat_the_standing_notice(proj):
    """It carries a thread, so `resume` already has the earlier turns."""
    sid = ac.resume_session("codex", proj, "THREAD-R")
    try:
        with ac._REGISTRY_LOCK:
            sess = ac._REGISTRY[sid]
        argv = ac._build_argv_codex(sess, "codex", "next step")
        assert "resume" in argv and "THREAD-R" in argv
    finally:
        ac.end_session(sid)


# --------------------------------------------------------------------------- #
# The route the History list calls
# --------------------------------------------------------------------------- #

def test_resume_route_rebuilds_from_stored_history(client, proj):
    ah.record_turn("resume-route-1", "codex", proj, "user", "build it")
    ah.record_turn("resume-route-1", "codex", proj, "agent", "built",
                   native_session_id="THREAD-ROUTE")
    try:
        r = client.post("/api/agent/sessions/resume-route-1/resume",
                        json={}, headers=_auth())
        if r.status_code == 403:
            pytest.skip("agentic chat master flag is off on this machine")
        assert r.status_code == 200, r.get_data(as_text=True)
        body = r.get_json()
        assert body["session_id"] == "resume-route-1"
        assert body["native_session_id"] == "THREAD-ROUTE"
        assert body["resumed_thread"] is True
        ac.end_session("resume-route-1")
    finally:
        ah.delete_conversation("resume-route-1")


def test_resume_is_honest_when_there_is_no_saved_thread(client, proj):
    """Conversations recorded before this fix have no id. Reopening them is
    still useful — the files are there — but the model does NOT have the
    conversation, and the response says so instead of implying otherwise."""
    ah.record_turn("resume-route-2", "codex", proj, "user", "hi")
    ah.record_turn("resume-route-2", "codex", proj, "agent", "ok")
    try:
        r = client.post("/api/agent/sessions/resume-route-2/resume",
                        json={}, headers=_auth())
        if r.status_code == 403:
            pytest.skip("agentic chat master flag is off on this machine")
        assert r.status_code == 200
        assert r.get_json()["resumed_thread"] is False
        ac.end_session("resume-route-2")
    finally:
        ah.delete_conversation("resume-route-2")


def test_resume_of_an_unknown_conversation_404s(client):
    r = client.post("/api/agent/sessions/definitely-not-here/resume",
                    json={}, headers=_auth())
    assert r.status_code in (403, 404)


def test_resume_refuses_a_folder_that_is_gone(client):
    gone = os.path.join(tempfile.gettempdir(), "hub-deleted-project-xyz")
    ah.record_turn("resume-route-3", "codex", gone, "user", "hi")
    try:
        r = client.post("/api/agent/sessions/resume-route-3/resume",
                        json={}, headers=_auth())
        if r.status_code == 403:
            pytest.skip("agentic chat master flag is off on this machine")
        assert r.status_code == 400
        assert r.get_json()["code"] == "folder_gone"
    finally:
        ah.delete_conversation("resume-route-3")
