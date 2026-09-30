"""The "Running now" popup opens any conversation, helper or preview in a new
window, and shows a Multi run's helpers under their conversation.

Owner, 2026-09-30: "in the Running now popup, we can click and open each
running conversation or helper, no matter, in a new window".
"""
import app as A
import swarm_windows as SW

HTML = open("templates/index.html", encoding="utf-8").read()


def _js(start, end):
    body = HTML[HTML.index(start):]
    return body[:body.index(end)]


def test_every_row_can_open_in_a_new_window_safely():
    row = _js("function runRow(", "function openRunning(")
    assert "a.target = '_blank'" in row and "a.rel = 'noopener'" in row
    assert "in a new window" in row                                  # the link says so
    panel = _js("function openRunning(", "function load(){") + _js("function load(){", "load();\n    }")
    assert "href: '/agent/' + encodeURIComponent(x.session_id)" in panel
    assert "x.url || ('http://127.0.0.1:' + x.port" in panel         # previews open their site


def test_helpers_are_nested_under_their_conversation():
    panel = _js("function openRunning(", "/* Browse the LOCAL machine")
    assert "h.helper.owner === x.session_id" in panel
    assert "'Helper ' + h.index" in panel
    assert ".runrow-helper{" in HTML


def test_the_sessions_list_names_the_helpers(monkeypatch):
    run = SW._Run("goal", ".", "opencode", SW.clean_phases({"phases": [
        {"title": "Build the page", "task": "t"}, {"title": "Test it", "task": "t"}]}),
        owner="conv-9")
    run.agents[1].session_id = "w-9"
    run.agents[1].state = SW.RUNNING
    run.agents[0].past_sessions = ["w-old"]
    SW._remember(run)
    try:
        assert SW.worker_info("w-9")["index"] == 2
        assert SW.worker_info("w-9")["owner"] == "conv-9"
        assert SW.worker_info("w-old")["index"] == 1                # a retried worker too
        assert SW.worker_info("conv-9") is None
        monkeypatch.setattr(A, "_agent_gate", lambda: None)
        monkeypatch.setattr(A.agentic_chat, "list_sessions",
                            lambda: [{"session_id": "conv-9"}, {"session_id": "w-9"}])
        with A.app.test_request_context("/api/agent/sessions"):
            rows = A.api_agent_list_sessions().get_json()["sessions"]
        assert "helper" not in rows[0]
        assert rows[1]["helper"]["title"] == "Test it" and rows[1]["helper"]["state"] == SW.RUNNING
    finally:
        with SW._LOCK:
            SW._RUNS.pop(run.id, None)
