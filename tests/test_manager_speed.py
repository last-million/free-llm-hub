"""Manager pipeline speed + completeness (the tally run, MEASURED 2026-09-27).

Live: "tally.py + test_tally.py + README" with manager sub-claude/sonnet took
772 s and shipped WITHOUT the tests ("unfinished=1: part 2 ... done=2/3"). The
by-purpose spend (plan, verify, fix -- no supervise, no review) says the wall
clock ran out before the tests wave finished: every stage waited on its own
100-150 s subscription call. These tests pin the fixes with fakes (no network,
no CLI): fewer manager calls for the same plan, the paid verdict overlapping
free work, tests wired to the code they test and handed its interface, bad
tests caught and repaired for free, a deadline on every manager call.
"""
import json
import re
import threading
import time

import pytest

import swarm

TALLY = ("Build a tiny CLI. Deliver: 1) the full tally.py using argparse with flags "
         "-l,-w,-c that print line, word and character counts of a file 2) a pytest "
         "file test_tally.py with at least 5 tests covering each flag 3) a README "
         "section with a description, a usage block, a markdown table of the 3 flags "
         "and one example. Every part must be consistent.")

CODE_V1 = '''```python
import argparse
import sys


def count(text):
    return len(text.splitlines()), len(text.split()), len(text)


def main(argv=None):
    p = argparse.ArgumentParser(prog="tally")
    p.add_argument("-l", action="store_true")
    p.add_argument("-w", action="store_true")
    p.add_argument("-c", action="store_true")
    p.add_argument("file")
    a = p.parse_args(argv)
    with open(a.file, encoding="utf-8") as fh:
        lines, words, chars = count(fh.read())
    if a.l:
        print(lines)
    if a.w:
        print(words)
    if a.c:
        print(chars)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```'''

CODE_V2 = CODE_V1.replace("def main(argv=None):", "def count_chars(text):\n    return len(text)\n\n\n"
                          "def main(argv=None):")


def _tests_file(names="count, main"):
    body = "".join(
        "def test_%s(tmp_path, capsys):\n    f = tmp_path / 'a.txt'\n    f.write_text('a b\\n')\n"
        "    main(['-%s', str(f)])\n    assert capsys.readouterr().out.strip()\n\n\n" % (n, flag)
        for n, flag in (("lines", "l"), ("words", "w"), ("chars", "c"), ("all", "l"),
                        ("words_again", "w")))
    return "```python\nfrom tally import %s\n\n\n%s```" % (names, body)


GOOD_TESTS = _tests_file()
BAD_TESTS = _tests_file("count_lines, main")         # a name tally.py does not define
README = ("## tally\nCounts things.\n\n```\npython tally.py -l file.txt\n```\n\n"
          "| Flag | Meaning |\n|---|---|\n| -l | lines |\n| -w | words |\n"
          "| -c | chars |\n\nExample: `python tally.py -w notes.txt`")

# The plan a planner told to "maximise parallelism" writes: every phase
# independent -- the tests and the README cannot see tally.py.
PLAN = {"goal": "tally", "phases": [
    {"title": "tally.py", "task": "Implement tally.py with argparse flags -l -w -c "
                                  "printing line, word and character counts", "needs": []},
    {"title": "test_tally.py", "task": "Write test_tally.py: pytest tests for the CLI",
     "acceptance": ["at least 5 tests"], "needs": []},
    {"title": "README section", "task": "README section: description, usage block, "
                                        "markdown table of the flags, one example",
     "needs": []}]}


def _phase_title(user):
    m = re.search(r"YOUR PHASE \(\d+ of \d+\): (.+)", user)
    return m.group(1).strip() if m else ""


class Fleet:
    """Free dispatch: answers each stage; records every call with timings."""

    def __init__(self, plan=PLAN, answers=None, delay=0.0):
        self.plan = plan
        self.answers = dict(answers or {})
        self.delay = delay
        self.calls = []
        self.lock = threading.Lock()

    def stage(self, msgs):
        sys_ = msgs[0]["content"]
        user = msgs[-1]["content"]
        if sys_.startswith(swarm._PLAN_SYSTEM):
            return "plan"
        if sys_.startswith(swarm._PHASE_SYSTEM):
            if "A PREVIOUS ATTEMPT" in user:
                return "retry"
            return "phase"
        if sys_.startswith(swarm._SYNTH_SYSTEM):
            return "synth"
        if sys_.startswith(swarm._REVIEW_SYSTEM):
            return "review"
        if sys_.startswith(swarm._SUPERVISE_SYSTEM):
            return "supervise"
        return "other"

    def __call__(self, msgs, max_tokens, exclude_pids=()):
        st = self.stage(msgs)
        user = msgs[-1]["content"]
        title = _phase_title(user)
        t0 = time.monotonic()
        with self.lock:
            n = len(self.calls)
            rec = {"stage": st, "title": title, "user": user, "t0": t0,
                   "exclude": tuple(exclude_pids)}
            self.calls.append(rec)
        if st in ("phase", "retry") and self.delay:
            time.sleep(self.delay)
        if st == "plan":
            text = json.dumps(self.plan)
        elif st in ("phase", "retry"):
            a = self.answers.get((st, title), self.answers.get(title))
            if callable(a):
                a = a(user)
            if a is None:
                a = {"tally.py": CODE_V1, "test_tally.py": GOOD_TESTS,
                     "README section": README}.get(title, "output")
            text = a
        elif st == "synth":
            text = user.split("PHASE OUTPUTS\n", 1)[1].split("\n\nREVIEWER PROBLEMS", 1)[0]
        elif st == "review":
            text = '{"verdict": "ship", "problems": []}'
        elif st == "supervise":
            text = '{"missing": []}'
        else:
            text = ""
        rec["t1"] = time.monotonic()
        return text, ("free%d/model" % n if text else None)

    def of(self, stage, title=None):
        return [c for c in self.calls if c["stage"] == stage
                and (title is None or c["title"] == title)]


class Manager:
    """Scripted paid manager. A batched verdict ("JUDGE THESE") passes every
    phase it lists and answers the coverage question; `verdicts` overrides by
    phase title (a list consumed in order)."""

    def __init__(self, plan=PLAN, verdicts=None, delay=0.0, verify_delay=0.0):
        self.plan = plan
        self.verdicts = {k: list(v) for k, v in (verdicts or {}).items()}
        self.delay = delay
        self.verify_delay = verify_delay
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, msgs, max_tokens, purpose):
        user = msgs[-1]["content"]
        rec = {"purpose": purpose, "user": user, "t0": time.monotonic(),
               "chars": sum(len(m["content"]) for m in msgs)}
        with self.lock:
            self.calls.append(rec)
        time.sleep(self.delay + (self.verify_delay if purpose == "verify" else 0))
        if purpose == "plan":
            text = self.plan if isinstance(self.plan, str) else json.dumps(self.plan)
        elif purpose == "verify":
            text = self._verdict(user)
        elif purpose == "review":
            text = '{"verdict": "ship", "problems": []}'
        elif purpose == "supervise":
            text = '{"missing": []}'
        else:
            text = ""
        rec["t1"] = time.monotonic()
        return text, ("sub-claude/sonnet" if text else None), (10 if text else 0)

    def _verdict(self, user):
        def one(title):
            q = self.verdicts.get(title)
            if q:
                return q.pop(0)
            return {"ok": True, "problems": []}
        if "JUDGE THESE" in user:
            ents = []
            for n, title in re.findall(r"### PHASE (\d+): (.+)", user):
                e = dict(one(title.strip()))
                e["n"] = int(n)
                ents.append(e)
            return json.dumps({"phases": ents, "missing": [], "parts_missing": []})
        title = re.search(r"TASK: (.+)", user).group(1).strip()
        return json.dumps(one(title))

    def purposes(self):
        return [c["purpose"] for c in self.calls]


def _run(fleet, mgr, **kw):
    events = []
    out = swarm.run([{"role": "user", "content": TALLY}], fleet, manager=mgr,
                    on_event=lambda k, d: events.append((k, d)), **kw)
    out["_events"] = events
    return out


# --------------------------------------------------------------------------- #
# 1. Fewer manager calls for the same plan
# --------------------------------------------------------------------------- #

def test_the_tally_plan_now_costs_three_manager_calls_and_ships_every_part():
    fleet, mgr = Fleet(), Manager()
    out = _run(fleet, mgr)
    # Before: plan + one verdict PER PHASE (3) + supervise + review = 6 calls
    # on the critical path (the live run also paid a 2nd plan call). Now:
    # plan, the tally.py wave's verdict, and ONE verdict for the last wave
    # that also answers the supervisor's coverage question. Every phase passed
    # first time, so no review is paid for on top.
    assert mgr.purposes() == ["plan", "verify", "verify"], mgr.purposes()
    assert out["manager_calls"] == 3
    assert "manager_calls=3" in swarm.trailer_summary(out)
    assert not out.get("unfinished"), out.get("unfinished")
    assert out["planned"] == ["tally.py", "test_tally.py", "README section"]
    assert len(out["phases"]) == 3
    assert "from tally import count, main" in out["text"]
    assert out["text"].count("def test_") >= 5 and "| -l |" in out["text"]
    assert any(k == "supervise" and "last wave" in d for k, d in out["_events"])
    assert any(k == "review" and d.startswith("skipped") for k, d in out["_events"])
    # The final check saw the plan, the earlier work and the user's brief. The
    # tests are PROVEN by their only criterion ("at least 5 tests" -- counted),
    # so only the README is judged; the tests are shown for coverage.
    final = mgr.calls[-1]["user"]
    assert "PLAN\n" in final and "OTHER WORK" in final and "THE USER'S BRIEF" in final
    judged = final.split("JUDGE THESE", 1)[1]
    assert "### PHASE 3: README section" in judged and "### PHASE 2" not in judged
    assert "## 2. test_tally.py" in final.split("JUDGE THESE", 1)[0]
    for c in mgr.calls:
        assert c["chars"] < 16000, (c["purpose"], c["chars"])


def test_a_check_that_answers_coverage_replaces_the_supervisor_call():
    fleet, mgr = Fleet(), Manager()
    _run(fleet, mgr)
    assert "supervise" not in mgr.purposes() and fleet.of("supervise") == []


def test_a_phase_the_free_checks_prove_is_never_sent_for_a_verdict():
    # tally.py with a quoted literal as its only criterion: proven for free.
    plan = json.loads(json.dumps(PLAN))
    plan["phases"][0]["acceptance"] = ['uses "argparse"', 'defines `main()`']
    fleet, mgr = Fleet(plan=plan), Manager(plan=plan)
    out = _run(fleet, mgr)
    assert mgr.purposes() == ["plan", "verify"], mgr.purposes()
    assert "### PHASE 1" not in mgr.calls[-1]["user"].split("JUDGE THESE", 1)[1]
    assert not out.get("unfinished")


def test_an_unusable_manager_plan_is_retried_on_the_free_planner():
    fleet = Fleet()
    mgr = Manager(plan="Sure! First I would write the code, then some tests.")
    out = _run(fleet, mgr)
    assert mgr.purposes().count("plan") == 1           # was 2 paid plan calls
    assert len(fleet.of("plan")) == 1
    assert any(role == "plan:retry" for role, _ in out["models"])
    assert len(out["phases"]) == 3


# --------------------------------------------------------------------------- #
# 2. Paid verdicts overlap free work
# --------------------------------------------------------------------------- #

def test_the_next_wave_works_while_the_previous_wave_is_being_judged():
    fleet, mgr = Fleet(delay=0.05), Manager(verify_delay=0.5)
    t0 = time.monotonic()
    out = _run(fleet, mgr)
    wall = time.monotonic() - t0
    tally_verdict = mgr.calls[1]
    assert tally_verdict["purpose"] == "verify" and "TASK: tally.py" in tally_verdict["user"]
    tests_start = fleet.of("phase", "test_tally.py")[0]["t0"]
    # The tests worker started while tally.py's verdict was still out.
    assert tests_start < tally_verdict["t1"] - 0.2
    # Two 0.5 s verdicts, overlapped with the work: well under their sum plus
    # every free call in sequence.
    assert wall < 1.6, wall
    assert not out.get("unfinished")


def test_a_rejected_dependency_rebuilds_the_phases_built_on_it():
    fleet = Fleet(answers={("retry", "tally.py"): CODE_V2})
    mgr = Manager(verdicts={"tally.py": [{"ok": False, "problems": ["add count_chars()"]}]})
    out = _run(fleet, mgr)
    roles = [r for r, _ in out["models"]]
    assert "phase-retry:tally.py" in roles
    assert "phase-rebuild:test_tally.py" in roles and "phase-rebuild:README section" in roles
    rebuilt = fleet.of("phase", "test_tally.py")[-1]["user"]
    assert "def count_chars(text)" in rebuilt          # built on the FIXED code
    tally_out = [p for p in out["phases"] if p["title"] == "tally.py"][0]["output"]
    assert "count_chars" in tally_out
    # The retry PASSED its second verdict: what ships was checked, so no
    # separate review -- plan, tally verdict, tally re-verdict, final check.
    assert mgr.purposes() == ["plan", "verify", "verify", "verify"], mgr.purposes()


# --------------------------------------------------------------------------- #
# 3. The tests phase that shipped missing now completes
# --------------------------------------------------------------------------- #

def test_tests_are_wired_to_the_code_and_given_its_exact_interface():
    fleet, mgr = Fleet(), Manager()
    out = _run(fleet, mgr)
    assert any(k == "plan" and d.startswith("wired:") for k, d in out["_events"])
    tests_prompt = fleet.of("phase", "test_tally.py")[0]["user"]
    assert "### Output of phase 1 (tally.py)" in tests_prompt
    assert "INTERFACE OF tally.py (module `tally`)" in tests_prompt
    assert "def count(text)" in tests_prompt and "def main(argv=None)" in tests_prompt
    assert "-l, -w, -c" in tests_prompt
    assert "HOW TO WRITE THESE TESTS" in tests_prompt and "At least 5 test" in tests_prompt
    readme_prompt = fleet.of("phase", "README section")[0]["user"]
    assert "INTERFACE OF tally.py" in readme_prompt
    assert "HOW TO WRITE THESE TESTS" not in readme_prompt
    # The worker started only after tally.py existed.
    assert fleet.of("phase", "test_tally.py")[0]["t0"] >= fleet.of("phase", "tally.py")[0]["t1"]


def test_tests_that_import_a_missing_name_are_repaired_for_free():
    fleet = Fleet(answers={("phase", "test_tally.py"): BAD_TESTS})
    mgr = Manager()
    out = _run(fleet, mgr)
    retry = fleet.of("retry", "test_tally.py")
    assert len(retry) == 1
    prompt = retry[0]["user"]
    assert "count_lines" in prompt and "it defines: count, main" in prompt
    assert "THE REJECTED ATTEMPT" in prompt and "from tally import count_lines" in prompt
    tests = [p for p in out["phases"] if p["title"] == "test_tally.py"][0]["output"]
    assert tests == GOOD_TESTS
    # Caught by a parser, repaired by a free model, proven by its criterion:
    # not one manager call was spent on the bad tests.
    for c in mgr.calls[1:]:
        assert "count_lines" not in c["user"]
    assert "fix" not in mgr.purposes()
    assert not out.get("unfinished")


def test_a_manager_that_cannot_fix_leaves_a_free_repair_pass():
    # Both free attempts miss the test count; the manager has no fix answer.
    few = "```python\nfrom tally import main\n\n\ndef test_one():\n    assert main\n```"
    calls = {"n": 0}

    def tests_answer(user):
        calls["n"] += 1
        return few if calls["n"] <= 2 else GOOD_TESTS
    fleet = Fleet(answers={"test_tally.py": tests_answer})
    out = _run(fleet, Manager())
    roles = [r for r, _ in out["models"]]
    assert "phase-repair:test_tally.py" in roles
    tests = [p for p in out["phases"] if p["title"] == "test_tally.py"][0]["output"]
    assert tests == GOOD_TESTS


# --------------------------------------------------------------------------- #
# 4. Every manager call has its own deadline
# --------------------------------------------------------------------------- #

def test_a_manager_verdict_past_its_deadline_falls_back_and_the_run_moves_on(monkeypatch):
    monkeypatch.setitem(swarm.MANAGER_DEADLINES, "verify", 0.2)
    fleet, mgr = Fleet(), Manager(verify_delay=3.0)
    t0 = time.monotonic()
    out = _run(fleet, mgr)
    assert time.monotonic() - t0 < 2.5
    assert any("no answer within" in d for _k, d in out["_events"])
    assert not out.get("unfinished") and len(out["phases"]) == 3
    # Unjudged phases are not "clean": the final review still runs.
    assert "review" in mgr.purposes() or fleet.of("review")


# --------------------------------------------------------------------------- #
# 5. A first attempt cut by the cap races the fast re-run
# --------------------------------------------------------------------------- #

def test_a_cut_off_first_attempt_can_still_win_the_grace_window():
    plan = {"goal": "g", "phases": [
        {"title": "Quick", "task": "write the quick part", "needs": []},
        {"title": "Slow", "task": "write the slow part", "needs": []}]}

    def slow(user):
        time.sleep(0.8)
        return "slow original answer"
    fleet = Fleet(plan=plan, answers={"Quick": "quick answer", "Slow": slow})

    def fast(msgs, max_tokens, exclude_pids=()):
        time.sleep(2.0)
        return "fast rerun answer", "fast/model"
    t0 = time.monotonic()
    out = swarm.run([{"role": "user", "content": "do two things"}], fleet,
                    max_seconds=0.3, grace_seconds=4, fast_dispatch=fast)
    assert time.monotonic() - t0 < 1.9
    slow_out = [p for p in out["phases"] if p["title"] == "Slow"][0]["output"]
    assert slow_out == "slow original answer"
    assert any(r == "phase:Slow" for r, _ in out["models"])


# --------------------------------------------------------------------------- #
# 6. The helpers, directly
# --------------------------------------------------------------------------- #

def test_code_problems_prove_only_what_a_parser_can():
    code_ph = {"title": "tally.py", "task": "x", "needs": []}
    assert swarm._code_problems(code_ph, CODE_V1) == []
    broken = CODE_V1.replace("def count(text):", "def count(text)")
    assert "not valid Python" in swarm._code_problems(code_ph, broken)[0]
    tests_ph = {"title": "test_tally.py", "task": "at least 5 tests", "needs": [1]}
    deps = {"tally": "\n".join(swarm._py_sources(CODE_V1))}
    assert swarm._code_problems(tests_ph, GOOD_TESTS, deps) == []
    bad = swarm._code_problems(tests_ph, BAD_TESTS, deps)
    assert any("count_lines" in p for p in bad)
    stub = "```python\ndef test_a():\n    assert 1\n```"
    probs = swarm._code_problems(tests_ph, stub, deps)
    assert any("only 1 test function" in p for p in probs)
    assert any("never import tally" in p for p in probs)
    # A README is never parsed as Python, and prose phases are untouched.
    assert swarm._code_problems({"title": "README section", "task": "document tally.py"},
                                "```python\n$ tally -l\n```") == []
    assert swarm._code_problems({"title": "Hero", "task": "write copy"}, "Hi") == []


def test_phase_kinds_are_read_conservatively():
    assert swarm._is_test_phase({"title": "Tests", "task": "pytest for tally.py"})
    assert swarm._is_test_phase({"title": "x", "task": "Write test_tally.py"})
    assert not swarm._is_test_phase({"title": "CLI", "task": "tally.py, easy to unit test"})
    assert not swarm._is_test_phase({"title": "tally.py", "task": "tested by test_tally.py"})
    assert swarm._is_docs_phase({"title": "README section", "task": "usage of tally.py"})
    assert swarm._code_file({"title": "tally.py", "task": "..."}) == "tally.py"
    assert swarm._code_file({"title": "README", "task": "document tally.py"}) == ""


def test_tests_listed_before_the_code_are_moved_after_it():
    phases = [{"title": "test_tally.py", "task": "tests", "needs": []},
              {"title": "tally.py", "task": "code", "needs": []},
              {"title": "Hero", "task": "copy", "needs": [1]}]
    got, notes = swarm.wire_code_deps([dict(p) for p in phases])
    assert [p["title"] for p in got] == ["tally.py", "test_tally.py", "Hero"]
    assert got[1]["needs"] == [1]
    assert got[2]["needs"] == [2]            # still the tests phase, renumbered
    assert notes and "moved after tally.py" in notes[0]
    # Nothing to wire: untouched.
    plain = [{"title": "A", "task": "a", "needs": []}, {"title": "B", "task": "b", "needs": []}]
    assert swarm.wire_code_deps(plain) == (plain, [])


def test_criteria_are_proven_only_when_nothing_is_left_unchecked():
    assert swarm._criterion_proven('includes "Start free"', "Start free now") is True
    assert swarm._criterion_proven('includes "Start free"', "nothing") is False
    assert swarm._criterion_proven("at least 5 tests", GOOD_TESTS) is True
    assert swarm._criterion_proven("defines `main()`", CODE_V1) is True
    assert swarm._criterion_proven('mentions "argparse" and handles missing files',
                                   CODE_V1) is None
    assert swarm._criterion_proven("the tone is friendly", "hi") is None


def test_a_plan_cut_off_mid_value_keeps_every_complete_phase():
    full = json.dumps(PLAN)
    cut = full[:full.index('"acceptance"') + len('"acceptance": ["at le')]
    got = swarm._parse_json(cut)
    assert [p["title"] for p in got["phases"]][:2] == ["tally.py", "test_tally.py"]
    cut2 = full[:full.index('"needs": []}, {"title": "README') + len('"needs": ')]
    assert swarm._parse_json(cut2)["phases"][0]["title"] == "tally.py"


def test_interface_lists_signatures_and_flags():
    iface = swarm._interface("\n".join(swarm._py_sources(CODE_V1)))
    assert "def count(text)" in iface and "def main(argv=None)" in iface
    assert "argparse): -l, -w, -c, file" in iface


# --------------------------------------------------------------------------- #
# 7. The fastest one-shot flags of the claude CLI (app side)
# --------------------------------------------------------------------------- #

HELP = ("Usage: claude [options]\n"
        "  --mcp-config <configs...>   Load MCP servers\n"
        "  --no-session-persistence    Disable session persistence\n"
        "  --disable-slash-commands    Disable all skills\n"
        "  --strict-mcp-config         Only use MCP servers from --mcp-config\n"
        "  --tools <tools...>          Specify the list of available tools\n")


class _Proc:
    returncode = 0
    stderr = ""

    def __init__(self, out):
        self.stdout = out


@pytest.fixture
def app_mod(monkeypatch):
    import app
    app._CLAUDE_HELP_CACHE.clear()
    yield app
    app._CLAUDE_HELP_CACHE.clear()


def test_claude_fast_flags_only_when_the_cli_lists_them(app_mod, monkeypatch):
    runs = []

    def fake_run(argv, **kw):
        runs.append(argv)
        return _Proc(HELP)
    monkeypatch.setattr(app_mod.subprocess, "run", fake_run)
    got = app_mod._claude_fast_args("C:/x/claude.exe")
    assert got == ["--strict-mcp-config", "--no-session-persistence",
                   "--disable-slash-commands", "--tools", ""]
    # Cached: --help runs once per binary.
    app_mod._claude_fast_args("C:/x/claude.exe")
    assert len(runs) == 1
    # A .cmd shim never gets the empty argv element.
    assert "--tools" not in app_mod._claude_fast_args("C:/x/claude.cmd")
    # An old CLI whose --help lists none of them gets none of them.
    app_mod._CLAUDE_HELP_CACHE.clear()
    monkeypatch.setattr(app_mod.subprocess, "run", lambda argv, **kw: _Proc("Usage: claude"))
    assert app_mod._claude_fast_args("C:/x/claude.exe") == []


def test_sub_run_sends_the_fast_flags_to_claude(app_mod, monkeypatch, tmp_path):
    import config
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(tmp_path / "state" / "config.json"))
    config.invalidate_settings_cache()
    monkeypatch.setattr(app_mod, "_sub_master_on", lambda: True)
    monkeypatch.setattr(app_mod, "_sub_state", lambda p: (True, True, True, "ok"))
    monkeypatch.setattr(app_mod, "_sub_bin", lambda p, model=None: "C:/x/claude.exe")
    monkeypatch.setattr(app_mod, "_sub_launcher", lambda path: [path])
    monkeypatch.setattr(app_mod, "_sub_env", lambda p=None, model=None: {})
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv)
        return _Proc(HELP if "--help" in argv else "hello")
    monkeypatch.setattr(app_mod.subprocess, "run", fake_run)
    status, text, _ = app_mod._sub_run("sub-claude", "hi", model="sonnet")
    assert status == 200 and text == "hello"
    argv = seen[-1]
    assert argv[:3] == ["C:/x/claude.exe", "-p", "--output-format"]
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv
    assert argv[argv.index("--model") + 1] == "sonnet"
    config.invalidate_settings_cache()
