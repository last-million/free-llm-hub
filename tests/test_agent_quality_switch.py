"""Switch a live agent session between normal and max, mid-conversation.

The start-of-session question was not enough: you only learn a task needs the
strong models once you are already several turns in.

This works precisely because the CLI is re-spawned for every turn -- both
_agentic_env call sites build the child's environment fresh, so ANTHROPIC_MODEL
is decided per turn, not once at session start. A turn already in flight keeps
the mode it began with, because its child process is already launched.
"""
from unittest import mock

import agentic_chat
import config


def _client():
    import app
    app.app.config["TESTING"] = True
    return app.app.test_client()


def _hdrs():
    return {"X-Free-LLM-Hub-Token": config.ensure_control_token(),
            "X-Free-LLM-Hub": "dashboard"}


def _register(quality="normal"):
    sess = agentic_chat._Session("claude", "/tmp/proj", quality=quality)
    with agentic_chat._REGISTRY_LOCK:
        agentic_chat._REGISTRY[sess.id] = sess
    return sess


def test_quality_can_be_changed_on_a_live_session():
    sess = _register("normal")
    try:
        assert agentic_chat.set_quality(sess.id, "max") == "max"
        assert sess.quality == "max"
        assert agentic_chat.set_quality(sess.id, "normal") == "normal"
        assert sess.quality == "normal"
    finally:
        with agentic_chat._REGISTRY_LOCK:
            agentic_chat._REGISTRY.pop(sess.id, None)


def test_an_invalid_value_changes_nothing():
    sess = _register("max")
    try:
        # "swarm" is a real mode now, so it is no longer an invalid value.
        for bad in ("pipeline", "", None, "MAX", 1):
            assert agentic_chat.set_quality(sess.id, bad) is None
        assert sess.quality == "max", "a rejected value must not clear the mode"
    finally:
        with agentic_chat._REGISTRY_LOCK:
            agentic_chat._REGISTRY.pop(sess.id, None)


def test_an_unknown_session_reports_it():
    assert agentic_chat.set_quality("does-not-exist", "max") is None


def test_the_next_turn_launches_with_the_new_mode():
    """The whole point: the change must reach the CLI's environment."""
    sess = _register("normal")
    try:
        agentic_chat.set_quality(sess.id, "max")
        env = {}
        with mock.patch.object(agentic_chat, "_isolated_signed_in", return_value=False), \
                mock.patch.object(agentic_chat, "_hub_base_url", return_value="http://h"):
            agentic_chat._apply_claude_hub_fallback(env, "/cfg", sess.quality)
        assert env["ANTHROPIC_MODEL"] == "best"
    finally:
        with agentic_chat._REGISTRY_LOCK:
            agentic_chat._REGISTRY.pop(sess.id, None)


def test_endpoint_round_trips_and_validates():
    sess = _register("normal")
    c = _client()
    try:
        r = c.post("/api/agent/sessions/%s/quality" % sess.id,
                   json={"quality": "max"}, headers=_hdrs())
        assert r.status_code == 200 and r.get_json()["quality"] == "max"
        assert sess.quality == "max"
        # swarm is accepted now; something genuinely unknown still is not.
        assert c.post("/api/agent/sessions/%s/quality" % sess.id,
                      json={"quality": "swarm"}, headers=_hdrs()).status_code == 200
        assert c.post("/api/agent/sessions/%s/quality" % sess.id,
                      json={"quality": "pipeline"}, headers=_hdrs()).status_code == 400
        assert c.post("/api/agent/sessions/nope/quality",
                      json={"quality": "max"}, headers=_hdrs()).status_code == 404
    finally:
        with agentic_chat._REGISTRY_LOCK:
            agentic_chat._REGISTRY.pop(sess.id, None)


def test_the_switch_is_in_the_session_bar_and_wired_in_scope():
    import io, os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html = io.open(os.path.join(root, "templates", "index.html"), encoding="utf-8").read()
    assert 'id="agent-quality"' in html
    i = html.find('id="agent-quality"')
    assert 'agent-session-bar' in html[max(0, i - 800):i], "must live in the session bar"
    # Defined and called at the SAME indent -- the out-of-scope call is what
    # took the whole dashboard down once already.
    assert "    function initAgentQuality(){" in html
    assert "    initAgentQuality();" in html
    assert "\n  initAgentQuality();" not in html
