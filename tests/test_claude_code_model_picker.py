"""Claude Code must KNOW the hub's model ids, or it will not use them properly.

MEASURED 2026-09-27 with Claude Code 2.1.283 pointed at the hub
(ANTHROPIC_BASE_URL=http://127.0.0.1:8787, fresh CLAUDE_CONFIG_DIR):

    claude -p "What is 2 plus 2?" --model auto
    stderr: "auto" isn't described by this version's model catalog; update
            Claude Code, or map it with behavesAs on a modelPicker row ...
            [claude-code:unrecognized_model] {"model":"auto","query_source":"sdk"}

The fix the binary itself names, verified the same way: a `modelPicker` in
user settings with one row per hub id carrying `behavesAs`. Then the same call
is silent, `--model opus` (ANTHROPIC_DEFAULT_OPUS_MODEL=max) goes out as "max",
and CLAUDE_CODE_AUTO_COMPACT_WINDOW=128000 gives effectiveWindow=108000 in the
debug log (MAX_CONTEXT_TOKENS alone is ignored once an id is "known").

Connect writes that mapping and remembers what it replaced; Disconnect removes
exactly it and puts the old values back; /agent sessions get the same.
Every test runs in a throwaway HOME / config dir.
"""
import json
import os
import shutil
import tempfile
import types

import pytest

import agentic_chat as ac
import app

HUB_ROOT = "http://127.0.0.1:%d" % app.PORT
HUB_V1 = HUB_ROOT + "/v1"
KEY = "k-test-claude-models"


@pytest.fixture
def home(monkeypatch):
    d = tempfile.mkdtemp(prefix="hub-pytest-claude-models-")
    monkeypatch.setattr(app, "_home", lambda: d)
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", os.path.join(d, "hubcfg", "config.json"))
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    store = {}
    monkeypatch.setattr(app.config, "set_setting", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(app.config, "get_setting", lambda k, default=None: store.get(k, default))
    monkeypatch.setattr(app.config, "get_local_api_key", lambda: None)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def cfg_home():
    d = tempfile.mkdtemp(prefix="hub-pytest-claude-iso-")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _entry():
    e = dict(next(x for x in app.CLI_REGISTRY if x["id"] == "claude"))
    p = app._p_claude()
    e.update(write_path=p,
             config_paths=[p, os.path.join(os.path.dirname(p), "settings.local.json")])
    return e


def _wj(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _rj(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _connect():
    e = _entry()
    res = app._AUTOFIXERS[e["autofix"]](e, KEY, HUB_ROOT, HUB_V1, "groq/some-model")
    assert res["ok"], res
    return e


# --------------------------------------------------------------------------- #
# The mapping itself: derived from the hub's own list, never hardcoded
# --------------------------------------------------------------------------- #

def test_picker_rows_are_the_opencode_picker_minus_claudes_own_aliases():
    rows = ac.claude_model_picker()["options"]
    ids = [r["model"] for r in rows]
    expected = [m for m in ac._opencode_hub_models() if m not in ac._CLAUDE_CODE_ALIASES]
    assert ids == expected
    assert {"auto", "max", "multi", "swarm", "coding", "coding-swarm", "fast"} <= set(ids)
    assert "best" not in ids          # Claude Code's own alias: sent as claude-fable-5-1
    for r in rows:
        assert r["behavesAs"] == ac._CLAUDE_BEHAVES_AS
        assert r["label"] and r["description"]
        assert app._is_hub_virtual_model(r["model"]), r["model"]
    by_id = {r["model"]: r for r in rows}
    assert by_id["coding-swarm"]["label"] == "coding + swarm"
    assert by_id["auto"]["label"] == "effort: auto"


def test_picker_follows_the_hub_list(monkeypatch):
    real = ac._opencode_hub_models()
    fake = dict(real)
    fake["newcat"] = dict(real["coding"], name="mode: newcat -- new models only")
    monkeypatch.setattr(ac, "_opencode_hub_models", lambda: fake)
    rows = ac.claude_model_picker()["options"]
    assert {"model": "newcat", "label": "mode: newcat", "description": "new models only",
            "behavesAs": ac._CLAUDE_BEHAVES_AS} in rows


def test_picker_replaces_the_builtin_rows():
    assert ac.claude_model_picker()["replaceBuiltInOptions"] is True


def test_hub_env_takes_the_declared_window(monkeypatch):
    # (cli=: the opencode picker rows behind it ask per CLI -- live window steering)
    monkeypatch.setattr(ac, "declared_window", lambda model_id=None, cli=None: 64000)
    env = ac.claude_hub_env()
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "64000"
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "64000"


def test_declared_window_defaults_to_the_codex_figure():
    assert ac.declared_window() == ac._CODEX_CONTEXT_WINDOW
    assert ac.declared_window("coding-swarm") == ac._CODEX_CONTEXT_WINDOW


def test_family_aliases_map_to_hub_tiers():
    env = ac.claude_hub_env()
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "max"
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "auto"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "fast"
    assert ac.claude_hub_env(opus="best")["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "max"


def test_a_family_target_without_a_row_is_left_unmapped(monkeypatch):
    real = ac._opencode_hub_models()
    monkeypatch.setattr(ac, "_opencode_hub_models",
                        lambda: {k: v for k, v in real.items() if k != "fast"})
    assert "ANTHROPIC_DEFAULT_HAIKU_MODEL" not in ac.claude_hub_env()


# --------------------------------------------------------------------------- #
# Connect / Disconnect of a real install
# --------------------------------------------------------------------------- #

def test_connect_writes_the_mapping(home):
    _connect()
    data = _rj(app._p_claude())
    env = data["env"]
    assert env["ANTHROPIC_MODEL"] == "auto"
    for k, v in ac.claude_hub_env().items():
        assert env[k] == v, k
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == str(ac.declared_window())
    assert data["modelPicker"] == ac.claude_model_picker()


def test_disconnect_removes_it_and_restores_prior_values(home):
    p = app._p_claude()
    mine = {"options": [{"model": "opus", "label": "Mine"}]}
    _wj(p, {"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000",
                    "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-8", "FOO": "1"},
            "modelPicker": mine, "permissions": {"allow": ["Bash"]}})
    e = _connect()
    wired = _rj(p)
    assert wired["modelPicker"] == ac.claude_model_picker()
    assert wired["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "max"

    first = app._disconnect_claude(e)
    assert first["changed"]
    after = _rj(p)
    assert after == {"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000",
                             "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-8", "FOO": "1"},
                     "modelPicker": mine, "permissions": {"allow": ["Bash"]}}
    second = app._disconnect_claude(e)
    assert not second.get("changed")
    assert _rj(p) == after


def test_disconnect_with_nothing_before_leaves_no_mapping(home):
    p = app._p_claude()
    e = _connect()
    app._disconnect_claude(e)
    after = _rj(p) if os.path.isfile(p) else {}
    assert "modelPicker" not in after
    for k in ac.claude_hub_env():
        assert k not in (after.get("env") or {}), k


def test_reconnect_keeps_the_first_prior_values(home):
    p = app._p_claude()
    _wj(p, {"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000"}})
    e = _connect()
    _connect()                       # the current values are OURS by now
    app._disconnect_claude(e)
    assert _rj(p)["env"] == {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000"}


def test_a_value_the_user_changed_after_connect_is_theirs(home):
    p = app._p_claude()
    e = _connect()
    data = _rj(p)
    data["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = "150000"
    _wj(p, data)
    app._disconnect_claude(e)
    assert _rj(p)["env"] == {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "150000"}


def test_a_connect_without_a_record_is_still_stripped(home):
    """A settings file an earlier Connect wrote, before the record existed."""
    p = app._p_claude()
    env = {"ANTHROPIC_BASE_URL": HUB_ROOT, "ANTHROPIC_AUTH_TOKEN": KEY,
           "ANTHROPIC_MODEL": "auto", "KEEP": "me"}
    env.update(ac.claude_hub_env())
    _wj(p, {"env": env, "modelPicker": ac.claude_model_picker()})
    app._disconnect_claude(_entry())
    assert _rj(p) == {"env": {"KEEP": "me"}}


# --------------------------------------------------------------------------- #
# /agent sessions (the hub's isolated Claude Code copy)
# --------------------------------------------------------------------------- #

def _fallback(cfg_home, monkeypatch, quality="normal", mode=None, signed_in=False):
    monkeypatch.setattr(ac, "_isolated_signed_in", lambda cli: signed_in)
    monkeypatch.setattr(ac, "_hub_base_url", lambda session_id=None: "http://127.0.0.1:8787")
    env = {}
    ac._apply_claude_hub_fallback(env, cfg_home, quality, None, mode)
    return env


def test_agent_env_carries_the_mapping(cfg_home, monkeypatch):
    env = _fallback(cfg_home, monkeypatch)
    assert env["ANTHROPIC_MODEL"] == "auto"
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == str(ac.declared_window())
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == str(ac.declared_window())
    # opus follows the SESSION's tier: a normal turn runs `--model opus`
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "auto"
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "auto"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "fast"
    assert _rj(os.path.join(cfg_home, "settings.json"))["modelPicker"] == ac.claude_model_picker()


def test_agent_max_session_is_spelled_max(cfg_home, monkeypatch):
    env = _fallback(cfg_home, monkeypatch, quality="max")
    assert env["ANTHROPIC_MODEL"] == "max"
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "max"


def test_agent_mode_session_maps_opus_to_the_mode(cfg_home, monkeypatch):
    env = _fallback(cfg_home, monkeypatch, mode="coding")
    assert env["ANTHROPIC_MODEL"] == "coding"
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "coding"


def test_agent_turn_argv_never_says_best(monkeypatch):
    monkeypatch.setattr(ac, "_hub_backs", lambda cli: True)
    sess = types.SimpleNamespace(quality="max", mode=None)
    assert ac._claude_model_for(sess) == "max"


def test_agent_seed_keeps_other_keys_and_a_foreign_picker(cfg_home, monkeypatch):
    path = os.path.join(cfg_home, "settings.json")
    _wj(path, {"theme": "dark"})
    _fallback(cfg_home, monkeypatch)
    assert _rj(path) == {"theme": "dark", "modelPicker": ac.claude_model_picker()}
    mine = {"options": [{"model": "opus"}]}
    _wj(path, {"modelPicker": mine})
    _fallback(cfg_home, monkeypatch)
    assert _rj(path) == {"modelPicker": mine}


def test_agent_sign_in_removes_the_picker(cfg_home, monkeypatch):
    path = os.path.join(cfg_home, "settings.json")
    _fallback(cfg_home, monkeypatch)
    assert os.path.isfile(path)
    env = _fallback(cfg_home, monkeypatch, signed_in=True)
    assert env == {}
    assert not os.path.exists(path)          # it held nothing but ours
    _wj(path, {"theme": "dark", "modelPicker": ac.claude_model_picker()})
    _fallback(cfg_home, monkeypatch, signed_in=True)
    assert _rj(path) == {"theme": "dark"}


def test_agent_seed_never_creates_a_missing_dir(monkeypatch):
    missing = os.path.join(tempfile.gettempdir(), "hub-pytest-no-such-dir-xyz")
    shutil.rmtree(missing, ignore_errors=True)
    _fallback(missing, monkeypatch)
    assert not os.path.exists(missing)
