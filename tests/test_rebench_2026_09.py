"""Static ranking re-checked against the public boards on 2026-09-27.

Every ORDER asserted here is one two independent sources agree on
(Artificial Analysis Intelligence Index + the Arena text leaderboard), or one
authoritative narrow-domain board for a category (LMArena Vision, tau-bench,
Terminal-Bench 4.0 on tbench.ai + AA). Pairs the boards DISAGREE on are left
unasserted on purpose -- see the comments at each change in app.py and
model_categories.py.
"""
import pytest

import app
import model_categories as MC


@pytest.fixture(autouse=True)
def no_aa(monkeypatch):
    monkeypatch.setattr(app, "_aa_scores", {})


def s(model, pid="p"):
    return app._benchmark_score(pid, model)


# --------------------------------------------------------------------------- #
# New-version heuristic reads a VERSION, not a parameter count
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("model", ["meta/codellama-70b", "qwen-72b-chat",
                                   "gemini-exp-1206"])
def test_a_size_or_date_is_not_a_new_version(model):
    assert app._strong_new_version_score(app._canon_model_id(model.lower())) == 0


@pytest.mark.parametrize("model", ["llama-5-70b", "qwen/qwen3.8-27b", "glm-5.3",
                                   "deepseek-ai/deepseek-v4.1-flash", "kimi-k3",
                                   "minimax-m3", "models/gemini-3.8-flash"])
def test_real_versions_still_count(model):
    assert app._strong_new_version_score(app._canon_model_id(model.lower())) == 100


def test_codellama_no_longer_outranks_the_current_field():
    """Live 2026-09-27: nvidia/meta/codellama-70b scored 104, #2 in coding."""
    assert s("meta/codellama-70b", "nvidia") < s("mistral-medium-3.5")


# --------------------------------------------------------------------------- #
# GLM-5.3-Flash: AA 42 / Arena 1474
# --------------------------------------------------------------------------- #

def test_glm_53_flash_above_what_both_boards_rank_under_it():
    flash = s("GLM-5.3-Flash", "llm7")
    assert flash > s("deepseek-ai/deepseek-v4-pro", "nvidia")   # AA 36 / Arena 1458
    assert flash > s("MiniMaxAI/MiniMax-M3")                     # AA 29 / Arena 1440


def test_glm_53_flash_below_what_both_boards_rank_over_it():
    flash = s("GLM-5.3-Flash", "llm7")
    for over in ("z-ai/glm-5.3",          # AA 45 / Arena 1480
                 "moonshotai/kimi-k3",    # AA 44 / Arena 1488
                 "qwen/qwen3.8-max"):     # AA 45 / Arena 1479
        assert flash < s(over), over


def test_glm_flash_newer_never_lower_and_stays_under_dsv4():
    f53, f54, f60 = s("glm-5.3-flash"), s("glm-5.4-flash"), s("glm-6-flash")
    assert f53 <= f54 <= f60
    assert f60 < app._PREF_FLOORS[9]


def test_other_speed_cuts_keep_the_soft_cap():
    """Only the GLM flash cut was measured; the rule does not leak."""
    assert s("z-ai/glm-5.3-air") <= app._STRONG_SPEED_CAP + 1.0
    assert s("glm-4.7-flash") <= 30


# --------------------------------------------------------------------------- #
# MiMo-V2.6-Pro: AA 46 / Arena 1480
# --------------------------------------------------------------------------- #

def test_mimo_26_pro_joins_the_qwen38_level():
    mimo = s("xiaomi/mimo-v2.6-pro")
    assert mimo >= s("qwen/qwen3.8-max")                        # 46 v 45, 1480 v 1479
    assert mimo > s("deepseek-ai/deepseek-v4.1-flash")          # 39 / 1477
    assert mimo > s("minimax-m3")                               # 29 / 1440
    assert mimo < s("kimi-k3")                                  # disputed -> unchanged


def test_mimo_floor_matches_relay_spellings():
    for mid in ("XiaomiMiMo/MiMo-V2.6-Pro", "mimo-z/mimo-v2.6-pro"):
        assert s(mid, "g4f") >= app._MIMO_PRO_FLOOR - app._RELAY_DISCOUNT["g4f"], mid


def test_mimo_older_or_speed_cuts_are_not_floored():
    assert s("xiaomi/mimo-v2.5-pro") < s("minimax-m3")         # AA 26 < 29
    assert s("opencode-zen/mimo-v2.6-flash-free") < app._MIMO_PRO_FLOOR


def test_mimo_version_bump_is_bounded():
    assert s("mimo-v2.6-pro") <= s("mimo-v2.7-pro") <= s("mimo-v9-pro")
    assert s("mimo-v9-pro") < s("hy3")


# --------------------------------------------------------------------------- #
# DeepSeek V4.x: AA V4.1 Flash 39 > V4 Pro 36 > V4 Flash Vision 35
# --------------------------------------------------------------------------- #

def test_deepseek_v41_above_v4_but_under_qwen38_and_gemini38():
    v41 = s("deepseek-ai/deepseek-v4.1-flash", "nvidia")
    assert v41 > s("deepseek-ai/deepseek-v4-flash", "nvidia")
    assert v41 > s("deepseek-ai/deepseek-v4-pro", "nvidia")
    assert v41 < s("qwen/qwen3.8-max")              # AA 45 / Arena 1479
    assert v41 < s("models/gemini-3.8-flash")       # AA 41 / Arena 1492


def test_user_rule_flash_over_pro_survives_the_minor_bump():
    assert s("deepseek-v4.1-pro") < s("deepseek-v4-flash")


# --------------------------------------------------------------------------- #
# Mid field: Gemini 3.5 Flash-Lite > Mistral Medium 3.5 > Mistral Large 3 > Llama
# --------------------------------------------------------------------------- #

def test_mid_field_order_both_boards_agree_on():
    lite = s("models/gemini-3.5-flash-lite", "google")   # AA 22 / Arena 1456
    medium = s("mistral-medium-3.5")                      # AA 14 / Arena 1426
    large = s("mistral-large-3")                          # AA 9  / Arena 1413
    assert lite > medium > large
    for pid, llama in (("cerebras", "llama-3.3-70b-instruct"),   # AA 8* / 1318
                       ("groq", "llama-3.3-70b-versatile"),
                       ("nvidia", "meta/llama-4-maverick-17b-128e-instruct")):
        assert medium > s(llama, pid), llama
    assert large > s("llama-3.3-70b-instruct", "cerebras")


def test_flash_lite_lift_is_bounded():
    assert s("models/gemini-3.5-flash-lite", "google") < app._TOOLS_MIN_SCORE
    assert s("models/gemini-3.1-flash-lite", "google") <= 31   # no board row


def test_llama_still_clears_the_simple_floor_on_its_fast_hosts():
    floor = app._DIFFICULTY_FLOOR["simple"]
    assert s("llama-3.3-70b-versatile", "groq") >= floor
    assert s("llama-3.3-70b-instruct", "cerebras") >= floor


# --------------------------------------------------------------------------- #
# AA lookup reaches vendor-namespaced ids
# --------------------------------------------------------------------------- #

def test_aa_lookup_matches_across_vendor_namespaces(monkeypatch):
    monkeypatch.setattr(app, "_aa_scores", {"deepseekdeepseekv41flash": 88.7,
                                            "minimaxm27": 70.0})
    # a pre-fix cache key (vendor joined on) still matches exactly
    assert app._aa_score_for("deepseek-ai/deepseek-v4.1-flash") == 88.7
    # a post-fix key matches every host's spelling
    assert app._aa_score_for("MiniMaxAI/MiniMax-M2.7") == 70.0
    assert app._aa_score_for("minimax/minimax-m2.7") == 70.0
    assert app._aa_score_for("unknown-model-x") is None


def test_aa_slug_strips_openrouter_vendor_namespaces():
    for a, b in (("qwen/qwen3.8-max", "qwen3.8-max"),
                 ("deepseek/deepseek-v4.1-flash", "deepseek-ai/deepseek-v4.1-flash"),
                 ("xiaomi/mimo-v2.6-pro", "mimo-v2.6-pro"),
                 ("x-ai/grok-4.7", "grok-4.7")):
        assert app._normalize_aa_slug(a) == app._normalize_aa_slug(b), a


# --------------------------------------------------------------------------- #
# Category membership
# --------------------------------------------------------------------------- #

def cats(identity):
    return MC.categories_for("prov", identity, identity)


@pytest.mark.parametrize("model", ["qwen3.8-max", "glm-5.3-flash", "kimi-k2.6"])
def test_lmarena_vision_leaders_are_in_vision(model):
    assert "vision" in cats(model)


@pytest.mark.parametrize("model", ["gemini-3.8-flash", "grok-4.7"])
def test_terminal_bench_leaders_are_in_coding(model):
    assert "coding" in cats(model)


@pytest.mark.parametrize("model", ["qwen3.8-max", "glm-5.2"])
def test_tau_bench_leaders_are_in_swarm(model):
    assert "swarm" in cats(model)


def test_seo_still_excludes_the_flash_cut():
    assert "seo" not in cats("glm-5.3-flash")
