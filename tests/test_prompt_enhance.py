"""Prompt enhancement + "best except trivial" routing (2026-07-31).

Two user-requested behaviours, tested where they can break silently:

1. `_enhance_prompt` rewrites the user's OPENING prompt so a two-word ask still
   gets the model's best work. It is a helper call, so it must fail OPEN — any
   error, non-200, empty answer or runaway output returns the ORIGINAL text
   rather than corrupting what the user typed.
2. `_route_by_difficulty` now sends `medium` down the same strongest-model path
   as `hard`; only `simple` keeps the cheap floor-based pick.

No network: the upstream call is monkeypatched.
"""
import pytest

import app
import config


class _Resp:
    """Minimal _dispatch_chat return shape (same surface as _SubResponse)."""

    def __init__(self, status=200, content="", raise_exc=None, bad_json=False):
        self.status_code = status
        self._content = content
        self._raise = raise_exc
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return {"choices": [{"message": {"role": "assistant", "content": self._content}}]}

    def close(self):
        pass


@pytest.fixture
def routed(monkeypatch):
    """Pretend one provider is routable, and capture the payload sent to it."""
    sent = {}
    monkeypatch.setattr(app, "_route_by_difficulty",
                        lambda *a, **k: ("openrouter", "some-model", "simple"))
    monkeypatch.setattr(app, "_build_chain", lambda pid, model, *a, **k: [(pid, model)])
    monkeypatch.setattr(app, "_is_sub", lambda pid: False)
    return sent


def _patch_reply(monkeypatch, resp, sent=None):
    def fake(pid, payload, stream):
        if sent is not None:
            sent["pid"] = pid
            sent["payload"] = payload
        if isinstance(resp, Exception):
            raise resp
        return resp
    monkeypatch.setattr(app, "_dispatch_chat", fake)


# --------------------------------------------------------------------------- #
# _enhance_prompt — the happy path
# --------------------------------------------------------------------------- #

def test_enhanced_prompt_replaces_the_original(monkeypatch, routed):
    _patch_reply(monkeypatch, _Resp(content="A red fox mid-stride in autumn birch woods, "
                                            "low side light, shallow depth of field."),
                 routed)
    out, model = app._enhance_prompt("a fox", "image")
    assert out.startswith("A red fox mid-stride")
    assert model == "openrouter/some-model"


def test_the_kind_selects_the_system_prompt(monkeypatch, routed):
    _patch_reply(monkeypatch, _Resp(content="rewritten"), routed)
    app._enhance_prompt("a fox", "image")
    system = routed["payload"]["messages"][0]
    assert system["role"] == "system"
    assert "image prompt" in system["content"]
    app._enhance_prompt("why is the sky blue", "chat")
    assert "question" in routed["payload"]["messages"][0]["content"]


def test_an_unknown_kind_falls_back_to_chat(monkeypatch, routed):
    _patch_reply(monkeypatch, _Resp(content="rewritten"), routed)
    app._enhance_prompt("hello", "nonsense-kind")
    assert "question" in routed["payload"]["messages"][0]["content"]


def test_surrounding_quotes_are_stripped(monkeypatch, routed):
    _patch_reply(monkeypatch, _Resp(content='"a quoted rewrite"'), routed)
    out, _ = app._enhance_prompt("x", "chat")
    assert out == "a quoted rewrite"


def test_the_helper_call_never_requires_tools_but_forces_a_capable_tier(monkeypatch):
    """It must not consume a tool-capable hop real work is queued on — but it
    MUST override the classifier. MEASURED: routed as the `simple` its text
    classifies as, it landed on a 7B model that answered "fix my python bug"
    instead of rewriting it."""
    seen = {}

    def fake_route(messages, max_tokens=None, est=None, require_tools=False,
                   force_difficulty=None):
        seen.update(require_tools=require_tools, max_tokens=max_tokens,
                    force_difficulty=force_difficulty)
        return None, None, "simple"
    monkeypatch.setattr(app, "_route_by_difficulty", fake_route)
    app._enhance_prompt("hi", "chat")
    assert seen["require_tools"] is False
    assert seen["max_tokens"] == app._ENHANCE_MAX_TOKENS
    assert seen["force_difficulty"] == "medium", "must not route the rewrite as trivial"


@pytest.mark.parametrize("answered", [
    "Sure! Here is how to fix it...",
    "Please provide the specific bug in your Python code.",
    "I can help you with that.",
    "Certainly, let's look at your code.",
    "Here's a corrected version of the function.",
    "As an AI language model, I cannot run code.",
    "Great question! The issue is a typo.",
    "To fix this, change line 4.",
])
def test_a_reply_instead_of_a_rewrite_is_rejected(monkeypatch, routed, answered):
    """The exact live failure this guard exists for: a model that answers the
    prompt would REPLACE the user's question with an assistant reply."""
    _patch_reply(monkeypatch, _Resp(content=answered), routed)
    out, model = app._enhance_prompt("fix my python bug", "chat")
    assert out == "fix my python bug"
    assert model is None


def test_a_genuine_rewrite_is_not_mistaken_for_a_reply(monkeypatch, routed):
    good = ("Debug a Python TypeError raised in my sorting function; give the "
            "corrected code and name the line that caused it.")
    _patch_reply(monkeypatch, _Resp(content=good), routed)
    out, _ = app._enhance_prompt("fix my python bug", "chat")
    assert out == good


def test_force_difficulty_overrides_the_classifier():
    """`hi` classifies simple; forcing medium must survive into the routing."""
    assert app._classify_difficulty([{"role": "user", "content": "hi"}]) == "simple"
    import inspect
    src = inspect.getsource(app._route_by_difficulty)
    assert "force_difficulty or _classify_difficulty" in src


# --------------------------------------------------------------------------- #
# _enhance_prompt — fail-open. Each of these must yield the ORIGINAL text.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("resp", [
    _Resp(status=502, content="nope"),                  # upstream error
    _Resp(content="   "),                               # empty answer
    _Resp(content="x" * 5000),                          # ran away / answered instead
    _Resp(bad_json=True),                               # unparseable body
])
def test_bad_replies_return_the_original(monkeypatch, routed, resp):
    _patch_reply(monkeypatch, resp, routed)
    out, model = app._enhance_prompt("a fox", "image")
    assert out == "a fox"
    assert model is None


def test_upstream_exception_returns_the_original(monkeypatch, routed):
    _patch_reply(monkeypatch, RuntimeError("no key"), routed)
    out, model = app._enhance_prompt("a fox", "image")
    assert out == "a fox" and model is None


def test_no_routable_provider_returns_the_original(monkeypatch):
    monkeypatch.setattr(app, "_route_by_difficulty", lambda *a, **k: (None, None, "simple"))
    out, model = app._enhance_prompt("a fox", "image")
    assert out == "a fox" and model is None


def test_empty_and_oversized_prompts_are_passed_through(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not route for a skipped prompt")
    monkeypatch.setattr(app, "_route_by_difficulty", boom)
    assert app._enhance_prompt("", "chat") == ("", None)
    assert app._enhance_prompt("   ", "chat") == ("", None)
    big = "x" * (app._ENHANCE_MAX_INPUT + 1)
    assert app._enhance_prompt(big, "chat") == (big, None)


def test_a_paid_subscription_hop_is_never_spent_on_a_rewrite(monkeypatch):
    monkeypatch.setattr(app, "_route_by_difficulty",
                        lambda *a, **k: ("sub-claude", "claude", "simple"))
    monkeypatch.setattr(app, "_build_chain", lambda pid, model, *a, **k: [(pid, model)])

    def boom(*a, **k):
        raise AssertionError("a sub-* hop must be skipped")
    monkeypatch.setattr(app, "_dispatch_chat", boom)
    out, model = app._enhance_prompt("a fox", "image")
    assert out == "a fox" and model is None


# --------------------------------------------------------------------------- #
# Anti-slop: the whole point of the feature
# --------------------------------------------------------------------------- #

def test_image_system_prompt_bans_the_usual_slop_tokens():
    sys_prompt = app._ENHANCE_SYSTEM["image"].lower()
    for banned in ("masterpiece", "8k", "ultra-detailed", "hyper-realistic",
                   "award-winning", "trending on artstation", "highly detailed"):
        assert banned in sys_prompt, "%r not explicitly banned" % banned
    # and it must not invent real people or brands
    assert "brand" in sys_prompt and "trademark" in sys_prompt


def test_image_enhancer_must_not_invent_a_style():
    """User direction 2026-07-31: the enhancer clarifies, it does not art-direct.
    An earlier version turned "a fox" into "whimsical ... warm orange tones,
    soft diffused lighting" — inventing a look the user never asked for."""
    sys_prompt = app._ENHANCE_SYSTEM["image"].lower()
    assert "the user directs the image, not you" in sys_prompt
    for forbidden in ("style", "medium", "mood", "lighting", "colour palette",
                      "camera angle", "lens", "setting"):
        assert forbidden in sys_prompt, "%r not named in the do-not-invent list" % forbidden
    # returning the prompt untouched must be framed as correct, not as a failure
    assert "unchanged" in sys_prompt


def test_puter_image_call_sends_no_style_defaults_of_its_own():
    """Separate from the enhancer: the provider adapter itself must not smuggle
    in a style, quality or size default — only the user's prompt goes out."""
    import inspect
    src = inspect.getsource(app._puter_generate_image)
    assert 'args = {"prompt": (prompt or "")[:32000]}' in src
    for smuggled in ("style", "quality", "hd", "vivid", "natural"):
        assert '"%s"' % smuggled not in src, "%r default leaked into the request" % smuggled


def test_chat_system_prompt_bans_roleplay_padding_and_answering():
    sys_prompt = app._ENHANCE_SYSTEM["chat"].lower()
    for banned in ("act as a world-class expert", "think step by step",
                   "provide a comprehensive overview", "flattery"):
        assert banned in sys_prompt, "%r not explicitly banned" % banned
    assert "never answer the question" in sys_prompt
    assert "preserve the user's intent" in sys_prompt


# --------------------------------------------------------------------------- #
# The endpoint
# --------------------------------------------------------------------------- #

@pytest.fixture
def client(monkeypatch):
    # tempfile.mkdtemp, NOT pytest's tmp_path: this machine's pytest temp root
    # is permission-broken (known environmental issue, ~238 suite errors).
    import os
    import tempfile
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG",
                       os.path.join(tempfile.mkdtemp(prefix="hub-pytest-"), "config.json"))
    return app.app.test_client()


def _headers():
    return {"X-Free-LLM-Hub": "dashboard",
            "X-Free-LLM-Hub-Token": config.ensure_control_token()}


def test_endpoint_rejects_an_empty_prompt(client):
    r = client.post("/api/enhance-prompt", json={"prompt": "  "}, headers=_headers())
    assert r.status_code == 400


def test_endpoint_reports_whether_anything_changed(client, monkeypatch):
    monkeypatch.setattr(app, "_enhance_prompt", lambda t, k="chat": ("better prompt", "p/m"))
    r = client.post("/api/enhance-prompt", json={"prompt": "meh", "kind": "chat"},
                    headers=_headers())
    assert r.status_code == 200
    body = r.get_json()
    assert body["original"] == "meh"
    assert body["enhanced"] == "better prompt"
    assert body["changed"] is True
    assert body["model"] == "p/m"

    monkeypatch.setattr(app, "_enhance_prompt", lambda t, k="chat": (t, None))
    r2 = client.post("/api/enhance-prompt", json={"prompt": "already sharp"},
                     headers=_headers())
    assert r2.get_json()["changed"] is False


def test_the_feature_can_be_switched_off(client, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not call the model while disabled")
    monkeypatch.setattr(app, "_enhance_prompt", boom)
    config.set_flag("prompt_enhance", False)
    r = client.post("/api/enhance-prompt", json={"prompt": "hello"}, headers=_headers())
    body = r.get_json()
    assert body["disabled"] is True
    assert body["enhanced"] == "hello" and body["changed"] is False


# --------------------------------------------------------------------------- #
# Routing: best except trivial
# --------------------------------------------------------------------------- #

def test_medium_now_takes_the_same_path_as_hard():
    """Both tiers must reach the strongest-model branch; only `simple` may fall
    through to the cheap _DIFFICULTY_FLOOR pick."""
    import inspect
    src = inspect.getsource(app._route_by_difficulty)
    assert 'if difficulty in ("hard", "medium"):' in src, \
        "medium no longer shares the strongest-model branch with hard"
    floor_at = src.index("_DIFFICULTY_FLOOR[difficulty]")
    branch_at = src.index('if difficulty in ("hard", "medium"):')
    assert branch_at < floor_at, "the floor pick must remain unreachable for medium"
