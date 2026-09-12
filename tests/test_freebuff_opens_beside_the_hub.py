r"""Freebuff is installed and opened BESIDE the hub, not routed through it.

Asked for: "connect freebuff from the hub ... run in background ... use their
CLI." Freebuff is Codebuff's free coding agent -- a terminal TUI, not an
OpenAI-compatible endpoint. Its free models answer only requests that look
byte-for-byte like its own CLI (system prompt, publisher, one-model session;
see common/src/constants/free-agents.ts), and the upstream 403s a direct call
with "may get your account banned". So the hub does NOT proxy it, strip its
ads, or rotate accounts against its gate -- that would be circumventing its
access control.

What the hub does: install Freebuff into its own isolated npm prefix + HOME,
and open it in its own window in the project folder a Build session is using.
It runs alongside the hub (the hub returns at once, does not block); it needs
its own console because the TUI exits 139 with no terminal, so it is not
pretended to be headless. The ads stay in Freebuff's own window.
"""
import os

import pytest

import app as A


APP = open("app.py", encoding="utf-8").read()
SRC = open("templates/index.html", encoding="utf-8").read()


@pytest.fixture(autouse=True)
def _tok(monkeypatch):
    monkeypatch.setattr(A, "_has_control_token", lambda: True)
    yield


def _hdr():
    return {"X-Free-LLM-Hub": "dashboard",
            "X-Free-LLM-Hub-Token": A.config.get_control_token() or ""}


# --------------------------------------------------------------------------- #
# Isolation
# --------------------------------------------------------------------------- #

def test_freebuff_lives_in_its_own_isolated_dirs():
    assert A._freebuff_install_dir().endswith(os.path.join("freebuff", "install"))
    assert A._freebuff_home().endswith(os.path.join("freebuff", "home"))
    assert "isolated-clis" in A._freebuff_root()


def test_its_env_is_a_home_of_its_own_with_no_hub_pointers(monkeypatch, tmp_path):
    monkeypatch.setattr(A, "_freebuff_home", lambda: str(tmp_path / "fbhome"))
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:8787")
    monkeypatch.setattr(A, "_points_at_hub", lambda v: isinstance(v, str) and "127.0.0.1:8787" in v)
    env = A._freebuff_env()
    assert env["HOME"] == str(tmp_path / "fbhome")
    assert env["USERPROFILE"] == str(tmp_path / "fbhome")
    assert "ANTHROPIC_BASE_URL" not in env, "a hub-pointing var must be stripped"


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #

def test_status_reports_not_installed(monkeypatch):
    monkeypatch.setattr(A, "_freebuff_bin", lambda: None)
    r = A.app.test_client().get("/api/freebuff/status", headers=_hdr())
    assert r.status_code == 200
    d = r.get_json()
    assert d["installed"] is False
    assert "not routed through the hub" in d["note"]


def test_status_reports_installed(monkeypatch, tmp_path):
    fake = tmp_path / "freebuff.cmd"
    fake.write_text("x", encoding="utf-8")
    monkeypatch.setattr(A, "_freebuff_bin", lambda: str(fake))
    d = A.app.test_client().get("/api/freebuff/status", headers=_hdr()).get_json()
    assert d["installed"] is True and d["bin_path"]


# --------------------------------------------------------------------------- #
# Open
# --------------------------------------------------------------------------- #

def test_open_refuses_a_folder_that_is_not_there(monkeypatch, tmp_path):
    monkeypatch.setattr(A, "_freebuff_bin", lambda: str(tmp_path / "freebuff.cmd"))
    r = A.app.test_client().post("/api/freebuff/open",
                                 json={"project_dir": str(tmp_path / "nope")}, headers=_hdr())
    assert r.status_code == 400


def test_open_needs_it_installed(monkeypatch, tmp_path):
    monkeypatch.setattr(A, "_freebuff_bin", lambda: None)
    r = A.app.test_client().post("/api/freebuff/open",
                                 json={"project_dir": str(tmp_path)}, headers=_hdr())
    assert r.status_code == 400 and r.get_json()["code"] == "not_installed"


def test_open_launches_in_its_own_window_and_returns_at_once(monkeypatch, tmp_path):
    fake = tmp_path / "freebuff.cmd"
    fake.write_text("x", encoding="utf-8")
    monkeypatch.setattr(A, "_freebuff_bin", lambda: str(fake))
    monkeypatch.setattr(A, "_freebuff_env", lambda: {"HOME": str(tmp_path)})
    calls = {}

    class _Popen:
        def __init__(self, argv, **kw):
            calls["argv"] = argv
            calls["cwd"] = kw.get("cwd")
            calls["flags"] = kw.get("creationflags")
    monkeypatch.setattr(A.subprocess, "Popen", _Popen)
    if os.name == "nt":
        monkeypatch.setattr(A.subprocess, "CREATE_NEW_CONSOLE", 0x10, raising=False)
    r = A.app.test_client().post("/api/freebuff/open",
                                 json={"project_dir": str(tmp_path)}, headers=_hdr())
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert "--cwd" in calls["argv"] and str(tmp_path) in calls["argv"]
    assert calls["cwd"] == str(tmp_path)
    if os.name == "nt":
        # its own console -- never CREATE_NO_WINDOW, which crashes the TUI
        assert calls["flags"] and calls["flags"] & A.subprocess.CREATE_NEW_CONSOLE
        assert calls["flags"] & A._CREATE_NO_WINDOW == 0 or A._CREATE_NO_WINDOW == 0


def test_install_needs_npm(monkeypatch):
    monkeypatch.setattr(A.shutil, "which", lambda n: None)
    r = A.app.test_client().post("/api/freebuff/install", json={}, headers=_hdr())
    assert r.status_code == 400 and "npm" in r.get_json()["error"].lower()


# --------------------------------------------------------------------------- #
# The hub does NOT proxy it
# --------------------------------------------------------------------------- #

def test_freebuff_is_not_a_routed_provider():
    """No /v1 provider, no chain hop, no ad-stripping: only install/open/status."""
    import re
    routes = set(re.findall(r'@app\.route\("(/api/freebuff/[^"]+)"', APP))
    assert routes == {"/api/freebuff/status", "/api/freebuff/install", "/api/freebuff/open"}
    # It is not registered as a provider anywhere.
    assert '"freebuff"' not in APP[APP.index("PROVIDERS = "):APP.index("PROVIDERS = ") + 200] \
        if "PROVIDERS = " in APP else True


def test_the_code_says_why_it_is_not_proxied():
    body = APP[APP.index("# Freebuff -- the free Codebuff"):]
    body = body[:body.index("_FREEBUFF_PKG")]
    assert "may get your account banned" in body
    assert "does NOT proxy it, strip its" in body


# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #

def test_the_build_page_has_the_button():
    assert 'id="agent-freebuff"' in SRC
    body = SRC[SRC.index("function initAgentFreebuff()"):]
    body = body[:body.index("function initAgentMode()")]
    assert "/api/freebuff/status" in body
    assert "/api/freebuff/install" in body
    assert "/api/freebuff/open" in body
    assert "cxAgentProjectDir" in body
    assert "initAgentFreebuff();" in SRC


# --------------------------------------------------------------------------- #
# The Providers page card
# --------------------------------------------------------------------------- #

def test_the_providers_page_has_a_freebuff_card():
    assert 'id="freebuff-card"' in SRC
    assert 'id="fb-install"' in SRC and 'id="fb-open"' in SRC
    body = SRC[SRC.index("function initFreebuffCard()"):]
    body = body[:body.index("function loadProviders()")]
    assert "/api/freebuff/status" in body
    assert "/api/freebuff/install" in body
    assert "/api/freebuff/open" in body
    assert "/api/agent/new-project" in body, "the card makes a folder to run in"
    assert "initFreebuffCard();" in SRC


def test_the_card_says_it_is_not_a_routed_provider():
    i = SRC.index('id="freebuff-card"')
    around = SRC[i - 400:i + 700]
    assert "not proxied through the hub" in around or "not a routed" in around
    assert "ads stay in" in around
