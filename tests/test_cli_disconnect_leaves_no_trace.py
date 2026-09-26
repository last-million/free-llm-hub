"""Disconnect means the CLI no longer sees the hub -- in any file.

REPORTED 2026-09-26: "when I disconnect a CLI from the hub it should not show
anymore the models and efforts and modes of Calvoun hub -- they are still there
and when I want to use them I have issues."

Three leftovers produced that:
  1. codex's /model catalog (~/.codex/model_catalog.json, the hub's category
     entries with four effort tiers) was (re)written at EVERY hub start whether
     codex was connected or not, and Disconnect never removed it;
  2. the CLIs' own pickers write the hub's virtual ids ("coding",
     "coding-swarm", "multi") back into their config as the default model --
     a provider strip left those behind, pointing at nothing;
  3. the pre-hub default the connector replaced was dropped, not restored.

Every test runs in a throwaway HOME: no real ~/.codex, ~/.config, ~/.claude.
"""
import json
import os
import shutil
import tempfile

import pytest

import app
import mcp_manager

try:
    import tomllib
except ImportError:                                              # pragma: no cover
    tomllib = None

HUB_ROOT = "http://127.0.0.1:%d" % app.PORT
HUB_V1 = HUB_ROOT + "/v1"
KEY = "k-test-123"


@pytest.fixture
def home(monkeypatch):
    d = tempfile.mkdtemp(prefix="hub-pytest-disconnect-")
    monkeypatch.setattr(app, "_home", lambda: d)
    monkeypatch.setenv("XDG_CONFIG_HOME", os.path.join(d, ".config"))
    monkeypatch.setenv("HERMES_HOME", os.path.join(d, "hermes"))
    monkeypatch.setenv("OPENCLAW_CONFIG", os.path.join(d, ".openclaw", "openclaw.json"))
    monkeypatch.setenv("MCP_MANAGER_HOME", d)
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", os.path.join(d, "hubcfg", "config.json"))
    for v in ("OPENCLAW_CONFIG_PATH", "OPENCLAW_STATE_DIR", "OPENCLAW_HOME",
              "OPENAI_BASE_URL", "OPENAI_API_BASE", "ANTHROPIC_BASE_URL",
              "GOOGLE_GEMINI_BASE_URL", "GEMINI_API_BASE_URL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(mcp_manager, "_REPO_DIR", os.path.join(d, "repo"))
    store = {}
    monkeypatch.setattr(app.config, "set_setting", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(app.config, "get_setting", lambda k, default=None: store.get(k, default))
    monkeypatch.setattr(app.config, "get_local_api_key", lambda: None)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _original(cid):
    return next(e for e in app.CLI_REGISTRY if e["id"] == cid)


def _entry(cid):
    """The real registry entry, re-pointed at the throwaway HOME (entries
    resolve their paths at import time, i.e. against the real home)."""
    e = dict(_original(cid))
    if cid == "claude":
        p = app._p_claude()
        e.update(write_path=p,
                 config_paths=[p, os.path.join(os.path.dirname(p), "settings.local.json")])
    elif cid == "pi":
        e.update(write_path=app._p_pi_models(), config_paths=[app._p_pi_models()])
    elif cid == "aider":
        e.update(write_path=app._p_aider(), config_paths=[app._p_aider()])
    elif cid == "opencode":
        p = app._p_opencode()
        e.update(write_path=p,
                 config_paths=[p, os.path.join(os.path.dirname(p), "opencode.jsonc")])
    elif cid == "qwen":
        p = app._p_qwen_env()
        e.update(write_path=p,
                 config_paths=[os.path.join(os.path.dirname(p), "settings.json"), p])
    elif cid == "codex":
        e.update(write_path=app._p_codex(), config_paths=[app._p_codex()])
    elif cid == "openclaw":
        e.update(write_path=app._p_openclaw(), config_paths=[app._p_openclaw()])
    elif cid == "hermes":
        e.update(write_path=app._p_hermes(), config_paths=[app._p_hermes()])
    elif cid == "kimi":
        e.update(write_path=app._p_kimi(), config_paths=[app._p_kimi()])
    return e


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _wj(path, data):
    _write(path, json.dumps(data, indent=2) + "\n")


def _rj(path):
    return json.loads(_read(path))


def _connect(cid):
    e = _entry(cid)
    res = app._AUTOFIXERS[e["autofix"]](e, KEY, HUB_ROOT, HUB_V1, "groq/some-model")
    assert res["ok"], res
    assert app._cli_connected(e)[0], "%s should read as connected after Connect" % cid
    return e


def _assert_no_trace(path):
    """Nothing in the file still points at, names, or depends on the hub --
    the hub's MCP tool server aside (kept by design, reported separately)."""
    if not os.path.isfile(path):
        return
    text = app._strip_hub_mcp_table(_read(path))
    assert not any(fr in text for fr in app._hub_fragments()), text
    for word in ("freehub", "free-hub", "free-llm-hub", "Calvoun", KEY):
        assert word not in text, "%r left in %s:\n%s" % (word, path, text)


def _disconnect_twice(entry):
    """Disconnect, then prove a second Disconnect is a no-op."""
    reverter = app._DISCONNECTERS[entry.get("autofix") or entry["id"]]
    first = reverter(entry)
    snap = {}
    for p in entry["config_paths"] + [entry["write_path"]]:
        snap[p] = _read(p) if os.path.isfile(p) else None
    second = reverter(entry)
    assert not second.get("changed"), second
    for p, before in snap.items():
        assert (_read(p) if os.path.isfile(p) else None) == before, p
    assert not app._cli_connected(entry)[0], "%s still reads as connected" % entry["id"]
    assert not os.path.exists(entry["write_path"] + ".freehub-bak"), "backup left behind"
    return first


# --------------------------------------------------------------------------- #
# codex: config.toml + the /model catalog
# --------------------------------------------------------------------------- #

CODEX_USER = (
    'model = "gpt-5-codex"\n'
    'model_reasoning_effort = "medium"\n'
    'approval_policy = "on-request"\n'
    '\n'
    "[projects.'C:\\work']\n"
    'trust_level = "trusted"\n'
)


def _hub_catalog():
    return {"models": [
        {"slug": "all", "display_name": "All free models (Calvoun hub)",
         "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"},
                                        {"effort": "high"}, {"effort": "xhigh"}]},
        {"slug": "coding", "display_name": "Coding only (Calvoun hub)"},
        {"slug": "gpt-5-codex", "display_name": "gpt-5-codex"},
    ]}


@pytest.mark.skipif(tomllib is None, reason="needs tomllib")
def test_codex_disconnect_removes_catalog_picker_ids_and_restores_defaults(home):
    cfg = app._p_codex()
    _write(cfg, CODEX_USER)
    e = _connect("codex")
    # What codex's own /model picker writes while connected: the CATEGORY as
    # the model and the hub's tier as the effort, plus a catalog pointer.
    cat = app._codex_catalog_path()
    _wj(cat, _hub_catalog())
    text = _read(cfg).replace('model = "auto"', 'model = "coding"')
    text = text.replace('model_reasoning_effort = "medium"', 'model_reasoning_effort = "xhigh"')
    text = "model_catalog_json = '%s'\n" % cat + text
    _write(cfg, text)

    out = _disconnect_twice(e)

    assert out["catalog"] == "deleted"
    assert not os.path.exists(cat) and not os.path.exists(cat + ".freehub-bak")
    parsed = tomllib.loads(_read(cfg))
    assert parsed["model"] == "gpt-5-codex", "pre-hub model not restored"
    assert parsed["model_reasoning_effort"] == "medium", "hub tier left as the effort"
    assert "model_provider" not in parsed
    assert "model_catalog_json" not in parsed, "points at a catalog that no longer exists"
    assert parsed["approval_policy"] == "on-request"
    assert parsed["projects"]["C:\\work"]["trust_level"] == "trusted"
    _assert_no_trace(cfg)


def test_codex_connect_from_nothing_then_disconnect_leaves_nothing_of_ours(home):
    cfg = app._p_codex()
    e = _connect("codex")
    _write(cfg, _read(cfg) + '\n[mcp_servers.context7]\nurl = "https://mcp.context7.com/mcp"\n')
    _disconnect_twice(e)
    text = _read(cfg)
    assert "[mcp_servers.context7]" in text, "unrelated user table dropped"
    assert "model" not in text.split("[", 1)[0], "a hub model id survived at top level"
    _assert_no_trace(cfg)


def _fake_codex(monkeypatch, priority=1):
    template = {"slug": "gpt-5-codex", "display_name": "gpt-5-codex", "priority": priority,
                "visibility": "list", "supported_reasoning_levels": [],
                "default_reasoning_level": "medium", "context_window": 1,
                "max_context_window": 1}
    monkeypatch.setattr(app, "_which_cli", lambda name: "codex-bin")
    monkeypatch.setattr(app, "_codex_dump_models", lambda b: {"models": [dict(template)]})
    monkeypatch.setattr(app, "_codex_accepts_catalog", lambda b, p: True)


def test_codex_users_own_catalog_comes_back_on_disconnect(home, monkeypatch):
    e = _connect("codex")
    cat = app._codex_catalog_path()
    mine = {"models": [{"slug": "my-model", "display_name": "Mine"}]}
    _wj(cat, mine)
    _fake_codex(monkeypatch)
    app._refresh_codex_catalog()
    assert app._codex_catalog_is_hubs(cat), "connected: the hub catalog is installed"
    assert _rj(cat + ".freehub-bak") == mine
    # A second refresh with a different schema must NOT back up our own file
    # over the user's (that made the "pre-hub" backup a hub catalog).
    _fake_codex(monkeypatch, priority=5)
    app._refresh_codex_catalog()
    assert _rj(cat + ".freehub-bak") == mine

    out = app._disconnect_codex(e)
    assert out["catalog"] == "restored"
    assert _rj(cat) == mine
    assert not os.path.exists(cat + ".freehub-bak")


def test_startup_refresh_does_nothing_for_a_disconnected_codex(home, monkeypatch):
    cfg = app._p_codex()
    _write(cfg, 'model = "gpt-5"\n')
    cat = app._codex_catalog_path()
    mine = {"models": [{"slug": "my-model", "display_name": "Mine"}]}
    _wj(cat, mine)
    calls = []
    monkeypatch.setattr(app, "_which_cli", lambda name: calls.append(name) or "codex-bin")
    monkeypatch.setattr(app, "_codex_dump_models", lambda b: calls.append("dump") or None)
    app._refresh_codex_catalog()
    assert calls == [], "a disconnected codex must not even be probed"
    assert _rj(cat) == mine, "the user's own catalog was touched"
    assert not os.path.exists(cat + ".freehub-bak")
    assert _read(cfg) == 'model = "gpt-5"\n'


def test_startup_removes_a_catalog_an_older_build_left_behind(home, monkeypatch):
    """Before this fix the refresh wrote the catalog for EVERY codex install.
    A disconnected codex gets it removed at the next start -- ours only."""
    cfg = app._p_codex()
    cat = app._codex_catalog_path()
    _write(cfg, "model_catalog_json = '%s'\nmodel = \"gpt-5\"\n" % cat)
    _wj(cat, _hub_catalog())
    _wj(cat + ".freehub-bak", _hub_catalog())   # old code backed up its own file
    monkeypatch.setattr(app, "_which_cli", lambda name: pytest.fail("probed"))
    app._refresh_codex_catalog()
    assert not os.path.exists(cat)
    assert not os.path.exists(cat + ".freehub-bak")
    assert _read(cfg) == 'model = "gpt-5"\n'


def test_a_catalog_key_naming_another_file_is_the_users(home):
    cfg = app._p_codex()
    cat = app._codex_catalog_path()
    other = os.path.join(home, "elsewhere", "catalog.json")
    _write(cfg, "model_catalog_json = '%s'\n" % other)
    _wj(cat, _hub_catalog())
    app._codex_disconnect_catalog()
    assert not os.path.exists(cat)
    assert _read(cfg) == "model_catalog_json = '%s'\n" % other


def test_startup_refresh_still_runs_for_a_connected_codex(home, monkeypatch):
    _connect("codex")
    _fake_codex(monkeypatch)
    app._refresh_codex_catalog()
    assert app._codex_catalog_is_hubs(app._codex_catalog_path())


# --------------------------------------------------------------------------- #
# opencode
# --------------------------------------------------------------------------- #

def test_opencode_disconnect_restores_default_and_drops_every_hub_reference(home):
    p = app._p_opencode()
    user = {"$schema": "https://opencode.ai/config.json",
            "model": "anthropic/claude-sonnet-4", "theme": "tokyonight",
            "provider": {"ollama": {"npm": "@ai-sdk/openai-compatible",
                                    "options": {"baseURL": "http://127.0.0.1:11434/v1"}}}}
    _wj(p, user)
    e = _connect("opencode")
    data = _rj(p)
    data["small_model"] = "free-llm-hub/auto"
    data["agent"] = {"build": {"model": "free-llm-hub/coding-swarm", "temperature": 0.1}}
    data["mcp"] = {"free-llm-hub": {"type": "remote", "url": HUB_ROOT + "/mcp"},
                   "context7": {"type": "remote", "url": "https://mcp.context7.com/mcp"}}
    _wj(p, data)

    _disconnect_twice(e)

    after = _rj(p)
    assert after["model"] == "anthropic/claude-sonnet-4", "pre-hub default not restored"
    assert after["provider"] == user["provider"]
    assert after["theme"] == "tokyonight"
    assert "small_model" not in after
    assert after["agent"]["build"] == {"temperature": 0.1}
    assert "free-llm-hub" in after["mcp"], "the hub MCP entry is kept by design"
    _assert_no_trace(p)


def test_opencode_repair_never_recreates_a_disconnected_provider(home):
    p = app._p_opencode()
    user = {"$schema": "https://opencode.ai/config.json", "theme": "x"}
    _wj(p, user)
    app._repair_opencode_config()
    assert _rj(p) == user
    assert not os.path.exists(p + ".freehub-bak"), "backup litter for a disconnected user"
    e = _connect("opencode")
    app._disconnect_opencode(e)
    app._repair_opencode_config()
    assert "provider" not in _rj(p)


# --------------------------------------------------------------------------- #
# claude
# --------------------------------------------------------------------------- #

def test_claude_disconnect_restores_env_and_drops_a_picked_hub_model(home):
    p = app._p_claude()
    _wj(p, {"env": {"ANTHROPIC_BASE_URL": "https://proxy.example/api", "FOO": "1"},
            "permissions": {"allow": ["Bash"]}})
    local = os.path.join(os.path.dirname(p), "settings.local.json")
    _wj(local, {"env": {"ANTHROPIC_BASE_URL": HUB_ROOT, "ANTHROPIC_MODEL": "auto",
                        "BAR": "2"}})
    e = _connect("claude")
    data = _rj(p)
    data["model"] = "coding-swarm"            # /model picked a hub id
    _wj(p, data)

    _disconnect_twice(e)

    after = _rj(p)
    assert after["env"] == {"ANTHROPIC_BASE_URL": "https://proxy.example/api", "FOO": "1"}
    assert after["permissions"] == {"allow": ["Bash"]}
    assert "model" not in after
    assert _rj(local) == {"env": {"BAR": "2"}}
    _assert_no_trace(p)
    _assert_no_trace(local)


def test_claude_keeps_its_own_model_aliases(home):
    p = app._p_claude()
    e = _connect("claude")
    data = _rj(p)
    data["model"] = "opus"
    _wj(p, data)
    app._disconnect_claude(e)
    assert _rj(p).get("model") == "opus"


# --------------------------------------------------------------------------- #
# pi / aider / qwen
# --------------------------------------------------------------------------- #

def test_pi_disconnect_forgets_a_hub_default(home):
    p = app._p_pi_models()
    _wj(p, {"providers": {"ollama": {"baseUrl": "http://127.0.0.1:11434/v1"}}})
    e = _connect("pi")
    settings = os.path.join(os.path.dirname(p), "settings.json")
    _wj(settings, {"defaultProvider": "free-llm-hub", "defaultModel": "auto",
                   "enabledModels": ["free-llm-hub/auto", "ollama/llama3"], "theme": "dark"})
    _disconnect_twice(e)
    assert _rj(p) == {"providers": {"ollama": {"baseUrl": "http://127.0.0.1:11434/v1"}}}
    assert _rj(settings) == {"enabledModels": ["ollama/llama3"], "theme": "dark"}
    _assert_no_trace(p)
    _assert_no_trace(settings)


def test_aider_disconnect_restores_the_original_file(home):
    p = app._p_aider()
    _write(p, "dark-mode: true\nmodel: gpt-4o\n")
    e = _connect("aider")
    _disconnect_twice(e)
    assert _read(p) == "dark-mode: true\nmodel: gpt-4o\n"


def test_aider_file_created_by_connect_is_removed(home):
    p = app._p_aider()
    e = _connect("aider")
    _disconnect_twice(e)
    assert not os.path.exists(p)


def test_qwen_disconnect_restores_env_and_drops_a_picked_hub_model(home):
    p = app._p_qwen_env()
    _write(p, "FOO=1\n")
    e = _connect("qwen")
    settings = os.path.join(os.path.dirname(p), "settings.json")
    _wj(settings, {"model": {"name": "coding"}, "ui": {"theme": "x"}})
    _disconnect_twice(e)
    assert _read(p) == "FOO=1\n"
    assert _rj(settings) == {"ui": {"theme": "x"}}
    _assert_no_trace(settings)


# --------------------------------------------------------------------------- #
# openclaw / hermes / kimi
# --------------------------------------------------------------------------- #

def test_openclaw_disconnect_restores_primary_and_prunes_our_shells(home):
    p = app._p_openclaw()
    user = {"agents": {"defaults": {"model": {"primary": "anthropic/claude"},
                                    "workspace": "~/w"}},
            "channels": {"telegram": {"enabled": True}}}
    _wj(p, user)
    e = _connect("openclaw")
    _disconnect_twice(e)
    assert _rj(p) == user
    _assert_no_trace(p)


def test_hermes_disconnect_restores_the_users_model_block(home):
    pytest.importorskip("yaml")
    import yaml
    p = app._p_hermes()
    _write(p, "model:\n  provider: openrouter\n  default: anthropic/claude-sonnet-4\n"
              "toolsets:\n- web\n")
    e = _connect("hermes")
    _disconnect_twice(e)
    data = yaml.safe_load(_read(p))
    assert data["model"] == {"provider": "openrouter", "default": "anthropic/claude-sonnet-4"}
    assert data["toolsets"] == ["web"]
    _assert_no_trace(p)


def test_kimi_disconnect_restores_the_managed_default(home):
    p = app._p_kimi()
    user = ('default_model = "kimi-code/kimi-for-coding"\n\n'
            '[providers."managed:kimi-code"]\ntype = "kimi"\n')
    _write(p, user)
    e = _connect("kimi")
    _disconnect_twice(e)
    assert _read(p) == user


# --------------------------------------------------------------------------- #
# The routes the dashboard reads
# --------------------------------------------------------------------------- #

def test_routes_report_disconnected_from_the_real_files(home, monkeypatch):
    entries = [_entry("codex"), _entry("opencode")]
    monkeypatch.setattr(app, "CLI_REGISTRY", entries)
    monkeypatch.setattr(app, "_CLI_BY_ID", {e["id"]: e for e in entries})
    monkeypatch.setattr(app, "_cli_installed", lambda e: (True, "bin"))
    monkeypatch.setattr(app, "_mark_hub_mode_unmanaged", lambda: None)
    _connect("codex")
    _wj(app._codex_catalog_path(), _hub_catalog())
    _connect("opencode")
    p = app._p_opencode()
    data = _rj(p)
    data["mcp"] = {"free-llm-hub": {"type": "remote", "url": HUB_ROOT + "/mcp"}}
    _wj(p, data)

    app.app.config["TESTING"] = True
    with app.app.test_client() as c:
        headers = {"X-Free-LLM-Hub": "dashboard",
                   "X-Free-LLM-Hub-Token": app.config.ensure_control_token()}
        rows = {r["id"]: r for r in c.get("/api/clis", headers=headers).get_json()}
        assert rows["codex"]["connected"] and rows["opencode"]["connected"]

        r = c.post("/api/clis/codex/disconnect", headers=headers).get_json()
        assert r["ok"] and r["connected"] is False and r["catalog"] == "deleted"
        assert "picker" in r["note"]

        r = c.post("/api/clis/opencode/disconnect", headers=headers).get_json()
        assert r["ok"] and r["connected"] is False
        assert r["mcp_kept"] and "MCP" in r["note"]

        rows = {r["id"]: r for r in c.get("/api/clis", headers=headers).get_json()}
        assert rows["codex"]["connected"] is False
        assert rows["opencode"]["connected"] is False
    assert not os.path.exists(app._codex_catalog_path())


def test_hub_virtual_ids_are_recognised_and_real_ones_are_not():
    for mid in ("auto", "coding", "coding-swarm", "multi", "crew-code",
                "free-llm-hub/auto", "freehub/auto"):
        assert app._is_hub_virtual_model(mid), mid
    for mid in ("gpt-5-codex", "anthropic/claude-sonnet-4", "opus", "", None, 3):
        assert not app._is_hub_virtual_model(mid), mid
