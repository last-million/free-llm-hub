"""Always route the hard/medium turn to the STRONGEST available model.

_spread_pick deliberately rotates across the top band so consecutive turns land
on different strong providers — more total capacity, slightly less quality per
turn. MEASURED 2026-07-31: glm-5.2 (score 135) sat in the pool while a 121 model
served the turn. The user asked repeatedly for the best model every time, so
always-best is the default and the spreading behaviour is behind a flag.
"""
import app
import config


def _pool():
    return [(101.0, "google", "gemini-3-flash"),
            (135.0, "g4f-nvidia", "z-ai/glm-5.2"),      # the best
            (121.0, "cloudflare", "kimi-k2.6")]


def _route(monkeypatch, pool, always_best):
    config.set_flag("route_always_best", always_best)
    monkeypatch.setattr(app, "_available_providers", lambda: ["g4f-nvidia"])
    monkeypatch.setattr(app, "_quota_headroom", lambda pid: 1.0)
    picks = set()
    for _ in range(8):
        picks.add(max(pool, key=lambda t: (t[0], 1.0))[1:] if always_best
                  else (app._spread_pick(pool) or (0, None, None))[1:])
    return picks


def test_always_best_pins_every_turn_to_the_top_model(monkeypatch):
    picks = _route(monkeypatch, _pool(), True)
    assert picks == {("g4f-nvidia", "z-ai/glm-5.2")}


def test_spreading_uses_more_than_one_model(monkeypatch):
    """The behaviour the flag restores — kept because it stretches quota."""
    picks = _route(monkeypatch, _pool(), False)
    assert len(picks) > 1


def test_the_flag_defaults_to_always_best():
    import inspect
    src = inspect.getsource(app._route_by_difficulty)
    assert 'config.get_flag("route_always_best", True)' in src


def test_hard_and_medium_share_the_strongest_branch():
    import inspect
    src = inspect.getsource(app._route_by_difficulty)
    assert 'if difficulty in ("hard", "medium"):' in src
    # simple must still take the cheap floor path, or trivial asks would burn
    # the scarce top-tier models.
    assert src.index('if difficulty in ("hard", "medium"):') < src.index("_DIFFICULTY_FLOOR[difficulty]")
