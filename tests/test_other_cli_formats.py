"""The config-only CLIs (qwen, openclaw, aider) get a file in their documented
format with every hub tier and its window, and Disconnect leaves nothing.

Found 2026-09-27 by connect/disconnect round trips in a throwaway home:
  * qwen got only ~/.qwen/.env -- no modelProviders (so /model had no tiers and
    no contextWindowSize) and no security.auth.selectedType, which a fresh
    Qwen Code needs to use the env instead of opening its auth dialog;
  * openclaw listed only "auto", and a Connect-created openclaw.json was left
    behind as "{}" after Disconnect;
  * aider pinned "openai/<one concrete free model>" (skipping orchestration,
    dying with that provider) and had no window for it.
Every test runs in a throwaway HOME."""
import json
import os
import shutil
import tempfile

import pytest
import yaml

import app

ROOT = "http://127.0.0.1:%d" % app.PORT
V1 = ROOT + "/v1"
KEY = "k-test-456"


@pytest.fixture
def home(monkeypatch):
    d = tempfile.mkdtemp(prefix="hub-pytest-formats-")
    monkeypatch.setattr(app, "_home", lambda: d)
    monkeypatch.setenv("XDG_CONFIG_HOME", os.path.join(d, ".config"))
    monkeypatch.setenv("OPENCLAW_CONFIG", os.path.join(d, ".openclaw", "openclaw.json"))
    store = {}
    monkeypatch.setattr(app.config, "set_setting", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(app.config, "get_setting", lambda k, default=None: store.get(k, default))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _entry(cid):
    e = dict(app._get_cli_entry(cid))
    if cid == "qwen":
        p = app._p_qwen_env()
        e.update(write_path=p, config_paths=[os.path.join(os.path.dirname(p), "settings.json"), p])
    elif cid == "aider":
        e.update(write_path=app._p_aider(), config_paths=[app._p_aider()])
    elif cid == "openclaw":
        e.update(write_path=app._p_openclaw(), config_paths=[app._p_openclaw()])
    return e


def _files(d):
    out = []
    for dp, _, fs in os.walk(d):
        out += [os.path.relpath(os.path.join(dp, f), d) for f in fs]
    return sorted(out)


def _rj(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _wj(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _connect(cid):
    e = _entry(cid)
    res = app._AUTOFIXERS[e["autofix"]](e, KEY, ROOT, V1, "groq/some-model")
    assert res["ok"], res
    assert app._cli_connected(e)[0]
    return e, res


def _disconnect(e):
    out = app._DISCONNECTERS[e["autofix"]](e)
    assert not app._cli_connected(e)[0]
    return out


# ---------------------------------------------------------------- qwen

def test_qwen_gets_the_documented_one_file_setup_with_every_tier(home):
    e, res = _connect("qwen")
    s = _rj(os.path.join(home, ".qwen", "settings.json"))
    ids = [p["id"] for p in s["modelProviders"]["openai"]]
    assert ids == list(app._HUB_TIER_IDS)
    for p in s["modelProviders"]["openai"]:
        assert p["baseUrl"] == V1 and p["envKey"] == "OPENAI_API_KEY"
        assert p["generationConfig"]["contextWindowSize"] >= 32000
    assert s["security"]["auth"]["selectedType"] == "openai"
    assert s["model"]["name"] == "auto"
    assert KEY not in json.dumps(s)                  # the key lives in .env only
    assert KEY not in repr(res)


def test_qwen_round_trip_from_nothing_leaves_nothing(home):
    e, _ = _connect("qwen")
    _disconnect(e)
    assert _files(home) == []


def test_qwen_round_trip_restores_the_users_settings(home):
    sp = os.path.join(home, ".qwen", "settings.json")
    user = {"security": {"auth": {"selectedType": "qwen-oauth"}},
            "model": {"name": "qwen3-coder-plus"},
            "modelProviders": {"openai": [{"id": "mine", "baseUrl": "https://x.example/v1",
                                           "envKey": "X_KEY"}]},
            "ui": {"theme": "dark"}}
    _wj(sp, user)
    e, _ = _connect("qwen")
    s = _rj(sp)
    assert s["modelProviders"]["openai"][0]["id"] == "mine"     # kept, ours appended
    assert s["ui"] == {"theme": "dark"}
    _disconnect(e)
    assert _rj(sp) == user
    assert not os.path.exists(sp + ".freehub-bak")


# ---------------------------------------------------------------- openclaw

def test_openclaw_lists_every_tier_with_a_window(home):
    _connect("openclaw")
    data = _rj(app._p_openclaw())
    models = data["models"]["providers"]["freehub"]["models"]
    assert [m["id"] for m in models] == list(app._HUB_TIER_IDS)
    assert all(m["contextWindow"] >= 32000 and m["maxTokens"] > 0 for m in models)
    allow = data["agents"]["defaults"]["models"]
    assert {"freehub/" + t for t in app._HUB_TIER_IDS} <= set(allow)
    assert data["agents"]["defaults"]["model"]["primary"] == "freehub/auto"


def test_openclaw_file_created_by_connect_is_removed(home):
    e, _ = _connect("openclaw")
    out = _disconnect(e)
    assert out.get("deleted") is True
    assert _files(home) == []


# ---------------------------------------------------------------- aider

def test_aider_routes_through_auto_and_knows_the_windows(home):
    _connect("aider")
    with open(app._p_aider(), encoding="utf-8") as f:
        conf = yaml.safe_load(f)
    assert conf["model"] == "openai/auto"            # never a pinned concrete model
    assert conf["openai-api-base"] == V1
    meta = _rj(app._p_aider_metadata())
    for t in app._HUB_TIER_IDS:
        m = meta["openai/" + t]
        assert m["max_input_tokens"] >= 32000 and m["input_cost_per_token"] == 0
        assert m["litellm_provider"] == "openai"


def test_aider_round_trip_keeps_the_users_metadata(home):
    mp = app._p_aider_metadata()
    _wj(mp, {"my/model": {"max_input_tokens": 1000}})
    e, _ = _connect("aider")
    _disconnect(e)
    assert _rj(mp) == {"my/model": {"max_input_tokens": 1000}}
    assert not os.path.exists(app._p_aider())


def test_aider_round_trip_from_nothing_leaves_nothing(home):
    e, _ = _connect("aider")
    _disconnect(e)
    assert _files(home) == []


# ---------------------------------------------------------------- opencode

def test_opencode_reports_the_model_it_wrote(home):
    e = dict(app._get_cli_entry("opencode"))
    p = app._p_opencode()
    e.update(write_path=p, config_paths=[p])
    res = app._autofix_opencode(e, KEY, ROOT, V1, "groq/some-model")
    assert _rj(p)["model"] == "free-llm-hub/auto"
    assert res["applied"]["model"] == "free-llm-hub/auto"


# ---------------------------------------------------------------- hub mode OFF

def test_byte_restore_also_reverts_side_files(home):
    """Hub mode OFF byte-restores write_path only; the second file each of
    these Connects writes must not survive it."""
    for cid in ("qwen", "aider"):
        _connect(cid)
    os.remove(app._p_qwen_env())                     # what the byte-restore does
    os.remove(app._p_aider())
    app._revert_side_files("qwen", app._p_qwen_env())
    app._revert_side_files("aider", app._p_aider())
    assert _files(home) == []
