"""The hub's browser runs with no window (2026-11-21).

REPORTED: an agent's web search (or any page check) popped a real Chrome window on
the owner's screen; the owner wants the browser used in the background.

CAUSE: @playwright/mcp is HEADED unless it gets --headless, and the hub started
the shared server (and registered the stdio fallback) without it.

FIX: flag `playwright_headless` (default ON) adds --headless to the shared server
and to the stdio fallback. The server outlives the hub and is adopted on the next
start, so its marker records "headless" and a server of ours started the other way
is retired once and relaunched. A server we did not start is never touched.
"""
import io

import app as A

SRC = io.open("app.py", encoding="utf-8").read()
URL = "http://localhost:8931/mcp"


def _body():
    body = SRC[SRC.index("def _start_playwright_mcp("):]
    return body[:body.index("\ndef _always_mcp(")]


def _flag(monkeypatch, value):
    monkeypatch.setattr(A, "_playwright_headless", lambda: value)


def _marker(monkeypatch, tmp_path, **kw):
    monkeypatch.setattr(A, "_playwright_marker_path", lambda: str(tmp_path / "marker.json"))
    if kw:
        A._write_playwright_marker(4321, **kw)


def _watch_retire(monkeypatch):
    killed = []
    monkeypatch.setattr(A, "_retire_stale_playwright",
                        lambda m: killed.append(m.get("pid")) or True)
    return killed


def _never_spawn(monkeypatch):
    monkeypatch.setattr(A, "_which_cli", lambda name: None)
    monkeypatch.setattr(A.shutil, "which", lambda name: None)


# --------------------------------------------------------------------------- #
# The flag
# --------------------------------------------------------------------------- #

def test_headless_is_the_default(monkeypatch):
    monkeypatch.setattr(A.config, "get_flag", lambda name, default=False: default)
    assert A._playwright_headless() is True


def test_the_flag_can_turn_it_off(monkeypatch):
    monkeypatch.setattr(A.config, "get_flag", lambda name, default=False: False)
    assert A._playwright_headless() is False


def test_an_unreadable_flag_means_headless(monkeypatch):
    def boom(*a, **k):
        raise OSError("config unreadable")
    monkeypatch.setattr(A.config, "get_flag", boom)
    assert A._playwright_headless() is True


# --------------------------------------------------------------------------- #
# The stdio fallback
# --------------------------------------------------------------------------- #

def test_the_stdio_fallback_is_headless_by_default(monkeypatch):
    _flag(monkeypatch, True)
    spec = A._playwright_stdio()
    assert spec["command"] == "npx"
    assert "--headless" in spec["args"]
    assert spec["args"][:2] == ["-y", "@playwright/mcp@latest"]


def test_the_stdio_fallback_is_headed_when_the_flag_is_off(monkeypatch):
    _flag(monkeypatch, False)
    assert A._playwright_stdio()["args"] == ["-y", "@playwright/mcp@latest"]


def test_registration_uses_that_spec_when_the_server_is_down(monkeypatch):
    _flag(monkeypatch, True)
    monkeypatch.setattr(A, "_playwright_url", [None])
    assert dict(A._always_mcp())["playwright"] == A._playwright_stdio()


def test_the_registered_url_is_unchanged_when_the_server_is_up(monkeypatch):
    monkeypatch.setattr(A, "_playwright_url", [URL])
    assert dict(A._always_mcp())["playwright"] == {"url": URL}


def test_the_module_constant_is_not_mutated(monkeypatch):
    _flag(monkeypatch, False)
    A._playwright_stdio()
    assert "--headless" in A._PLAYWRIGHT_STDIO["args"]


# --------------------------------------------------------------------------- #
# The shared server
# --------------------------------------------------------------------------- #

def _launch(monkeypatch, tmp_path, headless):
    """Run the start path once and return the argv it spawned."""
    _flag(monkeypatch, headless)
    _marker(monkeypatch, tmp_path)
    spawned = []

    class _P:
        pid = 777

    monkeypatch.setattr(A.subprocess, "Popen",
                        lambda argv, **k: spawned.append(list(argv)) or _P())
    probes = [None]                       # nothing on the port yet
    monkeypatch.setattr(A, "_playwright_probe",
                        lambda port: probes.pop(0) if probes else URL)
    monkeypatch.setattr(A, "_which_cli", lambda name: "npx")
    monkeypatch.setattr(A.time, "sleep", lambda s: None)
    monkeypatch.setattr(A, "_playwright_url", [None])
    A._start_playwright_mcp()
    assert len(spawned) == 1
    return spawned[0]


def test_the_server_is_started_headless(monkeypatch, tmp_path):
    argv = _launch(monkeypatch, tmp_path, True)
    assert "--headless" in argv
    assert "--shared-browser-context" in argv      # the rest of the launch is unchanged
    assert "--user-data-dir" in argv
    assert A._playwright_marker().get("headless") is True


def test_the_server_is_started_headed_when_the_flag_is_off(monkeypatch, tmp_path):
    argv = _launch(monkeypatch, tmp_path, False)
    assert "--headless" not in argv
    assert A._playwright_marker().get("headless") is False


def test_the_marker_records_how_it_was_started(tmp_path, monkeypatch):
    _marker(monkeypatch, tmp_path)
    A._write_playwright_marker(5, shared=True, headless=False)
    assert A._playwright_marker()["headless"] is False
    A._write_playwright_marker(5, shared=True, headless=True)
    assert A._playwright_marker()["headless"] is True


def test_the_launch_path_reads_the_flag_once_and_uses_it_for_the_marker():
    body = _body()
    assert "headless = _playwright_headless()" in body
    assert 'argv.append("--headless")' in body
    assert "headless=headless" in body


# --------------------------------------------------------------------------- #
# A server that is already running
# --------------------------------------------------------------------------- #

def test_our_headed_server_is_retired_so_it_comes_back_headless(monkeypatch, tmp_path):
    """The server the hub started before this change has no "headless" key."""
    _flag(monkeypatch, True)
    _marker(monkeypatch, tmp_path)
    (tmp_path / "marker.json").write_text(
        '{"port": 8931, "pid": 4321, "shared": true, "at": 1}', encoding="utf-8")
    killed = _watch_retire(monkeypatch)
    probes = [URL, None]
    monkeypatch.setattr(A, "_playwright_probe", lambda port: probes.pop(0))
    _never_spawn(monkeypatch)
    monkeypatch.setattr(A, "_playwright_url", [None])
    A._start_playwright_mcp()
    assert killed == [4321]


def test_a_headless_server_is_adopted_not_restarted(monkeypatch, tmp_path):
    _flag(monkeypatch, True)
    _marker(monkeypatch, tmp_path, shared=True, headless=True)
    killed = _watch_retire(monkeypatch)
    monkeypatch.setattr(A, "_playwright_probe", lambda port: URL)
    monkeypatch.setattr(A, "_playwright_url", [None])
    A._start_playwright_mcp()
    assert killed == []
    assert A._playwright_url[0] == URL


def test_switching_the_flag_off_retires_the_headless_server(monkeypatch, tmp_path):
    """Owner turns headless off to watch or sign in: the next start relaunches it headed."""
    _flag(monkeypatch, False)
    _marker(monkeypatch, tmp_path, shared=True, headless=True)
    killed = _watch_retire(monkeypatch)
    probes = [URL, None]
    monkeypatch.setattr(A, "_playwright_probe", lambda port: probes.pop(0))
    _never_spawn(monkeypatch)
    monkeypatch.setattr(A, "_playwright_url", [None])
    A._start_playwright_mcp()
    assert killed == [4321]


def test_a_headed_server_is_adopted_when_the_flag_is_off(monkeypatch, tmp_path):
    _flag(monkeypatch, False)
    _marker(monkeypatch, tmp_path, shared=True, headless=False)
    killed = _watch_retire(monkeypatch)
    monkeypatch.setattr(A, "_playwright_probe", lambda port: URL)
    monkeypatch.setattr(A, "_playwright_url", [None])
    A._start_playwright_mcp()
    assert killed == []


def test_a_server_we_did_not_start_is_never_killed(monkeypatch, tmp_path):
    """No marker = not ours, whatever window it opens."""
    _flag(monkeypatch, True)
    _marker(monkeypatch, tmp_path)          # no file written
    killed = _watch_retire(monkeypatch)
    monkeypatch.setattr(A, "_playwright_probe", lambda port: URL)
    monkeypatch.setattr(A, "_playwright_url", [None])
    A._start_playwright_mcp()
    assert killed == []
    assert A._playwright_url[0] == URL


def test_a_failed_retire_still_adopts_the_running_server(monkeypatch, tmp_path):
    """Fail open: if the old server cannot be stopped, agents keep using it."""
    _flag(monkeypatch, True)
    _marker(monkeypatch, tmp_path, shared=True, headless=False)
    monkeypatch.setattr(A, "_retire_stale_playwright", lambda m: False)
    monkeypatch.setattr(A, "_playwright_probe", lambda port: URL)
    monkeypatch.setattr(A, "_playwright_url", [None])
    A._start_playwright_mcp()
    assert A._playwright_url[0] == URL
