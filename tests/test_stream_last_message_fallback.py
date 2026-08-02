"""A real production turn: "hi create best store website... for restaurant
in chefchaouen" (13m12s) came back "claude produced no reply." -- but the
CLI's OWN on-disk transcript showed the model HAD replied: it planned a full
site, tried a Write, hit an unresolvable permission gate (Claude Code
requested interactive approval for the write; stdin is closed so nothing
could ever answer it -- --dangerously-skip-permissions did not prevent the
prompt this time), then asked "Write permission isn't granted yet... Should
I proceed and write the files?" as its LAST streamed line. The underlying
process then never emitted a closing `type:"result"` summary event.

Root cause: send_message_stream only ever set `final_text` from a `_final`
key, which `_claude_stream_events` only produces from a clean terminal
`result` event. Real text streamed via `{"event":"message",...}` was
yielded live for display but never kept as a fallback -- so a turn that
streamed a perfectly good reply and then died without a clean summary
reported "no reply" and threw the reply away entirely.

Knock-on effect, also covered here: because the turn "failed", nothing was
ever saved to sess.native_session_id, so the user's next message ("continue")
had no thread to resume and started a brand-new session with zero context
-- "he got error, and when i asked him to continue he didnt know what the
request was so all work done was lost".

Fix: track the last streamed message text; if the stream ends with no clean
`_final`/`_final_error`, use it as final_text instead of falling through to
"produced no reply" -- so the reply survives, and so does resumability
(native_id is still captured from the stream's own `system` init event
either way).
"""
import threading

import pytest

import agentic_chat as ac


class _FakeSession:
    def __init__(self, cli_id="claude", native_session_id=None, project_dir="."):
        self.cli_id = cli_id
        self.project_dir = project_dir
        self.native_session_id = native_session_id
        self.turn_count = 0
        self.proc = None
        self.proc_lock = threading.Lock()
        self.turn_lock = threading.Lock()
        self.last_interrupted = False
        self.tools_notified = True


@pytest.fixture
def registered_session(monkeypatch):
    monkeypatch.setattr(ac, "master_enabled", lambda: True)
    monkeypatch.setattr(ac, "_should_check_binary_identity", lambda s: False)
    monkeypatch.setattr(ac, "_resolve_bin", lambda cli: "/fake/" + cli)
    monkeypatch.setattr(ac.workspace, "missing_tools_message", lambda d: None)

    def make(cli_id="claude", native_session_id=None):
        sess = _FakeSession(cli_id=cli_id, native_session_id=native_session_id)
        sid = "test-" + cli_id + "-" + str(id(sess))
        ac._REGISTRY[sid] = sess
        return sid, sess
    yield make
    ac._REGISTRY.clear()


class _DiesAfterTextProc:
    """stdout streams a real init line + real assistant text, exactly like a
    genuine claude --output-format stream-json run that then hits something
    (a permission gate it can never resolve non-interactively) and the
    process just... stops, with no closing `type:"result"` line -- the
    measured production shape, not a hypothetical one."""
    def __init__(self, lines):
        self._lines = iter(lines)
        self.stderr = iter(())
        self.returncode = 0
        self.pid = 4321

    @property
    def stdout(self):
        return self

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._lines)  # StopIteration on exhaustion == real EOF

    def wait(self, timeout=None):
        return self.returncode


_STREAM_LINES = [
    '{"type":"system","session_id":"real-thread-abc"}\n',
    ('{"type":"assistant","message":{"role":"assistant","content":'
     '[{"type":"text","text":"Now I will build. Let me write all three files."}]}}\n'),
    # (a denied tool_result would appear here in the real transcript --
    # irrelevant to this parser, which has no case for etype=="user")
    ('{"type":"assistant","message":{"role":"assistant","content":'
     '[{"type":"text","text":"Write permission isn\'t granted yet. '
     'Should I proceed and write the files?"}]}}\n'),
    # process exits here -- NO closing {"type":"result",...} line
]


def test_a_turn_that_streams_text_then_dies_without_a_result_event_is_not_no_reply(
        registered_session, monkeypatch):
    sid, sess = registered_session(cli_id="claude", native_session_id=None)
    monkeypatch.setattr(ac.subprocess, "Popen",
                        lambda argv, **kw: _DiesAfterTextProc(list(_STREAM_LINES)))

    events = list(ac.send_message_stream(sid, "build me a store"))

    errors = [e for e in events if e.get("event") == "error"]
    done = [e for e in events if e.get("event") == "done"]
    assert not errors, "a real streamed reply must not be reported as an error: %r" % errors
    assert done, "expected a done event carrying the last streamed reply"
    assert "Should I proceed and write the files?" in done[0]["text"]


def test_the_recovered_reply_is_the_last_message_not_the_first(
        registered_session, monkeypatch):
    """Multiple assistant text chunks stream before the process dies -- the
    LAST one is the model's actual final word, not an earlier planning
    note."""
    sid, sess = registered_session(cli_id="claude", native_session_id=None)
    monkeypatch.setattr(ac.subprocess, "Popen",
                        lambda argv, **kw: _DiesAfterTextProc(list(_STREAM_LINES)))

    events = list(ac.send_message_stream(sid, "build me a store"))

    done = [e for e in events if e.get("event") == "done"][0]
    assert "Now I will build" not in done["text"]
    assert "Should I proceed" in done["text"]


def test_native_session_id_is_still_saved_so_the_next_turn_can_resume(
        registered_session, monkeypatch):
    """The knock-on bug: without a saved native id, "continue" started a
    brand-new session with zero context instead of resuming the real one."""
    sid, sess = registered_session(cli_id="claude", native_session_id=None)
    monkeypatch.setattr(ac.subprocess, "Popen",
                        lambda argv, **kw: _DiesAfterTextProc(list(_STREAM_LINES)))

    list(ac.send_message_stream(sid, "build me a store"))

    assert sess.native_session_id == "real-thread-abc"


def test_a_clean_final_error_is_not_overridden_by_the_fallback(registered_session, monkeypatch):
    """A REAL terminal error (is_error:true with real text) must still be
    reported as an error -- the fallback only fires when there is NO clean
    terminal event at all, never as a way to paper over a genuine failure."""
    sid, sess = registered_session(cli_id="claude", native_session_id=None)
    lines = [
        '{"type":"system","session_id":"thread-xyz"}\n',
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Trying..."}]}}\n',
        '{"type":"result","is_error":true,"errors":["No conversation found with session ID: thread-xyz"]}\n',
    ]
    monkeypatch.setattr(ac.subprocess, "Popen", lambda argv, **kw: _DiesAfterTextProc(lines))

    events = list(ac.send_message_stream(sid, "continue"))

    errors = [e for e in events if e.get("event") == "error"]
    assert errors, "a genuine terminal error must still surface as an error"
    assert "No conversation found" in errors[0]["detail"]


def test_no_text_at_all_still_reports_no_reply(registered_session, monkeypatch):
    """No regression on the ACTUAL no-reply case: a process that produces
    nothing usable must still say so, not fabricate a reply."""
    sid, sess = registered_session(cli_id="claude", native_session_id=None)
    lines = ['{"type":"system","session_id":"thread-empty"}\n']
    monkeypatch.setattr(ac.subprocess, "Popen", lambda argv, **kw: _DiesAfterTextProc(lines))

    events = list(ac.send_message_stream(sid, "hello"))

    errors = [e for e in events if e.get("event") == "error"]
    assert errors and "no reply" in errors[0]["detail"]
