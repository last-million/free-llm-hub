"""The same tool call N times in ONE response is served once.

MEASURED 2026-09-27: opencode rejected a reply with its doom_loop guard --
three IDENTICAL bash calls in one response from llm7/GLM-5.3-Flash.

Audit of the hub's own paths: the one that could re-send a call was
tool_rescue.rescue_stream -- a model that emitted a real call and then ALSO
typed it as model-native markup got the typed copy promoted into a second
call (index-colliding). Fixed at the source. Independently, an exact repeat
(same name, byte-identical arguments) within one response is collapsed to its
first copy on all three protocols, stream and non-stream; different arguments
are never touched. Fakes only, no network.
"""
import json

import pytest

import app as A
import tool_rescue as T

TOOLS = [{"type": "function", "function": {"name": "bash", "parameters": {
    "type": "object", "properties": {"command": {"type": "string"}}}}}]
LS = "{\"command\": \"ls -la\"}"
PWD = "{\"command\": \"pwd\"}"


def _call(cid, args=LS, name="bash", index=None):
    tc = {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}
    if index is not None:
        tc["index"] = index
    return tc


# --------------------------------------------------------------------------- #
# Non-stream
# --------------------------------------------------------------------------- #

def test_exact_repeats_collapse_to_the_first_copy():
    calls = [_call("a"), _call("b"), _call("c", PWD), _call("d")]
    kept, dropped = T.dedupe_calls(calls)
    assert dropped == 2
    assert [c["id"] for c in kept] == ["a", "c"]


def test_different_arguments_are_never_merged():
    calls = [_call("a"), _call("b", "{\"command\": \"ls  -la\"}"), _call("c", LS, name="sh")]
    kept, dropped = T.dedupe_calls(calls)
    assert dropped == 0 and kept == calls


def test_indexes_are_renumbered_without_a_gap():
    kept, _ = T.dedupe_calls([_call("a", index=0), _call("b", index=1),
                              _call("c", PWD, index=2)])
    assert [(c["id"], c["index"]) for c in kept] == [("a", 0), ("c", 1)]


def test_nameless_calls_are_left_alone():
    nameless = [{"id": "a", "function": {"arguments": LS}},
                {"id": "b", "function": {"arguments": LS}}]
    assert T.dedupe_calls(nameless) == (nameless, 0)


def test_dedupe_message_rewrites_the_chat_json():
    data = {"choices": [{"message": {"role": "assistant", "content": None,
                                     "tool_calls": [_call("a"), _call("b"), _call("c")]}}]}
    assert T.dedupe_message(data) == 2
    assert [c["id"] for c in data["choices"][0]["message"]["tool_calls"]] == ["a"]


def test_a_model_native_markup_repeat_is_one_call():
    one = ("<｜DSML｜invoke name=\"bash\">"
           "<｜DSML｜parameter name=\"command\" string=\"true\">ls</｜DSML｜parameter>"
           "</｜DSML｜invoke>")
    text = "<｜DSML｜function_calls>" + one * 3 + "</｜DSML｜function_calls>"
    assert len(T.parse(text, allowed_names=["bash"])) == 1


# --------------------------------------------------------------------------- #
# Stream
# --------------------------------------------------------------------------- #

def _frame(delta=None, finish=None):
    return ("data: " + json.dumps({"id": "c1", "object": "chat.completion.chunk",
                                    "created": 1, "model": "glm",
                                    "choices": [{"index": 0, "delta": delta or {},
                                                 "finish_reason": finish}]})).encode()


def _streamed_call(index, cid, args=LS, piece=4):
    """One call the way providers stream it: id + name first, then argument
    fragments with the index only."""
    out = [_frame({"tool_calls": [{"index": index, "id": cid, "type": "function",
                                   "function": {"name": "bash", "arguments": ""}}]})]
    for i in range(0, len(args), piece):
        out.append(_frame({"tool_calls": [{"index": index,
                                           "function": {"arguments": args[i:i + piece]}}]}))
    return out


def _upstream(*calls, lead=None):
    out = [_frame({"role": "assistant", "content": lead} if lead else {"role": "assistant"})]
    for c in calls:
        out += c
    return out + [_frame({}, "tool_calls"), b"data: [DONE]"]


def _decode(units):
    """([(index, id, name, args)], text, finish) from chat SSE units."""
    calls, text, fin = {}, [], None
    for u in units:
        for line in u.splitlines():
            line = line.strip()
            if not line.startswith(b"data:") or line[5:].strip() == b"[DONE]":
                continue
            obj = json.loads(line[5:])
            ch = (obj.get("choices") or [{}])[0]
            d = ch.get("delta") or {}
            if d.get("content"):
                text.append(d["content"])
            for tc in d.get("tool_calls") or []:
                slot = calls.setdefault(tc["index"], [None, "", ""])
                slot[0] = slot[0] or tc.get("id")
                slot[1] += (tc.get("function") or {}).get("name") or ""
                slot[2] += (tc.get("function") or {}).get("arguments") or ""
            fin = ch.get("finish_reason") or fin
    return [(i, *calls[i]) for i in sorted(calls)], "".join(text), fin


def test_the_same_call_three_times_is_streamed_once():
    units = _upstream(_streamed_call(0, "call_a"), _streamed_call(1, "call_b"),
                      _streamed_call(2, "call_c"))
    dropped = []
    out = list(T.dedupe_stream(iter(units), "lines",
                               on_drop=lambda n, a: dropped.append((n, a))))
    calls, _text, fin = _decode(out)
    assert calls == [(0, "call_a", "bash", LS)]
    assert fin == "tool_calls" and out[-1] == b"data: [DONE]"
    assert dropped == [("bash", LS), ("bash", LS)]


def test_frames_with_arbitrary_chunk_boundaries():
    units = _upstream(_streamed_call(0, "call_a"), _streamed_call(1, "call_b"),
                      _streamed_call(2, "call_c"))
    blob = b"".join(u + b"\n\n" for u in units)
    chunks = [blob[i:i + 11] for i in range(0, len(blob), 11)]
    out = list(T.dedupe_stream(iter(chunks), "frames"))
    assert all(u.endswith(b"\n\n") for u in out)
    calls, _t, fin = _decode(out)
    assert calls == [(0, "call_a", "bash", LS)] and fin == "tool_calls"


def test_a_repeat_between_two_different_calls_keeps_order_and_indexes():
    units = _upstream(_streamed_call(0, "call_a"), _streamed_call(1, "call_b"),
                      _streamed_call(2, "call_c", PWD))
    calls, _t, _f = _decode(list(T.dedupe_stream(iter(units), "lines")))
    assert calls == [(0, "call_a", "bash", LS), (1, "call_c", "bash", PWD)]


def test_same_prefix_then_different_arguments_is_kept_whole():
    longer = "{\"command\": \"ls -la /tmp\"}"
    units = _upstream(_streamed_call(0, "call_a"), _streamed_call(1, "call_b", longer))
    calls, _t, _f = _decode(list(T.dedupe_stream(iter(units), "lines")))
    assert calls == [(0, "call_a", "bash", LS), (1, "call_b", "bash", longer)]


def test_a_stream_nothing_could_repeat_passes_byte_for_byte():
    units = _upstream(_streamed_call(0, "call_a"), lead="Listing.")
    assert list(T.dedupe_stream(iter(units), "lines")) == units
    text_only = [_frame({"role": "assistant", "content": "4"}), _frame({}, "stop"),
                 b"data: [DONE]"]
    assert list(T.dedupe_stream(iter(text_only), "lines")) == text_only


def test_two_different_calls_to_one_tool_arrive_intact():
    """The second call is held only while its arguments could still be a
    copy of the first; what the client assembles is unchanged."""
    units = _upstream(_streamed_call(0, "call_a"), _streamed_call(1, "call_b", PWD),
                      lead="Listing.")
    out = list(T.dedupe_stream(iter(units), "lines"))
    assert _decode(out) == _decode(units)


def test_one_frame_carrying_all_three_calls():
    units = [_frame({"role": "assistant", "tool_calls": [
        _call("a", index=0), _call("b", index=1), _call("c", index=2)]}),
             _frame({}, "tool_calls"), b"data: [DONE]"]
    calls, _t, fin = _decode(list(T.dedupe_stream(iter(units), "lines")))
    assert calls == [(0, "a", "bash", LS)] and fin == "tool_calls"


def test_missing_indexes_get_one_per_call():
    """gemini-style: no index at all, each call starts with an id."""
    units = [_frame({"tool_calls": [_call("a")]}), _frame({"tool_calls": [_call("b")]}),
             _frame({"tool_calls": [_call("c", PWD)]}), _frame({}, "tool_calls"),
             b"data: [DONE]"]
    calls, _t, _f = _decode(list(T.dedupe_stream(iter(units), "lines")))
    assert calls == [(0, "a", "bash", LS), (1, "c", "bash", PWD)]


def test_rescue_stream_does_not_resend_a_real_call_the_model_also_typed():
    """THE HUB-SIDE DUPLICATE: the real call passed through, then the typed
    markup of the same call was promoted into a second call at index 0."""
    typed = ("<｜tool▁call▁begin｜>bash<｜tool▁sep｜>"
             + LS + "<｜tool▁call▁end｜>")
    units = [_frame({"role": "assistant"}),
             _frame({"tool_calls": [_call("call_a", index=0)]}),
             _frame({"content": typed}),
             _frame({}, "tool_calls"), b"data: [DONE]"]
    out = list(T.rescue_stream(iter(units), TOOLS, "lines"))
    ids = [tc.get("id") for u in out if u.startswith(b"data: {")
           for tc in (json.loads(u[6:])["choices"][0]["delta"].get("tool_calls") or [])]
    assert ids == ["call_a"]
    calls, text, fin = _decode(out)
    assert calls == [(0, "call_a", "bash", LS)] and "tool" not in text
    assert fin == "tool_calls"


# --------------------------------------------------------------------------- #
# End to end: every protocol, non-stream and stream
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


@pytest.fixture
def hub(monkeypatch):
    for name in ("_record_chat_usage", "_record_outcome", "_save_perf_stats",
                 "_act_pick", "_note_ttft", "_record_stream_outcome",
                 "_note_provider_timeout", "_throttle_failed_hop", "_note_nonanswer"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)
    monkeypatch.setattr(A, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(A, "_model_block_reason", lambda pid, m: None)
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("llm7", "GLM-5.3-Flash", "hard"))
    monkeypatch.setattr(A, "_resolve_model", lambda m: ("llm7", "GLM-5.3-Flash"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("llm7", "GLM-5.3-Flash")])
    units = _upstream(_streamed_call(0, "call_a"), _streamed_call(1, "call_b"),
                      _streamed_call(2, "call_c"))
    state = {"framed": False}

    def dispatch(pid, payload, stream):
        if stream:
            return _Resp(chunks=[u + b"\n\n" for u in units] if state["framed"] else units)
        return _Resp(payload={"choices": [{"index": 0, "finish_reason": "tool_calls",
                                           "message": {"role": "assistant", "content": None,
                                                       "tool_calls": [_call("call_a"),
                                                                      _call("call_b"),
                                                                      _call("call_c")]}}]})
    monkeypatch.setattr(A, "_dispatch_chat", dispatch)
    client = A.app.test_client()
    client.state = state
    return client


ASK = "List the files in this folder."


@pytest.mark.parametrize("stream", [False, True])
def test_chat_completions_serves_one_call(hub, stream):
    hub.state["framed"] = True
    r = hub.post("/v1/chat/completions", json={
        "model": "auto", "stream": stream, "tools": TOOLS,
        "messages": [{"role": "user", "content": ASK}]})
    if stream:
        calls, _t, fin = _decode(r.get_data().split(b"\n\n"))
        assert calls == [(0, "call_a", "bash", LS)] and fin == "tool_calls"
    else:
        tcs = r.get_json()["choices"][0]["message"]["tool_calls"]
        assert [(c["id"], c["function"]["arguments"]) for c in tcs] == [("call_a", LS)]


def _responses_calls(r, stream):
    if not stream:
        return [o for o in r.get_json()["output"] if o["type"] == "function_call"]
    done = [json.loads(line[5:]) for line in r.get_data(as_text=True).splitlines()
            if line.startswith("data:") and "response.output_item.done" in line]
    return [d["item"] for d in done if d["item"]["type"] == "function_call"]


@pytest.mark.parametrize("stream", [False, True])
def test_responses_serves_one_call(hub, stream):
    r = hub.post("/v1/responses", json={
        "model": "auto", "stream": stream, "input": ASK,
        "tools": [{"type": "function", "name": "bash",
                   "parameters": TOOLS[0]["function"]["parameters"]}]})
    fc = _responses_calls(r, stream)
    assert [(f["call_id"], f["name"], f["arguments"]) for f in fc] == [("call_a", "bash", LS)]


@pytest.mark.parametrize("stream", [False, True])
def test_messages_serves_one_call(hub, stream):
    r = hub.post("/v1/messages", json={
        "model": "auto", "max_tokens": 256, "stream": stream,
        "tools": [{"name": "bash", "input_schema": TOOLS[0]["function"]["parameters"]}],
        "messages": [{"role": "user", "content": ASK}]})
    if not stream:
        uses = [b for b in r.get_json()["content"] if b["type"] == "tool_use"]
        assert [(u["id"], u["input"]) for u in uses] == [("call_a", {"command": "ls -la"})]
        return
    events = [json.loads(line[5:]) for line in r.get_data(as_text=True).splitlines()
              if line.startswith("data:")]
    starts = [e for e in events if e.get("type") == "content_block_start"
              and e["content_block"]["type"] == "tool_use"]
    assert len(starts) == 1 and starts[0]["content_block"]["id"] == "call_a"
    idx = starts[0]["index"]
    args = "".join(e["delta"]["partial_json"] for e in events
                   if e.get("type") == "content_block_delta" and e.get("index") == idx
                   and e["delta"].get("type") == "input_json_delta")
    assert json.loads(args) == {"command": "ls -la"}
