"""A conversation opened while it works says so and follows the running turn.

Owner, 2026-09-30: "while working, and I open the conversation, I don't see
working anymore and I will think it was blocked". FOUND: opening a conversation
from the History list asked GET /sessions/<id>, mounted the transcript and said
"send a message to continue" without ever reading currently_running -- only a
page load (/resume) reattached. And currently_running meant "a CLI process is
alive", so the gap between two processes of one turn read as idle.
"""
import agentic_chat as AC

HTML = open("templates/index.html", encoding="utf-8").read()


def _open_from_list():
    body = HTML[HTML.index("api('/api/agent/sessions/' + encodeURIComponent(conv.session_id)).then(function(r){"):]
    return body[:body.index("if (body.code === 'folder_gone')")]


def test_both_ways_of_opening_from_the_list_reattach_a_running_turn():
    body = _open_from_list()
    assert body.count("if (r.currently_running){") == 2
    assert body.count("showReconnectedStillWorking(sessionId") == 2
    # ...and it checks BEFORE the "send a message to continue" toast
    assert body.index("showReconnectedStillWorking") < body.index("still live, send a message")


def test_a_turn_between_two_processes_still_reads_as_working(monkeypatch):
    sess = AC._Session("opencode", ".")
    with AC._REGISTRY_LOCK:
        AC._REGISTRY[sess.id] = sess
    try:
        assert AC.get_session(sess.id)["currently_running"] is False
        sess.turn_lock.acquire()                       # the turn owns the session
        try:
            assert AC.get_session(sess.id)["currently_running"] is True
        finally:
            sess.turn_lock.release()
        assert AC.get_session(sess.id)["currently_running"] is False
    finally:
        with AC._REGISTRY_LOCK:
            AC._REGISTRY.pop(sess.id, None)


# --------------------------------------------------------------------------- #
# The stage is shown as it happens (owner, 2026-09-30: "it should show if it is
# planning etc ... instantly, like /activity")
# --------------------------------------------------------------------------- #
import time

import app as A
import swarm_windows as SW


def test_a_slow_plan_says_planning_at_once_and_keeps_saying_it(monkeypatch):
    monkeypatch.setattr(A, "_MULTI_PLAN_HEARTBEAT", 0.05)
    monkeypatch.setattr(A, "_MULTI_POLL", 0.02)
    seen_planning = []

    def slow_start(*a, **k):
        seen_planning.append(A._multi_run_plan("conv-p") or {})
        time.sleep(0.2)
        raise SW.SwarmWindowsError("could not turn that into phases")
    monkeypatch.setattr(A.swarm_windows, "start", slow_start)
    monkeypatch.setattr(A, "_multi_context", lambda *a, **k: "")
    monkeypatch.setattr(A.swarm_windows, "last_run_for", lambda sid: None)
    evs = list(A._multi_turn_events("conv-p", {"cli": "opencode", "project_dir": ".",
                                               "quality": "multi"}, "build it"))
    assert evs[0] == {"event": "tool", "text": A._MULTI_PLANNING_LINE}
    assert any("Still planning (" in e.get("text", "") for e in evs)
    assert evs[-1]["event"] == "error"
    # while it planned, the task strip said so; afterwards it does not
    assert seen_planning[0]["tasks"][0]["doing"] is True
    assert "planning" in seen_planning[0]["source"]
    assert A._multi_run_plan("conv-p") is None


def test_the_plan_names_what_each_helper_waits_for():
    # Phases start as soon as their own needs are finished (not wave by wave),
    # so the lines say what each one waits for.
    lines = A._multi_plan_lines({"agents": [
        {"index": 1, "title": "Backend", "needs": []},
        {"index": 2, "title": "Frontend", "needs": []},
        {"index": 3, "title": "API tests", "needs": [1]},
        {"index": 4, "title": "Review and finish", "needs": [1, 2, 3]}],
        "waves": [[1, 2], [3], [4]]})
    assert lines == [
        "Plan · start now (2 helpers at the same time): 1 · Backend | 2 · Frontend",
        "Plan · 3 · API tests: starts when 1 is done",
        "Plan · 4 · Review and finish: starts when all the others are done"]


def test_the_plan_is_shown_wave_by_wave():
    lines = A._multi_plan_lines({"agents": [
        {"index": 1, "title": "Backend"}, {"index": 2, "title": "Frontend"},
        {"index": 3, "title": "Review and finish"}], "waves": [[1, 2], [3]]})
    assert lines == ["Plan \u00b7 first (2 helpers at the same time): 1 \u00b7 Backend | 2 \u00b7 Frontend",
                     "Plan \u00b7 then: 3 \u00b7 Review and finish"]


# --------------------------------------------------------------------------- #
# Stop works in the whole span the page calls "working"
# --------------------------------------------------------------------------- #
def _registered():
    sess = AC._Session("opencode", ".")
    with AC._REGISTRY_LOCK:
        AC._REGISTRY[sess.id] = sess
    return sess


def _drop(sess):
    with AC._REGISTRY_LOCK:
        AC._REGISTRY.pop(sess.id, None)


def test_a_stop_between_processes_waits_for_the_next_one():
    sess = _registered()
    try:
        assert AC.stop_session(sess.id) is False and sess.stop_pending is False   # idle
        sess.turn_lock.acquire()
        try:
            assert AC.stop_session(sess.id) is True
            assert sess.stop_pending is True
        finally:
            sess.turn_lock.release()
    finally:
        _drop(sess)


def test_every_spawn_honours_it_and_every_new_turn_forgets_an_old_one():
    src = open("agentic_chat.py", encoding="utf-8").read()
    assert src.count("stop_now, sess.stop_pending = sess.stop_pending, False") == 2
    assert src.count("sess.stop_pending = False\n") >= 2          # both turn entries
