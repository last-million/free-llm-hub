"""The conversation's page shows what a Multi run's working phase is doing.

MEASURED 2026-09-30, run swarm-4f2aab7204a4: phase 2 worked for an hour (164
edit / bash / note events in its log) and the Build page showed nothing
between "phase 2 started" and the end. Owner: "why I see nothing happen ...
it's blocked or what?"
"""
import app as A
import swarm_windows as SW


def _run():
    run = SW._Run("fix it", ".", "opencode", SW.clean_phases({"phases": [
        {"title": "Diagnose", "task": "t"}, {"title": "Fix", "task": "t"}]}), owner="c")
    run.state = SW.RUNNING
    run.agents[0].state = SW.DONE
    run.agents[1].state = SW.RUNNING
    SW._remember(run)
    return run


def _emit(agent, *events):
    for e in events:
        agent.events.append(e)
        agent.event_total += 1


def _forget(run):
    with SW._LOCK:
        SW._RUNS.pop(run.id, None)


def test_new_actions_of_the_working_phase_are_shown_once():
    run = _run()
    try:
        fwd = {}
        _emit(run.agents[1], {"event": "tool", "text": "read app.py"})
        assert A._multi_activity(SW.status(run.id, with_events=True), fwd) == ["Phase 2 · read app.py"]
        assert A._multi_activity(SW.status(run.id, with_events=True), fwd) == []                # nothing new
        _emit(run.agents[1], {"event": "message", "text": "The blur inflates by 1px."},
              {"event": "tool", "text": "edit app.py"},
              {"event": "usage", "text": "ignored"})
        assert A._multi_activity(SW.status(run.id, with_events=True), fwd) == [
            "Phase 2 · The blur inflates by 1px.", "Phase 2 · edit app.py"]
    finally:
        _forget(run)


def test_a_page_that_reloads_sees_the_last_few_not_the_history():
    run = _run()
    try:
        _emit(run.agents[1], *[{"event": "tool", "text": "step %d" % i} for i in range(50)])
        lines = A._multi_activity(SW.status(run.id, with_events=True), {})
        assert lines == ["Phase 2 · step %d" % i for i in range(46, 50)]
    finally:
        _forget(run)


def test_a_full_ring_still_shows_what_is_new():
    run = _run()
    try:
        fwd = {}
        _emit(run.agents[1], *[{"event": "tool", "text": "old %d" % i}
                               for i in range(SW.EVENT_BUFFER + 10)])
        A._multi_activity(SW.status(run.id, with_events=True), fwd)
        _emit(run.agents[1], {"event": "tool", "text": "brand new"})
        assert A._multi_activity(SW.status(run.id, with_events=True), fwd) == ["Phase 2 · brand new"]
    finally:
        _forget(run)


def test_finished_phases_and_long_lines():
    run = _run()
    try:
        _emit(run.agents[0], {"event": "tool", "text": "done-phase action"})
        _emit(run.agents[1], {"event": "tool", "text": "bash " + "x" * 400})
        lines = A._multi_activity(SW.status(run.id, with_events=True), {})
        assert len(lines) == 1 and lines[0].startswith("Phase 2 · bash ")
        assert len(lines[0]) <= len("Phase 2 · ") + A._MULTI_ACTIVITY_CHARS
    finally:
        _forget(run)


def test_the_follower_forwards_them_as_tool_lines():
    src = open("app.py", encoding="utf-8").read()
    body = src[src.index("def _multi_follow_events("):]
    body = body[:body.index("\ndef ", 10)]
    assert "swarm_windows.status(run_id, with_events=True)" in body
    assert "_multi_activity(st, forwarded)" in body
    assert body.index("_multi_activity(st, forwarded)") < body.index("time.sleep(_MULTI_POLL)")
