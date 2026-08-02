"""Real URLs per view, and a quick chat that remembers.

Three reports, one turn:

  * "i dont want links /# but each page should have here url that i can open
    them in new tab" -- every view was a #fragment, which is not a location.
  * "why when i right click in menu items in sidebar i cant open in new tab" --
    the nav items were <button>, and no browser offers that menu on a button.
  * "even in #sec-chat we should have history and be able to continue
    conversation and load all previous conversation ... and we can remove
    history conversations" -- the quick chat lived in browser memory only.
"""
import json
import os
import re

import pytest

import app
import quick_history


@pytest.fixture
def client():
    app.app.config["TESTING"] = True
    with app.app.test_client() as c:
        yield c


def _dash(client, method, url, **kw):
    """A dashboard call, carrying the control token /api/* requires."""
    headers = {"X-Free-LLM-Hub": "dashboard",
               "X-Free-LLM-Hub-Token": app.config.ensure_control_token()}
    headers.update(kw.pop("headers", {}))
    return getattr(client, method)(url, headers=headers, **kw)


# --------------------------------------------------------------------------- #
# A URL per view
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("slug", ["hub", "activity", "chat", "agent", "images",
                                  "providers", "image-providers", "subscriptions",
                                  "routing", "quota", "usage", "tracking"])
def test_every_view_has_a_url_that_serves_the_app(client, slug):
    """Opening one in a new tab has to work on a cold request -- the server,
    not just the running page, must answer for it."""
    r = client.get("/" + slug)
    assert r.status_code == 200
    assert b"cx-sidebar" in r.data, "served something that is not the dashboard"


def test_a_conversation_url_survives_a_hard_refresh(client):
    """/agent/<id> and /chat/<id> are what make a refresh return to the same
    conversation instead of a blank page."""
    assert client.get("/agent/9f8e7d6c").status_code == 200
    assert client.get("/chat/c123abc").status_code == 200


def test_an_unknown_path_still_404s(client):
    """A catch-all would render the dashboard for every typo and hide real
    routing mistakes."""
    assert client.get("/definitely-not-a-view").status_code == 404


def test_the_server_and_the_page_agree_on_the_slugs():
    """Two lists that must never drift: the server decides which paths exist,
    the page decides which view each one shows. A slug in one and not the
    other is a URL that 404s or a page that opens the wrong view."""
    html = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "templates", "index.html"), encoding="utf-8").read()
    block = re.search(r"var VIEW_PATHS = \{(.*?)\};", html, re.S).group(1)
    page_slugs = set(re.findall(r"'sec-[a-z-]+'\s*:\s*'([a-z-]+)'", block))
    assert page_slugs == set(app._VIEW_SLUGS), (
        "page slugs %s != server slugs %s" % (sorted(page_slugs), sorted(app._VIEW_SLUGS)))


def test_the_sidebar_items_are_links_not_buttons():
    """Right-click > Open in new tab, middle-click and ctrl-click only exist on
    a real <a href>. They were buttons -- reported."""
    html = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "templates", "index.html"), encoding="utf-8").read()
    # Only items that GO somewhere. "Settings" is also a .cx-nav-item but it
    # opens a drawer over the current page -- it has no URL of its own, so a
    # button is the honest element for it.
    view_buttons = re.findall(r'<button class="cx-nav-item" data-view=', html)
    assert not view_buttons, "a nav item that navigates is still a button"
    links = re.findall(r'<a class="cx-nav-item" href="(/[a-z-]+)"', html)
    assert links, "the sidebar has no links at all"
    # A SUBSET, not an equality: a view can keep its URL while being taken out
    # of the menu. "Image providers" was removed from the sidebar on request,
    # and /image-providers still works so existing links do not break.
    assert set(links) <= {"/" + s for s in app._VIEW_SLUGS}, set(links)


def test_modified_clicks_are_left_to_the_browser():
    """Intercepting ctrl-click would break "open in a new tab" just as
    thoroughly as a <button> did."""
    html = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "templates", "index.html"), encoding="utf-8").read()
    assert "e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0" in html


def test_old_hash_links_are_not_abandoned():
    """Anyone who bookmarked #sec-providers gets moved to /providers rather
    than a blank dashboard."""
    html = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "templates", "index.html"), encoding="utf-8").read()
    assert "function legacyHash()" in html


# --------------------------------------------------------------------------- #
# Quick-chat history
# --------------------------------------------------------------------------- #

def test_a_turn_is_saved_listed_loaded_and_deleted(client):
    cid = quick_history.new_conversation_id()
    try:
        r = _dash(client, "post", "/api/chat/history/%s/turn" % cid,
                  json={"user": "what is 2+2", "assistant": "4", "model": "auto"})
        assert r.status_code == 200

        rows = _dash(client, "get", "/api/chat/history?limit=50").get_json()["conversations"]
        assert any(row["id"] == cid for row in rows), "saved turn is not in the list"

        conv = _dash(client, "get", "/api/chat/history/%s" % cid).get_json()
        assert [t["content"] for t in conv["turns"]] == ["what is 2+2", "4"]

        assert _dash(client, "delete", "/api/chat/history/%s" % cid).get_json()["deleted"] is True
        assert _dash(client, "get", "/api/chat/history/%s" % cid).status_code == 404
    finally:
        quick_history.delete_conversation(cid)


def test_a_half_turn_is_refused(client):
    """Storing a question with no answer (or the reverse) would put a
    conversation on screen that reads as if the model said nothing."""
    cid = quick_history.new_conversation_id()
    r = _dash(client, "post", "/api/chat/history/%s/turn" % cid, json={"user": "hi"})
    assert r.status_code == 400
    assert quick_history.load_conversation(cid) is None


def test_history_needs_the_control_token(client):
    """These are the user's conversations; /api/* is gated and this must be no
    different."""
    assert client.get("/api/chat/history").status_code == 401


def test_a_traversal_id_cannot_reach_outside_the_store(client):
    """The id becomes a filename. It is minted by the page, so anything else is
    a bug or an attack."""
    r = _dash(client, "post", "/api/chat/history/%s/turn" % "..%2f..%2fevil",
              json={"user": "a", "assistant": "b"})
    assert r.status_code in (200, 400, 404)
    assert not os.path.exists(os.path.join(os.path.expanduser("~"), "evil.json"))
