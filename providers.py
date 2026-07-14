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
#   'pricing_zero'-> models_url row has zero prompt+completion price
#   'all'         -> the whole listed catalog is usable on the free tier
#   'family'      -> only ids matching `free_families` are free
PROVIDERS: Dict[str, dict] = {
    "openrouter": {
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "models_url": "https://openrouter.ai/api/v1/models",
        "signup_url": "https://openrouter.ai/keys",
        "key_hint": "sk-or-...",
        "free_filter": "suffix_free",
        "default_free_models": [
            "meta-llama/llama-3.3-70b-instruct:free",
            "qwen/qwen3-coder:free",
            "deepseek/deepseek-chat:free",
        ],
        "notes": "One key unlocks many models. Free = ids ending ':free'. ~50 req/day (1000 after a one-time $10 top-up).",
    },
    "groq": {
        "name": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "models_url": "https://api.groq.com/openai/v1/models",
        "signup_url": "https://console.groq.com/keys",
        "key_hint": "gsk_...",
        "free_filter": "all",
        "default_free_models": ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "llama-3.1-8b-instant"],
        "notes": "Extremely fast. Free tier, no card. ~1,000 req/day per model.",
    },
    "cerebras": {
        "name": "Cerebras",
        "base_url": "https://api.cerebras.ai/v1",
        "models_url": "https://api.cerebras.ai/v1/models",
        "signup_url": "https://cloud.cerebras.ai/",
        "key_hint": "csk-...",
        "free_filter": "all",
        "default_free_models": ["gpt-oss-120b", "llama-3.3-70b", "llama3.1-8b"],
        "notes": "Fastest tokens/sec. Free: 14,400 req/day, 1M tok/day.",
    },
    "nvidia": {
        "name": "NVIDIA NIM",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "models_url": "https://integrate.api.nvidia.com/v1/models",
        "signup_url": "https://build.nvidia.com/settings/api-keys",
        "key_hint": "nvapi-...",
        "free_filter": "all",
        "default_free_models": ["meta/llama-3.3-70b-instruct", "nvidia/llama-3.1-nemotron-70b-instruct"],
        "notes": "Large open models incl. Llama 405B. Free key needs phone verification. ~40 req/min.",
    },
    "morph": {
        "name": "Morph",
        "base_url": "https://api.morphllm.com/v1",
        "models_url": "https://api.morphllm.com/v1/models",
        "signup_url": "https://morphllm.com/dashboard",
        "key_hint": "sk-...",
        "free_filter": "all",
        "default_free_models": [
            "morph-glm52-744b", "morph-minimax3-428b",
            "morph-dsv4flash", "morph-qwen36-27b",
        ],
        "notes": "OpenAI-compatible. Free tier ~200 req/mo + trial credits. Fast general models (GLM/MiniMax/DeepSeek/Qwen) plus fast-apply code editing.",
    },
    "google": {
        "name": "Google Gemini (AI Studio)",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "models_url": "https://generativelanguage.googleapis.com/v1beta/openai/models",
        "signup_url": "https://aistudio.google.com/apikey",
        "key_hint": "AIza...",
        "free_filter": "family",
        "free_families": ["flash", "gemma", "flash-lite"],
        "default_free_models": ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemma-3-27b-it"],
        "notes": "Free tier = Flash family + Gemma. ToS: prompts may be used for training outside EU/UK/CH.",
    },
    "mistral": {
        "name": "Mistral",
        "base_url": "https://api.mistral.ai/v1",
        "models_url": "https://api.mistral.ai/v1/models",
        "signup_url": "https://console.mistral.ai/api-keys/",
        "key_hint": "...",
        "free_filter": "all",
        "default_free_models": ["mistral-small-latest", "open-mistral-nemo", "codestral-latest"],
        "notes": "Free 'Experiment' plan requires opting into data-training + phone verification.",
    },
    "sambanova": {
        "name": "SambaNova Cloud",
        "base_url": "https://api.sambanova.ai/v1",
        "models_url": "https://api.sambanova.ai/v1/models",
        "signup_url": "https://cloud.sambanova.ai/apis",
        "key_hint": "...",
        "free_filter": "all",
        "default_free_models": ["Meta-Llama-3.3-70B-Instruct", "DeepSeek-V3-0324"],
        "notes": "Fast inference, free trial tier.",
    },
    "huggingface": {
        "name": "HuggingFace Router",
        "base_url": "https://router.huggingface.co/v1",
        "models_url": "https://router.huggingface.co/v1/models",
        "signup_url": "https://huggingface.co/settings/tokens",
        "key_hint": "hf_...",
        "free_filter": "all",
        "default_free_models": ["meta-llama/Llama-3.3-70B-Instruct", "Qwen/Qwen2.5-72B-Instruct"],
        "notes": "Unified router across HF partners. Free credit ~$0.10/month.",
    },
    "github-models": {
        "name": "GitHub Models",
        "base_url": "https://models.github.ai/inference",
        "models_url": "https://models.github.ai/catalog/models",
        "signup_url": "https://github.com/settings/tokens",
        "key_hint": "github_pat_... (models:read)",
        "free_filter": "all",
        "default_free_models": ["openai/gpt-4o-mini", "meta/Llama-3.3-70B-Instruct", "deepseek/DeepSeek-V3-0324"],
        "notes": "Auth with a GitHub PAT (models:read). Tight token limits; scales with Copilot tier.",
    },
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "models_url": "https://api.deepseek.com/v1/models",
        "signup_url": "https://platform.deepseek.com/api_keys",
        "key_hint": "sk-...",
        "free_filter": "all",
        "paid": True,  # PAID/credit-based, NOT a free tier — only surface when paid models are allowed
        "default_free_models": ["deepseek-v4-flash", "deepseek-v4-pro"],
        "notes": "PAID (credit-based) — not a free tier, but explicitly allowed. Legacy deepseek-chat/deepseek-reasoner retire 2026-07-24 (alias to deepseek-v4-flash/pro).",
    },
    "together": {
        "name": "Together AI",
        "base_url": "https://api.together.ai/v1",
        "models_url": "https://api.together.ai/v1/models",
        "signup_url": "https://api.together.ai/settings/api-keys",
        "key_hint": "...",
        "free_filter": "family",
        "free_families": ["-free"],
        "default_free_models": ["meta-llama/Llama-3.3-70B-Instruct-Turbo-Free"],
        "notes": "$25 signup credit; a few '-Free' endpoints. Mostly credit-based.",
    },
    "scaleway": {
        "name": "Scaleway",
        "base_url": "https://api.scaleway.ai/v1",
        "models_url": "https://api.scaleway.ai/v1/models",
        "signup_url": "https://console.scaleway.com/iam/api-keys",
        "key_hint": "...",
        "free_filter": "all",
        "default_free_models": ["llama-3.3-70b-instruct", "gpt-oss-120b"],
        "notes": "Free beta of the Generative API.",
    },
    # --- Chinese / additional free-tier providers (verified against official docs) ---
    "glm": {
        "name": "Z.AI (Zhipu GLM)",
        "base_url": "https://api.z.ai/api/paas/v4",
        "models_url": "https://api.z.ai/api/paas/v4/models",
        "signup_url": "https://z.ai/manage-apikey/apikey-list",
        "key_hint": "...",
        "free_filter": "family",
        "free_families": ["flash"],
        "default_free_models": ["glm-4.5-flash", "glm-4.7-flash"],
        "notes": "PERMANENT free: GLM-4.5/4.7-Flash. International z.ai (email/Google signup, no China phone). ~1 req/s.",
    },
    "kimi": {
        "name": "Kimi (Moonshot)",
        "base_url": "https://api.moonshot.ai/v1",
        "models_url": "https://api.moonshot.ai/v1/models",
        "signup_url": "https://platform.moonshot.ai/console/api-keys",
        "key_hint": "sk-...",
        "free_filter": "all",
        "trial": True,
        "default_free_models": ["moonshot-v1-8k", "kimi-k2.5"],
        "notes": "TRIAL credits only (one-time, ~small), then pay-per-token. International platform.moonshot.ai.",
    },
    "minimax": {
        "name": "MiniMax",
        "base_url": "https://api.minimax.io/v1",
        "models_url": None,  # no documented /models endpoint — use defaults
        "signup_url": "https://platform.minimax.io/user-center/basic-information/interface-key",
        "key_hint": "...",
        "free_filter": "all",
        "trial": True,
        "default_free_models": ["MiniMax-M2", "MiniMax-M2.5"],
        "notes": "TRIAL credits at signup, then pay-as-you-go. Global api.minimax.io (China = api.minimaxi.com). No /models list.",
    },
    "qwen": {
        "name": "Qwen (Alibaba Model Studio)",
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "models_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/models",
        "signup_url": "https://bailian.console.alibabacloud.com/",
        "key_hint": "sk-...",
        "free_filter": "family",
        "free_families": ["plus", "turbo", "flash", "coder", "air", "lite"],
        "default_free_models": ["qwen-plus", "qwen-turbo", "qwen3-coder-plus"],
        "notes": "FREE 1,000,000 tokens PER MODEL, 90 days. Scoped to the cheaper siblings (plus/turbo/flash/coder) so the flagship qwen-max isn't the auto-default. MUST use the International (Singapore) region base above — US/China modes get NO free quota.",
    },
    "siliconflow": {
        "name": "SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "models_url": "https://api.siliconflow.cn/v1/models",
        "signup_url": "https://cloud.siliconflow.cn/account/ak",
        "key_hint": "sk-...",
        "free_filter": "family",
        "free_families": ["qwen3-8b", "distill-qwen-7b", "deepseek-ocr"],
        "default_free_models": ["Qwen/Qwen3-8B", "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B"],
        "notes": "PERMANENT free: Qwen3-8B, DeepSeek-R1-Distill-Qwen-7B, DeepSeek-OCR (rate-limited).",
    },
    "modelscope": {
        "name": "ModelScope (Alibaba)",
        "base_url": "https://api-inference.modelscope.cn/v1",
        "models_url": "https://api-inference.modelscope.cn/v1/models",
        "signup_url": "https://modelscope.cn/my/myaccesstoken",
        "key_hint": "ms-...",
        "free_filter": "all",
        "default_free_models": ["Qwen/Qwen3-235B-A22B-Instruct", "deepseek-ai/DeepSeek-V3"],
        "notes": "Free 2,000 API calls/day (500/model) over 900+ models. Requires a ModelScope account.",
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
        "free_filter": "all",
        "trial": True,
        "default_free_models": ["meta-llama/Llama-3.3-70B-Instruct", "Qwen/Qwen2.5-72B-Instruct"],
        "notes": "Trial credits (no card). Then pay-as-you-go.",
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
        "free_filter": "all",
        "default_free_models": ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen3-235B"],
        "notes": "Decentralized (Bittensor) compute; free/cheap open models. OpenAI-compatible.",
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
        "free_filter": "all",
        "default_free_models": ["gpt-oss:120b", "qwen3-coder:480b"],
        "notes": "Ollama's hosted cloud, free tier. OpenAI-compatible.",
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
        "key_hint": "pplx-...", "free_filter": "all", "default_free_models": ["sonar", "sonar-pro"], "paid": True, "notes": "Sonar models with live web search. Paid."},
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
        "key_hint": "any", "free_filter": "all", "default_free_models": ["mercury-coder"], "paid": True, "notes": "Diffusion-LLM (Mercury) — very fast. Paid."},
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


# Non-chat models (audio / embeddings / moderation / image) — excluded from the
# chat free-model list so the gateway never picks e.g. Whisper for text gen.
_NON_CHAT_PATTERNS = [
    r"whisper", r"\btts\b", r"text-to-speech", r"\bstt\b", r"speech",
    r"orpheus", r"canopylabs", r"parler", r"bark",  # TTS voice models
    r"embed", r"rerank", r"moderation", r"guard", r"safeguard",
    r"stable-diffusion", r"\bflux\b", r"\bsdxl\b", r"image-gen", r"\bdall",
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
    if known_free:
        low_free = {str(k).lower() for k in known_free}
        return mid.lower() in low_free
    free_filter = prov.get("free_filter", "all")
    low = mid.lower()
    if free_filter == "suffix_free":
        return low.endswith(":free")
    if free_filter == "family":
        families = [f.lower() for f in (prov.get("free_families") or [])]
        return bool(families) and any(fam in low for fam in families)
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
    if provider_id == "custom" or (provider_id not in PROVIDERS):
        return custom_base
    return PROVIDERS[provider_id].get("base_url")


def is_known_provider(provider_id: str) -> bool:
    return provider_id in PROVIDERS
