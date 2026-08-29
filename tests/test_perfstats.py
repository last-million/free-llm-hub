"""Measured performance that survives a restart, and the latency penalty.

The hub already learned which hops deliver, but kept it in a plain dict -- and
it restarts on every auto-update (5h) and every reboot, so a signal designed to
accumulate over a week never got past a few hours. These cover the two halves
of the fix: the learning is now persisted, and measured latency joins measured
reliability as a demotion signal.

Both signals stay penalty-only and neutral-when-unknown, which is what lets
them be added to a live ranking without reordering anything the hub has never
actually run.
"""
import time
from unittest import mock

import app
import perfstats


def _clear():
    with app._outcome_lock:
        app._outcomes.clear()
        app._latencies.clear()


# --------------------------------------------------------------------------- #
# perfstats: the on-disk half
# --------------------------------------------------------------------------- #

def test_round_trip_keeps_both_halves(tmp_path):
    now = time.time()
    outcomes = {("groq", "qwen/qwen3.8-27b"): {"ok": 9, "fail": 1, "last": now}}
    latency = {("groq", "qwen/qwen3.8-27b"): {"ms": 2200.0, "n": 5, "last": now}}
    with mock.patch.object(perfstats, "_path", return_value=str(tmp_path / "p.json")):
        assert perfstats.save(outcomes, latency, force=True) is True
        back_o, back_l = perfstats.load()
    assert back_o == {("groq", "qwen/qwen3.8-27b"): {"ok": 9, "fail": 1,
                                                     "last": round(now, 3)}}
    assert back_l[("groq", "qwen/qwen3.8-27b")]["ms"] == 2200.0
    assert back_l[("groq", "qwen/qwen3.8-27b")]["n"] == 5


def test_stale_records_are_dropped_on_load(tmp_path):
    old = time.time() - perfstats.TTL - 60
    outcomes = {("dead", "m"): {"ok": 3, "fail": 0, "last": old}}
    with mock.patch.object(perfstats, "_path", return_value=str(tmp_path / "p.json")):
        # save() drops it too, so write the file by hand to test load's guard.
        (tmp_path / "p.json").write_text(
            '{"schema": 1, "pairs": [{"pid": "dead", "model": "m", "ok": 3,'
            ' "fail": 0, "last": %f}]}' % old, encoding="utf-8")
        assert perfstats.load() == ({}, {})
        assert perfstats.save(outcomes, {}, force=True) is True
        assert perfstats.load() == ({}, {})


def test_a_corrupt_or_missing_file_is_neutral_not_fatal(tmp_path):
    """The whole design rests on unknown == neutral, so a bad file must read as
    'no measurements', never raise, and never take a boot down."""
    p = tmp_path / "p.json"
    with mock.patch.object(perfstats, "_path", return_value=str(p)):
        assert perfstats.load() == ({}, {})          # missing
        p.write_text("{not json at all", encoding="utf-8")
        assert perfstats.load() == ({}, {})          # corrupt
        p.write_text('{"schema": 999, "pairs": []}', encoding="utf-8")
        assert perfstats.load() == ({}, {})          # wrong schema


def test_save_is_throttled_but_force_always_writes(tmp_path):
    with mock.patch.object(perfstats, "_path", return_value=str(tmp_path / "p.json")):
        perfstats._last_save[0] = 0.0
        assert perfstats.save({}, {}, force=True) is True
        assert perfstats.save({}, {}) is False       # inside SAVE_INTERVAL
        assert perfstats.save({}, {}, force=True) is True


# --------------------------------------------------------------------------- #
# app: the in-memory half
# --------------------------------------------------------------------------- #

def test_startup_restores_what_the_last_run_learned():
    _clear()
    now = time.time()
    restored = ({("groq", "m"): {"ok": 7, "fail": 0, "last": now}},
                {("groq", "m"): {"ms": 1500.0, "n": 9, "last": now}})
    with mock.patch.object(perfstats, "load", return_value=restored):
        app._load_perf_stats()
    assert app._reliability("groq", "m") > 0.5
    assert app._measured_latency_ms("groq", "m") == 1500.0
    _clear()


def test_latency_is_neutral_until_there_is_real_evidence():
    _clear()
    assert app._latency_penalty("groq", "m") == 0.0     # never measured
    for _ in range(app._LATENCY_MIN_SAMPLES - 1):
        app._record_latency("groq", "m", 80000.0)
    # Slow, but too few samples to judge on -- still neutral.
    assert app._measured_latency_ms("groq", "m") is None
    assert app._latency_penalty("groq", "m") == 0.0
    _clear()


def test_a_consistently_slow_hop_is_demoted_and_a_quick_one_is_not():
    _clear()
    for _ in range(10):
        app._record_latency("slowpid", "m", 90000.0)
        app._record_latency("fastpid", "m", 1200.0)
    assert app._latency_penalty("slowpid", "m") == app._LATENCY_WEIGHT
    # Being fast earns NOTHING: this is penalty-only, speed is not quality.
    assert app._latency_penalty("fastpid", "m") == 0.0
    _clear()


def test_latency_penalty_never_exceeds_its_weight():
    _clear()
    for _ in range(10):
        app._record_latency("glacial", "m", 10 * 60 * 1000.0)
    assert app._latency_penalty("glacial", "m") == app._LATENCY_WEIGHT
    _clear()


def test_the_ewma_follows_a_provider_that_speeds_up():
    _clear()
    for _ in range(10):
        app._record_latency("p", "m", 80000.0)
    slow = app._measured_latency_ms("p", "m")
    for _ in range(15):
        app._record_latency("p", "m", 1000.0)
    fast = app._measured_latency_ms("p", "m")
    assert fast < slow, (slow, fast)
    # ...and the penalty it was carrying is released once it is genuinely quick.
    assert app._latency_penalty("p", "m") == 0.0
    _clear()


def test_a_failed_or_streaming_hop_is_never_timed():
    """Failing fast is not being fast, and a stream returns at headers -- timing
    either would average two different quantities into one number."""
    _clear()
    resp = mock.Mock(status_code=429)
    with mock.patch.object(app, "_upstream_chat", return_value=resp), \
            mock.patch.object(app, "_is_sub", return_value=False):
        app._dispatch_chat("p", {"model": "m"}, False)
    assert app._measured_latency_ms("p", "m") is None

    ok = mock.Mock(status_code=200)
    with mock.patch.object(app, "_upstream_chat", return_value=ok), \
            mock.patch.object(app, "_is_sub", return_value=False):
        app._dispatch_chat("p", {"model": "m"}, True)     # streaming
    assert app._measured_latency_ms("p", "m") is None
    _clear()


def test_a_delivered_nonstreaming_hop_is_timed():
    _clear()

    def slow_upstream(_pid, _payload, _stream):
        time.sleep(0.01)                      # a real, measurable duration
        return mock.Mock(status_code=200)

    with mock.patch.object(app, "_upstream_chat", side_effect=slow_upstream), \
            mock.patch.object(app, "_is_sub", return_value=False):
        for _ in range(app._LATENCY_MIN_SAMPLES):
            app._dispatch_chat("p", {"model": "m"}, False)
    measured = app._measured_latency_ms("p", "m")
    assert measured is not None and measured >= 5.0, measured
    _clear()


def test_agentic_score_subtracts_the_latency_penalty():
    _clear()
    entry = (100.0, "slowpid", "m")
    with mock.patch.object(app, "_sustain_penalty", return_value=0.0), \
            mock.patch.object(app, "_tool_dialect_penalty", return_value=0.0), \
            mock.patch.object(app, "_reliability_penalty", return_value=0.0):
        assert app._agentic_score(entry) == 100.0        # unknown -> untouched
        for _ in range(10):
            app._record_latency("slowpid", "m", 90000.0)
        assert app._agentic_score(entry) == 100.0 - app._LATENCY_WEIGHT
    _clear()
