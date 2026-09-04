"""Agent replies render as Markdown, not as a wall of asterisks.

ASKED 2026-09-04: "he write thinks in markdown but he should in /agent talk to
me in html or a code to auto convert to html the markdown to look beautiful".

/agent was setting `bubble.textContent = text`, so every heading, table and code
fence arrived as literal characters.

THE SECURITY POINT, and the reason this is hand-written rather than a library:
the text comes from a MODEL, so it is untrusted -- and worse, it routinely
contains material the agent just read out of a file. Every character is
HTML-escaped FIRST and the Markdown transforms run over the escaped text, so
model output has no path to injecting markup. Most Markdown libraries pass raw
HTML through by default, which would hand that capability to whatever the agent
happened to echo back.

Link hrefs are checked separately, because an escaped [x](javascript:alert(1))
is still a working link once it becomes an <a>.
"""
import re


def _template():
    with open("templates/index.html", encoding="utf-8") as f:
        return f.read()


def _fn(name, size=2600):
    """The body of one JS function, for asserting against.

    mdToHtml is long enough that a short window silently skipped the list
    handler and passed a test that was checking for it."""
    html = _template()
    i = html.index("function " + name + "(")
    return html[i:i + size]


# --------------------------------------------------------------------------- #
# It is wired in
# --------------------------------------------------------------------------- #

def test_the_renderer_exists():
    html = _template()
    for fn in ("mdEsc", "mdInline", "mdToHtml", "setMd", "mdSafeHref"):
        assert "function " + fn + "(" in html, fn


def test_agent_replies_use_it():
    html = _template()
    assert "if (role === 'agent' || role === 'assistant') setMd(b, text);" in html


def test_the_streamed_final_message_uses_it_too():
    """The reply that actually arrives during a /build turn."""
    html = _template()
    assert "setMd(ans, ev.text)" in html


def test_user_messages_are_still_plain_text():
    """What the user typed is shown back as typed; rendering it would change
    their own words."""
    html = _template()
    assert "else b.textContent = text;" in html


def test_a_renderer_failure_never_loses_the_answer():
    body = _fn("setMd")
    assert "catch" in body
    assert "el.textContent" in body


# --------------------------------------------------------------------------- #
# Escaping comes first
# --------------------------------------------------------------------------- #

def test_everything_is_escaped_before_any_transform():
    """The single property the whole design rests on."""
    body = _fn("mdToHtml")
    first = body.index("mdEsc(src)")
    assert first < 200, "escaping must be the first thing mdToHtml does"


def test_the_escaper_covers_every_dangerous_character():
    body = _fn("mdEsc")
    for ch in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
        assert ch in body, ch


def test_the_blockquote_rule_matches_the_escaped_form():
    """'>' is already '&gt;' by the time block parsing runs, so a rule written
    against a literal '>' would silently never fire."""
    body = _fn("mdToHtml")
    assert "&gt;" in body


# --------------------------------------------------------------------------- #
# Links
# --------------------------------------------------------------------------- #

def test_only_safe_schemes_become_links():
    body = _fn("mdSafeHref")
    assert "https?:" in body


def test_a_javascript_url_is_not_linkable():
    """An escaped [x](javascript:...) is still a working link once it is an <a>."""
    html = _template()
    i = html.index("function mdSafeHref(")
    rx = re.search(r"/\^\((.*?)\)\[\^\\s\]\*\$/i", html[i:i + 400])
    assert rx, "no scheme allowlist found"
    allowed = rx.group(1)
    assert "javascript" not in allowed and "data:" not in allowed


def test_links_open_safely():
    body = _fn("mdInline")
    assert 'rel="noopener noreferrer"' in body


# --------------------------------------------------------------------------- #
# What agents actually emit
# --------------------------------------------------------------------------- #

def test_fenced_code_is_handled():
    body = _fn("mdToHtml", 8000)
    assert "md-code" in body and "<code>" in body


def test_an_unclosed_fence_still_renders():
    """While a reply is streaming the closing fence has not arrived yet; showing
    the partial block as code is what it is going to become."""
    body = _fn("mdToHtml")
    assert "skip the closing fence if present" in body


def test_code_spans_are_protected_from_emphasis():
    """`a*b*c` must not become italic inside the code span."""
    body = _fn("mdInline")
    i = body.index("codes.push")
    j = body.index("<strong>")
    assert i < j, "code spans must be extracted before emphasis runs"


def test_tables_are_handled():
    body = _fn("mdToHtml", 8000)
    assert "md-table" in body and "<thead>" in body


def test_wide_tables_scroll_rather_than_stretch_the_page():
    html = _template()
    assert "md-tablewrap" in html
    assert ".md .md-tablewrap{overflow-x:auto" in html


def test_headings_lists_quotes_and_rules_are_handled():
    body = _fn("mdToHtml", 8000)
    for marker in ("md-h", "md-list", "md-quote", "md-hr"):
        assert marker in body, marker


def test_bold_italic_and_strikethrough():
    body = _fn("mdInline")
    assert "<strong>" in body and "<em>" in body and "<del>" in body


# --------------------------------------------------------------------------- #
# It is styled
# --------------------------------------------------------------------------- #

def test_the_rendered_output_has_styles():
    html = _template()
    for rule in (".md .md-p{", ".md .md-code{", ".md .md-table{", ".md .md-list{"):
        assert rule in html, rule


def test_code_blocks_scroll_instead_of_overflowing():
    html = _template()
    i = html.index(".md .md-code{")
    assert "overflow-x:auto" in html[i:i + 400]


def test_the_language_label_is_shown():
    html = _template()
    assert ".md .md-code[data-lang]::before" in html
