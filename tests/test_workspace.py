"""Running the project the agent just wrote, next to the chat.

The agent chat could already drive a CLI inside a folder; it could not CLOSE THE
LOOP — files were written and nobody ever ran them. These cover the two things
that make the preview trustworthy: the start command is DERIVED from the folder
(never taken from a request, so nothing here reaches a shell), and a crash
becomes a clickable problem instead of a wall of log.
"""
import os
import shutil
import tempfile
import time

import pytest

import app
import workspace


@pytest.fixture
def proj():
    """Own temp dir, not pytest's `tmp_path`: the tmp_path factory raises
    PermissionError on this machine (it is the cause of the suite's known
    errors), and these tests must actually run."""
    d = tempfile.mkdtemp(prefix="hubws-")
    try:
        yield d
    finally:
        workspace.stop(d)
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Detection — the only thing that decides what gets executed
# --------------------------------------------------------------------------- #

def test_a_static_folder_is_served(proj):
    open(os.path.join(proj, "index.html"), "w").write("<h1>hi</h1>")
    spec = workspace.detect(proj)
    assert spec["kind"] == "static"
    assert "http.server" in spec["argv"]


def test_a_package_script_wins_over_everything_else(proj):
    open(os.path.join(proj, "index.html"), "w").write("<h1>hi</h1>")
    open(os.path.join(proj, "package.json"), "w").write(
        '{"scripts": {"dev": "vite", "start": "node ."}}')
    spec = workspace.detect(proj)
    assert spec["kind"] == "npm:dev", "the project's own dev script must win"
    assert spec["needs_install"] is True, "no node_modules yet"


def test_start_script_is_used_when_there_is_no_dev(proj):
    open(os.path.join(proj, "package.json"), "w").write('{"scripts": {"start": "node ."}}')
    assert workspace.detect(proj)["kind"] == "npm:start"


def test_a_python_entry_point_is_found(proj):
    open(os.path.join(proj, "app.py"), "w").write("print('x')")
    assert workspace.detect(proj)["kind"] == "python:app.py"


def test_an_empty_folder_says_so_instead_of_guessing(proj):
    with pytest.raises(workspace.WorkspaceError) as e:
        workspace.detect(proj)
    assert "nothing runnable" in str(e.value)


def test_detect_never_returns_a_shell_string(proj):
    """argv is a LIST on every path. A string would be a shell injection the
    moment a folder name contained a quote."""
    open(os.path.join(proj, "index.html"), "w").write("x")
    assert isinstance(workspace.detect(proj)["argv"], list)


# --------------------------------------------------------------------------- #
# Problems — what the user actually clicks
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("line,kind", [
    ("Error: Cannot find module 'express'", "module"),
    ("ModuleNotFoundError: No module named 'flask'", "module"),
    ("SyntaxError: Unexpected token '}'", "syntax"),
    ("TypeError: Cannot read properties of undefined", "exception"),
    ("Error: listen EADDRINUSE: address already in use :::3000", "address"),
])
def test_real_failures_are_recognised(line, kind):
    got = workspace.problems_from([line])
    assert got and got[0]["kind"] == kind, got


def test_normal_startup_chatter_is_not_a_problem():
    """A panel that cries wolf gets ignored, which defeats having one."""
    quiet = [
        "VITE v5.0.0  ready in 312 ms",
        "  Local:   http://127.0.0.1:5800/",
        "npm warn deprecated inflight@1.0.6",
        "found 0 vulnerabilities",
        "Compiled successfully.",
        "Browserslist: caniuse-lite is outdated",
    ]
    assert workspace.problems_from(quiet) == []


def test_the_same_error_repeated_is_listed_once():
    """A dev server reprints its error on every file save."""
    got = workspace.problems_from(["SyntaxError: Unexpected token"] * 12)
    assert len(got) == 1


def test_problems_keep_the_raw_line_for_the_agent():
    """The agent needs the exact text, not our summary of it."""
    raw = "  ModuleNotFoundError: No module named 'flask'  "
    got = workspace.problems_from([raw])
    assert got[0]["line"].strip() == raw.strip()


def test_the_problem_list_is_bounded():
    lines = ["SyntaxError: bad token %d" % i for i in range(200)]
    assert len(workspace.problems_from(lines)) <= workspace.MAX_PROBLEMS


def test_absurdly_long_lines_are_skipped():
    """A minified bundle dumped to stderr must not become a 'problem'."""
    assert workspace.problems_from(["Error: " + "x" * 5000]) == []


# --------------------------------------------------------------------------- #
# Lifecycle — the real thing, on a real port
# --------------------------------------------------------------------------- #

def test_a_static_site_actually_starts_and_serves(proj):
    import urllib.request
    open(os.path.join(proj, "index.html"), "w").write("<h1>preview works</h1>")
    workspace.start(proj)
    st = {}
    for _ in range(60):
        st = workspace.status(proj)
        if st["state"] in ("running", "failed"):
            break
        time.sleep(0.5)
    assert st["state"] == "running", st.get("error") or st.get("log")
    body = urllib.request.urlopen(st["url"], timeout=5).read()
    assert b"preview works" in body
    assert workspace.stop(proj) is True
    assert workspace.status(proj)["state"] == "idle"


def test_each_project_gets_its_own_port():
    root = tempfile.mkdtemp(prefix="hubws-")
    a, b = os.path.join(root, "a"), os.path.join(root, "b")
    for d in (a, b):
        os.makedirs(d)
        open(os.path.join(d, "index.html"), "w").write("x")
    try:
        workspace.start(a)
        workspace.start(b)
        assert workspace.status(a)["port"] != workspace.status(b)["port"]
    finally:
        workspace.stop(a)
        workspace.stop(b)
        shutil.rmtree(root, ignore_errors=True)


def test_status_of_an_unknown_folder_is_idle_not_an_error(proj):
    st = workspace.status(proj)
    assert st["running"] is False and st["state"] == "idle"


# --------------------------------------------------------------------------- #
# API surface
# --------------------------------------------------------------------------- #

@pytest.fixture
def client():
    app.app.config["TESTING"] = True
    with app.app.test_client() as c:
        yield c


def _auth():
    import config
    return {"X-Free-LLM-Hub": "dashboard",
            "X-Free-LLM-Hub-Token": config.ensure_control_token()}


def test_start_requires_a_project_dir(client):
    r = client.post("/api/workspace/start", json={}, headers=_auth())
    assert r.status_code in (400, 403)
    if r.status_code == 400:
        assert "project_dir" in r.get_json()["error"]


def test_a_missing_folder_is_rejected(client):
    r = client.post("/api/workspace/start",
                    json={"project_dir": "/definitely/not/here/at/all"}, headers=_auth())
    assert r.status_code in (400, 403)


def test_status_of_a_missing_folder_is_rejected(client):
    r = client.get("/api/workspace/status?project_dir=/nope/nope", headers=_auth())
    assert r.status_code == 400
