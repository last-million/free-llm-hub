r"""The tutorial page: last in the menu, centred, and actually a tutorial.

REQUESTED 2026-09-09: "i want also in tutorial to be last one in the menue and
in this tutorial page all should be centred and the video too, and also show
steps to start: adding providers and explaining each page what it do with
slides".

WHAT IT WAS. A nav item wedged between Hub controls and Activity -- so the
first-run reading order put a video tutorial before the page that actually
starts you -- and a body that was one full-width iframe and a single sentence.
Nothing told a new user what to do first, and nothing explained what any of the
twelve pages were for.

WHY THE LINKS CLICK THE SIDEBAR. cxShowView is a closure inside the SPA's IIFE
and is not exported. A link calling window.cxShowView would have been a link
that silently did nothing, so these find the matching sidebar item and click
it: one path, already wired, already handling history.
"""
import io
import re

SRC = io.open("templates/index.html", encoding="utf-8").read()
NAV = SRC[SRC.index('<nav class="cx-nav"'):SRC.index("</nav>")]


# --------------------------------------------------------------------------- #
# Last in the menu
# --------------------------------------------------------------------------- #

def test_the_tutorial_is_the_last_nav_item():
    others = [m.group(1) for m in re.finditer(r'data-view="(sec-[a-z-]+)"', NAV)]
    assert others[-1] == "sec-tutorial", "nav order ends with %r" % others[-1]


def test_it_is_no_longer_wedged_into_the_hub_group():
    hub = NAV[NAV.index('data-group="hub"'):NAV.index('data-group="generate"')]
    assert "sec-tutorial" not in hub


def test_every_other_nav_item_survived_the_move():
    """Moving one item must not drop another."""
    for view in ("sec-lifecycle", "sec-activity", "sec-chat", "sec-agent",
                 "sec-images", "sec-providers", "sec-subs", "sec-default",
                 "sec-quota", "sec-usage", "sec-tracking"):
        assert view in NAV, view


def test_the_nav_markup_is_still_balanced():
    """The tutorial anchor used to close the Hub group's body; moving it out
    without putting that </div> back would have swallowed the sidebar."""
    assert NAV.count("<div") == NAV.count("</div>") + NAV.count('<div class="cx-group-body"') * 0
    assert NAV.count('<div class="cx-group"') == NAV.count('<div class="cx-group-body"')


# --------------------------------------------------------------------------- #
# Centred
# --------------------------------------------------------------------------- #

def _rule(sel):
    m = re.search(re.escape(sel) + r"\s*\{([^}]*)\}", SRC)
    assert m, "no rule for %s" % sel
    return m.group(1).replace(" ", "")


def test_the_page_is_centred_and_narrow():
    """It is read as prose, not scanned as a dashboard, and a 1600px line of
    explanation is not read by anyone."""
    rule = _rule(".tut")
    assert "margin:0auto" in rule
    assert "max-width" in rule


def test_the_video_is_centred_and_responsive():
    rule = _rule(".tut-video")
    assert "aspect-ratio:16/9" in rule
    assert "width:100%" in rule
    assert 'width="560"' not in SRC.split('id="sec-tutorial"')[1][:2000], \
        "still a fixed-size iframe"


# --------------------------------------------------------------------------- #
# Steps to start
# --------------------------------------------------------------------------- #

def test_there_are_numbered_steps_that_start_with_providers():
    body = SRC[SRC.index('id="sec-tutorial"'):SRC.index("</section>", SRC.index('id="sec-tutorial"'))]
    assert "tut-steps" in body
    assert body.index("Providers") < body.index("Connect"), \
        "adding a key has to come before connecting a tool"
    assert "Test" in body


def test_the_steps_are_a_real_ordered_list():
    """Numbered by the list, not typed into the text, so they cannot get out of
    order when one is inserted."""
    assert "<ol class=\"tut-steps\">" in SRC
    assert "counter-increment:tut" in SRC.replace(" ", "")


# --------------------------------------------------------------------------- #
# Slides
# --------------------------------------------------------------------------- #

def test_there_is_a_slide_deck():
    assert 'id="tut-deck"' in SRC and 'id="tut-slides"' in SRC
    assert "TUT_PAGES" in SRC


def test_every_view_in_the_sidebar_is_explained():
    """A deck that skips pages is worse than no deck: the reader cannot tell
    whether a page is missing or simply not worth explaining."""
    described = set(re.findall(r'"(sec-[a-z-]+)"', SRC[SRC.index("var TUT_PAGES"):
                                                       SRC.index("function initTutorial")]))
    in_nav = set(re.findall(r'data-view="(sec-[a-z-]+)"', NAV)) - {"sec-tutorial"}
    missing = in_nav - described
    assert not missing, "not explained: %s" % sorted(missing)


def test_each_slide_says_when_to_use_the_page():
    block = SRC[SRC.index("var TUT_PAGES"):SRC.index("function initTutorial")]
    assert block.count("[") >= 12
    assert "tut-when" in SRC


def test_the_deck_is_keyboard_operable():
    assert "ArrowLeft" in SRC and "ArrowRight" in SRC
    assert 'role="tablist"' in SRC and 'aria-selected' in SRC


def test_only_one_slide_is_exposed_to_a_screen_reader():
    assert "aria-hidden" in SRC[SRC.index("function initTutorial"):
                                SRC.index("function initTutorial") + 3000]


def test_reduced_motion_is_respected():
    i = SRC.index(".tut-deck{")
    assert "@media (prefers-reduced-motion:reduce)" in SRC[i:i + 2500]


# --------------------------------------------------------------------------- #
# The links work
# --------------------------------------------------------------------------- #

def test_the_links_click_the_sidebar_rather_than_a_closure():
    """window.cxShowView does not exist -- it is a closure inside the SPA IIFE.
    A link calling it would silently do nothing."""
    i = SRC.index("data-tut-nav]")
    handler = SRC[i:i + 800]
    assert "cx-nav-item[data-view=" in handler
    assert "item.click()" in handler
    assert "window.cxShowView" not in SRC


def test_an_unknown_view_falls_back_to_the_href():
    i = SRC.index("data-tut-nav]")
    assert "if (!item) return;" in SRC[i:i + 800]


def test_the_deck_is_initialised_at_boot():
    assert "initTutorial();" in SRC
