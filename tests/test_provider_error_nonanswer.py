"""A provider's error sentence served as the assistant's answer.

MEASURED in a live sweep: /v1/responses (model "best", non-stream) returned
HTTP 200 whose content was an upstream relay's own message, "The API key used
for this request has reached ..." -- and the hub served it as the answer.
_NONANSWER_RE only knew a few exact relay strings.

Now: short, mostly-that-error replies are a non-answer on every protocol
(non-stream verdict and the stream peek), the failure is filed, a key/quota
limit cools the pair down, and the next hop answers. Legit answers that talk
about API keys are left alone. Fakes only, no network.
"""
import json

import pytest

import app as A

KEY_LIMIT = ("The API key used for this request has reached its usage limit. "
             "Please upgrade your plan or try again later.")


@pytest.mark.parametrize("text", [
    KEY_LIMIT,
    "The API key used for this request has reached the maximum number of requests.",
    "You exceeded your current quota, please check your plan and billing details.",
    "Insufficient credits.",
    "Rate limit reached for requests. Please try again later.",
    "Invalid API key provided.",
    "Your account has been suspended.",
    "The model `gpt-5-mini` is not available.",
    "Please try again later.",
])
def test_provider_error_replies_are_non_answers(text):
    assert A._is_upstream_nonanswer(text, prompt="What is 2 plus 2?") is True


@pytest.mark.parametrize("text", [
    "Set your API key in the OPENAI_API_KEY environment variable, then run the script.",
    "To authenticate, pass your API key in the Authorization header as a Bearer token.",
    "If your API key has expired, generate a new one in the dashboard.",
    "Store the API key in a .env file and never commit it.",
    "4",
    "Here is the fixed function:\n\ndef add(a, b):\n    return a + b",
])
def test_legit_answers_mentioning_api_keys_are_not_flagged(text):
    assert A._is_upstream_nonanswer(text, prompt="How do I call the API from Python?") is False


# MEASURED 2026-09-27, served as the answer on all three protocols: the opening
# sentence is only ~17% of the page, the rest is what-to-do lines.
POLLINATIONS_PAGE = (
    "The API key used for this request has reached its budget. Please [raise the key "
    "budget](https://enter.pollinations.ai/edit-key?id=xxxx&ref=agent_key_budget), then "
    "try again.\n\nTopping up the wallet does not raise this limit. If this isn’t "
    "your Pollinations account, contact whoever runs the app or service you’re using.")


@pytest.mark.parametrize("text", [
    POLLINATIONS_PAGE,
    "Sign up and repeat your request.",
    "Your API key has exceeded its quota. Please top up your wallet. Contact support if this persists.",
])
def test_multi_sentence_error_pages_are_non_answers(text):
    assert A._is_upstream_nonanswer(text, prompt="What is 1234 plus 1? Answer with only the number.") is True


@pytest.mark.parametrize("text", [
    # An answer that EXPLAINS a key error to the user, then continues with content.
    "Your API key has reached its limit. The script retries with backoff, logs each "
    "attempt, and writes the partial results to out.json so nothing is lost.",
    "The API key has expired. Rotate it in the console, then update KEY in config.py "
    "and restart the worker so the new value is loaded.",
])
def test_an_answer_that_opens_with_a_key_error_and_goes_on_is_kept(text):
    assert A._is_upstream_nonanswer(text, prompt="Why does my nightly job stop at 2am?") is False


def test_a_question_about_quotas_may_get_a_short_answer_about_them():
    assert A._is_upstream_nonanswer(
        "Your API key has reached its daily limit.",
        prompt="Why does my API key keep failing?") is False


def test_a_long_reply_is_never_flagged():
    long = KEY_LIMIT + " " + "Here is a full explanation of the design. " * 12
    assert A._is_upstream_nonanswer(long, prompt="explain the design") is False


def test_mostly_an_answer_is_not_flagged():
    text = ("The sum of the two numbers is 42, which you can verify by counting up "
            "from 17 twenty-five times. Try again later.")
    assert A._is_upstream_nonanswer(text, prompt="add 17 and 25") is False


# --------------------------------------------------------------------------- #
# Consequences: recorded, key/quota throttled, no 6h dead-mark
# --------------------------------------------------------------------------- #

@pytest.fixture
def ledger(monkeypatch):
    seen = {"outcome": [], "throttle": [], "dead": []}
    monkeypatch.setattr(A, "_record_outcome", lambda p, m, ok: seen["outcome"].append((p, m, ok)))
    monkeypatch.setattr(A, "_throttle_failed_hop",
                        lambda p, m, exc=None, secs=None: seen["throttle"].append((p, m, secs)))
    monkeypatch.setattr(A, "_mark_model_dead", lambda p, m, s: seen["dead"].append((p, m)))
    return seen


def test_a_key_limit_is_recorded_and_throttled(ledger):
    assert A._chat_json_nonanswer({"choices": [{"message": {"content": KEY_LIMIT}}]}) is True
    A._note_nonanswer("relay", "m")
    assert ledger["outcome"] == [("relay", "m", False)]
    assert ledger["throttle"] == [("relay", "m", A._PROVIDER_QUOTA_COOLDOWN)]
    assert ledger["dead"] == []


def test_a_plain_provider_error_is_recorded_not_throttled(ledger):
    assert A._is_upstream_nonanswer("Please try again later.", prompt="hi") is True
    A._note_nonanswer("relay", "m")
    assert ledger["outcome"] == [("relay", "m", False)] and not ledger["throttle"]
    assert ledger["dead"] == []


def test_the_old_relay_pages_still_dead_mark(ledger):
    assert A._is_upstream_nonanswer("No cake credits. Bake proof-of-work cakes at g4f.dev/chat")
    A._note_nonanswer("g4f", "m")
    assert ledger["dead"] == [("g4f", "m")]


def test_the_stream_peek_judges_the_whole_text_and_hands_back_the_kind(ledger):
    frames = [b'data: {"choices":[{"delta":{"content":"The API key used for this "}}]}',
              b'data: {"choices":[{"delta":{"content":"request has reached its limit."}}]}',
              b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
              b"data: [DONE]"]
    status, _buf = A._peek_until_content(iter(frames), 5)
    assert status == "nonanswer"
    A._note_nonanswer("relay", "m")
    assert ledger["throttle"] and not ledger["dead"]


# --------------------------------------------------------------------------- #
# End to end: every protocol, non-stream and stream -> the next hop answers
# --------------------------------------------------------------------------- #

class _Resp:
    def __init__(self, payload=None, chunks=None):
        self.status_code = 200
        self._payload = payload
        self._chunks = chunks
        self.headers = {}
        self.text = ""

    def json(self):
        return self._payload

    def close(self):
        pass

    def iter_content(self, chunk_size=None):
        return iter(self._chunks or ())

    def iter_lines(self, decode_unicode=False):
        return iter(self._chunks or ())


def _sse(text, framed):
    units = [("data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": text}}]})
              ).encode(),
             b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
             b"data: [DONE]"]
    return [u + b"\n\n" for u in units] if framed else units


@pytest.fixture
def hub(monkeypatch, ledger):
    for name in ("_record_chat_usage", "_save_perf_stats", "_act_pick", "_note_ttft",
                 "_record_stream_outcome", "_note_provider_timeout"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)
    monkeypatch.setattr(A, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(A, "_model_block_reason", lambda pid, m: None)
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("relay", "m1", "hard"))
    monkeypatch.setattr(A, "_resolve_model", lambda m: ("relay", "m1"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("relay", "m1"), ("good", "m2")])
    state = {"framed": False}

    def dispatch(pid, payload, stream):
        text = KEY_LIMIT if pid == "relay" else "4"
        if stream:
            return _Resp(chunks=_sse(text, state["framed"]))
        return _Resp(payload={"choices": [{"index": 0, "finish_reason": "stop",
                                           "message": {"role": "assistant", "content": text}}]})
    monkeypatch.setattr(A, "_dispatch_chat", dispatch)
    client = A.app.test_client()
    client.state = state
    return client


Q = "What is 2 plus 2?"


@pytest.mark.parametrize("stream", [False, True])
def test_chat_completions_moves_to_the_next_hop(hub, ledger, stream):
    hub.state["framed"] = True
    r = hub.post("/v1/chat/completions", json={
        "model": "auto", "stream": stream, "messages": [{"role": "user", "content": Q}]})
    body = r.get_data(as_text=True)
    assert "API key" not in body and "4" in body
    assert ("relay", "m1", False) in ledger["outcome"]
    assert ledger["throttle"] and ledger["throttle"][0][:2] == ("relay", "m1")


@pytest.mark.parametrize("stream", [False, True])
def test_responses_moves_to_the_next_hop(hub, ledger, stream):
    r = hub.post("/v1/responses", json={"model": "best", "stream": stream, "input": Q})
    body = r.get_data(as_text=True)
    assert "API key" not in body and "4" in body
    assert ("relay", "m1", False) in ledger["outcome"]


@pytest.mark.parametrize("stream", [False, True])
def test_messages_moves_to_the_next_hop(hub, ledger, stream):
    r = hub.post("/v1/messages", json={"model": "auto", "max_tokens": 64, "stream": stream,
                                        "messages": [{"role": "user", "content": Q}]})
    body = r.get_data(as_text=True)
    assert "API key" not in body and "4" in body
    assert ("relay", "m1", False) in ledger["outcome"]


def test_asking_about_the_key_keeps_the_short_answer(hub):
    r = hub.post("/v1/chat/completions", json={
        "model": "auto", "stream": False,
        "messages": [{"role": "user", "content": "Did my API key reach its quota?"}]})
    assert "reached its usage limit" in r.get_json()["choices"][0]["message"]["content"]
