"""No AI slop: prevented in planning, checked in output.

Owner, 2026-10-04: "In web design: no AI slop, no AI watermark/tells --
perfect. And slop must be prevented from the BEGINNING, in planning and in
designing the architecture, not only caught at the end."

- slopcheck.check_files: every rule fires on a real-looking sloppy page AND
  stays silent on a crafted one (real copy, a named palette, paired faces, alt
  text, readable contrast). The false-positive cases each rule was tuned
  against are pinned below, one by one.
- slopcheck.check_design: the slop CHOICES of a spec and the decisions it
  never made, before any code.
- plan_check: a web build's plan is slop-checked in the dry run; a high
  finding takes the planner's existing one re-ask, the rest are warnings on
  the "Plan check:" line. Non-web goals and small fixes are never checked.
- craft: DESIGN DECISIONS FIRST ships with the plan on web/UI turns, and
  WEB_DESIGN carries the anti-slop list, within budget.
"""
import json
import time

import pytest

import craft
import plan_check
import slopcheck as S
import swarm_windows as SW


def _rules(findings):
    return {f["rule"] for f in findings}


# --------------------------------------------------------------------------- #
# Two pages: the AI default, and a crafted one
# --------------------------------------------------------------------------- #

SLOPPY = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="generator" content="v0.dev">
<title>Your Company</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;700&display=swap" rel="stylesheet">
<style>
body { font-family: 'Inter', sans-serif; text-align: center; min-width: 1200px; }
.hero { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; }
.muted { color: #aaaaaa; background: #ffffff; }
.card { backdrop-filter: blur(12px); border-radius: 16px; }
.nav { backdrop-filter: blur(10px); }
.modal { backdrop-filter: blur(20px); }
button:focus { outline: none; }
a { outline: none }
@keyframes float { from { transform: translateY(0) } to { transform: translateY(-10px) } }
@keyframes fade { from { opacity: 0 } to { opacity: 1 } }
.hero h1 { animation: float 3s infinite; }
.card { animation: fade 1s; }
</style>
</head>
<body>
<nav class="nav"><a href="#">\U0001F3E0 Home</a> <a href="#">\U0001F680 Features</a> <a href="#">\U0001F4AC Contact</a></nav>
<section class="hero">
  <h1>Elevate your workflow with cutting-edge AI</h1>
  <p>Unlock the power of seamless collaboration. In today's fast-paced world, we revolutionize teamwork.</p>
  <img src="hero.png">
  <button class="btn">\U0001F680 Get started</button>
</section>
<section class="features">
  <div class="card">Feature 1</div><div class="card">Feature 2</div><div class="card">Feature 3</div>
</section>
<section class="services">
  <div class="card">Fast</div><div class="card">Secure</div><div class="card">Scalable</div>
</section>
<section class="testimonials">
  <div class="card">"Amazing product!" - Sarah Johnson, CEO at TechCorp</div>
  <div class="card">"Changed our life." - Michael Chen</div>
  <div class="card">"Five stars." - Emily Rodriguez</div>
</section>
<section class="stats">
  <div class="card">10,000+ happy customers</div><div class="card">99% satisfaction rate</div><div class="card">50+ countries</div>
</section>
<footer>
  <p>&copy; 2024 Your Company. Contact john.doe@example.com or (555) 123-4567.</p>
  <p>Built with AI using v0</p>
</footer>
</body>
</html>
"""

GOOD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ferrand Bakery - wood-fired sourdough on rue Paradis, Marseille</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Libre+Caslon+Text:wght@400;700&family=Work+Sans:wght@400;600&display=swap">
<style>
:root { --ink: #1f2a24; --paper: #f6f1e7; --crust: #8a3f14; --muted: #4f534b; }
body { margin: 0; font-family: "Work Sans", system-ui, sans-serif; color: var(--ink); background: var(--paper); }
h1, h2 { font-family: "Libre Caslon Text", Georgia, serif; }
.wrap { max-width: 68rem; margin: 0 auto; padding: 0 1rem; }
.hero { display: grid; grid-template-columns: 1.2fr 1fr; gap: 2rem; }
.lede { color: var(--muted); background: var(--paper); }
.btn { background: var(--crust); color: #ffffff; padding: .75rem 1.25rem; border-radius: 4px; }
.btn:focus-visible, a:focus-visible { outline: 3px solid var(--crust); outline-offset: 3px; }
.hours { width: 100%; max-width: 640px; }
.site-nav { position: sticky; top: 0; backdrop-filter: blur(6px); }
@keyframes rise { from { transform: translateY(8px); opacity: .6 } to { transform: none; opacity: 1 } }
@keyframes slide { from { transform: translateX(-12px) } to { transform: none } }
.hero h1 { animation: rise .5s cubic-bezier(.16,1,.3,1); }
.menu li { animation: slide .4s ease-out; }
@media (prefers-reduced-motion: reduce) { * { animation: none !important; } }
@media (max-width: 40rem) { .hero { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<header class="wrap site-nav">
  <a href="/" class="logo">Ferrand Bakery</a>
  <nav><a href="#bread">The bread</a> <a href="#hours">Hours</a> <a href="tel:+33491550123">04 91 55 01 23</a></nav>
</header>
<main>
<section class="hero wrap" id="top">
  <div>
    <h1>Sourdough proved for 36 hours, baked in a wood oven since 1987</h1>
    <p class="lede">Every morning at six on rue Paradis: rye tourte, tradition baguette and the olive fougasse made with Nyons olives.</p>
    <a class="btn" href="#hours">See today's hours</a>
  </div>
  <img src="img/rye-tourtes-leaving-the-oven.webp" alt="Rye tourtes on the peel as they leave the wood oven" width="800" height="600">
</section>
<section id="bread" class="wrap">
  <h2>The bread</h2>
  <ul class="menu"><li>Rye tourte - 6.40 EUR</li><li>Tradition baguette - 1.30 EUR</li><li>Olive fougasse - 4.20 EUR</li></ul>
</section>
<section id="story" class="wrap">
  <h2>Three generations at one oven</h2>
  <p>Michel Ferrand lit the oven in 1987; his granddaughter Ines kneads there today.</p>
  <figure><img src="img/ines-kneading-dough.webp" alt="Ines Ferrand kneading dough by hand in the bakehouse" width="600" height="400"></figure>
</section>
<section id="reviews" class="wrap">
  <h2>What regulars say</h2>
  <blockquote>"The only tourte that lasts the whole week." <cite>Google review by Paul R., March 2026</cite></blockquote>
</section>
<section id="hours" class="wrap">
  <h2>Hours</h2>
  <table class="hours"><tr><th>Tue-Sat</th><td>6:00-19:30</td></tr><tr><th>Sun</th><td>6:30-13:00</td></tr></table>
</section>
</main>
<footer class="wrap"><p>Ferrand Bakery, 112 rue Paradis, 13006 Marseille - hello@ferrand-bakery.fr</p></footer>
</body>
</html>
"""

SLOPPY_JSX = """import React from 'react';

const features = [
  { icon: "\U0001F680", title: "Lightning fast" },
  { icon: "\U0001F512", title: "Secure by default" },
  { icon: "✨", title: "Seamless integration" },
];

export default function Landing() {
  return (
    <main className="w-[1280px] mx-auto">
      <section className="bg-gradient-to-r from-indigo-500 via-purple-500 to-pink-500 text-center">
        <h1 className="text-5xl">Supercharge your team</h1>
        <button className="rounded-xl focus:outline-none">Get started</button>
        <img src="/team-photo.png" alt="" />
      </section>
      <div className="backdrop-blur-md">a</div>
      <div className="backdrop-blur-lg">b</div>
      <div className="backdrop-blur">c</div>
    </main>
  );
}
"""

GOOD_JSX = """import React from 'react';

const loaves = [
  { name: "Rye tourte", price: "6.40 EUR", note: "keeps a week in a linen bag" },
  { name: "Tradition baguette", price: "1.30 EUR", note: "out of the oven at 6:00 and 16:00" },
];

export default function Bread() {
  return (
    <section className="mx-auto max-w-5xl px-4 grid md:grid-cols-[2fr_1fr] gap-8">
      <h2 className="font-display text-4xl">The bread</h2>
      <ul>
        {loaves.map((l) => (
          <li key={l.name} className="py-3 border-b border-stone-300">
            {l.name} - {l.price} <span className="text-stone-700">{l.note}</span>
          </li>
        ))}
      </ul>
      <a href="/hours" className="bg-[#8a3f14] text-white px-5 py-3 focus-visible:outline focus-visible:outline-2">See hours</a>
      <img src="/img/rye-tourte.webp" alt="A rye tourte cut open on a board" width={800} height={600} />
    </section>
  );
}
"""

PAGE_RULES = {"placeholder", "ai_credit", "no_viewport", "img_alt", "contrast", "ai_gradient",
              "outline_none", "centered_everything", "ai_copy", "fake_proof", "emoji_icons",
              "dead_link", "fixed_width", "identical_cards", "generic_fonts", "glassmorphism",
              "reduced_motion"}


def test_the_sloppy_page_trips_every_rule():
    found = S.check_files([("index.html", SLOPPY)])
    assert PAGE_RULES <= _rules(found), PAGE_RULES - _rules(found)
    for f in found:
        assert set(f) == {"rule", "severity", "where", "why", "fix"}
        assert f["severity"] in S.SEVERITIES and f["why"] and f["fix"]
        assert f["where"].startswith("index.html:")
    sev = [f["severity"] for f in found]
    assert sev == sorted(sev, key=S.SEVERITIES.index), "high first"


def test_the_crafted_page_is_clean():
    found = S.check_files([("index.html", GOOD)])
    assert found == [], found
    assert S.summary(found) == {"score": 100, "high": 0, "medium": 0, "low": 0,
                                "line": "Slop check: 100/100 -- nothing found"}


def test_jsx_and_tailwind_both_ways():
    bad = _rules(S.check_files({"Landing.tsx": SLOPPY_JSX}))
    assert {"ai_gradient", "outline_none", "ai_copy", "emoji_icons", "img_alt",
            "fixed_width", "glassmorphism"} <= bad, bad
    assert S.check_files({"Bread.tsx": GOOD_JSX}) == []


def test_where_points_at_the_line():
    found = S.check_files([("index.html", SLOPPY)])
    lines = SLOPPY.splitlines()
    vp = [f for f in found if f["rule"] == "ai_gradient"][0]
    n = int(vp["where"].split(":")[1])
    assert "linear-gradient" in lines[n - 1]
    alt = [f for f in found if f["rule"] == "img_alt"][0]
    assert "<img" in lines[int(alt["where"].split(":")[1]) - 1]


# --------------------------------------------------------------------------- #
# Each rule: fires on the tell, silent on what only looks like it
# --------------------------------------------------------------------------- #

HTML_HEAD = ('<!doctype html><html><head><meta name="viewport" content="width=device-width">'
             '</head><body>%s</body></html>')


def _page(body):
    return S.check_files([("p.html", HTML_HEAD % body)])


@pytest.mark.parametrize("bad,rule", [
    ("<p>Lorem ipsum dolor sit amet.</p>", "placeholder"),
    ("<p>Call us: 555-123-4567</p>", "placeholder"),
    ("<p>Appelez le 01 23 45 67 89</p>", "placeholder"),
    ("<p>Write to hello@example.com</p>", "placeholder"),
    ("<h3>Service 1</h3><h3>Service 2</h3>", "placeholder"),
    ("<p>John Doe, founder</p>", "placeholder"),
    ("<p>Elevate your mornings.</p>", "ai_copy"),
    ("<p>We revolutionize payroll.</p>", "ai_copy"),
    ("<p>Made with ChatGPT</p>", "ai_credit"),
    ("<!-- Generated by Claude -->", "ai_credit"),
    ("<p>Join 5,000+ happy customers</p>", "fake_proof"),
    ('<a href="#">Pricing</a>', "dead_link"),
])
def test_a_tell_fires(bad, rule):
    assert rule in _rules(_page(bad)), bad


@pytest.mark.parametrize("fine", [
    "<p>We help your business grow without a single new hire.</p>",       # not "Your Company"
    "<ol><li>Step 1: weigh the flour</li><li>Step 2: fold</li></ol>",      # real steps
    "<p>Linen bread bags, 12 EUR. Welcome back on Tuesday.</p>",
    "<p>Powered by AI that reads your invoices.</p>",                       # an AI product's own copy
    "<p>Bread made by Claude, our head baker since 2009.</p>",              # a person named Claude
    "<p>1,247 customers ordered last month (source: Shopify export, Sept 2026).</p>",
    "<p>We bake 37 loaves an hour.</p>",                                    # not a round claim
    '<input type="email" placeholder="you@example.com">',                   # a form hint, not copy
    '<a href="#menu">Menu</a> <a href="/contact">Contact</a>',              # real anchors
    "<p>Open since 1987 &mdash; 400 loaves a day.</p>",
    '<button>Close ✕</button><span>★★★★☆</span>',  # glyphs, not emoji icons
    '<p>Thanks! \U0001F389</p>',                                            # one emoji in a sentence
])
def test_what_only_looks_like_a_tell_stays_silent(fine):
    assert _page(fine) == [], fine


def test_ai_credit_and_generator_meta():
    assert "ai_credit" in _rules(S.check_files({"a.html": '<meta name="generator" content="Lovable">'}))
    f = [x for x in S.check_files({"a.html": '<meta name="generator" content="Hugo 0.120">'})
         if x["rule"] == "ai_credit"]
    assert f and f[0]["severity"] == "low", "a static-site generator is a leftover, not an AI tell"


def test_viewport_only_for_a_whole_document():
    assert "no_viewport" in _rules(S.check_files({"a.html": "<html><head></head><body>x</body></html>"}))
    assert "no_viewport" not in _rules(S.check_files({"partial.html": "<div class='card'>x</div>"}))


def test_alt_text():
    rules = _rules(_page('<img src="team-photo.jpg">'))
    assert "img_alt" in rules
    assert _page('<img src="img/divider-wave.svg" alt="">') == [], "decoration may be alt=\"\""
    assert _page('<img src="logo.svg" alt="" aria-hidden="true">') == []
    empty = [f for f in _page('<img src="img/product-shot.webp" alt="">') if f["rule"] == "img_alt"]
    assert empty and empty[0]["severity"] == "medium"


def test_emoji_as_icons():
    assert "emoji_icons" in _rules(_page(
        "<ul><li>✅ Free shipping</li><li>\U0001F512 Secure checkout</li></ul>"))
    assert "emoji_icons" not in _rules(_page(
        "<ul><li>✓ Free shipping</li><li>✓ Secure checkout</li></ul>"))


def test_gradient_only_the_default_ai_one():
    css = ".hero{background:linear-gradient(90deg,#6366f1,#8b5cf6)}"
    assert "ai_gradient" in _rules(S.check_files({"s.css": css}))
    assert "ai_gradient" in _rules(S.check_files({"s.css": ".banner{background-image:linear-gradient(to right, purple, blue)}"}))
    for fine in (".hero{background:linear-gradient(90deg,#ff7e5f,#feb47b)}",            # sunset
                 ".hero{background:linear-gradient(#0f766e,#14b8a6)}",                    # brand teal
                 ".hero{background:linear-gradient(120deg,#ff7e5f,#a855f7,#3b82f6)}",     # passes through, warm start
                 ".hero{background:linear-gradient(#111,#333)}"):
        assert "ai_gradient" not in _rules(S.check_files({"s.css": fine})), fine
    sev = [f["severity"] for f in S.check_files({"s.css": ".btn{background:linear-gradient(#6366f1,#3b82f6)}"})
           if f["rule"] == "ai_gradient"]
    assert sev == ["medium"], "off the hero it is a warning"


def test_glassmorphism_needs_to_be_everywhere():
    one = ".nav{backdrop-filter:blur(8px)}"
    assert "glassmorphism" not in _rules(S.check_files({"s.css": one}))
    three = one + ".card{backdrop-filter:blur(8px)}.modal{-webkit-backdrop-filter:blur(4px)}"
    assert "glassmorphism" in _rules(S.check_files({"s.css": three}))


def test_generic_fonts():
    assert "generic_fonts" in _rules(S.check_files({"s.css": "body{font-family:Inter,system-ui,sans-serif}"}))
    assert "generic_fonts" in _rules(S.check_files({"s.css": "body{font: 400 1rem/1.5 -apple-system, 'Segoe UI', Roboto, sans-serif}"}))
    for fine in ("body{font-family:'Work Sans',sans-serif} h1{font-family:'Libre Caslon Text',serif}",
                 "body{font-family:Inter,sans-serif} h1{font-family:'GT Sectra',serif}",   # paired
                 "code{font-family:monospace}",                                          # mono only
                 ":root{--body:'Söhne',sans-serif} body{font-family:var(--body)}"):
        assert "generic_fonts" not in _rules(S.check_files({"s.css": fine})), fine


def test_focus_styles():
    assert "outline_none" in _rules(S.check_files({"s.css": "button:focus{outline:none}"}))
    assert "outline_none" in _rules(S.check_files({"s.css": "*{outline:0}"}))
    for fine in ("button:focus{outline:none}button:focus-visible{outline:2px solid #8a3f14}",
                 "a:focus{outline:none;box-shadow:0 0 0 3px #8a3f14}",
                 ".card{outline:none}"):          # not focusable, nothing lost
        assert "outline_none" not in _rules(S.check_files({"s.css": fine})), fine
    low = S.check_files([("a.html", HTML_HEAD % '<a href="/x">x</a>'),
                         ("s.css", ".btn{background:#8a3f14;color:#fff}")])
    assert [f["severity"] for f in low if f["rule"] == "no_focus_styles"] == ["low"]


def test_fixed_widths():
    assert "fixed_width" in _rules(S.check_files({"s.css": ".wrap{width:1140px}"}))
    for fine in (".wrap{width:1140px;max-width:100%}",
                 "@media (min-width: 1200px){.wrap{width:1140px}}",
                 "img.hero{width:1200px}",
                 ".icon{width:24px}"):
        assert "fixed_width" not in _rules(S.check_files({"s.css": fine})), fine


def test_reduced_motion():
    heavy = ("@keyframes a{to{opacity:1}}@keyframes b{to{opacity:1}}"
             ".x{animation:a 1s}.y{animation:b 1s}")
    assert "reduced_motion" in _rules(S.check_files({"s.css": heavy}))
    guarded = heavy + "@media (prefers-reduced-motion: reduce){*{animation:none}}"
    assert "reduced_motion" not in _rules(S.check_files({"s.css": guarded}))
    assert "reduced_motion" in _rules(S.check_files({"main.js": "import { gsap } from 'gsap';\ngsap.to('.x', {y: 10})"}))
    assert "reduced_motion" not in _rules(S.check_files({"s.css": ".btn{transition:transform .2s}"}))


def test_layout_rules_need_a_pattern_not_one_instance():
    card = '<div class="card">x</div>' * 3
    four_grids = "".join("<section>%s</section>" % card for _ in range(4))
    assert "identical_cards" in _rules(_page(four_grids))
    one_grid = "<section>%s</section>" % card + "<section><p>a</p></section>" * 3
    assert "identical_cards" not in _rules(_page(one_grid))
    centred = "".join('<section class="text-center"><p>x</p></section>' for _ in range(4))
    assert "centered_everything" in _rules(_page(centred))
    mixed = '<section class="text-center"><h2>Book a table</h2></section>' + "<section><p>x</p></section>" * 3
    assert "centered_everything" not in _rules(_page(mixed))


# --------------------------------------------------------------------------- #
# Contrast: WCAG math, then the rule
# --------------------------------------------------------------------------- #

def test_contrast_matches_known_wcag_pairs():
    assert S.contrast_ratio("#000", "#fff") == pytest.approx(21.0)
    assert S.contrast_ratio("#fff", "#fff") == pytest.approx(1.0)
    assert S.contrast_ratio("#777777", "#ffffff") == pytest.approx(4.48, abs=0.01)   # the classic fail
    assert S.contrast_ratio("#767676", "#ffffff") == pytest.approx(4.54, abs=0.01)   # the lightest pass
    assert S.contrast_ratio("#595959", "#ffffff") == pytest.approx(7.0, abs=0.01)
    assert S.contrast_ratio("#ffffff", "#0000ff") == pytest.approx(8.59, abs=0.01)
    assert S.contrast_ratio("#fff", "#777") == S.contrast_ratio("#777", "#fff")
    assert S.contrast_ratio("rgb(119,119,119)", "white") == pytest.approx(4.48, abs=0.01)
    assert S.contrast_ratio("hsl(0, 0%, 0%)", "#fff") == pytest.approx(21.0)
    assert S.contrast_ratio((0, 0, 0), (255, 255, 255)) == pytest.approx(21.0)
    # translucent text is composited over its background first
    assert S.contrast_ratio("rgba(0,0,0,0.5)", "#fff") == pytest.approx(
        S.contrast_ratio("#808080", "#fff"), abs=0.05)
    with pytest.raises(ValueError):
        S.contrast_ratio("var(--ink)", "#fff")


def test_contrast_rule_is_conservative():
    hi = [f for f in S.check_files({"s.css": ".m{color:#aaa;background:#fff}"}) if f["rule"] == "contrast"]
    assert hi and hi[0]["severity"] == "high"
    mid = [f for f in S.check_files({"s.css": ".m{color:#777;background-color:#fff}"}) if f["rule"] == "contrast"]
    assert mid and mid[0]["severity"] == "medium"
    via_vars = ":root{--fg:#999;--bg:#fff}.m{color:var(--fg);background:var(--bg)}"
    assert "contrast" in _rules(S.check_files({"s.css": via_vars}))
    inline = HTML_HEAD % '<p style="color:#bbb;background:#fff">x</p>'
    assert "contrast" in _rules(S.check_files({"a.html": inline}))
    for fine in (".m{color:#595959;background:#fff}",
                 ".m{color:#aaa}",                                       # no pair in one rule
                 ".m{color:#aaa;background:linear-gradient(#fff,#eee)}",  # unknown backdrop
                 ".m{color:#aaa;background:rgba(255,255,255,.4)}",       # translucent backdrop
                 "input::placeholder{color:#aaa;background:#fff}",
                 "button:disabled{color:#aaa;background:#fff}"):
        assert "contrast" not in _rules(S.check_files({"s.css": fine})), fine


# --------------------------------------------------------------------------- #
# Inputs and the score
# --------------------------------------------------------------------------- #

def test_inputs_paths_texts_dicts_and_skips(tmp_path):
    page = tmp_path / "index.html"
    page.write_text(SLOPPY, encoding="utf-8")
    css = tmp_path / "s.css"
    css.write_text(".m{color:#aaa;background:#fff}", encoding="utf-8")
    vendor = tmp_path / "node_modules" / "lib" / "x.html"
    vendor.parent.mkdir(parents=True)
    vendor.write_text("<p>Lorem ipsum</p>", encoding="utf-8")
    minified = tmp_path / "app.min.css"
    minified.write_text(".m{color:#aaa;background:#fff}", encoding="utf-8")
    found = S.check_files([str(page), str(css), str(vendor), str(minified)])
    wheres = {f["where"].rsplit(":", 1)[0] for f in found}
    assert wheres == {str(page), str(css)}
    assert S.check_files(str(css))[0]["rule"] == "contrast"
    assert S.check_files("<p>Lorem ipsum dolor</p>")[0]["where"] == "text:1"
    assert S.check_files(["a{}", "<p>Lorem ipsum</p>"])[0]["where"] == "text[2]:1"
    assert S.check_files([{"name": "x.md", "text": "Elevate your baking."}])[0]["rule"] == "ai_copy"
    assert S.check_files({"x": "body{color:#aaa;background:#fff}"}, kind="css")[0]["rule"] == "contrast"
    assert S.check_files([]) == [] and S.check_files(None) == []
    assert S.check_files([str(tmp_path / "missing.html")]) == []


def test_markdown_copy_is_checked_but_code_is_not():
    md = "# Install\n\n```\nlorem ipsum in a code sample\n```\n\nRun `seamless` to start.\n"
    assert S.check_files({"README.md": md}) == []
    assert "placeholder" in _rules(S.check_files({"README.md": "Contact: Jane Doe"}))


def test_summary_score_and_line():
    found = S.check_files([("index.html", SLOPPY)])
    s = S.summary(found)
    assert s["high"] == sum(f["severity"] == "high" for f in found)
    assert s["medium"] + s["low"] + s["high"] == len(found)
    assert s["score"] == 0 and s["line"].startswith("Slop check: 0/100 -- ")
    one = [{"rule": "ai_copy", "severity": "medium", "where": "a:1", "why": "w", "fix": "f"}]
    assert S.summary(one)["score"] == 93
    assert S.summary(one * 3)["score"] == 91, "a repeat costs little; a new rule costs its weight"
    assert S.summary(one)["line"] == "Slop check: 93/100 -- 1 medium (ai_copy)"


# --------------------------------------------------------------------------- #
# The design, before any code
# --------------------------------------------------------------------------- #

SLOPPY_SPEC = """Design: a modern, clean and minimal landing page with a sleek feel.
Hero with a purple to blue gradient, then a features section with 3 cards,
testimonials, pricing and a final CTA."""

GOOD_SPEC = """Visual decisions
- palette: #1F2A24 ink (text), #F6F1E7 paper (background), #8A3F14 crust (accent), #4F534B muted (captions)
- type: Libre Caslon Text for headings, Work Sans for body
- layout: an editorial two-column hero (the oven photo right, opening hours left) because regulars come for the hours; the menu is a priced list, not cards
- motion: one rise on the hero line, list items slide in; static under prefers-reduced-motion
- copy: the owner's own menu, prices and story from the brief; anything missing is [NEEDS INPUT]
No purple gradient anywhere, and not Inter."""


def test_a_sloppy_spec_is_named_choice_by_choice():
    found = S.check_design(SLOPPY_SPEC, request="build a website for my bakery")
    by = {f["rule"]: f["severity"] for f in found}
    assert by == {"default_gradient": "high", "vague_style": "high", "no_palette": "high",
                  "no_type_pairing": "high", "stock_skeleton": "medium",
                  "no_layout_concept": "medium", "no_copy_source": "medium"}
    assert all(f["where"] == "design" and f["fix"] for f in found)


def test_a_concrete_spec_passes():
    assert S.check_design(GOOD_SPEC, request="build a website for my bakery") == []


def test_negations_and_the_users_own_choices_are_respected():
    spec = GOOD_SPEC.replace("No purple gradient anywhere, and not Inter.",
                             "Avoid Inter, Poppins and Montserrat; no indigo-to-violet gradient.")
    assert S.check_design(spec) == []
    asked = "Hero: a purple gradient behind the headline. " + GOOD_SPEC
    assert "default_gradient" in _rules(S.check_design(asked))
    assert "default_gradient" not in _rules(S.check_design(asked, request="I want a purple gradient hero"))
    inter = GOOD_SPEC.replace("Work Sans", "Inter")
    assert "slop_font" in _rules(S.check_design(inter))
    assert "slop_font" not in _rules(S.check_design(inter, request="our brand font is Inter"))


def test_decisions_given_elsewhere_count():
    bare = "layout: a two-column editorial grid because the menu is long; copy: the owner's menu"
    assert {"no_palette", "no_type_pairing"} <= _rules(S.check_design(bare))
    # ... by the user, in the request
    got = S.check_design(bare, request="use #0B3D2E and #F4EFE6, headings in Gambarino, body in Switzer")
    assert not {"no_palette", "no_type_pairing"} & _rules(got)
    # ... or by the project that already exists
    got = S.check_design(bare + "; keep the existing palette and fonts from styles.css")
    assert not {"no_palette", "no_type_pairing"} & _rules(got)
    one_face = S.check_design(bare + "; palette #111111 #fafafa; type: Söhne throughout")
    assert [f["severity"] for f in one_face if f["rule"] == "no_type_pairing"] == ["medium"]
    assert "no_type_pairing" not in _rules(S.check_design(bare + "; palette #111111 #fafafa; system font stack"))


def test_issue_numbers_are_not_a_palette():
    assert "no_palette" in _rules(S.check_design("fixes #123 and #456; Libre Caslon + Work Sans"))


# --------------------------------------------------------------------------- #
# plan_check: prevention at planning time
# --------------------------------------------------------------------------- #

WEB_GOAL = "Build a website for Ferrand Bakery in Marseille"

SLOPPY_PLAN = {
    "design": {"components": ["index.html: the page", "styles.css: the look"],
               "interfaces": ["styles.css classes used by index.html"]},
    "phases": [
        {"title": "Markup", "files": ["index.html"],
         "task": "build index.html with a hero, a features section with 3 cards, testimonials and a CTA",
         "done_when": "index.html opens in a browser with every section"},
        {"title": "Styles", "files": ["styles.css"],
         "task": "write styles.css: a modern, clean, minimal look with a purple to blue gradient hero",
         "done_when": "styles.css styles every section of the page"},
    ]}

GOOD_PLAN = {
    "design": {"components": ["index.html: the page", "styles.css: the look"],
               "interfaces": ["styles.css classes used by index.html"],
               "visual": {"palette": ["#1F2A24 ink (text)", "#F6F1E7 paper (background)",
                                      "#8A3F14 crust (accent)", "#4F534B muted"],
                          "type": "Libre Caslon Text for headings, Work Sans for body",
                          "layout": "editorial two-column hero with the opening hours, because regulars come for them",
                          "motion": "one rise on the hero line; static under reduced motion",
                          "copy": "the owner's menu, prices and story from the brief; gaps are [NEEDS INPUT]"}},
    "phases": [
        {"title": "Markup", "files": ["index.html"],
         "task": "build index.html: the hours hero, the priced bread list and the family story",
         "done_when": "index.html opens in a browser with every section"},
        {"title": "Styles", "files": ["styles.css"],
         "task": "write styles.css to the visual decisions in the design",
         "done_when": "styles.css styles every section of the page"},
    ]}


def _phases(plan):
    return [dict(p, needs=[]) for p in plan["phases"]]


def test_a_web_plan_with_slop_gets_replan_findings_for_the_highs():
    _fixed, report = plan_check.check_plan(_phases(SLOPPY_PLAN),
                                           plan_check.normalize_design(SLOPPY_PLAN["design"]),
                                           WEB_GOAL, None)
    slop = [f for f in report["findings"] if f["kind"] == "design_slop"]
    assert {f["rule"] for f in slop if f["action"] == "replan"} == {
        "default_gradient", "vague_style", "no_palette", "no_type_pairing"}
    assert {f["rule"] for f in slop if f["action"] == "warn"} == {
        "stock_skeleton", "no_layout_concept", "no_copy_source"}
    assert all(f["text"].startswith("design: ") and f["fix"] for f in slop)
    ask = plan_check.replan_ask(WEB_GOAL, [f for f in slop if f["action"] == "replan"],
                                _phases(SLOPPY_PLAN), SLOPPY_PLAN["design"])
    assert "design: no concrete palette" in ask and "-- fix: name 4-6 hex values" in ask
    assert '"visual": {"palette"' in ask, "the planner is told WHERE the look goes"


def test_a_concrete_web_plan_passes_the_slop_check():
    _fixed, report = plan_check.check_plan(_phases(GOOD_PLAN),
                                           plan_check.normalize_design(GOOD_PLAN["design"]),
                                           WEB_GOAL, None)
    assert [f for f in report["findings"] if f["kind"] == "design_slop"] == []


@pytest.mark.parametrize("goal", [
    "build the items app with a REST API",                  # not web work
    "fix the broken footer link on my website",             # a fix, not a build
])
def test_non_web_goals_and_fixes_are_never_slop_checked(goal):
    _fixed, report = plan_check.check_plan(_phases(SLOPPY_PLAN), {}, goal, None)
    assert not [f for f in report["findings"] if f["kind"] == "design_slop"]


def test_a_one_phase_web_plan_is_not_slop_checked():
    _fixed, report = plan_check.check_plan(_phases(SLOPPY_PLAN)[:1], {}, WEB_GOAL, None)
    assert not [f for f in report["findings"] if f["kind"] == "design_slop"]


def test_the_web_design_switch_turns_the_check_off(monkeypatch):
    monkeypatch.setattr(craft, "_SKILL_SOURCE", lambda: ({"web_design"}, []))
    _fixed, report = plan_check.check_plan(_phases(SLOPPY_PLAN), {}, WEB_GOAL, None)
    assert not [f for f in report["findings"] if f["kind"] == "design_slop"]


def test_the_visual_decisions_survive_normalize_and_lead_the_design_block():
    d = plan_check.normalize_design(GOOD_PLAN["design"])
    assert d["visual"][0].startswith("palette: #1F2A24 ink (text), #F6F1E7")
    assert [v.split(":")[0] for v in d["visual"]] == ["palette", "type", "layout", "motion", "copy"]
    assert plan_check.normalize_design(d) == d, "a stored run reads back the same"
    flat = plan_check.normalize_design({"components": ["a"], "palette": "#111111, #fafafa",
                                        "typography": {"display": "Gambarino", "body": "Switzer"}})
    assert flat["visual"] == ["palette: #111111, #fafafa", "type: display: Gambarino; body: Switzer"]
    old = {"components": ["a"], "interfaces": ["b"], "data_flow": "x -> y"}
    assert plan_check.normalize_design(old) == old, "a design without visual is unchanged"
    assert plan_check.design_line(d) == "Design: 2 components, 1 interface, visual decisions"
    big = dict(d, components=["c%d: " % i + "x" * 230 for i in range(12)])
    block = plan_check.render_design(plan_check.normalize_design(big), [(1, "A", ["a.py"])], 1, ["a.py"])
    assert len(block) <= plan_check.DESIGN_CHARS
    assert "#1F2A24" in block and "Work Sans" in block, "a clip never cuts the look"


# ---- the whole Multi path: plan, dry run, one re-ask, helpers -------------- #

@pytest.fixture
def _runs(tmp_path, monkeypatch):
    monkeypatch.setenv(SW._STORE_ENV, str(tmp_path / "runs"))
    monkeypatch.setattr(SW, "SPAWN_STAGGER", 0.0)
    SW._RUNS.clear()
    yield
    for run in list(SW._RUNS.values()):
        run.stop_flag.set()
    SW._RUNS.clear()


class _World:
    def __init__(self):
        self.n = 0
        self.prompts = {}

    def spawn(self, cli, project):
        self.n += 1
        return "sess-%d" % self.n

    def run_turn(self, sid, prompt):
        self.prompts[sid] = prompt
        yield {"type": "message", "text": "done"}
        yield {"type": "done"}


def _planner(*answers, asks):
    answers = list(answers)

    def planner(system, user):
        asks.append(user)
        a = answers.pop(0) if len(answers) > 1 else answers[0]
        return json.dumps(a)
    return planner


def _wait(run_id, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        st = SW.status(run_id)
        if st and st["state"] in (SW.DONE, SW.FAILED, SW.STOPPED):
            return st
        time.sleep(0.02)
    return SW.status(run_id)


def test_a_sloppy_web_plan_is_re_asked_and_the_decided_look_reaches_every_helper(tmp_path, _runs):
    asks, w = [], _World()
    rid = SW.start(WEB_GOAL, str(tmp_path), "opencode", w.spawn, w.run_turn,
                   planner=_planner(SLOPPY_PLAN, GOOD_PLAN, asks=asks))
    st = _wait(rid)
    assert st["state"] == SW.DONE
    assert len(asks) == 2, "the existing one re-ask, nothing more"
    assert "design: no concrete palette" in asks[1] and '"visual"' in asks[1]
    check = st["plan_check"]
    assert check["replanned"] is True
    assert check["fixed"][0].startswith("re-planned to cover ")
    assert '"a hex palette"' in check["fixed"][0] and '"a type pairing"' in check["fixed"][0]
    assert not [x for x in check["warnings"] if x.startswith("design:")]
    assert st["design"]["visual"][0].startswith("palette: #1F2A24")
    for prompt in w.prompts.values():
        assert "Visual decisions (every helper uses exactly these" in prompt
        assert "#8A3F14 crust (accent)" in prompt and "Libre Caslon Text" in prompt


def test_a_re_ask_that_keeps_the_slop_fails_open_with_warnings(tmp_path, _runs):
    asks, w = [], _World()
    rid = SW.start(WEB_GOAL, str(tmp_path), "opencode", w.spawn, w.run_turn,
                   planner=_planner(SLOPPY_PLAN, asks=asks))
    st = _wait(rid)
    assert st["state"] == SW.DONE, "fail open: the run goes ahead"
    assert len(asks) == 2
    warns = st["plan_check"]["warnings"]
    assert "design: no concrete palette (no hex values)" in warns
    assert "design: the default purple/indigo/blue gradient hero" in warns
    assert "planner re-asked once" in st["plan_check"]["line"]
    assert all(f["action"] != "replan" for f in st["plan_check"]["findings"])


def test_a_non_web_run_is_not_re_asked_for_its_look(tmp_path, _runs):
    asks, w = [], _World()
    rid = SW.start("build the items app with a REST API", str(tmp_path), "opencode",
                   w.spawn, w.run_turn, planner=_planner(SLOPPY_PLAN, asks=asks))
    st = _wait(rid)
    assert len(asks) == 1
    assert not [x for x in st["plan_check"]["warnings"] if x.startswith("design:")]


# --------------------------------------------------------------------------- #
# craft: DESIGN DECISIONS FIRST + the tightened WEB_DESIGN
# --------------------------------------------------------------------------- #

def test_design_first_ships_on_web_turns_after_the_plan():
    body = craft.system_message("build me a restaurant website")["content"]
    assert "DESIGN DECISIONS FIRST" in body
    assert body.index("PLAN FIRST") < body.index("DESIGN DECISIONS FIRST") < body.index("ACT (applies")
    for decision in ("Palette: 4-6 hex", "display + body pairing", "Layout:", "Motion:",
                     "[NEEDS INPUT]", "no defaults", "shared DESIGN already fixes them? use it"):
        assert decision in craft.DESIGN_FIRST, decision
    tool_less = craft.system_message("build me a restaurant website", tools=False)["content"]
    assert tool_less.index("DESIGN DECISIONS FIRST") < tool_less.index("VERIFY, FIX, STOP")


def test_design_first_stays_off_non_web_turns():
    for text in ("refactor the auth module", "write tests for the parser",
                 "build an online store and deploy it"):          # ecommerce, no web_design slot
        msg = craft.system_message(text)
        assert msg is None or "DESIGN DECISIONS FIRST" not in msg["content"], text


def test_web_design_carries_the_anti_slop_list():
    body = craft.WEB_DESIGN
    for item in ("lorem ipsum", "Your Company", "Feature 1/2/3", "[NEEDS INPUT]",
                 "purple->blue/indigo hero gradient", "Built with AI", "generator meta",
                 "the same card grid in every section", "everything centred",
                 "In today's fast-paced", "Revolutionize", "cutting-edge", "Welcome to",
                 ">=4.5:1", "viewport meta", "fixed px widths", "real alt text",
                 ":focus-visible", "outline:none", "Poppins", "display face paired with a body face",
                 "all as hex"):
        assert item in body, item
    assert "one family" not in body.lower(), "the old rule contradicted pairing"


def test_the_new_blocks_stay_within_budget():
    # 5088 chars before 2026-10-04; the owner's cap for the WEB_DESIGN increase
    # is ~120 tokens (chars / 4, the suite's convention).
    assert (len(craft.WEB_DESIGN) - 5088) / 4 <= 120
    assert len(craft.DESIGN_FIRST) / 4 <= 125


@pytest.mark.parametrize("text,web", [
    ("build me a restaurant website", True),
    ("redesign the landing page", True),
    ("Construis un site web pour ma boulangerie", True),
    ("a web app UI for the inventory", True),
    ("refactor the parser", False),
    ("site reliability runbook", False),
    ("write a blog post about sourdough", False),
])
def test_is_web_ui(text, web):
    assert craft.is_web_ui(text) is web
