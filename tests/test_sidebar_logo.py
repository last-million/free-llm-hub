"""Sidebar menu + logo polish (user: "check css of side bar menu make it
perfect and also the logo issue").

Measured on the live dashboard with headless Chromium before the fix:
  * /favicon.ico answered 204 although static/calvoun.ico ships, so every page
    without a <link rel=icon> (JSON endpoints, setup page) showed a blank globe;
  * the header carried a generic lightning bolt next to the sidebar owl -- two
    different logos side by side;
  * collapsed rail (56px) / hidden strip (46px): logo + toggle (68px) did not
    fit, so the owl was pushed off the left edge;
  * a second `var CHECK_SVG` (no width/height) silently replaced the 12px one
    and every CLI card's "installed" badge rendered a ~90px check mark;
  * sidebar captions were 3.69:1 / 3.82:1 (under 4.5:1 AA).
"""
import os
import re

import pytest

import app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8").read()


@pytest.fixture
def client():
    app.app.config["TESTING"] = True
    with app.app.test_client() as c:
        yield c


def test_favicon_ico_serves_the_branded_icon(client):
    r = client.get("/favicon.ico")
    assert r.status_code == 200
    assert r.mimetype == "image/x-icon"
    with open(os.path.join(ROOT, "static", "calvoun.ico"), "rb") as f:
        assert r.data == f.read()


def test_head_links_existing_icon_assets():
    for href in re.findall(r'<link[^>]+rel="[^"]*icon[^"]*"[^>]+href="([^"]+)"', HTML):
        assert href.startswith("/static/"), href
        assert os.path.isfile(os.path.join(ROOT, href.lstrip("/"))), href


def test_header_uses_the_same_owl_as_the_sidebar():
    header = HTML[HTML.index("<header>"):HTML.index("</header>")]
    assert 'class="logo-mark" src="/static/calvoun-logo.webp"' in header
    assert "M13 2 3 14h7l-1 8 11-13h-7l1-7z" not in header  # the old bolt


def test_collapsed_brand_stacks_logo_above_toggle():
    assert re.search(r"body\.cx-rail \.cx-brand, body\.cx-hidden \.cx-brand\{ flex-direction:column;", HTML)


def test_check_svg_is_declared_once_and_sized():
    decls = re.findall(r"var CHECK_SVG = '(<svg[^']*)'", HTML)
    assert len(decls) == 1, "a second CHECK_SVG silently replaces the sized one"
    assert 'width="12"' in decls[0] and 'height="12"' in decls[0]


def test_settings_grid_track_cannot_be_blown_wide_by_content():
    # a bare `1fr` track has an auto minimum: one long line made /settings
    # 998px wide at a 390px viewport.
    assert ".sd-grid{display:grid;gap:12px;grid-template-columns:minmax(0,1fr)}" in HTML


def test_sidebar_captions_have_their_own_contrast_token():
    assert "--nav-caption:" in HTML
    assert re.search(r"\.cx-group-label\{[^}]*color:var\(--nav-caption\)", HTML)
