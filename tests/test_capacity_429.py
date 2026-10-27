"""A provider's "this model is full" 429 is capacity, not a failure of the model or the key.

Dahl's DOCUMENTED shape (https://inference.dahl.global/docs/api/, the 429 `model_concurrency` row):
"This model is at concurrency capacity ... Retry after `Retry-After` or switch `model`." Its live body
(2026-10-27) also names the models that are free right now and sends `Retry-After: 10`.

MEASURED on the owner's hub the same day: dahl's two chat models carried 6 ok / 32 fail and 0 / 27 in the
reliability ledger, because (a) the classifier read the capacity 429 as scope "unknown", so
_apply_429 throttled the WHOLE provider for the Retry-After, siblings that were free included, (b) the
only key was marked exhausted, and (c) every non-2xx hop files _record_outcome(False), the strike that
demotes a pair for hours. A pair that answers "busy, retry in 10 s" was ranked like a dead one.

Now the signature is read as what the docs say it is: model scope, minute window, reset from Retry-After,
the key left alone, and no reliability strike (a short-lived mark, the same mechanism a local DNS blip
uses). Ordinary 429s are untouched: they still strike. Every body below is a recorded SHAPE.
"""
import json
import time

import pytest

import app
import quota

DAHL_BODY = {"error": {
    "code": "model_concurrency",
    "message": ("This model is at concurrency capacity. Signed-in and paid accounts are admitted first. "
                "Create an account and link this API key there, retry after Retry-After, or switch model. "
                "Other models available now: MiniMaxAI/MiniMax-M2.7, deepseek-ai/DeepSeek-V4-Flash-0731"),
    "type": "rate_limit_error"}}
PLAIN_BODY = {"error": {"code": "rate_limit_exceeded", "message": "Too many requests, slow down."}}
MODEL = "zai-org/GLM-5.3-Flash"
SIBLING = "MiniMaxAI/MiniMax-M2.7"


class _Resp429:
    status_code = 429

    def __init__(self, body, headers=None):
        self.text = json.dumps(body) if not isinstance(body, str) else body
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text)

    def close(self):
        pass


@pytest.fixture
def fresh_quota():
    saved = (dict(quota._STATE), dict(quota._MODEL_STATE),
             dict(quota._MODEL_THROTTLE), dict(quota._DYNAMIC),
             dict(quota._MODEL_DYNAMIC), dict(quota._TOKENS),
             dict(quota._KEY_COOLDOWN),
             quota._PERSIST_PATH, quota._persist_last, quota._key_counter)
    dicts = (quota._STATE, quota._MODEL_STATE, quota._MODEL_THROTTLE,
             quota._DYNAMIC, quota._MODEL_DYNAMIC, quota._TOKENS, quota._KEY_COOLDOWN)
    for d in dicts:
        d.clear()
    quota._PERSIST_PATH = None
    quota._persist_last = 0.0
    quota._key_counter = None
    app._capacity_marks.clear()
    try:
        yield
    finally:
        for d, old in zip(dicts, saved[:7]):
            d.clear()
            d.update(old)
        quota._PERSIST_PATH, quota._persist_last, quota._key_counter = saved[7:]
        app._capacity_marks.clear()


# --------------------------------------------------------------------------- #
# The classifier
# --------------------------------------------------------------------------- #

def test_model_concurrency_is_read_as_model_scope_with_the_retry_after():
    info = quota.classify_429("dahl", {"Retry-After": "10"}, json.dumps(DAHL_BODY), MODEL)
    assert info["scope"] == "model"
    assert info["window"] == "minute"
    assert info["source"] == "concurrency"
    assert info["seconds"] == pytest.approx(10, abs=1)


def test_without_a_retry_after_it_is_still_a_model_scoped_minute_429():
    info = quota.classify_429("dahl", {}, json.dumps(DAHL_BODY), MODEL)
    assert (info["scope"], info["window"], info["source"]) == ("model", "minute", "concurrency")


def test_an_ordinary_429_is_not_read_as_capacity():
    info = quota.classify_429("somewhere", {}, json.dumps(PLAIN_BODY), MODEL)
    assert info["source"] != "concurrency"
    assert info["scope"] is None


def test_a_numeric_error_code_does_not_break_the_classifier():
    info = quota.classify_429("x", {}, json.dumps({"error": {"code": 429, "message": "slow"}}), MODEL)
    assert info["source"] != "concurrency"


# --------------------------------------------------------------------------- #
# What gets parked
# --------------------------------------------------------------------------- #

def test_only_the_busy_model_is_parked_siblings_and_the_provider_keep_serving(fresh_quota):
    resp = _Resp429(DAHL_BODY, {"Retry-After": "10"})
    app._apply_429("dahl", MODEL, app._classify_429("dahl", resp, MODEL), resp)
    assert quota.is_model_throttled("dahl", MODEL)
    until = quota._MODEL_THROTTLE[("dahl", MODEL)]["throttled_until"]
    assert 0 < until - time.time() <= 15, "parked for the Retry-After, not a flat guess"
    assert not quota.is_model_throttled("dahl", SIBLING), "the model the 429 itself names as free"
    assert (quota._STATE.get("dahl") or {}).get("throttled_until", 0) <= time.time()
    assert not quota.is_exhausted("dahl")


def test_upstream_chat_leaves_the_key_alone_and_marks_capacity(fresh_quota, monkeypatch):
    monkeypatch.setattr(app.config, "get_provider_config",
                        lambda pid: {"api_keys": ["k1"], "enabled": True})
    monkeypatch.setattr(app, "_next_key_start", lambda pid, n: 0)
    monkeypatch.setattr(app.requests, "post",
                        lambda url, **kw: _Resp429(DAHL_BODY, {"Retry-After": "10"}))
    resp = app._upstream_chat("dahl", {"model": MODEL, "messages": []}, False)
    assert resp.status_code == 429
    assert quota.is_model_throttled("dahl", MODEL)
    assert not quota.is_model_throttled("dahl", SIBLING)
    assert not quota.is_exhausted("dahl")
    assert not quota._KEY_COOLDOWN, "the key is fine; the MODEL is full"
    assert app._take_capacity_mark("dahl", MODEL) is True


def test_an_ordinary_429_still_benches_the_key_and_leaves_no_capacity_mark(fresh_quota, monkeypatch):
    monkeypatch.setattr(app.config, "get_provider_config",
                        lambda pid: {"api_keys": ["k1"], "enabled": True})
    monkeypatch.setattr(app, "_next_key_start", lambda pid, n: 0)
    monkeypatch.setattr(app.requests, "post", lambda url, **kw: _Resp429(PLAIN_BODY))
    app._upstream_chat("mistral", {"model": "m", "messages": []}, False)
    assert quota._KEY_COOLDOWN, "old behaviour for every other 429"
    assert app._take_capacity_mark("mistral", "m") is False


# --------------------------------------------------------------------------- #
# The reliability ledger
# --------------------------------------------------------------------------- #

def _fails(pid, model):
    return (app._outcomes.get((pid, model)) or {}).get("fail", 0)


@pytest.fixture
def ledger(fresh_quota):
    keys = [("dahl", "cap-test-a"), ("dahl", "cap-test-b")]
    for k in keys:
        app._outcomes.pop(k, None)
    yield
    for k in keys:
        app._outcomes.pop(k, None)


def test_a_capacity_429_files_no_strike(ledger):
    app._mark_capacity_429("dahl", "cap-test-a")
    app._record_outcome("dahl", "cap-test-a", False)
    assert _fails("dahl", "cap-test-a") == 0


def test_the_mark_covers_one_failure_only(ledger):
    app._mark_capacity_429("dahl", "cap-test-a")
    app._record_outcome("dahl", "cap-test-a", False)
    app._record_outcome("dahl", "cap-test-a", False)          # a real second failure
    assert _fails("dahl", "cap-test-a") == 1


def test_a_failure_with_no_mark_still_strikes(ledger):
    app._record_outcome("dahl", "cap-test-b", False)
    assert _fails("dahl", "cap-test-b") == 1


def test_a_junk_answer_is_never_excused_by_a_mark(ledger):
    app._mark_capacity_429("dahl", "cap-test-a")
    app._record_outcome("dahl", "cap-test-a", False, junk=True)
    assert _fails("dahl", "cap-test-a") >= 1


def test_the_mark_is_per_pair(ledger):
    app._mark_capacity_429("dahl", "cap-test-a")
    app._record_outcome("dahl", "cap-test-b", False)
    assert _fails("dahl", "cap-test-b") == 1


def test_an_expired_mark_excuses_nothing(ledger, monkeypatch):
    app._mark_capacity_429("dahl", "cap-test-a")
    real = time.monotonic
    monkeypatch.setattr(app.time, "monotonic", lambda: real() + app._CAPACITY_MARK_TTL + 1)
    app._record_outcome("dahl", "cap-test-a", False)
    assert _fails("dahl", "cap-test-a") == 1


def test_with_the_flag_off_the_old_strike_comes_back(ledger, monkeypatch):
    real = app.config.get_flag
    monkeypatch.setattr(app.config, "get_flag",
                        lambda k, d=None: False if k == "capacity_429_not_a_strike" else real(k, d))
    app._mark_capacity_429("dahl", "cap-test-a")
    app._record_outcome("dahl", "cap-test-a", False)
    assert _fails("dahl", "cap-test-a") == 1


def test_a_success_is_unaffected_by_a_mark(ledger):
    app._mark_capacity_429("dahl", "cap-test-a")
    app._record_outcome("dahl", "cap-test-a", True)
    assert (app._outcomes.get(("dahl", "cap-test-a")) or {}).get("ok", 0) == 1
