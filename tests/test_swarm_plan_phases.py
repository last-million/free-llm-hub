"""Swarm mode asks the agent for a phased plan, with the parallel work marked.

The prose pipeline already plans this way on its own -- swarm._waves groups
phases whose dependencies are satisfied and runs each wave concurrently. A CLI
in swarm mode drives its OWN loop instead, so nothing was asking it to plan at
all; it just started working.

Opening turn only. Re-sending a plan instruction mid-task invites replanning
work that is already finished.
"""
from unittest import mock

import app
import craft


TOOLS = [{"type": "function", "function": {"name": "shell", "parameters": {}}}]


def _capture(messages):
    """Run _swarm_tool_turn far enough to see what it would have sent."""
    seen = {}

    def fake_route(msgs, *a, **k):
        seen["messages"] = msgs
        return (None, None, "hard")          # stop right after the injection

    with mock.patch.object(app, "_route_by_difficulty", side_effect=fake_route), \
            app.app.test_request_context("/v1/chat/completions"):
        app._swarm_tool_turn({"model": "swarm", "messages": messages, "tools": TOOLS})
    return seen.get("messages") or []


def test_the_opening_turn_is_asked_to_plan():
    """MOVED 2026-08-31. The swarm used to inject the plan itself, which is
    exactly why it existed in swarm mode ALONE while Normal and Max got none
    ("i dont see the todolists and phases in work"). It now ships from
    craft.system_message() for every tool-carrying opening turn, and a swarm
    tool turn reaches that same injector like any other -- so the assertion
    moves to where the block is now produced. See test_plan_in_every_mode.py."""
    body = craft.system_message("build me a landing page", tools=True)["content"]
    assert "PLAN FIRST" in body


def test_the_swarm_no_longer_adds_its_own_copy():
    """It goes through the shared injector, so a second copy would be pure
    duplication in the mode that pays for its context three times over."""
    out = _capture([{"role": "user", "content": "build me a landing page"}])
    joined = " ".join((m.get("content") or "") for m in out if m.get("role") == "system")
    assert "PLAN FIRST" not in joined


def test_the_plan_asks_for_phases_dependencies_and_parallelism():
    """The three things that make it a plan rather than a wish."""
    low = craft.PLAN_PHASES.lower()
    assert "phase" in low
    assert "needs" in low or "need" in low
    assert "same time" in low, "parallel work must be called out explicitly"
    assert "done when" in low, "each phase needs an observable completion test"


def test_it_tells_the_agent_not_to_stop_between_phases():
    """The whole point, given the reported behaviour: 13 turns that each did one
    thing and stopped."""
    assert "do not stop" in craft.PLAN_PHASES.lower()


def test_a_mid_task_turn_is_not_asked_to_replan():
    out = _capture([
        {"role": "user", "content": "build me a landing page"},
        {"role": "assistant", "tool_calls": [
            {"id": "1", "type": "function",
             "function": {"name": "shell", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "ok"},
        {"role": "user", "content": "continue"},
    ])
    joined = " ".join((m.get("content") or "") for m in out if m.get("role") == "system")
    assert "PLAN FIRST" not in joined


def test_the_agents_own_system_prompt_still_comes_first():
    out = _capture([{"role": "system", "content": "you are codex"},
                    {"role": "user", "content": "build it"}])
    assert out[0].get("content") == "you are codex", \
        "the caller's own instructions must keep priority"


def test_it_can_be_turned_off():
    with mock.patch.object(app.config, "get_flag",
                           side_effect=lambda n, d=False: False if n == "swarm_plan_phases" else d):
        out = _capture([{"role": "user", "content": "build me a landing page"}])
    joined = " ".join((m.get("content") or "") for m in out if m.get("role") == "system")
    assert "PLAN FIRST" not in joined


def test_the_prose_pipeline_already_runs_waves_concurrently():
    """Guards the claim the CLI-side instruction is modelled on."""
    import io, os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = io.open(os.path.join(root, "swarm.py"), encoding="utf-8").read()
    assert "def _waves(phases)" in src
    assert "ThreadPoolExecutor" in src
    assert "RUN AT THE SAME TIME" in src, "the planner must still ask for parallelism"
