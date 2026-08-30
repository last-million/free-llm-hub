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
