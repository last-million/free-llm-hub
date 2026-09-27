"""The adaptive time budgets are set from MEASURED latency, not by hand.

SNAPSHOT: /api/model-speed on the live hub, 2026-09-27 (samples since its
last start). Each row: (id, ttft samples, ttft p50 ms, ttft p95 ms). This
file re-derives every constant the comment block above _ADAPTIVE_HOP_FLOOR
in app.py cites, so a later re-tune only has to swap the snapshot.
"""
import math

import app as A

SNAPSHOT = [
    ("llm7/codestral-latest", 17, 1544, 98381),        # p95 is one hung stream
    ("groq/qwen/qwen3.8-27b", 18, 4649, 15397),
    ("glm/glm-4.5-flash", 16, 14466, 48154),
    ("kilocode/stepfun/step-3.7-flash:free", 14, 15401, 22235),
    ("pollinations/openai-fast", 12, 34424, 55101),
    ("nvidia/z-ai/glm-5.3-flash", 9, 28162, 62288),
    ("zenmux/z-ai/glm-4.6v-flash-free", 8, 13860, 20656),
    ("g4f/srv:z-ai/glm-5.3", 7, 7701, 22528),
    ("openrouter/qwen/qwen3.8-27b:free", 7, 13836, 18152),
    ("g4f/srv:anthropic/claude-haiku-4-5", 6, 12080, 14986),
    ("google/models/gemini-3.8-flash", 5, 9979, 12609),
    ("kilocode/liquid/lfm-2.5-2.6b:free", 4, 13960, 19008),
    ("nvidia/moonshotai/kimi-k3", 3, 5708, 18772),
    ("g4f/srv:moonshotai/kimi-k3", 1, 3000, 3000),
    ("llm7/GLM-5.3-Flash", 1, 6783, 6783),
    ("openrouter/liquid/lfm-2.5-2.6b:free", 1, 7837, 7837),
    ("g4f/srv:models/gemini-3.7-flash", 1, 9237, 9237),
    ("google/models/gemini-3-flash-preview", 1, 28962, 28962),
]
# codestral's TOTAL duration (non-streamed, n=7): the fastest class measured.
FASTEST_TOTAL_P50_MS, FASTEST_TOTAL_P95_MS = 1293, 2229
HUNG = 10.0          # p95 > 10x p50: a hang, not a tail


def _healthy(min_n):
    return [r for r in SNAPSHOT if r[1] >= min_n and r[3] / r[2] <= HUNG]


def _median(v):
    v = sorted(v)
    n = len(v)
    return (v[(n - 1) // 2] + v[n // 2]) / 2.0


def _round_up_half(x):
    return math.ceil(x * 2) / 2.0


def test_tail_ratios_match_the_documented_numbers():
    ratios = sorted(round(r[3] / r[2], 2) for r in _healthy(5))
    assert ratios == [1.24, 1.26, 1.31, 1.44, 1.49, 1.6, 2.21, 2.93, 3.31, 3.33]


def test_adaptive_multiplier_covers_the_heaviest_healthy_tail():
    worst = max(r[3] / r[2] for r in _healthy(5))
    assert A._ADAPTIVE_HOP_MULT == _round_up_half(worst) == 3.5
    # every healthy model's p95 fits in MULT x p50 (p90 >= p50 always)
    for _id, _n, p50, p95 in _healthy(5):
        assert A._ADAPTIVE_HOP_MULT * p50 >= p95, _id


def test_hedge_multiplier_is_the_median_tail_ratio():
    med = _median([r[3] / r[2] for r in _healthy(5)])
    assert abs(med - 1.55) < 0.01
    assert A._HEDGE_DELAY_MULT == round(med * 2) / 2.0 == 1.5


def test_floors_sit_above_the_fastest_class():
    assert A._ADAPTIVE_HOP_FLOOR >= 2.5 * FASTEST_TOTAL_P95_MS / 1000.0
    # codestral's p50 is healthy even though its p95 is a hang
    fastest_ttft_p50 = min(r[2] for r in SNAPSHOT if r[1] >= 5) / 1000.0
    assert fastest_ttft_p50 == 1.544
    assert A._HEDGE_DELAY_MIN >= A._HEDGE_DELAY_MULT * fastest_ttft_p50


def test_trivial_ceiling_covers_most_measured_p95s():
    rows = [r for r in SNAPSHOT if r[1] >= 4 and r[3] / r[2] <= HUNG]
    p95s = sorted(round(r[3] / 1000.0, 1) for r in rows)
    assert p95s == [12.6, 15.0, 15.4, 18.2, 19.0, 20.7, 22.2, 22.5, 48.2, 55.1, 62.3]
    covered = sum(1 for p in p95s if p <= A._TRIVIAL_HOP_BUDGET)
    assert covered == 8
    # ...and every model above the ceiling is already not speed-first
    for _id, _n, p50, p95 in rows:
        if p95 / 1000.0 > A._TRIVIAL_HOP_BUDGET:
            assert p50 > A._SIMPLE_SLOW_MS, _id


def test_simple_slow_line_splits_the_fleet_near_its_median():
    p50s = [r[2] for r in SNAPSHOT]
    assert A._percentile(p50s, 50) == 12080       # the hub's own nearest-rank p50
    faster = sum(1 for v in p50s if v <= A._SIMPLE_SLOW_MS) / len(p50s)
    assert 0.35 <= faster <= 0.6


def test_the_live_budget_uses_the_derived_multiplier(monkeypatch):
    # groq qwen3.8-27b shape: fast p50, heavy tail, p90 near its p50
    monkeypatch.setattr(A, "_ttft", {("p", "m"): [4000] * 19 + [15000]})
    monkeypatch.setattr(A, "_speed", {})
    assert A._adaptive_hop_budget("p", "m", 25) == 14.0      # 3.5 x 4.0 s
