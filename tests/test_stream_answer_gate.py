"""The answer gate on STREAMS: hold visible text, judge it, serve the clean part.

MEASURED LIVE after _answer_gate shipped (streaming /v1/chat/completions,
"What is N plus 1? Answer with only the number.", max_tokens 40,
llm7/GLM-5.3-Flash):

    best   -> "3324TouchableOpacity_FP$.\\n\\nActually, the answer is"
    coding -> "319231923192"

Non-stream answers were already trimmed; on a stream the gate only ran after
the bytes were with the client. These tests drive _StreamAnswerGate (and the
three stream relays behind it) with fake upstreams -- no network.
"""
import json
import time

import pytest

import answer_check
import app

P_BEST = "What is 3323 plus 1? Answer with only the number."
P_CODING = "What is 3191 plus 1? Answer with only the number."
BEST = "3324TouchableOpacity_FP$.\n\nActually, the answer is"
CODING = "319231923192"
BAD = ("llm7", "GLM-5.3-Flash")
GOOD = ("groq", "llama-3.3-70b-versatile")


@pytest.fixture(autouse=True)
def clean_state():
    with app._outcome_lock:
        app._outcomes.clear()
    with app._dead_lock:
        app._dead_models.clear()
    yield
    with app._outcome_lock:
        app._outcomes.clear()
    with app._dead_lock:
        app._dead_models.clear()


def _outcome(pid, model):
    with app._outcome_lock:
        rec = app._outcomes.get((pid, model)) or {}
    return rec.get("ok", 0), rec.get("fail", 0)


# --------------------------------------------------------------------------- #
# Fake upstream SSE
# --------------------------------------------------------------------------- #

def _chunk(content=None, fin=None, tool=None, reasoning=None):
    delta = {}
    if content is not None:
        delta["content"] = content
    if tool is not None:
        delta["tool_calls"] = tool
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    return {"id": "chatcmpl-up", "object": "chat.completion.chunk", "created": 1,
            "model": "m", "choices": [{"index": 0, "delta": delta, "finish_reason": fin}]}


def _line(obj):
    return b"data: " + json.dumps(obj).encode("utf-8")


def _tokens(text, size=4):
    return [text[i:i + size] for i in range(0, len(text), size)]


def _lines(text, fin="length", size=4):
    return ([_line(_chunk(t)) for t in _tokens(text, size)]
            + [_line(_chunk(fin=fin)), b"data: [DONE]"])


def _frames(text, fin="length", size=4):
    return [ln + b"\n\n" for ln in _lines(text, fin, size)]


def _rechunk(frames, n=7):
    blob = b"".join(frames)
    return [blob[i:i + n] for i in range(0, len(blob), n)]


def _parse_chat(body):
    """(visible text, [finish reasons], ends with [DONE]) of chat SSE bytes."""
    text, fins = [], []
    for frame in body.split(b"\n\n"):
        frame = frame.strip()
        if not frame.startswith(b"data:"):
            continue
        payload = frame[5:].strip()
        if payload == b"[DONE]":
            continue
        ch = json.loads(payload)
        c = ch["choices"][0]
        if c.get("delta", {}).get("content"):
            text.append(c["delta"]["content"])
        if c.get("finish_reason"):
            fins.append(c["finish_reason"])
    return "".join(text), fins, body.rstrip().endswith(b"data: [DONE]")


def _gate(items, prompt, mode="bytes", tools=False, **kw):
    return app._StreamAnswerGate(iter(items), mode=mode, hop_pid=BAD[0],
                                 hop_model=BAD[1], prompt_text=prompt,
                                 last_prompt=prompt, tools_offered=tools, **kw)


# --------------------------------------------------------------------------- #
# The two live samples
# --------------------------------------------------------------------------- #

def test_live_best_sample_streams_only_the_number():
    g = _gate(_rechunk(_frames(BEST)), P_BEST)
    text, fins, done = _parse_chat(b"".join(g))
    assert text == "3324"
    assert fins == ["stop"], "the served text is complete: never 'length'"
    assert done
    assert g.cut
    assert _outcome(*BAD) == (0, 1), "a junk-tailed hop is a failed delivery"


def test_live_coding_sample_streams_only_the_number():
    g = _gate(_frames(CODING, size=1), P_CODING)
    text, fins, done = _parse_chat(b"".join(g))
    assert text == "3192"
    assert fins == ["stop"] and done
    assert _outcome(*BAD) == (0, 1)


def test_glued_number_rule_leaves_short_period_numbers_alone():
    for n in ("111111", "121212", "3192", "1000000"):
        assert answer_check.inspect(n, prompt_text=P_CODING)["ok"], n


# --------------------------------------------------------------------------- #
# What must stream unchanged
# --------------------------------------------------------------------------- #

LEGIT = (
    "## Setup\n\nInstall the package, then configure it as shown below. The "
    "defaults are fine for most projects, but the cache directory matters on "
    "shared machines.\n\n"
    "| option | default |\n|---|---|\n| cache | ~/.cache |\n| workers | 4 |\n\n"
    "```python\n" + "        print('hello world again')\n" * 12 + "```\n\n"
    "---\n\nEach worker prints the same line; that is expected. Next, run the "
    "tests and check the report. If a test fails, read its traceback first, "
    "since most failures are configuration mistakes rather than bugs.\n" * 3)


@pytest.mark.parametrize("mode", ["bytes", "lines"])
def test_long_legit_markdown_and_code_streams_unchanged(mode):
    items = _rechunk(_frames(LEGIT, fin="stop", size=5)) if mode == "bytes" \
        else _lines(LEGIT, fin="stop", size=5)
    g = _gate(items, "how do I set this up?", mode=mode)
    out = list(g)
    if mode == "bytes":
        assert b"".join(out) == b"".join(items)
    else:
        assert out == items
    assert not g.cut
    assert _outcome(*BAD) == (0, 0), "a clean stream is judged by the relay, not here"


def test_tool_call_stream_is_unchanged():
    tool = [{"index": 0, "id": "call_1", "type": "function",
             "function": {"name": "read_file", "arguments": ""}}]
    frames = ([_line(_chunk("Let me read the file.")) + b"\n\n",
               _line(_chunk(tool=tool)) + b"\n\n"]
              + [_line(_chunk(tool=[{"index": 0, "function": {"arguments": a}}])) + b"\n\n"
                 for a in ('{"pa', 'th": "a.py"}')]
              + [_line(_chunk(fin="tool_calls")) + b"\n\n", b"data: [DONE]\n\n"])
    items = _rechunk(frames, 11)
    g = _gate(items, "read a.py", tools=True)
    assert b"".join(g) == b"".join(items)
    assert not g.cut


def test_reasoning_and_keepalives_are_never_held():
    reasoning = _line(_chunk(reasoning="thinking")) + b"\n\n"
    keep = b": keepalive\n\n"
    frames = [_line(_chunk("Hello")) + b"\n\n", reasoning, keep,
              _line(_chunk(" there.", fin="stop")) + b"\n\n", b"data: [DONE]\n\n"]
    out = list(_gate(frames, "greet me"))
    assert out.index(reasoning) < out.index(frames[0]), "reasoning passes at once"
    assert out.index(keep) < out.index(frames[0])
    assert _parse_chat(b"".join(out))[0] == "Hello there."


def test_hold_window_releases_on_time():
    def slow():
        yield _line(_chunk("A clean opening sentence.")) + b"\n\n"
        time.sleep(1.0)
        yield _line(_chunk(fin="stop")) + b"\n\n"
        yield b"data: [DONE]\n\n"
    t0 = time.monotonic()
    first_at = None
    for fr in _gate(slow(), "say something", hold_seconds=0.2):
        if first_at is None and b"clean opening" in fr:
            first_at = time.monotonic() - t0
    assert first_at is not None and first_at < 0.8, first_at


def test_upstream_dying_mid_hold_still_delivers_the_held_text():
    def dies():
        yield _line(_chunk("Partial but clean answer.")) + b"\n\n"
        raise ConnectionError("reset")
    out = []
    with pytest.raises(ConnectionError):
        for fr in _gate(dies(), "say something"):
            out.append(fr)
    assert _parse_chat(b"".join(out))[0] == "Partial but clean answer."


# --------------------------------------------------------------------------- #
# After release: the rolling tail check
# --------------------------------------------------------------------------- #

def test_mid_stream_loop_is_cut_with_a_valid_terminator():
    head = "".join("Point %d of the plan is settled and written down. " % i
                   for i in range(12))
    loop = "I will check the config file now. " * 40
    g = _gate(_frames(head + loop, fin="length", size=6), "plan the migration")
    text, fins, done = _parse_chat(b"".join(g))
    assert text.startswith(head)
    assert text.count("I will check the config file now.") < 10
    assert fins == ["stop"] and done
    assert g.cut and _outcome(*BAD) == (0, 1)


def test_inspect_tail_masks_a_code_block_opened_before_the_window():
    code = "Here:\n```python\n" + "x = compute_value(1)\n" * 40 + "y = 2\n"
    assert answer_check.inspect_tail(code, prompt_text="write code") is None
    assert answer_check.inspect_tail("Fine answer. " + "OK出具证明的，原试题解析做题如有雷同",
                                     prompt_text="hi") is not None


# --------------------------------------------------------------------------- #
# The Responses and Messages translators behind the gate (lines mode)
# --------------------------------------------------------------------------- #

class _StreamResp:
    headers = {}
    status_code = 200

    def __init__(self, items=()):
        self._items = list(items)

    def iter_content(self, chunk_size=None):
        return iter(self._items)

    def iter_lines(self, decode_unicode=False):
        return iter(self._items)

    def close(self):
        pass


def _events(body):
    out = []
    for block in body.split("\n\n"):
        for ln in block.split("\n"):
            if ln.startswith("data:"):
                try:
                    out.append(json.loads(ln[5:].strip()))
                except ValueError:
                    pass
    return out


def test_responses_stream_serves_the_salvage_and_completes():
    g = _gate(_lines(CODING, size=1), P_CODING, mode="lines")
    body = "".join(x if isinstance(x, str) else x.decode() for x in app._responses_stream(
        _StreamResp(), "auto", line_iter=g, hop_pid=BAD[0], hop_model=BAD[1],
        prompt_text=P_CODING, last_prompt=P_CODING, answer_gate=g))
    ev = _events(body)
    deltas = "".join(e["delta"] for e in ev if e.get("type") == "response.output_text.delta")
    assert deltas == "3192"
    assert ev[-1]["type"] == "response.completed"
    assert _outcome(*BAD) == (0, 1), "filed once, by the gate -- not twice"


def test_messages_stream_serves_the_salvage_and_stops():
    g = _gate(_lines(BEST), P_BEST, mode="lines")
    body = "".join(x if isinstance(x, str) else x.decode() for x in app._anthropic_stream(
        _StreamResp(), "claude", 5, line_iter=g, hop_pid=BAD[0], hop_model=BAD[1],
        prompt_text=P_BEST, last_prompt=P_BEST, answer_gate=g))
    ev = _events(body)
    text = "".join(e["delta"]["text"] for e in ev
                   if e.get("type") == "content_block_delta"
                   and e["delta"].get("type") == "text_delta")
    assert text == "3324"
    assert ev[-1]["type"] == "message_stop"
    stop = [e for e in ev if e.get("type") == "message_delta"][0]["delta"]["stop_reason"]
    assert stop == "end_turn"
    assert _outcome(*BAD) == (0, 1)


# --------------------------------------------------------------------------- #
# The pre-commit peek
# --------------------------------------------------------------------------- #

def test_peek_keeps_a_short_answer_whose_done_arrives_alone():
    assert app._peek_until_content(iter(_lines("OK", fin="stop")), 5)[0] == "content"


def test_peek_calls_unsalvageable_junk_junk_and_salvageable_content():
    chk = {"prompt_text": "What is 5 + 6505?", "last_prompt": "What is 5 + 6505?",
           "tools_offered": False}
    # ("</tool_call>..." is the older typed-tool-call detector's: nonanswer)
    junk = _lines("</arg_value></arg_value>")
    assert app._peek_until_content(iter(junk), 5, check=chk)[0] == "junk"
    salv = _lines("6510</arg_value></tool_call>6510</arg_value></tool_call>")
    assert app._peek_until_content(iter(salv), 5, check=chk)[0] == "content"


# --------------------------------------------------------------------------- #
# End to end through the three routes
# --------------------------------------------------------------------------- #

@pytest.fixture
def wire(monkeypatch):
    calls = []
    streams = {}

    def fake_dispatch(pid, payload, stream):
        calls.append(pid)
        return _StreamResp(streams[pid]())

    for name in ("_record_chat_usage", "_note_ttft", "_act_pick", "_save_perf_stats"):
        monkeypatch.setattr(app, name, lambda *a, **k: None)
    monkeypatch.setattr(app, "_dispatch_chat", fake_dispatch)
    monkeypatch.setattr(app, "_build_chain", lambda *a, **k: [BAD, GOOD])
    monkeypatch.setattr(app, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(app, "_resolve_model", lambda m: BAD)
    monkeypatch.setattr(app, "_route_by_difficulty", lambda *a, **k: (BAD[0], BAD[1], "hard"))
    return calls, streams


def test_chat_route_streams_the_salvage(wire):
    calls, streams = wire
    streams[BAD[0]] = lambda: _frames(BEST)
    streams[GOOD[0]] = lambda: _frames("REAL ANSWER", fin="stop")
    r = app.app.test_client().post("/v1/chat/completions", json={
        "model": "auto", "max_tokens": 40, "stream": True,
        "messages": [{"role": "user", "content": P_BEST}]})
    text, fins, done = _parse_chat(r.get_data())
    assert text == "3324" and fins == ["stop"] and done
    assert calls == [BAD[0]]


def test_chat_route_moves_past_unsalvageable_junk(wire):
    calls, streams = wire
    streams[BAD[0]] = lambda: _frames("</arg_value></arg_value>")
    streams[GOOD[0]] = lambda: _frames("6510", fin="stop")
    r = app.app.test_client().post("/v1/chat/completions", json={
        "model": "auto", "max_tokens": 40, "stream": True,
        "messages": [{"role": "user", "content": "What is 5 + 6505?"}]})
    assert _parse_chat(r.get_data())[0] == "6510"
    assert calls == [BAD[0], GOOD[0]]
    assert _outcome(*BAD)[1] == 1


def test_responses_route_streams_the_salvage(wire):
    calls, streams = wire
    streams[BAD[0]] = lambda: _lines(CODING, size=1)
    r = app.app.test_client().post("/v1/responses", json={
        "model": "auto", "stream": True, "input": P_CODING})
    ev = _events(r.get_data(as_text=True))
    assert "".join(e["delta"] for e in ev
                   if e.get("type") == "response.output_text.delta") == "3192"
    assert ev[-1]["type"] == "response.completed"


def test_messages_route_streams_the_salvage(wire):
    calls, streams = wire
    streams[BAD[0]] = lambda: _lines(BEST)
    r = app.app.test_client().post("/v1/messages", json={
        "model": "auto", "max_tokens": 40, "stream": True,
        "messages": [{"role": "user", "content": P_BEST}]})
    ev = _events(r.get_data(as_text=True))
    text = "".join(e["delta"].get("text", "") for e in ev
                   if e.get("type") == "content_block_delta")
    assert text == "3324"
    assert ev[-1]["type"] == "message_stop"
