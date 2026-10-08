"""`[ctx] declared window inputs:` -- the log line that says WHY the safe
declared window is what it is, next to `[ctx] declared windows changed:`.

MEASURED 2026-10-08 03:36:44: `all 65536/400000->32000/400000` -- the safe
figure fell to the clamp floor and the log could not say whether the 25th
percentile or the 3rd-provider cap did it. Log only: no value or policy moves.
Fake fleets, no network, nothing from the owner's real files.
"""
import logging
import re

import pytest

import agentic_chat as AC
import app as A


@pytest.fixture(autouse=True)
def _clean():
    A._declared_fleet_cache[1] = None
    A._declared_inputs_logged.clear()
    saved = A._declared_resync_last[0]
    yield
    A._declared_resync_last[0] = saved
    A._declared_inputs_logged.clear()
    A._declared_fleet_cache[1] = None


def _fleet(monkeypatch, rows):
    monkeypatch.setattr(A, "_declared_fleet", lambda: list(rows))
    monkeypatch.setattr(A, "_mode_allows", lambda *a, **k: True)


def _rows(*specs):
    out = []
    for pid, n, w in specs:
        out += [(pid, "sk-SECRET-model-%s-%d" % (pid, i), w, 135.0) for i in range(n)]
    return out


# ---- the numbers ------------------------------------------------------------

def test_the_provider_cap_is_named_when_it_is_what_binds(monkeypatch):
    rows = _rows(("google", 3, 1048576), ("openrouter", 1, 1000000), ("kilocode", 1, 262144),
                 ("nvidia", 2, 250000)) + [("g4f", "srv_%d" % i, 1000000, 130.0) for i in range(6)]
    _fleet(monkeypatch, rows)
    info = A._declared_window_inputs("auto")
    assert info["rule"] == "provider cap"
    assert info["percentile"] > info["cap"] == 262144
    assert info["final"] == A._declared_window_for("auto") == 262144
    assert [p for p, _w in info["providers"]] == ["google", "openrouter", "kilocode", "nvidia"], \
        "ids only, biggest first, the relay never listed"
    assert info["relay_rows"] == 6 and info["rows"] == len(rows)


def test_the_percentile_is_named_when_it_is_what_binds(monkeypatch):
    rows = _rows(("google", 1, 1048576), ("openrouter", 1, 1000000), ("nvidia", 1, 250000),
                 ("glm", 12, 65536), ("zenmux", 3, 40000))
    _fleet(monkeypatch, rows)
    info = A._declared_window_inputs("auto")
    assert info["rule"] == "percentile"
    assert info["percentile"] == 65536 < info["cap"]
    assert info["final"] == A._declared_window_for("auto") == 65536


def test_the_floor_is_named_with_what_produced_the_raw_figure(monkeypatch):
    # a quarter of the rows are 8K groq models: the quantile lands in that cluster
    rows = _rows(("openrouter", 3, 1000000), ("nvidia", 6, 250000), ("kilocode", 4, 262144),
                 ("groq", 14, 8000), ("zenmux", 8, 32768))
    _fleet(monkeypatch, rows)
    info = A._declared_window_inputs("auto")
    assert info["rule"] == "floor"
    assert info["raw"] == 8000 and info["percentile"] == 8000
    assert info["final"] == A._declared_window_for("auto") == AC._DECLARED_WINDOW_MIN == 32000
    line = A._declared_inputs_line(info)
    assert "floor 32000 (raw 8000 from the percentile)" in line


def test_a_small_third_provider_is_a_floor_from_the_provider_cap(monkeypatch):
    rows = _rows(("openrouter", 10, 1000000), ("nvidia", 10, 250000), ("groq", 1, 8000))
    _fleet(monkeypatch, rows)
    info = A._declared_window_inputs("auto")
    assert info["rule"] == "floor" and info["cap"] == 8000 < info["percentile"]
    assert "floor 32000 (raw 8000 from the provider cap)" in A._declared_inputs_line(info)


def test_unknown_and_pinned_pools_say_so_and_give_no_figure(monkeypatch):
    _fleet(monkeypatch, _rows(("google", 2, 262144), ("openrouter", 20, None)))
    info = A._declared_window_inputs("auto")
    assert info["rule"] == "unknown pool" and info["final"] is None
    assert A._declared_window_for("auto") is None
    _fleet(monkeypatch, _rows(("google", 1, 262144)))
    pinned = A._declared_window_inputs("google/sk-SECRET-model-google-0")
    assert pinned["rule"] == "pinned" and pinned["final"] == 262144


def test_the_explanation_always_agrees_with_the_real_figure(monkeypatch):
    fleets = [_rows(("google", 3, 1048576), ("openrouter", 1, 1000000), ("kilocode", 1, 262144)),
              _rows(("a", 5, 8000), ("b", 5, 65536), ("c", 5, 131072), ("d", 5, 250000)),
              _rows(("a", 1, 2000000), ("b", 1, 2000000), ("c", 1, 2000000),
                    ("d", 1, 2000000), ("e", 1, 2000000), ("f", 1, 2000000))]
    for rows in fleets:
        _fleet(monkeypatch, rows)
        A._declared_fleet_cache[1] = None
        for mid in ("auto", "best", "multi"):
            assert A._declared_window_inputs(mid)["final"] == A._declared_window_for(mid), \
                (mid, rows[:1])


def test_it_changes_no_value_and_never_raises(monkeypatch):
    _fleet(monkeypatch, _rows(("google", 3, 1048576), ("nvidia", 3, 250000),
                              ("kilocode", 3, 262144)))
    before = {m: A._declared_window_for(m) for m in ("auto", "best", "coding")}
    A._declared_window_inputs("auto")
    A._declared_inputs_line(A._declared_window_inputs("auto"))
    assert before == {m: A._declared_window_for(m) for m in before}

    def boom(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(A, "_tier_pool", boom)
    assert A._declared_window_inputs("auto") is None
    assert A._declared_inputs_line(None) is None
    A._log_declared_inputs((("auto", 1, 2),), {"auto": (3, 4)})        # swallowed


# ---- the log line at the `changed` moment -----------------------------------

def _lines(caplog, prefix):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(prefix)]


def _drive(monkeypatch, sigs):
    """Feed _resync_declared_windows_if_changed a scripted sequence of signatures."""
    it = iter(sigs)
    monkeypatch.setattr(A, "_declared_window_signature", lambda: next(it))
    monkeypatch.setattr(A, "_resync_declared_windows", lambda: [])


def test_one_inputs_line_per_changed_figure_next_to_the_changed_line(monkeypatch, caplog):
    _fleet(monkeypatch, _rows(("openrouter", 3, 1000000), ("nvidia", 6, 250000),
                              ("kilocode", 4, 262144), ("groq", 14, 8000),
                              ("zenmux", 8, 32768)))
    s1 = (("auto", 65536, 400000), ("best", 65536, 400000), ("<none>", 65536))
    s2 = (("auto", 32000, 400000), ("best", 32000, 400000), ("<none>", 32000))
    s3 = (("auto", 65536, 400000), ("best", 65536, 400000), ("<none>", 65536))
    _drive(monkeypatch, [s1, s2, s2, s3])
    A._declared_resync_last[0] = None
    with caplog.at_level(logging.INFO, logger=A._log.name):
        A._resync_declared_windows_if_changed()          # first pass: nothing to compare
        A._resync_declared_windows_if_changed()          # 65536 -> 32000
        A._resync_declared_windows_if_changed()          # unchanged
        A._resync_declared_windows_if_changed()          # 32000 -> 65536
    changed = _lines(caplog, "[ctx] declared windows changed:")
    inputs = _lines(caplog, "[ctx] declared window inputs:")
    assert len(changed) == 2 and len(inputs) == 2, "exactly one inputs line per change"
    assert "tier auto safe 65536->32000 (+2 more tiers changed)" in inputs[0]
    assert "rule=floor" in inputs[0] and "raw 8000" in inputs[0]
    assert ("providers counted (5, largest window each): openrouter 1000000, kilocode 262144, "
            "nvidia 250000, zenmux 32768, groq 8000") in inputs[0]
    assert "tier auto safe 32000->65536" in inputs[1]


def test_the_same_figure_is_not_described_twice(monkeypatch, caplog):
    _fleet(monkeypatch, _rows(("openrouter", 3, 1000000), ("nvidia", 6, 250000),
                              ("kilocode", 4, 262144)))
    a = (("auto", 65536, 400000),)
    b = (("auto", 32000, 400000),)
    _drive(monkeypatch, [a, b, a, b])
    A._declared_resync_last[0] = None
    with caplog.at_level(logging.INFO, logger=A._log.name):
        for _ in range(4):
            A._resync_declared_windows_if_changed()
    # 65536->32000, 32000->65536, 65536->32000: three changes, three DIFFERENT
    # (tier, figure) keys in a row, so three lines -- but never two for one key.
    keys = [re.search(r"tier auto safe (\d+->\d+)", m).group(1)
            for m in _lines(caplog, "[ctx] declared window inputs:")]
    assert keys == ["65536->32000", "32000->65536", "65536->32000"]
    A._declared_inputs_logged["auto"] = (32000, 400000)
    _drive(monkeypatch, [a, b])
    A._declared_resync_last[0] = None
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=A._log.name):
        A._resync_declared_windows_if_changed()
        A._resync_declared_windows_if_changed()
    assert _lines(caplog, "[ctx] declared window inputs:") == [], \
        "(tier, figure) already described is not logged again"


def test_the_line_carries_only_ids_and_numbers(monkeypatch, caplog):
    _fleet(monkeypatch, _rows(("openrouter", 3, 1000000), ("nvidia", 6, 250000),
                              ("kilocode", 4, 262144), ("g4f", 2, 1000000)))
    _drive(monkeypatch, [(("auto", 65536, 400000),), (("auto", 250000, 400000),)])
    A._declared_resync_last[0] = None
    with caplog.at_level(logging.INFO, logger=A._log.name):
        A._resync_declared_windows_if_changed()
        A._resync_declared_windows_if_changed()
    [line] = _lines(caplog, "[ctx] declared window inputs:")
    assert "SECRET" not in line and "sk-" not in line, "no model name, key or token"
    assert "g4f" not in line.split("providers counted")[1], "a relay is never a counted provider"
    assert "relay rows" in line


def test_a_failing_explanation_never_breaks_the_resync(monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(A, "_declared_window_inputs", boom)
    _drive(monkeypatch, [(("auto", 65536, 400000),), (("auto", 32000, 400000),)])
    A._declared_resync_last[0] = None
    with caplog.at_level(logging.INFO, logger=A._log.name):
        A._resync_declared_windows_if_changed()
        A._resync_declared_windows_if_changed()
    assert len(_lines(caplog, "[ctx] declared windows changed:")) == 1
    assert _lines(caplog, "[ctx] declared window inputs:") == []
