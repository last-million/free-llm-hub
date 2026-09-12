r"""How many models answer one swarm turn is decided per turn, 2 to 5.

REQUESTED: "in swarm agents I want 4 different best models, or 5 maximum at the
same time, or 3, or just 2 -- depending on the orchestrator."

A fixed five was wrong in both directions. On a one-line question it spends five
models' quota to agree with itself. On a hard build where only three genuinely
different models are alive it asks for five and fills the last two with repeats
- which is the same waste the identity spread in _swarm_rank exists to stop.

Three inputs decide, and each is a real measurement rather than a guess:

  * how many DISTINCT models are actually available (the candidate list is
    already de-duplicated by identity, so four entries is four opinions);
  * how hard the turn really is - the extra opinions are what "hard" buys;
  * what the fleet can afford - a swarm that finishes off the last providers
    leaves the single-model path with nothing to fall back to.
"""
import pytest

import app as A


@pytest.fixture(autouse=True)
def _fleet(monkeypatch):
    """A healthy fleet and no explicit setting, so the orchestrator decides."""
    monkeypatch.setattr(A.config, "get_setting", lambda k, d=None: d)
    monkeypatch.setattr(A, "_available_providers",
                        lambda: ["a", "b", "c", "d", "e", "f"])
    monkeypatch.setattr(A, "_quota_headroom", lambda pid: 1.0)
    yield


def _cands(n):
    return [("p%d" % i, "model-%d" % i) for i in range(n)]


# --------------------------------------------------------------------------- #
# The difficulty of the turn
# --------------------------------------------------------------------------- #

def test_a_hard_turn_gets_the_full_width():
    assert A._swarm_fanout(_cands(8), "hard") == 5


def test_a_medium_turn_gets_three():
    assert A._swarm_fanout(_cands(8), "medium") == 3


def test_a_simple_turn_gets_two():
    """A second opinion on a one-line answer is quota spent agreeing with
    itself."""
    assert A._swarm_fanout(_cands(8), "simple") == 2


def test_an_unclassified_turn_is_treated_as_hard():
    """Not knowing is not a reason to buy fewer opinions."""
    assert A._swarm_fanout(_cands(8), None) == 5


# --------------------------------------------------------------------------- #
# What is actually available
# --------------------------------------------------------------------------- #

def test_it_never_asks_for_more_models_than_exist():
    assert A._swarm_fanout(_cands(3), "hard") == 3


def test_two_providers_serving_one_model_is_one_opinion():
    """The slot spent on the second copy is spent on nothing."""
    cands = [("groq", "openai/gpt-oss-120b"), ("cerebras", "gpt-oss-120b"),
             ("nvidia", "openai/gpt-oss-120b"), ("tokenrouter", "z-ai/glm-5.3-free")]
    assert A._swarm_fanout(cands, "hard") == 2


def test_it_never_drops_below_two():
    """One model is not a swarm, and a fan-out that quietly becomes a single
    call is worse than one that says it narrowed."""
    assert A._swarm_fanout(_cands(1), "hard") >= 2
    assert A._swarm_fanout([], "simple") >= 2


def test_no_candidate_list_still_answers():
    assert 2 <= A._swarm_fanout(None, "hard") <= 5


# --------------------------------------------------------------------------- #
# Spending is NOT decided here
# --------------------------------------------------------------------------- #

def test_the_sizer_does_not_second_guess_the_budget():
    """_swarm_rank already splits candidates on _SWARM_MIN_HEADROOM and puts a
    drained provider at the back, so a second budget test in the sizer protects
    nothing -- and it cost two rounds of real bugs to learn that: counting
    PROVIDERS punished a single strong provider serving four models, and
    _quota_headroom answering 0 for an id it has never heard of let any
    unrecognised provider silently halve the swarm."""
    src = open("app.py", encoding="utf-8").read()
    body = src[src.index("def _swarm_fanout("):]
    body = body[:body.index(chr(10) + "# How deep into the chain")]
    # The CALL, not the word: the comment explaining why it is gone names it.
    assert "_quota_headroom(" not in body
    assert "Size is a question about the WORK" in body


def test_the_ranker_is_still_the_one_that_protects_quota():
    src = open("app.py", encoding="utf-8").read()
    body = src[src.index("def _swarm_rank("):]
    body = body[:body.index(chr(10) + "def _swarm_tool_result(")]
    assert "_SWARM_MIN_HEADROOM" in body


def test_one_provider_serving_four_models_gets_four_slots():
    """The case the provider-count rule got wrong."""
    cands = [("solo", "m%d" % i) for i in range(4)]
    assert A._swarm_fanout(cands, "hard") == 4


# --------------------------------------------------------------------------- #
# An explicit setting still wins
# --------------------------------------------------------------------------- #

def test_a_number_someone_typed_is_obeyed(monkeypatch):
    monkeypatch.setattr(A.config, "get_setting",
                        lambda k, d=None: 7 if k == "swarm_fanout" else d)
    assert A._swarm_fanout(_cands(8), "simple") == 7


def test_an_explicit_setting_is_still_clamped(monkeypatch):
    monkeypatch.setattr(A.config, "get_setting",
                        lambda k, d=None: 99 if k == "swarm_fanout" else d)
    assert A._swarm_fanout(_cands(8), "hard") == A._SWARM_FANOUT_MAX


def test_rubbish_in_the_setting_falls_back_to_deciding(monkeypatch):
    monkeypatch.setattr(A.config, "get_setting",
                        lambda k, d=None: "five" if k == "swarm_fanout" else d)
    assert A._swarm_fanout(_cands(8), "hard") == 5


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #

def test_the_ranker_sizes_from_this_turn():
    src = open("app.py", encoding="utf-8").read()
    body = src[src.index("def _swarm_rank("):]
    body = body[:body.index("\ndef _swarm_tool_result(")]
    assert "_swarm_fanout(cands, difficulty)" in body


def test_the_tool_path_passes_the_real_difficulty():
    """The router forces "hard" so a tool turn gets a tool-capable model; how
    many opinions to buy is a different question and uses the real reading."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index("def _swarm_tool_result(")
    body = src[i:i + 6000]
    assert "_classify_difficulty(messages" in body
    assert "_swarm_rank(cands, _real)" in body


def test_the_bounds_are_what_was_asked_for():
    assert A._SWARM_FANOUT_MIN == 2
    assert A._SWARM_FANOUT_SOFT_MAX == 5


# --------------------------------------------------------------------------- #
# What a worker may run on
# --------------------------------------------------------------------------- #

def test_a_worker_is_never_planned_onto_the_cheap_tier(monkeypatch):
    """Offered the whole list, the planner filed "create hello.txt" under
    `fast`, and the small model invented the file it claimed to have written
    (MEASURED 2026-09-12). A worker's output is files on disk."""
    monkeypatch.setattr(A, "_mode_keys", lambda: ("coding", "reasoning", "fast", "vision"))
    assert A._worker_mode_keys() == ("coding", "reasoning", "vision")


def test_every_swarm_start_offers_the_worker_list():
    src = open("app.py", encoding="utf-8").read()
    assert "modes=_mode_keys()" not in src
    assert src.count("modes=_worker_mode_keys()") == 3
