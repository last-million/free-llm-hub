"""Context-window management, in every mode and for every CLI.

REQUESTED 2026-09-26: "context-window management must be perfect in all modes
and for every CLI". The audit behind it found ten separate faults; each section
below pins one fix, with fakes only (no network, no real provider):

  1. the NEWEST tool result could be deleted with its call (Codex looped)
  2. the FIRST user message was pinned (AGENTS.md / a system-reminder), not
     the latest instruction
  3. usage reported to CLIs was post-compaction or missing, so a CLI's own
     compaction never fired
  4. a CLI was never told about overflow -- everything ended in 503
  5. provider-wide figures stood in for known per-model windows
  6. no room reserved for the reply; output caps learned as input windows;
     learned windows never expired
  7. the compaction recap was keyed by the exact dropped set, so it almost
     never arrived -- and was RAM-only for /v1 clients
  8. a category mode was silently dropped on big contexts
  9. estimates: English-only, flat images, count_tokens without tools
 10. routing prefers models that hold the window the CLIs are told
"""
import base64
import json
import struct
import time

import pytest

import app as A
import ctxwin


# --------------------------------------------------------------------------- #
# Fakes and fixtures
# --------------------------------------------------------------------------- #

class _Resp:
    """The subset of a requests.Response the hub touches."""

    def __init__(self, status=200, payload=None, chunks=None, text=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self._chunks = chunks
        self.headers = {}
        self.text = text if text is not None else json.dumps(self._payload)
        self.closed = False

    def json(self):
        return self._payload

    def close(self):
        self.closed = True

    def iter_content(self, chunk_size=None):
        return iter(self._chunks or ())

    def iter_lines(self, decode_unicode=False):
        return iter(self._chunks or ())


@pytest.fixture(autouse=True)
def isolated_windows():
    """Every learned/catalog/output table restored after each test."""
    saved = [(d, dict(d)) for d in (A._MODEL_MAX_INPUT, A._MODEL_LEARNED_AT,
                                    A._MODEL_CATALOG_CTX, A._MODEL_MAX_OUTPUT)]
    no_so = set(A._NO_STREAM_OPTIONS)
    yield
    for d, snap in saved:
        d.clear()
        d.update(snap)
    A._NO_STREAM_OPTIONS.clear()
    A._NO_STREAM_OPTIONS.update(no_so)


@pytest.fixture
def quiet(monkeypatch):
    for name in ("_record_chat_usage", "_record_outcome", "_act_pick", "_note_ttft",
                 "_record_stream_outcome", "_note_provider_timeout",
                 "_throttle_failed_hop"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)
    monkeypatch.setattr(A, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(A, "_model_block_reason", lambda pid, m: None)
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("cerebras", "zai-glm-4.7")])
    yield


PINNED = "cerebras/zai-glm-4.7"


def _answer(text="Done.", usage=None):
    d = {"id": "x", "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": text}}]}
    if usage is not None:
        d["usage"] = usage
    return d


# Longer than _PEEK_JUDGE_CHARS, so the first-content peek commits the stream
# on the content itself rather than judging it at the [DONE] line.
_LONG_ANSWER = ("Here is the refactored handler, with the parsing split out into "
                "its own function and the error paths made explicit. ") * 8


def _sse_chunks(text=_LONG_ANSWER, usage=None, newline=True):
    frames = [
        {"id": "x", "object": "chat.completion.chunk",
         "choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                      "finish_reason": None}]},
        {"id": "x", "object": "chat.completion.chunk",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    if usage is not None:
        frames.append({"id": "x", "object": "chat.completion.chunk", "choices": [],
                       "usage": usage})
    end = b"\n\n" if newline else b""
    return ([b"data: " + json.dumps(f).encode() + end for f in frames]
            + [b"data: [DONE]" + end])


def _compacting_dispatch(resp_factory, before=90000, after=30000, seen=None):
    """A stand-in for _dispatch_chat that behaves like a hop whose payload
    _upstream_chat compacted to a third of its size."""
    def fake(pid, payload, stream):
        if seen is not None:
            seen.append(dict(payload))
        A._ctx_note_hop(pid, payload.get("model"), before, after)
        return resp_factory(stream)
    return fake


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


# --------------------------------------------------------------------------- #
# 1. A tool call and its result are never separated
# --------------------------------------------------------------------------- #

BIG_FILE = "def handler(event):\n    return process(event)\n" * 3500   # ~150 KB


def _codex_read_loop():
    return [
        {"role": "system", "content": "You are Codex, a coding agent."},
        {"role": "user", "content": "# AGENTS.md instructions for /repo\n\n<INSTRUCTIONS>\n"
                                    "Use 4 spaces.\n</INSTRUCTIONS>"},
        {"role": "user", "content": "<environment_context>\n  <cwd>/repo</cwd>\n"
                                    "</environment_context>"},
        {"role": "user", "content": "Refactor the event handler in big.py"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "shell", "arguments": "{\"cmd\": \"cat big.py\"}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": BIG_FILE},
    ]


def _check_pair(out):
    out = A._sanitize_tool_messages(out)
    calls = [m for m in out if m.get("role") == "assistant" and m.get("tool_calls")]
    results = [m for m in out if m.get("role") == "tool"]
    assert calls and calls[-1]["tool_calls"][0]["id"] == "call_1", "the call was dropped"
    assert len(results) == 1 and results[0]["tool_call_id"] == "call_1"
    assert results[0]["content"] != "(tool result unavailable)", "result replaced by a stub"
    return results[0]["content"]


def test_the_newest_tool_result_survives_with_its_call_truncated_not_dropped():
    """A Codex turn that read a 150 KB file on a 32K model: the old compaction
    kept only the result, dropped its call, and the sanitizer then deleted the
    result as an orphan -- the model saw nothing and read the file again."""
    out, did = A._compact_to_budget(_codex_read_loop(), None, 32768)
    assert did
    content = _check_pair(out)
    assert "omitted by the hub" in content and "tool output is truncated" in content
    assert content.startswith("def handler"), "the head of the result must survive"
    assert A._est_tokens(out) <= int(32768 * 0.85)


def test_claude_code_shape_keeps_the_pair_behind_a_trailing_reminder():
    """Claude Code's newest message is often a <system-reminder>, with the
    large tool result one unit further back -- the current turn still wins."""
    msgs = [{"role": "system", "content": "You are Claude Code."},
            {"role": "user", "content": "<system-reminder>ctx</system-reminder>\n"
                                        "Fix the login bug"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "Read", "arguments": "{\"file_path\": \"a.py\"}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": BIG_FILE},
            {"role": "user", "content": "<system-reminder>todo list is empty"
                                        "</system-reminder>"}]
    out, did = A._compact_to_budget(msgs, None, 32768)
    assert did
    _check_pair(out)
    assert any("Fix the login bug" in (m.get("content") or "") for m in out)


def test_message_units_group_a_call_with_its_results():
    msgs = _codex_read_loop()[1:]
    units = A._message_units(msgs)
    assert [len(u) for u in units] == [1, 1, 1, 2]


# --------------------------------------------------------------------------- #
# 2. The LATEST instruction is pinned in full; the original as an excerpt
# --------------------------------------------------------------------------- #

def _tool_unit(i, size=2000):
    return [{"role": "assistant", "content": None, "tool_calls": [{
                "id": "c%d" % i, "type": "function",
                "function": {"name": "read_file", "arguments": "{\"path\": \"src/m%d.py\"}" % i}}]},
            {"role": "tool", "tool_call_id": "c%d" % i, "content": "x = %d\n" % i * (size // 6)}]


LATEST = "Now add a dark mode toggle to the header and keep the layout"


def _long_codex_session():
    msgs = [{"role": "system", "content": "You are Codex."},
            {"role": "user", "content": "# AGENTS.md instructions for /repo\n\n"
                                        "<INSTRUCTIONS>\nrules\n</INSTRUCTIONS>"},
            {"role": "user", "content": "<environment_context>\n  <cwd>/repo</cwd>\n"
                                        "</environment_context>"},
            {"role": "user", "content": "Build a todo app called Tasko"}]
    for i in range(30):
        msgs += _tool_unit(i)
    msgs.append({"role": "user", "content": LATEST})
    for i in range(30, 70):
        msgs += _tool_unit(i)
    return msgs


def test_the_latest_instruction_is_pinned_in_full_during_a_long_tool_loop():
    out, did = A._compact_to_budget(_long_codex_session(), None, 16000)
    assert did
    assert any(m.get("content") == LATEST for m in out), "latest instruction dropped"
    assert A._est_tokens(out) <= int(16000 * 0.85)


def test_the_original_request_is_pinned_as_an_excerpt_not_agents_md():
    out, _ = A._compact_to_budget(_long_codex_session(), None, 16000)
    pins = [m for m in out if "[Original request of this conversation]"
            in str(m.get("content"))]
    assert len(pins) == 1
    assert "Tasko" in pins[0]["content"]
    assert "AGENTS.md" not in pins[0]["content"]


def test_the_pinned_instruction_keeps_its_place_before_the_kept_tool_turns():
    out, _ = A._compact_to_budget(_long_codex_session(), None, 16000)
    idx = next(i for i, m in enumerate(out) if m.get("content") == LATEST)
    later = [m for m in out[idx + 1:] if m.get("role") in ("assistant", "tool")]
    assert later, "the kept tool turns must come after the instruction they serve"


@pytest.mark.parametrize("text,real", [
    ("<system-reminder>only a reminder</system-reminder>", False),
    ("<environment_context><cwd>/x</cwd></environment_context>", False),
    ("# AGENTS.md instructions for /x\n\n<INSTRUCTIONS>\nr\n</INSTRUCTIONS>", False),
    ("<system-reminder>r</system-reminder>\nplease fix the tests", True),
    ("add a footer", True),
])
def test_real_instructions_are_told_apart_from_cli_wrapper_blocks(text, real):
    assert ctxwin.is_real_instruction({"role": "user", "content": text}) is real


def test_the_dropped_tool_turns_are_named_in_the_notice():
    out, _ = A._compact_to_budget(_long_codex_session(), None, 16000)
    notice = next(m["content"] for m in out if m.get("role") == "system"
                  and "dropped to fit" in m.get("content", ""))
    assert "Earlier tool calls" in notice and "read_file(" in notice


# --------------------------------------------------------------------------- #
# 3. Usage is reported on the ORIGINAL request size, on all three protocols
# --------------------------------------------------------------------------- #

def test_reported_prompt_tokens_scale_by_the_hop_compaction():
    with A.app.test_request_context("/v1/chat/completions"):
        A._ctx_begin({}, [], 5000)
        A._ctx_note_hop("p", "m", 90000, 30000)
        assert A._reported_prompt_tokens(1000, 5000, "p", "m") == 3000
        assert A._reported_prompt_tokens(None, 5000, "p", "m") == 5000
        assert A._reported_prompt_tokens(1000, 5000, "p", "other") == 1000


def test_chat_non_stream_usage_is_pre_compaction(quiet, monkeypatch):
    monkeypatch.setattr(A, "_dispatch_chat", _compacting_dispatch(
        lambda s: _Resp(200, _answer(usage={"prompt_tokens": 1000,
                                            "completion_tokens": 7,
                                            "total_tokens": 1007}))))
    r = A.app.test_client().post("/v1/chat/completions", json={
        "model": PINNED, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    u = r.get_json()["usage"]
    assert u["prompt_tokens"] == 3000 and u["completion_tokens"] == 7
    assert u["total_tokens"] == 3007


def test_chat_non_stream_usage_is_filled_in_when_upstream_sends_none(quiet, monkeypatch):
    monkeypatch.setattr(A, "_dispatch_chat", lambda pid, p, s: _Resp(200, _answer("x" * 400)))
    body = {"model": PINNED, "messages": [{"role": "user", "content": "y" * 4000}]}
    r = A.app.test_client().post("/v1/chat/completions", json=body)
    u = r.get_json()["usage"]
    assert u["prompt_tokens"] >= 1000, "the original request is ~1000 tokens"
    assert u["completion_tokens"] == 100


def test_chat_stream_usage_frame_is_rewritten(quiet, monkeypatch):
    monkeypatch.setattr(A, "_dispatch_chat", _compacting_dispatch(
        lambda s: _Resp(200, chunks=_sse_chunks(usage={"prompt_tokens": 1000,
                                                       "completion_tokens": 5,
                                                       "total_tokens": 1005}))))
    r = A.app.test_client().post("/v1/chat/completions", json={
        "model": PINNED, "stream": True, "stream_options": {"include_usage": True},
        "messages": [{"role": "user", "content": "hi"}]})
    usages = [o["usage"] for _n, o in _sse_events(r.get_data(as_text=True))
              if isinstance(o, dict) and o.get("usage")]
    assert usages and usages[-1]["prompt_tokens"] == 3000


def test_chat_stream_usage_is_injected_when_asked_and_missing(quiet, monkeypatch):
    monkeypatch.setattr(A, "_dispatch_chat",
                        lambda pid, p, s: _Resp(200, chunks=_sse_chunks()))
    r = A.app.test_client().post("/v1/chat/completions", json={
        "model": PINNED, "stream": True, "stream_options": {"include_usage": True},
        "messages": [{"role": "user", "content": "z" * 4000}]})
    raw = r.get_data(as_text=True)
    usages = [o["usage"] for _n, o in _sse_events(raw) if o.get("usage")]
    assert usages, "client asked include_usage and got no usage frame"
    assert usages[-1]["prompt_tokens"] >= 1000
    assert raw.rstrip().endswith("data: [DONE]")


def test_responses_non_stream_usage_is_pre_compaction(quiet, monkeypatch):
    monkeypatch.setattr(A, "_dispatch_chat", _compacting_dispatch(
        lambda s: _Resp(200, _answer(usage={"prompt_tokens": 1000,
                                            "completion_tokens": 3}))))
    r = A.app.test_client().post("/v1/responses", json={"model": PINNED, "input": "hi"})
    assert r.status_code == 200
    assert r.get_json()["usage"]["input_tokens"] == 3000


def test_responses_stream_always_reports_usage_and_asks_upstream_for_it(quiet, monkeypatch):
    seen = []
    monkeypatch.setattr(A, "_dispatch_chat", _compacting_dispatch(
        lambda s: _Resp(200, chunks=_sse_chunks(newline=False)), seen=seen))
    r = A.app.test_client().post("/v1/responses", json={
        "model": PINNED, "stream": True, "input": "w" * 4000})
    done = [o for n, o in _sse_events(r.get_data(as_text=True))
            if n == "response.completed"]
    assert done and done[0]["response"]["usage"]["input_tokens"] >= 1000
    assert done[0]["response"]["usage"]["output_tokens"] > 0
    assert seen[0].get("stream_options") == {"include_usage": True}


def test_responses_stream_scales_an_upstream_count(quiet, monkeypatch):
    monkeypatch.setattr(A, "_dispatch_chat", _compacting_dispatch(
        lambda s: _Resp(200, chunks=_sse_chunks(usage={"prompt_tokens": 1000,
                                                       "completion_tokens": 5},
                                                newline=False))))
    r = A.app.test_client().post("/v1/responses", json={
        "model": PINNED, "stream": True, "input": "hi"})
    done = [o for n, o in _sse_events(r.get_data(as_text=True))
            if n == "response.completed"]
    assert done[0]["response"]["usage"]["input_tokens"] == 3000


ANTH_TOOLS = [{"name": "Read", "description": "Read a file from disk. " * 40,
               "input_schema": {"type": "object",
                                "properties": {"file_path": {"type": "string"}}}}]


def test_messages_non_stream_usage_is_pre_compaction(quiet, monkeypatch):
    monkeypatch.setattr(A, "_dispatch_chat", _compacting_dispatch(
        lambda s: _Resp(200, _answer(usage={"prompt_tokens": 1000,
                                            "completion_tokens": 3}))))
    r = A.app.test_client().post("/v1/messages", json={
        "model": PINNED, "max_tokens": 1000,
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert r.get_json()["usage"]["input_tokens"] == 3000


def test_messages_stream_reports_input_including_tools(quiet, monkeypatch):
    seen = []
    monkeypatch.setattr(A, "_dispatch_chat", _compacting_dispatch(
        lambda s: _Resp(200, chunks=_sse_chunks(usage={"prompt_tokens": 1000,
                                                       "completion_tokens": 5},
                                                newline=False)), seen=seen))
    body = {"model": PINNED, "max_tokens": 1000, "stream": True, "tools": ANTH_TOOLS,
            "messages": [{"role": "user", "content": "hi"}]}
    r = A.app.test_client().post("/v1/messages", json=body)
    events = _sse_events(r.get_data(as_text=True))
    start = next(o for n, o in events if n == "message_start")
    delta = next(o for n, o in events if n == "message_delta")
    assert start["message"]["usage"]["input_tokens"] == A._estimate_input_tokens(body)
    assert start["message"]["usage"]["input_tokens"] > 200, "tools must be counted"
    assert delta["usage"]["input_tokens"] == 3000
    assert seen[0].get("stream_options") == {"include_usage": True}


# --------------------------------------------------------------------------- #
# 4. The CLI is told, in its own protocol, when the context overflowed
# --------------------------------------------------------------------------- #

def _overflowing(pid, payload, stream):
    A._ctx_note_overflow(32768)
    raise A._ContextOverflow("would drop most of the history")


def test_chat_overflow_is_openai_context_length_exceeded(quiet, monkeypatch):
    monkeypatch.setattr(A, "_dispatch_chat", _overflowing)
    r = A.app.test_client().post("/v1/chat/completions", json={
        "model": PINNED, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 400
    err = r.get_json()["error"]
    assert err["code"] == "context_length_exceeded"
    assert "maximum context length is 32768" in err["message"]
    assert r.headers.get("X-Free-LLM-Hub-Last-Error") == "context"


def test_messages_overflow_is_prompt_is_too_long(quiet, monkeypatch):
    monkeypatch.setattr(A, "_dispatch_chat", _overflowing)
    r = A.app.test_client().post("/v1/messages", json={
        "model": PINNED, "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 400
    body = r.get_json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["message"].startswith("prompt is too long")


def test_responses_stream_overflow_is_a_response_failed_event(quiet, monkeypatch):
    monkeypatch.setattr(A, "_dispatch_chat", _overflowing)
    r = A.app.test_client().post("/v1/responses", json={
        "model": PINNED, "stream": True, "input": "hi"})
    assert r.status_code == 200
    events = _sse_events(r.get_data(as_text=True))
    failed = [o for n, o in events if n == "response.failed"]
    assert failed and failed[0]["response"]["error"]["code"] == "context_length_exceeded"


def test_responses_non_stream_overflow_is_a_400(quiet, monkeypatch):
    monkeypatch.setattr(A, "_dispatch_chat", _overflowing)
    r = A.app.test_client().post("/v1/responses", json={"model": PINNED, "input": "hi"})
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "context_length_exceeded"


def test_an_upstream_context_400_is_relayed_as_overflow_not_503(quiet, monkeypatch):
    body = {"error": {"message": "This model's maximum context length is 32768 tokens. "
                                 "However, your messages resulted in 50000 tokens."}}
    monkeypatch.setattr(A, "_dispatch_chat", lambda pid, p, s: _Resp(400, body))
    r = A.app.test_client().post("/v1/chat/completions", json={
        "model": PINNED, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "context_length_exceeded"


def test_the_signal_can_be_switched_off(quiet, monkeypatch):
    real = A.config.get_flag
    monkeypatch.setattr(A.config, "get_flag", lambda name, default=False: (
        False if name == "context_overflow_signal" else real(name, default)))
    monkeypatch.setattr(A, "_dispatch_chat", _overflowing)
    r = A.app.test_client().post("/v1/chat/completions", json={
        "model": PINNED, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 503


def _long_convo(turns=60, size=2000):
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(turns):
        msgs.append({"role": "user", "content": "step %d: " % i + "u" * size})
        msgs.append({"role": "assistant", "content": "done %d " % i + "a" * size})
    return msgs


def test_upstream_chat_skips_a_hop_that_would_drop_most_of_the_history():
    A._MODEL_MAX_INPUT[("pz", "mz")] = 8000
    with A.app.test_request_context("/v1/chat/completions"):
        A._ctx_begin({}, [], 60000)
        with pytest.raises(A._ContextOverflow):
            A._upstream_chat("pz", {"model": "mz", "_no_craft": True,
                                    "messages": _long_convo()}, False)
        assert A._ctx_g("_ctx_overflow")["hops"] == 1


def test_a_cli_compaction_request_is_never_refused_for_size():
    """The overflow error is the CLI's cue to compact; refusing the compaction
    request itself would leave it no way out."""
    A._MODEL_MAX_INPUT[("pz", "mz")] = 8000
    msgs = _long_convo() + [{"role": "user", "content": "You are performing a CONTEXT "
                                                        "CHECKPOINT COMPACTION. Create a "
                                                        "handoff summary."}]
    with A.app.test_request_context("/v1/chat/completions"):
        A._ctx_begin({}, [], 60000)
        with pytest.raises(RuntimeError):      # got past compaction: no base_url for "pz"
            A._upstream_chat("pz", {"model": "mz", "_no_craft": True,
                                    "messages": msgs}, False)


def test_a_guessed_window_never_signals_overflow():
    """Nothing known about the model: the truth arrives as a real 400."""
    with A.app.test_request_context("/v1/chat/completions"):
        A._ctx_begin({}, [], 60000)
        with pytest.raises(RuntimeError):
            A._upstream_chat("unknown-provider", {"model": "m", "_no_craft": True,
                                                  "messages": _long_convo(400)}, False)


def test_a_refit_that_would_gut_the_history_is_refused_when_signalling():
    """A 400 taught a much smaller window: re-fitting to it would silently drop
    most of the conversation, so with the signal on the hop is given up and
    the overflow counted instead."""
    A._MODEL_MAX_INPUT[("pz", "mr")] = 8000
    payload = {"model": "mr", "messages": _long_convo()}
    with A.app.test_request_context("/v1/chat/completions"):
        A._ctx_begin({}, [], 60000)
        assert A._refit_payload_to_learned_ctx("pz", payload) is None
        assert A._ctx_g("_ctx_overflow")["hops"] == 1
    # Outside a signalling request the old behaviour stands: re-fit and serve.
    assert A._refit_payload_to_learned_ctx("pz", payload) is not None


def test_the_abort_fraction_leaves_the_payload_untouched():
    st = {}
    msgs = _long_convo()
    out, did = A._compact_to_budget(msgs, None, 8000, stats=st, abort_frac=0.30)
    assert (out, did) == (msgs, False) and st["overflow"] is True
    assert st["dropped_frac"] > 0.30


def test_an_output_cap_error_is_not_a_context_overflow():
    with A.app.test_request_context("/v1/chat/completions"):
        A._ctx_begin({}, [], 100)
        A._ctx_note_overflow_resp(_Resp(400, text="max_tokens (32000) exceeds the "
                                                  "maximum allowed (8192)"), "p", "m")
        assert not A._ctx_g("_ctx_overflow")


# --------------------------------------------------------------------------- #
# 5. A known per-model window beats the provider-wide figure
# --------------------------------------------------------------------------- #

def test_a_known_1m_window_is_not_capped_by_the_provider_row():
    A._MODEL_MAX_INPUT[("openrouter", "vendor/model-1m")] = 1048576
    assert A._model_ctx_budget("openrouter", "vendor/model-1m") == 1048576
    assert A._model_ctx_info("openrouter", "vendor/model-1m")[1] == "learned"
    assert "vendor/model-1m" in A._big_window_models(300000).get("openrouter", set())
    assert A._model_ctx_info("openrouter", "not-known-x") == (A._PROVIDER_TPM["openrouter"],
                                                              "table")
    assert A._model_ctx_info("no-such-provider", "m")[1] == "default"


def test_a_hard_per_request_cap_still_applies():
    """groq's 8000 is a measured per-request TPM cap, not a window stand-in."""
    A._MODEL_MAX_INPUT[("groq", "llama-x")] = 131072
    assert A._model_ctx_budget("groq", "llama-x") == A._PROVIDER_TPM["groq"]
    assert "llama-x" not in A._big_window_models(20000).get("groq", set())


def test_catalog_windows_are_remembered_apart_from_learned_ones():
    A._learn_ctx_from_catalog("pc", {"data": [{"id": "m", "context_length": 200000}]})
    assert A._MODEL_CATALOG_CTX[("pc", "m")] == 200000
    assert ("pc", "m") not in A._MODEL_LEARNED_AT


# --------------------------------------------------------------------------- #
# 6. Output reserve, max_tokens clamp, output caps, learned-window TTL
# --------------------------------------------------------------------------- #

def test_the_output_reserve_is_capped_at_a_quarter_of_the_window():
    assert A._output_reserve(32768, 32000) == 8192
    assert A._output_reserve(128000, 4096) == 4096
    assert A._output_reserve(128000, None) == 0


def test_compaction_leaves_room_for_the_reply():
    msgs = _long_convo(30)
    out, did = A._compact_to_budget(msgs, None, 32768, reserve=8192)
    assert did and A._est_tokens(out) <= 32768 - 8192


class _Post:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.sent = []

    def __call__(self, url=None, json=None, **kw):
        self.sent.append(json)
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


@pytest.fixture
def fake_upstream(monkeypatch):
    monkeypatch.setattr(A.config, "get_provider_config", lambda pid: {"api_keys": ["k"]})
    monkeypatch.setattr(A, "_resolve_base_url", lambda pid, pcfg: "https://example.invalid/v1")
    post = _Post(_Resp(200, _answer()))
    monkeypatch.setattr(A.requests, "post", post)
    return post


def test_max_tokens_is_clamped_to_what_is_left_of_the_window(fake_upstream):
    A._MODEL_MAX_INPUT[("cloudflare", "@cf/test/m32k")] = 32768
    msgs = [{"role": "user", "content": "q" * 20000}]
    A._upstream_chat("cloudflare", {"model": "@cf/test/m32k", "max_tokens": 32000,
                                    "_no_craft": True, "messages": msgs}, False)
    sent = fake_upstream.sent[0]
    assert sent["max_tokens"] < 32000
    assert sent["max_tokens"] + A._est_tokens(sent["messages"]) <= 32768


def test_a_learned_output_cap_is_applied_before_sending(fake_upstream):
    A._MODEL_MAX_INPUT[("cloudflare", "@cf/test/m32k")] = 32768
    A._MODEL_MAX_OUTPUT[("cloudflare", "@cf/test/m32k")] = 4096
    A._upstream_chat("cloudflare", {"model": "@cf/test/m32k", "max_tokens": 16000,
                                    "_no_craft": True,
                                    "messages": [{"role": "user", "content": "hi"}]}, False)
    assert fake_upstream.sent[0]["max_tokens"] == 4096


def test_stream_options_are_dropped_and_remembered_when_rejected(monkeypatch):
    monkeypatch.setattr(A.config, "get_provider_config", lambda pid: {"api_keys": ["k"]})
    monkeypatch.setattr(A, "_resolve_base_url", lambda pid, pcfg: "https://example.invalid/v1")
    post = _Post(_Resp(400, text='{"error": "Unrecognized request argument supplied: '
                                 'stream_options"}'),
                 _Resp(200, chunks=_sse_chunks()))
    monkeypatch.setattr(A.requests, "post", post)
    resp = A._upstream_chat("cerebras", {"model": "m", "_no_craft": True, "stream": True,
                                         "stream_options": {"include_usage": True},
                                         "messages": [{"role": "user", "content": "hi"}]},
                            True)
    assert resp.status_code == 200
    assert "stream_options" in post.sent[0] and "stream_options" not in post.sent[1]
    assert "cerebras" in A._NO_STREAM_OPTIONS


GROQ_OUT = ('{"error":{"message":"`max_tokens` must be less than or equal to `8192`, the '
            'maximum value for `max_tokens` is less than the `context_window` for this '
            'model","type":"invalid_request_error"}}')
ANTH_OUT = ('{"type":"error","error":{"type":"invalid_request_error","message":'
            '"max_tokens: 100000 > 64000, which is the maximum allowed number of output '
            'tokens for claude-x"}}')
GENERIC_OUT = '{"error":{"message":"max_tokens (32000) exceeds the maximum allowed (8192)"}}'
CF_400 = ('{"errors":[{"message":"AiError: {\\"error\\":{\\"message\\":\\"This '
          "model's maximum context length is 32768 tokens. However, you requested "
          '64 output tokens\\"}}"}]}')


@pytest.mark.parametrize("body,cap", [(GROQ_OUT, 8192), (ANTH_OUT, 64000),
                                      (GENERIC_OUT, 8192)])
def test_an_output_cap_is_never_learned_as_the_input_window(body, cap):
    A._MODEL_MAX_INPUT.pop(("po", "mo"), None)
    A._MODEL_MAX_OUTPUT.pop(("po", "mo"), None)
    A._learn_context_limit("po", "mo", _Resp(400, text=body))
    assert A._MODEL_MAX_OUTPUT.get(("po", "mo")) == cap
    assert ("po", "mo") not in A._MODEL_MAX_INPUT


def test_a_context_error_is_still_learned_as_the_window_not_an_output_cap():
    A._MODEL_MAX_INPUT.pop(("po", "mc"), None)
    A._learn_context_limit("po", "mc", _Resp(400, text=CF_400))
    assert A._MODEL_MAX_INPUT.get(("po", "mc")) == 32768
    assert ("po", "mc") not in A._MODEL_MAX_OUTPUT


def test_refit_lowers_max_tokens_for_a_learned_output_cap():
    A._MODEL_MAX_INPUT[("po", "mr")] = 128000
    A._MODEL_MAX_OUTPUT[("po", "mr")] = 8192
    out = A._refit_payload_to_learned_ctx("po", {
        "model": "mr", "max_tokens": 32000, "messages": [{"role": "user", "content": "hi"}]})
    assert out is not None and out["max_tokens"] == 8192


def test_a_learned_window_expires_back_to_the_catalog():
    A._MODEL_CATALOG_CTX[("pt", "m")] = 128000
    A._MODEL_MAX_INPUT[("pt", "m")] = 128000
    A._set_learned_ctx("pt", "m", 16000)
    assert A._model_ctx_budget("pt", "m") == 16000
    A._MODEL_LEARNED_AT[("pt", "m")] = time.time() - A._LEARNED_CTX_TTL - 60
    assert A._model_ctx_budget("pt", "m") == 128000
    assert ("pt", "m") not in A._MODEL_LEARNED_AT


def test_a_learned_window_with_no_catalog_expires_to_unknown():
    A._set_learned_ctx("pt", "n", 16000)
    A._MODEL_LEARNED_AT[("pt", "n")] = time.time() - A._LEARNED_CTX_TTL - 60
    assert A._ctx_limit("pt", "n") is None
    assert A._context_ok("pt", "n", 100000) is True


def test_an_expired_lesson_is_not_restored_from_disk():
    old = time.time() - A._LEARNED_CTX_TTL - 60
    A._dead_state_load({"model_max_input": {"pt|old": 9000, "pt|new": 9000},
                        "model_max_input_ts": {"pt|old": old, "pt|new": time.time()}})
    assert ("pt", "old") not in A._MODEL_MAX_INPUT
    assert A._MODEL_MAX_INPUT[("pt", "new")] == 9000
    assert ("pt", "new") in A._MODEL_LEARNED_AT


def test_output_caps_and_provenance_survive_a_restart():
    A._MODEL_MAX_OUTPUT[("pt", "o")] = 4096
    A._set_learned_ctx("pt", "w", 12000)
    blob = A._dead_state_dump()
    A._MODEL_MAX_OUTPUT.pop(("pt", "o"))
    A._MODEL_MAX_INPUT.pop(("pt", "w"))
    A._MODEL_LEARNED_AT.pop(("pt", "w"))
    A._dead_state_load(blob)
    assert A._MODEL_MAX_OUTPUT[("pt", "o")] == 4096
    assert A._MODEL_MAX_INPUT[("pt", "w")] == 12000 and ("pt", "w") in A._MODEL_LEARNED_AT


# --------------------------------------------------------------------------- #
# 7. Rolling recap: one per conversation, incremental, persisted
# --------------------------------------------------------------------------- #

class _SyncThread:
    def __init__(self, target=None, args=(), kwargs=None, **_kw):
        self._t, self._a, self._k = target, args, kwargs or {}

    def start(self):
        self._t(*self._a, **self._k)


@pytest.fixture
def recap_world(tmp_path, monkeypatch):
    path = tmp_path / "recaps.json"
    monkeypatch.setattr(A, "_RECAP_STORE_PATH", str(path))
    monkeypatch.setattr(A, "_recap_store", ctxwin.RecapStore(A._recap_store_path))
    monkeypatch.setattr(A.threading, "Thread", _SyncThread)
    A._summary_cache.clear()
    A._summary_inflight.clear()
    replies, seen = ["RECAP-1: building Tasko", "RECAP-2: Tasko plus dark mode"], []
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("p", "m", "medium"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("p", "m")])

    def fake_dispatch(pid, payload, stream):
        seen.append(payload["messages"][1]["content"])
        return _Resp(200, _answer(replies[min(len(seen), len(replies)) - 1]))

    monkeypatch.setattr(A, "_dispatch_chat", fake_dispatch)
    yield path, seen
    A._summary_cache.clear()
    A._summary_inflight.clear()


def _history(n):
    msgs = [{"role": "user", "content": "Build a todo app called Tasko with a sidebar"}]
    for i in range(n):
        msgs += [{"role": "assistant", "content": "Working on step %d. " % i + "w" * 300,
                  "tool_calls": [{"id": "t%d" % i, "type": "function",
                                  "function": {"name": "read_file",
                                               "arguments": "{\"path\": \"src/f%d.ts\"}" % i}}]},
                 {"role": "tool", "tool_call_id": "t%d" % i, "content": "file %d " % i + "c" * 300}]
    return msgs


def test_the_rolling_recap_hits_across_turns_and_survives_a_restart(recap_world,
                                                                     monkeypatch):
    path, seen = recap_world
    turn1, turn2 = _history(10), _history(25)
    with A.app.test_request_context("/v1/chat/completions",
                                    headers={"X-Session-Id": "ses_42"}):
        A._ctx_begin({}, turn1, 1000)
        assert A._summarize_dropped(turn1) is None            # computed off-path
        assert A._summarize_dropped(turn1).startswith("RECAP-1")
        # The conversation grew: the recap still arrives (the old exact-set key
        # changed every turn), and an incremental update is scheduled.
        assert A._summarize_dropped(turn2).startswith("RECAP-1")
        assert A._summarize_dropped(turn2).startswith("RECAP-2")
    assert "[called read_file(" in seen[0], "tool-call-only turns must reach the recap"
    assert "EXISTING RECAP" in seen[1] and "RECAP-1" in seen[1]
    # A hub restart: RAM gone, the file is all that is left.
    A._summary_cache.clear()
    monkeypatch.setattr(A, "_recap_store", ctxwin.RecapStore(A._recap_store_path))
    with A.app.test_request_context("/v1/chat/completions",
                                    headers={"X-Session-Id": "ses_42"}):
        A._ctx_begin({}, turn2, 1000)
        assert A._summarize_dropped(turn2).startswith("RECAP-2")
    assert len(seen) == 2, "a persisted recap must not be recomputed"


def test_a_different_conversation_does_not_get_this_recap(recap_world):
    turn = _history(10)
    with A.app.test_request_context("/v1/chat/completions",
                                    headers={"X-Session-Id": "ses_a"}):
        A._ctx_begin({}, turn, 1000)
        A._summarize_dropped(turn)
    other = [{"role": "user", "content": "Write a haiku about rain, then a limerick"}] + turn[1:]
    with A.app.test_request_context("/v1/chat/completions",
                                    headers={"X-Session-Id": "ses_b"}):
        A._ctx_begin({}, other, 1000)
        assert A._rolling_recap("hdr:ses_b", other) is None


def test_the_recap_store_is_bounded_and_expires(tmp_path):
    store = ctxwin.RecapStore(lambda: str(tmp_path / "r.json"), max_items=3, ttl=100)
    for i in range(5):
        store.put("c%d" % i, {"recap": "r%d" % i})
    assert len(store) == 3 and store.get("c0") is None and store.get("c4")["recap"] == "r4"
    reloaded = ctxwin.RecapStore(lambda: str(tmp_path / "r.json"), max_items=3, ttl=100)
    assert reloaded.get("c4")["recap"] == "r4"
    expired = ctxwin.RecapStore(lambda: str(tmp_path / "r.json"), max_items=3, ttl=-1)
    assert expired.get("c4") is None


# --------------------------------------------------------------------------- #
# ...keyed by the CLI's OWN conversation id on /v1
# --------------------------------------------------------------------------- #

def _key(headers=None, body=None, messages=None, sid=None):
    with A.app.test_request_context("/v1/chat/completions", headers=headers or {}):
        return ctxwin.conversation_key(A.request.headers, body, sid, messages)


def test_opencode_session_headers():
    assert _key({"X-Session-Id": "ses_abc", "User-Agent": "opencode/1.2"}) == "hdr:ses_abc"
    assert _key({"x-session-affinity": "ses_def"}) == "hdr:ses_def"


def test_claude_code_session_header_and_metadata():
    assert _key({"X-Claude-Code-Session-Id": "5f2c9e1a-0000-4000-8000-000000000001"}) \
        == "hdr:5f2c9e1a-0000-4000-8000-000000000001"
    legacy = {"metadata": {"user_id": "user_ab12cd_account_1111-2222_session_"
                                      "3f1e2d3c-aaaa-bbbb-cccc-1234567890ab"}}
    assert _key(body=legacy) == "meta:3f1e2d3c-aaaa-bbbb-cccc-1234567890ab"
    newer = {"metadata": {"user_id": json.dumps({"device_id": "d1",
                                                 "session_id": "s-123-abc"})}}
    assert _key(body=newer) == "meta:s-123-abc"


def test_codex_prompt_cache_key():
    """Codex's session_id / conversation_id HEADERS never reach Flask: the
    werkzeug server drops header names containing underscores. Its body's
    prompt_cache_key carries the same conversation id."""
    assert _key(body={"prompt_cache_key": "0199a1b2-c3d4-7e5f-8000-abcdefabcdef"}) \
        == "pck:0199a1b2-c3d4-7e5f-8000-abcdefabcdef"


def test_the_hub_agent_session_wins():
    assert _key({"X-Session-Id": "x"}, sid="agent-7") == "agent:agent-7"


def test_the_fallback_hash_skips_codex_wrapper_messages():
    agents = {"role": "user", "content": "# AGENTS.md instructions for /r\n\n"
                                         "<INSTRUCTIONS>\nx\n</INSTRUCTIONS>"}
    env = {"role": "user", "content": "<environment_context><cwd>/r</cwd>"
                                      "</environment_context>"}
    a = [{"role": "system", "content": "codex"}, agents, env,
         {"role": "user", "content": "build a shop"}]
    b = a[:3] + [{"role": "user", "content": "fix the CI"}]
    assert _key(messages=a) == _key(messages=a + [{"role": "assistant", "content": "ok"}])
    assert _key(messages=a) != _key(messages=b), "same folder, different conversation"


def test_the_handler_records_the_conversation_for_the_recap(quiet, monkeypatch):
    got = {}

    def fake(pid, payload, stream):
        got["conv"] = A._ctx_g("_ctx_conv")
        return _Resp(200, _answer())

    monkeypatch.setattr(A, "_dispatch_chat", fake)
    A.app.test_client().post("/v1/messages", json={
        "model": PINNED, "max_tokens": 100,
        "metadata": {"user_id": "user_1_account_2_session_aaaabbbb-cccc"},
        "messages": [{"role": "user", "content": "hi"}]})
    assert got["conv"] == "meta:aaaabbbb-cccc"


# --------------------------------------------------------------------------- #
# 8. A category mode survives a big context
# --------------------------------------------------------------------------- #

MODE_MODELS = {"pa": ["code-strong", "chat-strong"], "pb": ["chat-mid"]}
MODE_SCORES = {"code-strong": 134.0, "chat-strong": 130.0, "chat-mid": 100.0}
MSGS = [{"role": "user", "content": "add a dark mode toggle to the header"}]


@pytest.fixture
def mode_world(monkeypatch):
    monkeypatch.setattr(A, "_available_providers", lambda *a, **k: ["pa", "pb"])
    monkeypatch.setattr(A, "_prefetch_auto_models", lambda pids: dict(MODE_MODELS))
    monkeypatch.setattr(A, "_auto_models", lambda pid: list(MODE_MODELS[pid]))
    monkeypatch.setattr(A, "_provider_capable", lambda pid, est: True)
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: MODE_SCORES[m])
    monkeypatch.setattr(A, "_weighted_pick", lambda pool, *a, **k: max(pool))
    monkeypatch.setattr(A, "_supports_tools", lambda pid, m: True)
    monkeypatch.setattr(A, "_is_tool_proven", lambda m: True)
    monkeypatch.setattr(A, "_session_pin_get", lambda key: None)
    monkeypatch.setattr(A, "_session_pin_set", lambda *a, **k: None)
    # The category's only model has LEARNED a limit smaller than this turn.
    monkeypatch.setattr(A, "_context_ok",
                        lambda pid, m, est: not (m == "code-strong" and est > 1000))
    monkeypatch.setattr(A, "_active_mode", lambda: "coding")
    monkeypatch.setattr(A, "_valid_mode", lambda m: m)
    monkeypatch.setattr(A, "_mode_allows",
                        lambda mode, pid, model, *a, **k: model.startswith("code-"))
    yield


def test_the_primary_stays_in_the_category_on_a_big_context(mode_world):
    _pid, model, _d = A._route_by_difficulty(MSGS, None, 60000, quality_mode=True,
                                             require_tools=True)
    assert model == "code-strong", "left the category for %s" % model


def test_the_chain_keeps_the_category_model_on_a_big_context(mode_world):
    chain = A._build_chain("", "", 60000, require_tools=True, messages=MSGS)
    assert ("pa", "code-strong") in chain
    assert chain[0] == ("pa", "code-strong")


def test_a_small_context_is_unchanged(mode_world):
    _pid, model, _d = A._route_by_difficulty(MSGS, None, 50, quality_mode=True,
                                             require_tools=True)
    assert model == "code-strong"


# --------------------------------------------------------------------------- #
# 9. Estimates: non-Latin text, images by size, count_tokens with tools
# --------------------------------------------------------------------------- #

def test_cjk_text_is_about_one_token_per_character():
    zh = "我们需要一个新的登录页面" * 100
    est = A._est_tokens([{"role": "user", "content": zh}], overhead=0)
    assert est >= len(zh) * 0.9, "chars/4 under-counts CJK about four times"


def test_arabic_text_is_denser_than_english():
    ar = "مرحبا بكم في الموقع الجديد " * 60
    en = "welcome to the new website " * 60
    assert (A._est_tokens([{"role": "user", "content": ar}], overhead=0)
            > 1.5 * A._est_tokens([{"role": "user", "content": en}], overhead=0))


def test_english_estimate_is_unchanged():
    en = "hello world " * 100
    assert A._est_tokens([{"role": "user", "content": en}], overhead=0) == len(en) // 4


def _png(w, h):
    raw = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", w, h) \
        + b"\x08\x02\x00\x00\x00" + b"\x00" * 4
    return "data:image/png;base64," + base64.b64encode(raw).decode()


@pytest.mark.parametrize("w,h,expect", [(100, 100, 85), (1000, 1000, 1334),
                                        (4000, 3000, 1600)])
def test_images_are_sized_from_their_own_header(w, h, expect):
    assert ctxwin.image_tokens(url=_png(w, h)) == expect


def test_unreadable_or_low_detail_images():
    assert ctxwin.image_tokens(url="https://example.com/cat.png") == 1000
    assert ctxwin.image_tokens(url=_png(4000, 3000), detail="low") == 85


def test_the_chat_estimate_uses_the_image_size():
    small = [{"role": "user", "content": [{"type": "image_url",
                                           "image_url": {"url": _png(100, 100)}}]}]
    assert A._est_tokens(small, overhead=0) == 85


def test_count_tokens_includes_tools_and_tool_blocks():
    client = A.app.test_client()
    base = {"model": "claude-x", "messages": [{"role": "user", "content": "hi"}]}
    n0 = client.post("/v1/messages/count_tokens", json=base).get_json()["input_tokens"]
    n1 = client.post("/v1/messages/count_tokens",
                     json=dict(base, tools=ANTH_TOOLS)).get_json()["input_tokens"]
    assert n1 > n0 + 200
    tool_turn = {"model": "claude-x", "messages": [
        {"role": "user", "content": "read it"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read",
                                           "input": {"file_path": "p" * 800}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                      "content": [{"type": "text", "text": "r" * 4000}]}]}]}
    n2 = client.post("/v1/messages/count_tokens", json=tool_turn).get_json()["input_tokens"]
    assert n2 >= (800 + 4000) // 4


# --------------------------------------------------------------------------- #
# 10. Routing prefers models that hold the window the CLIs are told
# --------------------------------------------------------------------------- #

def test_a_model_known_to_be_far_smaller_than_the_declared_window_goes_later(mode_world,
                                                                            monkeypatch):
    monkeypatch.setattr(A, "_active_mode", lambda: "all")
    monkeypatch.setattr(A, "_valid_mode", lambda m: "all")
    monkeypatch.setattr(A, "_context_ok", lambda pid, m, est: True)
    A._MODEL_MAX_INPUT[("pa", "code-strong")] = 32768
    chain = A._build_chain("", "", 50, require_tools=True, messages=MSGS)
    assert chain.index(("pa", "chat-strong")) < chain.index(("pa", "code-strong"))
    assert A._below_declared_window("pa", "code-strong") is True
    assert A._below_declared_window("pa", "never-seen") is False


# --------------------------------------------------------------------------- #
# SSE usage rewriting survives frames split across network reads
# --------------------------------------------------------------------------- #

def test_usage_rewrite_handles_split_frames():
    raw = b"".join(_sse_chunks(usage={"prompt_tokens": 10, "completion_tokens": 2,
                                      "total_tokens": 12}))
    chunks = [raw[i:i + 7] for i in range(0, len(raw), 7)]
    out = b"".join(ctxwin.fix_chat_sse_usage(chunks, lambda p: p * 3))
    assert b'"prompt_tokens": 30' in out and b'"total_tokens": 32' in out
    assert out.rstrip().endswith(b"data: [DONE]")


def test_crlf_framed_streams_are_not_held_back():
    """Some servers end SSE events with CRLF CRLF; waiting for a bare LF LF
    would have buffered the whole answer until the stream ended."""
    frames = [f.replace(b"\n\n", b"\r\n\r\n") for f in _sse_chunks()]
    gen = ctxwin.fix_chat_sse_usage(iter(frames), lambda p: p, inject_if_missing=True)
    first = next(gen)
    assert b"content" in first, "the first frame must pass through immediately"
    rest = b"".join(gen)
    assert b'"usage"' in rest and rest.rstrip().endswith(b"[DONE]")


def test_usage_is_injected_once_before_done():
    out = b"".join(ctxwin.fix_chat_sse_usage(_sse_chunks(), lambda p: 500,
                                             inject_if_missing=True))
    assert out.count(b'"usage"') == 1
    assert out.index(b'"usage"') < out.index(b"[DONE]")
