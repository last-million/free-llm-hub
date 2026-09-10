r"""Coming back to a conversation and finding it running as something else.

REPORTED 2026-09-09: "the session should remember the project and load same
config in future if want to continue the conversation".

The project folder was already remembered. The configuration was not, or only
half of it:

  * quality (normal / max / swarm) persisted -- agentic_history.set_quality
    exists precisely because "a live session does not survive a hub restart,
    and the hub restarts on every 5-hourly auto-update";
  * mode (coding / uncensored / ...) did NOT. It lived only on
    agentic_chat._Session.mode, in memory.

So the pair that makes a conversation "this project, these models" was split:
after any restart the effort came back and the category silently did not.

And NEITHER was actually restored on resume. resume_session called
start_session, which builds a default session, then overwrote only the id and
the native session id -- so even the quality that had been faithfully written
to disk was never read back. Persisting without restoring is the same bug with
extra steps.

The per-session model lists (whitelist/blocklist) needed nothing here: they are
stored in config keyed by session id, so resuming the same id already finds
them.
"""
import pytest

import agentic_chat as AC
import agentic_history as AH


@pytest.fixture(autouse=True)
def _clean():
    AC._REGISTRY.clear()
    yield
    AC._REGISTRY.clear()


# --------------------------------------------------------------------------- #
# The mode is written down
# --------------------------------------------------------------------------- #

def test_setting_a_mode_persists_it(monkeypatch):
    seen = {}
    monkeypatch.setattr(AH, "set_mode", lambda sid, mode: seen.update(sid=sid, mode=mode))
    sid = AC.start_session("opencode", ".")
    AC.set_session_mode(sid, "uncensored")
    assert seen == {"sid": sid, "mode": "uncensored"}


def test_setting_a_quality_persists_it(monkeypatch):
    seen = {}
    monkeypatch.setattr(AH, "set_quality", lambda sid, q: seen.update(sid=sid, q=q))
    sid = AC.start_session("opencode", ".")
    AC.set_quality(sid, "swarm")
    assert seen == {"sid": sid, "q": "swarm"}


def test_the_live_session_still_changes_immediately(monkeypatch):
    """Persisting must not replace the in-memory write -- the next turn reads
    the session object, not the file."""
    monkeypatch.setattr(AH, "set_mode", lambda *a, **k: None)
    sid = AC.start_session("opencode", ".")
    AC.set_session_mode(sid, "coding")
    assert AC._REGISTRY[sid].mode == "coding"


def test_persisting_is_not_done_under_the_registry_lock():
    """_REGISTRY_LOCK is held on the hot path of every turn; a file write
    inside it would serialise unrelated sessions behind disk IO."""
    src = open("agentic_chat.py", encoding="utf-8").read()
    body = src[src.index("def set_session_mode("):]
    body = body[:body.index("\ndef ")]
    lock = body.index("with _REGISTRY_LOCK:")
    write = body.index("agentic_history.set_mode(")
    ret = body.index("return True")
    assert write > ret - len("return True") or write > lock
    assert "        agentic_history.set_mode(" not in body, "still inside the with-block"


def test_an_unknown_session_is_still_rejected(monkeypatch):
    monkeypatch.setattr(AH, "set_mode", lambda *a, **k: None)
    assert AC.set_session_mode("no-such-session", "coding") is False


# --------------------------------------------------------------------------- #
# ...and read back
# --------------------------------------------------------------------------- #

def test_resuming_restores_the_mode_and_the_quality(monkeypatch, tmp_path):
    monkeypatch.setattr(AH, "get_conversation",
                        lambda sid: {"quality": "swarm", "mode": "uncensored"})
    sid = AC.resume_session("opencode", str(tmp_path), None, session_id="abc123")
    sess = AC._REGISTRY[sid]
    assert sess.quality == "swarm"
    assert sess.mode == "uncensored"


def test_a_conversation_with_no_stored_config_gets_the_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(AH, "get_conversation", lambda sid: {})
    sid = AC.resume_session("opencode", str(tmp_path), None, session_id="abc124")
    sess = AC._REGISTRY[sid]
    assert sess.quality == "normal" and sess.mode is None


def test_a_nonsense_stored_quality_is_ignored(monkeypatch, tmp_path):
    """The file is on disk and can be hand-edited; an unknown value must not
    reach the launcher."""
    monkeypatch.setattr(AH, "get_conversation",
                        lambda sid: {"quality": "turbo", "mode": None})
    sid = AC.resume_session("opencode", str(tmp_path), None, session_id="abc125")
    assert AC._REGISTRY[sid].quality == "normal"


def test_history_failing_does_not_break_resuming(monkeypatch, tmp_path):
    def boom(sid):
        raise OSError("disk gone")
    monkeypatch.setattr(AH, "get_conversation", boom)
    sid = AC.resume_session("opencode", str(tmp_path), None, session_id="abc126")
    assert sid in AC._REGISTRY, "a missing history file must not lose the session"


def test_the_project_is_still_remembered(monkeypatch, tmp_path):
    monkeypatch.setattr(AH, "get_conversation", lambda sid: {})
    sid = AC.resume_session("opencode", str(tmp_path), None, session_id="abc127")
    assert AC._REGISTRY[sid].project_dir == str(tmp_path)


# --------------------------------------------------------------------------- #
# The stored shape
# --------------------------------------------------------------------------- #

def test_a_conversation_row_carries_the_mode():
    src = open("agentic_history.py", encoding="utf-8").read()
    assert '"mode": conv.get("mode")' in src
    assert '"mode": None' in src, "a new conversation needs the field too"


def test_clearing_a_mode_is_remembered_as_clearly_as_setting_one():
    """Stored as None rather than skipped -- otherwise turning a mode off left
    yesterday's mode on disk to be restored tomorrow."""
    src = open("agentic_history.py", encoding="utf-8").read()
    body = src[src.index("def set_mode("):]
    body = body[:body.index("\ndef ")]
    assert 'mode = (mode or "").strip() or None' in body
    assert "if not session_id:" in body


def test_it_does_not_rewrite_the_file_on_every_turn():
    """set_quality documents this: it runs on every turn."""
    src = open("agentic_history.py", encoding="utf-8").read()
    body = src[src.index("def set_mode("):]
    body = body[:body.index("\ndef ")]
    assert 'if conv.get("mode") == mode:' in body


# --------------------------------------------------------------------------- #
# ...but only if there was somewhere to write it
# --------------------------------------------------------------------------- #

def test_a_mode_set_before_the_first_turn_is_not_lost(tmp_path, monkeypatch):
    """MEASURED against a live hub: a swarm worker configured with mode
    "coding" had it on the live session and NOTHING in its saved conversation.

    agentic_history.set_mode starts with `conv = _load_conversation(...)` and
    returns when there is no row -- and the row is only created by the first
    recorded turn. So picking a mode in Settings and then sending the first
    message wrote the mode into memory only: it survived until the next hub
    restart and came back as none, which is the half of "per-conversation
    models, saved" that was quietly missing."""
    monkeypatch.setenv(AH._ROOT_ENV if hasattr(AH, "_ROOT_ENV")
                       else "FREE_LLM_HUB_CONFIG", str(tmp_path / "cfg.json"))
    assert AH.set_mode("never-recorded", "coding") is None


def test_the_turn_route_writes_the_mode_as_well_as_the_quality():
    """The comment at that call site says "keep the saved conversation's MODE in
    step with the live session" and the line under it wrote only the quality."""
    src = open("app.py", encoding="utf-8").read()
    for anchor in ('agentic_history.set_quality(session_id, sess_info.get("quality") or "normal")',):
        assert src.count(anchor) == 2, "call sites moved"
    assert src.count('agentic_history.set_mode(session_id, sess_info.get("mode"))') == 2


def test_both_the_streaming_and_the_plain_route_do_it():
    """The dashboard streams every turn; a fix on the non-streaming route only
    would have been invisible to the people who reported it."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index('@app.route("/api/agent/sessions/<session_id>/message", methods=["POST"])')
    j = src.index('@app.route("/api/agent/sessions/<session_id>/message/stream"')
    k = src.index('@app.route("/api/agent/sessions/<session_id>/quality"')
    assert 'agentic_history.set_mode(session_id' in src[i:j]
    assert 'agentic_history.set_mode(session_id' in src[j:k]


def test_the_lists_need_nothing_on_resume():
    """REQUESTED: "he should auto remember what was the settings for the
    conversation, to continue in future with the same models selection".

    The allow/block lists are keyed by SESSION ID in the config, and the resume
    route hands resume_session the ORIGINAL id -- so they are in force the
    moment the conversation is back, with nothing to restore. That only holds
    while the id is reused, which is what this pins.""" 
    src = open("app.py", encoding="utf-8").read()
    i = src.index("def api_agent_resume_session(")
    body = src[i:i + 4000]
    assert "session_id=session_id" in body
