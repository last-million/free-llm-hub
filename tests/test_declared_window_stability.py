"""What a CLI is told stays put unless the change is real.

MEASURED 2026-10-03/04 (hub.log): the 30-min resync rewrote opencode.json (and
codex's catalog) 7 times in ~7 h -- the computed declared figures followed the
live fleet and flipped back and forth. A decrease must still apply at once (a
too-big window is the 503 risk); an increase only after it has held.
"""
import pytest

import app as A


@pytest.fixture
def fleet(monkeypatch):
    box = {"v": 262144}
    monkeypatch.setattr(A, "_declared_window_for", lambda mid=None, cli=None: box["v"])
    monkeypatch.setattr(A, "_declared_published", {})
    monkeypatch.setattr(A, "_declared_raise_seen", {})
    clock = {"t": 1000.0}
    monkeypatch.setattr(A.time, "time", lambda: clock["t"])
    return box, clock


def test_first_figure_is_published(fleet):
    assert A._stable_declared_window_for("coding", cli="opencode") == 262144


def test_a_decrease_applies_at_once(fleet):
    box, _clock = fleet
    A._stable_declared_window_for("coding", cli="opencode")
    box["v"] = 250000
    assert A._stable_declared_window_for("coding", cli="opencode") == 250000


def test_a_flip_back_up_waits_until_it_has_held(fleet):
    box, clock = fleet
    A._stable_declared_window_for("coding", cli="opencode")       # 262144
    box["v"] = 250000
    assert A._stable_declared_window_for("coding", cli="opencode") == 250000
    box["v"] = 262144                                             # the flip back
    assert A._stable_declared_window_for("coding", cli="opencode") == 250000
    clock["t"] += 1800                                            # one resync later
    assert A._stable_declared_window_for("coding", cli="opencode") == 250000
    clock["t"] += A._DECLARED_RAISE_AFTER                          # held long enough
    assert A._stable_declared_window_for("coding", cli="opencode") == 262144


def test_an_up_down_flip_never_raises_the_published_figure(fleet):
    box, clock = fleet
    A._stable_declared_window_for("auto", cli=None)               # 262144
    for _ in range(6):                                            # 3 h of flipping
        box["v"] = 400000
        assert A._stable_declared_window_for("auto", cli=None) == 262144
        clock["t"] += 1800
        box["v"] = 262144
        assert A._stable_declared_window_for("auto", cli=None) == 262144
        clock["t"] += 1800


def test_each_cli_and_id_keeps_its_own_figure(fleet):
    box, _clock = fleet
    assert A._stable_declared_window_for("coding", cli="opencode") == 262144
    box["v"] = 400000
    assert A._stable_declared_window_for("coding", cli="codex") == 400000
    assert A._stable_declared_window_for("coding", cli="opencode") == 262144


def test_none_passes_through(monkeypatch):
    monkeypatch.setattr(A, "_declared_window_for", lambda mid=None, cli=None: None)
    assert A._stable_declared_window_for("auto") is None


def test_the_resync_names_what_changed(monkeypatch, caplog):
    sigs = iter([(("auto", 262144, 400000), ("<none>", 262144)),
                 (("auto", 250000, 400000), ("<none>", 250000))])
    monkeypatch.setattr(A, "_declared_window_signature", lambda: next(sigs))
    monkeypatch.setattr(A, "_resync_declared_windows", lambda: [])
    monkeypatch.setattr(A, "_declared_resync_last", [None])
    A._resync_declared_windows_if_changed()
    with caplog.at_level("INFO", logger="free-llm-hub"):
        A._resync_declared_windows_if_changed()
    text = caplog.text
    assert "declared windows changed" in text
    assert "auto 262144/400000->250000/400000" in text
