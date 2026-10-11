"""A hub restart comes back, and does not wait 600 s for requests that ended.

MEASURED 2026-10-11 on the owner's machine (install path with spaces:
"...\\bureau 2024\\ALL\\python perso\\..."):

1. The re-exec died with no log line (19:00 and 22:06): os.execv on Windows
   joins its arguments with plain spaces, so the new interpreter read
   "2024\\ALL\\python" as its script, printed "can't open file" into the
   hidden console and exited. The hub stayed down until the self-heal task.
2. Each restart first waited its full 600 s drain (new work refused) because
   the /v1 in-flight counter read 7 with nothing running: werkzeug skips the
   body's close() when the client left mid-stream, so the counter's
   call_on_close never ran.

Hermetic: tmp folders, a tiny Flask app on a real werkzeug server bound to
port 0, no hub restart, no network beyond loopback.
"""
import gc
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time

import pytest
from flask import Flask, Response
from werkzeug.serving import make_server

import app as A


# --------------------------------------------------------------------------- #
# 1. The re-exec keeps every argument whole
# --------------------------------------------------------------------------- #

def test_windows_arguments_are_quoted_one_by_one():
    args = A._reexec_argv(executable=r"C:\a b\python.exe",
                          argv=["app.py", "x y", 'q"z', ""], nt=True)
    assert args == ['"C:\\a b\\python.exe"', "app.py", '"x y"', 'q\\"z', '""']


def test_elsewhere_the_arguments_are_left_alone():
    args = A._reexec_argv(executable="/opt/a b/python", argv=["app.py", "x y"], nt=False)
    assert args == ["/opt/a b/python", "app.py", "x y"]


def test_the_default_is_this_interpreter_and_its_arguments():
    args = A._reexec_argv(nt=False)
    assert args == [sys.executable] + sys.argv


@pytest.mark.skipif(os.name != "nt", reason="os.execv quoting is a Windows behaviour")
def test_a_real_reexec_from_a_folder_with_spaces_starts_the_script(tmp_path):
    folder = tmp_path / "bureau 2024" / "python perso"
    folder.mkdir(parents=True)
    script = folder / "hub.py"
    out = folder / "out.json"
    script.write_text(
        "import json, os, sys\n"
        "if os.environ.get('HUB_REEXEC_TEST') == '1':\n"
        "    os.environ['HUB_REEXEC_TEST'] = '2'\n"
        "    os.execv(sys.executable, json.loads(os.environ['HUB_REEXEC_ARGS']))\n"
        "else:\n"
        "    open(%r, 'w').write(json.dumps(sys.argv))\n" % str(out),
        encoding="utf-8")
    argv = [str(script), "x y", ""]
    env = dict(os.environ, HUB_REEXEC_TEST="1",
               HUB_REEXEC_ARGS=json.dumps(A._reexec_argv(executable=sys.executable,
                                                         argv=argv, nt=True)))
    subprocess.run([sys.executable, str(script)], env=env, timeout=60,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    end = time.time() + 30
    while time.time() < end and not out.exists():
        time.sleep(0.05)
    assert out.exists(), "the re-executed process never ran the script"
    assert json.loads(out.read_text(encoding="utf-8")) == argv


def test_the_reexec_logs_before_it_replaces_the_process():
    src = open(os.path.join(os.path.dirname(A.__file__), "app.py"), encoding="utf-8").read()
    body = src[src.index("def _do_reexec():"):]
    body = body[:body.index("\n\n\n")]
    assert "_reexec_argv()" in body and "Re-exec: starting" in body
    assert body.index("_log.info") < body.index("os.execv")


# --------------------------------------------------------------------------- #
# 2. A response's close callbacks run even when the client left mid-stream
# --------------------------------------------------------------------------- #

class _Src:
    def __init__(self):
        self.closed = 0

    def __iter__(self):
        return iter([b"a", b"b"])

    def close(self):
        self.closed += 1


def test_close_runs_once():
    src = _Src()
    body = A._AlwaysClose(src)
    assert list(body) == [b"a", b"b"]
    body.close()
    body.close()
    del body
    gc.collect()
    assert src.closed == 1


def test_a_body_the_server_forgot_is_closed_when_it_is_dropped():
    src = _Src()
    body = A._AlwaysClose(src)
    next(body)
    del body
    gc.collect()
    assert src.closed == 1


def test_the_hub_wraps_every_response():
    from werkzeug.test import EnvironBuilder
    env = EnvironBuilder(path="/health", base_url="http://127.0.0.1:8787").get_environ()
    body = A.app.wsgi_app(env, lambda *a, **k: None)
    try:
        assert isinstance(body, A._AlwaysClose)
    finally:
        body.close()


def _serve(flask_app):
    srv = make_server("127.0.0.1", 0, A._BuildPrefix(flask_app.wsgi_app), threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.mark.parametrize("reset", [False, True])
def test_a_client_that_leaves_mid_stream_still_closes_the_response(reset):
    fa = Flask("leave")
    closed = threading.Event()

    @fa.route("/s", methods=["POST"])
    def s():
        def gen():
            for i in range(400):
                yield ("data: %d %s\n\n" % (i, "x" * 2000)).encode()
                time.sleep(0.01)
        r = Response(gen(), mimetype="text/event-stream")
        r.call_on_close(closed.set)
        return r

    srv = _serve(fa)
    try:
        c = socket.create_connection(("127.0.0.1", srv.server_port), timeout=10)
        body = b'{"a":1}'
        c.sendall(b"POST /s HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                  b"Content-Length: %d\r\n\r\n%s" % (len(body), body))
        c.recv(4096)
        if reset:
            c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        c.close()
        end = time.time() + 20
        while time.time() < end and not closed.is_set():
            gc.collect()
            time.sleep(0.1)
        assert closed.is_set(), "the response's close callback never ran"
    finally:
        srv.shutdown()


def test_the_in_flight_counter_returns_to_zero_after_a_client_leaves(monkeypatch):
    """The hub's own counter hooks on a real server: a counted /v1-shaped
    stream whose client leaves mid-stream is un-counted."""
    fa = Flask("count")

    @fa.before_request
    def _count():
        from flask import g
        A._runtime_active[0] += 1
        g.runtime_counted = True

    fa.after_request(A._runtime_after)

    @fa.route("/v1/s", methods=["POST"])
    def s():
        def gen():
            for i in range(400):
                yield ("data: %d %s\n\n" % (i, "x" * 2000)).encode()
                time.sleep(0.01)
        return Response(gen(), mimetype="text/event-stream")

    monkeypatch.setattr(A, "_runtime_active", [0])
    srv = _serve(fa)
    try:
        c = socket.create_connection(("127.0.0.1", srv.server_port), timeout=10)
        c.sendall(b"POST /v1/s HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n")
        c.recv(4096)
        assert A._runtime_active[0] == 1
        c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        c.close()
        end = time.time() + 20
        while time.time() < end and A._runtime_active[0]:
            gc.collect()
            time.sleep(0.1)
        assert A._runtime_active[0] == 0
    finally:
        srv.shutdown()
