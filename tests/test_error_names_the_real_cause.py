"""An exhausted chain says WHY, when the reason is the user's own setting.

REPORTED 2026-09-05: "why now he say all providers failed". It was not the
providers. A category button in Settings had switched most of the catalog off --
396 ids blocked, 69 of 325 models left usable -- and the 503 read

    All providers failed: none available

which points at the upstreams, and never mentions the one cause that actually
produced it and that only the user can undo.

An error naming a cause the reader cannot act on, while hiding the cause they
can, is worse than a short one.
"""
from unittest import mock

import app as A


def test_nothing_is_added_when_nothing_is_switched_off():
    """The common case must stay quiet -- a hint on every 503 is noise."""
    with mock.patch.object(A, "_blocked_models", return_value=set()):
        assert A._no_candidates_hint() == ""


def test_it_says_how_many_are_switched_off():
    with mock.patch.object(A, "_blocked_models", return_value={"a/b", "c/d"}):
        hint = A._no_candidates_hint()
    assert "2 model(s)" in hint and "Settings" in hint


def test_it_warns_that_a_category_replaces_the_selection():
    """The specific way this happens: the buttons are a filter, not an 'add'."""
    with mock.patch.object(A, "_blocked_models", return_value={"a/b"}):
        assert "replace" in A._no_candidates_hint()


def test_a_broken_block_list_does_not_break_the_error_path():
    """This runs while ALREADY reporting a failure; it must not raise on top."""
    with mock.patch.object(A, "_blocked_models", side_effect=RuntimeError("boom")):
        assert A._no_candidates_hint() == ""


def test_the_hint_reaches_the_actual_503s():
    """Three routes build that message; a hint only one of them uses is a hint
    the user meets by luck. They now share ONE builder, which carries it."""
    src = open("app.py", encoding="utf-8").read()
    # Each route passes the request size and tool flag so the hint can tell
    # whether an off-list model could actually have served (2026-10-07).
    assert src.count("_chain_exhausted_text(errors, last_hard, est, has_tools)") >= 3
    with mock.patch.object(A, "_blocked_models", return_value={"a/b"}):
        assert "Settings" in A._chain_exhausted_text(["p: HTTP 404"])


def test_the_hint_still_blames_the_off_list_when_it_could_have_served():
    """A big request whose off-list holds a model big enough: the note stays."""
    with mock.patch.object(A, "_blocked_models", return_value={"nv/big"}), \
            mock.patch.object(A, "_model_ctx_info", return_value=(262144, "learned")), \
            mock.patch.object(A, "_supports_tools", return_value=True), \
            mock.patch.object(A, "_is_low_quality", return_value=False):
        text = A._chain_exhausted_text(["p: HTTP 404"], None, 79000, True)
    assert "1 model(s) are switched OFF in Settings" in text


def test_the_hint_names_the_real_constraint_when_the_off_list_could_not_have():
    """MEASURED 2026-10-07: a 79K turn's 503 blamed 35 switched-off models that
    were all too small to hold it. Then the note states the size instead."""
    with mock.patch.object(A, "_blocked_models", return_value={"groq/small"}), \
            mock.patch.object(A, "_model_ctx_info", return_value=(8000, "learned")), \
            mock.patch.object(A, "_supports_tools", return_value=True), \
            mock.patch.object(A, "_is_low_quality", return_value=False):
        text = A._chain_exhausted_text(["p: HTTP 404"], None, 79000, True)
    assert "switched OFF" not in text
    assert "~79000 tokens" in text
