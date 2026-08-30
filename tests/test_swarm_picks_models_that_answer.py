"""A swarm slot is not a fallback hop -- a member that won't answer is waste.

MEASURED 2026-08-30 from the hub's own activity feed, across 24 swarm slots on
real turns:

    7 used   3 no-answer   nvidia/moonshotai/kimi-k3
    6 used   1 no-answer   glm/glm-4.6v-flash
    3 used   2 no-answer   nvidia/deepseek-ai/deepseek-v4-pro-0813
    2 used   2 no-answer   nvidia/deepseek-ai/deepseek-v4-flash-0731
    1 used   1 no-answer   g4f/AnyProvider:gemini-3.6-flash
    1 used   1 no-answer   g4f/GeminiPro:models/gemini-3.1-pro-preview
    1 used   1 no-answer   g4f/Ollama:kimi-k3
    1 used   0 no-answer   google/models/gemini-3.7-flash
    1 used   0 no-answer   google/models/gemini-3.6-flash

12 of 24 dead. Reported as "in swarm works that many have no answer so WTF all
should work man" -- three models' worth of quota buying one and a half models'
worth of opinions, and the failures were not spread evenly: g4f went 0-for-3
and nvidia 4-for-11, while google and glm answered 8 of 9.

Root cause: the swarm took its members straight off _build_chain, which orders
by RAW benchmark score -- correct for a fallback chain (try the best, drop to
the next when it fails) and wrong for a swarm, where every member runs at once.
Hops 2 and 3 of a fallback chain are, by construction, the entries the hub
already ranks lower and trusts less.

The hub was already measuring exactly the missing signal -- _reliability(), a
Laplace-smoothed delivery rate fed by _record_outcome on every hop -- and
_agentic_score already folds it in. The swarm just never asked.
"""
from unittest import mock

import app


def _rel(mapping, default=0.5):
    """Patch measured reliability. Anything unlisted stays neutral, which is
    what _reliability itself returns for a model with no history."""
    return mock.patch.object(app, "_reliability",
                             side_effect=lambda pid, model:
                             mapping.get((pid, model), default))


def _score(mapping, default=100.0):
    return mock.patch.object(app, "_benchmark_score",
                             side_effect=lambda pid, model:
                             mapping.get((pid, model), default))


def test_a_model_that_never_delivers_loses_its_slot():
    """The headline: g4f went 0-for-3 in the measured window."""
    cands = [("g4f", "AnyProvider:gemini-3.6-flash"),
             ("g4f", "Ollama:kimi-k3"),
             ("google", "models/gemini-3.7-flash"),
             ("glm", "glm-4.6v-flash")]
    with _rel({("g4f", "AnyProvider:gemini-3.6-flash"): 0.2,
               ("g4f", "Ollama:kimi-k3"): 0.2,
               ("google", "models/gemini-3.7-flash"): 0.75,
               ("glm", "glm-4.6v-flash"): 0.8}), _score({}):
        picks = app._swarm_rank(cands)
    assert ("g4f", "AnyProvider:gemini-3.6-flash") not in picks[:2]
    assert ("google", "models/gemini-3.7-flash") in picks
    assert ("glm", "glm-4.6v-flash") in picks


def test_a_model_with_no_history_is_never_judged():
    """_reliability returns a flat 0.5 for an unknown model on purpose. A new
    model must still get a chance to prove itself, exactly as it does in the
    ordinary chain -- demotion is for a REAL track record only."""
    cands = [("acme", "brand-new"), ("g4f", "known-bad")]
    with _rel({("g4f", "known-bad"): 0.1}), _score({}):
        picks = app._swarm_rank(cands)
    assert picks[0] == ("acme", "brand-new")


def test_the_swarm_never_shrinks_below_its_fanout():
    """Better a member that probably fails than a smaller swarm: the fan-out is
    the whole point, and a demoted model still might answer."""
    cands = [("g4f", "a"), ("g4f", "b"), ("g4f", "c"), ("g4f", "d")]
    with _rel({}, default=0.15), _score({}):
        picks = app._swarm_rank(cands)
    assert len(picks) == app._SWARM_TOOL_FANOUT


def test_strength_still_decides_between_two_healthy_models():
    """Reliability filters; it does not replace the ranking. A weak model that
    always answers must not beat a strong one that also answers."""
    cands = [("a", "weak"), ("b", "strong")]
    with _rel({("a", "weak"): 0.95, ("b", "strong"): 0.8}), \
            _score({("a", "weak"): 60.0, ("b", "strong"): 130.0}):
        picks = app._swarm_rank(cands)
    assert picks[0] == ("b", "strong")


def test_the_members_are_spread_across_providers():
    """One provider having a bad minute must not take the whole swarm down with
    it -- measured, nvidia contributed three separate failing entries and one
    turn drew two of its three members from it."""
    cands = [("nvidia", "m1"), ("nvidia", "m2"), ("nvidia", "m3"),
             ("google", "g1"), ("glm", "z1")]
    with _rel({}), _score({("nvidia", "m1"): 130.0, ("nvidia", "m2"): 129.0,
                           ("nvidia", "m3"): 128.0, ("google", "g1"): 120.0,
                           ("glm", "z1"): 119.0}):
        picks = app._swarm_rank(cands)
    assert len({pid for pid, _ in picks}) == 3, picks
    assert picks[0] == ("nvidia", "m1")          # the best still leads


def test_one_provider_is_still_allowed_when_it_is_all_there_is():
    cands = [("nvidia", "m1"), ("nvidia", "m2"), ("nvidia", "m3")]
    with _rel({}), _score({}):
        assert len(app._swarm_rank(cands)) == 3


def test_an_empty_candidate_list_is_safe():
    assert app._swarm_rank([]) == []


def test_fewer_candidates_than_the_fanout_are_all_used():
    with _rel({}), _score({}):
        assert len(app._swarm_rank([("a", "x"), ("b", "y")])) == 2


# --------------------------------------------------------------------------- #
# Wiring: the fan-out really goes through the ranking
# --------------------------------------------------------------------------- #

def test_the_fanout_ranks_its_candidates_instead_of_taking_chain_order():
    chain = [("g4f", "dead-1"), ("g4f", "dead-2"), ("g4f", "dead-3"),
             ("google", "alive-1"), ("glm", "alive-2")]
    seen = {}

    def _rank(cands):
        seen["cands"] = list(cands)
        return [("google", "alive-1")]

    with mock.patch.object(app, "_route_by_difficulty",
                           return_value=("g4f", "dead-1", "hard")), \
            mock.patch.object(app, "_build_chain", return_value=chain), \
            mock.patch.object(app, "_swarm_rank", side_effect=_rank), \
            mock.patch.object(app, "_dispatch_chat_with_deadline",
                              return_value=(None, None)), \
            app.app.test_request_context("/v1/chat/completions"):
        app._swarm_tool_result({"messages": [{"role": "user", "content": "hi"}],
                                "tools": [{"type": "function"}]})
    # It must see MORE than the fan-out to have anything to choose between --
    # taking only the first three off the chain is the bug being fixed.
    assert len(seen.get("cands") or []) > app._SWARM_TOOL_FANOUT, seen


def test_the_candidate_pool_is_deeper_than_the_fanout():
    assert app._SWARM_TOOL_CANDIDATES > app._SWARM_TOOL_FANOUT


# --------------------------------------------------------------------------- #
# The relay problem: one model, dozens of ids, a ledger that never generalises
# --------------------------------------------------------------------------- #

def test_a_fresh_relay_id_inherits_its_providers_record():
    """MEASURED after the ranking above shipped: a swarm turn STILL came back
    2-of-3 dead, and the dead member was 'g4f/RelayRouter:gemini-3.7-flash-free'
    -- an id the ledger had never seen, on a provider the ledger had already
    measured at 0 successes in 16 tries under four OTHER ids.

    g4f fronts the same model under a backend-prefixed id per backend, so every
    new listing is a new key and the hub relearns the same lesson forever. A
    provider with a real, measured record is a better prior for an unknown id
    on it than a flat neutral 0.5."""
    outcomes = {("g4f", "srv_a:gemini-3.6-flash"): {"ok": 0, "fail": 16},
                ("g4f", "srv_b:gemini-3.5-flash"): {"ok": 0, "fail": 16}}
    with mock.patch.object(app, "_provider_outcome_totals",
                           return_value=outcomes[("g4f", "srv_a:gemini-3.6-flash")]), \
            mock.patch.object(app, "_reliability", return_value=0.5):
        # never-seen id on that provider
        assert app._swarm_reliability("g4f", "RelayRouter:brand-new") < 0.4


def test_a_known_id_still_uses_its_own_record():
    """The provider prior only fills a GAP -- a model with its own history is
    judged on that history, good or bad."""
    with mock.patch.object(app, "_reliability", return_value=0.9), \
            mock.patch.object(app, "_provider_outcome_totals",
                              return_value={"ok": 0, "fail": 50}):
        assert app._swarm_reliability("g4f", "the-one-good-id") == 0.9


def test_a_provider_with_no_record_stays_neutral():
    with mock.patch.object(app, "_reliability", return_value=0.5), \
            mock.patch.object(app, "_provider_outcome_totals", return_value=None):
        assert app._swarm_reliability("acme", "anything") == 0.5


def test_a_healthy_provider_does_not_drag_an_unknown_model_down():
    with mock.patch.object(app, "_reliability", return_value=0.5), \
            mock.patch.object(app, "_provider_outcome_totals",
                              return_value={"ok": 40, "fail": 2}):
        assert app._swarm_reliability("google", "some-new-model") > 0.5


def test_the_provider_prior_needs_real_evidence():
    """Two failures on a provider is not a verdict on everything it hosts."""
    with mock.patch.object(app, "_reliability", return_value=0.5), \
            mock.patch.object(app, "_provider_outcome_totals",
                              return_value={"ok": 0, "fail": 2}):
        assert app._swarm_reliability("newprov", "x") == 0.5


def test_provider_totals_add_up_every_model_on_that_provider():
    with mock.patch.dict(app._outcomes,
                         {("g4f", "a"): {"ok": 0, "fail": 5, "last": app.time.time()},
                          ("g4f", "b"): {"ok": 1, "fail": 3, "last": app.time.time()},
                          ("google", "c"): {"ok": 9, "fail": 0, "last": app.time.time()}},
                         clear=True):
        assert app._provider_outcome_totals("g4f") == {"ok": 1, "fail": 8}
        assert app._provider_outcome_totals("google") == {"ok": 9, "fail": 0}
        assert app._provider_outcome_totals("nobody") is None


def test_the_ranking_uses_the_provider_aware_rate():
    """Wiring: _swarm_rank must consult _swarm_reliability, or none of the
    above changes which models actually get a slot."""
    cands = [("g4f", "fresh-relay-id"), ("google", "g1"), ("glm", "z1"), ("nvidia", "n1")]
    with mock.patch.object(app, "_swarm_reliability",
                           side_effect=lambda pid, m: 0.1 if pid == "g4f" else 0.8), \
            _score({}):
        picks = app._swarm_rank(cands)
    assert ("g4f", "fresh-relay-id") not in picks, picks
