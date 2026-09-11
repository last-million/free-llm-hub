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
        # REFRESHED 2026-09-11 against the LIVE fleet and current community
        # ranking. 67 of 156 live models matched no mode at all, including the
        # highest-scoring model on the fleet -- so picking a mode excluded the
        # best model for it. The names below exist here and answer.
        ("claude-sonnet", "claude-fable", "claude-haiku", "gpt-4o", "gpt-5.2",
         "gpt-5.6", "deepseek-v4-pro", "llama-3.3-70b", "gpt-oss",
         "nemotron-3", "gemini-3", "kimi-k3", "kimi-k2.6", "qwen3.6",
         "minimax-m3", "glm-5.3", "hy4"),
    ),
    (
        "coding",
        "Coding",
        "Real-world software work: SWE-bench-style fixes, big files, terminal "
        "and refactoring tasks.",
        # Community ranking, September 2026: Qwen 3.6 (1M context, reliable
        # tool use), Kimi K2.6 (long-horizon agent work), DeepSeek V4 and
        # GLM 5.x are the open-weight models people actually put on agentic
        # coding. qwen3.6-27b is the top-scoring live model here and matched
        # NOTHING before this.
        ("claude-sonnet", "claude-fable", "claude-haiku", "deepseek-v4",
         "qwen3-coder", "qwen3.6", "codestral", "codellama", "granite-34b-code",
         "mimo", "gpt-5.6", "gpt-5.2", "glm-5.3", "glm-5.2", "kimi-k3",
         "kimi-k2.6", "devstral", "hy3", "hy4"),
    ),
    (
        "reasoning",
        "Heavy reasoning",
        "Thinkers for maths, logic and competitive programming (AIME/MATH). "
        "Good as validator agents over another model's work.",
        ("deepseek-r1", "o3-2025", "chat-model-reasoning", "qwq", "thinking",
         "deepseek-v4-pro", "reasoner", "-r1", "inkling", "nemotron-3-ultra",
         "glm-5.2-thinking", "magistral"),
    ),
    (
        "context",
        "Long context",
        "Whole codebases, many PDFs, long log files. Needle-in-a-haystack "
        "recall at very large token counts.",
        # qwen3.6 carries the longest context in the open-weight class
        # (1M tokens) and llama-4-maverick is the other very-long-context
        # family the fleet actually serves.
        ("gemini-3", "kimi-k3", "kimi-k2.6", "minimax-m3", "qwen3.6",
         "llama-4-maverick", "glm-5.3", "hy4"),
    ),
    (
        "vision",
        "Vision",
        "Screenshots, diagrams, charts and OCR. Needed for any agent that has "
        "to LOOK at something.",
        # "-vl" already caught the Qwen-VL/InternVL spellings; the families
        # below are multimodal without carrying "vl" in the name, which is how
        # a live vision model ended up outside the vision mode.
        ("vl-plus", "-vl", "vision", "veo-", "vila", "gemini-3", "llava",
         "internvl", "qwen3-vl", "llama-4-maverick", "llama-4-scout",
         "gemma-4", "pixtral", "molmo"),
    ),
    (
        "uncensored",
        "Uncensored / steerable",
        "Follows a system prompt without lecturing, and takes on work other "
        "models decline. The hub's own safety filter still applies on top.",
        # USER-REPORTED 2026-09-05, from their own use of these models: glm
        # belongs here. It was the only one of the three they named that was
        # missing -- qwen3.8 (7 live ids) and deepseek-v4-flash (12) already
        # matched, and grok-4-fast simply happened to win the pick they saw.
        # "glm-5" rather than "glm": 5.2 and 5.3 are the steerable generation,
        # and a bare "glm" would also sweep in glm-4.6-thinking, which belongs
        # to `reasoning` and is a different claim about behaviour.
        ("hermes-3", "cydonia", "grok-", "qwen3.8", "hy3", "deepseek-v4-flash",
         "gpt-oss", "dolphin", "openhermes", "glm-5"),
    ),
    (
        "fast",
        "Fast / cheap",
        "Small and quick, for trivial steps done many times. Not for building "
        "anything.",
        # "-mini", NOT "mini": a bare "mini" is a substring of geMINI, so every
        # Gemini model on the fleet -- gemini-3.1-PRO included -- was being
        # sold as small and quick. Found by asking why a flagship was excluded
        # from a category that subtracts the cheap tier.
        ("flash", "-mini", "lite", "nano", "-1b", "-2b", "-3b", "-7b", "-8b",
         "small", "turbo", "lfm-", "haiku", "lightning", "micro", "-a4b"),
    ),
    (
        # REQUESTED 2026-09-11: "add category for SEO please, to select best
        # models in SEO that will use max thinking for best results".
        #
        # SEO work is not coding and not general chat: it is following a long,
        # detailed brief exactly -- target keyword, heading structure, word
        # count, internal links, tone -- while holding a lot of competitor page
        # data in context at once. Independent testing through 2026 puts Claude
        # first for precisely that (it adheres to a detailed brief more
        # consistently than the alternatives) with GPT-5 close behind, and
        # found that most models score below 73/100 on on-page signals from a
        # standard prompt -- which is the case FOR picking the model rather
        # than letting routing choose whatever is cheapest.
        #
        # "max thinking" is the other half of the ask, so the thinking and
        # reasoning variants are named here: an SEO brief is a planning job
        # before it is a writing job.
        "seo",
        "SEO / content",
        "Long-form content against a brief: keyword targets, heading "
        "structure, competitor pages and schema. Follows a detailed spec "
        "exactly, and thinks before it writes.",
        ("claude-sonnet", "claude-opus", "claude-fable", "gpt-5.6", "gpt-5.2",
         "gpt-4o", "thinking", "reasoner", "deepseek-v4-pro", "kimi-k3",
         "kimi-k2.6", "qwen3.6", "glm-5.3", "gemini-3", "magistral",
         # ...but never the cheap tier: "max thinking" is the whole request,
         # and a flash/lite/mini variant is the one thing it rules out.
         # "!-mini" for the same reason "-mini" is spelled that way above:
         # a bare "mini" would subtract every Gemini model from SEO, which is
         # exactly the family the category wants most.
         "!flash", "!lite", "!-mini", "!nano", "!-a4b", "!micro"),
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

    def _hit(pat):
        return pat in hay or (ident and pat in ident)

    # A LEADING "!" EXCLUDES. Some categories are a claim about a model's
    # BEHAVIOUR that a whole family does not share: "seo" asks for the
    # thinking tier, and a positive pattern like "glm-5.3" cannot help also
    # matching glm-5.3-flash, which is the opposite of what was asked for
    # ("select best models in SEO that will use max thinking"). Writing the
    # family once and subtracting the cheap tier is both shorter and truer
    # than enumerating every full-size spelling a provider might use.
    for pat in pats:
        if pat.startswith("!") and _hit(pat[1:]):
            return False
    return any(_hit(p) for p in pats if not p.startswith("!"))


def categories_for(provider, model, identity=None):
    """Every category this model belongs to."""
    return [k for k in CATEGORY_KEYS if matches(k, provider, model, identity)]
