"""codex 0.165+ sends its tools as an INPUT ITEM, and apply_patch is a FREEFORM tool.

MEASURED 2026-10-30 with the real codex 0.165.0 against a fake Responses server:
the request has no top-level `tools`; input[0] is {"type": "additional_tools",
"role": "developer", "tools": [namespace "functions" (exec_command, apply_patch,
...), namespace "collaboration" (spawn_agent, ...)]}. The hub read only
`body["tools"]`, so every codex turn reached the models with NO tools (hub.log
`tools=False`) and they answered "I can't run commands / reach your WSL".
codex runs exec_command returned as a function_call (with or without
`namespace: "functions"`), but apply_patch only as a `custom_tool_call` carrying
`input` text: as a function_call it answers "Fatal error: tool apply_patch invoked
with incompatible payload".
"""
import json

import pytest

import app as A


EXEC = {"type": "function", "name": "exec_command", "strict": False,
        "description": "Runs a command.",
        "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}},
                       "required": ["cmd"]}}
PATCH = {"type": "custom", "name": "apply_patch",
         "description": "The `apply_patch` tool can be used to edit files. This is a "
                        "FREEFORM tool, so do not wrap the patch in JSON.",
         "format": {"type": "grammar", "syntax": "lark",
                    "definition": 'start: begin_patch hunk+ end_patch\n'
                                  'begin_patch: "*** Begin Patch" LF'}}
SPAWN = {"type": "function", "name": "spawn_agent", "description": "Spawn a sub-agent.",
         "parameters": {"type": "object", "properties": {"message": {"type": "string"}}}}
ADDITIONAL = {"type": "additional_tools", "id": "at_1", "role": "developer",
              "tools": [{"type": "namespace", "name": "functions", "description": "",
                         "tools": [EXEC, PATCH]},
                        {"type": "namespace", "name": "collaboration",
                         "description": "Sub-agents.", "tools": [SPAWN]}]}
PATCH_TEXT = "*** Begin Patch\n*** Add File: hello.txt\n+hi\n*** End Patch\n"


def _codex_body(**extra):
    body = {"model": "auto", "stream": False, "prompt_cache_key": "thread-at",
            "input": [dict(ADDITIONAL),
                      {"type": "message", "role": "developer",
                       "content": [{"type": "input_text", "text": "You are Codex."}]},
                      {"type": "message", "role": "user",
                       "content": [{"type": "input_text", "text": "list my WSL home"}]}]}
    body.update(extra)
    return body


def _flags(monkeypatch, **off):
    real = A.config.get_flag

    def fake(name, default=None):
        if name in off:
            return off[name]
        return real(name, default)
    monkeypatch.setattr(A.config, "get_flag", fake)


# --------------------------------------------------------------------------- #
# the lift
# --------------------------------------------------------------------------- #

def test_tools_are_lifted_out_of_the_input_item():
    body = _codex_body()
    out = A._lift_additional_tools(body)
    assert out is body                                    # changed in place
    assert [t.get("name") for t in body["tools"]] == ["functions", "collaboration"]
    assert all(it.get("type") != "additional_tools" for it in body["input"])
    assert len(body["input"]) == 2                        # the messages stay


def test_a_body_without_the_item_is_untouched():
    body = {"model": "auto", "tools": [EXEC], "input": [{"role": "user", "content": "hi"}]}
    before = json.dumps(body, sort_keys=True)
    A._lift_additional_tools(body)
    assert json.dumps(body, sort_keys=True) == before


def test_top_level_tools_are_kept_and_a_later_definition_wins():
    newer = dict(EXEC, description="newer")
    body = {"tools": [EXEC, SPAWN],
            "input": [{"type": "additional_tools", "tools": [newer]},
                      {"role": "user", "content": "go"}]}
    A._lift_additional_tools(body)
    names = [t["name"] for t in body["tools"]]
    assert names == ["exec_command", "spawn_agent"]
    assert body["tools"][0]["description"] == "newer"


def test_lifting_twice_changes_nothing():
    body = _codex_body()
    A._lift_additional_tools(body)
    once = json.dumps(body, sort_keys=True)
    A._lift_additional_tools(body)
    assert json.dumps(body, sort_keys=True) == once


def test_flag_off_leaves_the_body_alone(monkeypatch):
    _flags(monkeypatch, responses_additional_tools=False)
    body = _codex_body()
    A._lift_additional_tools(body)
    assert "tools" not in body
    assert body["input"][0]["type"] == "additional_tools"


# --------------------------------------------------------------------------- #
# the freeform tool, offered and handed back
# --------------------------------------------------------------------------- #

def test_a_freeform_tool_is_offered_as_one_string_argument():
    tools = A._responses_tools_to_chat([PATCH])
    fn = tools[0]["function"]
    assert fn["name"] == "apply_patch"
    assert fn["parameters"]["required"] == ["input"]
    assert fn["parameters"]["properties"]["input"]["type"] == "string"
    assert "Begin Patch" in fn["description"]           # the format reaches the model
    assert A._responses_custom_map([PATCH]) == {"apply_patch": "apply_patch"}


@pytest.mark.parametrize("args, want", [
    (json.dumps({"input": PATCH_TEXT}), PATCH_TEXT),
    (PATCH_TEXT, PATCH_TEXT),                          # not JSON: the text itself
    (json.dumps(PATCH_TEXT), PATCH_TEXT),              # a JSON string
    (json.dumps({"patch": PATCH_TEXT}), PATCH_TEXT),   # one string field, other name
])
def test_the_raw_input_is_read_from_the_models_arguments(args, want):
    assert A._custom_input_from_args(args) == want


def _chat(tool_name, args):
    return {"choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "call_1", "type": "function",
                        "function": {"name": tool_name, "arguments": args}}]}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 5}}


def test_non_stream_hands_apply_patch_back_as_a_custom_call():
    tools = [{"type": "namespace", "name": "functions", "tools": [EXEC, PATCH]}]
    out = A._chat_to_responses(_chat("apply_patch", json.dumps({"input": PATCH_TEXT})),
                               "auto", tool_defs=A._responses_tools_to_chat(tools),
                               ns_map=A._responses_ns_backmap(tools),
                               custom_map=A._responses_custom_map(tools))
    item = out["output"][0]
    assert item["type"] == "custom_tool_call"
    assert item["name"] == "apply_patch" and item["input"] == PATCH_TEXT
    assert "arguments" not in item and "namespace" not in item


def test_non_stream_keeps_exec_command_a_namespaced_function_call():
    tools = [{"type": "namespace", "name": "functions", "tools": [EXEC, PATCH]}]
    out = A._chat_to_responses(_chat("exec_command", json.dumps({"cmd": "ls"})),
                               "auto", tool_defs=A._responses_tools_to_chat(tools),
                               ns_map=A._responses_ns_backmap(tools),
                               custom_map=A._responses_custom_map(tools))
    item = out["output"][0]
    assert item["type"] == "function_call"
    assert item["name"] == "exec_command" and item["namespace"] == "functions"


class _DummyResp:
    def close(self):
        pass


def _sse_tool_call(name, args):
    frames = [
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "c1",
          "type": "function", "function": {"name": name, "arguments": ""}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0,
          "function": {"arguments": args}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]
    return iter([b"data: " + json.dumps(f).encode() for f in frames] + [b"data: [DONE]"])


def _stream_events(name, args, tools):
    evs = []
    for ev in A._responses_stream(_DummyResp(), "auto", line_iter=_sse_tool_call(name, args),
                                  tool_defs=A._responses_tools_to_chat(tools),
                                  ns_map=A._responses_ns_backmap(tools),
                                  custom_map=A._responses_custom_map(tools)):
        for ln in ev.splitlines():
            if ln.startswith(b"data: "):
                try:
                    evs.append(json.loads(ln[6:]))
                except ValueError:
                    pass
    return evs


def test_stream_hands_apply_patch_back_as_a_custom_call():
    tools = [{"type": "namespace", "name": "functions", "tools": [EXEC, PATCH]}]
    evs = _stream_events("apply_patch", json.dumps({"input": PATCH_TEXT}), tools)
    kinds = [e["type"] for e in evs]
    assert "response.function_call_arguments.delta" not in kinds
    assert "response.function_call_arguments.done" not in kinds
    added = [e["item"] for e in evs if e["type"] == "response.output_item.added"]
    done = [e["item"] for e in evs if e["type"] == "response.output_item.done"]
    assert added[0]["type"] == "custom_tool_call" and added[0]["name"] == "apply_patch"
    assert done[0]["type"] == "custom_tool_call" and done[0]["input"] == PATCH_TEXT
    final = [e for e in evs if e["type"] == "response.completed"][0]["response"]["output"]
    assert final[0]["type"] == "custom_tool_call"


def test_stream_keeps_exec_command_a_namespaced_function_call():
    tools = [{"type": "namespace", "name": "functions", "tools": [EXEC, PATCH]}]
    evs = _stream_events("exec_command", json.dumps({"cmd": "wsl -e ls"}), tools)
    done = [e["item"] for e in evs if e["type"] == "response.output_item.done"]
    assert done[0]["type"] == "function_call"
    assert done[0]["name"] == "exec_command" and done[0]["namespace"] == "functions"


def test_custom_flag_off_is_the_old_behaviour(monkeypatch):
    _flags(monkeypatch, responses_custom_tools=False)
    tools = [PATCH]
    assert A._responses_custom_map(tools) == {}
    fn = A._responses_tools_to_chat(tools)[0]["function"]
    assert fn["parameters"] == {"type": "object", "properties": {}}
    out = A._chat_to_responses(_chat("apply_patch", "{}"), "auto",
                               custom_map=A._responses_custom_map(tools))
    assert out["output"][0]["type"] == "function_call"


# --------------------------------------------------------------------------- #
# the history the model sees
# --------------------------------------------------------------------------- #

def test_a_custom_call_and_its_output_reach_the_model():
    body = {"input": [{"role": "user", "content": "add the file"},
                      {"type": "custom_tool_call", "call_id": "k1", "name": "apply_patch",
                       "input": PATCH_TEXT},
                      {"type": "custom_tool_call_output", "call_id": "k1",
                       "output": "Success. Updated the following files:\nA hello.txt"}]}
    msgs = A._responses_to_chat(body)
    call = msgs[1]["tool_calls"][0]
    assert call["id"] == "k1" and call["function"]["name"] == "apply_patch"
    assert json.loads(call["function"]["arguments"]) == {"input": PATCH_TEXT}
    assert msgs[2] == {"role": "tool", "tool_call_id": "k1",
                       "content": "Success. Updated the following files:\nA hello.txt"}


def test_a_nameless_custom_call_is_dropped_with_its_output():
    body = {"input": [{"role": "user", "content": "go"},
                      {"type": "custom_tool_call", "call_id": "k2", "name": "", "input": "x"},
                      {"type": "custom_tool_call_output", "call_id": "k2", "output": "?"}]}
    msgs = A._responses_to_chat(body)
    assert [m["role"] for m in msgs] == ["user"]


# --------------------------------------------------------------------------- #
# the real handler, end to end
# --------------------------------------------------------------------------- #

class _Resp:
    status_code = 200
    text = ""

    def __init__(self, data):
        self.headers = {}
        self._data = data

    def json(self):
        return self._data

    def close(self):
        pass


@pytest.fixture
def upstream(monkeypatch):
    seen = {"payloads": [], "reply": None}
    for name in ("_record_chat_usage", "_save_perf_stats", "_act_pick", "_note_ttft",
                 "_note_provider_timeout", "_throttle_failed_hop", "_v1_observe"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)
    monkeypatch.setattr(A, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(A, "_model_block_reason", lambda pid, m: None)
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    monkeypatch.setattr(A, "_is_trivial_turn", lambda *a, **k: False)
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: 20)
    monkeypatch.setattr(A, "_stream_peek_timeout", lambda *a, **k: 1.0)
    monkeypatch.setattr(A, "_record_outcome", lambda *a, **k: None)
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("p1", "m1", "hard"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("p1", "m1")])

    def dispatch(pid, payload, stream):
        seen["payloads"].append(payload)
        return _Resp(seen["reply"])
    monkeypatch.setattr(A, "_dispatch_chat", dispatch)
    yield seen


def _offered(payload):
    return {(t.get("function") or {}).get("name") for t in payload.get("tools") or []}


def test_the_models_get_codexs_tools_and_a_command_comes_back(upstream):
    upstream["reply"] = _chat("exec_command", json.dumps({"cmd": "wsl -e ls ~"}))
    r = A.app.test_client().post("/v1/responses", json=_codex_body())
    assert r.status_code == 200, r.get_data()[:300]
    offered = _offered(upstream["payloads"][0])
    assert {"exec_command", "apply_patch", "spawn_agent"} <= offered
    item = r.get_json()["output"][0]
    assert item["type"] == "function_call"
    assert item["name"] == "exec_command" and item["namespace"] == "functions"
    assert json.loads(item["arguments"]) == {"cmd": "wsl -e ls ~"}


def test_an_edit_comes_back_as_a_patch_codex_can_apply(upstream):
    upstream["reply"] = _chat("apply_patch", json.dumps({"input": PATCH_TEXT}))
    r = A.app.test_client().post("/v1/responses", json=_codex_body())
    assert r.status_code == 200, r.get_data()[:300]
    item = r.get_json()["output"][0]
    assert item == {**item, "type": "custom_tool_call", "name": "apply_patch",
                    "input": PATCH_TEXT}


def test_with_the_lift_off_the_models_get_no_tools(upstream, monkeypatch):
    """The negative control: what codex 0.165 turns looked like before the fix."""
    _flags(monkeypatch, responses_additional_tools=False)
    upstream["reply"] = {"choices": [{"index": 0, "finish_reason": "stop", "message": {
        "role": "assistant", "content": "I can't reach your WSL from here."}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 5}}
    r = A.app.test_client().post("/v1/responses", json=_codex_body())
    assert r.status_code == 200
    assert not upstream["payloads"][0].get("tools")


# --------------------------------------------------------------------------- #
# pipeline tiers (swarm / multi / crews) replay their actor's call to codex
# --------------------------------------------------------------------------- #

def _swarm_reply(monkeypatch, name, args):
    monkeypatch.setattr(A, "_swarm_for", lambda *a, **k: (_chat(name, args), {}))
    monkeypatch.setattr(A, "_pipeline_usage", lambda *a, **k: None)


def _swarm_items(stream):
    body = _codex_body(stream=stream)
    A._lift_additional_tools(body)
    tools = A._responses_tools_to_chat(body["tools"])
    with A.app.test_request_context():
        resp = A._swarm_as_responses(body, [], tools, 100)
    if not stream:
        return resp[0].get_json()["output"]
    evs = []
    for ln in b"".join(resp.response).splitlines():
        if ln.startswith(b"data: "):
            try:
                evs.append(json.loads(ln[6:]))
            except ValueError:
                pass
    return [e["item"] for e in evs if e["type"] == "response.output_item.done"]


@pytest.mark.parametrize("stream", [False, True])
def test_a_swarm_tier_hands_apply_patch_back_as_a_custom_call(monkeypatch, stream):
    _swarm_reply(monkeypatch, "apply_patch", json.dumps({"input": PATCH_TEXT}))
    item = _swarm_items(stream)[0]
    assert item["type"] == "custom_tool_call"
    assert item["name"] == "apply_patch" and item["input"] == PATCH_TEXT


@pytest.mark.parametrize("stream", [False, True])
def test_a_swarm_tier_keeps_a_commands_namespace(monkeypatch, stream):
    _swarm_reply(monkeypatch, "exec_command", json.dumps({"cmd": "wsl -e ls"}))
    item = _swarm_items(stream)[0]
    assert item["type"] == "function_call"
    assert item["name"] == "exec_command" and item["namespace"] == "functions"
