"""Pipelines verify and search (2026-10-04, owner-approved design adapted from
Sakana's Trinity / Conductor / AB-MCTS, no training).

  1. The prose swarm's reviewer avoids the FAMILIES that produced the work
     (injected `family`, a hint `avoid_families` the dispatch honours).
  2. The write/design crews run ONE revision when the reviewer marks a problem
     HIGH severity; code/research unchanged.
  3. Multi without a manager: an injected FREE verdict for a phase no observed
     check settled and whose outcome is risky/claimed; not-ok HIGH = the one
     revision. Observed evidence short-circuits it; None = as before.
  4. Wider vs deeper (AB-MCTS lite): only where a scorer exists, Thompson
     sampling on per-run Beta posteriors, at most 2 extra attempts, every
     choice + outcome recorded (and persisted for Multi).

No network: every model call is a stub.
"""
import json
import re
import threading
import time

import pytest

import crews
import evidence as E
import receipts
import swarm
import swarm_windows as SW


# --------------------------------------------------------------------------- #
# Stubs
# --------------------------------------------------------------------------- #

class Pick:
    """A scripted Thompson draw: `choice` always wins (betavariate is drawn
    in SEARCH_CHOICES order, wider then deeper, under the policy's lock)."""

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


def make_dispatch(answer, sig="plain"):
    """A dispatch scripted by `answer(stage, title, messages, kw, n)`. `sig`:
    "plain" (exclude_pids only -- what every existing caller has), "avoid"
    (names avoid_families), "kwargs" (takes **kw, like app's wrapper)."""
    calls = []
    lock = threading.Lock()

    def _record(messages, kw):
        stage, title = _stage(messages)
        with lock:
            n = len(calls)
            calls.append({"stage": stage, "title": title, "user": messages[-1]["content"],
                          "kw": dict(kw)})
        return answer(stage, title, messages, kw, n)

    if sig == "avoid":
        def dispatch(messages, max_tokens, exclude_pids=(), avoid_families=()):
            return _record(messages, {"exclude_pids": exclude_pids,
                                      "avoid_families": avoid_families})
    elif sig == "kwargs":
        def dispatch(messages, max_tokens, **kw):
            return _record(messages, kw)
    else:
        def dispatch(messages, max_tokens, exclude_pids=()):
            return _record(messages, {"exclude_pids": exclude_pids})
    dispatch.calls = calls
    dispatch.of = lambda stage, title=None: [c for c in calls if c["stage"] == stage
                                             and (title is None or c["title"] == title)]
    return dispatch


PLAN = json.dumps({"goal": "Build the parser", "phases": [
    {"title": "Alpha", "task": "write the parser", "acceptance": ['include "ALPHA-TOKEN"']},
    {"title": "Beta", "task": "write the usage notes"}]})
GOOD_A = "ALPHA-TOKEN: the parser reads the CSV file and returns every row as a dict."
BAD_A = "The parser reads the CSV file and returns every row as a dictionary object."
GOOD_B = "The usage notes explain how to call the parser and what it returns."
SHIP = json.dumps({"verdict": "ship", "problems": []})
NO_GAPS = json.dumps({"missing": []})
ASK = [{"role": "user", "content": "build me a CSV parser with usage notes"}]
PROVIDER = {"Alpha": "p1/kimi-k3", "Beta": "p2/kimi-k2.6"}


def scripted(alpha=(GOOD_A,), review=SHIP, reviewer=None):
    """Alpha answers from `alpha` in order (the last one repeats); Beta is
    always good. The reviewer is llama when "kimi" is to be avoided, else
    kimi (or `reviewer(kw)`)."""
    alpha = list(alpha)
    lock = threading.Lock()
    served = {"n": 0}

    def answer(stage, title, messages, kw, n):
        if stage == "plan":
            return PLAN, "planner/glm-5"
        if stage == "phase":
            if title == "Alpha":
                with lock:
                    k = served["n"]
                    served["n"] += 1
                return alpha[min(k, len(alpha) - 1)], "p%d/kimi-k3" % (k + 1)
            return GOOD_B, PROVIDER["Beta"]
        if stage == "supervise":
            return NO_GAPS, "sup/qwen3"
        if stage == "review":
            if reviewer:
                return review, reviewer(kw)
            avoid = kw.get("avoid_families") or ()
            return review, ("p9/llama-4" if "kimi" in avoid else "p1/kimi-k3")
        if stage == "synth":
            return "FINAL", "syn/deepseek-v4"
        return "", None
    return answer


# --------------------------------------------------------------------------- #
# 1. The reviewer avoids the producers' families
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("mid,fam", [
    ("moonshotai/kimi-k3", "kimi"), ("groq/llama-3.3-70b", "llama"),
    ("@cf/qwen/qwen3-coder", "qwen"), ("p1/kimi-k2.6", "kimi"),
    ("deepseek-v4", "deepseek"), ("", ""), (None, ""), ("org/123b", "")])
def test_the_default_family_is_the_last_segments_leading_letters(mid, fam):
    assert swarm.default_family(mid) == fam


def test_a_dispatch_that_names_the_hint_gets_the_producers_families():
    d = make_dispatch(scripted(), sig="avoid")
    out = swarm.run(ASK, d)
    review = d.of("review")[0]
    assert review["kw"]["avoid_families"] == ("kimi",)
    assert set(review["kw"]["exclude_pids"]) == {"p1", "p2"}, "provider exclusion kept"
    assert out["review_family"] == {"producers": ["kimi"], "reviewer": "llama",
                                    "distinct": True, "hinted": True}


def test_a_dispatch_that_does_not_know_the_hint_never_receives_it():
    """Every dispatch in use today (and app's **kw wrapper around one that
    would reject it) must keep working: no keyword it does not name."""
    d = make_dispatch(scripted(), sig="plain")
    out = swarm.run(ASK, d)
    assert out["text"] == "FINAL"
    assert "avoid_families" not in d.of("review")[0]["kw"]
    assert out["review_family"]["hinted"] is False
    assert out["review_family"]["distinct"] is False      # it could not be steered

    k = make_dispatch(scripted(), sig="kwargs")
    swarm.run(ASK, k)
    assert "avoid_families" not in k.of("review")[0]["kw"], "a bare **kw is not opt-in"


def test_an_injected_family_function_is_used_and_opts_in():
    seen = []

    def family(who):
        seen.append(who)
        return "moonshot" if "kimi" in who else who.split("/")[-1].split("-")[0]
    d = make_dispatch(scripted(reviewer=lambda kw: "p9/llama-4"), sig="kwargs")
    out = swarm.run(ASK, d, family=family)
    assert d.of("review")[0]["kw"]["avoid_families"] == ("moonshot",)
    assert out["review_family"] == {"producers": ["moonshot"], "reviewer": "llama",
                                    "distinct": True, "hinted": True}
    assert "p1/kimi-k3" in seen, "family() is called with the dispatch's pid/model"


def test_a_family_function_that_raises_costs_nothing():
    def family(who):
        raise RuntimeError("boom")
    d = make_dispatch(scripted(), sig="kwargs")
    out = swarm.run(ASK, d, family=family)
    assert out["text"] == "FINAL"
    assert "avoid_families" not in d.of("review")[0]["kw"]   # no family known: no hint
    assert out["review_family"]["producers"] == []


def test_a_manager_review_is_not_a_free_reviewer():
    def manager(msgs, max_tokens, purpose):
        return (SHIP, "sub-claude/sonnet") if purpose == "review" else ("", None)
    d = make_dispatch(scripted(), sig="avoid")
    out = swarm.run(ASK, d, manager=manager)
    assert not d.of("review"), "the manager reviewed"
    assert "review_family" not in out


# --------------------------------------------------------------------------- #
# 2. Write/design crews: one revision on a HIGH-severity problem
# --------------------------------------------------------------------------- #

CREW_PLAN = json.dumps({"goal": "G", "phases": [
    {"title": "Part A", "task": "do part A", "done_when": "A exists"},
    {"title": "Part B", "task": "do part B", "done_when": "B exists"}]})
REVISED = "## Part A\nREVISED-DRAFT of part A\n\n## Part B\ndraft B"


def _crew_dispatch(review):
    def answer(stage, title, messages, kw, n):
        return {"plan": (CREW_PLAN, "pl/glm"), "supervise": (NO_GAPS, "s/q"),
                "review": (review, "r/llama"), "synth": ("FINAL", "y/ds"),
                "revision": (REVISED, "v/qwen"), "instruct": ("", None)}.get(
            stage, ({"Part A": "draft A", "Part B": "draft B"}.get(title, ""), "w/kimi")
            if stage == "phase" else ("", None))
    return make_dispatch(answer)


def _revise(severity=None, problems=("part A is wrong",)):
    body = {"verdict": "revise", "problems": list(problems)}
    if severity:
        body["severity"] = severity
    return json.dumps(body)


@pytest.mark.parametrize("crew", ["write", "design"])
def test_write_and_design_revise_once_on_a_high_severity_problem(crew):
    d = _crew_dispatch(_revise("high"))
    out = crews.run(ASK, d, crew)
    assert len(d.of("revision")) == 1, "a high-severity problem got no revision"
    assert "REVISED-DRAFT" in d.of("synth")[0]["user"]
    assert out["text"] == "FINAL"


@pytest.mark.parametrize("crew", ["write", "design"])
@pytest.mark.parametrize("severity", ["medium", "low", None])
def test_write_and_design_do_not_revise_below_high(crew, severity):
    d = _crew_dispatch(_revise(severity))
    crews.run(ASK, d, crew)
    assert not d.of("revision"), "polish re-rolled the prose"
    assert "part A is wrong" in d.of("synth")[0]["user"], "problems still reach synthesis"


@pytest.mark.parametrize("crew", ["code", "research"])
def test_code_and_research_are_unchanged(crew):
    assert "revise_on" not in crews.CREWS[crew]
    d = _crew_dispatch(_revise("low"))
    crews.run(ASK, d, crew)
    assert len(d.of("revision")) == 1, "max_revisions 1 still revises any revise verdict"


def test_the_write_and_design_reviewers_are_asked_for_a_severity():
    for crew in ("write", "design"):
        prof = crews.CREWS[crew]
        assert prof["revise_on"] == "high" and prof["max_revisions"] == 0
        assert '"severity": "high" | "medium" | "low"' in prof["review_system"]


def test_a_ship_verdict_marked_high_never_revises():
    d = _crew_dispatch(json.dumps({"verdict": "ship", "problems": [], "severity": "high"}))
    crews.run(ASK, d, "write")
    assert not d.of("revision")


@pytest.mark.parametrize("review,sev", [
    ({"severity": "HIGH"}, "high"),
    ({"problems": [{"problem": "x", "severity": "critical"}]}, "high"),
    ({"problems": ["[high] the hero section is missing"]}, "high"),
    ({"problems": ["Medium: spacing"], "severity": "low"}, "medium"),
    ({"problems": ["minor: a typo"]}, "low"),
    ({"problems": ["no tag here"]}, ""),
    ("not a dict", ""),
])
def test_review_severity_reads_every_shape(review, sev):
    assert swarm.review_severity(review) == sev


def test_review_problems_are_plain_text_whatever_the_shape():
    assert swarm.review_problems({"problems": [
        "[HIGH] the hero is missing", {"problem": "nav overlaps", "severity": "high"},
        {"text": "typo"}, "", None]}) == ["the hero is missing", "nav overlaps", "typo"]
    assert swarm.review_problems({"problems": "one problem"}) == ["one problem"]


def test_crews_forward_family_and_search_only_when_given(monkeypatch):
    got = []
    monkeypatch.setattr(crews.swarm, "run", lambda *a, **k: got.append(k) or {"text": "x"})
    fam = swarm.default_family
    crews.run(ASK, None, "write", family=fam, search=True)
    crews.run(ASK, None, "write")
    assert got[0]["family"] is fam and got[0]["search"] is True
    assert "family" not in got[1] and "search" not in got[1]


# --------------------------------------------------------------------------- #
# 4a. Wider vs deeper in the prose swarm (a scorer: the free checks)
# --------------------------------------------------------------------------- #

def test_the_search_policy_is_thompson_on_beta_posteriors():
    s = swarm.Search(rng=Pick("deeper"))
    assert s.post == {"wider": [1, 1], "deeper": [1, 1]}
    assert s.choose() == "deeper"
    s.note("deeper", True, phase=1)
    s.note("wider", False, phase=1)
    snap = s.snapshot()
    assert snap["posteriors"] == {"wider": [1, 2], "deeper": [2, 1]}
    assert [r["choice"] for r in snap["log"]] == ["deeper", "wider"]
    back = swarm.Search(log=snap["log"])
    assert back.post == snap["posteriors"], "a persisted log re-derives the posteriors"
    real = swarm.Search()
    assert real.choose() in swarm.SEARCH_CHOICES
    assert swarm.make_search(None) is None and swarm.make_search(False) is None
    assert isinstance(swarm.make_search(True), swarm.Search)
    assert swarm.make_search(s) is s
    assert swarm.Search(max_extra=9).max_extra == swarm.SEARCH_MAX_EXTRA


def test_no_scorer_nothing_extra_runs():
    d = make_dispatch(scripted(alpha=(GOOD_A,)))
    out = swarm.run(ASK, d, search=swarm.Search(rng=Pick("wider")))
    assert len(d.of("phase", "Alpha")) == 1 and len(d.of("phase", "Beta")) == 1
    assert out["search"] == {"posteriors": {"wider": [1, 1], "deeper": [1, 1]}, "log": []}


def test_search_off_by_default_changes_nothing():
    d = make_dispatch(scripted(alpha=(BAD_A,)))
    out = swarm.run(ASK, d)
    assert len(d.of("phase", "Alpha")) == 1, "no manager, no search: unchecked as before"
    assert "search" not in out


def test_at_most_two_extra_attempts_each_recorded():
    d = make_dispatch(scripted(alpha=(BAD_A,)))
    out = swarm.run(ASK, d, search=swarm.Search(rng=Pick("wider")))
    assert len(d.of("phase", "Alpha")) == 1 + swarm.SEARCH_MAX_EXTRA
    log = out["search"]["log"]
    assert [(r["phase"], r["attempt"], r["choice"], r["success"], r["accepted"])
            for r in log] == [(1, 1, "wider", False, False), (1, 2, "wider", False, False)]
    assert all(r["score_before"] == 1 and r["score_after"] == 1 for r in log)
    assert out["search"]["posteriors"] == {"wider": [1, 3], "deeper": [1, 1]}
    assert {"phase", "title", "attempt", "choice", "success", "score_before",
            "score_after", "accepted", "at"} <= set(log[0])


def test_wider_starts_fresh_on_another_provider_deeper_refines_the_best():
    d = make_dispatch(scripted(alpha=(BAD_A,)))
    swarm.run(ASK, d, search=swarm.Search(rng=Pick("wider")))
    wide = d.of("phase", "Alpha")[1]
    assert "REJECTED" not in wide["user"], "wider is the phase from scratch"
    assert "p1" in wide["kw"]["exclude_pids"], "the provider that failed is excluded"

    d2 = make_dispatch(scripted(alpha=(BAD_A,)))
    swarm.run(ASK, d2, search=swarm.Search(rng=Pick("deeper")))
    deep = d2.of("phase", "Alpha")[1]
    assert "A PREVIOUS ATTEMPT AT THIS PHASE WAS REJECTED" in deep["user"]
    assert BAD_A in deep["user"], "deeper is shown the best attempt to refine"
    assert 'missing required text "ALPHA-TOKEN"' in deep["user"]
    assert deep["kw"]["exclude_pids"] == ()


def test_an_accepted_attempt_stops_the_search_and_ships():
    d = make_dispatch(scripted(alpha=(BAD_A, GOOD_A)))
    out = swarm.run(ASK, d, search=swarm.Search(rng=Pick("wider")))
    assert len(d.of("phase", "Alpha")) == 2
    (rec,) = out["search"]["log"]
    assert (rec["success"], rec["accepted"], rec["score_after"]) == (True, True, 0)
    assert "ALPHA-TOKEN" in d.of("synth")[0]["user"]
    assert any(role == "phase-wider:Alpha" for role, _w in out["models"])


def test_the_best_attempt_is_never_replaced_by_a_worse_one():
    plan = json.dumps({"goal": "G", "phases": [
        {"title": "Alpha", "task": "write the parser",
         "acceptance": ['include "ALPHA-TOKEN"', 'include "OMEGA-TOKEN"']},
        {"title": "Beta", "task": "write the usage notes"}]})
    base = scripted(alpha=(GOOD_A, BAD_A))       # 1 problem, then 2 problems

    def answer(stage, title, messages, kw, n):
        return (plan, "planner/glm") if stage == "plan" else base(stage, title, messages, kw, n)
    d = make_dispatch(answer)
    out = swarm.run(ASK, d, search=swarm.Search(rng=Pick("deeper")))
    assert [r["score_after"] for r in out["search"]["log"]] == [2, 2]
    assert GOOD_A in d.of("synth")[0]["user"] and BAD_A not in d.of("synth")[0]["user"]


def test_past_the_first_extra_attempt_the_clock_must_allow(monkeypatch):
    monkeypatch.setattr(swarm, "SEARCH_MIN_SECONDS", 10 ** 6)
    d = make_dispatch(scripted(alpha=(BAD_A,)))
    out = swarm.run(ASK, d, max_seconds=600, search=swarm.Search(rng=Pick("wider")))
    assert len(out["search"]["log"]) == 1, "the first stands in for today's retry"


def test_with_a_manager_the_free_checks_search_before_the_verdict():
    def manager(msgs, max_tokens, purpose):
        return "", None
    d = make_dispatch(scripted(alpha=(BAD_A,)))
    out = swarm.run(ASK, d, manager=manager, search=swarm.Search(rng=Pick("deeper")))
    log = out["search"]["log"]
    assert [(r["choice"], r["attempt"]) for r in log] == [("deeper", 1), ("deeper", 2)]

    d0 = make_dispatch(scripted(alpha=(BAD_A,)))
    swarm.run(ASK, d0, manager=manager)
    assert len(d.of("phase", "Alpha")) == len(d0.of("phase", "Alpha")) + 1, \
        "one attempt more than today's retry, then the same fix path"


# --------------------------------------------------------------------------- #
# Multi fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def runs(tmp_path, monkeypatch):
    monkeypatch.setenv(SW._STORE_ENV, str(tmp_path / "runs"))
    monkeypatch.setattr(SW, "SPAWN_STAGGER", 0)
    monkeypatch.setattr(SW, "RETRY_BACKOFF", 0)
    monkeypatch.setattr(receipts, "root", lambda: str(tmp_path / "state" / "receipts"))
    SW._RUNS.clear()
    yield tmp_path
    for run in list(SW._RUNS.values()):
        run.stop_flag.set()
    SW._RUNS.clear()


def _wait(run_id, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        st = SW.status(run_id)
        if st and st["state"] in (SW.DONE, SW.FAILED, SW.STOPPED):
            return st
        time.sleep(0.02)
    return SW.status(run_id)


PYTEST_FAIL = "FAILED tests/test_app.py::test_add\n2 failed, 11 passed in 0.42s\n"
PYTEST_PASS = "13 passed in 0.40s\n"
PHASE = [{"title": "Build", "task": "write app.py and its tests", "needs": []}]


class _World:
    def __init__(self, scripts, on_turn=None):
        self.scripts = scripts
        self.prompts = []
        self.sids = []
        self.n = 0
        self.on_turn = on_turn

    def spawn(self, cli, project):
        self.n += 1
        return "w%d" % self.n

    def run_turn(self, sid, prompt):
        self.prompts.append(prompt)
        self.sids.append(sid)
        if self.on_turn:
            self.on_turn(sid, prompt)
        title = prompt.split("Your phase is called: ", 1)[1].splitlines()[0]
        queue = self.scripts.get(title) or []
        evs = queue.pop(0) if queue else [{"event": "message", "text": "did " + title}]
        for e in evs:
            yield e


def _attempt(code, out, summary):
    return [{"event": "tool", "text": "Bash: pytest -q"},
            {"event": "tool_result", "command": "pytest -q", "exit_code": code,
             "is_error": code != 0, "output_tail": out},
            {"event": "message", "text": summary}]


def _msg(text):
    return [{"event": "message", "text": text}]


class _Verdicts:
    def __init__(self, reply=None, exc=None):
        self.reply = reply
        self.exc = exc
        self.briefs = []

    def __call__(self, brief):
        self.briefs.append(brief)
        if self.exc:
            raise self.exc
        return self.reply


# --------------------------------------------------------------------------- #
# 3. Multi: a FREE verdict without a manager
# --------------------------------------------------------------------------- #

CLAIM = "Wrote app.py. All 12 tests pass."


def test_an_ok_free_verdict_passes_the_phase(runs):
    fv = _Verdicts({"ok": True, "problems": [], "severity": ""})
    w = _World({"Build": [_msg(CLAIM)]})
    rid = SW.start("app", str(runs), "claude", w.spawn, w.run_turn, phases=PHASE,
                   review=False, free_verdict=fv)
    a = _wait(rid)["agents"][0]
    assert (a["state"], a["revisions"]) == (SW.DONE, 0)
    assert len(fv.briefs) == 1
    brief = fv.briefs[0]
    assert brief["reason"] == "claims checks pass, none observed"
    assert brief["title"] == "Build" and brief["summary"] == CLAIM
    assert "The agent's final message:" in brief["text"] and CLAIM in brief["text"]
    assert a["free_check"]["ok"] is True
    assert a["claimed_not_observed"] is True, "the label stays honest"


def test_a_not_ok_high_free_verdict_gets_the_one_revision(runs):
    fv = _Verdicts({"ok": False, "problems": ["app.py never handles empty input"],
                    "severity": "high"})
    w = _World({"Build": [_msg(CLAIM), _msg("Handled empty input.")]})
    rid = SW.start("app", str(runs), "claude", w.spawn, w.run_turn, phases=PHASE,
                   review=False, free_verdict=fv)
    a = _wait(rid)["agents"][0]
    assert (a["state"], a["revisions"]) == (SW.DONE, 1)
    assert len(fv.briefs) == 1, "at most one free verdict per phase"
    assert "WAS CHECKED AND REJECTED" in w.prompts[1]
    assert "- app.py never handles empty input" in w.prompts[1]
    assert a["free_check"] == {"asked": True, "answered": True, "ok": False,
                               "severity": "high",
                               "problems": ["app.py never handles empty input"],
                               "reason": "claims checks pass, none observed"}


@pytest.mark.parametrize("severity", ["medium", "low", ""])
def test_a_not_ok_verdict_below_high_changes_nothing(runs, severity):
    fv = _Verdicts({"ok": False, "problems": ["could be tidier"], "severity": severity})
    w = _World({"Build": [_msg(CLAIM)]})
    rid = SW.start("app", str(runs), "claude", w.spawn, w.run_turn, phases=PHASE,
                   review=False, free_verdict=fv)
    a = _wait(rid)["agents"][0]
    assert (a["state"], a["revisions"]) == (SW.DONE, 0)


def test_observed_evidence_short_circuits_the_free_verdict(runs):
    fv = _Verdicts({"ok": False, "problems": ["x"], "severity": "high"})
    w = _World({"Build": [_attempt(0, PYTEST_PASS, CLAIM)]})
    rid = SW.start("app", str(runs), "claude", w.spawn, w.run_turn, phases=PHASE,
                   review=False, free_verdict=fv)
    a = _wait(rid)["agents"][0]
    assert (a["state"], a["verified"], a["revisions"]) == (SW.DONE, True, 0)
    assert fv.briefs == [], "an observed PASS decides: ok"

    fv2 = _Verdicts({"ok": False, "problems": ["x"], "severity": "high"})
    w2 = _World({"Build": [_attempt(1, PYTEST_FAIL, CLAIM), _attempt(0, PYTEST_PASS, "Fixed.")]})
    rid2 = SW.start("app", str(runs), "claude", w2.spawn, w2.run_turn, phases=PHASE,
                    review=False, free_verdict=fv2)
    a2 = _wait(rid2)["agents"][0]
    assert (a2["state"], a2["verified"], a2["revisions"]) == (SW.DONE, True, 1)
    assert fv2.briefs == [], "an observed FAIL takes the existing revision"


def test_no_free_verifier_is_the_run_as_before(runs):
    w = _World({"Build": [_msg(CLAIM)]})
    rid = SW.start("app", str(runs), "claude", w.spawn, w.run_turn, phases=PHASE, review=False)
    a = _wait(rid)["agents"][0]
    assert (a["state"], a["revisions"], a["free_check"]) == (SW.DONE, 0, None)
    assert SW.get(rid).free_verdict is None


def test_a_phase_with_nothing_risky_is_not_asked(runs):
    fv = _Verdicts({"ok": False, "problems": ["x"], "severity": "high"})
    w = _World({"Build": [_msg("Wrote the README section.")]})
    rid = SW.start("app", str(runs), "claude", w.spawn, w.run_turn, phases=PHASE,
                   review=False, free_verdict=fv)
    a = _wait(rid)["agents"][0]
    assert (a["state"], a["revisions"], a["free_check"]) == (SW.DONE, 0, None)
    assert fv.briefs == []


def test_changed_source_files_with_no_check_are_risky(runs):
    proj = runs / "proj"
    proj.mkdir()
    fv = _Verdicts({"ok": True})
    w = _World({"Build": [_msg("Wrote app.py.")]},
               on_turn=lambda sid, prompt: (proj / "app.py").write_text("x = 1\n"))
    rid = SW.start("app", str(proj), "claude", w.spawn, w.run_turn, phases=PHASE,
                   review=False, free_verdict=fv)
    _wait(rid)
    assert [b["reason"] for b in fv.briefs] == ["changed source files, no check ran"]
    assert fv.briefs[0]["changed_files"] == ["app.py"]


def test_a_free_verifier_that_raises_or_says_nothing_loses_nothing(runs):
    for fv in (_Verdicts(exc=RuntimeError("down")), _Verdicts(None)):
        w = _World({"Build": [_msg(CLAIM)]})
        rid = SW.start("app", str(runs), "claude", w.spawn, w.run_turn, phases=PHASE,
                       review=False, free_verdict=fv)
        a = _wait(rid)["agents"][0]
        assert (a["state"], a["revisions"]) == (SW.DONE, 0)
        assert a["free_check"] == {"asked": True, "answered": False,
                                   "reason": "claims checks pass, none observed"}


def test_with_a_manager_the_free_verifier_is_not_used(runs):
    fv = _Verdicts({"ok": False, "problems": ["x"], "severity": "high"})

    def manager(system, user, purpose, max_tokens):
        return (json.dumps({"ok": True}), 10) if purpose == "verify" else ("", 0)
    w = _World({"Build": [_msg(CLAIM)]})
    rid = SW.start("app", str(runs), "claude", w.spawn, w.run_turn, phases=PHASE,
                   review=False, manager=manager, free_verdict=fv)
    a = _wait(rid)["agents"][0]
    assert (a["state"], a["reviewed"]) == (SW.DONE, True)
    assert fv.briefs == []


def test_the_free_check_survives_a_restart(runs):
    fv = _Verdicts({"ok": True, "severity": "low"})
    w = _World({"Build": [_msg(CLAIM)]})
    rid = SW.start("app", str(runs), "claude", w.spawn, w.run_turn, phases=PHASE,
                   review=False, free_verdict=fv)
    _wait(rid)
    SW._RUNS.clear()
    assert SW.load() >= 1
    assert SW.status(rid)["agents"][0]["free_check"]["ok"] is True


# --------------------------------------------------------------------------- #
# 4b. Wider vs deeper in Multi (the scorer: observed test counts)
# --------------------------------------------------------------------------- #

def test_deeper_continues_the_same_session(runs):
    w = _World({"Build": [_attempt(1, PYTEST_FAIL, "Done."), _attempt(0, PYTEST_PASS, "Fixed.")]})
    rid = SW.start("app", str(runs), "claude", w.spawn, w.run_turn, phases=PHASE,
                   review=False, search=swarm.Search(rng=Pick("deeper")))
    st = _wait(rid)
    a = st["agents"][0]
    assert (a["state"], a["revisions"], a["verified"]) == (SW.DONE, 1, True)
    assert w.n == 1 and w.sids == ["w1", "w1"], "the same worker continued"
    assert "WAS CHECKED AND REJECTED" in w.prompts[1]
    (rec,) = st["search"]["log"]
    assert (rec["choice"], rec["success"], rec["accepted"], rec["score_before"],
            rec["score_after"], rec["phase"]) == ("deeper", True, True, 2, 0, 1)
    assert st["search"]["posteriors"]["deeper"] == [2, 1]


def test_wider_is_a_fresh_session_steered_off_its_earlier_model(runs):
    seen = {}

    def on_turn(sid, prompt):
        if sid == "w2":
            seen["during"] = SW.sibling_sessions("w2")
    w = _World({"Build": [_attempt(1, PYTEST_FAIL, "Done."), _attempt(0, PYTEST_PASS, "Fixed.")]},
               on_turn=on_turn)
    rid = SW.start("app", str(runs), "claude", w.spawn, w.run_turn, phases=PHASE,
                   review=False, search=swarm.Search(rng=Pick("wider")))
    st = _wait(rid)
    assert st["agents"][0]["state"] == SW.DONE
    assert w.sids == ["w1", "w2"], "a fresh session"
    assert seen["during"] == ["w1"], "the hub's rotation is told the earlier session"
    assert SW.sibling_sessions("w2") == [], "only while the wider attempt runs"
    assert st["search"]["log"][0]["choice"] == "wider"


def test_search_caps_at_two_extra_attempts_without_it_one(runs):
    fails = [_attempt(1, PYTEST_FAIL, "Done.") for _ in range(4)]
    w = _World({"Build": list(fails)})
    rid = SW.start("app", str(runs), "claude", w.spawn, w.run_turn, phases=PHASE,
                   review=False, search=swarm.Search(rng=Pick("deeper")))
    st = _wait(rid)
    a = st["agents"][0]
    assert (a["state"], a["revisions"], a["verified"]) == (SW.FAILED, 2, False)
    assert [r["attempt"] for r in st["search"]["log"]] == [1, 2]
    assert len(w.prompts) == 3

    w0 = _World({"Build": list(fails)})
    rid0 = SW.start("app", str(runs), "claude", w0.spawn, w0.run_turn, phases=PHASE,
                    review=False)
    a0 = _wait(rid0)["agents"][0]
    assert (a0["state"], a0["revisions"]) == (SW.FAILED, 1)
    assert SW.status(rid0)["search"] is None


def test_the_second_extra_attempt_needs_the_phase_budget(runs, monkeypatch):
    monkeypatch.setattr(SW, "_search_time_ok", lambda agent, last: False)
    w = _World({"Build": [_attempt(1, PYTEST_FAIL, "Done.") for _ in range(3)]})
    rid = SW.start("app", str(runs), "claude", w.spawn, w.run_turn, phases=PHASE,
                   review=False, search=swarm.Search(rng=Pick("deeper")))
    a = _wait(rid)["agents"][0]
    assert (a["state"], a["revisions"]) == (SW.FAILED, 1), "today's one revision still runs"


def test_search_time_ok_measures_against_the_phase_timeout(monkeypatch):
    monkeypatch.setattr(SW, "AGENT_TIMEOUT", 150.0)
    agent = SW._Agent(1, {"title": "t", "task": "x"})
    agent.started_at = time.time() - 100
    assert SW._search_time_ok(agent, 10) is True
    assert SW._search_time_ok(agent, 60) is False


def test_no_observed_failure_no_search(runs):
    def manager(system, user, purpose, max_tokens):
        return (json.dumps({"ok": False, "problems": ["wrong"]}), 10) \
            if purpose == "verify" else ("", 0)
    w = _World({"Build": [_msg("Wrote app.py."), _msg("Wrote app.py again.")]})
    rid = SW.start("app", str(runs), "claude", w.spawn, w.run_turn, phases=PHASE,
                   review=False, manager=manager, search=swarm.Search(rng=Pick("deeper")))
    st = _wait(rid)
    a = st["agents"][0]
    assert (a["state"], a["revisions"]) == (SW.FAILED, 1), "a judgement is not a scorer"
    assert w.n == 2, "the old fresh-worker revision, not a deeper turn"
    assert st["search"]["log"] == []

    w2 = _World({"Build": [_msg("Wrote app.py.")]})
    rid2 = SW.start("app", str(runs), "claude", w2.spawn, w2.run_turn, phases=PHASE,
                    review=False, search=swarm.Search(rng=Pick("deeper")))
    a2 = _wait(rid2)["agents"][0]
    assert (a2["state"], a2["revisions"]) == (SW.DONE, 0)


def test_a_search_attempt_that_produces_nothing_leaves_the_earlier_one(runs):
    w = _World({"Build": [_attempt(1, PYTEST_FAIL, "Done."),
                          [{"event": "error", "detail": "provider hiccup"}],
                          _attempt(0, PYTEST_PASS, "Fixed.")]})
    rid = SW.start("app", str(runs), "claude", w.spawn, w.run_turn, phases=PHASE,
                   review=False, search=swarm.Search(rng=Pick("deeper")))
    st = _wait(rid)
    a = st["agents"][0]
    assert (a["state"], a["revisions"], a["verified"]) == (SW.DONE, 2, True)
    assert [r["success"] for r in st["search"]["log"]] == [False, True]


def test_the_search_record_survives_a_restart_and_seeds_a_resume(runs):
    w = _World({"Build": [_attempt(1, PYTEST_FAIL, "Done."), _attempt(0, PYTEST_PASS, "Fixed.")]})
    rid = SW.start("app", str(runs), "claude", w.spawn, w.run_turn, phases=PHASE,
                   review=False, search=swarm.Search(rng=Pick("deeper")))
    _wait(rid)
    SW._RUNS.clear()
    assert SW.load() >= 1
    st = SW.status(rid)
    assert [r["choice"] for r in st["search"]["log"]] == ["deeper"]
    run = SW.get(rid)
    assert run.search is None and run.search_saved["log"]
    policy = SW._search_policy(True, run)
    assert policy.post == {"wider": [1, 1], "deeper": [2, 1]}
