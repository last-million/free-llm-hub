"""No AI slop in web work: prevented in the design, checked in the output.

Owner, 2026-10-04: "In web design: no AI slop, no AI watermark/tells --
perfect. And slop must be prevented from the BEGINNING, in planning and in
designing the architecture, not only caught at the end."

Two halves, both pure (stdlib only, no model call, no command; the only I/O
is reading the files a caller names):

- check_design(design_text, request=""): a design spec or a plan, BEFORE any
  code -- the slop CHOICES it makes (the default purple->blue gradient hero,
  "modern clean minimal" with nothing concrete, the stock hero + 3 cards +
  testimonials + CTA skeleton) and the decisions it never made (no hex
  palette, no type pairing, no layout concept, no real copy source).
  plan_check runs it on every web plan; a high finding earns the planner's
  one re-ask, so the plan is fixed before a helper writes a line.
- check_files(paths_or_texts, kind=None): the built pages (HTML, CSS,
  JSX/TSX, Vue/Svelte, Markdown copy).

A finding is {"rule", "severity": "high"|"medium"|"low", "where":
"file:line" or "design", "why", "fix"}. Every rule is tuned to stay SILENT on
a crafted page (real copy, a named palette, paired faces, alt text, readable
contrast) -- tests/test_slopcheck.py runs each one both ways. When unsure a
rule says nothing: a false alarm teaches the reader to ignore the list.
"""
import colorsys
import os
import re
from collections import Counter

SEVERITIES = ("high", "medium", "low")
_WEIGHT = {"high": 15, "medium": 7, "low": 2}      # first finding of a rule
_EXTRA = {"high": 3, "medium": 1, "low": 0}        # each further one (<= 4)
MAX_PER_RULE = 3              # findings one rule files per file
MAX_FILE_BYTES = 2_000_000
_SKIP_PATH_RE = re.compile(
    r"(?:^|[\\/])(?:node_modules|vendor|bower_components|\.git)(?:[\\/]|$)|\.min\.(?:js|css)$", re.I)

_KIND_BY_EXT = {
    ".html": "html", ".htm": "html", ".xhtml": "html",
    ".css": "css", ".scss": "css", ".sass": "css", ".less": "css", ".pcss": "css",
    ".jsx": "jsx", ".tsx": "jsx",
    ".js": "script", ".mjs": "script", ".cjs": "script", ".ts": "script",
    ".vue": "vue", ".svelte": "vue", ".astro": "vue",
    ".md": "md", ".mdx": "md", ".markdown": "md", ".txt": "md",
}
_COPY_KINDS = ("html", "vue", "jsx", "md")
_MARKUP_KINDS = ("html", "vue", "jsx")


def _f(rule, severity, where, why, fix):
    return {"rule": rule, "severity": severity, "where": where, "why": why, "fix": fix}


def _line(text, pos):
    return text.count("\n", 0, max(0, pos)) + 1


def _blank(m):
    """The match as spaces, newlines kept: offsets and line numbers survive."""
    return re.sub(r"[^\n]", " ", m.group(0))


# --------------------------------------------------------------------------- #
# Colour: parsing and WCAG contrast
# --------------------------------------------------------------------------- #

_NAMED = {
    "black": "000000", "white": "ffffff", "red": "ff0000", "green": "008000",
    "blue": "0000ff", "yellow": "ffff00", "orange": "ffa500", "purple": "800080",
    "violet": "ee82ee", "indigo": "4b0082", "fuchsia": "ff00ff", "magenta": "ff00ff",
    "pink": "ffc0cb", "cyan": "00ffff", "aqua": "00ffff", "teal": "008080",
    "navy": "000080", "gray": "808080", "grey": "808080", "silver": "c0c0c0",
    "maroon": "800000", "olive": "808000", "lime": "00ff00", "brown": "a52a2a",
    "gold": "ffd700", "beige": "f5f5dc", "ivory": "fffff0", "lavender": "e6e6fa",
    "plum": "dda0dd", "orchid": "da70d6", "blueviolet": "8a2be2",
    "mediumpurple": "9370db", "rebeccapurple": "663399", "slateblue": "6a5acd",
    "darkviolet": "9400d3", "darkorchid": "9932cc", "mediumslateblue": "7b68ee",
    "royalblue": "4169e1", "dodgerblue": "1e90ff", "deepskyblue": "00bfff",
    "skyblue": "87ceeb", "steelblue": "4682b4", "cornflowerblue": "6495ed",
    "hotpink": "ff69b4", "deeppink": "ff1493", "crimson": "dc143c",
    "tomato": "ff6347", "coral": "ff7f50", "salmon": "fa8072", "khaki": "f0e68c",
    "lightgray": "d3d3d3", "lightgrey": "d3d3d3", "darkgray": "a9a9a9",
    "darkgrey": "a9a9a9", "dimgray": "696969", "dimgrey": "696969",
    "gainsboro": "dcdcdc", "whitesmoke": "f5f5f5", "snow": "fffafa",
    "linen": "faf0e6", "darkblue": "00008b", "midnightblue": "191970",
    "darkslateblue": "483d8b", "mediumblue": "0000cd", "lightblue": "add8e6",
}
_NUM = r"[-+]?\d*\.?\d+%?"
_COLOR_TOKEN_RE = re.compile(
    r"#[0-9a-fA-F]{3,8}(?![\w-])|(?:rgba?|hsla?)\(\s*[^()]*\)|(?<![\w-])(?:"
    + "|".join(sorted(_NAMED, key=len, reverse=True)) + r"|transparent)(?![\w-])", re.I)


def _channel(v, scale=255.0):
    v = v.strip()
    if v.endswith("%"):
        return max(0.0, min(1.0, float(v[:-1]) / 100.0)) * 255.0
    return max(0.0, min(scale, float(v))) * (255.0 / scale)


def _alpha(v):
    v = (v or "1").strip()
    if v.endswith("%"):
        return max(0.0, min(1.0, float(v[:-1]) / 100.0))
    return max(0.0, min(1.0, float(v)))


def parse_color(value):
    """(r, g, b, a) with r/g/b in 0-255 and a in 0-1, or None when the value is
    not a plain colour (a gradient, var(), currentColor, oklch...)."""
    if isinstance(value, (tuple, list)) and len(value) in (3, 4):
        r, g, b = (float(x) for x in value[:3])
        return (r, g, b, float(value[3]) if len(value) == 4 else 1.0)
    s = str(value or "").strip().lower().replace("!important", "").strip()
    if not s:
        return None
    if s == "transparent":
        return (0.0, 0.0, 0.0, 0.0)
    if s in _NAMED:
        s = "#" + _NAMED[s]
    try:
        if s.startswith("#"):
            h = s[1:]
            if not re.fullmatch(r"[0-9a-f]{3,8}", h) or len(h) in (5, 7):
                return None
            if len(h) in (3, 4):
                h = "".join(c * 2 for c in h)
            a = int(h[6:8], 16) / 255.0 if len(h) == 8 else 1.0
            return (float(int(h[0:2], 16)), float(int(h[2:4], 16)), float(int(h[4:6], 16)), a)
        m = re.fullmatch(r"(rgba?|hsla?)\(\s*(.*?)\s*\)", s)
        if not m:
            return None
        parts = [p for p in re.split(r"[\s,/]+", m.group(2)) if p]
        if len(parts) not in (3, 4):
            return None
        a = _alpha(parts[3]) if len(parts) == 4 else 1.0
        if m.group(1).startswith("rgb"):
            return (_channel(parts[0]), _channel(parts[1]), _channel(parts[2]), a)
        hue = float(re.sub(r"deg$", "", parts[0])) % 360 / 360.0
        sat = float(parts[1].rstrip("%")) / 100.0
        lig = float(parts[2].rstrip("%")) / 100.0
        r, g, b = colorsys.hls_to_rgb(hue, max(0.0, min(1.0, lig)), max(0.0, min(1.0, sat)))
        return (r * 255.0, g * 255.0, b * 255.0, a)
    except (ValueError, IndexError):
        return None


def _luminance(rgb):
    def lin(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb[:3]
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def contrast_ratio(fg, bg):
    """WCAG 2.x contrast ratio of text `fg` on `bg` (hex, rgb()/hsl(), a
    named colour or an (r, g, b[, a]) tuple), 1.0-21.0. A translucent `fg` is
    composited over `bg` first. Raises ValueError for a value it cannot read."""
    f, b = parse_color(fg), parse_color(bg)
    if f is None or b is None:
        raise ValueError("not a plain colour: %r / %r" % (fg, bg))
    if f[3] < 1.0:
        f = tuple(f[i] * f[3] + b[i] * (1.0 - f[3]) for i in range(3)) + (1.0,)
    l1, l2 = _luminance(f), _luminance(b)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def _hsv(c):
    h, s, v = colorsys.rgb_to_hsv(c[0] / 255.0, c[1] / 255.0, c[2] / 255.0)
    return h * 360.0, s, v


def _family(c):
    """'violet' / 'blue' / 'pink' / 'other' for a saturated colour, None for a
    near-neutral one (greys, near-black, near-white wash)."""
    h, s, v = _hsv(c)
    if s < 0.25 or v < 0.25:
        return None
    if 235 <= h < 300:
        return "violet"          # indigo, violet, purple
    if 195 <= h < 235:
        return "blue"
    if 300 <= h < 345:
        return "pink"            # fuchsia, magenta, hot pink
    return "other"


def _ai_gradient_stops(stops):
    """The default AI hero: a violet-family stop with a blue / pink /
    violet-family partner and NOTHING outside that cool band (a sunset that
    passes through purple is not this)."""
    fams = [f for f in (_family(c) for c in stops if c and c[3] > 0.2) if f]
    return ("violet" in fams and len(fams) >= 2 and "other" not in fams
            and (len(set(fams)) >= 2 or fams.count("violet") >= 2))


def _gradients(value):
    """The inside of every *-gradient(...) in a CSS value (balanced parens)."""
    out, s = [], value or ""
    for m in re.finditer(r"(?:repeating-)?(?:linear|radial|conic)-gradient\(", s, re.I):
        depth, i = 1, m.end()
        while i < len(s) and depth:
            depth += {"(": 1, ")": -1}.get(s[i], 0)
            i += 1
        out.append(s[m.end():i - 1])
    return out


def _gradient_is_ai(value):
    for inner in _gradients(value):
        stops = [parse_color(t.group(0)) for t in _COLOR_TOKEN_RE.finditer(inner)]
        if _ai_gradient_stops([c for c in stops if c]):
            return True
    return False


_TW_GRAD_RE = re.compile(r"(?<![\w-])(?:[a-z]+:)*(from|via|to)-(purple|violet|indigo|fuchsia|blue|sky|pink|"
                         r"cyan|teal|emerald|green|lime|yellow|amber|orange|red|rose|slate|gray|zinc|"
                         r"neutral|stone|white|black)-?(\d{2,3})?(?![\w-])")
_TW_FAMILY = {"purple": "violet", "violet": "violet", "indigo": "violet", "fuchsia": "pink",
              "pink": "pink", "blue": "blue", "sky": "blue"}


def _tailwind_ai_gradient(classes):
    """`bg-gradient-to-r from-indigo-500 via-purple-500 to-pink-500`."""
    if not re.search(r"(?<![\w-])bg-(?:gradient|linear)-", classes):
        return False
    fams = []
    for m in _TW_GRAD_RE.finditer(classes):
        name = m.group(2)
        if name in ("slate", "gray", "zinc", "neutral", "stone", "white", "black"):
            continue
        fams.append(_TW_FAMILY.get(name, "other"))
    return ("violet" in fams and "other" not in fams and len(fams) >= 2
            and (len(set(fams)) >= 2 or fams.count("violet") >= 2))


# --------------------------------------------------------------------------- #
# CSS: a small, forgiving rule parser
# --------------------------------------------------------------------------- #

class _Rule:
    __slots__ = ("selector", "decls", "pos", "media", "keyframes")

    def __init__(self, selector, decls, pos, media, keyframes):
        self.selector, self.decls, self.pos = selector, decls, pos
        self.media, self.keyframes = media, keyframes


def _decls(body):
    out = {}
    for seg in body.split(";"):
        if "{" in seg or "}" in seg or ":" not in seg:
            continue
        prop, _, val = seg.partition(":")
        prop = prop.strip().lower()
        val = re.sub(r"!\s*important", "", val).strip()
        if prop and val and re.fullmatch(r"-{0,2}[a-z][a-z0-9-]*", prop):
            out[prop] = val
    return out


def _parse_css(css, base=0):
    """[_Rule] with absolute offsets. Nesting, @media / @supports blocks and
    @keyframes are followed; anything unreadable is skipped, never raised."""
    s = re.sub(r"/\*.*?\*/", _blank, css or "", flags=re.S)
    rules, stack, start = [], [], 0
    for i, ch in enumerate(s):
        if ch == "{":
            stack.append((s[start:i].strip(), i, start))
            start = i + 1
        elif ch == "}":
            if stack:
                prelude, open_i, pstart = stack.pop()
                if prelude and not prelude.startswith("@"):
                    outer = [p for p, _o, _s in stack]
                    if not any(p.lower().startswith(("@keyframes", "@-webkit-keyframes")) for p in outer):
                        body = s[open_i + 1:i]
                        lead = len(s[pstart:open_i]) - len(s[pstart:open_i].lstrip())
                        rules.append(_Rule(prelude, _decls(body), base + pstart + lead,
                                           [p for p in outer if p.startswith("@")], False))
            start = i + 1
        elif ch == ";":
            start = i + 1
    return rules


def _decl_rule(decl_text, pos, selector):
    return _Rule(selector, _decls(decl_text), pos, [], False)


# --------------------------------------------------------------------------- #
# Reading the inputs
# --------------------------------------------------------------------------- #

_TAG_RE = re.compile(
    r"<([a-zA-Z][\w:.-]*)((?:[^>\"'{]|\"[^\"]*\"|'[^']*'|\{(?:[^{}]|\{[^{}]*\})*\})*)>", re.S)
_CLASS_ATTR_RE = re.compile(
    r"(?<![\w-])(?:class|className)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|\{\s*[\"'`]([^\"'`]*)[\"'`]\s*\})")
_STYLE_ATTR_RE = re.compile(r"(?<![\w-])style\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")
_JSX_STYLE_RE = re.compile(r"(?<![\w-])style\s*=\s*\{\{(.*?)\}\}", re.S)
_JSX_ATTR_RE = re.compile(
    r"(?<![\w-])(?:className|class|style|href|src|srcSet|id|key|placeholder|type|name|to|rel|"
    r"target|htmlFor|role|xmlns|viewBox|d|fill|stroke|width|height|as|variant|size|"
    r"data-[\w-]+|aria-[\w-]+)\s*=\s*(?:\"[^\"]*\"|'[^']*'|\{(?:[^{}]|\{[^{}]*\})*\})")


class _Doc:
    def __init__(self, name, text, kind):
        self.name, self.text, self.kind = name, text, kind
        self.code = re.sub(r"<script\b.*?</script\s*>|<!--.*?-->", _blank, text, flags=re.S | re.I) \
            if kind in ("html", "vue") else text
        self.visible = _visible(text, kind)
        self.tags = [(m.group(1), m.group(2) or "", m.start())
                     for m in _TAG_RE.finditer(self.code)] if kind in _MARKUP_KINDS else []
        self.rules = []
        for base, chunk, wrapped in _css_chunks(text, kind):
            self.rules += _parse_css(chunk, base) if not wrapped else \
                [_decl_rule(chunk, base, "styled")]
        self.inline = []          # (tag, attrs, _Rule)
        self.classes = []         # (class string, pos)
        for tag, attrs, pos in self.tags:
            for m in _CLASS_ATTR_RE.finditer(attrs):
                self.classes.append((m.group(1) or m.group(2) or m.group(3) or "", pos))
            m = _STYLE_ATTR_RE.search(attrs)
            if m:
                self.inline.append((tag, attrs, _decl_rule(m.group(1) or m.group(2) or "",
                                                           pos, "%s[style]" % tag)))
            m = _JSX_STYLE_RE.search(attrs)
            if m:
                pairs = ["%s: %s" % (re.sub(r"([A-Z])", lambda c: "-" + c.group(1).lower(), k), v)
                         for k, v in re.findall(r"([A-Za-z]+)\s*:\s*[\"']([^\"']+)[\"']", m.group(1))]
                self.inline.append((tag, attrs, _decl_rule("; ".join(pairs), pos, "%s[style]" % tag)))

    def where(self, pos):
        return "%s:%d" % (self.name, _line(self.text, pos))


def _visible(text, kind):
    """The copy a reader sees, same length and line breaks as `text`."""
    if kind in ("html", "vue"):
        t = re.sub(r"<script\b.*?</script\s*>|<style\b.*?</style\s*>|<!--.*?-->", _blank,
                   text, flags=re.S | re.I)
        return re.sub(r"<[^>]*>", _blank, t, flags=re.S)
    if kind == "jsx":
        t = re.sub(r"/\*.*?\*/", _blank, text, flags=re.S)
        t = re.sub(r"(?<![:\"'\w])//[^\n]*", _blank, t)
        t = re.sub(r"^[ \t]*(?:import|export\s+\{)[^\n]*", _blank, t, flags=re.M)
        return _JSX_ATTR_RE.sub(_blank, t)
    if kind == "md":
        t = re.sub(r"```.*?```|~~~.*?~~~|<!--.*?-->", _blank, text, flags=re.S)
        return re.sub(r"`[^`\n]*`", _blank, t)
    return ""


def _css_chunks(text, kind):
    """(offset, css, is_declaration_list) for every stylesheet in a file."""
    if kind == "css":
        return [(0, text, False)]
    out = []
    if kind in _MARKUP_KINDS:
        out += [(m.start(1), m.group(1), False)
                for m in re.finditer(r"<style\b[^>]*>(.*?)</style\s*>", text, re.S | re.I)]
    if kind in ("jsx", "script", "vue"):
        for m in re.finditer(r"(?:\bcss|createGlobalStyle|styled(?:\.\w+|\([^()]*\)))\s*`([^`]*)`", text):
            body = m.group(1)
            out.append((m.start(1), body, "{" not in body))
    return out


def _sniff(text):
    head = text[:6000]
    low = head.lower()
    if "classname=" in low or re.search(r"\bfrom\s+['\"]react['\"]|\bimport\s+react\b", low):
        return "jsx"
    if "<template" in low and "<script" in low:
        return "vue"
    if re.search(r"<(?:!doctype|html|head|body|div|section|main|header|nav|footer|p|h[1-6]|img|a|ul)\b", low):
        return "html"
    if "<" not in head and re.search(r"[^{}\n]+\{[^{}]*:[^{}]*\}", head):
        return "css"
    return "md"


def _looks_like_path(s):
    return ("\n" not in s and len(s) < 1024 and s.strip() == s and bool(s)
            and os.path.isfile(s))


def _read(path):
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES or b"\x00" in data[:4096]:
            return None
        return data.decode("utf-8", errors="replace")
    except OSError:
        return None


def _inputs(paths_or_texts):
    """[(name, text)]. Accepts one path or text, a dict {name: text}, or a list
    of paths, texts, (name, text) pairs or {"name"/"path", "text"} dicts."""
    if paths_or_texts is None:
        return []
    if isinstance(paths_or_texts, dict) and not ({"text", "path"} & set(paths_or_texts)):
        return [(str(k), v if isinstance(v, str) else str(v or "")) for k, v in paths_or_texts.items()]
    if isinstance(paths_or_texts, (str, bytes, os.PathLike, dict)):
        items = [paths_or_texts]
    else:
        items = list(paths_or_texts)
    out = []
    for i, it in enumerate(items):
        label = "text" if len(items) == 1 else "text[%d]" % (i + 1)
        name, text = None, None
        if isinstance(it, bytes):
            it = it.decode("utf-8", errors="replace")
        if isinstance(it, dict):
            name = str(it.get("name") or it.get("path") or label)
            text = it.get("text")
            if text is None and it.get("path") and os.path.isfile(str(it["path"])):
                text = _read(str(it["path"]))
        elif isinstance(it, (tuple, list)) and len(it) == 2:
            name, text = str(it[0]), it[1]
        elif isinstance(it, os.PathLike) or (isinstance(it, str) and _looks_like_path(it)):
            name = os.fspath(it)
            if _SKIP_PATH_RE.search(name.replace("\\", "/")):
                continue
            text = _read(name)
        else:
            name, text = label, str(it or "")
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        if text is not None:
            out.append((name, str(text)))
    return out


def _kind_of(name, text, kind):
    if kind:
        k = str(kind).lower().lstrip(".")
        return {"htm": "html", "tsx": "jsx", "js": "script", "ts": "script", "svelte": "vue",
                "scss": "css", "markdown": "md", "mdx": "md"}.get(k, k)
    ext = os.path.splitext(name)[1].lower()
    k = _KIND_BY_EXT.get(ext)
    if k == "script" and re.search(r"className=|<[A-Z]\w*[\s/>]|return\s*\(\s*<", text):
        return "jsx"
    return k or _sniff(text)


# --------------------------------------------------------------------------- #
# Copy rules (visible text)
# --------------------------------------------------------------------------- #

_AI_COPY = [
    (r"\belevate your\b", "Elevate your"),
    (r"\bunlock (?:the (?:full )?(?:power|potential)|your (?:full |true )?potential)\b", "Unlock the power"),
    (r"\bseamless(?:ly)?\b", "seamless(ly)"),
    (r"\bin today'?s (?:fast[- ]paced|digital|ever[- ]changing|modern|competitive)\b", "In today's fast-paced"),
    (r"\brevolutioni[sz](?:e|es|ed|ing)\b", "Revolutionize"),
    (r"\bcutting[- ]edge\b", "cutting-edge"),
    (r"\bgame[- ]chang(?:er|ing)\b", "game-changer"),
    (r"\b(?:take|takes|taking) (?:your|it) \w+(?: \w+)? to the next level\b", "to the next level"),
    (r"\bharness the power\b", "harness the power"),
    (r"\bsupercharge\w*\b", "supercharge"),
    (r"\bunleash\w*\b", "unleash"),
    (r"\blook no further\b", "look no further"),
    (r"\bempower(?:s|ing)? (?:you|your|businesses|teams|creators)\b", "empower your"),
    (r"\bworld[- ]class\b", "world-class"),
    (r"\bnext[- ]generation\b", "next-generation"),
    (r"\bstate[- ]of[- ]the[- ]art\b", "state-of-the-art"),
    (r"\bbest[- ]in[- ]class\b", "best-in-class"),
]
_AI_COPY_RE = [(re.compile(p, re.I), label) for p, label in _AI_COPY]
_WELCOME_RE = re.compile(r"\bwelcome to\b", re.I)

# (pattern, label, flags). "Your Company" is matched only where it stands
# as a NAME (a copyright line, alone on a line, "... Name") -- "grow your
# business" is ordinary copy.
_PLACEHOLDER = [
    (r"\blorem ipsum\b|\bdolor sit amet\b|\bconsectetur adipiscing\b", "lorem ipsum", re.I),
    (r"(?:©|&copy;|\(c\)|copyright)\s*(?:\d{4}\s*)?Your (?:Company|Business|Brand)\b"
     r"|\bYour (?:Company|Business|Brand) Name\b|\bCompany Name\b|\bYourCompany\b|\bYour Logo\b"
     r"|^[ \t]*Your (?:Company|Business|Brand)[ \t]*$|\byour name here\b", "Your Company", re.M),
    (r"\b(?:insert|add|your) (?:text|content|title|headline|tagline|image) here\b"
     r"|\b(?:text|content|title|headline|tagline|image|copy|description|logo|name) goes here\b",
     "'... goes here' text", re.I),
    (r"\b(?:john|jane) (?:doe|smith)\b", "John Doe", re.I),
    (r"\bacme(?: corp(?:oration)?| inc\.?| co\.?)?\b", "Acme", re.I),
    (r"\b[\w.+-]+@(?:example|domain|yourdomain|yoursite|yourcompany|company|email|test|website)"
     r"\.(?:com|org|net|fr)\b", "placeholder email", re.I),
    (r"\(?\b555\)?[-. ]\d{3}[-. ]\d{4}\b|\b555[-. ]\d{4}\b|\b123[-. ]456[-. ]7890\b"
     r"|\(123\)\s?456[-. ]7890|\+?\s?1?\s?\(?123\)?[-. ]?456[-. ]?789\d?\b"
     r"|\b0?1[ .]23[ .]45[ .]67[ .]89\b|\b0{3}[-. ]0{3}[-. ]0{4}\b", "placeholder phone", 0),
    (r"\[(?:your|insert|company|placeholder|client)[^\]\n]{0,30}\]", "[Your ...] bracket", re.I),
]
_PLACEHOLDER_RE = [(re.compile(p, fl), label) for p, label, fl in _PLACEHOLDER]
_NUMBERED_RE = re.compile(
    r"\b(feature|service|item|product|benefit|card|title|heading|testimonial|client|member|"
    r"category)\s+(?:#\s?)?([1-9])\b", re.I)

_STAT_RE = re.compile(
    r"(?<![\w.,])(\d{1,3}(?:[,.\s]\d{3})+|\d+(?:\.\d)?\s?[kKmM](?![a-z])|\d{2,})\s?(\+?)\s*"
    r"(?:happy |satisfied |active |loyal |global )?"
    r"(?:customers|clients|users|projects|companies|businesses|teams|downloads|members|"
    r"students|countries|reviews|installs|developers|brands|partners|orders)\b", re.I)
_PCT_RE = re.compile(r"(?<![\d.])(9\d|100)(?:\.\d)?\s?%\s+(?:\w+\s+)?"
                     r"(?:satisfaction|satisfied|uptime|success|accuracy|retention|happy)", re.I)
_SOURCED_RE = re.compile(
    r"<cite\b|\bsource\s*:|\bsources?\b.{0,20}\bhttps?://|\baccording to\b|\bas of (?:19|20)\d\d\b|"
    r"\bdata from\b|\[needs input\]|\btrustpilot\b|\bg2\b|\bgoogle reviews\b|\bverified (?:review|purchase)",
    re.I)
_TESTIMONIAL_CTX_RE = re.compile(r"testimonial|\breviews?\b|<blockquote\b|\bquote\b", re.I)
_LLM_NAMES_RE = re.compile(
    r"\b(?:sarah (?:johnson|chen|mitchell|williams)|michael (?:chen|brown|rodriguez)|"
    r"emily (?:rodriguez|chen|davis|watson)|david (?:kim|chen|park|thompson)|jessica (?:lee|williams|taylor)|"
    r"alex (?:thompson|rivera|morgan|johnson)|james (?:wilson|carter)|maria (?:garcia|rodriguez)|"
    r"priya (?:patel|sharma)|marcus (?:johnson|chen)|lisa (?:wang|chen)|jennifer (?:lee|walsh))\b", re.I)
# "Sarah M." is NOT here: real reviews use initials for privacy too.
_FAKE_CO_RE = re.compile(r"\b(?:techcorp|techstart|innovateco|startupx\w*|globex|initech|growthco|"
                         r"brightpath|company inc|nexus corp|cloudsync|dataflow inc)\b", re.I)


def _copy_rules(doc):
    out = []
    v = doc.visible
    if not v.strip():
        return out
    hits = []
    for rx, label in _AI_COPY_RE:
        m = rx.search(v)
        if m:
            hits.append((m.start(), label))
    for pos, label in sorted(hits)[:MAX_PER_RULE]:
        out.append(_f("ai_copy", "medium", doc.where(pos),
                      'AI copy tell: "%s"' % label,
                      "say the specific thing this product does, in the owner's words"))
    m = _WELCOME_RE.search(v)
    if m:
        out.append(_f("ai_copy", "low", doc.where(m.start()), 'AI copy tell: "Welcome to"',
                      "open with what the visitor gets, not a greeting"))
    ph = []
    for rx, label in _PLACEHOLDER_RE:
        m = rx.search(v)
        if m:
            ph.append((m.start(), label))
    for m in re.finditer(r"\b(?:href|src)\s*=\s*[\"'](?:mailto:|tel:)?([^\"']+)[\"']", doc.code if
                         doc.kind in _MARKUP_KINDS else ""):
        val = m.group(1)
        if re.search(r"@(?:example|domain|yourdomain|company|email)\.|^https?://(?:www\.)?example\.(?:com|org)"
                     r"|^\+?1?5550|555-?\d{4}$|1234567890", val, re.I):
            ph.append((m.start(), "placeholder link %s" % val[:40]))
            break
    nums = {}
    for m in _NUMBERED_RE.finditer(v):
        nums.setdefault(m.group(1).lower(), []).append((m.start(), m.group(2)))
    for word, seen in nums.items():
        if len({n for _p, n in seen}) >= 2:
            ph.append((seen[0][0], '"%s 1/2/3"' % word.capitalize()))
            break
    for pos, label in sorted(ph)[:MAX_PER_RULE]:
        out.append(_f("placeholder", "high", doc.where(pos), "placeholder left in: %s" % label,
                      "use the owner's real name, contact and copy, or mark it [NEEDS INPUT]"))
    sourced = bool(_SOURCED_RE.search(doc.text))
    if not sourced:
        stat = None
        for m in _STAT_RE.finditer(v):
            if _is_round(m.group(1), m.group(2)):
                stat = m
                break
        stat = stat or _PCT_RE.search(v)
        if stat:
            out.append(_f("fake_proof", "medium", doc.where(stat.start()),
                          'round statistic with no source: "%s"' % " ".join(stat.group(0).split())[:50],
                          "cite where the number comes from, or remove it ([NEEDS INPUT])"))
        if _TESTIMONIAL_CTX_RE.search(doc.text):
            m = _LLM_NAMES_RE.search(v) or _FAKE_CO_RE.search(v)
            if m:
                out.append(_f("fake_proof", "medium", doc.where(m.start()),
                              'testimonial reads invented: "%s"' % m.group(0).strip()[:40],
                              "quote a real customer with where it was said, or leave [NEEDS INPUT]"))
    return out


def _is_round(number, plus=""):
    """"500+", "10k", "10,000" are round; "1,247" and "37" are not."""
    if plus or re.search(r"[kKmM]$", number.strip()):
        return True
    n = re.sub(r"\D", "", number)
    return len(n) >= 3 and n.endswith("00")


# --------------------------------------------------------------------------- #
# Tells in the markup itself
# --------------------------------------------------------------------------- #

_AI_TOOLS = r"(?:ai|a\.i\.|chatgpt|chat gpt|gpt-?\d*(?:\.\d)?|openai|claude(?: code)?|gemini|copilot|" \
            r"v0(?:\.dev)?|lovable|bolt(?:\.new)?|cursor|windsurf|replit agent|framer ai|wix adi|" \
            r"10web|relume|uizard|galileo ai)"
# "Powered by AI" is left alone: on an AI product it is the product's own
# copy. "made BY Claude" is left alone: Claude is also a person's name.
_CREDIT_RE = re.compile(
    r"\b(?:generated|written)\s+(?:by|with|using)\s+(?:the help of\s+)?" + _AI_TOOLS + r"(?![\w-])|"
    r"\b(?:built|made|created|designed|crafted)\s+(?:with|using)\s+(?:the help of\s+)?"
    + _AI_TOOLS + r"(?![\w-])|"
    r"\b(?:built|made|created|designed|crafted)\s+by\s+(?:an?\s+)?(?:ai|a\.i\.|chatgpt|gpt-?\d*|openai|"
    r"v0|lovable|bolt|copilot)(?![\w-])|\bas an ai(?: language)? model\b|"
    r"\bedit with lovable\b|lovable-badge|\bdata-lov-id\b|\bmade in bolt\b", re.I)
_GENERATOR_RE = re.compile(r"<meta\b[^>]*\bname\s*=\s*[\"']?generator[\"']?[^>]*>", re.I)
_EMOJI = (r"(?:[\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F900-\U0001FAFF\U0001F1E6-\U0001F1FF"
          r"☀-☄☇-⛿✀-✒✗-➿⭐⭕⬛⬜]️?)")
_EMOJI_ICON_RE = re.compile(
    r"<(?:nav|button|a|li|h[1-6]|span|div|i|dt|label|summary|th|td|strong|b|p)\b[^>]*>\s*(" + _EMOJI
    + r")|\bicon\s*[:=]\s*[\"'`{]\s*(" + _EMOJI + ")", re.I)


def _markup_rules(doc):
    out = []
    raw = doc.text
    m = _CREDIT_RE.search(raw)
    if m:
        out.append(_f("ai_credit", "high", doc.where(m.start()),
                      'AI credit / watermark: "%s"' % " ".join(m.group(0).split())[:60],
                      "remove it -- the page credits the owner, not the tool"))
    for g in _GENERATOR_RE.finditer(raw):
        content = re.search(r"\bcontent\s*=\s*[\"']([^\"']*)", g.group(0), re.I)
        val = content.group(1) if content else ""
        ai = re.search(r"(?<![\w-])" + _AI_TOOLS + r"(?![\w-])", val, re.I)
        out.append(_f("ai_credit", "high" if ai else "low", doc.where(g.start()),
                      'generator meta tag: "%s"' % val[:40],
                      "delete the <meta name=\"generator\"> tag"))
        break
    if doc.kind not in _MARKUP_KINDS:
        return out
    if doc.kind == "html" and re.search(r"<html\b|<head\b|<!doctype", raw, re.I) \
            and not re.search(r"<meta\b[^>]*\bname\s*=\s*[\"']?viewport", raw, re.I):
        head = re.search(r"<head\b|<html\b|<!doctype", raw, re.I)
        out.append(_f("no_viewport", "high", doc.where(head.start() if head else 0),
                      "no <meta name=\"viewport\">: phones render the desktop page shrunk",
                      'add <meta name="viewport" content="width=device-width, initial-scale=1">'))
    emoji = [m.start(1) if m.group(1) else m.start(2) for m in _EMOJI_ICON_RE.finditer(doc.code)]
    for nav in re.finditer(r"<nav\b.*?</nav\s*>", doc.code, re.S | re.I):
        emoji += [nav.start() + e.start() for e in re.finditer(_EMOJI, nav.group(0))]
    emoji = sorted(set(emoji))
    if len(emoji) >= 2:
        out.append(_f("emoji_icons", "medium", doc.where(emoji[0]),
                      "emoji used as icons (%d in nav / buttons / list items)" % len(emoji),
                      "use one consistent SVG icon set (Lucide, Phosphor, Heroicons) or none"))
    missing, empty, dead = [], [], []
    for tag, attrs, pos in doc.tags:
        t = tag.lower()
        if t == "img" or tag == "Image":          # <image> is SVG: no alt there
            if "{..." in attrs.replace(" ", ""):
                continue
            hidden = re.search(r"aria-hidden\s*=\s*[\"'{]?\s*true|role\s*=\s*[\"'](?:presentation|none)", attrs, re.I)
            if not re.search(r"(?<![\w-])alt\s*=", attrs):
                if not hidden:
                    missing.append(pos)
            elif re.search(r"(?<![\w-])alt\s*=\s*(?:\"\s*\"|'\s*'|\{\s*[\"']\s*[\"']\s*\})", attrs) and not hidden:
                if _CONTENT_IMG_RE.search(attrs) and not _DECOR_IMG_RE.search(attrs):
                    empty.append(pos)
        if t == "a" and re.search(r"(?<![\w-])href\s*=\s*(?:\"(?:#|javascript:void\(0\);?)?\"|'(?:#|javascript:void\(0\);?)?')", attrs, re.I):
            dead.append(pos)
    if missing:
        out.append(_f("img_alt", "high", doc.where(missing[0]),
                      "%d image(s) with no alt attribute" % len(missing),
                      "describe what each image shows (alt=\"\" only for pure decoration)"))
    if empty:
        out.append(_f("img_alt", "medium", doc.where(empty[0]),
                      "%d content image(s) with an empty alt" % len(empty),
                      "a photo of the product / team / dish needs real alt text"))
    if dead:
        out.append(_f("dead_link", "medium", doc.where(dead[0]),
                      '%d link(s) to nowhere (href="#")' % len(dead),
                      "link a real page or anchor, or make it a <button> that does something"))
    return out


_CONTENT_IMG_RE = re.compile(
    r"photo|product|team|portrait|hero|gallery|screenshot|founder|chef|dish|menu|project|"
    r"headshot|staff|interior|food|room|property|case-?stud|cover", re.I)
_DECOR_IMG_RE = re.compile(
    r"(?<![a-z])(?:bg|background|decor\w*|pattern|texture|shape|blob|divider|ornament|spacer|"
    r"wave|noise|grain|gradient|separator|flourish|icon)(?![a-z])", re.I)


# --------------------------------------------------------------------------- #
# Style rules (CSS, inline styles, Tailwind classes)
# --------------------------------------------------------------------------- #

_HERO_SEL_RE = re.compile(r"hero|header|banner|masthead|jumbotron|splash|landing|intro|cover|"
                          r"(?:^|[\s,>])(?:body|html|main|:root|h1)(?=$|[\s,.:#>\[])", re.I)
_SKIP_CONTRAST_SEL_RE = re.compile(r"::?placeholder|:disabled|\[disabled\]|\.disabled|sr-only|"
                                   r"visually-hidden|::-webkit|::-moz|::selection", re.I)
_MEDIA_EL_RE = re.compile(r"(?:^|[\s,>+~])(?:img|video|canvas|svg|iframe|picture|table|pre|code)"
                          r"(?=$|[\s,.:#>\[])", re.I)
_GENERIC_FONTS = {
    "inter", "inter var", "inter variable", "roboto", "arial", "helvetica", "helvetica neue",
    "system-ui", "-apple-system", "blinkmacsystemfont", "segoe ui", "sans-serif", "serif",
    "ui-sans-serif", "ui-serif", "open sans", "noto sans", "apple color emoji",
    "segoe ui emoji", "segoe ui symbol", "noto color emoji", "liberation sans", "ubuntu",
    "cantarell", "oxygen", "fira sans", "droid sans", "inherit", "initial", "unset", "times",
    "times new roman"}
_MONO_FONTS = {"monospace", "ui-monospace", "sfmono-regular", "sf mono", "menlo", "monaco",
               "consolas", "liberation mono", "courier new", "courier"}


def _root_vars(docs):
    out = {}
    for d in docs:
        for r in d.rules:
            if r.media or not re.search(r":root|(?:^|[\s,])(?:html|body)(?=$|[\s,{:.\[])", r.selector):
                continue
            for k, v in r.decls.items():
                if k.startswith("--") and k not in out:
                    out[k] = v
    return out


def _resolve(value, rvars, depth=0):
    if "var(" not in (value or "") or depth > 5:
        return value

    def sub(m):
        name, fb = m.group(1), m.group(2)
        if name in rvars:
            return rvars[name]
        return fb.strip() if fb else m.group(0)
    return _resolve(re.sub(r"var\(\s*(--[\w-]+)\s*(?:,\s*([^()]*(?:\([^()]*\))?[^()]*))?\)", sub, value),
                    rvars, depth + 1)


def _bg_color(decls, rvars):
    if "background-color" in decls:
        return parse_color(_resolve(decls["background-color"], rvars))
    bg = _resolve(decls.get("background") or "", rvars)
    if not bg or re.search(r"gradient\(|url\(", bg, re.I):
        return None
    m = _COLOR_TOKEN_RE.search(bg)
    return parse_color(m.group(0)) if m else None


def _all_rules(doc):
    return [(r, r.selector) for r in doc.rules] + [(r, r.selector) for _t, _a, r in doc.inline]


def _style_rules(doc, rvars):
    out = []
    contrast, widths, grads = [], [], []
    for r, sel in _all_rules(doc):
        d = r.decls
        # contrast: colour and background set by the SAME rule (conservative)
        if "color" in d and ("background-color" in d or "background" in d) \
                and not _SKIP_CONTRAST_SEL_RE.search(sel):
            fg = parse_color(_resolve(d["color"], rvars))
            bg = _bg_color(d, rvars)
            if fg and bg and fg[3] > 0.05 and bg[3] >= 0.99:
                ratio = contrast_ratio(fg, bg)
                if ratio < 4.5:
                    contrast.append((r.pos, sel, ratio, d["color"], d.get("background-color") or d.get("background")))
        # fixed widths outside any media query
        if not r.media and not _MEDIA_EL_RE.search(" " + sel) and "max-width" not in d:
            for prop in ("width", "min-width"):
                m = re.fullmatch(r"(\d+(?:\.\d+)?)px", (d.get(prop) or "").strip())
                if m and float(m.group(1)) >= 600:
                    widths.append((r.pos, sel, prop, d[prop]))
        for prop in ("background", "background-image"):
            if prop in d and _gradient_is_ai(_resolve(d[prop], rvars)):
                grads.append((r.pos, sel))
    for pos, sel, ratio, fg, bg in contrast[:MAX_PER_RULE]:
        out.append(_f("contrast", "high" if ratio < 3.0 else "medium", doc.where(pos),
                      "text contrast %.2f:1 (%s on %s) in %s, under 4.5:1"
                      % (ratio, fg, bg, sel.strip()[:40]),
                      "darken the text or lighten the background until it reaches 4.5:1"))
    for pos, sel, prop, val in widths[:MAX_PER_RULE]:
        out.append(_f("fixed_width", "medium", doc.where(pos),
                      "%s: %s on %s breaks phones" % (prop, val, sel.strip()[:40]),
                      "use max-width with width:100% (or a fluid unit) instead"))
    for cls, pos in doc.classes:
        m = re.search(r"(?<![\w:-])(?:min-)?w-\[(\d+)px\]", cls)
        if m and int(m.group(1)) >= 600 and len(widths) < MAX_PER_RULE:
            widths.append((pos, "", "", ""))
            out.append(_f("fixed_width", "medium", doc.where(pos),
                          "fixed %spx width class breaks phones" % m.group(1),
                          "use max-w-* with w-full instead"))
        if _tailwind_ai_gradient(cls):
            grads.append((pos, "class"))
    for pos, sel in grads[:1]:
        hero = sel == "class" or bool(_HERO_SEL_RE.search(" " + sel))
        out.append(_f("ai_gradient", "high" if hero else "medium", doc.where(pos),
                      "the default AI purple/indigo/blue gradient%s" % (" on the hero" if hero else ""),
                      "pick colours from this product's own palette; a flat field or a photo beats it"))
    return out


def _sections(doc):
    if doc.kind not in _MARKUP_KINDS:
        return []
    return [(m.start(), m.group(1) or "", m.group(2))
            for m in re.finditer(r"<section\b([^>]*)>(.*?)</section\s*>", doc.code, re.S | re.I)]


_CARDISH_RE = re.compile(r"card|tile|feature|service|benefit|pricing|plan|box|testimonial|item", re.I)


def _cls(m):
    return " ".join((m.group(1) or m.group(2) or m.group(3) or "").split())


def _layout_rules(doc, centered_classes):
    out = []
    secs = _sections(doc)
    if len(secs) >= 4:
        grids = []
        centred = 0
        for pos, attrs, body in secs:
            counts = Counter(_cls(m) for m in _CLASS_ATTR_RE.finditer(body))
            if any(n >= 3 and s and (_CARDISH_RE.search(s) or ("rounded" in s and ("shadow" in s or "border" in s)))
                   for s, n in counts.items()):
                grids.append(pos)
            head = attrs + body[:400]
            toks = set(re.findall(r"[\w-]+", " ".join(_cls(m) for m in _CLASS_ATTR_RE.finditer(head))))
            if re.search(r"(?<![\w-])text-center(?![\w-])|text-align\s*:\s*center", head) or toks & centered_classes:
                centred += 1
        if len(grids) >= 3 and len(grids) >= 0.6 * len(secs):
            out.append(_f("identical_cards", "medium", doc.where(grids[0]),
                          "%d of %d sections are the same card grid" % (len(grids), len(secs)),
                          "give each section the form its content needs: a list, a table, a quote, a photo"))
        if centred >= 0.8 * len(secs):
            out.append(_f("centered_everything", "medium", doc.where(secs[0][0]),
                          "%d of %d sections are centre-aligned" % (centred, len(secs)),
                          "left-align body copy; centre only what earns it (a hero line, a CTA)"))
    return out


def _site_rules(docs, rvars):
    """Rules that judge the whole set: type, blur, focus, motion."""
    out = []
    css_rules = [(d, r, s) for d in docs for r, s in _all_rules(d)]
    # Typography: Inter / system only
    fams, first = [], None
    for d, r, _s in css_rules:
        for prop in ("font-family", "font"):
            if prop not in r.decls:
                continue
            val = _resolve(r.decls[prop], rvars)
            if prop == "font":           # `600 1rem/1.5 "Work Sans", serif`
                m = re.search(r"(?:^|\s)[\d.]+(?:px|r?em|%|pt|vw|vh|ex|ch)(?:\s*/\s*[\d.]+[a-z%]*)?\s+(.+)$",
                              val, re.I)
                if not m:
                    continue
                val = m.group(1)
            for fam in val.split(","):
                fam = fam.strip().strip("\"'").lower()
                if fam and not fam.startswith("var("):
                    fams.append(fam)
                    first = first or (d, r.pos)
    for d in docs:
        for m in re.finditer(r"fonts\.googleapis\.com/css2?\?([^\"'\s)]+)", d.text):
            for fam in re.findall(r"family=([^:&]+)", m.group(1)):
                fams.append(fam.replace("+", " ").lower())
                first = first or (d, m.start())
        for m in re.finditer(r"\b(?:import\s*\{([^}]*)\}\s*from\s*['\"]next/font/google['\"])", d.text):
            for fam in m.group(1).split(","):
                fams.append(fam.strip().replace("_", " ").lower())
                first = first or (d, m.start())
    text_fams = [f for f in fams if f not in _MONO_FONTS]
    if text_fams and all(f in _GENERIC_FONTS for f in text_fams):
        d, pos = first
        out.append(_f("generic_fonts", "medium", d.where(pos),
                      "typography is Inter / system fonts only, no chosen pairing",
                      "pair a display face with a body face picked for this product"))
    # Glassmorphism everywhere
    blur = []
    for d, r, _s in css_rules:
        if any(k in r.decls and "blur(" in r.decls[k] for k in ("backdrop-filter", "-webkit-backdrop-filter")):
            blur.append((d, r.pos))
    for d in docs:
        blur += [(d, pos) for cls, pos in d.classes if re.search(r"(?<![\w-])(?:[a-z]+:)*backdrop-blur", cls)]
    if len(blur) >= 3:
        d, pos = blur[0]
        out.append(_f("glassmorphism", "medium", d.where(pos),
                      "frosted-glass blur on %d elements" % len(blur),
                      "keep blur for one overlay at most; give surfaces a solid colour"))
    # Focus
    all_classes = " ".join(c for d in docs for c, _p in d.classes)
    focus_rules = [r for _d, r, s in css_rules if ":focus" in s]
    replaced = any(
        any(k in r.decls and not re.fullmatch(r"\s*(?:none|0|0px)\s*", r.decls[k])
            for k in ("outline", "box-shadow", "border", "border-color", "background", "background-color",
                      "text-decoration", "outline-color"))
        for r in focus_rules) or bool(re.search(
            r"(?<![\w-])focus(?:-visible)?:(?:ring|shadow|border|bg|underline|outline-(?!none))", all_classes))
    killed = None
    for d, r, s in css_rules:
        o = (r.decls.get("outline") or r.decls.get("outline-style") or "").strip().lower()
        if o in ("none", "0", "0px", "0 none", "none 0") and (
                ":focus" in s or re.search(r"(?:^|[\s,>])(?:\*|a|button|input|select|textarea|summary)(?=$|[\s,.:\[])"
                                           r"|\.btn|\[tabindex", " " + s)):
            same_rule_ok = any(k in r.decls and r.decls[k].strip().lower() not in ("none", "0")
                               for k in ("box-shadow", "border-color", "background", "background-color"))
            if not (":focus" in s and same_rule_ok):
                killed = killed or (d, r.pos)
    for d in docs:
        for cls, pos in d.classes:
            if re.search(r"(?<![\w-])(?:focus(?:-visible)?:)?outline-none(?![\w-])", cls):
                killed = killed or (d, pos)
    if killed and not replaced:
        d, pos = killed
        out.append(_f("outline_none", "high", d.where(pos),
                      "outline: none with no visible focus style to replace it",
                      "add :focus-visible { outline: 2px solid <accent>; outline-offset: 2px }"))
    elif not focus_rules and "focus" not in all_classes:
        interactive = any(re.search(r"<(?:a|button|input|select|textarea)\b", d.code, re.I) for d in docs
                          if d.kind in _MARKUP_KINDS)
        styles_controls = [(d, r.pos) for d, r, s in css_rules
                           if re.search(r"(?:^|[\s,>])(?:a|button|input)(?=$|[\s,.:\[])|\.btn|\.button|\.cta", " " + s)]
        if interactive and styles_controls:
            d, pos = styles_controls[0]
            out.append(_f("no_focus_styles", "low", d.where(pos),
                          "custom-styled controls with no :focus-visible style",
                          "give links and buttons a visible :focus-visible style"))
    # Motion without prefers-reduced-motion
    points, at = 0, None
    for d in docs:
        for m in re.finditer(r"@(?:-webkit-)?keyframes\b", d.text):
            points += 1
            at = at or (d, m.start())
        for m in re.finditer(r"\b(?:gsap|ScrollTrigger|framer-motion|AOS\.init|anime\(|lottie|ScrollReveal|"
                             r"locomotive-scroll|@studio-freight/lenis|from\s+['\"]motion(?:/react)?['\"]|"
                             r"from\s+['\"]three['\"])", d.text):
            points += 3
            at = at or (d, m.start())
            break
        anim = [pos for cls, pos in d.classes
                if re.search(r"(?<![\w-])animate-(?!none|spin)[\w-]+", cls)]
        points += len(anim)
        if anim:
            at = at or (d, anim[0])
    for d, r, _s in css_rules:
        a = r.decls.get("animation") or r.decls.get("animation-name")
        if a and a.strip().lower() not in ("none", "initial", "inherit", "unset"):
            points += 1
            at = at or (d, r.pos)
    guarded = any(re.search(r"prefers-reduced-motion|useReducedMotion|reducedMotion|motion-reduce:|motion-safe:",
                            d.text) for d in docs)
    if points >= 3 and not guarded:
        d, pos = at
        out.append(_f("reduced_motion", "medium", d.where(pos),
                      "heavy animation with no prefers-reduced-motion fallback",
                      "wrap it in @media (prefers-reduced-motion: no-preference) or stop it under reduce"))
    return out


# --------------------------------------------------------------------------- #
# Public: files
# --------------------------------------------------------------------------- #

def _order(findings):
    rank = {s: i for i, s in enumerate(SEVERITIES)}
    return sorted(findings, key=lambda f: rank.get(f["severity"], 9))


def check_files(paths_or_texts, kind=None):
    """Findings for built web output: HTML, CSS, JSX/TSX, Vue/Svelte, and the
    copy in Markdown. `paths_or_texts`: one path or text, a list of paths /
    texts / (name, text) pairs, or a dict {name: text}. `kind` forces the kind
    for every input (else the extension, else a sniff). Never raises; a file
    it cannot read is skipped."""
    docs = []
    for name, text in _inputs(paths_or_texts):
        try:
            docs.append(_Doc(name, text, _kind_of(name, text, kind)))
        except Exception:                                        # noqa: BLE001
            continue
    if not docs:
        return []
    findings = []
    rvars = {}
    try:
        rvars = _root_vars(docs)
    except Exception:                                            # noqa: BLE001
        pass
    centered = set()
    for d in docs:
        for r in d.rules:
            if (r.decls.get("text-align") or "").strip().lower() == "center":
                centered.update(re.findall(r"^\.([\w-]+)$", r.selector.strip()))
                if not r.media and re.fullmatch(r"\s*(?:body|html|main|\*|section|\.container|\.section|"
                                                r"\.wrapper|\.content)\s*", r.selector):
                    findings.append(_f("centered_everything", "medium", d.where(r.pos),
                                       "text-align: center on %s centres the whole page" % r.selector.strip(),
                                       "left-align body copy; centre only what earns it"))
    for d in docs:
        for fn in (lambda: _copy_rules(d) if d.kind in _COPY_KINDS else [],
                   lambda: _markup_rules(d),
                   lambda: _style_rules(d, rvars),
                   lambda: _layout_rules(d, centered)):
            try:
                findings += fn()
            except Exception:                                    # noqa: BLE001
                continue
    try:
        findings += _site_rules(docs, rvars)
    except Exception:                                            # noqa: BLE001
        pass
    seen, out = set(), []
    for f in findings:
        key = (f["rule"], f["where"], f["why"])
        if key not in seen:
            seen.add(key)
            out.append(f)
    return _order(out)


# --------------------------------------------------------------------------- #
# Public: the design, before any code
# --------------------------------------------------------------------------- #

_FONT_NAMES = (
    "inter", "roboto", "open sans", "lato", "montserrat", "poppins", "raleway", "nunito",
    "nunito sans", "source sans 3", "source sans pro", "source serif 4", "source serif pro",
    "merriweather", "playfair display", "playfair", "lora", "pt serif", "pt sans", "noto sans",
    "noto serif", "work sans", "rubik", "karla", "manrope", "dm sans", "dm serif display",
    "dm serif text", "dm mono", "ibm plex sans", "ibm plex serif", "ibm plex mono", "ibm plex",
    "space grotesk", "space mono", "jetbrains mono", "fira sans", "fira code", "barlow",
    "archivo", "archivo black", "archivo narrow", "oswald", "bebas neue", "anton", "cormorant",
    "cormorant garamond", "eb garamond", "garamond", "libre baskerville", "libre franklin",
    "libre caslon", "libre caslon text", "libre caslon display", "crimson pro", "crimson text",
    "zilla slab", "roboto slab", "roboto mono", "roboto condensed", "josefin sans", "mulish",
    "figtree", "onest", "geist", "geist mono", "plus jakarta sans", "syne", "sora", "lexend",
    "fraunces", "recoleta", "instrument sans", "instrument serif", "literata", "alegreya",
    "alegreya sans", "bricolage grotesque", "schibsted grotesk", "hanken grotesk",
    "familjen grotesk", "darker grotesque", "cabinet grotesk", "satoshi", "general sans",
    "clash display", "clash grotesk", "switzer", "chillax", "zodiak", "gambetta", "erode",
    "sentient", "boska", "tanker", "gambarino", "neue montreal", "pp neue montreal",
    "pp editorial new", "editorial new", "migra", "ogg", "gt america", "gt sectra",
    "gt walsheim", "gt super", "gt alpina", "söhne", "sohne", "untitled sans", "untitled serif",
    "tiempos", "tiempos text", "tiempos headline", "graphik", "founders grotesk", "canela",
    "domaine display", "domaine text", "freight text", "freight display", "freight big",
    "mabry", "apercu", "aeonik", "helvetica", "helvetica neue", "arial", "georgia",
    "times new roman", "verdana", "trebuchet ms", "futura", "avenir", "avenir next", "gill sans",
    "baskerville", "didot", "bodoni", "bodoni moda", "caslon", "proxima nova",
    "brandon grotesque", "gotham", "din next", "din pro", "univers", "akzidenz-grotesk",
    "franklin gothic", "century gothic", "rockwell", "courier new", "menlo", "consolas",
    "sf pro", "segoe ui", "chivo", "red hat display", "red hat text", "public sans",
    "atkinson hyperlegible", "besley", "young serif", "gloock", "unbounded",
    "big shoulders display", "abril fatface", "yeseva one", "prata", "marcellus", "cinzel",
    "tenor sans", "italiana", "petrona", "piazzolla", "vollkorn", "ibarra real nova", "cardo",
    "old standard tt", "overpass", "heebo", "inconsolata", "saira", "exo 2", "titillium web",
    "kanit", "mukta", "dosis", "varela round", "comfortaa", "pacifico", "dancing script",
    "permanent marker", "space grotesk", "redaction", "pp mori", "pp fragment", "neue haas grotesk",
    "neue haas unica", "suisse int'l", "suisse", "work sans", "newsreader", "spectral", "bitter",
    "epilogue", "outfit", "quicksand", "cabin", "prompt", "caveat", "lobster", "assistant")
# Names that are also ordinary words: counted only when Capitalised.
_FONT_WORDS = {"newsreader", "spectral", "bitter", "epilogue", "outfit", "quicksand", "cabin",
               "prompt", "caveat", "lobster", "assistant", "suisse", "redaction", "canela", "ogg",
               "migra", "erode", "tanker", "sentient"}
_FONT_RE = re.compile(r"(?<![\w-])(" + "|".join(
    re.escape(n) for n in sorted(set(_FONT_NAMES), key=len, reverse=True)) + r")(?![\w-])", re.I)
_ANTI_FONTS = {"inter", "geist", "plus jakarta sans", "space grotesk", "instrument sans",
               "instrument serif", "fraunces", "recoleta", "playfair", "playfair display",
               "dm sans", "dm serif display", "dm serif text", "outfit", "syne", "montserrat",
               "poppins"}
_SYSTEM_STACK_RE = re.compile(r"\bsystem(?:-ui| ui| font(?: stack)?s?| stack)\b|-apple-system|ui-sans-serif", re.I)
_QUOTED_FONT_RE = re.compile(r"font(?:-family)?\s*:\s*[\"']([^\"']{3,40})[\"']", re.I)

_NEG_RE = re.compile(r"\b(?:no|not|never|avoid\w*|without|instead of|rather than|ban\w*|don'?t|"
                     r"do not|nor|ditch|drop|forbid\w*|reject\w*|replac\w*|swap\w* out|"
                     r"anything but|other than)\b", re.I)
_HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![0-9a-zA-Z_-])")
_FUNC_COLOR_RE = re.compile(r"\b(?:rgba?|hsla?|oklch|oklab|lab|lch)\(\s*[\d.][^)]*\)", re.I)
# "the existing palette", "reuse styles.css", "the brand colours": a decision
# already made elsewhere. Bare "keep" / "same" are not here ("keep the style
# consistent" decides nothing).
_EXISTING_RE = re.compile(
    r"\b(?:existing|current|reuse[sd]?|re-use[sd]?|brand'?s?|client'?s?|owner'?s?)\b([^.\n;]{0,60})", re.I)
_KEEPS_PALETTE_RE = re.compile(r"\b(?:palette|colou?rs?|brand|design system|tokens?|theme|"
                               r"stylesheet|styles?|look)\b", re.I)
_KEEPS_TYPE_RE = re.compile(r"\b(?:fonts?|typography|typefaces?|brand|design system|stylesheet|"
                            r"styles?|look)\b", re.I)
_VAGUE_RE = re.compile(r"\b(?:modern|clean|minimal(?:ist|istic)?|sleek|professional|beautiful|stunning|"
                       r"elegant|contemporary|fresh|polished|eye-catching|visually appealing|cutting-edge)\b", re.I)
_STOCK_SECTIONS = [
    ("hero", r"\bhero\b"),
    ("features", r"\b(?:features?|benefits?)\b(?:\s+(?:grid|cards?|section|list|row))?|\b(?:3|three)\s+(?:\w+\s+)?cards?\b"),
    ("testimonials", r"\btestimonials?\b|\breviews? section\b"),
    ("cta", r"\bcta\b|\bcall[- ]to[- ]action\b"),
    ("pricing", r"\bpricing\b"),
    ("faq", r"\bfaq\b"),
    ("logos", r"\blogo (?:cloud|wall|strip|bar|row)\b|\btrusted by\b"),
    ("stats", r"\bstats?\b(?:\s+(?:section|bar|band|row))?"),
    ("newsletter", r"\bnewsletter\b"),
]
_REASON_RE = re.compile(
    r"\bbecause\b|\bso that\b|\bso (?:visitors|users|people|customers|readers|buyers|guests|diners)\b|"
    r"\bwhy\b|\breason\b|\bproves?\b|\bto (?:show|prove|answer|let|help)\b|\bwho (?:need|want)\b", re.I)
_LAYOUT_RE = re.compile(
    r"\b(?:grid|\d+[- ]column|columns?|asymmetric\w*|split[- ](?:screen|layout|hero)|editorial|bento|sidebar|"
    r"full[- ]bleed|timeline|masonry|magazine|z-pattern|f-pattern|single[- ]column|two[- ]column|"
    r"sticky|horizontal scroll|scrollytelling|overlap\w*|offset|broken grid|swiss|brutalis\w*|"
    r"poster|long[- ]scroll|composition|stacked|scroll[- ]driven|menu board|index page)\b|"
    r"layout\s*(?:concept)?\s*[:=]", re.I)
_COPY_SRC_RE = re.compile(
    r"\b(?:real|actual|client'?s?|owner'?s?|user'?s?|customer'?s?|provided|existing|supplied|"
    r"approved|their own|brand'?s?|business'?s?|restaurant'?s?|shop'?s?)\s+(?:own\s+)?"
    r"(?:copy|content|text|wording|words|menu|prices?|facts|details|photos|story|reviews|quotes)\b|"
    r"\bcopy\s*(?:source|from|deck|doc)\b|\bcopy\s*:|\bcontent\s*(?:source|from)\b|\[needs input\]|"
    r"\bneeds input\b|\bfrom the (?:brief|request|user|client|owner|interview)\b", re.I)
_GRAD_WORD_RE = re.compile(r"\bgradients?\b|\b(?:purple|violet|indigo)[- ]?(?:to|->|→|-)[- ]?"
                           r"(?:blue|indigo|violet|purple|pink)\b", re.I)
_VIOLET_WORD_RE = re.compile(r"\b(?:purple|violet|indigo|lavender|lilac|fuchsia|magenta|plum)\b", re.I)


def _sentences(text):
    return [s for s in re.split(r"(?<=[.!?;])\s+|\n+", text or "") if s.strip()]


def _negated(sentence, pos):
    """Is the mention at `pos` in `sentence` ruled OUT ("no purple", "avoid
    Inter, Poppins")? A positive verb between the negation and the mention
    ("instead of Inter, use Söhne") re-opens it."""
    before = sentence[max(0, pos - 80):pos]
    negs = list(_NEG_RE.finditer(before))
    if not negs:
        return False
    after_neg = before[negs[-1].end():]
    return not re.search(r"\b(?:use|uses|using|set in|pair\w*|choose|pick\w*|go with)\b", after_neg, re.I)


def _fonts(text):
    """[(name, negated)] for every named typeface in `text`."""
    out = []
    for s in _sentences(text):
        for m in _FONT_RE.finditer(s):
            name = m.group(1).lower()
            if name in _FONT_WORDS and not m.group(1)[:1].isupper():
                continue
            out.append((name, _negated(s, m.start())))
        for m in _QUOTED_FONT_RE.finditer(s):
            name = m.group(1).split(",")[0].strip().lower()
            if name and name not in {n for n, _ng in out} and name not in _GENERIC_FONTS:
                out.append((name, False))
    return out


def _colours(text):
    vals = set()
    for m in _HEX_RE.finditer(text or ""):
        v = m.group(0).lower()
        if len(v) == 4 and not re.search(r"[a-f]", v) and v != "#000":
            continue                       # "#123" is an issue number
        vals.add(v)
    vals.update(" ".join(m.group(0).lower().split()) for m in _FUNC_COLOR_RE.finditer(text or ""))
    return vals


def _design_gradient(text, request):
    if _VIOLET_WORD_RE.search(request or ""):
        return None                         # the user asked for that colour
    for s in _sentences(text):
        if _gradient_is_ai(s) or _tailwind_ai_gradient(s):
            return s
        if _GRAD_WORD_RE.search(s):
            for m in _VIOLET_WORD_RE.finditer(s):
                if not _negated(s, m.start()):
                    return s
    return None


def check_design(design_text, request=""):
    """Findings for a design spec or plan text, before any code. `request` is
    the user's own ask: a choice the user made is never called slop, and a
    decision the user already gave (their palette, their fonts) counts."""
    text = str(design_text or "")
    req = str(request or "")
    full = text + "\n" + req
    out = []
    colours = _colours(full)
    existing = [m.group(1) for m in _EXISTING_RE.finditer(full)]
    keeps_palette = any(_KEEPS_PALETTE_RE.search(e) for e in existing)
    keeps_type = any(_KEEPS_TYPE_RE.search(e) for e in existing)
    fonts = [n for n, neg in _fonts(full) if not neg]
    families = sorted(set(fonts))
    system_stack = bool(_SYSTEM_STACK_RE.search(full))
    has_palette = len(colours) >= 2 or keeps_palette
    has_type = bool(families) or system_stack or keeps_type

    g = _design_gradient(text, req)
    if g:
        out.append(_f("default_gradient", "high", "design",
                      "the default purple/indigo/blue gradient hero",
                      "pick a hero treatment from this product's own palette (a photo, a flat field, "
                      "a texture) and name its hex values"))
    vague = {m.group(0).lower() for s in _sentences(text) for m in _VAGUE_RE.finditer(s)
             if not _negated(s, m.start())}
    if len(vague) >= 2 and len(colours) < 2 and not families:
        out.append(_f("vague_style", "high", "design",
                      'style is adjectives only ("%s") with no concrete palette or type'
                      % ", ".join(sorted(vague)[:3]),
                      "replace the adjectives with decisions: hex values, named faces, a layout idea"))
    if not has_palette:
        out.append(_f("no_palette", "high", "design",
                      "no concrete palette (%s)" % ("one colour value" if colours else "no hex values"),
                      "name 4-6 hex values with roles (background, text, accent, muted), text "
                      ">=4.5:1 -- or name the existing stylesheet whose palette is kept"))
    if not has_type:
        out.append(_f("no_type_pairing", "high", "design",
                      "no typefaces named",
                      "name a display face and a body face chosen for this product (or a system "
                      "stack, said so) -- not Inter by default"))
    elif len(families) == 1 and not system_stack and not keeps_type:
        out.append(_f("no_type_pairing", "medium", "design",
                      "one typeface (%s), no pairing" % families[0],
                      "pair it with a contrasting display or body face, or say why one family"))
    slop = [f for f in families if f in _ANTI_FONTS
            and not re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(f), req, re.I)]
    if slop:
        out.append(_f("slop_font", "medium", "design",
                      "default AI typeface: %s" % ", ".join(slop[:3]),
                      "choose a face with a point of view for this product"))
    stock = [name for name, rx in _STOCK_SECTIONS if re.search(rx, text, re.I)]
    if "hero" in stock and len(stock) >= 4 and not _REASON_RE.search(text):
        out.append(_f("stock_skeleton", "medium", "design",
                      "stock skeleton (%s) with no product reason" % " + ".join(stock[:5]),
                      "derive the sections from what THIS visitor needs to decide; say why each exists"))
    if not _LAYOUT_RE.search(text):
        out.append(_f("no_layout_concept", "medium", "design",
                      "no layout concept",
                      "one sentence on the composition (grid, rhythm, what leads) and why it fits"))
    if not _COPY_SRC_RE.search(full):
        out.append(_f("no_copy_source", "medium", "design",
                      "no source for the real copy",
                      "say where the words, facts and prices come from; missing ones are [NEEDS INPUT]"))
    return _order(out)


# --------------------------------------------------------------------------- #
# Public: the score
# --------------------------------------------------------------------------- #

def summary(findings):
    """{"score": 0-100, "high": n, "medium": n, "low": n, "line": str}. Each
    rule costs its weight once (high 15, medium 7, low 2) plus a little per
    repeat, so one rule firing on every page does not zero the score alone."""
    findings = [f for f in (findings or ()) if isinstance(f, dict)]
    counts = {s: 0 for s in SEVERITIES}
    occ, sev = Counter(), {}
    rank = {s: i for i, s in enumerate(SEVERITIES)}
    for f in findings:
        s = f.get("severity") if f.get("severity") in counts else "low"
        counts[s] += 1
        rule = str(f.get("rule") or "?")
        occ[rule] += 1
        if rule not in sev or rank[s] < rank[sev[rule]]:
            sev[rule] = s
    penalty = sum(_WEIGHT[sev[r]] + _EXTRA[sev[r]] * min(n - 1, 4) for r, n in occ.items())
    score = max(0, 100 - penalty)
    if not findings:
        line = "Slop check: 100/100 -- nothing found"
    else:
        bits = []
        for s in SEVERITIES:
            rules = [r for r in occ if sev[r] == s]
            if counts[s]:
                bits.append("%d %s (%s%s)" % (counts[s], s, ", ".join(rules[:3]),
                                              ", +%d more" % (len(rules) - 3) if len(rules) > 3 else ""))
        line = "Slop check: %d/100 -- %s" % (score, ", ".join(bits))
    return {"score": score, "high": counts["high"], "medium": counts["medium"],
            "low": counts["low"], "line": line}
