"""Two ranking corrections, both from MEASURED 2026-08-30 data.

1. The stated preference names "latest kimi / qwen / deepseek" as top picks
   together, but only kimi, glm and deepseek were ever floored. qwen sat on its
   natural score: qwen3.8-27b scored 110.9 against glm-5.2's 134.0 and never won
   a slot. qwen3.8 and qwen3.6 also scored IDENTICALLY, so nothing in the table
   knew 3.8 was newer and every future release would flatline the same way.

2. 15 of the top 20 eligible models were g4f entries and the whole top 8 was.
   g4f relays advertise ids like 'srv_...:claude-sonnet-4' -- the NAME earns
   claude's 138 floor, but nothing verifies the relay serves what it claims, and
   a live smoke test put the pool at 110 ok / 112 fail. The least reliable
   provider owned every top slot while first-party hosts of genuinely strong
   models sat underneath, unused.
"""
import app


def test_latest_qwen_is_floored_beside_its_named_peers():
    glm = app._benchmark_score("wandb", "zai-org/GLM-5.2")
    for mid in ("qwen/qwen3.8-27b", "qwen/qwen3.6-27b", "Qwen/Qwen3.5-35B-A3B"):
        assert app._benchmark_score("groq", mid) >= glm, mid


def test_a_newer_qwen_outranks_an_older_one():
    """The flatline bug: 3.8 and 3.6 used to score exactly the same."""
    s38 = app._benchmark_score("groq", "qwen/qwen3.8-27b")
    s36 = app._benchmark_score("groq", "qwen/qwen3.6-27b")
    s35 = app._benchmark_score("groq", "qwen/qwen3.5-27b")
    assert s38 > s36 > s35, (s38, s36, s35)


def test_older_qwen_lines_are_not_lifted():
    """The preference was about the LATEST qwen; floating the whole family would
    lift the small old ones too."""
    glm = app._benchmark_score("wandb", "zai-org/GLM-5.2")
    for mid in ("Qwen/Qwen3-30B-A3B-Instruct-2507", "Qwen/Qwen2.5-72B-Instruct"):
        assert app._benchmark_score("deepinfra", mid) < glm, mid


def test_qwen_stays_under_the_named_top_models():
    """Level with glm/deepseek, not above kimi-k3 or hy3."""
    q = app._benchmark_score("groq", "qwen/qwen3.8-27b")
    assert q < app._benchmark_score("nvidia", "moonshotai/kimi-k3")
    assert q < app._benchmark_score("opencode-zen", "hy3-free")


def test_a_relayed_model_scores_below_a_first_party_one():
    """The headline fix: a claimed name must not beat a real host."""
    relayed = app._benchmark_score("g4f", "srv_abc:claude-sonnet-4")
    for pid, mid in (("nvidia", "moonshotai/kimi-k3"),
                     ("opencode-zen", "hy3-free")):
        assert app._benchmark_score(pid, mid) > relayed, (pid, mid)


def test_the_discount_survives_the_preference_floors():
    """It is applied LAST on purpose: the floors use max(score, floor), so a
    bias added earlier is simply erased -- which is how a relayed
    'claude-sonnet-4' inherited claude's full 138 in the first place."""
    assert app._benchmark_score("g4f", "srv_abc:claude-sonnet-4") == \
        app._PREF_FLOORS[5] - app._RELAY_DISCOUNT["g4f"]


def test_relays_are_discounted_not_banned():
    """g4f must still be a usable fallback, well above the ordinary field."""
    relayed = app._benchmark_score("g4f", "srv_abc:claude-sonnet-4")
    assert relayed > app._benchmark_score("deepinfra", "Qwen/Qwen2.5-72B-Instruct")
    assert relayed > 100


def test_first_party_providers_are_untouched_by_the_discount():
    for pid in ("nvidia", "groq", "cerebras", "wandb", "cloudflare"):
        assert pid not in app._RELAY_DISCOUNT, pid
