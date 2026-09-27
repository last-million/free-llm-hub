"""Memory and context in EVERY mode: swarm, crews, multi, /agent, quick chat.

REPORTED (audit): the prose pipelines briefed their planner with only the LAST
user message, so a follow-up such as "make it better" lost the whole
conversation; "continue" in Multi sessions started a fresh run whose goal was
the word "continue"; long-term facts were never written; the manager saw no
memory; the quick chat had no recap of its own; and the manager-less revision
still replaced a draft with a rewrite of a CLIPPED copy of it. These pin the
fixes. No network: every model call is a fake.
"""
import json
import os
import shutil
import tempfile
import threading
import time

import pytest

import crews
import ctxwin
import memory
import swarm
import swarm_windows as SW


# --------------------------------------------------------------------------- #
# Shared fakes
# --------------------------------------------------------------------------- #

PLAN = json.dumps({"goal": "g", "phases": [
    {"title": "Part A", "task": "do part A", "needs": []},
    {"title": "Part B", "task": "do part B", "needs": []}]})
SHIP = json.dumps({"verdict": "ship", "problems": []})
REVISE = json.dumps({"verdict": "revise", "problems": ["part A misses the FAQ"]})


def _stage(msgs):
    sys_ = msgs[0]["content"]
    for name, prompt in (("plan", swarm._PLAN_SYSTEM), ("phase", swarm._PHASE_SYSTEM),
                         ("supervise", swarm._SUPERVISE_SYSTEM),
                         ("review", swarm._REVIEW_SYSTEM), ("synth", swarm._SYNTH_SYSTEM),
                         ("verify", swarm._VERDICT_SYSTEM),
                         ("instruct", swarm._INSTRUCT_SYSTEM),
                         ("apply", swarm._APPLY_SYSTEM),
                         ("confirm", swarm._CONFIRM_SYSTEM)):
        if sys_.startswith(prompt):
            return name
    return "?"


def _dispatch(phase_text=lambda user: "output", review=SHIP, apply=None):
    calls, lock = [], threading.Lock()

    def dispatch(messages, max_tokens, exclude_pids=()):
        st = _stage(messages)
        user = messages[-1]["content"]
        with lock:
            n = len(calls)
            calls.append({"stage": st, "user": user, "max_tokens": max_tokens})
        if st == "phase":
            text = phase_text(user)
        elif st == "apply":
            text = apply(user) if apply else ""
        else:
            text = {"plan": PLAN, "supervise": '{"missing": []}', "review": review,
                    "synth": "FINAL"}.get(st, "")
        return text, ("free%d/m" % n if text else None)

    dispatch.calls = calls
    dispatch.of = lambda st: [c for c in calls if c["stage"] == st]
    return dispatch


def _manager():
    calls = []

    def manager(messages, max_tokens, purpose):
        calls.append({"purpose": purpose, "user": messages[-1]["content"]})
        return {"plan": PLAN, "review": SHIP, "supervise": '{"missing": []}',
                "verify": '{"ok": true}'}.get(purpose, ""), "sub-x/y", 5

    manager.calls = calls
    return manager


FOLLOW_UP = [
    {"role": "user", "content": "Build a landing page for Luna Bakery with a hero and a menu."},
    {"role": "assistant", "content": "<html><h1>Luna Bakery</h1> MENU-SECTION ... </html>"},
    {"role": "user", "content": "make it better"},
]


# --------------------------------------------------------------------------- #
# 1. The brief is the conversation, not its last message
# --------------------------------------------------------------------------- #

def test_an_opening_turn_is_briefed_exactly_as_before():
    d = _dispatch()
    swarm.run([{"role": "user", "content": "build me a tool"}], d)
    assert d.of("plan")[0]["user"] == "build me a tool"


def test_a_follow_up_carries_the_earlier_request_and_the_previous_answer():
    d = _dispatch()
    swarm.run(FOLLOW_UP, d)
    plan_user = d.of("plan")[0]["user"]
    assert plan_user.startswith("make it better")
    assert "Luna Bakery with a hero" in plan_user        # the earlier request
    assert "MENU-SECTION" in plan_user                   # the previous deliverable


def test_the_added_context_is_bounded():
    history = []
    for i in range(40):
        history.append({"role": "user", "content": "request %d " % i + "x" * 900})
        history.append({"role": "assistant", "content": "answer %d " % i + "y" * 20000})
    history.append({"role": "user", "content": "make it better"})
    brief, block = swarm.conversation_brief(history, context="R" * 9000)
    assert len(block) <= swarm.BRIEF_CONTEXT_CHARS
    assert brief.startswith("make it better")
    assert len(brief) <= len("make it better") + swarm.BRIEF_CONTEXT_CHARS + 400


def test_the_callers_recap_reaches_the_planner_and_crews_forward_it():
    d = _dispatch()
    crews.run(FOLLOW_UP, d, "write", context="RECAP: the client hates purple")
    # The crew swaps the plan SYSTEM prompt, so the plan is simply call one.
    assert "the client hates purple" in d.calls[0]["user"]


def test_a_follow_up_keeps_the_crew_of_the_conversation():
    msgs = [{"role": "user", "content": "Design a landing page for my coffee shop"},
            {"role": "assistant", "content": "..."},
            {"role": "user", "content": "make it better"}]
    assert crews.detect_crew(msgs) == "design"


# --------------------------------------------------------------------------- #
# 4. The manager's plan and review see a bounded excerpt of it
# --------------------------------------------------------------------------- #

def test_manager_plan_and_review_see_a_bounded_context_excerpt():
    m = _manager()
    d = _dispatch()
    swarm.run(FOLLOW_UP, d, manager=m, context="MEMO " + "z" * 8000)
    # The review, or -- when every phase passed first time and the review is
    # skipped -- the last wave's combined check, which carries the same brief.
    final_look = [c for c in m.calls if c["purpose"] == "review"] or \
        [c for c in m.calls if c["purpose"] == "verify" and "JUDGE THESE" in c["user"]]
    assert final_look, [c["purpose"] for c in m.calls]
    for purpose, call in (("plan", next(c for c in m.calls if c["purpose"] == "plan")),
                          ("final look", final_look[-1])):
        assert "CONVERSATION CONTEXT" in call["user"], purpose
        ctx = call["user"].split("CONVERSATION CONTEXT", 1)[1]
        assert len(ctx) <= swarm.MANAGER_CONTEXT_CHARS + 400, purpose


# --------------------------------------------------------------------------- #
# 6. Workers see the request; synthesis scales; the revision never clips
# --------------------------------------------------------------------------- #

def test_workers_see_the_users_request_bounded():
    ask = "Make a menu. DETAIL-THE-PLANNER-DROPPED. " + "w" * 20000
    d = _dispatch()
    swarm.run([{"role": "user", "content": ask}], d)
    for c in d.of("phase"):
        assert "THE USER'S REQUEST" in c["user"]
        assert "DETAIL-THE-PLANNER-DROPPED" in c["user"]
        tail = c["user"].split("THE USER'S REQUEST", 1)[1]
        assert len(tail) <= swarm.WORKER_BRIEF_CHARS + 200


def test_synthesis_tokens_scale_with_the_phase_outputs_within_a_cap():
    big = " ".join("word%d" % i for i in range(3000))       # ~27K chars per phase
    d = _dispatch(phase_text=lambda user: big)
    swarm.run([{"role": "user", "content": "build it all"}], d)
    mt = d.of("synth")[0]["max_tokens"]
    assert swarm.SYNTH_MAX_TOKENS < mt <= swarm.SYNTH_MAX_CAP


def test_a_draft_too_long_for_one_apply_is_revised_per_phase_never_clipped():
    a = "Part A body. " + " ".join("alpha%d" % i for i in range(3000))
    b = "Part B body. " + " ".join("beta%d" % i for i in range(3000))

    def phase(user):
        return a if "do part A" in user else b

    def apply(user):
        work = user.split("THE WORK\n", 1)[1].split("\n\nFIX INSTRUCTIONS\n", 1)[0]
        if work.startswith("Part A"):
            return work + "\n\n## FAQ\nQ: fresh? A: daily."
        return work                                          # untouched part
    d = _dispatch(phase_text=phase, review=REVISE, apply=apply)
    swarm.run([{"role": "user", "content": "build it"}], d, profile={"max_revisions": 1})
    applies = d.of("apply")
    assert len(applies) == 2, "expected one apply per phase"
    for c in applies:
        assert "[... trimmed ...]" not in c["user"].split("FIX INSTRUCTIONS")[0]
    synth = d.of("synth")[0]["user"]
    assert "## FAQ" in synth and a in synth and b in synth


# --------------------------------------------------------------------------- #
# 3. Long-term facts are harvested, with no model call
# --------------------------------------------------------------------------- #

@pytest.fixture
def mem_dir(monkeypatch):
    d = tempfile.mkdtemp(prefix="hubmem-")
    monkeypatch.setenv(memory._ROOT_ENV, d)
    monkeypatch.setattr(memory, "_FACT_EXTRACTOR", None)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


REPLY = """Done. Here is what changed.

**Decision:** use pnpm, not npm, for every install.
- Never commit the .env file.
Constraint: the API must stay on port 8080.

```python
# Must not be harvested: it is code
```

All 12 tests passed."""


def test_a_turn_files_decisions_preferences_files_and_passing_commands(mem_dir):
    proj = os.path.join(mem_dir, "proj")
    os.makedirs(proj)
    tools = ["write %s" % os.path.join(proj, "src", "app.py"),
             "Write: README.md", "bash pytest -q", "read other.txt"]
    filed = memory.harvest_facts("s1", request="Always use tabs. Build me a page.",
                                 reply=REPLY, project_dir=proj, tools=tools)
    facts = memory.project_facts(proj)
    assert "Decision: use pnpm, not npm, for every install." in facts
    assert "Never commit the .env file." in facts
    assert any(f.startswith("Constraint: the API must stay on port 8080") for f in facts)
    assert "User preference: Always use tabs" in facts
    assert not any("Must not be harvested" in f for f in facts)
    files = [f for f in facts if f.startswith(memory.FILES_FACT_PREFIX)]
    assert files and "src/app.py" in files[0] and "README.md" in files[0]
    cmds = [f for f in facts if f.startswith(memory.COMMANDS_FACT_PREFIX)]
    assert cmds and "pytest -q" in cmds[0]
    assert filed


def test_the_rolling_facts_are_one_each_and_deduplicated(mem_dir):
    proj = os.path.join(mem_dir, "p2")
    os.makedirs(proj)
    for name in ("a.py", "b.py", "a.py"):
        memory.harvest_facts("s1", reply="ok", project_dir=proj, tools=["write " + name])
    memory.harvest_facts("s1", reply=REPLY, project_dir=proj)
    memory.harvest_facts("s1", reply=REPLY, project_dir=proj)
    facts = memory.project_facts(proj)
    files = [f for f in facts if f.startswith(memory.FILES_FACT_PREFIX)]
    assert len(files) == 1 and files[0].count("a.py") == 1 and "b.py" in files[0]
    assert facts.count("Never commit the .env file.") == 1


def test_a_failing_run_files_no_verified_command(mem_dir):
    memory.harvest_facts("s9", reply="3 failed, 9 passed", tools=["pytest -q"])
    assert not any(f.startswith(memory.COMMANDS_FACT_PREFIX)
                   for f in memory.get("s9").get("facts") or [])


def test_the_optional_extractor_runs_only_when_switched_on(mem_dir):
    seen = []

    def extractor(req, rep):
        seen.append(req)
        return ["The deploy target is Fly.io."]
    extractor.enabled = lambda: False
    memory.set_fact_extractor(extractor)
    try:
        memory.harvest_facts("s3", request="deploy it", reply="ok")
        time.sleep(0.1)
        assert seen == []
        extractor.enabled = lambda: True
        memory.harvest_facts("s3", request="deploy it", reply="ok")
        end = time.time() + 3
        while time.time() < end and "The deploy target is Fly.io." not in (
                memory.get("s3").get("facts") or []):
            time.sleep(0.02)
        assert "The deploy target is Fly.io." in memory.get("s3")["facts"]
    finally:
        memory.set_fact_extractor(None)


def test_an_agent_turn_end_harvests(mem_dir):
    import agentic_chat
    proj = os.path.join(mem_dir, "p3")
    os.makedirs(proj)
    agentic_chat._memory_turn_end("s4", "Never use jQuery", project_dir=proj,
                                  reply="Decision: vanilla JS only.")
    facts = memory.project_facts(proj)
    assert "Decision: vanilla JS only." in facts
    assert "User preference: Never use jQuery" in facts


def test_the_durable_turn_path_harvests_with_its_tool_calls():
    src = open("agentic_chat.py", encoding="utf-8").read()
    assert "tools_all.append(ev[\"text\"])" in src
    assert "tools=list(tools_all)" in src


# --------------------------------------------------------------------------- #
# 2. Multi: context in the plan and every brief; "continue" resumes
# --------------------------------------------------------------------------- #

@pytest.fixture
def runs(monkeypatch):
    d = tempfile.mkdtemp(prefix="hubruns-")
    monkeypatch.setenv(SW._STORE_ENV, d)
    monkeypatch.setattr(SW, "SPAWN_STAGGER", 0)
    monkeypatch.setattr(SW, "RETRY_BACKOFF", 0)
    SW._RUNS.clear()
    try:
        yield d
    finally:
        for r in list(SW._RUNS.values()):
            r.stop_flag.set()
        SW._RUNS.clear()
        shutil.rmtree(d, ignore_errors=True)


def _wait(rid, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        st = SW.status(rid)
        if st and st["state"] in (SW.DONE, SW.FAILED, SW.STOPPED):
            return st
        time.sleep(0.02)
    return SW.status(rid)


_n = [0]


def _spawn(cli, project):
    _n[0] += 1
    return "sess-%d" % _n[0]


def test_the_planner_and_every_worker_get_the_bounded_context(runs):
    asked = []

    def planner(system, goal):
        asked.append(goal)
        return json.dumps({"phases": [{"title": "Build", "task": "build it"}]})
    prompts = []

    def run_turn(sid, prompt):
        prompts.append(prompt)
        yield {"type": "message", "text": "done"}
        yield {"type": "done"}
    ctx = "MEMORY-BLOCK: uses pnpm. " + "q" * 9000
    rid = SW.start("make it better", ".", "opencode", _spawn, run_turn,
                   planner=planner, context=ctx)
    _wait(rid)
    assert asked[0].startswith("make it better") and "MEMORY-BLOCK" in asked[0]
    assert len(asked[0]) <= len("make it better") + SW.PLAN_CONTEXT_CHARS + 200
    assert prompts and all("MEMORY-BLOCK" in p for p in prompts)
    assert all(p.count("q" * 50) <= SW.WORKER_CONTEXT_CHARS // 50 for p in prompts)


def test_continue_resumes_only_the_unfinished_phases(runs):
    ran, broken = [], [True]

    def run_turn(sid, prompt):
        ran.append(prompt.split("\n", 1)[0])
        if prompt.startswith("build the api") and broken[0]:
            raise RuntimeError("that CLI died")
        yield {"type": "message", "text": "ok"}
        yield {"type": "done"}
    phases = [{"title": "Schema", "task": "design the schema", "needs": []},
              {"title": "API", "task": "build the api", "needs": [1]}]
    rid = SW.start("goal", ".", "opencode", _spawn, run_turn, phases=phases,
                   owner="conv1")
    st = _wait(rid)
    assert [a["state"] for a in st["agents"]][:2] == [SW.DONE, SW.FAILED]
    run = SW.last_run_for("conv1")
    assert run.id == rid and [a.title for a in SW.unfinished(run)] == ["API"]
    ran.clear()
    broken[0] = False
    assert SW.resume(rid, _spawn, run_turn) == rid
    st = _wait(rid)
    assert st["state"] == SW.DONE
    assert "design the schema" not in ran, "a finished phase ran again"
    assert "build the api" in ran
    assert SW.unfinished(SW.get(rid)) == []
    assert SW.resume(rid, _spawn, run_turn) is None     # nothing left to resume


def test_the_managers_verdict_sees_the_context_excerpt(runs):
    seen = []

    def manager(system, user, purpose, max_tokens):
        seen.append((purpose, user))
        return '{"ok": true}', 3

    def run_turn(sid, prompt):
        yield {"type": "message", "text": "done"}
        yield {"type": "done"}
    rid = SW.start("goal", ".", "opencode", _spawn, run_turn,
                   phases=[{"title": "Build", "task": "build it"}], review=False,
                   manager=manager, context="CTX-NOTE " + "c" * 5000)
    _wait(rid)
    verdicts = [u for p, u in seen if p == "verify"]
    assert verdicts and "CTX-NOTE" in verdicts[0]
    assert verdicts[0].count("c" * 50) <= SW.MANAGER_CONTEXT_CHARS // 50


class _Prev:
    id = "swarm-prev"


def test_the_multi_tier_resumes_on_continue_and_briefs_new_work(monkeypatch):
    import app as A
    calls = {"start": [], "resume": []}
    todo = [type("Ag", (), {"title": "API"})()]
    monkeypatch.setattr(A.swarm_windows, "last_run_for", lambda owner: _Prev())
    monkeypatch.setattr(A.swarm_windows, "unfinished", lambda run: todo)
    monkeypatch.setattr(A.swarm_windows, "format_result",
                        lambda rid: "Swarm run swarm-prev - failed\n### Phase 2 - API [failed]")
    monkeypatch.setattr(A.swarm_windows, "resume",
                        lambda rid, *a, **k: calls["resume"].append((rid, k)) or rid)
    monkeypatch.setattr(A.swarm_windows, "start",
                        lambda goal, *a, **k: calls["start"].append((goal, k)) or "swarm-new")
    monkeypatch.setattr(A.swarm_windows, "status",
                        lambda rid, with_events=False: {"state": SW.DONE, "agents": [],
                                                        "total": 1, "done": 1})
    monkeypatch.setattr(A, "_MULTI_POLL", 0.0)
    for name in ("note_turn", "remember_recent", "remember_fact"):
        monkeypatch.setattr(A.memory, name, lambda *a, **k: 2)
    monkeypatch.setattr(A.memory, "context_block", lambda *a, **k: "MEM: port 8080")
    A._MULTI_RUNS.clear()
    sess = {"cli": "codex", "project_dir": "C:/proj"}
    try:
        list(A._multi_turn_events("s1", sess, "continue"))
        assert [r[0] for r in calls["resume"]] == ["swarm-prev"] and not calls["start"]
        list(A._multi_turn_events("s1", sess, "now add a contact form"))
        goal, kw = calls["start"][0]
        assert goal == "now add a contact form"
        assert "MEM: port 8080" in kw["context"]
        assert "Phase 2 - API [failed]" in kw["context"]
        assert len(kw["context"]) <= SW.CONTEXT_CHARS
    finally:
        A._MULTI_RUNS.clear()


# --------------------------------------------------------------------------- #
# 5 + 7. Per-conversation recap for the quick chat and every /v1 CLI
# --------------------------------------------------------------------------- #

@pytest.fixture
def recaps(monkeypatch):
    import app as A
    d = tempfile.mkdtemp(prefix="hubrecap-")
    monkeypatch.setattr(A, "_RECAP_STORE_PATH", os.path.join(d, "recaps.json"))
    try:
        yield A
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_a_cli_swarm_request_uses_its_sessions_recap(recaps, monkeypatch):
    A = recaps
    A._recap_store.put("hdr:cli-sess-42", {"recap": "RECAP: header is sticky, blue theme"})
    seen = {}

    def fake_run(messages, dispatch, **kw):
        seen.update(kw)
        return {"text": "done", "plan": {}, "phases": [], "review": {}, "models": []}
    monkeypatch.setattr(A.swarm, "run", fake_run)
    monkeypatch.setattr(A, "_swarm_fast_path", lambda body, messages: False)
    monkeypatch.setattr(A, "_swarm_manager_kwargs", lambda: {})
    r = A.app.test_client().post(
        "/v1/chat/completions", headers={"X-Session-Id": "cli-sess-42"},
        json={"model": "swarm", "messages": FOLLOW_UP})
    assert r.status_code == 200
    assert "header is sticky" in seen.get("context", "")
    assert len(seen["context"]) <= A._PIPELINE_CONTEXT_CHARS


def test_without_a_recap_the_pipeline_gets_no_context_kwarg(recaps, monkeypatch):
    A = recaps
    seen = {}

    def fake_run(messages, dispatch, **kw):
        seen.update(kw)
        return {"text": "done", "plan": {}, "phases": [], "review": {}, "models": []}
    monkeypatch.setattr(A.swarm, "run", fake_run)
    monkeypatch.setattr(A, "_swarm_fast_path", lambda body, messages: False)
    monkeypatch.setattr(A, "_swarm_manager_kwargs", lambda: {})
    A.app.test_client().post("/v1/chat/completions", headers={"X-Session-Id": "none-here"},
                             json={"model": "swarm", "messages": FOLLOW_UP})
    assert "context" not in seen


def test_the_quick_chat_sends_its_id_and_its_recap_is_keyed_by_it(recaps, monkeypatch):
    A = recaps
    monkeypatch.setattr(A, "_has_control_token", lambda: True)
    monkeypatch.setattr(A.quick_history, "delete_conversation", lambda cid: True)
    html = open(os.path.join("templates", "index.html"), encoding="utf-8").read()
    assert "'X-Conversation-Id': 'quick-' + ensureChatConvId()" in html
    key = ctxwin.conversation_key({"X-Conversation-Id": "quick-c123abc"}, {}, None, [])
    assert key == ctxwin.quick_chat_key("c123abc") == "hdr:quick-c123abc"
    A._recap_store.put(key, {"recap": "quick chat recap"})
    assert A._conversation_recap(key) == "quick chat recap"
    r = A.app.test_client().delete(
        "/api/chat/history/c123abc",
        headers={"X-Free-LLM-Hub": "dashboard",
                 "X-Free-LLM-Hub-Token": A.config.get_control_token() or ""})
    assert r.status_code == 200
    assert A._recap_store.get(key) is None
