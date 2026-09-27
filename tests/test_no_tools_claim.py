"""A reply CLAIMING the model has no tools, on a turn that offered them.

MEASURED 2026-09-27 (protocol_sweep): llm7/codestral answered three tool-turn
checks in text -- "I don't have access to tools/functions..." -- while the
request carried a tools array, and the hub served that as the answer.

Now: on a tools turn, a reply that opens with such a claim and makes no tool
call is a non-answer -- the next hop answers and the pair gets a quality
failure, never a dead-mark. Left alone: a reply after tools already ran this
turn, a user who asked ABOUT tools, a turn without tools, and ordinary text
that mentions functions or files. Fakes only, no network.
"""
import json

import pytest

import app as A

CLAIM = ("I don't have access to tools or functions that would let me read files. "
         "You can run `ls` yourself and paste the output here.")


@pytest.mark.parametrize("text", [
    CLAIM,
    "I'm sorry, but I don't have the ability to execute commands or access your file system.",
    "I cannot access files on your computer. Please paste the content here.",
    "As an AI language model, I don't have access to external tools.",
    "I do not have access to any tools/functions in this conversation.",
    "Unfortunately, I can't run shell commands. However, you can run `ls` yourself.",
    "I'm unable to call functions or use tools directly.",
    "I don't have function-calling capabilities.",
    "I don't have tools available to check that.",
    "Sorry, I can't execute code or access the filesystem.",
    "I have no access to your files.",
    "I'm not able to use tools in this context.",
    "Note: I don't have any tools available in this environment, so here is my guess.",
    "I'm a text-based AI and can't access your files.",
    "No tools are available to me, so I will answer directly.",
    "I don't have direct access to your local machine.",
    "I can\u2019t access your files directly.",
    "Sorry for the confusion. I don't have access to tools in this chat.",
])
def test_claims_to_have_no_tools_are_recognised(text):
    assert A._looks_like_no_tools_claim(text) is True


@pytest.mark.parametrize("text", [
    "I can't find the function `parse` in utils.py; could you share it?",
    "I can't call the function directly, but here is how it works.",
    "I don't have the file you mentioned.",
    "I don't have access to the internet, but 2+2 is 4.",
    "Here is the answer: 4. I can't run tools to verify it.",
    "The function can't access files outside the sandbox.",
    "I think your script can't access files in C:\\Windows because of permissions.",
    "4",
    "```python\nprint(1)\n```\nI can't run commands, so please test it.",
    "I can't guarantee this works, but run commands like `ls` to check.",
    "I cannot use the search tool for that query.",
    "Refactored parser.py and wrote tests; everything passes. " * 3
    + "I can't run commands on your machine though.",
])
def test_ordinary_text_is_not_a_claim(text):
    assert A._looks_like_no_tools_claim(text) is False


# --------------------------------------------------------------------------- #
# The policy around it
# --------------------------------------------------------------------------- #

def test_only_on_a_turn_that_offered_tools():
    assert A._no_tools_claim(CLAIM, tools_offered=True, prompt="List the files.") is True
    assert A._no_tools_claim(CLAIM, tools_offered=False, prompt="List the files.") is False


def test_not_after_tools_ran_this_turn():
    assert A._no_tools_claim(CLAIM, tools_offered=True, prompt=A._TOOL_RESULT_TURN) is False
    assert A._no_tools_claim(CLAIM, tools_offered=True, prompt="List the files.",
                             used_tools=True) is False


@pytest.mark.parametrize("prompt", [
    "Do you have any tools?",
    "What tools do you have access to?",
    "Can you call functions in this chat?",
    "Can you access my files?",
    "Answer without using any tools: what is 2+2?",
    "List your tools.",
])
def test_a_user_asking_about_tools_may_hear_there_are_none(prompt):
    assert A._no_tools_claim(CLAIM, tools_offered=True, prompt=prompt) is False


def test_cli_wrapper_blocks_are_not_the_user_asking():
    """Claude Code's <system-reminder> talks about tools all the time; only
    the user's own words decide."""
    prompt = ("<system-reminder>What tools do you have? Use your tools wisely?"
              "</system-reminder>List the files in this folder.")
    assert A._no_tools_claim(CLAIM, tools_offered=True, prompt=prompt) is True


@pytest.mark.parametrize("turns,used", [
    ([{"role": "user", "content": "list files"}], False),
    ([{"role": "user", "content": "list files"},
      {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function",
                                                             "function": {"name": "bash",
                                                                          "arguments": "{}"}}]},
      {"role": "tool", "tool_call_id": "c1", "content": "a.txt"}], True),
    ([{"role": "user", "content": "list files"},
      {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "bash",
                                         "input": {}}]},
      {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                    "content": "a.txt"}]}], True),
    ([{"role": "user", "content": "list files"},
      {"type": "function_call", "call_id": "c1", "name": "bash", "arguments": "{}"},
      {"type": "function_call_output", "call_id": "c1", "output": "a.txt"}], True),
    # tools ran in an EARLIER turn; this turn is a fresh instruction
    ([{"role": "user", "content": "list files"},
      {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function",
                                                             "function": {"name": "bash",
                                                                          "arguments": "{}"}}]},
      {"role": "tool", "tool_call_id": "c1", "content": "a.txt"},
      {"role": "assistant", "content": "a.txt"},
      {"role": "user", "content": "now read it"}], False),
])
def test_turn_used_tools_reads_every_protocol_shape(turns, used):
    assert A._turn_used_tools(turns) is used


TOOLS = [{"type": "function", "function": {"name": "bash", "parameters": {
    "type": "object", "properties": {"command": {"type": "string"}}}}}]


def _chat(content=None, tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"index": 0, "finish_reason": "tool_calls" if tool_calls else "stop",
                         "message": msg}]}


CALL = {"id": "call_1", "type": "function",
        "function": {"name": "bash", "arguments": "{\"command\": \"ls\"}"}}


def test_the_non_stream_verdict_and_its_kind():
    A._set_nonanswer_kind(None)
    assert A._chat_json_nonanswer(_chat(CLAIM), True, TOOLS) is True
    assert A._take_nonanswer_kind() == "no_tools_claim"
    # a reply that ALSO called a tool did the work
    assert A._chat_json_nonanswer(_chat(CLAIM, [CALL]), True, TOOLS) is False
    # plain chat: not this detector's business
    assert A._chat_json_nonanswer(_chat(CLAIM), False, None) is False


@pytest.fixture
def ledger(monkeypatch):
    seen = {"outcome": [], "throttle": [], "dead": []}
    monkeypatch.setattr(A, "_record_outcome",
                        lambda p, m, ok, junk=False: seen["outcome"].append((p, m, ok)))
    monkeypatch.setattr(A, "_throttle_failed_hop",
                        lambda p, m, exc=None, secs=None: seen["throttle"].append((p, m, secs)))
    monkeypatch.setattr(A, "_mark_model_dead", lambda p, m, s: seen["dead"].append((p, m)))
    return seen


def test_a_claim_is_a_quality_failure_not_a_dead_mark(ledger):
    A._note_nonanswer("p1", "codestral", kind="no_tools_claim")
    assert ledger["outcome"] == [("p1", "codestral", False)]
    assert ledger["dead"] == [] and ledger["throttle"] == []


def _frames(text):
    return [("data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": text}}]})
             ).encode(),
            b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
            b"data: [DONE]"]


def test_the_stream_peek_says_nonanswer_and_hands_back_the_kind():
    check = {"tools_offered": True, "tools": TOOLS, "last_prompt": "List the files."}
    status, _buf = A._peek_until_content(iter(_frames(CLAIM)), 5, check=check)
    assert status == "nonanswer"
    assert A._take_nonanswer_kind() == "no_tools_claim"


def test_the_stream_peek_keeps_a_report_after_tools_ran():
    check = {"tools_offered": True, "tools": TOOLS, "last_prompt": "List the files.",
             "used_tools": True}
    status, _buf = A._peek_until_content(iter(_frames(CLAIM)), 5, check=check)
    assert status == "content"


def test_peek_check_knows_whether_tools_ran():
    fresh = {"tools": TOOLS, "messages": [{"role": "user", "content": "ls"}]}
    after = {"tools": TOOLS, "messages": [
        {"role": "user", "content": "ls"},
        {"role": "assistant", "content": None, "tool_calls": [CALL]},
        {"role": "tool", "tool_call_id": "call_1", "content": "a.txt"}]}
    assert A._peek_check(fresh, True)["used_tools"] is False
    assert A._peek_check(after, True)["used_tools"] is True


# --------------------------------------------------------------------------- #
# End to end: every protocol, non-stream and stream -> the next hop acts
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


def _tool_frames():
    return [("data: " + json.dumps({"choices": [{"index": 0, "delta": {
        "role": "assistant", "tool_calls": [dict(CALL, index=0)]}}]})).encode(),
            b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}',
            b"data: [DONE]"]


@pytest.fixture
def hub(monkeypatch, ledger):
    for name in ("_record_chat_usage", "_save_perf_stats", "_act_pick", "_note_ttft",
                 "_record_stream_outcome", "_note_provider_timeout"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)
    monkeypatch.setattr(A, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(A, "_model_block_reason", lambda pid, m: None)
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("p1", "codestral", "hard"))
    monkeypatch.setattr(A, "_resolve_model", lambda m: ("p1", "codestral"))
    monkeypatch.setattr(A, "_build_chain",
                        lambda *a, **k: [("p1", "codestral"), ("p2", "good-model")])
    state = {"framed": False, "text": CLAIM, "calls": [], "per_token": False}

    def dispatch(pid, payload, stream):
        state["calls"].append(pid)
        if pid == "p1":
            if stream:
                units = (_token_frames(state["text"]) if state["per_token"]
                         else _frames(state["text"]))
                return _Resp(chunks=[u + b"\n\n" for u in units] if state["framed"] else units)
            return _Resp(payload=_chat(state["text"]))
        if stream:
            units = _tool_frames()
            return _Resp(chunks=[u + b"\n\n" for u in units] if state["framed"] else units)
        return _Resp(payload=_chat(None, [CALL]))
    monkeypatch.setattr(A, "_dispatch_chat", dispatch)
    client = A.app.test_client()
    client.state = state
    return client


ASK = "List the files in this folder."


@pytest.mark.parametrize("stream", [False, True])
def test_chat_completions_moves_to_the_next_hop(hub, ledger, stream):
    hub.state["framed"] = True
    r = hub.post("/v1/chat/completions", json={
        "model": "auto", "stream": stream, "tools": TOOLS,
        "messages": [{"role": "user", "content": ASK}]})
    body = r.get_data(as_text=True)
    assert "have access to tools" not in body and "bash" in body
    assert ("p1", "codestral", False) in ledger["outcome"]
    assert ledger["dead"] == []


@pytest.mark.parametrize("stream", [False, True])
def test_responses_moves_to_the_next_hop(hub, ledger, stream):
    r = hub.post("/v1/responses", json={
        "model": "auto", "stream": stream, "input": ASK,
        "tools": [{"type": "function", "name": "bash",
                   "parameters": TOOLS[0]["function"]["parameters"]}]})
    body = r.get_data(as_text=True)
    assert "have access to tools" not in body and "function_call" in body
    assert ("p1", "codestral", False) in ledger["outcome"]
    assert ledger["dead"] == []


@pytest.mark.parametrize("stream", [False, True])
def test_messages_moves_to_the_next_hop(hub, ledger, stream):
    r = hub.post("/v1/messages", json={
        "model": "auto", "max_tokens": 256, "stream": stream,
        "tools": [{"name": "bash", "input_schema": TOOLS[0]["function"]["parameters"]}],
        "messages": [{"role": "user", "content": ASK}]})
    body = r.get_data(as_text=True)
    assert "have access to tools" not in body and "tool_use" in body
    assert ("p1", "codestral", False) in ledger["outcome"]
    assert ledger["dead"] == []


@pytest.mark.parametrize("stream", [False, True])
def test_a_report_after_tools_ran_is_served(hub, ledger, stream):
    hub.state["framed"] = True
    hub.state["text"] = "I can't access files outside the project; the read was refused."
    r = hub.post("/v1/chat/completions", json={
        "model": "auto", "stream": stream, "tools": TOOLS,
        "messages": [{"role": "user", "content": "Read /etc/hosts"},
                     {"role": "assistant", "content": None, "tool_calls": [CALL]},
                     {"role": "tool", "tool_call_id": "call_1",
                      "content": "error: outside the project"}]})
    assert "the read was refused" in r.get_data(as_text=True)
    assert hub.state["calls"] == ["p1"]


def test_a_fan_out_member_claiming_no_tools_loses_its_slot():
    """swarm tool turns: the best-ranked member said it has no tools; before,
    it won the slot whenever nobody else acted."""
    from unittest import mock

    def resp(content):
        r = mock.Mock(status_code=200)
        r.json.return_value = _chat(content)
        r.close = mock.Mock()
        return r

    replies = {"a": resp("The folder has a.txt and b.txt."), "c": resp(CLAIM)}
    picks = [("p1", "a"), ("p3", "c")]
    recorded = []
    with mock.patch.object(A, "_route_by_difficulty", return_value=("p1", "a", "hard")), \
            mock.patch.object(A, "_build_chain", side_effect=lambda *a, **k: picks), \
            mock.patch.object(A, "_dispatch_chat_with_deadline",
                              side_effect=lambda pid, payload, deadline=None:
                              (replies[payload["model"]], None)), \
            mock.patch.object(A, "_benchmark_score", side_effect=lambda p, m: {"a": 10, "c": 30}[m]), \
            mock.patch.object(A, "_record_chat_usage"), \
            mock.patch.object(A, "_record_outcome",
                              side_effect=lambda p, m, ok, junk=False: recorded.append((p, m, ok))), \
            mock.patch.object(A, "_mark_model_dead") as dead, \
            mock.patch.object(A, "_routing_headers", return_value={}):
        with A.app.test_request_context("/v1/chat/completions"):
            out = A._swarm_tool_turn({"messages": [{"role": "user", "content": ASK}],
                                      "tools": TOOLS})
    data = out[0].get_json()
    assert data["model"].endswith("/a")
    assert "a.txt" in data["choices"][0]["message"]["content"]
    assert ("p3", "c", False) in recorded
    assert not dead.called


# --------------------------------------------------------------------------- #
# MEASURED 2026-09-27 (live /v1/responses, coding tier, plain tools): the claim
# came AFTER a preamble ("I'm here to help, but I currently don't have the
# t[ools]...") and was served. Two gaps: the adverb before "don't" defeated
# the regex, and the stream peek judged on ~4 per-token frames (its budget is
# frame BYTES), before the claim arrived. A SHORT complete reply now also
# counts when the claim sits in its first few sentences behind preamble only.
# --------------------------------------------------------------------------- #

LIVE = ("I'm here to help, but I currently don't have the tools to execute code or "
        "access your files directly. However, you can run the command yourself and "
        "share the output with me, and I'll help you interpret it.")


@pytest.mark.parametrize("text", [
    LIVE,
    "I'm here to help, but I currently don't have the ability to run functions.",
    "I'm here to help, but I currently don't have the ability to execute functions "
    "or access your files.",
    "Hello! I'm here to help. Unfortunately, I currently don't have the ability to run "
    "commands or access your files. Could you paste the output here?",
    "I'd be happy to help with that! However, I currently don't have access to tools "
    "that can read files on your machine. Please paste the file here.",
    "Sure, I can help with that. I don't currently have the ability to execute "
    "functions, but here's what you can do: run `ls -la` and share the result.",
    "To list the files, I would need to run a command. Unfortunately, I don't have the "
    "tools to do that in this chat.",
    "I understand you want me to fix config.py. I currently lack the tools to edit "
    "files directly, so here is the change to make by hand.",
    "I, unfortunately, cannot access your file system.",
    "I'm currently unable to run commands on your machine.",
    "I actually can't access files on your computer.",
])
def test_the_live_claim_and_its_variants_are_recognised(text):
    assert A._looks_like_no_tools_claim(text) is True
    assert A._no_tools_claim(text, tools_offered=True, prompt=ASK) is True


@pytest.mark.parametrize("text", [
    # answered first, then named the limitation
    "Here is the answer: 4. I can't run tools to verify it.",
    "The bug is on line 12: `x = y` should be `x == y`. I can't run commands to test "
    "it, though.",
    "Your function returns None because the loop never runs. I currently don't have "
    "the tools to test it, so please run it.",
    "Done - I renamed the helper. I can't run commands on your machine, so please run "
    "the tests.",
    # a LONG helpful answer: a limitation in its second sentence is not the reply
    "I'd be happy to walk you through the migration. I can't run commands on your "
    "machine, so here is every step. " + "First, back up the database with pg_dump, "
    "then apply each migration in order and check the row counts after each one. " * 6,
    # code before the claim
    "`ls -la` lists them. I don't have the tools to run it here.",
])
def test_legit_short_and_long_answers_stay_answers(text):
    assert A._looks_like_no_tools_claim(text) is False


def test_the_short_reply_rule_needs_the_whole_reply():
    """A stream still arriving has no known length: only the opening rule."""
    text = ("Hello! I'm here to help. Unfortunately, I currently don't have the ability "
            "to run commands.")
    assert A._looks_like_no_tools_claim(text, complete=True) is True
    assert A._looks_like_no_tools_claim(text, complete=False) is False
    assert A._no_tools_peek_state(text) == "open"


@pytest.mark.parametrize("text,state", [
    ("I'm here to help, but I currently don't have the tools", "claim"),
    ("Hello! I'm here to", "open"),
    ("Hello! I'm here to help. Unfortunately, I currently don't have the tools", "open"),
    ("The folder has three files. Here", "clear"),
    ("```python\nprint(1)", "clear"),
    ("Hi. Sure. I'm here to help. Then", "clear"),
    ("x" * 700, "clear"),
])
def test_the_peek_reads_on_only_while_a_claim_is_possible(text, state):
    assert A._no_tools_peek_state(text) == state


@pytest.mark.parametrize("prompt,used,expected", [
    (ASK, False, True),
    (ASK, True, False),                        # tools already ran this turn
    ("Do you have any tools?", False, False),   # the user asked about tools
    ("Can you access my files?", False, False),
])
def test_the_live_claim_keeps_the_policy(prompt, used, expected):
    assert A._no_tools_claim(LIVE, tools_offered=True, prompt=prompt,
                             used_tools=used) is expected
    assert A._no_tools_claim(LIVE, tools_offered=False, prompt=prompt) is False


def _token_frames(text):
    """One SSE frame per word, full-size chunk envelopes -- what codestral and
    most providers really send. ~170 bytes each, so _PEEK_JUDGE_CHARS (600
    bytes) is reached after ~4 words."""
    import re as _re
    out = []
    for tok in _re.findall(r"\S+\s*", text):
        out.append(("data: " + json.dumps({
            "id": "chatcmpl-abc123", "object": "chat.completion.chunk",
            "created": 1727400000, "model": "codestral-latest",
            "choices": [{"index": 0, "delta": {"content": tok}, "finish_reason": None}]})
        ).encode())
    out.append(b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}')
    out.append(b"data: [DONE]")
    return out


def test_the_stream_peek_catches_the_live_claim_on_per_token_frames():
    check = {"tools_offered": True, "tools": TOOLS, "last_prompt": ASK}
    status, _buf = A._peek_until_content(iter(_token_frames(LIVE)), 5, check=check)
    assert status == "nonanswer"
    assert A._take_nonanswer_kind() == "no_tools_claim"


def test_the_stream_peek_catches_a_short_reply_claim_after_preamble():
    text = ("Hello! I'm here to help. Unfortunately, I currently don't have the ability "
            "to run commands or access your files. Could you paste the output here?")
    check = {"tools_offered": True, "tools": TOOLS, "last_prompt": ASK}
    status, _buf = A._peek_until_content(iter(_token_frames(text)), 5, check=check)
    assert status == "nonanswer"


@pytest.mark.parametrize("text", [
    "The folder has three files: a.txt, b.txt and notes.md. " * 12,
    "I'd be happy to walk you through the migration. I can't run commands on your "
    "machine, so here is every step. " + "First, back up the database with pg_dump, "
    "then apply each migration in order and check the row counts. " * 8,
])
def test_the_stream_peek_commits_a_real_answer_without_reading_it_all(text):
    check = {"tools_offered": True, "tools": TOOLS, "last_prompt": ASK}
    frames = _token_frames(text)
    status, buf = A._peek_until_content(iter(frames), 5, check=check)
    assert status == "content"
    assert len(buf) < len(frames) - 2          # committed mid-stream, not at the end


def test_the_stream_peek_does_not_read_on_when_not_watching():
    check = {"tools_offered": True, "tools": TOOLS, "last_prompt": ASK, "used_tools": True}
    status, buf = A._peek_until_content(iter(_token_frames(LIVE)), 5, check=check)
    assert status == "content" and len(buf) <= 5
    status, buf = A._peek_until_content(iter(_token_frames(LIVE)), 5,
                                        check={"tools_offered": False, "last_prompt": ASK})
    assert status == "content" and len(buf) <= 5


def test_the_read_on_is_bounded_in_time(monkeypatch):
    monkeypatch.setattr(A, "_NO_TOOLS_PEEK_EXTRA_S", 0.0)
    check = {"tools_offered": True, "tools": TOOLS, "last_prompt": ASK}
    text = "Hello! I'm here to help. " + "Unfortunately, I don't have the tools to run it."
    status, buf = A._peek_until_content(iter(_token_frames(text)), 5, check=check)
    assert status == "content" and len(buf) <= 6


@pytest.mark.parametrize("stream", [False, True])
def test_chat_completions_skips_the_live_claim(hub, ledger, stream):
    hub.state.update(framed=True, text=LIVE, per_token=True)
    r = hub.post("/v1/chat/completions", json={
        "model": "auto", "stream": stream, "tools": TOOLS,
        "messages": [{"role": "user", "content": ASK}]})
    body = r.get_data(as_text=True)
    assert "have the tools" not in body and "bash" in body
    assert ("p1", "codestral", False) in ledger["outcome"]
    assert ledger["dead"] == []


@pytest.mark.parametrize("stream", [False, True])
def test_responses_skips_the_live_claim(hub, ledger, stream):
    hub.state.update(text=LIVE, per_token=True)
    r = hub.post("/v1/responses", json={
        "model": "auto", "stream": stream, "input": ASK,
        "tools": [{"type": "function", "name": "bash",
                   "parameters": TOOLS[0]["function"]["parameters"]}]})
    body = r.get_data(as_text=True)
    assert "have the tools" not in body and "function_call" in body
    assert ("p1", "codestral", False) in ledger["outcome"]
    assert ledger["dead"] == []


@pytest.mark.parametrize("stream", [False, True])
def test_messages_skips_the_live_claim(hub, ledger, stream):
    hub.state.update(text=LIVE, per_token=True)
    r = hub.post("/v1/messages", json={
        "model": "auto", "max_tokens": 256, "stream": stream,
        "tools": [{"name": "bash", "input_schema": TOOLS[0]["function"]["parameters"]}],
        "messages": [{"role": "user", "content": ASK}]})
    body = r.get_data(as_text=True)
    assert "have the tools" not in body and "tool_use" in body
    assert ("p1", "codestral", False) in ledger["outcome"]
    assert ledger["dead"] == []


@pytest.mark.parametrize("stream", [False, True])
def test_a_long_real_answer_on_per_token_frames_is_served(hub, stream):
    text = ("I'd be happy to walk you through the migration. I can't run commands on "
            "your machine, so here is every step. " + "First, back up the database "
            "with pg_dump, then apply each migration in order. " * 8)
    hub.state.update(framed=True, text=text, per_token=True)
    r = hub.post("/v1/chat/completions", json={
        "model": "auto", "stream": stream, "tools": TOOLS,
        "messages": [{"role": "user", "content": ASK}]})
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and "pg_dump" in body and "bash" not in body
    assert hub.state["calls"] == ["p1"]


def test_a_user_asking_about_tools_gets_the_first_answer(hub):
    hub.state["text"] = "I don't have tools available in this chat."
    r = hub.post("/v1/chat/completions", json={
        "model": "auto", "stream": False, "tools": TOOLS,
        "messages": [{"role": "user", "content": "Do you have any tools?"}]})
    assert "don't have tools" in r.get_json()["choices"][0]["message"]["content"]
    assert hub.state["calls"] == ["p1"]
