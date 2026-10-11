"""One Multi run per conversation (and per folder), and the Build page shows
the run that is WORKING.

MEASURED 2026-10-11 (conversation f0e3b185, folder "bount en"): a message
sent while the first run was still planning (3 min) passed the "already
working" check, which only knew runs whose plan had finished, so a second run
(swarm-df98fc7678fb) worked the same folder beside swarm-7d2d17f482bd. The
later run ended first, and the conversation's page then showed the old
PROGRESS.md while swarm-7d2d17f482bd was still working. Hermetic: fake runs
inserted into swarm_windows' registry, no planner, no CLI, no network.
"""
import os

import pytest

import app as A
import swarm_windows as SW

PHASES = [{"title": "Build", "task": "build it", "needs": []},
          {"title": "Review", "task": "review it", "needs": [1]}]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(A, "_MULTI_CLAIMS", {})
    monkeypatch.setattr(A, "_MULTI_PLANNING", {})
    monkeypatch.setattr(SW, "_RUNS", {})
    yield


def _run(owner, folder, state=SW.RUNNING, created=None):
    run = SW._Run("goal", folder, "opencode", PHASES, owner=owner)
    run.state = state
    if created is not None:
        run.created_at = created
    SW._RUNS[run.id] = run
    return run


def test_a_second_message_while_planning_is_refused(tmp_path):
    folder = str(tmp_path)
    assert A._multi_claim("conv-a", folder) is None
    why = A._multi_claim("conv-a", folder)
    assert why and "still being planned" in why
    A._multi_release("conv-a")
    assert A._multi_claim("conv-a", folder) is None


def test_another_conversation_cannot_start_in_the_same_folder(tmp_path):
    folder = str(tmp_path)
    assert A._multi_claim("conv-a", folder) is None
    why = A._multi_claim("conv-b", os.path.join(folder, "."))
    assert why and "/agent/conv-a" in why
    other = tmp_path / "other"
    other.mkdir()
    assert A._multi_claim("conv-b", str(other)) is None


def test_the_folder_rule_has_a_switch(tmp_path, monkeypatch):
    real = A.config.get_flag
    monkeypatch.setattr(A.config, "get_flag",
                        lambda n, d=None: False if n == "multi_one_run_per_folder" else real(n, d))
    folder = str(tmp_path)
    assert A._multi_claim("conv-a", folder) is None
    assert A._multi_claim("conv-b", folder) is None
    # The same conversation is still refused: that is not a folder rule.
    assert A._multi_claim("conv-a", folder)


def test_a_live_run_blocks_its_conversation_and_its_folder(tmp_path):
    folder = str(tmp_path)
    run = _run("conv-a", folder)
    assert "already working on this conversation" in A._multi_claim("conv-a", folder)
    why = A._multi_claim("conv-b", folder)
    assert why and run.id in why and "/agent/conv-a" in why
    run.state = SW.DONE
    assert A._multi_claim("conv-b", folder) is None


def test_the_turn_answers_409_while_claimed_and_releases_after(tmp_path, monkeypatch):
    folder = str(tmp_path)
    seen = []

    def fake(session_id, sess_info, text, bare=False):
        seen.append(dict(A._MULTI_CLAIMS))
        yield {"event": "done", "text": "ok"}

    monkeypatch.setattr(A, "_multi_turn_events_claimed", fake)
    info = {"project_dir": folder}
    assert list(A._multi_turn_events("conv-a", info, "build")) == [{"event": "done", "text": "ok"}]
    assert "conv-a" in seen[0]                 # held while the turn ran
    assert A._MULTI_CLAIMS == {}               # released after

    assert A._multi_claim("conv-a", folder) is None
    evs = list(A._multi_turn_events("conv-a", info, "again"))
    assert evs[0]["event"] == "error" and evs[0]["status"] == 409
    assert len(seen) == 1                      # no second run started


def test_a_turn_dropped_midway_still_releases(tmp_path, monkeypatch):
    def fake(session_id, sess_info, text, bare=False):
        yield {"event": "tool", "text": "planning"}
        yield {"event": "done", "text": "ok"}

    monkeypatch.setattr(A, "_multi_turn_events_claimed", fake)
    gen = A._multi_turn_events("conv-a", {"project_dir": str(tmp_path)}, "x")
    next(gen)
    assert "conv-a" in A._MULTI_CLAIMS
    gen.close()
    assert A._MULTI_CLAIMS == {}


def test_live_run_for_prefers_the_working_run():
    old = _run("conv-a", ".", state=SW.RUNNING, created=100.0)
    _run("conv-a", ".", state=SW.DONE, created=200.0)
    assert SW.last_run_for("conv-a").created_at == 200.0
    assert SW.live_run_for("conv-a") is old
    assert SW.live_run_for("conv-b") is None
    assert SW.live_run_for(None) is None


def test_the_page_shows_the_working_run_not_the_newer_finished_one(tmp_path):
    folder = str(tmp_path)
    live = _run("conv-a", folder, state=SW.RUNNING, created=100.0)
    done = _run("conv-a", folder, state=SW.DONE, created=200.0)
    done.ended_at = 150.0
    (tmp_path / "PROGRESS.md").write_text("- [x] old plan\n", encoding="utf-8")
    plan = A._multi_run_plan("conv-a", folder)
    assert plan is not None
    assert plan["run_id"] == live.id and plan["run_state"] == SW.RUNNING


def _page():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "templates", "index.html"), encoding="utf-8") as f:
        return f.read()


def test_the_page_says_a_run_is_still_working_and_offers_stop():
    src = _page()
    assert 'id="agent-bg-run"' in src and 'role="status"' in src
    assert "Multi run still working in the background" in src
    assert "function renderBgRun" in src
    # Stop goes through the run's own stop route.
    i = src.index("function renderBgRun")
    body = src[i:i + 3000]
    assert "'/api/swarm-windows/' + encodeURIComponent(run.run_id)" in body
    assert "method: 'DELETE'" in body


def test_messages_typed_while_it_works_wait_in_the_queue():
    src = _page()
    assert "if (turnBusy || _bgRunLive){" in src
    assert "if (turnBusy || _bgRunLive || _queuePaused" in src
    i = src.index("var bgLive = ")
    assert "queueDrain()" in src[i:i + 1200]
