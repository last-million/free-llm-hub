"""Hub bugs found by the live /agent end-to-end run (2026-09-27).

1. A reply that is ONLY an offered tool's name ("shell_command") was served as
   the turn's answer: codex on llm7/codestral-latest ended a /agent turn with
   the literal text "shell_command" and no file created. It is a failed hop.
2. nvidia 400'd every chat request carrying `prompt_cache_key` ("Validation:
   Unsupported parameter(s): `prompt_cache_key`"), request after request. An
   optional field a provider refuses BY NAME is now dropped, the call retried
   once, and the field never sent to that provider again.
No network: every upstream is a fake.
"""
import json

import app as A
import tool_rescue

CHAT_TOOLS = [{"type": "function", "function": {"name": "shell_command",
                                                "parameters": {"type": "object"}}},
              {"type": "function", "function": {"name": "apply_patch",
                                                "parameters": {"type": "object"}}}]
RESPONSES_TOOLS = [{"type": "function", "name": "shell_command"},
                   {"type": "custom", "name": "apply_patch"}]


# --------------------------------------------------------------------------- #
# 1. bare tool name
# --------------------------------------------------------------------------- #

def test_bare_tool_name_is_detected_in_every_wrapping():
    for text in ("shell_command", " shell_command\n", "`shell_command`", "shell_command()",
                 "functions.shell_command", '"apply_patch"', "apply_patch."):
        assert tool_rescue.is_bare_tool_name(text, CHAT_TOOLS), text
    assert tool_rescue.is_bare_tool_name("shell_command", RESPONSES_TOOLS)


def test_real_answers_are_never_mistaken_for_a_bare_tool_name():
    for text in ("42", "Done. I ran shell_command and it printed 42.",
                 "shell_command failed", "shell\ncommand", ""):
        assert not tool_rescue.is_bare_tool_name(text, CHAT_TOOLS), text
    # no tools offered -> never
    assert not tool_rescue.is_bare_tool_name("shell_command", None)
    assert not tool_rescue.is_bare_tool_name("shell_command", [])


def _chat(text):
    return {"choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}]}


def test_non_stream_bare_tool_name_is_a_nonanswer_only_on_a_tools_turn():
    assert A._chat_json_nonanswer(_chat("shell_command"), True, CHAT_TOOLS)
    assert not A._chat_json_nonanswer(_chat("shell_command"), False, None)
    assert not A._chat_json_nonanswer(_chat("It printed 42."), True, CHAT_TOOLS)


def test_streamed_bare_tool_name_is_judged_a_nonanswer():
    payload = {"model": "m", "messages": [{"role": "user", "content": "make hello.py"}],
               "tools": CHAT_TOOLS}
    check = A._peek_check(payload, True)
    assert check and check.get("tools") == CHAT_TOOLS
    assert A._judge_peeked(['data: {"choices":[{"delta":{"content":"shell_command"}}]}'],
                           check) == "nonanswer"
    # the same text with no tools on the request is left alone
    plain = A._peek_check({"model": "m", "messages": payload["messages"]}, False)
    assert A._judge_peeked(['data: {"choices":[{"delta":{"content":"shell_command"}}]}'],
                           plain) != "nonanswer"


# --------------------------------------------------------------------------- #
# 2. optional params refused by name
# --------------------------------------------------------------------------- #

NVIDIA_400 = json.dumps({"message": "Validation: Unsupported parameter(s): `prompt_cache_key`",
                         "type": "Bad Request", "code": 400})


def test_rejected_optional_params_reads_the_field_name():
    payload = {"model": "m", "messages": [], "prompt_cache_key": "abc", "user": "u",
               "tools": CHAT_TOOLS}
    assert A._rejected_optional_params(payload, NVIDIA_400) == {"prompt_cache_key"}
    assert A._rejected_optional_params(
        payload, "Unrecognized request argument supplied: prompt_cache_key") == {"prompt_cache_key"}
    # a short generic key only when quoted
    assert A._rejected_optional_params(payload, "unknown field 'user'") == {"user"}
    assert A._rejected_optional_params(payload, "unknown user, please sign in") == set()


def test_core_fields_and_unrelated_400s_are_never_dropped():
    payload = {"model": "m", "messages": [], "tools": CHAT_TOOLS, "max_tokens": 10,
               "prompt_cache_key": "abc"}
    assert A._rejected_optional_params(payload, "Unsupported parameter: `tools`") == set()
    assert A._rejected_optional_params(payload, "unsupported value for `max_tokens`") == set()
    # a 400 that does not read as a parameter refusal names nothing
    assert A._rejected_optional_params(
        payload, "This model's maximum context length is 8192 tokens (prompt_cache_key)") == set()


class _Resp:
    def __init__(self, status=200, payload=None, text=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = {}
        self.text = text if text is not None else json.dumps(self._payload)

    def json(self):
        return self._payload

    def close(self):
        pass


class _Post:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.sent = []

    def __call__(self, url=None, json=None, **kw):
        self.sent.append(json)
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def test_upstream_drops_a_refused_param_retries_and_remembers(monkeypatch):
    monkeypatch.setattr(A.config, "get_provider_config", lambda pid: {"api_keys": ["k"]})
    monkeypatch.setattr(A, "_resolve_base_url", lambda pid, pcfg: "https://example.invalid/v1")
    post = _Post(_Resp(400, text=NVIDIA_400), _Resp(200, _chat("ok")))
    monkeypatch.setattr(A.requests, "post", post)
    payload = {"model": "z-ai/glm-5.3", "_no_craft": True, "max_tokens": 64,
               "prompt_cache_key": "sess-1",
               "messages": [{"role": "user", "content": "hi"}]}
    resp = A._upstream_chat("nvidia", dict(payload), False)
    assert resp.status_code == 200
    assert post.sent[0]["prompt_cache_key"] == "sess-1"
    assert "prompt_cache_key" not in post.sent[1]
    # never sent to that provider again...
    A._upstream_chat("nvidia", dict(payload), False)
    assert "prompt_cache_key" not in post.sent[2]
    # ...but other providers still get it
    A._upstream_chat("groq", dict(payload), False)
    assert post.sent[3]["prompt_cache_key"] == "sess-1"


# --------------------------------------------------------------------------- #
# 3. a model-gone 404 on a multi-key provider
# --------------------------------------------------------------------------- #

NOT_FOUND = json.dumps({"status": 404, "title": "Not Found",
                        "detail": "Function 'x': Not found for account 'y'"})


class _KeyPost:
    """Answers per bearer key: {key: _Resp}."""

    def __init__(self, by_key):
        self.by_key = by_key
        self.keys = []

    def __call__(self, url=None, json=None, headers=None, **kw):
        key = (headers or {}).get("Authorization", "").replace("Bearer ", "")
        self.keys.append(key)
        return self.by_key[key]


def _two_keys(monkeypatch, post):
    monkeypatch.setattr(A.config, "get_provider_config", lambda pid: {"api_keys": ["k1", "k2"]})
    monkeypatch.setattr(A, "_resolve_base_url", lambda pid, pcfg: "https://example.invalid/v1")
    monkeypatch.setattr(A.requests, "post", post)
    monkeypatch.setattr(A, "_next_key_start", lambda pid, n: 0)


def _msg_payload(model):
    return {"model": model, "_no_craft": True, "messages": [{"role": "user", "content": "hi"}]}


def test_a_404_on_every_key_dead_marks_the_model(monkeypatch):
    post = _KeyPost({"k1": _Resp(404, text=NOT_FOUND), "k2": _Resp(404, text=NOT_FOUND)})
    _two_keys(monkeypatch, post)
    model = "mistralai/codestral-e2e-test"
    try:
        resp = A._upstream_chat("nvidia", _msg_payload(model), False)
        assert resp.status_code == 404
        assert post.keys == ["k1", "k2"]
        assert A._is_model_dead_upstream("nvidia", model)
    finally:
        with A._dead_lock:
            A._dead_models.pop(("nvidia", model), None)


def test_a_per_account_404_is_served_by_the_other_key(monkeypatch):
    post = _KeyPost({"k1": _Resp(404, text=NOT_FOUND), "k2": _Resp(200, _chat("ok"))})
    _two_keys(monkeypatch, post)
    model = "mistralai/codestral-e2e-test2"
    resp = A._upstream_chat("nvidia", _msg_payload(model), False)
    assert resp.status_code == 200
    assert post.keys == ["k1", "k2"]
    assert not A._is_model_dead_upstream("nvidia", model)


# --------------------------------------------------------------------------- #
# 6. a tools turn that ENDS on a lead-in colon
# --------------------------------------------------------------------------- #

LEAD_IN = ("The default working directory isn't valid, but I can pinpoint it by "
           "locating where the brief file is:\n\n")


def test_dangling_lead_in_is_a_nonanswer_on_a_tools_turn_only():
    assert A._looks_like_dangling_lead_in(LEAD_IN)
    assert A._chat_json_nonanswer(_chat(LEAD_IN), True, CHAT_TOOLS)
    assert not A._chat_json_nonanswer(_chat(LEAD_IN), False, None)
    payload = {"model": "m", "messages": [{"role": "user", "content": "go"}],
               "tools": CHAT_TOOLS}
    frame = 'data: ' + json.dumps({"choices": [{"delta": {"content": LEAD_IN}}]})
    assert A._judge_peeked([frame], A._peek_check(payload, True)) == "nonanswer"


def test_finished_answers_ending_otherwise_are_kept():
    for text in ("Done: both files created and the test passes.",
                 "Created mathutil.py and test_mathutil.py; `python test_mathutil.py` passed.",
                 "Here is the code:\n```python\nprint(1)\n```",
                 "x" * 500 + ":"):
        assert not A._looks_like_dangling_lead_in(text), text


# --------------------------------------------------------------------------- #
# 9. deleting a conversation whose turn is still running
# --------------------------------------------------------------------------- #

import os
import threading
import time

import pytest

import agentic_chat
import agentic_history
import memory


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv(memory._ROOT_ENV, str(tmp_path / "mem"))
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(tmp_path / "cfg" / "config.json"))
    return tmp_path


def _hdr():
    return {"X-Free-LLM-Hub": "dashboard",
            "X-Free-LLM-Hub-Token": A.config.get_control_token() or ""}


def _fake_running_turn(monkeypatch, sid, proj, finishes_after):
    """A turn that is busy until stopped, then takes `finishes_after` seconds
    to write its ending -- the reply and memory -- like the durable wrapper."""
    state = {"busy": True, "stopped": False}

    def stop(session_id):
        if session_id != sid or state["stopped"]:
            return False
        state["stopped"] = True

        def _finish():
            time.sleep(finishes_after)
            agentic_history.record_turn(sid, "codex", str(proj), "agent", "late reply")
            memory.remember_recent(sid, "late reply", "agent")
            state["busy"] = False
        threading.Thread(target=_finish, daemon=True).start()
        return True

    monkeypatch.setattr(agentic_chat, "stop_session", stop)
    monkeypatch.setattr(agentic_chat, "turn_busy", lambda s: s == sid and state["busy"])
    monkeypatch.setattr(agentic_chat, "get_session", lambda s: None)
    return state


def test_delete_waits_for_a_running_turn_so_it_cannot_come_back(isolated_state, monkeypatch):
    proj = isolated_state / "proj"
    proj.mkdir()
    sid = "e2e-delete-running-1"
    agentic_history.record_turn(sid, "codex", str(proj), "user", "list the files")
    memory.remember_recent(sid, "list the files", "user")
    state = _fake_running_turn(monkeypatch, sid, proj, finishes_after=0.5)
    r = A.app.test_client().delete("/api/agent/history/" + sid, headers=_hdr())
    assert r.status_code == 200
    assert state["stopped"] and not state["busy"]
    time.sleep(0.3)
    assert agentic_history.get_conversation(sid) is None
    assert not os.path.exists(memory._path(sid))


def test_a_turn_ending_after_the_settle_wait_is_swept_again(isolated_state, monkeypatch):
    proj = isolated_state / "proj"
    proj.mkdir()
    sid = "e2e-delete-running-2"
    agentic_history.record_turn(sid, "codex", str(proj), "user", "list the files")
    monkeypatch.setattr(A, "_DELETE_SETTLE_SECS", 0.1)
    state = _fake_running_turn(monkeypatch, sid, proj, finishes_after=0.6)
    r = A.app.test_client().delete("/api/agent/history/" + sid, headers=_hdr())
    assert r.status_code == 200
    deadline = time.time() + 6
    while time.time() < deadline and (state["busy"] or agentic_history.get_conversation(sid)
                                      or os.path.exists(memory._path(sid))):
        time.sleep(0.1)
    assert agentic_history.get_conversation(sid) is None
    assert not os.path.exists(memory._path(sid))


# --------------------------------------------------------------------------- #
# 8. a relay's billing page after a tool result
# --------------------------------------------------------------------------- #

POLLINATIONS_PAGE = (
    "The API key used for this request has reached its budget. Please [raise the key "
    "budget](https://enter.pollinations.ai/edit-key?id=XXXX&ref=agent_key_budget), then "
    "try again.\n\nTopping up the wallet does not raise this limit. If this isn’t your "
    "Pollinations account, contact whoever runs the app or service you’re using.")


def test_relay_billing_page_is_caught_even_after_a_tool_result():
    assert A._provider_error_kind(POLLINATIONS_PAGE, A._TOOL_RESULT_TURN) == "provider_quota"
    assert A._is_upstream_nonanswer(POLLINATIONS_PAGE, A._TOOL_RESULT_TURN)


def test_an_agent_reporting_a_tool_error_is_still_an_answer():
    for text in ("The registry returned 429 Too Many Requests, so the install failed.",
                 "npm says: rate limit exceeded. I will retry the install later.",
                 "Your API key has expired according to the CLI output; set a new one in .env."):
        assert A._provider_error_kind(text, A._TOOL_RESULT_TURN) is None, text


# --------------------------------------------------------------------------- #
# 7. multi-run phase summaries: leaked <think> blocks
# --------------------------------------------------------------------------- #

def test_phase_summary_drops_a_leaked_think_block():
    import swarm_windows
    leaked = ("```html\n<think>The user has successfully created and executed the "
              "test file.</think>\n```")
    out = swarm_windows.clean_summary(leaked)
    assert "<think" not in out and "```" not in out
    assert out.startswith("The user has successfully created")
    mixed = "<think>plan it</think>\nCreated strutil.py; the test passes."
    assert swarm_windows.clean_summary(mixed) == "Created strutil.py; the test passes."
    assert swarm_windows.clean_summary("Done.\n<think>unclosed") == "Done."
    plain = "Created a.py\n```python\nprint(1)\n```"
    assert swarm_windows.clean_summary(plain) == plain


# --------------------------------------------------------------------------- #
# 5. a nameless function_call replayed by codex poisons the conversation
# --------------------------------------------------------------------------- #

def test_nameless_function_call_and_its_output_are_left_out():
    body = {"instructions": "sys", "input": [
        {"type": "message", "role": "user", "content": "go"},
        {"type": "function_call", "call_id": "c1", "name": "", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "unsupported call"},
        {"type": "function_call", "call_id": "c2", "name": "shell_command",
         "arguments": '{"command": "ls"}'},
        {"type": "function_call_output", "call_id": "c2", "output": "a.py"},
    ]}
    msgs = A._responses_to_chat(body)
    calls = [tc for m in msgs for tc in (m.get("tool_calls") or [])]
    assert [tc["function"]["name"] for tc in calls] == ["shell_command"]
    assert [m.get("tool_call_id") for m in msgs if m["role"] == "tool"] == ["c2"]
    assert all(tc["function"]["name"] for tc in calls)


def test_named_calls_are_untouched():
    body = {"input": [
        {"type": "function_call", "call_id": "c2", "name": "shell_command", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c2", "output": "ok"}]}
    msgs = A._responses_to_chat(body)
    assert len(msgs) == 2 and msgs[1]["tool_call_id"] == "c2"


# --------------------------------------------------------------------------- #
# 4. "prompt is too long" when compacting cannot help
# --------------------------------------------------------------------------- #

def test_compaction_futile_only_when_the_fixed_part_fills_the_window():
    assert A._ctx_compaction_futile(31000, 32768)
    assert not A._ctx_compaction_futile(8000, 32768)
    assert not A._ctx_compaction_futile(0, 32768)
    assert not A._ctx_compaction_futile(31000, 0)


def test_fixed_part_counts_system_and_tools_only():
    big_sys = {"role": "system", "content": "x " * 40000}
    user = {"role": "user", "content": "y " * 40000}
    tools = [{"name": "t%d" % i, "description": "d " * 200,
              "input_schema": {"type": "object"}} for i in range(5)]
    only_sys = A._ctx_fixed_part_est([big_sys, user], {"tools": tools})
    with_user = A._est_tokens([big_sys, user], tools)
    assert 0 < only_sys < with_user
    assert A._ctx_fixed_part_est([user], {}) == 0


def test_overflow_reply_becomes_a_capacity_failure_when_compaction_is_futile():
    with A.app.test_request_context("/v1/messages", method="POST"):
        A._ctx_set("_ctx_signal", True)
        A._ctx_set("_ctx_orig_est", 32000)
        A._ctx_set("_ctx_overflow", {"hops": 2, "window": 32768})
        A._ctx_set("_ctx_tried", {("nvidia", "a"), ("groq", "b")})
        A._ctx_set("_ctx_fixed_est", 30000)
        assert A._ctx_overflow_reply("anthropic") is None
        # a long CONVERSATION on the same window still gets the native error
        A._ctx_set("_ctx_fixed_est", 6000)
        out = A._ctx_overflow_reply("anthropic")
        assert out is not None and out[1] == 400
