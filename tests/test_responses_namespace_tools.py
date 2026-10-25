"""codex advertises its native sub-agents and the hub's own MCP tools as
`type: "namespace"` tools (`multi_agent_v1`, `mcp__free_llm_hub`). The old
Responses->chat converter dropped the children, so the free model saw one
empty uncallable function per namespace and a call came back to codex as
"unsupported call: <namespace>" -- the hub's crew/swarm tools (and codex's
native spawn_agent) were never reachable.

VERIFIED LIVE 2026-10-25 against the installed codex: a function_call with
name=<child> + a `namespace` field dispatches; name alone, or a glued
`ns__child` name, answer "unsupported call". So the hub expands each child
into a callable function and emits the model's call back as the bare child
name plus the namespace.
"""
import json

import app as A


TOOLS = [
    {"type": "function", "name": "exec_command",
     "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}},
                    "required": ["cmd"]}},
    {"type": "namespace", "name": "mcp__free_llm_hub", "tools": [
        {"type": "function", "name": "swarm_windows_start",
         "parameters": {"type": "object", "properties": {"goal": {"type": "string"}}}},
        {"type": "function", "name": "crew_run",
         "parameters": {"type": "object", "properties": {}}},
    ]},
    {"type": "namespace", "name": "multi_agent_v1", "tools": [
        {"type": "function", "name": "spawn_agent",
         "parameters": {"type": "object", "properties": {"task_name": {"type": "string"}}}},
    ]},
]


def test_children_become_callable_functions():
    chat = A._responses_tools_to_chat(TOOLS)
    names = [t["function"]["name"] for t in chat]
    assert "exec_command" in names
    assert "swarm_windows_start" in names and "crew_run" in names
    assert "spawn_agent" in names
    # the namespace group names themselves are NOT presented as callable tools
    assert "mcp__free_llm_hub" not in names and "multi_agent_v1" not in names
    # child schema is carried, not an empty stub
    sw = next(t for t in chat if t["function"]["name"] == "swarm_windows_start")
    assert "goal" in sw["function"]["parameters"]["properties"]


def test_backmap_points_each_child_at_its_namespace():
    bm = A._responses_ns_backmap(TOOLS)
    assert bm["swarm_windows_start"] == ("swarm_windows_start", "mcp__free_llm_hub")
    assert bm["spawn_agent"] == ("spawn_agent", "multi_agent_v1")
    assert "exec_command" not in bm


def test_non_stream_emits_child_name_plus_namespace():
    chat_json = {"choices": [{"message": {"role": "assistant", "content": None,
                 "tool_calls": [{"id": "c1", "type": "function",
                                 "function": {"name": "swarm_windows_start",
                                              "arguments": '{"goal":"x"}'}}]}}],
                 "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    out = A._chat_to_responses(chat_json, "auto", tool_defs=A._responses_tools_to_chat(TOOLS),
                               ns_map=A._responses_ns_backmap(TOOLS))
    fc = [o for o in out["output"] if o.get("type") == "function_call"][0]
    assert fc["name"] == "swarm_windows_start"
    assert fc["namespace"] == "mcp__free_llm_hub"
    assert json.loads(fc["arguments"]) == {"goal": "x"}


def test_plain_tool_gets_no_namespace():
    chat_json = {"choices": [{"message": {"role": "assistant", "content": None,
                 "tool_calls": [{"id": "c1", "type": "function",
                                 "function": {"name": "exec_command",
                                              "arguments": '{"cmd":"ls"}'}}]}}]}
    out = A._chat_to_responses(chat_json, "auto", tool_defs=A._responses_tools_to_chat(TOOLS),
                               ns_map=A._responses_ns_backmap(TOOLS))
    fc = [o for o in out["output"] if o.get("type") == "function_call"][0]
    assert fc["name"] == "exec_command" and "namespace" not in fc


def _chat_sse_tool_call(name, args):
    frames = [
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "c1",
          "type": "function", "function": {"name": name, "arguments": ""}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0,
          "function": {"arguments": args}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]
    lines = [b"data: " + json.dumps(f).encode() for f in frames]
    lines.append(b"data: [DONE]")
    return iter(lines)


class _DummyResp:
    def close(self):
        pass


def _run_stream(name, args, ns_map):
    events = list(A._responses_stream(
        _DummyResp(), "auto", line_iter=_chat_sse_tool_call(name, args),
        tool_defs=A._responses_tools_to_chat(TOOLS), ns_map=ns_map))
    items = []
    for ev in events:
        for ln in ev.splitlines():
            if ln.startswith(b"data: "):
                try:
                    obj = json.loads(ln[6:])
                except Exception:
                    continue
                if obj.get("type") in ("response.output_item.added",
                                       "response.output_item.done"):
                    it = obj.get("item") or {}
                    if it.get("type") == "function_call":
                        items.append((obj["type"], it.get("name"), it.get("namespace")))
    return items


def test_stream_emits_child_name_plus_namespace():
    items = _run_stream("crew_run", "{}", A._responses_ns_backmap(TOOLS))
    assert items, "no function_call item emitted"
    for _kind, name, ns in items:
        assert name == "crew_run"
        assert ns == "mcp__free_llm_hub"


def test_collision_is_qualified_and_maps_back():
    tools = [
        {"type": "namespace", "name": "alpha", "tools": [
            {"type": "function", "name": "status", "parameters": {"type": "object", "properties": {}}}]},
        {"type": "namespace", "name": "beta", "tools": [
            {"type": "function", "name": "status", "parameters": {"type": "object", "properties": {}}}]},
    ]
    chat = A._responses_tools_to_chat(tools)
    names = sorted(t["function"]["name"] for t in chat)
    assert names == ["alpha__status", "beta__status"]
    bm = A._responses_ns_backmap(tools)
    assert bm["alpha__status"] == ("status", "alpha")
    assert bm["beta__status"] == ("status", "beta")


def test_flag_off_is_the_old_behaviour(monkeypatch):
    monkeypatch.setattr(A.config, "get_flag",
                        lambda name, default=None: False if name == "responses_namespace_tools" else default)
    chat = A._responses_tools_to_chat(TOOLS)
    names = [t["function"]["name"] for t in chat]
    # the namespace collapses to one function named after itself, children dropped
    assert "mcp__free_llm_hub" in names
    assert "swarm_windows_start" not in names and "spawn_agent" not in names
    assert A._responses_ns_backmap(TOOLS) == {}
