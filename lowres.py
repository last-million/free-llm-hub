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


def status():
    m = machine()
    return {"mode": mode(), "active": active(m), "weak_machine": is_weak(m),
            "machine": m, "workers": workers(4, m),
            "thresholds": {"weak_ram_gb": WEAK_RAM_GB, "weak_cores": WEAK_CORES,
                           "low_free_gb": LOW_FREE_GB}}
