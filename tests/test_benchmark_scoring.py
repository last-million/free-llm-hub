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

def test_user_ranking_claude_then_gpt5_then_kimi_k3_then_gemini():
    """USER RANKING, 2nd revision 2026-07-31: claude > gpt-5.x > kimi-k3 >
    gemini. K3 was briefly top; the user corrected it to sit AFTER the gpt-5
    family and claude."""
    kimi = app._benchmark_score("testpid", "kimi-k3")
    claude = app._benchmark_score("puter", "claude-opus-5")
    gpt = app._benchmark_score("puter", "gpt-5.6-sol")
    gemini = app._benchmark_score("puter", "gemini-3-pro")
    assert claude > gpt > kimi > gemini, (claude, gpt, kimi, gemini)
    # kimi-k3 and claude are flat floors; the GPT floor SCALES with the version
    # (since 2026-07-31), so it is checked as a band rather than an exact value.
    # REVISED: kimi-k3 now ranks BELOW claude and every gpt-5.x, per
    # "kimi k3 is the best one AFTER gpt models from 5 up and claude models".
    assert claude == 138
    assert 135 <= gpt < 138
    assert kimi < gpt, (kimi, gpt)


def test_gemini_is_ranked_last_by_getting_no_floor_at_all():
    """A floor only ever LIFTS a model, so gemini is ranked last by being left
    on its natural score — not by inventing a negative floor."""
    gemini = app._benchmark_score("puter", "gemini-3-pro")
    # index 4 is 0 since the kimi-k2 floor was removed, so compare against the
    # lowest ACTIVE floor rather than min() of the raw tuple.
    assert gemini < min(f for f in app._PREF_FLOORS if f)


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
    assert gpt4o < min(f for f in app._PREF_FLOORS if f)
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


def test_every_available_claude_model_is_top_including_legacy():
    """USER DIRECTIVE, restated: "ALL available Claude models should be in top."
    That now includes the legacy ids (claude-v2, claude-instant, claude-2.x) —
    I had excluded them as 2023 models and the user overruled it."""
    for mid in ("claude-opus-5", "claude-opus-4", "claude-sonnet-4-5",
                "claude-fable-5", "claude-haiku-4-5", "claude-3-5-haiku",
                "anthropic.claude-v2", "claude-instant-1.2", "claude-2.1",
                "claude-3-opus", "us.anthropic.claude-opus-4-6-v1:0",
                "anthropic/claude-sonnet-4", "Claude-Opus-5"):
        assert app._benchmark_score("p", mid) >= app._PREF_FLOORS[5], mid


def test_claude_is_exempt_from_the_speed_cap():
    """The cap runs LAST and overrode the floor: claude-instant matched the
    "instant" keyword and landed at 30 despite being floored to 138. A floor
    that a later rule can silently undo is not a floor."""
    assert app._benchmark_score("p", "claude-instant-1.2") == app._PREF_FLOORS[5]
    # ...but the cap still bites everything else.
    for mid in ("gpt-4o-mini", "gemini-3.1-flash-lite", "mistral-nemo"):
        assert app._benchmark_score("p", mid) <= 30, mid


def test_the_claude_floor_still_excludes_the_cli_relay_handles():
    """'claude' / 'claude-agentrouter' name the local CLI RELAY, not a model.
    Promoting them would push free routing onto a PAID subscription hop."""
    for mid in ("claude", "claude-agentrouter"):
        assert app._benchmark_score("p", mid) < app._PREF_FLOORS[5], mid


def test_deepseek_v4_is_level_with_glm52_and_minimax_m3_sits_under_both():
    """USER 2026-08-01: "DeepSeek V4 should have priority almost as much as
    GLM 5.2, and MiniMax M3 is also good, almost like DeepSeek 4." Then, later
    the same day: "deepseek v4 seems good so it should be the SAME level as
    glm 5.2" -- so this is equality now, not "just under".

    Order asserted, not raw numbers, so the tiers can be re-tuned without
    rewriting the test."""
    glm = app._benchmark_score("p", "glm-5.2")
    ds4 = app._benchmark_score("p", "deepseek-v4")
    mm3 = app._benchmark_score("p", "minimax-m3")
    assert glm == ds4 > mm3
    # ...and both clear the whole ordinary field.
    #
    # qwen3.5+ LEFT this list on 2026-08-30. The preference has always named
    # "latest kimi / qwen / deepseek" as top picks together, but only kimi, glm
    # and deepseek were ever floored, so qwen sat on its natural score (measured:
    # qwen3.8-27b 110.9 vs glm-5.2 134.0) and never won a slot. Floating it to
    # its named peers necessarily lifts it past minimax-m3, which was only ever
    # "almost like deepseek 4" -- i.e. just under that top group, not inside it.
    # The older qwen3 / qwen2.5 lines stay ordinary field and are still asserted.
    for weaker in ("qwen3-32b", "qwen2.5-72b-instruct", "mimo-2.5",
                   "deepseek-v3.1", "minimax-m2", "kimi-k2.7"):
        assert mm3 > app._benchmark_score("p", weaker), weaker


def test_deepseek_v4_flash_beats_the_pro_variant():
    """USER CORRECTION 2026-08-03: "deepseek v4 flash is better than the pro --
    the pro is LESS than the flash." The opposite order from Gemini (where pro
    beats flash) -- confirms the two families are judged independently, not
    via one shared "pro always wins" rule.

    Flash keeps the family's established level (equal to glm-5.2, per the
    still-standing "same level" instruction); pro drops one notch below it but
    must stay above minimax-m3, which sits "almost like deepseek 4"."""
    glm = app._benchmark_score("p", "glm-5.2")
    flash = app._benchmark_score("p", "deepseek-v4-flash")
    pro = app._benchmark_score("p", "deepseek-v4-pro")
    mm3 = app._benchmark_score("p", "minimax-m3")
    assert flash == glm, "flash must keep the family's established level"
    assert flash > pro > mm3, (flash, pro, mm3)


def test_the_named_top_four_still_outrank_them():
    """The new floors must not displace the ranking the user set earlier."""
    ds4 = app._benchmark_score("p", "deepseek-v4")
    for stronger in ("claude-sonnet-4-5", "gpt-5.6", "kimi-k3", "hy3-large"):
        assert app._benchmark_score("p", stronger) > ds4, stronger


def test_the_floors_reach_every_id_spelling():
    """Relays spell these several ways; a floor that only matches one is a floor
    that silently does not apply."""
    ds4 = app._PREF_FLOORS[9]
    for mid in ("deepseek-v4", "deepseek-v4-flash", "deepseek/deepseek-v4-flash",
                "morph-dsv4flash", "DeepSeek-V4"):
        assert app._benchmark_score("p", mid) >= ds4, mid
    mm3 = app._PREF_FLOORS[10]
    for mid in ("minimax-m3", "MiniMaxAI/MiniMax-M3", "morph-minimax3-428b"):
        assert app._benchmark_score("p", mid) >= mm3, mid


def test_older_generations_keep_their_measured_score():
    """Only v4+/m3+ are floored. v3/r1 and m2 must NOT be lifted."""
    for mid in ("deepseek-v3.1", "deepseek-r1", "minimax-m2", "minimax-m2.7"):
        assert app._benchmark_score("p", mid) < app._PREF_FLOORS[10], mid


def test_gemini_31_and_up_beat_deepseek_v4():
    """USER 2026-08-01: "gemini 3.1, 3.5, 3.6 and up versions are better than
    deepseek 4 or v4."

    Still true after deepseek-v4 drew level with glm-5.2 -- which is what
    pushed gemini above glm as well. See test_gemini_now_outranks_glm52."""
    ds4 = app._benchmark_score("p", "deepseek-v4")
    for mid in ("gemini-3.1-pro", "models/gemini-3.5-flash",
                "models/gemini-3.6-flash", "gemini-4"):
        assert app._benchmark_score("p", mid) > ds4, mid


def test_gemini_versions_are_monotonic():
    """A newer gemini must always outrank an older one — scaling on the minor
    alone put gemini-4.0 BELOW gemini-3.6."""
    ordered = ["gemini-3.1", "gemini-3.5", "gemini-3.6", "gemini-4", "gemini-4.2",
               "gemini-5"]
    scores = [app._benchmark_score("p", m) for m in ordered]
    assert scores == sorted(scores), list(zip(ordered, scores))


def test_gemini_30_and_older_get_no_floor():
    """The user named 3.1 as the floor of the good ones."""
    ds4 = app._benchmark_score("p", "deepseek-v4")
    for mid in ("gemini-3.0-pro", "models/gemini-3-flash-preview",
                "gemini-2.5-flash"):
        assert app._benchmark_score("p", mid) < ds4, mid


def test_floored_gemini_survives_the_speed_cap():
    """Nearly every gemini id is a '-flash'. The cap runs LAST, so without an
    exemption it would undo the floor on the whole family and the user's
    ranking would silently not hold."""
    assert app._benchmark_score("p", "models/gemini-3.6-flash") > \
        app._benchmark_score("p", "deepseek-v4")
    # ...but an OLD gemini flash is still capped.
    assert app._benchmark_score("p", "gemini-2.5-flash") <= 30
    # ...and flash-LITE is Google's cheapest tier, neither "flash" nor "pro":
    # the user named "flash or pro", so it stays in the small tier it was in.
    assert app._benchmark_score("p", "gemini-3.1-flash-lite-preview") <= 40


def test_gemini_pro_beats_gemini_flash():
    """USER 2026-08-01: "gemini pro is better than gemini flash, of course."
    The two bands must not overlap, so pro of ANY version beats flash of any."""
    assert app._benchmark_score("p", "gemini-3.1-pro") > \
        app._benchmark_score("p", "gemini-5-flash")
    for v in ("3.1", "3.5", "3.6", "4"):
        assert app._benchmark_score("p", "gemini-%s-pro" % v) > \
            app._benchmark_score("p", "gemini-%s-flash" % v), v


"""REPLACED by test_gemini_now_outranks_glm52_because_deepseek_drew_level_with_it.

test_gemini_stays_under_glm52 asserted the opposite and was right at the time:
gemini sat between glm-5.2 and deepseek-v4, so "under glm" and "over deepseek"
were both satisfiable. When the user put deepseek level with glm-5.2 that gap
closed, and the two things they actually SAID -- gemini beats deepseek-v4,
deepseek-v4 equals glm-5.2 -- force gemini above glm. "Under glm" was never a
stated preference, only an inference from a gap that no longer exists, so the
test asserting it had to go rather than be quietly loosened.
"""


def test_gemini_now_outranks_glm52_because_deepseek_drew_level_with_it():
    """Not a preference of its own -- a consequence of two the user did state.

    "gemini 3.1+ beats deepseek-v4" and "deepseek-v4 is the same level as
    glm-5.2" cannot both hold with gemini below glm. Gemini was only ever put
    under glm by inference (it sat between glm and deepseek, and deepseek was
    lower); that gap closed, so the inference went with it."""
    glm = app._benchmark_score("p", "glm-5.2")
    for mid in ("gemini-3.1-pro", "models/gemini-3.5-flash", "gemini-4"):
        assert app._benchmark_score("p", mid) > glm, mid


def test_gemini_pro_still_beats_gemini_flash_of_any_version():
    """The one thing the user was emphatic about ("gemini pro is better than
    gemini flash, of course") must survive the band move."""
    assert app._benchmark_score("p", "gemini-3.1-pro") >            app._benchmark_score("p", "gemini-5-flash")


def test_the_named_top_four_still_outrank_gemini():
    """Moving gemini up must not push it past the four the user ranked highest."""
    top = app._benchmark_score("p", "hy3-large")
    for mid in ("gemini-3.1-pro", "gemini-9-pro"):
        assert app._benchmark_score("p", mid) < top, mid
