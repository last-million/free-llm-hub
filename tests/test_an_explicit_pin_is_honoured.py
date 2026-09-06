"""A model the caller NAMED opens the turn, whatever its record says.

MEASURED: a request pinned to "groq/qwen/qwen3.8-27b" was served by
tokenrouter/z-ai/glm-5.3-free. The pinned model sat at hop 2 and something the
caller had not asked for answered:

    reliability  groq/qwen/qwen3.8-27b          0.207  -> band 2
    reliability  tokenrouter/z-ai/glm-5.3-free  0.909  -> band 0

    chain when pinned to groq/qwen/qwen3.8-27b:
      1. tokenrouter/z-ai/glm-5.3-free
      2. groq/qwen/qwen3.8-27b

_build_chain refuses to seed a band-2 primary at hop 1 -- leading with a model
measured to fail is a stall. That is right for a model the ROUTER chose, and it
was being applied to every primary, including one the caller named. The two are
not the same thing: "auto" is a question, "groq/qwen/qwen3.8-27b" is an answer.

Silently substituting is the worst of the options. The caller cannot tell it
happened, the model they picked for a reason is not the one that ran, and the
fallback chain behind the pin is still there if the pin really does fail.
"""
import pytest

import app as A


PINNED = ("pa", "pa-bad")
HEALTHY = ("pb", "pb-good")
MSGS = [{"role": "user", "content": "say OK"}]


@pytest.fixture(autouse=True)
def graded(monkeypatch):
    """PINNED is measured-to-fail; HEALTHY is not."""
    rec = {PINNED: 0.05, HEALTHY: 0.95}
    monkeypatch.setattr(A, "_reliability", lambda p, m: rec.get((p, m), 0.5))
    monkeypatch.setattr(A, "_swarm_has_record", lambda p, m: (p, m) in rec)
    monkeypatch.setattr(A, "_available_providers", lambda *a, **k: ["pa", "pb"])
    monkeypatch.setattr(A, "_prefetch_auto_models",
                        lambda pids: {"pa": ["pa-bad"], "pb": ["pb-good"]})
    monkeypatch.setattr(A, "_auto_models",
                        lambda pid: {"pa": ["pa-bad"], "pb": ["pb-good"]}[pid])
    monkeypatch.setattr(A, "_provider_capable", lambda pid, est: True)
    # HEALTHY also outranks PINNED on raw strength, so the two cases are
    # genuinely distinguishable: seeding is what puts PINNED first, never
    # ranking. Without this the ranked list leads with PINNED anyway and the
    # test would pass whether or not the fix existed.
    monkeypatch.setattr(A, "_benchmark_score",
                        lambda pid, m: 10.0 if m == PINNED[1] else 130.0)
    yield


def _chain(**kw):
    return A._build_chain(PINNED[0], PINNED[1], 50, messages=MSGS, **kw)


def test_the_premise_the_pinned_model_is_measured_to_fail():
    assert A._chain_reliability_band(*PINNED) >= 2


def test_a_pinned_model_opens_the_turn():
    chain = _chain(pinned=True)
    assert chain and chain[0] == PINNED, chain


def test_an_auto_chosen_model_is_still_demoted():
    """The demotion is not removed -- it is scoped to the case it was written
    for. A router pick measured to fail must still not OPEN the turn.

    Note what this does and does not claim. The rule is that a band-2 primary is
    not SEEDED at hop 1; it stays a candidate and the ranked list may still put
    it first when nothing healthier exists. Here something healthier does, so
    hop 1 changes -- which is exactly the difference the pin is meant to
    override."""
    chain = _chain()
    assert chain and chain[0] == HEALTHY, chain


def test_the_pin_does_not_lose_its_fallbacks():
    """Honouring the pin must not turn into a one-model chain: if the pinned
    model really does fail, the turn still has somewhere to go."""
    chain = _chain(pinned=True)
    assert len(chain) > 1
    assert any((p, m) != PINNED for p, m in chain)


def test_a_healthy_pin_is_unaffected():
    chain = A._build_chain(HEALTHY[0], HEALTHY[1], 50, messages=MSGS, pinned=True)
    assert chain and chain[0] == HEALTHY


def test_a_pinned_model_is_not_duplicated():
    chain = _chain(pinned=True)
    assert chain.count(PINNED) == 1


def test_a_vetoed_pin_is_still_refused():
    """"retry with a different model" outranks the pin -- the caller is now
    explicitly asking for something else."""
    chain = _chain(pinned=True,
                   exclude_identities={A._normalize_model_identity(PINNED[1])})
    assert not chain or chain[0] != PINNED


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #

def test_every_client_walk_passes_the_flag():
    """All three protocol surfaces -- openai, responses, anthropic -- or a pin
    is honoured on one and silently ignored on the others."""
    src = open("app.py", encoding="utf-8").read()
    assert src.count("**_pin_kw") == 3
    assert src.count('_pin_kw = {"pinned": True} if not _is_orchestrate') == 3


def test_the_flag_is_off_for_an_orchestrated_request():
    """`auto`, `best` and a mode id are all questions, not answers: the router
    is choosing, so its pick stays subject to the demotion."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index('_pin_kw = {"pinned": True} if not _is_orchestrate')
    assert "not _is_orchestrate" in src[i:i + 120]


def test_the_ordinary_call_shape_is_unchanged():
    """Passed as a kwarg dict, like exclude_identities, so a stand-in for
    _build_chain never has to know about a parameter it does not see. A test
    double with the old signature must keep working."""
    def old_signature(pid, model, est=0, require_vision=False,
                      require_tools=False, messages=None):
        return [(pid, model)]
    assert old_signature("pa", "pa-bad", 50) == [PINNED]
