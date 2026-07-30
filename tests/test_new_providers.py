"""Unit tests for the providers added in the July 2026 registry expansion:
github-models, uncloseai, llm7, api-airforce, navy, routeway — registry shape,
is_free_model() behavior per free_filter, no_key flags, and the matching
quota.py FREE_LIMITS rows. No Flask app involved (registry + quota only).

Also covers the keyless gateways added 2026-07-30: g4f-groq, g4f-gemini,
g4f-nvidia (g4f.space relays, no key at all) and kilocode (anonymous tier,
literal bearer "anonymous").
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
    for pid in ("github-models", "uncloseai", "llm7", "api-airforce", "navy"):
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
