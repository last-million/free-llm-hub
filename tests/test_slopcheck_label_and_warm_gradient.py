"""slopcheck false positives (2026-10-30 review, core-7 / core-8).

- A "Company Name" form label or table heading is not a "placeholder left in"
  (HIGH) finding; "Company Name" standing as the company's name still is.
- check_design's default_gradient fires only for a gradient in the violet /
  blue / pink band: a warm gradient with a plum accent, or a plum palette with
  "no gradients", is not the default AI hero.
"""
import pytest

import slopcheck as S

HEAD = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>Contact</title></head><body>')
TAIL = "</body></html>"


def _placeholders(html):
    return [f for f in S.check_files([("contact.html", HEAD + html + TAIL)])
            if f["rule"] == "placeholder"]


def _rules(findings):
    return {f["rule"] for f in findings}


# --------------------------------------------------------------------------- #
# core-7: "Company Name" as a field label
# --------------------------------------------------------------------------- #

def test_a_company_name_form_label_is_not_a_placeholder():
    assert _placeholders('<form><label for="c">Company Name</label>'
                         '<input id="c" name="company"></form>') == []


def test_a_label_with_nested_markup_or_a_colon_is_not_a_placeholder():
    assert _placeholders('<form><label for="c"><span>Company Name</span> *</label>'
                         '<input id="c"></form>') == []
    assert _placeholders('<form><p>Company Name: <input name="company"></p></form>') == []
    assert _placeholders("<table><tr><th>Company Name</th><th>Plan</th></tr>"
                         "<tr><td>Fixli</td><td>Pro</td></tr></table>") == []
    assert _placeholders('<form><fieldset><legend>Company Name</legend>'
                         '<input name="company"></fieldset></form>') == []


def test_a_markdown_table_heading_is_not_a_placeholder():
    md = "# Clients\n\n| Company Name | Plan |\n| --- | --- |\n| Fixli | Pro |\n"
    assert [f for f in S.check_files([("README.md", md)]) if f["rule"] == "placeholder"] == []


def test_company_name_standing_as_the_name_is_still_a_placeholder():
    for html in ('<header><a class="logo" href="/">Company Name</a></header><main><p>We fix roofs.</p></main>',
                 "<footer><p>Company Name, 12 rue des Lilas, Lyon</p></footer>"):
        found = _placeholders(html)
        assert [f["severity"] for f in found] == ["high"], html
        assert "Your Company" in found[0]["why"]
    # a label elsewhere on the page does not hide the logo placeholder
    found = _placeholders('<header><a class="logo">Company Name</a></header>'
                          '<form><label>Company Name</label><input></form>')
    assert len(found) == 1


def test_your_company_name_is_still_caught_even_in_a_label():
    # "Your Company Name" is a template string, not an ordinary field label.
    assert len(_placeholders("<footer>&copy; 2026 Your Company Name</footer>")) == 1


# --------------------------------------------------------------------------- #
# core-8: default_gradient in a design spec
# --------------------------------------------------------------------------- #

def test_a_warm_gradient_with_a_plum_accent_is_not_the_default_gradient():
    spec = ("Hero: a warm gradient from sand #e8dcc4 to terracotta #c4622d with plum #5b2a4a "
            "headings, set in Fraunces over Work Sans.")
    assert "default_gradient" not in _rules(S.check_design(spec, request="landing page for a ceramics studio"))
    worded = "Hero: a sunset gradient from coral to plum behind the headline."
    assert "default_gradient" not in _rules(S.check_design(worded))


def test_a_plum_palette_with_no_gradients_is_not_the_default_gradient():
    spec = "Palette: plum #4a1942 on cream #f6efe3, no gradients."
    assert "default_gradient" not in _rules(S.check_design(spec))


def test_the_real_default_gradient_is_still_flagged():
    for spec in ("Hero with a purple to blue gradient behind the headline.",
                 "Hero: a purple gradient behind the headline.",
                 "Hero: a gradient from indigo #4f46e5 to pink #ec4899.",
                 "Hero: an indigo-to-violet gradient with no orange anywhere.",
                 "Hero: a purple gradient behind the headline, no gradients on the buttons."):
        found = {f["rule"]: f["severity"] for f in S.check_design(spec)}
        assert found.get("default_gradient") == "high", spec


def test_a_colour_outside_the_gradient_does_not_excuse_it():
    # An accent, a banner or a footer colour is not one of the gradient's stops
    # (the CSS rule _ai_gradient_stops looks only at the stops too).
    for spec in ("Hero: a vibrant purple-to-indigo gradient with gold accents on the buttons.",
                 "Hero: a purple to blue gradient behind the headline and a red error banner.",
                 "Hero: violet #7c3aed to blue #2563eb gradient with an amber #f59e0b CTA.",
                 "Hero: a purple gradient, forest green footer.",
                 "Hero: a lavender gradient with a teal accent."):
        found = {f["rule"]: f["severity"] for f in S.check_design(spec)}
        assert found.get("default_gradient") == "high", spec


def test_a_warm_stop_inside_the_gradient_still_excuses_it():
    for spec in ("Hero: a gradient of coral and purple.",
                 "Avoid purple, blue and pink gradients.",
                 "Hero: a warm gradient with plum headings."):
        assert "default_gradient" not in _rules(S.check_design(spec)), spec


def test_unclosed_option_or_dt_does_not_hide_a_later_placeholder():
    # <option> and <dt> may legally omit their end tag; they must not cover the logo.
    for html in ('<select><option>A<option>B</select>'
                 '<header><a class="logo">Company Name</a></header>',
                 '<dl><dt>Plan<dd>Pro</dl><header><a class="logo">Company Name</a></header>'):
        assert len(_placeholders(html)) == 1, html
    # but the text of an unclosed <dt> itself is a label
    assert _placeholders("<dl><dt>Company Name<dd>Fixli</dl>") == []


def test_emphasis_or_a_heading_with_a_colon_is_still_a_placeholder():
    for md in ("# **Company Name**\n\nWe fix roofs.\n",
               "Welcome to *Company Name*, the roofers.\n"):
        found = [f for f in S.check_files([("README.md", md)]) if f["rule"] == "placeholder"]
        assert len(found) == 1, md
    assert len(_placeholders("<h1>Company Name: we fix roofs</h1>")) == 1
    # a colon followed by a field or a blank is still a label
    assert _placeholders('<form><p>Company Name: <input name="c"></p></form>') == []
    assert [f for f in S.check_files([("form.md", "Company Name: ________\n")])
            if f["rule"] == "placeholder"] == []


def test_the_same_warm_palette_as_css_was_already_silent():
    css = ".hero{background:linear-gradient(90deg,#e8dcc4,#c4622d)} h1{color:#5b2a4a}"
    assert "ai_gradient" not in _rules(S.check_files({"s.css": css}))


@pytest.mark.parametrize("text", [
    "Hero: a purple, blue and pink gradient.",
    "Hero: a violet, blue and pink gradient.",
    "Hero: a deep purple (#6d28d9) to blue gradient.",
    "Hero: a warm purple gradient.",
    "Hero: a warm purple-to-blue gradient.",
    "Hero: a warm violet-to-indigo gradient.",
    "Hero: an earthy purple gradient.",
])
def test_a_listed_or_warm_described_violet_gradient_is_still_the_default_one(text):
    """A comma inside the list of stops and a bracketed hex do not end the
    gradient's clause, and "warm" is an adjective, not a stop."""
    assert S._design_gradient(text, "")


@pytest.mark.parametrize("text", [
    "Hero: a warm gradient with a plum accent.",
    "Hero: a terracotta to sand gradient, with a violet logo.",
    "Hero: a soft gradient behind the headline.",
])
def test_a_gradient_whose_own_stops_are_warm_is_not_flagged(text):
    assert not S._design_gradient(text, "")


def test_a_label_that_starts_with_a_checkbox_an_icon_or_a_break_is_not_a_placeholder():
    assert _placeholders('<form><label><input type="checkbox" name="c"> Company Name</label></form>') == []
    assert _placeholders('<form><label><svg viewBox="0 0 8 8"><path d="M0 0h8v8z"/></svg> '
                         'Company Name</label><input></form>') == []
    assert _placeholders("<table><tr><th>Billing<br>Company Name</th></tr>"
                         "<tr><td>Fixli</td></tr></table>") == []
    md = "# Clients\n\n| **Company Name** | Plan |\n| --- | --- |\n| Fixli | Pro |\n"
    assert [f for f in S.check_files([("README.md", md)]) if f["rule"] == "placeholder"] == []


def test_an_icon_before_a_logo_name_does_not_hide_the_placeholder():
    found = _placeholders('<header><a class="logo" href="/"><svg viewBox="0 0 8 8">'
                          '<path d="M0 0h8v8z"/></svg> Company Name</a></header>')
    assert [f["severity"] for f in found] == ["high"]

