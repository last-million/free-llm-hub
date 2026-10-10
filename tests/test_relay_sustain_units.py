"""Relay quota units (OWNER DECISION 2026-10-10).

`_sustain_penalty` demotes a provider whose request budget is scarce PER DAY, so
a 50/day free tier does not out-rank a sustainable large provider on agentic
turns. It read the FREE_LIMITS `limit` as if it were always per-day, but the
table stores each limit in its OWN window (minute / day / month): a relay listed
as "5 requests per MINUTE" (g4f) was read as "5 per DAY" and demoted ~29 points.
A units bug -- the demotion was right in effect (g4f's real budget is tiny) but
wrong in mechanism.

The fix converts every request limit to a per-day equivalent first
(_rs_per_day_requests), so like compares with like. Flag relay_sustain_units
(default ON); OFF restores the old per-window-blind penalty byte for byte.

Hermetic: quota.status is faked, no network. Every other honest demotion (the
relay discount, the lead gate, throttles, measured failure) is proven to still
keep a relay behind a healthy first-party model.
"""
import time

import pytest

import app
import benchmarks
import config
import quota


# The fleet, shaped like the live one (quota.FREE_LIMITS windows, 2026-10-10):
# per-minute relays, day-window tiers, a month tier, and an unknown-budget host.
FLEET_LIMITS = {
    "g4f":        (5,    "minute"),   # relay; real budget is token-based
    "llm7":       (20,   "minute"),
    "navy":       (20,   "minute"),
    "nararouter": (10,   "minute"),
    "openrouter": (50,   "day"),      # genuinely scarce PER DAY -> keeps its penalty
    "groq":       (1000, "day"),      # abundant per day
    "cohere":     (1000, "month"),    # month window -> left as-is (conservative)
    "nvidia":     (None, "day"),      # unknown budget -> never penalised
}

# Old (per-window-blind) penalties: (150 - limit) / 5 for 0 < limit < 150.
OLD_PENALTY = {
    "g4f": 29.0, "llm7": 26.0, "navy": 26.0, "nararouter": 28.0,
    "openrouter": 20.0, "groq": 0.0, "cohere": 0.0, "nvidia": 0.0,
}

GLM53 = "z-ai/glm-5.3"
KIMI_K3 = "moonshotai/kimi-k3"
QWEN_27B = "qwen/qwen3.8-27b"
RELAY_OPUS = "srv_x:anthropic/claude-opus-5.5"
RELAY_GPT = "srv_x:openai/gpt-6.1"
RELAY_SONNET = "srv_x:anthropic/claude-sonnet-4-5"

# (pid, model) rows used for score ordering.
SCORE_FLEET = [
    ("nvidia", GLM53), ("nvidia", KIMI_K3), ("groq", QWEN_27B),
    ("g4f", RELAY_OPUS), ("g4f", RELAY_GPT), ("g4f", RELAY_SONNET),
]


def _fake_status(limits):
    def status(pid):
        lim, win = limits.get(pid, (None, "day"))
        return {"limit_known": isinstance(lim, int), "limit": lim, "window": win,
                "remaining": None, "throttled": False, "exhausted": False}
    return status


def _set_flag(monkeypatch, on):
    orig = config.get_flag

    def fake(name, default=False):
        if name == "relay_sustain_units":
            return on
        # Stay hermetic: every other flag reads as its shipped default.
        return default

    monkeypatch.setattr(config, "get_flag", fake)
    return orig


@pytest.fixture
def fleet(monkeypatch):
    monkeypatch.setattr(quota, "status", _fake_status(FLEET_LIMITS))
    monkeypatch.setattr(app, "_aa_scores", {})     # no live AA (as test_evidence_ranking)
    benchmarks.reset()
    yield


def gen(pid, model):
    return app._benchmark_score(pid, model)


def tool(pid, model):
    return app._agentic_score((gen(pid, model), pid, model))


# --------------------------------------------------------------------------- #
# 1. The unit conversion itself
# --------------------------------------------------------------------------- #

def test_per_day_conversion_factors():
    assert app._rs_per_day_requests(5, "minute") == 7200.0
    assert app._rs_per_day_requests(20, "hour") == 480.0
    assert app._rs_per_day_requests(50, "day") == 50.0
    assert app._rs_per_day_requests(1000, "month") == 1000.0   # unlisted -> as-is
    assert app._rs_per_day_requests(7, None) == 7.0            # unknown -> as-is


# --------------------------------------------------------------------------- #
# 2. Per-minute relays stop eating a daily-scarcity penalty (flag ON)
# --------------------------------------------------------------------------- #

def test_per_minute_relays_lose_the_penalty(fleet, monkeypatch):
    _set_flag(monkeypatch, True)
    for pid in ("g4f", "llm7", "navy", "nararouter"):
        assert app._sustain_penalty(pid) == 0.0, pid


def test_day_window_scarcity_is_untouched(fleet, monkeypatch):
    _set_flag(monkeypatch, True)
    assert app._sustain_penalty("openrouter") == 20.0   # 50/day really is scarce
    assert app._sustain_penalty("groq") == 0.0          # 1000/day abundant
    assert app._sustain_penalty("cohere") == 0.0        # month left as-is (1000 >= 150)
    assert app._sustain_penalty("nvidia") == 0.0        # unknown budget


# --------------------------------------------------------------------------- #
# 3. Flag OFF = the old penalty byte for byte
# --------------------------------------------------------------------------- #

def test_flag_off_is_the_old_penalty(fleet, monkeypatch):
    _set_flag(monkeypatch, False)
    for pid, old in OLD_PENALTY.items():
        assert app._sustain_penalty(pid) == old, pid


# --------------------------------------------------------------------------- #
# 4. The fix is surgical: _benchmark_score and the relay discount are unchanged
# --------------------------------------------------------------------------- #

def test_benchmark_score_is_unchanged_by_the_flag(fleet, monkeypatch):
    _set_flag(monkeypatch, True)
    on = {(p, m): gen(p, m) for p, m in SCORE_FLEET}
    _set_flag(monkeypatch, False)
    off = {(p, m): gen(p, m) for p, m in SCORE_FLEET}
    assert on == off          # _benchmark_score never used _sustain_penalty


def test_relay_discount_still_applies(fleet, monkeypatch):
    _set_flag(monkeypatch, True)
    # Same underlying model, relay vs first-party: g4f pays _RELAY_DISCOUNT.
    first_party = gen("nvidia", "anthropic/claude-opus-5.5")
    relay = gen("g4f", RELAY_OPUS)
    assert first_party - relay == pytest.approx(app._RELAY_DISCOUNT["g4f"])


def test_relay_hop_cap_and_discount_constants_unchanged():
    assert app._TOOL_RELAY_MAX_HOPS == 3
    assert app._RELAY_DISCOUNT["g4f"] == 4.0


# --------------------------------------------------------------------------- #
# 5. Agentic score rises by exactly the removed penalty
# --------------------------------------------------------------------------- #

def test_agentic_score_rises_by_the_penalty_delta(fleet, monkeypatch):
    _set_flag(monkeypatch, True)
    on = tool("g4f", RELAY_OPUS)
    _set_flag(monkeypatch, False)
    off = tool("g4f", RELAY_OPUS)
    assert on - off == pytest.approx(OLD_PENALTY["g4f"])   # 29 points back


# --------------------------------------------------------------------------- #
# 6. A strong first-party model still opens the tool turn; relays never lead
# --------------------------------------------------------------------------- #

def test_glm53_opens_the_tool_turn_and_relays_do_not_lead(fleet, monkeypatch):
    _set_flag(monkeypatch, True)     # relays un-penalised: the hard case
    pool = [(tool(p, m), p, m) for p, m in SCORE_FLEET]
    best = max(pool, key=lambda e: e[0])
    assert (best[1], best[2]) == ("nvidia", GLM53)
    lead = app._may_lead_pool(pool)
    assert any(c[2] == GLM53 for c in lead)
    assert not any(c[1] == "g4f" for c in lead)   # no relay in the lead pool


def test_chat_opener_is_unchanged_by_the_flag(fleet, monkeypatch):
    # Chat (tool-free) ordering is _benchmark_score, which the fix never touches.
    _set_flag(monkeypatch, True)
    on = max(SCORE_FLEET, key=lambda pm: gen(*pm))
    _set_flag(monkeypatch, False)
    off = max(SCORE_FLEET, key=lambda pm: gen(*pm))
    assert on == off


# --------------------------------------------------------------------------- #
# 7. Throttle and measured failure still keep a relay behind
# --------------------------------------------------------------------------- #

def test_a_throttled_relay_is_still_excluded(monkeypatch):
    # A throttle lives in quota.status (throttled/exhausted), a layer the scoring
    # penalty never reads -- so a long Retry-After keeps g4f out even at penalty 0.
    throttled = dict(FLEET_LIMITS)
    status = _fake_status(throttled)

    def with_throttle(pid):
        s = status(pid)
        if pid == "g4f":
            s["throttled"] = True
            s["exhausted"] = True
        return s

    monkeypatch.setattr(quota, "status", with_throttle)
    _set_flag(monkeypatch, True)
    assert app._sustain_penalty("g4f") == 0.0           # scoring says "fine"
    assert app._canary_provider_eligible("g4f") is False  # gating still says "out"


def test_measured_failure_still_demotes_a_relay(fleet, monkeypatch):
    _set_flag(monkeypatch, True)
    key = ("g4f", RELAY_OPUS)
    assert app._chain_reliability_band(*key) == 0        # healthy to start
    assert app._reliability_penalty(*key) == 0.0
    monkeypatch.setitem(app._outcomes, key,
                        {"ok": 0, "fail": 8, "last": time.time()})
    assert app._chain_reliability_band(*key) == 2        # measured to fail
    assert app._reliability_penalty(*key) > 0.0          # demoted on top of sustain 0


# --------------------------------------------------------------------------- #
# Report: before/after numbers for the final summary (run with -s to see).
# --------------------------------------------------------------------------- #

def test_report_before_after(fleet, monkeypatch, capsys):
    rows = []
    for pid in ("g4f", "llm7", "navy", "nararouter", "openrouter", "groq", "cohere"):
        _set_flag(monkeypatch, False)
        before = app._sustain_penalty(pid)
        _set_flag(monkeypatch, True)
        after = app._sustain_penalty(pid)
        rows.append((pid, FLEET_LIMITS[pid], before, after))
    _set_flag(monkeypatch, True)
    glm_tool = tool("nvidia", GLM53)
    relay_tool_on = tool("g4f", RELAY_OPUS)
    _set_flag(monkeypatch, False)
    relay_tool_off = tool("g4f", RELAY_OPUS)
    _set_flag(monkeypatch, True)
    chat_opener = max(SCORE_FLEET, key=lambda pm: gen(*pm))
    with capsys.disabled():
        print(f"  chat opener (by _benchmark_score, unchanged by flag): "
              f"{chat_opener[0]}/{chat_opener[1]} = {gen(*chat_opener):.2f}")
        print("\n  provider      limit        penalty before -> after")
        for pid, lw, b, a in rows:
            print(f"  {pid:<12} {str(lw):<12} {b:>6.1f} -> {a:<6.1f}")
        print(f"  GLM5.3 tool score (opens tool turn): {glm_tool:.2f}")
        print(f"  g4f claude-opus-5.5 tool score: {relay_tool_off:.2f} (off) -> "
              f"{relay_tool_on:.2f} (on)  [still < GLM {glm_tool:.2f}]")
    # Sanity inside the test too.
    assert relay_tool_on > relay_tool_off
    assert glm_tool > relay_tool_on
