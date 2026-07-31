"""Unit tests for the providers added in the July 2026 registry expansion:
github-models, uncloseai, llm7, api-airforce, navy, routeway — registry shape,
is_free_model() behavior per free_filter, no_key flags, and the matching
quota.py FREE_LIMITS rows. No Flask app involved (registry + quota only).

Also covers the keyless gateways added 2026-07-30: g4f-groq, g4f-gemini,
g4f-nvidia (g4f.space relays, no key at all) and kilocode (anonymous tier,
literal bearer "anonymous"), plus puter (user-pays gateway, BYOK).
"""
import providers as prov
import quota

NEW_PROVIDERS = ("github-models", "uncloseai", "llm7",
                 "api-airforce", "navy", "routeway")

NO_KEY_PROVIDERS = ("uncloseai", "llm7")
KEYED_PROVIDERS = ("github-models", "api-airforce", "navy", "routeway")

# Providers with a researched request budget in quota.FREE_LIMITS.
KNOWN_LIMITS = ("github-models", "llm7", "navy", "routeway")
# Genuinely free but with NO published figure -> deliberately absent from
# FREE_LIMITS so they track as UNKNOWN via DEFAULT_LIMIT (never a budget of 0
# and never a fabricated number).
UNKNOWN_LIMITS = ("uncloseai", "api-airforce")


def test_new_providers_registered_with_required_fields():
    for pid in NEW_PROVIDERS:
        p = prov.get_provider(pid)
        assert p is not None, pid + " missing from PROVIDERS"
        assert p.get("name"), pid
        assert isinstance(p.get("base_url"), str) and p["base_url"].startswith("https://"), pid
        assert p.get("signup_url"), pid
        assert p.get("free_filter") in prov.FREE_FILTERS, pid
        assert isinstance(p.get("default_free_models"), list), pid
        assert prov.is_known_provider(pid), pid


def test_suffix_free_provider_accepts_only_suffixed_ids():
    # routeway uses the OpenRouter ':free' convention.
    assert prov.is_free_model("routeway", "openai/gpt-4.1:free")
    assert prov.is_free_model("routeway", "deepseek-r1:free")
    assert not prov.is_free_model("routeway", "openai/gpt-4.1")
    assert not prov.is_free_model("routeway", "deepseek-r1")
    # The bare-word trap: ':free' must be a SUFFIX, not a substring.
    assert not prov.is_free_model("routeway", "free-model-x")


def test_all_filter_providers_accept_ordinary_ids():
    # 'all' is only honest on providers with no paid catalog to leak.
    # (llm7 was here until 2026-07-30, when its catalog gained a PAID 'pro'
    # tier — it is now family/exact-pinned; see the re-verification block.)
    for pid in ("github-models", "uncloseai", "api-airforce", "navy"):
        assert prov.get_provider(pid)["free_filter"] == "all", pid
        assert prov.is_free_model(pid, "openai/gpt-4.1"), pid
        assert prov.is_free_model(pid, "deepseek/DeepSeek-R1"), pid
        assert prov.is_free_model(pid, "meta/Llama-4-Scout"), pid


def test_is_free_model_rejects_empty_and_non_free_tier():
    for pid in NEW_PROVIDERS:
        assert not prov.is_free_model(pid, None), pid
        assert not prov.is_free_model(pid, ""), pid
        assert not prov.is_free_model(pid, "openai/gpt-4.1", is_free_tier=False), pid
    assert not prov.is_free_model("no-such-provider", "openai/gpt-4.1")


def test_no_key_flags():
    for pid in NO_KEY_PROVIDERS:
        assert prov.get_provider(pid).get("no_key") is True, pid
    for pid in KEYED_PROVIDERS:
        assert not prov.get_provider(pid).get("no_key"), pid


def test_pinned_default_models_pass_their_own_free_check():
    # A pinned default that fails is_free_model() would be silently unroutable.
    for pid in NEW_PROVIDERS:
        for mid in prov.get_provider(pid)["default_free_models"]:
            assert prov.is_free_model(pid, mid), (pid, mid)
            assert prov.is_model_allowed(mid), (pid, mid)
            assert prov.is_chat_model(mid), (pid, mid)


def test_known_free_limits_rows():
    for pid in KNOWN_LIMITS:
        row = quota.FREE_LIMITS.get(pid)
        assert row is not None, pid + " missing from FREE_LIMITS"
        assert isinstance(row["limit"], int) and row["limit"] > 0, pid
        assert row["window"] in ("minute", "day", "month"), pid


def test_unknown_limit_providers_fall_back_to_default():
    for pid in UNKNOWN_LIMITS:
        assert pid not in quota.FREE_LIMITS, pid + " must not carry a fabricated budget"
        row = quota._limit_for(pid)
        assert row is quota.DEFAULT_LIMIT, pid
        assert row["limit"] is None and row.get("unknown") is True, pid


def test_github_models_fixes_dangling_references():
    # The bug this expansion fixes: quota.py and app.py referenced
    # 'github-models' with no registry entry. The quota row predates the
    # provider — both halves must now exist and agree it has a free budget.
    assert prov.is_known_provider("github-models")
    row = quota.FREE_LIMITS["github-models"]
    assert row["limit"] > 0
    assert prov.get_provider("github-models")["base_url"] == \
        "https://models.github.ai/inference"


# --------------------------------------------------------------------------- #
# Keyless public gateways added 2026-07-30:
# g4f-groq / g4f-gemini / g4f-nvidia (g4f.space community reverse proxies, no
# key at all) and kilocode (anonymous free tier, literal bearer "anonymous").
# --------------------------------------------------------------------------- #

KEYLESS_PROVIDERS = ("g4f-groq", "g4f-gemini", "g4f-nvidia", "kilocode")
G4F_PROVIDERS = ("g4f-groq", "g4f-gemini", "g4f-nvidia")


def test_keyless_gateways_registered_with_required_fields():
    for pid in KEYLESS_PROVIDERS:
        p = prov.get_provider(pid)
        assert p is not None, pid + " missing from PROVIDERS"
        assert p.get("name"), pid
        assert isinstance(p.get("base_url"), str) and p["base_url"].startswith("https://"), pid
        assert p.get("signup_url"), pid
        assert p.get("free_filter") in prov.FREE_FILTERS, pid
        assert isinstance(p.get("default_free_models"), list), pid
        assert prov.is_known_provider(pid), pid


def test_keyless_gateways_no_key_flags():
    for pid in KEYLESS_PROVIDERS:
        assert prov.get_provider(pid).get("no_key") is True, pid
    # The g4f.space relays take NO credential at all: no static_key, so the
    # no-key path sends no Authorization header (pollinations precedent).
    for pid in G4F_PROVIDERS:
        assert not prov.get_provider(pid).get("static_key"), pid
    # kilocode's anonymous tier authenticates with the literal bearer string.
    assert prov.get_provider("kilocode").get("static_key") == "anonymous"


def test_keyless_gateways_free_filters():
    # g4f-* relays are free-only -> 'all' accepts ordinary ids.
    for pid in G4F_PROVIDERS:
        assert prov.get_provider(pid)["free_filter"] == "all", pid
        assert prov.is_free_model(pid, "llama-3.3-70b-versatile"), pid
        assert prov.is_free_model(pid, "gemini-2.5-flash"), pid
        assert not prov.is_free_model(pid, None), pid
        assert not prov.is_free_model(pid, ""), pid
        assert not prov.is_free_model(pid, "llama-3.3-70b", is_free_tier=False), pid
    # kilocode uses the OpenRouter ':free' suffix convention (routeway shape).
    assert prov.get_provider("kilocode")["free_filter"] == "suffix_free"
    assert prov.is_free_model("kilocode", "openai/gpt-4.1:free")
    assert not prov.is_free_model("kilocode", "openai/gpt-4.1")
    assert not prov.is_free_model("kilocode", "free-model-x")  # suffix, not substring


def test_keyless_gateways_quota_rows():
    # g4f-*: community-observed ~5 req/min, tracked per-minute.
    for pid in G4F_PROVIDERS:
        row = quota.FREE_LIMITS.get(pid)
        assert row is not None, pid + " missing from FREE_LIMITS"
        assert row["limit"] == 5, pid
        assert row["window"] == "minute", pid
    # kilocode: no published figure -> deliberately ABSENT, tracks as UNKNOWN
    # via DEFAULT_LIMIT (never a budget of 0, never a fabricated number).
    assert "kilocode" not in quota.FREE_LIMITS
    row = quota._limit_for("kilocode")
    assert row is quota.DEFAULT_LIMIT
    assert row["limit"] is None and row.get("unknown") is True


# --------------------------------------------------------------------------- #
# Live re-verification fixes, 2026-07-30 (direct curl probes of each gateway):
#   uncloseai — 405B flagship gone; catalog is now the single 8B AWQ id.
#   llm7      — catalog gained a PAID token-priced 'pro' tier (+ image/video);
#               anonymous key serves ONLY the 4 'turbo' ids (pro 401s), so the
#               row is family/exact-pinned to keep paid ids out of free routing.
#   g4f-groq / g4f-nvidia — /models 404s on the doubled /v1 path; the working
#               models_url drops the /v1 (chat keeps base_url + /v1).
#   kilocode  — anonymous /models discovery IS available; pinned fallback added.
# --------------------------------------------------------------------------- #

LLM7_TURBO = ("codestral-latest", "gemini-3.1-flash-lite",
              "gpt-oss:20b", "minimax-m2.7")


def test_llm7_is_family_exact_pinned_to_the_free_turbo_tier():
    p = prov.get_provider("llm7")
    assert p["free_filter"] == "family"
    assert p.get("free_exact") is True
    assert tuple(p["free_families"]) == LLM7_TURBO
    assert tuple(p["default_free_models"]) == LLM7_TURBO
    for mid in LLM7_TURBO:
        assert prov.is_free_model("llm7", mid), mid
    # The paid 'pro' catalog (and video/image ids) must NOT qualify as free.
    for paid in ("gpt-5.4-mini", "claude-opus-5", "kimi-k3",
                 "gemini-veo31", "seedance-2.0"):
        assert not prov.is_free_model("llm7", paid), paid
    # free_exact: a near-miss id that merely CONTAINS a free id fails closed.
    assert not prov.is_free_model("llm7", "gpt-oss:20b-extended")
    assert not prov.is_free_model("llm7", "codestral-latestx")


def test_uncloseai_pinned_default_matches_live_catalog():
    p = prov.get_provider("uncloseai")
    assert p["default_free_models"] == ["solidrust/Hermes-3-Llama-3.1-8B-AWQ"]
    assert prov.is_free_model("uncloseai", "solidrust/Hermes-3-Llama-3.1-8B-AWQ")


def test_g4f_models_urls_avoid_the_doubled_v1_path():
    # Live probe 2026-07-30: on the groq/nvidia relays /api/<up>/v1/models 404s
    # (the relay appends /v1 itself); /api/<up>/models returns 200. Chat still
    # uses base_url + /v1. g4f-gemini answered 200 on BOTH forms today, but is
    # aligned to the same /v1-less canonical form as its siblings.
    assert prov.get_provider("g4f-groq")["models_url"] == \
        "https://g4f.space/api/groq/models"
    assert prov.get_provider("g4f-nvidia")["models_url"] == \
        "https://g4f.space/api/nvidia/models"
    assert prov.get_provider("g4f-gemini")["models_url"] == \
        "https://g4f.space/api/gemini/models"
    for pid in G4F_PROVIDERS:
        mu = prov.get_provider(pid)["models_url"]
        assert "/v1/models" not in mu, pid
        assert prov.get_provider(pid)["base_url"].endswith("/v1"), pid


def test_keyless_pinned_defaults_pass_their_own_free_check():
    # A pinned fallback that fails is_free_model() would be silently unroutable.
    for pid in KEYLESS_PROVIDERS:
        for mid in prov.get_provider(pid)["default_free_models"]:
            assert prov.is_free_model(pid, mid), (pid, mid)
            assert prov.is_model_allowed(mid), (pid, mid)
            assert prov.is_chat_model(mid), (pid, mid)


def test_kilocode_has_live_models_discovery():
    # Verified 2026-07-30: Bearer anonymous on /models returns 200 with an
    # OpenRouter-shaped catalog — no longer None (the old 'custom' precedent).
    assert prov.get_provider("kilocode")["models_url"] == \
        "https://api.kilo.ai/api/openrouter/models"


# --------------------------------------------------------------------------- #
# puter (added 2026-07-30): user-pays AI gateway — one free puter.com account
# auth token unlocks the whole OpenAI-compatible catalog (500+ models incl.
# the GPT-5.6 flagship family). BYOK (NOT no_key), free_filter 'all' (the one
# account covers the entire catalog), no published fair-use numbers -> absent
# from FREE_LIMITS (UNKNOWN via DEFAULT_LIMIT; uncloseai/api-airforce
# precedent).
# --------------------------------------------------------------------------- #


def test_puter_registered_with_required_fields():
    p = prov.get_provider("puter")
    assert p is not None, "puter missing from PROVIDERS"
    assert p.get("name")
    assert p["base_url"] == "https://api.puter.com/puterai/openai/v1"
    # NOT <base_url>/models — that route does not exist on Puter's gateway
    # (404 "not_found" with a valid bearer too, probed 2026-07-31), which made
    # every key Test fail "✗ HTTP 404". The catalog route puter.js itself calls
    # is /puterai/chat/models/details.
    assert p["models_url"] == "https://api.puter.com/puterai/chat/models/details"
    assert not p["models_url"].startswith(p["base_url"])
    assert p["signup_url"] == "https://puter.com"
    assert p.get("key_hint")
    assert p["free_filter"] in prov.FREE_FILTERS
    # NOT a free tier: ~25 US cents PER MONTH, measured from Puter's own
    # /metering/usage. A populated default_free_models would be returned as
    # "free models" regardless of free_filter and put puter back into free
    # auto-rotation, where it runs dry in a few dozen calls and then 402s.
    assert p["paid"] is True and p["trial"] is True
    assert p["default_free_models"] == []
    assert prov.is_known_provider("puter")


def test_puter_is_byok_not_no_key():
    # The free account still requires its auth token — keyless would 401.
    assert not prov.get_provider("puter").get("no_key")
    assert not prov.get_provider("puter").get("static_key")


def test_puter_models_are_paid_class_not_free():
    """Puter is a metered account with a ~25c/month credit, so nothing it serves
    counts as free — it is reachable by an explicit pin or by switching auto
    routing to 'mix', never as part of the free fleet."""
    for mid in ("gpt-5.6-sol", "gpt-4.1", "claude-opus-5", "gemini-3.1-pro"):
        assert not prov.is_free_model("puter", mid), mid


def test_puter_contributes_nothing_to_free_auto_routing():
    """The invariant that matters, and the only one that holds without a
    network call: puter adds NOTHING to the free fleet. (Its paid catalog is
    discovered live, so counting it here would just test connectivity.)"""
    import app
    assert app._auto_models("puter") == []
    assert prov.get_provider("puter")["paid"] is True


def test_puter_pinned_defaults_pass_their_own_free_check():
    # A pinned fallback that fails is_free_model() would be silently unroutable.
    for mid in prov.get_provider("puter")["default_free_models"]:
        assert prov.is_free_model("puter", mid), mid
        assert prov.is_model_allowed(mid), mid
        assert prov.is_chat_model(mid), mid


def test_puter_quota_is_deliberately_unknown():
    # Fair-use limits apply but Puter publishes NO numbers -> no fabricated
    # budget; tracks as UNKNOWN via DEFAULT_LIMIT (real 429s still throttle).
    assert "puter" not in quota.FREE_LIMITS
    row = quota._limit_for("puter")
    assert row is quota.DEFAULT_LIMIT
    assert row["limit"] is None and row.get("unknown") is True
