"""Mobile/tablet sidebar drawer: full sidebar, a real X, tap-outside to close.

Reported: the hamburger opened a "fucked" sidebar showing only icons, no
close button, and tapping outside the drawer did nothing.

Root cause: the sidebar's desktop collapse states (rail = icons only, hidden =
a thin strip) are stored in localStorage and applied unconditionally, with no
guard for viewport width. A phone opening the drawer while a stored preference
said "rail" got a 56px icon strip instead of the drawer it asked for.
"""
import os

HTML = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "templates", "index.html"), encoding="utf-8").read()


def _block(start_marker, end_marker):
    start = HTML.index(start_marker)
    end = HTML.index(end_marker, start)
    return HTML[start:end]


def test_rail_and_hidden_narrowing_only_applies_above_the_drawer_breakpoint():
    """The actual bug: these rules had no width guard at all, so a desktop
    preference stored in localStorage rendered as icons-only inside the mobile
    drawer too."""
    collapse_block = _block(
        "body.cx-rail{ padding-left:var(--sidebar-rail); }",
        "/@media (min-width:1025px) -- end of rail/hidden collapse rules")
    assert "@media (min-width:1025px)" in HTML[:HTML.index(collapse_block)][-200:], (
        "the collapse rules are not wrapped in a desktop-only media query")
    # Sanity: the actual narrowing declarations are IN that guarded region.
    assert "width:var(--sidebar-rail)" in collapse_block
    assert "display:none" in collapse_block  # the label-hiding rule


def test_the_drawer_breakpoint_forces_full_width_regardless_of_stored_mode():
    mobile_block = _block("@media (max-width:1024px){", "}\n  @media (prefers-reduced-motion")
    assert "min(var(--sidebar-w), 84vw)" in mobile_block, (
        "the mobile drawer must render the FULL sidebar width, not a collapsed one")


def test_a_close_button_exists_and_is_mobile_only():
    assert 'id="cx-drawer-close"' in HTML
    assert ".cx-drawer-close{ display:none; }" in HTML, "must be hidden on desktop by default"
    mobile_block = _block("@media (max-width:1024px){", "}\n  @media (prefers-reduced-motion")
    assert ".cx-drawer-close{ display:inline-grid" in mobile_block, (
        "the X must be shown specifically inside the mobile breakpoint")


def test_the_rail_cycle_button_is_hidden_in_the_mobile_drawer():
    """"how narrow should this be" is not a question a full-screen drawer has;
    showing both that control and a plain X would be confusing."""
    mobile_block = _block("@media (max-width:1024px){", "}\n  @media (prefers-reduced-motion")
    assert "#cx-sidebar-toggle{ display:none; }" in mobile_block


def test_click_outside_the_drawer_closes_it():
    assert "addEventListener('pointerdown'" in HTML
    handler = _block("document.addEventListener('pointerdown'", "});")
    assert "cx-drawer-open" in handler
    assert "sidebarEl.contains(e.target)" in handler, "must not close on a click INSIDE the drawer"


def test_escape_closes_the_drawer():
    handler = _block("document.addEventListener('keydown'", "});")
    assert "Escape" in handler and "cx-drawer-open" in handler


def test_picking_a_destination_closes_the_drawer():
    """Otherwise the scrim and the (now stale) menu sit on top of the page you
    just navigated to."""
    assert "closest('.cx-nav-item')) setDrawerOpen(false)" in HTML


def test_the_close_button_is_wired_to_the_same_close_function():
    idx = HTML.index("var drawerClose = document.getElementById('cx-drawer-close');")
    assert "setDrawerOpen(false)" in HTML[idx:idx + 200]
