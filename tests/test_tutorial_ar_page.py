"""Tutorial AR: a sidebar entry + dashboard page embedding the Arabic-language
YouTube walkthrough (https://www.youtube.com/watch?v=T_HS_Yl77SA), plus a
README mention pointing GitHub visitors at the same video -- explicitly
flagged as Arabic in both places so a non-Arabic-speaking reader isn't
surprised.

Follows the existing view-URL pattern exactly (see
test_view_urls_and_chat_history.py): a slug in app.py's _VIEW_SLUGS, a
matching entry in the page's own VIEW_PATHS, and a real <a href> in the
sidebar -- that consistency is already covered there for every slug
including this one. This file covers what's specific to THIS page: the
video actually being embedded, and the CSP actually allowing it to render.
"""
import os
import re

import app

_TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "templates", "index.html")
_VIDEO_ID = "T_HS_Yl77SA"


def _html():
    return open(_TEMPLATE, encoding="utf-8").read()


def test_tutorial_ar_route_serves_the_dashboard():
    with app.app.test_client() as c:
        r = c.get("/tutorial-ar")
    assert r.status_code == 200
    assert b"cx-sidebar" in r.data


def test_the_page_embeds_the_actual_video_id():
    html = _html()
    assert "youtube.com/embed/" + _VIDEO_ID in html


def test_the_embed_is_marked_as_arabic_on_the_page():
    section = re.search(r'id="sec-tutorial".*?</div>\s*</section>', _html(), re.S)
    assert section, "no #sec-tutorial section found"
    assert "Arabic" in section.group(0)


def test_the_sidebar_link_points_at_the_route():
    html = _html()
    assert re.search(
        r'<a class="cx-nav-item" href="/tutorial-ar" data-view="sec-tutorial"',
        html), "sidebar link missing or pointing at the wrong view"


def test_csp_explicitly_allows_the_youtube_embed_host():
    with app.app.test_client() as c:
        csp = c.get("/health").headers.get("Content-Security-Policy", "")
    assert "https://www.youtube.com" in csp


def test_readme_links_the_tutorial_and_says_arabic():
    readme = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "README.md")
    text = open(readme, encoding="utf-8").read()
    assert "https://www.youtube.com/watch?v=" + _VIDEO_ID in text
    # Not just present anywhere in the file -- must say Arabic close enough to
    # the link that a reader can't miss the language before clicking play.
    idx = text.index("https://www.youtube.com/watch?v=" + _VIDEO_ID)
    window = text[max(0, idx - 120):idx + 40]
    assert "Arabic" in window
