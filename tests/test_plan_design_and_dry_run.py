"""Design, plan, dry run -- then go.

Owner, 2026-10-04: "In all works the hub must do steps: design, plan well in a
perfect architecture, then go -- and prevent problems with a DRY RUN in
planning."

- Multi: the planner returns a `design` (components, interfaces, data flow)
  before its phases, and each phase the `files` it owns. Every helper's prompt
  carries the design (bounded), the run persists it, the Build page shows it
  as one collapsed row.
- plan_check.check_plan dry-runs the plan before any helper starts (no model
  call, no command): parallel phases owning one file get a dependency, a
  phase reading a file an earlier phase writes waits for it, a missing
  done-when / input file is a warning, an enumerated part no phase covers
  earns ONE re-ask of the free planner, then the run goes ahead anyway.
- Single sessions (every CLI): craft.PLAN_PHASES asks for the same DESIGN and
  DRY-RUN steps, within a small token budget.
"""
import json
import os
import time

import pytest

import craft
import plan_check
import swarm_windows as SW


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setenv(SW._STORE_ENV, str(tmp_path / "runs"))
    monkeypatch.setattr(SW, "SPAWN_STAGGER", 0.0)
    SW._RUNS.clear()
    yield
    for run in list(SW._RUNS.values()):
        run.stop_flag.set()
    SW._RUNS.clear()


def _wait(run_id, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        st = SW.status(run_id)
        if st and st["state"] in (SW.DONE, SW.FAILED, SW.STOPPED):
            return st
        time.sleep(0.02)
    return SW.status(run_id)


class _World:
    def __init__(self):
        self.n = 0
        self.prompts = {}

    def spawn(self, cli, project):
        self.n += 1
        return "sess-%d" % self.n

    def run_turn(self, sid, prompt):
        self.prompts[sid] = prompt
        yield {"type": "message", "text": "done: " + prompt.split("\n", 1)[0][:40]}
        yield {"type": "done"}

    def prompt_starting(self, head):
        return [p for p in self.prompts.values() if p.startswith(head)]


DESIGNED_PLAN = {
    "goal": "items app",
    "design": {
        "components": [{"name": "store.py", "role": "reads and writes items.json"},
                       "api.py: the HTTP layer"],
        "interfaces": ["store.load() -> list[dict]; store.save(items) -> None",
                       {"name": "GET /api/items", "contract": "-> [{id, name}]"}],
        "data_flow": ["browser", "api.py", "store.py", "items.json"],
    },
    "phases": [
        {"title": "Store", "task": "build the store module", "done_when": "pytest test_store.py passes",
         "files": ["store.py", "items.json"], "needs": []},
        {"title": "API", "task": "build the api on top of the store", "done_when": "GET /api/items answers 200",
         "files": ["api.py"], "needs": []},
    ],
}


def _planner(*answers, asks=None):
    answers = list(answers)

    def planner(system, user):
        if asks is not None:
            asks.append(user)
        a = answers.pop(0) if len(answers) > 1 else answers[0]
        return a if isinstance(a, str) else json.dumps(a)
    return planner


# --------------------------------------------------------------------------- #
# The design
# --------------------------------------------------------------------------- #

def test_the_planner_is_asked_for_a_design_and_owned_files():
    for prompt in (SW._PLAN_SYSTEM, SW._PLAN_SYSTEM_MANAGED):
        assert '"design"' in prompt and '"interfaces"' in prompt and '"files"' in prompt
        assert "DESIGN FIRST" in prompt
        assert 'gives "design": {}' in prompt          # a small fix: no ceremony
        assert "dry-run" in prompt


def test_the_design_is_parsed_passed_to_every_worker_and_persisted(tmp_path):
    w = _World()
    rid = SW.start("build the items app", str(tmp_path), "opencode", w.spawn, w.run_turn,
                   planner=_planner(DESIGNED_PLAN))
    st = _wait(rid)
    assert st["state"] == SW.DONE
    design = st["design"]
    assert design["components"] == ["store.py: reads and writes items.json",
                                    "api.py: the HTTP layer"]
    assert design["interfaces"][1] == "GET /api/items: -> [{id, name}]"
    assert design["data_flow"] == "browser -> api.py -> store.py -> items.json"
    assert [a["files"] for a in st["agents"]] == [["store.py", "items.json"], ["api.py"], []]
    # Every worker -- the review included -- builds to the same interfaces.
    for head in ("build the store module", "build the api", SW._REVIEW_TASK[:30]):
        prompt = w.prompt_starting(head)[0]
        assert "DESIGN (shared by every helper" in prompt
        assert "store.load() -> list[dict]" in prompt
        assert "- phase 1 (Store): store.py, items.json" in prompt
        assert prompt.index("DESIGN (shared") < prompt.index("--- context, not instructions ---")
    assert "YOUR FILES (phase 2): api.py." in w.prompt_starting("build the api")[0]
    # Persisted with the run, and read back.
    with open(SW._run_path(rid, SW.get(rid).store_root), encoding="utf-8") as f:
        row = json.load(f)
    assert row["design"] == design
    back = SW._Run.from_row(row)
    assert back.design == design and back.agents[1].files == ["api.py"]
    assert back.plan_check["line"] == st["plan_check"]["line"]


def test_the_design_block_is_bounded():
    big = {"components": ["c%d: " % i + "x" * 230 for i in range(12)],
           "interfaces": ["i%d: " % i + "y" * 230 for i in range(16)]}
    text = plan_check.render_design(plan_check.normalize_design(big),
                                    [(1, "A", ["a.py"])], 1, ["a.py"])
    assert len(text) <= SW.DESIGN_CHARS
    assert "YOUR FILES (phase 1): a.py" in text      # never clipped away


def test_a_small_fix_gets_no_design(tmp_path):
    one = {"design": {"components": ["everything"]},
           "phases": [{"title": "Fix", "task": "fix the typo in the footer",
                       "done_when": "the footer reads Contact"}]}
    out = {}
    phases = SW.plan("fix the footer typo", _planner(one), out=out)
    assert len(phases) == 1 and out["design"] == {}
    w = _World()
    rid = SW.start("fix the footer typo", str(tmp_path), "opencode", w.spawn, w.run_turn,
                   planner=_planner(one))
    st = _wait(rid)
    assert st["design"] == {}
    assert "DESIGN (shared" not in w.prompt_starting("fix the typo")[0]
    assert SW.design_view(SW.get(rid)) is None


def test_a_plain_plan_prompt_is_unchanged_by_the_design_block():
    run = SW._Run("g", ".", "opencode", [{"title": "A", "task": "do a", "needs": []}])
    assert SW.design_block(run, run.agents[0]) == ""
    assert "DESIGN (shared" not in SW._agent_prompt(run, run.agents[0])


# --------------------------------------------------------------------------- #
# The dry run: what it fixes
# --------------------------------------------------------------------------- #

def test_two_parallel_phases_owning_one_file_get_a_dependency():
    phases = [{"title": "Backend", "task": "t", "done_when": "pytest passes", "needs": [],
               "files": ["app.py"]},
              {"title": "Routes", "task": "t", "done_when": "pytest passes", "needs": [],
               "files": ["./App.py", "routes.py"]},
              {"title": "Docs", "task": "t", "done_when": "README has usage", "needs": [],
               "files": ["README.md"]}]
    fixed, report = plan_check.check_plan(phases, {}, "g", None)
    assert [p["needs"] for p in fixed] == [[], [1], []]
    assert phases[1]["needs"] == [], "the input is not mutated"
    conflict = [f for f in report["findings"] if f["kind"] == "file_conflict"]
    assert len(conflict) == 1 and conflict[0]["action"] == "fixed"
    assert conflict[0]["text"] == "phase 2 waits for 1 (both edit app.py)"
    assert report["start_now"] == 2


def test_a_folder_owner_conflicts_with_a_file_inside_it():
    phases = [{"title": "UI", "task": "t", "done_when": "page renders", "needs": [],
               "files": ["src/ui/"]},
              {"title": "Button", "task": "t", "done_when": "page renders", "needs": [],
               "files": ["src/ui/button.tsx"]}]
    fixed, _ = plan_check.check_plan(phases, {}, "g", None)
    assert fixed[1]["needs"] == [1]


def test_a_transitive_dependency_needs_no_change():
    phases = [{"title": "A", "task": "t", "done_when": "x exists", "needs": [], "files": ["app.py"]},
              {"title": "B", "task": "t", "done_when": "y exists", "needs": [1], "files": ["b.py"]},
              {"title": "C", "task": "t", "done_when": "z exists", "needs": [2], "files": ["app.py"]}]
    fixed, report = plan_check.check_plan(phases, {}, "g", None)
    assert [p["needs"] for p in fixed] == [[], [1], [2]]
    assert not [f for f in report["findings"] if f["kind"] == "file_conflict"]


def test_a_reader_waits_for_the_earlier_phase_that_writes_its_input(tmp_path):
    phases = [{"title": "Schema", "task": "design the schema", "done_when": "schema.json exists",
               "needs": [], "files": ["schema.json"]},
              {"title": "API", "task": "Read schema.json and generate the API handlers",
               "done_when": "the handlers import", "needs": [], "files": ["api.py"]}]
    fixed, report = plan_check.check_plan(phases, {}, "g", str(tmp_path))
    assert fixed[1]["needs"] == [1]
    assert any(f["kind"] == "input_order" and f["action"] == "fixed" for f in report["findings"])


def test_what_clean_phases_sanitised_is_reported_not_silent():
    notes = []
    raw = {"phases": [{"title": "A", "task": "a", "needs": [2]},
                      {"title": "B", "task": "b", "needs": [2]},
                      {"title": "Empty", "task": ""}]
           + [{"title": "P%d" % i, "task": "p"} for i in range(9)]}
    phases = SW.clean_phases(raw, notes=notes)
    assert len(phases) == SW.MAX_AGENTS - 1       # the cap counts the empty one too
    kinds = [n["kind"] for n in notes]
    assert kinds.count("need_dropped") == 2 and "no_task" in kinds and "too_many_phases" in kinds
    _fixed, report = plan_check.check_plan(phases, {}, "g", None, notes=notes)
    by_kind = {f["kind"]: f["action"] for f in report["findings"]}
    assert by_kind["need_dropped"] == "fixed"
    assert by_kind["too_many_phases"] == "warn"      # the run budget: phases dropped


def test_a_merged_look_only_phase_is_reported():
    notes = []
    SW.clean_phases({"phases": [{"title": "Locate the bug", "task": "find it"},
                                {"title": "Fix the bug", "task": "fix it", "needs": [1]}]},
                    notes=notes)
    assert notes and notes[0]["kind"] == "merged"


# --------------------------------------------------------------------------- #
# The dry run: what it warns about
# --------------------------------------------------------------------------- #

def test_a_phase_without_a_concrete_done_when_is_a_finding():
    phases = [{"title": "A", "task": "t", "needs": []},
              {"title": "B", "task": "t", "done_when": "Done.", "needs": []},
              {"title": "C", "task": "t", "done_when": "pytest tests/ passes", "needs": []},
              {"title": "D", "task": "t", "acceptance": "`index.html` exists", "needs": []}]
    _fixed, report = plan_check.check_plan(phases, {}, "g", None)
    flagged = [f["phase"] for f in report["findings"] if f["kind"] == "no_done_when"]
    assert flagged == [1, 2]
    assert all(f["action"] == "warn" for f in report["findings"] if f["kind"] == "no_done_when")


def test_an_input_file_that_does_not_exist_is_a_finding(tmp_path):
    (tmp_path / "data.csv").write_text("a,b\n")
    phases = [{"title": "Report", "task": "Load the rows from data.csv and plot them",
               "done_when": "chart.png exists", "needs": [], "files": ["report.py"]},
              {"title": "Page", "task": "build the page", "done_when": "index.html renders",
               "inputs": "the brand colours in brief.txt", "needs": [], "files": ["index.html"]}]
    _fixed, report = plan_check.check_plan(phases, {}, "g", str(tmp_path))
    missing = [f for f in report["findings"] if f["kind"] == "missing_input"]
    assert [f["text"] for f in missing] == ["phase 2 reads brief.txt: not in the project"]


def test_a_new_project_folder_says_nothing_about_missing_files(tmp_path):
    phases = [{"title": "Page", "task": "build it", "done_when": "index.html renders",
               "inputs": "brief.txt", "needs": []}]
    _fixed, report = plan_check.check_plan(phases, {}, "g", str(tmp_path / "not-yet"))
    assert not [f for f in report["findings"] if f["kind"] == "missing_input"]


def test_a_fully_sequential_plan_is_flagged():
    phases = [{"title": "A", "task": "t", "done_when": "a works now", "needs": []},
              {"title": "B", "task": "t", "done_when": "b works now", "needs": [1]},
              {"title": "C", "task": "t", "done_when": "c works now", "needs": [2]}]
    _fixed, report = plan_check.check_plan(phases, {}, "g", None)
    assert [f["kind"] for f in report["findings"]] == ["sequential"]


# --------------------------------------------------------------------------- #
# An uncovered enumerated part: one re-ask, then fail open
# --------------------------------------------------------------------------- #

GOAL = ("Build a word-count CLI. Deliver:\n1) a parser module\n"
        "2) a README with usage examples\n3) unit tests for the parser")
NO_README = {"design": {"components": ["wc.py: the parser"], "interfaces": ["count(text) -> dict"]},
             "phases": [{"title": "Parser", "task": "Build the parser module in wc.py",
                         "done_when": "python wc.py f.txt prints counts", "files": ["wc.py"]},
                        {"title": "Tests", "task": "Write unit tests for the parser",
                         "done_when": "pytest passes", "files": ["test_wc.py"], "needs": [1]}]}
WITH_README = dict(NO_README, phases=NO_README["phases"] + [
    {"title": "Docs", "task": "Write README.md with usage examples",
     "done_when": "README.md has a usage section", "files": ["README.md"]}])


def test_an_uncovered_part_earns_one_re_ask_and_the_better_plan_runs(tmp_path):
    asks = []
    w = _World()
    rid = SW.start(GOAL, str(tmp_path), "opencode", w.spawn, w.run_turn,
                   planner=_planner(NO_README, WITH_README, asks=asks))
    st = _wait(rid)
    assert len(asks) == 2
    assert "dry run of your previous plan found these problems" in asks[1]
    assert 'no phase covers "a README with usage examples"' in asks[1]
    assert '"Parser"' in asks[1], "the planner fixes ITS plan, it does not start over"
    assert [a["title"] for a in st["agents"]] == ["Parser", "Tests", "Docs", SW.REVIEW_TITLE]
    check = st["plan_check"]
    assert check["replanned"] is True
    assert check["fixed"] == ['re-planned to cover "a README with usage examples"']
    assert check["warnings"] == []


def test_a_re_ask_that_does_not_help_fails_open_with_the_findings_surfaced(tmp_path):
    asks = []
    w = _World()
    rid = SW.start(GOAL, str(tmp_path), "opencode", w.spawn, w.run_turn,
                   planner=_planner(NO_README, asks=asks))
    st = _wait(rid)
    assert len(asks) == 2, "ONE re-ask, never a loop"
    assert st["state"] == SW.DONE, "fail open: the run goes ahead"
    check = st["plan_check"]
    assert check["warnings"] == ['no phase covers "a README with usage examples"']
    assert check["line"] == ('Plan check: 3 phases, 1 start now, planner re-asked once, '
                             '0 fixed, 1 warning (no phase covers "a README with usage examples")')
    assert all(f["action"] != "replan" for f in check["findings"])


def test_a_covered_plan_is_not_re_asked(tmp_path):
    asks = []
    w = _World()
    rid = SW.start(GOAL, str(tmp_path), "opencode", w.spawn, w.run_turn,
                   planner=_planner(WITH_README, asks=asks))
    st = _wait(rid)
    assert len(asks) == 1
    assert st["plan_check"]["replanned"] is False


def test_phases_given_directly_are_checked_but_never_re_asked(tmp_path):
    w = _World()
    rid = SW.start(GOAL, str(tmp_path), "opencode", w.spawn, w.run_turn,
                   phases=NO_README["phases"])
    st = _wait(rid)
    assert st["plan_check"]["warnings"] == ['no phase covers "a README with usage examples"']
    assert st["plan_check"]["replanned"] is False


def test_a_dry_run_that_breaks_never_stops_the_run(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug in the check")
    monkeypatch.setattr(plan_check, "check_plan", boom)
    w = _World()
    rid = SW.start("g", str(tmp_path), "opencode", w.spawn, w.run_turn,
                   planner=_planner(DESIGNED_PLAN))
    st = _wait(rid)
    assert st["state"] == SW.DONE and st["plan_check"] is None


# --------------------------------------------------------------------------- #
# What the owner sees
# --------------------------------------------------------------------------- #

def test_the_report_line_counts_the_plan_as_it_runs(tmp_path):
    plan = {"phases": [
        {"title": "Backend", "task": "build the backend", "done_when": "pytest passes",
         "files": ["app.py"]},
        {"title": "Routes", "task": "add the routes", "done_when": "pytest passes",
         "files": ["app.py"]},
        {"title": "Docs", "task": "write docs", "files": ["README.md"]}]}
    w = _World()
    rid = SW.start("g", str(tmp_path), "opencode", w.spawn, w.run_turn, planner=_planner(plan))
    st = _wait(rid)
    assert [a["needs"] for a in st["agents"]][:3] == [[], [1], []]
    assert st["plan_check"]["line"] == (
        "Plan check: 4 phases, 2 start now, 1 fixed (phase 2 waits for 1 (both edit app.py)), "
        "1 warning (phase 3 (Docs) has no concrete done-when)")


def test_the_report_line_reaches_the_conversation(monkeypatch):
    import app as A
    line = "Plan check: 2 phases, 1 start now, 0 fixed, 0 warnings"
    frame = {"run_id": "swarm-x", "state": SW.DONE, "total": 2, "done": 2, "resumes": 0,
             "agents": [{"index": 1, "title": "A", "state": SW.DONE, "needs": [], "summary": "ok"},
                        {"index": 2, "title": "B", "state": SW.DONE, "needs": [1], "summary": "ok"}],
             "plan_check": {"line": line}, "manager_tokens": 0, "manager_calls": 0}
    monkeypatch.setattr(A.swarm_windows, "status", lambda rid, with_events=False: frame)
    monkeypatch.setattr(A.swarm_windows, "format_result", lambda rid: "REPORT")
    monkeypatch.setattr(A, "_MULTI_POLL", 0.0)
    tools = [e["text"] for e in A._multi_follow_events("swarm-x", "opencode")
             if e["event"] == "tool"]
    assert line in tools
    # Right after the plan lines, through the same event path.
    assert tools.index(line) == 1 + max(i for i, t in enumerate(tools)
                                        if t.startswith("Plan ·"))
    frame["resumes"] = 1                          # a "continue": already checked
    tools = [e["text"] for e in A._multi_follow_events("swarm-x", "opencode")
             if e["event"] == "tool"]
    assert not [t for t in tools if t.startswith("Plan check:")]


def test_the_build_page_shows_the_design_as_one_collapsed_row(tmp_path):
    import app as A
    phases = [dict(p) for p in DESIGNED_PLAN["phases"]]
    run = SW._Run("g", str(tmp_path), "opencode", phases, owner="conv-design",
                  design=plan_check.normalize_design(DESIGNED_PLAN["design"]),
                  check_report={"line": "Plan check: 2 phases, 2 start now, 0 fixed, 0 warnings"})
    run.state = SW.RUNNING
    SW._RUNS[run.id] = run
    got = A._multi_run_plan("conv-design", str(tmp_path))
    assert got["design"]["line"] == "Design: 2 components, 2 interfaces, data flow"
    assert "Files each phase owns:" in got["design"]["text"]
    assert got["plan_check"].startswith("Plan check:")
    html = open(os.path.join(os.path.dirname(__file__), "..", "templates", "index.html"),
                encoding="utf-8").read()
    assert "function designRow(" in html and "p.design.line" in html
    assert "createElement('details')" in html     # collapsed until opened


# --------------------------------------------------------------------------- #
# Single sessions: the brief every CLI gets
# --------------------------------------------------------------------------- #

def test_the_opening_brief_asks_for_a_design_and_a_dry_run():
    text = craft.PLAN_PHASES
    assert "DESIGN first for a build/feature (skip for a small fix)" in text
    assert "interfaces" in text and "which files each phase touches" in text
    assert "DRY-RUN the plan before editing" in text
    for check in ("every asked part has a phase", "no two same-time phases edit one file",
                  "each has a done when", "Fix the plan first"):
        assert check in text
    # It reaches every tool-carrying opening turn, hit or no hit.
    assert "DRY-RUN" in craft.system_message("refactor this function")["content"]


def test_the_brief_stays_within_its_token_budget():
    # 626 chars before the DESIGN + DRY-RUN steps; the owner's cap for the
    # addition is ~150 tokens (chars / 4, the suite's convention).
    assert (len(craft.PLAN_PHASES) - 626) / 4 <= 150
    assert len(craft.PLAN_PHASES) / 4 <= 230
