"""Every other free-tier gateway routes on AVAILABILITY (is it up, does it have
quota). None can route on "did this hop actually deliver" -- a pure proxy never
sees a request through to a usable answer. This hub does, so routing can learn
from real outcomes instead of a hand-typed list going stale (_TOOL_PROVEN is
four hardcoded strings).

THE MEASURED PROBLEM: g4f-nvidia/mistral-medium-3.5-128b ReadTimeout'd ~7min
per attempt, four attempts running (2026-08-03), then ConnectionError + HTTP
524 on consecutive requests (2026-08-05). Every cooldown we added is a SHORT
sideline that expires; nothing remembered "this hop has a bad track record", so
it kept winning chain slots on raw benchmark strength alone.

DELIBERATELY PENALTY-ONLY: a perfect record earns no bonus (promoting a
mediocre-but-reliable model over a stronger one is a real regression risk, and
this is a reliability signal, not a quality one), and an unknown hop scores
exactly as it did before this existed.
"""
import time

import pytest

import app


@pytest.fixture(autouse=True)
def clean_outcomes():
    """Each test starts from an empty ledger -- these are module-level dicts."""
    with app._outcome_lock:
        app._outcomes.clear()
    yield
    with app._outcome_lock:
        app._outcomes.clear()


# --------------------------------------------------------------------------- #
# _reliability / _reliability_penalty
# --------------------------------------------------------------------------- #

def test_an_unknown_hop_is_exactly_neutral():
    """The whole safety argument: a model the hub has never routed to must
    score EXACTLY as it did before this feature existed."""
    assert app._reliability("never", "seen") == 0.5
    assert app._reliability_penalty("never", "seen") == 0.0


def test_one_failure_does_not_condemn_a_model():
    """Laplace smoothing on purpose: (0+1)/(1+2) = 0.33, not 0. A single blip
    nudges; it must not bench a model outright."""
    app._record_outcome("p", "m", False)
    penalty = app._reliability_penalty("p", "m")
    assert 0 < penalty < app._OUTCOME_WEIGHT / 2


def test_repeated_failure_earns_a_real_demotion():
    for _ in range(10):
        app._record_outcome("g4f-nvidia", "mistral-medium-3.5-128b", False)
    assert app._reliability_penalty("g4f-nvidia", "mistral-medium-3.5-128b") > 6.0


def test_a_perfect_record_never_earns_a_bonus():
    """Penalty-only. A reliability signal must not promote a weaker model over
    a genuinely stronger one."""
    for _ in range(20):
        app._record_outcome("groq", "llama", True)
    assert app._reliability_penalty("groq", "llama") == 0.0


def test_a_mixed_record_is_neutral_not_punished():
    for _ in range(5):
        app._record_outcome("mix", "m", True)
        app._record_outcome("mix", "m", False)
    assert app._reliability_penalty("mix", "m") == 0.0


def test_a_recovered_provider_can_climb_back_out():
    """The counter-halving exists so a hop that was broken for an hour is not
    condemned forever once it genuinely starts answering again."""
    for _ in range(10):
        app._record_outcome("flaky", "m", False)
    condemned = app._reliability_penalty("flaky", "m")
    for _ in range(40):
        app._record_outcome("flaky", "m", True)
    assert app._reliability_penalty("flaky", "m") < condemned
    assert app._reliability_penalty("flaky", "m") == 0.0


def test_stale_evidence_is_ignored():
    app._record_outcome("old", "m", False)
    with app._outcome_lock:
        app._outcomes[("old", "m")]["last"] = time.time() - app._OUTCOME_TTL - 60
    assert app._reliability("old", "m") == 0.5
    assert app._reliability_penalty("old", "m") == 0.0


def test_recording_never_raises_on_junk():
    app._record_outcome(None, None, True)
    app._record_outcome("", "", False)
    app._record_outcome("p", None, True)


# --------------------------------------------------------------------------- #
# Integration with _agentic_score
# --------------------------------------------------------------------------- #

def test_agentic_score_demotes_a_proven_unreliable_hop():
    entry = (100.0, "g4f-nvidia", "mistral-medium-3.5-128b")
    before = app._agentic_score(entry)
    for _ in range(10):
        app._record_outcome("g4f-nvidia", "mistral-medium-3.5-128b", False)
    after = app._agentic_score(entry)
    assert after < before - 6.0, "a repeatedly-failing hop must lose real ground"


def test_agentic_score_is_unchanged_for_an_unrouted_model():
    entry = (100.0, "some-provider", "some-model")
    with app._outcome_lock:
        app._outcomes.clear()
    baseline = app._agentic_score(entry)
    # Another model failing must not affect this one.
    for _ in range(10):
        app._record_outcome("other-provider", "other-model", False)
    assert app._agentic_score(entry) == baseline


# --------------------------------------------------------------------------- #
# Persistence (rides the existing quota extra_dump/extra_load bridge)
# --------------------------------------------------------------------------- #

def test_outcomes_survive_a_restart_round_trip():
    """Earned one real request at a time -- losing it on every restart would
    mean re-learning a bad hop by burning real chain slots on it again."""
    for _ in range(6):
        app._record_outcome("g4f-nvidia", "mistral", False)
    penalty = app._reliability_penalty("g4f-nvidia", "mistral")
    blob = app._dead_state_dump()
    assert "outcomes" in blob and blob["outcomes"]
    with app._outcome_lock:
        app._outcomes.clear()
    assert app._reliability_penalty("g4f-nvidia", "mistral") == 0.0
    app._dead_state_load(blob)
    assert app._reliability_penalty("g4f-nvidia", "mistral") == pytest.approx(penalty)


def test_stale_records_are_not_revived_by_a_restart():
    app._record_outcome("old", "m", False)
    with app._outcome_lock:
        app._outcomes[("old", "m")]["last"] = time.time() - app._OUTCOME_TTL - 60
    blob = app._dead_state_dump()
    assert not blob.get("outcomes"), "TTL-expired evidence must not be persisted"


def test_loading_junk_never_raises():
    app._dead_state_load({"outcomes": {"bad": [1, 2, 3], "p|m": "nope",
                                       "q|n": [1, 2], "r|o": ["a", "b", "c"]}})
    assert app._reliability_penalty("p", "m") == 0.0


# --------------------------------------------------------------------------- #
# Visibility: a demotion nobody can inspect is indistinguishable from a bug.
# --------------------------------------------------------------------------- #

def test_activity_exposes_what_routing_learned():
    for _ in range(8):
        app._record_outcome("g4f-nvidia", "mistral-medium-3.5-128b", False)
    for _ in range(8):
        app._record_outcome("groq", "llama", True)
    # /api/* is control-token gated (see _local_control_guard).
    import config
    token = config.get_control_token()
    headers = {"X-Free-LLM-Hub-Token": token} if token else {}
    body = app.app.test_client().get("/api/activity", headers=headers).get_json()
    learned = body["learned_unreliable"]
    assert any(r["provider"] == "g4f-nvidia" and r["score_penalty"] > 0 for r in learned)
    assert not any(r["provider"] == "groq" for r in learned), \
        "a healthy hop carries no penalty, so it must not be listed as unreliable"
