"""The `swarm` virtual model: pipeline behaviour and its refusal rules.

The swarm is opt-in BY DESIGN. An automatic multi-pass mode would corrupt the
agent loops Codex/Claude Code run, so the two properties that matter most here
are: (1) it only ever runs when explicitly selected, and (2) every stage
degrades instead of failing. No network — dispatch is a stub.
"""
import json

import pytest

import app
import swarm


def _dispatch_from(script):
    """A dispatch stub returning scripted texts in order, recording each call."""
    calls = []

    def dispatch(messages, max_tokens, exclude_pids=()):
        calls.append({"system": messages[0]["content"], "user": messages[-1]["content"],
                      "max_tokens": max_tokens, "exclude_pids": exclude_pids})
        i = len(calls) - 1
        text = script[i] if i < len(script) else ""
        return text, ("prov%d/model%d" % (i, i) if text else None)
    dispatch.calls = calls
    return dispatch


PLAN = json.dumps({"goal": "Build a landing page",
                   "phases": [{"title": "Copy", "task": "write the hero copy",
                               "done_when": "hero exists"},
                              {"title": "Layout", "task": "structure the sections",
                               "done_when": "sections listed"}]})
SHIP = json.dumps({"verdict": "ship", "problems": []})
REVISE = json.dumps({"verdict": "revise", "problems": ["hero is generic"]})


# --------------------------------------------------------------------------- #
# Selection: opt-in only
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("mid", ["swarm", "SWARM", " team ", "plan", "swarm/anything"])
def test_swarm_ids_are_recognised(mid):
    assert app._is_swarm_model(mid)


@pytest.mark.parametrize("mid", ["auto", "", "gpt-4.1", "puter/gpt-5.6-sol", "claude-opus-5"])
def test_ordinary_models_never_trigger_the_swarm(mid):
    assert not app._is_swarm_model(mid)


def test_tool_calling_turns_are_refused_not_silently_reshaped():
    """The whole reason this is a model and not a mode: an agent's tool turn
    must never be multi-passed."""
    client = app.app.test_client()
    r = client.post("/v1/chat/completions", json={
        "model": "swarm", "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "x", "parameters": {}}}]})
    assert r.status_code == 400
    assert "tool" in r.get_json()["error"]["message"].lower()


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #

def test_full_pipeline_runs_plan_phases_review_synthesis():
    d = _dispatch_from([PLAN, "hero copy", "layout", REVISE, "final answer"])
    out = swarm.run([{"role": "user", "content": "build me a landing page"}], d)
    assert out["text"] == "final answer"
    assert [p["title"] for p in out["phases"]] == ["Copy", "Layout"]
    assert [s for s, _ in out["models"]] == [
        "plan", "phase:Copy", "phase:Layout", "review", "synthesis"]


def test_each_phase_sees_the_previous_phase_output():
    d = _dispatch_from([PLAN, "HERO-TEXT", "layout", SHIP, "final"])
    swarm.run([{"role": "user", "content": "go"}], d)
    assert "HERO-TEXT" in d.calls[2]["user"], "phase 2 did not receive phase 1's work"


def test_the_reviewer_is_kept_off_the_provider_that_wrote_the_draft():
    """A model reviewing its own output agrees with itself."""
    d = _dispatch_from([PLAN, "a", "b", SHIP, "final"])
    swarm.run([{"role": "user", "content": "go"}], d)
    review_call = d.calls[3]
    assert review_call["exclude_pids"], "review ran with no provider exclusion"
    assert "prov1" in review_call["exclude_pids"]


def test_a_single_phase_that_passes_review_is_returned_unchanged():
    """Re-writing an approved one-phase answer can only make it worse."""
    one = json.dumps({"goal": "g", "phases": [{"title": "Do", "task": "do it"}]})
    d = _dispatch_from([one, "the answer", SHIP])
    out = swarm.run([{"role": "user", "content": "go"}], d)
    assert out["text"] == "the answer"
    assert len(d.calls) == 3, "a synthesis pass ran when it was not needed"


# --------------------------------------------------------------------------- #
# Degradation — a swarm request must never fail where a plain model would answer
# --------------------------------------------------------------------------- #

def test_an_unusable_plan_degrades_to_one_phase():
    d = _dispatch_from(["not json at all", "the answer", SHIP])
    out = swarm.run([{"role": "user", "content": "do the thing"}], d)
    assert out["text"] == "the answer"
    assert d.calls[1]["user"].count("do the thing"), "phase lost the original ask"


def test_a_failed_review_still_returns_the_work():
    d = _dispatch_from([PLAN, "a", "b", "", "final"])
    out = swarm.run([{"role": "user", "content": "go"}], d)
    assert out["text"] == "final"


def test_a_failed_synthesis_falls_back_to_the_draft():
    d = _dispatch_from([PLAN, "phase one", "phase two", REVISE, ""])
    out = swarm.run([{"role": "user", "content": "go"}], d)
    assert "phase one" in out["text"] and "phase two" in out["text"]


def test_total_failure_returns_empty_so_the_caller_can_error_honestly():
    d = _dispatch_from([])
    out = swarm.run([{"role": "user", "content": "go"}], d)
    assert out["text"] == ""


def test_phase_count_is_capped():
    many = json.dumps({"goal": "g", "phases": [
        {"title": "P%d" % i, "task": "t%d" % i} for i in range(20)]})
    d = _dispatch_from([many] + ["out"] * 30)
    out = swarm.run([{"role": "user", "content": "go"}], d)
    assert len(out["phases"]) <= swarm.MAX_PHASES


def test_json_wrapped_in_a_fence_or_prose_still_parses():
    assert swarm._parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert swarm._parse_json('Sure! {"a": 1} hope that helps') == {"a": 1}
    assert swarm._parse_json("no json here") is None


# --------------------------------------------------------------------------- #
# The trailer
# --------------------------------------------------------------------------- #

def test_the_trailer_shows_the_plan_and_the_real_per_stage_models():
    """Naming the model per stage is the direct answer to 'is it really using
    the good models' -- the question a silent fallback made unanswerable."""
    out = swarm.format_answer({
        "text": "BODY", "plan": {"goal": "G"},
        "phases": [{"title": "Copy", "output": "x"}],
        "review": {"problems": []},
        "models": [("plan", "puter/kimi-k3"), ("review", "g4f/glm-5.2")]})
    assert out.startswith("BODY")
    assert "Plan followed" in out and "[x] Copy" in out
    assert "puter/kimi-k3" in out and "g4f/glm-5.2" in out


def test_the_trailer_does_not_claim_reviewer_problems_were_fixed():
    """It cannot verify that, and asserting it would be the same unverifiable
    claim the reviewer exists to catch."""
    out = swarm.format_answer({
        "text": "BODY", "plan": {}, "phases": [{"title": "A", "output": "x"}],
        "review": {"verdict": "revise", "problems": ["missing the statement"]},
        "models": []})
    assert "missing the statement" in out
    assert "applied above" not in out


def test_an_empty_result_formats_to_empty():
    assert swarm.format_answer({"text": "", "plan": {}, "phases": [], "models": []}) == ""
