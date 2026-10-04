"""Observed evidence, not claims (owner-approved 2026-10-04).

  1. the three stream parsers emit a `tool_result` event (command, exit code,
     is_error, output tail) beside their unchanged events -- from event lines
     in the shapes the installed CLIs really print;
  2. memory files a "verified command" ONLY when its last observed run PASSED;
  3. Multi: an observed FAIL is a problem (one revision, no manager cost), a
     summary claiming a pass with none observed is "claimed, not checked";
  4. honest labels: verified = an observed PASS, reviewed = a manager read;
  5. receipts: one JSON file per phase / per turn, LRU-pruned.
"""
import hashlib
import json
import os
import time

import pytest

import agentic_chat as ac
import evidence as E
import memory
import receipts
import swarm_windows as SW

# --------------------------------------------------------------------------- #
# Event lines, in the shapes each CLI prints
# --------------------------------------------------------------------------- #

# codex 0.154 `exec --json`: CommandExecutionItem {command, aggregated_output,
# exit_code, status} (codex-rs/exec/src/exec_events.rs); command = argv
# shlex-joined, argv[0] = the Windows shell (a real rollout's argv, 2026-09-27).
_PWSH = ("'C:\\Program Files\\WindowsApps\\Microsoft.PowerShell_7.6.6.0_x64__8wekyb3d8bbwe"
         "\\pwsh.exe' -NoProfile -Command ")
CODEX_PASS = json.dumps({"type": "item.completed", "item": {
    "id": "item_3", "type": "command_execution",
    "command": _PWSH + "'python -m pytest -q'",
    "aggregated_output": "............\r\n12 passed in 0.31s\r\n",
    "exit_code": 0, "status": "completed"}})
CODEX_FAIL = json.dumps({"type": "item.completed", "item": {
    "id": "item_4", "type": "command_execution",
    "command": _PWSH + "'npx tsc --noEmit'",
    "aggregated_output": "src/app.ts(3,7): error TS2322: Type 'string' is not assignable "
                         "to type 'number'.\r\n",
    "exit_code": 2, "status": "failed"}})
CODEX_STARTED = json.dumps({"type": "item.started", "item": {
    "id": "item_3", "type": "command_execution", "command": _PWSH + "'python -m pytest -q'",
    "aggregated_output": "", "exit_code": None, "status": "in_progress"}})

# Claude Code 2.1.288 stream-json: the command in the assistant's tool_use, the
# result later by tool_use_id; a non-zero exit is "Exit code N\n..." with
# is_error true (same shape as the owner's own transcripts).
CLAUDE_CALL = json.dumps({"type": "assistant", "session_id": "S", "message": {"content": [
    {"type": "tool_use", "id": "toolu_01RcrDL2WdMVbKsR7ZXRemMC", "name": "Bash",
     "input": {"command": "python -m pytest -q", "description": "Run the tests"}}]}})
CLAUDE_FAIL = json.dumps({"type": "user", "session_id": "S", "parent_tool_use_id": None,
                          "message": {"role": "user", "content": [
                              {"tool_use_id": "toolu_01RcrDL2WdMVbKsR7ZXRemMC",
                               "type": "tool_result", "is_error": True,
                               "content": "Exit code 1\nFAILED tests/test_app.py::test_add\n"
                                          "2 failed, 11 passed in 0.42s"}]},
                          "tool_use_result": "Error: Exit code 1"})
CLAUDE_CALL2 = CLAUDE_CALL.replace("toolu_01RcrDL2WdMVbKsR7ZXRemMC", "toolu_02")
CLAUDE_OK = json.dumps({"type": "user", "session_id": "S", "parent_tool_use_id": None,
                        "message": {"role": "user", "content": [
                            {"tool_use_id": "toolu_02", "type": "tool_result",
                             "content": "13 passed in 0.40s", "is_error": False}]},
                        "tool_use_result": {"stdout": "13 passed in 0.40s", "stderr": "",
                                            "interrupted": False, "isImage": False,
                                            "noOutputExpected": False}})

# opencode 1.18.34 `run --format json`: tool_use only once completed/error;
# the shell tool's exit code is state.metadata.exit (its binary).
OPENCODE_PASS = json.dumps({"type": "tool_use", "timestamp": 1790000004000, "sessionID": "ses_1",
                            "part": {"id": "prt_1", "sessionID": "ses_1", "messageID": "msg_1",
                                     "type": "tool", "callID": "call_1", "tool": "bash",
                                     "state": {"status": "completed",
                                               "input": {"command": "npx vitest run",
                                                         "description": "Run the tests"},
                                               "output": " Test Files  2 passed (2)\n"
                                                         "      Tests  9 passed (9)\n",
                                               "title": "npx vitest run",
                                               "metadata": {"output": "", "exit": 0,
                                                            "truncated": False},
                                               "time": {"start": 1790000000000,
                                                        "end": 1790000004000}}}})
OPENCODE_ERR = json.dumps({"type": "tool_use", "sessionID": "ses_1", "part": {
    "type": "tool", "tool": "bash", "state": {
        "status": "error", "input": {"command": "pytest -q"}, "error": "User aborted the command",
        "time": {"start": 1, "end": 2}}}})


def _results(events):
    return [e for e in events if e.get("event") == "tool_result"]


def test_codex_emits_the_exit_code_beside_its_unchanged_output_event():
    evs = ac._codex_stream_events(CODEX_PASS)
    assert evs[0] == {"event": "output", "text": "............\r\n12 passed in 0.31s\r\n"}
    (r,) = _results(evs)
    assert (r["exit_code"], r["is_error"]) == (0, False)
    assert r["command"].endswith("-Command 'python -m pytest -q'")
    assert E.from_event(r)["verdict"] == E.PASS
    (f,) = _results(ac._codex_stream_events(CODEX_FAIL))
    assert (f["exit_code"], f["is_error"]) == (2, True)
    assert E.from_event(f)["verdict"] == E.FAIL
    # Still running: only the old "tool" line.
    assert ac._codex_stream_events(CODEX_STARTED) == [
        {"event": "tool", "text": _PWSH + "'python -m pytest -q'"}]


def test_claude_pairs_the_result_with_its_call_and_reads_the_exit_code():
    call = ac._claude_stream_events(CLAUDE_CALL)
    assert call == [{"event": "tool", "text": "Bash: python -m pytest -q"}]
    evs = ac._claude_stream_events(CLAUDE_FAIL)
    assert evs[0]["event"] == "output"
    (r,) = _results(evs)
    assert (r["command"], r["exit_code"], r["is_error"]) == ("python -m pytest -q", 1, True)
    assert E.from_event(r)["verdict"] == E.FAIL
    ac._claude_stream_events(CLAUDE_CALL2)
    (ok,) = _results(ac._claude_stream_events(CLAUDE_OK))
    assert (ok["exit_code"], ok["is_error"]) == (0, False)
    assert E.from_event(ok)["verdict"] == E.PASS


def test_claude_does_not_invent_an_exit_code():
    bg = json.loads(CLAUDE_CALL.replace("toolu_01RcrDL2WdMVbKsR7ZXRemMC", "toolu_bg"))
    bg["message"]["content"][0]["input"]["run_in_background"] = True
    ac._claude_stream_events(json.dumps(bg))
    line = CLAUDE_OK.replace("toolu_02", "toolu_bg")
    (r,) = _results(ac._claude_stream_events(line))
    assert r["exit_code"] is None, "a background command has not finished"
    # A grep-like special case (returnCodeInterpretation) is not "exit 0" either.
    ac._claude_stream_events(CLAUDE_CALL.replace("toolu_01RcrDL2WdMVbKsR7ZXRemMC", "toolu_g"))
    g = json.loads(CLAUDE_OK.replace("toolu_02", "toolu_g"))
    g["tool_use_result"]["returnCodeInterpretation"] = "No matches found"
    (r,) = _results(ac._claude_stream_events(json.dumps(g)))
    assert r["exit_code"] is None


def test_opencode_reads_state_metadata_exit():
    evs = ac._opencode_stream_events(OPENCODE_PASS)
    assert {"event": "tool", "text": "bash npx vitest run"} in evs
    (r,) = _results(evs)
    assert (r["exit_code"], r["is_error"], r["started_at"], r["ended_at"]) == \
        (0, False, 1790000000.0, 1790000004.0)
    assert E.from_event(r)["verdict"] == E.PASS
    (e,) = _results(ac._opencode_stream_events(OPENCODE_ERR))
    assert (e["exit_code"], e["is_error"]) == (None, True)
    assert E.from_event(e)["verdict"] == E.UNDETERMINED


def test_the_output_tail_is_capped():
    big = json.loads(CODEX_PASS)
    big["item"]["aggregated_output"] = "x" * 9000 + "\n12 passed in 0.31s\n"
    (r,) = _results(ac._codex_stream_events(json.dumps(big)))
    assert len(r["output_tail"]) == 4000 and r["output_tail"].endswith("12 passed in 0.31s\n")


# --------------------------------------------------------------------------- #
# Memory: only an observed PASS verifies a command
# --------------------------------------------------------------------------- #

@pytest.fixture
def mem_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(memory._ROOT_ENV, str(tmp_path / "mem"))
    monkeypatch.setattr(memory, "_FACT_EXTRACTOR", None)
    monkeypatch.setattr(receipts, "root", lambda: str(tmp_path / "state" / "receipts"))
    return tmp_path


def _obs(cmd, code, out):
    return {"event": "tool_result", "command": cmd, "exit_code": code, "is_error": code != 0,
            "output_tail": out}


def test_memory_files_a_command_only_when_its_last_observed_run_passed(mem_dir):
    fail = _obs("pytest -q", 1, "2 failed, 11 passed in 0.42s")
    ok = _obs("pytest -q", 0, "13 passed in 0.40s")
    assert memory.harvest_commands(results=[fail, ok]) == ["pytest -q"]
    assert memory.harvest_commands(results=[ok, fail]) == [], "it failed last"
    assert memory.harvest_commands(["bash: pytest -q"], "All 13 tests passed, all green.") == []
    words = _obs("pytest -q", 0, "all green, no errors")
    assert memory.harvest_commands(results=[words]) == [], "exit 0 alone is not a pass"
    codex = {"event": "tool_result", "command": _PWSH + "'python -m pytest -q'",
             "exit_code": 0, "output_tail": "12 passed in 0.31s"}
    assert memory.harvest_commands(results=[codex]) == ["python -m pytest -q"]


def test_the_commands_fact_links_its_receipt(mem_dir):
    proj = mem_dir / "proj"
    proj.mkdir()
    path = receipts.write("sess-1", "agent-turn", [E.from_event(_obs("pytest -q", 0,
                                                                     "13 passed in 0.40s"))],
                          cwd=str(proj))
    memory.harvest_facts("sess-1", reply="done", project_dir=str(proj),
                         results=[_obs("pytest -q", 0, "13 passed in 0.40s")], receipt=path)
    (fact,) = [f for f in memory.project_facts(str(proj))
               if f.startswith(memory.COMMANDS_FACT_PREFIX)]
    assert fact == memory.COMMANDS_FACT_PREFIX + "`pytest -q` (receipt: receipts/sess-1/1.json)"


def _durable(monkeypatch, tmp_path, events, sid="obs-1"):
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    monkeypatch.setattr(ac, "get_session", lambda s: {"cli": "claude", "project_dir": str(proj)})
    monkeypatch.setattr(ac, "turn_busy", lambda s: False)
    monkeypatch.setattr(ac.agentic_history, "record_turn", lambda *a, **k: None)

    def fake_stream(session_id, text):
        time.sleep(0.3)        # a real CLI needs seconds before its first write
        (proj / "app.py").write_bytes(b"print('hi')\n")
        for e in events:
            yield e
    monkeypatch.setattr(ac, "send_message_stream", fake_stream)
    got = list(ac.send_message_stream_durable(sid, "build it"))
    return got, proj


def test_a_durable_turn_forwards_only_check_results_and_writes_a_receipt(mem_dir, monkeypatch):
    events = [
        {"event": "tool", "text": "Bash: ls"},
        {"event": "tool_result", "command": "ls", "exit_code": 0, "is_error": False,
         "output_tail": "app.py", "started_at": None, "ended_at": time.time()},
        {"event": "tool", "text": "Bash: pytest -q"},
        {"event": "tool_result", "command": "pytest -q", "exit_code": 0, "is_error": False,
         "output_tail": "13 passed in 0.40s", "started_at": None, "ended_at": time.time()},
        {"event": "done", "text": "Wrote app.py; tests pass."},
    ]
    got, proj = _durable(monkeypatch, mem_dir, events)
    results = _results(got)
    assert [r["command"] for r in results] == ["pytest -q"], "an `ls` is not evidence"
    assert ac._LIVE.get("obs-1") is None or not any(
        e.get("event") == "tool_result" for e in ac._LIVE["obs-1"].events)
    folder = mem_dir / "state" / "receipts" / "obs-1"
    (name,) = os.listdir(folder)
    rec = json.loads((folder / name).read_text(encoding="utf-8"))
    assert rec["kind"] == "agent-turn" and rec["source"] == "observed"
    assert rec["verdict"] == E.PASS and rec["cwd"] == str(proj)
    (row,) = rec["results"]
    assert (row["command"], row["argv"], row["tool"], row["exit_code"], row["passed"],
            row["verdict"]) == ("pytest -q", ["pytest", "-q"], "pytest", 0, 13, E.PASS)
    assert [c["path"] for c in rec["changed_files"]] == ["app.py"]
    assert rec["changed_files"][0]["sha256"] == hashlib.sha256(b"print('hi')\n").hexdigest()
    facts = memory.project_facts(str(proj))
    assert any(f.startswith(memory.COMMANDS_FACT_PREFIX + "`pytest -q`") for f in facts)


def test_a_reply_claiming_green_over_an_observed_failure_files_nothing(mem_dir, monkeypatch):
    events = [
        {"event": "tool_result", "command": "pytest -q", "exit_code": 1, "is_error": True,
         "output_tail": "2 failed, 11 passed in 0.42s"},
        {"event": "done", "text": "All tests passed, everything is green."},
    ]
    _got, proj = _durable(monkeypatch, mem_dir, events, sid="obs-2")
    facts = memory.project_facts(str(proj))
    assert not any(f.startswith(memory.COMMANDS_FACT_PREFIX) for f in facts)
    rec = receipts.read(os.path.join(receipts.root(), "obs-2", "1.json"))
    assert rec["verdict"] == E.FAIL


# --------------------------------------------------------------------------- #
# Multi: observed FAIL -> one revision; claimed-not-observed; labels
# --------------------------------------------------------------------------- #

@pytest.fixture
def runs(tmp_path, monkeypatch):
    monkeypatch.setenv(SW._STORE_ENV, str(tmp_path / "runs"))
    monkeypatch.setattr(SW, "SPAWN_STAGGER", 0)
    monkeypatch.setattr(SW, "RETRY_BACKOFF", 0)
    monkeypatch.setattr(receipts, "root", lambda: str(tmp_path / "state" / "receipts"))
    SW._RUNS.clear()
    yield tmp_path
    for run in list(SW._RUNS.values()):
        run.stop_flag.set()
    SW._RUNS.clear()


def _wait(run_id, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        st = SW.status(run_id)
        if st and st["state"] in (SW.DONE, SW.FAILED, SW.STOPPED):
            return st
        time.sleep(0.02)
    return SW.status(run_id)


PYTEST_FAIL = "FAILED tests/test_app.py::test_add\n2 failed, 11 passed in 0.42s\n"
PYTEST_PASS = "13 passed in 0.40s\n"


class _World:
    def __init__(self, scripts):
        self.scripts = scripts          # title -> list of attempts (lists of events)
        self.prompts = []
        self.n = 0

    def spawn(self, cli, project):
        self.n += 1
        return "w%d" % self.n

    def run_turn(self, sid, prompt):
        self.prompts.append(prompt)
        title = prompt.split("Your phase is called: ", 1)[1].splitlines()[0]
        queue = self.scripts.get(title) or []
        evs = queue.pop(0) if queue else [{"event": "message", "text": "did " + title}]
        for e in evs:
            yield e


def _attempt(code, out, summary):
    return [{"event": "tool", "text": "Bash: pytest -q"},
            {"event": "tool_result", "command": "pytest -q", "exit_code": code,
             "is_error": code != 0, "output_tail": out},
            {"event": "message", "text": summary}]


PHASE = [{"title": "Build", "task": "write app.py and its tests", "needs": []}]


def test_an_observed_failure_gets_one_revision_without_a_manager(runs):
    w = _World({"Build": [_attempt(1, PYTEST_FAIL, "Done. All tests pass."),
                          _attempt(0, PYTEST_PASS, "Fixed test_add.")]})
    rid = SW.start("app", str(runs), "claude", w.spawn, w.run_turn, phases=PHASE, review=False)
    st = _wait(rid)
    a = st["agents"][0]
    assert (a["state"], a["revisions"], a["verified"], a["reviewed"]) == (SW.DONE, 1, True, False)
    assert a["check"] == {"kind": "observed_pass", "text": "13 passed (observed)"}
    assert a["problems"] == []
    second = w.prompts[1]
    assert "WAS CHECKED AND REJECTED" in second
    assert "`pytest -q` failed when you ran it (exit 1: 2 failed, 11 passed)" in second
    assert [e["verdict"] for e in a["evidence"]] == [E.FAIL, E.PASS]
    report = SW.format_result(rid)
    assert "(1/1 phases done, 1 verified by an observed test/build run)" in report
    assert "Checked: 13 passed (observed)" in report


def test_a_failure_still_observed_after_the_revision_fails_the_phase(runs):
    w = _World({"Build": [_attempt(1, PYTEST_FAIL, "Done."), _attempt(1, PYTEST_FAIL, "Done.")]})
    rid = SW.start("app", str(runs), "claude", w.spawn, w.run_turn, phases=PHASE, review=False)
    st = _wait(rid)
    a = st["agents"][0]
    assert (a["state"], a["verified"], a["revisions"]) == (SW.FAILED, False, 1)
    assert a["check"] == {"kind": "observed_fail", "text": "2 failed (observed)"}
    assert "Still failing its checks: `pytest -q` failed" in SW.format_result(rid)


def test_with_a_manager_the_failure_costs_no_manager_call_and_the_brief_shows_results(runs):
    calls = []

    def manager(system, user, purpose, max_tokens):
        calls.append((purpose, user))
        return (json.dumps({"ok": True}), 50) if purpose == "verify" else ("", 0)
    w = _World({"Build": [_attempt(1, PYTEST_FAIL, "Done."), _attempt(0, PYTEST_PASS, "Fixed.")]})
    rid = SW.start("app", str(runs), "claude", w.spawn, w.run_turn, phases=PHASE,
                   review=False, manager=manager)
    st = _wait(rid)
    a = st["agents"][0]
    assert (a["state"], a["verified"], a["reviewed"], a["revisions"]) == (SW.DONE, True, True, 1)
    verifies = [u for p, u in calls if p == "verify"]
    assert len(verifies) == 1, "the observed failure was caught without asking the manager"
    assert "Test/build commands the hub OBSERVED" in verifies[0]
    assert "- `pytest -q` -> exit 1, FAIL: 2 failed, 11 passed" in verifies[0]
    assert "- `pytest -q` -> exit 0, PASS: 13 passed" in verifies[0]


def test_a_claimed_pass_with_nothing_observed_is_claimed_not_checked(runs):
    w = _World({"Build": [[{"event": "message", "text": "Wrote app.py. All 12 tests pass."}]]})
    # Two phases, so the run gets its review phase (with_review skips a
    # single-phase plan).
    phases = PHASE + [{"title": "Docs", "task": "write the README", "needs": []}]
    rid = SW.start("app", str(runs), "claude", w.spawn, w.run_turn, phases=phases, review=True)
    st = _wait(rid)
    a = st["agents"][0]
    assert (a["state"], a["revisions"], a["verified"]) == (SW.DONE, 0, None)
    assert a["claimed_not_observed"] is True
    assert a["check"] == {"kind": "claimed", "text": "claimed, not checked"}
    review = [p for p in w.prompts if "Your phase is called: " + SW.REVIEW_TITLE in p][0]
    assert ("phase 1 (Build): says its tests/build pass, but no passing run was observed "
            "-- run them") in review
    assert "Checked: claimed, not checked" in SW.format_result(rid)


def test_the_manager_ok_on_a_summary_is_reviewed_not_verified(runs):
    def manager(system, user, purpose, max_tokens):
        return (json.dumps({"ok": True}), 40) if purpose == "verify" else ("", 0)
    w = _World({"Build": [[{"event": "message", "text": "Wrote app.py."}]]})
    rid = SW.start("app", str(runs), "claude", w.spawn, w.run_turn, phases=PHASE,
                   review=False, manager=manager)
    a = _wait(rid)["agents"][0]
    assert (a["verified"], a["reviewed"], a["check"]) == (None, True,
                                                         {"kind": "reviewed", "text": "reviewed"})
    report = SW.format_result(rid)
    assert "0 verified by an observed test/build run, 1 only reviewed" in report
    assert "Checked: reviewed by the manager" in report


def test_a_plain_run_with_nothing_checked_reads_as_before(runs):
    w = _World({})
    rid = SW.start("app", str(runs), "claude", w.spawn, w.run_turn, phases=PHASE, review=False)
    _wait(rid)
    report = SW.format_result(rid)
    assert "(1/1 phases done)" in report and "Checked:" not in report


def test_a_phase_receipt_and_the_evidence_survive_a_restart(runs):
    proj = runs / "proj"
    proj.mkdir()
    (proj / "app.py").write_bytes(b"x = 1\n")
    (proj / "README.md").write_bytes(b"untouched\n")
    w = _World({"Build": [_attempt(0, PYTEST_PASS, "Done.")]})

    def run_turn(sid, prompt):
        time.sleep(0.05)
        (proj / "app.py").write_bytes(b"x = 22\n")
        yield from w.run_turn(sid, prompt)
    rid = SW.start("app", str(proj), "claude", w.spawn, run_turn, phases=PHASE, review=False)
    a = _wait(rid)["agents"][0]
    assert a["receipt"] and a["receipt"].endswith(os.path.join(rid, "1.json"))
    rec = receipts.read(a["receipt"])
    assert rec["kind"] == "multi-phase" and rec["run_id"] == rid and rec["phase"] == 1
    assert rec["verdict"] == E.PASS and rec["source"] == "observed"
    assert rec["changed_files"] == [{"path": "app.py", "bytes": 7,
                                     "sha256": hashlib.sha256(b"x = 22\n").hexdigest()}]
    SW._RUNS.clear()
    assert SW.load() >= 1
    back = SW.status(rid)["agents"][0]
    assert back["verified"] is True and back["evidence"][0]["verdict"] == E.PASS
    assert back["check"]["text"] == "13 passed (observed)" and back["receipt"] == a["receipt"]


def test_an_old_run_file_reads_manager_verified_as_reviewed():
    row = {"run_id": "swarm-old", "goal": "g", "project_dir": ".", "cli": "claude",
           "state": SW.DONE, "agents": [{"title": "A", "task": "t", "state": SW.DONE,
                                         "summary": "ok", "verified": True}]}
    run = SW._Run.from_row(row)
    assert (run.agents[0].verified, run.agents[0].reviewed) == (None, True)


# --------------------------------------------------------------------------- #
# Labels on the page
# --------------------------------------------------------------------------- #

def test_the_helpers_panel_shows_the_check(runs):
    import app as A
    w = _World({"Build": [_attempt(0, PYTEST_PASS, "Done.")]})
    rid = SW.start("app", str(runs), "claude", w.spawn, w.run_turn, phases=PHASE,
                   review=False, owner="conv-obs")
    _wait(rid)
    plan = A._multi_run_plan("conv-obs", str(runs / "elsewhere"))
    assert plan["tasks"][0]["check"] == {"kind": "observed_pass", "text": "13 passed (observed)"}


def _ui():
    with open(os.path.join(os.path.dirname(__file__), "..", "templates", "index.html"),
              encoding="utf-8") as fh:
        return fh.read()


def test_the_ui_words_what_was_observed_and_what_was_only_claimed():
    src = _ui()
    i = src.index("function helperRow(")
    body = src[i:i + 4000]
    assert "t.check && t.check.text" in body and "'hp-check ' + (t.check.kind" in body
    assert "HP_CHECK_TIP[t.check.kind]" in body
    for kind in ("observed_pass", "observed_fail", "claimed", "reviewed", "no_tests"):
        assert kind + ":" in src
    assert ".hp-check.observed_pass" in src and ".hp-check.claimed" in src
    assert "verified (observed)" in src
    assert "Checked by the subscription manager\">&check; verified" not in src


# --------------------------------------------------------------------------- #
# Receipts
# --------------------------------------------------------------------------- #

def test_a_receipt_carries_git_head_and_hashes_only_changed_files_within_caps(tmp_path, monkeypatch):
    monkeypatch.setattr(receipts, "root", lambda: str(tmp_path / "receipts"))
    proj = tmp_path / "proj"
    (proj / ".git" / "refs" / "heads").mkdir(parents=True)
    sha = "a" * 40
    (proj / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (proj / ".git" / "refs" / "heads" / "main").write_text(sha + "\n")
    for i in range(5):
        (proj / ("f%d.txt" % i)).write_bytes(b"x" * (i + 1))
    monkeypatch.setattr(receipts, "HASH_MAX_FILES", 3)
    row = E.from_event(_obs("pytest -q", 0, "13 passed in 0.40s"))
    path = receipts.write("s9", "agent-turn", [row], cwd=str(proj),
                          changed=["f0.txt", "f1.txt", "f2.txt", "f3.txt", "f4.txt"])
    rec = receipts.read(path)
    assert rec["git_head"] == sha
    assert len(rec["changed_files"]) == 3 and rec["changed_files_total"] == 5
    assert rec["changed_files_capped"] is True
    assert rec["changed_files"][1]["sha256"] == hashlib.sha256(b"xx").hexdigest()
    small = receipts.hash_files(str(proj), ["f4.txt", "f3.txt"], max_bytes=6)
    assert small[0]["sha256"] and small[1]["sha256"] is None and "budget" in small[1]["skipped"]
    assert set(rec) >= {"schema", "kind", "scope", "source", "cwd", "git_head", "started_at",
                        "ended_at", "verdict", "results", "changed_files"}
    (r,) = rec["results"]
    assert set(r) >= {"command", "argv", "tool", "version", "exit_code", "passed", "failed",
                      "skipped", "verdict", "line", "started_at", "ended_at", "source"}


def test_receipts_are_pruned_oldest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(receipts, "root", lambda: str(tmp_path / "receipts"))
    monkeypatch.setattr(receipts, "MAX_RECEIPTS", 4)
    row = E.from_event(_obs("pytest -q", 0, "13 passed in 0.40s"))
    paths = []
    for i in range(6):
        paths.append(receipts.write("s%d" % (i % 2), "agent-turn", [row]))
        t = time.time() - 100 + i
        os.utime(paths[-1], (t, t))
    receipts.prune(keep=4)
    left = sorted(os.path.join(dp, f) for dp, _d, fs in os.walk(tmp_path / "receipts") for f in fs)
    assert left == sorted(paths[2:])
    assert receipts.write("s0", "agent-turn", []) is None, "nothing observed, no receipt"


def test_a_detached_head_and_packed_refs_are_read_without_running_git(tmp_path, monkeypatch):
    import snapshots
    monkeypatch.setattr(snapshots, "_run", lambda *a, **k: pytest.fail("git was run"))
    (tmp_path / "a" / ".git").mkdir(parents=True)
    (tmp_path / "a" / ".git" / "HEAD").write_text("b" * 40 + "\n")
    assert receipts.git_head(str(tmp_path / "a")) == "b" * 40
    (tmp_path / "b" / ".git").mkdir(parents=True)
    (tmp_path / "b" / ".git" / "HEAD").write_text("ref: refs/heads/dev\n")
    (tmp_path / "b" / ".git" / "packed-refs").write_text(
        "# pack-refs with: peeled\n" + "c" * 40 + " refs/heads/dev\n")
    (tmp_path / "b" / "sub").mkdir()
    assert receipts.git_head(str(tmp_path / "b" / "sub")) == "c" * 40
