"""A reply the provider's content filter blocked hands the turn to the next model (2026-11-07).

MEASURED (the owner's OpenCode `coding-max`, 22:37 UTC): the turn ended on "The response was
blocked by the provider's content filter" with no text at all. Activity shows the hub served it
ok / 200 after hop 1 (a g4f HTTP 500) and a second g4f relay copy answered with
`finish_reason: "content_filter"` and nothing else. Nothing in app.py read that finish_reason
(only the Anthropic stop-reason map did), so the whitespace-only reply was committed, OpenCode
raised its ContentFilterError and the turn stopped: no other model was ever tried.

Reproduced first: a stream of role delta + ONE whitespace content delta + finish content_filter +
[DONE] was served (text "\\n", finish content_filter); a bare stream and one with real text
already walked on.

Fix: a stream that ENDS with a filter finish and no tool call, or whose content was only
whitespace, fails the hop in the peek (shared by chat, responses and messages) and the chain walks
on; a non-streamed JSON reply with a filter finish does the same, and so do the roles actor hop
and the swarm stage hop; a committed stream filtered before any visible text ends with the
retryable broken-stream frame instead. The pair is filed as a failed delivery and a recent
failure -- never dead-marked, because a filter is about THIS prompt, not about the model. At most
_FILTER_WALKS filtered hops are walked per request; the next one is served, so a request every
model blocks ends on the real cause and not on a deadline.
"""
import json

import pytest

import app as A


TOOLS = [{"type": "function", "function": {"name": "exec_command",
                                           "parameters": {"type": "object"}}}]
RELAY_A = ("g4f", "srv_aaa111:Pollinations:anthropic/claude-opus-5")
RELAY_B = ("g4f", "srv_bbb222:poe/GLM-5.3-Flash-T")
RELAY_C = ("freemodel", "srv_ccc333:poe/DeepSeek-V4.1-Flash-T")
DIRECT = ("nvidia", "z-ai/glm-5.3")
ANSWER = ("Both test files pass now. " + " ".join(
    "Change %d: I fixed the %s bound in %s.py so the last token is kept and updated its "
    "caller in %s_test.py." % (i, w, w, w)
    for i, w in enumerate(("loop", "slice", "index", "window", "offset", "margin"), 1)))


class _Resp:
    status_code = 200
    text = ""

    def __init__(self, factory):
        self.headers = {}
        self.closed = False
        self._factory = factory

    def json(self):
        return {}

    def close(self):
        self.closed = True

    def iter_content(self, chunk_size=None):
        return self._factory(self, "frames")

    def iter_lines(self, decode_unicode=False):
        return self._factory(self, "lines")


class _JsonResp:
    """A non-streamed 200 whose body is `data`."""
    status_code = 200
    text = ""

    def __init__(self, data):
        self._d = data
        self.headers = {}
        self.closed = False

    def json(self):
        return self._d

    def close(self):
        self.closed = True

    def iter_content(self, chunk_size=None):
        return iter(())

    def iter_lines(self, decode_unicode=False):
        return iter(())


def _frame(delta, mode, finish=None):
    obj = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    line = b"data: " + json.dumps(obj).encode("utf-8")
    return line if mode == "lines" else line + b"\n\n"


def _done(mode):
    return b"data: [DONE]" if mode == "lines" else b"data: [DONE]\n\n"


def _filtered(content=None, finish="content_filter", piece=7):
    """A stream that ends in a content-filter finish: an optional bit of text first."""
    def factory(resp, mode):
        yield _frame({"role": "assistant", "content": ""}, mode)
        if content is not None:
            for i in range(0, len(content), piece):
                yield _frame({"content": content[i:i + piece]}, mode)
        yield _frame({}, mode, finish=finish)
        yield _done(mode)
    return factory


def _says(text, piece=7):
    def factory(resp, mode):
        yield _frame({"role": "assistant", "content": ""}, mode)
        for i in range(0, len(text), piece):
            yield _frame({"content": text[i:i + piece]}, mode)
        yield _frame({}, mode, finish="stop")
        yield _done(mode)
    return factory


def _blank_then_filter(resp, mode):
    # enough whitespace frames (> _PEEK_JUDGE_CHARS bytes) that the peek commits on them
    yield _frame({"role": "assistant", "content": ""}, mode)
    for _ in range(14):
        yield _frame({"content": " \n"}, mode)
    yield _frame({}, mode, finish="content_filter")
    yield _done(mode)


def _text_then_filter(resp, mode):
    yield _frame({"role": "assistant", "content": ""}, mode)
    for i in range(0, len(ANSWER), 7):
        yield _frame({"content": ANSWER[i:i + 7]}, mode)
    yield _frame({}, mode, finish="content_filter")
    yield _done(mode)


def _json_reply(content, finish="stop", tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"id": "chatcmpl-x", "object": "chat.completion", "created": 1, "model": "m",
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


@pytest.fixture
def quiet(monkeypatch):
    for name in ("_record_chat_usage", "_save_perf_stats", "_act_pick", "_note_ttft",
                 "_record_stream_outcome", "_note_provider_timeout", "_throttle_failed_hop",
                 "_v1_observe"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)
    monkeypatch.setattr(A, "_record_outcome", lambda *a, **k: None)
    monkeypatch.setattr(A, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(A, "_model_block_reason", lambda pid, m: None)
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    monkeypatch.setattr(A, "_is_trivial_turn", lambda *a, **k: False)
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: 30)
    monkeypatch.setattr(A, "_stream_peek_timeout", lambda *a, **k: 5.0)


@pytest.fixture(autouse=True)
def _clean_ledgers():
    A._recent_hop_fail.clear()
    A._filter_retries.clear()
    yield
    A._recent_hop_fail.clear()
    A._filter_retries.clear()


@pytest.fixture
def flag_off(monkeypatch):
    real = A.config.get_flag
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=None: False if k == "content_filter_handover" else real(k, d))


def _fleet(monkeypatch, hops):
    """hops: [(pid, model, factory-or-json-dict)] in chain order; returns the call log."""
    monkeypatch.setattr(A, "_route_by_difficulty",
                        lambda *a, **k: (hops[0][0], hops[0][1], "hard"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [(p, m) for p, m, _f in hops])
    table = {(p, m): f for p, m, f in hops}
    calls = []

    def dispatch(pid, payload, stream):
        calls.append((pid, payload.get("model")))
        f = table[(pid, payload.get("model"))]
        return _JsonResp(f) if isinstance(f, dict) else _Resp(f)
    monkeypatch.setattr(A, "_dispatch_chat", dispatch)
    return calls


def _read(body):
    """(assembled assistant text, tool-call names, finish reasons) of an SSE chat body."""
    text, tools, fins = [], [], []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block.startswith("data:") or block.endswith("[DONE]"):
            continue
        try:
            obj = json.loads(block[5:].strip())
        except ValueError:
            continue
        for ch in obj.get("choices") or []:
            delta = ch.get("delta") or {}
            if delta.get("content"):
                text.append(delta["content"])
            for tc in delta.get("tool_calls") or []:
                name = (tc.get("function") or {}).get("name")
                if name:
                    tools.append(name)
            if ch.get("finish_reason"):
                fins.append(ch["finish_reason"])
    return "".join(text), tools, fins


def _post(tools=True, stream=True):
    body = {"model": "auto", "stream": stream,
            "messages": [
                {"role": "user", "content": "run the tests and fix what fails"},
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "c0", "type": "function",
                     "function": {"name": "exec_command", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "c0", "content": "2 failed"},
                {"role": "user", "content": "continue"}]}
    if tools:
        body["tools"] = TOOLS
    return A.app.test_client().post("/v1/chat/completions", json=body)


# --------------------------------------------------------------------------- #
# The peek, on its own (shared by chat, responses and messages)
# --------------------------------------------------------------------------- #

def _stream_items(content, finish, mode="frames", piece=7, tool_call=False):
    items = [_frame({"role": "assistant", "content": ""}, mode)]
    if content:
        for i in range(0, len(content), piece):
            items.append(_frame({"content": content[i:i + piece]}, mode))
    if tool_call:
        items.append(_frame({"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                             "function": {"name": "exec_command",
                                                          "arguments": "{}"}}]}, mode))
    items.append(_frame({}, mode, finish=finish))
    items.append(_done(mode))
    return items


def _peek(items, check=None):
    status, buf = A._peek_until_content(iter(items), 5, check=check)
    return status, A._take_nonanswer_kind(), len(buf), len(items)


@pytest.mark.parametrize("mode", ["frames", "lines"])
@pytest.mark.parametrize("content", [None, "\n", " \n ",
                                     "The scanner module is done and its notes are"])
def test_a_stream_that_ends_in_a_content_filter_fails_the_hop(mode, content):
    # piece=30: under the ~600 frame bytes where the peek gives its first verdict (per-token
    # frames reach that after ~7 tokens; a filter later than that finds the stream committed,
    # which the gate's retryable ending covers -- see the committed-stream tests below)
    status, kind, _n, _total = _peek(_stream_items(content, "content_filter", mode, piece=30))
    assert (status, kind) == ("nonanswer", "content_filter")


@pytest.mark.parametrize("finish", ["content_filter", "content-filter", "CONTENT_FILTER", "SAFETY",
                                    "safety", "refusal", "blocked", "PROHIBITED_CONTENT",
                                    "BLOCKLIST", "SPII", "RECITATION", "image_safety"])
def test_every_spelling_of_a_provider_block_is_read(finish):
    status, kind, _n, _total = _peek(_stream_items("\n", finish))
    assert (status, kind) == ("nonanswer", "content_filter")


@pytest.mark.parametrize("finish", ["stop", "length", "tool_calls", "function_call", None,
                                    "end_turn", "max_tokens"])
def test_an_ordinary_finish_is_not_a_block(finish):
    assert not A._is_filter_finish(finish)
    status, kind, _n, _total = _peek(_stream_items("Both test files pass now. " * 3, finish))
    assert (status, kind) == ("content", None)


def test_a_filtered_stream_is_judged_on_tool_turns_and_relay_hops_too():
    for check in ({"tools_offered": True, "tools": TOOLS, "last_prompt": "continue",
                   "prompt_text": "continue", "relay": True, "pid": "g4f", "model": RELAY_A[1]},
                  {"tools_offered": False, "last_prompt": "continue", "prompt_text": "continue",
                   "relay": False, "pid": "nvidia", "model": "m"}):
        status, kind, _n, _total = _peek(_stream_items("\n", "content_filter"), check=check)
        assert (status, kind) == ("nonanswer", "content_filter"), check


def test_a_tool_call_commits_at_once_and_is_never_called_filtered():
    status, kind, _n, _total = _peek(_stream_items(None, "content_filter", tool_call=True),
                                     check={"tools_offered": True, "tools": TOOLS,
                                            "last_prompt": "go", "prompt_text": "go"})
    assert (status, kind) == ("content", None)


def test_a_whitespace_only_answer_is_an_empty_hop_even_without_a_filter():
    # the MEASURED shape of the incident with an ordinary finish: the content frames held only
    # whitespace, so there was nothing to show -- it used to be committed as content
    for content in ("\n", "   ", " \n\n "):
        status, kind, _n, _total = _peek(_stream_items(content, "stop"))
        assert (status, kind) == ("empty", None), repr(content)


def test_a_bare_stream_with_no_content_frame_is_still_an_empty_hop_without_a_filter():
    assert _peek(_stream_items(None, "stop"))[:2] == ("empty", None)


def test_a_real_short_answer_is_still_served():
    status, kind, n, total = _peek(_stream_items("4", "stop", piece=1))
    assert (status, kind) == ("content", None) and n == total


def test_the_flag_off_keeps_the_old_peek(flag_off):
    # bare: empty as before; whitespace (+ filter): committed as content, as before
    assert _peek(_stream_items(None, "content_filter"))[:2] == ("empty", None)
    assert _peek(_stream_items("\n", "content_filter"))[:2] == ("content", None)
    assert _peek(_stream_items("\n", "stop"))[:2] == ("content", None)


def test_a_flag_read_that_raises_counts_as_off(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("config unreadable")
    monkeypatch.setattr(A.config, "get_flag", boom)
    assert A._content_filter_on() is False


# --------------------------------------------------------------------------- #
# End to end through the real chat handler (the path OpenCode uses)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("shape", ["bare", "whitespace", "partial_text"])
@pytest.mark.parametrize("tools", [True, False])
def test_e2e_chat_a_filtered_stream_hands_the_turn_to_the_next_hop(quiet, monkeypatch, shape,
                                                                   tools):
    content = {"bare": None, "whitespace": "\n",
               "partial_text": "The scanner module is done and its notes are"}[shape]
    calls = _fleet(monkeypatch, [(*RELAY_A, _filtered(content, piece=30)),
                                 (*DIRECT, _says(ANSWER))])
    r = _post(tools=tools)
    body = r.get_data().decode("utf-8")
    text, _tools, fins = _read(body)
    assert calls == [RELAY_A, DIRECT], (shape, tools, text, fins)
    assert r.headers.get("X-Free-LLM-Hub-Provider") == "nvidia"
    assert text == ANSWER and fins == ["stop"]


def test_e2e_chat_the_blocked_pair_is_demoted_not_dead_marked(quiet, monkeypatch):
    _fleet(monkeypatch, [(*RELAY_A, _filtered("\n")), (*DIRECT, _says(ANSWER))])
    _post()
    assert A._recent_hop_failure(*RELAY_A) == "content_filter"
    assert A._is_model_dead(*RELAY_A) is False
    assert A._is_model_dead(*DIRECT) is False


def test_e2e_chat_a_filtered_hop_is_filed_as_a_failed_delivery(monkeypatch, quiet):
    filed = []
    monkeypatch.setattr(A, "_record_outcome", lambda pid, m, ok, **k: filed.append((pid, m, ok)))
    _fleet(monkeypatch, [(*RELAY_A, _filtered("\n")), (*DIRECT, _says(ANSWER))])
    _post()
    assert (RELAY_A[0], RELAY_A[1], False) in filed


def test_e2e_chat_a_relay_servers_other_models_are_skipped_in_the_same_walk(quiet, monkeypatch):
    # the filter belongs to the gateway: on a tool turn a failed relay hop rests its whole
    # server for the rest of the walk (_ChainClock._close_hop), so the sibling is not tried
    sib1 = ("g4f", "srv_aaa111:poe/DeepSeek-V4.1-Flash-T")
    sib2 = ("g4f", "srv_aaa111:poe/GLM-5.3-Flash-T")
    calls = _fleet(monkeypatch, [(*sib1, _filtered("\n")), (*sib2, _says(ANSWER)),
                                 (*DIRECT, _says(ANSWER))])
    r = _post(tools=True)
    assert calls == [sib1, DIRECT]
    assert r.headers.get("X-Free-LLM-Hub-Provider") == "nvidia"


def test_e2e_chat_a_request_every_model_blocks_ends_on_the_real_cause(quiet, monkeypatch):
    # _FILTER_WALKS (2) filtered hops walk on; the third is served, so the client reads the
    # provider's own "content filter" and not a deadline -- and the fourth hop never runs
    calls = _fleet(monkeypatch, [(*RELAY_A, _filtered("\n")), (*RELAY_B, _filtered("\n")),
                                 (*RELAY_C, _filtered("\n")), (*DIRECT, _says(ANSWER))])
    r = _post(tools=False)
    body = r.get_data().decode("utf-8")
    _text, _tools, fins = _read(body)
    assert calls == [RELAY_A, RELAY_B, RELAY_C]
    assert r.status_code == 200
    assert fins == ["content_filter"]            # the real cause reaches the CLI, once
    assert "ECONNRESET" not in body              # ...and is not turned into a retry loop


def test_e2e_chat_the_flag_off_serves_the_filtered_reply_as_before(quiet, monkeypatch, flag_off):
    calls = _fleet(monkeypatch, [(*RELAY_A, _filtered("\n")), (*DIRECT, _says(ANSWER))])
    r = _post()
    _text, _tools, fins = _read(r.get_data().decode("utf-8"))
    assert calls == [RELAY_A] and fins == ["content_filter"]


@pytest.mark.parametrize("tools", [True, False])
def test_e2e_chat_a_non_streamed_filtered_reply_hands_the_turn_over(quiet, monkeypatch, tools):
    blocked = _json_reply("", finish="content_filter")
    good = _json_reply(ANSWER)
    calls = _fleet(monkeypatch, [(*RELAY_A, blocked), (*DIRECT, good)])
    r = _post(tools=tools, stream=False)
    assert r.status_code == 200
    assert calls == [RELAY_A, DIRECT]
    data = r.get_json()
    assert data["choices"][0]["message"]["content"] == ANSWER
    assert data["choices"][0]["finish_reason"] == "stop"
    assert A._recent_hop_failure(*RELAY_A) == "content_filter"
    assert A._is_model_dead(*RELAY_A) is False


def test_e2e_chat_a_non_streamed_reply_cut_by_the_filter_after_some_text_also_hands_over(
        quiet, monkeypatch):
    partial = _json_reply("The scanner module is done and its notes are", finish="content_filter")
    calls = _fleet(monkeypatch, [(*RELAY_A, partial), (*DIRECT, _json_reply(ANSWER))])
    r = _post(tools=False, stream=False)
    assert calls == [RELAY_A, DIRECT]
    assert r.get_json()["choices"][0]["message"]["content"] == ANSWER


def test_e2e_chat_a_non_streamed_tool_call_is_never_called_filtered(quiet, monkeypatch):
    call = [{"id": "c9", "type": "function",
             "function": {"name": "exec_command", "arguments": "{}"}}]
    data = _json_reply(None, finish="content_filter", tool_calls=call)
    calls = _fleet(monkeypatch, [(*RELAY_A, data), (*DIRECT, _json_reply(ANSWER))])
    _post(tools=True, stream=False)
    assert calls == [RELAY_A]


def test_e2e_chat_a_non_streamed_blocked_reply_with_text_is_served_when_the_walk_spent_its_hops(
        quiet, monkeypatch):
    blocked = _json_reply("The scanner module is done and its notes are", finish="content_filter")
    calls = _fleet(monkeypatch, [(*RELAY_A, blocked), (*RELAY_B, blocked), (*RELAY_C, blocked),
                                 (*DIRECT, _json_reply(ANSWER))])
    r = _post(tools=False, stream=False)
    # two hops walk on; the third blocked reply is the one that is served (hop 4 never runs)
    assert calls == [RELAY_A, RELAY_B, RELAY_C]
    assert r.get_json()["choices"][0]["finish_reason"] == "content_filter"


def test_e2e_chat_a_non_streamed_blank_blocked_reply_keeps_walking_like_any_empty_reply(
        quiet, monkeypatch):
    # no text to serve: the existing empty-200 rule walks on to the end of the chain
    blocked = _json_reply("", finish="content_filter")
    calls = _fleet(monkeypatch, [(*RELAY_A, blocked), (*RELAY_B, blocked), (*RELAY_C, blocked),
                                 (*DIRECT, _json_reply(ANSWER))])
    r = _post(tools=False, stream=False)
    assert calls == [RELAY_A, RELAY_B, RELAY_C, DIRECT]
    assert r.get_json()["choices"][0]["message"]["content"] == ANSWER


# --------------------------------------------------------------------------- #
# A COMMITTED stream: filtered before any visible text -> the retryable ending
# --------------------------------------------------------------------------- #

def test_e2e_chat_a_committed_stream_filtered_before_any_text_ends_with_the_retryable_frame(
        quiet, monkeypatch):
    _fleet(monkeypatch, [(*RELAY_A, _blank_then_filter), (*DIRECT, _says(ANSWER))])
    r = _post(tools=False)
    body = r.get_data().decode("utf-8")
    text, _tools, fins = _read(body)
    assert "ECONNRESET" in body and "server_error" in body       # what OpenCode retries on
    assert fins == []                                            # no finish_reason, no content-filter
    assert "[DONE]" not in body                                  # [DONE] = "complete" to a CLI
    assert text.strip() == ""
    assert A._recent_hop_failure(*RELAY_A) == "content_filter"   # so the retry starts elsewhere
    assert A._is_model_dead(*RELAY_A) is False


def test_e2e_chat_a_filter_a_few_words_in_also_ends_with_the_retryable_frame(quiet, monkeypatch):
    # per-token frames: the peek has committed by the time the finish arrives. The user does
    # not have to type "continue": the CLI's own retry reaches another model.
    _fleet(monkeypatch, [(*RELAY_A, _text_then_filter), (*DIRECT, _says(ANSWER))])
    r = _post(tools=False)
    body = r.get_data().decode("utf-8")
    text, _tools, fins = _read(body)
    assert "ECONNRESET" in body and "[DONE]" not in body
    assert fins == []                                            # never "content_filter"
    assert text and ANSWER.startswith(text)                      # what was shown stays shown
    assert A._recent_hop_failure(*RELAY_A) == "content_filter"


def test_e2e_chat_a_stream_with_a_tool_call_passes_a_filter_finish_unchanged(quiet, monkeypatch):
    def call_then_filter(resp, mode):
        yield _frame({"role": "assistant", "tool_calls": [
            {"index": 0, "id": "call_77", "type": "function",
             "function": {"name": "exec_command", "arguments": "{\"cmd\": \"pytest"}}]}, mode)
        yield _frame({}, mode, finish="content_filter")
        yield _done(mode)
    _fleet(monkeypatch, [(*RELAY_A, call_then_filter), (*DIRECT, _says(ANSWER))])
    body = _post(tools=True).get_data().decode("utf-8")
    _text, tools, fins = _read(body)
    assert tools == ["exec_command"] and fins == ["content_filter"]
    assert "ECONNRESET" not in body


def test_e2e_chat_the_committed_retry_is_bounded_per_conversation(quiet, monkeypatch):
    _fleet(monkeypatch, [(*RELAY_A, _blank_then_filter), (*DIRECT, _says(ANSWER))])
    seen = []
    for _ in range(A._FILTER_RETRIES + 2):
        body = _post(tools=False).get_data().decode("utf-8")
        seen.append("ECONNRESET" in body)
    assert seen[:A._FILTER_RETRIES] == [True] * A._FILTER_RETRIES
    assert seen[A._FILTER_RETRIES:] == [False, False]            # then the real cause, no loop


def test_e2e_chat_a_committed_filter_is_not_retried_once_the_walk_spent_its_hops(
        quiet, monkeypatch):
    _fleet(monkeypatch, [(*RELAY_A, _filtered("\n")), (*RELAY_B, _filtered("\n")),
                         (*RELAY_C, _blank_then_filter), (*DIRECT, _says(ANSWER))])
    body = _post(tools=False).get_data().decode("utf-8")
    assert "ECONNRESET" not in body


def test_e2e_chat_the_flag_off_commits_a_blank_stream_as_before(quiet, monkeypatch, flag_off):
    _fleet(monkeypatch, [(*RELAY_A, _blank_then_filter), (*DIRECT, _says(ANSWER))])
    body = _post(tools=False).get_data().decode("utf-8")
    _text, _tools, fins = _read(body)
    assert fins == ["content_filter"] and "ECONNRESET" not in body


# --------------------------------------------------------------------------- #
# The other two protocols share the peek and the gate
# --------------------------------------------------------------------------- #

def _post_responses():
    return A.app.test_client().post("/v1/responses", json={
        "model": "auto", "stream": True,
        "input": [{"role": "user", "content": "explain the failing test"}]})


def _post_messages():
    return A.app.test_client().post("/v1/messages", json={
        "model": "claude-sonnet-4", "max_tokens": 1024, "stream": True,
        "messages": [{"role": "user", "content": "explain the failing test"}]})


def _sse_objects(body):
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:") and not line.endswith("[DONE]"):
            try:
                yield json.loads(line[5:].strip())
            except ValueError:
                continue


def _responses_text(body):
    return "".join(o.get("delta") or "" for o in _sse_objects(body)
                   if o.get("type") == "response.output_text.delta")


def _messages_text(body):
    return "".join((o.get("delta") or {}).get("text") or "" for o in _sse_objects(body)
                   if o.get("type") == "content_block_delta"
                   and (o.get("delta") or {}).get("type") == "text_delta")


@pytest.mark.parametrize("content", [None, "\n"])
def test_e2e_responses_a_filtered_stream_hands_the_turn_to_the_next_hop(quiet, monkeypatch,
                                                                        content):
    calls = _fleet(monkeypatch, [(*RELAY_A, _filtered(content)), (*DIRECT, _says(ANSWER))])
    body = _post_responses().get_data().decode("utf-8")
    assert calls == [RELAY_A, DIRECT]
    assert _responses_text(body) == ANSWER and "response.completed" in body


@pytest.mark.parametrize("content", [None, "\n"])
def test_e2e_messages_a_filtered_stream_hands_the_turn_to_the_next_hop(quiet, monkeypatch,
                                                                       content):
    calls = _fleet(monkeypatch, [(*RELAY_A, _filtered(content)), (*DIRECT, _says(ANSWER))])
    body = _post_messages().get_data().decode("utf-8")
    assert calls == [RELAY_A, DIRECT]
    assert _messages_text(body) == ANSWER


def test_e2e_responses_a_committed_blank_stream_ends_as_a_retryable_failure(quiet, monkeypatch):
    _fleet(monkeypatch, [(*RELAY_A, _blank_then_filter), (*DIRECT, _says(ANSWER))])
    body = _post_responses().get_data().decode("utf-8")
    assert "response.failed" in body and "upstream_error" in body
    assert "response.completed" not in body


def test_e2e_messages_a_committed_blank_stream_ends_without_a_clean_stop(quiet, monkeypatch):
    _fleet(monkeypatch, [(*RELAY_A, _blank_then_filter), (*DIRECT, _says(ANSWER))])
    body = _post_messages().get_data().decode("utf-8")
    assert "message_stop" not in body          # a cleanly ended body would read as complete


# --------------------------------------------------------------------------- #
# The roles actor hop and the swarm stage hop
# --------------------------------------------------------------------------- #

class _Plain:
    status_code = 200

    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d

    def close(self):
        pass


ROLE_BODY = {"model": "m", "messages": [{"role": "user", "content": "go"}], "tools": TOOLS}


def test_the_roles_actor_hop_fails_over_a_filtered_reply(quiet):
    v = A._role_judge("g4f", "srv_a:m", _Plain(_json_reply("partial words here",
                                                          finish="content_filter")),
                      None, ROLE_BODY, dict(ROLE_BODY), 1000, "coding|hard|tools|m")
    assert v["ok"] is False and v["fail"] == "filtered"
    assert A._recent_hop_failure("g4f", "srv_a:m") == "content_filter"
    assert A._is_model_dead("g4f", "srv_a:m") is False


def test_the_roles_actor_hop_keeps_a_filtered_tool_call_and_a_clean_reply(quiet):
    call = [{"id": "c9", "type": "function",
             "function": {"name": "exec_command", "arguments": "{}"}}]
    v = A._role_judge("g4f", "srv_a:m", _Plain(_json_reply(None, finish="content_filter",
                                                          tool_calls=call)),
                      None, ROLE_BODY, dict(ROLE_BODY), 1000, "coding|hard|tools|m")
    assert v["ok"] is True
    v = A._role_judge("nvidia", "z-ai/glm-5.3", _Plain(_json_reply(ANSWER)),
                      None, ROLE_BODY, dict(ROLE_BODY), 1000, "coding|hard|tools|m")
    assert v["ok"] is True


def test_the_roles_actor_hop_flag_off_serves_the_filtered_reply(quiet, flag_off):
    v = A._role_judge("g4f", "srv_a:m", _Plain(_json_reply(ANSWER, finish="content_filter")),
                      None, ROLE_BODY, dict(ROLE_BODY), 1000, "coding|hard|tools|m")
    assert v["ok"] is True


@pytest.fixture
def stage(monkeypatch):
    chain = [("p1", "m1"), ("p2", "m2")]
    answers = {}
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("p1", "m1", "hard"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: list(chain))
    monkeypatch.setattr(A, "_est_tokens", lambda *a, **k: 100)
    monkeypatch.setattr(A, "_record_outcome", lambda *a, **k: None)
    monkeypatch.setattr(A, "_record_chat_usage", lambda *a, **k: None)
    monkeypatch.setattr(A, "_act_pick", lambda *a, **k: None)
    monkeypatch.setattr(A, "_pipeline_time_left", lambda: None)
    monkeypatch.setattr(A, "_answer_gate", lambda *a, **k: "ok")
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: 100.0)
    monkeypatch.setattr(A, "_is_low_quality", lambda m: False)
    monkeypatch.setattr(A, "_thinks_by_default", lambda pid, m: False)
    monkeypatch.setattr(A, "_measured_latency_ms", lambda pid, m: None)
    monkeypatch.setattr(A, "_is_slow_model", lambda pid, m: False)
    monkeypatch.setattr(A, "_supports_tools", lambda pid, m: True)
    monkeypatch.setattr(A, "_model_output_cap", lambda pid, m: None)
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline",
                        lambda pid, payload, deadline: (answers[payload["model"]], None))
    return answers


def test_a_swarm_stage_walks_past_a_filtered_hop(stage):
    stage["m1"] = _Plain(_json_reply("a half sentence that the filter", finish="content_filter"))
    stage["m2"] = _Plain(_json_reply(ANSWER))
    text, who = A._swarm_dispatch([{"role": "user", "content": "write it"}], 100)
    assert (text, who) == (ANSWER, "p2/m2")
    assert A._recent_hop_failure("p1", "m1") == "content_filter"


def test_a_swarm_stage_keeps_the_longest_partial_when_every_hop_is_blocked(stage):
    stage["m1"] = _Plain(_json_reply("short partial", finish="content_filter"))
    stage["m2"] = _Plain(_json_reply("a somewhat longer partial", finish="content_filter"))
    text, who = A._swarm_dispatch([{"role": "user", "content": "write it"}], 100)
    assert text == "a somewhat longer partial" and who == "p2/m2"


def test_a_swarm_stage_flag_off_serves_the_filtered_hop(stage, flag_off):
    stage["m1"] = _Plain(_json_reply(ANSWER, finish="content_filter"))
    stage["m2"] = _Plain(_json_reply("other"))
    text, who = A._swarm_dispatch([{"role": "user", "content": "write it"}], 100)
    assert (text, who) == (ANSWER, "p1/m1")


# --------------------------------------------------------------------------- #
# Every client: OpenCode, Hermes, OpenClaw, qwen, kimi, pi, aider (chat), Codex (responses),
# Claude Code (messages) -- and the surfaces built on the chat router (/v1/completions,
# Ollama, Gemini). The fix sits in the shared peek / gate / JSON paths, so it is client-blind.
# --------------------------------------------------------------------------- #

CLIENT_AGENTS = ["opencode/1.19.0", "hermes-agent/0.4", "openclaw/2026.3", "qwen-code/0.2",
                 "kimi-code/0.39.1", "pi-coding-agent/0.8", "aider/0.86 litellm", "python-httpx/0.27",
                 "OpenAI/Python 1.40", "llm/0.20"]


@pytest.mark.parametrize("agent", CLIENT_AGENTS)
@pytest.mark.parametrize("stream", [True, False])
def test_e2e_every_chat_client_gets_the_next_model_when_one_is_blocked(quiet, monkeypatch,
                                                                       agent, stream):
    if stream:
        hops = [(*RELAY_A, _filtered("\n")), (*DIRECT, _says(ANSWER))]
    else:
        hops = [(*RELAY_A, _json_reply("", finish="content_filter")),
                (*DIRECT, _json_reply(ANSWER))]
    calls = _fleet(monkeypatch, hops)
    r = A.app.test_client().post("/v1/chat/completions", headers={"User-Agent": agent}, json={
        "model": "auto", "stream": stream,
        "messages": [{"role": "user", "content": "explain the failing test"}]})
    assert calls == [RELAY_A, DIRECT], agent
    if stream:
        text, _t, fins = _read(r.get_data().decode("utf-8"))
        assert text == ANSWER and fins == ["stop"]
    else:
        assert r.get_json()["choices"][0]["message"]["content"] == ANSWER


def test_e2e_the_completions_surface_gets_the_next_model(quiet, monkeypatch):
    calls = _fleet(monkeypatch, [(*RELAY_A, _filtered("\n")), (*DIRECT, _says(ANSWER))])
    r = A.app.test_client().post("/v1/completions", json={
        "model": "auto", "prompt": "explain the failing test", "stream": True})
    body = r.get_data().decode("utf-8")
    assert calls == [RELAY_A, DIRECT]
    text = "".join(((o.get("choices") or [{}])[0]).get("text") or "" for o in _sse_objects(body))
    assert text == ANSWER


def test_e2e_the_gemini_surface_gets_the_next_model(quiet, monkeypatch):
    calls = _fleet(monkeypatch, [(*RELAY_A, _filtered("\n")), (*DIRECT, _says(ANSWER))])
    r = A.app.test_client().post("/v1beta/models/auto:streamGenerateContent?alt=sse", json={
        "contents": [{"role": "user", "parts": [{"text": "explain the failing test"}]}]})
    body = r.get_data().decode("utf-8")
    assert calls == [RELAY_A, DIRECT]
    text = "".join(p.get("text") or "" for o in _sse_objects(body)
                   for c in (o.get("candidates") or [])
                   for p in ((c.get("content") or {}).get("parts") or []))
    assert text == ANSWER


def test_e2e_the_ollama_surface_gets_the_next_model(quiet, monkeypatch):
    monkeypatch.setattr(A, "_ollama_enabled", lambda: True)
    calls = _fleet(monkeypatch, [(*RELAY_A, _filtered("\n")), (*DIRECT, _says(ANSWER))])
    r = A.app.test_client().post("/api/chat", json={
        "model": "auto", "stream": True,
        "messages": [{"role": "user", "content": "explain the failing test"}]})
    body = r.get_data().decode("utf-8")
    assert calls == [RELAY_A, DIRECT]
    text = ""
    for line in body.splitlines():
        try:
            text += (json.loads(line).get("message") or {}).get("content") or ""
        except ValueError:
            continue
    assert text == ANSWER


def test_e2e_a_committed_filter_ends_each_surface_in_its_own_error_shape(quiet, monkeypatch):
    # the chat router ends the stream with the retryable frame; the surfaces built on it
    # translate that into their own "upstream died" ending instead of a clean stop
    _fleet(monkeypatch, [(*RELAY_A, _blank_then_filter), (*DIRECT, _says(ANSWER))])
    r = A.app.test_client().post("/v1beta/models/auto:streamGenerateContent?alt=sse", json={
        "contents": [{"role": "user", "parts": [{"text": "explain the failing test"}]}]})
    assert "UNAVAILABLE" in r.get_data().decode("utf-8")
