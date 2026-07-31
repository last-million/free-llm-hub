"""Calvoun Free LLM Hub — domain craft briefs, injected when they apply.

WHY THIS LIVES IN THE HUB
-------------------------
A skill file under .agents/skills/ only helps a CLI that scans that directory
(Kimi Code does; Codex and Claude Code do not). Putting the guidance in the
gateway means EVERY connected tool gets it, from one place.

WHY IT IS SAFE
--------------
This never rewrites the user's turn. It appends ONE short system message, and
only on the OPENING turn of a conversation — before any tool loop is in flight.
Rewriting a turn that carries tool_calls is what breaks Codex/Claude Code (the
same reason the prompt enhancer is scoped to the dashboard), so that is exactly
what this avoids.

WHY THE BRIEFS ARE SHORT AND SPECIFIC
-------------------------------------
Every injected token is a token the model cannot spend on the work, and it is
paid on a small free context window. So each brief is ~150 words of the
decisions a good practitioner actually makes — not a style lecture, not
"be creative", and explicitly NOT the generic advice models already over-produce
(that is what the ANTI sections exist to suppress).
"""
import re

# --------------------------------------------------------------------------- #
# Briefs. Each is (name, trigger regex, text).
#
# Trigger rules, learned from how these misfire:
#   * require a DOMAIN noun, not a verb. "design" alone matches "design a
#     database schema"; "landing page"/"hero section" do not.
#   * keep them narrow enough that a normal coding question gets NOTHING, since
#     an unwanted brief is pure token cost and unwanted instructions.
# --------------------------------------------------------------------------- #

WEB_DESIGN = """WEB DESIGN BRIEF (apply unless the user says otherwise)
- Decide a POV first: who it is for, the one feeling it should give, the one action it asks for. Then design to that.
- Type: one family, 3-4 sizes max, headline 1.1-1.2 line-height, body 1.5-1.65, measure 60-75 chars. Real hierarchy comes from size+weight+space, not colour.
- Space: one scale (4/8px). Section rhythm beats decoration. Whitespace is the design.
- Colour: one accent, one neutral ramp, semantic tokens. Never convey meaning by colour alone.
- Motion: 150-300ms, transform/opacity only, honour prefers-reduced-motion.
- Responsive: mobile-first, no horizontal scroll, tap targets >=44px, images with width/height so nothing shifts.
ANTI (these read as AI-made): centred hero + 3 identical feature cards + generic gradient blob; "Elevate/Unlock/Seamless/Empower" copy; stock-photo grids; emoji as icons; a testimonial or statistic you invented."""

SEO = """SEO BRIEF (apply unless the user says otherwise)
- One page = one intent = one primary keyword. Do not write a page you cannot name the query for.
- Title <=60 chars with the term near the front; meta description written to earn the click, not to repeat the title. One H1, headings in real order, no skipped levels.
- Internal links with descriptive anchor text; the same anchor must not point to two different URLs.
- Technical: canonical, indexable, real status codes, image alt text, sitemap, fast LCP, CLS<0.1.
- Schema only for what is genuinely on the page (Article/Product/FAQ/LocalBusiness).
- E-E-A-T: name the author, cite sources, show real specifics.
ANTI: keyword stuffing, thin doorway pages, invented statistics, fabricated reviews or ratings, schema that does not match visible content, "in today's fast-paced world" openers."""

ECOMMERCE = """E-COMMERCE BRIEF (apply unless the user says otherwise)
- Product page order: image, name, price, variant, add-to-cart above the fold; then trust (shipping, returns, stock), then detail.
- Money in integer minor units. Never floats. Currency + tax rules explicit.
- Cart/checkout: guest checkout, one column, address autocomplete, show total early including shipping, never surprise a fee at the last step.
- State: server is the source of truth for price and stock; re-validate at checkout. Idempotent order creation.
- Trust: real policies, visible contact, no dark patterns (fake countdowns, fake stock counts).
ANTI: inventing prices, brands, reviews, or delivery promises the user never gave — mark those [NEEDS INPUT]."""

LANDING = """LANDING PAGE / FUNNEL BRIEF (apply unless the user says otherwise)
- One page, ONE conversion goal. Every block earns its place against that goal or is cut.
- Order that works: specific promise + proof -> the problem in the reader's words -> what it is -> proof (numbers, names, screenshots) -> objection handling -> offer + risk reversal -> single CTA repeated.
- Headline states the OUTCOME and who it is for. Not the product category.
- One primary CTA, same words everywhere. Secondary actions visually quieter.
- Form: fewest fields that let you follow up. Every extra field costs conversions.
- Measure: define the event that counts as conversion before building.
ANTI: multiple competing CTAs, a carousel, vague benefit copy ("streamline your workflow"), and above all invented testimonials, logos, or metrics — leave [NEEDS INPUT] instead."""

PROGRAMMING = """ENGINEERING BRIEF (apply unless the user says otherwise)
- Read the existing code before changing it; match its conventions over your own defaults.
- Smallest change that fully solves it. No speculative abstraction, no unrequested refactor.
- Handle the real failure modes: empty, huge, concurrent, offline, malformed. State assumptions you had to make.
- Name things for what they are. Comments explain WHY, never restate the code.
- Tests: the failure you would actually ship, not a tautology. Prove it fails before the fix.
- Security: validate input at the boundary, parameterise queries, never log a secret.
ANTI: dumping a rewritten file when a small edit was asked for; try/except that swallows errors; claiming something works without running it."""

_BRIEFS = [
    ("landing", re.compile(
        r"\blanding page\b|\bsales page\b|\bfunnel\b|\bconversion rate\b|\bcta\b|\bopt[- ]?in page\b", re.I),
     LANDING),
    ("ecommerce", re.compile(
        r"\be-?commerce\b|\bonline store\b|\bstorefront\b|\bshopping cart\b|\bcheckout\b|"
        r"\bproduct page\b|\bshopify\b|\bstripe checkout\b", re.I), ECOMMERCE),
    ("seo", re.compile(
        r"\bseo\b|\bsearch engine optimi[sz]\w*\b|\bserp\b|\bmeta description\b|"
        r"\bkeyword research\b|\bschema markup\b|\bbacklink\b", re.I), SEO),
    ("web_design", re.compile(
        r"\blanding page\b|\bweb ?site\b|\bwebpage\b|\bweb design\b|\bui design\b|"
        r"\bhero section\b|\bredesign\b|\bstyle the\b|\btailwind\b|\bfront[- ]?end\b", re.I),
     WEB_DESIGN),
    # NOTE: bare "optimi[sz]e" was here and misfired on "write a blog post and
    # optimise it for SEO" — a writing task that got the engineering brief. Any
    # trigger this generic costs tokens on requests it has no business touching,
    # so it now needs a code noun next to it.
    ("programming", re.compile(
        r"\brefactor\b|\bdebug\b|\bfix the bug\b|\bwrite tests?\b|\bunit tests?\b|"
        r"\bapi endpoint\b|\bmigration\b|\bstack trace\b|"
        r"\boptimi[sz]e (?:the )?(?:query|queries|function|code|algorithm|build|bundle|performance)\b",
        re.I), PROGRAMMING),
]

MAX_BRIEFS = 2          # two is plenty; more is just context tax


def match(text):
    """[(name, brief)] for the domains this text is actually about.

    Capped at MAX_BRIEFS, most specific first: "build me an online store landing
    page" should get LANDING + ECOMMERCE, not five overlapping briefs."""
    if not isinstance(text, str) or not text.strip():
        return []
    out = []
    for name, rx, brief in _BRIEFS:
        if rx.search(text):
            out.append((name, brief))
            if len(out) >= MAX_BRIEFS:
                break
    return out


def system_message(text):
    """One system message for `text`, or None when nothing applies."""
    hits = match(text)
    if not hits:
        return None
    body = "\n\n".join(b for _n, b in hits)
    return {"role": "system", "content": body}


def names(text):
    return [n for n, _b in match(text)]
