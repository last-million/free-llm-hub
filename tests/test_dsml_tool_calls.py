"""DeepSeek's native tool-call markup typed as TEXT becomes a real tool call.

MEASURED in a live sweep of the three CLI protocols: on NON-STREAM tool turns
(/v1/responses model auto and coding, /v1/messages model auto) the reply text
started with "<｜DSML｜" -- DeepSeek V4's own tool-call syntax, fullwidth bars
(U+FF5C) -- and was served as prose, so the CLI executed nothing.

tool_rescue now parses it (plus the ASCII-bar spelling, the older
<｜tool▁call▁begin｜> token format and Kimi's), on every protocol, non-stream
and stream. Unparseable markup is a non-answer for that hop, never text.
Fakes only, no network.
"""
import json

import pytest

import app as A
import tool_rescue as T

WANT = {"a": 17, "b": 25}

DSML = ("<｜DSML｜function_calls>\n"
        "<｜DSML｜invoke name=\"add\">\n"
        "<｜DSML｜parameter name=\"a\" string=\"false\">17</｜DSML｜parameter>\n"
        "<｜DSML｜parameter name=\"b\" string=\"false\">25</｜DSML｜parameter>\n"
        "</｜DSML｜invoke>\n"
        "</｜DSML｜function_calls>")
DSML_ASCII = DSML.replace("｜", "|")
DSML_NO_HINT = DSML.replace(' string="false"', "")
OLD_V3 = ("<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>function<｜tool▁sep｜>add\n"
          "```json\n{\"a\": 17, \"b\": 25}\n```<｜tool▁call▁end｜><｜tool▁calls▁end｜>")
V31 = ("<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>add<｜tool▁sep｜>"
       "{\"a\": 17, \"b\": 25}<｜tool▁call▁end｜><｜tool▁calls▁end｜>")
V3_ASCII = OLD_V3.replace("｜", "|").replace("▁", "_")
KIMI = ("<|tool_calls_section_begin|><|tool_call_begin|>functions.add:0"
        "<|tool_call_argument_begin|>{\"a\": 17, \"b\": 25}<|tool_call_end|>"
        "<|tool_calls_section_end|>")
SAMPLES = [DSML, DSML_ASCII, DSML_NO_HINT, OLD_V3, V31, V3_ASCII, KIMI]
IDS = ["dsml", "dsml-ascii", "dsml-no-hint", "v3", "v3.1", "v3-ascii", "kimi"]

SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"},
                                           "b": {"type": "integer"}},
          "required": ["a", "b"]}
OAI_TOOLS = [{"type": "function", "function": {"name": "add", "parameters": SCHEMA}}]
RESP_TOOLS = [{"type": "function", "name": "add", "parameters": SCHEMA}]
ANTH_TOOLS = [{"name": "add", "input_schema": SCHEMA}]


# --------------------------------------------------------------------------- #
# The parser
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text", SAMPLES, ids=IDS)
def test_every_dialect_parses_to_the_offered_call(text):
    calls = T.parse(text, allowed_names=["add"])
    assert len(calls) == 1, calls
    assert calls[0]["name"] == "add"
    assert json.loads(calls[0]["arguments"]) == WANT


def test_string_true_keeps_the_text_verbatim():
    text = ("<｜DSML｜function_calls><｜DSML｜invoke name=\"read\">"
            "<｜DSML｜parameter name=\"path\" string=\"true\">007.txt</｜DSML｜parameter>"
            "</｜DSML｜invoke></｜DSML｜function_calls>")
    assert json.loads(T.parse(text, ["read"])[0]["arguments"]) == {"path": "007.txt"}


def test_the_schema_types_a_value_the_model_gave_no_hint_for():
    text = DSML_NO_HINT.replace('name="a">17', 'name="a">0017')
    schemas = {"add": {"properties": {"a": {"type": "string"}, "b": {"type": "integer"}}}}
    args = json.loads(T.parse(text, ["add"], schemas=schemas)[0]["arguments"])
    assert args == {"a": "0017", "b": 25}


def test_two_invokes_are_two_calls():
    two = DSML.replace("</｜DSML｜invoke>\n", "</｜DSML｜invoke>\n"
                       "<｜DSML｜invoke name=\"add\">\n"
                       "<｜DSML｜parameter name=\"a\" string=\"false\">1</｜DSML｜parameter>\n"
                       "<｜DSML｜parameter name=\"b\" string=\"false\">2</｜DSML｜parameter>\n"
                       "</｜DSML｜invoke>\n", 1)
    calls = T.parse(two, ["add"])
    assert [json.loads(c["arguments"]) for c in calls] == [WANT, {"a": 1, "b": 2}]


def test_an_invented_tool_name_is_not_a_call():
    assert T.parse(DSML.replace('"add"', '"subtract"'), ["add"]) == []


def _chat(content):
    return {"choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}]}


def test_rescue_keeps_the_prose_and_drops_the_markup():
    data = _chat("I'll add them.\n\n" + DSML)
    assert T.rescue(data, OAI_TOOLS) is True
    msg = data["choices"][0]["message"]
    assert msg["content"] == "I'll add them."
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == WANT
    assert data["choices"][0]["finish_reason"] == "tool_calls"


# --------------------------------------------------------------------------- #
# The verdict: parsed -> an answer; unparseable -> the next hop; no tools -> alone
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text", SAMPLES, ids=IDS)
def test_a_parsed_sample_is_an_answer(text):
    data = _chat(text)
    assert A._chat_json_nonanswer(data, True, OAI_TOOLS) is False
    assert data["choices"][0]["message"]["tool_calls"]


@pytest.mark.parametrize("text", [
    "<｜DSML｜function_calls>\n<｜DSML｜invoke>\n</｜DSML｜invoke>",     # no name
    DSML.replace('"add"', '"subtract"'),                                   # not offered
    "<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>add<｜tool▁sep｜>{\"a\": 1",  # cut JSON
])
def test_unparseable_markup_is_a_non_answer_on_a_tools_turn(text):
    assert A._chat_json_nonanswer(_chat(text), True, OAI_TOOLS) is True


def test_without_tools_the_markup_is_left_alone():
    """Only when tools were offered: explaining DSML in plain chat is an answer."""
    assert A._chat_json_nonanswer(_chat(DSML), False, None) is False


# --------------------------------------------------------------------------- #
# The stream transformer
# --------------------------------------------------------------------------- #

def _frame(delta=None, finish=None):
    return ("data: " + json.dumps({"id": "c1", "object": "chat.completion.chunk",
                                    "created": 1, "model": "deepseek-v4",
                                    "choices": [{"index": 0, "delta": delta or {},
                                                 "finish_reason": finish}]})).encode()


def _pieces(text, size=5):
    return [text[i:i + size] for i in range(0, len(text), size)]


def _upstream_lines(text, lead=""):
    out = [_frame({"role": "assistant", "content": ""})]
    out += [_frame({"content": p}) for p in _pieces(lead + text)]
    out += [_frame({}, "stop"), b"data: [DONE]"]
    return out


def _decode(units):
    """(text, {index: [name, args]}, finish, errors) from chat SSE units."""
    text, calls, fin, errors = [], {}, None, []
    for u in units:
        for line in u.splitlines():
            line = line.strip()
            if not line.startswith(b"data:") or line[5:].strip() == b"[DONE]":
                continue
            obj = json.loads(line[5:])
            if obj.get("error"):
                errors.append(obj["error"])
                continue
            ch = (obj.get("choices") or [{}])[0]
            d = ch.get("delta") or {}
            if d.get("content"):
                text.append(d["content"])
            for tc in d.get("tool_calls") or []:
                slot = calls.setdefault(tc["index"], ["", ""])
                slot[0] += (tc.get("function") or {}).get("name") or ""
                slot[1] += (tc.get("function") or {}).get("arguments") or ""
            fin = ch.get("finish_reason") or fin
    return "".join(text), calls, fin, errors


@pytest.mark.parametrize("text", SAMPLES, ids=IDS)
def test_stream_lines_become_tool_call_deltas(text):
    out = list(T.rescue_stream(iter(_upstream_lines(text, "Adding.\n")), OAI_TOOLS, "lines"))
    prose, calls, fin, errors = _decode(out)
    assert not errors
    assert "DSML" not in prose and "tool" not in prose and prose.strip() == "Adding."
    assert [c[0] for c in calls.values()] == ["add"]
    assert json.loads(calls[0][1]) == WANT
    assert fin == "tool_calls"
    assert out[-1] == b"data: [DONE]"


def _with_blank_separators(units):
    """What requests' iter_lines() really yields: b'' between SSE events."""
    out = []
    for u in units:
        out += [u, b""]
    return out


@pytest.mark.parametrize("text", SAMPLES, ids=IDS)
def test_stream_lines_with_blank_separators_become_tool_call_deltas(text):
    """The blank line after a delta ending in '<' used to flush the held
    prefix, so the rest of the opener never matched and the raw markup
    reached /v1/responses and /v1/messages clients as text."""
    lines = _with_blank_separators(_upstream_lines(text, "Adding.\n"))
    out = list(T.rescue_stream(iter(lines), OAI_TOOLS, "lines"))
    prose, calls, fin, errors = _decode(out)
    assert not errors
    assert "DSML" not in prose and "tool" not in prose and prose.strip() == "Adding."
    assert [c[0] for c in calls.values()] == ["add"]
    assert fin == "tool_calls"


def test_prose_that_mentions_a_token_in_backticks_is_not_a_call():
    text = ("Done. The parser now recognises Kimi's `<|tool_call_begin|>` token and "
            "DeepSeek's `<｜DSML｜function_calls>` block.")
    lines = _with_blank_separators(_upstream_lines(text))
    out = list(T.rescue_stream(iter(lines), OAI_TOOLS, "lines"))
    prose, calls, fin, errors = _decode(out)
    assert not errors and not calls and fin == "stop"
    assert prose == text
    assert T.has_model_markup(text) is False
    assert A._chat_json_nonanswer(_chat(text), True, OAI_TOOLS) is False


def test_stream_frames_survive_arbitrary_chunk_boundaries():
    blob = b"".join(u + b"\n\n" for u in _upstream_lines(DSML))
    chunks = [blob[i:i + 7] for i in range(0, len(blob), 7)]
    out = list(T.rescue_stream(iter(chunks), OAI_TOOLS, "frames"))
    assert all(u.endswith(b"\n\n") for u in out)
    _prose, calls, fin, _e = _decode(out)
    assert json.loads(calls[0][1]) == WANT and fin == "tool_calls"


def test_ordinary_text_passes_through_byte_for_byte():
    units = _upstream_lines("if a < b and x <| y: print('<|im_end|>')")
    assert list(T.rescue_stream(iter(units), OAI_TOOLS, "lines")) == units


def test_unparseable_stream_markup_ends_in_an_error_frame_never_text():
    bad = "<｜DSML｜function_calls>\n<｜DSML｜invoke>\n"
    out = list(T.rescue_stream(iter(_upstream_lines(bad)), OAI_TOOLS, "lines"))
    prose, calls, _fin, errors = _decode(out)
    assert errors and not calls and "DSML" not in prose


def test_no_tools_means_no_transformation():
    units = _upstream_lines(DSML)
    assert list(T.rescue_stream(iter(units), None, "lines")) == units


# --------------------------------------------------------------------------- #
# End to end, every protocol, non-stream and stream
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
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("p1", "deepseek-v4", "hard"))
    monkeypatch.setattr(A, "_resolve_model", lambda m: ("p1", "deepseek-v4"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("p1", "deepseek-v4")])

    def serve(stream_frames):
        def dispatch(pid, payload, stream):
            if stream:
                return _Resp(chunks=stream_frames)
            return _Resp(payload=_chat(DSML))
        monkeypatch.setattr(A, "_dispatch_chat", dispatch)
    serve([u + b"\n\n" for u in _upstream_lines(DSML)])
    return A.app.test_client()


ASK = "Use the add tool to add 17 and 25"


def test_chat_completions_non_stream(hub):
    r = hub.post("/v1/chat/completions", json={
        "model": "auto", "stream": False, "tools": OAI_TOOLS,
        "messages": [{"role": "user", "content": ASK}]})
    msg = r.get_json()["choices"][0]["message"]
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == WANT
    assert not msg.get("content")


def test_chat_completions_stream(hub):
    r = hub.post("/v1/chat/completions", json={
        "model": "auto", "stream": True, "tools": OAI_TOOLS,
        "messages": [{"role": "user", "content": ASK}]})
    body = r.get_data()
    prose, calls, fin, _e = _decode(body.split(b"\n\n"))
    assert b"DSML" not in body
    assert calls[0][0] == "add" and json.loads(calls[0][1]) == WANT
    assert fin == "tool_calls"


def test_responses_non_stream(hub):
    r = hub.post("/v1/responses", json={"model": "auto", "stream": False,
                                         "input": ASK, "tools": RESP_TOOLS})
    out = r.get_json()["output"]
    fc = [o for o in out if o["type"] == "function_call"]
    assert fc and fc[0]["name"] == "add" and json.loads(fc[0]["arguments"]) == WANT
    assert "DSML" not in json.dumps(out, ensure_ascii=False)


def test_responses_stream(hub):
    r = hub.post("/v1/responses", json={"model": "auto", "stream": True,
                                         "input": ASK, "tools": RESP_TOOLS})
    body = r.get_data(as_text=True)
    assert "DSML" not in body
    done = [json.loads(line[5:]) for line in body.splitlines()
            if line.startswith("data:") and "response.output_item.done" in line]
    fc = [d["item"] for d in done if d["item"]["type"] == "function_call"]
    assert fc and fc[0]["name"] == "add" and json.loads(fc[0]["arguments"]) == WANT


def test_messages_non_stream(hub):
    r = hub.post("/v1/messages", json={"model": "auto", "max_tokens": 256, "stream": False,
                                        "tools": ANTH_TOOLS,
                                        "messages": [{"role": "user", "content": ASK}]})
    blocks = r.get_json()["content"]
    use = [b for b in blocks if b["type"] == "tool_use"]
    assert use and use[0]["name"] == "add" and use[0]["input"] == WANT
    assert "DSML" not in json.dumps(blocks, ensure_ascii=False)


def test_messages_stream(hub):
    r = hub.post("/v1/messages", json={"model": "auto", "max_tokens": 256, "stream": True,
                                        "tools": ANTH_TOOLS,
                                        "messages": [{"role": "user", "content": ASK}]})
    body = r.get_data(as_text=True)
    assert "DSML" not in body
    events = [json.loads(line[5:]) for line in body.splitlines() if line.startswith("data:")]
    starts = [e["content_block"] for e in events if e.get("type") == "content_block_start"
              and e["content_block"]["type"] == "tool_use"]
    assert starts and starts[0]["name"] == "add"
    args = "".join(e["delta"]["partial_json"] for e in events
                   if e.get("type") == "content_block_delta"
                   and e["delta"].get("type") == "input_json_delta")
    assert json.loads(args) == WANT
    assert any(e.get("type") == "message_delta"
               and e["delta"]["stop_reason"] == "tool_use" for e in events)
