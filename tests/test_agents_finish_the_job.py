r"""An agent that stops in the middle of its own todo list.

REPORTED 2026-09-10: "pourquoi les agents ne continuent pas et ils s'arretent
et ils continuent pas jusqu'au bout".

A CLI agent ends its turn when IT decides it is done, and the common failure is
that it decides that halfway: it writes a todo list, does the first item,
describes the next one, and stops. Nothing looks broken -- the process exits
zero, the reply is recorded, the work is half finished -- and the only fix was
a human noticing and typing "continue".

The hub already knows a turn ended and already has the machinery to send
another one, so it reads the reply and sends the nudge itself.

DELIBERATELY NARROW, because the opposite mistake is a CLI running forever on a
job that IS done: it stops at a hard limit, never continues a reply that asks a
QUESTION (being asked something is the agent doing its job, and answering is
not the hub's to do), never continues an interrupted turn, and leaves alone any
reply with no sign of unfinished work.
"""
import agentic_chat as AC
import pytest


# --------------------------------------------------------------------------- #
# Reading the agent's own words
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text", [
    "- [x] schema\n- [ ] api\n- [ ] docs",
    "* [ ] write the tests",
    "I created the schema. Next I will build the API.",
    "Now I'll wire up the routes.",
    "Done with step one. Then I will add the styles.",
])
def test_a_reply_that_says_there_is_more_to_do(text):
    assert AC.looks_unfinished(text)


@pytest.mark.parametrize("text", [
    "Done. Created both files.",
    "All done - nothing else to do.",
    "Everything is done and the tests pass.",
    "- [x] one\n- [x] two\nFinished.",
    "The feature is complete and ready to use.",
])
def test_a_reply_that_says_it_is_finished(text):
    assert not AC.looks_unfinished(text)


@pytest.mark.parametrize("text", [
    "Should I use Postgres or SQLite?",
    "- [ ] a\n- [ ] b\n\nWhich database do you want me to use?",
    "I can do it two ways. Which do you prefer?",
])
def test_a_question_is_never_continued(text):
    """Being asked something is the agent doing its job. A nudge would talk
    over the person who was asked."""
    assert not AC.looks_unfinished(text)


@pytest.mark.parametrize("text", [None, "", "   ", 42, [], {"a": 1}])
def test_junk_is_not_unfinished(text):
    assert not AC.looks_unfinished(text)


def test_finished_wins_over_a_trailing_intention():
    """"...I'll leave it there. All done." should not loop."""
    assert not AC.looks_unfinished("I'll stop there. All done.")


def test_an_unchecked_box_beats_everything():
    """The agent wrote the list itself and left items on it -- the strongest
    signal there is."""
    assert AC.looks_unfinished("- [ ] still to do\nI think that covers it.")


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #

def _fake_stream(replies):
    """A CLI whose successive turns end with the given texts."""
    seen = []

    def stream(session_id, text):
        seen.append(text)
        reply = replies[min(len(seen) - 1, len(replies) - 1)]
        yield {"event": "output", "text": "working"}
        yield {"event": "done", "text": reply}

    return stream, seen


def test_it_continues_a_turn_that_stopped_early(monkeypatch):
    stream, seen = _fake_stream(["- [ ] more to do", "All done."])
    monkeypatch.setattr(AC, "send_message_stream", stream)
    monkeypatch.setattr(AC, "get_session", lambda sid: {"cli": "opencode",
                                                        "project_dir": "."})
    list(AC.send_message_stream_durable("s1", "build it"))
    assert len(seen) == 2
    assert seen[0] == "build it"
    assert seen[1] == AC._CONTINUE_NUDGE


def test_it_stops_as_soon_as_the_work_is_done(monkeypatch):
    stream, seen = _fake_stream(["All done."])
    monkeypatch.setattr(AC, "send_message_stream", stream)
    monkeypatch.setattr(AC, "get_session", lambda sid: None)
    list(AC.send_message_stream_durable("s1", "build it"))
    assert seen == ["build it"]


def test_it_cannot_loop_forever(monkeypatch):
    """The opposite mistake -- a CLI running all night on a finished job -- is
    worse than stopping one step early."""
    stream, seen = _fake_stream(["- [ ] never ending"])
    monkeypatch.setattr(AC, "send_message_stream", stream)
    monkeypatch.setattr(AC, "get_session", lambda sid: None)
    list(AC.send_message_stream_durable("s1", "go"))
    assert len(seen) == AC._MAX_AUTO_CONTINUE + 1


def test_an_interrupted_turn_is_not_continued(monkeypatch):
    """Stop means stop."""
    def stream(session_id, text):
        yield {"event": "done", "text": "- [ ] half of it"}
        yield {"event": "stopped"}
    monkeypatch.setattr(AC, "send_message_stream", stream)
    monkeypatch.setattr(AC, "get_session", lambda sid: None)
    seen = list(AC.send_message_stream_durable("s1", "go"))
    assert sum(1 for e in seen if e.get("event") == "done") == 1


def test_an_errored_turn_is_not_continued(monkeypatch):
    def stream(session_id, text):
        yield {"event": "done", "text": "- [ ] half of it"}
        yield {"event": "error", "error": "the CLI died"}
    monkeypatch.setattr(AC, "send_message_stream", stream)
    monkeypatch.setattr(AC, "get_session", lambda sid: None)
    seen = list(AC.send_message_stream_durable("s1", "go"))
    assert sum(1 for e in seen if e.get("event") == "done") == 1


def test_the_reader_is_told_it_continued(monkeypatch):
    """Otherwise the transcript gains turns nobody asked for and nothing
    explains them."""
    stream, _seen = _fake_stream(["- [ ] more", "Done."])
    monkeypatch.setattr(AC, "send_message_stream", stream)
    monkeypatch.setattr(AC, "get_session", lambda sid: None)
    evs = list(AC.send_message_stream_durable("s1", "go"))
    notices = [e for e in evs if e.get("event") == "notice"]
    assert notices and "continuing" in notices[0]["text"]


def test_every_events_still_reaches_the_reader(monkeypatch):
    stream, _seen = _fake_stream(["- [ ] more", "Done."])
    monkeypatch.setattr(AC, "send_message_stream", stream)
    monkeypatch.setattr(AC, "get_session", lambda sid: None)
    evs = list(AC.send_message_stream_durable("s1", "go"))
    assert sum(1 for e in evs if e.get("event") == "output") == 2
    assert sum(1 for e in evs if e.get("event") == "done") == 2
