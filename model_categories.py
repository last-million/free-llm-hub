"""Calvoun Free LLM Hub -- what each model is GOOD AT, as one shared table.

ASKED 2026-09-01: "i want like buttons in settings for this part of models to
use and i want clique on uncensored and he select only those ones ... or all
models or the ones good for swarm agents ... and also our orchestrator should
know this to know which models to use ... but remember we want always best
models please and if not available use next one available in quality".

WHY IT LIVES HERE, not in the UI. The buttons and the router have to agree
about what "good for swarm agents" means, and the only way to guarantee that is
one definition both read. The Settings buttons write the user's choice into the
per-model allowlist (app._set_model_blocked), and routing already honours that
allowlist at a single seam (app._is_model_dead) -- so picking a category takes
effect everywhere at once: orchestration, the fallback chain and the swarm.

"ALWAYS THE BEST, ELSE THE NEXT BEST" needs no new machinery. A category is a
SET, not an order. Inside whatever set is enabled, the existing ranking picks
the strongest and the existing chain falls through to the next -- which is
exactly the behaviour asked for.

MATCHING is on substrings of the lowercased id, checked against BOTH the full
"provider/model" and the normalised model identity, so every provider's
spelling of one model lands in the same category (nvidia's "moonshotai/kimi-k3",
morph's "morph-kimik3" and g4f's "srv_x:moonshotai/kimi-k3" are one model).

EVERY MODEL NAMED BELOW WAS CHECKED against the live catalog on 2026-09-01
before being written down -- all 39 were found. Two honest notes on that check:

  - Reputation and the hub's own score often disagree, and the score is what
    routing uses. o3-2025 and grok-3 score 6.0 here, chat-model-reasoning 9.0,
    Cydonia 15.0, Hermes-3 28.8 -- all of them g4f relay listings the hub has
    no benchmark data for and therefore ranks low. Putting a model in a
    category decides that it is IN THE RUNNING; it does not promote it.
  - A model belongs to as many categories as fit. glm-5.3 is good at coding AND
    holds a long context; pretending otherwise would make the buttons lie.
"""
from __future__ import annotations

# (key, label, help, patterns)
#
# Patterns are substrings, not regexes: they are read and edited by people, and
# a regex here would be a foot-gun for whoever adds the next model family.
CATEGORIES = [
    (
        "swarm",
        "Swarm agents",
        "Orchestrators: strict JSON, reliable multi-turn tool calling, long "
        "system prompts. The ones that can actually drive an agent loop "
        "(BFCL-style function calling).",
        ("claude-sonnet", "gpt-4o", "gpt-5.2", "gpt-5.6-sol", "deepseek-v4-pro",
         "llama-3.3-70b", "gpt-oss", "nemotron-3", "gemini-3", "kimi-k3",
         "glm-5.3", "hy4"),
    ),
    (
        "coding",
        "Coding",
        "Real-world software work: SWE-bench-style fixes, big files, terminal "
        "and refactoring tasks.",
        ("claude-sonnet", "deepseek-v4-flash", "deepseek-v4-pro", "qwen3-coder",
         "codestral", "granite-34b-code", "gpt-5.6-sol", "gpt-5.2", "glm-5.3",
         "kimi-k3", "hy3", "hy4"),
    ),
    (
        "reasoning",
        "Heavy reasoning",
        "Thinkers for maths, logic and competitive programming (AIME/MATH). "
        "Good as validator agents over another model's work.",
        ("deepseek-r1", "o3-2025", "chat-model-reasoning", "qwq", "thinking",
         "deepseek-v4-pro", "reasoner"),
    ),
    (
        "context",
        "Long context",
        "Whole codebases, many PDFs, long log files. Needle-in-a-haystack "
        "recall at very large token counts.",
        ("gemini-3", "kimi-k3", "kimi-k2.6", "minimax-m3", "glm-5.3", "hy4"),
    ),
    (
        "vision",
        "Vision",
        "Screenshots, diagrams, charts and OCR. Needed for any agent that has "
        "to LOOK at something.",
        ("vl-plus", "-vl", "vision", "veo-", "vila", "gemini-3", "llava"),
    ),
    (
        "uncensored",
        "Uncensored / steerable",
        "Follows a system prompt without lecturing, and takes on work other "
        "models decline. The hub's own safety filter still applies on top.",
        ("hermes-3", "cydonia", "grok-", "qwen3.8", "hy3", "deepseek-v4-flash",
         "gpt-oss", "dolphin", "openhermes"),
    ),
    (
        "fast",
        "Fast / cheap",
        "Small and quick, for trivial steps done many times. Not for building "
        "anything.",
        ("flash", "mini", "lite", "nano", "-1b", "-2b", "-3b", "-7b", "-8b",
         "small", "turbo", "lfm-"),
    ),
    (
        "specialist",
        "Specialists",
        "Domain-tuned: medicine, finance, and live web search.",
        ("palmyra-med", "palmyra-fin", "sonar", "med-", "-fin-"),
    ),
]

CATEGORY_KEYS = tuple(k for k, _l, _h, _p in CATEGORIES)
_BY_KEY = {k: pats for k, _l, _h, pats in CATEGORIES}


def labels():
    """[(key, label, help)] for the UI, in display order."""
    return [(k, lab, helptext) for k, lab, helptext, _p in CATEGORIES]


def matches(key, provider, model, identity=None):
    """True when this (provider, model) belongs to category `key`.

    `identity` is app._normalize_model_identity(model) when the caller has it.
    Passing it is what makes one model land in the same category under every
    provider's spelling of its name."""
    pats = _BY_KEY.get(key)
    if not pats:
        return False
    hay = ("%s/%s" % (provider or "", model or "")).lower()
    ident = (identity or "").lower()
    return any(p in hay or (ident and p in ident) for p in pats)


def categories_for(provider, model, identity=None):
    """Every category this model belongs to."""
    return [k for k in CATEGORY_KEYS if matches(k, provider, model, identity)]
