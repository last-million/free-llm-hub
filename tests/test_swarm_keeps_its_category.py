"""Swarm / crew / multi from a CLI: the right category, fast, clean answers.

REPORTED 2026-09-26, four defects behind "multi from a CLI is slow and noisy":

1. CATEGORY LOSS. "coding-swarm" sets g.model_mode = "coding"
   (_apply_category_effort), but swarm.run runs parallel waves on worker
   threads, where Flask's `g` does not exist -- _active_mode() fell back to the
   global mode and the coding swarm's workers were routed outside coding.
2. SPEED. MEASURED live: "coding-swarm" took 231 s for "What is 5767 plus 1".
3. TRAILERS. "**Plan followed** / **Models used** / **Reviewer raised**" and
   "**Crew:**" were appended to the deliverable a CLI writes into its files.
4. LABELS. "Multi sessions" from a CLI is the crew pipeline, not windows.

No network: every model call is a stub.
"""
import json
import threading
import time
from unittest import mock

import pytest
from flask import g

import app as A
import swarm


HARD = ("Build a full REST API with auth, tests and a React frontend, "
        "deployed with docker")
SIMPLE = "What is 5767 plus 1"

PLAN_3_PARALLEL = json.dumps({"goal": "G", "phases": [
    {"title": "Alpha", "task": "a", "needs": []},
    {"title": "Beta", "task": "b", "needs": []},
    {"title": "Gamma", "task": "c", "needs": []}]})


def _stage(msgs):
    system = msgs[0]["content"]
    user = msgs[-1]["content"]
    if system == swarm._PLAN_SYSTEM:
        return "plan"
    if "YOUR PHASE (" in user or "\nYOUR TASK: " in user:
        return "phase"
    if "WHAT THE TEAM PRODUCED" in user:
        return "supervise"
    if "PHASE OUTPUTS" in user:
        return "synth"
    if user.startswith("BRIEF\n"):
        return "review"
    return "other"


def _recording_dispatch(seen, plan=PLAN_3_PARALLEL):
    """A dispatch stub that records the mode each stage was routed under, and
    on which thread."""
    lock = threading.Lock()

    def _d(msgs, max_tokens, exclude_pids=()):
        with lock:
            seen.append((_stage(msgs), A._active_mode(),
                         threading.current_thread() is threading.main_thread()))
        st = _stage(msgs)
        if st == "plan":
            return plan, "p/planner"
        if st == "supervise":
            return '{"missing": []}', "p/sup"
        if st == "review":
            return '{"verdict": "ship", "problems": []}', "q/rev"
        if st == "synth":
            return "FINAL", "p/synth"
        return "part", "p/worker"
    return _d


@pytest.fixture
def global_all(monkeypatch):
    """The global mode is 'all' -- anything not carried falls back to it."""
    monkeypatch.setattr(A, "_global_mode", lambda: A.MODE_ALL)
    monkeypatch.setattr(A, "_build_sid", lambda: None)


# --------------------------------------------------------------------------- #
# 1. The category reaches every stage, on every thread
# --------------------------------------------------------------------------- #

def test_the_bug_unbound_parallel_workers_lose_the_category(global_all):
    """The failure, reproduced: the same pipeline with the RAW dispatch routes
    its worker-thread stages by the global mode."""
    seen = []
    with A.app.test_request_context():
        g.model_mode = "coding"
        swarm.run([{"role": "user", "content": HARD}], _recording_dispatch(seen))
    off_thread = [mode for st, mode, main in seen if not main]
    assert off_thread, "the wave must actually run on worker threads"
    assert set(off_thread) == {A.MODE_ALL}


def test_bound_dispatch_keeps_the_category_on_worker_threads(global_all):
    seen = []
    with A.app.test_request_context():
        g.model_mode = "coding"
        swarm.run([{"role": "user", "content": HARD}],
                  A._pipeline_bound(_recording_dispatch(seen)))
    assert any(not main for _st, _m, main in seen)
    assert {mode for _st, mode, _main in seen} == {"coding"}


def test_swarm_completion_carries_the_category_into_every_stage(global_all,
                                                                monkeypatch):
    """End to end through the handler a "coding-swarm" id lands in."""
    seen = []
    monkeypatch.setattr(A, "_swarm_dispatch", _recording_dispatch(seen))
    monkeypatch.setattr(A, "_act_pipeline_watcher", lambda: None)
    with A.app.test_request_context(json={}):
        g.model_mode = "coding"
        A._swarm_completion({"model": "swarm",
                             "messages": [{"role": "user", "content": HARD}]})
    assert {st for st, _m, _main in seen} >= {"plan", "phase", "review"}
    assert {mode for _st, mode, _main in seen} == {"coding"}


def test_crews_carry_the_category_too(global_all, monkeypatch):
    seen = []
    monkeypatch.setattr(A, "_swarm_dispatch", _recording_dispatch(seen))
    monkeypatch.setattr(A, "_act_pipeline_watcher", lambda: None)
    with A.app.test_request_context(json={}):
        g.model_mode = "coding"
        A._swarm_completion({"model": "crew-code",
                             "messages": [{"role": "user", "content": HARD}]})
    assert seen and {mode for _st, mode, _main in seen} == {"coding"}


def test_the_bound_worker_can_reach_the_activity_row(global_all):
    """The same missing `g` made _act_pick raise inside _swarm_dispatch AFTER a
    hop answered, and the stage's broad except threw the answer away."""
    out = {}
    with A.app.test_request_context():
        g.act = {"hops": []}

        def _worker():
            try:
                A._act_pick("p", "m")
                out["ok"] = True
            except RuntimeError as exc:
                out["err"] = exc
        t = threading.Thread(target=A._pipeline_bound(_worker))
        t.start()
        t.join()
        assert out.get("ok"), out.get("err")
        assert g.act["hops"] == ["p/m"]


def test_the_deadline_thread_inherits_the_mode(global_all, monkeypatch):
    """_dispatch_chat_with_deadline starts ANOTHER thread per hop, with an empty
    context of its own."""
    seen = []
    monkeypatch.setattr(A, "_dispatch_chat",
                        lambda pid, payload, stream: seen.append(A._active_mode()))
    A._pipeline_bound(lambda: A._dispatch_chat_with_deadline("p", {}, 5),
                      "coding")()
    assert seen == ["coding"]


def test_the_tool_fan_out_members_keep_the_category(global_all, monkeypatch):
    seen = []
    monkeypatch.setattr(A, "_route_by_difficulty",
                        lambda *a, **k: ("p1", "m1", "hard"))
    monkeypatch.setattr(A, "_build_chain",
                        lambda *a, **k: [("p1", "m1"), ("p2", "m2")])
    monkeypatch.setattr(A, "_swarm_rank", lambda cands, d=None: list(cands))
    monkeypatch.setattr(A, "_record_outcome", lambda *a, **k: None)

    def _fake(pid, payload, deadline=None):
        seen.append(A._active_mode())
        return None, RuntimeError("stub")
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _fake)
    with A.app.test_request_context(json={}):
        g.model_mode = "coding"
        A._swarm_tool_result({"model": "swarm", "tools": [{"type": "function"}],
                              "messages": [{"role": "user", "content": HARD}]})
    assert len(seen) == 2 and set(seen) == {"coding"}


def test_the_multi_session_planner_uses_the_conversation_category(global_all,
                                                                  monkeypatch):
    """It runs on the event feeder's thread: no request, no `g`."""
    seen = []
    monkeypatch.setattr(A, "_swarm_windows_planner",
                        lambda system, goal: seen.append(A._active_mode()) or "{}")
    planner = A._pipeline_bound(A._swarm_windows_planner,
                                A._session_mode_or_none({"mode": "coding"}))
    t = threading.Thread(target=planner, args=("sys", "goal"))
    t.start()
    t.join()
    assert seen == ["coding"]
    assert A._session_mode_or_none({}) is None
    assert A._session_mode_or_none({"mode": "nonsense"}) is None


def test_every_planner_call_site_is_bound():
    src = open("app.py", encoding="utf-8").read()
    assert "planner=_swarm_windows_planner," not in src
    assert src.count("planner=_pipeline_bound(_swarm_windows_planner") == 3


# --------------------------------------------------------------------------- #
# 2. Speed: a simple question takes one strong model; a run has a wall clock
# --------------------------------------------------------------------------- #

def _run_completion(body, headers=None, **patches):
    single = mock.Mock(side_effect=lambda b: (A.jsonify({"choices": [
        {"index": 0, "message": {"role": "assistant", "content": "5768"}}]}), 200))
    with A.app.test_request_context(json=body, headers=headers or {}), \
            mock.patch.object(A, "_chat_completions_uncached", single), \
            mock.patch.object(A, "_act_pipeline_watcher", lambda: None):
        for k, v in patches.items():
            mock.patch.object(A.swarm, k, v).start()
        try:
            rv = A._swarm_completion(body)
            resp = A.app.make_response(rv)
        finally:
            mock.patch.stopall()
    return resp, single


@pytest.mark.parametrize("model", ["swarm", "crew-code", "multi"])
def test_a_simple_question_skips_the_pipeline(model):
    boom = mock.Mock(side_effect=AssertionError("pipeline must not run"))
    resp, single = _run_completion(
        {"model": model, "messages": [{"role": "user", "content": SIMPLE}]},
        run=boom)
    assert not boom.called
    assert single.call_args[0][0]["model"] == "best"
    assert "fast-path" in resp.headers.get("X-Free-LLM-Hub-Pipeline", "")


def test_a_simple_tool_turn_is_fast_pathed_only_on_a_fresh_instruction():
    """CHANGED 2026-09-27: LIVE, Claude Code on multi/coding-swarm fanned
    "What is N plus 1?" out to five models (~180 s). A trivial ask carrying a
    tools array now takes the fast path too -- but never mid-loop, when the
    turn ends on a tool result."""
    tools = {"tools": [{"type": "function"}]}
    assert A._swarm_fast_path(tools, [{"role": "user", "content": SIMPLE}]) is True
    mid_loop = [{"role": "user", "content": SIMPLE},
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "c1", "type": "function",
                     "function": {"name": "x", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "c1", "content": "5768"}]
    assert A._swarm_fast_path(tools, mid_loop) is False


def test_the_fast_path_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(A.config, "get_flag",
                        lambda name, default=False:
                            False if name == "swarm_fast_path" else default)
    assert A._swarm_fast_path({}, [{"role": "user", "content": SIMPLE}]) is False


def test_a_hard_ask_still_runs_the_pipeline():
    assert A._swarm_fast_path({}, [{"role": "user", "content": HARD}]) is False


def test_the_wall_clock_default_and_off_switch(monkeypatch):
    """The BASE of the plan-sized budget: raised from 180 (a subscription
    manager spent all of it on three calls, see swarm.run)."""
    monkeypatch.setattr(A.config, "get_setting", lambda name, default=None: default)
    assert A._swarm_max_seconds() == A._SWARM_MAX_SECONDS_DEFAULT >= 300
    monkeypatch.setattr(A.config, "get_setting", lambda name, default=None: 0)
    assert A._swarm_max_seconds() is None
    monkeypatch.setattr(A.config, "get_setting", lambda name, default=None: "junk")
    assert A._swarm_max_seconds() == A._SWARM_MAX_SECONDS_DEFAULT


def _slow_dispatch(calls, slow=("Beta", "Gamma"), delay=2.0):
    def _d(msgs, max_tokens, exclude_pids=()):
        st = _stage(msgs)
        calls.append(st)
        if st == "plan":
            return PLAN_3_PARALLEL, "p/planner"
        if st == "phase":
            if any(("YOUR PHASE" in msgs[-1]["content"] and t in msgs[-1]["content"])
                   for t in slow):
                time.sleep(delay)
            return "part", "p/worker"
        if st == "review":
            return '{"verdict": "ship", "problems": []}', "q/rev"
        return "FINAL", "p/synth"
    return _d


def test_past_the_cap_the_run_stops_waiting_and_delivers():
    calls = []
    t0 = time.monotonic()
    out = swarm.run([{"role": "user", "content": HARD}], _slow_dispatch(calls),
                    max_seconds=0.3)
    assert time.monotonic() - t0 < 1.5, "must not wait for the slow phases"
    assert out["timed_out"] is True
    # The part that finished ships -- and the parts that did not are NAMED
    # (no grace given here, so nothing finished them): never silently partial.
    assert out["text"].startswith("part")
    assert "Not finished" in out["text"] and "Beta" in out["text"]
    assert out["unfinished"] == ["Beta", "Gamma"]
    assert "review" not in calls and "supervise" not in calls


def test_under_the_cap_nothing_changes():
    calls = []
    out = swarm.run([{"role": "user", "content": HARD}],
                    _slow_dispatch(calls, slow=()), max_seconds=30)
    assert out["timed_out"] is False
    assert {"plan", "phase", "supervise", "review", "synth"} <= set(calls)
    assert out["text"] == "FINAL"


def test_with_nothing_in_hand_it_waits_for_one_answer():
    """A cap spent before ANY phase answered must not deliver nothing."""
    calls = []
    out = swarm.run([{"role": "user", "content": HARD}],
                    _slow_dispatch(calls, slow=("Alpha", "Beta", "Gamma"), delay=0.4),
                    max_seconds=0.05)
    assert out["text"] and out["timed_out"] is True


def test_the_handler_passes_the_cap(monkeypatch):
    seen = {}

    def _fake_run(messages, dispatch, on_event=None, **kw):
        seen.update(kw)
        return {"text": "done", "models": []}
    monkeypatch.setattr(A, "_swarm_max_seconds", lambda: 42.0)
    _run_completion({"model": "swarm",
                     "messages": [{"role": "user", "content": HARD}]}, run=_fake_run)
    assert seen.get("max_seconds") == 42.0


# --------------------------------------------------------------------------- #
# 3. Trailers: the deliverable only, for anyone but the dashboard chat
# --------------------------------------------------------------------------- #

RESULT = {"text": "THE DELIVERABLE", "plan": {"goal": "G"},
          "phases": [{"title": "Écrire", "output": "x"}],
          "models": [("plan", "p/a"), ("synthesis", "p/b")],
          "review": {"problems": ["off by one"]}}


def test_an_api_caller_gets_the_deliverable_only():
    resp, _single = _run_completion(
        {"model": "swarm", "messages": [{"role": "user", "content": HARD}]},
        run=mock.Mock(return_value=dict(RESULT)))
    content = resp.get_json()["choices"][0]["message"]["content"]
    assert content == "THE DELIVERABLE"
    hdr = resp.headers["X-Free-LLM-Hub-Pipeline"]
    assert "models=plan:p/a" in hdr and "reviewer_raised=1" in hdr


def test_a_crew_api_caller_gets_no_crew_line(monkeypatch):
    monkeypatch.setattr(A.crews, "run",
                        lambda m, d, c, on_event=None, **kw: dict(RESULT, crew="code"))
    resp, _single = _run_completion(
        {"model": "crew-code", "messages": [{"role": "user", "content": HARD}]})
    content = resp.get_json()["choices"][0]["message"]["content"]
    assert "**Crew:**" not in content and "**Models used**" not in content
    assert "crew=code" in resp.headers["X-Free-LLM-Hub-Pipeline"]


def test_the_dashboard_chat_keeps_its_trailer():
    resp, _single = _run_completion(
        {"model": "swarm", "messages": [{"role": "user", "content": HARD}]},
        headers={"X-Free-LLM-Hub": "dashboard"},
        run=mock.Mock(return_value=dict(RESULT)))
    content = resp.get_json()["choices"][0]["message"]["content"]
    assert content.startswith("THE DELIVERABLE")
    assert "**Models used**" in content and "**Plan followed**" in content


def test_a_streamed_answer_carries_the_header_too():
    resp, _single = _run_completion(
        {"model": "swarm", "stream": True,
         "messages": [{"role": "user", "content": HARD}]},
        run=mock.Mock(return_value=dict(RESULT)))
    assert "models=" in resp.headers["X-Free-LLM-Hub-Pipeline"]
    body = "".join(x if isinstance(x, str) else x.decode() for x in resp.response)
    assert "Models used" not in body and "THE DELIVERABLE" in body


def test_the_dashboard_chat_marks_its_request():
    html = open("templates/index.html", encoding="utf-8").read()
    i = html.index("fetch('/v1/chat/completions'")
    assert "'X-Free-LLM-Hub': 'dashboard'" in html[i - 1200:i]


def test_the_summary_is_header_safe():
    line = swarm.trailer_summary(dict(RESULT, phases=[{"title": "Ünï\r\ncode"}],
                                      timed_out=True))
    line.encode("latin-1")
    assert "\n" not in line and "\r" not in line and "timed_out=1" in line
    assert swarm.trailer_summary({}) == ""


def test_the_activity_row_gets_what_the_trailer_had():
    with A.app.test_request_context():
        g.act = {}
        A._act_pipeline_result(dict(RESULT, timed_out=True))
        act = g.act
    assert act["pipeline_plan"] == ["Écrire"]
    assert act["pipeline_goal"] == "G"
    assert act["review_problems"] == ["off by one"]
    assert act["pipeline_timed_out"] is True


# --------------------------------------------------------------------------- #
# 4. Honest labels
# --------------------------------------------------------------------------- #

def test_multi_is_labelled_as_what_a_cli_actually_gets():
    import agentic_chat as AC
    assert "phased crew" in A._virtual_model_label("multi")
    assert "Multi sessions" not in A._virtual_model_label("multi")
    xhigh = [lvl for lvl in A._CODEX_LEVELS if lvl["effort"] == "xhigh"][0]
    assert "phased crew" in xhigh["description"]
    assert AC._OPENCODE_EFFORT["multi"].startswith("effort: multi")
    assert "phased crew" in AC._OPENCODE_EFFORT["multi"]
