"""The memory writer: durable facts written AND updated after every finished
turn, in every CLI (owner 2026-10-29).

Hermetic: no network (the extractor's model call is faked at _route_by_difficulty /
_build_chain / _dispatch_chat), no real CLI, the memory folder is a tmp dir, the
config flags are monkeypatched, every conversation is synthetic."""
import logging
import os
import time

import pytest

import app as A
import config
import memory


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

class _Resp:
    def __init__(self, content, status=200):
        self.status_code = status
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}

    def close(self):
        pass


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """A private memory folder, default flags, an empty /v1 ledger, and a model
    call that never leaves the process (records what it was sent)."""
    monkeypatch.setenv(memory._ROOT_ENV, str(tmp_path / "memory"))
    flags = {}
    monkeypatch.setattr(config, "get_flag",
                        lambda name, default=None: flags.get(name, default))
    monkeypatch.setattr(A, "_build_sid", lambda: None)
    sent, replies = [], []

    def route(msgs, max_tokens=None, **k):
        return "fakepid", "fake-model", "simple"

    def chain(pid, model, *a, **k):
        return [(pid, model)]

    def dispatch(pid, payload, stream):
        sent.append(payload)
        return _Resp(replies.pop(0) if replies else "NONE")

    monkeypatch.setattr(A, "_route_by_difficulty", route)
    monkeypatch.setattr(A, "_build_chain", chain)
    monkeypatch.setattr(A, "_dispatch_chat", dispatch)
    monkeypatch.setattr(A, "_is_sub", lambda pid: False)
    monkeypatch.setattr(memory, "_FACT_EXTRACTOR", None)
    A._MFX_LAST.clear()
    A._MFX_CALLS.clear()
    yield {"flags": flags, "sent": sent, "replies": replies, "tmp": tmp_path}
    A._MFX_LAST.clear()
    A._MFX_CALLS.clear()


def _wait(pred, timeout=4.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return bool(pred())


def _proj(sandbox, name="proj"):
    d = sandbox["tmp"] / name
    d.mkdir(exist_ok=True)
    return str(d)


class _Recorder:
    """A fake extractor: records its calls, returns canned facts."""

    def __init__(self, facts=(), wants_known=None, enabled=True):
        self.calls, self.facts = [], list(facts)
        self.enabled = lambda: enabled
        if wants_known is not None:
            self.wants_known = lambda: wants_known

    def __call__(self, request, reply, **kw):
        self.calls.append((request, reply, kw))
        return list(self.facts)


# --------------------------------------------------------------------------- #
# memory.py: the pass can be split from the pattern harvest
# --------------------------------------------------------------------------- #

def test_model_pass_false_runs_the_pattern_harvest_only(sandbox):
    rec = _Recorder(["The deploy target is Fly.io."])
    memory.set_fact_extractor(rec)
    proj = _proj(sandbox)
    filed = memory.harvest_facts("s1", request="Always use tabs.", reply="Decision: use Vite.",
                                 project_dir=proj, model_pass=False)
    time.sleep(0.15)
    assert rec.calls == []
    assert "Decision: use Vite." in filed
    assert "The deploy target is Fly.io." not in memory.project_facts(proj)


def test_the_default_harvest_still_runs_the_model_pass(sandbox):
    rec = _Recorder(["The deploy target is Fly.io."])
    memory.set_fact_extractor(rec)
    proj = _proj(sandbox)
    memory.harvest_facts("s1", request="ship it", reply="done", project_dir=proj)
    assert _wait(lambda: "The deploy target is Fly.io." in memory.project_facts(proj))


def test_extract_facts_files_into_the_project_with_its_conversation(sandbox):
    rec = _Recorder(["The API listens on port 8080."])
    memory.set_fact_extractor(rec)
    proj = _proj(sandbox)
    assert memory.extract_facts("conv-1", request="run the api", reply="It runs on 8080.",
                                project_dir=proj) is True
    assert _wait(lambda: "The API listens on port 8080." in memory.project_facts(proj))
    meta = memory.get(memory.project_key(proj))["fact_meta"]
    assert meta[memory._fact_key("The API listens on port 8080.")]["by"] == "conv-1"
    assert rec.calls[0][:2] == ("run the api", "It runs on 8080.")


def test_extract_facts_needs_an_installed_enabled_extractor(sandbox):
    proj = _proj(sandbox)
    assert memory.extract_facts("c", request="a", reply="b", project_dir=proj) is False
    memory.set_fact_extractor(_Recorder(["x" * 20], enabled=False))
    assert memory.extract_facts("c", request="a", reply="b", project_dir=proj) is False
    memory.set_fact_extractor(_Recorder(["x" * 20]))
    assert memory.extract_facts("c", request="", reply="", project_dir=proj) is False


# --------------------------------------------------------------------------- #
# memory.py: updates, not only appends
# --------------------------------------------------------------------------- #

def _seed(proj, *facts):
    for f in facts:
        memory.remember_project_fact(proj, f)


def _run(rec, proj, by="conv"):
    memory._run_extractor(rec, memory.project_key(proj), "req", "rep", proj, by=by)


def test_a_replacement_supersedes_the_older_fact(sandbox):
    proj = _proj(sandbox)
    _seed(proj, "Decision: use pnpm for every install.", "Never commit the .env file.")
    rec = _Recorder([{"text": "The project now uses npm instead of pnpm.",
                      "replaces": "Decision: use pnpm for every install."}], wants_known=True)
    _run(rec, proj)
    facts = memory.project_facts(proj)
    assert "Decision: use pnpm for every install." not in facts
    assert "The project now uses npm instead of pnpm." in facts
    assert "Never commit the .env file." in facts
    meta = memory.get(memory.project_key(proj))["fact_meta"]
    assert memory._fact_key("Decision: use pnpm for every install.") not in meta


def test_the_extractor_is_shown_the_known_facts_without_the_rolling_lists(sandbox):
    proj = _proj(sandbox)
    _seed(proj, "Decision: use Vite for the build.",
          memory.FILES_FACT_PREFIX + "a.py, b.py",
          memory.COMMANDS_FACT_PREFIX + "`pytest -q`")
    rec = _Recorder([], wants_known=True)
    _run(rec, proj)
    assert rec.calls[0][2] == {"known": ["Decision: use Vite for the build."]}


def test_a_restatement_is_not_filed_twice_but_a_reversal_is(sandbox):
    proj = _proj(sandbox)
    _seed(proj, "Decision: use Vite for the build.", "Use pnpm, not npm")
    rec = _Recorder(["use vite for the build", "Use npm, not pnpm"], wants_known=True)
    _run(rec, proj)
    facts = memory.project_facts(proj)
    assert not any(f == "use vite for the build" for f in facts)
    assert "Use npm, not pnpm" in facts and "Use pnpm, not npm" in facts


def test_a_replacement_naming_an_unknown_fact_removes_nothing(sandbox):
    proj = _proj(sandbox)
    _seed(proj, "Never commit the .env file.")
    rec = _Recorder([{"text": "Deploys go to Fly.io now.",
                      "replaces": "Something the model invented."}], wants_known=True)
    _run(rec, proj)
    facts = memory.project_facts(proj)
    assert "Never commit the .env file." in facts and "Deploys go to Fly.io now." in facts


def test_a_rolling_list_is_never_superseded(sandbox):
    proj = _proj(sandbox)
    files = memory.FILES_FACT_PREFIX + "a.py, b.py"
    _seed(proj, files)
    assert memory.supersede_fact(memory.project_key(proj), files,
                                 "The project has one file now.", project_dir=proj)
    assert files in memory.project_facts(proj)


def test_a_secret_replacement_removes_nothing_and_files_nothing(sandbox):
    proj = _proj(sandbox)
    _seed(proj, "Decision: use pnpm for every install.")
    rec = _Recorder([{"text": "The api key is sk-abcdef1234567890XYZ",
                      "replaces": "Decision: use pnpm for every install."}], wants_known=True)
    _run(rec, proj)
    facts = memory.project_facts(proj)
    assert facts == ["Decision: use pnpm for every install."]


def test_a_plain_extractor_keeps_the_old_behaviour(sandbox):
    """No `wants_known` (or it says no): called with two arguments only, dicts
    ignored, restatements filed as before (exact duplicates still deduped)."""
    proj = _proj(sandbox)
    _seed(proj, "Decision: use Vite for the build.")
    calls = []

    def plain(req, rep):
        calls.append((req, rep))
        return ["use vite for the build", {"text": "x" * 30, "replaces": "y"}]
    plain.enabled = lambda: True
    _run(plain, proj)
    assert calls == [("req", "rep")]
    facts = memory.project_facts(proj)
    assert "use vite for the build" in facts and ("x" * 30) not in facts
    off = _Recorder(["use vite for the build again"], wants_known=False)
    _run(off, proj)
    assert off.calls[0][2] == {}


def test_supersede_logs_no_fact_text(sandbox, caplog):
    proj = _proj(sandbox)
    _seed(proj, "Decision: keep the MARKERX port.")
    with caplog.at_level(logging.DEBUG):
        memory.supersede_fact(memory.project_key(proj), "Decision: keep the MARKERX port.",
                              "The MARKERY port replaced it.", project_dir=proj)
    assert "MARKERX" not in caplog.text and "MARKERY" not in caplog.text


# --------------------------------------------------------------------------- #
# app.py: the hub's extractor
# --------------------------------------------------------------------------- #

def test_the_extractor_is_on_by_default(sandbox):
    assert A._memory_fact_extractor_on() is True
    assert A._memory_fact_extractor.enabled() is True
    assert A._memory_fact_extractor.wants_known() is True
    sandbox["flags"]["memory_fact_extractor"] = False
    assert A._memory_fact_extractor_on() is False
    assert A._memory_fact_extractor("a", "b") == []
    sandbox["flags"]["memory_fact_updates"] = False
    assert A._memory_fact_extractor.wants_known() is False


def test_without_known_facts_the_prompt_is_the_old_one(sandbox):
    sandbox["replies"].append("- The repo uses pnpm.\n2. Tests run with pytest -q.")
    out = A._memory_fact_extractor("use pnpm", "ok, done")
    assert out == ["The repo uses pnpm.", "Tests run with pytest -q."]
    msgs = sandbox["sent"][0]["messages"]
    assert msgs == [{"role": "system", "content": A._FACT_EXTRACT_SYSTEM},
                    {"role": "user", "content": "USER:\nuse pnpm\n\nAGENT:\nok, done"}]


def test_known_facts_are_numbered_and_a_replaces_line_is_parsed(sandbox):
    known = ["Decision: use pnpm.", "Never commit the .env file."]
    sandbox["replies"].append("REPLACES 1: The project uses npm now.\n"
                              "`REPLACES 9: Out of range is a plain fact.`\n"
                              "Deploys go to Fly.io.")
    out = A._memory_fact_extractor("switch to npm", "Switched.", known=known)
    assert out == [{"text": "The project uses npm now.", "replaces": "Decision: use pnpm."},
                   "Out of range is a plain fact.", "Deploys go to Fly.io."]
    sys_msg, user_msg = sandbox["sent"][0]["messages"]
    assert sys_msg["content"].startswith(A._FACT_EXTRACT_SYSTEM)
    assert "REPLACES <number>" in sys_msg["content"]
    assert user_msg["content"].startswith(
        "KNOWN FACTS:\n1. Decision: use pnpm.\n2. Never commit the .env file.\n\nUSER:\n")


def test_a_paid_subscription_is_never_spent(sandbox, monkeypatch):
    monkeypatch.setattr(A, "_is_sub", lambda pid: True)
    assert A._memory_fact_extractor("a" * 40, "b" * 40) == []
    assert sandbox["sent"] == []


def test_a_failing_model_files_nothing(sandbox, monkeypatch):
    def boom(pid, payload, stream):
        raise RuntimeError("upstream down")
    monkeypatch.setattr(A, "_dispatch_chat", boom)
    assert A._memory_fact_extractor("a" * 40, "b" * 40, known=["x" * 20]) == []


# --------------------------------------------------------------------------- #
# app.py: which /v1 request closes an exchange
# --------------------------------------------------------------------------- #

def _sys(cwd):
    return {"role": "system", "content": "<cwd>%s</cwd>\nYou are a coding agent." % cwd}


INSTR1 = "Set up the build with Vite and keep the API on port 8080 from now on."
FINAL1 = "Done. Decision: the build uses Vite; the API stays on port 8080."
INSTR2 = "Now add a booking page."


def _loop(cwd):
    """Instruction 1, two tool cycles, its final reply."""
    return [_sys(cwd), {"role": "user", "content": INSTR1},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "bash", "arguments": '{"command": "ls"}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "src\npackage.json"},
            {"role": "assistant", "content": "Let me write it.", "tool_calls": [
                {"id": "c2", "type": "function",
                 "function": {"name": "write", "arguments": '{"path": "vite.config.js"}'}}]},
            {"role": "tool", "tool_call_id": "c2", "content": "ok"},
            {"role": "assistant", "content": FINAL1}]


def test_a_fresh_instruction_after_a_finished_reply_closes_the_exchange():
    msgs = _loop("/x") + [{"role": "user", "content": INSTR2}]
    assert A._mfx_exchange(msgs) == (INSTR1, FINAL1)


def test_mid_loop_requests_close_nothing():
    full = _loop("/x")
    for n in range(2, len(full)):           # every hop of the tool loop
        assert A._mfx_exchange(full[:n]) is None
    assert A._mfx_exchange(full) is None    # ends on the reply itself


def test_an_interrupted_tool_call_is_not_a_finished_reply():
    msgs = _loop("/x")[:5] + [{"role": "user", "content": INSTR2}]
    assert A._mfx_exchange(msgs) is None


def test_wrapper_blocks_are_not_an_instruction():
    reminder = {"role": "user", "content": "<system-reminder>todo list is empty</system-reminder>"}
    assert A._mfx_exchange(_loop("/x") + [reminder]) is None
    # A Claude Code user turn: reminder parts plus the real text.
    real = {"role": "user", "content": [
        {"type": "text", "text": "<system-reminder>context</system-reminder>"},
        {"type": "text", "text": INSTR2}]}
    assert A._mfx_exchange(_loop("/x") + [reminder, real]) == (INSTR1, FINAL1)


def test_compaction_and_tiny_exchanges_close_nothing():
    compact = [_sys("/x"), {"role": "user", "content": "Create a new anchored summary of the work."},
               {"role": "assistant", "content": "Summary: a long recap of everything done so far."},
               {"role": "user", "content": INSTR2}]
    assert A._mfx_exchange(compact) is None
    tiny = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hello!"},
            {"role": "user", "content": INSTR2}]
    assert A._mfx_exchange(tiny) is None
    no_instr = [{"role": "assistant", "content": FINAL1}, {"role": "user", "content": INSTR2}]
    assert A._mfx_exchange(no_instr) is None


# --------------------------------------------------------------------------- #
# app.py: _v1_observe runs the model pass once per finished exchange
# --------------------------------------------------------------------------- #

@pytest.fixture
def v1(sandbox, monkeypatch):
    """_v1_observe with the pass and the harvest recorded (synchronous)."""
    passes, harvests = [], []
    monkeypatch.setattr(A, "_orch_key", lambda b=None, m=None: "conv-a")
    monkeypatch.setattr(A.memory, "extract_facts",
                        lambda sid, **k: passes.append(dict(k, sid=sid)) or True)
    monkeypatch.setattr(A.memory, "harvest_facts",
                        lambda sid, **k: harvests.append(dict(k, sid=sid)) or [])
    return {"passes": passes, "harvests": harvests, "cwd": _proj(sandbox)}


def test_a_whole_turn_costs_one_model_pass(v1):
    full = _loop(v1["cwd"])
    for n in range(2, len(full) + 1):       # every hop of turn 1, plus its end
        A._v1_observe(full[:n], {})
    assert v1["passes"] == []
    assert all(h["model_pass"] is False for h in v1["harvests"])     # pattern only
    A._v1_observe(full + [{"role": "user", "content": INSTR2}], {})
    assert len(v1["passes"]) == 1
    p = v1["passes"][0]
    assert (p["request"], p["reply"], p["sid"]) == (INSTR1, FINAL1, "conv-a")
    assert os.path.normcase(p["project_dir"]) == os.path.normcase(v1["cwd"])


def test_the_cooldown_and_the_hourly_cap(v1, monkeypatch):
    msgs = _loop(v1["cwd"]) + [{"role": "user", "content": INSTR2}]
    A._v1_observe(msgs, {})
    A._v1_observe(msgs, {})                  # a retry of the same turn
    assert len(v1["passes"]) == 1
    monkeypatch.setattr(A, "_orch_key", lambda b=None, m=None: "conv-b")
    A._v1_observe(msgs, {})                  # another conversation: its own cooldown
    assert len(v1["passes"]) == 2
    t0 = time.monotonic()
    assert A._mfx_reserve("conv-b", now=t0 + A._MFX_COOLDOWN_S + 1)    # cooldown over
    A._MFX_LAST.clear()
    A._MFX_CALLS.clear()
    monkeypatch.setattr(A, "_MFX_HOURLY_CAP", 2)
    assert A._mfx_reserve("k1", now=t0) and A._mfx_reserve("k2", now=t0)
    assert not A._mfx_reserve("k3", now=t0)                          # cap reached
    assert A._mfx_reserve("k3", now=t0 + 3601)                       # an hour later


def test_no_pass_on_compaction_build_sessions_or_without_a_folder(v1, monkeypatch):
    base = _loop(v1["cwd"])
    A._v1_observe(base + [{"role": "user", "content":
                           "Create a new anchored summary from the conversation history"}], {})
    assert v1["passes"] == []
    monkeypatch.setattr(A, "_build_sid", lambda: "abc123")
    A._v1_observe(base + [{"role": "user", "content": INSTR2}], {})
    assert v1["passes"] == []
    monkeypatch.setattr(A, "_build_sid", lambda: None)
    no_cwd = [m for m in base if m.get("role") != "system"] + [{"role": "user", "content": INSTR2}]
    A._v1_observe(no_cwd, {})
    assert v1["passes"] == []


def test_flags_switch_the_v1_pass_off_but_not_the_pattern_harvest(v1, sandbox):
    msgs = _loop(v1["cwd"]) + [{"role": "user", "content": INSTR2}]
    sandbox["flags"]["memory_fact_extractor_v1"] = False
    A._v1_observe(msgs, {})
    assert v1["passes"] == [] and len(v1["harvests"]) == 1
    sandbox["flags"]["memory_fact_extractor_v1"] = True
    sandbox["flags"]["memory_fact_extractor"] = False
    A._v1_observe(msgs, {})
    assert v1["passes"] == [] and len(v1["harvests"]) == 2


def test_v1_observe_never_raises_when_the_pass_fails(v1, monkeypatch):
    monkeypatch.setattr(A.memory, "extract_facts",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    A._v1_observe(_loop(v1["cwd"]) + [{"role": "user", "content": INSTR2}], {})


# --------------------------------------------------------------------------- #
# end to end: the hub's own extractor, real memory, a faked model
# --------------------------------------------------------------------------- #

def test_written_then_updated_across_two_finished_exchanges(sandbox, monkeypatch, caplog):
    memory.set_fact_extractor(A._memory_fact_extractor)
    cwd = _proj(sandbox, "site")
    monkeypatch.setattr(A, "_orch_key", lambda b=None, m=None: "conv-e2e")
    sandbox["replies"].append("The build uses Vite.\nThe API listens on port 8080.")
    with caplog.at_level(logging.DEBUG):
        A._v1_observe(_loop(cwd) + [{"role": "user", "content": INSTR2}], {})
        assert _wait(lambda: "The API listens on port 8080." in memory.project_facts(cwd))
    assert "The build uses Vite." in memory.project_facts(cwd)
    first_prompt = sandbox["sent"][0]["messages"][1]["content"]
    assert INSTR1 in first_prompt and FINAL1 in first_prompt

    # The second exchange reverses a decision: the older fact is replaced.
    A._MFX_LAST.clear()
    later = (_loop(cwd) + [{"role": "user", "content": INSTR2},
                           {"role": "assistant", "content":
                            "Booking page added. The API moved to port 9090 as you asked."},
                           {"role": "user", "content": "Thanks, now write the README."}])
    known = memory.known_facts(memory.project_key(cwd))
    n = known.index("The API listens on port 8080.") + 1
    sandbox["replies"].append("REPLACES %d: The API listens on port 9090." % n)
    with caplog.at_level(logging.DEBUG):
        A._v1_observe(later, {})
        assert _wait(lambda: "The API listens on port 9090." in memory.project_facts(cwd))
    facts = memory.project_facts(cwd)
    assert "The API listens on port 8080." not in facts
    assert "The build uses Vite." in facts
    second = sandbox["sent"][1]["messages"]
    assert "KNOWN FACTS:" in second[1]["content"] and "REPLACES" in second[0]["content"]
    # Nothing the user or the agent said reaches the log.
    for text in (INSTR1, INSTR2, "port 9090", "Booking page"):
        assert text not in caplog.text
