"""A relay's text-only reply on a tool turn is judged whole, whatever its length (2026-10-31).

MEASURED (the owner's OpenCode `coding-max` session, 00:21-01:34): seven "continue" turns in a
row ended with no tool call. Every one was served HTTP 200 / "ok" by a g4f poe relay copy
(DeepSeek-V4.1-Flash-T, GLM-5.3-Flash-T). Three carried the relay's "stop calling tools" gag
inside a leaked <think> at the very START of the reply (offset 90-416 of 2-7K characters);
two ended on "I'll ..." / "Let me ..." in a reply of 600+ characters. The peek only held a
reply up to _ANNOUNCE_MAX_CHARS (400) to see how it ends, so every one of these was committed
unjudged and OpenCode ended its turn on it. (Codex got a retry for this at the END of its
stream; the chat protocol cannot take back frames it has already relayed.)

Fix: on a RELAY hop of a TOOL turn the peek keeps reading a text-only reply until a tool call
arrives (commit), the stream ends (judged whole: the gag, or an announcement as the last
paragraph, fails the hop so the chain walks on) or the bounded hold runs out (committed, as
before). Direct providers and tool-free turns are untouched.
"""
import json

import pytest

import app as A


TOOLS = [{"type": "function", "function": {"name": "exec_command",
                                           "parameters": {"type": "object"}}}]
MODEL = "srv_mv57e8714aedb1a00bf8:poe/DeepSeek-V4.1-Flash-T"

THINK = ("<think>\nOk, continuing. The instruction says don't request "
         "another function call, so I will just summarise where things stand.\n</think>\n")
# varied prose on purpose: a repeated sentence is cut by the stream gate's loop detector
BODY = "Here is where things stand. " + " ".join(
    "Part %d: the %s module is written, its notes are stored in notes/%s.md and two leads "
    "from the %s review are still open." % (i, w, w, w)
    for i, w in enumerate(("scanner", "parser", "reporter", "scheduler", "cache", "router",
                           "loader", "writer", "indexer", "planner", "checker", "exporter"),
                          1)) + " "
GAGGED = THINK + BODY
ENDS_ON_INTENT = (BODY + "Next, let me run the full test suite against the new module "
                  "and then read the report.")
FINAL = "Both test files pass now. " + " ".join(
    "Change %d: I fixed the %s bound in %s.py so the last token is kept and updated its "
    "caller in %s_test.py." % (i, w, w, w)
    for i, w in enumerate(("loop", "slice", "index", "window", "offset", "margin", "buffer",
                           "cursor"), 1)) + " Nothing else needed to change."
AWAITS = (BODY + "I'll wait for your confirmation before touching the production config.")


def _check(relay=True, tools=True):
    return {"tools_offered": tools, "tools": TOOLS if tools else None,
            "last_prompt": "continue", "prompt_text": "continue",
            "relay": relay, "pid": "g4f", "model": MODEL}


def _cut(text, n):
    return [text[i:i + n] for i in range(0, len(text), n)]


def _frames(pieces, tool_call=False):
    out = [("data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": p}}]})
            ).encode() for p in pieces]
    if tool_call:
        out.append(b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
                   b'"id":"c1","function":{"name":"exec_command","arguments":"{}"}}]}}]}')
    out.append(b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}')
    out.append(b"data: [DONE]")
    return out


def _peek(pieces, check=None, tool_call=False):
    frames = _frames(pieces, tool_call)
    status, buf = A._peek_until_content(iter(frames), 5,
                                        check=check if check is not None else _check())
    return status, A._take_nonanswer_kind(), len(buf), len(frames)


@pytest.fixture(autouse=True)
def _clean_ledger():
    A._relay_tool_fail.clear()
    yield
    A._relay_tool_fail.clear()


def test_a_long_gagged_relay_reply_fails_the_hop_before_anything_is_committed():
    assert len(GAGGED) > 1000 and len(GAGGED) > A._ANNOUNCE_MAX_CHARS
    for size in (1, 3, 20, 400):
        status, kind, _n, _total = _peek(_cut(GAGGED, size))
        assert (status, kind) == ("nonanswer", "announced"), size


def test_the_gag_is_also_caught_when_the_reply_is_longer_than_400_frames():
    long_reply = THINK + BODY * 6            # per-token frames: well past max_lines
    assert len(_cut(long_reply, 3)) > 400
    status, kind, _n, _total = _peek(_cut(long_reply, 3))
    assert (status, kind) == ("nonanswer", "announced")


def test_the_gagged_server_is_rested_for_tool_turns_in_the_same_request():
    assert A._relay_tool_sick("g4f", MODEL) is False
    _peek(_cut(GAGGED, 8))
    assert A._relay_tool_sick("g4f", MODEL) is True


def test_a_long_relay_reply_that_ends_on_an_announcement_fails_the_hop():
    assert len(ENDS_ON_INTENT) > A._ANNOUNCE_MAX_CHARS
    for size in (5, 50):
        status, kind, _n, _total = _peek(_cut(ENDS_ON_INTENT, size))
        assert (status, kind) == ("nonanswer", "announced"), size


def test_a_long_plain_final_answer_is_served_once_it_has_ended():
    status, kind, n, total = _peek(_cut(FINAL, 6))
    assert (status, kind) == ("content", None)
    assert n == total                          # held to its end, then committed


def test_a_reply_that_hands_the_turn_to_the_user_is_served():
    assert len(AWAITS) > A._ANNOUNCE_MAX_CHARS
    status, kind, _n, _total = _peek(_cut(AWAITS, 9))
    assert (status, kind) == ("content", None)


def test_a_gagged_reply_that_then_calls_a_tool_commits_at_once():
    status, kind, _n, _total = _peek(_cut(GAGGED, 40), tool_call=True)
    assert (status, kind) == ("content", None)
    assert A._relay_tool_sick("g4f", MODEL) is False      # it chained: nothing to rest


def test_a_direct_provider_is_not_held():
    status, kind, n, total = _peek(_cut(FINAL, 6), check=_check(relay=False))
    assert (status, kind) == ("content", None)
    assert n < total                           # committed before the end, as always
    status, kind, n, total = _peek(_cut(GAGGED, 6), check=_check(relay=False))
    assert status == "content" and n < total


def test_a_tool_free_turn_is_not_held():
    status, kind, n, total = _peek(_cut(GAGGED, 6), check=_check(tools=False))
    assert status == "content" and n < total


def test_the_hold_is_time_bounded(monkeypatch):
    monkeypatch.setattr(A, "_ANNOUNCE_HOLD_S", 0.0)
    status, _kind, n, total = _peek(_cut(GAGGED, 6))
    assert status == "content" and n < total


def test_the_flag_off_keeps_the_old_behaviour(monkeypatch):
    real = A.config.get_flag
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=None: False if k == "relay_text_hold" else real(k, d))
    status, kind, n, total = _peek(_cut(GAGGED, 6))
    assert (status, kind) == ("content", None) and n < total


def test_a_flag_read_that_raises_counts_as_off(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("config unreadable")
    monkeypatch.setattr(A.config, "get_flag", boom)
    assert A._relay_text_hold_on() is False


def test_a_second_gagged_reply_in_one_request_is_served_not_walked_again():
    # _announce_skip_spent: two walks per request, then the third reply is served so a pool
    # of gagged relays cannot burn the whole deadline.
    class _G:
        pass
    with A.app.test_request_context("/"):
        assert A._announce_skip_spent("announced") is False
        assert A._announce_skip_spent("announced") is False
        assert A._announce_skip_spent("announced") is True


# --------------------------------------------------------------------------- #
# End to end through the real chat handler (the path OpenCode uses)
# --------------------------------------------------------------------------- #

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


def _frame(delta, mode, finish=None):
    obj = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    line = b"data: " + json.dumps(obj).encode("utf-8")
    return line if mode == "lines" else line + b"\n\n"


def _done(mode):
    return b"data: [DONE]" if mode == "lines" else b"data: [DONE]\n\n"


def _says(text, piece=7):
    def factory(resp, mode):
        yield _frame({"role": "assistant", "content": ""}, mode)
        for i in range(0, len(text), piece):
            yield _frame({"content": text[i:i + piece]}, mode)
        yield _frame({}, mode, finish="stop")
        yield _done(mode)
    return factory


def _calls_a_tool(resp, mode):
    yield _frame({"role": "assistant", "tool_calls": [
        {"index": 0, "id": "call_77", "type": "function",
         "function": {"name": "exec_command", "arguments": "{\"cmd\": \"pytest -q\"}"}}]}, mode)
    yield _frame({}, mode, finish="tool_calls")
    yield _done(mode)


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


def _fleet(monkeypatch, hops):
    """hops: [(pid, model, factory)] in chain order; returns the (pid, model) call log."""
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: (hops[0][0], hops[0][1], "hard"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [(p, m) for p, m, _f in hops])
    table = {(p, m): f for p, m, f in hops}
    calls = []

    def dispatch(pid, payload, stream):
        calls.append((pid, payload.get("model")))
        return _Resp(table[(pid, payload.get("model"))])
    monkeypatch.setattr(A, "_dispatch_chat", dispatch)
    return calls


def _read(body):
    """(assembled assistant text, names of the tool calls) of an SSE chat body: the text is
    cut into small deltas, so a phrase is never contiguous in the raw bytes."""
    text, tools = [], []
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
    return "".join(text), tools


def _post():
    return A.app.test_client().post("/v1/chat/completions", json={
        "model": "auto", "stream": True, "tools": TOOLS,
        "messages": [
            {"role": "user", "content": "run the tests and fix what fails"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c0", "type": "function",
                 "function": {"name": "exec_command", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c0", "content": "2 failed"},
            {"role": "user", "content": "continue"}]})


RELAY_A = ("g4f", "srv_aaa111:poe/DeepSeek-V4.1-Flash-T")
RELAY_B = ("freemodel", "srv_bbb222:poe/GLM-5.3-Flash-T")
RELAY_C = ("g4f", "srv_ccc333:poe/DeepSeek-V4.1-Flash-T")
DIRECT = ("nvidia", "z-ai/glm-5.3")


def test_e2e_a_gagged_relay_reply_is_walked_past_and_the_next_hop_serves(quiet, monkeypatch):
    calls = _fleet(monkeypatch, [(*RELAY_A, _says(GAGGED)), (*DIRECT, _calls_a_tool)])
    r = _post()
    body = r.get_data().decode("utf-8")
    assert r.status_code == 200
    assert r.headers.get("X-Free-LLM-Hub-Provider") == "nvidia"
    text, tools = _read(body)
    assert tools == ["exec_command"] and "pytest -q" in body
    assert text == ""                       # nothing of the gagged reply reached the client
    assert "Here is where things stand" not in text
    assert "don't request another function call" not in text
    assert calls == [RELAY_A, DIRECT]
    assert A._relay_tool_sick(*RELAY_A) is True                # and its server is rested


def test_e2e_a_long_relay_reply_that_ends_on_an_announcement_is_walked_past(quiet, monkeypatch):
    calls = _fleet(monkeypatch, [(*RELAY_A, _says(ENDS_ON_INTENT)), (*DIRECT, _calls_a_tool)])
    r = _post()
    body = r.get_data().decode("utf-8")
    assert r.headers.get("X-Free-LLM-Hub-Provider") == "nvidia"
    text, tools = _read(body)
    assert tools == ["exec_command"] and text == ""
    assert "let me run the full test suite" not in text
    assert calls == [RELAY_A, DIRECT]


def test_e2e_a_relay_that_really_finished_keeps_its_answer(quiet, monkeypatch):
    calls = _fleet(monkeypatch, [(*RELAY_A, _says(FINAL)), (*DIRECT, _calls_a_tool)])
    r = _post()
    body = r.get_data().decode("utf-8")
    assert r.headers.get("X-Free-LLM-Hub-Provider") == "g4f"
    text, tools = _read(body)
    assert text == FINAL and tools == []    # held to its end, then served whole and unchanged
    assert calls == [RELAY_A]


def test_e2e_a_pool_of_gagged_relays_cannot_burn_the_request(quiet, monkeypatch):
    # two walks per request (_ANNOUNCE_RETRIES), then the third reply is served. Two relay
    # PROVIDERS: the walk already puts a provider with two failed hops behind the others.
    calls = _fleet(monkeypatch, [(*RELAY_A, _says(GAGGED)), (*RELAY_B, _says(GAGGED)),
                                 (*RELAY_C, _says(GAGGED)), (*DIRECT, _calls_a_tool)])
    r = _post()
    body = r.get_data().decode("utf-8")
    assert r.status_code == 200
    assert calls == [RELAY_A, RELAY_B, RELAY_C]
    assert r.headers.get("X-Free-LLM-Hub-Provider") == "g4f"
    text, tools = _read(body)
    assert text == GAGGED and tools == []   # the third gagged reply is served, not walked again


def test_e2e_the_flag_off_serves_the_gagged_reply_as_before(quiet, monkeypatch):
    real = A.config.get_flag
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=None: False if k == "relay_text_hold" else real(k, d))
    calls = _fleet(monkeypatch, [(*RELAY_A, _says(GAGGED)), (*DIRECT, _calls_a_tool)])
    r = _post()
    body = r.get_data().decode("utf-8")
    assert r.headers.get("X-Free-LLM-Hub-Provider") == "g4f"
    text, tools = _read(body)
    assert text == GAGGED and tools == []   # the old behaviour: the gagged reply is served
    assert calls == [RELAY_A]
