"""Tests for the NEW agentic-chat feature (agentic_chat.py + the /api/agent/*
routes in app.py). Never spawns a real CLI subprocess -- every subprocess.Popen
boundary is monkeypatched with a fake process object.

This is ADDITIVE to _SUB_PROVIDERS/_sub_run/_subscription_chat; none of those
are touched or exercised here.
"""
import json
import os
import threading
import time

import pytest

import agentic_chat
import agentic_history
import app
import config
import vision_status


class _FakeVersionCheck:
    """Stand-in for subprocess.run()'s CompletedProcess. subprocess.run() is
    used for two unrelated things in agentic_chat.py: (1) _signal_tree()'s
    taskkill call (return value ignored entirely -- a fake object here is as
    harmless as the old `None` stub), and (2) _verify_claude_binary_identity()'s
    `--version` probe, where .stdout IS inspected for "Claude Code". Default to
    a PASSING fake version response so every pre-existing test in this file --
    none of which know about the binary-identity check -- keeps working
    unchanged; tests that specifically exercise the identity check override
    this per-test via monkeypatch."""

    def __init__(self, stdout="2.1.212 (Claude Code)", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@pytest.fixture
def agent_config(tmp_path, monkeypatch):
    path = tmp_path / "state" / "config.json"
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(path))
    # Reset the in-memory session registry AND recent-projects list between
    # tests -- both are module-level global state, deliberately not persisted
    # to disk.
    agentic_chat._REGISTRY.clear()
    agentic_chat._recent_projects.clear()
    # _signal_tree() (the process-TREE kill, on top of proc.terminate()/kill())
    # shells out to real `taskkill`/os.killpg against proc.pid. FakeProc's pid
    # is a bogus placeholder (not a real child of this test process), so never
    # let that reach a real OS call -- neutralize it here, once, for every test.
    # See _FakeVersionCheck above for why this returns a passing fake object
    # rather than None.
    monkeypatch.setattr(agentic_chat.subprocess, "run", lambda *a, **kw: _FakeVersionCheck())
    monkeypatch.setattr(agentic_chat.os, "killpg", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(agentic_chat.os, "getpgid", lambda pid: pid, raising=False)
    yield tmp_path
    agentic_chat._REGISTRY.clear()
    agentic_chat._recent_projects.clear()


def _enable():
    config.set_flag("agentic_chat_enabled", True)


class FakeProc:
    """Minimal stand-in for subprocess.Popen -- covers every attribute/method
    agentic_chat.py touches: .pid, .communicate(), .poll(), .wait(), .returncode.

    `hang=True` simulates a still-running process: communicate()/wait() block
    on a real threading.Event (like the real Popen blocks on the OS) until
    terminate()/kill() is called (or the event is set directly), or the given
    timeout elapses -- NOT an immediate raise, so tests that terminate it from
    another thread exercise real interleaving instead of a race against a
    synchronous stub."""

    def __init__(self, stdout="", stderr="", returncode=0, hang=False):
        self.pid = 4242
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._terminated = False
        self._killed = False
        self._done = threading.Event()
        if not hang:
            self._done.set()

    def communicate(self, timeout=None):
        if not self._done.wait(timeout=timeout):
            import subprocess as sp
            raise sp.TimeoutExpired(cmd="fake", timeout=timeout)
        return self._stdout, self._stderr

    def poll(self):
        return self.returncode if self._done.is_set() else None

    def terminate(self):
        self._terminated = True
        self._done.set()

    def kill(self):
        self._terminated = True
        self._killed = True
        self._done.set()

    def wait(self, timeout=None):
        if not self._done.wait(timeout=timeout):
            import subprocess as sp
            raise sp.TimeoutExpired(cmd="fake", timeout=timeout)
        return self.returncode


def _claude_json(result="Done.", session_id="native-abc", is_error=False):
    return json.dumps({"result": result, "session_id": session_id, "is_error": is_error})


# --------------------------------------------------------------------------- #
# start_session -- validation
# --------------------------------------------------------------------------- #

def test_start_session_rejects_when_master_flag_off(agent_config):
    with pytest.raises(agentic_chat.AgenticError) as exc:
        agentic_chat.start_session("claude", str(agent_config))
    assert exc.value.status == 403


def test_start_session_rejects_unknown_cli(agent_config, monkeypatch):
    _enable()
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda name: "/usr/bin/" + name)
    with pytest.raises(agentic_chat.AgenticError) as exc:
        agentic_chat.start_session("gpt4all-cli", str(agent_config))
    assert exc.value.status == 400


def test_start_session_rejects_missing_project_dir(agent_config, monkeypatch):
    _enable()
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda name: "/usr/bin/" + name)
    with pytest.raises(agentic_chat.AgenticError) as exc:
        agentic_chat.start_session("claude", str(agent_config / "does-not-exist"))
    assert "does not exist" in str(exc.value)


def test_start_session_rejects_empty_project_dir(agent_config, monkeypatch):
    _enable()
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda name: "/usr/bin/" + name)
    with pytest.raises(agentic_chat.AgenticError):
        agentic_chat.start_session("claude", "")


def test_start_session_rejects_file_as_project_dir(agent_config, monkeypatch):
    _enable()
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda name: "/usr/bin/" + name)
    f = agent_config / "somefile.txt"
    f.write_text("hi", encoding="utf-8")
    with pytest.raises(agentic_chat.AgenticError):
        agentic_chat.start_session("claude", str(f))


def test_start_session_rejects_when_binary_not_installed(agent_config, monkeypatch):
    _enable()
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda name: None)
    with pytest.raises(agentic_chat.AgenticError) as exc:
        agentic_chat.start_session("claude", str(agent_config))
    assert "not installed" in str(exc.value)


def test_start_session_codex_now_supported(agent_config, monkeypatch):
    """Codex agentic mode was live-verified on codex-cli 0.144.5 (resume+bypass
    confirmed) and is now the default/supported backend -- start_session() must
    register a codex session instead of refusing it."""
    _enable()
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda name: "/usr/bin/" + name)
    sid = agentic_chat.start_session("codex", str(agent_config))
    assert agentic_chat.get_session(sid)["cli"] == "codex"
    assert agentic_chat.cli_support()["codex"]["supported"] is True
    assert agentic_chat.cli_support()["claude"]["supported"] is True


def test_start_session_success_registers_session(agent_config, monkeypatch):
    _enable()
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda name: "/usr/bin/" + name)
    sid = agentic_chat.start_session("claude", str(agent_config))
    assert sid in agentic_chat._REGISTRY
    row = agentic_chat.get_session(sid)
    assert row["cli"] == "claude"
    assert row["turn_count"] == 0
    assert row["currently_running"] is False
    assert row["has_native_session"] is False


# --------------------------------------------------------------------------- #
# send_message -- turn 1 vs turn 2 argv differences
# --------------------------------------------------------------------------- #

def _start(agent_config, monkeypatch):
    _enable()
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda name: "/usr/bin/" + name)
    return agentic_chat.start_session("claude", str(agent_config))


def test_send_message_turn1_has_no_resume_flag(agent_config, monkeypatch):
    sid = _start(agent_config, monkeypatch)
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["cwd"] = kwargs.get("cwd")
        return FakeProc(stdout=_claude_json())

    monkeypatch.setattr(agentic_chat.subprocess, "Popen", fake_popen)
    status, text, detail = agentic_chat.send_message(sid, "hello")
    assert status == 200
    assert text == "Done."
    assert "--resume" not in captured["argv"]
    assert "--dangerously-skip-permissions" in captured["argv"]
    assert captured["cwd"] == str(agent_config)
    # prompt travels as a positional argv arg, immediately after -p
    assert captured["argv"][captured["argv"].index("-p") + 1] == "hello"
    row = agentic_chat.get_session(sid)
    assert row["turn_count"] == 1
    assert row["has_native_session"] is True


def test_send_message_turn2_includes_resume_with_native_id(agent_config, monkeypatch):
    sid = _start(agent_config, monkeypatch)
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append(argv)
        return FakeProc(stdout=_claude_json(result="turn %d" % len(calls),
                                            session_id="native-abc"))

    monkeypatch.setattr(agentic_chat.subprocess, "Popen", fake_popen)
    agentic_chat.send_message(sid, "first")
    status, text, detail = agentic_chat.send_message(sid, "second")
    assert status == 200
    second_argv = calls[1]
    assert "--resume" in second_argv
    assert second_argv[second_argv.index("--resume") + 1] == "native-abc"
    # Full-tool-access flag must be present on EVERY turn, not just turn 1.
    assert "--dangerously-skip-permissions" in second_argv


def test_send_message_unknown_session(agent_config):
    _enable()
    status, text, detail = agentic_chat.send_message("nope", "hi")
    assert status == 404
    assert text is None


def test_send_message_master_flag_off(agent_config, monkeypatch):
    sid = _start(agent_config, monkeypatch)
    config.set_flag("agentic_chat_enabled", False)
    status, text, detail = agentic_chat.send_message(sid, "hi")
    assert status == 403


def test_send_message_rejects_empty_text(agent_config, monkeypatch):
    sid = _start(agent_config, monkeypatch)
    status, text, detail = agentic_chat.send_message(sid, "   ")
    assert status == 400


def test_send_message_rejects_oversized_text(agent_config, monkeypatch):
    sid = _start(agent_config, monkeypatch)
    status, text, detail = agentic_chat.send_message(sid, "x" * (agentic_chat._MAX_MESSAGE_CHARS + 1))
    assert status == 400
    assert "capped" in detail


def test_send_message_busy_when_turn_already_running(agent_config, monkeypatch):
    sid = _start(agent_config, monkeypatch)
    sess = agentic_chat._REGISTRY[sid]
    assert sess.turn_lock.acquire(blocking=False)  # simulate an in-flight turn
    try:
        status, text, detail = agentic_chat.send_message(sid, "hi")
        assert status == 409
    finally:
        sess.turn_lock.release()


def test_send_message_nonzero_exit_no_output_is_502(agent_config, monkeypatch):
    sid = _start(agent_config, monkeypatch)
    monkeypatch.setattr(agentic_chat.subprocess, "Popen",
                        lambda argv, **kw: FakeProc(stdout="", stderr="boom", returncode=1))
    status, text, detail = agentic_chat.send_message(sid, "hi")
    assert status == 502
    assert text is None


def test_send_message_is_error_json_surfaces_message(agent_config, monkeypatch):
    sid = _start(agent_config, monkeypatch)
    payload = json.dumps({"result": "auth required", "session_id": "x", "is_error": True})
    monkeypatch.setattr(agentic_chat.subprocess, "Popen",
                        lambda argv, **kw: FakeProc(stdout=payload, returncode=1))
    status, text, detail = agentic_chat.send_message(sid, "hi")
    assert status == 502
    assert "auth required" in detail


def test_send_message_auth_error_maps_to_403_not_502(agent_config, monkeypatch):
    """If the subscription session itself expired mid-agentic-session, the
    caller should see 403 ('sign back in'), not a generic 502 ('it failed')."""
    sid = _start(agent_config, monkeypatch)
    monkeypatch.setattr(
        agentic_chat.subprocess, "Popen",
        lambda argv, **kw: FakeProc(stdout="", stderr="Not authenticated. Please run /login",
                                    returncode=1))
    status, text, detail = agentic_chat.send_message(sid, "hi")
    assert status == 403
    assert text is None
    assert "please run /login" in detail.lower()


def test_build_argv_message_at_cap_stays_under_windows_cmdline_limit(agent_config):
    """_MAX_MESSAGE_CHARS is sized so a full-length message, plus the resume
    flag/session-id/other flags cmd.exe /c wraps around an npm .cmd shim, stays
    safely under cmd.exe's ~8191-char command-line ceiling (see _sub_run's own
    '~8k chars' comment in app.py for the same constraint on the same launch
    path)."""
    sess = agentic_chat._Session("claude", str(agent_config))
    sess.native_session_id = "01234567-89ab-cdef-0123-456789abcdef"
    long_bin = r"C:\Users\somewhat-long-username\AppData\Roaming\npm\claude.cmd"
    text = "x" * agentic_chat._MAX_MESSAGE_CHARS
    argv = agentic_chat._build_argv(sess, long_bin, text)
    # Worst-case list2cmdline overhead: each arg's own length + a separating
    # space + 2 quote characters (none of our args need escaped internal
    # quotes/backslashes, so this is a safe upper bound, not just an estimate).
    total = sum(len(a) + 3 for a in argv)
    assert total < 8191


def test_send_message_timeout(agent_config, monkeypatch):
    sid = _start(agent_config, monkeypatch)
    monkeypatch.setattr(agentic_chat, "_TURN_TIMEOUT", 0.05)
    monkeypatch.setattr(agentic_chat, "_terminate", lambda proc: None)
    monkeypatch.setattr(agentic_chat.subprocess, "Popen",
                        lambda argv, **kw: FakeProc(hang=True))
    status, text, detail = agentic_chat.send_message(sid, "hi")
    assert status == 504


def test_send_message_secret_scrubbed_from_error(agent_config, monkeypatch):
    cfg = config.load_config(strict=True)
    cfg.setdefault("providers", {})["groq"] = {"api_keys": ["sk-supersecret123"], "enabled": True}
    config.save_config(cfg)
    sid = _start(agent_config, monkeypatch)
    monkeypatch.setattr(agentic_chat.subprocess, "Popen",
                        lambda argv, **kw: FakeProc(stdout="", stderr="key sk-supersecret123 invalid",
                                                    returncode=1))
    status, text, detail = agentic_chat.send_message(sid, "hi")
    assert "sk-supersecret123" not in (detail or "")
    assert "***" in (detail or "")


# --------------------------------------------------------------------------- #
# stop_session / end_session
# --------------------------------------------------------------------------- #

def test_stop_session_terminates_running_process(agent_config, monkeypatch):
    sid = _start(agent_config, monkeypatch)
    proc = FakeProc(hang=True)
    sess = agentic_chat._REGISTRY[sid]
    sess.proc = proc
    stopped = agentic_chat.stop_session(sid)
    assert stopped is True
    assert proc._terminated is True
    assert sess.last_interrupted is True


def test_stop_session_escalates_to_kill_when_terminate_is_ignored(agent_config, monkeypatch):
    """Simulate an unresponsive process: terminate() doesn't actually stop it,
    so _terminate() must escalate to kill() after the grace period."""
    sid = _start(agent_config, monkeypatch)

    class UnresponsiveProc(FakeProc):
        def terminate(self):
            pass   # ignored -- simulates a process that doesn't honor SIGTERM

    monkeypatch.setattr(agentic_chat, "_KILL_GRACE", 0.05)
    proc = UnresponsiveProc(hang=True)
    agentic_chat._REGISTRY[sid].proc = proc
    assert agentic_chat.stop_session(sid) is True
    assert proc._killed is True


def test_stop_session_noop_when_nothing_running(agent_config, monkeypatch):
    sid = _start(agent_config, monkeypatch)
    stopped = agentic_chat.stop_session(sid)
    assert stopped is False


def test_stop_session_noop_for_unknown_session(agent_config):
    assert agentic_chat.stop_session("does-not-exist") is False


def test_stop_session_works_even_when_master_flag_off(agent_config, monkeypatch):
    """A kill switch must still be able to kill: stop_session() is deliberately
    NOT gated by the master flag."""
    sid = _start(agent_config, monkeypatch)
    proc = FakeProc(hang=True)
    agentic_chat._REGISTRY[sid].proc = proc
    config.set_flag("agentic_chat_enabled", False)
    assert agentic_chat.stop_session(sid) is True


def test_send_message_after_stop_reports_interrupted(agent_config, monkeypatch):
    sid = _start(agent_config, monkeypatch)
    monkeypatch.setattr(agentic_chat.subprocess, "Popen", lambda argv, **kw: FakeProc(hang=True))

    result = {}

    def run_turn():
        result["r"] = agentic_chat.send_message(sid, "hi")

    t = threading.Thread(target=run_turn)
    t.start()
    # Wait until the turn has actually registered as running (sess.proc is set
    # and still alive), then stop it -- FakeProc's communicate() genuinely
    # blocks on an Event until terminate()/kill() fires, so this exercises
    # real cross-thread interleaving rather than racing a synchronous stub.
    for _ in range(500):
        row = agentic_chat.get_session(sid)
        if row and row["currently_running"]:
            break
        time.sleep(0.01)
    else:
        pytest.fail("turn never became visible as currently_running")
    agentic_chat.stop_session(sid)
    t.join(timeout=5)
    assert not t.is_alive()
    assert result["r"][0] == 499


def test_end_session_removes_from_registry(agent_config, monkeypatch):
    sid = _start(agent_config, monkeypatch)
    assert agentic_chat.end_session(sid) is True
    assert sid not in agentic_chat._REGISTRY
    assert agentic_chat.get_session(sid) is None


def test_end_session_false_for_unknown(agent_config):
    assert agentic_chat.end_session("nope") is False


def test_end_session_stops_running_process_first(agent_config, monkeypatch):
    sid = _start(agent_config, monkeypatch)
    proc = FakeProc(hang=True)
    agentic_chat._REGISTRY[sid].proc = proc
    assert agentic_chat.end_session(sid) is True
    assert proc._terminated is True


def test_list_sessions(agent_config, monkeypatch):
    sid1 = _start(agent_config, monkeypatch)
    sid2 = _start(agent_config, monkeypatch)
    ids = {row["session_id"] for row in agentic_chat.list_sessions()}
    assert ids == {sid1, sid2}


# --------------------------------------------------------------------------- #
# _agentic_env -- hub-pointing vars stripped
# --------------------------------------------------------------------------- #

def test_agentic_env_strips_hub_pointing_vars(agent_config, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:%d/v1" % agentic_chat._port())
    env = agentic_chat._agentic_env()
    assert "ANTHROPIC_BASE_URL" not in env


def test_agentic_env_passes_through_other_vars(agent_config, monkeypatch):
    monkeypatch.setenv("SOME_OTHER_VAR", "keep-me")
    env = agentic_chat._agentic_env()
    assert env.get("SOME_OTHER_VAR") == "keep-me"


# --------------------------------------------------------------------------- #
# Flask routes -- master-flag-off 403 path on every new route (except settings,
# and except stop/end which must survive the kill switch being off)
# --------------------------------------------------------------------------- #

_DASH = {"X-Free-LLM-Hub": "dashboard"}


def test_api_agent_settings_get_default_off(agent_config):
    client = app.app.test_client()
    resp = client.get("/api/agent/settings")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["enabled"] is False
    assert body["clis"]["codex"]["supported"] is True
    assert body["clis"]["claude"]["supported"] is True


def test_api_agent_settings_post_toggles_flag(agent_config):
    client = app.app.test_client()
    resp = client.post("/api/agent/settings", json={"enabled": True}, headers=_DASH)
    assert resp.status_code == 200
    assert resp.get_json()["enabled"] is True
    assert config.get_flag("agentic_chat_enabled", False) is True


def test_api_agent_settings_post_requires_bool(agent_config):
    client = app.app.test_client()
    resp = client.post("/api/agent/settings", json={"enabled": "yes"}, headers=_DASH)
    assert resp.status_code == 400


def test_api_agent_start_session_403_when_disabled(agent_config):
    client = app.app.test_client()
    resp = client.post("/api/agent/sessions",
                       json={"cli": "claude", "project_dir": str(agent_config)},
                       headers=_DASH)
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "agentic_chat_disabled"


def test_api_agent_list_sessions_403_when_disabled(agent_config):
    client = app.app.test_client()
    resp = client.get("/api/agent/sessions")
    assert resp.status_code == 403


def test_api_agent_get_session_403_when_disabled(agent_config):
    client = app.app.test_client()
    resp = client.get("/api/agent/sessions/whatever")
    assert resp.status_code == 403


def test_api_agent_send_message_403_when_disabled(agent_config):
    client = app.app.test_client()
    resp = client.post("/api/agent/sessions/whatever/message", json={"text": "hi"}, headers=_DASH)
    assert resp.status_code == 403


def test_api_agent_stop_session_works_when_disabled(agent_config, monkeypatch):
    """stop is NOT gated by the master flag -- a kill switch must still kill."""
    sid = _start(agent_config, monkeypatch)
    config.set_flag("agentic_chat_enabled", False)
    client = app.app.test_client()
    resp = client.post("/api/agent/sessions/%s/stop" % sid, headers=_DASH)
    assert resp.status_code == 200
    assert resp.get_json()["stopped"] is False   # nothing was running, but no 403 either


def test_api_agent_end_session_works_when_disabled(agent_config, monkeypatch):
    sid = _start(agent_config, monkeypatch)
    config.set_flag("agentic_chat_enabled", False)
    client = app.app.test_client()
    resp = client.delete("/api/agent/sessions/%s" % sid, headers=_DASH)
    assert resp.status_code == 200
    assert resp.get_json()["ended"] is True


def test_api_agent_start_session_requires_dashboard_header(agent_config):
    _enable()
    client = app.app.test_client()
    resp = client.post("/api/agent/sessions", json={"cli": "claude", "project_dir": str(agent_config)})
    assert resp.status_code == 403   # local-control-guard header check, before the agent gate


def test_api_agent_full_flow_via_http(agent_config, monkeypatch):
    _enable()
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(agentic_chat.subprocess, "Popen",
                        lambda argv, **kw: FakeProc(stdout=_claude_json(result="hi there")))
    client = app.app.test_client()

    resp = client.post("/api/agent/sessions",
                       json={"cli": "claude", "project_dir": str(agent_config)}, headers=_DASH)
    assert resp.status_code == 200
    sid = resp.get_json()["session_id"]

    resp = client.post("/api/agent/sessions/%s/message" % sid, json={"text": "hello"}, headers=_DASH)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == 200
    assert body["text"] == "hi there"

    resp = client.get("/api/agent/sessions/%s" % sid)
    assert resp.status_code == 200
    assert resp.get_json()["turn_count"] == 1

    resp = client.delete("/api/agent/sessions/%s" % sid, headers=_DASH)
    assert resp.status_code == 200
    assert resp.get_json()["ended"] is True


def test_api_agent_start_session_bad_cli_400(agent_config):
    _enable()
    client = app.app.test_client()
    resp = client.post("/api/agent/sessions",
                       json={"cli": "notacli", "project_dir": str(agent_config)}, headers=_DASH)
    assert resp.status_code == 400
    assert "cli must be" in resp.get_json()["error"]


# --------------------------------------------------------------------------- #
# Default CLI -- Codex is the user-chosen default wherever a default is offered
# (API default when `cli` is omitted, and the value the frontend picker
# preselects). Claude Code remains supported and selectable.
# --------------------------------------------------------------------------- #

def test_default_cli_helper_returns_codex():
    assert agentic_chat.default_cli() == "codex"


def test_start_session_defaults_cli_to_codex_when_omitted(agent_config, monkeypatch):
    _enable()
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda name: "/usr/bin/" + name)
    sid = agentic_chat.start_session(None, str(agent_config))
    assert agentic_chat.get_session(sid)["cli"] == "codex"


def test_start_session_defaults_cli_for_empty_string(agent_config, monkeypatch):
    _enable()
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda name: "/usr/bin/" + name)
    sid = agentic_chat.start_session("", str(agent_config))
    assert agentic_chat.get_session(sid)["cli"] == "codex"


def test_api_agent_settings_get_includes_default_cli(agent_config):
    client = app.app.test_client()
    resp = client.get("/api/agent/settings")
    assert resp.get_json()["default_cli"] == "codex"


def test_api_agent_start_session_omits_cli_defaults_to_codex(agent_config, monkeypatch):
    _enable()
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda name: "/usr/bin/" + name)
    client = app.app.test_client()
    resp = client.post("/api/agent/sessions", json={"project_dir": str(agent_config)}, headers=_DASH)
    assert resp.status_code == 200
    assert resp.get_json()["cli"] == "codex"


# --------------------------------------------------------------------------- #
# cli_support() -- "installed" flag, for the dashboard picker to offer Install
# proactively (independent of the start_session() distinct-error path below).
# --------------------------------------------------------------------------- #

def test_cli_support_includes_installed_flag(monkeypatch):
    monkeypatch.setattr(agentic_chat.shutil, "which",
                        lambda name: "/usr/bin/" + name if name == "claude" else None)
    support = agentic_chat.cli_support()
    assert support["claude"]["installed"] is True
    assert support["codex"]["installed"] is False


# --------------------------------------------------------------------------- #
# Auto-detect + one-click install -- start_session() returns a DISTINCT error
# shape (code="cli_not_installed" + install_provider) instead of a generic 400,
# for BOTH claude and codex, so the frontend can offer an Install button that
# calls the EXISTING /api/subscriptions/<pid>/install-isolated route.
# --------------------------------------------------------------------------- #

def test_agentic_error_carries_code_and_extra():
    exc = agentic_chat.AgenticError("boom", 400, code="cli_not_installed",
                                    install_provider="sub-claude")
    assert exc.status == 400
    assert exc.code == "cli_not_installed"
    assert exc.extra == {"install_provider": "sub-claude"}


def test_start_session_not_installed_gives_distinct_code_and_provider(agent_config, monkeypatch):
    _enable()
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda name: None)
    with pytest.raises(agentic_chat.AgenticError) as exc:
        agentic_chat.start_session("claude", str(agent_config))
    assert exc.value.code == "cli_not_installed"
    assert exc.value.extra == {"install_provider": "sub-claude"}


def test_start_session_codex_not_installed_gives_sub_codex_provider(agent_config, monkeypatch):
    """Applies to BOTH clis: an uninstalled codex must surface as installable,
    not get masked by the "not currently supported" message -- the installed
    check runs BEFORE the supported-mode check."""
    _enable()
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda name: None)
    with pytest.raises(agentic_chat.AgenticError) as exc:
        agentic_chat.start_session("codex", str(agent_config))
    assert exc.value.code == "cli_not_installed"
    assert exc.value.extra == {"install_provider": "sub-codex"}


def test_start_session_codex_installed_creates_session(agent_config, monkeypatch):
    """Once installed, codex (now a supported backend) registers a session."""
    _enable()
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda name: "/usr/bin/" + name)
    sid = agentic_chat.start_session("codex", str(agent_config))
    assert agentic_chat.get_session(sid)["cli"] == "codex"


def test_api_agent_start_session_not_installed_error_shape(agent_config, monkeypatch):
    _enable()
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda name: None)
    client = app.app.test_client()
    resp = client.post("/api/agent/sessions",
                       json={"cli": "claude", "project_dir": str(agent_config)}, headers=_DASH)
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["code"] == "cli_not_installed"
    assert body["install_provider"] == "sub-claude"


# --------------------------------------------------------------------------- #
# Workspace: create_new folder-creation validation
# --------------------------------------------------------------------------- #

def test_start_session_create_new_creates_missing_folder(agent_config, monkeypatch):
    _enable()
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda name: "/usr/bin/" + name)
    new_dir = agent_config / "brand-new-project"
    assert not new_dir.exists()
    sid = agentic_chat.start_session("claude", str(new_dir), create_new=True)
    assert new_dir.is_dir()
    assert sid in agentic_chat._REGISTRY


def test_start_session_create_new_rejects_existing_nonempty_dir(agent_config, monkeypatch):
    _enable()
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda name: "/usr/bin/" + name)
    existing = agent_config / "already-has-stuff"
    existing.mkdir()
    (existing / "file.txt").write_text("hi", encoding="utf-8")
    with pytest.raises(agentic_chat.AgenticError) as exc:
        agentic_chat.start_session("claude", str(existing), create_new=True)
    assert "not empty" in str(exc.value)


def test_start_session_create_new_allows_existing_empty_dir(agent_config, monkeypatch):
    _enable()
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda name: "/usr/bin/" + name)
    empty = agent_config / "already-empty"
    empty.mkdir()
    sid = agentic_chat.start_session("claude", str(empty), create_new=True)
    assert sid in agentic_chat._REGISTRY


def test_start_session_create_new_rejects_file_at_path(agent_config, monkeypatch):
    _enable()
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda name: "/usr/bin/" + name)
    f = agent_config / "a-file.txt"
    f.write_text("hi", encoding="utf-8")
    with pytest.raises(agentic_chat.AgenticError) as exc:
        agentic_chat.start_session("claude", str(f), create_new=True)
    assert "not a directory" in str(exc.value)


def test_start_session_create_new_rejects_missing_parent(agent_config, monkeypatch):
    _enable()
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda name: "/usr/bin/" + name)
    deep = agent_config / "does-not-exist-parent" / "child"
    with pytest.raises(agentic_chat.AgenticError) as exc:
        agentic_chat.start_session("claude", str(deep), create_new=True)
    assert "parent directory" in str(exc.value)


def test_start_session_create_new_false_still_requires_existing_dir(agent_config, monkeypatch):
    """Regression: create_new defaults to False -- prior behavior unchanged."""
    _enable()
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda name: "/usr/bin/" + name)
    with pytest.raises(agentic_chat.AgenticError) as exc:
        agentic_chat.start_session("claude", str(agent_config / "nope"))
    assert "does not exist" in str(exc.value)


def test_api_agent_start_session_create_new_via_http(agent_config, monkeypatch):
    _enable()
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda name: "/usr/bin/" + name)
    client = app.app.test_client()
    new_dir = agent_config / "http-new-project"
    resp = client.post("/api/agent/sessions",
                       json={"cli": "claude", "project_dir": str(new_dir), "create_new": True},
                       headers=_DASH)
    assert resp.status_code == 200
    assert new_dir.is_dir()


def test_api_agent_start_session_create_new_must_be_bool(agent_config):
    _enable()
    client = app.app.test_client()
    resp = client.post("/api/agent/sessions",
                       json={"cli": "claude", "project_dir": str(agent_config), "create_new": "yes"},
                       headers=_DASH)
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# Workspace: get_recent_projects()
# --------------------------------------------------------------------------- #

def test_get_recent_projects_tracks_most_recent_first(agent_config, monkeypatch):
    _enable()
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda name: "/usr/bin/" + name)
    dir_a = agent_config / "a"
    dir_a.mkdir()
    dir_b = agent_config / "b"
    dir_b.mkdir()
    agentic_chat.start_session("claude", str(dir_a))
    agentic_chat.start_session("claude", str(dir_b))
    recent = agentic_chat.get_recent_projects()
    assert recent[0] == os.path.abspath(str(dir_b))
    assert recent[1] == os.path.abspath(str(dir_a))


def test_get_recent_projects_dedupes_and_moves_to_front(agent_config, monkeypatch):
    _enable()
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda name: "/usr/bin/" + name)
    dir_a = agent_config / "a"
    dir_a.mkdir()
    dir_b = agent_config / "b"
    dir_b.mkdir()
    agentic_chat.start_session("claude", str(dir_a))
    agentic_chat.start_session("claude", str(dir_b))
    agentic_chat.start_session("claude", str(dir_a))  # reuse dir_a -- must jump back to front
    recent = agentic_chat.get_recent_projects()
    assert recent == [os.path.abspath(str(dir_a)), os.path.abspath(str(dir_b))]


def test_get_recent_projects_caps_at_max(agent_config, monkeypatch):
    _enable()
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda name: "/usr/bin/" + name)
    total = agentic_chat._RECENT_PROJECTS_MAX + 3
    for i in range(total):
        d = agent_config / ("proj-%d" % i)
        d.mkdir()
        agentic_chat.start_session("claude", str(d))
    recent = agentic_chat.get_recent_projects()
    assert len(recent) == agentic_chat._RECENT_PROJECTS_MAX
    assert recent[0] == os.path.abspath(str(agent_config / ("proj-%d" % (total - 1))))


def test_api_agent_recent_projects_route(agent_config, monkeypatch):
    _enable()
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda name: "/usr/bin/" + name)
    agentic_chat.start_session("claude", str(agent_config))
    client = app.app.test_client()
    resp = client.get("/api/agent/recent-projects")
    assert resp.status_code == 200
    assert os.path.abspath(str(agent_config)) in resp.get_json()["recent_projects"]


def test_api_agent_recent_projects_403_when_disabled(agent_config):
    client = app.app.test_client()
    resp = client.get("/api/agent/recent-projects")
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Binary-identity safety check (claude-only GPT-proxy-shim defense)
# --------------------------------------------------------------------------- #

def test_verify_claude_binary_identity_pass(monkeypatch):
    monkeypatch.setattr(agentic_chat.subprocess, "run",
                        lambda *a, **kw: _FakeVersionCheck(stdout="2.1.212 (Claude Code)"))
    ok, detail = agentic_chat._verify_claude_binary_identity("/usr/bin/claude")
    assert ok is True
    assert detail is None


def test_verify_claude_binary_identity_fail_wrong_output(monkeypatch):
    monkeypatch.setattr(agentic_chat.subprocess, "run",
                        lambda *a, **kw: _FakeVersionCheck(stdout="some-other-cli v1.0.0"))
    ok, detail = agentic_chat._verify_claude_binary_identity("/usr/bin/claude")
    assert ok is False
    assert "does not appear to be Claude Code" in detail


def test_verify_claude_binary_identity_checks_stderr_too(monkeypatch):
    """A version string on stderr (not just stdout) must also count."""
    monkeypatch.setattr(agentic_chat.subprocess, "run",
                        lambda *a, **kw: _FakeVersionCheck(stdout="", stderr="2.1.212 (Claude Code)"))
    ok, detail = agentic_chat._verify_claude_binary_identity("/usr/bin/claude")
    assert ok is True


def test_verify_claude_binary_identity_fail_on_exception(monkeypatch):
    def _raise(*a, **kw):
        raise OSError("boom")
    monkeypatch.setattr(agentic_chat.subprocess, "run", _raise)
    ok, detail = agentic_chat._verify_claude_binary_identity("/usr/bin/claude")
    assert ok is False
    assert "could not run" in detail


def test_verify_claude_binary_identity_fail_on_timeout(monkeypatch):
    def _raise(*a, **kw):
        raise agentic_chat.subprocess.TimeoutExpired(cmd="claude --version", timeout=10)
    monkeypatch.setattr(agentic_chat.subprocess, "run", _raise)
    ok, detail = agentic_chat._verify_claude_binary_identity("/usr/bin/claude")
    assert ok is False


def test_should_check_binary_identity_only_claude_turn_one(agent_config):
    claude_sess = agentic_chat._Session("claude", str(agent_config))
    codex_sess = agentic_chat._Session("codex", str(agent_config))
    assert agentic_chat._should_check_binary_identity(claude_sess) is True
    assert agentic_chat._should_check_binary_identity(codex_sess) is False
    claude_sess.turn_count = 1
    assert agentic_chat._should_check_binary_identity(claude_sess) is False


def test_send_message_turn1_fails_closed_when_binary_identity_bad(agent_config, monkeypatch):
    sid = _start(agent_config, monkeypatch)
    monkeypatch.setattr(agentic_chat.subprocess, "run",
                        lambda *a, **kw: _FakeVersionCheck(stdout="not the real cli"))
    popen_calls = []
    monkeypatch.setattr(agentic_chat.subprocess, "Popen",
                        lambda argv, **kw: (popen_calls.append(argv) or FakeProc(stdout=_claude_json())))
    status, text, detail = agentic_chat.send_message(sid, "hi")
    assert status == 500
    assert text is None
    assert "does not appear to be Claude Code" in detail
    assert popen_calls == []  # the real turn must NEVER run once identity fails
    assert agentic_chat.get_session(sid)["turn_count"] == 0


def test_send_message_turn2_does_not_recheck_binary_identity(agent_config, monkeypatch):
    sid = _start(agent_config, monkeypatch)
    monkeypatch.setattr(agentic_chat.subprocess, "Popen",
                        lambda argv, **kw: FakeProc(stdout=_claude_json()))
    status1, text1, _ = agentic_chat.send_message(sid, "first")
    assert status1 == 200
    assert text1 == "Done."
    # Sabotage the version check for turn 2 -- if it were re-checked, this
    # would fail closed at 500; it must NOT, since the check is turn-1-only.
    monkeypatch.setattr(agentic_chat.subprocess, "run",
                        lambda *a, **kw: _FakeVersionCheck(stdout="not the real cli"))
    status2, text2, _ = agentic_chat.send_message(sid, "second")
    assert status2 == 200
    assert text2 == "Done."


# --------------------------------------------------------------------------- #
# Best-model injection: --model opus on every turn
# --------------------------------------------------------------------------- #

def test_build_argv_includes_model_opus_on_turn1(agent_config):
    sess = agentic_chat._Session("claude", str(agent_config))
    argv = agentic_chat._build_argv(sess, "/usr/bin/claude", "hello")
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "opus"


def test_build_argv_includes_model_opus_on_resume_turn(agent_config):
    sess = agentic_chat._Session("claude", str(agent_config))
    sess.native_session_id = "native-abc"
    argv = agentic_chat._build_argv(sess, "/usr/bin/claude", "hello")
    assert "--resume" in argv
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "opus"


def test_send_message_argv_includes_model_flag(agent_config, monkeypatch):
    sid = _start(agent_config, monkeypatch)
    captured = {}

    def fake_popen(argv, **kw):
        captured["argv"] = argv
        return FakeProc(stdout=_claude_json())

    monkeypatch.setattr(agentic_chat.subprocess, "Popen", fake_popen)
    agentic_chat.send_message(sid, "hi")
    assert "--model" in captured["argv"]
    assert captured["argv"][captured["argv"].index("--model") + 1] == "opus"


# --------------------------------------------------------------------------- #
# /api/agent/sessions/<id>/message -- wiring to agentic_history.record_turn()
# (new in this stage; agentic_chat.py itself is untouched by this wiring).
# --------------------------------------------------------------------------- #

def test_message_route_records_both_sides_on_success(agent_config, monkeypatch):
    _enable()
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(agentic_chat.subprocess, "Popen",
                        lambda argv, **kw: FakeProc(stdout=_claude_json(result="hi there")))
    client = app.app.test_client()
    resp = client.post("/api/agent/sessions",
                       json={"cli": "claude", "project_dir": str(agent_config)}, headers=_DASH)
    sid = resp.get_json()["session_id"]

    resp = client.post("/api/agent/sessions/%s/message" % sid, json={"text": "hello"}, headers=_DASH)
    assert resp.status_code == 200

    conv = agentic_history.get_conversation(sid)
    assert conv is not None
    assert conv["cli_id"] == "claude"
    assert conv["project_dir"] == str(agent_config)
    assert [(t["role"], t["text"]) for t in conv["turns"]] == [
        ("user", "hello"), ("agent", "hi there")]


def test_message_route_records_user_side_even_when_turn_fails(agent_config, monkeypatch):
    """The user's message must survive even when the CLI call itself fails
    (502/504/etc) -- only a genuine reply (status 200) gets an agent turn."""
    _enable()
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(agentic_chat.subprocess, "Popen",
                        lambda argv, **kw: FakeProc(stdout="", stderr="boom", returncode=1))
    client = app.app.test_client()
    resp = client.post("/api/agent/sessions",
                       json={"cli": "claude", "project_dir": str(agent_config)}, headers=_DASH)
    sid = resp.get_json()["session_id"]

    resp = client.post("/api/agent/sessions/%s/message" % sid, json={"text": "hello"}, headers=_DASH)
    assert resp.status_code == 502

    conv = agentic_history.get_conversation(sid)
    assert conv is not None
    assert [(t["role"], t["text"]) for t in conv["turns"]] == [("user", "hello")]


def test_message_route_records_nothing_for_unknown_session(agent_config):
    _enable()
    client = app.app.test_client()
    resp = client.post("/api/agent/sessions/does-not-exist/message", json={"text": "hi"}, headers=_DASH)
    assert resp.status_code == 404
    assert agentic_history.get_conversation("does-not-exist") is None


# --------------------------------------------------------------------------- #
# /api/agent/history* -- list/get/delete conversations, create/list checkpoints
# --------------------------------------------------------------------------- #

def test_api_agent_history_list_and_get(agent_config, monkeypatch):
    agentic_history.record_turn("sess-1", "claude", str(agent_config), "user", "hi")
    client = app.app.test_client()

    resp = client.get("/api/agent/history")
    assert resp.status_code == 200
    rows = resp.get_json()["conversations"]
    assert len(rows) == 1 and rows[0]["session_id"] == "sess-1"

    resp = client.get("/api/agent/history/sess-1")
    assert resp.status_code == 200
    assert resp.get_json()["turns"][0]["text"] == "hi"

    resp = client.get("/api/agent/history/does-not-exist")
    assert resp.status_code == 404


def test_api_agent_history_available_even_when_master_flag_off(agent_config):
    """History routes are NOT gated by the agentic_chat_enabled master flag --
    they only touch the locally-persisted transcript, never a live CLI."""
    config.set_flag("agentic_chat_enabled", False)
    agentic_history.record_turn("sess-1", "claude", str(agent_config), "user", "hi")
    client = app.app.test_client()
    assert client.get("/api/agent/history").status_code == 200
    assert client.get("/api/agent/history/sess-1").status_code == 200


def test_api_agent_history_delete(agent_config):
    agentic_history.record_turn("sess-1", "claude", str(agent_config), "user", "hi")
    client = app.app.test_client()
    resp = client.delete("/api/agent/history/sess-1", headers=_DASH)
    assert resp.status_code == 200
    assert resp.get_json()["deleted"] is True
    assert client.get("/api/agent/history/sess-1").status_code == 404
    assert client.delete("/api/agent/history/sess-1", headers=_DASH).status_code == 404


def test_api_agent_history_checkpoints_create_and_list(agent_config):
    agentic_history.record_turn("sess-1", "claude", str(agent_config), "user", "hi")
    client = app.app.test_client()

    resp = client.post("/api/agent/history/sess-1/checkpoints",
                       json={"label": "before big change"}, headers=_DASH)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["turn_index"] == 1
    assert body["label"] == "before big change"

    resp = client.get("/api/agent/history/sess-1/checkpoints")
    assert resp.status_code == 200
    checkpoints = resp.get_json()["checkpoints"]
    assert len(checkpoints) == 1
    assert checkpoints[0]["label"] == "before big change"


def test_api_agent_history_checkpoint_unknown_conversation_404s(agent_config):
    client = app.app.test_client()
    resp = client.post("/api/agent/history/does-not-exist/checkpoints", json={}, headers=_DASH)
    assert resp.status_code == 404


def test_api_agent_history_checkpoint_rejects_non_string_label(agent_config):
    agentic_history.record_turn("sess-1", "claude", str(agent_config), "user", "hi")
    client = app.app.test_client()
    resp = client.post("/api/agent/history/sess-1/checkpoints", json={"label": 5}, headers=_DASH)
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# Test-verification flag route -- mirrors /api/agent/settings' GET/POST shape.
# --------------------------------------------------------------------------- #

def test_api_agent_test_verification_get_default_off(agent_config):
    client = app.app.test_client()
    resp = client.get("/api/agent/test-verification")
    assert resp.status_code == 200
    assert resp.get_json()["enabled"] is False


def test_api_agent_test_verification_post_toggles_flag(agent_config):
    client = app.app.test_client()
    resp = client.post("/api/agent/test-verification", json={"enabled": True}, headers=_DASH)
    assert resp.status_code == 200
    assert resp.get_json()["enabled"] is True
    assert agentic_chat.test_verification_enabled() is True
    assert config.get_flag("agentic_test_verification_enabled", False) is True


def test_api_agent_test_verification_post_requires_bool(agent_config):
    client = app.app.test_client()
    resp = client.post("/api/agent/test-verification", json={"enabled": "yes"}, headers=_DASH)
    assert resp.status_code == 400


def test_api_agent_test_verification_not_gated_by_master_flag(agent_config):
    """This IS the route that configures the flag, so (like /api/agent/settings
    itself) it must work even while agentic chat's master flag is off."""
    client = app.app.test_client()
    assert client.get("/api/agent/test-verification").status_code == 200
    resp = client.post("/api/agent/test-verification", json={"enabled": True}, headers=_DASH)
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Vision-status route + underlying vision_status.py module
# --------------------------------------------------------------------------- #

def test_api_agent_vision_status_unavailable_by_default(agent_config):
    """A fresh config has no provider enabled/keyed at all -> unavailable."""
    client = app.app.test_client()
    resp = client.get("/api/agent/vision-status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["available"] is False
    assert body["providers"] == []


def test_vision_status_qualifies_enabled_keyed_vision_provider(agent_config):
    """'google' is a REAL registry provider (providers.py) with no_key=False
    and a non-empty vision_models list."""
    config.set_provider_config("google", api_key="fake-key-123", enabled=True)
    result = vision_status.status()
    assert result["available"] is True
    assert "google" in result["providers"]


def test_vision_status_ignores_disabled_or_unkeyed_vision_provider(agent_config):
    config.set_provider_config("google", enabled=False)
    assert vision_status.status()["available"] is False
    config.set_provider_config("google", enabled=True)  # no key saved
    assert vision_status.status()["available"] is False


def test_vision_status_flip_stamps_became_available_at(agent_config):
    before = vision_status.status()
    assert before["available"] is False
    assert before["vision_became_available_at"] is None
    config.set_provider_config("google", api_key="fake-key-123", enabled=True)
    after = vision_status.status()
    assert after["available"] is True
    assert after["vision_became_available_at"] is not None
    # Going unavailable again clears the streak marker.
    config.set_provider_config("google", enabled=False)
    cleared = vision_status.status()
    assert cleared["available"] is False
    assert cleared["vision_became_available_at"] is None


def test_start_heartbeat_is_idempotent(agent_config, monkeypatch):
    monkeypatch.setattr(vision_status, "_heartbeat_thread", None)
    vision_status.start_heartbeat()
    first = vision_status._heartbeat_thread
    assert first is not None
    vision_status.start_heartbeat()
    assert vision_status._heartbeat_thread is first


# --------------------------------------------------------------------------- #
# System-prompt injection (--append-system-prompt) -- confirmed (live doc
# fetch, code.claude.com/docs/en/cli-reference) to be a real CLI-usable flag
# alongside -p that does NOT persist across --resume, so (like --model) it
# must be re-sent on EVERY turn, not just turn 1.
# --------------------------------------------------------------------------- #

def test_system_prompt_absent_when_verification_off_and_vision_available(agent_config, monkeypatch):
    sid = _start(agent_config, monkeypatch)
    monkeypatch.setattr(agentic_chat.vision_status, "status",
                        lambda: {"available": True, "providers": ["google"]})
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        return FakeProc(stdout=_claude_json())

    monkeypatch.setattr(agentic_chat.subprocess, "Popen", fake_popen)
    agentic_chat.send_message(sid, "hello")
    assert "--append-system-prompt" not in captured["argv"]


def test_system_prompt_carries_vision_gap_notice_when_unavailable(agent_config, monkeypatch):
    sid = _start(agent_config, monkeypatch)
    monkeypatch.setattr(agentic_chat.vision_status, "status",
                        lambda: {"available": False, "providers": []})
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        return FakeProc(stdout=_claude_json())

    monkeypatch.setattr(agentic_chat.subprocess, "Popen", fake_popen)
    agentic_chat.send_message(sid, "hello")
    assert "--append-system-prompt" in captured["argv"]
    text = captured["argv"][captured["argv"].index("--append-system-prompt") + 1]
    assert "vision-capable model" in text


def test_system_prompt_carries_test_verification_notice_when_enabled(agent_config, monkeypatch):
    sid = _start(agent_config, monkeypatch)
    monkeypatch.setattr(agentic_chat.vision_status, "status",
                        lambda: {"available": True, "providers": ["google"]})
    agentic_chat.set_test_verification_enabled(True)
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        return FakeProc(stdout=_claude_json())

    monkeypatch.setattr(agentic_chat.subprocess, "Popen", fake_popen)
    agentic_chat.send_message(sid, "hello")
    assert "--append-system-prompt" in captured["argv"]
    text = captured["argv"][captured["argv"].index("--append-system-prompt") + 1]
    assert "Testing/verification" in text
    assert "vision-capable model" not in text  # vision available -> no gap notice


def test_system_prompt_present_on_every_turn_not_just_turn1(agent_config, monkeypatch):
    sid = _start(agent_config, monkeypatch)
    monkeypatch.setattr(agentic_chat.vision_status, "status",
                        lambda: {"available": False, "providers": []})
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append(argv)
        return FakeProc(stdout=_claude_json(result="turn %d" % len(calls), session_id="native-abc"))

    monkeypatch.setattr(agentic_chat.subprocess, "Popen", fake_popen)
    agentic_chat.send_message(sid, "first")
    agentic_chat.send_message(sid, "second")
    assert "--append-system-prompt" in calls[0]
    assert "--append-system-prompt" in calls[1]


def test_api_agent_test_verification_toggle_flows_into_argv_via_http(agent_config, monkeypatch):
    """End-to-end: flipping the flag via the HTTP route changes the next
    turn's actual argv."""
    _enable()
    monkeypatch.setattr(agentic_chat.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(agentic_chat.vision_status, "status",
                        lambda: {"available": True, "providers": ["google"]})
    client = app.app.test_client()
    resp = client.post("/api/agent/sessions",
                       json={"cli": "claude", "project_dir": str(agent_config)}, headers=_DASH)
    sid = resp.get_json()["session_id"]
    client.post("/api/agent/test-verification", json={"enabled": True}, headers=_DASH)
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        return FakeProc(stdout=_claude_json())

    monkeypatch.setattr(agentic_chat.subprocess, "Popen", fake_popen)
    client.post("/api/agent/sessions/%s/message" % sid, json={"text": "hi"}, headers=_DASH)
    assert "--append-system-prompt" in captured["argv"]
