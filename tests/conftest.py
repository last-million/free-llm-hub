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

RESOLVER: netresolve wraps the process-wide socket.getaddrinfo. Importing app
must never install that wrapper during the suite (this module is imported before
any test module imports app, so the boot switch is flipped here), and whatever a
test installs or patches is put back after it: socket.getaddrinfo is the
original resolver again after EVERY test.
"""
import socket
import sys
import time

import pytest

import netresolve

_REAL_GETADDRINFO = socket.getaddrinfo
netresolve.BOOT_INSTALL = False


def _restore_the_real_resolver():
    netresolve.uninstall()
    if socket.getaddrinfo is not _REAL_GETADDRINFO:
        socket.getaddrinfo = _REAL_GETADDRINFO
    netresolve._orig = netresolve._import_time
    netresolve._prev = None
    netresolve._sleep = time.sleep
    netresolve._now = time.time
    netresolve.reset()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    """The global resolver and netresolve's own state are the originals after
    EVERY test, whatever it installed, patched or left behind. A hook wrapper,
    not a fixture: monkeypatch undoes its patches in its own finalizer, which
    a fixture of ours would run BEFORE (so a test that patched
    socket.getaddrinfo while the wrapper was installed had the wrapper put
    back by monkeypatch after our cleanup)."""
    yield
    _restore_the_real_resolver()


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
                 "_swarm_member_fail", "_empty_200", "_pair_rest", "_local_net_marks",
                 "_PLANNER_FAILED"):
        other = getattr(mod, name, None) if mod else None
        if isinstance(other, dict):
            other.clear()
    # PROVIDER FAIRNESS: the routing-pick log and the in-flight counter are
    # process-wide too; one test's picks would make the next one's tie-break
    # prefer a different provider (load-aware spread inside the top band).
    for name in ("_ROUTE_LOG", "_PROVIDER_INFLIGHT"):
        other = getattr(mod, name, None) if mod else None
        if other is not None and hasattr(other, "clear"):
            other.clear()
    # DEAD KEYS (quota.mark_key_dead / note_key_auth_failure): tests share fake
    # key strings ("k1", "sk-test"...), so one test's dead mark would drop that
    # key from usable_keys in the next.
    q = sys.modules.get("quota")
    for name in ("_KEY_DEAD", "_KEY_AUTH_STRIKES"):
        ledger = getattr(q, name, None) if q else None
        if isinstance(ledger, dict):
            ledger.clear()


def _clear_version_rank():
    """VERSION RANKING anchors (app._rank_anchors): the newest reachable release
    per family+tier is cached for 30 s, so one test's fake fleet would otherwise
    cap the next test's scores."""
    mod = sys.modules.get("app")
    reset = getattr(mod, "_rank_reset", None) if mod else None
    if callable(reset):
        reset()


@pytest.fixture(autouse=True)
def _reset_version_rank():
    _clear_version_rank()
    yield
    _clear_version_rank()


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
def _no_live_governor(monkeypatch):
    """The live RAM/CPU governor (lowres.GOV) samples the REAL processes of
    this PC from a daemon thread and carries state between runs; a test that
    asserts a concurrency must not depend on it. Its own tests build a
    Governor with fakes (tests/test_multi_parallel_models.py)."""
    import lowres
    monkeypatch.setattr(lowres, "acquire_monitor", lambda: None)
    monkeypatch.setattr(lowres, "release_monitor", lambda: None)
    lowres.GOV.reset()
    yield
    lowres.GOV.reset()


@pytest.fixture(autouse=True)
def _no_real_tunnel(monkeypatch):
    """PUBLISH: no test may start a real cloudflared, spawn the publish timer
    thread, probe a real port or download anything. Every default effect of
    publish.Manager is replaced by one that fails loudly or finds nothing; the
    tests that exercise the engine (tests/test_publish_engine.py) inject a fake
    cloudflared script, a fake clock and a fake downloader through the
    Manager's constructor, which bypasses these module-level defaults."""
    try:
        import publish
    except Exception:                                            # noqa: BLE001
        yield
        return

    def _refuse(*_a, **_k):
        raise AssertionError("a test tried to start a real cloudflared; inject "
                             "a fake spawn into publish.Manager")

    def _no_download(*_a, **_k):
        raise AssertionError("a test tried to download something from the network")

    monkeypatch.setattr(publish, "_spawn", _refuse)
    monkeypatch.setattr(publish, "_find_cloudflared", lambda bin_dir=None: None)
    monkeypatch.setattr(publish, "_https_fetch", _no_download)
    monkeypatch.setattr(publish, "_http_probe", lambda port, timeout=2.0: False)
    monkeypatch.setattr(publish, "_iter_processes", lambda: iter(()))
    monkeypatch.setattr(publish.Manager, "_ensure_timer", lambda self: None)
    if hasattr(publish, "_auto_timer"):     # the automatic cloudflared install's timer
        def _no_auto_timer(*_a, **_k):
            raise AssertionError("a test tried to arm the real automatic cloudflared "
                                 "install; inject timer= into publish.AutoInstaller")
        monkeypatch.setattr(publish, "_auto_timer", _no_auto_timer)
    yield
    for _name, _method in (("auto", "stop"), ("default", "shutdown")):
        try:
            getattr(getattr(publish, _name), _method)()
        except Exception:                                        # noqa: BLE001
            pass


@pytest.fixture(autouse=True)
def _no_real_heartbeat_scheduler(monkeypatch):
    """The heartbeat scheduler is a daemon thread that, once the kill switch is
    on, starts REAL Multi runs on the owner's projects. No test may launch it;
    tests/test_heartbeat.py drives heartbeat.Scheduler.tick() by hand with
    fakes and an injected clock."""
    try:
        import app
        monkeypatch.setattr(app, "_start_heartbeat_scheduler", lambda: None)
    except Exception:                                            # noqa: BLE001
        pass
    yield


@pytest.fixture(autouse=True)
def _no_real_network_pauses(monkeypatch):
    """The chain walk's pause after 2+ hosts fail to resolve (1 s, then 3 s) is
    a real sleep in production; no test waits for it. tests/test_dns_resilience
    installs a recording sleep of its own."""
    import app
    monkeypatch.setattr(app, "_LOCAL_NET_SLEEP", lambda seconds: None)
    yield


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
    # Live window steering asks which UA-less CLIs (pi, openclaw, hermes,
    # kimi) are wired to the hub by READING the owner's real configs: a test's
    # reported usage must not depend on that machine state.
    # tests/test_live_window_steering.py runs the real one on temp homes.
    monkeypatch.setattr(app, "_steer_connected_unidentifiable", lambda: ())
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


# Tests written against the old tool-turn RACE (best-of-N fan-out): its
# member grace, its "doomed provider" cut, its winner labels, its ranking of
# members. The race is still shipped -- flag `tool_turn_race` (default OFF
# since 2026-10-04, see "Roles instead of racing" in AGENTS.md and
# tests/test_tool_turn_roles.py) -- so these keep testing it, with the flag on.
_RACE_TESTS = {
    "test_client_disconnect_stops_work": {
        "test_a_client_that_leaves_the_tool_fan_out_stops_every_member"},
    "test_swarm_falls_back_instead_of_503": {
        "test_the_fan_out_rejects_a_member_that_refuses",
        "test_it_no_longer_waits_for_every_member",
        "test_the_grace_starts_on_a_tool_call_or_a_checked_text_answer",
        "test_a_member_that_raises_does_not_lose_the_others"},
    "test_swarm_fanout_members": {
        "test_members_of_a_provider_that_failed_this_race_are_not_waited_on",
        "test_a_sick_pair_is_never_dispatched_end_to_end"},
    "test_swarm_picks_models_that_answer": {
        "test_the_fanout_ranks_its_candidates_instead_of_taking_chain_order"},
    "test_swarm_stops_waiting": {
        "test_a_close_second_still_gets_in",
        "test_a_tool_call_inside_the_grace_still_wins_over_prose",
        "test_a_member_the_hub_stopped_waiting_for_is_not_shown_as_no_answer"},
    "test_swarm_tool_turns": {"test_among_equals_the_stronger_model_wins"},
    "test_the_swarm_decides_how_wide": {"test_the_tool_path_passes_the_real_difficulty"},
    "test_tool_turn_reliability": {"test_fan_out_members_see_the_clis_system_prompt"},
    "test_tool_turn_timeouts": {
        "test_a_tool_call_inside_the_grace_still_beats_an_earlier_text",
        "test_a_minority_text_answer_keeps_the_tool_grace"},
    "test_no_tools_claim": {"test_a_fan_out_member_claiming_no_tools_loses_its_slot"},
}


@pytest.fixture(autouse=True)
def _race_tests_keep_the_race(request, monkeypatch):
    mod = getattr(request.node, "module", None)
    name = getattr(request.node, "originalname", None) or request.node.name
    if mod is not None and name in _RACE_TESTS.get(mod.__name__.rsplit(".", 1)[-1], ()):
        import app
        monkeypatch.setattr(app, "_tool_turn_race_on", lambda: True)
    yield


@pytest.fixture(autouse=True)
def _team_notes_off_unless_tested(request, monkeypatch):
    """TEAM NOTES (parallel specialists) are default ON; every other test file
    is about its own subject and must not see extra specialist calls."""
    mod = getattr(request.node, "module", None)
    if mod is None or mod.__name__.rsplit(".", 1)[-1] != "test_tool_turn_specialists":
        import app
        monkeypatch.setattr(app, "_team_flag_on", lambda: False)
    yield


@pytest.fixture(autouse=True)
def _weak_models_not_force_verified(request, monkeypatch):
    """Tests written with low fake scores (10-100) are about their own subject;
    the "weak actor is always verified" wiring has its own test file."""
    mod = getattr(request.node, "module", None)
    if mod is not None and mod.__name__.rsplit(".", 1)[-1] == "test_wiring_roles_guides_pipelines":
        yield
        return
    import app
    monkeypatch.setattr(app, "_model_is_weak", lambda pid, model: False)
    yield


@pytest.fixture(autouse=True)
def _team_and_verifier_ledgers_start_empty():
    """The specialist / verifier success ledgers are process-wide memory seeded
    from the owner's turn-roles.jsonl; every test starts with empty ledgers and
    never reads the real log."""
    try:
        import app
    except Exception:                                            # noqa: BLE001
        yield
        return
    app._team_stats.clear()
    app._verifier_stats.clear()
    app._team_stats_seeded[0] = True
    yield
    app._team_stats.clear()
    app._verifier_stats.clear()


@pytest.fixture(autouse=True)
def _bandit_tie_break_is_deterministic(request, monkeypatch):
    """The learned tie-breaker (bandit.py) draws random numbers on purpose, so
    any routing test that expects a fixed pick among EQUAL scores would flake
    (MEASURED 2026-10-04: test_model_mode.py::test_the_primary_pick_is_
    unrestricted_under_all failed 4 of 15 runs). Every file sees a zero nudge unless it
    sets the module attribute USES_REAL_BANDIT = True (or the test carries
    @pytest.mark.real_bandit) (an explicit opt-in, not
    a substring guess: a file that merely MENTIONS the bandit was flaky)."""
    mod = getattr(request.node, "module", None)
    if (getattr(mod, "USES_REAL_BANDIT", False)
            or request.node.get_closest_marker("real_bandit")):
        yield
        return
    import app
    monkeypatch.setattr(app, "_bandit_delta", lambda kind, pid, model, base: 0.0)
    yield


@pytest.fixture(autouse=True)
def _graceful_update_stays_in_the_test(monkeypatch):
    """The update drain, the resume plan and the pending-update labels are
    process-wide in app.py: a test that began a drain (or read a marker) must
    not leave /v1 answering 503 for the next one. And no test may ever replace
    the process -- app._do_reexec is the ONE place the hub re-executes itself,
    so it is a tripwire here; tests/test_graceful_update.py swaps in a fake."""
    try:
        import app
    except Exception:                                            # noqa: BLE001
        yield
        return

    def _no_reexec():
        raise AssertionError("a test tried to re-exec the hub (app._do_reexec)")

    def _reset():
        app._UPDATE_DRAIN.end()
        app._UPDATE_PLAN.update({"loaded": False, "plan": None})
        app._UPDATE_LABELS.update({"from": "", "to": "", "reason": "update"})
        app._UPDATE_RESUME_WANTED[0] = True

    monkeypatch.setattr(app, "_do_reexec", _no_reexec)
    _reset()
    yield
    _reset()


_DEPLOY_TEST_FILES = {"test_deploy_perfect", "test_deploy_after_run"}


@pytest.fixture(autouse=True)
def _deploy_features_stay_out(request, monkeypatch, tmp_path):
    """DEPLOY-PERFECT (2026-10-10). The planning-time machine probe runs real
    `--version` commands, the run-end deploy check starts a real preview, and
    a passing check writes the remembered start into the state dir. No test
    file gets any of that: the probe is off everywhere (its tests pass fakes),
    the canonical-start store is a per-test temp file, and the run-end check is
    off except in the deploy test files (which inject their own fakes)."""
    import envprobe
    import workspace
    monkeypatch.setattr(envprobe, "ENABLED", False)
    monkeypatch.setattr(workspace, "CANON_PATH", str(tmp_path / "preview-starts.json"))
    mod = getattr(request.node, "module", None)
    if mod is None or mod.__name__.rsplit(".", 1)[-1] not in _DEPLOY_TEST_FILES:
        try:
            import app
        except Exception:                                        # noqa: BLE001
            yield
            return
        monkeypatch.setattr(app, "_dp_run_check_on", lambda: False)
    yield
