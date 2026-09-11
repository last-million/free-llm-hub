r"""A turn that lost a race for the CLI's own database answered 502.

REPORTED: "http 502".

MEASURED live: opencode keeps ONE SQLite database for the whole machine
(~/.local/share/opencode/opencode.db). Two of its processes writing at once and
the loser gets

    502  Error: Unexpected error / database is locked

and the turn is over before it began. swarm_windows already retried this for
its workers - so sending a message on /agent while a swarm was running was a
502 for the ordinary path and a quiet retry for the orchestrated one. That is
backwards: the person watching is the one who notices.

One retry, and only for a failure that says it is temporary. A model that
refused, a CLI that is not signed in, a prompt that is wrong - those fail the
same way twice, and retrying spends the tokens again to learn nothing.
"""
import io

import pytest

import agentic_chat as AC


SRC = io.open("agentic_chat.py", encoding="utf-8").read()


# --------------------------------------------------------------------------- #
# What counts as temporary
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("detail", [
    "Error: Unexpected error\n\ndatabase is locked",
    "database table is locked",
    "SQLITE_BUSY",
    "resource temporarily unavailable",
    "The file is being used by another process",
    "EBUSY: resource busy or locked",
])
def test_a_temporary_failure_is_recognised(detail):
    assert AC._looks_transient(detail) is True, detail


@pytest.mark.parametrize("detail", [
    "no API key for this provider",
    "the model refused the request",
    "context length exceeded",
    "not signed in",
    "",
    None,
])
def test_a_real_failure_is_not(detail):
    """Retrying these spends the tokens again to learn nothing."""
    assert AC._looks_transient(detail) is False, detail


# --------------------------------------------------------------------------- #
# Both send paths retry it, exactly once
# --------------------------------------------------------------------------- #

def test_the_streaming_path_retries():
    """The one the dashboard and every swarm worker actually use."""
    body = SRC[SRC.index("def send_message_stream(session_id, text):"):]
    assert "transient_retry_used" in body
    assert "_looks_transient(detail) and not transient_retry_used" in body


def test_the_plain_path_retries_too():
    body = SRC[SRC.index("def send_message(session_id, text):"):]
    body = body[:body.index("\ndef send_message_stream(")]
    assert "transient_retry_used" in body


def test_it_retries_once_and_not_forever():
    """A database that is locked for good must still end in an error the user
    can see, not a loop."""
    assert SRC.count("transient_retry_used = True") == 2      # one per path
    assert SRC.count("transient_retry_used = False") == 2     # one per path


def test_an_auth_failure_still_wins():
    """403 with a Sign in button beats a retry that cannot possibly work."""
    i = SRC.index("code=\"cli_not_signed_in\"")
    after = SRC[i:i + 600]
    assert after.index("_looks_transient") > 0, "auth is checked first"


def _stream_retry():
    """The STREAMING occurrence -- rindex, not index: the plain path's copy
    comes first in the file and only sleeps, because that path has no event
    stream to say anything on and no native id salvaged to resume from."""
    i = SRC.rindex("_looks_transient(detail) and not transient_retry_used")
    return SRC[i:i + 900]


def test_the_retry_resumes_rather_than_restarts():
    """The work already done is still in the CLI's own thread; starting over
    would pay for it twice."""
    body = _stream_retry()
    assert "sess.native_session_id = native_id" in body
    assert "resuming" in body


def test_it_says_so_out_loud():
    """A turn that silently starts over looks like a hang."""
    assert '"event": "notice"' in _stream_retry()


def test_it_waits_before_trying_again():
    """Immediately retrying a lock is just losing the same race faster."""
    assert 0 < AC._TRANSIENT_RETRY_WAIT <= 10
    assert "_TRANSIENT_RETRY_WAIT" in _stream_retry()


# --------------------------------------------------------------------------- #
# The orchestrator kept its own
# --------------------------------------------------------------------------- #

def test_the_swarm_still_retries_its_workers():
    """This adds a second layer; it does not move the first one."""
    import swarm_windows as SW
    assert SW._is_transient("database is locked") is True
    assert SW.AGENT_ATTEMPTS >= 2
