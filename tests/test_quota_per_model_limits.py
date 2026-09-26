"""Per-(provider, model) limits learned from headers, real reset rules, and the
no-free-tier / models-exhausted status fields.

THE BUG THESE PIN DOWN
----------------------
Groq and Cerebras meter every model on its OWN budget and report that model's
bucket in the x-ratelimit-* headers. The hub stored every reading provider-wide,
so one model's spent daily bucket made status(groq) read exhausted and
is_exhausted() pulled every sibling model -- each with a full budget -- out of
routing. The reading now lives per model; the provider keeps serving the rest.
"""
import json
import os
import tempfile
import time

import pytest

import quota


@pytest.fixture
def fresh_quota():
    saved = (dict(quota._STATE), dict(quota._MODEL_STATE),
             dict(quota._MODEL_THROTTLE), dict(quota._DYNAMIC),
             dict(quota._MODEL_DYNAMIC), dict(quota._TOKENS),
             quota._PERSIST_PATH, quota._persist_last, quota._key_counter)
    for d in (quota._STATE, quota._MODEL_STATE, quota._MODEL_THROTTLE,
              quota._DYNAMIC, quota._MODEL_DYNAMIC, quota._TOKENS):
        d.clear()
    quota._PERSIST_PATH = None
    quota._persist_last = 0.0
    quota._key_counter = None
    try:
        yield
    finally:
        for d, old in zip((quota._STATE, quota._MODEL_STATE, quota._MODEL_THROTTLE,
                           quota._DYNAMIC, quota._MODEL_DYNAMIC, quota._TOKENS),
                          saved[:6]):
            d.clear()
            d.update(old)
        quota._PERSIST_PATH, quota._persist_last, quota._key_counter = saved[6:]


def _spent(reset="30s"):
    return {"x-ratelimit-remaining-requests": "0",
            "x-ratelimit-limit-requests": "1000",
            "x-ratelimit-reset-requests": reset}


def test_one_spent_groq_model_leaves_the_provider_and_its_siblings(fresh_quota):
    quota.observe_headers("groq", _spent(), model="llama-3.3-70b-versatile")

    ms = quota.model_status("groq", "llama-3.3-70b-versatile")
    assert ms["exhausted"] is True
    assert ms["source"] == "headers"
    assert ms["limit"] == 1000 and ms["remaining"] == 0
    assert ms["resets_at"] and ms["resets_at"] > time.time()

    assert quota.model_status("groq", "llama-3.1-8b-instant")["exhausted"] is False
    st = quota.status("groq")
    assert st["exhausted"] is False            # provider still routable
    assert st["models_exhausted"] == 1
    assert quota.is_model_exhausted("groq", "llama-3.3-70b-versatile") is True
    assert quota.is_model_exhausted("groq", "llama-3.1-8b-instant") is False


def test_account_wide_provider_headers_stay_provider_wide(fresh_quota):
    """OpenRouter's free pool is ONE account-wide budget: unchanged behaviour."""
    quota.observe_headers("openrouter", _spent(), model="some/model:free")
    assert quota.status("openrouter")["exhausted"] is True
    assert quota._MODEL_DYNAMIC == {}


def test_no_model_id_falls_back_to_the_provider_slot(fresh_quota):
    quota.observe_headers("groq", _spent())
    assert quota.status("groq")["exhausted"] is True


def test_cerebras_day_headers_are_read_per_model(fresh_quota):
    quota.observe_headers("cerebras", {
        "x-ratelimit-limit-requests-day": "14400",
        "x-ratelimit-remaining-requests-day": "0",
        "x-ratelimit-reset-requests-day": "33011.38",
    }, model="gpt-oss-120b")
    ms = quota.model_status("cerebras", "gpt-oss-120b")
    assert ms["exhausted"] is True and ms["limit"] == 14400
    assert 33000 < ms["resets_at"] - time.time() <= 33012
    assert quota.status("cerebras")["exhausted"] is False


def test_model_needs_every_key_spent(fresh_quota):
    quota.set_key_counter(lambda pid: 2)
    quota.observe_headers("groq", _spent(), key="k-one", model="m")
    assert quota.model_status("groq", "m")["exhausted"] is False   # key two unheard
    quota.observe_headers("groq", _spent(), key="k-two", model="m")
    assert quota.model_status("groq", "m")["exhausted"] is True


def test_model_comes_back_when_its_reset_passes(fresh_quota):
    quota.observe_headers("groq", _spent(), model="m")
    for d in quota._MODEL_DYNAMIC["groq"]["m"].values():
        d["reset_at"] = time.time() - 1
    assert quota.model_status("groq", "m")["exhausted"] is False
    assert quota.status("groq")["models_exhausted"] == 0


def test_throttled_model_counts_as_exhausted(fresh_quota):
    quota.mark_model_throttled("google", "gemini-x", 60)
    st = quota.status("google")
    assert st["models_exhausted"] == 1
    assert "gemini-x" in quota.exhausted_models("google")


def test_no_free_tier_is_exposed_not_hidden(fresh_quota):
    st = quota.status("morph")
    assert st["exhausted"] is True             # kept exhausted by design
    assert st["no_free_tier"] is True
    assert quota.status("groq")["no_free_tier"] is False
    assert quota.status("some-unknown-provider")["no_free_tier"] is False


def test_no_free_tier_survives_key_scaling(fresh_quota):
    quota.set_key_counter(lambda pid: 4)
    assert quota.status("morph")["no_free_tier"] is True


def test_reset_kinds():
    assert quota.reset_kind("google") == "tz:America/Los_Angeles"
    assert quota.reset_kind("groq") == "rolling-24h"
    assert quota.reset_kind("openrouter") == "utc-midnight"
    for pid, (kind, _v) in quota.RESET_RULES.items():
        assert kind in ("tz", "rolling")


def test_rolling_window_anchors_on_first_request(fresh_quota, monkeypatch):
    t0 = 1_800_000_123.0                       # deliberately not a midnight
    clock = [t0]
    monkeypatch.setattr(quota.time, "time", lambda: clock[0])

    quota.record("groq", "m")
    start, reset = quota._window_bounds("day", t0, "groq")
    assert start == t0 and reset == t0 + 86400

    clock[0] = t0 + 3600
    quota.record("groq", "m")
    st = quota.status("groq")
    assert st["used"] == 2
    assert st["resets_at"] == int(t0 + 86400)
    assert st["reset_kind"] == "rolling-24h"

    clock[0] = t0 + 86400 + 5                  # 24 h after the FIRST request
    assert quota.status("groq")["used"] == 0
    quota.record("groq", "m")
    assert quota._STATE["groq"]["window_start"] == t0 + 86400 + 5


def test_utc_default_unchanged_for_unlisted_providers():
    now = 1_800_000_123.0
    start, reset = quota._window_bounds("day", now, "openrouter")
    assert start % 86400 == 0 and reset - start == 86400


def test_model_dynamic_persists_and_old_files_still_load(fresh_quota):
    fd, path = tempfile.mkstemp(prefix="quota-test-", suffix=".json")
    os.close(fd)
    try:
        quota.observe_headers("groq", _spent("1h"), model="m")
        quota._PERSIST_PATH = path
        quota.save_state()
        with open(path, encoding="utf-8") as f:
            assert "model_dynamic" in json.load(f)

        quota._MODEL_DYNAMIC.clear()
        quota._load_state(path)
        assert quota.model_status("groq", "m")["exhausted"] is True

        # A state file written before this field existed loads cleanly.
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"state": {}, "dynamic": {}}, f)
        quota._MODEL_DYNAMIC.clear()
        quota._load_state(path)
        assert quota._MODEL_DYNAMIC == {}
    finally:
        quota._PERSIST_PATH = None
        try:
            os.unlink(path)
        except OSError:
            pass


def test_stale_model_reading_is_not_revived_on_load(fresh_quota):
    fd, path = tempfile.mkstemp(prefix="quota-test-", suffix=".json")
    os.close(fd)
    try:
        old = time.time() - 2 * quota._DYNAMIC_TTL
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"model_dynamic": {"groq": {"m": {"": {
                "remaining": 0, "limit": 5, "reset_at": None, "seen": old}}}}}, f)
        quota._load_state(path)
        assert quota._MODEL_DYNAMIC == {}
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
