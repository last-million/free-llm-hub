"""A disconnected CLI shows nothing of the hub -- every CLI (owner, 2026-10-20).

"When I disconnect a CLI from Free LLM Hub, nothing of the hub should appear
in it -- not only codex, every CLI." MEASURED on the owner's machine: claude,
codex, openclaw and kimi were disconnected yet still listed the hub's tools
(the "free-llm-hub" MCP server), and codex still offered "Auto - Free LLM Hub"
plus two "(..., via hub) - TEST ONLY" models from a catalog an older hub-era
setup wrote by hand without the "(Calvoun hub)" label, so Disconnect took it
for the user's own. Every test runs in a throwaway HOME.
"""
import os

import app
import mcp_manager
from test_cli_disconnect_leaves_no_trace import (  # noqa: F401  (fixture)
    HUB_ROOT, _connect, _entry, _rj, _wj, _write, home)

HUB_MCP = {"type": "remote", "url": HUB_ROOT + "/mcp"}
OTHER_MCP = {"type": "remote", "url": "https://mcp.context7.com/mcp"}


def _client(monkeypatch, cids):
    entries = [_entry(c) for c in cids]
    monkeypatch.setattr(app, "CLI_REGISTRY", entries)
    monkeypatch.setattr(app, "_CLI_BY_ID", {e["id"]: e for e in entries})
    monkeypatch.setattr(app, "_cli_installed", lambda e: (True, "bin"))
    monkeypatch.setattr(app, "_mark_hub_mode_unmanaged", lambda: None)
    monkeypatch.setattr(app, "_first_free_model_id", lambda: "groq/some-model")
    app.app.config["TESTING"] = True
    headers = {"X-Free-LLM-Hub": "dashboard",
               "X-Free-LLM-Hub-Token": app.config.ensure_control_token()}
    return app.app.test_client(), headers


def _opencode_with_mcp(servers):
    _connect("opencode")
    p = app._p_opencode()
    data = _rj(p)
    data["mcp"] = dict(servers)
    _wj(p, data)
    return p


def test_disconnect_removes_the_hubs_tools_and_keeps_the_users(home, monkeypatch):
    c, headers = _client(monkeypatch, ["opencode"])
    p = _opencode_with_mcp({"free-llm-hub": HUB_MCP, "context7": OTHER_MCP})
    r = c.post("/api/clis/opencode/disconnect", headers=headers).get_json()
    assert r["ok"] and r["connected"] is False
    assert r["mcp_removed"] == ["free-llm-hub"] and "mcp_kept" not in r
    mcp = _rj(p).get("mcp") or {}
    assert "free-llm-hub" not in mcp
    assert mcp.get("context7") is not None, "the user's own server is never touched"


def test_connect_puts_the_hubs_tools_back(home, monkeypatch):
    c, headers = _client(monkeypatch, ["opencode"])
    p = _opencode_with_mcp({"free-llm-hub": HUB_MCP})
    c.post("/api/clis/opencode/disconnect", headers=headers)
    assert "free-llm-hub" not in (_rj(p).get("mcp") or {})
    r = c.post("/api/clis/opencode/autofix", headers=headers).get_json()
    assert r.get("ok") and r.get("mcp_restored") is True
    assert (_rj(p).get("mcp") or {}).get("free-llm-hub")
    assert "opencode" not in (app.config.get_setting(app._HUB_MCP_REMOVED_SETTING) or [])


def test_connect_never_adds_tools_a_disconnect_did_not_remove(home, monkeypatch):
    c, headers = _client(monkeypatch, ["opencode"])
    p = _opencode_with_mcp({})
    r = c.post("/api/clis/opencode/autofix", headers=headers).get_json()
    assert r.get("ok") and "mcp_restored" not in r
    assert "free-llm-hub" not in (_rj(p).get("mcp") or {})


def test_the_switch_off_keeps_the_old_behaviour(home, monkeypatch):
    real_flag = app.config.get_flag
    monkeypatch.setattr(app.config, "get_flag",
                        lambda name, default=False: False if name == "disconnect_removes_hub_mcp"
                        else real_flag(name, default))
    c, headers = _client(monkeypatch, ["opencode"])
    p = _opencode_with_mcp({"free-llm-hub": HUB_MCP})
    r = c.post("/api/clis/opencode/disconnect", headers=headers).get_json()
    assert "mcp_removed" not in r and r["mcp_kept"]
    assert (_rj(p).get("mcp") or {}).get("free-llm-hub")


def test_only_this_hubs_endpoint_counts():
    port = app.PORT
    for url in ("http://127.0.0.1:%d/mcp" % port, "http://localhost:%d/mcp/" % port,
                "http://[::1]:%d/mcp" % port):
        assert app._is_hub_mcp_url(url), url
    for url in ("http://127.0.0.1:%d/mcp" % (port + 1), "https://mcp.context7.com/mcp",
                "http://127.0.0.1:%d/v1" % port, "", None, "not a url"):
        assert not app._is_hub_mcp_url(url), url


def test_an_older_hand_written_hub_catalog_is_removed_on_disconnect(home, monkeypatch):
    c, headers = _client(monkeypatch, ["codex"])
    _connect("codex")
    cat = app._codex_catalog_path()
    _wj(cat, {"models": [
        {"slug": "gpt-5.5", "display_name": "GPT-5.5", "description": "OpenAI model"},
        {"slug": "auto", "display_name": "Auto - Free LLM Hub",
         "description": "Routed automatically by the Calvoun Free LLM Hub."},
        {"slug": "nvidia/deepseek-ai/deepseek-v4-pro",
         "display_name": "DeepSeek V4 Pro (nvidia, via hub) - TEST ONLY",
         "description": "Test entry pinned to a free-hub model."}]})
    cfg = app._p_codex()
    with open(cfg, "a", encoding="utf-8") as f:
        f.write("\n")
    text = open(cfg, encoding="utf-8").read()
    _write(cfg, "model_catalog_json = '%s'\n" % cat + text)
    assert app._codex_catalog_is_hubs(cat)
    r = c.post("/api/clis/codex/disconnect", headers=headers).get_json()
    assert r["ok"] and r["connected"] is False and r["catalog"] == "deleted"
    assert not os.path.exists(cat)
    assert "model_catalog_json" not in open(cfg, encoding="utf-8").read()


def test_a_users_own_catalog_is_still_left_alone(home):
    cat = app._codex_catalog_path()
    _wj(cat, {"models": [{"slug": "gpt-5.5", "display_name": "GPT-5.5",
                          "description": "Balances speed and reasoning depth"},
                         {"slug": "my-local", "display_name": "My local model",
                          "description": "served on my own box, routed via ollama"}]})
    assert not app._codex_catalog_is_hubs(cat)
    assert app._codex_disconnect_catalog()["catalog"] is None
    assert os.path.exists(cat)


def test_one_boot_sweep_cleans_disconnected_clis_once(home, monkeypatch):
    _client(monkeypatch, ["opencode", "codex"])
    # opencode connected with the hub's tools: untouched
    p = _opencode_with_mcp({"free-llm-hub": HUB_MCP})
    # codex disconnected but still carrying the hub's tools
    ok, msg = mcp_manager.add_server("codex", "free-llm-hub", {"url": HUB_ROOT + "/mcp"})
    assert ok, msg
    assert not app._cli_row(_entry("codex")).get("connected")

    assert app._sweep_disconnected_cli_traces() == ["codex"]
    names = [e["name"] for e in mcp_manager.list_servers()["codex"]]
    assert "free-llm-hub" not in names
    assert (_rj(p).get("mcp") or {}).get("free-llm-hub"), "a connected CLI keeps them"

    # the owner's own later click is never undone: the sweep ran once
    mcp_manager.add_server("codex", "free-llm-hub", {"url": HUB_ROOT + "/mcp"})
    assert app._sweep_disconnected_cli_traces() == []
    assert "free-llm-hub" in [e["name"] for e in mcp_manager.list_servers()["codex"]]
