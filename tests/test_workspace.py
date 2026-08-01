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


def test_the_preview_iframe_is_allowed_by_the_csp(client):
    """The preview frames the user's own project on a loopback port we allocate
    at run time. Without an explicit frame-src the policy falls back to
    default-src 'none' and the browser refuses the frame outright — measured:
    "Refused to frame 'http://127.0.0.1:5801/'"."""
    csp = client.get("/health").headers.get("Content-Security-Policy", "")
    assert "frame-src" in csp, "no frame-src: the preview iframe cannot render"
    assert "http://127.0.0.1:*" in csp


def test_the_csp_still_refuses_remote_frames():
    """Loopback only — this must not become a general framing permission."""
    import re as _re
    with app.app.test_client() as c:
        csp = c.get("/health").headers.get("Content-Security-Policy", "")
    frame_src = _re.search(r"frame-src ([^;]+)", csp).group(1)
    for token in frame_src.split():
        assert token.startswith("http://127.0.0.1") or token.startswith("http://localhost"), token


def test_stop_stays_reachable_when_the_master_switch_is_off(client, monkeypatch):
    """A kill switch must still be able to kill: flipping the agentic-chat flag
    off while a preview server runs must not strand that process."""
    monkeypatch.setattr(app.agentic_chat, "master_enabled", lambda: False)
    r = client.post("/api/workspace/stop",
                    json={"project_dir": tempfile.gettempdir()}, headers=_auth())
    assert r.status_code == 200


def test_status_is_gated_when_the_master_switch_is_off(client, monkeypatch):
    """It reads, and its 400-on-missing-directory is an existence oracle."""
    monkeypatch.setattr(app.agentic_chat, "master_enabled", lambda: False)
    r = client.get("/api/workspace/status?project_dir=" + tempfile.gettempdir(),
                   headers=_auth())
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# Folder browsing.
#
# A browser cannot hand us an absolute local path — not from a drop, not from a
# file input, by design. The hub runs on the same machine, so browsing happens
# server-side and the user clicks down to the folder.
# --------------------------------------------------------------------------- #

def test_browse_lists_subfolders(proj):
    os.makedirs(os.path.join(proj, "alpha"))
    os.makedirs(os.path.join(proj, "beta"))
    open(os.path.join(proj, "a-file.txt"), "w").write("x")
    r = workspace.list_dirs(proj)
    names = [d["name"] for d in r["dirs"]]
    assert names == ["alpha", "beta"], "files must not be listed, and order is by name"
    assert r["path"] == os.path.abspath(proj)
    assert r["parent"]


def test_browse_skips_hidden_and_system_entries(proj):
    for name in (".git", ".venv", "$RECYCLE.BIN", "real"):
        os.makedirs(os.path.join(proj, name))
    names = [d["name"] for d in workspace.list_dirs(proj)["dirs"]]
    assert names == ["real"]


def test_browse_reports_whether_the_folder_is_runnable(proj):
    assert workspace.list_dirs(proj)["runnable"] is False
    open(os.path.join(proj, "index.html"), "w").write("<h1>x</h1>")
    assert workspace.list_dirs(proj)["runnable"] is True


def test_browse_is_capped(proj):
    """node_modules alone can hold thousands of entries."""
    for i in range(520):
        os.makedirs(os.path.join(proj, "d%03d" % i))
    assert len(workspace.list_dirs(proj)["dirs"]) <= 500


def test_browse_rejects_a_non_directory(proj):
    f = os.path.join(proj, "file.txt")
    open(f, "w").write("x")
    with pytest.raises(workspace.WorkspaceError):
        workspace.list_dirs(f)


def test_browse_defaults_to_home():
    r = workspace.list_dirs(None)
    assert r["path"] == os.path.abspath(os.path.expanduser("~"))


def test_browse_route_is_gated(client, monkeypatch):
    """It is a filesystem listing endpoint — it must not be reachable just
    because the port is open."""
    monkeypatch.setattr(app.agentic_chat, "master_enabled", lambda: False)
    r = client.get("/api/workspace/browse", headers=_auth())
    assert r.status_code == 403


def test_browse_route_returns_a_listing(client):
    r = client.get("/api/workspace/browse?path=" + tempfile.gettempdir(),
                   headers=_auth())
    assert r.status_code in (200, 403)
    if r.status_code == 200:
        assert "dirs" in r.get_json()


# --------------------------------------------------------------------------- #
# The file tree and code viewer.
#
# This decides which of the user's files a browser can read, so the containment
# check is the part that matters.
# --------------------------------------------------------------------------- #

def test_tree_lists_dirs_before_files(proj):
    os.makedirs(os.path.join(proj, "src"))
    open(os.path.join(proj, "index.html"), "w").write("x")
    names = [e["name"] for e in workspace.tree(proj)["entries"]]
    assert names == ["src", "index.html"]


def test_tree_skips_the_noise(proj):
    for d in ("node_modules", ".git", "__pycache__", "dist", "keep"):
        os.makedirs(os.path.join(proj, d))
    names = [e["name"] for e in workspace.tree(proj)["entries"]]
    assert names == ["keep"]


def test_tree_descends(proj):
    os.makedirs(os.path.join(proj, "src"))
    open(os.path.join(proj, "src", "app.js"), "w").write("const x = 1;")
    sub = workspace.tree(proj, "src")
    assert [e["name"] for e in sub["entries"]] == ["app.js"]
    assert sub["rel"] == "src"


def test_read_file_returns_text(proj):
    open(os.path.join(proj, "a.js"), "w").write("const x = 1;")
    r = workspace.read_file(proj, "a.js")
    assert r["text"] == "const x = 1;"
    assert r["lang"] == "js"


def test_binary_files_are_flagged_not_dumped(proj):
    with open(os.path.join(proj, "b.dat"), "wb") as fh:
        fh.write(bytes([0, 1, 2, 3]) * 100)
    r = workspace.read_file(proj, "b.dat")
    assert r["binary"] is True and r["text"] is None


def test_oversized_files_are_refused(proj, monkeypatch):
    monkeypatch.setattr(workspace, "MAX_FILE_BYTES", 10)
    open(os.path.join(proj, "big.txt"), "w").write("x" * 100)
    r = workspace.read_file(proj, "big.txt")
    assert r["too_big"] is True and r["text"] is None


@pytest.mark.parametrize("evil", [
    "../../../etc/passwd",
    os.path.join("..", "..", "secrets.txt"),
    "sub/../../outside.txt",
])
def test_path_traversal_is_refused(proj, evil):
    """The check compares realpaths — a prefix test is beaten by '..' and by a
    symlink pointing out of the project."""
    with pytest.raises(workspace.WorkspaceError):
        workspace.read_file(proj, evil)


def test_a_symlink_out_of_the_project_is_refused(proj):
    outside = tempfile.mkdtemp(prefix="hubws-out-")
    try:
        open(os.path.join(outside, "secret.txt"), "w").write("nope")
        link = os.path.join(proj, "escape")
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError):
            pytest.skip("symlinks not permitted on this machine")
        with pytest.raises(workspace.WorkspaceError):
            workspace.read_file(proj, "escape/secret.txt")
    finally:
        shutil.rmtree(outside, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Idle previews
# --------------------------------------------------------------------------- #

def test_running_reports_idle_countdown(proj):
    open(os.path.join(proj, "index.html"), "w").write("x")
    workspace.start(proj)
    rows = [r for r in workspace.running() if r["project_dir"] == os.path.abspath(proj)]
    assert rows and rows[0]["idle_stops_in"] > 0


def test_status_counts_as_watching(proj):
    """The dashboard polls status while the pane is open, so that is what keeps
    a preview alive — the reaper must only fire once the user has moved on."""
    open(os.path.join(proj, "index.html"), "w").write("x")
    workspace.start(proj)
    key = os.path.abspath(proj)
    workspace._procs[key].touched_at = time.time() - workspace.IDLE_TIMEOUT - 5
    workspace.status(proj)                      # someone looked
    assert workspace.reap_idle() == []


def test_an_unwatched_preview_is_stopped(proj):
    open(os.path.join(proj, "index.html"), "w").write("x")
    workspace.start(proj)
    key = os.path.abspath(proj)
    workspace._procs[key].touched_at = time.time() - workspace.IDLE_TIMEOUT - 5
    assert workspace.reap_idle() == [key]
    assert workspace.status(proj)["state"] == "idle"


def test_reaping_is_a_pause_not_a_ban(proj):
    """Run must bring it straight back."""
    open(os.path.join(proj, "index.html"), "w").write("x")
    workspace.start(proj)
    key = os.path.abspath(proj)
    workspace._procs[key].touched_at = time.time() - workspace.IDLE_TIMEOUT - 5
    workspace.reap_idle()
    workspace.start(proj)
    assert workspace.status(proj)["state"] in ("installing", "starting", "running")
