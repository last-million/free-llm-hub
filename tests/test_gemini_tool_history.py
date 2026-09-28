"""Google stays a candidate for tool-calling continuations.

Gemini validates a thought signature on tool calls in the history; a call it
did not sign (another model's, or its own after a client dropped the
signature) used to 400, so the hub excluded Google from EVERY tool
continuation -- i.e. from every /agent turn after the first. MEASURED
2026-09-28: that left a 54K-token opencode session with nvidia (stalling),
dahl/openrouter (429) and tokenrouter (503) -- four 504s and a failed turn --
while Google had 569 of 600 requests left. The Google hop now carries
Google's skip value on unsigned calls (live: gemini-flash-latest HTTP 200),
and a refusal of that value brings the exclusion back for a while.
"""
import json
import time
import types

import pytest

import app

SENTINEL = app._GEMINI_SKIP_SIGNATURE


@pytest.fixture(autouse=True)
def _reset_rejection():
    app._gemini_sig_rejected_at[0] = 0.0
    yield
    app._gemini_sig_rejected_at[0] = 0.0


def _history(extra=None):
    call = {"id": "call_1", "type": "function",
            "function": {"name": "bash", "arguments": "{\"cmd\": \"ls\"}"}}
    if extra:
        call["extra_content"] = extra
    second = {"id": "call_2", "type": "function",
              "function": {"name": "read", "arguments": "{}"}}
    return [{"role": "system", "content": "You are opencode."},
            {"role": "user", "content": "fix the zone numbers"},
            {"role": "assistant", "content": None, "tool_calls": [call, second]},
            {"role": "tool", "tool_call_id": "call_1", "content": "app.py"},
            {"role": "tool", "tool_call_id": "call_2", "content": "ok"},
            {"role": "user", "content": "continue"}]


def _sig(tc):
    return ((tc.get("extra_content") or {}).get("google") or {}).get("thought_signature")


def test_the_first_unsigned_call_of_each_step_gets_the_skip_value():
    payload = {"model": "models/gemini-3.8-flash", "messages": _history()}
    out = app._with_gemini_history_signatures(payload)
    calls = out["messages"][2]["tool_calls"]
    assert _sig(calls[0]) == SENTINEL
    assert _sig(calls[1]) is None                   # Gemini checks the first call of a step
    # the caller's payload is untouched (other hops reuse it)
    assert "extra_content" not in payload["messages"][2]["tool_calls"][0]
    assert out["messages"][3] is payload["messages"][3]


def test_a_step_already_signed_by_gemini_is_left_alone():
    real = {"google": {"thought_signature": "CiQB-real-signature"}}
    payload = {"model": "m", "messages": _history(extra=real)}
    assert app._with_gemini_history_signatures(payload) is payload


def test_no_tool_calls_means_no_change():
    payload = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    assert app._with_gemini_history_signatures(payload) is payload


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self.headers = {}
        self.text = json.dumps(body if body is not None else {
            "choices": [{"message": {"role": "assistant", "content": "done"}}]})

    def json(self):
        return json.loads(self.text)

    def close(self):
        pass


@pytest.fixture
def capture(monkeypatch):
    box = types.SimpleNamespace(sent=[], reply=_Resp())

    def post(url, **kw):
        box.sent.append(kw.get("json"))
        return box.reply
    monkeypatch.setattr(app.config, "get_provider_config",
                        lambda pid: {"api_keys": ["k1"], "enabled": True})
    monkeypatch.setattr(app, "_next_key_start", lambda pid, n: 0)
    monkeypatch.setattr(app.requests, "post", post)
    return box


def test_only_the_google_hop_gets_it(capture):
    tools = [{"type": "function", "function": {"name": "bash", "parameters": {}}}]
    app._upstream_chat("google", {"model": "models/gemini-3.8-flash", "tools": tools,
                                  "messages": _history()}, False)
    app._upstream_chat("groq", {"model": "qwen/qwen3.8-27b", "tools": tools,
                                "messages": _history()}, False)
    google, groq = capture.sent

    def calls(body):
        return [tc for m in body["messages"] for tc in (m.get("tool_calls") or ())]
    assert _sig(calls(google)[0]) == SENTINEL
    assert all("extra_content" not in tc for tc in calls(groq))


def test_google_is_a_candidate_on_tool_continuations():
    msgs = _history()
    assert app._exclude_google_for_foreign_tool_history(
        ["nvidia", "google", "dahl"], True, msgs) == ["nvidia", "google", "dahl"]


def test_a_refused_skip_value_brings_the_exclusion_back_for_a_while(capture):
    capture.reply = _Resp(400, {"error": {"message":
                                "Function call is missing a thought_signature in functionCall parts."}})
    app._upstream_chat("google", {"model": "models/gemini-3.8-flash",
                                  "messages": _history()}, False)
    assert app._gemini_signature_rejected()
    msgs = _history()
    assert app._exclude_google_for_foreign_tool_history(["google", "nvidia"], True, msgs) == ["nvidia"]
    # still fails open when Google is the only candidate
    assert app._exclude_google_for_foreign_tool_history(["google"], True, msgs) == ["google"]
    payload = {"model": "m", "messages": msgs}
    assert app._with_gemini_history_signatures(payload) is payload
    # ...and after the retry window it is tried again
    app._gemini_sig_rejected_at[0] = time.time() - app._GEMINI_SIG_RETRY_SECONDS - 1
    assert not app._gemini_signature_rejected()
    assert "google" in app._exclude_google_for_foreign_tool_history(["google", "nvidia"], True, msgs)


def test_a_signature_400_without_the_value_on_it_proves_nothing(capture):
    capture.reply = _Resp(400, {"error": {"message": "missing thought_signature"}})
    app._upstream_chat("google", {"model": "m", "messages": [
        {"role": "user", "content": "hi"}]}, False)
    assert not app._gemini_signature_rejected()
    capture.reply = _Resp(400, {"error": {"message": "missing thought_signature"}})
    app._upstream_chat("nvidia", {"model": "m", "messages": _history()}, False)
    assert not app._gemini_signature_rejected()
