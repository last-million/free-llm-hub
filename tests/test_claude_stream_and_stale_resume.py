"""Claude agent-chat replies were silently dropped and stale sessions never
recovered: "Failed / 2s / claude produced no reply."

Root cause, MEASURED directly against claude-code-cli 2.1.220 with an
isolated, unauthenticated config -- the isolated copy has no login, exactly
because isolation is new (added the same day this was found):

  1. `--resume <id>` against a config that has never heard of <id> comes back
     with NO "result" field at all -- only an "errors" list:

       {"type":"result","subtype":"error_during_execution","is_error":true,
        "errors":["No conversation found with session ID: <id>"], ...}

     `_parse_claude_json`/`_claude_stream_events` read ONLY data["result"],
     so this shape was invisible: no text, no detail, the generic
     "claude produced no reply." replaced a perfectly good reason with a
     useless one.

  2. Separately, an assistant-side auth failure ("Not logged in · Please run
     /login") arrives as a normal-shaped assistant message, distinguished only
     by a sibling field (is_api_error_message: true) the code never checked --
     so it streamed to the chat as if it were a real answer.

  3. Every session created before isolation landed carries a native id from
     the OLD (shared, now-abandoned) config. After isolation, --resume with
     that id hits exactly the failure in (1), for every single one of them,
     forever, with no way to recover short of ending the session by hand.
"""
import threading

import pytest

import agentic_chat as ac


# --------------------------------------------------------------------------- #
# The parser gap: an execution failure with no "result" string
# --------------------------------------------------------------------------- #

_STALE_RESUME_JSON = (
    '{"type":"result","subtype":"error_during_execution","duration_ms":0,'
    '"duration_api_ms":0,"is_error":true,"num_turns":0,"stop_reason":null,'
    '"session_id":"00000000-0000-0000-0000-000000000000","total_cost_usd":0,'
    '"usage":{},"errors":["No conversation found with session ID: '
    '00000000-0000-0000-0000-000000000000"]}'
)


def test_claude_result_text_reads_the_errors_array_when_result_is_absent():
    import json
    ev = json.loads(_STALE_RESUME_JSON)
    assert ac._claude_result_text(ev) == \
        "No conversation found with session ID: 00000000-0000-0000-0000-000000000000"


def test_the_non_streaming_parser_no_longer_drops_this_shape():
    text, native_id, detail = ac._parse_claude_json(_STALE_RESUME_JSON, "", 1)
    assert text is None                            # still a failure...
    assert "No conversation found" in detail        # ...but no longer a silent one
    assert native_id == "00000000-0000-0000-0000-000000000000"


def test_the_streaming_parser_flags_it_as_an_error_not_a_success():
    events = ac._claude_stream_events(_STALE_RESUME_JSON)
    finals = [e for e in events if "_final" in e]
    errors = [e for e in events if "_final_error" in e]
    assert finals == [], "an execution error must never become a normal reply"
    assert errors and "No conversation found" in errors[0]["_final_error"]


# --------------------------------------------------------------------------- #
# An auth failure must not stream as if it were a real answer
# --------------------------------------------------------------------------- #

_AUTH_FAIL_ASSISTANT_JSON = (
    '{"type":"assistant","message":{"role":"assistant","content":'
    '[{"type":"text","text":"Not logged in \\u00b7 Please run /login"}]},'
    '"session_id":"s1","error":"authentication_failed","is_api_error_message":true}'
)
_AUTH_FAIL_RESULT_JSON = (
    '{"type":"result","is_error":true,"session_id":"s1",'
    '"result":"Not logged in \\u00b7 Please run /login"}'
)


def test_an_auth_failure_never_streams_as_a_normal_message():
    events = ac._claude_stream_events(_AUTH_FAIL_ASSISTANT_JSON)
    assert events == [], "is_api_error_message must suppress the fake reply"


def test_the_terminal_auth_failure_is_an_error_not_a_reply():
    events = ac._claude_stream_events(_AUTH_FAIL_RESULT_JSON)
    assert not any("_final" in e for e in events)
    assert any(e.get("_final_error", "").startswith("Not logged in") for e in events)


def test_a_real_successful_answer_is_unaffected():
    """The success path must be byte-identical to before this fix."""
    ok = '{"type":"result","is_error":false,"session_id":"s1","result":"Hello!"}'
    events = ac._claude_stream_events(ok)
    assert {"_final": "Hello!"} in events
    assert {"event": "message", "text": "Hello!"} in events
    assert not any("_final_error" in e for e in events)


# --------------------------------------------------------------------------- #
# Stale-resume detection, per CLI, against the exact measured wording
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("cli,detail", [
    ("claude", "No conversation found with session ID: abc"),
    ("codex", "no rollout found for thread id abc (code -32600)"),
    ("opencode", "Session not found"),
])
def test_stale_resume_is_recognised_per_cli(cli, detail):
    assert ac._is_stale_resume_error(cli, detail)


def test_an_unrelated_error_is_not_mistaken_for_a_stale_resume():
    assert not ac._is_stale_resume_error("claude", "Not logged in · Please run /login")
    assert not ac._is_stale_resume_error("codex", "network timeout")


def test_stale_resume_detection_is_scoped_to_its_own_cli():
    """codex's wording must not accidentally match claude's checker or vice
    versa -- each CLI's failure text is specific to it."""
    assert not ac._is_stale_resume_error("claude", "no rollout found for thread id x")
    assert not ac._is_stale_resume_error("codex", "No conversation found with session ID: x")


# --------------------------------------------------------------------------- #
# End to end: a stale id recovers silently, an auth failure surfaces plainly
# --------------------------------------------------------------------------- #

class _FakeSession:
    """The subset of _Session that send_message_stream touches."""
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
    """Wires one fake session into the real registry/dispatch path, so this
    exercises send_message_stream's actual control flow -- not a reimplementation
    of it -- with only the subprocess replaced."""
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


class _FakeProc:
    """Enough of subprocess.Popen for the stdout-line loop + wait() to work,
    without spawning anything. `lines` is what `for line in proc.stdout` yields."""
    def __init__(self, lines, returncode=0):
        self.stdout = iter(lines)
        self.stderr = iter(())
        self.returncode = returncode
        self.pid = 999

    def wait(self, timeout=None):
        return self.returncode


def test_a_stale_resume_id_recovers_silently_end_to_end(registered_session, monkeypatch):
    sid, sess = registered_session(cli_id="claude", native_session_id="stale-id")
    calls = []

    def fake_popen(argv, **kw):
        calls.append(argv)
        if len(calls) == 1:
            assert "--resume" in argv, "first attempt must have tried to resume"
            return _FakeProc([_STALE_RESUME_JSON + "\n"], returncode=1)
        assert "--resume" not in argv, "retry must not resume the SAME dead id"
        return _FakeProc(['{"type":"result","is_error":false,"session_id":'
                          '"fresh-id","result":"Hello!"}\n'], returncode=0)

    monkeypatch.setattr(ac.subprocess, "Popen", fake_popen)
    events = list(ac.send_message_stream(sid, "say OK"))

    assert len(calls) == 2, "must retry exactly once, not loop or give up silently"
    assert not any(e.get("event") == "error" for e in events), \
        "the user must never see the stale-resume error -- it recovers"
    assert any(e.get("event") == "done" and e.get("text") == "Hello!" for e in events)
    assert sess.native_session_id == "fresh-id"


def test_a_stale_resume_id_is_not_retried_twice(registered_session, monkeypatch):
    """If the SECOND attempt somehow also looks stale, that is a real failure,
    not something to loop on forever."""
    sid, sess = registered_session(cli_id="claude", native_session_id="stale-id")
    calls = []

    def fake_popen(argv, **kw):
        calls.append(argv)
        return _FakeProc([_STALE_RESUME_JSON + "\n"], returncode=1)

    monkeypatch.setattr(ac.subprocess, "Popen", fake_popen)
    events = list(ac.send_message_stream(sid, "say OK"))

    assert len(calls) == 2, "exactly one retry, then stop"
    assert any(e.get("event") == "error" for e in events)


def test_a_genuine_auth_failure_is_not_treated_as_a_stale_resume(registered_session, monkeypatch):
    """No --resume in play, so there is nothing to retry -- the real problem
    (not logged in) must reach the user, with the isolated-copy help text."""
    sid, sess = registered_session(cli_id="claude", native_session_id=None)
    calls = []

    def fake_popen(argv, **kw):
        calls.append(argv)
        return _FakeProc([_AUTH_FAIL_ASSISTANT_JSON + "\n", _AUTH_FAIL_RESULT_JSON + "\n"],
                         returncode=1)

    monkeypatch.setattr(ac.subprocess, "Popen", fake_popen)
    events = list(ac.send_message_stream(sid, "say OK"))

    assert len(calls) == 1, "nothing here should trigger a retry"
    errors = [e for e in events if e.get("event") == "error"]
    assert errors and errors[0]["status"] == 403
    assert "isolated copy" in errors[0]["detail"]


def test_the_non_streaming_path_recovers_the_same_way(registered_session, monkeypatch):
    """send_message() (used by the non-streaming API) gets the identical fix,
    not a stream-only patch."""
    sid, sess = registered_session(cli_id="claude", native_session_id="stale-id")
    calls = []

    def fake_run(argv, cwd=None, env=None, **kw):
        calls.append(argv)
        import types
        r = types.SimpleNamespace()
        if len(calls) == 1:
            r.returncode = 1
            return r
        r.returncode = 0
        return r

    class _P:
        def __init__(self, argv, **kw):
            self.argv = argv
            self.returncode = 1 if len(calls) == 0 else 0
        def communicate(self, timeout=None):
            calls.append(self.argv)
            if len(calls) == 1:
                self.returncode = 1
                return _STALE_RESUME_JSON, ""
            self.returncode = 0
            return '{"type":"result","is_error":false,"session_id":"fresh-id","result":"Hello!"}', ""

    monkeypatch.setattr(ac.subprocess, "Popen", _P)
    status, text, detail = ac.send_message(sid, "say OK")

    assert len(calls) == 2
    assert status == 200
    assert text == "Hello!"
    assert sess.native_session_id == "fresh-id"


# --------------------------------------------------------------------------- #
# The stderr-drain race
# --------------------------------------------------------------------------- #

def test_stderr_is_actually_waited_for_before_being_read():
    """The drain thread is a SEPARATE scheduling unit from the stdout loop;
    reading stderr_buf immediately after the stdout loop finishes can race a
    thread that has not been scheduled yet, silently losing text that WAS
    written. This asserts the wait exists, not just that it usually works --
    a race is exactly the kind of bug that passes on a fast machine."""
    import inspect
    src = inspect.getsource(ac.send_message_stream)
    assert "drain_done" in src and ".wait(" in src


# --------------------------------------------------------------------------- #
# ANSI escape codes (opencode's raw stderr) must not reach the user
# --------------------------------------------------------------------------- #

def test_ansi_colour_codes_are_stripped():
    raw = "\x1b[91m\x1b[1mError: \x1b[0mSession not found"
    assert ac._sanitize(raw) == "Error: Session not found"


def test_sanitize_still_scrubs_secrets_after_the_ansi_fix(monkeypatch):
    monkeypatch.setattr(ac, "_secret_values", lambda: ["sk-secret-value-xyz"])
    assert "sk-secret-value-xyz" not in ac._sanitize("key was sk-secret-value-xyz")
