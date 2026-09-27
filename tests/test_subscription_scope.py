"""subscription_scope: "all" (default, legacy) vs "manager_only".

manager_only must keep sub-* out of EVERY routing path (candidates, primary,
chain, /v1/models listing, explicit picks, the dispatch shim) while
_manager_dispatch still spends the subscription. "all" must be unchanged.

Never runs a real CLI: _sub_run / _sub_state / _sub_models are faked, and the
config lives in a tmp dir (FREE_LLM_HUB_CONFIG).
"""
import pytest

import app
import config
import quota

H = {"X-Free-LLM-Hub": "dashboard"}
FREE = {"groq": ["llama-3.3-70b-versatile"]}
SUBS = ("sub-claude", "sub-codex")


def _msgs(text):
    return [{"role": "user", "content": text}]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(tmp_path / "state" / "config.json"))
    config.invalidate_settings_cache()
    monkeypatch.setattr(app, "_home", lambda: str(tmp_path))
    monkeypatch.setattr(app, "_sub_detect_models", lambda pid: [])
    app._SUB_MODEL_CACHE.clear()
    app._SUB_MODEL_REFRESHING.clear()
    with app._MANAGER_LOCK:
        app._MANAGER_TOKENS.update({"day": "", "spent": 0, "calls": 0, "by_purpose": {}})
    saved_q = (dict(quota._STATE), dict(quota._MODEL_STATE),
               dict(quota._MODEL_THROTTLE), dict(quota._DYNAMIC),
               quota._PERSIST_PATH, quota._persist_last)
    for d in (quota._STATE, quota._MODEL_STATE, quota._MODEL_THROTTLE, quota._DYNAMIC):
        d.clear()
    quota._PERSIST_PATH = None
    # Subscriptions: master ON, both providers enabled + signed in, none dead.
    config.set_flag(app._SUB_MASTER_FLAG, True)
    monkeypatch.setattr(app, "_sub_state", lambda pid: (True, True, True, "ok"))
    monkeypatch.setattr(app, "_sub_models",
                        lambda pid, background=False: ["opus"] if pid == "sub-claude" else ["codex"])
    monkeypatch.setattr(app, "_is_model_dead", lambda pid, m: False)
    monkeypatch.setattr(app, "_context_ok", lambda pid, m, est: True)
    monkeypatch.setattr(app.prov, "is_model_allowed", lambda m: True)
    monkeypatch.setattr(app, "_aa_scores", {})
    monkeypatch.setattr(app.quota, "record", lambda *a, **k: None)
    calls = []

    def fake_sub_run(pid, prompt, model=None):
        calls.append((pid, model))
        return 200, "sub answer", None
    monkeypatch.setattr(app, "_sub_run", fake_sub_run)
    app._session_pins.clear()
    yield calls
    app._session_pins.clear()
    (s, m, t, d, quota._PERSIST_PATH, quota._persist_last) = saved_q
    for live, old in ((quota._STATE, s), (quota._MODEL_STATE, m),
                      (quota._MODEL_THROTTLE, t), (quota._DYNAMIC, d)):
        live.clear()
        live.update(old)
    config.invalidate_settings_cache()


def _free(monkeypatch, models):
    monkeypatch.setattr(app, "_available_providers", lambda: list(models))
    monkeypatch.setattr(app, "_auto_models", lambda pid: list(models.get(pid, ())))


def _scope(value):
    config.set_setting(app._SUB_SCOPE_SETTING, value)


# --------------------------------------------------------------------------- #
# defaults / "all" unchanged
# --------------------------------------------------------------------------- #

def test_default_scope_is_all_and_legacy_routing_unchanged(env, monkeypatch):
    assert app._sub_scope() == "all"
    assert app._sub_routing_on() is True
    assert app._sub_available_providers() == list(SUBS)
    _free(monkeypatch, FREE)
    chain = app._build_chain("groq", "llama-3.3-70b-versatile", messages=_msgs("hi"))
    assert ("sub-claude", "opus") in chain and ("sub-codex", "codex") in chain
    # sub-* stays the TAIL, after the free model.
    assert chain[0] == ("groq", "llama-3.3-70b-versatile")
    _free(monkeypatch, {})
    pid, model, _d = app._route_by_difficulty(_msgs("hi"))
    assert pid in SUBS
    assert app._resolve_model("sub-claude/opus") == ("sub-claude", "opus")
    assert app._check_provider_ready("sub-claude") is None
    ids = {m["id"] for m in app.aggregated_models()}
    assert "sub-claude/opus" in ids


def test_all_scope_dispatch_shim_still_runs_the_cli(env):
    resp = app._dispatch_chat("sub-claude", {"model": "opus", "messages": _msgs("hi")}, False)
    assert resp.status_code == 200
    assert env == [("sub-claude", "opus")]


def test_unknown_scope_value_falls_back_to_all(env):
    _scope("bogus")
    assert app._sub_scope() == "all"


# --------------------------------------------------------------------------- #
# manager_only
# --------------------------------------------------------------------------- #

def test_manager_only_keeps_sub_out_of_every_routing_path(env, monkeypatch):
    _scope("manager_only")
    assert app._sub_master_on() is True          # master switch untouched
    assert app._sub_routing_on() is False
    assert app._sub_available_providers() == []
    # chain: free fleet present -> no sub tail (also for tool requests)
    _free(monkeypatch, FREE)
    for kw in ({}, {"require_tools": True}, {"pinned": True}):
        chain = app._build_chain("groq", "llama-3.3-70b-versatile",
                                 messages=_msgs("hi"), **kw)
        assert not any(app._is_sub(p) for p, _m in chain), (kw, chain)
    # primary: nothing free alive -> no primary at all, never a sub
    _free(monkeypatch, {})
    for diff in (None, "simple", "medium", "hard"):
        pid, _m, _d = app._route_by_difficulty(_msgs("hi"), force_difficulty=diff)
        assert pid is None, diff
    # /v1/models listing
    assert not any(app._is_sub(m["provider"]) for m in app.aggregated_models())
    # explicit pick is refused with an honest error, not silently routed
    pid, err = app._resolve_model("sub-claude/opus")
    assert pid is None and "Manager only" in err
    assert "Manager only" in (app._check_provider_ready("sub-codex") or "")
    assert env == []                             # no CLI ran


def test_manager_only_dispatch_shim_refuses_without_running(env):
    _scope("manager_only")
    resp = app._dispatch_chat("sub-claude", {"model": "opus", "messages": _msgs("hi")}, False)
    assert resp.status_code == 403
    assert "Manager only" in resp.json()["error"]["message"]
    assert env == []


def test_manager_only_manager_dispatch_still_calls_subscription(env):
    _scope("manager_only")
    config.set_value("manager_model", "sub-claude/sonnet")
    assert app._manager_enabled() is True
    text, who = app._manager_dispatch(_msgs("x" * 400), 100, "plan")
    assert (text, who) == ("sub answer", "sub-claude/sonnet")
    assert env == [("sub-claude", "sonnet")]


def test_manager_only_v1_chat_never_spends_subscription(env, monkeypatch):
    _scope("manager_only")
    _free(monkeypatch, {})
    c = app.app.test_client()
    r = c.post("/v1/chat/completions",
               json={"model": "sub-claude/opus", "messages": _msgs("hi")})
    assert r.status_code >= 400
    r = c.post("/v1/chat/completions",
               json={"model": "auto", "messages": _msgs("hi")})
    assert r.status_code >= 400
    assert env == []


def test_manager_off_when_master_off_even_with_scope(env):
    _scope("manager_only")
    config.set_flag(app._SUB_MASTER_FLAG, False)
    config.set_value("manager_model", "sub-claude/sonnet")
    assert app._manager_enabled() is False
    assert app._manager_dispatch(_msgs("x"), 100, "plan") == ("", None)
    assert env == []


# --------------------------------------------------------------------------- #
# API + dashboard
# --------------------------------------------------------------------------- #

def test_api_get_and_post_scope(env):
    c = app.app.test_client()
    body = c.get("/api/subscriptions").get_json()
    assert body["subscription_scope"] == "all" and body["routing_enabled"] is True
    r = c.post("/api/subscriptions", json={"subscription_scope": "manager_only"}, headers=H)
    assert r.status_code == 200
    body = r.get_json()
    assert body["subscription_scope"] == "manager_only"
    assert body["routing_enabled"] is False and body["enabled"] is True
    assert config.get_setting("subscription_scope") == "manager_only"
    r = c.post("/api/subscriptions", json={"subscription_scope": "all"}, headers=H)
    assert r.get_json()["subscription_scope"] == "all"


@pytest.mark.parametrize("bad", ["", "manager", None, 1, ["all"]])
def test_api_post_rejects_bad_scope_without_writing(env, bad):
    r = app.app.test_client().post(
        "/api/subscriptions",
        json={"subscription_scope": bad, "manager_model": "sub-claude/sonnet"}, headers=H)
    assert r.status_code == 400
    assert config.get_setting("subscription_scope") is None
    assert app._manager_model() == ""


def test_dashboard_has_scope_radio():
    import os
    path = os.path.join(os.path.dirname(app.__file__), "templates", "index.html")
    with open(path, encoding="utf-8") as fh:
        html = fh.read()
    assert 'name="sub-scope" value="manager_only"' in html
    assert 'name="sub-scope" value="all"' in html
    assert "Manager only (recommended)" in html
    assert "Manager + fallback for all traffic" in html
    assert "subscription_scope: el.value" in html
