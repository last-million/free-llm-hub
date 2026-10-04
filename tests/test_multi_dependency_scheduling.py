r"""A Multi run starts each phase as soon as what it NEEDS has finished.

Owner, 2026-10-04: "each phase should work in parallel if it does not need to
wait for other things". The run used to walk dependency WAVES and wait for the
whole wave: in [1 slow, 2 fast, 3 needs 2], phase 3 waited for slow phase 1
only because 1 shared phase 2's wave.

Kept exactly: a failed dependency does not block its dependents, the
concurrency cap (re-read before every start), the stagger, Stop reaching the
running workers, resume running only the unfinished phases, the review last,
one manager verdict per phase.

Everything is a fake: spawn, run_turn and the manager are injected, and each
phase's duration is set by its title.
"""
import json
import re
import threading
import time

import pytest

import lowres
import swarm_windows as SW


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setenv(SW._STORE_ENV, str(tmp_path / "runs"))
    # A fake has no shared database to collide on.
    monkeypatch.setattr(SW, "SPAWN_STAGGER", 0)
    monkeypatch.setattr(SW, "RETRY_BACKOFF", 0)
    SW._RUNS.clear()
    yield
    for run in list(SW._RUNS.values()):
        run.stop_flag.set()
    SW._RUNS.clear()


def _wait(run_id, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        st = SW.status(run_id)
        if st and st["state"] in (SW.DONE, SW.FAILED, SW.STOPPED):
            return st
        time.sleep(0.02)
    raise AssertionError("run did not finish: %s" % SW.status(run_id))


def _until(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class _World:
    """Fake CLI: each phase's turn lasts durations[title] (or waits on
    gates[title]) and replies replies[title] (a queue; "" = no reply)."""

    def __init__(self, durations=None, replies=None, gates=None):
        self.durations = durations or {}
        self.replies = {k: list(v) for k, v in (replies or {}).items()}
        self.gates = gates or {}
        self.lock = threading.Lock()
        self.t0 = time.monotonic()
        self.n = 0
        self.live = 0
        self.peak = 0
        self.turns = []          # [title, start, end] in start order
        self.sessions = {}       # session id -> title
        self.prompts = {}        # title -> last prompt

    def now(self):
        return time.monotonic() - self.t0

    def spawn(self, cli, project):
        with self.lock:
            self.n += 1
            return "s%d" % self.n

    def run_turn(self, sid, prompt):
        title = prompt.split("Your phase is called: ", 1)[1].splitlines()[0]
        with self.lock:
            self.sessions[sid] = title
            self.prompts[title] = prompt
            self.live += 1
            self.peak = max(self.peak, self.live)
            row = [title, self.now(), None]
            self.turns.append(row)
        try:
            gate = self.gates.get(title)
            if gate is not None:
                gate.wait(10)
            else:
                time.sleep(self.durations.get(title, 0.05))
        finally:
            with self.lock:
                self.live -= 1
                row[2] = self.now()
        queue = self.replies.get(title)
        text = queue.pop(0) if queue else "Finished %s: wrote the files and checked them." % title
        if text:
            yield {"event": "message", "text": text}
        yield {"event": "done"}

    def first(self, title):
        return next(r for r in self.turns if r[0] == title)

    def ran(self, title):
        return [r for r in self.turns if r[0] == title]


def _phases(*rows):
    """(title, needs) pairs -> phases."""
    return [{"title": t, "task": "do " + t.lower(), "needs": list(n)} for t, n in rows]


SLOW_FAST = _phases(("Slow", ()), ("Fast", ()), ("After fast", (2,)))
DURATIONS = {"Slow": 1.2, "Fast": 0.2, "After fast": 0.3}


def _barrier_walk(run, w):
    """The OLD execution, for comparison: one wave at a time, each wave run to
    its end before the next (a wave's phases are independent, so _run_wave on
    one wave is the old wave)."""
    run.state = SW.RUNNING
    for wave in run.waves:
        SW._run_wave(run, wave, w.spawn, w.run_turn)
    run.state = SW.DONE


# --------------------------------------------------------------------------- #
# (a) a phase starts when ITS dependencies are done, not its wave
# --------------------------------------------------------------------------- #

def test_a_phase_starts_while_an_unrelated_slow_phase_still_runs():
    w = _World(durations=DURATIONS)
    rid = SW.start("g", ".", "opencode", w.spawn, w.run_turn, phases=SLOW_FAST,
                   review=False)
    st = _wait(rid)
    assert st["state"] == SW.DONE
    assert st["waves"] == [[1, 2], [3]]          # the plan as shown is unchanged
    slow, fast, after = w.first("Slow"), w.first("Fast"), w.first("After fast")
    assert after[1] >= fast[2], "phase 3 started before the phase it needs ended"
    assert after[1] < slow[2], "phase 3 waited for slow phase 1, which it does not need"
    assert after[1] - fast[2] < 0.6


def test_the_dependency_walk_is_faster_than_the_wave_barrier():
    """Measured on the same plan and the same fake durations."""
    w_old = _World(durations=DURATIONS)
    old = SW._Run("g", ".", "opencode", SW.clean_phases({"phases": SLOW_FAST}))
    t = time.monotonic()
    _barrier_walk(old, w_old)
    before = time.monotonic() - t

    w_new = _World(durations=DURATIONS)
    new = SW._Run("g", ".", "opencode", SW.clean_phases({"phases": SLOW_FAST}))
    t = time.monotonic()
    SW._walk(new, w_new.spawn, w_new.run_turn)
    after = time.monotonic() - t

    assert [a.state for a in old.agents] == [a.state for a in new.agents] == [SW.DONE] * 3
    # before ~ 1.2 + 0.3 (wave 2 waits for Slow), after ~ 1.2 (3 runs inside it)
    assert before >= 1.45
    assert after < before - 0.2, (before, after)


# --------------------------------------------------------------------------- #
# (b) the concurrency cap
# --------------------------------------------------------------------------- #

def _count_workers(monkeypatch):
    """Thread-level concurrency: a worker counts from its start to the end of
    _run_agent (verification included), which is what the cap bounds."""
    real = SW._run_agent
    state = {"live": 0, "peak": 0}
    lock = threading.Lock()

    def counted(*a, **k):
        with lock:
            state["live"] += 1
            state["peak"] = max(state["peak"], state["live"])
        try:
            return real(*a, **k)
        finally:
            with lock:
                state["live"] -= 1
    monkeypatch.setattr(SW, "_run_agent", counted)
    return state


def test_never_more_workers_than_the_cap(monkeypatch):
    monkeypatch.setattr(lowres, "workers", lambda default, m=None: 2)
    workers = _count_workers(monkeypatch)
    phases = _phases(("P1", ()), ("P2", ()), ("P3", (1,)), ("P4", (1,)), ("P5", ()))
    w = _World(durations={t: 0.25 for t in ("P1", "P2", "P3", "P4", "P5")})
    rid = SW.start("g", ".", "opencode", w.spawn, w.run_turn, phases=phases,
                   review=False)
    assert _wait(rid)["state"] == SW.DONE
    assert workers["peak"] == 2
    assert w.peak == 2
    assert len(w.turns) == 5
    # ready phases start in plan order: P1, P2 first; then the next free slot
    # goes to the lowest-numbered ready phase
    assert [r[0] for r in w.turns][:2] == ["P1", "P2"]


def test_low_resource_one_worker_runs_strictly_one_at_a_time(monkeypatch):
    monkeypatch.setattr(lowres, "workers", lambda default, m=None: 1)
    workers = _count_workers(monkeypatch)
    phases = _phases(("P1", ()), ("P2", ()), ("P3", (1,)), ("P4", ()))
    w = _World(durations={t: 0.1 for t in ("P1", "P2", "P3", "P4")})
    rid = SW.start("g", ".", "opencode", w.spawn, w.run_turn, phases=phases,
                   review=True)
    assert _wait(rid)["state"] == SW.DONE
    assert workers["peak"] == 1
    for prev, nxt in zip(w.turns, w.turns[1:]):
        assert nxt[1] >= prev[2], "two workers overlapped under a cap of 1"
    assert [r[0] for r in w.turns] == ["P1", "P2", "P3", "P4", SW.REVIEW_TITLE]


def test_the_cap_is_read_again_before_every_start(monkeypatch):
    """RAM freed mid-run: the queued phases may run side by side again."""
    caps = {"n": 1}
    monkeypatch.setattr(lowres, "workers", lambda default, m=None: caps["n"])
    gate = threading.Event()
    phases = _phases(("P1", ()), ("P2", ()), ("P3", ()), ("P4", ()))
    w = _World(durations={t: 0.3 for t in ("P2", "P3", "P4")}, gates={"P1": gate})
    rid = SW.start("g", ".", "opencode", w.spawn, w.run_turn, phases=phases,
                   review=False)
    assert _until(lambda: len(w.turns) == 1)
    time.sleep(0.2)
    assert len(w.turns) == 1                     # cap 1: the rest wait
    caps["n"] = 4
    gate.set()
    assert _wait(rid)["state"] == SW.DONE
    assert w.peak == 3                           # P2..P4 together once P1 ended


def test_spawns_stay_staggered(monkeypatch):
    monkeypatch.setattr(SW, "SPAWN_STAGGER", 0.3)
    phases = _phases(("P1", ()), ("P2", ()), ("P3", ()))
    w = _World(durations={t: 0.05 for t in ("P1", "P2", "P3")})
    rid = SW.start("g", ".", "opencode", w.spawn, w.run_turn, phases=phases,
                   review=False)
    assert _wait(rid)["state"] == SW.DONE
    starts = [r[1] for r in w.turns]
    assert all(b - a >= 0.28 for a, b in zip(starts, starts[1:])), starts


# --------------------------------------------------------------------------- #
# (c) a failed dependency behaves exactly as before
# --------------------------------------------------------------------------- #

def _failed_dep_outcome(walk):
    phases = _phases(("Base", ()), ("Uses base", (1,)), ("Other", ()))
    w = _World(durations={"Base": 0.1, "Uses base": 0.05, "Other": 0.3},
               replies={"Base": [""]})            # Base ends with no reply
    run = SW._Run("g", ".", "opencode", SW.clean_phases({"phases": phases}))
    walk(run, w)
    return run, w


def test_a_failed_dependency_does_not_block_and_matches_the_wave_walk():
    old, w_old = _failed_dep_outcome(_barrier_walk)
    new, w_new = _failed_dep_outcome(lambda run, w: SW._walk(run, w.spawn, w.run_turn))
    for run in (old, new):
        assert [a.state for a in run.agents] == [SW.FAILED, SW.DONE, SW.DONE]
        assert run.agents[0].error == "the agent produced no result"
    assert new.state == SW.DONE
    for w in (w_old, w_new):
        assert len(w.ran("Base")) == 1            # no extra attempt
        assert "NOTE: phase(s) 1 did not produce a result" in w.prompts["Uses base"]
    # It still waited for the failed phase to END, but not for "Other".
    assert w_new.first("Uses base")[1] >= w_new.first("Base")[2]
    assert w_new.first("Uses base")[1] < w_new.first("Other")[2]


# --------------------------------------------------------------------------- #
# (d) the review runs last
# --------------------------------------------------------------------------- #

def test_the_review_starts_after_every_other_phase():
    w = _World(durations={"Slow": 0.6, "Fast": 0.05, "After fast": 0.05,
                          SW.REVIEW_TITLE: 0.05})
    rid = SW.start("g", ".", "opencode", w.spawn, w.run_turn, phases=SLOW_FAST,
                   review=True)
    assert _wait(rid)["state"] == SW.DONE
    review = w.first(SW.REVIEW_TITLE)
    others = [r for r in w.turns if r[0] != SW.REVIEW_TITLE]
    assert len(others) == 3
    assert review[1] >= max(r[2] for r in others)


def test_an_unsatisfiable_graph_runs_the_rest_and_the_review_still_last():
    """A cycle (never produced by clean_phases, possible in a hand-made run)
    degrades to "run the rest together", as waves() does."""
    phases = [{"title": "X", "task": "do x", "needs": [2]},
              {"title": "Y", "task": "do y", "needs": [1]},
              {"title": "Z", "task": "do z", "needs": [7]},
              {"title": SW.REVIEW_TITLE, "task": "review", "needs": [1, 2, 3]}]
    run = SW._Run("g", ".", "opencode", phases)
    w = _World(durations={"X": 0.2, "Y": 0.2, "Z": 0.2, SW.REVIEW_TITLE: 0.05})
    done = threading.Event()
    threading.Thread(target=lambda: (SW._walk(run, w.spawn, w.run_turn), done.set()),
                     daemon=True).start()
    assert done.wait(10), "the walk hung on a cycle"
    assert [a.state for a in run.agents] == [SW.DONE] * 4
    review = w.first(SW.REVIEW_TITLE)
    assert review[1] >= max(r[2] for r in w.turns if r[0] != SW.REVIEW_TITLE)


# --------------------------------------------------------------------------- #
# (e) Stop: running workers are stopped, nothing new starts
# --------------------------------------------------------------------------- #

def test_stop_mid_run_stops_the_running_workers_and_starts_nothing():
    gates = {"A": threading.Event(), "B": threading.Event()}
    stopped = []

    def stop(sid):
        stopped.append(sid)
        for g in gates.values():                 # what killing the CLI does
            g.set()

    phases = _phases(("A", ()), ("B", ()), ("After A", (1,)))
    w = _World(gates=gates)
    rid = SW.start("g", ".", "opencode", w.spawn, w.run_turn, phases=phases,
                   review=True, stop=stop)
    try:
        assert _until(lambda: len(w.turns) == 2)
        assert SW.stop(rid) is True
        assert _until(lambda: sorted(w.sessions[s] for s in stopped) == ["A", "B"], 3), stopped
        st = _wait(rid)
        time.sleep(0.3)
        assert st["state"] == SW.STOPPED
        assert [r[0] for r in w.turns] == ["A", "B"]          # nothing new
        assert w.n == 2                                       # no new session
        states = {a["title"]: a["state"] for a in SW.status(rid)["agents"]}
        assert states["After A"] == SW.STOPPED
        assert states[SW.REVIEW_TITLE] == SW.STOPPED
    finally:
        for g in gates.values():
            g.set()


# --------------------------------------------------------------------------- #
# (f) resume runs only the unfinished phases, in dependency order
# --------------------------------------------------------------------------- #

def test_continue_reruns_only_unfinished_phases_and_respects_needs():
    phases = _phases(("A", ()), ("B", ()), ("After B", (2,)))
    w = _World(durations={"A": 0.05, "B": 0.3, "After B": 0.05},
               replies={"B": [""], "After B": [""]})   # both fail the first time
    rid = SW.start("g", ".", "opencode", w.spawn, w.run_turn, phases=phases,
                   review=True)
    st = _wait(rid)
    assert [a["state"] for a in st["agents"]] == [SW.DONE, SW.FAILED, SW.FAILED, SW.DONE]
    first_walk = len(w.turns)

    assert SW.resume(rid, w.spawn, w.run_turn) == rid
    st = _wait(rid)
    assert [a["state"] for a in st["agents"]] == [SW.DONE] * 4
    again = w.turns[first_walk:]
    assert [r[0] for r in again] == ["B", "After B", SW.REVIEW_TITLE]
    assert len(w.ran("A")) == 1                       # never run twice
    b, after = again[0], again[1]
    assert after[1] >= b[2]
    assert again[2][1] >= after[2]


def test_a_restart_resume_runs_only_interrupted_phases_in_order():
    phases = _phases(("A", ()), ("B", ()), ("After B", (2,)), ("Other", ()))
    run = SW._Run("build it", ".", "opencode", phases)
    run.agents[0].state = SW.DONE
    run.agents[0].summary = "a was done before"
    run.agents[1].state = SW.RUNNING
    run.agents[2].state = SW.PENDING
    run.agents[3].state = SW.PENDING
    run.state = SW.RUNNING
    SW._persist(run)
    SW._RUNS.clear()
    assert SW.load() == 1
    w = _World(durations={"B": 0.4, "After B": 0.05, "Other": 0.05})
    assert SW.resume_interrupted(w.spawn, w.run_turn) == [run.id]
    st = _wait(run.id)
    assert [a["state"] for a in st["agents"]] == [SW.DONE] * 4
    assert not w.ran("A")
    assert w.first("After B")[1] >= w.first("B")[2]
    assert w.first("Other")[1] < w.first("B")[2]      # independent: did not wait


# --------------------------------------------------------------------------- #
# (g) manager verdicts: one per phase, as with waves
# --------------------------------------------------------------------------- #

class _Manager:
    def __init__(self):
        self.lock = threading.Lock()
        self.verifies = []                       # (phase number, time)

    def __call__(self, system, user, purpose, max_tokens):
        if purpose != "verify":
            return ("", 0)
        m = re.search(r"\bPhase (\d+):", user)
        with self.lock:
            self.verifies.append((int(m.group(1)) if m else None, time.monotonic()))
        return (json.dumps({"ok": True, "problems": []}), 50)


def _managed(walk, folder):
    phases = _phases(("Page", ()), ("Styles", ()), ("Script", (1,)))
    run = SW._Run("g", str(folder), "opencode", SW.clean_phases({"phases": phases}),
                  manager=_Manager())
    assert run.waves == [[1, 2], [3]]            # a two-wave plan
    w = _World(durations={"Page": 0.1, "Styles": 0.5, "Script": 0.05})
    walk(run, w)
    return run, w


def test_manager_verdicts_stay_one_per_phase_for_a_two_wave_plan(tmp_path):
    old, _ = _managed(_barrier_walk, tmp_path)
    new, w = _managed(lambda run, w: SW._walk(run, w.spawn, w.run_turn), tmp_path)
    assert len(new.manager.verifies) == len(old.manager.verifies) == 3
    assert new.manager_calls == old.manager_calls == 3
    assert all(a.verified is True for a in new.agents)
    # Script builds on Page's CHECKED output: it starts after Page's verdict,
    # and without waiting for Styles.
    page_verdict = next(t for n, t in new.manager.verifies if n == 1)
    script_start = w.t0 + w.first("Script")[1]
    assert script_start >= page_verdict
    assert w.first("Script")[1] < w.first("Styles")[2]
