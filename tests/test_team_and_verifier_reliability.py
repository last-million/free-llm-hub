"""Team notes and the verifier, made reliable (2026-10-04).

MEASURED on turn-roles.jsonl (214 role turns): team notes ran on 8 (3.7%) --
difficulty split 183 medium / 22 hard and the gate needed `hard`; 12
specialist calls, 5 answered (42%), 7 failed (4 hit the 25 s limit, 2 empty,
1 429), answered p50 11.9 s / p90 19.4 s; nvidia/kimi-k3 and the qwen3.8-27b
:free copies 0/3, gemini-3.6-flash 2/2. The verifier ran on 19 turns: 15 "no
verdict (fail-open)", 2 ok, 2 revise = 21% usable.

  * specialists are ranked by measured answered rate (Beta(1,1) prior) then
    p50, only inside the top band, a pair that failed 2 of its last 3 calls
    rests 30 minutes, the limit is 35 s;
  * the verifier asks for `VERDICT: ACCEPT|REVISE` FIRST (so a reply cut by the
    token cap still carries it), the parser reads far more shapes, an unread
    reply gets ONE retry on the next-best verifier with the strict one-line
    contract, the verifier pool is ranked by measured usable-verdict rate;
  * the team gate also accepts MEDIUM build/implement/refactor turns
    (<= 30K tokens, two specialists).

No network: every model call is a stub.
"""
import json
import time

import pytest

import app as A
import verify as V


# --------------------------------------------------------------------------- #
# 4. verdict contract + parser
# --------------------------------------------------------------------------- #

# The shapes the 15 real "no verdict" replies fell into, as recalled, plus the
# new contract itself. (reply, expected ok, expected severity or None)
SHAPES = [
    ("The answer looks correct.", True, None),
    ("VERDICT: ACCEPT", True, None),
    ("**VERDICT:** ACCEPT\nThe edit matches the instruction.", True, None),
    ("Verdict: REVISE\nPROBLEMS:\n- the path is wrong\n- tests were not run\nSEVERITY: high",
     False, "high"),
    ("VERDICT: REVISE - the edit deletes the wrong function", False, "low"),
    ('```json\n{"ok": false, "problems": ["bad import"], "severity": "high"}\n```\n'
     "Hope this helps!", False, "high"),
    ("{'ok': True, 'problems': [], 'severity': 'low'}", True, None),
    ('Here is my review:\n{"ok": true, "problems": [], "severity": "low"}\nLet me know.',
     True, None),
    ("I reviewed the proposal. VERDICT: ACCEPT because it follows the instruction.",
     True, None),
    ("This should be revised because the function name does not match the request.",
     False, "low"),
    ("No issues found.", True, None),
    ("<think>checking the diff...</think>\nVERDICT: ACCEPT", True, None),
    # a reply CUT by the token cap after the verdict line is still readable
    ("VERDICT: REVISE\nPROBLEMS:\n- the command would delete the build dir and the", False, "low"),
    ('{"verdict": "approve", "reason": ""}', True, None),
]

UNREADABLE = [
    "I could not tell.",
    "",
    "Let me analyze the proposal step by step. First, the user asked for a parser and",
    "It looks correct but the import is wrong.",       # approves AND objects
]


@pytest.mark.parametrize("reply,ok,sev", SHAPES)
def test_replayed_shapes_are_read(reply, ok, sev):
    v = V.parse_verdict(reply)
    assert not v.get("unparsed"), reply
    assert v["ok"] is ok
    if sev:
        assert v["severity"] == sev
    if not ok:
        assert v["problems"], "a revise verdict names a problem"


@pytest.mark.parametrize("reply", UNREADABLE)
def test_what_cannot_be_read_stays_unparsed(reply):
    assert V.parse_verdict(reply).get("unparsed") is True


def test_the_usable_rate_on_the_replayed_shapes_is_well_above_sixty_percent():
    allr = [s[0] for s in SHAPES] + UNREADABLE
    usable = sum(1 for r in allr if not V.parse_verdict(r).get("unparsed"))
    assert usable / len(allr) >= 0.75


def test_a_prose_revise_is_never_high_severity():
    v = V.parse_verdict("The proposal is wrong and would break everything, critical failure.")
    assert v["ok"] is False and v["severity"] == "low", "a sentence alone never triggers a corrector"


def test_the_json_form_is_still_accepted():
    v = V.parse_verdict('{"ok": false, "problems": ["x"], "severity": "high"}')
    assert (v["ok"], v["severity"], v["problems"]) == (False, "high", ["x"])


def test_the_contract_asks_for_the_verdict_line_first():
    assert "VERDICT: ACCEPT" in V.VERIFIER_SYSTEM and "VERDICT: REVISE" in V.VERIFIER_SYSTEM
    assert "SEVERITY" in V.VERIFIER_SYSTEM and "PROBLEMS" in V.VERIFIER_SYSTEM
    msgs = V.digest([{"role": "user", "content": "do it"}], {"role": "assistant", "content": "ok"})
    assert msgs[0]["content"] == V.VERIFIER_SYSTEM
    strict = V.digest([{"role": "user", "content": "do it"}],
                      {"role": "assistant", "content": "ok"}, strict=True)
    assert strict[0]["content"] == V.STRICT_VERIFIER_SYSTEM
    assert len(V.STRICT_VERIFIER_SYSTEM) < len(V.VERIFIER_SYSTEM)


# ---- retry on the next verifier ----------------------------------------------

class _Resp:
    def __init__(self, text, status=200):
        self.status_code = status
        self._t = text

    def json(self):
        return {"choices": [{"finish_reason": "stop",
                             "message": {"role": "assistant", "content": self._t}}]}

    def close(self):
        pass


WRITE = {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "write_file",
                                      "arguments": json.dumps({"path": "a.py", "content": "x"})}}]}
MESSAGES = [{"role": "system", "content": "agent"},
            {"role": "user", "content": "build the parser module for the csv files"}]
POOL = [("p2", "m2", 137.0), ("p3", "m3", 136.5), ("p4", "m4", 136.0)]


@pytest.fixture
def verifier(monkeypatch):
    sent = []
    replies = []

    def fake(pid, payload, deadline):
        sent.append((pid, payload["model"], payload["messages"][0]["content"]))
        r = replies[min(len(sent) - 1, len(replies) - 1)]
        return (None, RuntimeError("down")) if r is None else (r if not isinstance(r, str)
                                                                else _Resp(r)), None
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", fake)
    monkeypatch.setattr(A, "_role_candidates", lambda *a, **k: list(POOL))
    monkeypatch.setattr(A, "_bandit_reward", lambda *a, **k: None)
    monkeypatch.setattr(A, "_record_chat_usage", lambda *a, **k: None)
    monkeypatch.setattr(A, "_est_tokens", lambda *a, **k: 500)
    monkeypatch.setattr(A, "_observed_pass", lambda m: False)
    monkeypatch.setattr(A, "_model_is_weak", lambda *a: False)
    monkeypatch.setattr(A, "_swarm_note_member_status", lambda *a, **k: None)
    monkeypatch.setattr(A, "_swarm_note_member_exc", lambda *a, **k: None)
    monkeypatch.setattr(A, "_verify_family", lambda: None)
    return sent, replies


def _verify_once(turn_left=100.0):
    rec = {"calls": 0, "sent_tokens": 0}
    rows = []
    out = A._role_verify_and_correct(
        {"messages": MESSAGES}, MESSAGES, ("p1", "m1"), dict(WRITE), [("p1", "m1")],
        "k", "hard", rec, rows, time.monotonic() + turn_left, 500)
    return out, rec, rows


def test_an_unreadable_reply_is_retried_once_on_the_next_verifier_with_the_strict_contract(verifier):
    sent, replies = verifier
    replies += ["I could not tell.", "VERDICT: ACCEPT"]
    _out, rec, _rows = _verify_once()
    assert [s[0] for s in sent] == ["p2", "p3"]
    assert sent[0][2] == V.VERIFIER_SYSTEM and sent[1][2] == V.STRICT_VERIFIER_SYSTEM
    assert rec["verdict"] == "ok" and rec["verifier"] == "p3/m3"
    assert rec["verifier_unparsed"] == 1 and rec["verifier_retry"] == 1
    assert A._verifier_rate("p2", "m2") < A._verifier_rate("p3", "m3")


def test_a_second_unreadable_reply_fails_open_with_no_third_call(verifier):
    sent, replies = verifier
    replies += ["hmm", "still no idea"]
    out, rec, rows = _verify_once()
    assert out is None and len(sent) == 2
    assert rec["verdict"] == "no verdict (fail-open)"
    assert rec["verifier_unparsed"] == 1 and rec["verifier_retry"] == 1


def test_a_readable_first_reply_is_one_call_and_no_retry(verifier):
    sent, replies = verifier
    replies += ["VERDICT: REVISE\nPROBLEMS:\n- wrong file\nSEVERITY: low"]
    _out, rec, _rows = _verify_once()
    assert len(sent) == 1 and rec["verdict"] == "revise"
    assert not rec.get("verifier_retry") and not rec.get("verifier_unparsed")


def test_a_failed_call_is_not_retried_as_unreadable(verifier):
    sent, replies = verifier
    replies += [_Resp("boom", 500)]
    out, rec, _rows = _verify_once()
    assert out is None and len(sent) == 1 and not rec.get("verifier_retry")


def test_no_retry_without_the_time_for_it(verifier):
    sent, replies = verifier
    replies += ["I could not tell.", "VERDICT: ACCEPT"]
    _out, rec, _rows = _verify_once(turn_left=_verify_left())
    assert len(sent) == 1 and not rec.get("verifier_retry")


def _verify_left():
    return A._VERIFY_MIN_SECONDS + 3.0        # first call fits, the retry (needs +4) does not


def test_free_verdict_retries_an_unreadable_reply_once(verifier, monkeypatch):
    sent, replies = verifier
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("p1", "m1", "medium"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("p1", "m1")])
    replies += ["Let me think about it", "VERDICT: REVISE - the schema lacks a primary key"]
    out = A._free_verdict({"text": "PHASE: schema\n\nOUTPUT\nCREATE TABLE t(a int);",
                           "producer": "p1/m1"})
    assert [s[0] for s in sent] == ["p2", "p3"]
    assert sent[1][2] == V.STRICT_VERIFIER_SYSTEM
    assert out["ok"] is False and out["problems"] == ["the schema lacks a primary key"]


def test_free_verdict_gives_up_after_the_retry(verifier, monkeypatch):
    sent, replies = verifier
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("p1", "m1", "medium"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("p1", "m1")])
    replies += ["??", "!!"]
    assert A._free_verdict({"text": "PHASE: x\n\nOUTPUT\ny", "producer": "p1/m1"}) is None
    assert len(sent) == 2


def test_verifier_pool_prefers_the_measured_usable_rate_inside_the_band():
    for _ in range(4):
        A._verifier_stats_note("p2", "m2", False)
        A._verifier_stats_note("p3", "m3", True)
    pool = A._rank_verifier_pool([("p2", "m2", 137.0), ("p3", "m3", 136.5), ("far", "z", 100.0)])
    pick = V.pick_verifier(("p1", "m1"), pool)
    assert pick == ("p3", "m3"), "same family tier, so the usable-verdict rate decides"
    # family diversity still comes first: a different family beats a better rate
    pool2 = A._rank_verifier_pool([("p2", "kimi-k3", 137.0), ("p3", "gemini-3-flash", 136.5)])
    assert V.pick_verifier(("p1", "kimi-k2"), pool2) == ("p3", "gemini-3-flash")
    # nothing outside the band can win a tie
    assert V.pick_verifier(("p1", "m1"), [("far", "z", 100.0), ("p2", "m2", 137.0),
                                          ("p3", "m3", 136.0)])[0] != "far"


# --------------------------------------------------------------------------- #
# 3. specialists ranked by what they measured
# --------------------------------------------------------------------------- #

def test_the_specialist_limit_is_35_seconds():
    assert A._TEAM_HOP_SECONDS == 35.0


def test_the_rate_has_a_beta_prior():
    assert A._team_rate("p", "m") == pytest.approx(0.5)
    A._team_stats_note("p", "m", "scout", True, 10)
    assert A._team_rate("p", "m") == pytest.approx(2 / 3)
    for _ in range(3):
        A._team_stats_note("q", "n", "scout", False, 35)
    assert A._team_rate("q", "n") == pytest.approx(0.2)


ROWS = [("a", "m1", 138.0), ("b", "m2", 137.5), ("c", "m3", 137.0), ("d", "m4", 136.5),
        ("e", "far", 120.0)]


def test_ranking_is_by_answered_rate_then_latency_inside_the_band_only():
    for _ in range(3):
        A._team_stats_note("a", "m1", "scout", False, 35)     # nvidia-like: 0/3 ... dropped below
    A._team_stats_note("b", "m2", "scout", True, 18)
    A._team_stats_note("c", "m3", "scout", True, 6)
    A._team_stats_note("d", "m4", "scout", True, 9)
    out = A._team_rank(ROWS)
    names = [r[1] for r in out]
    assert "m1" not in names, "failed 3 of 3: rests"
    assert names[:3] == ["m3", "m4", "m2"] or names[:3] == ["m3", "m2", "m4"]
    assert names[0] == "m3", "same rate, quickest first"
    assert names[-1] == "far", "outside the band keeps its place at the back"


def test_an_unmeasured_model_ranks_between_good_and_bad():
    A._team_stats_note("b", "m2", "scout", True, 10)
    A._team_stats_note("b", "m2", "critic", True, 10)
    A._team_stats_note("c", "m3", "scout", False, 35)
    names = [r[1] for r in A._team_rank([("a", "m1", 138.0), ("b", "m2", 137.9),
                                         ("c", "m3", 137.8)])]
    assert names == ["m2", "m1", "m3"]


def test_two_failures_in_the_last_three_rest_for_thirty_minutes():
    now = time.time()
    A._team_stats_note("a", "m", "scout", True, 5, ts=now - 4000)
    A._team_stats_note("a", "m", "scout", False, 35, ts=now - 1000)
    assert not A._team_recently_failing("a", "m", now), "one failure is not enough"
    A._team_stats_note("a", "m", "critic", False, 35, ts=now - 600)
    assert A._team_recently_failing("a", "m", now)
    assert not A._team_recently_failing("a", "m", now + 1300), "30 minutes after the latest failure"
    # a success among the last three: 1 of 3 failed
    A._team_stats_note("b", "m", "scout", False, 35, ts=now - 300)
    A._team_stats_note("b", "m", "scout", True, 5, ts=now - 200)
    A._team_stats_note("b", "m", "scout", True, 5, ts=now - 100)
    assert not A._team_recently_failing("b", "m", now)


def test_the_ledger_is_seeded_from_the_last_rows_of_the_log(tmp_path):
    rows = [{"event": "turn", "ts": time.time() - 100,
             "specialists": [{"role": "scout", "model": "nvidia/moonshotai/kimi-k3",
                              "ok": False, "ms": 25000, "why": "no answer in time"},
                             {"role": "critic", "model": "google/models/gemini-3.6-flash",
                              "ok": True, "ms": 9000, "why": ""}],
             "verifier": "p2/m2", "verdict": "no verdict (fail-open)"},
            {"event": "turn", "ts": time.time() - 50, "verifier": "p3/m3", "verdict": "ok"},
            {"event": "turn", "ts": time.time() - 40, "verifier": "p3/m3", "verdict": "revise"}]
    path = tmp_path / "turn-roles.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n", encoding="utf-8")
    A._team_stats_seed(str(path), force=True)
    assert A._team_rate("nvidia", "moonshotai/kimi-k3") < 0.5
    assert A._team_rate("google", "models/gemini-3.6-flash") > 0.5
    assert A._team_p50("google", "models/gemini-3.6-flash") == pytest.approx(9.0)
    assert A._verifier_rate("p3", "m3") > A._verifier_rate("p2", "m2")


def test_team_pick_uses_the_ranking(monkeypatch):
    cands = [("a", "m1", 138.0), ("b", "m2", 137.8), ("c", "m3", 137.6), ("d", "m4", 137.4)]
    monkeypatch.setattr(A, "_role_candidates", lambda *a, **k: list(cands))
    monkeypatch.setattr(A, "_is_low_quality", lambda m: False)
    monkeypatch.setattr(A, "_verify_family", lambda: (lambda m: m))
    for _ in range(3):
        A._team_stats_note("a", "m1", "scout", False, 35)
    A._team_stats_note("c", "m3", "scout", True, 5)
    picks = A._team_pick([], ("x", "y"), "k", 2)
    assert picks[0] == ("c", "m3") and ("a", "m1") not in picks


def test_a_specialist_call_is_filed_in_the_ledger(monkeypatch):
    def fake_start(i, role, pid, model, digest, deadline, q, est):
        q.put((i, {"ok": role != "critic", "why": "" if role != "critic" else "empty",
                   "text": "- a useful note about the file layout", "secs": 7.0}))

        class _T:
            def cancel(self, *a):
                pass
        return _T()
    monkeypatch.setattr(A, "_team_start", fake_start)
    rec = {"calls": 0, "sent_tokens": 0, "specialists": []}
    A._team_run([{"role": "user", "content": "x"}], None,
                [("scout", "p1", "m1"), ("critic", "p2", "m2")], 5.0, rec, [])
    assert A._team_rate("p1", "m1") > 0.5 and A._team_rate("p2", "m2") < 0.5
    assert A._team_p50("p1", "m1") == pytest.approx(7.0)


# --------------------------------------------------------------------------- #
# 5. the medium gate
# --------------------------------------------------------------------------- #

BUILD_ASKS = [
    "Implement a rate limiter middleware for the Flask app and wire it into every route",
    "Refactor the payment module so the retry logic lives in one place and update the tests",
    "Create a small CLI tool that converts csv files to json and add tests for it",
    "Build a settings page with a dark mode toggle and persist the choice in the config file",
    "Can you migrate the database layer from sqlite3 to SQLAlchemy across the codebase please",
    "Fix the failing import error across all the modules after the package was renamed",
    "Add a new endpoint that returns the user's invoices as pdf and cover it with tests",
]
NOT_BUILD = [
    "How does the retry logic in the payment module decide when to stop retrying?",
    "What is the difference between the two caching layers we have in this project?",
    "Explain why the build is failing on windows but not on linux, please",
    "Rename foo to bar in utils.py",
    "Fix the typo in the README heading about installation steps",
    "Can you tell me which file defines the user model and what fields it has?",
    "build it",
    "ok thanks that works now",
]


@pytest.mark.parametrize("text", BUILD_ASKS)
def test_build_asks_are_medium_team_turns(text):
    assert A._medium_build_ask(text), text


@pytest.mark.parametrize("text", NOT_BUILD)
def test_questions_tiny_edits_and_chatter_are_not(text):
    assert not A._medium_build_ask(text), text


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.setattr(A, "_team_flag_on", lambda: True)
    monkeypatch.setattr(A.lowres, "active", lambda m=None: False)
    monkeypatch.setattr(A, "_classify_difficulty", lambda *a, **k: "medium")
    monkeypatch.setattr(A, "_is_trivial_ask", lambda *a, **k: False)
    monkeypatch.setattr(A, "_team_medium_flag_on", lambda: True)


def _body(text, extra=()):
    return {"messages": [{"role": "system", "content": "agent"},
                         {"role": "user", "content": text}] + list(extra), "tools": []}


def test_a_medium_build_turn_gets_the_team(gate):
    assert A._specialists_wanted(_body(BUILD_ASKS[0]), "k", est=12000, real="medium")


def test_a_medium_question_does_not(gate):
    assert not A._specialists_wanted(_body(NOT_BUILD[0]), "k", est=12000, real="medium")


def test_a_medium_turn_over_thirty_thousand_tokens_does_not(gate):
    assert not A._specialists_wanted(_body(BUILD_ASKS[0]), "k", est=31000, real="medium")
    assert A._specialists_wanted(_body(BUILD_ASKS[0]), "k", est=30000, real="medium")


def test_the_medium_flag_turns_it_off_but_hard_is_unchanged(gate, monkeypatch):
    monkeypatch.setattr(A, "_team_medium_flag_on", lambda: False)
    assert not A._specialists_wanted(_body(BUILD_ASKS[0]), "k", est=12000, real="medium")
    assert A._specialists_wanted(_body(NOT_BUILD[0]), "k", est=12000, real="hard")


def test_a_tool_result_continuation_is_never_a_team_turn(gate):
    extra = [{"role": "assistant", "content": "", "tool_calls": [
        {"id": "c", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c", "content": "file contents"}]
    assert not A._specialists_wanted(_body(BUILD_ASKS[0], extra), "k", est=12000, real="medium")


def test_simple_turns_never_get_the_team(gate):
    assert not A._specialists_wanted(_body(BUILD_ASKS[0]), "k", est=1000, real="simple")


def test_a_medium_turn_runs_at_most_two_specialists(gate, monkeypatch):
    jobs_seen = []

    def fake_run(messages, tools, jobs, deadline, rec, rows):
        jobs_seen.append([j[0] for j in jobs])
        return [(j[0], "- note for %s about the files" % j[0]) for j in jobs]
    monkeypatch.setattr(A, "_team_run", fake_run)
    monkeypatch.setattr(A, "_team_pick", lambda chain, routed, kind, n: [
        ("a", "m1"), ("b", "m2"), ("c", "m3")][:n])
    A._team_cache.clear()
    rec = {"calls": 0, "sent_tokens": 0, "specialists": [], "specialists_ok": 0}
    text = "Create a web landing page website for a bakery with a hero section and pricing table"
    body = _body(text)
    out = A._team_notes_for_turn(body, body["messages"], [("a", "m1")], ("x", "y"), 9000,
                                 "medium", "k", rec, [], time.monotonic() + 200)
    assert jobs_seen == [["scout", "critic"]], "a web ask would add a designer on a hard turn"
    assert rec["team_tier"] == "medium" and rec["team"] == "ran"
    assert any("TEAM NOTES" in str(m.get("content")) for m in out["messages"])
    A._team_cache.clear()


def test_a_hard_turn_is_still_three_specialists_for_a_web_ask(gate, monkeypatch):
    jobs_seen = []
    monkeypatch.setattr(A, "_team_run", lambda m, t, jobs, d, rec, rows:
                        jobs_seen.append([j[0] for j in jobs]) or [])
    monkeypatch.setattr(A, "_team_pick", lambda chain, routed, kind, n: [
        ("a", "m1"), ("b", "m2"), ("c", "m3")][:n])
    A._team_cache.clear()
    rec = {"calls": 0, "sent_tokens": 0, "specialists": [], "specialists_ok": 0}
    body = _body("Create a web landing page website for a bakery with a hero section and pricing")
    A._team_notes_for_turn(body, body["messages"], [("a", "m1")], ("x", "y"), 9000,
                           "hard", "k", rec, [], time.monotonic() + 200)
    assert jobs_seen == [["scout", "designer", "critic"]]
    A._team_cache.clear()
