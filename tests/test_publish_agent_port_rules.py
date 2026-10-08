"""Which ports an AGENT may publish (2026-10-08 security fix).

The brief asks the model to get a yes from the user first, but the server cannot
see the chat: a prompt-injected agent could call publish_start for ANY local HTTP
service (a database admin page on 8080). So `_publish_cli_start` now asks
`_publish_port_is_project_server(folder, port)` before it ever reaches the
engine: only a server running FROM the project folder may be published. Fails
closed. The dashboard's Publish button (the /api/publish/* routes) is a human
click and keeps its own consent.

Everything here is hermetic: a fake psutil module in sys.modules (no socket, no
real process), a fake workspace.running, a fake engine. No tunnel is started and
nothing contacts Cloudflare.
"""
import collections
import json
import logging
import os
import sys
import types

import pytest

import app as A
import config
import hub_mcp
import publish

PORT = 4000
URL = "https://quiet-river-123.trycloudflare.com"
Addr = collections.namedtuple("addr", "ip port")


class AccessDenied(Exception):
    pass


class NoSuchProcess(Exception):
    pass


class FakeConn:
    def __init__(self, port, ip="127.0.0.1", pid=None, status="LISTEN"):
        self.status = status
        self.laddr = Addr(ip, port) if port is not None else ()
        self.pid = pid


class FakeProc:
    def __init__(self, pid, cwd, parent=None, cwd_error=None):
        self.pid = pid
        self._cwd = cwd
        self._parent = parent
        self._cwd_error = cwd_error
        self.sockets = []                  # for the per-process fallback

    def cwd(self):
        if self._cwd_error is not None:
            raise self._cwd_error
        return self._cwd

    def parent(self):
        return self._parent

    def net_connections(self, kind="inet"):
        return list(self.sockets)


class FakePsutil(types.ModuleType):
    CONN_LISTEN = "LISTEN"

    def __init__(self):
        super().__init__("psutil")
        self.procs = {}
        self.conns = []
        self.system_error = None           # raised by the system-wide call
        self.iter_error = None

    def net_connections(self, kind="inet"):
        if self.system_error is not None:
            raise self.system_error
        return list(self.conns)

    def Process(self, pid):                                        # noqa: N802
        if pid not in self.procs:
            raise NoSuchProcess(pid)
        return self.procs[pid]

    def process_iter(self, attrs=None):
        if self.iter_error is not None:
            raise self.iter_error
        return iter(list(self.procs.values()))


class Box:
    """One project folder, a sibling that merely shares its prefix, and a fake
    machine to put listeners on."""

    def __init__(self, tmp_path, psutil):
        self.folder = str(tmp_path / "proj")
        self.evil = str(tmp_path / "proj-evil")
        self.other = str(tmp_path / "elsewhere")
        for d in (self.folder, self.evil, self.other):
            os.makedirs(d, exist_ok=True)
        os.makedirs(os.path.join(self.folder, "frontend"), exist_ok=True)
        self.ps = psutil
        self._pid = 100

    def listen(self, cwd, port=PORT, ip="127.0.0.1", parent=None, cwd_error=None,
               pid=None, conn_pid=True):
        self._pid += 1
        pid = pid or self._pid
        proc = FakeProc(pid, cwd, parent=parent, cwd_error=cwd_error)
        self.ps.procs[pid] = proc
        conn = FakeConn(port, ip, pid if conn_pid else None)
        self.ps.conns.append(conn)
        proc.sockets.append(conn)
        return proc

    def process(self, cwd, parent=None, cwd_error=None):
        self._pid += 1
        proc = FakeProc(self._pid, cwd, parent=parent, cwd_error=cwd_error)
        self.ps.procs[proc.pid] = proc
        return proc

    def ok(self, port=PORT):
        return A._publish_port_is_project_server(self.folder, port)


@pytest.fixture
def box(monkeypatch, tmp_path):
    ps = FakePsutil()
    monkeypatch.setitem(sys.modules, "psutil", ps)
    monkeypatch.setattr(A.workspace, "running", lambda: [])
    # A home that is NOT an ancestor of tmp_path, whatever machine this runs on.
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: str(tmp_path / "home" / "me") if p == "~" else p)
    return Box(tmp_path, ps)


# --------------------------------------------------------------------------- #
# 1. (a) the hub's own preview of this folder
# --------------------------------------------------------------------------- #

def test_allowed_by_the_hubs_own_preview(box, monkeypatch):
    monkeypatch.setattr(A.workspace, "running", lambda: [
        {"project_dir": box.folder, "port": PORT, "state": "running", "external": False}])
    assert box.ok()


def test_a_preview_needs_no_psutil(box, monkeypatch):
    monkeypatch.setattr(A.workspace, "running", lambda: [
        {"project_dir": box.folder, "port": PORT, "state": "running"}])
    monkeypatch.setitem(sys.modules, "psutil", None)
    assert box.ok()


@pytest.mark.skipif(os.name != "nt", reason="case folding is a Windows rule")
def test_preview_folder_is_compared_case_insensitively_on_windows(box, monkeypatch):
    monkeypatch.setattr(A.workspace, "running", lambda: [
        {"project_dir": box.folder.upper(), "port": PORT, "state": "running"}])
    assert box.ok()


@pytest.mark.parametrize("row", [
    {"port": PORT + 1},                                   # another port
    {"state": "starting"},                                # not up yet
    {"state": "failed"},
    {"external": True},                                   # adopted on the agent's word
    {"project_dir": "other"},                             # another project's preview
])
def test_a_preview_that_is_not_this_projects_running_server_does_not_count(box, monkeypatch, row):
    base = {"project_dir": box.folder, "port": PORT, "state": "running", "external": False}
    base.update(row)
    if base["project_dir"] == "other":
        base["project_dir"] = box.evil
    monkeypatch.setattr(A.workspace, "running", lambda: [base])
    assert not box.ok()


def test_a_crashing_preview_list_does_not_hide_a_provable_listener(box, monkeypatch):
    def boom():
        raise RuntimeError("registry broke")
    monkeypatch.setattr(A.workspace, "running", boom)
    box.listen(box.folder)
    assert box.ok()


# --------------------------------------------------------------------------- #
# 2. (b) a listener whose working directory is inside the folder
# --------------------------------------------------------------------------- #

def test_allowed_when_the_listener_runs_in_the_folder(box):
    box.listen(box.folder)
    assert box.ok()


def test_allowed_when_the_listener_runs_in_a_subfolder(box):
    box.listen(os.path.join(box.folder, "frontend"))
    assert box.ok()


@pytest.mark.parametrize("ip", ["127.0.0.1", "127.0.1.1", "::1", "0.0.0.0", "::",
                                "::ffff:127.0.0.1"])
def test_loopback_and_wildcard_binds_count(box, ip):
    # Node's listen(port) binds "::", Flask's host="0.0.0.0": both answer on loopback.
    box.listen(box.folder, ip=ip)
    assert box.ok()


def test_allowed_via_the_parent_process(box):
    npm = box.process(box.folder)
    box.listen(box.other, parent=npm)                     # node started by npm
    assert box.ok()


def test_the_parent_walk_goes_three_levels_up_and_no_further(box):
    top = box.process(box.folder)
    mid2 = box.process(box.other, parent=top)
    mid1 = box.process(box.other, parent=mid2)
    box.listen(box.other, parent=mid1)                    # listener -> mid1 -> mid2 -> top
    assert box.ok()
    deeper = box.process(box.other, parent=mid1)
    deeper2 = box.process(box.other, parent=deeper)
    box.ps.conns.clear()
    box.listen(box.other, parent=deeper2)                 # top is now 4 levels up
    assert not box.ok()


def test_a_parent_cycle_does_not_loop(box):
    a = box.process(box.other)
    b = box.process(box.other, parent=a)
    a._parent = b
    box.listen(box.other, parent=a)
    assert not box.ok()


# --------------------------------------------------------------------------- #
# 3. fail closed
# --------------------------------------------------------------------------- #

def test_refused_when_the_listener_runs_elsewhere(box):
    box.listen(box.other)                                 # e.g. a database admin page
    assert not box.ok()


def test_refused_for_a_sibling_that_only_shares_the_prefix(box):
    box.listen(box.evil)                                  # proj-evil is not inside proj
    assert not box.ok()
    assert not A._publish_inside(box.folder, box.evil)
    assert A._publish_inside(box.folder, box.folder)
    assert A._publish_inside(box.folder, os.path.join(box.folder, "a", "b"))


@pytest.mark.parametrize("err", [AccessDenied("pid 4"), NoSuchProcess(4)])
def test_refused_when_the_working_directory_cannot_be_read(box, err):
    box.listen(box.folder, cwd_error=err)
    assert not box.ok()


def test_an_unreadable_listener_is_not_rescued_by_a_readable_parent(box):
    npm = box.process(box.folder)
    box.listen(box.folder, parent=npm, cwd_error=AccessDenied("x"))
    assert not box.ok()


def test_refused_when_a_parent_cannot_be_read(box):
    npm = box.process(box.folder, cwd_error=AccessDenied("x"))
    box.listen(box.other, parent=npm)
    assert not box.ok()


def test_refused_for_an_empty_working_directory(box):
    box.listen("")
    assert not box.ok()


def test_refused_when_the_process_vanished_after_the_listing(box):
    box.listen(box.folder)
    box.ps.procs.clear()                                  # Process(pid) -> NoSuchProcess
    assert not box.ok()


def test_refused_with_no_listener(box):
    assert not box.ok()


def test_refused_for_a_listener_on_another_port(box):
    box.listen(box.folder, port=PORT + 1)
    assert not box.ok()


def test_a_connection_that_is_not_listening_does_not_count(box):
    proc = box.process(box.folder)
    box.ps.conns.append(FakeConn(PORT, pid=proc.pid, status="ESTABLISHED"))
    assert not box.ok()


def test_a_listener_on_a_lan_address_is_not_a_loopback_server(box):
    box.listen(box.folder, ip="192.168.1.20")
    assert not box.ok()


def test_refused_for_a_listener_we_cannot_name(box):
    box.listen(box.folder, conn_pid=False)                # psutil gave no pid
    assert not box.ok()


def test_every_listener_on_the_port_must_be_the_projects(box):
    # A specific bind can win over a wildcard one (Windows), so the first match
    # proves nothing: both have to be the project's.
    box.listen(box.folder, ip="127.0.0.1")
    box.listen(box.other, ip="::")
    assert not box.ok()
    box.ps.conns.clear()
    box.listen(box.folder, ip="127.0.0.1")
    box.listen(os.path.join(box.folder, "frontend"), ip="::1")
    assert box.ok()


def test_one_process_on_two_sockets_is_one_listener(box):
    proc = box.listen(box.folder, ip="127.0.0.1")
    box.ps.conns.append(FakeConn(PORT, "::1", proc.pid))
    assert box.ok()


def test_refused_when_psutil_is_missing(box, monkeypatch):
    box.listen(box.folder)
    monkeypatch.setitem(sys.modules, "psutil", None)      # `import psutil` raises
    assert not box.ok()


def test_refused_when_the_socket_table_and_the_process_list_both_fail(box):
    box.listen(box.folder)
    box.ps.system_error = AccessDenied("root only")
    box.ps.iter_error = AccessDenied("no")
    assert not box.ok()


def test_any_unexpected_error_reads_as_no(box, monkeypatch):
    box.listen(box.folder)
    monkeypatch.setattr(A, "_publish_listener_pids",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert not box.ok()


# --------------------------------------------------------------------------- #
# 4. macOS: the system-wide table needs root -> ask each process about itself
# --------------------------------------------------------------------------- #

def test_without_root_each_process_is_asked_about_its_own_sockets(box):
    box.listen(box.folder)
    box.ps.system_error = AccessDenied("needs root")
    assert box.ok()


def test_the_per_process_fallback_is_just_as_strict(box):
    box.listen(box.other)
    box.ps.system_error = AccessDenied("needs root")
    assert not box.ok()
    box.ps.procs.clear()
    assert not box.ok()                                   # nothing visible: no


def test_the_fallback_skips_processes_that_refuse_to_answer(box):
    mine = box.listen(box.folder)
    box.ps.system_error = AccessDenied("needs root")

    class Locked(FakeProc):
        def net_connections(self, kind="inet"):
            raise AccessDenied("not mine")
    box.ps.procs[999] = Locked(999, box.other)
    assert box.ok()
    assert mine.pid in box.ps.procs


# --------------------------------------------------------------------------- #
# 5. the folder is the agent's own argument: too broad = never a project
# --------------------------------------------------------------------------- #

def test_a_filesystem_root_is_never_a_project_folder(box):
    root = os.path.splitdrive(os.getcwd())[0] + os.sep
    box.listen(root)                                      # daemons often live in "/"
    assert not A._publish_port_is_project_server(root, PORT)


def test_the_home_folder_and_everything_above_it_is_never_a_project_folder(box, tmp_path):
    home = tmp_path / "home" / "me"
    home.mkdir(parents=True)
    box.listen(str(home))
    for broad in (home, home.parent, tmp_path):
        assert not A._publish_port_is_project_server(str(broad), PORT), broad
    ok = home / "bakery"
    ok.mkdir()
    box.ps.conns.clear()
    box.listen(str(ok))
    assert A._publish_port_is_project_server(str(ok), PORT)


# --------------------------------------------------------------------------- #
# 6. wired in front of the engine, through the real MCP tool
# --------------------------------------------------------------------------- #

class Engine:
    def __init__(self):
        self.calls = []
        self.tunnels = []
        self.n = 0

    def _t(self, folder, port, state="live"):
        self.n += 1
        return {"id": "t%d" % self.n, "project_dir": folder, "port": port,
                "url": URL if state == "live" else None, "state": state, "error": None,
                "source": "agent", "started_at": 1000, "expires_at": "2026-10-08T12:00:00Z",
                "ttl_seconds": 3600, "remaining_seconds": 3570}

    def status(self, project_dir=None):
        self.calls.append(("status", project_dir))
        rows = [dict(t) for t in self.tunnels
                if project_dir is None or os.path.abspath(t["project_dir"]) == os.path.abspath(project_dir)]
        return {"server_time": 1000, "cloudflared": {"available": True, "installable": True},
                "tunnels": rows,
                "limits": {"default_ttl_minutes": 60, "ttl_choices": [15, 60], "max_tunnels": 3}}

    def start(self, project_dir, port=None, ttl_minutes=None, source="agent"):
        self.calls.append(("start", project_dir, port, ttl_minutes, source))
        t = self._t(project_dir, port)
        self.tunnels.append(t)
        return dict(t)

    def renew(self, tunnel_id, ttl_minutes=None):
        self.calls.append(("renew", tunnel_id, ttl_minutes))
        for t in self.tunnels:
            if t["id"] == tunnel_id:
                return dict(t)
        raise publish.PublishError("not_found", "No such tunnel.")

    def stop(self, tunnel_id):
        self.calls.append(("stop", tunnel_id))
        return {"id": tunnel_id, "state": "stopped"}

    def starts(self):
        return [c for c in self.calls if c[0] == "start"]


@pytest.fixture
def eng(monkeypatch):
    e = Engine()
    monkeypatch.setattr(publish, "default", e)
    monkeypatch.setattr(A, "_PUBLISH_POLL_SECONDS", 0)
    monkeypatch.setattr(A, "_PUBLISH_WAIT_SECONDS", 1)
    monkeypatch.setattr(A, "_PUBLISH_PENDING", {})
    return e


@pytest.fixture
def clock(monkeypatch):
    c = {"t": 5000.0}
    monkeypatch.setattr(A, "_publish_clock", lambda: c["t"])
    return c


def _flags(monkeypatch, **flags):
    real = config.get_flag
    monkeypatch.setattr(config, "get_flag",
                        lambda name, default=False: flags.get(name, real(name, default)))


def tool(name, **args):
    out, _ = hub_mcp.handle_rpc({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                 "params": {"name": name, "arguments": args}})
    assert "error" not in out, out
    res = out["result"]
    return json.loads(res["content"][0]["text"]), bool(res.get("isError"))


REFUSAL = ("Only a server running from the project folder proj can be published. "
           "Start the app from that folder, then ask again.")


def test_a_server_from_somewhere_else_is_refused_before_the_engine_is_touched(
        box, eng, caplog):
    box.listen(box.other)                                 # a database admin on the port
    with caplog.at_level(logging.INFO, logger="free-llm-hub"):
        out, is_err = tool("publish_start", port=PORT, project_dir=box.folder)
    assert is_err
    assert out == {"error": REFUSAL, "code": "not_project_server"}
    assert eng.calls == []                                # not even a status call
    lines = [r.getMessage() for r in caplog.records if "[publish]" in r.getMessage()]
    assert lines == ["[publish] agent start refused (not_project_server) for proj port %d" % PORT]
    assert "http" not in lines[0] and "trycloudflare" not in lines[0]


def test_no_listener_at_all_is_refused_the_same_way(box, eng):
    out, is_err = tool("publish_start", port=PORT, project_dir=box.folder)
    assert is_err and out["code"] == "not_project_server" and eng.calls == []


def test_the_project_server_goes_through_as_before(box, eng):
    box.listen(box.folder)
    out, is_err = tool("publish_start", port=PORT, project_dir=box.folder, ttl_minutes=60)
    assert not is_err and out["url"] == URL and out["state"] == "live"
    assert eng.starts() == [("start", os.path.abspath(box.folder), PORT, 60, "agent")]
    assert A._PUBLISH_PENDING == {}                       # default flow holds nothing


def test_the_hubs_own_preview_goes_through_without_a_folder_argument(box, eng, monkeypatch):
    monkeypatch.setattr(A.workspace, "running", lambda: [
        {"project_dir": box.folder, "port": PORT, "state": "running", "external": False}])
    out, is_err = tool("publish_start", port=PORT)
    assert not is_err and out["url"] == URL and len(eng.starts()) == 1


def test_an_engine_refusal_still_comes_back_after_the_rule_passes(box, eng):
    box.listen(box.folder)
    eng.start = lambda *a, **k: (_ for _ in ()).throw(
        publish.PublishError("too_many", "Too many tunnels."))
    out, is_err = tool("publish_start", port=PORT, project_dir=box.folder)
    assert is_err and out["code"] == "too_many"


def test_renew_status_and_stop_do_not_ask_about_the_port(box, eng):
    folder = os.path.abspath(box.folder)
    eng.tunnels.append(eng._t(folder, PORT))              # started earlier; no listener now
    out, is_err = tool("publish_renew", id="t1")
    assert not is_err and out["url"] == URL
    out, is_err = tool("publish_status", project_dir=folder)
    assert not is_err and out["tunnels"][0]["id"] == "t1"
    out, is_err = tool("publish_stop", id="t1")
    assert not is_err and out["state"] == "stopped"


# --------------------------------------------------------------------------- #
# 7. strict mode: agent_publish_requires_approval
# --------------------------------------------------------------------------- #

NOTE = "Waiting for the user to approve this in the Build page's Publish panel."


def test_strict_mode_is_off_by_default():
    assert A._publish_requires_approval() is False


def test_strict_mode_holds_the_request_and_starts_nothing(box, eng, clock, monkeypatch, caplog):
    _flags(monkeypatch, agent_publish_requires_approval=True)
    box.listen(box.folder)
    with caplog.at_level(logging.INFO, logger="free-llm-hub"):
        out, is_err = tool("publish_start", port=PORT, project_dir=box.folder)
    assert not is_err and out == {"pending": True, "note": NOTE}
    assert eng.starts() == [] and eng.tunnels == []
    assert A._publish_pending_list() == [
        {"project_dir": os.path.abspath(box.folder), "port": PORT, "requested_at": 5000.0}]
    lines = [r.getMessage() for r in caplog.records if "[publish]" in r.getMessage()]
    assert lines == ["[publish] agent start held for approval for proj port %d" % PORT]
    assert "http" not in lines[0] and "trycloudflare" not in lines[0]


def test_strict_mode_still_refuses_a_server_that_is_not_the_projects(box, eng, clock, monkeypatch):
    _flags(monkeypatch, agent_publish_requires_approval=True)
    box.listen(box.other)
    out, is_err = tool("publish_start", port=PORT, project_dir=box.folder)
    assert is_err and out["code"] == "not_project_server"
    assert A._publish_pending_list() == []                # nothing to "approve"


def test_the_same_request_twice_is_one_entry_and_keeps_its_first_time(box, eng, clock, monkeypatch):
    _flags(monkeypatch, agent_publish_requires_approval=True)
    box.listen(box.folder)
    tool("publish_start", port=PORT, project_dir=box.folder)
    clock["t"] += 120
    out, _ = tool("publish_start", port=PORT, project_dir=box.folder)
    assert out["pending"] is True
    rows = A._publish_pending_list()
    assert len(rows) == 1 and rows[0]["requested_at"] == 5000.0


def test_two_ports_of_one_project_are_two_requests(box, eng, clock, monkeypatch):
    _flags(monkeypatch, agent_publish_requires_approval=True)
    box.listen(box.folder)
    box.listen(box.folder, port=PORT + 1)
    tool("publish_start", port=PORT, project_dir=box.folder)
    tool("publish_start", port=PORT + 1, project_dir=box.folder)
    assert sorted(r["port"] for r in A._publish_pending_list()) == [PORT, PORT + 1]
    assert len(A._publish_pending_list(box.folder)) == 2
    assert A._publish_pending_list(box.other) == []


def test_a_request_expires_after_ten_minutes(box, eng, clock, monkeypatch):
    _flags(monkeypatch, agent_publish_requires_approval=True)
    box.listen(box.folder)
    tool("publish_start", port=PORT, project_dir=box.folder)
    clock["t"] += 600
    assert len(A._publish_pending_list()) == 1            # still its last second
    clock["t"] += 1
    assert A._publish_pending_list() == []
    assert A._PUBLISH_PENDING == {}                       # and forgotten, not just hidden
    out, _ = tool("publish_start", port=PORT, project_dir=box.folder)   # asking again is new
    assert out["pending"] is True
    assert A._publish_pending_list()[0]["requested_at"] == 5601.0


def test_the_pending_list_is_bounded(box, eng, clock, monkeypatch):
    for i in range(A._PUBLISH_PENDING_MAX + 5):
        clock["t"] += 1
        A._publish_pending_add(box.folder, 5000 + i)
    rows = A._publish_pending_list()
    assert len(rows) == A._PUBLISH_PENDING_MAX
    assert min(r["port"] for r in rows) == 5005           # the oldest went first


def test_a_tunnel_the_user_already_started_is_returned_not_held(box, eng, clock, monkeypatch):
    _flags(monkeypatch, agent_publish_requires_approval=True)
    box.listen(box.folder)
    eng.tunnels.append(eng._t(os.path.abspath(box.folder), PORT))
    out, is_err = tool("publish_start", port=PORT, project_dir=box.folder)
    assert not is_err and out["url"] == URL and "pending" not in out
    assert A._publish_pending_list() == [] and eng.starts() == []


def test_a_pending_request_disappears_once_a_tunnel_for_it_is_live(box, eng, clock):
    A._publish_pending_add(box.folder, PORT)
    live = eng._t(os.path.abspath(box.folder), PORT)
    assert A._publish_pending_list(tunnels=[]) != []
    assert A._publish_pending_list(tunnels=[live]) == []
    assert A._publish_pending_list(tunnels=[]) == []      # forgotten for good
    A._publish_pending_add(box.folder, PORT)
    dead = dict(live, state="expired")
    assert len(A._publish_pending_list(tunnels=[dead])) == 1   # an expired one is no approval


def test_flag_off_leaves_the_flow_alone_even_with_a_stale_pending_entry(box, eng, clock):
    box.listen(box.folder)
    A._publish_pending_add(box.folder, PORT)
    out, is_err = tool("publish_start", port=PORT, project_dir=box.folder)
    assert not is_err and out["url"] == URL and len(eng.starts()) == 1


def test_an_unreadable_config_reads_as_strict(monkeypatch):
    def boom(*a, **k):
        raise OSError("config gone")
    monkeypatch.setattr(config, "get_flag", boom)
    assert A._publish_requires_approval() is True


# --------------------------------------------------------------------------- #
# 8. the dashboard side: GET /api/publish lists it, the Publish button answers it
# --------------------------------------------------------------------------- #

def _client():
    c = A.app.test_client()
    hdr = {"X-Free-LLM-Hub": "dashboard"}
    token = A.config.get_control_token()
    if token:
        hdr["X-Free-LLM-Hub-Token"] = token
    c._hdr = hdr
    return c


def test_get_publish_lists_what_an_agent_asked_for(eng, clock, tmp_path):
    folder = str(tmp_path / "proj")
    os.makedirs(folder)
    A._publish_pending_add(folder, PORT)
    c = _client()
    body = c.get("/api/publish", headers=c._hdr).get_json()
    assert body["pending_agent_requests"] == [
        {"project_dir": os.path.abspath(folder), "port": PORT, "requested_at": 5000.0}]
    assert "tunnels" in body and "limits" in body         # the old shape is intact
    mine = c.get("/api/publish", query_string={"project_dir": folder}, headers=c._hdr).get_json()
    assert len(mine["pending_agent_requests"]) == 1
    other = c.get("/api/publish", query_string={"project_dir": str(tmp_path / "x")},
                  headers=c._hdr).get_json()
    assert other["pending_agent_requests"] == []


def test_get_publish_has_an_empty_list_when_nothing_is_waiting(eng, clock):
    c = _client()
    assert c.get("/api/publish", headers=c._hdr).get_json()["pending_agent_requests"] == []


def test_pressing_publish_answers_the_request(eng, clock, tmp_path):
    folder = str(tmp_path / "proj")
    os.makedirs(folder)
    A._publish_pending_add(folder, PORT)
    A._publish_pending_add(folder, PORT + 1)
    c = _client()
    r = c.post("/api/publish/start", json={"project_dir": folder, "port": PORT,
                                           "confirm": True}, headers=c._hdr)
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert [p["port"] for p in A._publish_pending_list()] == [PORT + 1]
    # the live tunnel also keeps it out of the next listing, once it expires or not
    body = c.get("/api/publish", headers=c._hdr).get_json()
    assert [p["port"] for p in body["pending_agent_requests"]] == [PORT + 1]


def test_the_dashboard_start_route_does_not_ask_the_port_rule(eng, clock, tmp_path, monkeypatch):
    # A human click is its own consent: no listener needed here.
    monkeypatch.setitem(sys.modules, "psutil", None)
    folder = str(tmp_path / "proj")
    os.makedirs(folder)
    c = _client()
    r = c.post("/api/publish/start", json={"project_dir": folder, "port": PORT,
                                           "confirm": True}, headers=c._hdr)
    assert r.status_code == 200 and len(eng.starts()) == 1
