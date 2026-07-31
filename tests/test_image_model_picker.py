"""The image-generation model picker is CARDS, not a dropdown (2026-07-31).

A <select> could show one line per option and hid all of them behind a click,
so provider, in-image-text quality and configured-ness were invisible until
after picking. The replacement is a card grid — but a card grid is only an
upgrade if it keeps what the native control gave away for free: a real
radiogroup, roving tabindex, arrow-key movement, and a selected state that is
not signalled by colour alone. These tests pin exactly that.

Template-level assertions (no browser): the markup and the picker's inline JS.
"""
import re
from pathlib import Path

import app

INDEX_HTML = Path(app.__file__).resolve().parent / "templates" / "index.html"
HTML = INDEX_HTML.read_text(encoding="utf-8")


def test_the_dropdown_is_gone():
    assert 'id="image-model"' not in HTML, "the <select> model picker came back"
    assert "$('#image-model')" not in HTML, "dead reference to the removed <select>"


def test_picker_container_is_a_real_radiogroup():
    m = re.search(r'<div class="model-cards" id="image-model-cards"[^>]*>', HTML)
    assert m, "model-cards container missing"
    tag = m.group(0)
    assert 'role="radiogroup"' in tag
    assert 'aria-label="Model to generate with"' in tag


def test_cards_are_radios_with_roving_tabindex():
    # Each card is a radio...
    assert 'role="radio"' in HTML
    assert "aria-checked" in HTML
    # ...and only the selected one is tab-reachable (roving tabindex), which is
    # what makes a radiogroup one tab stop instead of N.
    assert re.search(r"c\.tabIndex\s*=\s*on\s*\?\s*0\s*:\s*-1", HTML), \
        "roving tabindex not implemented"


def test_arrow_keys_and_space_enter_select():
    for key in ("ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp", "Home", "End"):
        assert "'%s'" % key in HTML, "keyboard support for %s missing" % key
    assert "e.key === ' ' || e.key === 'Enter'" in HTML


def test_selection_is_not_colour_alone():
    """WCAG 1.4.1: a check icon accompanies the colour change — and it is an
    SVG, not an emoji (this project's icon rule)."""
    assert 'class="mc-check"' in HTML
    assert re.search(r'<svg class="mc-check"[^>]*aria-hidden="true"', HTML)
    check_css = re.search(r"\.model-card\[aria-checked=\"true\"\] \.mc-check\{([^}]*)\}", HTML)
    assert check_css and "opacity:1" in check_css.group(1)


def test_grid_is_responsive_without_its_own_breakpoint():
    css = re.search(r"\.model-cards\{([^}]*)\}", HTML)
    assert css, ".model-cards rule missing"
    body = css.group(1)
    assert "auto-fill" in body and "minmax(" in body, \
        "grid must reflow by intrinsic sizing, not fixed columns"


def test_focus_ring_and_reduced_motion_are_respected():
    assert ".model-card:focus-visible{outline:2px solid var(--accent)" in HTML
    rm = re.search(r"@media \(prefers-reduced-motion:reduce\)\{\s*\.model-card[^}]*\}", HTML)
    assert rm, "card transitions not disabled under prefers-reduced-motion"


def test_picker_exposes_the_select_value_contract():
    """Call sites read `.value` exactly as they did from the <select>, so the
    swap stayed local to the picker."""
    assert "Object.defineProperty(ModelPicker.prototype, 'value'" in HTML
    assert "var modelSel = imagePicker;" in HTML


def test_unknown_or_vanished_selection_falls_back_to_auto():
    """A model can disappear between loads (provider disabled, key removed);
    the picker must not keep pointing at it."""
    assert re.search(r"this\._value\s*=\s*known\s*\?\s*v\s*:\s*'auto'", HTML)
    assert "this.value = this._value;" in HTML, "re-validation after render missing"
