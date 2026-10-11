"""Roles: the no-answer causes, measured 2026-10-11.

MEASURED (scripts/role_eval.py --since 2026-10-08T05:30 --why on the owner's
turn-roles.jsonl): 334 roles turns, 47 (14.1%) no-answer, 2.03 calls/turn.
Per-hop failure classes of the no-answer turns: 49 window, 30 deadline, 18
http4xx, 6 429, 1 invalid. hub.log shows the dominant pattern is a BIG
`coding|hard|tools|l` turn whose actor (a slow thinker, or space-bunny/kimi-k3
returning "200 with an empty message") burns the whole ~180 s stage across 3-4
short hops, after which the `best` fallback gets ~0 s ("nothing served after
180s" -> CHAT-DEADLINE).

Two flag-gated big-turn timing fixes (default on; off = byte-identical):
  rn_big_turn_one_attempt   -- the first fitting actor gets ONE long attempt
                               (whole room) + a parallel backup, not 2-3 short
                               sequential ones; fewer calls, more time to finish.
  rn_empty_actor_cap_big    -- a pair with a recent empty/junk 200 gets only a
                               short PROBE window as actor on a big turn, so it
                               cannot burn the stage returning empty.

Hermetic: no network, no file writes; the deadline math and the role_eval
--why breakdown are exercised directly.
"""
import importlib.util
import os
import time

import pytest

import app as A

# role_eval lives under scripts/ (not a package); load it by path, as the other
# roles tests do.
_spec = importlib.util.spec_from_file_location(
    "role_eval", os.path.join(os.path.dirname(A.__file__), "scripts", "role_eval.py"))
RE = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(RE)


LONG = A.LONG_CTX_SPEED_TOKENS + 1000       # a "big" request
SMALL = 1000                                 # under the clearing / long-ctx floor


def _flag_off(monkeypatch, off_name):
    monkeypatch.setattr(A.config, "get_flag",
                        lambda name, default=True: False if name == off_name else default)


@pytest.fixture(autouse=True)
def _empty_ledger_isolated():
    """The empty-200 ledger is a process-wide global; start every test here with
    a clean copy so the probe-cap tests are deterministic, and restore after."""
    with A._empty_200_lock:
        saved = dict(A._empty_200)
        A._empty_200.clear()
    yield
    with A._empty_200_lock:
        A._empty_200.clear()
        A._empty_200.update(saved)


# --------------------------------------------------------------------------- #
# Fix 1: rn_big_turn_one_attempt -- one long actor attempt on a big request
# --------------------------------------------------------------------------- #

def test_rn_actor_attempts_one_on_a_big_request():
    # a big request -> one long attempt; a small one or flag off -> base
    assert A._rn_actor_attempts(LONG, 3) == 1
    assert A._rn_actor_attempts(SMALL, 3) == 3


def test_rn_actor_attempts_flag_off_is_byte_identical(monkeypatch):
    _flag_off(monkeypatch, "rn_big_turn_one_attempt")
    assert A._rn_actor_attempts(LONG, 3) == 3
    assert A._rn_actor_attempts(LONG, 1) == 1


def test_one_attempt_gives_the_actor_the_whole_room(monkeypatch):
    # No measured long-ctx floor, so the share is pure room / attempts.
    monkeypatch.setattr(A, "_role_longctx_floor", lambda pid, model, est: 0.0)
    # The two-attempt split (what _role_hop_deadline caps a big request to today)
    two = A._role_hop_deadline(None, "px", "mx", 180.0, 3, LONG, None)
    # Fix 1 feeds attempts_left=1 at the call site -> the whole room.
    one = A._role_hop_deadline(None, "px", "mx", 180.0, 1, LONG, None)
    assert two == pytest.approx(90.0, abs=0.5)       # unchanged old behaviour
    assert one == pytest.approx(180.0, abs=0.5)      # one long attempt
    assert one > two                                 # the slow thinker gets ~2x the time


# --------------------------------------------------------------------------- #
# Fix 2: rn_empty_actor_cap_big -- a recently-empty pair gets a short probe
# --------------------------------------------------------------------------- #

@pytest.fixture
def empty_pair():
    key = ("pe", "me")
    with A._empty_200_lock:
        saved = A._empty_200.get(key)
        A._empty_200[key] = [(time.time(), "empty")]
    yield key
    with A._empty_200_lock:
        if saved is None:
            A._empty_200.pop(key, None)
        else:
            A._empty_200[key] = saved


def test_empty_pair_is_capped_to_a_probe_on_a_big_turn(monkeypatch, empty_pair):
    monkeypatch.setattr(A, "_role_longctx_floor", lambda pid, model, est: 0.0)
    pid, model = empty_pair
    assert A._empty_streak(pid, model) >= 1
    # big turn: the full-room (one attempt) share is cut to the probe window
    d = A._role_hop_deadline(None, pid, model, 180.0, 1, LONG, None)
    assert d == pytest.approx(A._RN_EMPTY_PROBE_SECONDS, abs=0.01)
    # a short room already under the probe is untouched (never raises the share)
    d2 = A._role_hop_deadline(None, pid, model, 30.0, 1, LONG, None)
    assert d2 == pytest.approx(30.0, abs=0.01)


def test_empty_cap_is_inert_off_flag_small_turn_and_clean_pair(monkeypatch, empty_pair):
    monkeypatch.setattr(A, "_role_longctx_floor", lambda pid, model, est: 0.0)
    pid, model = empty_pair
    # flag off -> the big-turn two-attempt share, no probe cap
    _flag_off(monkeypatch, "rn_empty_actor_cap_big")
    assert A._role_hop_deadline(None, pid, model, 180.0, 3, LONG, None) == pytest.approx(90.0, abs=0.5)
    # (flag off throughout) small turn -> never capped (est under the long-ctx floor)
    assert A._role_hop_deadline(None, pid, model, 180.0, 3, SMALL, None) == pytest.approx(60.0, abs=0.5)
    # a pair with NO empty history is never capped on a big turn (flag off is irrelevant)
    assert A._role_hop_deadline(None, "clean", "pair", 180.0, 1, LONG, None) == pytest.approx(180.0, abs=0.5)


def test_empty_cap_respects_an_explicit_budget(monkeypatch, empty_pair):
    monkeypatch.setattr(A, "_role_longctx_floor", lambda pid, model, est: 0.0)
    pid, model = empty_pair
    # an explicit budget still wins when it is tighter than the probe
    assert A._role_hop_deadline(None, pid, model, 180.0, 1, LONG, 20.0) == 20.0


# --------------------------------------------------------------------------- #
# role_eval --why breakdown (offline, synthetic rows)
# --------------------------------------------------------------------------- #

def _noans(failed, calls=0, hedge=None):
    return {"event": "turn", "turn": "tool", "served": None, "calls": calls,
            "hedge": hedge, "failed": failed, "ts": 1.0}


def test_hop_cause_classes():
    assert RE._hop_cause("window too small for ~94025 tokens") == "window"
    assert RE._hop_cause("tool schema alone exceeds this model's window") == "window"
    assert RE._hop_cause("no answer in time") == "deadline"
    assert RE._hop_cause("HTTP 429") == "429"
    assert RE._hop_cause("HTTP 503") == "5xx"
    assert RE._hop_cause("HTTP 400") == "http4xx"
    assert RE._hop_cause("200 with an empty message") == "empty/junk"
    assert RE._hop_cause("a tool call the CLI cannot run") == "invalid/prose"


def test_why_breakdown_counts_causes():
    rows = [
        {"event": "turn", "turn": "tool", "served": "p/m", "calls": 1, "failed": []},  # served
        _noans([{"pair": "a/b", "why": "no answer in time"},
                {"pair": "c/d", "why": "window too small for ~90000 tokens"}], calls=4),
        _noans([{"pair": "e/f", "why": "HTTP 400"}], calls=1),
        _noans([], calls=0),                       # no fitting model, no call
        {"event": "turn", "turn": "tool", "single": True, "served": None},   # team-notes, ignored
    ]
    w = RE.why_breakdown(rows)
    assert w["of_turns"] == 4 and w["no_answer_turns"] == 3
    assert w["hop_classes"]["window"] == 1 and w["hop_classes"]["deadline"] == 1
    assert w["hop_classes"]["http4xx"] == 1
    assert w["spent_no_call"] == 1
    assert w["first_hop_class"]["deadline"] == 1
    assert ("deadline", "window") in w["turn_cause_sets"]


def test_main_without_why_is_unchanged(tmp_path, capsys):
    roles = tmp_path / "turn-roles.jsonl"
    roles.write_text('{"event":"turn","turn":"tool","served":null,"calls":0,"failed":[],"ts":1.0}\n',
                     encoding="utf-8")
    log = tmp_path / "hub.log"
    log.write_text("", encoding="utf-8")
    RE.main(["--log", str(log), "--roles", str(roles)])
    out = capsys.readouterr().out
    assert "== roles ==" in out and "== why" not in out
    RE.main(["--log", str(log), "--roles", str(roles), "--why"])
    out = capsys.readouterr().out
    assert "== why (no-answer roles turns) ==" in out
