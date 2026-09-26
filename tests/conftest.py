"""Shared test isolation.

app._recent_hop_fail is a process-wide, in-memory ledger (a (provider, model)
that 429'd or ran out its time is ordered last for ten minutes). Chain-loop
tests drive fake providers into exactly those failures, so without a reset a
later test that builds a real chain over the same fake ids would inherit
another test's demotions. Cleared before every test, only if app is loaded.
"""
import sys

import pytest


@pytest.fixture(autouse=True)
def _reset_recent_hop_failures():
    mod = sys.modules.get("app")
    ledger = getattr(mod, "_recent_hop_fail", None) if mod else None
    if isinstance(ledger, dict):
        ledger.clear()
    yield
