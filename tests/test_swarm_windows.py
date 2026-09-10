r"""Several REAL agent sessions in parallel, in the background.

REQUESTED 2026-09-09: "a new mode in CLI's called multi swarm windows ... he can
orchestrate swarm agents, each agent in his proper session terminal and the one
we talk with in the terminal manages them ... can also use different best models
for the task ... our conversation should have access to all their context
windows ... let them dispatch the todolist in phases and each phase should wait
for the other if depends on it", then: "je dois pas ouvrir les fenetres terminal
pour qu'ils soient visibles mais en background".

WHY THIS IS NOT swarm.py. That one fans a single prompt across several MODELS
and picks a winner -- every stage is one chat request and nothing it does
touches a file. Here each worker is a real agent SESSION: its own CLI process,
its own project directory, its own context window, its own tools.

WHY EACH WORKER GETS ITS OWN SESSION. The context window is the scarce
resource. One agent doing five phases pays for every earlier phase's transcript
on every later one; five agents doing one phase each pay only for their own. The
orchestrator still sees everything because it reads SUMMARIES, not transcripts
-- which is what the research into Hermes' kanban swarm and Claude Code's
subagents both landed on independently, and what OpenAI-Swarm-style shared-history
handoffs get wrong (cost grows quadratically).

NOTHING HERE SPAWNS A PROCESS IN A TEST. `spawn` and `run_turn` are injected --
app.py passes agentic_chat.start_session and send_message_stream_durable, these
tests pass fakes. That is also why this module imports neither app nor
agentic_chat: the same cycle-avoidance the rest of the codebase uses.
"""
import os
import threading
import time

import pytest

import swarm_windows as SW


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    # Runs are written to disk now, so every test gets its own directory --
    # otherwise the suite would file two hundred swarm runs into the user's own
    # hub state and then evict the real ones past MAX_RUNS.
    monkeypatch.setenv(SW._STORE_ENV, str(tmp_path / "runs"))
    SW._RUNS.clear()
    yield
    for run in list(SW._RUNS.values()):
        run.stop_flag.set()
    SW._RUNS.clear()


def _wait(run_id, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        st = SW.status(run_id)
        if st and st["state"] in (SW.DONE, SW.FAILED, SW.STOPPED):
            return st
        time.sleep(0.02)
    return SW.status(run_id)


def _spawn(cli, project):
    return "sess-%s-%s" % (cli, uuid_counter())


_counter = [0]


def uuid_counter():
    _counter[0] += 1
    return _counter[0]


def _turn(text):
    """A fake CLI turn: the normalized event shape agentic_chat emits."""
    def run_turn(session_id, prompt):
        yield {"type": "output", "text": "working"}
        yield {"type": "message", "text": text}
        yield {"type": "done"}
    return run_turn


PHASES = [
    {"title": "Schema", "task": "design the schema", "needs": []},
    {"title": "API", "task": "build the api", "needs": [1]},
    {"title": "Docs", "task": "write the docs", "needs": [1]},
]


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #

def test_a_plan_becomes_phases():
    planner = lambda sysmsg, goal: (
        '{"goal":"g","phases":[{"title":"A","task":"do a","needs":[]},'
        '{"title":"B","task":"do b","needs":[1]}]}')
    phases = SW.plan("build a thing", planner)
    assert [p["title"] for p in phases] == ["A", "B"]
    assert phases[1]["needs"] == [1]


def test_a_fenced_plan_is_still_read():
    """Models fence JSON, prefix it with prose, or both."""
    planner = lambda s, g: 'Sure!\n```json\n{"phases":[{"title":"A","task":"x"}]}\n```'
    assert len(SW.plan("g", planner)) == 1


def test_a_planner_that_throws_is_not_fatal():
    def boom(s, g):
        raise RuntimeError("no model")
    assert SW.plan("g", boom) == []


def test_unusable_output_yields_no_phases():
    assert SW.plan("g", lambda s, g: "I could not do that") == []


@pytest.mark.parametrize("needs,expect", [
    ([2], []),          # forward reference
    ([1], []),          # self reference (phase 1)
    (["x"], []),        # not a number
    ([99], []),         # does not exist
])
def test_a_bad_dependency_is_dropped_not_obeyed(needs, expect):
    """A self- or forward-reference would deadlock the wave scheduler."""
    phases = SW.clean_phases({"phases": [{"title": "A", "task": "t", "needs": needs}]})
    assert phases[0]["needs"] == expect


def test_a_phase_with_no_task_is_dropped():
    phases = SW.clean_phases({"phases": [{"title": "A", "task": "  "},
                                         {"title": "B", "task": "real"}]})
    assert [p["title"] for p in phases] == ["B"]


def test_the_phase_count_is_capped():
    many = {"phases": [{"title": str(i), "task": "t"} for i in range(50)]}
    assert len(SW.clean_phases(many)) <= SW.MAX_AGENTS


# --------------------------------------------------------------------------- #
# Waves
# --------------------------------------------------------------------------- #

def test_independent_phases_share_a_wave():
    """"each phase should wait for the other if depends on it" -- and only
    then."""
    assert SW.waves(SW.clean_phases({"phases": PHASES})) == [[1], [2, 3]]


def test_a_chain_is_one_phase_per_wave():
    chain = [{"title": "a", "task": "t", "needs": []},
             {"title": "b", "task": "t", "needs": [1]},
             {"title": "c", "task": "t", "needs": [2]}]
    assert SW.waves(SW.clean_phases({"phases": chain})) == [[1], [2], [3]]


def test_everything_independent_is_one_wave():
    flat = [{"title": str(i), "task": "t", "needs": []} for i in range(4)]
    assert SW.waves(SW.clean_phases({"phases": flat})) == [[1, 2, 3, 4]]


def test_an_unsatisfiable_graph_runs_rather_than_hangs():
    """A swarm that deadlocks is worse than one whose last phases get less
    context. Built by hand because clean_phases would have sanitised it."""
    bad = [{"title": "a", "task": "t", "needs": [2]},
           {"title": "b", "task": "t", "needs": [1]}]
    assert SW.waves(bad) == [[1, 2]]


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #

def test_a_run_finishes_and_every_phase_reports():
    # review=False here and below: these cover the RUN mechanics, and the extra
    # reviewer would only make every count in them one larger. The review phase
    # has its own tests at the bottom of this file.
    rid = SW.start("goal", ".", "opencode", _spawn, _turn("done it"),
                   phases=PHASES, review=False)
    st = _wait(rid)
    assert st["state"] == SW.DONE
    assert st["done"] == 3 and st["failed"] == 0
    assert all(a["summary"] == "done it" for a in st["agents"])


def test_each_agent_gets_its_own_session():
    """The whole point: its own context window."""
    rid = SW.start("goal", ".", "opencode", _spawn, _turn("ok"), phases=PHASES)
    st = _wait(rid)
    sids = [a["session_id"] for a in st["agents"]]
    assert len(set(sids)) == len(sids)
    assert all(sids)


def test_a_dependent_phase_sees_its_parents_summary():
    seen = {}

    def run_turn(session_id, prompt):
        seen[session_id] = prompt
        yield {"type": "message", "text": "SUMMARY-" + session_id}
        yield {"type": "done"}

    rid = SW.start("goal", ".", "opencode", _spawn, run_turn, phases=PHASES)
    _wait(rid)
    st = SW.status(rid)
    parent_sid = st["agents"][0]["session_id"]
    child_prompt = seen[st["agents"][1]["session_id"]]
    assert "SUMMARY-" + parent_sid in child_prompt


def test_a_worker_is_never_handed_a_parents_transcript():
    """Summaries, not transcripts -- the difference between linear and
    quadratic cost, and the reason the orchestrator can read everything."""
    def run_turn(session_id, prompt):
        yield {"type": "output", "text": "NOISE-" + session_id}
        yield {"type": "message", "text": "clean summary"}
        yield {"type": "done"}

    seen = {}

    def spying(session_id, prompt):
        seen[session_id] = prompt
        return run_turn(session_id, prompt)

    rid = SW.start("goal", ".", "opencode", _spawn, spying, phases=PHASES)
    _wait(rid)
    child = SW.status(rid)["agents"][1]["session_id"]
    assert "NOISE-" not in seen[child]
    assert "clean summary" in seen[child]


def test_the_goal_reaches_every_worker():
    seen = []

    def run_turn(session_id, prompt):
        seen.append(prompt)
        yield {"type": "message", "text": "x"}

    rid = SW.start("BUILD THE THING", ".", "opencode", _spawn, run_turn, phases=PHASES)
    _wait(rid)
    assert all("BUILD THE THING" in p for p in seen)


# --------------------------------------------------------------------------- #
# Failure is expected, not exceptional
# --------------------------------------------------------------------------- #

def test_one_dead_worker_does_not_take_the_wave():
    def run_turn(session_id, prompt):
        if "api" in prompt.lower():
            raise RuntimeError("that CLI died")
        yield {"type": "message", "text": "fine"}

    rid = SW.start("goal", ".", "opencode", _spawn, run_turn, phases=PHASES)
    st = _wait(rid)
    states = {a["index"]: a["state"] for a in st["agents"]}
    assert states[2] == SW.FAILED
    assert states[1] == SW.DONE and states[3] == SW.DONE
    assert st["state"] == SW.DONE, "a run with one failed phase is not a failed run"


def test_a_worker_that_produces_nothing_is_a_failure_not_a_success():
    def run_turn(session_id, prompt):
        yield {"type": "done"}

    rid = SW.start("goal", ".", "opencode", _spawn, run_turn,
                   phases=[{"title": "a", "task": "t", "needs": []}])
    st = _wait(rid)
    assert st["agents"][0]["state"] == SW.FAILED
    assert "no result" in (st["agents"][0]["error"] or "")


def test_a_child_whose_parent_failed_still_runs():
    """It simply gets less context, and is told so."""
    seen = {}

    def run_turn(session_id, prompt):
        seen[session_id] = prompt
        # Keyed on the TASK text, which is the worker's own instruction, rather
        # than on the prompt's framing -- the framing moved once already when
        # the first live run showed workers answering it instead of the task.
        if prompt.startswith("design the schema"):
            raise RuntimeError("dead")
        yield {"event": "message", "text": "ok"}

    rid = SW.start("goal", ".", "opencode", _spawn, run_turn, phases=PHASES)
    st = _wait(rid)
    assert st["agents"][1]["state"] == SW.DONE
    assert "did not produce a result" in seen[st["agents"][1]["session_id"]]


def test_every_phase_failing_is_a_failed_run():
    def run_turn(session_id, prompt):
        raise RuntimeError("nope")

    rid = SW.start("goal", ".", "opencode", _spawn, run_turn, phases=PHASES)
    st = _wait(rid)
    assert st["state"] == SW.FAILED and st["error"] == "every phase failed"


def test_a_spawn_that_fails_is_recorded_not_raised():
    def bad_spawn(cli, project):
        raise RuntimeError("no such CLI")

    rid = SW.start("goal", ".", "nope", bad_spawn, _turn("x"), phases=PHASES)
    st = _wait(rid)
    assert all(a["state"] == SW.FAILED for a in st["agents"])


def test_an_error_event_is_kept_as_the_reason():
    def run_turn(session_id, prompt):
        yield {"type": "error", "error": "rate limited"}

    rid = SW.start("g", ".", "opencode", _spawn, run_turn,
                   phases=[{"title": "a", "task": "t", "needs": []}])
    st = _wait(rid)
    assert st["agents"][0]["error"] == "rate limited"


def test_a_garbage_event_does_not_crash_a_worker():
    def run_turn(session_id, prompt):
        yield "not a dict"
        yield {"type": "message", "text": "still fine"}

    rid = SW.start("g", ".", "opencode", _spawn, run_turn,
                   phases=[{"title": "a", "task": "t", "needs": []}])
    assert _wait(rid)["agents"][0]["state"] == SW.DONE


# --------------------------------------------------------------------------- #
# Bounds
# --------------------------------------------------------------------------- #

def test_no_more_than_the_cap_run_at_once():
    """Each worker is a real CLI process with a model behind it -- the user's
    own words were "it will consume the ram more"."""
    live, peak, lock = [0], [0], threading.Lock()

    def run_turn(session_id, prompt):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.05)
        with lock:
            live[0] -= 1
        yield {"type": "message", "text": "ok"}

    flat = [{"title": str(i), "task": "t", "needs": []} for i in range(8)]
    rid = SW.start("g", ".", "opencode", _spawn, run_turn, phases=flat,
                   review=False)
    st = _wait(rid, timeout=20)
    assert peak[0] <= SW.MAX_CONCURRENT, "peak concurrency was %d" % peak[0]
    assert st["done"] == 8, "everything past the cap must still run"


def test_a_run_can_be_stopped():
    started = threading.Event()

    def run_turn(session_id, prompt):
        started.set()
        time.sleep(5)
        yield {"type": "message", "text": "too late"}

    rid = SW.start("g", ".", "opencode", _spawn, run_turn, phases=PHASES)
    started.wait(5)
    assert SW.stop(rid) is True
    st = SW.status(rid)
    assert st["state"] == SW.STOPPED


def test_stopping_something_that_does_not_exist_is_false():
    assert SW.stop("no-such-run") is False


def test_old_runs_are_not_kept_forever():
    for _ in range(SW.MAX_RUNS + 6):
        rid = SW.start("g", ".", "opencode", _spawn, _turn("x"),
                       phases=[{"title": "a", "task": "t", "needs": []}])
        _wait(rid, timeout=5)
    assert len(SW._RUNS) <= SW.MAX_RUNS + 1


# --------------------------------------------------------------------------- #
# What the orchestrator reads
# --------------------------------------------------------------------------- #

def test_the_orchestrator_can_read_every_agents_log():
    """"our conversation should have access to all their context windows"."""
    rid = SW.start("g", ".", "opencode", _spawn, _turn("summary"), phases=PHASES)
    _wait(rid)
    st = SW.status(rid, with_events=True)
    assert all(a["log"] for a in st["agents"])
    assert any(e.get("text") == "working" for e in st["agents"][0]["log"])


def test_the_result_is_summaries_not_transcripts():
    """This is what the parent conversation pastes into its own context."""
    rid = SW.start("g", ".", "opencode", _spawn, _turn("the summary"),
                   phases=PHASES, review=False)
    _wait(rid)
    res = SW.result(rid)
    assert [p["summary"] for p in res["phases"]] == ["the summary"] * 3
    assert "log" not in res["phases"][0]


def test_the_result_reads_back_as_text():
    rid = SW.start("build it", ".", "opencode", _spawn, _turn("did the thing"),
                   phases=PHASES)
    _wait(rid)
    text = SW.format_result(rid)
    assert "build it" in text and "did the thing" in text and "Phase 1" in text


def test_runs_are_listable():
    rid = SW.start("g", ".", "opencode", _spawn, _turn("x"), phases=PHASES)
    _wait(rid)
    assert any(r["run_id"] == rid for r in SW.list_runs())


def test_an_unknown_run_is_none_not_an_error():
    assert SW.status("nope") is None and SW.result("nope") is None


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #

def test_a_run_needs_a_goal():
    with pytest.raises(SW.SwarmWindowsError):
        SW.start("  ", ".", "opencode", _spawn, _turn("x"), phases=PHASES)


def test_a_run_needs_phases_or_a_planner():
    with pytest.raises(SW.SwarmWindowsError):
        SW.start("g", ".", "opencode", _spawn, _turn("x"))


def test_an_unplannable_goal_is_refused_clearly():
    with pytest.raises(SW.SwarmWindowsError):
        SW.start("g", ".", "opencode", _spawn, _turn("x"),
                 planner=lambda s, g: "no json here")


def test_this_module_stays_free_of_the_app_import_cycle():
    """spawn/run_turn/planner are injected precisely so this can be tested
    without spawning a process -- and so model_categories-style leaf status is
    preserved."""
    src = open("swarm_windows.py", encoding="utf-8").read()
    assert "import app" not in src
    assert "import agentic_chat" not in src


# --------------------------------------------------------------------------- #
# Wiring: routes and the MCP surface
# --------------------------------------------------------------------------- #

def test_the_hub_exposes_it_over_http():
    import app as A
    paths = {str(r) for r in A.app.url_map.iter_rules()}
    assert "/api/swarm-windows" in paths
    assert "/api/swarm-windows/<run_id>" in paths


def test_a_run_needs_a_real_project_folder():
    """These workers write files. Inventing a folder for them is not this
    endpoint's call."""
    import app as A
    import config
    A.app.config["TESTING"] = True
    c = A.app.test_client()
    h = {"X-Free-LLM-Hub-Token": config.get_setting("control_token") or "",
         "X-Free-LLM-Hub": "dashboard"}
    r = c.post("/api/swarm-windows", headers=h,
               json={"goal": "g", "project_dir": "/no/such/folder/anywhere"})
    assert r.status_code == 400
    assert "existing folder" in r.get_json()["error"]["message"]


def test_a_run_over_http_needs_a_goal(tmp_path):
    import app as A
    import config
    A.app.config["TESTING"] = True
    c = A.app.test_client()
    h = {"X-Free-LLM-Hub-Token": config.get_setting("control_token") or "",
         "X-Free-LLM-Hub": "dashboard"}
    r = c.post("/api/swarm-windows", headers=h,
               json={"goal": "  ", "project_dir": str(tmp_path)})
    assert r.status_code == 400


def test_an_unknown_run_is_a_404():
    import app as A
    import config
    A.app.config["TESTING"] = True
    c = A.app.test_client()
    h = {"X-Free-LLM-Hub-Token": config.get_setting("control_token") or ""}
    assert c.get("/api/swarm-windows/nope", headers=h).status_code == 404


def test_every_cli_can_drive_it_through_mcp():
    """MCP is the surface opencode, codex, claude and the rest all speak, so
    one wiring reaches every one of them instead of a per-CLI integration."""
    import app  # noqa: F401  (wires hub_mcp on import)
    import hub_mcp
    names = [t["name"] for t in hub_mcp._tools()]
    for tool in ("swarm_windows_start", "swarm_windows_status", "swarm_windows_stop"):
        assert tool in names


def test_the_mcp_tools_are_hidden_when_not_wired(monkeypatch):
    """A client must never be shown a tool that cannot run."""
    import hub_mcp
    monkeypatch.setattr(hub_mcp, "_SWARM", None)
    assert not [t for t in hub_mcp._tools() if t["name"].startswith("swarm_windows")]


def test_a_bad_argument_is_a_protocol_error_a_bad_world_is_tool_text(monkeypatch):
    """The model can act on "that folder does not exist"; it cannot act on a
    JSON-RPC error."""
    import hub_mcp
    monkeypatch.setattr(hub_mcp, "_SWARM", {
        "start": lambda *a: (_ for _ in ()).throw(RuntimeError("no such folder")),
        "status": lambda r, e=False: None,
        "stop": lambda r: False,
    })
    bad_arg = hub_mcp._call_tool({"name": "swarm_windows_start",
                                  "arguments": {"project_dir": "."}})
    assert bad_arg.get("code") == -32602

    bad_world = hub_mcp._call_tool({"name": "swarm_windows_start",
                                    "arguments": {"goal": "g", "project_dir": "."}})
    assert bad_world.get("isError") or "could not start" in str(bad_world)


# --------------------------------------------------------------------------- #
# The event shape the CLI actually emits
# --------------------------------------------------------------------------- #

def test_the_real_event_key_is_read():
    """MEASURED on the first live run. agentic_chat emits {"event": "message",
    "text": ...}; this module was reading ev["type"], the OpenAI streaming
    spelling. Every event fell through, so every worker looked like it had
    produced nothing -- two agents that had answered perfectly well were both
    recorded as failed."""
    rid = SW.start("g", ".", "opencode",
                   _spawn, lambda sid, p: iter([{"event": "message", "text": "real"}]),
                   phases=[{"title": "a", "task": "t", "needs": []}])
    st = _wait(rid)
    assert st["agents"][0]["state"] == SW.DONE
    assert st["agents"][0]["summary"] == "real"


def test_the_openai_spelling_still_works_too():
    rid = SW.start("g", ".", "opencode",
                   _spawn, lambda sid, p: iter([{"type": "message", "text": "also real"}]),
                   phases=[{"title": "a", "task": "t", "needs": []}])
    assert _wait(rid)["agents"][0]["summary"] == "also real"


def test_a_done_event_carrying_the_text_counts():
    """The live stream's terminal event repeats the final message."""
    rid = SW.start("g", ".", "opencode",
                   _spawn, lambda sid, p: iter([{"event": "done", "text": "final"}]),
                   phases=[{"title": "a", "task": "t", "needs": []}])
    assert _wait(rid)["agents"][0]["summary"] == "final"


def test_the_task_comes_first_in_the_prompt():
    """Both workers on the first live run replied "What's the shared goal? I
    need the task before I can start" -- they had been handed the task and
    answered the FRAMING instead. The codebase already records this exact
    failure for codex: "leading with the notice made the agent answer the
    notice instead of the user"."""
    seen = {}

    def run_turn(session_id, prompt):
        seen[session_id] = prompt
        yield {"event": "message", "text": "ok"}

    rid = SW.start("the overall goal", ".", "opencode", _spawn, run_turn,
                   phases=[{"title": "T", "task": "WRITE THE FILE", "needs": []}])
    _wait(rid)
    prompt = list(seen.values())[0]
    assert prompt.startswith("WRITE THE FILE")
    assert prompt.index("WRITE THE FILE") < prompt.index("the overall goal")


def test_the_worker_is_told_there_is_nobody_to_ask():
    seen = {}

    def run_turn(session_id, prompt):
        seen[session_id] = prompt
        yield {"event": "message", "text": "ok"}

    rid = SW.start("g", ".", "opencode", _spawn, run_turn,
                   phases=[{"title": "a", "task": "t", "needs": []}])
    _wait(rid)
    assert "nobody to ask" in list(seen.values())[0]


# --------------------------------------------------------------------------- #
# A different kind of model per phase
# --------------------------------------------------------------------------- #

def test_a_phase_can_name_the_kind_of_model_it_needs():
    """"can also use different best models for the task"."""
    ph = SW.clean_phases({"phases": [{"title": "a", "task": "t", "mode": "coding"}]},
                         modes=("coding", "vision"))
    assert ph[0]["mode"] == "coding"


def test_an_invented_mode_is_dropped():
    """A planner answering "mode": "genius" would otherwise reach
    set_session_mode and either fail or silently restrict a phase to nothing."""
    ph = SW.clean_phases({"phases": [{"title": "a", "task": "t", "mode": "genius"}]},
                         modes=("coding", "vision"))
    assert ph[0]["mode"] is None


def test_a_phase_may_name_no_mode_at_all():
    ph = SW.clean_phases({"phases": [{"title": "a", "task": "t"}]}, modes=("coding",))
    assert ph[0]["mode"] is None


def test_the_planner_is_told_which_modes_exist():
    seen = {}

    def planner(sysmsg, goal):
        seen["sys"] = sysmsg
        return '{"phases":[{"title":"A","task":"x"}]}'

    SW.plan("g", planner, modes=("coding", "uncensored"))
    assert "coding, uncensored" in seen["sys"]


def test_the_mode_is_applied_to_that_agents_session():
    applied = {}
    rid = SW.start("g", ".", "opencode", _spawn, _turn("ok"),
                   phases=[{"title": "a", "task": "t", "mode": "coding"}],
                   configure=lambda sid, mode: applied.update(sid=sid, mode=mode),
                   modes=("coding",))
    st = _wait(rid)
    assert applied.get("mode") == "coding"
    assert applied.get("sid") == st["agents"][0]["session_id"]


def test_a_phase_with_no_mode_configures_nothing():
    calls = []
    rid = SW.start("g", ".", "opencode", _spawn, _turn("ok"),
                   phases=[{"title": "a", "task": "t"}],
                   configure=lambda sid, mode: calls.append(mode))
    _wait(rid)
    assert calls == []


def test_a_session_that_refuses_a_mode_still_does_its_work():
    def boom(sid, mode):
        raise RuntimeError("no such session")
    rid = SW.start("g", ".", "opencode", _spawn, _turn("ok"),
                   phases=[{"title": "a", "task": "t", "mode": "coding"}],
                   configure=boom, modes=("coding",))
    assert _wait(rid)["agents"][0]["state"] == SW.DONE


def test_the_mode_is_reported():
    rid = SW.start("g", ".", "opencode", _spawn, _turn("ok"),
                   phases=[{"title": "a", "task": "t", "mode": "coding"}],
                   modes=("coding",))
    st = _wait(rid)
    assert st["agents"][0]["mode"] == "coding"
    assert SW.result(rid)["phases"][0]["mode"] == "coding"


def test_the_hub_passes_its_real_modes_and_a_configurer():
    src = open("app.py", encoding="utf-8").read()
    assert "configure=_swarm_windows_configure" in src
    assert "modes=_mode_keys()" in src
    body = src[src.index("def _swarm_windows_configure("):]
    body = body[:body.index("\ndef ")]
    assert "set_session_mode" in body


# --------------------------------------------------------------------------- #
# The agents work together, not just in parallel
# --------------------------------------------------------------------------- #

def test_a_review_phase_is_appended():
    """REQUESTED: "pour le swarm les models doivent travailler ensemble pour
    trouver la plus meilleure solution pertinente". Phases alone are division
    of labour -- five agents each doing their own piece and nobody ever looking
    at the whole. swarm.py already ends its chat pipeline with review and synth
    for exactly this reason."""
    ph = SW.with_review(SW.clean_phases({"phases": PHASES}))
    assert ph[-1]["title"] == SW.REVIEW_TITLE
    assert ph[-1]["needs"] == [1, 2, 3]


def test_the_review_runs_last_and_alone():
    ph = SW.with_review(SW.clean_phases({"phases": PHASES}))
    assert SW.waves(ph)[-1] == [len(ph)]


def test_it_sees_every_other_phase_s_result():
    """It depends on all of them, so _agent_prompt hands it all their
    summaries -- that is what makes it a review rather than a fourth worker."""
    seen = {}

    def run_turn(session_id, prompt):
        seen[session_id] = prompt
        yield {"event": "message", "text": "SUM-" + session_id}

    rid = SW.start("g", ".", "opencode", _spawn, run_turn, phases=PHASES)
    _wait(rid, timeout=20)
    st = SW.status(rid)
    reviewer = [a for a in st["agents"] if a["title"] == SW.REVIEW_TITLE][0]
    others = [a for a in st["agents"] if a["title"] != SW.REVIEW_TITLE]
    prompt = seen[reviewer["session_id"]]
    for a in others:
        assert "SUM-" + a["session_id"] in prompt


def test_it_is_told_to_fix_rather_than_report():
    ph = SW.with_review(SW.clean_phases({"phases": PHASES}))
    task = ph[-1]["task"].lower()
    assert "fix" in task
    assert "not to summarise" in task or "not a summar" in task


def test_a_single_phase_gets_no_reviewer():
    """Nothing to reconcile between one piece of work, and a second agent
    re-reading it is a whole extra model call to say "looks fine"."""
    one = SW.with_review(SW.clean_phases({"phases": [{"title": "a", "task": "x"}]}))
    assert len(one) == 1


def test_adding_it_twice_does_not_stack():
    ph = SW.with_review(SW.clean_phases({"phases": PHASES}))
    assert len(SW.with_review(ph)) == len(ph)


def test_it_can_be_turned_off():
    rid = SW.start("g", ".", "opencode", _spawn, _turn("ok"),
                   phases=PHASES, review=False)
    st = _wait(rid)
    assert not [a for a in st["agents"] if a["title"] == SW.REVIEW_TITLE]


def test_it_is_on_by_default():
    rid = SW.start("g", ".", "opencode", _spawn, _turn("ok"), phases=PHASES)
    st = _wait(rid, timeout=20)
    assert [a for a in st["agents"] if a["title"] == SW.REVIEW_TITLE]


# --------------------------------------------------------------------------- #
# A run outlives the process that started it
# --------------------------------------------------------------------------- #

def test_a_finished_run_is_on_disk():
    """REQUESTED: "memory management ... and also persistence ... nothing can
    escape". The hub restarts itself every five hours to git pull; before this,
    a swarm that finished at hour four was gone at hour five."""
    rid = SW.start("g", ".", "opencode", _spawn, _turn("the summary"),
                   phases=PHASES, review=False)
    _wait(rid)
    assert os.path.isfile(SW._run_path(rid))


def test_it_reads_back_after_a_restart():
    rid = SW.start("build the thing", ".", "opencode", _spawn, _turn("the summary"),
                   phases=PHASES, review=False)
    _wait(rid)
    SW._RUNS.clear()                      # the restart
    assert SW.status(rid) is None
    assert SW.load() == 1
    st = SW.status(rid)
    assert st["state"] == SW.DONE
    assert st["goal"] == "build the thing"
    assert [a["summary"] for a in st["agents"]] == ["the summary"] * 3


def test_the_result_still_reads_back():
    """What the orchestrator actually pastes into its own context."""
    rid = SW.start("g", ".", "opencode", _spawn, _turn("did it"), phases=PHASES,
                   review=False)
    _wait(rid)
    SW._RUNS.clear()
    SW.load()
    assert "did it" in SW.format_result(rid)


def test_a_restored_run_says_so():
    rid = SW.start("g", ".", "opencode", _spawn, _turn("x"), phases=PHASES,
                   review=False)
    _wait(rid)
    SW._RUNS.clear()
    SW.load()
    assert SW.status(rid)["restored"] is True


def test_a_worker_s_log_survives_too():
    rid = SW.start("g", ".", "opencode", _spawn, _turn("x"), phases=PHASES,
                   review=False)
    _wait(rid)
    SW._RUNS.clear()
    SW.load()
    st = SW.status(rid, with_events=True)
    assert any(e.get("text") == "working" for e in st["agents"][0]["log"])


def test_only_a_tail_of_the_log_is_written():
    """The ring holds 400 raw CLI events per agent; writing all of them to disk
    on every phase boundary is a build transcript per swarm."""
    def chatty(session_id, prompt):
        for i in range(SW.PERSIST_EVENTS + 40):
            yield {"event": "message", "text": "line %d" % i}

    rid = SW.start("g", ".", "opencode", _spawn, chatty,
                   phases=[{"title": "a", "task": "t", "needs": []}], review=False)
    _wait(rid)
    SW._RUNS.clear()
    SW.load()
    log = SW.status(rid, with_events=True)["agents"][0]["log"]
    assert len(log) <= SW.PERSIST_EVENTS


def test_a_run_interrupted_by_the_restart_is_not_still_running():
    """Nothing is walking it any more. Left alone it would display as live for
    the rest of the hub's life."""
    row = {"run_id": "swarm-abc123", "goal": "g", "state": SW.RUNNING,
           "project_dir": ".", "cli": "opencode", "created_at": time.time(),
           "agents": [{"index": 1, "title": "a", "task": "t", "needs": [],
                       "state": SW.RUNNING}]}
    run = SW._Run.from_row(row)
    assert run.state == SW.FAILED
    assert run.agents[0].state == SW.FAILED
    assert "restart" in run.agents[0].error


def test_a_live_run_outranks_its_own_file():
    """load() runs at startup, but a run started since must never be clobbered
    by the snapshot of it written a moment earlier."""
    rid = SW.start("g", ".", "opencode", _spawn, _turn("live"), phases=PHASES,
                   review=False)
    _wait(rid)
    SW.load()
    assert SW.status(rid)["restored"] is False


def test_evicting_a_run_removes_its_file_too():
    """Or the directory becomes the unbounded thing MAX_RUNS exists to stop."""
    ids = []
    for _ in range(SW.MAX_RUNS + 6):
        rid = SW.start("g", ".", "opencode", _spawn, _turn("x"),
                       phases=[{"title": "a", "task": "t", "needs": []}],
                       review=False)
        _wait(rid, timeout=5)
        ids.append(rid)
    files = [n for n in os.listdir(SW._store_root()) if n.endswith(".json")]
    assert len(files) <= SW.MAX_RUNS + 1


def test_a_corrupt_run_file_is_skipped_not_fatal():
    rid = SW.start("g", ".", "opencode", _spawn, _turn("good"), phases=PHASES,
                   review=False)
    _wait(rid)
    with open(os.path.join(SW._store_root(), "swarm-broken.json"), "w",
              encoding="utf-8") as fh:
        fh.write("{ not json")
    SW._RUNS.clear()
    assert SW.load() == 1
    assert SW.status(rid) is not None


def test_loading_from_nothing_is_not_an_error():
    assert SW.load() == 0


def test_a_run_that_cannot_be_written_still_runs(monkeypatch, tmp_path):
    """Best-effort, like every other memory in this hub: a swarm never fails
    because the record of it could not be written. A file where the directory
    should be: makedirs cannot win."""
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    monkeypatch.setenv(SW._STORE_ENV, str(blocker / "inside"))
    rid = SW.start("g", ".", "opencode", _spawn, _turn("still works"),
                   phases=PHASES, review=False)
    st = _wait(rid)
    assert st["state"] == SW.DONE


def test_a_run_id_can_never_name_a_file_outside_the_store():
    assert SW._run_path("../../etc/passwd") is None
    assert SW._run_path("") is None


# --------------------------------------------------------------------------- #
# Every phase gets the model its mode asked for -- including past the cap
# --------------------------------------------------------------------------- #

def test_the_fifth_phase_is_configured_too():
    """MAX_CONCURRENT is 4, so phases 5+ start from the queue -- and the queue
    was starting them without `configure`, which is how the fifth phase, and
    only ever the fifth, ran under the default model instead of its own."""
    seen = {}
    flat = [{"title": str(i), "task": "t", "needs": [], "mode": "coding"}
            for i in range(SW.MAX_CONCURRENT + 2)]
    rid = SW.start("g", ".", "opencode", _spawn, _turn("ok"), phases=flat,
                   review=False,
                   configure=lambda sid, mode: seen.__setitem__(sid, mode))
    st = _wait(rid, timeout=20)
    assert st["done"] == len(flat)
    assert len(seen) == len(flat), "only %d of %d phases were configured" % (
        len(seen), len(flat))
