"""The fallback chain fetched every provider's catalog one at a time.

MEASURED 2026-09-09, 16 providers:

    _build_chain COLD  14.26s
    _build_chain WARM   0.55s

Every request pays that before its FIRST hop is dispatched -- which is most of
what a CLI experiences as the hub being slow to answer, and what had opencode
reporting "Unable to connect. Is the computer able to access the url".

_auto_models is a live, network-bound /models fetch on a cold or expired cache
entry, and _build_chain called it inside its per-provider loop.
_route_by_difficulty already prefetched concurrently for exactly this reason;
the chain never did. After: 3.66s cold, and the chain is byte-identical.

This is deliberately NOT the other available fix. Making _build_chain a
generator, so a request that succeeds on hop 1 never computes the tail at all,
would be faster still -- and every caller indexes and measures the returned
list across some twenty sites and much of this suite. Same data, same order,
only the fetching parallel, is the version that cannot change a routing
decision.
"""
import pytest

import app as A


MSGS = [{"role": "user", "content": "build a landing page"}]

CASES = [
    ("plain small", dict(est=50)),
    ("tools small", dict(est=50, require_tools=True)),
    ("tools 60K", dict(est=60000, require_tools=True)),
    ("huge 162K", dict(est=162710, require_tools=True)),
    ("vision", dict(est=50, require_vision=True)),
    ("pinned", dict(est=50, pinned=True)),
]


@pytest.fixture
def world(monkeypatch):
    """A synthetic fleet, so this asserts the CODE and not today's providers."""
    pids = ["pa", "pb", "pc", "pd"]
    catalog = {p: [p + "-m1", p + "-m2"] for p in pids}
    monkeypatch.setattr(A, "_available_providers", lambda *a, **k: list(pids))
    monkeypatch.setattr(A, "_auto_models", lambda pid: list(catalog[pid]))
    monkeypatch.setattr(A, "_provider_capable", lambda pid, est: est < 100000)
    yield catalog


def test_the_catalogs_are_fetched_concurrently(world, monkeypatch):
    seen = {"n": 0}
    real = A._prefetch_auto_models

    def counted(pids):
        seen["n"] += 1
        return real(pids)

    monkeypatch.setattr(A, "_prefetch_auto_models", counted)
    A._build_chain("pa", "pa-m1", 50, messages=MSGS)
    assert seen["n"] == 1, "the chain must prefetch once, not per provider"


def test_it_does_not_fetch_per_provider_in_the_loop():
    """The shape of the defect: _auto_models called inside the loop."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index("def _build_chain(")
    body = src[i:src.index("\ndef ", i + 10)]
    assert "_prefetch_auto_models(" in body
    assert "for m in _auto_models(pid):" not in body


@pytest.mark.parametrize("label,kw", CASES, ids=[c[0] for c in CASES])
def test_the_chain_is_identical_either_way(world, monkeypatch, label, kw):
    """Same data, same order -- only the fetching is parallel. A prefetch that
    changed a routing decision would be a far worse bug than the latency."""
    real = A._prefetch_auto_models
    monkeypatch.setattr(A, "_prefetch_auto_models",
                        lambda pids: {p: A._auto_models(p) for p in pids})
    sequential = A._build_chain("pa", "pa-m1", messages=MSGS, **kw)
    monkeypatch.setattr(A, "_prefetch_auto_models", real)
    concurrent = A._build_chain("pa", "pa-m1", messages=MSGS, **kw)
    assert sequential == concurrent


def test_the_compaction_tail_reuses_the_same_fetch(world):
    """The too-small providers are walked again at the end. That second pass
    must not re-fetch -- it was the other _auto_models call site."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index("if _too_small and len(chain) < MAX_HOPS:")
    tail = src[i:i + 900]
    assert "_catalogs.get(pid)" in tail
    assert "_auto_models(pid)" not in tail


def test_a_provider_whose_fetch_fails_contributes_nothing(world, monkeypatch):
    """Unchanged from calling it directly: _prefetch_auto_models returns [] for
    a provider that raises, so one bad provider cannot empty the chain."""
    def flaky(pid):
        if pid == "pb":
            raise RuntimeError("down")
        return [pid + "-m1"]
    monkeypatch.setattr(A, "_auto_models", flaky)
    chain = A._build_chain("pa", "pa-m1", 50, messages=MSGS)
    assert chain
    assert not any(p == "pb" for p, _m in chain)
