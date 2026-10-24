"""A relay's raw <think> block never reaches the CLI as the visible answer.

MEASURED 2026-10-23/24: g4f's poe-GLM backends put their scratchpad in the reply text
("<think>\\nThe user wants ... \\n</think>\\n\\nthe answer"), and opencode showed it as the
assistant's text -- the whole session's replies began with a wall of reasoning. The hub only
ever stripped such blocks for ITS OWN internal calls (recaps, fact extraction, canary checks).

The rule is deliberately narrow, because a wrong strip erases an answer:
  * RELAY hops only (a direct provider that writes <think> in its text is left alone);
  * only a CLOSED block at the very START of the reply;
  * only when something else is left to show (visible text, or tool calls);
  * never when the user's own prompt is about <think> tags;
  * the stripped text moves to `reasoning_content` instead of being thrown away;
  * the non-stream paths of all three protocols and the roles (pipeline) path.
Streamed single-model replies are NOT rewritten (a half-seen tag cannot be judged safely).
Flag `strip_leaked_think` (default on).
"""
import copy

import pytest

import app as A

THINK = "<think>\nThe user wants a number. 17*23+19 = 410.\n</think>\n\n"


def msg(content, tool_calls=None, **kw):
    m = {"role": "assistant", "content": content}
    if tool_calls:
        m["tool_calls"] = tool_calls
    m.update(kw)
    return m


CALL = [{"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]


def test_a_closed_leading_block_moves_to_reasoning_content():
    m = msg(THINK + "410")
    assert A._strip_leaked_think_msg(m, relay=True) is True
    assert m["content"] == "410"
    assert "17*23+19" in m["reasoning_content"] and "<think>" not in m["reasoning_content"]


def test_both_spellings_and_leading_whitespace():
    for open_t, close_t in (("<think>", "</think>"), ("<thinking>", "</thinking>"),
                            ("<THINK>", "</THINK>")):
        m = msg("  \n" + open_t + "scratch" + close_t + "\nAnswer here")
        assert A._strip_leaked_think_msg(m, relay=True), open_t
        assert m["content"] == "Answer here"


def test_an_existing_reasoning_field_is_kept_not_overwritten():
    m = msg(THINK + "410", reasoning_content="the provider's own reasoning")
    assert A._strip_leaked_think_msg(m, relay=True)
    assert m["reasoning_content"] == "the provider's own reasoning"
    assert m["content"] == "410"


def test_a_tool_call_reply_with_only_a_think_block_keeps_its_call():
    m = msg("<think>run the command</think>", tool_calls=CALL)
    assert A._strip_leaked_think_msg(m, relay=True)
    assert m["content"] in (None, "")
    assert m["tool_calls"] == CALL


def test_a_reply_that_is_only_thinking_is_left_alone():
    # Stripping would leave an empty answer; the answer gate / non-answer logic handles it.
    for text in ("<think>only thinking</think>", "<think>cut off mid-thought"):
        m = msg(text)
        before = copy.deepcopy(m)
        assert A._strip_leaked_think_msg(m, relay=True) is False
        assert m == before, text


def test_an_unclosed_block_is_never_touched():
    m = msg("<think>no end tag but then an answer follows: 410")
    before = copy.deepcopy(m)
    assert not A._strip_leaked_think_msg(m, relay=True)
    assert m == before


def test_a_tag_in_the_middle_or_in_code_is_never_touched():
    for text in ("Here is how to write it: <think>x</think> is a tag models use.",
                 "```\n<think>quoted</think>\n```\nand the answer"):
        m = msg(text)
        before = copy.deepcopy(m)
        assert not A._strip_leaked_think_msg(m, relay=True), text
        assert m == before


def test_a_direct_provider_is_left_alone():
    m = msg(THINK + "410")
    before = copy.deepcopy(m)
    assert not A._strip_leaked_think_msg(m, relay=False)
    assert m == before


def test_a_prompt_about_think_tags_gets_its_text_untouched():
    m = msg(THINK + "410")
    before = copy.deepcopy(m)
    assert not A._strip_leaked_think_msg(m, relay=True, prompt="explain what a <think> tag is")
    assert m == before


def test_the_switch_off_restores_the_old_behaviour(monkeypatch):
    orig = A.config.get_flag
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=None: False if k == "strip_leaked_think" else orig(k, d))
    m = msg(THINK + "410")
    before = copy.deepcopy(m)
    assert not A._strip_leaked_think_msg(m, relay=True)
    assert m == before


@pytest.mark.parametrize("bad", [None, "text", 7, {}, {"content": None}, {"content": 5}, []])
def test_garbage_never_raises(bad):
    assert A._strip_leaked_think_msg(bad, relay=True) in (True, False)
    assert A._strip_leaked_think_data(bad, "g4f", None) in (True, False)


def test_the_data_wrapper_reads_the_first_choice_and_the_relay_class():
    data = {"choices": [{"message": msg(THINK + "410")}]}
    assert A._strip_leaked_think_data(data, "g4f", None)       # g4f is a relay
    assert data["choices"][0]["message"]["content"] == "410"
    direct = {"choices": [{"message": msg(THINK + "410")}]}
    assert not A._strip_leaked_think_data(direct, "nvidia", None)
    assert direct["choices"][0]["message"]["content"].startswith("<think>")


# --- end to end through the real handlers -------------------------------------------------

import json
import time

TEXT = "The command ran and printed AAA. Everything you asked for is finished, so there is nothing left."
BASH = {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}


class _Resp:
    status_code = 200
    text = ""

    def __init__(self):
        self.headers = {}

    def json(self):
        return {"id": "x", "object": "chat.completion", "created": 1,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": THINK + TEXT}}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 12, "total_tokens": 52}}

    def close(self):
        pass

    def iter_lines(self, decode_unicode=False):
        return iter(())

    iter_content = iter_lines


@pytest.fixture
def relay_world(monkeypatch):
    for name in ("_record_chat_usage", "_save_perf_stats", "_act_pick", "_note_ttft",
                 "_note_provider_timeout", "_throttle_failed_hop"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)
    monkeypatch.setattr(A, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(A, "_model_block_reason", lambda pid, m: None)
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    monkeypatch.setattr(A, "_is_trivial_turn", lambda *a, **k: False)
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: 20)
    monkeypatch.setattr(A, "_record_outcome", lambda *a, **k: None)
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("g4f", "m1", "hard"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("g4f", "m1")])
    monkeypatch.setattr(A, "_dispatch_chat", lambda pid, payload, stream: _Resp())
    yield


def _post(path, body):
    A.app.config["TESTING"] = True
    r = A.app.test_client().post(path, json=body)
    assert r.status_code == 200, r.get_data()[:200]
    return r.get_json()


def test_chat_completions_hides_the_leaked_block(relay_world):
    d = _post("/v1/chat/completions", {
        "model": "auto", "messages": [{"role": "user", "content": "run echo AAA then tell me"}],
        "tools": [{"type": "function", "function": {"name": "bash", "description": "run",
                                                    "parameters": BASH}}]})
    m = d["choices"][0]["message"]
    assert m["content"] == TEXT
    assert "17*23+19" in (m.get("reasoning_content") or "")


def test_responses_hides_the_leaked_block(relay_world):
    d = _post("/v1/responses", {
        "model": "auto", "input": "run echo AAA then tell me",
        "tools": [{"type": "function", "name": "bash", "description": "run", "parameters": BASH}]})
    text = json.dumps(d)
    assert "17*23+19" not in text.replace("\\n", " ") or "<think>" not in text
    assert "<think>" not in text and TEXT in text


def test_messages_hides_the_leaked_block(relay_world):
    d = _post("/v1/messages", {
        "model": "auto", "max_tokens": 300,
        "messages": [{"role": "user", "content": "run echo AAA then tell me"}],
        "tools": [{"name": "bash", "description": "run", "input_schema": BASH}]})
    text = json.dumps(d)
    assert "<think>" not in text and TEXT in text


# --- the roles (pipeline) path: what opencode's swarm / crew / multi tiers go through -------

from tests.test_tool_turn_roles import roles, _dispatcher, _ro_body  # noqa: E402,F401
from tests.test_tool_turn_roles import _Resp as _RolesResp  # noqa: E402


def _text_reply(payload):
    return {"choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": THINK + TEXT}}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 12, "total_tokens": 52}}


def test_the_roles_path_strips_a_relay_actors_leaked_block(roles, monkeypatch):
    calls = []
    monkeypatch.setattr(A, "_is_relay_pid", lambda pid: pid == "p1")      # p1 is the relay
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, _text_reply, 200), "p2": (0.0, _text_reply, 200),
        "p3": (0.0, _text_reply, 200)}, calls))
    data, _hdrs = A._swarm_tool_result(_ro_body())
    m = data["choices"][0]["message"]
    assert m["content"] == TEXT
    assert "17*23+19" in (m.get("reasoning_content") or "")


def test_the_roles_path_leaves_a_direct_providers_text_alone(roles, monkeypatch):
    calls = []
    monkeypatch.setattr(A, "_is_relay_pid", lambda pid: False)
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, _text_reply, 200), "p2": (0.0, _text_reply, 200),
        "p3": (0.0, _text_reply, 200)}, calls))
    data, _hdrs = A._swarm_tool_result(_ro_body())
    assert data["choices"][0]["message"]["content"].startswith("<think>")
