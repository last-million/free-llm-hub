"""Stopping the hub disconnects every CLI wired to it; the next start wires
back exactly those (owner, 2026-09-30: "when we click Stop the hub in
Settings it should really stop and auto disconnect from all CLIs")."""
from flask import jsonify

import app as A

_REAL_DISCONNECT_ALL = A._disconnect_all_clis        # before conftest stubs it
_REAL_RECONNECT = A._reconnect_clis_after_stop


def test_every_connected_cli_is_disconnected(monkeypatch):
    monkeypatch.setattr(A, "CLI_REGISTRY", [{"id": "a"}, {"id": "b"}, {"id": "c"}])
    monkeypatch.setattr(A, "_cli_connected",
                        lambda e: (e["id"] in ("a", "b"), "config", ""))
    calls = []

    def disconnect(cid):
        calls.append(cid)
        return jsonify({"ok": True, "still_connected": cid == "b", "note": "env var kept"})
    monkeypatch.setattr(A, "api_cli_disconnect", disconnect)
    with A.app.test_request_context():
        out = _REAL_DISCONNECT_ALL()
    assert calls == ["a", "b"]                         # "c" was not connected
    assert out["disconnected"] == ["a"]
    assert out["failed"][0]["id"] == "b"


def test_the_next_start_reconnects_exactly_those(monkeypatch):
    store = {A._STOP_DISCONNECTED_SETTING: ["codex", "opencode"]}
    monkeypatch.setattr(A.config, "get_setting", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(A.config, "set_setting", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(A, "api_cli_autofix",
                        lambda cid: jsonify({"ok": cid == "codex"}))
    assert _REAL_RECONNECT() == ["codex"]
    assert store[A._STOP_DISCONNECTED_SETTING] is None      # asked once, then forgotten
    assert _REAL_RECONNECT() == []


def test_the_stop_route_disconnects_before_it_shuts_down():
    src = open("app.py", encoding="utf-8").read()
    body = src[src.index("def api_runtime_stop("):]
    body = body[:body.index("\n@app.route")]
    assert body.index("_disconnect_all_clis()") < body.index("_graceful_shutdown_worker")
    assert '"clis_disconnected": cli_result["disconnected"]' in body
    boot = src[src.index("    _mark_runtime_started()\n"):][:1500]
    assert "_reconnect_clis_after_stop()" in boot


def test_the_dashboard_says_which_clis_were_disconnected():
    html = open("templates/index.html", encoding="utf-8").read()
    body = html[html.index("return api('/api/runtime/stop'"):][:1200]
    assert "r.clis_disconnected" in body and "they reconnect when it starts again" in body
