"""Show WHICH agent ran on WHICH model for a swarm/crew request.

USER 2026-08-08: "is it possible in /activity to see the subagents and swarm
agents crews and which models?"

It nearly was already. A swarm request is ONE Flask request, so every stage's
_act_pick lands on the SAME activity row -- `hops` already accumulated all ~10
stage models, with no way to tell the planner from a worker from the reviewer.
And swarm.py has always tracked the rest:

  * result["models"] -> [(role, model)] with roles "plan", "phase:<title>",
    "supervisor", "review", "repair:<title>", "synthesis"
  * 12 emit(kind, detail) call sites for live stage progress
  * crews.run adds result["crew"]

swarm.run even took an `on_event` parameter for exactly this -- app.py just
never passed one, so the plumbing sat unused and the data only ever reached the
answer's text trailer.
"""
import pytest

import app
import crews
import swarm


class _G:
    """Stand-in for flask.g carrying an activity dict."""
    def __init__(self):
        self.act = {"id": 1, "hops": []}


@pytest.fixture
def act(monkeypatch):
    g = _G()
    monkeypatch.setattr(app, "g", g)
    return g.act


# --------------------------------------------------------------------------- #
# The watcher
# --------------------------------------------------------------------------- #

def test_stage_events_land_on_the_activity_row(act):
    on_event = app._act_pipeline_watcher()
    on_event("plan", "3 phases")
    on_event("phase", "2 in parallel: api, ui")
    on_event("done", "complete")
    kinds = [s["kind"] for s in act["stages"]]
    assert kinds == ["plan", "phase", "done"]
    assert act["stages"][1]["detail"] == "2 in parallel: api, ui"
    assert all("at" in s for s in act["stages"])


def test_the_stage_trail_is_bounded(act):
    on_event = app._act_pipeline_watcher()
    for i in range(200):
        on_event("phase", "step %d" % i)
    assert len(act["stages"]) <= 40, "an unbounded trail would grow the feed forever"


def test_watcher_is_a_noop_without_an_activity_row(monkeypatch):
    """Swarm can run outside a request (MCP tool path) -- must not explode."""
    class _Empty:
        act = None
    monkeypatch.setattr(app, "g", _Empty())
    assert app._act_pipeline_watcher() is None


def test_watcher_never_raises_on_junk(act):
    on_event = app._act_pipeline_watcher()
    on_event(None, None)
    on_event(object(), object())


# --------------------------------------------------------------------------- #
# The result stamp
# --------------------------------------------------------------------------- #

def test_per_role_models_are_recorded(act):
    app._act_pipeline_result({
        "crew": "code",
        "models": [("plan", "google/gemini-3.5-flash"),
                   ("phase:API layer", "nvidia/z-ai/glm-5.2"),
                   ("review", "kilocode/tencent/hy3:free")],
    })
    assert act["crew"] == "code"
    roles = [p["role"] for p in act["pipeline"]]
    assert roles == ["plan", "phase:API layer", "review"]
    assert act["pipeline"][1]["model"] == "nvidia/z-ai/glm-5.2"


def test_a_generic_swarm_has_no_crew_but_still_lists_agents(act):
    """crews.run sets crew="" for an undetected persona -- that must not
    masquerade as a crew, but the agents still matter."""
    app._act_pipeline_result({"crew": "", "models": [("plan", "groq/llama")]})
    assert "crew" not in act
    assert len(act["pipeline"]) == 1


def test_result_stamp_never_raises_on_junk(act):
    app._act_pipeline_result(None)
    app._act_pipeline_result({"models": "not-a-list"})
    app._act_pipeline_result({"models": [("only-one",)]})


# --------------------------------------------------------------------------- #
# The hook is actually reachable end to end
# --------------------------------------------------------------------------- #

def test_crews_run_forwards_on_event_to_swarm(monkeypatch):
    """crews.run gained the parameter for this; without the forward, every
    crew request would still show an opaque row."""
    seen = {}

    def fake_swarm_run(messages, dispatch, profile=None, on_event=None):
        seen["on_event"] = on_event
        return {"models": [], "text": "x"}

    monkeypatch.setattr(crews.swarm, "run", fake_swarm_run)
    sentinel = lambda k, d: None
    crews.run([{"role": "user", "content": "hi"}], lambda *a, **k: ("", ""),
              "code", on_event=sentinel)
    assert seen["on_event"] is sentinel


def test_swarm_run_still_accepts_no_on_event():
    """Optional everywhere -- the MCP/crew-tool paths call it without one."""
    import inspect
    assert inspect.signature(swarm.run).parameters["on_event"].default is None
    assert inspect.signature(crews.run).parameters["on_event"].default is None
