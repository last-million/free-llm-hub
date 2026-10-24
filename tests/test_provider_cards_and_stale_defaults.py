"""Provider cards judge the LIVE catalog, and the stale defaults are fixed (2026-10-24).

REPORTED by the owner: ZenMux, OpenCode Zen, Kilo Code, G4F and Dahl all read "all
models dead". MEASURED the same day against the providers themselves:

  * a provider CARD never does network discovery, so it judged every provider by the
    registry's built-in default ids -- renamed away on all five (Kilo Code's
    step-3.7-flash:free, ZenMux's kimi-k2.7-code-free / glm-5.2-free, Dahl's
    Kimi-K2.6, OpenCode's deepseek-v4-flash-free) -- even when the hub held a fresh
    live catalog. Kilo Code (HTTP 200 on recursive/flint-v1.0:free) and G4F were
    serving tool turns all day while their cards were red;
  * OpenCode Zen is genuinely unusable: a live '-free' id gets HTTP 403 FreeTierError
    "can only be used from within OpenCode" with every key;
  * Dahl's two keys are spent (HTTP 402 "available tokens exhausted"): mint a new one;
  * ZenMux has 1 working key of 5 (the others are PAYG keys: 403 "no permission").
Together AI was removed at the owner's request: no free tier since 2026-08-30.
"""
import time

import pytest

import app as A
import providers as P

STALE = {
    "kilocode": "stepfun/step-3.7-flash:free",
    "zenmux": "z-ai/glm-5.2-free",
    "dahl": "moonshotai/Kimi-K2.6",
}


@pytest.fixture
def clean(monkeypatch):
    monkeypatch.setattr(A, "_model_cache", {})
    monkeypatch.setattr(A, "_dead_models", {})
    yield


def _kill(pid, model):
    with A._dead_lock:
        A._dead_models[(pid, model)] = time.time() + 3600


def test_a_card_uses_the_cached_live_list_instead_of_the_stale_defaults(clean):
    A._model_cache["kilocode"] = (time.time(), ["recursive/flint-v1.0:free", "x/y:free"])
    assert A._card_free_models("kilocode", False) == ["recursive/flint-v1.0:free", "x/y:free"]


def test_without_a_fresh_cache_the_card_falls_back_to_the_defaults(clean):
    assert A._card_free_models("kilocode", False) == P.PROVIDERS["kilocode"]["default_free_models"]
    A._model_cache["kilocode"] = (time.time() - A._CARD_CACHE_MAX_AGE - 5, ["old:free"])
    assert A._card_free_models("kilocode", False) == P.PROVIDERS["kilocode"]["default_free_models"]
    A._model_cache["kilocode"] = (time.time(), [])           # an empty discovery is no discovery
    assert A._card_free_models("kilocode", False) == P.PROVIDERS["kilocode"]["default_free_models"]


def test_one_dead_old_id_no_longer_turns_a_working_provider_red(clean):
    _kill("kilocode", STALE["kilocode"])                      # the renamed-away default
    live = ["recursive/flint-v1.0:free", "nvidia/nemotron-3.5-super-vl:free"]
    st = A._provider_out_status("kilocode", P.PROVIDERS["kilocode"], live)
    assert st["status_reason"] == "ok", st
    # ...and it still goes red when EVERY listed model really is dead.
    for m in live:
        _kill("kilocode", m)
    assert A._provider_out_status("kilocode", P.PROVIDERS["kilocode"], live)["status_reason"] \
        == "models_dead"


@pytest.mark.parametrize("pid", sorted(STALE))
def test_the_renamed_away_defaults_are_gone(pid):
    assert STALE[pid] not in P.PROVIDERS[pid]["default_free_models"]
    assert P.PROVIDERS[pid]["default_free_models"], pid       # and something live replaced them


def test_the_new_defaults_are_the_ids_measured_live():
    assert P.PROVIDERS["kilocode"]["default_free_models"] == ["recursive/flint-v1.0:free"]
    assert all(m.endswith("-free") for m in P.PROVIDERS["zenmux"]["default_free_models"])
    # Dahl pins only the id verified to answer; the other two catalog ids wait for a live key.
    assert P.PROVIDERS["dahl"]["default_free_models"] == ["MiniMaxAI/MiniMax-M2.7"]


def test_opencode_zen_is_paid_marked_and_not_recommended():
    p = P.PROVIDERS["opencode-zen"]
    assert p["paid"] is True and p["default_free_models"] == []
    assert not P.is_free_model("opencode-zen", "glm-5.3-flash-free")
    assert not p.get("recommended")
    assert A._provider_quality_score("opencode-zen", p["default_free_models"]) \
        < A._RECOMMENDED_QUALITY_THRESHOLD
    assert "FreeTierError" in p["notes"] and "not the problem" in p["notes"]


def test_together_is_gone_from_the_registry_and_the_badges():
    assert "together" not in P.PROVIDERS
    assert "together" not in A._NEW_PROVIDER_IDS
    assert P.get_provider("together") is None


def test_zenmux_keys_are_created_on_the_pay_as_you_go_page():
    # Owner 2026-10-24: only keys made at /platform/pay-as-you-go work on the free models; the
    # card's "Get a free key" link and its note must send people there.
    z = P.PROVIDERS["zenmux"]
    assert z["signup_url"] == "https://zenmux.ai/platform/pay-as-you-go"
    assert "zenmux.ai/platform/pay-as-you-go" in z["notes"]


def test_opencode_zen_sits_in_the_paid_group_on_the_page():
    # Owner 2026-10-24: its free tier only works inside OpenCode, so it belongs under "Paid".
    src = open("templates/index.html", encoding="utf-8").read()
    assert "var PURE_PAID = { openai: 1, anthropic: 1, higgsfield: 1, 'opencode-zen': 1 };" in src
    assert P.PROVIDERS["opencode-zen"]["paid"] is True
