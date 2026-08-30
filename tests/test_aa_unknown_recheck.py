"""A brand-new model triggers a benchmark re-check instead of ranking last.

MEASURED 2026-08-30: an id nothing recognises scores ~11.8 against a 358-model
pool -- dead last, so it is never routed to and never gets a chance to prove
itself. Discovery notices a new model within MODEL_CACHE_TTL (60s), but the
scores behind it only moved every AA_REFRESH_INTERVAL (6h), so a genuinely new
flagship could sit at the bottom of the chain for most of a day.

Now the discovery path itself asks the benchmark source when it meets something
unscoreable -- debounced, off-thread, and fail-open like everything else here.
"""
from unittest import mock

import app


def _reset():
    with app._aa_unknown_lock:
        app._aa_unknown_last_check[0] = 0.0


def test_a_recognised_model_triggers_nothing():
    """kimi-k3 has a real AA score; there is nothing to go and look up."""
    _reset()
    with mock.patch.object(app, "_aa_score_for", return_value=71.0), \
            mock.patch.object(app, "_aa_refresh_once") as refresh:
        assert app._maybe_recheck_aa_for_unknown(["moonshotai/kimi-k3"]) is False
    assert not refresh.called


def test_a_model_matching_a_known_family_triggers_nothing():
    """No AA row, but the static tier table recognises the family -- that is a
    real number already, not a shrug."""
    _reset()
    with mock.patch.object(app, "_aa_score_for", return_value=None), \
            mock.patch.object(app, "_static_benchmark_score", return_value=88.0), \
            mock.patch.object(app, "_aa_refresh_once") as refresh:
        assert app._maybe_recheck_aa_for_unknown(["vendor/known-family-7b"]) is False
    assert not refresh.called


def test_a_new_version_of_a_strong_family_triggers_nothing():
    """glm-6 / kimi-k4 inherit their family's strength heuristically."""
    _reset()
    with mock.patch.object(app, "_aa_score_for", return_value=None), \
            mock.patch.object(app, "_static_benchmark_score", return_value=None), \
            mock.patch.object(app, "_strong_new_version_score", return_value=120.0), \
            mock.patch.object(app, "_aa_refresh_once") as refresh:
        assert app._maybe_recheck_aa_for_unknown(["zai/glm-6"]) is False
    assert not refresh.called


def test_a_genuinely_unknown_model_triggers_a_recheck():
    """The headline behaviour."""
    _reset()
    done = []
    with mock.patch.object(app, "_aa_score_for", return_value=None), \
            mock.patch.object(app, "_static_benchmark_score", return_value=None), \
            mock.patch.object(app, "_strong_new_version_score", return_value=0), \
            mock.patch.object(app, "_aa_refresh_once", side_effect=lambda: done.append(1)):
        assert app._maybe_recheck_aa_for_unknown(["acme/brandnew-9000"]) is True
    for t in list(app.threading.enumerate()):
        if t is not app.threading.current_thread() and t.daemon and t.is_alive():
            t.join(timeout=2)
    assert done, "the refresh thread never ran"


def test_the_recheck_is_debounced():
    """One unrecognised id must not mean one HTTP call per discovery pass --
    provider_free_models runs constantly."""
    _reset()
    with mock.patch.object(app, "_aa_score_for", return_value=None), \
            mock.patch.object(app, "_static_benchmark_score", return_value=None), \
            mock.patch.object(app, "_strong_new_version_score", return_value=0), \
            mock.patch.object(app, "_aa_refresh_once"):
        assert app._maybe_recheck_aa_for_unknown(["acme/x"]) is True
        for _ in range(5):
            assert app._maybe_recheck_aa_for_unknown(["acme/x"]) is False


def test_an_empty_or_missing_list_is_safe():
    _reset()
    with mock.patch.object(app, "_aa_refresh_once") as refresh:
        assert app._maybe_recheck_aa_for_unknown([]) is False
        assert app._maybe_recheck_aa_for_unknown(None) is False
    assert not refresh.called


def test_unscoreable_is_the_and_of_all_three_sources():
    with mock.patch.object(app, "_aa_score_for", return_value=None), \
            mock.patch.object(app, "_static_benchmark_score", return_value=None), \
            mock.patch.object(app, "_strong_new_version_score", return_value=0):
        assert app._model_is_unscoreable("acme/nope") is True
    with mock.patch.object(app, "_aa_score_for", return_value=50.0):
        assert app._model_is_unscoreable("acme/nope") is False
