"""Experiential Labs, added 2026-09-05 at user request.

Every fact here was READ LIVE from their own API that day, not taken from docs
or inferred from another provider's shape:

  - https://api.experientiallabs.ai/api/models is a KEYLESS public catalog.
    763 models, and a "promotions" array of exactly five entries carrying
    "free": true.
  - The five, verbatim: qwen3.8-27b, deepseek-v4-flash, gpt-5.6-luna,
    gpt-6-astra, claude-fable-5.1.
  - The catalog marks the first three "requires_payment_method": false and the
    last two true. CORRECTED the same day by a real key: that flag describes
    which promotion applies, NOT whether the call is served. With a valid key
    and no card on file, ALL FIVE answer "HTTP 429: Requires a card on file to
    spend platform credits". The card is free; the gate is on spending platform
    credits at all.
  - The slug is claude-fable-5.1 with a DOT. claude-fable-5-1 does not exist in
    the catalog; the hyphenated form is the upstream provider_model_id, not the
    gateway slug. claude-fable-5 exists and is PAID -- it is not in promotions.

These tests pin the facts that would silently misroute real money or real
requests if someone edited them from memory. They deliberately do NOT hit the
network: the values are the record of what was verified.
"""
import pytest

import providers as P


PID = "experientiallabs"

# The promotions list as it read on 2026-09-05. Every one needs a card on file,
# whatever the per-model flag says -- see the module docstring.
FREE = ["claude-fable-5.1", "gpt-6-astra", "gpt-5.6-luna",
        "deepseek-v4-flash", "qwen3.8-27b"]


@pytest.fixture
def entry():
    return P.PROVIDERS[PID]


def test_the_provider_exists(entry):
    assert entry["name"] == "Experiential Labs"


def test_it_is_in_the_recommended_zone(entry):
    """Asked for explicitly: it gives away GPT-6 Astra and Claude Fable 5.1."""
    assert entry.get("recommended") is True


def test_the_base_url_is_the_verified_one(entry):
    """Verbatim from their llms.txt. A wrong base_url fails every call with a
    404 that looks like a dead provider."""
    assert entry["base_url"] == "https://api.experientiallabs.ai/v1"


def test_the_models_url_is_under_the_same_v1_surface(entry):
    assert entry["models_url"] == "https://api.experientiallabs.ai/v1/models"


def test_the_key_hint_matches_their_real_prefix(entry):
    """Keys are xpl_ + 40 hex. The hint is what the dashboard shows the user."""
    assert entry["key_hint"].startswith("xpl_")


def test_a_user_can_find_where_to_get_a_key(entry):
    assert "experientiallabs.ai" in entry["signup_url"]


# --------------------------------------------------------------------------- #
# The free set is exactly the promotions list, and nothing else
# --------------------------------------------------------------------------- #

def test_every_promotion_model_is_free():
    for m in FREE:
        assert P.is_free_model(PID, m), m


def test_the_paid_twin_of_the_headline_model_is_not_free():
    """claude-fable-5 is a real, active, PAID model in the same catalog and is
    absent from promotions. A substring match on 'claude-fable-5' would swallow
    'claude-fable-5.1' and vice versa -- which is why the match is exact."""
    assert not P.is_free_model(PID, "claude-fable-5")


def test_an_arbitrary_paid_model_is_not_free():
    """758 of their 763 models are paid. The filter must fail closed."""
    for m in ("gpt-4o", "claude-fable-5-batch", "claude-fable-latest",
              "gpt-6-astra-preview", "qwen3.8-27b-instruct"):
        assert not P.is_free_model(PID, m), m


def test_the_match_is_exact_not_substring(entry):
    """free_exact is what stops the leak above. Without it 'qwen3.8-27b' as a
    substring matches every qwen variant in a 763-model catalog."""
    assert entry.get("free_exact") is True
    assert entry.get("free_filter") == "family"


def test_the_free_families_are_the_five_promotions(entry):
    assert sorted(entry["free_families"]) == sorted(FREE)


def test_the_hyphenated_fable_slug_is_not_used(entry):
    """claude-fable-5-1 is the upstream provider_model_id and is ABSENT from the
    gateway catalog. Pinning it would 404 every call."""
    blob = " ".join(entry["free_families"] + entry["default_free_models"])
    assert "claude-fable-5-1" not in blob
    assert "claude-fable-5.1" in blob


# --------------------------------------------------------------------------- #
# Ordering, and saying the card part out loud
# --------------------------------------------------------------------------- #

def test_the_strongest_model_leads(entry):
    """Ordered by strength. An earlier version ordered by "needs no card",
    which sorted on a distinction the gateway does not actually make."""
    assert entry["default_free_models"][0] == "claude-fable-5.1"
    assert entry["default_free_models"][1] == "gpt-6-astra"


def test_all_five_are_reachable(entry):
    assert sorted(entry["default_free_models"]) == sorted(FREE)


def test_the_card_requirement_is_the_first_thing_a_user_reads(entry):
    """Every free model 429s without one, so a user who misses this sees a
    provider that looks simply broken. The notes are what the dashboard shows,
    and they must not repeat the catalog's claim that three need no card."""
    notes = entry["notes"]
    assert "CARD ON FILE" in notes
    assert "add-card" in notes
    assert "need no card" not in notes.lower()


# --------------------------------------------------------------------------- #
# It has to work like every other provider
# --------------------------------------------------------------------------- #

def test_it_is_not_marked_paid(entry):
    """A provider-level 'paid' flag would exclude it from free routing entirely."""
    assert not entry.get("paid")


def test_it_speaks_plain_openai_http(entry):
    """No driver_api: it is OpenAI-compatible over HTTP, so every surface the
    hub exposes (chat/completions, responses, messages, ollama) reaches it
    through the normal path with no adapter."""
    assert "driver_api" not in entry


def test_the_free_models_pass_the_chat_model_filter():
    """filter_models drops embedding/non-chat ids; all five must survive it or
    they never reach a chain."""
    kept = P.filter_models(list(FREE))
    assert sorted(kept) == sorted(FREE)
