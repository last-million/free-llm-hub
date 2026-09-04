"""Swarm slots go to models with a measured record, not to untried ones.

ASKED 2026-09-04: "why some models have no answer in swarm ... can this be
fixed yes or no?" -- and, of the four causes, this is the half that could be.

_swarm_reliability returns a flat 0.5 for "no idea". That is the right NUMBER:
an unmeasured model must not be judged. But the ranking split candidates on the
number alone, and 0.5 clears the 0.4 health bar -- so "never tried" landed in
the same bucket as "measured, delivers", and an untried model could take a slot
ahead of one known to answer.

For a fallback chain that hardly matters: an unknown that fails costs one retry.
For a swarm it costs a whole slot out of five, and the slot is the scarce thing.
Untried models are exactly where "no answer" comes from.

The hub was already recording who delivers (_record_outcome on every hop). The
swarm was reading the score without asking whether any evidence stood behind it.

Demoted, never dropped: a model has to get its first slot somewhere, or nothing
new is ever measured and the ranking freezes around whatever was tried first.
"""
from unittest import mock

import app as A


def _rank(monkeypatch, records, cands):
    """records: {(pid, model): reliability or None for 'no evidence'}"""
    monkeypatch.setattr(A, "_swarm_reliability",
                        lambda p, m: 0.5 if records.get((p, m)) is None
                        else records[(p, m)])
    monkeypatch.setattr(A, "_swarm_has_record",
                        lambda p, m: records.get((p, m)) is not None)
    monkeypatch.setattr(A, "_quota_headroom", lambda p: 1.0)
    monkeypatch.setattr(A, "_agentic_score", lambda t: 0.0)
    monkeypatch.setattr(A, "_benchmark_score", lambda p, m: 0.0)
    monkeypatch.setattr(A, "_swarm_fanout", lambda: 3)
    return A._swarm_rank(cands)


PROVEN = ("good", "m")
UNTRIED = ("new", "m")
BAD = ("bad", "m")


def test_a_measured_model_outranks_an_untried_one(monkeypatch):
    """The whole point. Both used to score 0.5-or-better and sort by strength
    alone, so the untried one could win the slot."""
    order = _rank(monkeypatch, {PROVEN: 0.8, UNTRIED: None}, [UNTRIED, PROVEN])
    assert order[0] == PROVEN


def test_an_untried_model_still_outranks_a_known_bad_one(monkeypatch):
    """No evidence is better than evidence of failing."""
    order = _rank(monkeypatch, {UNTRIED: None, BAD: 0.1}, [BAD, UNTRIED])
    assert order[0] == UNTRIED


def test_the_three_tiers_are_in_order(monkeypatch):
    order = _rank(monkeypatch, {PROVEN: 0.9, UNTRIED: None, BAD: 0.05},
                  [BAD, UNTRIED, PROVEN])
    assert order == [PROVEN, UNTRIED, BAD]


def test_an_untried_model_is_never_dropped(monkeypatch):
    """It has to get a first slot somewhere, or nothing new is ever measured
    and the ranking freezes around whatever happened to be tried first."""
    order = _rank(monkeypatch, {PROVEN: 0.9, UNTRIED: None}, [PROVEN, UNTRIED])
    assert UNTRIED in order


def test_untried_models_alone_still_fill_the_swarm(monkeypatch):
    """A fresh install has no records at all; it must not produce an empty
    swarm."""
    a, b = ("p1", "m"), ("p2", "m")
    order = _rank(monkeypatch, {a: None, b: None}, [a, b])
    assert len(order) == 2


# --------------------------------------------------------------------------- #
# Telling "measured" from "never tried"
# --------------------------------------------------------------------------- #

def test_a_model_with_its_own_history_counts_as_measured(monkeypatch):
    monkeypatch.setattr(A, "_reliability", lambda p, m: 0.9)
    assert A._swarm_has_record("p", "m") is True


def test_a_model_with_no_history_anywhere_does_not(monkeypatch):
    monkeypatch.setattr(A, "_reliability", lambda p, m: 0.5)
    monkeypatch.setattr(A, "_provider_outcome_totals", lambda p: None)
    assert A._swarm_has_record("p", "m") is False


def test_a_providers_record_counts_for_its_unmeasured_models(monkeypatch):
    """The relay problem this hub already documents: one model is listed under
    many ids, so per-id evidence is thin while the provider's is not."""
    monkeypatch.setattr(A, "_reliability", lambda p, m: 0.5)
    monkeypatch.setattr(A, "_provider_outcome_totals",
                        lambda p: {"ok": 20, "fail": 2})
    assert A._swarm_has_record("p", "m") is True


def test_a_provider_with_too_little_evidence_does_not_count(monkeypatch):
    monkeypatch.setattr(A, "_reliability", lambda p, m: 0.5)
    monkeypatch.setattr(A, "_provider_outcome_totals",
                        lambda p: {"ok": 1, "fail": 0})
    assert A._swarm_has_record("p", "m") is False


# --------------------------------------------------------------------------- #
# The straggler grace, widened on request
# --------------------------------------------------------------------------- #

def test_the_grace_covers_the_measured_spread():
    """The 25s version answered 1-of-5: members measured at 78s and 111s were
    cut off by a first answer at 5s."""
    assert A._SWARM_STRAGGLER_GRACE >= 80


def test_it_is_still_bounded_by_the_hop_deadline():
    assert A._SWARM_STRAGGLER_GRACE < A._SWARM_TOOL_HOP_DEADLINE
