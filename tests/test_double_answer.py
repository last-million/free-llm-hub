"""Exactly ONE answer reaches the client per request.

REPORTED 2026-09-27 (live recap runs): Claude Code `-p --model max` printed two
answers glued together on several turns, e.g.

    "...Nothing else needed. Next: what would you like to do?The last line of
     big3.txt is: ... 2000 lines total -- read with `tail -n 250`, ..."

and codex once ended a turn on "I'll read the last line of big1.txt for you."
without doing it.

WHAT WAS CHECKED, with fakes (part 1 below): the best/max quality fallback,
the trivial-turn hedge, the starved-stub retry and the three stream
translators never put a second hop's text into a response -- a response
commits ONE hop, and a hop cut before its commit is discarded, never flushed.
Checked against the real Claude Code CLI (2.1.283) with a fake server: `-p`
prints the LAST text block only, so a second block or a second request never
glues. The glued text was ONE text block -- one upstream stream carrying two
answers, the second glued on with no whitespace (the model's end-of-turn
token dropped by the provider). Part 2 reproduces that and pins the fix: the
stream gate ends the stream where the restatement starts, and not one
character of it reaches the client.

The same CLI check found a second way to a second answer: a malformed
Anthropic stream (a delta or stop for an already-stopped block) makes Claude
Code finalize the turn as "cut off mid-stream" and, in -p mode, ask the model
to resume -- a whole second request. Part 3 pins that every block is started
once and stopped once. Part 4: the announced-but-didn't-act turn.
Fakes only, tiny sleeps, no network.
"""
import json
import re
import time

import pytest

import answer_check as AC
import app as A


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class _Resp:
    def __init__(self, status=200, payload=None, lines=None):
        self.status_code = status
        self._payload = payload or {}
        self._lines = lines
        self.headers = {}
        self.text = ""
        self.closed = False

    def json(self):
        return self._payload

    def close(self):
        self.closed = True

    def iter_content(self, chunk_size=None):
        return (ln + b"\n\n" for ln in (self._lines or ()))

    def iter_lines(self, decode_unicode=False):
        return iter(self._lines or ())


def _chunk(delta, fin=None):
    return ("data: " + json.dumps({
        "id": "chatcmpl-x", "object": "chat.completion.chunk", "created": 1727400000,
        "model": "m", "choices": [{"index": 0, "delta": delta, "finish_reason": fin}]})
    ).encode()


def _token_lines(text, gap=0.0, fin="stop"):
    """One frame per word (what most providers send), then finish + [DONE]."""
    for tok in re.findall(r"\S+\s*", text):
        if gap:
            time.sleep(gap)
        yield _chunk({"content": tok})
    yield _chunk({}, fin)
    yield b"data: [DONE]"


def _tool_lines(name="bash", args='{"command": "tail -n 1 big1.txt"}'):
    yield _chunk({"role": "assistant", "tool_calls": [{
        "index": 0, "id": "call_1", "type": "function",
        "function": {"name": name, "arguments": args}}]})
    yield _chunk({}, "tool_calls")
    yield b"data: [DONE]"


def _answer_json(text=None, tool_calls=None):
    msg = {"role": "assistant", "content": text}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"index": 0, "finish_reason": "tool_calls" if tool_calls else "stop",
                         "message": msg}]}


def _chat_text(body):
    out = []
    for ln in body.splitlines():
        if ln.startswith("data:") and "[DONE]" not in ln:
            try:
                ch = json.loads(ln[5:])
            except ValueError:
                continue
            for c in ch.get("choices") or []:
                out.append((c.get("delta") or {}).get("content") or "")
    return "".join(out)


def _events(body):
    evs = []
    for ln in body.splitlines():
        if ln.startswith("data:"):
            try:
                evs.append(json.loads(ln[5:]))
            except ValueError:
                pass
    return evs


def _anthropic_text(body):
    return "".join(e["delta"].get("text", "") for e in _events(body)
                   if e.get("type") == "content_block_delta"
                   and e["delta"].get("type") == "text_delta")


def _responses_text(body):
    return "".join(e.get("delta") or "" for e in _events(body)
                   if e.get("type") == "response.output_text.delta")


def _assert_well_formed(body):
    """Every block started once, stopped once, deltas only to open blocks,
    indexes increasing, one message_stop at the very end."""
    started, stopped, last_start = set(), set(), -1
    types = []
    for e in _events(body):
        t = e.get("type")
        types.append(t)
        if t == "content_block_start":
            i = e["index"]
            assert i not in started, "block %d started twice" % i
            assert i > last_start, "block indexes must increase"
            started.add(i)
            last_start = i
        elif t == "content_block_delta":
            assert e["index"] in started and e["index"] not in stopped, \
                "delta for a block that is not open: %d" % e["index"]
        elif t == "content_block_stop":
            assert e["index"] in started and e["index"] not in stopped, \
                "stop for a block that is not open: %d" % e["index"]
            stopped.add(e["index"])
    assert started == stopped and started, (started, stopped)
    assert types.count("message_stop") == 1 and types[-1] == "message_stop", types
    assert types.count("message_delta") == 1


TOOLS_OAI = [{"type": "function", "function": {"name": "bash", "parameters": {
    "type": "object", "properties": {"command": {"type": "string"}}}}}]
TOOLS_ANTH = [{"name": "bash", "input_schema": {
    "type": "object", "properties": {"command": {"type": "string"}}}}]
TOOLS_RESP = [{"type": "function", "name": "bash",
               "parameters": TOOLS_OAI[0]["function"]["parameters"]}]


def _post(client, proto, stream, text, tools=True, model="auto"):
    if proto == "messages":
        body = {"model": model, "max_tokens": 4096, "stream": stream,
                "messages": [{"role": "user", "content": text}]}
        if tools:
            body["tools"] = TOOLS_ANTH
        return client.post("/v1/messages", json=body)
    if proto == "responses":
        body = {"model": model, "stream": stream, "input": text}
        if tools:
            body["tools"] = TOOLS_RESP
        return client.post("/v1/responses", json=body)
    body = {"model": model, "stream": stream, "messages": [{"role": "user", "content": text}]}
    if tools:
        body["tools"] = TOOLS_OAI
    return client.post("/v1/chat/completions", json=body)


def _text_of(proto, body, stream):
    if not stream:
        data = json.loads(body)
        if proto == "messages":
            return "".join(b.get("text", "") for b in data.get("content") or []
                           if b.get("type") == "text")
        if proto == "responses":
            return "".join(c.get("text", "") for item in data.get("output") or []
                           for c in item.get("content") or [] if isinstance(c, dict))
        return data["choices"][0]["message"].get("content") or ""
    return {"messages": _anthropic_text, "responses": _responses_text,
            "chat": _chat_text}[proto](body)


@pytest.fixture
def ledger(monkeypatch):
    seen = {"outcome": [], "dead": []}
    monkeypatch.setattr(A, "_record_outcome",
                        lambda p, m, ok, junk=False: seen["outcome"].append((p, m, ok)))
    monkeypatch.setattr(A, "_mark_model_dead", lambda p, m, s: seen["dead"].append((p, m)))
    monkeypatch.setattr(A, "_throttle_failed_hop", lambda *a, **k: None)
    return seen


@pytest.fixture
def hub(monkeypatch, ledger):
    """A chain of scripted hops: state["script"][pid](stream) -> _Resp."""
    for name in ("_record_chat_usage", "_save_perf_stats", "_act_pick", "_note_ttft",
                 "_record_stream_outcome", "_note_provider_timeout"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)
    monkeypatch.setattr(A, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(A, "_model_block_reason", lambda pid, m: None)
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("p1", "m1", "hard"))
    monkeypatch.setattr(A, "_resolve_model", lambda m: ("p1", "m1"))
    monkeypatch.setattr(A, "_is_trivial_turn", lambda *a, **k: False)
    state = {"script": {}, "calls": [],
             "chain": [("p1", "m1"), ("p2", "m2"), ("p3", "m3")]}
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: list(state["chain"]))

    def dispatch(pid, payload, stream):
        state["calls"].append(pid)
        return state["script"][pid](stream)
    monkeypatch.setattr(A, "_dispatch_chat", dispatch)
    client = A.app.test_client()
    client.state = state
    return client


# --------------------------------------------------------------------------- #
# Part 1: no hub path merges two hops into one response
# --------------------------------------------------------------------------- #

PRIMARY = ("The strong model explains that a hash map resolves collisions by chaining "
           "entries in buckets or by open addressing with a probe sequence.")
FALLBACK = "The quick model says: collisions are resolved by chaining or probing."
ASK = "Explain how a hash map resolves collisions."


@pytest.fixture
def best_max(hub, monkeypatch):
    monkeypatch.setattr(A, "_is_fast", lambda p, m: m == "quick")
    monkeypatch.setattr(A, "_is_trivial_ask", lambda *a, **k: False)
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: 30)
    monkeypatch.setattr(A, "_ADAPTIVE_HOP_FLOOR", 0.05)
    monkeypatch.setattr(A, "_QUALITY_FALLBACK_SHARE", 0.0)
    monkeypatch.setattr(A, "_QUALITY_FALLBACK_MIN_SECONDS", 0.4)
    hub.state["chain"] = [("nv", "strong-a"), ("nv", "strong-b"), ("fastp", "quick")]
    return hub


@pytest.mark.parametrize("proto", ["messages", "chat", "responses"])
def test_quality_fallback_discards_a_primary_it_cut_mid_answer(best_max, monkeypatch, proto):
    """The primary is WRITING when the best/max mark hits and is still writing
    when its peek grace ends: it is cut before its commit, its text is
    discarded, and the fallback is the only answer."""
    monkeypatch.setattr(A, "_PEEK_CONTENT_GRACE", 0.4)
    best_max.state["script"] = {
        "nv": lambda s: _Resp(lines=_token_lines(PRIMARY, gap=0.3)) if s
        else _Resp(payload=_answer_json(PRIMARY)),
        "fastp": lambda s: _Resp(lines=list(_token_lines(FALLBACK))) if s
        else _Resp(payload=_answer_json(FALLBACK))}
    r = _post(best_max, proto, True, ASK, model="max")
    text = _text_of(proto, r.get_data(as_text=True), True)
    assert r.headers.get("X-Free-LLM-Hub-Fallback") == "max->auto"
    assert text.strip() == FALLBACK
    assert "strong model" not in text


@pytest.mark.parametrize("proto", ["messages", "chat", "responses"])
def test_quality_fallback_never_adds_to_a_primary_that_answered(best_max, monkeypatch, proto):
    """A primary still writing at the mark but finishing inside its grace is
    committed and served ALONE -- the fallback is never also dispatched."""
    monkeypatch.setattr(A, "_PEEK_CONTENT_GRACE", 5.0)
    best_max.state["script"] = {
        "nv": lambda s: _Resp(lines=_token_lines(PRIMARY, gap=0.03)),
        "fastp": lambda s: _Resp(lines=list(_token_lines(FALLBACK)))}
    r = _post(best_max, proto, True, ASK, model="max")
    text = _text_of(proto, r.get_data(as_text=True), True)
    assert text.strip() == PRIMARY
    assert best_max.state["calls"] == ["nv"]


@pytest.mark.parametrize("proto", ["messages", "chat", "responses"])
def test_a_hedged_turn_serves_one_leg_only(hub, monkeypatch, proto):
    """Both legs of a trivial-turn hedge produce a valid answer: exactly one
    of them reaches the client."""
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=None: True if k == "hedge_simple_turns" else d)
    monkeypatch.setattr(A, "_is_trivial_turn", lambda *a, **k: True)
    monkeypatch.setattr(A, "_TRIVIAL_HOP_BUDGET", 3.0)
    monkeypatch.setattr(A, "_TRIVIAL_SLOW_HOP_BUDGET", 3.0)
    monkeypatch.setattr(A, "_HEDGE_DELAY_UNKNOWN", 0.2)
    monkeypatch.setattr(A, "_ADAPTIVE_HOP_FLOOR", 1.0)

    def late(text):
        time.sleep(0.35)                 # past the hedge delay, then answers too
        yield from _token_lines(text)
    hub.state["script"] = {
        "p1": lambda s: _Resp(lines=late("Leg zero answer: 5768.")),
        "p2": lambda s: _Resp(lines=list(_token_lines("Leg one answer: 5768.")))}
    r = _post(hub, proto, True, "What is 5767 plus 1?", tools=False)
    text = _text_of(proto, r.get_data(as_text=True), True)
    assert sorted(hub.state["calls"]) == ["p1", "p2"], "the hedge fired"
    assert text.count("answer: 5768") == 1, text


@pytest.mark.parametrize("proto", ["messages", "chat", "responses"])
def test_a_starved_retry_replaces_the_stub(hub, monkeypatch, proto):
    """A stub ended at "length" is retried on the same pair before any byte is
    committed; only the retry's answer is served."""
    tries = []

    def p1(stream):
        tries.append(1)
        if len(tries) == 1:
            return _Resp(lines=list(_token_lines("94", fin="length")))
        return _Resp(lines=list(_token_lines("The sum is 2994.")))
    hub.state["script"] = {"p1": p1}
    r = _post(hub, proto, True, "What is 2993 plus 1?", tools=False)
    text = _text_of(proto, r.get_data(as_text=True), True)
    assert len(tries) == 2
    assert text.strip() == "The sum is 2994."


# --------------------------------------------------------------------------- #
# Part 2: one upstream stream carrying two answers (the live symptom)
# --------------------------------------------------------------------------- #

ANS1 = ("The last line of big3.txt is:\n\n```\nBIG3-ENDMARK 28357 meadow-harbor\n``` "
        "2000 lines total, read via `tail -n 250`. Nothing else needed. Next: what "
        "would you like to do?")
ANS2 = ("The last line of big3.txt is:\n\n```\nBIG3-ENDMARK 28357 meadow-harbor\n``` "
        "2000 lines total -- read with `tail -n 250`, not the whole file.")
GLUED = ANS1 + ANS2                       # verbatim shape of the live reply


def test_the_live_samples_are_cut_where_the_second_answer_starts():
    q = AC.glued_restart(GLUED)
    assert q == len(ANS1)
    v = AC.inspect(GLUED)
    assert not v["ok"] and "restarted" in v["reasons"]
    assert v["salvage"] == ANS1.rstrip()
    live4 = ("The first line of big2.txt is:\n\n```\nbig2 row 00000 xxxx\n``` Read via "
             "`head -n 250`. Nothing else needed. Next: what's next?The first line of "
             "big2.txt is:\n\n```\nbig2 row 00000 xxxx\n``` Read with `head -n 250`.")
    assert live4[AC.glued_restart(live4):].startswith("The first line of big2.txt is:")


@pytest.mark.parametrize("text", [
    # an answer that restates its opening -- with whitespace, as prose does
    "The last line of big3.txt is:\n\n```\nX\n```\n\nSo, again: The last line of big3.txt is: X.",
    # quoting its own heading
    "Here is the summary of the changes:\n- a\nThe heading 'Here is the summary of the changes' stays.",
    # the opening repeated inside a code block
    "Install the package with pip first.\n```\n# Install the package with pip first.\n```",
    # an opening too plain to be distinctive
    "Yes.Yes.", "4\n4", "OK. Done.OK. Done.",
    # a paragraph, then the same paragraph as a new paragraph
    "Paris is the capital of France.\n\nParis is the capital of France.",
])
def test_a_real_answer_is_not_cut(text):
    assert AC.glued_restart(text) is None
    assert "restarted" not in AC.inspect(text)["reasons"]


def _gate_text(frames, **kw):
    g = A._StreamAnswerGate(iter(frames), mode="lines", **kw)
    out = [f for f in g]
    return "".join((json.loads(f[5:])["choices"][0]["delta"].get("content") or "")
                   for f in out if f.startswith(b"data: {")), g


@pytest.mark.parametrize("hold", [True, False])
def test_the_gate_never_releases_a_character_of_the_second_answer(hold):
    """Token by token, released early or held: the stream ends exactly where
    the restatement starts -- "The last" of the second answer never goes out,
    though it was on its way before the restatement could be recognised."""
    kw = {} if hold else {"hold_chars": 0, "hold_seconds": 0.0, "early_chars": None}
    text, g = _gate_text(list(_token_lines(GLUED)), **kw)
    assert text == ANS1
    assert g.cut and g.reasons == ["restarted"]


def test_the_gate_leaves_a_clean_stream_alone():
    two = ANS1 + "\n\nAnything else? " + "More detail follows here. " * 5
    text, g = _gate_text(list(_token_lines(two)))
    assert text == two and not g.cut


@pytest.mark.parametrize("proto", ["messages", "chat", "responses"])
def test_one_stream_with_two_answers_reaches_the_client_as_one(hub, proto):
    hub.state["script"] = {"p1": lambda s: _Resp(lines=list(_token_lines(GLUED)))}
    r = _post(hub, proto, True, "What is the last line of big3.txt?")
    text = _text_of(proto, r.get_data(as_text=True), True)
    assert text == ANS1
    assert text.count("The last line of big3.txt is:") == 1


@pytest.mark.parametrize("proto", ["messages", "chat"])
def test_a_non_streamed_reply_with_two_answers_is_served_as_one(hub, proto):
    hub.state["script"] = {"p1": lambda s: _Resp(payload=_answer_json(GLUED))}
    r = _post(hub, proto, False, "What is the last line of big3.txt?")
    text = _text_of(proto, r.get_data(as_text=True), False)
    assert text.count("The last line of big3.txt is:") == 1
    assert text.strip() == ANS1.strip()


# --------------------------------------------------------------------------- #
# Part 3: a well-formed Anthropic stream (no "resume" second request)
# --------------------------------------------------------------------------- #

def _interleaved():
    yield _chunk({"content": "Let me check the file. "})
    yield _chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                  "function": {"name": "bash", "arguments": '{"comm'}}]})
    yield _chunk({"content": "Running it now."})         # text in the middle of a call
    yield _chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'and": "ls"}'}}]})
    yield _chunk({"tool_calls": [{"index": 1, "id": "call_2", "type": "function",
                                  "function": {"name": "bash", "arguments": '{"command":'}}]})
    yield _chunk({"tool_calls": [{"index": 0, "function": {"arguments": ""}}]})
    yield _chunk({"tool_calls": [{"index": 1, "function": {"arguments": ' "pwd"}'}}]})
    yield _chunk({}, "tool_calls")
    yield b"data: [DONE]"


def test_text_between_tool_argument_deltas_keeps_the_stream_well_formed():
    body = b"".join(A._anthropic_stream(_Resp(), "max", 10,
                                        line_iter=_interleaved())).decode()
    _assert_well_formed(body)
    evs = _events(body)
    args = {}
    for e in evs:
        if e.get("type") == "content_block_delta" and e["delta"]["type"] == "input_json_delta":
            args[e["index"]] = args.get(e["index"], "") + e["delta"]["partial_json"]
    assert sorted(json.loads(v)["command"] for v in args.values()) == ["ls", "pwd"]
    assert "Running it now." in _anthropic_text(body)      # not lost, sent after the tools


def test_a_failure_after_the_last_block_never_stops_it_twice(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("usage exploded")
    monkeypatch.setattr(A, "_anthropic_final_usage", boom)
    body = b"".join(A._anthropic_stream(
        _Resp(), "max", 10, line_iter=_token_lines("Four."))).decode()
    stops = [e for e in _events(body) if e.get("type") == "content_block_stop"]
    assert [s["index"] for s in stops] == [0]


def test_an_upstream_failure_mid_stream_is_never_reported_done():
    """Was "still ends well-formed" (message_delta + message_stop on the
    partial turn): that told Claude Code the answer was complete. The body now
    just ends -- Claude Code's documented dropped-connection retry; see
    tests/test_broken_stream_retry.py. What WAS sent stays valid: no block
    stopped twice, no delta to a stopped block, the open tool block (a call
    that never finished) never stopped."""
    def dies():
        yield _chunk({"content": "Partial answer "})
        yield _chunk({"tool_calls": [{"index": 0, "id": "c", "type": "function",
                                      "function": {"name": "bash", "arguments": "{}"}}]})
        raise ConnectionError("reset")
    body = b"".join(A._anthropic_stream(_Resp(), "max", 10, line_iter=dies())).decode()
    started, stopped = set(), []
    for e in _events(body):
        if e.get("type") == "content_block_start":
            started.add(e["index"])
        elif e.get("type") == "content_block_delta":
            assert e["index"] in started and e["index"] not in stopped
        elif e.get("type") == "content_block_stop":
            assert e["index"] in started and e["index"] not in stopped
            stopped.append(e["index"])
    types = [e.get("type") for e in _events(body)]
    assert "message_stop" not in types and "message_delta" not in types
    tool_blocks = [e["index"] for e in _events(body) if e.get("type") == "content_block_start"
                   and e["content_block"]["type"] == "tool_use"]
    assert tool_blocks and not set(tool_blocks) & set(stopped)


# --------------------------------------------------------------------------- #
# Part 4: announced but didn't act (single-hop tool turns)
# --------------------------------------------------------------------------- #

ANNOUNCE = "I'll read the last line of big1.txt for you."


def test_the_live_codex_reply_is_an_announcement():
    assert A._ends_with_announcement(ANNOUNCE)
    for text in ("Let me open big1.txt and check the last line.",
                 "Sure. I'm going to run tail on the file now.",
                 "I'll now craft the layout and rebuild the CSS."):
        assert A._ends_with_announcement(text), text


@pytest.mark.parametrize("text", [
    "The last line is BIG1-ENDMARK 42. Let me know if you want me to add tests.",
    "I'm here to help.",
    "I read the file: its last line is BIG1-ENDMARK 42.",
    "I'll read it if you share the path?",
    "I'm here to help, but I currently can't run that. You can run it yourself, "
    "and I'll help you interpret it.",
    "Done -- all tests pass.",
])
def test_a_real_final_reply_is_not_an_announcement(text):
    assert not A._ends_with_announcement(text)


@pytest.mark.parametrize("proto", ["responses", "messages", "chat"])
@pytest.mark.parametrize("stream", [True, False])
def test_an_announcement_without_a_tool_call_walks_to_the_next_hop(hub, ledger, proto, stream):
    hub.state["script"] = {
        "p1": lambda s: _Resp(lines=list(_token_lines(ANNOUNCE))) if s
        else _Resp(payload=_answer_json(ANNOUNCE)),
        "p2": lambda s: _Resp(lines=list(_tool_lines())) if s
        else _Resp(payload=_answer_json(None, [{
            "id": "call_1", "type": "function",
            "function": {"name": "bash", "arguments": '{"command": "tail -n 1 big1.txt"}'}}]))}
    r = _post(hub, proto, stream, "What is the last line of big1.txt?")
    body = r.get_data(as_text=True)
    assert hub.state["calls"] == ["p1", "p2"]
    assert "I'll read the last line" not in body
    assert "tail -n 1 big1.txt" in body
    assert ("p1", "m1", False) in ledger["outcome"]
    assert ledger["dead"] == [], "one announcement is a quality failure, not a dead-mark"


@pytest.mark.parametrize("lead", [ANNOUNCE, "Let me update the manifest to add the new route."])
@pytest.mark.parametrize("proto", ["responses", "messages", "chat"])
def test_an_announcement_followed_by_its_tool_call_is_served(hub, proto, lead):
    """Per-token frames: the text passes the peek's judge point (600 bytes)
    long before the call arrives. It used to be judged there -- on "Let me
    update the" -- and thrown away; it is the turn working as intended."""
    def acts(stream):
        words = [_chunk({"content": t}) for t in re.findall(r"\S+\s*", lead)]
        return _Resp(lines=words + list(_tool_lines()))
    hub.state["script"] = {"p1": acts, "p2": lambda s: pytest.fail("p1 acted")}
    r = _post(hub, proto, True, "What is the last line of big1.txt?")
    body = r.get_data(as_text=True)
    assert hub.state["calls"] == ["p1"]
    assert "tail -n 1 big1.txt" in body


@pytest.mark.parametrize("proto", ["responses", "messages", "chat"])
def test_the_announcement_retry_happens_once_per_request(hub, proto):
    """p1 and p2 both only announce: p1 is retried on p2 ONCE, p2's reply is
    served, p3 is never tried -- a pool that keeps announcing cannot eat the
    whole deadline."""
    hub.state["script"] = {
        "p1": lambda s: _Resp(lines=list(_token_lines(ANNOUNCE))),
        "p2": lambda s: _Resp(lines=list(_token_lines("Let me check big1.txt now."))),
        "p3": lambda s: pytest.fail("the announcement retry is once per request")}
    r = _post(hub, proto, True, "What is the last line of big1.txt?")
    assert hub.state["calls"] == ["p1", "p2"]
    assert "Let me check big1.txt now." in _text_of(proto, r.get_data(as_text=True), True)
