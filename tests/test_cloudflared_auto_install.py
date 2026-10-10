"""cloudflared installs itself (publish.AutoInstaller, app.py's _cfi_* glue, the
Publish panel's "Install cloudflared automatically" box).

HERMETIC: no network, no real timer thread, no real cloudflared, nothing run.
  * The Manager gets a fake downloader (the release document and the asset, by
    URL, optionally held on a gate), a fake platform and its own bin folder.
  * The AutoInstaller gets a fake timer (fired by hand), a fake wall clock, its
    flag in a dict and its state file under tmp_path. A "reboot" is a fresh
    Manager + AutoInstaller on the same folders.
  * tests/conftest.py keeps every real default of publish failing loudly,
    including the automatic install's own timer (publish._auto_timer).
  * After every test no engine download thread and no automatic-install timer
    thread is left running.
"""
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import threading
import time

import pytest

import publish as P

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T0 = 1_000_000.0


def sha(data):
    return hashlib.sha256(data).hexdigest()


def _refuse(*_a, **_k):
    raise AssertionError("the install path must run nothing")


class Net:
    """The fake downloader: the release document, then the asset."""

    def __init__(self, asset, payload, digest="good"):
        self.url = ("https://github.com/cloudflare/cloudflared/releases/download/2099.1.0/%s"
                    % asset)
        row = {"name": asset, "browser_download_url": self.url}
        if digest == "good":
            row["digest"] = "sha256:" + sha(payload)
        elif digest == "bad":
            row["digest"] = "sha256:" + sha(b"something else")
        self.meta = {"tag_name": "2099.1.0", "body": "", "assets": [row]}
        self.payload = payload
        self.calls = []
        self.gate = None

    def __call__(self, url, sink, max_bytes, on_progress=None, hosts=None):
        self.calls.append((url, frozenset(hosts or ())))
        if url == P.RELEASE_API:
            data = json.dumps(self.meta).encode()
        else:
            if self.gate is not None:
                self.gate.wait(10)
            data = self.payload
        sink(data)
        if on_progress:
            on_progress(len(data), len(data))
        return len(data)


class Handle:
    def __init__(self, delay, fn):
        self.delay, self.fn = delay, fn
        self.cancelled = self.fired = False

    def cancel(self):
        self.cancelled = True


class Timers:
    """The fake timer factory: records (delay, fn); a test fires it by hand."""

    def __init__(self):
        self.all = []

    def __call__(self, delay, fn):
        h = Handle(delay, fn)
        self.all.append(h)
        return h

    def pending(self):
        return [h for h in self.all if not h.cancelled and not h.fired]

    def fire(self):
        (h,) = self.pending()
        h.fired = True
        h.fn()
        return h


_RIGS = []


class Rig:
    def __init__(self, tmp_path, system=("linux", "amd64"), digest="good",
                 publish_on=True, auto_on=True):
        self.dir = tmp_path
        self.bindir = tmp_path / "bin"
        self.exe = self.bindir / P._exe_name()
        self.state = tmp_path / "state" / P.AUTO_STATE_FILE
        self.system = system
        self.payload = b"\x7fELF fake cloudflared " * 40
        self.net = Net(P._ASSETS.get(P.platform_key(*system)) or "none", self.payload, digest)
        self.flags = {"publish": publish_on, "auto": auto_on}
        self.t = T0
        self.why = None                 # what blocked() answers
        self.managers = []
        _RIGS.append(self)
        self.boot()

    def boot(self):
        """A (re)started hub: a fresh Manager + AutoInstaller on the same bin
        folder, downloader and state file. The old process's timers are gone."""
        self.timers = Timers()
        self.m = P.Manager(
            fetch=self.net, system=lambda: self.system, bin_dir=lambda: str(self.bindir),
            locate=lambda: str(self.exe) if self.exe.exists() else None,
            spawn=_refuse, version_of=_refuse, flag=lambda: self.flags["publish"],
            timer=False, probe=lambda port: False, preview_port=lambda d: None,
            hub_ports=lambda: {8787}, procs=lambda: iter(()), home=lambda: str(self.dir),
            clock=lambda: self.t, wall=lambda: self.t)
        self.managers.append(self.m)
        self.a = P.AutoInstaller(
            self.m, flag=lambda: self.flags["auto"],
            set_flag=lambda v: self.flags.__setitem__("auto", v),
            wall=lambda: self.t, timer=self.timers, path=lambda: str(self.state))
        return self

    def start(self):
        self.a.start(blocked=lambda: self.why)

    def wait(self):
        th = self.m._install["thread"]
        if th is not None:
            th.join(10)
            assert not th.is_alive()

    def fire(self):
        h = self.timers.fire()
        self.wait()
        return h

    def wait_calls(self, n):
        """Until the download thread has made `n` calls (it then waits on the gate)."""
        deadline = time.monotonic() + 10
        while len(self.net.calls) < n and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(self.net.calls) == n

    def cf(self):
        return self.m.status()["cloudflared"]

    def record(self):
        return json.loads(self.state.read_text(encoding="utf-8"))

    def downloads(self):
        return [u for u, _h in self.net.calls if u != P.RELEASE_API]

    def leftovers(self):
        return sorted(f.name for f in self.bindir.iterdir() if f.name.startswith(".")) \
            if self.bindir.exists() else []


@pytest.fixture(autouse=True)
def _nothing_left_running():
    yield
    while _RIGS:
        rig = _RIGS.pop()
        if rig.net.gate is not None:
            rig.net.gate.set()
        for m in rig.managers:
            th = m._install["thread"]
            if th is not None:
                th.join(10)
    alive = [t.name for t in threading.enumerate()
             if t.name in ("cloudflared-auto", "publish-install") and t.is_alive()]
    assert alive == [], alive


# --------------------------------------------------------------------------- #
# Never at import, never on the boot thread
# --------------------------------------------------------------------------- #

def test_importing_publish_arms_nothing_and_reads_nothing(tmp_path):
    env = dict(os.environ, FREE_LLM_HUB_CONFIG=str(tmp_path / "config.json"))
    code = ("import threading, publish; a = publish.auto; "
            "print(a._handle is None and a._due is None and not a._started and a._record is None "
            "and not any(t.name == 'cloudflared-auto' for t in threading.enumerate()))")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True,
                         text=True, timeout=60)
    assert out.stdout.strip() == "True", out.stderr[-2000:]
    assert not (tmp_path / P.AUTO_STATE_FILE).exists()


def test_importing_app_arms_nothing():
    import app  # noqa: F401
    assert P.auto._handle is None and P.auto._due is None and not P.auto._started
    assert not any(t.name == "cloudflared-auto" for t in threading.enumerate())


def test_the_real_timer_is_fenced_off_in_tests():
    with pytest.raises(AssertionError):
        P._auto_timer(1, lambda: None)


def test_the_boot_hook_is_one_call_right_after_the_tunnel_sweep():
    src = io.open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    main = src.index('if __name__ == "__main__":')
    calls = [m.start() for m in re.finditer(r"^\s+_cfi_start\(\)", src, re.M)]
    assert len(calls) == 1 and calls[0] > main
    assert src.index("publish.default.sweep_leftovers()", main) < calls[0] \
        < src.index("_reconnect_clis_after_stop()", main)
    assert "auto.start(blocked=_cfi_blocked)" in src


# --------------------------------------------------------------------------- #
# The automatic install
# --------------------------------------------------------------------------- #

def test_one_look_a_minute_after_boot_then_the_verified_install(tmp_path):
    r = Rig(tmp_path)
    r.start()
    (h,) = r.timers.pending()
    assert h.delay == P.AUTO_DELAY_SECONDS == 60
    assert r.net.calls == [] and r.m._install["thread"] is None       # start() returned at once
    cf = r.cf()
    assert cf["auto_state"] == "scheduled" and cf["auto_next_attempt"] == T0 + 60
    assert cf["available"] is False and not r.state.exists()
    r.t += 60
    r.fire()
    # the Manager's own install: same hosts, same checksum, nothing run
    assert r.net.calls == [(P.RELEASE_API, P.METADATA_HOSTS), (r.net.url, P.DOWNLOAD_HOSTS)]
    assert r.exe.read_bytes() == r.payload and r.leftovers() == []
    cf = r.cf()
    assert cf["available"] is True and cf["auto_state"] == "installed"
    assert cf["auto_last_attempt"] == T0 + 60 and cf["auto_error"] is None
    rec = r.record()
    assert rec["result"] == "installed" and rec["last_attempt"] == T0 + 60
    assert rec["platform"] == "linux/amd64"
    assert r.timers.pending() == []
    # installed: a later boot looks once and does nothing
    r.boot().start()
    r.fire()
    assert len(r.net.calls) == 2


@pytest.mark.parametrize("case,why", [
    ("available", "available"), ("publish_off", "disabled"), ("flag_off", "off")])
def test_skipped_when_there_is_nothing_to_do(tmp_path, case, why):
    r = Rig(tmp_path)
    if case == "available":
        r.bindir.mkdir()
        r.exe.write_bytes(b"already here")
    elif case == "publish_off":
        r.flags["publish"] = False
    else:
        r.flags["auto"] = False
    r.start()
    if case == "flag_off":
        assert r.timers.all == []                    # nothing armed at all
    else:
        r.fire()
    assert r.a.run_once() == why
    assert r.net.calls == [] and r.timers.pending() == [] and not r.state.exists()


@pytest.mark.parametrize("why", ["draining", "stopped"])
def test_never_during_a_drain_or_a_stop_it_looks_again_later(tmp_path, why):
    r = Rig(tmp_path)
    r.why = why
    r.start()
    r.fire()
    (h,) = r.timers.pending()
    assert h.delay == P.AUTO_RECHECK_SECONDS and r.net.calls == [] and not r.state.exists()
    r.fire()                                         # still draining / stopped
    assert r.net.calls == [] and len(r.timers.pending()) == 1
    r.why = None                                     # the drain ended without a restart
    r.fire()
    assert r.exe.exists() and r.record()["result"] == "installed"


def test_a_blocked_check_that_raises_waits_instead_of_guessing(tmp_path):
    r = Rig(tmp_path)
    r.a.start(blocked=lambda: 1 / 0)
    r.fire()
    assert r.net.calls == [] and r.timers.pending()[0].delay == P.AUTO_RECHECK_SECONDS


@pytest.mark.parametrize("digest,words", [("bad", "does not match"), ("none", "no checksum")])
def test_a_checksum_problem_installs_nothing_and_is_reported(tmp_path, digest, words):
    r = Rig(tmp_path, digest=digest)
    r.start()
    r.t += 60
    r.fire()
    assert not r.exe.exists() and r.leftovers() == []
    cf = r.cf()
    assert cf["available"] is False and cf["auto_state"] == "failed"
    assert words in cf["auto_error"] and "nothing" in cf["auto_error"]
    assert cf["install_error"] == cf["auto_error"]          # the panel says it once
    rec = r.record()
    assert rec["result"] == "failed" and words in rec["error"]


def test_a_failure_waits_24_hours_across_restarts_and_the_button_still_works(tmp_path):
    r = Rig(tmp_path, digest="bad")
    r.start()
    r.t += 60
    r.fire()
    assert len(r.downloads()) == 1
    (h,) = r.timers.pending()
    assert h.delay == P.AUTO_RETRY_SECONDS == 24 * 3600
    assert r.cf()["auto_next_attempt"] == T0 + 60 + 24 * 3600
    # restarted an hour later: the boot look finds the failure and waits the rest
    r.t += 3600
    r.boot().start()
    assert r.cf()["auto_state"] == "failed"
    r.t += 60
    r.fire()
    (h,) = r.timers.pending()
    assert h.delay == pytest.approx(24 * 3600 - 3600 - 60) and len(r.downloads()) == 1
    # the Install button works any time (and leaves the automatic record alone)
    r.m.install()
    r.wait()
    assert len(r.downloads()) == 2 and r.record()["last_attempt"] == T0 + 60
    # 24 h after the failed attempt: tried again, once
    r.t = T0 + 60 + 24 * 3600
    r.fire()
    assert len(r.downloads()) == 3 and r.record()["last_attempt"] == r.t
    assert len(r.timers.pending()) == 1                      # the next try, a day out


def test_an_unsupported_platform_is_recorded_and_never_tried(tmp_path):
    r = Rig(tmp_path, system=("linux", "riscv64"))
    r.start()
    r.fire()
    assert r.net.calls == [] and r.timers.pending() == []
    rec = r.record()
    assert rec["result"] == "unsupported" and rec["platform"] == "linux/riscv64"
    cf = r.cf()
    assert cf["auto_state"] == "unsupported" and "no automatic download" in cf["auto_error"]
    for _ in range(2):                                       # later boots never try either
        r.boot().start()
        r.fire()
        assert r.net.calls == [] and r.timers.pending() == []


def test_never_two_downloads_at_once(tmp_path):
    # the button first: the automatic look leaves it alone and records nothing
    r = Rig(tmp_path)
    r.net.gate = threading.Event()
    r.m.install()
    r.wait_calls(2)                                  # the button's download, held on the gate
    r.start()
    r.timers.fire()
    assert r.a._mine is None and not r.state.exists()
    assert len(r.net.calls) == 2
    assert r.cf()["auto_state"] != "installing"
    r.net.gate.set()
    r.wait()
    assert r.exe.exists() and r.cf()["auto_state"] == "idle"
    # the automatic install first: the button joins it instead of a second one
    r2 = Rig(tmp_path / "two")
    r2.net.gate = threading.Event()
    r2.start()
    r2.timers.fire()
    r2.wait_calls(2)
    first = r2.m._install["thread"]
    assert r2.cf()["auto_state"] == "installing" and r2.record()["result"] == "installing"
    r2.m.install()
    assert r2.m._install["thread"] is first
    assert r2.a.run_once() == "busy"
    r2.net.gate.set()
    r2.wait()
    assert len(r2.net.calls) == 2 and r2.cf()["auto_state"] == "installed"


def test_an_attempt_cut_by_a_restart_is_tried_once_more_then_counts_as_failed(tmp_path):
    r = Rig(tmp_path)
    r.state.parent.mkdir(parents=True)
    r.state.write_text(json.dumps({"result": "installing", "last_attempt": T0 - 100}),
                       encoding="utf-8")
    r.boot()
    assert r.cf()["auto_state"] == "idle"                    # not a failure yet
    r.net.gate = threading.Event()
    r.start()
    r.timers.fire()
    assert r.record() == dict(r.record(), result="installing", interrupted=1)
    # cut short again: the next boot reads it as a failure and waits
    r.boot()
    cf = r.cf()
    assert cf["auto_state"] == "failed" and "cut short twice" in cf["auto_error"]
    assert r.a.run_once() == "waiting"
    r.net.gate.set()


def test_the_state_file_is_replaced_atomically(tmp_path, monkeypatch):
    seen = []
    real = os.replace

    def spy(src, dst):
        seen.append((os.path.dirname(os.path.abspath(src)), os.path.basename(src), dst))
        return real(src, dst)
    r = Rig(tmp_path, system=("linux", "riscv64"))
    with monkeypatch.context() as mp:
        mp.setattr(P.os, "replace", spy)
        r.a.run_once()
    (folder, name, dst) = seen[-1]
    assert dst == os.path.abspath(str(r.state)) and folder == str(r.state.parent)
    assert name.startswith(".cloudflared-auto-") and name.endswith(".tmp")
    assert sorted(os.listdir(r.state.parent)) == [P.AUTO_STATE_FILE]
    assert r.record()["result"] == "unsupported"


def test_status_fields(tmp_path):
    r = Rig(tmp_path)
    cf = r.cf()
    assert {"auto_install", "auto_state", "auto_last_attempt", "auto_error"} <= set(cf)
    assert cf["auto_install"] is True and cf["auto_state"] == "idle"
    assert cf["auto_last_attempt"] is None and cf["auto_error"] is None
    r.flags["auto"] = False
    assert (r.cf()["auto_install"], r.cf()["auto_state"]) == (False, "off")
    r.flags.update(auto=True, publish=False)
    assert r.cf()["auto_state"] == "off"
    # a Manager with no AutoInstaller keeps exactly its old fields
    plain = P.Manager(system=lambda: ("linux", "amd64"), locate=lambda: None, flag=lambda: True,
                      bin_dir=lambda: str(tmp_path / "x"), timer=False)
    assert not any(k.startswith("auto_") for k in plain.status()["cloudflared"])


def test_flag_off_is_the_explicit_click_exactly_as_before(tmp_path):
    r = Rig(tmp_path, auto_on=False)
    r.start()
    assert r.timers.all == [] and r.a.run_once() == "off"
    assert r.net.calls == [] and not r.state.exists()
    cf = r.cf()
    assert cf["auto_install"] is False and cf["auto_state"] == "off"
    assert cf["installable"] is True and cf["installing"] is False
    r.m.install()                                            # the click
    r.wait()
    assert r.net.calls == [(P.RELEASE_API, P.METADATA_HOSTS), (r.net.url, P.DOWNLOAD_HOSTS)]
    assert r.exe.read_bytes() == r.payload
    assert not r.state.exists() and r.timers.all == [] and r.a._mine is None


# --------------------------------------------------------------------------- #
# The route and the glue
# --------------------------------------------------------------------------- #

def _client():
    import app
    c = app.app.test_client()
    hdr = {"X-Free-LLM-Hub": "dashboard"}
    token = app.config.get_control_token()
    if token:
        hdr["X-Free-LLM-Hub-Token"] = token
    c._hdr = hdr
    return c


def test_the_box_sets_the_flag_through_the_install_route(tmp_path, monkeypatch):
    r = Rig(tmp_path)
    monkeypatch.setattr(P, "default", r.m)
    monkeypatch.setattr(P, "auto", r.a)
    c = _client()
    r.start()
    g = c.get("/api/publish", headers=c._hdr).get_json()
    assert g["cloudflared"]["auto_install"] is True and g["cloudflared"]["auto_state"] == "scheduled"
    j = c.post("/api/publish/install", json={"auto": False}, headers=c._hdr)
    assert j.status_code == 200 and j.headers["Cache-Control"] == "no-store"
    cf = j.get_json()["cloudflared"]
    assert r.flags["auto"] is False and cf["auto_install"] is False and cf["auto_state"] == "off"
    assert r.timers.pending() == [] and r.net.calls == []
    j = c.post("/api/publish/install", json={"auto": True}, headers=c._hdr)
    assert j.status_code == 200 and r.flags["auto"] is True
    (h,) = r.timers.pending()
    assert h.delay == P.AUTO_REARM_SECONDS and r.net.calls == []   # nothing downloaded by the click
    bad = c.post("/api/publish/install", json={"auto": "yes"}, headers=c._hdr)
    assert bad.status_code == 400 and bad.get_json()["code"] == "bad_request"
    assert r.flags["auto"] is True
    # the explicit click is unchanged
    assert c.post("/api/publish/install", json={}, headers=c._hdr).get_json()["code"] == "confirm_required"
    ok = c.post("/api/publish/install", json={"confirm": True}, headers=c._hdr)
    assert ok.status_code == 200
    r.wait()
    assert r.exe.exists() and not r.state.exists()


def test_a_flag_that_cannot_be_saved_is_a_plain_error(tmp_path, monkeypatch):
    r = Rig(tmp_path)

    def boom(_v):
        raise OSError("disk full")
    r.a._set_flag_fn = boom
    monkeypatch.setattr(P, "default", r.m)
    monkeypatch.setattr(P, "auto", r.a)
    c = _client()
    j = c.post("/api/publish/install", json={"auto": False}, headers=c._hdr)
    assert j.status_code == 500 and j.get_json()["code"] == "save_failed"


def test_blocked_names_a_drain_or_a_stop(monkeypatch):
    import app

    d = {"on": False}
    monkeypatch.setattr(app._UPDATE_DRAIN, "active", lambda: d["on"])
    monkeypatch.setattr(app, "_restart_is_vetoed_by_stop", lambda: False)
    monkeypatch.setitem(app._auto_update_state, "updating", False)
    assert app._cfi_blocked() is None
    d["on"] = True
    assert app._cfi_blocked() == "draining"
    d["on"] = False
    monkeypatch.setitem(app._auto_update_state, "updating", True)
    assert app._cfi_blocked() == "draining"
    monkeypatch.setitem(app._auto_update_state, "updating", False)
    monkeypatch.setattr(app, "_restart_is_vetoed_by_stop", lambda: True)
    assert app._cfi_blocked() == "stopped"
    got = {}

    class Fake:
        def start(self, blocked=None):
            got["blocked"] = blocked
    monkeypatch.setattr(P, "auto", Fake())
    app._cfi_start()
    assert got["blocked"] is app._cfi_blocked


# --------------------------------------------------------------------------- #
# The Publish panel (static) and the README
# --------------------------------------------------------------------------- #

HTML = io.open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8").read()


def _between(start, end, src=HTML):
    i = src.index(start)
    return src[i:src.index(end, i)]


JS = _between("/* publish-js:start */", "/* publish-js:end */")
INSTALL_BOX = _between('<div id="publish-install"', '<div id="publish-form">')


def test_the_panel_has_the_box_the_progress_line_and_the_failure_line():
    box = _between('<label class="publish-consent" for="publish-auto"', "</label>", INSTALL_BOX)
    assert '<input type="checkbox" id="publish-auto">' in box
    assert "Install cloudflared automatically" in box
    assert 'id="publish-install-btn"' in INSTALL_BOX and 'id="publish-install-cmd"' in INSTALL_BOX
    assert "Installing cloudflared automatically…" in JS
    assert "'The automatic install failed: ' + cf.auto_error" in JS
    assert "autoState === 'installing'" in JS and "cf.auto_install" in JS
    assert "api('/api/publish/install', { method: 'POST', body: { auto: want } })" in JS
    assert "autoBox.addEventListener('change', doAuto)" in JS
    # the button is still an explicit click, and still offered when it failed
    assert "installBtn.addEventListener('click', doInstall)" in JS
    assert "setHidden(installBtn, !auto)" in JS and "setHidden(installCmdRow, !cmd)" in JS


def test_the_readme_says_it_installs_itself_and_how_to_turn_it_off():
    readme = io.open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    section = _between("## Publish a project online", "## Endpoints", readme)
    assert "by itself" in section and "`cloudflared_auto_install`" in section
    assert "Install cloudflared automatically" in section
    assert "Nothing is downloaded until you press it" not in section
