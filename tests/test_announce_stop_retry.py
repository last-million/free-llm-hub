"""A committed codex stream that ends on "I'll do X" is retried (2026-10-30).

The stream peek holds only replies up to _ANNOUNCE_MAX_CHARS (400) to judge them
whole; a LONGER reply streams to codex as it arrives. One that ended "...Next I'll
run the tests." with no tool call was closed with response.completed, and codex
ended the turn with nothing done. Now the end of such a /v1/responses stream is
`response.failed` (a retryable code, no output_item.done -- codex retries it like a
dropped stream and keeps nothing of the partial reply) and the pair is filed, at
most _ANNOUNCE_STOP_RETRIES times per instruction.
"""
import json
import re

import pytest

import app as A


def _chunk(delta, fin=None):
    return ("data: " + json.dumps({
        "id": "chatcmpl-x", "object": "chat.completion.chunk", "created": 1727400000,
        "model": "m", "choices": [{"index": 0, "delta": delta, "finish_reason": fin}]})
    ).encode()


def _stream(text, fin="stop"):
    frames = [_chunk({"content": t}) for t in re.findall(r"\S+\s*", text)]
    return iter(frames + [_chunk({}, fin), b"data: [DONE]"])


def _tool_stream(text):
    frames = [_chunk({"content": t}) for t in re.findall(r"\S+\s*", text)]
    frames.append(_chunk({"role": "assistant", "tool_calls": [{
        "index": 0, "id": "call_1", "type": "function",
        "function": {"name": "bash", "arguments": '{"command": "ls"}'}}]}))
    return iter(frames + [_chunk({}, "tool_calls"), b"data: [DONE]"])


class _Resp:
    status_code = 200

    def close(self):
        pass
    headers = {}


REPORT = ("I read the scope page and the program rules. The in-scope assets are the main "
          "web app, the public API and the mobile backend; the marketing site and the status "
          "page are out of scope, and automated scanning is limited to five requests per "
          "second with the program's header on every request. Testing accounts must be "
          "created with the program's own email alias, and any finding needs a working proof "
          "of concept before it is reported.")
LONG_ANNOUNCE = REPORT + "\n\nNext, I'll fetch the API documentation and map every endpoint."
LONG_ANSWER = REPORT + "\n\nThose are the rules that apply to every test in this program."


@pytest.fixture
def filed(monkeypatch):
    rec = {"nonanswer": [], "recent": [], "outcome": []}
    monkeypatch.setattr(A, "_note_nonanswer",
                        lambda pid, model, kind=None: rec["nonanswer"].append((pid, model, kind)))
    monkeypatch.setattr(A, "_note_recent_hop_failure",
                        lambda pid, model, kind: rec["recent"].append((pid, model, kind)))
    monkeypatch.setattr(A, "_record_stream_outcome",
                        lambda pid, model, text, **kw: rec["outcome"].append((pid, model)))
    A._announce_stop_seen.clear()
    yield rec
    A._announce_stop_seen.clear()


def _run(lines, instruction="map the program's scope", tools=True):
    body = b"".join(A._responses_stream(
        _Resp(), "auto", line_iter=lines, hop_pid="p1", hop_model="m1",
        tools_offered=tools, last_prompt=instruction)).decode()
    events = [json.loads(ln[5:]) for ln in body.split("\n") if ln.startswith("data:")]
    return [e.get("type") for e in events], events


def test_a_long_reply_that_ends_on_an_announcement_is_retried(filed):
    assert len(LONG_ANNOUNCE) > A._ANNOUNCE_MAX_CHARS
    types, events = _run(_stream(LONG_ANNOUNCE))
    assert types[-1] == "response.failed"
    assert "response.completed" not in types
    assert "response.output_item.done" not in types, "codex would keep the partial reply"
    err = events[-1]["response"]["error"]
    assert err["code"] == "upstream_error"
    assert filed["nonanswer"] == [("p1", "m1", "announced")]
    assert filed["recent"] == [("p1", "m1", "announced")]
    assert filed["outcome"] == []


def test_a_short_announcement_that_reached_the_end_is_retried_too(filed):
    types, _ = _run(_stream("I'll start with Box and Arc to read their published scope lists."))
    assert types[-1] == "response.failed"


def test_the_retries_are_capped_per_instruction(filed):
    for _ in range(A._ANNOUNCE_STOP_RETRIES):
        assert _run(_stream(LONG_ANNOUNCE))[0][-1] == "response.failed"
    types, _ = _run(_stream(LONG_ANNOUNCE))
    assert types[-1] == "response.completed", "past the cap the reply is served: no loop"
    assert _run(_stream(LONG_ANNOUNCE), instruction="another task")[0][-1] == "response.failed"


@pytest.mark.parametrize("text", [
    LONG_ANSWER,
    REPORT + "\n\nShould I start with the API or the mobile backend?",
    REPORT + "\n\nI'll wait for your confirmation before testing anything.",
    "```\nI'll run the tests.",
])
def test_a_real_answer_or_a_question_is_served(filed, text):
    types, _ = _run(_stream(text))
    assert types[-1] == "response.completed"
    assert filed["nonanswer"] == []


def test_a_reply_with_a_tool_call_is_served(filed):
    types, _ = _run(_tool_stream(LONG_ANNOUNCE))
    assert types[-1] == "response.completed"
    assert filed["nonanswer"] == []


def test_a_turn_without_tools_is_served(filed):
    types, _ = _run(_stream(LONG_ANNOUNCE), tools=False)
    assert types[-1] == "response.completed"


def test_a_reply_cut_by_the_token_cap_is_not_retried_here(filed):
    types, _ = _run(_stream(LONG_ANNOUNCE, fin="length"))
    assert "response.failed" not in types


def test_the_flag_off_serves_it_as_before(filed, monkeypatch):
    real = A.config.get_flag
    monkeypatch.setattr(A.config, "get_flag",
                        lambda name, default=None: False if name == "announce_stop_retry"
                        else real(name, default))
    types, _ = _run(_stream(LONG_ANNOUNCE))
    assert types[-1] == "response.completed"
