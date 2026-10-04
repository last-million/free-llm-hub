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
# processes (only those carrying the CALVOUN_AGENT_TURN marker). Any error,
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


def _psutil_cpu():
    """Overall CPU % of everything that is not the hub's workers or the hub."""
    import psutil
    total = psutil.cpu_percent(None)
    cores = psutil.cpu_count() or 1
    own = 0.0
    try:
        own = psutil.Process().cpu_percent(None) / cores
    except Exception:                                            # noqa: BLE001
        pass
    return max(0.0, total - own)


def _psutil_priority(pids, low):
    import psutil
    for pid in pids:
        try:
            p = psutil.Process(pid)
            if os.name == "nt":
                p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if low
                       else psutil.NORMAL_PRIORITY_CLASS)
            else:
                p.nice(10 if low else 0)
        except Exception:                                        # noqa: BLE001
            continue


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

    def _priority(self, free_gb, pids):
        if mode() == "off":
            return
        try:
            if free_gb < CRITICAL_FREE_GB and pids:
                self._prio(pids, True)
                self.low_prio = True
                self._low_pids = list(pids)
            elif self.low_prio and free_gb >= RECOVER_FREE_GB:
                self._prio(self._low_pids, False)
                self.low_prio = False
                self._low_pids = []
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
    # the run ended: give the workers' priority back
    try:
        if GOV.low_prio:
            GOV._prio(GOV._low_pids, False)
            GOV.low_prio = False
    except Exception:                                            # noqa: BLE001
        pass


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
