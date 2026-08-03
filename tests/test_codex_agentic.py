"""Codex as an agentic backend (codex-cli 0.144.5): argv shape + JSONL parsing.

These lock in the live-verified invocation (see agentic_chat._build_argv_codex /
_parse_codex_json) so a future refactor can't silently regress Codex support back
to the disabled state.
"""
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agentic_chat as ac


def _sess(native=None):
    # project_dir is required now: the craft brief is written INTO the project
    # (see agentic_chat.write_task_brief) rather than inlined into argv.
    import tempfile
    return types.SimpleNamespace(cli_id="codex", native_session_id=native,
                                 project_dir=tempfile.gettempdir())


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


# --------------------------------------------------------------------------- #
# The "Model metadata for `auto` not found" notice: routine on EVERY codex
# turn (model="auto" is the hub's own routing sentinel, not a real model
# name codex's local table knows), but reported live as the ONLY thing
# visible in the transcript for long stretches -- read as an alarming error
# by the user rather than the harmless noise it is. Suppressed at the
# source so it never reaches a "notice" event or shadows a real error.
# --------------------------------------------------------------------------- #

def test_the_real_captured_metadata_notice_is_recognised_as_benign():
    # item_1 in CODEX_EVENTS above -- captured live, 2026-07-17.
    assert ac._is_benign_codex_notice("Model metadata for `auto` not found.")


def test_the_longer_form_the_user_actually_saw_is_also_recognised():
    assert ac._is_benign_codex_notice(
        "Model metadata for `auto` not found. Defaulting to fallback "
        "metadata; this can degrade performance and cause issues.")


def test_an_unrelated_error_is_not_treated_as_benign():
    assert not ac._is_benign_codex_notice("boom")
    assert not ac._is_benign_codex_notice("unexpected status 401 Unauthorized")


def test_the_streaming_parser_drops_the_benign_notice_entirely():
    events = ac._codex_stream_events(
        '{"type":"item.completed","item":{"id":"item_1","type":"error",'
        '"message":"Model metadata for `auto` not found."}}')
    assert events == []


def test_the_streaming_parser_still_surfaces_a_real_error_notice():
    events = ac._codex_stream_events(
        '{"type":"item.completed","item":{"id":"item_9","type":"error",'
        '"message":"boom"}}')
    assert {"event": "notice", "text": "boom"} in events


def test_the_benign_notice_never_becomes_the_non_streaming_failure_detail():
    """A real failure with ONLY the benign notice as prior context must not
    have that notice masquerade as the reason -- it would say nothing useful
    about what actually went wrong."""
    ev = ('{"type":"thread.started","thread_id":"abc"}\n'
          '{"type":"item.completed","item":{"type":"error",'
          '"message":"Model metadata for `auto` not found."}}')
    text, native, detail = ac._parse_codex_json(ev, "", 1)
    assert text is None
    assert "Model metadata" not in (detail or "")


def test_parse_codex_json_extracts_reply_even_with_the_benign_notice_first():
    """CODEX_EVENTS' own item_1 IS this notice -- confirms the existing
    happy-path fixture still resolves correctly now that it is filtered."""
    text, native, detail = ac._parse_codex_json(CODEX_EVENTS, "", 0)
    assert text == "Done: created hello.txt."
    assert detail is None


def test_build_argv_codex_fresh(monkeypatch):
    monkeypatch.setattr(ac, "_system_prompt_addition", lambda *a, **k: "")
    monkeypatch.setattr(ac, "_launcher", lambda b: [b])
    argv = ac._build_argv_codex(_sess(None), "codex", "make a file")
    assert argv == ["codex", "exec", "--json",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "--skip-git-repo-check", "make a file"]


def test_build_argv_codex_resume(monkeypatch):
    monkeypatch.setattr(ac, "_system_prompt_addition", lambda *a, **k: "")
    monkeypatch.setattr(ac, "_launcher", lambda b: [b])
    argv = ac._build_argv_codex(_sess("TID-123"), "codex", "next step")
    assert argv == ["codex", "exec", "resume", "TID-123", "--json",
                    "--dangerously-bypass-approvals-and-sandbox", "next step"]
    # --skip-git-repo-check is NOT accepted by the `resume` subcommand
    assert "--skip-git-repo-check" not in argv


def test_build_argv_codex_puts_the_task_first(monkeypatch):
    """Codex has no --append-system-prompt, so the notice is inlined — but it
    must come AFTER the task.

    ASSERTION CORRECTED 2026-08-01. It used to require "NOTE.\\n\\ndo it", and
    that ordering broke the agent in practice: the user asked four times for a
    restaurant website and got back "That's noted as a standing instruction —
    I'll verify changes by actually running them... What would you like me to
    work on?". It was answering the NOTICE, because the notice was the opening
    line of every message and the real request read as trailing context."""
    monkeypatch.setattr(ac, "_system_prompt_addition", lambda *a, **k: "NOTE.")
    monkeypatch.setattr(ac, "_launcher", lambda b: [b])
    prompt = ac._build_argv_codex(_sess(None), "codex", "do it")[-1]
    assert prompt.startswith("do it"), "the user's task must lead the prompt"
    assert "NOTE." in prompt
    assert "Standing instruction" in prompt, "the notice must be marked ancillary"


def test_build_argv_codex_does_not_repeat_the_notice_on_later_turns(monkeypatch):
    """`resume` already carries the earlier turns, so re-sending it every turn is
    noise — and it read as the user repeating themselves. The agent said so:
    "You've sent that instruction three times now... I won't be acting on
    anything until you give me the actual task"."""
    monkeypatch.setattr(ac, "_system_prompt_addition", lambda *a, **k: "NOTE.")
    monkeypatch.setattr(ac, "_launcher", lambda b: [b])
    prompt = ac._build_argv_codex(_sess("thread-1"), "codex", "do it")[-1]
    assert prompt == "do it"


def test_codex_is_default_and_supported(monkeypatch):
    """The built-in default, i.e. what a fresh install starts on. A choice the
    user has saved in the dashboard wins over it -- see test_agent_cli_cards."""
    monkeypatch.setattr(ac.config, "get_value", lambda k, d=None: None)
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
    monkeypatch.setattr(ac, "_system_prompt_addition", lambda *a, **k: "")
    monkeypatch.setattr(ac, "_launcher", lambda b: [b])
    s = types.SimpleNamespace(cli_id="claude", native_session_id=None,
                              project_dir=tempfile.gettempdir())
    argv = ac._build_argv(s, "claude", "hi", stream=True)
    assert "stream-json" in argv and "--verbose" in argv
    argv2 = ac._build_argv(s, "claude", "hi", stream=False)
    assert "json" in argv2 and "stream-json" not in argv2 and "--verbose" not in argv2


def test_build_argv_codex_stream_matches_nonstream(monkeypatch):
    monkeypatch.setattr(ac, "_system_prompt_addition", lambda *a, **k: "")
    monkeypatch.setattr(ac, "_launcher", lambda b: [b])
    s = types.SimpleNamespace(cli_id="codex", native_session_id=None,
                              project_dir=tempfile.gettempdir())
    assert ac._build_argv(s, "codex", "hi", stream=True) == ac._build_argv(s, "codex", "hi", stream=False)
    assert "--json" in ac._build_argv(s, "codex", "hi", stream=True)


# --------------------------------------------------------------------------- #
# Isolated CLI copies.
#
# The hub can install a CLI into ~/.free-llm-hub/isolated-clis/<cli> with its own
# npm prefix and its own config dir, so driving it as an agent never disturbs the
# user's interactive setup. That mechanism already existed — the agent chat just
# was not using it: every session resolved the GLOBAL binary via shutil.which(),
# the same install the user types into by hand.
# --------------------------------------------------------------------------- #

def test_the_isolated_copy_wins_over_the_global_one(monkeypatch):
    monkeypatch.setattr(ac, "_isolated_bin", lambda cid: "/iso/" + cid)
    monkeypatch.setattr(ac.shutil, "which", lambda *a, **k: "/usr/bin/" + a[0])
    assert ac._resolve_bin("codex") == "/iso/codex"


def test_it_falls_back_to_the_users_own_install(monkeypatch):
    """Nobody should have to install anything twice for this to keep working."""
    monkeypatch.setattr(ac, "_isolated_bin", lambda cid: None)
    monkeypatch.setattr(ac.shutil, "which", lambda *a, **k: "/usr/bin/" + a[0])
    assert ac._resolve_bin("codex") == "/usr/bin/codex"


def test_missing_everywhere_reports_not_installed(monkeypatch):
    monkeypatch.setattr(ac, "_isolated_bin", lambda cid: None)
    monkeypatch.setattr(ac.shutil, "which", lambda *a, **k: None)
    assert ac._resolve_bin("codex") is None


def test_isolated_lookup_never_raises(monkeypatch):
    """A broken HOME or an unreadable dir must not take down session start."""
    def boom(*a, **k):
        raise OSError("nope")
    monkeypatch.setattr(ac.shutil, "which", boom)
    assert ac._isolated_bin("codex") is None


def test_isolated_path_matches_the_hubs_install_layout():
    """Must agree with app.py's _isolated_install_dir, or the agent chat would
    look in a folder the installer never writes to."""
    import app
    monkey = ac._isolated_bin("codex")
    expected_dir = app._isolated_install_dir("codex")
    if monkey:                      # only assert when a copy is actually present
        assert monkey.startswith(expected_dir)


# --------------------------------------------------------------------------- #
# Craft briefs reaching the AGENT CHAT.
#
# app.py injects them into requests that pass THROUGH the hub. An agent session
# never does — _agentic_env() strips every hub-pointing variable so the CLI
# cannot call back into us, and the hub log confirmed it: zero /v1/responses
# hits while a full session ran. So for the main way people build things here,
# the design/SEO/image/security rules were reaching nothing.
#
# Observed cost: asked for "a restaurant in Fez, Morocco", a session produced
# "Calvoun Store - Premium Products / Discover Premium Quality / Elevate your
# lifestyle", selling wireless headphones. "Discover", "Elevate" and the
# generic-template shape are all named in the WEB_DESIGN ANTI list.
# --------------------------------------------------------------------------- #

RESTAURANT = ("crerat ebst store website web deisng you can do pleae for restaurant "
              "in fez in morocco it shodul be very engaigng converison rate and many "
              "secitons ad shoudl have images of course")


def _sess_in(tmp, cli="codex", native=None):
    return types.SimpleNamespace(cli_id=cli, native_session_id=native, project_dir=tmp)


def test_the_brief_is_written_into_the_project(monkeypatch):
    d = tempfile.mkdtemp(prefix="hubbrief-")
    assert ac.write_task_brief(d, RESTAURANT) is True
    body = open(os.path.join(d, ac.BRIEF_FILENAME), encoding="utf-8").read()
    assert "WEB DESIGN BRIEF" in body
    assert "Elevate" in body, "the ANTI list must reach the agent"


def test_no_brief_file_for_a_task_that_needs_none():
    d = tempfile.mkdtemp(prefix="hubbrief-")
    assert ac.write_task_brief(d, "explain how a mutex works") is False
    assert not os.path.exists(os.path.join(d, ac.BRIEF_FILENAME))


def test_both_clis_point_at_the_brief(monkeypatch):
    """It has to work for every CLI, not just the default one."""
    monkeypatch.setattr(ac, "_launcher", lambda b: [b])
    d = tempfile.mkdtemp(prefix="hubbrief-")
    codex = ac._build_argv_codex(_sess_in(d), "codex", RESTAURANT)
    claude = ac._build_argv(_sess_in(d, "claude"), "claude", RESTAURANT)
    for name, argv in (("codex", codex), ("claude", claude)):
        joined = " ".join(argv)
        assert ac.BRIEF_FILENAME in joined, name
        assert "restate in ONE line" in joined, name


def test_the_command_line_stays_under_the_windows_ceiling(monkeypatch):
    """The briefs are ~9,000 chars. Inlining them would have taken the worst
    case to roughly 15,700 against cmd.exe's ~8191 limit and broken every turn
    on Windows — which is why they go in a FILE and the prompt just points."""
    monkeypatch.setattr(ac, "_launcher", lambda b: [b])
    d = tempfile.mkdtemp(prefix="hubbrief-")
    for build, cli in ((ac._build_argv_codex, "codex"),):
        argv = build(_sess_in(d, cli), cli, RESTAURANT)
        assert sum(len(a) for a in argv) < 8191, cli
    argv = ac._build_argv(_sess_in(d, "claude"), "claude", RESTAURANT)
    assert sum(len(a) for a in argv) < 8191


def test_the_brief_is_not_resent_on_later_turns(monkeypatch):
    """`resume` already carries the earlier turns."""
    monkeypatch.setattr(ac, "_launcher", lambda b: [b])
    d = tempfile.mkdtemp(prefix="hubbrief-")
    prompt = ac._build_argv_codex(_sess_in(d, native="T1"), "codex", RESTAURANT)[-1]
    assert prompt == RESTAURANT
    assert not os.path.exists(os.path.join(d, ac.BRIEF_FILENAME))


def test_the_agent_is_told_to_restate_the_subject(monkeypatch):
    """The request mixed "store website" with "restaurant in fez" and the model
    resolved that silently and wrongly. One restated line surfaces it in
    seconds instead of after a full build."""
    add = ac._system_prompt_addition(RESTAURANT)
    assert "restate in ONE line" in add
    assert "Never substitute a generic template" in add


def test_a_brief_failure_never_costs_the_user_their_turn(monkeypatch):
    """Standards are a bonus; a read-only folder must not break the session."""
    monkeypatch.setattr(ac.craft, "system_message",
                        lambda t: (_ for _ in ()).throw(RuntimeError("boom")))
    assert ac.write_task_brief(tempfile.mkdtemp(), RESTAURANT) is False
