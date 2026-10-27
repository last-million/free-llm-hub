"""Request rates the providers' OWN documentation states, read by the rate-aware spreading.

Owner, 2026-10-27: "all providers ... he should adapt with them smartly ... and respect requests",
and "read the documentation before testing". Seven research agents read the official docs of all 59
named providers (no API calls, no keys; a number was kept only when a page stated it, with the
page recorded). The ones below are provider- or account-wide REQUEST rates the docs print, which
quota.FREE_LIMITS cannot hold (it keeps one window per provider) and which _rh_limits then folds in as
the "notes" source: the tightest figure wins, a provider at 80% of it yields to an equally good one
(reorder only, never a gate).

Deliberately NOT here, because the docs scope them per MODEL or per HOUR and the hint table is
provider-wide per-minute: unorouter (1/min PER MODEL), ovhcloud (2/min per IP per model), kilocode
(200/HOUR per IP), llm7's 250/hour, and every figure a page contradicted (llmgateway, cohere's 40/min).
"""
import pytest

import app
import quota

# pid: (hint key, value, where the docs print it)
DOCUMENTED = {
    "cerebras":     ("per_min", 5,  "inference-docs.cerebras.ai rate limits: free trial 5 RPM per organization"),
    "openrouter":   ("per_min", 20, "openrouter.ai/docs/api-reference/limits: ':free' models 20 requests per minute"),
    "cohere":       ("per_min", 20, "docs.cohere.com rate limits page: trial 20 calls/min (the errors page shows 40: the lower wins)"),
    "requesty":     ("per_min", 20, "docs.requesty.ai: free limit per organization, 20/min for new orgs"),
    "api-airforce": ("per_min", 1,  "api.airforce free plan: 1 request per minute"),
    "fireworks":    ("per_min", 10, "docs.fireworks.ai rate limits: no payment method on file = 10 RPM per account"),
    "aion":         ("per_min", 15, "aionlabs docs: 15 RPM free"),
    "nararouter":   ("per_min", 15, "router.bynara.id/docs: Free plan 15 RPM (the hub's quota row says 10: the lower wins)"),
    "llm7":         ("per_sec", 1,  "llm7.io limits page: free token 1 request per second"),
    "uncloseai":    ("per_sec", 3,  "uncloseai docs: public host 3 requests per second per IP"),
}


@pytest.fixture
def one_key(monkeypatch):
    monkeypatch.setattr(quota, "key_count", lambda pid: 1)
    yield


@pytest.mark.parametrize("pid", sorted(DOCUMENTED))
def test_a_documented_rate_is_a_hint(pid):
    key, value, _src = DOCUMENTED[pid]
    assert app._RH_HINTS.get(pid, {}).get(key) == value


@pytest.mark.parametrize("pid", sorted(DOCUMENTED))
def test_rh_limits_reads_it(one_key, pid):
    key, value, _src = DOCUMENTED[pid]
    lim = app._rh_limits(pid)
    assert lim is not None and lim[key] is not None
    # a FREE_LIMITS minute row or a header may be tighter, never looser than the docs
    assert lim[key] <= value


def test_the_tighter_hub_row_still_wins_for_nararouter(one_key):
    assert app._rh_limits("nararouter")["per_min"] == 10        # quota.FREE_LIMITS says 10/min


def test_a_pool_of_keys_scales_a_per_account_rate(monkeypatch):
    monkeypatch.setattr(quota, "key_count", lambda pid: 3)
    assert app._rh_limits("fireworks")["per_min"] == 30


def test_the_old_hints_are_untouched():
    assert app._RH_HINTS["glm"] == {"per_sec": 1}
    assert app._RH_HINTS["sambanova"] == {"per_min": 20}
    assert app._RH_HINTS["routeway"] == {"per_min": 5}


def test_a_provider_with_no_documented_rate_has_no_limit(one_key):
    """An unknown limit must never be read as 'full'."""
    for pid in ("unorouter", "ovhcloud", "kilocode", "zenmux", "dahl", "freemodel"):
        lim = app._rh_limits(pid)
        assert lim is None or (lim["per_min"] is None and lim["per_sec"] is None) or pid in ("freemodel",)


# --------------------------------------------------------------------------- #
# Providers whose own docs say there is no free quota any more are gone
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("pid", ["github-models", "hyperbolic", "aimlapi"])
def test_a_provider_without_free_quota_is_not_in_the_registry(pid):
    import providers as prov
    assert prov.get_provider(pid) is None
    assert pid not in quota.FREE_LIMITS
    assert pid not in app._RH_HINTS
