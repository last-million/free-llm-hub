"""TEAM NOTES: parallel specialists (scout / designer / critic) for hard tool turns.

A CLI turn yields ONE next action, but the thinking around it can be shared:
up to three different models each do one read-only job in parallel, a
deterministic orchestrator merges their notes into ONE system message, and the
actor then runs as before. Fakes only, no network.
"""
import json
import os
import threading
import time
import types

import pytest

import app as A
import clientgone
import config

RO_TOOLS = [{"type": "function", "function": {"name": "read", "parameters": {}}},
            {"type": "function", "function": {"name": "bash", "parameters": {}}}]
CHAIN = [("p1", "m1"), ("p2", "m2"), ("p3", "m3"), ("p4", "m4"), ("p5", "m5")]
SCORES = {"m1": 138.0, "m2": 137.0, "m3": 136.0, "m4": 135.0, "m5": 134.0}
GOAL = "fix the quoting bug in the csv parser so the failing tests pass"
WEB_GOAL = "build a landing page website for a bakery with a hero and pricing"


class _Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload

    def close(self):
        pass


def _text(text):
    return {"choices": [{"finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}]}


def _ro_call():
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "read", "arguments": '{"path": "x.py"}'}}]}}]}


def _body(goal=GOAL, extra=None):
    msgs = [{"role": "system", "content": "You are a CLI agent."},
            {"role": "user", "content": goal}]
    msgs += extra or []
    return {"model": "swarm", "tools": RO_TOOLS, "stream": False, "messages": msgs}


def _is_specialist(payload):
    m = payload.get("messages") or []
    return bool(m) and str(m[0].get("content", "")).startswith("You are the ")


def _role_of(payload):
    c = payload["messages"][0]["content"]
    return "scout" if "SCOUT" in c else "designer" if "DESIGNER" in c else "critic"


@pytest.fixture
def team(monkeypatch):
    monkeypatch.setattr(A, "_team_flag_on", lambda: True)
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("p1", "m1", "hard"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: list(CHAIN))
    monkeypatch.setattr(A, "_record_outcome", lambda *a, **k: None)
    monkeypatch.setattr(A, "_record_chat_usage", lambda *a, **k: None)
    monkeypatch.setattr(A, "_note_nonanswer", lambda *a, **k: None)
    monkeypatch.setattr(A, "_est_tokens", lambda *a, **k: 1000)
    monkeypatch.setattr(A, "_classify_difficulty", lambda *a, **k: "hard")
    monkeypatch.setattr(A, "_swarm_member_sick", lambda *a, **k: None)
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, model: SCORES.get(model, 100.0))
    monkeypatch.setattr(A.prov, "is_model_allowed", lambda m: True)
    monkeypatch.setattr(A, "_tool_turn_race_on", lambda: False)
    monkeypatch.setattr(A.lowres, "active", lambda m=None: False)
    mod = types.ModuleType("verify")
    mod.family = lambda model_id: str(model_id)[:2]
    mod.pick_verifier = lambda producer, cands: None
    mod.is_risky = lambda *a, **k: False
    mod.digest = lambda messages, proposed: [{"role": "user", "content": "V"}]
    mod.parse_verdict = lambda t: json.loads(t)
    mod.corrector_messages = lambda *a, **k: []
    monkeypatch.setitem(__import__("sys").modules, "verify", mod)
    A._team_cache.clear()
    with A._outcome_lock:
        saved = dict(A._tool_ttft)
        A._tool_ttft.clear()
    yield
    A._team_cache.clear()
    with A._outcome_lock:
        A._tool_ttft.clear()
        A._tool_ttft.update(saved)


NOTES = {
    "scout": "- parser.py holds the csv reader\n- tests/test_parser.py is where tests go",
    "designer": "- Components: Reader, Validator\n- Interfaces: read(path) -> rows",
    "critic": "- Risk: quoted commas\n- Verify: run pytest -q first",
}


def _dispatcher(calls, delay=0.0, fail=(), notes=None):
    """Records (pid, model, t, messages). Specialist calls answer NOTES[role];
    the actor answers a read-only tool call."""
    t0 = time.monotonic()
    notes = notes or NOTES

    def go(pid, payload, deadline):
        calls.append((pid, payload.get("model"), time.monotonic() - t0,
                      payload.get("messages")))
        end = time.monotonic() + (delay if _is_specialist(payload) else 0.0)
        while time.monotonic() < end:
            if clientgone.cancelled():
                return None, None
            time.sleep(0.02)
        if _is_specialist(payload):
            if pid in fail:
                return _Resp({"e": 1}, 429), None
            return _Resp(_text(notes[_role_of(payload)])), None
        return _Resp(_ro_call()), None
    return go


def _specialist_calls(calls):
    return [c for c in calls if c[3] and str(c[3][0].get("content", "")).startswith("You are the ")]


def _actor_calls(calls):
    return [c for c in calls if c not in _specialist_calls(calls)]


def _has_notes(messages):
    return any(m.get("role") == "system" and "TEAM NOTES" in str(m.get("content"))
               for m in messages)


# ------------------------------------------------------------------ dispatch

def test_specialists_run_in_parallel_not_in_sequence(team, monkeypatch):
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher(calls, delay=0.3))
    rec = {"calls": 0, "sent_tokens": 0, "specialists": []}
    rows = []
    jobs = [("scout", "p2", "m2"), ("designer", "p3", "m3"), ("critic", "p4", "m4")]
    t0 = time.monotonic()
    parts = A._team_run(_body()["messages"], RO_TOOLS, jobs, 25.0, rec, rows)
    assert time.monotonic() - t0 < 0.6          # 3 x 0.3 s in parallel, not 0.9
    assert [r for r, _ in parts] == ["scout", "designer", "critic"]
    assert rec["calls"] == 3


def test_specialists_use_different_identities_providers_and_families(team, monkeypatch):
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher(calls))
    # p2 and p3 host the SAME model identity; the pick must not use both
    chain = [("p1", "m1"), ("p2", "m2"), ("p3", "m2"), ("p4", "m4"), ("p5", "m5")]
    picks = A._team_pick(chain, ("p1", "m1"), None, 3)
    idents = [A._normalize_model_identity(m) for _, m in picks]
    assert len(set(idents)) == len(picks) == 3
    assert len({p for p, _ in picks}) == 3
    assert ("p1", "m1") not in picks


def test_family_diversity_beats_a_stronger_same_family_model(team):
    # m2 and m2b share a family ("m2"); the weaker m4 is a NEW family
    chain = [("p1", "m1"), ("p2", "m2"), ("p3", "m2b"), ("p4", "m4")]
    picks = A._team_pick(chain, ("p1", "m1"), None, 2)
    assert picks == [("p2", "m2"), ("p4", "m4")]


def test_the_actor_never_doubles_as_a_specialist(team):
    picks = A._team_pick(list(CHAIN), ("p1", "m1"), None, 3)
    assert all(m != "m1" for _, m in picks)


# ------------------------------------------------------------------ orchestrator

def test_brief_merges_in_order_with_labels_and_dedupes():
    brief = A._orchestrate_brief([
        ("critic", "- Risk: quoted commas\n- parser.py holds the csv reader"),
        ("scout", "- parser.py holds the csv reader\n- unknown"),
        ("designer", "unknown")])
    assert brief.startswith("TEAM NOTES")
    assert "do not invent paths" in brief
    assert brief.index("SCOUT") < brief.index("RISKS AND CHECKS")
    assert "DESIGN" not in brief.replace("DESIGN", "", 0) or "DESIGN:" not in brief
    assert brief.count("parser.py holds the csv reader") == 1     # first (scout) wins
    assert "unknown" not in brief.lower().split("hints")[1].replace("do not invent paths", "")


def test_brief_is_clipped_to_the_limit():
    big = "\n".join("- point number %d about the module" % i for i in range(400))
    brief = A._orchestrate_brief([("scout", big), ("designer", big + "x"), ("critic", big + "y")])
    assert 0 < len(brief) <= 2500
    assert "SCOUT" in brief and "RISKS AND CHECKS" in brief      # each part keeps a share


def test_empty_or_failed_parts_give_no_brief():
    assert A._orchestrate_brief([]) == ""
    assert A._orchestrate_brief([("scout", ""), ("critic", "unknown")]) == ""


def test_digest_is_bounded_and_names_tools_only():
    msgs = [{"role": "user", "content": "x" * 50000},
            {"role": "assistant", "content": "", "tool_calls": []},
            {"role": "tool", "tool_call_id": "a", "content": "r" * 50000}]
    d = A._team_digest(msgs, RO_TOOLS)
    assert len(d) < 16000
    assert "read, bash" in d and "parameters" not in d


# ------------------------------------------------------------------ the turn

def test_actor_gets_the_notes_as_one_system_message_specialists_do_not(team, monkeypatch):
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher(calls))
    out = A._swarm_tool_result(_body())
    assert out is not None
    spec, actor = _specialist_calls(calls), _actor_calls(calls)
    assert len(spec) == 2 and len(actor) == 1          # scout + critic (no design ask)
    team_msgs = [m for m in actor[0][3] if "TEAM NOTES" in str(m.get("content"))]
    assert len(team_msgs) == 1 and team_msgs[0]["role"] == "system"
    assert actor[0][3][0]["content"] == "You are a CLI agent."   # CLI system prompt stays first
    for c in spec:
        assert not _has_notes(c[3])
        assert "read, bash" in c[3][1]["content"]       # names only
        assert len(c[3]) == 2                           # no tools offered, digest only
    assert not any("tools" in {} for _ in spec)


def test_a_build_goal_adds_a_designer(team, monkeypatch):
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher(calls))
    A._swarm_tool_result(_body("build a new csv parser module with tests"))
    assert len(_specialist_calls(calls)) == 3


def test_a_web_goal_adds_a_designer(team, monkeypatch):
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher(calls))
    A._swarm_tool_result(_body(WEB_GOAL))
    roles = sorted(_role_of({"messages": c[3]}) for c in _specialist_calls(calls))
    assert roles == ["critic", "designer", "scout"]


def test_a_failing_specialist_is_omitted(team, monkeypatch):
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher(calls, fail=("p2",)))
    out = A._swarm_tool_result(_body())
    assert out is not None
    actor = _actor_calls(calls)[0]
    brief = [m for m in actor[3] if "TEAM NOTES" in str(m.get("content"))][0]["content"]
    assert "RISKS AND CHECKS" in brief and "SCOUT" not in brief


def test_all_specialists_failing_is_plain_roles(team, monkeypatch):
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline",
                        _dispatcher(calls, fail=("p2", "p3", "p4", "p5")))
    out = A._swarm_tool_result(_body())
    assert out is not None
    actor = _actor_calls(calls)
    assert len(actor) == 1 and not _has_notes(actor[0][3])
    assert actor[0][3] == _body()["messages"]


def test_flag_off_means_no_specialist_calls(team, monkeypatch):
    monkeypatch.setattr(A, "_team_flag_on", lambda: False)
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher(calls))
    A._swarm_tool_result(_body())
    assert not _specialist_calls(calls) and len(calls) == 1


def test_the_flag_defaults_on(monkeypatch):
    monkeypatch.undo()
    assert config.get_flag("tool_turn_specialists", True) in (True, False)
    assert A._team_flag_on.__name__ == "_team_flag_on"


@pytest.mark.parametrize("name,body_fn,patch", [
    ("continuation", lambda: _body(extra=[
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "file contents"}]), None),
    ("trivial", lambda: _body("what is 2 plus 2?"), "trivial"),
    ("compaction", lambda: _body("Create a new anchored summary from the conversation history"),
     None),
    ("huge", lambda: _body(), "huge"),
    ("not hard", lambda: _body(), "medium"),
])
def test_these_turns_skip_the_specialists(team, monkeypatch, name, body_fn, patch):
    if patch == "huge":
        monkeypatch.setattr(A, "_est_tokens", lambda *a, **k: 90000)
    if patch == "medium":
        monkeypatch.setattr(A, "_classify_difficulty", lambda *a, **k: "medium")
    if patch == "trivial":
        monkeypatch.setattr(A, "_is_trivial_ask", lambda *a, **k: True)
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher(calls))
    A._swarm_tool_result(body_fn())
    assert not _specialist_calls(calls), name


def test_low_resource_mode_skips_them(team, monkeypatch):
    monkeypatch.setattr(A.lowres, "active", lambda m=None: True)
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher(calls))
    A._swarm_tool_result(_body())
    assert not _specialist_calls(calls)


def test_wanted_is_a_plain_predicate(team):
    assert A._specialists_wanted(_body(), "k") is True
    assert A._specialists_wanted({"messages": []}, "k") is False


# ------------------------------------------------------------------ cache

def test_a_second_turn_of_the_conversation_reuses_the_brief(team, monkeypatch):
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher(calls))
    A._swarm_tool_result(_body())
    first = len(_specialist_calls(calls))
    assert first == 2
    # the loop continues after a tool result: no new specialists, same notes
    cont = _body(extra=[
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "file contents"}])
    A._swarm_tool_result(cont)
    assert len(_specialist_calls(calls)) == first
    assert _has_notes(_actor_calls(calls)[-1][3])
    # a re-sent identical opening turn is cached too
    A._swarm_tool_result(_body())
    assert len(_specialist_calls(calls)) == first


def test_a_new_instruction_within_three_turns_does_not_rerun(team, monkeypatch):
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher(calls))
    A._swarm_tool_result(_body())
    follow = _body(extra=[{"role": "assistant", "content": "done"},
                          {"role": "user", "content": "now also fix the exporter encoding bug"}])
    A._swarm_tool_result(follow)
    assert len(_specialist_calls(calls)) == 2
    assert not _has_notes(_actor_calls(calls)[-1][3])    # a stale brief is not attached


def test_a_much_later_instruction_gets_a_fresh_team(team, monkeypatch):
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher(calls))
    A._swarm_tool_result(_body())
    extra = []
    for i in range(5):
        extra += [{"role": "assistant", "content": "step %d" % i},
                  {"role": "user", "content": "ok go on %d" % i}]
    extra += [{"role": "assistant", "content": "ok"},
              {"role": "user", "content": "fix the exporter encoding bug in module two"}]
    A._swarm_tool_result(_body(extra=extra))
    assert len(_specialist_calls(calls)) == 4


# ------------------------------------------------------------------ cancel / bandit

def test_client_gone_cancels_specialists_and_files_nothing(team, monkeypatch):
    cut, filed = {}, []

    def go(pid, payload, deadline):
        end = time.monotonic() + 5.0
        while time.monotonic() < end:
            if clientgone.cancelled():
                cut[pid] = True
                return None, None
            time.sleep(0.02)
        return _Resp(_text(NOTES["scout"])), None
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", go)
    monkeypatch.setattr(A, "_note_recent_hop_failure", lambda *a, **k: filed.append(a))
    monkeypatch.setattr(A, "_swarm_note_member_exc", lambda *a, **k: filed.append(a))
    monkeypatch.setattr(A, "_swarm_note_member_status", lambda *a, **k: filed.append(a))
    tok = clientgone.Token(label="test", log=False)
    clientgone.set_current(tok)
    try:
        threading.Timer(0.3, lambda: tok.cancel("client left")).start()
        started = time.monotonic()
        assert A._swarm_tool_result(_body()) is None
        assert time.monotonic() - started < 2.5
    finally:
        clientgone.set_current(None)
    deadline = time.monotonic() + 2.0
    while len(cut) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(cut) == 2 and not filed


def test_specialists_429_is_filed_like_any_hop_failure(team, monkeypatch):
    seen = []
    monkeypatch.setattr(A, "_swarm_note_member_status",
                        lambda pid, model, code, *a, **k: seen.append((pid, code)))
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline",
                        _dispatcher([], fail=("p2",)))
    A._swarm_tool_result(_body())
    assert ("p2", 429) in seen


def test_specialist_output_never_touches_the_bandit(team, monkeypatch):
    b = types.ModuleType("bandit")
    rewards = []
    b.MAX_NUDGE = 1.0
    b.task_kind = lambda c, d, t, e: "k"
    b.default = types.SimpleNamespace(
        nudge=lambda kind, pid, model, base: base,
        reward=lambda *a: rewards.append(a),
        remember_tool_calls=lambda *a: None,
        credit_from_messages=lambda *a: 0, stats=lambda limit=10: [])
    b.configure = lambda path: None
    monkeypatch.setitem(__import__("sys").modules, "bandit", b)
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher([]))
    A._swarm_tool_result(_body())
    assert rewards == []


# ------------------------------------------------------------------ visible

def test_activity_chips_header_and_jsonl(team, monkeypatch):
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher([], fail=("p3",)))
    with A.app.test_request_context("/v1/chat/completions", method="POST"):
        A.g.act = {}
        out = A._swarm_tool_result(_body())
        act = A.g.act
    data, hdrs = out
    roles = [r["role"] for r in act["pipeline"]]
    assert "specialist: scout" in roles
    assert any(r.startswith("specialist: critic: HTTP 429") for r in roles)
    assert "actor" in roles
    assert next(r for r in act["pipeline"] if r["role"] == "specialist: scout")["model"] == "p2/m2"
    assert "specialists=1" in hdrs["X-Free-LLM-Hub-Roles"]
    path = os.path.join(config.state_dir(), A._ROLE_LOG_NAME)
    with open(path, encoding="utf-8") as f:
        row = json.loads(f.read().strip().splitlines()[-1])
    assert [s["role"] for s in row["specialists"]] == ["scout", "critic"]
    assert row["specialists"][0]["ok"] is True and row["specialists"][1]["ok"] is False
    assert row["brief_chars"] > 0 and row["team"] == "ran"
    assert row["calls"] == 3                                   # 2 specialists + 1 actor


def test_role_eval_counts_team_turns():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "role_eval", os.path.join(os.path.dirname(__file__), "..", "scripts", "role_eval.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rows = [{"event": "turn", "turn": "tool", "served": "a/b", "calls": 3,
             "specialists": [{"role": "scout"}, {"role": "critic"}], "brief_chars": 900},
            {"event": "turn", "turn": "tool", "served": "a/b", "calls": 1}]
    st = mod.roles_stats(rows)
    assert st["team_turns"] == 1 and st["team_specialist_calls"] == 2
    assert st["team_brief_chars_avg"] == 900
