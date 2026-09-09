r"""Chrome that followed you down the page, on the one view with no room for it.

REPORTED 2026-09-09: "remove the footer from the page /agent please, and
notices in top -- sometimes there is sticky notices in top that eat from the
screen height in /agent ... and when we scroll in other pages those notices
stay sticky and it's ugly".

Two separate complaints about the same element.

THE QUOTA BANNER WAS position:sticky;top:0;z-index:50. So it rode the top of
every page through every scroll: a permanent strip of screen height spent on a
sentence the reader finished reading on arrival, pinned over content it has
nothing to do with. It says "a provider ran out of free quota" -- worth seeing
when you arrive, not worth following you around. It scrolls away now. The
header keeps its own stickiness, and moves to top:0 because it no longer has to
reserve space under a banner that no longer sticks.

THE AGENT VIEW is a full-height working surface -- terminal above preview --
so every strip of chrome comes straight out of the space being worked in. The
header was already hidden there for exactly that reason (the comment in cxShow
says so). The banner and the site footer were not, and both are read-once
information with a permanent cost on that view.
"""
import re

SRC = open("templates/index.html", encoding="utf-8").read()


def _rule(selector):
    """The declaration block for a selector, as written in the stylesheet."""
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", SRC)
    assert m, "no rule found for %r" % selector
    return m.group(1)


# --------------------------------------------------------------------------- #
# The banner does not follow the reader
# --------------------------------------------------------------------------- #

def test_the_quota_banner_is_not_sticky():
    assert "position:static" in _rule("#exhaust-banner").replace(" ", "")


def test_the_header_still_is():
    """Navigation staying put is the point of a sticky header; the complaint
    was about the notice, not the nav."""
    assert "position:sticky" in _rule("header").replace(" ", "")


def test_the_header_no_longer_reserves_space_under_the_banner():
    """top:var(--exhaust-h) offset the header by the banner's height. With the
    banner scrolling away that would leave a page-wide gap whenever it showed."""
    body = _rule("header").replace(" ", "")
    assert "top:0" in body
    assert "--exhaust-h" not in body


# --------------------------------------------------------------------------- #
# ...and neither of them costs the agent view any height
# --------------------------------------------------------------------------- #

def test_the_banner_is_gone_from_the_agent_view():
    assert "display:none" in _rule("body.cx-agent-view #exhaust-banner").replace(" ", "")


def test_the_footer_is_gone_from_the_agent_view():
    assert "display:none" in _rule("body.cx-agent-view .site-footer").replace(" ", "")


def test_the_header_is_still_gone_from_the_agent_view():
    """This one was already right and must stay right."""
    assert "display:none" in _rule("body.cx-agent-view > header").replace(" ", "")


# --------------------------------------------------------------------------- #
# ...without removing them from anywhere else
# --------------------------------------------------------------------------- #

def test_the_footer_still_exists_for_every_other_view():
    """It carries the licence notice; hiding it globally would be a different
    change from the one that was asked for."""
    assert '<footer class="site-footer">' in SRC
    assert "PolyForm Noncommercial" in SRC


def test_the_banner_still_exists_and_still_shows():
    assert '<div id="exhaust-banner"></div>' in SRC
    assert "#exhaust-banner.show" in SRC


def test_the_agent_view_class_is_what_gates_it():
    """cxShow toggles this on the body; if that ever stops happening these
    rules silently do nothing."""
    assert "classList.toggle('cx-agent-view', view === 'sec-agent')" in SRC
