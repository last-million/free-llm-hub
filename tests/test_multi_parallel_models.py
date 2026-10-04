r"""Multi: up to 6 DIFFERENT models at once, never at the cost of the user's RAM.

Owner, 2026-10-04: "I prefer 4 DIFFERENT models at once, or 5-6 if needed,
always working TOGETHER" and "the user's own programs come first".

Covered: the concurrency formula (setting / machine / fleet / 429 back-off),
distinct-model rotation across six workers, the planner's parallel ask,
MAX_AGENTS 10, PAIR mode, the panel text, and the live RAM/CPU governor
(fake machine, fake clock, fake processes, fake priority setter).

NOTE: no pytest tmp_path for config -- this machine's basetemp is
permission-denied; the config lives in tempfile.mkdtemp.
"""
import os
import shutil
import tempfile
import threading
import time

import pytest

import app as A
import config
import lowres
import swarm_windows as SW

H = {"X-Free-LLM-Hub": "dashboard"}
BIG = {"total_gb": 40.0, "free_gb": 20.0, "cores": 8}
WEAK = {"total_gb": 4.0, "free_gb": 2.0, "cores": 4}


@pytest.fixture
def cfg(monkeypatch):
    root = tempfile.mkdtemp(prefix="hub-pytest-")
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", os.path.join(root, "state", "config.json"))
    lowres._CACHE.update(at=0.0, value=None)
    monkeypatch.setattr(SW, "_HOOKS", {"fleet": None, "rate429": None})
    monkeypatch.setattr(lowres, "machine", lambda: dict(BIG))
    try:
        yield root
    finally:
        lowres._CACHE.update(at=0.0, value=None)
        shutil.rmtree(root, ignore_errors=True)


def _machine(monkeypatch, m):
    monkeypatch.setattr(lowres, "machine", lambda: dict(m))


# --------------------------------------------------------------------------- #
# 1. the concurrency formula
# --------------------------------------------------------------------------- #

def test_default_is_six_on_a_big_machine(cfg):
    assert SW.MAX_CONCURRENT == 6
    assert SW._concurrency() == 6


def test_the_setting_caps_and_is_clamped(cfg):
    config.set_setting("multi_parallel_max", 3)
    assert SW._concurrency() == 3
    config.set_setting("multi_parallel_max", 99)
    assert SW.parallel_cap() == SW.MAX_PARALLEL_LIMIT == 8
    assert SW._concurrency() == 8
    config.set_setting("multi_parallel_max", 0)
    assert SW._concurrency() == 1


def test_cores_and_free_ram_bound_the_machine_part(cfg, monkeypatch):
    _machine(monkeypatch, {"total_gb": 16.0, "free_gb": 20.0, "cores": 4})
    assert SW._concurrency() == 4                      # cores
    _machine(monkeypatch, {"total_gb": 40.0, "free_gb": 2.0, "cores": 8})
    assert SW._concurrency() == 4                      # 2 GB / 0.5 GB each
    _machine(monkeypatch, {"total_gb": 40.0, "free_gb": 1.0, "cores": 8})
    assert SW._concurrency() == 1                      # free RAM short: one


def test_a_weak_machine_is_unchanged(cfg, monkeypatch):
    _machine(monkeypatch, WEAK)
    assert SW._concurrency() == 1                      # lowres.workers: < 6 GB
    config.set_setting("low_resource_mode", "off")
    assert SW._concurrency() == 6                      # off = full speed, no RAM guard


def test_unreadable_numbers_behave_as_before(cfg, monkeypatch):
    _machine(monkeypatch, {"total_gb": None, "free_gb": None, "cores": None})
    assert SW._concurrency() == SW.LEGACY_CONCURRENT == 4


def test_fleet_limits_but_never_under_two(cfg):
    SW._HOOKS["fleet"] = lambda: 3
    assert SW._concurrency() == 3
    SW._HOOKS["fleet"] = lambda: 1
    assert SW._concurrency() == 2
    SW._HOOKS["fleet"] = lambda: 0                      # unknown = no limit
    assert SW._concurrency() == 6
    SW._HOOKS["fleet"] = lambda: 1 / 0                  # broken hook = no limit
    assert SW._concurrency() == 6


def test_429_backoff_one_per_three_floor_two(cfg, caplog):
    SW._HOOKS["rate429"] = lambda window: 2
    assert SW._concurrency() == 6
    SW._HOOKS["rate429"] = lambda window: 7             # 7 // 3 = 2 fewer
    with caplog.at_level("INFO", logger="free-llm-hub"):
        assert SW._concurrency() == 4
    assert any("backing off to 4 (429s)" in r.getMessage() for r in caplog.records)
    SW._HOOKS["rate429"] = lambda window: 60
    assert SW._concurrency() == 2
    assert SW.concurrency_info()["limited_by"] == "429s"


def test_the_app_registers_its_fleet_and_429_counters():
    assert SW._HOOKS["fleet"] is A._multi_fleet_size or callable(SW._HOOKS["fleet"])
    assert callable(A._recent_429_count)


def test_429_counter_counts_recent_pairs_only(monkeypatch):
    now = time.time()
    monkeypatch.setattr(A, "_recent_hop_fail", {
        ("a", "m1"): (now - 5, "429"), ("a", "m2"): (now - 30, "429"),
        ("b", "m3"): (now - 600, "429"), ("c", "m4"): (now - 5, "timeout")})
    assert A._recent_429_count(120.0) == 2


def test_fleet_counts_distinct_healthy_good_models(monkeypatch):
    fleet = [("p1", "z-ai/glm-5.3", 1, 138.0), ("p2", "z-ai/glm-5.3", 1, 138.0),
             ("p1", "moonshotai/kimi-k3", 1, 135.0), ("p3", "qwen/qwen3.8-27b", 1, 134.0),
             ("p3", "weak/tiny", 1, 40.0), ("p4", "nvidia/nemotron-3-ultra", 1, 137.0),
             ("p5", "deepseek/deepseek-v4-pro", 1, 133.0)]
    monkeypatch.setattr(A, "_declared_fleet", lambda: fleet)
    monkeypatch.setattr(A, "_swarm_member_sick",
                        lambda p, m: "throttled" if p == "p5" else None)
    # glm (two listings = one identity), kimi, qwen; tiny is outside the band,
    # nemotron is low-quality, p5 is throttled
    assert A._multi_fleet_size() == 3


# --------------------------------------------------------------------------- #
# 2. different models by construction
# --------------------------------------------------------------------------- #

POOL = [(138.0, "nvidia", "z-ai/glm-5.3"),
        (138.0, "kilocode", "z-ai/glm-5.3"),                  # same identity, other host
        (137.7, "openrouter", "stealth/space-bunny-alpha"),
        (136.5, "nvidia", "moonshotai/kimi-k3"),
        (135.0, "google", "gemini-3.5-flash"),
        (134.0, "groq", "qwen/qwen3.8-27b"),
        (133.0, "mistral", "deepseek/deepseek-v4-pro"),
        (137.0, "nvidia", "nvidia/nemotron-3-ultra"),         # low quality
        (125.0, "acme", "acme/mid-tier")]


def _run(monkeypatch, n_workers, pool=POOL):
    """Pick `n_workers` workers one after another, each taking the first model
    the rotation leaves; return their (pid, model)."""
    monkeypatch.setattr(A, "_WORKER_MODEL", {})
    monkeypatch.setattr(A, "_WORKER_PID", {})
    sids = ["w-%d" % i for i in range(n_workers)]
    got = []
    for i, sid in enumerate(sids):
        monkeypatch.setattr(A.swarm_windows, "sibling_sessions",
                            lambda s, _prev=sids[:i]: list(_prev))
        pick = A._rotate_within_run(list(pool), sid)[0]
        A._note_worker_model(sid, pick[2], pick[1])
        got.append((pick[1], pick[2]))
    return got


def test_six_workers_get_six_distinct_identities(monkeypatch):
    got = _run(monkeypatch, 6)
    idents = [A._normalize_model_identity(m) for _p, m in got]
    assert len(set(idents)) == 6, got
    assert all("nemotron" not in m and "mid-tier" not in m for _p, m in got)


def test_the_wide_band_opens_only_when_the_narrow_one_is_short(monkeypatch):
    # five distinct identities within 4 points of 138 (glm, bunny, kimi,
    # gemini, qwen): the sixth worker is the first to need the 6-point band
    got = _run(monkeypatch, 5)
    assert "deepseek/deepseek-v4-pro" not in [m for _p, m in got]
    got = _run(monkeypatch, 6)
    assert got[5][1] == "deepseek/deepseek-v4-pro"


def test_a_model_outside_the_wide_band_never_joins(monkeypatch):
    got = _run(monkeypatch, 8)                      # more workers than good models
    assert "acme/mid-tier" not in [m for _p, m in got]
    assert not any("nemotron" in m for _p, m in got)


def test_a_new_provider_and_family_beats_a_closer_score(monkeypatch):
    pool = [(138.0, "nvidia", "z-ai/glm-5.3"),
            (137.5, "nvidia", "moonshotai/kimi-k3"),         # same provider
            (136.0, "google", "gemini-3.5-flash")]           # new provider + family
    got = _run(monkeypatch, 2, pool)
    assert got[1] == ("google", "gemini-3.5-flash")


def test_a_new_family_is_preferred_when_no_new_provider(monkeypatch):
    pool = [(138.0, "nvidia", "z-ai/glm-5.3"),
            (137.5, "nvidia", "z-ai/glm-5.2"),               # same family
            (136.0, "nvidia", "moonshotai/kimi-k3")]         # other family
    got = _run(monkeypatch, 2, pool)
    assert got[1][1] == "moonshotai/kimi-k3"


def test_user_blocked_models_are_never_rotated_onto(monkeypatch):
    monkeypatch.setattr(A, "_is_model_blocked_by_user",
                        lambda p, m: m == "moonshotai/kimi-k3")
    got = _run(monkeypatch, 6)
    assert "moonshotai/kimi-k3" not in [m for _p, m in got]


# --------------------------------------------------------------------------- #
# 3. the planner asks for real parallelism
# --------------------------------------------------------------------------- #

BIG_GOAL = ("Build the whole thing:\n- an API with auth\n- a React dashboard\n"
            "- a billing module\n- docs and tests for all of it")


def test_the_prompt_names_the_helper_count_and_asks_for_micro_tasks(cfg):
    for managed in (False, True):
        s = SW.plan_system(BIG_GOAL, managed, helpers=6)
        assert "up to 6 agents" in s
        assert "MICRO-TASKS" in s and "6 helpers" in s
        assert '"parallel": true' in s and "integrate/review" in s


def test_a_small_goal_gets_no_ceremony(cfg):
    s = SW.plan_system("Fix the typo in README.md", False, helpers=6)
    assert "up to 6 agents" in s
    assert "MICRO-TASKS" not in s
    assert not SW.sizeable_goal("Fix the typo in README.md")
    assert SW.sizeable_goal(BIG_GOAL)


def test_no_micro_ask_when_fewer_than_three_helpers(cfg):
    assert "MICRO-TASKS" not in SW.plan_system(BIG_GOAL, False, helpers=2)


def test_the_prompt_uses_the_live_concurrency_by_default(cfg):
    config.set_setting("multi_parallel_max", 5)
    assert "up to 5 agents" in SW.plan_system("x")


def test_ten_phases_are_kept_and_the_eleventh_dropped():
    assert SW.MAX_AGENTS == 10
    plan = {"phases": [{"title": "P%d" % i, "task": "do %d" % i} for i in range(12)]}
    notes = []
    assert len(SW.clean_phases(plan, notes=notes)) == 10
    assert any(n["kind"] == "too_many_phases" for n in notes)
    assert "Between 2 and 10 phases" in SW._PLAN_SYSTEM


def test_the_parallel_mark_survives_cleaning():
    got = SW.clean_phases({"phases": [{"title": "A", "task": "t", "parallel": True},
                                      {"title": "B", "task": "t"}]})
    assert got[0].get("parallel") is True and "parallel" not in got[1]


def test_the_plan_check_line_says_how_many_run_at_once():
    import plan_check
    line = plan_check.check_line({"phases": 5, "start_now": 3, "max_parallel": 6})
    assert "running up to 6 helpers at once" in line
    assert "helpers at once" not in plan_check.check_line({"phases": 1, "start_now": 1,
                                                           "max_parallel": 6})


# --------------------------------------------------------------------------- #
# 4. PAIR mode
# --------------------------------------------------------------------------- #

@pytest.fixture
def calm(cfg, tmp_path, monkeypatch):
    """No live governor, no stagger, a private run store."""
    monkeypatch.setenv(SW._STORE_ENV, str(tmp_path / "runs"))
    monkeypatch.setattr(SW, "SPAWN_STAGGER", 0)
    monkeypatch.setattr(SW, "RETRY_BACKOFF", 0)
    monkeypatch.setattr(lowres, "acquire_monitor", lambda: None)
    monkeypatch.setattr(lowres, "release_monitor", lambda: None)
    monkeypatch.setattr(lowres, "GOV", lowres.Governor())
    SW._RUNS.clear()
    d = tmp_path / "proj"
    d.mkdir()
    yield str(d)
    for run in list(SW._RUNS.values()):
        run.stop_flag.set()
    SW._RUNS.clear()


class _Fake:
    """spawn / run_turn / stop fakes. The lead waits for the pair to start (or
    not), the pair waits to be stopped (or finishes at once)."""

    def __init__(self, pair_blocks=False):
        self.n = 0
        self.lock = threading.Lock()
        self.prompts = {}
        self.stopped = []
        self.pair_started = threading.Event()
        self.pair_stop = threading.Event()
        self.pair_blocks = pair_blocks
        self.lead_wait_for_pair = True

    def spawn(self, cli, project):
        with self.lock:
            self.n += 1
            return "s%d" % self.n

    def stop(self, sid):
        self.stopped.append(sid)
        self.pair_stop.set()

    def run_turn(self, sid, prompt):
        self.prompts[sid] = prompt
        if prompt.startswith("You are the CO-PILOT"):
            self.pair_started.set()
            if self.pair_blocks:
                self.pair_stop.wait(10)
                return iter([])                      # stopped: no result
            return iter([{"event": "message", "text": "pair found a bug in x.py"}])
        if self.lead_wait_for_pair:
            self.pair_started.wait(8)
        return iter([{"event": "message", "text": "lead did the work"}])


BIGPHASE = {"title": "Big", "task": "build everything", "files": ["a.py", "b.py", "c.py", "d.py"]}


def _go(calm, fake, phases):
    rid = SW.start("goal", calm, "opencode", fake.spawn, fake.run_turn, phases=phases,
                   review=False, stop=fake.stop)
    end = time.time() + 30
    while time.time() < end:
        st = SW.status(rid)
        if st["state"] in (SW.DONE, SW.FAILED, SW.STOPPED):
            return rid, st
        time.sleep(0.02)
    return rid, SW.status(rid)


def test_a_big_phase_gets_a_pair_and_the_summaries_merge(calm):
    fake = _Fake()
    rid, st = _go(calm, fake, [dict(BIGPHASE)])
    a = SW.get(rid).agents[0]
    assert a.pair_state == "done" and a.pair_session and a.pair_session != a.session_id
    assert "lead did the work" in a.summary and "CO-PILOT" in a.summary
    assert "pair found a bug in x.py" in a.summary
    assert st["agents"][0]["pair"]["state"] == "done"


def test_the_pair_is_ownership_safe(calm):
    fake = _Fake()
    phases = [dict(BIGPHASE), {"title": "Docs", "task": "docs", "files": ["README.md"]}]
    rid, _st = _go(calm, fake, phases)
    prompt = next(p for p in fake.prompts.values() if p.startswith("You are the CO-PILOT"))
    assert "NEVER edit or overwrite a file the lead owns: a.py, b.py, c.py, d.py" in prompt
    assert "README.md" in prompt                        # owned by another phase too
    assert "Do NOT run installs" in prompt and "PROGRESS.md" in prompt


def test_the_pair_ends_when_the_lead_ends(calm):
    fake = _Fake(pair_blocks=True)
    rid, _st = _go(calm, fake, [dict(BIGPHASE)])
    a = SW.get(rid).agents[0]
    assert a.pair_session in fake.stopped
    assert a.pair_state == "stopped"
    assert a.state == SW.DONE and "lead did the work" in a.summary


def test_the_parallel_mark_earns_a_pair_without_many_files(calm):
    fake = _Fake()
    rid, _st = _go(calm, fake, [{"title": "Big", "task": "t", "parallel": True}])
    assert SW.get(rid).agents[0].pair_state == "done"


def test_a_small_phase_gets_no_pair(calm):
    fake = _Fake()
    fake.lead_wait_for_pair = False
    rid, _st = _go(calm, fake, [{"title": "S", "task": "t", "files": ["a.py"]}])
    assert SW.get(rid).agents[0].pair_state is None
    assert not fake.pair_started.is_set()


def test_the_flag_turns_pairs_off(calm):
    config.set_setting("multi_pair_phases", False)
    fake = _Fake()
    fake.lead_wait_for_pair = False
    rid, _st = _go(calm, fake, [dict(BIGPHASE)])
    assert SW.get(rid).agents[0].pair_state is None


def test_no_pair_without_a_spare_slot(calm):
    config.set_setting("multi_parallel_max", 1)
    fake = _Fake()
    fake.lead_wait_for_pair = False
    rid, _st = _go(calm, fake, [dict(BIGPHASE)])
    assert SW.get(rid).agents[0].pair_state is None


def test_no_pair_while_another_phase_waits_for_a_slot(calm):
    config.set_setting("multi_parallel_max", 2)
    fake = _Fake()
    fake.lead_wait_for_pair = False
    # phase 2 is still pending (needs 1) while phase 1 runs: its slot is not spare
    rid, _st = _go(calm, fake, [dict(BIGPHASE), {"title": "After", "task": "t", "needs": [1]}])
    assert SW.get(rid).agents[0].pair_state is None


def test_the_pair_is_a_sibling_so_the_hub_picks_another_model(calm):
    fake = _Fake(pair_blocks=True)
    rid, _st = _go(calm, fake, [dict(BIGPHASE)])
    a = SW.get(rid).agents[0]
    assert a.pair_session in SW.sibling_sessions(a.session_id)
    assert a.session_id in SW.sibling_sessions(a.pair_session)


def test_pair_state_round_trips_through_the_run_file(calm):
    fake = _Fake()
    rid, _st = _go(calm, fake, [dict(BIGPHASE)])
    row = SW.get(rid).agents[0].row()
    assert row["pair"]["state"] == "done" and row["parallel"] is False


# --------------------------------------------------------------------------- #
# 5. what the page says
# --------------------------------------------------------------------------- #

def test_the_header_line_and_pair_marker(calm):
    fake = _Fake()
    rid, _st = _go(calm, fake, [dict(BIGPHASE)])
    pv = SW.parallel_view(SW.get(rid))
    assert pv["line"] == "0 helpers at once (max 6)"
    assert pv["pairs"] == [1]


def test_pair_view_for_the_panel(monkeypatch):
    class Ag:
        pair_state, pair_session = "running", "s9"
    monkeypatch.setattr(A, "_WORKER_MODEL", {"s9": "kimi-k3"})
    assert A._multi_pair_view(Ag()) == {"model": "kimi-k3", "state": "running",
                                        "session_id": "s9", "url": "/agent/s9"}

    class No:
        pair_state = None
    assert A._multi_pair_view(No()) is None


# --------------------------------------------------------------------------- #
# 6. the settings API
# --------------------------------------------------------------------------- #

def test_api_multi_parallel_roundtrip(cfg):
    c = A.app.test_client()
    v = c.get("/api/multi-parallel").get_json()
    assert v["setting"] == 6 and v["now"] == 6 and v["limit"] == 8
    v = c.post("/api/multi-parallel", json={"max": 4, "pair_phases": False,
                                            "ram_reserve_gb": 5}, headers=H).get_json()
    assert v["setting"] == 4 and v["now"] == 4 and v["pair_phases"] is False
    assert v["ram_reserve_gb"] == 5.0
    assert c.post("/api/multi-parallel", json={"max": 9}, headers=H).status_code == 400
    assert c.post("/api/multi-parallel", json={"max": True}, headers=H).status_code == 400
    assert c.post("/api/multi-parallel", json={"pair_phases": "maybe"},
                  headers=H).status_code == 400


def test_api_multi_parallel_is_control_gated(cfg, monkeypatch):
    monkeypatch.setattr(config, "get_control_token", lambda: "secret-token")
    c = A.app.test_client()
    assert c.get("/api/multi-parallel").status_code == 401
    assert c.post("/api/multi-parallel", json={"max": 3}).status_code == 403


def test_low_resource_status_gains_the_governor_fields(cfg):
    v = A.app.test_client().get("/api/low-resource").get_json()
    for k in ("reserve_gb", "allowed_now", "per_helper_gb", "limited_by"):
        assert k in v
    assert v["reserve_gb"] == 8.0 and v["per_helper_gb"] == 0.5


# --------------------------------------------------------------------------- #
# 7. the live RAM / CPU governor
# --------------------------------------------------------------------------- #

class Rig:
    """A Governor on a fake machine, clock, process list, CPU and priority."""

    def __init__(self, total=40.0, free=20.0, cores=8):
        self.m = {"total_gb": total, "free_gb": free, "cores": cores}
        self.t = 1000.0
        self.procs = []
        self.cpu = 10.0
        self.calls = []
        self.g = lowres.Governor(read_machine=lambda: dict(self.m), clock=lambda: self.t,
                                 procs=lambda: list(self.procs), cpu=lambda: self.cpu,
                                 set_priority=lambda pids, low: self.calls.append((list(pids), low)))

    def tick(self, advance=0.0, **machine):
        self.t += advance
        self.m.update(machine)
        self.g.tick()
        return self.g


def test_reserve_is_max_3gb_or_20_percent(cfg):
    assert lowres.reserve_gb({"total_gb": 40.0}) == 8.0
    assert lowres.reserve_gb({"total_gb": 10.0}) == 3.0
    config.set_setting("multi_ram_reserve_gb", 6)
    assert lowres.reserve_gb({"total_gb": 40.0}) == 6.0
    config.set_setting("multi_ram_reserve_gb", "auto")
    assert lowres.reserve_gb({"total_gb": 16.0}) == 3.2


def test_capacity_is_available_minus_reserve_over_helper_cost(cfg):
    g = Rig(total=40, free=20).tick()
    assert g.per_helper_gb() == 0.5 and g.reserve == 8.0
    assert g.headroom() == int((20 - 8) / 0.5) == 24


def test_ram_falling_lowers_at_once_and_blocks_new_spawns(cfg):
    r = Rig(free=20)
    r.tick()
    assert r.g.headroom() == 24
    r.tick(advance=2, free_gb=8.4)                      # user opened a heavy program
    assert r.g.headroom() == 0
    r.tick(advance=2, free_gb=9.2)
    assert r.g.headroom() == 0 or r.g.headroom() <= 2   # raises only after 20 s


def test_a_higher_allowance_must_hold_for_20_seconds(cfg):
    r = Rig(free=8.5)
    r.tick()
    assert r.g.headroom() == 1                          # (8.5 - 8) / .5
    r.tick(advance=1, free_gb=20.0)
    assert r.g.headroom() == 1
    r.tick(advance=10)
    assert r.g.headroom() == 1
    r.tick(advance=10.5)
    assert r.g.headroom() == 24                         # held for 20 s


def test_a_dip_inside_the_window_restarts_nothing_but_caps_the_raise(cfg):
    r = Rig(free=8.5)
    r.tick()
    r.tick(advance=1, free_gb=20.0)
    r.tick(advance=10, free_gb=12.0)                    # dips: raw 8
    r.tick(advance=10.5, free_gb=20.0)
    assert r.g.headroom() == 8                          # the lowest value that held


def test_the_per_helper_cost_is_measured_p90_of_last_10(cfg):
    r = Rig(free=20)
    r.procs = [{"pid": 1, "ppid": 0, "rss_gb": 1.2, "marker": True, "cpu": 1.0},
               {"pid": 2, "ppid": 1, "rss_gb": 0.8, "marker": True, "cpu": 1.0}]
    g = r.tick()
    assert g.helpers == 1 and g.per_helper_gb() == 2.0  # the whole tree
    assert g.headroom() == int(12 / 2.0)
    for i in range(12):                                 # a long history of small ones
        r.procs = [{"pid": 10 + i, "ppid": 0, "rss_gb": 0.4, "marker": True, "cpu": 1.0}]
        r.tick(advance=2)
    assert len(g.samples) == lowres.SAMPLES
    assert g.per_helper_gb() == 0.4


def test_spawns_not_yet_visible_in_ram_are_counted(cfg):
    r = Rig(free=9.0)                                   # room for 2
    g = r.tick()
    assert g.headroom() == 2
    g.note_spawn()
    assert g.headroom() == 1
    r.tick(advance=1)                                   # RAM has not moved yet
    assert g.headroom() == 1                            # the spawn still counts


def test_critical_floor_lowers_priority_then_restores(cfg):
    r = Rig(free=20)
    r.procs = [{"pid": 7, "ppid": 0, "rss_gb": 0.5, "marker": True, "cpu": 1.0}]
    r.tick()
    assert r.calls == []
    r.tick(advance=2, free_gb=0.8)
    assert r.calls == [([7], True)] and r.g.low_prio
    r.tick(advance=2, free_gb=1.2)                      # not recovered yet
    assert len(r.calls) == 1
    r.tick(advance=2, free_gb=3.0)
    assert r.calls[-1] == ([7], False) and not r.g.low_prio


def test_priority_is_not_touched_when_the_mode_is_off(cfg):
    config.set_setting("low_resource_mode", "off")
    r = Rig(free=0.5)
    r.procs = [{"pid": 7, "ppid": 0, "rss_gb": 0.5, "marker": True, "cpu": 1.0}]
    r.tick()
    assert r.calls == [] and r.g.headroom() is None


def test_cpu_hold_after_10_seconds_above_90(cfg):
    r = Rig(free=20)
    r.cpu = 95.0
    r.tick()
    assert r.g.headroom() == 24                         # not yet
    r.tick(advance=5)
    assert r.g.headroom() == 24
    r.tick(advance=6)
    assert r.g.headroom() == 0 and r.g.limited_by == "cpu"
    r.cpu = 30.0
    r.tick(advance=2)
    assert r.g.headroom() == 24


def test_a_failing_monitor_means_no_live_limit(cfg):
    r = Rig()
    r.tick()
    assert r.g.headroom() == 24
    r.g._read = lambda: (_ for _ in ()).throw(RuntimeError("psutil gone"))
    r.tick(advance=2)
    assert r.g.failed and r.g.headroom() is None


def test_unreadable_machine_numbers_mean_no_live_limit(cfg):
    r = Rig()
    r.tick(free_gb=None)
    assert r.g.headroom() is None


def test_a_stale_monitor_means_no_live_limit(cfg):
    r = Rig()
    r.tick()
    r.t += lowres.STALE_SECONDS + 1
    assert r.g.headroom() is None


def test_off_mode_ignores_ram_limits(cfg):
    config.set_setting("low_resource_mode", "off")
    r = Rig(free=0.2)
    r.tick()
    assert r.g.headroom() is None


def test_status_reports_the_governor(cfg, monkeypatch):
    r = Rig(free=9.0)
    r.t = time.monotonic()
    monkeypatch.setattr(lowres, "GOV", r.g)
    r.g._clock = time.monotonic
    r.g.tick()
    s = lowres.status()
    assert s["reserve_gb"] == 8.0 and s["allowed_now"] == 2 and s["per_helper_gb"] == 0.5
    r.m["free_gb"] = 8.0
    r.g.tick()
    assert lowres.status()["limited_by"] == "ram"


# -- the scheduler obeys it, and never touches a running helper ------------- #

def test_over_budget_stops_new_starts_but_running_helpers_finish(calm, monkeypatch):
    g = lowres.Governor(clock=time.monotonic)
    monkeypatch.setattr(lowres, "GOV", g)
    g.allowed, g.last_tick, g.failed = 0, time.monotonic(), False

    def keep_fresh():
        g.last_tick = time.monotonic()
    gate = threading.Event()
    started = []

    def spawn(cli, project):
        return "w%d" % (len(started) + 1)

    def run_turn(sid, prompt):
        started.append((sid, time.time()))
        if sid == "w1":
            gate.wait(10)
        return iter([{"event": "message", "text": "done " + sid}])

    phases = [{"title": "A", "task": "ta"}, {"title": "B", "task": "tb"}]
    rid = SW.start("g", calm, "opencode", spawn, run_turn, phases=phases, review=False)
    for _ in range(40):                                  # A runs; B waits for RAM
        keep_fresh()
        time.sleep(0.05)
    assert [s for s, _t in started] == ["w1"]            # nothing new, A untouched
    assert SW.waiting_for_ram() is True
    g.allowed = 3                                        # RAM came back
    for _ in range(80):
        keep_fresh()
        if len(started) == 2:
            break
        time.sleep(0.05)
    assert [s for s, _t in started] == ["w1", "w2"]
    gate.set()
    end = time.time() + 20
    while time.time() < end and SW.status(rid)["state"] not in (SW.DONE, SW.FAILED):
        keep_fresh()
        time.sleep(0.05)
    st = SW.status(rid)
    assert st["done"] == 2 and all(a["summary"].startswith("done") for a in st["agents"])


def test_a_run_with_nothing_running_always_gets_one_helper(calm, monkeypatch):
    g = lowres.Governor(clock=time.monotonic)
    monkeypatch.setattr(lowres, "GOV", g)
    g.allowed, g.last_tick, g.failed = 0, time.monotonic(), False
    assert g.headroom() == 0
    assert SW.spawn_allowed(0) is True                  # progress is guaranteed
    assert SW.spawn_allowed(1) is False


def test_the_monitor_is_acquired_and_released_around_a_walk(calm, monkeypatch):
    seen = []
    monkeypatch.setattr(lowres, "acquire_monitor", lambda: seen.append("acquire"))
    monkeypatch.setattr(lowres, "release_monitor", lambda: seen.append("release"))
    fake = _Fake()
    fake.lead_wait_for_pair = False
    _go(calm, fake, [{"title": "S", "task": "t"}])
    assert seen == ["acquire", "release"]


def test_the_ram_line_for_the_conversation(cfg, monkeypatch):
    r = Rig(free=20)
    r.g._clock = time.monotonic
    monkeypatch.setattr(lowres, "GOV", r.g)
    r.g.tick()

    class Run:
        class _A:
            state = SW.PENDING
        agents = [_A()]
    pv = SW.parallel_view(Run())
    assert pv["ram_line"] == "RAM: 20.0 GB free, keeping 8.0 GB for your other programs"
    assert not pv["waiting"]
    r.m["free_gb"] = 7.0
    r.g.tick()
    pv = SW.parallel_view(Run())
    assert pv["waiting"] and "waiting for RAM to free up" in pv["ram_line"]
