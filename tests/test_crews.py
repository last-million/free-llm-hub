"""The `crew-*` virtual models: specialised swarm pipelines and their rules.

Crews are the swarm (see tests/test_swarm.py for why it is a model and not a
mode) with a personality: a code crew reviews like a senior engineer, a
research crew hunts invented facts, and the code/research crews run ONE
bounded revision pass (plan -> do -> review -> fix) instead of only folding
the reviewer's complaints into synthesis. The properties that matter here:
(1) detection sends a request to the crew that fits it, (2) the revise loop
runs exactly when the profile says so and degrades when it fails, and
(3) crews are recognised and refused exactly like the swarm at the HTTP edge.
No network — dispatch is a stub.
"""
import json
import threading

import pytest

import app
import craft
import crews
import swarm


def _stage_of(system_prompt, user_prompt):
    """Which pipeline stage a call belongs to.

    Crew profiles override the stage SYSTEM prompts, so a crew call cannot be
    identified by prompt equality alone — the user-message scaffolding built
    inside swarm.run is the stable part. Positional scripting is not an
    option either: phases in a wave run concurrently."""
    # The revision worker reuses the phase SYSTEM prompt, so identify it by
    # the draft it is handed BEFORE the exact-prompt matches below.
    if "\n\nDRAFT\n" in user_prompt and "REVIEWER PROBLEMS TO FIX" in user_prompt:
        return "revision"
    for name, prompt in (("plan", swarm._PLAN_SYSTEM),
                         ("phase", swarm._PHASE_SYSTEM),
                         ("supervise", swarm._SUPERVISE_SYSTEM),
                         ("review", swarm._REVIEW_SYSTEM),
                         ("synth", swarm._SYNTH_SYSTEM)):
        if system_prompt == prompt:
            return name
    if "YOUR PHASE (" in user_prompt or "\nYOUR TASK: " in user_prompt:
        return "phase"
    if "WHAT THE TEAM PRODUCED" in user_prompt:
        return "supervise"
    if "PHASE OUTPUTS" in user_prompt:
        return "synth"
    if user_prompt.startswith("BRIEF\n"):
        return "review"
    return "plan"     # plan/retry carry no scaffolding, just the brief


def _dispatch(plan="", phases=(), supervise="", review="", synth="",
              revision="", provider=None):
    """A dispatch stub scripted BY STAGE. `phases` is consumed in call order for
    the phase stage; a plain string answers every phase."""
    calls = []
    remaining = list(phases) if not isinstance(phases, str) else None
    lock = threading.Lock()

    def dispatch(messages, max_tokens, exclude_pids=()):
        stage = _stage_of(messages[0]["content"], messages[-1]["content"])
        with lock:
            n = len(calls)
            calls.append({"stage": stage, "system": messages[0]["content"],
                          "user": messages[-1]["content"], "max_tokens": max_tokens,
                          "exclude_pids": exclude_pids})
            if stage == "phase":
                if remaining is None:
                    text = phases
                else:
                    text = remaining.pop(0) if remaining else ""
            else:
                text = {"plan": plan, "supervise": supervise, "review": review,
                        "synth": synth, "revision": revision}.get(stage, "")
        pid = provider(stage, n) if provider else "prov%d/model%d" % (n, n)
        return text, (pid if text else None)

    dispatch.calls = calls
    dispatch.of = lambda stage: [c for c in calls if c["stage"] == stage]
    return dispatch


def _revision_calls(d):
    """Calls between the first review and the synthesis that are neither —
    i.e. the revision worker, if one ran."""
    calls = d.calls
    first_review = next(i for i, c in enumerate(calls) if c["stage"] == "review")
    first_synth = next((i for i, c in enumerate(calls) if c["stage"] == "synth"),
                       len(calls))
    return [c for c in calls[first_review + 1:first_synth]
            if c["stage"] not in ("review", "synth")]


PLAN = json.dumps({"goal": "Build the thing",
                   "phases": [{"title": "Part A", "task": "do part A",
                               "done_when": "A exists"},
                              {"title": "Part B", "task": "do part B",
                               "done_when": "B exists"}]})
SHIP = json.dumps({"verdict": "ship", "problems": []})
REVISE = json.dumps({"verdict": "revise", "problems": ["part A is wrong"]})
NO_GAPS = json.dumps({"missing": []})
ASK = [{"role": "user", "content": "build me a small tool"}]
CREW_NAMES = ("code", "research", "write", "design")


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

def test_crew_ids_contents():
    assert crews.CREW_IDS == ("crew", "crew-code", "crew-research",
                              "crew-write", "crew-design")


@pytest.mark.parametrize("name", CREW_NAMES)
def test_every_crew_id_maps_to_a_runnable_profile(name):
    """A crew id without a profile would silently run as the generic swarm."""
    assert name in crews.CREWS, "crew-%s has no registry profile" % name
    d = _dispatch(plan=PLAN, phases=["a", "b"], supervise=NO_GAPS,
                  review=SHIP, synth="final")
    out = crews.run(ASK, d, name)
    assert out["text"] == "final", "crew %r did not run its pipeline" % name
    assert out["crew"] == name


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text,expected", [
    ("Fix this Python bug in my Flask endpoint and add unit tests", "code"),
    ("Research the causes of the 2008 financial crisis and compare the main "
     "explanations", "research"),
    ("Write a 2000-word essay about my grandmother's garden", "write"),
    ("Design a landing page for my coffee shop", "design"),
])
def test_detect_crew_picks_the_fitting_crew(text, expected):
    assert crews.detect_crew([{"role": "user", "content": text}]) == expected


def test_an_ambiguous_request_falls_back_to_the_generic_crew():
    """No clear domain vocabulary -> "" -> the generic pipeline, because a
    wrong crew is worse than none."""
    assert crews.detect_crew([{"role": "user", "content": "Thanks!"}]) == ""


def test_crew_name_auto_routes_through_detect_crew(monkeypatch):
    seen = {}

    def fake_detect(messages):
        seen["messages"] = messages
        return "write"

    monkeypatch.setattr(crews, "detect_crew", fake_detect)
    d = _dispatch(plan=PLAN, phases=["a", "b"], supervise=NO_GAPS,
                  review=SHIP, synth="final")
    out = crews.run(ASK, d, "auto")
    assert seen.get("messages") is ASK, "auto did not ask detect_crew"
    assert out["text"] == "final"


# --------------------------------------------------------------------------- #
# Crew behaviour
# --------------------------------------------------------------------------- #

def test_the_design_crew_briefs_its_workers_with_the_craft_brief():
    """The design crew's whole point: workers produce craft-grade UI, which is
    what craft.WEB_DESIGN exists to force."""
    d = _dispatch(plan=PLAN, phases=["a", "b"], supervise=NO_GAPS,
                  review=SHIP, synth="final")
    crews.run([{"role": "user", "content": "design a landing page"}], d, "design")
    phase_calls = d.of("phase")
    assert phase_calls, "design crew ran no workers"
    assert any("WEB DESIGN BRIEF" in c["system"] for c in phase_calls), \
        "no worker received craft.WEB_DESIGN"


def test_the_code_crew_does_not_get_the_design_brief():
    d = _dispatch(plan=PLAN, phases=["a", "b"], supervise=NO_GAPS,
                  review=SHIP, synth="final")
    crews.run(ASK, d, "code")
    assert all("WEB DESIGN BRIEF" not in c["system"] for c in d.of("phase"))


# --------------------------------------------------------------------------- #
# The revise loop — one bounded plan -> do -> review -> FIX pass
# --------------------------------------------------------------------------- #

def test_a_revise_verdict_runs_a_revision_before_synthesis():
    d = _dispatch(plan=PLAN, phases=["draft A", "draft B"], supervise=NO_GAPS,
                  review=REVISE, revision="REVISED-DRAFT", synth="FINAL")
    out = swarm.run(ASK, d, profile={"max_revisions": 1})
    revisions = _revision_calls(d)
    assert revisions, "verdict was revise but nothing was revised"
    assert "REVISED-DRAFT" in d.of("synth")[0]["user"], \
        "synthesis did not receive the revised draft"
    assert out["text"] == "FINAL"


def test_the_revision_worker_is_shown_the_draft_and_the_problems():
    """It cannot fix what it cannot see."""
    d = _dispatch(plan=PLAN, phases=["draft A", "draft B"], supervise=NO_GAPS,
                  review=REVISE, revision="REVISED-DRAFT", synth="FINAL")
    swarm.run(ASK, d, profile={"max_revisions": 1})
    rev = _revision_calls(d)[0]
    assert "draft A" in rev["user"], "revision worker was not shown the draft"
    assert "part A is wrong" in rev["user"], \
        "revision worker was not shown the reviewer's problems"


def test_max_revisions_zero_keeps_todays_behaviour():
    """profile defaults must reproduce the swarm exactly: a revise verdict is
    folded into synthesis, never worked on by a revision pass."""
    d = _dispatch(plan=PLAN, phases=["draft A", "draft B"], supervise=NO_GAPS,
                  review=REVISE, revision="REVISED-DRAFT", synth="FINAL")
    out = swarm.run(ASK, d, profile={"max_revisions": 0})
    assert not _revision_calls(d), "a revision ran with max_revisions=0"
    assert out["text"] == "FINAL"
    assert "part A is wrong" in d.of("synth")[0]["user"], \
        "revise problems stopped reaching synthesis"


def test_no_profile_at_all_is_the_plain_swarm():
    d = _dispatch(plan=PLAN, phases=["draft A", "draft B"], supervise=NO_GAPS,
                  review=REVISE, revision="REVISED-DRAFT", synth="FINAL")
    out = swarm.run(ASK, d)
    assert not _revision_calls(d)
    assert out["text"] == "FINAL"


def test_a_ship_verdict_never_triggers_a_revision():
    d = _dispatch(plan=PLAN, phases=["draft A", "draft B"], supervise=NO_GAPS,
                  review=SHIP, revision="REVISED-DRAFT", synth="FINAL")
    swarm.run(ASK, d, profile={"max_revisions": 1})
    assert not _revision_calls(d), "approved work was revised anyway"


def test_the_code_crew_revises_but_the_write_crew_does_not():
    """Revise loop ON for code/research, OFF for write/design."""
    d = _dispatch(plan=PLAN, phases=["draft A", "draft B"], supervise=NO_GAPS,
                  review=REVISE, revision="REVISED-DRAFT", synth="FINAL")
    crews.run(ASK, d, "code")
    assert _revision_calls(d), "code crew did not revise a revise verdict"

    d2 = _dispatch(plan=PLAN, phases=["draft A", "draft B"], supervise=NO_GAPS,
                   review=REVISE, revision="REVISED-DRAFT", synth="FINAL")
    crews.run(ASK, d2, "write")
    assert not _revision_calls(d2), "write crew ran a revision pass"


# --------------------------------------------------------------------------- #
# Degradation — a failed revision must never cost the answer
# --------------------------------------------------------------------------- #

def test_a_failed_revision_still_yields_a_final_answer():
    d = _dispatch(plan=PLAN, phases=["draft A", "draft B"], supervise=NO_GAPS,
                  review=REVISE, revision="", synth="FINAL")
    out = swarm.run(ASK, d, profile={"max_revisions": 1})
    assert out["text"], "a failed revision pass lost the whole answer"


def test_a_failed_revision_does_not_poison_synthesis():
    d = _dispatch(plan=PLAN, phases=["draft A", "draft B"], supervise=NO_GAPS,
                  review=REVISE, revision="", synth="FINAL")
    out = swarm.run(ASK, d, profile={"max_revisions": 1})
    if d.of("synth"):
        assert out["text"] == "FINAL"
    else:   # synthesis skipped: the draft itself must survive instead
        assert "draft A" in out["text"]


# --------------------------------------------------------------------------- #
# The trailer
# --------------------------------------------------------------------------- #

def test_format_answer_shows_the_result_and_empty_formats_to_empty():
    out = crews.format_answer({"text": "BODY", "plan": {"goal": "G"},
                               "phases": [{"title": "A", "output": "x"}],
                               "review": {}, "models": []})
    assert out.startswith("BODY")
    assert crews.format_answer({"text": "", "plan": {}, "phases": [],
                                "models": []}) == ""


# --------------------------------------------------------------------------- #
# HTTP wiring — crews are swarm models: recognised, and refused with tools
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("mid", list(crews.CREW_IDS) + ["CREW-CODE"])
def test_crew_ids_are_recognised_as_swarm_models(mid):
    assert app._is_swarm_model(mid)


def test_a_crew_call_without_messages_is_a_400():
    client = app.app.test_client()
    r = client.post("/v1/chat/completions", json={"model": "crew-code"})
    assert r.status_code == 400


def test_crew_tool_calling_turns_are_refused():
    """Same rule as the swarm: an agent's tool turn must never be multi-passed."""
    client = app.app.test_client()
    r = client.post("/v1/chat/completions", json={
        "model": "crew-code",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function",
                   "function": {"name": "x", "parameters": {}}}]})
    assert r.status_code == 400
    assert "tool" in r.get_json()["error"]["message"].lower()
