"""A client that leaves while the hub is still WAITING stops the work it started.

MEASURED 2026-10-03 23:13 on the live hub: `curl -N -m 3` against a streaming
/v1/chat/completions (coding-max) left after 3 s; the activity row stayed
`in_progress` and the chain kept walking g4f relay hops for 58+ s. The same
evening terminal OpenCode (coding-multi, swarm tool fan-out) rows ran up to
600 s after the owner stopped. werkzeug notices a gone client only when it
WRITES, and while the hub waits (the first-content peek, the chain walk, a
buffered tool turn, the fan-out, the pipelines) nothing is written.

These tests run the REAL werkzeug threaded server (the one app.py serves with)
on an ephemeral port, a raw-TCP fake provider that records when its connection
is closed, and a raw-socket client that leaves. They pin: the provider's
connection is cut, no further hop starts, the activity row ends `cancelled`
(499) within a couple of seconds, nothing is filed against the provider -- and
a live client (slow upstream, idle keep-alive) is never cancelled.
"""
import http.client
import json
import socket
import subprocess
import sys
import threading
import time

import pytest
import requests
from werkzeug.serving import make_server

import app as A
import clientgone
import swarm_windows as SW


ANSWER = ("A hash map stores each key in a bucket chosen by its hash, and when two "
          "keys land in the same bucket it either chains them in a small list or "
          "probes for the next free slot, so lookups stay close to constant time "
          "while the table is kept below its load factor by resizing it. Deletion "
          "needs care with open addressing: a tombstone marks the freed slot so a "
          "later probe sequence does not stop early, and a periodic rebuild clears "
          "them once they pile up. Iteration order is whatever the buckets hold, "
          "which is why ordered maps keep a separate linked list of insertions.")

# Chunked, like every real SSE upstream: requests' iter_content(None) on a
# body with neither a length nor chunking reads until EOF.
SSE_HEAD = (b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
            b"Cache-Control: no-cache\r\nTransfer-Encoding: chunked\r\n\r\n")


def _chunked(data):
    return b"%x\r\n" % len(data) + data + b"\r\n"


def _frame(delta, fin=None):
    return _chunked(("data: " + json.dumps({
        "id": "chatcmpl-x", "object": "chat.completion.chunk", "created": 1727400000,
        "model": "m", "choices": [{"index": 0, "delta": delta, "finish_reason": fin}]})
        + "\n\n").encode())


def _json_reply(text):
    body = json.dumps({"id": "chatcmpl-y", "object": "chat.completion",
                       "choices": [{"index": 0, "finish_reason": "stop",
                                    "message": {"role": "assistant", "content": text}}],
                       "usage": {"prompt_tokens": 5, "completion_tokens": 9}}).encode()
    return (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body)) + body


# --------------------------------------------------------------------------- #
# A provider that records when its client (the hub) hung up
# --------------------------------------------------------------------------- #

class Upstream:
    """Raw TCP: reads one HTTP request per connection, then plays `script`
    ((delay_s, bytes) steps), then waits for the hub to hang up and records
    when it did. `script` may be a callable(n) -> steps for connection n."""

    def __init__(self, script=()):
        self.script = script
        self.conns = []
        self._lock = threading.Lock()
        self._srv = socket.socket()
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(32)
        self.url = "http://127.0.0.1:%d/v1/chat/completions" % self._srv.getsockname()[1]
        self._stop = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self._stop.set()
        try:
            self._srv.close()
        except OSError:
            pass

    def _accept(self):
        while not self._stop.is_set():
            try:
                c, _ = self._srv.accept()
            except OSError:
                return
            with self._lock:
                rec = {"opened": time.monotonic(), "closed": None, "n": len(self.conns)}
                self.conns.append(rec)
            threading.Thread(target=self._serve, args=(c, rec), daemon=True).start()

    @staticmethod
    def _read_request(c):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = c.recv(65536)
            if not chunk:
                return False
            data += chunk
        head, _, body = data.partition(b"\r\n\r\n")
        length = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1])
        while len(body) < length:
            chunk = c.recv(65536)
            if not chunk:
                return False
            body += chunk
        return True

    def _peer_gone(self, c, wait):
        """Wait up to `wait` s; True (and recorded) once the hub hung up."""
        end = time.monotonic() + wait
        c.settimeout(0.05)
        while time.monotonic() < end and not self._stop.is_set():
            try:
                if c.recv(4096) == b"":
                    return True
            except socket.timeout:
                continue
            except OSError:
                return True
        return False

    def _serve(self, c, rec):
        try:
            if not self._read_request(c):
                rec["closed"] = time.monotonic()
                return
            steps = self.script(rec["n"]) if callable(self.script) else self.script
            for delay, data in steps:
                if delay and self._peer_gone(c, delay):
                    rec["closed"] = time.monotonic()
                    return
                try:
                    c.sendall(data)
                except OSError:
                    rec["closed"] = time.monotonic()
                    return
            if self._peer_gone(c, 30):
                rec["closed"] = time.monotonic()
        finally:
            try:
                c.close()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# The hub, served by the real werkzeug threaded server
# --------------------------------------------------------------------------- #

@pytest.fixture
def filed(monkeypatch):
    """Routing pinned to a 3-hop chain whose every hop is a REAL requests call
    to the fake provider. The failure ledgers stay REAL -- the reliability
    one on a fresh dict, the recent-failure one cleared by conftest -- so the
    guards inside them are what is under test."""
    seen = {"dispatch": [], "throttled": []}
    monkeypatch.setattr(A, "_outcomes", {})
    monkeypatch.setattr(A, "_junk_bench_note", lambda *a, **k: None)
    monkeypatch.setattr(A.quota, "mark_model_throttled",
                        lambda *a, **k: seen["throttled"].append(a))
    monkeypatch.setattr(A.quota, "mark_throttled",
                        lambda *a, **k: seen["throttled"].append(a))
    for name in ("_record_chat_usage", "_save_perf_stats", "_note_ttft"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)
    monkeypatch.setattr(A.usage_history, "record", lambda *a, **k: None)
    monkeypatch.setattr(A, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(A, "_model_block_reason", lambda pid, m: None)
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    monkeypatch.setattr(A, "_is_trivial_turn", lambda *a, **k: False)
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("p1", "m1", "hard"))
    monkeypatch.setattr(A, "_apply_orchestrator", lambda pid, resolved, *a, **k: (pid, resolved))
    monkeypatch.setattr(A, "_build_chain",
                        lambda *a, **k: [("p1", "m1"), ("p2", "m2"), ("p3", "m3")])
    # When the handler itself RETURNED (the request thread let go), for the
    # chat-completions surfaces: the row being finished is not enough.
    seen["ended"] = []
    _orig_router = A._chat_completions_uncached

    def _timed_router(body):
        try:
            return _orig_router(body)
        finally:
            seen["ended"].append(time.monotonic())
    monkeypatch.setattr(A, "_chat_completions_uncached", _timed_router)
    with A._activity_lock:
        A._activity.clear()
    yield seen


def _wire(monkeypatch, filed, up):
    def dispatch(pid, payload, stream):
        filed["dispatch"].append((pid, payload.get("model")))
        return requests.post(up.url, json={"model": payload.get("model")},
                             stream=stream, timeout=(5, 30))
    monkeypatch.setattr(A, "_dispatch_chat", dispatch)


@pytest.fixture
def hub():
    srv = make_server("127.0.0.1", 0, A.app, threaded=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_port
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def upstream():
    made = []

    def _make(script=()):
        up = Upstream(script)
        made.append(up)
        return up
    yield _make
    for up in made:
        up.close()


def _send(port, body, path="/v1/chat/completions"):
    s = socket.create_connection(("127.0.0.1", port))
    data = json.dumps(body).encode()
    s.sendall(b"POST " + path.encode() + b" HTTP/1.1\r\nHost: 127.0.0.1\r\n"
              b"Content-Type: application/json\r\nContent-Length: "
              + str(len(data)).encode() + b"\r\n\r\n" + data)
    return s


def _row():
    with A._activity_lock:
        return dict(A._activity[0]) if A._activity else None


def _until(pred, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.05)
    return pred()


def _wait_conns(up, n, timeout=10):
    return _until(lambda: len(up.conns) >= n, timeout)


def _assert_cancelled(up, filed, left_at, conns=1, pairs=(("p1", "m1"),)):
    row = _until(lambda: (_row() or {}).get("finished") and _row(), 4)
    assert row and row["status"] == "cancelled", row
    assert row["http"] == 499
    assert row["finished"] - (time.time() - (time.monotonic() - left_at)) < 3.0, \
        "finished when the cancel was detected, not whenever the hop ended"
    assert _until(lambda: all(c["closed"] for c in up.conns), 4), \
        "the provider's connection must be cut: %r" % up.conns
    for c in up.conns:
        assert c["closed"] - left_at < 3.0, "cut within ~2 s of the client leaving"
    time.sleep(1.2)
    assert len(up.conns) == conns, "no further hop may start for a gone client"
    assert len(filed["dispatch"]) == conns
    assert not [k for k, r in A._outcomes.items() if r.get("fail")], \
        "the client left: nothing filed against the provider: %r" % A._outcomes
    assert not filed["throttled"]
    for p, m in pairs:
        assert A._recent_hop_failure(p, m) is None
    if filed["ended"]:
        assert max(filed["ended"]) - left_at < 3.5, \
            "the request thread let go, not just the activity row"


# --------------------------------------------------------------------------- #
# The monitor itself (socket pairs, no server)
# --------------------------------------------------------------------------- #

def _pair():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    c = socket.create_connection(srv.getsockname())
    s, _ = srv.accept()
    srv.close()
    return c, s


def test_the_monitor_tells_a_gone_client_from_a_live_one():
    # background=False: only the explicit check() passes below look. With the
    # thread, a loaded machine started it late, its first pass ran after the
    # close and took the gone token first, so check() returned [] (FULL SUITE,
    # 2026-10-04). The FIN's arrival is waited for, never assumed.
    mon = clientgone.Monitor(poll=60, background=False)
    live_c, live_s = _pair()
    busy_c, busy_s = _pair()
    gone_c, gone_s = _pair()
    t_live, t_busy, t_gone = clientgone.Token(), clientgone.Token(), clientgone.Token()
    try:
        for s, t in ((live_s, t_live), (busy_s, t_busy), (gone_s, t_gone)):
            assert mon.watch(s, t)
        busy_c.sendall(b"GET /next HTTP/1.1\r\n")    # a pipelined request: data pending
        assert mon.check() == [], "nobody has left yet"
        gone_c.close()
        found = []
        assert _until(lambda: found.extend(mon.check()) or found, 10), \
            "the closed client is seen within 10 s"
        assert found == [t_gone]
        assert t_gone.cancelled and not t_live.cancelled and not t_busy.cancelled
        assert not mon.watching(t_gone) and mon.watching(t_live) and mon.watching(t_busy)
        # No false positive, however many passes: a live idle client and one
        # with bytes pending stay watched and uncancelled.
        for _ in range(5):
            assert mon.check() == []
            time.sleep(0.05)
        assert not t_live.cancelled and not t_busy.cancelled
        # MSG_PEEK consumed nothing: the pending bytes are all still there
        busy_s.settimeout(5)
        assert busy_s.recv(100) == b"GET /next HTTP/1.1\r\n"
        assert mon.check() == []                     # a live idle client, again
    finally:
        for s in (live_c, live_s, busy_c, busy_s, gone_s):
            s.close()


def test_a_killed_client_process_reads_as_gone():
    """/agent Stop kills the CLI's process tree; the OS closes its sockets."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    child = subprocess.Popen([sys.executable, "-c",
                              "import socket,time;c=socket.create_connection(('127.0.0.1',%d));"
                              "time.sleep(60)" % srv.getsockname()[1]])
    try:
        srv.settimeout(30)
        s, _ = srv.accept()
        mon, tok = clientgone.Monitor(poll=60, background=False), clientgone.Token()
        mon.watch(s, tok)
        assert mon.check() == [] and not tok.cancelled
        child.kill()
        child.wait(10)
        found = []
        assert _until(lambda: found.extend(mon.check()) or found, 10)
        assert found == [tok] and tok.cancelled
        s.close()
    finally:
        if child.poll() is None:
            child.kill()
        srv.close()


def test_a_cancel_runs_its_hooks_once_and_refuses_new_upstream_calls(upstream):
    up = upstream([(0, _json_reply("hello"))])
    tok = clientgone.Token()
    hits = []
    tok.add_hook(lambda: hits.append(1))
    assert tok.cancel("test") is True
    assert tok.cancel("again") is False and hits == [1]
    clientgone.set_current(tok)
    try:
        with pytest.raises(clientgone.ClientGone):
            requests.post(up.url, json={}, timeout=(5, 5))
    finally:
        clientgone.set_current(None)
    time.sleep(0.3)
    assert up.conns == [], "no connection is even opened for a gone client"
    # outside any request nothing changes
    r = requests.post(up.url, json={}, timeout=(5, 5))
    assert r.status_code == 200


# --------------------------------------------------------------------------- #
# End to end: the real server, a real provider socket, a client that leaves
# --------------------------------------------------------------------------- #

def test_a_client_that_leaves_during_the_first_content_peek_stops_the_chain(
        hub, upstream, filed, monkeypatch):
    """The measured shape: streaming, the provider answered headers and then
    nothing; the hub sat in its pre-commit peek and walked on for 58+ s."""
    up = upstream([(0, SSE_HEAD), (0, _chunked(b": keepalive\n\n"))])
    _wire(monkeypatch, filed, up)
    cli = _send(hub, {"model": "auto", "stream": True,
                      "messages": [{"role": "user", "content": "explain hash maps in depth"}]})
    assert _wait_conns(up, 1)
    time.sleep(0.8)
    cli.close()
    left = time.monotonic()
    _assert_cancelled(up, filed, left)


@pytest.mark.parametrize("path,body", [
    ("/v1/responses", {"model": "auto", "stream": True,
                       "input": "explain hash maps in depth"}),
    ("/v1/messages", {"model": "auto", "stream": True, "max_tokens": 512,
                      "messages": [{"role": "user", "content": "explain hash maps in depth"}]}),
])
def test_codex_and_claude_code_paths_stop_the_same_way(
        hub, upstream, filed, monkeypatch, path, body):
    up = upstream([(0, SSE_HEAD), (0, _chunked(b": keepalive\n\n"))])
    _wire(monkeypatch, filed, up)
    cli = _send(hub, body, path=path)
    assert _wait_conns(up, 1)
    time.sleep(0.8)
    cli.close()
    left = time.monotonic()
    _assert_cancelled(up, filed, left)


def test_a_client_that_leaves_mid_stream_after_commit_cuts_the_provider(
        hub, upstream, filed, monkeypatch):
    up = upstream([(0, SSE_HEAD), (0, _frame({"role": "assistant", "content": ANSWER}))])
    _wire(monkeypatch, filed, up)
    cli = _send(hub, {"model": "auto", "stream": True,
                      "messages": [{"role": "user", "content": "explain hash maps in depth"}]})
    cli.settimeout(10)
    got = b""
    while b"hash map" not in got:
        chunk = cli.recv(65536)
        assert chunk, "the stream was committed and its first text relayed"
        got += chunk
    assert b"200 OK" in got
    cli.close()
    left = time.monotonic()
    _assert_cancelled(up, filed, left)


def test_a_non_stream_client_that_leaves_cuts_the_generation(
        hub, upstream, filed, monkeypatch):
    """A buffered request: the provider generates, the hub writes nothing."""
    up = upstream([])
    _wire(monkeypatch, filed, up)
    cli = _send(hub, {"model": "auto", "stream": False,
                      "messages": [{"role": "user", "content": "refactor the parser module"}]})
    assert _wait_conns(up, 1)
    time.sleep(0.8)
    cli.close()
    left = time.monotonic()
    _assert_cancelled(up, filed, left)


def test_a_client_that_leaves_the_tool_fan_out_stops_every_member(
        hub, upstream, filed, monkeypatch):
    """Swarm tool turn: several members in flight, then the CLI is stopped."""
    up = upstream([])
    _wire(monkeypatch, filed, up)
    monkeypatch.setattr(A, "_swarm_fast_path", lambda *a, **k: False)
    monkeypatch.setattr(A, "_swarm_tool_candidates", lambda chain: (list(chain), []))
    monkeypatch.setattr(A, "_swarm_rank", lambda cands, difficulty=None: list(cands))
    cli = _send(hub, {"model": "swarm", "stream": True,
                      "tools": [{"type": "function", "function": {
                          "name": "write_file", "parameters": {"type": "object"}}}],
                      "messages": [{"role": "user",
                                    "content": "build the parser module and its tests"}]})
    assert _wait_conns(up, 3)
    time.sleep(0.8)
    cli.close()
    left = time.monotonic()
    _assert_cancelled(up, filed, left, conns=3,
                      pairs=(("p1", "m1"), ("p2", "m2"), ("p3", "m3")))
    for pair in (("p1", "m1"), ("p2", "m2"), ("p3", "m3")):
        assert not A._swarm_member_failed(*pair)


def test_a_client_that_leaves_a_prose_pipeline_stops_it_between_stages(
        hub, upstream, filed, monkeypatch):
    """A tool-free swarm turn (plan -> workers -> review -> synthesis): the
    stage in flight is cut, and no later stage starts a hop."""
    up = upstream([])
    _wire(monkeypatch, filed, up)
    monkeypatch.setattr(A, "_swarm_fast_path", lambda *a, **k: False)
    monkeypatch.setattr(A, "_swarm_manager_kwargs", lambda: {})
    cli = _send(hub, {"model": "swarm", "stream": False,
                      "messages": [{"role": "user", "content":
                                    "Write a tokenizer, a parser and a test suite for a "
                                    "small arithmetic language, with a README."}]})
    assert _wait_conns(up, 1)
    time.sleep(0.8)
    cli.close()
    left = time.monotonic()
    _assert_cancelled(up, filed, left, conns=len(up.conns))
    assert len(up.conns) == 1, "the plan hop only: no stage started after the cancel"


def test_a_killed_cli_process_cancels_its_request(hub, upstream, filed, monkeypatch):
    """What /agent Stop does to a CLI (agentic_chat._terminate): the process
    dies, the OS closes its socket, the hub stops the request it had open."""
    up = upstream([(0, SSE_HEAD)])
    _wire(monkeypatch, filed, up)
    body = json.dumps({"model": "auto", "stream": True,
                       "messages": [{"role": "user", "content": "explain hash maps"}]})
    code = ("import socket,time\n"
            "c=socket.create_connection(('127.0.0.1',%d))\n"
            "b=%r.encode()\n"
            "c.sendall(b'POST /v1/chat/completions HTTP/1.1\\r\\nHost: 127.0.0.1\\r\\n"
            "Content-Type: application/json\\r\\nContent-Length: '+str(len(b)).encode()"
            "+b'\\r\\n\\r\\n'+b)\n"
            "time.sleep(60)\n" % (hub, body))
    child = subprocess.Popen([sys.executable, "-c", code])
    try:
        assert _wait_conns(up, 1)
        time.sleep(0.8)
        child.kill()
        child.wait(10)
        left = time.monotonic()
        _assert_cancelled(up, filed, left)
    finally:
        if child.poll() is None:
            child.kill()


# --------------------------------------------------------------------------- #
# No false positive: a live client is never cancelled
# --------------------------------------------------------------------------- #

def test_a_live_client_waiting_on_a_slow_first_token_is_not_cancelled(
        hub, upstream, filed, monkeypatch):
    up = upstream([(0, SSE_HEAD), (2.5, _frame({"role": "assistant", "content": ANSWER})),
                   (0, _frame({}, "stop")), (0, _chunked(b"data: [DONE]\n\n")),
                   (0, b"0\r\n\r\n")])
    _wire(monkeypatch, filed, up)
    cli = _send(hub, {"model": "auto", "stream": True,
                      "messages": [{"role": "user", "content": "explain hash maps in depth"}]})
    cli.settimeout(20)
    got = b""
    while b"[DONE]" not in got:
        chunk = cli.recv(65536)
        if not chunk:
            break
        got += chunk
    cli.close()
    assert b"tombstone" in got and b"[DONE]" in got
    row = _until(lambda: (_row() or {}).get("finished") and _row(), 4)
    assert row["status"] == "ok", row
    assert len(up.conns) == 1 and len(filed["dispatch"]) == 1


def test_an_idle_keep_alive_client_between_two_requests_is_not_cancelled(
        hub, upstream, filed, monkeypatch):
    up = upstream([(1.2, _json_reply(ANSWER))])
    _wire(monkeypatch, filed, up)
    conn = http.client.HTTPConnection("127.0.0.1", hub, timeout=20)
    body = json.dumps({"model": "auto", "stream": False,
                       "messages": [{"role": "user", "content": "explain hash maps"}]})
    statuses = []
    for _ in range(2):
        conn.request("POST", "/v1/chat/completions", body=body,
                     headers={"Content-Type": "application/json",
                              "Connection": "keep-alive"})
        resp = conn.getresponse()
        data = resp.read()
        statuses.append(resp.status)
        assert b"tombstone" in data
        time.sleep(1.0)                          # idle on the (kept) connection
    conn.close()
    assert statuses == [200, 200]
    with A._activity_lock:
        rows = [dict(r) for r in A._activity]
    assert [r["status"] for r in rows] == ["ok", "ok"], rows
    assert clientgone.MONITOR.count() == 0, "nothing left registered"


# --------------------------------------------------------------------------- #
# A hop the hub gives up on is closed WITHOUT blocking the next one
# --------------------------------------------------------------------------- #
# MEASURED before the fix (Windows, requests 2.34 / urllib3 2.7): after a
# first-content peek gave up on a relay that sent headers then nothing, the
# loop's resp.close() waited on the peek worker's blocked read until the read
# timeout -- on the 8799 sandbox with an 8 s peek, hop 2 had not started 20 s
# later. clientgone.nonblocking_close shuts the socket down first.

def _two_hop_relay(first):
    """Connection 0: `first`; connection 1: a complete streamed answer."""
    answer = [(0, SSE_HEAD), (0, _frame({"role": "assistant", "content": ANSWER})),
              (0, _frame({}, "stop")), (0, _chunked(b"data: [DONE]\n\n")),
              (0, b"0\r\n\r\n")]
    return lambda n: first if n == 0 else answer


def test_a_silent_hop_after_its_peek_does_not_hold_the_next_hop(
        hub, upstream, filed, monkeypatch):
    peek = 1.5
    monkeypatch.setattr(A, "_stream_peek_timeout", lambda *a, **k: peek)
    up = upstream(_two_hop_relay([(0, SSE_HEAD), (0, _chunked(b": keepalive\n\n"))]))
    _wire(monkeypatch, filed, up)
    cli = _send(hub, {"model": "auto", "stream": True,
                      "messages": [{"role": "user", "content": "explain hash maps in depth"}]})
    cli.settimeout(30)
    got = b""
    while b"[DONE]" not in got:
        chunk = cli.recv(65536)
        if not chunk:
            break
        got += chunk
    cli.close()
    assert b"tombstone" in got, "hop 2 answered"
    assert len(up.conns) == 2
    gap = up.conns[1]["opened"] - up.conns[0]["opened"]
    assert gap < peek + 1.0, "hop 2 started %.2fs after hop 1 (peek %.1fs)" % (gap, peek)
    assert _until(lambda: up.conns[0]["closed"], 2), "the abandoned stream is closed"
    assert up.conns[0]["closed"] - up.conns[0]["opened"] < peek + 1.0
    # ...and the hop that SERVED is released too, not kept open by the close
    # wrapper (a reference cycle there held it until the cyclic GC).
    assert _until(lambda: up.conns[1]["closed"], 3), "the served stream is released"
    row = _until(lambda: (_row() or {}).get("finished") and _row(), 4)
    assert row["status"] == "ok", row


def test_a_hop_out_of_budget_has_its_call_cut_not_left_open(
        hub, upstream, filed, monkeypatch):
    """A buffered hop past its budget (_call_with_wall_clock): the request
    walks on, and the abandoned call's connection is closed at once instead
    of staying open until the provider's read timeout."""
    budget = 1.0
    monkeypatch.setattr(A, "_is_trivial_turn", lambda *a, **k: True)
    monkeypatch.setattr(A, "_TRIVIAL_HOP_BUDGET", budget)
    monkeypatch.setattr(A, "_TRIVIAL_SLOW_HOP_BUDGET", budget)
    monkeypatch.setattr(A, "_adaptive_hop_budget", lambda pid, model, ceiling, stream=None: ceiling)
    up = upstream(lambda n: [] if n == 0 else [(0, _json_reply(ANSWER))])
    _wire(monkeypatch, filed, up)
    conn = http.client.HTTPConnection("127.0.0.1", hub, timeout=20)
    conn.request("POST", "/v1/chat/completions", body=json.dumps({
        "model": "auto", "stream": False,
        "messages": [{"role": "user", "content": "What is 5767 plus 1?"}]}),
        headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    assert resp.status == 200 and b"tombstone" in data
    assert len(up.conns) == 2
    assert up.conns[1]["opened"] - up.conns[0]["opened"] < budget + 1.0
    assert _until(lambda: up.conns[0]["closed"], 2), "the abandoned call is cut"
    assert up.conns[0]["closed"] - up.conns[0]["opened"] < budget + 1.0


def test_a_response_nobody_reads_still_closes_inline():
    """No live socket (a fully read body, a fake): close() is the plain one."""
    import requests as rq

    class _Raw:
        _connection = None
        _fp = None

    r = rq.Response()
    r.raw = _Raw()
    closed = []
    r.raw.close = lambda: closed.append(1)
    clientgone.nonblocking_close(r)
    clientgone.nonblocking_close(r)              # idempotent
    r.close()
    assert closed == [1]


# --------------------------------------------------------------------------- #
# Multi: Stop reaches the workers that are already running
# --------------------------------------------------------------------------- #

def test_stopping_a_multi_run_stops_its_running_workers(tmp_path, monkeypatch):
    """swarm_windows.stop used to set a flag that only stopped NEW phases;
    a running worker's CLI kept editing the folder and calling the hub."""
    monkeypatch.setenv(SW._STORE_ENV, str(tmp_path / "runs"))
    started = threading.Event()
    release = threading.Event()
    stopped = []

    def run_turn(session_id, prompt):
        started.set()
        release.wait(10)
        yield {"type": "message", "text": "too late"}

    def stop(sid):
        stopped.append(sid)
        release.set()            # what killing the CLI does to its turn

    rid = SW.start("g", ".", "opencode", lambda cli, project: "sess-w1", run_turn,
                   phases=[{"title": "T", "task": "t", "needs": []}], review=False,
                   stop=stop)
    try:
        assert started.wait(10)
        assert SW.stop(rid) is True
        assert _until(lambda: stopped == ["sess-w1"], 3), stopped
    finally:
        release.set()
        SW._RUNS.pop(rid, None)


# --------------------------------------------------------------------------- #
# The dashboard shows it finished, neutral
# --------------------------------------------------------------------------- #

def test_the_activity_view_renders_cancelled_as_finished_and_neutral():
    import pathlib
    html = (pathlib.Path(A.__file__).parent / "templates" / "index.html").read_text(
        encoding="utf-8")
    assert "st === 'cancelled' ? 'cancel'" in html
    assert ".af-status.cancel{" in html
    pending_line = next(ln for ln in html.splitlines() if "var pending = (st ===" in ln)
    assert "cancelled" not in pending_line, "cancelled is finished, never 'running'"
