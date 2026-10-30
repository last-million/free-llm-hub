"""Prose swarm review fixes (2026-10-30).

  1. A NEGATED acceptance criterion ('Must not include "lorem ipsum"') forbids
     its quoted text: the free checks used to read it as REQUIRED, rejecting
     clean output (and telling the retry to ADD the text) and "proving" output
     that contained it. A negation they cannot read with certainty is left
     undecided -- never guessed.
  2. `_clean_phases` translates `needs` from the planner's numbering to the
     kept phases', so a dropped entry no longer erases or re-points one, and
     MAX_PHASES counts usable phases only.
  3. `phases` written as an object ({"1": {...}}) no longer crashes swarm.run.
  4. The wider/deeper search (and the retry fallback) never replaces a real
     attempt with a degenerate one.

No network: every model call is a stub.
"""
import json
import re
import threading

import pytest

import swarm


# --------------------------------------------------------------------------- #
# Stubs (the same shape as tests/test_pipeline_verify_and_search.py)
# --------------------------------------------------------------------------- #

class Pick:
    """A scripted Thompson draw: `choice` always wins."""

    def __init__(self, choice):
        self.seq = [0.9, 0.1] if choice == "wider" else [0.1, 0.9]
        self.i = 0

    def betavariate(self, a, b):
        v = self.seq[self.i % 2]
        self.i += 1
        return v


_PHASE_RE = re.compile(r"YOUR PHASE \(\d+ of \d+\): (.+)")


def _stage(messages):
    sys_, user = messages[0]["content"], messages[-1]["content"]
    m = _PHASE_RE.search(user)
    if m:
        return "phase", m.group(1).strip()
    if sys_ == swarm._INSTRUCT_SYSTEM:
        return "instruct", ""
    if sys_ == swarm._APPLY_SYSTEM:
        return "revision", ""
    if "WHAT THE TEAM PRODUCED" in user:
        return "supervise", ""
    if "PHASE OUTPUTS" in user:
        return "synth", ""
    if user.startswith("BRIEF\n"):
        return "review", ""
    if "REQUIRED PARTS" in user:
        return "parts", ""
    return "plan", ""


SHIP = json.dumps({"verdict": "ship", "problems": []})
NO_GAPS = json.dumps({"missing": []})
ASK = [{"role": "user", "content": "write the landing page copy with usage notes"}]
GOOD_B = "The usage notes explain how to call the parser and what it returns."


def make_dispatch(plan, alpha):
    """Phase "Alpha" answers from `alpha` in order (the last one repeats);
    every other phase answers GOOD_B."""
    calls = []
    lock = threading.Lock()
    served = {"n": 0}
    alpha = list(alpha)

    def dispatch(messages, max_tokens, exclude_pids=()):
        stage, title = _stage(messages)
        with lock:
            calls.append({"stage": stage, "title": title,
                          "user": messages[-1]["content"]})
        if stage == "plan":
            return plan, "planner/glm-5"
        if stage == "phase":
            if title == "Alpha":
                with lock:
                    k = served["n"]
                    served["n"] += 1
                return alpha[min(k, len(alpha) - 1)], "p%d/kimi-k3" % (k + 1)
            return GOOD_B, "p2/qwen3"
        if stage == "supervise":
            return NO_GAPS, "sup/qwen3"
        if stage == "review":
            return SHIP, "rev/llama-4"
        if stage == "synth":
            return "FINAL", "syn/deepseek-v4"
        return "", None
    dispatch.calls = calls
    dispatch.of = lambda stage, title=None: [c for c in calls if c["stage"] == stage
                                             and (title is None or c["title"] == title)]
    return dispatch


def _plan(acceptance):
    return json.dumps({"goal": "Write the page", "phases": [
        {"title": "Alpha", "task": "write the hero copy", "acceptance": acceptance},
        {"title": "Beta", "task": "write the usage notes"}]})


class _JunkCheck:
    """answer_check stand-in: any text containing JUNK is degenerate."""

    @staticmethod
    def inspect(text, prompt_text=None, **_kw):
        if "JUNK" in (text or ""):
            return {"ok": False, "reasons": ["loop"]}
        return {"ok": True, "reasons": []}


def _alpha_output(out):
    return [p for p in out["phases"] if p["title"] == "Alpha"][0]["output"]


# --------------------------------------------------------------------------- #
# 1. Negated acceptance criteria
# --------------------------------------------------------------------------- #

CLEAN = "Ship faster with Acme. Every plan includes support."
LOREM = "Lorem ipsum dolor sit amet."


@pytest.mark.parametrize("crit,bad", [
    ('Must not include "lorem ipsum"', LOREM),
    ('Does not contain "TODO"', "Ship it. TODO: add pricing."),
    ('Never use "eval"', "Then call eval(x) on the input."),
    ("Doesn't mention \"Acme Corp\"", "Brought to you by Acme Corp."),
    ('Must not include "lorem" or "ipsum"', "A bit of ipsum here."),
    ('Contains no "TODO"', "TODO: write the footer."),
    ('Includes neither "lorem" nor "ipsum"', "Some lorem text."),
])
def test_a_negated_criterion_forbids_its_text(crit, bad):
    ph = {"acceptance": [crit]}
    assert swarm._mechanical_problems(ph, CLEAN) == []
    assert swarm._criterion_proven(crit, CLEAN) is True
    assert swarm._proven(ph, CLEAN) is True
    probs = swarm._mechanical_problems(ph, bad)
    assert len(probs) == 1 and probs[0].startswith("contains forbidden text")
    assert "missing required text" not in probs[0]
    assert swarm._criterion_proven(crit, bad) is False
    assert swarm._proven(ph, bad) is False


def test_a_forbidden_word_inside_another_word_is_not_a_hit():
    """"eval" is not used in "evaluate": a false hit would make the retry
    rewrite correct text."""
    ph = {"acceptance": ['Never use "eval"']}
    text = "We evaluate every input before saving it."
    assert swarm._mechanical_problems(ph, text) == []
    assert swarm._criterion_proven('Never use "eval"', text) is True


@pytest.mark.parametrize("crit", [
    'Include "Alpha" but not "Omega"',         # mixed: Omega is forbidden, Alpha is not
    'No filler, include "Pricing"',            # the negation is not on the include
    '"eval" is never called',                  # negation after the literal
    'Must not use emojis but must include "Pricing"',
    'Includes every section except "FAQ"',
    'Must not include "Alpha", only "Pricing"',  # the second literal is wanted
    'Must not include "Alpha" and never "eval"',  # a second negation
])
def test_a_negation_the_checks_cannot_read_leaves_the_literals_undecided(crit):
    ph = {"acceptance": [crit]}
    for text in ("Alpha and Omega and Pricing and FAQ and eval()", "nothing relevant"):
        assert not any("required text" in p or "forbidden text" in p
                       for p in swarm._mechanical_problems(ph, text))
        assert swarm._criterion_proven(crit, text) is None
        assert swarm._proven(ph, text) is False


@pytest.mark.parametrize("crit", [
    'Does not need to include "FAQ"',
    'Need not include "FAQ"',
    "Needn't include \"FAQ\"",
    'Not required to include "FAQ"',
    "Doesn't have to mention \"FAQ\"",
    'It is not necessary to include "FAQ"',
    'May or may not include "FAQ"',
    'Requires no "FAQ"',
    'Including "FAQ" is optional, not mandatory',
])
def test_a_negated_need_makes_the_text_optional_not_forbidden(crit):
    """"Does not need to include X" allows X: reading it as a ban would
    reject valid output and tell the retry to REMOVE allowed text."""
    ph = {"acceptance": [crit]}
    for text in ("The FAQ is below.", "Nothing about it here."):
        assert not any("forbidden text" in p or "required text" in p
                       for p in swarm._mechanical_problems(ph, text))
        assert swarm._criterion_proven(crit, text) is None
    assert swarm._literal_rule(crit, swarm._literal_spans(crit)) is None


@pytest.mark.parametrize("crit", [
    'Do not forget to include "Pricing"',
    "Don't forget to mention \"Pricing\"",
    'Never forget to include "Pricing"',
    'Must not fail to include "Pricing"',
    'Do not neglect to include "Pricing"',
    "Don't hesitate to use \"Pricing\"",
    'Do not hesitate to show "Pricing"',
    'Should not just include "Pricing" but also explain it',
    'Should not merely mention "Pricing"',
    'Not including "Pricing" is a failure',
    'It cannot help but include "Pricing"',
    'Never stop using "Pricing"',
])
def test_a_negation_that_inverts_is_never_read_as_a_ban(crit):
    """"Do not forget to include X" REQUIRES X. Read as a ban it rejected
    correct output and told the retry to remove required text."""
    assert swarm._literal_rule(crit, swarm._literal_spans(crit)) != "forbidden"
    for text in ("Our Pricing is below.", "Nothing about it here."):
        assert not any("forbidden text" in p
                       for p in swarm._mechanical_problems({"acceptance": [crit]}, text))
    assert swarm._criterion_proven(crit, "Nothing about it here.") is not True


@pytest.mark.parametrize("crit", [
    'Must not include "lorem ipsum"',
    'Never use "eval"',
    'Must not ever include "lorem ipsum"',
    'Do not include "TODO" or "FIXME"',
])
def test_plain_bans_with_auxiliaries_stay_bans(crit):
    assert swarm._literal_rule(crit, swarm._literal_spans(crit)) == "forbidden"


def test_positive_criteria_read_exactly_as_before():
    # A negation INSIDE the literal, or "no more than N words", is no negation.
    assert swarm._criterion_proven('includes "Start free"', "Start free now") is True
    assert swarm._criterion_proven('includes "Start free"', "nothing") is False
    assert swarm._criterion_proven('Must include "Not found"', "Not found") is True
    assert swarm._criterion_proven('Must include "Not found"', "Found") is False
    assert swarm._mechanical_problems({"acceptance": ['Must include "Not found"']},
                                      "Found")[0].startswith("missing required text")
    ph = {"acceptance": ['Includes "Pricing", no more than 300 words']}
    assert swarm._mechanical_problems(ph, "Pricing is simple.") == []
    probs = swarm._mechanical_problems(ph, "Plans are simple.")
    assert probs and probs[0].startswith('missing required text "Pricing"')


def test_a_run_with_a_negated_criterion_never_asks_to_add_the_forbidden_text():
    """No manager, search on: clean output is accepted on the first attempt,
    and output with the forbidden text is retried with a 'remove it' problem."""
    plan = _plan(['Must not include "lorem ipsum"'])
    d = make_dispatch(plan, [CLEAN])
    out = swarm.run(ASK, d, search=swarm.Search(rng=Pick("deeper")))
    assert len(d.of("phase", "Alpha")) == 1, "clean output is accepted"
    assert out["search"]["log"] == []

    d2 = make_dispatch(plan, [LOREM, CLEAN])
    out2 = swarm.run(ASK, d2, search=swarm.Search(rng=Pick("deeper")))
    retry = d2.of("phase", "Alpha")[1]["user"]
    assert 'contains forbidden text "lorem ipsum"' in retry
    assert "missing required text" not in retry
    assert _alpha_output(out2) == CLEAN


# --------------------------------------------------------------------------- #
# 2. needs follow the planner's numbering
# --------------------------------------------------------------------------- #

def test_needs_are_translated_through_the_kept_phases():
    plan = {"phases": [
        {"title": "Schema", "task": "s"},
        {"title": "Dropped", "task": ""},
        {"title": "API", "task": "a", "needs": [1]},
        {"title": "UI", "task": "u", "needs": [3]},
        {"title": "Docs", "task": "d", "needs": [2]},
    ]}
    cleaned = swarm._clean_phases(plan)
    assert [(p["title"], p["needs"]) for p in cleaned] == [
        ("Schema", []), ("API", [1]), ("UI", [2]), ("Docs", [])]
    assert swarm._waves(cleaned) == [[1, 4], [2], [3]]


def test_a_non_object_entry_does_not_shift_the_needs():
    plan = {"phases": ["oops", {"title": "A", "task": "a"},
                       {"title": "B", "task": "b", "needs": [2]}]}
    cleaned = swarm._clean_phases(plan)
    assert [(p["title"], p["needs"]) for p in cleaned] == [("A", []), ("B", [1])]


def test_max_phases_counts_usable_phases_only():
    raw = [{"title": "P1", "task": "t"}, {"title": "X", "task": ""}]
    raw += [{"title": "P%d" % i, "task": "t"} for i in range(2, swarm.MAX_PHASES + 2)]
    cleaned = swarm._clean_phases({"phases": raw})
    assert len(cleaned) == swarm.MAX_PHASES
    assert [p["title"] for p in cleaned] == ["P%d" % i for i in range(1, swarm.MAX_PHASES + 1)]


def test_a_need_on_a_phase_past_the_limit_is_dropped():
    raw = [{"title": "P%d" % i, "task": "t"} for i in range(1, swarm.MAX_PHASES + 2)]
    raw[1]["needs"] = [1, swarm.MAX_PHASES + 1]
    cleaned = swarm._clean_phases({"phases": raw})
    assert cleaned[1]["needs"] == [1]


def test_self_and_forward_references_are_still_stripped():
    plan = {"phases": [{"title": "a", "task": "t", "needs": [1, 2, 5]},
                       {"title": "b", "task": "t", "needs": [1, 2]}]}
    cleaned = swarm._clean_phases(plan)
    assert cleaned[0]["needs"] == [] and cleaned[1]["needs"] == [1]


# --------------------------------------------------------------------------- #
# 3. phases written as an object
# --------------------------------------------------------------------------- #

def test_phases_as_an_object_are_read_in_key_order():
    plan = {"goal": "g", "phases": {
        "2": {"title": "B", "task": "b", "needs": [1]},
        "1": {"title": "A", "task": "a"}}}
    cleaned = swarm._clean_phases(plan)
    assert [(p["title"], p["needs"]) for p in cleaned] == [("A", []), ("B", [1])]


@pytest.mark.parametrize("phases", ["one big phase", 5, None, {"x": "not a phase"}])
def test_phases_of_any_other_shape_are_unusable_not_a_crash(phases):
    assert swarm._clean_phases({"goal": "g", "phases": phases}) == []


def test_a_run_whose_planner_writes_phases_as_an_object_does_not_raise():
    plan = json.dumps({"goal": "g", "phases": {
        "1": {"title": "Alpha", "task": "write the hero copy"},
        "2": {"title": "Beta", "task": "write the usage notes"}}})
    d = make_dispatch(plan, [CLEAN])
    out = swarm.run(ASK, d)
    assert out["text"] == "FINAL"
    assert len(d.of("phase", "Alpha")) == 1 and len(d.of("phase", "Beta")) == 1


# --------------------------------------------------------------------------- #
# 4. A degenerate attempt never replaces a real one
# --------------------------------------------------------------------------- #

REAL = "The hero copy says what the parser does, in two short sentences."
JUNK = "JUNK JUNK JUNK JUNK JUNK"


def test_search_keeps_the_real_attempt_over_a_degenerate_one(monkeypatch):
    monkeypatch.setattr(swarm, "answer_check", _JunkCheck)
    d = make_dispatch(_plan(['include "ALPHA-TOKEN"']), [REAL, JUNK, ""])
    out = swarm.run(ASK, d, search=swarm.Search(rng=Pick("deeper")))
    assert _alpha_output(out) == REAL
    synth = d.of("synth")[0]["user"]
    assert REAL in synth and "JUNK" not in synth
    first = out["search"]["log"][0]
    assert (first["score_before"], first["score_after"], first["success"]) == (1, 1, False)


def test_search_lets_a_real_attempt_replace_a_degenerate_best(monkeypatch):
    """A degenerate best counts ONE problem; a real attempt with two used to
    lose to it on count alone."""
    monkeypatch.setattr(swarm, "answer_check", _JunkCheck)
    plan = _plan(['include "ALPHA-TOKEN"', 'include "OMEGA-TOKEN"'])
    d = make_dispatch(plan, [JUNK, REAL, ""])
    out = swarm.run(ASK, d, search=swarm.Search(rng=Pick("deeper")))
    assert _alpha_output(out) == REAL
    first = out["search"]["log"][0]
    assert (first["choice"], first["score_after"], first["success"]) == ("wider", 2, True)


def test_the_retry_fallback_never_ships_a_degenerate_retry(monkeypatch):
    """With a manager that cannot fix, the phase ships its last REAL attempt,
    not the junk retry that followed it."""
    monkeypatch.setattr(swarm, "answer_check", _JunkCheck)

    def manager(msgs, max_tokens, purpose):
        return "", None
    d = make_dispatch(_plan(['include "ALPHA-TOKEN"']), [REAL, JUNK])
    out = swarm.run(ASK, d, manager=manager)
    assert _alpha_output(out) == REAL
    assert "JUNK" not in d.of("synth")[0]["user"]
