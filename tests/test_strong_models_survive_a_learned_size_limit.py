"""The router's evidence must not punish the models it has evidence about.

REPORTED 2026-09-05: "why i see he use laguna WTF hhh why he dont use better
models ... this model i think is bad man". poolside/laguna-s-2.1 scores 10, the
joint-lowest thing in the catalog, and it was leading agentic builds.

The route is gated by `_context_ok`, which is LEARNED from real 413s. Only a
model the hub has actually USED can ever acquire a limit -- and the strong models
are the ones it uses. So on a big turn `_context_ok` drops exactly them, and what
survives is the never-tried weak tail.

MEASURED against the live fleet at est=60000:

    clean state                             70 candidates, 43 clear the 90 floor
    after the strong ones learned their 413 27 candidates,  0 clear the floor

`agentic = [>= floor] or pool` then failed open onto that tail, and a score-10
model led the build.

A learned limit is not a refusal. _upstream_chat compacts to each model's own
window before sending (_compact_to_budget), so it is a TRIM -- and a strong model
on a trimmed context beats a weak one on the whole thing. The tier is re-admitted
instead of collapsing, and ONLY when nothing that fits clears the floor, so an
ordinary request never trades a model that fits for one that must be compacted.
"""
import pytest

import app as A


PIDS = ["pa", "pb"]

# "-strong" clears every floor, "-weak" clears none (laguna's real score is 10).
# The two strong ones differ so an assertion can tell WHICH one was chosen --
# equal scores made the pick a coin flip and the test passed by luck.
MODELS = {p: [p + "-strong", p + "-weak"] for p in PIDS}
SCORES = {"pa-strong": 134.0, "pb-strong": 120.0, "pa-weak": 10.0, "pb-weak": 10.0}

BIG = 60000
SMALL = 50
MSGS = [{"role": "user", "content": "add a dark mode toggle to the header"}]


@pytest.fixture(autouse=True)
def world(monkeypatch):
    monkeypatch.setattr(A, "_available_providers", lambda *a, **k: list(PIDS))
    monkeypatch.setattr(A, "_prefetch_auto_models", lambda pids: dict(MODELS))
    monkeypatch.setattr(A, "_auto_models", lambda pid: list(MODELS[pid]))
    monkeypatch.setattr(A, "_provider_capable", lambda pid, est: True)
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: SCORES[m])
    # These tests are about which candidates reach the pick, not about the
    # softmax. A deterministic argmax keeps them from passing by coin flip.
    monkeypatch.setattr(A, "_weighted_pick", lambda pool, *a, **k: max(pool))
    monkeypatch.setattr(A, "_supports_tools", lambda pid, m: True)
    monkeypatch.setattr(A, "_is_tool_proven", lambda m: True)
    monkeypatch.setattr(A, "_session_pin_get", lambda key: None)
    monkeypatch.setattr(A, "_session_pin_set", lambda *a, **k: None)
    yield


def _learn_limits_on_the_strong_ones(monkeypatch):
    """Exactly what a run of real 413s produces: the models that got USED are
    the models with a limit."""
    monkeypatch.setattr(A, "_context_ok",
                        lambda pid, m, est: not (m.endswith("-strong") and est > 1000))


def _route(est, **kw):
    return A._route_by_difficulty(MSGS, None, est, quality_mode=True, **kw)


# --------------------------------------------------------------------------- #
# The reported failure
# --------------------------------------------------------------------------- #

def test_a_strong_model_leads_even_once_it_has_learned_a_limit(monkeypatch):
    _learn_limits_on_the_strong_ones(monkeypatch)
    pid, model, _d = _route(BIG, require_tools=True)
    assert model.endswith("-strong"), "led with %s/%s" % (pid, model)


def test_the_weak_tail_does_not_inherit_the_build(monkeypatch):
    """The whole complaint in one line: a score-10 model must not lead."""
    _learn_limits_on_the_strong_ones(monkeypatch)
    _pid, model, _d = _route(BIG, require_tools=True)
    assert not model.endswith("-weak")


def test_a_plain_chat_turn_is_covered_too(monkeypatch):
    """The tier floor collapses the same way without tools; the difficulty floor
    is what a chat turn has instead of _TOOLS_MIN_SCORE."""
    _learn_limits_on_the_strong_ones(monkeypatch)
    _pid, model, _d = _route(BIG, require_tools=False)
    assert model.endswith("-strong")


# --------------------------------------------------------------------------- #
# ...without changing an ordinary request
# --------------------------------------------------------------------------- #

def test_nothing_is_re_admitted_while_a_fitting_model_clears_the_floor(monkeypatch):
    """The guard is "nothing that FITS clears the floor". With no learned limits
    at all there is nothing to re-admit and the route is what it always was."""
    monkeypatch.setattr(A, "_context_ok", lambda pid, m, est: True)
    _pid, model, _d = _route(BIG, require_tools=True)
    assert model.endswith("-strong")


def test_a_model_that_fits_is_preferred_over_one_that_must_be_compacted(monkeypatch):
    """Only ONE provider learned a limit, so a fitting strong model still exists
    and must win -- fewer old turns is still a worse answer. Note pa-strong
    (134) outscores pb-strong (120): the model that FITS wins anyway, which is
    the whole point of gating the re-admission instead of always doing it."""
    monkeypatch.setattr(A, "_context_ok",
                        lambda pid, m, est: not (pid == "pa" and m.endswith("-strong")
                                                 and est > 1000))
    pid, model, _d = _route(BIG, require_tools=True)
    assert (pid, model) == ("pb", "pb-strong")


def test_a_small_request_is_untouched(monkeypatch):
    _learn_limits_on_the_strong_ones(monkeypatch)
    _pid, model, _d = _route(SMALL, require_tools=True)
    assert model.endswith("-strong")


# --------------------------------------------------------------------------- #
# What may come back
# --------------------------------------------------------------------------- #

def test_the_strong_one_still_leads_when_the_whole_fleet_is_oversized(monkeypatch):
    """Re-admission does not need its own floor: everything excluded for size
    comes back and the tier filter downstream picks among them as it always
    did."""
    monkeypatch.setattr(A, "_context_ok", lambda pid, m, est: est <= 1000)
    seen = set()

    def spy(pool, *a, **k):
        seen.update(m for _s, _p, m in pool)
        return max(pool)

    monkeypatch.setattr(A, "_weighted_pick", spy)
    _pid, model, _d = _route(BIG, require_tools=True)
    assert model.endswith("-strong")
    assert seen and not any(m.endswith("-weak") for m in seen), seen


def test_an_all_weak_oversized_fleet_is_served_rather_than_refused(monkeypatch):
    """The case a floor on the re-admission itself got wrong. Every model is
    both weak and too big, so a floor would re-admit nothing, leave the
    candidate list empty and return no model -- refusing the turn outright when
    a compacted weak answer was available."""
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: 10.0)
    monkeypatch.setattr(A, "_context_ok", lambda pid, m, est: est <= 1000)
    monkeypatch.setattr(A, "_weighted_pick", lambda pool, *a, **k: max(pool))
    monkeypatch.setattr(A, "_sub_available_providers", lambda *a, **k: [])
    pid, model, _d = _route(BIG, require_tools=True)
    assert pid and model


def test_the_request_is_still_served_when_everything_is_weak(monkeypatch):
    """Fail-open survives: if NOTHING clears the floor, fitting or not, the weak
    pool is still better than no answer."""
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: 10.0)
    monkeypatch.setattr(A, "_context_ok", lambda pid, m, est: True)
    monkeypatch.setattr(A, "_weighted_pick", lambda pool, *a, **k: max(pool))
    pid, model, _d = _route(BIG, require_tools=True)
    assert pid and model


def test_no_candidates_at_all_still_returns_nothing(monkeypatch):
    """Re-admission must not manufacture a candidate out of an empty fleet."""
    monkeypatch.setattr(A, "_available_providers", lambda *a, **k: [])
    monkeypatch.setattr(A, "_prefetch_auto_models", lambda pids: {})
    monkeypatch.setattr(A, "_sub_available_providers", lambda *a, **k: [])
    pid, model, _d = _route(BIG, require_tools=True)
    assert pid is None and model is None


# --------------------------------------------------------------------------- #
# The same rule in the RETRY LIST behind the primary
#
# MEASURED 2026-09-05, a real opencode turn through the live hub:
#     tokenrouter/z-ai/glm-5.3-free ! timeout (200 but no content)
#     openrouter/poolside/laguna-s-2.1:free                    <- hop 2
# The primary was right. The chain behind it had collapsed to score-10 models
# because everything stronger had learned a 413 limit.
# --------------------------------------------------------------------------- #

def _chain(est, **kw):
    return A._build_chain("pa", "pa-strong", est, require_tools=True,
                          messages=MSGS, **kw)


def test_the_chain_does_not_collapse_to_the_weak_tail(monkeypatch):
    _learn_limits_on_the_strong_ones(monkeypatch)
    chain = _chain(BIG)
    assert chain, "no chain at all"
    assert any(m.endswith("-strong") for _p, m in chain), chain


def test_the_weak_tail_does_not_own_the_first_retry(monkeypatch):
    """Hop 1 is the primary; hop 2 is the one that answered as laguna."""
    _learn_limits_on_the_strong_ones(monkeypatch)
    chain = _chain(BIG)
    assert len(chain) > 1
    assert chain[1][1].endswith("-strong"), chain


def test_a_fitting_chain_is_untouched(monkeypatch):
    """Nothing learned a limit, so there is nothing to re-admit."""
    monkeypatch.setattr(A, "_context_ok", lambda pid, m, est: True)
    before = _chain(BIG)
    assert before and all(m.endswith(("-strong", "-weak")) for _p, m in before)
    assert before[0] == ("pa", "pa-strong")


def test_the_chain_keeps_a_fitting_strong_model_over_a_compacted_one(monkeypatch):
    """pa-strong learned a limit, pb-strong still fits and is a real ALTERNATIVE
    in the chain body, so the floor is cleared and nothing is re-admitted."""
    monkeypatch.setattr(A, "_context_ok",
                        lambda pid, m, est: not (pid == "pa" and m.endswith("-strong")
                                                 and est > 1000))
    chain = A._build_chain("pa", "pa-weak", BIG, require_tools=True, messages=MSGS)
    assert ("pb", "pb-strong") in chain
    assert ("pa", "pa-strong") not in chain, chain


def test_the_primary_does_not_count_towards_the_floor(monkeypatch):
    """The load-bearing subtlety, and exactly the reported turn: the primary WAS
    a strong model that fits -- tokenrouter/z-ai/glm-5.3-free -- and it timed
    out. A chain is a RETRY list, so what decides whether the tier has collapsed
    is whether a strong ALTERNATIVE exists, not whether the hop that already
    failed was strong. Counting the primary here would leave the retry list as
    the score-10 tail that was reported."""
    monkeypatch.setattr(A, "_context_ok",
                        lambda pid, m, est: not (m.endswith("-strong")
                                                 and pid == "pb" and est > 1000))
    # pa-strong is the primary and fits; pb-strong is the only other strong
    # model and has learned a limit.
    chain = A._build_chain("pa", "pa-strong", BIG, require_tools=True, messages=MSGS)
    assert ("pb", "pb-strong") in chain, chain
    assert chain[1][1].endswith("-strong"), chain


def test_re_admitted_models_are_ranked_not_appended(monkeypatch):
    """They go through the same sort as everything else -- this changes WHICH
    models the chain may use, never the order it prefers them in."""
    _learn_limits_on_the_strong_ones(monkeypatch)
    chain = _chain(BIG)
    strong_at = [i for i, (_p, m) in enumerate(chain) if m.endswith("-strong")]
    weak_at = [i for i, (_p, m) in enumerate(chain) if m.endswith("-weak")]
    assert strong_at and weak_at
    assert max(strong_at) < min(weak_at), chain
