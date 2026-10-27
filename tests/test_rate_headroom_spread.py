"""Rate-aware provider spreading (2026-10-27).

Owner: "don't always use the same provider; if there are many requests for the
same provider, don't block requests per second and per minute."

The gap this closes (measured by reading the code, not by a live sweep): quota.py
hard-gates a provider at 100% of its budget, but it counts a request only after
the response headers arrive (so N hops started together all see "0 used"), in
calendar-minute buckets, and never reacts below 100%; and the fairness layer
(_fair_spread_band) only knows in-flight count and routing share. Nothing compared
how many requests a provider got in the LAST SECOND / MINUTE with its known limit.

`ratehead.RateWatch` counts requests STARTED; `app._rh_*` turns that plus the known
limit into a headroom, used in exactly three places -- never across the quality
band, never dropping anything, fail-open everywhere:
  1. _fair_spread_band  (a fresh pick): a provider at >= 80% of its limit yields
     to an equally good one with headroom;
  2. _pick_same_model_host (a pinned session): same weights, other host;
  3. _ChainClock.walk (fallback hops): a pressed head steps behind a comparable,
     equally healthy alternative. A pinned head (explicit provider/model) never moves.

Hermetic: a fake clock and fake limits; no network, no live hub, no real config.
"""
import logging
import os
import tempfile
import threading

import pytest

import app
import config
import quota
import ratehead


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def _lim(per_min=None, per_sec=None, per_model=False, src="test"):
    return {"per_min": per_min, "per_sec": per_sec, "per_model": per_model, "src": src}


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def watch(monkeypatch, clock):
    w = ratehead.RateWatch(clock=clock)
    monkeypatch.setattr(app, "_RATE_WATCH", w)
    return w


@pytest.fixture
def limits(monkeypatch):
    """Fake limits: g4f 5/min (so 4 starts = 80%), llm7 20/min, hog 1 per second.
    nvidia / groq / dahl have NO known limit."""
    table = {"g4f": _lim(per_min=5), "llm7": _lim(per_min=20),
             "hog": _lim(per_sec=1)}
    monkeypatch.setattr(app, "_rh_limits", lambda pid: table.get(pid))
    return table


@pytest.fixture(autouse=True)
def _idle_fleet(monkeypatch):
    """Spreading only engages under load; keep the other signals quiet and the
    scores equal to the benchmark so a band is exactly what the test writes."""
    with app._inflight_lock:
        app._PROVIDER_INFLIGHT.clear()
    with app._route_log_lock:
        app._ROUTE_LOG.clear()
    monkeypatch.setattr(app, "_agentic_score", lambda c, *a, **k: c[0])
    monkeypatch.setattr(app, "_is_fast", lambda pid, m: False)
    yield
    with app._inflight_lock:
        app._PROVIDER_INFLIGHT.clear()
    with app._route_log_lock:
        app._ROUTE_LOG.clear()


def _flag_off(monkeypatch, name="rate_headroom_spread"):
    real = config.get_flag
    monkeypatch.setattr(config, "get_flag",
                        lambda n, default=False: False if n == name else real(n, default))


# --------------------------------------------------------------------------- #
# The rolling counter.
# --------------------------------------------------------------------------- #
def test_counts_last_second_and_last_minute(clock):
    w = ratehead.RateWatch(clock=clock)
    for _ in range(3):
        w.note_start("llm7", "m")
    assert w.counts("llm7") == (3, 3)
    clock.advance(0.5)
    w.note_start("llm7", "m")
    assert w.counts("llm7") == (4, 4)
    clock.advance(0.6)                       # the first three are now > 1 s old
    assert w.counts("llm7") == (1, 4)
    clock.advance(59.0)                      # ... and the first three left the minute
    assert w.counts("llm7") == (0, 1)
    clock.advance(1.0)
    assert w.counts("llm7") == (0, 0)


def test_counts_can_be_asked_for_one_model(clock):
    w = ratehead.RateWatch(clock=clock)
    w.note_start("groq", "a")
    w.note_start("groq", "a")
    w.note_start("groq", "b")
    assert w.counts("groq") == (3, 3)
    assert w.counts("groq", "a") == (2, 2)
    assert w.counts("groq", "b") == (1, 1)
    assert w.counts("groq", "nope") == (0, 0)
    assert w.per_model("groq") == {"a": (2, 2), "b": (1, 1)}


def test_an_unseen_provider_is_zero_and_busy_is_empty_when_idle(clock):
    w = ratehead.RateWatch(clock=clock)
    assert w.counts("ghost") == (0, 0)
    assert w.busy() == {}
    w.note_start("a")
    assert set(w.busy()) == {"a"}
    clock.advance(61)
    assert w.busy() == {}


def test_memory_is_bounded(clock):
    w = ratehead.RateWatch(clock=clock, max_events=64, max_providers=8)
    for _ in range(1000):
        w.note_start("flood")
    assert w.counts("flood")[1] == 64          # overflow UNDERcounts: fail-open
    for i in range(40):
        w.note_start("p%d" % i)
    assert len(w._ev) <= 8


def test_clear_forgets_everything(clock):
    w = ratehead.RateWatch(clock=clock)
    w.note_start("a")
    w.clear()
    assert w.busy() == {} and w.counts("a") == (0, 0)


def test_thread_safety_smoke(clock):
    w = ratehead.RateWatch(clock=clock)
    n_threads, per = 16, 200
    start = threading.Barrier(n_threads + 2)
    errors = []

    def writer(k):
        start.wait()
        for _ in range(per):
            w.note_start("g4f", "m%d" % (k % 3))
            w.note_start("llm7")

    def reader():
        start.wait()
        try:
            for _ in range(300):
                w.counts("g4f")
                w.busy()
                w.per_model("g4f")
        except Exception as exc:                                  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=writer, args=(k,)) for k in range(n_threads)]
    ts += [threading.Thread(target=reader) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors
    assert w.counts("g4f") == (n_threads * per, n_threads * per)
    assert w.counts("llm7") == (n_threads * per, n_threads * per)


# --------------------------------------------------------------------------- #
# headroom() -- pure.
# --------------------------------------------------------------------------- #
def test_headroom_per_minute_and_per_second():
    assert ratehead.headroom(0, 0, per_min=20) == 1.0
    assert ratehead.headroom(0, 10, per_min=20) == pytest.approx(0.5)
    assert ratehead.headroom(0, 16, per_min=20) == pytest.approx(0.2)
    assert ratehead.headroom(0, 99, per_min=20) == 0.0           # clamped
    assert ratehead.headroom(1, 0, per_sec=1) == 0.0
    assert ratehead.headroom(0, 0, per_sec=1) == 1.0


def test_headroom_takes_the_tightest_limit():
    # plenty per minute, none left this second -> the second binds
    assert ratehead.headroom(1, 2, per_min=100, per_sec=1) == 0.0


def test_headroom_is_none_without_a_usable_limit():
    assert ratehead.headroom(5, 50) is None
    for bad in (0, -3, None, True, "20", 2.5):
        assert ratehead.headroom(5, 50, per_min=bad) is None


def test_pressed_at_eighty_percent():
    assert ratehead.is_pressed(0.2)
    assert ratehead.is_pressed(0.0)
    assert not ratehead.is_pressed(0.21)
    assert not ratehead.is_pressed(None)          # unknown never yields
    # 4 of 5 per minute is exactly 80% used
    assert ratehead.is_pressed(ratehead.headroom(0, 4, per_min=5))
    assert not ratehead.is_pressed(ratehead.headroom(0, 3, per_min=5))


# --------------------------------------------------------------------------- #
# 1. A fresh pick: _fair_spread_band.
# --------------------------------------------------------------------------- #
def test_a_saturated_low_limit_provider_yields_to_an_equal_alternative(watch, limits):
    band = [(138.0, "g4f", "m1"), (137.9, "nvidia", "m2")]
    assert {c[1] for c in app._fair_spread_band(band, est=90000)} == {"g4f", "nvidia"}
    for _ in range(4):                       # 4 of 5 per minute = 80%
        watch.note_start("g4f", "m1")
    out = app._fair_spread_band(band, est=90000)
    assert {c[1] for c in out} == {"nvidia"}


def test_simulated_burst_never_exceeds_the_documented_limit(watch, limits, clock):
    """Ten picks in a row where the pick takes the top-scored survivor of the band
    and 'dispatches' it. Without the feature g4f (138.0 > 137.9) would take all
    ten and burst past 5/min; with it g4f stops at 4 and nvidia takes the rest."""
    band = [(138.0, "g4f", "m1"), (137.9, "nvidia", "m2")]
    taken = []
    for _ in range(10):
        out = app._fair_spread_band(band, est=90000)
        pick = max(out)
        watch.note_start(pick[1], pick[2])
        taken.append(pick[1])
    assert taken.count("g4f") == 4
    assert taken.count("nvidia") == 6
    # a minute later the window has rolled and g4f is welcome again
    clock.advance(61)
    assert max(app._fair_spread_band(band, est=90000))[1] == "g4f"


def test_negative_control_flag_off_the_same_burst_goes_to_one_provider(
        watch, limits, monkeypatch):
    _flag_off(monkeypatch)
    band = [(138.0, "g4f", "m1"), (137.9, "nvidia", "m2")]
    taken = []
    for _ in range(10):
        out = app._fair_spread_band(band, est=90000)
        pick = max(out)
        watch.note_start(pick[1], pick[2])
        taken.append(pick[1])
    assert taken == ["g4f"] * 10               # the bug the feature fixes


def test_providers_with_no_known_limit_are_unchanged(watch, limits):
    band = [(138.0, "nvidia", "m1"), (137.9, "groq", "m2"), (137.8, "dahl", "m3")]
    for _ in range(500):                      # a flood, but no limit is known
        watch.note_start("nvidia", "m1")
    assert app._fair_spread_band(band, est=90000) == band


def test_the_quality_band_is_never_crossed(watch, limits):
    """A saturated provider yields only to a model INSIDE the band. A 134 sitting
    4 points back is not 'equally good': the band excludes it before the rate
    step runs, so the saturated 138 stays rather than a weaker model winning."""
    pool = [(138.0, "g4f", "m1"), (134.0, "nvidia", "m2")]
    for _ in range(5):
        watch.note_start("g4f", "m1")
    band = app._auto_top_band(pool)
    assert {c[1] for c in band} == {"g4f"}
    out = app._fair_spread_band(band, est=90000)
    assert set(out) <= set(band)
    assert {c[1] for c in out} == {"g4f"}


def test_a_pressed_leader_yields_only_inside_the_band(watch, limits):
    pool = [(138.0, "g4f", "m1"), (137.0, "nvidia", "m2"), (134.0, "groq", "m3")]
    for _ in range(5):
        watch.note_start("g4f", "m1")
    band = app._auto_top_band(pool)                    # groq is 4 back: not in it
    out = app._fair_spread_band(band, est=90000)
    assert {c[1] for c in out} == {"nvidia"}
    assert "groq" not in {c[1] for c in out}           # never reaches down for room


def test_it_only_ever_narrows_its_input(watch, limits):
    band = [(138.0, "g4f", "a"), (137.8, "llm7", "b"), (137.5, "nvidia", "c")]
    for _ in range(5):
        watch.note_start("g4f", "a")
    for _ in range(17):
        watch.note_start("llm7", "b")
    out = app._fair_spread_band(band, est=90000)
    assert set(out) <= set(band)
    assert {c[1] for c in out} == {"nvidia"}


def test_everyone_saturated_fails_open_to_the_full_band(watch, limits):
    band = [(138.0, "g4f", "m1"), (137.9, "llm7", "m2")]
    for _ in range(5):
        watch.note_start("g4f", "m1")
    for _ in range(19):
        watch.note_start("llm7", "m2")
    assert app._fair_spread_band(band, est=90000) == band


def test_headroom_is_judged_before_the_small_request_fast_rule(watch, limits, monkeypatch):
    """A small request prefers FAST providers. If the only fast one is saturated,
    narrowing to it first would leave nothing to yield to -- the rate step runs
    first, so the slow-but-free provider wins instead of the saturated fast one."""
    monkeypatch.setattr(app, "_is_fast", lambda pid, m: pid == "g4f")
    band = [(138.0, "g4f", "m1"), (137.9, "nvidia", "m2")]
    assert {c[1] for c in app._fair_spread_band(band, est=4000)} == {"g4f"}
    for _ in range(4):
        watch.note_start("g4f", "m1")
    assert {c[1] for c in app._fair_spread_band(band, est=4000)} == {"nvidia"}


def test_a_per_second_limit_yields_for_the_next_second_only(watch, limits, clock):
    band = [(138.0, "hog", "m1"), (137.9, "nvidia", "m2")]
    watch.note_start("hog", "m1")             # 1 req/s provider just used
    assert {c[1] for c in app._fair_spread_band(band, est=90000)} == {"nvidia"}
    clock.advance(1.01)
    assert {c[1] for c in app._fair_spread_band(band, est=90000)} == {"hog", "nvidia"}


def test_one_provider_band_is_unchanged(watch, limits):
    band = [(138.0, "g4f", "m1"), (137.9, "g4f", "m2")]
    for _ in range(5):
        watch.note_start("g4f", "m1")
    assert app._fair_spread_band(band, est=90000) == band


def test_flag_off_is_byte_identical(watch, limits, monkeypatch):
    band = [(138.0, "g4f", "m1"), (137.9, "nvidia", "m2")]
    for _ in range(5):
        watch.note_start("g4f", "m1")
    _flag_off(monkeypatch)
    assert app._fair_spread_band(band, est=90000) == band


def test_a_broken_limit_lookup_fails_open(watch, monkeypatch):
    def boom(pid):
        raise RuntimeError("limits exploded")
    monkeypatch.setattr(app, "_rh_limits", boom)
    watch.note_start("g4f", "m1")
    band = [(138.0, "g4f", "m1"), (137.9, "nvidia", "m2")]
    assert app._fair_spread_band(band, est=90000) == band


def test_per_model_limits_only_press_the_busy_model(watch, monkeypatch):
    monkeypatch.setattr(app, "_rh_limits",
                        lambda pid: _lim(per_min=5, per_model=True) if pid == "groq" else None)
    for _ in range(4):
        watch.note_start("groq", "busy")
    band = [(138.0, "groq", "busy"), (137.9, "groq", "idle"), (137.8, "nvidia", "n")]
    out = app._fair_spread_band(band, est=90000)
    assert (138.0, "groq", "busy") not in out
    assert (137.9, "groq", "idle") in out
    # the provider-wide count is 4/5 too, but a per-model limit ignores siblings
    assert app._rh_headroom("groq", "idle") == 1.0
    assert app._rh_headroom("groq", "busy") == pytest.approx(0.2)


def test_one_log_line_when_headroom_changed_a_pick(watch, limits, caplog):
    caplog.set_level(logging.INFO, logger="free-llm-hub")
    band = [(138.0, "g4f", "m1"), (137.9, "nvidia", "m2")]
    app._fair_spread_band(band, est=90000)
    assert not [r for r in caplog.records if "[rate]" in r.getMessage()]
    for _ in range(4):
        watch.note_start("g4f", "m1")
    for _ in range(3):                         # a burst of picks, one line
        app._fair_spread_band(band, est=90000)
    lines = [r.getMessage() for r in caplog.records if "[rate]" in r.getMessage()]
    assert len(lines) == 1
    assert "g4f" in lines[0] and "nvidia" in lines[0]


# --------------------------------------------------------------------------- #
# 2. A pinned session: the SAME weights on a host with headroom.
# --------------------------------------------------------------------------- #
def test_a_pinned_session_prefers_an_unpressed_host_of_the_same_model(watch, limits):
    pool = [(138.0, "g4f", "org/model-x"), (138.0, "nvidia", "org/model-x"),
            (130.0, "dahl", "other")]
    for _ in range(4):
        watch.note_start("g4f", "org/model-x")
    for _ in range(20):                        # random.choice would hit both
        host = app._pick_same_model_host(pool, ("g4f", "org/model-x"))
        assert host == ("nvidia", "org/model-x")


def test_same_model_hosts_unchanged_without_pressure(watch, limits, monkeypatch):
    # equal scores: the relay discount of the real g4f row would otherwise take
    # it out of the same-host band before any pressure is looked at
    monkeypatch.setattr(app, "_benchmark_score", lambda pid, m, _rank=True: 138.0)
    pool = [(138.0, "g4f", "org/model-x"), (138.0, "nvidia", "org/model-x")]
    seen = {app._pick_same_model_host(pool, ("g4f", "org/model-x"))[0] for _ in range(60)}
    assert seen == {"g4f", "nvidia"}


def test_same_model_hosts_all_pressed_fails_open(watch, limits, monkeypatch):
    monkeypatch.setattr(app, "_benchmark_score", lambda pid, m, _rank=True: 138.0)
    pool = [(138.0, "g4f", "org/model-x"), (138.0, "llm7", "org/model-x")]
    for _ in range(5):
        watch.note_start("g4f", "org/model-x")
    for _ in range(20):
        watch.note_start("llm7", "org/model-x")
    seen = {app._pick_same_model_host(pool, ("g4f", "org/model-x"))[0] for _ in range(60)}
    assert seen == {"g4f", "llm7"}


# --------------------------------------------------------------------------- #
# 3. The fallback walk.
# --------------------------------------------------------------------------- #
@pytest.fixture
def walk_world(monkeypatch):
    """Equal scores unless a test says otherwise; no pair is benched, failing or
    sick; the 'fast' / 'declared window' predicates are constants, so the group
    signature only differs where a test makes it differ."""
    scores = {}
    monkeypatch.setattr(app, "_benchmark_score",
                        lambda pid, m, _rank=True: scores.get((pid, m), 138.0))
    monkeypatch.setattr(app, "_below_declared_window", lambda pid, m: False)
    monkeypatch.setattr(app, "_is_pair_benched", lambda pid, m: False)
    return scores


def _drain(chain, **kw):
    with app.app.test_request_context("/v1/chat/completions"):
        clock = app._ChainClock(tools=kw.pop("tools", False), est=kw.pop("est", 2000), **kw)
        return list(clock.walk(list(chain)))


def test_the_walk_steps_a_saturated_head_behind_a_comparable_alternative(
        watch, limits, walk_world):
    chain = [("g4f", "a"), ("nvidia", "b"), ("dahl", "c")]
    assert _drain(chain)[0] == ("g4f", "a")            # nothing pressed: as built
    for _ in range(5):
        watch.note_start("g4f", "a")
    out = _drain(chain)
    assert sorted(out) == sorted(chain)                # reordered, NEVER dropped
    # the saturated head keeps stepping behind each comparable, free alternative
    assert out == [("nvidia", "b"), ("dahl", "c"), ("g4f", "a")]


def test_the_walk_never_crosses_the_quality_band(watch, limits, walk_world):
    walk_world[("g4f", "a")] = 138.0
    walk_world[("nvidia", "b")] = 130.0                # 8 back: NOT equally good
    for _ in range(5):
        watch.note_start("g4f", "a")
    chain = [("g4f", "a"), ("nvidia", "b")]
    assert _drain(chain) == chain


def test_negative_control_without_the_band_the_pure_pick_does_cross_it():
    """Proves the band test above is sensitive: the same decision with an
    unbounded band promotes the weaker model."""
    rest = [("g4f", "a"), ("nvidia", "b")]
    scores = {("g4f", "a"): 138.0, ("nvidia", "b"): 130.0}
    pressed = lambda e: e[0] == "g4f"
    args = dict(rest=rest, i=0, pressed_fn=pressed, score_fn=lambda e: scores[e],
                sig_fn=lambda e: 0)
    assert ratehead.walk_index(band=2.0, **args) == 0
    assert ratehead.walk_index(band=1e9, **args) == 1   # the unguarded behaviour


def test_the_walk_takes_a_better_scoring_unpressed_model_too(watch, limits, walk_world):
    walk_world[("nvidia", "b")] = 139.5                # stronger AND free
    for _ in range(5):
        watch.note_start("g4f", "a")
    assert _drain([("g4f", "a"), ("nvidia", "b")])[0] == ("nvidia", "b")


def test_a_pinned_head_never_moves(watch, limits, walk_world):
    for _ in range(5):
        watch.note_start("g4f", "a")
    chain = [("g4f", "a"), ("nvidia", "b"), ("dahl", "c")]
    out = _drain(chain, pinned=True)
    assert out[0] == ("g4f", "a")                      # the user named it
    assert sorted(out) == sorted(chain)


def test_a_pinned_walk_still_spreads_its_fallback_hops(watch, limits, walk_world):
    for _ in range(17):                       # 17 of 20 per minute = 85%
        watch.note_start("llm7", "b")
    # the named head is fine; the first FALLBACK is saturated -> the next one
    out = _drain([("nvidia", "a"), ("llm7", "b"), ("dahl", "c")], pinned=True)
    assert out == [("nvidia", "a"), ("dahl", "c"), ("llm7", "b")]


def test_the_walk_does_not_leapfrog_a_junk_benched_pair(watch, limits, walk_world, monkeypatch):
    monkeypatch.setattr(app, "_is_pair_benched", lambda pid, m: (pid, m) == ("nvidia", "b"))
    for _ in range(5):
        watch.note_start("g4f", "a")
    out = _drain([("g4f", "a"), ("nvidia", "b"), ("dahl", "c")])
    assert out[0] == ("dahl", "c")                     # not the benched one
    assert sorted(out) == sorted([("g4f", "a"), ("nvidia", "b"), ("dahl", "c")])


def test_the_walk_does_not_leapfrog_a_recently_failed_pair(watch, limits, walk_world, monkeypatch):
    real = app._recent_hop_failure
    monkeypatch.setattr(app, "_recent_hop_failure",
                        lambda pid, m: "429" if (pid, m) == ("nvidia", "b") else real(pid, m))
    for _ in range(5):
        watch.note_start("g4f", "a")
    out = _drain([("g4f", "a"), ("nvidia", "b"), ("dahl", "c")])
    assert out[0] == ("dahl", "c")                     # a different health group is skipped


def test_the_walk_does_not_leapfrog_a_low_quality_family(watch, limits, walk_world):
    for _ in range(5):
        watch.note_start("g4f", "a")
    out = _drain([("g4f", "a"), ("nvidia", "openai/gpt-oss-20b"), ("dahl", "c")])
    assert out[0] == ("dahl", "c")


def test_the_walk_on_a_tool_turn_keeps_a_sick_pair_behind(watch, limits, walk_world, monkeypatch):
    monkeypatch.setattr(app, "_tool_turn_sick", lambda pid, m: (pid, m) == ("nvidia", "b"))
    monkeypatch.setattr(app, "_tool_turn_slow", lambda pid, m: False)
    monkeypatch.setattr(app, "_may_lead_agentic", lambda s, m: True)
    for _ in range(5):
        watch.note_start("g4f", "a")
    out = _drain([("g4f", "a"), ("nvidia", "b"), ("dahl", "c")], tools=True)
    assert out[0] == ("dahl", "c")


def test_the_walk_flag_off_is_byte_identical(watch, limits, walk_world, monkeypatch):
    for _ in range(5):
        watch.note_start("g4f", "a")
    chain = [("g4f", "a"), ("nvidia", "b"), ("dahl", "c")]
    _flag_off(monkeypatch)
    assert _drain(chain) == chain


def test_the_walk_with_no_pressure_is_the_chain_as_built(watch, limits, walk_world):
    chain = [("g4f", "a"), ("llm7", "b"), ("nvidia", "c"), ("dahl", "d")]
    watch.note_start("g4f", "a")                       # 1 of 5: nowhere near 80%
    assert _drain(chain) == chain


def test_the_walk_with_every_head_saturated_keeps_the_order(watch, limits, walk_world):
    for _ in range(5):
        watch.note_start("g4f", "a")
    for _ in range(20):
        watch.note_start("llm7", "b")
    chain = [("g4f", "a"), ("llm7", "b")]
    assert _drain(chain) == chain


def test_a_clock_built_without_init_still_walks(watch, limits, walk_world):
    clock = app._ChainClock.__new__(app._ChainClock)
    clock.trivial = False                     # what the other bare-clock tests set
    for _ in range(5):
        watch.note_start("g4f", "a")
    with app.app.test_request_context("/v1/chat/completions"):
        chain = [("g4f", "a"), ("nvidia", "b")]
        # the bare clock reads the flag lazily (_ensure_ledgers) and still spreads
        assert list(clock.walk(list(chain))) == [("nvidia", "b"), ("g4f", "a")]


# --------------------------------------------------------------------------- #
# Where the limit comes from (the correctness trap: a header's day-sized limit
# must never be read as a per-minute one).
# --------------------------------------------------------------------------- #
@pytest.fixture
def real_limits(monkeypatch):
    monkeypatch.setattr(quota, "key_count", lambda pid: 1)
    real = config.get_setting
    monkeypatch.setattr(config, "get_setting",
                        lambda name, default=None:
                        None if name == "provider_rate_limits" else real(name, default))
    return monkeypatch


def test_the_static_minute_row_is_the_limit_scaled_by_keys(real_limits):
    assert app._rh_limits("g4f")["per_min"] == 5
    real_limits.setattr(quota, "key_count", lambda pid: 4)   # round-robin start key
    assert app._rh_limits("g4f")["per_min"] == 20
    assert app._rh_limits("llm7")["per_min"] == 80
    assert app._rh_limits("g4f")["src"] == "quota"


def test_a_day_window_row_is_not_a_per_minute_limit(real_limits):
    # openrouter: 50 per DAY in quota.FREE_LIMITS, which must NOT be read as 50/min; its docs
    # (2026-10-27) state 20 requests/min for ':free' ids, and that is what pacing uses.
    assert app._rh_limits("openrouter")["per_min"] == 20
    assert app._rh_limits("groq") is None              # no figure in the repo
    assert app._rh_limits("nvidia") is None            # unknown budget
    assert app._rh_limits("huggingface") is None       # limit 0 (no free tier)


def test_negative_control_a_day_sized_header_never_reads_as_headroom(real_limits):
    """MEASURED shape (observe_headers): g4f sends limit=500 remaining=432 for a
    DAY bucket while the static row is 5/minute. status() would report limit 500
    with window 'minute'. The spreader must keep the 5/minute figure."""
    now = quota.time.time()
    real_limits.setitem(quota._DYNAMIC, "g4f",
                        {"remaining": 432, "limit": 500,
                         "reset_at": now + 86400, "seen": now})
    assert quota.status("g4f")["limit"] == 500          # the trap, demonstrated
    assert app._rh_limits("g4f")["per_min"] == 5        # ... and not followed
    assert app._rh_limits("g4f")["src"] == "quota"


def test_a_header_bucket_that_resets_within_the_minute_is_a_learned_limit(real_limits):
    """The provider says 3 per window and resets in 30 s: tighter than the
    researched 5/minute, so the header is followed (a real tightening)."""
    now = quota.time.time()
    real_limits.setitem(quota._DYNAMIC, "g4f",
                        {"remaining": 1, "limit": 3, "reset_at": now + 30, "seen": now})
    lim = app._rh_limits("g4f")
    assert lim["per_min"] == 3 and lim["src"] == "headers"


def test_a_looser_header_never_overrides_the_researched_figure(real_limits):
    """A retry-after reset next to a huge (token- or day-sized) limit must not
    loosen g4f's 5/minute just as it gets hot: the tighter figure wins."""
    now = quota.time.time()
    real_limits.setitem(quota._DYNAMIC, "g4f",
                        {"remaining": 30, "limit": 60, "reset_at": now + 30, "seen": now})
    lim = app._rh_limits("g4f")
    assert lim["per_min"] == 5 and lim["src"] == "quota"


def test_a_header_with_no_reset_is_not_learned(real_limits):
    now = quota.time.time()
    real_limits.setitem(quota._DYNAMIC, "g4f",
                        {"remaining": 1, "limit": 3, "reset_at": None, "seen": now})
    assert app._rh_limits("g4f")["per_min"] == 5 and app._rh_limits("g4f")["src"] == "quota"


def test_the_tightest_figure_wins_over_a_note_hint_too(real_limits):
    # sambanova has no minute row: its notes say 20 RPM; a header saying 4 binds
    now = quota.time.time()
    assert app._rh_limits("sambanova")["per_min"] == 20
    real_limits.setitem(quota._DYNAMIC, "sambanova",
                        {"remaining": 2, "limit": 4, "reset_at": now + 20, "seen": now})
    lim = app._rh_limits("sambanova")
    assert lim["per_min"] == 4 and lim["src"] == "headers"


def test_explicit_per_second_hints_come_from_the_provider_notes(real_limits):
    # providers.py notes: glm "~1 req/s"
    assert app._rh_limits("glm")["per_sec"] == 1
    assert app._rh_limits("sambanova")["per_min"] == 20


def test_the_owner_can_declare_a_limit(monkeypatch):
    monkeypatch.setattr(quota, "key_count", lambda pid: 1)
    real = config.get_setting
    declared = {"groq": {"per_min": 30, "per_model": True},
                "junk": {"per_min": -4}, "bad": "x", "g4f": {"per_min": 50}}
    monkeypatch.setattr(config, "get_setting",
                        lambda name, default=None:
                        declared if name == "provider_rate_limits" else real(name, default))
    lim = app._rh_limits("groq")
    assert lim["per_min"] == 30 and lim["per_model"] is True and lim["src"] == "setting"
    assert app._rh_limits("g4f")["per_min"] == 50       # the declaration wins
    assert app._rh_limits("junk") is None
    assert app._rh_limits("bad") is None


# --------------------------------------------------------------------------- #
# Pooled, not per key: the counter sums every key of a provider, so the limit it
# is compared with must be the pool's.
# --------------------------------------------------------------------------- #
def test_a_header_reading_from_one_key_is_scaled_to_the_whole_pool(real_limits):
    """One key of four has answered (limit 2, resets in 30 s): the pool is four
    allowances, so 8 -- not the 2 of the single key that happened to be heard."""
    now = quota.time.time()
    real_limits.setattr(quota, "key_count", lambda pid: 4)
    real_limits.setitem(quota._DYNAMIC, "g4f|abcd",
                        {"remaining": 1, "limit": 2, "reset_at": now + 30, "seen": now})
    lim = app._rh_limits("g4f")
    assert lim["per_min"] == 8 and lim["src"] == "headers"   # the static pool is 20
    # all four heard: their limits are simply summed (status() does the same)
    for k in ("b", "c", "d"):
        real_limits.setitem(quota._DYNAMIC, "g4f|" + k,
                            {"remaining": 1, "limit": 2, "reset_at": now + 30, "seen": now})
    assert app._rh_limits("g4f")["per_min"] == 8


def test_a_multi_key_provider_is_not_pressed_by_one_keys_allowance(watch, real_limits):
    """4 keys x 5/min = 20/min: eight starts (spread over the keys) is 40%, not
    the 160% a one-key figure would read."""
    real_limits.setattr(quota, "key_count", lambda pid: 4)
    for _ in range(8):
        watch.note_start("g4f", "m")
    assert app._rh_headroom("g4f") == pytest.approx(0.6)
    assert not app._rh_pressed("g4f")
    for _ in range(8):
        watch.note_start("g4f", "m")
    assert app._rh_pressed("g4f")                      # 16 of 20 = 80%


def test_a_per_second_hint_is_scaled_by_the_pool_too(real_limits):
    real_limits.setattr(quota, "key_count", lambda pid: 3)
    assert app._rh_limits("glm")["per_sec"] == 3


def test_per_model_header_providers_are_never_learned_from_headers(real_limits):
    """groq/cerebras headers describe ONE model's bucket; read provider-wide they
    would press every sibling. Only the owner's declaration can name them."""
    now = quota.time.time()
    real_limits.setitem(quota._DYNAMIC, "groq",
                        {"remaining": 3, "limit": 6, "reset_at": now + 30, "seen": now})
    assert app._rh_limits("groq") is None
    # cerebras: the header (limit 6) is NOT read; only its documented 5 RPM (notes) is known
    assert app._rh_limits("cerebras")["per_min"] == 5 and app._rh_limits("cerebras")["src"] == "notes"


def test_a_header_bucket_that_may_be_per_second_is_not_a_minute_limit(real_limits):
    now = quota.time.time()
    real_limits.setitem(quota._DYNAMIC, "g4f",
                        {"remaining": 1, "limit": 3, "reset_at": now + 0.8, "seen": now})
    assert app._rh_limits("g4f")["src"] == "quota"


def test_an_unknown_limit_is_no_effect_never_full(watch, real_limits):
    for _ in range(500):
        watch.note_start("nvidia", "m")
    assert app._rh_headroom("nvidia") is None
    assert not app._rh_pressed("nvidia")


# --------------------------------------------------------------------------- #
# Feeding the counter: the one place a request is really sent.
# --------------------------------------------------------------------------- #
class _Up:
    """A requests-like response for a fake requests.post."""
    def __init__(self, status=200, text=""):
        self.status_code = status
        self.text = text
        self.headers = {}
        self._payload = {"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": "OK"}}]}

    def json(self):
        return self._payload

    def close(self):
        pass

    def iter_lines(self, decode_unicode=False):
        return iter(())


@pytest.fixture
def quiet_config(monkeypatch):
    d = tempfile.mkdtemp(prefix="hub-pytest-rate-")
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", os.path.join(d, "state", "config.json"))
    yield d


_MSGS = [{"role": "user", "content": "hi"}]


def test_a_real_request_is_counted_once_when_it_is_sent(quiet_config, watch, monkeypatch):
    monkeypatch.setattr(app.requests, "post", lambda *a, **k: _Up())
    for _ in range(3):
        app._upstream_chat("uncloseai", {"model": "m", "messages": _MSGS}, False)
    assert watch.counts("uncloseai") == (3, 3)
    assert watch.counts("uncloseai", "m") == (3, 3)


def test_a_streamed_request_is_counted_once_too(quiet_config, watch, monkeypatch):
    monkeypatch.setattr(app.requests, "post", lambda *a, **k: _Up())
    app._upstream_chat("uncloseai", {"model": "m", "messages": _MSGS}, True)
    assert watch.counts("uncloseai")[1] == 1


def test_a_failed_attempt_still_counts_because_it_hit_the_provider(quiet_config, watch,
                                                                    monkeypatch):
    def boom(*a, **k):
        raise app.requests.ConnectionError("down")
    monkeypatch.setattr(app.requests, "post", boom)
    with pytest.raises(app.requests.ConnectionError):
        app._upstream_chat("uncloseai", {"model": "m", "messages": _MSGS}, False)
    assert watch.counts("uncloseai")[1] == 1


def test_a_transparent_retry_that_really_sends_counts_once_more(quiet_config, watch,
                                                                monkeypatch):
    """A provider that refuses stream_options is retried at once without it: two
    requests really left, so two are counted -- each exactly once."""
    sent = []

    def post(*a, **k):
        sent.append(1)
        if len(sent) == 1:
            return _Up(400, "unknown parameter stream_options")
        return _Up()

    monkeypatch.setattr(app.requests, "post", post)
    try:
        app._upstream_chat("uncloseai", {"model": "m", "messages": _MSGS, "stream": True,
                                         "stream_options": {"include_usage": True}}, True)
    finally:
        app._NO_STREAM_OPTIONS.discard("uncloseai")
    assert len(sent) == 2
    assert watch.counts("uncloseai")[1] == 2


def test_a_hop_that_raises_before_sending_counts_nothing(quiet_config, watch, monkeypatch):
    called = []
    monkeypatch.setattr(app.requests, "post", lambda *a, **k: called.append(1))
    with pytest.raises(RuntimeError):                  # nvidia needs a key; none saved
        app._upstream_chat("nvidia", {"model": "m", "messages": _MSGS}, False)
    assert not called and watch.busy() == {}


def test_picking_walking_and_planning_never_count(watch, limits, walk_world):
    """Only a request that is really sent counts: the pick, the band narrowing, the
    walk (a generator that merely yields hops) and the pinned-host choice do not."""
    band = [(138.0, "g4f", "m1"), (137.9, "nvidia", "m2")]
    app._fair_spread_band(band, est=90000)
    app._pick_same_model_host([(138.0, "g4f", "x"), (138.0, "nvidia", "x")], ("g4f", "x"))
    _drain([("g4f", "a"), ("nvidia", "b"), ("dahl", "c")])
    assert watch.busy() == {}


# --------------------------------------------------------------------------- #
# Explicit picks and the orchestrator are never moved; the key is the ordering key.
# --------------------------------------------------------------------------- #
def test_the_orchestrators_pick_never_moves_even_when_saturated(watch, limits, walk_world):
    for _ in range(5):
        watch.note_start("g4f", "a")
    chain = [("g4f", "a"), ("nvidia", "b"), ("dahl", "c")]
    with app.app.test_request_context("/v1/chat/completions"):
        app.g.hub_orchestrator_pair = ("g4f", "a")
        clock = app._ChainClock(tools=False, est=2000)
        out = list(clock.walk(list(chain)))
    assert out[0] == ("g4f", "a")                      # the user's chosen model opens the turn
    assert sorted(out) == sorted(chain)


def test_a_tool_turn_judges_the_band_on_the_agentic_key(watch, limits, walk_world,
                                                         monkeypatch):
    """Equal chat scores are not 'equally good' on a tool turn: the chain was
    ordered by _agentic_score, so a candidate 5 points back there is not promoted
    over a saturated head even though the benchmark scores tie."""
    agentic = {"g4f": 140.0, "nvidia": 135.0, "dahl": 139.5}
    monkeypatch.setattr(app, "_agentic_score", lambda c, *a, **k: agentic[c[1]])
    monkeypatch.setattr(app, "_tool_turn_sick", lambda pid, m: False)
    monkeypatch.setattr(app, "_tool_turn_slow", lambda pid, m: False)
    monkeypatch.setattr(app, "_may_lead_agentic", lambda s, m: True)
    for _ in range(5):
        watch.note_start("g4f", "a")
    out = _drain([("g4f", "a"), ("nvidia", "b"), ("dahl", "c")], tools=True)
    assert out[0] == ("dahl", "c")                     # nvidia is 5 back on the tool key


def test_a_trivial_turn_never_promotes_a_slow_model_over_the_fast_tier(
        watch, limits, walk_world, monkeypatch):
    """A one-liner walks fast models first (a slow thinker costs it a minute). A
    pressed fast head whose next fast hop is out of the band must NOT hand the turn
    to a later, higher-scoring SLOW hop: the fast/slow tier is part of the group."""
    walk_world[("g4f", "a")] = 138.0
    walk_world[("nvidia", "b")] = 130.0                # fast but 8 back: out of the band
    walk_world[("dahl", "c")] = 139.0                  # slow thinker, in the band
    monkeypatch.setattr(app, "_is_fast", lambda pid, m: pid in ("g4f", "nvidia"))
    for _ in range(5):
        watch.note_start("g4f", "a")
    chain = [("g4f", "a"), ("nvidia", "b"), ("dahl", "c")]
    out = _drain(chain, trivial=True)
    assert out == chain                                # nothing promoted
    # negative control: with the tier out of the signature the slow hop WOULD win
    rest = list(chain)
    pressed = lambda e: e[0] == "g4f"
    sc = {("g4f", "a"): 138.0, ("nvidia", "b"): 130.0, ("dahl", "c"): 139.0}
    assert ratehead.walk_index(rest, 0, pressed, lambda e: sc[e], lambda e: 0, 2.0) == 2


def test_a_throttled_candidate_is_not_promoted(watch, limits, walk_world, monkeypatch):
    monkeypatch.setattr(quota, "is_model_throttled", lambda pid, m: (pid, m) == ("nvidia", "b"))
    for _ in range(5):
        watch.note_start("g4f", "a")
    out = _drain([("g4f", "a"), ("nvidia", "b"), ("dahl", "c")])
    assert out[0] == ("dahl", "c")


def test_pick_and_pinned_host_ride_on_provider_fairness(watch, limits, monkeypatch):
    for _ in range(5):
        watch.note_start("g4f", "m1")
    real = config.get_flag
    monkeypatch.setattr(config, "get_flag",
                        lambda n, default=False: False if n == "provider_fairness"
                        else real(n, default))
    band = [(138.0, "g4f", "m1"), (137.9, "nvidia", "m2")]
    assert app._fair_spread_band(band, est=90000) == band
    monkeypatch.setattr(app, "_benchmark_score", lambda pid, m, _rank=True: 138.0)
    pool = [(138.0, "g4f", "x"), (138.0, "nvidia", "x")]
    for _ in range(5):
        watch.note_start("g4f", "x")
    seen = {app._pick_same_model_host(pool, ("g4f", "x"))[0] for _ in range(60)}
    assert seen == {"g4f", "nvidia"}                   # fairness off: nothing yields


# --------------------------------------------------------------------------- #
# Visibility: GET /api/provider-load (no new route).
# --------------------------------------------------------------------------- #
@pytest.fixture
def isolated_config(monkeypatch):
    root = tempfile.mkdtemp(prefix="hub-pytest-")
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", os.path.join(root, "state", "config.json"))
    yield root


def _hdrs():
    return {"X-Free-LLM-Hub": "dashboard",
            "X-Free-LLM-Hub-Token": config.ensure_control_token()}


def test_provider_load_rows_carry_the_rate_numbers(isolated_config, watch, limits, monkeypatch):
    config.ensure_control_token()
    monkeypatch.setattr(app, "_enabled_keyed", lambda: ["g4f", "llm7", "nvidia"])
    for _ in range(4):
        watch.note_start("g4f", "m1")
    watch.note_start("nvidia", "m2")
    j = app.app.test_client().get("/api/provider-load", headers=_hdrs()).get_json()
    rows = {r["provider"]: r for r in j["providers"]}
    g = rows["g4f"]
    assert g["starts_60s"] == 4 and g["starts_1s"] == 4
    assert g["rate_limit_per_min"] == 5
    assert g["rate_limit_src"] == "test"
    assert g["headroom"] == pytest.approx(0.2)
    assert g["pressed"] is True
    assert rows["llm7"]["starts_60s"] == 0
    assert rows["llm7"]["headroom"] == 1.0 and rows["llm7"]["pressed"] is False
    n = rows["nvidia"]
    assert n["starts_60s"] == 1 and n["rate_limit_per_min"] is None
    assert n["headroom"] is None and n["pressed"] is False
    assert j["rate_headroom_on"] is True
    assert j["rate_yield_at"] == pytest.approx(0.8)


def test_provider_load_still_answers_with_the_flag_off(isolated_config, watch, limits, monkeypatch):
    config.ensure_control_token()
    monkeypatch.setattr(app, "_enabled_keyed", lambda: ["g4f"])
    _flag_off(monkeypatch)
    r = app.app.test_client().get("/api/provider-load", headers=_hdrs())
    assert r.status_code == 200
    assert r.get_json()["rate_headroom_on"] is False
