r"""A tool call the CLI never runs, because the stream said the turn was over.

REPORTED 2026-09-09: "make sure that opencode and codex and all CLI's will work
perfectly and will not stop or crash, specially when he sees the bash".

MEASURED against the live hub, 36 models across 15 providers, each asked to run
a shell command with a `bash` tool attached and `stream: true`. 26 emitted a
tool call. Exactly one emitted a tool call a strict client cannot act on:

    google/models/gemini-flash-latest   tool_call  17.8s
        no index key; finish_reason='stop' with tool_calls

Both halves matter, and the second is the one that matches the report.

WHY finish_reason MATTERS MORE THAN IT LOOKS. opencode is the AI SDK, and its
mapping is a plain switch (read out of the shipped binary):

    case"stop":return"stop"; case"tool_calls":return"tool-calls";

"stop" ends the turn. The tool call is already parsed and buffered at that
point -- and then discarded, because the turn is over. No error, no crash, no
log line: the agent simply stops with a bash command it never ran. That is
exactly the reported shape, and it is why this was never visible as a failure.

WHY index MATTERS. Two accumulators ship in that same binary:

    let s=T.index;                      <- @ai-sdk/openai, NO fallback
    let i=(D=F.index)!=null?D:X.length; <- @ai-sdk/openai-compatible

The hub's opencode config binds openai-compatible, so a missing index is
survivable TODAY -- one npm field away from not being. Codex's translator
defaults it too (`tcd.get("index", 0)`). The Python openai SDK does not.

AND THE HUB EMITS THE SAME DEFECT ITSELF. Reproduced through the hub with
model "swarm", tools attached, streaming:

    "protocol_errors": ["tool_call index is None (NoneType), must be int"]

_swarm_stream_chunks copies the NON-streaming tool_calls list into a delta
verbatim; that shape has no index at all. Its own docstring says "a tool_calls
delta that goes missing here is a turn that writes no files", which is the
right worry aimed one field to the left.

WHY THE REPAIR IS A WRAPPER AND NOT AN EDIT TO _proxy_sse. _proxy_sse is a
deliberate byte passthrough that also enforces _STREAM_PROGRESS_DEADLINE and
the terminator guarantee. Reaching into it to parse frames would put a JSON
parser in the path of every stream the hub serves, tools or not. This wraps it
instead, and only when the request actually carries tools -- so a plain chat
stream is byte-for-byte what it was, and the parser only ever runs on the
turns that need it.

FAIL-OPEN, like the rest of the hub: anything this cannot parse is forwarded
untouched. A repair that drops a frame it did not understand would be worse
than the defect it fixes.
"""
import json

import app as A


def _run(events, tools=True):
    """Feed complete SSE frames through the repair and read back what a CLI gets."""
    raw = b"".join(events)
    out = b"".join(A._repair_tool_sse(iter([raw]))) if tools else raw
    frames = []
    for part in out.split(b"\n\n"):
        part = part.strip()
        if not part.startswith(b"data: "):
            continue
        body = part[6:]
        if body.strip() == b"[DONE]":
            frames.append("[DONE]")
            continue
        frames.append(json.loads(body))
    return frames, out


def _sse(obj):
    return b"data: " + json.dumps(obj).encode("utf-8") + b"\n\n"


def _delta(tool_calls=None, content=None, finish=None):
    d = {}
    if content is not None:
        d["content"] = content
    if tool_calls is not None:
        d["tool_calls"] = tool_calls
    return {"id": "x", "object": "chat.completion.chunk", "created": 1,
            "model": "m", "choices": [{"index": 0, "delta": d,
                                       "finish_reason": finish}]}


# --------------------------------------------------------------------------- #
# The measured defect: gemini's shape
# --------------------------------------------------------------------------- #

GEMINI = [
    _sse(_delta(tool_calls=[{"id": "call_1", "type": "function",
                             "function": {"name": "bash",
                                          "arguments": '{"command":"df -h"}'}}])),
    _sse(_delta(finish="stop")),
    b"data: [DONE]\n\n",
]


def test_a_turn_that_called_a_tool_does_not_end_with_stop():
    """The whole bug in one assertion."""
    frames, _ = _run(GEMINI)
    assert frames[-2]["choices"][0]["finish_reason"] == "tool_calls"


def test_the_tool_call_delta_gets_an_index():
    frames, _ = _run(GEMINI)
    assert frames[0]["choices"][0]["delta"]["tool_calls"][0]["index"] == 0


def test_the_call_itself_is_not_altered():
    """Repair means repair. The name and the arguments are the model's work."""
    frames, _ = _run(GEMINI)
    tc = frames[0]["choices"][0]["delta"]["tool_calls"][0]
    assert tc["id"] == "call_1"
    assert tc["function"]["name"] == "bash"
    assert json.loads(tc["function"]["arguments"]) == {"command": "df -h"}


def test_done_still_terminates_the_stream():
    frames, _ = _run(GEMINI)
    assert frames[-1] == "[DONE]"


# --------------------------------------------------------------------------- #
# A stream that was already correct must not change AT ALL
# --------------------------------------------------------------------------- #

CLEAN = [
    _sse(_delta(tool_calls=[{"index": 0, "id": "call_a", "type": "function",
                             "function": {"name": "bash", "arguments": ""}}])),
    _sse(_delta(tool_calls=[{"index": 0, "function": {"arguments": '{"cmd":'}}])),
    _sse(_delta(tool_calls=[{"index": 0, "function": {"arguments": '"ls"}'}}])),
    _sse(_delta(finish="tool_calls")),
    b"data: [DONE]\n\n",
]


def test_a_conforming_tool_stream_is_passed_through_unchanged():
    """26 of the 36 models measured were already correct. None of them may be
    touched: this repair exists for the one that was not."""
    _, out = _run(CLEAN)
    assert out == b"".join(CLEAN)


def test_a_plain_text_stream_is_passed_through_unchanged():
    events = [_sse(_delta(content="hello ")), _sse(_delta(content="world")),
              _sse(_delta(finish="stop")), b"data: [DONE]\n\n"]
    _, out = _run(events)
    assert out == b"".join(events)


def test_stop_stays_stop_when_no_tool_was_called():
    """Rewriting finish_reason on a prose turn would tell the CLI to look for a
    tool call that does not exist."""
    frames, _ = _run([_sse(_delta(content="just talking")),
                      _sse(_delta(finish="stop")), b"data: [DONE]\n\n"])
    assert frames[-2]["choices"][0]["finish_reason"] == "stop"


def test_length_is_not_rewritten_either():
    """A truncated tool call is a different failure and the CLI must see it as
    one -- 'tool_calls' would tell it to run half a command."""
    frames, _ = _run([_sse(_delta(tool_calls=[{"index": 0, "id": "c",
                                               "function": {"name": "bash",
                                                            "arguments": "{"}}])),
                      _sse(_delta(finish="length")), b"data: [DONE]\n\n"])
    assert frames[-2]["choices"][0]["finish_reason"] == "length"


# --------------------------------------------------------------------------- #
# The other shapes that make a strict client throw
# --------------------------------------------------------------------------- #

def test_a_first_delta_with_no_id_gets_one():
    """Verbatim from the shipped opencode binary:

        if(F.id==null) throw new r({data:F,message:"Expected 'id' to be a string."})

    A thrown error here is the crash half of the report, as opposed to the
    silent-stop half."""
    frames, _ = _run([_sse(_delta(tool_calls=[{"function": {"name": "bash",
                                                            "arguments": "{}"}}])),
                      _sse(_delta(finish="stop")), b"data: [DONE]\n\n"])
    tc = frames[0]["choices"][0]["delta"]["tool_calls"][0]
    assert isinstance(tc["id"], str) and tc["id"]


def test_dict_arguments_become_a_json_string():
    """The zod schema is function:{name:z.string(),arguments:z.string()} -- an
    object there fails validation before any tool runs."""
    frames, _ = _run([_sse(_delta(tool_calls=[
        {"index": 0, "id": "c", "function": {"name": "bash",
                                             "arguments": {"command": "ls"}}}])),
        _sse(_delta(finish="tool_calls")), b"data: [DONE]\n\n"])
    args = frames[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"]
    assert isinstance(args, str)
    assert json.loads(args) == {"command": "ls"}


def test_two_parallel_calls_keep_separate_indexes():
    """An id STARTS a call; a delta without one continues the current call.
    Collapsing both into index 0 would concatenate two commands into one."""
    frames, _ = _run([
        _sse(_delta(tool_calls=[{"id": "a", "function": {"name": "bash",
                                                         "arguments": '{"command":"ls"}'}}])),
        _sse(_delta(tool_calls=[{"id": "b", "function": {"name": "read",
                                                         "arguments": '{"filePath":"x"}'}}])),
        _sse(_delta(finish="stop")), b"data: [DONE]\n\n"])
    first = frames[0]["choices"][0]["delta"]["tool_calls"][0]
    second = frames[1]["choices"][0]["delta"]["tool_calls"][0]
    assert first["index"] == 0 and second["index"] == 1


def test_continuation_deltas_stay_on_the_same_call():
    """Argument fragments arrive with no id. Giving each a fresh index would
    split one command across several phantom calls."""
    frames, _ = _run([
        _sse(_delta(tool_calls=[{"id": "a", "function": {"name": "bash",
                                                         "arguments": '{"comm'}}])),
        _sse(_delta(tool_calls=[{"function": {"arguments": 'and":"ls"}'}}])),
        _sse(_delta(finish="stop")), b"data: [DONE]\n\n"])
    assert frames[0]["choices"][0]["delta"]["tool_calls"][0]["index"] == 0
    assert frames[1]["choices"][0]["delta"]["tool_calls"][0]["index"] == 0


# --------------------------------------------------------------------------- #
# Fail-open: never eat what you cannot read
# --------------------------------------------------------------------------- #

def test_an_unparseable_frame_is_forwarded_verbatim():
    junk = b"data: {not json at all\n\n"
    out = b"".join(A._repair_tool_sse(iter([junk])))
    assert out == junk


def test_sse_comments_and_keepalives_survive():
    ka = b": keepalive\n\n"
    out = b"".join(A._repair_tool_sse(iter([ka + b"data: [DONE]\n\n"])))
    assert out.startswith(ka)


def test_a_frame_split_across_chunks_is_reassembled():
    """The upstream picks the byte boundaries, not the frame boundaries."""
    whole = b"".join(GEMINI)
    pieces = [whole[i:i + 7] for i in range(0, len(whole), 7)]
    out = b"".join(A._repair_tool_sse(iter(pieces)))
    assert b'"finish_reason": "tool_calls"' in out or b'"finish_reason":"tool_calls"' in out


def test_a_trailing_frame_with_no_blank_line_is_still_emitted():
    """Nothing may be swallowed because the upstream ended without \\n\\n."""
    out = b"".join(A._repair_tool_sse(iter([b"data: [DONE]"])))
    assert b"[DONE]" in out


def test_an_empty_stream_stays_empty():
    assert b"".join(A._repair_tool_sse(iter([]))) == b""


# --------------------------------------------------------------------------- #
# The hub's own swarm stream had the same defect
# --------------------------------------------------------------------------- #

def test_the_swarm_delta_carries_an_index():
    """Measured through the live hub before the fix:
        "tool_call index is None (NoneType), must be int" """
    data = {"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "call_z", "type": "function",
         "function": {"name": "bash", "arguments": '{"command":"ls"}'}}]},
        "finish_reason": "stop"}]}
    chunks = list(A._swarm_stream_chunks(data))
    tc = chunks[0]["choices"][0]["delta"]["tool_calls"][0]
    assert tc["index"] == 0


def test_the_swarm_finish_reason_reports_the_tool_call():
    """The fan-out's own winner had finish_reason 'stop' on a turn that called a
    tool -- the same silent stop, generated by the hub rather than relayed."""
    data = {"choices": [{"message": {"tool_calls": [
        {"id": "c", "type": "function",
         "function": {"name": "bash", "arguments": "{}"}}]},
        "finish_reason": "stop"}]}
    assert list(A._swarm_stream_chunks(data))[-1]["choices"][0]["finish_reason"] \
        == "tool_calls"


def test_a_prose_swarm_answer_still_finishes_with_stop():
    data = {"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}]}
    assert list(A._swarm_stream_chunks(data))[-1]["choices"][0]["finish_reason"] == "stop"


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #

def test_the_repair_is_wired_into_the_streaming_relay():
    """Otherwise the fix exists and nothing calls it."""
    src = open("app.py", encoding="utf-8").read()
    assert src.count("_repair_tool_sse(") >= 2, "defined but never called"


def test_it_is_only_applied_to_tool_turns():
    """A plain chat stream must keep the zero-copy passthrough -- putting a JSON
    parser in front of every stream the hub serves is not a bug fix."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index("relay = _repair_tool_sse(relay)")
    assert "if has_tools:" in src[max(0, i - 200):i]
