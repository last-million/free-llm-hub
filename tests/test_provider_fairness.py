"""Provider fairness (2026-10-07).

Owner: "it doesn't use all providers equally; groq and other providers are
almost never used." MEASURED over 7 days of tool turns: nvidia served 29.6% of
all turns (tried 1076x, median 65.7s) while it piled up. The fix is a LOAD-aware
tie-break INSIDE the top band (_fair_spread_band): among models already within
_AUTO_TOP_BAND of the best (the owner's "equally good"), prefer the provider
carrying the least current load. It must NEVER break "best available first" -- a
134 model may never beat an available 138 via spreading -- and must always fail
open (only ever narrow the band to a subset of equally-good candidates).
"""
import os
import tempfile
import threading

import pytest

import app
import config


@pytest.fixture(autouse=True)
def _clean_fairness_state():
    """The in-flight counter and the routing-pick log are process-wide; clear
    them so each test starts from an idle fleet."""
    with app._inflight_lock:
        app._PROVIDER_INFLIGHT.clear()
    with app._route_log_lock:
        app._ROUTE_LOG.clear()
    yield
    with app._inflight_lock:
        app._PROVIDER_INFLIGHT.clear()
    with app._route_log_lock:
        app._ROUTE_LOG.clear()


@pytest.fixture
def isolated_config(monkeypatch):
    root = tempfile.mkdtemp(prefix="hub-pytest-")
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", os.path.join(root, "state", "config.json"))
    yield root


@pytest.fixture
def identity_scores(monkeypatch):
    monkeypatch.setattr(app, "_agentic_score", lambda c, *a, **k: c[0])


# --------------------------------------------------------------------------- #
# The band rule is never broken by spreading.
# --------------------------------------------------------------------------- #
def test_a_134_never_beats_an_available_138_via_spreading(identity_scores):
    pool = [(138.0, "nvidia", "z-ai/glm-5.3"),
            (137.9, "g4f", "srv:z-ai/glm-5.3"),
            (134.0, "groq", "openai/gpt-oss-120b"),   # 4 points back -> not in band
            (130.0, "dahl", "mid/tier")]
    band = app._auto_top_band(pool)
    out = app._fair_spread_band(band, est=90000)
    ids = {c[2] for c in out}
    assert "openai/gpt-oss-120b" not in ids
    assert "mid/tier" not in ids
    assert ids <= {"z-ai/glm-5.3", "srv:z-ai/glm-5.3"}


def test_fair_spread_only_ever_narrows_its_input(identity_scores):
    band = [(138.0, "nvidia", "a"), (137.8, "g4f", "b"), (137.5, "groq", "c")]
    out = app._fair_spread_band(band, est=90000)
    assert set(out) <= set(band)          # never introduces anything new


# --------------------------------------------------------------------------- #
# Load-based tie-break moves the pick to the less-loaded provider.
# --------------------------------------------------------------------------- #
def test_load_tie_break_prefers_the_least_loaded_provider():
    band = [(138.0, "nvidia", "m1"), (137.9, "g4f", "m2")]
    # nvidia is carrying two open requests; g4f is idle.
    app._inflight_inc("nvidia")
    app._inflight_inc("nvidia")
    out = app._fair_spread_band(band, est=90000)
    assert {c[1] for c in out} == {"g4f"}


def test_a_cold_fleet_is_left_untouched():
    band = [(138.0, "nvidia", "m1"), (137.9, "g4f", "m2")]
    out = app._fair_spread_band(band, est=90000)
    # Nobody is loaded -> the whole band reaches the weighted pick as before.
    assert set(out) == set(band)


# --------------------------------------------------------------------------- #
# Small requests let the fast provider compete (this is where groq belongs);
# never a model whose window is too small (the band is window-filtered already,
# and the function only narrows -- it cannot add one).
# --------------------------------------------------------------------------- #
def test_small_request_prefers_the_fast_provider(monkeypatch):
    monkeypatch.setattr(app, "_is_fast", lambda pid, m: pid in ("groq", "cerebras"))
    band = [(138.0, "nvidia", "slow/model"),
            (137.9, "groq", "openai/gpt-oss-120b")]
    out = app._fair_spread_band(band, est=4000)      # < _FAIR_SMALL_EST (8000)
    assert {c[1] for c in out} == {"groq"}


def test_big_request_does_not_force_the_fast_provider(monkeypatch):
    monkeypatch.setattr(app, "_is_fast", lambda pid, m: pid == "groq")
    band = [(138.0, "nvidia", "m1"), (137.9, "groq", "m2")]
    out = app._fair_spread_band(band, est=90000)     # big -> fast rule skipped
    assert {c[1] for c in out} == {"nvidia", "groq"}


# --------------------------------------------------------------------------- #
# Per-provider in-flight soft cap.
# --------------------------------------------------------------------------- #
def test_soft_cap_yields_to_a_provider_under_the_cap(monkeypatch):
    monkeypatch.setattr(config, "get_setting",
                        lambda name, default=None: 3 if name == "provider_inflight_soft_cap" else default)
    band = [(138.0, "nvidia", "m1"), (137.9, "g4f", "m2")]
    for _ in range(3):                      # nvidia is AT the cap
        app._inflight_inc("nvidia")
    out = app._fair_spread_band(band, est=90000)
    assert {c[1] for c in out} == {"g4f"}


def test_soft_cap_fails_open_when_everyone_is_over_it(monkeypatch):
    monkeypatch.setattr(config, "get_setting",
                        lambda name, default=None: 1 if name == "provider_inflight_soft_cap" else default)
    band = [(138.0, "nvidia", "m1"), (137.9, "g4f", "m2")]
    app._inflight_inc("nvidia")
    app._inflight_inc("g4f")                # both over the cap of 1
    out = app._fair_spread_band(band, est=90000)
    # Both equally loaded and equally capped -> the band survives (fail open).
    assert {c[1] for c in out} == {"nvidia", "g4f"}


# --------------------------------------------------------------------------- #
# A provider carrying >= 50% of the last 15 min of picks yields.
# --------------------------------------------------------------------------- #
def test_share_hog_yields_to_an_equally_good_model_elsewhere():
    for _ in range(9):                      # nvidia ~90% of recent picks
        app._note_route_pick("nvidia")
    app._note_route_pick("g4f")
    assert app._provider_recent_share("nvidia") >= 0.5
    band = [(138.0, "nvidia", "m1"), (137.9, "g4f", "m2")]
    out = app._fair_spread_band(band, est=90000)
    assert {c[1] for c in out} == {"g4f"}


# --------------------------------------------------------------------------- #
# Fail-open and off switch.
# --------------------------------------------------------------------------- #
def test_one_provider_band_is_unchanged():
    band = [(138.0, "nvidia", "m1"), (137.9, "nvidia", "m2")]
    app._inflight_inc("nvidia")
    assert app._fair_spread_band(band, est=90000) == band


def test_fairness_off_returns_the_band_unchanged(monkeypatch):
    monkeypatch.setattr(config, "get_flag",
                        lambda name, default=False: False if name == "provider_fairness" else default)
    band = [(138.0, "nvidia", "m1"), (137.9, "g4f", "m2")]
    app._inflight_inc("nvidia")
    app._inflight_inc("nvidia")
    assert app._fair_spread_band(band, est=90000) == band


# --------------------------------------------------------------------------- #
# In-flight counter correctness under threads.
# --------------------------------------------------------------------------- #
def test_inflight_counter_balances_under_threads():
    start = threading.Barrier(16)
    hi = {"max": 0}
    lock = threading.Lock()

    def worker():
        start.wait()
        for _ in range(200):
            app._inflight_inc("nvidia")
            with lock:
                hi["max"] = max(hi["max"], app._inflight_count("nvidia"))
            app._inflight_dec("nvidia")

    ts = [threading.Thread(target=worker) for _ in range(16)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert app._inflight_count("nvidia") == 0           # every inc was released
    assert hi["max"] >= 1                                # it did rise while held


def test_inflight_never_goes_negative():
    app._inflight_dec("ghost")                           # dec with nothing held
    assert app._inflight_count("ghost") == 0


def test_dispatch_chat_holds_then_releases_a_non_stream_hop(monkeypatch):
    seen = {}

    class _Resp:
        status_code = 200

    def fake_upstream(pid, payload, stream, **kw):
        seen["during"] = app._inflight_count(pid)
        return _Resp()

    monkeypatch.setattr(app, "_upstream_chat", fake_upstream)
    monkeypatch.setattr(app, "_record_latency", lambda *a, **k: None)
    monkeypatch.setattr(app, "_record_long_ctx_speed", lambda *a, **k: None)
    monkeypatch.setattr(app, "_is_sub", lambda pid: False)
    app._dispatch_chat("nvidia", {"model": "m", "messages": []}, False)
    assert seen["during"] == 1                           # counted during the call
    assert app._inflight_count("nvidia") == 0            # released after


def test_dispatch_chat_holds_a_stream_until_the_response_dies(monkeypatch):
    import gc

    class _Resp:                                         # supports weakref
        pass

    monkeypatch.setattr(app, "_upstream_chat", lambda *a, **k: _Resp())
    monkeypatch.setattr(app, "_nb_close", lambda r: r)
    monkeypatch.setattr(app, "_is_sub", lambda pid: False)
    monkeypatch.setattr(app, "_payload_est_tokens", lambda p: 0)
    resp = app._dispatch_chat("nvidia", {"model": "m", "messages": []}, True)
    assert app._inflight_count("nvidia") == 1            # held for the whole stream
    del resp
    gc.collect()
    assert app._inflight_count("nvidia") == 0            # released when it dies


# --------------------------------------------------------------------------- #
# The endpoint.
# --------------------------------------------------------------------------- #
def _hdrs(token=True):
    h = {"X-Free-LLM-Hub": "dashboard"}
    if token:
        h["X-Free-LLM-Hub-Token"] = config.ensure_control_token()
    return h


def test_provider_load_endpoint_is_control_token_gated(isolated_config):
    config.ensure_control_token()
    c = app.app.test_client()
    assert c.get("/api/provider-load").status_code == 401
    r = c.get("/api/provider-load", headers=_hdrs())
    assert r.status_code == 200


def test_provider_load_endpoint_reports_load(isolated_config, monkeypatch):
    config.ensure_control_token()
    monkeypatch.setattr(app, "_enabled_keyed", lambda: ["nvidia", "g4f", "groq"])
    app._inflight_inc("nvidia")
    app._inflight_inc("nvidia")
    for _ in range(3):
        app._note_route_pick("nvidia")
    app._note_route_pick("g4f")
    c = app.app.test_client()
    j = c.get("/api/provider-load", headers=_hdrs()).get_json()
    rows = {r["provider"]: r for r in j["providers"]}
    assert rows["nvidia"]["inflight"] == 2
    assert rows["nvidia"]["routed_15m"] == 3
    assert rows["g4f"]["routed_15m"] == 1
    assert j["total_inflight"] == 2
    assert j["routed_15m"] == 4
    assert abs(rows["nvidia"]["share_15m"] - 0.75) < 1e-6
    assert j["fairness_on"] is True
    assert j["soft_cap"] >= 1
