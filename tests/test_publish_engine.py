"""Publish a project's preview online through a free Cloudflare quick tunnel
(publish.py + the /api/publish* routes).

HERMETIC: no real cloudflared, no Cloudflare, no download, no wall-clock waiting.
  * cloudflared is a tiny python script (FAKE_SCRIPT) that prints log lines with
    a fake https://<words>.trycloudflare.com address and sleeps until its control
    file disappears; the Manager really spawns it, reads its pipe and kills its
    process tree -- only the clock, the HTTP probe, the preview port lookup and
    the downloader are fakes.
  * The clock is injected: expiry and the 25 s retry are driven by Manager.tick()
    after FakeClock.advance(), not by sleeping.
  * tests/conftest.py makes every default effect of publish (spawn, PATH lookup,
    download, probe, timer thread) fail loudly; these tests pass their own through
    the constructor. The real defaults are grabbed below at import (collection)
    time, before any fixture patches them.
"""
import http.client
import http.server
import io
import json
import logging
import os
import re
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request

import psutil
import pytest

import publish
import workspace

P = publish
_REAL_SPAWN = publish._spawn
_REAL_PROBE = publish._http_probe
_REAL_FETCH = publish._https_fetch
_REAL_TIMER = publish.Manager._ensure_timer
_REAL_ITER = publish._iter_processes

FAKE_URL_RE = re.compile(r"^https://fake-[0-9a-f]{8}-quick-test\.trycloudflare\.com$")

FAKE_SCRIPT = r'''
import json, os, subprocess, sys, time, uuid

ctl_path = os.environ["FAKE_CF_CONTROL"]
argv = sys.argv[1:]
if argv[:1] == ["--version"]:
    print("cloudflared version 2099.1.0 (fake)")
    sys.exit(0)
ctl = json.load(open(ctl_path))
try:
    pgid = os.getpgid(0)
except AttributeError:
    pgid = None
# one file per launch: two fakes appending to one file at the same moment can
# overwrite each other on Windows
with open(ctl["log"] + "." + str(os.getpid()), "w") as fh:
    fh.write(json.dumps({
        "t": time.time_ns(), "argv": argv, "pid": os.getpid(), "pgid": pgid,
        "marker": os.environ.get("CALVOUN_TUNNEL"),
        "home": os.environ.get("CALVOUN_TUNNEL_HOME"),
        "tunnel_env": sorted(k for k in os.environ if k.upper().startswith("TUNNEL_")),
        "port_env": os.environ.get("PORT")}) + "\n")
mode = ctl.get("mode", "ok")


def say(text):
    print(text, flush=True)


def new_url():
    return "https://fake-%s-quick-test.trycloudflare.com" % uuid.uuid4().hex[:8]


say("2099-01-01T00:00:00Z INF Thank you for trying Cloudflare Tunnel.")
if ctl.get("child"):
    code = "import os,sys,time\nwhile os.path.exists(sys.argv[1]): time.sleep(0.1)\n"
    kid = subprocess.Popen([sys.executable, "-c", code, ctl_path])
    with open(ctl["child"] + "." + str(os.getpid()), "w") as fh:
        fh.write(str(kid.pid))
if mode == "die_early":
    say("ERR boom")
    sys.exit(3)
if mode == "needs_http2" and "http2" not in argv:
    pass
elif mode == "never":
    pass
elif mode == "api_first":
    say('ERR failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel": dial tcp: i/o timeout')
    say("INF see https://evil.com/?x.trycloudflare.com and https://x.trycloudflare.com.evil.net/ "
        "and https://a.trycloudflare.com@evil.net/ and http://plain.trycloudflare.com")
    if ctl.get("then_url"):
        say("|  " + new_url() + "  |")
else:
    say("INF Requesting new quick Tunnel on trycloudflare.com...")
    say("|  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |")
    say("|  " + new_url() + "                         |")
say("SENTINEL")
while os.path.exists(ctl_path):
    time.sleep(0.1)
'''


class Clock:
    def __init__(self):
        self.m = 1000.0
        self.w = 1_800_000_000.0

    def mono(self):
        return self.m

    def wall(self):
        return self.w

    def advance(self, seconds, mono=True, wall=True):
        if mono:
            self.m += seconds
        if wall:
            self.w += seconds


def alive(pid):
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def wait_until(pred, timeout=20.0, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    raise AssertionError("timed out waiting for %s" % what)


def wait_dead(pid, timeout=10.0):
    wait_until(lambda: not alive(pid), timeout, "process %s to die" % pid)


class Rig:
    def __init__(self, tmp_path, monkeypatch):
        self.dir = tmp_path
        self.script = tmp_path / "fake_cloudflared.py"
        self.script.write_text(FAKE_SCRIPT, encoding="utf-8")
        self.ctl = tmp_path / "ctl.json"
        self.log = tmp_path / "launches.jsonl"
        self.clock = Clock()
        self.managers = []
        self.set()
        monkeypatch.setenv("FAKE_CF_CONTROL", str(self.ctl))

    def set(self, **kw):
        data = {"log": str(self.log), "child": str(self.dir / "child"), "mode": "ok"}
        if self.ctl.exists():
            data.update(json.loads(self.ctl.read_text()))
        data.update(kw)
        self.ctl.write_text(json.dumps(data))

    def manager(self, **over):
        kw = dict(
            locate=lambda: str(self.script),
            launcher=lambda path: [sys.executable, path],
            clock=self.clock.mono, wall=self.clock.wall, spawn=_REAL_SPAWN,
            probe=lambda port: True, preview_port=lambda d: 5801,
            hub_ports=lambda: {8787}, bin_dir=lambda: str(self.dir / "bin"),
            flag=lambda: True, version_of=lambda path: "2099.1.0", timer=False,
            system=lambda: ("linux", "amd64"), procs=lambda: iter(()))
        kw.update(over)
        m = P.Manager(**kw)
        self.managers.append(m)
        return m

    def launches(self):
        rows = []
        for f in self.dir.glob(self.log.name + ".*"):
            try:
                rows.append(json.loads(f.read_text()))
            except ValueError:          # being written at this very moment
                pass
        return sorted(rows, key=lambda r: r["t"])

    def children(self):
        out = []
        for f in self.dir.glob("child.*"):
            try:
                out.append(int(f.read_text()))
            except ValueError:
                pass
        return out

    def row(self, m, tid):
        for r in m.status()["tunnels"]:
            if r["id"] == tid:
                return r
        return None

    def live(self, m, tid, timeout=20.0):
        wait_until(lambda: (self.row(m, tid) or {}).get("state") == "live", timeout,
                   "tunnel %s to go live" % tid)
        return self.row(m, tid)

    def sentinels(self, m, tid):
        return sum(1 for l in m._tunnels[tid].tail if l == "SENTINEL")

    def close(self):
        pids = [l["pid"] for l in self.launches()] + self.children()
        for m in self.managers:
            try:
                m.shutdown()
            except Exception:                                    # noqa: BLE001
                pass
        try:
            self.ctl.unlink()
        except OSError:
            pass
        for pid in pids:                    # only ever OUR fakes, never a recycled pid
            try:
                p = psutil.Process(pid)
                if any("fake_cloudflared.py" in c or "ctl.json" in c for c in p.cmdline()):
                    p.kill()
            except Exception:                                    # noqa: BLE001
                pass


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    yield r
    r.close()


# --------------------------------------------------------------------------- #
# The address: first valid match, strict host
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text,want", [
    ("|  https://abstract-smoking-seeks-ate.trycloudflare.com   |",
     "https://abstract-smoking-seeks-ate.trycloudflare.com"),
    ("Visit it at https://A-B-C.TryCloudflare.com/ now",
     "https://a-b-c.trycloudflare.com"),
    ("see https://one-two.trycloudflare.com. Then https://x-y.trycloudflare.com",
     "https://one-two.trycloudflare.com"),
    ('url="https://q-r-s.trycloudflare.com"', "https://q-r-s.trycloudflare.com"),
    ("https://evil.com/?x.trycloudflare.com", None),
    ("https://x.trycloudflare.com.evil.net/", None),
    ("https://x.trycloudflare.com@evil.net/", None),
    ("https://a.trycloudflare.com:8443/", None),
    ("http://plain.trycloudflare.com", None),
    ("https://sub.domain.trycloudflare.com", None),
    ("https://trycloudflare.com", None),
    ("https://-bad.trycloudflare.com", None),
    # cloudflared's own control endpoint, printed in its failure messages
    ('Post "https://api.trycloudflare.com/tunnel": dial tcp: i/o timeout', None),
    ("https://www.trycloudflare.com", None),
    ('Post "https://api.trycloudflare.com/tunnel" then https://real-one.trycloudflare.com',
     "https://real-one.trycloudflare.com"),
    ("", None), (None, None),
])
def test_parse_tunnel_url(text, want):
    assert P.parse_tunnel_url(text) == want


def test_scrub_removes_every_address():
    s = P.scrub('x https://a-b.trycloudflare.com/p y api.trycloudflare.com z '
                'https://x.trycloudflare.com.evil.net/q')
    assert "trycloudflare" not in s and "<link>" in s


# --------------------------------------------------------------------------- #
# Start: the command, the environment, the contract shape
# --------------------------------------------------------------------------- #

def test_start_goes_live_with_the_exact_command_and_clean_env(rig, monkeypatch):
    monkeypatch.setenv("TUNNEL_TOKEN", "must-not-reach-quick-tunnels")
    monkeypatch.setenv("PORT", "8787")
    m = rig.manager()
    t = m.start("proj-a")
    assert t["state"] in ("starting", "live")
    assert set(t) == {"id", "project_dir", "port", "url", "state", "error", "source",
                      "started_at", "expires_at", "ttl_seconds", "remaining_seconds"}
    assert t["port"] == 5801 and t["source"] == "build" and t["ttl_seconds"] == 3600
    assert t["project_dir"] == os.path.abspath("proj-a")
    row = rig.live(m, t["id"])
    assert FAKE_URL_RE.match(row["url"])
    assert row["remaining_seconds"] == 3600
    assert row["expires_at"] == pytest.approx(rig.clock.w + 3600)
    assert row["started_at"] == rig.clock.w
    (launch,) = rig.launches()
    assert launch["argv"] == ["tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:5801",
                              "--http-host-header", "127.0.0.1:5801"]
    assert launch["marker"] == t["id"]
    assert launch["tunnel_env"] == []                 # a user's TUNNEL_* never leaks in
    assert launch["port_env"] is None                 # the hub's own PORT is stripped
    st = m.status()
    assert st["limits"] == {"default_ttl_minutes": 60, "ttl_choices": [15, 60, 240, 720, 1440],
                            "max_tunnels": 3}
    assert st["server_time"] == rig.clock.w
    cf = st["cloudflared"]
    assert set(cf) >= {"available", "path", "version", "platform", "installable",
                       "installing", "install_error"}
    assert cf["available"] and cf["version"] == "2099.1.0" and cf["platform"] == "linux/amd64"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_cloudflared_gets_its_own_process_group(rig):
    m = rig.manager()
    t = m.start("p")
    rig.live(m, t["id"])
    assert rig.launches()[0]["pgid"] != os.getpgid(0)


def test_source_agent_is_kept(rig):
    m = rig.manager()
    assert m.start("p", source="agent")["source"] == "agent"


def test_status_returns_only_the_tunnels_of_the_project_asked_for(rig, tmp_path):
    m = rig.manager()
    pa, pb = str(tmp_path / "pa"), str(tmp_path / "pb")
    a = m.start(pa, port=5801)
    a2 = m.start(pa, port=5803)
    b = m.start(pb, port=5802)
    ids = lambda proj: sorted(r["id"] for r in m.status(proj)["tunnels"])
    assert ids(pa) == sorted([a["id"], a2["id"]])
    assert ids(pb) == [b["id"]]
    # the same directory spelled differently (trailing separator, ./ and .. parts,
    # and on Windows another letter case) is the same project
    assert ids(pa + os.sep) == ids(pa)
    assert ids(os.path.join(pa, "..", "pa")) == ids(pa)
    assert ids(os.path.join(str(tmp_path), ".", "pb")) == [b["id"]]
    if os.name == "nt":
        assert ids(pa.upper()) == ids(pa)
    assert ids(str(tmp_path / "other")) == []              # a project with none: empty, not everything
    assert len(m.status()["tunnels"]) == 3                 # no filter: all
    assert len(m.status(None)["tunnels"]) == 3
    # start/renew hand back the complete tunnel dict
    keys = {"id", "project_dir", "port", "url", "state", "error", "source", "started_at",
            "expires_at", "ttl_seconds", "remaining_seconds"}
    assert set(a) == keys and set(m.renew(b["id"])) == keys


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #

def code_of(fn, *a, **k):
    with pytest.raises(P.PublishError) as e:
        fn(*a, **k)
    return e.value.code


@pytest.mark.parametrize("bad", [0, 30, 61, 1441, -5, "60", True, 1.5, [60]])
def test_bad_ttl(rig, bad):
    m = rig.manager()
    assert code_of(m.start, "p", ttl_minutes=bad) == "bad_ttl"
    assert rig.launches() == []


def test_every_ttl_choice_is_accepted_and_sets_the_length():
    for c in P.TTL_CHOICES:
        assert P.check_ttl(c) == c
    assert P.check_ttl(None) == 60


def test_ttl_is_applied(rig):
    m = rig.manager()
    t = m.start("p", ttl_minutes=15)
    assert t["ttl_seconds"] == 900
    assert rig.live(m, t["id"])["remaining_seconds"] == 900


@pytest.mark.parametrize("port", [8787, 22, 80, 1023, 3306, 5432, 6379, 27017, 9200,
                                  11211, 2375, 5900, 3389, 445, 139, 65536, 0, -1,
                                  True, "5173", 5173.0, [5173]])
def test_forbidden_ports(rig, port):
    m = rig.manager()
    assert code_of(m.start, "p", port=port) == "forbidden_port"
    assert rig.launches() == []


def test_the_hubs_own_port_is_refused_from_every_source(rig, monkeypatch):
    monkeypatch.setenv("PORT", "9123")
    got = P._hub_ports()
    assert {8787, 9123} <= got
    m = rig.manager(hub_ports=P._hub_ports)
    assert code_of(m.start, "p", port=9123) == "forbidden_port"
    # ... also when the preview lookup is what returns it
    m2 = rig.manager(hub_ports=P._hub_ports, preview_port=lambda d: 9123)
    assert code_of(m2.start, "p") == "forbidden_port"


def test_no_preview(rig):
    m = rig.manager(preview_port=lambda d: None)
    assert code_of(m.start, "p") == "no_preview"
    assert code_of(m.start, "") == "no_preview"
    assert code_of(m.start, None) == "no_preview"
    assert code_of(m.start, 5) == "no_preview"
    # an explicit port needs no preview
    assert m.start("p", port=5805)["port"] == 5805


def test_the_preview_port_comes_from_workspace_status(monkeypatch):
    seen = []

    def fake_status(project_dir):
        seen.append(project_dir)
        return {"running": True, "port": 5812, "state": "running"}
    monkeypatch.setattr(workspace, "status", fake_status)
    assert P._preview_port("/some/proj") == 5812
    monkeypatch.setattr(workspace, "status", lambda d: {"running": False, "port": 5812})
    assert P._preview_port("/some/proj") is None          # installing / failed: no port yet
    monkeypatch.setattr(workspace, "status", lambda d: {"running": True, "port": None})
    assert P._preview_port("/some/proj") is None


def test_not_http(rig):
    m = rig.manager(probe=lambda port: False)
    assert code_of(m.start, "p") == "not_http"
    assert rig.launches() == []


def test_no_cloudflared(rig):
    m = rig.manager(locate=lambda: None)
    assert code_of(m.start, "p") == "no_cloudflared"
    assert m.status()["cloudflared"]["available"] is False


def test_a_missing_binary_is_a_clean_refusal_and_leaves_no_row(rig):
    m = rig.manager(spawn=lambda argv, env: (_ for _ in ()).throw(FileNotFoundError(2, "nope")))
    assert code_of(m.start, "p") == "no_cloudflared"
    assert m.status()["tunnels"] == []


def test_at_most_three_online_and_dead_rows_do_not_count(rig):
    m = rig.manager()
    a = m.start("p", port=5801)
    m.start("p", port=5802)
    m.start("p", port=5803)
    assert code_of(m.start, "p", port=5804) == "too_many"
    m.stop(a["id"])                                    # frees a slot; the row stays
    d = m.start("p", port=5804)
    assert d["state"] in ("starting", "live")
    assert len(m.status()["tunnels"]) == 4             # three online + the stopped row
    assert code_of(m.start, "q", port=5805) == "too_many"


def test_already_published_returns_the_same_tunnel(rig):
    m = rig.manager()
    a = m.start("p")
    ra = rig.live(m, a["id"])
    b = m.start("p")
    assert b["id"] == a["id"] and b["url"] == ra["url"]
    assert m.start("p", port=5801, ttl_minutes=15)["id"] == a["id"]   # ttl of a duplicate: ignored
    assert len(rig.launches()) == 1
    assert len(m.status()["tunnels"]) == 1
    # a different port of the same project is a different tunnel
    assert m.start("p", port=5802)["id"] != a["id"]


def test_flag_off_refuses_start_renew_install_but_never_stop_or_status(rig):
    on = {"v": True}
    m = rig.manager(flag=lambda: on["v"])
    t = m.start("p")
    rig.live(m, t["id"])
    on["v"] = False
    assert code_of(m.start, "p", port=5803) == "disabled"
    assert code_of(m.renew, t["id"]) == "disabled"
    assert code_of(m.install) == "disabled"
    st = m.status()
    assert st["enabled"] is False and st["cloudflared"]["installable"] is False
    assert m.stop(t["id"])["state"] == "stopped"


def test_the_real_flag_defaults_on_and_follows_the_config():
    import config
    assert P._flag_on() is True
    config.set_flag("publish_enabled", False)
    try:
        assert P._flag_on() is False
    finally:
        config.set_flag("publish_enabled", True)


# --------------------------------------------------------------------------- #
# Time: expiry, renew, stop, the HTTP/2 retry, failures
# --------------------------------------------------------------------------- #

def test_expiry_kills_the_whole_process_tree_and_keeps_the_row(rig):
    m = rig.manager()
    t = m.start("p")
    rig.live(m, t["id"])
    wait_until(lambda: rig.children(), what="the fake's child process")
    pid, kid = rig.launches()[0]["pid"], rig.children()[0]
    assert alive(pid) and alive(kid)
    rig.clock.advance(3599)
    m.tick()
    row = rig.row(m, t["id"])
    assert row["state"] == "live" and row["remaining_seconds"] == 1
    rig.clock.advance(2)
    m.tick()
    row = rig.row(m, t["id"])
    assert row["state"] == "expired" and row["remaining_seconds"] == 0
    assert row["url"] is None and row["error"] is None
    wait_dead(pid)
    wait_dead(kid)
    assert len(m.status()["tunnels"]) == 1             # still visible: the UI offers a new link


def test_expiry_follows_the_earlier_of_the_monotonic_and_the_wall_clock(rig):
    m = rig.manager()
    t = m.start("p", port=5801)
    u = m.start("p", port=5802)
    rig.live(m, t["id"])
    rig.live(m, u["id"])
    rig.clock.advance(3601, mono=False)                # the wall clock jumped forward
    m.tick()
    assert rig.row(m, t["id"])["state"] == "expired"
    assert rig.row(m, u["id"])["state"] == "expired"
    v = m.start("p", port=5803)
    rig.live(m, v["id"])
    rig.clock.advance(3601, wall=False)                # monotonic only (a wall clock set back)
    m.tick()
    assert rig.row(m, v["id"])["state"] == "expired"


def test_renew_gives_a_new_address_and_a_fresh_ttl(rig):
    m = rig.manager()
    a = m.start("p", ttl_minutes=15)
    ra = rig.live(m, a["id"])
    old_pid = rig.launches()[0]["pid"]
    rig.clock.advance(100)
    b = m.renew(a["id"])
    assert b["id"] != a["id"] and b["ttl_seconds"] == 900        # same TTL unless asked
    rb = rig.live(m, b["id"])
    assert rb["url"] != ra["url"] and FAKE_URL_RE.match(rb["url"])
    assert rb["remaining_seconds"] == 900 and rb["port"] == 5801
    assert [r["id"] for r in m.status()["tunnels"]] == [b["id"]]  # the old row is replaced
    wait_dead(old_pid)
    assert code_of(m.renew, a["id"]) == "not_found"
    c = m.renew(b["id"], ttl_minutes=240)
    assert c["ttl_seconds"] == 14400 and rig.live(m, c["id"])["url"] != rb["url"]
    assert code_of(m.renew, c["id"], ttl_minutes=7) == "bad_ttl"
    assert rig.row(m, c["id"])["state"] == "live"      # a refused renew leaves the tunnel alone


def test_renew_an_expired_row(rig):
    m = rig.manager()
    a = m.start("p")
    rig.live(m, a["id"])
    rig.clock.advance(3601)
    m.tick()
    assert rig.row(m, a["id"])["state"] == "expired"
    b = m.renew(a["id"])
    rig.live(m, b["id"])
    assert [r["id"] for r in m.status()["tunnels"]] == [b["id"]]


def test_renew_does_not_count_itself_against_the_limit(rig):
    m = rig.manager()
    ids = [m.start("p", port=5801 + i)["id"] for i in range(3)]
    new = m.renew(ids[0])
    assert new["id"] != ids[0]
    assert len(m.status()["tunnels"]) == 3


def test_stop_then_dismiss(rig):
    m = rig.manager()
    t = m.start("p")
    rig.live(m, t["id"])
    pid = rig.launches()[0]["pid"]
    s = m.stop(t["id"])
    assert s["state"] == "stopped" and s["url"] is None and s["remaining_seconds"] == 0
    wait_dead(pid)
    assert rig.row(m, t["id"])["state"] == "stopped"
    assert m.stop(t["id"])["state"] == "stopped"       # second stop dismisses the row
    assert m.status()["tunnels"] == []
    assert code_of(m.stop, t["id"]) == "not_found"
    assert code_of(m.stop, "nope") == "not_found"


def test_http2_retry_after_the_address_does_not_come(rig):
    rig.set(mode="needs_http2")
    m = rig.manager()
    t = m.start("p")
    wait_until(lambda: rig.sentinels(m, t["id"]) == 1, what="the first attempt's output")
    first = rig.launches()[0]
    assert "--protocol" not in first["argv"]
    assert rig.row(m, t["id"])["state"] == "starting"
    rig.clock.advance(24)
    m.tick()
    assert len(rig.launches()) == 1                    # not yet: 25 s have not passed
    rig.clock.advance(2)
    m.tick()
    row = rig.live(m, t["id"])                         # the HTTP/2 attempt answers
    second = rig.launches()[1]
    assert second["argv"][-2:] == ["--protocol", "http2"]
    assert second["argv"][:6] == first["argv"][:6]
    wait_dead(first["pid"])                            # the stale attempt was killed
    assert row["error"] is None and FAKE_URL_RE.match(row["url"])
    assert row["remaining_seconds"] == 3600            # the countdown starts at "live"


def test_failed_after_both_attempts_with_a_plain_reason(rig):
    rig.set(mode="never")
    m = rig.manager()
    t = m.start("p")
    wait_until(lambda: rig.sentinels(m, t["id"]) == 1)
    rig.clock.advance(26)
    m.tick()
    wait_until(lambda: len(rig.launches()) == 2 and rig.sentinels(m, t["id"]) == 2,
               what="the HTTP/2 attempt")
    rig.clock.advance(26)
    m.tick()
    row = rig.row(m, t["id"])
    assert row["state"] == "failed" and row["url"] is None
    assert "HTTP/2" in row["error"] and "trycloudflare" not in row["error"]
    assert "INF" not in row["error"] and "SENTINEL" not in row["error"]   # never a log line
    for l in rig.launches():
        wait_dead(l["pid"])
    # a failed row frees its slot
    assert m.start("p", port=5802)["state"] in ("starting", "live")


def test_a_process_that_dies_at_once_is_retried_once_then_failed(rig):
    rig.set(mode="die_early", child=None)
    m = rig.manager()
    t = m.start("p")
    wait_until(lambda: (rig.row(m, t["id"]) or {}).get("state") == "failed", what="failure")
    assert len(rig.launches()) == 2 and rig.launches()[1]["argv"][-1] == "http2"
    assert "HTTP/2" in rig.row(m, t["id"])["error"]


def test_a_live_tunnel_whose_process_dies_becomes_failed(rig):
    rig.set(child=None)
    m = rig.manager()
    t = m.start("p")
    rig.live(m, t["id"])
    psutil.Process(rig.launches()[0]["pid"]).kill()
    wait_until(lambda: (rig.row(m, t["id"]) or {}).get("state") == "failed", what="failure")
    row = rig.row(m, t["id"])
    assert "stopped unexpectedly" in row["error"] and row["url"] is None
    assert code_of(m.renew, "nope") == "not_found"
    assert m.renew(t["id"])["state"] in ("starting", "live")     # "New link" works from a failed row


def test_tick_notices_a_dead_process_even_when_its_pipe_is_held_open(rig):
    # the fake's child inherits the pipe, so the reader sees no EOF; poll() does
    m = rig.manager()
    t = m.start("p")
    rig.live(m, t["id"])
    wait_until(lambda: rig.children(), what="the fake's child")
    psutil.Process(rig.launches()[0]["pid"]).kill()
    wait_until(lambda: (m.tick() or True) and rig.row(m, t["id"])["state"] == "failed",
               what="tick to notice")
    assert "stopped unexpectedly" in rig.row(m, t["id"])["error"]
    if os.name != "nt":      # POSIX kills the whole group; Windows cannot walk a dead parent's tree
        wait_dead(rig.children()[0])


def test_lookalike_and_api_addresses_never_make_a_tunnel_live(rig):
    rig.set(mode="api_first", then_url=False)
    m = rig.manager()
    t = m.start("p")
    wait_until(lambda: rig.sentinels(m, t["id"]) == 1)
    row = rig.row(m, t["id"])
    assert row["state"] == "starting" and row["url"] is None
    assert not any("trycloudflare" in l for l in m._tunnels[t["id"]].tail)   # scrubbed tail


def test_the_real_address_after_error_lines_is_taken(rig):
    rig.set(mode="api_first", then_url=True)
    m = rig.manager()
    t = m.start("p")
    row = rig.live(m, t["id"])
    assert FAKE_URL_RE.match(row["url"])


def test_shutdown_stops_everything(rig):
    m = rig.manager()
    ids = [m.start("p", port=5801 + i)["id"] for i in range(2)]
    for i in ids:
        rig.live(m, i)
    pids = [l["pid"] for l in rig.launches()]
    m.shutdown()
    for pid in pids:
        wait_dead(pid)
    assert {r["state"] for r in m.status()["tunnels"]} == {"stopped"}


def test_one_timer_thread_for_any_number_of_tunnels(rig, monkeypatch):
    monkeypatch.setattr(P.Manager, "_ensure_timer", _REAL_TIMER)
    m = rig.manager(timer=True, tick_seconds=0.05)
    a = m.start("p", port=5801)
    b = m.start("p", port=5802)
    rig.live(m, a["id"])
    rig.live(m, b["id"])
    timers = [t for t in threading.enumerate() if t.name == "publish-timer"]
    assert len(timers) == 1 and timers[0].daemon
    rig.clock.advance(3601)                            # the timer thread, not us, expires them
    wait_until(lambda: {r["state"] for r in m.status()["tunnels"]} == {"expired"},
               what="the timer to expire both")
    m.shutdown()
    assert not timers[0].is_alive()


def test_the_test_suite_cannot_start_a_real_cloudflared_or_a_timer():
    with pytest.raises(AssertionError):
        P._spawn(["cloudflared"], {})
    assert P._find_cloudflared() is None
    before = [t for t in threading.enumerate() if t.name == "publish-timer"]
    P.default._ensure_timer()
    assert [t for t in threading.enumerate() if t.name == "publish-timer"] == before
    assert code_of(P.default.start, "p", port=5801) == "no_cloudflared"
    with pytest.raises(AssertionError):
        P._https_fetch("https://github.com/x", lambda c: None, 10)


# --------------------------------------------------------------------------- #
# The preview and the tunnel live and die together
# --------------------------------------------------------------------------- #

def test_stopping_the_preview_stops_its_tunnel(rig, monkeypatch, tmp_path):
    m = rig.manager()
    monkeypatch.setattr(P, "default", m)
    proj = str(tmp_path / "proj")
    t = m.start(proj, port=5801)
    other = m.start(str(tmp_path / "other"), port=5802)
    rig.live(m, t["id"])
    rig.live(m, other["id"])
    pid = next(l["pid"] for l in rig.launches() if l["marker"] == t["id"])
    workspace.stop(proj)
    row = rig.row(m, t["id"])
    assert row["state"] == "stopped" and "preview" in row["error"]
    wait_dead(pid)
    assert rig.row(m, other["id"])["state"] == "live"  # another project's tunnel is untouched


def test_the_idle_reaper_leaves_a_published_preview_alone(rig, monkeypatch, tmp_path):
    m = rig.manager()
    monkeypatch.setattr(P, "default", m)
    proj = os.path.abspath(str(tmp_path / "proj"))
    proc = workspace._Proc(proj, 5801, "static")
    proc.touched_at = 0
    monkeypatch.setitem(workspace._procs, proj, proc)
    t = m.start(proj, port=5801)
    rig.live(m, t["id"])
    assert workspace.reap_idle(now=workspace.IDLE_TIMEOUT * 10) == []
    assert proj in workspace._procs and rig.row(m, t["id"])["state"] == "live"
    m.stop(t["id"])
    assert workspace.reap_idle(now=workspace.IDLE_TIMEOUT * 10) == [proj]


# --------------------------------------------------------------------------- #
# The address is a capability: never in logs, errors or output
# --------------------------------------------------------------------------- #

def test_the_public_url_never_reaches_the_log(rig, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    m = rig.manager()
    t = m.start("secretproj")
    r1 = rig.live(m, t["id"])
    rig.clock.advance(3601)
    m.tick()
    u = m.renew(t["id"])
    r2 = rig.live(m, u["id"])
    m.stop(u["id"])
    rig.set(mode="never")
    v = m.start("secretproj", port=5809)
    wait_until(lambda: rig.sentinels(m, v["id"]) == 1)
    rig.clock.advance(60)
    m.tick()
    wait_until(lambda: rig.sentinels(m, v["id"]) == 2)
    rig.clock.advance(60)
    m.tick()
    assert rig.row(m, v["id"])["state"] == "failed"
    out = capsys.readouterr()
    for blob in (caplog.text, out.out, out.err):
        assert "trycloudflare" not in blob
        assert r1["url"] not in blob and r2["url"] not in blob
    assert "[publish]" in caplog.text and "secretproj" in caplog.text
    for row in m.status()["tunnels"]:
        assert "trycloudflare" not in (row["error"] or "")


# --------------------------------------------------------------------------- #
# Boot sweep: only cloudflared carrying the marker
# --------------------------------------------------------------------------- #

class _FakeProc:
    def __init__(self, pid, name, env, cmd, kids=()):
        self.pid, self._name, self._env, self._cmd, self._kids = pid, name, env, cmd, list(kids)
        self.terminated = self.killed = False

    def name(self):
        return self._name

    def environ(self):
        return self._env

    def cmdline(self):
        return self._cmd

    def children(self, recursive=False):
        return self._kids

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def is_running(self):
        return False


def test_sweep_touches_only_marked_cloudflared(monkeypatch):
    monkeypatch.setattr(P, "_hub_pids", lambda: {7})
    M = {P.MARKER: "abc123"}
    kid = _FakeProc(101, "helper.exe", M, ["helper"])
    stranger_kid = _FakeProc(102, "helper.exe", {}, ["helper"])
    procs = [
        _FakeProc(1, "cloudflared.exe", M, ["cloudflared", "tunnel"], kids=[kid, stranger_kid]),
        _FakeProc(2, "python.exe", M, ["python", "app.py"]),                 # marker, not cloudflared
        _FakeProc(3, "cloudflared", {}, ["cloudflared", "tunnel", "run"]),   # the owner's own tunnel
        _FakeProc(4, "cloudflared", {"CALVOUN_PREVIEW": "1"}, ["cloudflared"]),
        _FakeProc(5, "cloudflared-linux-amd64", M, ["/x/cloudflared-linux-amd64"]),
        _FakeProc(7, "cloudflared", M, ["cloudflared"]),                     # a hub pid: never
        _FakeProc(8, "chrome.exe", {}, ["chrome"]),
    ]
    m = P.Manager(procs=lambda: iter(procs), timer=False)
    got = m.sweep_leftovers()
    assert [g["pid"] for g in got] == [1, 5]
    assert [p.terminated for p in procs] == [True, False, False, False, True, False, False]
    assert kid.terminated and not stranger_kid.terminated


def test_sweep_never_touches_another_hubs_tunnels(monkeypatch):
    monkeypatch.setattr(P, "_hub_pids", lambda: set())
    mine = {P.MARKER: "a", P.MARKER_HOME: "/state/real"}
    other = {P.MARKER: "b", P.MARKER_HOME: "/state/sandbox"}
    old = {P.MARKER: "c"}                                    # before the stamp existed: ours
    procs = [_FakeProc(1, "cloudflared", mine, ["cloudflared"]),
             _FakeProc(2, "cloudflared", other, ["cloudflared"]),
             _FakeProc(3, "cloudflared", old, ["cloudflared"])]
    m = P.Manager(procs=lambda: iter(procs), home=lambda: "/state/real", timer=False)
    assert [g["pid"] for g in m.sweep_leftovers()] == [1, 3]
    assert [p.terminated for p in procs] == [True, False, True]
    # and a hub that cannot name itself sweeps by the marker alone (the contract)
    procs2 = [_FakeProc(4, "cloudflared", other, ["cloudflared"])]
    m2 = P.Manager(procs=lambda: iter(procs2), home=lambda: "", timer=False)
    assert [g["pid"] for g in m2.sweep_leftovers()] == [4]


def test_the_default_process_test():
    f = P.looks_like_cloudflared
    assert f("cloudflared") and f("cloudflared.exe") and f("Cloudflared.EXE")
    assert f("cloudflared-linux-amd64") and f("x", ["/usr/bin/cloudflared"])
    assert not f("python.exe", ["python", "x.py"]) and not f("cloudflare-warp")
    assert not f("node", ["node", "cloudflared.js"]) and not f("", [])


def test_sweep_with_real_processes(rig, tmp_path):
    match = lambda name, cmd: any("fake_cloudflared.py" in str(c) for c in cmd)
    owner = rig.manager(procs=_REAL_ITER, proc_match=match)
    t = owner.start("p")
    rig.live(owner, t["id"])
    marked_pid = rig.launches()[0]["pid"]
    sleeper = "import time\nwhile True: time.sleep(1)\n"
    decoy = subprocess.Popen([sys.executable, "-c", sleeper])
    env = dict(os.environ, CALVOUN_PREVIEW="1")
    preview_decoy = subprocess.Popen([sys.executable, "-c", sleeper], env=env)
    env2 = dict(os.environ, **{P.MARKER: "zzz"})
    wrong_name = subprocess.Popen([sys.executable, "-c", sleeper], env=env2)
    try:
        # the Manager that owns the tunnel never sweeps its own process
        assert owner.sweep_leftovers() == []
        assert alive(marked_pid)
        # a fresh Manager (the next boot) does -- and only that process
        boot = P.Manager(procs=_REAL_ITER, proc_match=match, timer=False)
        swept = boot.sweep_leftovers()
        assert marked_pid in [s["pid"] for s in swept]
        wait_dead(marked_pid)
        for p in (decoy, preview_decoy, wrong_name):
            assert p.poll() is None and alive(p.pid)
    finally:
        for p in (decoy, preview_decoy, wrong_name):
            p.kill()
            p.wait()


def test_a_second_hub_does_not_sweep_the_first_hubs_live_tunnel(rig):
    match = lambda name, cmd: any("fake_cloudflared.py" in str(c) for c in cmd)
    real = rig.manager(home=lambda: "/state/real")
    sandbox = rig.manager(home=lambda: "/state/sandbox", bin_dir=lambda: str(rig.dir / "bin2"))
    a = real.start("p", port=5801)
    b = sandbox.start("p", port=5802)
    rig.live(real, a["id"])
    rig.live(sandbox, b["id"])
    pids = {l["marker"]: l["pid"] for l in rig.launches()}
    boot_of_sandbox = rig.manager(home=lambda: "/state/sandbox", procs=_REAL_ITER, proc_match=match)
    # the sandbox hub boots: its own tunnel's process dies, the real hub's lives
    swept = [s["pid"] for s in boot_of_sandbox.sweep_leftovers()]
    assert pids[b["id"]] in swept and pids[a["id"]] not in swept
    wait_dead(pids[b["id"]])
    assert alive(pids[a["id"]]) and rig.row(real, a["id"])["state"] == "live"
    stamps = {l["marker"]: l["home"] for l in rig.launches()}
    assert stamps == {a["id"]: "/state/real", b["id"]: "/state/sandbox"}


def test_the_boot_calls_the_sweep_and_imports_publish():
    src = open("app.py", encoding="utf-8").read()
    boot = src.index("    _mark_runtime_started()\n")
    stale = src.index("agent_servers.stop_stale_agent_clis()", boot)
    sweep = src.index("publish.default.sweep_leftovers()", boot)
    assert stale < sweep < src.index("_print_banner()", boot)
    import app
    assert app.publish is P
    assert "publish.default" in src[src.index("def api_publish_start"):][:900]


# --------------------------------------------------------------------------- #
# The real HTTP probe (loopback only)
# --------------------------------------------------------------------------- #

def _serve(status):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_the_http_probe_accepts_any_status_line_and_rejects_non_http():
    for status in (200, 404, 503):
        srv = _serve(status)
        try:
            assert _REAL_PROBE(srv.server_address[1]) is True
        finally:
            srv.shutdown()
            srv.server_close()
    raw = socket.socket()
    raw.bind(("127.0.0.1", 0))
    raw.listen(1)

    def garbage():
        c, _ = raw.accept()
        c.recv(100)
        c.sendall(b"+PONG not http\r\n")
        c.close()
    threading.Thread(target=garbage, daemon=True).start()
    assert _REAL_PROBE(raw.getsockname()[1], timeout=2) is False
    raw.close()
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()
    assert _REAL_PROBE(port, timeout=1) is False


# --------------------------------------------------------------------------- #
# Install: allowlist, checksum, archive safety, platforms (a fake downloader)
# --------------------------------------------------------------------------- #

import hashlib


def sha(data):
    return hashlib.sha256(data).hexdigest()


def tgz(members):
    """members: [(name, bytes | None, tarinfo type)]"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
            else:
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                tf.addfile(info)
    return buf.getvalue()


class Net:
    """The fake downloader: serves a release document and files by URL."""

    def __init__(self, asset, payload, tag="2099.1.0", digest="good", body="", url=None):
        self.asset, self.payload = asset, payload
        self.url = url or "https://github.com/cloudflare/cloudflared/releases/download/%s/%s" % (tag, asset)
        row = {"name": asset, "browser_download_url": self.url}
        if digest == "good":
            row["digest"] = "sha256:" + sha(payload)
        elif digest == "bad":
            row["digest"] = "sha256:" + sha(b"something else")
        self.meta = {"tag_name": tag, "body": body, "assets": [
            {"name": asset + ".deb", "browser_download_url": self.url + ".deb",
             "digest": "sha256:" + sha(b"deb")}, row]}
        self.calls = []
        self.gate = None

    def __call__(self, url, sink, max_bytes, on_progress=None, hosts=None):
        self.calls.append((url, frozenset(hosts or ())))
        data = json.dumps(self.meta).encode() if url == P.RELEASE_API else self.payload
        if url != P.RELEASE_API and self.gate:
            self.gate.wait(10)
        for i in range(0, len(data), 5):
            sink(data[i:i + 5])
            if on_progress:
                on_progress(min(len(data), i + 5), len(data))
        return len(data)


def installer(rig, net, system=("linux", "amd64"), **over):
    bindir = rig.dir / "bin"
    exe = bindir / P._exe_name()
    kw = dict(fetch=net, system=lambda: system, bin_dir=lambda: str(bindir),
              locate=lambda: str(exe) if exe.exists() else None,
              spawn=lambda *a: (_ for _ in ()).throw(AssertionError("install must run nothing")),
              version_of=lambda p: (_ for _ in ()).throw(AssertionError("install must run nothing")))
    kw.update(over)
    m = rig.manager(**kw)
    return m, bindir, exe


def run_install(m):
    st = m.install()
    th = m._install["thread"]
    th.join(15)
    assert not th.is_alive()
    return st, m.status()["cloudflared"]


def leftovers(bindir):
    return [f.name for f in bindir.iterdir() if f.name.startswith(".")] if bindir.exists() else []


def test_install_verifies_the_digest_and_runs_nothing(rig):
    payload = b"\x7fELF fake cloudflared binary " * 50
    net = Net("cloudflared-linux-amd64", payload)
    m, bindir, exe = installer(rig, net)
    st0 = m.status()["cloudflared"]
    assert st0["available"] is False and st0["installable"] is True
    _, done = run_install(m)
    assert done["available"] and done["path"] == str(exe) and done["install_error"] is None
    assert done["installing"] is False
    assert done["version"] == "2099.1.0"               # the tag recorded at install, nothing was run
    assert exe.read_bytes() == payload
    if os.name != "nt":
        assert os.stat(exe).st_mode & 0o111
    assert leftovers(bindir) == []
    assert net.calls[0] == (P.RELEASE_API, frozenset({"api.github.com"}))
    assert net.calls[1] == (net.url, P.DOWNLOAD_HOSTS)
    assert len(net.calls) == 2
    # installed already: pressing it again does nothing
    m.install()
    assert len(net.calls) == 2


def test_install_progress_shows_in_status(rig):
    net = Net("cloudflared-linux-amd64", b"x" * 500)
    net.gate = threading.Event()
    m, bindir, exe = installer(rig, net)
    m.install()
    wait_until(lambda: m.status()["cloudflared"]["installing"], what="installing")
    assert m.status()["cloudflared"]["install_stage"] in ("starting", "downloading", "looking up the latest release")
    assert m.install()["cloudflared"]["installing"]     # a second press joins, it does not restart
    net.gate.set()
    m._install["thread"].join(10)
    assert m.status()["cloudflared"]["available"]
    assert len([c for c in net.calls if c[0] == net.url]) == 1


def test_install_windows_asset(rig):
    net = Net("cloudflared-windows-amd64.exe", b"MZ" + b"x" * 100)
    m, bindir, exe = installer(rig, net, system=("windows", "amd64"))
    _, done = run_install(m)
    assert done["available"] and exe.read_bytes().startswith(b"MZ")


def test_install_takes_the_digest_from_the_release_notes_when_the_asset_has_none(rig):
    payload = b"binary" * 40
    body = ("SHA256 Checksums:\n"
            "cloudflared-linux-amd64.deb: " + sha(b"deb") + "\n"
            "cloudflared-linux-amd64: " + sha(payload) + "\n")
    net = Net("cloudflared-linux-amd64", payload, digest="none", body=body)
    m, bindir, exe = installer(rig, net)
    _, done = run_install(m)
    assert done["available"] and done["install_error"] is None


@pytest.mark.parametrize("digest,body,why", [
    ("none", "", "checksum"),                                                  # nothing published
    ("none", "cloudflared-linux-amd64.deb: " + "a" * 64, "checksum"),          # only another file's
    ("bad", "", "does not match"),                                             # mismatch
    ("good", "cloudflared-linux-amd64: " + "b" * 64, "two different"),         # sources disagree
    ("none", "cloudflared-linux-amd64: " + "c" * 64 + " " + "d" * 64, "checksum"),  # ambiguous
])
def test_install_refuses_without_a_verified_checksum_and_deletes_a_mismatch(rig, digest, body, why):
    payload = b"binary" * 40
    net = Net("cloudflared-linux-amd64", payload, digest=digest, body=body)
    m, bindir, exe = installer(rig, net)
    _, done = run_install(m)
    assert done["available"] is False and not exe.exists()
    assert why in done["install_error"]
    assert leftovers(bindir) == []                      # the mismatching file was deleted
    assert done["installing"] is False


@pytest.mark.parametrize("url", [
    "https://evil.example/cloudflare/cloudflared/releases/download/1/cloudflared-linux-amd64",
    "http://github.com/cloudflare/cloudflared/releases/download/1/cloudflared-linux-amd64",
    "https://github.com.evil.net/cloudflare/cloudflared/releases/download/1/cloudflared-linux-amd64",
    "https://user@github.com/cloudflare/cloudflared/releases/download/1/cloudflared-linux-amd64",
    "https://github.com:8443/cloudflare/cloudflared/releases/download/1/cloudflared-linux-amd64",
    "https://github.com/someone-else/repo/releases/download/1/cloudflared-linux-amd64",
    "https://api.github.com/cloudflare/cloudflared/releases/download/1/cloudflared-linux-amd64",
    None,
])
def test_install_refuses_a_download_host_outside_the_allowlist(rig, url):
    net = Net("cloudflared-linux-amd64", b"bin", url=url or "x")
    if url is None:
        net.meta["assets"][-1]["browser_download_url"] = None
    m, bindir, exe = installer(rig, net)
    _, done = run_install(m)
    assert not done["available"] and done["install_error"]
    assert [c[0] for c in net.calls] == [P.RELEASE_API]  # the file itself was never requested


def test_the_official_download_hosts():
    assert P.DOWNLOAD_HOSTS == {"github.com", "objects.githubusercontent.com",
                                "release-assets.githubusercontent.com"}
    assert P.METADATA_HOSTS == {"api.github.com"}


def test_install_macos_tgz(rig):
    binary = b"\xcf\xfa\xed\xfe fake mach-o " * 30
    archive = tgz([("README", b"hi", "file"), ("cloudflared", binary, "file")])
    net = Net("cloudflared-darwin-arm64.tgz", archive)
    m, bindir, exe = installer(rig, net, system=("darwin", "arm64"))
    _, done = run_install(m)
    assert done["available"] and exe.read_bytes() == binary      # the member, not the archive
    assert leftovers(bindir) == []


@pytest.mark.parametrize("members", [
    [("../../evil", b"x", "file"), ("cloudflared", b"ok", "file")],
    [("/abs/path", b"x", "file"), ("cloudflared", b"ok", "file")],
    [("sub\\..\\evil", b"x", "file"), ("cloudflared", b"ok", "file")],
    [("a/../../b", b"x", "file"), ("cloudflared", b"ok", "file")],
    [("cloudflared", None, "symlink")],
    [("README", b"no binary here", "file")],
    [("cloudflared", b"one", "file"), ("./cloudflared", b"two", "file")],
])
def test_install_refuses_a_hostile_or_wrong_archive(rig, members):
    net = Net("cloudflared-darwin-amd64.tgz", tgz(members))
    m, bindir, exe = installer(rig, net, system=("darwin", "x86_64"))
    _, done = run_install(m)
    assert not done["available"] and done["install_error"]
    assert not exe.exists() and leftovers(bindir) == []
    assert not (rig.dir / "evil").exists() and not (rig.dir.parent / "evil").exists()
    assert not os.path.exists("/abs/path") if os.name != "nt" else True


@pytest.mark.parametrize("system,hint", [
    (("linux", "riscv64"), "package"), (("windows", "arm64"), "winget"),
    (("darwin", "386"), "brew"), (("freebsd", "amd64"), "https://developers.cloudflare.com"),
])
def test_unsupported_platform_says_how_to_install_by_hand(rig, system, hint):
    net = Net("cloudflared-linux-amd64", b"x")
    m, bindir, exe = installer(rig, net, system=system)
    cf = m.status()["cloudflared"]
    assert cf["installable"] is False and cf["available"] is False
    assert "cloudflared yourself" in cf["install_error"] and hint in cf["install_error"]
    assert cf["platform"] == "%s/%s" % system
    assert code_of(m.install) == "install_failed"
    assert net.calls == []


def test_platform_mapping():
    a = P.asset_for
    assert a("Windows", "AMD64") == "cloudflared-windows-amd64.exe"
    assert a("Linux", "x86_64") == "cloudflared-linux-amd64"
    assert a("Linux", "aarch64") == "cloudflared-linux-arm64"
    assert a("Linux", "arm64") == "cloudflared-linux-arm64"
    assert a("Darwin", "x86_64") == "cloudflared-darwin-amd64.tgz"
    assert a("Darwin", "arm64") == "cloudflared-darwin-arm64.tgz"
    assert a("Windows", "ARM64") is None and a("FreeBSD", "amd64") is None
    assert a("Linux", "armv7l") is None


def test_every_redirect_hop_is_checked():
    h = P._AllowlistRedirect(P.DOWNLOAD_HOSTS)
    req = urllib.request.Request("https://github.com/cloudflare/cloudflared/x")
    hdrs = http.client.HTTPMessage()
    for bad in ("https://evil.com/x", "http://objects.githubusercontent.com/x",
                "https://objects.githubusercontent.com.evil.net/x",
                "https://u:p@objects.githubusercontent.com/x", "file:///etc/passwd"):
        with pytest.raises(P.PublishError):
            h.redirect_request(req, None, 302, "Found", hdrs, bad)
    ok = h.redirect_request(req, None, 302, "Found", hdrs,
                            "https://release-assets.githubusercontent.com/github-production/x")
    assert ok.full_url.startswith("https://release-assets.githubusercontent.com/")
    # the real fetcher checks the first URL too, before any network use
    for bad in ("https://evil.com/x", "http://github.com/x"):
        with pytest.raises(P.PublishError):
            _REAL_FETCH(bad, lambda c: None, 100)


def test_checksum_from_body():
    h = "ab" * 32
    f = P.checksum_from_body
    assert f("%s  cloudflared-linux-amd64\n" % h, "cloudflared-linux-amd64") == h
    assert f("| cloudflared-linux-amd64 | %s |" % h.upper(), "cloudflared-linux-amd64") == h
    assert f("cloudflared-linux-amd64.deb: %s" % h, "cloudflared-linux-amd64") is None
    assert f("cloudflared-linux-amd64-fips: %s" % h, "cloudflared-linux-amd64") is None
    assert f("", "x") is None and f(None, "x") is None


def test_the_installer_downloads_nothing_until_asked(rig):
    net = Net("cloudflared-linux-amd64", b"x")
    m, bindir, exe = installer(rig, net)
    m.status()
    m.status("p")
    assert net.calls == [] and not bindir.exists()


# --------------------------------------------------------------------------- #
# The HTTP routes
# --------------------------------------------------------------------------- #

def _client():
    import app
    c = app.app.test_client()
    hdr = {"X-Free-LLM-Hub": "dashboard"}
    token = app.config.get_control_token()
    if token:
        hdr["X-Free-LLM-Hub-Token"] = token
    c._hdr = hdr
    c._token = token
    return c


def test_routes_are_gated_like_every_api_route(rig, monkeypatch):
    m = rig.manager()
    monkeypatch.setattr(P, "default", m)
    c = _client()
    # a write without the dashboard header
    r = c.post("/api/publish/start", json={"project_dir": "p", "confirm": True})
    assert r.status_code == 403
    if c._token:
        assert c.get("/api/publish").status_code == 401
        assert c.get("/api/publish", headers={"X-Free-LLM-Hub": "dashboard"}).status_code == 401
    assert rig.launches() == []


@pytest.mark.parametrize("confirm", [None, False, "true", 1, "yes", [True]])
def test_start_needs_a_literal_true_confirm(rig, monkeypatch, confirm):
    m = rig.manager()
    monkeypatch.setattr(P, "default", m)
    c = _client()
    body = {"project_dir": "p"}
    if confirm is not None:
        body["confirm"] = confirm
    r = c.post("/api/publish/start", json=body, headers=c._hdr)
    assert r.status_code == 400 and r.get_json()["code"] == "confirm_required"
    assert r.get_json()["error"]
    assert rig.launches() == [] and m.status()["tunnels"] == []
    assert c.post("/api/publish/install", json={"confirm": "true"}, headers=c._hdr).status_code == 400
    assert c.post("/api/publish/install", json={}, headers=c._hdr).status_code == 400


def test_start_stop_renew_status_round_trip(rig, monkeypatch, tmp_path):
    m = rig.manager()
    monkeypatch.setattr(P, "default", m)
    c = _client()
    proj = str(tmp_path / "proj")
    r = c.post("/api/publish/start", json={"project_dir": proj, "confirm": True, "ttl_minutes": 15},
               headers=c._hdr)
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    body = r.get_json()
    assert body["ok"] is True and body["tunnel"]["ttl_seconds"] == 900
    tid = body["tunnel"]["id"]
    rig.live(m, tid)
    g = c.get("/api/publish", query_string={"project_dir": proj}, headers=c._hdr).get_json()
    assert [t["id"] for t in g["tunnels"]] == [tid] and FAKE_URL_RE.match(g["tunnels"][0]["url"])
    assert g["limits"]["max_tunnels"] == 3 and "cloudflared" in g
    assert c.get("/api/publish", query_string={"project_dir": str(tmp_path / "x")},
                 headers=c._hdr).get_json()["tunnels"] == []
    nr = c.post("/api/publish/renew", json={"id": tid}, headers=c._hdr).get_json()
    assert nr["ok"] and nr["tunnel"]["id"] != tid
    rig.live(m, nr["tunnel"]["id"])
    sr = c.post("/api/publish/stop", json={"id": nr["tunnel"]["id"]}, headers=c._hdr).get_json()
    assert sr["ok"] and sr["tunnel"]["state"] == "stopped"


@pytest.mark.parametrize("path,body,status,code", [
    ("/api/publish/start", {"project_dir": "p", "confirm": True, "ttl_minutes": 7}, 400, "bad_ttl"),
    ("/api/publish/start", {"project_dir": "p", "confirm": True, "port": 22}, 403, "forbidden_port"),
    ("/api/publish/start", {"project_dir": "p", "confirm": True, "port": 8787}, 403, "forbidden_port"),
    ("/api/publish/start", {"confirm": True}, 400, "bad_request"),
    ("/api/publish/start", {"project_dir": 5, "confirm": True}, 400, "bad_request"),
    ("/api/publish/stop", {"id": "nope"}, 404, "not_found"),
    ("/api/publish/stop", {}, 400, "bad_request"),
    ("/api/publish/renew", {"id": "nope"}, 404, "not_found"),
])
def test_errors_map_to_4xx_with_error_and_code(rig, monkeypatch, path, body, status, code):
    m = rig.manager()
    monkeypatch.setattr(P, "default", m)
    c = _client()
    r = c.post(path, json=body, headers=c._hdr)
    assert r.status_code == status
    j = r.get_json()
    assert j["code"] == code and isinstance(j["error"], str) and j["error"]


def test_more_error_codes_through_the_routes(rig, monkeypatch):
    c = _client()
    for kw, status, code in (
            (dict(preview_port=lambda d: None), 409, "no_preview"),
            (dict(locate=lambda: None), 409, "no_cloudflared"),
            (dict(probe=lambda port: False), 409, "not_http"),
            (dict(flag=lambda: False), 403, "disabled")):
        monkeypatch.setattr(P, "default", rig.manager(**kw))
        r = c.post("/api/publish/start", json={"project_dir": "p", "confirm": True}, headers=c._hdr)
        assert (r.status_code, r.get_json()["code"]) == (status, code)
    monkeypatch.setattr(P, "default", rig.manager(system=lambda: ("linux", "riscv64"),
                                                  locate=lambda: None))
    r = c.post("/api/publish/install", json={"confirm": True}, headers=c._hdr)
    assert r.status_code == 409 and r.get_json()["code"] == "install_failed"
    m = rig.manager()
    monkeypatch.setattr(P, "default", m)
    for i in range(3):
        c.post("/api/publish/start", json={"project_dir": "p", "port": 5801 + i, "confirm": True},
               headers=c._hdr)
    r = c.post("/api/publish/start", json={"project_dir": "p", "port": 5805, "confirm": True},
               headers=c._hdr)
    assert r.status_code == 409 and r.get_json()["code"] == "too_many"


def test_install_route_returns_the_status(rig, monkeypatch):
    net = Net("cloudflared-linux-amd64", b"bin" * 30)
    m, bindir, exe = installer(rig, net)
    monkeypatch.setattr(P, "default", m)
    c = _client()
    r = c.post("/api/publish/install", json={"confirm": True}, headers=c._hdr)
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True and "cloudflared" in j and "tunnels" in j
    m._install["thread"].join(10)
    assert c.get("/api/publish", headers=c._hdr).get_json()["cloudflared"]["available"]
