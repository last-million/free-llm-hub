r"""The memory manager is only worth having if the hub actually uses it.

A module nothing calls is a module that does nothing, and every gap it was
built to close is at a specific call site:

  * the compaction recap was cached in RAM (`_summary_cache`, 64 entries) and
    lost on the 5-hourly auto-update restart;
  * standing instructions shipped on turn 1 and never again -- for codex and
    opencode literally `addition = ""` from turn 2 -- so a long session was
    following rules it was told about once, before compaction ate the message
    carrying them;
  * `_Session.turn_count` resets to 0 on resume, so nothing could ask how long
    a conversation had been going.

These assert the wiring, not the module: test_memory_manager covers the
behaviour on its own.
"""
import io

import agentic_chat as AC
import memory
import pytest


APP = io.open("app.py", encoding="utf-8").read()
AGENT = io.open("agentic_chat.py", encoding="utf-8").read()


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(memory._ROOT_ENV, str(tmp_path))
    yield


# --------------------------------------------------------------------------- #
# The recap outlives the process
# --------------------------------------------------------------------------- #

def test_the_compaction_recap_is_persisted():
    body = APP[APP.index("def _summarize_worker("):]
    body = body[:body.index("\ndef ")]
    assert "memory.remember_summary(sid, out)" in body


def test_the_conversation_is_resolved_on_the_request_thread():
    """_build_sid reads the request context; the summariser runs on a thread
    that does not have one, so resolving it inside the worker would always
    yield None and quietly persist nothing."""
    i = APP.index("threading.Thread(target=_summarize_worker")
    before = APP[max(0, i - 500):i]
    assert "_build_sid()" in before
    assert "args=(key, text, sid)" in APP


def test_a_gateway_request_with_no_session_still_works():
    """Most traffic has no agent session at all; it must keep using the cache
    and not try to file a recap against nothing."""
    body = APP[APP.index("def _summarize_worker("):]
    body = body[:body.index("\ndef ")]
    assert "if sid:" in body


def test_the_ram_cache_is_still_there():
    """The durable copy is an addition, not a replacement: the cache is what
    makes a recap free on the second hop of the same request."""
    body = APP[APP.index("def _summarize_worker("):]
    body = body[:body.index("\ndef ")]
    assert "_summary_cache[key] = out" in body


# --------------------------------------------------------------------------- #
# The rules are said again
# --------------------------------------------------------------------------- #

def test_the_turn_one_only_gate_is_gone():
    """`if sess.native_session_id: addition = ""` was the whole reason an agent
    heard its standing instructions once."""
    assert 'if sess.native_session_id and not _due_for_restate(sess):' in AGENT
    assert AGENT.count("if sess.native_session_id and not _due_for_restate(sess):") == 2


def test_restating_is_scheduled_not_every_turn():
    """A repeated notice reads as a repeated user instruction -- the codex
    failure recorded in _build_argv_codex."""
    body = AGENT[AGENT.index("def _due_for_restate("):]
    body = body[:body.index("\ndef ")]
    assert "memory.should_restate_rules" in body
    assert "memory.mark_rules_restated" in body


def test_a_session_with_no_id_is_not_due():
    class _S:
        id = None
    assert AC._due_for_restate(_S()) is False


def test_it_becomes_due_and_then_resets():
    class _S:
        id = "sess-abc"
    sess = _S()
    for _ in range(memory.RESTATE_EVERY - 1):
        memory.note_turn(sess.id)
    assert AC._due_for_restate(sess) is False
    memory.note_turn(sess.id)
    assert AC._due_for_restate(sess) is True
    assert AC._due_for_restate(sess) is False, "did not reset after restating"


def test_a_broken_memory_never_fails_a_turn(monkeypatch):
    class _S:
        id = "sess-abc"
    monkeypatch.setattr(memory, "should_restate_rules",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk gone")))
    assert AC._due_for_restate(_S()) is False


# --------------------------------------------------------------------------- #
# Turns are counted where every turn goes through
# --------------------------------------------------------------------------- #

def test_every_turn_is_counted():
    body = AGENT[AGENT.index("def send_message_stream(session_id, text):"):]
    body = body[:body.index("\n    def err(")]
    assert "memory.note_turn(session_id)" in body


def test_counting_cannot_fail_a_turn():
    body = AGENT[AGENT.index("def send_message_stream(session_id, text):"):]
    body = body[:body.index("\n    def err(")]
    assert "except Exception" in body


def test_the_counter_survives_a_resumed_session():
    """The point of not using _Session.turn_count, which resets to 0."""
    memory.note_turn("sess-abc")
    memory.note_turn("sess-abc")
    assert memory.get("sess-abc")["turns"] == 2
