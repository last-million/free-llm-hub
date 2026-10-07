"""ECC skills: opt-in coding-agent rules vendored from github.com/affaan-m/ECC.

MIT, (c) 2026 Affaan Mustafa. The markdown lives in `skills_ecc/<name>.md`
(see `skills_ecc/VENDORED.md` for provenance and what was left out). This module
is the pure part (stdlib only, never raises): the catalog shown in Settings,
keyword matching, and a mtime-cached reader that strips each file's YAML
frontmatter before the body is injected.

The skills are OFF by default. app.py stores which ids are ON in the
`ecc_enabled` setting and hands them to craft.match() through the skill source,
so an enabled+matched ECC skill rides the opening turn exactly like a built-in
brief or a USER SKILL block -- every protocol, CLI, /agent brief file and crew.
A missing directory or unreadable file yields no hits (the off state).
"""
import os
import re
import threading

DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills_ecc")

MAX_PER_TURN = 2       # ECC skills added to one request (context tax)
MAX_CHARS = 1500       # injected block per skill, header included

CREDIT = "opt-in; MIT, (c) Affaan Mustafa -- github.com/affaan-m/ECC"

# (id, filename, display name, one-line description, trigger keywords). The id is
# "ecc:<upstream skill name>"; name/description/keywords are drawn from each
# skill's own frontmatter description (see skills_ecc/VENDORED.md). Keywords are
# matched as whole words/phrases, case-insensitively (never as a regex).
CATALOG = [
    ("ecc:tdd-workflow", "tdd-workflow.md", "TDD workflow (ECC)",
     "Test-driven development: write the test first, red-green-refactor, real coverage.",
     ("tdd", "test-driven", "test driven", "test first", "test coverage",
      "red-green-refactor", "write the test first")),
    ("ecc:verification-loop", "verification-loop.md", "Verification loop (ECC)",
     "Verify the work actually passes before claiming a task is complete.",
     ("verification loop", "verify the work", "before claiming", "self-verify",
      "double-check the", "make sure it works")),
    ("ecc:security-review", "security-review.md", "Security review (ECC)",
     "Security checklist for auth, user input, secrets, endpoints and payments.",
     ("security review", "authentication", "secrets", "vulnerability",
      "vulnerabilities", "secure", "owasp", "sensitive data")),
    ("ecc:coding-standards", "coding-standards.md", "Coding standards (ECC)",
     "Cross-project conventions: naming, readability, immutability, code-quality review.",
     ("coding standards", "code quality", "naming convention", "readability",
      "code review", "best practices")),
    ("ecc:agent-introspection-debugging", "agent-introspection-debugging.md",
     "Agent self-debugging (ECC)",
     "Structured self-debugging when a run fails, loops, or drifts off task.",
     ("debug", "debugging", "failing repeatedly", "stuck in a loop", "looping",
      "introspection", "diagnose the failure")),
    ("ecc:backend-patterns", "backend-patterns.md", "Backend patterns (ECC)",
     "Server-side patterns for Node.js, Express and Next.js API routes and data access.",
     ("backend", "server-side", "express", "api route", "database optimization",
      "node.js api")),
    ("ecc:frontend-patterns", "frontend-patterns.md", "Frontend patterns (ECC)",
     "React / Next.js component, state-management and render-performance patterns.",
     ("frontend", "react", "next.js", "state management", "render performance",
      "ui component")),
    ("ecc:api-design", "api-design.md", "API design (ECC)",
     "REST conventions: resource naming, status codes, pagination, versioning, rate limiting.",
     ("rest api", "api design", "endpoint", "status code", "pagination",
      "versioning", "rate limiting")),
    ("ecc:e2e-testing", "e2e-testing.md", "E2E testing (ECC)",
     "Playwright E2E patterns: page object model, CI integration, flaky-test strategies.",
     ("e2e", "end-to-end test", "playwright", "page object", "flaky test")),
]

IDS = tuple(c[0] for c in CATALOG)
_BY_ID = {c[0]: c for c in CATALOG}

_lock = threading.Lock()
_cache = {}            # filename -> (mtime, body)


def ids():
    return IDS


def _keyword_re(keywords):
    parts = [r"(?<!\w)%s(?!\w)" % re.escape(k) for k in keywords if k]
    return re.compile("|".join(parts), re.I) if parts else None


_MATCHERS = {c[0]: _keyword_re(c[4]) for c in CATALOG}


def matches(sid, text):
    """Does this ECC skill's keywords fire on `text`? (enabled state is separate)"""
    rx = _MATCHERS.get(sid)
    return bool(rx and isinstance(text, str) and rx.search(text))


def _strip_frontmatter(text):
    """Drop a leading `---\\n...\\n---` YAML block; return the instruction body."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            nl = text.find("\n", end + 1)
            if nl != -1:
                return text[nl + 1:].lstrip("\n")
    return text


def load_body(filename):
    """The frontmatter-stripped body of one vendored file, cached by mtime.

    "" on any error (missing dir/file, read failure) -- the off state."""
    path = os.path.join(DIR, filename)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    with _lock:
        hit = _cache.get(filename)
        if hit and hit[0] == mtime:
            return hit[1]
    try:
        with open(path, encoding="utf-8") as fh:
            body = _strip_frontmatter(fh.read()).strip()
    except OSError:
        return ""
    with _lock:
        _cache[filename] = (mtime, body)
    return body


def render(sid):
    """The bounded system-message block for one ECC skill, or "" if unreadable."""
    entry = _BY_ID.get(sid)
    if not entry:
        return ""
    body = load_body(entry[1])
    if not body:
        return ""
    header = "ECC SKILL: %s (%s)\n" % (entry[2], CREDIT)
    budget = MAX_CHARS - len(header)
    if len(body) > budget:
        cut = body[:budget - 6]
        nl = cut.rfind("\n")
        if nl > budget // 2:                 # keep a clean line boundary
            cut = cut[:nl]
        body = cut.rstrip() + "\n[...]"
    return header + body


def hits(text, enabled_ids):
    """[(id, block)] of the ENABLED ECC skills this text calls for, capped.

    `enabled_ids` is whatever app.py stored (a list/set of catalog ids); unknown
    ids are ignored. Catalog order, at most MAX_PER_TURN, each block bounded."""
    if not isinstance(text, str) or not text.strip() or not enabled_ids:
        return []
    enabled = set(enabled_ids)
    out = []
    for sid in IDS:
        if sid in enabled and matches(sid, text):
            block = render(sid)
            if block:
                out.append((sid, block))
                if len(out) >= MAX_PER_TURN:
                    break
    return out


def view(enabled_ids):
    """[{id, name, description, enabled}] for Settings; pure, no file reads."""
    enabled = set(enabled_ids or ())
    return [{"id": c[0], "name": c[2], "description": c[3], "enabled": c[0] in enabled}
            for c in CATALOG]
