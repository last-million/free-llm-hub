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


def test_the_same_model_under_different_namespaces_is_one_identity():
    """MEASURED 2026-08-29: hosts disagree about the NAMESPACE, not the model.
    gpt-oss-120b ships as 'openai/gpt-oss-120b' (groq, nvidia), bare
    'gpt-oss-120b' (cerebras, sambanova) and '@cf/openai/gpt-oss-120b'
    (cloudflare). Comparing the whole string made those three unrelated models,
    so none of the spreading in this file ever fired across them."""
    ident = app._normalize_model_identity
    assert ident("openai/gpt-oss-120b") == ident("gpt-oss-120b")
    assert ident("@cf/openai/gpt-oss-120b") == ident("gpt-oss-120b")
    assert ident("nvidia/nemotron-3-ultra-550b-a55b:free") == \
        ident("nvidia/nemotron-3-ultra-550b-a55b")
    # Genuinely different models must still compare different.
    assert ident("openai/gpt-oss-120b") != ident("openai/gpt-oss-20b")
    assert ident("google/gemma-4-31b-it") != ident("google/gemma-4-26b-it")


def test_hosts_that_spell_a_model_differently_still_share_the_load():
    """The bug this fixes: tencent/hy3 took 80 requests on openrouter (a 50/day
    cap it blew straight past) while an identical copy sat on kilocode."""
    pool = _pool(("groq", "openai/gpt-oss-120b"),
                 ("cerebras", "gpt-oss-120b"),
                 ("cloudflare", "@cf/openai/gpt-oss-120b"))
    with mock.patch.object(app, "_quota_headroom", return_value=1.0):
        seen = {app._pick_same_model_host(pool, ("groq", "openai/gpt-oss-120b"))[0]
                for _ in range(300)}
    assert seen == {"groq", "cerebras", "cloudflare"}, seen


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


def test_a_relayed_host_does_not_take_an_equal_share_of_a_real_one():
    """MEASURED 2026-08-30: a creation ask sent 24 of 30 turns to g4f relays of
    kimi-k3 (130.8) rather than nvidia's first-party copy (134.8) -- purely
    because g4f lists that model four times and nvidia once, and the host choice
    looked only at quota headroom. Headroom is 1.0 for every provider with an
    unknown limit, so nearly everything tied and the winner was decided by how
    many times a provider happened to list the id. That silently defeated the
    relay discount."""
    pool = _pool(("nvidia", "moonshotai/kimi-k3"),
                 ("g4f", "srv_a:moonshotai/kimi-k3"),
                 ("g4f", "srv_b:moonshotai/kimi-k3"),
                 ("g4f", "srv_c:moonshotai/kimi-k3"))
    with mock.patch.object(app, "_quota_headroom", return_value=1.0), \
            mock.patch.object(app, "_benchmark_score",
                              side_effect=lambda p, m: 130.8 if p == "g4f" else 134.8):
        seen = {app._pick_same_model_host(pool, ("nvidia", "moonshotai/kimi-k3"))[0]
                for _ in range(200)}
    assert seen == {"nvidia"}, seen


def test_first_party_hosts_a_point_apart_still_share():
    """The band is sized ABOVE the provider bias spread (0-2) on purpose: this
    is the load-sharing that stopped one provider's daily cap being drained
    while an identical copy sat idle, and it must survive."""
    pool = _pool(("cerebras", "gpt-oss-120b"),
                 ("groq", "openai/gpt-oss-120b"),
                 ("nvidia", "openai/gpt-oss-120b"))
    scores = {"cerebras": 100.0, "groq": 99.8, "nvidia": 98.2}   # bias-sized gaps
    with mock.patch.object(app, "_quota_headroom", return_value=1.0), \
            mock.patch.object(app, "_benchmark_score",
                              side_effect=lambda p, m: scores[p]):
        seen = {app._pick_same_model_host(pool, ("cerebras", "gpt-oss-120b"))[0]
                for _ in range(300)}
    assert seen == {"cerebras", "groq", "nvidia"}, seen
