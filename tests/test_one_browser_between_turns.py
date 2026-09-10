r"""One browser, kept between turns, instead of a new one every turn.

REQUESTED: "make it also using playwright perfectly and also playwright CLI,
and keep same session ... and he can do all actions smoothly".

WHAT WAS WRONG. playwright was registered as an STDIO MCP server -- the CLI
spawns `npx @playwright/mcp` itself. And this hub spawns a fresh CLI process
for EVERY turn (agentic_chat's own words: "the CLI is re-spawned for every
turn"), so the MCP child died with it and took the browser with it. Every turn
opened a brand-new browser: logged out, blank page, nothing the previous turn
had navigated to. An agent could not click on turn two what it had opened on
turn one, which is most of what driving a browser is.

@playwright/mcp also runs as a long-lived SSE/HTTP server (`--port`). One of
those, started by the hub and shared by every agent and every turn, is a
browser that stays where it was left; `--user-data-dir` keeps the profile
across hub restarts as well.

FAILS OPEN. No node, or a server that does not come up, and the STDIO spec is
registered exactly as before -- never worse than it was.

VERIFIED LIVE on this machine: the server came up, was registered as
http://127.0.0.1:8931/mcp, and a second start() adopted it instead of spawning
a second one (6 playwright processes before, 6 after).
"""
import io

import app as A


SRC = io.open("app.py", encoding="utf-8").read()


# --------------------------------------------------------------------------- #
# It is a server, not a per-turn child
# --------------------------------------------------------------------------- #

def test_the_server_is_started_with_a_port():
    body = SRC[SRC.index("def _start_playwright_mcp("):]
    body = body[:body.index("\ndef ")]
    assert '"--port"' in body


def test_the_profile_survives_a_restart():
    body = SRC[SRC.index("def _start_playwright_mcp("):]
    body = body[:body.index("\ndef ")]
    assert '"--user-data-dir"' in body
    assert A._playwright_profile_dir()


def test_it_binds_loopback_only():
    body = SRC[SRC.index("def _start_playwright_mcp("):]
    body = body[:body.index("\ndef ")]
    assert '"--host", "127.0.0.1"' in body


def test_it_opens_no_console_window():
    body = SRC[SRC.index("def _start_playwright_mcp("):]
    body = body[:body.index("\ndef ")]
    assert "_CREATE_NO_WINDOW" in body


# --------------------------------------------------------------------------- #
# Registration follows what actually happened
# --------------------------------------------------------------------------- #

def test_agents_get_a_url_once_the_server_is_up(monkeypatch):
    monkeypatch.setattr(A, "_playwright_url", ["http://127.0.0.1:8931/mcp"])
    spec = dict(A._always_mcp())["playwright"]
    assert spec == {"url": "http://127.0.0.1:8931/mcp"}


def test_it_falls_back_to_stdio_when_the_server_is_not_there(monkeypatch):
    """Never worse than before: no node, or a server that will not start, and
    every agent still gets the per-turn browser it used to have."""
    monkeypatch.setattr(A, "_playwright_url", [None])
    spec = dict(A._always_mcp())["playwright"]
    assert spec == A._PLAYWRIGHT_STDIO
    assert spec["command"] == "npx"


def test_the_other_two_servers_are_untouched(monkeypatch):
    monkeypatch.setattr(A, "_playwright_url", [None])
    names = [n for n, _s in A._always_mcp()]
    assert names == ["free-llm-hub", "context7", "playwright"]


def test_the_registrar_reads_the_function_not_a_stale_constant():
    """It was a module-level tuple, which would have frozen the STDIO spec at
    import time -- before the server had a chance to start."""
    assert "for name, spec in _always_mcp():" in SRC
    assert "for name, spec in _ALWAYS_MCP:" not in SRC


# --------------------------------------------------------------------------- #
# Starting it
# --------------------------------------------------------------------------- #

def test_an_already_running_server_is_adopted(monkeypatch):
    """Two hubs, or a restart, must not leave two browsers behind."""
    spawned = []
    monkeypatch.setattr(A, "_playwright_url", [None])
    monkeypatch.setattr(A, "_playwright_probe", lambda port: "http://127.0.0.1:%d/mcp" % port)
    monkeypatch.setattr(A.subprocess, "Popen",
                        lambda *a, **k: spawned.append(a) or (_ for _ in ()).throw(
                            AssertionError("should not spawn")))
    A._start_playwright_mcp()
    assert A._playwright_url[0].endswith("/mcp")
    assert not spawned


def test_no_node_means_no_attempt(monkeypatch):
    monkeypatch.setattr(A, "_playwright_url", [None])
    monkeypatch.setattr(A, "_playwright_probe", lambda port: None)
    monkeypatch.setattr(A, "_which_cli", lambda name: None)
    monkeypatch.setattr(A.shutil, "which", lambda name: None)
    A._start_playwright_mcp()
    assert A._playwright_url[0] is None


def test_a_failure_to_start_is_not_fatal(monkeypatch):
    monkeypatch.setattr(A, "_playwright_url", [None])
    monkeypatch.setattr(A, "_playwright_probe", lambda port: None)
    monkeypatch.setattr(A, "_which_cli", lambda name: "npx")
    def boom(*a, **k):
        raise OSError("no npx after all")
    monkeypatch.setattr(A.subprocess, "Popen", boom)
    A._start_playwright_mcp()          # must not raise
    assert A._playwright_url[0] is None


def test_the_probe_prefers_the_modern_transport():
    """/sse can answer a second before /mcp during start-up; latching the older
    one on that timing would be luck rather than a choice."""
    body = SRC[SRC.index("def _playwright_probe("):]
    body = body[:body.index("\ndef ")]
    assert body.index('"/mcp"') < body.index('"/sse"')
    start = SRC[SRC.index("def _start_playwright_mcp("):]
    start = start[:start.index("\ndef ")]
    assert "_playwright_probe(_PLAYWRIGHT_PORT) or found" in start, \
        "no re-probe once it has settled"


def test_it_is_started_at_boot_off_the_main_thread():
    """The first run downloads a package; blocking boot on that would make the
    hub look hung."""
    assert "target=_start_playwright_mcp" in SRC
    i = SRC.index("target=_start_playwright_mcp")
    assert "daemon=True" in SRC[i - 200:i + 200]


def test_the_registered_url_uses_localhost_not_the_loopback_ip():
    """@playwright/mcp enforces its own host check: anything but
    localhost:<port> gets "Access is only allowed at localhost:8931". A URL
    spelled 127.0.0.1 registers cleanly and then 403s for every agent that
    tries to use it. Found by connecting to the running server, not by reading
    the code."""
    body = SRC[SRC.index("def _playwright_probe("):]
    body = body[:body.index("\ndef ")]
    assert 'http://localhost:%d' in body
    assert "127.0.0.1:%d%s" not in body


def test_the_context_is_shared_between_clients():
    """A long-lived server was only half of "keep same session".

    Without --shared-browser-context every connected HTTP client gets its OWN
    browser context, and this hub re-spawns the CLI for every turn -- so every
    turn is a new client and therefore a new context: a fresh browser, logged
    out, on a blank page. Which is the exact failure the shared server exists to
    fix. From @playwright/mcp --help: "reuse the same browser context between
    all connected HTTP clients"."""
    body = SRC[SRC.index("def _start_playwright_mcp("):]
    body = body[:body.index("\ndef _always_mcp(")]
    assert "--shared-browser-context" in body


def test_it_is_on_the_server_not_on_the_stdio_fallback():
    """The stdio spec is one browser per CLI process by construction; the flag
    would be meaningless there and is a real cost on the server."""
    assert "--shared-browser-context" not in str(A._PLAYWRIGHT_STDIO)


def test_the_profile_is_still_kept_on_disk():
    """The context is shared between clients; the PROFILE -- logins, cookies --
    is what survives a hub restart, and that is --user-data-dir."""
    body = SRC[SRC.index("def _start_playwright_mcp("):]
    body = body[:body.index("\ndef _always_mcp(")]
    assert "--user-data-dir" in body


# --------------------------------------------------------------------------- #
# A server started before the flag existed must not be adopted forever
# --------------------------------------------------------------------------- #

def test_a_server_we_started_without_it_is_retired(monkeypatch, tmp_path):
    """The server outlives the hub -- that is the point of it -- so the next
    start adopts whatever is on the port. Adopting one launched before
    --shared-browser-context would keep handing every turn its own fresh
    browser forever, and nothing over HTTP can tell the difference."""
    monkeypatch.setattr(A, "_playwright_marker_path",
                        lambda: str(tmp_path / "marker.json"))
    A._write_playwright_marker(4321, shared=False)
    killed = []
    monkeypatch.setattr(A, "_retire_stale_playwright",
                        lambda m: killed.append(m.get("pid")) or True)
    probes = ["http://localhost:8931/mcp", None]
    monkeypatch.setattr(A, "_playwright_probe", lambda port: probes.pop(0))
    monkeypatch.setattr(A, "_which_cli", lambda name: None)
    monkeypatch.setattr(A.shutil, "which", lambda name: None)
    A._playwright_url[0] = None
    A._start_playwright_mcp()
    assert killed == [4321]


def test_a_server_someone_else_runs_is_never_killed(monkeypatch, tmp_path):
    """No marker means we did not start it. Killing someone's browser because
    it lacks a flag we happen to want is not a trade this hub gets to make."""
    monkeypatch.setattr(A, "_playwright_marker_path",
                        lambda: str(tmp_path / "none.json"))
    killed = []
    monkeypatch.setattr(A, "_retire_stale_playwright",
                        lambda m: killed.append(m) or True)
    monkeypatch.setattr(A, "_playwright_probe",
                        lambda port: "http://localhost:8931/mcp")
    A._playwright_url[0] = None
    A._start_playwright_mcp()
    assert killed == []
    assert A._playwright_url[0] == "http://localhost:8931/mcp"


def test_our_own_shared_server_is_adopted_not_restarted(monkeypatch, tmp_path):
    monkeypatch.setattr(A, "_playwright_marker_path",
                        lambda: str(tmp_path / "marker.json"))
    A._write_playwright_marker(999, shared=True)
    killed = []
    monkeypatch.setattr(A, "_retire_stale_playwright",
                        lambda m: killed.append(m) or True)
    monkeypatch.setattr(A, "_playwright_probe",
                        lambda port: "http://localhost:8931/mcp")
    A._playwright_url[0] = None
    A._start_playwright_mcp()
    assert killed == []


def test_a_missing_marker_reads_as_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(A, "_playwright_marker_path",
                        lambda: str(tmp_path / "nope.json"))
    assert A._playwright_marker() == {}


def test_a_corrupt_marker_reads_as_empty(monkeypatch, tmp_path):
    path = tmp_path / "marker.json"
    path.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(A, "_playwright_marker_path", lambda: str(path))
    assert A._playwright_marker() == {}


def test_retiring_without_a_pid_does_nothing():
    assert A._retire_stale_playwright({}) is False
    assert A._retire_stale_playwright({"pid": "not a pid"}) is False
