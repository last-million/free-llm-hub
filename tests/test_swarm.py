"""The `swarm` virtual model: pipeline behaviour and its refusal rules.

The swarm is opt-in BY DESIGN. An automatic multi-pass mode would corrupt the
agent loops Codex/Claude Code run, so the two properties that matter most here
are: (1) it only ever runs when explicitly selected, and (2) every stage
degrades instead of failing. No network — dispatch is a stub.
"""
import json
import threading

import pytest

from unittest import mock

import app
import craft
import swarm


def _stage_of(system_prompt):
    """Which pipeline stage a call belongs to, from its system prompt.

    Identified by stage rather than by call INDEX because phases now run
    concurrently: within a wave, completion order is genuinely nondeterministic,
    so any positional script would be flaky by construction."""
    for name, prompt in (("plan", swarm._PLAN_SYSTEM),
                         ("phase", swarm._PHASE_SYSTEM),
                         ("supervise", swarm._SUPERVISE_SYSTEM),
                         ("review", swarm._REVIEW_SYSTEM),
                         ("synth", swarm._SYNTH_SYSTEM)):
        if system_prompt == prompt:
            return name
    return "?"


def _dispatch(plan="", phases=(), supervise="", review="", synth="", provider=None):
    """A dispatch stub scripted BY STAGE. `phases` is consumed in call order for
    the phase stage; a plain string answers every phase."""
    calls = []
    remaining = list(phases) if not isinstance(phases, str) else None
    lock = threading.Lock()

    def dispatch(messages, max_tokens, exclude_pids=()):
        stage = _stage_of(messages[0]["content"])
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
                text = {"plan": plan, "supervise": supervise,
                        "review": review, "synth": synth}.get(stage, "")
        pid = provider(stage, n) if provider else "prov%d/model%d" % (n, n)
        return text, (pid if text else None)

    dispatch.calls = calls
    dispatch.of = lambda stage: [c for c in calls if c["stage"] == stage]
    return dispatch


def _dispatch_from(script):
    """Legacy positional stub, kept for the stages that are still strictly
    sequential (plan -> single phase -> review)."""
    calls = []

    def dispatch(messages, max_tokens, exclude_pids=()):
        calls.append({"stage": _stage_of(messages[0]["content"]),
                      "system": messages[0]["content"], "user": messages[-1]["content"],
                      "max_tokens": max_tokens, "exclude_pids": exclude_pids})
        i = len(calls) - 1
        text = script[i] if i < len(script) else ""
        return text, ("prov%d/model%d" % (i, i) if text else None)
    dispatch.calls = calls
    dispatch.of = lambda stage: [c for c in calls if c["stage"] == stage]
    return dispatch


PLAN = json.dumps({"goal": "Build a landing page",
                   "phases": [{"title": "Copy", "task": "write the hero copy",
                               "done_when": "hero exists"},
                              {"title": "Layout", "task": "structure the sections",
                               "done_when": "sections listed"}]})
SHIP = json.dumps({"verdict": "ship", "problems": []})
NO_GAPS = json.dumps({"missing": []})
# Same two phases, but phase 2 DECLARES it needs phase 1 -> they serialise.
CHAIN_PLAN = json.dumps({"goal": "Build a landing page",
                         "phases": [{"title": "Copy", "task": "write the hero copy"},
                                    {"title": "Layout", "task": "structure the sections",
                                     "needs": [1]}]})
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


def test_a_tool_turn_takes_the_parallel_path_not_the_prose_pipeline():
    """CHANGED 2026-08-30. A tool turn used to be refused with a 400, because
    the prose pipeline emits no tool calls and an agent driven by it writes
    nothing. Refusing was correct but useless -- "swarm" then simply did not
    work in any CLI.

    A tool turn now goes to _swarm_tool_turn instead: the same request, with the
    real tools, on several distinct strong models at once, best response wins.
    The prose pipeline is still never used for a tool turn, which is what this
    test really exists to guarantee."""
    called = {}

    def fake_tool_turn(body):
        called["body"] = body
        return (app.jsonify({"ok": True}), 200, {})

    def boom(*a, **k):                      # the prose pipeline must NOT run
        raise AssertionError("a tool turn must never reach swarm.run")

    with mock.patch.object(app, "_swarm_tool_turn", side_effect=fake_tool_turn),             mock.patch.object(app.swarm, "run", side_effect=boom):
        r = app.app.test_client().post("/v1/chat/completions", json={
            "model": "swarm", "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "x", "parameters": {}}}]})
    assert r.status_code == 200
    assert called["body"]["tools"], "the tools must be passed through, not stripped"


def test_a_tool_turn_that_nothing_can_serve_fails_loudly():
    """Falling through to a single model would silently drop the mode the user
    chose; answering with prose would leave the agent nothing to execute."""
    with mock.patch.object(app, "_swarm_tool_turn", return_value=None):
        r = app.app.test_client().post("/v1/chat/completions", json={
            "model": "swarm", "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "x", "parameters": {}}}]})
    assert r.status_code == 503


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #

def test_full_pipeline_runs_plan_phases_review_synthesis():
    d = _dispatch(plan=PLAN, phases=["hero copy", "layout"], supervise=NO_GAPS,
                  review=REVISE, synth="final answer")
    out = swarm.run([{"role": "user", "content": "build me a landing page"}], d)
    assert out["text"] == "final answer"
    assert [p["title"] for p in out["phases"]] == ["Copy", "Layout"]
    stages = [s for s, _ in out["models"]]
    assert stages[0] == "plan" and stages[-1] == "synthesis"
    assert {"phase:Copy", "phase:Layout", "supervisor", "review"} <= set(stages)


def test_a_phase_receives_the_output_of_what_it_DECLARED_it_needs():
    d = _dispatch(plan=CHAIN_PLAN, phases=["HERO-TEXT", "layout"],
                  supervise=NO_GAPS, review=SHIP, synth="final")
    swarm.run([{"role": "user", "content": "go"}], d)
    second = [c for c in d.of("phase") if "Layout" in c["user"]][0]
    assert "HERO-TEXT" in second["user"], "phase 2 did not receive the work it needs"


def test_the_reviewer_is_kept_off_the_provider_that_wrote_the_draft():
    """A model reviewing its own output agrees with itself."""
    d = _dispatch(plan=PLAN, phases=["a", "b"], supervise=NO_GAPS,
                  review=SHIP, synth="final",
                  provider=lambda stage, n: "writer/m" if stage == "phase" else "other/m")
    swarm.run([{"role": "user", "content": "go"}], d)
    review = d.of("review")[0]
    assert review["exclude_pids"], "review ran with no provider exclusion"
    assert "writer" in review["exclude_pids"]


def test_a_single_phase_that_passes_review_is_returned_unchanged():
    """Re-writing an approved one-phase answer can only make it worse.

    Reached through the FALLBACK path now: a one-phase PLAN is refused (see
    MIN_PHASES), so the single phase here is the whole-brief fallback."""
    d = _dispatch(plan="not a plan", phases="the answer", review=SHIP)
    out = swarm.run([{"role": "user", "content": "go"}], d)
    assert out["text"] == "the answer"
    assert not d.of("synth"), "a synthesis pass ran when it was not needed"


def test_a_one_phase_plan_is_refused_so_no_scope_is_dropped():
    """Observed live: a 3-part brief came back as a single phase titled "Hero
    Headline", the worker delivered exactly that, and two thirds of the request
    vanished. The whole-brief fallback answers all of it."""
    one = json.dumps({"goal": "g", "phases": [{"title": "Hero", "task": "the headline"}]})
    d = _dispatch(plan=one, phases="the answer", review=SHIP)
    out = swarm.run([{"role": "user", "content": "headline, about text and 6 items"}], d)
    assert len(d.of("plan")) == 2, "a one-phase plan was accepted without a retry"
    phase = d.of("phase")[0]["user"]
    assert "6 items" in phase, "the fallback phase lost part of the request"


# --------------------------------------------------------------------------- #
# Degradation — a swarm request must never fail where a plain model would answer
# --------------------------------------------------------------------------- #

def test_an_unusable_plan_degrades_to_one_phase():
    d = _dispatch(plan="not json at all", phases="the answer", review=SHIP)
    out = swarm.run([{"role": "user", "content": "do the thing"}], d)
    assert out["text"] == "the answer"
    assert "do the thing" in d.of("phase")[0]["user"], "phase lost the original ask"


def test_a_failed_review_still_returns_the_work():
    d = _dispatch(plan=PLAN, phases=["a", "b"], supervise=NO_GAPS,
                  review="", synth="final")
    out = swarm.run([{"role": "user", "content": "go"}], d)
    assert out["text"] == "final"


def test_a_failed_synthesis_falls_back_to_the_draft():
    d = _dispatch(plan=PLAN, phases=["phase one", "phase two"], supervise=NO_GAPS,
                  review=REVISE, synth="")
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


# --------------------------------------------------------------------------- #
# Supervisor + subagents: each worker in its OWN context, waves in parallel.
#
# The point is capacity. Five workers on 32K windows have ~160K of usable
# context between them; the old pipeline concatenated every earlier phase into
# every later one and ran out on exactly the large builds it existed for.
# --------------------------------------------------------------------------- #

def _plan(*phases):
    return json.dumps({"goal": "g", "phases": list(phases)})


def test_independent_phases_are_grouped_into_one_wave():
    phases = [{"title": "a", "task": "t", "needs": []},
              {"title": "b", "task": "t", "needs": []},
              {"title": "c", "task": "t", "needs": [1, 2]}]
    assert swarm._waves(phases) == [[1, 2], [3]]


def test_a_dependency_chain_serialises():
    phases = [{"title": "a", "task": "t", "needs": []},
              {"title": "b", "task": "t", "needs": [1]},
              {"title": "c", "task": "t", "needs": [2]}]
    assert swarm._waves(phases) == [[1], [2], [3]]


def test_waves_never_hang_on_an_unsatisfiable_graph():
    """A swarm must never deadlock: run the remainder rather than spin."""
    phases = [{"title": "a", "task": "t", "needs": [99]}]
    assert swarm._waves(phases) == [[1]]


def test_forward_and_self_references_are_stripped():
    """A self-reference or a forward reference would deadlock the scheduler."""
    plan = {"phases": [{"title": "a", "task": "t", "needs": [1, 2, 5]},
                       {"title": "b", "task": "t", "needs": [1, 2]}]}
    cleaned = swarm._clean_phases(plan)
    assert cleaned[0]["needs"] == []      # phase 1 cannot need 1, 2 or 5
    assert cleaned[1]["needs"] == [1]     # phase 2 may need 1, never itself


def test_independent_workers_actually_run_concurrently():
    """Not just grouped — genuinely overlapping in time."""
    import threading
    import time
    inside = []
    peak = [0]
    lock = threading.Lock()

    def dispatch(messages, max_tokens, exclude_pids=()):
        stage = _stage_of(messages[0]["content"])
        if stage != "phase":
            return {"plan": _plan({"title": "a", "task": "t", "needs": []},
                                  {"title": "b", "task": "t", "needs": []}),
                    "supervise": NO_GAPS, "review": SHIP,
                    "synth": "done"}.get(stage, ""), "p/m"
        with lock:
            inside.append(1)
            peak[0] = max(peak[0], len(inside))
        time.sleep(0.15)
        with lock:
            inside.pop()
        return "out", "p/m"

    swarm.run([{"role": "user", "content": "go"}], dispatch)
    assert peak[0] == 2, "independent phases ran one after the other"


def test_a_worker_never_sees_a_phase_it_did_not_ask_for():
    """Context isolation is the whole design: an unrelated teammate's output
    must not land in this worker's window."""
    d = _dispatch(plan=_plan({"title": "Copy", "task": "t", "needs": []},
                             {"title": "Data", "task": "t", "needs": []},
                             {"title": "Assemble", "task": "t", "needs": [1]}),
                  phases=["COPY-OUT", "DATA-OUT", "assembled"],
                  supervise=NO_GAPS, review=SHIP, synth="final")
    swarm.run([{"role": "user", "content": "go"}], d)
    assemble = [c for c in d.of("phase") if "Assemble" in c["user"]][0]
    assert "COPY-OUT" in assemble["user"], "declared dependency was not provided"
    assert "DATA-OUT" not in assemble["user"], "worker was given work it never asked for"


def test_a_parallel_worker_is_told_it_cannot_see_the_others():
    """Otherwise it writes as if it can, and the outputs contradict."""
    d = _dispatch(plan=_plan({"title": "a", "task": "t", "needs": []},
                             {"title": "b", "task": "t", "needs": []}),
                  phases="out", supervise=NO_GAPS, review=SHIP, synth="final")
    swarm.run([{"role": "user", "content": "go"}], d)
    assert all("cannot see their output" in c["user"] for c in d.of("phase"))


def test_a_dependency_is_clipped_so_one_worker_cannot_blow_the_window():
    """Handing over an unbounded teammate output puts the ceiling straight back."""
    d = _dispatch(plan=_plan({"title": "Copy", "task": "t", "needs": []},
                             {"title": "Use", "task": "t", "needs": [1]}),
                  phases=["X" * 40000, "ok"], supervise=NO_GAPS, review=SHIP, synth="f")
    swarm.run([{"role": "user", "content": "go"}], d)
    use = [c for c in d.of("phase") if "Use" in c["user"]][0]
    assert len(use["user"]) < swarm.DEP_CONTEXT_CHARS + 2000
    assert "trimmed" in use["user"]


def test_the_supervisor_fills_a_gap_it_finds():
    gap = json.dumps({"missing": [{"title": "Pricing", "task": "the pricing table"}]})
    d = _dispatch(plan=PLAN, phases=["a", "b", "PRICING-TABLE"],
                  supervise=gap, review=SHIP, synth="final")
    out = swarm.run([{"role": "user", "content": "go"}], d)
    assert "repair:Pricing" in [s for s, _ in out["models"]]
    assert any(p["title"] == "Pricing" for p in out["phases"])


def test_the_supervisor_stays_quiet_when_nothing_is_missing():
    """A supervisor that always finds work doubles the cost for nothing."""
    d = _dispatch(plan=PLAN, phases=["a", "b"], supervise=NO_GAPS,
                  review=SHIP, synth="final")
    out = swarm.run([{"role": "user", "content": "go"}], d)
    assert not [s for s, _ in out["models"] if s.startswith("repair:")]


def test_no_supervisor_round_for_a_single_phase():
    """There is no team to reconcile — it would be pure cost."""
    d = _dispatch(plan=_plan({"title": "Do", "task": "do it", "needs": []}),
                  phases="the answer", supervise=NO_GAPS, review=SHIP)
    swarm.run([{"role": "user", "content": "go"}], d)
    assert not d.of("supervise")


def test_one_worker_dying_does_not_kill_the_wave():
    def dispatch(messages, max_tokens, exclude_pids=()):
        stage = _stage_of(messages[0]["content"])
        if stage == "plan":
            return _plan({"title": "a", "task": "t", "needs": []},
                         {"title": "b", "task": "t", "needs": []}), "p/m"
        if stage == "phase":
            if "a" in messages[-1]["content"].split("YOUR PHASE (1")[-1][:40]:
                raise RuntimeError("worker exploded")
            return "survivor", "p/m"
        return {"supervise": NO_GAPS, "review": SHIP, "synth": "final"}.get(stage, ""), "p/m"

    out = swarm.run([{"role": "user", "content": "go"}], dispatch)
    assert out["text"], "a single failed worker took down the whole swarm"


# --------------------------------------------------------------------------- #
# Plan robustness. The plan is the linchpin: no plan means no team, and the
# swarm silently degrades to the single model the user did NOT select.
# --------------------------------------------------------------------------- #

# Captured verbatim from a real planner (groq/allam-2-7b): it opens an object
# per phase and never closes any of them. Not truncation — the structure is
# wrong — so balancing braces cannot fix it, but the content is all there.
REAL_BROKEN_PLAN = '''{"goal": "Build a page", "phases": [
{
  "title": "Search Engine Landscape",
  "task": "Discover images", "done_when": "Stable image URLs ready",
{
  "title": "Page structure",
  "task": "Arrange content pages", "done_when": "Layout defined",
{
  "title": "Copy",
  "task": "Write Hero Headline", "done_when": "Headline'''


@pytest.mark.parametrize("raw,expected", [
    ('{"a":[1,2,],}', {"a": [1, 2]}),
    ('```json\n{"goal":"g"}\n```', {"goal": "g"}),
    ('Sure! {"goal":"g"} Hope that helps.', {"goal": "g"}),
    ('{"goal":"g","phases":[{"title":"t","task":"do', {"goal": "g", "phases": [{"title": "t", "task": "do"}]}),
    ('I cannot do that', None),
    ('', None),
])
def test_parse_json_survives_how_models_actually_reply(raw, expected):
    assert swarm._parse_json(raw) == expected


def test_a_structurally_broken_plan_still_yields_a_team():
    """The failure that made a live swarm run as one model."""
    got = swarm._phases_from_text(REAL_BROKEN_PLAN)
    assert [p["title"] for p in got] == [
        "Search Engine Landscape", "Page structure", "Copy"]
    assert all(p["needs"] == [] for p in got), "a salvaged plan must not invent deps"


def test_recovered_phases_run_as_a_real_team():
    d = _dispatch(plan=REAL_BROKEN_PLAN, phases="out", supervise=NO_GAPS,
                  review=SHIP, synth="final")
    out = swarm.run([{"role": "user", "content": "go"}], d)
    assert len(d.of("phase")) == 3, "salvaged plan did not produce three workers"
    assert out["text"] == "final"


def test_the_planner_gets_one_retry_before_the_swarm_gives_up():
    calls = {"n": 0}

    def dispatch(messages, max_tokens, exclude_pids=()):
        stage = _stage_of(messages[0]["content"])
        if stage == "plan":
            calls["n"] += 1
            if calls["n"] == 1:
                return "I'd be happy to help you plan this!", "p/m"
            return _plan({"title": "a", "task": "t", "needs": []},
                         {"title": "b", "task": "t", "needs": []}), "p/m"
        if stage == "phase":
            return "work", "p/m"
        return {"supervise": NO_GAPS, "review": SHIP, "synth": "final"}.get(stage, ""), "p/m"

    out = swarm.run([{"role": "user", "content": "go"}], dispatch)
    assert calls["n"] == 2, "planner was not retried"
    assert len(out["phases"]) == 2, "retry did not produce a team"


def test_a_hopeless_planner_still_answers_the_user():
    """Two bad plans must degrade to one phase, never to an error."""
    d = _dispatch(plan="no.", phases="the answer", review=SHIP)
    out = swarm.run([{"role": "user", "content": "do the thing"}], d)
    assert out["text"] == "the answer"
    assert len(d.of("plan")) == 2


# --------------------------------------------------------------------------- #
# Craft briefs must NOT reach swarm-internal stages.
# --------------------------------------------------------------------------- #

def test_swarm_stages_opt_out_of_craft_briefs():
    """Observed live: the planner is told "reply with JSON ONLY", the injected
    WEB_DESIGN/SEO/IMAGES briefs told it to build a website, and it obeyed the
    brief — returning publication-ready copy instead of a plan. Every creation
    task therefore fell back to a single model, which is the one task the swarm
    exists for."""
    payload = {"model": "m", "messages": [
        {"role": "system", "content": swarm._PLAN_SYSTEM},
        {"role": "user", "content": "build me a restaurant website"}], "_no_craft": True}
    assert craft.names(payload["messages"][-1]["content"]), "test needs a matching brief"
    seen = {}

    def fake_post(pid, p, stream):
        seen["messages"] = p["messages"]
        seen["keys"] = set(p)
        raise RuntimeError("stop here")

    import app as _app
    orig = _app._upstream_chat
    try:
        # exercise the real injection point
        _app._upstream_chat = fake_post
        try:
            _app._dispatch_chat("groq", payload, False)
        except RuntimeError:
            pass
    finally:
        _app._upstream_chat = orig
    assert len(seen["messages"]) == 2, "a craft brief was injected into a swarm stage"


def test_the_internal_flag_never_reaches_a_provider():
    """`_no_craft` is ours. A provider receiving an unknown top-level field can
    reject the whole request."""
    import app as _app
    sent = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

        def close(self):
            pass

    def fake_post(url, **kw):
        sent["body"] = kw.get("json") or {}
        return _Resp()

    orig = _app.requests.post
    try:
        _app.requests.post = fake_post
        _app._upstream_chat("groq", {"model": "m", "messages": [
            {"role": "user", "content": "hi"}], "_no_craft": True}, False)
    except Exception:                                            # noqa: BLE001
        pass                       # provider config may reject; we only need the body
    finally:
        _app.requests.post = orig
    if sent.get("body") is not None:
        assert "_no_craft" not in sent["body"], "internal flag leaked to the provider"


# --------------------------------------------------------------------------- #
# A hung non-streaming hop must die at the OVERALL deadline, not hold the
# stage forever. Observed live 2026-08-06: tokenrouter/kimi-k3-free trickled
# keepalive bytes (resetting requests' per-recv read timeout on every byte)
# while never answering stream:false — one swarm stage was hostage 24+ min.
# --------------------------------------------------------------------------- #

def test_hung_hop_is_abandoned_at_the_deadline():
    import app as _app
    import time as _time

    def hanging_dispatch(pid, payload, stream):
        _time.sleep(30)            # the provider that never answers
        raise AssertionError("the test must have moved on long before this")

    orig = _app._dispatch_chat
    orig_deadline = _app._SWARM_HOP_DEADLINE
    try:
        _app._dispatch_chat = hanging_dispatch
        _app._SWARM_HOP_DEADLINE = 0.3
        t0 = _time.time()
        resp, exc = _app._dispatch_chat_with_deadline("pid", {"model": "m"},
                                                      _app._SWARM_HOP_DEADLINE)
        elapsed = _time.time() - t0
    finally:
        _app._dispatch_chat = orig
        _app._SWARM_HOP_DEADLINE = orig_deadline
    assert resp is None and exc is None
    assert elapsed < 5, "a hung hop must be abandoned at the deadline, not 300s+"


def test_healthy_hop_passes_through_the_deadline_wrapper():
    import app as _app

    class _Resp:
        status_code = 200

    def fast_dispatch(pid, payload, stream):
        return _Resp()

    orig = _app._dispatch_chat
    try:
        _app._dispatch_chat = fast_dispatch
        resp, exc = _app._dispatch_chat_with_deadline("pid", {"model": "m"}, 5)
    finally:
        _app._dispatch_chat = orig
    assert resp is not None and resp.status_code == 200 and exc is None


def test_raising_hop_reports_its_exception_without_raising():
    import app as _app

    def broken_dispatch(pid, payload, stream):
        raise RuntimeError("conn refused")

    orig = _app._dispatch_chat
    try:
        _app._dispatch_chat = broken_dispatch
        resp, exc = _app._dispatch_chat_with_deadline("pid", {"model": "m"}, 5)
    finally:
        _app._dispatch_chat = orig
    assert resp is None and isinstance(exc, RuntimeError)


def test_swarm_dispatch_walks_past_a_hung_hop_to_a_healthy_one():
    """End to end through _swarm_dispatch: first hop hangs, second answers —
    the stage must return the healthy hop's text, fast."""
    import app as _app
    import time as _time

    class _Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "stage answer"}}]}

        def close(self):
            pass

    def dispatch(pid, payload, stream):
        if pid == "hangpid":
            _time.sleep(30)
        return _Resp()

    orig = (_app._dispatch_chat, _app._route_by_difficulty,
            _app._build_chain, _app._SWARM_HOP_DEADLINE, _app._act_pick)
    try:
        _app._dispatch_chat = dispatch
        _app._route_by_difficulty = lambda *a, **k: ("hangpid", "m1", None)
        _app._build_chain = lambda pid, model, est: [("hangpid", "m1"),
                                                     ("goodpid", "m2")]
        _app._act_pick = lambda pid, model: None
        _app._SWARM_HOP_DEADLINE = 0.3
        t0 = _time.time()
        text, who = _app._swarm_dispatch([{"role": "user", "content": "hi"}], 100)
        elapsed = _time.time() - t0
    finally:
        (_app._dispatch_chat, _app._route_by_difficulty,
         _app._build_chain, _app._SWARM_HOP_DEADLINE, _app._act_pick) = orig
    assert text == "stage answer" and who == "goodpid/m2"
    assert elapsed < 5, "the chain must walk past the hung hop, not wait for it"


def test_truncated_hop_walks_the_chain_and_keeps_the_longest_partial():
    """finish_reason=length means the PROVIDER capped the completion — observed
    live: kilocode/hy3 cut a crew synthesis mid-attribute and shipped broken
    HTML. The stage must try the next hop; if every hop truncates, the longest
    partial is better than nothing."""
    import app as _app

    def make_resp(text, finish):
        class _Resp:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content": text},
                                     "finish_reason": finish}]}

            def close(self):
                pass
        return _Resp()

    # Case 1: first hop truncates, second completes -> the COMPLETE one wins.
    def dispatch_1(pid, payload, stream):
        return make_resp("cut off mid-sen", "length") if pid == "cutpid" \
            else make_resp("the full answer", "stop")

    orig = (_app._dispatch_chat, _app._route_by_difficulty,
            _app._build_chain, _app._act_pick, _app._record_chat_usage)
    try:
        _app._route_by_difficulty = lambda *a, **k: ("cutpid", "m1", None)
        _app._build_chain = lambda pid, model, est: [("cutpid", "m1"),
                                                     ("fullpid", "m2")]
        _app._act_pick = lambda pid, model: None
        _app._record_chat_usage = lambda *a, **k: None
        _app._dispatch_chat = dispatch_1
        text, who = _app._swarm_dispatch([{"role": "user", "content": "hi"}], 100)
        assert text == "the full answer" and who == "fullpid/m2"

        # Case 2: every hop truncates -> the LONGEST partial is returned.
        _app._build_chain = lambda pid, model, est: [("cutpid", "m1"),
                                                     ("fullpid", "m2")]
        _app._dispatch_chat = lambda pid, payload, stream: (
            make_resp("short", "length") if pid == "cutpid"
            else make_resp("a somewhat longer partial answer", "length"))
        text, who = _app._swarm_dispatch([{"role": "user", "content": "hi"}], 100)
        assert text == "a somewhat longer partial answer" and who == "fullpid/m2"
    finally:
        (_app._dispatch_chat, _app._route_by_difficulty,
         _app._build_chain, _app._act_pick, _app._record_chat_usage) = orig
