"""Inception (Mercury) and CodeCraft API, added 2026-10-23 at the owner's request.

Facts were READ from the providers' docs and live endpoints the same day (see the
comments above each entry in providers.py). What these tests pin is what could
silently go wrong in the direction of billing the owner or of trusting a name:

  * Inception routes only ZERO-PRICED models as free (mercury-3 today); the billed
    mercury-2 / 2.5 stay pin-only, and the check fails CLOSED without a live list.
  * CodeCraft is an untrusted pay-gateway: relay class (discount, relay gates, no
    board bonus for a claimed Claude/GPT) and only open-weight ids are free.
  * Mercury's real Artificial Analysis score reaches the plain id its own API uses.
"""
import os
import shutil
import tempfile
from urllib.parse import urlparse

import pytest

import app as A
import providers as P
import quota


@pytest.mark.parametrize("pid,host,signup_host", [
    ("inception", "api.inceptionlabs.ai", "platform.inceptionlabs.ai"),
    ("codecraft", "codecraftapi.com", "codecraftapi.com"),
    ("radoxai", "radoxai.com", "radoxai.com"),
    ("freemodel", "freemodel.in", "freemodel.in"),
])
def test_the_registry_entry_is_complete_and_https(pid, host, signup_host):
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


NEWEST = ("inception", "codecraft", "radoxai", "freemodel")


@pytest.fixture
def isolated_config(monkeypatch):
    d = tempfile.mkdtemp(prefix="hub-pytest-newprov-")
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", os.path.join(d, "state", "config.json"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_all_four_are_in_the_recommended_zone_and_flagged_new():
    for pid in NEWEST:
        assert P.PROVIDERS[pid]["recommended"] is True, pid
    assert set(NEWEST) <= set(A._NEW_PROVIDER_IDS)
    assert set(A._FRESH_PROVIDER_IDS) == set(NEWEST)


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


def test_codecraft_frees_only_open_weight_ids_never_a_claimed_claude_or_gpt():
    f = P.is_free_model
    for mid in ("deepseek-v4-flash-0731", "deepseek-v4-pro-0813", "qwen3.8-27b",
                "seed-2.1-turbo"):
        assert f("codecraft", mid), mid
    for mid in ("claude-opus-4.8", "claude-fable-5", "gpt-5.6-luna", "gemma-2-2b",
                "some-new-model"):
        assert not f("codecraft", mid), mid
    assert not any("claude" in m or "gpt" in m
                   for m in P.PROVIDERS["codecraft"]["default_free_models"])


def test_the_notes_warn_about_the_untrusted_gateway_and_the_card():
    assert "do NOT send private code" in P.PROVIDERS["codecraft"]["notes"]
    assert "Billing page" in P.PROVIDERS["inception"]["notes"]


def test_the_three_gateway_clones_are_relays_inception_is_not():
    for pid in ("codecraft", "radoxai", "freemodel"):
        assert A._RELAY_DISCOUNT[pid] == 4.0, pid
        assert A._is_relay_pid(pid), pid
        assert A._rp_claim_pair(pid, "claude-opus-5-5"), pid
        assert not A._relay_proven(pid, "claude-opus-5-5"), pid
    assert "inception" not in A._RELAY_DISCOUNT
    assert not A._is_relay_pid("inception")
    assert A._rp_claim_pair("codecraft", "claude-opus-4.8")
    assert A._rp_claim_pair("codecraft", "gpt-5.6-luna")
    assert not A._rp_claim_pair("codecraft", "qwen3.8-27b")      # open-weight: no stub evidence
    assert not A._relay_proven("codecraft", "claude-opus-4.8")   # nothing measured yet


def test_a_claimed_claude_on_codecraft_is_discounted_like_g4f(monkeypatch):
    monkeypatch.setattr(A, "_rank_fleet_ids", lambda: [])
    A._rank_reset()
    first_party = A._benchmark_score("anthropic", "claude-opus-4.8")
    assert A._benchmark_score("codecraft", "claude-opus-4.8") == pytest.approx(
        first_party - A._RELAY_DISCOUNT["codecraft"])
    A._rank_reset()


def test_the_request_budgets_are_declared():
    assert quota.FREE_LIMITS["inception"] == {"limit": 1000, "window": "minute"}
    assert quota.FREE_LIMITS["codecraft"]["unknown"] is True
    assert quota.FREE_LIMITS["codecraft"]["window"] == "month"
    for pid in ("inception", "codecraft"):
        assert A._sustain_penalty(pid) == 0.0                    # neither is a scarce budget


def test_the_aa_vendor_prefix_no_longer_hides_mercury(monkeypatch):
    assert A._normalize_aa_slug("inception/mercury-2.5") == "mercury25"
    assert A._normalize_aa_slug("mercury-2.5") == "mercury25"
    monkeypatch.setattr(A, "_aa_scores", {"inceptionmercury2": 60.0, "inceptionmercury25": 61.5})
    assert A._aa_score_for("mercury-2") == 60.0
    assert A._aa_score_for("mercury-2.5") == 61.5
    monkeypatch.setattr(A, "_aa_scores", {"mercury2": 60.0, "mercury25": 61.5})   # after a refresh
    assert A._aa_score_for("mercury-2.5") == 61.5
    assert A._aa_score_for("inception/mercury-2") == 60.0


@pytest.mark.parametrize("pid", ["radoxai", "freemodel"])
def test_the_clones_free_only_open_weight_ids(pid):
    f = P.is_free_model
    assert f(pid, "qwen3.8-27b")
    for mid in ("claude-opus-5-5", "claude-opus-4.8", "claude-fable-5-1", "claude-mythos-preview",
                "gpt-5.6-sol", "gpt-6-luna", "gpt-5.6-luna", "grok-4.6", "gemma-2-2b"):
        assert not f(pid, mid), (pid, mid)
    assert not any(t in m for m in P.PROVIDERS[pid]["default_free_models"]
                   for t in ("claude", "gpt", "grok", "gemini"))
    assert "do NOT send private code" in P.PROVIDERS[pid]["notes"]


def test_freemodels_pinned_ids_are_the_ones_read_from_its_live_catalog():
    assert set(P.PROVIDERS["freemodel"]["default_free_models"]) == {
        "deepseek-v4.1-flash", "glm-5.3-flash", "mimo-v2.6-flash", "qwen3.8-27b"}
    for mid in P.PROVIDERS["freemodel"]["default_free_models"]:
        assert P.is_free_model("freemodel", mid), mid


def test_the_newest_batch_is_marked_fresh_and_the_older_new_badges_are_not(isolated_config):
    for pid in NEWEST:
        row = A._provider_row(pid, live_models=False)
        assert row["fresh"] is True and row["new"] is True and row["recommended"] is True, pid
    for pid in ("g4f", "dahl", "puter", "groq"):
        assert A._provider_row(pid, live_models=False)["fresh"] is False, pid


def test_the_page_puts_the_fresh_batch_first_in_the_recommended_zone():
    src = open("templates/index.html", encoding="utf-8").read()
    assert ".sort(byFreshThenConnectedQuality)" in src
    assert "var fa = a.fresh ? 1 : 0, fb = b.fresh ? 1 : 0;" in src
    # the New badge those cards carry is the existing one
    assert "if (p['new']) badges +=" in src
