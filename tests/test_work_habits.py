"""Four work habits, taken from a "war council" style agent prompt and made MODEL-AGNOSTIC.

The prompt that inspired them names one model, one CLI and a persona. None of that is wanted here:
each habit is a plain instruction (or a plain nudge) that any model in any CLI can follow, and a
test pins that no model, vendor or CLI name leaks into the new texts.

  1. BASELINE   -- the plan brief (craft.PLAN_PHASES): run what covers the code BEFORE editing, so a
                   failure that was already there is not blamed on the change.
  2. RED TEAM   -- the code crew's reviewer and the Multi "Review and finish" phase hunt the classes
                   a reviewer usually skips (races, leaks, swallowed errors, tests that pass for the
                   wrong reason).
  3. ORDERS     -- when the request really OFFERS a delegating tool, the sub-agent hint says what an
                   order contains and that a sub-agent's report is a claim (flag agent_order_template).
  4. NUDGE      -- an auto-continue after a turn with NO new progress asks for a different approach or
                   an honest blocker, not the bare Continue text again (flag stall_nudge).

Hermetic: no network, no CLI, no thread outlives a test.
"""
import os
import re
import shutil
import tempfile

import pytest

import app as A
import config
import craft
import crews
import swarm_windows as SW

NAMES = ("claude", "opus", "sonnet", "haiku", "gpt", "openai", "gemini", "kimi", "glm", "deepseek",
         "qwen", "opencode", "codex", "anthropic", "war council", "orchestrator")


@pytest.fixture
def isolated_config(monkeypatch):
    root = tempfile.mkdtemp(prefix="hub-pytest-")
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", os.path.join(root, "state", "config.json"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _tool(name):
    return {"type": "function", "function": {"name": name}}


def _no_names(text):
    low = text.lower()
    return [n for n in NAMES if n in low]


# --------------------------------------------------------------------------- #
# 1. BASELINE
# --------------------------------------------------------------------------- #

def _baseline_line():
    lines = [l for l in craft.PLAN_PHASES.split("\n") if l.startswith("- BASELINE")]
    assert len(lines) == 1
    return lines[0]


def test_the_plan_brief_asks_for_a_baseline_before_editing():
    line = _baseline_line()
    assert "before editing" in line
    assert "run the tests/build that cover it" in line
    assert "note what already fails" in line
    assert "a failure that was there first is not yours" in line


def test_the_baseline_reaches_every_tool_carrying_opening_turn_and_never_a_tool_less_one():
    msg = craft.system_message("fix the failing parser test in my project", tools=True)
    assert msg and "BASELINE" in msg["content"]
    plain = craft.system_message("fix the failing parser test in my project", tools=False)
    assert plain is None or "BASELINE" not in plain["content"]


def test_the_baseline_is_planned_before_the_work_and_the_loop_stays_last():
    text = craft.PLAN_PHASES
    assert text.index("- DRY-RUN") < text.index("- BASELINE") < text.index("- Execute it")
    body = craft.system_message("fix the failing parser test", tools=True)["content"]
    assert body.index(craft.PLAN_PHASES) < body.index(craft.ACT_RUN) < body.index(craft.VERIFY_RUN)


def test_the_progress_rules_the_old_last_bullet_carried_survive_the_tightening():
    t = craft.PLAN_PHASES
    assert "after EACH step" in t and "PROGRESS.md" in t and "stale" in t
    assert "Do not stop between phases to report; the list IS the report." in t
    assert "- [x] or - [~]" in t and "- [~] in progress" in t


def test_the_plan_brief_stays_inside_its_new_budget():
    assert len(craft.PLAN_PHASES) / 4 <= 270


# --------------------------------------------------------------------------- #
# 2. RED TEAM
# --------------------------------------------------------------------------- #

def test_the_code_reviewer_hunts_what_a_reviewer_usually_skips():
    rv = crews.CREWS["code"]["review_system"]
    for phrase in ("race conditions", "check-then-act on shared state", "leaked resources",
                   "errors swallowed or retried without limit",
                   "tests that pass for the wrong reason", "the real logic mocked away"):
        assert phrase in rv, phrase


def test_the_code_reviewer_keeps_its_contract_and_its_restraint():
    rv = crews.CREWS["code"]["review_system"]
    assert '"verdict": "ship" | "revise"' in rv
    assert "do not manufacture criticism" in rv
    assert "Do not report style preferences" in rv


def test_only_the_code_reviewer_got_the_code_checklist():
    for name in ("research", "write", "design"):
        assert "race conditions" not in crews.CREWS[name]["review_system"], name


def test_the_multi_review_phase_attacks_the_work_once():
    t = " ".join(SW._REVIEW_TASK.split())          # the source wraps its lines
    assert "Then attack the work once" in t
    assert "tests that pass for the wrong reason" in t
    assert "A real defect gets a fix and a regression test." in t
    # the old job is intact
    assert "Fix what you find." in t and "Look for: files that reference something nobody created" in t
    assert t.index("Look for:") < t.index("attack the work once") < t.index("Fix what you find.")


# --------------------------------------------------------------------------- #
# 3. ORDERS
# --------------------------------------------------------------------------- #

OLD_HINT = ("SUB-AGENTS: to run work in parallel, actually CALL your `task` tool. Never say you "
            "delegated, spawned sub-agents, or ran work in parallel unless you called it in THIS "
            "turn -- otherwise do the work yourself with your own tools. The user can also pick "
            "the Multi or Swarm tier in their model picker for a full hub-run parallel build.")


def test_an_offered_delegating_tool_comes_with_the_order_template(isolated_config):
    h = A._crew_agent_hint([_tool("read"), _tool("task")])
    for phrase in ("write it an order", "the goal in one sentence",
                   "the files it may touch and the files it must not",
                   "the command that must exit 0 when it is done", "what to report back",
                   "Give parallel agents separate files", "report is a claim",
                   "re-run its command yourself before you say it worked"):
        assert phrase in h, phrase
    assert "`task`" in h and "Never say you delegated" in h          # the old ban stays


def test_the_template_sits_before_the_pointer_to_the_tiers(isolated_config):
    h = A._crew_agent_hint([_tool("task")])
    assert h.index("write it an order") < h.index("The user can also pick")


def test_no_template_without_a_tool_to_delegate_with(isolated_config):
    h = A._crew_agent_hint([_tool("read"), _tool("bash")])
    assert "write it an order" not in h and "SUB-AGENTS" in h


def test_flag_off_keeps_the_previous_hint_byte_for_byte(isolated_config):
    config.set_flag("agent_order_template", False)
    assert A._crew_agent_hint([_tool("task")]) == OLD_HINT


def test_the_template_is_injected_once_on_an_opening_turn(isolated_config):
    msgs = [{"role": "system", "content": "You are a coding agent."},
            {"role": "user", "content": "add a booking page and a test for it"}]
    out = A._apply_craft_brief(msgs, agentic=True, tools=[_tool("task")])
    hints = [m for m in out if m.get("role") == "system"
             and "SUB-AGENTS" in (m.get("content") or "")]
    assert len(hints) == 1 and "write it an order" in hints[0]["content"]
    assert sum((m.get("content") or "").count("write it an order") for m in out) == 1


def test_the_template_never_tells_an_agent_to_post_a_crew(isolated_config):
    h = A._crew_agent_hint([_tool("task")])
    assert "POST" not in h and "crew" not in h.lower()


# --------------------------------------------------------------------------- #
# 4. NUDGE
# --------------------------------------------------------------------------- #

@pytest.fixture
def stall_on(monkeypatch):
    A._STALL_RETRIES.clear()
    real = A.config.get_flag
    monkeypatch.setattr(A.config, "get_flag",
                        lambda name, default=None: True if name == "stall_recheck" else real(name, default))
    yield
    A._STALL_RETRIES.clear()


def _info(**kw):
    base = {"cli": "opencode", "project_dir": "C:/p", "quality": "max", "final_reply": "",
            "used_tools": False, "observed": 0, "interrupted": False, "rejected": False,
            "stopped": False, "errored": False, "instruction": "fix the failing test in parser.py"}
    base.update(kw)
    return base


def test_a_turn_that_did_new_work_is_continued_with_the_plain_text():
    assert A._stall_continue_text(True) == A._CONTINUE_TEXT


def test_a_turn_with_no_progress_gets_the_same_text_plus_the_nudge():
    t = A._stall_continue_text(False)
    assert t.startswith(A._CONTINUE_TEXT) and t.endswith(A._STALL_NUDGE)
    assert t == A._CONTINUE_TEXT + " " + A._STALL_NUDGE
    assert "different approach" in t and "say exactly what blocks you" in t


def test_the_nudged_text_is_still_a_continue_for_the_rest_of_the_hub():
    assert A._multi_is_continue(A._stall_continue_text(False)) is True
    assert A._multi_is_continue(A._stall_continue_text(True)) is True


def test_the_flag_off_sends_the_bare_text_even_after_a_stall(isolated_config):
    config.set_flag("stall_nudge", False)
    assert A._stall_continue_text(False) == A._CONTINUE_TEXT


def test_the_hook_sends_the_nudge_after_an_unproductive_turn(stall_on):
    sent = []
    A._stall_on_turn_end("s-nudge", _info(), send=lambda s, t: sent.append(t), sync=True)
    assert sent == [A._stall_continue_text(False)]
    assert sent[0].endswith(A._STALL_NUDGE)


def test_the_hook_sends_the_plain_text_after_a_turn_that_did_new_work(stall_on):
    sent = []
    A._stall_on_turn_end("s-plain", _info(used_tools=True, tool_sigs=["edit parser.py"],
                                          observed=1, final_reply=""),
                         send=lambda s, t: sent.append(t), sync=True)
    assert sent == [A._CONTINUE_TEXT]


def test_a_chain_is_plain_while_it_works_and_nudged_once_it_stalls(stall_on):
    sent = []

    def go(info):
        A._stall_on_turn_end("s-chain2", info, send=lambda s, t: sent.append(t), sync=True)

    go(_info(used_tools=True, tool_sigs=["edit a.py"], observed=1, final_reply=""))
    go(_info(used_tools=True, tool_sigs=["edit b.py"], observed=1, final_reply=""))
    go(_info(used_tools=True, tool_sigs=["edit b.py"], observed=0, final_reply=""))   # nothing new
    assert sent[:2] == [A._CONTINUE_TEXT, A._CONTINUE_TEXT]
    assert sent[2] == A._stall_continue_text(False)


# --------------------------------------------------------------------------- #
# Model-agnostic: no vendor, model, CLI or persona name in anything new
# --------------------------------------------------------------------------- #

def test_no_model_vendor_cli_or_persona_name_in_the_new_texts():
    reviewer = crews.CREWS["code"]["review_system"]
    attack = SW._REVIEW_TASK[SW._REVIEW_TASK.index("Then attack"):SW._REVIEW_TASK.index("Fix what you find.")]
    new = {
        "baseline": _baseline_line(),
        "order template": A._AGENT_ORDER_TEMPLATE,
        "nudge": A._STALL_NUDGE,
        "review phase": attack,
        "code reviewer": reviewer[reviewer.index("race conditions"):reviewer.index("Do not report style")],
    }
    for label, text in new.items():
        assert _no_names(text) == [], (label, _no_names(text))
    assert re.search(r"[A-Za-z]", A._AGENT_ORDER_TEMPLATE)          # not an empty string
