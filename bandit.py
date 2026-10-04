"""Learned model choice: which model is best for which KIND of task.

Conductor/Trinity-style, with no training: the hub keeps a Beta posterior of
answer QUALITY per (task kind, provider, model) and per (provider, model)
overall, fed by its own measured outcomes, and turns a Thompson draw from it
into a small, bounded nudge on the router's static score. A model that keeps
doing well on, say, ``coding|hard|tools|m`` rises a little for that kind of
turn only; one that keeps failing sinks a little; one nobody has measured
stays where the static tables put it (the prior Beta(1,1) nudges 0 on
average).

THE BANDIT IS A TIE-BREAKER, NOT A RANKING. Owner rule (2026-10-04): always
prioritize the best AVAILABLE models first. The benchmarks (Artificial
Analysis, LMArena) and the owner's preference floors set the order; the
nudge is at most ``MAX_NUDGE`` = 1.0 point, so two models can swap only when
their static scores are within 2 points -- the router's top band
(app.py ``_AUTO_TOP_BAND`` = 2.0). A learned favourite never lifts a weaker
model over a clearly stronger one.

Interface (other modules code against exactly this):

- ``task_kind(category, difficulty, tools, est_tokens)`` -> e.g.
  ``"coding|hard|tools|m"`` (category None/"all" -> "any"; size band
  "s" < 12K tokens, "m" < 60K, "l" beyond).
- ``Bandit.nudge(kind, pid, model, base_score)`` -> base_score + delta,
  ``|delta| <= MAX_NUDGE``. The draw comes from the kind posterior once it
  holds >= ``MIN_KIND_OBS`` observations, else from the model's global
  posterior, else from the prior.
- ``Bandit.propensity_note(kind, pid, model)`` -> ``{"alpha", "beta", "n",
  "source"}`` with source "kind" | "model" | "prior": the posterior a nudge
  for that triple would draw from (log it next to the routing decision).
- ``Bandit.reward(kind, pid, model, value)`` with value in {0, 0.5, 1};
  anything else is ignored.
- ``Bandit.remember_tool_calls(call_ids, pid, model, kind)`` +
  ``Bandit.credit_from_messages(messages, grade)``: delayed credit for a tool
  turn. The hub remembers which model emitted each tool call id (LRU 5000,
  TTL 2 h); when a later request carries the role "tool" result for that id,
  ``grade(tool_message)`` (0 / 0.5 / 1, or None = cannot tell, skip) is
  rewarded ONCE per id -- the conversation history repeats every turn, an id
  is never credited twice.
- ``Bandit.stats(limit)``, ``Bandit.save()`` / ``Bandit.load()`` (json at
  ``path``, atomic replace, autosave throttled to one write per
  ``SAVE_MIN_INTERVAL`` seconds, never raises), ``default`` (in memory until
  ``configure(path)``).

REWARD VALUES ARE ANSWER QUALITY ONLY. A 429, a provider outage, a member the
hub stopped waiting for (abandoned swarm/hedge member), a client that went
away, a deadline cut -- none of these says anything about how good the model
is at this kind of task, so callers must NEVER call ``reward`` for them (not
even with 0). Availability is already tracked elsewhere (reliability ledger,
throttles, dead marks); feeding it here would double-count it and teach the
bandit that busy models are bad ones.

Old evidence decays toward the prior with a half-life of ``HALF_LIFE``
(7 days), so a model that improves -- or regresses -- is re-learned.

Pure: stdlib only, no import from app / swarm / verify. Thread-safe (one
lock). Never raises from its public methods on bad input or disk trouble.
"""
import collections
import json
import math
import os
import random
import threading
import time

MAX_NUDGE = 1.0                 # score points, Thompson nudge bound: two
                                # nudges (+1 / -1) span at most the 2-point
                                # top band, never more
MIN_KIND_OBS = 3                # kind posterior used from this many obs on
HALF_LIFE = 7 * 86400.0         # seconds; evidence halves toward the prior
SMALL_BAND = 12_000             # est tokens: "s" below
MEDIUM_BAND = 60_000            # est tokens: "m" below, "l" from here
TOOL_CALL_TTL = 2 * 3600.0      # seconds a remembered tool call id lives
TOOL_CALL_MAX = 5000            # remembered tool call ids (LRU)
SAVE_MIN_INTERVAL = 30.0        # seconds between two autosaves
_MIN_EVIDENCE = 0.05            # below this a decayed entry is noise / pruned
# The kind gate counts DECAYED evidence, so "3 observations" an hour old are
# 2.99: this slack (~4 h of decay on 3 obs) keeps them counting as 3.
_KIND_OBS_SLACK = 0.05
_VALID_REWARDS = (0.0, 0.5, 1.0)
_DIFFICULTIES = ("simple", "medium", "hard")
_FILE_VERSION = 1


def task_kind(category, difficulty, tools, est_tokens) -> str:
    """The bucket a request is learned under, e.g. ``"coding|hard|tools|m"``."""
    cat = str(category or "").strip().lower().replace("|", "/")
    if not cat or cat == "all":
        cat = "any"
    diff = str(difficulty or "").strip().lower()
    if diff not in _DIFFICULTIES:
        diff = "any"
    try:
        est = int(est_tokens or 0)
    except (TypeError, ValueError):
        est = 0
    if est < SMALL_BAND:
        size = "s"
    elif est < MEDIUM_BAND:
        size = "m"
    else:
        size = "l"
    return "|".join((cat, diff, "tools" if tools else "notools", size))


def _valid_reward(value):
    """The reward as a float when it is one of 0 / 0.5 / 1, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    v = float(value)
    return v if v in _VALID_REWARDS else None


def _finite_nonneg(x):
    return (isinstance(x, (int, float)) and not isinstance(x, bool)
            and math.isfinite(x) and x >= 0)


class Bandit:
    """Beta posteriors of answer quality per (kind, pid, model) and per
    (pid, model), with Thompson-sampled bounded score nudges."""

    def __init__(self, path=None, clock=time.time, rng=None):
        self.path = path
        self._clock = clock
        self._rng = rng if rng is not None else random.Random()
        self._lock = threading.RLock()
        # key -> [successes, failures, last update time] (evidence, no prior)
        self._kind = {}         # (kind, pid, model)
        self._model = {}        # (pid, model)
        # tool call id -> {"pid", "model", "kind", "t", "credited"}
        self._calls = collections.OrderedDict()
        self._last_save = None
        self._dirty = False
        if path:
            self.load()

    # -- evidence ---------------------------------------------------------
    def _now(self):
        try:
            return float(self._clock())
        except Exception:
            return time.time()

    @staticmethod
    def _decayed(rec, now):
        """(successes, failures) of ``rec`` decayed to ``now``."""
        if not rec:
            return 0.0, 0.0
        s, f, t = rec
        factor = 0.5 ** (max(0.0, now - t) / HALF_LIFE)
        return s * factor, f * factor

    def _posterior(self, kind, pid, model, now):
        """(alpha, beta, n, source) a draw for this triple would use."""
        s, f = self._decayed(self._kind.get((kind, pid, model)), now)
        if s + f >= MIN_KIND_OBS - _KIND_OBS_SLACK:
            return 1.0 + s, 1.0 + f, s + f, "kind"
        s, f = self._decayed(self._model.get((pid, model)), now)
        if s + f >= _MIN_EVIDENCE:
            return 1.0 + s, 1.0 + f, s + f, "model"
        return 1.0, 1.0, 0.0, "prior"

    def nudge(self, kind, pid, model, base_score) -> float:
        """``base_score`` plus a Thompson-sampled delta in
        [-MAX_NUDGE, +MAX_NUDGE]; 0 on average for an unmeasured model."""
        try:
            base = float(base_score)
        except (TypeError, ValueError):
            return base_score
        try:
            with self._lock:
                a, b, _n, _src = self._posterior(
                    str(kind), str(pid), str(model), self._now())
                draw = self._rng.betavariate(a, b)
        except Exception:
            return base
        delta = (draw - 0.5) * 2.0 * MAX_NUDGE
        delta = max(-MAX_NUDGE, min(MAX_NUDGE, delta))
        return base + delta

    def propensity_note(self, kind, pid, model) -> dict:
        """The posterior a nudge for this triple draws from."""
        with self._lock:
            a, b, n, src = self._posterior(
                str(kind), str(pid), str(model), self._now())
        return {"alpha": a, "beta": b, "n": n, "source": src}

    def _reward_locked(self, kind, pid, model, v, now):
        for table, key in ((self._kind, (kind, pid, model)),
                           (self._model, (pid, model))):
            s, f = self._decayed(table.get(key), now)
            table[key] = [s + v, f + (1.0 - v), now]
        self._dirty = True

    def reward(self, kind, pid, model, value) -> None:
        """File one QUALITY observation (0 / 0.5 / 1). Anything else is
        ignored. Never call it for a 429, an abandoned member or a client
        that went away -- see the module docstring."""
        v = _valid_reward(value)
        if v is None:
            return
        with self._lock:
            self._reward_locked(str(kind), str(pid), str(model), v,
                                self._now())
            self._autosave_locked()

    # -- delayed credit for tool calls -------------------------------------
    def _expire_calls_locked(self, now):
        while self._calls:
            info = next(iter(self._calls.values()))
            if now - info["t"] <= TOOL_CALL_TTL:
                break
            self._calls.popitem(last=False)

    def remember_tool_calls(self, call_ids, pid, model, kind) -> None:
        """Remember which model emitted these tool call ids (LRU
        ``TOOL_CALL_MAX``, TTL ``TOOL_CALL_TTL``)."""
        if isinstance(call_ids, str):
            call_ids = [call_ids]
        try:
            ids = [str(c) for c in (call_ids or ()) if c]
        except TypeError:
            return
        if not ids:
            return
        with self._lock:
            now = self._now()
            self._expire_calls_locked(now)
            for cid in ids:
                old = self._calls.pop(cid, None)
                self._calls[cid] = {
                    "pid": str(pid), "model": str(model), "kind": str(kind),
                    "t": now,
                    "credited": bool(old and old.get("credited")),
                }
            while len(self._calls) > TOOL_CALL_MAX:
                self._calls.popitem(last=False)

    def credit_from_messages(self, messages, grade) -> int:
        """Reward the model behind each remembered, not yet credited tool call
        whose role "tool" result is in ``messages``, with ``grade(message)``
        (0 / 0.5 / 1; None or an invalid value = skip, retried on a later
        look). Each id is credited at most once. Returns how many were."""
        if not isinstance(messages, (list, tuple)) or not callable(grade):
            return 0
        pending = []
        with self._lock:
            now = self._now()
            self._expire_calls_locked(now)
            seen = set()
            for msg in messages:
                if not isinstance(msg, dict) or msg.get("role") != "tool":
                    continue
                cid = msg.get("tool_call_id")
                if not cid or not isinstance(cid, str) or cid in seen:
                    continue
                info = self._calls.get(cid)
                if info is None or info["credited"]:
                    continue
                seen.add(cid)
                pending.append((cid, msg))
        credited = 0
        for cid, msg in pending:
            try:
                v = _valid_reward(grade(msg))
            except Exception:
                v = None
            if v is None:
                continue
            with self._lock:
                info = self._calls.get(cid)
                if info is None or info["credited"]:
                    continue
                info["credited"] = True
                self._reward_locked(info["kind"], info["pid"], info["model"],
                                    v, self._now())
                credited += 1
        if credited:
            with self._lock:
                self._autosave_locked()
        return credited

    # -- reporting ---------------------------------------------------------
    def stats(self, limit=50) -> list:
        """Top (kind, pid, model) entries by posterior mean, then evidence."""
        with self._lock:
            now = self._now()
            rows = []
            for (kind, pid, model), rec in self._kind.items():
                s, f = self._decayed(rec, now)
                n = s + f
                if n < _MIN_EVIDENCE:
                    continue
                rows.append({"kind": kind, "pid": pid, "model": model,
                             "mean": round((1.0 + s) / (2.0 + n), 4),
                             "n": round(n, 3)})
        rows.sort(key=lambda r: (-r["mean"], -r["n"], r["kind"], r["pid"],
                                 r["model"]))
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 50
        return rows[:max(0, limit)]

    def clear(self) -> None:
        """Forget every observation and remembered tool call (in memory)."""
        with self._lock:
            self._kind.clear()
            self._model.clear()
            self._calls.clear()
            self._dirty = True

    # -- persistence -------------------------------------------------------
    def _autosave_locked(self):
        if not self.path:
            return
        now = self._now()
        if self._last_save is not None and now - self._last_save < SAVE_MIN_INTERVAL:
            return
        self._save_locked(now)

    def _save_locked(self, now):
        def rows(table, width):
            out = []
            for key, rec in table.items():
                s, f = self._decayed(rec, now)
                if s + f < _MIN_EVIDENCE:
                    continue
                out.append(list(key[:width]) + [round(s, 6), round(f, 6), now])
            return out

        data = {
            "version": _FILE_VERSION,
            "saved_at": now,
            "half_life": HALF_LIFE,
            # [kind, pid, model, successes, failures, as_of]
            "kinds": rows(self._kind, 3),
            # [pid, model, successes, failures, as_of]
            "models": rows(self._model, 2),
        }
        self._last_save = now
        path = self.path
        tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
        try:
            folder = os.path.dirname(os.path.abspath(path))
            os.makedirs(folder, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, separators=(",", ":"))
            os.replace(tmp, path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False
        self._dirty = False
        return True

    def save(self, force=False) -> bool:
        """Write the posteriors to ``path`` (atomic replace). Without
        ``force`` at most one write per ``SAVE_MIN_INTERVAL`` seconds.
        Returns whether a write happened; never raises."""
        if not self.path:
            return False
        with self._lock:
            now = self._now()
            if (not force and self._last_save is not None
                    and now - self._last_save < SAVE_MIN_INTERVAL):
                return False
            return bool(self._save_locked(now))

    def load(self) -> bool:
        """Replace the posteriors with the file at ``path``. A missing,
        unreadable or corrupt file leaves an empty bandit; bad rows are
        skipped. Returns whether the file was read; never raises."""
        kinds, models = {}, {}
        ok = False
        if self.path:
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    for row in data.get("kinds") or ():
                        if (isinstance(row, list) and len(row) == 6
                                and all(isinstance(x, str) for x in row[:3])
                                and all(_finite_nonneg(x) for x in row[3:])):
                            kinds[tuple(row[:3])] = [float(row[3]),
                                                     float(row[4]),
                                                     float(row[5])]
                    for row in data.get("models") or ():
                        if (isinstance(row, list) and len(row) == 5
                                and all(isinstance(x, str) for x in row[:2])
                                and all(_finite_nonneg(x) for x in row[2:])):
                            models[tuple(row[:2])] = [float(row[2]),
                                                      float(row[3]),
                                                      float(row[4])]
                    ok = True
            except Exception:
                kinds, models, ok = {}, {}, False
        with self._lock:
            self._kind = kinds
            self._model = models
            self._dirty = False
        return ok


default = Bandit()


def configure(path) -> None:
    """Point ``default`` at a json path and load it (None = in memory only,
    empty)."""
    with default._lock:
        default.path = path or None
        default._last_save = None
        default.load()
