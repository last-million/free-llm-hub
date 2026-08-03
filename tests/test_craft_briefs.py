"""Domain craft briefs, and the rules that keep them from doing harm.

Injected guidance is only worth its tokens if it fires on the right requests and
NEVER on the wrong ones — an unwanted brief costs context on a small free window
AND adds instructions the user did not ask for. It is also injected on the
OPENING turn only: adding instructions to a turn that carries tool_calls is what
breaks Codex/Claude Code agent loops.
"""
import pytest

import app
import craft


_ORTHO = {n for n, _rx, _b in craft._ORTHOGONAL}


def _domains(text):
    """Just the DOMAIN briefs. seo/images are orthogonal — they ride along on
    any request that ships a page, and are asserted on their own below."""
    return [n for n in craft.names(text) if n not in _ORTHO]


@pytest.mark.parametrize("text,expected", [
    ("Build me an online store landing page for solar panels", ["landing", "ecommerce"]),
    ("write the copy for our sales page", ["landing"]),
    ("add a shopping cart and stripe checkout", ["ecommerce"]),
    ("write a blog post and optimise it for SEO", []),
    ("redesign the hero section with tailwind", ["web_design"]),
    ("optimise the query in src/db.py", ["programming"]),
    ("refactor the auth module and write tests", ["programming"]),
])
def test_briefs_fire_on_their_own_domain(text, expected):
    assert _domains(text) == expected


@pytest.mark.parametrize("text", [
    "what is the capital of France",
    "explain how a mutex works",
    "design a database schema",          # 'design' alone must not mean web design
    "translate this to French",
    "",
])
def test_nothing_fires_on_unrelated_requests(text):
    """A brief on an unrelated ask is pure token cost plus unwanted instructions."""
    assert craft.names(text) == []


def test_at_most_two_domain_briefs():
    """'online store landing page with SEO and tests' could match five; more than
    two is context tax, not help.

    IMAGES is excluded from the count on purpose: it is not a competing domain
    view of the same task, it is a CAPABILITY the model cannot discover on its
    own (a free local generator). Ranked against the domains it would lose on
    exactly the requests that need it, which is how a build ended up announcing
    it had no image tool and falling back to Unsplash."""
    hits = _domains("build an ecommerce landing page with SEO, refactor and write tests")
    assert len(hits) <= craft.MAX_BRIEFS


def test_every_brief_names_what_NOT_to_do():
    """The ANTI sections are the point: models over-produce exactly the generic
    output these forbid."""
    for _name, _rx, body in craft._BRIEFS:
        assert "ANTI" in body, _name


def test_briefs_forbid_inventing_facts():
    """The failure that matters most in this domain: fabricated testimonials,
    metrics, prices and reviews."""
    allb = dict((n, b) for n, _r, b in craft._BRIEFS + craft._ORTHOGONAL)
    for name in ("landing", "ecommerce", "seo"):
        body = allb[name].lower()
        assert "invent" in body or "fabricat" in body, name


def test_briefs_stay_small():
    """Injected into a small free context window on every matching opening turn.

    web_design carries a larger allowance than the rest, and it earned it: it
    absorbed the named anti-pattern list (specific fonts and structures that read
    as AI-made, which a generic "avoid generic layouts" line never prevented),
    the hero-motion spec, and (on request) a numbered end-of-task self-check --
    every clickable thing verified to actually work, each page of a multi-page
    build confirmed distinct rather than reskinned, a real phone-width check.
    Those are concrete bans, concrete recipes and concrete checks, not
    style talk — the part a model cannot supply for itself. The real guard is
    test_worst_case_brief_cost below, which measures what a request ACTUALLY
    pays."""
    budget = {"web_design": 4500}
    for name, _rx, body in craft._BRIEFS:
        limit = budget.get(name, 1400)
        assert len(body) < limit, "%s brief is too long (%d chars)" % (name, len(body))
    # The orthogonal two are allowed to be larger: they carry runnable commands
    # and hard numbers (endpoint, WebP conversion, CWV thresholds, schema types
    # Google retired) rather than style guidance, and that is the part a model
    # cannot supply for itself. Still bounded — see test_worst_case_brief_cost.
    for name, _rx, body in craft._ORTHOGONAL:
        assert len(body) < 3600, "%s brief is too long (%d chars)" % (name, len(body))


def test_a_plain_coding_question_pays_nothing():
    """The property that keeps the whole scheme honest: briefs are free unless
    they apply."""
    for text in ("refactor this sorting function", "explain how a mutex works",
                 "what does this regex do", "write a python script to parse csv"):
        msg = craft.system_message(text)
        assert msg is None or "security" not in craft.names(text), text


def test_worst_case_brief_cost():
    """The whole point of MAX_BRIEFS: this must stay a small slice of even the
    smallest free context window the hub routes to (~32K)."""
    worst = max(
        len(craft.system_message(t)["content"])
        for t in ("build an online store and deploy it",
                  "create a landing page for my saas",
                  "build me a restaurant website"))
    # 11% of the SMALLEST window we route to, and only for the heaviest request
    # — a saas landing page, which legitimately pulls landing + web_design +
    # security + seo + images. Every one of those was asked for explicitly, and
    # capping the list silently dropped IMAGES from precisely the request that
    # produced "I don't have the image_gen tool available". Most models here run
    # 200K-1M; 32K is the floor. A simple coding question still pays nothing,
    # which is the property that actually keeps this honest.
    assert worst / 4 < 32768 * 0.11, "briefs cost ~%d tokens" % (worst // 4)


# --------------------------------------------------------------------------- #
# Injection safety
# --------------------------------------------------------------------------- #

def test_injected_on_the_opening_turn():
    msgs = [{"role": "system", "content": "You are Codex."},
            {"role": "user", "content": "build a landing page for my solar store"}]
    out = app._apply_craft_brief(msgs)
    assert len(out) == 3
    assert "LANDING PAGE" in out[1]["content"]


def test_the_callers_own_system_prompt_still_comes_first():
    """Codex's agent prompt must win on any conflict, so the brief goes AFTER it."""
    msgs = [{"role": "system", "content": "You are Codex."},
            {"role": "user", "content": "build a landing page"}]
    assert app._apply_craft_brief(msgs)[0]["content"] == "You are Codex."


def test_never_injected_mid_conversation():
    msgs = [{"role": "user", "content": "build a landing page"},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "make it better"}]
    assert app._apply_craft_brief(msgs) is msgs


def test_never_injected_into_a_running_tool_loop():
    """The failure mode that made the prompt enhancer dashboard-only."""
    msgs = [{"role": "user", "content": "build a landing page"},
            {"role": "assistant", "tool_calls": [{"function": {"name": "shell"}}]}]
    assert app._apply_craft_brief(msgs) is msgs


def test_unmatched_requests_are_returned_untouched():
    msgs = [{"role": "user", "content": "what is the capital of France"}]
    assert app._apply_craft_brief(msgs) is msgs


def test_multimodal_opening_turn_is_read_for_text():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "redesign this hero section"},
                                         {"type": "image_url", "image_url": {"url": "x"}}]}]
    assert len(app._apply_craft_brief(msgs)) == 2


def test_injection_never_raises_on_junk():
    for bad in (None, [], "nope", [None], [{"role": "user"}]):
        app._apply_craft_brief(bad)


# --------------------------------------------------------------------------- #
# SHIP — finish the job instead of stopping at "you can now deploy this"
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text", [
    "build a store and deploy it to vercel",
    "deploy this to production",
    "publish the landing page",
    "ship it",
    "set up hosting for this",
])
def test_ship_brief_fires_on_deployment_work(text):
    assert "ship" in craft.names(text)


@pytest.mark.parametrize("text", [
    "build me an online store",          # building is not deploying
    "explain how a mutex works",
])
def test_ship_brief_stays_out_of_non_deploy_work(text):
    assert "ship" not in craft.names(text)


def test_ship_means_LOCAL_not_cloud():
    """User: "I don't want Vercel, I just want local deployment." Cloud hosting
    needs an account and a browser login no agent can complete; node/npm/python
    are already on the machine."""
    body = craft.SHIP
    assert "RUNNING ON THIS MACHINE" in body
    assert "do not tell the user to sign up" in body.lower()
    assert "unless they explicitly ask for a public URL" in body


def test_ship_brief_names_the_local_run_paths():
    body = craft.SHIP
    for path in ("npm run dev", "npm start", "python -m http.server", "npx serve"):
        assert path in body, path


def test_ship_brief_requires_actually_fetching_the_running_site():
    """A start command that returns is not proof the site works."""
    body = craft.SHIP
    assert "curl" in body and "127.0.0.1" in body
    assert "not proof" in body


def test_ship_brief_requires_background_start_and_a_stop_instruction():
    """A foreground server hangs the agent's session for the rest of the task."""
    body = craft.SHIP.lower()
    assert "background" in body
    assert "how to stop it" in body
    assert "foreground server" in body


def test_ship_brief_forbids_the_observed_failures():
    body = craft.SHIP
    assert "you can now deploy this to Vercel" in body, "must forbid trailing off"
    assert "run npm start to see it" in body, "handing back instructions is the same failure"
    assert "inventing a URL" in body


# --------------------------------------------------------------------------- #
# IMAGES — the model announced "I don't have the image_gen tool available" and
# silently fell back to Unsplash, while a free local generator answered in ~3s.
# --------------------------------------------------------------------------- #

def test_images_brief_fires_for_a_build_with_no_image_word():
    """The case that actually bit: "build a restaurant website" names no image
    word at all, yet the whole page is pictures."""
    assert "images" in craft.names("build me a restaurant website with a menu")
    assert "images" in craft.names("create a landing page for my saas")
    assert "images" in craft.names("build an online store")


def test_images_brief_fires_on_explicit_image_words():
    for text in ("design a logo for my brand", "I need a hero image",
                 "generate some product photos", "make an icon set"):
        assert "images" in craft.names(text), text


def test_images_brief_stays_off_pure_code_tasks():
    """An unwanted brief is pure token cost on a small free context window."""
    for text in ("refactor this auth middleware", "write a python script to parse csv",
                 "fix the bug in my sorting function", "write unit tests for the parser"):
        assert "images" not in craft.names(text), text


def test_images_does_not_consume_a_domain_brief_slot():
    """Ranked as a domain it would lose every time — a website request already
    spends both MAX_BRIEFS slots on web_design/landing, which is exactly the
    request that needs it most."""
    assert "images" in craft.names("create a landing page for my saas")
    assert len(_domains("create a landing page for my saas")) == craft.MAX_BRIEFS


def test_images_brief_names_the_real_local_endpoint():
    """A brief that points at a URL the hub does not serve is worse than none."""
    assert "127.0.0.1:8787/v1/images/generations" in craft.IMAGES


def test_images_brief_does_not_hardcode_a_png_extension():
    """The hub's default free model (flux-1-schnell via cloudflare) returns
    JPEG. Saving those bytes as .png mislabels every generated file."""
    line = [l for l in craft.IMAGES.splitlines() if l.strip().startswith("python -c")]
    assert line, "the brief must carry a runnable save command"
    assert "'img/hero.png'" not in line[0]
    assert "b64_json" in line[0] and "url" in line[0], "must handle BOTH shapes"


def test_images_brief_offers_exactly_three_choices():
    for marker in ("1. FREE STOCK", "2. GENERATED", "3. BOTH"):
        assert marker in craft.IMAGES, marker


# --------------------------------------------------------------------------- #
# SEO — "websites should be optimised for SEO already", not only when asked.
# Rules adapted from AgriciDaniel/claude-seo (MIT).
# --------------------------------------------------------------------------- #

def test_seo_applies_to_every_site_build_without_being_asked():
    """USER 2026-08-01: websites must ship SEO-optimised already. Nobody says
    the word "SEO" when they ask for a restaurant website."""
    for text in ("build me a restaurant website", "create a landing page for my saas",
                 "build an online store", "start a blog"):
        assert "seo" in craft.names(text), text


def test_seo_stays_off_non_web_work():
    for text in ("refactor this auth middleware", "write a python script to parse csv",
                 "explain how a mutex works"):
        assert "seo" not in craft.names(text), text


def test_seo_does_not_recommend_retired_rich_results():
    """Google removed HowTo rich results in 2023 and retired FAQPage rich
    results for ALL sites on 2026-05-07. The brief used to recommend FAQ."""
    body = craft.SEO
    assert "Never HowTo" in body
    assert "QAPage" in body
    assert "FAQPage no longer earns a rich result" in body


def test_seo_carries_the_current_vitals_and_not_fid():
    """INP replaced FID on 2024-03-12; citing FID dates the whole brief."""
    body = craft.SEO
    for marker in ("LCP <=2.5s", "INP <=200ms", "CLS <0.1"):
        assert marker in body, marker
    assert "never cite FID" in body


def test_seo_requires_server_rendered_head_tags():
    """AI crawlers do not execute JavaScript."""
    assert "Server-render" in craft.SEO
    assert "do not run JavaScript" in craft.SEO


def test_seo_does_not_sell_llms_txt_as_a_ranking_lever():
    """Google states it neither helps nor harms; presenting it as SEO is wrong."""
    assert "llms.txt" in craft.SEO.lower()
    assert "ANTI" in craft.SEO


# --------------------------------------------------------------------------- #
# WebP
# --------------------------------------------------------------------------- #

def test_images_brief_requires_webp_for_stock_too():
    """Generated images arrive as WebP from the hub; downloaded ones do not."""
    body = craft.IMAGES
    assert "EVERY image ships as WebP" in body
    assert "WEBP" in body and "quality=82" in body


def test_images_brief_protects_the_lcp_image():
    """Lazy-loading the hero directly harms LCP — the most common real mistake."""
    body = craft.IMAGES
    assert 'fetchpriority="high"' in body
    assert 'NEVER be loading="lazy"' in body
    assert 'loading="lazy" decoding="async"' in body


# --------------------------------------------------------------------------- #
# Design rules adapted from pbakaus/impeccable (Apache-2.0), plus the user's
# motion and hero requirements.
# --------------------------------------------------------------------------- #

def test_the_overused_font_list_is_named():
    """"One family" never stopped a model reaching for Inter. Naming the
    training-data defaults is what makes the rule checkable."""
    body = craft.WEB_DESIGN
    for face in ("Inter", "Geist", "Space Grotesk", "Montserrat", "Playfair"):
        assert face in body, face


def test_the_font_ban_does_not_cause_broken_webfonts():
    """A small model told "not Inter" will happily name a face it cannot load."""
    assert "system stack" in craft.WEB_DESIGN
    assert "@font-face" in craft.WEB_DESIGN


def test_the_named_structural_tells_are_banned():
    body = craft.WEB_DESIGN.lower()
    for tell in ("icon tile above a heading", "eyebrow above a heading",
                 "cards inside cards", "01/02/03"):
        assert tell in body, tell


def test_the_category_self_check_is_present():
    """The sharpest rule in the source: it closes the loophole where an
    anti-slop brief just produces a DIFFERENT predictable look."""
    body = craft.WEB_DESIGN
    assert "category-plus-the-obvious-avoidance" in body
    assert "cream + serif" in body


def test_motion_is_required_everywhere_but_varied():
    """USER: "animations in almost each section and button". The counter-rule
    matters as much: one identical entrance on every section is itself a tell."""
    body = craft.WEB_DESIGN
    assert "Motion everywhere, but not the SAME motion everywhere" in body
    assert "hover/active/focus" in body


def test_content_is_visible_without_javascript():
    """The failure mode of animate-everything: a script error leaves a blank
    page because the content was hidden at rest waiting for an entrance."""
    assert "CONTENT MUST BE VISIBLE BY DEFAULT" in craft.WEB_DESIGN


def test_hero_video_is_licence_checked_and_looped():
    """USER: free footage in heroes, cut to a few seconds, looped."""
    body = craft.WEB_DESIGN
    assert "licence-free" in body and "seamless loop" in body
    assert "autoplay muted loop playsinline" in body
    assert "name the source" in body, "an unchecked 'free' clip is a licence risk"


def test_hero_3d_has_a_fallback_and_does_not_block_paint():
    body = craft.WEB_DESIGN
    assert "IntersectionObserver" in body
    assert "static fallback" in body
    assert "never block first paint" in body


def test_video_and_3d_are_exclusive():
    """Both at once is a heavy hero and a slow one."""
    assert "pick ONE, never both" in craft.WEB_DESIGN


# --------------------------------------------------------------------------- #
# SECURITY — designed for on the first turn, not bolted on afterwards.
# Checklist coverage informed by trailofbits/skills (CC BY-SA 4.0); the text is
# written independently, both because that licence is ShareAlike and because the
# repo has no web-application content at all.
# --------------------------------------------------------------------------- #

def test_security_fires_on_work_that_actually_needs_it():
    for text in ("add login and password reset to my saas",
                 "build me an online store with stripe checkout",
                 "let users upload a profile photo",
                 "add role-based permission to the admin panel",
                 "build an api endpoint that returns user data"):
        assert "security" in craft.names(text), text


def test_security_stays_off_work_that_does_not():
    for text in ("redesign the hero section with tailwind",
                 "write a blog post about pasta",
                 "explain how a mutex works",
                 "refactor this sorting function"):
        assert "security" not in craft.names(text), text


def test_security_does_not_consume_a_domain_slot():
    """"Build me a store with checkout" spends both slots on ecommerce/landing —
    which is exactly the build where auth and payments need designing first."""
    got = craft.names("build me an online store with stripe checkout")
    assert "security" in got
    assert len(_domains("build me an online store with stripe checkout")) <= craft.MAX_BRIEFS


def test_security_demands_a_threat_model_before_code():
    """The user asked for detailed security planning FROM THE BEGINNING."""
    body = craft.SECURITY
    assert "BEFORE you write the feature" in body
    assert "what an attacker gains" in body


def test_security_covers_the_web_basics_trailofbits_omits():
    """That repo has zero content on SQLi, XSS, CSRF, cookie flags, headers,
    upload validation or PCI — more than half of what a store needs."""
    body = craft.SECURITY.lower()
    for topic in ("parameterised queries", "csrf", "samesite", "httponly",
                  "content-security-policy", "cvv", "upload"):
        assert topic in body, topic


def test_security_names_the_fail_open_secret_pattern():
    """The single most common real leak in generated code."""
    body = craft.SECURITY
    assert "fail CLOSED" in body
    assert 'dev-secret' in body


def test_security_requires_a_password_kdf_not_a_hash():
    body = craft.SECURITY
    assert "Argon2id" in body
    assert "never MD5/SHA-1/SHA-256" in body


def test_security_forbids_client_side_price_trust():
    assert "never trust an amount from the client" in craft.SECURITY
