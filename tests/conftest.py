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
    # Tool-turn ledgers (per-pair TTFT/outcomes, per-relay-server failures).
    for name in ("_tool_ttft", "_tool_outcomes", "_relay_tool_fail",
                 "_swarm_member_fail"):
        other = getattr(mod, name, None) if mod else None
        if isinstance(other, dict):
            other.clear()
    # DEAD KEYS (quota.mark_key_dead / note_key_auth_failure): tests share fake
    # key strings ("k1", "sk-test"...), so one test's dead mark would drop that
    # key from usable_keys in the next.
    q = sys.modules.get("quota")
    for name in ("_KEY_DEAD", "_KEY_AUTH_STRIKES"):
        ledger = getattr(q, name, None) if q else None
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
    # Optional params a provider refused by name (prompt_cache_key etc.).
    getattr(mod, "_PARAM_REJECTED", {}).clear()


@pytest.fixture(autouse=True)
def _reset_thinking_ledgers():
    _clear_thinking_ledgers()
    yield
    _clear_thinking_ledgers()


@pytest.fixture(autouse=True)
def _steady_machine(monkeypatch):
    """Low-resource mode reads the REAL machine; pin a roomy one so a test that
    runs while this PC is short of RAM (or on a weak CI box) keeps the
    multi-session concurrency it asserts. tests/test_low_resource.py
    overrides it."""
    import lowres
    lowres._CACHE.update(at=0.0, value=None)
    monkeypatch.setattr(lowres, "_read_machine",
                        lambda: {"total_gb": 32.0, "free_gb": 16.0, "cores": 8})
    yield
    lowres._CACHE.update(at=0.0, value=None)


@pytest.fixture(autouse=True)
def _stop_never_touches_real_clis(monkeypatch):
    """POST /api/runtime/stop disconnects every CLI wired to the hub
    (app._disconnect_all_clis) and a boot reconnects them -- both read and
    WRITE the owner's real CLI config files. No test may do that;
    tests/test_stop_disconnects_clis.py checks the real functions on fakes."""
    import app
    monkeypatch.setattr(app, "_disconnect_all_clis",
                        lambda: {"disconnected": [], "failed": []})
    monkeypatch.setattr(app, "_reconnect_clis_after_stop", lambda: [])
    # The declared-window resync (boot pass + periodic check) rewrites the
    # window fields of every CLI wired to the hub -- the owner's real files.
    # tests/test_declared_window_providers.py runs the real one on temp homes.
    monkeypatch.setattr(app, "_resync_declared_windows", lambda: [])
    monkeypatch.setattr(app, "_start_declared_window_resync", lambda: None)
    yield


@pytest.fixture(autouse=True)
def _aa_scores_stay_put(monkeypatch, tmp_path_factory):
    """No test fetches benchmark scores or writes the real cache.

    MEASURED 2026-09-30: a test that showed discovery an unknown model id made
    app._maybe_recheck_aa_for_unknown start _aa_refresh_once on a thread -- a
    real GET to OpenRouter's catalog -- which then overwrote the owner's
    ~/.free-llm-hub/aa_scores.json and left 214 real scores in memory, so a
    later test's scores depended on test ORDER (tests/test_benchmark_scoring.py
    failed after tests/test_openrouter_free_and_space_bunny.py).
    tests/test_aa_unknown_recheck.py patches _aa_refresh_once itself."""
    import app
    before = app._aa_scores
    monkeypatch.setattr(app, "_aa_refresh_once", lambda: None)
    monkeypatch.setattr(app, "AA_SCORE_CACHE_PATH",
                        str(tmp_path_factory.getbasetemp() / "aa_scores.json"))
    yield
    app._aa_scores = before


@pytest.fixture(autouse=True)
def _no_multi_intent_model(monkeypatch):
    """app._multi_intent_by_model asks a real model "WORK or CHAT?" before a
    Multi run; tests get "no verdict" (the language-independent fallback) and
    tests/test_multi_french_work.py patches in the verdicts it checks."""
    import app
    monkeypatch.setattr(app, "_multi_intent_by_model", lambda text: None)
    yield


@pytest.fixture(autouse=True)
def _category_evidence_yields_to_patched_patterns(monkeypatch):
    """app._category_by_evidence (2026-09-29) adds top-band tool-capable
    models to coding/swarm/... Tests that DEFINE membership by replacing
    model_categories.matches (a fleet where every model scores 134) mean
    exactly that membership, so evidence stands aside for them only.
    tests/test_category_evidence.py leaves the patterns alone and keeps it."""
    import app
    import model_categories
    real_matches = model_categories.matches
    real_evidence = app._category_by_evidence

    def evidence(key, pid, model):
        if model_categories.matches is not real_matches:
            return False
        return real_evidence(key, pid, model)
    monkeypatch.setattr(app, "_category_by_evidence", evidence)
    yield
