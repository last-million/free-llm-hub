"""Inception (Mercury) and FreeModel are the new providers that stayed (2026-10-23/24).

Facts were READ from the providers' docs and live endpoints (see the comments in
providers.py). Inception is an official API with a public catalog; FreeModel is a
third-party gateway that WORKED once the owner enabled its free tokens with Telegram
verification (a real generation on glm-5.3-flash, canary correct). CodeCraft and Radox AI,
the same white-label kit, answered every request HTTP 402 "Billing verification failed"
until that activation, and the owner removed them. What these tests pin is what could
silently go wrong in the direction of billing the owner or of trusting a name:

  * Inception routes only ZERO-PRICED models as free (mercury-3 today); the billed
    mercury-2 / 2.5 stay pin-only, and the check fails CLOSED without a live list;
  * Mercury's real Artificial Analysis score reaches the plain id its own API uses;
  * FreeModel is an untrusted RELAY: discounted, open-weight ids only as free, a claimed
    Claude/GPT never leads on its name, and its card says to activate with Telegram first;
  * CodeCraft and Radox AI stay gone, with nothing left in the relay tables or badges.
"""
import os
import shutil
import tempfile
from urllib.parse import urlparse

import pytest

import app as A
import providers as P
import quota

REMOVED = ("codecraft", "radoxai")


@pytest.fixture
def isolated_config(monkeypatch):
    d = tempfile.mkdtemp(prefix="hub-pytest-newprov-")
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", os.path.join(d, "state", "config.json"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.mark.parametrize("pid,host,signup_host", [
    ("inception", "api.inceptionlabs.ai", "platform.inceptionlabs.ai"),
    ("freemodel", "freemodel.in", "freemodel.in"),
])
def test_the_entry_is_complete_and_https(pid, host, signup_host):
    p = P.PROVIDERS[pid]
    for key in ("name", "base_url", "models_url", "signup_url", "key_hint",
                "free_filter", "default_free_models", "notes"):
        assert p.get(key), (pid, key)
    for url in ("base_url", "models_url", "signup_url"):
        assert urlparse(p[url]).scheme == "https", (pid, url)
    assert urlparse(p["base_url"]).hostname == host
    assert urlparse(p["models_url"]).hostname == host
    assert urlparse(p["signup_url"]).hostname == signup_host
    assert p["free_filter"] in P.FREE_FILTERS
    assert not p.get("paid") and not p.get("no_key")        # BYOK, a key is required
    assert "static_key" not in p                            # nothing secret in the repo


def test_both_are_recommended_new_and_fresh(isolated_config):
    for pid in ("inception", "freemodel"):
        assert P.PROVIDERS[pid]["recommended"] is True, pid
        assert pid in A._NEW_PROVIDER_IDS, pid
        row = A._provider_row(pid, live_models=False)
        assert row["fresh"] is True and row["new"] is True and row["recommended"] is True, pid
    assert set(A._FRESH_PROVIDER_IDS) == {"inception", "freemodel"}
    for pid in ("g4f", "dahl", "puter", "groq"):
        assert A._provider_row(pid, live_models=False)["fresh"] is False, pid


# --- Inception ------------------------------------------------------------------------

def test_inception_routes_only_zero_priced_models_as_free():
    f = P.is_free_model
    assert P.PROVIDERS["inception"]["free_filter"] == "pricing_zero"
    assert P.PROVIDERS["inception"]["default_free_models"] == ["mercury-3"]
    # With no live list the check fails CLOSED -- it never claims free-ness it cannot prove.
    for mid in ("mercury-3", "mercury-2.5", "mercury-2", "mercury-decide"):
        assert not f("inception", mid), mid
    # With the live zero-priced list, only that id qualifies.
    assert f("inception", "mercury-3", known_free=["mercury-3"])
    assert not f("inception", "mercury-2.5", known_free=["mercury-3"])
    assert "Billing page" in P.PROVIDERS["inception"]["notes"]


def test_inception_is_a_first_party_api_not_a_relay():
    assert "inception" not in A._RELAY_DISCOUNT
    assert not A._is_relay_pid("inception")


def test_the_inception_request_budget_is_declared():
    assert quota.FREE_LIMITS["inception"] == {"limit": 1000, "window": "minute"}
    assert A._sustain_penalty("inception") == 0.0


def test_the_aa_vendor_prefix_no_longer_hides_mercury(monkeypatch):
    assert A._normalize_aa_slug("inception/mercury-2.5") == "mercury25"
    assert A._normalize_aa_slug("mercury-2.5") == "mercury25"
    monkeypatch.setattr(A, "_aa_scores", {"inceptionmercury2": 60.0, "inceptionmercury25": 61.5})
    assert A._aa_score_for("mercury-2") == 60.0
    assert A._aa_score_for("mercury-2.5") == 61.5
    monkeypatch.setattr(A, "_aa_scores", {"mercury2": 60.0, "mercury25": 61.5})   # after a refresh
    assert A._aa_score_for("mercury-2.5") == 61.5
    assert A._aa_score_for("inception/mercury-2") == 60.0


# --- FreeModel ------------------------------------------------------------------------

def test_freemodel_is_an_untrusted_relay():
    assert A._RELAY_DISCOUNT["freemodel"] == 4.0
    assert A._is_relay_pid("freemodel")
    assert A._rp_claim_pair("freemodel", "claude-opus-5-5")
    assert A._rp_claim_pair("freemodel", "gpt-5.6-sol")
    assert not A._rp_claim_pair("freemodel", "qwen3.8-27b")      # open-weight: no stub evidence
    assert not A._relay_proven("freemodel", "claude-opus-5-5")   # nothing measured yet


def test_a_claimed_claude_on_freemodel_is_discounted_like_g4f(monkeypatch):
    monkeypatch.setattr(A, "_rank_fleet_ids", lambda: [])
    A._rank_reset()
    first_party = A._benchmark_score("anthropic", "claude-opus-4.8")
    assert A._benchmark_score("freemodel", "claude-opus-4.8") == pytest.approx(
        first_party - A._RELAY_DISCOUNT["freemodel"])
    A._rank_reset()


def test_freemodel_frees_only_open_weight_ids_never_a_claimed_claude_or_gpt():
    f = P.is_free_model
    for mid in ("deepseek-v4.1-flash", "glm-5.3-flash", "mimo-v2.6-flash", "qwen3.8-27b",
                "seed-2.1-turbo", "deepseek-v4-flash-0731"):
        assert f("freemodel", mid), mid
    for mid in ("claude-opus-5-5", "claude-opus-4.8", "claude-fable-5-1", "claude-mythos-preview",
                "gpt-5.6-sol", "gpt-6-luna", "gpt-5.6-luna", "grok-4.6", "gemma-2-2b"):
        assert not f("freemodel", mid), mid
    # the four pinned ids were all read from the live catalog and generated for the owner
    assert set(P.PROVIDERS["freemodel"]["default_free_models"]) == {
        "deepseek-v4.1-flash", "glm-5.3-flash", "mimo-v2.6-flash", "qwen3.8-27b"}
    assert not any(t in m for m in P.PROVIDERS["freemodel"]["default_free_models"]
                   for t in ("claude", "gpt", "grok", "gemini"))


def test_the_freemodel_card_says_to_activate_with_telegram_first():
    notes = P.PROVIDERS["freemodel"]["notes"]
    assert notes.startswith("ACTIVATE THE FREE TOKENS FIRST")
    for needle in ("NEEDS TELEGRAM ACTIVATION to enable the free 1M tokens per month", "Telegram",
                   "Billing verification failed", "Do not add a payment method",
                   "VERIFIED WORKING", "1M TOKENS A MONTH IS SMALL", "do NOT send private code"):
        assert needle in notes, needle


def test_the_freemodel_pitch_is_on_the_face_of_the_card():
    src = open("templates/index.html", encoding="utf-8").read()
    assert "freemodel: 'Needs Telegram activation to enable the free 1M tokens per month'" in src


def test_the_freemodel_request_budget_is_unknown_and_monthly():
    q = quota.FREE_LIMITS["freemodel"]
    assert q["unknown"] is True and q["window"] == "month"
    assert A._sustain_penalty("freemodel") == 0.0


# --- CodeCraft and Radox AI stay removed ----------------------------------------------

@pytest.mark.parametrize("pid", REMOVED)
def test_a_removed_gateway_is_gone_everywhere(pid):
    assert pid not in P.PROVIDERS and P.get_provider(pid) is None
    assert pid not in A._RELAY_DISCOUNT
    assert not A._is_relay_pid(pid)
    assert pid not in A._NEW_PROVIDER_IDS and pid not in A._FRESH_PROVIDER_IDS
    assert pid not in quota.FREE_LIMITS
    assert not P.is_free_model(pid, "qwen3.8-27b")


def test_the_relay_tables_hold_only_g4f_and_freemodel():
    assert A._RELAY_DISCOUNT == {"g4f": 4.0, "freemodel": 4.0}
    assert A._TOOL_RELAY_PIDS == ("g4f", "freemodel")


def test_why_they_were_removed_is_kept_as_a_comment():
    src = open("providers.py", encoding="utf-8").read()
    assert "REMOVED 2026-10-24" in src and "Billing verification failed" in src
    assert "cleanapis" in src and "migrations v5" in src
    for host in ("codecraftapi.com", "radoxai.com"):
        assert host in src, host
