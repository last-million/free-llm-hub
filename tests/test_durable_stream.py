"""A turn that finishes for real, but whose reply gets thrown away anyway.

THE BUG (found live): "Marseille restaurant" turn ran two full agent replies,
called update_plan, and exited clean around 14:05 UTC -- confirmed from
codex's own on-disk rollout transcript. agentic_history still showed only
the user's message; the agent's side never got recorded.

Root cause: send_message_stream is a plain generator. app.py's SSE route
only advances it -- and only reaches the code that persists the reply --
when Flask's WSGI layer is actively writing to a connected client. Whatever
was reading that stream stopped (tab closed, laptop slept, network dropped)
before the turn ended, so nothing ever called next() on the generator again.
The underlying CLI process kept running and genuinely finished; the hub
never noticed, and a completed reply was silently discarded.

Fix: send_message_stream_durable runs the real generator on a background
thread instead of leaving it to whoever reads the wrapper. The thread keeps
calling next() regardless of whether anyone is draining the queue it feeds,
and it is the thread -- not the route -- that persists the reply once the
turn actually ends.
"""
import threading
import time

import pytest

import agentic_chat as ac
import agentic_history as ah


class _FakeSession:
    def __init__(self, cli_id="claude", native_session_id=None, project_dir="."):
        self.id = None            # set by the registered_session fixture below
        self.cli_id = cli_id
        self.project_dir = project_dir
        self.native_session_id = native_session_id
        self.turn_count = 0
        self.created_at = time.time()
        self.proc = None
        self.proc_lock = threading.Lock()
        self.turn_lock = threading.Lock()
        self.last_interrupted = False
        self.tools_notified = True


class _FakeProc:
    """A real claude --output-format stream-json shape: init line, one
    assistant text line, a clean closing result line -- no delay needed,
    the point under test is who drives the read loop, not its timing."""
    def __init__(self, lines):
        self._lines = iter(lines)
        self.stderr = iter(())
        self.returncode = 0
        self.pid = 9911

    @property
    def stdout(self):
        return self

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._lines)

    def wait(self, timeout=None):
        return self.returncode


_LINES = [
    '{"type":"system","session_id":"durable-thread-1"}\n',
    ('{"type":"assistant","message":{"role":"assistant","content":'
     '[{"type":"text","text":"Building the restaurant site now."}]}}\n'),
    ('{"type":"result","subtype":"success","result":'
     '"Building the restaurant site now."}\n'),
]


@pytest.fixture
def registered_session(monkeypatch):
    monkeypatch.setattr(ac, "master_enabled", lambda: True)
    monkeypatch.setattr(ac, "_should_check_binary_identity", lambda s: False)
    monkeypatch.setattr(ac, "_resolve_bin", lambda cli: "/fake/" + cli)
    monkeypatch.setattr(ac.workspace, "missing_tools_message", lambda d: None)
    monkeypatch.setattr(ac.subprocess, "Popen",
                        lambda argv, **kw: _FakeProc(list(_LINES)))

    def make(cli_id="claude", native_session_id=None, project_dir="."):
        sess = _FakeSession(cli_id=cli_id, native_session_id=native_session_id,
                            project_dir=project_dir)
        sid = "durable-test-" + cli_id + "-" + str(id(sess))
        sess.id = sid
        ac._REGISTRY[sid] = sess
        return sid, sess
    yield make
    ac._REGISTRY.clear()


def _wait_for_conversation(session_id, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        conv = ah.get_conversation(session_id)
        if conv and any(t.get("role") == "agent" for t in conv.get("turns") or []):
            return conv
        time.sleep(0.02)
    return ah.get_conversation(session_id)


def test_a_fully_consumed_stream_still_relays_every_event_live(registered_session):
    """Baseline: draining the wrapper normally must behave exactly like
    draining send_message_stream directly -- same events, nothing lost."""
    sid, sess = registered_session()
    try:
        events = list(ac.send_message_stream_durable(sid, "build it"))
        done = [e for e in events if e.get("event") == "done"]
        assert done, "expected a done event: %r" % events
        assert "Building the restaurant site" in done[0]["text"]
    finally:
        ah.delete_conversation(sid)


def test_abandoning_the_generator_early_still_persists_the_reply(registered_session):
    """THE regression test. Simulates a client that reads the first live
    event and then goes away -- exactly what a closed tab or a dropped
    connection looks like from the generator's side: nobody calls next()
    on it ever again. The reply must still land in agentic_history."""
    sid, sess = registered_session()
    try:
        gen = ac.send_message_stream_durable(sid, "build it")
        first = next(gen)                 # one pull, like a client that saw
        assert first.get("event")         # the first event and then vanished
        del gen                           # no further reads -- simulated drop

        conv = _wait_for_conversation(sid)
        assert conv, "no conversation was ever persisted for %r" % sid
        agent_turns = [t for t in conv["turns"] if t.get("role") == "agent"]
        assert agent_turns, ("the agent's reply must be saved even though "
                             "nothing kept reading the stream: %r" % conv["turns"])
        assert "Building the restaurant site" in agent_turns[0]["text"]
    finally:
        ah.delete_conversation(sid)
