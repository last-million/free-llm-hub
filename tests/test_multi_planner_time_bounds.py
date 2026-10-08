"""Multi planning is time-bounded (2026-10-08, flag `planner_time_bounds`).

MEASURED, Build conversation 2e3525ad (hub.log, UTC): a Multi run planned for
526 s before one helper started -- attempt 1 190 s and EMPTY, attempt 2 198 s,
then the dry run's OPTIONAL re-ask 138 s and EMPTY ("0 fixed, 7 warnings").
Old worst case: PLAN_ATTEMPTS 2 x 3 hops x the 300 s hop deadline, plus the
re-ask's 3 x 300 s = 2700 s.

Under test:
  * swarm_windows opens a thread-local WINDOW around every planner call (an
    attempt: PLAN_ATTEMPT_SECONDS; the dry run's re-ask: DRY_RUN_SECONDS) and
    the re-ask is skipped when the plan alone took over DRY_RUN_SKIP_AFTER;
  * app._swarm_dispatch(plan=...) gives a planner hop PLAN_HOP_SECONDS (never
    past the window), walks on INSIDE the attempt on an empty / cut-off /
    not-a-plan / timed-out hop, gives a thinker room, and remembers the pairs
    that failed it;
  * app._planner_order puts fast non-thinking models first INSIDE the top
    band only, re-orders and never drops;
  * the activity row shows the model that answered; flag off = the old walk.

No network, no real sleeps: every model call is a stub and time is a fake.
"""
import json
import logging

import pytest

import app as A
import config
import plan_check
import swarm_windows as SW

_REAL_ACT_PICK = A._act_pick          # the env fixture stubs it; one test wants the real one
PLAN_JSON = json.dumps({"phases": [{"title": "A", "task": "do a", "needs": []}]})
GOAL = ("Build a word-count CLI. Deliver:\n1) a parser module\n"
        "2) a README with usage examples\n3) unit tests for the parser")
NO_README = {"design": {"components": ["wc.py: the parser"], "interfaces": ["count(text) -> dict"]},
             "phases": [{"title": "Parser", "task": "Build the parser module in wc.py",
                         "done_when": "python wc.py f.txt prints counts", "files": ["wc.py"]},
                        {"title": "Tests", "task": "Write unit tests for the parser",
                         "done_when": "pytest passes", "files": ["test_wc.py"], "needs": [1]}]}


class Clock:
    """A fake monotonic clock; calling it reads, .advance() moves it."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(SW, "_mono", c)
    monkeypatch.setattr(A, "_PLANNER_CLOCK", c)
    return c


@pytest.fixture
def flag(monkeypatch):
    """flag(False) turns planner_time_bounds off; every other flag keeps its default."""
    def set_to(value):
        real = config.get_flag
        monkeypatch.setattr(
            config, "get_flag",
            lambda k, d=False: value if k == "planner_time_bounds" else real(k, d))
    return set_to


# --------------------------------------------------------------------------- #
# 1. swarm_windows: the window around every planner call
# --------------------------------------------------------------------------- #

def test_an_attempt_runs_inside_a_window_and_leaves_none_behind(clock):
    seen = []

    def planner(system, user):
        seen.append(SW.planning_seconds_left())
        clock.advance(100)
        return "{}" if len(seen) < 2 else PLAN_JSON
    assert SW.planning_seconds_left() is None
    phases = SW.plan("g", planner)
    assert phases and len(seen) == 2
    assert seen[0] == SW.PLAN_ATTEMPT_SECONDS
    assert seen[1] == SW.PLAN_ATTEMPT_SECONDS, "attempt 2 gets a FRESH window"
    assert SW.planning_seconds_left() is None, "the window does not outlive the call"


def test_the_window_counts_down_while_the_planner_works(clock):
    left = []

    def planner(system, user):
        clock.advance(60)
        left.append(SW.planning_seconds_left())
        clock.advance(300)
        left.append(SW.planning_seconds_left())
        return PLAN_JSON
    SW.plan("g", planner)
    assert left == [SW.PLAN_ATTEMPT_SECONDS - 60, 0.0], "never negative"


def test_a_planner_that_raises_still_closes_the_window(clock):
    def planner(system, user):
        raise RuntimeError("down")
    assert SW.plan("g", planner) == []
    assert SW.planning_seconds_left() is None


def test_flag_off_the_planner_is_called_plainly(clock, flag):
    flag(False)
    seen = []

    def planner(system, user):
        seen.append(SW.planning_seconds_left())
        return PLAN_JSON
    SW.plan("g", planner)
    assert seen == [None]


def test_the_planner_stays_a_plain_two_argument_callable(clock):
    """The fakes of the whole suite take exactly (system, user): the window is
    thread-local, not a new argument."""
    calls = []
    SW.plan("g", lambda system, user: calls.append((system, user)) or PLAN_JSON)
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# 2. the dry run's optional re-ask: its own short budget and a skip rule
# --------------------------------------------------------------------------- #

def _reask_planner(window_log):
    def planner(system, user):
        window_log.append(SW.planning_seconds_left())
        return json.dumps(NO_README)
    return planner


def test_the_re_ask_gets_its_own_short_window(clock):
    log = []
    _p, _d, report = SW.dry_run(GOAL, NO_README["phases"], NO_README["design"], None,
                                planner=_reask_planner(log), planned_in=30.0)
    assert log == [SW.DRY_RUN_SECONDS] and SW.DRY_RUN_SECONDS <= 45
    assert report["replanned"] is True and "reask_skipped" not in report


def test_a_plan_that_took_over_four_minutes_skips_the_re_ask(clock):
    log = []
    _p, _d, report = SW.dry_run(GOAL, NO_README["phases"], NO_README["design"], None,
                                planner=_reask_planner(log),
                                planned_in=SW.DRY_RUN_SKIP_AFTER + 1)
    assert log == [], "the planner is not called at all"
    assert report.get("replanned") is not True
    assert report["reask_skipped"] == "4 min 01 s"
    assert report["findings"] and all(f["action"] != "replan" for f in report["findings"]), \
        "the findings stay on the report as warnings: the run goes ahead"


def test_the_skip_rule_edge_is_inclusive_of_four_minutes(clock):
    log = []
    SW.dry_run(GOAL, NO_README["phases"], NO_README["design"], None,
               planner=_reask_planner(log), planned_in=SW.DRY_RUN_SKIP_AFTER)
    assert len(log) == 1, "exactly four minutes still earns the re-ask"


def test_flag_off_the_re_ask_is_asked_whatever_the_plan_took(clock, flag):
    flag(False)
    log = []
    _p, _d, report = SW.dry_run(GOAL, NO_README["phases"], NO_README["design"], None,
                                planner=_reask_planner(log), planned_in=9999.0)
    assert log == [None] and report["replanned"] is True and "reask_skipped" not in report


def test_the_skip_is_said_on_the_plan_check_line(clock):
    _p, _d, report = SW.dry_run(GOAL, NO_README["phases"], NO_README["design"], None,
                                planner=_reask_planner([]), planned_in=388.0)
    plan_check.summarize(report, NO_README["phases"])
    assert "planner re-ask skipped (the plan took 6 min 28 s)" in report["line"]
    assert "re-asked once" not in report["line"]


def test_a_covered_plan_has_no_re_ask_to_skip(clock):
    covered = dict(NO_README, phases=NO_README["phases"] + [
        {"title": "Docs", "task": "Write README.md with usage examples",
         "done_when": "README.md has a usage section", "files": ["README.md"]}])
    log = []
    _p, _d, report = SW.dry_run(GOAL, covered["phases"], covered["design"], None,
                                planner=_reask_planner(log), planned_in=9999.0)
    assert log == [] and "reask_skipped" not in report


def test_start_measures_how_long_the_plan_took_and_skips_the_re_ask(tmp_path, monkeypatch, clock):
    monkeypatch.setenv(SW._STORE_ENV, str(tmp_path / "runs"))
    monkeypatch.setattr(SW, "SPAWN_STAGGER", 0.0)
    SW._RUNS.clear()
    asks = []

    def planner(system, user):
        asks.append(user)
        clock.advance(300)            # a slow planner: five minutes
        return json.dumps(NO_README)
    try:
        rid = SW.start(GOAL, str(tmp_path), "opencode", lambda c, p: "s", lambda s, p: iter(()),
                       planner=planner)
        st = SW.status(rid)
        assert len(asks) == 1, "the re-ask was skipped: the plan alone took 300 s"
        assert "planner re-ask skipped (the plan took 5 min 00 s)" in st["plan_check"]["line"]
    finally:
        for run in list(SW._RUNS.values()):
            run.stop_flag.set()
        SW._RUNS.clear()


def test_start_with_a_fast_plan_still_asks_once(tmp_path, monkeypatch, clock):
    monkeypatch.setenv(SW._STORE_ENV, str(tmp_path / "runs"))
    monkeypatch.setattr(SW, "SPAWN_STAGGER", 0.0)
    SW._RUNS.clear()
    asks = []

    def planner(system, user):
        asks.append(user)
        clock.advance(20)
        return json.dumps(NO_README)
    try:
        rid = SW.start(GOAL, str(tmp_path), "opencode", lambda c, p: "s", lambda s, p: iter(()),
                       planner=planner)
        st = SW.status(rid)
        assert len(asks) == 2 and st["plan_check"]["replanned"] is True
    finally:
        for run in list(SW._RUNS.values()):
            run.stop_flag.set()
        SW._RUNS.clear()


def test_the_worst_case_is_bounded_by_construction():
    """2 attempts x three full-length hops, and no re-ask on a plan that long."""
    old = 2 * 3 * 300 + 3 * 300
    new = SW.PLAN_ATTEMPTS * SW.PLAN_ATTEMPT_SECONDS
    assert SW.PLAN_ATTEMPT_SECONDS == 3 * SW.PLAN_HOP_SECONDS
    assert new <= 540 and new < old / 4
    assert SW.PLAN_HOP_SECONDS == 90 and SW.DRY_RUN_SECONDS <= 45
    assert SW.DRY_RUN_SKIP_AFTER == 240


# --------------------------------------------------------------------------- #
# 3. app._swarm_dispatch(plan=...): hops, deadlines, empty replies
# --------------------------------------------------------------------------- #

SCORES = {"m1": 138.0, "m2": 137.5, "m3": 137.0, "m4": 136.8, "m5": 136.5,
          "m6": 136.2, "m7": 136.0, "far": 120.0}


class _Resp:
    status_code = 200

    def __init__(self, text, finish="stop", status=200):
        self._t, self._f, self.status_code = text, finish, status

    def json(self):
        return {"choices": [{"finish_reason": self._f,
                             "message": {"role": "assistant", "content": self._t}}]}

    def close(self):
        pass


class Env:
    """The scripted world under _swarm_dispatch: `script[model]` is a callable
    (payload) -> (resp, exc, seconds); the fake clock advances by `seconds`."""

    def __init__(self, clock):
        self.clock = clock
        self.calls = []                       # (pid, model, deadline, payload)
        self.script = {}
        self.default = lambda payload: (_Resp(PLAN_JSON), None, 5)
        self.thinking_noted = []

    def dispatch(self, pid, payload, deadline):
        model = payload["model"]
        self.calls.append((pid, model, deadline, dict(payload)))
        resp, exc, secs = self.script.get(model, self.default)(payload)
        self.clock.advance(secs)
        return resp, exc

    @property
    def models(self):
        return [c[1] for c in self.calls]

    @property
    def deadlines(self):
        return [c[2] for c in self.calls]


@pytest.fixture
def env(monkeypatch, clock):
    e = Env(clock)
    chain = [("p%d" % (i + 1), m) for i, m in
             enumerate(("m1", "m2", "m3", "m4", "m5", "m6", "m7", "far"))]
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("p1", "m1", "hard"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: list(chain))
    monkeypatch.setattr(A, "_est_tokens", lambda *a, **k: 100)
    monkeypatch.setattr(A, "_record_outcome", lambda *a, **k: None)
    monkeypatch.setattr(A, "_record_chat_usage", lambda *a, **k: None)
    monkeypatch.setattr(A, "_act_pick", lambda *a, **k: None)
    monkeypatch.setattr(A, "_pipeline_time_left", lambda: None)
    monkeypatch.setattr(A, "_answer_gate", lambda *a, **k: "ok")
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: SCORES.get(m, 100.0))
    monkeypatch.setattr(A, "_is_low_quality", lambda m: m.startswith("weak"))
    monkeypatch.setattr(A, "_thinks_by_default", lambda pid, m: m in THINKERS)
    monkeypatch.setattr(A, "_measured_latency_ms", lambda pid, m: SLOW_MS.get(m))
    monkeypatch.setattr(A, "_is_slow_model", lambda pid, m: False)
    monkeypatch.setattr(A, "_supports_tools", lambda pid, m: not m.startswith("nt"))
    monkeypatch.setattr(A, "_model_output_cap", lambda pid, m: None)
    monkeypatch.setattr(A, "_note_thinking",
                        lambda pid, m, why="": e.thinking_noted.append((pid, m, why)))
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", e.dispatch)
    return e


THINKERS = set()
SLOW_MS = {}


@pytest.fixture(autouse=True)
def _reset_world():
    THINKERS.clear()
    SLOW_MS.clear()
    A._PLANNER_FAILED.clear()
    yield
    THINKERS.clear()
    SLOW_MS.clear()
    A._PLANNER_FAILED.clear()


def _stage(clock, seconds=SW.PLAN_ATTEMPT_SECONDS):
    return A._PlannerStage(seconds, clock=clock)


def _run(stage, messages=None):
    return A._swarm_dispatch(messages or [{"role": "user", "content": "plan it"}], 3000,
                             plan=stage)


def _empty_length(payload):
    return _Resp("", finish="length"), None, 60


def _empty_stop(payload):
    return _Resp("", finish="stop"), None, 3


def _hang(payload):
    return None, None, 90


def _prose(payload):
    return _Resp("Sure! Here is my plan: first we build, then we test."), None, 4


def test_a_planner_hop_gets_ninety_seconds_not_three_hundred(env, clock):
    text, who = _run(_stage(clock))
    assert who == "p1/m1" and text == PLAN_JSON
    assert env.deadlines == [SW.PLAN_HOP_SECONDS] == [90]
    # every other stage keeps its own deadline, byte for byte
    env.calls.clear()
    A._swarm_dispatch([{"role": "user", "content": "x"}], 100)
    assert env.deadlines == [A._SWARM_HOP_DEADLINE] == [300]


def test_a_hop_never_gets_more_than_the_window_has_left(env, clock):
    _run(_stage(clock, 40))
    assert env.deadlines == [40]


def test_a_window_with_under_eight_seconds_left_starts_no_hop(env, clock):
    assert _run(_stage(clock, 7)) == ("", None)
    assert env.calls == []


def test_the_window_ends_the_walk_between_hops(env, clock):
    env.script = {m: _hang for m in ("m1", "m2", "m3", "m4", "m5")}
    text, who = _run(_stage(clock, 200))
    assert (text, who) == ("", None)
    # 90 s + 90 s, then 20 s left: a third hop gets exactly the 20 s
    assert env.deadlines == [90, 90, 20]


def test_an_empty_reply_moves_to_the_next_model_inside_the_same_attempt(env, clock):
    env.script = {"m1": _empty_length}
    stage = _stage(clock)
    text, who = _run(stage)
    assert who == "p2/m2" and text == PLAN_JSON
    assert env.models == ["m1", "m2"]
    assert stage.rows[0][:3] == ("p1", "m1", "empty: out of tokens while thinking")
    assert stage.rows[1][:3] == ("p2", "m2", "")
    assert stage.answered == ("p2", "m2")


def test_an_empty_length_reply_teaches_that_the_model_thinks(env, clock):
    env.script = {"m1": _empty_length, "m2": _empty_stop}
    stage = _stage(clock)
    _run(stage)
    assert env.thinking_noted == [("p1", "m1", "starved-empty")], \
        "only the starved one: an empty stop is not hidden reasoning"
    assert [r[2] for r in stage.rows[:2]] == ["empty: out of tokens while thinking",
                                              "empty reply"]


def test_a_reply_that_is_not_a_plan_moves_on_too(env, clock):
    env.script = {"m1": _prose, "m2": lambda p: (_Resp('{"note": "no phases here"}'), None, 4)}
    stage = _stage(clock)
    text, who = _run(stage)
    assert who == "p3/m3" and text == PLAN_JSON
    assert [r[2] for r in stage.rows[:2]] == ["not a plan", "not a plan"]


def test_when_nothing_is_a_plan_the_longest_reply_still_reaches_plan(env, clock):
    env.script = {m: (lambda p, m=m: (_Resp("prose " * (1 + int(m[1:]))), None, 4))
                  for m in ("m1", "m2", "m3", "m4", "m5")}
    text, who = _run(_stage(clock))
    assert who == "p5/m5" and text.startswith("prose"), \
        "plan() logs it and re-asks with the nudge, as before"


def test_a_cut_off_reply_is_a_partial_not_a_plan(env, clock):
    cut = '{"phases": [{"title": "A", "task": "do'
    env.script = {"m1": lambda p: (_Resp(cut, finish="length"), None, 50)}
    stage = _stage(clock)
    text, who = _run(stage)
    assert who == "p2/m2" and text == PLAN_JSON
    assert stage.rows[0][2] == "cut off at the token cap"


def test_a_timeout_an_error_and_an_http_failure_each_hand_over(env, clock):
    env.script = {"m1": _hang,
                  "m2": lambda p: (None, ConnectionError("reset"), 2),
                  "m3": lambda p: (_Resp("", status=429), None, 1)}
    stage = _stage(clock)
    text, who = _run(stage)
    assert who == "p4/m4"
    assert [r[2] for r in stage.rows[:3]] == ["no answer in 90s", "error: ConnectionError",
                                              "HTTP 429"]


def test_a_planner_attempt_walks_up_to_five_hops_the_old_stage_three(env, clock):
    env.script = {m: _empty_stop for m in ("m1", "m2", "m3", "m4", "m5", "m6", "m7", "far")}
    _run(_stage(clock))
    assert len(env.calls) == A._PLANNER_MAX_HOPS == 5
    env.calls.clear()
    A._swarm_dispatch([{"role": "user", "content": "x"}], 100)
    assert len(env.calls) == A._SWARM_STAGE_MAX_HOPS == 3


def test_the_old_walk_is_unchanged_without_a_stage(env, clock):
    env.script = {"m1": _empty_length, "m2": _prose}
    text, who = A._swarm_dispatch([{"role": "user", "content": "x"}], 3000)
    assert who == "p2/m2" and text.startswith("Sure!"), "prose is accepted, as it always was"
    assert env.deadlines == [300, 300]
    assert env.thinking_noted == [], "no planner bookkeeping outside the planner"
    assert A._PLANNER_FAILED == {}
    assert all(c[3]["max_tokens"] == 3000 and "reasoning_effort" not in c[3]
               for c in env.calls)


def test_pairs_that_failed_this_planner_are_not_re_walked_by_the_next_attempt(env, clock):
    env.script = {"m1": _empty_length, "m2": _hang}
    _run(_stage(clock))                       # attempt 1: m1 empty, m2 hung, m3 answers
    assert env.models == ["m1", "m2", "m3"]
    env.calls.clear()
    env.script = {}
    text, who = _run(_stage(clock))           # attempt 2 / the re-ask
    assert env.models[0] == "m3", "the dead head m1 / m2 is not asked again first"
    assert who == "p3/m3"


def test_a_pair_that_answers_clears_its_planner_mark(env, clock):
    A._planner_mark_failed("p1", "m1", "empty reply")
    assert A._planner_recently_failed("p1", "m1")
    A._PlannerStage(10, clock=clock).ok("p1", "m1", clock())
    assert not A._planner_recently_failed("p1", "m1")


def test_a_planner_mark_expires(env, clock):
    A._PLANNER_FAILED[("p1", "m1")] = (0.0, "empty reply")      # epoch 0: ages ago
    assert not A._planner_recently_failed("p1", "m1")
    assert ("p1", "m1") not in A._PLANNER_FAILED


# ---- thinking room ---------------------------------------------------------

def test_a_thinker_gets_low_effort_and_room_a_plain_model_gets_neither(env, clock):
    THINKERS.update(SCORES)                      # every candidate thinks: the chain order stands
    env.script = {"m1": _empty_length}
    _run(_stage(clock))
    by = {c[1]: c[3] for c in env.calls}
    assert by["m1"]["reasoning_effort"] == "low"
    assert by["m1"]["max_tokens"] == 3000 + A._THINKING_ALLOWANCE["low"] > 3000
    assert "_hub_caller_max_tokens" in by["m1"], "stripped by _dispatch_chat before upstream"
    THINKERS.clear()
    env.calls.clear()
    A._PLANNER_FAILED.clear()
    _run(_stage(clock))
    plain = env.calls[0][3]
    assert plain["max_tokens"] == 3000 and "reasoning_effort" not in plain


# --------------------------------------------------------------------------- #
# 4. the planner's ordering: fast non-thinkers first, inside the band only
# --------------------------------------------------------------------------- #

def _chain(*names):
    return [("p%d" % (i + 1), n) for i, n in enumerate(names)]


def test_fast_non_thinking_models_lead_inside_the_band(env):
    THINKERS.add("m1")
    SLOW_MS["m3"] = 0.7 * SW.PLAN_HOP_SECONDS * 1000            # measured slow
    chain = _chain("m1", "m2", "m3")
    assert [m for _p, m in A._planner_order(chain)] == ["m2", "m1", "m3"]


def test_nothing_outside_the_band_is_promoted_and_nothing_is_dropped(env):
    THINKERS.update({"m1", "m2"})
    chain = _chain("m1", "m2", "far", "weak-1", "m3")
    got = A._planner_order(chain)
    assert [m for _p, m in got] == ["m3", "m1", "m2", "far", "weak-1"], \
        "the fast 'far' (18 points back) stays behind the whole band"
    assert sorted(got) == sorted(chain)
    best = max(SCORES.get(m, 100.0) for _p, m in chain)
    assert SCORES[got[0][1]] >= best - A._AUTO_TOP_BAND, "the head never leaves the band"


def test_a_last_resort_family_is_never_in_the_band(env):
    SCORES["weak-1"] = 137.9
    try:
        chain = _chain("m1", "weak-1", "m2")
        assert [m for _p, m in A._planner_order(chain)][-1] == "weak-1"
        assert A._planner_order(chain)[0] == ("p1", "m1")
    finally:
        SCORES.pop("weak-1", None)


def test_a_model_without_tool_support_goes_last_in_the_band(env):
    chain = _chain("nt-base", "m1", "m2")
    SCORES["nt-base"] = 138.5
    try:
        assert [m for _p, m in A._planner_order(chain)] == ["m1", "m2", "nt-base"]
    finally:
        SCORES.pop("nt-base", None)


def test_ties_keep_the_chains_own_order(env):
    chain = _chain("m3", "m1", "m2")
    assert A._planner_order(chain) == chain


def test_a_pair_that_just_failed_goes_behind_everything_but_is_kept(env):
    A._planner_mark_failed("p2", "m2", "empty reply")
    got = A._planner_order(_chain("m1", "m2", "m3", "far"))
    assert [m for _p, m in got] == ["m1", "m3", "far", "m2"]


def test_a_single_model_band_or_chain_is_left_as_built(env):
    one = _chain("m1")
    assert A._planner_order(one) == one
    assert A._planner_order([]) == []
    solo_band = _chain("far", "m1")      # m1 alone is in the band: not re-ordered
    assert A._planner_order(solo_band) == solo_band


def test_the_order_is_applied_to_the_planner_stage_only(env, clock):
    THINKERS.add("m1")
    _run(_stage(clock))
    assert env.models[0] == "m2", "the planner opened on the fast non-thinker"
    env.calls.clear()
    A._swarm_dispatch([{"role": "user", "content": "x"}], 100)
    assert env.models[0] == "m1", "a worker / reviewer / synthesis stage keeps the chain as built"


def test_the_ordering_never_raises(env, monkeypatch):
    monkeypatch.setattr(A, "_benchmark_score", lambda *a: 1 / 0)
    chain = _chain("m1", "m2")
    assert A._planner_order(chain) == chain


# --------------------------------------------------------------------------- #
# 5. the planner call itself: flag, activity row, trail
# --------------------------------------------------------------------------- #

def test_flag_off_the_planner_makes_the_old_unbounded_call(env, clock, flag):
    flag(False)
    assert A._planner_stage() is None
    env.script = {"m1": _empty_length}
    with A.app.test_request_context("/"):
        out = A._swarm_windows_planner("sys", "goal")
    assert out == PLAN_JSON and env.models == ["m1", "m2"]
    assert env.deadlines == [300, 300] and A._PLANNER_FAILED == {}


def test_the_stage_takes_the_window_swarm_windows_opened(clock):
    assert A._planner_stage().left() == SW.PLAN_ATTEMPT_SECONDS, "no window: the attempt default"
    seen = []

    def planner(system, user):
        seen.append(A._planner_stage().left())
        return PLAN_JSON
    SW.dry_run(GOAL, NO_README["phases"], NO_README["design"], None, planner=planner)
    assert seen == [SW.DRY_RUN_SECONDS]


def test_the_activity_row_shows_the_model_that_answered_and_the_failed_hops(env, clock,
                                                                           monkeypatch):
    env.script = {"m1": _empty_length, "m2": _hang}
    monkeypatch.setattr(A, "_act_pick", _REAL_ACT_PICK)     # the real one, not the fixture's no-op
    with A.app.test_request_context("/"):
        out = A._swarm_windows_planner("sys", "goal")
        assert out == PLAN_JSON
        with A._activity_lock:
            row = A._activity[0]
    assert row["status"] == "ok" and row["model_req"] == "plan"
    assert (row["provider"], row["model"]) == ("p3", "m3")
    assert [(p["role"], p["model"]) for p in row["pipeline"]] == [
        ("planner: empty: out of tokens while thinking", "p1/m1"),
        ("planner: no answer in 90s", "p2/m2"),
        ("planner", "p3/m3")]


def test_one_log_line_names_every_planner_hop(env, clock, caplog):
    env.script = {"m1": _empty_length}
    with caplog.at_level(logging.INFO, logger=A._log.name):
        with A.app.test_request_context("/"):
            A._swarm_windows_planner("sys", "goal")
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[plan] hops")]
    assert len(lines) == 1
    assert "p1/m1 empty: out of tokens while thinking (60s)" in lines[0]
    assert "p2/m2 answered (5s)" in lines[0]


def test_a_planner_that_cannot_answer_ends_the_row_in_error_with_its_trail(env, clock):
    env.script = {m: _empty_stop for m in SCORES}
    with A.app.test_request_context("/"):
        out = A._swarm_windows_planner("sys", "goal")
        with A._activity_lock:
            row = A._activity[0]
    assert out == "" and row["status"] == "error" and row["http"] == 502
    assert len(row["pipeline"]) == A._PLANNER_MAX_HOPS


def test_the_planner_source_keeps_the_literals_the_older_tests_pin():
    src = open(A.__file__, encoding="utf-8").read()
    body = src[src.index("def _swarm_windows_planner("):]
    body = body[:body.index("\ndef ")]
    assert '_act_begin("build" if _build_sid() else "hub", "plan"' in body
    assert "_act_end(act, bool(text))" in body and "_act_end(act, False)" in body
    assert "3000)" in body and "1500)" not in body
    assert "_planner_stage_close(stage, act)" in body


def test_end_to_end_plan_survives_an_empty_first_hop_in_one_attempt(env, clock):
    """The incident's shape through swarm_windows.plan: the first attempt used
    to burn on empty hops; now the same attempt reaches a model that answers."""
    env.script = {"m1": _empty_length, "m2": _empty_length}
    planner = A._pipeline_bound(A._swarm_windows_planner)
    asks = []

    def counting(system, user):
        asks.append(user)
        return planner(system, user)
    with A.app.test_request_context("/"):
        phases = SW.plan("build a thing", counting)
    assert phases and len(asks) == 1, "one attempt, not two"
    assert env.models == ["m1", "m2", "m3"]
