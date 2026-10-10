"""The goal sits in the top bar of the Build page: one line, folded by default.

REQUESTED 2026-10-10: "the goal should appear in the TOP BAR section, not after
the helpers". The task board's panel (#agent-tasks, filled by loadTasks()) used
to sit inside the chat column, after the helper bar, the update banner and the
Plan/Helpers strip. It is now a compact row directly under the Build toolbar:
"Goal: <text>" (ellipsis, the full text in the tooltip), an "N open tasks"
toggle that opens the existing list and forms in place (folded by default,
remembered by this browser only, never server state), and "No goal yet" +
"Set goal" when there is no goal. Static checks, like the other UI tests;
tests/test_goal_everywhere.py keeps pinning the ids and routes.
"""
import io
import re

import pytest

HTML = io.open("templates/index.html", encoding="utf-8").read()


def _between(start, end, src=HTML):
    i = src.index(start)
    return src[i:src.index(end, i)]


ROW = _between('<div class="agent-tasks" id="agent-tasks"', '<div class="agent-split" id="agent-split">')
JS = _between("/* ---------- The task board: the goal behind the work ---------- */",
              "/* One helper of a Multi run")
CSS = _between(".agent-tasks{", ".agent-plan{")


def test_the_goal_row_sits_under_the_toolbar_above_the_helpers_and_the_plan():
    i = HTML.index('id="agent-tasks"')
    assert HTML.index('id="agent-end"') < i < HTML.index('id="agent-split"'), \
        "between the toolbar row and the split"
    for later in ('id="agent-helper-bar"', 'id="agent-update-banner"', 'id="agent-plan"'):
        assert i < HTML.index(later), later
    assert i < HTML.index('class="agent-chat-col"'), "not inside the chat column any more"
    # in #agent-session: once initAgentLayout folds the toolbar into the top
    # line, this is the first thing under it while a session is open
    assert 'id="agent-tasks"' in _between('<div class="agent-session" id="agent-session"', 'id="agent-split"')


def test_one_line_goal_count_and_toggle():
    head = _between('<div class="agent-tasks-h">', '<div class="agent-tasks-body"', ROW)
    assert ">Goal:<" in head and 'id="agent-tasks-goal"' in head
    toggle = _between('id="agent-tasks-toggle"', "</button>", head)
    assert 'aria-expanded="false"' in toggle and 'aria-controls="agent-tasks-body"' in toggle
    assert 'id="agent-tasks-n"' in toggle and "<svg" in toggle and 'aria-hidden="true"' in toggle
    set_goal = _between('id="agent-goal-set"', "</button>", head)
    assert ">Set goal" in set_goal and 'aria-expanded="false"' in set_goal
    assert 'aria-controls="agent-tasks-body"' in set_goal
    body = _between('<div class="agent-tasks-body" id="agent-tasks-body"', "</form>", ROW)
    assert body.split(">")[0].rstrip().endswith("hidden"), "folded by default"


def test_every_existing_piece_is_still_there_inside_the_body():
    body = ROW[ROW.index('id="agent-tasks-body"'):]
    for i in ("agent-goal-full", "agent-tasks-list", "agent-task-form", "agent-task-input",
              "agent-goal-form", "agent-goal-input", "agent-goal-close"):
        assert 'id="%s"' % i in body, i
    assert "loadTasks(" in HTML and "/api/goals" in JS and "/api/tasks" in JS
    # one "Set goal" per screen: the form's own button says Save goal
    assert ">Save goal</button>" in body


def test_the_script_fills_the_line_and_remembers_open_or_folded_per_browser():
    assert "goalEl.title = full;" in JS                             # the whole goal in the tooltip
    assert "goalEl.textContent = active ? full : 'No goal yet';" in JS
    assert "fullEl.textContent = full;" in JS                       # ...and in the opened body
    assert "'No open tasks'" in JS and "'1 open task'" in JS and "' open tasks'" in JS
    assert "var GOAL_OPEN_KEY = 'flh.goalOpen';" in JS
    assert "try { return localStorage.getItem(GOAL_OPEN_KEY) === '1'; } catch (e){ return false; }" in JS
    assert "try { localStorage.setItem(GOAL_OPEN_KEY, on ? '1' : '0'); } catch (e){}" in JS
    assert "b.setAttribute('aria-expanded', on ? 'true' : 'false')" in JS
    # Set goal opens the body on the form; Close goal hands focus to Set goal
    assert "setGoalOpen(true, true);" in JS and "if (inp) inp.focus();" in JS
    assert "if (s && !s.hidden) s.focus();" in JS
    # the open/folded flag is the only thing this keeps in the browser
    assert len(re.findall(r"localStorage\.", JS)) == 2


def test_the_css_is_tokens_only_wraps_and_has_phone_targets():
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", CSS), "hard-coded colour"
    goal = _between(".agent-tasks-goal{", "}", CSS)
    assert "text-overflow:ellipsis" in goal and "white-space:nowrap" in goal and "min-width:0" in goal
    assert "flex-wrap:wrap" in _between(".agent-tasks-h{", "}", CSS)
    phone = _between("@media (max-width:640px){", "@media (prefers-reduced-motion", CSS)
    assert "min-height:var(--tap, 44px)" in phone
    for sel in (".agent-tasks-h .btn", ".tk-done", ".agent-tasks-add button", ".agent-tasks-add input"):
        assert sel in phone, sel
    assert "@media (prefers-reduced-motion:reduce){ .agent-tasks-toggle .att-ic{ transition:none; } }" in CSS
    assert "max-height:240px; overflow:auto" in _between(".agent-tasks-body{", "}", CSS)
    for sel in (".agent-goal-full{", ".agent-tasks-list .tk-text{"):
        assert "overflow-wrap:anywhere" in _between(sel, "}", CSS), sel


# --------------------------------------------------------------------------- #
# Contrast, measured from the template's own tokens, both themes
# --------------------------------------------------------------------------- #

def _theme(name):
    def parse(block):
        return dict(re.findall(r"--([\w-]+):\s*([^;]+);", block))
    dark = parse(re.search(r"\n  :root\{(.*?)\n  \}", HTML, re.S).group(1))
    if name == "dark":
        return dark
    light = dict(dark)
    light.update(parse(re.search(r':root\[data-theme="light"\]\{(.*?)\n  \}', HTML, re.S).group(1)))
    return light


def _rgb(v):
    v = v.strip()
    return tuple(int(v[i:i + 2], 16) for i in (1, 3, 5))


def _ratio(a, b):
    def lum(c):
        def ch(x):
            x /= 255
            return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
        return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2])
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_every_goal_row_text_pair_meets_aa(theme):
    t = _theme(theme)
    T = lambda k: _rgb(t[k])                                    # noqa: E731
    pairs = {
        "Goal: label": (T("text"), T("surface")),
        "goal text": (T("text-dim"), T("surface")),
        "toggle / Set goal / Close goal": (T("text"), T("surface")),
        "full goal": (T("text"), T("surface")),
        "task chip": (T("text-dim"), T("surface-2")),
        "doing chip": (T("ok-text"), T("surface-2")),
        "failed chip": (T("danger-text"), T("surface-2")),
        "done chip": (T("on-accent"), T("accent-strong")),
        "Done button": (T("ok-text"), T("surface")),
        "form input": (T("text"), T("bg")),
    }
    low = {k: round(_ratio(*v), 2) for k, v in pairs.items() if _ratio(*v) < 4.5}
    assert not low, "%s theme under 4.5:1: %s" % (theme, low)
