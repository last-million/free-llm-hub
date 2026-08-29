"""
Calvoun Free LLM Hub — Provider Registry (source of truth).

Central catalog of BYOK (bring-your-own-key) providers that offer free-tier
models via an OpenAI-compatible API. Consumed by:
  - app.py (live /models discovery, gateway routing, dashboard API)
  - config.py callers (per-provider key/enabled/base_url storage)

Design notes:
  - Every provider here is OpenAI-compatible (chat/completions).
  - `signup_url` MUST be the correct page where a user creates a free API key.
  - `free_filter` tells discovery how to identify which of the provider's models
    are free (see FREE_FILTERS).
  - SAFETY: is_model_allowed() blocks uncensored / abliterated / NSFW / jailbreak
    models regardless of provider (pattern-based; mainstream ids never match).
  - is_free_model() re-checks a PINNED model id against a provider's free_filter
    so a paid model can't be smuggled into a free-tier slot.

Pure stdlib: only `re` and `typing`.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

# --------------------------------------------------------------------------- #
# Provider catalog
# --------------------------------------------------------------------------- #
# free_filter values:
#   'suffix_free' -> model id ends with ':free' (OpenRouter)
#   'pricing_zero'-> models_url row has zero prompt+completion price. Fails
#                    CLOSED without a live catalog, so it is also the honest
#                    encoding for "nothing here is free" (see `paid` below).
#   'all'         -> the whole listed catalog is usable on the free tier. Reads
#                    as unsafe but is CORRECT wherever the provider has no
#                    paid catalog to leak (cerebras/mistral/modelscope/
#                    ollama-cloud — each says why inline). Never use it on a
#                    provider that also sells models.
#   'family'      -> only ids matching `free_families` are free (substring,
#                    case-insensitive). Add `free_exact: True` to match the
#                    FULL id instead — needed when a paid id has a free id as
#                    its prefix (glm-4.7-flash vs the PAID glm-4.7-flashX),
#                    which substring matching structurally cannot express.
#
# `paid: True` = this provider has NO genuine free tier. It is the mechanism
# that keeps a provider out of free routing: is_free_model() then rejects every
# id, so live discovery yields nothing — which is why `paid` rows ALSO carry
# `default_free_models: []` (the discovery-failure fallback is served WITHOUT a
# free-ness re-check, so a non-empty list there would still be routed as free).
# Both halves are required. Explicit '<pid>/<model>' pins still work.
#
# Free-tier facts below were researched per provider against official docs and
# live catalogs (2026-07-15). Do NOT "tidy" a filter or model id from memory:
# roughly half of what looked obvious here was wrong, in the direction of
# billing the user. See quota.py FREE_LIMITS for the matching request budgets.
PROVIDERS: Dict[str, dict] = {
    "openrouter": {
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "models_url": "https://openrouter.ai/api/v1/models",
        "signup_url": "https://openrouter.ai/keys",
        "key_hint": "sk-or-...",
        # KEEP suffix_free — do NOT "upgrade" this to pricing_zero. 3 zero-priced
        # non-':free' models exist and 2 of them (google/lyria-3-*) bill PER
        # SONG/CLIP ($0.08/$0.04), a unit the prompt/completion pricing fields
        # don't model: they report 0 and would silently spend real money.
        # All 20 ':free' ids are zero across every pricing field — no false positives.
        "free_filter": "suffix_free",
        # RE-VERIFIED 2026-07-27 against the live /models catalog + a real 1-token
        # generation per id through the hub itself. Removed: llama-3.3-70b-instruct
        # (openrouter itself 503s "unavailable for free, use this slug instead"),
        # qwen3-next-80b-a3b-instruct/qwen3-coder (no longer in the live catalog at
        # all), hermes-3-llama-3.1-405b (same), gpt-oss-20b/gemma-4-31b-it (both
        # return a 200 with EMPTY content on openrouter specifically — the hub's own
        # cross-provider fallback was silently rescuing these onto sambanova, masking
        # that openrouter itself never actually answers them). Added the models that
        # verifiably produced real content THROUGH openrouter, incl. nemotron-3-ultra-
        # 550b-a55b — the same top-ranked model nvidia hosts, now also a real
        # openrouter candidate instead of nvidia being the only source of it.
        "default_free_models": [
            "nvidia/nemotron-3-ultra-550b-a55b:free",
            "nvidia/nemotron-3-super-120b-a12b:free",
            "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
            "nvidia/nemotron-3-nano-30b-a3b:free",
            "nvidia/nemotron-nano-12b-v2-vl:free",
            "google/gemma-4-26b-a4b-it:free",
            "poolside/laguna-s-2.1:free",
        ],
        # IMAGE GENERATION — PAID, "free": False so it never enters auto image
        # routing (see _image_candidates() in app.py): reachable only via an
        # explicit "openrouter/<id>" pin. Billed against the same paid credits
        # as any non-':free' chat model on this key.
        "image_models": [
            {"id": "bytedance-seed/seedream-4.5", "label": "Seedream 4.5",
             "text_in_image": "excellent", "free": False},
        ],
        "notes": "One key unlocks many models. Free = ids ending ':free' (always free, never billed against credits). 50 req/day TOTAL across all free models (1,000/day after a one-time $10 top-up).",
    },
    "groq": {
        "name": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "models_url": "https://api.groq.com/openai/v1/models",
        "signup_url": "https://console.groq.com/keys",
        "key_hint": "gsk_...",
        # 'family', not 'all': Groq exposes NO machine-readable free signal (no
        # ':free' suffix, no pricing field in /v1/models), and 'all' leaked
        # non-chat ids that hard-fail on /chat/completions (whisper STT, orpheus
        # TTS, llama-prompt-guard classifiers) plus kimi-k2-instruct-0905, which
        # is absent from the official Free Plan Limits table. Substrings are
        # deliberately precise: bare 'llama' would match llama-prompt-guard,
        # bare 'qwen3' would match enterprise-only qwen3-vl-32b.
        # Groq rotates models aggressively — re-validate this list periodically.
        # Re-validated 2026-08-29 against live /v1/models (14 ids). The llama-*
        # and qwen3-32b pins below now match nothing upstream; they are kept as
        # cheap insurance in case Groq restores them, since an unmatched
        # substring costs nothing. qwen3.8-27b was the real miss: pinning the
        # exact point version meant the 3.6 -> 3.8 refresh went invisible.
        "free_filter": "family",
        "free_families": ["llama-3.1-8b-instant", "llama-3.3-70b-versatile", "llama-4-scout",
                          "gpt-oss", "qwen3-32b", "qwen3.6-27b", "qwen3.8-27b",
                          "compound", "allam-2-7b"],
        # Fallback when live discovery fails — must list models that ACTUALLY
        # exist, or a network blip hands the router a set of guaranteed 404s.
        "default_free_models": [
            "openai/gpt-oss-120b", "openai/gpt-oss-20b",
            "qwen/qwen3.8-27b", "qwen/qwen3.6-27b",
            "groq/compound", "groq/compound-mini", "allam-2-7b",
        ],
        "notes": "Extremely fast. Free tier, no card. ~1,000 req/day per model.",
    },
    "cerebras": {
        "name": "Cerebras",
        "base_url": "https://api.cerebras.ai/v1",
        "models_url": "https://api.cerebras.ai/v1/models",
        "signup_url": "https://cloud.cerebras.ai/",
        "key_hint": "csk-...",
        # 'all' is CORRECT here and must stay, despite reading as unsafe: docs
        # state verbatim "All models on Cerebras public endpoints are free to
        # use, subject to rate limits" — there is no paid model that could leak.
        # (Free vs Developer is a rate-limit tier over the same 3 ids.) /v1/models
        # returns no pricing field, so pricing_zero is impossible anyway.
        "free_filter": "all",
        # gpt-oss-120b first: the only PRODUCTION-tier id (the other two are
        # PREVIEW and can be pulled with less notice).
        "default_free_models": ["gpt-oss-120b", "zai-glm-4.7", "gemma-4-31b"],
        "notes": "Fastest tokens/sec. Free: 5 req/min, 1M tok/day (no req/day cap is documented). Limits apply per ORG, not per user.",
    },
    "dahl": {
        "name": "Dahl Inference (Gonka)",
        "base_url": "https://inference.dahl.global/v1",
        # PUBLIC — 200 with no Authorization header. Returns only
        # id/object/created/owned_by: NO context_length field at all, so
        # _learn_ctx_from_catalog finds nothing here. Context is published on the
        # marketing page only (MiniMax 200K, Kimi 256K).
        "models_url": "https://inference.dahl.global/v1/models",
        "signup_url": "https://inference.dahl.global/account",
        "key_hint": "dahl_...",
        # NOT no_key (inference 401s "Missing API token" without a Bearer) and NOT
        # static_key either: every key owns its OWN private 100M-token pool, so a
        # key shared in this repo would burn one allowance for all users. The key
        # is free and instant — see KEY_MINT_URL below.
        "key_mint_url": "https://inference.dahl.global/tokens",
        "balance_url": "https://inference.dahl.global/tokens/current",
        # 'all' is CORRECT and must stay: docs state "There is no payment UI yet",
        # the entire 2-model catalog is served from the same free allowance, and
        # /v1/models carries no pricing field (pricing_zero cannot work).
        "free_filter": "all",
        # Both chat-verified with real content. Kimi first: already Tier S in
        # _BENCH_FAMILY and it returns clean content with thinking in a SEPARATE
        # `reasoning` field, while MiniMax leaks a raw <think> block into
        # `content` (harmless — _strip_thinking handles it — but noisier).
        # zai-org/GLM-5.2 is advertised on the site as "Soon" but 400s
        # 'unsupported model' today. Do NOT add it until it answers.
        "default_free_models": ["moonshotai/Kimi-K2.6", "MiniMaxAI/MiniMax-M2.7"],
        # NO vision_models on purpose: the catalog page badges both models
        # "Vision", but a data-URI image is SILENTLY DROPPED — 200 OK,
        # prompt_tokens counts the text only, and the model answers "I cannot see
        # an image". It fails unsafely, so we must not route images here.
        "notes": "Free 100,000,000 tokens PER KEY (not per month) on the decentralized Gonka network. OpenAI-compatible, streaming works. The key is instant and anonymous — no email, no card. When a key is spent it returns 402 'available tokens exhausted'; mint another. Sends NO rate-limit headers and publishes no per-minute/hour/day request cap (48 concurrent requests all returned 200), so the token pool is the only budget. Check the balance with GET /tokens/current. GLM-5.2 is listed as coming soon but 400s today. Kimi K2.6 256K ctx, MiniMax M2.7 200K ctx (marketing page figures — /v1/models exposes no context field).",
    },
    "nvidia": {
        "name": "NVIDIA NIM",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "models_url": "https://integrate.api.nvidia.com/v1/models",
        "signup_url": "https://build.nvidia.com/settings/api-keys",
        "key_hint": "nvapi-...",
        # 2026-07-24 RE-CLASSIFIED (owner decision, live-probed). Was
        # paid=True + pricing_zero, which made is_free_model() return False for
        # EVERY id (providers.py:1143) -> the hub reported "0 free models" and
        # never routed here. A full-catalog probe (97 chat ids) found
        # **39 WORKING** models and ZERO 402s: nemotron-3-ultra-550b-a55b,
        # deepseek-v4-pro/flash, llama-4-maverick, llama-3.3-70b, minimax-m3,
        # qwen3-next-80b, mistral-small-4-119b, step-3.7-flash, gpt-oss-20b...
        # So the credits are live and the capacity is real. trial=True keeps the
        # "Trial credits" badge so the finite/expiring balance stays visible, and
        # a real 402 ("Cloud credits expired") still sidelines the provider via
        # the normal authfail path — so enabling this cannot fail silently.
        # pricing_zero is impossible here (NVIDIA's /models returns no pricing),
        # hence "all" + an exclude list for the legacy ids that 404.
        "paid": False,
        "trial": True,  # finite 1,000-credit lifetime budget (90-day expiry); 402s once spent
        "free_filter": "all",
        "exclude_families": [
            # probed 2026-07-24: these 404 (retired) or are non-chat
            "yi-large", "fuyu", "jamba", "sea-lion", "starcoder", "dbrx",
            "codegemma", "deplot", "coder-6.7b", "bge", "embed", "rerank",
            "nemoguard", "nemoretriever", "ocr", "parakeet", "riva", "clip",
        ],
        "default_free_models": [
            "nvidia/nemotron-3-ultra-550b-a55b",
            "deepseek-ai/deepseek-v4-pro",
            "deepseek-ai/deepseek-v4-flash",
            "meta/llama-4-maverick-17b-128e-instruct",
            "meta/llama-3.3-70b-instruct",
            "qwen/qwen3-next-80b-a3b-instruct",
            "minimaxai/minimax-m3",
            "nvidia/nemotron-3-super-120b-a12b",
            "mistralai/mistral-small-4-119b-2603",
            "stepfun-ai/step-3.7-flash",
            "openai/gpt-oss-20b",
        ],
        "notes": "TRIAL, not a free tier — 1,000 lifetime credits (max 5,000 via business email), 90-day expiry, then HTTP 402 'Cloud credits expired'. Not renewable: every remote call to an NVIDIA-hosted endpoint spends the balance. Self-hosting the NIM containers is separately free for Developer Program members.",
    },
    "morph": {
        "name": "Morph",
        "base_url": "https://api.morphllm.com/v1",
        "models_url": "https://api.morphllm.com/v1/models",
        "signup_url": "https://morphllm.com/dashboard",
        "key_hint": "sk-...",
        # 2026-07-24 RE-CLASSIFIED (same bug as nvidia): paid=True made
        # is_free_model() reject every id, so the hub showed "0 free models"
        # even though a live probe answered 200 on TEN of them (morph-v3-large,
        # morph-v3-fast, auto, morph-compactor, morph-minimax3-428b,
        # morph-minimax27-230b, morph-gemma4-31b, morph-dsv4flash,
        # deepseek/deepseek-v4-flash[-20260423]). The monthly TOKEN allowance is
        # partly spent (3 ids already 402 "Monthly quota exceeded"), so this is
        # genuinely finite — trial=True keeps that visible and a real 402 still
        # sidelines the provider. pricing_zero yields [] here (no pricing field).
        "paid": False,
        "trial": True,  # 250K tokens/month, token-metered; 402s when spent
        "free_filter": "all",
        "exclude_families": ["computer-use", "warp-grep", "embed", "rerank"],
        # 2026-08-01 RE-PROBED on a fresh key: the catalog grew from 11 ids to 17
        # and ALL TEN general-purpose ones answered 200. Ordered best-first by the
        # hub's ranking, so a chain built from this list opens with Kimi K3.
        # NOTE the quota is PER KEY and monthly: a spent key 402s "Monthly quota
        # exceeded" on EVERY id at once (verified — key #0 402'd on all four
        # probes while key #1 answered all four). 402 is in _KEY_ROTATE_STATUSES,
        # so the hub rotates to the next key on its own.
        # Left out on purpose: "auto" (a router alias, not a model),
        # "morph-compactor" (context-compaction specialist), and the
        # computer-use / warp-grep ids (already in exclude_families).
        "default_free_models": [
            "morph-kimik3",           # Kimi K3 — top tier after gpt-5/claude
            "morph-glm52-744b",       # GLM 5.2
            "morph-kimik3-fast",
            "morph-qwen36-27b",
            "morph-qwen35-397b",
            "morph-minimax3-428b",    # MiniMax 3
            "morph-minimax27-230b",
            "morph-dsv4flash",
            "deepseek/deepseek-v4-flash",
            "morph-gemma4-31b",
            # Morph's own FAST-APPLY edit models. They answer normal chat, but
            # they are specialists for applying a diff to a file, so they sort
            # last and should never be the opening hop for a general question.
            "morph-v3-large",
            "morph-v3-fast",
        ],
        "notes": "No free models — all 8 models bill per token. The '200 req free every month' headline actually meters TOKENS ($2.50 / 250K credits per month): a coding CLI's 20-50K-token turns make the real allowance ~5-12 requests/month.",
    },
    "nararouter": {
        "name": "NaraRouter",
        "base_url": "https://router.bynara.id/v1",
        "models_url": "https://router.bynara.id/v1/models",
        "signup_url": "https://router.bynara.id/register",
        "key_hint": "sk-nry-...",
        # DO NOT restore free_filter 'all' here. It was set on the theory that
        # NaraRouter sells fixed-price SUBSCRIPTIONS, so an out-of-tier model
        # would error rather than bill. Probing all 35 ids with a real key on
        # 2026-07-15 disproved that outright - it is PAY-PER-TOKEN off a credit
        # balance, and says so itself:
        #   402 "Insufficient credits: your balance is 0.000000000 but this
        #        request needs about 0.189000000. Top up to continue"
        # So this provider DOES sell models, which is exactly the case the
        # header rule forbids 'all' for. Only 4/35 ids answered on the free
        # tier; the other 31 (claude-opus-4.8, claude-sonnet-5, gpt-5.5/5.6-*,
        # gemini-3.5-flash, grok-4.5, deepseek-v4-*, qwen3.7-max ...) all
        # demanded a top-up. With a 0 balance they merely refuse - but the day
        # a balance exists, 'all' would spend it on Opus. Hence exact pins.
        #
        # free_exact: substring matching cannot express this catalog safely -
        # 'mistral-large' is free while 'minimax-m3' is not, and note that
        # 'glm-5.2-free' is NOT free despite the name (429, top-up required).
        # Never infer free-ness from an id here; re-probe with a key instead.
        "free_filter": "family",
        "free_exact": True,
        # ACCOUNT GATE RE-LOCKED, emptied 2026-07-31. Every endpoint (/v1/models,
        # /v1/me, /v1/usage, and completions on all three ids below) now answers:
        #   HTTP 403 {"type":"forbidden","message":"telegram_required: Join the
        #     required Telegram group/channel and relink at /settings to continue."}
        # The KEY IS STILL VALID — a bogus key returns 401 "A valid API key is
        # required", this one returns 403 — so it is a per-account human step,
        # not an outage and not a bad credential. Nothing the hub does can clear
        # it, so these ids only burned one guaranteed 403 hop per request.
        # TO RESTORE: rejoin the Telegram channel, relink at
        # router.bynara.id/settings, then move these three ids back up.
        "free_families": [],
        "_free_families_pending_telegram_relink": [
            "agnes-2.0-flash",      # verified 200, 7.5s
            "mistral-large",        # verified 200, 2.1s
            "mistral-medium-3-5",   # verified 200, 1.8s
            # "tencent-hy3",        # PULLED 2026-07-24: now 402 "Insufficient
            #   credits. Please top up your balance." hy3 carries the +135
            #   preference floor (_PREF_FLOORS), so it was tried FIRST on every
            #   agentic request and 402'd every time — burning the top slot of
            #   the chain and pushing Codex down to cerebras/gpt-oss-120b on
            #   every turn. Re-enable the moment the NaraRouter balance is
            #   topped up (it answered 200 in 4.5s when it had credit).
        ],
        # Same 4 ids: this list is served WITHOUT a free-ness re-check when live
        # discovery fails, so it must never contain an unverified id.
        # kimi-k2.7-code-free is advertised as free but is BROKEN upstream, not
        # merely slow: 3 probes, each ~126s, all Cloudflare 524 (origin timeout).
        # Re-probe before ever pinning it; a hang is not a free-ness verdict.
        # Emptied 2026-07-31 together with free_families above — the account's
        # Telegram link has lapsed, so all three 403 "telegram_required" on every
        # call. Restore both lists once relinked (see the comment above).
        "default_free_models": [],
        "_default_free_models_pending_telegram_relink": [
            "agnes-2.0-flash",
            "mistral-large",
            "mistral-medium-3-5",
            # "tencent-hy3",  # PULLED 2026-07-24 - 402 insufficient credits (see above)
        ],
        "notes": ("SETUP - the key does NOT work until you do these 2 steps (verified live 2026-07-15). "
                  "STEP 1: link your Telegram account at router.bynara.id/settings. "
                  "STEP 2: follow/join their Telegram channel. "
                  "Then the API key starts working - order matters, and a key you minted before "
                  "finishing both stays blocked until you relink. Until then EVERY endpoint "
                  "(/v1/models, /v1/chat/completions, /v1/usage, /v1/me) returns "
                  "403 'telegram_required' and this provider just stays unused (fail-safe, "
                  "nothing else in the hub breaks). "
                  "That gate is also why this is a bonus tier, never a dependency: leave the "
                  "channel and your key can die. "
                  "Free plan: 6M tokens/DAY (resets 07:00 WIB) + 10 req/min. Free forever, "
                  "no credit card, OpenAI AND Anthropic compatible. "
                  "FREE MODELS (probed with a real key 2026-07-15, only 4 of the 35 listed "
                  "actually answer): agnes-2.0-flash, mistral-large, mistral-medium-3-5, "
                  "tencent-hy3. The catalog also LISTS claude-opus-4.8, claude-sonnet-5, "
                  "gpt-5.5/5.6, gemini-3.5-flash, grok-4.5 etc - those are PAID and refuse "
                  "with 402/429 'insufficient credits' until you top up. Even 'glm-5.2-free' "
                  "is paid despite its name. The hub only routes the 4 verified ids. "
                  "NOTE the viral '7M tokens/day, 30+ models free' claim is marketing - and "
                  "even the '~5 free models' on the pricing page measured as 4. "
                  "BILLING: this is pay-per-token off a credit balance (NOT a flat "
                  "subscription). At balance 0 paid ids simply refuse, so nothing can be "
                  "charged silently - but if you ever top up, only the 4 pinned ids stay free. "
                  "⚠ TRUST: this is a reseller, not an inference provider - it lists "
                  "'Claude Fams' family-plan access and 8 'Out of stock' tiers (an API does "
                  "not run out of stock unless it pools accounts), uses rebranded ids "
                  "(DeepSeek V4 Pro byNara), and is Telegram-supported. The curated MIT list "
                  "cheahjs/free-llm-api-resources - which excludes non-legitimate services - "
                  "does NOT list it. Could disappear without notice; keep it as a bonus tier, "
                  "never a dependency. Model ids are discovered live once you add a key."),
    },
    "pollinations": {
        "name": "Pollinations.AI",
        "base_url": "https://text.pollinations.ai/openai",
        "models_url": "https://text.pollinations.ai/models",
        "signup_url": "https://enter.pollinations.ai",
        "key_hint": "(no key needed)",
        "no_key": True,   # anonymous tier: no signup, no API key, no card
        # 'family' pinned to the one verified anonymous-tier model, NOT 'all':
        # Pollinations DOES run a large paid catalog, and pinning keeps this row
        # safe even if the legacy endpoint later gains paid entries.
        "free_filter": "family",
        "free_families": ["openai-fast"],
        "default_free_models": ["openai-fast"],
        # IMAGE GENERATION — separate anonymous GET-URL image API (NOT chat),
        # image.pollinations.ai/prompt/{prompt}, no key required. Only the two
        # genuinely-free models are listed; nanobanana/gptimage/seedream need
        # paid Pollen credits and are deliberately excluded so "free" stays true
        # (cross-checked against the shipped SEO Quantum Pro image registry).
        "image_models": [
            {"id": "flux", "label": "FLUX", "text_in_image": "medium"},
            {"id": "turbo", "label": "Turbo", "text_in_image": "medium"},
        ],
        "notes": ("Anonymous tier: NO key, NO signup, NO card. LIVE-VERIFIED — POST "
                  "text.pollinations.ai/openai/chat/completions returns 200 with "
                  "user_tier:anonymous, served by openai-fast (GPT-OSS 20B). Leak test "
                  "PASSED: claude/gpt-5/grok all 404 here, so a paid model is structurally "
                  "unreachable and a surprise bill is impossible. Documented rate is 1 req "
                  "per 15s (a burst test saw no 429, but the published figure is used). "
                  "DO NOT switch to gen.pollinations.ai/v1 — that catalog is 186 models "
                  "ALL priced in consumable 'pollen' with 402 PAYMENT_REQUIRED: exactly the "
                  "free-until-the-credits-burn pattern that produced 13 bad providers here. "
                  "GOTCHA: it 403s python-urllib's default User-Agent — the hub uses "
                  "`requests`, which gets a clean 200, so this only bites hand-rolled tests."),
    },
    "aihorde": {
        "name": "AI Horde",
        "base_url": "https://aihorde.net/api",
        "models_url": None,
        "signup_url": "https://aihorde.net/register",
        "key_hint": "(optional — anonymous key 0000000000 used automatically if you skip this)",
        "no_key": True,   # anonymous tier: no signup, no API key, no card required to use it
        "free_filter": "pricing_zero",
        "default_free_models": [],
        # IMAGE GENERATION ONLY -- community-run, GPU-donated distributed
        # inference network (formerly "Stable Horde"). LIVE-VERIFIED
        # 2026-07-27: submitted a real job with the public anonymous key
        # "0000000000", watched queue_position genuinely advance, downloaded
        # the completed generation, hex-dumped it -> real WebP file
        # (RIFF....WEBP header), zero payment, zero registration. Anonymous
        # requests sit at the BACK of the queue (real test: ~24min initial
        # ETA for a 512x512/20-step job, though it finished faster in
        # practice) -- a free personal account/key (signup_url above) jumps
        # priority dramatically and is worth doing if this becomes a
        # regular fallback, but is not required for it to work at all.
        "image_models": [
            {"id": "stable_diffusion", "label": "Stable Diffusion (AI Horde)",
             "text_in_image": "medium"},
        ],
        "notes": ("Volunteer GPU network, not a company -- no SLA, quality/speed depends on "
                  "which workers are online right now. Genuinely free and keyless (fixed "
                  "public anonymous key, not per-user). Register a free account for a "
                  "personal key to skip the anonymous queue's low priority."),
    },
    "cloudflare": {
        "name": "Cloudflare Workers AI",
        # ACCOUNT-SCOPED: this template is documentation, not a usable URL. The
        # user MUST paste their resolved base into the card's "Advanced: custom
        # base URL" field (base_url_for honors it). Until they do, calls fail.
        "base_url": "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
        # Cloudflare's model list is NOT OpenAI-shaped (returns a CF envelope, and
        # the OpenAI-compat base exposes no /v1/models at all), so live discovery
        # can't parse it and falls back to default_free_models below. That is fine.
        # Kept templated (not None) so it stays documented; app.py fills {account_id}.
        "models_url": None,
        "signup_url": "https://dash.cloudflare.com/sign-up",
        "key_hint": "Cloudflare API token (Workers AI scope)",
        # Families widened from the maintained MIT list cheahjs/free-llm-api-resources:
        # the earlier @cf/meta + @cf/openai pair silently excluded most of the free
        # catalog (gemma, granite, kimi, nemotron, qwen, glm, sea-lion).
        # DO NOT enable live discovery here without fixing BOTH problems below.
        # 1) Shape: CF returns {"result":[...]} - _parse_model_ids only reads
        #    "data"/"models" - and each row's "id" is a UUID while the real model
        #    id is in "name", so the parser would pin UUIDs.
        # 2) LEAK: free_filter 'family' includes "@cf/zai-org", which matches the
        #    PAID @cf/zai-org/glm-5.2 as well as the free glm-4.7-flash. Live
        #    discovery would route a paid model as free - the exact bug class that
        #    produced the 13 bad rows here. Family matching cannot express this
        #    catalog; only exact pins can.
        "free_filter": "family",
        "free_exact": True,
        # Probed 2026-07-15 against the real catalog (GET /ai/models/search: 61
        # models, 26 Text Generation) with a live key. 24/26 answered; the list
        # below is the 22 that are actually USABLE for chat, fastest first.
        # Excluded on evidence, do not "restore" them:
        #   @cf/zai-org/glm-5.2                   403 "not available on this plan" (PAID)
        #   @cf/meta/llama-3.2-11b-vision-instruct 403 needs a Model Agreement accepted
        #   @cf/meta/llama-guard-3-8b             a MODERATION model - replies "safe",
        #                                         not chat (same trap as groq prompt-guard)
        #   @cf/meta-llama/llama-2-7b-chat-hf-lora 51s and returns gibberish
        # NOTE the previously-shipped "@cf/meta/llama-3.1-8b-instruct" DOES NOT
        # EXIST - the real id is "-fp8". It was a guess and it was wrong.
        "free_families": [
            "@cf/meta/llama-3.2-3b-instruct",
            "@cf/mistral/mistral-7b-instruct-v0.2-lora",
            "@cf/meta/llama-4-scout-17b-16e-instruct",      # VISION, 0.7s
            "@cf/google/gemma-2b-it-lora",
            "@cf/qwen/qwen2.5-coder-32b-instruct",
            "@cf/aisingapore/gemma-sea-lion-v4-27b-it",
            "@cf/openai/gpt-oss-20b",
            "@cf/meta/llama-3.1-8b-instruct-fp8",
            "@cf/meta/llama-3.2-1b-instruct",
            "@cf/google/gemma-4-26b-a4b-it",
            "@cf/ibm-granite/granite-4.0-h-micro",
            "@cf/nvidia/nemotron-3-120b-a12b",
            "@cf/mistralai/mistral-small-3.1-24b-instruct",
            "@cf/openai/gpt-oss-120b",
            "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
            "@cf/qwen/qwen3-30b-a3b-fp8",
            "@cf/moonshotai/kimi-k2.7-code",
            "@cf/moonshotai/kimi-k2.6",
            "@cf/google/gemma-7b-it-lora",
            "@cf/zai-org/glm-4.7-flash",
            "@cf/qwen/qwq-32b",                             # reasoning, slow
            "@cf/deepseek-ai/deepseek-r1-distill-qwen-32b",  # reasoning, slow
        ],
        "default_free_models": [
            "@cf/meta/llama-3.2-3b-instruct",
            "@cf/mistral/mistral-7b-instruct-v0.2-lora",
            "@cf/meta/llama-4-scout-17b-16e-instruct",
            "@cf/google/gemma-2b-it-lora",
            "@cf/qwen/qwen2.5-coder-32b-instruct",
            "@cf/aisingapore/gemma-sea-lion-v4-27b-it",
            "@cf/openai/gpt-oss-20b",
            "@cf/meta/llama-3.1-8b-instruct-fp8",
            "@cf/meta/llama-3.2-1b-instruct",
            "@cf/google/gemma-4-26b-a4b-it",
            "@cf/ibm-granite/granite-4.0-h-micro",
            "@cf/nvidia/nemotron-3-120b-a12b",
            "@cf/mistralai/mistral-small-3.1-24b-instruct",
            "@cf/openai/gpt-oss-120b",
            "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
            "@cf/qwen/qwen3-30b-a3b-fp8",
            "@cf/moonshotai/kimi-k2.7-code",
            "@cf/moonshotai/kimi-k2.6",
            "@cf/google/gemma-7b-it-lora",
            "@cf/zai-org/glm-4.7-flash",
            "@cf/qwen/qwq-32b",
            "@cf/deepseek-ai/deepseek-r1-distill-qwen-32b",
        ],
        # Multimodal routing is deliberately fail-closed. Only image-capable
        # chat ids that were verified end-to-end belong in this exact list.
        "vision_models": ["@cf/meta/llama-4-scout-17b-16e-instruct"],
        # IMAGE GENERATION (text-to-image, not chat). Separate REST surface from
        # the OpenAI-compat chat path: POST .../accounts/{account_id}/ai/run/{id}
        # (native Cloudflare shape, not /chat/completions). Ids cross-verified
        # against the already-shipped, production Cloudflare image path in the
        # main SEO Quantum Pro app (services/image_provider_registry.py +
        # image_generation_service.py) rather than re-derived from scratch.
        # Shares the SAME 10k-Neurons/day budget as chat — quota.record("cloudflare", ...)
        # is deliberately reused as-is (not a separate image quota) because the
        # underlying account-level budget really is one shared pool.
        "image_models": [
            {"id": "@cf/black-forest-labs/flux-1-schnell", "label": "FLUX.1 Schnell",
             "text_in_image": "medium", "notes": "fast 4-step, short text only"},
            {"id": "@cf/bytedance/stable-diffusion-xl-lightning", "label": "SDXL Lightning",
             "text_in_image": "poor"},
            {"id": "@cf/stabilityai/stable-diffusion-xl-base-1.0", "label": "Stable Diffusion XL",
             "text_in_image": "poor"},
            {"id": "@cf/lykon/dreamshaper-8-lcm", "label": "DreamShaper 8 LCM",
             "text_in_image": "poor"},
        ],
        "notes": ("SAFE-FREE: 10,000 Neurons/day, reset 00:00 UTC. On the Workers FREE plan "
                  "the allocation is a HARD CAP — exceeding it fails with an error, it does "
                  "NOT bill (Workers Paid bills $0.011/1k Neurons past it). Free plan is the "
                  "default, no card for the first call. "
                  "SETUP: just paste a Workers-AI-scoped API token — the hub resolves your "
                  "account id from the token itself (GET /client/v4/accounts) and fills in the "
                  "account-scoped base URL for you. If that lookup fails (token too narrowly "
                  "scoped), paste https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/ai/v1 "
                  "into 'Advanced: custom base URL' instead. "
                  "Quota is denominated in NEURONS (varies per model), not requests, so the "
                  "hub tracks it as UNKNOWN rather than inventing a request count — a fixed "
                  "request cap CANNOT honestly represent it (cost/request varies ~14x by model "
                  "and with every prompt length; one long glm-5.2 completion could eat 80% of "
                  "the day). WHAT 10k NEURONS ACTUALLY BUYS (computed from CF's published "
                  "per-model rates, docs dateModified 2026-07-08, on a 300-in/500-out turn): "
                  "~1,800 req/day granite-4.0-h-micro (cheapest) | ~535 llama-3.1-8b-fp8-fast | "
                  "~524 gpt-oss-20b | ~229 gpt-oss-120b | ~221 plain llama-3.1-8b. "
                  "COUNTERINTUITIVE: neurons track GPU compute, NOT parameter count — plain "
                  "llama-3.1-8b costs MORE per output token (75,147/M) than the 120B "
                  "gpt-oss-120b (68,182/M). Prefer the -fp8/-awq variants; that is why the "
                  "pinned list carries llama-3.1-8b-instruct-fp8, not the bare id. "
                  "Also a separate 300 req/MINUTE text-gen cap, which never binds in practice "
                  "(the daily neuron budget runs out first). No official REST endpoint exists "
                  "to read remaining neurons — dashboard only. "
                  "⚠ CF's docs claim no model is plan-gated; our LIVE probe disproves that "
                  "(glm-5.2 -> 403 'not available on this plan'). Trust the probe, not the docs. "
                  "Model ids verified by probing the real catalog with a live key, not read off "
                  "the docs index (which renders bare slugs without the @cf/ prefix)."),
    },
    "google": {
        "name": "Google Gemini (AI Studio)",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "models_url": "https://generativelanguage.googleapis.com/v1beta/openai/models",
        "signup_url": "https://aistudio.google.com/apikey",
        "key_hint": "AIza...",
        # gemini-2.5-pro IS "Free of charge" in Google's own pricing HTML (the
        # third-party claim that Pro left the free tier in Apr 2026 is FALSE) —
        # it's the one free model worth routing hard tasks to. 'flash-lite' was
        # dead weight (already a substring of 'flash'); 'gemma-3-27b-it' is stale.
        # KNOWN GAP: substring families can't close this alone — 'flash' also
        # matches paid/unavailable-on-free *-image, omni-*, *-live, *-audio ids
        # and '2.5-pro' matches gemini-2.5-pro-preview-tts. pricing_zero is
        # impossible (the OpenAI-compat models endpoint returns no pricing), so
        # default_free_models is the real safety net; a per-provider exclude
        # list is the proper fix.
        #
        # RETIRED-BUT-STILL-LISTED (probed live 2026-07-15): Google's own
        # /v1beta/openai/models STILL RETURNS gemini-2.5-flash and
        # gemini-2.5-flash-lite, but calling either gives
        #   404 "This model models/gemini-2.5-flash is no longer available to
        #        new users."
        # So live discovery advertises models that can never work. Without this
        # exclude the family match on "flash" re-admits them every cycle: each
        # gets tried, 404s, is sidelined by the dead-model tracker for 6h, then
        # re-probed forever - burning a request every 6h per model, and showing
        # phantom models in the dashboard. One substring covers both ("-lite"
        # contains the base id as a prefix) and matches nothing we want:
        # gemini-3.5-flash / 3.1-flash-lite / 2.5-pro are unaffected.
        # The dead-tracker is the safety net, NOT the fix - it only reacts after
        # a wasted call.
        #
        # RE-PROBED 2026-07-24 (live, real key) — the note above about 2.5-pro
        # being free is NOW STALE. Google's API answers with an explicit
        # `limit: 0` free-tier quota for these ids, i.e. NO free tier at all
        # (not "quota spent"): gemini-2.5-pro, gemini-2.0-flash(-001),
        # gemini-2.0-flash-lite(-001), gemini-2.5-computer-use, gemini-3-pro-
        # preview, gemini-3.1-pro-preview, gemini-omni-flash-preview,
        # gemini-pro-latest, lyria-*, nano-banana-pro. Because "flash" also
        # matches gemini-2.0-flash* and "2.5-pro" whitelisted a dead id, the
        # router kept picking models that can NEVER serve free -> every pick
        # burned a 429 and looked like "Gemini free tier is broken".
        # CONFIRMED-FREE on the same probe (HTTP 200): gemini-3-flash-preview,
        # gemini-3.1-flash-lite(-preview), gemini-3.5-flash-lite,
        # gemini-flash-lite-latest, gemma-4-26b-a4b-it, gemma-4-31b-it, plus
        # gemini-3.5-flash / 3.6-flash / gemini-flash-latest (reasoning models:
        # answer once max_tokens is large enough).
        # Google no longer PUBLISHES per-model free RPM/RPD (docs defer to AI
        # Studio), so limits stay dynamically learned from real 429 headers
        # rather than hardcoded to an invented number.
        "exclude_families": [
            "gemini-2.5-flash",   # retired for new users (404)
            "gemini-2.0",         # limit: 0 - no free tier
            "gemini-2.5-pro",     # limit: 0 - no free tier (was free, no longer)
            "-pro-preview",       # gemini-3/3.1-pro-preview: limit: 0
            "pro-latest",         # gemini-pro-latest: limit: 0
            "omni",               # gemini-omni-flash-preview: limit: 0
            "computer-use",       # limit: 0
            "lyria", "nano-banana",
        ],
        "free_filter": "family",
        "free_families": ["flash", "gemma"],
        # Probed 2026-07-15: 6 answered AND all read text out of a test PNG (real
        # vision) -- gemini-2.0-flash returned 429 THEN, read as "free quota spent
        # by probing, not broken". Superseded 2026-07-26 by a full sweep sending a
        # REAL generation request: gemini-2.5-pro and gemini-2.0-flash both 429
        # "You exceeded your current quota" on a fresh key with no prior probing
        # this session -- i.e. a genuine no-free-tier limit:0, not exhaustion. This
        # matches exclude_families below (added later, from the same evidence) --
        # that list correctly EXCLUDED them from discovery, but nobody had gone
        # back to scrub these two hand-pinned defaults, so routing/testing could
        # still pick a model the SAME registry entry documents as dead.
        "default_free_models": [
            "gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-3-flash-preview",
            "gemma-4-31b-it",
        ],
        "vision_models": [
            "gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-3-flash-preview",
            "gemma-4-31b-it",
        ],
        # IMAGE GENERATION — PAID, "free": False so these never enter auto image
        # routing (see _image_candidates() in app.py's per-model "free" filter):
        # reachable only via an explicit "google/<id>" pin. Google's own
        # pricing page confirms no free tier for image generation (2026-07).
        "image_models": [
            {"id": "gemini-3.1-flash-image", "label": "Nano Banana 2",
             "text_in_image": "excellent", "free": False,
             "notes": "PAID -- Google's own pricing page confirms no free tier for image generation (verified 2026-07)."},
            {"id": "gemini-3-pro-image-preview", "label": "Nano Banana Pro",
             "text_in_image": "excellent", "free": False},
        ],
        "notes": ("Free tier = Flash family + Gemma + 2.5-Pro. Best free VISION in the fleet "
                  "(verified: all 6 read text from an image). ⚠ gemini-2.5-flash and "
                  "gemini-2.5-flash-lite are RETIRED ('no longer available to new users') yet "
                  "Google still lists them - excluded above, do not re-add from the /models list. "
                  "ToS: free-tier prompts/responses may be used to improve Google's products "
                  "outside EU/UK/CH."),
    },
    "mistral": {
        "name": "Mistral",
        "base_url": "https://api.mistral.ai/v1",
        "models_url": "https://api.mistral.ai/v1/models",
        "signup_url": "https://console.mistral.ai/api-keys/",
        "key_hint": "...",
        # 'all' is CORRECT: "Free mode" is a rate-limit tier over the WHOLE
        # catalog, not a model subset, so there is no free/paid split to leak
        # across. (Caveat: 'all' also surfaces non-chat ids — mistral-embed,
        # mistral-ocr-*, mistral-moderation-*, voxtral-* — but that's a
        # chat-capability concern, handled by filter_models(), not free-ness.)
        "free_filter": "all",
        # All '-latest' aliases ON PURPOSE: the previous pinned 'open-mistral-nemo'
        # RETIRES 2026-07-31 and sat in this discovery-failure fallback, i.e. it
        # would have broken exactly in the scenario the fallback exists for.
        # Aliases can't rot the same way. (Mistral names Ministral 3 8B as nemo's
        # replacement.)
        "default_free_models": [
            "mistral-small-latest", "mistral-medium-latest", "mistral-large-latest",
            "ministral-8b-latest", "ministral-3b-latest", "codestral-latest",
        ],
        "notes": "Free mode (the default plan) = $0 access to the full catalog, no card. Requires phone verification; requests may be used to train Mistral's models unless you opt out (Settings -> Privacy). Limits are per-org and unpublished (Admin Console -> Limits).",
    },
    "sambanova": {
        "name": "SambaNova Cloud",
        "base_url": "https://api.sambanova.ai/v1",
        "models_url": "https://api.sambanova.ai/v1/models",
        "signup_url": "https://cloud.sambanova.ai/apis",
        "key_hint": "...",
        # PROBED 2026-07-24 with a real key: 402 "A payment method is required" —
        # the free tier below is NOT being applied on a real account, so the only
        # thing this key ever bought was the one-time $5 Developer credit. Flagged
        # trial so the UI warns before a user relies on it. NB this contradicts the
        # researched `notes` (and quota.py's 20/day row), which are deliberately
        # left intact: the doc says one thing, the live 402 says another.
        "trial": True,
        # 'family', not 'all': 'all' leaked MiniMax-M2.7, which is in the catalog
        # and the Developer-tier limits table but deliberately ABSENT from the
        # Free-tier table — it fails on a card-less account. These 4 families
        # select exactly the 5 documented free models.
        # pricing_zero is a TRAP here: /v1/models DOES return a pricing object,
        # but the prices are non-zero for every model INCLUDING the free ones
        # (it's a rate card, not a free marker) — free-ness is account-level
        # (no payment method linked), so pricing_zero would silently yield [].
        "free_filter": "family",
        "free_families": ["DeepSeek", "Meta-Llama", "gpt-oss", "gemma"],
        "default_free_models": [
            "Meta-Llama-3.3-70B-Instruct", "DeepSeek-V3.1", "gpt-oss-120b",
            "DeepSeek-V3.2", "gemma-4-31B-it",
        ],
        "notes": "Genuinely free tier — applied automatically while no payment method is linked (nothing is consumed, it doesn't expire). 20 req/min, 20 req/day, 200k tokens/day. The separate $5 Developer credit is a trial, not this.",
    },
    "huggingface": {
        "name": "HuggingFace Router",
        "base_url": "https://router.huggingface.co/v1",
        "models_url": "https://router.huggingface.co/v1/models",
        "signup_url": "https://huggingface.co/settings/tokens",
        "key_hint": "hf_...",
        "paid": True,  # credit allowance, NOT a free tier — keep OUT of free routing
        "trial": True,  # quota.py FREE_LIMITS huggingface limit:0 — $0.10/MONTH of credits, not a request tier; PROBED 2026-07-24: 402 "You have depleted your monthly included credits"
        # Live router catalog: is_free:true matches EXACTLY 0 of 102 models, so
        # 'all' was admitting 100% paid inventory as free. NOTE for any future
        # pricing_zero implementation: HF nests pricing PER PROVIDER
        # (data[].providers[].pricing), and ships an explicit is_free boolean
        # that outranks pricing==0.
        "free_filter": "pricing_zero",
        "default_free_models": [],
        "notes": "No free models — a $0.10/month credit allowance consumed at full pay-as-you-go rates (~17 requests on GLM-5.2, ~1,400 on Llama-3.1-8B), 'subject to change'. Note the widely-cited '1,000 requests / 5 min' is the Hub API bucket, NOT inference.",
    },
    # --- Chinese / additional free-tier providers (verified against official docs) ---
    "glm": {
        "name": "Z.AI (Zhipu GLM)",
        "base_url": "https://api.z.ai/api/paas/v4",
        "models_url": "https://api.z.ai/api/paas/v4/models",
        "signup_url": "https://z.ai/manage-apikey/apikey-list",
        "key_hint": "...",
        # PAID-MODEL LEAK, now closed. free_families ['flash'] matched the PAID
        # glm-4.7-flashX ($0.07/$0.40 per M) — naming trap: 'Flash' = free,
        # 'FlashX' = paid, same generation. Tightening the substring CANNOT fix
        # this: 'glm-4.7-flash' is itself a substring of 'glm-4.7-flashx'.
        # Hence free_exact: the free set is exactly these 3 named ids, so an
        # exact-id match is both feasible and the only correct encoding.
        "free_filter": "family",
        "free_exact": True,
        "free_families": ["glm-4.7-flash", "glm-4.5-flash", "glm-4.6v-flash"],
        "default_free_models": ["glm-4.7-flash", "glm-4.5-flash", "glm-4.6v-flash"],
        "vision_models": ["glm-4.6v-flash"],
        "notes": "PERMANENT free ($0 in/out): GLM-4.7-Flash (200K ctx), GLM-4.5-Flash, GLM-4.6V-Flash (vision). Note glm-4.7-FlashX is PAID despite the name. International z.ai (email/Google signup, no China phone). ~1 req/s, 1 concurrent; Z.AI publishes no request quota.",
    },
    "qwen": {
        "name": "Qwen (Alibaba Model Studio)",
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "models_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/models",
        "signup_url": "https://bailian.console.alibabacloud.com/",
        "key_hint": "sk-...",
        "paid": True,  # 90-day consumable trial, NOT a free tier — keep OUT of free routing
        "trial": True,  # quota.py FREE_LIMITS qwen limit:0 — consumable 1M-tokens-PER-MODEL / 90-day trial, then AllocationQuota.FreeTierOnly on every call
        # The old families were leaky in both directions: 'air' matched ZERO Qwen
        # models (a GLM/Zhipu convention, copy-pasted), while bare 'plus'/'flash'/
        # 'lite' pulled in non-chat ids that break a CLI (qwen-mt-* translation,
        # qwen3-vl-* vision, qwen-image-plus whose quota is denominated in IMAGES)
        # and even deepseek-v4-flash. Cleared rather than re-tuned: nothing here
        # is free, so no family should assert free-ness.
        "free_filter": "pricing_zero",
        "free_families": [],
        "default_free_models": [],
        "notes": "Not a free tier — a consumable trial: 1,000,000 tokens PER MODEL, expiring 90 days after activating Model Studio, International (Singapore) deployment only. 'After the quota expires or is exhausted, you will be charged for continued use' (then AllocationQuota.FreeTierOnly errors). No permanently-free model exists. NOTE: the qwen-code CLI's OAuth path IS a genuine renewing free tier (2,000/day, 60 RPM) — different auth, would need its own entry.",
    },
    # REMOVED 2026-07-31 at explicit user request ("agentrouter.org doesn't
    # work — remove it"), re-confirmed live the same day: agentrouter.org
    # itself answers 200, but /v1/models still returns
    # 401 "unauthorized client detected" to every generic HTTP client — the
    # same WAF policy documented since 2026-07-27, unchanged. The isolated-CLI
    # relay that worked around it (_SUB_PROVIDERS' "sub-agentrouter") is gone
    # too; its probe hung for 240s. Kept as a comment, not code, so a future
    # re-add starts from the measured facts instead of re-probing:
    #   base_url    https://agentrouter.org/v1
    #   signup_url  https://agentrouter.org/register?aff=udWz  (maintainer's referral)
    #   nature      third-party relay, ONE-TIME consumable signup credit, no free tier
    "siliconflow": {
        "name": "SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "models_url": "https://api.siliconflow.cn/v1/models",
        "signup_url": "https://cloud.siliconflow.cn/account/ak",
        "key_hint": "sk-...",
        # The old families missed 5 free general-chat models and pinned
        # DeepSeek-R1-Distill-Qwen-7B, which is NOT free anywhere (0 occurrences
        # in the .cn catalog; $0.05/M on .com).
        # CLOSED: 'qwen/qwen2.5-7b-instruct' also substring-matched the PAID twin
        # 'Pro/Qwen/Qwen2.5-7B-Instruct' (¥0.35/M). SiliconFlow's rule is
        # free = original name, paid = 'Pro/' prefix, so the free_families match
        # hit both. exclude_families is checked BEFORE every filter rule, so the
        # paid twin can never be re-admitted.
        # This list is LOAD-BEARING, not a mere fallback: /v1/models requires
        # auth and exposes no pricing, so free-ness is not discoverable at runtime.
        "exclude_families": ["pro/"],
        "free_filter": "family",
        "free_families": ["qwen/qwen3-8b", "qwen/qwen3.5-4b", "qwen/qwen2.5-7b-instruct",
                          "thudm/glm-4-9b-0414", "thudm/glm-z1-9b-0414",
                          "deepseek-ai/deepseek-r1-0528-qwen3-8b", "tencent/hunyuan-mt-7b"],
        "default_free_models": [
            "Qwen/Qwen3-8B", "Qwen/Qwen3.5-4B", "THUDM/GLM-Z1-9B-0414",
            "THUDM/GLM-4-9B-0414", "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B",
            "Qwen/Qwen2.5-7B-Instruct",
        ],
        "notes": "PERMANENT free ($0) models on the CHINA platform only (api.siliconflow.cn — the same model is billed on .com). CAVEAT: the full free set needs Chinese real-name verification (实名认证, mainland ID/HK-Macau-Taiwan permit + Alipay facial recognition); without it accounts are capped ~100 req/day. The ¥14 coupon / '20M free tokens' promos are credits, not this tier.",
    },
    "modelscope": {
        "name": "ModelScope (Alibaba)",
        "base_url": "https://api-inference.modelscope.cn/v1",
        "models_url": "https://api-inference.modelscope.cn/v1/models",
        "signup_url": "https://modelscope.cn/my/myaccesstoken",
        "key_hint": "ms-...",
        # 'all' is CORRECT: everything on api-inference.modelscope.cn IS the free
        # service (paid/SLA inference is a different product on a different
        # base_url), and /v1/models carries no pricing so nothing else is even
        # implementable.
        "free_filter": "all",
        # BOTH previous ids were DEAD (100% dead fallback): the real ids carry
        # the -2507 suffix / are DeepSeek-V3.2. Spread across vendors so the
        # 500/model/day sub-cap doesn't exhaust them together; DeepSeek-V3.2 sits
        # mid-list because its family carries a lower ~100/model/day cap.
        "default_free_models": [
            "Qwen/Qwen3-235B-A22B-Instruct-2507", "ZhipuAI/GLM-5",
            "Qwen/Qwen3-Next-80B-A3B-Instruct", "moonshotai/Kimi-K2.5",
            "deepseek-ai/DeepSeek-V3.2", "MiniMax/MiniMax-M2.5", "Qwen/Qwen3-32B",
        ],
        # IMAGE GENERATION — bespoke async endpoint, NOT the OpenAI-compat chat
        # path: POST /v1/images/generations with X-ModelScope-Async-Mode:true
        # returns a task_id; poll /v1/tasks/{id} until SUCCEED. Same 2,000
        # calls/day account budget as chat, so no separate quota tracking.
        # Ids/shape cross-verified against the shipped SEO Quantum Pro image
        # path (services/image_generation_service.py generate_images_with_modelscope).
        "image_models": [
            {"id": "Qwen/Qwen-Image", "label": "Qwen-Image", "text_in_image": "excellent",
             "notes": "best open model for EN+CN typography"},
            {"id": "Tongyi/Z-Image-Turbo", "label": "Z-Image Turbo", "text_in_image": "excellent"},
            {"id": "black-forest-labs/FLUX.1-dev", "label": "FLUX.1 Dev", "text_in_image": "good"},
            {"id": "black-forest-labs/FLUX.1-schnell", "label": "FLUX.1 Schnell", "text_in_image": "medium"},
        ],
        "notes": "Free 2,000 API calls/day per account (500/model/day; some large models ~100/day), resets 00:00 UTC+8, no rollover. Signup needs an Alibaba Cloud account (KYC).",
    },
    "baidu": {
        "name": "Baidu Qianfan (ERNIE)",
        "base_url": "https://qianfan.baidubce.com/v2",
        "models_url": "https://qianfan.baidubce.com/v2/models",
        "signup_url": "https://console.bce.baidu.com/qianfan/ais/console/applicationConsole/application",
        "key_hint": "...",
        "free_filter": "family",
        "free_families": ["speed", "lite"],
        "default_free_models": ["ernie-speed-8k", "ernie-lite-8k"],
        "notes": "Free ERNIE-Speed / ERNIE-Lite. China KYC/phone likely required.",
    },
    "tencent": {
        "name": "Tencent Hunyuan",
        "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
        "models_url": "https://api.hunyuan.cloud.tencent.com/v1/models",
        "signup_url": "https://console.cloud.tencent.com/hunyuan/api-key",
        "key_hint": "...",
        "free_filter": "family",
        "free_families": ["lite"],
        "default_free_models": ["hunyuan-lite"],
        "notes": "hunyuan-lite is free. China KYC likely required.",
    },
    "iflytek": {
        "name": "iFlytek Spark",
        "base_url": "https://spark-api-open.xf-yun.com/v1",
        "models_url": None,
        "signup_url": "https://console.xfyun.cn/",
        "key_hint": "APIPassword",
        "free_filter": "all",
        "default_free_models": ["lite"],
        "notes": "Spark Lite (model id 'lite') is free. ToS restricts proxy use. No /models list.",
    },
    "ovhcloud": {
        "name": "OVHcloud AI Endpoints",
        "base_url": "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1",
        "models_url": "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/models",
        "signup_url": "https://endpoints.ai.cloud.ovh.net/",
        "key_hint": "...",
        "free_filter": "all",
        "default_free_models": ["Meta-Llama-3_3-70B-Instruct", "Mixtral-8x7B-Instruct-v0.1"],
        "notes": "RECURRING free: ~2 req/min per IP, 20+ open models, EU-hosted. Full keys via an OVHcloud account.",
    },
    "novita": {
        "name": "Novita AI",
        "base_url": "https://api.novita.ai/v3/openai",
        "models_url": "https://api.novita.ai/v3/openai/models",
        "signup_url": "https://novita.ai/settings/key-management",
        "key_hint": "sk-...",
        "free_filter": "all",
        "trial": True,
        "default_free_models": ["meta-llama/llama-3.1-8b-instruct"],
        "notes": "Small one-time free credit. Then pay-as-you-go.",
    },
    "opencode-zen": {
        "name": "OpenCode Zen",
        "base_url": "https://opencode.ai/zen/v1",
        "models_url": "https://opencode.ai/zen/v1/models",
        "signup_url": "https://opencode.ai/auth",
        "key_hint": "sk-...",
        # SWITCHED from 'pricing_zero' 2026-07-27: that filter fails closed with
        # no live pricing data (by design — "don't claim free-ness we can't
        # prove"), so it NEVER once picked up live catalog changes; the stale
        # 'minimax-m2.5-free' (removed from OpenCode's own catalog, 401 on every
        # call) sat undetected until manually re-verified. Their catalog already
        # marks free ids with a bare '-free' suffix (mimo-v2.5-free, deepseek-v4-
        # flash-free, ...) — same convention as openrouter's ':free', just a
        # different literal suffix — so suffix_free + free_suffix makes this
        # provider's free list genuinely LIVE: new/removed '-free' ids are
        # detected automatically, no more hand-re-verification needed.
        "free_filter": "suffix_free",
        "free_suffix": "-free",
        # RE-VERIFIED 2026-07-27 via the live /v1/models catalog + a real generation
        # per id. Removed 'minimax-m2.5-free': not in the live catalog at all (401
        # "Model minimax-m2.5-free is not supported" — the paid 'minimax-m2.5' exists,
        # the '-free' suffix on it never did). Added 'north-mini-code-free' (confirmed:
        # real content, cost 0) and 'mimo-v2.5-free' (real, cost 0, but a heavy
        # reasoner — burned 500 tokens on reasoning_content with zero visible output in
        # this probe; needs a generous max_tokens or reasoning-aware detection to be
        # recognized as alive, same class of issue _peek_until_content already handles
        # for streaming via saw_reasoning — see _finish()'s non-streaming probe path).
        "default_free_models": ["deepseek-v4-flash-free", "north-mini-code-free", "mimo-v2.5-free"],
        "notes": "OpenCode's own multi-model gateway. Free = zero-priced '-free' models (DeepSeek/MiniMax/GLM/Nemotron).",
    },
    "llama": {
        "name": "Meta Llama API",
        "base_url": "https://api.llama.com/compat/v1",
        "models_url": "https://api.llama.com/compat/v1/models",
        "signup_url": "https://llama.developer.meta.com",
        "key_hint": "LLM|...",
        "free_filter": "all",
        "default_free_models": ["llama-4-scout-17b-16e-instruct-fp8", "llama-3.3-70b-instruct"],
        "notes": "Meta's official Llama API, free developer tier. OpenAI-compatible at /compat/v1.",
    },
    "nova": {
        "name": "Amazon Nova",
        "base_url": "https://api.nova.amazon.com/v1",
        "models_url": "https://api.nova.amazon.com/v1/models",
        "signup_url": "https://nova.amazon.com/dev",
        "key_hint": "any",
        "free_filter": "all",
        "default_free_models": ["nova-2-pro-v1", "nova-2-lite-v1"],
        "notes": "Amazon Nova free developer tier (nova.amazon.com/dev). OpenAI-compatible.",
    },
    "aimlapi": {
        "name": "AI/ML API",
        "base_url": "https://api.aimlapi.com/v1",
        "models_url": "https://api.aimlapi.com/v1/models",
        "signup_url": "https://aimlapi.com/app/keys",
        "key_hint": "any",
        "free_filter": "all",
        "default_free_models": ["gpt-4o-mini", "deepseek-chat", "meta-llama/Llama-3.3-70B-Instruct-Turbo"],
        "notes": "300+ models via one key; free allowance for new accounts. OpenAI-compatible.",
    },
    "upstage": {
        "name": "Upstage (Solar)",
        "base_url": "https://api.upstage.ai/v1/solar",
        "models_url": "https://api.upstage.ai/v1/solar/models",
        "signup_url": "https://console.upstage.ai/api-keys",
        "key_hint": "up_...",
        "free_filter": "all",
        "default_free_models": ["solar-pro2"],
        "notes": "Upstage Solar; free trial tier. OpenAI-compatible.",
    },
    "wandb": {
        "name": "Weights & Biases Inference",
        "base_url": "https://api.inference.wandb.ai/v1",
        "models_url": "https://api.inference.wandb.ai/v1/models",
        "signup_url": "https://wandb.ai/authorize",
        "key_hint": "any",
        "free_filter": "all",
        "default_free_models": ["meta-llama/Llama-3.3-70B-Instruct", "deepseek-ai/DeepSeek-V3"],
        "notes": "W&B Inference free credits for open models. OpenAI-compatible.",
    },
    "ollama-cloud": {
        "name": "Ollama Cloud",
        "base_url": "https://ollama.com/v1",
        "models_url": "https://ollama.com/v1/models",
        "signup_url": "https://ollama.com/settings/keys",
        "key_hint": "any",
        # 'all' is CORRECT: /v1/models returns only cloud models and access is
        # NOT tier-gated — free vs Pro is quota + concurrency, not catalog.
        "free_filter": "all",
        # Low-Usage models FIRST. The previous two ids were real and live, but
        # were the two most quota-hungry choices possible ('Medium Usage'
        # gpt-oss:120b and 'High Usage' qwen3-coder:480b), so a discovery failure
        # fell back to exactly the models that burn a light free tier fastest.
        # ID GOTCHA: use BARE ids — the ':cloud'/'-cloud' suffix exists only for
        # the local daemon proxying to cloud; the hosted API returns bare ids.
        "default_free_models": [
            "gpt-oss:20b", "gemma3:12b", "gemma3:4b", "ministral-3:8b",
            "gpt-oss:120b", "gemma3:27b", "qwen3-coder-next",
        ],
        "notes": "Ollama's hosted cloud, genuinely free ($0, no card). Metered on GPU TIME, not tokens/requests — usage weight varies hugely per model (gpt-oss:20b Low ... deepseek-v4-pro Extra High). Session limits reset every 5h, weekly every 7d; no numeric quota is published. Free allows only ONE concurrent cloud model, so parallel fan-out will contend.",
    },
    "clarifai": {
        "name": "Clarifai",
        "base_url": "https://api.clarifai.com/v2/ext/openai/v1",
        "models_url": "https://api.clarifai.com/v2/ext/openai/v1/models",
        "signup_url": "https://clarifai.com/settings/security",
        "key_hint": "any",
        "free_filter": "all",
        "default_free_models": ["deepseek-ai/DeepSeek-R1"],
        "notes": "Clarifai community models, free tier. OpenAI-compatible shim.",
    },
    "zenmux": {
        "name": "ZenMux",
        "base_url": "https://zenmux.ai/api/v1",
        "models_url": "https://zenmux.ai/api/v1/models",
        "signup_url": "https://zenmux.ai/settings/keys",
        "key_hint": "sk-...",
        "free_filter": "pricing_zero",
        "default_free_models": ["moonshotai/kimi-k2.7-code-free", "z-ai/glm-5.2-free"],
        "notes": "Model router with free '-free' variants (Kimi/GLM). Free = zero-priced models.",
    },
    "unorouter": {
        "name": "UnoRouter",
        "base_url": "https://api.unorouter.com/v1",
        "models_url": "https://api.unorouter.com/v1/models",
        "signup_url": "https://unorouter.com",
        "key_hint": "sk-...",
        "free_filter": "pricing_zero",
        "default_free_models": ["glm-4.5-flash:free"],
        "notes": "Model router with ':free' models. Free = zero-priced models.",
    },
    "llmgateway": {
        "name": "LLMGateway",
        "base_url": "https://api.llmgateway.io/v1",
        "models_url": "https://api.llmgateway.io/v1/models",
        "signup_url": "https://llmgateway.io/dashboard",
        "key_hint": "any",
        "free_filter": "pricing_zero",
        "default_free_models": [],
        "notes": "Open-source gateway/router; free-tier models. Free = zero-priced models.",
    },
    "iflow": {
        "name": "iFlow",
        "base_url": "https://apis.iflow.cn/v1",
        "models_url": "https://apis.iflow.cn/v1/models",
        "signup_url": "https://platform.iflow.cn",
        "key_hint": "sk-...",
        "free_filter": "all",
        "default_free_models": ["qwen3-coder-plus", "deepseek-v3", "kimi-k2"],
        "notes": "iFlow (China) free tier for Qwen/DeepSeek/Kimi. OpenAI-compatible.",
    },
    # ── PAID gateways / inference hosts (opt-in; excluded from the free system) ─
    # paid=True keeps them OUT of the free selection, but base_url_for() makes
    # them usable for normal API requests. Each has a get-key link.
    "fireworks": {"name": "Fireworks AI", "base_url": "https://api.fireworks.ai/inference/v1",
        "models_url": "https://api.fireworks.ai/inference/v1/models", "signup_url": "https://app.fireworks.ai/settings/users/api-keys",
        "key_hint": "fw_...", "free_filter": "all", "default_free_models": [], "paid": True, "notes": "Fast OpenAI-compatible host (Llama/Qwen/DeepSeek/Flux). Pay-as-you-go."},
    "perplexity": {"name": "Perplexity", "base_url": "https://api.perplexity.ai",
        "models_url": None, "signup_url": "https://www.perplexity.ai/account/api/keys",
        "key_hint": "pplx-...", "free_filter": "all",
        "default_free_models": ["sonar", "sonar-pro", "sonar-reasoning", "sonar-reasoning-pro", "sonar-deep-research"],
        "paid": True,
        "notes": "Sonar models w/ live web search. Endpoint /chat/completions (NO /models list -> models hardcoded, was empty=unusable). $5 FREE API credit on first signup (trial), then paid."},
    # ── PAID image-generation-only providers (opt-in, explicit "<pid>/<model>"
    # pin ONLY — every image_models row here carries "free": False, which keeps
    # them out of _image_candidates()'s auto/manual rotation exactly like the
    # paid chat providers above). No default_free_models/chat models: these
    # rows exist purely to carry an image_models list.
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models_url": None,
        "signup_url": "https://platform.openai.com/api-keys",
        "key_hint": "sk-...",
        "paid": True,
        "free_filter": "pricing_zero",
        "default_free_models": [],
        "image_models": [
            {"id": "gpt-image-1.5", "label": "GPT Image 1.5", "text_in_image": "excellent", "free": False},
            {"id": "gpt-image-2", "label": "GPT Image 2", "text_in_image": "excellent", "free": False},
        ],
        "notes": "One OpenAI key does everything: TEXT via an openai/gpt-... pin (chat/completions) AND images via openai/gpt-image-2. Paid, pay-as-you-go.",
    },
    "higgsfield": {
        "name": "Higgsfield AI",
        "base_url": "https://platform.higgsfield.ai",
        "models_url": None,
        "signup_url": "https://cloud.higgsfield.ai/api-keys",
        "key_hint": "KEY_ID:KEY_SECRET (paste both, colon-separated, e.g. abc123:def456)",
        "paid": True,
        "free_filter": "pricing_zero",
        "default_free_models": [],
        "image_models": [
            {"id": "higgsfield/text2image/soul", "label": "Higgsfield Soul 2.0", "text_in_image": "good", "free": False},
            {"id": "flux-pro/kontext/max/text-to-image", "label": "Flux Pro Kontext", "text_in_image": "good", "free": False},
            {"id": "bytedance/seedream/v4/text-to-image", "label": "Seedream 4 (Higgsfield)", "text_in_image": "excellent", "free": False},
            {"id": "higgsfield/nano-banana-pro", "label": "Nano Banana Pro (Higgsfield)", "text_in_image": "excellent", "free": False},
        ],
        "notes": "Bespoke API (not OpenAI-compatible). Composite credential: paste as KEY_ID:KEY_SECRET in the single key field. Async submit-then-poll. IMAGE ONLY -- this hub does not do video generation.",
    },
    "aiand": {
        "name": "AIAND",
        # CONFIRMED live 2026-07-17 (docs.aiand.com code samples + a real,
        # unauthenticated request to the endpoint itself -- api.aiand.com
        # returned a proper OpenAI-shaped {"error":{"message","type","param",
        # "code":"invalid_api_key"}} for GET /v1/models with no key, and the
        # bare host returned {"error":{"message":"Not found. Use
        # /v1/chat/completions, /v1/completions, /v1/responses, or
        # /v1/models",...}} -- confirms both the base URL and genuine OpenAI
        # compatibility). Model ids look vendor-prefixed (docs example:
        # "openai/gpt-oss-120b"), suggesting a multi-provider router similar
        # to OpenRouter.
        # NOT CONFIRMED: exact free-tier terms/limits and which specific
        # models are actually free vs. credit-metered -- the marketing site is
        # a JS-rendered SPA (curl got the raw HTML, no "free"/"credit" text
        # anywhere in it; the claim comes from the user, not hub research).
        # free_filter:"all" is a placeholder until a real key is tested here --
        # use the dashboard's Test button once a key is saved to see the REAL
        # live model list, and treat any specific model as free only after
        # confirming it actually answers (same discipline as every other
        # provider in this file: probe, don't infer a free tier from a page).
        "base_url": "https://api.aiand.com/v1",
        "models_url": "https://api.aiand.com/v1/models",
        "signup_url": "https://console.aiand.com/api-keys",
        "key_hint": "sk-...",
        # PROBED 2026-07-24: /balance, /credits and /me ALL return 402 "Insufficient
        # credits. Add credits at console.aiand.com" — prepaid only, no signup grant
        # and no monthly grant. Worse, it MASKS the empty balance as a 404
        # model_not_found on chat calls, so the card must say "trial" out loud.
        "trial": True,
        # PROBED with a real key 2026-07-20 — the placeholder above is now resolved.
        # Of the 7 ids the live catalog advertises, exactly ONE answers:
        #   qwen/qwen3.6-27b              200  (vendor catalog lists it Free / Free)
        #   deepseek-ai/deepseek-v4-pro   402  insufficient_credits  (priced $1.00/$2.50)
        #   deepseek-ai/deepseek-v4-flash 402  insufficient_credits  (priced $0.15/$0.25)
        #   zai-org/glm-5.2               404  |  moonshotai/kimi-k2.7-code   404
        #   google/gemma-4-31b-it         404  |  openai/gpt-oss-120b         404
        # ai& is prepaid-only (docs.aiand.com/billing/credits: "prepaid credit
        # model", no signup grant, no monthly grant) — at a zero balance every
        # PRICED id 402s forever, so routing to them only ever burns a chain hop.
        # Pinned to the probed-free id; widen this ONLY after re-probing with credit.
        "free_filter": "family",
        # RE-PROBED 2026-07-31: nothing here is callable any more. The catalog
        # still lists 8 ids and every one is now PRICED — qwen/qwen3.6-27b, the
        # single model that was free at a zero balance on 2026-07-20, is now
        # $0.32/$3.20 per 1M. With all accounts at zero credit, every catalog id
        # answers 404 "does not exist or you do not have access to it", which is
        # an entitlement wall, not a bad id: a genuinely unknown id returns a
        # DIFFERENT error (400 invalid_value). Verified straight against the
        # upstream, bypassing the hub, across all saved keys — not a hub bug.
        # Emptied so aiand contributes ZERO routing hops instead of one
        # guaranteed 404 per request. Add credit at console.aiand.com ($1 min)
        # and re-probe to restore it.
        "free_families": [],
        "default_free_models": [],
        "notes": "Prepaid only — NO free tier and no signup credits (docs.aiand.com/billing/credits). NOTHING is callable at a zero balance as of 2026-07-31: the last free id (qwen/qwen3.6-27b, free when probed 2026-07-20) is now priced, and every catalog id returns 404 'no access' until credit is added at console.aiand.com/settings/billing ($1 minimum).",
    },
    "github-models": {
        "name": "GitHub Models",
        "base_url": "https://models.github.ai/inference",
        "models_url": "https://models.github.ai/inference/models",
        "signup_url": "https://github.com/marketplace/models",
        "key_hint": "github_pat_... or ghp_... (needs the models:read scope)",
        # 'all' is correct here: the WHOLE catalog has a free tier (per-model
        # rate limits, not per-model billing) — GitHub Models is a free
        # evaluation playground, there is no paid catalog to leak. The real
        # metering is per-TIER request caps (see quota.py FREE_LIMITS), and
        # real 429s sideline an exhausted id via the per-model throttle.
        "free_filter": "all",
        # Publisher-prefixed ids. Live /models discovery widens this; the pins
        # below are well-known free-tier ids so the provider is usable even
        # when discovery fails. NOTE: a token WITHOUT the models:read scope
        # 403s on EVERY call (app.py's probe-before-default path exists
        # precisely because llama-4-maverick ranked best while 403ing).
        # RETIRED BY GITHUB 2026-07-30 — emptied 2026-07-31 so it contributes
        # ZERO routing hops. Every endpoint (/inference/models, /catalog/models,
        # completions on every id) answers:
        #   HTTP 410 Gone
        #   {"error":{"code":"github_models_retirement_brownout",
        #             "message":"GitHub Models is temporarily unavailable as
        #                        part of a scheduled retirement brownout."}}
        # The "brownout" wording is stale tooling text: the brownouts were
        # 2026-07-16 and 07-23, the hard shutdown was 07-30, and GitHub's docs
        # now state the playground, catalog, inference API and BYOK are gone for
        # every customer. Confirmed NOT a local problem: the same PAT still
        # returns 200 on api.github.com/user, and the 410 is returned
        # unauthenticated too. 410 is permanent, not 503.
        # Entry KEPT (not deleted) so the quota row, tests and any saved key
        # stay coherent, and so a future revival is a one-line restore.
        "default_free_models": [],
        "notes": "RETIRED — GitHub fully shut down GitHub Models on 2026-07-30; every endpoint now returns HTTP 410 'github_models_retirement_brownout' for all customers, authenticated or not. Nothing here is callable and the provider contributes no routing hops. Kept registered only so a revival would be a one-line change.",
    },
    "tokenrouter": {
        "name": "TokenRouter",
        "base_url": "https://api.tokenrouter.com/v1",
        "models_url": "https://api.tokenrouter.com/v1/models",
        "signup_url": "https://www.tokenrouter.com/console/token",
        "key_hint": "sk-...",
        # VERIFIED 2026-08-04 via a real generation per id, not just /models
        # listing (that alone said nothing about what's actually callable):
        #   moonshotai/kimi-k3-free  -> real 200, real content. Genuinely free.
        #   nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free -> 403
        #     insufficient_user_quota, "$0.00 remaining credit, please
        #     recharge" -- the ':free' in the id does NOT mean free on this
        #     platform; it still draws the paid balance. Do NOT trust a
        #     provider's own '-free'/':free' naming here without a real probe.
        "free_filter": "suffix_free",
        "free_suffix": "-free",
        "default_free_models": ["moonshotai/kimi-k3-free"],
        # No rate-limit headers on a real response and the marketing site
        # 403s WebFetch, so the real per-minute/day cap is UNKNOWN -- not
        # guessed here (see quota.py: an unknown provider is never reported
        # exhausted, but a real 429 still throttles it via the normal path).
        "notes": "Keyless-style OpenAI-compatible aggregator (100+ models). Only moonshotai/kimi-k3-free confirmed genuinely free by a real call; other '-free'/':free'-tagged ids on this platform have been seen to still bill the account balance despite the name -- verify each with a real generation before trusting the suffix.",
    },
    "uncloseai": {
        "name": "UncloseAI (Hermes)",
        "base_url": "https://hermes.ai.unturf.com/v1",
        "models_url": "https://hermes.ai.unturf.com/v1/models",
        "signup_url": "https://hermes.ai.unturf.com",
        "key_hint": "(no key needed — any non-empty string is accepted)",
        "no_key": True,   # free forever, no signup, no card
        # The server accepts ANY non-empty bearer (or none); app.py's no-key
        # path sends `Authorization: Bearer uncloseai` via this static_key.
        "static_key": "uncloseai",
        # Free-only service (nothing is sold), so 'all' cannot leak a paid id.
        "free_filter": "all",
        # RE-VERIFIED 2026-07-30: the old flagship 'hermes-3-llama-3.1-405b' is
        # GONE (404 "does not exist"); the live /models catalog is now a single
        # id, solidrust/Hermes-3-Llama-3.1-8B-AWQ — chat-verified 200 with the
        # static_key bearer. Discovery still widens this automatically if the
        # catalog regrows.
        "default_free_models": ["solidrust/Hermes-3-Llama-3.1-8B-AWQ"],
        "notes": "Free-forever community endpoint (vLLM). Keyless: any non-empty string works as the API key. Catalog has shrunk to one 8B AWQ Hermes id (verified 2026-07-30). No published rate limit — quota tracks it as UNKNOWN (real 429s still throttle it). Volunteer-run: no SLA.",
    },
    "llm7": {
        "name": "LLM7.io",
        "base_url": "https://api.llm7.io/v1",
        "models_url": "https://api.llm7.io/v1/models",
        "signup_url": "https://llm7.io",
        "key_hint": "(no key needed — the literal string 'unused' works)",
        "no_key": True,   # free without signup, no card
        # Documented credential is the literal string "unused" — app.py's
        # no-key path sends it as `Authorization: Bearer unused`.
        "static_key": "unused",
        # llm7 is NOT a free-only catalog: /models mixes a PAID 'pro' tier
        # (token-priced, incl. image/video) with the free anonymous 'turbo'
        # tier, so 'all' would leak paid ids into free routing (pro ids 401
        # "invalid_api_key", but only AFTER wasting a routed attempt).
        #
        # RE-VERIFIED 2026-08-29: the hand-pinned free_exact list this row used
        # to carry had rotted in under a month — 'gpt-oss:20b' was renamed to
        # 'gpt-oss' (400 model_unavailable) and 'gemini-3.1-flash-lite' moved to
        # the paid tier (401), while three live free ids were invisible. The
        # catalog now labels every row with `tier` and `usage_based_only`, so
        # free-ness is machine-readable: read that instead of re-pinning a list
        # that will rot again. New turbo models are picked up automatically.
        "free_filter": "free_tier",
        "free_tiers": ["turbo"],
        # Fallback only (used when discovery fails). The 5 turbo ids live on
        # 2026-08-29; the live filter above supersedes this whenever it works.
        "default_free_models": ["gpt-oss", "codestral-latest", "minimax-m2.7",
                                "meta-Llama-3.1-8B-Instruct-Turbo",
                                "mistral-Nemo-Instruct-2407"],
        "notes": "Free OpenAI-compatible gateway, no signup (the literal string 'unused' is the documented API key). The catalog's 'pro' tier is token-priced and 401s anonymously; this row filters on the catalog's own `tier` field ('turbo' = free) plus the `usage_based_only` paid flag, so new free models appear automatically and paid ids stay out of free routing. Documented rate: ~20 req/min, 100 req/hour.",
    },
    "api-airforce": {
        "name": "API Airforce",
        "base_url": "https://api.airforce/v1",
        "models_url": "https://api.airforce/v1/models",
        "signup_url": "https://api.airforce",
        "key_hint": "free key from the api.airforce panel",
        # BYOK but the key itself is free and the whole ~55-model catalog is
        # served at $0 — nothing paid to leak, so 'all' is honest.
        "free_filter": "all",
        "default_free_models": [],
        "notes": "Free BYOK gateway (~55 free models): grab a free key from the api.airforce panel, no card. No published daily request cap — quota tracks it as UNKNOWN (DEFAULT_LIMIT) rather than a fabricated budget; real 429s still sideline it.",
    },
    "navy": {
        "name": "Navy",
        "base_url": "https://api.navy/v1",
        "models_url": "https://api.navy/v1/models",
        "signup_url": "https://api.navy",
        "key_hint": "free key from api.navy",
        # Shared free pool behind a free key; nothing paid on the free tier to
        # leak, so 'all' is honest.
        "free_filter": "all",
        "default_free_models": [],
        "notes": "Free shared pool (~150K tokens/day at ~20 RPM) behind a free key from api.navy. The real budget is TOKENS/day, which a request counter can't express — quota tracks the documented ~20 RPM request rate and real 429s retire it when the pool is spent.",
    },
    "routeway": {
        "name": "RouteWay",
        "base_url": "https://api.routeway.ai/v1",
        "models_url": "https://api.routeway.ai/v1/models",
        "signup_url": "https://api.routeway.ai",
        "key_hint": "free key from api.routeway.ai",
        # OpenRouter-style ':free' suffix marks the free ids — live discovery
        # picks up new/removed ':free' entries automatically.
        "free_filter": "suffix_free",
        "default_free_models": [],
        "notes": "BYOK router with a free tier on ':free'-suffixed ids (~5 RPM, ~200 req/day). The 5/min burst limit is handled by the 429 cooldown path; quota.py tracks the daily budget.",
    },
    # ONE CARD, not three. Until 2026-08-06 this was g4f-groq / g4f-gemini /
    # g4f-nvidia: three keyless entries, one per upstream catalog, each on its
    # own /api/<upstream>/v1 path. That split stopped making sense the moment
    # g4f ended anonymous access -- all three now authenticate with the SAME
    # g4f.dev account key, so three cards meant pasting one key three times.
    #
    # VERIFIED LIVE 2026-08-06, which is what makes the merge correct rather
    # than cosmetic: https://g4f.space/v1 is a real unified endpoint (the one
    # the member dashboard itself prints next to the key). GET /v1/models
    # returns 200 with 550 models spanning every upstream at once -- gemini
    # (38), llama (45), qwen (90), gpt (57), deepseek (39), nemotron (27),
    # glm (27), mistral (16), kimi (10) -- so the single card is a superset of
    # what the three separate ones covered.
    "g4f": {
        "name": "G4F (g4f.space)",
        "base_url": "https://g4f.space/v1",
        "models_url": "https://g4f.space/v1/models",
        "signup_url": "https://g4f.dev/members.html",
        "key_hint": "key from g4f.dev/members.html",
        # KEYLESS ACCESS ENDED (verified live 2026-08-06): a keyless chat call
        # returns HTTP 402 insufficient_credits — "No cake credits. Bake
        # proof-of-work cakes at g4f.dev/chat to earn anonymous usage, or sign
        # up at g4f.dev/members.html." The /models catalog is still public
        # (200), so ONLY the chat path broke. The hub cannot mine browser
        # proof-of-work "cakes", so this is a keyed provider now: sign up free
        # and paste the key. Without one it is cleanly excluded from routing
        # instead of burning a chain hop on a guaranteed 402.
        # Community-donated servers with no pricing field anywhere in the
        # catalog -- nothing here can bill the user -> 'all' is safe.
        "free_filter": "all",
        # SECOND LAYER ONLY. All 7 api.airforce-owned rows in the keyed catalog
        # sit on this ONE donated server and answer HTTP 200 with "The model
        # does not exist in https://api.airforce / discord.gg/airforce" as the
        # assistant MESSAGE (verified live 2026-08-07) -- a successful response
        # by every status-based check in the hub, which is why it survived four
        # rounds of fixes. Because claude-* scores a 138 floor, it was usually
        # hop 1, so this saves a guaranteed-wasted top-priority hop.
        # It must never be the only defence: these server ids rotate as
        # volunteers join and leave, and this does nothing for the other 41
        # backends. _chat_json_nonanswer in app.py is the real fix.
        "exclude_families": ["srv_mp3lmkuad07322459f47"],
        # /v1/models SERVES TWO DIFFERENT CATALOGS depending on the request
        # (both verified live 2026-08-06):
        #   no Authorization -> 549 community-server ids shaped
        #                       "srv_<server>:<model>", volatile as servers
        #                       join and leave;
        #   with a bearer    -> 273 CLEAN ids (gemini-3.5-flash, o3-mini,
        #                       hy3, ...) plus per-model vision/image flags.
        # The keyed catalog is the only one that matters now, and the hub only
        # runs live discovery once a key exists anyway (provider_free_models
        # returns defaults for a keyed provider with no key). So these pins are
        # purely the discovery-failed fallback, and they must be ids the KEYED
        # catalog actually serves.
        #
        # PINNED FROM A REAL SWEEP (2026-08-07), not from the catalog listing:
        # the top 24 ids by score were each sent a live 1-token request. Only
        # 7 came back FREE AND WORKING, and they were the Gemini 3.x family.
        # Everything scoring 134-138 -- claude-opus-4-8, claude-haiku-4.5,
        # gpt-5.5, kimi-k3 -- answers 402 PAYMENT_REQUIRED ("this request costs
        # ~0.0472 pollen, your available balance is 0.0000"), i.e. g4f has a
        # paid currency and those models want it. The whole gpt-5.6-* family
        # 500/502s. "auto" exists only in the anonymous listing, and "default"
        # (the keyed router alias) 429'd on every attempt -- both were pinned
        # here before this sweep and neither survives contact.
        "default_free_models": ["gemini-3.5-flash", "gemini-3.6-flash",
                                "gemini-3.1-pro-low"],
        "notes": "Community relay pooling ~550 models donated across many volunteer servers (OpenAI-compatible, ~5 req/min). ONE card since 2026-08-06: the old per-upstream g4f-groq / g4f-gemini / g4f-nvidia entries all authenticate with the same g4f.dev key and are superseded by the unified https://g4f.space/v1 endpoint. NO LONGER KEYLESS as of the same date: anonymous calls return 402 'No cake credits' and now need either browser proof-of-work or a free account key from g4f.dev/members.html. Unstable volunteer-run gateway: quality varies per donated server, ids come and go as servers join and leave, and the whole proxy can go down without warning — no SLA. Treat as opportunistic capacity, never the only link in a chain.",
    },
    "kilocode": {
        "name": "Kilo Code (anonymous)",
        # NOTE: base_url is the PREFIX only — app.py appends '/chat/completions'
        # itself, so the real endpoint is the documented
        # https://api.kilo.ai/api/openrouter/chat/completions.
        "base_url": "https://api.kilo.ai/api/openrouter",
        # VERIFIED 2026-07-30: /models IS reachable anonymously
        # (Bearer anonymous, OpenRouter-shaped {"data": [...]}, 343 ids with an
        # isFree flag) — live discovery works; the ':free' filter keeps the 9
        # genuinely-free ids and drops the paid catalog.
        "models_url": "https://api.kilo.ai/api/openrouter/models",
        "signup_url": "https://kilo.ai",
        "key_hint": "(no signup — the literal string 'anonymous' works)",
        "no_key": True,   # anonymous free tier, no signup, no card
        # The anonymous tier's documented credential is the literal bearer
        # string "anonymous" — the no-key path sends it as
        # `Authorization: Bearer anonymous` (same mechanism as uncloseai/llm7).
        "static_key": "anonymous",
        # OpenRouter-style ':free' suffix marks the free ids (routeway shape).
        "free_filter": "suffix_free",
        # Chat-verified 200 anonymously on 2026-07-30 (fallback if discovery
        # fails). openai/gpt-4.1:free 404s "unavailable for free" — free slugs
        # rotate with the upstream OpenRouter pool.
        "default_free_models": ["stepfun/step-3.7-flash:free"],
        "notes": "Anonymous free tier of the Kilo Code OpenRouter relay: no signup, the literal bearer 'anonymous' authenticates. Free ids carry the ':free' suffix. Unstable community-run gateway: the anonymous tier is a courtesy, rate limits are unpublished (quota tracks it as UNKNOWN), quality varies and it can go down without warning — no SLA.",
    },
    "puter": {
        "name": "Puter",
        # Pinned in the dashboard's Recommended zone: one free puter.com
        # account token unlocks the newest flagship catalog (GPT-5.6 family).
        "recommended": True,
        # DRIVER-BASED, not OpenAI-compatible for the tokens this hub can get.
        # PROBED LIVE 2026-07-31 with a real popup token: POST
        # <base_url>/chat/completions answers 403 "This endpoint is only
        # available to user sessions" — that surface accepts a browser SESSION,
        # not the app token puter.js hands out. The token DOES work on
        # POST https://api.puter.com/drivers/call, which is what puter.js
        # itself calls for every AI feature. app.py's `driver_api` adapter
        # translates OpenAI chat-completions <-> that driver in both
        # directions (streaming included: the driver answers NDJSON).
        # base_url is kept for reference/documentation only; nothing posts to it.
        "driver_api": "puter",
        "base_url": "https://api.puter.com/puterai/openai/v1",
        # NOT <base_url>/models — that path does NOT EXIST on Puter's gateway
        # (probed 2026-07-31: GET returns 404 "not_found" WITH a valid bearer
        # too, so it is a missing route, not an auth gate). Pointing the key
        # test at it made every Puter test fail "✗ HTTP 404: Not Found" before
        # it ever reached the generation probe. Puter's real catalog route is
        # the one its own puter.js SDK calls, /puterai/chat/models/details
        # (200, public, 563 models, {"models":[{"id":..}]} — a shape
        # _parse_model_ids already accepts). It needs no auth, so it can't
        # certify a key on its own; that is fine, the key test ALWAYS follows
        # the listing with a real generation call.
        "models_url": "https://api.puter.com/puterai/chat/models/details",
        "signup_url": "https://puter.com",
        "key_hint": "manual fallback — prefer the Connect with Puter button above",
        # BYOK (NOT no_key): one free puter.com account yields an auth token
        # that unlocks the whole OpenAI-compatible catalog. Puter is a
        # "user-pays" gateway — the free account IS the payment identity, so
        # 'all' cannot leak a separately-billed id; the single token covers
        # 500+ models incl. the latest GPT-5.6 family (sol/terra/luna + -pro),
        # gpt-5.5-pro, gpt-5.4, gpt-5.3-codex, gpt-4.1, GPT Image and the
        # Claude/Gemini/Grok/DeepSeek/Mistral/Llama catalogs. Fair-use limits
        # apply but Puter publishes NO numbers -> quota.py deliberately leaves
        # it out of FREE_LIMITS (UNKNOWN via DEFAULT_LIMIT; uncloseai/
        # api-airforce precedent) and real 429s sideline it.
        #
        # MEASURED 2026-07-31 from the live activity trail: 399 of the catalog's
        # 563 ids are ROUTING-PREFIXED duplicates of a plain id
        # ('openrouter:google/gemini-3-flash-preview', 'infron:anthropic/
        # claude-opus-4.6-fast', 'alibaba:qwen/qvq-max'), plus ':batch' variants
        # that are not interactive endpoints at all. The driver rejects every one
        # with HTTP 400 — and because they score identically to the plain id, the
        # router kept picking a prefixed variant FIRST, so every request burned
        # its first hop on a guaranteed 400 and fell through to a weaker
        # provider (observed: a real ask landing on llm7/gemini-3.1-flash-LITE
        # after puter 400 + two g4f 429s). Excluding ':' keeps the 164 canonical
        # ids and drops every prefixed twin. exclude_families is checked BEFORE
        # every other filter rule, so a prefixed id can never be re-admitted.
        "exclude_families": [":"],
        # NOT A FREE TIER — a metered account with a tiny monthly credit.
        # MEASURED 2026-07-31 from Puter's own /metering/usage: the allowance is
        # 25,000,000 micro-cents = ~25 US CENTS PER MONTH, shared across chat AND
        # images. One Gemini image is 15% of the whole month; 98 ordinary calls
        # spent it completely. Declaring `free_filter: "all"` (as this entry did)
        # put all 164 canonical ids into FREE auto-rotation, so the router kept
        # reaching for a pot that runs dry in a few dozen calls and then 402s.
        # Same treatment as alibaba's consumable trial: no free models, so puter
        # is reachable by an explicit '<pid>/<model>' pin or by switching auto
        # routing to 'mix'/'paid' — never as part of the free fleet.
        "paid": True,
        "trial": True,
        "free_filter": "pricing_zero",
        "free_families": [],
        # Live probe 2026-07-31 against the real catalog (563 models): the
        # gateway is up and auth-gated (chat completions answers
        # 'reauth_required' on a dummy bearer). Pins below are VERIFIED to
        # exist in that catalog and keep the provider usable if discovery
        # fails. 'gpt-5.5-pro' was a PHANTOM id — the served one is dated,
        # gpt-5.5-pro-2026-04-23 (still matched by the app.py _PREF_FLOORS
        # substring, so its scoring floor is unaffected).
        # EMPTY on purpose: a populated list here is returned as "free models"
        # regardless of free_filter, which would put puter straight back into
        # free auto-rotation. These ids are still perfectly usable — pin them as
        # 'puter/gpt-5.6-sol' etc., or set auto routing to 'mix'.
        "default_free_models": [],
        # TEXT-TO-IMAGE via the same drivers/call endpoint as chat
        # (interface "puter-image-generation", method "generate").
        #
        # THE CATALOG IS DISCOVERABLE, and is NOT the chat catalog: POST
        # drivers/call {"interface":"puter-image-generation","method":"models"}
        # returns 59 image models with per-model pricing (an earlier note here
        # wrongly said no catalog existed — it was looked for in
        # /puterai/chat/models/details, which holds zero image models).
        #
        # NONE OF THE 59 ARE FREE. Every one carries a cost and bills the
        # account; there is no free image tier to filter for. So every row
        # below is `free: False` — visible and one click away in the picker,
        # but never auto-selected, so an Auto generation can't quietly spend
        # the balance. (Puter CHAT stays in the free rotation: tokens cost
        # fractions of a cent, while one image costs 0.3–17c.)
        #
        # A listed model is NOT necessarily a working one — togetherai:lykon/
        # dreamshaper is in the catalog but 400s "Unable to access model" from
        # Together upstream. Every id below was generated end-to-end on
        # 2026-07-31; prices are the catalog's own figures for 1024x1024.
        #
        # Result shape differs by upstream (both handled in _puter_image_b64):
        # OpenAI/Gemini return a data: URI, Replicate/Together return a bare
        # https URL to a .webp/.jpg that must be downloaded.
        #
        # IMAGE-TO-IMAGE is NOT available: puter.js binds txt2img, txt2vid,
        # img2txt, txt2speech, speech2txt, speech2speech — no img2img.
        # TEXT-TO-VIDEO exists ("puter-video-generation") but 402s
        # "insufficient_funds" on a free account.
        "image_models": [
            {"id": "gpt-image-1-mini", "label": "GPT Image 1 mini", "free": False,
             "text_in_image": "excellent",
             "notes": "~0.5c per 1024x1024 (low) / 1.1c (medium). Cheapest of the strong text-rendering models — best default here."},
            {"id": "gpt-image-1", "label": "GPT Image 1", "free": False,
             "text_in_image": "excellent",
             "notes": "~1.1c per 1024x1024 (low) / 4.2c (medium) / 16.7c (high)."},
            {"id": "gemini-2.5-flash-image", "label": "Nano Banana (Gemini 2.5 Flash Image)",
             "free": False, "text_in_image": "excellent",
             "notes": "~3.9c per image. Strong at edits and photoreal scenes."},
            {"id": "togetherai:black-forest-labs/flux.1-schnell", "label": "FLUX.1 Schnell (Together)",
             "free": False, "text_in_image": "medium",
             "notes": "~0.27c per image — the cheapest that actually works here. Returns a JPEG URL. Weak at text in the image."},
            {"id": "black-forest-labs/flux-schnell", "label": "FLUX Schnell (Replicate)",
             "free": False, "text_in_image": "medium",
             "notes": "~0.3c per image. Returns a WEBP URL."},
            {"id": "ai-image", "label": "Puter default", "free": False,
             "text_in_image": "excellent",
             "notes": "Sends no model at all and lets Puter choose (currently a GPT-Image-class model, C2PA-signed PNG). Use when you don't care which."},
        ],
        "notes": "User-pays AI gateway: one free puter.com account auth token unlocks the whole OpenAI-compatible catalog (500+ models incl. the newest GPT-5.6 flagship family, GPT Image, and Claude/Gemini/Grok/DeepSeek/Mistral/Llama via the same account). Fair-use limits apply but are unpublished, so quota tracks it as UNKNOWN. Flagship ids carry a scoring preference floor (app.py _PREF_FLOORS) so puter ranks first among equals — user-requested top priority.",
    },
    # ------------------------------------------------------------------ #
    # OmniRoute-sourced expansion (2026-08-04) - verified BYOK / OpenAI-
    # compatible providers that expose genuine free models. Every id below
    # was cross-checked against OmniRoute's registry; the same-model hosts
    # are what lets _build_chain / _pick_same_model_host spread a single
    # model across providers for failover + load balancing (e.g. many of
    # these serve gpt-oss-120b / deepseek-v4-flash / llama-3.3-70b alongside
    # the providers already in the fleet, so requests dispatch onto whichever
    # host has the most free quota left rather than hammering one key).
    # ------------------------------------------------------------------ #
    "deepinfra": {
        "name": "DeepInfra",
        "base_url": "https://api.deepinfra.com/v1/openai",
        "models_url": "https://api.deepinfra.com/v1/openai/models",
        "signup_url": "https://deepinfra.com/login",
        "key_hint": "deepinfra_...",
        "free_filter": "family",
        "free_exact": False,
        "free_families": [
            "gpt-oss-120b", "gpt-oss-20b", "meta-llama/Llama-3.3-70B-Instruct",
            "meta-llama/Llama-3.1-8B-Instruct", "deepseek-ai/DeepSeek-V3",
            "deepseek-ai/DeepSeek-R1", "meta-llama/Llama-3.2-3B-Instruct",
            "Qwen/Qwen2.5-72B-Instruct", "Qwen/Qwen2.5-Coder-32B-Instruct",
            "Qwen/QwQ-32B", "google/gemma-3-27b-it",
            "NousResearch/Hermes-3-Llama-3.1-70B",
            "Qwen/Qwen2.5-7B-Instruct", "meta-llama/Llama-3.3-8B-Instruct",
        ],
        "default_free_models": [
            "meta-llama/Llama-3.3-70B-Instruct",
            "deepseek-ai/DeepSeek-R1",
            "deepseek-ai/DeepSeek-V3",
            "Qwen/QwQ-32B",
            "Qwen/Qwen2.5-72B-Instruct",
            "Qwen/Qwen2.5-Coder-32B-Instruct",
            "meta-llama/Llama-3.2-3B-Instruct",
            "google/gemma-3-27b-it",
            "NousResearch/Hermes-3-Llama-3.1-70B",
        ],
        "notes": "Serverless OpenAI-compatible inference. Free = DeepInfra's legacy Open Source models (no card to use them; a card only unlocks the PAID catalog). Covers many of the same flagship ids as the rest of the fleet, so the hub can spread a single model across hosts. Limit ~10 req/min on the free OS models (real 429s sideline it); billable if you pin a non-OS id.",
    },
    "together": {
        "name": "Together AI",
        "base_url": "https://api.together.xyz/v1",
        "models_url": "https://api.together.xyz/v1/models",
        "signup_url": "https://api.together.xyz/settings/api-keys",
        "key_hint": "tgp_...",
        "free_filter": "family",
        "free_exact": False,
        "free_families": ["-free"],
        "default_free_models": [
            "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",
            "meta-llama/Llama-Vision-Free",
            "deepseek-ai/DeepSeek-R1-Distill-Llama-70B-Free",
        ],
        "notes": "Free tier on the '-Free'/'-Turbo-Free' slugs (no card to start). Shared with OpenRouter/NVIDIA/sambanova on llama-3.3-70b and deepseek-r1 so the hub load-balances. Paid: the non-Free slugs and Qwen3-235B/Llama-4-Maverick (only reachable via an explicit '<pid>/<model>' pin).",
    },
    "hyperbolic": {
        "name": "Hyperbolic",
        "base_url": "https://api.hyperbolic.xyz/v1",
        "models_url": "https://api.hyperbolic.xyz/v1/models",
        "signup_url": "https://app.hyperbolic.xyz/keys",
        "key_hint": "sk-...",
        "free_filter": "family",
        "free_exact": True,
        "free_families": [
            "Qwen/QwQ-32B",
            "deepseek-ai/DeepSeek-R1",
            "deepseek-ai/DeepSeek-V3",
            "meta-llama/Llama-3.3-70B-Instruct",
            "meta-llama/Llama-3.2-3B-Instruct",
            "Qwen/Qwen2.5-72B-Instruct",
            "Qwen/Qwen2.5-Coder-32B-Instruct",
            "NousResearch/Hermes-3-Llama-3.1-70B",
        ],
        "default_free_models": [
            "Qwen/QwQ-32B",
            "deepseek-ai/DeepSeek-R1",
            "deepseek-ai/DeepSeek-V3",
            "meta-llama/Llama-3.3-70B-Instruct",
            "meta-llama/Llama-3.2-3B-Instruct",
            "Qwen/Qwen2.5-72B-Instruct",
            "Qwen/Qwen2.5-Coder-32B-Instruct",
            "NousResearch/Hermes-3-Llama-3.1-70B",
        ],
        "notes": "OpenAI-compatible. Free = the 'Community' model set; 'Premium' ids bill (and share id shapes with Community ones, so we pin exactly). Shares deepseek-r1/v3 + llama-3.3-70b with the rest of the fleet for cross-provider failover. Free Community models limited to ~1 req/s - real 429s sideline it.",
    },
    "nebius": {
        "name": "Nebius AI",
        "base_url": "https://api.tokenfactory.nebius.com/v1",
        "models_url": "https://api.tokenfactory.nebius.com/v1/models",
        "signup_url": "https://nebius.ai/",
        "key_hint": "sk-...",
        "free_filter": "family",
        "free_exact": True,
        "free_families": [
            "meta-llama/Llama-3.3-70B-Instruct",
            "Qwen/Qwen2.5-32B-Instruct",
            "Qwen/Qwen2.5-Coder-32B-Instruct",
            "deepseek-ai/DeepSeek-V3",
            "google/gemma-2-9b-it",
            "meta-llama/Llama-3.1-8B-Instruct",
        ],
        "default_free_models": [
            "meta-llama/Llama-3.3-70B-Instruct",
            "Qwen/Qwen2.5-32B-Instruct",
            "Qwen/Qwen2.5-Coder-32B-Instruct",
            "deepseek-ai/DeepSeek-V3",
            "meta-llama/Llama-3.1-8B-Instruct",
        ],
        "notes": "OpenAI-compatible (tokenfactory endpoint). Now offers a small no-card free tier on common open models - shares llama-3.3-70b / deepseek-v3 / qwen2.5 with the fleet. Free tier rate-limited by Nebius; real 429s sideline it.",
    },
    "cohere": {
        "name": "Cohere",
        "base_url": "https://api.cohere.com/compatibility/v1",
        "models_url": "https://api.cohere.com/compatibility/v1/models",
        "signup_url": "https://dashboard.cohere.com/api-keys",
        "key_hint": "sk-...",
        "free_filter": "family",
        "free_exact": True,
        "free_families": [
            "command-r-plus-08-2024",
            "command-r-08-2024",
            "command-r7b-12-2024",
            "command-a-03-2025",
            "command-a-vision-07-2025",
            "command-a-reasoning-08-2025",
        ],
        "default_free_models": [
            "command-r-plus-08-2024",
            "command-r-08-2024",
            "command-r7b-12-2024",
            "command-a-03-2025",
        ],
        "notes": "OpenAI-compatible via the /compatibility/v1 layer. Free Community/Command tier on fixed ids (no card). Distinct model family (command-*) that the rest of the fleet does not cover, so it adds capability rather than overlap.",
    },
    "scaleway": {
        "name": "Scaleway Inference",
        "base_url": "https://api.scaleway.ai/v1",
        "models_url": "https://api.scaleway.ai/v1/models",
        "signup_url": "https://console.scaleway.com/iam/api-keys",
        "key_hint": "scw_...",
        "free_filter": "family",
        "free_exact": True,
        "free_families": [
            "qwen3-235b-a22b-instruct-2507",
            "llama-3.1-70b-instruct",
            "llama-3.1-8b-instruct",
            "mistral-small-3.2-24b-instruct-2506",
            "deepseek-v3-0324",
            "gpt-oss-120b",
        ],
        "default_free_models": [
            "qwen3-235b-a22b-instruct-2507",
            "llama-3.1-70b-instruct",
            "llama-3.1-8b-instruct",
            "mistral-small-3.2-24b-instruct-2506",
            "gpt-oss-120b",
        ],
        "notes": "EU/GDPR OpenAI-compatible host with a no-card free tier (Paris) on a fixed set of open models - shares gpt-oss-120b / llama-3.1-70b with the fleet for failover. Free tier rate-limited by Scaleway; real 429s sideline it. Do NOT pin the paid ':paid' product ids.",
    },
    "stepfun": {
        "name": "StepFun",
        "base_url": "https://api.stepfun.com/v1",
        "models_url": "https://api.stepfun.com/v1/models",
        "signup_url": "https://platform.stepfun.com/",
        "key_hint": "sk-...",
        "free_filter": "family",
        "free_exact": False,
        "free_families": ["step-3.7-flash", "step-3.5-flash"],
        "default_free_models": [
            "step-3.7-flash",
            "step-3.5-flash",
        ],
        "notes": "OpenAI-compatible. Free tier on the step-3.x-flash slugs (no card). Already overlaps with Kilo Code's 'stepfun/step-3.7-flash:free' relay, so the hub can spread step-3.7-flash across both hosts.",
    },
    "aion": {
        "name": "Aion Labs",
        "base_url": "https://api.aionlabs.ai/v1",
        "models_url": "https://api.aionlabs.ai/v1/models",
        "signup_url": "https://aionlabs.ai",
        "key_hint": "sk-...",
        "free_filter": "family",
        "free_exact": True,
        "free_families": [
            "aion-labs/aion-3.0",
            "aion-labs/aion-3.0-mini",
            "aion-labs/aion-2.5",
            "aion-labs/aion-2.0",
            "aion-labs/aion-rp-llama-3.1-8b",
        ],
        "default_free_models": [
            "aion-labs/aion-3.0",
            "aion-labs/aion-3.0-mini",
            "aion-labs/aion-2.5",
            "aion-labs/aion-2.0",
        ],
        "notes": "OpenAI-compatible aggregator with a no-card free key (20k tok/day). First-party Aion models; pin exactly so paid siblings stay out of free routing. Adds capability the rest of the fleet lacks.",
    },
    "sealion": {
        "name": "SEA-LION (AI Singapore)",
        "base_url": "https://api.sea-lion.ai/v1",
        "models_url": "https://api.sea-lion.ai/v1/models",
        "signup_url": "https://sea-lion.ai",
        "key_hint": "sk-...",
        "free_filter": "family",
        "free_exact": True,
        "free_families": [
            "aisingapore/Llama-SEA-LION-v3.5-70B-R",
            "aisingapore/Llama-SEA-LION-v3-70B-IT",
            "aisingapore/Gemma-SEA-LION-v4-27B-IT",
            "aisingapore/Qwen-SEA-LION-v4.5-27B-IT",
            "aisingapore/Qwen-SEA-LION-v4-32B-IT",
        ],
        "default_free_models": [
            "aisingapore/Llama-SEA-LION-v3.5-70B-R",
            "aisingapore/Llama-SEA-LION-v3-70B-IT",
            "aisingapore/Llama-SEA-LION-v3-70B-IT",
            "aisingapore/Qwen-SEA-LION-v4.5-27B-IT",
            "aisingapore/Qwen-SEA-LION-v4-32B-IT",
        ],
        "notes": "OpenAI-compatible first-party API (AI Singapore). Free key via Google sign-in (no card), 10 RPM. SEA-LION regional models (Llama/Gemma/Qwen variants) add capability the fleet does not cover.",
    },
    "requesty": {
        "name": "Requesty",
        "base_url": "https://router.requesty.ai/v1",
        "models_url": "https://router.requesty.ai/v1/models",
        "signup_url": "https://requesty.ai/",
        "key_hint": "sk-...",
        "free_filter": "suffix_free",
        "default_free_models": [
            "openai/gpt-oss-120b:free",
        ],
        "notes": "Model router aggregating many upstream providers. Free = pass-through free ids (OpenRouter ':free' convention). Shares gpt-oss-120b with the fleet; real 429s sideline it. Reach any upstream model via an explicit 'requesty/<id>' pin.",
    },
    "custom": {
        "name": "Custom (OpenAI-compatible)",
        "base_url": None,  # user supplies via per-provider config base_url
        "models_url": None,
        "signup_url": None,
        "key_hint": "any",
        "free_filter": "all",
        "default_free_models": [],
        "notes": "Any OpenAI-compatible endpoint. You provide the base URL + key + models.",
    },
}

FREE_FILTERS = ("suffix_free", "pricing_zero", "all", "family", "free_tier")

# --------------------------------------------------------------------------- #
# SAFETY: block uncensored / abliterated / NSFW / jailbreak models
# --------------------------------------------------------------------------- #
# These fine-tunes strip safety guardrails and are a real liability if served to
# end users. We block them from being discovered, selected, or run — regardless
# of provider. Case-insensitive. Extend freely.
_BLOCK_PATTERNS: List[str] = [
    r"abliterat",          # abliterated / abliteration
    r"uncensor",           # uncensored
    r"unfiltered",
    r"unhinged",
    r"unaligned",
    r"no[-_ ]?guardrail",
    r"no[-_ ]?moderation",
    r"jailbreak",
    r"jailbroken",
    r"\bnsfw\b",
    r"\berp\b",            # erotic roleplay
    r"\bdolphin\b",        # dolphin-* fine-tunes are explicitly uncensored
    r"\bventice\b|\bvenice\b",  # Venice AI = uncensored-by-design
    r"\blewd\b",
    r"pornographic|porn\b",
    r"\btoxic\b",
    r"\bdegenerate\b",
]
_BLOCK_RE = re.compile("|".join(_BLOCK_PATTERNS), re.IGNORECASE)


def is_model_allowed(model_id: Optional[str]) -> bool:
    """Return False for uncensored/abliterated/NSFW/jailbreak models.

    Mainstream models (deepseek, llama, qwen, claude, gpt-*, ...) are never
    affected since none of the block patterns match their ids.
    """
    if not model_id:
        return False
    mid = str(model_id)
    if _BLOCK_RE.search(mid):
        return False  # block wins — never serve an uncensored fine-tune
    return True


# Non-chat models (audio / OCR / embeddings / moderation / image) — excluded from
# the chat free-model list so the gateway never picks e.g. Whisper for text gen.
# These are NOT "paid" and NOT "dead": they're a different API surface, so they
# hard-fail on /chat/completions no matter what key or quota you have.
#
# The second block was added from a LIVE 150-model bulk test: every id there was
# observed failing on /chat/completions with this exact key. Without them,
# free_filter='all' leaked them into routing (11 of mistral's 13 failures).
_NON_CHAT_PATTERNS = [
    r"whisper", r"\btts\b", r"text-to-speech", r"\bstt\b", r"speech",
    r"orpheus", r"canopylabs", r"parler", r"bark",  # TTS voice models
    r"embed", r"rerank", r"moderation", r"guard", r"safeguard",
    r"stable-diffusion", r"\bflux\b", r"\bsdxl\b", r"image-gen", r"\bdall",
    # --- verified non-chat by live bulk test (2026-07-15) ---
    r"\bocr\b",          # mistral-ocr-* -> HTTP 400 (document OCR, not chat)
    r"transcribe",       # voxtral-mini-transcribe-* -> 400
    r"realtime",         # voxtral-mini-realtime-*, *-realtime-* -> 400 (streaming audio API)
    r"voxtral-mini",     # audio-only; NOTE voxtral-SMALL *is* chat-capable and must stay
    r"native-audio",     # gemini-*-native-audio-* -> 404 on chat (Live API surface)
    r"live-preview",     # gemini-*-live-preview -> 404 on chat (Live API surface)
    # Image GENERATION ids (gemini-2.5-flash-image, *-pro-image). Anchored to a
    # trailing '-image' so multimodal CHAT models that merely accept images
    # (llama-3.2-11b-vision-instruct, *-vl-*) are NOT caught.
    r"[-_/]image(?:[-_.]|$)",
]
_NONCHAT_RE = re.compile("|".join(_NON_CHAT_PATTERNS), re.IGNORECASE)


def is_free_model(provider_id: str, model_id: Optional[str],
                  is_free_tier: bool = True,
                  known_free: Optional[List[str]] = None) -> bool:
    """True if `model_id` is actually inside `provider_id`'s FREE catalog.

    Guards a pinned model (e.g. set via the dashboard) from smuggling a PAID
    model into the free system — a 'family'-filtered provider's flagship
    (qwen-max), a non-':free' OpenRouter variant, etc. `known_free`, when given
    (the provider's live/cached discovered free list), wins; otherwise falls
    back to a static check against the registry's own free_filter rule.

    `is_free_tier=False` short-circuits to False (the row isn't claiming to be
    free, so nothing qualifies as a "free model" for it).
    """
    if not is_free_tier:
        return False
    if not model_id:
        return False
    prov = PROVIDERS.get(provider_id)
    if not prov:
        return False
    if prov.get("paid"):
        return False  # a provider-level paid gateway is never "free"
    mid = str(model_id)
    # Per-provider PAID exclusions. Needed where a paid id can't be told from a
    # free one by the provider's own filter rule — e.g. SiliconFlow ships a PAID
    # 'Pro/'-prefixed twin of each free model ('Pro/Qwen/Qwen2.5-7B-Instruct'),
    # which a family/substring match on the free id matches too. Checked before
    # every filter below so no rule can re-admit an excluded id.
    for ex in (prov.get("exclude_families") or []):
        if str(ex).lower() in mid.lower():
            return False
    if known_free:
        low_free = {str(k).lower() for k in known_free}
        return mid.lower() in low_free
    free_filter = prov.get("free_filter", "all")
    low = mid.lower()
    if free_filter == "suffix_free":
        # Default ':free' matches openrouter's convention; a provider whose own
        # catalog marks free ids with a DIFFERENT suffix (opencode-zen uses a
        # bare '-free', e.g. 'mimo-v2.5-free') overrides via free_suffix so its
        # free-model list stays genuinely LIVE — new/removed '-free' ids are
        # picked up automatically instead of needing a hand-maintained
        # default_free_models list re-verified by hand every time it drifts.
        return low.endswith(str(prov.get("free_suffix") or ":free"))
    if free_filter == "family":
        families = [f.lower() for f in (prov.get("free_families") or [])]
        if not families:
            return False
        if prov.get("free_exact"):
            # Exact-id match: the provider's free set is a fixed named list AND
            # a paid id has a free id as its prefix (glm-4.7-flash is a
            # substring of the PAID glm-4.7-flashX), so substring matching would
            # leak the paid model. Fails closed on unseen snapshot ids, which is
            # the safe direction — default_free_models still covers the fallback.
            return low in families
        return any(fam in low for fam in families)
    if free_filter == "pricing_zero":
        # Live pricing can't be verified without a fetch; without a
        # known_free list to check against, don't claim a free-ness we can't
        # prove (fail closed — the caller falls back to its discovered list).
        return False
    if free_filter == "free_tier":
        # The tier label lives in the live catalog, so with no known_free list
        # there is nothing here to read it from. Fall back to the row's own
        # hand-verified defaults rather than to False: those ids were confirmed
        # free by hand, and answering False for them would make a legitimately
        # pinned default unroutable (is_free_model also guards dashboard pins,
        # not just discovery). Anything NOT on that list still fails closed, so
        # a paid 'pro' id can never sneak through on the static path.
        return low in {str(m).lower() for m in (prov.get("default_free_models") or [])}
    return True  # 'all' -> the whole listed catalog is free


def is_chat_model(model_id: Optional[str]) -> bool:
    """False for non-chat models (audio/embeddings/moderation/image generators)."""
    if not model_id:
        return False
    return not _NONCHAT_RE.search(str(model_id))


def filter_models(model_ids: List[str]) -> List[str]:
    """Drop blocked (uncensored) AND non-chat models, preserving order."""
    return [m for m in (model_ids or []) if is_model_allowed(m) and is_chat_model(m)]


# --------------------------------------------------------------------------- #
# Accessors
# --------------------------------------------------------------------------- #
def get_provider(provider_id: str) -> Optional[dict]:
    return PROVIDERS.get(provider_id)


def list_providers(include_custom: bool = False) -> List[dict]:
    out = []
    for pid, meta in PROVIDERS.items():
        if pid == "custom" and not include_custom:
            continue
        out.append({"id": pid, **meta})
    return out


def signup_url(provider_id: str) -> Optional[str]:
    p = PROVIDERS.get(provider_id)
    return p.get("signup_url") if p else None


def base_url_for(provider_id: str, custom_base: Optional[str] = None) -> Optional[str]:
    """Resolve a provider's base URL. A user-set `custom_base` ALWAYS wins.

    It used to be honored ONLY for pid=="custom"/unknown ids, which made the
    dashboard's per-provider "Advanced: custom base URL" field a no-op for all
    ~53 known providers: config.py stores it, the API saves it, _upstream_chat
    passes it in — and this function dropped it on the floor. An explicit
    override the user typed must take effect.

    It also makes account-scoped providers expressible: Cloudflare Workers AI's
    base is `.../accounts/{account_id}/ai/v1`, so the registry row can only carry
    a template and the user pastes their resolved URL here.
    """
    if isinstance(custom_base, str) and custom_base.strip():
        return custom_base.strip()
    if provider_id == "custom" or (provider_id not in PROVIDERS):
        return custom_base
    return PROVIDERS[provider_id].get("base_url")


def is_known_provider(provider_id: str) -> bool:
    return provider_id in PROVIDERS
