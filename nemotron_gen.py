"""Which NVIDIA Nemotron generations the hub treats as current (pure, stdlib,
never raises, no import from app).

Owner decision 2026-10-23: "allow the latest nemotron 3.5". Until then the whole
nemotron family was a last resort: blocked by default and partitioned to the
TAIL of every chain (app._LOW_QUALITY_RE). That was right for the Nemotron 3
line the owner measured (nano / super / ultra) and for the 2024 Nemotron-4
340B and the llama-/mistral-nemotron fine-tunes; it is wrong for the 3.5
generation (super-vl 120B scores 76 on Artificial Analysis, above 3 Ultra's 72).

The rule is ONE version cut, `ALLOWED_FROM`, compared as an (major, minor) int
tuple against the version written in the id. Anything this module cannot read
as a version >= the cut keeps today's behaviour (blocked / tail): the default is
deny, so a spelling it does not know costs nothing, and a later generation
(3.6, 4, 5 ...) passes by itself as long as it is written the way NVIDIA writes
every generation since 3: `nemotron-<version>-<tier>-...`.

    generation(id)          -> (major, minor) or None
    is_non_chat(id)         -> True for classifier / parser / reward variants
    is_current_chat(id)     -> generation >= ALLOWED_FROM and a chat model

Id shapes read (checked against the catalogs this hub sees, 2026-10-23):

    nvidia/nvidia/nemotron-3.5-super-vl-120b-a12b      -> (3, 5)
    openrouter/nvidia/nemotron-3.5-lightning:free      -> (3, 5)
    g4f/srv_mkomb...:nvidia/nemotron-3.5-super-vl      -> (3, 5)   (relay prefix ignored)
    nemotron-3-5-lightning / nemotron_3_5_x / v3.5     -> (3, 5)
    nemotron-3-ultra-550b-a55b, llm7 nemotron-3-nano:30b -> (3, 0)
    nemotron-nano-3-30b-a3b                            -> (3, 0)   (tier before version)
    nemotron-4-340b-instruct                           -> None     (the 2024 model, see LEGACY)
    llama-3.1-nemotron-70b-instruct, mistral-nemotron  -> None     (fine-tunes, no version)
    nemotron-parse-2.0, nemotron-3.5-content-safety    -> None / non-chat
"""
import re
from functools import lru_cache

# THE version cut. A Nemotron whose id says (major, minor) >= this is the
# current generation: not shipped-blocked, not in the last-resort tail. Raise
# it only when a newer generation turns out to be a regression; lower it never
# (Nemotron 3 nano / super / ultra stay last-resort on the owner's evidence).
ALLOWED_FROM = (3, 5)

# A major above this is a size or a date, not a generation ("nemotron-340").
_MAX_MAJOR = 20

# Words NVIDIA puts between "nemotron" and the version in the OLD order
# ("nemotron-nano-3-30b-a3b" is Nemotron 3 Nano). Only these are read, so an
# unknown word before a number is never taken for a tier.
_TIER_WORDS = frozenset(("nano", "super", "ultra", "lightning", "bolt", "mini",
                         "micro"))

# Variants that share the name but are not chat models: a safety classifier, a
# document parser, a reward model, an embedder/reranker. They answer "safe" /
# a score / vectors, so they stay blocked and in the tail whatever their
# generation (nemotron-3.5-content-safety is shipped-blocked for this reason).
NON_CHAT_WORDS = frozenset((
    "safety", "guard", "guardrail", "guardrails", "reward", "parse", "ocr",
    "embed", "embedding", "embedqa", "rerank", "rerankqa", "retriever",
    "classifier",
))

# LEGACY. The 2024 Nemotron-4 line: the number 4 is NOT a newer generation than
# 3.5 here, because NVIDIA's numbering restarted with Nemotron 3 (2025). Its
# ids carry a model SIZE straight after the version ("nemotron-4-340b-instruct",
# "-reward"), which the size test below catches, and its small members are named
# here explicitly. This wins over the numeric cut. A future "nemotron-4-ultra"
# (version, then a tier word) is not legacy and passes.
_LEGACY_RE = re.compile(r"nemotron[-_ ]4[-_ ](?:340b|mini|minitron)")

_NAME_RE = re.compile(r"(?<![a-z0-9])nemotron(?![a-z])")
_SPLIT_RE = re.compile(r"[-_ /:@]+")
# "3", "3.5", "v3.5" -- a bare version token; never a size ("340b") or a date.
_VERSION_RE = re.compile(r"^v?(\d{1,2})(?:\.(\d{1,2}))?$")
_PLAIN_INT_RE = re.compile(r"^\d{1,2}$")
_SIZE_RE = re.compile(r"^\d+(?:\.\d+)?[bm]$")


def _tokens(rest):
    return [t for t in _SPLIT_RE.split(rest) if t]


def _version_at(toks, i):
    """Read a version starting at toks[i] -> ((major, minor), tokens used) or
    None. '3.5' is one token; '3', '5' (a dash for the dot) is two -- but only
    when the second is a bare 1-2 digit number, so '3-30b' stays a 3."""
    if i >= len(toks):
        return None
    m = _VERSION_RE.match(toks[i])
    if not m:
        return None
    major = int(m.group(1))
    if major > _MAX_MAJOR:
        return None
    if m.group(2) is not None:
        return (major, int(m.group(2))), 1
    if (_PLAIN_INT_RE.match(toks[i]) and i + 1 < len(toks)
            and _PLAIN_INT_RE.match(toks[i + 1])):
        return (major, int(toks[i + 1])), 2
    return (major, 0), 1


@lru_cache(maxsize=4096)
def _parse(low):
    m = _NAME_RE.search(low)
    if not m:
        return None, ()
    rest = low[m.end():]
    toks = _tokens(rest)
    if _LEGACY_RE.search(low):
        return None, tuple(toks)
    # Shape A: nemotron-<version>[-tier ...]
    got = _version_at(toks, 0)
    if got is not None:
        ver, used = got
        nxt = toks[used] if used < len(toks) else ""
        if _SIZE_RE.match(nxt):
            return None, tuple(toks)           # "nemotron-4-340b": legacy shape
        return ver, tuple(toks)
    # Shape B: nemotron-<tier>-<version>[-size ...]
    if toks and toks[0] in _TIER_WORDS:
        got = _version_at(toks, 1)
        if got is not None:
            return got[0], tuple(toks)
    return None, tuple(toks)


def generation(model_id):
    """The (major, minor) generation the id names, or None when it names none
    this module can read (a fine-tune, a parser, an unknown spelling)."""
    try:
        return _parse(str(model_id or "").strip().lower())[0]
    except Exception:                                            # noqa: BLE001
        return None


def is_nemotron(model_id):
    try:
        return bool(_NAME_RE.search(str(model_id or "").lower()))
    except Exception:                                            # noqa: BLE001
        return False


def is_non_chat(model_id):
    """True for a Nemotron utility model (safety classifier, parser, reward,
    embedder). Read from the words after 'nemotron' only, so a relay prefix can
    never trip it."""
    try:
        toks = _parse(str(model_id or "").strip().lower())[1]
        return any(t in NON_CHAT_WORDS for t in toks)
    except Exception:                                            # noqa: BLE001
        return False


def is_current_chat(model_id):
    """True when the id is a Nemotron of the current generation (>= ALLOWED_FROM)
    AND a chat model. False for anything older, unreadable or non-chat -- those
    keep the family's old last-resort handling."""
    try:
        low = str(model_id or "").strip().lower()
        ver, toks = _parse(low)
        if ver is None or ver < ALLOWED_FROM:
            return False
        return not any(t in NON_CHAT_WORDS for t in toks)
    except Exception:                                            # noqa: BLE001
        return False
