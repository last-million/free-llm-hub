"""One unresponsive provider must not consume the entire fallback chain.

REPORTED 2026-09-05, a run of 503s out of opencode. Half of them looked like
this -- one hop, then nothing:

    CHAT-503 est=204826 errors=[nvidia: ReadTimeout;
    stopped after 240s: the client's header timeout was about to expire]

MEASURED against nvidia directly, bypassing the hub entirely:

    small   996 bytes      FAILED at 300.6s: ReadTimeout
    40K     164176 bytes   FAILED at 300.7s: ReadTimeout
    160K    656176 bytes   FAILED at 301.7s: ReadTimeout
    205K    840676 bytes   FAILED at 301.4s: ReadTimeout

Not a size problem: nvidia answered NOTHING at any size. It accepts the TCP
connection and never sends response headers. The hub still elects it primary --
it benchmarks 134 -- and then waits.

The structural defect is that the wait was longer than the whole chain was
allowed to take:

    STREAM_IDLE_TIMEOUT      280   <- one hop's socket ceiling
    _STREAM_HEADER_BUDGET    240   <- the whole chain's budget

requests applies its read timeout to the wait for RESPONSE HEADERS as well as to
the gaps between chunks, so a provider that goes quiet burns 280s in hop one, the
budget is spent, and the chain stops having tried exactly one model.

The control says a cap is safe. Time-to-headers on providers that work:

    groq        @ 6K     1.11s
    openrouter  @ 60K    2.73s
    tokenrouter @ 60K   10.83s
    dahl                 ConnectionError in 0.6s

Worst healthy case 10.8s. So the header wait is capped well above that and far
below the chain budget, and the inter-chunk idle timeout is left alone -- once
content is flowing the client already has its headers and the budget is moot.
"""
import threading
import time

import pytest
import requests

import app as A


class _Resp:
    status_code = 200

    def __init__(self, tag="ok"):
        self.tag = tag


# --------------------------------------------------------------------------- #
# The constants have to be able to do the job
# --------------------------------------------------------------------------- #

def test_the_header_wait_is_shorter_than_the_chain_budget():
    """The whole defect in one comparison. A per-hop ceiling above the chain
    budget guarantees a one-hop 503 whenever a provider goes quiet."""
    assert A._STREAM_HEADER_WAIT < A._STREAM_HEADER_BUDGET


def test_several_hops_fit_inside_the_budget():
    """A fallback chain that only ever gets one hop is not a fallback chain."""
    assert A._STREAM_HEADER_BUDGET // A._STREAM_HEADER_WAIT >= 3


def test_the_cap_clears_the_slowest_healthy_provider_measured():
    """tokenrouter took 10.83s to headers on a 60K request. The cap must leave
    real room above that -- killing healthy hops is the failure mode the
    adaptive peek timeouts were written to avoid."""
    assert A._STREAM_HEADER_WAIT >= 45


def test_the_idle_timeout_is_left_alone():
    """It governs the gaps BETWEEN chunks, after headers are in hand. By then
    the client is served and the header budget no longer applies, so a slow
    reasoning model must keep its long leash."""
    assert A.STREAM_IDLE_TIMEOUT > A._STREAM_HEADER_WAIT


# --------------------------------------------------------------------------- #
# The bounded post
# --------------------------------------------------------------------------- #

def test_a_provider_that_never_answers_gives_up_at_the_deadline():
    def never(**_kw):
        time.sleep(30)
        return _Resp()

    started = time.monotonic()
    with pytest.raises(requests.RequestException):
        A._post_with_header_deadline(0.3, never)
    assert time.monotonic() - started < 5, "waited past the deadline"


def test_the_give_up_is_a_request_exception_so_the_chain_handles_it():
    """_upstream_chat already rotates keys and falls through to the next hop on
    RequestException. Raising anything else would escape that path."""
    with pytest.raises(requests.RequestException):
        A._post_with_header_deadline(0.2, lambda **_k: time.sleep(30))


def test_a_healthy_response_passes_straight_through():
    resp = A._post_with_header_deadline(5, lambda **_k: _Resp("fast"))
    assert resp.tag == "fast"


def test_the_arguments_reach_the_call_unchanged():
    seen = {}

    def spy(**kw):
        seen.update(kw)
        return _Resp()

    A._post_with_header_deadline(5, spy, url="u", json={"a": 1}, stream=True)
    assert seen["url"] == "u" and seen["json"] == {"a": 1} and seen["stream"] is True


def test_an_error_from_the_call_is_raised_not_swallowed():
    """A connection refused must still look like a connection refused -- turning
    every failure into a timeout would lose the reason the hop failed, which is
    what the activity feed reports."""
    def boom(**_kw):
        raise requests.ConnectionError("refused")

    with pytest.raises(requests.ConnectionError):
        A._post_with_header_deadline(5, boom)


def test_a_non_request_error_still_propagates():
    def boom(**_kw):
        raise ValueError("bad payload")

    with pytest.raises(ValueError):
        A._post_with_header_deadline(5, boom)


def test_the_abandoned_call_does_not_block_the_caller():
    """The worker is a daemon: it keeps running against its own socket timeout
    and is simply not waited on. What must not happen is the caller joining it."""
    release = threading.Event()

    def slow(**_kw):
        release.wait(20)
        return _Resp()

    started = time.monotonic()
    with pytest.raises(requests.RequestException):
        A._post_with_header_deadline(0.3, slow)
    elapsed = time.monotonic() - started
    release.set()
    assert elapsed < 5


# --------------------------------------------------------------------------- #
# Only streaming is bounded
# --------------------------------------------------------------------------- #

def test_only_the_streaming_path_is_bounded(monkeypatch):
    """A non-streaming one-shot generation legitimately takes minutes, and its
    caller is waiting on a BODY, not on headers -- the budget this protects is
    the client's header timeout, which only a streaming request has."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index("_post_with_header_deadline(_STREAM_HEADER_WAIT")
    window = src[max(0, i - 400):i + 200]
    assert "if stream" in window or "stream else" in window, window
