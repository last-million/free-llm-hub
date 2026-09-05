"""The chain stops walking before the client stops listening.

REPORTED 2026-09-05 from opencode: "Provider response headers timed out after
300000ms" -- and it does not retry, it ends the turn.

A streaming request deliberately withholds its 200 until a hop produces real
content, so a dead hop can still be replaced by the next one. That is the right
design and it makes time-to-first-HEADER unbounded:

    MAX_HOPS (6)  x  STREAM_SLOW_BIG_PEEK_TIMEOUT (90s)  =  540s of silence

Every OpenAI-shaped client has a header timeout. opencode's is 300s, which the
chain can exceed while doing exactly what it was designed to do -- and once it
does, nothing the hub finds afterwards can be delivered, because nobody is
listening. The socket stays open past the point of any use.

So the walk gets a budget under that. A 503 that ARRIVES is worth more than a
better answer that does not.
"""
import app as A


def test_the_budget_is_under_a_typical_client_timeout():
    """opencode's is 300s. The budget has to leave room to send the error."""
    assert A._STREAM_HEADER_BUDGET < 300


def test_it_is_long_enough_for_a_real_chain():
    """Several hops on free models legitimately take minutes; cutting at 60s
    would throw away answers that were coming."""
    assert A._STREAM_HEADER_BUDGET >= 180


def test_the_old_worst_case_really_did_exceed_it():
    """The arithmetic that made this a bug, pinned so it cannot drift back."""
    assert A.MAX_HOPS * A.STREAM_SLOW_BIG_PEEK_TIMEOUT > 300


def test_a_fresh_walk_has_not_spent_its_budget():
    assert A._header_budget_spent(A.time.monotonic()) is False


def test_an_old_walk_has():
    started = A.time.monotonic() - (A._STREAM_HEADER_BUDGET + 1)
    assert A._header_budget_spent(started) is True


def test_it_never_raises_on_nonsense():
    """This runs inside the hop loop; an exception here would fail the turn it
    is trying to rescue."""
    assert A._header_budget_spent(None) is False
    assert A._header_budget_spent("not a time") is False


def test_every_client_facing_walk_is_guarded():
    """chat/completions, responses and messages -- a client left unguarded
    still hangs, and it would be the one CLI nobody tested."""
    src = open("app.py", encoding="utf-8").read()
    assert src.count("_header_budget_spent(_walk_started)") == 3
    assert src.count("_walk_started = time.monotonic()") == 3


def test_the_guard_only_applies_to_streaming():
    """A buffered request's caller waits on a body, not on headers, and cutting
    it short would abandon an answer for no reason."""
    src = open("app.py", encoding="utf-8").read()
    assert src.count("if stream and _header_budget_spent(_walk_started):") == 3


def test_giving_up_says_why():
    """"all providers failed" would send the reader hunting for a provider
    problem that is not there."""
    src = open("app.py", encoding="utf-8").read()
    assert "the client's header timeout was" in src
