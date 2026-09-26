"""The subscription MANAGER inside the prose swarm and the crews.

The user's goal: a paid manager plans, briefs, checks and fixes while the free
models do the work, spending as few subscription tokens as possible. So these
tests pin down WHO is called for WHAT: the manager only ever plans, supervises,
reviews, judges a worker (verify) and fixes; phases, gap repairs and synthesis
stay on the free dispatch; "" from the manager (budget) falls back to free; and
manager=None is the old pipeline. No network: dispatch and manager are fakes.
"""
import json
import threading

from unittest import mock

import crews
import swarm

ASK = [{"role": "user", "content": "Build a landing page: hero, pricing, FAQ."}]

PLAN = json.dumps({"goal": "a landing page", "phases": [
    {"title": "Hero", "task": "write the hero", "needs": [],
     "acceptance": ['includes "Start free"'], "output_format": "markdown"},
    {"title": "Pricing", "task": "write the pricing table", "needs": [],
     "inputs": "three tiers: Free, Pro, Team", "constraints": ["no invented prices"]},
]})


def _stage(msgs):
    sys_ = msgs[0]["content"]
    for name, prompt in (("plan", swarm._PLAN_SYSTEM), ("phase", swarm._PHASE_SYSTEM),
                         ("supervise", swarm._SUPERVISE_SYSTEM),
                         ("review", swarm._REVIEW_SYSTEM), ("synth", swarm._SYNTH_SYSTEM),
                         ("verify", swarm._VERDICT_SYSTEM), ("fix", swarm._FIX_SYSTEM)):
        if sys_ == prompt:
            if name == "phase" and "A PREVIOUS ATTEMPT" in msgs[-1]["content"]:
                return "retry"
            return name
    return "?"


def _free(plan=PLAN, phase=None, supervise='{"missing": []}',
          review='{"verdict": "ship", "problems": []}', synth="FINAL"):
    """Free dispatch scripted by stage. `phase(user_text, n)` answers workers."""
    calls, lock = [], threading.Lock()

    def dispatch(messages, max_tokens, exclude_pids=()):
        st = _stage(messages)
        with lock:
            n = len(calls)
            calls.append({"stage": st, "user": messages[-1]["content"],
                          "exclude": tuple(exclude_pids), "max_tokens": max_tokens})
        if st in ("phase", "retry"):
            text = phase(messages[-1]["content"], st) if phase else \
                "Start free — output for this phase."
        else:
            text = {"plan": plan, "supervise": supervise, "review": review,
                    "synth": synth}.get(st, "")
        who = "free%d/model" % n
        return text, (who if text else None)

    dispatch.calls = calls
    dispatch.of = lambda st: [c for c in calls if c["stage"] == st]
    return dispatch


def _manager(answers=None, tokens=10):
    """Manager scripted by purpose; answers[purpose] is a str or a list
    consumed in order. Records (purpose, max_tokens, prompt chars)."""
    answers = dict(answers or {})
    calls, lock = [], threading.Lock()

    def manager(messages, max_tokens, purpose):
        with lock:
            calls.append({"purpose": purpose, "max_tokens": max_tokens,
                          "stage": _stage(messages),
                          "chars": sum(len(m["content"]) for m in messages),
                          "user": messages[-1]["content"]})
            a = answers.get(purpose, "")
            if isinstance(a, list):
                a = a.pop(0) if a else ""
        return a, ("sub-claude/sonnet" if a else None), (tokens if a else 0)

    manager.calls = calls
    manager.purposes = lambda: [c["purpose"] for c in calls]
    return manager


GOOD_MGR = {"plan": PLAN, "supervise": '{"missing": []}',
            "review": '{"verdict": "ship", "problems": []}',
            "verify": '{"ok": true, "problems": []}'}


# ---- 1. role-aware dispatch ------------------------------------------------

def test_manager_only_plans_supervises_reviews_and_verifies():
    d, m = _free(), _manager(GOOD_MGR)
    out = swarm.run(ASK, d, manager=m)
    assert set(m.purposes()) <= {"plan", "supervise", "review", "verify", "fix"}
    assert m.purposes().count("plan") == 1
    assert "supervise" in m.purposes() and "review" in m.purposes()
    assert m.purposes().count("verify") == 2          # one short verdict per phase
    # Free models did the work and never planned/supervised/reviewed.
    assert [c["stage"] for c in d.calls if c["stage"] in ("plan", "supervise", "review")] == []
    assert len(d.of("phase")) == 2 and len(d.of("synth")) == 1
    assert out["text"] == "FINAL"
    assert out["manager_tokens"] == 10 * len(m.calls)


def test_manager_calls_use_small_token_caps_and_clipped_views():
    # ~40K chars per phase, varied so the loop detector has nothing to flag.
    huge = " ".join("word%d" % i for i in range(5000))
    d, m = _free(phase=lambda u, st: 'Start free ' + huge), _manager(GOOD_MGR)
    swarm.run(ASK, d, manager=m)
    caps = {c["purpose"]: c["max_tokens"] for c in m.calls}
    assert caps["plan"] == swarm.MANAGER_PLAN_TOKENS
    assert caps["verify"] == swarm.MANAGER_VERDICT_TOKENS
    assert caps["review"] == swarm.MANAGER_REVIEW_TOKENS
    # Never a full transcript: every manager prompt is far below one phase.
    for c in m.calls:
        assert c["chars"] < 12000, (c["purpose"], c["chars"])
    # ...while the free reviewer fallback path would still get everything.
    assert any(len(c["user"]) > 40000 for c in d.of("synth"))


def test_manager_empty_reply_falls_back_to_free_models():
    d, m = _free(), _manager({})                          # budget spent: always ""
    out = swarm.run(ASK, d, manager=m)
    assert len(d.of("plan")) == 1 and len(d.of("supervise")) == 1
    assert len(d.of("review")) == 1
    assert out["text"] == "FINAL"
    assert out["manager_tokens"] == 0
    # An unreadable (empty) verdict passes the phase: no retry was paid for.
    assert d.of("retry") == []


def test_manager_none_is_the_plain_pipeline():
    d1, d2 = _free(), _free()
    a = swarm.run(ASK, d1)
    b = swarm.run(ASK, d2, manager=None)
    assert "manager_tokens" not in a and "manager_tokens" not in b
    assert sorted((c["stage"], c["user"]) for c in d1.calls) == \
        sorted((c["stage"], c["user"]) for c in d2.calls)
    assert a["text"] == b["text"]


# ---- 3. per-worker verification ------------------------------------------

def _phase_by_attempt(first, retry):
    def phase(user, st):
        if "Hero" not in user:
            return "Pricing: Free, Pro, Team."
        return retry if st == "retry" else first
    return phase


def test_mechanical_failure_retries_on_another_free_model():
    # First Hero attempt misses the quoted text the acceptance requires.
    d = _free(phase=_phase_by_attempt("A hero with no call to action.",
                                      "Start free today."))
    m = _manager(GOOD_MGR)
    out = swarm.run(ASK, d, manager=m)
    retries = d.of("retry")
    assert len(retries) == 1
    first_hero = [c for c in d.of("phase") if "Hero" in c["user"]][0]
    failing_pid = "free%d" % d.calls.index(first_hero)
    assert failing_pid in retries[0]["exclude"]           # a DIFFERENT free model
    assert "Start free" in retries[0]["user"]             # problems became instructions
    assert "fix" not in m.purposes()
    # The mechanical check caught it for free: only the retry needed a verdict.
    assert m.purposes().count("verify") == 2
    assert any(r.startswith("phase-retry:Hero") for r, _ in out["models"])


def test_two_failures_hand_the_phase_to_the_manager():
    d = _free(phase=_phase_by_attempt("no cta", "still no cta"))
    answers = dict(GOOD_MGR, fix="Start free — written by the manager.")
    m = _manager(answers)
    out = swarm.run(ASK, d, manager=m)
    assert len(d.of("retry")) == 1                        # exactly one free retry
    assert m.purposes().count("fix") == 1
    fix = [c for c in m.calls if c["purpose"] == "fix"][0]
    assert fix["max_tokens"] == swarm.MANAGER_FIX_TOKENS
    hero = [p for p in out["phases"] if p["title"] == "Hero"][0]
    assert "written by the manager" in hero["output"]
    assert ("fix:Hero", "sub-claude/sonnet") in out["models"]


def test_manager_verdict_rejection_retries_then_passes():
    d = _free()
    answers = dict(GOOD_MGR, verify=['{"ok": false, "problems": ["tone is wrong"]}',
                                     '{"ok": true}', '{"ok": true}'])
    m = _manager(answers)
    swarm.run(ASK, d, manager=m)
    assert len(d.of("retry")) == 1
    assert "tone is wrong" in d.of("retry")[0]["user"]
    assert "fix" not in m.purposes()


def test_degenerate_output_is_caught_by_answer_check_without_a_verdict():
    loop = "Start free now. " + "\n".join(["the same repeated line of text here"] * 40)
    d = _free(phase=_phase_by_attempt(loop, "Start free today."))
    m = _manager(GOOD_MGR)
    swarm.run(ASK, d, manager=m)
    assert len(d.of("retry")) == 1
    assert "degenerated" in d.of("retry")[0]["user"]


def test_fix_refused_keeps_the_last_real_attempt():
    d = _free(phase=_phase_by_attempt("no cta one", "no cta two"))
    m = _manager(GOOD_MGR)                                # no "fix" answer -> ""
    out = swarm.run(ASK, d, manager=m)
    hero = [p for p in out["phases"] if p["title"] == "Hero"][0]
    assert hero["output"] == "no cta two"


# ---- 5. unreadable review ---------------------------------------------------

def test_unreadable_review_is_reasked_then_shipped_with_a_warning():
    d = _free(review="I think it is fine overall.")
    m = _manager(dict(GOOD_MGR, review=["looks good to me", "still prose"]))
    out = swarm.run(ASK, d, manager=m)
    assert m.purposes().count("review") == 2
    assert "unreadable" in out["review_warning"]
    assert "Warning" in swarm.format_answer(out)


def test_reasked_review_that_parses_is_used():
    d = _free()
    m = _manager(dict(GOOD_MGR, review=[
        "prose", '{"verdict": "revise", "problems": ["FAQ missing"]}']))
    out = swarm.run(ASK, d, manager=m)
    assert out["review"]["problems"] == ["FAQ missing"]
    assert "review_warning" not in out


def test_without_manager_unreadable_review_still_counts_as_ship():
    d = _free(review="not json at all")
    out = swarm.run(ASK, d)
    assert len(d.of("review")) == 1 and "review_warning" not in out


# ---- 2. clear worker briefs ------------------------------------------------

def test_brief_fields_are_kept_and_rendered_for_the_worker():
    phases = swarm._clean_phases(json.loads(PLAN))
    assert phases[0]["acceptance"] == ['includes "Start free"']
    assert phases[1]["inputs"] == "three tiers: Free, Pro, Team"
    assert phases[1]["constraints"] == ["no invented prices"]
    d = _free()
    swarm.run(ASK, d)
    users = {("Hero" if "Hero" in c["user"] else "Pricing"): c["user"]
             for c in d.of("phase")}
    assert "Acceptance criteria" in users["Hero"] and '"Start free"' in users["Hero"]
    assert "Output format: markdown" in users["Hero"]
    assert "Inputs to use:\nthree tiers" in users["Pricing"]
    assert "Constraints:\n- no invented prices" in users["Pricing"]


def test_plan_without_brief_fields_renders_nothing_extra():
    bare = json.dumps({"goal": "g", "phases": [{"title": "A", "task": "a"},
                                               {"title": "B", "task": "b"}]})
    phases = swarm._clean_phases(json.loads(bare))
    assert set(phases[0]) == {"title", "task", "done_when", "needs"}
    assert swarm._render_brief(phases[0]) == ""


def test_planner_prompt_asks_for_the_brief_fields():
    for field in ("inputs", "constraints", "output_format", "acceptance"):
        assert '"%s"' % field in swarm._PLAN_SYSTEM


def test_mechanical_checks_are_conservative():
    ph = {"acceptance": ['includes "Pricing"', "at least 5 words", "is friendly"],
          "output_format": "JSON object"}
    probs = swarm._mechanical_problems(ph, "hello")
    assert any("Pricing" in p for p in probs)
    assert any("words" in p for p in probs)
    assert any("JSON" in p for p in probs)
    assert swarm._mechanical_problems(ph, '{"a": "Pricing is here for you all"}') == []
    # A criterion with no mechanical reading is left alone.
    assert swarm._mechanical_problems({"acceptance": ["is friendly"]}, "x") == []


# ---- plumbing: crews + app ---------------------------------------------------

def test_crews_forwards_the_manager_only_when_given():
    seen = []

    def fake_run(messages, dispatch, profile=None, on_event=None, **kw):
        seen.append(kw)
        return {"text": "t"}
    with mock.patch.object(crews.swarm, "run", side_effect=fake_run):
        crews.run(ASK, lambda *a, **k: ("", None), "crew-code")
        mgr = lambda *a: ("", None)                       # noqa: E731
        crews.run(ASK, lambda *a, **k: ("", None), "crew-code", manager=mgr)
    assert "manager" not in seen[0]
    assert seen[1]["manager"] is mgr


def test_app_passes_a_manager_only_when_enabled():
    import app
    with mock.patch.object(app, "_manager_enabled", return_value=False):
        assert app._swarm_manager_kwargs() == {}
    with mock.patch.object(app, "_manager_enabled", return_value=True):
        assert callable(app._swarm_manager_kwargs()["manager"])


def test_app_manager_adapter_reports_charged_tokens():
    import app

    def fake_dispatch(messages, max_tokens=None, purpose="other"):
        assert purpose == "swarm:verify"
        app._MANAGER_LAST.tokens = 123
        return '{"ok": true}', "sub-claude/sonnet"
    with mock.patch.object(app, "_manager_dispatch", side_effect=fake_dispatch):
        assert app._swarm_manager([{"role": "user", "content": "x"}], 300, "verify") == \
            ('{"ok": true}', "sub-claude/sonnet", 123)
    with mock.patch.object(app, "_manager_dispatch", side_effect=RuntimeError):
        assert app._swarm_manager([], 300, "plan") == ("", None, 0)
