"""Ox Alpha is GLM-5.3, it works, and it belongs in the top band.

ASKED 2026-08-30: "can you check if ox alpha model available? if yes put it
with best models it's the best one now".

WHAT IT ACTUALLY IS. "Ox Alpha" was the stealth name GLM-5.3-Flash shipped
under -- listed unnamed and free on OpenRouter 2026-08-20, revealed 2026-08-26.
Published coding numbers under that name beat the models already at the top of
_PREF_FLOORS: DeepSWE 80% against Claude Fable 5's 65% and GPT-5.6 Sol's 52%.

WHAT IS ACTUALLY REACHABLE, measured against the live catalog rather than
assumed. Searching all 460 discovered ids for "ox-alpha" returned exactly one:

    g4f/srv_mp5miql908c8738d71be:pegalink/ox-alpha-agent

...which answered HTTP 400 on 6 of 6 attempts across three id spellings, every
one falling through to kilocode. Promoting THAT would have put a dead model at
the top of the chain. The real model is reachable and was verified answering
before anything here was changed:

    tokenrouter/z-ai/glm-5.3-free   -> served by itself, replied "OK"
    g4f/GLM:GLM-5.3-Flash           -> fell through to kilocode (g4f: 78/181)
    g4f/OrcaRouter:z-ai/glm-5.3     -> fell through to kilocode

Two things were wrong once it was found.
"""
import app


# --------------------------------------------------------------------------- #
# 1. glm-5.3 scored exactly the same as glm-5.2
# --------------------------------------------------------------------------- #

def test_glm_5_3_joins_the_top_band():
    """It was floored at _PREF_FLOORS[7] (134) as plain "glm-5.x", tied with
    5.2 -- so the newest, strongest member of the family got no credit for
    being newer, and sat below claude (138) and every gpt-5.x (135+)."""
    top = app._PREF_FLOORS[5]                     # the claude/top-band floor
    assert app._benchmark_score("tokenrouter", "z-ai/glm-5.3-free") >= top
    assert app._benchmark_score("tokenrouter", "z-ai/glm-5.3") >= top


def test_the_flash_variant_is_the_one_that_was_ox_alpha():
    """Ox Alpha WAS glm-5.3-flash specifically, so the -flash spelling must not
    be the one that misses the promotion."""
    top = app._PREF_FLOORS[5]
    assert app._benchmark_score("tokenrouter", "z-ai/glm-5.3-flash") >= top
    assert app._benchmark_score("glm", "glm-5.3-flash") >= top


def test_glm_5_2_is_left_exactly_where_the_user_put_it():
    """An earlier, explicit preference: "GPT-5 versions are better than GLM
    5.2". Promoting 5.3 must not quietly drag 5.2 up past gpt-5.x with it."""
    s52 = app._benchmark_score("glm", "glm-5.2")
    assert s52 == app._PREF_FLOORS[7], s52
    assert s52 < app._PREF_FLOORS[5]


def test_later_glm_versions_stay_in_the_top_band():
    top = app._PREF_FLOORS[5]
    assert app._benchmark_score("glm", "glm-5.4") >= top
    assert app._benchmark_score("glm", "glm-6") >= top


def test_glm_4_is_untouched():
    """glm-4.x variants are explicitly excluded from the glm floor and left to
    the speed cap -- measured, glm-4.6v-flash scores 30."""
    assert app._benchmark_score("glm", "glm-4.6v-flash") < 100
    assert app._benchmark_score("glm", "glm-4.7-flash") < app._PREF_FLOORS[5]


def test_a_relayed_copy_still_pays_the_relay_discount():
    """g4f's copies of glm-5.3 are exactly the ones that did NOT answer. The
    discount is applied after the floors for this reason."""
    assert (app._benchmark_score("g4f", "GLM:GLM-5.3-Flash")
            < app._benchmark_score("tokenrouter", "z-ai/glm-5.3-free"))


# --------------------------------------------------------------------------- #
# 2. the '-free' spelling made one model look like two
# --------------------------------------------------------------------------- #

def test_a_hyphen_free_suffix_is_the_same_model():
    """openrouter marks its free tier ':free' and that was already stripped;
    tokenrouter writes '-free', which was not -- so 'z-ai/glm-5.3-free' and
    'z-ai/glm-5.3' compared as two unrelated models. Every piece of same-model
    machinery reads this: cross-provider load sharing, the shared quota
    penalty, and the swarm's own dedupe (which would otherwise buy one model's
    opinion twice under two spellings)."""
    ident = app._normalize_model_identity
    assert ident("z-ai/glm-5.3-free") == ident("z-ai/glm-5.3") == "glm-5.3"
    assert ident("moonshotai/kimi-k3-free") == ident("moonshotai/kimi-k3")
    assert ident("z-ai/glm-5.2:free") == ident("z-ai/glm-5.2")


def test_free_is_only_stripped_as_a_suffix():
    """A model whose NAME contains the word must survive intact."""
    ident = app._normalize_model_identity
    assert ident("acme/free-willy-7b") == "free-willy-7b"
    assert ident("acme/freeform-2") == "freeform-2"


def test_genuinely_different_models_still_differ():
    ident = app._normalize_model_identity
    assert ident("z-ai/glm-5.3-free") != ident("z-ai/glm-5.2")
    assert ident("openai/gpt-oss-120b") != ident("openai/gpt-oss-20b")


def test_the_two_spellings_now_share_the_load():
    """The point of the identity fix: two hosts of one model alternate instead
    of one being drained while the other sits idle."""
    from unittest import mock
    pool = [(134.0, "tokenrouter", "z-ai/glm-5.3-free"), (134.0, "glm", "glm-5.3")]
    with mock.patch.object(app, "_quota_headroom", return_value=1.0):
        seen = {app._pick_same_model_host(pool, ("glm", "glm-5.3"))[0]
                for _ in range(200)}
    assert seen == {"tokenrouter", "glm"}, seen
