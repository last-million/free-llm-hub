"""Multi runs its helpers side by side, and never splits "find it" from "fix it".

Owner, 2026-09-30: "multi session mode should also do jobs in parallel agents
to speed up". MEASURED the same day, run swarm-4f2aab7204a4: four phases,
waves [[1], [2], [3], [4]] -- "Locate code and diagnose contour defects" ->
"Implement contour fixes" -> verify -> review, strictly one after another.
The machine allowed 4 helpers at once; the PLAN was the bottleneck.
"""
import swarm_windows as SW


def _plan(*phases):
    return {"phases": [dict(title=t, task="do " + t, needs=n) for t, n in phases]}


def test_a_diagnose_then_fix_chain_becomes_one_helper():
    phases = SW.clean_phases(_plan(
        ("Locate code and diagnose contour defects", []),
        ("Implement contour fixes", [1]),
        ("Regression verification and visual QA", [2])))
    assert [p["title"] for p in phases] == ["Implement contour fixes",
                                            "Regression verification and visual QA"]
    assert phases[0]["task"].startswith("First -- Locate code and diagnose")
    assert "Then -- do Implement contour fixes" in phases[0]["task"]
    assert [p["needs"] for p in phases] == [[], [1]]


def test_a_look_phase_that_feeds_several_phases_stays():
    phases = SW.clean_phases(_plan(
        ("Investigate the API", []),
        ("Build the backend", [1]),
        ("Build the frontend", [1])))
    assert len(phases) == 3                       # its result is shared: keep it


def test_a_phase_that_acts_is_never_folded():
    phases = SW.clean_phases(_plan(
        ("Find and fix the zoom bug", []),
        ("Write tests for the zoom", [1])))
    assert len(phases) == 2


def test_independent_phases_keep_running_side_by_side():
    phases = SW.clean_phases(_plan(
        ("Build the backend", []), ("Build the frontend", []), ("Write docs", [])))
    assert [p["needs"] for p in phases] == [[], [], []]            # one wave, 3 at once


def test_needs_are_renumbered_after_a_merge():
    phases = SW.clean_phases(_plan(
        ("Build the backend", []),
        ("Diagnose the slow query", []),
        ("Optimise the slow query", [2]),
        ("Review everything", [1, 3])))
    assert [p["title"] for p in phases] == ["Build the backend", "Optimise the slow query",
                                            "Review everything"]
    assert [p["needs"] for p in phases] == [[], [], [1, 2]]


def test_the_planner_is_told_what_makes_a_plan_fast():
    assert "SLOWEST plan" in SW._PLAN_SYSTEM
    assert 'Never plan "diagnose X" followed by "fix X"' in SW._PLAN_SYSTEM
    assert "SLOWEST plan" in SW._PLAN_SYSTEM_MANAGED        # the managed planner too
