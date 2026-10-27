"""The completeness check for hub-owned Build turns (app._stall_*): when a turn ends, judge like a
human whether it FINISHED, and if not, schedule ONE bounded auto-retry (same model first, then another).

Hermetic: the actual re-send is injected, nothing starts a CLI or a thread that outlives the test.
"""
import pytest

import app as A
import stallcheck


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    A._STALL_RETRIES.clear()
    monkeypatch.setattr(A.config, "get_flag",
                        lambda name, default=None: True if name == "stall_recheck" else default)
    yield
    A._STALL_RETRIES.clear()


def _info(**kw):
    base = {"cli": "opencode", "project_dir": "C:/p", "quality": "max", "final_reply": "",
            "used_tools": False, "observed": 0, "interrupted": False, "rejected": False,
            "stopped": False, "errored": False, "instruction": "fix the failing test in parser.py"}
    base.update(kw)
    return base


# --- is this reply a real, finished answer? --------------------------------------------------

REAL = ("The test failed because parse() dropped the last token; I changed the loop bound to "
        "len(tokens) and the suite now passes (14 passed).")


def test_a_real_answer_is_ok():
    assert A._stall_answer_ok(REAL, "fix the failing test in parser.py") is True


@pytest.mark.parametrize("reply", [
    "",
    "   ",
    "<think>I should look at the file</think>",
    "Sure - how can I assist you today? I can help with:\n\n- Explaining concepts",
    ("The API key used for this request has reached its budget. Please raise the key budget "
     "(https://enter.pollinations.ai/edit-key?id=X&ref=agent_key_budget), then try again."),
    "It looks like your message contains only a tool setup and no specific request.",
    "I'll read the config file next.",
    "Let me check hp_monitor.py and the identity config.",
])
def test_non_answers_are_not_ok(reply):
    assert A._stall_answer_ok(reply, "fix the failing test in parser.py") is False


def test_a_question_back_to_the_user_is_a_real_answer():
    assert A._stall_answer_ok("Which environment should I target, staging or prod?", "deploy it") is True


# --- the decision, from a turn's info --------------------------------------------------------

def test_a_finished_turn_is_never_retried():
    d = A._stall_decide("s1", _info(final_reply=REAL, used_tools=True))
    assert d.retry is False and d.reason == stallcheck.DONE


def test_a_user_stop_is_never_retried():
    d = A._stall_decide("s1", _info(stopped=True, interrupted=True))
    assert d.retry is False


def test_a_rejected_turn_is_never_retried():
    assert A._stall_decide("s1", _info(rejected=True)).retry is False


def test_an_empty_turn_retries_same_model_first_then_switches():
    d1 = A._stall_decide("s1", _info())
    assert d1.retry and d1.switch_model is False
    A._STALL_RETRIES["s1"] = ("fix the failing test in parser.py", 1)
    d2 = A._stall_decide("s1", _info())
    assert d2.retry and d2.switch_model is True


def test_an_error_switches_model_at_once():
    d = A._stall_decide("s1", _info(errored=True, interrupted=True))
    assert d.retry and d.switch_model is True and d.reason == stallcheck.ERROR


def test_it_gives_up_after_the_cap():
    A._STALL_RETRIES["s1"] = ("fix the failing test in parser.py", stallcheck.MAX_RETRIES)
    d = A._stall_decide("s1", _info())
    assert d.retry is False and d.reason == stallcheck.EXHAUSTED


def test_a_new_instruction_resets_the_count():
    A._STALL_RETRIES["s1"] = ("an older instruction", stallcheck.MAX_RETRIES)
    d = A._stall_decide("s1", _info(instruction="now fix the other thing"))
    assert d.retry is True and d.attempt == 1


# --- where it must NOT run -------------------------------------------------------------------

def test_the_flag_off_disables_it(monkeypatch):
    monkeypatch.setattr(A.config, "get_flag",
                        lambda name, default=None: False if name == "stall_recheck" else default)
    sent = []
    A._stall_on_turn_end("s1", _info(), send=lambda sid, text: sent.append((sid, text)), sync=True)
    assert sent == []


def test_a_multi_conversation_is_left_to_its_own_retries():
    sent = []
    A._stall_on_turn_end("s1", _info(quality="multi"), send=lambda sid, t: sent.append(t), sync=True)
    assert sent == []


def test_a_multi_worker_session_is_left_alone(monkeypatch):
    monkeypatch.setattr(A.swarm_windows, "worker_info", lambda sid: {"run_id": "r"})
    sent = []
    A._stall_on_turn_end("w1", _info(), send=lambda sid, t: sent.append(t), sync=True)
    assert sent == []


# --- the retry itself ------------------------------------------------------------------------

def test_a_stalled_turn_resends_continue_on_the_same_model_first(monkeypatch):
    dropped = []
    monkeypatch.setattr(A, "_session_pin_drop", lambda k: dropped.append(k) or True)
    sent = []
    A._stall_on_turn_end("s1", _info(), send=lambda sid, t: sent.append((sid, t)), sync=True)
    assert len(sent) == 1 and sent[0][0] == "s1"
    assert "continue" in sent[0][1].lower()
    assert dropped == []                                 # same model: the pin is kept


def test_the_second_retry_drops_the_pin_to_switch_model(monkeypatch):
    dropped = []
    monkeypatch.setattr(A, "_session_pin_drop", lambda k: dropped.append(k) or True)
    sent = []
    A._stall_on_turn_end("s1", _info(), send=lambda sid, t: sent.append(t), sync=True)
    A._stall_on_turn_end("s1", _info(), send=lambda sid, t: sent.append(t), sync=True)
    assert len(sent) == 2 and dropped == ["s1"]          # the 2nd one switched


def test_it_never_retries_more_than_the_cap(monkeypatch):
    monkeypatch.setattr(A, "_session_pin_drop", lambda k: True)
    sent = []
    for _ in range(6):
        A._stall_on_turn_end("s1", _info(), send=lambda sid, t: sent.append(t), sync=True)
    assert len(sent) == stallcheck.MAX_RETRIES


def test_a_failing_send_never_raises(monkeypatch):
    monkeypatch.setattr(A, "_session_pin_drop", lambda k: True)

    def boom(sid, t):
        raise RuntimeError("cli exploded")
    A._stall_on_turn_end("s1", _info(), send=boom, sync=True)        # must not raise


def test_a_finished_turn_clears_its_retry_count():
    A._STALL_RETRIES["s1"] = ("fix the failing test in parser.py", 1)
    A._stall_on_turn_end("s1", _info(final_reply=REAL, used_tools=True),
                         send=lambda sid, t: None, sync=True)
    assert "s1" not in A._STALL_RETRIES


def test_the_hook_is_registered_with_the_agent_layer():
    assert A.agentic_chat._turn_end_hook is not None


# --- the real durable turn calls the hook with the right facts ---------------------------------

AC = A.agentic_chat


def _drive(monkeypatch, events, quality="max"):
    """Run the real send_message_stream_durable over a fake CLI that yields `events`; return
    the single (session_id, info) the turn-end hook received."""
    got = []
    monkeypatch.setattr(AC, "_turn_end_hook", lambda sid, info: got.append((sid, info)))
    monkeypatch.setattr(AC, "send_message_stream", lambda sid, text: iter(events))
    monkeypatch.setattr(AC, "get_session", lambda sid: {"cli": "opencode", "project_dir": ".",
                                                        "quality": quality})
    list(AC.send_message_stream_durable("s-int", "fix the bug"))
    assert len(got) == 1
    return got[0]


def test_a_finished_turn_reports_its_reply_and_tools(monkeypatch):
    sid, info = _drive(monkeypatch, [
        {"event": "tool", "text": "edit parser.py"},
        {"event": "done", "text": "All done: the bug was in parse()."}])
    assert sid == "s-int"
    assert info["final_reply"].startswith("All done") and info["used_tools"] is True
    assert info["stopped"] is False and info["errored"] is False
    assert info["instruction"] == "fix the bug" and info["quality"] == "max"


def test_a_turn_with_no_reply_reports_nothing_done(monkeypatch):
    _sid, info = _drive(monkeypatch, [{"event": "output", "text": "..."}])
    assert not info["final_reply"] and info["used_tools"] is False


def test_a_user_stop_is_reported_as_stopped_not_errored(monkeypatch):
    _sid, info = _drive(monkeypatch, [{"event": "stopped"}])
    assert info["stopped"] is True and info["errored"] is False and info["interrupted"] is True


def test_a_failed_turn_is_reported_as_errored(monkeypatch):
    _sid, info = _drive(monkeypatch, [{"event": "error", "status": 502, "detail": "bad gateway"}])
    assert info["errored"] is True and info["stopped"] is False


def test_a_hook_that_raises_never_breaks_the_turn(monkeypatch):
    def boom(sid, info):
        raise RuntimeError("hook exploded")
    monkeypatch.setattr(AC, "_turn_end_hook", boom)
    monkeypatch.setattr(AC, "send_message_stream",
                        lambda sid, text: iter([{"event": "done", "text": "ok done"}]))
    monkeypatch.setattr(AC, "get_session", lambda sid: None)
    evs = list(AC.send_message_stream_durable("s-int2", "go"))
    assert any(e.get("event") == "done" for e in evs)


def test_the_whole_chain_is_bounded_and_the_second_retry_switches_model(monkeypatch):
    """A CLI that NEVER finishes: each retry is a REAL durable turn whose own end fires the hook
    again. Exactly 1 + MAX_RETRIES turns run, the 2nd retry (and only it) drops the pin, and the
    chain stops by itself -- the proof that this can never run all night."""
    turns, dropped = [], []
    monkeypatch.setattr(A, "_session_pin_drop", lambda k: dropped.append(k) or True)

    def stream(sid, text):
        turns.append(text)
        yield {"event": "output", "text": "..."}
    monkeypatch.setattr(AC, "send_message_stream", stream)
    monkeypatch.setattr(AC, "get_session", lambda sid: {"cli": "opencode", "project_dir": ".",
                                                        "quality": "max"})
    monkeypatch.setattr(AC, "_turn_end_hook", lambda sid, info: A._stall_on_turn_end(
        sid, info, send=lambda s, t: list(AC.send_message_stream_durable(s, t)), sync=True))
    list(AC.send_message_stream_durable("s-chain", "fix the failing test in parser.py"))
    assert len(turns) == 1 + stallcheck.MAX_RETRIES
    assert turns[0] == "fix the failing test in parser.py"
    assert turns[1:] == [A._CONTINUE_TEXT] * stallcheck.MAX_RETRIES
    assert dropped == ["s-chain"]            # 1st retry same model (pin kept), 2nd switched


def test_a_chain_that_finishes_on_the_retry_stops_there(monkeypatch):
    turns = []
    monkeypatch.setattr(A, "_session_pin_drop", lambda k: True)

    def stream(sid, text):
        turns.append(text)
        if len(turns) == 1:
            yield {"event": "output", "text": "..."}                      # stalls
        else:
            yield {"event": "tool", "text": "edit parser.py"}
            yield {"event": "done", "text": "Fixed: parse() dropped the last token; tests pass."}
    monkeypatch.setattr(AC, "send_message_stream", stream)
    monkeypatch.setattr(AC, "get_session", lambda sid: {"cli": "opencode", "project_dir": ".",
                                                        "quality": "max"})
    monkeypatch.setattr(AC, "_turn_end_hook", lambda sid, info: A._stall_on_turn_end(
        sid, info, send=lambda s, t: list(AC.send_message_stream_durable(s, t)), sync=True))
    list(AC.send_message_stream_durable("s-ok", "fix the failing test in parser.py"))
    assert len(turns) == 2                    # the original + exactly one retry, no more
    assert "s-ok" not in A._STALL_RETRIES     # a finished chain leaves nothing behind


def test_a_user_stop_during_a_retry_ends_the_chain(monkeypatch):
    turns = []
    monkeypatch.setattr(A, "_session_pin_drop", lambda k: True)

    def stream(sid, text):
        turns.append(text)
        if len(turns) == 1:
            yield {"event": "output", "text": "..."}
        else:
            yield {"event": "stopped"}
    monkeypatch.setattr(AC, "send_message_stream", stream)
    monkeypatch.setattr(AC, "get_session", lambda sid: {"cli": "opencode", "project_dir": ".",
                                                        "quality": "max"})
    monkeypatch.setattr(AC, "_turn_end_hook", lambda sid, info: A._stall_on_turn_end(
        sid, info, send=lambda s, t: list(AC.send_message_stream_durable(s, t)), sync=True))
    list(AC.send_message_stream_durable("s-stop", "fix the failing test in parser.py"))
    assert len(turns) == 2 and "s-stop" not in A._STALL_RETRIES


def test_end_to_end_an_empty_turn_schedules_exactly_one_same_model_retry(monkeypatch):
    sent = []
    monkeypatch.setattr(A, "_session_pin_drop", lambda k: True)
    monkeypatch.setattr(AC, "send_message_stream", lambda sid, text: iter(
        [{"event": "output", "text": "..."}]))
    monkeypatch.setattr(AC, "get_session", lambda sid: {"cli": "opencode", "project_dir": ".",
                                                        "quality": "max"})
    monkeypatch.setattr(AC, "_turn_end_hook",
                        lambda sid, info: A._stall_on_turn_end(
                            sid, info, send=lambda s, t: sent.append(t), sync=True))
    list(AC.send_message_stream_durable("s-e2e", "fix the failing test in parser.py"))
    assert len(sent) == 1 and sent[0] == A._CONTINUE_TEXT
