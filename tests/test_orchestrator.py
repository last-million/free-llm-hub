"""The orchestrator: pick the model for one conversation or for all of them.

REQUESTED 2026-09-28: a specific orchestrator model per conversation, "set
orchestrator doesn't work", and a /orchestrator command inside the CLIs.
MEASURED: the dashboard button saved config.set_default(), which only
resolves bare/unknown model names -- every CLI sends auto/best/a category, and
those always went through the difficulty router, so the choice never showed.
"""
import json
import os
import shutil
import tempfile

import pytest

import app
import config
import orchestrator as O
from test_cli_disconnect_leaves_no_trace import (  # noqa: F401
    _connect, _disconnect_twice, _rj, _wj, home)

H = {"X-Free-LLM-Hub": "dashboard"}
LIVE = [("nvidia", "moonshotai/kimi-k3", True, 134.0),
        ("tokenrouter", "moonshotai/kimi-k3-free", True, 134.0),
        ("groq", "qwen/qwen3.8-27b", True, 120.0),
        ("pollinations", "openai-fast", False, 60.0)]


# --------------------------------------------------------------- the command

@pytest.mark.parametrize("text,want", [
    ("/orchestrator", {"action": "show"}),
    ("  /orchestrator  ", {"action": "show"}),
    ("/orchestrator help", {"action": "show"}),
    ("/orchestrator kimi-k3", {"action": "set", "target": "kimi-k3", "scope": "conversation"}),
    ("/Orchestrator AUTO", {"action": "set", "target": "auto", "scope": "conversation"}),
    ("/orchestrator best", {"action": "set", "target": "auto", "scope": "conversation"}),
    ("/orchestrator all kimi k3", {"action": "set", "target": "kimi k3", "scope": "all"}),
    ("/orchestrator kimi --all", {"action": "set", "target": "kimi", "scope": "all"}),
    ("/orchestrator reset", {"action": "set", "target": None, "scope": "conversation"}),
    ("/orchestrator all auto", {"action": "set", "target": "auto", "scope": "all"}),
    ("[free-llm-hub] /orchestrator groq/qwen/qwen3.8-27b",
     {"action": "set", "target": "groq/qwen/qwen3.8-27b", "scope": "conversation"}),
])
def test_the_command_is_parsed(text, want):
    assert O.parse_command(text) == want


@pytest.mark.parametrize("text", [
    "please run /orchestrator kimi", "/orchestrator kimi\nand then fix the bug",
    "/orchestrators", "orchestrator kimi", "", None])
def test_only_the_whole_message_is_a_command(text):
    assert O.parse_command(text) is None


def test_a_name_finds_the_model():
    assert O.match_model("nvidia/moonshotai/kimi-k3", LIVE)[0] == ("nvidia", "moonshotai/kimi-k3")
    assert O.match_model("kimi-k3", LIVE)[0] == ("nvidia", "moonshotai/kimi-k3")
    assert O.match_model("qwen", LIVE)[0] == ("groq", "qwen/qwen3.8-27b")
    pair, others = O.match_model("kimi", LIVE)
    assert pair[1].startswith("moonshotai/kimi-k3") and others
    assert O.match_model("gemini", LIVE) == (None, [])
    # among equals, one that can call tools wins
    assert O.match_model("openai-fast", LIVE + [("x", "openai-fast", True, 1.0)])[0] == ("x", "openai-fast")


def test_the_conversation_store_is_bounded_and_forgets(monkeypatch):
    root = tempfile.mkdtemp(prefix="hub-pytest-")
    try:
        st = O.ConversationStore(os.path.join(root, "o.json"))
        st.set("a", "nvidia/m")
        assert O.ConversationStore(st.path).get("a") == "nvidia/m"     # persisted
        st.set("a", None)
        assert st.get("a") is None
        monkeypatch.setattr(O, "STORE_MAX", 2)
        for k in "xyz":
            st.set(k, "auto")
        assert st.get("x") is None and st.get("z") == "auto"
        monkeypatch.setattr(O, "STORE_TTL", -1)
        assert st.get("z") is None
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------- the hub

@pytest.fixture
def hub(monkeypatch):
    root = tempfile.mkdtemp(prefix="hub-pytest-")
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", os.path.join(root, "state", "config.json"))
    live = {p: [] for p, _m, _t, _s in LIVE}
    for p, m, _t, _s in LIVE:
        live[p].append(m)
    tools = {(p, m): t for p, m, t, _s in LIVE}
    monkeypatch.setattr(app, "_available_providers", lambda: list(live))
    monkeypatch.setattr(app, "_prefetch_free_models",
                        lambda pids: {p: live.get(p, []) for p in pids})
    monkeypatch.setattr(app, "_supports_tools", lambda p, m: tools.get((p, m), False))
    monkeypatch.setattr(app, "_benchmark_score",
                        lambda p, m: next((s for pp, mm, _t, s in LIVE if (pp, mm) == (p, m)), 0))
    monkeypatch.setattr(app, "_model_block_reason", lambda p, m: None)
    monkeypatch.setattr(app, "_is_model_skipped", lambda p, m: False)
    monkeypatch.setattr(app.quota, "is_model_throttled", lambda p, m: False)
    monkeypatch.setattr(app, "_model_ctx_info", lambda p, m: (131072, "catalog"))
    monkeypatch.setattr(app, "_is_vision_model", lambda p, m: False)

    def no_upstream(*a, **k):
        raise AssertionError("an /orchestrator command must never reach a model")
    monkeypatch.setattr(app.requests, "post", no_upstream)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _ctx(session="conv-1"):
    return app.app.test_request_context("/v1/chat/completions", method="POST",
                                        headers={"X-Session-Id": session})


MSGS = [{"role": "user", "content": "fix the zone numbers"}]


def test_nothing_chosen_keeps_the_routers_pick(hub):
    with _ctx():
        assert app._apply_orchestrator("groq", "qwen/qwen3.8-27b", MSGS, 5000, True) == \
            ("groq", "qwen/qwen3.8-27b")


def test_the_global_choice_opens_every_conversation(hub):
    config.set_setting(O.GLOBAL_SETTING, "nvidia/moonshotai/kimi-k3")
    with _ctx():
        assert app._apply_orchestrator("groq", "qwen/qwen3.8-27b", MSGS, 5000, True) == \
            ("nvidia", "moonshotai/kimi-k3")
        assert "all conversations" in app.g.hub_orchestrator


def test_a_conversations_own_choice_wins_and_auto_means_auto(hub):
    config.set_setting(O.GLOBAL_SETTING, "nvidia/moonshotai/kimi-k3")
    with _ctx("conv-2"):
        key = app._orch_key(None, MSGS)
    app._orch_conversations().set(key, "groq/qwen/qwen3.8-27b")
    with _ctx("conv-2"):
        assert app._apply_orchestrator("x", "y", MSGS, 5000, True) == ("groq", "qwen/qwen3.8-27b")
    app._orch_conversations().set(key, O.AUTO)
    with _ctx("conv-2"):
        assert app._apply_orchestrator("x", "y", MSGS, 5000, True) == ("x", "y")
    with _ctx("another"):                                  # others keep the global one
        assert app._apply_orchestrator("x", "y", MSGS, 5000, True)[0] == "nvidia"


def test_a_model_that_cannot_serve_the_turn_is_skipped_with_the_reason(hub, monkeypatch):
    config.set_setting(O.GLOBAL_SETTING, "pollinations/openai-fast")
    with _ctx():
        assert app._apply_orchestrator("groq", "q", MSGS, 5000, True) == ("groq", "q")
        assert "cannot call the tools" in app.g.hub_orchestrator
        assert app._apply_orchestrator("groq", "q", MSGS, 5000, False) == ("pollinations", "openai-fast")
    monkeypatch.setattr(app, "_model_ctx_info", lambda p, m: (8192, "catalog"))
    with _ctx():
        assert app._apply_orchestrator("groq", "q", MSGS, 54000, False) == ("groq", "q")
        assert "bigger than its window" in app.g.hub_orchestrator
    config.set_setting(O.GLOBAL_SETTING, "cerebras/gone")
    with _ctx():
        assert app._apply_orchestrator("groq", "q", MSGS, 10, False) == ("groq", "q")
        assert "off or has no working key" in app.g.hub_orchestrator


def _chat(client, text, stream=False, session="conv-9"):
    return client.post("/v1/chat/completions", headers={"X-Session-Id": session},
                       json={"model": "auto", "stream": stream,
                             "messages": [{"role": "user", "content": text}]})


def test_the_command_is_answered_by_the_hub_in_every_protocol(hub):
    c = app.app.test_client()
    r = _chat(c, "/orchestrator kimi-k3")
    assert r.status_code == 200
    text = r.get_json()["choices"][0]["message"]["content"]
    assert "Orchestrator for this conversation: nvidia/moonshotai/kimi-k3" in text
    assert r.headers["X-Free-LLM-Hub-Provider"] == "hub"
    with _ctx("conv-9"):
        assert app._orch_effective(app._orch_key(None, MSGS)) == \
            ("nvidia/moonshotai/kimi-k3", "this conversation")
    s = _chat(c, "/orchestrator", stream=True).get_data(as_text=True)
    assert "Orchestrator for this conversation" in s and "[DONE]" in s
    m = c.post("/v1/messages", headers={"X-Claude-Code-Session-Id": "cc-1"},
               json={"model": "auto", "max_tokens": 100, "messages": [{"role": "user", "content": [
                   {"type": "text", "text": "<system-reminder>ctx</system-reminder>"},
                   {"type": "text", "text": "/orchestrator all qwen"}]}]}).get_json()
    assert "all conversations: groq/qwen/qwen3.8-27b" in m["content"][0]["text"]
    assert config.get_setting(O.GLOBAL_SETTING) == "groq/qwen/qwen3.8-27b"
    resp = c.post("/v1/responses", json={"model": "auto", "input": "/orchestrator auto",
                                         "prompt_cache_key": "codex-1"}).get_json()
    assert "Auto" in json.dumps(resp)


def test_an_unknown_name_gets_suggestions_not_a_guess(hub):
    text = _chat(app.app.test_client(), "/orchestrator gemini-9").get_json()[
        "choices"][0]["message"]["content"]
    assert 'No model matches "gemini-9"' in text and "nvidia/moonshotai/kimi-k3" in text


def test_the_dashboard_api_sets_global_and_per_session(hub, monkeypatch):
    c = app.app.test_client()
    assert c.get("/api/orchestrator").get_json()["global"] == "auto"
    r = c.post("/api/orchestrator", json={"model": "nvidia/moonshotai/kimi-k3"}, headers=H)
    assert r.get_json()["global"] == "nvidia/moonshotai/kimi-k3"
    assert c.post("/api/orchestrator", json={"model": "nope"}, headers=H).status_code == 400
    monkeypatch.setattr(app.agentic_chat, "get_session", lambda sid: {"session_id": sid})
    r = c.post("/api/orchestrator", json={"model": "auto", "session_id": "s1"}, headers=H).get_json()
    assert r["session_choice"] == "auto"
    assert c.get("/api/orchestrator?session_id=s1").get_json()["session_choice"] == "auto"
    c.post("/api/orchestrator", json={"model": "", "session_id": "s1"}, headers=H)
    assert c.get("/api/orchestrator?session_id=s1").get_json()["session_choice"] is None
    assert c.post("/api/orchestrator", json={"model": "auto"}, headers=H).get_json()["global"] == "auto"


def test_the_api_is_control_gated(hub, monkeypatch):
    monkeypatch.setattr(config, "get_control_token", lambda: "secret-token")
    c = app.app.test_client()
    assert c.get("/api/orchestrator").status_code == 401
    assert c.post("/api/orchestrator", json={"model": "auto"}).status_code == 403


def test_the_build_page_answers_it_without_starting_the_cli(hub, monkeypatch):
    rec = []
    monkeypatch.setattr(app, "_agent_gate", lambda: None)
    monkeypatch.setattr(app.agentic_chat, "get_session", lambda sid: {
        "session_id": sid, "cli": "opencode", "project_dir": "C:/p", "quality": "normal"})
    monkeypatch.setattr(app.agentic_chat, "precheck_turn", lambda *a, **k: None)
    monkeypatch.setattr(app.agentic_history, "record_turn",
                        lambda sid, cli, d, role, text, **k: rec.append((role, text)))

    def no_cli(*a, **k):
        raise AssertionError("the CLI must not start for /orchestrator")
    monkeypatch.setattr(app.agentic_chat, "send_message_stream_durable", no_cli)
    out = app.app.test_client().post("/api/agent/sessions/s7/message/stream", headers=H,
                                     json={"text": "/orchestrator kimi-k3"}).get_data(as_text=True)
    assert '"event": "done"' in out and "nvidia/moonshotai/kimi-k3" in out
    assert [r[0] for r in rec] == ["user", "agent"]
    assert app._orch_conversations().get("agent:s7") == "nvidia/moonshotai/kimi-k3"


# --------------------------------------------------------------- opencode

def test_opencode_gets_the_command_and_loses_it_on_disconnect(home):
    p = app._p_opencode()
    _wj(p, {"$schema": "https://opencode.ai/config.json", "theme": "x"})
    e = _connect("opencode")
    cmd = _rj(p)["command"]["orchestrator"]
    assert cmd["template"].startswith("[free-llm-hub] /orchestrator") and "$ARGUMENTS" in cmd["template"]
    assert O.parse_command(cmd["template"].replace("$ARGUMENTS", "kimi"))["target"] == "kimi"
    _disconnect_twice(e)
    assert "command" not in _rj(p)


def test_a_users_own_orchestrator_command_is_left_alone(home):
    p = app._p_opencode()
    mine = {"template": "my own thing", "description": "mine"}
    _wj(p, {"$schema": "https://opencode.ai/config.json", "command": {"orchestrator": mine}})
    e = _connect("opencode")
    assert _rj(p)["command"]["orchestrator"] == mine
    _disconnect_twice(e)
    assert _rj(p)["command"]["orchestrator"] == mine


def test_the_pages_have_the_pickers():
    src = open(os.path.join(os.path.dirname(app.__file__), "templates", "index.html"),
               encoding="utf-8").read()
    for needle in ('id="agent-orchestrator"', "initAgentOrchestrator();",
                   "function loadAgentOrchestrator", "'/api/orchestrator'",
                   "ensureAutoOption(sel)", "<code>/orchestrator</code>"):
        assert needle in src, needle
