"""A Multi run started from a terminal CLI survives a hub restart.

MEASURED 2026-10-19 (swarm-25d638086224, opencode in a terminal, folder
"bount en"): the owner conversation the hub opened for the CLI had no history
record until the run's report was filed, so set_auto_resume(True) was a no-op,
the boot resume did not know the owner, and the graceful restart left the run
"interrupted by a hub restart" (phase 6 never ran). Hermetic: reuses the faked
CLI-Multi fixture; the history writes go to its tmp state dir.
"""
import agentic_history as H

import app as A
from test_cli_multi_live_and_async import _call, cm  # noqa: F401  (fixture)

# The real history functions, captured before the fixture replaces some.
REAL = {n: getattr(H, n) for n in ("set_auto_resume", "set_quality", "set_mode",
                                   "set_title", "get_conversation")}


def _real_history(monkeypatch):
    for name, fn in REAL.items():
        monkeypatch.setattr(A.agentic_history, name, fn)


def test_the_owner_of_a_cli_run_is_known_and_continues_after_a_restart(cm, monkeypatch):
    _real_history(monkeypatch)
    resp, raw, _b, _m = _call()
    assert resp is not None
    sid = cm["start_session"][0]["sid"]
    assert sid in H.known_session_ids()
    assert H.auto_resume(sid) is True
    conv = H.get_conversation(sid)
    assert conv["quality"] == "multi"
    assert conv["turns"][0]["role"] == "user"
    assert "dashboard with a chart and a table" in conv["turns"][0]["text"]


def test_the_boot_resume_now_accepts_that_run(cm, monkeypatch):
    _real_history(monkeypatch)
    _call()
    sid = cm["start_session"][0]["sid"]

    class Run:
        owner = sid
        id = "swarm-x"
    assert A._multi_should_auto_resume(Run()) is True


def test_a_history_failure_never_stops_the_run(cm, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(A.agentic_history, "record_turn", boom)
    resp, raw, _b, _m = _call()
    assert resp is not None and "DONE: built" in raw
