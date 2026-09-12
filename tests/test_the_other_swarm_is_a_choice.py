r"""Two different things were both called "Swarm", and one of them was invisible.

`Swarm` in the quality row fans ONE turn across several models and keeps the
best answer: one agent, several opinions. Multi swarm windows is the other
thing entirely -- several REAL agent sessions, each its own CLI process and
context window, working phases of one job in parallel.

Both were called swarm. One was a radio button in the row where you choose how
to work; the other lived in a tab, and the report was exactly what you would
expect: "I do not see the new mode swarm multi real sessions as a choice".

So it is a choice now, in that row, next to the others -- and the two stopped
sharing a name.

(It was first a BUTTON in that row that switched to the Swarm tab. That drew
the next report -- "it shows a new window and does not stay in the
conversation" -- and it is a radio tier now, run in the conversation; see
test_multi_sessions_stay_in_the_conversation.py. The tests here that named
the button are gone with it.)

Also here, because they landed together and are the same kind of tidying:
  * a conversation's allow/block lists are dropped when the session ends,
    instead of accumulating in the config for the life of the install;
  * the craft brief is written per SESSION, so swarm workers sharing a project
    folder stop overwriting each other's copy of it -- which mattered from the
    moment that file started carrying what a conversation had established.
"""
import os
import tempfile

import pytest

import agentic_chat as AC
import app as A


SRC = open("templates/index.html", encoding="utf-8").read()


# --------------------------------------------------------------------------- #
# The choice
# --------------------------------------------------------------------------- #

def test_the_multi_session_swarm_is_in_the_quality_row():
    row = SRC[SRC.index('id="agent-quality"'):]
    row = row[:row.index("</div>") + 6]
    assert 'value="multi"' in row, "it is not in the row people choose from"


def test_it_says_what_it_is():
    i = SRC.index('value="multi"')
    around = SRC[i - 900:i + 200]
    assert "REAL agent sessions" in around
    assert "own context window" in around


def test_the_two_swarms_no_longer_share_a_name():
    assert "<span>Swarm best-of-N</span>" in SRC
    assert "<span>Swarm</span>" not in SRC


def test_it_is_a_radio_the_server_stores_per_session():
    """The first version was a button that switched tabs, on the grounds that
    it was "not a routing tier the server stores". It is one now: the tier
    is what makes the next message run as a swarm IN the conversation, and
    what brings the conversation back in that tier after a restart."""
    i = SRC.index('value="multi"')
    assert 'type="radio"' in SRC[i - 60:i + 10]
    assert "multi" in AC.QUALITIES


# --------------------------------------------------------------------------- #
# The lists do not outlive the conversation
# --------------------------------------------------------------------------- #

@pytest.fixture
def isolated(tmp_path, monkeypatch):
    import config
    p = tmp_path / "config.json"
    p.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(p))
    monkeypatch.setattr(config, "_config_path", lambda: str(p))
    config.invalidate_settings_cache()
    yield
    config.invalidate_settings_cache()


def test_ending_a_conversation_drops_its_model_rules(isolated):
    A._set_session_model("sess-gone", "some-model", block=True)
    assert A._session_model_rules("sess-gone")["block"]
    assert A._forget_session_models("sess-gone") is True
    assert not A._session_model_rules("sess-gone")["block"]


def test_forgetting_one_leaves_the_others(isolated):
    A._set_session_model("sess-a", "m1", block=True)
    A._set_session_model("sess-b", "m2", block=True)
    A._forget_session_models("sess-a")
    assert A._session_model_rules("sess-b")["block"] == {"m2"}


def test_forgetting_something_that_has_none_is_harmless(isolated):
    assert A._forget_session_models("never-had-any") is False
    assert A._forget_session_models("") is False


def test_it_is_called_when_a_session_ends():
    src = open("app.py", encoding="utf-8").read()
    i = src.index("def api_agent_end_session(")
    body = src[i:i + 1200]
    assert "_forget_session_models(session_id)" in body


def test_a_restart_does_not_forget_them():
    """A session id survives a restart -- the resume route reuses it -- and
    that is exactly what makes "continue with the same models" work."""
    src = open("app.py", encoding="utf-8").read()
    body = src[src.index("def _forget_session_models("):]
    body = body[:body.index(chr(10) + "def _is_model_blocked_by_user")]
    assert "Never on a restart" in body


# --------------------------------------------------------------------------- #
# One brief per session, not per folder
# --------------------------------------------------------------------------- #

def test_two_sessions_in_one_folder_get_their_own_brief():
    a = AC.brief_filename("aaaaaaaaaaaaaaaa")
    b = AC.brief_filename("bbbbbbbbbbbbbbbb")
    assert a != b and a.startswith(".calvoun-brief-")


def test_no_session_still_means_the_shared_name():
    assert AC.brief_filename(None) == AC.BRIEF_FILENAME
    assert AC.brief_filename("") == AC.BRIEF_FILENAME


def test_a_session_id_cannot_name_a_file_outside_the_project():
    assert AC.brief_filename("../../etc/passwd") == AC.BRIEF_FILENAME
    assert AC.brief_filename("a/b") == AC.BRIEF_FILENAME


def test_the_name_stays_short():
    """The pointer to this file rides in argv, and the worst-case turn-1
    command line is already within ~150 characters of cmd.exe's ceiling."""
    name = AC.brief_filename("0123456789abcdef0123456789abcdef")
    assert len(name) <= 32, name


def test_the_pointer_names_the_file_that_was_written():
    written = ".calvoun-brief-abcdef123456.md"
    add = AC._system_prompt_addition("build a landing page", has_brief=written)
    assert written in add


def test_an_older_caller_passing_true_still_works():
    add = AC._system_prompt_addition("build a landing page", has_brief=True)
    assert AC.BRIEF_FILENAME in add


def test_writing_one_returns_its_name(tmp_path):
    out = AC.write_task_brief(str(tmp_path), "build a landing page website",
                              session_id="abc123def456")
    assert out == ".calvoun-brief-abc123def456.md"
    assert (tmp_path / out).exists()


def test_a_stale_brief_from_a_finished_session_is_swept(tmp_path):
    old = tmp_path / ".calvoun-brief-oldsession1.md"
    old.write_text("x", encoding="utf-8")
    ancient = os.path.getmtime(old) - (AC._BRIEF_STALE_AFTER + 60)
    os.utime(old, (ancient, ancient))
    AC.write_task_brief(str(tmp_path), "build a landing page website",
                        session_id="abc123def456")
    assert not old.exists()


def test_a_recent_one_is_left_alone(tmp_path):
    """A session paused overnight has to find its own brief when it comes
    back."""
    other = tmp_path / ".calvoun-brief-stillhere12.md"
    other.write_text("x", encoding="utf-8")
    AC.write_task_brief(str(tmp_path), "build a landing page website",
                        session_id="abc123def456")
    assert other.exists()


def test_the_update_manifest_ignores_them(tmp_path):
    """They are no more part of the install than the shared one was."""
    src = open("app.py", encoding="utf-8").read()
    assert 'fn.startswith(".calvoun-brief-")' in src
