r"""The current Nemotron generation (3.5 and later) is a normal model; the older
ones stay last resorts (owner decision 2026-10-23: "allow the latest nemotron
3.5").

One version cut, nemotron_gen.ALLOWED_FROM, compared as an (major, minor) tuple.
Every id below is an id the hub's own /v1/models listed on 2026-10-23 (copied
verbatim, relay prefixes and all) unless it is marked hypothetical.

Pinned here:
  * 3.5 and every later version pass, in every spelling the catalogs use;
  * Nemotron 3 nano / super / ultra, the 2024 Nemotron-4 340B and the
    llama-/mistral-nemotron fine-tunes do NOT pass (the number 4 in
    nemotron-4-340b is not a newer generation than 3.5);
  * utility variants (content-safety, parse, reward) never pass;
  * the exemption never shadows gpt-oss / gemma;
  * the shipped blocklist never blocks a current chat Nemotron.
"""
import pytest

import app as A
import nemotron_gen as N

# --- ids the hub saw -------------------------------------------------------- #
CURRENT = [
    "nvidia/nvidia/nemotron-3.5-super-vl-120b-a12b",
    "nvidia/nvidia/nemotron-3.5-lightning-30b-a3b",
    "nvidia/nvidia/nemotron-3.5-bolt-e4b",
    "openrouter/nvidia/nemotron-3.5-super-vl:free",
    "openrouter/nvidia/nemotron-3.5-lightning:free",
    "kilocode/nvidia/nemotron-3.5-super-vl:free",
    "kilocode/nvidia/nemotron-3.5-lightning:free",
    "g4f/srv_mkombumpae45db46dcb8:nvidia/nemotron-3.5-lightning-30b-a3b",
    "g4f/srv_mve15k17fe3d549fd0b8:nvidia/nemotron-3.5-super-vl:free",
    "g4f/srv_mkombumpae45db46dcb8:nvidia/nemotron-3.5-super-vl-120b-a12b",
    "g4f/srv_monk1pkz433a519ff2be:nvidia/nemotron-3.5-lightning:free",
    # bare identities and other spellings of the same thing
    "nemotron-3.5-super-vl-120b-a12b",
    "NVIDIA-Nemotron-3.5-Super-VL",
    "nemotron-3-5-lightning",            # a dash for the dot
    "nemotron_3_5_lightning",
    "nemotron-v3.5-super",
]
# Hypothetical later generations: they must pass with no code change.
FUTURE = [
    "nemotron-3.6-ultra-600b",
    "nvidia/nemotron-3.10-super",
    "nemotron-4-ultra-1t",               # version, then a tier word: not legacy
    "nemotron-4.0-nano",
    "nemotron-5-super-vl",
    "openrouter/nvidia/nemotron-4-super:free",
    "g4f/srv_abc123:nvidia/nemotron-5.1-lightning:free",
    "nemotron-nano-3.5-8b",              # the old tier-before-version order
]
OLDER = [
    "nvidia/nvidia/nemotron-3-ultra-550b-a55b",
    "nvidia/nvidia/nemotron-3-super-120b-a12b",
    "nvidia/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
    "nvidia/nvidia/nemotron-nano-3-30b-a3b",
    "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
    "openrouter/nvidia/nemotron-3-super-120b-a12b:free",
    "kilocode/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "llm7/nemotron-3-nano:30b",
    "g4f/srv_mrgykg8eea645e7bb006:nemotron-3-nano:30b",
    "g4f/srv_mrgykg8eea645e7bb006:nemotron-3-ultra",
    "g4f/srv_mrgxsoeba484330133cb:nemotron-3-super",
    "g4f/srv_mp2i8rco3148dd85bec1:nemotron-3-ultra",   # digits in the relay prefix
    "g4f/pa:547dae23:nemotron-3-ultra-free",
    "g4f/KiloCode:nvidia/nemotron-3-super-120b-a12b",
    "nemotron-3.4-lightning",            # hypothetical: just under the cut
    "nemotron-3.0-super",
    "nemotron-3-4-super",
]
LEGACY = [
    "nvidia/nvidia/nemotron-4-340b-instruct",     # the 2024 model: 4 is NOT > 3.5
    "nvidia/nvidia/nemotron-4-340b-reward",
    "nemotron-4-340b-base",
    "nemotron-4-mini-hindi-4b-instruct",
    "nemotron-mini-4b-instruct",
    "nvidia/nvidia/llama-3.1-nemotron-51b-instruct",
    "nvidia/nvidia/llama-3.1-nemotron-70b-instruct",
    "nvidia/nvidia/llama-3.1-nemotron-ultra-253b-v1",
    "llama-3.3-nemotron-super-49b-v1.5",
    "nemotron-nano-9b-v2",
    "mistral-nemotron",
    "nemotron",
]
UTILITY_35 = [
    "nvidia/nvidia/nemotron-3.5-content-safety",
    "openrouter/nvidia/nemotron-3.5-content-safety:free",
    "kilocode/nvidia/nemotron-3.5-content-safety:free",
    "g4f/srv_mkombumpae45db46dcb8:nvidia/nemotron-3.5-content-safety",
    "nemotron-3.6-parse",
    "nemotron-3.5-reward",
    "nemotron-3.5-embed",
]
UTILITY_OLD = ["nvidia/nvidia/nemotron-parse", "nvidia/nvidia/nemotron-parse-2.0"]


# --- the parser -------------------------------------------------------------- #

def test_the_cut_is_one_named_tuple():
    assert N.ALLOWED_FROM == (3, 5)
    assert isinstance(N.ALLOWED_FROM, tuple) and len(N.ALLOWED_FROM) == 2


@pytest.mark.parametrize("model", CURRENT + FUTURE)
def test_current_and_later_generations_pass(model):
    assert N.is_current_chat(model), model
    assert N.generation(model) >= N.ALLOWED_FROM


@pytest.mark.parametrize("model", OLDER + LEGACY + UTILITY_35 + UTILITY_OLD)
def test_older_legacy_and_utility_variants_do_not_pass(model):
    assert not N.is_current_chat(model), model


def test_the_generation_is_read_exactly():
    assert N.generation("nemotron-3.5-lightning") == (3, 5)
    assert N.generation("nemotron-3-5-lightning") == (3, 5)
    assert N.generation("nemotron-3-ultra-550b-a55b") == (3, 0)
    assert N.generation("nemotron-nano-3-30b-a3b") == (3, 0)
    assert N.generation("nemotron-4-ultra-1t") == (4, 0)
    assert N.generation("nemotron-3.10-x") == (3, 10)          # 10 > 5, not "1.0"
    assert N.generation("g4f/srv_mp2i8rco3148dd85bec1:nemotron-3-ultra") == (3, 0)


def test_nemotron_4_340b_is_the_2024_model_not_a_newer_generation():
    for model in ("nemotron-4-340b-instruct", "nemotron-4-340b-reward",
                  "nemotron-4-mini-hindi-4b"):
        assert N.generation(model) is None
        assert not N.is_current_chat(model)
    # ...while a FUTURE Nemotron 4 (version, then a tier word) passes.
    assert N.is_current_chat("nemotron-4-ultra-1t")
    assert N.is_current_chat("nemotron-4-super")


def test_a_size_or_date_is_never_taken_for_a_version():
    assert N.generation("nemotron-340b") is None
    assert N.generation("nemotron-70b-instruct") is None
    assert N.generation("nemotron-3-30b") is None
    assert N.generation("nemotron-2025-instruct") is None


def test_utility_variants_are_non_chat_whatever_their_generation():
    for model in UTILITY_35 + UTILITY_OLD + ["nvidia/nvidia/nemotron-4-340b-reward"]:
        assert N.is_non_chat(model), model
    for model in CURRENT + OLDER:
        assert not N.is_non_chat(model), model


def test_a_relay_prefix_is_never_read_as_part_of_the_id():
    # the 3.5 tail after the prefix wins, whatever digits/words the prefix holds
    assert N.is_current_chat("g4f/srv_guard35embed:nvidia/nemotron-3.5-lightning")
    assert not N.is_current_chat("g4f/srv_3535353535:nemotron-3-ultra")


def test_unreadable_input_never_raises_and_means_not_current():
    for junk in (None, "", 0, 3.5, [], {}, "nemotron-", "-", "gemma-3", "gpt-oss-120b"):
        assert N.is_current_chat(junk) is False
        assert N.generation(junk) is None


# --- the hub's last-resort rule --------------------------------------------- #

@pytest.mark.parametrize("model", CURRENT + FUTURE)
def test_a_current_nemotron_is_not_a_last_resort(model):
    assert A._is_low_quality(model) is False, model


@pytest.mark.parametrize("model", OLDER + LEGACY + UTILITY_35 + UTILITY_OLD)
def test_older_nemotron_stays_a_last_resort(model):
    assert A._is_low_quality(model) is True, model


def test_gpt_oss_and_gemma_stay_last_resorts():
    for model in ("openai/gpt-oss-120b", "@cf/openai/gpt-oss-20b", "gemma-4-31b-it",
                  "google/gemma-3-27b-it", "gpt_oss_120b"):
        assert A._is_low_quality(model) is True, model


def test_the_exemption_never_shadows_gpt_oss_or_gemma():
    # an id that names a current Nemotron AND a demoted family is still demoted
    assert A._is_low_quality("nemotron-3.5-gemma-merge") is True
    assert A._is_low_quality("nemotron-3.5-gpt-oss-distill") is True


def test_ordinary_models_are_untouched():
    for model in ("zai-glm-4.7", "moonshotai/kimi-k2.6", "gemini-3.8-flash", "", None):
        assert A._is_low_quality(model) is False


# --- the shipped blocklist --------------------------------------------------- #

def test_no_shipped_default_blocks_a_current_chat_nemotron():
    bad = [i for i in A._DEFAULT_BLOCKED_IDENTITIES if N.is_current_chat(i)]
    assert bad == [], "a current Nemotron must not ship blocked: %r" % bad


def test_the_3_5_chat_variants_left_the_shipped_list_and_the_classifier_stayed():
    assert "nemotron-3.5-lightning-30b" not in A._DEFAULT_BLOCKED_IDENTITIES
    assert "nemotron-3.5-lightning-30b-a3b" not in A._DEFAULT_BLOCKED_IDENTITIES
    assert "nemotron-3.5-content-safety" in A._DEFAULT_BLOCKED_IDENTITIES
    # older generations are still shipped blocked
    for ident in ("nemotron-3-nano", "nemotron-3-super", "nemotron-3-ultra",
                  "nemotron-3-super-120b-a12b", "nemotron-4-340b-instruct"):
        assert ident in A._DEFAULT_BLOCKED_IDENTITIES, ident


def test_retired_ids_are_the_ones_that_left_the_shipped_list():
    assert A._RETIRED_DEFAULT_BLOCKS == {"nemotron-3.5-lightning-30b",
                                         "nemotron-3.5-lightning-30b-a3b"}
    assert not (A._RETIRED_DEFAULT_BLOCKS & A._DEFAULT_BLOCKED_IDENTITIES)
    for ident in A._RETIRED_DEFAULT_BLOCKS:
        assert N.is_current_chat(ident)


def test_a_fresh_install_does_not_block_the_current_generation(monkeypatch):
    data = {}
    monkeypatch.setattr(A.config, "get_setting", lambda k, d=None: data.get(k, d))
    monkeypatch.setattr(A.config, "set_setting", lambda k, v: data.__setitem__(k, v))
    A._seed_default_blocks()
    with A.app.test_request_context("/v1/chat/completions"):
        for model in ("nvidia/nemotron-3.5-super-vl-120b-a12b",
                      "nvidia/nemotron-3.5-lightning-30b-a3b",
                      "nvidia/nemotron-3.5-lightning:free",
                      "nvidia/nemotron-3.5-super-vl:free",
                      "nvidia/nemotron-3.5-bolt-e4b"):
            assert not A._is_model_blocked_by_user("nvidia", model), model
        # the older ones and the classifier are off, in every host's spelling
        for model in ("nvidia/nemotron-3-super-120b-a12b",
                      "nvidia/nemotron-3.5-content-safety",
                      "nvidia/nemotron-4-340b-instruct"):
            assert A._is_model_blocked_by_user("nvidia", model), model
