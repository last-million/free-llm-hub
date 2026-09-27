"""What the manager cost, and what the answer canary said, are VISIBLE.

REQUESTED: per-run manager tokens (swarm.run's result["manager_tokens"], a
multi-session run's manager_tokens/manager_calls) on the activity row and the
Swarm/Multi run panel, and the canary verdict ("answered correctly" /
"answered with junk" / "HTTP error") in the provider Test UI -- the APIs
already returned all of it; the page showed none. Static checks on the
template: the page is one file with no JS test harness.
"""
import io

SRC = io.open("templates/index.html", encoding="utf-8").read()


def _body(name, span=4000):
    i = SRC.index("function %s(" % name)
    return SRC[i:i + span]


def test_canary_badge_names_every_verdict():
    body = _body("canaryBadge", 1500)
    for verdict, label in (("correct", "answered correctly"),
                           ("junk", "answered with junk"),
                           ("wrong", "answered wrong"),
                           ("empty", "empty reply")):
        assert "'%s'" % verdict in body and label in body
    assert "HTTP error" in body
    assert "esc(tip)" in body, "the reply snippet is escaped into the tooltip"


def test_the_provider_test_shows_the_canary_overall_and_per_key():
    i = SRC.index("$('.act-test', card).addEventListener('click'")
    body = SRC[i:i + 3500]
    assert "canaryBadge(r.canary, ok)" in body
    assert "canaryBadge(k.canary, !!k.ok)" in body


def test_the_subscription_test_shows_the_canary_per_model():
    body = _body("subTestResultHTML", 1800)
    # A reply gets its verdict badge; a failed CLI hop does not get the
    # generic "HTTP error" badge -- its own error text is already on the row.
    assert "canaryBadge(m.canary, true)" in body
    assert "m.skipped || !m.ok ? ''" in body


def test_the_activity_row_shows_manager_cost_and_an_unreadable_review():
    body = _body("renderActivity", 5000)
    assert "managerCostHTML(a.manager_tokens, a.manager_calls)" in body
    assert "a.review_warning" in body
    # A run whose only annotation is its manager cost still gets the detail row.
    assert "a.manager_tokens){" in body.replace(" ", "")


def test_the_swarm_panel_shows_manager_cost_and_verified_phases():
    body = _body("renderSwarmRuns", 3500)
    assert "managerCostHTML(r.manager_tokens, r.manager_calls)" in body
    assert "a.verified === true" in body


def test_manager_cost_is_silent_without_a_manager():
    body = _body("managerCostHTML", 800)
    assert "if (t <= 0) return '';" in body
    assert "manager: " in body
