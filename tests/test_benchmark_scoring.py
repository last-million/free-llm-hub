import app


def test_gemini_flash_lite_does_not_outscore_flagship_models():
    """Regression: a bare "gemini-3" tier-1 substring used to match
    "gemini-3.1-flash-lite-preview" too (Google's cheapest free tier), which
    outscored every real candidate (101.0) and got picked as "the best" by
    /api/default/auto -- reproduced live against a real running hub."""
    flash_lite = app._benchmark_score("google", "models/gemini-3.1-flash-lite-preview")
    deepseek_v3 = app._benchmark_score("deepseek", "deepseek-v3-0324")
    llama_4_maverick = app._benchmark_score("github-models", "llama-4-maverick")
    gemini_2_5_pro = app._benchmark_score("google", "gemini-2.5-pro")
    assert flash_lite < deepseek_v3
    assert flash_lite < llama_4_maverick
    assert flash_lite < gemini_2_5_pro
    assert flash_lite < 40  # should land in the weak/small tier, not flagship


def test_gemini_ids_do_not_collide_with_bare_mini_pattern():
    """Regression: the weak-tier "mini" substring (meant for "gpt-4o-mini" etc.)
    also matches "gemini" — ge-MINI — so a bare substring test dragged every
    Gemini id into the tiny/speed tier. Fixed by requiring a leading hyphen.

    ASSERTION CORRECTED 2026-07-31. It used to claim
    `untiered_gemini < real_mini_model`, which contradicted its own docstring
    and had been failing for some time: commit 9a48654 deliberately exempted
    gemini-3-flash from the speed cap, so a Gemini flagship now scores ABOVE a
    capped -mini model, which is the intended ordering. What the regression is
    actually about is that Gemini must not be CAPPED by the -mini pattern, so
    that is what is asserted now."""
    gemini = app._benchmark_score("google", "models/gemini-3-flash-preview")
    real_mini_model = app._benchmark_score("openai", "gpt-4o-mini")
    # A real '-mini' id IS capped into the tiny tier...
    assert real_mini_model <= 40, real_mini_model
    # ...while 'gemini' escapes that pattern entirely. If the collision ever
    # comes back, gemini lands at the same capped value and this fails.
    assert gemini > real_mini_model
    assert gemini > 40, "gemini got dragged into the tiny tier by the -mini pattern"
    # The cap itself still works on Gemini ids that genuinely ARE small.
    assert app._benchmark_score("google", "gemini-3.1-flash-lite") <= 40


def test_true_flagship_gemini_still_scores_top_tier():
    """A genuine flagship id (gemini-3-pro / 3.5-pro / *-ultra) must still
    hit the top tier -- the fix narrows the substring, it doesn't remove
    flagship recognition entirely."""
    assert app._benchmark_score("google", "gemini-3-pro") >= 100
    assert app._benchmark_score("google", "gemini-3.5-pro") >= 100
    assert app._benchmark_score("google", "gemini-3-ultra") >= 100


# --------------------------------------------------------------------------- #
# 2026-07-30 retune: qwen back to top tier (demotion removed); nemotron /
# gpt-oss / gemma demoted BELOW the strong families; glm-4.x, gemini-2.5-pro,
# llama-4 / 3.3-70b, mistral-large, gpt-4o, MiMo promoted to the top bands.
# --------------------------------------------------------------------------- #

def test_qwen3_outranks_gpt_oss():
    """Same-class ids: qwen3 is a strong family again while gpt-oss is
    demoted -- qwen3 must score clearly ABOVE gpt-oss, not below it."""
    qwen3 = app._benchmark_score("testpid", "qwen3-32b")
    gpt_oss = app._benchmark_score("testpid", "gpt-oss-120b")
    assert qwen3 > gpt_oss


def test_qwen_demotion_is_gone():
    """The 2026-07-25 -45 qwen demotion is REMOVED: a bare qwen3 id scores its
    natural 108 (100 strong-root + 8 coding boost), not 108 - 45 = 63."""
    assert app._benchmark_score("testpid", "qwen3") == 108.0


def test_glm47_outranks_nemotron3():
    """glm-4.x is top-tier per the user; nemotron-3 is demoted -- even the
    biggest nemotron id (550b, on the +1.2 nvidia bias) stays well below."""
    glm = app._benchmark_score("testpid", "glm-4.7")
    nemotron = app._benchmark_score("nvidia", "nemotron-3-ultra-550b-a55b")
    assert glm > nemotron


def test_gemini25_flash_outranks_gemma4():
    """gemini-2.5+ belongs to the strong bands (the flash variant is still
    speed-capped at 30) while the gemma family is demoted below them."""
    gemini = app._benchmark_score("google", "gemini-2.5-flash")
    gemma = app._benchmark_score("google", "gemma-4")
    assert gemini > gemma


def test_demoted_families_stay_below_the_strong_band():
    """No nemotron / gpt-oss / gemma id -- at any plausible size -- may reach
    the strong-family band (Tier A = 84 and up)."""
    strong_floor = app._benchmark_score("testpid", "mistral-large")  # 84
    for demoted in ("nemotron-3-ultra-550b-a55b", "nemotron-3-120b",
                    "gpt-oss-120b", "gpt-oss-20b", "gemma-4", "gemma-3-27b"):
        assert app._benchmark_score("testpid", demoted) < strong_floor, demoted


def test_promoted_families_hit_the_top_bands():
    """The user's top-tier list: llama-4 / llama-3.3-70b / mistral-large /
    gpt-4o land in Tier A (84+); glm-4.7 / gemini-2.5-pro / qwen3-max in Tier S."""
    for tier_a in ("llama-4-maverick", "llama-3.3-70b-instruct",
                   "mistral-large", "gpt-4o"):
        assert app._benchmark_score("testpid", tier_a) >= 84, tier_a
    for tier_s in ("glm-4.7", "gemini-2.5-pro", "qwen3-max"):
        assert app._benchmark_score("testpid", tier_s) >= 100, tier_s


# --------------------------------------------------------------------------- #
# puter preference floors (2026-07-30): the newest GPT flagship via puter is
# the user-requested TOP priority — the gpt-5.6-sol(-pro) class floors at 136,
# one above hy3 (135); gpt-5.6-terra / gpt-5.5-pro floor level with hy3.
# Id-keyed like the other floors (no provider-id checks in routing).
# --------------------------------------------------------------------------- #

def test_user_ranking_kimi_k3_then_claude_then_gpt55_then_gemini():
    """USER RANKING, stated explicitly 2026-07-31 and replacing the earlier
    puter-sol-first order: kimi-k3 > claude > gpt-5.5-and-up > gemini."""
    kimi = app._benchmark_score("testpid", "kimi-k3")
    claude = app._benchmark_score("puter", "claude-opus-5")
    gpt = app._benchmark_score("puter", "gpt-5.6-sol")
    gemini = app._benchmark_score("puter", "gemini-3-pro")
    assert kimi > claude > gpt > gemini, (kimi, claude, gpt, gemini)
    # kimi-k3 and claude are flat floors; the GPT floor SCALES with the version
    # (since 2026-07-31), so it is checked as a band rather than an exact value.
    assert (kimi, claude) == (140, 138)
    assert 135 <= gpt < 138


def test_gemini_is_ranked_last_by_getting_no_floor_at_all():
    """A floor only ever LIFTS a model, so gemini is ranked last by being left
    on its natural score — not by inventing a negative floor."""
    gemini = app._benchmark_score("puter", "gemini-3-pro")
    assert gemini < min(app._PREF_FLOORS)


def test_every_claude_id_shape_gets_the_claude_floor():
    for mid in ("claude-opus-5", "anthropic/claude-fable-5", "claude-3-7-sonnet",
                "claude-opus-4-6"):
        assert app._benchmark_score("puter", mid) == app._PREF_FLOORS[5], mid
    # ...but a CLI-relay id is a subscription hop, scored elsewhere.
    assert not app._CLAUDE_FAMILY_RE.search("claude-agentrouter")


def test_the_gpt_floor_scales_with_the_version():
    """"All GPT-5 versions are good, and a higher version means it's better" —
    two flat tiers could not express that (gpt-5.0 and gpt-5.4 tied), so the
    floor is computed from the version number itself."""
    scores = [app._benchmark_score("puter", "gpt-5.%d" % i) for i in range(7)]
    assert scores == sorted(scores), scores
    assert scores[0] < scores[-1], "a higher version must actually rank higher"
    # every GPT-5 outranks GLM 5.2...
    assert min(scores) > app._benchmark_score("puter", "glm-5.2")
    # ...and gpt-6 outranks every gpt-5...
    assert app._benchmark_score("puter", "gpt-6") > max(scores)
    # ...but nothing in the family may overtake claude, a deliberate ranking.
    assert app._benchmark_score("puter", "gpt-9.9") < app._benchmark_score("puter", "claude-opus-5")
    # gpt-4.x is not in the family at all.
    assert app._benchmark_score("puter", "gpt-4.1") < 100


def test_puter_sol_keeps_its_own_floor():
    """Its floor now comes from the version-scaled GPT rule, so it is >= the
    old flat 136 rather than exactly equal to it."""
    sol = app._benchmark_score("puter", "gpt-5.6-sol")
    assert sol >= app._PREF_FLOORS[2] == 136
    assert app._benchmark_score("puter", "gpt-5.6-sol-pro") == sol


def test_plain_gpt_4o_does_not_get_the_puter_floor():
    # The floor is pinned to the 5.6-sol / 5.6-terra / 5.5-pro ids only — a
    # plain gpt-4o keeps its natural Tier A score, far below every floor.
    gpt4o = app._benchmark_score("puter", "gpt-4o")
    assert gpt4o < min(app._PREF_FLOORS)
    assert gpt4o >= 84  # still Tier A, just not floored
    # gpt-5.4 IS floored since 2026-07-31 ("all GPT-5 versions are good, and a
    # higher version means better") — the bar moved from 5.5 down to the whole
    # GPT-5 family. What must stay unfloored is gpt-4.x.
    assert app._benchmark_score("puter", "gpt-5.4") > app._benchmark_score("puter", "glm-5.2")
    # gpt-5.6-luna, however, IS 5.5-and-up: since 2026-07-31 it takes the
    # gpt-5.5+ floor by rule rather than needing its own pinned entry.
    assert app._benchmark_score("puter", "gpt-5.6-luna") >= app._PREF_FLOORS[6]


def test_every_real_claude_id_shape_is_floored():
    """"All Claude models are good — if available they should be used." Providers
    ship the same model under very different id shapes, and a shape that misses
    the floor is a Claude the router will not reach for."""
    for mid in ("claude-opus-5", "claude-opus-4-6", "claude-sonnet-4-5",
                "claude-haiku-4-5", "claude-fable-5", "claude-3-7-sonnet",
                "claude-3-5-haiku", "claude-4.5-sonnet",
                "anthropic/claude-opus-4.6-fast", "claude-opus-4-1-20250805",
                # AWS Bedrock shapes — these were MISSED until 2026-07-31 because
                # the boundary class had no '.', so a whole id family scored 18.
                "us.anthropic.claude-sonnet-4-5-v1:0",
                "eu.anthropic.claude-opus-4-6",
                "bedrock/anthropic.claude-3-5-sonnet"):
        assert app._benchmark_score("p", mid) >= app._PREF_FLOORS[5], mid


def test_the_claude_floor_still_excludes_relay_and_legacy_ids():
    """Two deliberate exclusions:
    - 'claude' / 'claude-agentrouter' name the local CLI RELAY (a paid
      subscription hop), not a model the free router should promote.
    - claude-instant is a 2023 model; flooring it to 138 would rank it above
      GPT-5.6, which is the opposite of "use the good models"."""
    for mid in ("claude", "claude-agentrouter", "claude-instant-1.2"):
        assert app._benchmark_score("p", mid) < app._PREF_FLOORS[5], mid
