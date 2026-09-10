r"""The dashboard is one 12,000-line template. A typo in it is a blank page.

Every other test in this suite reads the template as TEXT -- it asserts that a
handler is wired, that an id exists, that a call carries the right body. None of
them would notice an unbalanced brace, because a string containing broken
JavaScript is still a perfectly good string.

That is not hypothetical: this file exists because two separate edits in one
session put a raw newline inside a JavaScript string literal (the tooling
collapsing a backslash-n), which every text assertion happily passed.

`node --check` parses without executing, so this costs nothing and needs no
browser. Skipped where node is missing rather than failed: not every machine
running this suite has it, and a missing checker is not a broken page.
"""
import io
import os
import re
import subprocess
import tempfile

import pytest


HTML = io.open("templates/index.html", encoding="utf-8").read()


def _node():
    from shutil import which
    return which("node")


def _blocks():
    """Inline scripts, with Jinja rendered away.

    `{{ control_token | tojson }}` is not JavaScript and never reaches a
    browser as itself -- substituting a literal is what the template engine
    does, so it is what the checker has to see."""
    src = re.sub(r"\{\{[^}]*\}\}", '"X"', HTML)
    src = re.sub(r"\{%[^%]*%\}", "", src)
    return [b for b in re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                                  src, re.S) if b.strip()]


@pytest.mark.skipif(not _node(), reason="node is not installed")
def test_every_inline_script_parses():
    tmp = tempfile.mkdtemp()
    failures = []
    for i, block in enumerate(_blocks()):
        path = os.path.join(tmp, "block%d.js" % i)
        io.open(path, "w", encoding="utf-8").write(block)
        out = subprocess.run([_node(), "--check", path],
                             capture_output=True, text=True)
        if out.returncode != 0:
            failures.append((i, (out.stderr or "").strip()[:400]))
    assert not failures, failures


def test_there_is_something_to_check():
    """A regex that silently matched nothing would make the test above pass
    forever."""
    assert len(_blocks()) >= 3


def test_no_string_literal_holds_a_raw_newline():
    """The specific failure this file was written after: tooling turning a
    backslash-n inside a quoted string into an actual line break."""
    bad = []
    for n, line in enumerate(HTML.splitlines(), 1):
        if line.count("'") % 2 and "//" not in line and "/*" not in line:
            stripped = re.sub(r"\.", "", line)
            if stripped.count("'") % 2 and re.search(r"=\s*'[^']*$", stripped):
                bad.append((n, line.strip()[:90]))
    assert not bad, bad


# --------------------------------------------------------------------------- #
# Light is the default, and everything that paints has to agree
# --------------------------------------------------------------------------- #

def test_the_page_opens_light():
    """REQUESTED: "make the app default run in light mode theme not in dark
    mode, but he can always switch to dark mode of course". A first visit, a new
    browser and a private window all open light; the pre-paint script switches
    to dark only for someone who chose it."""
    assert '<html lang="en" data-theme="light">' in HTML
    i = HTML.index("localStorage.getItem('flh.theme')")
    boot = HTML[i:i + 260]
    assert "==='dark'" in boot, "the boot script no longer opts IN to dark"


def test_the_browser_chrome_matches_the_page():
    """theme-color was hardcoded near-black, so a phone painted a dark frame
    around a white page."""
    i = HTML.index('name="theme-color"')
    assert "#F5F6F8" in HTML[i:i + 80]


def test_switching_moves_the_chrome_too():
    body = HTML[HTML.index("function applyTheme("):]
    body = body[:body.index(chr(10) + "  function initThemeToggle(")]
    assert 'meta[name="theme-color"]' in body
    assert "#0C0F14" in body and "#F5F6F8" in body


def test_dark_is_still_one_click_away():
    assert "Switch to dark theme" in HTML
    assert "id=\"theme-toggle\"" in HTML
