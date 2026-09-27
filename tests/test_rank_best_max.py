"""best/max must mean the genuinely strongest AND reliable model.

MEASURED 2026-09: auto, best and max all picked llm7/GLM-5.3-Flash. The flash
speed cap was skipped for strong roots (glm >= 5 -> sv = 100) and the GLM >= 5.3
floor gave the FAST cut Claude's 138, so it outranked every full frontier model.
And the chat hard/medium pick had no reliability term, so a model with a real
string of failures kept the primary slot on name alone.
"""
import pytest

import app


@pytest.fixture
def no_aa(monkeypatch):
    monkeypatch.setattr(app, "_aa_scores", {})


# --------------------------------------------------------------------------- #
# Scoring: speed-tier cuts never share the flagship's rank
# --------------------------------------------------------------------------- #

# REBENCH 2026-09-27: deepseek-v4-pro and qwen3-max left this list. Artificial
# Analysis (GLM-5.3-Flash 42 vs DeepSeek V4 Pro 36) and the Arena text board
# (1474 vs 1458) both rank the flash cut ABOVE V4 Pro; Qwen3 Max has no row on
# either board and AA's newer Qwen3.7 Max already sits below GLM-5.3-Flash.
# See tests/test_rebench_2026_09.py for the measured order.
FRONTIER = [
    ("nvidia", "z-ai/glm-5.2"),
    ("tokenrouter", "z-ai/glm-5.3"),
    ("nvidia", "moonshotai/kimi-k3"),
    ("openrouter", "anthropic/claude-opus-5"),
    ("openrouter", "qwen/qwen3.8-max"),
    ("nvidia", "deepseek-ai/deepseek-v4-flash"),
]


@pytest.mark.parametrize("pid,model", FRONTIER)
def test_glm_53_flash_ranks_below_every_full_frontier_model(no_aa, pid, model):
    flash = app._benchmark_score("llm7", "GLM-5.3-Flash")
    assert flash < app._benchmark_score(pid, model), model


def test_glm_53_flash_is_not_in_the_top_preference_band(no_aa):
    """Measured floor (rebench 2026-09-27), still under the deepseek-v4 /
    glm-5.2 level and far under the claude/glm-5.3 band."""
    flash = app._benchmark_score("llm7", "GLM-5.3-Flash")
    assert flash < app._PREF_FLOORS[9]
    assert flash < app._PREF_FLOORS[5]


def test_strong_speed_variant_stays_a_usable_agentic_fallback(no_aa):
    """Softer cap, not the 30-point tiny tier: still clears the tool bar."""
    assert app._benchmark_score("llm7", "GLM-5.3-Flash") >= app._TOOLS_MIN_SCORE


def test_user_ranked_flash_families_are_untouched(no_aa):
    """deepseek-v4 flash > pro (user 2026-08-03); gemini 3.1+ flash keeps its band."""
    assert (app._benchmark_score("nvidia", "deepseek-ai/deepseek-v4-flash")
            >= app._benchmark_score("nvidia", "deepseek-ai/deepseek-v4-pro"))
    assert app._benchmark_score("google", "gemini-3.5-flash") > 134


@pytest.mark.parametrize("model,expected", [
    ("GLM-5.3-Flash", True),
    ("z-ai/glm-5.3-air", True),
    ("gpt-5-mini", True),
    ("qwen3-small", True),
    ("z-ai/glm-5.3", False),
    ("minimax-m3", False),          # '-mini' prefix of a longer word
    ("mistral/ministral-8b", False),
    ("claude-instant-2", False),    # user: every Claude in the top
    ("deepseek-v4-flash", False),   # user: v4 flash > pro
    ("gemini-3.5-flash", False),    # user-ranked gemini band
])
def test_is_speed_variant(model, expected):
    assert app._is_speed_variant(model) is expected


# --------------------------------------------------------------------------- #
# Routing: quality_mode prefers full models; reliability costs the primary
# --------------------------------------------------------------------------- #

@pytest.fixture
def two_live(monkeypatch):
    live = {"llm7": ["GLM-5.3-Flash"], "nvidia": ["z-ai/glm-5.2"]}
    monkeypatch.setattr(app, "_available_providers", lambda: list(live))
    monkeypatch.setattr(app, "_auto_models", lambda pid: list(live.get(pid, ())))
    monkeypatch.setattr(app, "_sub_available_providers", lambda: [])
    monkeypatch.setattr(app, "_is_model_dead", lambda pid, m: False)
    monkeypatch.setattr(app, "_context_ok", lambda pid, m, est: True)
    monkeypatch.setattr(app.prov, "is_model_allowed", lambda m: True)
    monkeypatch.setattr(app.quota, "is_model_throttled", lambda pid, m: False)
    monkeypatch.setattr(app.quota, "model_status", lambda pid, m: {"exhausted": False})
    monkeypatch.setattr(app, "_quota_headroom", lambda pid: 1.0)
    monkeypatch.setattr(app, "_is_fast", lambda pid, m: pid == "llm7")
    monkeypatch.setattr(app, "_reliability_penalty", lambda pid, m: 0.0)
    monkeypatch.setattr(app, "_latency_penalty", lambda pid, m: 0.0)
    monkeypatch.setattr(app, "_aa_scores", {})
    # Pin the default (always-best) so the machine's real config can't flip
    # the pick into spread mode; spread mode gets its own test below.
    _real_flag = app.config.get_flag
    monkeypatch.setattr(app.config, "get_flag",
                        lambda name, default=False: True if name == "route_always_best"
                        else _real_flag(name, default))
    app._session_pins.clear()
    yield live
    app._session_pins.clear()


def _msgs(text):
    return [{"role": "user", "content": text}]


def test_best_picks_full_glm_over_glm_53_flash_real_scores(two_live):
    pid, model, _d = app._route_by_difficulty(_msgs("hi there"), quality_mode=True)
    assert (pid, model) == ("nvidia", "z-ai/glm-5.2")


def test_quality_mode_prefers_full_tier_even_when_flash_outscores(two_live, monkeypatch):
    """Partition, not just score: even a flash id that out-scores (e.g. via an
    AA number) yields the max-quality primary to a live full model."""
    scores = {("llm7", "GLM-5.3-Flash"): 150.0, ("nvidia", "z-ai/glm-5.2"): 134.0}
    monkeypatch.setattr(app, "_benchmark_score", lambda pid, m: scores[(pid, m)])
    pid, model, _d = app._route_by_difficulty(_msgs("hi again"), quality_mode=True)
    assert model == "z-ai/glm-5.2"


def test_quality_mode_fails_open_to_flash_when_nothing_else_lives(two_live, monkeypatch):
    two_live.pop("nvidia")
    pid, model, _d = app._route_by_difficulty(_msgs("hello"), quality_mode=True)
    assert model == "GLM-5.3-Flash"


_HARD_TEXT = (
    "refactor the whole routing chain, then write code for comprehensive "
    "tests, debug any failures, and optimize performance " + "x" * 2000
)


def test_poor_reliability_loses_the_primary_slot(two_live, monkeypatch):
    scores = {("llm7", "GLM-5.3-Flash"): 138.0, ("nvidia", "z-ai/glm-5.2"): 134.0}
    monkeypatch.setattr(app, "_benchmark_score", lambda pid, m: scores[(pid, m)])
    # Baseline: the higher score wins when neither has a track record.
    pid, _m, diff = app._route_by_difficulty(_msgs(_HARD_TEXT))
    assert diff == "hard" and pid == "llm7"
    app._session_pins.clear()
    # Measured failures on the leader: 0 ok / 5 fail -> real penalty.
    monkeypatch.setattr(app, "_reliability_penalty",
                        lambda pid, m: 6.4 if pid == "llm7" else 0.0)
    pid, _m, _d = app._route_by_difficulty(_msgs(_HARD_TEXT + " again"))
    assert pid == "nvidia"


def test_spread_mode_stops_rotating_onto_an_unreliable_hop(two_live, monkeypatch):
    """route_always_best off: the band rotation must not keep handing a proven
    failure one turn in N."""
    monkeypatch.setattr(app.config, "get_flag",
                        lambda name, default=False: False if name == "route_always_best"
                        else default)
    scores = {("llm7", "GLM-5.3-Flash"): 134.0, ("nvidia", "z-ai/glm-5.2"): 134.0}
    monkeypatch.setattr(app, "_benchmark_score", lambda pid, m: scores[(pid, m)])
    monkeypatch.setattr(app, "_reliability",
                        lambda pid, m: 0.14 if pid == "llm7" else 0.5)
    for i in range(6):
        app._session_pins.clear()
        pid, _m, _d = app._route_by_difficulty(_msgs(_HARD_TEXT + " turn %d" % i))
        assert pid == "nvidia"


def test_chat_pick_key_uses_the_real_ledger(monkeypatch):
    """_chat_pick_key folds in _reliability_penalty from the outcome ledger."""
    monkeypatch.setattr(app, "_quota_headroom", lambda pid: 1.0)
    monkeypatch.setattr(app, "_latency_penalty", lambda pid, m: 0.0)
    # Seeded directly: _record_outcome persists to the real state dir.
    monkeypatch.setattr(app, "_outcomes", {
        ("llm7", "GLM-5.3-Flash"): {"ok": 0, "fail": 5, "last": app.time.time()}})
    bad = app._chat_pick_key((138.0, "llm7", "GLM-5.3-Flash"))
    good = app._chat_pick_key((134.0, "nvidia", "z-ai/glm-5.2"))
    assert good > bad
