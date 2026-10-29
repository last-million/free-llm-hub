"""Two owner reports of 2026-10-29.

1. "continue" -> the model answered with a leaked <think> ("... Let me poll the crew status.")
   and "Crew job ... accepted. Polling its status." -- no tool call -- and OpenCode ended the
   turn. The hub served it: its announcement detectors only knew "I'll / let me".
2. Owner order: DeepSeek V4.1 Pro > MiMo V2.6 Pro > DeepSeek V4.1 Flash > GLM 5.3. GLM won
   every tool turn because every "deepseek-v4" id lost a 25-point dialect penalty.
"""
import json

import pytest

import app as A
from test_peek_holds_short_replies import _check, _cut, _peek

THINK = ("<think>\nThe crew job started with job_id 73b89e641792. I need to poll it to see "
         "the status. The user said continue. Let me poll the crew status.\n</think>\n")
VISIBLE = "Crew job 73b89e641792 accepted. Polling its status."
PASTED = THINK + VISIBLE


# --------------------------------------------------------------------------- announcements

def test_the_pasted_reply_is_an_announcement_both_ways():
    assert A._ends_with_announcement(PASTED)
    assert A._looks_like_announced_action(PASTED)


@pytest.mark.parametrize("text", [
    "Polling its status.",
    "Checking the logs now.",
    "Running the tests.",
    "Done with the plan. Reading the config file next.",
    "Now fetching the page.",
    "Loading...",
])
def test_a_bare_progress_line_is_an_announcement(text):
    assert A._ends_with_announcement(text)


@pytest.mark.parametrize("text", [
    "Testing complete.",
    "Running the tests showed 3 failures.",
    "Everything is working.",
    "Nothing to do.",
    "Waiting for your confirmation.",
    "Looking forward to your feedback.",
    "Following the steps above fixed it.",
    "The crew job finished: 4 phases passed. Building is done.",
    "Both tests pass now; I fixed the loop bound in parser.py.",
])
def test_a_report_or_a_deliberate_stop_is_not(text):
    assert not A._ends_with_announcement(text)
    assert not A._looks_like_announced_action(text)


def test_thinking_that_ends_on_an_intent_plus_a_short_status_note():
    t = "<think>I have the job id. Let me poll the crew result.</think>Crew job accepted."
    assert A._ends_with_announcement(t)


def test_thinking_that_ends_on_writing_the_answer_is_a_real_answer():
    t = "<think>I have everything. Let me write the final answer.</think>The bug is on line 12."
    assert not A._ends_with_announcement(t)
    t2 = "<think>Done. Let me summarize.</think>All 6 phases passed."
    assert not A._ends_with_announcement(t2)


def test_a_long_leaked_think_no_longer_switches_the_check_off():
    long_think = "<think>" + ("I am weighing the options carefully. " * 40) + "Let me check it.</think>"
    assert len(long_think) > A._ANNOUNCE_MAX_CHARS
    assert A._ends_with_announcement(long_think + "Checking the file now.")
    assert A._announce_possible("<think>still reasoning about the job")


# --------------------------------------------------------------------------- the real stream

def test_the_pasted_reply_streamed_token_by_token_hands_the_turn_on():
    status, kind = _peek(_cut(PASTED, 3))
    assert status == "nonanswer" and kind == "announced"


def test_the_pasted_reply_in_big_chunks_too():
    status, kind = _peek(_cut(PASTED, 400))
    assert status == "nonanswer" and kind == "announced"


def test_a_real_final_answer_after_a_leaked_think_still_commits():
    final = THINK.replace("Let me poll the crew status.", "The job is done.") + \
        "The crew job finished: 6 phases passed and the report is in lab/notes.md."
    status, _kind = _peek(_cut(final, 3))
    assert status == "content"


def test_a_tool_call_after_the_progress_line_commits():
    status, _kind = _peek(_cut(PASTED, 3), tool_call=True)
    assert status == "content"


def test_without_tools_nothing_is_held():
    status, _kind = _peek(_cut(PASTED, 3), check=_check(tools=False))
    assert status == "content"


# --------------------------------------------------------------------------- the owner order

@pytest.fixture
def neutral(monkeypatch):
    for name in ("_reliability_penalty", "_latency_penalty", "_answer_quality_penalty"):
        monkeypatch.setattr(A, name, lambda p, m: 0.0)
    monkeypatch.setattr(A, "_sustain_penalty", lambda p: 0.0)
    monkeypatch.setattr(A, "_rp_withholds_evidence", lambda p, m: False)
    monkeypatch.setattr(A, "_gpt_below_claude", lambda s, m: s)


def _chat(m):
    return A._benchmark_score("nvidia", m, _rank=False)


def _tool(m):
    return A._agentic_score((_chat(m), "nvidia", m))


ORDER = ["deepseek/deepseek-v4.1-pro", "xiaomi/mimo-v2.6-pro",
         "deepseek-ai/deepseek-v4.1-flash", "z-ai/glm-5.3"]


def test_chat_follows_the_owner_order():
    s = [_chat(m) for m in ORDER]
    assert s == sorted(s, reverse=True) and len(set(s)) == 4
    assert _chat("deepseek-ai/deepseek-v4.1-flash") > _chat("zai-org/GLM-5.3-Flash")


def test_tool_turns_follow_the_owner_order(neutral):
    s = [_tool(m) for m in ORDER]
    assert s == sorted(s, reverse=True) and len(set(s)) == 4
    assert _tool("deepseek-ai/deepseek-v4.1-flash") - _tool("z-ai/glm-5.3") > A._AUTO_TOP_BAND, \
        "DeepSeek V4.1 Flash goes first while healthy, not a coin flip with GLM"


def test_the_order_stays_under_claude_and_kimi(neutral):
    top = _tool("deepseek/deepseek-v4.1-pro")
    assert _tool("anthropic/claude-opus-5.5") > top
    assert _tool("moonshotai/kimi-k3.1-pro") > top
    assert _chat("anthropic/claude-opus-5.5") > _chat("deepseek/deepseek-v4.1-pro")


@pytest.mark.parametrize("m", ["deepseek-ai/deepseek-v4.1-flash", "g4f/srv_x:poe/DeepSeek-V4.1-Flash-T",
                               "deepseek/deepseek-v4.1-pro", "srv_y:deepseek/deepseek-v4-1-flash",
                               "deepseek-v4.2-flash"])
def test_deepseek_v41_and_later_lose_no_dialect_points(m):
    assert A._tool_dialect_penalty(m) == 0.0


@pytest.mark.parametrize("m", ["deepseek/deepseek-v4-pro", "deepseek-ai/DeepSeek-V4-Flash-0731",
                               "minimaxai/minimax-m3.1-flash", "glm-4.7"])
def test_the_older_dialect_evidence_still_applies(m):
    assert A._tool_dialect_penalty(m) == A._TOOL_DIALECT_PENALTY


def test_a_newer_deepseek_flash_still_leads_v41():
    assert _chat("deepseek-v4.2-flash") > _chat("deepseek-ai/deepseek-v4.1-flash")


def test_a_relay_copy_keeps_its_relay_discount():
    assert A._benchmark_score("g4f", "srv_x:deepseek/deepseek-v4.1-flash", _rank=False) \
        < _chat("deepseek-ai/deepseek-v4.1-flash")
