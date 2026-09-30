"""After a restart a conversation waits for Continue, unless its owner ticked
"Continue by itself after a restart" (owner, 2026-09-30: "not let all
conversations continue automatically after restarting the computer or hub;
continued manually by me, or a checkbox if I want it to continue itself")."""
import agentic_history as AH
import app as A
import swarm_windows as SW

HTML = open("templates/index.html", encoding="utf-8").read()


def test_the_choice_is_kept_per_conversation_and_off_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(AH, "_root", lambda: str(tmp_path))
    AH.record_turn("c-1", "opencode", str(tmp_path), "user", "build it")
    assert AH.auto_resume("c-1") is False
    assert AH.set_auto_resume("c-1", True) is True and AH.auto_resume("c-1") is True
    assert AH.set_auto_resume("nope", True) is None


class _Run:
    def __init__(self, owner):
        self.owner = owner


def test_only_ticked_conversations_resume_by_themselves(monkeypatch):
    monkeypatch.setattr(A.agentic_history, "auto_resume", lambda sid: sid == "ticked")
    assert A._multi_should_auto_resume(_Run(None)) is True          # Swarm tab / MCP run
    assert A._multi_should_auto_resume(_Run("ticked")) is True
    assert A._multi_should_auto_resume(_Run("not-ticked")) is False


def test_a_run_not_resumed_stays_and_offers_continue(monkeypatch):
    run = SW._Run("goal", ".", "opencode", SW.clean_phases({"phases": [
        {"title": "a", "task": "t"}]}), owner="conv-w")
    run.restored, run.interrupted = True, True
    run.state = SW.FAILED
    run.agents[0].state, run.agents[0].error = SW.FAILED, SW.INTERRUPTED_ERROR
    SW._remember(run)
    noted = []
    monkeypatch.setattr(A.memory, "note_interrupted",
                        lambda sid, **k: noted.append((sid, k.get("why"))))
    try:
        got = SW.resume_interrupted(lambda *a: "s", lambda *a: iter(()),
                                    should_resume=lambda r: False)
        assert run.id not in got and run in SW.interrupted_runs()
        A._file_unresumed_runs()
        assert ("conv-w", "hub restarted") in noted
    finally:
        with SW._LOCK:
            SW._RUNS.pop(run.id, None)


def test_the_continue_button_continues_a_multi_run():
    assert A._multi_is_continue(A._CONTINUE_TEXT) is True
    assert A._CONTINUE_TEXT in HTML.replace("\n", " ") or \
        "Continue exactly where you stopped. Do not start over;" in HTML


def test_only_ticked_turns_continue_by_themselves(monkeypatch):
    monkeypatch.setattr(A.agentic_history, "auto_resume", lambda sid: sid == "ticked")
    resumed, sent = [], []
    monkeypatch.setattr(A, "api_agent_resume_session", lambda sid: resumed.append(sid))
    monkeypatch.setattr(A.agentic_chat, "send_message_stream_durable",
                        lambda sid, text: (sent.append((sid, text)) or iter(())))
    A._auto_continue_turns(["ticked", "waits"])
    assert resumed == ["ticked"] and sent == [("ticked", A._CONTINUE_TEXT)]


def test_the_page_has_the_checkbox_and_keeps_it_in_sync():
    assert 'id="agent-auto-resume"' in HTML
    assert "Continue by itself after a restart" in HTML
    assert HTML.count("setAutoResume(r.auto_resume);") == 3
    assert "'/auto-resume'" in HTML
