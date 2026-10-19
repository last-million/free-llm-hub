"""The goal lives in the FIRST bar of the Build page, open or folded.

Owner, 2026-10-19: "the goal bar should be included in the first bar ... it
takes space too", then "the goal bar must be inside the first bar ... when the
first bar is collapsed I see the goal too ... UI/UX perfect and responsive".

The goal element is moved once into #agent-collapse-bar (the line with the
chevron) and stays there in both states, so it never jumps between rows; its
list and forms open as a panel floating under it instead of pushing the chat
down. Static checks of the template; the layout was checked in a real browser
at 1440, 1280 and 390 px wide.
"""
import os
import re

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(HERE, "templates", "index.html"), encoding="utf-8").read()


def _fn(name):
    i = HTML.index("function %s(" % name)
    return HTML[i:HTML.index("\n    }", i)]


def _rule(selector):
    i = HTML.index(selector + "{")
    return HTML[i:HTML.index("}", i)]


def test_the_goal_is_placed_in_the_first_bar_and_stays_there():
    place = _fn("placeGoal")
    assert "document.getElementById('agent-tasks')" in place
    assert "document.getElementById('agent-collapse-bar')" in place
    assert "host.insertBefore(t, btn)" in place
    assert "agent-session-bar" not in place, "it no longer moves between bars"
    paint = _fn("paint")
    assert "placeGoal();" in paint
    assert paint.index("placeGoal();") < paint.index("sec.classList.toggle('agent-top-collapsed', on);")


def test_reading_the_goal_never_folds_the_top():
    i = HTML.index("if (bar) bar.addEventListener('click'")
    handler = HTML[i:HTML.index("});", i)]
    assert "closest('#agent-tasks')" in handler


def test_it_is_one_compact_part_of_the_bar_hidden_without_a_session():
    chip = _rule(".agent-collapse-bar > .agent-tasks")
    for want in ("position:relative", "flex:0 1 auto", "min-width:0", "max-width:460px",
                 "background:var(--surface-2)"):
        assert want in chip, want
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", chip), "theme tokens only"
    assert "#agent-section:not(.agent-has-session) .agent-collapse-bar > .agent-tasks{ display:none; }" in HTML
    head = _rule(".agent-collapse-bar > .agent-tasks > .agent-tasks-h")
    assert "flex-wrap:nowrap" in head
    # an icon, not an emoji
    row = HTML[HTML.index('<div class="agent-tasks-h">'):HTML.index('id="agent-goal-k"')]
    assert 'class="agent-goal-ic"' in row and 'aria-hidden="true"' in row


def test_the_list_opens_as_a_floating_panel_that_never_pushes_the_chat():
    panel = _rule(".agent-collapse-bar > .agent-tasks > .agent-tasks-body")
    for want in ("position:absolute", "top:calc(100% + 6px)", "right:-1px",
                 "background:var(--surface)", "var(--scrim)", "overflow:auto"):
        assert want in panel, want
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", panel), "theme tokens only"
    body = HTML[HTML.index('id="agent-tasks-body"'):]
    body = body[:body.index(">")]
    assert 'role="group"' in body and 'aria-label="Goal and tasks"' in body


def test_esc_and_a_click_outside_close_the_panel():
    i = HTML.index("(function wireTaskForms(){")
    js = HTML[i:HTML.index("})();", i)]
    assert "e.key !== 'Escape' || !goalPanelOpen()" in js
    assert "document.querySelector('dialog[open]')" in js, "a modal keeps its own Esc"
    assert "back.focus();" in js
    assert "box.contains(e.target)" in js


def test_an_x_closes_the_panel_and_finishing_the_goal_asks_twice():
    # owner, 2026-10-19: "when I click Close goal it removes it, we want an
    # icon to remove it alone" -- the X closes the panel, the goal stays
    body = HTML[HTML.index('id="agent-tasks-body"'):HTML.index('id="agent-goal-full"')]
    x = body[body.index('id="agent-tasks-close"'):]
    x = x[:x.index("</button>")]
    assert 'aria-label="Close this panel"' in x and "<svg" in x and 'aria-hidden="true"' in x
    i = HTML.index("(function wireTaskForms(){")
    js = HTML[i:HTML.index("})();", i)]
    xh = js[js.index("xBtn.addEventListener('click'"):]
    xh = xh[:xh.index("});")]
    assert "setGoalOpen(false);" in xh and "/close" not in xh, "the X never finishes the goal"
    # the finish button says what it does and needs a second click
    assert ">Mark goal done</button>" in HTML and ">Close goal<" not in HTML
    fin = js[js.index("closeBtn.addEventListener('click'"):]
    fin = fin[:fin.index("/close")]
    assert "closeBtn.classList.contains('armed')" in fin and "return;" in fin
    assert "'Click again to finish the goal'" in fin and "setTimeout(disarm, 4000)" in fin
    assert ".agent-tasks-close{" in HTML and "#agent-goal-close.armed{" in HTML


def test_folded_is_one_line_on_a_desktop_and_one_box_on_a_phone():
    assert "max-width:48%; font-size:11.5px;" in HTML
    i = HTML.index("/* Open: the bar is the first line, the goal takes what the chevron leaves.")
    phone = HTML[HTML.rindex("@media (max-width:640px){", 0, i):]
    phone = phone[:phone.index("\n  }\n")]
    # open: one column, the bar first; folded: line 1 summary + chevron, line 2 goal
    assert "grid-template-columns:minmax(0,1fr);" in phone
    assert "order:-1;" in phone
    assert "order:3; flex:1 1 100%;" in phone
    assert "min-height:var(--tap, 44px)" in phone
    assert "left:-1px; right:-1px;" in phone


def test_thin_folded_targets_are_desktop_only():
    i = HTML.index("body.cx-agent-view #agent-section.agent-top-collapsed .agent-collapse-bar .agent-tasks-h .btn{")
    assert "@media (min-width:641px)" in HTML[max(0, i - 300):i]


def test_every_session_bar_control_has_one_height():
    i = HTML.index("/* ONE height for every control of the session bar")
    block = HTML[i:i + 1400]
    assert "min-height:30px" in block
    assert "min-height:var(--tap, 44px)" in block
    for sel in (".agent-session-bar .btn", ".agent-session-bar .agent-mode", ".agent-quality .q-opt span"):
        assert sel in block, sel
