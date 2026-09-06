"""When a mode cannot be honoured, say so.

_apply_mode is fail-open BY DESIGN: a mode whose models are all busy, withdrawn
or rate-limited must degrade to answering rather than to refusing. That is right
and it stays. What was missing is any record that it happened.

MEASURED: a request sent with model "reasoning" was answered by
groq/qwen/qwen3.8-27b -- which is in `uncensored`, not `reasoning`. Nothing in
the response, the log or the activity row said the mode had not applied, so from
outside it was indistinguishable from a routing bug.

Reporting only. Nothing here may change which model answers.
"""
import pytest

import app as A


@pytest.fixture
def in_mode(monkeypatch):
    monkeypatch.setattr(A, "_active_mode", lambda: "coding")
    monkeypatch.setattr(A, "_mode_allows",
                        lambda mode, pid, m: m.endswith("-coder"))
    yield


def test_a_mode_that_applied_is_reported_as_such(in_mode):
    h = A._routing_headers("pa", "pa-coder", 1)
    assert h["X-Free-LLM-Hub-Mode"] == "coding"
    assert "X-Free-LLM-Hub-Mode-Applied" not in h


def test_a_mode_that_fell_open_is_flagged(in_mode):
    h = A._routing_headers("pa", "pa-chat", 1)
    assert h["X-Free-LLM-Hub-Mode"] == "coding"
    assert h["X-Free-LLM-Hub-Mode-Applied"] == "no"


def test_no_mode_means_no_extra_headers(monkeypatch):
    monkeypatch.setattr(A, "_active_mode", lambda: A.MODE_ALL)
    h = A._routing_headers("pa", "pa-chat", 1)
    assert "X-Free-LLM-Hub-Mode" not in h
    assert "X-Free-LLM-Hub-Mode-Applied" not in h


def test_the_existing_headers_are_untouched(in_mode):
    h = A._routing_headers("pa", "pa-coder", 3, last_error="timeout")
    assert h["X-Free-LLM-Hub-Attempts"] == "3"
    assert h["X-Free-LLM-Hub-Last-Error"] == "timeout"
    assert h["X-Free-LLM-Hub-Provider"] == "pa"
    assert h["X-Free-LLM-Hub-Model"] == "pa-coder"


def test_it_says_nothing_when_it_cannot_tell(in_mode):
    """A chain-exhausted error has no answering model to judge. Claiming a
    mismatch it cannot prove would be worse than silence."""
    h = A._routing_headers(None, None, 5)
    assert "X-Free-LLM-Hub-Mode" not in h


def test_a_broken_mode_lookup_is_silent(monkeypatch):
    def boom():
        raise RuntimeError("no request context")
    monkeypatch.setattr(A, "_active_mode", boom)
    h = A._routing_headers("pa", "pa-coder", 1)
    assert "X-Free-LLM-Hub-Mode" not in h
    assert h["X-Free-LLM-Hub-Attempts"] == "1"


def test_reporting_never_touches_routing():
    """_mode_status is read only where a response is being described, never
    where a candidate list is being built."""
    src = open("app.py", encoding="utf-8").read()
    assert src.count("_mode_status(") == 2          # the def, and _routing_headers
    i = src.index("def _apply_mode(")
    assert "_mode_status" not in src[i:i + 1500]
