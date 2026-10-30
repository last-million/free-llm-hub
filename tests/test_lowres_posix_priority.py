"""The RAM governor's worker priority on Linux / macOS (lowres.Governor._priority).

An unprivileged POSIX process can RAISE a niceness but cannot lower it again
(setpriority answers EACCES): the governor used to set its workers to nice 10
below the critical RAM floor, then "restore" them with nice(0), which the OS
refused -- the error was swallowed and the governor reported normal priority
while the workers stayed slow for the rest of their life.

Everything here is fake: a table of fake processes that applies the kernel rule
(raising a niceness always works, lowering it needs privilege), a fake `os`
(name, geteuid), fake capability / RLIMIT_NICE readers, a fake clock and
machine. No real process is ever reniced: the fake factory only answers for
odd pids no Windows process can have (Windows pids are multiples of 4) and
hands every other pid to the real psutil.Process for any other thread.
"""
import os

import psutil
import pytest

import lowres

PID = 70001                       # odd: never a real Windows pid
PID2 = 70003
PID3 = 70005


class Table:
    """Fake processes under the POSIX rule: raising a niceness is always
    allowed, lowering it needs privilege (`privileged`)."""

    def __init__(self, real_factory):
        self.rows = {}                 # pid -> {"nice", "ctime"}
        self.fake_pids = set()
        self.privileged = False
        self.sets = []                 # (pid, value) the "kernel" applied
        self.denied = []               # (pid, value) the "kernel" refused
        self.broken = set()            # pids whose nice() raises a plain error
        self._real = real_factory

    def add(self, pid, nice=0, ctime=100.0):
        self.rows[pid] = {"nice": nice, "ctime": ctime}
        self.fake_pids.add(pid)

    def nice_of(self, pid):
        return self.rows[pid]["nice"]

    def factory(self, pid):
        if pid not in self.fake_pids:
            return self._real(pid)     # another thread's real lookup: untouched
        if pid not in self.rows:
            raise psutil.NoSuchProcess(pid)
        return FakeProc(self, pid)


class FakeProc:
    def __init__(self, table, pid):
        self.t, self.pid = table, pid

    def _row(self):
        row = self.t.rows.get(self.pid)
        if row is None:
            raise psutil.NoSuchProcess(self.pid)
        return row

    def create_time(self):
        return self._row()["ctime"]

    def nice(self, value=None):
        row = self._row()
        if self.pid in self.t.broken:
            raise RuntimeError("boom")
        if value is None:
            return row["nice"]
        if value < row["nice"] and not self.t.privileged:
            self.t.denied.append((self.pid, value))
            raise psutil.AccessDenied(self.pid)
        row["nice"] = value
        self.t.sets.append((self.pid, value))


class FakeOS:
    """The real os module, except the platform name and the effective uid."""

    def __init__(self, name, euid):
        self.name = name
        self._euid = euid

    def geteuid(self):
        return self._euid

    def __getattr__(self, attr):
        return getattr(os, attr)


class Box:
    def __init__(self, table):
        self.table = table
        self.caps = 0                  # effective capability mask: none
        self.rlimit = 0                # RLIMIT_NICE soft limit: the usual default
        self.m = {"total_gb": 40.0, "free_gb": 20.0, "cores": 8}
        self.t = 1000.0
        self.procs = []

    def governor(self, **kw):
        return lowres.Governor(read_machine=lambda: dict(self.m), clock=lambda: self.t,
                               procs=lambda: list(self.procs), cpu=lambda: 10.0, **kw)

    def tick(self, g, free):
        self.t += 2.0
        self.m["free_gb"] = free
        g.tick()
        assert not g.failed
        return g


@pytest.fixture
def box(monkeypatch):
    """A POSIX machine where the hub runs as an ordinary user (uid 1000)."""
    table = Table(psutil.Process)
    b = Box(table)
    b.os = FakeOS("posix", 1000)
    monkeypatch.setattr(lowres, "os", b.os)
    monkeypatch.setattr(psutil, "Process", table.factory)
    monkeypatch.setattr(lowres, "_read_cap_eff", lambda *a, **k: b.caps, raising=False)
    monkeypatch.setattr(lowres, "_init_user_ns", lambda *a, **k: True, raising=False)
    monkeypatch.setattr(lowres, "_rlimit_nice", lambda proc: b.rlimit, raising=False)
    monkeypatch.setattr(lowres, "_ORIG_NICE", {}, raising=False)
    monkeypatch.setattr(lowres, "mode", lambda: "auto")
    monkeypatch.setattr(lowres, "reserve_gb", lambda m=None: 8.0)
    return b


def _worker(b, pid=PID, nice=0, ctime=100.0):
    b.table.add(pid, nice=nice, ctime=ctime)
    b.procs.append({"pid": pid, "ppid": 0, "rss_gb": 0.5, "marker": True, "cpu": 1.0})


# --------------------------------------------------------------------------- #
# 1. the bug: an unprivileged hub never lowers what it cannot give back
# --------------------------------------------------------------------------- #

def test_an_unprivileged_hub_never_lowers_a_worker_it_cannot_raise_back(box):
    _worker(box)
    g = box.governor()
    box.tick(g, free=20.0)
    box.tick(g, free=0.8)                         # below the critical floor
    assert box.table.sets == [] and box.table.nice_of(PID) == 0
    assert not g.low_prio and g._low_pids == []   # nothing held, nothing claimed
    box.tick(g, free=3.0)                         # RAM is back
    assert box.table.nice_of(PID) == 0
    assert box.table.denied == []                 # no refused "restore" either
    assert not g.low_prio


def test_ram_still_blocks_new_spawns_when_priority_is_left_alone(box):
    _worker(box)
    g = box.governor()
    box.tick(g, free=0.8)
    assert g.headroom() == 0                      # the main protection still holds
    assert box.table.nice_of(PID) == 0


# --------------------------------------------------------------------------- #
# 2. a restore the OS refuses is never reported as done, and is retried
# --------------------------------------------------------------------------- #

def test_a_refused_restore_stays_held_and_is_retried(box):
    _worker(box)
    box.rlimit = 20                               # RLIMIT_NICE allows nice 0 again
    box.table.privileged = True
    g = box.governor()
    box.tick(g, free=0.8)
    assert box.table.nice_of(PID) == 10 and g.low_prio and g._low_pids == [PID]

    box.table.privileged = False                  # the limit was lowered meanwhile
    box.tick(g, free=3.0)
    assert box.table.nice_of(PID) == 10           # the OS refused the restore ...
    assert g.low_prio and g._low_pids == [PID]    # ... and the governor says so
    assert g.prio_unrestored == [PID]
    box.tick(g, free=3.0)
    assert len(box.table.denied) == 2             # tried again on the next tick

    box.table.privileged = True
    box.tick(g, free=3.0)
    assert box.table.nice_of(PID) == 0
    assert not g.low_prio and g._low_pids == [] and g.prio_unrestored == []


def test_a_refused_restore_survives_a_new_drop_below_the_floor(box):
    _worker(box)
    box.rlimit = 20
    box.table.privileged = True
    g = box.governor()
    box.tick(g, free=0.8)
    box.table.privileged = False
    box.tick(g, free=3.0)                         # refused: still held
    box.procs = []                                # its listing vanished this tick
    _worker(box, pid=PID2)
    box.procs = [p for p in box.procs if p["pid"] == PID2]
    box.tick(g, free=0.8)                         # low again: the old one is kept
    assert PID in g._low_pids and PID2 in g._low_pids
    box.table.privileged = True
    box.tick(g, free=3.0)
    assert box.table.nice_of(PID) == 0 and box.table.nice_of(PID2) == 0
    assert not g.low_prio


def test_the_end_of_run_restore_is_truthful(box, monkeypatch):
    _worker(box)
    box.rlimit = 20
    box.table.privileged = True
    g = box.governor()
    box.tick(g, free=0.8)
    assert box.table.nice_of(PID) == 10
    box.table.privileged = False
    monkeypatch.setattr(lowres, "GOV", g)
    monkeypatch.setattr(lowres, "_mon", {"users": 0, "thread": None})
    lowres._monitor_loop()                        # no users: ends at once, then restores
    assert box.table.nice_of(PID) == 10
    assert g.low_prio and g.prio_unrestored == [PID]
    box.table.privileged = True
    g.restore_priority()
    assert box.table.nice_of(PID) == 0 and not g.low_prio


# --------------------------------------------------------------------------- #
# 3. what a privileged hub lowers, it gives back exactly
# --------------------------------------------------------------------------- #

def test_the_restore_goes_back_to_the_niceness_the_worker_had(box):
    _worker(box, nice=5)                          # the hub itself runs under nice 5
    box.caps = 1 << 23                            # Linux, CAP_SYS_NICE held
    box.table.privileged = True
    g = box.governor()
    for _ in range(3):
        box.tick(g, free=0.8)                     # several ticks below the floor
    assert box.table.nice_of(PID) == 10
    box.tick(g, free=3.0)
    assert box.table.nice_of(PID) == 5            # not 0: never above where it was
    assert not g.low_prio


def test_a_worker_already_niced_lower_is_left_alone(box):
    _worker(box, nice=15)
    box.caps = None                               # macOS: no capability set ...
    box.os._euid = 0                              # ... and the hub runs as root
    box.table.privileged = True
    g = box.governor()
    box.tick(g, free=0.8)
    assert box.table.nice_of(PID) == 15 and box.table.sets == []
    assert not g.low_prio
    box.tick(g, free=3.0)
    assert box.table.nice_of(PID) == 15 and box.table.sets == []


# --------------------------------------------------------------------------- #
# 4. the priority function reports what it could not change
# --------------------------------------------------------------------------- #

def test_psutil_priority_reports_the_pids_it_did_not_change(box):
    t = box.table
    t.add(PID)
    t.add(PID2)
    t.add(PID3)
    t.broken.add(PID3)
    memo = {}
    can = lambda proc, nice: proc.pid == PID2        # only PID2 can be given back
    missed = lowres._psutil_priority([PID, PID2, PID3, 70007], True, posix=True,
                                     proc_factory=t.factory, can_restore=can, memo=memo)
    assert sorted(missed) == [PID, PID3]             # 70007 is gone: nothing to do
    assert t.nice_of(PID) == 0 and t.nice_of(PID2) == 10
    assert memo == {(PID2, 100.0): 0}
    t.privileged = True
    assert lowres._psutil_priority([PID2], False, posix=True, proc_factory=t.factory,
                                   memo=memo) == []
    assert t.nice_of(PID2) == 0 and memo == {}


def test_a_gone_worker_counts_as_restored(box):
    t = box.table
    t.add(PID)
    memo = {}
    yes = lambda proc, nice: True
    assert lowres._psutil_priority([PID], True, posix=True, proc_factory=t.factory,
                                   can_restore=yes, memo=memo) == []
    del t.rows[PID]                                  # the helper exited
    assert lowres._psutil_priority([PID], False, posix=True, proc_factory=t.factory,
                                   memo=memo) == []
    assert memo == {}


def test_a_reused_pid_is_never_touched_by_a_restore(box):
    t = box.table
    t.add(PID)
    t.privileged = True
    memo = {}
    yes = lambda proc, nice: True
    lowres._psutil_priority([PID], True, posix=True, proc_factory=t.factory,
                            can_restore=yes, memo=memo)
    t.add(PID, nice=7, ctime=200.0)                  # another process got the pid
    t.sets.clear()
    assert lowres._psutil_priority([PID], False, posix=True, proc_factory=t.factory,
                                   memo=memo) == []
    assert t.nice_of(PID) == 7 and t.sets == [] and memo == {}


def test_psutil_priority_never_raises(box):
    def explode(pid):
        raise TypeError("no")
    assert lowres._psutil_priority([PID, PID2], True, posix=True,
                                   proc_factory=explode, can_restore=lambda p, n: True,
                                   memo={}) == [PID, PID2]


def test_a_hook_that_reports_misses_is_believed(box):
    calls = []

    def hook(pids, low):
        calls.append((list(pids), low))
        return answers.pop(0)

    answers = [[PID2], [PID], []]
    box.procs = [{"pid": p, "ppid": 0, "rss_gb": 0.5, "marker": True, "cpu": 1.0}
                 for p in (PID, PID2)]
    g = box.governor(set_priority=hook)
    box.tick(g, free=0.8)
    assert g._low_pids == [PID] and g.low_prio      # PID2 was not lowered
    box.tick(g, free=3.0)
    assert calls[-1] == ([PID], False)              # only what was lowered is restored
    assert g.low_prio and g.prio_unrestored == [PID]
    box.tick(g, free=3.0)
    assert not g.low_prio and g._low_pids == []


# --------------------------------------------------------------------------- #
# 5. who may lower a niceness again
# --------------------------------------------------------------------------- #

def test_can_restore_nice_follows_the_kernel_rule():
    f = lowres._can_restore_nice
    none = lambda *a: None
    user, root = (lambda: 1000), (lambda: 0)
    host, rootless = (lambda: True), (lambda: False)
    nice_cap = lambda: 1 << 23
    # macOS / BSD (no capability set): root only
    assert f(None, 0, geteuid=root, cap_eff=none, rlimit_nice=none)
    assert not f(None, 0, geteuid=user, cap_eff=none, rlimit_nice=none)
    # Linux: CAP_SYS_NICE in the initial user namespace, not the uid
    assert f(None, 0, geteuid=user, cap_eff=nice_cap, rlimit_nice=none, init_userns=host)
    assert not f(None, 0, geteuid=root, cap_eff=lambda: (1 << 23) - 1, rlimit_nice=lambda p: 0,
                 init_userns=host)                       # root in a default Docker container
    assert not f(None, 0, geteuid=root, cap_eff=nice_cap, rlimit_nice=lambda p: 0,
                 init_userns=rootless)                   # rootless container
    # RLIMIT_NICE: 20 - nice <= soft limit
    assert f(None, 0, geteuid=user, cap_eff=lambda: 0, rlimit_nice=lambda p: 20)
    assert not f(None, 0, geteuid=user, cap_eff=lambda: 0, rlimit_nice=lambda p: 19)
    assert f(None, 0, geteuid=user, cap_eff=lambda: 0, rlimit_nice=lambda p: float("inf"))
    assert f(None, 5, geteuid=user, cap_eff=lambda: 0, rlimit_nice=lambda p: 15)
    assert not f(None, 5, geteuid=user, cap_eff=lambda: 0, rlimit_nice=lambda p: 14)


def test_can_restore_nice_fails_closed_and_never_raises():
    def boom(*a):
        raise OSError("no")
    f = lowres._can_restore_nice
    assert not f(None, 0, geteuid=boom, cap_eff=boom, rlimit_nice=boom)
    assert not f(None, 0, geteuid=boom, cap_eff=lambda: None, rlimit_nice=boom)
    assert not f(None, 0, geteuid=lambda: 1000, cap_eff=lambda: 1 << 23, rlimit_nice=boom,
                 init_userns=boom)


def test_the_linux_readers_parse_their_files(tmp_path):
    p = tmp_path / "status"
    p.write_text("Name:\tpython\nCapEff:\t0000000000800000\n", encoding="ascii")
    assert lowres._read_cap_eff(str(p)) == 1 << 23
    assert lowres._read_cap_eff(str(tmp_path / "missing")) is None
    m = tmp_path / "uid_map"
    m.write_text("         0          0 4294967295\n", encoding="ascii")
    assert lowres._init_user_ns(str(m)) is True
    m.write_text("         0       1000          1\n         1     100000      65536\n",
                 encoding="ascii")
    assert lowres._init_user_ns(str(m)) is False
    assert lowres._init_user_ns(str(tmp_path / "missing")) is True


# --------------------------------------------------------------------------- #
# 6. Windows: exactly the calls it always made
# --------------------------------------------------------------------------- #

def test_windows_keeps_its_calls_and_reports_nothing(monkeypatch):
    below, normal = 16384, 32
    monkeypatch.setattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS", below, raising=False)
    monkeypatch.setattr(psutil, "NORMAL_PRIORITY_CLASS", normal, raising=False)
    calls = []
    real = psutil.Process

    class WinProc:
        def __init__(self, pid):
            self.pid = pid

        def nice(self, value=None):
            if self.pid == PID2:
                raise psutil.AccessDenied(self.pid)
            calls.append((self.pid, value))

    def factory(pid):
        return WinProc(pid) if pid in (PID, PID2) else real(pid)

    monkeypatch.setattr(psutil, "Process", factory)
    monkeypatch.setattr(lowres, "os", FakeOS("nt", None))
    monkeypatch.setattr(lowres, "mode", lambda: "auto")
    monkeypatch.setattr(lowres, "reserve_gb", lambda m=None: 8.0)
    b = Box(None)
    b.procs = [{"pid": p, "ppid": 0, "rss_gb": 0.5, "marker": True, "cpu": 1.0}
               for p in (PID, PID2)]
    g = b.governor()
    b.tick(g, free=0.8)
    assert calls == [(PID, below)]
    assert g.low_prio and g._low_pids == [PID, PID2]     # as before: every listed pid
    b.tick(g, free=3.0)
    assert calls[-1] == (PID, normal)
    assert not g.low_prio and g._low_pids == []
    assert lowres._psutil_priority([PID, PID2], True) == []

# --------------------------------------------------------------------------- #
# 5. merge review (2026-10-30): inherited low level, start time read on one side
# --------------------------------------------------------------------------- #

def test_a_child_that_inherited_the_low_level_is_given_back(box):
    """A process a lowered worker starts inherits nice 10. On a host where the hub
    may set it back it is held and restored with the others; it used to be reported
    'already this low' and stay at 10 for good."""
    _worker(box)                                  # the parent worker, nice 0
    box.caps = 1 << 23                            # Linux, CAP_SYS_NICE held
    box.table.privileged = True
    g = box.governor()
    box.tick(g, free=0.8)
    assert box.table.nice_of(PID) == 10
    _worker(box, pid=PID2, nice=10)               # started by it: inherited nice 10
    box.tick(g, free=0.8)
    assert PID2 in g._low_pids
    box.tick(g, free=3.0)
    assert box.table.nice_of(PID) == 0 and box.table.nice_of(PID2) == 0
    assert not g.low_prio


def test_a_worker_at_the_low_level_is_left_alone_when_the_hub_lowered_nothing(box):
    _worker(box, nice=10)
    box.caps = 1 << 23
    box.table.privileged = True
    g = box.governor()
    box.tick(g, free=0.8)
    box.tick(g, free=3.0)
    assert box.table.nice_of(PID) == 10 and box.table.sets == []
    assert not g.low_prio


def _unreadable_start_time(self):
    raise RuntimeError("start time unreadable right now")


def test_a_start_time_unread_when_lowering_still_restores(box, monkeypatch):
    t = box.table
    t.add(PID)
    t.privileged = True
    memo = {}
    yes = lambda proc, nice: True
    real = FakeProc.create_time
    monkeypatch.setattr(FakeProc, "create_time", _unreadable_start_time)
    assert lowres._psutil_priority([PID], True, posix=True, proc_factory=t.factory,
                                   can_restore=yes, memo=memo) == []
    assert t.nice_of(PID) == 10 and memo == {(PID, None): 0}
    monkeypatch.setattr(FakeProc, "create_time", real)   # readable again at restore
    assert lowres._psutil_priority([PID], False, posix=True, proc_factory=t.factory,
                                   memo=memo) == []
    assert t.nice_of(PID) == 0 and memo == {}


def test_a_start_time_unread_when_restoring_still_restores(box, monkeypatch):
    t = box.table
    t.add(PID)
    t.privileged = True
    memo = {}
    yes = lambda proc, nice: True
    assert lowres._psutil_priority([PID], True, posix=True, proc_factory=t.factory,
                                   can_restore=yes, memo=memo) == []
    monkeypatch.setattr(FakeProc, "create_time", _unreadable_start_time)
    assert lowres._psutil_priority([PID], False, posix=True, proc_factory=t.factory,
                                   memo=memo) == []
    assert t.nice_of(PID) == 0 and memo == {}
