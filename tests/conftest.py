"""Suite-wide isolation for app.py's module-level JUNK BENCH.

The bench (app._junk_bench_note) is fed from every junk outcome -- the answer
gate, the stream gate and the canary -- so tests that record a couple of junk
answers each for the same pair would otherwise add up ACROSS tests and bench
it three tests later, changing that test's scores. Cleared around every test,
and only when app is already imported: a test that never touches app must not
pay its import.
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


@pytest.fixture(autouse=True)
def _isolate_junk_bench():
    _clear_bench()
    yield
    _clear_bench()
