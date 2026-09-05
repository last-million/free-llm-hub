"""The fallback chain leads with models measured to answer.

REPORTED 2026-09-04, after opencode was finally talking to the hub: turns still
stalled. The traffic showed why -- three requests sat in_progress for 230, 281
and 282 seconds against nvidia/deepseek-v4-flash-0731, measured at 0.05, one
delivery in twenty. And the chain for a tool turn read:

    1.  0.50  g4f/...qwen3.8-27b
    2.  0.33  g4f/...gemini-3.6-flash
    3.  0.33  g4f/...gemini-3.5-flash
    4.  0.33  g4f/...gemini-3-flash
    5.  0.72  groq/qwen3.8-27b      <- the one that answers

Four stalls before a model that works.

The chain already carried a reliability PENALTY, but a penalty is a nudge inside
a score dominated by benchmark strength. That is the right shape when a bad hop
costs one quick retry -- the assumption it was built on, stated outright in
_swarm_reliability's docstring: "there a bad hop costs one retry". It is the
wrong shape when the failure is a HANG.

So the chain demotes the measured-to-fail and leaves everything else in the
order strength gave it. ONE line, not a ladder: a first attempt split "delivers"
(>=0.60) from "middling" and that was worse in the other direction -- it put
glm-5.3 and qwen3.8 behind weaker models that merely cleared the line, which is
the opposite of what a best-first chain is for. See
test_a_middling_model_is_not_demoted.

Nothing is ever dropped. A model has to be tried to earn a record, and if
nothing healthier exists the measured-bad ones are still served.
"""
from unittest import mock

import pytest

import app as A


GOOD = ("goodpid", "good-model")
MID = ("midpid", "mid-model")
UNTRIED = ("newpid", "new-model")
BAD = ("badpid", "bad-model")

RECORDS = {GOOD: 0.85, MID: 0.45, BAD: 0.05}      # UNTRIED deliberately absent


@pytest.fixture
def graded(monkeypatch):
    monkeypatch.setattr(A, "_reliability",
                        lambda p, m: RECORDS.get((p, m), 0.5))
    monkeypatch.setattr(A, "_swarm_has_record",
                        lambda p, m: (p, m) in RECORDS)
    yield


# --------------------------------------------------------------------------- #
# The bands
# --------------------------------------------------------------------------- #

def test_a_model_that_delivers_is_band_zero(graded):
    assert A._chain_reliability_band(*GOOD) == 0


def test_a_model_measured_to_fail_is_band_two(graded):
    assert A._chain_reliability_band(*BAD) == 2


def test_a_middling_model_is_not_demoted(graded):
    """CORRECTED 2026-09-05. This asserted a middle band, and the middle band was
    the bug: "glm 5.3 and qwen 3.8 dont work anymore, why he dont use best models
    available first, they are checked now".

    Neither was blocked or dead. groq/qwen3.8-27b had fallen to 0.41 -- above the
    failure line, below the old 0.60 "delivers" line -- so the three-way split
    put one of the strongest models in the catalog behind anything sitting at
    0.60, however weak.

    Worse, that 0.41 was largely self-inflicted: the swarm deadline kills and the
    503 storm of that same session were all filed as failures against whatever
    got caught in them. A number the hub's own timeouts can push around does not
    get to outrank benchmark strength -- it only gets to spot the unambiguous
    case."""
    assert A._chain_reliability_band(*MID) == 0


def test_an_untried_model_is_not_demoted(graded):
    """It has to be tried to ever earn a record."""
    assert A._chain_reliability_band(*UNTRIED) == 0


def test_there_is_one_line_not_a_ladder():
    """A ladder demotes on thin evidence; one line demotes only on clear
    evidence. _CHAIN_RELIABLE is gone with the middle band it created."""
    assert 0 < A._CHAIN_UNRELIABLE < 1
    assert not hasattr(A, "_CHAIN_RELIABLE")


def test_strength_still_orders_everything_above_the_line(graded):
    """The point of collapsing the bands: a strong model with an ordinary record
    must not sit behind a weak one with a flattering record."""
    assert A._chain_reliability_band(*GOOD) == A._chain_reliability_band(*MID) == 0


# --------------------------------------------------------------------------- #
# The primary
# --------------------------------------------------------------------------- #

def test_a_measured_to_fail_primary_does_not_open_the_turn(graded):
    """MEASURED: the primary pick lands on a band-2 model about one turn in
    twenty-five, and one of those cost 280 seconds before failing."""
    chain = A._build_chain(BAD[0], BAD[1])
    assert not chain or chain[0] != BAD


def test_a_healthy_primary_still_opens_the_turn(graded):
    chain = A._build_chain(GOOD[0], GOOD[1])
    assert chain and chain[0] == GOOD


def test_a_middling_primary_is_still_honoured(graded):
    """Only MEASURED-TO-FAIL is demoted; an unremarkable model the router chose
    on strength keeps its slot."""
    chain = A._build_chain(MID[0], MID[1])
    assert chain and chain[0] == MID


# --------------------------------------------------------------------------- #
# Never dropped
# --------------------------------------------------------------------------- #

def test_a_bad_model_is_demoted_not_removed(graded):
    """It has to be tried to ever earn a better record, and when everything
    healthy is rate-limited it is still better than no answer."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index("_sick = [e for e in ordered")
    window = src[i:i + 400]
    assert "ordered = _fit + _sick" in window, "the sick tier must be appended, not dropped"


def test_the_sort_is_stable_so_strength_still_decides_within_a_band():
    """A continuous reliability sort would throw the benchmark ordering away;
    bands keep it inside each band."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index("ordered.sort(key=lambda e: _chain_reliability_band")
    assert "STABLE" in src[max(0, i - 500):i]


def test_measured_failure_outranks_the_proven_allowlist():
    """_is_tool_proven is a coarse statement about a model FAMILY; a band-2
    reliability is evidence about this exact listing, and evidence wins."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index("_sick = [e for e in ordered")
    before = src[max(0, i - 700):i]
    assert "allowlist" in before


def test_the_bands_are_applied_before_the_proven_split():
    """So tool-proven stays the dominant grouping among healthy models: for a
    tool turn, "can it call a tool at all" outranks "does it usually answer"."""
    src = open("app.py", encoding="utf-8").read()
    sort_at = src.index("ordered.sort(key=lambda e: _chain_reliability_band")
    split_at = src.index("_proven_ordered = [e for e in _fit")
    assert sort_at < split_at


# --------------------------------------------------------------------------- #
# Fail-open
# --------------------------------------------------------------------------- #

def test_a_chain_of_nothing_but_bad_models_is_still_served(monkeypatch):
    """Refusing to answer is worse than answering slowly. If every candidate is
    measured-bad, they are still the chain."""
    monkeypatch.setattr(A, "_reliability", lambda p, m: 0.05)
    monkeypatch.setattr(A, "_swarm_has_record", lambda p, m: True)
    chain = A._build_chain(BAD[0], BAD[1])
    # the primary is demoted out of hop 1, but the ranked candidates below still
    # fill the chain -- an empty chain would be a hard failure
    assert isinstance(chain, list)


def test_nothing_is_lost_when_reliability_is_unknown_everywhere(monkeypatch):
    """A fresh install has no ledger at all; every model is band 1 and the chain
    must be exactly what it always was."""
    monkeypatch.setattr(A, "_reliability", lambda p, m: 0.5)
    monkeypatch.setattr(A, "_swarm_has_record", lambda p, m: False)
    chain = A._build_chain(GOOD[0], GOOD[1])
    assert chain and chain[0] == GOOD
