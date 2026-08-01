"""New free models must be picked up — and RANKED — without editing providers.py.

Discovery itself already worked for `free_filter: "all"`: the live catalog
replaces default_free_models wholesale on a 60s TTL. What did NOT work:

1. Relays that compress vendor ids fell through every family match and every
   preference floor, so a discovered flagship ranked as an unknown model and sat
   at the BOTTOM of the chain. Morph's whole catalog is spelled that way.
2. '-mini' was tested as a bare substring, so ANY '-minimax' id tripped the
   tiny-model speed cap — a 428B flagship scored 30 while the same model without
   a provider prefix scored 108.
3. `free_filter: "pricing_zero"` failed closed on every id (no pricing was ever
   parsed), so those providers were frozen on their static lists forever.
"""
import app
import providers as prov


# --------------------------------------------------------------------------- #
# 1. compressed vendor ids
# --------------------------------------------------------------------------- #

def test_compressed_ids_reach_their_family():
    assert app._canon_model_id("morph-glm52-744b") == "morph-glm5.2-744b"
    assert app._canon_model_id("morph-qwen35-397b") == "morph-qwen3.5-397b"
    assert app._canon_model_id("morph-minimax3-428b") == "morph-minimax-m3-428b"
    assert app._canon_model_id("morph-dsv4flash") == "morph-deepseek-v4flash"


def test_already_canonical_ids_are_untouched():
    """The rules must never rewrite an id that was already spelled correctly."""
    for mid in ("qwen3-30b-a3b-fp8", "glm-4.6", "llama-3.3-70b-versatile",
                "gpt-5.2", "claude-sonnet-4-5", "gemma-3-27b-it",
                "qwen3-235b-a22b", "deepseek-v3.1"):
        assert app._canon_model_id(mid) == mid, mid


def test_morph_kimi_k3_wins_the_top_slot():
    """Kimi K3 is the user's top pick after gpt-5/claude. Before the fix morph's
    spelling never matched the floor and it lost to lesser models."""
    ms = prov.PROVIDERS["morph"]["default_free_models"]
    best = max(ms, key=lambda m: app._benchmark_score("morph", m))
    assert "kimik3" in best


def test_morph_glm52_gets_the_glm5_floor():
    assert app._benchmark_score("morph", "morph-glm52-744b") >= 134


def test_compressed_deepseek_matches_the_spelled_out_one():
    """morph-dsv4flash IS deepseek/deepseek-v4-flash. They scored 10 vs 108."""
    a = app._benchmark_score("morph", "morph-dsv4flash")
    b = app._benchmark_score("morph", "deepseek/deepseek-v4-flash")
    assert abs(a - b) < 1.0, (a, b)


# --------------------------------------------------------------------------- #
# 2. the '-mini' / '-minimax' collision
# --------------------------------------------------------------------------- #

def test_minimax_is_not_treated_as_a_mini_model():
    """'morph-minimax3-428b' contains '-mini'. It is a 428B flagship."""
    assert app._benchmark_score("morph", "morph-minimax3-428b") > 100


def test_real_mini_models_are_still_capped():
    """The boundary must not disarm the cap for genuine small models."""
    for mid in ("gpt-4o-mini", "gemini-3-mini"):
        assert app._benchmark_score("groq", mid) <= 40, mid
    assert app._benchmark_score("groq", "ministral-8b") <= 40


def test_mini_suffix_regex_boundary():
    assert app._MINI_SUFFIX_RE.search("gpt-4o-mini")
    assert not app._MINI_SUFFIX_RE.search("morph-minimax3-428b")
    assert not app._MINI_SUFFIX_RE.search("x-ministral-8b")


# --------------------------------------------------------------------------- #
# 3. pricing_zero live discovery
# --------------------------------------------------------------------------- #

def test_zero_priced_ids_are_discovered():
    payload = {"data": [
        {"id": "free-one", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "paid-one", "pricing": {"prompt": "0.0000015", "completion": "0.000006"}},
        {"id": "free-two", "pricing": {"input": 0, "output": 0}},
    ]}
    assert app._zero_priced_ids(payload) == ["free-one", "free-two"]


def test_a_row_with_no_price_is_not_assumed_free():
    """Unknown must never read as free — that routes a paid model as free."""
    assert app._zero_priced_ids({"data": [{"id": "mystery"}]}) == []


def test_a_partly_priced_row_is_not_free():
    """Free prompt + paid completion is a PAID model."""
    payload = {"data": [{"id": "half", "pricing": {"prompt": "0", "completion": "0.002"}}]}
    assert app._zero_priced_ids(payload) == []


def test_pricing_zero_providers_can_now_see_a_new_free_model():
    """End of the freeze: with prices in hand, is_free_model stops failing closed."""
    pricing_zero = [pid for pid, p in prov.PROVIDERS.items()
                    if p.get("free_filter") == "pricing_zero"]
    assert pricing_zero, "no pricing_zero provider left to cover"
    pid = pricing_zero[0]
    assert prov.is_free_model(pid, "brand-new-free", known_free=["brand-new-free"])
    assert not prov.is_free_model(pid, "brand-new-paid", known_free=["brand-new-free"])


# --------------------------------------------------------------------------- #
# 4. a newly adopted id that does not work must not loop forever
# --------------------------------------------------------------------------- #

def test_unsupported_model_is_recognised_as_missing():
    """Auto-adoption means the hub WILL sometimes pick up an id the provider
    cannot serve — dahl advertises GLM-5.2 but answers
    400 'unsupported model'. Without this it burns a chain hop on every single
    request, forever, because 400 is not in _DEAD_STATUSES."""
    assert app._MISSING_MODEL_RE.search('unsupported model "zai-org/GLM-5.2"')
    assert app._MISSING_MODEL_RE.search("this model is not supported")
