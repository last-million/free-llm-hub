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
