"""Design, then a DRY RUN of the plan, before any Multi helper starts.

Owner, 2026-10-04: "In all works the hub must do steps: design, plan well in a
perfect architecture, then go -- and prevent problems with a DRY RUN in
planning."

Two things live here, both pure (no model call, no command run; the only I/O
is a bounded walk of the project folder's file NAMES):

- THE DESIGN a build plan carries before its phases (swarm_windows._PLAN_SYSTEM
  asks for it): components, the interfaces between them, the data flow -- plus
  the `files` each phase owns. `normalize_design` reads whatever shape a model
  answered with; `render_design` is the block every helper's prompt gets, so
  helpers working side by side build to the same contracts.

- THE DRY RUN (`check_plan`): what the plan would do, checked before it does
  it. Each finding is
      {"kind", "action", "text", "phase"}
  with action "fixed" (already applied to the returned copy), "replan" (worth
  ONE re-ask of the planner -- swarm_windows.dry_run does that) or "warn"
  (surfaced, the run goes ahead: fail open).
"""
import os
import re

# --------------------------------------------------------------------------- #
# The design
# --------------------------------------------------------------------------- #

DESIGN_CHARS = 2500          # the block a worker prompt gets, at most
_ITEM_CHARS = 240
_MAX_COMPONENTS = 12
_MAX_INTERFACES = 16
_FLOW_CHARS = 600
MAX_FILES = 20               # paths one phase may claim


def _item(value):
    """One component / interface as a line: models answer with a string, or
    an object ({"name", "role"} / {"name", "between", "contract"} ...)."""
    if isinstance(value, dict):
        name = str(value.get("name") or value.get("id") or "").strip()
        rest = [str(v).strip() for k, v in value.items()
                if k not in ("name", "id") and isinstance(v, (str, int, float))
                and str(v).strip()]
        rest += ["; ".join(str(x) for x in v) for k, v in value.items()
                 if isinstance(v, (list, tuple)) and v]
        text = (name + ": " if name and rest else name) + " -- ".join(rest)
    else:
        text = str(value or "")
    return " ".join(text.split())[:_ITEM_CHARS]


def _items(value, cap):
    if isinstance(value, dict):
        value = [{"name": k, "what": v} if not isinstance(v, dict) else dict(v, name=k)
                 for k, v in value.items()]
    if isinstance(value, str):
        value = [s for s in re.split(r"\n+|;\s+", value) if s.strip()]
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for v in value:
        line = _item(v)
        if line and line not in out:
            out.append(line)
    return out[:cap]


def normalize_design(raw):
    """{"components": [str], "interfaces": [str], "data_flow": str}, keys only
    when non-empty; {} for no design (a small fix, an unreadable answer).
    Never raises."""
    try:
        if not isinstance(raw, dict):
            return {}
        out = {}
        comps = _items(raw.get("components"), _MAX_COMPONENTS)
        if comps:
            out["components"] = comps
        ifaces = _items(raw.get("interfaces") or raw.get("contracts"), _MAX_INTERFACES)
        if ifaces:
            out["interfaces"] = ifaces
        flow = raw.get("data_flow") or raw.get("dataflow") or raw.get("flow")
        if isinstance(flow, (list, tuple)):
            flow = " -> ".join(str(x).strip() for x in flow if str(x or "").strip())
        flow = " ".join(str(flow or "").split())[:_FLOW_CHARS]
        if flow:
            out["data_flow"] = flow
        return out
    except Exception:                                            # noqa: BLE001
        return {}


def _norm_path(path):
    p = str(path or "").strip().strip("`'\"").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    p = re.sub(r"/{2,}", "/", p)
    return p


def norm_files(raw):
    """A phase's `files` as clean relative paths (a list, or one string with
    commas / newlines). [] when nothing usable."""
    if isinstance(raw, str):
        raw = re.split(r"[,\n]+", raw)
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for f in raw:
        if isinstance(f, dict):
            f = f.get("path") or f.get("file") or ""
        p = _norm_path(f)[:200]
        if p and "://" not in p and p not in out:
            out.append(p)
    return out[:MAX_FILES]


def design_line(design):
    """"Design: 3 components, 4 interfaces" -- the collapsed line."""
    d = design or {}
    n, m = len(d.get("components") or ()), len(d.get("interfaces") or ())
    bits = ["%d component%s" % (n, "" if n == 1 else "s"),
            "%d interface%s" % (m, "" if m == 1 else "s")]
    if d.get("data_flow"):
        bits.append("data flow")
    return "Design: " + ", ".join(bits)


def render_design(design, owners=(), your_index=None, your_files=(), limit=DESIGN_CHARS):
    """The block a helper's prompt carries: the shared design and who owns
    which file, with THIS helper's files first (so a clip never loses them).
    "" when there is neither a design nor any owned file."""
    d = design or {}
    owners = [(i, t, list(f)) for i, t, f in (owners or ()) if f]
    if not d and not owners:
        return ""
    head = ["DESIGN (shared by every helper of this run -- build to these "
            "interfaces; do not change one on your own):"]
    if your_files:
        head.append("YOUR FILES (phase %s): %s. Change another phase's files "
                    "only if your task says so." % (your_index, ", ".join(your_files)))
    body = []
    if d.get("components"):
        body += ["Components:"] + ["- " + c for c in d["components"]]
    if d.get("interfaces"):
        body += ["Interfaces:"] + ["- " + c for c in d["interfaces"]]
    if d.get("data_flow"):
        body += ["Data flow: " + d["data_flow"]]
    if owners:
        body += ["Files each phase owns:"] + [
            "- phase %s (%s): %s" % (i, t, ", ".join(f)) for i, t, f in owners]
    text = "\n".join(head + body)
    if len(text) > limit:
        text = text[:max(0, limit - 1)].rstrip() + "…"
    return text


# --------------------------------------------------------------------------- #
# The dry run
# --------------------------------------------------------------------------- #

_EXTS = ("html|htm|css|scss|js|mjs|cjs|ts|tsx|jsx|py|json|md|txt|yml|yaml|toml|"
         "sql|sh|bat|ps1|go|rs|java|kt|c|cc|cpp|h|hpp|cs|php|rb|vue|svelte|xml|"
         "svg|csv|ini|cfg|lock|gradle|swift|dart|env|db|sqlite")
_PATH = r"((?:[\w.-]+/)*[\w-][\w.-]*\.(?:%s))(?![\w/])" % _EXTS
_ANY_PATH_RE = re.compile(r"(?<![\w/.@-])`?" + _PATH, re.I)
_TICK_RE = re.compile(r"`([^`\s*?]+\.[A-Za-z0-9]{1,8})`")
_FILLER = r"(?:(?:the|a|an|existing|current|provided|given|shared|file|files|data|from)\s+)*"
# A path right after a verb that READS it. Conservative on purpose: "in
# app.py" can be a read or a write, so it is not here.
_READ_RE = re.compile(
    r"\b(?:read|reads|reading|load|loads|loading|parse|parses|parsing|import|"
    r"imports|importing|use|uses|using|based\s+on|according\s+to|extend|extends|"
    r"extending|following|described\s+in|defined\s+in|specified\s+in|listed\s+in|"
    r"given\s+in|from)\s+" + _FILLER + r"`?" + _PATH, re.I)
# ... and right after one that WRITES it (a producer, for a reader's order).
_WRITE_RE = re.compile(
    r"\b(?:create|creates|write|writes|generate|generates|produce|produces|save|"
    r"saves|output|outputs|emit|emits|add|adds|build|builds|scaffold)\s+"
    + _FILLER + r"`?" + _PATH, re.I)
_URL_RE = re.compile(r"https?://\S+", re.I)
_NOT_FILES = {"node.js", "next.js", "vue.js", "nuxt.js", "express.js", "react.js",
              "three.js", "chart.js", "d3.js", "socket.io", "p5.js", "alpine.js",
              "ember.js", "backbone.js", "angular.js", "deno.js", "bun.js"}
_VAGUE_DONE_RE = re.compile(
    r"^\s*(?:(?:it|this|the\s+\w+|everything|all)\s+)?(?:is\s+|are\s+)?"
    r"(?:done|finished|complete|completed|works?|working|ok|okay|good|ready|"
    r"implemented|fixed|handled)\s*[.!]?\s*$", re.I)
_INDEX_MAX = 5000
_INDEX_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", ".next",
               "dist", "build", ".cache", ".pytest_cache", ".mypy_cache"}
_LINE_ITEMS = 3
_LINE_ITEM_CHARS = 90


def _key(path):
    return _norm_path(path).casefold()


def _is_file_name(name):
    low = name.lower()
    if "://" in low or low in _NOT_FILES:
        return False
    # "Vue.js"-style framework names, capitalised and bare.
    return not ("/" not in name and low.endswith(".js") and name[:1].isupper())


def _paths(rx, text):
    out = []
    for m in rx.finditer(_URL_RE.sub(" ", text or "")):
        name = _norm_path(m.group(1))
        if name and _is_file_name(name) and name not in out:
            out.append(name)
    return out


def _phase_text(p):
    return " ".join(str(p.get(k) or "") for k in ("title", "task", "done_when"))


def _reads(p):
    """Files a phase reads: every path its "inputs" names, and a path its task
    names right after a reading verb."""
    out = _paths(_TICK_RE, p.get("inputs")) + _paths(_ANY_PATH_RE, p.get("inputs"))
    out += _paths(_READ_RE, _phase_text(p))
    seen, keep = set(), []
    for f in out:
        if _key(f) not in seen:
            seen.add(_key(f))
            keep.append(f)
    return keep


def _writes(p):
    """Files a phase produces: its declared `files`, and a path its task names
    right after a writing verb."""
    out = list(p.get("files") or ()) + _paths(_WRITE_RE, _phase_text(p))
    return [f for i, f in enumerate(out) if _key(f) not in {_key(g) for g in out[:i]}]


def _ancestors(phases):
    """For each phase (0-based list), the set of 1-based phases it waits for,
    directly or through another phase. "needs" only point earlier."""
    anc = []
    for i, p in enumerate(phases, start=1):
        s = set()
        for n in p.get("needs") or ():
            if isinstance(n, int) and 1 <= n < i:
                s.add(n)
                s |= anc[n - 1]
        anc.append(s)
    return anc


def _overlap(a, b):
    """The first path phase A and phase B both own, or None. A path ending in
    "/" (or holding a glob) owns everything under it."""
    def owned(files):
        out = []
        for f in files:
            k = _key(f)
            if any(ch in k for ch in "*?["):
                k = k[:k.find(next(ch for ch in k if ch in "*?["))]
                k = k[:k.rfind("/") + 1]
                if not k:
                    continue
            out.append((k, f))
        return out
    for ka, fa in owned(a):
        for kb, _fb in owned(b):
            if ka == kb or (ka.endswith("/") and kb.startswith(ka)) or \
                    (kb.endswith("/") and ka.startswith(kb)):
                return fa
    return None


def _index(project_dir):
    """(relative paths, basenames), casefolded, of the project's files -- or
    None when the folder is not there (a new project: nothing can be said)."""
    try:
        if not project_dir or not os.path.isdir(project_dir):
            return None
        rel, base, n = set(), set(), 0
        for root, dirs, files in os.walk(project_dir):
            dirs[:] = [d for d in dirs if d not in _INDEX_SKIP]
            for name in files:
                path = os.path.relpath(os.path.join(root, name), project_dir)
                rel.add(path.replace("\\", "/").casefold())
                base.add(name.casefold())
                n += 1
                if n >= _INDEX_MAX:
                    return rel, base
        return rel, base
    except Exception:                                            # noqa: BLE001
        return None


def _exists(path, index, project_dir):
    k = _key(path)
    if os.path.isabs(path) or re.match(r"^[a-z]:/", k):
        return os.path.exists(path)
    rel, base = index
    return k in rel or ("/" not in k and k in base) or \
        os.path.exists(os.path.join(project_dir, path))


def _done_when_missing(p):
    if str(p.get("acceptance") or "").strip():
        return False
    dw = " ".join(str(p.get("done_when") or "").split())
    return len(dw.split()) < 2 or bool(_VAGUE_DONE_RE.match(dw))


def _uncovered_parts(goal_text, phases):
    """The parts the user ENUMERATED (swarm.required_parts) that no phase
    addresses (swarm's own coverage matcher), as text. [] when the goal lists
    nothing, or the matcher cannot be loaded."""
    try:
        import swarm
        parts = swarm.required_parts(goal_text)
        if not parts:
            return []
        # swarm._phase_text reads acceptance/constraints as LISTS; here they
        # are strings. A phase's files count as what it addresses.
        shim = [{"title": p.get("title"),
                 "task": " ".join([str(p.get("task") or "")] + list(p.get("files") or ())),
                 "done_when": p.get("done_when"), "inputs": p.get("inputs"),
                 "output_format": p.get("output_format"),
                 "acceptance": [p["acceptance"]] if p.get("acceptance") else [],
                 "constraints": [p["constraints"]] if p.get("constraints") else []}
                for p in phases]
        return [parts[k - 1] for k in swarm._uncovered(parts, shim)]
    except Exception:                                            # noqa: BLE001
        return []


def _note_finding(note):
    """A clean_phases note ({kind, text}) as a finding."""
    kind = str(note.get("kind") or "sanitised")
    action = "warn" if kind == "too_many_phases" else "fixed"
    return {"kind": kind, "action": action, "text": str(note.get("text") or kind),
            "phase": note.get("phase")}


def check_plan(phases, design, goal_text, project_dir, notes=(), max_phases=None):
    """Dry-run `phases` (cleaned: 1-based "needs" to earlier phases only).

    Returns (fixed_phases, report). `fixed_phases` is a copy with the
    automatic fixes applied; `report` = {"findings": [...], "phases": n,
    "start_now": k, "replanned": False}. Checks:

      file_conflict   two phases that can run at the same time own the same
                      file -> the later one now needs the earlier (fixed)
      input_order     a phase reads a file an earlier phase it does not wait
                      for writes -> it now needs that phase (fixed)
      input_later     ... a file only a LATER phase writes (warn)
      missing_input   ... a file that is not in the project and that no
                      phase mentions (warn)
      no_done_when    no concrete done_when / acceptance (warn)
      uncovered_part  a part the user enumerated that no phase covers
                      (replan: worth one re-ask)
      sequential      3+ phases and nothing runs side by side (warn)
      `notes`         what clean_phases changed: needs dropped (a cycle or a
                      dangling need), look-only phases merged, phases past
                      the limit (fixed / warn)
    Never raises: a check that fails is skipped."""
    out = [dict(p, needs=list(p.get("needs") or ())) for p in (phases or ())]
    findings = [_note_finding(n) for n in (notes or ()) if isinstance(n, dict)]

    def title(i):
        return out[i - 1].get("title") or ("Phase %d" % i)

    def need(j, i):
        out[j - 1]["needs"] = sorted(set(out[j - 1]["needs"]) | {i})

    # 1. Two phases that can run at the same time own the same file.
    try:
        for j in range(2, len(out) + 1):
            for i in range(1, j):
                if i in _ancestors(out)[j - 1]:
                    continue
                shared = _overlap(out[i - 1].get("files") or (), out[j - 1].get("files") or ())
                if shared:
                    need(j, i)
                    findings.append({
                        "kind": "file_conflict", "action": "fixed", "phase": j,
                        "text": "phase %d waits for %d (both edit %s)" % (j, i, shared)})
    except Exception:                                            # noqa: BLE001
        pass

    # 2. A phase reads a file: is it there, or made before it?
    try:
        index = _index(project_dir)
        writers = [[_key(f) for f in _writes(p)] for p in out]
        mentions = [_URL_RE.sub(" ", _phase_text(p) + " " + str(p.get("inputs") or "")).casefold()
                    for p in out]
        for j, p in enumerate(out, start=1):
            own = {_key(f) for f in (p.get("files") or ())}
            for f in _reads(p):
                k = _key(f)
                if k in own:
                    continue
                anc = _ancestors(out)[j - 1]
                makers = [i for i, w in enumerate(writers, start=1) if i != j and k in w]
                if any(i in anc for i in makers):
                    continue
                earlier = [i for i in makers if i < j]
                if earlier:
                    need(j, earlier[0])
                    findings.append({
                        "kind": "input_order", "action": "fixed", "phase": j,
                        "text": "phase %d waits for %d (reads %s)" % (j, earlier[0], f)})
                    continue
                if index is not None and _exists(f, index, project_dir):
                    continue
                if makers:
                    findings.append({
                        "kind": "input_later", "action": "warn", "phase": j,
                        "text": "phase %d reads %s before phase %d writes it"
                                % (j, f, makers[0])})
                elif index is not None and not any(
                        k in m for i, m in enumerate(mentions, start=1) if i != j):
                    findings.append({
                        "kind": "missing_input", "action": "warn", "phase": j,
                        "text": "phase %d reads %s: not in the project" % (j, f)})
    except Exception:                                            # noqa: BLE001
        pass

    # 3. Every phase says when it is done.
    try:
        for j, p in enumerate(out, start=1):
            if _done_when_missing(p):
                findings.append({"kind": "no_done_when", "action": "warn", "phase": j,
                                 "text": "phase %d (%s) has no concrete done-when"
                                         % (j, title(j))})
    except Exception:                                            # noqa: BLE001
        pass

    # 4. Every part the user listed has a phase.
    for part in _uncovered_parts(goal_text, out):
        findings.append({"kind": "uncovered_part", "action": "replan", "phase": None,
                         "part": part[:80], "text": 'no phase covers "%s"' % part[:80]})

    # 5. Budget: a chain is the slowest plan.
    try:
        depth = []
        for j, p in enumerate(out, start=1):
            depth.append(1 + max([depth[n - 1] for n in p["needs"] if 1 <= n < j] or [0]))
        if len(out) >= 3 and max(depth) == len(out):
            findings.append({"kind": "sequential", "action": "warn", "phase": None,
                             "text": "nothing runs side by side (%d phases in a chain)"
                                     % len(out)})
    except Exception:                                            # noqa: BLE001
        pass

    report = {"findings": findings[:30], "phases": len(out),
              "start_now": sum(1 for p in out if not p.get("needs")),
              "replanned": False}
    return out, report


def _clip_items(texts):
    texts = [t if len(t) <= _LINE_ITEM_CHARS else t[:_LINE_ITEM_CHARS - 1] + "…"
             for t in texts]
    shown = "; ".join(texts[:_LINE_ITEMS])
    if len(texts) > _LINE_ITEMS:
        shown += "; +%d more" % (len(texts) - _LINE_ITEMS)
    return shown


def summarize(report, phases=None):
    """`report` with what the conversation and the run file show: counts on
    the plan as it RUNS (`phases`, review included), the fixed / warning
    texts and the one line. Returns the same dict."""
    findings = report.get("findings") or []
    if phases is not None:
        report["phases"] = len(phases)
        report["start_now"] = sum(1 for p in phases if not p.get("needs"))
    report["fixed"] = [f["text"] for f in findings if f.get("action") == "fixed"]
    report["warnings"] = [f["text"] for f in findings if f.get("action") != "fixed"]
    report["line"] = check_line(report)
    return report


def check_line(report):
    """"Plan check: 5 phases, 2 start now, 1 fixed (phase 3 waits for 2 (both
    edit app.py)), 1 warning (phase 2 (API) has no concrete done-when)"."""
    n, k = int(report.get("phases") or 0), int(report.get("start_now") or 0)
    fixed = list(report.get("fixed") or ())
    warns = list(report.get("warnings") or ())
    bits = ["%d phase%s" % (n, "" if n == 1 else "s"), "%d start now" % k]
    if report.get("replanned"):
        bits.append("planner re-asked once")
    bits.append("%d fixed" % len(fixed) + (" (%s)" % _clip_items(fixed) if fixed else ""))
    bits.append("%d warning%s" % (len(warns), "" if len(warns) == 1 else "s")
                + (" (%s)" % _clip_items(warns) if warns else ""))
    return "Plan check: " + ", ".join(bits)


def replan_ask(goal, findings, phases, design):
    """The planner's second ask: the goal, what the dry run found, and the
    plan it found it in -- fix THAT plan, do not start over."""
    import json
    prev = {"design": design or {},
            "phases": [{k: v for k, v in p.items() if v not in (None, "", [])}
                       for p in phases]}
    try:
        prev_text = json.dumps(prev, ensure_ascii=False)
    except (TypeError, ValueError):
        prev_text = ""
    return (goal + "\n\n(A dry run of your previous plan found these problems. "
            "Fix them -- add or change phases, keep what was right -- and reply "
            "with the whole corrected JSON object only.)\nProblems:\n"
            + "\n".join("- " + f["text"] for f in findings)
            + ("\nYour previous plan:\n" + prev_text[:6000] if prev_text else ""))
