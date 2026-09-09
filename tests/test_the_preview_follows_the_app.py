r"""The preview stayed blank for apps that were up and serving.

REPORTED 2026-09-09: "in /agent page he don't render/launch the app in the
preview".

TWO CAUSES, both on the launch path.

1. THE PORT THE HUB HANDS OUT IS NOT ALWAYS THE PORT THE APP TAKES.
_argv_with_port puts it on the command line for static, vite and npm -- and for
a python entry point returns argv unchanged:

    def _argv_with_port(argv, kind, port):
        if kind == "static": return argv + [str(port)]
        if kind == "vite" or kind.startswith("npm:"): ...
        return argv                       # python:app.py lands here

leaving only the environment (PORT, VITE_PORT, FLASK_RUN_PORT). A generated
`app.py` ending in `app.run(debug=True)` binds 5000; `uvicorn.run(app,
port=8000)` binds 8000; neither reads any of those (FLASK_RUN_PORT is for
`flask run`, not app.run). So the hub waited on its own port for the full
START_TIMEOUT of 90 seconds and then reported

    "no response on port 5801 after 90s"

about an application that had been serving on 5000 the whole time. Blank
preview, red error, working app.

Rather than try to force every stack onto a chosen port, the hub now asks the
process it started what it actually bound.

2. THE FIRST RUN OF A PYTHON PROJECT USED THE WRONG INTERPRETER.
detect() freezes argv, and _venv_python falls back to the hub's own
interpreter when the project has no .venv:

    return exe if os.path.isfile(exe) else sys.executable

On a first run there is never a .venv at detect() time -- install() creates it
afterwards. So argv[0] stayed pointing at the hub's python while the
dependencies went into the project's venv, and the app died with
ModuleNotFoundError; the second Run worked, because by then the venv existed.
Flask projects hid it, since the hub itself has Flask installed. argv[0] is
absolute, so the PATH that _env_for prepends could never have corrected it.
"""
import os
import sys
import textwrap
import time

import pytest

import workspace as W

psutil = pytest.importorskip("psutil")


@pytest.fixture(autouse=True)
def _clean():
    W._procs.clear()
    W._adopted.clear()
    yield
    for d in list(W._procs):
        try:
            W.stop(d)
        except Exception:                                        # noqa: BLE001
            pass
    W._procs.clear()
    W._adopted.clear()


# --------------------------------------------------------------------------- #
# Following the port the app really bound
# --------------------------------------------------------------------------- #

def test_the_hubs_own_port_is_never_followed(monkeypatch):
    """Otherwise a project could be 'previewed' at the dashboard itself."""
    hub = W._hub_port()

    class P:
        pid = 1
        def children(self, recursive=False):
            return []
        def net_connections(self, kind=None):
            return [type("C", (), {"status": psutil.CONN_LISTEN,
                                   "laddr": type("A", (), {"port": hub})()})()]
    monkeypatch.setattr(psutil, "Process", lambda pid: P())
    assert W._child_listen_port(1) is None


def test_a_port_from_the_hubs_own_range_is_never_followed(monkeypatch):
    """A stale preview elsewhere in 5800-5899 must not be mistaken for this
    project's server."""
    class P:
        pid = 1
        def children(self, recursive=False):
            return []
        def net_connections(self, kind=None):
            return [type("C", (), {"status": psutil.CONN_LISTEN,
                                   "laddr": type("A", (), {"port": W.PORT_RANGE[0] + 5})()})()]
    monkeypatch.setattr(psutil, "Process", lambda pid: P())
    assert W._child_listen_port(1) is None


def test_a_real_port_is_followed(monkeypatch):
    class P:
        pid = 1
        def children(self, recursive=False):
            return []
        def net_connections(self, kind=None):
            return [type("C", (), {"status": psutil.CONN_LISTEN,
                                   "laddr": type("A", (), {"port": 5000})()})()]
    monkeypatch.setattr(psutil, "Process", lambda pid: P())
    assert W._child_listen_port(1) == 5000


def test_a_child_holding_the_socket_counts(monkeypatch):
    """npm and a shell wrapper both hold it one level down."""
    child = type("C2", (), {
        "net_connections": lambda self, kind=None: [
            type("C", (), {"status": psutil.CONN_LISTEN,
                           "laddr": type("A", (), {"port": 5173})()})()]})()

    class P:
        pid = 1
        def children(self, recursive=False):
            return [child]
        def net_connections(self, kind=None):
            return []
    monkeypatch.setattr(psutil, "Process", lambda pid: P())
    assert W._child_listen_port(1) == 5173


def test_a_dead_or_unreadable_process_is_not_an_error(monkeypatch):
    def boom(pid):
        raise psutil.NoSuchProcess(pid)
    monkeypatch.setattr(psutil, "Process", boom)
    assert W._child_listen_port(999999) is None


# --------------------------------------------------------------------------- #
# End to end: an app that ignores every port hint the hub gives it
# --------------------------------------------------------------------------- #

STUBBORN = textwrap.dedent("""
    # Binds a port of its own choosing and ignores PORT / FLASK_RUN_PORT --
    # exactly what app.run() and uvicorn.run() do.
    import http.server, socketserver
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", %d), http.server.SimpleHTTPRequestHandler) as s:
        s.serve_forever()
""")


def _wait(project, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        st = W.status(project)
        if st.get("state") in ("running", "failed"):
            return st
        time.sleep(0.25)
    return W.status(project)


def test_an_app_on_its_own_port_is_previewed_not_failed(tmp_path, monkeypatch):
    """The whole bug: 90 seconds of waiting, then a red error, about an app
    that was serving the entire time."""
    port = 5321
    (tmp_path / "app.py").write_text(STUBBORN % port, encoding="utf-8")
    (tmp_path / "index.html").write_text("<title>t</title>", encoding="utf-8")
    monkeypatch.setattr(W, "START_TIMEOUT", 30.0)
    W.start(str(tmp_path))
    st = _wait(str(tmp_path))
    try:
        assert st["state"] == "running", st.get("error")
        assert str(port) in (st.get("url") or ""), st
    finally:
        W.stop(str(tmp_path))


# --------------------------------------------------------------------------- #
# The interpreter
# --------------------------------------------------------------------------- #

def test_the_interpreter_is_resolved_after_the_install(tmp_path):
    """detect() runs before install(), so the argv it froze cannot know about a
    .venv that does not exist yet."""
    src = open("workspace.py", encoding="utf-8").read()
    body = src.split("def start(", 1)[1]
    body = body[:body.index("\ndef ")]
    i_install = body.index("install(run_dir, proc.log)")
    i_resolve = body.index("_venv_python(run_dir)")
    assert i_resolve > i_install, "still launching with the interpreter detect() froze"


def test_a_project_venv_wins_over_the_hubs_interpreter(tmp_path):
    vd = W._venv_dir(str(tmp_path))
    bindir = os.path.join(vd, "Scripts" if os.name == "nt" else "bin")
    os.makedirs(bindir, exist_ok=True)
    exe = os.path.join(bindir, "python.exe" if os.name == "nt" else "python")
    open(exe, "w").close()
    assert W._venv_python(str(tmp_path)) == exe


def test_no_venv_still_falls_back_to_the_hubs_interpreter(tmp_path):
    """Deliberate: a project with no dependencies starts instantly instead of
    paying for a venv it does not need."""
    assert W._venv_python(str(tmp_path)) == sys.executable
