"""Max reasoning & deep thinking (owner 2026-10-11: "always the max of
reasoning and deep thinking, for the best quality"; a Settings switch, ON by
default; Activity shows "deep thinking" / "reasoning").

Pins, with the flag `max_reasoning` ON (its default):
  * a medium or hard turn asks every model that can think for effort "high":
    reasoning-named models, default thinkers AND catalog hybrids;
  * the output-budget cap still applies (high on a tiny budget returns empty);
  * simple / unknown difficulty and non-thinking models are unchanged;
  * a TOOL turn whose sent size is past LONG_CTX_SPEED_TOKENS keeps the old
    mapping (thinkers there were the main cause of no-answer turns);
  * roles calls get it under that size and nothing above it;
  * the Activity row carries `reasoning` = "deep" / "on" for the picked hop;
  * GET/POST /api/reasoning, control-gated; the Settings switch exists.
With the flag OFF everything is the old difficulty mapping.
No network: nothing is dispatched except to a fake.
"""
import os
import queue

import pytest

import app as A
import config

H = {"X-Free-LLM-Hub": "dashboard"}
GEMINI = ("google", "models/gemini-3-flash-preview")      # default thinker by name
PLAIN = ("groq", "llama-3.3-70b-versatile")              # does not think
TOOLS = [{"type": "function", "function": {"name": "bash", "parameters": {}}}]


@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(tmp_path / "state" / "config.json"))
    yield tmp_path


@pytest.fixture
def flag_off(monkeypatch):
    monkeypatch.setattr(A, "_mr_on", lambda: False)


def test_the_flag_is_on_by_default(isolated_config):
    assert A._mr_on() is True


def test_real_turns_get_high_effort_on_every_thinker():
    for diff in ("medium", "hard"):
        out = A._apply_reasoning_effort({"max_tokens": 8000}, "gpt-oss-120b", diff)
        assert out["reasoning_effort"] == "high", diff
        out = A._apply_reasoning_effort({"max_tokens": 32000}, GEMINI[1], diff,
                                        pid=GEMINI[0])
        assert out["reasoning_effort"] == "high", diff


def test_a_hybrid_is_never_switched_on():
    """MEASURED live 2026-10-11: nvidia/z-ai/glm-5.3 (a catalog hybrid) sent an
    effort switched its thinking on, spent ~4 min and returned EMPTY twice."""
    A._learn_ctx_from_catalog("openrouter", {"data": [
        {"id": "vendor/hybrid-mr", "supported_parameters": ["reasoning", "tools"]}]})
    for diff in ("simple", "medium", "hard"):
        out = A._apply_reasoning_effort({"max_tokens": 8000}, "vendor/hybrid-mr", diff,
                                        pid="openrouter")
        assert "reasoning_effort" not in out, diff


def test_a_pair_only_learned_to_think_is_not_maxed():
    pair = ("nvidia", "z-ai/glm-5.3")
    A._note_thinking(*pair)
    assert A._thinks_by_default(*pair)
    out = A._apply_reasoning_effort({"max_tokens": 32000}, pair[1], "hard", pid=pair[0])
    assert "reasoning_effort" not in out


def test_simple_unknown_and_non_thinkers_are_unchanged():
    assert A._apply_reasoning_effort({"max_tokens": 8000}, "gpt-oss-120b",
                                     "simple")["reasoning_effort"] == "low"
    assert "reasoning_effort" not in A._apply_reasoning_effort(
        {"max_tokens": 8000}, "gpt-oss-120b", None)
    assert A._apply_reasoning_effort({"max_tokens": 40}, PLAIN[1], "hard",
                                     pid=PLAIN[0]) == {"max_tokens": 40}


def test_the_output_budget_still_caps_the_effort():
    assert A._apply_reasoning_effort({"max_tokens": 500}, "deepseek-r1",
                                     "hard")["reasoning_effort"] == "low"
    assert A._apply_reasoning_effort({"max_tokens": 1500}, GEMINI[1], "medium",
                                     pid=GEMINI[0])["reasoning_effort"] == "medium"


def test_a_big_agent_turn_keeps_the_old_mapping():
    big = A.LONG_CTX_SPEED_TOKENS
    out = A._apply_reasoning_effort({"max_tokens": 32000, "tools": TOOLS}, GEMINI[1],
                                    "hard", pid=GEMINI[0], est=big)
    assert "reasoning_effort" not in out          # default thinker: its own level
    out = A._apply_reasoning_effort({"max_tokens": 8000, "tools": TOOLS},
                                    "gpt-oss-120b", "medium", est=big)
    assert out["reasoning_effort"] == "medium"    # old difficulty mapping
    # the same size without tools is not an agent turn
    out = A._apply_reasoning_effort({"max_tokens": 32000}, GEMINI[1], "hard",
                                    pid=GEMINI[0], est=big)
    assert out["reasoning_effort"] == "high"
    # under the size, a tool turn is maxed
    out = A._apply_reasoning_effort({"max_tokens": 32000, "tools": TOOLS}, GEMINI[1],
                                    "hard", pid=GEMINI[0], est=big - 1)
    assert out["reasoning_effort"] == "high"


def test_flag_off_is_the_old_mapping(flag_off):
    for diff, eff in (("simple", "low"), ("medium", "medium"), ("hard", "high")):
        out = A._apply_reasoning_effort({"max_tokens": 8000}, "gpt-oss-120b", diff)
        assert out["reasoning_effort"] == eff
    out = A._apply_reasoning_effort({"max_tokens": 32000}, GEMINI[1], "hard",
                                    pid=GEMINI[0])
    assert "reasoning_effort" not in out


def test_a_provider_that_rejects_the_parameter_never_gets_it(monkeypatch):
    monkeypatch.setattr(A, "_reasoning_rejected", lambda pid, model: True)
    out = A._apply_reasoning_effort({"max_tokens": 32000}, GEMINI[1], "hard",
                                    pid=GEMINI[0])
    assert "reasoning_effort" not in out


def test_the_activity_row_follows_the_picked_hop():
    with A.app.test_request_context("/v1/chat/completions", method="POST"):
        act = {"provider": None, "model": None}
        A.g.act = act
        A._act_pick(*GEMINI)
        A._apply_reasoning_effort({"max_tokens": 32000}, GEMINI[1], "hard", pid=GEMINI[0])
        assert act["reasoning"] == "deep"
        # effort applied BEFORE the pick works too
        A._apply_reasoning_effort({"max_tokens": 32000}, "gpt-oss-120b", "simple",
                                  pid="groq")
        A._act_pick("groq", "gpt-oss-120b")
        assert act["reasoning"] == "on"
        # a model that does not think clears it
        A._apply_reasoning_effort({"max_tokens": 40}, PLAIN[1], "hard", pid=PLAIN[0])
        A._act_pick(*PLAIN)
        assert "reasoning" not in act


def _leg_payload(monkeypatch, est):
    seen = []

    def fake(pid, payload, deadline):
        seen.append(dict(payload))
        return None, RuntimeError("fake")
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", fake)
    q = queue.Queue()
    body = {"model": "x", "messages": [{"role": "user", "content": "hi"}],
            "tools": TOOLS, "max_tokens": 32000}
    with A.app.test_request_context("/v1/chat/completions", method="POST"):
        A._role_start_leg(0, GEMINI[0], GEMINI[1], body, 30, q, est, "coding|hard|tools|s")
        q.get(timeout=10)
    return seen[0]


def test_roles_calls_are_maxed_under_the_size_only(monkeypatch):
    assert _leg_payload(monkeypatch, 5000)["reasoning_effort"] == "high"
    assert "reasoning_effort" not in _leg_payload(monkeypatch, A.LONG_CTX_SPEED_TOKENS)


def test_roles_calls_unchanged_with_the_flag_off(monkeypatch, flag_off):
    assert "reasoning_effort" not in _leg_payload(monkeypatch, 5000)


def test_api_roundtrip(isolated_config):
    c = A.app.test_client()
    assert c.get("/api/reasoning").get_json() == {"enabled": True}
    assert c.post("/api/reasoning", json={"enabled": False},
                  headers=H).get_json() == {"enabled": False}
    assert config.get_flag("max_reasoning", True) is False
    assert A._mr_on() is False
    assert c.post("/api/reasoning", json={"enabled": True},
                  headers=H).get_json() == {"enabled": True}


def test_api_is_control_gated(isolated_config, monkeypatch):
    monkeypatch.setattr(config, "get_control_token", lambda: "secret-token")
    c = A.app.test_client()
    assert c.get("/api/reasoning").status_code == 401
    assert c.post("/api/reasoning", json={"enabled": False}).status_code == 403


def test_settings_switch_and_activity_label_exist():
    src = open(os.path.join(os.path.dirname(A.__file__), "templates", "index.html"),
               encoding="utf-8").read()
    for needle in ('id="drawer-reasoning-switch"', "/api/reasoning", "initReasoning();",
                   "loadReasoning(silent)", "a.reasoning === 'deep'",
                   ">deep thinking</span>", ">reasoning</span>", ".af-think.deep{"):
        assert needle in src, needle
