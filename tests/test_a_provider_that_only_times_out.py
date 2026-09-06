"""A provider that answers nothing is eventually parked -- but only when the
evidence is unambiguously about the provider.

MEASURED against nvidia directly, bypassing the hub: a 996-byte request timed
out at 25.4s and again at 300.6s; an 840,676-byte one at 301.4s. It answers
nothing at any size. Yet it benchmarks 134, carries a provider bias, and kept
winning the primary slot on every large request.

It was invisible to every learned signal. Both existing provider breakers run
only AFTER a response object exists, and a read timeout raises at requests.post
into a handler that records nothing at all -- no quota, no outcome, no provider
result. So its reliability stayed at the neutral 0.5, its band stayed 0, and the
"do not seed a measured-to-fail primary" rule never fired.

THE GATES MATTER MORE THAN THE BREAKER. An over-eager version of this is worse
than the defect, and the first draft was refuted on exactly that: it counted
requests.ConnectTimeout, which is a SUBCLASS of Timeout, so a thirty-second wifi
drop would have parked the entire fleet for half an hour -- and the "a success
resets it" defence is inert during an outage, because nothing succeeds.
"""
import time
from unittest import mock

import pytest
import requests

import app as A


@pytest.fixture(autouse=True)
def clean():
    A._provider_timeout_fail.clear()
    A._dead_providers.pop("pX", None)
    A._last_fleet_2xx[0] = time.time()      # the fleet is alive by default
    yield
    A._provider_timeout_fail.clear()
    A._dead_providers.pop("pX", None)


def _strike(n, exc=None, pid="pX"):
    """n INDEPENDENT requests, since strikes are deduplicated per request."""
    exc = exc or requests.exceptions.ReadTimeout("quiet")
    for _ in range(n):
        with A.app.test_request_context("/"):
            A._note_provider_timeout(pid, exc)


# --------------------------------------------------------------------------- #
# The hierarchy the whole design rests on
# --------------------------------------------------------------------------- #

def test_a_connect_timeout_is_a_timeout_but_not_a_read_timeout():
    """This is why testing for ReadTimeout is enough to exclude the local case,
    and why the first draft -- which tested for Timeout -- was fatal."""
    CT = requests.exceptions.ConnectTimeout
    assert issubclass(CT, requests.Timeout)
    assert not issubclass(CT, requests.exceptions.ReadTimeout)


# --------------------------------------------------------------------------- #
# It parks a provider that really is silent
# --------------------------------------------------------------------------- #

def test_enough_read_timeouts_park_the_provider():
    _strike(A._PROVIDER_TIMEOUT_THRESHOLD)
    assert A._is_provider_dead("pX")


def test_one_short_of_the_threshold_does_not():
    _strike(A._PROVIDER_TIMEOUT_THRESHOLD - 1)
    assert not A._is_provider_dead("pX")


def test_the_park_expires_so_it_is_re_probed():
    _strike(A._PROVIDER_TIMEOUT_THRESHOLD)
    assert 0 < A._dead_providers["pX"] - time.time() <= A._PROVIDER_DEAD_TTL


def test_one_real_answer_clears_the_streak():
    _strike(A._PROVIDER_TIMEOUT_THRESHOLD - 1)
    A._note_provider_result("pX", ok=True)
    _strike(A._PROVIDER_TIMEOUT_THRESHOLD - 1)
    assert not A._is_provider_dead("pX")


# --------------------------------------------------------------------------- #
# GATE 1: a local network problem is not a provider problem
# --------------------------------------------------------------------------- #

def test_a_connect_timeout_never_counts():
    """"Could not reach the host" is a fact about this machine -- a wifi drop, a
    VPN reconnect, a laptop waking, a dead proxy. Counting it would park the
    whole fleet on a brief outage."""
    _strike(A._PROVIDER_TIMEOUT_THRESHOLD * 3,
            requests.exceptions.ConnectTimeout("unreachable"))
    assert not A._is_provider_dead("pX")


def test_a_plain_connection_error_never_counts():
    _strike(A._PROVIDER_TIMEOUT_THRESHOLD * 3,
            requests.exceptions.ConnectionError("refused"))
    assert not A._is_provider_dead("pX")


def test_an_outage_cannot_park_the_fleet():
    """The scenario that refuted the first draft, end to end: every provider
    unreachable at once, many times over."""
    for pid in ("pA", "pB", "pC", "pD"):
        A._dead_providers.pop(pid, None)
        _strike(10, requests.exceptions.ConnectTimeout("down"), pid=pid)
    assert not any(A._is_provider_dead(p) for p in ("pA", "pB", "pC", "pD"))


# --------------------------------------------------------------------------- #
# GATE 2: one request cannot supply the whole quorum
# --------------------------------------------------------------------------- #

def test_many_hops_in_ONE_request_count_once():
    """A single turn calls the same provider several times -- several of its
    models in one chain, plus the whole-chain retry -- and would otherwise
    convict it single-handedly."""
    with A.app.test_request_context("/"):
        for _ in range(A._PROVIDER_TIMEOUT_THRESHOLD * 4):
            A._note_provider_timeout("pX", requests.exceptions.ReadTimeout("quiet"))
    assert not A._is_provider_dead("pX")


def test_separate_requests_do_count():
    _strike(A._PROVIDER_TIMEOUT_THRESHOLD)
    assert A._is_provider_dead("pX")


# --------------------------------------------------------------------------- #
# GATE 3: if nothing anywhere answers, the problem is here
# --------------------------------------------------------------------------- #

def test_nothing_is_parked_while_the_fleet_is_silent():
    A._last_fleet_2xx[0] = 0.0            # nothing has ever answered
    _strike(A._PROVIDER_TIMEOUT_THRESHOLD * 3)
    assert not A._is_provider_dead("pX")


def test_a_success_anywhere_re_arms_the_breaker():
    A._last_fleet_2xx[0] = 0.0
    _strike(A._PROVIDER_TIMEOUT_THRESHOLD * 2)
    assert not A._is_provider_dead("pX")
    A._note_provider_result("pOTHER", ok=True)     # a DIFFERENT provider answered
    _strike(A._PROVIDER_TIMEOUT_THRESHOLD)
    assert A._is_provider_dead("pX")


def test_a_stale_success_does_not_count_as_liveness():
    A._last_fleet_2xx[0] = time.time() - A._PROVIDER_DEAD_TTL - 60
    _strike(A._PROVIDER_TIMEOUT_THRESHOLD * 2)
    assert not A._is_provider_dead("pX")


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #

def test_all_three_client_walks_report_timeouts():
    src = open("app.py", encoding="utf-8").read()
    assert src.count("_note_provider_timeout(hop_pid, exc)") == 3


def test_a_success_marks_the_fleet_alive():
    src = open("app.py", encoding="utf-8").read()
    i = src.index("def _note_provider_result(")
    assert "_note_fleet_alive()" in src[i:i + 1200]
