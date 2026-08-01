"""Heavy asks in the dashboard chat must reach the strong models.

Reported: "why doesn't it use the best models depending on the heaviness of the
task, like CLI mode does?" Measured on 2026-08-01 before the fix, in the
dashboard chat, with the user's own prompt:

    "crerat ebst store website web deisng ... for restaurant in fez in morocco"
        -> classified SIMPLE -> allam-2-7b            (a 7B model, for a website)
    "fix this bug in my code: the login returns 401 after refresh"
        -> classified SIMPLE -> allam-2-7b

Three separate causes, one per section below.
"""
import app


def _d(text):
    return app._classify_difficulty([{"role": "user", "content": text}])


# --------------------------------------------------------------------------- #
# 1. The classifier only knew verbs, and the verb is what gets misspelled
# --------------------------------------------------------------------------- #

def test_the_reported_prompt_is_no_longer_simple():
    """The verb ("crerat") is misspelled, so a verb-only hint list matched
    nothing. What the user asked FOR -- a store website -- was spelled fine."""
    assert _d("crerat ebst store website web deisng you can do pleae "
              "for restaurant in fez in morocco") == "hard"


def test_naming_a_deliverable_is_enough_on_its_own():
    for ask in ("make me a portfolio site",
                "build me a full ecommerce site with cart and checkout",
                "a landing page for my shop"):
        assert _d(ask) == "hard", ask


def test_a_bare_deliverable_with_no_other_signal_is_medium_which_is_fine():
    """"i want a dashboard" carries one signal and lands on medium, not hard --
    and that costs nothing: medium takes the same strongest-model path as hard
    (BEST-EXCEPT-TRIVIAL). Only `simple` routes to a cheap model, which is what
    this whole fix is about keeping it out of."""
    assert _d("i want a dashboard") == "medium"
    src = open(app.__file__, encoding="utf-8", errors="replace").read()
    assert 'if difficulty in ("hard", "medium"):' in src, (
        "medium must not fall through to the cheap floor path")


def test_casual_debugging_is_not_trivial():
    """The list had "debug" but not "bug", and nothing for "it is broken"."""
    for ask in ("fix this bug in my code: the login returns 401 after refresh",
                "my api is not working after deploy",
                "the build crashes with an exception"):
        assert _d(ask) != "simple", ask


def test_genuinely_trivial_asks_stay_cheap():
    """The point of the tier is to keep strong quota for real work. If
    everything becomes hard, that saving is gone."""
    for ask in ("hi", "thanks", "what is the capital of morocco",
                "translate this to french: good morning",
                "summarize this article for me"):
        assert _d(ask) == "simple", ask


def test_artifact_words_are_matched_on_word_boundaries():
    """"app" is inside "happen", "site" inside "opposite", "game" inside
    "gamely" -- substring matching would make ordinary sentences heavy."""
    for ask in ("what happened yesterday in the news",
                "is it opposite of what i said",
                "he played gamely despite the injury"):
        assert _d(ask) == "simple", ask


# --------------------------------------------------------------------------- #
# 2. A misclassified ask could still land on a 7B
# --------------------------------------------------------------------------- #

def test_the_simple_tier_floor_excludes_toy_models():
    """A free-text chat box guarantees the classifier is sometimes wrong. The
    floor decides how bad that is: at 20 the worst case was a 7B answering a
    request to build a website."""
    assert app._DIFFICULTY_FLOOR["simple"] >= 45
    assert app._DIFFICULTY_FLOOR["simple"] < app._DIFFICULTY_FLOOR["medium"], (
        "the tiers must still be ordered -- simple is a saving, not a synonym")


# --------------------------------------------------------------------------- #
# 3. Chat filtered out the strong-but-slow models; CLI turns never did
# --------------------------------------------------------------------------- #

def test_hard_chat_turns_see_the_same_pool_a_cli_turn_sees(monkeypatch):
    """The fast-only prefilter removed strong models (hy3, gpt-oss-120b score as
    "slow") from chat entirely -- the other half of "why not like CLI mode".
    Simple and medium keep it: there the strength gap is small and latency is
    what you actually feel."""
    strong_slow = (140.0, "p", "strong-but-slow")
    quick_weak = (60.0, "p", "quick-but-weak")

    monkeypatch.setattr(app, "_available_providers", lambda: ["p"])
    monkeypatch.setattr(app, "_provider_capable", lambda p, est: True)
    monkeypatch.setattr(app, "_auto_models", lambda p: ["strong-but-slow", "quick-but-weak"])
    monkeypatch.setattr(app, "_benchmark_score",
                        lambda p, m: strong_slow[0] if m == strong_slow[2] else quick_weak[0])
    monkeypatch.setattr(app, "_is_fast", lambda p, m: m == quick_weak[2])
    monkeypatch.setattr(app, "_context_ok", lambda p, m, est: True)
    monkeypatch.setattr(app, "_is_low_quality", lambda m: False)
    monkeypatch.setattr(app.prov, "is_model_allowed", lambda m: True)
    monkeypatch.setattr(app, "_is_model_dead", lambda p, m: False)
    monkeypatch.setattr(app.quota, "is_model_throttled", lambda p, m: False)
    monkeypatch.setattr(app.quota, "model_status", lambda p, m: {"exhausted": False})
    monkeypatch.setattr(app.config, "get_flag", lambda k, d=None: True)

    msgs = [{"role": "user", "content": "build me a full ecommerce site with checkout"}]
    _pid, model, difficulty = app._route_by_difficulty(msgs)
    assert difficulty == "hard"
    assert model == "strong-but-slow", "a hard chat turn still cannot see slow strong models"

    msgs = [{"role": "user", "content": "hey"}]
    _pid, model, difficulty = app._route_by_difficulty(msgs)
    assert difficulty == "simple"


def test_always_best_is_the_default_when_the_flag_was_never_set():
    """It sat False in a real config with no UI that could have set it, and the
    only symptom was heavy turns landing on mid models. Whatever else changes,
    the DEFAULT has to be best-first."""
    src = open(app.__file__, encoding="utf-8", errors="replace").read()
    assert 'config.get_flag("route_always_best", True)' in src


def test_spreading_mode_announces_itself_at_startup():
    """Invisible was the real problem: answering "why not the good models"
    needed a routing trace. If it is off, the banner says so."""
    src = open(app.__file__, encoding="utf-8", errors="replace").read()
    assert "SPREADING across the top band" in src
