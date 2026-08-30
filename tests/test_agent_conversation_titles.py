"""Agent conversations get a real name, not a timestamp.

The history list only ever showed project_dir -- 'project-20260830-205446',
which says WHEN you started something and nothing about what it was. Quick chat
already titles its rows from the first user message (quick_history._title_from);
this gives agent conversations the same treatment, plus a rename for when the
guess is wrong.

Storage isolation comes from the root conftest (it repoints FREE_LLM_HUB_CONFIG
at a tmp state dir), so these write to a throwaway history like every other
agentic_history test. Session ids are unique per test to avoid cross-talk.
"""
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


def test_title_comes_from_the_first_user_message():
    ah.record_turn("t-title-1", "claude", "/x/project-20260830-205446",
                   "user", "build me a landing page for a bakery")
    assert _row("t-title-1")["title"] == "build me a landing page for a bakery"


def test_a_long_first_message_is_truncated_not_dumped():
    ah.record_turn("t-title-2", "claude", "/x", "user", "x" * 400)
    title = _row("t-title-2")["title"]
    assert len(title) <= ah.MAX_TITLE_CHARS + 1      # +1 for the ellipsis
    assert title.endswith("…")


def test_whitespace_is_collapsed():
    ah.record_turn("t-title-3", "claude", "/x", "user", "  fix   the\n\n login   bug ")
    assert _row("t-title-3")["title"] == "fix the login bug"


def test_the_title_is_set_once_and_later_turns_never_rewrite_it():
    """A long conversation must keep the name of what it was actually for."""
    ah.record_turn("t-title-4", "claude", "/x", "user", "first thing I asked")
    ah.record_turn("t-title-4", "claude", "/x", "agent", "sure, doing that")
    ah.record_turn("t-title-4", "claude", "/x", "user", "now something different")
    assert _row("t-title-4")["title"] == "first thing I asked"


def test_an_assistant_first_turn_does_not_name_the_conversation():
    """The first thing said BY the agent is a reply, not the subject."""
    ah.record_turn("t-title-5", "claude", "/x", "agent", "Hello! How can I help?")
    assert _row("t-title-5")["title"] == ah.DEFAULT_TITLE


def test_an_empty_message_falls_back_to_the_default():
    ah.record_turn("t-title-6", "claude", "/x", "user", "   ")
    assert _row("t-title-6")["title"] == ah.DEFAULT_TITLE


def test_rename_overrides_the_auto_title():
    ah.record_turn("t-title-7", "claude", "/x", "user", "some vague opening line")
    assert ah.set_title("t-title-7", "Bakery site redesign") == "Bakery site redesign"
    assert _row("t-title-7")["title"] == "Bakery site redesign"
    # ...and a later turn must not clobber the name the user chose.
    ah.record_turn("t-title-7", "claude", "/x", "user", "another message")
    assert _row("t-title-7")["title"] == "Bakery site redesign"


def test_renaming_to_blank_resets_rather_than_leaving_it_unlabelled():
    ah.record_turn("t-title-8", "claude", "/x", "user", "hello there")
    assert ah.set_title("t-title-8", "   ") == ah.DEFAULT_TITLE


def test_renaming_an_unknown_conversation_reports_it():
    assert ah.set_title("t-title-does-not-exist", "x") is None


def test_rename_endpoint_validates_and_404s():
    c = _client()
    assert c.post("/api/agent/history/whatever/title", json={},
                  headers=_hdrs()).status_code == 400
    assert c.post("/api/agent/history/whatever/title", json={"title": 5},
                  headers=_hdrs()).status_code == 400
    assert c.post("/api/agent/history/t-title-nope/title", json={"title": "x"},
                  headers=_hdrs()).status_code == 404


def test_rename_endpoint_round_trips():
    ah.record_turn("t-title-9", "claude", "/x", "user", "original opening line")
    r = _client().post("/api/agent/history/t-title-9/title",
                       json={"title": "  Renamed  via   API "}, headers=_hdrs())
    assert r.status_code == 200
    assert r.get_json()["title"] == "Renamed via API"
    assert _row("t-title-9")["title"] == "Renamed via API"
