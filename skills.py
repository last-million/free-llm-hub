"""Skills: the hub's built-in briefs (craft.py) plus the user's own, named ones.

A SKILL here is a block of instructions the hub adds as a system message on the
opening turn of a request whose text calls for it -- exactly how the craft
briefs have always shipped. This module is the pure part: the built-in catalog
shown in Settings, validation of user skills, and keyword matching. Storage is
config.py (`skills_disabled`: built-in ids switched off, `custom_skills`: the
user's list); app.py registers the source with craft.set_skill_source() so
craft.match() honours both.

User keywords are matched as plain words (re.escape, word-bounded, any case),
never as a regex the user typed: a pasted pattern cannot blow up the router.
"""
import re
import uuid

# (id, name, what it does). Ids are craft.py's brief names; "last30days" is the
# vendored agent skill in .agents/skills/, gated through /api/web-search-policy.
BUILTIN = [
    ("web_design", "Web design",
     "Premium layout, typography and spacing rules for any site or page; bans generic AI-looking layouts."),
    ("landing", "Landing pages & funnels",
     "Conversion structure for landing, sales and opt-in pages: one goal, clear CTA, proof."),
    ("ecommerce", "E-commerce",
     "Store, product page, cart and checkout conventions."),
    ("programming", "Engineering",
     "Refactor, debug, tests, migrations: small verified changes, no guessing."),
    ("ship", "Run & deploy",
     "Finish the job: start the project locally in the background and hand over the URL."),
    ("security", "Security",
     "Auth, payments and user data designed safely on the first turn."),
    ("seo", "SEO",
     "Every page built to rank: titles, meta, headings, schema, sitemap."),
    ("images", "Images",
     "Use the hub's local image generator (WebP) instead of stock-photo links."),
    ("last30days", "last30days web research",
     "Agent skill for research on the last 30 days (web, YouTube, public Reddit)."),
]
BUILTIN_IDS = tuple(b[0] for b in BUILTIN)

MAX_CUSTOM = 20            # skills a user can save
MAX_CUSTOM_PER_TURN = 3    # user skills added to one request (context tax)
MAX_NAME = 60
MAX_INSTRUCTIONS = 4000
MAX_KEYWORDS = 20
MAX_KEYWORD_LEN = 40
TRIGGERS = ("keywords", "always")


class SkillError(ValueError):
    """A user skill that cannot be saved; the message is shown as-is."""


def _clean_keywords(raw):
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, (list, tuple)):
        raise SkillError("keywords must be a comma-separated list.")
    out = []
    for k in raw:
        if not isinstance(k, str):
            raise SkillError("each keyword must be text.")
        k = " ".join(k.split())
        if not k:
            continue
        if len(k) > MAX_KEYWORD_LEN:
            raise SkillError("keyword '%s...' is longer than %d characters."
                             % (k[:20], MAX_KEYWORD_LEN))
        if k.lower() not in (x.lower() for x in out):
            out.append(k)
    if len(out) > MAX_KEYWORDS:
        raise SkillError("at most %d keywords per skill." % MAX_KEYWORDS)
    return out


def validate(body, existing=()):
    """A clean skill dict from a request body, or SkillError.

    `existing` is the saved list; an `id` in the body that names one of them is
    an update (its id is kept), anything else is a new skill with a fresh id."""
    if not isinstance(body, dict):
        raise SkillError("invalid JSON body.")
    name = body.get("name")
    if not isinstance(name, str) or not " ".join(name.split()):
        raise SkillError("give the skill a name.")
    name = " ".join(name.split())
    if len(name) > MAX_NAME:
        raise SkillError("the name is longer than %d characters." % MAX_NAME)
    text = body.get("instructions")
    if not isinstance(text, str) or not text.strip():
        raise SkillError("write the skill's instructions.")
    text = text.strip()
    if len(text) > MAX_INSTRUCTIONS:
        raise SkillError("instructions are longer than %d characters." % MAX_INSTRUCTIONS)
    trigger = body.get("trigger", "keywords")
    if trigger not in TRIGGERS:
        raise SkillError("trigger must be 'keywords' or 'always'.")
    keywords = _clean_keywords(body.get("keywords", []))
    if trigger == "keywords" and not keywords:
        raise SkillError("add at least one keyword, or choose 'always'.")
    enabled = body.get("enabled", True)
    if not isinstance(enabled, bool):
        raise SkillError("enabled must be true or false.")
    ids = {s.get("id") for s in existing if isinstance(s, dict)}
    sid = body.get("id") if body.get("id") in ids else None
    for s in existing:
        if (isinstance(s, dict) and s.get("id") != sid
                and str(s.get("name", "")).lower() == name.lower()):
            raise SkillError("a skill named '%s' already exists." % name)
    if sid is None and len(existing) >= MAX_CUSTOM:
        raise SkillError("at most %d skills; delete one first." % MAX_CUSTOM)
    return {"id": sid or uuid.uuid4().hex[:12], "name": name, "trigger": trigger,
            "keywords": keywords, "instructions": text, "enabled": enabled}


def upsert(existing, skill):
    """The saved list with `skill` replacing its id, or appended."""
    out = [s for s in existing if isinstance(s, dict)]
    for i, s in enumerate(out):
        if s.get("id") == skill["id"]:
            out[i] = skill
            return out
    return out + [skill]


def _keyword_re(keywords):
    parts = [r"(?<!\w)%s(?!\w)" % re.escape(k) for k in keywords if k]
    return re.compile("|".join(parts), re.I) if parts else None


def matches(skill, text):
    if not isinstance(skill, dict) or not skill.get("enabled", True):
        return False
    if skill.get("trigger") == "always":
        return True
    rx = _keyword_re(skill.get("keywords") or ())
    return bool(rx and isinstance(text, str) and rx.search(text))


def render(skill):
    """The system-message block for one user skill."""
    return ("USER SKILL: %s (the user's own instructions -- apply unless they say "
            "otherwise)\n%s" % (skill.get("name", "skill"), skill.get("instructions", "")))


def custom_hits(text, custom):
    """[(name, block)] of the user skills this text calls for, capped."""
    out = []
    for s in custom or ():
        if matches(s, text):
            out.append(("custom:%s" % s.get("id"), render(s)))
            if len(out) >= MAX_CUSTOM_PER_TURN:
                break
    return out
