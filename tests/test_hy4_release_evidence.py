"""Hy4 (released 2026-11-04) ranks by its own numbers on tool turns, not just by a floor.

ASKED 2026-11-09: "new model hy4 is perfect and surpasses all Chinese models in many
benchmarks; if it is available he should have a higher benchmark, from this
documentation (https://hy.tencent.ai/research/hy4)".

WHAT WAS MEASURED (sandbox, nothing live touched) BEFORE this change:
    chat  : hy4 138.00 (the owner's floor for hy4-and-up) -- already above GLM 5.3
            (134.03) and DeepSeek V4.1 Flash (134.05), level with Claude Opus 5.5.
    tool  : hy4 138.00 -- BELOW GLM 5.3 (138.85) and DeepSeek V4.1 Flash (141.07),
            because it had no board row and so got no agentic bonus.
    hy4-preview scored exactly like the release (same floor, same parse).

WHAT THE RELEASE POST SAYS (Terminal-Bench 4.0 column, Tencent's own harness): Hy4
44.4, Hy4 preview 26.3, GLM-5.3 42.9, DeepSeek-V4.1-Flash 42.3, Kimi K3.1 Pro 65.7,
GPT-6.1 Sol 55.9, Claude Opus 5.5 66.4. So it beats GLM 5.3 and matches DeepSeek Flash,
but is NOT ahead of Kimi K3.1 Pro on the public benchmarks (nor of Opus / Sol on
Terminal-Bench 4.0) -- the same table has Kimi well ahead on TB4.0, SWE-Marathon, HLE
and AutomationBench -- and most of its other wins are on Tencent's own "Hy-*" internal
benchmarks. The figures are VENDOR-REPORTED: Artificial Analysis does not list Hy4 yet.

So the row carries Terminal-Bench 4.0 only (AutomationBench is another scale there, and
there is no AA Index / LMArena figure), chat is left exactly where the owner put it, and
the order on tool turns becomes  Kimi K3.1 Pro > hy4 > DeepSeek Flash > GLM 5.3.
"""
import benchmarks
import app


def chat(pid, mid):
    return app._benchmark_score(pid, mid)


def tool(pid, mid):
    return app._agentic_score((chat(pid, mid), pid, mid))


# ---------------------------------------------------------------- the row itself
def test_the_row_is_vendor_reported_and_says_so():
    ev = benchmarks.agentic_evidence("tencent/hy4")
    assert ev is not None
    assert ev["tb4"] == 44.4
    assert ev["date"] == "2026-11-09"
    src = ev["source"].lower()
    assert "vendor-reported" in src
    assert "hy.tencent.ai/research/hy4" in src
    assert "artificial analysis" in src          # says it is NOT on that board yet


def test_nothing_is_invented_beyond_terminal_bench():
    """AutomationBench in the post is a different scale from AutomationBench-AA, and the
    post carries no AA Index or LMArena figure: those stay None, never guessed."""
    ev = benchmarks.agentic_evidence("tencent/hy4")
    assert ev["automation"] is None
    assert ev["aa"] is None and ev["arena"] is None
    assert benchmarks.general_evidence("tencent/hy4") is None


def test_every_spelling_reads_the_row():
    for mid in ("tencent/hy4", "hy4", "hunyuan-4", "tencent/hunyuan-4",
                "tencent/hy4:free", "tencent/hy4-free", "Tencent/HY4",
                "srv_msjekdik2f3768a4ee42:tencent/hy4:free",
                # the flagship's own size, with or without the active-parameter tail
                "tencent/hy4-770b", "tencent/hy4-770b-a49b"):
        ev = benchmarks.agentic_evidence(mid)
        assert ev is not None and ev["tb4"] == 44.4, mid


def test_a_small_size_variant_does_not_inherit_the_flagship_row():
    """The reason size is part of the key: an 8B cut is not the 770B flagship."""
    assert benchmarks.agentic_evidence("tencent/hy-4-lite-8b") is None
    assert benchmarks.agentic_evidence("tencent/hy4-mini-14b") is None


def test_unrelated_ids_are_untouched():
    for mid in ("acme/hypernova-3", "acme/alchemy-7b", "openai/gpt-oss-120b"):
        assert benchmarks.agentic_evidence(mid) is None, mid


# ---------------------------------------------------------------- the order
def test_on_tool_turns_it_is_above_glm_and_deepseek_flash():
    h = tool("kilocode", "tencent/hy4")
    assert h > tool("nvidia", "z-ai/glm-5.3")
    assert h > tool("nvidia", "deepseek-v4.1-flash")
    assert h > tool("nvidia", "z-ai/glm-5.3-flash")


def test_it_is_not_above_kimi_k31_pro():
    """The owner's standing directive (Kimi K3.1 is the #1) and the same table: Kimi
    K3.1 Pro 65.7 vs 44.4 on Terminal-Bench 4.0. 'Surpasses all Chinese models' is
    not what the numbers say."""
    k = "moonshotai/kimi-k3.1-pro"
    assert tool("nvidia", k) > tool("kilocode", "tencent/hy4")
    assert chat("nvidia", k) > chat("kilocode", "tencent/hy4")


def test_chat_is_left_exactly_where_the_owner_put_it():
    assert chat("kilocode", "tencent/hy4") == app._PREF_FLOORS[5]
    assert chat("kilocode", "tencent/hy3:free") == app._PREF_FLOORS[0]


def test_the_agentic_bonus_is_the_documented_one():
    bonus = benchmarks.agentic_delta(benchmarks.agentic_evidence("tencent/hy4"))
    assert abs(bonus - benchmarks.TB_WEIGHT * 0.444) < 1e-9
    assert abs(tool("kilocode", "tencent/hy4") - (chat("kilocode", "tencent/hy4") + bonus)) < 1e-6


def test_board_evidence_lets_it_lead_a_tool_turn():
    assert app._ev_has_agentic_evidence("tencent/hy4")


# ---------------------------------------------------------------- the preview
def test_the_preview_never_reads_the_releases_row():
    """Tencent's own table: preview 26.3 vs release 44.4. Both parse to (hy, '', (4, 0)),
    so without the guard the preview would be credited with the release's figure."""
    for mid in ("tencent/hy4-preview", "tencent/Hy4-preview", "hy4-preview",
                "tencent/hy4-preview:free", "tencent/hy4-770b-preview"):
        assert benchmarks.agentic_evidence(mid) is None, mid
        assert not app._ev_has_agentic_evidence(mid), mid


def test_the_preview_keeps_its_floor_but_not_the_releases_points():
    p = tool("kilocode", "tencent/hy4-preview")
    assert chat("kilocode", "tencent/hy4-preview") == app._PREF_FLOORS[5]   # unchanged
    assert p == chat("kilocode", "tencent/hy4-preview")                       # no bonus
    assert tool("kilocode", "tencent/hy4") > p


# ---------------------------------------------------------------- newer / older
def test_a_newer_generation_inherits_the_row_and_ranks_above():
    ev = benchmarks.agentic_evidence("tencent/hy5")
    assert ev is not None and ev["inherited_from"] == "tencent/hy4"
    assert ev["tb4"] == 44.4 and 0 < ev["floor_bump"] <= 0.3
    assert tool("kilocode", "tencent/hy5") > tool("kilocode", "tencent/hy4")


def test_hy3_is_left_exactly_where_it_was():
    assert benchmarks.agentic_evidence("tencent/hy3:free") is None
    assert tool("kilocode", "tencent/hy3:free") == app._PREF_FLOORS[0]


def test_older_hunyuan_is_not_promoted():
    assert benchmarks.agentic_evidence("tencent/hy2") is None
    assert benchmarks.agentic_evidence("hunyuan-1") is None


# ---------------------------------------------------------------- nothing else moved
def test_other_rows_are_unchanged():
    """The row and the two guards only add; every row the owner ranked before keeps its
    figures (a size spelling must not have moved any non-hy family)."""
    assert benchmarks.agentic_evidence("z-ai/glm-5.3")["tb4"] == 42
    assert benchmarks.agentic_evidence("moonshotai/kimi-k3.1-pro")["tb4"] == 67.2
    assert benchmarks.agentic_evidence("qwen3.8-27b")["tb4"] == 6
    # a 27B qwen is still a SIZE variant: it never reads a row of another size
    assert benchmarks.agentic_evidence("qwen3.8-235b") is None
    assert benchmarks._size_key("qwen", "qwen3.8-27b") == 27
    assert benchmarks._size_key("hy", "tencent/hy4-770b") is None
    assert benchmarks._size_key("hy", "tencent/hy-4-lite-8b") == 8
