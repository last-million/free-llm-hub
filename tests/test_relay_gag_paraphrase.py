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


# --------------------------------------------------------------------------- the retry's route
# Codex's retry is a fresh request with the SAME session, so it reaches the router with the
# same pin. The bench only helps if a pinned session leaves a benched relay.

PIN = ("g4f", "srv_abc:z-ai/glm-5.3")
MSGS = [{"role": "user", "content": "make proof.txt with the output of uname"}]
SCORES = {"srv_abc:z-ai/glm-5.3": 134.0, "deepseek-ai/deepseek-v4.1-flash": 130.0}
WORLD = {"g4f": ["srv_abc:z-ai/glm-5.3"], "nvidia": ["deepseek-ai/deepseek-v4.1-flash"]}


@pytest.fixture
def pinned_fleet(monkeypatch):
    world = {p: list(ms) for p, ms in WORLD.items()}
    monkeypatch.setattr(A, "_available_providers", lambda *a, **k: list(world))
    monkeypatch.setattr(A, "_prefetch_auto_models", lambda pids: dict(world))
    monkeypatch.setattr(A, "_auto_models", lambda pid: list(world[pid]))
    monkeypatch.setattr(A, "_provider_capable", lambda pid, est: True)
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: SCORES.get(m, 10.0))
    monkeypatch.setattr(A, "_supports_tools", lambda pid, m: True)
    monkeypatch.setattr(A, "_session_pin_get", lambda key: PIN)
    monkeypatch.setattr(A, "_session_pin_set", lambda *a, **k: None)
    monkeypatch.setattr(A, "_weighted_pick", lambda pool, *a, **k: max(pool))
    monkeypatch.setattr(A, "_active_mode", lambda: A.MODE_ALL)
    monkeypatch.setattr(A, "_simple_turn", lambda: False)
    monkeypatch.setattr(A, "_relay_injection_rest_on", lambda: True)
    with A._relay_tool_lock:
        A._relay_tool_fail.clear()
    yield world
    with A._relay_tool_lock:
        A._relay_tool_fail.clear()


def _route():
    pid, model, _diff = A._route_by_difficulty(MSGS, None, 500, require_tools=True)
    return pid, model


def test_control_a_pinned_session_stays_on_its_pair_until_the_relay_is_benched(pinned_fleet):
    assert _route() == PIN


def test_a_benched_relay_releases_a_pinned_session(pinned_fleet):
    A._note_relay_injection(PIN[0], PIN[1], ["relay_tool_injection"])
    assert A._relay_tool_sick(*PIN)
    assert _route() == ("nvidia", "deepseek-ai/deepseek-v4.1-flash")


def test_when_the_relay_is_all_there_is_it_is_still_used(pinned_fleet):
    pinned_fleet.pop("nvidia")
    A._note_relay_injection(PIN[0], PIN[1], ["relay_tool_injection"])
    assert _route() == PIN
