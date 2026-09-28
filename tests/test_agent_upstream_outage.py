"""An /agent turn silenced by the hub's own failures stops with the reason.

MEASURED 2026-09-28 15:02-15:27 (session 47a25faa, opencode, ~54K tokens):
four requests in a row ended 504 at the request deadline, opencode retried
them silently, the watchdog said "looks wedged, resuming", resumed into the
same outage and failed after 25 minutes with no reason given.
"""
import pytest

import agentic_chat as AC
import app
from test_agent_early_server import DONE, HANG, START, _notices, clock_turn  # noqa: F401

HOPS = ["nvidia/moonshotai/kimi-k3 ! nvidia: _HopBudgetExceeded",
        "dahl/deepseek-ai/DeepSeek-V4-Flash-0731 ! dahl: HTTP 429",
        "dahl/zai-org/GLM-5.3-Flash ! dahl: HTTP 429",
        "tokenrouter/moonshotai/kimi-k3-free ! tokenrouter: HTTP 503",
        "kilocode/liquid/lfm-2.5-2.6b:free ! kilocode: _ContextOverflow",
        "nvidia/z-ai/glm-5.3 ! nvidia: _HopBudgetExceeded"]


@pytest.fixture(autouse=True)
def _clean_ledger():
    app._AGENT_UPSTREAM.clear()
    yield
    app._AGENT_UPSTREAM.clear()


def _act(session, status, http, hops=()):
    act = {"session": session, "hops": list(hops), "finished": None}
    app._activity_done(act, status, http)
    return act


def test_the_hop_list_reads_as_plain_words():
    s = app._hops_failure_summary(HOPS)
    assert s == ("nvidia: too slow; dahl: rate-limited; tokenrouter: server error; "
                 "kilocode: conversation too long for it")
    assert app._hops_failure_summary(["groq/x"]) == ""          # a served hop is no failure


def test_a_session_that_found_no_model_is_reported():
    t0 = __import__("time").time() - 1
    _act("s1", "error", 504, HOPS)
    _act("s1", "error", 504, HOPS)
    rep = app._agent_upstream_probe("s1", t0)
    assert rep["failures"] == 2 and rep["http"] == 504 and not rep["ok_after"]
    assert "nvidia: too slow" in rep["why"]
    assert app._agent_upstream_probe("s1", t0 + 3600) is None     # nothing that recent
    assert app._agent_upstream_probe("other", t0) is None


def test_a_success_after_the_failure_clears_it():
    t0 = __import__("time").time() - 1
    _act("s2", "error", 503, HOPS)
    _act("s2", "ok", 200)
    assert app._agent_upstream_probe("s2", t0)["ok_after"] is True


def test_only_chain_failures_count_and_non_agent_traffic_is_ignored():
    t0 = __import__("time").time() - 1
    _act("s3", "error", 400)                 # a client error is not an outage
    _act("s3", "empty", 200)
    _act(None, "error", 504, HOPS)
    assert app._agent_upstream_probe("s3", t0) is None
    assert list(app._AGENT_UPSTREAM) == ["s3"]


def test_a_row_is_counted_once():
    t0 = __import__("time").time() - 1
    act = _act("s4", "error", 504, HOPS)
    app._activity_done(act, "error", 504)
    assert app._agent_upstream_probe("s4", t0)["failures"] == 1


def test_the_ledger_is_bounded(monkeypatch):
    monkeypatch.setattr(app, "_AGENT_UPSTREAM_SESSIONS", 3)
    for i in range(5):
        _act("s%d" % i, "error", 504, HOPS)
    assert list(app._AGENT_UPSTREAM) == ["s2", "s3", "s4"]


def test_the_probe_is_registered_with_the_agent_runner():
    assert AC._upstream_probe is app._agent_upstream_probe


def test_the_failure_text_names_the_cause_and_the_next_step():
    text = AC.outage_detail("opencode", {"failures": 2, "why": "nvidia: too slow"})
    assert text.startswith("No free model could answer this turn: 2 requests in a row")
    assert "(nvidia: too slow)" in text and "Send \"continue\"" in text
    assert "looks wedged" not in text


# ---------------------------------------------------------------- the watchdog

def _probe(rep, seen):
    def fn(sid, since):
        seen.append((sid, since))
        return rep
    return fn


def test_a_silence_the_hub_explains_stops_without_a_blind_resume(clock_turn, monkeypatch):
    seen = []
    monkeypatch.setattr(AC, "_upstream_probe", _probe(
        {"failures": 2, "http": 504, "why": "nvidia: too slow; dahl: rate-limited",
         "ok_after": False}, seen))
    events, rec, sess = clock_turn([(START + [HANG], None, None), (DONE, None, None)])
    assert len(rec["procs"]) == 1                                # no resume
    assert not any("looks wedged" in n for n in _notices(events))
    last = events[-1]
    assert last["event"] == "error" and last["status"] == 503
    assert "No free model could answer this turn" in last["detail"]
    assert "nvidia: too slow; dahl: rate-limited" in last["detail"]
    assert seen and seen[0][0] == sess.id and isinstance(seen[0][1], float)


def test_an_unexplained_silence_keeps_the_old_resume(clock_turn, monkeypatch):
    monkeypatch.setattr(AC, "_upstream_probe", _probe(None, []))
    events, rec, _ = clock_turn([(START + [HANG], None, None), (DONE, None, None)])
    assert len(rec["procs"]) == 2
    assert any("looks wedged" in n for n in _notices(events))
    assert events[-1]["event"] == "done"


def test_a_model_that_answered_after_the_failures_keeps_the_old_resume(clock_turn, monkeypatch):
    monkeypatch.setattr(AC, "_upstream_probe", _probe(
        {"failures": 1, "http": 504, "why": "x", "ok_after": True}, []))
    events, rec, _ = clock_turn([(START + [HANG], None, None), (DONE, None, None)])
    assert len(rec["procs"]) == 2


def test_a_mode_and_a_quality_are_both_sent():
    # MEASURED 2026-09-28: Max + coding asked for bare "coding" (Normal tier).
    assert AC._hub_model_for("max", "coding") == "coding-max"
    assert AC._hub_model_for("swarm", "coding") == "coding-swarm"
    assert AC._hub_model_for("normal", "coding") == "coding"
    assert AC._hub_model_for("max") == "best"
    # every id handed to a CLI exists in the picker the hub writes for it
    ids = set(AC._opencode_hub_models())
    assert {"coding-max", "coding-swarm", "coding"} <= ids
    assert app._split_category_effort("coding-max") == ("coding", "max")
