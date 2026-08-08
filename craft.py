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
- Motion everywhere, but not the SAME motion everywhere. Every section and every button should feel alive: hover/active/focus states on all controls, and an entrance for each section. Vary it — a section that slides, one that staggers its children, one that reveals on scroll, one with parallax — because one identical fade-and-rise on all of them is itself an AI tell. 150-300ms for state changes, up to 500ms for a section entrance, ease-out (cubic-bezier(0.16,1,0.3,1)), exit faster than entrance. Transform/opacity only (they run on the compositor; animating layout properties janks). Honour prefers-reduced-motion, and CONTENT MUST BE VISIBLE BY DEFAULT — JS enhances an entrance, it never gates whether the content exists, or a script error leaves a blank page. An installed GSAP/animation skill (hyperframes-animation, gsap) is a technique reference for eases/stagger/timelines/3D — never copy its data-*/class="clip" composition markup into a live page, that is for its own video renderer.
- Hero motion when the brief wants impact — pick ONE, never both:
  * VIDEO: a genuinely licence-free clip (Pexels/Pixabay/Coverr; check the clip's own licence and name the source). Cut 4-6s, seamless loop, under ~2MB: <video autoplay muted loop playsinline poster="still.webp">. Overlay to hold text contrast; fall back to the poster on prefers-reduced-motion and narrow screens.
  * 3D/CANVAS: a real generative scene in JS. One canvas, devicePixelRatio capped at 2, paused when hidden or scrolled out (IntersectionObserver), static fallback without WebGL, and it must never block first paint.
- Responsive: mobile-first, no horizontal scroll, tap targets >=44px, images with width/height so nothing shifts.
- Space above a heading must exceed the space below it — a heading binds to what follows.
- Elevation once: border OR shadow, never both. Shadows have an offset and a soft blur; a zero-offset coloured halo is decoration, not depth.
- Type scale must actually be a scale: largest at least 2x the smallest, ~1.25x between adjacent steps. Interactive text never below 11px.
- Multi-page: share the SYSTEM (type, colour, spacing, nav, footer), never the same hero/section layout with different words. Each page's composition reflects what makes THAT page different.
- Every clickable thing works, before you report done: no href="#", no dead button, no CTA opening nothing. Real anchor, real page, real submit, or real toggle. Missing real content (phone, address, price) is [NEEDS INPUT], never a link to nowhere.
SELF-CHECK before you report done — fix what fails, do not make the user catch it for you:
1. Could someone guess your look from the product category alone — or from category-plus-the-obvious-avoidance? cream + serif display, near-black + one neon glow, broadsheet hairlines + italic serif, when the brief left the look open, all fail this. Rework.
2. Click every link and button yourself. Anything dead or wrong?
3. Multi-page: flip through each page — same system, but does it still look distinct, not reskinned?
4. Phone-width viewport: anything overflow, overlap, clip, or fall under a 44px tap target?
5. Every section its own entrance, not one fade-and-rise pasted everywhere? Hover/active/focus on everything interactive?
ANTI (each of these is a specific, recognisable AI tell):
- Fonts: Inter, Geist, Plus Jakarta Sans, Space Grotesk, Instrument Sans/Serif, Fraunces, Recoleta, Playfair, DM Sans/Serif, Outfit, Syne, Montserrat. Pick a face with a point of view — but if you cannot verify the webfont actually loads, use a system stack rather than ship a broken @font-face.
- Structure: a rounded-square icon tile above a heading; a tiny tracked uppercase eyebrow above a heading; cards inside cards; 01/02/03 section numbers; a coloured border-left on cards or callouts; centred hero + 3 identical feature cards + gradient blob.
- Copy: "Elevate/Unlock/Seamless/Empower/supercharge/world-class/next-generation"; manufactured-contrast aphorisms ("Not a feature. A platform.", "X. Just Y.") — once is fine, three sections doing it is the tell.
- Also: gradient text, glassmorphism as decoration, emoji as icons, stock-photo grids, a testimonial or statistic you invented."""

SEO = """SEO BRIEF (build every page this way — do not wait to be asked for SEO)
- One page = one intent = one primary keyword. Do not write a page you cannot name the query for.
- Title 30-60 chars, term near the front, unique per page. Meta description 120-160 chars, written to earn the click, not to repeat the title. One H1, headings in real order, no skipped levels.
- Put the primary term in the title, H1, URL slug, meta description, first 100 words and at least one image alt — NOT in every H2/H3.
- Internal links: descriptive anchors, 3-5 per 1000 words, no orphan pages, important pages within 3 clicks of home. The same anchor must not point to two different URLs.
- Server-render title, meta description, canonical, robots and JSON-LD into the initial HTML. AI crawlers do not run JavaScript, and Google will not render JS on a non-200 response.
- Vitals: LCP <=2.5s, INP <=200ms, CLS <0.1 (75th percentile field data). INP replaced FID — never cite FID.
- Schema: JSON-LD only, absolute URLs, only for what is genuinely on the page (Article/Product/LocalBusiness/QAPage). Never HowTo (rich results removed 2023). FAQPage no longer earns a rich result (retired for all sites 2026-05-07) — use QAPage for genuine Q&A.
- Sitemap: <=50,000 URLs and <=50MB; drop <priority> and <changefreq> (Google ignores both); honest <lastmod>; never list noindex, redirected or non-canonical URLs.
- Written for AI answers as well as blue links: answer the question in the first 40-60 words of a section, keep self-contained ~150-word answer blocks near the top, question-based H2s, visible published + updated dates.
- E-E-A-T: name the author, cite sources, show real specifics. Google's test is Who / How / Why.
ANTI: keyword stuffing, thin doorway pages, invented statistics, fabricated reviews or ratings, schema that does not match visible content, selling llms.txt as a ranking lever (Google ignores it), "in today's fast-paced world" openers."""

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

SHIP = """FINISH THE JOB — DEPLOY LOCALLY (applies to any build/run/deploy task)
- "Deploy" here means RUNNING ON THIS MACHINE. Do not reach for Vercel/Netlify/cloud hosting, and do not tell the user to sign up for anything, unless they explicitly ask for a public URL.
- "Done" means it RUNS and you SAW it run. Install deps, start it, fetch it, report the real status code. Never hand over code you did not execute.
- Use what the project already has, in this order: its own script (npm run dev / npm start / pnpm dev), then the framework CLI (vite / next / astro), then a plain static server (npx serve . or python -m http.server PORT) for plain HTML/CSS/JS.
- Start it in the BACKGROUND so the shell is not blocked, then verify with a real request (curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:PORT). A start command that returns is not proof the site works.
- Print the exact local URL, plus how to stop it (the PID or the command). If the port is taken, pick a free one and say which.
- BLOCKED? One line: the exact blocker and the exact command that clears it. Never trail off mid-task.
ANTI: stopping at "you can now deploy this to Vercel" or "run npm start to see it"; claiming it works without fetching it; inventing a URL or a port you never opened; leaving a foreground server that hangs the session."""

IMAGES = """IMAGES — YOU HAVE A LOCAL GENERATOR, ASK FIRST (any task that will show images)
- Do NOT say you have no image tool and silently fall back to Unsplash. This gateway generates images locally, free, in ~3s. Never claim otherwise.
- The moment you know the build needs pictures, STOP and offer exactly three options, then wait for the answer (skip the question only if the user already told you which they want):
  1. FREE STOCK — real photos, no copyright issue (Unsplash/Pexels source URLs). Best for real faces, food, places, anything that must look authentically photographed.
  2. GENERATED — made here from your prompt, unique to this project, no attribution and no licence question. Best for hero art, backgrounds, illustrations, icons, textures, anything abstract or brand-specific.
  3. BOTH — stock for photographic content, generated for hero/abstract/brand art. Usually the right answer for a real site.
- Generate with a plain HTTP call, no API key. The hub returns WebP already and reports the type in `mime`:
  curl -s -X POST http://127.0.0.1:8787/v1/images/generations -H "Content-Type: application/json" -d '{"prompt":"YOUR PROMPT","n":1,"size":"1024x1024"}' > out.json
  python -c "import json,base64,urllib.request as u;d=json.load(open('out.json'))['data'][0];s=d.get('b64_json') or '';b=base64.b64decode(s) if s else u.urlopen(d['url']).read();e=(d.get('mime') or '').split('/')[-1].replace('jpeg','jpg') or 'webp';p='img/hero.'+e;open(p,'wb').write(b);print(p)"
- EVERY image ships as WebP, generated or stock. Generated ones already are. Convert anything you download (pip install Pillow if needed):
  python -c "from PIL import Image;import sys;i=Image.open(sys.argv[1]);i.convert('RGBA' if 'A' in i.getbands() else 'RGB').save(sys.argv[2],'WEBP',quality=82)" in/photo.jpg img/photo.webp
  A plain <img src="*.webp"> is correct — every current browser takes it. Add a <picture> fallback only if the project must support pre-2020 browsers.
- Omit "model" and the hub picks the best free one available. Write real files into the project (img/ or assets/) and reference them with relative paths.
- Ship every <img> correctly, this is where image SEO is actually won:
  width + height on every one (or CSS aspect-ratio) so nothing shifts; alt 10-125 chars describing the content, alt="" for purely decorative; descriptive-hyphenated-lowercase filenames (blue-running-shoes.webp, never IMG_1234); srcset + sizes at 400w/800w/1200w when the image is large.
  The hero/LCP image gets fetchpriority="high" and must NEVER be loading="lazy" — lazy-loading above the fold directly harms LCP. Everything below the fold gets loading="lazy" decoding="async".
  Size budget: thumbnails <50KB, content images <100KB, hero <200KB.
- Prompt like a photographer, not a keyword list: subject, setting, light, lens/mood. "warm rustic italian dining room, evening window light, shallow depth of field" beats "restaurant image nice".
ANTI: hotlinking a stock URL you never checked resolves; source.unsplash.com/random (it is retired and returns nothing); the same generic gradient for every section; inventing a photographer credit; shipping a page whose images 404."""

_IMAGES_RE = re.compile(
    r"\bimages?\b|\bphotos?\b|\bpictures?\b|\billustrations?\b|\blogos?\b|"
    r"\bicons?\b|\bbanners?\b|\bthumbnails?\b|\bavatars?\b|\bartwork\b|"
    r"\bvisuals?\b|\bgaller(?:y|ies)\b|\bhero (?:section|image|banner)\b|"
    # Build tasks that always end up needing pictures — this is the case that
    # actually bit: "build a restaurant website" names no image word at all, and
    # the model announced it had no image tool and reached for Unsplash.
    r"\bweb ?site\b|\bwebpage\b|\bhomepage\b|\blanding page\b|\bportfolio\b|"
    r"\bonline store\b|\bstorefront\b|\be-?commerce\b|\bblog\b|\brestaurant\b",
    re.I)

SECURITY = """SECURITY BRIEF — decide this BEFORE you write the feature, not after
- First, in one line each: what an attacker gains, which data is worth stealing, which input a hostile user controls. Then design against three people — one who turns security off via configuration, one who copies the first example they find, one who swaps two arguments and gets no type error. If the insecure path is the easy path, the design is wrong.
- Secrets fail CLOSED: os.environ["KEY"], never .get("KEY", "dev-secret") or process.env.X || "fallback". Missing config stops the app at boot instead of running on a default everyone knows. Never in code, the repo, or a log line.
- Passwords: Argon2id (or scrypt/bcrypt). A fast hash is NOT a password KDF — never MD5/SHA-1/SHA-256, never "encrypt and store". Reject over-long passwords rather than silently truncating them.
- Sessions: regenerate the session id on every privilege change (login, logout, role change). Ids from a CSPRNG, never a timestamp or a counter. Never accept a session id from the request. Cookies: HttpOnly, Secure, SameSite=Lax (Strict for admin), a real expiry.
- Tokens: hardcode ONE signing algorithm and verify against it — never let the token name its own (alg:none and RS256->HS256 confusion are both full auth bypasses). Reset/OTP tokens are single-use and short-lived.
- Compare secrets in constant time (hmac.compare_digest / crypto.timingSafeEqual). Never == on a token, MAC, signature or password hash.
- Authorization deny-by-default, in ONE place. Never read a record's owner from the request (?user_id=). Never match roles by substring. Check ownership on every read AND write — the classic hole is a locked-down list endpoint beside an unchecked detail endpoint.
- Injection: parameterised queries only, never string-built SQL. Escape on OUTPUT by context (HTML/attr/JS/URL); prefer a template engine that escapes by default. Never eval, new Function, setTimeout(string), yaml.load, unserialize, shell=True, or unpickling untrusted data. Block __proto__/constructor/prototype in any object merge.
- CSRF: a token or SameSite on every state-changing request. CORS: an explicit origin allowlist — never "*", and never "*" together with credentials.
- Uploads: validate type by CONTENT not extension, cap size, store outside the webroot under a generated name, serve with Content-Disposition and nosniff.
- Payments: never store a card number or CVV. Use hosted fields/tokens (Stripe Elements or equivalent) so card data never reaches your server. Recompute every price and total server-side — never trust an amount from the client.
- Rate-limit auth, reset and payment endpoints. Uniform errors: one "invalid credentials" for both unknown user and wrong password. Never return a stack trace to a client; log it server-side and return a generic 500.
- Ship the headers: Content-Security-Policy, HSTS, X-Content-Type-Options: nosniff, Referrer-Policy. Dependencies pinned with a committed lockfile.
ANTI: "we will add auth later"; a TODO where a permission check belongs; logging passwords, tokens, card numbers or full session ids; disabling TLS verification to make something work; inventing your own crypto or your own token format; leaving debug mode, stack traces, GraphQL introspection or a seeded admin password enabled in production."""

# Checklist coverage informed by trailofbits/skills (CC BY-SA 4.0). The text above
# is written from scratch rather than adapted, deliberately: that repo is
# ShareAlike, and it also has NO web-application content at all (no SQLi, XSS,
# CSRF, cookie flags, security headers, upload validation or PCI), so more than
# half of what a store or SaaS actually needs had to come from elsewhere anyway.
_SECURITY_RE = re.compile(
    r"\bauth(?:entication|orization|entify)?\b|\blog ?in\b|\bsign ?up\b|\bpassword\b|"
    r"\bsession\b|\bjwt\b|\btoken\b|\boauth\b|\bsso\b|\bpermission\b|\brole-based\b|"
    r"\bpayment\b|\bcheckout\b|\bstripe\b|\bbilling\b|\bsubscription\b|\bcredit card\b|"
    r"\bupload\b|\buser data\b|\bpersonal data\b|\bgdpr\b|\bsecurity\b|\bsecure\b|"
    r"\bvulnerab\w*\b|\bxss\b|\bcsrf\b|\bsql ?injection\b|\bencrypt\w*\b|"
    r"\bapi (?:endpoint|key)\b|\badmin panel\b|\bdashboard for users\b|"
    r"\bonline store\b|\bstorefront\b|\be-?commerce\b|\bsaas\b|\bmulti-?tenant\b",
    re.I)

_SHIP_RE = re.compile(
    r"\bdeploy\w*\b|\bship it\b|\bgo live\b|\bpublish\b|\bhost(?:ing)?\b|"
    r"\bvercel\b|\bnetlify\b|\bcloudflare pages\b|\bproduction\b|"
    r"\brun (?:it|the (?:app|site|project|server))\b|\bserve (?:it|the)\b|"
    r"\bpreview\b|\blocalhost\b|\bstart the (?:app|server|site)\b", re.I)

_BRIEFS = [
    ("ship", _SHIP_RE, SHIP),
    ("landing", re.compile(
        r"\blanding page\b|\bsales page\b|\bfunnel\b|\bconversion rate\b|\bcta\b|\bopt[- ]?in page\b", re.I),
     LANDING),
    ("ecommerce", re.compile(
        r"\be-?commerce\b|\bonline store\b|\bstorefront\b|\bshopping cart\b|\bcheckout\b|"
        r"\bproduct page\b|\bshopify\b|\bstripe checkout\b", re.I), ECOMMERCE),
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

MAX_BRIEFS = 2          # two DOMAIN briefs is plenty; more is just context tax
# A CEILING on orthogonal briefs, currently a no-op at 3 (all of them), and that
# is deliberate. "Build a landing page for my saas" matches all three and costs
# ~3,200 tokens — 10% of the SMALLEST window we route to. Capping it at 2 was
# tried and reverted: it silently dropped IMAGES from exactly the request that
# produced "I don't have the image_gen tool available", and each of these three
# was asked for explicitly. 10% is the worst case on the worst window, on the
# most complex build; most models here have 200K-1M. The cap exists so a FOURTH
# orthogonal brief cannot be added without someone re-deciding this tradeoff.
MAX_ORTHOGONAL = 3

# ORTHOGONAL briefs. These are not competing views of the same task, so they do
# not fight the domain briefs for a slot — they ride along whenever they apply.
#
# Both earned it the same way: ranked as domains they lose exactly when they are
# needed most. "Build a restaurant website" spends both slots on
# web_design/landing, and that is the request where the model announced it had
# no image tool and reached for Unsplash, and where nobody said the word "SEO"
# yet every page still has to rank. The user's instruction is explicit: images
# always WebP, websites SEO-optimised already.
#
# Their triggers therefore include plain build nouns, not just topic words. Pure
# code tasks ("refactor the auth module") still match neither and pay nothing.
_SITE_NOUNS = (r"\bweb ?site\b|\bwebpage\b|\bhomepage\b|\blanding page\b|"
               r"\bportfolio\b|\bonline store\b|\bstorefront\b|\be-?commerce\b|"
               r"\bblog\b|\brestaurant\b")

_SEO_RE = re.compile(
    r"\bseo\b|\bsearch engine optimi[sz]\w*\b|\bserp\b|\bmeta description\b|"
    r"\bkeyword research\b|\bschema markup\b|\bbacklink\b|"
    r"\brank(?:ing)?\b|\bsitemap\b|\bcore web vitals\b|" + _SITE_NOUNS, re.I)

_ORTHOGONAL = [
    # Security rides along rather than competing for a domain slot, for the same
    # reason images and seo do: "build me a store with checkout" spends both
    # slots on ecommerce/landing, and that is precisely the build where auth,
    # payments and user data need to be designed for on the FIRST turn. Bolting
    # security on afterwards is the failure this exists to prevent.
    ("security", _SECURITY_RE, SECURITY),
    ("seo", _SEO_RE, SEO),
    ("images", _IMAGES_RE, IMAGES),
]


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
    extra = 0
    for name, rx, brief in _ORTHOGONAL:
        if rx.search(text):
            out.append((name, brief))
            extra += 1
            if extra >= MAX_ORTHOGONAL:
                break
    return out


# --------------------------------------------------------------------------- #
# THE LOOP. Every brief above is a one-shot SPEC: what to produce, and what not
# to. None of them tells the model to CHECK its own output and go again, which
# is the whole of loop engineering -- plan, act, check, fix, stop.
#
# It lives HERE rather than inside the eight brief bodies for three reasons:
# eight copies would cost ~5,000 chars against caps with 21-38 chars of
# headroom; it is generic, so duplicating it adds no information; and every
# brief already carries an ANTI section, which the tool-less branch reuses as
# its rubric. It borrows criteria instead of shipping its own -- that is why it
# fits. Living in system_message() also leaves match()/names() byte-identical,
# so no routing test is disturbed.
#
# TWO VARIANTS, chosen STRUCTURALLY on whether the caller sent tools -- never by
# asking the model to self-assess. A model with no shell, told "run it and paste
# the output", invents a terminal transcript, and a fabricated verification is
# strictly worse than no instruction at all.
#
# Both are BOUNDED. Unbounded "keep going until it's right" is the named failure
# mode of agent loops, so each caps the retries and says what to do when the cap
# is hit: hand over the failure list, which IS the deliverable.
VERIFY_RUN = """VERIFY, FIX, STOP (applies to every brief above)
- Name the ONE check that catches you being wrong: a command, a URL and its expected status, or a file and the string it must contain.
- Run it. Paste the real output. Never write output you did not get back from a real run; if you cannot run it, write "not run" and name the missing tool.
- Failed? Fix and re-run THAT check, twice at most. Still failing: stop and list what is broken -- that list is the deliverable."""

VERIFY_READ = """VERIFY, FIX, STOP (applies to every brief above)
- No shell, browser or file access here: never say you ran, tested, opened or verified anything. A verification you did not perform is the worst answer possible.
- Instead: re-read your output against every ANTI line above, name the 3 you came closest to breaking and the one-line fix for each. Two rounds, max.
- Then list the checks you could NOT run, as commands with their pass condition, and say what is unresolved."""

# ACT is the loop's OTHER end -- a break BEFORE verify even starts, not covered
# by VERIFY_RUN. OBSERVED LIVE 2026-08-08: an agentic session's last message
# was "Now run the full deep verification across all modules..." and the turn
# ended right there -- no tool call, nothing run, just the announcement. User
# confirmed this recurs across different models, not one model's quirk.
# Narrow trigger on purpose: "you did something this turn AND your own last
# line names the very next call" is the one pattern actually observed. A
# broader "if nothing stops you, just do it" would also fire on a genuinely
# open decision (which real option, anything destructive) where stopping to
# state it and wait is the CORRECT behavior, not the bug.
ACT_RUN = """ACT (applies to every brief above, comes before VERIFY/FIX/STOP)
- If you already called a tool this turn and your own last sentence names the exact next call you now have everything needed to make, make it -- do not end the turn to announce it. Stating "next I'll..." about something already in reach is a stop dressed as a plan.
- This does not cover a genuinely open decision (which real option, anything destructive/irreversible, anything you are missing information for) -- state those and wait, same as always."""


def system_message(text, tools=True):
    """One system message for `text`, or None when nothing applies.

    `tools` says whether the CALLER can actually execute a check. It selects
    WHICH verify block ships -- never whether one ships: a tool-less client
    still gets a real, tool-free verifier (re-read against the ANTI lines) plus
    an explicit ban on claiming an execution it cannot perform. ACT ships only
    for a tool-carrying caller -- a tool-less one has nothing to "just call"
    instead of narrating."""
    hits = match(text)
    if not hits:
        return None
    # The loop goes LAST: it says "every brief above" and "every ANTI line
    # above", and both references dangle if it is prepended.
    tail = [ACT_RUN, VERIFY_RUN] if tools else [VERIFY_READ]
    body = "\n\n".join([b for _n, b in hits] + tail)
    return {"role": "system", "content": body}


def names(text):
    return [n for n, _b in match(text)]
