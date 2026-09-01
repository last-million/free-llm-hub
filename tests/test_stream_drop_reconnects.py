"""A dropped stream is not a failed turn.

REPORTED 2026-09-01, mid-build: "i see network error ... he should automatically
keep retrying please and dont loose the context ... when i ask to continue he
say: Failed / Failed to fetch, and when i refresh page it's like he will start
from beginning man".

Three symptoms, one cause. The agent turn's fetch had a bare

    .catch(function(e){ asstEl.className = 'msg assistant error';
                        line('err', String(e.message)); })

so ANY interruption of the stream -- the hub restarting, a moment of the port
not being served, a browser sleeping a tab -- immediately marked the turn
Failed. Nothing retried, nothing reconnected.

That reading was wrong about what had actually happened. When the stream drops:

  - the CLI SUBPROCESS is still running on the hub and still writing files;
  - the transcript is already on disk (agentic_history records the user turn
    BEFORE the CLI is called, and the agent turn when it returns);
  - and even if the hub itself restarted, the CLI keeps its own thread id, which
    /resume rebuilds a live session around.

So the work and its context both survive a dropped stream. Throwing them away
and starting the next message from nothing is what made it look like a reset.

It now reconnects: poll the session, wait while it is still running, load the
finished transcript when it is done, and rebuild through /resume when the hub
restarted underneath it. Only after ~6 minutes of genuinely failing does it give
up -- and it says the work may still have finished rather than claiming it died.
"""


def _template():
    with open("templates/index.html", encoding="utf-8") as f:
        return f.read()


def _recover():
    html = _template()
    i = html.index("function recoverStream(")
    return html[i:i + 3200]


# --------------------------------------------------------------------------- #
# The turn no longer fails on the first hiccup
# --------------------------------------------------------------------------- #

def test_the_stream_catch_recovers_instead_of_failing():
    html = _template()
    assert "return recoverStream(sessionId, asstEl, line, e, 1);" in html


def test_it_no_longer_marks_the_turn_failed_immediately():
    """The exact line that produced "Failed / Failed to fetch"."""
    html = _template()
    assert (".catch(function(e){ asstEl.className = 'msg assistant error'; "
            "line('err', String((e && e.message) || e)); })") not in html


def test_it_tells_the_user_the_work_is_not_lost():
    body = _recover()
    assert "reconnecting" in body.lower()
    assert "nothing is lost" in body.lower()


# --------------------------------------------------------------------------- #
# What it does while reconnecting
# --------------------------------------------------------------------------- #

def test_it_waits_while_the_turn_is_still_running():
    body = _recover()
    assert "currently_running" in body
    assert "recoverStream(sid, asstEl, line, err, attempt + 1)" in body


def test_it_loads_the_transcript_when_the_turn_finished_without_us():
    body = _recover()
    assert "loadFullHistory" in body


def test_a_restarted_hub_is_rebuilt_through_resume():
    """A 404 means the SESSION is gone, i.e. the hub restarted -- not that the
    work vanished. The CLI keeps its own thread and the transcript is on disk."""
    body = _recover()
    assert "/resume" in body
    assert "probeErr.status === 404" in body


def test_a_hub_that_is_simply_down_keeps_being_retried():
    """Anything that is not a 404 is the hub not being back yet."""
    body = _recover()
    assert "if (!gone) return recoverStream" in body


def test_it_gives_up_eventually_rather_than_looping_forever():
    body = _recover()
    assert "STREAM_RECOVER_TRIES" in body
    assert "attempt > STREAM_RECOVER_TRIES" in body


def test_giving_up_is_honest_about_what_it_does_not_know():
    """It cannot tell whether the work finished, so it must not claim it died."""
    body = _recover()
    assert "may still have finished" in body


def test_the_retry_window_is_long_enough_for_a_real_turn():
    """A free-tier build turn legitimately runs for minutes; a 30-second give-up
    would defeat the point."""
    html = _template()
    tries = int(html.split("var STREAM_RECOVER_TRIES = ", 1)[1].split(";", 1)[0].split()[0])
    wait = int(html.split("var STREAM_RECOVER_WAIT = ", 1)[1].split(";", 1)[0].split()[0])
    assert tries * wait >= 5 * 60 * 1000, "%d x %dms is under five minutes" % (tries, wait)


# --------------------------------------------------------------------------- #
# ...and reloading the page during a restart keeps the conversation
# --------------------------------------------------------------------------- #

def _reload_block():
    html = _template()
    i = html.index("(function attemptResume(tries)")
    return html[i:i + 1800]


def test_reloading_while_the_hub_is_down_retries():
    """The second half of the same report. Reloading DURING a restart -- which
    is exactly when someone reloads, because the page had just said "Failed to
    fetch" -- failed this resume call on the network."""
    body = _reload_block()
    assert "attemptResume(tries + 1)" in body
    assert "tries < 20" in body


def test_a_network_failure_does_not_wipe_the_session_from_the_url():
    """This is what made it look like starting from the beginning: the catch
    cleared the session id, so the conversation was no longer even addressed."""
    body = _reload_block()
    keep = body.split("if (!answered", 1)[1]
    # the clear only happens on the ANSWERED path
    assert "cxSetHashSession('sec-agent', null)" in body
    assert body.index("answered = e && e.status") < body.index("cxSetHashSession('sec-agent', null)")


def test_a_real_verdict_from_the_hub_is_still_honoured():
    """No stored conversation, or a deleted project folder, IS gone -- and must
    still be reported rather than retried forever."""
    body = _reload_block()
    assert "answered = e && e.status" in body
    assert "no longer open on the hub" in body


def test_it_says_the_conversation_is_safe_rather_than_lost():
    body = _reload_block()
    assert "conversation is safe" in body
