r"""In the Multi sessions tier a question is answered, not planned.

MEASURED 2026-09-12: "so all ok ?" in a conversation set to Multi sessions
became a three-phase swarm -- "Run the test suite", "Verify build and static
checks", "Review and finish" -- four minutes of planning (the first plan came
back as garbage and was retried) and two workers, for a three-word question.
The Build page said "Working... 7m 25s"; /activity showed nothing, because
the planner's own requests were the one thing on the hub the feed did not
record.

Now the routes ask first whether the message is work at all; a question, a
thanks, a one-liner goes to the conversation's own ordinary turn with a
notice saying why. And the planner has an activity row like everything else.
"""
import pytest

import app as A


APP = open("app.py", encoding="utf-8").read()


@pytest.mark.parametrize("text", [
    "so all ok ?", "thanks", "ok", "why is the preview blank?", "what did you change?",
    "all done ? i can close ?", "can you fix the zoom?", "",
])
def test_a_question_or_a_word_is_answered_directly(text):
    assert A._multi_wants_a_swarm(text) is False


@pytest.mark.parametrize("text", [
    "fix the pen tool zoom", "continue", "make it blue", "test",
    "Build me a landing page for a bakery in Fez with a menu and a contact form",
    "add a booking page and wire it to the API",
    "x" * 200,
])
def test_work_is_still_split(text):
    assert A._multi_wants_a_swarm(text) is True


def test_both_routes_ask_first():
    stream = APP[APP.index("def api_agent_send_message_stream("):]
    stream = stream[:stream.index("\n@app.route")]
    assert 'sess_info.get("quality") == "multi" and _multi_wants_a_swarm(text)' in stream
    assert "Answered directly -- a short question is not a job" in stream
    plain = APP[APP.index("def api_agent_send_message("):]
    plain = plain[:plain.index("\n@app.route")]
    assert '_multi_wants_a_swarm(body["text"])' in plain


def test_the_direct_answer_is_the_ordinary_durable_turn():
    """Same recording, same live buffer, same memory bookkeeping as any turn;
    the notice is the only addition, and it is not buffered twice."""
    stream = APP[APP.index("def _direct():"):]
    stream = stream[:stream.index("events = _direct()")]
    assert "agentic_chat.send_message_stream_durable(session_id, text)" in stream
    assert "live_run" not in stream


def test_the_planner_is_on_the_activity_feed():
    body = APP[APP.index("def _swarm_windows_planner("):]
    body = body[:body.index("\ndef ")]
    assert '_act_begin("build" if _build_sid() else "hub", "plan"' in body
    assert "_act_end(act, bool(text))" in body
    assert "_act_end(act, False)" in body


def test_a_hub_made_row_looks_like_the_others():
    with A.app.test_request_context("/"):
        act = A._act_begin("hub", "plan")
        assert act["status"] == "in_progress" and act["model_req"] == "plan"
        assert A.g.act is act
        A._act_pick("groq", "qwen/qwen3.8-27b")
        assert act["provider"] == "groq"
        A._act_end(act, True)
        assert act["status"] == "ok" and act["http"] == 200 and act["finished"]
        assert "duration_ms" in act
        assert A.g.act is None
    with A._activity_lock:
        assert any(a is act for a in A._activity)
        A._activity.remove(act)


def test_a_failed_plan_is_an_error_row():
    with A.app.test_request_context("/"):
        act = A._act_begin("hub", "plan")
        A._act_end(act, False)
        assert act["status"] == "error" and act["http"] == 502
    with A._activity_lock:
        A._activity.remove(act)
