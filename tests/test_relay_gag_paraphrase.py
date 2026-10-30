"""A relay model that paraphrases its relay's "do not request another function call"
gag marks that relay, and on codex the stopped turn is retried on another model.

MEASURED 2026-10-30: the real codex 0.165 through the hub, served by
g4f srv_mv57e8714aedb1a00bf8:poe/DeepSeek-V4.1-Flash-T, ran `wsl -e uname -s` (one
tool call), then its leaked thinking said "The instruction says don't request another
function call" and it ended the turn without creating the file it was asked for.
"""
import json
import re

import pytest

import answer_check as ac
import app as A


GAGGED = ("<think>The instruction says don't request another function call. I can't "
          "claim the file was created.</think>\nStep 1 returned `Linux`, but I have no "
          "confirmed result for step 2.")
RELAY, MODEL = "g4f", "srv_mv57e8714aedb1a00bf8:poe/DeepSeek-V4.1-Flash-T"
DIRECT = ("nvidia", "deepseek-ai/deepseek-v4.1-flash")


@pytest.mark.parametrize("text", [
    GAGGED,
    "The instruction from the caller says don't request another function call.",
    "I must not request another tool call here.",
    "We should not request any more function calls.",
    "The declared function has now been executed by the caller.",
    "I cannot request a second function call.",
])
def test_the_gag_is_recognised_in_the_models_own_words(text):
    assert ac.has_relay_gag(text)


@pytest.mark.parametrize("text", [
    "I'll request another review from the team.",
    "Don't call the function twice; one call is enough.",
    "Next I will run the tests and then request a merge.",
    "The tool call returned three files.",
])
def test_ordinary_replies_are_not_the_gag(text):
    assert not ac.has_relay_gag(text)


def test_inspect_marks_the_paraphrase_on_a_relay_hop_only():
    on_relay = ac.inspect(GAGGED, prompt_text="make proof.txt", tools_offered=True, relay=True)
    direct = ac.inspect(GAGGED, prompt_text="make proof.txt", tools_offered=True, relay=False)
    assert "relay_tool_injection" in on_relay["reasons"]
    assert "relay_tool_injection" not in direct["reasons"]


# --------------------------------------------------------------------------- codex stream


def _chunk(delta, fin=None):
    return ("data: " + json.dumps({"choices": [{"index": 0, "delta": delta,
                                                "finish_reason": fin}]})).encode()


def _stream(text):
    frames = [_chunk({"content": t}) for t in re.findall(r"\S+\s*", text)]
    return iter(frames + [_chunk({}, "stop"), b"data: [DONE]"])


class _Resp:
    status_code = 200
    headers = {}

    def close(self):
        pass


@pytest.fixture
def filed(monkeypatch):
    rec = {"bench": [], "nonanswer": []}
    monkeypatch.setattr(A, "_note_relay_injection",
                        lambda pid, model, reasons: rec["bench"].append((pid, model)))
    monkeypatch.setattr(A, "_note_nonanswer",
                        lambda pid, model, kind=None: rec["nonanswer"].append(kind))
    monkeypatch.setattr(A, "_note_recent_hop_failure", lambda *a, **k: None)
    monkeypatch.setattr(A, "_record_stream_outcome", lambda *a, **k: None)
    monkeypatch.setattr(A, "_relay_injection_rest_on", lambda: True)
    A._announce_stop_seen.clear()
    yield rec
    A._announce_stop_seen.clear()


def _types(pid, model, text="", instruction="make proof.txt"):
    body = b"".join(A._responses_stream(
        _Resp(), "auto", line_iter=_stream(text or GAGGED), hop_pid=pid, hop_model=model,
        tools_offered=True, last_prompt=instruction)).decode()
    return [json.loads(ln[5:]).get("type") for ln in body.split("\n") if ln.startswith("data:")]


def test_a_gagged_relay_reply_is_retried_and_its_relay_benched(filed):
    types = _types(RELAY, MODEL)
    assert types[-1] == "response.failed"
    assert "response.output_item.done" not in types
    assert filed["bench"] == [(RELAY, MODEL)]


def test_the_same_reply_from_a_direct_provider_is_served(filed):
    types = _types(*DIRECT)
    assert types[-1] == "response.completed"
    assert filed["bench"] == []


def test_the_retry_is_capped_per_instruction(filed):
    for _ in range(A._ANNOUNCE_STOP_RETRIES):
        assert _types(RELAY, MODEL)[-1] == "response.failed"
    assert _types(RELAY, MODEL)[-1] == "response.completed"


def test_with_the_rest_flag_off_it_is_served(filed, monkeypatch):
    monkeypatch.setattr(A, "_relay_injection_rest_on", lambda: False)
    assert _types(RELAY, MODEL)[-1] == "response.completed"
