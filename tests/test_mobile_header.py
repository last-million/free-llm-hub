"""Phone header: one row, pills folded, not sticky, 44px targets.

Reported: at 390x844 the sticky top header (brand, subtitle, five status pills,
theme + settings) wrapped to ~157 rendered px and stayed pinned on scroll --
a fifth of the screen gone on every page.

Measured with headless Chromium against the live hub (template injected):
header 196.6 -> 80.2 CSS px (x0.8 zoom = ~157 -> ~64 rendered), every header
control 55 CSS px = 44 rendered, no horizontal scroll, desktop unchanged.
"""
import os
import re

HTML = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "templates", "index.html"), encoding="utf-8").read()


def _header_markup():
    return HTML[HTML.index("<header>"):HTML.index("</header>")]


def _phone_block():
    start = HTML.index("@media (max-width:640px){\n    :root{ --tap:")
    return HTML[start:HTML.index("\n  }\n", start)]


def test_actions_sit_outside_the_foldable_status_row():
    hdr = _header_markup()
    status = hdr[hdr.index('id="statusbar"'):hdr.index('class="hd-actions"')]
    actions = hdr[hdr.index('class="hd-actions"'):]
    assert 'class="pill' in status
    for bid in ("hd-status-toggle", "theme-toggle", "settings-drawer-open"):
        assert 'id="%s"' % bid in actions, bid + " must stay on the brand row"
        assert 'id="%s"' % bid not in status


def test_status_toggle_is_an_accessible_disclosure():
    hdr = _header_markup()
    tag = re.search(r'<button[^>]*id="hd-status-toggle"[^>]*>', hdr).group(0)
    assert 'type="button"' in tag
    assert 'aria-controls="statusbar"' in tag
    assert 'aria-expanded="false"' in tag
    assert "aria-label=" in tag


def test_phone_header_is_one_static_row_with_folded_pills():
    blk = _phone_block()
    assert "header{position:static}" in blk, "phone header must scroll away, not stick"
    assert ".statusbar{" in blk and "display:none" in blk.split(".statusbar{", 1)[1].split("}", 1)[0]
    assert "header.hd-status-open .statusbar{display:flex}" in blk
    assert "#hd-status-toggle{display:inline-flex}" in blk
    assert "white-space:nowrap" in blk and "text-overflow:ellipsis" in blk
    # Desktop keeps the sticky header and hides the phone-only opener.
    assert "position:sticky;top:0;z-index:40;" in HTML
    assert "#hd-status-toggle{display:none;position:relative}" in HTML


def test_touch_targets_are_44_rendered_px_under_the_root_zoom():
    blk = _phone_block()
    # html{zoom:var(--ui-scale)} shrinks everything; 44px must be divided back.
    assert "--tap:calc(44px / var(--ui-scale, .8))" in blk
    assert "min-width:var(--tap);min-height:var(--tap)" in blk
    assert ".cx-burger{width:var(--tap);height:var(--tap)}" in blk
    # The phone block must come AFTER the drawer block it overrides.
    assert HTML.index("body.cx-on header .logo{ margin-left:38px") < HTML.index(blk)
    assert HTML.index(".icon-btn.icon-only{padding") < HTML.index(blk)


def test_toggle_is_wired_and_summary_follows_every_pill_update():
    assert "initHeaderStatusToggle();" in HTML
    setpill = HTML[HTML.index("function setPill("):]
    setpill = setpill[:setpill.index("\n  }\n")]
    assert "syncHeaderStatusSummary();" in setpill
    # The auth pill is neutralised AFTER setPill; the summary must re-sync.
    assert "$('#pill-auth').classList.remove('ok'); syncHeaderStatusSummary();" in HTML
    fn = HTML[HTML.index("function initHeaderStatusToggle"):HTML.index("function syncHeaderStatusSummary")]
    assert "'Escape'" in fn and "aria-expanded" in fn
