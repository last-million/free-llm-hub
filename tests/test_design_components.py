"""Build with real components and real design tooling, not from scratch slop.

ASKED 2026-08-31: "all is perfect in maximuim the UI UX and all and use skills
web desing 0 sLOP ect and allll woow perfect and he can even use free available
componenents for all websoites types".

WEB_DESIGN already carried the "0 slop" half and carried it well -- a POV rule,
a real type scale, per-section motion, a SELF-CHECK, and a long ANTI list of
named AI tells (Inter/Geist, rounded-square icon tiles, 01/02/03 numbering,
gradient text, invented testimonials). None of that needed rewriting.

Two things were missing, and both are about not building everything by hand:

  COMPONENTS. Nothing told the agent that free, accessible, production-grade
  component sets exist. Hand-rolling a modal, a combobox or a date picker is
  where accessibility quietly dies -- no focus trap, no aria, no keyboard path --
  and it is slower than using one that already works.

  SKILLS. A design skill installed on the machine (ui-ux-pro-max and friends)
  is a searchable engine of patterns, palettes and type pairings. The brief
  never mentioned looking for one, so it was never used.

Both are worded as "use it if it fits, say which one" rather than a mandate:
a static one-page site does not need a component library, and pulling one in
regardless is its own kind of slop.

BUDGET. Funded first by rewriting -- the motion bullet and both new lines were
tightened twice -- and then by the same single ceiling move that funded PLAN
FIRST, rather than a separate slice per feature. See the note in
test_craft_briefs.test_worst_case_brief_cost.
"""
import craft


def _web(text="design me a website"):
    msg = craft.system_message(text, tools=True)
    return msg["content"] if msg else ""


def test_component_libraries_are_offered():
    body = _web().lower()
    assert "component" in body
    # named, because "use a component library" with no names is not actionable
    assert any(name in body for name in ("shadcn", "daisyui", "flowbite",
                                         "headless", "radix"))


def test_the_hard_widgets_are_the_ones_it_names():
    """Where hand-rolling actually hurts: the widgets with a keyboard contract."""
    body = _web().lower()
    assert any(w in body for w in ("modal", "dialog", "combobox", "date picker"))


def test_an_installed_design_skill_is_looked_for():
    body = _web().lower()
    assert "skill" in body


def test_it_is_a_judgement_call_not_a_mandate():
    """A one-page static site does not need a component library, and pulling
    one in regardless is its own slop."""
    body = _web()
    assert "WEB DESIGN BRIEF" in body
    low = body.lower()
    assert "if" in low or "when" in low


def test_licence_still_has_to_be_checked():
    """"free available components" -- free as in usable. The brief already makes
    this point for video clips; components get the same treatment."""
    low = _web().lower()
    assert "licence" in low or "license" in low


def test_none_of_the_anti_slop_rules_were_lost():
    """The tightening that funded this must not have eaten a rule."""
    body = _web()
    for rule in ("ANTI", "SELF-CHECK", "prefers-reduced-motion",
                 "Inter", "gradient text", "44px", "cubic-bezier"):
        assert rule in body, rule


def test_the_motion_rules_survived_the_trim():
    body = _web()
    for rule in ("Transform/opacity", "150-300ms", "IntersectionObserver",
                 "VISIBLE BY DEFAULT"):
        assert rule in body, rule


def test_the_budget_was_not_raised_again():
    """One ceiling move covers both of today's additions; this asserts the
    result actually lands under it."""
    worst = max(len(craft.system_message(t)["content"]) for t in
                ("build an online store and deploy it",
                 "create a landing page for my saas",
                 "build me a restaurant website"))
    assert worst / 4 < 32768 * 0.125, "briefs cost ~%d tokens" % (worst // 4)
