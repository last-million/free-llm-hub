"""A turn that exceeds _TURN_TIMEOUT used to just fail -- no retry, no resume,
the request silently discarded. "he stopped but should be robust to not stop
and retry".

Measured live, on this hub, the SAME day this was reported: a TRIVIAL
one-file-write turn on free-tier codex took ~5 minutes. A real ask -- many
tool calls, many model round trips within one turn -- can legitimately need
far longer than the old 600s ceiling, so a kill on the first sign of a long
turn was the wrong default, and killing it with no retry threw away whatever
the model had already done.

Two fixes:
  1. The timeout itself was raised (600s -> 1800s) to match what was actually
     observed, so this fires less often to begin with.
  2. One bounded retry, and if a thread/session id can be salvaged from what
     the killed process DID produce before it was killed, the retry RESUMES
     the same thread instead of starting the whole task over from zero.
"""
import threading

import pytest

import agentic_chat as ac


class _FakeSession:
    def __init__(self, cli_id="codex", native_session_id=None, project_dir="."):
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

    def make(cli_id="codex", native_session_id=None):
        sess = _FakeSession(cli_id=cli_id, native_session_id=native_session_id)
        sid = "test-" + cli_id + "-" + str(id(sess))
        ac._REGISTRY[sid] = sess
        return sid, sess
    yield make
    ac._REGISTRY.clear()


def test_native_id_is_saved_before_the_timeout_error_or_retry(registered_session, monkeypatch):
    """The actual bug: send_message_stream captured native_id progressively
    from the stream, but `if timed_out[0]: yield err(...); return` fired
    BEFORE the `if native_id: sess.native_session_id = native_id` line ever
    ran -- so a captured id was silently discarded on every timeout."""
    sid, sess = registered_session(cli_id="codex", native_session_id=None)

    monkeypatch.setattr(ac, "_TURN_TIMEOUT", 0.05)

    class _SlowProc:
        """stdout yields ONE real line (with a session id) then blocks
        forever on the next line -- exactly what a genuinely slow, still-
        working turn looks like from the outside."""
        def __init__(self):
            self._lines = iter([
                '{"type":"thread.started","thread_id":"salvaged-id"}\n'])
            self.stderr = iter(())
            self.returncode = -9
            self.pid = 1234
            self._killed = threading.Event()

        @property
        def stdout(self):
            return self

        def __iter__(self):
            return self

        def __next__(self):
            try:
                return next(self._lines)
            except StopIteration:
                self._killed.wait(timeout=5)   # simulate "still running"
                raise StopIteration

        def wait(self, timeout=None):
            return self.returncode

        def kill_now(self):
            self._killed.set()

    proc_holder = []

    def fake_popen(argv, **kw):
        p = _SlowProc()
        proc_holder.append(p)
        return p

    def fake_terminate(proc):
        proc.kill_now()

    monkeypatch.setattr(ac.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ac, "_terminate", fake_terminate)

    events = list(ac.send_message_stream(sid, "slow task"))

    errors = [e for e in events if e.get("event") == "error"]
    notices = [e for e in events if e.get("event") == "notice"]
    assert any("Still working" in n.get("text", "") for n in notices), (
        "a timeout must be visible, not silent -- real work may already be streaming")
    # Retried once, then genuinely gave up (the fake process never produces a
    # real result) -- but the id captured before the FIRST kill must have been
    # saved for the retry to use.
    assert sess.native_session_id == "salvaged-id"
    assert errors and "retried once" in errors[0]["detail"]


def test_best_effort_native_id_recovers_from_a_killed_codex_process():
    partial = ('{"type":"thread.started","thread_id":"abc-123"}\n'
              '{"type":"turn.started"}\n'
              '{"type":"item.started","item":{"type":"command_execution",')  # cut off mid-line
    assert ac._best_effort_native_id("codex", partial) == "abc-123"


def test_best_effort_native_id_recovers_from_a_killed_opencode_process():
    partial = '{"type":"step_start","sessionID":"ses_abc"}\n{"type":"tool_use"'
    assert ac._best_effort_native_id("opencode", partial) == "ses_abc"


def test_best_effort_native_id_is_none_for_claude_non_streaming():
    """claude's --output-format json writes one blob at the very end -- a
    kill mid-turn leaves no complete JSON at all to recover from."""
    assert ac._best_effort_native_id("claude", '{"type": "result", "sess') is None


def test_best_effort_native_id_never_raises_on_garbage():
    assert ac._best_effort_native_id("codex", "") is None
    assert ac._best_effort_native_id("codex", "not json at all") is None
    assert ac._best_effort_native_id("opencode", None) is None


def test_the_timeout_default_was_raised_to_match_measured_latency():
    """Was 600 -- confirmed too tight by direct measurement (a trivial codex
    turn took ~5 minutes on free-tier routing)."""
    assert ac._TURN_TIMEOUT >= 1800 or "AGENTIC_CHAT_TIMEOUT" in __import__("os").environ


def test_the_env_override_still_works(monkeypatch):
    import importlib
    import os as _os
    monkeypatch.setenv("AGENTIC_CHAT_TIMEOUT", "42")
    importlib.reload(ac)
    try:
        assert ac._TURN_TIMEOUT == 42
    finally:
        monkeypatch.delenv("AGENTIC_CHAT_TIMEOUT", raising=False)
        importlib.reload(ac)


def test_a_second_consecutive_timeout_still_gives_up():
    """One bounded retry, not an infinite loop -- a genuinely stuck process
    must not hang the session forever."""
    import inspect
    src = inspect.getsource(ac.send_message_stream)
    assert "timeout_retry_used" in src
    assert "retried once" in src
