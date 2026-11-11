"""Image-generation models ordered by a public quality board.

Pure: stdlib only, no network, no import from the hub, never raises.

WHY. The hub's automatic image pick was "the first model of the first provider
in a fixed list". That is an order of providers, not of quality: with several
free image providers switched on, the model that answered was whichever one the
list put first, and the fallbacks were whichever came next. The owner asked for
the best available model by default.

THE NUMBERS are read from a real page and are never estimated:
Artificial Analysis, text-to-image arena (blind votes, Elo), read on the date
below. A model the board does not list has NO score here (None) -- it is never
given a guessed one, it simply sorts after every listed model, in the order the
caller already had. Old generations are not on the board at all (Stable
Diffusion 1.5 / XL / XL Lightning, DreamShaper, Seedream 4.0), which is itself
the board's verdict on them.

An id is matched by what it NAMES, whichever host spells it
("@cf/black-forest-labs/flux-1-schnell", "black-forest-labs/FLUX.1-schnell",
"togetherai:black-forest-labs/flux.1-schnell"). A bare alias that names no
model ("flux" on Pollinations' anonymous endpoint, "ai-image" on Puter) is not
matched: what serves it is the host's choice and is not stated.
"""
import re

SOURCE = "Artificial Analysis text-to-image arena (AA-Image-T2I v2.1)"
URL = "https://artificialanalysis.ai/image/leaderboard/text-to-image"
DATE = "2026-11-11"

# (pattern on the lower-cased id, the board's own name, arena score)
_ROWS = (
    (r"gpt-image-2(?![\d.])", "GPT Image 2 (high)", 1190),
    (r"gemini-3\.1-flash-image", "Nano Banana 2 (Gemini 3.1 Flash Image)", 1143),
    (r"gpt-image-1\.5", "GPT Image 1.5", 1125),
    (r"gemini-3-pro-image|nano-banana-pro", "Nano Banana Pro (Gemini 3 Pro Image)", 1120),
    (r"seedream-4\.5", "Seedream 4.5", 1030),
    (r"gpt-image-1(?![\d.]|-mini)", "GPT Image 1", 1016),
    (r"gemini-2\.5-flash-image", "Nano Banana (Gemini 2.5 Flash Image)", 987),
    (r"z-image-turbo", "Z-Image Turbo", 936),
    (r"gpt-image-1-mini", "GPT Image 1 Mini", 936),
    (r"kontext[/-]max", "FLUX.1 Kontext [max]", 931),
    (r"qwen-image(?![\w.-])", "Qwen Image", 906),
    (r"flux[.-]?1[.-]dev", "FLUX.1 [dev]", 820),
    (r"flux(?:[.-]?1)?[.-]schnell", "FLUX.1 [schnell]", 742),
)
_COMPILED = tuple((re.compile(p), name, score) for p, name, score in _ROWS)


def _norm(model_id):
    try:
        return str(model_id or "").strip().lower().replace("_", "-").replace(" ", "-")
    except Exception:
        return ""


def info(model_id):
    """{"score": int, "name": the board's name} for a model the board lists,
    else None. Never raises."""
    try:
        text = _norm(model_id)
        if not text:
            return None
        for rx, name, score in _COMPILED:
            if rx.search(text):
                return {"score": score, "name": name}
    except Exception:
        pass
    return None


def score(model_id):
    """The board's arena score for this model id, or None when it is not listed."""
    hit = info(model_id)
    return hit["score"] if hit else None


def rank(pairs):
    """`pairs` = [(provider_id, model_id), ...] in the caller's own order.
    Returns the same pairs: the listed models first, best score first, then the
    unlisted ones. The sort is stable, so equal scores and all unlisted models
    keep the order the caller gave them. Nothing is added or dropped; on any
    error the input comes back unchanged."""
    try:
        items = list(pairs)

        def key(pair):
            s = score(pair[1])
            return (0, -s) if s is not None else (1, 0)

        return sorted(items, key=key)
    except Exception:
        try:
            return list(pairs)
        except Exception:
            return pairs
