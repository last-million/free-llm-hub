"""One model, every provider that hosts it — so the daily budgets ADD UP.

Session affinity pins a conversation to one model so a multi-turn build does not
get answered by a different model every turn (that produced incoherent output and
is why the pin exists). But it pinned the (provider, model) PAIR, so a long
conversation drained ONE provider while an identical copy of the same model sat
untouched on another: measured 18 of 23 turns on nvidia/z-ai/glm-5.2 while
g4f-nvidia listed the very same id. 94 models are currently served by more than
one available provider.

Hopping between providers of the SAME model costs nothing in coherence — same
weights, same behaviour — so the pin now covers the model and the provider
rotates underneath it.
"""
from unittest import mock

import app


def _pool(*pairs):
    """[(score, pid, model)] — the shape _route_by_difficulty works with."""
    return [(134.0, pid, model) for pid, model in pairs]


def test_a_lone_host_is_returned_unchanged():
    pool = _pool(("nvidia", "z-ai/glm-5.2"))
    assert app._pick_same_model_host(pool, ("nvidia", "z-ai/glm-5.2")) == \
        ("nvidia", "z-ai/glm-5.2")


def test_a_model_that_is_gone_returns_none():
    """Signal to the caller to re-pick and re-pin from scratch."""
    pool = _pool(("groq", "openai/gpt-oss-120b"))
    assert app._pick_same_model_host(pool, ("nvidia", "z-ai/glm-5.2")) is None


def test_both_hosts_of_the_same_model_get_used():
    """The headline behaviour: alternate instead of always picking one."""
    pool = _pool(("nvidia", "z-ai/glm-5.2"), ("g4f-nvidia", "z-ai/glm-5.2"))
    with mock.patch.object(app, "_quota_headroom", return_value=1.0):
        seen = {app._pick_same_model_host(pool, ("nvidia", "z-ai/glm-5.2"))[0]
                for _ in range(200)}
    assert seen == {"nvidia", "g4f-nvidia"}, seen


def test_the_model_never_changes():
    """Rotation is between HOSTS only — a different model would reintroduce the
    incoherence the pin exists to prevent."""
    pool = _pool(("nvidia", "z-ai/glm-5.2"), ("g4f-nvidia", "z-ai/glm-5.2"),
                 ("groq", "openai/gpt-oss-120b"))
    with mock.patch.object(app, "_quota_headroom", return_value=1.0):
        for _ in range(100):
            assert app._pick_same_model_host(
                pool, ("nvidia", "z-ai/glm-5.2"))[1] == "z-ai/glm-5.2"


def test_the_host_with_more_budget_left_wins():
    """Spreading is not blind: a nearly drained provider is skipped while a
    fresh copy of the same model is available."""
    pool = _pool(("nvidia", "z-ai/glm-5.2"), ("g4f-nvidia", "z-ai/glm-5.2"))
    with mock.patch.object(app, "_quota_headroom",
                           side_effect=lambda p: 0.05 if p == "nvidia" else 0.9):
        for _ in range(50):
            assert app._pick_same_model_host(
                pool, ("nvidia", "z-ai/glm-5.2"))[0] == "g4f-nvidia"


def test_suffixed_ids_count_as_the_same_model():
    """openrouter's ':free' suffix must not hide a shared model from spreading."""
    pool = _pool(("openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free"),
                 ("nvidia", "nvidia/nemotron-3-ultra-550b-a55b"))
    with mock.patch.object(app, "_quota_headroom", return_value=1.0):
        seen = {app._pick_same_model_host(
            pool, ("nvidia", "nvidia/nemotron-3-ultra-550b-a55b"))[0]
            for _ in range(200)}
    assert seen == {"openrouter", "nvidia"}, seen


def test_near_equal_headroom_still_alternates():
    """Tiny differences must not collapse the rotation back onto one provider."""
    pool = _pool(("a", "m"), ("b", "m"))
    with mock.patch.object(app, "_quota_headroom",
                           side_effect=lambda p: 1.0 if p == "a" else 0.99):
        seen = {app._pick_same_model_host(pool, ("a", "m"))[0] for _ in range(200)}
    assert seen == {"a", "b"}, seen
