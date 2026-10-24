"""GPT stays below Claude on TOOL turns (owner decision 2026-10-24).

After the real Terminal-Bench / AutomationBench rows landed, first-party tool-turn
scores read Opus 5.5 144.66, Sonnet 5.5 144.48, GPT-6 Astra 143.60, Fable 5.1
143.49, GPT-6.1 Sol 143.46: Astra sat INSIDE _AUTO_TOP_BAND (2.0) of Opus, so the
weighted pick could choose either. The owner's standing order is Claude > GPT >
Gemini, so a GPT-family score is now held OUTSIDE the band of the best reachable
Claude flagship (flag `gpt_below_claude_tools`, default on). The rule lives at the
end of app._agentic_score (`_gpt_below_claude`), built from the same fleet and TTL
as the chat ladder's anchors; it only lowers, only GPT, only tool scores.

Hermetic: the AA cache, the fleet and the outcome ledgers are replaced per test;
nothing reads the owner's files, no network.
"""
import time

import pytest

import app as A
import config

RELAY = "g4f"
BAND = A._AUTO_TOP_BAND

OPUS, SONNET, FABLE, HAIKU = "claude-opus-5.5", "claude-sonnet-5.5", "claude-fable-5.1", "claude-haiku-5.5"
ASTRA, SOL61 = "gpt-6-astra", "gpt-6.1-sol"
GLM, KIMI, GEMINI = "z-ai/glm-5.3", "moonshotai/kimi-k3", "gemini-3.8-flash"

# a relay Opus the owner's ledger measured as a dead budget stub (0 ok / 21 fail)
DEAD_OPUS = "srv_mr9dda21bf67b4c62086:anthropic/claude-opus-5.5"

TABLE = [OPUS, SONNET, FABLE, ASTRA, SOL61]
FLEET = TABLE + [HAIKU, "gpt-6-luna", "gpt-5.6-sol", GLM, KIMI, GEMINI, "qwen/qwen3.8-27b"]

_REAL_RANK_FLEET_IDS = A._rank_fleet_ids

# every family the rule must leave alone, plus the GPT-ish ids it must not misread
NON_GPT = [OPUS, SONNET, FABLE, HAIKU, "claude-opus-6", GLM, "z-ai/glm-5.3-flash", KIMI,
           "moonshotai/kimi-k3.1-pro", GEMINI, "gemini-4-argon", "deepseek-v4.1-flash",
           "qwen/qwen3.8-27b", "grok-5", "gpt-oss-120b", "mistral-large"]
GPT_IDS = [ASTRA, SOL61, "gpt-6.2-sol", "gpt-7", "gpt-6-luna", "gpt-6-terra", "gpt-5.6-sol", "gpt-5.4",
           "openai/gpt-6.1-sol", "srv_x:openai/gpt-6.1-sol"]
PIDS = ["anthropic", "openai", "nvidia", "google", RELAY]


@pytest.fixture(autouse=True)
def _world(monkeypatch):
    monkeypatch.setattr(A, "_aa_scores", {})
    monkeypatch.setattr(A, "_outcomes", {})
    monkeypatch.setattr(A, "_tool_outcomes", {})
    monkeypatch.setattr(A, "_rank_fleet_ids", lambda: list(FLEET))
    A._rank_reset()
    yield
    A._rank_reset()


def _fleet(monkeypatch, ids):
    monkeypatch.setattr(A, "_rank_fleet_ids", lambda: list(ids))
    A._rank_reset()


def _flag(monkeypatch, on):
    """Flip the rule's flag. The flag is read when the reference is rebuilt, so the
    cache is reset here, exactly as a config edit is picked up within the TTL."""
    orig = config.get_flag

    def fake(name, default=False):
        if name == "gpt_below_claude_tools":
            return on
        return orig(name, default)
    monkeypatch.setattr(config, "get_flag", fake)
    A._rank_reset()


def chat(pid, model):
    return A._benchmark_score(pid, model)


def tool(pid, model):
    return A._agentic_score((A._benchmark_score(pid, model), pid, model))


def seed(pid, model, ok, fail, rok=None, rfail=0, idle_hours=0.0):
    now = time.time()
    rec = {"ok": ok, "fail": fail, "last": now - idle_hours * 3600.0}
    if rok is not None:
        rec.update(rok=rok, rfail=rfail, rstart=now - 3600.0)
    A._outcomes[(pid, model)] = rec


def seed_healthy(pid, model):
    seed(pid, model, 30, 1, rok=8, rfail=0)
    A._tool_outcomes[(pid, model)] = {"ok": 8, "fail": 0, "last": time.time()}


def _pool(models=TABLE):
    pids = {m: ("openai" if m.startswith("gpt") else "anthropic") for m in models}
    return [(A._benchmark_score(pids[m], m), pids[m], m) for m in models]


def _band_models(models=TABLE):
    return {c[2] for c in A._auto_top_band(_pool(models))}


def _assert_gpt_outside_the_claude_band():
    """THE main property, through the real band helper on the five-model pool."""
    got = _band_models()
    assert got == {OPUS, SONNET, FABLE}, got
    best = max(tool("anthropic", m) for m in (OPUS, SONNET, FABLE))
    for g in (ASTRA, SOL61):
        assert tool("openai", g) < best - BAND, g


# =========================================================================== #
# 1. The five-model table, before and after
# =========================================================================== #
def test_the_five_model_table_before_and_after(monkeypatch):
    _flag(monkeypatch, False)
    before = {m: tool("openai" if m.startswith("gpt") else "anthropic", m) for m in TABLE}
    assert before[OPUS] == pytest.approx(144.655, abs=1e-3)
    assert before[SONNET] == pytest.approx(144.478, abs=1e-3)
    assert before[ASTRA] == pytest.approx(143.595, abs=1e-3)
    assert before[FABLE] == pytest.approx(143.494, abs=1e-3)
    assert before[SOL61] == pytest.approx(143.459, abs=1e-3)
    assert before[OPUS] - before[ASTRA] < BAND, "before the rule Astra is inside Opus's band"

    _flag(monkeypatch, True)
    after = {m: tool("openai" if m.startswith("gpt") else "anthropic", m) for m in TABLE}
    for m in (OPUS, SONNET, FABLE):                       # the Claude pins are not touched
        assert after[m] == before[m], m
    assert after[OPUS] > after[FABLE] and after[OPUS] > after[SONNET]      # Opus stays on top
    for g in (ASTRA, SOL61):
        assert after[g] < before[g], "GPT is lowered"
        assert after[OPUS] - after[g] > BAND, "...to OUTSIDE the band (the test is >=)"
        assert after[OPUS] - after[g] < BAND + 0.05, "...and no further than it must"
    _assert_gpt_outside_the_claude_band()


def test_negative_control_without_the_rule_the_main_property_fails(monkeypatch):
    # the rule removed: Astra and Sol are in Opus's band again, so the property fails
    monkeypatch.setattr(A, "_gpt_below_claude", lambda score, model_id: score)
    assert {ASTRA, SOL61} <= _band_models()
    with pytest.raises(AssertionError):
        _assert_gpt_outside_the_claude_band()


def test_flag_off_is_byte_identical_to_the_rule_not_existing(monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(A, "_gpt_below_claude", lambda score, model_id: score)
        A._rank_reset()
        stub = {(p, i): tool(p, i) for p in PIDS for i in GPT_IDS + NON_GPT}
        pool_stub = _band_models()
    A._rank_reset()
    _flag(monkeypatch, False)
    off = {(p, i): tool(p, i) for p in PIDS for i in GPT_IDS + NON_GPT}
    assert off == stub                                    # exact equality, not approx
    assert _band_models() == pool_stub and {ASTRA, SOL61} <= pool_stub
    assert A._gc_ref[0] is None


# =========================================================================== #
# 2. (a) only when a Claude flagship is reachable AND healthy
# =========================================================================== #
@pytest.mark.parametrize("ids", [
    [ASTRA, SOL61, GLM, KIMI],                              # no Claude at all
    [ASTRA, SOL61, HAIKU],                                  # only the small tier
    [ASTRA, SOL61, "claude-opus-4.6", "claude-sonnet-4-5"],  # old Claude: no agentic board row
    [ASTRA, SOL61, "claude-3-opus"],
    [],                                                     # unreadable / empty fleet
])
def test_no_reachable_claude_flagship_changes_nothing(monkeypatch, ids):
    _fleet(monkeypatch, ids)
    for g in (ASTRA, SOL61, "gpt-7"):
        assert tool("openai", g) == pytest.approx(
            A._benchmark_score("openai", g) + A._ev_agentic_bonus(g)), g
    assert A._gc_ref[0] is None
    # GPT keeps its board score and may lead
    pool = _pool([ASTRA, GLM, KIMI])
    assert max(pool, key=A._agentic_score)[2] == ASTRA


def test_a_dead_stub_relay_opus_is_not_a_reachable_flagship(monkeypatch):
    # the REAL fleet filter (not the fake list): a relay Opus measured to fail 0 ok / 21
    # fail is a claim, not a model, so it anchors nothing and GPT keeps its score
    seed(RELAY, DEAD_OPUS, 0, 21, rok=0, rfail=5)
    assert A._chain_reliability_band(RELAY, DEAD_OPUS) >= 2
    monkeypatch.setattr(A, "_rank_fleet_ids", _REAL_RANK_FLEET_IDS)
    monkeypatch.setattr(A, "_cached_catalogs", lambda: {})
    monkeypatch.setattr(A, "_alive_models", lambda cats: [(RELAY, DEAD_OPUS), ("openai", ASTRA), ("nvidia", GLM)])
    A._rank_reset()
    assert A._rank_fleet_ids() == [ASTRA, GLM]
    assert tool("openai", ASTRA) == pytest.approx(143.595, abs=1e-3)
    assert A._gc_ref[0] is None


def test_the_same_relay_opus_untried_counts_like_it_does_for_the_chat_ladder(monkeypatch):
    # "same anchor logic": an untried relay claim is listed and not measured to fail,
    # so it anchors the chat ladder -- and this rule. The relay copy itself still pays
    # its 4.0 discount and gets no board bonus, so nothing real is promoted by it.
    monkeypatch.setattr(A, "_rank_fleet_ids", _REAL_RANK_FLEET_IDS)
    monkeypatch.setattr(A, "_cached_catalogs", lambda: {})
    monkeypatch.setattr(A, "_alive_models", lambda cats: [(RELAY, DEAD_OPUS), ("openai", ASTRA), ("nvidia", GLM)])
    A._rank_reset()
    assert A._rank_fleet_ids() == [DEAD_OPUS, ASTRA, GLM]
    assert A._rank_anchors()[("claude", "opus")][0] == (5, 5)         # the chat ladder sees it
    assert A._gc_ref[0] == pytest.approx(144.655, abs=1e-3)
    assert tool("openai", ASTRA) < A._gc_ref[0] - BAND
    assert tool(RELAY, DEAD_OPUS) == pytest.approx(chat(RELAY, DEAD_OPUS))   # no bonus, discounted
    assert tool(RELAY, DEAD_OPUS) < tool("nvidia", GLM) - 3.0


@pytest.mark.parametrize("why", ["blocked", "parked"])
def test_a_blocked_or_parked_claude_is_not_reachable(monkeypatch, why):
    monkeypatch.setattr(A, "_rank_fleet_ids", _REAL_RANK_FLEET_IDS)
    monkeypatch.setattr(A, "_cached_catalogs", lambda: {})
    monkeypatch.setattr(A, "_alive_models", lambda cats: [("anthropic", OPUS), ("openai", ASTRA)])
    if why == "blocked":
        monkeypatch.setattr(A, "_is_model_blocked_by_user", lambda pid, m: m == OPUS)
    else:
        monkeypatch.setattr(A, "_ctx_hop_wait_seconds", lambda pid, m: A._CTX_OVERFLOW_LONG_WAIT + 1
                            if pid == "anthropic" else 0)
    A._rank_reset()
    assert A._rank_fleet_ids() == [ASTRA]
    assert tool("openai", ASTRA) == pytest.approx(143.595, abs=1e-3)
    assert A._gc_ref[0] is None


def test_the_reference_is_the_best_reachable_full_size_claude(monkeypatch):
    _fleet(monkeypatch, [OPUS, SONNET, FABLE, HAIKU, ASTRA])
    assert A._gc_ref[0] is None                             # built lazily, on the first score
    tool("openai", ASTRA)
    assert A._gc_ref[0] == pytest.approx(tool("anthropic", OPUS))
    for ids, lead in (([SONNET, FABLE, ASTRA], SONNET), ([FABLE, ASTRA], FABLE)):
        _fleet(monkeypatch, ids)
        tool("openai", ASTRA)
        assert A._gc_ref[0] == pytest.approx(tool("anthropic", lead)), ids
        assert tool("openai", ASTRA) < A._gc_ref[0] - BAND
    # Haiku is the SMALL tier: it is never the reference
    _fleet(monkeypatch, [HAIKU, ASTRA])
    assert tool("openai", ASTRA) == pytest.approx(143.595, abs=1e-3)


# =========================================================================== #
# 3. (b) only lowers, (c) relays, (d) other families, (e) chat
# =========================================================================== #
def test_it_never_raises_any_score_in_any_shape(monkeypatch):
    _flag(monkeypatch, False)
    off = {(p, i): tool(p, i) for p in PIDS for i in GPT_IDS + NON_GPT}
    _flag(monkeypatch, True)
    lowered = 0
    for key, was in off.items():
        now = tool(*key)
        assert now <= was, key
        lowered += now < was
    assert lowered >= 4, "the rule does bite"


def test_non_gpt_families_are_exactly_untouched(monkeypatch):
    _flag(monkeypatch, False)
    off = {(p, i): tool(p, i) for p in PIDS for i in NON_GPT}
    _flag(monkeypatch, True)
    tool("openai", ASTRA)
    assert A._gc_ref[0] is not None                         # the rule is live...
    for key, was in off.items():
        assert tool(*key) == was, key                       # ...and moves none of these, exactly


def test_gpt_oss_and_unparsed_ids_are_not_gpt_family(monkeypatch):
    from modelrank import parse
    assert parse("gpt-oss-120b") is None
    tool("openai", ASTRA)
    assert A._gc_ref[0] is not None
    assert tool("openai", "gpt-oss-120b") == pytest.approx(chat("openai", "gpt-oss-120b"))


def test_chat_scores_are_identical_with_the_rule_on_and_off(monkeypatch):
    ids = GPT_IDS + NON_GPT
    _flag(monkeypatch, False)
    off = {(p, i): chat(p, i) for p in PIDS for i in ids}
    _flag(monkeypatch, True)
    tool("openai", ASTRA)
    assert A._gc_ref[0] is not None
    assert {(p, i): chat(p, i) for p in PIDS for i in ids} == off


def test_an_untried_relay_gpt_is_untouched_and_a_proven_one_clamps_like_first_party(monkeypatch):
    _flag(monkeypatch, False)
    untried_off = tool(RELAY, ASTRA)
    assert untried_off == pytest.approx(chat(RELAY, ASTRA)), "relay: discount, no board bonus"
    first_party_off = tool("openai", ASTRA)
    _flag(monkeypatch, True)
    # not double-penalised: the discounted relay copy sits far under the cap, exactly as before
    assert tool(RELAY, ASTRA) == untried_off
    assert tool(RELAY, ASTRA) < tool("anthropic", OPUS) - BAND - 5
    # a PROVEN relay copy competes on first-party terms (no discount, board bonus): so it must
    # clamp like first-party -- a relay GPT may never out-rank the first-party one
    seed_healthy(RELAY, ASTRA)
    assert A._relay_proven(RELAY, ASTRA)
    proven_on = tool(RELAY, ASTRA)
    assert proven_on == pytest.approx(tool("openai", ASTRA))
    assert proven_on < first_party_off
    assert tool("anthropic", OPUS) - proven_on > BAND
    # and the clamp never goes below the first-party clamp: no second penalty stacked on it
    assert proven_on >= tool("anthropic", OPUS) - BAND - 0.05


def test_the_rule_does_not_touch_the_relay_discount_or_proven_lead_logic(monkeypatch):
    seed_healthy(RELAY, OPUS)
    assert A._relay_proven(RELAY, OPUS)
    assert chat(RELAY, OPUS) == pytest.approx(chat("anthropic", OPUS))      # no discount, as before
    assert tool(RELAY, OPUS) == pytest.approx(tool("anthropic", OPUS))      # keeps its bonus
    assert A._relay_discount(RELAY, ASTRA) == A._RELAY_DISCOUNT["g4f"]       # unproven GPT: discount
    seed_healthy(RELAY, ASTRA)
    assert A._relay_discount(RELAY, ASTRA) == 0.0


# =========================================================================== #
# 4. Future ids rank themselves, newest-first order survives
# =========================================================================== #
def test_unknown_future_gpt_ids_inherit_and_stay_below_claude_in_newest_first_order(monkeypatch):
    ids = [ASTRA, SOL61, "gpt-6.2-sol", "gpt-7", "gpt-7.1-sol"]
    _flag(monkeypatch, False)
    raw = {i: tool("openai", i) for i in ids}
    _flag(monkeypatch, True)
    now = {i: tool("openai", i) for i in ids}
    best_claude = max(tool("anthropic", m) for m in (OPUS, SONNET, FABLE))
    for i in ids:
        assert best_claude - now[i] > BAND, i
        assert now[i] <= raw[i]
    # a flat clamp would tie them all; the order the boards + version gave must survive
    for a in ids:
        for b in ids:
            assert (raw[a] < raw[b]) == (now[a] < now[b]), (a, b)
            assert (raw[a] == raw[b]) == (now[a] == now[b]), (a, b)      # ties stay ties, nothing new
    assert now["gpt-7"] > now["gpt-6.2-sol"] > now[ASTRA] > now[SOL61]


def test_the_squeeze_is_monotone_bounded_and_lowers_only():
    tool("openai", ASTRA)
    ref = A._gc_ref[0]
    cap = ref - BAND - A._BAND_MINOR_STEP
    prev = None
    for i in range(0, 4000):
        raw = cap - 0.5 + i * 0.0025                          # from well under the cap to 9.5 over
        got = A._gpt_below_claude(raw, ASTRA)
        assert got <= raw + 1e-12
        assert got < cap + 1e-12
        assert ref - got > BAND
        if prev is not None:
            assert got >= prev, raw                           # never reorders two GPT scores
        if raw > cap:
            assert got > cap - A._GC_SLIVER - 1e-12
        prev = got
    assert A._gpt_below_claude(cap - 1.0, ASTRA) == cap - 1.0          # untouched well under the cap
    nan = float("nan")
    assert A._gpt_below_claude(nan, ASTRA) is nan


# =========================================================================== #
# 5. Cheap, deterministic, fail-open, flag semantics
# =========================================================================== #
def test_no_config_read_per_candidate(monkeypatch):
    tool("openai", ASTRA)                                    # builds the reference (reads the flag once)
    reads = []
    orig = config.get_flag

    def spy(name, default=False):
        reads.append(name)
        return orig(name, default)
    monkeypatch.setattr(config, "get_flag", spy)
    for _ in range(50):
        for i in GPT_IDS + NON_GPT:
            tool("openai", i)
            tool("anthropic", i)
    assert "gpt_below_claude_tools" not in reads


def test_the_flag_is_read_once_per_rebuild(monkeypatch):
    reads = []
    orig = config.get_flag

    def spy(name, default=False):
        if name == "gpt_below_claude_tools":
            reads.append(name)
        return orig(name, default)
    monkeypatch.setattr(config, "get_flag", spy)
    A._rank_reset()
    for _ in range(20):
        tool("openai", ASTRA)
    assert len(reads) == 1
    A._rank_reset()
    tool("openai", ASTRA)
    assert len(reads) == 2


def test_toggling_the_flag_takes_effect_when_the_reference_rebuilds(monkeypatch):
    assert tool("openai", ASTRA) < tool("anthropic", OPUS) - BAND
    _flag(monkeypatch, False)
    assert tool("openai", ASTRA) == pytest.approx(143.595, abs=1e-3)
    _flag(monkeypatch, True)
    assert tool("openai", ASTRA) < tool("anthropic", OPUS) - BAND


def test_it_is_deterministic():
    runs = [[tool(p, i) for p in PIDS for i in GPT_IDS + NON_GPT] for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]


def test_it_fails_open_on_any_error(monkeypatch):
    orig = config.get_flag

    def boom(name, default=False):
        if name == "gpt_below_claude_tools":
            raise RuntimeError("config unreadable")
        return orig(name, default)
    with monkeypatch.context() as m:
        m.setattr(config, "get_flag", boom)
        A._rank_reset()
        assert tool("openai", ASTRA) == pytest.approx(143.595, abs=1e-3)   # no rule, no crash
        assert A._gc_ref[0] is None
    A._rank_reset()
    with monkeypatch.context() as m:
        # an error inside the reference build must not take the anchors (or a score) down
        m.setattr(A, "_gc_build_reference", lambda ids: 1 / 0)
        assert isinstance(tool("openai", ASTRA), float)
    with monkeypatch.context() as m:
        m.setattr(A, "_rank_anchors", lambda: 1 / 0)
        assert A._gpt_below_claude(143.0, ASTRA) == 143.0


def test_the_pools_that_band_on_chat_scores_are_not_touched():
    # specialist / verifier / planner pools band on _benchmark_score (chat scale): same
    # numbers with the rule on as with it off
    on = {i: chat("openai", i) for i in GPT_IDS}
    tool("openai", ASTRA)
    assert A._gc_ref[0] is not None
    assert {i: chat("openai", i) for i in GPT_IDS} == on
