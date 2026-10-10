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


def _evidence(model_id):
    """The raw evidence row for a model id, with inheritance. Returns a dict
    {tb4, automation, aa, arena, source, date[, inherited_from, floor_bump]} or
    None. A size variant only matches a row of the SAME explicit size."""
    try:
        p = modelrank.parse(model_id)
        if p is None:
            return None
        low = str(model_id or "").lower()
        gkey = (p.family, p.tier, _size_of(low))
        group = _groups().get(gkey)
        if not group:
            return None
        ver = tuple(p.version)
        if ver in group:
            mid, ev = group[ver]
            out = dict(ev)
            out.update(source=SOURCE, date=DATE, floor_bump=0.0)
            return out
        newest = max(group)                         # highest listed version
        if ver > newest:                            # a newer, unlisted release
            mid, ev = group[newest]
            out = dict(ev)
            out.update(source=SOURCE, date=DATE, inherited_from=mid,
                       floor_bump=max(0.0, min((_vnum(ver) - _vnum(newest))
                                               * _INHERIT_PER_VERSION, _INHERIT_MAX)))
            return out
        return None                                 # older / between -> no evidence
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
