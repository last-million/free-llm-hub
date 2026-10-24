"""A reply that is ONLY thinking is empty, not an answer (2026-10-24).

MEASURED: a live roles tool turn ("ok go" continuation, recon on app.evaa.finance) was
served a reply whose ENTIRE content was one well-formed <think>...</think> block with
nothing before or after it. `_strip_leaked_think_msg` deliberately leaves that case
untouched (stripping would leave nothing to show), on the stated assumption that "the
answer gate / non-answer logic handles it" (see test_leaked_think_is_stripped.py's
test_a_reply_that_is_only_thinking_is_left_alone). It did not: every empty-reply check
tested `(content or "").strip()`, and raw <think> TEXT is non-empty text, so a
pure-reasoning reply read as "has content" and was served -- no tool call, nothing the
CLI could act on, the turn just stopped.

`answer_check.is_blank_after_thinking` / `visible_text` fix the three empty-reply checks
(_chat_json_is_empty, the roles actor judge, the old race judge) to strip reasoning markup
FIRST. Applies on every provider, relay or direct: a reply with zero visible content and
no tool call is a failed hop regardless of who sent it. Never changes what the client
sees -- only whether the hub treats a reply as empty.
"""
import copy

import pytest

import answer_check as a
import app as A

LEAKED = """<think>
The user said "ok go" -- proceed with evaa-web recon. The harness note says a declared
function was executed, but I don't see a result in this message. Last real state: we
decided to pivot to evaa-web. Next step: navigate to app.evaa.finance and start recon.
</think>"""


def test_the_exact_reported_reply_is_blank():
    assert a.is_blank_after_thinking(LEAKED)
    assert a.visible_text(LEAKED).strip() == ""


def test_a_real_answer_is_never_blank():
    assert not a.is_blank_after_thinking("Navigating to app.evaa.finance now.")
    assert not a.is_blank_after_thinking("42")
    assert not a.is_blank_after_thinking(LEAKED + "\n\nStarting the recon now.")


def test_an_unclosed_think_tag_with_nothing_after_is_blank():
    assert a.is_blank_after_thinking("<think>still reasoning and it never closes")


def test_a_real_answer_followed_by_a_trailing_think_block_is_not_blank():
    assert not a.is_blank_after_thinking("Done. <think>more thoughts</think>")


def test_both_spellings_and_case():
    for open_t, close_t in (("<think>", "</think>"), ("<thinking>", "</thinking>"),
                            ("<THINK>", "</THINK>")):
        assert a.is_blank_after_thinking(open_t + "scratch" + close_t), open_t


def test_visible_text_never_mutates_the_input():
    before = copy.deepcopy(LEAKED)
    a.visible_text(LEAKED)
    a.is_blank_after_thinking(LEAKED)
    assert LEAKED == before


@pytest.mark.parametrize("bad", [None, "", 7, [], {}])
def test_garbage_never_raises(bad):
    assert a.is_blank_after_thinking(bad) in (True, False)
    a.visible_text(bad)        # must not raise


# --- the three wired checks ------------------------------------------------------------

def test_chat_json_is_empty_catches_a_pure_think_reply():
    data = {"choices": [{"message": {"role": "assistant", "content": LEAKED}}]}
    assert A._chat_json_is_empty(data) is True


def test_chat_json_is_empty_still_passes_a_real_answer():
    data = {"choices": [{"message": {"role": "assistant",
                                     "content": LEAKED + "\n\nDone."}}]}
    assert A._chat_json_is_empty(data) is False


def test_chat_json_is_empty_still_passes_a_tool_call_with_no_text():
    data = {"choices": [{"message": {"role": "assistant", "content": "",
                                     "tool_calls": [{"id": "c1", "type": "function",
                                                    "function": {"name": "bash",
                                                                 "arguments": "{}"}}]}}]}
    assert A._chat_json_is_empty(data) is False


from tests.test_tool_turn_roles import roles, _dispatcher, _ro_body  # noqa: E402,F401


def _think_only_reply(payload):
    return {"choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": LEAKED}}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 30, "total_tokens": 70}}


def _real_tool_call(payload):
    return {"choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "read", "arguments": '{"path": "x.py"}'}}]}}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50}}


def test_the_roles_path_treats_a_pure_think_reply_as_a_failed_hop_and_moves_on(
        roles, monkeypatch):
    calls = []
    monkeypatch.setattr(A, "_is_relay_pid", lambda pid: False)      # a DIRECT provider too
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, _think_only_reply, 200), "p2": (0.0, _real_tool_call, 200),
        "p3": (0.0, _real_tool_call, 200)}, calls))
    data, _hdrs = A._swarm_tool_result(_ro_body())
    m = data["choices"][0]["message"]
    assert m["tool_calls"]                       # the hub moved on to a model that acted
    assert any(pid == "p1" for pid, *_ in calls)  # p1's pure-think reply was tried and rejected
