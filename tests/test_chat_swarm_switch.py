"""A per-conversation swarm switch, flippable mid-conversation.

USER REQUEST 2026-08-30: "in each conversation I want a toggle enabling or
disabling swarm agents, and in the same conversation we can enable it or
disable it and continue normal."

The switch overrides the model picker rather than replacing it: the model you
chose is still selected underneath, so turning the switch off returns to it.
Read fresh on every send, so the NEXT message obeys whatever it says now and
the conversation history carries over either way.
"""
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _html():
    return io.open(os.path.join(ROOT, "templates", "index.html"),
                   encoding="utf-8").read()


def test_the_switch_exists_in_the_chat_bar():
    html = _html()
    assert 'id="chat-swarm-switch"' in html
    i = html.find('id="chat-swarm-switch"')
    bar = html[max(0, i - 1200):i]
    assert 'id="chat-model"' in bar, "the switch must live in the chat bar"


def test_it_is_read_on_every_send_not_captured_once():
    """Flipping it mid-conversation has to take effect on the NEXT message."""
    html = _html()
    assert "var swarmSw = $('#chat-swarm-switch');" in html
    assert "var swarmOn = !!(swarmSw && swarmSw.checked);" in html
    i = html.find("var swarmOn = !!(swarmSw && swarmSw.checked);")
    j = html.find("function sendChat(retryOf){")
    assert j != -1 and j < i, "the read must happen inside sendChat, per send"


def test_switching_on_routes_to_the_crew_pipeline():
    assert "if (swarmOn && !retryOf) model = 'crew';" in _html()


def test_the_picker_is_overridden_not_overwritten():
    """`model` is a local; modelSel.value is untouched, so switching the toggle
    off returns to whatever the user actually picked."""
    html = _html()
    i = html.find("if (swarmOn && !retryOf) model = 'crew';")
    assert i != -1
    window = html[i:i + 400]
    assert "modelSel.value = 'crew'" not in window


def test_the_project_gate_does_not_nag_when_the_switch_is_already_on():
    """The gate asks crew-vs-plain. With the switch on, that is answered."""
    html = _html()
    i = html.find("looksLikeFullProject(content)")
    assert i != -1
    assert "!swarmOn" in html[max(0, i - 400):i]


def test_turning_it_on_says_what_it_costs():
    """A switch that silently makes the next reply 20x slower is a trap."""
    html = _html()
    assert "function initSwarmSwitch(){" in html
    assert "initSwarmSwitch();" in html
    i = html.find("function initSwarmSwitch(){")
    body = html[i:i + 1600]
    assert "Minutes, not seconds" in body
    assert "Turn it off any time" in body


def test_a_retry_never_silently_becomes_a_swarm_run():
    """'Retry with a different model' asks another MODEL for the same answer.
    Turning it into a multi-minute pipeline run would be a different thing than
    the button says."""
    assert "if (swarmOn && !retryOf) model = 'crew';" in _html()
