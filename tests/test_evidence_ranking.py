"""Ranking follows the public boards (OWNER DECISION 2026-10-10).

  "benchmark them in our hub like the public boards, and in future a NEW version
   must of course rank higher than them automatically."

Two evidence kinds, two routing paths (benchmarks.py + app._benchmark_score /
app._agentic_score):

  * AGENTIC evidence (Terminal-Bench 4.0 %, AutomationBench %) orders TOOL turns.
    Today: GLM 5.3 > GLM 5.3 Flash > DeepSeek V4.1 Flash > Gemini 3.8 Flash >
    Kimi K3 > Qwen 3.8 27B, with GLM 5.3 clearly OUTSIDE the auto band above
    Kimi K3 (a 29-point TB4.0 gap is never a coin flip).
  * GENERAL evidence (AA Intelligence Index + LMArena) orders TOOL-FREE (chat)
    turns: Kimi K3, GLM 5.3 and Gemini 3.8 Flash close at the top, the rest below.

Hermetic: no network. The TOOL-order tests zero the learned/dialect/sustain
penalties (as test_best_model_leads does) so the pure board order is asserted;
production layers those real penalties on top (e.g. DeepSeek V4's documented
tool-dialect penalty still demotes it on live tool turns).
"""
import pytest

import app
import benchmarks


GLM53 = "z-ai/glm-5.3"
GLM53_FLASH = "z-ai/glm-5.3-flash"
DSV41_FLASH = "deepseek-ai/deepseek-v4.1-flash"
GEMINI38_FLASH = "models/gemini-3.8-flash"
KIMI_K3 = "moonshotai/kimi-k3"
QWEN_27B = "qwen/qwen3.8-27b"
SIX = [GLM53, GLM53_FLASH, DSV41_FLASH, GEMINI38_FLASH, KIMI_K3, QWEN_27B]

PID = "nvidia"


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    # No live AA (general_floor uses the dated benchmarks table, not live AA).
    monkeypatch.setattr(app, "_aa_scores", {})
    benchmarks.reset()


@pytest.fixture
def _no_penalties(monkeypatch):
    """Zero every learned/dialect/sustain penalty so _agentic_score shows the
    pure board order; production keeps them (see the module docstring)."""
    for name in ("_tool_dialect_penalty", "_reliability_penalty",
                 "_latency_penalty", "_answer_quality_penalty"):
        monkeypatch.setattr(app, name, lambda *a, **k: 0.0)
    monkeypatch.setattr(app, "_sustain_penalty", lambda pid: 0.0)


def gen(mid, pid=PID):
    return app._benchmark_score(pid, mid)


def tool(mid, pid=PID):
    return app._agentic_score((gen(mid, pid), pid, mid))


# --------------------------------------------------------------------------- #
# 1. AGENTIC evidence orders tool turns
# --------------------------------------------------------------------------- #

def test_tool_turn_order_is_exactly_the_terminal_bench_order(_no_penalties):
    order = sorted(SIX, key=lambda m: -tool(m))
    assert order == [GLM53, GLM53_FLASH, DSV41_FLASH, GEMINI38_FLASH, KIMI_K3, QWEN_27B], \
        {m: round(tool(m), 3) for m in SIX}


def test_glm53_is_outside_the_band_above_kimi_on_tool_turns(_no_penalties):
    assert tool(GLM53) - tool(KIMI_K3) > app._AUTO_TOP_BAND          # 29-pt TB4.0 gap


def test_a_close_agentic_pair_stays_inside_the_band(_no_penalties):
    assert 0 < tool(GLM53) - tool(GLM53_FLASH) < app._AUTO_TOP_BAND  # 9-pt TB4.0 gap


def test_qwen_27b_is_last_on_tool_turns(_no_penalties):
    assert tool(QWEN_27B) == min(tool(m) for m in SIX)


# --------------------------------------------------------------------------- #
# 2. GENERAL evidence orders tool-free (chat) turns -- a DIFFERENT order
# --------------------------------------------------------------------------- #

def test_chat_top_three_are_close_and_the_rest_are_below():
    top3 = sorted(SIX, key=lambda m: -gen(m))[:3]
    assert set(top3) == {KIMI_K3, GEMINI38_FLASH, GLM53}
    spread = max(gen(m) for m in top3) - min(gen(m) for m in top3)
    assert spread < 0.5, {m: round(gen(m), 3) for m in top3}        # "close at the top"
    for low in (GLM53_FLASH, DSV41_FLASH, QWEN_27B):
        assert gen(low) < min(gen(m) for m in top3), low            # the rest below


def test_chat_order_is_not_the_tool_order(_no_penalties):
    # GLM 5.3 leads agent work but not chat; Kimi K3 tops the chat cluster but is
    # near the bottom of the agentic order -- the two paths genuinely differ.
    assert max(SIX, key=lambda m: -gen(m) * 0 + gen(m)) != GLM53     # chat leader is not GLM 5.3
    assert max(SIX, key=tool) == GLM53                               # tool leader IS GLM 5.3
    assert gen(KIMI_K3) >= gen(GLM53) and tool(KIMI_K3) < tool(GLM53)


# --------------------------------------------------------------------------- #
# 3. A new, unlisted version inherits its predecessor's evidence + a bump
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("newer,older", [
    ("z-ai/glm-5.4", GLM53),
    ("moonshotai/kimi-k3.5", KIMI_K3),
    ("moonshotai/kimi-k4", KIMI_K3),
    ("deepseek-ai/deepseek-v4.2-flash", DSV41_FLASH),
    ("qwen/qwen3.9-27b", QWEN_27B),
])
def test_a_new_version_lands_just_above_its_predecessor(_no_penalties, newer, older):
    assert gen(newer) > gen(older)                                  # general: just above
    assert tool(newer) > tool(older)                                # tool: just above
    assert gen(newer) - gen(older) < app._AUTO_TOP_BAND             # "just" above, not a leap


def test_a_new_version_does_not_vault_past_the_owner_floors():
    assert gen("z-ai/glm-5.4") < app._PREF_FLOORS[5]                # under Claude's 138
    assert gen("moonshotai/kimi-k4") < app._PREF_FLOORS[12]         # under Space Bunny


def test_the_inherited_row_is_flagged():
    ev = benchmarks.agentic_evidence("z-ai/glm-5.4")
    assert ev and ev.get("inherited_from") == GLM53
    assert benchmarks.agentic_evidence(GLM53).get("inherited_from") is None


# --------------------------------------------------------------------------- #
# 4. Size variants never inherit the full-size flagship floor
# --------------------------------------------------------------------------- #

def test_qwen_27b_sits_below_the_full_size_flagship():
    assert gen(QWEN_27B) < gen("qwen/qwen3.8-max")


def test_qwen_27b_below_glm53_flash_and_deepseek_v41_flash():
    assert gen(QWEN_27B) < gen(GLM53_FLASH)
    assert gen(QWEN_27B) < gen(DSV41_FLASH)


def test_the_flagship_keeps_its_own_family_floor():
    # qwen3.8-max is not in the evidence table, so it keeps the qwen floor (~134).
    assert benchmarks.general_evidence("qwen/qwen3.8-max") is None
    assert gen("qwen/qwen3.8-max") >= app._PREF_FLOORS[7]


# --------------------------------------------------------------------------- #
# 5. Models with no board evidence are untouched
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("mid", ["minimax-m3", "mistral-large-3", "llama-3.3-70b",
                                 "claude-opus-5", "gpt-5.6-sol"])
def test_models_without_evidence_get_no_bonus_and_no_floor(mid):
    assert app._ev_agentic_bonus(mid) == 0.0
    assert app._ev_general_floor(app._canon_model_id(mid.lower())) is None
    assert benchmarks.agentic_evidence(mid) is None


# --------------------------------------------------------------------------- #
# 6. Relay copies still pay the discount
# --------------------------------------------------------------------------- #

def test_relay_copy_scores_under_the_first_party_model():
    assert gen("GLM:GLM-5.3", "g4f") < gen(GLM53, "tokenrouter")     # relay discount


def test_relay_claude_never_leads_first_party_glm53_on_tool_turns():
    # Real penalties on purpose (the g4f sustain penalty is the point).
    relay_claude = app._agentic_score(
        (app._benchmark_score("g4f", "srv_x:anthropic/claude-opus-5.5"),
         "g4f", "srv_x:anthropic/claude-opus-5.5"))
    assert relay_claude < tool(GLM53)


# --------------------------------------------------------------------------- #
# 7. Keyless refresh hook parses an OpenRouter-catalog-shaped sample
# --------------------------------------------------------------------------- #

def test_keyless_parse_extracts_agentic_fields_when_present():
    # A FORWARD-COMPATIBLE sample: the day OpenRouter exposes these sub-indexes,
    # the hook reads them with no code change (none are published today).
    row = {"id": "z-ai/glm-5.3", "benchmarks": {"artificial_analysis": {
        "intelligence_index": 45.0, "terminal_bench_4": 42.0, "automation_bench": 62.0}}}
    got = benchmarks.parse_openrouter_row(row)
    assert got == {"aa": 45.0, "tb4": 42.0, "automation": 62.0}


def test_keyless_parse_todays_shape_has_only_intelligence_index():
    # What OpenRouter actually serves now: the top-line index, no sub-indexes.
    row = {"id": "moonshotai/kimi-k3",
           "benchmarks": {"artificial_analysis": {"intelligence_index": 44.0}}}
    got = benchmarks.parse_openrouter_row(row)
    assert got == {"aa": 44.0}
    assert "tb4" not in got and "automation" not in got


def test_keyless_parse_never_raises_on_junk():
    for row in (None, {}, {"benchmarks": None}, {"benchmarks": {"artificial_analysis": 7}},
                "nope", {"benchmarks": {}}):
        assert benchmarks.parse_openrouter_row(row) == {}


# --------------------------------------------------------------------------- #
# 8. /api/tracking evidence views
# --------------------------------------------------------------------------- #

def test_tracking_views_carry_both_evidence_kinds():
    ag = app._ev_agentic_evidence_view(GLM53)
    assert ag["tb4"] == 42 and ag["automation"] == 62 and ag["date"] == benchmarks.DATE
    gn = app._ev_general_evidence_view(GLM53)
    assert gn["aa"] == 45 and gn["arena"] == 1478
    assert app._ev_agentic_evidence_view("minimax-m3") is None
    assert app._ev_general_evidence_view("minimax-m3") is None
