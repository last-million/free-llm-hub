"""A gateway's key-budget notice is never an answer, on any hop, and its backend rests.

MEASURED 2026-10-25: a user's CLI showed Pollinations' "The API key used for this
request has reached its budget ... raise the key budget (https://enter.pollinations.ai/
edit-key?...)" as the model's reply. answer_check rejected that text only on hops the
hard-coded relay list names, and the hedge race judged its legs with no hop identity at
all, so any other hop served it. The canary also showed the same g4f backend failing
notice-by-notice on opus, sonnet, fable, astra and sol: each model cost a hop.

Fixes covered here (flag `relay_notice_rest`, default on, for the second):
  * the unmistakable Pollinations signatures are rejected on EVERY hop;
  * hedge legs are judged with their (pid, model);
  * a relay backend that served a notice is skipped by the walk for a while
    (fail-open when it is all the chain has).
"""
import json

import pytest

import answer_check as ac
import app as A

NOTICE = ("The API key used for this request has reached its budget. Please raise the "
          "key budget (https://enter.pollinations.ai/edit-key?id=XXXX&ref=agent_key_budget), "
          "then try again.\nTopping up the wallet does not raise this limit. If this isn't "
          "your Pollinations account, contact whoever runs the app or service you're using.")
QUOTA_ONLY = ("You have exceeded your current quota. Please check your plan and billing "
              "details, then try again.")
SERVER = "srv_abc123"
SID = "g4f|srv_abc123"   # _relay_server_id is pid-qualified
PAIRS = [("g4f", SERVER + ":claude-opus-5.5"), ("g4f", SERVER + ":claude-sonnet-5.5")]


@pytest.fixture(autouse=True)
def _clean():
    A._relay_notice_at.clear()
    yield
    A._relay_notice_at.clear()


# --- the signature is rejected on every hop ------------------------------------------

@pytest.mark.parametrize("relay", [False, True])
def test_the_pollinations_notice_is_junk_on_any_hop(relay):
    v = ac.inspect(NOTICE, relay=relay)
    assert not v["ok"] and "provider_notice" in v["reasons"]
    assert ac.reads_as_answer(NOTICE, relay=relay) is False


def test_a_direct_hop_keeps_every_other_phrase_it_always_passed():
    # Only the unmistakable signature widened: a generic quota sentence on a DIRECT
    # hop is still judged as before (a direct provider reports quota as HTTP errors).
    assert ac.inspect(QUOTA_ONLY, relay=False)["ok"]
    assert not ac.inspect(QUOTA_ONLY, relay=True)["ok"]


def test_an_answer_about_the_topic_still_passes():
    v = ac.inspect(NOTICE, relay=False,
                   prompt_text="why does pollinations say my api key budget is reached?")
    assert v["ok"]
    assert ac.inspect("Rotate the key in settings, then retry the call.", relay=False)["ok"]


def test_a_long_answer_that_quotes_the_notice_is_not_cut():
    long = ("Here is what happened in your logs. " * 40) + NOTICE
    assert len(long) > ac._NOTICE_MAX_CHARS
    assert ac.inspect(long, relay=False)["ok"]


# --- hedge legs are judged with their hop ---------------------------------------------

PAYLOAD = {"messages": [{"role": "user", "content": "hi there, what is two plus two?"}]}


def _sse(text, size=8):
    out = []
    for i in range(0, len(text), size):
        chunk = {"choices": [{"index": 0, "delta": {"content": text[i:i + size]}}]}
        out.append(b"data: " + json.dumps(chunk).encode() + b"\n\n")
    out.append(b"data: " + json.dumps(
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}).encode() + b"\n\n")
    out.append(b"data: [DONE]\n\n")
    return out


def _peek_leg(text):
    return (1, "peek", None, ("content", _sse(text), None))


def test_a_streamed_hedge_leg_serving_a_relay_quota_notice_is_not_a_winner():
    hop = ("g4f", SERVER + ":x")
    assert A._hedge_leg_verdict(_peek_leg(QUOTA_ONLY), PAYLOAD, hop=hop) == "salvaged"
    # without the hop identity the relay-only check could not run (the old gap)
    assert A._hedge_leg_verdict(_peek_leg(QUOTA_ONLY), PAYLOAD) == "ok"


def test_a_streamed_hedge_leg_serving_the_pollinations_notice_never_wins():
    assert A._hedge_leg_verdict(_peek_leg(NOTICE), PAYLOAD) == "salvaged"


def test_a_streamed_hedge_leg_with_a_real_answer_wins():
    leg = _peek_leg("Two plus two is four, and the sum is exactly that.")
    assert A._hedge_leg_verdict(leg, PAYLOAD, hop=("g4f", SERVER + ":x")) == "ok"


class _Resp:
    def __init__(self, text):
        self._d = {"choices": [{"message": {"role": "assistant", "content": text},
                                "finish_reason": "stop"}]}

    def json(self):
        return self._d


def test_a_non_streamed_hedge_leg_serving_the_notice_is_not_a_winner():
    for hop in (None, ("g4f", SERVER + ":x")):
        item = (1, "json", _Resp(NOTICE), None)
        assert A._hedge_leg_verdict(item, PAYLOAD, hop=hop) != "ok"


# --- a backend that served a notice is skipped, never the only option ------------------

def _walk(chain, **kw):
    with A.app.test_request_context("/v1/chat/completions"):
        return list(A._ChainClock(**kw).walk(list(chain)))


def test_a_noticed_backend_is_skipped_by_the_next_walk():
    A._note_relay_notice("g4f", SERVER + ":claude-opus-5.5", ["provider_notice"])
    chain = PAIRS + [("inception", "mercury-3")]
    assert _walk(chain, tools=False, est=900) == [("inception", "mercury-3")]
    assert _walk(chain, tools=True, est=900) == [("inception", "mercury-3")]


def test_the_notice_rests_the_whole_backend_not_one_model():
    A._note_relay_notice("g4f", SERVER + ":claude-opus-5.5", ["provider_notice"])
    assert A._relay_server_id("g4f", SERVER + ":gpt-6-astra") in A._relay_notice_servers()
    other = ("g4f", "srv_other:claude-opus-5.5")
    assert _walk([PAIRS[1], other], tools=False, est=900) == [other]


def test_it_is_fail_open_when_the_backend_is_all_that_is_left():
    A._note_relay_notice("g4f", SERVER + ":claude-opus-5.5", ["provider_notice"])
    assert _walk(PAIRS, tools=False, est=900) == PAIRS
    assert _walk(PAIRS, tools=True, est=900) == PAIRS[:A._TOOL_RELAY_MAX_HOPS]


def test_other_reasons_and_direct_providers_rest_nothing():
    A._note_relay_notice("g4f", SERVER + ":x", ["junk"])
    A._note_relay_notice("inception", "mercury-3", ["provider_notice"])
    assert not A._relay_notice_servers()


def test_the_rest_expires(monkeypatch):
    A._note_relay_notice("g4f", SERVER + ":x", ["provider_notice"])
    assert A._relay_notice_servers()
    real = A.time.monotonic
    monkeypatch.setattr(A.time, "monotonic", lambda: real() + A._RELAY_NOTICE_TTL + 1)
    assert not A._relay_notice_servers()


def test_the_non_stream_quota_nonanswer_rests_the_backend():
    with A.app.test_request_context("/v1/chat/completions"):
        A._note_nonanswer("g4f", SERVER + ":claude-opus-5.5", kind="provider_quota")
    assert SID in A._relay_notice_servers()


def test_the_flag_turns_the_rest_off(monkeypatch):
    monkeypatch.setattr(A.config, "get_flag",
                        lambda name, default=None:
                        False if name == "relay_notice_rest" else default)
    A._note_relay_notice("g4f", SERVER + ":x", ["provider_notice"])
    assert not A._relay_notice_servers()


# --- the gates that see the notice file it ---------------------------------------------

def test_the_non_stream_gate_files_the_backend():
    data = {"choices": [{"message": {"role": "assistant", "content": NOTICE},
                         "finish_reason": "stop"}]}
    with A.app.test_request_context("/v1/chat/completions"):
        gate = A._answer_gate(data, PAYLOAD, False, hop=("g4f", SERVER + ":claude-opus-5.5"))
    assert gate == "junk"
    assert SID in A._relay_notice_servers()


def test_the_stream_gate_cuts_the_notice_and_files_the_backend():
    with A.app.test_request_context("/v1/chat/completions"):
        g = A._StreamAnswerGate(iter(_sse(NOTICE)), mode="bytes",
                                hop_pid="g4f", hop_model=SERVER + ":claude-opus-5.5",
                                prompt_text="what is two plus two?",
                                last_prompt="what is two plus two?", tools_offered=False)
        body = b"".join(g)
    assert g.cut and b"enter.pollinations.ai" not in body
    assert SID in A._relay_notice_servers()
