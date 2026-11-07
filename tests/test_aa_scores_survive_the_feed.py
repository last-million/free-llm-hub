"""A benchmark score the feed drops is kept, and the live agentic index has its own scale.

MEASURED 2026-11-07 (hub.log + OpenRouter's public catalog): on 2026-10-28 the feed went
from 421 scores to 74 in one refresh (129 now), and each refresh REPLACED the table, so
347 scores vanished and those models fell back to a family guess. The same catalog now
carries `agentic_index` for 135 entries, which nothing read: `parse_openrouter_row`, the
hook documented as picking such a field up "with no code change", was never called, and
it mapped the index to Terminal-Bench 4.0, a different scale (Qwen 3.8 27B: 45.8 vs 6).

Hermetic: no network, the cache file is a temp file.
"""
import json

import pytest

import app as A
import benchmarks
import config

# conftest stands this aside for every test (no test may refetch): these test it.
REFRESH = A._aa_refresh_once
NOW = 1_800_000_000.0
DAY = 86400.0


@pytest.fixture
def table(monkeypatch, tmp_path):
    monkeypatch.setattr(A, "AA_SCORE_CACHE_PATH", str(tmp_path / "aa_scores.json"))
    monkeypatch.setattr(A, "_aa_scores", {})
    monkeypatch.setattr(A, "_aa_seen", {})
    monkeypatch.setattr(A, "_aa_agentic", {})
    monkeypatch.setattr(A, "_aa_agentic_fresh", [{}])
    monkeypatch.setattr(A, "_aa_last_refresh", 0.0)
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(tmp_path / "state" / "config.json"))
    return tmp_path


def _refresh(monkeypatch, scores, agentic=None):
    monkeypatch.setattr(A, "_fetch_aa_scores", lambda: dict(scores))
    A._aa_agentic_fresh[0] = dict(agentic or {})
    REFRESH()


def test_fresh_figures_win_and_dropped_ones_stay():
    merged, seen = A._aa_merge_kept({"a": 90.0}, {"a": 50.0, "b": 70.0}, {"b": NOW - DAY}, NOW, NOW)
    assert merged == {"a": 90.0, "b": 70.0}
    assert seen == {"a": NOW, "b": NOW - DAY}


def test_a_score_nobody_has_listed_for_too_long_goes():
    old = NOW - A.AA_KEEP_SECONDS - 1
    merged, _seen = A._aa_merge_kept({"a": 90.0}, {"b": 70.0}, {"b": old}, NOW, NOW)
    assert merged == {"a": 90.0}


def test_a_cache_from_before_this_change_dates_from_its_last_fetch():
    merged, seen = A._aa_merge_kept({"a": 1.0}, {"b": 2.0}, {}, NOW - DAY, NOW)
    assert merged["b"] == 2.0 and seen["b"] == NOW - DAY


def test_the_collapse_of_october_28_loses_nothing(table, monkeypatch):
    full = {"m%d" % i: float(i) for i in range(421)}
    _refresh(monkeypatch, full)
    _refresh(monkeypatch, {"m%d" % i: 60.0 for i in range(74)})
    assert len(A._aa_scores) == 421
    assert A._aa_scores["m10"] == 60.0 and A._aa_scores["m400"] == 400.0


def test_the_refresh_is_saved_with_when_each_was_seen(table, monkeypatch):
    _refresh(monkeypatch, {"a": 1.0}, {"a": 50.0})
    saved = json.load(open(A.AA_SCORE_CACHE_PATH, encoding="utf-8"))
    assert saved["scores"] == {"a": 1.0} and "a" in saved["seen"]
    assert saved["agentic"] == {"a": 50.0}


def test_flag_off_is_the_old_replace_and_the_old_file(table, monkeypatch):
    config.set_flag("aa_keep_scores", False)
    _refresh(monkeypatch, {"a": 1.0, "b": 2.0})
    _refresh(monkeypatch, {"a": 3.0})
    assert A._aa_scores == {"a": 3.0}
    assert set(json.load(open(A.AA_SCORE_CACHE_PATH, encoding="utf-8"))) == {"fetched_at", "scores"}


def test_a_failed_fetch_changes_nothing(table, monkeypatch):
    _refresh(monkeypatch, {"a": 1.0})
    monkeypatch.setattr(A, "_fetch_aa_scores", lambda: {})
    REFRESH()
    assert A._aa_scores == {"a": 1.0}


def test_an_empty_agentic_read_keeps_the_last_good_one(table, monkeypatch):
    _refresh(monkeypatch, {"a": 1.0}, {"a": 50.0})
    _refresh(monkeypatch, {"a": 1.0}, {})
    assert A._aa_agentic == {"a": 50.0}


# --------------------------------------------------------------------------- #
# The agentic index
# --------------------------------------------------------------------------- #

class _Catalog:
    status_code = 200

    def json(self):
        return {"data": [
            {"id": "minimax/minimax-m3", "benchmarks": {"artificial_analysis": {
                "intelligence_index": 18.9, "agentic_index": 29.5}}},
            {"id": "some/agentic-only", "benchmarks": {"artificial_analysis": {
                "agentic_index": 40.0}}},
        ]}


def test_the_refresh_reads_the_agentic_index(monkeypatch, table):
    monkeypatch.setattr(A.requests, "get", lambda *a, **k: _Catalog())
    monkeypatch.setattr(A, "_learn_thinking_from_row", lambda *a, **k: None)
    raw = A._fetch_aa_scores_keyless()
    assert A._normalize_aa_slug("minimax/minimax-m3") in raw
    fresh = A._aa_agentic_fresh[0]
    assert fresh[A._normalize_aa_slug("minimax/minimax-m3")] == 29.5
    assert fresh[A._normalize_aa_slug("some/agentic-only")] == 40.0


def test_a_model_with_no_board_row_earns_points_from_it(table, monkeypatch):
    monkeypatch.setattr(A, "_aa_agentic", {A._normalize_aa_slug("vendor/no-table-model"): 40.0})
    assert A._ev_agentic_bonus("vendor/no-table-model") == pytest.approx(2.0)


def test_the_dated_board_row_always_wins(table, monkeypatch):
    with_row = A._ev_agentic_bonus("z-ai/glm-5.3")
    monkeypatch.setattr(A, "_aa_agentic", {A._normalize_aa_slug("z-ai/glm-5.3"): 99.0})
    assert A._ev_agentic_bonus("z-ai/glm-5.3") == with_row


def test_it_is_half_the_weight_so_it_cannot_outrank_real_board_evidence():
    assert benchmarks.feed_delta(100.0) < benchmarks.agentic_delta({"tb4": 60.0})
    assert benchmarks.feed_delta(None) == 0.0
    assert benchmarks.feed_delta(53.1) == pytest.approx(2.655)


def test_it_never_grants_the_right_to_lead_a_tool_turn(table, monkeypatch):
    monkeypatch.setattr(A, "_aa_agentic", {A._normalize_aa_slug("vendor/no-table-model"): 90.0})
    assert A._ev_has_agentic_evidence("vendor/no-table-model") is False


def test_no_figure_means_no_change(table):
    assert A._ev_agentic_bonus("vendor/no-table-model") == 0.0


def test_the_flag_stops_it_being_read(table, monkeypatch):
    config.set_flag("aa_agentic_feed", False)
    _refresh(monkeypatch, {"a": 1.0}, {"a": 50.0})
    assert A._aa_agentic == {}


def test_the_row_parser_never_files_it_as_terminal_bench():
    got = benchmarks.parse_openrouter_row({"benchmarks": {"artificial_analysis": {
        "intelligence_index": 20.1, "agentic_index": 45.8}}})
    assert got == {"aa": 20.1, "agentic_index": 45.8}
    assert "tb4" not in got


def test_the_lookup_uses_the_table_it_is_given():
    assert A._aa_score_for("any/model", {}) is None
    key = A._normalize_aa_slug("any/model")
    assert A._aa_score_for("any/model", {key: 7.0}) == 7.0
