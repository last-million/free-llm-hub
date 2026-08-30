"""Artificial Analysis scores without an AA API key.

Until 2026-08-30 the AA integration was dead weight on every install that had
no key -- which was this one: aa_scores.json had never been written, no [aa]
line had ever been logged, and 100% of ranking came from a hand-typed tier
table with preference floors nobody re-dates.

OpenRouter's public catalog carries AA's own per-model numbers under
benchmarks.artificial_analysis and needs no key, no account and no card
(verified 2026-08-30: HTTP 200 unauthenticated, 396 models, 151 AA-scored).
Same data, same freshness, so the existing 6-hourly refresh now does real work.

Coverage is partial by design: the mainstream ids match, the private provider
aliases (morph-kimik3, llama-3.3-70b-versatile) cannot and keep their tier
score. Measurement where it exists, an honest guess elsewhere.
"""
from unittest import mock

import app


def _catalog(rows):
    r = mock.Mock(status_code=200)
    r.json.return_value = {"data": rows}
    return r


def test_scores_are_harvested_from_the_public_catalog():
    rows = [
        {"id": "moonshotai/kimi-k3", "canonical_slug": "moonshotai/kimi-k3-20260715",
         "benchmarks": {"artificial_analysis": {"intelligence_index": 71.0}}},
        {"id": "qwen/qwen3.8-27b", "canonical_slug": "qwen/qwen3.8-27b-20260814",
         "benchmarks": {"artificial_analysis": {"intelligence_index": 52.0}}},
    ]
    with mock.patch.object(app.requests, "get", return_value=_catalog(rows)):
        raw = app._fetch_aa_scores_keyless()
    assert raw[app._normalize_aa_slug("moonshotai/kimi-k3")] == 71.0
    assert raw[app._normalize_aa_slug("qwen/qwen3.8-27b")] == 52.0


def test_the_plain_id_is_keyed_not_only_the_dated_canonical_slug():
    """REGRESSION. Keying on canonical_slug alone matched almost nothing: it
    carries a release DATE ('moonshotai/kimi-k3-20260715' -> 'kimik320260715')
    which no hub id can ever equal. kimi-k3, qwen3.8 and glm-5.2 all silently
    missed until the plain id was keyed too."""
    rows = [{"id": "moonshotai/kimi-k3",
             "canonical_slug": "moonshotai/kimi-k3-20260715",
             "benchmarks": {"artificial_analysis": {"intelligence_index": 71.0}}}]
    with mock.patch.object(app.requests, "get", return_value=_catalog(rows)):
        raw = app._fetch_aa_scores_keyless()
    # A hub id carrying no date must resolve.
    assert app._normalize_aa_slug("moonshotai/kimi-k3") in raw
    # ...and the dated form is still registered, costing nothing.
    assert app._normalize_aa_slug("moonshotai/kimi-k3-20260715") in raw


def test_rows_without_an_aa_score_are_skipped():
    rows = [
        {"id": "a/no-bench"},
        {"id": "b/empty-bench", "benchmarks": {}},
        {"id": "c/other-bench", "benchmarks": {"design_arena": [{"elo": 1}]}},
        {"id": "d/null-index",
         "benchmarks": {"artificial_analysis": {"intelligence_index": None}}},
        {"id": "e/scored",
         "benchmarks": {"artificial_analysis": {"intelligence_index": 40.0}}},
    ]
    with mock.patch.object(app.requests, "get", return_value=_catalog(rows)):
        raw = app._fetch_aa_scores_keyless()
    assert list(raw) == [app._normalize_aa_slug("e/scored")]


def test_any_failure_is_fail_open():
    """A ranking source going down must never take routing with it -- callers
    fall back to the static tier table, exactly as before this existed."""
    bad = mock.Mock(status_code=503)
    with mock.patch.object(app.requests, "get", return_value=bad):
        assert app._fetch_aa_scores_keyless() == {}
    with mock.patch.object(app.requests, "get",
                           side_effect=app.requests.RequestException("boom")):
        assert app._fetch_aa_scores_keyless() == {}
    malformed = mock.Mock(status_code=200)
    malformed.json.side_effect = ValueError("not json")
    with mock.patch.object(app.requests, "get", return_value=malformed):
        assert app._fetch_aa_scores_keyless() == {}


def test_the_keyless_path_runs_only_when_there_is_no_key():
    rows = [{"id": "x/y",
             "benchmarks": {"artificial_analysis": {"intelligence_index": 50.0}}}]
    with mock.patch.object(app.config, "get_aa_api_key", return_value=None), \
            mock.patch.object(app, "_fetch_aa_scores_keyless",
                              return_value={"xy": 50.0}) as keyless, \
            mock.patch.object(app, "_calibrate_aa_scores", side_effect=lambda r: r):
        assert app._fetch_aa_scores() == {"xy": 50.0}
    assert keyless.called
    # With a key present the keyless path must NOT be used.
    with mock.patch.object(app.config, "get_aa_api_key", return_value="k"), \
            mock.patch.object(app, "_fetch_aa_scores_keyless") as keyless2, \
            mock.patch.object(app.requests, "get", return_value=_catalog(rows)):
        app._fetch_aa_scores()
    assert not keyless2.called


def test_an_empty_harvest_does_not_pretend_to_have_data():
    with mock.patch.object(app.config, "get_aa_api_key", return_value=None), \
            mock.patch.object(app, "_fetch_aa_scores_keyless", return_value={}):
        assert app._fetch_aa_scores() == {}
