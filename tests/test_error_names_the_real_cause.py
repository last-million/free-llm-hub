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
    the user meets by luck."""
    src = open("app.py", encoding="utf-8").read()
    assert src.count("_no_candidates_hint()") >= 3
