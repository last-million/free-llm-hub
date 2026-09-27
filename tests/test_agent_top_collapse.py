"""The /agent view's top section folds away behind one icon button.

Owner request (2026-09-27): an icon button that collapses / shows the
permissions notice, the Session/History/Swarm tabs, the session bar and the
folder picker, with a one-line summary while collapsed; and the folder
heading removed. Static template checks, like the other UI tests.
"""
import os
import re

HTML = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "templates", "index.html"), encoding="utf-8").read()


def _tag(tag_id):
    m = re.search(r'<[a-z]+[^>]*\bid="%s"[^>]*>' % re.escape(tag_id), HTML, re.S)
    assert m, "no element with id=%s" % tag_id
    return m.group(0)


def _fn(name):
    body = HTML[HTML.index("function %s(" % name):]
    return body[:body.index("\n  }\n") + 4]


def test_the_toggle_is_an_icon_button_with_disclosure_semantics():
    btn = _tag("agent-collapse-btn")
    assert btn.startswith("<button") and 'type="button"' in btn
    assert 'aria-expanded="true"' in btn          # starts expanded
    assert 'aria-label="' in btn and 'title="Hide controls"' in btn
    controls = re.search(r'aria-controls="([^"]+)"', btn).group(1).split()
    for target in ("agent-warning", "agent-view-toggle", "agent-session-bar", "agent-setup"):
        assert target in controls
        assert 'id="%s"' % target in HTML, "aria-controls names a missing id: " + target
    # an inline SVG chevron, not an emoji or a text glyph
    after = HTML[HTML.index(btn):]
    inner = after[len(btn):after.index("</button>")]
    assert "<svg" in inner and 'aria-hidden="true"' in inner
    assert not re.search(r"[←-⇿▲-▿\U0001F300-\U0001FAFF]", inner)


def test_the_button_stays_outside_what_it_folds():
    """It lives in its own bar, a sibling of the notice and .section-body, so
    folding the section can never take the button with it."""
    bar = HTML.index('id="agent-collapse-bar"')
    assert HTML.index('id="agent-warning"') < bar < HTML.index('id="sec-agent"')
    assert HTML.index('id="agent-collapse-btn"') > bar


def test_the_folder_heading_is_gone_and_the_input_keeps_its_name():
    assert "Where should it work?" not in HTML
    assert 'aria-label="Project folder"' in _tag("agent-dir")
    assert 'aria-label="Workspace mode"' in _tag("agent-dir-mode")


def test_collapsed_shows_a_one_line_summary_that_keeps_the_permissions_flag():
    summary = HTML[HTML.index('id="agent-collapse-summary"'):]
    summary = summary[:summary.index("</p>")]
    assert "Full permissions" in summary
    fn = _fn("initAgentTopCollapse")
    assert "agent-session-info" in fn and "agent-quality" in fn
    assert "Show controls" in fn and "Hide controls" in fn
    assert "aria-expanded" in fn


def test_state_is_remembered_and_every_storage_access_is_guarded():
    fn = _fn("initAgentTopCollapse")
    uses = [m.start() for m in re.finditer(r"localStorage\.", fn)]
    assert len(uses) >= 2, "must both read and write the remembered state"
    for pos in uses:
        line_start = fn.rindex("\n", 0, pos)
        line = fn[line_start:fn.index("\n", pos)]
        assert "try {" in line and "catch(e)" in line, "unguarded storage access: " + line.strip()
    # wired into the page's init, after the topline exists
    assert re.search(r"initAgentLayout\(\);\s*initAgentTopCollapse\(\);", HTML)


def test_motion_is_short_and_respects_reduced_motion():
    css = HTML[HTML.index("Collapsible top of the agent view"):]
    css = css[:css.index("</style>")] if "</style>" in css[:20000] else css[:20000]
    durations = [float(d) for d in re.findall(r"(\.\d+)s ease", css[:css.index("@media (prefers-reduced-motion")])]
    assert durations and all(0.15 <= d <= 0.25 for d in durations), durations
    assert "@media (prefers-reduced-motion:reduce)" in css
    assert "reducedMotion()" in _fn("initAgentTopCollapse")
    # the phone target is the rendered-44px --tap token
    assert "width:var(--tap, 44px); height:var(--tap, 44px)" in css
    assert ".agent-collapse-btn:focus-visible{ outline:2px solid" in css
