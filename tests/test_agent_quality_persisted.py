"""The mode you picked survives — per conversation, across a hub restart.

REPORTED 2026-08-30: "in new build project i selected swarm agent mode and in
top i see just normal selected". Two separate defects behind that one sentence:

1. The dropdown never reflected the answer given at session start. Fixed in the
   template (three switches now, and doStart feeds r.quality back into them);
   test_agent_quality_switch.py covers the control itself.

2. Nothing on DISK remembered the mode. agentic_chat._Session held it, but that
   registry is in-memory by design (see its module docstring) and the hub
   restarts every 5h to auto-update. So Continue tomorrow silently resumed a
   Swarm build on Normal. That is what this file is about.

Storage isolation comes from the root conftest (FREE_LLM_HUB_CONFIG points at a
tmp state dir), same as every other agentic_history test.
"""
import uuid
from unittest import mock

import agentic_chat
import agentic_history as ah
import config


def _client():
    import app
    app.app.config["TESTING"] = True
    return app.app.test_client()


def _hdrs():
    return {"X-Free-LLM-Hub-Token": config.ensure_control_token(),
            "X-Free-LLM-Hub": "dashboard"}


def _row(session_id):
    for r in ah.list_conversations(limit=200):
        if r["session_id"] == session_id:
            return r
    raise AssertionError("conversation %r not in the index" % session_id)


def _conversation(label, quality=None, session_id=None):
    """A brand-new conversation, with an id unique to this RUN.

    The root conftest points FREE_LLM_HUB_CONFIG at a FIXED tmp path shared by
    every pytest run (see its comment about pytest pruning basetemp), so a fixed
    id here would be read back from the last run's file -- which is exactly how
    the 'defaults to normal' case first failed against a record written before
    this field existed."""
    session_id = session_id or "qp-%s-%s" % (label, uuid.uuid4().hex[:8])
    ah.record_turn(session_id, "claude", "/tmp/proj", "user", "build me a site")
    if quality:
        ah.set_quality(session_id, quality)
    return session_id


# --------------------------------------------------------------------------- #
# The storage layer
# --------------------------------------------------------------------------- #

def test_a_new_conversation_defaults_to_normal():
    sid = _conversation("default")
    assert _row(sid)["quality"] == "normal"
    assert ah.get_conversation(sid).get("quality") == "normal"


def test_the_mode_is_written_to_the_conversation():
    sid = _conversation("write")
    assert ah.set_quality(sid, "swarm") == "swarm"
    assert ah.get_conversation(sid)["quality"] == "swarm"


def test_the_mode_shows_on_the_history_row():
    """continueConversation reads conv.quality off the list row, so the index
    has to carry it, not only the conversation file."""
    sid = _conversation("row", "max")
    assert _row(sid)["quality"] == "max"


def test_two_conversations_keep_their_own_modes():
    """'save for each conversation which one was enabled' -- per conversation,
    not one global setting."""
    a = _conversation("a", "swarm")
    b = _conversation("b", "normal")
    assert _row(a)["quality"] == "swarm"
    assert _row(b)["quality"] == "normal"


def test_an_unknown_mode_is_refused():
    sid = _conversation("bogus", "max")
    assert ah.set_quality(sid, "ultra") is None
    assert ah.get_conversation(sid)["quality"] == "max"


def test_an_unknown_conversation_is_not_created():
    sid = "qp-nothing-here-%s" % uuid.uuid4().hex[:8]
    assert ah.set_quality(sid, "swarm") is None
    assert ah.get_conversation(sid) is None


def test_setting_the_same_mode_does_not_rewrite_the_file():
    """This runs on every single turn (app.py syncs it next to record_turn), so
    the unchanged case must not cost a load+save of a growing transcript."""
    sid = _conversation("noop", "swarm")
    with mock.patch.object(ah, "_save_conversation") as save:
        assert ah.set_quality(sid, "swarm") == "swarm"
    assert not save.called


# --------------------------------------------------------------------------- #
# The wiring: the live session and the stored conversation stay in step
# --------------------------------------------------------------------------- #

def test_changing_the_mode_mid_conversation_persists_it():
    sess = agentic_chat._Session("claude", "/tmp/proj")
    with agentic_chat._REGISTRY_LOCK:
        agentic_chat._REGISTRY[sess.id] = sess
    try:
        _conversation("live", session_id=sess.id)
        r = _client().post("/api/agent/sessions/%s/quality" % sess.id,
                           json={"quality": "swarm"}, headers=_hdrs())
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["quality"] == "swarm"
        # the live session AND the thing that outlives it
        assert sess.quality == "swarm"
        assert _row(sess.id)["quality"] == "swarm"
    finally:
        with agentic_chat._REGISTRY_LOCK:
            agentic_chat._REGISTRY.pop(sess.id, None)


def _resume(stored_id):
    """POST the resume route with a stand-in rebuilt session, returning it and
    the response. resume_session is patched because the real one launches a CLI
    subprocess; the route's own restore step is what is under test."""
    rebuilt = agentic_chat._Session("claude", "/tmp/proj")
    with agentic_chat._REGISTRY_LOCK:
        agentic_chat._REGISTRY[rebuilt.id] = rebuilt
    try:
        with mock.patch.object(agentic_chat, "resume_session",
                               return_value=rebuilt.id),                 mock.patch("app.os.path.isdir", return_value=True):
            r = _client().post("/api/agent/sessions/%s/resume" % stored_id,
                               json={}, headers=_hdrs())
        return rebuilt, r
    finally:
        with agentic_chat._REGISTRY_LOCK:
            agentic_chat._REGISTRY.pop(rebuilt.id, None)


def test_resume_puts_the_rebuilt_session_back_in_the_saved_mode():
    """The actual bug: the hub restarts, Continue rebuilds a session pointed at
    the CLI's own thread -- a BRAND NEW _Session, which starts on normal. The
    conversation knew better."""
    rebuilt, r = _resume(_conversation("resume", "swarm"))
    assert r.status_code == 200, r.get_json()
    assert rebuilt.quality == "swarm"
    assert r.get_json().get("quality") == "swarm"


def test_resuming_a_normal_conversation_stays_normal():
    rebuilt, r = _resume(_conversation("resume-normal"))
    assert r.status_code == 200, r.get_json()
    assert rebuilt.quality == "normal"


# --------------------------------------------------------------------------- #
# The control itself: three switches, exactly one live
# --------------------------------------------------------------------------- #

def _template():
    with open("templates/index.html", encoding="utf-8") as f:
        return f.read()


def test_the_control_is_switches_not_a_dropdown():
    """Asked for explicitly: 'i dont want it dropdown but as 3 switchers and
    only one that can be enabled'. Radios ARE that -- the browser enforces the
    mutual exclusion itself rather than us policing it in JS."""
    html = _template()
    assert '<select id="agent-quality"' not in html
    assert 'id="agent-quality"' in html and 'role="radiogroup"' in html
    for value in ("normal", "max", "swarm"):
        assert 'name="agent-quality" value="%s"' % value in html


def test_the_session_start_answer_is_shown_on_the_switch():
    """The reported symptom -- pick Swarm, the control still reads Normal."""
    html = _template()
    assert "setQuality(r.quality || quality)" in html


def test_continue_restores_the_saved_mode_in_the_ui():
    html = _template()
    assert html.count("setQuality(r.quality || conv.quality || 'normal')") == 2
