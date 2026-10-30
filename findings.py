"""Security finding claims in an agent's reply, checked against what the hub OBSERVED.

Free models often "find" vulnerabilities that are not real. This module is the pure
half of the hub's finding verifier (stdlib only, no import from the app, never raises):

  * detect(text)                -> the finding CLAIMS in one assistant reply: a
                                   vulnerability class stated as found / confirmed /
                                   vulnerable / exploitable (or a report-style heading).
                                   Hedged or hypothetical talk ("might", "to test",
                                   "check whether") is not a claim.
  * tool_results(messages)      -> the OBSERVED tool results of an OpenAI-shaped
                                   conversation (each call's name + arguments + output).
  * evidence(claim, results)    -> a deterministic first pass: is there an observed tool
                                   result that names the target AND shows the response
                                   that proves this class of issue? Known false positives
                                   are rules as data (_RULES): reflected input without
                                   execution is not XSS, a 200 alone is not access, a
                                   version banner alone is not a vulnerability...
  * Ledger(root)                -> one JSON file per project folder (never inside it):
                                   re-detection of a finding updates its row.
  * note_text(rows)             -> the deterministic "HUB FINDINGS CHECK" note.

The hub runs no command and sends nothing to a target: it only reads tool output the
agent's own CLI already produced. Nothing here is specific to a bug-bounty program; a
user testing their own application gets exactly the same checks.
"""
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from urllib.parse import unquote, urlsplit

STATUSES = ("proven", "unverified", "false_positive", "needs_review")
MAX_FINDINGS = 200            # rows per project ledger; the least recently seen go first
MAX_CLAIMS = 10               # claims read from one reply
NOTE_MAX_CHARS = 900
NOTE_MAX_AGE = 7 * 86400      # a finding not seen for a week leaves the note
MAX_TEXT = 40000              # chars of one reply that are read
MAX_RESULTS = 200             # tool results kept per conversation pass
MAX_OUTPUT = 20000            # chars of one tool result kept (head + tail)
_TITLE_MAX = 90
_REASON_MAX = 160
_POINTER_MAX = 220

# --------------------------------------------------------------------------- #
# Vulnerability classes
# --------------------------------------------------------------------------- #

# (id, label, pattern). Order only breaks ties: the class named FIRST in a sentence wins.
_CLASSES = (
    ("subdomain_takeover", "Subdomain takeover",
     r"sub-?domain\s+take-?over|domain\s+take-?over|dangling\s+(?:cname|dns)"),
    ("request_smuggling", "HTTP request smuggling",
     r"request\s+smuggling|desync\s+attack"),
    ("cache_poisoning", "Web cache poisoning", r"cache\s+(?:poisoning|deception)"),
    ("open_redirect", "Open redirect",
     r"open[\s-]+redirect(?:ion)?s?|unvalidated\s+redirects?"),
    ("ssrf", "SSRF", r"\bssrf\b|server[\s-]+side\s+request\s+forgery"),
    ("csrf", "CSRF",
     r"(?:\bcsrf\b|\bxsrf\b)(?![\s_-]*(?:tokens?|protection|headers?|cookies?|middleware|checks?|fields?"
     r"|nonces?|secrets?))|cross[\s-]+site\s+request\s+forgery"),
    ("xss", "Cross-site scripting (XSS)",
     r"(?<!x-)\bxss\b(?![\s_-]*(?:protection|filter|auditor))|cross[\s-]+site\s+scripting"),
    ("sqli", "SQL injection", r"\bsqli\b|\bsql[\s-]+injection|\bblind\s+sql\b"),
    ("ssti", "Server-side template injection", r"\bssti\b|template\s+injection"),
    ("xxe", "XXE", r"\bxxe\b|xml\s+external\s+entit\w*"),
    ("rce", "Remote code execution",
     r"\brce\b|remote\s+code\s+execution|(?:os\s+)?command\s+injection|arbitrary\s+code\s+execution"
     r"|insecure\s+deserializ\w*"),
    ("path_traversal", "Path traversal / file inclusion",
     r"\blfi\b|\brfi\b|path\s+traversal|directory\s+traversal|(?:local|remote)\s+file\s+inclusion"),
    ("idor", "IDOR", r"\bidor\b|\bbola\b|insecure\s+direct\s+object\s+ref\w*|broken\s+object[\s-]+level"),
    ("auth_bypass", "Authentication / access control bypass",
     r"auth(?:entication|orization)?\s+bypass|bypass(?:es|ed|ing)?\s+(?:the\s+)?"
     r"(?:auth\w*|login|access\s+control|2fa|mfa)|broken\s+access\s+control|privilege\s+escalation"
     r"|account\s+takeover|unauthori[sz]ed\s+access"),
    ("cors", "CORS misconfiguration",
     r"\bcors\s+(?:misconfig\w*|issue|vulnerab\w*|bypass)|misconfigured\s+cors"),
    ("jwt", "JWT weakness",
     r"\bjwt\s+(?:none|alg\w*|weak\w*|forg\w*|bypass|secret|confusion)|\balg(?:orithm)?\s*[:=]\s*[\"']?none\b"),
    ("clickjacking", "Clickjacking", r"click-?jacking|ui\s+redress\w*"),
    ("info_disclosure", "Information disclosure",
     r"information\s+(?:disclosure|leak\w*)|info\s+leak\w*|sensitive\s+(?:data|information)\s+"
     r"(?:exposure|leak\w*|disclos\w*)|exposed\s+(?:\.git|\.env|api\s+keys?|secrets?|credentials?|backups?)"
     r"|leaked\s+(?:api\s+keys?|secrets?|credentials?|tokens?)|source\s+code\s+disclosure"
     r"|directory\s+listing"),
    ("race_condition", "Race condition", r"race\s+condition|\btoctou\b"),
)
_CLASS_RES = tuple((cid, label, re.compile(rx, re.I)) for cid, label, rx in _CLASSES)
CLASS_LABELS = {cid: label for cid, label, _rx in _CLASSES}
_ACCESS_CLASSES = ("idor", "auth_bypass")

# --------------------------------------------------------------------------- #
# Claim vs hedge vs negation
# --------------------------------------------------------------------------- #

_CLAIM_RE = re.compile(
    r"\b(?:found|discovered|identified|confirmed|verified|validated|reproduced|exploited|exploitable"
    r"|vulnerable|susceptible|affected|successful(?:ly)?|achieved|obtained|works|worked|detected"
    r"|able\s+to\s+(?:confirm|exploit|reproduce|trigger|extract|read|access|bypass|execute|inject)"
    r"|proof[\s-]+of[\s-]+concept|poc|leads?\s+to|allows?\s+(?:an?\s+)?(?:attacker|unauthenticated|remote|any)"
    r"|(?:present|exists?)\s+(?:on|in|at)|there\s+is\s+an?|we\s+have\s+an?)\b", re.I)
# Verb / clause hedges count when they come BEFORE the claim (see _hedged).
_HEDGE_VERB_RE = re.compile(
    r"\b(?:might|may|could|would|perhaps|maybe|possibly|potentially|probably|likely|seems?|appears?"
    r"|if|whether|unless|suppose|assuming|let(?:'s|\s+(?:me|us))"
    r"|(?:will|going\s+to|plan\s+to|want\s+to|need\s+to|try\s+to|trying\s+to|about\s+to)\s+"
    r"(?:test|check|try|verify|confirm|probe|look|see|investigate|fuzz|send|run)"
    r"|(?<!able )to\s+(?:test|check|verify|confirm|probe|see|determine|look|try|find|explore|investigate)"
    r"|check(?:ing)?\s+(?:if|whether|for)|test(?:ing)?\s+(?:if|whether|for)|look(?:ing)?\s+for"
    r"|hunt(?:ing)?\s+for|next\s+step|todo|should\s+(?:test|check|try|verify|confirm|look))\b", re.I)
# Adjective hedges count when they sit right before the class ("possible XSS").
_HEDGE_ADJ_RE = re.compile(
    r"\b(?:potential(?:ly)?|possible|possibly|suspected|suspicious|likely|probable|candidate"
    r"|hypothetical|theoretical|unconfirmed|unverified|untested|speculative)\b[\w\s,/-]{0,30}$", re.I)
_NEGATION_RE = re.compile(
    r"(?:\bnot|n't|\bnever)\s+(?:\w+\s+){0,2}(?:vulnerable|affected|exploitable|confirmed|verified"
    r"|reproduc\w*|found|find|work\w*|susceptible|present|exist\w*|detected|an?\s+issue)\b"
    r"|\b(?:no|zero|none\s+of\s+the)\s+(?:\w+[\s-]+){0,3}?(?:xss|sqli|ssrf|csrf|idor|rce|lfi|ssti|xxe"
    r"|vulnerabilit\w*|issues?|findings?|injection|redirect|evidence|bypass|takeover|disclosure)\b"
    r"(?!\s*(?:token|protection|header|check|validation|filter|nonce|limit|cookie))"
    r"|\bfalse\s+positive|\bruled\s+out|\bnothing\s+(?:found|exploitable|vulnerable)"
    r"|\b(?:properly|correctly|safely)\s+(?:escaped|sanitiz\w*|encoded|validated|rejected|blocked)"
    r"|\bwas\s+(?:blocked|rejected|filtered|escaped|sanitiz\w*)|\bblocked\s+by\b", re.I)

_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
_MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_BOLD_LINE_RE = re.compile(r"^\s*(?:\d+[.)]\s*)?(\*\*|__)(.+?)\1\s*:?\s*$")
_PREFIX_HEADING_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?\s*(?:finding|vulnerability|vuln|issue|bug)\s*#?\s*\d*\s*(?:\*\*)?\s*"
    r"[:\-–—.]\s*\S", re.I)
_FINDING_PREFIX_RE = re.compile(
    r"^\s*(?:\d+[.)]\s*)?(?:finding|vulnerability|vuln|issue|bug)\s*#?\s*\d*\s*[:\-–—.]", re.I)
_REPORT_FIELD_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?\s*(?:severity|impact|cvss(?:\s+score)?|steps\s+to\s+reproduce"
    r"|reproduction(?:\s+steps)?|proof\s+of\s+concept|poc|affected\s+(?:url|endpoint|asset|host)"
    r"|vulnerable\s+(?:url|endpoint|parameter))\s*(?:\*\*)?\s*:")
_FINDINGS_SECTION_RE = re.compile(
    r"\b(?:findings?|vulnerabilit(?:y|ies)|confirmed\s+(?:issues|bugs|vulnerabilities)"
    r"|issues\s+found|bugs?\s+found|results|summary\s+of\s+(?:findings|issues)|report)\b", re.I)
_HEDGED_SECTION_RE = re.compile(
    r"\b(?:next\s+steps?|to\s+(?:test|do|check|verify|investigate)|todo|plan|methodology|approach"
    r"|test\s+(?:plan|cases?|ideas)|ideas|leads|potential|possible|suspected|candidates?|hypothes\w*"
    r"|untested|unverified|not\s+(?:yet\s+)?(?:confirmed|verified|tested)|out\s+of\s+scope"
    r"|recommendations?|remediation|mitigations?|false\s+positives?|ruled\s+out|negative\s+results"
    r"|what\s+(?:i|we)\s+(?:will|would|could))\b", re.I)
_ITEM_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)]|\|)\s*")
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+(?=[A-Z0-9\[(*\"'`])|;\s+")
_THINK_RE = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.S | re.I)

_SEV_RES = (
    re.compile(r"\b(?:severity|cvss|risk|priority|impact\s+rating)\b[^\n]{0,24}?\b(critical|high|medium"
               r"|moderate|low|informational|info|p[1-5])\b", re.I),
    re.compile(r"[(\[]\s*(critical|high|medium|moderate|low|p[1-5])\s*(?:severity|risk)?\s*[)\]]", re.I),
    re.compile(r"\b(critical|high|medium|moderate|low)[\s-]+(?:severity|risk|impact)\b", re.I),
)
_SEV_NORM = {"critical": "critical", "high": "high", "medium": "medium", "moderate": "medium",
             "low": "low", "informational": "info", "info": "info", "p1": "critical",
             "p2": "high", "p3": "medium", "p4": "low", "p5": "info"}

# --------------------------------------------------------------------------- #
# Targets
# --------------------------------------------------------------------------- #

_URL_RE = re.compile(r"https?://[^\s<>\"'`)\]|,]+", re.I)
_HOST_RE = re.compile(
    r"(?<![\w@./-])(?:localhost|\d{1,3}(?:\.\d{1,3}){3}|(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z]{2,24})(?::\d{2,5})?(?![\w-])", re.I)
_PATH_RE = re.compile(r"(?<![\w.:/~<-])/[A-Za-z0-9._~%!$&'()*+,;=:@{}-]+(?:/[A-Za-z0-9._~%!$&'()*+,;=:@{}-]*)*")
_FILE_EXTS = frozenset((
    "py", "js", "ts", "tsx", "jsx", "json", "html", "htm", "php", "txt", "md", "css", "xml", "yml",
    "yaml", "sh", "go", "rb", "java", "jsp", "asp", "aspx", "png", "jpg", "jpeg", "gif", "svg", "log",
    "csv", "env", "lock", "toml", "ini", "cfg", "conf", "pdf", "zip", "gz", "tar", "exe", "dll", "so",
    "bak", "sql", "db", "pem", "key", "crt", "map", "vue", "rs", "c", "h", "cpp", "cs", "kt", "swift",
    "mjs", "cjs", "lua", "pl", "ps1", "bat", "cmd", "doc", "docx", "xls", "xlsx", "jar", "war"))
_PAYLOAD_PATHS = re.compile(
    r"^/(?:etc/(?:passwd|shadow|hosts|issue)|proc/|windows/|winnt/|bin/|dev/|tmp/|usr/|var/log|boot\.ini)",
    re.I)
_ID_SEG_RE = re.compile(
    r"^(?:\d+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{16,}|\{[\w-]+\}"
    r"|:[\w-]+|<[\w-]+>)$", re.I)


def _clean_host(h):
    h = (h or "").strip().strip(".").lower()
    if not h:
        return ""
    if h.startswith("www."):
        h = h[4:]
    for port in (":80", ":443"):
        if h.endswith(port):
            h = h[: -len(port)]
    return h


def _norm_path(p):
    """'/api/Users/123/' -> '/api/users/{id}' (query, fragment and a trailing slash dropped)."""
    p = (p or "").split("?", 1)[0].split("#", 1)[0].strip()
    segs = []
    for s in p.split("/"):
        s = s.strip()
        if not s:
            continue
        segs.append("{id}" if _ID_SEG_RE.match(s) else s.lower())
    return "/" + "/".join(segs) if segs else ""


def _valid_host(h):
    if not h:
        return False
    base = h.split(":", 1)[0]
    if base == "localhost" or re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", base):
        return True
    tld = base.rsplit(".", 1)[-1]
    return "." in base and tld.isalpha() and tld not in _FILE_EXTS


def find_target(text):
    """(host, path) named in `text` -- a URL first, then a host (with the path right after
    it), then a bare endpoint path. ("", "") when nothing is named."""
    text = text or ""
    urls = []
    for m in _URL_RE.finditer(text):
        try:
            u = urlsplit(m.group(0).rstrip(".:;!?"))
        except ValueError:
            continue
        host = _clean_host(u.netloc.rsplit("@", 1)[-1])
        if _valid_host(host):
            urls.append((host, _norm_path(u.path)))
    if urls:
        # "my app at http://localhost:3000 ... SQL injection at http://localhost:3000/api/x":
        # the URL with an endpoint is the target, the bare origin is just the app.
        return next((u for u in urls if u[1]), urls[0])
    for m in _HOST_RE.finditer(text):
        host = _clean_host(m.group(0))
        if not _valid_host(host):
            continue
        rest = text[m.end():]
        pm = re.match(r"(/[^\s<>\"'`)\]|,]*)", rest)
        if pm:
            return host, _norm_path(pm.group(1))
        # "The /api/fetch endpoint on api.example.com": host and path named apart.
        return host, _bare_path(text)
    return "", _bare_path(text)


def _bare_path(text):
    for m in _PATH_RE.finditer(text or ""):
        p = m.group(0).rstrip(".:;!?,)")
        if len(p) < 2 or _PAYLOAD_PATHS.match(p) or re.match(r"^/[a-z]:", p, re.I):
            continue
        np = _norm_path(p)
        if np and np != "/":
            return np
    return ""


def target_key(host, path):
    return (host or "") + (path or "")


def _registrable(host):
    base = (host or "").split(":", 1)[0]
    if base == "localhost" or re.match(r"^\d", base):
        return base
    parts = base.split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "net", "gov", "ac", "edu"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

def _first_class(text):
    best = None
    for cid, _label, rx in _CLASS_RES:
        m = rx.search(text)
        if m and (best is None or m.start() < best[1]):
            best = (cid, m.start())
    return best


def _hedged(sentence, class_pos):
    """True when the sentence talks about the class hypothetically."""
    before = sentence[:class_pos]
    if _HEDGE_ADJ_RE.search(before[-40:]):
        return True
    claim = _CLAIM_RE.search(sentence)
    anchor = max(class_pos, claim.start() if claim else class_pos)
    return bool(_HEDGE_VERB_RE.search(sentence[:anchor]))


def _severity(*texts):
    for t in texts:
        if not t:
            continue
        for rx in _SEV_RES:
            m = rx.search(t)
            if m:
                return _SEV_NORM.get(m.group(1).lower(), "")
    return ""


def _clip(s, n):
    s = " ".join(str(s or "").split())
    s = "".join(ch for ch in s if ch.isprintable())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


_FIELD_LINE_RE = re.compile(
    r"^\s*(?:severity|impact|cvss(?:\s+score)?|steps\s+to\s+reproduce|proof\s+of\s+concept|poc"
    r"|affected\s+\w+|vulnerable\s+\w+|url|endpoint|parameter|status|type|asset|cwe|host|target)\s*:", re.I)


def _heading_text(line):
    for rx in (_MD_HEADING_RE, _BOLD_LINE_RE):
        m = rx.match(line)
        if m:
            text = m.group(m.lastindex)
            return None if _FIELD_LINE_RE.match(text) else text
    if _PREFIX_HEADING_RE.match(line) or re.match(r"^\s*title\s*:\s*\S", line, re.I):
        return re.sub(r"^\s*(?:[-*]\s*)?(?:\*\*)?", "", line).strip("* ")
    return None


def _make_claim(cid, quote, context, source):
    host, path = find_target(quote)
    if not (host or path):
        host, path = find_target(context)
    target = target_key(host, path)
    label = CLASS_LABELS.get(cid, cid)
    return {"class": cid, "label": label, "host": host, "path": path, "target": target,
            "key": cid + "|" + target.lower(), "severity": _severity(quote, context),
            "title": _clip(label + (" on " + target if target else ""), _TITLE_MAX),
            "quote": _clip(quote, 300), "source": source}


def detect(text, max_claims=MAX_CLAIMS):
    """Finding claims in one assistant reply -> [{class, label, host, path, target, key,
    severity, title, quote, source}] (deduplicated by class + target). Never raises."""
    try:
        return _detect(text, max_claims)
    except Exception:                                            # noqa: BLE001
        return []


def _detect(text, max_claims):
    if not isinstance(text, str) or not text.strip():
        return []
    text = _THINK_RE.sub(" ", text[:MAX_TEXT]).replace("’", "'").replace("\r\n", "\n")
    lines = text.split("\n")
    in_fence = [False] * len(lines)
    fence = False
    for i, ln in enumerate(lines):
        if _FENCE_RE.match(ln):
            fence = not fence
            in_fence[i] = True
            continue
        in_fence[i] = fence
    heads = [i for i, ln in enumerate(lines) if not in_fence[i] and _heading_text(ln) is not None]
    section_of = {}
    for n, h in enumerate(heads):
        end = heads[n + 1] if n + 1 < len(heads) else len(lines)
        for j in range(h + 1, end):
            section_of[j] = h
    found = {}

    def add(claim):
        if claim["key"] not in found:
            found[claim["key"]] = claim

    for n, h in enumerate(heads):
        end = heads[n + 1] if n + 1 < len(heads) else len(lines)
        htext = _heading_text(lines[h]) or ""
        body = "\n".join(lines[h + 1:end])
        if _HEDGED_SECTION_RE.search(htext) or _NEGATION_RE.search(htext):
            continue
        fc = _first_class(htext)
        if not fc:
            continue
        if _hedged(htext, fc[1]):
            continue
        if (_FINDING_PREFIX_RE.match(htext) or _CLAIM_RE.search(htext)
                or _REPORT_FIELD_RE.search(body)):
            add(_make_claim(fc[0], htext, htext + "\n" + body, "heading"))

    for i, ln in enumerate(lines):
        if in_fence[i] or i in heads or not ln.strip():
            continue
        h = section_of.get(i)
        htext = (_heading_text(lines[h]) or "") if h is not None else ""
        if htext and _HEDGED_SECTION_RE.search(htext):
            continue
        is_item = bool(_ITEM_RE.match(ln))
        findings_section = bool(htext) and bool(_FINDINGS_SECTION_RE.search(htext))
        # Where a target may sit when the sentence names none: the line, the next two
        # plain lines of the same block (never another item), the section heading.
        nxt = []
        for j in range(i + 1, min(len(lines), i + 6)):
            if j in heads or len(nxt) >= 2 or (lines[j].strip() and _ITEM_RE.match(lines[j])):
                break
            if lines[j].strip():
                nxt.append(lines[j])
        context = "\n".join([ln] + nxt + [htext])
        if ln.lstrip().startswith("|"):
            if re.match(r"^\s*\|[\s:|-]+\|?\s*$", ln):
                continue                                         # a table's separator row
            sentences = [ln.strip().strip("|").replace("|", " , ")]
        else:
            sentences = _SENT_SPLIT_RE.split(_ITEM_RE.sub("", ln))
        for sent in sentences:
            fc = _first_class(sent)
            if not fc or _NEGATION_RE.search(sent) or _hedged(sent, fc[1]):
                continue
            if _CLAIM_RE.search(sent) or (is_item and findings_section):
                add(_make_claim(fc[0], sent, context, "item" if is_item else "sentence"))
        if len(found) >= max_claims * 2:
            break
    claims = list(found.values())
    with_target = {c["class"] for c in claims if c["target"]}
    claims = [c for c in claims if c["target"] or c["class"] not in with_target]
    return claims[:max_claims]


# --------------------------------------------------------------------------- #
# Observed tool results
# --------------------------------------------------------------------------- #

def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for p in content:
            if isinstance(p, dict):
                t = p.get("text") if isinstance(p.get("text"), str) else p.get("content")
                if isinstance(t, str):
                    out.append(t)
            elif isinstance(p, str):
                out.append(p)
        return "\n".join(out)
    if isinstance(content, dict):
        return _text_of(content.get("text") or content.get("content"))
    return ""


def _head_tail(s, n):
    if len(s) <= n:
        return s
    h = int(n * 0.6)
    return s[:h] + "\n[... %d chars ...]\n" % (len(s) - n) + s[-(n - h):]


def tool_results(messages, upto=None):
    """[{id, name, args, output, index}] for the tool results in `messages[:upto]`, each
    paired with the call that produced it. Bounded. Never raises."""
    out = []
    try:
        msgs = list(messages or ())[: upto if upto is not None else None]
        calls = {}
        for i, m in enumerate(msgs):
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            if role == "assistant":
                for tc in m.get("tool_calls") or ():
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
                    args = fn.get("arguments")
                    if not isinstance(args, str):
                        try:
                            args = json.dumps(args, ensure_ascii=False)
                        except (TypeError, ValueError):
                            args = str(args)
                    calls[str(tc.get("id") or "")] = (str(fn.get("name") or "tool"), args or "")
            elif role == "tool":
                tcid = str(m.get("tool_call_id") or "")
                name, args = calls.get(tcid, (str(m.get("name") or "tool"), ""))
                output = _text_of(m.get("content"))
                out.append({"id": tcid, "name": name[:60], "args": args[:4000],
                            "output": _head_tail(output, MAX_OUTPUT), "index": i})
    except Exception:                                            # noqa: BLE001
        pass
    return out[-MAX_RESULTS:]


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #

_STATUS_LINE_RE = re.compile(r"(?im)^\s*(?:<\s*)?HTTP/\d(?:\.\d)?\s+(\d{3})\b")
_HEADER_LINE_RE = re.compile(
    r"(?im)^\s*(?:<\s*)?(?:content-type|content-length|server|set-cookie|location|date|cache-control"
    r"|access-control-allow-origin|x-powered-by|www-authenticate|x-frame-options)\s*:")
_STATUS_FIELD_RE = re.compile(
    r"(?i)[\"']?(?:status(?:_code|code)?|http_code|response_code)[\"']?\s*[:=]\s*[\"']?(\d{3})\b")
_ERROR_ONLY_RE = re.compile(
    r"(?i)could not resolve host|connection refused|connection timed out|operation timed out"
    r"|name or service not known|no route to host|failed to connect|temporary failure in name resolution"
    r"|getaddrinfo|curl: \(\d+\)|ssl[_ ]error|certificate verify failed")
_SCANNER_LINE_RE = re.compile(
    r"(?im)^\s*\[[\w.:/-]+\]\s+\[(?:http|dns|tcp|ssl|network|headless|code|file)\]\s+"
    r"\[(?:critical|high|medium|low|info|unknown)\]")
# Only a line that REPORTS an interaction counts. The collaborator domain itself (oast.fun,
# interactsh, burpcollaborator) is the agent's own payload: it shows up in curl -v's echoed
# request and in error bodies that reflect the URL, and proves nothing.
_OOB_RE = (r"(?i)received\s+(?:an?\s+)?(?:http|dns|smtp|ldap|ftp|smb)\s+interaction"
           r"|collaborator\s+(?:server\s+received|interaction|received)"
           r"|dns\s+interaction\s+(?:from|received)"
           r"|\"protocol\"\s*:\s*\"(?:dns|https?|smtp|ldap|ftp|smb)\"(?=[^\n]*\"remote-address\"\s*:)")
_FILE_CONTENT_RE = (r"root:[x*]:0:0:|daemon:[x*]:1:1:|\[(?:boot loader|extensions|fonts)\]"
                    r"|for 16-bit app support")

# Rules as data. proof = patterns in an observed OUTPUT that show the class for real;
# fp = (pattern, reason) for what is commonly mistaken for it; need = what proof looks like.
_RULES = {
    "xss": {
        "proof": (r"(?i)\b(?:alert|confirm|prompt|dialog)\b[^\n]{0,40}\b(?:fired|triggered|executed|popped"
                  r"|opened|appeared|was\s+called|detected|shown)\b",
                  r"(?i)\b(?:payload|script|javascript|js)\s+(?:was\s+)?(?:executed|triggered|fired|ran)\b",
                  r"(?m)^\s*\[(?:POC\]\[)?V\]", r"(?i)\bverified\s+(?:xss|payload)\b"),
        "fp": ((r"(?i)<script|on(?:error|load|mouseover|focus)\s*=|javascript:|alert\(|prompt\(|<svg|<img",
                "the payload is only reflected in the response; reflected input without execution is "
                "not XSS"),),
        "need": "the payload executing in a browser (an observed alert/dialog or DOM effect), not only "
                "reflected",
    },
    "sqli": {
        "proof": (r"(?i)identified\s+the\s+following\s+injection\s+point|parameter\s+'[^']+'\s+(?:is\s+"
                  r"vulnerable|appears\s+to\s+be\s+'[^']+'\s+injectable)|back-end\s+dbms\s+is"
                  r"|parameter:\s*\S+\s*\((?:get|post|uri|cookie)\)",
                  r"(?i)you\s+have\s+an\s+error\s+in\s+your\s+sql\s+syntax|mysql_fetch|\bORA-\d{5}\b"
                  r"|SQLSTATE\[|unclosed\s+quotation\s+mark|quoted\s+string\s+not\s+properly\s+terminated"
                  r"|SQLite3?::|pg_query\(\)|syntax\s+error\s+at\s+or\s+near|Microsoft\s+OLE\s+DB\s+Provider"),
        "fp": ((r"(?im)^\s*(?:<\s*)?HTTP/\d(?:\.\d)?\s+500\b|[\"']?status[\"']?\s*[:=]\s*500",
                "an HTTP 500 alone is not SQL injection (no database error, extracted data or timing "
                "difference shown)"),),
        "need": "a database error, extracted data or a measured time delay in the observed response",
    },
    "ssrf": {
        "proof": (_OOB_RE, r"(?i)\bami-id\b|\binstance-id\b|iam/security-credentials|computeMetadata/v1"
                  r"|\"AccessKeyId\"|metadata\.google\.internal"),
        "fp": (),
        "need": "an out-of-band interaction from the target or internal content (e.g. cloud metadata) in "
                "the observed response; a URL parameter alone is not SSRF",
    },
    "rce": {
        "proof": (r"\buid=\d+\([\w.-]+\)\s+gid=\d+", _FILE_CONTENT_RE,
                  r"Microsoft\s+Windows\s+\[Version|nt\s+authority\\", _OOB_RE),
        "fp": (),
        "need": "command output from the target (e.g. id / whoami output or an out-of-band interaction) "
                "in the observed response",
    },
    "path_traversal": {
        "proof": (_FILE_CONTENT_RE, r"<\?php\b"),
        "fp": (),
        "need": "a file's real contents (e.g. root:x:0:0 from /etc/passwd) in the observed response",
    },
    "xxe": {
        "proof": (_FILE_CONTENT_RE, _OOB_RE),
        "fp": (),
        "need": "a local file's contents or an out-of-band interaction from the XML parser",
    },
    "subdomain_takeover": {
        "proof": (r"(?i)NoSuchBucket|The\s+specified\s+bucket\s+does\s+not\s+exist"
                  r"|There\s+isn't\s+a\s+GitHub\s+Pages\s+site\s+here|no-such-app|No\s+such\s+app"
                  r"|Fastly\s+error:\s+unknown\s+domain|Domain\s+(?:is\s+)?not\s+configured"
                  r"|The\s+thing\s+you\s+were\s+looking\s+for\s+is\s+no\s+longer\s+here"
                  r"|Sorry,\s+this\s+shop\s+is\s+currently\s+unavailable|project\s+not\s+found",
                  r"(?i)\[vulnerable\]|\bvulnerable\b[^\n]{0,30}\btake-?over\b"),
        "fp": ((r"(?i)\bCNAME\b|canonical\s+name",
                "a CNAME to a third-party service alone is not a takeover; the service must show an "
                "unclaimed resource that can be claimed"),),
        "need": "the third-party service's unclaimed-resource page for the host (e.g. NoSuchBucket) in "
                "the observed response",
    },
    "info_disclosure": {
        "proof": (r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bAKIA[0-9A-Z]{16}\b"
                  r"|repositoryformatversion|ref:\s+refs/heads/"
                  r"|<title>Index of /|(?:DB_PASSWORD|SECRET_KEY|API_KEY|AWS_SECRET_ACCESS_KEY)\s*="
                  r"|[\"'](?:password|secret|api_?key|access_?token)[\"']\s*:\s*[\"'][^\"']{6,}",),
        # A stack trace proves only inside an observed HTTP response (after a status line):
        # anywhere else it may be the agent's own script crashing (see _evidence).
        "trace": (r"Traceback \(most recent call last\)|\bat\s+[\w.$]+\([\w]+\.java:\d+\)",),
        "fp": ((r"(?im)^\s*(?:<\s*)?(?:server|x-powered-by|x-aspnet(?:mvc)?-version)\s*:[^\n]*\d"
                r"|\bversion\b\s*[:=]?\s*v?\d+\.\d+",
                "a version banner alone is not a vulnerability"),),
        "need": "the sensitive data itself (a secret, credential, personal data or internal file) in the "
                "observed response",
    },
    "clickjacking": {
        "proof": (),
        "fp": ((r".", "a missing X-Frame-Options / frame-ancestors header alone is usually not reportable; "
                      "show a sensitive one-click action working inside a frame"),),
        "need": "a sensitive one-click action that works inside a frame on another origin",
    },
    "csrf": {
        "proof": (),
        "fp": (),
        "need": "an observed state-changing request accepted from another origin without a valid token "
                "(request + response showing the change)",
    },
}
_GENERIC_NEED = "an observed request and the response that shows the issue"
_ACCESS_NEED = ("two observed requests (one per account) showing another account's data returned "
                "or changed")


def _rule(cid):
    return _RULES.get(cid) or {}


def _pattern_line(rx, text):
    m = re.search(rx, text)
    if not m:
        return None
    s = text.rfind("\n", 0, m.start()) + 1
    e = text.find("\n", m.end())
    return text[s:e if e >= 0 else len(text)]


def _unquoted(text):
    """`text` URL-decoded (twice at most), lower-cased."""
    t = text or ""
    for _ in range(2):
        d = unquote(t)
        if d == t:
            break
        t = d
    return t.casefold()


def _proof_line(rx, text, args):
    """The line of the first match of proof pattern `rx` in `text` whose matched
    text is NOT in the call's own arguments `args`: an error page that echoes the
    agent's payload ('url not allowed: http://metadata.google.internal/...') shows
    what the agent SENT, not what the target did. None when every match is such
    an echo."""
    sent = _unquoted(args)
    for m in re.finditer(rx, text):
        hit = m.group(0).strip().casefold()
        if hit and sent and hit in sent:
            continue
        s = text.rfind("\n", 0, m.start()) + 1
        e = text.find("\n", m.end())
        return text[s:e if e >= 0 else len(text)]
    return None


def _status(output):
    codes = _STATUS_LINE_RE.findall(output)
    if codes:
        return int(codes[-1])
    m = _STATUS_FIELD_RE.search(output)
    return int(m.group(1)) if m else 0


def _status_at(output, pos):
    """The status code of the response a line at `pos` belongs to: the nearest status line
    before it, else a status field, else 0 (unknown)."""
    code = 0
    for m in _STATUS_LINE_RE.finditer(output):
        if m.start() > pos:
            break
        code = int(m.group(1))
    if not code:
        m = _STATUS_FIELD_RE.search(output)
        code = int(m.group(1)) if m else 0
    return code


_REQUEST_ECHO_RE = re.compile(r"[ \t]*>(?:[ \t]|\r?$)")


def _response_view(output):
    """The output without the request lines curl -v echoes ("> GET /x HTTP/1.1",
    "> Host: ..."): what the agent SENT is never proof of what the target did."""
    if ">" not in output:
        return output
    return "\n".join(ln for ln in output.split("\n") if not _REQUEST_ECHO_RE.match(ln))


def _after_status(output):
    """The text after the first observed HTTP status line ("" when there is none)."""
    m = _STATUS_LINE_RE.search(output)
    return output[m.end():] if m else ""


def _injected_regs(args, own_reg):
    """Registrable domains the call's own arguments carry (URL-decoded), other than the
    target's site: where an open-redirect test pointed the redirect."""
    text = args or ""
    for _ in range(2):
        dec = unquote(text)
        if dec == text:
            break
        text = dec
    hosts = [m.group(1) for m in re.finditer(
        r"(?i)//(?:[^\s/?#@\"'`<>\\]*@)?([^\s/?#:@\"'`<>\\]+)", text)]
    hosts += [m.group(0) for m in _HOST_RE.finditer(text)]
    regs = set()
    for h in hosts:
        h = _clean_host(h)
        if _valid_host(h):
            reg = _registrable(h)
            if reg and reg != own_reg:
                regs.add(reg)
    return regs


def _sent_origins(args):
    """The Origin header values the call sent (normalised), e.g. curl -H 'Origin: x'."""
    out = set()
    for m in re.finditer(r"(?i)(?<![\w-])origin(?:\\?[\"'])?\s*[:=]\s*(?:\\?[\"'])?([^\s\"',;}\\]+)",
                         args or ""):
        out.add(m.group(1).rstrip("/").lower())
    return out


def _body(output):
    """The body after the LAST status line's header block, else the whole output."""
    ms = list(_STATUS_LINE_RE.finditer(output))
    if not ms:
        return output
    rest = output[ms[-1].end():]
    parts = re.split(r"\r?\n\s*\r?\n", rest, maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


def _shows_response(cid, output):
    if not output or not output.strip():
        return False
    if (_STATUS_LINE_RE.search(output) or _HEADER_LINE_RE.search(output)
            or _STATUS_FIELD_RE.search(output) or _SCANNER_LINE_RE.search(output)):
        return True
    if _ERROR_ONLY_RE.search(output) and len(output.strip()) < 400:
        return False
    view = _response_view(output)
    for rx in _rule(cid).get("proof", ()):
        if re.search(rx, view):
            return True
    return len(output.strip()) >= 40 and not _ERROR_ONLY_RE.search(output[:200])


def _names_target(claim, res):
    host, path = claim.get("host") or "", claim.get("path") or ""
    if not (host or path):
        return False
    blob = (res.get("args") or "") + "\n" + (res.get("output") or "")
    low = blob.lower()
    if host and host not in low and ("www." + host) not in low:
        return False
    if path:
        segs = [s for s in path.split("/") if s]
        rx = "/" + "/".join("[^/?#\\s\"'<>]+" if s == "{id}" else re.escape(s) for s in segs)
        if not re.search(rx + r"(?:[/?#\s\"'<>\]),;]|$)", blob, re.I):
            return False
    return True


def _special(cid, claim, res):
    """Class checks that need more than a pattern -> (verdict, reason, line) or None."""
    out, args = res.get("output") or "", res.get("args") or ""
    if cid == "open_redirect":
        locs = [(m.start(), m.group(1))
                for m in re.finditer(r"(?im)^\s*(?:<\s*)?location\s*:\s*(\S+)", out)]
        if not locs:
            return None
        base = _registrable(claim.get("host") or "")
        injected, review = None, None
        for pos, loc in locs:
            full = "http:" + loc if loc.startswith("//") else loc
            if not re.match(r"(?i)https?://", full):
                continue
            try:
                h = _clean_host(urlsplit(full).netloc.rsplit("@", 1)[-1])
            except ValueError:
                continue
            if not (h and base and _registrable(h) != base):
                continue
            # Proof = a 3xx sending the browser to the host the REQUEST injected; a
            # redirect to the site's own login provider or a 200 that carries a
            # Location header is not an open redirect.
            status = _status_at(out, pos)
            if injected is None:
                injected = _injected_regs(args, base)
            if 300 <= status < 400 and _registrable(h) in injected:
                return ("proven", "", "Location: " + full)
            if review is None:
                if not 300 <= status < 400:
                    why = ("the response carrying the external Location is %s, not a redirect"
                           % ("HTTP %d" % status if status else "shown with no status line"))
                else:
                    why = ("the redirect goes to %s, which the request did not inject (it may be the "
                           "site's own login or partner redirect)" % h)
                review = ("needs_review", why, "Location: " + full)
        if review is not None:
            return review
        return ("false_positive", "the observed redirect stays on the same site; that is not an open "
                                  "redirect", "Location: " + locs[-1][1])
    if cid == "cors":
        acao = re.findall(r"(?im)^\s*(?:<\s*)?access-control-allow-origin\s*:\s*(\S+)", out)
        if not acao:
            return None
        cred = re.search(r"(?im)^\s*(?:<\s*)?access-control-allow-credentials\s*:\s*true\b", out)
        origin = acao[-1].strip()
        if origin == "*":
            return ("false_positive", "Access-Control-Allow-Origin: * without credentials is not "
                                      "exploitable", "Access-Control-Allow-Origin: *")
        oh = _clean_host(urlsplit(origin).netloc) if "://" in origin else ""
        if oh and _registrable(oh) == _registrable(claim.get("host") or ""):
            return ("false_positive", "the allowed origin is the site itself",
                    "Access-Control-Allow-Origin: " + origin)
        if cred:
            # Proof = the server allowing the Origin the REQUEST sent (or "null"); a fixed
            # trusted partner origin is not something an attacker controls.
            if origin.lower() == "null" or origin.rstrip("/").lower() in _sent_origins(args):
                return ("proven", "", "Access-Control-Allow-Origin: %s + credentials" % origin)
            return ("needs_review", "the allowed origin %s is not the Origin the request sent; a fixed "
                                    "trusted origin is not exploitable unless an attacker controls it"
                    % origin, "Access-Control-Allow-Origin: %s + credentials" % origin)
        return ("false_positive", "a reflected origin without Access-Control-Allow-Credentials: true is "
                                  "usually not exploitable", "Access-Control-Allow-Origin: " + origin)
    if cid == "ssti":
        if re.search(r"\{\{\s*7\s*\*\s*7\s*\}\}|\$\{\s*7\s*\*\s*7\s*\}|<%=\s*7\s*\*\s*7\s*%>|#\{\s*7\s*\*\s*7\s*\}",
                     out):
            return ("false_positive", "the template expression is only reflected, not evaluated", "")
        if re.search(r"7\s*\*\s*['\"]?7", args):
            line = _pattern_line(r"\b(?:49|7777777)\b", out)
            if line is not None:
                return ("proven", "", line)
        return None
    if cid in _ACCESS_CLASSES:
        status = _status(out)
        if status in (401, 403) or 300 <= status < 400:
            return ("false_positive", "the observed response is %d: access was not granted" % status,
                    "HTTP %d" % status)
        body = _body(out).strip()
        has_data = bool(re.search(r"[\"'][\w-]{2,}[\"']\s*:\s*\S", body)) or len(body) > 80
        if 200 <= status < 300 and not has_data:
            return ("false_positive", "a 200 alone is not access to another account's data",
                    "HTTP %d" % status)
        if has_data and (200 <= status < 300 or status == 0):
            return ("needs_review", "a response with data is observed; it still has to be shown that the "
                                    "data belongs to ANOTHER account", "")
        return None
    return None


def evidence(claim, results):
    """Deterministic first pass over the OBSERVED tool results for one claim ->
    {"proof": "observed"|"none", "status": proven|unverified|false_positive|needs_review,
     "pointer": str, "reasons": [str]}. Proof needs an observed result that names the
    claim's target AND shows the response proving the class. Never raises."""
    try:
        return _evidence(claim or {}, list(results or ()))
    except Exception:                                            # noqa: BLE001
        return {"proof": "none", "status": "unverified", "pointer": "",
                "reasons": ["the hub could not read the observed output"]}


def _pointer(res, line):
    head = "%s call %s" % (res.get("name") or "tool", (res.get("id") or "?")[:12])
    line = redact(_clip(line or "", 140))
    return _clip(head + (": " + line if line else ""), _POINTER_MAX)


def _evidence(claim, results):
    cid = claim.get("class") or ""
    rule = _rule(cid)
    need = rule.get("need") or (_ACCESS_NEED if cid in _ACCESS_CLASSES else _GENERIC_NEED)
    target = claim.get("target") or ""
    if not target:
        return {"proof": "none", "status": "unverified", "pointer": "",
                "reasons": ["the finding names no target (URL, host or endpoint); missing: " + need]}
    named = [r for r in results if _names_target(claim, r)]
    shown = [r for r in named if _shows_response(cid, r.get("output") or "")]
    if not shown:
        why = ("no observed request + response for %s" % target if not named else
               "the observed calls to %s show no response (error or empty output)" % target)
        return {"proof": "none", "status": "unverified", "pointer": "",
                "reasons": [why + "; missing: " + need]}
    review, fp, scanner = None, None, None
    for r in reversed(shown):
        out = r.get("output") or ""
        sp = _special(cid, claim, r)
        if sp and sp[0] == "proven":
            return {"proof": "observed", "status": "proven", "pointer": _pointer(r, sp[2]), "reasons": []}
        view = _response_view(out)
        for rx in rule.get("proof", ()):
            line = _proof_line(rx, view, r.get("args") or "")
            if line is not None:
                return {"proof": "observed", "status": "proven", "pointer": _pointer(r, line),
                        "reasons": []}
        after = _after_status(view)
        for rx in rule.get("trace", ()):
            line = _pattern_line(rx, after) if after else None
            if line is not None:
                return {"proof": "observed", "status": "proven", "pointer": _pointer(r, line),
                        "reasons": []}
            line = _pattern_line(rx, view) if review is None else None
            if line is not None:
                review = (r, "a stack trace was observed, but no HTTP status line shows it is %s's "
                             "response (it may be the tool's own error)" % target, line)
        if sp and sp[0] == "needs_review" and review is None:
            review = (r, sp[1], sp[2])
        elif sp and sp[0] == "false_positive" and fp is None:
            fp = (r, sp[1], sp[2])
        if fp is None and not sp:
            for rx, reason in rule.get("fp", ()):
                line = _pattern_line(rx, out)
                if line is not None:
                    fp = (r, reason, line)
                    break
        if scanner is None:
            line = _pattern_line(_SCANNER_LINE_RE.pattern, out)
            if line is not None:
                scanner = (r, line)
    if review is not None:
        return {"proof": "observed", "status": "needs_review", "pointer": _pointer(review[0], review[2]),
                "reasons": [review[1] + "; missing: " + need]}
    if scanner is not None:
        return {"proof": "observed", "status": "needs_review", "pointer": _pointer(*scanner),
                "reasons": ["a scanner template matched; a template match still has to be confirmed by "
                            "hand; missing: " + need]}
    if fp is not None:
        return {"proof": "none", "status": "false_positive", "pointer": _pointer(fp[0], fp[2]),
                "reasons": [fp[1]]}
    last = shown[-1]
    return {"proof": "none", "status": "unverified", "pointer": _pointer(last, ""),
            "reasons": ["the observed response for %s does not show it; missing: %s" % (target, need)]}


def relevant_excerpt(claim, results, limit=3000):
    """The observed calls that name the claim's target (most recent last), head + tail,
    for a second model to read. Redacted by `redact`; the caller may redact again."""
    try:
        named = [r for r in (results or ()) if _names_target(claim or {}, r)][-3:]
        if not named:
            return ""
        share = max(300, limit // len(named))
        parts = []
        for r in named:
            block = "$ %s %s\n%s" % (r.get("name") or "tool", _clip(r.get("args") or "", 300),
                                    _head_tail(r.get("output") or "", share))
            parts.append(redact(block))
        return _head_tail("\n---\n".join(parts), limit)
    except Exception:                                            # noqa: BLE001
        return ""


# --------------------------------------------------------------------------- #
# Redaction (pointers and excerpts never carry credentials)
# --------------------------------------------------------------------------- #

_REDACT_RES = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
     "[redacted private key]"),
    (re.compile(r"(?im)^(\s*(?:<\s*|>\s*)?(?:authorization|proxy-authorization|cookie|set-cookie"
                r"|x-api-key|api-key|x-auth-token)\s*:\s*).+$"), r"\1[redacted]"),
    # the same headers inside a command line (curl -H 'Cookie: ...'), and curl's cookie flags
    (re.compile(r"(?i)\b((?:proxy-)?authorization|cookie|x-api-key|api-key|x-auth-token)\s*:\s*[^'\"\n]+"),
     r"\1: [redacted]"),
    (re.compile(r"(?i)(\s(?:-b|--cookie)\s+)(['\"]?)[^\s'\"]+\2"), r"\1[redacted]"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"), "[redacted jwt]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[redacted key]"),
    (re.compile(r"\b(?:sk|pk|rk)[-_](?:live|test|proj)?[-_]?[A-Za-z0-9]{16,}\b"), "[redacted key]"),
    (re.compile(r"\b(?:ghp|gho|ghs|ghu|github_pat|glpat|xox[abprs])[-_][A-Za-z0-9_-]{10,}"), "[redacted key]"),
    (re.compile(r"(?i)([\"']?(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret"
                r"|private[_-]?key|session(?:id)?)[\"']?\s*[:=]\s*)[\"']?[^\s\"',;&]{3,}[\"']?"), r"\1[redacted]"),
)


def redact(text):
    s = str(text or "")
    for rx, rep in _REDACT_RES:
        s = rx.sub(rep, s)
    return s


# --------------------------------------------------------------------------- #
# The conversation's last finished reply
# --------------------------------------------------------------------------- #

def last_finished_reply(messages):
    """(index, text) of the assistant reply that came right before the trailing user
    instruction -- a FINISHED exchange -- or (None, ""). Never raises."""
    try:
        msgs = list(messages or ())
        i = len(msgs) - 1
        while i >= 0 and isinstance(msgs[i], dict) and msgs[i].get("role") in ("system", "developer"):
            i -= 1
        if i < 0 or not isinstance(msgs[i], dict) or msgs[i].get("role") != "user":
            return None, ""
        while i >= 0 and isinstance(msgs[i], dict) and msgs[i].get("role") in ("user", "system", "developer"):
            i -= 1
        if i < 0 or not isinstance(msgs[i], dict) or msgs[i].get("role") != "assistant":
            return None, ""
        text = _text_of(msgs[i].get("content"))
        return (i, text) if text.strip() else (None, "")
    except Exception:                                            # noqa: BLE001
        return None, ""


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #

def folder_key(project_dir):
    """sha256(normcase(realpath(folder)))[:16] -- the ledger's file name."""
    try:
        real = os.path.normcase(os.path.realpath(os.path.abspath(str(project_dir))))
    except (OSError, ValueError, TypeError):
        real = str(project_dir)
    return hashlib.sha256(real.encode("utf-8", "replace")).hexdigest()[:16]


def finding_id(key):
    return "f_" + hashlib.sha256(str(key).encode("utf-8", "replace")).hexdigest()[:10]


def _merge_reasons(old, new):
    out = []
    for r in list(new or ()) + list(old or ()):
        r = _clip(r, _REASON_MAX)
        if r and r not in out:
            out.append(r)
    return out[:3]


class Ledger:
    """One JSON file per project folder under `root`. In memory after the first read;
    writes go through (temp file + os.replace). Bounded to MAX_FINDINGS rows."""

    def __init__(self, root, clock=time.time):
        self.root = str(root)
        self._clock = clock
        self._lock = threading.RLock()
        self._cache = {}
        self._conv_index = None

    # ---- storage --------------------------------------------------------- #
    def path_for(self, project_dir):
        return os.path.join(self.root, folder_key(project_dir) + ".json")

    def _load(self, fkey):
        if fkey in self._cache:
            return self._cache[fkey]
        rows = []
        try:
            with open(os.path.join(self.root, fkey + ".json"), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("findings"), list):
                rows = [r for r in data["findings"] if isinstance(r, dict) and r.get("id")]
        except (OSError, ValueError):
            rows = []
        self._cache[fkey] = rows
        return rows

    def _save(self, fkey, project_dir, rows):
        os.makedirs(self.root, exist_ok=True)
        rows.sort(key=lambda r: -float(r.get("last_seen") or 0))
        del rows[MAX_FINDINGS:]
        data = {"v": 1, "project_dir": str(project_dir), "findings": rows}
        fd, tmp = tempfile.mkstemp(prefix="." + fkey + ".", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            for attempt in range(5):
                try:
                    os.replace(tmp, os.path.join(self.root, fkey + ".json"))
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        self._cache[fkey] = rows
        if self._conv_index is not None:
            for r in rows:
                if r.get("conversation"):
                    self._conv_index.setdefault(r["conversation"], set()).add(fkey)

    def _index(self):
        if self._conv_index is None:
            idx = {}
            try:
                names = os.listdir(self.root)
            except OSError:
                names = []
            for name in names:
                if not re.match(r"^[0-9a-f]{16}\.json$", name):
                    continue
                fkey = name[:-5]
                for r in self._load(fkey):
                    if r.get("conversation"):
                        idx.setdefault(r["conversation"], set()).add(fkey)
            self._conv_index = idx
        return self._conv_index

    # ---- reads ----------------------------------------------------------- #
    def has_conversation(self, conv):
        """Cheap: does any ledger hold a finding of this conversation?"""
        try:
            with self._lock:
                return bool(conv) and bool(self._index().get(conv))
        except Exception:                                        # noqa: BLE001
            return False

    def rows(self, project_dir):
        try:
            with self._lock:
                return [dict(r) for r in self._load(folder_key(project_dir))]
        except Exception:                                        # noqa: BLE001
            return []

    def note_for(self, project_dir, conv):
        try:
            if not conv:
                return ""
            rows = [r for r in self.rows(project_dir) if r.get("conversation") == conv]
            return note_text(rows, now=self._clock())
        except Exception:                                        # noqa: BLE001
            return ""

    # ---- writes ---------------------------------------------------------- #
    def upsert(self, project_dir, items, conv):
        """items = [(claim, ev, producer_family)] -> [(row id, status, wants_verdict)]. A
        finding already in the ledger (same class + target) is updated, never duplicated.
        wants_verdict: not proven and no second-model verdict since its status last changed."""
        now = self._clock()
        out = []
        with self._lock:
            fkey = folder_key(project_dir)
            rows = self._load(fkey)
            by_id = {r["id"]: r for r in rows}
            for claim, ev, producer in items:
                fid = finding_id(claim["key"])
                st = ev.get("status") or "unverified"
                row = by_id.get(fid)
                if row is None:
                    row = {"id": fid, "title": claim.get("title") or "", "class": claim.get("class") or "",
                           "target": claim.get("target") or "", "severity": claim.get("severity") or "",
                           "status": st, "status_source": "observed",
                           "reasons": _merge_reasons([], ev.get("reasons")),
                           "evidence": ev.get("pointer") or "", "conversation": conv or "",
                           "first_seen": now, "last_seen": now, "seen": 1, "verifier": "",
                           "verified_at": 0, "producer": producer or ""}
                    rows.append(row)
                    by_id[fid] = row
                    out.append((fid, st, True))
                    continue
                before = row.get("status")
                row["last_seen"] = now
                row["seen"] = int(row.get("seen") or 0) + 1
                if conv:
                    row["conversation"] = conv
                if claim.get("severity") and not row.get("severity"):
                    row["severity"] = claim["severity"]
                if producer:
                    row["producer"] = producer
                if before == "proven":
                    pass                                         # once proven, stays proven
                elif (st in ("proven", "false_positive", "needs_review")
                        or row.get("status_source") != "verifier"):
                    # A verifier's call stands until the OBSERVED state says something new.
                    row["status"] = st
                    row["status_source"] = "observed"
                    row["reasons"] = (_merge_reasons([], ev.get("reasons")) if st != before
                                      else _merge_reasons(row.get("reasons"), ev.get("reasons")))
                    if ev.get("pointer"):
                        row["evidence"] = ev["pointer"]
                    if st != before:
                        row["verifier"], row["verified_at"] = "", 0
                out.append((fid, row["status"], row["status"] != before))
            for n, (fid, _st, _changed) in enumerate(out):
                row = by_id[fid]
                out[n] = (fid, row["status"], row["status"] != "proven" and not row.get("verifier"))
            self._save(fkey, project_dir, rows)
        return out

    def apply_verdict(self, project_dir, fid, verdict, family=""):
        """Fold a second model's verdict into one row. None (error, timeout, unreadable)
        -> needs_review (fail-open). A verdict can never make a finding proven: only
        observed output does that. Returns the new status or None."""
        now = self._clock()
        with self._lock:
            fkey = folder_key(project_dir)
            rows = self._load(fkey)
            row = next((r for r in rows if r.get("id") == fid), None)
            if row is None or row.get("status") == "proven":
                return row.get("status") if row else None
            problems = []
            if isinstance(verdict, dict):
                problems = [_clip(p, _REASON_MAX) for p in (verdict.get("problems") or []) if str(p).strip()][:3]
            if not isinstance(verdict, dict):
                row["status"] = "needs_review"
                row["reasons"] = _merge_reasons(row.get("reasons"), [
                    "the second-model check could not be completed; a person should review it"])
            elif verdict.get("ok"):
                row["status"] = "needs_review"
                row["reasons"] = _merge_reasons(row.get("reasons"), [
                    "a second model finds it plausible, but no observed request + response proves it yet"])
            elif str(verdict.get("severity") or "").lower() == "high":
                row["status"] = "false_positive"
                row["reasons"] = _merge_reasons(row.get("reasons"), problems or [
                    "a second model judged it not a real vulnerability"])
            else:
                if row.get("status") != "false_positive":
                    row["status"] = "unverified"
                row["reasons"] = _merge_reasons(row.get("reasons"), problems)
            row["status_source"] = "verifier"
            row["verifier"] = _clip(family or "", 40)
            row["verified_at"] = now
            self._save(fkey, project_dir, rows)
            return row["status"]


# --------------------------------------------------------------------------- #
# The note
# --------------------------------------------------------------------------- #

NOTE_HEAD = ("HUB FINDINGS CHECK -- the hub compared the security findings reported earlier in this "
             "conversation with the tool output it actually observed:")
NOTE_TAIL = ("Do not present the unconfirmed ones as confirmed. Before you report or fix any of them "
             "as confirmed, show the observed request and the response that proves it.")
_NOTE_LABEL = {"false_positive": "LIKELY FALSE POSITIVE", "needs_review": "NEEDS REVIEW",
               "unverified": "NOT CONFIRMED"}
_NOTE_ORDER = {"false_positive": 0, "needs_review": 1, "unverified": 2}


def note_text(rows, now=None, max_chars=NOTE_MAX_CHARS):
    """The deterministic note for a conversation's findings, or "" when none is open.
    Open = unverified / false_positive / needs_review; proven ones are listed briefly."""
    try:
        now = time.time() if now is None else now
        live = [r for r in rows or () if isinstance(r, dict)
                and now - float(r.get("last_seen") or 0) <= NOTE_MAX_AGE]
        open_rows = sorted((r for r in live if r.get("status") in _NOTE_ORDER),
                           key=lambda r: (_NOTE_ORDER[r["status"]], -float(r.get("last_seen") or 0),
                                          r.get("id") or ""))
        if not open_rows:
            return ""
        proven = sorted((r for r in live if r.get("status") == "proven"),
                        key=lambda r: (-float(r.get("last_seen") or 0), r.get("id") or ""))
        budget = max_chars - len(NOTE_HEAD) - len(NOTE_TAIL) - 2
        lines = []
        for n, r in enumerate(open_rows):
            reason = _clip((r.get("reasons") or [""])[0], 150)
            line = "- %s: %s%s" % (_NOTE_LABEL[r["status"]], _clip(r.get("title") or r.get("class"), 70),
                                   (" -- " + reason) if reason else "")
            more = len(open_rows) - n - 1
            tail = "\n- (+%d more)" % more if more else ""
            if sum(len(x) + 1 for x in lines) + len(line) + 1 + len(tail) > budget:
                if not lines:
                    line = _clip(line, max(40, budget - len(tail) - 1))
                else:
                    lines.append("- (+%d more)" % (len(open_rows) - n))
                    break
            lines.append(line)
        if proven:
            p = "- Proven: " + ", ".join(_clip(r.get("title") or r.get("class"), 50) for r in proven[:3])
            if len(proven) > 3:
                p += " (+%d more)" % (len(proven) - 3)
            if sum(len(x) + 1 for x in lines) + len(p) + 1 <= budget:
                lines.append(p)
        text = NOTE_HEAD + "\n" + "\n".join(lines) + "\n" + NOTE_TAIL
        return text[:max_chars]
    except Exception:                                            # noqa: BLE001
        return ""
