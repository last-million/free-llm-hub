"""The activity feed says WHERE a request came from, and for what project.

A session the dashboard's Build page starts and a CLI you run yourself in your
own terminal are the same program with the same User-Agent -- both show up as
"Codex" and were indistinguishable in the feed.

An agent CLI is an ordinary API client that forwards none of its environment,
so the URL is the only channel available: a hub-launched session is pointed at
<hub>/build/<session_id>, the prefix is stripped in WSGI before routing (so no
route is duplicated and no endpoint behaves differently), and the session id is
left on the environ for the activity row to resolve into a project name.
"""
from unittest import mock

import agentic_chat
import app


def test_the_prefix_is_stripped_before_routing():
    """Everything downstream must see the ordinary path, or every endpoint
    would need a second route."""
    seen = {}

    def inner(environ, start_response):
        seen["path"] = environ.get("PATH_INFO")
        seen["sid"] = environ.get("flh.build_session")
        start_response("200 OK", [])
        return [b""]

    mw = app._BuildPrefix(inner)
    mw({"PATH_INFO": "/build/abc123/v1/chat/completions"}, lambda *a, **k: None)
    assert seen["path"] == "/v1/chat/completions"
    assert seen["sid"] == "abc123"


def test_an_ordinary_path_is_untouched():
    seen = {}

    def inner(environ, start_response):
        seen["path"] = environ.get("PATH_INFO")
        seen["sid"] = environ.get("flh.build_session")
        start_response("200 OK", [])
        return [b""]

    app._BuildPrefix(inner)({"PATH_INFO": "/v1/chat/completions"}, lambda *a, **k: None)
    assert seen["path"] == "/v1/chat/completions"
    assert seen["sid"] is None


def test_a_bogus_prefix_is_not_mistaken_for_a_session():
    for path in ("/build", "/build/", "/buildx/y/v1/messages", "/v1/build/x"):
        seen = {}

        def inner(environ, start_response):
            seen["path"] = environ.get("PATH_INFO")
            start_response("200 OK", [])
            return [b""]

        app._BuildPrefix(inner)({"PATH_INFO": path}, lambda *a, **k: None)
        assert seen["path"] == path, path


def test_the_hub_url_carries_the_session_for_a_build_launch():
    with mock.patch.object(agentic_chat, "_port", return_value=8787):
        assert agentic_chat._hub_base_url() == "http://127.0.0.1:8787"
        assert agentic_chat._hub_base_url("sid42") == "http://127.0.0.1:8787/build/sid42"


def test_the_row_names_the_project_not_the_session_id():
    """A session id means nothing to a person; the folder name is the thing you
    recognise, and the feed column is narrow."""
    with app.app.test_request_context("/v1/chat/completions",
                                      environ_overrides={"flh.build_session": "s1"}), \
            mock.patch.object(app.agentic_chat, "get_session",
                              return_value={"project_dir": "C:/x/y/project-20260830-024030"}):
        assert app._build_project() == "project-20260830-024030"


def test_a_plain_cli_request_has_no_project_and_no_session():
    with app.app.test_request_context("/v1/chat/completions"):
        assert app._build_sid() is None
        assert app._build_project() is None


def test_an_unknown_session_does_not_break_the_row():
    with app.app.test_request_context("/v1/chat/completions",
                                      environ_overrides={"flh.build_session": "gone"}), \
            mock.patch.object(app.agentic_chat, "get_session", return_value=None):
        assert app._build_project() is None


def test_the_feed_renders_the_source_and_the_swarm_race():
    import io, os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html = io.open(os.path.join(root, "templates", "index.html"), encoding="utf-8").read()
    assert "af-src" in html and ".af-src.build{" in html
    assert "a.source === 'build'" in html
    assert "a.project" in html
    # the race is rendered by the pipeline chips the prose swarm already used
    assert "af-agent" in html


def test_the_activity_hook_is_still_the_registered_before_request():
    """CAUGHT IN PRODUCTION 2026-08-30: the two helpers above were inserted
    BETWEEN @app.before_request and _activity_before, so the decorator bound to
    _build_sid instead. The hub kept serving and every request still succeeded --
    the activity feed simply recorded nothing, silently, forever.

    Nothing about that is visible in a passing test suite or a 200 response,
    which is why it is asserted here directly."""
    import re, io, os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = io.open(os.path.join(root, "app.py"), encoding="utf-8").read()
    m = re.search(r"@app\.before_request\s*\ndef (\w+)\(\):", src)
    assert m, "no @app.before_request hook found at all"
    names = re.findall(r"@app\.before_request\s*\ndef (\w+)\(", src)
    assert "_activity_before" in names, \
        "@app.before_request is bound to %s, not _activity_before" % names
    # The helpers must be plain functions, never hooks.
    assert not re.search(r"@app\.before_request\s*\ndef _build_sid\(", src)
    assert not re.search(r"@app\.before_request\s*\ndef _build_project\(", src)


def test_a_request_actually_produces_an_activity_row():
    """The end-to-end guard: the decorator check above proves it is registered,
    this proves it still populates a row."""
    from unittest import mock as _m
    before = len(app._activity)
    with _m.patch.object(app, "_resolve_model", return_value=(None, "no models")):
        app.app.test_client().post("/v1/chat/completions",
                                   json={"model": "auto",
                                         "messages": [{"role": "user", "content": "hi"}]})
    assert len(app._activity) > before, "the request left no activity row"
    row = app._activity[0]
    assert row.get("source") == "cli"
    assert row.get("project") is None
