"""Calvoun Free LLM Hub -- one Multi helper per TARGET (the pure half).

WHAT IT IS FOR
--------------
The owner tests several targets at once from one project folder: several
authorized programs, or several of their own apps / sites. One helper working
"all of it" mixes scopes and forgets what it already did on which target. So
when the folder's scope card (scopecard.py) knows two or more targets and the
goal is an authorized-testing task, a Multi run plans ONE phase per target, and
each phase's worker is told only ITS target: its scope, its open findings, its
open board tasks and the last state of its own notes file. A later run in the
same folder reads the same notes file, so each target continues from where it
stopped.

WHAT IS PURE HERE
-----------------
Stdlib + scopecard, no import of app/config/swarm_windows, never raises (every
public function fails open to "nothing": no targets, no block, the plan as given).
app.py supplies the card, the findings ledger rows, the board tasks and the notes
text (section "MULTI PER TARGET"); swarm_windows calls the two hooks app registers.

  plan_targets(card, goal) -> [name]             the targets this goal works on (>= 2 or [])
  notes_map(names) -> {name: "notes/<slug>.md"}  one notes file per target, never shared
  plan_ask(names, final_title) -> str            the planner's extra instruction
  fit_plan(phases, names, goal, final_title)     one phase per target + ONE final phase
  annotate(phases, names)                        tag given phases with their target
  target_hosts(card, name) -> [pattern]          the hosts a finding must name to be this target's
  block(name, scope_text, rows, hosts, tasks, notes_text, notes_file) -> str  (<= 1500 chars)
  overview(names) -> str                         what a non-target phase is told
"""
import hashlib
import re

import scopecard

MAX_TARGETS = 8                 # phases: up to 8 targets + 1 setup + the final one
BLOCK_MAX_CHARS = 1500
SCOPE_CHARS = 560
OVERVIEW_MAX_CHARS = 600
NOTES_DIR = "notes"
SUMMARY_FILE = NOTES_DIR + "/summary.md"
NOTES_MAX_BYTES = 64 * 1024
GOAL_IN_TASK_CHARS = 800

_OPEN_TASK_STATUSES = ("doing", "todo", "blocked", "failed")
_FINDING_ORDER = {"false_positive": 0, "needs_review": 1, "unverified": 2, "proven": 3}
_FINDING_LABEL = {"false_positive": "LIKELY FALSE POSITIVE", "needs_review": "NEEDS REVIEW",
                  "unverified": "NOT CONFIRMED", "proven": "PROVEN"}


def _clip(text, n):
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[:max(0, n - 3)].rstrip() + "..."


def _norm(name):
    return " ".join(str(name or "").split())


def _word_in(name, text):
    """`name` appears in `text` as a whole word (case-insensitive)."""
    n = _norm(name).lower()
    if not n:
        return False
    return re.search(r"(?<![\w.-])%s(?![\w-])" % re.escape(n), str(text or "").lower()) is not None


# --------------------------------------------------------------------------- #
# Which targets
# --------------------------------------------------------------------------- #

def _own_in(card, name):
    """The in-scope entries tagged with `name` (not the shared ones)."""
    key = _norm(name).lower()
    return [e for e in (card or {}).get("in") or ()
            if isinstance(e, dict) and str(e.get("tg") or "").lower() == key]


def _mentions(goal, card, name):
    """The goal names this target: its name as a word, or a host its OWN
    in-scope list covers."""
    if _word_in(name, goal):
        return True
    own = _own_in(card, name)
    if not own:
        return False
    sub = {"in": own, "out": []}
    return any(scopecard.in_scope(h, sub) is True for h in scopecard.hosts_in_text(goal))


def plan_targets(card, goal, max_targets=MAX_TARGETS):
    """The targets a goal works on, or [] (the run is planned as before).

    A card with two or more NAMED targets: the ones the goal names when it names
    two or more, none when it names exactly one (that is one target's work), all
    of them when it names none. A card with no named target: the hosts the goal
    names that the card lists in scope, when there are two or more."""
    try:
        card = scopecard.normalize(card)
        goal = str(goal or "")
        named = [t["name"] for t in scopecard.targets(card) if t["name"]]
        if len(named) >= 2:
            said = [n for n in named if _mentions(goal, card, n)]
            if len(said) == 1:
                return []
            chosen = said if len(said) >= 2 else named
            return chosen[:max_targets]
        if named:
            return []
        hosts = []
        for h in scopecard.hosts_in_text(goal):
            if scopecard.in_scope(h, card) is True and h not in hosts:
                hosts.append(h)
        return hosts[:max_targets] if len(hosts) >= 2 else []
    except Exception:                                            # noqa: BLE001
        return []


def slug(name):
    s = re.sub(r"[^a-z0-9.-]+", "-", _norm(name).lower()).strip("-.")
    s = re.sub(r"-{2,}", "-", s)[:48].strip("-.")
    return s or "target-" + hashlib.sha1(_norm(name).encode("utf-8", "replace")).hexdigest()[:8]


def notes_map(names):
    """{name: "notes/<slug>.md"}: one file per target, never shared (two names
    with the same slug get -2, -3... in the order given). Deterministic."""
    out, used = {}, set()
    for n in names or ():
        base = slug(n)
        s, k = base, 2
        while s in used or s == "summary":
            s, k = "%s-%d" % (base, k), k + 1
        used.add(s)
        out[n] = "%s/%s.md" % (NOTES_DIR, s)
    return out


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #

def plan_ask(names, final_title):
    """The planner's extra instruction for a per-target plan ("" without 2+ names)."""
    names = list(names or ())
    if len(names) < 2:
        return ""
    nm = notes_map(names)
    listing = ", ".join('"%s" (notes file %s)' % (n, nm[n]) for n in names)
    return ("\nPER-TARGET PLAN: this is authorized testing of %d separate targets, each with its "
            "own scope: %s.\nPlan exactly ONE phase per target, in that order: \"title\" = the "
            "target name exactly, \"target\" = the target name, \"parallel\": true, \"needs\": [], "
            "\"files\": [that target's notes file only] -- no file shared between targets. Its "
            "\"task\" works ONLY on that target's in-scope assets (its helper is given that target's "
            "scope, open findings, board tasks and notes from earlier runs, and continues from "
            "them). Then ONE last phase titled \"%s\" that needs every target phase and merges their "
            "results into %s (per target: proven findings, unverified leads, what remains). No "
            "other phases.\n" % (len(names), listing, final_title, SUMMARY_FILE))


def match_target(phase, names):
    """The target a planned phase is about: its "target" field, else its title
    (exactly a name, or naming exactly one), else None."""
    try:
        lows = {_norm(n).lower(): n for n in names}
        t = _norm(phase.get("target")).lower()
        if t in lows:
            return lows[t]
        title = _norm(phase.get("title")).lower()
        if title in lows:
            return lows[title]
        hits = [n for n in names if _word_in(n, title)]
        return hits[0] if len(hits) == 1 else None
    except Exception:                                            # noqa: BLE001
        return None


def _done_when(rel):
    return ("%s is up to date (done / remaining / leads) and every finding in it is marked "
            "proven (observed request + response) or unverified." % rel)


def _target_phase(name, goal, rel):
    return {"title": name, "target": name,
            "task": ("Work ONLY on the target \"%s\" for this goal:\n%s\n\nStay inside %s's scope "
                     "and rules (given below). Continue from %s if it exists; do not redo what it "
                     "lists as done." % (name, _clip(goal, GOAL_IN_TASK_CHARS), name, rel)),
            "done_when": _done_when(rel), "needs": [], "mode": None,
            "files": [rel], "parallel": True}


def _final_phase(names, nm, final_title, needs):
    listing = ", ".join("%s (%s)" % (n, nm[n]) for n in names)
    return {"title": final_title,
            "task": ("Every target of this run was worked by its own helper: %s. You are the last "
                     "agent. Merge their results into %s: one section per target with (1) its "
                     "proven findings, each with the observed request + response that proves it, "
                     "(2) its unverified leads, labelled unverified, (3) what remains to do. Read "
                     "each target's notes file and the summaries below. Keep every target inside "
                     "its own scope; run no new test unless a proof is missing and that target's "
                     "scope allows it. Change nothing else." % (listing, SUMMARY_FILE)),
            "done_when": "%s has one section per target: proven, unverified, remaining." % SUMMARY_FILE,
            "needs": list(needs), "mode": None, "files": [SUMMARY_FILE]}


def fit_plan(phases, names, goal, final_title, max_phases=10):
    """`phases` shaped as ONE phase per target (title = the name, "target", its
    own notes file, parallel, needing no other target) plus ONE final phase
    `final_title` that merges them. A plan that already has exactly one phase per
    target keeps its tasks (and any setup phase that needs no target); any other
    plan is replaced by the hub's own per-target plan. Idempotent. Never raises:
    a failure returns `phases` unchanged."""
    try:
        names = [n for n in (names or ())][:MAX_TARGETS]
        if len(names) < 2:
            return phases
        nm = notes_map(names)
        src = [dict(p) for p in (phases or ()) if isinstance(p, dict)]
        tags = [match_target(p, names) for p in src]
        if not src or any(tags.count(n) != 1 for n in names):
            out = [_target_phase(n, goal, nm[n]) for n in names]
            out.append(_final_phase(names, nm, final_title, range(1, len(out) + 1)))
            return out
        target_idx = {i for i, t in enumerate(tags, start=1) if t}
        kept, room = [], max(0, max_phases - len(names) - 1)
        for i, (p, t) in enumerate(zip(src, tags), start=1):
            if t is None:
                needs = {n for n in (p.get("needs") or ()) if isinstance(n, int)}
                if p.get("title") == final_title or needs & target_idx or room <= 0:
                    continue                 # the hub's final phase replaces a merge phase
                room -= 1
            kept.append((i, p, t))
        renum = {old: new for new, (old, _p, _t) in enumerate(kept, start=1)}
        setup = {renum[old] for old, _p, t in kept if t is None}
        out = []
        for old, p, t in kept:
            q = dict(p)
            needs = sorted({renum[n] for n in (q.get("needs") or ()) if n in renum})
            if t:
                q["title"] = t
                q["target"] = t
                q["parallel"] = True
                q["files"] = [nm[t]]
                q["needs"] = [n for n in needs if n in setup]
                if not str(q.get("done_when") or "").strip():
                    q["done_when"] = _done_when(nm[t])
            else:
                q.pop("target", None)
                q["needs"] = needs
            out.append(q)
        out.append(_final_phase(names, nm, final_title, range(1, len(out) + 1)))
        return out
    except Exception:                                            # noqa: BLE001
        return phases


def annotate(phases, names):
    """Phases given by the caller (not planned here): each one that is about
    exactly one target gets "target"; nothing else changes."""
    try:
        out = []
        for p in phases or ():
            q = dict(p) if isinstance(p, dict) else p
            if isinstance(q, dict):
                t = match_target(q, names)
                if t:
                    q["target"] = t
            out.append(q)
        return out
    except Exception:                                            # noqa: BLE001
        return phases


# --------------------------------------------------------------------------- #
# The worker's target block
# --------------------------------------------------------------------------- #

def target_hosts(card, name):
    """The host patterns a finding must name to be this target's: the target's
    OWN in-scope entries (all of its in-scope entries when it has none of its
    own), plus its name when the name is a host. On a card with no named target
    (the goal named hosts): just the name."""
    try:
        card = scopecard.normalize(card)
        named = any(t["name"] for t in scopecard.targets(card))
        out = []
        if named:
            own = [e["p"] for e in _own_in(card, name)]
            if not own:
                row = next((t for t in scopecard.targets(card)
                            if t["name"].lower() == _norm(name).lower()), None)
                own = [e["p"] for e in (row or {}).get("in") or ()]
            out.extend(own)
        p = _as_pattern(name)
        if p and p not in out:
            out.append(p)
        return out
    except Exception:                                            # noqa: BLE001
        return []


def _as_pattern(name):
    """`name` as a card host pattern when it is one (a host, a wildcard, an IP), else None."""
    got = scopecard.normalize({"in": [{"p": _norm(name)}]})["in"]
    return got[0]["p"] if got else None


def _host_of(row):
    t = str((row or {}).get("target") or "").strip().lower()
    return t.split("/", 1)[0] if t else ""


def host_matches(host, patterns):
    """`host` (a port allowed) is covered by one of `patterns`: an exact host,
    a wildcard (its sub-hosts and, for attribution, its apex), an IP or a CIDR."""
    if not host or not patterns:
        return False
    try:
        card = {"in": [{"p": p, "kind": "program", "src": "x"} for p in patterns], "out": []}
        if scopecard.in_scope(host, card) is True:
            return True
        bare = host.split(":", 1)[0] if host.count(":") == 1 else host
        return any(p.startswith("*.") and bare == p[2:] for p in patterns)
    except Exception:                                            # noqa: BLE001
        return False


def findings_for(rows, hosts):
    """The findings ledger rows on this target's hosts, open first (likely false
    positive, needs review, not confirmed), then proven. Deterministic."""
    try:
        out = [r for r in rows or () if isinstance(r, dict)
               and r.get("status") in _FINDING_ORDER and host_matches(_host_of(r), hosts)]
        out.sort(key=lambda r: (_FINDING_ORDER[r["status"]], -float(r.get("last_seen") or 0),
                                str(r.get("id") or "")))
        return out
    except Exception:                                            # noqa: BLE001
        return []


def tasks_for(tasks, name, hosts):
    """Open board tasks of this target: their title or detail names it (or one
    of its exact hosts). Doing first, then todo, blocked, failed; plan order."""
    try:
        exact = [h for h in hosts or () if not h.startswith("*.")]
        out = []
        for t in tasks or ():
            if not isinstance(t, dict) or t.get("status") not in _OPEN_TASK_STATUSES:
                continue
            text = "%s %s" % (t.get("title") or "", t.get("detail") or "")
            if _word_in(name, text) or any(_word_in(h, text) for h in exact):
                out.append(t)
        out.sort(key=lambda t: _OPEN_TASK_STATUSES.index(t["status"]))
        return out
    except Exception:                                            # noqa: BLE001
        return []


_NOTE_HEAD_RE = re.compile(
    r"^\s*(?:#{1,6}\s*|\*\*)?(?P<h>done|completed|finished|remaining|to ?do|todo|next(?: steps)?|"
    r"open|leads?|ideas|findings?)\b[^\n]*$", re.I)
_CHECK_RE = re.compile(r"^\s*[-*+]\s*\[(?P<x>[ xX])\]\s*(?P<t>.+)$")
_ITEM_RE = re.compile(r"^\s*(?:[-*+•]|\d{1,3}[.)])\s+(?P<t>.+)$")
_SECTION_OF = {"done": "done", "completed": "done", "finished": "done", "remaining": "remaining",
               "todo": "remaining", "to do": "remaining", "next": "remaining",
               "next steps": "remaining", "open": "remaining", "lead": "leads", "leads": "leads",
               "ideas": "leads", "finding": "leads", "findings": "leads"}


def notes_state(text):
    """{"done", "remaining", "leads": [item], "tail": str} read from a target's
    notes file: items under Done / Remaining / Leads headings, and "- [x]" /
    "- [ ]" checkboxes anywhere. When nothing is structured, "tail" holds the
    end of the file."""
    state = {"done": [], "remaining": [], "leads": [], "tail": ""}
    try:
        if not isinstance(text, str) or not text.strip():
            return state
        sec = None
        for raw in text.splitlines():
            m = _NOTE_HEAD_RE.match(raw)
            if m and not _ITEM_RE.match(raw) and len(raw.strip()) <= 60:
                sec = _SECTION_OF.get(" ".join(m.group("h").lower().split()))
                continue
            if raw.lstrip().startswith("#"):
                sec = None
                continue
            c = _CHECK_RE.match(raw)
            if c:
                state["done" if c.group("x").strip() else "remaining"].append(_clip(c.group("t"), 120))
                continue
            it = _ITEM_RE.match(raw)
            if it and sec:
                state[sec].append(_clip(it.group("t"), 120))
        if not (state["done"] or state["remaining"] or state["leads"]):
            state["tail"] = _clip(text.strip()[-500:], 400)
    except Exception:                                            # noqa: BLE001
        pass
    return state


def _fit_lines(head, lines, budget):
    """`head` + as many of `lines` as fit in `budget` chars, "(+N more)" after."""
    if not lines or budget < len(head) + 20:
        return ""
    out, used = [head], len(head)
    for i, ln in enumerate(lines):
        more = len(lines) - i - 1
        tail = len("\n- (+%d more)" % more) if more else 0
        if used + 1 + len(ln) + tail > budget:
            if len(out) == 1:
                ln = _clip(ln, max(20, budget - used - 1 - len("\n- (+%d more)" % more)))
                out.append(ln)
                used += 1 + len(ln)
                more = len(lines) - i - 1
                if more:
                    out.append("- (+%d more)" % more)
            else:
                out.append("- (+%d more)" % (len(lines) - i))
            break
        out.append(ln)
        used += 1 + len(ln)
    return "\n".join(out)


def block(name, scope_text, rows, hosts, tasks, notes_text, notes_file,
          max_chars=BLOCK_MAX_CHARS):
    """ONE target's block for its worker: the target, its scope, its findings, the
    last state of its notes file, its open board tasks, and how to keep notes --
    deterministic for the same inputs (no clock), never over `max_chars`."""
    try:
        name = _norm(name)
        head = ("YOUR TARGET: %s -- work ONLY on this target; every other target of this run has "
                "its own helper." % name)
        tail = ("NOTES: keep %s up to date as you go (\"## Done\", \"## Remaining\", \"## Leads\"); it "
                "is the only file you write for this target and the next run continues from it. A "
                "finding stays \"unverified\" there until an observed request + response proves it."
                % notes_file)
        room = max(0, max_chars - len(head) - len(tail) - 2)
        parts = [head]
        scope = str(scope_text or "").strip()
        if scope:
            if len(scope) > min(SCOPE_CHARS, room):
                scope = scope[:max(0, min(SCOPE_CHARS, room) - 3)].rstrip() + "..."
        else:
            scope = ("SCOPE: no scope is recorded for this target -- confirm it with the user before "
                     "any request.")
        parts.append(scope)
        room -= len(scope) + 1
        found = findings_for(rows, hosts)
        if found:
            lines = []
            for r in found:
                reason = (r.get("reasons") or [""])[0] if r["status"] != "proven" else ""
                lines.append("- %s: %s%s" % (_FINDING_LABEL[r["status"]],
                                             _clip(r.get("title") or r.get("class"), 70),
                                             (" -- " + _clip(reason, 110)) if reason else ""))
            text = _fit_lines("FINDINGS ON THIS TARGET (the hub's check of observed output):",
                              lines, int(room * 0.45))
            if text:
                parts.append(text)
                room -= len(text) + 1
        st = notes_state(notes_text)
        if notes_text and str(notes_text).strip():
            lines = []
            for label, key in (("Remaining", "remaining"), ("Leads", "leads")):
                if st[key]:
                    lines.append("- %s: %s" % (label, "; ".join(st[key])))
            if st["done"]:
                lines.append("- Done (%d): %s" % (len(st["done"]), "; ".join(st["done"])))
            if st["tail"]:
                lines.append("- " + st["tail"])
            lines = [_clip(ln, max(40, int(room * 0.6))) for ln in lines]
            text = _fit_lines("LAST STATE (from %s, earlier work -- continue from it, do not redo "
                              "what is done):" % notes_file, lines, int(room * 0.75))
            if text:
                parts.append(text)
                room -= len(text) + 1
        mine = tasks_for(tasks, name, hosts)
        if mine:
            lines = ["- [%s] %s" % (t["status"], _clip(t.get("title"), 80)) for t in mine]
            text = _fit_lines("OPEN BOARD TASKS FOR THIS TARGET:", lines, room)
            if text:
                parts.append(text)
        parts.append(tail)
        out = "\n".join(parts)
        if len(out) > max_chars:
            out = out[:max(0, max_chars - len(tail) - 5)].rstrip() + "...\n" + tail
            out = out[:max_chars]
        return out
    except Exception:                                            # noqa: BLE001
        return ""


def overview(names, max_chars=OVERVIEW_MAX_CHARS):
    """What a phase that is not one target's (the final merge, a setup phase) is told."""
    try:
        names = list(names or ())
        if len(names) < 2:
            return ""
        nm = notes_map(names)
        text = ("THIS RUN WORKS ON %d TARGETS, one helper each: %s. Keep every target's results "
                "separate and inside its own scope; a finding is proven only by an observed request "
                "+ response, otherwise it stays unverified."
                % (len(names), ", ".join("%s (%s)" % (n, nm[n]) for n in names)))
        return text if len(text) <= max_chars else text[:max_chars - 3].rstrip() + "..."
    except Exception:                                            # noqa: BLE001
        return ""
