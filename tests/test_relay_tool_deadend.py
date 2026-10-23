"""A relay's "your tool call was invalid" sentence is not the turn's answer.

MEASURED 2026-10-23 (opencode `coding-swarm` session ses_e9602c99, 18:54-19:26):
g4f's poe backend (GLM-5.3-Flash-T) typed its tool call as text, g4f's gateway
could not parse it and answered HTTP 200 with its OWN sentence "The model
returned an invalid or undeclared tool request; no tool was executed." as the
content. The hub judged that clean prose, served it as the final answer, the
CLI saw no tool_calls and ended the turn -- once per "go", for half an hour.
Probed against the live hub: that pair gave 0 tool calls in 6/6 runs where the
native glm/glm-4.5-flash gave the right call 6/6. The hub's own delivery of
tool results was fine (all four acceptance cases and five history shapes).

Like the quota notice it is judged on RELAY hops only, and only on a tool turn:
a direct provider reports a bad call as an HTTP error, and a tool-less reply
that happens to discuss the error is an answer.
"""
import answer_check as ac

DEADEND = "The model returned an invalid or undeclared tool request; no tool was executed."
THINKING_DEADEND = "<think>\nThe user wants two commands run.\n</think>\n\n" + DEADEND
GO = "go"


def test_relay_tool_deadend_on_a_tool_turn_is_not_an_answer():
    for text in (DEADEND, THINKING_DEADEND,
                 "The model returned an invalid tool call; no tool was executed.",
                 "the model returned an undeclared function call."):
        r = ac.inspect(text, prompt_text=GO, tools_offered=True, relay=True)
        assert not r["ok"], text
        assert "tool_deadend" in r["reasons"]
        assert r["salvage"] is None          # nothing in it is an answer


def test_a_direct_provider_or_a_tool_free_turn_is_left_alone():
    assert ac.inspect(DEADEND, prompt_text=GO, tools_offered=True, relay=False)["ok"]
    assert ac.inspect(DEADEND, prompt_text=GO, tools_offered=False, relay=True)["ok"]


def test_a_user_asking_about_the_error_gets_an_answer():
    ask = "why does my agent print: The model returned an invalid or undeclared tool request?"
    assert ac.inspect(DEADEND, prompt_text=ask, last_prompt=ask,
                      tools_offered=True, relay=True)["ok"]


def test_a_real_answer_that_mentions_it_passes():
    long_answer = (
        "That message comes from the gateway, not from your code. It appears when the "
        "model writes a tool call in a shape the gateway cannot parse, so the response "
        "carries the sentence 'The model returned an invalid or undeclared tool "
        "request; no tool was executed.' instead of a tool_calls array. Switch to a "
        "model with native tool calling, or declare the tool in the request's tools "
        "list so the gateway can match the name. " * 2)
    assert len(long_answer) > 400
    assert ac.inspect(long_answer, prompt_text=GO, tools_offered=True, relay=True)["ok"]


def test_ordinary_tool_turn_replies_are_untouched():
    for text in ("Done. The file is written and the tests pass.",
                 "I ran both commands: the first printed AAA and the second BBB.",
                 "No tool was needed for this: the answer is 42."):
        assert ac.inspect(text, prompt_text=GO, tools_offered=True, relay=True)["ok"], text


def test_a_streamed_deadend_is_never_released_early():
    assert not ac.reads_as_answer(DEADEND + " Please try again with a valid call.",
                                  last_prompt=GO, relay=True)


# --- wiring: the roles actor judge, the path the failing session ran through ---

import app as A

_TOOLS = [{"type": "function", "function": {
    "name": "bash", "description": "run a command",
    "parameters": {"type": "object", "properties": {"command": {"type": "string"}},
                   "required": ["command"]}}}]


class _FakeResp:
    status_code = 200

    def __init__(self, message):
        self._m = message

    def json(self):
        return {"choices": [{"index": 0, "finish_reason": "stop", "message": self._m}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    def close(self):
        pass


def _judge(pid, model, message):
    body = {"model": model, "tools": _TOOLS, "messages": [
        {"role": "user", "content": "go"}]}
    return A._role_judge(pid, model, _FakeResp(message), None, body, dict(body), 100, "t")


def test_the_roles_judge_fails_a_relay_actor_that_returns_the_deadend():
    v = _judge("g4f", "srv_x:poe/GLM-5.3-Flash-T", {"role": "assistant", "content": DEADEND})
    assert not v["ok"], v
    assert v["fail"] == "junk"          # the walk hands the turn to the next model


def test_the_roles_judge_still_serves_a_native_tool_call_from_a_relay():
    call = {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call_1", "type": "function",
        "function": {"name": "bash", "arguments": "{\"command\": \"echo BBB\"}"}}]}
    assert _judge("g4f", "srv_x:poe/GLM-5.3-Flash-T", call)["ok"]


def test_the_same_sentence_from_a_direct_provider_is_not_treated_as_junk():
    # A direct provider reports a bad call as an HTTP error; this text from one is
    # whatever the model wrote, so only the older prose checks may judge it.
    v = _judge("nvidia", "z-ai/glm-5.3-flash", {"role": "assistant", "content": DEADEND})
    assert v.get("fail") != "junk"
