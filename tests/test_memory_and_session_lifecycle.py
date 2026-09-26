"""Memory and session lifecycle -- every way a turn starts, ends, dies,
is rewound or deleted, and whether what the hub remembers about it reaches
the agent on the next turn.

REQUESTED: "memory and session management must be perfect in all modes".
The audit that followed found nine gaps, one test group each below:

  1. a stopping place was only handed over on a scheduled restate turn, so
     "continue" after a stop on turn >= 2 never saw it;
  2. memory outlived deleted and pruned conversations forever;
  3. a rewind left memory describing the undone work;
  4. turns were counted before validation, and auto-continue nudges as turns;
  5. stopping a first turn lost the CLI's thread id;
  6. a turn the process died in left no stopping place at all;
  7. the plain route kept no memory, corrupt files were overwritten silently,
     and a locked file dropped a write without a word;
  8. the per-session brief could be committed into the user's repo;
  9. the memory block was a flat 2000 characters whatever the window.
"""
import json
import logging
import os
import shutil
import subprocess
import threading
import time

import pytest

import agentic_chat as ac
import agentic_history as ah
import app as A
import memory


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(memory._ROOT_ENV, str(tmp_path / "mem"))
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(tmp_path / "cfg" / "config.json"))
    monkeypatch.setattr(ac.vision_status, "status",
                        lambda: {"available": True, "providers": []})
    monkeypatch.setattr(ac, "test_verification_enabled", lambda: False)
    ac._EXCLUDED_REPOS.clear()
    yield
    ac._REGISTRY.clear()


# --------------------------------------------------------------------------- #
# Fakes: a session in the registry, and a CLI process
# --------------------------------------------------------------------------- #

class _FakeSession:
    def __init__(self, cli_id="claude", project_dir="."):
        self.id = None
        self.cli_id = cli_id
        self.project_dir = project_dir
        self.native_session_id = None
        self.turn_count = 0
        self.created_at = time.time()
        self.proc = None
        self.proc_lock = threading.Lock()
        self.turn_lock = threading.Lock()
        self.last_interrupted = False
        self.tools_notified = True


class _StreamProc:
    """stream-json lines; `on_line(i)` runs before line i is handed out."""
    def __init__(self, lines, on_line=None):
        self._lines = list(lines)
        self._i = 0
        self._on_line = on_line
        self.stderr = iter(())
        self.returncode = 0
        self.pid = 4242

    @property
    def stdout(self):
        return self

    def __iter__(self):
        return self

    def __next__(self):
        if self._on_line:
            self._on_line(self._i)
        if self._i >= len(self._lines):
            raise StopIteration
        self._i += 1
        return self._lines[self._i - 1]

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode


def _claude_lines(text, thread="THREAD-1"):
    return ['{"type":"system","session_id":"%s"}\n' % thread,
            json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": text}]}}) + "\n",
            json.dumps({"type": "result", "subtype": "success", "result": text}) + "\n"]


@pytest.fixture
def registered(monkeypatch, tmp_path):
    monkeypatch.setattr(ac, "master_enabled", lambda: True)
    monkeypatch.setattr(ac, "_master_on", lambda: True)
    monkeypatch.setattr(ac, "_should_check_binary_identity", lambda s: False)
    monkeypatch.setattr(ac, "_resolve_bin", lambda cli: "/fake/" + cli)
    monkeypatch.setattr(ac.workspace, "missing_tools_message", lambda d: None)
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)

    def make(cli_id="claude"):
        sess = _FakeSession(cli_id=cli_id, project_dir=str(proj))
        sess.id = "lifecycle-" + cli_id + "-" + str(id(sess))
        ac._REGISTRY[sess.id] = sess
        return sess.id, sess
    return make


def _hdr():
    return {"X-Free-LLM-Hub": "dashboard",
            "X-Free-LLM-Hub-Token": A.config.get_control_token() or ""}


# --------------------------------------------------------------------------- #
# 1. The stopping place and the list reach the agent
# --------------------------------------------------------------------------- #

def test_a_stop_makes_the_next_turn_due():
    sid = "s-stop"
    for _ in range(3):
        memory.note_turn(sid)
    memory.mark_rules_restated(sid)
    assert not memory.should_restate_rules(sid)
    memory.note_interrupted(sid, request="build the shop", doing=["write index.html"])
    assert memory.should_restate_rules(sid), "a stop on turn 3 waited for turn 8"


def test_continue_after_a_stop_on_turn_two_carries_the_resume_note(tmp_path):
    proj = tmp_path / "shop"
    proj.mkdir()
    sess = ac._Session("codex", str(proj))
    sess.native_session_id = "THREAD"            # turn >= 2: a thread exists
    memory.note_turn(sess.id)
    memory.note_turn(sess.id)
    memory.mark_rules_restated(sess.id)
    assert ac._build_argv_codex(sess, "codex", "hello")[-1] == "hello"
    memory.note_interrupted(sess.id, request="build the shop in Fez",
                            doing=["edit index.html"], partial="Writing the menu")
    prompt = ac._build_argv_codex(sess, "codex", "continue")[-1]
    assert "Standing instruction" in prompt
    brief = (proj / ac.brief_filename(sess.id)).read_text(encoding="utf-8")
    assert "YOUR PREVIOUS TURN WAS STOPPED BY THE USER" in brief
    assert "build the shop in Fez" in brief and "edit index.html" in brief
    # delivered once; the next ordinary turn is quiet again
    assert ac._build_argv_codex(sess, "codex", "and the footer")[-1] == "and the footer"


def test_a_list_edited_between_turns_is_handed_over(tmp_path):
    sid, proj = "s-list", tmp_path
    (proj / "PROGRESS.md").write_text("- [x] header\n- [ ] menu\n", encoding="utf-8")
    memory.update_tasks(sid, "", str(proj), seen=True)      # the agent wrote it
    memory.note_turn(sid)
    memory.mark_rules_restated(sid)
    ac._memory_turn_start(sid, "next thing", str(proj))
    assert not memory.should_restate_rules(sid), "the agent's own list is not news"
    (proj / "PROGRESS.md").write_text("- [x] header\n- [ ] menu\n- [ ] contact page\n",
                                      encoding="utf-8")
    ac._memory_turn_start(sid, "next thing", str(proj))
    assert memory.should_restate_rules(sid)
    memory.mark_rules_restated(sid)
    assert not memory.should_restate_rules(sid)


def test_saying_continue_delivers_the_open_list():
    sid = "s-continue"
    memory.set_tasks(sid, [{"text": "header", "done": True}, {"text": "menu"}], seen=True)
    memory.note_turn(sid)
    memory.mark_rules_restated(sid)
    ac._memory_turn_start(sid, "Continue please", None)
    assert memory.should_restate_rules(sid)


def test_continue_with_nothing_open_is_an_ordinary_turn():
    sid = "s-nothing"
    memory.set_tasks(sid, [{"text": "header", "done": True}, {"text": "menu", "done": True}],
                     seen=True)
    memory.note_turn(sid)
    memory.mark_rules_restated(sid)
    ac._memory_turn_start(sid, "continue", None)
    assert not memory.should_restate_rules(sid)


def test_the_resume_words():
    for yes in ("continue", "Continue.", "please continue", "resume", "keep going",
                "reprends", "vas-y", "Continuer le travail", "ok, continue"):
        assert memory.asks_to_resume(yes), yes
    for no in ("fix the header", "continuous integration setup", "", None):
        assert not memory.asks_to_resume(no), no


def test_a_first_turn_marks_what_it_delivered(tmp_path):
    """A first turn ships the memory without asking _due_for_restate, so it
    has to mark the list seen itself, or turn 2 would ship it again."""
    sess = ac._Session("codex", str(tmp_path))
    memory.set_tasks(sess.id, [{"text": "a"}, {"text": "b"}])       # not seen
    ac._build_argv_codex(sess, "codex", "build it")
    sess.native_session_id = "T"
    memory.note_turn(sess.id)
    assert ac._build_argv_codex(sess, "codex", "go on with b")[-1] == "go on with b"


# --------------------------------------------------------------------------- #
# 2. Memory dies with its conversation
# --------------------------------------------------------------------------- #

def test_deleting_a_conversation_forgets_its_memory_and_brief(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    sid = "0123456789abcdef0123456789abcdef"
    ah.record_turn(sid, "codex", str(proj), "user", "hi")
    memory.remember_fact(sid, "decided: vite")
    memory.begin_inflight(sid, "hi", str(proj))
    name = ac.write_task_brief(str(proj), "hi", session_id=sid)
    assert (proj / name).exists()
    memory.remember_project_fact(str(proj), "this repo uses pnpm")
    assert ah.delete_conversation(sid)
    assert not os.path.exists(memory._path(sid))
    assert memory.inflight(sid) is None
    assert not (proj / name).exists()
    assert memory.project_facts(str(proj)) == ["this repo uses pnpm"], "project memory stays"


def test_retention_pruning_forgets_too(monkeypatch):
    calls = []
    monkeypatch.setattr(ah, "_FORGET_HOOKS", [lambda sid, pd: calls.append((sid, pd))])
    monkeypatch.setattr(ah, "MAX_CONVERSATIONS", 1)
    ah.record_turn("old-1", "codex", "P1", "user", "a")
    time.sleep(0.01)
    ah.record_turn("new-1", "codex", "P2", "user", "b")
    assert calls == [("old-1", "P1")]


def test_the_app_registers_the_hook():
    assert A._forget_conversation_state in ah._FORGET_HOOKS


def test_orphaned_memory_is_pruned_but_projects_and_global_stay(tmp_path):
    for sid in ("orphan", "kept", "fresh", "busy"):
        memory.remember_fact(sid, "x")
    memory.begin_inflight("busy", "still going")
    memory.remember_project_fact(str(tmp_path), "p")
    memory.remember_fact(memory.GLOBAL_KEY, "g")
    root = memory._root()
    old = time.time() - 40 * 86400
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if os.path.isfile(path) and name != "fresh.json":
            os.utime(path, (old, old))
    assert memory.prune_orphans({"kept"}, max_age_days=30) == ["orphan"]
    left = set(os.listdir(root))
    assert {"kept.json", "fresh.json", "busy.json", memory.GLOBAL_KEY + ".json"} <= left
    assert any(n.startswith("proj-") for n in left)


def test_boot_recovers_and_prunes_after_the_single_instance_claim():
    src = open("app.py", encoding="utf-8").read()
    main = src[src.index('if __name__ == "__main__":'):]
    assert main.index("_claim_single_instance()") < main.index("target=_recover_memory_state")
    body = src[src.index("def _recover_memory_state("):]
    body = body[:body.index("\ndef ") if "\ndef " in body else len(body)]
    assert "memory.recover_inflight()" in body
    assert "memory.prune_orphans(agentic_history.known_session_ids()" in body


# --------------------------------------------------------------------------- #
# 3. A rewind rewinds the memory
# --------------------------------------------------------------------------- #

def _two_turns(sid, proj):
    memory.note_turn(sid)
    memory.remember_recent(sid, "turn one", "user")
    memory.remember_recent(sid, "done one", "agent")
    memory.set_tasks(sid, [{"text": "a"}], seen=True)
    time.sleep(0.02)
    cut = time.time()
    time.sleep(0.02)
    memory.note_turn(sid)
    memory.remember_recent(sid, "turn two", "user")
    memory.remember_summary(sid, "we built the cart")
    memory.remember_fact(sid, "the cart uses redux", project_dir=str(proj))
    memory.set_tasks(sid, [{"text": "a", "done": True}, {"text": "cart"}])
    memory.note_interrupted(sid, request="turn two")
    return cut


def test_rewind_drops_what_the_undone_turns_left(tmp_path):
    sid = "rw-1"
    cut = _two_turns(sid, tmp_path)
    assert memory.rewind(sid, cutoff=cut, request="turn two", kept_turns=1,
                         project_dir=str(tmp_path))
    mem = memory.get(sid)
    assert [r["text"] for r in mem["recent"]] == ["turn one", "done one"]
    assert mem["summary"] == ""
    assert "the cart uses redux" not in mem["facts"]
    assert mem["tasks"] == []
    assert mem["interrupted"] is None
    assert mem["turns"] == 1
    assert memory.should_restate_rules(sid)
    block = memory.resume_block(sid)
    assert "REWOUND" in block and "turn two" in block


def test_rewind_takes_the_restored_progress_file(tmp_path):
    sid = "rw-2"
    cut = _two_turns(sid, tmp_path)
    (tmp_path / "PROGRESS.md").write_text("- [ ] a\n- [ ] b\n", encoding="utf-8")
    memory.rewind(sid, cutoff=cut, kept_turns=1, project_dir=str(tmp_path))
    assert [t["text"] for t in memory.tasks(sid)] == ["a", "b"]


def test_a_finished_turn_after_a_rewind_drops_the_note(tmp_path):
    sid = "rw-3"
    memory.rewind(sid, cutoff=time.time(), request="x", kept_turns=0)
    assert memory.get(sid)["rewound"]
    memory.clear_interrupted(sid)
    assert memory.get(sid)["rewound"] is None


def test_the_rewind_route_rewinds_the_memory(tmp_path, monkeypatch):
    proj = tmp_path / "rw"
    proj.mkdir()
    sid = "rewind-route-1"
    monkeypatch.setattr(A, "_agent_gate", lambda: None)
    monkeypatch.setattr(A.snapshots, "restore", lambda *a, **k: True)
    ah.record_turn(sid, "codex", str(proj), "user", "first", snapshot="s1")
    memory.note_turn(sid)
    memory.remember_recent(sid, "first", "user")
    time.sleep(0.02)
    ah.record_turn(sid, "codex", str(proj), "agent", "did first")
    ah.record_turn(sid, "codex", str(proj), "user", "second", snapshot="s2")
    time.sleep(0.02)
    memory.note_turn(sid)
    memory.remember_recent(sid, "second", "user")
    memory.note_interrupted(sid, request="second")
    r = A.app.test_client().post("/api/agent/history/%s/rewind" % sid,
                                 json={"index": 2}, headers=_hdr())
    assert r.status_code == 200, r.get_data(as_text=True)
    mem = memory.get(sid)
    assert [x["text"] for x in mem["recent"]] == ["first"]
    assert mem["interrupted"] is None and mem["turns"] == 1
    assert mem["rewound"]["request"] == "second"


# --------------------------------------------------------------------------- #
# 4. Only real turns are counted
# --------------------------------------------------------------------------- #

def test_a_refused_turn_is_not_a_turn(registered):
    sid, sess = registered()
    sess.turn_lock.acquire()
    try:
        evs = list(ac.send_message_stream(sid, "from a second tab"))
    finally:
        sess.turn_lock.release()
    assert evs[-1]["status"] == 409
    assert list(ac.send_message_stream(sid, "   "))[-1]["status"] == 400
    mem = memory.get(sid)
    assert mem["turns"] == 0 and mem["recent"] == []


def test_a_busy_session_is_refused_without_being_touched(registered):
    sid, sess = registered()
    sess.turn_lock.acquire()
    try:
        evs = list(ac.send_message_stream_durable(sid, "from a second tab"))
    finally:
        sess.turn_lock.release()
    assert [e.get("status") for e in evs] == [409]
    assert memory.interrupted(sid) is None, "a 409 is not the running turn's stopping place"
    assert memory.inflight(sid) is None


def test_nudges_are_not_user_turns(registered, monkeypatch):
    sid, sess = registered()
    replies = iter(["Step one done.\n- [x] a\n- [ ] b", "All done."])
    monkeypatch.setattr(ac.subprocess, "Popen",
                        lambda argv, **kw: _StreamProc(_claude_lines(next(replies))))
    list(ac.send_message_stream_durable(sid, "build it"))
    assert next(replies, None) is None, "the auto-continue nudge did run"
    mem = memory.get(sid)
    assert mem["turns"] == 1
    assert [r["text"] for r in mem["recent"] if r["role"] == "user"] == ["build it"]
    assert memory.inflight(sid) is None, "a turn that ended leaves no marker"


def test_the_routes_refuse_before_recording(registered, monkeypatch):
    sid, sess = registered()
    monkeypatch.setattr(A, "_agent_gate", lambda: None)
    c = A.app.test_client()
    sess.turn_lock.acquire()
    try:
        r = c.post("/api/agent/sessions/%s/message/stream" % sid,
                   json={"text": "second tab"}, headers=_hdr())
        body = r.get_data(as_text=True)
        r2 = c.post("/api/agent/sessions/%s/message" % sid,
                    json={"text": "second tab"}, headers=_hdr())
    finally:
        sess.turn_lock.release()
    assert '"status": 409' in body
    assert r2.status_code == 409
    assert ah.get_conversation(sid) is None, "a refused send is not a user turn"


# --------------------------------------------------------------------------- #
# 5. The CLI thread survives a stopped first turn
# --------------------------------------------------------------------------- #

def test_stopping_the_first_turn_keeps_the_thread(registered, monkeypatch):
    sid, sess = registered()
    ah.record_turn(sid, "claude", sess.project_dir, "user", "build it")

    def stop_after_init(i):
        if i == 1:
            sess.last_interrupted = True        # Stop pressed after the init line
            proc._i = len(proc._lines)
    proc = _StreamProc(_claude_lines("never finished", thread="THREAD-FIRST"),
                       on_line=stop_after_init)
    monkeypatch.setattr(ac.subprocess, "Popen", lambda argv, **kw: proc)
    evs = list(ac.send_message_stream(sid, "build it"))
    assert evs[-1] == {"event": "stopped"}
    assert sess.native_session_id == "THREAD-FIRST"
    assert ah.get_conversation(sid)["native_session_id"] == "THREAD-FIRST"


def test_resume_prefers_the_newest_thread():
    src = open("app.py", encoding="utf-8").read()
    body = src[src.index("def api_agent_resume_session("):]
    body = body[:body.index("\ndef ")]
    assert 'native = conv.get("native_session_id")\n    if not native:' in body.replace("\r\n", "\n")


# --------------------------------------------------------------------------- #
# 6. A turn the process died in is resumed at boot
# --------------------------------------------------------------------------- #

def _mark_dead(sid):
    rec = memory.inflight(sid)
    rec["pid"] = -1
    with open(memory._inflight_path(sid), "w", encoding="utf-8") as fh:
        json.dump(rec, fh)


def test_a_turn_the_process_died_in_is_resumed_at_boot():
    sid = "died-1"
    memory.note_turn(sid)
    memory.mark_rules_restated(sid)
    memory.begin_inflight(sid, "build the shop")
    memory.touch_inflight(sid, doing=["write index.html"], partial="Writing", force=True)
    _mark_dead(sid)
    assert memory.recover_inflight() == [sid]
    cut = memory.interrupted(sid)
    assert cut["why"] == "hub restarted"
    assert cut["request"] == "build the shop" and cut["doing"] == ["write index.html"]
    assert memory.inflight(sid) is None
    assert memory.should_restate_rules(sid)
    assert "CUT SHORT (hub restarted)" in memory.resume_block(sid)


def test_a_marker_of_this_process_is_left_alone():
    memory.begin_inflight("alive-1", "running now")
    assert memory.recover_inflight() == []
    assert memory.inflight("alive-1") is not None


def test_touching_is_throttled():
    memory.begin_inflight("t-1", "x")
    assert memory.touch_inflight("t-1", doing=["a"]) is False
    assert memory.touch_inflight("t-1", doing=["a"], force=True) is True
    assert memory.touch_inflight("never-started", doing=["a"], force=True) is False


# --------------------------------------------------------------------------- #
# 7. No silent loss
# --------------------------------------------------------------------------- #

class _PlainProc:
    def __init__(self, stdout, on_communicate=None):
        self._stdout = stdout
        self._cb = on_communicate
        self.returncode = 0
        self.pid = 4343

    def communicate(self, timeout=None):
        if self._cb:
            self._cb()
        return self._stdout, ""

    def poll(self):
        return self.returncode


def test_the_plain_route_keeps_memory(registered, monkeypatch):
    sid, sess = registered()
    out = json.dumps({"type": "result", "subtype": "success", "session_id": "T-PLAIN",
                      "result": "Header done.\n- [x] header\n- [ ] menu"})
    monkeypatch.setattr(ac.subprocess, "Popen", lambda argv, **kw: _PlainProc(out))
    status, text, _detail = ac.send_message(sid, "build it")
    assert status == 200
    mem = memory.get(sid)
    assert mem["turns"] == 1
    assert [r["role"] for r in mem["recent"]] == ["user", "agent"]
    assert [t["text"] for t in mem["tasks"]] == ["header", "menu"]
    assert memory.inflight(sid) is None

    monkeypatch.setattr(ac.subprocess, "Popen", lambda argv, **kw: _PlainProc(
        "", on_communicate=lambda: setattr(sess, "last_interrupted", True)))
    status, _t, _d = ac.send_message(sid, "now the menu")
    assert status == 499
    assert memory.interrupted(sid)["why"] == "stopped"
    assert memory.interrupted(sid)["request"] == "now the menu"


def test_a_corrupt_memory_file_is_kept_aside(caplog):
    path = memory._path("c1")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{ not json")
    with caplog.at_level(logging.WARNING, logger="free-llm-hub"):
        assert memory.get("c1")["facts"] == []
    backups = [n for n in os.listdir(memory._root()) if n.startswith("c1.json.corrupt-")]
    assert len(backups) == 1
    with open(os.path.join(memory._root(), backups[0]), encoding="utf-8") as fh:
        assert fh.read() == "{ not json"
    assert any("unreadable" in r.getMessage() for r in caplog.records)
    assert memory.remember_fact("c1", "starts fresh")
    assert memory.get("c1")["facts"] == ["starts fresh"]


def test_a_briefly_locked_file_is_retried(monkeypatch):
    real, calls = os.replace, [0]

    def flaky(a, b):
        calls[0] += 1
        if calls[0] < 3:
            raise PermissionError(32, "being used by another process")
        return real(a, b)
    monkeypatch.setattr(memory.os, "replace", flaky)
    assert memory.remember_fact("l1", "kept")
    monkeypatch.setattr(memory.os, "replace", real)
    assert memory.get("l1")["facts"] == ["kept"]


def test_a_file_that_stays_locked_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(memory, "_IO_RETRY_SLEEPS", (0, 0, 0, 0, 0))

    def locked(a, b):
        raise PermissionError(32, "being used by another process")
    monkeypatch.setattr(memory.os, "replace", locked)
    with caplog.at_level(logging.WARNING, logger="free-llm-hub"):
        assert memory.remember_fact("l2", "lost") is False
    assert any("NOT saved" in r.getMessage() for r in caplog.records)


def test_a_corrupt_history_index_is_rebuilt_not_emptied():
    ah.record_turn("h1", "codex", "P", "user", "hello")
    ah.record_turn("h2", "codex", "P", "user", "again")
    with open(ah._index_path(), "w", encoding="utf-8") as fh:
        fh.write("{broken")
    assert {r["session_id"] for r in ah.list_conversations()} == {"h1", "h2"}
    assert any(n.startswith("index.json.corrupt-") for n in os.listdir(ah._root()))


def test_a_corrupt_transcript_is_kept_aside():
    ah.record_turn("h3", "codex", "P", "user", "hello")
    with open(ah._conv_path("h3"), "w", encoding="utf-8") as fh:
        fh.write("not json")
    ah.record_turn("h3", "codex", "P", "user", "again")
    assert any(n.startswith("h3.json.corrupt-") for n in os.listdir(ah._root()))
    assert [t["text"] for t in ah.get_conversation("h3")["turns"]] == ["again"]


# --------------------------------------------------------------------------- #
# 8. The brief never ends up in the user's commits
# --------------------------------------------------------------------------- #

def test_the_brief_is_excluded_locally_not_in_gitignore(tmp_path):
    proj = tmp_path / "repo"
    (proj / ".git" / "info").mkdir(parents=True)
    ac.write_task_brief(str(proj), "build", session_id="abcdef1234567890")
    ac._EXCLUDED_REPOS.clear()
    ac.write_task_brief(str(proj), "build", session_id="abcdef1234567890")
    exclude = (proj / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert exclude.count(".calvoun-brief*.md") == 1
    assert not (proj / ".gitignore").exists()


def test_a_worktree_writes_to_the_common_dir(tmp_path):
    common = tmp_path / "main" / ".git"
    wtdir = common / "worktrees" / "wt"
    wtdir.mkdir(parents=True)
    (wtdir / "commondir").write_text("../..", encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: " + str(wtdir), encoding="utf-8")
    ac.write_task_brief(str(wt), "build", session_id="abcdef1234567890")
    assert ".calvoun-brief*.md" in (common / "info" / "exclude").read_text(encoding="utf-8")


@pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
def test_git_really_ignores_it(tmp_path):
    proj = tmp_path / "real"
    proj.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(proj), check=True)
    name = ac.write_task_brief(str(proj), "build", session_id="abcdef1234567890")
    (proj / "index.html").write_text("<p>hi</p>", encoding="utf-8")
    out = subprocess.run(["git", "status", "--porcelain"], cwd=str(proj),
                         capture_output=True, text=True, check=True).stdout
    assert "index.html" in out and name not in out


def test_the_brief_goes_when_the_session_is_deleted(tmp_path):
    name = ac.write_task_brief(str(tmp_path), "build", session_id="abcdef1234567890")
    assert ac.remove_task_brief(str(tmp_path), "abcdef1234567890")
    assert not (tmp_path / name).exists()
    ac.write_task_brief(str(tmp_path), "build")                     # the shared one
    assert not ac.remove_task_brief(str(tmp_path), None)
    assert (tmp_path / ac.BRIEF_FILENAME).exists()


# --------------------------------------------------------------------------- #
# 9. The memory block follows the window, modestly
# --------------------------------------------------------------------------- #

def test_the_budget_scales_modestly():
    assert memory.budget_for_window(None) == 2000
    assert memory.budget_for_window(0) == 2000
    assert memory.budget_for_window(128000) == 4000
    assert memory.budget_for_window(200000) == 6000
    assert memory.budget_for_window(2000000) == 6000
    assert memory.budget_for_window(8000) == 1600
    for w in (4000, 8000, 32000, 128000, 200000, 2000000):
        assert memory.budget_for_window(w) <= w * 0.05 * 4


def test_the_session_window_sizes_the_block(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(memory, "context_block",
                        lambda sid, budget_chars=0, **k: seen.append(budget_chars) or "")
    ac._memory_block(ac._Session("claude", str(tmp_path)))
    ac._memory_block(ac._Session("codex", str(tmp_path)))
    assert seen == [6000, 4000]
