"""Protocol / CLI leftovers (2026-09-27), one block per item:

1. kimi: Kimi Code 0.39.1 reads <KIMI_CODE_HOME or ~/.kimi-code>/mcp.json
   ({"mcpServers": {name: {"transport": ..}}}), not the [mcp_servers] tables
   of ~/.kimi/config.toml -- mcp_manager writes/reads/deletes the file the
   installed kimi uses, and still lists/removes legacy TOML entries.
2. hermes: Connect writes per-tier context_length on a named providers: entry
   (matched by base URL, as hermes_cli/config_providers.py does); Disconnect
   removes it.
3. /v1/responses never emits a function_call without a name: repaired from
   the arguments when unambiguous, else dropped (and an all-nameless stream
   is the next hop, not a committed dead turn).
4. /v1/responses carries the served provider/model (headers + metadata)
   while `model` still echoes what the client asked for.
5. dahl 400 "This model is not currently offered: X" = a 60 s skip, not a
   hop failure, not a dead mark, not the relayed error.
6. slowness by measured TTFT, name regex only as fallback.

Every config is written under a temp home; nothing real is touched.
"""
import json
import os
import shutil
import tempfile
import time

import pytest
import yaml

import app as A
import mcp_manager as m


@pytest.fixture
def tmp_path():
    """Own temp dir: pytest's tmp_path hits a known PermissionError on this
    machine (see AGENTS.md "Tests")."""
    import pathlib
    d = tempfile.mkdtemp(prefix="hub-pytest-protomisc-")
    try:
        yield pathlib.Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 1. kimi MCP file
# --------------------------------------------------------------------------- #

@pytest.fixture
def kimi_home(monkeypatch):
    d = tempfile.mkdtemp(prefix="hub-pytest-kimimcp-")
    monkeypatch.setenv("MCP_MANAGER_HOME", d)
    monkeypatch.delenv("KIMI_CODE_HOME", raising=False)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _legacy(d):
    return os.path.join(d, ".kimi", "config.toml")


def _mcp_json(d):
    return os.path.join(d, ".kimi-code", "mcp.json")


def test_kimi_code_home_gets_mcp_json_in_kimis_own_shape(kimi_home):
    os.makedirs(os.path.join(kimi_home, ".kimi-code"))
    assert m._config_path("kimi") == _mcp_json(kimi_home)
    ok, msg = m.add_server("kimi", "free-llm-hub", {"url": "http://127.0.0.1:8787/mcp"})
    assert ok, msg
    ok, msg = m.add_server("kimi", "pw", {"command": "npx", "args": ["-y", "pw"],
                                          "env": {"A": "1"}})
    assert ok, msg
    data = json.load(open(_mcp_json(kimi_home), encoding="utf-8"))
    # Kimi Code's zod schema: discriminated on "transport".
    assert data["mcpServers"]["free-llm-hub"] == {
        "transport": "http", "url": "http://127.0.0.1:8787/mcp"}
    assert data["mcpServers"]["pw"] == {
        "transport": "stdio", "command": "npx", "args": ["-y", "pw"], "env": {"A": "1"}}
    assert not os.path.exists(_legacy(kimi_home)), "legacy file must not be created"
    names = {e["name"]: e for e in m.list_servers()["kimi"]}
    assert names["free-llm-hub"]["transport"] == "http"
    assert names["pw"]["command"] == "npx"
    ok, _ = m.remove_server("kimi", "pw")
    assert ok
    data = json.load(open(_mcp_json(kimi_home), encoding="utf-8"))
    assert list(data["mcpServers"]) == ["free-llm-hub"]


def test_kimi_mcp_json_keeps_other_keys_and_servers(kimi_home):
    p = _mcp_json(kimi_home)
    os.makedirs(os.path.dirname(p))
    open(p, "w", encoding="utf-8").write(json.dumps(
        {"mcpServers": {"mine": {"transport": "sse", "url": "http://x/sse"}},
         "other": 1}))
    ok, msg = m.add_server("kimi", "free-llm-hub", {"url": "http://h/mcp"})
    assert ok, msg
    data = json.load(open(p, encoding="utf-8"))
    assert data["other"] == 1
    assert data["mcpServers"]["mine"] == {"transport": "sse", "url": "http://x/sse"}
    assert os.path.isfile(p + m._BACKUP_SUFFIX)


def test_kimi_fresh_machine_defaults_to_kimi_code(kimi_home):
    # No config at all: the current `kimi` is Kimi Code -> mcp.json.
    assert m._config_path("kimi") == _mcp_json(kimi_home)


def test_legacy_only_machine_keeps_toml(kimi_home):
    lp = _legacy(kimi_home)
    os.makedirs(os.path.dirname(lp))
    open(lp, "w", encoding="utf-8").write('default_model = "x"\n')
    assert m._config_path("kimi") == lp
    ok, msg = m.add_server("kimi", "free-llm-hub", {"url": "http://h/mcp"})
    assert ok, msg
    assert "[mcp_servers.free-llm-hub]" in open(lp, encoding="utf-8").read()
    assert [e["name"] for e in m.list_servers()["kimi"]] == ["free-llm-hub"]


def test_legacy_toml_entries_stay_visible_and_removable_after_upgrade(kimi_home):
    """An older hub wrote [mcp_servers.*] into ~/.kimi/config.toml; once Kimi
    Code is installed the entry must still be listed and removable."""
    lp = _legacy(kimi_home)
    os.makedirs(os.path.dirname(lp))
    open(lp, "w", encoding="utf-8").write(
        'default_model = "x"\n\n[mcp_servers.free-llm-hub]\nurl = "http://h/mcp"\n')
    os.makedirs(os.path.join(kimi_home, ".kimi-code"))
    listed = [e["name"] for e in m.list_servers()["kimi"]]
    assert listed == ["free-llm-hub"]
    # Installing again writes the ACTIVE file (not "exists" from the legacy one).
    ok, msg = m.add_server("kimi", "free-llm-hub", {"url": "http://h/mcp"})
    assert ok, msg
    assert os.path.isfile(_mcp_json(kimi_home))
    ok, msg = m.remove_server("kimi", "free-llm-hub")
    assert ok, msg
    assert "free-llm-hub" not in open(lp, encoding="utf-8").read()
    assert 'default_model = "x"' in open(lp, encoding="utf-8").read()
    data = json.load(open(_mcp_json(kimi_home), encoding="utf-8"))
    assert data["mcpServers"] == {}
    assert m.remove_server("kimi", "free-llm-hub") == (False, "not found")


def test_kimi_code_home_env_is_honoured_outside_tests(monkeypatch, tmp_path):
    monkeypatch.delenv("MCP_MANAGER_HOME", raising=False)
    monkeypatch.setenv("KIMI_CODE_HOME", str(tmp_path / "kc"))
    assert m._kimi_code_home() == str(tmp_path / "kc")
    # ...but never under a test's MCP_MANAGER_HOME override.
    monkeypatch.setenv("MCP_MANAGER_HOME", str(tmp_path / "h"))
    assert m._kimi_code_home() == os.path.join(str(tmp_path / "h"), ".kimi-code")


def test_hub_mcp_kept_and_strip_see_the_kimi_json(kimi_home):
    os.makedirs(os.path.join(kimi_home, ".kimi-code"))
    m.add_server("kimi", "free-llm-hub", {"url": "http://127.0.0.1:8787/mcp"})
    assert A._hub_mcp_kept("kimi") == _mcp_json(kimi_home)
    text = open(_mcp_json(kimi_home), encoding="utf-8").read()
    assert "8787" not in A._strip_hub_mcp_table(text)


def test_hermes_remove_works_on_yaml(tmp_path, monkeypatch):
    """remove_server had no yaml branch: every hermes remove was refused."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("MCP_MANAGER_HOME", str(tmp_path))
    assert m.add_server("hermes", "a", {"url": "http://a/mcp"})[0]
    assert m.add_server("hermes", "b", {"url": "http://b/mcp"})[0]
    ok, msg = m.remove_server("hermes", "a")
    assert ok, msg
    assert [e["name"] for e in m.list_servers()["hermes"]] == ["b"]


# --------------------------------------------------------------------------- #
# 2. hermes context windows
# --------------------------------------------------------------------------- #

KEY = "sk-local-test"
ROOT = "http://127.0.0.1:8787"
V1 = ROOT + "/v1"


def _hermes_entry(path):
    return {"id": "hermes", "write_path": path, "config_paths": [path],
            "bins": ["hermes"], "env_check": []}


def test_hermes_connect_declares_per_tier_windows_and_disconnect_cleans(tmp_path, monkeypatch):
    monkeypatch.setattr(A.agentic_chat, "declared_window",
                        lambda mid, cli=None: {"auto": 200000, "best": 150000}.get(mid, 128000))
    hdir = tmp_path / "hermes"
    hdir.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hdir))
    cfg = hdir / "config.yaml"
    original = {"soul": "keep", "providers": {"mine": {"api": "http://x/v1"}}}
    cfg.write_text(yaml.safe_dump(original), encoding="utf-8")
    entry = _hermes_entry(str(cfg))
    r = A._autofix_hermes(entry, KEY, ROOT, V1, "m")
    assert r["ok"], r
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    ours = data["providers"][A._HERMES_PROVIDER_KEY]
    # Hermes matches the entry to the active route by URL: same as model.base_url.
    assert ours["api"] == data["model"]["base_url"] == V1
    assert data["model"]["provider"] == "custom"
    assert ours["models"]["auto"]["context_length"] == 200000
    assert ours["models"]["best"]["context_length"] == 150000
    for mid in A._HUB_TIER_IDS:
        assert isinstance(ours["models"][mid]["context_length"], int)
    assert ours["context_length"] == 200000
    assert data["providers"]["mine"] == {"api": "http://x/v1"}
    assert KEY not in json.dumps(r["applied"])   # key never echoed
    d = A._disconnect_hermes(entry)
    assert d["changed"]
    assert yaml.safe_load(cfg.read_text(encoding="utf-8")) == original


def test_hermes_disconnect_keeps_a_foreign_entry_with_our_name(tmp_path):
    cfg = tmp_path / "config.yaml"
    original = {"providers": {A._HERMES_PROVIDER_KEY: {"api": "http://elsewhere/v1"}}}
    cfg.write_text(yaml.safe_dump(original), encoding="utf-8")
    A._disconnect_hermes(_hermes_entry(str(cfg)))
    assert yaml.safe_load(cfg.read_text(encoding="utf-8")) == original


def test_hermes_created_file_is_removed_on_disconnect(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    cfg = tmp_path / "hermes" / "config.yaml"
    entry = _hermes_entry(str(cfg))
    assert A._autofix_hermes(entry, KEY, ROOT, V1, "m")["ok"]
    d = A._disconnect_hermes(entry)
    assert d.get("deleted") and not cfg.exists()


# --------------------------------------------------------------------------- #
# 3. nameless tool calls on /v1/responses
# --------------------------------------------------------------------------- #

TOOLS = [
    {"type": "function", "function": {"name": "shell", "parameters": {
        "type": "object", "properties": {"command": {"type": "array"},
                                         "workdir": {"type": "string"}},
        "required": ["command"]}}},
    {"type": "function", "function": {"name": "read_file", "parameters": {
        "type": "object", "properties": {"path": {"type": "string"}},
        "required": ["path"]}}},
]


def _frame(delta, fin=None):
    ch = {"delta": delta}
    if fin:
        ch["finish_reason"] = fin
    return b"data: " + json.dumps({"choices": [ch]}).encode()


class _FakeResp:
    status_code = 200
    headers = {}
    text = ""

    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self, decode_unicode=False):
        return iter(self._lines)

    def close(self):
        pass


def _events(gen):
    out = []
    for chunk in gen:
        if isinstance(chunk, (bytes, bytearray)):
            chunk = chunk.decode("utf-8")
        for block in str(chunk).split("\n\n"):
            for line in block.split("\n"):
                if line.startswith("data: "):
                    try:
                        out.append(json.loads(line[6:]))
                    except ValueError:
                        pass
    return out


@pytest.fixture
def no_ledger(monkeypatch):
    for name in ("_record_stream_outcome", "_record_outcome"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)


def _nameless_lines(args):
    return [_frame({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                    "function": {"arguments": ""}}]}),
            _frame({"tool_calls": [{"index": 0, "function": {"arguments": args}}]}),
            _frame({}, fin="tool_calls"), b"data: [DONE]"]


def _function_calls(evs):
    items = [e.get("item") for e in evs if e.get("type") in (
        "response.output_item.added", "response.output_item.done")]
    items = [i for i in items if i and i.get("type") == "function_call"]
    done = [e for e in evs if e.get("type") == "response.completed"]
    final = [o for o in done[-1]["response"]["output"] if o.get("type") == "function_call"]
    return items, final


def test_a_nameless_call_is_repaired_from_unambiguous_arguments(no_ledger):
    lines = _nameless_lines('{"path": "a.py"}')
    evs = _events(A._responses_stream(_FakeResp(lines), "auto", line_iter=iter(lines),
                                      tool_defs=TOOLS))
    items, final = _function_calls(evs)
    assert items and all(i["name"] == "read_file" for i in items)
    assert final and final[0]["name"] == "read_file"
    assert json.loads(final[0]["arguments"]) == {"path": "a.py"}


def test_an_ambiguous_nameless_call_is_never_emitted(no_ledger):
    lines = _nameless_lines('{"x": 1}')
    evs = _events(A._responses_stream(_FakeResp(lines), "auto", line_iter=iter(lines),
                                      tool_defs=TOOLS))
    items, final = _function_calls(evs)
    assert items == [] and final == []
    assert not any(e.get("type") == "response.function_call_arguments.delta" for e in evs)


def test_a_name_on_a_later_delta_still_streams_normally(no_ledger):
    lines = [_frame({"tool_calls": [{"index": 0, "id": "c", "function": {"arguments": ""}}]}),
             _frame({"tool_calls": [{"index": 0, "function": {"name": "shell"}}]}),
             _frame({"tool_calls": [{"index": 0, "function": {"arguments": '{"command":'}}]}),
             _frame({"tool_calls": [{"index": 0, "function": {"arguments": '["ls"]}'}}]}),
             b"data: [DONE]"]
    evs = _events(A._responses_stream(_FakeResp(lines), "auto", line_iter=iter(lines),
                                      tool_defs=TOOLS))
    items, final = _function_calls(evs)
    assert items and all(i["name"] == "shell" for i in items)
    assert json.loads(final[0]["arguments"]) == {"command": ["ls"]}
    deltas = "".join(e["delta"] for e in evs
                     if e.get("type") == "response.function_call_arguments.delta")
    assert json.loads(deltas) == {"command": ["ls"]}
    # output indices stay 0-based and contiguous
    assert [o.get("output_index") for o in evs
            if o.get("type") == "response.output_item.added"] == [0]


def test_a_nameless_stream_that_ends_is_the_next_hop_in_the_peek():
    check = {"tools": TOOLS, "tools_offered": True}
    st, _ = A._peek_until_content(iter(_nameless_lines('{"x": 1}')), 5, check=check)
    assert st == "empty"
    st, _ = A._peek_until_content(iter(_nameless_lines('{"path": "a"}')), 5, check=check)
    assert st == "content"
    named = [_frame({"tool_calls": [{"index": 0, "id": "c",
                                     "function": {"name": "shell", "arguments": ""}}]})]
    st, _ = A._peek_until_content(iter(named + [b"data: [DONE]"]), 5, check=check)
    assert st == "content"


def test_non_stream_nameless_calls_are_repaired_or_dropped():
    data = {"choices": [{"message": {"content": "ok", "tool_calls": [
        {"id": "a", "type": "function", "function": {"name": "", "arguments": '{"path":"p"}'}},
        {"id": "b", "type": "function", "function": {"name": "", "arguments": '{"q":1}'}}]}}]}
    out = A._chat_to_responses(data, "auto", tool_defs=TOOLS)
    calls = [o for o in out["output"] if o["type"] == "function_call"]
    assert [c["name"] for c in calls] == ["read_file"]
    only_bad = {"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "b", "type": "function", "function": {"name": "", "arguments": "{}"}}]}}]}
    assert A._chat_json_is_empty(A._fix_nameless_tool_calls(only_bad, TOOLS))


def test_infer_tool_name_is_strict():
    assert A._infer_tool_name('{"command": ["ls"]}', TOOLS) == "shell"
    assert A._infer_tool_name('{"workdir": "x"}', TOOLS) is None      # required missing
    assert A._infer_tool_name('not json', TOOLS) is None
    assert A._infer_tool_name('{"path": "a"}', None) is None


# --------------------------------------------------------------------------- #
# 4 + 5. /v1/responses served headers/metadata; dahl "not currently offered"
# --------------------------------------------------------------------------- #

class _Resp:
    def __init__(self, status=200, payload=None, lines=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self._lines = lines
        self.headers = {}
        self.text = text
        self.closed = False

    def json(self):
        if self.text:
            return json.loads(self.text)
        return self._payload

    def close(self):
        self.closed = True

    def iter_lines(self, decode_unicode=False):
        return iter(self._lines or ())

    def iter_content(self, chunk_size=None):
        return iter(self._lines or ())


@pytest.fixture
def quiet(monkeypatch):
    outcomes = []
    for name in ("_record_chat_usage", "_save_perf_stats", "_act_pick", "_note_ttft",
                 "_record_stream_outcome", "_note_provider_timeout",
                 "_throttle_failed_hop"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)
    monkeypatch.setattr(A, "_record_outcome",
                        lambda pid, model, ok, **k: outcomes.append((pid, model, ok)))
    monkeypatch.setattr(A, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(A, "_model_block_reason", lambda pid, m: None)
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    monkeypatch.setattr(A, "_is_trivial_turn", lambda *a, **k: False)
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("dahl", "m1", "hard"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("dahl", "m1"), ("p2", "m2")])
    yield outcomes


NOT_OFFERED = json.dumps({"error": {"message": "This model is not currently offered: m1"}})


def _dispatch(stream_lines=None, answer=None):
    def fake(pid, payload, stream):
        if pid == "dahl":
            return _Resp(400, text=NOT_OFFERED)
        if stream:
            return _Resp(200, lines=stream_lines)
        return _Resp(200, payload=answer)
    return fake


def test_not_offered_walks_on_without_a_failure_and_headers_name_the_server(quiet, monkeypatch):
    monkeypatch.setattr(A, "_dispatch_chat", _dispatch(answer={
        "choices": [{"message": {"role": "assistant", "content": "The answer is 42."},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3}}))
    r = A.app.test_client().post("/v1/responses", json={
        "model": "auto", "input": "what is six times seven, explained"})
    assert r.status_code == 200, r.get_data()
    body = r.get_json()
    assert body["model"] == "auto"                          # codex keys on this
    assert body["metadata"] == {"free_llm_hub_provider": "p2", "free_llm_hub_model": "m2"}
    assert r.headers["X-Free-LLM-Hub-Provider"] == "p2"
    assert r.headers["X-Free-LLM-Hub-Model"] == "m2"
    assert ("dahl", "m1", False) not in quiet               # not a hop failure


def test_streamed_responses_carry_served_headers_and_metadata(quiet, monkeypatch):
    lines = [_frame({"content": "The answer is forty-two, because six sevens are "
                                "forty-two and nothing else about it is surprising."}),
             _frame({}, fin="stop"), b"data: [DONE]"]
    monkeypatch.setattr(A, "_dispatch_chat", _dispatch(stream_lines=lines))
    r = A.app.test_client().post("/v1/responses", json={
        "model": "auto", "stream": True, "input": "what is six times seven, explained"})
    raw = r.get_data(as_text=True)
    assert r.headers["X-Free-LLM-Hub-Provider"] == "p2"
    assert r.headers["X-Free-LLM-Hub-Model"] == "m2"
    evs = _events([raw])
    done = [e for e in evs if e.get("type") == "response.completed"][-1]["response"]
    assert done["model"] == "auto"
    assert done["metadata"]["free_llm_hub_provider"] == "p2"
    assert done["metadata"]["free_llm_hub_model"] == "m2"


def test_all_hops_not_offered_never_relays_the_upstream_400(quiet, monkeypatch):
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("dahl", "m1")])
    monkeypatch.setattr(A, "_dispatch_chat", _dispatch())
    monkeypatch.setattr(A, "_CHAIN_RETRY_DELAY", 0)
    r = A.app.test_client().post("/v1/responses", json={
        "model": "auto", "input": "what is six times seven, explained"})
    assert r.status_code != 400
    assert "not currently offered" not in r.get_data(as_text=True)
    assert ("dahl", "m1", False) not in quiet


def test_upstream_chat_files_a_short_skip_not_a_dead_mark(monkeypatch):
    monkeypatch.setattr(A.config, "get_provider_config",
                        lambda pid: {"api_keys": ["k1"], "enabled": True})
    monkeypatch.setattr(A, "_next_key_start", lambda pid, n: 0)
    monkeypatch.setattr(A.requests, "post", lambda url, **kw: _Resp(400, text=NOT_OFFERED))
    monkeypatch.setattr(A, "_not_offered", {})
    marked = []
    monkeypatch.setattr(A, "_mark_model_dead", lambda *a: marked.append(a))
    resp = A._upstream_chat("dahl", {"model": "zz-model", "messages": []}, False)
    assert resp.status_code == 400
    assert marked == []
    assert A._is_not_offered("dahl", "zz-model")
    assert A._is_model_skipped("dahl", "zz-model")
    assert not A._is_model_dead("dahl", "zz-model")         # dashboard: not dead
    # expires after the TTL
    A._not_offered[("dahl", "zz-model")] = time.time() - 1
    assert not A._is_model_skipped("dahl", "zz-model")


def test_skip_ttl_is_short():
    assert 30 <= A._NOT_OFFERED_TTL <= 120


# --------------------------------------------------------------------------- #
# 6. slowness from measured TTFT
# --------------------------------------------------------------------------- #

def _ttfts(monkeypatch, pid, model, values):
    monkeypatch.setattr(A, "_ttft", {})
    for v in values:
        A._record_ttft(pid, model, v)


def test_measured_ttft_makes_openai_fast_slow(monkeypatch):
    _ttfts(monkeypatch, "pollinations", "openai-fast", [30000, 34000, 36000, 33000, 35000])
    assert not A._SLOW_MODEL_RE.search("openai-fast")
    assert A._is_slow_model("pollinations", "openai-fast")
    assert A._stream_peek_timeout("openai-fast", 100, pid="pollinations") \
        == A.STREAM_SLOW_PEEK_TIMEOUT
    assert A._stream_peek_timeout("openai-fast", 100) == A.STREAM_CONTENT_PEEK_TIMEOUT


def test_measured_fast_overrides_the_name(monkeypatch):
    _ttfts(monkeypatch, "groq", "gpt-oss-120b", [800, 900, 1000, 1200, 1500])
    assert A._SLOW_MODEL_RE.search("gpt-oss-120b")
    assert not A._is_slow_model("groq", "gpt-oss-120b")


def test_few_samples_fall_back_to_the_name(monkeypatch):
    _ttfts(monkeypatch, "pollinations", "openai-fast", [34000, 34000])
    assert not A._is_slow_model("pollinations", "openai-fast")
    _ttfts(monkeypatch, "x", "deepseek-r1", [900])
    assert A._is_slow_model("x", "deepseek-r1")


def test_mixed_measurement_falls_back_to_the_name(monkeypatch):
    # p50 fast, p95 slow: not decisive either way.
    _ttfts(monkeypatch, "x", "deepseek-r1", [1000] * 8 + [40000] * 2)
    assert A._is_slow_model("x", "deepseek-r1")
    _ttfts(monkeypatch, "x", "llama-3.3-70b", [1000] * 8 + [40000] * 2)
    assert not A._is_slow_model("x", "llama-3.3-70b")


def test_trivial_budget_uses_measured_slowness(monkeypatch):
    _ttfts(monkeypatch, "pollinations", "openai-fast", [30000] * 6)
    seen = {}
    monkeypatch.setattr(A, "_adaptive_hop_budget",
                        lambda pid, model, ceiling, stream=None: seen.setdefault("c", ceiling))
    A._ChainClock(trivial=True)._budget_for("pollinations", "openai-fast")
    assert seen["c"] == A._TRIVIAL_SLOW_HOP_BUDGET
