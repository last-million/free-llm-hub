"""A quiet GitHub star ask in both first-run popups.

The repo lost its stars when it had to be deleted and re-created to clear a
stale contributor entry, so the two dialogs a user actually sees now carry a
link to star it.

Deliberately understated: in the what's-new dialog it is the LAST element,
under the release notes the user opened the dialog to read, and in the welcome
dialog it is one more pill in the social row beside YouTube and Discord. It
must never become the point of either dialog.
"""
import io
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = "https://github.com/last-million/free-llm-hub"


def _html():
    return io.open(os.path.join(ROOT, "templates", "index.html"),
                   encoding="utf-8").read()


def test_the_welcome_dialog_offers_a_star_alongside_the_socials():
    html = _html()
    assert 'class="cx-soc gh"' in html
    i = html.find('class="cx-soc gh"')
    row = html[max(0, i - 1500):i]
    assert 'cx-welcome-social' in row, "the pill must sit in the welcome social row"
    assert 'cx-soc yt' in row and 'cx-soc dc' in row, "should join YouTube/Discord"


def test_the_whats_new_dialog_ends_with_the_star_ask():
    """Last, so the release notes stay the point of the dialog."""
    html = _html()
    i = html.find('showWhatsNew')
    assert i != -1
    block = html[i:i + 4000]
    star = block.find('star-ask')
    body_append = block.find('body.appendChild(box)')
    assert star != -1, "no star ask in the what's-new dialog"
    assert star < body_append, "the star ask must be inside the notes box"
    notes = block.find('For more usage and quality')
    assert notes != -1 and notes < star, "the star ask must come AFTER the notes"


def test_both_links_point_at_the_real_repo_and_open_safely():
    html = _html()
    for m in re.finditer(r'<a[^>]*href="' + re.escape(REPO) + r'"[^>]*>', html):
        tag = m.group(0)
        assert 'target="_blank"' in tag, tag
        assert 'noopener' in tag and 'noreferrer' in tag, tag
    assert html.count('href="' + REPO + '"') >= 2, "expected a link in both dialogs"


def test_the_ask_is_styled_as_a_strip_not_a_banner():
    html = _html()
    assert ".star-ask{" in html
    assert ".cx-soc.gh{" in html


def test_it_says_why_rather_than_just_demanding_a_star():
    html = _html()
    assert "A star helps other people find it" in html
