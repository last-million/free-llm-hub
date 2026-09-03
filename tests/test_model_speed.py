"""p50/p95 and time-to-first-token, because a mean hides what makes a model painful.

Second of the three picked from the freellmapi comparison. The hub already
measured latency, as a single exponentially-weighted mean per (provider, model).
That is the right shape for ROUTING -- cheap, one number, forgets old behaviour
-- and the wrong shape for a person asking "is this model actually fast": a p50
of four seconds with a p95 of ninety is a very different model to use than a
steady twenty, and both average out the same.

TTFT was not measured at all, and the old code said why in a comment: for a
streaming hop the request call returns once headers are in, so timing it there
would have mixed time-to-first-byte into the same average as a non-streaming
total. That reasoning was right about the average and wrong to conclude the
number was worthless -- kept apart, it is the number that actually describes a
stream, and it is what a user perceives as "slow".

It is taken at the moment _peek_until_content succeeds, which the hub already
does on every streamed 200 to tell a real answer from an empty one. So it costs
nothing, and it is first CONTENT rather than first byte -- providers send a role
delta, keep-alives, and sometimes an entire reasoning block before any words.
"""
from unittest import mock

import pytest

import app as A
import config


@pytest.fixture
def client():
    return A.app.test_client()


def _ctl():
    return {"X-Free-LLM-Hub-Token": config.ensure_control_token(),
            "X-Free-LLM-Hub": "dashboard"}


@pytest.fixture(autouse=True)
def clean():
    with A._outcome_lock:
        A._speed.clear()
        A._ttft.clear()
    yield
    with A._outcome_lock:
        A._speed.clear()
        A._ttft.clear()


# --------------------------------------------------------------------------- #
# The percentile itself
# --------------------------------------------------------------------------- #

def test_nearest_rank_returns_a_value_that_was_actually_measured():
    """No interpolation: with at most 64 samples, averaging two real
    measurements invents a number nobody observed."""
    v = [10, 20, 30, 40]
    assert A._percentile(v, 50) in v
    assert A._percentile(v, 95) in v


def test_the_percentiles_are_ordered():
    v = list(range(1, 101))
    assert A._percentile(v, 50) < A._percentile(v, 95) <= max(v)


def test_a_single_sample_is_both_percentiles():
    assert A._percentile([7], 50) == 7 and A._percentile([7], 95) == 7


def test_no_samples_is_none_not_zero():
    """An unmeasured model is not a fast one."""
    assert A._percentile([], 50) is None


# --------------------------------------------------------------------------- #
# Collecting samples
# --------------------------------------------------------------------------- #

def test_recording_a_latency_also_keeps_the_raw_sample():
    """The EWMA and the distribution come from the same measurement."""
    for ms in (100, 200, 300):
        A._record_latency("p", "m", ms)
    prof = A._speed_profile("p", "m")
    assert prof["samples"] == 3
    assert prof["p50_ms"] in (100, 200, 300)


def test_zero_and_negative_durations_are_dropped():
    A._record_speed_sample(A._speed, "p", "m", 0)
    A._record_speed_sample(A._speed, "p", "m", -5)
    assert A._speed_profile("p", "m")["samples"] == 0


def test_samples_are_bounded():
    for i in range(A._SPEED_SAMPLES * 3):
        A._record_speed_sample(A._speed, "p", "m", i + 1)
    assert A._speed_profile("p", "m")["samples"] == A._SPEED_SAMPLES


def test_the_newest_samples_are_the_ones_kept():
    """A model that just got faster must be able to show it."""
    for i in range(A._SPEED_SAMPLES * 2):
        A._record_speed_sample(A._speed, "p", "m", 10000 if i < A._SPEED_SAMPLES else 5)
    assert A._speed_profile("p", "m")["p95_ms"] <= 5


def test_ttft_is_kept_apart_from_total_duration():
    """Folding them together is exactly the mistake the old comment refused to
    make; keeping both is the point of this change."""
    A._record_latency("p", "m", 9000)
    A._record_ttft("p", "m", 120)
    prof = A._speed_profile("p", "m")
    assert prof["p50_ms"] == 9000
    assert prof["ttft_p50_ms"] == 120


def test_an_unknown_model_profiles_as_nothing_known():
    prof = A._speed_profile("nope", "nope")
    assert prof["p50_ms"] is None and prof["samples"] == 0


# --------------------------------------------------------------------------- #
# TTFT off a live response
# --------------------------------------------------------------------------- #

def test_note_ttft_uses_the_clock_started_before_the_request():
    resp = mock.Mock()
    resp._hub_started = A.time.perf_counter() - 0.25
    A._note_ttft(resp, "p", "m")
    prof = A._speed_profile("p", "m")
    assert prof["ttft_samples"] == 1
    assert 200 <= prof["ttft_p50_ms"] < 5000


def test_a_response_without_a_clock_is_ignored_not_guessed():
    """A sub-* relay hop never went through the timed dispatch."""
    A._note_ttft(mock.Mock(spec=[]), "p", "m")
    assert A._speed_profile("p", "m")["ttft_samples"] == 0


def test_the_streaming_dispatch_starts_the_clock():
    src = open("app.py", encoding="utf-8").read()
    body = src.split("def _dispatch_chat(", 1)[1][:1600]
    assert "_hub_started = started" in body


def test_every_streaming_route_records_it():
    """Three routes peek for first content; all three must record, or the
    numbers silently describe only one protocol."""
    src = open("app.py", encoding="utf-8").read()
    assert src.count("_note_ttft(resp, hop_pid, hop_model)") == 3


def test_it_is_recorded_only_after_real_content_arrives():
    """Before the peek succeeds the hop may still fall through to another
    provider, and timing an answer that never came is meaningless."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index("_note_ttft(resp, hop_pid, hop_model)")
    before = src[max(0, i - 700):i]
    assert 'if status != "content":' in before
    assert "continue" in before


# --------------------------------------------------------------------------- #
# The route
# --------------------------------------------------------------------------- #

def test_it_reports_what_has_been_measured(client):
    A._record_latency("alpha", "m1", 500)
    A._record_ttft("alpha", "m1", 80)
    rows = client.get("/api/model-speed", headers=_ctl()).get_json()["models"]
    assert len(rows) == 1
    assert rows[0]["id"] == "alpha/m1"
    assert rows[0]["p50_ms"] == 500 and rows[0]["ttft_p50_ms"] == 80


def test_unmeasured_models_are_omitted_not_zeroed(client):
    rows = client.get("/api/model-speed", headers=_ctl()).get_json()["models"]
    assert rows == []


def test_the_slowest_come_first(client):
    """The reason to open this page is to find what is hurting."""
    A._record_latency("a", "fast", 100)
    A._record_latency("b", "slow", 9000)
    rows = client.get("/api/model-speed", headers=_ctl()).get_json()["models"]
    assert [r["model"] for r in rows] == ["slow", "fast"]


def test_a_model_measured_only_while_streaming_still_appears(client):
    """Agent traffic streams, so TTFT is often the only sample a model has."""
    A._record_ttft("a", "streamed", 300)
    rows = client.get("/api/model-speed", headers=_ctl()).get_json()["models"]
    assert [r["model"] for r in rows] == ["streamed"]
    assert rows[0]["p50_ms"] is None


def test_it_says_the_numbers_are_since_the_last_restart(client):
    """These live in memory, unlike the EWMA that routing persists, so the
    scope has to be stated rather than implied."""
    body = client.get("/api/model-speed", headers=_ctl()).get_json()
    assert "since the hub last started" in body["note"]


def test_it_is_control_gated(client):
    assert client.get("/api/model-speed").status_code == 401


# --------------------------------------------------------------------------- #
# ...and it is visible, not just measured
# --------------------------------------------------------------------------- #

def _template():
    with open("templates/index.html", encoding="utf-8") as f:
        return f.read()


def test_the_settings_model_table_shows_it():
    html = _template()
    assert "function loadSdSpeed()" in html
    assert "sdSpeedBadge(m.id)" in html


def test_it_loads_alongside_the_model_list():
    html = _template()
    i = html.index("function loadSdModels(")
    assert "loadSdSpeed();" in html[i:i + 900]


def test_the_badge_prefers_time_to_first_token():
    """Agent traffic streams, and TTFT is what a person experiences as slow."""
    html = _template()
    i = html.index("function sdSpeedBadge(")
    body = html[i:i + 1400]
    assert "ttft_p50_ms" in body
    assert "first token" in body


def test_a_slow_tail_is_marked():
    html = _template()
    i = html.index("function sdSpeedBadge(")
    assert "slow" in html[i:i + 1400]
    assert ".sd-speed.slow{" in html


def test_a_model_with_no_samples_shows_no_badge():
    """An unmeasured model must not look like a fast one."""
    html = _template()
    i = html.index("function sdSpeedBadge(")
    assert "if (!r) return '';" in html[i:i + 400]
