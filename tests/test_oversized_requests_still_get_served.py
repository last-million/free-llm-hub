"""An enormous turn falls back to providers that can serve a trimmed one.

REPORTED 2026-09-05: "i see some models in /activity have error 503". The hub's
own log named the cause exactly:

    CHAT-503 stream=True tools=True est=111875
    errors=[nvidia: ConnectionError; sub-claude: HTTP 413; groq: HTTP 413]

opencode was sending ~112,000-token turns. The size filter asks "can this
provider swallow `est` tokens" and answers for the request AS SENT, which left a
chain of three hops -- nvidia twice and a local relay. All three failed, so the
turn died, while eighty-odd other models sat unused.

Those models were not incapable. _upstream_chat compacts to each model's own
window before sending (_compact_to_budget), so a provider that cannot take the
whole conversation can usually serve a trimmed one. Filtering on the raw size
hid every one of them.

They are APPENDED, never promoted: a model that gets the whole conversation
beats one that gets part of it, so these are reached only once every full-size
option is gone. Fewer old turns is a worse answer. No answer is not an answer.

The chain is built against a SYNTHETIC provider set, not the live config, so
these assertions mean the same thing on a machine with 41 keys and on one with
none. `big*` can take the whole request; `small*` cannot.
"""
import pytest

import app as A


BIG = ["big1", "big2"]
SMALL = ["small1", "small2", "small3"]

HUGE = 111875          # the est from the reported 503
ORDINARY = 50


# Each provider lists its models WORST-FIRST, the way a real catalog does --
# openrouter's opens with score-10 ids and carries glm-5.2 ten entries down. A
# tail that walks the list raw picks "-a"; one that ranks picks "-c".
SCORES = {"-a": 10.0, "-b": 30.0, "-c": 134.0}


@pytest.fixture(autouse=True)
def world(monkeypatch):
    monkeypatch.setattr(A, "_available_providers", lambda *a, **k: BIG + SMALL)
    # BIG lists two models, SMALL three -- so four full-size hops leave exactly
    # two of MAX_HOPS' six for the tail, and the ranking has something to rank.
    monkeypatch.setattr(A, "_auto_models",
                        lambda pid: [pid + s for s in
                                     (("-a", "-b") if pid in BIG
                                      else ("-a", "-b", "-c"))])
    monkeypatch.setattr(A, "_provider_capable",
                        lambda pid, est: pid in BIG or est <= 1000)
    monkeypatch.setattr(A, "_benchmark_score",
                        lambda pid, m: SCORES.get(m[-2:], 0.0))
    yield


def _chain(est):
    return A._build_chain("big1", "big1-a", est, require_tools=True,
                          messages=[{"role": "user", "content": "go"}])


def _capable(chain, est):
    return [A._provider_capable(p, est) for p, _m in chain]


# --------------------------------------------------------------------------- #
# The fallback exists
# --------------------------------------------------------------------------- #

def test_a_huge_request_reaches_past_the_full_size_providers():
    """Before the tail this chain was exactly the `big` hops, and all of them
    failing was the reported 503."""
    chain = _chain(HUGE)
    assert any(not c for c in _capable(chain, HUGE)), \
        "no compaction fallback was appended: %r" % (chain,)


def test_the_huge_chain_is_longer_than_the_full_size_part():
    chain = _chain(HUGE)
    full = [h for h in chain if A._provider_capable(h[0], HUGE)]
    assert len(chain) > len(full)


# --------------------------------------------------------------------------- #
# ...but never at the expense of a model that fits
# --------------------------------------------------------------------------- #

def test_full_size_providers_come_first_and_stay_first():
    """A model that gets the whole conversation beats one that gets part of it.
    Once the list turns False it must never go back to True."""
    caps = _capable(_chain(HUGE), HUGE)
    assert caps[0] is True
    assert caps == sorted(caps, key=lambda c: not c), caps


def test_every_full_size_provider_is_used_before_the_tail():
    chain = _chain(HUGE)
    used_big = {p for p, _m in chain if p in BIG}
    assert used_big == set(BIG)


def test_one_model_per_fallback_provider():
    """A fallback, not a fan-out: a provider that cannot take the request whole
    should not occupy several hops with the same limitation."""
    small = [p for p, _m in _chain(HUGE) if p in SMALL]
    assert len(small) == len(set(small)), small


def test_the_tail_respects_the_hop_cap():
    """MAX_HOPS is 6 and there are 4 full-size hops, so exactly two fallbacks
    get in -- the third is dropped rather than growing the chain."""
    chain = _chain(HUGE)
    assert len([p for p, _m in chain if p in SMALL]) == 2
    assert len(chain) == A.MAX_HOPS


# --------------------------------------------------------------------------- #
# An ordinary request is untouched
# --------------------------------------------------------------------------- #

def test_an_ordinary_request_is_unchanged():
    """Nothing was too small for it, so there is nothing to append and the
    interleaved ordering it always had must survive intact."""
    chain = _chain(ORDINARY)
    assert all(_capable(chain, ORDINARY))
    assert len(chain) > A.MAX_HOPS      # the ordinary chain is not hop-capped here


def test_the_ordinary_chain_still_interleaves_providers():
    """The tail must not have disturbed the round-robin above it."""
    first_four = [p for p, _m in _chain(ORDINARY)[:4]]
    assert len(set(first_four)) == 4, first_four


# --------------------------------------------------------------------------- #
# The tail is subject to every filter the main loop applies
# --------------------------------------------------------------------------- #

# Each one rules out ONE provider and asserts the tail still picks up the others,
# so a test cannot pass by the tail simply never running.

def test_a_blocked_model_is_not_reintroduced(monkeypatch):
    """The user switched it off; a fallback is not a licence to switch it on."""
    monkeypatch.setattr(A.prov, "is_model_allowed",
                        lambda m: not m.startswith("small1"))
    picked = [p for p, _m in _chain(HUGE) if p in SMALL]
    assert "small1" not in picked and picked


def test_a_dead_model_is_not_reintroduced(monkeypatch):
    monkeypatch.setattr(A, "_is_model_dead", lambda pid, m: pid == "small1")
    picked = [p for p, _m in _chain(HUGE) if p in SMALL]
    assert "small1" not in picked and picked


def test_a_throttled_model_is_not_reintroduced(monkeypatch):
    monkeypatch.setattr(A.quota, "is_model_throttled",
                        lambda pid, m: pid == "small1")
    picked = [p for p, _m in _chain(HUGE) if p in SMALL]
    assert "small1" not in picked and picked


def test_an_exhausted_model_is_not_reintroduced(monkeypatch):
    monkeypatch.setattr(A.quota, "model_status",
                        lambda pid, m: {"exhausted": pid == "small1"})
    picked = [p for p, _m in _chain(HUGE) if p in SMALL]
    assert "small1" not in picked and picked


def test_a_vetoed_model_is_not_reintroduced():
    """"retry with a different model" must not hand back the model just rejected
    through the fallback door."""
    vetoed = A._normalize_model_identity("small1-a")
    chain = A._build_chain("big1", "big1-a", HUGE, require_tools=True,
                           messages=[{"role": "user", "content": "go"}],
                           exclude_identities={vetoed})
    assert ("small1", "small1-a") not in chain


def test_nothing_is_duplicated_between_the_chain_and_its_tail():
    chain = _chain(HUGE)
    assert len(chain) == len(set(chain))


# --------------------------------------------------------------------------- #
# The tail takes each provider's BEST model, not its first
#
# REPORTED 2026-09-05: "why i see he use laguna WTF ... this model i think is bad
# man". poolside/laguna-s-2.1 scores 10. It was not chosen on merit -- the first
# version of this tail walked _auto_models in CATALOG order and took whatever
# came first, so it served the weakest model of every provider it reached while
# z-ai/glm-5.2 (134) sat ten entries further down the same list.
# --------------------------------------------------------------------------- #

def test_the_tail_picks_the_best_model_of_each_provider():
    picked = {p: m for p, m in _chain(HUGE) if p in SMALL}
    assert picked, "the tail did not fire"
    assert all(m.endswith("-c") for m in picked.values()), picked


def test_catalog_order_does_not_decide():
    """"-a" is first in every provider's list and worst in every provider's
    list. If it appears, the tail is reading position instead of quality."""
    assert not [m for p, m in _chain(HUGE) if p in SMALL and m.endswith("-a")]


def test_the_next_best_is_taken_when_the_best_is_unavailable():
    """Ranking must not become a second way to dead-end: knock out "-c" and the
    provider should fall to "-b", not to nothing and not back to "-a"."""
    vetoed = {A._normalize_model_identity(p + "-c") for p in SMALL}
    chain = A._build_chain("big1", "big1-a", HUGE, require_tools=True,
                           messages=[{"role": "user", "content": "go"}],
                           exclude_identities=vetoed)
    picked = {p: m for p, m in chain if p in SMALL}
    assert picked and all(m.endswith("-b") for m in picked.values()), picked


def test_a_tool_turn_only_falls_back_onto_a_tool_capable_model(monkeypatch):
    """A hop that cannot call a tool is a wasted hop on an agentic turn."""
    monkeypatch.setattr(A, "_supports_tools",
                        lambda pid, m: not m.endswith("-c"))
    picked = {p: m for p, m in _chain(HUGE) if p in SMALL}
    assert picked and all(m.endswith("-b") for m in picked.values()), picked


def test_a_provider_with_no_tool_capable_model_is_skipped_not_forced(monkeypatch):
    """Fail-open PER PROVIDER: small1 drops out, the others still serve."""
    monkeypatch.setattr(A, "_supports_tools",
                        lambda pid, m: pid != "small1")
    picked = [p for p, _m in _chain(HUGE) if p in SMALL]
    assert "small1" not in picked and picked


def test_tools_are_not_required_of_a_plain_turn(monkeypatch):
    """The filter is conditional -- a non-tool request must not lose the tail."""
    monkeypatch.setattr(A, "_supports_tools", lambda pid, m: False)
    chain = A._build_chain("big1", "big1-a", HUGE, require_tools=False,
                           messages=[{"role": "user", "content": "go"}])
    assert [p for p, _m in chain if p in SMALL]


def test_a_vision_turn_only_falls_back_onto_a_vision_model(monkeypatch):
    monkeypatch.setattr(A, "_is_vision_model", lambda pid, m: m.endswith("-b"))
    chain = A._build_chain("big1", "big1-a", HUGE, require_vision=True,
                           messages=[{"role": "user", "content": "go"}])
    picked = {p: m for p, m in chain if p in SMALL}
    assert picked and all(m.endswith("-b") for m in picked.values()), picked


def test_the_learned_context_limit_is_not_applied_to_the_tail(monkeypatch):
    """_context_ok is the learned "cannot hold est tokens" signal, which is true
    of EVERYTHING in this tail by construction -- that is what put it here.
    Applying it would filter the fallback down to nothing and restore the 503."""
    monkeypatch.setattr(A, "_context_ok", lambda pid, m, est: est <= 1000)
    assert [p for p, _m in _chain(HUGE) if p in SMALL]
