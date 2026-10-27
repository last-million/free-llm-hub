"""Rate-aware provider spreading -- the pure half (stdlib only, never raises).

WHY. The fairness layer (app._fair_spread_band) spreads sessions by in-flight
count and routing share, and quota.py hard-gates a provider at 100% of its
budget. Nothing compared how many requests a provider got in the LAST SECOND or
MINUTE with the rate limit it is known to have: quota counts a request only
after its response headers arrive (so N hops started together all see "0 used"),
in fixed calendar-minute buckets, and says nothing below 100%. Parallel helpers
therefore pile onto one low-limit provider (llm7 20/min, g4f 5/min, a "1 req/s"
host) and its 429s block everyone while equally good providers idle.

WHAT THIS MODULE IS. A rolling per-provider counter of requests STARTED in the
last 1 s / 60 s (`RateWatch`, injectable clock, thread-safe, bounded) plus three
pure decision helpers that app.py wires in:

  * `headroom(n1, n60, per_min, per_sec)` -- the fraction of the TIGHTEST known
    limit still free, or None when no limit is known (then nothing changes);
  * `narrow(entries, pressed_fn)` -- drop the entries of providers at >= 80% of
    their limit, but ONLY when at least one entry survives (fail-open);
  * `walk_index(...)` -- the index to pop next in a fallback walk: the head
    unless its provider is at its limit, in which case the first LATER entry that
    has headroom, is as healthy (same group signature) and scores within the
    quality band of the head.

The owner's rule is preserved by construction: these helpers never introduce an
entry, never reorder anything but "a pressed head steps behind a comparable
alternative", and the caller supplies the quality band, so a weaker-than-band
model can never be promoted. Everything fails open: any error returns the input
unchanged.
"""
from __future__ import annotations

import collections
import threading
import time

WINDOW_SECONDS = 60.0        # the rolling "minute"
SECOND = 1.0                 # the rolling "second"
YIELD_AT = 0.80              # a provider at >= this fraction of its limit yields
MAX_EVENTS = 4096            # per provider; overflow DROPS the oldest = undercounts
MAX_PROVIDERS = 256          # distinct provider ids tracked at once
_EPS = 1e-9


class RateWatch:
    """Per-provider timestamps of requests STARTED, for the last minute.

    Memory is bounded twice: each provider keeps at most `max_events` stamps
    (a full deque silently drops its oldest, which can only UNDERcount -- toward
    "more headroom", i.e. fail-open) and at most `max_providers` providers are
    tracked (an idle one is dropped first). Stamps older than a minute are
    pruned on every read and write. One lock; every method is safe to call from
    any thread and never raises."""

    def __init__(self, clock=None, max_events=MAX_EVENTS, max_providers=MAX_PROVIDERS):
        self._clock = clock or time.monotonic
        self._max_events = max(8, int(max_events))
        self._max_providers = max(4, int(max_providers))
        self._lock = threading.Lock()
        self._ev = {}              # pid -> deque[(t, model)]
        self._logged = {}          # log key -> last time it was logged

    # -- bookkeeping --------------------------------------------------------- #

    def clear(self):
        """Forget everything (tests; also what a hub restart does)."""
        with self._lock:
            self._ev.clear()
            self._logged.clear()

    def _now(self):
        try:
            return float(self._clock())
        except Exception:                                        # noqa: BLE001
            return time.monotonic()

    def _prune(self, dq, now):
        cutoff = now - WINDOW_SECONDS
        while dq and dq[0][0] <= cutoff:
            dq.popleft()

    # -- writes -------------------------------------------------------------- #

    def note_start(self, pid, model=None):
        """One request to `pid` (optionally for `model`) is starting now."""
        if not pid:
            return
        try:
            now = self._now()
            with self._lock:
                dq = self._ev.get(pid)
                if dq is None:
                    if len(self._ev) >= self._max_providers:
                        self._evict_one(now)
                    dq = collections.deque(maxlen=self._max_events)
                    self._ev[pid] = dq
                self._prune(dq, now)
                dq.append((now, model if isinstance(model, str) else ""))
        except Exception:                                        # noqa: BLE001
            pass

    def _evict_one(self, now):
        """Make room: drop an idle provider, else the one quiet the longest."""
        for p, dq in list(self._ev.items()):
            self._prune(dq, now)
            if not dq:
                self._ev.pop(p, None)
                return
        oldest = min(self._ev, key=lambda p: self._ev[p][-1][0], default=None)
        if oldest is not None:
            self._ev.pop(oldest, None)

    # -- reads --------------------------------------------------------------- #

    @staticmethod
    def _tally(dq, now, model=None):
        n1 = n60 = 0
        edge = now - SECOND
        for t, m in dq:
            if model is not None and m != model:
                continue
            n60 += 1
            if t > edge:
                n1 += 1
        return n1, n60

    def counts(self, pid, model=None):
        """(started in the last 1 s, started in the last 60 s) for `pid`, or
        for one `model` of it when given. (0, 0) for an unseen provider."""
        try:
            now = self._now()
            with self._lock:
                dq = self._ev.get(pid)
                if not dq:
                    return 0, 0
                self._prune(dq, now)
                return self._tally(dq, now, model)
        except Exception:                                        # noqa: BLE001
            return 0, 0

    def busy(self):
        """{pid: (n1, n60)} for every provider with a start in the last minute.
        Empty on an idle hub, which is how callers skip all the work."""
        out = {}
        try:
            now = self._now()
            with self._lock:
                for pid, dq in list(self._ev.items()):
                    self._prune(dq, now)
                    if not dq:
                        self._ev.pop(pid, None)
                        continue
                    out[pid] = self._tally(dq, now)
        except Exception:                                        # noqa: BLE001
            return {}
        return out

    def per_model(self, pid):
        """{model: (n1, n60)} for `pid` (models with a start in the last minute)."""
        out = {}
        try:
            now = self._now()
            with self._lock:
                dq = self._ev.get(pid)
                if not dq:
                    return {}
                self._prune(dq, now)
                for m in {m for _t, m in dq}:
                    out[m] = self._tally(dq, now, m)
        except Exception:                                        # noqa: BLE001
            return {}
        return out

    def should_log(self, key, interval=5.0):
        """True at most once per `interval` seconds for `key`, so a burst of
        picks that all yield the same provider writes ONE log line."""
        try:
            now = self._now()
            with self._lock:
                last = self._logged.get(key)
                if last is not None and now - last < float(interval):
                    return False
                if len(self._logged) > 512:
                    self._logged.clear()
                self._logged[key] = now
                return True
        except Exception:                                        # noqa: BLE001
            return True


# --------------------------------------------------------------------------- #
# Pure decisions.
# --------------------------------------------------------------------------- #
def _pos_int(v):
    return v if (isinstance(v, int) and not isinstance(v, bool) and v > 0) else None


def headroom(n1, n60, per_min=None, per_sec=None):
    """Fraction (0..1) of the TIGHTEST known limit still free, given `n1`
    requests started in the last second and `n60` in the last minute. None when
    neither limit is a positive integer: an unknown limit must never be read as
    "full" or "empty", so callers treat None as "no effect"."""
    fracs = []
    pm, ps = _pos_int(per_min), _pos_int(per_sec)
    if pm:
        fracs.append(1.0 - float(n60) / pm)
    if ps:
        fracs.append(1.0 - float(n1) / ps)
    if not fracs:
        return None
    return max(0.0, min(1.0, min(fracs)))


def is_pressed(h, yield_at=YIELD_AT):
    """True when headroom `h` says the provider is at >= `yield_at` of a limit."""
    return h is not None and h <= (1.0 - float(yield_at)) + _EPS


def narrow(entries, pressed_fn):
    """`entries` without those whose provider is pressed -- but ONLY when that
    leaves at least one and actually removes something. Everyone pressed (or
    nobody) returns the input unchanged: yielding is a preference among equally
    good alternatives, never a reason to refuse a request. Returns
    (kept, removed). Never raises."""
    entries = list(entries or ())
    try:
        kept = [e for e in entries if not pressed_fn(e)]
        if kept and len(kept) < len(entries):
            return kept, [e for e in entries if e not in kept]
    except Exception:                                            # noqa: BLE001
        pass
    return entries, []


def walk_index(rest, i, pressed_fn, score_fn, sig_fn, band, ok_fn=None):
    """Which index of `rest` a fallback walk should pop next.

    `i` is what the walk would pop. If that head's provider is NOT pressed, or
    nothing better exists, `i` comes back unchanged. Otherwise the first LATER
    entry that (a) is not pressed, (b) is acceptable to the caller (`ok_fn`:
    not stalled, not benched ...), (c) has the SAME group signature as the head
    (so a healthy pair is never leapfrogged by a sick, low-quality or
    junk-benched one), and (d) scores within `band` of the head (so it is an
    equally good model, never a weaker one). Pure: it changes which index is
    popped, nothing is dropped or added."""
    try:
        if not 0 <= i < len(rest) - 1:
            return i
        head = rest[i]
        if not pressed_fn(head):
            return i
        s0, g0 = score_fn(head), sig_fn(head)
        for k in range(i + 1, len(rest)):
            e = rest[k]
            if pressed_fn(e):
                continue
            if ok_fn is not None and not ok_fn(e):
                continue
            if sig_fn(e) != g0:
                continue
            if score_fn(e) >= s0 - float(band):
                return k
    except Exception:                                            # noqa: BLE001
        pass
    return i
