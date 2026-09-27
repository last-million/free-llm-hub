"""Review fixes: exact facts never carry credentials, a reasoning model keeps
its big-prompt peek, Kimi's MCP target follows KIMI_CODE_HOME, a long
nameless tool stream is judged, and three dashboard fixes."""
import json
import os
import shutil
import tempfile

import pytest

import app as A
import ctxwin
import mcp_manager as m


HTML = open(os.path.join(os.path.dirname(A.__file__), "templates", "index.html"),
            encoding="utf-8").read()


# --------------------------------------------------------------------------- #
# exact facts: no credentials
# --------------------------------------------------------------------------- #

def test_exact_facts_never_carry_a_password_or_an_api_key():
    msgs = [
        {"role": "user", "content": "The db password is Hunter2xyz99 and the api key "
                                    "is sk-proj-abcdef1234567890. Always use port 8787."},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "shell", "arguments": json.dumps({"command": "cat config.env"})}}]},
        {"role": "tool", "tool_call_id": "c1",
         "content": "OPENAI_API_KEY=sk-live-abcdef1234567890\nPORT=8787"},
    ]
    facts = ctxwin.exact_facts(msgs)
    blob = "\n".join(facts)
    assert "Hunter2xyz99" not in blob and "sk-proj-" not in blob and "sk-live-" not in blob, facts
    assert not ctxwin._USER_FACT_RE.search("the password rule applies")


def test_a_stored_entry_with_a_secret_is_filtered_when_formatted():
    block = ctxwin.format_exact_facts(['user said: "token = " + "gh" + "p_abcdefghij1234567890"',
                                       'user said: "use port 5173"'])
    assert ("gh" + "p_") not in block and "5173" in block


# --------------------------------------------------------------------------- #
# peek: size-blind TTFT never clears a reasoning name on a big prompt
# --------------------------------------------------------------------------- #

def test_measured_fast_small_chats_do_not_shorten_a_big_reasoning_peek(monkeypatch):
    monkeypatch.setattr(A, "_ttft", {})
    for _ in range(6):
        A._record_ttft("deepseek", "deepseek-r1", 3000)
    assert not A._is_slow_model("deepseek", "deepseek-r1")   # small chats: fast
    assert A._stream_peek_timeout("deepseek-r1", 30000, pid="deepseek") \
        == A.STREAM_SLOW_BIG_PEEK_TIMEOUT
    # ...and a small request still uses the measurement.
    assert A._stream_peek_timeout("deepseek-r1", 100, pid="deepseek") \
        == A.STREAM_CONTENT_PEEK_TIMEOUT


# --------------------------------------------------------------------------- #
# Kimi MCP target
# --------------------------------------------------------------------------- #

def test_kimi_mcp_follows_kimi_code_home_even_before_its_dir_exists(monkeypatch):
    d = tempfile.mkdtemp(prefix="hub-pytest-kimimcp-")
    try:
        monkeypatch.delenv("MCP_MANAGER_HOME", raising=False)
        monkeypatch.setattr(m, "_home", lambda: d)
        os.makedirs(os.path.join(d, ".kimi"))
        with open(os.path.join(d, ".kimi", "config.toml"), "w", encoding="utf-8") as f:
            f.write('default_model = "x"\n')
        kc = os.path.join(d, "kc-home")                       # does not exist yet
        monkeypatch.setenv("KIMI_CODE_HOME", kc)
        assert os.path.normcase(m._config_path("kimi")) == \
            os.path.normcase(os.path.join(kc, "mcp.json"))
        monkeypatch.delenv("KIMI_CODE_HOME")
        assert m._config_path("kimi").endswith(os.path.join(".kimi", "config.toml"))
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------- #
# long nameless tool stream
# --------------------------------------------------------------------------- #

TOOLS = [{"type": "function", "function": {"name": "read_file", "parameters": {
    "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
         {"type": "function", "function": {"name": "open_file", "parameters": {
             "type": "object", "properties": {"path": {"type": "string"}},
             "required": ["path"]}}}]


def _nameless(n, args):
    head = [("data: " + json.dumps({"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": "c", "type": "function",
         "function": {"arguments": args}}]}}]})).encode()]
    filler = [("data: " + json.dumps({"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": ""}}]}}]})).encode()] * n
    return iter(head + filler)


def test_a_long_uninferable_nameless_stream_walks_to_the_next_hop():
    check = {"tools": TOOLS, "tools_offered": True}
    st, _ = A._peek_until_content(_nameless(500, '{"path": "a"}'), 10,
                                  max_lines=50, check=check)
    assert st == "empty"      # two tools fit {"path"}: ambiguous, never committed


def test_a_long_inferable_nameless_stream_is_still_committed():
    check = {"tools": TOOLS[:1], "tools_offered": True}
    st, _ = A._peek_until_content(_nameless(500, '{"path": "a"}'), 10,
                                  max_lines=50, check=check)
    assert st == "content"


# --------------------------------------------------------------------------- #
# dashboard
# --------------------------------------------------------------------------- #

def test_subscription_controls_render_from_the_post_and_re_enable():
    i = HTML.index("function initSubscriptions(")
    fn = HTML[i:HTML.index("\n  }\n", i)]
    assert "subscription_scope: el.value } }).then(function(r){\n          renderSubs(r || {});" in fn
    assert "m.disabled = false;\n        renderSubs(r || {});" in fn


def test_neutral_auth_pill_leaves_the_status_dot_denominator():
    assert "$('#pill-auth').classList.add('neutral'); $('#pill-auth').classList.remove('ok');" in HTML
    i = HTML.index("function syncHeaderStatusSummary(")
    fn = HTML[i:HTML.index("\n  }\n", i)]
    assert "classList.contains('neutral')) continue;" in fn
    assert "ok === counted" in fn


def test_a_visible_banner_drops_the_logo_burger_gutter():
    assert "#exhaust-banner.show ~ header .logo{margin-left:0}" in HTML
    assert "#exhaust-banner.show{min-height:calc(var(--tap) + 12px)}" in HTML
