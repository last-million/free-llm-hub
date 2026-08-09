"""Transient-storm retry: a 429 burst must not surface to the CLI as a hard 503.

MEASURED 2026-07-31 from a live Codex session: three turns returned HTTP 503
with EVERY hop 429 (g4f-gemini x9, llm7, g4f-nvidia), while the same providers
all read healthy moments later. An agentic CLI fires many requests in quick
succession and the free relays meter per MINUTE (g4f ~5/min, llm7 20/min), so a
burst throttles the whole chain at once. To the user that looks like "I told it
many times and it just stops".
"""
import app


def test_rate_limits_and_timeouts_count_as_transient():
    for err in ("g4f-gemini: HTTP 429", "llm7: HTTP 429", "x: HTTP 500",
                "x: HTTP 502", "x: HTTP 503", "x: HTTP 504",
                "y: timeout", "y: timed out"):
        assert app._TRANSIENT_ERR_RE.search(err), err


def test_a_raw_connection_error_counts_as_transient():
    """MEASURED LIVE 2026-08-09: every hop in a real chain failed with
    errors.append(exc.__class__.__name__) recording the bare exception class
    name "ConnectionError" (a brief local network blip -- the VERY NEXT
    request seconds later succeeded normally on the same providers), yet the
    storm-retry never fired because "ConnectionError" contains none of
    timeout/timed out/HTTP 429/500/502/503/504. A pure connection failure is
    arguably the MOST classic transient condition and was the one class this
    regex missed."""
    for err in ("nvidia/minimaxai/minimax-m3: ConnectionError",
                "cloudflare/@cf/meta/llama-4-scout-17b-16e-instruct: ConnectionError"):
        assert app._TRANSIENT_ERR_RE.search(err), err


def test_hard_failures_are_not_transient():
    """A 400/404/401 will fail again in six seconds — retrying only wastes the
    user's time, so those still surface immediately."""
    for err in ("z: HTTP 400", "z: HTTP 404", "z: HTTP 401", "z: HTTP 403",
                "z: empty (200 but no content)", "z: non-JSON 200 body"):
        assert not app._TRANSIENT_ERR_RE.search(err), err


def test_the_retry_is_wired_and_bounded():
    import inspect
    src = inspect.getsource(app.v1_responses)
    # exactly one retry, never a loop
    assert "not _retry_pass" in src
    assert "v1_responses(_retry_pass=True)" in src
    # and only when NOTHING failed hard
    assert "not last_hard and errors" in src
    assert "all(_TRANSIENT_ERR_RE.search" in src


def test_the_backoff_outlasts_a_per_minute_window_bucket():
    """Too short and the window has not refilled; too long and the CLI times
    out waiting."""
    assert 3.0 <= app._CHAIN_RETRY_DELAY <= 15.0


def test_the_handler_still_works_as_a_plain_route():
    """Flask calls it with no arguments; the retry flag must stay optional."""
    import inspect
    sig = inspect.signature(app.v1_responses)
    assert list(sig.parameters) == ["_retry_pass"]
    assert sig.parameters["_retry_pass"].default is False
