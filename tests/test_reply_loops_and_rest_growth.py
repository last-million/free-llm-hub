"""Three fixes of 2026-11-11, all measured on one OpenCode session:

* a reply that repeats a whole BLOCK (never the same line twice in a row) is a loop;
* typed "<task N>" calls that come back are cut at the repeat, not held to the end;
* a provider whose test request fails again rests longer each time.

Hermetic: synthetic text, a fake clock, no network.
"""
import json

import pytest

import answer_check as ac
import app
import tool_rescue as tr

TOOLS = [{"type": "function", "function": {"name": "task", "parameters": {
    "type": "object",
    "properties": {"description": {"type": "string"}, "prompt": {"type": "string"},
                   "subagent_type": {"type": "string"}},
    "required": ["description", "prompt", "subagent_type"]}}}]

PARA = ("The migration has to keep the old column until every reader has moved to the new one.\n"
        "After that the backfill job can run in batches of a thousand rows without a lock.\n"
        "Only then is the old column dropped, in a release of its own.\n")


def _varied(n):
    return "".join("Step %d covers a different part of the rollout and has its own owner and date.\n" % i
                   for i in range(n))


# --------------------------------------------------------------------------- block loops

def test_a_block_that_keeps_coming_back_is_a_loop():
    text = _varied(5) + "".join(PARA + "---\n" for _ in range(8))
    r = ac.inspect(text)
    assert r["ok"] is False and "repetition" in r["reasons"]
    assert r["salvage"] and "Step 4" in r["salvage"]


def test_a_very_long_line_needs_only_four_copies():
    long_line = "This single sentence is long enough to be a paragraph of its own, " * 4
    text = "".join("%s\nsomething different number %d follows here.\n" % (long_line.strip(), i)
                   for i in range(4))
    assert ac.inspect(text)["ok"] is False
    assert ac.inspect("".join("%s\nsomething different number %d.\n" % (long_line.strip(), i)
                              for i in range(3)))["ok"] is True


@pytest.mark.parametrize("text", [
    _varied(40),                                                     # long, nothing repeats
    PARA * 2 + _varied(6),                                           # said twice: content
    "```\n" + ("    result = compute_the_value(first_argument, second_argument, third)\n" * 9)
    + "```\nDone.",                                                  # code repeats
    "".join("| a fairly long table cell number one | another long table cell here | %d |\n" % i
            for i in range(30)),                                     # a table
    "".join("- [ ] Review the migration plan with the owner before the release window opens\n"
            "note %d\n" % i for i in range(8)),                      # a checklist item 8 times
])
def test_what_repeats_legitimately_is_left_alone(text):
    assert ac.inspect(text)["ok"] is True


def test_the_stream_gate_cuts_it_while_it_streams():
    text = _varied(5) + "".join(PARA + "---\n" for _ in range(10))
    state, cut, seen = {}, None, 0
    for i in range(0, len(text), 25):
        seen = i + 25
        cut = ac.inspect_tail(text[:seen], state=state)
        if cut is not None:
            break
    assert cut is not None and seen < len(text) * 0.75
    assert text[cut:].startswith("The migration has to keep")


def test_the_flag_off_is_the_old_behaviour(monkeypatch):
    monkeypatch.setattr(ac, "_flag", lambda name, default=True: False if name == "scattered_loop_cut" else default)
    text = _varied(5) + "".join(PARA + "---\n" for _ in range(8))
    assert ac.inspect(text)["ok"] is True
    assert ac.inspect_tail(text, state={}) is None


# --------------------------------------------------------------------------- typed calls that repeat

def _block(i, what):
    return ('<task %d>\ndescription: "%s"\nprompt: "Do the %s part of the work and report back."\n'
            'subagent_type: "general"\n\n' % (i, what, what))


def test_repeated_typed_calls_are_cut_at_the_repeat():
    once = _block(1, "api") + _block(2, "tests") + _block(3, "docs") + "Stop. End of turn. Done.\n\n---\n\n"
    text = "Three helpers.\n\n" + once * 12
    read = [0]

    def frames():
        for i in range(0, len(text), 5):
            read[0] = i + 5
            yield b"data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": text[i:i + 5]},
                                                       "finish_reason": None}]}).encode()
        yield b"data: " + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}).encode()
        yield b"data: [DONE]"

    calls, shown, fin, done = [], "", None, 0
    for u in tr.rescue_stream(frames(), TOOLS):
        s = u.decode()[6:]
        if s.strip() == "[DONE]":
            done += 1
            continue
        o = json.loads(s)
        assert "error" not in o
        d = o["choices"][0]["delta"]
        shown += d.get("content") or ""
        calls += [json.loads(tc["function"]["arguments"])["description"] for tc in d.get("tool_calls") or []]
        fin = o["choices"][0]["finish_reason"] or fin
    assert calls == ["api", "tests", "docs"]
    assert fin == "tool_calls" and done == 1
    assert read[0] < len(text) / 2                 # it stopped reading the loop
    assert shown.strip() == "Three helpers."


def test_the_stop_note_goes_even_with_a_separator_after_it():
    assert tr._drop_stop_note("Plan.\n\nStop. End of turn. Done.\n\n---\n") == "Plan."
    assert tr._drop_stop_note("Plan.\n\n---\n") == "Plan.\n\n---\n"


# --------------------------------------------------------------------------- the rest grows

def test_each_failed_test_doubles_the_rest_up_to_the_cap():
    base = app._PROVIDER_STALL_REST_S
    assert [app._psr_rest_seconds(n) for n in (1, 2, 3, 4, 5, 30)] == [
        base, base * 2, base * 4, base * 8, app._PROVIDER_STALL_REST_CAP, app._PROVIDER_STALL_REST_CAP]


def test_the_rest_growth_flag_off_is_flat(monkeypatch):
    monkeypatch.setattr(app, "_psr_escalates_on", lambda: False)
    assert app._psr_rest_seconds(6) == app._PROVIDER_STALL_REST_S


def test_a_stall_on_probation_starts_a_longer_rest(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(app.time, "time", lambda: now[0])
    monkeypatch.setattr(app, "_psr_on", lambda: True)
    monkeypatch.setattr(app, "_is_relay_pid", lambda pid: False)
    monkeypatch.setattr(app, "_provider_stalled_models", lambda pid, t: {"a", "b"})
    with app._provider_rest_lock:
        app._provider_rest.clear()
    try:
        app._psr_note_stall("slowhost")
        first = app._provider_rest["slowhost"]["until"] - now[0]
        now[0] += first + 60                      # the rest ran out: on probation
        app._psr_note_stall("slowhost")           # its test request stalled
        second = app._provider_rest["slowhost"]["until"] - now[0]
        now[0] += second + 60
        app._psr_note_stall("slowhost")
        third = app._provider_rest["slowhost"]["until"] - now[0]
        assert (first, second, third) == (1800.0, 3600.0, 7200.0)
        app._psr_note_answer("slowhost")          # one answer lifts it whatever the level
        assert "slowhost" not in app._provider_rest
    finally:
        with app._provider_rest_lock:
            app._provider_rest.clear()
