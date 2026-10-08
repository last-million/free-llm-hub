"""Version and tier ordering INSIDE one model family (pure, stdlib, never raises).

Owner rule (2026-10-08): "he should be smart to know higher models by version
number, and for Claude: Opus is better than Sonnet and Sonnet better than
Haiku, and Haiku is a short/small model."

The preference floors in app._benchmark_score are family-wide: every Claude id
carried the same 138, so Haiku 4.5 and Sonnet 4 tied with Opus 5.5, and the
GPT ladder (+0.2 per minor) sat inside the 2-point auto band, so an older
generation kept being picked beside the newest one. This module only ever
LOWERS the OLDER members of ONE family+tier (relative to the newest one the hub
can reach), so it cannot reorder two families the owner ranked and no member can
exceed its owner-set ceiling. Nothing is removed: an older generation stays in
the chain as a fallback, it just leaves the top band.

    parse(id)               -> Parsed(family, tier, version) or None
    gap(newest, version)    -> points `version` must sit under `newest`
    cap(parsed, anchor_for) -> the highest score an older member may keep
    build_anchors(ids, ...) -> {(family, tier): (newest strong version, score)}

The caller (app.py) owns the "reachable" fleet, the scoring function and the
cache; nothing here reads global state.
"""
import re
from collections import namedtuple
from functools import lru_cache

Parsed = namedtuple("Parsed", "family tier version")

# One major generation behind costs at least MAJOR_STEP - MINOR_SPAN = 2.5
# points (the minor term swings by less than MINOR_SPAN either way), so the
# older generation drops out of the 2.0-point auto band. Past the first
# generation each extra one costs LEGACY_EXTRA more: Claude 3.x / GPT-4.x are
# not "one release behind", the boards place them in the bottom tiers.
MAJOR_STEP = 3.0
LEGACY_EXTRA = 1.5
# A newer minor leads by a small step, strictly under MINOR_SPAN (<= 0.5), so
# it stays inside the band and the weighted pick still spreads across it.
MINOR_SPAN = 0.5
_MINOR_HALF = 4.0
# Claude tiers. Opus leads Sonnet by a gap inside the band (Sonnet 5.5 beats
# Opus 5.5 on Terminal-Bench 4.0 on the AA harness, 63.6 vs 59.6). Fable is the
# premium line that is neither: AA ranks it under Sonnet (53 vs 56), Arena level
# with Opus (1501 vs 1504), so it is pinned halfway between them. Haiku is the
# SMALL model: 6 points under the Sonnet of the same generation.
OPUS_OVER_SONNET = 0.6
FABLE_UNDER_OPUS = 0.3
HAIKU_UNDER_SONNET = 6.0
# Only models in the strong band are ordered by version (and only a strong
# newest release is an anchor): a 0.5B "qwen4" must not drag qwen3.8 down.
STRONG = 120.0

_MAX_MAJOR = 20          # "qwen-32b" is a size, not a generation

# --- family parsers ---------------------------------------------------------
_VER = r"(\d+)(?:\.(\d+))?(?!\d)"
_CL_GLUED = re.compile(r"claude(\d)(\d)?(opus|sonnet|haiku)")            # claude40sonnet
_CL_NEW = re.compile(r"claude[-_ .]?(opus|sonnet|haiku|fable)[-_ .]?v?(\d+)"
                     r"(?:[-.](\d{1,2})(?!\d))?")                        # claude-sonnet-4-5
_CL_OLD = re.compile(r"claude[-_ .]?v?(\d+)(?:[-.](\d{1,2})(?!\d))?"
                     r"[-_ .]?(opus|sonnet|haiku|fable)")                # claude-3-7-sonnet
_CL_INSTANT = re.compile(r"claude[-_ .]?instant[-_ .]?v?(\d+)(?:\.(\d+))?")
_CL_PLAIN = re.compile(r"claude[-_ .]?v?(\d+)(?:[-.](\d{1,2})(?!\d))?")   # claude-2.1
_FAMILIES = (
    ("gpt", re.compile(r"(?<![a-z0-9])gpt-?" + _VER)),
    ("gemini", re.compile(r"(?<![a-z0-9])gemini-?" + _VER)),
    ("grok", re.compile(r"(?<![a-z0-9])grok-?" + _VER)),
    ("qwen", re.compile(r"(?<![a-z0-9])qwen-?" + _VER)),
    ("glm", re.compile(r"(?<![a-z0-9])glm-?" + _VER)),
    ("deepseek", re.compile(r"(?<![a-z0-9])deepseek[-_/ ]?v" + _VER)),
    ("kimi", re.compile(r"(?<![a-z0-9])kimi-?k-?" + _VER)),
    ("minimax", re.compile(r"(?<![a-z0-9])minimax-?m" + _VER)),
    ("mimo", re.compile(r"(?<![a-z0-9])mimo-?v?" + _VER)),
    ("hy", re.compile(r"(?<![a-z0-9])(?:hy|hunyuan)-?(\d+)()(?!\d)")),
)
_DS_R1 = re.compile(r"(?<![a-z0-9])deepseek[-_/ ]?r(\d+)(?!\d)")
_TOK = re.compile(r"[^a-z0-9]+")
_PRO_FAMILIES = frozenset(("gemini", "deepseek", "mimo"))


def _minor(text):
    """'6' -> 6; a two-digit minor ending in 0 reads as a decimal ('4.20' is
    4.2, not minor 20)."""
    if not text:
        return 0
    try:
        if len(text) == 2 and text[1] == "0":
            text = text[0]
        return int(text)
    except ValueError:
        return 0


def _tier(family, low):
    """Size / speed tier from the id's words. Only the tiers that sit on their
    own rung: flash, flash-lite, lite, mini, nano, air, small, and `pro` where a
    vendor ships pro AND flash (gemini, deepseek, mimo)."""
    toks = [t for t in _TOK.split(low) if t]
    has = set(toks)
    flash = any(t.startswith("flash") for t in toks)
    if flash and "lite" in has:
        return "flash-lite"
    if flash:
        return "flash"
    for t in ("mini", "nano", "lite", "air", "small", "tiny"):
        if t in has:
            return t
    if "pro" in has and family in _PRO_FAMILIES:
        return "pro"
    return ""


def _int(text):
    return int(text) if text else 0


def _claude(low):
    m = _CL_GLUED.search(low)
    if m:
        return m.group(3), (int(m.group(1)), _int(m.group(2)))
    m = _CL_NEW.search(low)
    if m:
        return m.group(1), (int(m.group(2)), _int(m.group(3)))
    m = _CL_OLD.search(low)
    if m:
        return m.group(3), (int(m.group(1)), _int(m.group(2)))
    m = _CL_INSTANT.search(low)
    if m:
        return "instant", (int(m.group(1)), _int(m.group(2)))
    m = _CL_PLAIN.search(low)
    if m:
        return "", (int(m.group(1)), _int(m.group(2)))
    return None


@lru_cache(maxsize=8192)
def _parse(low):
    if "claude" in low:
        got = _claude(low)
        if got is None or got[1][0] > _MAX_MAJOR:
            return None
        return Parsed("claude", got[0], got[1])
    if "distill" in low:
        return None                   # a distilled student is not its teacher's line
    m = _DS_R1.search(low)
    if m:
        return Parsed("deepseek", "r", (int(m.group(1)), 0))
    for family, rx in _FAMILIES:
        m = rx.search(low)
        if not m:
            continue
        major = int(m.group(1))
        if major > _MAX_MAJOR:
            return None
        return Parsed(family, _tier(family, low), (major, _minor(m.group(2))))
    return None


def parse(model_id):
    """Family, tier and (major, minor) of a model id in ANY spelling the hub
    sees (relay prefixes 'srv_x:' / 'GithubCopilot:', 'anthropic/', 'models/',
    ':free', '-thinking', date suffixes, 'claude-sonnet-4-5' == 4.5, glued
    'claude40sonnet'). None for an id whose family or version is unknown.
    Pass the id as the hub canonicalises it for family matching when you can;
    this still reads raw ids. Never raises."""
    try:
        low = str(model_id or "").strip().lower()
        if not low:
            return None
        return _parse(low)
    except Exception:                                            # noqa: BLE001
        return None


# --- the ordering -----------------------------------------------------------
def _tm(minor):
    m = max(0, min(int(minor), 99))
    return MINOR_SPAN * m / (m + _MINOR_HALF)


def gap(newest, version):
    """Points `version` must sit under `newest` (both (major, minor)), 0 when it
    is not older. A newer minor: under MINOR_SPAN. A whole major generation:
    always more than MAJOR_STEP - MINOR_SPAN = 2.5, and LEGACY_EXTRA more for
    each generation past the first. Strictly increasing as `version` ages."""
    try:
        nmaj, nmin = newest
        vmaj, vmin = version
        d = int(nmaj) - int(vmaj)
        if d < 0:
            return 0.0
        if d == 0:
            return max(0.0, _tm(nmin) - _tm(vmin))
        return MAJOR_STEP * d + LEGACY_EXTRA * (d - 1) + (_tm(nmin) - _tm(vmin))
    except Exception:                                            # noqa: BLE001
        return 0.0


def _reference_tiers(family, tier):
    """The tiers whose newest release this tier's generation is measured
    against, first reachable wins. Claude: Haiku (and the old untiered ids) are
    measured against Sonnet, so 'Haiku is 6 under the Sonnet of the same
    generation' holds at every version."""
    if family == "claude":
        if tier in ("haiku", "instant"):
            return ("sonnet", "opus", "fable", tier)
        if tier == "":
            return ("sonnet", "opus", "fable", tier)
        return (tier,)
    return (tier,)


def tier_offset(family, tier, has_opus):
    """Points a tier sits under the family's top tier. Claude only; the Opus >
    Sonnet gap only applies when an Opus is reachable (no Opus, nothing to be
    under), the 'Haiku is small' gap always."""
    if family != "claude" or tier == "opus":
        return 0.0
    mid = OPUS_OVER_SONNET if has_opus else 0.0
    if tier == "fable":
        return FABLE_UNDER_OPUS if has_opus else 0.0
    if tier in ("haiku", "instant"):
        return mid + HAIKU_UNDER_SONNET
    return mid


def cap(parsed, anchor_for):
    """The highest score `parsed` may keep, or None (no adjustment).
    `anchor_for(family, tier)` -> (newest version, its score) or None, from
    build_anchors. The newest member of its own tier keeps what it has."""
    try:
        if parsed is None:
            return None
        fam, tier, ver = parsed
        anchor = None
        ref = tier
        for ref in _reference_tiers(fam, tier):
            anchor = anchor_for(fam, ref)
            if anchor is not None:
                break
        if anchor is None:
            return None
        nver, nscore = anchor
        if ref == tier and tuple(ver) > tuple(nver):
            return None               # newer than anything reachable: leave it
        has_opus = fam == "claude" and anchor_for(fam, "opus") is not None
        return float(nscore) - tier_offset(fam, tier, has_opus) - gap(nver, ver)
    except Exception:                                            # noqa: BLE001
        return None


def cap_for(model_id, anchor_for):
    return cap(parse(model_id), anchor_for)


def build_anchors(ids, score_fn, canon=None, strong=STRONG):
    """{(family, tier): (version, score)} of the NEWEST STRONG release per
    family+tier among `ids` (the models actually reachable). `score_fn(id)` is
    the provider-neutral score, `canon(lower_id)` the hub's id canonicaliser. A
    newest release scoring under `strong` (a tiny or speed cut) is skipped for
    the next version down. Never raises."""
    anchors = {}
    try:
        groups = {}
        for mid in ids or ():
            low = str(mid or "").strip().lower()
            if canon is not None:
                try:
                    low = canon(low)
                except Exception:                                # noqa: BLE001
                    pass
            p = parse(low)
            if p is None:
                continue
            groups.setdefault((p.family, p.tier), {}).setdefault(p.version, []).append(mid)
        for key, byver in groups.items():
            for ver in sorted(byver, reverse=True):
                best = None
                for mid in byver[ver]:
                    try:
                        s = float(score_fn(mid))
                    except Exception:                            # noqa: BLE001
                        continue
                    best = s if best is None else max(best, s)
                if best is not None and best >= strong:
                    anchors[key] = (ver, best)
                    break
    except Exception:                                            # noqa: BLE001
        pass
    return anchors
