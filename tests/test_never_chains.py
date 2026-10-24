"""A pair that never chains a tool call is not an actor for tool turns.

MEASURED 2026-10-23 (turn-roles.jsonl, 14 days, 546 served tool turns): three
g4f poe-GLM backends (GLM-5.3-Flash-T x2, GLM-5.3-Flash) were served on 39 tool
turns and produced a tool call on NONE of them -- 100% text -- while every other
pair with >= 8 turns sat at 0-9% text. The roles layer accepts clean text as a
valid final answer, so each one kept being elected, the CLI saw prose with no
tool_calls and the loop stopped after every step (opencode `coding-swarm`
session ses_e9602c99: a "go" per step, for half an hour). A live probe agreed:
that pair gave 0 tool calls in 6/6 runs where glm/glm-4.5-flash gave the right
call 6/6.

Text is a legitimate ending, so only CONTINUATIONS count (tools already ran this
turn) and only a long unbroken run of text-only serves marks the pair; one call
clears it. The pair goes behind the ones that call (`_tool_turn_sick`), never
out of the chain.
"""
import pytest

import app as A

PAIR = ("g4f", "srv_x:poe/GLM-5.3-Flash-T")
OTHER = ("nvidia", "z-ai/glm-5.3-flash")
CALL = {"tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "bash", "arguments": "{}"}}]}
TEXT = {"content": "The command ran and printed AAA."}

# a continuation: the user spoke, a tool call ran, its result is the last message
MID_LOOP = [
    {"role": "user", "content": "run echo twice"},
    {"role": "assistant", "content": None, "tool_calls": CALL["tool_calls"]},
    {"role": "tool", "tool_call_id": "c1", "content": "AAA"},
]
# an opening turn: no tool has run yet
OPENING = [{"role": "user", "content": "run echo twice"}]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    A._chain_seen.clear()
    monkeypatch.setattr(A.config, "get_flag", lambda k, d=None: d)
    yield
    A._chain_seen.clear()


def _text_serves(n, messages=MID_LOOP, pair=PAIR):
    for _ in range(n):
        A._note_continuation_outcome(*pair, messages, TEXT)


def test_a_pair_that_only_ever_answers_text_mid_loop_is_flagged():
    _text_serves(A._NEVER_CHAIN_MIN)
    assert A._never_chains(*PAIR)
    assert A._tool_turn_sick(*PAIR)           # joins the measured-to-fail group


def test_one_short_of_the_threshold_is_not_enough():
    _text_serves(A._NEVER_CHAIN_MIN - 1)
    assert not A._never_chains(*PAIR)
    assert not A._tool_turn_sick(*PAIR)


def test_a_single_tool_call_clears_it():
    _text_serves(A._NEVER_CHAIN_MIN + 3)
    assert A._never_chains(*PAIR)
    A._note_continuation_outcome(*PAIR, MID_LOOP, CALL)
    assert not A._never_chains(*PAIR)
    _text_serves(A._NEVER_CHAIN_MIN - 1)       # the run starts over
    assert not A._never_chains(*PAIR)


def test_opening_turns_do_not_count():
    # A first turn that answers in text (a question, a greeting) says nothing
    # about whether the pair can chain.
    _text_serves(A._NEVER_CHAIN_MIN + 5, messages=OPENING)
    assert not A._never_chains(*PAIR)


def test_a_call_on_an_opening_turn_also_clears_it():
    # A healthy pair doing "call a tool, then answer" tasks shows text on every
    # continuation; its opening-turn calls are what prove it can call.
    _text_serves(A._NEVER_CHAIN_MIN + 2)
    assert A._never_chains(*PAIR)
    A._note_continuation_outcome(*PAIR, OPENING, CALL)
    assert not A._never_chains(*PAIR)


def test_a_pair_that_calls_is_untouched():
    for _ in range(20):
        A._note_continuation_outcome(*OTHER, MID_LOOP, CALL)
    A._note_continuation_outcome(*OTHER, MID_LOOP, TEXT)    # a loop ends in text
    assert not A._never_chains(*OTHER)
    assert not A._tool_turn_sick(*OTHER)


def test_the_mark_expires_so_a_repaired_relay_gets_another_try(monkeypatch):
    _text_serves(A._NEVER_CHAIN_MIN)
    assert A._never_chains(*PAIR)
    real = A.time.time
    monkeypatch.setattr(A.time, "time", lambda: real() + A._NEVER_CHAIN_TTL + 5)
    assert not A._never_chains(*PAIR)


def test_the_switch_off_files_and_flags_nothing(monkeypatch):
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=None: False if k == "tool_chain_watch" else d)
    _text_serves(A._NEVER_CHAIN_MIN + 4)
    assert not A._chain_seen
    assert not A._never_chains(*PAIR)


def test_garbage_in_never_raises():
    A._note_continuation_outcome(None, None, None, None)
    A._note_continuation_outcome(*PAIR, "not a list", "not a dict")
    assert not A._never_chains(None, None)


def test_the_default_run_is_four():
    # LOWERED 2026-10-24 (owner: swap a stalled model faster). Four text-only
    # continuations in a row is enough; three is not.
    assert A._NEVER_CHAIN_MIN == 4
    _text_serves(4)
    assert A._never_chains(*PAIR)
    A._chain_seen.clear()
    _text_serves(3)
    assert not A._never_chains(*PAIR)


def test_the_threshold_is_tunable_per_install(monkeypatch):
    # `tool_chain_watch_min` raises or lowers the run that marks a pair; a bad
    # value falls back to the default, and it never drops below 2.
    vals = {}
    monkeypatch.setattr(A.config, "get_setting",
                        lambda k, d=None: vals.get(k, d))
    vals["tool_chain_watch_min"] = 6
    _text_serves(5)
    assert not A._never_chains(*PAIR)          # 5 < 6
    _text_serves(1)
    assert A._never_chains(*PAIR)              # 6 >= 6
    A._chain_seen.clear()
    vals["tool_chain_watch_min"] = "garbage"
    _text_serves(A._NEVER_CHAIN_MIN)
    assert A._never_chains(*PAIR)              # bad value -> default
    A._chain_seen.clear()
    vals["tool_chain_watch_min"] = 1
    _text_serves(2)
    assert A._never_chains(*PAIR)              # clamped up to 2
