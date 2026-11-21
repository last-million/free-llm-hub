"""The wait for a model follows that model's own measured speed.

MEASURED 2026-11-21 (the hub's saved first-content times on tool turns): nvidia kimi-k3.1-pro
p50 80 s / p90 115 s, g4f deepseek-v4.1-flash p50 57 s / p90 71 s, nvidia mistral-large p90 75 s,
g4f gemini-3.6-flash p90 118 s -- against flat limits of 35 s (a stream's first content), 60-90 s
(a "slow" model's) and 25-45 s on a short turn. A good model that normally answers a little later
than the limit was cut about half the time, and its own record never counted.

Flag `measured_patience` (default on). A pair with a real record (>= 5 samples) that is not failing
may wait up to 1.1 x its OWN p90, at most 60% of the request deadline, on ONE pair per walk, never on
a short turn or a subscription. Only ever LONGER than the normal wait.
"""
import pytest

import app as A

SLOW = ("nvidia", "kimi-k3.1-pro")
OTHER = ("g4f", "deepseek-v4.1-flash")
FAST = ("groq", "qwen3.8-27b")


def _ms(*seconds):
    return [s * 1000.0 for s in seconds]


@pytest.fixture
def world(monkeypatch):
    """A healthy fleet with a measured record for three pairs, the flag on."""
    stream = {SLOW: _ms(60, 70, 80, 90, 100, 110, 130, 75, 85, 95),     # p90 ~ 130
              OTHER: _ms(50, 55, 57, 60, 65, 70, 72, 58, 56, 66),         # p90 ~ 70
              FAST: _ms(2, 3, 2, 4, 3, 2, 5, 3, 2, 4)}
    monkeypatch.setattr(A, "_ttft", dict(stream))
    monkeypatch.setattr(A, "_tool_ttft", {})
    monkeypatch.setattr(A, "_recent_hop_failure", lambda pid, model: None)
    monkeypatch.setattr(A, "_chain_reliability_band", lambda pid, model: 0)
    monkeypatch.setattr(A, "_pair_resting", lambda pid, model: False)
    monkeypatch.setattr(A, "_is_pair_benched", lambda pid, model: False)
    monkeypatch.setattr(A, "_tool_turn_sick", lambda pid, model: False)
    monkeypatch.setattr(A.config, "get_flag",
                        lambda name, default=False: True if name == "measured_patience" else default)
    return stream


def _clock(**kw):
    kw.setdefault("est", 20000)
    kw.setdefault("trivial", False)
    kw.setdefault("stream", True)
    return A._ChainClock(**kw)


# --------------------------------------------------------------------------- #
# The measure
# --------------------------------------------------------------------------- #

def test_patience_is_a_little_over_the_pairs_own_p90(world):
    got = A._measured_patience(*SLOW, False, 240)
    assert got == pytest.approx(1.1 * A._percentile(world[SLOW], 90) / 1000.0)
    assert got > 100


def test_it_never_exceeds_the_share_of_the_deadline(world, monkeypatch):
    monkeypatch.setitem(A._ttft, SLOW, _ms(300, 310, 320, 330, 340, 350))
    assert A._measured_patience(*SLOW, False, 240) == pytest.approx(0.6 * 240)


def test_a_thin_record_gives_no_patience(world, monkeypatch):
    monkeypatch.setitem(A._ttft, SLOW, _ms(100, 110, 120, 130))      # 4 samples
    assert A._measured_patience(*SLOW, False, 240) is None
    assert A._measured_patience("x", "never-seen", False, 240) is None


def test_tool_turns_read_the_tool_turn_record(world, monkeypatch):
    monkeypatch.setitem(A._tool_ttft, SLOW, _ms(70, 80, 90, 95, 100, 100))
    assert A._measured_patience(*SLOW, True, 240) == pytest.approx(
        1.1 * A._percentile(A._tool_ttft[SLOW], 90) / 1000.0)


@pytest.mark.parametrize("what,patch", [
    ("a recent failure", ("_recent_hop_failure", lambda pid, model: "timeout")),
    ("a measured-to-fail pair", ("_chain_reliability_band", lambda pid, model: 2)),
    ("a resting pair", ("_pair_resting", lambda pid, model: True)),
    ("a benched pair", ("_is_pair_benched", lambda pid, model: True)),
])
def test_a_pair_that_is_failing_gets_no_patience(world, monkeypatch, what, patch):
    monkeypatch.setattr(A, patch[0], patch[1])
    assert A._measured_patience(*SLOW, False, 240) is None, what


def test_a_pair_sick_on_tool_turns_gets_none_there_only(world, monkeypatch):
    monkeypatch.setitem(A._tool_ttft, SLOW, _ms(100, 110, 120, 130, 140, 150))
    monkeypatch.setattr(A, "_tool_turn_sick", lambda pid, model: True)
    assert A._measured_patience(*SLOW, True, 240) is None
    assert A._measured_patience(*SLOW, False, 240) is not None


def test_the_flag_turns_it_off(world, monkeypatch):
    monkeypatch.setattr(A.config, "get_flag", lambda name, default=False: False)
    assert A._measured_patience(*SLOW, False, 240) is None


def test_it_never_raises(world, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("ledger unreadable")
    monkeypatch.setattr(A, "_qf_first_content_ms", boom)
    assert A._measured_patience(*SLOW, False, 240) is None


# --------------------------------------------------------------------------- #
# A stream's first content
# --------------------------------------------------------------------------- #

def test_the_peek_waits_for_a_slow_pairs_own_p90(world):
    with A.app.test_request_context("/v1/chat/completions"):
        c = _clock()
        flat = A._stream_peek_timeout(SLOW[1], 20000, pid=SLOW[0])
        got = c.peek_timeout(SLOW[1], 20000, pid=SLOW[0])
        assert got > flat, (got, flat)
        assert got == pytest.approx(A._measured_patience(*SLOW, False, c.limit), abs=0.01)


def test_a_fast_pair_keeps_the_normal_peek(world):
    with A.app.test_request_context("/v1/chat/completions"):
        c = _clock()
        assert c.peek_timeout(FAST[1], 20000, pid=FAST[0]) == pytest.approx(
            min(A._stream_peek_timeout(FAST[1], 20000, pid=FAST[0]), c.left()), abs=0.01)


def test_a_short_turn_is_never_made_to_wait(world):
    with A.app.test_request_context("/v1/chat/completions"):
        c = _clock(trivial=True, est=300)
        assert c.peek_timeout(SLOW[1], 300, pid=SLOW[0]) <= A._stream_peek_timeout(
            SLOW[1], 300, pid=SLOW[0]) + 0.01


def test_only_one_pair_per_walk_gets_the_longer_wait(world):
    with A.app.test_request_context("/v1/chat/completions"):
        c = _clock()
        first = c.peek_timeout(SLOW[1], 20000, pid=SLOW[0])
        second = c.peek_timeout(OTHER[1], 20000, pid=OTHER[0])
        assert first > A._stream_peek_timeout(SLOW[1], 20000, pid=SLOW[0])
        assert second <= A._stream_peek_timeout(OTHER[1], 20000, pid=OTHER[0]) + 0.01
        # ...and the first one keeps it on a later look
        assert c.peek_timeout(SLOW[1], 20000, pid=SLOW[0]) == pytest.approx(first, abs=0.5)


def test_the_peek_is_still_bounded_by_the_request_deadline(world):
    with A.app.test_request_context("/v1/chat/completions"):
        c = _clock()
        assert c.peek_timeout(SLOW[1], 20000, pid=SLOW[0]) <= c.left() + 0.01


def test_a_failing_pair_gets_the_normal_peek(world, monkeypatch):
    monkeypatch.setattr(A, "_recent_hop_failure", lambda pid, model: "timeout")
    with A.app.test_request_context("/v1/chat/completions"):
        c = _clock()
        assert c.peek_timeout(SLOW[1], 20000, pid=SLOW[0]) <= A._stream_peek_timeout(
            SLOW[1], 20000, pid=SLOW[0]) + 0.01


# --------------------------------------------------------------------------- #
# The per-provider share
# --------------------------------------------------------------------------- #

def test_the_provider_share_gives_way_to_a_measured_slow_pair(world):
    with A.app.test_request_context("/v1/chat/completions"):
        c = _clock()
        c._rest = [("inception", "mercury-3")]            # another provider is waiting
        cap = max(A._TOOL_PROVIDER_MIN_SECONDS, A._TOOL_PROVIDER_SHARE * float(c.limit))
        got = c._budget_for(*SLOW, stream=True)
        assert got is not None and got > cap, (got, cap)
        assert got <= A._PATIENT_MAX_SHARE * float(c.limit) + 0.01


def test_the_share_still_cuts_everything_else(world):
    with A.app.test_request_context("/v1/chat/completions"):
        c = _clock()
        c._rest = [("inception", "mercury-3")]
        cap = max(A._TOOL_PROVIDER_MIN_SECONDS, A._TOOL_PROVIDER_SHARE * float(c.limit))
        assert c._budget_for(*FAST, stream=True) <= cap + 0.01
        c.peek_timeout(SLOW[1], 20000, pid=SLOW[0])      # SLOW claims the one slot
        assert c._budget_for(*OTHER, stream=True) <= cap + 0.01


def test_time_already_spent_by_the_provider_counts(world):
    with A.app.test_request_context("/v1/chat/completions"):
        c = _clock()
        c._rest = [("inception", "mercury-3")]
        c._prov_secs[SLOW[0]] = 100.0
        got = c._budget_for(*SLOW, stream=True)
        assert got <= A._PATIENT_MAX_SHARE * float(c.limit) - 100.0 + 0.01


# --------------------------------------------------------------------------- #
# The header wait of the request itself
# --------------------------------------------------------------------------- #

def test_the_streaming_header_wait_follows_the_pairs_record(world, monkeypatch):
    seen = []

    class _R:
        status_code = 200
        text = ""
        headers = {}

        def close(self):
            pass

        def iter_lines(self, decode_unicode=False):
            return iter(())

    monkeypatch.setattr(A, "_post_with_header_deadline", lambda wait, fn, **kw: seen.append(wait) or _R())
    monkeypatch.setattr(A, "_needs_key", lambda pid: False)
    monkeypatch.setattr(A.quota, "record", lambda *a, **k: None)
    monkeypatch.setattr(A.quota, "record_key", lambda *a, **k: None)
    monkeypatch.setitem(A._ttft, ("uncloseai", "slow-model"), _ms(100, 110, 120, 130, 140, 150))
    body = {"stream": True, "messages": [{"role": "user", "content": "hi"}]}
    A._upstream_chat("uncloseai", dict(body, model="slow-model"), True)
    A._upstream_chat("uncloseai", dict(body, model="unmeasured-model"), True)
    assert seen[0] > A._STREAM_HEADER_WAIT + 30
    assert seen[1] < seen[0]
    assert seen[0] <= A._STREAM_HEADER_BUDGET
