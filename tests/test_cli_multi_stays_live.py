"""Stay in the CLI until the Multi run ends (owner, 2026-10-11: "I can't stay
just in the CLI?").

For a CLI whose stream timeout is MEASURED to be idle-only (opencode 1.19.0
held a 15-minute stream with one line every 20 s, see AGENTS.md), the START
turn stays open and shows the whole run live instead of handing off to the
background after ~20 s; the finished report is marked delivered so the next
message does not get it a second time. Other CLIs keep the hand-off. Reuses the
hermetic `cm` fixture of test_cli_multi_live_and_async (faked turn, follow
loop, owner sessions and swarm_windows; faked clock where time matters).
"""
import pytest

import app as A
from test_cli_multi_live_and_async import _FakeRun, _call, cm  # noqa: F401  (fixture)

LINK = "http://127.0.0.1:8787/agent/owner-1"


def test_opencode_stays_live_by_default():
    assert A._cm_stay_live("opencode") is True
    assert A._cm_safe_end_seconds("opencode") == A._CM_STAY_MAX_SECONDS
    assert A._cm_staying(A._cm_safe_end_seconds("opencode"))


@pytest.mark.parametrize("cli", ["codex", "claude", "kimi", "", None])
def test_unmeasured_clis_keep_the_hand_off(cli):
    assert A._cm_stay_live(cli) is False
    assert not A._cm_staying(A._cm_safe_end_seconds(cli))


def test_the_flag_turns_it_off(monkeypatch):
    real = A.config.get_flag
    monkeypatch.setattr(A.config, "get_flag",
                        lambda n, d=None: False if n == "cli_multi_stay_live" else real(n, d))
    assert A._cm_stay_live("opencode") is False
    assert A._cm_safe_end_seconds("opencode") == A._CM_SAFE_END["opencode"]


def test_a_staying_turn_never_hands_off_and_streams_the_report(cm, monkeypatch):
    # Half an hour between events (3 h in all, under the 6 h ceiling): a
    # hand-off turn would have ended long ago.
    ticks = iter([0.0] + [i * 1800.0 for i in range(1, 40)])
    monkeypatch.setattr(A.time, "monotonic", lambda: next(ticks))
    events = iter([{"event": "notice", "text": "Multi sessions: 2 phases (run r)."},
                   {"event": "tool", "text": "Phase 1/2 Build"},
                   {"event": "output", "text": "Phase 1/2 Build -- done: built"},
                   {"event": "tool", "text": "Phase 2/2 Test"},
                   {"event": "output", "text": "Phase 2/2 Test -- done: tested"},
                   {"event": "message", "text": "DONE: everything"}])
    delivered = []
    joined = "".join(A._cm_text_stream(
        "owner-1", "run-1", events, LINK, A._CM_STAY_MAX_SECONDS, 6, "HEADER\n",
        early_end=True, on_report=delivered.append))
    assert "working in the background" not in joined
    assert "Phase 2/2 Test -- done: tested" in joined and "DONE: everything" in joined
    assert delivered == ["run-1"]


def test_a_handing_off_turn_does_not_mark_the_report_delivered(cm, monkeypatch):
    ticks = iter([0.0, 100.0, 100.0, 100.0, 100.0])
    monkeypatch.setattr(A.time, "monotonic", lambda: next(ticks))
    events = iter([{"event": "notice", "text": "Multi sessions: 2 phases (run r)."},
                   {"event": "tool", "text": "Phase 1/2 Build"},
                   {"event": "tool", "text": "Phase 1/2 still going"},
                   {"event": "message", "text": "DONE later"}])
    delivered = []
    joined = "".join(A._cm_text_stream(
        "owner-1", "run-1", events, LINK, 540, 6, "HEADER\n",
        early_end=True, on_report=delivered.append))
    assert "Multi is working in the background" in joined
    assert delivered == []          # the next message still gets the report


def test_the_start_turn_says_it_stays_and_the_report_is_not_repeated(cm):
    cm["last_run"] = _FakeRun("run-9", A.swarm_windows.DONE)
    resp, raw, _b, _m = _call()
    assert resp is not None
    assert "I stay here and show the whole run live until it finishes" in raw
    assert "Esc stops watching, not the run" in raw
    assert "DONE: built" in raw
    # The streamed report was marked delivered: the next message is not handed
    # the same report again.
    row = A._cm_map_get(A._cm_map_key("conv-1", A._cm_folder_key(cm["proj"])))
    assert row and row.get("delivered") == "run-9"
