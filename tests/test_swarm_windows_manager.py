r"""The subscription manager's role in a multi-session run.

REQUESTED: "the subscription manager plans, instructs, verifies and fixes;
free models do the work; minimal subscription tokens". So, when the hub has a
manager:

  * it writes the plan -- with an explicit brief per phase (inputs,
    constraints, output format, acceptance) -- and the free planner is only
    the fallback when it declines;
  * every finished phase is checked CHEAPLY first (an answer at all, a sane
    answer, the files its acceptance names), and only then does the manager
    give one short verdict on a summary, never a transcript;
  * a rejected phase runs once more, told what was wrong; still failing, it is
    handed to the review agent by name;
  * what the manager cost is counted per run and shown under the /agent reply.

And with no manager, the run is exactly what it was before.

Everything is a fake: spawn, run_turn, planner and manager are injected.
"""
import json
import os
import time

import pytest

import swarm_windows as SW


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setenv(SW._STORE_ENV, str(tmp_path / "runs"))
    # Seconds of stagger and backoff are for real CLIs sharing a database;
    # a fake has nothing to collide on.
    monkeypatch.setattr(SW, "SPAWN_STAGGER", 0)
    monkeypatch.setattr(SW, "RETRY_BACKOFF", 0)
    SW._RUNS.clear()
    yield
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
    """Records every session, prompt and configure call."""

    def __init__(self, replies=None):
        self.n = 0
        self.prompts = {}        # session id -> prompt
        self.configured = []     # (session id, mode)
        self.replies = replies or {}   # phase title -> list of replies (in order)

    def spawn(self, cli, project):
        self.n += 1
        return "s%d" % self.n

    def configure(self, sid, mode):
        self.configured.append((sid, mode))

    def run_turn(self, sid, prompt):
        self.prompts[sid] = prompt
        title = prompt.split("Your phase is called: ", 1)[1].splitlines()[0]
        queue = self.replies.get(title)
        text = queue.pop(0) if queue else "did " + title
        if text:
            yield {"event": "message", "text": text}
        yield {"event": "done"}

    def prompts_for(self, title):
        return [p for p in self.prompts.values()
                if "Your phase is called: " + title + "\n" in p + "\n"]


class _Manager:
    """A scripted manager: plan reply, then verify replies in order."""

    def __init__(self, plan="", verdicts=(), tokens=100):
        self.plan = plan
        self.verdicts = list(verdicts)
        self.tokens = tokens
        self.calls = []

    def __call__(self, system, user, purpose, max_tokens):
        self.calls.append({"system": system, "user": user, "purpose": purpose,
                           "max_tokens": max_tokens})
        if purpose == "plan":
            return (self.plan, self.tokens if self.plan else 0)
        if not self.verdicts:
            return ("", 0)                         # budget spent / declined
        v = self.verdicts.pop(0)
        return (json.dumps(v) if isinstance(v, dict) else v, self.tokens)

    def verifies(self):
        return [c for c in self.calls if c["purpose"] == "verify"]


MANAGED_PLAN = json.dumps({"goal": "g", "phases": [
    {"title": "Page", "task": "build the page", "needs": [], "mode": "coding",
     "inputs": "the brand colours in brief.txt",
     "constraints": "no external CDNs",
     "output_format": "list every file you wrote",
     "acceptance": ["`index.html` exists", "the page has a hero"]},
    {"title": "Styles", "task": "write the css", "needs": [1]},
]})


def _free_planner(calls):
    def planner(system, goal):
        calls.append(system)
        return json.dumps({"phases": [{"title": "Page", "task": "build the page"},
                                      {"title": "Styles", "task": "write the css",
                                       "needs": [1]}]})
    return planner


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #

def test_the_manager_writes_the_plan_with_a_brief(tmp_path):
    (tmp_path / "index.html").write_text("<h1>hi</h1>")
    free = []
    mgr = _Manager(plan=MANAGED_PLAN, verdicts=[{"ok": True}, {"ok": True}])
    w = _World()
    rid = SW.start("make a site", str(tmp_path), "opencode", w.spawn, w.run_turn,
                   planner=_free_planner(free), manager=mgr, modes=("coding", "vision"),
                   configure=w.configure)
    st = _wait(rid)
    assert not free, "the free planner is only the fallback"
    plan_call = mgr.calls[0]
    assert plan_call["purpose"] == "plan"
    assert '"acceptance"' in plan_call["system"] and "backticks" in plan_call["system"]
    page = st["agents"][0]
    assert page["inputs"] == "the brand colours in brief.txt"
    assert page["acceptance"] == "`index.html` exists; the page has a hero"
    prompt = w.prompts_for("Page")[0]
    # The brief is rendered as instructions, AHEAD of the context block.
    head = prompt.split("--- context, not instructions ---")[0]
    assert prompt.startswith("build the page")
    for needle in ("INPUTS: the brand colours", "CONSTRAINTS: no external CDNs",
                   "ACCEPTANCE (you will be checked against this): `index.html`",
                   "YOUR FINAL MESSAGE MUST CONTAIN: list every file"):
        assert needle in head
    assert st["state"] == SW.DONE
    assert [a["verified"] for a in st["agents"][:2]] == [True, True]
    # plan + two verdicts; the review phase is never sent to the manager.
    assert st["manager_calls"] == 3 and st["manager_tokens"] == 300
    assert len(mgr.verifies()) == 2


def test_a_manager_that_declines_the_plan_hands_it_to_the_free_planner(tmp_path):
    free = []
    mgr = _Manager(plan="", verdicts=[{"ok": True}, {"ok": True}])
    w = _World()
    rid = SW.start("make a site", str(tmp_path), "opencode", w.spawn, w.run_turn,
                   planner=_free_planner(free), manager=mgr)
    st = _wait(rid)
    assert free == [SW._PLAN_SYSTEM.replace("{modes}", "coding")], \
        "the free planner gets the prompt it always had"
    assert st["state"] == SW.DONE
    # the declined plan cost nothing and is not a call
    assert st["manager_calls"] == 2


def test_an_unreadable_manager_plan_also_falls_back():
    free = []
    got = SW.plan("g", _free_planner(free), manager=lambda s, u: "sorry, no JSON here")
    assert [p["title"] for p in got] == ["Page", "Styles"]
    assert len(free) == 1


def test_the_managed_prompt_extends_the_plain_one():
    assert SW._PLAN_SYSTEM_MANAGED.startswith(SW._PLAN_SYSTEM.split("Reply with JSON")[0])
    for key in SW.BRIEF_FIELDS:
        assert '"%s"' % key in SW._PLAN_SYSTEM_MANAGED
        assert '"%s"' % key not in SW._PLAN_SYSTEM


# --------------------------------------------------------------------------- #
# Verification and the one revision
# --------------------------------------------------------------------------- #

PHASES = [{"title": "Page", "task": "build the page", "needs": []},
          {"title": "Styles", "task": "write the css", "needs": []}]


def test_a_rejected_phase_is_retried_with_its_problems_then_flagged(tmp_path):
    mgr = _Manager(verdicts=[
        {"ok": False, "problems": ["the hero is missing"], "mode": "vision"},
        {"ok": False, "problems": ["the hero is still missing"]},
        {"ok": True},                                  # Styles
    ])
    # Styles waits for Page, so the verdicts arrive in this order.
    phases = [PHASES[0], dict(PHASES[1], needs=[1])]
    w = _World()
    rid = SW.start("site", str(tmp_path), "opencode", w.spawn, w.run_turn,
                   phases=phases, manager=mgr, modes=("coding", "vision"),
                   configure=w.configure)
    st = _wait(rid)
    page = st["agents"][0]
    assert page["state"] == SW.FAILED
    assert page["verified"] is False
    assert page["revisions"] == 1
    assert page["problems"] == ["the hero is still missing"]
    assert "did not pass verification" in page["error"]
    # the revision was a fresh worker, told what was wrong, under the mode the
    # manager suggested
    first, second = w.prompts_for("Page")
    assert "WAS CHECKED AND REJECTED" not in first
    assert "WAS CHECKED AND REJECTED" in second and "- the hero is missing" in second
    assert page["mode"] == "vision"
    assert [m for _, m in w.configured] == ["vision"], "the retry ran under it"
    # its summary survives for the review
    assert page["summary"] == "did Page"
    # the review is told which phase still fails and which passed
    review = w.prompts_for(SW.REVIEW_TITLE)[0]
    assert "phase 1 (Page): FAILED its checks -- fix: the hero is still missing" in review
    assert "phase 2 (Styles): verified" in review
    # the run is not a failure: the review still ran
    assert st["state"] == SW.DONE
    report = SW.format_result(rid)
    assert "Still failing its checks: the hero is still missing" in report


def test_a_revision_that_passes_is_done_and_verified(tmp_path):
    mgr = _Manager(verdicts=[{"ok": False, "problems": ["no footer"]}, {"ok": True}])
    w = _World()
    rid = SW.start("site", str(tmp_path), "opencode", w.spawn, w.run_turn,
                   phases=[PHASES[0]], manager=mgr)
    st = _wait(rid)
    a = st["agents"][0]
    assert (a["state"], a["verified"], a["revisions"], a["problems"]) == (SW.DONE, True, 1, [])
    assert a["error"] is None


def test_an_invented_mode_suggestion_is_ignored(tmp_path):
    mgr = _Manager(verdicts=[{"ok": False, "problems": ["x"], "mode": "genius"},
                             {"ok": True}])
    w = _World()
    rid = SW.start("site", str(tmp_path), "opencode", w.spawn, w.run_turn,
                   phases=[dict(PHASES[0], mode="coding")], manager=mgr,
                   modes=("coding",), configure=w.configure)
    st = _wait(rid)
    assert st["agents"][0]["mode"] == "coding"


def test_cheap_checks_run_first_and_spare_the_manager(tmp_path):
    """A missing acceptance file is caught without a paid call."""
    mgr = _Manager(verdicts=[{"ok": True}])
    w = _World()
    phase = dict(PHASES[0], acceptance="`index.html` exists")
    rid = SW.start("site", str(tmp_path), "opencode", w.spawn, w.run_turn,
                   phases=[phase], manager=mgr)
    st = _wait(rid)
    a = st["agents"][0]
    assert a["state"] == SW.FAILED and a["verified"] is False
    assert "`index.html`" in a["problems"][0]
    assert mgr.verifies() == [], "the manager is never asked about a phase that fails cheaply"
    assert st["manager_calls"] == 0


def test_an_acceptance_file_the_worker_wrote_passes_the_cheap_check(tmp_path):
    mgr = _Manager(verdicts=[{"ok": True}])

    def run_turn(sid, prompt):
        (tmp_path / "index.html").write_text("<h1>hi</h1>")
        yield {"event": "message", "text": "wrote index.html"}

    rid = SW.start("site", str(tmp_path), "opencode", lambda c, p: "s1", run_turn,
                   phases=[dict(PHASES[0], acceptance="index.html has a hero")],
                   manager=mgr)
    st = _wait(rid)
    assert st["agents"][0]["verified"] is True
    brief = mgr.verifies()[0]["user"]
    assert "Files changed during the phase: index.html" in brief
    assert "wrote index.html" in brief


def test_the_manager_sees_a_clipped_summary_not_a_transcript(tmp_path):
    mgr = _Manager(verdicts=[{"ok": True}])
    long = "word " * 5000

    def run_turn(sid, prompt):
        for i in range(50):
            yield {"event": "output", "text": "TRANSCRIPT-LINE-%d" % i}
        yield {"event": "message", "text": long}

    rid = SW.start("site", str(tmp_path), "opencode", lambda c, p: "s1", run_turn,
                   phases=[PHASES[0]], manager=mgr)
    _wait(rid)
    brief = mgr.verifies()[0]["user"]
    assert "TRANSCRIPT-LINE" not in brief
    assert "[...clipped]" in brief
    assert len(brief) < SW.VERIFY_TEXT_CHARS + 2500
    assert mgr.verifies()[0]["max_tokens"] == SW.VERIFY_MAX_TOKENS


def test_an_empty_phase_is_retried_once_then_flagged(tmp_path):
    mgr = _Manager()
    w = _World(replies={"Page": ["", ""]})
    rid = SW.start("site", str(tmp_path), "opencode", w.spawn, w.run_turn,
                   phases=[PHASES[0]], manager=mgr)
    st = _wait(rid)
    a = st["agents"][0]
    assert a["state"] == SW.FAILED and a["revisions"] == 1
    assert "produced no result" in a["problems"][0]
    assert len(w.prompts_for("Page")) == 2
    assert mgr.verifies() == []


def test_a_broken_final_message_fails_the_cheap_check(tmp_path, monkeypatch):
    monkeypatch.setattr(SW.answer_check, "inspect",
                        lambda text, **k: {"ok": text != "LOOP",
                                           "reasons": ["repetition"], "salvage": None})
    mgr = _Manager(verdicts=[{"ok": True}])
    w = _World(replies={"Page": ["LOOP", "fine now"]})
    rid = SW.start("site", str(tmp_path), "opencode", w.spawn, w.run_turn,
                   phases=[PHASES[0]], manager=mgr)
    st = _wait(rid)
    a = st["agents"][0]
    assert a["state"] == SW.DONE and a["verified"] is True
    assert "repetition" in w.prompts_for("Page")[1]
    assert len(mgr.verifies()) == 1, "only the clean attempt reached the manager"


# --------------------------------------------------------------------------- #
# Budget exhausted / manager unavailable
# --------------------------------------------------------------------------- #

def test_a_spent_budget_leaves_the_phase_done_and_unverified(tmp_path):
    """_manager_dispatch answers ("", None) over budget -> ("", 0) here."""
    mgr = _Manager(plan="", verdicts=[])
    free = []
    w = _World()
    rid = SW.start("site", str(tmp_path), "opencode", w.spawn, w.run_turn,
                   planner=_free_planner(free), manager=mgr)
    st = _wait(rid)
    assert free, "the free planner planned"
    assert st["state"] == SW.DONE
    assert [a["state"] for a in st["agents"]] == [SW.DONE] * 3
    assert [a["verified"] for a in st["agents"]] == [None] * 3
    assert st["manager_calls"] == 0 and st["manager_tokens"] == 0
    assert all("WAS CHECKED AND REJECTED" not in p for p in w.prompts.values())


def test_a_manager_that_raises_is_not_fatal(tmp_path):
    def boom(*a, **k):
        raise RuntimeError("cli gone")
    w = _World()
    rid = SW.start("site", str(tmp_path), "opencode", w.spawn, w.run_turn,
                   planner=_free_planner([]), manager=boom)
    st = _wait(rid)
    assert st["state"] == SW.DONE
    assert all(a["state"] == SW.DONE for a in st["agents"])


# --------------------------------------------------------------------------- #
# No manager: unchanged
# --------------------------------------------------------------------------- #

def test_without_a_manager_nothing_is_verified_and_the_prompts_are_plain(tmp_path):
    free = []
    w = _World(replies={"Page": [""]})
    phases = [dict(PHASES[0], acceptance="`index.html` exists"), PHASES[1]]
    rid = SW.start("site", str(tmp_path), "opencode", w.spawn, w.run_turn,
                   phases=phases)
    st = _wait(rid)
    page, styles = st["agents"][0], st["agents"][1]
    # An empty phase fails, exactly as it always did -- and is not retried.
    assert page["state"] == SW.FAILED and page["revisions"] == 0
    assert page["error"] == "the agent produced no result"
    assert len(w.prompts_for("Page")) == 1
    # A missing acceptance file is not checked: nothing is, without a manager.
    assert styles["state"] == SW.DONE and styles["verified"] is None
    assert st["manager_calls"] == 0
    review = w.prompts_for(SW.REVIEW_TITLE)[0]
    assert "VERIFICATION OF THE OTHER PHASES" not in review
    assert "Still failing" not in SW.format_result(rid)


def test_a_plain_plan_keeps_its_old_shape():
    got = SW.clean_phases({"phases": [{"title": "A", "task": "x"}]})
    assert set(got[0]) == {"title", "task", "done_when", "needs", "mode"}


def test_a_plain_worker_prompt_carries_no_brief_lines():
    run = SW._Run("g", ".", "opencode", SW.clean_phases({"phases": [{"title": "A", "task": "do a"}]}))
    prompt = SW._agent_prompt(run, run.agents[0])
    for needle in ("INPUTS:", "CONSTRAINTS:", "ACCEPTANCE", "FINAL MESSAGE MUST",
                   "REJECTED", "VERIFICATION"):
        assert needle not in prompt


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #

def test_verification_and_the_manager_bill_survive_a_restart(tmp_path):
    mgr = _Manager(verdicts=[{"ok": False, "problems": ["p1"]}, {"ok": False, "problems": ["p2"]}])
    w = _World()
    rid = SW.start("site", str(tmp_path), "opencode", w.spawn, w.run_turn,
                   phases=[dict(PHASES[0], acceptance="a hero")], manager=mgr)
    _wait(rid)
    SW._RUNS.clear()
    assert SW.load() >= 1
    st = SW.status(rid)
    a = st["agents"][0]
    assert (a["verified"], a["problems"], a["revisions"]) == (False, ["p2"], 1)
    assert a["acceptance"] == "a hero"
    assert st["manager_calls"] == 2 and st["manager_tokens"] == 200


# --------------------------------------------------------------------------- #
# The hub's wiring
# --------------------------------------------------------------------------- #

def test_the_hub_passes_a_manager_only_when_one_is_enabled(monkeypatch):
    import app as A
    monkeypatch.setattr(A, "_manager_enabled", lambda: False)
    assert A._swarm_windows_manager_kw() == {}
    monkeypatch.setattr(A, "_manager_enabled", lambda: True)
    assert A._swarm_windows_manager_kw() == {"manager": A._swarm_windows_manager}


def test_the_hub_manager_reports_what_the_call_cost(monkeypatch):
    import app as A
    seen = {}

    def dispatch(messages, max_tokens=None, purpose="other"):
        seen.update(messages=messages, max_tokens=max_tokens, purpose=purpose)
        A._MANAGER_CALL.tokens = getattr(A._MANAGER_CALL, "tokens", 0) + 321
        return "verdict", "sub-claude/sonnet"

    monkeypatch.setattr(A, "_manager_dispatch", dispatch)
    A._MANAGER_CALL.tokens = 999        # a previous call's spend is not billed again
    assert A._swarm_windows_manager("sys", "usr", "verify", 400) == ("verdict", 321)
    assert seen["purpose"] == "multi-verify" and seen["max_tokens"] == 400
    assert seen["messages"][0] == {"role": "system", "content": "sys"}

    monkeypatch.setattr(A, "_manager_dispatch", lambda *a, **k: ("", None))
    assert A._swarm_windows_manager("s", "u", "plan", 3000) == ("", 0)


def test_the_charge_is_counted_per_thread(monkeypatch, tmp_path):
    import app as A
    monkeypatch.setattr(A.quota, "_persist_maybe", lambda: None)
    monkeypatch.setattr(A, "_MANAGER_TOKENS",
                        {"day": "", "spent": 0, "calls": 0, "by_purpose": {}})
    A._MANAGER_CALL.tokens = 0
    A._manager_charge(50, "multi-plan")
    assert A._MANAGER_CALL.tokens == 50


def test_the_footer_line():
    import app as A
    assert A._multi_manager_footer(0, 0) == ""
    assert A._multi_manager_footer(None, None) == ""
    assert A._multi_manager_footer(12345, 3) == \
        "-- manager: 12,345 subscription tokens over 3 calls"
    assert A._multi_manager_footer(80, 1).endswith("over 1 call")


def test_the_agent_reply_carries_the_footer_but_the_report_does_not(monkeypatch):
    import app as A
    frame = {"run_id": "swarm-x", "state": SW.DONE, "total": 1, "done": 1,
             "agents": [{"index": 1, "title": "Build", "state": SW.DONE,
                         "summary": "built", "error": None, "mode": None}],
             "manager_tokens": 700, "manager_calls": 2}
    monkeypatch.setattr(A.swarm_windows, "status", lambda rid, with_events=False: frame)
    monkeypatch.setattr(A.swarm_windows, "format_result", lambda rid: "REPORT")
    monkeypatch.setattr(A, "_MULTI_POLL", 0.0)
    evs = list(A._multi_follow_events("swarm-x", "opencode"))
    done = [e for e in evs if e["event"] == "done"][0]["text"]
    assert done == "REPORT\n\n-- manager: 700 subscription tokens over 2 calls"


def test_no_manager_no_footer(monkeypatch):
    import app as A
    frame = {"run_id": "swarm-x", "state": SW.DONE, "total": 1, "done": 1,
             "agents": [], "manager_tokens": 0, "manager_calls": 0}
    monkeypatch.setattr(A.swarm_windows, "status", lambda rid, with_events=False: frame)
    monkeypatch.setattr(A.swarm_windows, "format_result", lambda rid: "REPORT")
    monkeypatch.setattr(A, "_MULTI_POLL", 0.0)
    evs = list(A._multi_follow_events("swarm-x", "opencode"))
    assert [e for e in evs if e["event"] == "done"][0]["text"] == "REPORT"


def test_every_start_site_passes_the_manager_kw():
    src = open("app.py", encoding="utf-8").read()
    # Three start sites, plus the boot-time resume that re-attaches it.
    assert src.count("**_swarm_windows_manager_kw()") == 4
    i = src.index("def _resume_interrupted_swarms(")
    assert "**_swarm_windows_manager_kw()" in src[i:i + 1600]
