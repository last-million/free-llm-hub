"""The Build page shows a preview even when the app forbids being framed.

MEASURED live (2026-10-11): the owner's Express app uses helmet, which sends
`X-Frame-Options: SAMEORIGIN` and a CSP with `frame-ancestors 'self'`. The
preview runs on 127.0.0.1:5800 and the dashboard on 127.0.0.1:8787 -- another
port is another origin -- so Chrome refused the frame ("127.0.0.1 refused to
connect.") while the app worked in its own tab.

The fix is generic: a local relay (frameproxy.py) in front of the preview port
that removes X-Frame-Options and ONLY the frame-ancestors directive, used only
when a probe of GET / finds headers that would block the frame. Hermetic: a
tiny local HTTP server sends helmet-like headers; real sockets on ephemeral
ports; every server and relay is torn down.
"""
import http.client
import os
import re
import shutil
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import frameproxy
import workspace

HUB = 8787

HELMET_CSP = ("default-src 'self';base-uri 'self';font-src 'self' https: data:;"
              "form-action 'self';frame-ancestors 'self';img-src 'self' data:;"
              "object-src 'none';script-src 'self';script-src-attr 'none';"
              "style-src 'self' https: 'unsafe-inline';upgrade-insecure-requests")
HELMET_CSP_KEPT = ("default-src 'self';base-uri 'self';font-src 'self' https: data:;"
                   "form-action 'self';img-src 'self' data:;"
                   "object-src 'none';script-src 'self';script-src-attr 'none';"
                   "style-src 'self' https: 'unsafe-inline';upgrade-insecure-requests")
HUB_FA = ("frame-ancestors 'self' http://127.0.0.1:%d http://localhost:%d" % (HUB, HUB))


# --------------------------------------------------------------------------- #
# A tiny app: helmet-like headers, chunked, until-close, redirects, an
# Upgrade echo. ThreadingHTTPServer on 127.0.0.1:0.
# --------------------------------------------------------------------------- #

class _App(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):
        pass

    def _framing(self):
        if self.server.helmet:
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header("Content-Security-Policy", HELMET_CSP)
            self.send_header("Content-Security-Policy-Report-Only",
                             "frame-ancestors 'none'; report-uri /csp")

    def _send(self, code, body, extra=()):
        self.send_response(code)
        self._framing()
        for k, v in extra:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?")[0]
        port = self.server.server_port
        if path == "/ws" and self.headers.get("Upgrade", "").lower() == "websocket":
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.end_headers()
            conn = self.connection
            conn.settimeout(10)
            try:
                while True:
                    data = conn.recv(65536)
                    if not data:
                        break
                    conn.sendall(b"echo:" + data)
            except OSError:
                pass
            self.close_connection = True
        elif path == "/chunked":
            self.send_response(200)
            self._framing()
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for part in (b"alpha-", b"beta-", b"gamma"):
                self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
            self.wfile.write(b"0\r\n\r\n")
        elif path == "/close":
            self.send_response(200)
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"until-close")
            self.close_connection = True
        elif path == "/redirect":
            self._send(302, b"", [("Location", "http://127.0.0.1:%d/login" % port)])
        elif path == "/redirect-localhost":
            self._send(302, b"", [("Location", "http://localhost:%d/x?y=1" % port)])
        elif path == "/redirect-relative":
            self._send(302, b"", [("Location", "/login")])
        elif path == "/redirect-away":
            self._send(302, b"", [("Location", "https://example.com:%d/" % port)])
        elif path == "/host":
            self._send(200, self.headers.get("Host", "").encode())
        elif path == "/big":
            self._send(200, b"0123456789abcdef" * 65536 + b"tail")
        elif path == "/missing":
            self._send(404, b"nope", [("X-Custom", "kept")])
        else:
            self._send(200, b"<h1>hello</h1>",
                       [("X-Custom", "kept"), ("Set-Cookie", "a=1; Path=/")])

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self._send(201, b"got:" + self.rfile.read(n))


def _serve(helmet=True):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _App)
    srv.daemon_threads = True
    srv.helmet = helmet
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()
    return srv


def _close_server(srv):
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def app_srv():
    srv = _serve(helmet=True)
    try:
        yield srv
    finally:
        _close_server(srv)


@pytest.fixture
def plain_srv():
    srv = _serve(helmet=False)
    try:
        yield srv
    finally:
        _close_server(srv)


@pytest.fixture
def proxy(app_srv):
    px = frameproxy.FrameProxy(app_srv.server_port, hub_port=HUB).start()
    try:
        yield px
    finally:
        px.stop()


def _get(port, path, method="GET", body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.getheaders(), resp.read()
    finally:
        conn.close()


def _hdr(headers, name):
    return [v for k, v in headers if k.lower() == name.lower()]


def _raw(port, data, timeout=10):
    """Send raw bytes, read until the server closes."""
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.sendall(data)
        out = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                return out
            out += chunk
    finally:
        s.close()


def _refused(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return False
    except OSError:
        return True


# --------------------------------------------------------------------------- #
# Pure header logic
# --------------------------------------------------------------------------- #

def test_only_frame_ancestors_leaves_the_policy_every_other_directive_kept():
    assert frameproxy.strip_frame_ancestors(HELMET_CSP) == HELMET_CSP_KEPT
    assert frameproxy.strip_frame_ancestors("frame-ancestors 'none'") is None
    assert frameproxy.strip_frame_ancestors("default-src 'self'") == "default-src 'self'"
    assert (frameproxy.strip_frame_ancestors("frame-ancestors 'none'; report-uri /csp")
            == "report-uri /csp")
    assert (frameproxy.strip_frame_ancestors(
        "default-src 'self'; frame-ancestors 'self', img-src *")
        == "default-src 'self', img-src *")


@pytest.mark.parametrize("headers, blocks", [
    ([("X-Frame-Options", "SAMEORIGIN")], True),
    ([("x-frame-options", "deny")], True),
    ([("Content-Security-Policy", "frame-ancestors 'self'")], True),
    ([("Content-Security-Policy", "frame-ancestors 'none'")], True),
    ([("Content-Security-Policy", HELMET_CSP)], True),
    ([("Content-Security-Policy", HUB_FA)], False),
    ([("Content-Security-Policy", "frame-ancestors *")], False),
    ([("Content-Security-Policy", "frame-ancestors http:")], False),
    ([("Content-Security-Policy", "frame-ancestors https:")], True),
    ([("Content-Security-Policy",
       "frame-ancestors http://127.0.0.1:* http://localhost:*")], False),
    # frame-ancestors present: browsers ignore X-Frame-Options.
    ([("Content-Security-Policy", HUB_FA), ("X-Frame-Options", "DENY")], False),
    # Report-Only never blocks anything.
    ([("Content-Security-Policy-Report-Only", "frame-ancestors 'none'")], False),
    ([("Content-Security-Policy", "default-src 'self'")], False),
    ([("Content-Type", "text/html")], False),
    ([], False),
])
def test_the_probe_reads_framing_headers_like_a_browser(headers, blocks):
    assert bool(frameproxy.framing_block(headers, HUB)) is blocks


# --------------------------------------------------------------------------- #
# The relay itself
# --------------------------------------------------------------------------- #

def test_framing_headers_are_stripped_exactly_and_nothing_else(app_srv, proxy):
    status, headers, body = _get(proxy.port, "/")
    assert status == 200 and body == b"<h1>hello</h1>"
    assert _hdr(headers, "X-Frame-Options") == []
    csp = _hdr(headers, "Content-Security-Policy")
    assert HELMET_CSP_KEPT in csp, csp                  # every other directive kept
    assert "upgrade-insecure-requests" in HELMET_CSP_KEPT
    # The one added policy: only the hub (and the app itself) may frame it.
    assert HUB_FA in csp and len(csp) == 2
    assert _hdr(headers, "Content-Security-Policy-Report-Only") == ["report-uri /csp"]
    assert _hdr(headers, "X-Custom") == ["kept"]
    assert _hdr(headers, "Set-Cookie") == ["a=1; Path=/"]
    assert _hdr(headers, "Content-Length") == [str(len(body))]
    # What the browser sees through the relay no longer blocks the hub's frame.
    assert frameproxy.framing_block(headers, HUB) is None
    # The app itself still blocks: the relay changes nothing upstream.
    assert frameproxy.framing_block(frameproxy.fetch_headers(app_srv.server_port), HUB)


def test_status_and_body_pass_through_for_errors_too(proxy):
    status, headers, body = _get(proxy.port, "/missing")
    assert (status, body) == (404, b"nope")
    assert _hdr(headers, "X-Custom") == ["kept"]


def test_chunked_bodies_stay_chunked_and_intact(proxy):
    raw = _raw(proxy.port, b"GET /chunked HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    head, _, body = raw.partition(b"\r\n\r\n")
    assert b"Transfer-Encoding: chunked" in head
    assert b"X-Frame-Options" not in head
    assert body == b"6\r\nalpha-\r\n5\r\nbeta-\r\n5\r\ngamma\r\n0\r\n\r\n"
    assert _get(proxy.port, "/chunked")[2] == b"alpha-beta-gamma"


def test_a_big_body_is_streamed_whole(proxy):
    status, _h, body = _get(proxy.port, "/big")
    assert status == 200
    assert len(body) == 16 * 65536 + 4 and body.endswith(b"tail")
    assert body[:32] == b"0123456789abcdef" * 2


def test_a_body_that_ends_with_the_connection_is_relayed(proxy):
    raw = _raw(proxy.port, b"GET /close HTTP/1.1\r\nHost: x\r\n\r\n")
    assert raw.startswith(b"HTTP/1.1 200")
    assert raw.endswith(b"\r\n\r\nuntil-close")


def test_keep_alive_serves_several_requests_on_one_connection(proxy):
    conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=10)
    try:
        conn.request("GET", "/")
        r1 = conn.getresponse()
        assert r1.read() == b"<h1>hello</h1>"
        first = conn.sock
        conn.request("POST", "/echo", body=b"payload")
        r2 = conn.getresponse()
        assert (r2.status, r2.read()) == (201, b"got:payload")
        conn.request("HEAD", "/")
        r3 = conn.getresponse()
        assert r3.status == 200 and r3.read() == b""
        conn.request("GET", "/chunked")
        assert conn.getresponse().read() == b"alpha-beta-gamma"
        assert conn.sock is first                    # never reconnected
    finally:
        conn.close()


def test_the_app_answers_with_the_host_the_browser_used(proxy):
    """Host is NOT rewritten: the app answers it (measured here), and the
    absolute links it builds from it point back at the relay."""
    status, _h, body = _get(proxy.port, "/host")
    assert status == 200 and body == b"127.0.0.1:%d" % proxy.port


def test_locations_pointing_at_the_real_port_are_pointed_at_the_relay(proxy):
    _s, h, _b = _get(proxy.port, "/redirect")
    assert _hdr(h, "Location") == ["http://127.0.0.1:%d/login" % proxy.port]
    _s, h, _b = _get(proxy.port, "/redirect-localhost")
    assert _hdr(h, "Location") == ["http://127.0.0.1:%d/x?y=1" % proxy.port]
    _s, h, _b = _get(proxy.port, "/redirect-relative")
    assert _hdr(h, "Location") == ["/login"]
    status, h, _b = _get(proxy.port, "/redirect-away")
    assert status == 302 and _hdr(h, "Location")[0].startswith("https://example.com:")


def test_an_upgrade_is_tunnelled_raw_both_ways(proxy):
    s = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
    try:
        s.sendall(b"GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
                  b"Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n\r\n")
        head = b""
        while b"\r\n\r\n" not in head:
            head += s.recv(4096)
        assert head.startswith(b"HTTP/1.1 101")
        assert b"Upgrade: websocket" in head
        assert b"Content-Security-Policy" not in head   # a 101 is not a document
        rest = head.partition(b"\r\n\r\n")[2]
        assert rest == b""
        for msg in (b"ping", b"\x81\x05hello", b"x" * 70000):
            s.sendall(msg)
            got = b""
            deadline = time.time() + 10
            # The echo server may split a big message over several echoes.
            while (len(got.replace(b"echo:", b"")) < len(msg)
                   and time.time() < deadline):
                got += s.recv(65536)
            assert got.startswith(b"echo:")
            assert got.replace(b"echo:", b"") == msg
    finally:
        s.close()


def test_a_down_app_gets_a_502_not_a_hang(app_srv):
    port = app_srv.server_port
    _close_server(app_srv)
    px = frameproxy.FrameProxy(port, hub_port=HUB).start()
    try:
        status, _h, body = _get(px.port, "/")
        assert status == 502 and b"not answering" in body
    finally:
        px.stop()


def test_the_relay_binds_loopback_only_and_never_an_avoided_port(app_srv):
    seen = []

    def avoid(port):
        seen.append(port)
        return len(seen) == 1                  # refuse the first port it gets

    px = frameproxy.FrameProxy(app_srv.server_port, hub_port=HUB, avoid=avoid).start()
    try:
        assert px._sock.getsockname()[0] == "127.0.0.1"
        assert px.host == "127.0.0.1" and px.url == "http://127.0.0.1:%d" % px.port
        assert len(seen) >= 2 and px.port == seen[-1] and px.port != seen[0]
        assert px.port not in (HUB, app_srv.server_port)
    finally:
        px.stop()


def test_stop_closes_the_listener_and_open_tunnels(proxy):
    s = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
    s.sendall(b"GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
              b"Connection: Upgrade\r\n\r\n")
    head = b""
    while b"\r\n\r\n" not in head:
        head += s.recv(4096)
    port = proxy.port
    proxy.stop()
    try:
        assert s.recv(10) == b""               # the tunnel was closed
    except OSError:
        pass
    finally:
        s.close()
    assert _refused(port)
    proxy.stop()                               # idempotent


# --------------------------------------------------------------------------- #
# Workspace: used only when needed, lives exactly as long as the preview
# --------------------------------------------------------------------------- #

@pytest.fixture
def proj(monkeypatch):
    monkeypatch.setattr(workspace, "_frame_proxy_enabled", lambda: True)
    monkeypatch.setattr(workspace, "_hub_port", lambda: HUB)
    d = tempfile.mkdtemp(prefix="hubframe-")
    try:
        yield d
    finally:
        workspace.stop(d)
        workspace._frame_stop(d)
        shutil.rmtree(d, ignore_errors=True)


def _fake_running(d, port):
    """A preview of ours, running on `port`, with no process to kill."""
    p = workspace._Proc(os.path.abspath(d), port, "static")
    p.state = "running"
    with workspace._lock:
        workspace._procs[os.path.abspath(d)] = p
    return p


def test_the_relay_is_used_only_when_the_probe_finds_framing_headers(proj, app_srv):
    _fake_running(proj, app_srv.server_port)
    st = workspace.status(proj)
    f = workspace.frame_fields(proj, st)
    assert f["frame_blocked"] is False
    assert f["frame_url"] != st["url"]
    assert re.fullmatch(r"http://127\.0\.0\.1:\d+", f["frame_url"])
    assert st["url"] == "http://127.0.0.1:%d" % app_srv.server_port   # real url kept
    port = int(f["frame_url"].rsplit(":", 1)[1])
    _s, headers, body = _get(port, "/")
    assert body == b"<h1>hello</h1>" and _hdr(headers, "X-Frame-Options") == []
    # Asked again: the same relay, no second probe or second proxy.
    assert workspace.frame_fields(proj) == f
    assert "[hub] this app refuses to be shown inside another page" in "\n".join(
        workspace._procs[os.path.abspath(proj)].all_lines())


def test_an_app_that_allows_framing_is_framed_directly(proj, plain_srv):
    _fake_running(proj, plain_srv.server_port)
    f = workspace.frame_fields(proj)
    assert f == {"frame_url": "http://127.0.0.1:%d" % plain_srv.server_port,
                 "frame_blocked": False}
    assert workspace._frames[os.path.abspath(proj)].proxy is None


def test_flag_off_means_the_old_status_and_no_relay(proj, app_srv, monkeypatch):
    _fake_running(proj, app_srv.server_port)
    f = workspace.frame_fields(proj)
    port = int(f["frame_url"].rsplit(":", 1)[1])
    monkeypatch.setattr(workspace, "_frame_proxy_enabled", lambda: False)
    assert workspace.frame_fields(proj) == {}
    assert _refused(port)                      # switching it off closed the relay
    assert os.path.abspath(proj) not in workspace._frames


def test_the_flag_reads_config_and_defaults_on(monkeypatch):
    import config
    seen = []
    monkeypatch.setattr(config, "get_flag",
                        lambda name, default=False: seen.append((name, default)) or default)
    assert workspace._frame_proxy_enabled() is True
    assert seen == [("preview_frame_proxy", True)]


def test_the_relay_stops_with_the_preview(proj, app_srv):
    _fake_running(proj, app_srv.server_port)
    port = int(workspace.frame_fields(proj)["frame_url"].rsplit(":", 1)[1])
    assert not _refused(port)
    assert workspace.stop(proj) is True
    assert _refused(port)
    assert os.path.abspath(proj) not in workspace._frames
    assert workspace.frame_fields(proj) == {}       # nothing running: no frame


def test_the_relay_stops_when_the_preview_dies_or_moves(proj, app_srv, plain_srv):
    p = _fake_running(proj, app_srv.server_port)
    port = int(workspace.frame_fields(proj)["frame_url"].rsplit(":", 1)[1])
    # The preview followed its app to another port: the old relay goes.
    p.port = plain_srv.server_port
    f = workspace.frame_fields(proj)
    assert _refused(port)
    assert f["frame_url"] == "http://127.0.0.1:%d" % plain_srv.server_port
    # Back on the helmet app, then the app stops on its own.
    p.port = app_srv.server_port
    port = int(workspace.frame_fields(proj)["frame_url"].rsplit(":", 1)[1])
    p.state = "failed"
    assert workspace.frame_fields(proj) == {}
    assert _refused(port)


def test_the_reaper_closes_a_relay_nobody_owns(proj, app_srv):
    _fake_running(proj, app_srv.server_port)
    port = int(workspace.frame_fields(proj)["frame_url"].rsplit(":", 1)[1])
    with workspace._lock:
        workspace._procs.pop(os.path.abspath(proj))
    workspace.reap_idle()
    assert _refused(port)


def test_an_adopted_server_gets_the_relay_and_loses_it_on_forget(proj, app_srv):
    d = os.path.abspath(proj)
    now = time.time()
    with workspace._lock:
        workspace._adopted[d] = {"url": "http://127.0.0.1:%d" % app_srv.server_port,
                                 "port": app_srv.server_port, "since": now,
                                 "touched_at": now, "source": "agent", "proven": False}
    f = workspace.frame_fields(proj)
    port = int(f["frame_url"].rsplit(":", 1)[1])
    assert port != app_srv.server_port and not _refused(port)
    workspace.forget(proj)
    assert _refused(port)


def test_a_relay_that_cannot_start_shows_the_plain_message(proj, app_srv, monkeypatch):
    def boom(self):
        raise OSError("no port")
    monkeypatch.setattr(frameproxy.FrameProxy, "start", boom)
    _fake_running(proj, app_srv.server_port)
    f = workspace.frame_fields(proj)
    assert f == {"frame_url": "http://127.0.0.1:%d" % app_srv.server_port,
                 "frame_blocked": True}


def test_an_app_not_answering_yet_is_framed_directly_and_probed_again(proj, monkeypatch):
    calls = []
    monkeypatch.setattr(frameproxy, "fetch_headers",
                        lambda port, timeout=3.0, path="/": calls.append(port))
    monkeypatch.setattr(workspace, "FRAME_RETRY_SECONDS", 0.0)
    _fake_running(proj, 5999)
    f = workspace.frame_fields(proj)
    assert f == {"frame_url": "http://127.0.0.1:5999", "frame_blocked": False}
    for _ in range(workspace.FRAME_MAX_TRIES + 3):
        workspace.frame_fields(proj)
    assert len(calls) == workspace.FRAME_MAX_TRIES       # then it stops asking


# --------------------------------------------------------------------------- #
# The status route and the Build page
# --------------------------------------------------------------------------- #

def _auth():
    import config
    return {"X-Free-LLM-Hub": "dashboard",
            "X-Free-LLM-Hub-Token": config.ensure_control_token()}


def test_the_status_route_carries_frame_url_and_flag_off_does_not(proj, app_srv,
                                                                  monkeypatch):
    import app
    monkeypatch.setattr(app.agentic_chat, "master_enabled", lambda: True)
    _fake_running(proj, app_srv.server_port)
    app.app.config["TESTING"] = True
    with app.app.test_client() as c:
        r = c.get("/api/workspace/status?project_dir=" + proj, headers=_auth())
        st = r.get_json()
        assert r.status_code == 200
        assert st["url"] == "http://127.0.0.1:%d" % app_srv.server_port
        assert st["frame_url"] != st["url"] and st["frame_blocked"] is False
        monkeypatch.setattr(workspace, "_frame_proxy_enabled", lambda: False)
        st2 = c.get("/api/workspace/status?project_dir=" + proj,
                    headers=_auth()).get_json()
        assert "frame_url" not in st2 and "frame_blocked" not in st2


def _html():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "templates", "index.html")
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_the_iframe_loads_frame_url_and_open_in_a_new_tab_keeps_the_real_url():
    html = _html()
    assert 'id="preview-frame"' in html
    assert "var frameUrl = st.frame_url || st.url;" in html
    assert "frame.src = frameUrl; lastUrl = frameUrl;" in html
    assert "frame.src = st.url" not in html
    assert "openEl.href = st.url;" in html


def test_a_blocked_frame_shows_a_plain_message_and_the_real_url():
    html = _html()
    m = re.search(r'<div class="preview-placeholder" id="preview-blocked"[^>]*hidden>(.*?)</div>',
                  html, re.S)
    assert m, "no #preview-blocked box"
    box = m.group(1)
    assert "This app doesn't allow being shown inside another page." in box
    a = re.search(r'<a [^>]*id="preview-blocked-open"[^>]*>Open in a new tab</a>', box)
    assert a and 'target="_blank"' in a.group(0)
    assert 'rel="noopener noreferrer"' in a.group(0)
    assert "if (st.frame_blocked){" in html
    assert "blockedOpen.href = st.url;" in html
    assert html.count("blockedBox.hidden = true;") >= 3   # cleared on every other path
