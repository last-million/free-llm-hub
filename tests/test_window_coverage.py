"""Window coverage for the ids the live fleet still showed as "default".

MEASURED 2026-09-27 on /api/model-windows: 60 of 256 models on "default".
Replayed offline with this code (live catalogs + OpenRouter's public
catalog + pollinations' catalog): 35 left, all routers/aliases (auto,
openai, kilo-auto, default), image models (sana) or unlabeled community /
stealth ids with no sourced figure. Three mechanisms, all tested here:

  1. documented family rows (_CTX_REFERENCE): Claude, GPT-4o/4.1/5, Grok,
     mistral-tiny and the NIM families yi-large, Jamba 1.5, DBRX, StarCoder2,
     DeepSeek-Coder;
  2. relay re-spellings (_CTX_ID_REWRITES) so a catalog's figure for the
     same weights applies: morph-kimik3 -> kimi-k3, zai-org-glm-5-3-flash ->
     glm-5.3-flash, gpt-5-2 -> gpt-5.2, gemma4:31b -> gemma-4-31b-it, ...;
  3. the aliases a provider's OWN catalog row names (pollinations:
     openai-fast -> ["openai", "gpt-oss", "gpt-oss-20b"]).

Fakes only, no network.
"""
import pytest

import app as A


@pytest.fixture(autouse=True)
def isolated_windows():
    tables = (A._MODEL_MAX_INPUT, A._MODEL_LEARNED_AT, A._MODEL_CATALOG_CTX,
              A._MODEL_MAX_OUTPUT, A._REF_CATALOG_CTX, A._CTX_ALIASES)
    saved = [(d, dict(d)) for d in tables]
    for d in tables:
        d.clear()
    A._CTX_RESPELL_CACHE.clear()
    A._ctx_index_touch()
    yield
    for d, snap in saved:
        d.clear()
        d.update(snap)
    A._CTX_RESPELL_CACHE.clear()
    A._ctx_index_touch()


def _cat(pid, mid, win):
    A._learn_ctx_from_catalog(pid, {"data": [{"id": mid, "context_length": win}]})


# --------------------------------------------------------------------------- #
# 1. documented family rows
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("pid,mid,want", [
    ("g4f", "Airforce:gpt-4o-audio-preview-2024-12-17", 128000),
    ("g4f", "Airforce:mistral-tiny-latest", 32768),
    ("g4f", "Perplexity:claude41opusthinking", 200000),
    ("g4f", "Antigravity:gemini-claude-opus-4-6-thinking", 200000),
    ("g4f", "xai-z/grok-4-fast-non-reasoning", 256000),
    ("g4f", "x-ai/grok-3-mini", 131072),
    ("openrouter", "openai/gpt-4.1-nano", 1047576),
    ("openrouter", "openai/gpt-5.2", 272000),
    ("openrouter", "openai/gpt-5-chat", 128000),
    ("nvidia", "01-ai/yi-large", 32768),
    ("nvidia", "ai21labs/jamba-1.5-large-instruct", 262144),
    ("nvidia", "databricks/dbrx-instruct", 32768),
    ("nvidia", "bigcode/starcoder2-15b", 16384),
    ("nvidia", "deepseek-ai/deepseek-coder-6.7b-instruct", 16384),
    ("nvidia", "deepseek-ai/deepseek-coder-v2-lite-instruct", 131072),
])
def test_new_reference_rows(pid, mid, want):
    assert A._window_info(pid, mid) == (want, "reference")


def test_existing_reference_rows_are_unchanged():
    for mid, want in (("openai/gpt-oss-20b", 131072), ("mistralai/mistral-7b-instruct-v0.3", 32768),
                      ("models/gemini-flash-lite-latest", 1048576),
                      ("meta/llama-3.3-70b-instruct", 131072)):
        assert A._reference_ctx(mid) == want


# --------------------------------------------------------------------------- #
# 2. relay re-spellings pick up a catalog's figure for the same weights
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("pid,mid,canon", [
    ("morph", "morph-kimik3", "moonshotai/kimi-k3"),
    ("morph", "morph-kimik3-fast", "moonshotai/kimi-k3"),
    ("morph", "morph-glm53-744b", "z-ai/glm-5.3"),
    ("morph", "morph-glm53flash", "z-ai/glm-5.3-flash"),
    ("morph", "morph-dsv4flash-0731", "deepseek-ai/deepseek-v4-flash"),
    ("g4f", "srv_x:zai-z/zai-org-glm-5-3-flash", "z-ai/glm-5.3-flash"),
    ("g4f", "OpenaiChat:gpt-5-2", "openai/gpt-5.2"),
    ("g4f", "GithubCopilot:kimi-k3-base", "moonshotai/kimi-k3"),
    ("g4f", "srv_mp2i8rco3148dd85bec1:gemma4:31b", "google/gemma-4-31b-it"),
    ("g4f", "srv_y:gemma4:31b-cloud", "google/gemma-4-31b-it"),
])
def test_respelled_ids_inherit_the_catalog_window(pid, mid, canon):
    assert A._window_info(pid, mid)[1] in ("default", "reference")
    _cat("openrouter", canon, 123456)
    assert A._window_info(pid, mid) == (123456, "inferred")


def test_an_inferred_alias_beats_the_family_row():
    # glm-4.6 has a 200K family row; a catalog stating the weights wins.
    assert A._window_info("g4f", "zai-z/zai-org-glm-4.6") == (200000, "reference")
    _cat("openrouter", "z-ai/glm-4.6", 204800)
    assert A._window_info("g4f", "zai-z/zai-org-glm-4.6") == (204800, "inferred")


def test_the_id_itself_still_wins_over_an_alias():
    _cat("openrouter", "moonshotai/kimi-k3", 262144)
    _cat("morph", "morph-kimik3", 131072)
    assert A._window_info("morph", "morph-kimik3") == (131072, "catalog")


def test_context_ok_uses_the_alias_window():
    _cat("openrouter", "moonshotai/kimi-k3", 100000)
    assert "relay-x" not in A._PROVIDER_TPM
    assert A._context_ok("relay-x", "morph-kimik3", 90000)
    assert not A._context_ok("relay-x", "morph-kimik3", 120000)
    # ...still capped by a measured provider row (morph: 30000)
    assert not A._context_ok("morph", "morph-kimik3", 40000)


# --------------------------------------------------------------------------- #
# 3. aliases from the provider's own catalog row
# --------------------------------------------------------------------------- #
POLLINATIONS_ROW = {"name": "openai-fast", "description": "GPT-OSS 20B Reasoning LLM (OVH)",
                    "tier": "anonymous",
                    "aliases": ["openai", "gpt-oss", "gpt-oss-20b", "ovh-reasoning"]}


def test_pollinations_openai_fast_reads_its_catalog_aliases():
    assert A._window_info("pollinations", "openai-fast") == (None, "default")
    A._learn_ctx_from_catalog("pollinations", [POLLINATIONS_ROW])
    assert A._CTX_ALIASES[("pollinations", "openai-fast")][2] == "gpt-oss-20b"
    # most specific alias first
    assert A._ctx_alias_candidates("pollinations", "openai-fast")[0] == "gpt-oss-20b"
    assert A._window_info("pollinations", "openai-fast") == (131072, "reference")
    _cat("groq", "openai/gpt-oss-20b", 65536)
    assert A._window_info("pollinations", "openai-fast") == (65536, "inferred")


def test_aliases_are_per_provider():
    A._learn_ctx_from_catalog("pollinations", [POLLINATIONS_ROW])
    assert A._window_info("g4f", "srv_x:openai-fast") == (None, "default")


@pytest.mark.parametrize("pid,mid", [
    ("g4f", "AnyProvider:auto"), ("g4f", "srv_x:openai"), ("g4f", "srv_x:kilo-auto/free"),
    ("g4f", "srv_x:default"), ("g4f", "srv_x:sana"), ("opencode-zen", "space-bunny-free"),
    ("morph", "morph-compactor"),
])
def test_routers_and_unsourced_ids_stay_default(pid, mid):
    _cat("openrouter", "openai/gpt-oss-20b", 131072)
    assert A._window_info(pid, mid) == (None, "default")


def test_generic_aliases_are_ignored():
    A._learn_ctx_from_catalog("p", [{"name": "x-fast", "aliases": ["auto", "free", "ab"]}])
    assert A._ctx_alias_candidates("p", "x-fast") == []
