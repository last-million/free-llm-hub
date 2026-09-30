"""A Multi run follows the category and effort the user selected.

MEASURED 2026-09-30, session 47a25faa (Multi + coding): the planner gave each
worker its own category ("vision", "coding", none for "Review and finish" --
which then ran under "all"), and a turn the run did not split went out as the
Normal tier ("coding"), not the top one.
"""
import agentic_chat as AC
import app as A
import swarm_windows as SW


def test_the_selected_category_limits_the_plan():
    plan = {"phases": [{"title": "Look at the images", "task": "inspect", "mode": "vision"},
                       {"title": "Fix it", "task": "fix", "mode": "coding"},
                       {"title": "Review and finish", "task": "review"}]}
    phases = SW.clean_phases(plan, modes=("coding",))
    assert [p["mode"] for p in phases] == [None, "coding", None]


def test_a_phase_without_a_category_runs_in_the_selected_one():
    run = SW._Run("goal", ".", "opencode",
                  SW.clean_phases({"phases": [{"title": "Review and finish", "task": "review"}]}),
                  modes=("coding",), default_mode="coding")
    agent = run.agents[0] if isinstance(run.agents, list) else list(run.agents.values())[0]
    seen = []
    SW._run_agent_once(run, agent, spawn=lambda cli, d: "sid-1",
                       run_turn=lambda sid, prompt: iter(()),
                       configure=lambda sid, mode: seen.append((sid, mode)))
    assert seen == [("sid-1", "coding")]


def test_the_default_survives_a_restart():
    run = SW._Run("goal", ".", "opencode", SW.clean_phases({"phases": [{"title": "a", "task": "t"}]}),
                  default_mode="coding")
    assert run.row()["default_mode"] == "coding"


def test_without_a_selection_the_planner_still_chooses():
    plan = {"phases": [{"title": "Look", "task": "inspect", "mode": "vision"},
                       {"title": "Fix", "task": "fix", "mode": "coding"}]}
    phases = SW.clean_phases(plan, modes=("coding", "vision", "seo"))
    assert [p["mode"] for p in phases] == ["vision", "coding"]


def test_the_session_category_becomes_the_workers_category():
    assert A._multi_worker_modes({"mode": "coding"}) == ("coding",)
    assert A._multi_worker_modes({"mode": "all"}) == A._worker_mode_keys()
    assert A._multi_worker_modes({}) == A._worker_mode_keys()
    assert A._multi_worker_modes({"mode": "no-such-category"}) == A._worker_mode_keys()


def test_both_multi_paths_pass_the_selection():
    src = open("app.py", encoding="utf-8").read()
    body = src[src.index("def _multi_turn_events("):]
    body = body[:body.index("\ndef ", 10)]
    assert body.count("modes=_multi_worker_modes(sess_info)") == 2
    assert body.count("default_mode=_session_mode_or_none(sess_info)") == 2
    assert "modes=_worker_mode_keys()" not in body


def test_an_unsplit_multi_turn_is_the_top_tier_of_the_category():
    assert AC._hub_model_for("multi", "coding") == "coding-max"
    assert AC._hub_model_for("multi") == "best"
