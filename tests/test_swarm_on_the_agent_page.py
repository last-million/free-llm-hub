r"""Multi swarm windows, on the page where the project already is.

REQUESTED: "all should be completly robust ... and working anywhere in our
front end agent page and inside all cli's".

The orchestrator was reachable from every CLI over MCP and from a shell via
scripts/swarm.py, and not at all from the dashboard -- which is the one place
that already knows which folder you are working in.

THE TAB SWITCHER WAS A BOOLEAN. switchTab computed `toSession` and set history
to `!toSession`, which works for exactly two tabs; adding a third that way
would have made Swarm and History the same tab. It switches on WHICH now.
"""
import io
import re

SRC = io.open("templates/index.html", encoding="utf-8").read()


def _rule(sel):
    m = re.search(re.escape(sel) + r"\s*\{([^}]*)\}", SRC)
    assert m, "no rule for %s" % sel
    return m.group(1).replace(" ", "")


# --------------------------------------------------------------------------- #
# It is there
# --------------------------------------------------------------------------- #

def test_the_agent_page_has_a_swarm_tab():
    assert 'id="agent-view-swarm"' in SRC
    assert 'id="agent-swarm"' in SRC


def test_it_can_be_started_from_the_page():
    assert 'id="sw-goal"' in SRC and 'id="sw-go"' in SRC
    assert "'/api/swarm-windows'" in SRC


def test_it_uses_the_open_session_s_folder():
    """The dashboard already knows the project; typing the path again is the
    thing a UI is for avoiding."""
    assert "window.cxAgentProjectDir = currentProjectDir" in SRC
    i = SRC.index("function initAgentSwarm(")
    assert "cxAgentProjectDir" in SRC[i:i + 1200]


def test_it_refuses_clearly_with_no_session_open():
    i = SRC.index("function initAgentSwarm(")
    body = SRC[i:i + 1400]
    assert "Open or create a session first" in body


def test_a_goal_is_required():
    i = SRC.index("function initAgentSwarm(")
    assert "Say what the swarm should build" in SRC[i:i + 1400]


# --------------------------------------------------------------------------- #
# Three tabs, not two
# --------------------------------------------------------------------------- #

def test_the_switcher_handles_three_tabs():
    """`historyTab.hidden = toSession` is a two-tab idiom: with a third tab it
    would show History whenever Swarm was not selected."""
    i = SRC.index("function switchTab(which)")
    body = SRC[i:i + 1200]
    assert "swarm" in body
    assert "historyTab.hidden = toSession" not in body
    assert "!== which" in body or "=== which" in body


def test_each_tab_reports_its_own_selected_state():
    i = SRC.index("function switchTab(which)")
    assert "aria-selected" in SRC[i:i + 1200]


def test_leaving_the_tab_stops_the_poll():
    """A finished board does not need re-fetching every three seconds forever,
    and neither does a tab nobody is looking at."""
    i = SRC.index("function switchTab(which)")
    assert "stopSwarmPoll" in SRC[i:i + 1200]


def test_the_poll_stops_itself_when_nothing_is_running():
    i = SRC.index("function pollSwarm(")
    body = SRC[i:i + 1200]
    assert "live" in body and "stopSwarmPoll()" in body


def test_the_two_scopes_talk_through_exactly_two_exports():
    """The tab switcher is inside the agent IIFE; the poller is outside it."""
    assert "window.pollSwarm = pollSwarm;" in SRC
    assert "window.stopSwarmPoll = stopSwarmPoll;" in SRC


# --------------------------------------------------------------------------- #
# What a run looks like
# --------------------------------------------------------------------------- #

def test_every_phase_shows_its_state_and_dependencies():
    i = SRC.index("function renderSwarmRuns(")
    body = SRC[i:i + 2600]
    assert "a.state" in body and "a.needs" in body
    assert "sw-dot" in body


def test_the_per_phase_model_kind_is_shown():
    """"can also use different best models for the task" -- so which kind each
    phase got has to be visible."""
    i = SRC.index("function renderSwarmRuns(")
    assert "a.mode" in SRC[i:i + 2600]


def test_a_run_can_be_stopped_from_the_page():
    i = SRC.index("function renderSwarmRuns(")
    body = SRC[i:i + 2600]
    assert "data-sw-stop" in body and "'DELETE'" in body


def test_state_is_not_carried_by_colour_alone():
    i = SRC.index("function renderSwarmRuns(")
    assert 'class="sw-state ' in SRC[i:i + 2600]
    assert "esc(r.state)" in SRC[i:i + 2600]


def test_there_is_an_empty_state():
    i = SRC.index("function renderSwarmRuns(")
    assert "No swarm has run" in SRC[i:i + 2600]


def test_reduced_motion_is_respected():
    i = SRC.index(".agent-swarm{")
    assert "@media (prefers-reduced-motion:reduce)" in SRC[i:i + 2200]
