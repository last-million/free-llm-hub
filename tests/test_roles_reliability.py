"""Roles reliability (2026-10-10): fewer tool turns with no answer.

MEASURED (turn-roles.jsonl, 2026-10-08 05:30 UTC -> 2026-10-10): 323 roles
turns, 47 (15%) no-answer; ~45 failed actor hops were "window too small for
~N tokens" and 6 "tool schema alone exceeds ..." -- but per-hop clearing sends
far less than the ORIGINAL est, so those fits were decided on the wrong size.
Verifier: 123 runs, 50 "no verdict" (41%), the usable one fast, the failures
default-THINKING models that time out.

Every change is flag-gated; these hermetic tests use fake fleets/clock/upstream
(no network) and prove both the new behaviour and that it is inert when a flag
is off or the request does not clear. `_rr_` names are this session's.
"""
import importlib.util
import json
import pathlib
import time
import types

import pytest
import requests

import app as A
import clientgone


# --------------------------------------------------------------------------- #
# Shared fakes (same shape as tests/test_tool_turn_roles.py)
# --------------------------------------------------------------------------- #
TOOLS = [{"type": "function", "function": {"name": "write_file", "parameters": {}}},
         {"type": "function", "function": {"name": "bash", "parameters": {}}}]


class _Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload) if isinstance(payload, (dict, list)) else str(payload)

    def json(self):
        return self._payload

    def close(self):
        pass


def _tool_call(name="write_file", args="{}", cid="c1"):
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": cid, "type": "function",
                        "function": {"name": name, "arguments": args}}]}}]}


def _ro_call(cid="c1"):
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": cid, "type": "function",
                        "function": {"name": "read", "arguments": '{"path": "x.py"}'}}]}}]}


RO_TOOLS = [{"type": "function", "function": {"name": "read", "parameters": {}}}]


def _body(tools=None, stream=False):
    return {"model": "swarm", "tools": tools if tools is not None else TOOLS,
            "stream": stream,
            "messages": [{"role": "user", "content": "build the parser module"}]}


def _dispatcher(script, calls):
    """script: {pid: (delay, payload-or-None, status)}; records each dispatch."""
    def go(pid, payload, deadline):
        calls.append((pid, payload.get("model")))
        delay, out, status = script[pid]
        end = time.monotonic() + delay
        while time.monotonic() < end:
            if clientgone.cancelled():
                return None, None
            time.sleep(0.01)
        if out is None:
            return None, requests.RequestException("refused")
        return _Resp(out, status), None
    return go


@pytest.fixture
def roles_env(monkeypatch):
    """Pin routing + fleet so only the roles behaviour under test runs; read-only
    tools so the real verify.is_risky adds no verifier call unless a test opts in."""
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("p1", "m1", "hard"))
    monkeypatch.setattr(A, "_build_chain",
                        lambda *a, **k: [("p1", "m1"), ("p2", "m2"), ("p3", "m3")])
    monkeypatch.setattr(A, "_record_outcome", lambda *a, **k: None)
    monkeypatch.setattr(A, "_record_chat_usage", lambda *a, **k: None)
    monkeypatch.setattr(A, "_note_nonanswer", lambda *a, **k: None)
    monkeypatch.setattr(A, "_classify_difficulty", lambda *a, **k: "hard")
    monkeypatch.setattr(A, "_swarm_member_sick", lambda *a, **k: None)
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, model: 100.0)
    monkeypatch.setattr(A.prov, "is_model_allowed", lambda m: True)
    monkeypatch.setattr(A, "_tool_turn_race_on", lambda: False)
    monkeypatch.setattr(A, "_role_log", lambda row: None)     # no file writes
    with A._outcome_lock:
        saved = dict(A._tool_ttft)
        A._tool_ttft.clear()
    yield monkeypatch
    with A._outcome_lock:
        A._tool_ttft.clear()
        A._tool_ttft.update(saved)


# --------------------------------------------------------------------------- #
# Fix #1: size fit decisions on the post-clear estimate
# --------------------------------------------------------------------------- #

def test_rr_compute_sent_est_mirrors_clearing(monkeypatch):
    msgs = [{"role": "user", "content": "x"}]
    cleared = [{"role": "user", "content": "cleared"}]
    monkeypatch.setattr(A.ctxwin, "clear_old_tool_results",
                        lambda m, est_tokens=None: (cleared, {"cleared": 4}))
    monkeypatch.setattr(A, "_est_tokens",
                        lambda m, tools=None: 65000 if m is cleared else 180000)
    # flag on, est over the threshold -> the post-clear (sent) size
    assert A._rr_compute_sent_est(msgs, None, 180000) == 65000
    # under the clearing threshold -> original (clearing never runs)
    assert A._rr_compute_sent_est(msgs, None, 1000) == 1000
    # flag off -> original, byte-for-byte
    monkeypatch.setattr(A, "_rr_fit_on", lambda: False)
    assert A._rr_compute_sent_est(msgs, None, 180000) == 180000


def test_rr_fit_reads_g_and_is_capped(monkeypatch):
    with A.app.test_request_context():
        A.g.rr_sent_est = 65000
        assert A._rr_fit(180000) == 65000          # the per-request sent size
        assert A._rr_fit(50000) == 50000           # never larger than the caller's est
        monkeypatch.setattr(A, "_rr_fit_on", lambda: False)
        assert A._rr_fit(180000) == 180000         # flag off -> original


def test_rr_fit_falls_back_to_orig_without_g():
    # No app context (the roles tests) -> the original est, today's behaviour.
    assert A._rr_fit(180000) == 180000


def test_roles_walk_fits_a_model_on_the_cleared_size(roles_env, monkeypatch):
    """A model whose KNOWN window holds the SENT (cleared) size but not the
    original est is TRIED and serves -- instead of being skipped 'window too
    small' on the original size."""
    cleared = [{"role": "user", "content": "cleared"}]
    monkeypatch.setattr(A.ctxwin, "clear_old_tool_results",
                        lambda m, est_tokens=None: (cleared, {"cleared": 9}))
    # cleared list -> 65K, the full conversation -> 180K, the tools-only ([])
    # estimate -> tiny (so the tool-schema pre-filter never trips).
    monkeypatch.setattr(A, "_est_tokens",
                        lambda m, tools=None: 65000 if m is cleared else (180000 if m else 10))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("p1", "m1")])
    seen = []
    monkeypatch.setattr(A, "_window_fits",
                        lambda pid, model, est: seen.append(est) or (est <= 100000))
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline",
                        _dispatcher({"p1": (0.0, _ro_call(), 200)}, calls))
    out = A._swarm_tool_result(_body(tools=RO_TOOLS))
    assert out is not None, "the model that fits the cleared size should serve"
    data, _hdrs = out
    assert data["model"] == "p1/m1"
    assert 65000 in seen and 180000 not in seen, "the walk checked the cleared size"
    assert calls == [("p1", "m1")]


def test_roles_walk_without_clearing_skips_on_the_original_size(roles_env, monkeypatch):
    """Flag off (or nothing cleared): the fit is decided on the original est, so
    a too-small model is skipped exactly as before -- no answer, caller falls
    back to `best`."""
    monkeypatch.setattr(A, "_rr_fit_on", lambda: False)
    monkeypatch.setattr(A, "_est_tokens", lambda m, tools=None: 180000 if m else 10)
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("p1", "m1")])
    monkeypatch.setattr(A, "_window_fits", lambda pid, model, est: est <= 100000)
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline",
                        _dispatcher({"p1": (0.0, _ro_call(), 200)}, calls))
    out = A._swarm_tool_result(_body(tools=RO_TOOLS))
    assert out is None, "too small on the original size -> no answer (fallback to best)"
    assert calls == [], "a skipped hop is never dispatched"


def test_front_door_refuses_only_when_the_cleared_size_cannot_fit(monkeypatch):
    msgs = [{"role": "user", "content": "x"}]
    cleared = [{"role": "user", "content": "cleared"}]
    monkeypatch.setattr(A.ctxwin, "is_compaction_request", lambda m: False)
    monkeypatch.setattr(A.ctxwin, "clear_old_tool_results",
                        lambda m, est_tokens=None: (cleared, {"cleared": 9}))
    monkeypatch.setattr(A, "_est_tokens",
                        lambda m, tools=None: 65000 if m is cleared else 180000)
    monkeypatch.setattr(A, "_front_door_bound", lambda tools, images: (100000, 3))
    monkeypatch.setattr(A, "_ctx_fixed_part_est", lambda m, p: 1000)
    monkeypatch.setattr(A, "_ctx_compaction_futile", lambda fixed, bound: False)
    # Original 180K > 1.15x100K, but the cleared 65K fits -> NOT refused.
    assert A._front_door_overflow("openai", msgs, None, 180000) is None


# --------------------------------------------------------------------------- #
# Fix #2: a hop the hub knows it cannot make never spends an actor hop
# --------------------------------------------------------------------------- #

def test_rr_tools_exceed_and_prefilter(monkeypatch):
    big = [{"type": "function", "function": {
        "name": "f", "description": "d" * 60000, "parameters": {}}}]
    monkeypatch.setattr(A, "_model_ctx_budget", lambda pid, model: 8000)
    assert A._rr_tools_exceed("groq", "q", big) is True
    assert A._rr_tools_exceed("groq", "q", RO_TOOLS) is False     # small tools fit
    assert A._rr_tools_exceed("groq", "q", None) is False
    assert A._rr_prefilter_skip("groq", "q", big) == "tool schema exceeds this model's window"
    # a dead / not-offered model is pre-skipped
    monkeypatch.setattr(A, "_is_model_skipped", lambda pid, model: (pid, model) == ("p1", "m1"))
    assert A._rr_prefilter_skip("p1", "m1", RO_TOOLS) == "model dead or not offered"
    assert A._rr_prefilter_skip("p2", "m2", RO_TOOLS) is None
    # flag off -> never pre-skips
    monkeypatch.setattr(A.config, "get_flag",
                        lambda name, default=True: False if name == "rr_prefilter_hops" else default)
    assert A._rr_prefilter_skip("p1", "m1", big) is None


def test_prefiltered_hop_is_not_dispatched(roles_env, monkeypatch):
    """A model the hub knows is dead/not-offered is skipped WITHOUT a call; the
    next model serves."""
    monkeypatch.setattr(A, "_is_model_skipped", lambda pid, model: (pid, model) == ("p1", "m1"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("p1", "m1"), ("p2", "m2")])
    monkeypatch.setattr(A, "_window_fits", lambda pid, model, est: True)
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, _ro_call(), 200), "p2": (0.0, _ro_call(), 200)}, calls))
    out = A._swarm_tool_result(_body(tools=RO_TOOLS))
    assert out is not None
    data, hdrs = out
    assert data["model"] == "p2/m2"
    assert calls == [("p2", "m2")], "the skipped model was never dispatched"
    assert "calls=1" in hdrs["X-Free-LLM-Hub-Roles"], "the skip spent no call"


def test_a_404_marks_the_model_dead_so_it_is_not_tried_again(roles_env, monkeypatch):
    A._dead_models.pop(("p1", "m1"), None)
    monkeypatch.setattr(A, "_is_model_skipped", lambda pid, model: False)   # first turn: live
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("p1", "m1"), ("p2", "m2")])
    monkeypatch.setattr(A, "_window_fits", lambda pid, model, est: True)
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, {"error": "model not found"}, 404),
        "p2": (0.0, _ro_call(), 200)}, calls))
    try:
        out = A._swarm_tool_result(_body(tools=RO_TOOLS))
        assert out is not None and out[0]["model"] == "p2/m2"
        assert A._is_model_dead_upstream("p1", "m1") is True, "404 model remembered dead"
    finally:
        A._dead_models.pop(("p1", "m1"), None)


# --------------------------------------------------------------------------- #
# Fix #3: reserve a slice of the request clock for the `best` fallback
# --------------------------------------------------------------------------- #

def test_roles_turn_end_reserves_a_fallback_slice():
    started = 1000.0
    # Big non-stream turn: request clock 330 s, stage 360 s -> roles stops at
    # 330 - reserve(=99) = 231 s, leaving 99 s for the `best` fallback.
    end = A._rr_roles_turn_end(started, 360, started + 330)
    assert end - started == pytest.approx(231.0, abs=0.5)


def test_roles_turn_end_does_not_starve_the_stage():
    started = 1000.0
    # A small request clock (100 s): reserving would leave roles < _RR_ROLES_MIN,
    # so no reserve -- roles keeps the whole clock (the fallback relies on the
    # cleared context being fast). End == the request clock.
    end = A._rr_roles_turn_end(started, 360, started + 100)
    assert end - started == pytest.approx(100.0, abs=0.01)


def test_roles_turn_end_inert_without_a_request_clock_or_flag(monkeypatch):
    started = 1000.0
    assert A._rr_roles_turn_end(started, 180, None) - started == pytest.approx(180.0)
    monkeypatch.setattr(A.config, "get_flag",
                        lambda name, default=True: False if name == "rr_reserve_fallback" else default)
    # flag off -> stage deadline capped by the request clock, no reserve
    end = A._rr_roles_turn_end(started, 180, started + 330)
    assert end - started == pytest.approx(180.0)


# --------------------------------------------------------------------------- #
# Fix #4: a verifier that never answers is not a second opinion
# --------------------------------------------------------------------------- #

def test_rr_verifier_worth(monkeypatch):
    # a fast / non-thinking model is always worth a call
    monkeypatch.setattr(A, "_thinks_by_default", lambda pid, model: False)
    monkeypatch.setattr(A, "_is_slow_model", lambda pid, model: False)
    assert A._rr_verifier_worth("p", "fast") is True
    # a default-thinker with NO usable record -> not worth it (it would time out)
    monkeypatch.setattr(A, "_thinks_by_default", lambda pid, model: True)
    A._verifier_stats.pop(("p", "thinker"), None)
    assert A._rr_verifier_worth("p", "thinker") is False
    # ... but a thinker with a real usable-verdict record IS worth it
    now = time.time()
    A._verifier_stats[("p", "thinker")] = [(now, True)] * 8
    try:
        assert A._rr_verifier_worth("p", "thinker") is True
    finally:
        A._verifier_stats.pop(("p", "thinker"), None)


def test_rr_verifier_pool_drops_unworthy_and_can_empty(monkeypatch):
    pool = [("p", "thinker", 138.0), ("p", "fast", 137.0)]
    monkeypatch.setattr(A, "_rr_verifier_worth",
                        lambda pid, model: model == "fast")
    assert A._rr_verifier_pool(pool) == [("p", "fast", 137.0)]
    monkeypatch.setattr(A, "_rr_verifier_worth", lambda pid, model: False)
    assert A._rr_verifier_pool(pool) == []           # none worth it -> caller skips
    # flag off -> unchanged
    monkeypatch.setattr(A.config, "get_flag",
                        lambda name, default=True: False if name == "rr_verifier_prefilter" else default)
    assert A._rr_verifier_pool(pool) == pool


def test_verifier_is_skipped_when_no_candidate_is_worth_a_call(roles_env, monkeypatch):
    """A risky step whose only verifier candidates are thinkers-with-no-record:
    the verifier is SKIPPED (ship the original) instead of burning a call that
    would time out."""
    mod = types.ModuleType("verify")
    mod.VERIFY_MAX_TOKENS = 300
    mod.family = lambda model_id: str(model_id)[:1]
    picked = []
    mod.pick_verifier = lambda producer, candidates: picked.append(1)
    mod.is_risky = lambda first, difficulty, observed_pass=None: True
    mod.digest = lambda messages, proposed, strict=False: [{"role": "user", "content": "V"}]
    mod.parse_verdict = lambda text: json.loads(text)
    monkeypatch.setitem(__import__("sys").modules, "verify", mod)
    monkeypatch.setattr(A, "_thinks_by_default", lambda pid, model: True)  # all thinkers
    rec = {"calls": 0, "sent_tokens": 0, "verifier": None, "verdict": None,
           "severity": None, "corrector": None, "corrected": False,
           "verifier_unparsed": 0, "verifier_retry": 0}
    rows = []
    out = A._role_verify_and_correct(
        {"messages": [], "tools": TOOLS}, [], ("p1", "m1"),
        _tool_call()["choices"][0]["message"], [("p2", "m2"), ("p3", "m3")],
        "kind", "hard", rec, rows, time.monotonic() + 60, 1000, tool_turn=True)
    assert out is None
    assert rec["verdict"] == "skipped: no usable verifier"
    assert picked == [], "no verifier model was called"


def test_a_usable_verifier_still_runs(roles_env, monkeypatch):
    """Inert check: when a worthy (non-thinking) verifier exists, it runs as
    before and an ACCEPT ships the original."""
    mod = types.ModuleType("verify")
    mod.VERIFY_MAX_TOKENS = 300
    mod.family = lambda model_id: str(model_id)[:1]
    picked = []
    mod.pick_verifier = lambda producer, candidates: (
        picked.append(list(candidates)) or (candidates[0][0], candidates[0][1]))
    mod.is_risky = lambda first, difficulty, observed_pass=None: True
    mod.digest = lambda messages, proposed, strict=False: [{"role": "user", "content": "V"}]
    mod.parse_verdict = lambda text: json.loads(text)
    monkeypatch.setitem(__import__("sys").modules, "verify", mod)
    monkeypatch.setattr(A, "_thinks_by_default", lambda pid, model: False)
    monkeypatch.setattr(A, "_is_slow_model", lambda pid, model: False)
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p2": (0.0, {"choices": [{"message": {"role": "assistant",
              "content": json.dumps({"ok": True, "problems": [], "severity": "low"})}}]}, 200),
    }, []))
    rec = {"calls": 0, "sent_tokens": 0, "verifier": None, "verdict": None,
           "severity": None, "corrector": None, "corrected": False,
           "verifier_unparsed": 0, "verifier_retry": 0}
    out = A._role_verify_and_correct(
        {"messages": [], "tools": TOOLS}, [], ("p1", "m1"),
        _tool_call()["choices"][0]["message"], [("p2", "m2")],
        "kind", "hard", rec, [], time.monotonic() + 60, 1000, tool_turn=True)
    assert out is None                      # ACCEPT -> ship the original
    assert rec["verdict"] == "ok"
    assert picked, "a worthy verifier was chosen and run"


# --------------------------------------------------------------------------- #
# Fix #5: role_eval --since and the roles-turn definition
# --------------------------------------------------------------------------- #

def _load_role_eval():
    p = pathlib.Path(A.__file__).parent / "scripts" / "role_eval.py"
    spec = importlib.util.spec_from_file_location("role_eval_test", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parse_since_forms():
    re_ = _load_role_eval()
    assert re_.parse_since("1700000000") == pytest.approx(1700000000.0)
    assert re_.parse_since("2026-10-08T05:30") == pytest.approx(
        __import__("datetime").datetime(2026, 10, 8, 5, 30,
            tzinfo=__import__("datetime").timezone.utc).timestamp())
    now = re_._now_ts()
    assert now - 7 * 86400 - 2 < re_.parse_since("7d") < now - 7 * 86400 + 2
    assert re_.parse_since("garbage") is None
    assert re_.parse_since(None) is None


def test_roles_stats_excludes_single_team_rows_and_old_rows_work():
    re_ = _load_role_eval()
    rows = [
        {"event": "turn", "turn": "tool", "served": "p/m", "calls": 1,
         "actor_calls": 1, "wasted_calls": 0, "latency_s": 1.0},        # a roles turn
        {"event": "turn", "turn": "tool", "single": True, "served": None,
         "calls": 0, "latency_s": 0.1},                                 # team notes (excluded)
        {"event": "turn", "turn": "text", "verifier": "p/v", "verdict": "ok"},  # a text review
        {"event": "credit", "credited": 1},
    ]
    st = re_.roles_stats(rows)
    assert st["turns"] == 1 and st["single_team_turns"] == 1
    assert st["served"] == 1 and st["zero_answer_rate"] == 0.0


def test_role_eval_since_filters_rows(tmp_path):
    re_ = _load_role_eval()
    f = tmp_path / "roles.jsonl"
    old = {"event": "turn", "turn": "tool", "served": "p/m", "calls": 1,
           "actor_calls": 1, "latency_s": 1.0, "ts": 1000.0}
    new = {"event": "turn", "turn": "tool", "served": None, "calls": 2,
           "actor_calls": 2, "latency_s": 1.0, "ts": 5000.0}
    f.write_text(json.dumps(old) + "\n" + json.dumps(new) + "\n", encoding="utf-8")
    out = re_.main(["--roles", str(f), "--log", str(tmp_path / "none.log"),
                    "--since", "4000", "--json"])
    assert out["roles"]["turns"] == 1                   # only the ts>=4000 row
    assert out["roles"]["zero_answer_rate"] == 1.0      # that one had no answer
