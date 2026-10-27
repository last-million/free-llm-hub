"""A service notice is not an answer, even when the task brief mentions quotas or API keys.

MEASURED 2026-10-27 00:19 (opencode `coding-max`, a long hand-off brief): the Pollinations
"The API key used for this request has reached its budget ..." notice was served as the model's
reply and the agent's turn ended on it. The notice check had a topic exemption -- skipped when the
user's last message mentions budget / quota / api key / rate limit / credits -- meant for a short
QUESTION about quotas. On an agent turn the last user message is the long original brief, and any
security or web task says "rate limit" or "api keys" somewhere, so the check switched itself off
for the whole task. Replaying the hub's gate on the 13 recorded assistant replies that were the
notice: the gate as committed accepted 7, every one with an exempting word (rate limit / api keys /
credit) in a 2.3K-46K char brief; the fixed gate accepts none.

Now the exemption needs a SHORT instruction (a question, not a brief); the Pollinations signature
is exempt only when that short instruction names Pollinations and does not itself contain the
notice; the gateway's opening sentence is a signature too (so a stream is held before it commits);
and an assistant turn that already holds the notice is replaced per hop so a model does not copy it.
"""
import json

import pytest

import answer_check as ac
import app as A

NOTICE = ("The API key used for this request has reached its budget. Please [raise the key "
          "budget](https://enter.pollinations.ai/edit-key?id=FAKEID0000&ref=agent_key_budget), "
          "then try again.\n\nTopping up the wallet does not raise this limit. If this isn't your "
          "Pollinations account, contact whoever runs the app or service you're using.")
QUOTA_ONLY = ("You have exceeded your current quota. Please check your plan and billing "
              "details, then try again.")
OPENER = "The API key used for this request has reached its budget. Please raise the key budget"
TOOLS = [{"type": "function", "function": {"name": "bash", "parameters": {"type": "object"}}}]
SERVER = "srv_abc123"
RELAY = ("g4f", SERVER + ":claude-opus-5.5")
DIRECT = ("pollinations", "openai-fast")
WORDS = ["rate limit", "rate limiting", "API keys", "api key", "credits", "quota", "budget",
         "billing", "wallet"]


def brief(word, size=3000):
    """A synthetic task brief (NOT anyone's real text) that mentions `word` once."""
    head = ("Task: audit the web app in this folder and report what is wrong. Scope: the public "
            "pages only. Done so far: the login flow was reviewed. Pending: check the scripts for "
            "anything that would hurt a user. Note for later: if the server returns a %s message, "
            "back off and retry slowly. " % word)
    filler = "Step %d: look at file number %d and write down what you saw. "
    out = head
    i = 0
    while len(out) < size:
        out += filler % (i, i * 3)
        i += 1
    return out


@pytest.fixture(autouse=True)
def _clean():
    A._relay_notice_at.clear()
    yield
    A._relay_notice_at.clear()


# --- the check itself ---------------------------------------------------------------------

@pytest.mark.parametrize("word", WORDS)
@pytest.mark.parametrize("relay", [True, False])
def test_the_pollinations_notice_is_junk_whatever_a_long_brief_mentions(word, relay):
    v = ac.inspect(NOTICE, last_prompt=brief(word), tools_offered=True, relay=relay)
    assert not v["ok"] and "provider_notice" in v["reasons"], (word, relay)
    assert v["salvage"] is None
    assert ac.reads_as_answer(NOTICE, last_prompt=brief(word), relay=relay) is False


@pytest.mark.parametrize("word", ["rate limit", "API keys", "credits"])
def test_a_generic_quota_notice_from_a_relay_is_junk_whatever_a_long_brief_mentions(word):
    v = ac.inspect(QUOTA_ONLY, last_prompt=brief(word), tools_offered=True, relay=True)
    assert not v["ok"] and "provider_notice" in v["reasons"], word


def test_the_exemption_needs_a_short_instruction():
    short = "Why does it say we exceeded the quota? " + "x" * 40
    assert ac._NOTICE_TOPIC_MAX_PROMPT >= len(short)
    assert ac.inspect(QUOTA_ONLY, last_prompt=short, relay=True)["ok"]
    near = "How do I handle the rate limit? " + "pad " * 130
    assert len(near) < ac._NOTICE_TOPIC_MAX_PROMPT
    assert ac.inspect(QUOTA_ONLY, last_prompt=near, relay=True)["ok"]
    over = "How do I handle the rate limit? " + "pad " * 200
    assert len(over) > ac._NOTICE_TOPIC_MAX_PROMPT
    assert not ac.inspect(QUOTA_ONLY, last_prompt=over, relay=True)["ok"]


def test_a_short_question_that_names_pollinations_still_gets_its_answer():
    for q in ("How do I raise my API key budget on Pollinations?",
              "why does pollinations say my api key budget is reached?"):
        for relay in (True, False):
            assert ac.inspect(NOTICE, last_prompt=q, relay=relay)["ok"], (q, relay)


def test_a_short_topic_question_that_does_not_name_pollinations_does_not_excuse_the_notice():
    # the notice text is the gateway's boilerplate, not an answer to "how do I raise my budget"
    for relay in (True, False):
        assert not ac.inspect(NOTICE, last_prompt="How do I raise my API key budget?",
                              relay=relay)["ok"]


def test_a_prompt_that_pastes_the_notice_does_not_excuse_repeating_it():
    pasted = "Pollinations says this, fix it: " + NOTICE
    assert len(pasted) <= ac._NOTICE_TOPIC_MAX_PROMPT
    for relay in (True, False):
        assert not ac.inspect(NOTICE, last_prompt=pasted, relay=relay)["ok"]


def test_the_gateway_opening_sentence_alone_is_a_signature():
    for relay in (True, False):
        assert not ac.inspect(OPENER, relay=relay)["ok"]
        assert ac.reads_as_answer(OPENER + ", then try again.", relay=relay) is False
    assert ac.inspect("The API key you gave is stored in .env and is read at start.", relay=False)["ok"]


def test_real_answers_and_long_replies_are_untouched():
    long = ("Here is what happened in your logs. " * 40) + NOTICE
    assert len(long) > ac._NOTICE_MAX_CHARS
    assert ac.inspect(long, last_prompt=brief("quota"), relay=False)["ok"]
    assert ac.inspect("Done: I added the retry wrapper and the tests pass.",
                      last_prompt=brief("rate limit"), tools_offered=True, relay=True)["ok"]


# --- the hub's real gates ------------------------------------------------------------------

def _payload(word="rate limit"):
    return {"tools": TOOLS, "messages": [{"role": "user", "content": brief(word)}]}


def _data(text):
    return {"choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}]}


@pytest.mark.parametrize("hop", [RELAY, DIRECT])
def test_the_non_stream_gate_rejects_the_notice_for_a_long_brief(hop):
    with A.app.test_request_context("/v1/chat/completions"):
        assert A._answer_gate(_data(NOTICE), _payload(), True, hop=hop) == "junk"


def test_the_non_stream_gate_rests_the_relay_backend():
    with A.app.test_request_context("/v1/chat/completions"):
        A._answer_gate(_data(NOTICE), _payload(), True, hop=RELAY)
    assert "g4f|" + SERVER in A._relay_notice_servers()


def _frames(text, size):
    out = [("data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": text[i:i + size]}}]})
            ).encode() for i in range(0, len(text), size)]
    out.append(b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}')
    out.append(b"data: [DONE]")
    return out


@pytest.mark.parametrize("size", [1, 8, 400])
@pytest.mark.parametrize("pid", ["g4f", "pollinations", "groq"])
def test_the_pre_commit_peek_holds_the_notice_back(pid, size):
    check = A._peek_check(_payload(), True, pid=pid, model="m")
    status, _buf = A._peek_until_content(iter(_frames(NOTICE, size)), 5, check=check)
    assert status != "content", (pid, size)


def test_the_notice_hold_is_bounded_and_does_not_delay_ordinary_replies():
    brief_text = brief("rate limit")
    # still a possible notice: the gateway sentence is not finished yet
    assert ac.is_provider_notice_possible("The API k", brief_text, relay=False)
    assert ac.is_provider_notice_possible("Topping up", brief_text, relay=False)
    # diverged: a direct hop is released at once, a relay hop only after the looser openers fail
    assert not ac.is_provider_notice_possible("The API key is read from .env", brief_text, relay=False)
    assert not ac.is_provider_notice_possible("Fixed the failing test in parser.py", brief_text)
    # a short question that names the gateway excuses the notice, so nothing is held for it
    assert not ac.is_provider_notice_possible("The API k", "Why does Pollinations say that?", relay=False)
    # never held for long
    assert not ac.is_provider_notice_possible("The API key " + "x" * 300, brief_text)


@pytest.mark.parametrize("pid", ["g4f", "pollinations"])
def test_a_reply_that_merely_starts_like_a_notice_is_committed_once_it_diverges(pid):
    check = A._peek_check(_payload(), True, pid=pid, model="m")
    text = ("The API key is read from the .env file at start-up and never logged; I checked "
            "config.py and the tests that cover it, and they pass.")
    status, _buf = A._peek_until_content(iter(_frames(text, 1)), 5, check=check)
    assert status == "content"


def test_the_pre_commit_peek_still_commits_a_real_answer():
    check = A._peek_check(_payload(), True, pid="g4f", model="m")
    text = ("The scan finished: three pages load a script from an unknown host, and I listed "
            "each one in REPORT.md with the line that includes it.")
    status, _buf = A._peek_until_content(iter(_frames(text, 8)), 5, check=check)
    assert status == "content"


def _sse(text, size=8):
    out = [b"data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": text[i:i + size]}}]}
                                  ).encode() + b"\n\n" for i in range(0, len(text), size)]
    out.append(b"data: " + json.dumps(
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}).encode() + b"\n\n")
    out.append(b"data: [DONE]\n\n")
    return out


@pytest.mark.parametrize("pid", ["g4f", "pollinations"])
def test_the_committed_stream_gate_cuts_the_notice(pid):
    brief_text = brief("rate limit")
    with A.app.test_request_context("/v1/chat/completions"):
        g = A._StreamAnswerGate(iter(_sse(NOTICE)), mode="bytes", hop_pid=pid,
                                hop_model=SERVER + ":claude-opus-5.5",
                                prompt_text=brief_text, last_prompt=brief_text, tools_offered=True)
        body = b"".join(g)
    assert g.cut and b"enter.pollinations.ai" not in body


# --- a notice already in the thread is not copied -------------------------------------------

def _thread(last_user, notice=NOTICE):
    return [{"role": "system", "content": "You are a coding agent."},
            {"role": "user", "content": last_user},
            {"role": "assistant", "content": notice},
            {"role": "user", "content": "continue"}]


def test_a_served_notice_in_the_thread_is_replaced_for_the_hop():
    msgs = _thread(brief("rate limit"))
    snapshot = json.dumps(msgs)
    with A.app.test_request_context("/v1/chat/completions"):
        out = A._scrub_empty_claims(msgs)
    assert json.dumps(msgs) == snapshot                       # the CLI's list is never mutated
    assert out[2]["content"] == A._NOTICE_TURN_NOTE and out[2]["role"] == "assistant"
    assert [m["role"] for m in out] == [m["role"] for m in msgs]
    assert out[1] is msgs[1] and out[3] is msgs[3]


def test_a_notice_free_thread_comes_back_unchanged():
    msgs = _thread(brief("quota"), notice="Done: the report is in REPORT.md.")
    with A.app.test_request_context("/v1/chat/completions"):
        assert A._scrub_empty_claims(msgs) is msgs


def test_an_assistant_turn_with_tool_calls_is_never_scrubbed():
    msgs = _thread(brief("quota"))
    msgs[2] = {"role": "assistant", "content": NOTICE,
               "tool_calls": [{"id": "c1", "type": "function",
                               "function": {"name": "bash", "arguments": "{}"}}]}
    with A.app.test_request_context("/v1/chat/completions"):
        assert A._scrub_empty_claims(msgs) is msgs


def test_a_short_question_about_pollinations_keeps_its_own_answer():
    msgs = _thread("Why does Pollinations say my API key budget is reached?")
    with A.app.test_request_context("/v1/chat/completions"):
        assert A._scrub_empty_claims(msgs) is msgs


def test_the_scrub_flag_turns_it_off(monkeypatch):
    msgs = _thread(brief("quota"))
    monkeypatch.setattr(A.config, "get_flag",
                        lambda name, default=None:
                        False if name == "scrub_empty_claim_turns" else default)
    with A.app.test_request_context("/v1/chat/completions"):
        assert A._scrub_empty_claims(msgs) is msgs
