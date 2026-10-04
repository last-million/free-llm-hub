# Calvoun hub: model benchmark

Snapshot of `/api/tracking` taken 2026-10-04 00:41: 229 models across 13 providers.

**Hub score** is the number routing uses to rank models. It comes from Artificial Analysis scores (via OpenRouter's public catalog), the family table, LMArena for unknown models, and the owner's fixed floors (Kimi K3 138.1, GLM 5.3 138, Space Bunny 137.7). Higher is stronger.

**State:** ok 203, provider-exhausted 23, dead 3.

**Context:** the input window the hub uses. The source in brackets says where it came from: catalog, learned (from a real error), inferred (other providers' catalogs), reference (family table) or default (a guess).


## Strongest models (best copy of each model)

| # | Model | Best provider | Hub score | LMArena | Context | Tools | State |
|---|---|---|---|---|---|---|---|
| 1 | morph-kimik3 | morph | 138.1 | - | 1.0M (inferred) | yes | provider-exhausted |
| 2 | morph-kimik3-fast | morph | 138.1 | - | 1.0M (inferred) | yes | provider-exhausted |
| 3 | moonshotai/kimi-k3 | nvidia | 138.1 | 1488 (#16) | 1.0M (inferred) | yes | ok |
| 4 | morph-glm53-744b | morph | 138.0 | - | 1.0M (inferred) | yes | provider-exhausted |
| 5 | z-ai/glm-5.3 | nvidia | 138.0 | 1478 (#27) | 1.0M (inferred) | yes | ok |
| 6 | stealth/space-bunny-alpha | openrouter | 137.7 | - | 1.0M (learned) | yes | ok |
| 7 | AnyProvider:kimi-k3 | g4f | 134.1 | - | 1.0M (inferred) | yes | ok |
| 8 | models/gemini-3.5-flash | google | 134.1 | 1477 (#28) | 1.0M (catalog) | yes | ok |
| 9 | models/gemini-3.6-flash | google | 134.1 | 1483 (#22) | 1.0M (catalog) | yes | ok |
| 10 | models/gemini-3.7-flash | google | 134.1 | 1488 (#17) | 1.0M (catalog) | yes | ok |
| 11 | models/gemini-3.8-flash | google | 134.1 | 1495 (#8) | 1.0M (catalog) | yes | ok |
| 12 | qwen/qwen3.8-27b | groq | 134.1 | 1438 (#99) | 7K (learned) | yes | ok |
| 13 | turboderp/Qwen3.8-27B-exl3 | uncloseai | 134.1 | - | 66K (catalog) | yes | ok |
| 14 | deepseek-ai/DeepSeek-V4-Flash-0731 | dahl | 134.0 | 1438 (#98) | 1.0M (inferred) | yes | ok |
| 15 | GithubCopilot:claude-sonnet-5.5 | g4f | 134.0 | 1471 (#45) | 1.0M (inferred) | yes | ok |
| 16 | claude-sonnet-5.5 | g4f | 134.0 | 1471 (#45) | 1.0M (inferred) | yes | dead |
| 17 | srv_mp1v9cyha31b95fa8c9a:anthropic/claude-haiku-4-5 | g4f | 134.0 | 1414 (#142) | 200K (inferred) | yes | ok |
| 18 | srv_mp1v9cyha31b95fa8c9a:anthropic/claude-sonnet-4 | g4f | 134.0 | 1402 (#157) | 200K (inferred) | yes | ok |
| 19 | srv_mp1v9cyha31b95fa8c9a:anthropic/claude-sonnet-4-5 | g4f | 134.0 | 1457 (#65) | 200K (inferred) | yes | ok |
| 20 | srv_mp2huzrg06e426ad12f3:tencent/Hy4-preview | g4f | 134.0 | - | 1.0M (inferred) | yes | ok |
| 21 | srv_mr9dda21bf67b4c62086:anthropic/claude-fable-5 | g4f | 134.0 | - | 1.0M (inferred) | yes | ok |
| 22 | deepseek/deepseek-v4-flash | morph | 134.0 | 1438 (#98) | 1.0M (inferred) | yes | provider-exhausted |
| 23 | deepseek/deepseek-v4-flash-20260423 | morph | 134.0 | 1438 (#98) | 1.0M (inferred) | yes | provider-exhausted |
| 24 | morph-dsv41flash | morph | 134.0 | - | ? | yes | provider-exhausted |
| 25 | morph-dsv4flash | morph | 134.0 | - | 1.0M (inferred) | yes | provider-exhausted |
| 26 | morph-dsv4flash-0731 | morph | 134.0 | - | 1.0M (inferred) | yes | provider-exhausted |
| 27 | morph-glm52-744b | morph | 134.0 | - | 1.0M (inferred) | yes | provider-exhausted |
| 28 | deepseek-ai/deepseek-v4.1-flash | nvidia | 134.0 | 1474 (#38) | 1.0M (inferred) | yes | ok |
| 29 | zai-org/GLM-5.3-Flash | dahl | 133.6 | 1473 (#41) | 1.0M (inferred) | yes | ok |
| 30 | srv_mrdypihj16e8b1776409:openai/gpt-6-luna | g4f | 133.0 | - | 1.1M (inferred) | yes | ok |
| 31 | srv_mrdypihj16e8b1776409:openai/gpt-5.6-luna | g4f | 132.2 | - | 1.1M (inferred) | yes | ok |
| 32 | gemini-3.1-pro-high | g4f | 130.2 | 1487 (#18) | 1.0M (reference) | yes | dead |
| 33 | srv_mlv668eaa6d92f50ff10:gemini-3.1-pro-high | g4f | 130.2 | - | 1.0M (reference) | yes | ok |
| 34 | gemini-3.6-flash-high | g4f | 130.1 | 1483 (#22) | 1.0M (reference) | yes | ok |
| 35 | gemini-3.8-flash-high | g4f | 130.1 | 1495 (#8) | 1.0M (reference) | yes | ok |
| 36 | srv_mlv668eaa6d92f50ff10:gemini-3.8-flash | g4f | 130.1 | - | 1.0M (inferred) | yes | ok |
| 37 | srv_mlv668eaa6d92f50ff10:gemini-3.8-flash-high | g4f | 130.1 | - | 1.0M (reference) | yes | ok |
| 38 | srv_mp2huzrg06e426ad12f3:zai-org/GLM-5.2 | g4f | 130.0 | - | 1.0M (inferred) | yes | ok |
| 39 | srv_mtsj8uzo97d3c0d49960:deepseek-z/deepseek-v4-pro | g4f | 129.5 | 1464 (#56) | 1.0M (inferred) | yes | ok |
| 40 | MiniMaxAI/MiniMax-M2.7 | dahl | 108.0 | 1415 (#137) | 205K (inferred) | yes | ok |

## Every model in the hub (229 rows, one per provider copy)

| # | Model | Provider | Hub score | LMArena | Context | Tools | Fast | State |
|---|---|---|---|---|---|---|---|---|
| 1 | morph-kimik3 | morph | 138.1 | - | 1.0M (inferred) | yes | yes | provider-exhausted |
| 2 | morph-kimik3-fast | morph | 138.1 | - | 1.0M (inferred) | yes | yes | provider-exhausted |
| 3 | moonshotai/kimi-k3 | nvidia | 138.1 | 1488 (#16) | 1.0M (inferred) | yes | no | ok |
| 4 | morph-glm53-744b | morph | 138.0 | - | 1.0M (inferred) | yes | no | provider-exhausted |
| 5 | z-ai/glm-5.3 | nvidia | 138.0 | 1478 (#27) | 1.0M (inferred) | yes | no | ok |
| 6 | stealth/space-bunny-alpha | openrouter | 137.7 | - | 1.0M (learned) | yes | no | ok |
| 7 | AnyProvider:kimi-k3 | g4f | 134.1 | - | 1.0M (inferred) | yes | yes | ok |
| 8 | kimi-k3 | g4f | 134.1 | 1488 (#16) | 1.0M (inferred) | yes | yes | dead |
| 9 | srv_mkombumpae45db46dcb8:moonshotai/kimi-k3 | g4f | 134.1 | - | 1.0M (inferred) | yes | yes | ok |
| 10 | models/gemini-3.5-flash | google | 134.1 | 1477 (#28) | 1.0M (catalog) | yes | yes | ok |
| 11 | models/gemini-3.6-flash | google | 134.1 | 1483 (#22) | 1.0M (catalog) | yes | yes | ok |
| 12 | models/gemini-3.7-flash | google | 134.1 | 1488 (#17) | 1.0M (catalog) | yes | yes | ok |
| 13 | models/gemini-3.8-flash | google | 134.1 | 1495 (#8) | 1.0M (catalog) | yes | yes | ok |
| 14 | qwen/qwen3.8-27b | groq | 134.1 | 1438 (#99) | 7K (learned) | yes | yes | ok |
| 15 | qwen/qwen3.8-27b:free | kilocode | 134.1 | 1438 (#99) | 262K (learned) | yes | yes | ok |
| 16 | qwen/qwen3.8-27b:free | openrouter | 134.1 | 1438 (#99) | 262K (learned) | yes | no | ok |
| 17 | turboderp/Qwen3.8-27B-exl3 | uncloseai | 134.1 | - | 66K (catalog) | yes | yes | ok |
| 18 | deepseek-ai/DeepSeek-V4-Flash-0731 | dahl | 134.0 | 1438 (#98) | 1.0M (inferred) | yes | no | ok |
| 19 | GithubCopilot:claude-sonnet-5.5 | g4f | 134.0 | 1471 (#45) | 1.0M (inferred) | yes | yes | ok |
| 20 | claude-sonnet-5.5 | g4f | 134.0 | 1471 (#45) | 1.0M (inferred) | yes | yes | dead |
| 21 | srv_mkombumpae45db46dcb8:z-ai/glm-5.3 | g4f | 134.0 | - | 1.0M (inferred) | yes | yes | ok |
| 22 | srv_mp1v9cyha31b95fa8c9a:anthropic/claude-haiku-4-5 | g4f | 134.0 | 1414 (#142) | 200K (inferred) | yes | yes | ok |
| 23 | srv_mp1v9cyha31b95fa8c9a:anthropic/claude-sonnet-4 | g4f | 134.0 | 1402 (#157) | 200K (inferred) | yes | yes | ok |
| 24 | srv_mp1v9cyha31b95fa8c9a:anthropic/claude-sonnet-4-5 | g4f | 134.0 | 1457 (#65) | 200K (inferred) | yes | yes | ok |
| 25 | srv_mp1v9cyha31b95fa8c9a:z-ai/glm-5.3 | g4f | 134.0 | - | 1.0M (inferred) | yes | yes | ok |
| 26 | srv_mp2huzrg06e426ad12f3:tencent/Hy4-preview | g4f | 134.0 | - | 1.0M (inferred) | yes | yes | ok |
| 27 | srv_mp2huzrg06e426ad12f3:zai-org/GLM-5.3 | g4f | 134.0 | - | 1.0M (inferred) | yes | yes | ok |
| 28 | srv_mr9dda21bf67b4c62086:anthropic/claude-fable-5 | g4f | 134.0 | - | 1.0M (inferred) | yes | yes | ok |
| 29 | DeepSeek-V4-Flash-0731 | llm7 | 134.0 | 1438 (#98) | 1.0M (inferred) | yes | no | provider-exhausted |
| 30 | deepseek/deepseek-v4-flash | morph | 134.0 | 1438 (#98) | 1.0M (inferred) | yes | no | provider-exhausted |
| 31 | deepseek/deepseek-v4-flash-0731 | morph | 134.0 | 1438 (#98) | 1.0M (inferred) | yes | no | provider-exhausted |
| 32 | deepseek/deepseek-v4-flash-20260423 | morph | 134.0 | 1438 (#98) | 1.0M (inferred) | yes | no | provider-exhausted |
| 33 | morph-dsv41flash | morph | 134.0 | - | ? | yes | yes | provider-exhausted |
| 34 | morph-dsv4flash | morph | 134.0 | - | 1.0M (inferred) | yes | yes | provider-exhausted |
| 35 | morph-dsv4flash-0731 | morph | 134.0 | - | 1.0M (inferred) | yes | yes | provider-exhausted |
| 36 | morph-glm52-744b | morph | 134.0 | - | 1.0M (inferred) | yes | no | provider-exhausted |
| 37 | deepseek-ai/deepseek-v4.1-flash | nvidia | 134.0 | 1474 (#38) | 1.0M (inferred) | yes | no | ok |
| 38 | srv_monk1pkz433a519ff2be:stealth/space-bunny-alpha | g4f | 133.7 | - | 1.0M (inferred) | yes | yes | ok |
| 39 | zai-org/GLM-5.3-Flash | dahl | 133.6 | 1473 (#41) | 1.0M (inferred) | yes | yes | ok |
| 40 | GLM-5.3-Flash | llm7 | 133.6 | 1473 (#41) | 1.0M (inferred) | yes | yes | provider-exhausted |
| 41 | z-ai/glm-5.3-flash | nvidia | 133.6 | 1473 (#41) | 1.0M (inferred) | yes | no | ok |
| 42 | srv_mrdypihj16e8b1776409:openai/gpt-6-luna | g4f | 133.0 | - | 1.1M (inferred) | yes | yes | ok |
| 43 | srv_mrdypihj16e8b1776409:openai/gpt-5.6-luna | g4f | 132.2 | - | 1.1M (inferred) | yes | yes | ok |
| 44 | gemini-3.1-pro-high | g4f | 130.2 | 1487 (#18) | 1.0M (reference) | yes | yes | dead |
| 45 | srv_mlv668eaa6d92f50ff10:gemini-3.1-pro-high | g4f | 130.2 | - | 1.0M (reference) | yes | yes | ok |
| 46 | gemini-3.6-flash-high | g4f | 130.1 | 1483 (#22) | 1.0M (reference) | yes | yes | ok |
| 47 | gemini-3.8-flash-high | g4f | 130.1 | 1495 (#8) | 1.0M (reference) | yes | yes | ok |
| 48 | srv_mkom688d57c76d8a3542:qwen/qwen3.8-27b | g4f | 130.1 | - | 8K (learned) | yes | yes | ok |
| 49 | srv_mlv668eaa6d92f50ff10:gemini-3.8-flash | g4f | 130.1 | - | 1.0M (inferred) | yes | yes | ok |
| 50 | srv_mlv668eaa6d92f50ff10:gemini-3.8-flash-high | g4f | 130.1 | - | 1.0M (reference) | yes | yes | ok |
| 51 | srv_monk1pkz433a519ff2be:qwen/qwen3.8-27b:free | g4f | 130.1 | - | 262K (inferred) | yes | yes | ok |
| 52 | srv_mrgy0nmbc8a86c407f17:models/gemini-3.5-flash | g4f | 130.1 | - | 1.0M (inferred) | yes | yes | ok |
| 53 | srv_mrgy0nmbc8a86c407f17:models/gemini-3.6-flash | g4f | 130.1 | - | 1.0M (inferred) | yes | yes | ok |
| 54 | srv_mrgy0nmbc8a86c407f17:models/gemini-3.8-flash | g4f | 130.1 | - | 1.0M (inferred) | yes | yes | ok |
| 55 | srv_mp1v9cyha31b95fa8c9a:deepseek-ai/deepseek-v4.1-flash | g4f | 130.0 | 1474 (#38) | 1.0M (inferred) | yes | no | ok |
| 56 | srv_mp2huzrg06e426ad12f3:zai-org/GLM-5.2 | g4f | 130.0 | - | 1.0M (inferred) | yes | yes | ok |
| 57 | srv_mtsj8uzo97d3c0d49960:deepseek-z/deepseek-v4-flash | g4f | 130.0 | 1438 (#98) | 1.0M (inferred) | yes | no | ok |
| 58 | srv_mkombumpae45db46dcb8:z-ai/glm-5.3-flash | g4f | 129.6 | - | 1.0M (inferred) | yes | yes | ok |
| 59 | srv_mtsj8uzo97d3c0d49960:deepseek-z/deepseek-v4-pro | g4f | 129.5 | 1464 (#56) | 1.0M (inferred) | yes | no | ok |
| 60 | MiniMaxAI/MiniMax-M2.7 | dahl | 108.0 | 1415 (#137) | 205K (inferred) | yes | yes | ok |
| 61 | minimax-m2.7 | llm7 | 108.0 | 1415 (#137) | 205K (inferred) | yes | yes | provider-exhausted |
| 62 | srv_mtsj8uzo97d3c0d49960:zai-z/zai-org-glm-4.6 | g4f | 104.0 | - | 200K (inferred) | yes | yes | ok |
| 63 | models/gemini-3-flash-preview | google | 101.0 | 1473 (#42) | 1.0M (catalog) | yes | yes | ok |
| 64 | morph-glm53flash | morph | 99.0 | - | 1.0M (inferred) | yes | yes | provider-exhausted |
| 65 | moonshotai/kimi-k2.6 | nvidia | 98.0 | 1461 (#58) | 262K (inferred) | yes | no | ok |
| 66 | Airforce:grok-4.20-multi-agent-0309 | g4f | 96.0 | 1471 (#46) | 1.0M (inferred) | yes | yes | ok |
| 67 | gemini-3-flash | g4f | 96.0 | 1473 (#42) | 1.0M (inferred) | yes | yes | ok |
| 68 | srv_mtsj8uzo97d3c0d49960:xai-z/grok-4-1-fast-non-reasoning | g4f | 96.0 | - | 2.0M (inferred) | yes | no | ok |
| 69 | srv_mtsj8uzo97d3c0d49960:xai-z/grok-4-fast-non-reasoning | g4f | 96.0 | - | 256K (reference) | yes | no | ok |
| 70 | srv_mtsj8uzo97d3c0d49960:qwen-z/qwen3.8-flash | g4f | 95.1 | - | 1.0M (inferred) | yes | yes | ok |
| 71 | srv_mtsj8uzo97d3c0d49960:qwen-z/qwen3-coder-flash | g4f | 95.0 | - | 1.0M (inferred) | yes | yes | ok |
| 72 | nvidia/nemotron-3-ultra-550b-a55b | nvidia | 90.6 | - | 262K (inferred) | yes | no | ok |
| 73 | nvidia/nemotron-3-ultra-550b-a55b:free | kilocode | 89.4 | - | 1.0M (learned) | yes | no | ok |
| 74 | nvidia/nemotron-3-ultra-550b-a55b:free | openrouter | 89.4 | - | 1.0M (learned) | yes | no | ok |
| 75 | thinkingmachines/inkling-small:free | kilocode | 72.5 | 1405 (#152) | 262K (learned) | yes | no | ok |
| 76 | thinkingmachines/inkling-small:free | openrouter | 72.5 | 1405 (#152) | 262K (learned) | yes | no | ok |
| 77 | thinkingmachines/inkling:free | openrouter | 71.7 | 1441 (#95) | 262K (learned) | yes | no | ok |
| 78 | google/gemma-4-31b-it | nvidia | 65.8 | - | 262K (inferred) | yes | no | ok |
| 79 | models/gemma-4-31b-it | google | 65.6 | - | 262K (catalog) | yes | yes | ok |
| 80 | google/gemma-4-31b-it:free | openrouter | 64.6 | - | 262K (learned) | yes | no | ok |
| 81 | nvidia/nemotron-3-super-120b-a12b | nvidia | 64.3 | - | 262K (inferred) | yes | no | ok |
| 82 | meta/codellama-70b | nvidia | 64.0 | 1119 (#391) | 16K (reference) | yes | no | ok |
| 83 | openai/gpt-oss-120b | groq | 63.6 | 1352 (#212) | 131K (learned) | yes | no | ok |
| 84 | nvidia/nemotron-3-super-120b-a12b:free | kilocode | 63.1 | - | 262K (learned) | yes | no | ok |
| 85 | nvidia/nemotron-3-super-120b-a12b:free | openrouter | 63.1 | - | 262K (learned) | yes | no | ok |
| 86 | models/gemini-3.5-flash-lite | google | 60.0 | 1455 (#71) | 1.0M (catalog) | yes | yes | ok |
| 87 | nvidia/nemotron-3.5-lightning:free | kilocode | 58.4 | - | 1.0M (learned) | yes | yes | ok |
| 88 | nvidia/nemotron-3.5-lightning:free | openrouter | 58.4 | - | 1.0M (learned) | yes | no | ok |
| 89 | openai/gpt-oss-20b | groq | 56.7 | 1318 (#263) | 131K (learned) | yes | no | ok |
| 90 | mistralai/mistral-large-2-instruct | nvidia | 56.2 | - | 131K (reference) | yes | no | ok |
| 91 | openai/gpt-oss-20b | nvidia | 56.1 | 1318 (#263) | 131K (inferred) | yes | no | ok |
| 92 | srv_mrgy0nmbc8a86c407f17:models/gemini-3.5-flash-lite | g4f | 56.0 | 1455 (#71) | 1.0M (inferred) | yes | yes | ok |
| 93 | mistralai/mistral-large | nvidia | 53.2 | 1314 (#266) | 128K (inferred) | yes | no | ok |
| 94 | nvidia/nemotron-4-340b-instruct | nvidia | 43.8 | 1277 (#301) | 4K (reference) | yes | no | ok |
| 95 | srv_mkombumpae45db46dcb8:nvidia/nemotron-3-ultra-550b-a55b | g4f | 42.0 | - | 262K (inferred) | yes | no | ok |
| 96 | nvidia/nemotron-4-340b-reward | nvidia | 40.8 | - | 4K (reference) | yes | no | ok |
| 97 | nvidia/llama-3.1-nemotron-ultra-253b-v1 | nvidia | 37.3 | 1348 (#219) | 131K (reference) | yes | no | ok |
| 98 | meta/llama-3.2-90b-vision-instruct | nvidia | 33.8 | - | 131K (reference) | yes | no | ok |
| 99 | nvidia/llama-3.1-nemotron-70b-instruct | nvidia | 33.0 | 1299 (#284) | 131K (inferred) | yes | no | ok |
| 100 | nvidia/llama-3.1-nemotron-51b-instruct | nvidia | 32.2 | 1287 (#295) | 131K (reference) | yes | no | ok |
| 101 | google/diffusiongemma-26b-a4b-it | nvidia | 31.2 | - | ? | yes | no | ok |
| 102 | models/gemma-4-26b-a4b-it | google | 31.0 | - | 262K (catalog) | yes | yes | ok |
| 103 | glm-4.6v-flash | glm | 30.0 | - | 200K (inferred) | yes | yes | ok |
| 104 | glm-4.7-flash | glm | 30.0 | 1365 (#198) | 200K (inferred) | yes | yes | ok |
| 105 | models/gemini-3.1-flash-lite | google | 30.0 | 1433 (#109) | 1.0M (catalog) | yes | yes | ok |
| 106 | models/gemini-3.1-flash-lite-preview | google | 30.0 | 1433 (#109) | 1.0M (catalog) | yes | yes | ok |
| 107 | cohere/north-mini-code:free | kilocode | 30.0 | - | 256K (learned) | yes | yes | ok |
| 108 | google/gemma-3-12b-it | nvidia | 30.0 | 1342 (#230) | 131K (inferred) | no | no | ok |
| 109 | google/gemma-3-4b-it | nvidia | 30.0 | 1303 (#281) | 131K (inferred) | no | no | ok |
| 110 | meta/llama-3.2-11b-vision-instruct | nvidia | 30.0 | - | 131K (reference) | yes | no | ok |
| 111 | cohere/north-mini-code:free | openrouter | 30.0 | - | 256K (learned) | yes | no | ok |
| 112 | google/gemma-4-26b-a4b-it:free | openrouter | 30.0 | - | 262K (learned) | yes | no | ok |
| 113 | z-ai/glm-4.6v-flash-free | zenmux | 30.0 | - | 200K (learned) | yes | yes | ok |
| 114 | z-ai/glm-4.7-flash-free | zenmux | 30.0 | - | 200K (learned) | yes | yes | ok |
| 115 | nvidia/nemotron-3-nano-omni-30b-a3b-reasoning | nvidia | 28.4 | - | 256K (inferred) | yes | no | ok |
| 116 | nvidia/nemotron-3.5-lightning-30b-a3b | nvidia | 28.4 | - | 131K (reference) | yes | no | ok |
| 117 | nvidia/nemotron-nano-3-30b-a3b | nvidia | 28.4 | - | 131K (reference) | yes | no | ok |
| 118 | google/gemma-2b | nvidia | 27.3 | - | 8K (reference) | no | no | ok |
| 119 | google/recurrentgemma-2b | nvidia | 27.3 | - | 8K (reference) | no | no | ok |
| 120 | nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free | kilocode | 27.2 | - | 256K (learned) | yes | no | ok |
| 121 | nemotron-3-nano:30b | llm7 | 27.2 | - | 131K (reference) | yes | yes | provider-exhausted |
| 122 | nvidia/nemotron-3.5-content-safety | nvidia | 27.2 | - | 128K (inferred) | yes | no | ok |
| 123 | nvidia/nemotron-parse | nvidia | 27.2 | - | ? | yes | no | ok |
| 124 | nvidia/nemotron-parse-2.0 | nvidia | 27.2 | - | ? | yes | no | ok |
| 125 | nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free | openrouter | 27.2 | - | 256K (learned) | yes | no | ok |
| 126 | srv_mkom688d57c76d8a3542:openai/gpt-oss-120b | g4f | 26.8 | - | 131K (inferred) | yes | no | ok |
| 127 | srv_mkombumpae45db46dcb8:nvidia/nemotron-3-super-120b-a12b | g4f | 26.8 | - | 262K (inferred) | yes | no | ok |
| 128 | GeminiPro:models/gemini-2.5-flash | g4f | 26.0 | 1409 (#148) | 1.0M (inferred) | yes | yes | ok |
| 129 | srv_mkombumpae45db46dcb8:google/diffusiongemma-26b-a4b-it | g4f | 26.0 | - | ? | yes | yes | ok |
| 130 | srv_mrgy0nmbc8a86c407f17:models/gemini-2.5-flash | g4f | 26.0 | - | 1.0M (inferred) | yes | yes | ok |
| 131 | srv_mrgy0nmbc8a86c407f17:models/gemini-2.5-flash-lite | g4f | 26.0 | - | 1.0M (inferred) | yes | yes | ok |
| 132 | srv_mrgy0nmbc8a86c407f17:models/gemini-3.1-flash-lite | g4f | 26.0 | 1433 (#109) | 1.0M (inferred) | yes | yes | ok |
| 133 | srv_mrgy0nmbc8a86c407f17:models/gemini-3.1-flash-lite-preview | g4f | 26.0 | 1433 (#109) | 1.0M (inferred) | yes | yes | ok |
| 134 | srv_mtkdrhxt45fe276b15c0:@cf/deepseek-ai/deepseek-r1-distill-qwen-32b | g4f | 26.0 | - | 131K (reference) | yes | no | ok |
| 135 | srv_mtkdrhxt45fe276b15c0:@cf/zai-org/glm-4.7-flash | g4f | 26.0 | - | 200K (inferred) | yes | yes | ok |
| 136 | nvidia/nemotron-3.5-content-safety:free | kilocode | 26.0 | - | 128K (learned) | yes | yes | ok |
| 137 | codestral-latest | llm7 | 26.0 | - | 262K (reference) | no | yes | provider-exhausted |
| 138 | nvidia/nemotron-3.5-content-safety:free | openrouter | 26.0 | - | 128K (learned) | yes | no | ok |
| 139 | srv_mkombumpae45db46dcb8:meta/llama-3.2-11b-vision-instruct | g4f | 25.4 | - | 131K (reference) | yes | yes | ok |
| 140 | srv_mtkdrhxt45fe276b15c0:@cf/meta-llama/llama-2-7b-chat-hf-lora | g4f | 25.3 | - | 4K (reference) | yes | yes | ok |
| 141 | srv_mtkdrhxt45fe276b15c0:@cf/meta/llama-3.2-1b-instruct | g4f | 25.0 | - | 60K (inferred) | no | yes | ok |
| 142 | writer/palmyra-creative-122b | nvidia | 24.1 | - | ? | yes | no | ok |
| 143 | ibm/granite-34b-code-instruct | nvidia | 23.6 | - | 8K (reference) | yes | no | ok |
| 144 | srv_mkombumpae45db46dcb8:nvidia/nemotron-3-nano-omni-30b-a3b-reasoning | g4f | 23.2 | - | 256K (inferred) | yes | no | ok |
| 145 | srv_mkombumpae45db46dcb8:nvidia/nemotron-3.5-lightning-30b-a3b | g4f | 23.2 | - | 131K (reference) | yes | yes | ok |
| 146 | srv_mrgykg8eea645e7bb006:gemma4:31b | g4f | 23.2 | - | 262K (inferred) | yes | yes | ok |
| 147 | srv_mrgykg8eea645e7bb006:nemotron-3-nano:30b | g4f | 23.2 | - | 131K (reference) | yes | yes | ok |
| 148 | srv_mkom688d57c76d8a3542:openai/gpt-oss-20b | g4f | 22.8 | - | 131K (inferred) | yes | no | ok |
| 149 | srv_mkombumpae45db46dcb8:openai/gpt-oss-20b | g4f | 22.8 | - | 131K (inferred) | yes | no | ok |
| 150 | srv_mrgxy5xec5aa99a83657:gpt-oss:20b-cloud | g4f | 22.8 | - | 131K (reference) | yes | no | ok |
| 151 | srv_mrgykg8eea645e7bb006:gpt-oss:20b | g4f | 22.8 | - | 131K (reference) | yes | no | ok |
| 152 | ibm/granite-3.0-8b-instruct | nvidia | 22.5 | 1183 (#353) | 4K (reference) | yes | no | ok |
| 153 | ibm/granite-8b-code-instruct | nvidia | 22.5 | - | 4K (reference) | yes | no | ok |
| 154 | zyphra/zamba2-7b-instruct | nvidia | 22.5 | - | ? | yes | no | ok |
| 155 | ibm/granite-3.0-3b-a800m-instruct | nvidia | 22.3 | - | 4K (reference) | no | no | ok |
| 156 | Airforce:codestral-latest | g4f | 22.0 | - | 262K (reference) | no | yes | ok |
| 157 | srv_mrgykg8eea645e7bb006:nemotron-3-ultra | g4f | 22.0 | - | 131K (reference) | yes | yes | ok |
| 158 | srv_mtwlixj3dacf5d9a65fa:codestral-2508 | g4f | 22.0 | - | 256K (inferred) | no | yes | ok |
| 159 | nvidia/ising-calibration-1.5-31b | nvidia | 20.4 | - | 131K (learned) | yes | no | ok |
| 160 | allam-2-7b | groq | 20.1 | - | 4K (learned) | yes | yes | ok |
| 161 | nvidia/neva-22b | nvidia | 20.1 | - | ? | yes | no | ok |
| 162 | nvidia/cosmos-reason2-8b | nvidia | 19.5 | - | ? | yes | no | ok |
| 163 | models/gemini-flash-lite-latest | google | 19.0 | - | 1.0M (catalog) | yes | yes | ok |
| 164 | apodex/apodex-1.1-mini:free | kilocode | 18.0 | - | 262K (catalog) | yes | yes | ok |
| 165 | apodex/apodex-1.1-mini:free | openrouter | 18.0 | - | 262K (catalog) | yes | no | ok |
| 166 | inclusionai/ling-3.0-tiny | zenmux | 18.0 | - | 262K (learned) | yes | yes | ok |
| 167 | mistralai/codestral-22b-instruct-v0.1 | nvidia | 17.1 | - | 33K (reference) | no | no | ok |
| 168 | nvidia/llama3-chatqa-1.5-70b | nvidia | 17.0 | - | 8K (reference) | yes | no | ok |
| 169 | srv_mt58lf608d01990edc9b:kilo-auto/free | g4f | 14.4 | - | ? | yes | yes | ok |
| 170 | microsoft/phi-3-vision-128k-instruct | nvidia | 14.2 | - | 131K (reference) | yes | no | ok |
| 171 | microsoft/phi-3.5-moe-instruct | nvidia | 14.2 | - | 131K (reference) | yes | no | ok |
| 172 | mistralai/mixtral-8x22b-v0.1 | nvidia | 14.1 | - | 66K (inferred) | no | no | ok |
| 173 | apodex/apodex-1.1-mini:free | g4f | 14.0 | - | 262K (inferred) | yes | yes | ok |
| 174 | srv_monk1pkz433a519ff2be:openrouter/free | g4f | 14.0 | - | ? | yes | yes | ok |
| 175 | srv_mp5miql908c8738d71be:auto | g4f | 14.0 | - | ? | yes | yes | ok |
| 176 | srv_mp5miql908c8738d71be:community/YoannDev90/poolside-laguna-s-2.1:free | g4f | 14.0 | - | ? | yes | yes | ok |
| 177 | srv_mp5miql908c8738d71be:openai | g4f | 14.0 | - | ? | yes | yes | ok |
| 178 | srv_mp5miql908c8738d71be:sana | g4f | 14.0 | - | ? | yes | yes | ok |
| 179 | srv_mrgy0nmbc8a86c407f17:models/gemini-flash-lite-latest | g4f | 14.0 | - | 1.0M (inferred) | yes | yes | ok |
| 180 | srv_mtsj8uzo97d3c0d49960:openai-z/gpt-4.1-mini | g4f | 14.0 | - | 1.0M (inferred) | yes | yes | ok |
| 181 | meta/llama2-70b | nvidia | 14.0 | - | 4K (reference) | yes | no | ok |
| 182 | writer/palmyra-fin-70b-32k | nvidia | 14.0 | - | 33K (reference) | yes | no | ok |
| 183 | writer/palmyra-med-70b | nvidia | 14.0 | - | ? | yes | no | ok |
| 184 | writer/palmyra-med-70b-32k | nvidia | 14.0 | - | 33K (reference) | yes | no | ok |
| 185 | meta/muse-glimmer-30b | nvidia | 12.4 | - | 131K (inferred) | yes | no | ok |
| 186 | microsoft/kosmos-2 | nvidia | 11.2 | - | ? | yes | no | ok |
| 187 | nvidia/ai-synthetic-video-detector | nvidia | 11.2 | - | ? | yes | no | ok |
| 188 | nvidia/vila | nvidia | 11.2 | - | ? | yes | no | ok |
| 189 | poolside/laguna-xs-2.1 | nvidia | 11.2 | - | 262K (inferred) | yes | no | ok |
| 190 | models/gemini-flash-latest | google | 11.0 | - | 1.0M (catalog) | yes | yes | ok |
| 191 | liquid/lfm-2.5-2.6b:free | kilocode | 10.2 | - | 66K (learned) | yes | yes | ok |
| 192 | liquid/lfm-2.5-2.6b:free | openrouter | 10.2 | - | 66K (learned) | yes | no | ok |
| 193 | glm-4.5-flash | glm | 10.0 | - | 128K (inferred) | yes | yes | ok |
| 194 | dots-studio/dots-3-note-preview:free | kilocode | 10.0 | - | 512K (learned) | yes | yes | ok |
| 195 | inclusionai/ling-3.0-flash-sante:free | kilocode | 10.0 | - | 262K (learned) | yes | yes | ok |
| 196 | poolside/laguna-s-2.1:free | kilocode | 10.0 | - | 262K (learned) | yes | yes | ok |
| 197 | poolside/laguna-xs-2.1:free | kilocode | 10.0 | - | 262K (learned) | yes | yes | ok |
| 198 | stepfun/step-3.7-flash:free | kilocode | 10.0 | - | 262K (learned) | yes | yes | ok |
| 199 | auto | morph | 10.0 | - | ? | yes | yes | provider-exhausted |
| 200 | morph-compactor | morph | 10.0 | - | ? | yes | yes | provider-exhausted |
| 201 | morph-systemone-v1 | morph | 10.0 | - | ? | yes | yes | provider-exhausted |
| 202 | morph-v3-fast | morph | 10.0 | - | 82K (inferred) | yes | yes | provider-exhausted |
| 203 | morph-v3-large | morph | 10.0 | - | 262K (inferred) | yes | yes | provider-exhausted |
| 204 | systemone-latest | morph | 10.0 | - | ? | yes | yes | provider-exhausted |
| 205 | dots-studio/dots-3-note-preview:free | openrouter | 10.0 | - | 512K (learned) | yes | no | ok |
| 206 | inclusionai/ling-3.0-flash-sante:free | openrouter | 10.0 | - | 262K (learned) | yes | no | ok |
| 207 | inclusionai/ling-3.1-flash | openrouter | 10.0 | - | 262K (catalog) | yes | no | ok |
| 208 | poolside/laguna-s-2.1:free | openrouter | 10.0 | - | 262K (learned) | yes | no | ok |
| 209 | poolside/laguna-xs-2.1:free | openrouter | 10.0 | - | 262K (learned) | yes | no | ok |
| 210 | openai-fast | pollinations | 10.0 | - | 131K (learned) | yes | yes | ok |
| 211 | atria-asi/atria-dawn-preview | zenmux | 10.0 | - | 262K (learned) | yes | yes | ok |
| 212 | dots-studio/dots3-note-prev | zenmux | 10.0 | - | 393K (learned) | yes | yes | ok |
| 213 | sapiens-ai/agnes-2.5-flash | zenmux | 10.0 | - | 524K (learned) | yes | yes | ok |
| 214 | ChatGPT:auto | g4f | 9.0 | - | ? | yes | yes | ok |
| 215 | nv-mistralai/mistral-nemo-12b-instruct | nvidia | 8.7 | - | 131K (reference) | yes | no | ok |
| 216 | mistralai/mistral-7b-instruct-v0.3 | nvidia | 8.5 | - | 33K (reference) | yes | no | ok |
| 217 | nvidia/mistral-nemo-minitron-8b-8k-instruct | nvidia | 8.5 | - | 8K (reference) | yes | no | ok |
| 218 | srv_mkombumpae45db46dcb8:meta/muse-glimmer-30b | g4f | 7.2 | - | 131K (inferred) | yes | yes | ok |
| 219 | mistral-Nemo-Instruct-2407 | llm7 | 7.0 | - | 131K (reference) | yes | yes | provider-exhausted |
| 220 | AnyProvider: | g4f | 6.0 | - | ? | yes | yes | ok |
| 221 | AnyProvider:auto | g4f | 6.0 | - | ? | yes | yes | ok |
| 222 | auto | g4f | 6.0 | - | ? | yes | yes | ok |
| 223 | omni | g4f | 6.0 | - | ? | yes | yes | ok |
| 224 | srv_mkopv2kp2e0038cdf550:turbo | g4f | 6.0 | - | ? | yes | yes | ok |
| 225 | srv_mkp3v4pj6b8669965b41:auto | g4f | 6.0 | - | ? | yes | yes | ok |
| 226 | srv_mrgy0nmbc8a86c407f17:models/gemini-flash-latest | g4f | 6.0 | - | 1.0M (inferred) | yes | yes | ok |
| 227 | srv_msjekdik2f3768a4ee42:kilo-auto/free | g4f | 6.0 | - | ? | yes | yes | ok |
| 228 | srv_msjekdik2f3768a4ee42:stepfun/step-3.7-flash:free | g4f | 6.0 | - | 256K (inferred) | yes | yes | ok |
| 229 | srv_mtsj8uzo97d3c0d49960:poolside/laguna-xs-2.1 | g4f | 6.0 | - | 262K (inferred) | yes | yes | ok |
