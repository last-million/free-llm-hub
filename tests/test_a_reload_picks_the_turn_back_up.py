r"""Reloading /agent mid-turn shows the turn, from its beginning, live.

REPORTED: "in /agent when I refresh the page I don't see running what he was
doing". A turn's events went from the CLI to whoever was reading the SSE
response and nowhere else, so a reload -- the natural thing to do when a page
looks stuck -- got a spinner over an empty panel until the turn ended.

Every turn now mirrors its events into a per-session ring buffer while it
runs; GET /api/agent/sessions/<id>/live replays that buffer and stays
attached until the turn ends; the page attaches to it on load, under the
transcript of the turns before.

Also from the same message: "if something should be opened from /agent it
should open in a new tab -- I clicked Models and it opened Settings in the
same page where I was working". It opens a new tab now, on that conversation.
"""
import threading
import time

import pytest

import agentic_chat as AC
import app as A


SRC = open("templates/index.html", encoding="utf-8").read()
APP = open("app.py", encoding="utf-8").read()


@pytest.fixture(autouse=True)
def _clean():
    with AC._LIVE_LOCK:
        AC._LIVE.clear()
    yield
    with AC._LIVE_LOCK:
        AC._LIVE.clear()


def _events(n, delay=0.0):
    for i in range(n):
        if delay:
            time.sleep(delay)
        yield {"event": "tool", "text": "step %d" % i}
    yield {"event": "done", "text": "the reply"}


# --------------------------------------------------------------------------- #
# The buffer
# --------------------------------------------------------------------------- #

def test_a_turn_is_live_while_it_runs_and_not_after():
    assert AC.turn_is_live("s1") is False
    out = []
    gen = AC.live_run("s1", _events(3, delay=0.05))
    out.append(next(gen))
    assert AC.turn_is_live("s1") is True
    out.extend(gen)
    assert len(out) == 4
    assert AC.turn_is_live("s1") is False


def test_the_reader_sees_what_the_turn_emitted():
    out = list(AC.live_run("s1", _events(3)))
    assert [e["event"] for e in out] == ["tool", "tool", "tool", "done"]


def test_a_follower_gets_the_beginning_it_missed():
    """The reload case: attach after the turn has been running a while."""
    started = threading.Event()

    def slow():
        for i in range(3):
            yield {"event": "tool", "text": "early %d" % i}
        started.set()
        time.sleep(0.3)
        yield {"event": "tool", "text": "late"}
        yield {"event": "done", "text": "the reply"}

    reader = threading.Thread(target=lambda: list(AC.live_run("s1", slow())))
    reader.start()
    assert started.wait(5)
    followed = list(AC.follow_turn("s1"))
    reader.join(5)
    texts = [e.get("text") for e in followed]
    assert texts == ["early 0", "early 1", "early 2", "late", "the reply"]


def test_a_follower_of_a_finished_turn_gets_it_all_once():
    list(AC.live_run("s1", _events(2)))
    assert [e.get("text") for e in AC.follow_turn("s1")] == ["step 0", "step 1", "the reply"]


def test_nothing_running_means_nothing_to_follow():
    assert list(AC.follow_turn("never-ran")) == []


def test_a_producer_that_blows_up_ends_the_turn_with_an_error():
    def bad():
        yield {"event": "tool", "text": "one"}
        raise RuntimeError("boom")
    out = list(AC.live_run("s1", bad()))
    assert out[-1]["event"] == "error" and "boom" in out[-1]["detail"]
    assert AC.turn_is_live("s1") is False


def test_the_buffer_is_bounded_and_says_so(monkeypatch):
    monkeypatch.setattr(AC, "_LIVE_KEEP", 5)
    list(AC.live_run("s1", _events(20)))
    followed = list(AC.follow_turn("s1"))
    assert followed[0]["event"] == "notice" and "not shown" in followed[0]["text"]
    assert len(followed) == 1 + 5


def test_the_next_turn_replaces_the_last():
    list(AC.live_run("s1", _events(1)))
    list(AC.live_run("s1", _events(2)))
    assert [e.get("text") for e in AC.follow_turn("s1")] == ["step 0", "step 1", "the reply"]


# --------------------------------------------------------------------------- #
# The CLI turn mirrors too
# --------------------------------------------------------------------------- #

def test_the_ordinary_turn_feeds_the_same_buffer():
    src = open("agentic_chat.py", encoding="utf-8").read()
    body = src[src.index("def send_message_stream_durable("):]
    body = body[:body.index("\ndef ", 10)]
    assert "_live_begin(session_id)" in body
    assert "_live_put(session_id, ev)" in body
    assert "_live_end(session_id)" in body


def test_a_reload_does_not_swap_out_a_session_between_processes(monkeypatch):
    """The resume route runs on every page load. Between a turn's CLI
    processes (a transient retry, an auto-continue) there is no live proc, and
    a multi-session turn is no proc of this session's -- the live buffer is
    what says the turn is on."""
    src = open("agentic_chat.py", encoding="utf-8").read()
    body = src[src.index("def resume_session("):]
    body = body[:body.index("\ndef ", 10)]
    assert "if turn_is_live(str(session_id)):" in body
    assert body.index("turn_is_live") < body.index("sid = start_session(")


# --------------------------------------------------------------------------- #
# The route
# --------------------------------------------------------------------------- #

def _hdr():
    return {"X-Free-LLM-Hub": "dashboard",
            "X-Free-LLM-Hub-Token": A.config.get_control_token() or ""}


def test_the_route_replays_then_ends(monkeypatch):
    monkeypatch.setattr(A, "_agent_gate", lambda: None)
    list(AC.live_run("s1", _events(2)))
    c = A.app.test_client()
    r = c.get("/api/agent/sessions/s1/live", headers=_hdr())
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert '"step 0"' in body and '"the reply"' in body
    assert body.rstrip().endswith("event: end\ndata: {}")


def test_the_route_says_idle_when_nothing_runs(monkeypatch):
    monkeypatch.setattr(A, "_agent_gate", lambda: None)
    c = A.app.test_client()
    body = c.get("/api/agent/sessions/nothing/live", headers=_hdr()).get_data(as_text=True)
    assert '{"event": "idle"}' in body


def test_the_resume_answer_counts_a_multi_run_as_running():
    body = APP[APP.index("def api_agent_resume_session("):]
    body = body[:body.index("\n@app.route")]
    assert "_multi_run_for(sid)" in body and "agentic_chat.turn_is_live(sid)" in body


# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #

def test_the_page_attaches_to_the_running_turn():
    assert "function attachLiveTurn(sid)" in SRC
    body = SRC[SRC.index("function attachLiveTurn(sid)"):]
    body = body[:body.index("/* How long to keep trying")]
    assert "'/live'" in body
    assert "openTurnView('Still working…')" in body


def test_the_running_turn_lands_under_the_transcript():
    body = SRC[SRC.index("function showReconnectedStillWorking(sid, turnCount)"):]
    body = body[:body.index("window.cxResumeAgentSession")]
    assert "/api/agent/history/" in body and "mountTranscript" in body
    assert "attachLiveTurn(sid)" in body
    assert "showReconnectedStillWorking(r.session_id, r.turn_count)" in SRC


def test_a_sent_message_and_a_reattached_turn_share_one_view():
    """Two copies is how the two paths drift apart."""
    assert SRC.count("function openTurnView(") == 1
    assert SRC.count("openTurnView(") >= 3      # the definition and both uses
    assert SRC.count("function pumpSse(") == 1
    assert SRC.count("pumpSse(resp, view.handle)") == 2


def test_a_turn_that_ended_before_the_attach_loads_the_transcript():
    body = SRC[SRC.index("function attachLiveTurn(sid)"):]
    body = body[:body.index("/* How long to keep trying")]
    assert "view.idle" in body and "loadFullHistory(sid)" in body


def test_models_opens_its_own_tab_on_this_conversation():
    body = SRC[SRC.index("function initAgentModelsLink()"):]
    body = body[:body.index("function initAgentMode()")]
    assert "window.open(url, '_blank', 'noopener')" in body
    assert "'/settings?scope=' + encodeURIComponent(sessionId)" in body
    assert "cxShow('sec-settings'" not in body, "same-page navigation is the bug"


def test_settings_reads_the_scope_off_its_url():
    body = SRC[SRC.index("new URLSearchParams(location.search).get('scope')"):]
    body = body[:body.index("initSdBulk();")]
    assert "_sdScope = want" in body


# --------------------------------------------------------------------------- #
# The thread carries the request with it
# --------------------------------------------------------------------------- #
# MEASURED 2026-09-12: the multi-session planner routes through the hub's own
# chain, which reads the request (`g`, headers); moved onto a thread it
# answered "" twice in seven seconds and the turn died at "could not turn that
# into phases".

def test_the_producer_runs_inside_the_context_it_was_given():
    seen = {}

    class Ctx:
        def __enter__(self):
            seen["entered_on"] = threading.current_thread().name
            return self

        def __exit__(self, *a):
            seen["exited"] = True
            return False

    def producer():
        seen["produced_on"] = threading.current_thread().name
        yield {"event": "done", "text": "x"}

    list(AC.live_run("s1", producer(), context=Ctx()))
    assert seen["entered_on"] == seen["produced_on"] != threading.main_thread().name
    assert seen["exited"] is True


def test_the_multi_route_hands_over_a_copy_of_its_request():
    stream = APP[APP.index("def api_agent_send_message_stream("):]
    stream = stream[:stream.index("\n@app.route")]
    assert "context=request_ctx._get_current_object().copy()" in stream


def test_the_planner_can_route_from_that_thread():
    """The real thing: a request context copied into live_run's thread lets
    the chain read the request there."""
    from flask import request as _request
    out = []

    def producer():
        out.append(_request.path)
        yield {"event": "done", "text": "x"}

    with A.app.test_request_context("/api/agent/sessions/s1/message/stream"):
        ctx = A.request_ctx._get_current_object().copy()
        list(AC.live_run("s1", producer(), context=ctx))
    assert out == ["/api/agent/sessions/s1/message/stream"]


def test_a_slow_follower_misses_nothing_when_the_buffer_wraps_twice(monkeypatch):
    """Found in review: the offset was re-based against the drops since the
    LAST read, which is only right when nothing dropped across two reads. A
    follower slower than the turn skipped events silently."""
    monkeypatch.setattr(AC, "_LIVE_KEEP", 5)
    turn = AC._live_begin("s1")
    for i in range(3):
        AC._live_put("s1", {"event": "tool", "text": "e%d" % i})
    gen = AC.follow_turn("s1", wait=0.01)
    got = [next(gen)["text"] for _ in range(3)]          # e0 e1 e2
    for i in range(3, 9):                                # six more: e3..e8, e0..e3 drop
        AC._live_put("s1", {"event": "tool", "text": "e%d" % i})
    got.append(next(gen)["text"])                         # first of the fresh batch
    for i in range(9, 13):                                # four more, another wrap
        AC._live_put("s1", {"event": "tool", "text": "e%d" % i})
    AC._live_end("s1")
    got += [e["text"] for e in gen]
    # e3 fell off before the reader got to it: it is reported, not skipped silently
    assert "1 lines of this turn were not shown." in got or "e3" in got
    seen = [g for g in got if g.startswith("e")]
    assert seen == sorted(seen, key=lambda t: int(t[1:])), "in order"
    assert seen[-1] == "e12" and "e8" in seen and "e9" in seen
    assert len(seen) == len(set(seen)), "nothing twice"
