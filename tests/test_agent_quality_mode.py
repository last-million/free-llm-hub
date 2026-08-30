"""A CLI session picks its model quality once, at start.

USER REQUEST 2026-08-30 was "ask at the beginning: swarm agents or normal
agentic mode". Swarm is NOT offered, and deliberately so: the swarm emits
finished prose, never tool calls, and _swarm_completion refuses tool-carrying
turns outright -- a "swarm mode" CLI session would write no files and do
nothing at all. Forcing the top tier is the working version of "use the best
models", so that is what the question asks.

The answer is baked into the CLI subprocess's environment at launch
(ANTHROPIC_MODEL=best vs auto), because the subprocess is an ordinary API
client that carries no session identity -- the model name is the only channel
back to the router, and it travels with every turn including the small ones.
"""
from unittest import mock

import agentic_chat
import app


def test_best_is_an_orchestrate_id():
    """It must route like `auto`, not be mistaken for a provider/model pin."""
    assert app._is_orchestrate("best")
    assert app._is_orchestrate("BEST")
    assert not app._is_orchestrate("groq/best")


def test_quality_mode_lifts_the_cheap_tier():
    msgs = [{"role": "user", "content": "hi"}]
    with mock.patch.object(app, "_available_providers", return_value=[]):
        _p, _m, normal = app._route_by_difficulty(msgs, 512, 20)
        _p, _m, maxq = app._route_by_difficulty(msgs, 512, 20, quality_mode=True)
    assert normal == "simple"
    assert maxq == "medium", "max quality must never take the cheap tier"


def test_quality_mode_does_not_downgrade_a_hard_task():
    msgs = [{"role": "user", "content": "build me a landing page for a bakery"}]
    with mock.patch.object(app, "_available_providers", return_value=[]):
        _p, _m, diff = app._route_by_difficulty(msgs, 2048, 200, quality_mode=True)
    assert diff == "hard"


def test_a_session_records_its_quality():
    s = agentic_chat._Session("claude", "/tmp/x", quality="max")
    assert s.quality == "max"
    assert agentic_chat._Session("claude", "/tmp/x").quality == "normal"


def test_an_unknown_quality_falls_back_to_normal():
    """An older dashboard, or a hand-rolled API call, must not break."""
    for bad in ("swarm", "", None, "MAX", 7):
        assert agentic_chat._Session("claude", "/tmp/x", quality=bad).quality == "normal"


def test_max_launches_the_cli_pointed_at_best():
    env = {}
    with mock.patch.object(agentic_chat, "_isolated_signed_in", return_value=False), \
            mock.patch.object(agentic_chat, "_hub_base_url", return_value="http://h"):
        agentic_chat._apply_claude_hub_fallback(env, "/cfg", "max")
    assert env["ANTHROPIC_MODEL"] == "best"


def test_normal_launches_the_cli_pointed_at_auto():
    env = {}
    with mock.patch.object(agentic_chat, "_isolated_signed_in", return_value=False), \
            mock.patch.object(agentic_chat, "_hub_base_url", return_value="http://h"):
        agentic_chat._apply_claude_hub_fallback(env, "/cfg", "normal")
    assert env["ANTHROPIC_MODEL"] == "auto"


def test_a_real_subscription_is_still_never_overridden():
    """These vars OVERRIDE stored login credentials whenever Claude Code sees
    them -- setting them on a signed-in copy would make the sign-in useless."""
    env = {}
    with mock.patch.object(agentic_chat, "_isolated_signed_in", return_value=True):
        agentic_chat._apply_claude_hub_fallback(env, "/cfg", "max")
    assert env == {}
