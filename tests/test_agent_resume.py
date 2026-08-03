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
import subprocess
import sys
import tempfile

import pytest

import agentic_chat as ac
import agentic_history as ah
import app


HTML = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "templates", "index.html"), encoding="utf-8").read()


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


def test_resuming_a_session_that_is_genuinely_still_running_does_not_clobber_it(proj):
    """THE BUG (found live): every /agent/<id> page load calls resume_session()
    unconditionally, including for a session that is genuinely still mid-turn.
    Without this guard, the pop/rename/reinsert below silently replaces the
    live Session -- proc handle and all -- with a fresh, proc-less stand-in
    under the same id: currently_running then reads False for a task that is
    still actually running, and sending a new message to that id starts a
    SECOND process on top of the first, in the same project folder."""
    sid = ac.start_session("codex", proj)
    real_proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        with ac._REGISTRY_LOCK:
            live_sess = ac._REGISTRY[sid]
            live_sess.proc = real_proc
        got = ac.resume_session("codex", proj, "SOME-THREAD", session_id=sid)
        assert got == sid
        with ac._REGISTRY_LOCK:
            assert ac._REGISTRY[sid] is live_sess, \
                "a live session must be handed back as-is, not swapped for a decoy"
            assert ac._REGISTRY[sid].proc is real_proc
    finally:
        real_proc.terminate()
        real_proc.wait(timeout=5)
        ac.end_session(sid)


def test_resuming_a_session_that_has_actually_finished_still_gets_a_fresh_thread(proj):
    """Contrast case: once the real process has exited, resume must go back to
    building a normal stand-in pointed at the saved thread id -- the guard
    above must not accidentally block the legitimate restart-recovery path."""
    sid = ac.start_session("codex", proj)
    real_proc = subprocess.Popen([sys.executable, "-c", "pass"])
    real_proc.wait(timeout=5)
    try:
        with ac._REGISTRY_LOCK:
            live_sess = ac._REGISTRY[sid]
            live_sess.proc = real_proc
        got = ac.resume_session("codex", proj, "SOME-THREAD", session_id=sid)
        assert got == sid
        with ac._REGISTRY_LOCK:
            assert ac._REGISTRY[sid] is not live_sess
            assert ac._REGISTRY[sid].native_session_id == "SOME-THREAD"
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


def test_resume_reports_the_real_persisted_turn_count_not_zero(client, proj):
    """THE BUG (found live): resume_session() always hands back a fresh Session
    object -- turn_count starts at 0 and only increments as turns play through
    THAT object. The route reported this raw in-memory value, so every resume
    (after a restart, or "Continue" from History) said turn_count: 0 even with
    a real transcript on disk, and the frontend's `if (!r.turn_count) return`
    gate never loaded it -- reopening a finished, multi-turn session showed
    the same empty placeholder as one that had never run anything."""
    ah.record_turn("resume-route-count", "codex", proj, "user", "build it")
    ah.record_turn("resume-route-count", "codex", proj, "agent", "built",
                   native_session_id="THREAD-COUNT")
    try:
        r = client.post("/api/agent/sessions/resume-route-count/resume",
                        json={}, headers=_auth())
        if r.status_code == 403:
            pytest.skip("agentic chat master flag is off on this machine")
        assert r.status_code == 200, r.get_data(as_text=True)
        assert r.get_json()["turn_count"] == 2
        ac.end_session("resume-route-count")
    finally:
        ah.delete_conversation("resume-route-count")


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


# --------------------------------------------------------------------------- #
# The frontend half: reopening a session mid-turn must not look abandoned.
#
# THE BUG (reported live): a long first turn was still genuinely running
# (real work, confirmed via the CLI's own on-disk rollout -- not stuck) but
# reopening its /agent/<id> URL showed the plain "Send a message to start
# working" placeholder, identical to a session that never ran anything.
# get_session()/the /resume route already returned currently_running; the
# frontend just never read it -- it only branched on turn_count, which stays
# 0 until a turn actually FINISHES, so the turn most likely to look
# abandoned (a long one, already many minutes in) was exactly the one this
# hit hardest.
# --------------------------------------------------------------------------- #

def _fn_body(marker, end_marker):
    start = HTML.index(marker)
    return HTML[start:HTML.index(end_marker, start)]


def test_resume_branches_on_currently_running_before_the_turn_count_check():
    fn = _fn_body("window.cxResumeAgentSession = function(r){", "};")
    running_idx = fn.index("r.currently_running")
    turn_count_idx = fn.index("r.turn_count")
    assert running_idx < turn_count_idx, \
        "currently_running must be checked before the turn_count early-return"


def test_reconnecting_mid_turn_does_not_show_the_empty_placeholder():
    fn = _fn_body("function showReconnectedStillWorking(sid){", "\n    }")
    assert "Still working" in fn
    assert "SPIN_SVG" in fn


def test_reconnecting_mid_turn_locks_the_ui_like_a_real_in_flight_turn():
    """Send disabled, Stop enabled -- the same doStop() already works against
    any session_id, so Stop must actually be clickable here, not just look
    like it is."""
    fn = _fn_body("function showReconnectedStillWorking(sid){", "\n    }")
    assert "setBusy(true)" in fn


def test_the_reconnect_poll_loads_real_history_once_the_turn_actually_ends():
    fn = _fn_body("function showReconnectedStillWorking(sid){", "\n    }")
    assert "currently_running" in fn
    assert "loadFullHistory(sid)" in fn
    assert "setBusy(false)" in fn


def test_the_reconnect_poll_stops_if_the_user_moves_to_a_different_session():
    """Without this, an abandoned poll from a session the user has since
    navigated away from keeps firing forever and can stomp the NEW session's
    UI state with a stale currently_running check."""
    fn = _fn_body("function showReconnectedStillWorking(sid){", "\n    }")
    assert "sessionId !== sid" in fn
    assert "clearInterval(poll)" in fn


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
