"""Low-resource mode: keep the hub usable on weak machines.

MEASURED 2026-09-27 on the dev PC (40 GB / 8 cores): hub idle 137 MB, ~0-1%
CPU; every /agent or multi-session worker is a Node CLI process of 150-400 MB,
and a multi run starts up to swarm_windows.MAX_CONCURRENT (4) of them at once
-- 1-2 GB, which is what sinks a 4-8 GB laptop. The shared Playwright server
stays as it is: its browser only launches when a browser tool is used.

Setting `low_resource_mode`: "auto" (default) | "on" | "off".
  auto -- on when the machine is weak (< WEAK_RAM_GB total RAM or
          < WEAK_CORES logical cores).
Whatever the mode, workers() also drops to 1 while free RAM is under
LOW_FREE_GB, so a big machine that is busy elsewhere is protected too
("off" disables that guard as well -- the user asked for full speed).

A leaf module: psutil and config only, both optional (fails open to "not
weak", i.e. the old behaviour).
"""
import os
import time

MODES = ("auto", "on", "off")
WEAK_RAM_GB = 8.0
WEAK_CORES = 4
LOW_FREE_GB = 1.5
SMALL_RAM_GB = 6.0     # under this, one worker at a time when the mode is active


_CACHE = {"at": 0.0, "value": None}
CACHE_SECONDS = 5.0    # the scheduler asks many times a second; RAM moves slower


def machine():
    """{total_gb, free_gb, cores} -- None for anything that cannot be read.
    Cached CACHE_SECONDS."""
    now = time.monotonic()
    if _CACHE["value"] is not None and now - _CACHE["at"] < CACHE_SECONDS:
        return dict(_CACHE["value"])
    _CACHE["value"] = _read_machine()
    _CACHE["at"] = now
    return dict(_CACHE["value"])


def _read_machine():
    total = free = None
    try:
        import psutil
        vm = psutil.virtual_memory()
        total, free = vm.total / 1024 ** 3, vm.available / 1024 ** 3
    except Exception:                                            # noqa: BLE001
        pass
    return {"total_gb": round(total, 1) if total else None,
            "free_gb": round(free, 1) if free else None,
            "cores": os.cpu_count()}


def is_weak(m=None):
    m = m or machine()
    return bool((m.get("total_gb") and m["total_gb"] < WEAK_RAM_GB)
                or (m.get("cores") and m["cores"] < WEAK_CORES))


def mode():
    try:
        import config
        v = config.get_setting("low_resource_mode", "auto")
    except Exception:                                            # noqa: BLE001
        v = "auto"
    return v if v in MODES else "auto"


def active(m=None):
    md = mode()
    if md == "on":
        return True
    if md == "off":
        return False
    return is_weak(m)


def workers(default, m=None):
    """How many CLI workers may run at once: `default` when the mode is off
    and RAM is fine; 1-2 when low-resource mode is active; 1 while free RAM
    is short (unless the mode is "off")."""
    m = m or machine()
    n = default
    if active(m):
        n = min(n, 1 if (m.get("total_gb") or 0) < SMALL_RAM_GB else 2)
    if mode() != "off" and m.get("free_gb") is not None and m["free_gb"] < LOW_FREE_GB:
        n = 1
    return max(1, n)


# --------------------------------------------------------------------------- #
# The RAM governor (owner, 2026-10-04): the user's own programs come first.
# --------------------------------------------------------------------------- #
# The hub measures free RAM LIVE while a Multi run is active and only STARTS
# a helper when what is left after the user's reserve pays for one more at its
# MEASURED cost. It never kills, suspends or loses a running helper: over
# budget means "do not start the next one" (queued phases wait). Below a
# critical floor it also lowers the OS priority of the hub's own worker
# processes (only those carrying the CALVOUN_AGENT_TURN marker); on Linux /
# macOS only a worker it can raise back later (see _can_restore_nice), and a
# restore the OS refuses is kept and retried, never reported as done. Any error,
# no psutil, a stale monitor or mode "off" = headroom() is None = the old
# behaviour (no live limit at all).
RESERVE_MIN_GB = 3.0
RESERVE_SHARE = 0.20
DEFAULT_HELPER_GB = 0.5          # until a helper's real tree has been measured
HELPER_FLOOR_GB = 0.2
SAMPLES = 10                     # per-helper RSS samples, p90 of the last 10
TICK_SECONDS = 2.0
RAISE_AFTER = 20.0               # a higher allowance must hold this long
CRITICAL_FREE_GB = 1.0           # below: lower the workers' OS priority
RECOVER_FREE_GB = 1.5            # at/above: restore it
CPU_HOT_PCT = 90.0
CPU_HOT_SECONDS = 10.0
UNACCOUNTED_SECONDS = 15.0       # a fresh spawn has not used its RAM yet
STALE_SECONDS = 10.0             # a monitor that stopped ticking = no limit
MARKER = "CALVOUN_AGENT_TURN"
_CANDIDATE_NAMES = ("node", "bun", "python", "opencode", "codex", "claude", "kimi",
                    "cmd", "powershell", "pwsh", "sh", "bash", "qwen", "aider",
                    "deno", "npm", "npx", "git")


def reserve_gb(m=None):
    """RAM kept for the user's other programs: setting multi_ram_reserve_gb
    ("auto" or a number of GB), auto = max(3 GB, 20% of total RAM)."""
    m = m or machine()
    total = m.get("total_gb") or 0.0
    try:
        import config
        v = config.get_setting("multi_ram_reserve_gb", "auto")
    except Exception:                                            # noqa: BLE001
        v = "auto"
    if v not in (None, "", "auto"):
        try:
            return max(0.0, min(float(v), total or float(v)))
        except (TypeError, ValueError):
            pass
    return round(max(RESERVE_MIN_GB, RESERVE_SHARE * total), 1)


def ram_cores_cap(default, m=None):
    """The most helpers this machine's RAM (at DEFAULT_HELPER_GB each) and
    cores allow. `default` when the numbers are unreadable or mode is off."""
    m = m or machine()
    if mode() == "off" or m.get("free_gb") is None:
        return default
    n = int(m["free_gb"] / DEFAULT_HELPER_GB)
    if m.get("cores"):
        n = min(n, int(m["cores"]))
    return max(1, n)


def _p90(values):
    xs = sorted(values)
    return xs[min(len(xs) - 1, int(0.9 * (len(xs) - 1) + 0.5))]


def _psutil_procs():
    """[{pid, ppid, rss_gb, marker, cpu}] for processes that may be helpers.
    The marker is read from the environment (cached per pid+create time)."""
    import psutil
    out = []
    cache = _ENV_CACHE
    for p in psutil.process_iter(["pid", "ppid", "name", "create_time"]):
        try:
            info = p.info
            name = (info.get("name") or "").lower()
            key = (info["pid"], info.get("create_time"))
            hit = cache.get(key)
            if hit is None:
                if not name.startswith(_CANDIDATE_NAMES):
                    continue
                try:
                    hit = MARKER in p.environ()
                except Exception:                                # noqa: BLE001
                    hit = False
                if len(cache) > 4000:
                    cache.clear()
                cache[key] = hit
            if not hit:
                continue
            out.append({"pid": info["pid"], "ppid": info.get("ppid"),
                        "rss_gb": p.memory_info().rss / 1024 ** 3,
                        "marker": True, "cpu": p.cpu_percent(None)})
        except Exception:                                        # noqa: BLE001
            continue
    return out


_ENV_CACHE = {}


_CPU_STATE = {}                 # pid -> psutil.Process, kept across ticks (see _hub_own_cpu)


def _hub_own_cpu(cores, proc_factory=None, children_of=None):
    """CPU % of the hub process AND its descendants (the agent / worker CLIs it
    spawns), divided by `cores`, using PERSISTENT psutil handles.

    psutil's cpu_percent(None) measures since the handle's LAST call, so a FRESH
    Process() every tick always reads 0.0 -- the bug this replaces, which made
    _psutil_cpu leave `own` at 0 and count the hub's own workers as "other apps",
    so a CPU-heavy Multi run tripped the governor's CPU hold against itself. A
    newly seen pid is primed this tick and counted only from the next one (its
    first reading is meaningless). Injectable for tests; never raises."""
    if proc_factory is None:
        import psutil
        proc_factory = psutil.Process
    if children_of is None:
        children_of = lambda p: p.children(recursive=True)
    hub = os.getpid()
    try:
        pids = {hub} | {k.pid for k in children_of(proc_factory(hub))}
    except Exception:                                            # noqa: BLE001
        pids = {hub}
    primed = set(_CPU_STATE)        # only pids seen BEFORE this tick have a valid delta
    for pid in pids:
        if pid not in _CPU_STATE:
            try:
                p = proc_factory(pid)
                p.cpu_percent(None)                 # prime; this reading is discarded
                _CPU_STATE[pid] = p
            except Exception:                        # noqa: BLE001
                pass
    for pid in list(_CPU_STATE):
        if pid not in pids:
            _CPU_STATE.pop(pid, None)                # process gone: drop its handle
    tot = 0.0
    for pid in list(primed & set(_CPU_STATE)):
        try:
            tot += float(_CPU_STATE[pid].cpu_percent(None))
        except Exception:                            # noqa: BLE001
            _CPU_STATE.pop(pid, None)
    return tot / max(1, int(cores or 1))


def _psutil_cpu():
    """Overall CPU % of everything that is NOT the hub or the agent CLIs it
    spawned, so a Multi run's own helpers never trip the CPU hold."""
    import psutil
    total = psutil.cpu_percent(None)
    cores = psutil.cpu_count() or 1
    try:
        own = _hub_own_cpu(cores)
    except Exception:                                            # noqa: BLE001
        own = 0.0
    return max(0.0, total - own)


LOW_NICE = 10                   # POSIX niceness a worker gets below the floor
CAP_SYS_NICE = 23               # Linux capability bit (linux/capability.h)
_ORIG_NICE = {}                 # (pid, create_time) -> niceness before the hub lowered it


def _read_cap_eff(path="/proc/self/status"):
    """The hub's effective capability mask on Linux, or None (other OS, any
    error)."""
    try:
        with open(path, "r", encoding="ascii", errors="replace") as f:
            for line in f:
                if line.startswith("CapEff:"):
                    return int(line.split(":", 1)[1].strip(), 16)
    except Exception:                                            # noqa: BLE001
        pass
    return None


def _init_user_ns(path="/proc/self/uid_map"):
    """False when the hub runs inside a non-initial Linux user namespace
    (rootless containers): capabilities held there do not count for
    setpriority, which the kernel checks against the initial namespace. True
    for the full identity map, or when the map cannot be read (no user
    namespaces)."""
    try:
        with open(path, "r", encoding="ascii", errors="replace") as f:
            rows = [ln.split() for ln in f.read().splitlines() if ln.strip()]
    except Exception:                                            # noqa: BLE001
        return True
    return rows == [["0", "0", "4294967295"]]


def _rlimit_nice(proc):
    """Soft RLIMIT_NICE of `proc` (Linux; the hub's own as a fallback, which a
    child inherits), float('inf') for unlimited, None when this OS has none
    (macOS, Windows) or it cannot be read."""
    try:
        import psutil
        if hasattr(psutil, "RLIMIT_NICE") and hasattr(proc, "rlimit"):
            soft = proc.rlimit(psutil.RLIMIT_NICE)[0]
            return float("inf") if soft == psutil.RLIM_INFINITY or soft < 0 else soft
    except Exception:                                            # noqa: BLE001
        pass
    try:
        import resource
        soft = resource.getrlimit(resource.RLIMIT_NICE)[0]
        return float("inf") if soft == resource.RLIM_INFINITY or soft < 0 else soft
    except Exception:                                            # noqa: BLE001
        return None


def _can_restore_nice(proc, nice, geteuid=None, cap_eff=None, rlimit_nice=None,
                      init_userns=None):
    """True when the hub may later set `proc` back to niceness `nice` after
    lowering it. A POSIX process may RAISE a niceness but not lower it again
    (setpriority answers EACCES) without privilege. Linux (kernel can_nice):
    CAP_SYS_NICE in the initial user namespace, or the process's RLIMIT_NICE
    soft limit allowing that value (20 - nice <= rlim_cur) -- the uid alone
    does not count (root in a default Docker container lacks CAP_SYS_NICE).
    macOS / BSD (no capability set to read): root only, or an RLIMIT_NICE
    where the OS has one. Every input is injectable (tests); unknown or any
    error = False, so the worker is simply not lowered. Never raises."""
    try:
        caps = (cap_eff or _read_cap_eff)()
        if caps is not None:
            if caps & (1 << CAP_SYS_NICE) and (init_userns or _init_user_ns)():
                return True
        else:
            ge = geteuid if geteuid is not None else getattr(os, "geteuid", None)
            if ge is not None and ge() == 0:
                return True
        soft = (rlimit_nice or _rlimit_nice)(proc)
        if soft is not None and 20 - int(nice) <= soft:
            return True
    except Exception:                                            # noqa: BLE001
        pass
    return False


def _forget_pid(memo, pid, keep=None):
    """Drop the memo entries of `pid`. With `keep` (this process's own key, its start
    time known) only an OLDER process with that pid goes: an entry whose start time
    could not be read may be this very process, so it stays."""
    for k in [k for k in memo if k[0] == pid and k != keep
              and (keep is None or k[1] is not None)]:
        memo.pop(k, None)


def _memo_key_for(memo, pid, ctime):
    """The key the hub lowered `pid` under, tolerant of a start time read on one side
    only (lowering vs restore): the exact key, else the pid's unread-time entry, else
    -- the time unread now -- the pid's only entry. None when the hub never lowered it,
    or when it cannot tell which entry is this process."""
    if (pid, ctime) in memo:
        return (pid, ctime)
    if ctime is not None:
        return (pid, None) if (pid, None) in memo else None
    keys = [k for k in memo if k[0] == pid]
    return keys[0] if len(keys) == 1 else None


def _proc_gone(exc):
    """The process no longer exists: nothing is left to lower or restore."""
    try:
        import psutil
        if isinstance(exc, psutil.NoSuchProcess):
            return True
    except Exception:                                            # noqa: BLE001
        pass
    return isinstance(exc, ProcessLookupError)


def _posix_priority(pids, low, proc_factory=None, can_restore=None, memo=None):
    """POSIX half of _psutil_priority: lower a worker to LOW_NICE only when the
    hub can raise it back later, restore each to the niceness it had before. A
    worker found at exactly LOW_NICE while the hub holds lowered workers is a
    process one of them started (a child inherits its parent's niceness): it is
    the hub's to give back too. Returns the pids it did not change. Never raises."""
    pids = list(pids or [])
    try:
        if proc_factory is None:
            import psutil
            proc_factory = psutil.Process
    except Exception:                                            # noqa: BLE001
        return pids                                  # nothing could be changed
    can_restore = can_restore or _can_restore_nice
    memo = _ORIG_NICE if memo is None else memo
    missed = []
    for pid in pids:
        try:
            p = proc_factory(pid)
            try:
                ctime = p.create_time()
            except Exception as e:                               # noqa: BLE001
                if _proc_gone(e):
                    raise
                ctime = None
            key = (pid, ctime)
            if ctime is not None:
                _forget_pid(memo, pid, keep=key)     # an older process with this pid is gone
            mk = _memo_key_for(memo, pid, ctime)
            if low:
                if mk is not None:
                    continue                         # lowered earlier, still the hub's to give back
                cur = p.nice()
                if cur >= LOW_NICE:
                    # Already this low. At exactly LOW_NICE while the hub holds lowered
                    # workers, it was started by one of them and inherited the level:
                    # the hub's to give back, to the highest original niceness of the
                    # workers it lowered (never above any of them). Niced lower by
                    # someone else, or a level the hub may not set: left alone.
                    base = max(memo.values()) if memo else None
                    if (cur == LOW_NICE and base is not None and base < cur
                            and can_restore(p, base)):
                        memo[key] = base
                    else:
                        missed.append(pid)
                    continue
                if not can_restore(p, cur):
                    missed.append(pid)               # could not be given back: never lowered
                    continue
                p.nice(LOW_NICE)
                memo[key] = cur
            else:
                if mk is None:
                    continue                         # the hub never lowered this process
                p.nice(memo[mk])
                memo.pop(mk, None)
        except Exception as e:                                   # noqa: BLE001
            if _proc_gone(e):
                _forget_pid(memo, pid)
                continue
            missed.append(pid)
    return missed


def _psutil_priority(pids, low, posix=None, proc_factory=None, can_restore=None,
                     memo=None):
    """Lower (low=True) or restore the OS priority of the worker `pids`.

    Returns the pids it could NOT change ([] = all done; a process that is gone
    counts as done). Windows: BELOW_NORMAL / NORMAL exactly as before, never a
    failure reported (a process's owner can always set it back to NORMAL).
    POSIX (`posix`, default: not Windows): see _posix_priority -- an
    unprivileged process cannot lower a niceness again, so a worker is lowered
    only when the restore is possible, and a refused restore is reported."""
    if posix is None:
        posix = os.name != "nt"
    if posix:
        return _posix_priority(pids, low, proc_factory=proc_factory,
                               can_restore=can_restore, memo=memo)
    import psutil
    for pid in pids:
        try:
            p = psutil.Process(pid)
            p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if low
                   else psutil.NORMAL_PRIORITY_CLASS)
        except Exception:                                        # noqa: BLE001
            continue
    return []


class Governor:
    """Live RAM / CPU governor. All inputs are injectable (tests)."""

    def __init__(self, read_machine=None, clock=time.monotonic, procs=None,
                 cpu=None, set_priority=None):
        self._read = read_machine or (lambda: _read_machine())
        self._clock = clock
        self._procs = procs or _psutil_procs
        self._cpu = cpu or _psutil_cpu
        self._prio = set_priority or _psutil_priority
        self._lock = __import__("threading").Lock()
        self.reset()

    def reset(self):
        self.samples = []
        self.allowed = None            # new helpers permitted now (hysteresis'd)
        self.raw = None
        self.helpers = 0
        self.free_gb = None
        self.reserve = None
        self.last_tick = None
        self.limited_by = None
        self.failed = False
        self.spawns = []
        self.debit = 0                 # spawns since the last tick (not in raw yet)
        self._higher_since = None
        self._higher_min = None
        self._hot_since = None
        self.cpu_hold = False
        self.low_prio = False
        self._low_pids = []
        self.prio_unrestored = []      # lowered workers whose restore the OS refused
        self.nonhub_cpu = None

    # -- measurements ------------------------------------------------------
    def per_helper_gb(self):
        if not self.samples:
            return DEFAULT_HELPER_GB
        return max(HELPER_FLOOR_GB, round(_p90(self.samples[-SAMPLES:]), 3))

    def _measure_helpers(self, procs):
        pids = {p["pid"] for p in procs}
        roots = [p for p in procs if p.get("ppid") not in pids]
        by_root = {}
        for p in procs:
            cur, seen = p, 0
            while cur.get("ppid") in pids and seen < 50:
                cur = next(q for q in procs if q["pid"] == cur["ppid"])
                seen += 1
            by_root[cur["pid"]] = by_root.get(cur["pid"], 0.0) + p["rss_gb"]
        for r in roots:
            self.samples.append(by_root.get(r["pid"], r["rss_gb"]))
        del self.samples[:-SAMPLES]
        self.helpers = len(roots)
        return sorted(pids)

    # -- the loop body -----------------------------------------------------
    def tick(self):
        """One measurement + decision. Never raises; a failure sets
        `failed`, which makes headroom() None (the old behaviour)."""
        try:
            now = self._clock()
            m = self._read()
            if m.get("free_gb") is None or not m.get("total_gb"):
                raise RuntimeError("machine numbers unavailable")
            procs = self._procs()
            with self._lock:
                pids = self._measure_helpers(procs) if procs else (
                    setattr(self, "helpers", 0) or [])
                self.free_gb = m["free_gb"]
                self.reserve = reserve_gb(m)
                self.spawns = [t for t in self.spawns
                               if now - t < UNACCOUNTED_SECONDS]
                cost = self.per_helper_gb()
                room = m["free_gb"] - self.reserve
                raw = max(0, int(room / cost) - len(self.spawns))
                self.raw = raw
                self._hysteresis(raw, now)
                self._cpu_check(now)
                self._priority(m["free_gb"], pids)
                self.limited_by = ("cpu" if self.cpu_hold else
                                   "ram" if (self.allowed or 0) <= 0 else None)
                self.failed = False
                self.debit = 0
                self.last_tick = now
        except Exception:                                        # noqa: BLE001
            self.failed = True
            self.last_tick = self._clock()

    def _hysteresis(self, raw, now):
        if self.allowed is None or raw <= self.allowed:
            self.allowed = raw                      # lower (or first) at once
            self._higher_since = self._higher_min = None
            return
        if self._higher_since is None:
            self._higher_since, self._higher_min = now, raw
        self._higher_min = min(self._higher_min, raw)
        if now - self._higher_since >= RAISE_AFTER:
            self.allowed = self._higher_min
            self._higher_since = self._higher_min = None

    def _cpu_check(self, now):
        try:
            pct = self._cpu()
        except Exception:                                        # noqa: BLE001
            pct = None
        self.nonhub_cpu = pct
        if pct is not None and pct > CPU_HOT_PCT:
            if self._hot_since is None:
                self._hot_since = now
            self.cpu_hold = now - self._hot_since >= CPU_HOT_SECONDS
        else:
            self._hot_since = None
            self.cpu_hold = False

    def _apply(self, pids, low):
        """Run the priority hook; the set of pids it reports it did NOT change.
        A hook that returns None (the old contract, tests' own fakes) = all
        changed, i.e. the old behaviour."""
        res = self._prio(pids, low)
        if res is None:
            return set()
        try:
            return set(res)
        except Exception:                                        # noqa: BLE001
            return set()

    def _priority(self, free_gb, pids):
        if mode() == "off":
            return
        try:
            if free_gb < CRITICAL_FREE_GB and pids:
                missed = self._apply(pids, True)
                prev = set(self._low_pids)
                # held low by the hub: what it lowered now or before, plus a
                # worker whose restore was refused (still low, still to restore)
                held = [p for p in pids if p not in missed or p in prev]
                held += [p for p in self.prio_unrestored if p not in held]
                self._low_pids = held
                self.low_prio = bool(held)
            elif self.low_prio and free_gb >= RECOVER_FREE_GB:
                self._restore_locked()
        except Exception:                                        # noqa: BLE001
            pass

    def _restore_locked(self):
        """Give the held workers their priority back. A worker the OS refused
        to restore stays held (low_prio stays True) and is tried again on a
        later tick: the governor never reports a restore it did not get."""
        missed = self._apply(self._low_pids, False)
        left = [p for p in self._low_pids if p in missed]
        self._low_pids = left
        self.prio_unrestored = list(left)
        self.low_prio = bool(left)

    def restore_priority(self):
        """The run ended: give the workers' priority back. Never raises."""
        try:
            with self._lock:
                if self.low_prio:
                    self._restore_locked()
        except Exception:                                        # noqa: BLE001
            pass

    # -- what the scheduler asks -------------------------------------------
    def fresh(self):
        return (self.last_tick is not None and not self.failed
                and self._clock() - self.last_tick <= STALE_SECONDS)

    def headroom(self):
        """How many NEW helpers may start now, or None = no live limit."""
        if mode() == "off" or not self.fresh():
            return None
        if self.cpu_hold:
            return 0
        return max(0, (self.allowed or 0) - self.debit)

    def note_spawn(self):
        with self._lock:
            self.spawns.append(self._clock())
            self.debit += 1


GOV = Governor()
_mon = {"users": 0, "thread": None}
_mon_lock = __import__("threading").Lock()


def acquire_monitor():
    """A Multi run is active: keep the shared monitor thread ticking."""
    with _mon_lock:
        _mon["users"] += 1
        t = _mon["thread"]
        if t is None or not t.is_alive():
            t = __import__("threading").Thread(target=_monitor_loop, daemon=True,
                                               name="lowres-governor")
            _mon["thread"] = t
            t.start()


def release_monitor():
    with _mon_lock:
        _mon["users"] = max(0, _mon["users"] - 1)


def _monitor_loop():
    while True:
        with _mon_lock:
            if _mon["users"] <= 0:
                _mon["thread"] = None
                break
        try:
            GOV.tick()
        except Exception:                                        # noqa: BLE001
            pass
        time.sleep(TICK_SECONDS)
    # the run ended: give the workers' priority back (a refused restore stays
    # held and is retried by the next run's ticks)
    GOV.restore_priority()


def status():
    m = machine()
    g = GOV
    live = g.fresh()
    return {"mode": mode(), "active": active(m), "weak_machine": is_weak(m),
            "machine": m, "workers": workers(4, m),
            "reserve_gb": reserve_gb(m),
            "per_helper_gb": g.per_helper_gb(),
            "allowed_now": g.headroom() if live else None,
            "limited_by": g.limited_by if live else None,
            "thresholds": {"weak_ram_gb": WEAK_RAM_GB, "weak_cores": WEAK_CORES,
                           "low_free_gb": LOW_FREE_GB}}
