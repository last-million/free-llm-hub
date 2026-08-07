"""Tests for crew AUTO-ESCALATION and the agent crew-delegation hint.

User request 2026-08-06: an API client (hermes, openclaw, a script) has no
human to answer the dashboard's project gate, so the hub itself decides: a
tool-free, image-free, OPENING-turn 'auto' request that reads as a full
project routes to the crew pipeline. Agent CLIs (tool-carrying turns) are
never touched — instead their opening turn gets a short hint that crews exist
and how to call one, so the agent delegates per its own judgement.

NOTE: no pytest tmp_path here — this machine's basetemp is permission-denied;
tempfile.mkdtemp(prefix="hub-pytest-") works.
"""

import os
import shutil
import tempfile

import pytest

import app
import config
import crews


@pytest.fixture
def isolated_config(monkeypatch):
    root = tempfile.mkdtemp(prefix="hub-pytest-")
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", os.path.join(root, "state", "config.json"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


PROJECT = ("Build me a complete multi-page website for my restaurant with "
           "a menu page, a gallery, online reservations and a contact form")


def _post(client, body):
    return client.post("/v1/chat/completions", json=body)


@pytest.fixture
def spy_crew(monkeypatch):
    """Capture crew invocations; the pipeline itself is stubbed out."""
    calls = []
    # on_event is optional and forwarded to swarm.run (app.py feeds the
    # activity feed's per-agent view through it) -- the spy must mirror the
    # real signature or every crew request 500s on an unexpected kwarg.
    monkeypatch.setattr(crews, "run",
                        lambda messages, dispatch, name, on_event=None:
                            calls.append(name) or {"crew": name})
    monkeypatch.setattr(crews, "format_answer",
                        lambda result: "crew answer via " + result["crew"])
    # If escalation does NOT fire the request must not wander onto the real
    # network — make routing fail fast and cheap instead.
    monkeypatch.setattr(app, "_route_by_difficulty", lambda *a, **k: (None, None, None))
    monkeypatch.setattr(app, "_resolve_model", lambda m: (None, "no models in test"))
    return calls


# --------------------------------------------------------------------------- #
# The heuristic (server twin of looksLikeFullProject in index.html)
# --------------------------------------------------------------------------- #

def test_heuristic_fires_on_full_projects():
    assert crews.looks_like_full_project(PROJECT) is True
    assert crews.looks_like_full_project(
        "Create a full-stack e-commerce app from scratch") is True
    assert crews.looks_like_full_project(
        "Develop a SaaS dashboard application with user authentication, "
        "billing, an admin panel, and reporting charts") is True


def test_heuristic_stays_quiet_on_basic_asks():
    assert crews.looks_like_full_project(
        "Create a landing page for a coffee shop") is False
    assert crews.looks_like_full_project(
        "what is the difference between TCP and UDP and ICMP and ARP?") is False
    assert crews.looks_like_full_project("fix my python bug please") is False
    assert crews.looks_like_full_project("") is False


# --------------------------------------------------------------------------- #
# Endpoint escalation
# --------------------------------------------------------------------------- #

def test_opening_project_on_auto_escalates_to_crew(isolated_config, spy_crew):
    client = app.app.test_client()
    resp = _post(client, {"model": "auto",
                          "messages": [{"role": "user", "content": PROJECT}]})
    assert resp.status_code == 200
    assert spy_crew == ["auto"]
    assert "crew answer via auto" in resp.get_json()["choices"][0]["message"]["content"]


def test_tool_carrying_turn_is_never_escalated(isolated_config, spy_crew):
    client = app.app.test_client()
    _post(client, {"model": "auto",
                   "messages": [{"role": "user", "content": PROJECT}],
                   "tools": [{"type": "function", "function": {
                       "name": "f", "parameters": {"type": "object"}}}]})
    assert spy_crew == []


def test_explicit_model_is_never_escalated(isolated_config, spy_crew):
    client = app.app.test_client()
    _post(client, {"model": "groq/llama-3.3-70b-versatile",
                   "messages": [{"role": "user", "content": PROJECT}]})
    assert spy_crew == []


def test_mid_conversation_is_never_escalated(isolated_config, spy_crew):
    client = app.app.test_client()
    _post(client, {"model": "auto", "messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": PROJECT}]})
    assert spy_crew == []


def test_flag_off_disables_escalation(isolated_config, spy_crew):
    config.set_flag("crew_auto_escalate", False)
    client = app.app.test_client()
    _post(client, {"model": "auto",
                   "messages": [{"role": "user", "content": PROJECT}]})
    assert spy_crew == []


# --------------------------------------------------------------------------- #
# The agent crew-delegation hint in _apply_craft_brief
# --------------------------------------------------------------------------- #

def test_agentic_opening_turn_gets_the_crew_hint(isolated_config):
    msgs = [{"role": "system", "content": "You are a coding agent."},
            {"role": "user", "content": "tweak the retry constant"}]
    out = app._apply_craft_brief(msgs, agentic=True)
    hints = [m for m in out if m.get("role") == "system"
             and "CREW DELEGATION" in (m.get("content") or "")]
    assert len(hints) == 1
    # After the caller's own system prompt (and after any craft brief), but
    # always BEFORE the user's first message.
    first_user = next(i for i, m in enumerate(out) if m.get("role") == "user")
    assert 1 <= out.index(hints[0]) < first_user


def test_non_agentic_turn_without_craft_match_gets_nothing(isolated_config):
    msgs = [{"role": "user", "content": "tweak the retry constant"}]
    out = app._apply_craft_brief(msgs, agentic=False)
    assert out is msgs or len(out) == len(msgs)


def test_hint_never_injected_mid_loop(isolated_config):
    msgs = [{"role": "system", "content": "agent"},
            {"role": "user", "content": "refactor the parser"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "1", "type": "function",
                             "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "1", "content": "ok"}]
    out = app._apply_craft_brief(msgs, agentic=True)
    assert out is msgs


def test_hint_flag_off(isolated_config):
    config.set_flag("crew_agent_hint", False)
    msgs = [{"role": "user", "content": "refactor the parser"}]
    out = app._apply_craft_brief(msgs, agentic=True)
    assert all("CREW DELEGATION" not in (m.get("content") or "") for m in out)
