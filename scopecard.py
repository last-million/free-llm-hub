"""Calvoun Free LLM Hub -- the program SCOPE CARD and the host reader of the scope guard.

WHAT IT IS FOR
--------------
Authorized security testing (a bug bounty program, a client engagement, or the
owner's OWN app) lives or dies on scope: a request to a host the program or the
owner excluded is a rules violation, whatever it finds. Free models forget the
scope a few turns in and compaction drops it, so the hub keeps one CARD per
project folder and (app.py) puts it back in front of the model on every hop and
refuses to pass on a tool call aimed at a host the card lists as OUT of scope.

WHAT IS PURE HERE
-----------------
Stdlib only, no import of app/config, never raises (every public function fails
closed to "nothing": an empty card, no hosts, no block).

  extract(text, kind=None, src="message") -> card   scope sections + inline
  merge(card, new) -> card                          union, bounded, ordered
  drop_source(card, src) -> card                    a re-read file replaces its part
  in_scope(host, card) -> True | False | None       None = not listed (never blocked)
  render(card, max_chars) -> str                    deterministic note text
  targets_in_call(name, arguments) -> [host]        what a tool call would hit
  check_calls(tool_calls, card) -> (host, entry) | None
  block_message(host, entry) -> str
  guard_stream(items, framing, check, ...)          SSE wrapper (see its docstring)
  Store(path)                                       scopes.json, atomic, bounded
  targets(card) -> [{name, in, out, rules}]         the card's NAMED targets
  render_target(card, name, max_chars) -> str       one target's scope note

A CARD is {"in": [entry], "out": [entry], "rules": [rule]}; an entry is
{"p": pattern, "kind": "program" | "owner-declared", "src": "message" | <file>}
where a pattern is an exact host ("app.example.com"), a wildcard
("*.example.com": sub-hosts only, never the apex), an IP or a CIDR. A rule is
{"t": text, "kind": ..., "src": ...}.

SEVERAL TARGETS ON ONE CARD (2026-10-29). A scope text that names two or more
programs / apps under their own headings ("## acme", "Program: acme",
"App: shop.example.com", each followed by its own in/out lists or rules) gives
each entry and rule it lists a "tg" (the target's name), and the card a
"targets" list [{"name", "src"}] in the order they were declared. Entries
written before the first target heading carry no "tg": they are shared by every
target. A text with fewer than two target headings is read exactly as before
(no "tg", no "targets"), so an old scopes.json loads unchanged. The scope guard
and render() still read the whole card (the union of all targets).
"""
import ipaddress
import json
import os
import re
import tempfile
import threading
import time
from urllib.parse import urlsplit

MAX_HOSTS = 200            # per list (in / out)
MAX_RULES = 40
MAX_RULE_CHARS = 160
MAX_TEXT = 200_000         # chars read from one source
NOTE_MAX_CHARS = 800
MAX_FOLDERS = 64           # cards kept in scopes.json
MAX_TARGETS = 12           # named targets on one card
TARGET_NAME_MAX = 60
KINDS = ("program", "owner-declared")


def empty():
    return {"in": [], "out": [], "rules": []}


def is_empty(card):
    try:
        return not (card.get("in") or card.get("out") or card.get("rules"))
    except Exception:                                            # noqa: BLE001
        return True


# --------------------------------------------------------------------------- #
# Hosts in free text
# --------------------------------------------------------------------------- #

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DOMAIN_RE = re.compile(r"(?<![\w@.*/-])((?:\*\.)?(?:%s\.)+[a-z]{2,24})(?![\w-])" % _LABEL, re.I)
_WILD_URL_DOMAIN_RE = re.compile(r"(?<=//)(\*\.(?:%s\.)+[a-z]{2,24})(?![\w-])" % _LABEL, re.I)
_IPV4_RE = re.compile(r"(?<![\w.:/-])(\d{1,3}(?:\.\d{1,3}){3})(/\d{1,2})?(?![\w.])")
_IPV6_RE = re.compile(r"(?<![\w:.])([0-9a-f]{0,4}(?::[0-9a-f]{0,4}){2,7})(/\d{1,3})?(?![\w:])", re.I)
_URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]{1,15}://[^\s'\"<>\]\)\}`]+", re.I)
# A two-label "domain" whose last label is a file extension is a file name
# (scope.md, app.py, package.json), not a host.
_FILE_EXT = frozenset((
    "md markdown txt json py js mjs cjs ts tsx jsx vue html htm css scss xml csv yml yaml "
    "toml ini cfg conf log sh ps1 bat cmd exe dll zip gz tgz tar png jpg jpeg gif svg webp "
    "pdf lock env rb go rs java php c h cpp hpp sql db bak out pem key crt txt").split())


def _norm_host(h):
    h = str(h or "").strip().strip("[]").lower().rstrip(".")
    return h


def _domain_ok(d):
    labels = d.split(".")
    if d.startswith("*."):
        return len(labels) >= 3
    if len(labels) == 2 and labels[-1] in _FILE_EXT:
        return False
    return len(labels) >= 2


def _url_host(url):
    try:
        net = urlsplit(url).netloc
    except ValueError:
        return None
    net = net.rsplit("@", 1)[-1]
    if net.startswith("["):
        host = net[1:].split("]", 1)[0]
    else:
        host = net.split(":", 1)[0]
    host = _norm_host(host)
    if not host:
        return None
    if _parse_ip(host) is not None:
        return host
    if re.fullmatch(r"(?:\*\.)?(?:%s\.)+[a-z]{2,24}" % _LABEL, host, re.I) and _domain_ok(host):
        return host
    if re.fullmatch(_LABEL, host, re.I):
        return host                      # a bare name (localhost, an intranet host)
    return None


def _parse_ip(s):
    try:
        return ipaddress.ip_network(str(s), strict=False)
    except (ValueError, TypeError):
        return None


def _hosts_with_pos(text, bare=True):
    """[(start, end, host)] in `text`: every URL's host, then (bare=True) bare
    domains, wildcards, IPv4/IPv6 addresses and CIDRs outside those URLs."""
    out = []
    if not isinstance(text, str) or not text:
        return out
    chars = list(text)
    for m in _URL_RE.finditer(text):
        h = _url_host(m.group(0))
        if h:
            out.append((m.start(), m.end(), h))
        for i in range(m.start(), m.end()):
            chars[i] = " "
    if bare:
        rest = "".join(chars)
        for m in _DOMAIN_RE.finditer(rest):
            d = _norm_host(m.group(1))
            if _domain_ok(d):
                out.append((m.start(1), m.end(1), d))
        for m in _IPV4_RE.finditer(rest):
            raw = m.group(1) + (m.group(2) or "")
            if _parse_ip(raw) is not None and all(int(p) <= 255 for p in m.group(1).split(".")):
                out.append((m.start(), m.end(), raw))
        for m in _IPV6_RE.finditer(rest):
            core = m.group(1)
            if "::" not in core and core.count(":") != 7:
                continue
            raw = core + (m.group(2) or "")
            if _parse_ip(raw) is not None:
                out.append((m.start(), m.end(), raw.lower()))
    out.sort()
    return out


def hosts_in_text(text, bare=True):
    """Unique hosts in `text`, in order of appearance."""
    seen, res = set(), []
    for _s, _e, h in _hosts_with_pos(text, bare=bare):
        if h not in seen:
            seen.add(h)
            res.append(h)
    return res


# --------------------------------------------------------------------------- #
# Reading a scope out of text
# --------------------------------------------------------------------------- #

_OWNER_RE = re.compile(
    r"\b(?:my|our)\s+(?:own\s+)?(?:web\s?)?(?:apps?|applications?|sites?|websites?|saas|apis?|"
    r"servers?|domains?|platforms?|products?|services?|assets?|infrastructure)\b|"
    r"\bi own\b|\bwe own\b|\bowned by (?:me|us)\b|\bowner[- ]declared\b|\bself[- ]test", re.I)
_PROGRAM_RE = re.compile(
    r"\bhackerone\b|\bbugcrowd\b|\bintigriti\b|\byeswehack\b|\bbug[- ]?bount(?:y|ies)\b|"
    r"\bbounty\b|\bprogram(?:me)?s?\b|\bresearchers?\b|\bvdp\b|\bdisclosure\b|\brewards?\b|"
    r"\bpayouts?\b", re.I)


def detect_kind(text):
    """"owner-declared" when the text speaks of the writer's own app and of no
    program, else "program"."""
    try:
        if _OWNER_RE.search(text or "") and not _PROGRAM_RE.search(text or ""):
            return "owner-declared"
    except Exception:                                            # noqa: BLE001
        pass
    return "program"


# Words that make a text worth reading for a scope at all (user messages are
# checked against this first: most never mention one).
_HINT_RE = re.compile(
    r"\bin[- ]scope\b|\bout[- ]of[- ]scope\b|\bscope\s*[:=]|\bscope (?:is|are|includes?|covers?|"
    r"limited to)\b|\bexclusions?\b|\bnot eligible\b|\btargets?\s*:|\bassets?\s*:|"
    r"\b(?:do not|don'?t|never|must not)\s+(?:test|scan|touch|attack|probe|fuzz)\b|"
    r"\brules of engagement\b|\bprogram rules\b|\boff[- ]limits\b|"
    r"\b(?:my|our)\s+(?:own\s+)?(?:web\s?)?(?:apps?|applications?|sites?|websites?|saas|apis?|"
    r"servers?|domains?|platforms?|services?|assets?)\s*(?:(?:is|are)\b|(?:lives?|runs?)\s+(?:at|on)|:)",
    re.I)


def has_hint(text):
    return isinstance(text, str) and bool(_HINT_RE.search(text))


_HEAD_RE = re.compile(
    r"^(?P<h>"
    r"(?P<out>out[- ]of[- ]scope(?:\s+(?:assets?|targets?|domains?|hosts?|items?|"
    r"vulnerabilit(?:y|ies)|issues|findings))?|oos|exclusions?|excluded(?:\s+(?:assets?|targets?|"
    r"domains?))?|not eligible|ineligible|non[- ]?eligible(?:\s+\w+)?|non[- ]?qualifying(?:\s+\w+)?|"
    r"off[- ]limits|do not test)"
    r"|(?P<in>in[- ]scope(?:\s+(?:assets?|targets?|domains?|hosts?|items?))?|scope|targets?|assets?|"
    r"eligible(?:\s+(?:assets?|targets?|domains?))?|domains?|"
    r"(?:my|our)\s+(?:own\s+)?(?:apps?|sites?|websites?|assets?|domains?))"
    r"|(?P<rules>rules|program rules|rules of engagement|testing (?:rules|guidelines|policy|"
    r"requirements)|guidelines|policy|restrictions|requirements|prohibited(?:\s+activities)?|"
    r"do'?s and don'?ts|engagement rules)"
    r")\s*(?:[:\-–—]\s*(?P<rest>.*))?$", re.I)


def _heading(line):
    """(kind, rest) when `line` is a scope heading ("## Out of scope", "In scope:",
    "**Rules**", "Out of scope: a.example.com, b.example.com"), else (None, None)."""
    s = line.strip()
    if not s or len(s) > 400:
        return None, None
    s = s.lstrip("#").strip()
    s = re.sub(r"^[>*_\s]+|[*_\s]+$", "", s)
    s = s.replace("**", "").replace("__", "")
    m = _HEAD_RE.match(s)
    if not m:
        return None, None
    rest = (m.group("rest") or "").strip()
    if not rest and len(s) > 60:
        return None, None
    kind = "out" if m.group("out") else "in" if m.group("in") else "rules"
    return kind, rest


_BULLET_RE = re.compile(r"^\s*(?:[-*+•>]|\d{1,3}[.)])\s+")


def _clean(line):
    s = _BULLET_RE.sub("", line)
    s = s.replace("|", " ").replace("**", "").replace("`", "")
    return " ".join(s.split())


def _scopeish(line):
    """A line that continues a scope list after a blank line: a bullet, a table
    row, or a line that is mostly hosts."""
    if _BULLET_RE.match(line) or line.lstrip().startswith("|"):
        return True
    words = [w for w in re.split(r"[\s,;]+", line) if w]
    hosts = len(_hosts_with_pos(line))
    return bool(hosts) and len(words) - hosts <= 3


# (regex, kind, direction): "after" governs the hosts listed right after it,
# "before" the hosts listed right before it.
_MARKERS = [
    (re.compile(r"\b(?:is|are)\s+(?:also\s+|strictly\s+|explicitly\s+|completely\s+|now\s+)?"
                r"(?:out[- ]of[- ]scope|excluded|off[- ]limits|not in scope|not eligible)\b", re.I),
     "out", "before"),
    (re.compile(r"\b(?:is|are)\s+(?:also\s+|fully\s+|now\s+)?in[- ]scope\b", re.I), "in", "before"),
    (re.compile(r"\bout[- ]of[- ]scope\b(?:\s+(?:assets?|targets?|domains?|hosts?|subdomains?|"
                r"items?|are|is|includes?))*\s*[:=\-]?", re.I), "out", "after"),
    (re.compile(r"\b(?:do not|don'?t|never|must not|may not|should not|cannot|can'?t)\s+(?:test|touch|"
                r"scan|attack|probe|fuzz|target|hit|access)\b(?:\s+(?:against|on|the|any(?:thing)?\s+"
                r"(?:on|under)))*", re.I), "out", "after-dont"),
    (re.compile(r"\b(?:except(?:\s+for)?|excluding|excluded\s*:|exclusions?\s*:|but not|other than|"
                r"not including)\b", re.I), "out", "after"),
    (re.compile(r"\bnot eligible\b\s*[:=\-]?|\boff[- ]limits\s*[:=\-]", re.I), "out", "after"),
    (re.compile(r"(?<!of )(?<!of-)\b(?:in[- ]scope|scope)\b(?:\s+(?:assets?|targets?|domains?|hosts?|"
                r"items?))?(?:\s*[:=\-]|\s+(?:is|are|includes?|covers?|(?:is\s+)?limited\s+to|"
                r"consists\s+of)\b)", re.I), "in", "after"),
    (re.compile(r"\btargets?\s*:", re.I), "in", "after"),
    (re.compile(r"\b(?:my|our)\s+(?:own\s+)?(?:web\s?)?(?:apps?|applications?|sites?|websites?|saas|"
                r"apis?|servers?|domains?|platforms?|services?|assets?|targets?)\b\s*(?:(?:is|are)\b"
                r"(?:\s+(?:at|on|hosted\s+(?:at|on)|running\s+(?:at|on)|live\s+(?:at|on)))?|"
                r"(?:lives?|runs?)\s+(?:at|on)|:)", re.I), "in", "after"),
]
_LIST_GAP_RE = re.compile(r"^[\s,;/&+'\"`*:.-]*(?:(?:and|or|plus|as well as|&)[\s,;/&+'\"`*:.-]*)?$", re.I)
_PAREN_RE = re.compile(r"\([^()]{0,80}\)|\[[^\[\]]{0,80}\]")
_QUALIFIER_RE = re.compile(r"^[\s,]*(?:with|using|via|by|through|at|faster|more|harder|beyond|"
                           r"above|over|during|for|in|without)\b", re.I)

_RULE_RE = re.compile(
    r"\brate[- ]?limit\w*|\brequests?\s*(?:per|/|a)\s*(?:second|sec|s|minute|min)\b|"
    r"\b\d+\s*(?:req(?:uest)?s?|rps|r/s)\b|\brps\b|\bautomated (?:scan\w*|tools?|testing|tests)\b|"
    r"\bscanners?\b|\b(?:d?dos|denial[- ]of[- ]service)\b|\bbrute[- ]?forc\w*|\bsocial engineering\b|"
    r"\bphishing\b|\bspam\w*\b|\b(?:own|test) accounts?\b|\bother users?'?s?\b|\buser[- ]agent\b|"
    r"\bx-(?:bugcrowd|hackerone|intigriti|ywh|bug-?bounty)[\w-]*|\bload[- ]test\w*|"
    r"\b(?:do not|don'?t|never|must not|may not|should not|prohibited|not allowed|forbidden)\b"
    r"[^.;]{0,60}\b(?:test\w*|scan\w*|fuzz\w*|attack\w*|probe|exploit\w*|access\w*|brute\w*|"
    r"automat\w*|send\w*|run\w*|pivot\w*|exfiltrat\w*|modif\w*|delet\w*)\b", re.I)
_SENT_SPLIT_RE = re.compile(r";|(?<=[^\s.])\.(?=\s+[A-Z(])|\n")


def _assign(clause, section):
    """[(host, "in"|"out")] for one clause: each host is governed by the marker
    right before it ("out of scope: X, Y"), else by the one right after it
    ("X and Y are out of scope"), else by the section it sits in."""
    hosts = _hosts_with_pos(clause)
    if not hosts:
        return []
    marks = []
    for rx, kind, direction in _MARKERS:
        for m in rx.finditer(clause):
            marks.append((m.start(), m.end(), kind, direction))
    marks.sort()
    # drop a marker that sits inside a longer one ("out of scope" inside
    # "is out of scope")
    kept = []
    for mk in marks:
        if any(o is not mk and o[0] <= mk[0] and mk[1] <= o[1] and (o[1] - o[0]) > (mk[1] - mk[0])
               for o in marks):
            continue
        kept.append(mk)
    marks = kept

    def gap_ok(a, b):
        return bool(_LIST_GAP_RE.match(_PAREN_RE.sub(" ", clause[a:b])))

    res = []
    for i, (hs, he, host) in enumerate(hosts):
        kind = None
        # an "after" marker before this host, with only a list of hosts between
        prev = [mk for mk in marks if mk[3].startswith("after") and mk[1] <= hs]
        if prev:
            mk = prev[-1]
            run_start = mk[1]
            ok = True
            for (ps, pe, _h) in hosts:
                if run_start <= ps and pe <= hs:
                    if not gap_ok(run_start, ps):
                        ok = False
                        break
                    run_start = pe
            if ok and gap_ok(run_start, hs) and not any(mk[1] <= o[0] < hs for o in marks
                                                      if o is not mk):
                kind = mk[2]
                if mk[3] == "after-dont":
                    # "do not test X with brute force" is a rule about HOW, not
                    # an exclusion of X: only a bare "do not test X" excludes.
                    run_end = he
                    for (ps, pe, _h) in hosts[i + 1:]:
                        if gap_ok(run_end, ps):
                            run_end = pe
                        else:
                            break
                    if _QUALIFIER_RE.match(clause[run_end:]):
                        kind = None
        if kind is None:
            nxt = [mk for mk in marks if mk[3] == "before" and mk[0] >= he]
            if nxt:
                mk = nxt[0]
                run_end = he
                ok = True
                for (ps, pe, _h) in hosts[i + 1:]:
                    if pe <= mk[0]:
                        if not gap_ok(run_end, ps):
                            ok = False
                            break
                        run_end = pe
                if ok and gap_ok(run_end, mk[0]):
                    kind = mk[2]
        if kind is None and section in ("in", "out"):
            kind = section
        if kind:
            res.append((host, kind))
    return res


def _pattern(host):
    """The card pattern for a host token, or None."""
    h = _norm_host(host)
    if not h:
        return None
    net = _parse_ip(h)
    if net is not None:
        return str(net) if "/" in h else str(net.network_address)
    if re.fullmatch(r"(?:\*\.)?(?:%s\.)+[a-z]{2,24}" % _LABEL, h, re.I) and _domain_ok(h):
        return h
    return None


def _clean_tg(tg):
    """A stored target name, or None."""
    if not isinstance(tg, str):
        return None
    tg = " ".join(tg.split())[:TARGET_NAME_MAX]
    return tg or None


def _add(card, lst, pattern, kind, src, tg=None):
    # An entry is unique per (pattern, target): two programs may both list a
    # host. A card with no named targets dedups exactly as before.
    if pattern and len(card[lst]) < MAX_HOSTS and all(
            e["p"] != pattern or e.get("tg") != tg for e in card[lst]):
        e = {"p": pattern, "kind": kind, "src": src}
        if tg:
            e["tg"] = tg
        card[lst].append(e)


def _add_rule(card, text, kind, src, tg=None):
    t = " ".join(str(text or "").split()).strip(" -*:.;")
    if len(t) < 3:
        return
    if len(t) > MAX_RULE_CHARS:
        t = t[:MAX_RULE_CHARS - 3].rstrip() + "..."
    low = t.lower()
    if len(card["rules"]) < MAX_RULES and all(
            r["t"].lower() != low or r.get("tg") != tg for r in card["rules"]):
        r = {"t": t, "kind": kind, "src": src}
        if tg:
            r["tg"] = tg
        card["rules"].append(r)


def _add_target(card, name, src):
    name = _clean_tg(name)
    if not name:
        return
    lst = card.setdefault("targets", [])
    if len(lst) < MAX_TARGETS and all(t["name"].lower() != name.lower() for t in lst):
        lst.append({"name": name, "src": src})


# --------------------------------------------------------------------------- #
# Several named targets in one text
# --------------------------------------------------------------------------- #

# "Program: acme", "App: shop.example.com", "Client - Globex". Not "Target:":
# "Targets: a.example.com" has always been an in-scope list.
_TARGET_LABEL_RE = re.compile(
    r"^(?:program(?:me)?|app|application|web\s?app|site|website|project|client|engagement|"
    r"product)(?:\s+name)?\s*[:\-–—]\s*(?P<name>\S.*)$", re.I)
_MD_HEAD_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*#*\s*$")
# Headings a program page carries that are never a target's name.
_GENERIC_HEADS = frozenset((
    "overview introduction intro about summary rewards reward bounties payouts payout "
    "disclosure contact contacts faq notes details description eligibility reporting "
    "submission submissions severity timeline legal terms updates changelog thanks "
    "acknowledgements acknowledgments references resources general appendix background "
    "context testing methodology findings report reports vulnerabilities vdp programs "
    "programmes apps applications sites websites projects clients").split()) | {
    "disclosure policy", "safe harbor", "safe harbour", "hall of fame", "response targets",
    "response times", "known issues", "getting started", "vulnerability types", "bug bounty",
    "how to report", "program details", "program overview"}
_SCOPE_WORDS_RE = re.compile(r"\bin[- ]scope\b|\bout[- ]of[- ]scope\b", re.I)


def _target_name(raw):
    """A clean target name from a heading's text ("Program: Acme", "acme (program)",
    "shop.example.com"), or None when it does not read as one."""
    s = re.sub(r"[*_`\"]", "", str(raw or "")).strip().strip(":").strip()
    m = _TARGET_LABEL_RE.match(s)
    if m:
        s = m.group("name").strip().strip(":").strip()
    s = re.sub(r"\s*\((?:program(?:me)?|scope|target|app)\)\s*$", "", s, flags=re.I)
    s = re.sub(r"\s+(?:program(?:me)?|scope|targets?|assets?)$", "", s, flags=re.I).strip()
    s = " ".join(s.split())
    if not s or len(s) > TARGET_NAME_MAX or len(s.split()) > 4 or re.search(r"[,;|]", s):
        return None
    if s.lower() in _GENERIC_HEADS or not re.search(r"[a-z0-9]", s, re.I):
        return None
    if len(_hosts_with_pos(s)) > 1:
        return None
    return s


def _target_heads(lines):
    """{line index: target name} for the lines of `lines` that open a named
    target, or {} when fewer than two DISTINCT targets are named (a single
    program page reads exactly as it always did).

    A markdown heading ("## acme") opens a target only when the part under it
    (to the next heading of the same or a higher level) carries its own scope
    section or an in/out-of-scope marker; a label line ("Program: acme") when
    the part under it (to the next target line) names a host or a scope
    section, or the name itself is a host. A scope heading ("## Out of scope")
    is never a target."""
    shapes = []                                   # (index, kind, level, name)
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line or len(line) > 200:
            continue
        m = _MD_HEAD_RE.match(line)
        if m:
            if _heading(line)[0]:
                continue
            name = _target_name(m.group(2))
            if name:
                shapes.append((i, "md", len(m.group(1)), name))
            continue
        s = re.sub(r"^(?:[-+•>*_]\s*)+|[*_\s]+$", "", line).replace("**", "").replace("__", "")
        if _TARGET_LABEL_RE.match(s) and not _heading(line)[0]:
            name = _target_name(s)
            if name:
                shapes.append((i, "label", 0, name))
    found = []                                    # (index, end, kind, name)
    for n, (i, kind, level, name) in enumerate(shapes):
        end = len(lines)
        if kind == "md":
            for j in range(i + 1, len(lines)):
                mm = _MD_HEAD_RE.match(lines[j].strip())
                if mm and len(mm.group(1)) <= level:
                    end = j
                    break
        elif n + 1 < len(shapes):
            end = shapes[n + 1][0]
        block = [ln.strip() for ln in lines[i + 1:end] if ln.strip()]
        if kind == "md":
            ok = any(_heading(ln)[0] in ("in", "out") or _SCOPE_WORDS_RE.search(ln)
                     for ln in block)
        else:
            ok = (_pattern(name) is not None
                  or any(_heading(ln)[0] or _hosts_with_pos(ln) for ln in block))
        if ok:
            found.append((i, end, kind, name))
    # A heading that holds other targets ("# Programs" over "## acme", "## globex")
    # is a container, not a target.
    heads = {i: name for i, end, kind, name in found
             if not (kind == "md" and any(i < j < end for j, _e, _k, _n in found))}
    if len({v.lower() for v in heads.values()}) < 2:
        return {}
    return heads


def extract(text, kind=None, src="message"):
    """The scope card written in `text` (a program page, a SCOPE.md, a user
    message). Only what a scope section or an explicit marker says counts: a
    host merely mentioned in prose is not scope. Never raises.

    Two or more named targets (see _target_heads) tag what each one lists with
    its name; the name of a target that is itself a host ("App: shop.example.com")
    is in scope for that target."""
    card = empty()
    try:
        if not isinstance(text, str) or not text.strip():
            return card
        text = text[:MAX_TEXT]
        kind = kind if kind in KINDS else detect_kind(text)
        src = str(src or "message")[:120]
        lines = text.splitlines()
        heads = _target_heads(lines)
        section, blank, tg = None, False, None
        for i, raw in enumerate(lines):
            line = raw.strip()
            if not line:
                blank = True
                continue
            if i in heads:
                tg = _clean_tg(heads[i])
                section, blank = None, False
                _add_target(card, tg, src)
                _add(card, "in", _pattern(tg), kind, src, tg)
                continue
            hk, rest = _heading(line)
            if hk:
                section, blank = hk, False
                if rest:
                    _take(card, rest, hk, kind, src, tg)
                continue
            if blank and section and not _scopeish(line):
                section = None
            blank = False
            _take(card, line, section, kind, src, tg)
    except Exception:                                            # noqa: BLE001
        return empty()
    return card


def _take(card, line, section, kind, src, tg=None):
    body = _clean(line)
    if not body:
        return
    clauses = [c for c in _SENT_SPLIT_RE.split(body) if c and c.strip()]
    found_host = False
    for clause in clauses:
        pairs = _assign(clause, section)
        for host, lst in pairs:
            found_host = True
            _add(card, lst, _pattern(host), kind, src, tg)
        if section == "rules":
            continue
        if _RULE_RE.search(clause):
            _add_rule(card, clause, kind, src, tg)
    if section == "rules":
        _add_rule(card, body, kind, src, tg)
    elif section == "out" and not found_host and re.search(r"[a-z]{3}", body, re.I) \
            and len(body) <= 100 and not _RULE_RE.search(body):
        # "Out of scope" lists issue types too (self-XSS, missing headers):
        # worth keeping as a rule line, never as a host.
        _add_rule(card, "Out of scope: " + body, kind, src, tg)


def normalize(card):
    """A clean copy of a card read from disk or built elsewhere."""
    out = empty()
    try:
        if not isinstance(card, dict):
            return out
        for lst in ("in", "out"):
            for e in card.get(lst) or ():
                if isinstance(e, dict) and isinstance(e.get("p"), str):
                    p = _pattern(e["p"])
                    k = e.get("kind") if e.get("kind") in KINDS else "program"
                    _add(out, lst, p, k, str(e.get("src") or "message")[:120],
                         _clean_tg(e.get("tg")))
        for r in card.get("rules") or ():
            if isinstance(r, dict) and isinstance(r.get("t"), str):
                k = r.get("kind") if r.get("kind") in KINDS else "program"
                _add_rule(out, r["t"], k, str(r.get("src") or "message")[:120],
                          _clean_tg(r.get("tg")))
        for t in card.get("targets") or ():
            if isinstance(t, dict):
                _add_target(out, t.get("name"), str(t.get("src") or "message")[:120])
    except Exception:                                            # noqa: BLE001
        return empty()
    return out


def merge(card, new):
    """`card` plus what `new` adds (union; an entry already present keeps its
    first source). Bounded. Never raises."""
    out = normalize(card)
    try:
        add = normalize(new)
        for lst in ("in", "out"):
            for e in add[lst]:
                _add(out, lst, e["p"], e["kind"], e["src"], e.get("tg"))
        for r in add["rules"]:
            _add_rule(out, r["t"], r["kind"], r["src"], r.get("tg"))
        for t in add.get("targets") or ():
            _add_target(out, t["name"], t["src"])
    except Exception:                                            # noqa: BLE001
        pass
    return out


def drop_source(card, src):
    """`card` without what came from `src` (a file that changed or went away)."""
    out = normalize(card)
    for lst in ("in", "out", "rules"):
        out[lst] = [e for e in out[lst] if e.get("src") != src]
    if "targets" in out:
        out["targets"] = [t for t in out["targets"] if t.get("src") != src]
        if not out["targets"]:
            del out["targets"]
    return out


def targets(card):
    """The card's targets as [{"name", "in", "out", "rules"}] (entries as in the
    card). Named targets in the order they were declared; each also gets the
    SHARED entries (written before any target heading). A card with no named
    target is ONE unnamed target ({"name": ""}, the whole card); an empty card
    has none. Never raises."""
    try:
        card = normalize(card)
        names = [t["name"] for t in card.get("targets") or ()]
        low = {n.lower() for n in names}
        for lst in ("in", "out", "rules"):
            for e in card[lst]:
                tg = e.get("tg")
                if tg and tg.lower() not in low:
                    names.append(tg)
                    low.add(tg.lower())
        if not names:
            if is_empty(card):
                return []
            return [{"name": "", "in": list(card["in"]), "out": list(card["out"]),
                     "rules": list(card["rules"])}]
        out = []
        for name in names[:MAX_TARGETS]:
            key = name.lower()

            def mine(e, key=key):
                tg = e.get("tg")
                return not tg or tg.lower() == key
            out.append({"name": name, "in": [e for e in card["in"] if mine(e)],
                        "out": [e for e in card["out"] if mine(e)],
                        "rules": [r for r in card["rules"] if mine(r)]})
        return out
    except Exception:                                            # noqa: BLE001
        return []


def _target_of(card, name):
    """The targets() row of `name` (case-insensitive). On a card with no named
    target, any name gets the one unnamed target (a host the goal named)."""
    ts = targets(card)
    if len(ts) == 1 and not ts[0]["name"]:
        return ts[0]
    key = " ".join(str(name or "").split()).lower()
    for t in ts:
        if t["name"].lower() == key:
            return t
    return None


# --------------------------------------------------------------------------- #
# Is a host in scope?
# --------------------------------------------------------------------------- #

def _target(host):
    """("ip", network) | ("dns", name) | None for a host string (port and
    brackets removed)."""
    h = _norm_host(host)
    if not h:
        return None
    net = _parse_ip(h)
    if net is not None:
        return "ip", net
    if h.count(":") == 1:
        h = h.split(":", 1)[0]
        net = _parse_ip(h)
        if net is not None:
            return "ip", net
    if re.fullmatch(r"(?:%s\.)*%s" % (_LABEL, _LABEL), h, re.I):
        return "dns", h
    return None


def _spec(pattern, target, lst):
    """Specificity of `pattern` matching `target`, or None. Exact hosts beat
    wildcards, longer suffixes beat shorter ones, smaller networks beat larger."""
    kind, val = target
    pnet = _parse_ip(pattern)
    if pnet is not None:
        if kind != "ip" or pnet.version != val.version:
            return None
        if val.num_addresses == 1:
            return (1, pnet.prefixlen) if val.network_address in pnet else None
        # a range target: OUT counts on any overlap, IN only when it holds it all
        if lst == "out":
            return (1, pnet.prefixlen) if val.overlaps(pnet) else None
        return (1, pnet.prefixlen) if val.subnet_of(pnet) else None
    if kind != "dns":
        return None
    if pattern.startswith("*."):
        suffix = pattern[2:]
        return (2, len(suffix)) if val.endswith("." + suffix) else None
    return (3, len(pattern)) if val == pattern else None


def _best(entries, target, lst):
    best, best_e = None, None
    for e in entries or ():
        try:
            s = _spec(e["p"], target, lst)
        except Exception:                                        # noqa: BLE001
            s = None
        if s is not None and (best is None or s > best):
            best, best_e = s, e
    return best, best_e


def decide(host, card):
    """(True|False|None, deciding entry). False = explicitly OUT of scope."""
    try:
        t = _target(host)
        if t is None or not isinstance(card, dict):
            return None, None
        si, ei = _best(card.get("in"), t, "in")
        so, eo = _best(card.get("out"), t, "out")
        if so is None and si is None:
            return None, None
        if so is not None and (si is None or so >= si):
            return False, eo
        return True, ei
    except Exception:                                            # noqa: BLE001
        return None, None


def in_scope(host, card):
    """True (listed in scope), False (listed OUT of scope; an out entry wins a
    tie) or None (not listed: never blocked)."""
    return decide(host, card)[0]


# --------------------------------------------------------------------------- #
# The note
# --------------------------------------------------------------------------- #

def _fit(items, budget):
    """", ".join(items) cut to `budget` chars with a "(+N more)" tail."""
    if not items:
        return ""
    out, used = [], 0
    for i, it in enumerate(items):
        add = len(it) + (2 if out else 0)
        tail = len(" (+%d more)" % (len(items) - i)) if i < len(items) - 1 else 0
        if used + add + tail > budget and out:
            return ", ".join(out) + " (+%d more)" % (len(items) - i)
        if used + add > budget:
            return it[:max(0, budget - 3)] + "..."
        out.append(it)
        used += add
    return ", ".join(out)


def _label(e):
    return e["p"] + (" (owner)" if e.get("kind") == "owner-declared" else "")


def render(card, max_chars=NOTE_MAX_CHARS):
    """The PROGRAM SCOPE note for `card`: deterministic (sorted, no clock), never
    longer than max_chars; "" for an empty card. OUT of scope is kept first and
    longest, the in-scope list and the rules give way first."""
    return _render(card, max_chars, None)


def render_target(card, name, max_chars=NOTE_MAX_CHARS):
    """The scope note of ONE target of `card` (its own entries plus the shared
    ones), same rules as render(); "" when the card does not know `name`. On a
    card with no named target the whole card is that target's scope."""
    try:
        t = _target_of(card, name)
        label = " ".join(str(name or "").split())[:TARGET_NAME_MAX]
        if t is None or not label:
            return ""
        sub = {lst: [{k: v for k, v in e.items() if k != "tg"} for e in t[lst]]
               for lst in ("in", "out", "rules")}
        return _render(sub, max_chars, t["name"] or label)
    except Exception:                                            # noqa: BLE001
        return ""


def _render(card, max_chars, target):
    try:
        card = normalize(card)
        if is_empty(card):
            return ""
        kinds = {e["kind"] for lst in ("in", "out", "rules") for e in card[lst]}
        who = ("program rules and owner-declared" if len(kinds) > 1
               else "owner-declared: your own app" if kinds == {"owner-declared"}
               else "from the program")
        if target is None:
            head = "PROGRAM SCOPE for this project folder (%s). Check every host against it before any request." % who
        else:
            head = ("SCOPE OF TARGET %s (%s). Test only this target; check every host against it "
                    "before any request." % (target, who))
        foot = ("A host not listed is NOT in the listed scope: confirm before testing it "
                "(package registries and docs are fine). The hub blocks tool calls aimed at an "
                "OUT-of-scope host.")
        # A host two targets both list is shown once.
        outs = sorted({_label(e) for e in card["out"]})
        ins = sorted({_label(e) for e in card["in"]})
        rules = list(dict.fromkeys(r["t"] for r in card["rules"]))
        room = max(0, int(max_chars) - len(head) - len(foot) - 3)
        lines = [head]
        if outs:
            pre = "OUT of scope, never send a request there: "
            lines.append(pre + _fit(outs, max(20, min(room, int(room * 0.5)) - len(pre))))
            room -= len(lines[-1]) + 1
        if ins:
            pre = "In scope: "
            lines.append(pre + _fit(ins, max(20, int(room * 0.55)) - len(pre)))
            room -= len(lines[-1]) + 1
        if rules and room > 40:
            pre = "Rules: "
            lines.append(pre + _fit(rules, room - len(pre)).replace(", ", "; "))
        lines.append(foot)
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max(0, int(max_chars) - 3)].rstrip() + "..."
        return text
    except Exception:                                            # noqa: BLE001
        return ""


# --------------------------------------------------------------------------- #
# What a tool call would hit
# --------------------------------------------------------------------------- #

# Argument keys whose value IS a target; keys whose value is a command or
# code to run. Anything else (file contents, patches, prompts, search queries)
# is not a request and is never read: an agent writing "payments.example.com is
# out of scope" into its notes is not sending a request there.
_URL_KEYS = frozenset((
    "url urls uri href link endpoint target targets host hosts hostname domain domains "
    "address ip origin base_url baseurl website site").split())
_CMD_KEYS = frozenset("command cmd commands script shell code args argv".split())
_FETCH_NAME_RE = re.compile(r"fetch|http|request|curl|browse|navigate|web_?get|scan|crawl|spider|"
                            r"probe|ping|dns|whois|recon", re.I)
_NET_PROGS = frozenset((
    "curl wget httpx http https httpie xh nmap masscan rustscan ffuf nuclei sqlmap gobuster "
    "dirsearch feroxbuster dirb wfuzz nikto subfinder amass dnsx naabu katana gau waybackurls "
    "hakrawler dig nslookup host whois ping traceroute tracert nc ncat netcat telnet ssh scp "
    "openssl testssl testssl.sh whatweb wafw00f arjun dalfox xsstrike commix hydra medusa "
    "invoke-webrequest invoke-restmethod iwr irm aria2c lynx w3m chromium chrome firefox "
    "playwright puppeteer zap zap-cli burp interactsh-client gospider paramspider").split())
_CODE_PROGS = frozenset((
    "python python3 python2 py node nodejs deno bun ruby perl php pwsh powershell "
    "powershell.exe pwsh.exe").split())
_SHELLS = frozenset((
    "bash sh zsh dash ksh fish wsl cmd eval xargs timeout env sudo nohup watch exec time nice "
    "stdbuf proxychains proxychains4 script").split())


def _prog(tok):
    t = tok.strip("\"'`()").lower()
    t = re.split(r"[\\/]", t)[-1]
    if t.endswith(".exe"):
        t = t[:-4]
    return t


def _segments(cmd):
    """A shell line as [[token, ...], ...] split on ; && || | and newlines, with
    quoted strings kept whole (quotes included) -- a tiny lexer, not a shell."""
    segs, toks, cur, quote, i = [], [], [], None, 0
    n = len(cmd)

    def flush_tok():
        if cur:
            toks.append("".join(cur))
            cur.clear()

    def flush_seg():
        flush_tok()
        if toks:
            segs.append(list(toks))
            toks.clear()

    while i < n:
        ch = cmd[i]
        if quote:
            cur.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < n:
                cur.append(cmd[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            cur.append(ch)
        elif ch in ";|\n" or (ch == "&" and cmd[i:i + 2] == "&&"):
            flush_seg()
            if cmd[i:i + 2] in ("&&", "||"):
                i += 1
        elif ch.isspace():
            flush_tok()
        else:
            cur.append(ch)
        i += 1
    flush_seg()
    return segs


def _cmd_hosts(cmd, depth=0):
    """Hosts a shell command line would contact: everything after a network
    tool (curl, nmap, ffuf...) and the URLs inside code a runner executes
    (python -c, node -e). A URL echoed into a file or passed to git/npm is not
    counted."""
    found = []
    if not isinstance(cmd, str) or depth > 3:
        return found
    for toks in _segments(cmd[:20000]):
        net_at = None
        code = shell = False
        for i, tok in enumerate(toks):
            p = _prog(tok)
            if p in _NET_PROGS and net_at is None:
                net_at = i
            elif p in _CODE_PROGS:
                code = True
            elif p in _SHELLS:
                shell = True
            # a quoted command handed to a shell (bash -c "curl ...", wsl ...):
            # read it as a command line too -- never one that is only echoed.
            if shell and len(tok) > 2 and tok[0] in "\"'" and tok[-1] == tok[0]:
                found += _cmd_hosts(tok[1:-1], depth + 1)
        if net_at is not None:
            rest = " ".join(toks[net_at + 1:])
            rest = re.sub(r"(?<=\s)--?[\w-]+=", " ", " " + rest)
            found += hosts_in_text(rest, bare=True)
        elif code:
            found += hosts_in_text(" ".join(toks), bare=False)
    return found


def _walk(name, value, key, out, fetchy, depth=0):
    if depth > 6:
        return
    k = str(key or "").lower()
    if isinstance(value, dict):
        for kk, vv in value.items():
            _walk(name, vv, kk, out, fetchy, depth + 1)
        return
    if isinstance(value, list):
        if k in _CMD_KEYS and value and all(isinstance(v, str) for v in value):
            out += _cmd_hosts(" ".join(('"%s"' % v) if " " in v else v for v in value))
            return
        for vv in value:
            _walk(name, vv, key, out, fetchy, depth + 1)
        return
    if not isinstance(value, str) or not value.strip():
        return
    if k in _URL_KEYS or (fetchy and k not in _CMD_KEYS):
        v = value.strip()
        hs = hosts_in_text(v, bare=False)
        if not hs:
            p = _target(v.split("/", 1)[0])
            if p is not None:
                hs = [_norm_host(v.split("/", 1)[0])]
        out += hs
    elif k in _CMD_KEYS:
        if k == "code":
            out += hosts_in_text(value, bare=False)
        else:
            out += _cmd_hosts(value)


def targets_in_call(name, arguments):
    """Unique hosts a tool call (name + JSON arguments) would send a request to."""
    out = []
    try:
        fetchy = bool(_FETCH_NAME_RE.search(str(name or "")))
        args = arguments
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except ValueError:
                args = {"command": args}
        if isinstance(args, (dict, list)):
            _walk(name, args, "", out, fetchy)
        seen, res = set(), []
        for h in out:
            h = _norm_host(h)
            if h and h not in seen:
                seen.add(h)
                res.append(h)
        return res
    except Exception:                                            # noqa: BLE001
        return []


def check_calls(tool_calls, card):
    """(host, deciding out entry) for the first tool call aimed at a host the
    card lists OUT of scope, else None. A host that is merely not listed is
    never returned."""
    try:
        if not card or not card.get("out"):
            return None
        for tc in tool_calls or ():
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
            for host in targets_in_call(fn.get("name"), fn.get("arguments")):
                ok, entry = decide(host, card)
                if ok is False:
                    return host, entry
    except Exception:                                            # noqa: BLE001
        return None
    return None


def block_message(host, entry):
    kind = (entry or {}).get("kind") or "program"
    src = (entry or {}).get("src") or "message"
    where = "a user message" if src == "message" else src
    return ("The hub blocked a call to %s: it is out of scope for this program (%s: %s). "
            "Nothing was sent. Pick an in-scope target, or ask the user." % (host, kind, where))


# --------------------------------------------------------------------------- #
# The streamed guard
# --------------------------------------------------------------------------- #

HOLD_MAX_BYTES = 256 * 1024
HOLD_MAX_SECONDS = 30.0
_FRAME_CAP = 1 << 20


def _units(items, framing):
    if framing == "lines":
        yield from items
        return
    buf = b""
    for chunk in items:
        if not chunk:
            continue
        if not isinstance(chunk, (bytes, bytearray)):
            chunk = str(chunk).encode("utf-8", "ignore")
        buf += chunk
        while True:
            cuts = [c for c in (buf.find(b"\n\n"), buf.find(b"\r\n\r\n")) if c >= 0]
            if not cuts:
                break
            cut = min(cuts)
            sep = 4 if buf[cut:cut + 4] == b"\r\n\r\n" else 2
            frame, buf = buf[:cut + sep], buf[cut + sep:]
            yield frame
        if len(buf) > _FRAME_CAP:
            yield buf
            buf = b""
    if buf:
        yield buf


def _payload(unit):
    raw = unit if isinstance(unit, (bytes, bytearray)) else str(unit).encode("utf-8", "ignore")
    datas = [ln.strip()[5:].strip() for ln in raw.splitlines() if ln.strip().startswith(b"data:")]
    if len(datas) != 1:
        return "other", None
    if datas[0] == b"[DONE]":
        return "done", None
    try:
        obj = json.loads(datas[0].decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return "other", None
    return ("json", obj) if isinstance(obj, dict) else ("other", None)


def guard_stream(items, framing, check, on_event=None, hold_max_bytes=HOLD_MAX_BYTES,
                 hold_max_seconds=HOLD_MAX_SECONDS, clock=time.monotonic):
    """Wrap an OpenAI chat SSE iterator ("frames": raw byte chunks, "lines": one
    SSE line per item) so a tool call can be checked BEFORE the client gets it.

    Every frame passes untouched until the first tool-call delta; from there to
    the end of the stream the frames are HELD (the calls' arguments arrive in
    pieces, and a host is only known once they are complete). At the end
    `check(calls)` (calls = [{"name", "arguments"}]) returns None -> the held
    frames go out byte for byte, or a message -> the calls are replaced by that
    message as assistant text and the stream ends at finish_reason "stop".
    Fail-open bound: a hold past `hold_max_bytes` or `hold_max_seconds` (a huge
    file written through a tool call, a slow upstream) is released unchecked
    (on_event("gave_up", why)); the calls are still read as they pass and a
    would-be block is reported afterwards (on_event("missed", message)).
    on_event("blocked", message) for a block. Never raises out of `check`."""
    end = b"\n\n" if framing == "frames" else b""
    calls, order, last = {}, [], [None]
    template = {}
    text_seen = [False]

    def emit(obj):
        return b"data: " + json.dumps(obj).encode("utf-8") + end

    def ev(kind, detail):
        if on_event is not None:
            try:
                on_event(kind, detail)
            except Exception:                                    # noqa: BLE001
                pass

    def note(tcs):
        for tc in tcs:
            if not isinstance(tc, dict):
                continue
            idx = tc.get("index")
            key = ("i", idx) if isinstance(idx, int) and not isinstance(idx, bool) else \
                ("d", tc.get("id")) if tc.get("id") else last[0] or ("i", 0)
            if key not in calls:
                calls[key] = {"name": "", "arguments": ""}
                order.append(key)
            last[0] = key
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            if fn.get("name") and not calls[key]["name"]:
                calls[key]["name"] = str(fn.get("name"))
            a = fn.get("arguments")
            if a is not None:
                calls[key]["arguments"] += a if isinstance(a, str) else json.dumps(a)

    def verdict():
        try:
            return check([{"function": calls[k]} for k in order])
        except Exception:                                        # noqa: BLE001
            return None

    def settle(held):
        msg = verdict()
        if not msg:
            yield from held
            return
        ev("blocked", msg)
        base = {"id": template.get("id") or "chatcmpl-scope",
                "object": "chat.completion.chunk",
                "created": template.get("created") or 0,
                "model": template.get("model") or ""}
        yield emit(dict(base, choices=[{"index": 0,
                                        "delta": {"content": ("\n\n" + msg) if text_seen[0] else msg},
                                        "finish_reason": None}]))
        yield emit(dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}]))
        done = None
        for u in held:
            k, o = _payload(u)
            if k == "done":
                done = u
            elif k == "json" and not o.get("choices") and o.get("usage"):
                yield u                      # usage-only frame
        if done is not None:
            yield done

    units = _units(items, framing)
    held, holding, gave_up, t0, nbytes = [], False, False, 0.0, 0
    try:
        for unit in units:
            kind, obj = _payload(unit)
            tcs = None
            if kind == "json":
                for k in ("id", "created", "model"):
                    if obj.get(k) is not None and k not in template:
                        template[k] = obj[k]
                ch = obj.get("choices")
                if isinstance(ch, list) and ch and isinstance(ch[0], dict):
                    d = ch[0].get("delta")
                    if isinstance(d, dict) and isinstance(d.get("tool_calls"), list):
                        tcs = d["tool_calls"]
                    if isinstance(d, dict) and d.get("content") and not holding:
                        text_seen[0] = True
            if tcs:
                note(tcs)
            if gave_up:
                yield unit
                continue
            if not holding:
                if tcs:
                    holding, t0 = True, clock()
                    held, nbytes = [unit], len(unit or b"")
                    continue
                yield unit
                continue
            held.append(unit)
            nbytes += len(unit or b"")
            if kind == "done":
                yield from settle(held)
                held, holding = [], False
                continue
            if nbytes > hold_max_bytes or clock() - t0 > hold_max_seconds:
                gave_up = True
                ev("gave_up", "%d bytes, %.0fs" % (nbytes, clock() - t0))
                yield from held
                held = []
        if holding and held:
            yield from settle(held)
        elif gave_up:
            msg = verdict()
            if msg:
                ev("missed", msg)
    finally:
        for it in (units, items):
            try:
                it.close()
            except Exception:                                    # noqa: BLE001
                pass


# --------------------------------------------------------------------------- #
# Storage: one card per project folder, outside the project
# --------------------------------------------------------------------------- #

class Store:
    """scopes.json: {"v": 1, "folders": {folder key: {"card", "files", "t"}}}.
    Atomic replace, at most MAX_FOLDERS folders (the least recently updated go
    first), every read fails open to "nothing stored"."""

    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.Lock()
        self._data = None

    def _load(self):
        if self._data is not None:
            return self._data
        data = {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict) and isinstance(raw.get("folders"), dict):
                data = raw["folders"]
        except Exception:                                        # noqa: BLE001
            data = {}
        self._data = data
        return data

    def get(self, key):
        try:
            with self._lock:
                ent = self._load().get(str(key))
            if not isinstance(ent, dict):
                return None
            files = ent.get("files") if isinstance(ent.get("files"), dict) else {}
            return {"card": normalize(ent.get("card")),
                    "files": {str(k): v for k, v in files.items()
                              if isinstance(v, list) and len(v) == 2}}
        except Exception:                                        # noqa: BLE001
            return None

    def put(self, key, card, files=None, now=None):
        try:
            with self._lock:
                data = dict(self._load())
                data[str(key)] = {"card": normalize(card),
                                  "files": dict(files or {}),
                                  "t": float(now if now is not None else time.time())}
                if len(data) > MAX_FOLDERS:
                    keep = sorted(data, key=lambda k: -float((data[k] or {}).get("t") or 0))
                    data = {k: data[k] for k in keep[:MAX_FOLDERS]}
                d = os.path.dirname(self.path) or "."
                os.makedirs(d, exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix=".scopes-", suffix=".tmp", dir=d)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump({"v": 1, "folders": data}, f, ensure_ascii=False,
                                  separators=(",", ":"))
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp, self.path)
                except Exception:                                # noqa: BLE001
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
                self._data = data
            return True
        except Exception:                                        # noqa: BLE001
            return False
