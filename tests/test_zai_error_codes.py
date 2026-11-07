"""Z.AI's documented 429 business codes are read for what they say.

Z.AI (pid "glm") sends balance, plan, rate and overload errors all as HTTP 429 with a
numeric `code` (https://docs.z.ai/api-reference/api-code, read 2026-11-07). Before this,
"Insufficient balance or no resource package" (1113) read as a 429 of unstated scope: the
key was skipped until the provider's day reset and the whole provider was throttled, so a
request for one PAID GLM model took the FREE GLM Flash models out with it.
"""
import json

import pytest

import app as A
import quota

NOW = 1_800_000_000.0


def _body(code, msg="x"):
    return json.dumps({"error": {"code": code, "message": msg}})


def _cls(code, pid="glm", msg="x"):
    return quota.classify_429(pid, {}, _body(code, msg), "glm-5.3", now=NOW)


@pytest.mark.parametrize("code", ["1113", "1311", 1113])
def test_a_model_that_needs_money_parks_only_that_model(code):
    info = _cls(code, msg="Insufficient balance or no resource package. Please recharge.")
    assert info["scope"] == "model" and info["window"] == "day"
    assert info["reset_at"] and info["reset_at"] > NOW


def test_overload_is_capacity_not_a_failure():
    info = _cls("1305", msg="The service may be temporarily overloaded, please try again later")
    assert info["source"] == "concurrency"
    assert info["scope"] == "model" and info["window"] == "minute"


def test_a_request_rate_limit_is_a_minute():
    info = _cls("1302", msg="Rate limit reached for requests")
    assert info["window"] == "minute" and info["scope"] is None


@pytest.mark.parametrize("code", ["1308", "1316", "1318", "1320"])
def test_a_usage_cap_that_resets_within_hours_is_an_hour(code):
    info = _cls(code)
    assert info["scope"] == "account" and info["window"] == "hour"


@pytest.mark.parametrize("code", ["1309", "1310", "1313", "1314", "1315", "1317", "1319", "1321"])
def test_an_expired_plan_or_fair_use_block_is_a_day(code):
    info = _cls(code)
    assert info["scope"] == "account" and info["window"] == "day"


def test_another_provider_with_the_same_number_is_not_read_as_zai():
    assert _cls("1113", pid="dahl")["scope"] is None
    assert _cls("1305", pid="dahl")["source"] != "concurrency"


@pytest.mark.parametrize("code", [None, True, "9999", {"x": 1}])
def test_an_unknown_or_odd_code_keeps_the_old_reading(code):
    info = _cls(code)
    assert info["source"] == "generic"


def test_an_unreadable_body_never_raises():
    assert quota.classify_429("glm", {}, "<html>oops", "glm-5.3", now=NOW)["source"] == "generic"


# --------------------------------------------------------------------------- #
# What the hub then does with it
# --------------------------------------------------------------------------- #

@pytest.fixture
def marks(monkeypatch):
    got = {"provider": [], "model": []}
    monkeypatch.setattr(quota, "mark_throttled", lambda pid, secs, **k: got["provider"].append(pid))
    monkeypatch.setattr(quota, "mark_model_throttled",
                        lambda pid, m, secs, **k: got["model"].append((pid, m, k.get("until"))))
    return got


class _Resp:
    headers = {}
    text = ""


def test_the_free_models_keep_serving_when_a_paid_one_needs_money(marks):
    info = _cls("1113")
    A._apply_429("glm", "glm-5.3", info, _Resp())
    assert marks["provider"] == []
    assert marks["model"] == [("glm", "glm-5.3", info["reset_at"])]
    assert A._key_cooldown_for_429(info, _Resp()) <= 60


def test_the_measured_old_reading_benched_the_whole_provider(marks):
    # Without the code: an unstated-scope 429 throttles the provider (the bug).
    info = quota.classify_429("glm", {}, json.dumps({"error": {"message": "Insufficient balance"}}),
                              "glm-5.3", now=NOW)
    A._apply_429("glm", "glm-5.3", info, _Resp())
    assert marks["provider"] == ["glm"]


def test_an_expired_plan_benches_the_provider(marks):
    A._apply_429("glm", "glm-4.7-flash", _cls("1309"), _Resp())
    assert marks["provider"] == ["glm"]
