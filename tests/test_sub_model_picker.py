"""Subscription model picker + manager primitive.

Never runs a real CLI: every subprocess / binary lookup / _sub_run boundary is
monkeypatched. Config lives in a tmp dir (FREE_LLM_HUB_CONFIG).
"""
import pytest

import agentic_chat as ac
import app
import config

_REAL_DETECT = app._sub_detect_models


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(tmp_path / "state" / "config.json"))
    config.invalidate_settings_cache()
    monkeypatch.setattr(app, "_home", lambda: str(tmp_path))
    # No real CLI detection, ever.
    monkeypatch.setattr(app, "_sub_detect_models", lambda pid: [])
    app._SUB_MODEL_CACHE.clear()
    app._SUB_MODEL_REFRESHING.clear()
    with app._MANAGER_LOCK:
        app._MANAGER_TOKENS.update({"day": "", "spent": 0, "calls": 0, "by_purpose": {}})
    yield tmp_path
    config.invalidate_settings_cache()


H = {"X-Free-LLM-Hub": "dashboard"}


# --------------------------------------------------------------------------- #
# model choices + detection parsing
# --------------------------------------------------------------------------- #

def test_defaults_match_previous_behaviour(cfg):
    assert app._sub_selected_model("sub-claude") == "opus"
    assert app._sub_selected_model("sub-codex") == ""
    # the routing identity resolves to the setting, never to a CLI model
    assert app._sub_cli_model("sub-claude", "claude") == "opus"
    assert app._sub_cli_model("sub-codex", "codex") == ""
    assert app._sub_cli_model("sub-claude", "sonnet") == "sonnet"


def test_flag_like_model_is_never_passed(cfg):
    config.set_value("sub_claude_model", "--dangerously-skip-permissions")
    assert app._sub_selected_model("sub-claude") == "opus"
    assert app._sub_cli_model("sub-claude", "-x") == "opus"


def test_claude_help_parsing():
    text = ("  --mcp-config <c>   stuff\n"
            "  --model <model>                       Model for the current session. Provide\n"
            "                                        an alias for the latest model (e.g.\n"
            "                                        'fable', 'opus', or 'sonnet') or a\n"
            "                                        model's full name (e.g.\n"
            "                                        'claude-fable-5').\n"
            "  -n, --name <name>                     Set a display name 'nope'\n")
    assert app._claude_help_models(text) == ["fable", "opus", "sonnet", "claude-fable-5"]
    assert app._claude_help_models("") == []


def test_codex_detection_keeps_visible_slugs_in_priority_order(cfg, monkeypatch):
    monkeypatch.setattr(app, "_sub_bin", lambda pid, model=None: "codex.exe")
    dump = {"models": [
        {"slug": "gpt-5.5", "visibility": "list", "priority": 12},
        {"slug": "internal", "visibility": "hide", "priority": 2},
        {"slug": "gpt-6-astra", "visibility": "list", "priority": 1},
    ]}
    monkeypatch.setattr(app, "_codex_dump_models", lambda binary: dump)
    assert _REAL_DETECT("sub-codex") == ["gpt-6-astra", "gpt-5.5"]


def test_choices_fallback_then_cached_detection(cfg, monkeypatch):
    # master off -> no detection thread, fallback list
    assert app._sub_model_choices("sub-claude") == ["opus", "sonnet", "haiku"]
    assert "sub-claude" not in app._SUB_MODEL_REFRESHING
    monkeypatch.setattr(app, "_sub_detect_models", lambda pid: ["fable", "opus"])
    got = app._sub_model_choices("sub-claude", background=False)
    assert got == ["opus", "sonnet", "haiku", "fable"]
    monkeypatch.setattr(app, "_sub_detect_models", lambda pid: pytest.fail("cached"))
    assert app._sub_model_choices("sub-claude") == got
    # a hand-set selection is always offered
    config.set_value("sub_claude_model", "claude-opus-9")
    assert "claude-opus-9" in app._sub_model_choices("sub-claude")


# --------------------------------------------------------------------------- #
# --model reaches the CLI
# --------------------------------------------------------------------------- #

class _Proc:
    returncode = 0
    stdout = "hello"
    stderr = "tokens used\n1,234\n"


def _arm(monkeypatch, pid):
    monkeypatch.setattr(app, "_sub_master_on", lambda: True)
    monkeypatch.setattr(app, "_sub_state", lambda p: (True, True, True, "ok"))
    monkeypatch.setattr(app, "_sub_bin", lambda p, model=None: "C:/x/" + p)
    monkeypatch.setattr(app, "_sub_launcher", lambda path: [path])
    monkeypatch.setattr(app, "_sub_env", lambda p=None, model=None: {})
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return _Proc()
    monkeypatch.setattr(app.subprocess, "run", fake_run)
    return seen


def test_sub_run_passes_selected_model_to_claude(cfg, monkeypatch):
    seen = _arm(monkeypatch, "sub-claude")
    status, text, _ = app._sub_run("sub-claude", "hi", model="claude")
    assert status == 200 and text == "hello"
    assert seen["argv"][seen["argv"].index("--model") + 1] == "opus"
    assert app._SUB_USAGE.total == 1234
    config.set_value("sub_claude_model", "sonnet")
    app._sub_run("sub-claude", "hi", model="claude")
    assert seen["argv"][seen["argv"].index("--model") + 1] == "sonnet"


def test_sub_run_codex_default_sends_no_model(cfg, monkeypatch):
    seen = _arm(monkeypatch, "sub-codex")
    app._sub_run("sub-codex", "hi", model="codex")
    assert "--model" not in seen["argv"]
    config.set_value("sub_codex_model", "gpt-5.5")
    app._sub_run("sub-codex", "hi", model="codex")
    argv = seen["argv"]
    assert argv[argv.index("--model") + 1] == "gpt-5.5"
    assert argv[-1] == "-"          # prompt still on stdin, last


def test_agent_turn_model_follows_setting(cfg, monkeypatch):
    monkeypatch.setattr(ac, "_session_model_id", lambda sess: None)
    assert ac._claude_model_for(object()) == "opus"
    config.set_value("sub_claude_model", "haiku")
    assert ac._claude_model_for(object()) == "haiku"
    config.set_value("sub_claude_model", "--evil")
    assert ac._claude_model_for(object()) == "opus"


# --------------------------------------------------------------------------- #
# manager primitive
# --------------------------------------------------------------------------- #

def _manager_on(monkeypatch, reply=(200, "plan: do X", None), usage=None):
    config.set_value("manager_model", "sub-claude/sonnet")
    monkeypatch.setattr(app, "_sub_master_on", lambda: True)
    monkeypatch.setattr(app, "_is_model_dead", lambda p, m: False)
    monkeypatch.setattr(app.quota, "record", lambda *a, **k: None)
    calls = []

    def fake_sub_run(pid, prompt, model=None):
        calls.append((pid, model))
        app._SUB_USAGE.total = usage
        return reply
    monkeypatch.setattr(app, "_sub_run", fake_sub_run)
    return calls


MSGS = [{"role": "user", "content": "x" * 400}]


def test_manager_off_by_default(cfg, monkeypatch):
    monkeypatch.setattr(app, "_sub_run", lambda *a, **k: pytest.fail("must not run"))
    assert app._manager_enabled() is False
    assert app._manager_dispatch(MSGS, 100, "plan") == ("", None)
    st = app._manager_status()
    assert st["model"] == "" and st["budget"] == 200000 and st["spent_today"] == 0


def test_manager_dispatch_counts_estimated_tokens(cfg, monkeypatch):
    calls = _manager_on(monkeypatch)
    text, who = app._manager_dispatch(MSGS, 100, "plan")
    assert (text, who) == ("plan: do X", "sub-claude/sonnet")
    assert calls == [("sub-claude", "sonnet")]
    st = app._manager_status()
    assert st["spent_today"] > 100 and st["calls_today"] == 1
    assert st["by_purpose"]["plan"] == st["spent_today"]


def test_manager_prefers_cli_reported_usage(cfg, monkeypatch):
    _manager_on(monkeypatch, usage=5000)
    app._manager_dispatch(MSGS, None, "review")
    assert app._manager_status()["spent_today"] == 5000


def test_manager_budget_enforced(cfg, monkeypatch):
    calls = _manager_on(monkeypatch, usage=950)
    config.set_setting("manager_daily_token_budget", 1000)
    assert app._manager_dispatch(MSGS, None, "a")[1] == "sub-claude/sonnet"
    # 950 spent; this prompt (~100 tokens est) would overrun -> refused, no run
    assert app._manager_dispatch(MSGS, None, "b") == ("", None)
    assert len(calls) == 1
    config.set_setting("manager_daily_token_budget", 0)     # unlimited
    assert app._manager_dispatch(MSGS, None, "c")[1] == "sub-claude/sonnet"


def test_manager_failure_returns_empty(cfg, monkeypatch):
    _manager_on(monkeypatch, reply=(504, "", "timed out"))
    assert app._manager_dispatch(MSGS, None, "plan") == ("", None)
    assert app._manager_status()["spent_today"] == 0


def test_manager_spend_survives_restart(cfg, monkeypatch):
    _manager_on(monkeypatch, usage=777)
    app._manager_dispatch(MSGS, None, "plan")
    blob = app._dead_state_dump()
    with app._MANAGER_LOCK:
        app._MANAGER_TOKENS.update({"day": "", "spent": 0, "calls": 0, "by_purpose": {}})
    app._dead_state_load(blob)
    assert app._manager_status()["spent_today"] == 777
    # yesterday's spend is not revived
    blob["manager_tokens"]["day"] = "1999-01-01"
    with app._MANAGER_LOCK:
        app._MANAGER_TOKENS.update({"day": "", "spent": 0, "calls": 0, "by_purpose": {}})
    app._dead_state_load(blob)
    assert app._manager_status()["spent_today"] == 0


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

def test_api_get_includes_models_and_manager(cfg):
    body = app.app.test_client().get("/api/subscriptions").get_json()
    rows = {r["id"]: r for r in body["providers"]}
    assert rows["sub-claude"]["selected_model"] == "opus"
    assert "sonnet" in rows["sub-claude"]["model_choices"]
    assert rows["sub-codex"]["selected_model"] == ""
    assert body["manager"]["model"] == "" and body["manager"]["budget"] == 200000


def test_api_post_model_manager_and_budget(cfg):
    c = app.app.test_client()
    r = c.post("/api/subscriptions", json={"provider": "sub-codex", "model": "gpt-5.5"}, headers=H)
    assert r.status_code == 200
    assert {x["id"]: x for x in r.get_json()["providers"]}["sub-codex"]["selected_model"] == "gpt-5.5"
    r = c.post("/api/subscriptions", json={"manager_model": "sub-codex/gpt-5.5",
                                           "manager_daily_token_budget": 5000}, headers=H)
    assert r.status_code == 200
    m = r.get_json()["manager"]
    assert m["model"] == "sub-codex/gpt-5.5" and m["budget"] == 5000
    # "" clears back to defaults
    r = c.post("/api/subscriptions", json={"provider": "sub-codex", "model": "",
                                           "manager_model": ""}, headers=H)
    body = r.get_json()
    assert {x["id"]: x for x in body["providers"]}["sub-codex"]["selected_model"] == ""
    assert body["manager"]["model"] == ""


@pytest.mark.parametrize("payload", [
    {"manager_model": "groq/llama"},
    {"manager_model": "sub-claude/--x"},
    {"manager_daily_token_budget": -1},
    {"manager_daily_token_budget": True},
    {"provider": "sub-claude", "model": "-bad"},
])
def test_api_post_rejects_bad_values_without_writing(cfg, payload):
    r = app.app.test_client().post("/api/subscriptions", json=payload, headers=H)
    assert r.status_code == 400
    assert app._manager_model() == ""
    assert app._sub_selected_model("sub-claude") == "opus"


def test_manager_does_not_charge_a_previous_runs_tokens(cfg, monkeypatch):
    """An early return inside _sub_run (here: not signed in) must not leave
    the last run's total on the thread for the manager to charge again."""
    config.set_value("manager_model", "sub-claude/sonnet")
    monkeypatch.setattr(app, "_sub_master_on", lambda: True)
    monkeypatch.setattr(app, "_is_model_dead", lambda p, m: False)
    monkeypatch.setattr(app, "_mark_model_dead", lambda *a, **k: None)
    monkeypatch.setattr(app.quota, "record", lambda *a, **k: None)
    monkeypatch.setattr(app, "_sub_state", lambda pid: (True, True, False, "not signed in"))
    app._SUB_USAGE.total = 12345
    assert app._manager_dispatch(MSGS, None, "verify") == ("", None)
    assert app._manager_status()["spent_today"] == 0


def test_manager_charges_a_cli_that_ran_and_timed_out(cfg, monkeypatch):
    config.set_value("manager_model", "sub-claude/sonnet")
    monkeypatch.setattr(app, "_sub_master_on", lambda: True)
    monkeypatch.setattr(app, "_is_model_dead", lambda p, m: False)
    monkeypatch.setattr(app.quota, "record", lambda *a, **k: None)

    def timed_out(pid, prompt, model=None):
        app._SUB_USAGE.total = None
        app._SUB_USAGE.ran = True
        return 504, "", "timed out"
    monkeypatch.setattr(app, "_sub_run", timed_out)
    assert app._manager_dispatch(MSGS, 500, "fix") == ("", None)
    assert app._manager_status()["spent_today"] >= 500 + 100


def test_concurrent_manager_calls_cannot_overrun_the_budget(cfg, monkeypatch):
    import threading
    import time as _t
    config.set_value("manager_model", "sub-claude/sonnet")
    config.set_setting("manager_daily_token_budget", 1000)
    monkeypatch.setattr(app, "_sub_master_on", lambda: True)
    monkeypatch.setattr(app, "_is_model_dead", lambda p, m: False)
    monkeypatch.setattr(app.quota, "record", lambda *a, **k: None)

    def slow(pid, prompt, model=None):
        _t.sleep(0.3)
        app._SUB_USAGE.total = 400
        app._SUB_USAGE.ran = True
        return 200, "ok", None
    monkeypatch.setattr(app, "_sub_run", slow)
    answered = []
    threads = [threading.Thread(
        target=lambda: answered.append(app._manager_dispatch(MSGS, 300, "verify")[1]))
        for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ok = [w for w in answered if w]
    # each reserves ~100 prompt + 300 output: at most 2 fit in 1000 at once
    assert 1 <= len(ok) <= 2
    assert app._manager_status()["spent_today"] <= 1000
    assert app._MANAGER_RESERVED[0] == 0
