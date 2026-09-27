"""Suite-wide isolation for app.py's module-level, in-memory ledgers.

JUNK BENCH: the bench (app._junk_bench_note) is fed from every junk outcome --
the answer gate, the stream gate and the canary -- so tests that record a
couple of junk answers each for the same pair would otherwise add up ACROSS
tests and bench it three tests later, changing that test's scores.

RECENT HOP FAILURES: app._recent_hop_fail is a process-wide ledger (a
(provider, model) that 429'd or ran out its time is ordered last for ten
minutes). Chain-loop tests drive fake providers into exactly those failures,
so without a reset a later test that builds a real chain over the same fake
ids would inherit another test's demotions.

Both are cleared around every test, and only when app is already imported: a
test that never touches app must not pay its import.
"""
import sys

import pytest


def _clear_bench():
    app = sys.modules.get("app")
    if app is None or not hasattr(app, "_junk_lock"):
        return
    with app._junk_lock:
        app._junk_events.clear()
        app._junk_bench.clear()


def _clear_recent_hop_failures():
    mod = sys.modules.get("app")
    ledger = getattr(mod, "_recent_hop_fail", None) if mod else None
    if isinstance(ledger, dict):
        ledger.clear()


@pytest.fixture(autouse=True)
def _isolate_junk_bench():
    _clear_bench()
    yield
    _clear_bench()


@pytest.fixture(autouse=True)
def _reset_recent_hop_failures():
    _clear_recent_hop_failures()
    yield


def _clear_thinking_ledgers():
    """THINKING LEDGERS: which (provider, model) reasons (catalog flags and
    runtime evidence) and which rejected reasoning_effort. Tests feed fake
    replies carrying reasoning text and starved stubs, which would otherwise
    give a later test's fake model a reasoning allowance it never earned."""
    mod = sys.modules.get("app")
    if mod is None or not hasattr(mod, "_thinking_lock"):
        return
    with mod._thinking_lock:
        mod._THINKING_CATALOG.clear()
        mod._THINKING_IDENTS.clear()
        mod._THINKING_LEARNED.clear()
        mod._REASONING_REJECTED.clear()


@pytest.fixture(autouse=True)
def _reset_thinking_ledgers():
    _clear_thinking_ledgers()
    yield
    _clear_thinking_ledgers()
