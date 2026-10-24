"""Frontier ranking: real Claude / GPT first, new ids rank themselves (2026-10-23).

Owner: "If new strong available models exist, add them in perfect order... the
most prioritised model is GLM... For Claude and GPT they must actually work, not
just be dummy models. Even when NEW models become available from different
providers they must rank right automatically."

WHY GLM LED TOOL TURNS (measured with the owner's cached boards): z-ai/glm-5.3
on nvidia = 134.03 chat + 4.82 Terminal-Bench 4.0 bonus = 138.85, while relay
claude-opus-5.5 = 138 - 4.0 (relay discount) = 134.00 with NO bonus (no board row
for Claude). Every flagship relay copy on the owner's ledger is a measured dead
stub (opus-5.5 0 ok / 21 fail, fable-5.1 0/33, gpt-6.1-sol 0/20), so the rows
below are built from those real ledger rows.

Hermetic: the AA cache, the fleet and the outcome ledger are replaced per test;
nothing reads the owner's files, no network.
"""
import time

import pytest

import app as A
import benchmarks as B
import config
import modelrank as R

RELAY = "g4f"

# --- real rows from the owner's perf-stats.json (2026-10-23) -----------------
DEAD_OPUS = "srv_mr9dda21bf67b4c62086:anthropic/claude-opus-5.5"      # 0 ok / 21 fail
DEAD_SONNET = "srv_mr9dda21bf67b4c62086:anthropic/claude-sonnet-5.5"
DEAD_SOL61 = "srv_mr9dda21bf67b4c62086:openai/gpt-6.1-sol"            # 0 ok / 20 fail
DEAD_FABLE = "srv_mr9dda21bf67b4c62086:anthropic/claude-fable-5.1"    # 0 ok / 33 fail
LUNA6 = "srv_mrdypihj16e8b1776409:openai/gpt-6-luna"                  # 37 ok / 1 fail
LUNA56 = "srv_mrdypihj16e8b1776409:openai/gpt-5.6-luna"               # 21 ok / 1 fail, idle 22 h

GLM53 = "z-ai/glm-5.3"
GLM53_PID = "nvidia"

FLEET = [
    DEAD_OPUS, DEAD_SONNET, DEAD_SOL61, DEAD_FABLE, LUNA6, LUNA56,
    "claude-opus-5.5", "claude-sonnet-5.5", "claude-haiku-5.5", "gpt-6.1-sol", "gpt-6-astra",
    GLM53, "moonshotai/kimi-k3", "gemini-3.8-flash", "qwen/qwen3.8-27b",
]


@pytest.fixture(autouse=True)
def _world(monkeypatch):
    monkeypatch.setattr(A, "_aa_scores", {})
    monkeypatch.setattr(A, "_outcomes", {})
    monkeypatch.setattr(A, "_rank_fleet_ids", lambda: list(FLEET))
    A._rank_reset()
    yield
    A._rank_reset()


def _fleet(monkeypatch, ids):
    monkeypatch.setattr(A, "_rank_fleet_ids", lambda: list(ids))
    A._rank_reset()


def _flag(monkeypatch, on):
    orig = config.get_flag

    def fake(name, default=False):
        if name == "relay_proven_lead":
            return on
        return orig(name, default)
    monkeypatch.setattr(config, "get_flag", fake)


def seed(pid, model, ok, fail, rok=None, rfail=0, idle_hours=0.0):
    now = time.time()
    rec = {"ok": ok, "fail": fail, "last": now - idle_hours * 3600.0}
    if rok is not None:
        rec.update(rok=rok, rfail=rfail, rstart=now - 3600.0)
    A._outcomes[(pid, model)] = rec


def seed_tools(pid, model, ok=8, fail=0):
    """A measured TOOL-turn record (in memory only, like the hub's)."""
    A._tool_outcomes[(pid, model)] = {"ok": ok, "fail": fail, "last": time.time()}


def seed_healthy(pid, model):
    seed(pid, model, 30, 1, rok=8, rfail=0)
    seed_tools(pid, model)


def chat(pid, model):
    return A._benchmark_score(pid, model)


def tool(pid, model):
    return A._agentic_score((A._benchmark_score(pid, model), pid, model))


# =========================================================================== #
# 1. The board rows: dated, sourced, nothing invented
# =========================================================================== #
def test_new_rows_carry_their_own_date_and_source_and_exact_figures():
    opus = B.agentic_evidence("claude-opus-5.5")
    sonnet = B.agentic_evidence("claude-sonnet-5.5")
    k31 = B.agentic_evidence("moonshotai/kimi-k3.1-pro")
    argon = B.agentic_evidence("gemini-4-argon")
    assert (opus["tb4"], sonnet["tb4"], k31["tb4"], argon["tb4"]) == (59.6, 63.6, 67.2, 57.1)
    assert (opus["automation"], sonnet["automation"], k31["automation"], argon["automation"]) == (
        69.5, 71.8, 73.3, 77.5)
    assert (opus["aa"], sonnet["aa"], k31["aa"], argon["aa"]) == (58, 56, 55, 53)
    # Kimi K3.1 Pro was last touched on 2026-10-23; the three rows completed from the
    # pages' embedded chart data carry the day they were read
    assert k31["date"] == "2026-10-23"
    for ev in (opus, sonnet, argon):
        assert ev["date"] == "2026-10-24"
    for ev in (opus, sonnet, k31, argon):
        assert "artificialanalysis.ai" in ev["source"]
    # the older rows keep the module-level snapshot stamp
    assert B.agentic_evidence(GLM53)["date"] == B.DATE


def test_a_figure_the_page_did_not_show_is_none_never_guessed():
    # 2026-10-24: the figures the 10-23 reading called "NOT FOUND" were in the pages'
    # embedded chart data, so Argon's Terminal-Bench 4.0 is its OWN 57.1 (it used to
    # borrow gemini-3.8-flash's 20) and Opus / Sonnet carry their AutomationBench.
    argon = B.agentic_evidence("gemini-4-argon")
    assert argon["tb4"] == 57.1 and "inherited_fields" not in argon
    assert B.agentic_delta(argon) == pytest.approx(B.TB_WEIGHT * 0.571 + B.AUTO_WEIGHT * 0.775)
    assert A._ev_agentic_bonus("gemini-4-argon") > A._ev_agentic_bonus("gemini-3.8-flash") + 3
    assert tool("google", "gemini-4-argon") > tool("google", "models/gemini-3.8-flash") + 3
    # a figure a page really does not show is still None, never guessed
    assert B.agentic_evidence("gpt-5.6-terra")["automation"] is None       # not on AutomationBench-AA
    assert B.agentic_evidence("gemini-3.1-pro")["automation"] is None
    assert B.general_evidence("moonshotai/kimi-k3.1-pro")["arena"] is None
    # NO row on purpose: Sol 6.0 (AA lists no figures) and Nemotron (no modelrank family)
    for mid in ("gpt-6-sol", "nvidia/nemotron-3.5-super-vl-120b-a12b", "nemotron-3-ultra"):
        assert B.agentic_evidence(mid) is None, mid
        assert B.general_evidence(mid) is None, mid
    assert A._ev_agentic_bonus("nvidia/nemotron-3.5-super-vl-120b-a12b") == 0.0


def test_claude_now_earns_the_agentic_bonus_glm_has_always_had():
    glm = A._ev_agentic_bonus(GLM53)
    assert A._ev_agentic_bonus("claude-opus-5.5") == pytest.approx(5.96 + 0.695)
    assert A._ev_agentic_bonus("claude-sonnet-5.5") == pytest.approx(6.36 + 0.718)
    assert A._ev_agentic_bonus("claude-opus-5.5") > glm
    # a 4-point TB4.0 gap Sonnet over Opus (+0.023 AutomationBench) is 0.42 --
    # smaller than the owner's 0.6 chat gap, so Opus stays ahead of Sonnet on tool
    # turns too (by ~0.18)
    first_party = {m: tool("anthropic", m) for m in ("claude-opus-5.5", "claude-sonnet-5.5")}
    assert first_party["claude-opus-5.5"] > first_party["claude-sonnet-5.5"]


def test_new_ids_inherit_their_predecessors_evidence_plus_a_bump():
    for new, old, day in (("claude-opus-6", "claude-opus-5.5", "2026-10-24"),
                          ("claude-sonnet-5.6", "claude-sonnet-5.5", "2026-10-24"),
                          ("moonshotai/kimi-k3.2", "moonshotai/kimi-k3.1-pro", "2026-10-23")):
        ev = B.agentic_evidence(new)
        assert ev["inherited_from"] == old and ev["date"] == day, new
        assert B.agentic_delta(ev) == pytest.approx(B.agentic_delta(B.agentic_evidence(old)))
        assert ev["floor_bump"] > 0
    # K3.1 Pro used to inherit K3's tb4=13 (a 54-point understatement)
    assert A._ev_agentic_bonus("moonshotai/kimi-k3.1-pro") > 7 > A._ev_agentic_bonus("moonshotai/kimi-k3")


def test_a_pro_cut_is_never_scored_under_its_own_flash_cut():
    pro, flash = "models/gemini-3.9-pro", "models/gemini-3.9-flash"
    assert B.agentic_evidence(pro)["inherited_from"] == "gemini-3.8-flash"
    assert A._ev_agentic_bonus(pro) > 0
    assert A._ev_agentic_bonus(pro) == pytest.approx(A._ev_agentic_bonus(flash))
    assert tool("google", pro) > tool("google", flash)        # chat: pro band above flash band


# =========================================================================== #
# 2. Tool turns: who leads, with a healthy world and with the owner's world
# =========================================================================== #
def test_first_party_claude_leads_glm_outside_the_auto_band():
    glm = tool(GLM53_PID, GLM53)
    assert tool("anthropic", "claude-opus-5.5") - glm > A._AUTO_TOP_BAND
    assert tool("anthropic", "claude-sonnet-5.5") - glm > A._AUTO_TOP_BAND


def test_an_untried_relay_claude_stays_behind_glm_exactly_as_before():
    glm = tool(GLM53_PID, GLM53)
    for mid in ("claude-opus-5.5", "claude-sonnet-5.5", DEAD_OPUS):
        assert tool(RELAY, mid) == pytest.approx(chat(RELAY, mid)), "no board bonus"
        assert tool(RELAY, mid) < glm - 3.0, mid
    assert chat(RELAY, "claude-opus-5.5") == pytest.approx(138.0 - A._RELAY_DISCOUNT["g4f"])


def test_a_proven_relay_opus_competes_on_the_same_terms_as_first_party():
    seed_healthy(RELAY, "claude-opus-5.5")
    assert A._relay_proven(RELAY, "claude-opus-5.5")
    assert chat(RELAY, "claude-opus-5.5") == pytest.approx(chat("anthropic", "claude-opus-5.5"))
    assert tool(RELAY, "claude-opus-5.5") == pytest.approx(tool("anthropic", "claude-opus-5.5"))
    assert tool(RELAY, "claude-opus-5.5") - tool(GLM53_PID, GLM53) > A._AUTO_TOP_BAND
    # ...and it is a lead candidate on board evidence alone
    assert not A._rp_evidence_only_lead(RELAY, "claude-opus-5.5", chat(RELAY, "claude-opus-5.5"))
    assert A._may_lead_agentic(chat(RELAY, "claude-opus-5.5"), "claude-opus-5.5")


def test_the_owners_real_ledger_rows_decide_real_from_dummy():
    seed(RELAY, DEAD_OPUS, 0, 21, rok=0, rfail=5)
    seed(RELAY, DEAD_SOL61, 0, 20, rok=0, rfail=1)
    seed(RELAY, DEAD_FABLE, 0, 33, rok=0, rfail=1)
    seed(RELAY, LUNA6, 37, 1, rok=9, rfail=0)
    seed(RELAY, LUNA56, 21, 1, rok=0, rfail=1, idle_hours=22.5)
    for dead in (DEAD_OPUS, DEAD_SOL61, DEAD_FABLE):
        assert not A._relay_proven(RELAY, dead), dead
        assert tool(RELAY, dead) < chat(RELAY, dead) - 5, "the measured failure also demotes it"
    # live check 2026-10-23: the luna ids ANSWER but never emit a native tool call,
    # so chat health alone (37 ok / 1 fail) is not proof...
    assert not A._relay_proven(RELAY, LUNA6)
    seed_tools(RELAY, LUNA6, ok=0, fail=5)
    assert not A._relay_proven(RELAY, LUNA6), "...and failing tool turns veto it"
    seed_tools(RELAY, LUNA6)                     # only a measured tool-turn record proves it
    assert A._relay_proven(RELAY, LUNA6), "37 ok / 1 fail, 8 tool turns ok: a real tool model"
    assert not A._relay_proven(RELAY, LUNA56), "good but idle 22 h: the fade makes it unproven again"
    # the real one pays no discount, but it is LUNA: the small tier stays under the flagships
    assert chat(RELAY, LUNA6) == pytest.approx(chat("openai", LUNA6))
    assert chat(RELAY, LUNA6) < chat("openai", "gpt-6.1-sol") - 5
    assert tool(RELAY, LUNA6) < tool(GLM53_PID, GLM53)


@pytest.mark.parametrize("damage", [
    "thin",            # 3 successes is not a record
    "flaky",           # 12 ok / 8 fail
    "stale",           # healthy a day and a half ago
    "recent_failure",  # a 429/timeout in the last 10 minutes
    "resting",         # same-way failure streak
    "benched",         # junk bench
    "canary",          # failed the answer canary twice
    "relay_server",    # the relay server failed tool turns repeatedly
    "never_chains",    # a sibling change's hook
])
def test_a_healthy_looking_record_is_not_proof_when_anything_says_it_is_failing(monkeypatch, damage):
    m = "claude-opus-5.5"
    seed_healthy(RELAY, m)
    assert A._relay_proven(RELAY, m)
    if damage == "thin":
        seed(RELAY, m, 3, 0, rok=3, rfail=0)
    elif damage == "flaky":
        seed(RELAY, m, 12, 8, rok=3, rfail=3)
    elif damage == "stale":
        seed(RELAY, m, 30, 1, idle_hours=36)
    elif damage == "recent_failure":
        monkeypatch.setattr(A, "_recent_hop_failure", lambda pid, model: "429")
    elif damage == "resting":
        monkeypatch.setattr(A, "_pair_resting", lambda pid, model: True)
    elif damage == "benched":
        monkeypatch.setattr(A, "_is_pair_benched", lambda pid, model: True)
    elif damage == "canary":
        monkeypatch.setattr(A, "_answer_quality_penalty", lambda pid, model: 15.0)
    elif damage == "relay_server":
        monkeypatch.setattr(A, "_relay_tool_sick", lambda pid, model: True)
    elif damage == "never_chains":
        monkeypatch.setattr(A, "_never_chains", lambda pid, model: True, raising=False)
    assert not A._relay_proven(RELAY, m), damage
    assert chat(RELAY, m) == pytest.approx(138.0 - 4.0)
    assert tool(RELAY, m) <= chat(RELAY, m) + 1e-9, "unproven again: no board bonus"


def test_the_rule_is_only_for_claude_and_gpt_relays_open_weight_relays_are_untouched():
    glm_relay = "srv_mkombumpae45db46dcb8:z-ai/glm-5.3"
    assert not A._rp_claim_pair(RELAY, glm_relay)
    assert tool(RELAY, glm_relay) - chat(RELAY, glm_relay) == pytest.approx(A._ev_agentic_bonus(glm_relay))
    assert chat(RELAY, glm_relay) == pytest.approx(chat("x", glm_relay) - 4.0)
    # a non-relay pid is never gated, a blank pid (the anchor build) never pays or waives
    assert not A._rp_claim_pair("anthropic", "claude-opus-5.5")
    assert chat("", "claude-opus-5.5") == chat("anthropic", "claude-opus-5.5")


def test_flag_off_restores_the_old_relay_ordering(monkeypatch):
    seed_healthy(RELAY, "claude-opus-5.5")
    _flag(monkeypatch, False)
    assert not A._relay_proven(RELAY, "claude-opus-5.5")
    assert chat(RELAY, "claude-opus-5.5") == pytest.approx(134.0)
    assert tool(RELAY, "claude-opus-5.5") == pytest.approx(134.0)
    assert tool("anthropic", "claude-opus-5.5") > 140, "first-party evidence is not behind the flag"


def test_proof_fails_closed_on_any_error(monkeypatch):
    seed_healthy(RELAY, "claude-opus-5.5")
    monkeypatch.setattr(A, "_reliability", lambda pid, model: 1 / 0)
    assert A._relay_proven(RELAY, "claude-opus-5.5") is False


def test_unproven_relay_claude_does_not_lead_on_evidence_alone(monkeypatch):
    # a first-party gemini-3 (tool-proven) at 134.1 sits 0.1 above the relay Opus
    assert A._rp_evidence_only_lead(RELAY, "claude-opus-5.5", 134.0)
    assert not A._rp_evidence_only_lead("anthropic", "claude-opus-5.5", 134.0)   # first party: evidence leads
    assert A._may_lead_agentic(134.0, "claude-opus-5.5")                    # the 2-arg gate is unchanged
    pool = [(chat(RELAY, "claude-opus-5.5"), RELAY, "claude-opus-5.5"),
            (134.1, "google", "models/gemini-3.8-flash"),
            (chat(GLM53_PID, GLM53), GLM53_PID, GLM53)]
    assert (RELAY, "claude-opus-5.5") not in {(c[1], c[2]) for c in A._may_lead_pool(pool)}
    seed_healthy(RELAY, "claude-opus-5.5")
    pool[0] = (chat(RELAY, "claude-opus-5.5"), RELAY, "claude-opus-5.5")
    assert (RELAY, "claude-opus-5.5") in {(c[1], c[2]) for c in A._may_lead_pool(pool)}


# =========================================================================== #
# 3. Tier order: GPT codenames and Claude generations
# =========================================================================== #
def test_luna_is_the_small_gpt_tier_like_haiku(monkeypatch):
    _fleet(monkeypatch, ["gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna", "gpt-5.6-luna"])
    sol, astra, luna, luna56 = (chat("x", m) for m in ("gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna", "gpt-5.6-luna"))
    assert sol > astra > luna > luna56
    assert astra - luna > 5.5 and sol - luna > 5.5
    assert R.parse("gpt-6-luna").tier == "luna" and R.parse("gpt-6-astra").tier == ""


def test_lunas_keep_their_own_order_when_the_reachable_flagship_is_older(monkeypatch):
    # The owner's fleet 2026-10-23: every GPT-6 flagship copy is a dead stub (not
    # reachable), so the only flagship left is gpt-5.4. Both lunas are NEWER than
    # that anchor; they used to tie at flagship - 6.
    _fleet(monkeypatch, ["gpt-5.4", "gpt-6-luna", "gpt-5.6-luna"])
    l6, l56, flagship = chat("x", "gpt-6-luna"), chat("x", "gpt-5.6-luna"), chat("x", "gpt-5.4")
    assert l6 > l56, (l6, l56)
    assert flagship - l6 >= R.GPT_LUNA_UNDER_FLAGSHIP - 1e-9 - 0.01
    # ...and a proven luna keeps that order with no relay discount
    assert chat("x", "gpt-6-luna") <= A._PREF_FLOORS[5] - R.GPT_LUNA_UNDER_FLAGSHIP


def test_borrowed_figures_are_labelled_in_the_tracking_view():
    # no listed row has a hole any more (Argon's TB4.0 is read), so the fill rule is
    # exercised on a synthetic one: a newer listed gemini with no TB4.0 figure
    real = B.TABLE
    rows = dict(real)
    rows["gemini-4.1-argon"] = {"tb4": None, "automation": 80.0, "aa": 54, "arena": 1530.0,
                                "date": "2026-11-01", "source": "synthetic"}
    B.TABLE = rows
    B.reset()
    try:
        view = A._ev_agentic_evidence_view("gemini-4.1-argon")
        assert view["tb4"] == 57.1 and view["inherited_fields"] == {"tb4": "gemini-4-argon"}
        assert view["automation"] == 80.0
        assert "inherited_fields" not in A._ev_agentic_evidence_view("claude-opus-5.5")
        assert "inherited_fields" not in A._ev_agentic_evidence_view("gemini-4-argon")
    finally:
        B.TABLE = real
        B.reset()


def test_a_small_release_never_anchors_the_flagship_line(monkeypatch):
    _fleet(monkeypatch, ["gpt-6.2-sol", "gpt-6.1-sol"])
    before = chat("x", "gpt-6.2-sol")
    _fleet(monkeypatch, ["gpt-6.2-sol", "gpt-6.1-sol", "gpt-7-luna"])
    assert chat("x", "gpt-6.2-sol") == pytest.approx(before)          # gpt-7-luna did not push it down
    assert chat("x", "gpt-7-luna") < chat("x", "gpt-6.2-sol") - 5


def test_same_generation_order_holds_when_a_newer_opus_exists(monkeypatch):
    _fleet(monkeypatch, ["claude-opus-6", "claude-opus-5.5", "claude-sonnet-5.5", "claude-haiku-5.5"])
    op6, op55, son55, hai55 = (chat("x", m) for m in
                               ("claude-opus-6", "claude-opus-5.5", "claude-sonnet-5.5", "claude-haiku-5.5"))
    assert op6 > op55 > son55 > hai55
    assert op55 - son55 == pytest.approx(R.OPUS_OVER_SONNET)
    assert op6 - op55 >= 2.5                                           # a generation behind leaves the band


def test_the_clamp_changes_nothing_while_that_generation_is_the_newest(monkeypatch):
    ids = ["claude-opus-5.5", "claude-sonnet-5.5", "claude-haiku-5.5", "claude-fable-5.1"]
    _fleet(monkeypatch, ids)
    assert chat("x", "claude-sonnet-5.5") == pytest.approx(138.0 - R.OPUS_OVER_SONNET)
    assert chat("x", "claude-haiku-5.5") == pytest.approx(138.0 - R.OPUS_OVER_SONNET - R.HAIKU_UNDER_SONNET)
    assert chat("x", "claude-fable-5.1") == pytest.approx(138.0 - R.FABLE_UNDER_OPUS)


# =========================================================================== #
# 4. GENERALITY: hypothetical future ids, every id shape, no code change
# =========================================================================== #
def _shapes(core, vendor):
    return [
        core,                                   # bare
        "%s/%s" % (vendor, core),               # vendor prefix
        "%s/%s:free" % (vendor, core),          # openrouter slug
        "srv_ab12cd34ef56:%s/%s" % (vendor, core),   # g4f relay server prefix
        "pa:1a2b3c4d:%s" % core,                # g4f 'pa' prefix
        "GithubCopilot:%s" % core,              # provider tag
        "%s-20261101" % core,                   # date suffix
    ]


def _strictly_decreasing(monkeypatch, chain, vendor, pid="x", extra=()):
    """chain: cores best -> worst. For EVERY id shape, with the whole chain
    (in that shape) reachable, each core scores above the next."""
    for k in range(len(_shapes(chain[0], vendor))):
        ids = [_shapes(c, vendor)[k] for c in chain]
        _fleet(monkeypatch, ids + list(extra))
        scores = [chat(pid, i) for i in ids]
        for hi, lo, a, b in zip(ids, ids[1:], scores, scores[1:]):
            assert a > b, (hi, a, lo, b)


def test_newer_claude_above_older_in_every_id_shape(monkeypatch):
    _strictly_decreasing(monkeypatch, ["claude-opus-6", "claude-opus-5.6", "claude-opus-5.5",
                                       "claude-opus-4-8"], "anthropic")
    _strictly_decreasing(monkeypatch, ["claude-sonnet-5.6", "claude-sonnet-5.5", "claude-sonnet-4-5"],
                         "anthropic")
    _strictly_decreasing(monkeypatch, ["claude-haiku-5.6", "claude-haiku-5.5", "claude-haiku-4-5"],
                         "anthropic")


def test_opus_above_sonnet_above_haiku_of_the_same_generation_in_every_shape(monkeypatch):
    for gen in ("claude-%s-5.6", "claude-%s-6", "claude-%s-5.5"):
        for k in range(len(_shapes("x", "anthropic"))):
            ids = [_shapes(gen % t, "anthropic")[k] for t in ("opus", "sonnet", "haiku")]
            _fleet(monkeypatch, ids)
            o, s, h = (chat("x", i) for i in ids)
            assert o > s > h, (ids, o, s, h)
            assert o - s == pytest.approx(R.OPUS_OVER_SONNET) and s - h == pytest.approx(R.HAIKU_UNDER_SONNET)


def test_gpt_new_versions_and_codenames_in_every_shape(monkeypatch):
    _strictly_decreasing(monkeypatch, ["gpt-7", "gpt-6.2", "gpt-6.1", "gpt-5.6"], "openai")
    _strictly_decreasing(monkeypatch, ["gpt-6.2-sol", "gpt-6.1-sol", "gpt-5.6-sol"], "openai")
    for k in range(len(_shapes("x", "openai"))):
        sol, astra, luna, terra = (_shapes("gpt-6.2-" + c, "openai")[k] for c in ("sol", "astra", "luna", "terra"))
        _fleet(monkeypatch, [sol, astra, luna, terra])
        s, a, l, t = (chat("x", i) for i in (sol, astra, luna, terra))
        assert s == pytest.approx(a), "sol and astra are one flagship line within a release"
        assert s - t == pytest.approx(R.GPT_TERRA_UNDER_FLAGSHIP) and s - l == pytest.approx(R.GPT_LUNA_UNDER_FLAGSHIP)
        assert s > t > l


def test_an_unknown_codename_is_ordered_by_version_only_no_score_is_invented(monkeypatch):
    _fleet(monkeypatch, ["gpt-6.2", "gpt-6.2-vega", "gpt-6.1-vega", "gpt-6.1"])
    assert R.parse("gpt-6.2-vega").tier == ""
    assert chat("x", "gpt-6.2-vega") == pytest.approx(chat("x", "gpt-6.2"))
    assert chat("x", "gpt-6.1-vega") == pytest.approx(chat("x", "gpt-6.1"))
    assert chat("x", "gpt-6.2-vega") > chat("x", "gpt-6.1-vega")


def test_nothing_exceeds_its_owner_ceiling(monkeypatch):
    ids = [c for c in ("claude-opus-6", "claude-opus-7", "claude-sonnet-6", "claude-fable-6", "claude-haiku-6")]
    ids += ["gpt-6.2", "gpt-7", "gpt-9", "gpt-7-sol", "gpt-7-astra"]
    _fleet(monkeypatch, ids)
    for m in ids[:5]:
        assert chat("anthropic", m) <= A._PREF_FLOORS[5] + 1e-9, m
    for m in ids[5:]:
        assert chat("openai", m) <= A._PREF_FLOORS[5] - 0.5 + 1e-9, m
    # the new board rows cannot lift a Claude above its floor either; the newest Opus keeps it
    assert chat("anthropic", "claude-opus-7") == pytest.approx(A._PREF_FLOORS[5])


def test_gemini_glm_kimi_deepseek_qwen_newer_above_older_in_every_shape(monkeypatch):
    _strictly_decreasing(monkeypatch, ["gemini-3.9-pro", "gemini-3.9-flash", "gemini-3.8-flash"], "google")
    _strictly_decreasing(monkeypatch, ["glm-5.4", "glm-5.3"], "z-ai", pid="nvidia")
    _strictly_decreasing(monkeypatch, ["kimi-k4", "kimi-k3.1-pro", "kimi-k3"], "moonshotai", pid="nvidia")
    _strictly_decreasing(monkeypatch, ["deepseek-v4.2-flash", "deepseek-v4.1-flash"], "deepseek-ai",
                         pid="nvidia")
    _strictly_decreasing(monkeypatch, ["qwen3.9-27b", "qwen3.8-27b"], "qwen", pid="groq")


def test_future_models_also_rank_themselves_on_tool_turns(monkeypatch):
    ids = ["glm-5.4", "glm-5.3", "kimi-k4", "kimi-k3.1-pro", "kimi-k3", "deepseek-v4.2-flash",
           "deepseek-v4.1-flash", "gemini-3.9-pro", "gemini-3.9-flash", "gemini-3.8-flash"]
    _fleet(monkeypatch, ids)
    for new, old in (("glm-5.4", "glm-5.3"), ("kimi-k4", "kimi-k3.1-pro"), ("kimi-k3.1-pro", "kimi-k3"),
                     ("deepseek-v4.2-flash", "deepseek-v4.1-flash"), ("gemini-3.9-pro", "gemini-3.9-flash"),
                     ("gemini-3.9-flash", "gemini-3.8-flash")):
        assert tool("nvidia", new) >= tool("nvidia", old) - 1e-9, (new, old)
    assert tool("nvidia", "glm-5.4") > tool("nvidia", "glm-5.3")
    assert tool("nvidia", "kimi-k3.1-pro") > tool("nvidia", GLM53)    # TB4.0 67.2 vs 42


def test_nemotron_newer_above_older_at_equal_evidence(monkeypatch):
    ids = ["nvidia/nemotron-4-ultra-550b-a55b", "nvidia/nemotron-3.6-ultra-550b-a55b",
           "nvidia/nemotron-3.5-ultra-550b-a55b", "nvidia/nemotron-3-ultra-550b-a55b"]
    _fleet(monkeypatch, ids)
    s = [chat("nvidia", i) for i in ids]
    assert s[0] > s[1] > s[2] > s[3], s
    # a size is not a version: the Llama-based 70B keeps its plain score
    assert A._nemotron_version_bump("llama-3.1-nemotron-70b-instruct") == 0.0
    assert A._nemotron_version_bump("nvidia/nemotron-nano-3-30b-a3b") == 0.0


def test_aa_finds_a_listed_model_behind_a_relay_prefix_or_a_moe_size_tail(monkeypatch):
    monkeypatch.setattr(A, "_aa_scores", {"nemotron35supervl": 76.1, "qwen38": 99.0})
    for mid in ("nvidia/nemotron-3.5-super-vl:free", "nvidia/nemotron-3.5-super-vl-120b-a12b",
                "srv_ab12cd34ef56:nvidia/nemotron-3.5-super-vl-120b-a12b",
                "pa:1a2b3c4d:nvidia/nemotron-3.5-super-vl:free"):
        assert A._aa_score_for(mid) == 76.1, mid
    # exact keys only: a dense size must never read as the flagship
    assert A._aa_score_for("qwen/qwen3.8-14b") is None
    assert A._aa_score_for("something-else") is None



# =========================================================================== #
# 9. 2026-10-24: GPT-6.x, Fable, Haiku and Gemini Pro rows (chart data, not prose)
# =========================================================================== #
# id -> (Terminal-Bench 4.0 %, AutomationBench-AA %, AA Index (integer), LMArena).
# Read from artificialanalysis.ai's embedded chart data (the visible prose names only the
# top three); the Max / headline variant of each, as Opus and Sonnet 5.5 do.
NEW_ROWS = {
    "gpt-6-astra": (59.1, 68.5, 53, 1475.5),
    "gpt-6.1-sol": (56.1, 64.9, 52, 1479.4),
    "gpt-6-luna": (12.6, 53.2, 38, 1441.0),
    "gpt-5.6-terra": (35.4, None, 42, 1467.1),
    "claude-fable-5.1": (52.0, 59.4, 53, 1502.6),
    "claude-haiku-5.5": (32.8, 35.4, 43, 1424.6),
    "gemini-3.1-pro": (4.0, None, 30, 1486.8),
}


@pytest.mark.parametrize("mid", sorted(NEW_ROWS))
def test_each_new_row_carries_its_exact_figures_date_and_source(mid):
    tb, auto, aa, arena = NEW_ROWS[mid]
    ev = B.agentic_evidence(mid)
    gen = B.general_evidence(mid)
    assert (ev["tb4"], ev["automation"]) == (tb, auto)
    assert (gen["aa"], gen["arena"]) == (aa, arena)
    assert ev["date"] == "2026-10-24" and "inherited_from" not in ev and "inherited_fields" not in ev
    assert "artificialanalysis.ai/evaluations/terminalbench-4-0" in ev["source"]
    assert "embedded chart data" in ev["source"]
    if auto is None:
        assert "No AutomationBench-AA entry" in ev["source"]
    else:
        assert "artificialanalysis.ai/evaluations/automationbench-aa" in ev["source"]


def test_the_headline_variant_is_used_never_the_best_scoring_effort_level():
    # Astra Xhigh reads TB4.0 59.6 and Fable 5.1 Xhigh 55.1; the Max entries the chart
    # selects by default read 59.1 and 52.0, and those are the rows
    assert B.agentic_evidence("gpt-6-astra")["tb4"] == 59.1
    assert B.agentic_evidence("claude-fable-5.1")["tb4"] == 52.0
    for mid in ("gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna", "gpt-5.6-terra", "claude-haiku-5.5"):
        assert "(Max)" in B.agentic_evidence(mid)["source"], mid
    assert "(Max, Default Fallback)" in B.agentic_evidence("claude-fable-5.1")["source"]
    # Haiku 5.5 has no Max entry on LMArena: its High entry is labelled as such, with
    # the (later) board it was pulled from
    src = B.general_evidence("claude-haiku-5.5")["source"]
    assert "Claude Haiku 5.5 (High)" in src and "2026-10-22" in src


def test_gpt_6_sol_never_borrows_astras_numbers():
    # gpt-6-astra and gpt-6-sol parse to the same (gpt, '', 6.0); the page lists no
    # figures for Sol 6.0, so it must not be answered with Astra's
    assert R.parse("gpt-6-sol") == R.parse("gpt-6-astra")
    for mid in ("gpt-6-sol", "openai/gpt-6-sol", "srv_ab12cd34ef56:openai/gpt-6-sol"):
        assert B.agentic_evidence(mid) is None and B.general_evidence(mid) is None, mid
        assert A._ev_agentic_evidence_view(mid) is None and A._ev_general_evidence_view(mid) is None
        assert A._ev_agentic_bonus(mid) == 0.0
    assert A._ev_agentic_evidence_view("gpt-6-astra")["tb4"] == 59.1
    # the same line from a newer release, or no codename at all, still reads the line's row
    for mid in ("gpt-6.2-sol", "gpt-6.2-astra", "gpt-6.1"):
        ev = B.agentic_evidence(mid)
        assert ev["tb4"] == 56.1, mid
    assert B.agentic_evidence("gpt-6.2-sol")["inherited_from"] == "gpt-6.1-sol"
    # the boards' own order (Arena: Sol 6.0 1458.8 under Astra 1475.5) now shows on tool turns
    assert tool("anthropic", "gpt-6-astra") - tool("anthropic", "gpt-6-sol") > 5
    assert chat("anthropic", "gpt-6-astra") == pytest.approx(chat("anthropic", "gpt-6-sol"))


def test_gpt_6_terra_inherits_the_5_6_terra_row_it_does_not_read_a_figure():
    ev = B.agentic_evidence("gpt-6-terra")
    assert ev["inherited_from"] == "gpt-5.6-terra" and ev["tb4"] == 35.4 and ev["floor_bump"] > 0
    assert A._ev_agentic_evidence_view("gpt-6-terra")["inherited_from"] == "gpt-5.6-terra"
    assert A._ev_agentic_evidence_view("gpt-5.6-terra").get("inherited_from") is None
    # ...and still sits under the flagship line (terra is the middle tier)
    assert tool("anthropic", "gpt-6-terra") < tool("anthropic", "gpt-6-astra")


def test_unlisted_newer_versions_inherit_and_rank_above_their_predecessors(monkeypatch):
    pairs = (("claude-fable-5.2", "claude-fable-5.1"), ("claude-haiku-5.6", "claude-haiku-5.5"),
             ("gpt-6.2-astra", "gpt-6.1-sol"), ("gpt-6.1-luna", "gpt-6-luna"),
             ("gpt-5.7-terra", "gpt-5.6-terra"))
    _fleet(monkeypatch, [i for p in pairs for i in p] + ["claude-opus-5.5", "gpt-6.1-sol"])
    for new, old in pairs:
        ev = B.agentic_evidence(new)
        row = B.agentic_evidence(old)
        assert ev["inherited_from"] in (old, "gpt-6.1-sol"), new
        assert ev["floor_bump"] > 0, new
        assert B.agentic_delta(ev) == pytest.approx(B.agentic_delta(row)), new
        assert tool("anthropic", new) >= tool("anthropic", old) - 1e-9, (new, old)
        assert chat("anthropic", new) >= chat("anthropic", old) - 1e-9, (new, old)


def test_pro_is_never_scored_under_its_flash_cut_and_3_1_pro_keeps_its_own_row():
    own = B.agentic_evidence("gemini-3.1-pro")
    assert own["tb4"] == 4.0 and "inherited_from" not in own
    assert B.general_evidence("gemini-3.1-pro")["aa"] == 30
    flash = B.agentic_evidence("gemini-3.8-flash")
    for pro in ("gemini-3.8-pro", "models/gemini-3.9-pro", "gemini-4-pro"):
        ev = B.agentic_evidence(pro)
        assert ev["inherited_from"] == "gemini-3.8-flash", pro
        assert ev["tb4"] == flash["tb4"] == 20 and B.general_evidence(pro)["aa"] == 41, pro
        assert A._ev_agentic_bonus(pro) >= A._ev_agentic_bonus("gemini-3.1-pro") + 2, pro
    # scores of the later pros are the ones they had before the 3.1 Pro row existed
    assert A._ev_agentic_bonus("gemini-3.8-pro") == pytest.approx(2.6)
    assert A._ev_agentic_bonus("gemini-3.1-pro") == pytest.approx(0.4)


def test_a_stronger_pro_line_wins_over_the_flash_line_for_a_newer_pro():
    real = B.TABLE
    rows = dict(real)
    rows["gemini-3.9-pro"] = {"tb4": 70.0, "automation": 80.0, "aa": 55, "arena": 1520.0,
                              "date": "2026-11-01", "source": "synthetic"}
    B.TABLE = rows
    B.reset()
    try:
        exact = B.agentic_evidence("gemini-3.9-pro")
        assert exact["tb4"] == 70.0 and "inherited_from" not in exact      # an exact pro row is kept
        newer = B.agentic_evidence("gemini-4-pro")
        assert newer["inherited_from"] == "gemini-3.9-pro" and newer["tb4"] == 70.0
    finally:
        B.TABLE = real
        B.reset()


def test_the_new_rows_never_lift_a_small_tier_in_chat(monkeypatch):
    # the board floor is a max(): it cannot lift a model whose owner floor already sits
    # above it. Checked with and without the flagship that anchors the small tier.
    fleets = (["gpt-6-luna"], ["gpt-6-luna", "gpt-5.4"], ["claude-haiku-5.5"],
              ["claude-haiku-5.5", "claude-fable-5.1"], ["gpt-5.6-terra"], ["gpt-5.6-terra", "gpt-6-luna"],
              ["gemini-3.1-pro"], ["gemini-3.1-pro", "gemini-3.8-flash"], ["gemini-3.1-pro", "gemini-3.8-pro"],
              ["claude-fable-5.1"], ["gpt-6-astra"], ["gpt-6.1-sol"], ["gpt-6.1-sol", "gpt-6-luna", "claude-haiku-5.5"])
    real = B.TABLE
    without = {k: v for k, v in real.items() if k not in NEW_ROWS}
    try:
        for ids in fleets:
            got = {}
            for label, table in (("with", real), ("without", without)):
                B.TABLE = table
                B.reset()
                _fleet(monkeypatch, ids)
                got[label] = {(m, pid): chat(pid, m) for m in ids for pid in ("anthropic", RELAY)}
            assert got["with"] == pytest.approx(got["without"]), ids
    finally:
        B.TABLE = real
        B.reset()


def test_tool_turn_order_with_the_new_rows():
    t = {m: tool("anthropic", m) for m in (
        "claude-opus-5.5", "claude-sonnet-5.5", "claude-fable-5.1", "claude-haiku-5.5", "gpt-6-astra",
        "gpt-6.1-sol", "gpt-6-luna", "gemini-4-argon", "gemini-3.8-flash", "moonshotai/kimi-k3.1-pro")}
    assert t["claude-opus-5.5"] > t["claude-sonnet-5.5"]                   # Opus stays over Sonnet
    assert t["claude-opus-5.5"] - t["claude-sonnet-5.5"] == pytest.approx(0.177, abs=0.01)
    assert t["claude-opus-5.5"] > max(t["gpt-6-astra"], t["gpt-6.1-sol"], t["claude-fable-5.1"])
    # Fable has its own Terminal-Bench row now: inside the auto band of Opus instead of ~6 behind
    assert t["claude-opus-5.5"] - t["claude-fable-5.1"] < A._AUTO_TOP_BAND
    # the small tiers sit far under their flagships (Haiku under Sonnet, Luna under Astra)
    assert t["claude-sonnet-5.5"] - t["claude-haiku-5.5"] > 8
    assert t["gpt-6-astra"] - t["gpt-6-luna"] > 8
    # Argon's own TB4.0 (57.1, not gemini-3.8-flash's 20) puts it between Kimi K3.1 Pro and GLM 5.3
    assert t["moonshotai/kimi-k3.1-pro"] > t["gemini-4-argon"] > tool(GLM53_PID, GLM53)
    assert t["gemini-4-argon"] > t["gemini-3.8-flash"] + 3


def test_relay_copies_of_the_new_rows_still_get_no_board_bonus():
    for mid in ("gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna", "claude-fable-5.1", "claude-haiku-5.5"):
        assert tool(RELAY, mid) == pytest.approx(chat(RELAY, mid)), mid
    seed_healthy(RELAY, "gpt-6-astra")
    assert A._relay_proven(RELAY, "gpt-6-astra")
    assert tool(RELAY, "gpt-6-astra") == pytest.approx(tool("anthropic", "gpt-6-astra"))


def test_nemotron_figures_are_recorded_not_wired():
    # modelrank knows no nemotron family, so a benchmarks row would be ignored -- and
    # a parsed one would lift a 26-point model through the 133.0 board floor
    for mid in ("nvidia/nemotron-3.5-super-vl-120b-a12b", "nemotron-3-ultra-550b-a55b", "nemotron-3.5-lightning"):
        assert R.parse(mid) is None
        assert B.agentic_evidence(mid) is None and B.general_evidence(mid) is None
        assert chat("nvidia", mid) < 100
    assert not [m for m, _ in B.rows() if "nemotron" in m]
