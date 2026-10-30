"""A short streamed reply that ENDS on "Let me check ..." is not committed (2026-10-25).

MEASURED (codex session 01-38, Activity ids 18169-18174, nvidia/minimax-m3.1-flash,
model streams token by token): the turn's last model message was

    "There's a local agent with session handling. Let me check `hp_monitor.py`
     and the identity config."

and codex recorded task_complete -- the model announced work, made no tool call,
and the hub served it as the final answer. _peek_until_content judges the
peeked text once 600 frame BYTES have arrived (about 6 per-token frames, i.e.
"There's a local"), and only kept reading when that text already looked like an
announcement (_announce_pending). Nothing in "There's a local" does, so the
stream was committed and the sentence that ended the reply was never judged.
The same text in bigger chunks WAS caught, which hid the bug in the tests.

Fix: on a tool turn, keep reading while the reply is still short enough to end
as a bare announcement (_announce_possible), bounded by _ANNOUNCE_HOLD_S.
"""
import json

import app as A


TOOLS = [{"type": "function", "function": {"name": "exec_command",
                                           "parameters": {"type": "object"}}}]
ANNOUNCE = ("There's a local agent with session handling. Let me check "
            "`hp_monitor.py` and the identity config.")
FINAL = ("Both tests pass now. I fixed the loop bound in parser.py so the last "
         "token is kept, and nothing else needed to change.")


def _check(tools=True):
    return {"tools_offered": tools, "tools": TOOLS if tools else None,
            "last_prompt": "investigate the repo", "prompt_text": "investigate the repo",
            "relay": False}


def _cut(text, n):
    return [text[i:i + n] for i in range(0, len(text), n)]


def _frames(pieces, tool_call=False):
    out = [("data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": p}}]})
            ).encode() for p in pieces]
    if tool_call:
        out.append(b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
                   b'"id":"c1","function":{"name":"exec_command","arguments":"{}"}}]}}]}')
    out.append(b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}')
    out.append(b"data: [DONE]")
    return out


def _peek(pieces, check=None, tool_call=False):
    status, _buf = A._peek_until_content(iter(_frames(pieces, tool_call)), 5,
                                         check=check if check is not None else _check())
    return status, A._take_nonanswer_kind()


def test_a_token_by_token_announcement_is_caught():
    for size in (1, 2, 4, 7):
        assert _peek(_cut(ANNOUNCE, size)) == ("nonanswer", "announced"), size


def test_the_same_text_in_big_chunks_is_still_caught():
    assert _peek([ANNOUNCE]) == ("nonanswer", "announced")
    assert _peek([ANNOUNCE[:50], ANNOUNCE[50:]]) == ("nonanswer", "announced")


def test_a_real_short_final_answer_still_commits():
    assert _peek(_cut(FINAL, 4)) == ("content", None)


def test_a_long_answer_commits_without_waiting_for_its_end():
    assert A._announce_possible("word " * 120) is False        # past _ANNOUNCE_MAX_CHARS
    assert _peek(_cut("word " * 120, 4)) == ("content", None)


def test_a_tool_call_commits_at_once():
    assert _peek(_cut("Let me look.", 3), tool_call=True) == ("content", None)


def test_a_question_is_a_real_reply_not_an_announcement():
    q = "There are two config files here. Which one should I change?"
    assert _peek(_cut(q, 4)) == ("content", None)


def test_a_turn_without_tools_is_not_held():
    # plain chat may say what it is about to do (test_announced_but_did_nothing)
    assert _peek(_cut(ANNOUNCE, 4), check=_check(tools=False)) == ("content", None)


def test_the_hold_is_time_bounded(monkeypatch):
    monkeypatch.setattr(A, "_ANNOUNCE_HOLD_S", 0.0)
    # with no hold time left the old behaviour returns: committed, not judged
    assert _peek(_cut(ANNOUNCE, 4))[0] == "content"


def test_announce_possible_shape():
    assert A._announce_possible("There's a local") is True
    assert A._announce_possible("") is False
    assert A._announce_possible(None) is False
    assert A._announce_possible("Which one?") is False
    assert A._announce_possible("see ```code```") is False


# --------------------------------------------------------------------------- #
# Any verb: the second stopped codex turn (2026-10-25, 02:59)
# --------------------------------------------------------------------------- #

EXTRACT = ("That searched the whole repo. Let me extract just the scope arrays "
           "from the saved HTML files.")


def test_the_second_real_reply_is_an_announcement():
    # "extract" is in neither the build-verb list nor the read-verb list; the
    # shape (a future intent opening the last sentence) is what counts.
    assert A._ends_with_announcement(EXTRACT) is True
    assert A._announced_only(EXTRACT) is True               # the non-stream path too


def test_it_is_caught_streamed_token_by_token():
    for size in (1, 3, 5):
        assert _peek(_cut(EXTRACT, size)) == ("nonanswer", "announced"), size


def test_any_verb_after_a_future_intent():
    for text in ("Let me parse the JSON.", "I'll trace the call chain now.",
                 "Now I'll dig into the logs.", "Let me try to extract it.",
                 "I will go ahead and fetch the page.",
                 "Found the repo. I'm going to compare the two files."):
        assert A._ends_with_announcement(text) is True, text


def test_courtesy_waiting_and_asking_are_not_announcements():
    for text in ("Done. All tests pass.", "Let me know if you want more.",
                 "I'll be happy to help further.", "I'll wait for your reply.",
                 "I will not be able to do that.", "I'll keep that in mind.",
                 "I'll need your API key.",
                 "Both tests pass now. I fixed the loop bound in parser.py.",
                 "Found it. The bug is on line 12."):
        assert A._ends_with_announcement(text) is False, text


def test_an_intent_in_the_middle_is_not_the_end():
    # only the LAST sentence decides: the model said what it would do and then
    # reported having done it
    assert A._ends_with_announcement(
        "Let me check the file. Checked: the bug is on line 12, fixed.") is False

# --------------------------------------------------------------------------- #
# A slow relay's short announcement (MEASURED 2026-10-30 02:19 UTC, codex `coding`):
# g4f claude-opus-4.7 took 18-23 s for this 189-char reply; the 10 s hold ran out,
# the stream was committed unjudged and the codex turn ended on it.
# --------------------------------------------------------------------------- #
CODEX_0219 = ("You're right \u2014 I haven't acted yet. Let me check HackerOne program pages "
              "now for unauth-testable targets.\nI'll start with Box, Arc, Live Nation, and "
              "A&F to read their published scope lists.")


def _slow(pieces, gap):
    import time as _t
    for f in _frames(pieces):
        _t.sleep(gap)
        yield f


def test_the_measured_codex_reply_is_an_announcement():
    assert A._ends_with_announcement(CODEX_0219) is True


def test_the_hold_outlasts_a_slow_relay():
    assert A._ANNOUNCE_HOLD_S >= 30


def test_a_reply_dribbled_within_the_hold_is_judged(monkeypatch):
    monkeypatch.setattr(A, "_ANNOUNCE_HOLD_S", 3.0)
    status, _buf = A._peek_until_content(_slow(_cut(CODEX_0219, 20), 0.12), 5, check=_check())
    assert (status, A._take_nonanswer_kind()) == ("nonanswer", "announced")


def test_two_announcements_walk_on_and_the_third_is_served():
    with A.app.test_request_context():
        assert A._ANNOUNCE_RETRIES == 2
        assert A._announce_skip_spent("announced") is False      # 1st: next model
        assert A._announce_skip_spent("announced") is False      # 2nd: next model
        assert A._announce_skip_spent("announced") is True       # 3rd: served
        assert A._announce_skip_spent("empty") is False          # other kinds untouched
