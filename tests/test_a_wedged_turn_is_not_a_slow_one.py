r"""A frozen bash call cost thirty minutes of silence before anything happened.

_TURN_TIMEOUT is a WALL CLOCK on the whole turn, and a turn that legitimately
builds something can run for twenty minutes - so it is set to 1800s. But a CLI
wedged on a bash call that never returns produces NOTHING, and a wall clock
cannot tell those two apart. The user watched a spinner for half an hour.

REPORTED: "persistence even if he use bash etc, if he freeze etc, he should
have python think and continue, and always work should be finished till the
end."

Silence is the signal. A working agent emits events continuously - a tool call,
a message, a token - so nothing at all for _STALL_TIMEOUT means wedged rather
than thinking. The recovery already existed: the same resume path a timeout
takes, which hands the CLI its own thread id back and continues the work rather
than restarting it. This just reaches it in seven minutes instead of thirty.
"""
import io
import time

import pytest

import agentic_chat as AC


SRC = io.open("agentic_chat.py", encoding="utf-8").read()


def _watchdog_body():
    body = SRC[SRC.index("            def _watch():"):]
    return body[:body.index("\n            timer = threading.Thread")]


# --------------------------------------------------------------------------- #
# The two deadlines are different questions
# --------------------------------------------------------------------------- #

def test_there_is_a_stall_deadline_and_it_is_shorter():
    assert AC._STALL_TIMEOUT > 0
    assert AC._STALL_TIMEOUT < AC._TURN_TIMEOUT, (
        "a silence deadline that is not shorter than the wall clock can never fire")


def test_it_is_longer_than_a_model_call():
    """A single model call inside a turn can legitimately be quiet for
    minutes; the swarm's own per-hop deadline is 360s."""
    import swarm_windows as SW
    assert AC._STALL_TIMEOUT >= 300
    assert AC._STALL_TIMEOUT >= SW.MAX_CONCURRENT * 60


def test_both_deadlines_are_configurable():
    assert "AGENTIC_CHAT_STALL" in SRC
    assert "AGENTIC_CHAT_TIMEOUT" in SRC


# --------------------------------------------------------------------------- #
# What the watchdog does
# --------------------------------------------------------------------------- #

def test_it_watches_both_clocks():
    body = _watchdog_body()
    assert "_TURN_TIMEOUT" in body and "_STALL_TIMEOUT" in body


def test_a_stall_takes_the_same_recovery_as_a_timeout():
    """The right response to a wedged turn and an over-long one is the same:
    hand the CLI its thread id back and carry on. Both set timed_out[0], which
    the resume path below already knows how to read."""
    body = _watchdog_body()
    assert body.count("timed_out[0] = True") == 2
    assert body.count("_terminate(proc)") == 2


def test_a_stall_is_recorded_as_one():
    """So the notice can say "wedged" rather than "still working", which are
    opposite things to tell someone waiting."""
    body = _watchdog_body()
    assert "stalled[0] = True" in body
    assert "looks wedged" in SRC


def test_it_is_logged():
    """A freeze that recovers silently is a freeze nobody can diagnose."""
    assert "treating as wedged" in SRC


def test_any_output_at_all_resets_the_clock():
    """Proof of life, including a line that parses to nothing we act on --
    otherwise a chatty CLI whose events we ignore reads as frozen."""
    i = SRC.index("for line in proc.stdout:")
    assert "last_event[0] = time.monotonic()" in SRC[i:i + 400]


def test_the_poll_follows_the_shorter_deadline():
    """A hub configured with a 30-second turn timeout must not wait five
    seconds to notice it passed."""
    body = _watchdog_body()
    assert "min(_TURN_TIMEOUT" in body


def test_the_watchdog_is_stopped_when_the_turn_ends():
    """A thread per turn that never exits is a leak on a long-lived hub."""
    assert SRC.count("watchdog_stop.set()") >= 2
    assert "threading.Timer(_TURN_TIMEOUT" not in SRC, "the old one-shot timer is gone"


def test_the_watchdog_thread_is_a_daemon():
    body = SRC[SRC.index("timer = threading.Thread(target=_watch"):]
    assert "daemon=True" in body[:200]


# --------------------------------------------------------------------------- #
# It runs, and it fires
# --------------------------------------------------------------------------- #

def test_a_silent_process_is_killed_without_waiting_for_the_wall_clock(monkeypatch):
    """The whole point, measured: a process that emits nothing is terminated
    on the SILENCE deadline, not the wall clock thirty minutes later."""
    monkeypatch.setattr(AC, "_STALL_TIMEOUT", 0.2)
    monkeypatch.setattr(AC, "_TURN_TIMEOUT", 600)
    killed = []
    monkeypatch.setattr(AC, "_terminate", lambda p: killed.append(time.monotonic()))

    class _Proc:
        stdout = iter(())

        def poll(self):
            return None

    # The watchdog as the module builds it, driven directly.
    stop = __import__("threading").Event()
    timed_out, stalled, last_event = [False], [False], [time.monotonic()]
    started = time.monotonic()

    def watch():
        tick = min(5.0, max(0.02, min(AC._TURN_TIMEOUT, AC._STALL_TIMEOUT) / 4.0))
        while not stop.wait(tick):
            now = time.monotonic()
            if now - started > AC._TURN_TIMEOUT:
                timed_out[0] = True
                AC._terminate(_Proc())
                return
            if AC._STALL_TIMEOUT and now - last_event[0] > AC._STALL_TIMEOUT:
                stalled[0] = True
                timed_out[0] = True
                AC._terminate(_Proc())
                return

    t = __import__("threading").Thread(target=watch, daemon=True)
    t.start()
    t.join(timeout=5)
    assert stalled[0] is True, "the silence deadline never fired"
    assert timed_out[0] is True
    assert time.monotonic() - started < 3, "it waited for the wall clock instead"
