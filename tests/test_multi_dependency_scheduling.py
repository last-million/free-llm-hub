r"""A Multi run starts each phase as soon as what it NEEDS has finished.

Owner, 2026-10-04: "each phase should work in parallel if it does not need to
wait for other things". The run used to walk dependency WAVES and wait for the
whole wave: in [1 slow, 2 fast, 3 needs 2], phase 3 waited for slow phase 1
only because 1 shared phase 2's wave.

Kept exactly: a failed dependency does not block its dependents, the
concurrency cap (re-read before every start), the stagger, Stop reaching the
running workers, resume running only the unfinished phases, the review last,
one manager verdict per phase.

Everything is a fake: spawn, run_turn and the manager are injected. Overlap
is PROVEN with events and barriers (a phase that must run alongside another
waits for it, with a generous bound), never inferred from sleeps -- a worker's
turn starts after its folder snapshot, whose time varies under load. Order and
stagger are read from the scheduler's own thread starts.
"""
import collections
import itertools
import json
import threading
import time
import types

import pytest

import lowres
import swarm_windows as SW

BOUND = 15          # seconds any event/barrier wait may take before it is a failure
# Happened-before, exactly: turn starts/ends and verdicts take numbers from one
# counter (the Windows clock ticks every ~16 ms, so two causally ordered
# events can share a timestamp).
_tick = itertools.count(1).__next__


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


@pytest.fixture
def folder(tmp_path):
    """An empty project folder: every phase snapshots it."""
    d = tmp_path / "proj"
    d.mkdir()
    return str(d)


@pytest.fixture
def starts(monkeypatch):
    """[(phase index, time.time())] of every worker thread the scheduler
    starts, in start order -- what the scheduler itself controls."""
    rec, lock = [], threading.Lock()

    class _Recorded(threading.Thread):
        def start(self):
            if self.name.startswith("swarm-swarm-"):
                with lock:
                    rec.append((int(self.name.rsplit("-", 1)[1]), time.time()))
            super().start()

    shim = types.ModuleType("threading")
    shim.__dict__.update(threading.__dict__)
    shim.Thread = _Recorded
    monkeypatch.setattr(SW, "threading", shim)
    return rec


def _wait(run_id, timeout=BOUND * 2):
    end = time.time() + timeout
    while time.time() < end:
        st = SW.status(run_id)
        if st and st["state"] in (SW.DONE, SW.FAILED, SW.STOPPED):
            return st
        time.sleep(0.02)
    raise AssertionError("run did not finish: %s" % SW.status(run_id))


def _until(cond, timeout=BOUND):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def _workers_gone(run_id):
    prefix = "swarm-%s-" % run_id
    return not any(t.name.startswith(prefix) for t in threading.enumerate())


class _World:
    """Fake CLI. A phase's turn sleeps durations[title], or runs
    holds[title]() (an event/barrier wait); replies[title] is a queue of
    replies ("" = no reply). began[title] is set when its turn starts."""

    def __init__(self, durations=None, replies=None):
        self.durations = durations or {}
        self.replies = {k: list(v) for k, v in (replies or {}).items()}
        self.holds = {}
        self.began = collections.defaultdict(threading.Event)
        self.lock = threading.Lock()
        self.n = 0
        self.turns = []          # [title, start tick, end tick] in start order
        self.sessions = {}       # session id -> title
        self.prompts = {}        # title -> last prompt

    def spawn(self, cli, project):
        with self.lock:
            self.n += 1
            return "s%d" % self.n

    def run_turn(self, sid, prompt):
        title = prompt.split("Your phase is called: ", 1)[1].splitlines()[0]
        with self.lock:
            self.sessions[sid] = title
            self.prompts[title] = prompt
            row = [title, _tick(), None]
            self.turns.append(row)
        self.began[title].set()
        try:
            hold = self.holds.get(title)
            if hold is not None:
                hold()
            else:
                time.sleep(self.durations.get(title, 0.02))
        finally:
            row[2] = _tick()
        queue = self.replies.get(title)
        text = queue.pop(0) if queue else "Finished %s: wrote the files and checked them." % title
        if text:
            yield {"event": "message", "text": text}
        yield {"event": "done"}

    def after(self, title, other):
        """Hold `title`'s turn until `other`'s turn has begun (bounded)."""
        self.holds[title] = lambda: self.began[other].wait(BOUND)

    def first(self, title):
        return next(r for r in self.turns if r[0] == title)

    def ran(self, title):
        return [r for r in self.turns if r[0] == title]


def _barrier(n):
    b = threading.Barrier(n, timeout=BOUND)
    return lambda: b.wait()


def _phases(*rows):
    """(title, needs) pairs -> phases."""
    return [{"title": t, "task": "do " + t.lower(), "needs": list(n)} for t, n in rows]


SLOW_FAST = _phases(("Slow", ()), ("Fast", ()), ("After fast", (2,)))


def _barrier_walk(run, w):
    """The OLD execution, for comparison: one wave at a time, each run to its
    end before the next (a wave's phases are independent, so _run_wave on one
    wave is exactly the old wave)."""
    run.state = SW.RUNNING
    for wave in run.waves:
        SW._run_wave(run, wave, w.spawn, w.run_turn)
    run.state = SW.DONE


def _new_walk(run, w):
    SW._walk(run, w.spawn, w.run_turn)


# --------------------------------------------------------------------------- #
# (a) a phase starts when ITS dependencies are done, not its wave
# --------------------------------------------------------------------------- #

def test_a_phase_starts_while_an_unrelated_slow_phase_still_runs(folder):
    w = _World()
    w.after("Slow", "After fast")       # Slow ends only once phase 3 has begun
    rid = SW.start("g", folder, "opencode", w.spawn, w.run_turn, phases=SLOW_FAST,
                   review=False)
    st = _wait(rid)
    assert st["state"] == SW.DONE
    assert st["waves"] == [[1, 2], [3]]          # the plan as shown is unchanged
    slow, fast, after = w.first("Slow"), w.first("Fast"), w.first("After fast")
    assert after[1] >= fast[2], "phase 3 started before the phase it needs ended"
    assert after[1] < slow[2], "phase 3 waited for slow phase 1, which it does not need"


def test_the_dependency_walk_is_faster_than_the_wave_barrier(folder):
    """Measured on the same plan and the same fake durations."""
    durations = {"Slow": 2.0, "Fast": 0.2, "After fast": 0.8}
    took = {}
    for label, walk in (("before", _barrier_walk), ("after", _new_walk)):
        w = _World(durations=durations)
        run = SW._Run("g", folder, "opencode", SW.clean_phases({"phases": SLOW_FAST}))
        t = time.monotonic()
        walk(run, w)
        took[label] = time.monotonic() - t
        assert [a.state for a in run.agents] == [SW.DONE] * 3
    # before >= 2.0 + 0.8 (wave 2 waits for Slow); after ~ 2.0 (3 runs inside it)
    assert took["before"] >= 2.8
    assert took["after"] < took["before"] - 0.4, took


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


def test_never_more_workers_than_the_cap(monkeypatch, folder, starts):
    monkeypatch.setattr(lowres, "workers", lambda default, m=None: 2)
    workers = _count_workers(monkeypatch)
    phases = _phases(("P1", ()), ("P2", ()), ("P3", (1,)), ("P4", (1,)), ("P5", ()))
    w = _World()
    both = _barrier(2)                  # P1 and P2 only pass while both run
    w.holds.update({"P1": both, "P2": both})
    rid = SW.start("g", folder, "opencode", w.spawn, w.run_turn, phases=phases,
                   review=False)
    st = _wait(rid)
    assert [a["state"] for a in st["agents"]] == [SW.DONE] * 5
    assert workers["peak"] == 2
    # ready phases start in plan order: the two first slots go to P1 and P2
    assert [i for i, _t in starts][:2] == [1, 2]
    assert sorted(i for i, _t in starts) == [1, 2, 3, 4, 5]


def test_low_resource_one_worker_runs_strictly_one_at_a_time(monkeypatch, folder, starts):
    monkeypatch.setattr(lowres, "workers", lambda default, m=None: 1)
    workers = _count_workers(monkeypatch)
    phases = _phases(("P1", ()), ("P2", ()), ("P3", (1,)), ("P4", ()))
    w = _World()
    rid = SW.start("g", folder, "opencode", w.spawn, w.run_turn, phases=phases,
                   review=True)
    assert _wait(rid)["state"] == SW.DONE
    assert workers["peak"] == 1
    for prev, nxt in zip(w.turns, w.turns[1:]):
        assert nxt[1] >= prev[2], "two workers overlapped under a cap of 1"
    assert [i for i, _t in starts] == [1, 2, 3, 4, 5]
    assert [r[0] for r in w.turns] == ["P1", "P2", "P3", "P4", SW.REVIEW_TITLE]


def test_the_cap_is_read_again_before_every_start(monkeypatch, folder, starts):
    """RAM freed mid-run: the queued phases may run side by side again."""
    caps = {"n": 1}
    monkeypatch.setattr(lowres, "workers", lambda default, m=None: caps["n"])
    gate = threading.Event()
    phases = _phases(("P1", ()), ("P2", ()), ("P3", ()), ("P4", ()))
    w = _World()
    three = _barrier(3)                 # P2..P4 only pass while all three run
    w.holds.update({"P1": lambda: gate.wait(BOUND), "P2": three, "P3": three,
                    "P4": three})
    rid = SW.start("g", folder, "opencode", w.spawn, w.run_turn, phases=phases,
                   review=False)
    assert w.began["P1"].wait(BOUND)
    time.sleep(0.3)
    assert len(starts) == 1                      # cap 1: the rest wait
    caps["n"] = 4
    gate.set()
    st = _wait(rid)
    assert [a["state"] for a in st["agents"]] == [SW.DONE] * 4


def test_spawns_stay_staggered(monkeypatch, folder, starts):
    monkeypatch.setattr(SW, "SPAWN_STAGGER", 0.3)
    phases = _phases(("P1", ()), ("P2", ()), ("P3", ()))
    w = _World()
    rid = SW.start("g", folder, "opencode", w.spawn, w.run_turn, phases=phases,
                   review=False)
    assert _wait(rid)["state"] == SW.DONE
    times = [t for _i, t in starts]
    assert len(times) == 3
    assert all(b - a >= 0.3 for a, b in zip(times, times[1:])), times


# --------------------------------------------------------------------------- #
# (c) a failed dependency behaves exactly as before
# --------------------------------------------------------------------------- #

def _failed_dep_outcome(walk, folder, gated):
    phases = _phases(("Base", ()), ("Uses base", (1,)), ("Other", ()))
    w = _World(replies={"Base": [""]})             # Base ends with no reply
    if gated:
        w.after("Other", "Uses base")
    run = SW._Run("g", folder, "opencode", SW.clean_phases({"phases": phases}))
    walk(run, w)
    return run, w


def test_a_failed_dependency_does_not_block_and_matches_the_wave_walk(folder):
    old, w_old = _failed_dep_outcome(_barrier_walk, folder, gated=False)
    new, w_new = _failed_dep_outcome(_new_walk, folder, gated=True)
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

def test_the_review_starts_after_every_other_phase(folder):
    w = _World(durations={"Slow": 0.4})
    rid = SW.start("g", folder, "opencode", w.spawn, w.run_turn, phases=SLOW_FAST,
                   review=True)
    assert _wait(rid)["state"] == SW.DONE
    review = w.first(SW.REVIEW_TITLE)
    others = [r for r in w.turns if r[0] != SW.REVIEW_TITLE]
    assert len(others) == 3
    assert review[1] >= max(r[2] for r in others)


def test_an_unsatisfiable_graph_runs_the_rest_and_the_review_still_last(folder):
    """A cycle (never produced by clean_phases, possible in a hand-made run)
    degrades to "run the rest together", as waves() does."""
    phases = [{"title": "X", "task": "do x", "needs": [2]},
              {"title": "Y", "task": "do y", "needs": [1]},
              {"title": "Z", "task": "do z", "needs": [7]},
              {"title": SW.REVIEW_TITLE, "task": "review", "needs": [1, 2, 3]}]
    run = SW._Run("g", folder, "opencode", phases)
    w = _World()
    done = threading.Event()
    threading.Thread(target=lambda: (_new_walk(run, w), done.set()), daemon=True).start()
    assert done.wait(BOUND * 2), "the walk hung on a cycle"
    assert [a.state for a in run.agents] == [SW.DONE] * 4
    review = w.first(SW.REVIEW_TITLE)
    assert review[1] >= max(r[2] for r in w.turns if r[0] != SW.REVIEW_TITLE)


# --------------------------------------------------------------------------- #
# (e) Stop: running workers are stopped, nothing new starts
# --------------------------------------------------------------------------- #

def test_stop_mid_run_stops_the_running_workers_and_starts_nothing(folder, starts):
    release = threading.Event()
    stopped = []

    def stop(sid):
        stopped.append(sid)
        release.set()                            # what killing the CLI does

    phases = _phases(("A", ()), ("B", ()), ("After A", (1,)))
    w = _World()
    w.holds.update({"A": lambda: release.wait(BOUND), "B": lambda: release.wait(BOUND)})
    rid = SW.start("g", folder, "opencode", w.spawn, w.run_turn, phases=phases,
                   review=True, stop=stop)
    try:
        assert w.began["A"].wait(BOUND) and w.began["B"].wait(BOUND)
        assert SW.stop(rid) is True
        assert _until(lambda: {w.sessions.get(s) for s in stopped} == {"A", "B"}), stopped
        assert _until(lambda: _workers_gone(rid)), "the stopped workers never ended"
        st = SW.status(rid)
        assert st["state"] == SW.STOPPED
        assert sorted(i for i, _t in starts) == [1, 2]       # nothing new
        assert sorted(r[0] for r in w.turns) == ["A", "B"]
        assert w.n == 2                                       # no new session
        states = {a["title"]: a["state"] for a in st["agents"]}
        assert states == {"A": SW.STOPPED, "B": SW.STOPPED, "After A": SW.STOPPED,
                          SW.REVIEW_TITLE: SW.STOPPED}
    finally:
        release.set()


def test_a_stop_during_the_session_spawn_starts_no_cli_turn(folder):
    """The scheduler stops the sessions it can see once; a worker whose
    session was still being made must not start its CLI turn after it."""
    run = SW._Run("g", folder, "opencode", _phases(("A", ())))
    w = _World()

    def spawn(cli, project):
        run.stop_flag.set()                      # Stop lands mid-spawn
        return w.spawn(cli, project)

    SW._walk(run, spawn, w.run_turn)
    assert w.turns == []
    assert run.agents[0].state == SW.STOPPED
    assert run.state == SW.STOPPED


# --------------------------------------------------------------------------- #
# (f) resume runs only the unfinished phases, in dependency order
# --------------------------------------------------------------------------- #

def test_continue_reruns_only_unfinished_phases_and_respects_needs(folder):
    phases = _phases(("A", ()), ("B", ()), ("After B", (2,)))
    w = _World(replies={"B": [""], "After B": [""]})   # both fail the first time
    rid = SW.start("g", folder, "opencode", w.spawn, w.run_turn, phases=phases,
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
    b, after, review = again
    assert after[1] >= b[2]
    assert review[1] >= after[2]


def test_a_restart_resume_runs_only_interrupted_phases_in_order(folder):
    phases = _phases(("A", ()), ("B", ()), ("After B", (2,)), ("Other", ()))
    run = SW._Run("build it", folder, "opencode", phases)
    run.agents[0].state = SW.DONE
    run.agents[0].summary = "a was done before"
    run.agents[1].state = SW.RUNNING
    run.agents[2].state = SW.PENDING
    run.agents[3].state = SW.PENDING
    run.state = SW.RUNNING
    SW._persist(run)
    SW._RUNS.clear()
    assert SW.load() == 1
    w = _World()
    w.after("B", "Other")                   # B ends only once Other has begun
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
    """Approves everything; records WHICH phase each verdict was for from the
    worker thread asking (named by the scheduler), not from prompt wording."""

    def __init__(self):
        self.lock = threading.Lock()
        self.verifies = []                       # (phase index, tick)

    def __call__(self, system, user, purpose, max_tokens):
        if purpose != "verify":
            return ("", 0)
        name = threading.current_thread().name
        idx = int(name.rsplit("-", 1)[1]) if name.startswith("swarm-swarm-") else None
        with self.lock:
            self.verifies.append((idx, _tick()))
        return (json.dumps({"ok": True, "problems": []}), 50)


def _managed(walk, folder, gated):
    phases = _phases(("Page", ()), ("Styles", ()), ("Script", (1,)))
    run = SW._Run("g", folder, "opencode", SW.clean_phases({"phases": phases}),
                  manager=_Manager())
    assert run.waves == [[1, 2], [3]]            # a two-wave plan
    w = _World()
    if gated:
        w.after("Styles", "Script")              # Styles ends once Script began
    walk(run, w)
    return run, w


def test_manager_verdicts_stay_one_per_phase_for_a_two_wave_plan(folder):
    old, _ = _managed(_barrier_walk, folder, gated=False)
    new, w = _managed(_new_walk, folder, gated=True)
    for run in (old, new):
        assert sorted(i for i, _t in run.manager.verifies) == [1, 2, 3]
        assert run.manager_calls == 3
        assert [a.state for a in run.agents] == [SW.DONE] * 3
        assert all(getattr(a, "reviewed", a.verified) is True for a in run.agents)
    # Script builds on Page's CHECKED output: it starts after Page's verdict,
    # and without waiting for Styles.
    page_verdict = next(t for i, t in new.manager.verifies if i == 1)
    assert w.first("Script")[1] > page_verdict
    assert w.first("Script")[1] < w.first("Styles")[2]
