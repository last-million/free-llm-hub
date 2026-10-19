"""The goal row has no line of its own any more (owner, 2026-10-19: "the goal
bar should be included in the first bar ... it takes space too").

One element, moved by the collapse logic: into the folded summary bar
(Full permissions . cli . project . quality) when the top is collapsed, into
the session bar when it is open. Static checks of the template.
"""
import os
import re

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(HERE, "templates", "index.html"), encoding="utf-8").read()


def _fn(name):
    i = HTML.index("function %s(" % name)
    return HTML[i:HTML.index("\n    }", i)]


def test_the_goal_moves_into_the_bar_that_is_showing():
    place = _fn("placeGoal")
    assert "document.getElementById('agent-tasks')" in place
    assert "on ? 'agent-collapse-bar' : 'agent-session-bar'" in place
    assert "host.insertBefore(t, btn)" in place and "host.appendChild(t)" in place
    paint = _fn("paint")
    assert "placeGoal(on);" in paint
    assert paint.index("placeGoal(on);") < paint.index("sec.classList.toggle('agent-top-collapsed', on);")


def test_reading_the_goal_never_folds_the_top():
    i = HTML.index("if (bar) bar.addEventListener('click'")
    handler = HTML[i:HTML.index("});", i)]
    assert "closest('#agent-tasks')" in handler


def test_in_the_bar_it_is_a_compact_inline_part_and_opens_full_width():
    assert ".agent-collapse-bar > .agent-tasks, .agent-session-bar > .agent-tasks{" in HTML
    i = HTML.index(".agent-collapse-bar > .agent-tasks, .agent-session-bar > .agent-tasks{")
    rule = HTML[i:HTML.index("}", i)]
    assert "border:0" in rule and "border-left:1px solid var(--border)" in rule
    assert "min-width:0" in rule
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", rule), "theme tokens only"
    assert ":has(> .agent-tasks-body:not([hidden]))" in HTML
    assert "flex-basis:100%" in HTML[HTML.index(":has(> .agent-tasks-body:not([hidden]))"):][:400]


def test_phones_keep_their_44px_targets():
    # The thinner buttons apply to desktop widths only.
    i = HTML.index(".agent-collapse-bar > .agent-tasks .agent-tasks-h .btn{")
    before = HTML[max(0, i - 300):i]
    assert "@media (min-width:641px)" in before
