"""Tests for the hub's MCP endpoint (hub_mcp.py + /mcp and /api/mcp routes).

The hub exposes the crew pipeline as MCP tools (crew_run sync 5-20 min,
crew_start/crew_result async) so any MCP-capable agent CLI can call them
natively. The runner is injectable (hub_mcp.init) — tests use a fake.

NOTE: no pytest tmp_path here — this machine's basetemp is permission-denied;
tempfile.mkdtemp(prefix="hub-pytest-") works.
"""

import os
import shutil
import tempfile
import time

import pytest

import app
import hub_mcp
import mcp_manager


@pytest.fixture
def fake_runner(monkeypatch):
    monkeypatch.setattr(hub_mcp, "_RUNNER",
                        lambda messages, crew_name: "artefact for " + crew_name)
    yield
    # restore the real wiring (app.py set it at import)
    monkeypatch.undo()


@pytest.fixture
def mcp_home(monkeypatch):
    """mcp_manager writes REAL CLI configs — point it at a throwaway HOME."""
    root = tempfile.mkdtemp(prefix="hub-pytest-")
    monkeypatch.setenv("MCP_MANAGER_HOME", root)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Protocol level
# --------------------------------------------------------------------------- #

def test_initialize_and_ping():
    out, status = hub_mcp.handle_rpc({"jsonrpc": "2.0", "id": 1,
                                      "method": "initialize", "params": {}})
    assert status == 200
    assert out["result"]["serverInfo"]["name"] == "free-llm-hub"
    assert out["result"]["capabilities"] == {"tools": {}}
    out, _ = hub_mcp.handle_rpc({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    assert out["result"] == {}


def test_notifications_get_no_response():
    out, status = hub_mcp.handle_rpc(
        {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert out is None and status == 204


def test_bad_payloads_are_jsonrpc_errors_never_raises():
    for payload in (None, "x", [], {"id": 1}, {"jsonrpc": "2.0", "id": 1}):
        out, status = hub_mcp.handle_rpc(payload)
        assert out["error"]["code"] in (-32600, -32601)
    out, _ = hub_mcp.handle_rpc({"jsonrpc": "2.0", "id": 9, "method": "nope"})
    assert out["error"]["code"] == -32601


def test_tools_list_exposes_the_three_crew_tools():
    out, _ = hub_mcp.handle_rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    tools = {t["name"]: t for t in out["result"]["tools"]}
    assert set(tools) == {"crew_run", "crew_start", "crew_result"}
    # the sync tool must warn about its runtime — agents route around it
    assert "minute" in tools["crew_run"]["description"].lower()


def test_crew_run_sync(fake_runner):
    out, _ = hub_mcp.handle_rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                 "params": {"name": "crew_run",
                                            "arguments": {"task": "build a page",
                                                          "crew": "design"}}})
    content = out["result"]["content"]
    assert content[0]["type"] == "text" and "artefact for design" in content[0]["text"]


def test_crew_start_and_result_roundtrip(fake_runner):
    out, _ = hub_mcp.handle_rpc({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                 "params": {"name": "crew_start",
                                            "arguments": {"task": "research x"}}})
    import json as _json
    job = _json.loads(out["result"]["content"][0]["text"])["job_id"]
    for _ in range(50):
        out, _ = hub_mcp.handle_rpc({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                                     "params": {"name": "crew_result",
                                                "arguments": {"job_id": job}}})
        res = _json.loads(out["result"]["content"][0]["text"])
        if res["status"] != "running":
            break
        time.sleep(0.05)
    assert res["status"] == "done" and "artefact" in res["text"]


def test_arg_validation_and_unknown_job():
    out, _ = hub_mcp.handle_rpc({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                 "params": {"name": "crew_run", "arguments": {}}})
    assert out["error"]["code"] == -32602
    out, _ = hub_mcp.handle_rpc({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                                 "params": {"name": "crew_result",
                                            "arguments": {"job_id": "nope"}}})
    assert out["result"]["isError"] is True


# --------------------------------------------------------------------------- #
# HTTP routes
# --------------------------------------------------------------------------- #

def test_mcp_route_roundtrip(fake_runner):
    client = app.app.test_client()
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                     "method": "initialize", "params": {}})
    assert resp.status_code == 200
    assert resp.get_json()["result"]["serverInfo"]["name"] == "free-llm-hub"
    resp = client.post("/mcp", json={"jsonrpc": "2.0",
                                     "method": "notifications/initialized"})
    assert resp.status_code == 204


def test_api_mcp_routes(mcp_home):
    client = app.app.test_client()
    token = app.config.get_local_api_key() if hasattr(app.config, "get_local_api_key") else None
    # token gating: the control guard must reject token-less POSTs
    resp = client.post("/api/mcp", json={"cli": "kimi", "name": "x",
                                         "spec": {"url": "http://localhost:9/mcp"}})
    assert resp.status_code in (401, 403)
    headers = {}
    import config as _cfg
    tok = _cfg.get_local_api_key() or _cfg.get_control_token()
    if tok:
        headers["X-Free-LLM-Hub-Token"] = tok
    headers["X-Free-LLM-Hub"] = "dashboard"
    resp = client.get("/api/mcp", headers=headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["hub_mcp"]["url"].endswith("/mcp")
    for cli in mcp_manager.supported_clis():
        assert cli in body
    # add -> install-hub -> delete round-trip against the fake HOME
    resp = client.post("/api/mcp", headers=headers,
                       json={"cli": "kimi", "name": "t1",
                             "spec": {"url": "http://localhost:9/mcp"}})
    assert resp.get_json()["ok"] is True
    resp = client.post("/api/mcp/install-hub", headers=headers, json={"cli": "kimi"})
    assert resp.get_json()["ok"] is True
    resp = client.post("/api/mcp/install-hub", headers=headers, json={"cli": "kimi"})
    assert resp.get_json()["ok"] is True     # force-retry keeps it idempotent
    resp = client.post("/api/mcp/delete", headers=headers,
                       json={"cli": "kimi", "name": "free-llm-hub"})
    assert resp.get_json()["ok"] is True
    resp = client.post("/api/mcp/delete", headers=headers,
                       json={"cli": "kimi", "name": "t1"})
    assert resp.get_json()["ok"] is True
    resp = client.post("/api/mcp", headers=headers,
                       json={"cli": "not-a-cli", "name": "x", "spec": {"url": "u"}})
    assert resp.status_code == 400
