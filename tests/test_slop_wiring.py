"""No AI slop, wired in (owner, 2026-10-04): decided in the plan, checked on the
finished work, at zero model cost.

  1. Multi's planner prompt asks for the visual decisions on a web/UI goal
     only (a non-web goal's prompt is unchanged);
  2. crew-design's planner and workers carry craft.DESIGN_FIRST (one text);
  3. a Multi phase that wrote web files is scanned: HIGH findings are
     problems for its ONE revision, the rest are warnings kept on the phase;
     a phase that wrote no web file is never scanned;
  4. the prose swarm / crews scan the HTML in the final text's code fences;
  5. verify.slop_problems is the shared pure helper.
"""
import json
import os
import threading
import time

import pytest

import craft
import crews
import receipts
import swarm
import swarm_windows as SW
import verify

from test_slopcheck import GOOD, SLOPPY

WEB_GOAL = "Build a landing page for our bakery with a hero section"
CODE_GOAL = "Add a CSV export command to the report script"


# --------------------------------------------------------------------------- #
# 1. The planner's prompt
# --------------------------------------------------------------------------- #

def test_a_web_goal_plan_prompt_asks_for_the_visual_decisions():
    for managed in (False, True):
        s = SW.plan_system(WEB_GOAL, managed)
        assert '"visual"' in s and "palette" in s and "reduced-motion" in s
        base = SW._PLAN_SYSTEM_MANAGED if managed else SW._PLAN_SYSTEM
        assert s.startswith(base.replace("{helpers}", str(SW._concurrency())))


def test_a_non_web_goal_plan_prompt_is_unchanged():
    n = str(SW._concurrency())
    assert SW.plan_system(CODE_GOAL) == SW._PLAN_SYSTEM.replace("{helpers}", n)
    assert SW.plan_system(CODE_GOAL, True) == SW._PLAN_SYSTEM_MANAGED.replace("{helpers}", n)


def test_the_visual_ask_is_small():
    assert len(SW._WEB_PLAN_ASK) // 4 < 130          # ~tokens


def test_plan_sends_the_web_ask_to_the_planner_only_for_web(monkeypatch):
    seen = []
    plan_json = json.dumps({"phases": [{"title": "A", "task": "a"}, {"title": "B", "task": "b"}]})

    def planner(system, user):
        seen.append(system)
        return plan_json

    SW.plan(WEB_GOAL, planner)
    SW.plan(CODE_GOAL, planner)
    assert '"visual"' in seen[0] and '"visual"' not in seen[1]


def test_the_web_design_skill_switch_removes_the_ask(monkeypatch):
    monkeypatch.setattr(craft, "skill_enabled", lambda name: False)
    assert SW.plan_system(WEB_GOAL, helpers=6) == SW._PLAN_SYSTEM.replace("{helpers}", "6") + (
        SW._MICRO_ASK.replace("{helpers}", "6") if SW.sizeable_goal(WEB_GOAL) else "")


# --------------------------------------------------------------------------- #
# 2. crew-design
# --------------------------------------------------------------------------- #

def test_crew_design_planner_and_workers_carry_the_design_first_block():
    prof = crews.CREWS["design"]
    assert craft.DESIGN_FIRST in prof["plan_system"]
    assert craft.DESIGN_FIRST in prof["phase_system"]
    assert prof["worker_extra"] == craft.WEB_DESIGN
    assert prof["slop_check"] is True
    assert craft.DESIGN_FIRST not in crews.CREWS["code"]["plan_system"]


def test_skill_off_strips_the_block_from_crew_design(monkeypatch):
    monkeypatch.setattr(craft, "skill_enabled", lambda name: False)
    seen = {}

    def fake_run(messages, dispatch, profile=None, **kw):
        seen["profile"] = profile
        return {"text": "x"}

    monkeypatch.setattr(swarm, "run", fake_run)
    crews.run([{"role": "user", "content": "design a landing page"}], lambda *a, **k: ("", None), "design")
    p = seen["profile"]
    assert craft.DESIGN_FIRST not in p["plan_system"] + p["phase_system"]
    assert p["slop_check"] is False


# --------------------------------------------------------------------------- #
# 3. Multi: the output check on a finished phase
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


class _World:
    """Each attempt writes (name, text) into the project, then reports."""

    def __init__(self, project, attempts):
        self.project, self.attempts, self.prompts, self.n = project, list(attempts), [], 0

    def spawn(self, cli, project):
        self.n += 1
        return "w%d" % self.n

    def run_turn(self, sid, prompt):
        self.prompts.append(prompt)
        name, text = self.attempts.pop(0) if self.attempts else ("x.txt", "x")
        with open(os.path.join(self.project, name), "w", encoding="utf-8") as fh:
            fh.write(text)
        yield {"event": "message", "text": "Wrote " + name + " as planned."}


PHASE = [{"title": "Build", "task": "write the page", "needs": []}]


def _run(runs, attempts):
    project = str(runs / "proj")
    os.makedirs(project)
    w = _World(project, attempts)
    w.rid = SW.start("page", project, "claude", w.spawn, w.run_turn, phases=PHASE, review=False)
    return w, _wait(w.rid)["agents"][0]


def test_a_sloppy_page_gets_one_revision_naming_the_slop(runs):
    w, a = _run(runs, [("index.html", SLOPPY), ("index.html", GOOD)])
    assert (a["state"], a["revisions"]) == (SW.DONE, 1)
    assert "AI-slop" in w.prompts[1]
    assert a["slop"]["high"] == 0 and a["slop"]["files"] == 1       # re-scanned after the fix


def test_a_clean_page_gets_no_revision(runs):
    w, a = _run(runs, [("index.html", GOOD)])
    assert (a["state"], a["revisions"]) == (SW.DONE, 0)
    assert a["slop"]["high"] == 0 and len(w.prompts) == 1


def test_a_page_still_sloppy_after_the_revision_stops_at_one(runs):
    w, a = _run(runs, [("index.html", SLOPPY), ("index.html", SLOPPY)])
    assert a["revisions"] == 1 and len(w.prompts) == 2
    assert a["slop"]["high"] > 0 and a["problems"]


def test_a_phase_with_no_web_file_is_never_scanned(runs, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("scanned a non-web phase")
    monkeypatch.setattr(verify, "slop_report", boom)
    w, a = _run(runs, [("notes.txt", SLOPPY)])
    assert (a["state"], a["revisions"], a["slop"]) == (SW.DONE, 0, None)


def test_warnings_are_kept_on_the_phase_and_do_not_cause_a_revision(runs, monkeypatch):
    rep = {"findings": [{}], "high": [], "warnings": ["medium index.html (x): why -- fix: f"],
           "counts": {"high": 0, "medium": 1, "low": 0}, "line": "Slop check: 0 high, 1 medium",
           "score": 93}
    monkeypatch.setattr(verify, "slop_report", lambda docs: rep)
    w, a = _run(runs, [("index.html", GOOD)])
    assert a["revisions"] == 0
    assert a["slop"]["line"] == "Slop check: 0 high, 1 medium"
    assert a["slop"]["warnings"] == ["medium index.html (x): why -- fix: f"]
    SW._RUNS.clear()
    assert SW.load() >= 1
    back = SW.status(w.rid)["agents"][0]
    assert back["slop"]["medium"] == 1 and back["slop"]["warnings"] == a["slop"]["warnings"]


# --------------------------------------------------------------------------- #
# 4. Prose swarm / crews: HTML in the final text
# --------------------------------------------------------------------------- #

def _prose_dispatch(draft_html, revised_html):
    calls = []
    lock = threading.Lock()
    plan = json.dumps({"goal": "page", "phases": [{"title": "Page", "task": "write the page",
                                                   "done_when": "html"}]})
    ship = json.dumps({"verdict": "ship", "problems": []})

    def dispatch(messages, max_tokens, exclude_pids=()):
        sysm, user = messages[0]["content"], messages[-1]["content"]
        if sysm == swarm._APPLY_SYSTEM:
            stage, text = "revision", "```html\n%s\n```" % revised_html
        elif sysm == swarm._INSTRUCT_SYSTEM:
            stage, text = "instruct", "1. remove the gradient"
        elif "YOUR PHASE (" in user or "\nYOUR TASK: " in user:
            stage, text = "phase", "```html\n%s\n```" % draft_html
        elif "WHAT THE TEAM PRODUCED" in user:
            stage, text = "supervise", json.dumps({"missing": []})
        elif "PHASE OUTPUTS" in user:
            stage, text = "synth", "```html\n%s\n```" % revised_html
        elif user.startswith("BRIEF\n"):
            stage, text = "review", ship
        else:
            stage, text = "plan", plan
        with lock:
            calls.append({"stage": stage, "user": user})
        return text, "p/m"

    dispatch.calls = calls
    return dispatch


ASK = [{"role": "user", "content": "design a landing page for a bakery"}]


def test_crew_design_revises_once_when_the_final_html_is_sloppy_even_if_the_reviewer_ships():
    d = _prose_dispatch(SLOPPY, GOOD)
    out = crews.run(ASK, d, "design")
    stages = [c["stage"] for c in d.calls]
    assert "revision" in stages, stages
    assert any("AI-slop" in c["user"] for c in d.calls if c["stage"] in ("instruct", "revision")) \
        or "AI-slop" in " ".join(c["user"] for c in d.calls)
    assert out.get("slop_check", {}).get("high", 0) > 0     # what it found in the draft


def test_clean_html_in_crew_design_triggers_no_revision():
    d = _prose_dispatch(GOOD, GOOD)
    out = crews.run(ASK, d, "design")
    assert "revision" not in [c["stage"] for c in d.calls]
    assert out.get("slop_check", {}).get("high", 0) == 0 or "slop_check" not in out


def test_a_non_web_crew_never_scans(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("scanned")
    monkeypatch.setattr(verify, "slop_report", boom)
    d = _prose_dispatch("print('x')", "print('x')")
    crews.run([{"role": "user", "content": "write a python function to parse csv"}], d, "code")


# --------------------------------------------------------------------------- #
# 5. verify.slop_problems
# --------------------------------------------------------------------------- #

def test_slop_problems_lists_high_findings_and_is_empty_for_a_clean_page():
    probs = verify.slop_problems([("index.html", SLOPPY)])
    assert probs and all(p.startswith("AI-slop") for p in probs)
    assert verify.slop_problems([("index.html", GOOD)]) == []
    assert verify.slop_problems(None) == []


def test_slop_problems_never_raises_when_slopcheck_breaks(monkeypatch):
    import slopcheck
    monkeypatch.setattr(slopcheck, "check_files", lambda *a, **k: 1 / 0)
    assert verify.slop_problems([("a.html", SLOPPY)]) == []


def test_html_blocks_and_file_reading_are_bounded(tmp_path):
    text = "x\n```html\n<div>a</div>\n```\n```python\nprint(1)\n```\n```css\na{}\n```"
    assert [n for n, _ in verify.html_blocks(text)] == ["block-1.html", "block-2.css"]
    (tmp_path / "a.html").write_text("<p>x</p>")
    (tmp_path / "b.py").write_text("x")
    (tmp_path / "big.css").write_text("a" * (verify.SLOP_MAX_BYTES + 1))
    got = verify.read_web_files(str(tmp_path), ["a.html", "b.py", "big.css", "gone.html"])
    assert [n for n, _ in got] == ["a.html"]
    many = ["f%d.html" % i for i in range(60)]
    for n in many:
        (tmp_path / n).write_text("<p>x</p>")
    assert len(verify.read_web_files(str(tmp_path), many)) == verify.SLOP_MAX_FILES
