"""A bare "how can I help you today?" is not an answer to a concrete task.

MEASURED 2026-10-24 (opencode `coding-swarm`, session ses_e9602c99, 01:46/01:49):
the user sent a concrete 372-char instruction ("start with whitebit and go exploit
all of..."); the real models stalled and a g4f relay's gpt-5 -- which never took
the conversation in -- answered with a generic offer to help:

    "Sure-how can I help today? Here are some common things I can do: ..."
    "What would you like me to help you with today? If you need me to run..."

Both are clean prose with no tool call, so the hub served them as the final
answer, the CLI's turn ended, and the user had to retype the task. The rule is
deliberately narrow: TOOL turns and RELAY hops only, a short reply, the offer at
its very start, and the user's own turn must be a real instruction (never a
greeting or a "what can you do" question, where an offer IS the right answer).
Flag-free (it rides inside `inspect`'s existing checks array, like tool_deadend).
"""
import copy

import pytest

import answer_check as a

REAL_OFFER_1 = ("Sure-how can I help today? Here are some common things I can "
                "do: explain a concept or code snippet, help debug an issue, "
                "or write some code for you.")
REAL_OFFER_2 = ("What would you like me to help you with today? If you need "
                "me to run a specific command, fetch information, or write "
                "code, just let me know.")
TASK = ("go man but whitechain-bridge we alrady worked on it, so go man start "
        "with whitebit and go to epxloit all of the scope now please")


def test_both_measured_replies_are_rejected():
    for text in (REAL_OFFER_1, REAL_OFFER_2):
        assert a.is_idle_greeting(text, TASK), text


def test_a_real_answer_to_the_same_task_is_not_rejected():
    text = "Done. I ran the scan and found 3 issues in auth.py."
    assert not a.is_idle_greeting(text, TASK)


def test_the_offer_is_the_right_answer_to_a_greeting():
    for prompt in ("hi", "hello there", "hey"):
        assert not a.is_idle_greeting(REAL_OFFER_2, prompt), prompt


def test_the_offer_is_the_right_answer_when_asked_what_the_assistant_can_do():
    for prompt in ("what can you do?", "what are your capabilities?",
                   "how can you help me with this project?"):
        assert not a.is_idle_greeting(REAL_OFFER_2, prompt), prompt


def test_a_short_task_still_counts_as_an_instruction():
    # >= _IDLE_MIN_PROMPT_WORDS (5) and not itself a "what can you do" ask.
    assert a.is_idle_greeting(REAL_OFFER_2, "fix the auth bug in server.py now")


def test_an_answer_that_ends_with_an_offer_is_still_an_answer():
    # Found by the full suite (tests/test_double_answer.py): content first, the offer last.
    text = ("The last line of big3.txt is: THE END. Nothing else needed. "
            "Next: what would you like to do?")
    assert not a.is_idle_greeting(text, "What is the last line of big3.txt please?")


def test_a_refusal_that_says_it_is_here_to_help_is_not_a_bare_greeting():
    # Found by the full suite (tests/test_no_tools_claim.py): this is a no-tools claim,
    # which has its own rule -- it must not be reclassified as an idle greeting.
    text = ("I'm here to help, but I currently don't have the tools to execute code or "
            "access your files directly. However, you can run the command yourself and "
            "share the output with me, and I'll help you interpret it.")
    assert not a.is_idle_greeting(text, "List the files in this folder please now")


def test_an_opening_offer_after_a_short_hello_is_still_caught():
    assert a.is_idle_greeting("Hello! How can I help you today?", TASK)


def test_offer_buried_past_the_head_window_is_left_alone():
    text = "Here is the plan:\n\n" + ("x " * 150) + REAL_OFFER_2
    assert not a.is_idle_greeting(text, TASK)


def test_an_overlong_reply_is_left_alone():
    text = REAL_OFFER_2 + (" more text" * 200)
    assert not a.is_idle_greeting(text, TASK)


@pytest.mark.parametrize("bad", [None, "", 7, {}, []])
def test_garbage_never_raises(bad):
    assert a.is_idle_greeting(bad, TASK) in (True, False)


def test_reads_as_answer_never_releases_the_offer_early():
    assert not a.reads_as_answer(REAL_OFFER_2, last_prompt=TASK, relay=True)
    # A genuine answer of similar length still releases.
    genuine = ("The scan is done. I found three dead-end tool calls in the "
               "relay layer and fixed the routing so the chain moves on.")
    assert a.reads_as_answer(genuine, last_prompt=TASK, relay=True)


def test_inspect_rejects_it_on_a_relay_tool_turn():
    v = a.inspect(REAL_OFFER_1, prompt_text=TASK, last_prompt=TASK,
                  tools_offered=True, relay=True)
    assert v["ok"] is False
    assert "idle_greeting" in v["reasons"]


def test_inspect_rejects_it_on_a_direct_providers_tool_turn_too():
    # Unlike provider_notice/tool_deadend, this applies on direct providers too:
    # an idle greeting to a concrete task is wrong wherever it comes from.
    v = a.inspect(REAL_OFFER_1, prompt_text=TASK, last_prompt=TASK,
                  tools_offered=True, relay=False)
    assert v["ok"] is False
    assert "idle_greeting" in v["reasons"]


def test_inspect_lets_it_pass_on_a_tool_free_relay_chat_turn():
    # Neither relay nor tools_offered -> the rule does not apply (see inspect()).
    v = a.inspect(REAL_OFFER_1, prompt_text=TASK, last_prompt=TASK,
                  tools_offered=False, relay=False)
    assert "idle_greeting" not in v["reasons"]


def test_garbage_never_mutates_the_input():
    before = copy.deepcopy(REAL_OFFER_1)
    a.is_idle_greeting(REAL_OFFER_1, TASK)
    assert REAL_OFFER_1 == before


# --- through the real roles path (what opencode's swarm/crew/multi go through) --

from tests.test_tool_turn_roles import roles, _dispatcher, _ro_body  # noqa: E402,F401


def _idle_reply(payload):
    return {"choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": REAL_OFFER_2}}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60}}


def _text_reply(payload):
    return {"choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant",
                                    "content": "Done. Fixed it."}}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 5, "total_tokens": 45}}


def test_the_roles_path_hands_an_idle_offer_over_to_the_next_actor(roles, monkeypatch):
    import app as A
    calls = []
    body = _ro_body()
    body["messages"] = [{"role": "user", "content": TASK}]
    monkeypatch.setattr(A, "_is_relay_pid", lambda pid: pid == "p1")
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, _idle_reply, 200), "p2": (0.0, _text_reply, 200),
        "p3": (0.0, _text_reply, 200)}, calls))
    data, _hdrs = A._swarm_tool_result(body)
    m = data["choices"][0]["message"]
    assert m["content"] != REAL_OFFER_2
    assert "Fixed" in m["content"]
    # p1's idle offer must have been tried and rejected, not just skipped.
    assert any(pid == "p1" for pid, *_ in calls)
