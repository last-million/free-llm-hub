"""OWNER DIRECTIVE 2026-09-27: "Pixel Canary" ranks above Kimi K3 and GPT-6
Astra (the owner's benchmark reading; no public source claimed). No provider
lists it yet, so these tests use a fake catalog.

SUPERSEDED FOR KIMI K3 on 2026-09-30: the owner chose Kimi K3 "just above
GLM 5.3" as the top free model (138.1), so Kimi K3 now ranks ABOVE Pixel
Canary; Pixel Canary still sits above the whole GPT ladder.

Guarded here:
- every id shape (plain, vendor-prefixed, relay-prefixed, suffixed) outranks
  kimi-k3 and gpt-6-astra served by the same provider, and never reaches
  Claude's owner-set 138;
- ids that only contain "canary" (the hub's own answer-canary vocabulary,
  chrome-canary, nvidia's canary ASR models) are untouched;
- speed cuts are capped like every other family's.
"""
import re

import pytest

import app


@pytest.fixture(autouse=True)
def no_aa(monkeypatch):
    monkeypatch.setattr(app, "_aa_scores", {})


def s(model, pid="p"):
    return app._benchmark_score(pid, model)


PIXEL_IDS = [
    "pixel-canary", "pixel_canary", "pixelcanary", "Pixel Canary",
    "Pixel-Canary-2", "pixel-canary-1.5-pro", "pixel-canary-70b",
    "pixel-canary-preview-20261001",
    "somevendor/pixel-canary", "pixel-labs/Pixel-Canary-2.5",
    "openrouter/pixel-canary:free",
]
RELAY_IDS = ["srv_x:pixel-canary", "Airforce:pixel-canary",
             "pa:abc123:pixel_canary-2", "RelayRouter:somevendor/pixel-canary"]


@pytest.mark.parametrize("model", PIXEL_IDS)
def test_every_id_shape_outranks_kimi_k3_and_gpt6_astra(model):
    for pid in ("p", "nvidia", "openrouter"):
        pc = s(model, pid)
        assert pc < s("moonshotai/kimi-k3", pid), (pid, model)     # 2026-09-30
        assert pc > s("gpt-6-astra", pid), (pid, model)
        assert pc > s("openai/gpt-6.4-astra", pid), (pid, model)
        assert pc > s("gpt-7", pid), (pid, model)


@pytest.mark.parametrize("model", RELAY_IDS)
def test_relay_prefixed_ids_outrank_the_same_relay_kimi_and_gpt6(model):
    # The g4f relay discount applies to every relayed id alike; within the
    # relay the order holds.
    assert s(model, "g4f") < s("srv_x:kimi-k3", "g4f")              # 2026-09-30
    assert s(model, "g4f") > s("srv_x:gpt-6-astra", "g4f")
    assert s(model, "g4f") == pytest.approx(app._PREF_FLOORS[11] - app._RELAY_DISCOUNT["g4f"])


@pytest.mark.parametrize("model", PIXEL_IDS)
def test_never_reaches_claudes_owner_floor(model):
    assert s(model) == pytest.approx(app._PREF_FLOORS[11])
    assert s(model) < app._PREF_FLOORS[5] == 138
    assert s(model) < s("anthropic/claude-opus-5")


def test_floor_sits_above_the_whole_gpt_ladder():
    assert app._PREF_FLOORS[11] > app._PREF_FLOORS[5] - 0.5   # gpt cap 137.5
    assert app._PREF_FLOORS[11] < app._PREF_FLOORS[1]          # kimi-k3 138.1 (2026-09-30)
    assert app._PREF_FLOORS[11] <= 138


def test_fake_catalog_best_pick_is_pixel_canary(monkeypatch):
    catalog = {
        "nvidia": ["moonshotai/kimi-k3", "z-ai/glm-5.2", "qwen/qwen3.8-27b"],
        "puter": ["gpt-6-astra", "gpt-5.6-sol", "gemini-3.8-flash"],
        "p": ["somevendor/pixel-canary-2", "pixel-canary-flash", "canary-build"],
    }
    monkeypatch.setattr(app, "_available_providers", lambda: list(catalog))
    monkeypatch.setattr(app, "_auto_models", lambda pid: catalog[pid])
    # Kimi K3 leads since 2026-09-30; Pixel Canary is next, above every GPT.
    assert app._best_free_pair(working_only=False) == ("nvidia", "moonshotai/kimi-k3")
    ranked = sorted(((s(m, pid), m) for pid, ms in catalog.items() for m in ms),
                    reverse=True)
    assert [m for _, m in ranked[:3]] == ["moonshotai/kimi-k3", "somevendor/pixel-canary-2",
                                          "gpt-6-astra"]


def test_floor_is_a_preference_not_a_natural_score_in_the_spread_band():
    assert app._PREF_FLOORS[11] in app._PREF_FLOORS


# --------------------------------------------------------------------------- #
# Unrelated "canary" ids are unaffected
# --------------------------------------------------------------------------- #

UNRELATED = ["canary", "canary-build", "chrome-canary", "nvidia/canary-1b",
             "nvidia/canary-1b-flash", "canary-qwen-2.5b", "pixel-art-canary",
             "pixel-7-canary", "pixelated-canary", "canary-pixel",
             "pixel-canaryx", "xpixel-canary", "llama-3.3-70b-canary"]


@pytest.mark.parametrize("model", UNRELATED)
def test_unrelated_canary_ids_do_not_match(model):
    low = app._canon_model_id(model.lower())
    assert not app._PIXEL_CANARY_RE.search(low), model


@pytest.mark.parametrize("model", UNRELATED)
def test_unrelated_canary_ids_score_exactly_as_before(monkeypatch, model):
    now = s(model)
    monkeypatch.setattr(app, "_PIXEL_CANARY_RE", re.compile(r"(?!x)x"))
    assert s(model) == now, model
    assert now < app._PREF_FLOORS[1]


# --------------------------------------------------------------------------- #
# Speed variants keep their caps
# --------------------------------------------------------------------------- #

def test_flash_cut_gets_the_strong_speed_cap_like_kimi_k3_flash():
    flash = s("pixel-canary-flash")
    assert flash == pytest.approx(app._STRONG_SPEED_CAP)
    assert flash < s("pixel-canary") < s("kimi-k3")         # kimi-k3 on top since 2026-09-30
    assert flash >= app._TOOLS_MIN_SCORE          # still a usable fallback
    assert s("somevendor/Pixel-Canary-2-Flash") == pytest.approx(app._STRONG_SPEED_CAP)
    assert app._is_speed_variant("pixel-canary-flash")


@pytest.mark.parametrize("model", ["pixel-canary-mini", "pixel-canary-lite",
                                   "pixel-canary-nano", "pixel-canary-2-flash-lite",
                                   "pixel-canary-7b"])
def test_small_cuts_are_on_the_tiny_cap(model):
    assert s(model) <= 30, model


def test_small_suffix_does_not_keep_the_flagship_floor():
    assert s("pixel-canary-small") <= app._STRONG_SPEED_CAP
