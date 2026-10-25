"""A relay's quota notice is caught BEFORE the stream is committed (2026-10-25).

MEASURED (hub.log 2026-10-25 01:16, opencode `coding-max`, 1m52s, "empty . 200"):
the Max pick went silent, the best->auto fallback moved to
g4f/srv_mrdypihj16e8b1776409:z-ai/glm-5.3-flash, which streamed a quota notice.
The pre-commit peek (_judge_peeked) ran answer_check.inspect WITHOUT relay=, so
the relay-only notice check never ran there; the stream was committed and the
_StreamAnswerGate (which does pass relay=) cut it to nothing -- an empty 200
instead of the next hop. _peek_check now carries the hop's provider.
"""
import json

import app as A


POLLINATIONS = ("The API key used for this request has reached its budget. Please "
                "[raise the key budget](https://enter.pollinations.ai/edit-key?id=FAKEID0000"
                "&ref=agent_key_budget), then try again.\n\nTopping up the wallet adds "
                "credits to every key.")
GOOGLE = ("You exceeded your current quota, please check your plan and billing details. "
          "For more information on this error, head to: "
          "https://ai.google.dev/gemini-api/docs/rate-limits.")
TOOLS = [{"type": "function", "function": {"name": "bash", "parameters": {"type": "object"}}}]


def _frames(text):
    return [("data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": text}}]})
             ).encode(),
            b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
            b"data: [DONE]"]


def _payload():
    return {"tools": TOOLS, "messages": [
        {"role": "user", "content": "Fix the failing test in parser.py and run it."}]}


def test_peek_check_marks_a_relay_hop():
    assert A._peek_check(_payload(), True, pid="g4f")["relay"] is True
    assert A._peek_check(_payload(), True, pid="nvidia")["relay"] is False
    assert A._peek_check(_payload(), True)["relay"] is False      # old callers


def test_a_relay_quota_notice_is_not_committed():
    for notice in (POLLINATIONS, GOOGLE):
        check = A._peek_check(_payload(), True, pid="g4f")
        status, _buf = A._peek_until_content(iter(_frames(notice)), 5, check=check)
        assert status != "content", notice                  # the chain walks on


def test_the_judge_passes_relay_to_answer_check(monkeypatch):
    seen = {}
    real = A.answer_check.inspect

    def spy(text, **kw):
        seen.update(kw)
        return real(text, **kw)
    monkeypatch.setattr(A.answer_check, "inspect", spy)
    check = A._peek_check(_payload(), True, pid="g4f")
    frames = _frames("A real answer: the bug is on line 4.")
    A._judge_peeked([f.decode() for f in frames[:1]], check, frames, complete=True)
    assert seen.get("relay") is True


def test_a_real_relay_answer_still_commits():
    check = A._peek_check(_payload(), True, pid="g4f")
    status, _buf = A._peek_until_content(
        iter(_frames("The test fails because parse() drops the last token; "
                     "I changed the loop bound to len(tokens) and it passes now.")),
        5, check=check)
    assert status == "content"


def test_every_chain_peek_names_its_provider():
    src = open("app.py", encoding="utf-8").read()
    assert src.count("_peek_check(payload, has_tools, pid=hop_pid, model=hop_model)") == 3
    assert "_peek_check(retry, has_tools, pid=pid, model=model)" in src
