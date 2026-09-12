r"""A message typed while the agent is on a turn waits, and goes out next.

ASKED FOR: "in /agent, input messages and send them to the queue while he
works". Send was disabled for the whole turn, so the next instruction had to
be held in your head until the agent came back -- on a build turn, minutes.

Now Send stays enabled and reads "Queue" while a turn runs; Enter queues too.
The queue shows under the input, each message removable or click-to-edit,
and when the turn FINISHES the next message goes out by itself, then the
next. After Stop or an error it waits with a Send next button instead: the
person stopped the agent for a reason. Kept per conversation in the browser,
so a reload mid-turn (which reattaches to the running turn) keeps the queue
and sends it when that turn ends.
"""
SRC = open("templates/index.html", encoding="utf-8").read()


def _agent():
    body = SRC[SRC.index("function initAgent(){"):]
    return body[:body.index("\n  function fmtDuration(ms){")]


def _fn(name, end="\n    }"):
    body = _agent()
    i = body.index(name)
    return body[i:body.index(end, i)]


# --------------------------------------------------------------------------- #
# Send does not refuse while a turn runs
# --------------------------------------------------------------------------- #

def test_send_stays_enabled_and_says_queue_while_busy():
    fn = _fn("function setBusy(on){")
    assert "sendBtn.disabled = false;" in fn
    assert "sendBtn.textContent = on ? 'Queue' : 'Send';" in fn
    assert "sendBtn.disabled = on;" not in fn


def test_a_message_sent_while_busy_is_queued_not_dropped():
    fn = _fn("function doSend(){")
    assert "if (turnBusy){" in fn and "queueAdd(text);" in fn
    assert "if (sendBtn.disabled) return;" not in fn, "the old refusal"


def test_enter_goes_through_the_same_path():
    assert "if (e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); doSend(); }" in _agent()


# --------------------------------------------------------------------------- #
# It goes out on its own, in order -- but not after a stop
# --------------------------------------------------------------------------- #

def test_the_next_message_goes_out_when_a_turn_finishes():
    fn = _fn("function queueDrain(){")
    assert "_queue.shift()" in fn and "doSend();" in fn
    send = _fn("function doSend(){")
    assert "queueAfterTurn(failed, view.stopped);" in send


def test_a_reattached_turn_drains_it_too():
    fn = _fn("function attachLiveTurn(sid){")
    assert "queueAfterTurn(failed, view.stopped);" in fn


def test_after_stop_or_error_it_waits_for_a_person():
    fn = _fn("function queueAfterTurn(failed, stopped){")
    assert "if (failed || stopped){" in fn
    assert "_queuePaused = !!_queue.length;" in fn
    render = _fn("function queueRender(){")
    assert "'Send next'" in render and "paused after the last turn stopped" in render


def test_a_stopped_turn_is_marked_as_one():
    fn = _fn("function openTurnView(label){")
    assert "view.stopped = true;" in fn
    assert "stopped: false" in fn


def test_nothing_is_sent_into_a_turn_already_running():
    fn = _fn("function queueDrain(){")
    assert "if (turnBusy || _queuePaused || !sessionId || !_queue.length) return;" in fn


# --------------------------------------------------------------------------- #
# Editable, removable, and it follows the conversation
# --------------------------------------------------------------------------- #

def test_a_queued_message_can_be_edited_or_removed():
    fn = _fn("function queueRender(){")
    assert "_queue.splice(i, 1);" in fn
    assert "textEl.value = text;" in fn, "click puts it back in the box"
    assert "aria-label', 'Remove from queue'" in fn


def test_it_is_kept_per_conversation_in_this_browser():
    assert "'flh.queue.' + sessionId" in _fn("function queueKey(){")
    assert "queueLoad();" in _fn("function showSessionState(cli, dir){")


def test_it_is_dropped_when_the_conversation_ends():
    fn = _fn("function showStartState(){")
    assert fn.index("queueForget(sessionId);") < fn.index("sessionId = null;")


def test_the_strip_is_in_the_markup():
    assert 'id="agent-queue"' in SRC
    assert ".queue-item{" in SRC
