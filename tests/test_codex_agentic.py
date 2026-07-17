"""Codex as an agentic backend (codex-cli 0.144.5): argv shape + JSONL parsing.

These lock in the live-verified invocation (see agentic_chat._build_argv_codex /
_parse_codex_json) so a future refactor can't silently regress Codex support back
to the disabled state.
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agentic_chat as ac


def _sess(native=None):
    return types.SimpleNamespace(cli_id="codex", native_session_id=native)


# Real event shapes captured from a live `codex exec --json` run (2026-07-17).
CODEX_EVENTS = "\n".join([
    "Reading additional input from stdin...",  # non-JSON noise on stdout -> skipped
    '{"type":"thread.started","thread_id":"019f71a6-8efb-7203-b372-628f5e7d5934"}',
    '{"type":"item.completed","item":{"id":"item_1","type":"error","message":"Model metadata for `auto` not found."}}',
    '{"type":"turn.started"}',
    '{"type":"item.started","item":{"id":"item_2","type":"command_execution","command":"echo hi"}}',
    '{"type":"item.completed","item":{"id":"item_2","type":"command_execution","command":"echo hi"}}',
    '{"type":"item.completed","item":{"id":"item_3","type":"agent_message","text":"Done: created hello.txt."}}',
    '{"type":"turn.completed","usage":{"input_tokens":10}}',
])


def test_parse_codex_json_extracts_reply_and_thread_id():
    text, native, detail = ac._parse_codex_json(CODEX_EVENTS, "", 0)
    assert text == "Done: created hello.txt."
    assert native == "019f71a6-8efb-7203-b372-628f5e7d5934"
    assert detail is None


def test_parse_codex_json_error_notice_not_fatal_when_message_present():
    # an error notice BEFORE a real agent_message must not mask the reply
    assert ac._parse_codex_json(CODEX_EVENTS, "", 0)[0] == "Done: created hello.txt."


def test_parse_codex_json_failure_when_no_agent_message():
    ev = ('{"type":"thread.started","thread_id":"abc"}\n'
          '{"type":"item.completed","item":{"type":"error","message":"boom"}}')
    text, native, detail = ac._parse_codex_json(ev, "stderr noise", 1)
    assert text is None
    assert native == "abc"
    assert "boom" in detail


def test_build_argv_codex_fresh(monkeypatch):
    monkeypatch.setattr(ac, "_system_prompt_addition", lambda: "")
    monkeypatch.setattr(ac, "_launcher", lambda b: [b])
    argv = ac._build_argv_codex(_sess(None), "codex", "make a file")
    assert argv == ["codex", "exec", "--json",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "--skip-git-repo-check", "make a file"]


def test_build_argv_codex_resume(monkeypatch):
    monkeypatch.setattr(ac, "_system_prompt_addition", lambda: "")
    monkeypatch.setattr(ac, "_launcher", lambda b: [b])
    argv = ac._build_argv_codex(_sess("TID-123"), "codex", "next step")
    assert argv == ["codex", "exec", "resume", "TID-123", "--json",
                    "--dangerously-bypass-approvals-and-sandbox", "next step"]
    # --skip-git-repo-check is NOT accepted by the `resume` subcommand
    assert "--skip-git-repo-check" not in argv


def test_build_argv_codex_prepends_system_prompt(monkeypatch):
    monkeypatch.setattr(ac, "_system_prompt_addition", lambda: "NOTE.")
    monkeypatch.setattr(ac, "_launcher", lambda b: [b])
    argv = ac._build_argv_codex(_sess(None), "codex", "do it")
    assert argv[-1] == "NOTE.\n\ndo it"  # codex has no --append-system-prompt


def test_codex_is_default_and_supported():
    assert ac.default_cli() == "codex"
    assert ac.cli_support()["codex"]["supported"] is True


# --- live-streaming event parsers ---------------------------------------------

def test_codex_stream_events_tool_message_and_noise():
    assert ac._codex_stream_events(
        '{"type":"item.started","item":{"type":"command_execution","command":"echo hi"}}'
    ) == [{"event": "tool", "text": "echo hi"}]
    msg = ac._codex_stream_events(
        '{"type":"item.completed","item":{"type":"agent_message","text":"All done."}}')
    assert {"event": "message", "text": "All done."} in msg
    assert {"_final": "All done."} in msg
    assert ac._codex_stream_events('{"type":"thread.started","thread_id":"T1"}') == [{"_native": "T1"}]
    assert ac._codex_stream_events("Reading additional input from stdin...") == []


def test_claude_stream_events_text_and_tool():
    a = ac._claude_stream_events(
        '{"type":"assistant","message":{"content":['
        '{"type":"text","text":"hello"},'
        '{"type":"tool_use","name":"Bash","input":{"command":"ls"}}]}}')
    assert {"event": "message", "text": "hello"} in a
    assert any(e.get("event") == "tool" and "ls" in e.get("text", "") for e in a)
    r = ac._claude_stream_events('{"type":"result","session_id":"S1","result":"final ans"}')
    assert {"_native": "S1"} in r
    assert {"_final": "final ans"} in r


def test_build_argv_claude_stream_uses_stream_json(monkeypatch):
    monkeypatch.setattr(ac, "_system_prompt_addition", lambda: "")
    monkeypatch.setattr(ac, "_launcher", lambda b: [b])
    s = types.SimpleNamespace(cli_id="claude", native_session_id=None)
    argv = ac._build_argv(s, "claude", "hi", stream=True)
    assert "stream-json" in argv and "--verbose" in argv
    argv2 = ac._build_argv(s, "claude", "hi", stream=False)
    assert "json" in argv2 and "stream-json" not in argv2 and "--verbose" not in argv2


def test_build_argv_codex_stream_matches_nonstream(monkeypatch):
    monkeypatch.setattr(ac, "_system_prompt_addition", lambda: "")
    monkeypatch.setattr(ac, "_launcher", lambda b: [b])
    s = types.SimpleNamespace(cli_id="codex", native_session_id=None)
    assert ac._build_argv(s, "codex", "hi", stream=True) == ac._build_argv(s, "codex", "hi", stream=False)
    assert "--json" in ac._build_argv(s, "codex", "hi", stream=True)
