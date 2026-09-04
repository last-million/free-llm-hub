"""A swarm that cannot answer must not take the turn down with it.

REPORTED 2026-09-04: "in last work i see error 503 in all last requests swarm".
Five consecutive /agent build turns from opencode, back to back between 02:35
and 02:49, every one HTTP 503 after ~155 seconds with no model recorded:

    cli OpenCode | model_req swarm | protocol openai | stream false
    503 after 156 / 157 / 152 / 168 / 162 seconds, model null, provider null

Three separate defects behind one symptom.

1. THE FATAL ONE. _swarm_completion's own comment says "Falling through to
   ordinary routing when nothing usable comes back is deliberate: a swarm that
   cannot answer must not take the turn down with it", and _swarm_tool_result's
   docstring promises the same thing twice. The code returned a hard 503
   instead. So choosing Swarm made a turn STRICTLY more likely to die than
   choosing Normal -- the better mode was the riskier one, which is exactly
   backwards.

2. IT WAS SILENT. The "N/M models answered" log line sits AFTER the early
   `if not results: return None`, so a run where nothing answered left no trace.
   Five failing turns produced an empty log and a 503 naming no model.

3. THE DEADLINE WAS FOR A DIFFERENT WORKLOAD. _SWARM_HOP_DEADLINE = 150 was
   measured against prose pipeline stages -- short, self-contained, many per
   run. A CLI agent turn carries the full system prompt, every tool schema the
   CLI declares and the whole conversation, dispatched NON-STREAMING so the
   model must finish generating before a byte returns. The five durations
   clustered just above 150s, which is what a deadline looks like when it is the
   thing doing the killing.
"""
from contextlib import ExitStack
from unittest import mock

import pytest

import app as A


@pytest.fixture
def client():
    return A.app.test_client()


TOOLS = [{"type": "function", "function": {"name": "write_file"}}]


def _body(**kw):
    b = {"model": "swarm", "messages": [{"role": "user", "content": "build it"}]}
    b.update(kw)
    return b


def _completion(content="done"):
    return {"id": "x", "object": "chat.completion", "model": "groq/q",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}]}


# --------------------------------------------------------------------------- #
# The fix: fall through, do not 503
#
# These call _swarm_completion DIRECTLY rather than posting to the route.
# Mocking _chat_completions_uncached and then posting intercepts the ROUTE's own
# first call to it, so the swarm branch inside never runs and the mock records
# the original request -- which is how the first draft of these tests "passed"
# while proving nothing.
# --------------------------------------------------------------------------- #

def _run_swarm(body, *patches):
    """_swarm_completion in a request context, with the single-model path mocked
    so the fallback is observable. Returns (response, single_model_mock)."""
    single = mock.Mock(side_effect=lambda b: (A.jsonify(_completion()), 200))
    with ExitStack() as stack:
        stack.enter_context(A.app.test_request_context(json=body))
        stack.enter_context(mock.patch.object(A, "_chat_completions_uncached", single))
        stack.enter_context(mock.patch.object(A, "_act_pipeline_watcher",
                                              mock.Mock(return_value=None)))
        stack.enter_context(mock.patch.object(A, "_act_pipeline_result",
                                              mock.Mock(return_value=None)))
        for target, attr, value in patches:
            stack.enter_context(mock.patch.object(target, attr, value))
        return A._swarm_completion(body), single


def _dead_fanout():
    return (A, "_swarm_tool_turn", mock.Mock(return_value=None))


def test_a_tool_swarm_that_answers_nothing_falls_back_to_one_model():
    """The whole bug: this used to be a 503 that killed the build."""
    rv, single = _run_swarm(_body(tools=TOOLS), _dead_fanout())
    assert single.called, "it must actually try a single model"
    status = rv[1] if isinstance(rv, tuple) else 200
    assert status == 200


def test_the_fallback_asks_for_the_strongest_models():
    """Asking for Swarm is asking for the best available; that intent should
    survive the fan-out failing."""
    _rv, single = _run_swarm(_body(tools=TOOLS), _dead_fanout())
    assert single.call_args[0][0]["model"] == "best"


def test_the_fallback_keeps_the_tools():
    """An agent turn without its tools is useless -- it would answer in prose
    and the CLI would execute nothing."""
    _rv, single = _run_swarm(_body(tools=TOOLS), _dead_fanout())
    assert single.call_args[0][0]["tools"] == TOOLS


def test_the_fallback_keeps_the_conversation():
    _rv, single = _run_swarm(_body(tools=TOOLS), _dead_fanout())
    assert single.call_args[0][0]["messages"][0]["content"] == "build it"


def test_a_working_swarm_is_untouched():
    """The fallback must only ever run when the fan-out produced nothing."""
    served = ({"served": True}, {})
    _rv, single = _run_swarm(
        _body(tools=TOOLS),
        (A, "_swarm_tool_turn", mock.Mock(return_value=served)))
    assert not single.called


def test_the_prose_pipeline_falls_back_too():
    """Same dead end on the non-tool path: a pipeline that produced no text
    used to 503 rather than try one model."""
    _rv, single = _run_swarm(
        _body(),
        (A.swarm, "run", mock.Mock(return_value={})),
        (A.swarm, "format_answer", mock.Mock(return_value="")))
    assert single.call_args[0][0]["model"] == "best"


def test_the_fallback_cannot_loop_forever():
    """'best' must not itself route back into the swarm, or a failing fan-out
    would recurse until the stack gave out."""
    assert not A._is_swarm_model("best")
    assert A._is_swarm_model("swarm")


def test_no_swarm_path_still_returns_a_bare_503():
    """The exact string that used to end these builds."""
    src = open("app.py", encoding="utf-8").read()
    assert "No model could serve this tool-calling turn right now." not in src
    assert "Every model in the swarm failed" not in src


# --------------------------------------------------------------------------- #
# It is no longer silent
# --------------------------------------------------------------------------- #

def test_a_zero_of_n_fan_out_is_logged():
    """It logged nothing, so five dead turns left an empty log -- the hardest
    possible shape to diagnose."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index("if not results:")
    window = src[i:i + 700]
    assert "_log.warning" in window
    assert "0/%d models answered" in window


def test_the_zero_log_names_the_models_that_were_asked():
    src = open("app.py", encoding="utf-8").read()
    i = src.index("0/%d models answered")
    assert "for p, m in picks" in src[i:i + 400]


def test_the_fallback_itself_is_logged():
    src = open("app.py", encoding="utf-8").read()
    assert "falling back to single-model routing" in src


# --------------------------------------------------------------------------- #
# The deadline
# --------------------------------------------------------------------------- #

def test_the_tool_fan_out_has_its_own_deadline():
    """A CLI agent turn is not a prose stage; 150s was measured for the latter."""
    assert A._SWARM_TOOL_HOP_DEADLINE > A._SWARM_HOP_DEADLINE


def test_it_is_long_enough_for_the_measured_failures():
    """152/156/157/162/168 seconds, all 0-of-5 -- every one of those members
    would now be inside the budget."""
    assert A._SWARM_TOOL_HOP_DEADLINE >= 200


def test_it_is_still_bounded():
    """Unbounded, one provider trickling keepalives holds the turn open
    indefinitely -- the 24-minute hostage the original note records."""
    assert A._SWARM_TOOL_HOP_DEADLINE <= 600


def test_the_fan_out_uses_the_tool_deadline():
    src = open("app.py", encoding="utf-8").read()
    body = src.split("def _swarm_tool_result(", 1)[1]
    body = body[:body.index("\ndef ")]
    assert "_SWARM_TOOL_HOP_DEADLINE" in body


def test_the_prose_stages_keep_the_short_one():
    """Many stages per run, so a tight per-stage bound is what stops a crew
    stalling; only the tool path needed the longer budget."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index("def _swarm_dispatch")
    assert "_SWARM_HOP_DEADLINE" in src[i:i + 1500]


# --------------------------------------------------------------------------- #
# A refusal is not an answer
# --------------------------------------------------------------------------- #

def test_the_fan_out_rejects_a_member_that_refuses():
    """How the reported build actually ended. MEASURED 2026-09-04 02:51:34:
    "1/5 models answered, 0 used a tool" -- the one member that replied wrote
    "Blocked. Every tool call needs approval ... run `/permissions` and allow
    Write, Edit, Bash(python:*)". The session was OPENCODE, which has no
    /permissions command, no Edit tool and no Bash(...) syntax: the model
    invented a Claude Code refusal. Nothing rejected it, so it won the slot and
    became the turn's answer -- and the user read it as the hub denying
    permissions."""
    src = open("app.py", encoding="utf-8").read()
    body = src.split("def _swarm_tool_result(", 1)[1]
    body = body[:body.index("\ndef ")]
    assert "_looks_like_refusal" in body


def test_the_refusal_check_only_applies_without_tool_calls():
    """A model that refuses in prose AND calls a tool has still done the work;
    only the prose-only refusal is a dead slot."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index('if not msg.get("tool_calls") and _looks_like_refusal')
    assert i > 0


def test_a_refusing_member_is_recorded_as_a_non_answer():
    """So the ledger learns it, and _swarm_rank stops handing it slots."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index('if not msg.get("tool_calls") and _looks_like_refusal')
    assert "_note_nonanswer" in src[i:i + 200]


# --------------------------------------------------------------------------- #
# It stops waiting once the turn is answerable
# --------------------------------------------------------------------------- #

def test_it_no_longer_waits_for_every_member():
    """MEASURED 2026-09-04 on a real tool turn: three of five answered, at 5s,
    78s and 111s -- and the request took 297 SECONDS, because the two that never
    answered burned the whole budget. Three extra minutes for an answer that had
    been sitting there since 111s."""
    src = open("app.py", encoding="utf-8").read()
    body = src.split("def _swarm_tool_result(", 1)[1]
    body = body[:body.index("\ndef ")]
    assert "ex.map(_run" not in body, "ex.map waits for the slowest member"
    assert "FIRST_COMPLETED" in body


def test_the_grace_starts_only_once_a_tool_was_called():
    """A prose-only answer must NOT start it: waiting longer is exactly right
    while the only thing on the table is something the CLI cannot execute."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index("_SWARM_STRAGGLER_GRACE)")
    window = src[max(0, i - 400):i]
    assert 'get("tool_calls")' in window


def test_the_grace_can_never_exceed_the_deadline():
    src = open("app.py", encoding="utf-8").read()
    i = src.index("_SWARM_STRAGGLER_GRACE)")
    assert "min(deadline" in src[max(0, i - 200):i + 60]


def test_the_grace_is_long_enough_for_a_close_second():
    assert 10 <= A._SWARM_STRAGGLER_GRACE <= 60


def test_the_executor_is_not_joined_on_the_way_out():
    """ThreadPoolExecutor.__exit__ calls shutdown(wait=True), which would join
    the very threads this stopped waiting for and undo the whole change."""
    src = open("app.py", encoding="utf-8").read()
    body = src.split("def _swarm_tool_result(", 1)[1]
    body = body[:body.index("\ndef ")]
    assert "shutdown(wait=False, cancel_futures=True)" in body
    assert "with concurrent.futures.ThreadPoolExecutor" not in body


def test_a_member_that_raises_does_not_lose_the_others():
    src = open("app.py", encoding="utf-8").read()
    body = src.split("def _swarm_tool_result(", 1)[1]
    body = body[:body.index("\ndef ")]
    i = body.index("fut.result()")
    assert "except Exception" in body[i:i + 200]
