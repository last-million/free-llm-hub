"""Per-run and per-conversation spend budgets.

A budget (tokens / seconds / calls) stops a Multi run from STARTING new phases
once it is spent: the ones already running finish their turn (no kill, no lost
work), the rest are marked "stopped (budget)", and the run says so. Spending is
MEASURED from the hub's own usage accounting (an injected `spent(run_id)`), the
seconds from the run clock. A resume respects whatever budget is left.

The swarm_windows half is driven with the same fake spawn/run_turn the other
swarm_windows tests use (no process, no model). The app half (the spend ledger,
the conversation cap, the routes) is driven through the test client.
"""
import time

import pytest

import swarm_windows as SW


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setenv(SW._STORE_ENV, str(tmp_path / "runs"))
    SW._RUNS.clear()
    yield
    for run in list(SW._RUNS.values()):
        run.stop_flag.set()
    SW._RUNS.clear()


def _spawn(cli, project):
    _spawn.n += 1
    return "sess-%d" % _spawn.n
_spawn.n = 0


def _wait(run_id, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        st = SW.status(run_id)
        if st and st["state"] in (SW.DONE, SW.FAILED, SW.STOPPED):
            return st
        time.sleep(0.02)
    return SW.status(run_id)


CHAIN = [{"title": "P1", "task": "do 1", "needs": []},
         {"title": "P2", "task": "do 2", "needs": [1]},
         {"title": "P3", "task": "do 3", "needs": [2]}]


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #
def test_normalize_budget():
    assert SW.normalize_budget({"tokens": 5, "seconds": None, "calls": 2}) == \
        {"tokens": 5, "seconds": None, "calls": 2}
    assert SW.normalize_budget({"tokens": None, "seconds": None}) is None
    assert SW.normalize_budget("nope") is None
    assert SW.normalize_budget({"tokens": -3})["tokens"] == 0


def test_budget_check_order_tokens_calls_seconds():
    b = {"tokens": 1000, "calls": 10, "seconds": 60}
    assert SW.budget_check(b, {"tokens": 1000, "calls": 0, "seconds": 0})[1] == "tokens"
    assert SW.budget_check(b, {"tokens": 0, "calls": 10, "seconds": 0})[1] == "calls"
    assert SW.budget_check(b, {"tokens": 0, "calls": 0, "seconds": 60})[1] == "seconds"
    reached, reason, msg = SW.budget_check(b, {"tokens": 999, "calls": 9, "seconds": 59})
    assert reached is False and reason is None and msg is None
    assert SW.budget_check(None, {"tokens": 10 ** 9}) == (False, None, None)


def test_run_measures_tokens_calls_and_seconds():
    run = SW._Run("g", ".", "cli", [{"title": "a", "task": "t", "needs": []}],
                  budget={"seconds": 50}, spent=lambda rid: {"tokens": 7, "calls": 3})
    sp = run.budget_spent()
    assert sp["tokens"] == 7 and sp["calls"] == 3
    run.budget_elapsed = 60.0
    reached, reason, _m = run.budget_status()
    assert reached is True and reason == "seconds"


# --------------------------------------------------------------------------- #
# the stop, end to end
# --------------------------------------------------------------------------- #
def test_an_already_spent_budget_starts_no_phase():
    """cap 0 tokens is reached before anything runs: every phase is stopped
    (budget), nothing is spawned, and the run says so."""
    def run_turn(sid, prompt):
        raise AssertionError("no phase should start")
        yield  # pragma: no cover

    rid = SW.start("goal", ".", "opencode", _spawn, run_turn, phases=CHAIN,
                   review=False, budget={"tokens": 0},
                   spent=lambda r: {"tokens": 0, "calls": 0})
    st = _wait(rid)
    assert st["done"] == 0
    assert all(a["state"] == SW.STOPPED for a in st["agents"])
    assert all(a["error"] == SW.BUDGET_STOPPED_ERROR for a in st["agents"])
    assert "3 tasks left for next time" in (st["budget_note"] or "")
    assert st["budget_reached"] == "tokens"


def test_running_phases_finish_then_the_rest_stop():
    """A chain of three, budget 100 tokens, each finished phase 'costs' 100:
    phase 1 runs to the end, then the budget trips and 2 and 3 are stopped."""
    spent = {"tokens": 0}

    def run_turn(sid, prompt):
        yield {"type": "message", "text": "ok " + sid}
        yield {"type": "done"}
        spent["tokens"] += 100       # counted the moment the turn is consumed

    rid = SW.start("goal", ".", "opencode", _spawn, run_turn, phases=CHAIN,
                   review=False, budget={"tokens": 100},
                   spent=lambda r: {"tokens": spent["tokens"], "calls": 0})
    st = _wait(rid)
    states = [a["state"] for a in st["agents"]]
    assert states[0] == SW.DONE                     # phase 1 finished its turn
    assert states[1] == SW.STOPPED and states[2] == SW.STOPPED
    assert st["agents"][1]["error"] == SW.BUDGET_STOPPED_ERROR
    assert st["budget_reached"] == "tokens"
    # Said in the report, with how many are left for next time.
    assert "2 tasks left for next time" in SW.format_result(rid)
    # Persisted on disk with the run.
    row = SW.get(rid).row(with_events=True)
    assert row["budget"]["tokens"] == 100
    assert row["budget_reached"] == "tokens"


def test_resume_respects_the_remaining_budget():
    spent = {"tokens": 0}

    def run_turn(sid, prompt):
        yield {"type": "message", "text": "ok"}
        yield {"type": "done"}
        spent["tokens"] += 100

    rid = SW.start("goal", ".", "opencode", _spawn, run_turn, phases=CHAIN,
                   review=False, budget={"tokens": 100},
                   spent=lambda r: {"tokens": spent["tokens"], "calls": 0})
    st = _wait(rid)
    assert [a["state"] for a in st["agents"]] == [SW.DONE, SW.STOPPED, SW.STOPPED]

    # Resuming with the SAME (already spent) budget does no more work.
    rid2 = SW.resume(rid, _spawn, run_turn,
                     spent=lambda r: {"tokens": spent["tokens"], "calls": 0})
    assert rid2 == rid
    st = _wait(rid)
    done = sum(1 for a in st["agents"] if a["state"] == SW.DONE)
    assert done == 1                                 # still just phase 1

    # Resuming with a bigger budget lets the rest finish.
    rid3 = SW.resume(rid, _spawn, run_turn, budget={"tokens": 10000},
                     spent=lambda r: {"tokens": spent["tokens"], "calls": 0})
    assert rid3 == rid
    st = _wait(rid)
    assert all(a["state"] == SW.DONE for a in st["agents"])


def test_no_budget_runs_exactly_as_before():
    def run_turn(sid, prompt):
        yield {"type": "message", "text": "done"}
        yield {"type": "done"}

    rid = SW.start("goal", ".", "opencode", _spawn, run_turn, phases=CHAIN,
                   review=False)
    st = _wait(rid)
    assert st["state"] == SW.DONE and st["done"] == 3
    assert st["budget"] is None


# --------------------------------------------------------------------------- #
# app: the spend ledger, the conversation cap, the routes
# --------------------------------------------------------------------------- #
def test_run_spent_sums_worker_sessions(monkeypatch):
    import app
    with app._session_spend_lock:
        app._SESSION_SPEND.clear()
    app._note_session_spend("ws1", 100, 50)
    app._note_session_spend("ws2", 10, 0)
    monkeypatch.setattr(app.swarm_windows, "get", lambda rid: object())
    monkeypatch.setattr(app.swarm_windows, "worker_session_ids",
                        lambda run: ["ws1", "ws2"])
    assert app._run_spent("x") == {"tokens": 160, "calls": 2}
    # A terminal CLI (no build session) files nothing.
    app._note_session_spend(None, 999, 999)
    assert app._session_spend("nope") == {"tokens": 0, "calls": 0}


def test_conversation_cap_blocks_a_turn_cleanly(monkeypatch):
    import app
    with app._session_spend_lock:
        app._SESSION_SPEND.clear()
    monkeypatch.setattr(app.agentic_history, "budget",
                        lambda sid: {"tokens": 1000})
    app._note_session_spend("conv1", 1200, 0)       # over the 1000 cap
    b = app._conversation_budget("conv1")
    assert b["reached"] is True and "Budget reached" in b["message"]
    blocked = app._conversation_budget_block("conv1")
    assert blocked is not None and blocked[0] == 429
    # Under the cap: not blocked.
    with app._session_spend_lock:
        app._SESSION_SPEND.clear()
    app._note_session_spend("conv1", 10, 0)
    assert app._conversation_budget_block("conv1") is None


def test_multi_budget_kwargs_uses_conversation_then_default(monkeypatch):
    import app
    monkeypatch.setattr(app.agentic_history, "budget",
                        lambda sid: {"tokens": 5} if sid == "has-cap" else None)
    monkeypatch.setattr(app.config, "get_json",
                        lambda name, default=None:
                        {"calls": 9} if name == "multi_default_budget" else default)
    kw = app._multi_budget_kwargs("has-cap")
    assert kw["budget"] == {"tokens": 5, "seconds": None, "calls": None}
    assert "spent" not in kw                          # the accountant is a module hook
    kw2 = app._multi_budget_kwargs(None)              # falls back to the default
    assert kw2["budget"]["calls"] == 9
    # No cap anywhere -> no kwargs at all, so a run starts exactly as before.
    monkeypatch.setattr(app.config, "get_json", lambda name, default=None: default)
    assert app._multi_budget_kwargs(None) == {}


def test_spent_counter_hook_measures_a_run_without_an_injected_fn(monkeypatch):
    import app
    with app._session_spend_lock:
        app._SESSION_SPEND.clear()
    app._note_session_spend("hook1", 300, 0)
    monkeypatch.setattr(app.swarm_windows, "get", lambda rid: object())
    monkeypatch.setattr(app.swarm_windows, "worker_session_ids", lambda run: ["hook1"])
    app.swarm_windows.set_spent_counter(app._run_spent)
    run = SW._Run("g", ".", "c", [{"title": "a", "task": "t", "needs": []}],
                  budget={"tokens": 100})           # no spent= injected
    assert run.budget_spent()["tokens"] == 300        # measured via the module hook
    assert run.budget_status()[0] is True


def _client():
    import app
    c = app.app.test_client()
    hdr = {"X-Free-LLM-Hub": "dashboard"}
    token = app.config.get_control_token()
    if token:
        hdr["X-Free-LLM-Hub-Token"] = token
    c._hdr = hdr
    return c


def test_heartbeat_routes_crud():
    import app
    app.config.set_setting("heartbeats", [])
    app.config.set_setting("heartbeat_state", {})
    c = _client()
    # add
    r = c.post("/api/heartbeats", json={"when": "every 30 min", "goal_id": "g"},
               headers=c._hdr)
    assert r.status_code == 200
    sid = r.get_json()["added"]["id"]
    # a bad spec is refused
    assert c.post("/api/heartbeats", json={"when": "nope"}, headers=c._hdr).status_code == 400
    # list
    g = c.get("/api/heartbeats", headers=c._hdr)
    assert g.status_code == 200
    assert any(s["id"] == sid for s in g.get_json()["schedules"])
    # kill switch
    c.post("/api/heartbeats", json={"enabled": True}, headers=c._hdr)
    assert c.get("/api/heartbeats", headers=c._hdr).get_json()["enabled"] is True
    # update
    u = c.put("/api/heartbeats/" + sid, json={"max_tasks": 2}, headers=c._hdr)
    assert u.status_code == 200 and u.get_json()["updated"]["max_tasks"] == 2
    # delete
    assert c.delete("/api/heartbeats/" + sid, headers=c._hdr).status_code == 200
    assert c.delete("/api/heartbeats/" + sid, headers=c._hdr).status_code == 404
    app.config.set_setting("heartbeats", [])
    app.config.set_flag("heartbeats_enabled", False)


def test_budgets_route_lists_runs(monkeypatch):
    import app
    monkeypatch.setattr(app.swarm_windows, "list_runs",
                        lambda: [{"run_id": "r1", "state": "running",
                                  "project_dir": "/p", "owner": "s1"}])
    fake = SW._Run("g", "/p", "cli", [{"title": "a", "task": "t", "needs": []}],
                   budget={"tokens": 1000}, spent=lambda r: {"tokens": 400, "calls": 2})
    monkeypatch.setattr(app.swarm_windows, "get", lambda rid: fake)
    c = _client()
    j = c.get("/api/budgets", headers=c._hdr).get_json()
    assert j["runs"] and j["runs"][0]["run_id"] == "r1"
    assert j["runs"][0]["budget"]["line"].startswith("Budget:")
