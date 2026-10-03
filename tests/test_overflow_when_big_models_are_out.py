"""A very long conversation, and the only models that could hold it are out.

MEASURED 2026-10-03 (hub.log), an OpenCode turn answered 503 over and over:

    CHAT-503 stream=True tools=True est=325869 errors=[kilocode: _ContextOverflow;
    groq: _ContextOverflow; google: HTTP 429; google: HTTP 429; kilocode: HTTP 400;
    nvidia: _ContextOverflow] last_hard=400/kilocode

Every model whose window could hold ~326K tokens (google gemini flash, 1M) had
spent its free DAILY quota, the rest overflowed, and one refused the request
outright. The native "context too long" reply was withheld because some hops
had failed "for another reason" -- so the CLI retried the identical oversized
turn into the same 503 forever, instead of compacting its own history.

The rule now (_ctx_overflow_reply / _ctx_others_cannot_serve): at least one
hop overflowed, and every tried hop that did NOT overflow is one a short wait
cannot fix -- its own KNOWN window is too small anyway, it is out for at least
_CTX_OVERFLOW_LONG_WAIT seconds (day quota spent, provider parked, model dead),
or it refused with a non-retryable, non-context 4xx. A SHORT rate limit, a
5xx, a timeout, a connection error or a 200 with nothing usable on a model
that could hold the request still blocks the native reply (old behaviour).
"""
import json
import time

import pytest
import requests

import app as A
import quota


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class _Resp:
    """The subset of a requests.Response the hub touches."""

    def __init__(self, status=200, payload=None, text=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = {}
        self.text = text if text is not None else json.dumps(self._payload)

    def json(self):
        return self._payload

    def close(self):
        pass

    def iter_content(self, chunk_size=None):
        return iter(())

    def iter_lines(self, decode_unicode=False):
        return iter(())


def _google_429(quota_id, model):
    """Google's own 429 shape (QuotaFailure + RetryInfo "37s"), as recorded in
    tests/test_429_scope_and_window.py."""
    return {"error": {
        "code": 429,
        "message": "You exceeded your current quota, please check your plan and "
                   "billing details. * Quota exceeded for metric: generativelanguage."
                   "googleapis.com/generate_content_free_tier_requests, limit: 250, "
                   "model: %s\nPlease retry in 37.47s." % model,
        "status": "RESOURCE_EXHAUSTED",
        "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
             "violations": [{
                 "quotaMetric": "generativelanguage.googleapis.com/"
                                "generate_content_free_tier_requests",
                 "quotaId": quota_id,
                 "quotaDimensions": {"location": "global", "model": model},
                 "quotaValue": "250"}]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"},
        ]}}


_DAY = "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
_MINUTE = "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"

# The measured chain, in its order. Windows are what the hub KNOWS (catalog).
KILO_BIG = ("kilocode", "kilo-big-262k")
GROQ = ("groq", "llama-3.3-70b-versatile")
GEMINI_A = ("google", "gemini-3.6-flash")
GEMINI_B = ("google", "gemini-3.7-flash")
KILO_OTHER = ("kilocode", "kilo-other-2m")
NVIDIA = ("nvidia", "nv-coder-131k")

WINDOWS = {KILO_BIG: 262144, GROQ: 131072, GEMINI_A: 1048576, GEMINI_B: 1048576,
           # Holds the request: only its 400 "not supported" explains it.
           KILO_OTHER: 2000000, NVIDIA: 131072}

PINNED = "cerebras/zai-glm-4.7"


def _behave(kind, pid, model, payload):
    if kind == "overflow":
        # What _upstream_chat does when serving would drop >30% of history.
        A._ctx_note_overflow(WINDOWS.get((pid, model), 131072), pid=pid, model=model)
        raise A._ContextOverflow("request would lose most of its history")
    if kind in ("day429", "minute429"):
        resp = _Resp(429, _google_429(_DAY if kind == "day429" else _MINUTE, model))
        # ...and what _upstream_chat files on a last-key 429 (the real code).
        A._apply_429(pid, model, A._classify_429(pid, resp, model), resp)
        return resp
    if kind == "refused400":
        return _Resp(400, {"error": {"message": "Model %s is not supported for this "
                                                "request." % model,
                                     "type": "invalid_request_error"}})
    if kind == "notoffered400":
        A._note_not_offered(pid, model)
        return _Resp(400, {"error": {"message": "This model is not currently offered: "
                                                "%s" % model}})
    if kind == "5xx":
        return _Resp(503, {"error": {"message": "upstream overloaded"}})
    if kind == "timeout":
        raise requests.exceptions.ReadTimeout("read timed out")
    if kind == "conn":
        raise requests.exceptions.ConnectionError("connection refused")
    if kind == "empty200":
        return _Resp(200, {"id": "x", "choices": [{
            "index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": ""}}]})
    raise AssertionError("unknown behaviour " + kind)


@pytest.fixture
def world(monkeypatch):
    """A quiet hub with isolated quota / window / dead-model state. Returns
    set_chain([((pid, model), behaviour), ...]) and the list of dispatched hops."""
    for name in ("_record_chat_usage", "_record_outcome", "_act_pick", "_note_ttft",
                 "_record_stream_outcome", "_note_provider_timeout",
                 "_throttle_failed_hop"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)
    monkeypatch.setattr(A, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(A, "_model_block_reason", lambda pid, m: None)
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    monkeypatch.setattr(A, "_dead_models", {})
    monkeypatch.setattr(A, "_not_offered", {})
    # Google's day resets at midnight Pacific; pinned 6 h away so the test
    # never runs into a reset a few minutes off.
    monkeypatch.setattr(quota, "_day_bounds_tz",
                        lambda zone, now: (now - 3600.0, now + 6 * 3600.0))
    qdicts = (quota._STATE, quota._MODEL_STATE, quota._MODEL_THROTTLE, quota._DYNAMIC,
              quota._MODEL_DYNAMIC, quota._TOKENS, quota._KEY_COOLDOWN)
    qsaved = [dict(d) for d in qdicts]
    persist = (quota._PERSIST_PATH, quota._persist_last)
    for d in qdicts:
        d.clear()
    quota._PERSIST_PATH = None
    wdicts = (A._MODEL_MAX_INPUT, A._MODEL_LEARNED_AT, A._MODEL_CATALOG_CTX,
              A._MODEL_MAX_OUTPUT)
    wsaved = [dict(d) for d in wdicts]
    for key, win in WINDOWS.items():
        A._MODEL_MAX_INPUT[key] = win
        A._MODEL_LEARNED_AT.pop(key, None)
    calls = []

    def set_chain(plan):
        plan = list(plan)
        behaviours = dict(plan)
        monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [hop for hop, _ in plan])

        def fake(pid, payload, stream):
            model = payload.get("model")
            calls.append((pid, model))
            return _behave(behaviours[(pid, model)], pid, model, payload)

        monkeypatch.setattr(A, "_dispatch_chat", fake)
        return calls

    try:
        yield set_chain
    finally:
        for d, old in zip(qdicts, qsaved):
            d.clear()
            d.update(old)
        quota._PERSIST_PATH, quota._persist_last = persist
        for d, old in zip(wdicts, wsaved):
            d.clear()
            d.update(old)


def _measured_plan(google="day429", other="refused400"):
    return [(KILO_BIG, "overflow"), (GROQ, "overflow"), (GEMINI_A, google),
            (GEMINI_B, google), (KILO_OTHER, other), (NVIDIA, "overflow")]


# ~326K tokens of conversation (the measured est=325869), as chat messages.
_TURN = ("Refactor step %d: moved the parser into its own module, kept the public "
         "names, and added a regression test for the empty-input case. ")


def _history(tokens=325000):
    chunk = (_TURN * 40)[:4000]
    msgs = [{"role": "system", "content": "You are OpenCode, a coding agent."}]
    i = 0
    while A._est_tokens(msgs) < tokens:
        msgs.append({"role": "user", "content": "step %d: " % i + chunk})
        msgs.append({"role": "assistant", "content": "done %d: " % i + chunk})
        i += 1
    msgs.append({"role": "user", "content": "Now run the tests and fix what fails."})
    return msgs


HISTORY = _history()

TOOLS = [{"type": "function", "function": {
    "name": "bash", "description": "Run a shell command.",
    "parameters": {"type": "object", "properties": {"command": {"type": "string"}},
                   "required": ["command"]}}}]


def _sse_events(raw):
    out = []
    for block in raw.split("\n\n"):
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
        if data and data != "[DONE]":
            try:
                out.append((name, json.loads(data)))
            except ValueError:
                pass
    return out


def _chat(stream, tools=True):
    body = {"model": PINNED, "stream": stream, "messages": HISTORY}
    if tools:
        body["tools"] = TOOLS
    return A.app.test_client().post("/v1/chat/completions", json=body)


def _messages():
    return A.app.test_client().post("/v1/messages", json={
        "model": PINNED, "max_tokens": 4096, "system": HISTORY[0]["content"],
        "messages": [m for m in HISTORY[1:]]})


def _responses_stream():
    return A.app.test_client().post("/v1/responses", json={
        "model": PINNED, "stream": True,
        "instructions": HISTORY[0]["content"],
        "input": [{"role": m["role"], "content": m["content"]} for m in HISTORY[1:]]})


def _assert_openai_overflow(r):
    assert r.status_code == 400, r.get_data(as_text=True)[:400]
    err = r.get_json()["error"]
    assert err["code"] == "context_length_exceeded"
    assert r.headers.get("X-Free-LLM-Hub-Last-Error") == "context"


# --------------------------------------------------------------------------- #
# The measured case: every protocol gets its native overflow error
# --------------------------------------------------------------------------- #

def test_the_measured_conversation_is_really_that_big():
    assert A._est_tokens(HISTORY) >= 325000


def test_chat_stream_measured_case_is_context_length_exceeded(world):
    calls = world(_measured_plan())
    r = _chat(stream=True)
    _assert_openai_overflow(r)
    assert len(calls) == 6, "every hop of the measured chain was tried"


def test_chat_non_stream_measured_case_is_context_length_exceeded(world):
    world(_measured_plan())
    _assert_openai_overflow(_chat(stream=False))


def test_responses_stream_measured_case_is_a_response_failed_event(world):
    world(_measured_plan())
    r = _responses_stream()
    assert r.status_code == 200
    events = _sse_events(r.get_data(as_text=True))
    failed = [o for n, o in events if n == "response.failed"]
    assert failed, events[:3]
    assert failed[0]["response"]["error"]["code"] == "context_length_exceeded"


def test_messages_measured_case_is_prompt_is_too_long(world):
    world(_measured_plan())
    r = _messages()
    assert r.status_code == 400, r.get_data(as_text=True)[:400]
    body = r.get_json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["message"].startswith("prompt is too long")


def test_the_new_path_is_logged(world, caplog):
    world(_measured_plan())
    with caplog.at_level("INFO"):
        _chat(stream=True)
    line = [rec.getMessage() for rec in caplog.records
            if "could hold it are out" in rec.getMessage()]
    assert line, "the narrowed path logs one line"
    assert "google/gemini-3.6-flash: rate-limited" in line[0]
    assert "kilocode/kilo-other-2m: refused with HTTP 400" in line[0]


# --------------------------------------------------------------------------- #
# Unchanged: a model that could hold it and only needs a short wait
# --------------------------------------------------------------------------- #

def test_a_short_rate_limit_on_a_model_that_could_hold_it_is_still_a_503(world):
    world(_measured_plan(google="minute429"))
    r = _chat(stream=True)
    assert r.status_code == 503
    assert r.get_json()["error"].get("code") != "context_length_exceeded"


def test_a_short_rate_limit_is_still_a_503_on_messages(world):
    world(_measured_plan(google="minute429"))
    r = _messages()
    assert r.status_code == 503
    assert not r.get_json()["error"]["message"].startswith("prompt is too long")


def test_a_short_rate_limit_is_still_not_an_overflow_on_responses(world, monkeypatch):
    monkeypatch.setattr(A, "_CHAIN_RETRY_DELAY", 0)
    world(_measured_plan(google="minute429"))
    raw = _responses_stream().get_data(as_text=True)
    assert "context_length_exceeded" not in raw


@pytest.mark.parametrize("failure", ["5xx", "timeout", "conn", "empty200"])
def test_a_transient_failure_on_a_model_that_could_hold_it_is_still_a_503(world, failure):
    world([(KILO_BIG, "overflow"), (GROQ, "overflow"), (GEMINI_A, failure),
           (KILO_OTHER, "refused400")])
    r = _chat(stream=False)
    assert r.status_code in (503, 502), r.get_data(as_text=True)[:300]
    assert r.get_json()["error"].get("code") != "context_length_exceeded"


def test_one_day_quota_does_not_excuse_a_sibling_that_only_timed_out(world):
    world([(KILO_BIG, "overflow"), (GEMINI_A, "day429"), (GEMINI_B, "timeout")])
    r = _chat(stream=False)
    assert r.status_code == 503


def test_a_not_offered_400_is_a_short_wait_not_a_refusal(world):
    world([(KILO_BIG, "overflow"), (GEMINI_A, "day429"), (KILO_OTHER, "notoffered400")])
    r = _chat(stream=False)
    assert r.status_code == 503


# --------------------------------------------------------------------------- #
# A hop whose own known window is too small never blocks, whatever it did
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("failure", ["5xx", "timeout", "conn", "minute429", "empty200"])
def test_a_hop_too_small_anyway_does_not_block_whatever_it_failed_on(world, failure):
    small = ("nvidia", "nv-small-131k")
    A._MODEL_MAX_INPUT[small] = 131072          # known, far under ~375K needed
    world([(KILO_BIG, "overflow"), (small, failure)])
    _assert_openai_overflow(_chat(stream=False))


def test_a_hop_whose_window_is_only_a_guess_still_blocks(world):
    unknown = ("mysteryhost", "mystery-model-x")
    assert A._model_ctx_info(*unknown)[1] == "default"
    world([(KILO_BIG, "overflow"), (unknown, "5xx")])
    assert _chat(stream=False).status_code == 503


# --------------------------------------------------------------------------- #
# The reply itself, driven directly (per-request state, no chain)
# --------------------------------------------------------------------------- #

def _ctx(est=326000, fixed=2000):
    A._ctx_set("_ctx_signal", True)
    A._ctx_set("_ctx_orig_est", est)
    A._ctx_set("_ctx_fixed_est", fixed)
    A._ctx_set("_ctx_overflow", None)
    A._ctx_set("_ctx_tried", set())
    A._ctx_set("_ctx_results", {})


def _tried(pid, model, status=None, exc=None):
    A._ctx_note_tried(pid, model)
    if status is not None or exc is not None:
        A._ctx_note_hop_result(pid, model, status=status, exc=exc)


def test_a_parked_provider_does_not_block(world, monkeypatch):
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: pid == "google")
    with A.app.test_request_context("/v1/chat/completions", method="POST"):
        _ctx()
        _tried(*KILO_BIG)
        A._ctx_note_overflow(262144, *KILO_BIG)
        _tried(*GEMINI_A, status=503)
        out = A._ctx_overflow_reply("openai")
        assert out is not None and out[1] == 400


def test_a_dead_model_does_not_block(world):
    A._dead_models[GEMINI_A] = time.time() + 3600
    with A.app.test_request_context("/v1/chat/completions", method="POST"):
        _ctx()
        _tried(*KILO_BIG)
        A._ctx_note_overflow(262144, *KILO_BIG)
        _tried(*GEMINI_A, exc=requests.exceptions.ConnectionError())
        assert A._ctx_overflow_reply("openai") is not None


@pytest.mark.parametrize("status", [401, 403, 404, 413, 422])
def test_a_non_retryable_4xx_does_not_block(world, status):
    with A.app.test_request_context("/v1/messages", method="POST"):
        _ctx()
        _tried(*KILO_BIG)
        A._ctx_note_overflow(262144, *KILO_BIG)
        _tried(*KILO_OTHER, status=status)
        out = A._ctx_overflow_reply("anthropic")
        assert out is not None and out[1] == 400


@pytest.mark.parametrize("status", [408, 409, 425, 500, 502, 503, 200])
def test_a_retryable_status_still_blocks(world, status):
    with A.app.test_request_context("/v1/chat/completions", method="POST"):
        _ctx()
        _tried(*KILO_BIG)
        A._ctx_note_overflow(262144, *KILO_BIG)
        _tried(*KILO_OTHER, status=status)
        assert A._ctx_overflow_reply("openai") is None


def test_a_429_blocks_unless_the_quota_says_it_is_out_for_long(world):
    with A.app.test_request_context("/v1/chat/completions", method="POST"):
        _ctx()
        _tried(*KILO_BIG)
        A._ctx_note_overflow(262144, *KILO_BIG)
        _tried(*GEMINI_A, status=429)
        quota.mark_model_throttled(*GEMINI_A, seconds=60)       # a minute
        assert A._ctx_overflow_reply("openai") is None
        quota.mark_model_throttled(*GEMINI_A,
                                   until=time.time() + A._CTX_OVERFLOW_LONG_WAIT + 120)
        assert A._ctx_overflow_reply("openai") is not None


def test_a_provider_wide_day_quota_counts_as_long(world):
    with A.app.test_request_context("/v1/chat/completions", method="POST"):
        _ctx()
        _tried(*KILO_BIG)
        A._ctx_note_overflow(262144, *KILO_BIG)
        _tried("openrouter", "big-free-1m", status=429)
        A._MODEL_MAX_INPUT[("openrouter", "big-free-1m")] = 1048576
        quota.mark_throttled("openrouter", seconds=3 * 3600)
        assert A._ctx_overflow_reply("openai") is not None


def test_a_hop_with_nothing_recorded_still_blocks(world):
    with A.app.test_request_context("/v1/chat/completions", method="POST"):
        _ctx()
        _tried(*KILO_BIG)
        A._ctx_note_overflow(262144, *KILO_BIG)
        _tried(*GEMINI_A)                       # dispatched, outcome unknown
        assert A._ctx_overflow_reply("openai") is None


def test_no_overflow_at_all_is_never_the_native_reply(world):
    with A.app.test_request_context("/v1/chat/completions", method="POST"):
        _ctx()
        _tried(*KILO_OTHER, status=400)
        assert A._ctx_overflow_reply("openai") is None


def test_futile_compaction_still_wins_on_the_new_path(world):
    """Kept exactly: when the system prompt + tools alone fill the window,
    compacting cannot help, so it stays a capacity failure."""
    with A.app.test_request_context("/v1/chat/completions", method="POST"):
        _ctx(fixed=250000)
        _tried(*KILO_BIG)
        A._ctx_note_overflow(262144, *KILO_BIG)
        _tried(*KILO_OTHER, status=400)
        assert A._ctx_overflow_reply("openai") is None


def test_the_signal_gate_still_applies_on_the_new_path(world):
    with A.app.test_request_context("/v1/chat/completions", method="POST"):
        _ctx()
        A._ctx_set("_ctx_signal", False)
        _tried(*KILO_BIG)
        A._ctx_note_overflow(262144, *KILO_BIG)
        _tried(*KILO_OTHER, status=400)
        assert A._ctx_overflow_reply("openai") is None


def test_the_helpers_never_raise_outside_a_request():
    A._ctx_note_hop_result("p", "m", status=429)
    assert A._ctx_others_cannot_serve({("p", "m")}, {"hops": 1, "keys": [["q", "n"]]}) is None
    assert A._ctx_hop_cannot_serve("p", "m", 1000, None) is None
