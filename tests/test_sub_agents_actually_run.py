"""Sub-agents actually run, and the hub stops telling models to fake it.

Owner 2026-10-27, across all CLIs/model pickers: "I see it doesn't run anything
and it tells me it ran sub-agents." Two flag-gated fixes, each byte-identical to
before when its flag is off:

1. HONEST SUB-AGENT HINT (`honest_crew_hint`, default on). The old
   `_CREW_AGENT_HINT` told EVERY agentic turn it could "delegate via a tool-free
   crew POST" -- which an agent mid-loop (its request carries tools) cannot do,
   so weak free models narrated delegation instead of performing it. The new
   `_crew_agent_hint(tools)` is built from the request's OWN tools (the
   _cmem_apply_note pattern): it names the real delegating tool when one is
   offered, says there is none when there is not, and -- the load-bearing line --
   bans claiming delegation that did not happen.

2. SWARM TIER RUNS REAL AGENTS (`cli_swarm_runs_agents`, default on). Picking
   "swarm" / "<category>-swarm" in any CLI now resolves to a real parallel
   swarm_windows run (like "multi"), not the single-actor roles path.
"""
import os
import shutil
import tempfile

import pytest

import app as A
import config


@pytest.fixture
def isolated_config(monkeypatch):
    """A throwaway config.json via FREE_LLM_HUB_CONFIG, so config.set_flag in
    these tests never touches the owner's real config (same shape as
    test_crew_auto_escalate.py's fixture)."""
    root = tempfile.mkdtemp(prefix="hub-pytest-")
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", os.path.join(root, "state", "config.json"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Fix 1 -- the honest, tools-derived hint
# --------------------------------------------------------------------------- #

def _tool(name):
    return {"type": "function", "function": {"name": name}}


def test_hint_names_the_delegating_tool_when_offered(isolated_config):
    # opencode `task`, Claude Code `Task`/`Agent`, codex `spawn_agent`, and the
    # hub MCP tools whether bare or client-prefixed.
    for name in ("task", "Task", "Agent", "spawn_agent", "swarm_windows_start",
                 "mcp__free-llm-hub__swarm_windows_start", "free-llm-hub_crew_run"):
        h = A._crew_agent_hint([_tool("read"), _tool(name), _tool("bash")])
        assert "SUB-AGENTS" in h
        assert name in h, (name, h)
        assert "Never say you delegated" in h          # the ban is present


def test_hint_when_none_offered_bans_the_claim_without_asserting_absence(isolated_config):
    # An MCP client often DEFERS its tools, so None does not prove there is no
    # sub-agent tool -- the hint must not say "you cannot delegate", only ban a
    # claim that did not happen.
    h = A._crew_agent_hint([_tool("read"), _tool("bash"), _tool("edit")])
    assert "SUB-AGENTS" in h
    assert "only say you delegated" in h.lower()
    assert "no sub-agent tool" not in h and "cannot delegate" not in h.lower()


def test_hint_never_tells_a_mid_loop_agent_to_POST_a_crew(isolated_config):
    # the exact failure: the old text's unusable instruction is gone.
    h = A._crew_agent_hint([_tool("task")])
    assert "POST" not in h and "crew" not in h.lower()


def test_delegating_tool_detector_is_conservative(isolated_config):
    assert A._delegating_tool_offered([_tool("task")]) == "task"
    assert A._delegating_tool_offered([_tool("Task")]) == "Task"
    assert A._delegating_tool_offered([_tool("Agent")]) == "Agent"      # Claude Code
    assert A._delegating_tool_offered([_tool("spawn_agent")]) == "spawn_agent"
    # hub MCP tools, bare and client-prefixed:
    assert A._delegating_tool_offered([_tool("swarm_windows_start")]) == "swarm_windows_start"
    assert A._delegating_tool_offered(
        [_tool("mcp__free-llm-hub__swarm_windows_start")]) == "mcp__free-llm-hub__swarm_windows_start"
    assert A._delegating_tool_offered([_tool("free-llm-hub_crew_run")]) == "free-llm-hub_crew_run"
    assert A._delegating_tool_offered([_tool("my_subagent_runner")]) == "my_subagent_runner"
    # ordinary tools are never mistaken for delegation:
    assert A._delegating_tool_offered([_tool("read"), _tool("bash"),
                                       _tool("todowrite"), _tool("webfetch")]) is None
    assert A._delegating_tool_offered([]) is None
    assert A._delegating_tool_offered(None) is None


def test_apply_craft_brief_injects_the_honest_hint(isolated_config):
    msgs = [{"role": "system", "content": "You are a coding agent."},
            {"role": "user", "content": "add a booking page"}]
    out = A._apply_craft_brief(msgs, agentic=True, tools=[_tool("task")])
    hints = [m for m in out if m.get("role") == "system"
             and "SUB-AGENTS" in (m.get("content") or "")]
    assert len(hints) == 1
    assert "`task`" in hints[0]["content"]
    # placement: after the caller's system prompt, before the first user msg.
    first_user = next(i for i, m in enumerate(out) if m.get("role") == "user")
    assert 1 <= out.index(hints[0]) < first_user


def test_flag_off_keeps_the_old_crew_text_byte_for_byte(isolated_config):
    config.set_flag("honest_crew_hint", False)
    msgs = [{"role": "user", "content": "add a booking page"}]
    out = A._apply_craft_brief(msgs, agentic=True, tools=[_tool("task")])
    hints = [m for m in out if m.get("role") == "system"
             and "CREW DELEGATION" in (m.get("content") or "")]
    assert len(hints) == 1 and hints[0]["content"] == A._CREW_AGENT_HINT
    assert all("SUB-AGENTS" not in (m.get("content") or "") for m in out)


def test_crew_agent_hint_master_flag_off_injects_nothing(isolated_config):
    config.set_flag("crew_agent_hint", False)
    msgs = [{"role": "user", "content": "add a booking page"}]
    out = A._apply_craft_brief(msgs, agentic=True, tools=[_tool("task")])
    assert all("SUB-AGENTS" not in (m.get("content") or "")
               and "CREW DELEGATION" not in (m.get("content") or "") for m in out)


# --------------------------------------------------------------------------- #
# Fix 3 -- the swarm tier resolves to a real run
# --------------------------------------------------------------------------- #

def test_swarm_resolves_to_a_run_by_default(isolated_config):
    assert A._cm_resolves_to_multi({"model": "multi"}) is True
    assert A._cm_resolves_to_multi({"model": "swarm"}) is True
    assert A._cm_resolves_to_multi({"model": "SWARM"}) is True


def test_swarm_does_not_resolve_when_flag_off(isolated_config):
    config.set_flag("cli_swarm_runs_agents", False)
    assert A._cm_resolves_to_multi({"model": "swarm"}) is False
    # multi is never affected by the swarm flag.
    assert A._cm_resolves_to_multi({"model": "multi"}) is True


def test_other_tiers_never_resolve(isolated_config):
    for m in ("auto", "best", "coding", "crew", "crew-code", "", None):
        assert A._cm_resolves_to_multi({"model": m}) is False, m


def test_coding_swarm_normalizes_to_swarm_on_every_handler_path(isolated_config):
    # The three /v1 handlers normalize the model before the intercept: chat and
    # messages via _apply_category_effort, responses via _mode_and_effort. Both
    # must turn `coding-swarm` into bare `swarm` so _cm_resolves_to_multi fires
    # for every protocol, not only chat.
    with A.app.test_request_context("/v1/chat/completions", method="POST"):
        for norm in (A._apply_category_effort, A._mode_and_effort):
            for mid, want in (("coding-swarm", "swarm"), ("swarm", "swarm"),
                              ("coding-multi", "multi"), ("multi", "multi")):
                b = norm({"model": mid, "messages": []})
                assert b.get("model") == want, (norm.__name__, mid, b.get("model"))
                assert A._cm_resolves_to_multi(b) is True
