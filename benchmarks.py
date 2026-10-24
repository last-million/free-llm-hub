"""Board-sourced benchmark evidence for model ranking (pure, stdlib, never raises).

OWNER DECISION 2026-10-10 (replaces the 2026-07-31 "Kimi K3 top free model,
138.1 just above GLM 5.3's 138"): "benchmark them in our hub like the public
boards, and in future a NEW version must of course rank higher than them
automatically."

Two kinds of evidence, each driving a DIFFERENT routing path in app.py:

  * AGENTIC evidence (Terminal-Bench 4.0 % + AutomationBench %, Artificial
    Analysis's agentic-coding harness) orders TOOL turns -- app._agentic_score
    adds agentic_delta(ev), so the model with the strongest agentic record leads
    agent/CLI work.
  * GENERAL evidence (AA Intelligence Index + the LMArena text board) orders
    TOOL-FREE (chat) turns -- app._benchmark_score places the named models in
    the strong band by general_rank(ev).

The numbers are a dated, sourced snapshot (see TABLE / SOURCE / DATE below, or a
benchmarks.json dropped beside this file with the same shape). Where a KEYLESS
official source the hub ALREADY fetches exposes the same field, the snapshot is
refreshed automatically -- see parse_openrouter_row(). Today OpenRouter's public
catalog (app._fetch_aa_scores_keyless) exposes AA's top-line intelligence_index
only; it does NOT expose the Terminal-Bench / AutomationBench sub-indexes, so the
agentic numbers stay in the table until a keyless source publishes them.

A NEW VERSION of a family+tier the boards do not list yet (glm-5.4, kimi-k3.5,
deepseek-v4.2-flash, qwen3.9-27b) INHERITS its predecessor's evidence -- matched
by modelrank.parse (family, tier) plus the explicit parameter size -- plus a
small version bump, so it ranks JUST ABOVE that predecessor automatically; once
a board lists it, its own row wins. Older versions are lowered by the existing
modelrank rule in app._benchmark_score. A SIZE variant (an id naming an explicit
small parameter count, e.g. 27b) is matched only to a row of the SAME size, so it
never inherits the full-size flagship's evidence or floor.

Nothing here reads global state or the network; every function fails open.
"""
import json
import os
import re
import threading

import modelrank

# --------------------------------------------------------------------------- #
# The dated, sourced snapshot. SOURCE/DATE are reported on /api/tracking.
# --------------------------------------------------------------------------- #
SOURCE = ("Artificial Analysis v4.3.2 + LMArena (arena.ai); "
          "TB4.0 = AA Terminal-Bench 4.0 (agentic coding in a terminal)")
DATE = "2026-10-10"

# id -> {tb4 (%), automation (%), aa (AA Intelligence Index), arena (LMArena rating)}.
# Read 2026-10-10. Qwen 3.8 27B is the SMALL size variant (the flagship
# Qwen3.8-Max is AA 45 / Arena #22 and is NOT listed here, so it keeps its own
# family floor). DeepSeek V4.1 Flash AA is its reasoning (max) index.
TABLE = {
    "z-ai/glm-5.3":        {"tb4": 42, "automation": 62, "aa": 45, "arena": 1478},
    "z-ai/glm-5.3-flash":  {"tb4": 33, "automation": 60, "aa": 42, "arena": 1475},
    "deepseek-v4.1-flash": {"tb4": 27, "automation": 69, "aa": 39, "arena": 1475},
    "gemini-3.8-flash":    {"tb4": 20, "automation": 60, "aa": 41, "arena": 1497},
    "moonshotai/kimi-k3":  {"tb4": 13, "automation": 58, "aa": 44, "arena": 1488},
    "qwen3.8-27b":         {"tb4":  6, "automation": 48, "aa": 34, "arena": 1438},
}

# Rows read LATER than the snapshot above carry their own source/date (the
# module-level SOURCE/DATE are only the default). Every number below was read
# from the page named in "source" -- none is estimated. A field the page did not
# show is None (never guessed): the model then keeps its family floor for that
# kind of evidence.
#
# HOW THE FIGURES WERE READ (2026-10-24; this SUPERSEDES the 2026-10-23 note that
# only the top three of 32 charted models were readable). The visible prose of an
# artificialanalysis.ai evaluation page names only the top three, but the same
# page ships the data behind its chart (the Next.js payload: one object per
# charted model with terminalBench40, automationBenchPartialScore,
# intelligenceIndex). The top-three sentence the page prints ("Gemini 4 Argon
# (High) scores the highest on AutomationBench-AA with a score of 77.5%, followed
# by Kimi K3.1 Pro with 73.3% ...") equals that payload's field to the decimal, so
# the field IS the board's own score. GPT-6 Astra's 59.1 / 68.5 were found a
# second time in the payload of its per-model page (artificialanalysis.ai/models/
# gpt-6-astra, which prints its Index as 53). The variant is the board's HEADLINE
# one ("Max", the entry the chart selects by default; Opus / Sonnet 5.5 read
# "(Max, Default Fallback)"), never the best-scoring effort level: Fable 5.1
# Xhigh reads TB4.0 55.1 against Max 52.0, Astra Xhigh 59.6 against Max 59.1.
#   * Terminal-Bench 4.0 (artificialanalysis.ai/evaluations/terminalbench-4-0, AA
#     harness, 32 charted entries): Kimi K3.1 Pro 67.2, Claude Sonnet 5.5 63.6,
#     Claude Opus 5.5 59.6 (both Max, Default Fallback), GPT-6 Astra (Max) 59.1,
#     Gemini 4 Argon (High) 57.1, GPT-6.1 Sol (Max) 56.1, Claude Fable 5.1 (Max,
#     Default Fallback) 52.0, Claude Haiku 5.5 (Max) 32.8, GPT-6 Luna (Max) 12.6.
#     The models index (artificialanalysis.ai/leaderboards/models, 272 models) adds
#     GPT-5.6 Terra (Max) 35.4 and Gemini 3.1 Pro Preview 4.0.
#   * AutomationBench-AA (artificialanalysis.ai/evaluations/automationbench-aa;
#     the page's "AutomationBench-AA" score = its partial score): Gemini 4 Argon
#     (High) 77.5, Kimi K3.1 Pro 73.3, Sonnet 5.5 71.8, Opus 5.5 69.5, Astra 68.5,
#     Sol 6.1 64.9, Fable 5.1 59.4, Luna 53.2, Haiku 5.5 35.4 (MiniMax-M3.1-Flash
#     72.5 NOT added: a speed cut with no other figure). GPT-5.6 Terra and Gemini
#     3.1 Pro are not among its 32 charted entries -> None.
#   * AA Intelligence Index v4.3.2 (artificialanalysis.ai/leaderboards/models),
#     rounded to the integer the site prints (Opus 5.5 57.6 -> 58): Opus 58,
#     Sonnet 56, Kimi K3.1 Pro 55, Argon 53, Astra 52.7 -> 53, Fable 5.1 53.4 ->
#     53, Sol 6.1 51.8 -> 52, Haiku 5.5 43.4 -> 43, Terra 5.6 42.1 -> 42, Luna
#     38.1 -> 38, Gemini 3.1 Pro 29.7 -> 30.
#   * LMArena text (the hub's arena_scores.json = lmarena-ai/leaderboard-dataset,
#     text_style_control, overall, board published 2026-10-20): claude-opus-5-5-
#     (high) 1508.8, claude-sonnet-5-5-(xhigh) 1477.1, gemini-4-argon-(high)
#     1523.9, claude-fable-5-1-(max) 1502.6, gpt-6-1-sol-(max) 1479.4,
#     gpt-6-astra-(max) 1475.5, gpt-6-luna-(max) 1441.0, gpt-5-6-terra-(xhigh)
#     1467.1, gemini-3-1-pro 1486.8. Claude Haiku 5.5 has no row in that copy; the
#     same dataset pulled live (datasets-server.huggingface.co/rows, board
#     published 2026-10-22) lists "Claude Haiku 5.5 (High)" 1424.6 (no Max entry).
#     Kimi K3.1 Pro has no Arena row.
# NO ROW on purpose (read 2026-10-24, kept here as a record):
#   * Nemotron -- AA Index / TB4.0: 3.5 Super VL 26.5 / 0.0 (AutomationBench
#     14.8), 3 Ultra 550B-A55B 22.9 / 0.5, 3.5 Lightning 12.9 / 0.5, 3.5 Bolt
#     74B-A9B 21.4 / 0.0; LMArena 3 Ultra 1426.3, 3.5 Super VL 1414.2, 3.5
#     Lightning 1353.2. modelrank.parse knows no nemotron family, so a row would be
#     ignored; and were it parsed, the general floor (133.0 at the bottom of the
#     AA / Arena range) would LIFT a 26-point model into the strong band. Nemotron
#     keeps the AA-cache path (app._aa_score_for) and its last-resort tail; its
#     TB4.0 is ~0, so no agentic points are lost.
#   * GPT-6 Sol 6.0 (AA lists only a "Daybreak Blue" variant with no figures) and
#     GPT-6 Terra (no such entry on AA or LMArena): no row. _evidence refuses to
#     answer gpt-6-sol with gpt-6-astra's figures although both parse as (6, 0).
#   * Claude Haiku 4.5, Fable 5.0: older than a listed row of the same line and
#     absent from the AA lists.
_AA_TB4 = "https://artificialanalysis.ai/evaluations/terminalbench-4-0"
_AA_AUTO = "https://artificialanalysis.ai/evaluations/automationbench-aa"
_AA_IDX = "https://artificialanalysis.ai/leaderboards/models"
_ROW_DATE = "2026-10-23"          # Kimi K3.1 Pro (unchanged since)
_ROW_DATE_2 = "2026-10-24"        # every row read or completed on 2026-10-24
TABLE.update({
    "claude-opus-5.5": {
        "tb4": 59.6, "automation": 69.5, "aa": 58, "arena": 1508.8,
        "date": _ROW_DATE_2,
        "source": "AA Terminal-Bench 4.0 (Max, Default Fallback) %s; AutomationBench-AA "
                  "(Max, Default Fallback) %s; AA Index v4.3.2 %s; LMArena "
                  "claude-opus-5-5-(high)" % (_AA_TB4, _AA_AUTO, _AA_IDX)},
    "claude-sonnet-5.5": {
        "tb4": 63.6, "automation": 71.8, "aa": 56, "arena": 1477.1,
        "date": _ROW_DATE_2,
        "source": "AA Terminal-Bench 4.0 (Max, Default Fallback) %s; AutomationBench-AA "
                  "(Max, Default Fallback) %s; AA Index v4.3.2 %s; LMArena "
                  "claude-sonnet-5-5-(xhigh)" % (_AA_TB4, _AA_AUTO, _AA_IDX)},
    "moonshotai/kimi-k3.1-pro": {
        "tb4": 67.2, "automation": 73.3, "aa": 55, "arena": None,
        "date": _ROW_DATE,
        "source": "AA Terminal-Bench 4.0 %s; AutomationBench-AA %s; AA Index "
                  "v4.3.2 %s" % (_AA_TB4, _AA_AUTO, _AA_IDX)},
    "gemini-4-argon": {
        "tb4": 57.1, "automation": 77.5, "aa": 53, "arena": 1523.9,
        "date": _ROW_DATE_2,
        "source": "AA Terminal-Bench 4.0 (High) %s; AutomationBench-AA (High) %s; AA "
                  "Index v4.3.2 (high) %s; LMArena gemini-4-argon-(high)"
                  % (_AA_TB4, _AA_AUTO, _AA_IDX)},
})

# Rows read 2026-10-24 (GPT-6.x, Fable, Haiku, Gemini Pro; see the note above).
_ARENA_COPY = "LMArena %s (arena_scores.json copy, board published 2026-10-20)"
_FROM_CHART = " Read from the pages' embedded chart data."


def _src(variant, tb=True, auto=True, arena=None, arena_note=_ARENA_COPY):
    """The source string of a 2026-10-24 row (fields not on a page are left out)."""
    parts = []
    if tb:
        parts.append("AA Terminal-Bench 4.0 (%s) %s" % (variant, _AA_TB4))
    if auto:
        parts.append("AutomationBench-AA (%s) %s" % (variant, _AA_AUTO))
    parts.append("AA Index v4.3.2 %s" % _AA_IDX)
    if arena:
        parts.append(arena_note % arena)
    return "; ".join(parts) + "." + _FROM_CHART


TABLE.update({
    "gpt-6-astra": {
        "tb4": 59.1, "automation": 68.5, "aa": 53, "arena": 1475.5,
        "date": _ROW_DATE_2,
        "source": _src("Max", arena="gpt-6-astra-(max)")
                  + " GPT-6 Sol 6.0 does not share this row."},
    "gpt-6.1-sol": {
        "tb4": 56.1, "automation": 64.9, "aa": 52, "arena": 1479.4,
        "date": _ROW_DATE_2,
        "source": _src("Max", arena="gpt-6-1-sol-(max)")},
    "gpt-6-luna": {
        "tb4": 12.6, "automation": 53.2, "aa": 38, "arena": 1441.0,
        "date": _ROW_DATE_2,
        "source": _src("Max", arena="gpt-6-luna-(max)")},
    "gpt-5.6-terra": {
        # GPT-6 Terra does not exist on the boards: this is the 5.6 Terra row, and
        # a gpt-6-terra would INHERIT it (inherited_from), not read a figure.
        "tb4": 35.4, "automation": None, "aa": 42, "arena": 1467.1,
        "date": _ROW_DATE_2,
        "source": _src("Max", auto=False, arena="gpt-5-6-terra-(xhigh)")
                  + " No AutomationBench-AA entry."},
    "claude-fable-5.1": {
        "tb4": 52.0, "automation": 59.4, "aa": 53, "arena": 1502.6,
        "date": _ROW_DATE_2,
        "source": _src("Max, Default Fallback", arena="claude-fable-5-1-(max)")},
    "claude-haiku-5.5": {
        "tb4": 32.8, "automation": 35.4, "aa": 43, "arena": 1424.6,
        "date": _ROW_DATE_2,
        "source": _src("Max", arena="Claude Haiku 5.5 (High)",
                       arena_note="LMArena %s (same dataset pulled live, board "
                                  "published 2026-10-22; no Max entry)")},
    "gemini-3.1-pro": {
        # The Preview. Not on the AutomationBench-AA page -> None. A newer pro
        # (3.8 / 4) never reads this row when its own flash line scores higher
        # (see _evidence): pro >= flash is the owner's rule.
        "tb4": 4.0, "automation": None, "aa": 30, "arena": 1486.8,
        "date": _ROW_DATE_2,
        "source": "AA Terminal-Bench 4.0 (Gemini 3.1 Pro Preview) %s; AA Index v4.3.2 "
                  "%s; LMArena %s.%s No AutomationBench-AA entry."
                  % (_AA_TB4, _AA_IDX,
                     "gemini-3-1-pro (arena_scores.json copy, board published "
                     "2026-10-20)", _FROM_CHART)},
})

# --------------------------------------------------------------------------- #
# Mappings (monotone, bounded, documented). Both are product decisions, not
# capability estimates, so they live here beside the numbers they consume.
# --------------------------------------------------------------------------- #
# AGENTIC -> the points app._agentic_score adds to a TOOL-turn candidate.
# TB4.0 is PRIMARY. A 29-point TB4.0 gap (GLM 5.3 42 vs Kimi K3 13) maps to 2.9
# points -- clear of app._AUTO_TOP_BAND (2.0), so a 29-point gap is never a coin
# flip. A 9-point gap (GLM 5.3 42 vs GLM 5.3 Flash 33) maps to 0.9, INSIDE the
# band. AutomationBench is a sub-point tiebreak that never flips the TB4.0 order.
TB_WEIGHT = 10.0
AUTO_WEIGHT = 1.0
AGENTIC_MAX = TB_WEIGHT + AUTO_WEIGHT        # the bonus can never exceed this

# GENERAL -> a 0..1 rank app maps into the strong band. AA and LMArena are each
# normalized across the current strong-free cluster, then averaged, so the three
# strongest free chat models stay "close at the top" and the rest sit just below.
AA_LO, AA_HI = 33.0, 46.0
ARENA_LO, ARENA_HI = 1435.0, 1500.0

# A newer, unlisted version inherits its predecessor's numbers plus this bump
# (0.2 pts per 1.0 of version, capped) so it ranks just above the predecessor.
_INHERIT_PER_VERSION = 0.2
_INHERIT_MAX = 0.30

_SIZE_RE = re.compile(r"(?<![a-z0-9.])(\d{1,4})b(?![a-z])")
_lock = threading.Lock()
_compiled = None                              # cached {(family, tier, size): {version: (id, ev)}}


def _clamp01(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def _size_of(low):
    """The explicit parameter size an id names (27 for 'qwen3.8-27b'), or None.
    A version like '5.3' is never read as a size (no trailing 'b')."""
    m = _SIZE_RE.search(low or "")
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _load_overrides():
    """benchmarks.json beside this module, same shape as TABLE, merged over it.
    Lets the owner refresh the numbers without editing code. Fails open to {}."""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "benchmarks.json")
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        rows = data.get("models") if isinstance(data, dict) else None
        return rows if isinstance(rows, dict) else {}
    except Exception:
        return {}


def _groups():
    global _compiled
    hit = _compiled
    if hit is not None:
        return hit
    with _lock:
        if _compiled is not None:
            return _compiled
        merged = dict(TABLE)
        for mid, ev in _load_overrides().items():
            if isinstance(ev, dict):
                merged[mid] = ev
        out = {}
        for mid, ev in merged.items():
            try:
                p = modelrank.parse(mid)
                if p is None:
                    continue
                gkey = (p.family, p.tier, _size_of(str(mid).lower()))
                out.setdefault(gkey, {})[tuple(p.version)] = (mid, dict(ev))
            except Exception:
                continue
        _compiled = out
        return out


def reset():
    """Drop the compiled cache (tests that patch TABLE/benchmarks.json call it)."""
    global _compiled
    with _lock:
        _compiled = None


def _vnum(ver):
    try:
        return float(ver[0]) + min(int(ver[1]), 9) * 0.1
    except Exception:
        return 0.0


def _fill_from_older(out, parsed, size, ver):
    """A row whose page did not show a figure (None) takes it from the same
    family's newest OLDER listed release (same parameter size; the same tier
    wins a tie on version):
    the rule every unlisted newer version already follows ("a newer release is
    at least as good as the one it replaces"), applied to the one hole a LISTED
    row can have. Gemini 4 Argon has an AutomationBench figure but no Terminal-
    Bench 4.0 one; without this its older sibling gemini-3.8-flash (TB4.0 20)
    outscored it on tool turns. Never touches a figure the row has, never
    invents one, and records where it came from."""
    try:
        for field in ("tb4", "automation"):
            if out.get(field) is not None:
                continue
            best = None
            for (fam, tier, sz), grp in _groups().items():
                if fam != parsed.family or sz != size:
                    continue
                for v, (mid, ev) in grp.items():
                    if v >= tuple(ver) or ev.get(field) is None:
                        continue
                    key = (v, tier == parsed.tier)
                    if best is None or key > best[0]:
                        best = (key, mid, ev[field])
            if best is not None:
                out[field] = best[2]
                out.setdefault("inherited_fields", {})[field] = best[1]
    except Exception:
        pass


_LINE_CODENAMES = ("astra", "sol")


def _line_codename(low):
    """The GPT flagship-line codename an id names ('astra' / 'sol'), else None.
    modelrank reads both as ONE untiered line, so gpt-6-astra and gpt-6-sol parse
    to the same (family, tier, version)."""
    try:
        toks = set(re.split(r"[^a-z0-9]+", low or ""))
        for name in _LINE_CODENAMES:
            if name in toks:
                return name
    except Exception:
        pass
    return None


def _resolve(group, parsed, low, size):
    """Evidence for `parsed` from ONE version group -> (row id, evidence dict) or
    None: its exact row, the newest row plus a version bump for a newer unlisted
    release, or None (older / between rows)."""
    ver = tuple(parsed.version)
    if ver in group:
        mid, ev = group[ver]
        if parsed.family == "gpt" and not parsed.tier:
            # gpt-6-sol is NOT gpt-6-astra: the same (6, 0) key, but the page
            # that gave Astra its figures lists none for Sol 6.0. Answering with
            # Astra's numbers would present them as Sol's (/api/tracking) and
            # settle the owner's open Sol-vs-Astra call by accident.
            a, b = _line_codename(low), _line_codename(str(mid).lower())
            if a and b and a != b:
                return None
        out = dict(ev)
        out.setdefault("source", SOURCE)
        out.setdefault("date", DATE)
        out["floor_bump"] = 0.0
        _fill_from_older(out, parsed, size, ver)
        return mid, out
    newest = max(group)                             # highest listed version
    if ver > newest:                                # a newer, unlisted release
        mid, ev = group[newest]
        out = dict(ev)
        out.setdefault("source", SOURCE)
        out.setdefault("date", DATE)
        out.update(inherited_from=mid,
                   floor_bump=max(0.0, min((_vnum(ver) - _vnum(newest))
                                           * _INHERIT_PER_VERSION, _INHERIT_MAX)))
        return mid, out
    return None                                     # older / between -> no evidence


def _evidence(model_id):
    """The raw evidence row for a model id, with inheritance. Returns a dict
    {tb4, automation, aa, arena, source, date[, inherited_from, floor_bump]} or
    None. A size variant only matches a row of the SAME explicit size."""
    try:
        p = modelrank.parse(model_id)
        if p is None:
            return None
        low = str(model_id or "").lower()
        size = _size_of(low)
        groups = _groups()
        group = groups.get((p.family, p.tier, size))
        hit = _resolve(group, p, low, size) if group else None
        if p.tier == "pro":
            # A vendor's PRO cut is never ranked under its own FLASH cut (owner
            # rule: "gemini pro is better than gemini flash"). The flash line's
            # evidence is the least the boards support for a pro id of the same
            # or a later release, and nothing is invented: without this a pro id
            # scored NOTHING on a tool turn while its flash sibling scored +2.6.
            # A pro row listed from an OLDER generation (Gemini 3.1 Pro: TB4.0
            # 4.0) must not drag a newer pro (3.8 / 4) under that flash cut, so
            # an unlisted pro takes whichever line is stronger on EITHER kind of
            # evidence; an EXACT pro row always keeps its own figures.
            flash_group = groups.get((p.family, "flash", size))
            fhit = _resolve(flash_group, p, low, size) if flash_group else None
            if fhit is not None:
                exact_pro = hit is not None and "inherited_from" not in hit[1]
                if not exact_pro and (
                        hit is None
                        or agentic_delta(fhit[1]) > agentic_delta(hit[1])
                        or (general_rank(fhit[1]) or 0.0) > (general_rank(hit[1]) or 0.0)):
                    hit = (fhit[0], dict(fhit[1]))
                    hit[1].setdefault("inherited_from", fhit[0])
        return hit[1] if hit is not None else None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Public API (app.py calls these; see the _ev_* wrappers there).
# --------------------------------------------------------------------------- #
def agentic_evidence(model_id):
    """Terminal-Bench 4.0 / AutomationBench evidence for a model, or None."""
    ev = _evidence(model_id)
    if ev is None or (ev.get("tb4") is None and ev.get("automation") is None):
        return None
    return ev


def general_evidence(model_id):
    """AA Intelligence Index / LMArena rating evidence for a model, or None."""
    ev = _evidence(model_id)
    if ev is None or (ev.get("aa") is None and ev.get("arena") is None):
        return None
    return ev


def agentic_delta(ev):
    """Points to ADD to a TOOL-turn score (app._agentic_score). 0 when no agentic
    evidence. Monotone in TB4.0 (primary) then AutomationBench (tiebreak)."""
    if not ev:
        return 0.0
    tb, au = ev.get("tb4"), ev.get("automation")
    if tb is None and au is None:
        return 0.0
    d = 0.0
    if tb is not None:
        d += TB_WEIGHT * _clamp01(tb / 100.0)
    if au is not None:
        d += AUTO_WEIGHT * _clamp01(au / 100.0)
    return d


def general_rank(ev):
    """A 0..1 general-strength rank (AA + LMArena averaged), or None when the row
    carries neither. app maps this into the strong band."""
    if not ev:
        return None
    parts = []
    if ev.get("aa") is not None:
        parts.append(_clamp01((float(ev["aa"]) - AA_LO) / (AA_HI - AA_LO)))
    if ev.get("arena") is not None:
        parts.append(_clamp01((float(ev["arena"]) - ARENA_LO) / (ARENA_HI - ARENA_LO)))
    if not parts:
        return None
    return sum(parts) / len(parts)


def parse_openrouter_row(row):
    """Best-effort KEYLESS enrichment: pull any agentic/coding evaluation field
    an OpenRouter-catalog row (row['benchmarks']['artificial_analysis'] and a few
    common shapes) exposes, mapped to our {tb4, automation, aa} keys. Returns {}
    when none are present -- which is the case today, so the dated TABLE above is
    what drives ranking until a keyless source publishes these sub-indexes. The
    hook exists so that the day it does, no code change is needed. Never raises."""
    out = {}
    try:
        bench = row.get("benchmarks") if isinstance(row, dict) else None
        aa = bench.get("artificial_analysis") if isinstance(bench, dict) else None
        if not isinstance(aa, dict):
            aa = {}
        # AA's top-line intelligence index (the one app already consumes).
        for k in ("intelligence_index", "aa_intelligence_index", "intelligence"):
            if aa.get(k) is not None:
                out["aa"] = float(aa[k])
                break
        # Agentic / terminal-coding sub-indexes, under several plausible names
        # (none published keyless as of 2026-10-10 -- forward-compatible only).
        for k in ("terminal_bench_4", "terminal_bench", "terminal_bench_4_0",
                  "tb4", "tb_4", "agentic_coding_index", "agentic_index"):
            if aa.get(k) is not None:
                out["tb4"] = float(aa[k])
                break
        for k in ("automation_bench", "automationbench", "automation",
                  "automation_index"):
            if aa.get(k) is not None:
                out["automation"] = float(aa[k])
                break
    except Exception:
        return {}
    return out


def rows():
    """The compiled snapshot, for introspection/tests: [(id, ev), ...]."""
    out = []
    for group in _groups().values():
        for ver in sorted(group):
            mid, ev = group[ver]
            out.append((mid, dict(ev)))
    return out
