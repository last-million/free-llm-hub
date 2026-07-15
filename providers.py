"""
Free LLM Hub — Provider Registry (source of truth).

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
        "default_free_models": [
            "meta-llama/llama-3.3-70b-instruct:free",
            "qwen/qwen3-next-80b-a3b-instruct:free",
            "nvidia/nemotron-3-super-120b-a12b:free",
            "openai/gpt-oss-20b:free",
            "google/gemma-4-31b-it:free",
            "qwen/qwen3-coder:free",
            "nousresearch/hermes-3-llama-3.1-405b:free",
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
        "free_filter": "family",
        "free_families": ["llama-3.1-8b-instant", "llama-3.3-70b-versatile", "llama-4-scout",
                          "gpt-oss", "qwen3-32b", "qwen3.6-27b", "compound", "allam-2-7b"],
        "default_free_models": [
            "llama-3.3-70b-versatile", "openai/gpt-oss-120b",
            "meta-llama/llama-4-scout-17b-16e-instruct", "openai/gpt-oss-20b",
            "qwen/qwen3-32b", "llama-3.1-8b-instant",
        ],
        "notes": "Extremely fast. Free tier, no card. ~1,000 req/day per model (llama-3.1-8b-instant: 14,400/day).",
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
    "nvidia": {
        "name": "NVIDIA NIM",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "models_url": "https://integrate.api.nvidia.com/v1/models",
        "signup_url": "https://build.nvidia.com/settings/api-keys",
        "key_hint": "nvapi-...",
        "paid": True,  # TRIAL credits, NOT a free tier — keep OUT of free routing
        "free_filter": "pricing_zero",
        "default_free_models": [],
        "notes": "TRIAL, not a free tier — 1,000 lifetime credits (max 5,000 via business email), 90-day expiry, then HTTP 402 'Cloud credits expired'. Not renewable: every remote call to an NVIDIA-hosted endpoint spends the balance. Self-hosting the NIM containers is separately free for Developer Program members.",
    },
    "morph": {
        "name": "Morph",
        "base_url": "https://api.morphllm.com/v1",
        "models_url": "https://api.morphllm.com/v1/models",
        "signup_url": "https://morphllm.com/dashboard",
        "key_hint": "sk-...",
        "paid": True,  # credit allowance, NOT a free tier — keep OUT of free routing
        "free_filter": "pricing_zero",
        "default_free_models": [],
        "notes": "No free models — all 8 models bill per token. The '200 req free every month' headline actually meters TOKENS ($2.50 / 250K credits per month): a coding CLI's 20-50K-token turns make the real allowance ~5-12 requests/month.",
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
    "cloudflare": {
        "name": "Cloudflare Workers AI",
        # ACCOUNT-SCOPED: this template is documentation, not a usable URL. The
        # user MUST paste their resolved base into the card's "Advanced: custom
        # base URL" field (base_url_for honors it). Until they do, calls fail.
        "base_url": "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
        # Cloudflare's model list is NOT OpenAI-shaped (returns a CF envelope, and
        # the OpenAI-compat base exposes no /v1/models at all), so live discovery
        # cannot parse it and falls back to default_free_models below. That is fine.
        "models_url": None,
        "signup_url": "https://dash.cloudflare.com/sign-up",
        "key_hint": "Cloudflare API token (Workers AI scope)",
        "free_filter": "family",
        "free_families": ["@cf/meta", "@cf/openai"],
        "default_free_models": [
            "@cf/meta/llama-3.1-8b-instruct",
            "@cf/openai/gpt-oss-120b",
            "@cf/meta/llama-4-scout-17b-16e-instruct",
        ],
        "notes": ("SAFE-FREE: 10,000 Neurons/day, reset 00:00 UTC. On the Workers FREE plan "
                  "the allocation is a HARD CAP — exceeding it fails with an error, it does "
                  "NOT bill (Workers Paid bills $0.011/1k Neurons past it). Free plan is the "
                  "default, no card for the first call. "
                  "⚠ SETUP: the base URL is account-scoped — paste "
                  "https://api.cloudflare.com/client/v4/accounts/<YOUR_ACCOUNT_ID>/ai/v1 into "
                  "'Advanced: custom base URL' on this card, or nothing will work. "
                  "Quota is denominated in NEURONS (varies per model), not requests, so the "
                  "hub tracks it as UNKNOWN rather than inventing a request count. Model ids "
                  "verified from individual model pages (the index renders bare slugs without "
                  "the @cf/ prefix and is NOT a safe source)."),
    },
    "agentrouter": {
        "name": "AgentRouter",
        "base_url": "https://agentrouter.org/v1",
        "models_url": "https://agentrouter.org/v1/models",
        "signup_url": "https://agentrouter.org/console/token",
        "key_hint": "sk-...",
        "paid": True,  # consumable signup credits only — keep OUT of free routing
        # The previous free_families/default_free_models here were FABRICATED
        # upstream: they trace verbatim to a single referral-spam gist (which
        # appears to have borrowed OpenRouter's ':free' reputation), not to any
        # official source. deepseek-v2-lite/qwen2-7b/mistral-7b are 2024-era
        # models no 2026 Claude-relay would serve. Removed rather than re-tuned.
        "free_filter": "pricing_zero",
        "free_families": [],
        "default_free_models": [],
        "notes": "No free models — a third-party API relay running on consumable signup credits ($100 via GitHub, $200 via referral); every call burns them. Publishes no rate limits and /v1/models 401s without a key, so nothing here can be verified as free. All prompts transit a third-party reseller. Consider removing.",
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
        "free_filter": "family",
        "free_families": ["flash", "gemma", "2.5-pro"],
        "default_free_models": [
            "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-3.5-flash",
            "gemini-3.1-flash-lite", "gemini-3-flash-preview", "gemini-2.5-pro",
            "gemini-2.0-flash", "gemma-4-31b-it",
        ],
        "notes": "Free tier = Flash family + Gemma + 2.5-Pro. ToS: free-tier prompts/responses may be used to improve Google's products outside EU/UK/CH.",
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
        # Live router catalog: is_free:true matches EXACTLY 0 of 102 models, so
        # 'all' was admitting 100% paid inventory as free. NOTE for any future
        # pricing_zero implementation: HF nests pricing PER PROVIDER
        # (data[].providers[].pricing), and ships an explicit is_free boolean
        # that outranks pricing==0.
        "free_filter": "pricing_zero",
        "default_free_models": [],
        "notes": "No free models — a $0.10/month credit allowance consumed at full pay-as-you-go rates (~17 requests on GLM-5.2, ~1,400 on Llama-3.1-8B), 'subject to change'. Note the widely-cited '1,000 requests / 5 min' is the Hub API bucket, NOT inference.",
    },
    "github-models": {
        "name": "GitHub Models",
        "base_url": "https://models.github.ai/inference",
        "models_url": "https://models.github.ai/catalog/models",
        "signup_url": "https://github.com/settings/tokens",
        "key_hint": "github_pat_... (models:read)",
        # Not a pricing leak (every catalog model is $0) — but 'all' surfaced 10
        # custom-tier OpenAI ids (gpt-5*, o1*, o3*, o4-mini) marked "Not
        # applicable" on Copilot Free, i.e. guaranteed hard failures, plus 2
        # embeddings models. These 6 families select exactly the 23 low/high-tier
        # models. deepseek-r1 is deliberately excluded by the narrow family:
        # it's free but capped at 8 req/day.
        "free_filter": "family",
        "free_families": ["openai/gpt-4", "meta/", "mistral-ai/", "microsoft/",
                          "cohere/", "deepseek/deepseek-v3"],
        # Low-tier (150/day) before high-tier (50/day). Lowercase ids: the live
        # catalog no longer uses the old CamelCase Azure names.
        "default_free_models": [
            "openai/gpt-4.1-mini", "openai/gpt-4o-mini", "mistral-ai/mistral-medium-2505",
            "cohere/cohere-command-a", "microsoft/phi-4", "openai/gpt-4.1",
            "meta/llama-3.3-70b-instruct", "deepseek/deepseek-v3-0324",
        ],
        "notes": ("Genuinely free with a GitHub PAT — nothing is consumed. 150 req/day (low-tier ids) "
                  "/ 50 req/day (high-tier). "
                  "⚠ THE TOKEN NEEDS THE 'models' SCOPE, or EVERY call returns 403 'No access to "
                  "model' even though the catalog lists fine (a key test that only lists models will "
                  "look OK - verified live: 0 of 23 models worked without it). "
                  "Fine-grained token: github.com/settings/personal-access-tokens/new -> Permissions -> "
                  "Account permissions -> Models: Read. "
                  "Classic token: github.com/settings/tokens/new -> tick the 'models' scope. "
                  "No repo access is needed - Models is an ACCOUNT permission, so grant nothing else. "
                  "GOTCHA: the free tier caps EVERY request at 8K in / 4K out regardless of the "
                  "model's advertised context."),
    },
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "models_url": "https://api.deepseek.com/v1/models",
        "signup_url": "https://platform.deepseek.com/api_keys",
        "key_hint": "sk-...",
        "free_filter": "pricing_zero",
        "paid": True,  # PAID/credit-based, NOT a free tier — only surface when paid models are allowed
        "default_free_models": [],
        "notes": "PAID (credit-based) — not a free tier, but explicitly allowed: pin 'deepseek/deepseek-v4-flash' or 'deepseek/deepseek-v4-pro' explicitly to use it. Both models bill per token; the widely-cited '5M free tokens on signup' appears on ZERO official pages. Only CONCURRENCY is published (no RPD/RPM) — the real limiter is account balance. Legacy deepseek-chat/deepseek-reasoner retire 2026-07-24 (alias to deepseek-v4-flash/pro).",
    },
    "together": {
        "name": "Together AI",
        "base_url": "https://api.together.ai/v1",
        "models_url": "https://api.together.ai/v1/models",
        "signup_url": "https://api.together.ai/settings/api-keys",
        "key_hint": "...",
        "paid": True,  # no free endpoints remain — keep OUT of free routing
        # free_families ['-free'] now matches ZERO live models and would leak a
        # billable one if Together ever ships an id containing '-free'.
        # pricing_zero is self-correcting: empty today, auto-picks up a real $0
        # endpoint later. CAVEAT for that implementation: test input==0 AND
        # output==0 only — Together's own docs show a paid model ($0.30/M) with
        # base:0, finetune:0, hourly:0.
        "free_filter": "pricing_zero",
        "free_families": [],
        "default_free_models": [],
        "notes": "No free tier — all four '-Free' serverless endpoints were removed during 2025 (Llama-Vision-Free 2025-08-28, Llama-3.3-70B-Turbo-Free and DeepSeek-R1-Distill-Llama-70B-free 2025-11-13, FLUX.1-schnell-free 2025-12-23). Paid credits only; the '$25 signup credit' was retired July 2025.",
    },
    "scaleway": {
        "name": "Scaleway",
        "base_url": "https://api.scaleway.ai/v1",
        "models_url": "https://api.scaleway.ai/v1/models",
        "signup_url": "https://console.scaleway.com/iam/api-keys",
        "key_hint": "...",
        "paid": True,  # card mandatory + silent billing — keep OUT of free routing
        "free_filter": "pricing_zero",
        "default_free_models": [],
        "notes": "No free tier — a validated payment method is MANDATORY before the first call. The only free part is a one-time 1,000,000-token allowance for new customers; after it, calls do NOT fail, they silently bill the card (llama-3.3-70b EUR 0.90/0.90 per M, glm-5.2 EUR 1.80/5.50) and NO response header exposes the remaining free tokens, so the switchover is undetectable. The old 'free beta' ended — the Generative API is GA with published per-token pricing.",
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
        "notes": "PERMANENT free ($0 in/out): GLM-4.7-Flash (200K ctx), GLM-4.5-Flash, GLM-4.6V-Flash (vision). Note glm-4.7-FlashX is PAID despite the name. International z.ai (email/Google signup, no China phone). ~1 req/s, 1 concurrent; Z.AI publishes no request quota.",
    },
    "kimi": {
        "name": "Kimi (Moonshot)",
        "base_url": "https://api.moonshot.ai/v1",
        "models_url": "https://api.moonshot.ai/v1/models",
        "signup_url": "https://platform.moonshot.ai/console/api-keys",
        "key_hint": "sk-...",
        "paid": True,  # docs: "There is no free tier" — keep OUT of free routing
        "free_filter": "pricing_zero",
        "trial": True,
        "default_free_models": [],
        "notes": "No free tier — docs verbatim: 'There is no free tier. To prevent abuse, you need to recharge at least $1 to start using.' All 11 models are paid, so calls either hard-fail on an unfunded account or bill per token. The only documented incentive is a $5 voucher AFTER $5 of cumulative recharge (a rebate, not credit). For free Kimi weights, route via OpenRouter's moonshotai/* ':free' variants instead.",
    },
    "minimax": {
        "name": "MiniMax",
        "base_url": "https://api.minimax.io/v1",
        # GET /v1/models IS documented now (this "no endpoint" note was stale),
        # but discovery stays off deliberately: with the classification fixed
        # (paid=True), is_free_model() rejects every id anyway, so enabling it
        # would only enumerate a paid catalog. Wire it up only alongside a real
        # zero-pricing check.
        "models_url": None,
        "signup_url": "https://platform.minimax.io/user-center/basic-information/interface-key",
        "key_hint": "...",
        "paid": True,  # NO free tier + fails by BILLING — keep OUT of free routing
        "free_filter": "pricing_zero",
        "trial": True,
        "default_free_models": [],
        "notes": "No free tier — the word 'free' appears nowhere in MiniMax's pricing docs (per-token billing or monthly subscription only). DANGEROUS: MiniMax-M2/M2.5 are real ids that cost ~$0.30/$1.20 per M, so calls SUCCEED, silently burn the ~30-day trial credits, then bill real money — nothing surfaces the mistake. Global api.minimax.io (China = api.minimaxi.com).",
    },
    "qwen": {
        "name": "Qwen (Alibaba Model Studio)",
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "models_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/models",
        "signup_url": "https://bailian.console.alibabacloud.com/",
        "key_hint": "sk-...",
        "paid": True,  # 90-day consumable trial, NOT a free tier — keep OUT of free routing
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
    "nebius": {
        "name": "Nebius AI Studio",
        "base_url": "https://api.studio.nebius.com/v1",
        "models_url": "https://api.studio.nebius.com/v1/models",
        "signup_url": "https://studio.nebius.com/",
        "key_hint": "...",
        "paid": True,  # $1 trial credit + card required — keep OUT of free routing
        # pricing_zero is honest AND self-correcting here: GET
        # /v1/models?verbose=true returns a Pricing object, so it matches nothing
        # today and would auto-pick up a real $0 model later.
        "free_filter": "pricing_zero",
        "trial": True,
        "default_free_models": [],
        "notes": "No free tier — $1 trial credit valid 30 days, and a bank card (or bank transfer) IS required at onboarding; no $0 models. All 60+ models are paid. Note: 'Nebius AI Studio' is now 'Nebius Token Factory' (canonical base api.tokenfactory.nebius.com/v1); the studio host above is aliased, not broken.",
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
    "xiaomi": {
        "name": "Xiaomi MiMo",
        "base_url": "https://api.xiaomimimo.com/v1",
        "models_url": "https://api.xiaomimimo.com/v1/models",
        "signup_url": "https://platform.xiaomimimo.com/",
        "key_hint": "MiMo API key from platform.xiaomimimo.com",
        "paid": True,
        "free_filter": "pricing_zero",
        "default_free_models": [],
        "notes": "Xiaomi MiMo-V2.5-Pro — OpenAI-compatible, paid (~$1/$3 per M tokens). Reasoning + multimodal.",
    },
    # ── OpenCode / models.dev free-capable providers (July 2026 catalog) ───────
    # All OpenAI-compatible; the SAFETY block below still strips any uncensored
    # models post-discovery. Routers use 'pricing_zero' so only $0 models surface.
    "opencode-zen": {
        "name": "OpenCode Zen",
        "base_url": "https://opencode.ai/zen/v1",
        "models_url": "https://opencode.ai/zen/v1/models",
        "signup_url": "https://opencode.ai/auth",
        "key_hint": "sk-...",
        "free_filter": "pricing_zero",
        "default_free_models": ["deepseek-v4-flash-free", "minimax-m2.5-free"],
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
    "chutes": {
        "name": "Chutes",
        "base_url": "https://llm.chutes.ai/v1",
        "models_url": "https://llm.chutes.ai/v1/models",
        "signup_url": "https://chutes.ai",
        "key_hint": "cpk_...",
        "paid": True,  # free tier retired 2026-03-15 — keep OUT of free routing
        # pricing_zero is mechanically supported here (the keyless endpoint DOES
        # expose pricing:{prompt,completion}) and correct under either the TEE or
        # full catalog — it just matches nothing today, which is the truth.
        "free_filter": "pricing_zero",
        "default_free_models": [],
        "notes": "No free tier — fully retired 2026-03-15 (and the prior '200 free requests/day' Early Access always required a $5 deposit). Every one of the 13 live models is paid, $0.0245/$0.0978 up to GLM-5.2-TEE at $1.40/$4.40 per M. Subscription or pay-per-use only. NOTE: Chutes is a major upstream for OpenRouter's ':free' variants — those are free because OPENROUTER subsidizes them, and this hub already has that access via the openrouter entry.",
    },
    "targon": {
        "name": "Targon",
        "base_url": "https://api.targon.com/v1",
        "models_url": "https://api.targon.com/v1/models",
        "signup_url": "https://targon.com/",
        "key_hint": "sn4_...",
        "free_filter": "all",
        "default_free_models": ["deepseek-ai/DeepSeek-V3", "deepseek-ai/DeepSeek-R1"],
        "notes": "Bittensor-backed inference; free tier for open models. OpenAI-compatible.",
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
    "deepinfra": {"name": "DeepInfra", "base_url": "https://api.deepinfra.com/v1/openai",
        "models_url": "https://api.deepinfra.com/v1/openai/models", "signup_url": "https://deepinfra.com/dash/api_keys",
        "key_hint": "any", "free_filter": "all", "default_free_models": [], "paid": True, "notes": "Cheap OpenAI-compatible host for open models. Pay-as-you-go."},
    "hyperbolic": {"name": "Hyperbolic", "base_url": "https://api.hyperbolic.xyz/v1",
        "models_url": "https://api.hyperbolic.xyz/v1/models", "signup_url": "https://app.hyperbolic.ai/settings",
        "key_hint": "any", "free_filter": "all", "default_free_models": [], "paid": True, "notes": "Low-cost open-model inference. OpenAI-compatible."},
    "baseten": {"name": "Baseten", "base_url": "https://inference.baseten.co/v1",
        "models_url": "https://inference.baseten.co/v1/models", "signup_url": "https://app.baseten.co/settings/api_keys",
        "key_hint": "any", "free_filter": "all", "default_free_models": [], "paid": True, "notes": "Model hosting; trial credit then paid. OpenAI-compatible."},
    "lambda": {"name": "Lambda", "base_url": "https://api.lambda.ai/v1",
        "models_url": "https://api.lambda.ai/v1/models", "signup_url": "https://cloud.lambda.ai/api-keys",
        "key_hint": "any", "free_filter": "all", "default_free_models": [], "paid": True, "notes": "Lambda Cloud inference. OpenAI-compatible."},
    "perplexity": {"name": "Perplexity", "base_url": "https://api.perplexity.ai",
        "models_url": None, "signup_url": "https://www.perplexity.ai/account/api/keys",
        "key_hint": "pplx-...", "free_filter": "all", "default_free_models": [], "paid": True, "notes": "Sonar models with live web search. Paid — no free tier."},
    "requesty": {"name": "Requesty", "base_url": "https://router.requesty.ai/v1",
        "models_url": "https://router.requesty.ai/v1/models", "signup_url": "https://app.requesty.ai/api-keys",
        "key_hint": "any", "free_filter": "all", "default_free_models": [], "paid": True, "notes": "Model router/aggregator. Paid."},
    "featherless": {"name": "Featherless", "base_url": "https://api.featherless.ai/v1",
        "models_url": "https://api.featherless.ai/v1/models", "signup_url": "https://featherless.ai/account/api-keys",
        "key_hint": "any", "free_filter": "all", "default_free_models": [], "paid": True, "notes": "Serverless open-model host (flat subscription). OpenAI-compatible."},
    "arcee": {"name": "Arcee", "base_url": "https://models.arcee.ai/v1",
        "models_url": "https://models.arcee.ai/v1/models", "signup_url": "https://conductor.arcee.ai/",
        "key_hint": "any", "free_filter": "all", "default_free_models": [], "paid": True, "notes": "Arcee small-model host. Paid."},
    "inception": {"name": "Inception (Mercury)", "base_url": "https://api.inceptionlabs.ai/v1",
        "models_url": "https://api.inceptionlabs.ai/v1/models", "signup_url": "https://platform.inceptionlabs.ai",
        "key_hint": "any", "free_filter": "all", "default_free_models": [], "paid": True, "notes": "Diffusion-LLM (Mercury) — very fast. Paid — no free tier."},
    "302ai": {"name": "302.AI", "base_url": "https://api.302.ai/v1",
        "models_url": "https://api.302.ai/v1/models", "signup_url": "https://dash.302.ai",
        "key_hint": "any", "free_filter": "all", "default_free_models": [], "paid": True, "notes": "Multi-model aggregator. Paid."},
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

FREE_FILTERS = ("suffix_free", "pricing_zero", "all", "family")

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
        return low.endswith(":free")
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
