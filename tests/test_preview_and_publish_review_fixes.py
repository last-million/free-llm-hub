"""Review fixes for the preview (workspace.py), Publish (publish.py) and the
preview relay (frameproxy.py). Hermetic: fake psutil processes, a fake `os`
for the POSIX kill path, real temp folders for the attachment link test, and
socket fakes for the relay. Nothing is started, killed or published."""
import os
import types

import pytest

import frameproxy
import publish
import workspace as W


# --------------------------------------------------------------------------- #
# A printed port is only taken when the project's own process tree holds it
# --------------------------------------------------------------------------- #

class _P:
    def __init__(self, lines, port=5800, pid=4242, project_dir="C:/proj"):
        self.lines = list(lines)
        self.port = port
        self.project_dir = project_dir
        self.popen = types.SimpleNamespace(pid=pid) if pid else None

    def tail(self, n=80):
        return self.lines[-n:]


def test_a_printed_port_in_the_preview_range_is_never_followed(monkeypatch):
    """Another project's preview lives in 5800-5899; _child_listen_port skipped
    that range on purpose, the printed-line path did not."""
    monkeypatch.setattr(W, "_port_open", lambda p: True)
    monkeypatch.setattr(W, "_tree_listen_ports", lambda pid: None)
    p = _P(["Running on http://127.0.0.1:5899"], pid=None)
    assert W._logged_listen_port(p) is None


def test_a_printed_foreign_port_is_not_taken_when_the_tree_is_readable(monkeypatch):
    """The project printed the address of some other local web service."""
    monkeypatch.setattr(W, "_port_open", lambda p: True)
    monkeypatch.setattr(W, "_tree_listen_ports", lambda pid: {5000})
    p = _P(["Running on http://127.0.0.1:56397"])
    assert W._logged_listen_port(p) is None
    p = _P(["Running on http://127.0.0.1:5000"])
    assert W._logged_listen_port(p) == 5000


def test_without_readable_sockets_the_line_still_decides(monkeypatch):
    """No psutil (the measured case the printed-line path exists for)."""
    monkeypatch.setattr(W, "_port_open", lambda p: p == 5000)
    monkeypatch.setattr(W, "_tree_listen_ports", lambda pid: None)
    assert W._logged_listen_port(_P([" * Running on http://127.0.0.1:5000"])) == 5000


def test_a_wsl_project_is_not_checked_against_windows_sockets(monkeypatch):
    """Its Linux sockets are invisible from Windows: an empty set there would
    refuse every printed port."""
    monkeypatch.setattr(W, "_port_open", lambda p: p == 5000)
    monkeypatch.setattr(W, "_tree_listen_ports", lambda pid: set())
    monkeypatch.setattr(W, "_wsl", lambda path: ("Ubuntu", "/home/u/p") if path else None)
    assert W._logged_listen_port(_P(["Running on http://127.0.0.1:5000"])) == 5000


def test_a_printed_debugger_port_is_never_followed(monkeypatch):
    monkeypatch.setattr(W, "_port_open", lambda p: True)
    monkeypatch.setattr(W, "_tree_listen_ports", lambda pid: None)
    assert W._logged_listen_port(_P(["Debugger at http://127.0.0.1:9229/json"])) is None


def _fake_psutil(monkeypatch, ports_by_proc):
    psutil = pytest.importorskip("psutil")

    def conn(port):
        return types.SimpleNamespace(status=psutil.CONN_LISTEN,
                                     laddr=types.SimpleNamespace(port=port))

    procs = [types.SimpleNamespace(net_connections=(lambda kind=None, ps=ps: [conn(x) for x in ps]))
             for ps in ports_by_proc]

    class Root:
        def __init__(self, pid):
            pass

        def children(self, recursive=False):
            return procs[1:]

        def net_connections(self, kind=None):
            return procs[0].net_connections()

    monkeypatch.setattr(psutil, "Process", Root)


def test_the_child_listen_port_skips_the_node_inspector(monkeypatch):
    """`node --inspect` binds 9229 before the app binds its own port."""
    _fake_psutil(monkeypatch, [[9229], [3000]])
    assert W._child_listen_port(1) == 3000


def test_tree_listen_ports_reads_the_whole_tree(monkeypatch):
    _fake_psutil(monkeypatch, [[9229], [3000, 3001]])
    assert W._tree_listen_ports(1) == {9229, 3000, 3001}


# --------------------------------------------------------------------------- #
# Debugger ports are never published
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("port", [9229, 9230, 9231, 9222, 6499])
def test_debugger_ports_are_refused(port):
    with pytest.raises(publish.PublishError) as e:
        publish.check_port(port, {8787})
    assert e.value.code == "forbidden_port"


def test_an_ordinary_dev_port_is_still_allowed():
    assert publish.check_port(3000, {8787}) == 3000


# --------------------------------------------------------------------------- #
# POSIX _kill_tree never signals a reaped pid's new group, nor the hub's
# --------------------------------------------------------------------------- #

class _Popen:
    def __init__(self, pid, exited):
        self.pid = pid
        self._exited = exited
        self.killed = False

    def poll(self):
        return 0 if self._exited else None

    def kill(self):
        self.killed = True


def _posix_os(monkeypatch, getpgid, hub_group=100):
    calls = []

    def killpg(pgid, sig):
        calls.append(("killpg", pgid, sig))

    def _getpgid(pid):
        calls.append(("getpgid", pid))
        return getpgid(pid)

    fake = types.SimpleNamespace(name="posix", getpgid=_getpgid, killpg=killpg,
                                 getpgrp=lambda: hub_group, path=os.path)
    monkeypatch.setattr(W, "os", fake)
    return calls


def test_an_exited_preview_is_not_looked_up_by_its_reused_pid(monkeypatch):
    """The pid now belongs to a process in the hub's group: getpgid would name
    the hub's group and SIGTERM it."""
    calls = _posix_os(monkeypatch, getpgid=lambda pid: 100)
    p = _Popen(555, exited=True)
    W._kill_tree(p, 15)
    assert ("getpgid", 555) not in calls
    assert calls == [("killpg", 555, 15)]
    assert not p.killed


def test_the_hubs_own_group_is_never_signalled(monkeypatch):
    calls = _posix_os(monkeypatch, getpgid=lambda pid: 100)
    p = _Popen(555, exited=False)
    W._kill_tree(p, 15)
    assert not any(c[0] == "killpg" for c in calls)
    assert p.killed                   # only the process itself


def test_a_running_preview_is_still_killed_by_its_group(monkeypatch):
    calls = _posix_os(monkeypatch, getpgid=lambda pid: pid)
    p = _Popen(555, exited=False)
    W._kill_tree(p, 15)
    assert ("killpg", 555, 15) in calls


# --------------------------------------------------------------------------- #
# A pasted image never leaves the project through a link
# --------------------------------------------------------------------------- #

def _make_dir_link(target, link):
    if os.name == "nt":
        try:
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
            return True
        except Exception:                                        # noqa: BLE001
            pass
    try:
        os.symlink(str(target), str(link), target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        return False


def test_an_attachment_folder_that_is_a_link_is_refused(tmp_path):
    proj = tmp_path / "proj"
    outside = tmp_path / "outside"
    proj.mkdir()
    outside.mkdir()
    if not _make_dir_link(outside, proj / W.ATTACH_DIR):
        pytest.skip("cannot create a directory link here")
    with pytest.raises(W.WorkspaceError):
        W.save_attachment(str(proj), b"\x89PNG", "png")
    assert os.listdir(outside) == []


def test_a_dangling_file_link_is_never_written_through(tmp_path):
    proj = tmp_path / "proj"
    outside = tmp_path / "outside"
    (proj / W.ATTACH_DIR).mkdir(parents=True)
    outside.mkdir()
    try:
        os.symlink(str(outside / "stolen.png"), str(proj / W.ATTACH_DIR / "shot-1.png"))
    except (OSError, NotImplementedError):
        pytest.skip("cannot create a file symlink here")
    rel = W.save_attachment(str(proj), b"\x89PNG", "png")
    assert rel == W.ATTACH_DIR + "/shot-2.png"
    assert os.listdir(outside) == []


def test_a_plain_attachment_still_works(tmp_path):
    rel = W.save_attachment(str(tmp_path), b"\x89PNG", "png")
    assert rel == W.ATTACH_DIR + "/shot-1.png"
    assert (tmp_path / W.ATTACH_DIR / "shot-1.png").read_bytes() == b"\x89PNG"


# --------------------------------------------------------------------------- #
# The boot sweep recognises only the hub's own launcher shapes
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("argv", [
    ["python", "-m", "uvicorn", "app:app", "--port", "5850"],
    ["/home/u/proj/.venv/bin/python", "server.py"],
    ["node", "C:\\Users\\u\\AppData\\Roaming\\npm\\node_modules\\serve\\build\\main.js",
     "-l", "5850"],
    ["npx", "serve", "-l", "5850"],
    ["python", "manage.py", "runserver", "5850"],
    ["python", "-m", "http.server", "5850"],          # the user's own, no --bind
    ["node", "vite.js", "--port", "5850"],            # vite without our --host
    ["python", "-m", "http.server", "--bind", "127.0.0.1", "5851"],  # other port
])
def test_a_users_own_server_is_not_recognised(argv):
    assert W._launcher_shape(argv, 5850) is False


@pytest.mark.parametrize("argv", [
    ["C:\\Python312\\python.exe", "-m", "http.server", "--bind", "127.0.0.1", "5850"],
    ["node", "C:\\p\\node_modules\\vite\\bin\\vite.js", "--host", "127.0.0.1",
     "--port", "5850"],
])
def test_the_hubs_launcher_shapes_are_recognised(argv):
    assert W._launcher_shape(argv, 5850) is True


def _sweep_psutil(monkeypatch, argv, env=None, port=5850):
    psutil = pytest.importorskip("psutil")
    conn = types.SimpleNamespace(status=psutil.CONN_LISTEN, pid=77,
                                 laddr=types.SimpleNamespace(port=port))
    monkeypatch.setattr(psutil, "net_connections", lambda kind=None: [conn])

    class Proc:
        def __init__(self, pid):
            pass

        def environ(self):
            return dict(env or {})

        def cmdline(self):
            return list(argv)

    monkeypatch.setattr(psutil, "Process", Proc)


def test_a_uvicorn_server_in_the_range_is_left_alone(monkeypatch):
    _sweep_psutil(monkeypatch, ["python", "-m", "uvicorn", "app:app", "--port", "5850"])
    assert W._is_hub_preview(5850) is False


def test_a_global_npm_tool_in_the_range_is_left_alone(monkeypatch):
    _sweep_psutil(monkeypatch, ["node", "C:\\Users\\u\\AppData\\Roaming\\npm\\x.js"])
    assert W._is_hub_preview(5850) is False


def test_the_marker_still_recognises_a_preview(monkeypatch):
    _sweep_psutil(monkeypatch, ["node", "whatever.js"],
                  env={W._PREVIEW_MARKER: "C:/proj"})
    assert W._is_hub_preview(5850) is True


# --------------------------------------------------------------------------- #
# The relay never leaks the upstream socket of an upgrade
# --------------------------------------------------------------------------- #

class _Sock:
    def __init__(self, fail_send=False):
        self.fail_send = fail_send
        self.closed = False

    def settimeout(self, t):
        pass

    def sendall(self, data):
        if self.fail_send:
            raise ConnectionResetError("browser went away")

    def shutdown(self, how):
        pass

    def close(self):
        self.closed = True


class _Rd:
    def __init__(self, pending):
        self.pending = pending

    def take(self):
        p, self.pending = self.pending, b""
        return p


def test_a_failed_first_write_still_closes_the_upstream_socket():
    fp = frameproxy.FrameProxy(5000, hub_port=8787)
    client, up = _Sock(fail_send=True), _Sock()
    fp._socks.update({client, up})
    with pytest.raises(ConnectionResetError):
        fp._tunnel(client, _Rd(b""), up, _Rd(b"hello"))
    assert up not in fp._socks
    assert up.closed


def test_a_failed_write_to_the_app_also_closes_it():
    fp = frameproxy.FrameProxy(5000, hub_port=8787)
    client, up = _Sock(), _Sock(fail_send=True)
    fp._socks.update({client, up})
    with pytest.raises(ConnectionResetError):
        fp._tunnel(client, _Rd(b"ping"), up, _Rd(b""))
    assert up not in fp._socks
    assert up.closed
