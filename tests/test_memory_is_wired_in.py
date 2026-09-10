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


# --------------------------------------------------------------------------- #
# The memory is READ BACK, not only written
# --------------------------------------------------------------------------- #

def test_the_memory_reaches_the_agent(tmp_path):
    """It was write-only. remember_summary had been filing the compaction recap
    since the memory manager landed and NOTHING ever read it back into a turn,
    so a conversation that had been compacted still forgot everything it had
    done -- the whole complaint the module exists for."""
    sess = AC._Session("claude", str(tmp_path))
    memory.remember_fact(sess.id, "the database is Postgres")
    memory.remember_summary(sess.id, "the schema and the API are done")
    AC.write_task_brief(str(tmp_path), "build a thing",
                        memory_block=AC._memory_block(sess))
    written = (tmp_path / AC.BRIEF_FILENAME).read_text(encoding="utf-8")
    assert "the database is Postgres" in written
    assert "the schema and the API are done" in written


def test_the_agent_is_told_the_file_carries_it():
    """A file nothing points at is a file nothing reads."""
    add = AC._system_prompt_addition("build a landing page", has_brief=True)
    assert AC.BRIEF_FILENAME in add
    assert "established" in add


def test_it_rides_in_the_file_not_the_command_line(tmp_path, monkeypatch):
    """The worst-case turn-1 argv already measures ~8006 chars against cmd.exe's
    ~8191 ceiling, so there is no room in the prompt for two thousand
    characters of recap. The pointer to the file is already being sent."""
    monkeypatch.setattr(AC.vision_status, "status",
                        lambda: {"available": False, "providers": []})
    monkeypatch.setattr(AC, "test_verification_enabled", lambda: True)
    long_bin = r"C:\Users\somewhat-long-username\AppData\Roaming\npm\claude.cmd"
    text = ("build me a landing page website " + "x" * AC._MAX_MESSAGE_CHARS
            )[:AC._MAX_MESSAGE_CHARS]
    for cli, build in (("claude", AC._build_argv),
                       ("codex", AC._build_argv_codex),
                       ("opencode", AC._build_argv_opencode)):
        sess = AC._Session(cli, str(tmp_path))
        sess.native_session_id = None
        memory.remember_summary(sess.id, "y" * memory.MAX_SUMMARY_CHARS)
        for i in range(memory.MAX_FACTS):
            memory.remember_fact(sess.id, "decision %d " % i + "z" * 200)
        argv = build(sess, long_bin, text)
        cost = sum(len(a) + 3 for a in argv)
        assert cost < 8191, "%s turn-1 argv with a full memory is %d chars" % (cli, cost)


def test_a_session_with_nothing_remembered_writes_no_memory_section(tmp_path):
    sess = AC._Session("claude", str(tmp_path))
    assert AC._memory_block(sess) == ""
    AC.write_task_brief(str(tmp_path), "build a landing page website",
                        memory_block="")
    written = (tmp_path / AC.BRIEF_FILENAME).read_text(encoding="utf-8")
    assert "already established" not in written


def test_memory_alone_is_enough_to_write_the_file(tmp_path, monkeypatch):
    """A task with no craft brief still has a conversation to remember. The
    brief is stubbed out because craft matches on every string today -- that is
    a craft decision, not one this path may assume."""
    monkeypatch.setattr(AC.craft, "system_message", lambda text: None)
    sess = AC._Session("claude", str(tmp_path))
    memory.remember_fact(sess.id, "MUST stay on Postgres")
    assert AC.write_task_brief(str(tmp_path), "zzzz",
                               memory_block=AC._memory_block(sess)) is True
    assert "Postgres" in (tmp_path / AC.BRIEF_FILENAME).read_text(encoding="utf-8")


def test_nothing_at_all_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(AC.craft, "system_message", lambda text: None)
    assert AC.write_task_brief(str(tmp_path), "zzzz", memory_block="") is False
    assert not (tmp_path / AC.BRIEF_FILENAME).exists()


def test_a_broken_memory_never_fails_a_build(monkeypatch, tmp_path):
    monkeypatch.setattr(memory, "context_block",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk gone")))
    assert AC._memory_block(AC._Session("claude", str(tmp_path))) == ""


# --------------------------------------------------------------------------- #
# There is something worth remembering in the first place
# --------------------------------------------------------------------------- #

def test_the_first_message_is_remembered_as_the_job():
    body = AGENT[AGENT.index("def send_message_stream(session_id, text):"):]
    body = body[:body.index("\n    def err(")]
    assert "memory.note_turn(session_id) == 1" in body
    assert "The original request" in body


def test_the_job_is_a_fact_so_it_is_never_trimmed():
    """context_block trims the summary and keeps the facts: a half-remembered
    goal is worse than a half-remembered narrative."""
    memory.note_turn("sess-abc")
    memory.remember_fact("sess-abc", "The original request: build a shop in Fez")
    memory.remember_summary("sess-abc", "x" * memory.MAX_SUMMARY_CHARS)
    block = memory.context_block("sess-abc", budget_chars=300)
    assert "build a shop in Fez" in block


def test_claude_restates_too():
    """claude blanks `text` after turn 1, which collapsed its addition to almost
    nothing -- the same "told once" failure codex and opencode had, by a
    different route."""
    body = AGENT[AGENT.index("def _build_argv(sess: _Session"):]
    body = body[:body.index("\ndef _build_argv_codex")]
    assert "_due_for_restate(sess)" in body


def test_the_gateway_tells_memory_when_it_compacts():
    i = APP.index("compacted, did = _compact_to_budget(msgs, payload.get(\"tools\"),\n"
                  "                                            _model_ctx_budget(")
    window = APP[i:i + 1400]
    assert "memory.note_compaction(" in window
    assert "_build_sid()" in window


def test_it_cannot_fail_the_request():
    i = APP.index("memory.note_compaction(")
    assert "except Exception" in APP[i:i + 300]
