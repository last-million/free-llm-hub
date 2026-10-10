"""Newest and biggest first, INSIDE one family (owner rule 2026-10-08):

    "he should be smart to know higher models by version number, and for
     Claude: Opus is better than Sonnet and Sonnet better than Haiku, and Haiku
     is a short/small model."

MEASURED the same day: the g4f relay rows of claude-opus-5.5, claude-sonnet-5.5,
claude-sonnet-4-5, claude-sonnet-4, claude-haiku-4-5 and gemini-claude-opus-4-6
all scored the SAME 134.0 (the owner floor is family-wide), so Haiku 4.5 tied
Opus 5.5 and an older generation kept being picked beside the newest one.

modelrank.py only ever LOWERS the OLDER members of ONE family+tier relative to
the newest one the hub can REACH (computed from the listed models, not
hard-coded). Everything here is hermetic: fake fleets, no network.
"""
import pytest

import app as A
import modelrank as R


# --------------------------------------------------------------------------- #
# parse(): every spelling the hub sees
# --------------------------------------------------------------------------- #

PARSE_CASES = [
    # (id, family, tier, version)
    ("claude-opus-5.5", "claude", "opus", (5, 5)),
    ("srv_mr9dda21bf67b4c62086:anthropic/claude-opus-5.5", "claude", "opus", (5, 5)),
    ("GithubCopilot:claude-sonnet-5.5", "claude", "sonnet", (5, 5)),
    ("OpenCode:claude-opus-5-5", "claude", "opus", (5, 5)),
    ("claude-sonnet-4-5", "claude", "sonnet", (4, 5)),               # 4-5 == 4.5
    ("anthropic/claude-sonnet-4-5-20250929", "claude", "sonnet", (4, 5)),   # date suffix
    ("claude-sonnet-4", "claude", "sonnet", (4, 0)),
    ("claude-sonnet-4-20250514", "claude", "sonnet", (4, 0)),        # 4 + a date, not 4.20250514
    ("claude-opus-4-1-20250805", "claude", "opus", (4, 1)),
    ("claude-haiku-4-5", "claude", "haiku", (4, 5)),
    ("claude-4.5-haiku", "claude", "haiku", (4, 5)),                 # version before the tier
    ("claude-3-7-sonnet", "claude", "sonnet", (3, 7)),
    ("claude-3-5-sonnet-20241022", "claude", "sonnet", (3, 5)),
    ("claude3opus", "claude", "opus", (3, 0)),                       # glued (Perplexity)
    ("Perplexity:claude40sonnetthinking_labs", "claude", "sonnet", (4, 0)),
    ("Antigravity:gemini-claude-opus-4-6-thinking", "claude", "opus", (4, 6)),   # Claude, not Gemini
    ("Puter:anthropic:anthropic/claude-fable-5-1", "claude", "fable", (5, 1)),
    ("KiloCode:stealth/claude-opus-4.8", "claude", "opus", (4, 8)),
    ("claude-2.1", "claude", "", (2, 1)),
    ("claude-instant-1.2", "claude", "instant", (1, 2)),
    ("gpt-5.6-luna", "gpt", "", (5, 6)),
    ("srv_x:openai/gpt-6.1-sol", "gpt", "", (6, 1)),
    ("gpt-6-astra", "gpt", "", (6, 0)),
    ("gpt-5-mini", "gpt", "mini", (5, 0)),
    ("gemini-3.8-flash", "gemini", "flash", (3, 8)),
    ("google/models/gemini-3.5-flash", "gemini", "flash", (3, 5)),
    ("models/gemini-3.1-pro-preview-customtools", "gemini", "pro", (3, 1)),
    ("gemini-3.5-flash-lite", "gemini", "flash-lite", (3, 5)),
    ("grok-4.6", "grok", "", (4, 6)),
    ("grok-4.20", "grok", "", (4, 2)),                               # 4.20 reads as 4.2
    ("qwen/qwen3.8-27b:free", "qwen", "", (3, 8)),
    ("glm-5.3", "glm", "", (5, 3)),
    ("z-ai/glm-5.3-flashx", "glm", "flash", (5, 3)),
    ("deepseek-ai/deepseek-v4.1-flash", "deepseek", "flash", (4, 1)),
    ("deepseek/deepseek-v4-pro", "deepseek", "pro", (4, 0)),
    ("moonshotai/kimi-k3", "kimi", "", (3, 0)),
    ("moonshotai/kimi-k2.6", "kimi", "", (2, 6)),
    ("tencent/Hy4-preview", "hy", "", (4, 0)),
]


@pytest.mark.parametrize("model_id,family,tier,version", PARSE_CASES)
def test_parse_reads_every_spelling(model_id, family, tier, version):
    assert R.parse(model_id) == (family, tier, version), model_id


@pytest.mark.parametrize("model_id", [
    "claude-code", "claude-opus", "gpt-oss-120b", "gemini-flash-latest", "llama-3.3-70b",
    "deepseek-r1-distill-qwen-32b", "qwen-32b", "", None, "auto", "whisper-large-v3",
])
def test_parse_returns_none_when_family_or_version_is_unknown(model_id):
    assert R.parse(model_id) is None


def test_parse_never_raises():
    for junk in (object(), 12, b"x", ["claude-opus-5"], {"a": 1}):
        R.parse(junk)       # must not raise


# --------------------------------------------------------------------------- #
# gap(): the arithmetic the owner's numbers fix
# --------------------------------------------------------------------------- #

def test_a_major_generation_costs_at_least_two_and_a_half_points():
    for nmin in range(0, 10):
        for vmin in range(0, 100):
            assert R.gap((5, nmin), (4, vmin)) >= 2.5, (nmin, vmin)
            assert R.gap((6, nmin), (4, vmin)) >= 5.0, (nmin, vmin)


def test_a_newer_minor_leads_by_a_small_step_inside_the_band():
    for vmin in range(0, 20):
        for nmin in range(vmin, 21):
            assert R.gap((5, nmin), (5, vmin)) < 0.6
            assert R.gap((5, nmin), (5, vmin)) < A._AUTO_TOP_BAND


def test_older_is_strictly_further_down():
    versions = [(5, 5), (5, 4), (5, 0), (4, 9), (4, 5), (4, 0), (3, 7), (3, 0), (2, 1)]
    gaps = [R.gap((5, 5), v) for v in versions]
    assert gaps == sorted(gaps) and len(set(gaps)) == len(gaps), gaps


def test_the_newest_and_anything_newer_has_no_gap():
    assert R.gap((5, 5), (5, 5)) == 0.0
    assert R.gap((5, 5), (6, 0)) == 0.0
    assert R.gap((5, 5), (5, 7)) == 0.0


def test_tiers_are_the_owners():
    assert R.OPUS_OVER_SONNET == 0.6
    assert R.HAIKU_UNDER_SONNET >= 6.0
    assert R.tier_offset("claude", "opus", True) == 0.0
    assert R.tier_offset("claude", "sonnet", True) == 0.6
    assert R.tier_offset("claude", "haiku", True) == pytest.approx(0.6 + 6.0)
    # no Opus reachable: nothing for Sonnet to be under, Haiku is still small
    assert R.tier_offset("claude", "sonnet", False) == 0.0
    assert R.tier_offset("claude", "haiku", False) == pytest.approx(6.0)
    assert R.tier_offset("gpt", "", True) == 0.0


# --------------------------------------------------------------------------- #
# The ranking through app._benchmark_score
# --------------------------------------------------------------------------- #

RELAY = "g4f"
CLAUDE = [
    "srv_mr9dda21bf67b4c62086:anthropic/claude-opus-5.5",
    "srv_mr9dda21bf67b4c62086:anthropic/claude-sonnet-5.5",
    "srv_mp1v9cyha31b95fa8c9a:anthropic/claude-sonnet-4-5",
    "srv_mp1v9cyha31b95fa8c9a:anthropic/claude-sonnet-4",
    "srv_mp1v9cyha31b95fa8c9a:anthropic/claude-haiku-4-5",
    "Antigravity:gemini-claude-opus-4-6-thinking",
    "srv_mr9dda21bf67b4c62086:anthropic/claude-fable-5",
    "Puter:anthropic:anthropic/claude-fable-5-1",
    "claude3opus",
    "claude-3-7-sonnet",
]
GPT = [
    "srv_mr9dda21bf67b4c62086:openai/gpt-6.1-sol",
    "srv_mr9dda21bf67b4c62086:openai/gpt-6-astra",
    "srv_mrdypihj16e8b1776409:openai/gpt-6-luna",
    "srv_mrdypihj16e8b1776409:openai/gpt-5.6-luna",
    "gpt-5.5",
    "gpt-4o",
]
GEMINI = [
    "google/models/gemini-3.8-pro",
    "google/models/gemini-3.8-flash",
    "google/models/gemini-3.5-flash",
    "google/models/gemini-3.5-flash-lite",
    "models/gemini-3.1-pro-preview-customtools",
]
OTHERS = [
    "nvidia/z-ai/glm-5.3", "z-ai/glm-5.2", "moonshotai/kimi-k3",
    "qwen/qwen3.8-27b", "qwen/qwen3.6-27b",
    "deepseek-ai/deepseek-v4.1-flash", "deepseek-ai/deepseek-v4-pro",
    "stealth/space-bunny-alpha", "pixel-canary",
]
FLEET = CLAUDE + GPT + GEMINI + OTHERS


def _fleet(monkeypatch, ids):
    """Reachable fleet = `ids` (fresh anchors)."""
    monkeypatch.setattr(A, "_rank_fleet_ids", lambda: list(ids))
    A._rank_reset()


def score(model_id, pid=None):
    return A._benchmark_score(pid or "x", model_id)


@pytest.fixture
def ranked(monkeypatch):
    _fleet(monkeypatch, FLEET)
    return FLEET


def test_claude_orders_opus_sonnet_sonnet_sonnet_haiku(ranked):
    s = {m: score(m, RELAY) for m in CLAUDE}
    opus55, son55, son45, son4, haiku45 = CLAUDE[:5]
    assert s[opus55] > s[son55] > s[son45] > s[son4] > s[haiku45], s


def test_opus_leads_sonnet_by_the_owners_small_gap(ranked):
    assert score(CLAUDE[0]) - score(CLAUDE[1]) == pytest.approx(0.6)
    assert score(CLAUDE[0]) - score(CLAUDE[1]) < A._AUTO_TOP_BAND


def test_an_older_claude_generation_leaves_the_band_but_stays_a_candidate(ranked):
    top = score(CLAUDE[0])
    for older in (CLAUDE[2], CLAUDE[3], CLAUDE[4], CLAUDE[5]):
        s = score(older)
        assert s <= top - 2.5, (older, s)
        assert s < top - A._AUTO_TOP_BAND, "out of the 2-point auto band"
        assert s > 100, "...but not buried: it is still a strong fallback"


def test_haiku_is_six_under_sonnet_of_the_same_generation(monkeypatch):
    # 4.5 (the relay row) and 5.5 (it exists: AA 43)
    _fleet(monkeypatch, FLEET + ["claude-haiku-5.5", "claude-sonnet-4.5"])
    assert score("claude-sonnet-4.5") - score(CLAUDE[4]) >= 6.0
    assert score(CLAUDE[1]) - score("claude-haiku-5.5") >= 6.0
    # ...and the generation step can beat the tier step two generations on
    assert score("claude-haiku-5.5") > score("claude-3-7-sonnet")


def test_fable_is_pinned_between_opus_and_sonnet(ranked):
    opus, son = score(CLAUDE[0]), score(CLAUDE[1])
    fable = score("Puter:anthropic:anthropic/claude-fable-5-1")
    assert opus > fable > son, (opus, fable, son)
    assert score(CLAUDE[6]) < fable, "fable 5.1 above fable 5"


def test_legacy_claude_is_not_in_the_top_band_any_more(ranked):
    top = score(CLAUDE[0])
    assert score("claude3opus") < top - 2 * 2.5
    assert score("claude-3-7-sonnet") < score(CLAUDE[3])


def test_gpt_orders_by_version_and_keeps_codenames_as_they_were(monkeypatch):
    _fleet(monkeypatch, FLEET)
    sol61, astra6, luna6, luna56, g55, g4o = [score(m, RELAY) for m in GPT]
    assert sol61 > astra6 > luna56
    assert sol61 > luna6 > luna56
    assert astra6 - luna56 >= 2.5 and sol61 - luna56 >= 2.5
    assert sol61 > g55 > g4o
    # the codenames of one version keep exactly the score they had with no
    # ranking at all (no evidence to split them)
    _fleet(monkeypatch, [])
    plain = {m: score(m, RELAY) for m in GPT}
    assert plain[GPT[1]] == plain[GPT[2]] == astra6 == luna6
    assert plain[GPT[0]] == sol61


def test_gemini_pro_above_flash_above_older_flash(ranked):
    pro, flash38, flash35, lite, pro31 = [score(m) for m in GEMINI]
    assert pro > flash38 > flash35
    assert flash35 > lite, "flash-lite stays under flash"
    assert pro > pro31, "3.8 pro above 3.1 pro"


def test_the_owners_cross_family_order_is_unchanged(monkeypatch):
    """Adjustments only LOWER an older member of one family+tier: the newest
    member of each owner-ranked family keeps its exact score, so the order
    between families does not move."""
    _fleet(monkeypatch, [])
    plain = {m: score(m) for m in FLEET}
    _fleet(monkeypatch, FLEET)
    ranked_ = {m: score(m) for m in FLEET}
    for m in FLEET:
        assert ranked_[m] <= plain[m] + 1e-9, "never raised: %s" % m
    newest = [CLAUDE[0], "nvidia/z-ai/glm-5.3", "moonshotai/kimi-k3",
              "stealth/space-bunny-alpha", "pixel-canary", GPT[0], "qwen/qwen3.8-27b",
              "google/models/gemini-3.8-pro", "google/models/gemini-3.8-flash",
              "deepseek-ai/deepseek-v4.1-flash", "deepseek-ai/deepseek-v4-pro"]
    for m in newest:
        assert ranked_[m] == pytest.approx(plain[m]), m
    # OWNER DECISION 2026-10-10: Kimi K3 and GLM 5.3 are ranked by the boards
    # now (general: AA + LMArena), no longer pinned above Claude. Claude keeps its
    # floor (it has the top AA Index), and the no-evidence owner floors (Space
    # Bunny, Pixel Canary) stay above the board-ranked free pair, which sits close
    # together (Kimi K3 just over GLM 5.3 on the AA+Arena average).
    assert (ranked_[CLAUDE[0]] > ranked_["stealth/space-bunny-alpha"]
            > ranked_["pixel-canary"] > ranked_["moonshotai/kimi-k3"]
            >= ranked_["nvidia/z-ai/glm-5.3"])


def test_the_one_deliberate_exception_sonnet_and_fable_sit_under_the_old_138(monkeypatch):
    """Rule (b) 'Opus over Sonnet' and rule (d) 'keep the owner's families in
    order' cannot both hold for the NEWEST Sonnet: Opus is capped at the owner's
    138, so Sonnet 5.5 is 137.4 -- under Space Bunny (137.7) and Pixel Canary
    (137.6), which the 2026-07-31 family-wide floor used to put above them. The
    owner's later, more specific Opus > Sonnet wins; pinned here so it is a
    decision, not an accident."""
    _fleet(monkeypatch, FLEET)
    sonnet55, bunny, pixel = score(CLAUDE[1]), score("stealth/space-bunny-alpha"), score("pixel-canary")
    assert sonnet55 == pytest.approx(138.0 - R.OPUS_OVER_SONNET)
    assert sonnet55 < pixel < bunny
    fable51, fable5 = score("Puter:anthropic:anthropic/claude-fable-5-1"), score(CLAUDE[6])
    assert fable51 == pytest.approx(138.0 - R.FABLE_UNDER_OPUS)
    assert score(CLAUDE[0]) > bunny >= fable51 > fable5 > sonnet55


def test_the_owner_floor_ceiling_is_never_exceeded(ranked):
    ceiling = A._PREF_FLOORS[5]
    for m in CLAUDE:
        assert score(m) <= ceiling + 1e-9, m


def test_the_relay_discount_still_comes_after(ranked):
    for m in CLAUDE + GPT + GEMINI:
        assert score(m, RELAY) == pytest.approx(score(m, "x") - A._RELAY_DISCOUNT["g4f"]), m


def test_a_newly_listed_claude_6_leads_automatically(monkeypatch):
    _fleet(monkeypatch, FLEET + ["claude-opus-6"])
    assert score("claude-opus-6") == pytest.approx(A._PREF_FLOORS[5])
    assert score("claude-opus-6") - score(CLAUDE[0]) >= 2.5
    # Sonnet has no 6 yet, so Sonnet 5.5 is still the newest Sonnet
    assert score(CLAUDE[1]) == pytest.approx(A._PREF_FLOORS[5] - R.OPUS_OVER_SONNET)
    _fleet(monkeypatch, FLEET + ["claude-opus-6", "claude-sonnet-6"])
    assert score("claude-sonnet-6") == pytest.approx(A._PREF_FLOORS[5] - R.OPUS_OVER_SONNET)
    assert score("claude-sonnet-6") - score(CLAUDE[1]) >= 2.5, "5.5 is the fallback now"
    assert score(CLAUDE[1]) > 100


def test_a_newer_gpt_pushes_the_old_ladder_down(monkeypatch):
    _fleet(monkeypatch, FLEET + ["gpt-7-sol"])
    assert score("gpt-7-sol") - score(GPT[0]) >= 2.5
    assert score(GPT[0]) - score(GPT[3]) >= 2.5


def test_no_reachable_fleet_means_no_adjustment(monkeypatch):
    _fleet(monkeypatch, [])
    assert score(CLAUDE[0]) == score(CLAUDE[4]) == A._PREF_FLOORS[5]
    assert score("gpt-5.6-luna") == pytest.approx(136.2)


def test_a_family_with_nothing_reachable_is_left_alone(monkeypatch):
    _fleet(monkeypatch, ["nvidia/z-ai/glm-5.3", "qwen/qwen3.8-27b"])
    assert score("claude-haiku-4-5") == A._PREF_FLOORS[5]
    assert score("gpt-5.6-luna") == pytest.approx(136.2)


def test_a_weak_newest_release_is_not_an_anchor(monkeypatch):
    """A tiny new model (size-capped to 30) must not drag the strong older line."""
    _fleet(monkeypatch, ["qwen/qwen3.8-27b", "qwen/qwen4-0.5b"])
    assert score("qwen/qwen4-0.5b") <= 30
    # OWNER DECISION 2026-10-10: the 27B size variant is placed by its own board
    # evidence (below the full-size flagship floor), ~133.08 now, not 134.08.
    assert score("qwen/qwen3.8-27b") == pytest.approx(133.08)


def test_the_anchor_scoring_never_re_enters_the_ranking(monkeypatch):
    seen = []

    def boom(family, tier):
        seen.append((family, tier))
        raise AssertionError("an anchor build must score with the ranking off")

    monkeypatch.setattr(A, "_rank_fleet_ids", lambda: list(FLEET))
    A._rank_reset()
    monkeypatch.setattr(A, "_rank_anchor_for", boom)
    # the neutral score used to BUILD anchors
    assert A._benchmark_score("", CLAUDE[4], _rank=False) == A._PREF_FLOORS[5]
    assert not seen


def test_anchors_are_cached_between_scores(monkeypatch):
    calls = []

    def fleet():
        calls.append(1)
        return list(FLEET)

    monkeypatch.setattr(A, "_rank_fleet_ids", fleet)
    A._rank_reset()
    for m in FLEET * 3:
        score(m)
    assert len(calls) == 1


def test_a_broken_fleet_listing_never_breaks_a_score(monkeypatch):
    def boom():
        raise RuntimeError("catalog on fire")

    monkeypatch.setattr(A, "_rank_fleet_ids", boom)
    A._rank_reset()
    assert score(CLAUDE[0]) == A._PREF_FLOORS[5]
    assert score(CLAUDE[0]) == A._PREF_FLOORS[5]      # and does not retry per score


# --------------------------------------------------------------------------- #
# Older generations stay in the chain (fallbacks), and the relay cap keeps the
# best-ranked relays
# --------------------------------------------------------------------------- #

@pytest.fixture
def chain_world(monkeypatch):
    """Real scores on a fake fleet: a first-party host plus the g4f relay."""
    world = {
        "nvidia": ["z-ai/glm-5.3", "moonshotai/kimi-k3"],
        RELAY: [CLAUDE[0], CLAUDE[1], CLAUDE[2], CLAUDE[3], CLAUDE[4], GPT[0], GPT[3],
                "GithubCopilot:gemini-3.5-flash", "GithubCopilot:gemini-3.8-flash"],
    }
    ids = [m for models in world.values() for m in models]
    monkeypatch.setattr(A, "_available_providers", lambda *a, **k: list(world))
    monkeypatch.setattr(A, "_prefetch_auto_models", lambda pids: dict(world))
    monkeypatch.setattr(A, "_auto_models", lambda pid: list(world[pid]))
    monkeypatch.setattr(A, "_provider_capable", lambda pid, est: True)
    monkeypatch.setattr(A, "_supports_tools", lambda pid, m: True)
    monkeypatch.setattr(A, "_is_fast", lambda pid, m: True)
    monkeypatch.setattr(A, "_active_mode", lambda: A.MODE_ALL)
    monkeypatch.setattr(A, "_is_model_dead", lambda pid, m: False)
    monkeypatch.setattr(A.quota, "is_model_throttled", lambda pid, m: False)
    monkeypatch.setattr(A.quota, "model_status", lambda pid, m: {"exhausted": False})
    monkeypatch.setattr(A, "_context_ok", lambda pid, m, est: True)
    monkeypatch.setattr(A, "_sub_available_providers", lambda: [])
    monkeypatch.setattr(A.config, "get_flag", lambda k, d=None: d)
    for name in ("_reliability_penalty", "_latency_penalty", "_answer_quality_penalty",
                 "_sustain_penalty"):
        monkeypatch.setattr(A, name, lambda *a, **k: 0.0)
    monkeypatch.setattr(A, "_chain_reliability_band", lambda pid, m: 0)
    monkeypatch.setattr(A, "_below_declared_window", lambda pid, m: False)
    monkeypatch.setattr(A, "_is_low_quality", lambda m: False)
    _fleet(monkeypatch, ids)
    return world


HARD = [{"role": "user", "content": "Read big.txt and refactor the parser in src/parse.py."}]


def _tool_chain():
    with A.app.test_request_context():
        A._mark_turn_shape("hard", 16000)
        return A._build_chain("", "", 16000, require_tools=True, messages=HARD)


def test_the_relay_cap_keeps_the_best_ranked_relays(chain_world):
    chain = _tool_chain()
    relays = [m for p, m in chain if p == RELAY]
    assert len(relays) == A._TOOL_RELAY_MAX_HOPS
    ranked_relays = sorted(chain_world[RELAY], key=lambda m: score(m, RELAY), reverse=True)
    assert relays == ranked_relays[:A._TOOL_RELAY_MAX_HOPS], (relays, ranked_relays)
    assert relays[0] == CLAUDE[0], "Opus 5.5 is the first relay hop"


def test_older_generations_are_fallbacks_not_deleted(chain_world, monkeypatch):
    # give every id its own non-relay host so the relay cap does not apply
    world = {"p%d" % i: [m] for i, m in enumerate(CLAUDE[:5])}
    monkeypatch.setattr(A, "_available_providers", lambda *a, **k: list(world))
    monkeypatch.setattr(A, "_prefetch_auto_models", lambda pids: dict(world))
    monkeypatch.setattr(A, "_auto_models", lambda pid: list(world[pid]))
    _fleet(monkeypatch, [m for ms in world.values() for m in ms])
    chain = _tool_chain()
    got = [m for _p, m in chain]
    assert got == CLAUDE[:5], got      # Opus 5.5 > Sonnet 5.5 > 4.5 > 4 > Haiku 4.5, all present


# --------------------------------------------------------------------------- #
# One failure is not "measured to fail" (the 5.5 / gpt-6 relay pairs sat in the
# sick tail on exactly one failure each)
# --------------------------------------------------------------------------- #

def _fail(pid, model, n=1, ok=0, junk=False):
    for _ in range(ok):
        A._record_outcome(pid, model, True)
    for _ in range(n):
        A._record_outcome(pid, model, False, junk=junk)


@pytest.fixture
def clean_ledger():
    with A._outcome_lock:
        saved = dict(A._outcomes)
        A._outcomes.clear()
    yield
    with A._outcome_lock:
        A._outcomes.clear()
        A._outcomes.update(saved)


def test_a_single_failure_does_not_put_a_pair_in_the_sick_tail(clean_ledger):
    _fail("g4f", "srv_a:gpt-6-astra", 1)
    assert A._reliability("g4f", "srv_a:gpt-6-astra") < A._CHAIN_UNRELIABLE, "premise: 1/3"
    assert A._chain_reliability_band("g4f", "srv_a:gpt-6-astra") == 0


def test_two_failures_a_junk_answer_and_a_good_record_keep_their_bands(clean_ledger):
    _fail("g4f", "srv_a:m", 2)
    assert A._chain_reliability_band("g4f", "srv_a:m") == 2
    _fail("g4f", "srv_a:junk", 1, junk=True)          # a junk answer counts double
    assert A._chain_reliability_band("g4f", "srv_a:junk") == 2
    _fail("g4f", "srv_a:good", 1, ok=5)
    assert A._chain_reliability_band("g4f", "srv_a:good") == 0


def test_a_first_party_gemini_does_not_hand_the_relay_slots_to_weaker_relay_copies(
        chain_world, monkeypatch):
    """MEASURED shape of the live fleet: google/gemini-3.8-flash (134.07, tool
    proven) is alive, so relay Claude at 134.0 is under the lead gate by 0.07
    while every relay COPY of a gemini-3 id is 'proven' and leads. The three
    relay slots went to those weaker copies first, and the best relay model
    (Opus 5.5) was dropped by the cap. The cap keeps the best-ranked relays."""
    chain_world["google"] = ["models/gemini-3.8-flash"]
    chain_world[RELAY] = chain_world[RELAY] + [
        "GithubCopilot:gemini-3.6-flash", "GithubCopilot:gemini-3.7-flash"]
    ids = [m for models in chain_world.values() for m in models]
    monkeypatch.setattr(A, "_available_providers", lambda *a, **k: list(chain_world))
    monkeypatch.setattr(A, "_prefetch_auto_models", lambda pids: dict(chain_world))
    monkeypatch.setattr(A, "_auto_models", lambda pid: list(chain_world[pid]))
    _fleet(monkeypatch, ids)
    chain = _tool_chain()
    relays = [m for p, m in chain if p == RELAY]
    best = sorted(chain_world[RELAY], key=lambda m: score(m, RELAY), reverse=True)
    assert len(relays) == A._TOOL_RELAY_MAX_HOPS
    assert set(relays) == set(best[:A._TOOL_RELAY_MAX_HOPS]), (relays, best[:4])
    assert CLAUDE[0] in relays


def test_the_reachable_fleet_skips_parked_and_blocked_providers_and_models(monkeypatch):
    """g4f was parked 21 h on a Retry-After: its Opus 5.5 is not 'reachable', so it
    must not hold the first-party Claude down -- but a short burst 429 must not
    flap the anchors either. A model the user blocked is not reachable."""
    monkeypatch.setattr(A, "_cached_catalogs", lambda: {
        "g4f": ["srv_a:anthropic/claude-opus-5.5"], "nvidia": ["anthropic/claude-sonnet-4-5"]})
    waits = {"g4f": 75000.0, "nvidia": 0.0}
    monkeypatch.setattr(A, "_ctx_hop_wait_seconds", lambda pid, m: waits[pid])
    monkeypatch.setattr(A, "_is_model_blocked_by_user", lambda pid, m: False)
    assert A._rank_fleet_ids() == ["anthropic/claude-sonnet-4-5"]
    waits["g4f"] = 60.0                      # a burst: still reachable
    assert sorted(A._rank_fleet_ids()) == ["anthropic/claude-sonnet-4-5",
                                          "srv_a:anthropic/claude-opus-5.5"]
    monkeypatch.setattr(A, "_is_model_blocked_by_user", lambda pid, m: "opus" in m)
    assert A._rank_fleet_ids() == ["anthropic/claude-sonnet-4-5"]


def test_a_listing_measured_to_fail_is_not_reachable(monkeypatch):
    """One bogus higher-version id on one relay must not hold the real family
    down: a listing the hub has measured to fail (2+ outcomes, mostly failures)
    does not count as the newest reachable release."""
    monkeypatch.setattr(A, "_cached_catalogs", lambda: {
        "g4f": ["srv_a:anthropic/claude-opus-9"], "nvidia": ["anthropic/claude-opus-5.5"]})
    monkeypatch.setattr(A, "_ctx_hop_wait_seconds", lambda pid, m: 0.0)
    monkeypatch.setattr(A, "_is_model_blocked_by_user", lambda pid, m: False)
    monkeypatch.setattr(A, "_chain_reliability_band",
                        lambda pid, m: 2 if "opus-9" in m else 0)
    assert A._rank_fleet_ids() == ["anthropic/claude-opus-5.5"]
    A._rank_reset()
    assert score("anthropic/claude-opus-5.5") == A._PREF_FLOORS[5], "not held down by the bogus id"
