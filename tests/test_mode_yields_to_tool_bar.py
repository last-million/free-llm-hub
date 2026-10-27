"""A category mode must not put a model BELOW the tool-turn bar in front of a healthy
model at or above it from outside the category.

MEASURED 2026-10-27 on the owner's hub (OpenCode, model `context-max`, ~62K-token tool
turns): all 40 requests since the restart were served by inception/mercury-3 as their
ONLY hop (hub score 26, 39 ok / 0 fail). The `context` category (known window >= 400K)
held only that and zenmux's score-10 models as healthy members: dahl answered HTTP 429
to every call (0/27, 6/32), nvidia's GLM models were resting after repeated deadlines,
g4f was parked (4 of 4 keys rejected). Healthy strong models OUTSIDE the category
existed (groq/qwen3.8 answered a one-line probe in 1-2 s, nvidia minimax-m3.1-flash 99,
freemodel DeepSeek 130) but two things kept them behind Mercury:

  * the ROUTER: `_apply_mode` keeps the category, then `agentic = [score >= 90] or pool`
    falls open to whatever the category has left;
  * the CHAIN: `_mode_fallback_tail` (the mode's way out) is appended after EVERY in-mode
    hop, score-10 ones included, so a 26-point model answers at hop 4 and the tail is
    never reached.

Owner, same day: "the orchestrator should always be set automatically, best models
available -- of course in intelligence available". The rule, in both places: the mode
still owns the turn while it has a model that clears _TOOLS_MIN_SCORE; once it has none,
the healthy out-of-category models that do clear it go first and the weak category
members become the last resort (reordered, never dropped).
"""
import pytest

import app as A

SCORES = {
    "strong-ctx": 134.0,    # in the mode, strong (but may be failing)
    "weak-ctx": 26.0,       # in the mode, healthy, BELOW the tool bar (Mercury)
    "weak2-ctx": 10.0,      # in the mode, BELOW the bar (zenmux atria)
    "strong-out": 130.0,    # outside the mode, strong and healthy
    "mid-out": 50.0,        # outside the mode, below the bar
}
WORLD = {
    "pa": ["strong-ctx"],
    "pb": ["weak-ctx", "weak2-ctx"],
    "pc": ["strong-out"],
    "pd": ["mid-out"],
}
MSGS = [{"role": "user", "content": "fix the failing test in the scheduler and add a regression test"}]


def _in_mode(m):
    return m.endswith("-ctx")


@pytest.fixture
def fleet(monkeypatch):
    world = {p: list(ms) for p, ms in WORLD.items()}
    monkeypatch.setattr(A, "_available_providers", lambda *a, **k: list(world))
    monkeypatch.setattr(A, "_prefetch_auto_models", lambda pids: dict(world))
    monkeypatch.setattr(A, "_auto_models", lambda pid: list(world[pid]))
    monkeypatch.setattr(A, "_provider_capable", lambda pid, est: True)
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: SCORES.get(m, 10.0))
    monkeypatch.setattr(A, "_supports_tools", lambda pid, m: True)
    monkeypatch.setattr(A, "_session_pin_get", lambda key: None)
    monkeypatch.setattr(A, "_session_pin_set", lambda *a, **k: None)
    monkeypatch.setattr(A, "_weighted_pick", lambda pool, *a, **k: max(pool))
    monkeypatch.setattr(A, "_is_relay_pid", lambda pid: False)
    monkeypatch.setattr(A.model_categories, "matches", lambda key, p, m, i=None: _in_mode(m))
    monkeypatch.setattr(A, "_active_mode", lambda: "coding")
    monkeypatch.setattr(A, "_simple_turn", lambda: False)
    yield world


def _flag(monkeypatch, on):
    real = A.config.get_flag
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=None: on if k == "mode_yields_to_tool_bar" else real(k, d))


def _without(world, *models):
    for p in world:
        world[p] = [m for m in world[p] if m not in models]


def _route(tools=True):
    return A._route_by_difficulty(MSGS, None, 500, require_tools=tools)


def _chain(primary=("pa", "strong-ctx"), tools=True):
    return A._build_chain(primary[0], primary[1], 500, require_tools=tools, messages=MSGS)


def _pos(chain, model):
    return [m for _p, m in chain].index(model)


# --------------------------------------------------------------------------- #
# The router
# --------------------------------------------------------------------------- #

def test_a_mode_with_nothing_above_the_bar_yields_the_pick(fleet, monkeypatch):
    """The live case: the category holds only Mercury-class models."""
    _flag(monkeypatch, True)
    _without(fleet, "strong-ctx")          # the category's strong members are gone
    _pid, model, _d = _route()
    assert model == "strong-out", model


def test_with_the_flag_off_the_mode_keeps_the_weak_pick(fleet, monkeypatch):
    _flag(monkeypatch, False)
    _without(fleet, "strong-ctx")
    _pid, model, _d = _route()
    assert _in_mode(model), model


def test_the_mode_still_owns_the_turn_while_it_has_a_model_above_the_bar(fleet, monkeypatch):
    _flag(monkeypatch, True)
    _pid, model, _d = _route()
    assert model == "strong-ctx", model


def test_it_does_not_yield_to_a_model_that_is_also_below_the_bar(fleet, monkeypatch):
    """Nothing outside clears the bar either: the category keeps its pick."""
    _flag(monkeypatch, True)
    _without(fleet, "strong-ctx", "strong-out")
    _pid, model, _d = _route()
    assert _in_mode(model), model


def test_a_tool_free_turn_is_unchanged(fleet, monkeypatch):
    """The bar is a TOOL-turn rule (the same one `agentic` applies)."""
    _flag(monkeypatch, True)
    _without(fleet, "strong-ctx")
    _pid, model, _d = _route(tools=False)
    assert _in_mode(model), model


# --------------------------------------------------------------------------- #
# The fallback chain
# --------------------------------------------------------------------------- #

def test_the_chain_puts_healthy_outside_models_before_weak_category_members(fleet, monkeypatch):
    """The live failure in one assertion: Mercury sat at hop 4, the way out never ran."""
    _flag(monkeypatch, True)
    chain = _chain()
    assert chain[0] == ("pa", "strong-ctx")              # the primary always opens
    assert _pos(chain, "strong-out") < _pos(chain, "weak-ctx"), chain
    assert _pos(chain, "strong-out") < _pos(chain, "weak2-ctx"), chain


def test_the_weak_members_are_kept_as_the_last_resort_never_dropped(fleet, monkeypatch):
    _flag(monkeypatch, True)
    chain = _chain()
    assert ("pb", "weak-ctx") in chain and ("pb", "weak2-ctx") in chain, chain
    assert len(chain) == len(set(chain)), chain


def test_with_the_flag_off_the_chain_is_the_old_one(fleet, monkeypatch):
    _flag(monkeypatch, False)
    chain = _chain()
    assert _pos(chain, "weak-ctx") < _pos(chain, "strong-out"), chain


def test_the_primary_is_never_moved_even_when_it_is_weak(fleet, monkeypatch):
    """A pinned or router-chosen primary opens the turn whatever its score."""
    _flag(monkeypatch, True)
    chain = _chain(primary=("pb", "weak-ctx"))
    assert chain[0] == ("pb", "weak-ctx"), chain


def test_no_strong_outside_model_means_no_reorder(fleet, monkeypatch):
    """Only a below-bar model outside the category: nothing to yield to."""
    _flag(monkeypatch, True)
    _without(fleet, "strong-out")
    chain = _chain()
    assert _pos(chain, "weak-ctx") < _pos(chain, "mid-out"), chain


def test_a_tool_free_chain_is_unchanged(fleet, monkeypatch):
    _flag(monkeypatch, True)
    chain = _chain(tools=False)
    assert _pos(chain, "weak-ctx") < _pos(chain, "strong-out"), chain


def test_a_trivial_turn_keeps_its_fast_first_order(fleet, monkeypatch):
    """Easy turns go to quick models on purpose (the quick-turn design)."""
    _flag(monkeypatch, True)
    monkeypatch.setattr(A, "_simple_turn", lambda: True)
    chain = _chain()
    assert _pos(chain, "weak-ctx") < _pos(chain, "strong-out"), chain


def test_an_unmoded_chain_is_untouched(fleet, monkeypatch):
    _flag(monkeypatch, True)
    monkeypatch.setattr(A, "_active_mode", lambda: A.MODE_ALL)
    chain = _chain()
    assert len(chain) == len(set(chain)), chain
    assert _pos(chain, "strong-out") < _pos(chain, "weak-ctx"), chain   # plain score order
