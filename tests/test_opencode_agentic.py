"""Driving OpenCode as a third agent CLI, and telling a beginner what is missing.

Everything asserted here about opencode's behaviour was measured against
opencode-ai 1.18.11 on 2026-08-01, not read from docs:

  * `opencode run --format json` emits one JSON object per line:
    step_start / tool_use / text / step_finish, each carrying sessionID.
  * `--session <id>` continues that session. Verified end to end through the
    hub's own API: turn 1 wrote a file, turn 2 recalled the filename and its
    contents WITHOUT reading the file.
  * It must be spawned with stdin CLOSED. With an open pipe it logs "init" and
    then blocks forever -- 0 bytes after 200s, twice. The identical command
    with stdin at /dev/null answered in under a second.
"""
import json
import os
import shutil
import tempfile

import pytest

import agentic_chat
import workspace


class _Sess:
    """Minimal stand-in: _build_argv only reads these four fields."""
    def __init__(self, native=None, project_dir="."):
        self.cli_id = "opencode"
        self.project_dir = project_dir
        self.native_session_id = native
        self.turn_count = 0


# --------------------------------------------------------------------------- #
# Invocation
# --------------------------------------------------------------------------- #

def test_opencode_is_offered_as_a_drivable_cli():
    assert agentic_chat._SUPPORT["opencode"][0] is True
    assert agentic_chat._CLI_BIN["opencode"] == "opencode"


def test_first_turn_runs_the_prompt_positionally(monkeypatch):
    monkeypatch.setattr(agentic_chat, "_system_prompt_addition", lambda *a, **k: "")
    argv = agentic_chat._build_argv(_Sess(), "/bin/opencode", "build me a page")
    assert argv[-4:] == ["run", "--format", "json", "build me a page"]
    assert "--session" not in argv


def test_later_turns_continue_the_same_opencode_session(monkeypatch):
    monkeypatch.setattr(agentic_chat, "_system_prompt_addition", lambda *a, **k: "")
    argv = agentic_chat._build_argv(_Sess(native="ses_abc123"), "/bin/opencode", "and now the footer")
    assert "--session" in argv and argv[argv.index("--session") + 1] == "ses_abc123"
    assert argv[-1] == "and now the footer"


def test_the_task_comes_before_the_standing_notice(monkeypatch):
    """The codex bug, which cost several turns: leading with the notice made
    the agent answer the NOTICE instead of the user's request."""
    monkeypatch.setattr(agentic_chat, "_system_prompt_addition", lambda *a, **k: "VERIFY YOUR WORK")
    monkeypatch.setattr(agentic_chat, "write_task_brief", lambda d, t: False)
    prompt = agentic_chat._build_argv(_Sess(), "/bin/opencode", "restaurant site in Fez")[-1]
    assert prompt.index("restaurant site in Fez") < prompt.index("VERIFY YOUR WORK")


def test_the_notice_is_not_repeated_on_every_turn(monkeypatch):
    """Resent each turn it reads as the user repeating themselves -- observed
    verbatim from codex: "You've sent that instruction three times now"."""
    monkeypatch.setattr(agentic_chat, "_system_prompt_addition", lambda *a, **k: "VERIFY YOUR WORK")
    prompt = agentic_chat._build_argv(_Sess(native="ses_x"), "/bin/opencode", "next task")[-1]
    assert prompt == "next task"


def test_stdin_is_closed_for_every_cli():
    """opencode hangs FOREVER on an open stdin pipe -- measured twice at 0 bytes
    after 200s. This is the line that keeps that from happening."""
    src = open(agentic_chat.__file__, encoding="utf-8", errors="replace").read()
    assert src.count("stdin=subprocess.DEVNULL") >= 2, (
        "a spawn path without stdin=DEVNULL will hang opencode indefinitely")


# --------------------------------------------------------------------------- #
# Reading what it says back
# --------------------------------------------------------------------------- #

_REAL_LINES = [
    '{"type":"step_start","sessionID":"ses_04094b5","part":{"type":"step-start"}}',
    '{"type":"tool_use","sessionID":"ses_04094b5","part":{"type":"tool","tool":"write",'
    '"state":{"input":{"filePath":"index.html"}}}}',
    '{"type":"step_finish","sessionID":"ses_04094b5","part":{"type":"step-finish","reason":"stop"}}',
    '{"type":"text","sessionID":"ses_04094b5","part":{"type":"text","text":"DONE"}}',
]


def test_the_reply_and_session_id_come_out_of_a_real_transcript():
    text, sid, detail = agentic_chat._parse_opencode_json("\n".join(_REAL_LINES), "", 0)
    assert text == "DONE"
    assert sid == "ses_04094b5", "without this, turn 2 starts a fresh session"
    assert detail is None


def test_the_last_text_wins_not_the_first():
    """A turn that used tools emits several steps; the answer is the final one."""
    lines = _REAL_LINES + [
        '{"type":"text","sessionID":"ses_04094b5","part":{"type":"text","text":"actually, finished"}}']
    text, _, _ = agentic_chat._parse_opencode_json("\n".join(lines), "", 0)
    assert text == "actually, finished"


def test_an_auth_error_is_reported_not_swallowed():
    """The real shape, from a run with no provider key configured."""
    line = ('{"type":"error","sessionID":"ses_1","error":{"name":"ProviderAuthError",'
            '"data":{"providerID":"google","message":"API key is missing."}}}')
    text, sid, detail = agentic_chat._parse_opencode_json(line, "", 0)
    assert text is None
    assert "API key is missing" in detail
    assert sid == "ses_1"


def test_log_noise_on_stdout_does_not_break_parsing():
    noisy = "loading config...\n" + "\n".join(_REAL_LINES) + "\nbye\n"
    text, sid, _ = agentic_chat._parse_opencode_json(noisy, "", 0)
    assert text == "DONE" and sid == "ses_04094b5"


def test_a_silent_failure_still_reports_something():
    text, _, detail = agentic_chat._parse_opencode_json("", "boom", 1)
    assert text is None and detail


# --------------------------------------------------------------------------- #
# Streaming
# --------------------------------------------------------------------------- #

def test_tool_events_name_the_file_not_just_the_tool():
    """"write" alone says nothing; "write index.html" is the progress line."""
    evs = agentic_chat._opencode_stream_events(_REAL_LINES[1])
    tools = [e for e in evs if e.get("event") == "tool"]
    assert tools and tools[0]["text"] == "write index.html"


def test_the_session_id_is_captured_from_the_stream():
    evs = agentic_chat._opencode_stream_events(_REAL_LINES[0])
    assert any(e.get("_native") == "ses_04094b5" for e in evs)


def test_text_events_become_both_a_message_and_the_final_answer():
    evs = agentic_chat._opencode_stream_events(_REAL_LINES[3])
    assert any(e.get("event") == "message" and e["text"] == "DONE" for e in evs)
    assert any(e.get("_final") == "DONE" for e in evs)


def test_garbage_lines_are_ignored():
    for line in ("", "not json", "{", "[]", '{"type":"unknown"}'):
        assert isinstance(agentic_chat._opencode_stream_events(line), list)


def test_the_stream_parser_is_wired_for_opencode():
    src = open(agentic_chat.__file__, encoding="utf-8", errors="replace").read()
    assert '"opencode": _opencode_stream_events' in src
    assert '"opencode": _parse_opencode_json' in src


# --------------------------------------------------------------------------- #
# "You need Node / git" -- said in the chat, with the link
# --------------------------------------------------------------------------- #

@pytest.fixture
def proj():
    d = tempfile.mkdtemp(prefix="hubtools-")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_a_next_js_project_on_a_machine_without_node_says_so(proj, monkeypatch):
    """The failure without this is "[WinError 2] The system cannot find the file
    specified" -- true, and useless to someone who has never installed Node."""
    open(os.path.join(proj, "package.json"), "w").write(
        json.dumps({"dependencies": {"next": "15.0.0", "react": "19"}}))
    monkeypatch.setattr(workspace.shutil, "which", lambda n, **k: None)
    msg = workspace.missing_tools_message(proj)
    assert "Node.js" in msg
    assert "https://nodejs.org/en/download" in msg, "naming the tool without a link is half an answer"
    assert "Next.js" in msg, "name the framework the user asked for, not 'a JS project'"


def test_git_is_named_with_its_own_link(proj, monkeypatch):
    os.makedirs(os.path.join(proj, ".git"))
    monkeypatch.setattr(workspace.shutil, "which", lambda n, **k: None)
    msg = workspace.missing_tools_message(proj)
    assert "Git" in msg and "https://git-scm.com/downloads" in msg


def test_npm_is_not_listed_separately_from_node(proj, monkeypatch):
    """Same download page twice reads like two separate problems."""
    open(os.path.join(proj, "package.json"), "w").write('{"dependencies":{"vite":"5"}}')
    monkeypatch.setattr(workspace.shutil, "which", lambda n, **k: None)
    assert workspace.missing_tools_message(proj).count("nodejs.org") == 1


def test_nothing_is_said_when_nothing_is_missing(proj, monkeypatch):
    open(os.path.join(proj, "package.json"), "w").write('{"dependencies":{"next":"15"}}')
    monkeypatch.setattr(workspace.shutil, "which", lambda n, **k: "/usr/bin/" + str(n))
    assert workspace.missing_tools_message(proj) is None


def test_a_plain_html_folder_needs_nothing(proj, monkeypatch):
    open(os.path.join(proj, "index.html"), "w").write("<h1>hi</h1>")
    monkeypatch.setattr(workspace.shutil, "which", lambda n, **k: None)
    assert workspace.missing_tools(proj) == []


def test_the_notice_is_sent_once_per_session_not_once_per_turn():
    src = open(agentic_chat.__file__, encoding="utf-8", errors="replace").read()
    assert "tools_notified" in src
    assert "missing_tools_message" in src
