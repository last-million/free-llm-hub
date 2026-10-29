"""THE MCP GATEWAY (mcpgateway.py, app `_gw_*`, flag `mcp_gateway`), 2026-10-29.

Hermetic: a fake stdio MCP server written by this file (a tiny python script
speaking JSON-RPC over stdin/stdout, started through an injected spawn), a fake
HTTP transport, a temporary config. The module's default spawn / HTTP are
fenced by tests/conftest.py for the whole suite; two tests reach a LOCAL
http.server on 127.0.0.1 through the real HTTP function captured below.
"""
import http.server
import json
import logging
import os
import subprocess
import sys
import threading
import time

import pytest

import hub_mcp
import mcpgateway

# Captured at import, before the suite's fence replaces it for each test.
_REAL_HTTP_POST = mcpgateway._default_http_post

FAKE_SERVER = r'''
import json, os, sys, time
log = os.environ.get("FAKE_LOG")
def send(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
def note(o):
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(o) + "\n")
for line in sys.stdin:
    try:
        m = json.loads(line)
    except ValueError:
        continue
    note(m)
    meth, mid = m.get("method"), m.get("id")
    if meth is None or mid is None:
        continue
    if meth == "initialize":
        send({"jsonrpc": "2.0", "method": "notifications/message", "params": {"x": 1}})
        send({"jsonrpc": "2.0", "id": "srv-1", "method": "roots/list"})
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "1"}}})
    elif meth == "tools/list":
        cur = (m.get("params") or {}).get("cursor")
        if not cur:
            send({"jsonrpc": "2.0", "id": mid, "result": {"nextCursor": "p2", "tools": [
                {"name": "echo", "description": "Echo text.",
                 "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
                {"name": "big", "description": "Big output.", "inputSchema": {"type": "object"}}]}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [
                {"name": "fail", "inputSchema": {"type": "object"}},
                {"name": "slow", "inputSchema": {"type": "object"}},
                {"name": "die", "inputSchema": {"type": "object"}},
                {"name": "env_check", "inputSchema": {"type": "object"}},
                {"name": "refuse", "inputSchema": {"type": "object"}},
                {"name": "odd name/with.dots", "inputSchema": "not a schema"}]}})
    elif meth == "tools/call":
        p = m.get("params") or {}
        name, args = p.get("name"), p.get("arguments") or {}
        if name == "echo":
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [
                {"type": "text", "text": "echo: " + str(args.get("text"))}]}})
        elif name == "big":
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [
                {"type": "text", "text": "x" * 200000},
                {"type": "image", "data": "A" * 5000, "mimeType": "image/png"}]}})
        elif name == "fail":
            send({"jsonrpc": "2.0", "id": mid, "result": {"isError": True, "content": [
                {"type": "text", "text": "it failed"}]}})
        elif name == "slow":
            time.sleep(3)
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": "late"}]}})
        elif name == "die":
            sys.exit(3)
        elif name == "env_check":
            ok = os.environ.get("MY_SECRET") == "s3cret-value-123"
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [
                {"type": "text", "text": "env ok" if ok else "env missing"}]}})
        elif name == "odd name/with.dots":
            send({"jsonrpc": "2.0", "id": mid, "result": {"structuredContent": {"odd": True}}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "no such tool"}})
    else:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "nope"}})
'''


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A temporary hub config (secretstore writes its key beside it)."""
    path = tmp_path / "state" / "config.json"
    path.parent.mkdir()
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(path))
    import secretstore
    secretstore.reset_cache()
    yield path
    secretstore.reset_cache()


@pytest.fixture
def script(tmp_path):
    p = tmp_path / "fake_mcp_server.py"
    p.write_text(FAKE_SERVER, encoding="utf-8")
    return str(p)


class Spawner:
    def __init__(self):
        self.procs = []

    def __call__(self, argv, env, cwd):
        kw = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE,
              "stderr": subprocess.DEVNULL, "env": env, "cwd": cwd}
        if os.name == "nt":
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        p = subprocess.Popen(list(argv), **kw)
        self.procs.append(p)
        return p


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def gw(cfg, tmp_path):
    spawner = Spawner()
    clock = Clock()
    g = mcpgateway.Gateway(store=mcpgateway.ConfigStore(), spawn=spawner,
                           which=lambda c: c, http_post=None, clock=clock,
                           cache_path=str(tmp_path / "cache.json"), start_reaper=False,
                           cwd=str(tmp_path), max_procs=4)
    g.spawner = spawner
    g.clock = clock
    yield g
    g.shutdown()
    for p in spawner.procs:
        if p.poll() is None:
            p.kill()


def add_fake(gw, script, name="fake", env=None):
    return gw.register(name, command=sys.executable, args=[script], env=env)


def text_of(res):
    return "".join(c.get("text", "") for c in res["content"] if c.get("type") == "text")


# --------------------------------------------------------------------------- #
# Names and validation
# --------------------------------------------------------------------------- #

def test_exposed_names_follow_the_client_rules():
    assert mcpgateway.exposed_name("github", "create_issue") == "github__create_issue"
    odd = mcpgateway.exposed_name("fake", "odd name/with.dots")
    assert odd == "fake__odd_name_with_dots"
    long = mcpgateway.exposed_name("shopify-dev", "x" * 120)
    assert len(long) <= mcpgateway.MAX_TOOL_NAME
    assert long == mcpgateway.exposed_name("shopify-dev", "x" * 120)       # stable
    taken = {"a__b_c"}
    other = mcpgateway.exposed_name("a", "b.c", taken)
    assert other != "a__b_c" and len(other) <= mcpgateway.MAX_TOOL_NAME
    import re
    for n in (odd, long, other):
        assert re.match(r"^[A-Za-z0-9_-]{1,64}$", n)


@pytest.mark.parametrize("url,ok", [
    ("https://mcp.example.com/mcp", True),
    ("http://127.0.0.1:9000/mcp", True),
    ("http://localhost:9000/mcp", True),
    ("http://mcp.example.com/mcp", False),
    ("ftp://mcp.example.com/mcp", False),
    ("https://user:pw@mcp.example.com/mcp", False),
    ("https://mcp.example.com/mcp#x", False),
    ("", False),
])
def test_url_rules(url, ok):
    if ok:
        assert mcpgateway.validate_url(url) == url
    else:
        with pytest.raises(mcpgateway.GatewayError):
            mcpgateway.validate_url(url)


@pytest.mark.parametrize("kw,code", [
    ({"name": "Bad_Name", "command": "x"}, "bad_name"),
    ({"name": "ok", "command": "x", "url": "https://a.example/mcp"}, "bad_request"),
    ({"name": "ok"}, "bad_request"),
    ({"name": "ok", "command": "x", "env": {"1BAD": "v"}}, "bad_request"),
    ({"name": "ok", "command": "x", "args": ["a\nb"]}, "bad_request"),
    ({"name": "ok", "url": "https://a.example/mcp", "token": "two words"}, "bad_request"),
])
def test_bad_registrations_are_refused(gw, kw, code):
    with pytest.raises(mcpgateway.GatewayError) as exc:
        gw.register(kw.pop("name"), **kw)
    assert exc.value.code == code
    assert gw.store().servers() == {}


def test_args_that_look_like_secrets_are_masked():
    masked = mcpgateway.mask_args(["-y", "@stripe/mcp", "--api-key=sk_live_abcdef",
                                   "--access-token", "sbp_123456", "ghp_" + "a" * 36,
                                   "plain"])
    assert masked == ["-y", "@stripe/mcp", "--api-key=***", "--access-token", "***",
                      "***", "plain"]


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #

def test_secrets_are_encrypted_at_rest_and_never_shown(gw, script, cfg, caplog):
    import secretstore
    caplog.set_level(logging.DEBUG)
    view = add_fake(gw, script, env={"MY_SECRET": "s3cret-value-123"})
    hview = gw.register("remote", url="https://mcp.example.com/mcp",
                        token="Bearer tok-SECRET-9876")
    assert view["env_keys"] == ["MY_SECRET"] and "s3cret" not in json.dumps(view)
    assert hview["has_token"] is True and "tok-SECRET" not in json.dumps(hview)
    raw = cfg.read_text(encoding="utf-8")
    if secretstore.available():
        assert "s3cret-value-123" not in raw and "tok-SECRET-9876" not in raw
        assert "enc.v1:" in raw
    # The child gets the real value.
    gw.refresh("fake")
    res = gw.call_tool("fake__env_check", {})
    assert text_of(res) == "env ok"
    blob = json.dumps(gw.servers_view()) + caplog.text
    assert "s3cret-value-123" not in blob and "tok-SECRET-9876" not in blob


def test_an_update_keeps_secrets_left_blank(gw, script):
    add_fake(gw, script, env={"MY_SECRET": "s3cret-value-123", "OTHER": "x"})
    before = gw.store().servers()["fake"]["env"]["MY_SECRET"]
    gw.register("fake", command=sys.executable, args=[script], env={"MY_SECRET": ""})
    row = gw.store().servers()["fake"]
    assert row["env"] == {"MY_SECRET": before}            # kept, OTHER dropped
    gw.register("fake", command=sys.executable, args=[script])
    assert gw.store().servers()["fake"]["env"] == {"MY_SECRET": before}
    gw.register("remote", url="https://mcp.example.com/mcp", token="tok-1")
    tok = gw.store().servers()["remote"]["token"]
    gw.register("remote", url="https://mcp.example.com/mcp")
    assert gw.store().servers()["remote"]["token"] == tok
    gw.register("remote", url="https://mcp.example.com/mcp", clear_token=True)
    assert "token" not in gw.store().servers()["remote"]


# --------------------------------------------------------------------------- #
# stdio servers
# --------------------------------------------------------------------------- #

def test_tools_are_listed_from_the_cache_and_listing_never_starts_a_server(gw, script):
    add_fake(gw, script)
    assert gw.list_tools() == []                              # not read yet
    assert gw.spawner.procs == []
    view = gw.refresh("fake")
    assert view["state"] == "ok" and view["tool_count"] == 8      # both pages
    names = [t["name"] for t in gw.list_tools()]
    assert names[:2] == ["fake__echo", "fake__big"]
    assert "fake__odd_name_with_dots" in names
    odd = [t for t in gw.list_tools() if t["name"] == "fake__odd_name_with_dots"][0]
    assert odd["inputSchema"] == {"type": "object"}              # junk schema replaced
    assert gw.list_tools()[0]["description"].startswith("[fake, via the hub] ")
    n = len(gw.spawner.procs)
    for _ in range(3):
        gw.list_tools()
    assert len(gw.spawner.procs) == n


def test_a_call_is_proxied_and_answers_a_servers_own_requests(gw, script, tmp_path, monkeypatch):
    log = tmp_path / "server.log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    add_fake(gw, script)
    gw.refresh("fake")
    res = gw.call_tool("fake__echo", {"text": "hi"})
    assert res == {"content": [{"type": "text", "text": "echo: hi"}]}
    seen = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    replies = [m for m in seen if m.get("id") == "srv-1"]
    assert replies and replies[0]["error"]["code"] == -32601   # roots/list answered
    assert any(m.get("method") == "notifications/initialized" for m in seen)
    called = [m for m in seen if m.get("method") == "tools/call"]
    assert called[0]["params"]["name"] == "echo"


def test_unknown_names_are_not_the_gateways(gw, script):
    add_fake(gw, script)
    gw.refresh("fake")
    assert gw.call_tool("crew_run", {}) is None
    assert gw.call_tool("fake__nope", {}) is None
    assert gw.call_tool("other__echo", {}) is None


def test_output_is_capped(gw, script):
    add_fake(gw, script)
    gw.refresh("fake")
    res = gw.call_tool("fake__big", {})
    total = sum(len(c.get("text", "")) for c in res["content"])
    assert total <= mcpgateway.MAX_RESULT_CHARS + 200
    assert "output cut by the hub" in text_of(res)
    assert len(json.dumps(res)) < mcpgateway.MAX_RESULT_CHARS + 2000


def test_errors_come_back_as_tool_errors(gw, script):
    add_fake(gw, script)
    gw.refresh("fake")
    res = gw.call_tool("fake__fail", {})
    assert res["isError"] is True and text_of(res) == "it failed"
    res = gw.call_tool("fake__refuse", {})
    assert res["isError"] is True and "refused the call" in text_of(res)
    res = gw.call_tool("fake__echo", "not a dict")
    assert res["isError"] is True
    res = gw.call_tool("fake__odd_name_with_dots", {})
    assert json.loads(text_of(res)) == {"odd": True}         # structuredContent as text


def test_a_slow_call_is_time_boxed(gw, script, monkeypatch):
    monkeypatch.setattr(mcpgateway, "CALL_TIMEOUT", 0.5)
    add_fake(gw, script)
    gw.refresh("fake")
    t0 = time.monotonic()
    res = gw.call_tool("fake__slow", {})
    assert time.monotonic() - t0 < 2.5
    assert res["isError"] is True and "did not answer" in text_of(res)


def test_a_server_that_dies_is_restarted_once(gw, script):
    add_fake(gw, script)
    gw.refresh("fake")
    assert len(gw.spawner.procs) == 1
    res = gw.call_tool("fake__die", {})
    assert res["isError"] is True and "stopped" in text_of(res)
    assert len(gw.spawner.procs) == 2                         # one fresh start, no more
    assert text_of(gw.call_tool("fake__echo", {"text": "back"})) == "echo: back"


def test_a_missing_command_is_a_plain_error(gw, script):
    gw.register("ghost", command="no-such-command-xyz", args=[])
    gw._which = lambda c: None
    view = gw.refresh("ghost")
    assert view["state"] == "error" and "command not found" in view["error"]
    assert gw.spawner.procs == []


def test_never_more_than_max_procs_and_idle_servers_stop(gw, script):
    gw.max_procs = 2
    for name in ("a", "b", "c"):
        add_fake(gw, script, name)
    gw.refresh("a")
    gw.clock.t += 1
    gw.refresh("b")
    gw.clock.t += 1
    gw.refresh("c")                                           # makes room: a goes
    assert gw.running() == ["b", "c"]
    first = gw.spawner.procs[0]
    first.wait(timeout=10)
    assert first.poll() is not None                           # really stopped
    gw.clock.t += gw.idle_ttl + 1
    assert sorted(gw.reap()) == ["b", "c"]
    assert gw.running() == []
    for p in gw.spawner.procs:
        p.wait(timeout=10)


def test_shutdown_stops_every_process(gw, script):
    add_fake(gw, script)
    gw.refresh("fake")
    gw.shutdown()
    gw.spawner.procs[0].wait(timeout=10)
    assert gw.running() == []


def test_the_tool_cache_survives_a_restart_and_a_changed_server_drops_it(gw, script, tmp_path):
    add_fake(gw, script)
    gw.refresh("fake")
    gw.shutdown()
    again = mcpgateway.Gateway(store=mcpgateway.ConfigStore(), spawn=gw.spawner,
                               which=lambda c: c, cache_path=str(tmp_path / "cache.json"),
                               start_reaper=False)
    assert len(again.list_tools()) == 8
    assert len(gw.spawner.procs) == 1                         # nothing started
    again.register("fake", command=sys.executable, args=[script, "--changed"])
    assert again.list_tools() == []


def test_switched_off_lists_nothing_and_calls_nothing(gw, script):
    add_fake(gw, script)
    gw.refresh("fake")
    gw.configure(enabled=lambda: False)
    assert gw.list_tools() == []
    assert gw.call_tool("fake__echo", {"text": "x"}) is None


def test_remove_stops_the_server_and_drops_its_tools(gw, script):
    add_fake(gw, script)
    gw.refresh("fake")
    assert gw.remove("fake") is True
    assert gw.list_tools() == [] and gw.running() == []
    assert gw.remove("fake") is False


@pytest.mark.skipif(os.name != "nt", reason="cmd.exe quoting is Windows-only")
def test_cmd_metacharacters_are_refused_for_a_cmd_shim(gw):
    gw.register("shim", command="npx", args=["-y", "pkg&calc"])
    gw._which = lambda c: r"C:\fake\npx.cmd"
    view = gw.refresh("shim")
    assert view["state"] == "error" and "cmd.exe" in view["error"]
    assert gw.spawner.procs == []


# --------------------------------------------------------------------------- #
# HTTP servers (fake transport)
# --------------------------------------------------------------------------- #

class FakeHttp:
    def __init__(self, status=200):
        self.status = status
        self.calls = []

    def __call__(self, url, body, headers, timeout, want_id=None):
        msg = json.loads(body.decode("utf-8"))
        self.calls.append((url, dict(headers), msg))
        if self.status != 200:
            return self.status, {}, []
        if "id" not in msg:
            return 202, {}, []
        meth = msg["method"]
        if meth == "initialize":
            return 200, {"mcp-session-id": "sess-1"}, [{"jsonrpc": "2.0", "id": msg["id"], "result": {
                "protocolVersion": "2025-03-26", "capabilities": {}}}]
        if meth == "tools/list":
            return 200, {}, [{"jsonrpc": "2.0", "method": "notifications/message"},
                             {"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [
                                 {"name": "search_docs", "inputSchema": {"type": "object"}}]}}]
        if meth == "tools/call":
            return 200, {}, [{"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [
                {"type": "text", "text": "found " + msg["params"]["arguments"]["q"]}]}}]
        return 200, {}, [{"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601}}]


def test_an_http_server_with_a_bearer_token_and_a_session(gw):
    fake = FakeHttp()
    gw.configure(http_post=fake)
    gw.register("docs", url="https://mcp.example.com/mcp", token="tok-abc")
    view = gw.refresh("docs")
    assert view["state"] == "ok" and view["tool_count"] == 1
    res = gw.call_tool("docs__search_docs", {"q": "workers"})
    assert text_of(res) == "found workers"
    assert all(c[1]["Authorization"] == "Bearer tok-abc" for c in fake.calls)
    after_init = [c for c in fake.calls if c[2].get("method") != "initialize"]
    assert after_init and all(c[1].get("Mcp-Session-Id") == "sess-1" for c in after_init)
    assert gw.spawner.procs == []


def test_an_http_server_that_refuses_the_token_is_a_plain_error(gw):
    gw.configure(http_post=FakeHttp(status=401))
    gw.register("docs", url="https://mcp.example.com/mcp", token="bad")
    view = gw.refresh("docs")
    assert view["state"] == "error" and "refused the token" in view["error"]


def test_sse_answers_are_parsed():
    events = list(mcpgateway._parse_sse([b"event: message\n", b'data: {"a": 1}\n', b"\n",
                                         b"data: {\"b\":\n", b"data: 2}\n", b"\n"]))
    assert [json.loads(e) for e in events] == [{"a": 1}, {"b": 2}]


class _Local(http.server.ThreadingHTTPServer):
    daemon_threads = True


def _serve(handler):
    srv = _Local(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_a_redirect_is_never_followed_with_the_token():
    hits = []

    class Target(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    target = _serve(Target)

    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(307)
            self.send_header("Location", "http://127.0.0.1:%d/steal" % target.server_port)
            self.end_headers()

        def log_message(self, *a):
            pass

    src = _serve(Redirect)
    try:
        conn = mcpgateway._HttpConn("http://127.0.0.1:%d/mcp" % src.server_port,
                                    "tok-NEVER-LEAK", _REAL_HTTP_POST)
        with pytest.raises(mcpgateway.GatewayError) as exc:
            conn.request("initialize", {}, 5)
        assert "307" in exc.value.message
        assert hits == []
    finally:
        src.shutdown()
        target.shutdown()


def test_a_real_local_streamable_http_answer_in_sse():
    class Sse(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Mcp-Session-Id", "s-9")
            self.end_headers()
            reply = {"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-03-26"}}
            self.wfile.write(b'event: message\ndata: {"jsonrpc":"2.0","method":"x"}\n\n')
            self.wfile.write(("data: " + json.dumps(reply) + "\n\n").encode())
            self.wfile.flush()

        def log_message(self, *a):
            pass

    srv = _serve(Sse)
    try:
        conn = mcpgateway._HttpConn("http://127.0.0.1:%d/mcp" % srv.server_port, None,
                                    _REAL_HTTP_POST)
        msg = conn.request("initialize", {}, 5)
        assert msg["result"]["protocolVersion"] == "2025-03-26"
        assert conn.session == "s-9"
    finally:
        srv.shutdown()


# --------------------------------------------------------------------------- #
# The catalog
# --------------------------------------------------------------------------- #

def test_the_catalog_is_verified_official_and_runnable_by_the_gateway():
    ids = [e["id"] for e in mcpgateway.CATALOG]
    assert len(ids) == len(set(ids))
    for e in mcpgateway.CATALOG:
        assert mcpgateway.NAME_RE.match(e["id"])
        assert e["source"].startswith("https://")
        assert e["auth"] in ("none", "token", "token_optional", "env")
        if e["transport"] == "http":
            assert mcpgateway.validate_url(e["url"]) == e["url"]
        else:
            assert e["transport"] == "stdio" and e["command"] and isinstance(e["args"], list)
    # OAuth-only servers cannot be reached with a bearer token: left out.
    blob = json.dumps(mcpgateway.CATALOG)
    assert "mcp.vercel.com" not in blob and "mcp.sentry.dev" not in blob
    for want in ("shopify-dev", "github", "supabase", "stripe", "cloudflare"):
        assert want in ids


# --------------------------------------------------------------------------- #
# Through the hub's own MCP endpoint
# --------------------------------------------------------------------------- #

def test_hub_mcp_lists_and_calls_gateway_tools_and_its_own_tools_win(gw, script, monkeypatch):
    monkeypatch.setattr(hub_mcp, "_EXTRA_PROVIDERS", [])
    clash = {"name": "crew_run", "description": "impostor", "inputSchema": {"type": "object"}}
    hub_mcp.add_tool_provider(lambda: gw.list_tools() + [clash],
                              lambda n, a: gw.call_tool(n, a))
    add_fake(gw, script)
    gw.refresh("fake")
    resp, _st = hub_mcp.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = [t["name"] for t in resp["result"]["tools"]]
    assert "fake__echo" in names and names.count("crew_run") == 1
    assert [t for t in resp["result"]["tools"] if t["name"] == "crew_run"][0]["description"] \
        != "impostor"
    resp, _st = hub_mcp.handle_rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                    "params": {"name": "fake__echo",
                                               "arguments": {"text": "via hub"}}})
    assert resp["result"]["content"][0]["text"] == "echo: via hub"
    resp, _st = hub_mcp.handle_rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                    "params": {"name": "nobody__x", "arguments": {}}})
    assert resp["error"]["code"] == -32602


def test_a_provider_that_raises_never_breaks_the_endpoint(monkeypatch):
    def boom(*_a):
        raise RuntimeError("x")
    monkeypatch.setattr(hub_mcp, "_EXTRA_PROVIDERS", [])
    hub_mcp.add_tool_provider(boom, boom)
    resp, _st = hub_mcp.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert "crew_run" in [t["name"] for t in resp["result"]["tools"]]
    resp, _st = hub_mcp.handle_rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                    "params": {"name": "x__y", "arguments": {}}})
    assert resp["result"]["isError"] is True


# --------------------------------------------------------------------------- #
# The routes
# --------------------------------------------------------------------------- #

@pytest.fixture
def client(gw, monkeypatch):
    import app
    import config
    gw.configure(enabled=lambda: app._gw_on(), http_post=FakeHttp())
    monkeypatch.setattr(mcpgateway, "default", gw)
    monkeypatch.setattr(app, "_gw_on", lambda: True)
    monkeypatch.setattr(app, "_ts_on", lambda: True)
    import toolscan
    monkeypatch.setattr(toolscan, "snapshot", lambda wait=0.0: {
        "at": 1.0, "native": {"gh": {"version": "2.62.0", "login": True}}, "wsl": {}})
    token = config.ensure_control_token()
    c = app.app.test_client()
    c.hdrs = {"X-Free-LLM-Hub": "dashboard", "X-Free-LLM-Hub-Token": token}
    return c


def test_route_get_lists_servers_catalog_and_scan_without_secrets(client, gw, script):
    add_fake(gw, script, env={"MY_SECRET": "s3cret-value-123"})
    gw.register("docs", url="https://mcp.example.com/mcp?key=zzz", token="tok-SECRET")
    r = client.get("/api/mcp-gateway", headers=client.hdrs)
    assert r.status_code == 200
    j = r.get_json()
    blob = json.dumps(j)
    assert "s3cret-value-123" not in blob and "tok-SECRET" not in blob and "zzz" not in blob
    assert [s["name"] for s in j["servers"]] == ["docs", "fake"]
    assert j["catalog"] and j["tool_scan"]["tools"][0]["name"] == "gh"
    assert j["tool_scan"]["block"].startswith("CLI TOOLS HERE:")


def test_route_add_needs_confirm_and_the_flag(client, monkeypatch):
    import app
    body = {"name": "docs", "url": "https://mcp.example.com/mcp", "token": "tok-1"}
    r = client.post("/api/mcp-gateway", json=body, headers=client.hdrs)
    assert r.status_code == 400 and r.get_json()["code"] == "confirm_required"
    monkeypatch.setattr(app, "_gw_on", lambda: False)
    r = client.post("/api/mcp-gateway", json=dict(body, confirm=True), headers=client.hdrs)
    assert r.status_code == 403 and r.get_json()["code"] == "disabled"
    monkeypatch.setattr(app, "_gw_on", lambda: True)
    r = client.post("/api/mcp-gateway", json=dict(body, confirm=True), headers=client.hdrs)
    assert r.status_code == 200 and r.get_json()["server"]["has_token"] is True
    assert "tok-1" not in r.get_data(as_text=True)
    r = client.post("/api/mcp-gateway", json={"name": "BAD", "url": "x", "confirm": True},
                    headers=client.hdrs)
    assert r.status_code == 400


def test_route_delete(client, gw, script):
    add_fake(gw, script)
    r = client.post("/api/mcp-gateway/delete", json={"name": "fake"}, headers=client.hdrs)
    assert r.status_code == 200
    r = client.post("/api/mcp-gateway/delete", json={"name": "fake"}, headers=client.hdrs)
    assert r.status_code == 404


def test_routes_are_token_and_header_gated(client):
    r = client.get("/api/mcp-gateway")
    assert r.status_code == 401
    r = client.post("/api/mcp-gateway", json={"confirm": True},
                    headers={"X-Free-LLM-Hub-Token": client.hdrs["X-Free-LLM-Hub-Token"]})
    assert r.status_code == 403


def test_the_dashboard_block_uses_text_content_and_theme_tokens():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "templates", "index.html"), encoding="utf-8") as f:
        html = f.read()
    js = html[html.index("/* mcp-gateway-js:start"):html.index("/* mcp-gateway-js:end")]
    assert "innerHTML" not in js
    css = html[html.index("/* mcp-gateway-css:start"):html.index("/* mcp-gateway-css:end")]
    import re
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", css)
    assert 'id="mcp-gateway"' in html and "loadMcpGateway();" in html
