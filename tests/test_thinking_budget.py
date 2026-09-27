"""Thinking models, small budgets and truncated stubs.

MEASURED LIVE 2026-09-27 (non-stream /v1/chat/completions, "What is N plus 1?
Answer with only the number.", max_tokens 40): google/models/gemini-3-flash-
preview answered "4" / "2" / "94" for four-digit sums -- finish_reason
"length", usage.completion_tokens 1. Gemini spent the rest of the budget on
hidden reasoning (and reports none of it). The same call with reasoning_effort
"low" answered in full. This pins:

  * a thinking model (docs table, catalog flag, runtime evidence) gets a
    reasoning allowance on top of a small caller budget, and LOW effort;
  * a non-thinking model is untouched;
  * a stub cut at "length" is not an answer: ONE same-pair retry with room,
    on all three protocols, streamed (before any byte is committed) and not;
  * a provider that rejects reasoning_effort is retried without it and never
    sent it again; the hub's private payload key never goes upstream;
  * the caller still gets ~its own budget of VISIBLE text;
  * trivial turns on pipeline ids (swarm / crew* / multi / compounds) take
    the one-strong-model fast path, with or without tools -- and ONLY trivial
    ones (the tally README prompt runs the pipeline).
No network anywhere: every upstream is a fake.
"""
import json
from unittest import mock

import pytest

import app as A

GEMINI = ("google", "models/gemini-3-flash-preview")
PLAIN = ("groq", "llama-3.3-70b-versatile")
Q = "What is 2993 plus 1? Answer with only the number."


def _chat_data(text, fin="stop", usage=None, reasoning=None):
    msg = {"role": "assistant", "content": text}
    if reasoning:
        msg["reasoning_content"] = reasoning
    d = {"id": "x", "object": "chat.completion",
         "choices": [{"index": 0, "finish_reason": fin, "message": msg}]}
    if usage is not None:
        d["usage"] = usage
    return d


def _sse(text, fin):
    frames = [{"id": "x", "object": "chat.completion.chunk",
               "choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                            "finish_reason": None}]},
              {"id": "x", "object": "chat.completion.chunk",
               "choices": [{"index": 0, "delta": {}, "finish_reason": fin}]}]
    return [b"data: " + json.dumps(f).encode() + b"\n\n" for f in frames] + [b"data: [DONE]\n\n"]


class _Resp:
    def __init__(self, status=200, payload=None, chunks=None, text=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self._chunks = list(chunks or ())
        self.headers = {}
        self.text = text if text is not None else json.dumps(self._payload)
        self.closed = False

    def json(self):
        return self._payload

    def close(self):
        self.closed = True

    def iter_content(self, chunk_size=None):
        return iter(self._chunks)

    def iter_lines(self, decode_unicode=False):
        out = []
        for c in self._chunks:
            out.extend(c.split(b"\n"))
        return iter(out)


@pytest.fixture
def pinned(monkeypatch):
    """Route every request to the chain the test names; nothing real."""
    state = {"chain": [GEMINI]}
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: list(state["chain"]))
    monkeypatch.setattr(A, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(A, "_model_block_reason", lambda pid, model: None)
    return state


def _fake_dispatch(monkeypatch, replies):
    """_dispatch_chat stand-in: pops `replies` per call and records the
    (pid, model, max_tokens, reasoning_effort, private-key-present) sent."""
    calls = []

    def fake(pid, payload, stream):
        calls.append({"pid": pid, "model": payload.get("model"),
                      "max_tokens": payload.get("max_tokens"),
                      "effort": payload.get("reasoning_effort"),
                      "stream": stream})
        return replies.pop(0)
    monkeypatch.setattr(A, "_dispatch_chat", fake)
    return calls


# --------------------------------------------------------------------------- #
# Who thinks, and what they get
# --------------------------------------------------------------------------- #

def test_a_thinking_model_gets_an_allowance_and_low_effort_on_a_small_ask():
    out = A._apply_reasoning_effort({"max_tokens": 40}, GEMINI[1], None, pid=GEMINI[0])
    assert out["reasoning_effort"] == "low"
    assert out["max_tokens"] == 40 + A._THINKING_ALLOWANCE["low"]
    assert out[A._CALLER_MAX_KEY] == 40


def test_a_non_thinking_model_is_untouched():
    out = A._apply_reasoning_effort({"max_tokens": 40}, PLAIN[1], "simple", pid=PLAIN[0])
    assert out == {"max_tokens": 40}


def test_flash_lite_is_not_a_default_thinker_by_name():
    assert not A._thinks_by_default("google", "models/gemini-2.5-flash-lite")
    assert A._thinks_by_default("google", "models/gemini-2.5-flash")
    assert A._thinks_by_default("google", "gemini-3.6")


def test_a_big_budget_keeps_its_value():
    out = A._apply_reasoning_effort({"max_tokens": 32000}, GEMINI[1], "hard", pid=GEMINI[0])
    assert out["max_tokens"] == 32000 and A._CALLER_MAX_KEY not in out


def test_a_catalog_flag_earns_room_but_no_effort():
    """A hybrid may think only when asked: sending it an effort would switch
    thinking ON. The catalog flag alone earns the allowance, nothing else."""
    A._learn_ctx_from_catalog("openrouter", {"data": [
        {"id": "vendor/hybrid-x", "supported_parameters": ["reasoning", "tools"]},
        {"id": "vendor/plain-y", "supported_parameters": ["tools"]}]})
    out = A._apply_reasoning_effort({"max_tokens": 40}, "vendor/hybrid-x", "simple",
                                    pid="openrouter")
    assert "reasoning_effort" not in out
    assert out["max_tokens"] == 40 + A._THINKING_ALLOWANCE["medium"]
    # the same identity on another host is known too
    assert A._can_think("someother", "hybrid-x")
    assert A._apply_reasoning_effort({"max_tokens": 40}, "vendor/plain-y", "simple",
                                     pid="openrouter") == {"max_tokens": 40}


def test_runtime_evidence_makes_a_default_thinker():
    pair = ("nvidia", "z-ai/glm-5.3")
    assert not A._thinks_by_default(*pair)
    A._note_thinking_evidence(*pair, data=_chat_data(
        "2862", usage={"completion_tokens": 28, "prompt_tokens": 28,
                       "completion_tokens_details": {"reasoning_tokens": 26}}))
    assert A._thinks_by_default(*pair)
    out = A._apply_reasoning_effort({"max_tokens": 40}, pair[1], None, pid=pair[0])
    assert out["reasoning_effort"] == "low" and out["max_tokens"] > 1000


# --------------------------------------------------------------------------- #
# Stub detection
# --------------------------------------------------------------------------- #

def test_stub_vs_plain_truncation():
    assert A._is_truncated_stub("94", "length", 40)
    assert A._is_truncated_stub("4", "length", 40, visible=1)
    # a non-thinking model cut at its budget filled the budget: not a stub
    assert not A._is_truncated_stub("word " * 40, "length", 40, visible=40)
    assert not A._is_truncated_stub("94", "stop", 40)
    assert not A._is_truncated_stub("", "length", 40)        # the empty case is older


def test_starve_kind_reads_usage_and_the_callers_budget():
    stub = _chat_data("94", "length", usage={"completion_tokens": 1, "prompt_tokens": 18})
    payload = {"max_tokens": 1064, A._CALLER_MAX_KEY: 40}
    assert A._starve_kind(stub, payload) == "stub"
    assert A._starve_kind(_chat_data("", "length"), payload) == "empty"
    assert A._starve_kind(_chat_data("2994"), payload) is None


def test_a_stub_never_wins_a_hedge_race():
    stub = _Resp(200, _chat_data("94", "length"))
    assert A._hedge_leg_verdict((0, "json", stub, None), {"max_tokens": 40}) == "starved"
    peek = (0, "peek", _Resp(), ("content", _sse("94", "length"), iter(())))
    assert A._hedge_leg_verdict(peek, {"max_tokens": 40}) == "starved"


# --------------------------------------------------------------------------- #
# The retry, through the real endpoints
# --------------------------------------------------------------------------- #

def test_chat_non_stream_stub_is_retried_and_the_full_answer_served(monkeypatch, pinned):
    calls = _fake_dispatch(monkeypatch, [
        _Resp(200, _chat_data("94", "length", usage={"completion_tokens": 1})),
        _Resp(200, _chat_data("2994"))])
    r = A.app.test_client().post("/v1/chat/completions", json={
        "model": "%s/%s" % GEMINI, "max_tokens": 40, "stream": False,
        "messages": [{"role": "user", "content": Q}]})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    assert r.get_json()["choices"][0]["message"]["content"] == "2994"
    assert [(c["pid"], c["model"]) for c in calls] == [GEMINI, GEMINI], calls
    assert calls[0]["max_tokens"] == 40 + 1024 and calls[0]["effort"] == "low"
    assert calls[1]["max_tokens"] > calls[0]["max_tokens"] and calls[1]["effort"] == "low"


def test_a_stub_that_starves_again_falls_through_and_is_filed(monkeypatch, pinned):
    pinned["chain"] = [GEMINI, PLAIN]
    filed = []
    monkeypatch.setattr(A, "_record_outcome",
                        lambda pid, model, ok, **kw: filed.append((pid, model, ok)))
    calls = _fake_dispatch(monkeypatch, [
        _Resp(200, _chat_data("94", "length")),
        _Resp(200, _chat_data("9", "length")),
        _Resp(200, _chat_data("2994"))])
    r = A.app.test_client().post("/v1/chat/completions", json={
        "model": "%s/%s" % GEMINI, "max_tokens": 40, "stream": False,
        "messages": [{"role": "user", "content": Q}]})
    assert r.get_json()["choices"][0]["message"]["content"] == "2994", r.get_json()
    assert [c["pid"] for c in calls] == ["google", "google", "groq"]
    assert ("google", GEMINI[1], False) in filed


def test_chat_stream_stub_is_retried_before_any_byte_is_sent(monkeypatch, pinned):
    calls = _fake_dispatch(monkeypatch, [
        _Resp(200, chunks=_sse("94", "length")),
        _Resp(200, chunks=_sse("2994", "stop"))])
    r = A.app.test_client().post("/v1/chat/completions", json={
        "model": "%s/%s" % GEMINI, "max_tokens": 40, "stream": True,
        "messages": [{"role": "user", "content": Q}]})
    body = r.get_data(as_text=True)
    assert "2994" in body
    assert '"94"' not in body, "the stub must never reach the client"
    assert len(calls) == 2 and calls[1]["max_tokens"] > calls[0]["max_tokens"]


def test_responses_non_stream_stub_is_retried(monkeypatch, pinned):
    calls = _fake_dispatch(monkeypatch, [
        _Resp(200, _chat_data("94", "length")),
        _Resp(200, _chat_data("2994"))])
    r = A.app.test_client().post("/v1/responses", json={
        "model": "%s/%s" % GEMINI, "max_output_tokens": 40, "stream": False,
        "input": Q})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    assert "2994" in r.get_data(as_text=True)
    assert len(calls) == 2


def test_messages_stream_stub_is_retried(monkeypatch, pinned):
    calls = _fake_dispatch(monkeypatch, [
        _Resp(200, chunks=_sse("94", "length")),
        _Resp(200, chunks=_sse("2994", "stop"))])
    r = A.app.test_client().post("/v1/messages", json={
        "model": "%s/%s" % GEMINI, "max_tokens": 40, "stream": True,
        "messages": [{"role": "user", "content": Q}]})
    body = r.get_data(as_text=True)
    assert "2994" in body and "message_stop" in body
    assert len(calls) == 2


def test_messages_non_stream_stub_is_retried(monkeypatch, pinned):
    calls = _fake_dispatch(monkeypatch, [
        _Resp(200, _chat_data("94", "length")),
        _Resp(200, _chat_data("2994"))])
    r = A.app.test_client().post("/v1/messages", json={
        "model": "%s/%s" % GEMINI, "max_tokens": 40, "stream": False,
        "messages": [{"role": "user", "content": Q}]})
    assert r.get_json()["content"][0]["text"] == "2994"
    assert len(calls) == 2


def test_a_non_thinking_model_is_served_as_before(monkeypatch, pinned):
    pinned["chain"] = [PLAIN]
    calls = _fake_dispatch(monkeypatch, [_Resp(200, _chat_data("2994"))])
    r = A.app.test_client().post("/v1/chat/completions", json={
        "model": "%s/%s" % PLAIN, "max_tokens": 40, "stream": False,
        "messages": [{"role": "user", "content": Q}]})
    assert r.get_json()["choices"][0]["message"]["content"] == "2994"
    assert calls == [{"pid": "groq", "model": PLAIN[1], "max_tokens": 40,
                      "effort": None, "stream": False}]


# --------------------------------------------------------------------------- #
# Visible budget
# --------------------------------------------------------------------------- #

def test_visible_text_is_kept_to_about_the_callers_budget():
    long = " ".join("word%d" % i for i in range(400))
    data = _chat_data(long)
    A._fit_visible_to_caller(data, {"max_tokens": 1064, A._CALLER_MAX_KEY: 40})
    assert len(data["choices"][0]["message"]["content"]) <= 160
    assert data["choices"][0]["finish_reason"] == "length"
    short = _chat_data("2994")
    A._fit_visible_to_caller(short, {"max_tokens": 1064, A._CALLER_MAX_KEY: 40})
    assert short["choices"][0]["message"]["content"] == "2994"


def test_the_stream_gate_caps_visible_text():
    long = " ".join("word%d" % i for i in range(400))
    gate = A._StreamAnswerGate(iter(_sse(long, "stop")), mode="bytes",
                               visible_cap=200, hold_chars=10 ** 6)
    out = b"".join(gate).decode()
    text, _tools, fin = A._sse_answer_digest([out.encode()])
    assert len(text) <= 200 and fin == "length"
    assert "[DONE]" in out


# --------------------------------------------------------------------------- #
# Parameter rejection, private key
# --------------------------------------------------------------------------- #

class _Post:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.sent = []

    def __call__(self, url=None, json=None, **kw):
        self.sent.append(json)
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def test_a_rejected_reasoning_effort_is_dropped_and_remembered(monkeypatch):
    monkeypatch.setattr(A.config, "get_provider_config", lambda pid: {"api_keys": ["k"]})
    monkeypatch.setattr(A, "_resolve_base_url", lambda pid, pcfg: "https://example.invalid/v1")
    post = _Post(_Resp(400, text='{"error": {"message": "`reasoning_effort` must be one '
                                 'of none, default"}}'),
                 _Resp(200, _chat_data("ok")))
    monkeypatch.setattr(A.requests, "post", post)
    pair = ("groq", "qwen/qwen3-32b")
    payload = {"model": pair[1], "_no_craft": True, "max_tokens": 1064,
               "reasoning_effort": "low", A._CALLER_MAX_KEY: 40,
               "messages": [{"role": "user", "content": "hi"}]}
    resp = A._upstream_chat(pair[0], payload, False)
    assert resp.status_code == 200
    assert post.sent[0]["reasoning_effort"] == "low"
    assert "reasoning_effort" not in post.sent[1]
    assert A._reasoning_rejected(*pair)
    # never again, whoever sets it -- and the hub never sets it itself
    A._upstream_chat(pair[0], dict(payload), False)
    assert "reasoning_effort" not in post.sent[2]
    A._note_thinking(*pair)
    assert "reasoning_effort" not in A._apply_reasoning_effort(
        {"max_tokens": 40}, pair[1], "simple", pid=pair[0])
    # the private key never went upstream
    assert all(A._CALLER_MAX_KEY not in s for s in post.sent)


def test_an_unrelated_400_is_not_mistaken_for_a_reasoning_rejection():
    assert not A._REASONING_PARAM_ERR_RE.search(
        "This model's maximum context length is 8192 tokens")
    assert A._REASONING_PARAM_ERR_RE.search("Unrecognized request argument: reasoning_effort")


# --------------------------------------------------------------------------- #
# Pipelines: trivial turns take one strong model, everything else the pipeline
# --------------------------------------------------------------------------- #

TOOLS = [{"type": "function", "function": {"name": "Bash", "parameters": {}}}]
TALLY = ("Write a concise README section for a command-line tool named tally that "
         "counts lines, words and bytes in files. Include: a one-paragraph "
         "description, a usage block, a markdown table of exactly 3 flags (-l, -w, "
         "-c) with descriptions, and one example command with its output.")


def _single():
    return mock.Mock(side_effect=lambda b: (A.jsonify({"choices": [
        {"index": 0, "message": {"role": "assistant", "content": "1235"}}]}), 200))


def test_the_tally_readme_prompt_runs_the_pipeline():
    assert A._swarm_fast_path({}, [{"role": "user", "content": TALLY}]) is False
    single = _single()
    ran = mock.Mock(return_value={"text": "## tally\n..."})
    body = {"model": "swarm", "messages": [{"role": "user", "content": TALLY}]}
    with A.app.test_request_context(json=body), \
            mock.patch.object(A, "_chat_completions_uncached", single), \
            mock.patch.object(A, "_act_pipeline_watcher", lambda: None), \
            mock.patch.object(A, "_swarm_manager_kwargs", lambda: {}), \
            mock.patch.object(A.swarm, "run", ran):
        resp = A.app.make_response(A._swarm_completion(body))
    assert ran.called, "the pipeline the user picked must run"
    assert "fast-path" not in resp.headers.get("X-Free-LLM-Hub-Pipeline", "")


@pytest.mark.parametrize("ask", [
    "Write a haiku about rain",
    "List the planets, their moons, their sizes and their distances",
    "Explain TCP.\n1. handshake\n2. windows\n3. congestion",
    "Summarize this. Then translate it. Then list the key terms. Then rate it.",
])
def test_non_trivial_asks_never_take_the_fast_path(ask):
    assert A._swarm_fast_path({}, [{"role": "user", "content": ask}]) is False


@pytest.mark.parametrize("model", ["swarm", "multi", "crew-code"])
def test_a_trivial_tool_turn_skips_the_fan_out(model):
    single = _single()
    fan = mock.Mock(side_effect=AssertionError("the fan-out must not run"))
    # Claude Code wraps the real ask in <system-reminder> blocks.
    content = [{"type": "text", "text": "<system-reminder>\n" + "ctx " * 400 +
                "\n</system-reminder>"},
               {"type": "text", "text": "What is 1234 plus 1?"}]
    body = {"model": model, "tools": TOOLS, "max_tokens": 32000,
            "messages": [{"role": "user", "content": content}]}
    with A.app.test_request_context(json=body), \
            mock.patch.object(A, "_chat_completions_uncached", single), \
            mock.patch.object(A, "_swarm_tool_turn", fan):
        resp = A.app.make_response(A._swarm_completion(body))
    assert single.call_args[0][0]["model"] == "best"
    assert single.call_args[0][0]["tools"] == TOOLS
    assert "fast-path" in resp.headers.get("X-Free-LLM-Hub-Pipeline", "")


def test_a_hard_tool_turn_still_fans_out():
    fan = mock.Mock(side_effect=lambda b: (A.jsonify({"ok": True}), 200, {}))
    body = {"model": "multi", "tools": TOOLS, "messages": [{"role": "user", "content":
            "Implement a REST API with auth, tests and a React frontend"}]}
    with A.app.test_request_context(json=body), \
            mock.patch.object(A, "_swarm_tool_turn", fan):
        A._swarm_completion(body)
    assert fan.called


def test_messages_trivial_tool_turn_on_a_compound_takes_one_model(monkeypatch, pinned):
    pinned["chain"] = [PLAIN]
    monkeypatch.setattr(A, "_route_by_difficulty",
                        lambda *a, **k: (PLAIN[0], PLAIN[1], "simple"))
    monkeypatch.setattr(A, "_swarm_tool_result",
                        mock.Mock(side_effect=AssertionError("fan-out ran")))
    _fake_dispatch(monkeypatch, [_Resp(200, _chat_data("1235"))])
    r = A.app.test_client().post("/v1/messages", json={
        "model": "coding-swarm", "max_tokens": 32000, "stream": False,
        "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
        "messages": [{"role": "user", "content": "What is 1234 plus 1?"}]})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    assert r.get_json()["content"][0]["text"] == "1235"
    assert "fast-path" in r.headers.get("X-Free-LLM-Hub-Pipeline", "")


def test_the_trivial_question_takes_the_fast_path():
    assert A._swarm_fast_path({}, [{"role": "user", "content": "What is 1234 plus 1?"}])
    assert A._swarm_fast_path({}, [{"role": "user", "content": Q}])
