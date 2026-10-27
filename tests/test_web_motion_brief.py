"""A website / landing-page build gets a motion, image, icon and loading-speed plan -- decided
before code, checked in the output.

Owner, 2026-10-27: when the hub detects a landing page or website build it should "do maximum
perfection in web design": motion design and parallax that reacts to the mouse and auto-reverses,
zero AI slop and all original, a PLAN FOR IMAGES, perfect transitions between sections, everything
fast and preloaded, icons drawn for the page's niche, and the hyperframes skill's motion know-how.

What already existed (craft.WEB_DESIGN, DESIGN_FIRST, IMAGES, slopcheck): varied entrances, hero
video/3D, the anti-slop list, contrast, reduced-motion. What did not: mouse-reactive depth with an idle
auto-reverse, designed section-to-section transitions, a written image plan, a per-niche icon set, an
explicit preload recipe, and checks for the two commonest speed/jank mistakes (animating layout
properties; a page whose hero image is never preloaded).

HyperFrames contributes TECHNIQUE only (easing doctrine, SVG path drawing, clip-path reveals,
velocity-matched transitions). Its data-* / class="clip" composition markup belongs to its video
runtime and must never reach a live page; the brief says so.
"""
import re

import pytest

import craft
import slopcheck

WEB_ASKS = ("build me a landing page for my dental clinic",
            "create a website for a bakery in Lyon",
            "redesign my portfolio homepage")
NOT_WEB = ("refactor this sorting function", "explain how a mutex works",
           "write a python script to parse csv")


def _rules(findings):
    return {f["rule"] for f in findings}


def _content(text, tools=True):
    msg = craft.system_message(text, tools=tools)
    return (msg or {}).get("content") or ""


# --------------------------------------------------------------------------- #
# The brief
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("ask", WEB_ASKS)
@pytest.mark.parametrize("tools", [True, False])
def test_every_web_build_gets_the_motion_plan(ask, tools):
    assert craft.WEB_MOTION in _content(ask, tools)


@pytest.mark.parametrize("ask", NOT_WEB)
def test_a_non_web_request_pays_nothing_for_it(ask):
    assert craft.WEB_MOTION not in _content(ask)
    assert craft.WEB_MOTION not in _content(ask, tools=False)


def test_the_brief_stays_small():
    assert 1500 < len(craft.WEB_MOTION) < 3300


@pytest.mark.parametrize("needle", [
    r"pointermove", r"requestAnimationFrame", r"lerp", r"alternate",          # mouse depth + auto-reverse
    r"prefers-reduced-motion", r"clip-path", r"View Transitions",             # fallbacks, section transitions
    r"scroll-driven|animation-timeline", r"passive",
    r"rel=preload", r"fetchpriority", r"as=font", r"font-display",            # fast by construction
    r"AVIF|WebP", r"loading=lazy", r"width/height",
    r"<symbol", r"currentColor", r"path draw|stroke-dashoffset",              # niche icons
    r"image plan",                                                            # written before building
    r"power3|sine\.inOut|expo",                                               # easing doctrine
])
def test_the_brief_carries_the_technique(needle):
    assert re.search(needle, craft.WEB_MOTION, re.I), needle


def test_the_brief_keeps_hyperframes_markup_out_of_a_live_page():
    low = craft.WEB_MOTION.lower()
    assert "hyperframes" in low
    assert "never" in low and ("data-*" in low or "class=\"clip\"" in low)


def test_the_brief_asks_for_one_signature_interaction_not_confetti():
    assert re.search(r"\bONE\b|one signature", craft.WEB_MOTION)


def test_the_cost_ceiling_still_holds_for_the_heaviest_request():
    worst = max(len(craft.system_message(t)["content"])
                for t in ("build an online store and deploy it",
                          "create a landing page for my saas",
                          "build me a restaurant website"))
    assert worst <= craft.BRIEF_CEILING_CHARS
    assert craft.BRIEF_CEILING_CHARS <= int(0.16 * 32768 * 4)        # one deliberate raise, not a drift
    # and the motion plan is not what gets dropped when the machine block does not fit
    assert craft.WEB_MOTION in craft.system_message("create a landing page for my saas")["content"]


def test_the_design_step_asks_for_the_three_plans():
    low = craft.DESIGN_FIRST.lower()
    assert "motion" in low and "image plan" in low and "icon" in low


# --------------------------------------------------------------------------- #
# Planning check (before any code)
# --------------------------------------------------------------------------- #

GOOD_DESIGN = (
    "Palette: bg #0f1a14, text #f4efe6, accent #d8a24a, muted #8b9a8f. "
    "Type: Bricolage Grotesque display with Source Serif 4 body. "
    "Layout: one long page, the clinic's hands and tools lead, the rhythm alternates dense and quiet "
    "text sections. Motion: mouse-reactive depth layers in the hero that drift on an auto-reverse loop when the "
    "pointer idles, a clip-path wipe between sections, reduced-motion shows the still. "
    "Image plan: six generated photographs in one warm light, a hero SVG illustration, WebP with srcset. "
    "Icons: a custom SVG set drawn from the dental trade (enamel, root, mirror). "
    "Copy: the clinic's real services and prices from the user; missing ones are [NEEDS INPUT].")


def test_a_complete_design_passes():
    rules = _rules(slopcheck.check_design(GOOD_DESIGN, request="a dental clinic landing page"))
    assert not rules & {"no_motion_plan", "no_image_plan", "no_icon_plan"}, rules


def test_a_design_with_no_motion_is_flagged():
    text = GOOD_DESIGN.replace("Motion:", "Mood:")
    text = re.sub(r"mouse-reactive[^.]*\.", "calm.", text)
    assert "no_motion_plan" in _rules(slopcheck.check_design(text))


def test_a_design_with_no_image_plan_is_flagged():
    text = re.sub(r"Image plan:[^.]*\.[^.]*\.", "", GOOD_DESIGN)
    text = text.replace("photographs", "").replace("illustration", "").replace("srcset", "")
    assert "no_image_plan" in _rules(slopcheck.check_design(text))


def test_a_design_with_no_icon_plan_is_flagged_lightly():
    text = re.sub(r"Icons:[^.]*\.", "", GOOD_DESIGN)
    found = [f for f in slopcheck.check_design(text) if f["rule"] == "no_icon_plan"]
    assert found and found[0]["severity"] == "low"


def test_the_new_planning_rules_are_never_high():
    for f in slopcheck.check_design("a website"):
        if f["rule"] in ("no_motion_plan", "no_image_plan", "no_icon_plan"):
            assert f["severity"] in ("medium", "low")


# --------------------------------------------------------------------------- #
# Output check: layout-property animation
# --------------------------------------------------------------------------- #

def _page(css="", body="", head=""):
    return ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>t</title>%s<style>%s</style></head><body>%s</body></html>" % (head, css, body))


def test_transitioning_a_layout_property_is_flagged():
    for css in (".panel{transition: width .3s ease}",
                ".panel{transition-property: height, opacity}",
                ".drawer{transition: left .4s}",
                ".x{transition: margin-left .3s}"):
        assert "layout_animation" in _rules(slopcheck.check_files([("a.html", _page(css))])), css


def test_keyframes_that_move_layout_are_flagged():
    css = "@keyframes slide{from{left:-100px}to{left:0}} .a{animation:slide 1s}"
    assert "layout_animation" in _rules(slopcheck.check_files([("a.html", _page(css))]))


def test_transform_and_opacity_motion_is_clean():
    css = (".a{transition: transform .3s cubic-bezier(.16,1,.3,1), opacity .3s}"
           "@keyframes drift{from{transform:translate3d(0,0,0)}to{transform:translate3d(12px,0,0)}}"
           ".b{animation:drift 9s ease-in-out infinite alternate}"
           "@media (prefers-reduced-motion: reduce){.b{animation:none}}")
    assert "layout_animation" not in _rules(slopcheck.check_files([("a.html", _page(css))]))


def test_a_colour_transition_is_not_layout():
    css = "a{transition: color .2s, background-color .2s, box-shadow .2s}"
    assert "layout_animation" not in _rules(slopcheck.check_files([("a.html", _page(css))]))


# --------------------------------------------------------------------------- #
# Output check: a hero image that is never preloaded
# --------------------------------------------------------------------------- #

HERO = "<img src=hero.webp width=1600 height=900 alt='The clinic waiting room'>"


def test_a_page_with_images_and_no_preload_is_flagged_lightly():
    found = [f for f in slopcheck.check_files([("a.html", _page(body=HERO))]) if f["rule"] == "no_preload"]
    assert found and found[0]["severity"] == "low"


def test_preload_or_fetchpriority_satisfies_it():
    head = "<link rel=preload as=image href=hero.webp fetchpriority=high>"
    assert "no_preload" not in _rules(slopcheck.check_files([("a.html", _page(body=HERO, head=head))]))
    img = "<img src=hero.webp width=1600 height=900 alt='x' fetchpriority=high>"
    assert "no_preload" not in _rules(slopcheck.check_files([("a.html", _page(body=img))]))


def test_a_page_without_images_or_a_fragment_is_not_flagged():
    assert "no_preload" not in _rules(slopcheck.check_files([("a.html", _page(body="<p>hi</p>"))]))
    assert "no_preload" not in _rules(slopcheck.check_files([("part.html", HERO)]))
