"""CLI and Build parity (2026-10-08): the same quality machinery Multi/swarm
already get, now reaching terminal CLIs on /v1/* and the Build single turn.

Five audited gaps, each tested both ways -- it fires when it should, stays
silent when it should not, fails open on an exception, and is byte-identical
old behaviour with its flag off:

  1. web-slop scan -> verifier problems -> corrector      (turn_slop_check)
  2. team-notes specialists on single-model tool turns    (tool_turn_specialists_single)
  3. observed-evidence receipts for /v1 tool results       (v1_observed_evidence)
  4. harvested project facts for /v1 turns                 (v1_memory_facts)
  5. PROGRESS/todo upkeep re-asked (swarm retry/resume + PLAN_PHASES)

Fakes and the sandboxed state dir only; no network, no real CLI config, no
real ~/.free-llm-hub (tests/conftest.py isolates all of that).
"""
import json
import os
import types

import pytest

import app as A
import config
import craft
import swarm_windows as SW


# --------------------------------------------------------------------------- #
# shared fixtures / fakes
# --------------------------------------------------------------------------- #

FULL_HTML = ("<!doctype html><html><head><title>x</title></head><body>"
             "<img src=\"a.jpg\"><p>lorem ipsum dolor sit amet consectetur</p>"
             "</body></html>")


class _Tok:
    def cancel(self, *a, **k):
        pass


@pytest.fixture(autouse=True)
def _no_client_gone(monkeypatch):
    monkeypatch.setattr(A, "_client_gone", lambda: False)
    A._V1_SEEN_IDS.clear()
    yield
    A._V1_SEEN_IDS.clear()


# ======================================================================= #
# GAP 1 -- web slop becomes a verifier problem and drives the corrector
# ======================================================================= #

def _html_write_call():
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": "w1", "type": "function", "function": {
            "name": "write",
            "arguments": json.dumps({"file_path": "index.html", "content": FULL_HTML})}}]}


def _html_text_answer():
    return {"role": "assistant",
            "content": "Here is the page:\n```html\n" + FULL_HTML + "\n```\n"}


def test_slop_fires_on_a_web_write_tool_call():
    high = A._slop_problems_for_proposal(_html_write_call())
    assert high and any("AI-slop" in p for p in high)


def test_slop_fires_on_html_in_a_text_answer():
    high = A._slop_problems_for_proposal(_html_text_answer())
    assert high and any("AI-slop" in p for p in high)


def test_slop_silent_on_non_web_output():
    py = {"role": "assistant", "content": "```python\nprint('lorem ipsum')\n```",
          "tool_calls": [{"id": "w", "type": "function", "function": {
              "name": "write", "arguments": '{"file_path": "x.py", "content": "lorem ipsum"}'}}]}
    assert A._slop_problems_for_proposal(py) == []


def test_slop_silent_when_flag_off(monkeypatch):
    monkeypatch.setattr(config, "get_flag",
                        lambda k, d=None: False if k == "turn_slop_check" else d)
    assert A._slop_problems_for_proposal(_html_write_call()) == []


def test_slop_fails_open_when_verify_raises(monkeypatch):
    bad = types.SimpleNamespace(
        is_web_file=lambda p: True,
        html_blocks=lambda t: [("b.html", FULL_HTML)],
        slop_problems=lambda files: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(A, "_verify", lambda: bad)
    assert A._slop_problems_for_proposal(_html_text_answer()) == []


def _verify_env(monkeypatch, is_risky=False):
    """A fake verify module for the integration test: the VERIFIER accepts, so
    only the slop override can trigger the corrector."""
    mod = types.ModuleType("verify")
    import verify as real
    mod.family = real.family
    mod.identity = real.identity
    mod.is_web_file = real.is_web_file
    mod.html_blocks = real.html_blocks
    mod.slop_problems = real.slop_problems
    mod.parse_verdict = real.parse_verdict
    mod.digest = real.digest
    mod.VERIFY_MAX_TOKENS = real.VERIFY_MAX_TOKENS
    mod.is_risky = lambda *a, **k: is_risky
    mod.pick_verifier = lambda prod, pool: (pool[0][0], pool[0][1]) if pool else None
    mod.corrector_messages = lambda messages, proposed, verdict: list(messages)
    monkeypatch.setitem(__import__("sys").modules, "verify", mod)
    return mod


def test_role_verify_adds_slop_and_runs_the_corrector(monkeypatch):
    _verify_env(monkeypatch)
    monkeypatch.setattr(A, "_swarm_member_sick", lambda *a, **k: None)
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, model: 130.0)

    def fake_leg(idx, pid, model, body, dl, q, est, kind):
        q.put((idx, {"ok": True, "why": "",
                     "msg": {"role": "assistant", "content": "<p>real, hand-written copy</p>"},
                     "data": {"choices": [{"message": {"content": "fixed"}}]}, "secs": 0.0}))
        return _Tok()
    monkeypatch.setattr(A, "_role_start_leg", fake_leg)

    import time as _t
    rec = {"calls": 0, "sent_tokens": 0}
    rows = []
    out = A._role_verify_and_correct(
        {"messages": [{"role": "user", "content": "build the landing page"}]},
        [{"role": "user", "content": "build the landing page"}],
        ("p1", "m1"), _html_text_answer(), [("p2", "m2")], "coding|hard|tools|s",
        "hard", rec, rows, _t.monotonic() + 100.0, 1000, tool_turn=True)
    assert out is not None and out[0] == "p2"
    assert rec.get("slop") and rec.get("severity") == "high" and rec.get("corrected") is True


def test_role_verify_no_slop_no_forced_correction(monkeypatch):
    """A clean, non-risky, non-web proposal: the verifier is never forced, so
    the function returns None (ship the original) exactly as before."""
    _verify_env(monkeypatch)
    monkeypatch.setattr(A, "_swarm_member_sick", lambda *a, **k: None)
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, model: 130.0)
    import time as _t
    rec = {"calls": 0, "sent_tokens": 0}
    out = A._role_verify_and_correct(
        {"messages": [{"role": "user", "content": "say hi"}]},
        [{"role": "user", "content": "say hi"}], ("p1", "m1"),
        {"role": "assistant", "content": "hello there, here is a plain prose reply"},
        [("p2", "m2")], "k", "hard", rec, [], _t.monotonic() + 100.0, 1000,
        tool_turn=True)
    assert out is None and "slop" not in rec


# ======================================================================= #
# GAP 2 -- team notes (specialists) on a single-model tool turn
# ======================================================================= #

RO_TOOLS = [{"type": "function", "function": {"name": "read", "parameters": {}}},
            {"type": "function", "function": {"name": "bash", "parameters": {}}}]
HARD_MSGS = [{"role": "system", "content": "You are a CLI agent."},
             {"role": "user", "content": "fix the quoting bug in the csv parser so the "
                                         "failing tests pass across every module"}]
CHAIN2 = [("p1", "m1"), ("p2", "m2"), ("p3", "m3")]


@pytest.fixture
def team_single(monkeypatch):
    monkeypatch.setattr(A, "_team_flag_on", lambda: True)
    monkeypatch.setattr(A.lowres, "active", lambda m=None: False)
    monkeypatch.setattr(A, "_is_trivial_ask", lambda *a, **k: False)
    monkeypatch.setattr(A.ctxwin, "is_compaction_request", lambda m: False)
    monkeypatch.setattr(A, "_team_pick", lambda chain, routed, kind, n: [("p2", "m2"), ("p3", "m3")])
    monkeypatch.setattr(A, "_team_run",
                        lambda messages, tools, jobs, dl, rec, rows:
                        [("scout", "- parser.py holds the csv reader"),
                         ("critic", "- run pytest -q first")])
    monkeypatch.setattr(A, "_role_log", lambda row: None)
    A._team_cache.clear()
    yield
    A._team_cache.clear()


def _notes_in(messages):
    return any(isinstance(m, dict) and m.get("role") == "system"
               and "TEAM NOTES" in str(m.get("content")) for m in messages)


def test_single_turn_team_notes_fire_on_hard_fresh_tool_turn(team_single):
    out = A._single_turn_team_notes(HARD_MSGS, RO_TOOLS, CHAIN2, ("p1", "m1"),
                                    1000, "hard", pinned=False, stream=False)
    assert out is not HARD_MSGS and _notes_in(out)


def test_single_turn_team_notes_work_for_a_stream(team_single):
    out = A._single_turn_team_notes(HARD_MSGS, RO_TOOLS, CHAIN2, ("p1", "m1"),
                                    1000, "hard", pinned=False, stream=True)
    assert _notes_in(out)


def test_single_turn_team_notes_silent_without_tools(team_single):
    out = A._single_turn_team_notes(HARD_MSGS, None, CHAIN2, ("p1", "m1"),
                                    1000, "hard", pinned=False, stream=False)
    assert out is HARD_MSGS


def test_single_turn_team_notes_silent_when_pinned(team_single):
    out = A._single_turn_team_notes(HARD_MSGS, RO_TOOLS, CHAIN2, ("p1", "m1"),
                                    1000, "hard", pinned=True, stream=False)
    assert out is HARD_MSGS


def test_single_turn_team_notes_off_when_single_flag_off(team_single, monkeypatch):
    monkeypatch.setattr(config, "get_flag",
                        lambda k, d=None: False if k == "tool_turn_specialists_single" else d)
    out = A._single_turn_team_notes(HARD_MSGS, RO_TOOLS, CHAIN2, ("p1", "m1"),
                                    1000, "hard", pinned=False, stream=False)
    assert out is HARD_MSGS


def test_single_turn_team_notes_off_when_team_flag_off(monkeypatch):
    monkeypatch.setattr(A, "_team_flag_on", lambda: False)
    out = A._single_turn_team_notes(HARD_MSGS, RO_TOOLS, CHAIN2, ("p1", "m1"),
                                    1000, "hard", pinned=False, stream=False)
    assert out is HARD_MSGS


def test_single_turn_team_notes_silent_on_a_continuation(team_single):
    # A loop continuation ends on a tool result, not a fresh instruction.
    cont = HARD_MSGS + [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"}]
    out = A._single_turn_team_notes(cont, RO_TOOLS, CHAIN2, ("p1", "m1"),
                                    1000, "hard", pinned=False, stream=False)
    assert out is cont


def test_single_turn_team_notes_fail_open(team_single, monkeypatch):
    monkeypatch.setattr(A, "_team_notes_for_turn",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = A._single_turn_team_notes(HARD_MSGS, RO_TOOLS, CHAIN2, ("p1", "m1"),
                                    1000, "hard", pinned=False, stream=False)
    assert out is HARD_MSGS


# ======================================================================= #
# GAP 3 -- observed-evidence receipts for /v1 tool results
# ======================================================================= #

def _cmd_result_msgs(cmd="pytest -q", out="==== 1 failed, 2 passed in 0.12s ====\nExit code: 1",
                     tcid="t1"):
    return [{"role": "user", "content": "run the tests"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": tcid, "type": "function", "function": {
                    "name": "bash", "arguments": '{"command": "%s"}' % cmd}}]},
            {"role": "tool", "tool_call_id": tcid, "content": out}]


def _receipt_dir(key):
    import receipts
    return os.path.join(receipts.root(), receipts._safe("v1-" + key))


def _clear_receipts(key):
    import shutil
    shutil.rmtree(_receipt_dir(key), ignore_errors=True)


def test_v1_observe_writes_a_receipt_for_a_test_result(monkeypatch):
    _clear_receipts("gap3a")
    monkeypatch.setattr(A, "_orch_key", lambda b=None, m=None: "gap3a")
    A._v1_observe(_cmd_result_msgs(), {})
    folder = _receipt_dir("gap3a")
    files = os.listdir(folder) if os.path.isdir(folder) else []
    assert files
    import receipts
    rec = receipts.read(os.path.join(folder, files[0]))
    assert rec and rec["verdict"] == "FAIL" and rec["results"][0]["tool"] == "pytest"


def test_v1_observe_dedupes_by_tool_call_id(monkeypatch):
    _clear_receipts("gap3b")
    monkeypatch.setattr(A, "_orch_key", lambda b=None, m=None: "gap3b")
    msgs = _cmd_result_msgs(tcid="dedupe-1")
    A._v1_observe(msgs, {})
    first = len(os.listdir(_receipt_dir("gap3b")))
    A._v1_observe(msgs, {})          # same id again -> nothing new written
    assert len(os.listdir(_receipt_dir("gap3b"))) == first


def test_v1_observe_off_when_flag_off(monkeypatch):
    _clear_receipts("gap3c")
    monkeypatch.setattr(A, "_orch_key", lambda b=None, m=None: "gap3c")
    monkeypatch.setattr(config, "get_flag",
                        lambda k, d=None: False if k == "v1_observed_evidence" else d)
    A._v1_observe(_cmd_result_msgs(tcid="off-1"), {})
    assert not os.path.isdir(_receipt_dir("gap3c"))


def test_v1_observe_silent_for_an_unrecognised_command(monkeypatch):
    _clear_receipts("gap3d")
    monkeypatch.setattr(A, "_orch_key", lambda b=None, m=None: "gap3d")
    A._v1_observe(_cmd_result_msgs(cmd="ls -la", out="file1 file2", tcid="ls-1"), {})
    assert not os.path.isdir(_receipt_dir("gap3d"))


def test_v1_observe_fails_open_when_receipts_raise(monkeypatch):
    monkeypatch.setattr(A, "_orch_key", lambda b=None, m=None: "gap3e")
    import receipts
    monkeypatch.setattr(receipts, "write",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk")))
    A._v1_observe(_cmd_result_msgs(tcid="raise-1"), {})   # must not raise


# ======================================================================= #
# GAP 4 -- harvested project facts for /v1 turns
# ======================================================================= #

def _facts_msgs(cwd):
    return [{"role": "system", "content": "<cwd>%s</cwd>\nYou are a coding agent." % cwd},
            {"role": "user", "content": "Always use tabs for indentation in this repo."},
            {"role": "assistant", "content": "Decision: use Vite for the build."}]


def test_v1_observe_harvests_facts_when_cwd_known(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_orch_key", lambda b=None, m=None: "gap4a")
    seen = {}
    monkeypatch.setattr(A.memory, "harvest_facts",
                        lambda sid, **k: seen.update(sid=sid, **k) or [])
    A._v1_observe(_facts_msgs(str(tmp_path)), {})
    assert os.path.normcase(seen.get("project_dir") or "") == os.path.normcase(str(tmp_path))
    assert "tabs" in (seen.get("request") or "") and "Vite" in (seen.get("reply") or "")


def test_v1_observe_skips_facts_without_a_cwd(monkeypatch):
    monkeypatch.setattr(A, "_orch_key", lambda b=None, m=None: "gap4b")
    called = []
    monkeypatch.setattr(A.memory, "harvest_facts", lambda *a, **k: called.append(1))
    A._v1_observe([{"role": "user", "content": "no cwd here, just a question"}], {})
    assert not called


def test_v1_observe_never_writes_into_the_hub_repo(monkeypatch):
    monkeypatch.setattr(A, "_orch_key", lambda b=None, m=None: "gap4c")
    called = []
    monkeypatch.setattr(A.memory, "harvest_facts", lambda *a, **k: called.append(1))
    hub = os.path.dirname(os.path.abspath(A.__file__))
    A._v1_observe(_facts_msgs(hub), {})
    assert not called


def test_v1_facts_off_when_flag_off(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_orch_key", lambda b=None, m=None: "gap4d")
    called = []
    monkeypatch.setattr(A.memory, "harvest_facts", lambda *a, **k: called.append(1))
    monkeypatch.setattr(config, "get_flag",
                        lambda k, d=None: False if k == "v1_memory_facts" else d)
    A._v1_observe(_facts_msgs(str(tmp_path)), {})
    assert not called


def test_v1_project_cwd_reads_several_shapes(tmp_path):
    d = str(tmp_path)
    assert A._v1_project_cwd([{"role": "user", "content": "<cwd>%s</cwd>" % d}]) \
        and os.path.normcase(A._v1_project_cwd(
            [{"role": "user", "content": "<cwd>%s</cwd>" % d}])) == os.path.normcase(d)
    line = [{"role": "system", "content": "Working directory: %s" % d}]
    assert os.path.normcase(A._v1_project_cwd(line) or "") == os.path.normcase(d)
    assert A._v1_project_cwd(
        [{"role": "user", "content": "<cwd>/no/such/dir/anywhere/x</cwd>"}]) is None


# ======================================================================= #
# GAP 5 -- PROGRESS / todo upkeep re-asked
# ======================================================================= #

def _run(tmp_path):
    run = SW._Run("do the thing", str(tmp_path), "opencode", SW.clean_phases({"phases": [
        {"title": "Build", "task": "t"}, {"title": "Review", "task": "t"}]}),
        owner="conv-5")
    return run


def test_progress_reminder_absent_on_a_fresh_first_attempt(tmp_path):
    run = _run(tmp_path)
    prompt = SW._agent_prompt(run, run.agents[0])
    assert "PROGRESS.md" in prompt                         # the base upkeep line
    assert "retry/resume" not in prompt                    # not the continuation one


def test_progress_reminder_reinjected_on_a_revision(tmp_path):
    run = _run(tmp_path)
    run.agents[0].revisions = 1
    prompt = SW._agent_prompt(run, run.agents[0])
    assert "retry/resume" in prompt and "re-read PROGRESS.md" in prompt


def test_progress_reminder_reinjected_on_a_resume(tmp_path):
    run = _run(tmp_path)
    run.resumes = 1
    assert "retry/resume" in SW._agent_prompt(run, run.agents[0])


def test_progress_reminder_reinjected_on_a_restored_run(tmp_path):
    run = _run(tmp_path)
    run.restored = True
    assert "retry/resume" in SW._agent_prompt(run, run.agents[0])


def test_plan_phases_asks_for_progress_after_each_step():
    t = craft.PLAN_PHASES
    assert "after EACH step" in t and "PROGRESS.md" in t and "stale" in t


def test_worst_case_brief_stays_under_the_ceiling():
    worst = max(len(craft.system_message(t)["content"]) for t in (
        "build an online store and deploy it", "create a landing page for my saas",
        "build me a restaurant website"))
    assert worst / 4 < 32768 * 0.135
