"""A chat stream that stops on "I'll do X" is retried, not served (2026-11-21).

MEASURED (the owner's OpenCode `coding-max`, structure only): of 66 turns that ended and waited for
the user over three days, three ended on "Let me run ..." / "Running baseline pilot ..." with ZERO
tool calls. The hub's own `_stopped_on_announcement` flags all three. The latest one was a 12K
character reply from nvidia/kimi-k3.1-pro: a DIRECT provider, one hop, streamed. The codex route
already retries such a reply at the END of its stream and a RELAY hop's reply is held to its end by
the peek, but a direct provider's long reply on the CHAT protocol was neither: the relay had forwarded
the finish frame and [DONE] before anything could be judged, so OpenCode ended the turn.

The gate sees the finish frame BEFORE it is relayed. On a chat stream that offered tools, saw no tool
call and is about to finish with "stop" on an announcement, it raises _AnnounceStop and the relay ends
like an upstream that died: the retryable ECONNRESET frame, no finish_reason, no [DONE]. The pair is
filed as an "announced" failure so the retry opens on another model, and a per-conversation budget
bounds it. Chat route only: /v1/responses and /v1/messages keep their own endings.
Hermetic: no network, no sleeping.
"""
import json
import logging

import pytest

import app as A


TOOLS = [{"type": "function", "function": {"name": "exec_command",
                                           "parameters": {"type": "object"}}}]
DIRECT = ("nvidia", "moonshotai/kimi-k3.1-pro")
OTHER = ("dahl", "deepseek-ai/DeepSeek-V4-Flash-0731")

REPORT = ("I read the scope page and the program rules. The in-scope assets are the main web app, "
          "the public API and the mobile backend; the marketing site and the status page are out of "
          "scope, and automated scanning is limited to five requests per second with the program's "
          "header on every request. Testing accounts must be created with the program's own email "
          "alias, and any finding needs a working proof of concept before it is reported.")
LONG_ANNOUNCE = REPORT + "\n\nLet me run the baseline pilot now."
LONG_PROGRESS = REPORT + "\n\nRunning baseline pilot against the staging copy."
LONG_ANSWER = REPORT + "\n\nThose are the rules that apply to every test in this program."
QUESTION = REPORT + "\n\nShould I start with the API or the mobile backend?"
AWAITS = REPORT + "\n\nI'll wait for your confirmation before testing anything."
ANSWER = REPORT + "\n\nAll checks pass now and the report is saved next to the notes."


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


def _says(text, piece=7, finish="stop", finish_on_last=False):
    """A per-token stream (piece chars a frame, so the peek has committed long before the end)."""
    def factory(resp, mode):
        yield _frame({"role": "assistant", "content": ""}, mode)
        chunks = [text[i:i + piece] for i in range(0, len(text), piece)]
        for n, c in enumerate(chunks):
            if finish_on_last and n == len(chunks) - 1:
                yield _frame({"content": c}, mode, finish=finish)     # text AND finish in one frame
            else:
                yield _frame({"content": c}, mode)
        if not finish_on_last:
            yield _frame({}, mode, finish=finish)
        yield _done(mode)
    return factory


def _says_then_calls(text, piece=7):
    def factory(resp, mode):
        yield _frame({"role": "assistant", "content": ""}, mode)
        for i in range(0, len(text), piece):
            yield _frame({"content": text[i:i + piece]}, mode)
        yield _frame({"tool_calls": [{"index": 0, "id": "call_9", "type": "function",
                                      "function": {"name": "exec_command",
                                                   "arguments": "{\"cmd\": \"ls\"}"}}]}, mode)
        yield _frame({}, mode, finish="tool_calls")
        yield _done(mode)
    return factory


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
    for led in (A._recent_hop_fail, A._announce_stop_seen):
        led.clear()
    yield
    for led in (A._recent_hop_fail, A._announce_stop_seen):
        led.clear()


@pytest.fixture
def flag_off(monkeypatch):
    real = A.config.get_flag
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=None: False if k == "announce_stop_retry" else real(k, d))


def _fleet(monkeypatch, hops):
    """hops: [(pid, model, factory)] in chain order; returns the call log."""
    monkeypatch.setattr(A, "_route_by_difficulty",
                        lambda *a, **k: (hops[0][0], hops[0][1], "hard"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [(p, m) for p, m, _f in hops])
    table = {(p, m): f for p, m, f in hops}
    calls = []

    def dispatch(pid, payload, stream):
        calls.append((pid, payload.get("model")))
        return _Resp(table[(pid, payload.get("model"))])
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


def _post(tools=True, prompt="continue"):
    body = {"model": "auto", "stream": True,
            "messages": [
                {"role": "user", "content": "run the baseline and report"},
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "c0", "type": "function",
                     "function": {"name": "exec_command", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "c0", "content": "2 failed"},
                {"role": "user", "content": prompt}]}
    if tools:
        body["tools"] = TOOLS
    return A.app.test_client().post("/v1/chat/completions", json=body)


def _retried(body):
    """The chat body ended the way OpenCode retries on: an error frame, no finish, no [DONE]."""
    _text, _tools, fins = _read(body)
    return "ECONNRESET" in body and "server_error" in body and fins == [] and "[DONE]" not in body


def _served(body, full):
    text, _tools, fins = _read(body)
    return fins == ["stop"] and "[DONE]" in body and "ECONNRESET" not in body and text == full


# --------------------------------------------------------------------------- #
# The fix
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("reply", [LONG_ANNOUNCE, LONG_PROGRESS])
def test_a_long_direct_reply_that_ends_on_an_announcement_ends_with_the_retryable_frame(
        quiet, monkeypatch, reply):
    assert len(reply) > A._ANNOUNCE_MAX_CHARS            # past what the peek ever holds
    calls = _fleet(monkeypatch, [(*DIRECT, _says(reply)), (*OTHER, _says(ANSWER))])
    body = _post().get_data().decode("utf-8")
    assert calls == [DIRECT]                             # committed: the CLI's retry reaches the next
    assert _retried(body)
    text = _read(body)[0]
    assert text and reply.startswith(text)               # what was shown stays shown
    assert A._recent_hop_failure(*DIRECT) == "announced"  # so the retry opens elsewhere
    assert A._is_model_dead(*DIRECT) is False            # a heuristic about ONE reply: never benched


def test_the_finish_riding_on_the_last_text_frame_is_judged_too(quiet, monkeypatch):
    _fleet(monkeypatch, [(*DIRECT, _says(LONG_ANNOUNCE, finish_on_last=True)),
                         (*OTHER, _says(ANSWER))])
    assert _retried(_post().get_data().decode("utf-8"))


def test_the_pair_is_filed_once_as_announced(quiet, monkeypatch):
    nonanswers, outcomes = [], []
    monkeypatch.setattr(A, "_note_nonanswer",
                        lambda pid, model, kind=None: nonanswers.append((pid, model, kind)))
    monkeypatch.setattr(A, "_record_outcome", lambda *a, **k: outcomes.append(a))
    _fleet(monkeypatch, [(*DIRECT, _says(LONG_ANNOUNCE)), (*OTHER, _says(ANSWER))])
    _post().get_data()
    assert nonanswers == [(*DIRECT, "announced")]
    assert outcomes == []                                # not a second failure on top


def test_the_retry_budget_is_spent_last_and_bounded(quiet, monkeypatch):
    _fleet(monkeypatch, [(*DIRECT, _says(LONG_ANNOUNCE)), (*OTHER, _says(ANSWER))])
    seen = []
    for _ in range(A._ANNOUNCE_STOP_RETRIES + 2):
        body = _post().get_data().decode("utf-8")
        seen.append("retry" if _retried(body) else "served" if _served(body, LONG_ANNOUNCE)
                    else "?")
    assert seen == ["retry"] * A._ANNOUNCE_STOP_RETRIES + ["served", "served"]   # never a loop


def test_another_instruction_has_its_own_budget(quiet, monkeypatch):
    _fleet(monkeypatch, [(*DIRECT, _says(LONG_ANNOUNCE)), (*OTHER, _says(ANSWER))])
    for _ in range(A._ANNOUNCE_STOP_RETRIES):
        assert _retried(_post(prompt="continue").get_data().decode("utf-8"))
    assert _served(_post(prompt="continue").get_data().decode("utf-8"), LONG_ANNOUNCE)
    assert _retried(_post(prompt="now check the mobile backend").get_data().decode("utf-8"))


def test_ordinary_replies_do_not_spend_the_budget(quiet, monkeypatch):
    _fleet(monkeypatch, [(*DIRECT, _says(LONG_ANSWER)), (*OTHER, _says(ANSWER))])
    for _ in range(A._ANNOUNCE_STOP_RETRIES + 3):
        assert _served(_post().get_data().decode("utf-8"), LONG_ANSWER)
    assert A._announce_stop_seen == {}
    _fleet(monkeypatch, [(*DIRECT, _says(LONG_ANNOUNCE)), (*OTHER, _says(ANSWER))])
    assert _retried(_post().get_data().decode("utf-8"))   # the full budget is still there


# --------------------------------------------------------------------------- #
# What is left alone
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("reply", [LONG_ANSWER, QUESTION, AWAITS])
def test_a_real_answer_a_question_or_a_hand_back_to_the_user_is_served(quiet, monkeypatch, reply):
    _fleet(monkeypatch, [(*DIRECT, _says(reply)), (*OTHER, _says(ANSWER))])
    body = _post().get_data().decode("utf-8")
    assert _served(body, reply)
    assert A._recent_hop_failure(*DIRECT) is None


def test_a_turn_without_tools_is_served(quiet, monkeypatch):
    _fleet(monkeypatch, [(*DIRECT, _says(LONG_ANNOUNCE)), (*OTHER, _says(ANSWER))])
    assert _served(_post(tools=False).get_data().decode("utf-8"), LONG_ANNOUNCE)


def test_a_reply_cut_at_length_is_not_judged_here(quiet, monkeypatch):
    _fleet(monkeypatch, [(*DIRECT, _says(LONG_ANNOUNCE, finish="length")), (*OTHER, _says(ANSWER))])
    body = _post().get_data().decode("utf-8")
    assert "ECONNRESET" not in body and _read(body)[2] == ["length"]


def test_a_reply_that_called_a_tool_passes_unchanged(quiet, monkeypatch):
    _fleet(monkeypatch, [(*DIRECT, _says_then_calls(LONG_ANNOUNCE)), (*OTHER, _says(ANSWER))])
    body = _post().get_data().decode("utf-8")
    text, tools, fins = _read(body)
    assert tools == ["exec_command"] and fins == ["tool_calls"] and "ECONNRESET" not in body
    assert text == LONG_ANNOUNCE


def test_the_flag_off_serves_it_as_before(quiet, monkeypatch, flag_off):
    _fleet(monkeypatch, [(*DIRECT, _says(LONG_ANNOUNCE)), (*OTHER, _says(ANSWER))])
    assert _served(_post().get_data().decode("utf-8"), LONG_ANNOUNCE)
    assert A._recent_hop_failure(*DIRECT) is None


def test_a_flag_read_that_raises_counts_as_off(quiet, monkeypatch):
    real = A.config.get_flag

    def flaky(k, d=None):
        if k == "announce_stop_retry":
            raise OSError("config unreadable")
        return real(k, d)
    monkeypatch.setattr(A.config, "get_flag", flaky)
    _fleet(monkeypatch, [(*DIRECT, _says(LONG_ANNOUNCE)), (*OTHER, _says(ANSWER))])
    assert _served(_post().get_data().decode("utf-8"), LONG_ANNOUNCE)


# --------------------------------------------------------------------------- #
# It cooperates with the peek's own per-request bound (_ANNOUNCE_RETRIES)
# --------------------------------------------------------------------------- #

SHORT = "Let me run the baseline pilot now."
THIRD = ("dahl", "other-model-3")


def test_a_request_that_already_walked_past_its_announcing_hops_is_not_retried_again(
        quiet, monkeypatch):
    """The peek skips two announcing hops and SERVES the third so a pool that keeps announcing
    cannot eat the deadline; the gate must not add a retry on top of that bound."""
    third = _says(SHORT)
    calls = _fleet(monkeypatch, [(*DIRECT, _says(SHORT)), (*OTHER, _says(SHORT)), (*THIRD, third)])
    body = _post().get_data().decode("utf-8")
    assert calls == [DIRECT, OTHER, THIRD]
    assert _served(body, SHORT)                          # the third announcement is served, as before
    assert A._announce_stop_seen == {}                   # and no gate budget was spent on it


def test_a_request_that_walked_one_announcing_hop_still_gets_the_gate_retry(quiet, monkeypatch):
    calls = _fleet(monkeypatch, [(*DIRECT, _says(SHORT)), (*OTHER, _says(LONG_ANNOUNCE))])
    body = _post().get_data().decode("utf-8")
    assert calls == [DIRECT, OTHER]                      # hop 1 skipped by the peek, hop 2 committed
    assert _retried(body)
    assert A._recent_hop_failure(*OTHER) == "announced"


# --------------------------------------------------------------------------- #
# Chat only: the other two routes keep their own endings
# --------------------------------------------------------------------------- #

def _gate(announce_retry, mode="bytes", has_tools=True):
    return A._gate_stream(iter(()), mode, "nvidia", "m",
                          {"messages": [{"role": "user", "content": "go"}]}, has_tools,
                          announce_retry=announce_retry)


def test_only_the_chat_route_opts_in():
    assert _gate(True)._announce_ok is True
    assert _gate(False)._announce_ok is False
    assert _gate(True, has_tools=False)._announce_ok is False
    assert A._gate_stream(iter(()), "lines", "nvidia", "m", {"messages": []}, True)._announce_ok is False


def _sse_objects(body):
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:") and not line.endswith("[DONE]"):
            try:
                yield json.loads(line[5:].strip())
            except ValueError:
                continue


def test_the_responses_route_keeps_its_own_end_of_stream_retry(quiet, monkeypatch):
    _fleet(monkeypatch, [(*DIRECT, _says(LONG_ANNOUNCE)), (*OTHER, _says(ANSWER))])
    body = A.app.test_client().post("/v1/responses", json={
        "model": "auto", "stream": True,
        "tools": [{"type": "function", "name": "exec_command", "parameters": {"type": "object"}}],
        "input": [{"role": "user", "content": "run the baseline and report"}]}).get_data().decode()
    types = [o.get("type") for o in _sse_objects(body)]
    assert types[-1] == "response.failed" and "response.completed" not in types
    assert "ECONNRESET" not in body                      # the chat frame is not what codex gets


def test_the_messages_route_is_unchanged(quiet, monkeypatch):
    _fleet(monkeypatch, [(*DIRECT, _says(LONG_ANNOUNCE)), (*OTHER, _says(ANSWER))])
    body = A.app.test_client().post("/v1/messages", json={
        "model": "claude-sonnet-4", "max_tokens": 1024, "stream": True,
        "tools": [{"name": "exec_command", "description": "run", "input_schema": {"type": "object"}}],
        "messages": [{"role": "user", "content": "run the baseline and report"}]}).get_data().decode()
    assert "message_stop" in body                        # served as before (Claude Code not changed)
    assert "ECONNRESET" not in body


# --------------------------------------------------------------------------- #
# Housekeeping
# --------------------------------------------------------------------------- #

def test_the_stop_is_its_own_class_and_files_announced_not_conn(monkeypatch):
    assert issubclass(A._AnnounceStop, A._UpstreamStreamError)
    assert not issubclass(A._AnnounceStop, A._ContentFilterStop)
    filed = []
    monkeypatch.setattr(A, "_note_nonanswer", lambda p, m, kind=None: filed.append(("na", p, m, kind)))
    monkeypatch.setattr(A, "_note_recent_hop_failure", lambda p, m, kind: filed.append(("rf", p, m, kind)))
    monkeypatch.setattr(A, "_client_gone", lambda: False)
    A._note_broken_stream("nvidia", "m1", A._AnnounceStop("x"), "chat")
    assert filed == [("na", "nvidia", "m1", "announced"), ("rf", "nvidia", "m1", "announced")]


def test_nothing_is_filed_when_the_client_already_left(monkeypatch):
    filed = []
    monkeypatch.setattr(A, "_note_nonanswer", lambda *a, **k: filed.append(a))
    monkeypatch.setattr(A, "_note_recent_hop_failure", lambda *a, **k: filed.append(a))
    monkeypatch.setattr(A, "_client_gone", lambda: True)
    A._note_broken_stream("nvidia", "m1", A._AnnounceStop("x"), "chat")
    assert filed == []


def test_the_log_line_carries_lengths_never_the_reply(quiet, monkeypatch, caplog):
    _fleet(monkeypatch, [(*DIRECT, _says(LONG_ANNOUNCE)), (*OTHER, _says(ANSWER))])
    with caplog.at_level(logging.WARNING):
        _post().get_data()
    lines = [r.getMessage() for r in caplog.records if "[announce-stop] chat" in r.getMessage()]
    assert len(lines) == 1
    assert "nvidia/moonshotai/kimi-k3.1-pro" in lines[0] and "chars" in lines[0]
    for word in ("baseline", "staging", "scope page", "proof of concept"):
        assert word not in lines[0]
