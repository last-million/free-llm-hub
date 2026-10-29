"""The CLI's own to-do list reaches the hub's task board (app `_cli_todo_sync`,
flag `cli_todo_sync`).

Hermetic: synthetic conversations only, a temp taskboard file per test (never
the real ~/.free-llm-hub board), no network, no real CLI. Shapes are the ones
read from the installed CLIs on 2026-10-29 (see the section comment in app.py):
opencode `todowrite`, Claude Code `TodoWrite` and `TaskCreate` / `TaskUpdate`,
codex `update_plan`.
"""
import json
import logging
import os

import pytest

import app as A
import config
import taskboard


# --------------------------------------------------------------------------- #
# fixtures and builders
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def board(tmp_path):
    """A fresh board backed by a temp file; the memo of synced lists cleared."""
    path = str(tmp_path / "board" / "taskboard.json")
    taskboard.configure(path)
    A._CTD_MEMO.clear()
    yield taskboard.default
    taskboard.configure(None)
    A._CTD_MEMO.clear()


@pytest.fixture
def proj(tmp_path):
    d = tmp_path / "myproject"
    d.mkdir()
    return str(d)


def _env_sys(folder):
    return {"role": "system",
            "content": "You are opencode.\n<env>\nWorking directory: %s\n"
                       "Is directory a git repo: No\nPlatform: win32\n</env>" % folder}


def _user(text):
    return {"role": "user", "content": text}


def _call(name, args, cid="c1", as_string=True):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": cid, "type": "function",
        "function": {"name": name,
                     "arguments": json.dumps(args) if as_string else args}}]}


def _result(cid, text):
    return {"role": "tool", "tool_call_id": cid, "content": text}


def _todos(*pairs):
    return {"todos": [{"content": t, "status": s, "priority": "medium"} for t, s in pairs]}


def _sync(messages, headers=None, body=None):
    hdrs = {"X-Session-Id": "ses_test_1"} if headers is None else headers
    with A.app.test_request_context("/v1/chat/completions", method="POST", headers=hdrs):
        A._cli_todo_sync(messages, body or {})


def _opencode(proj, todos, instruction="build the invoice exporter"):
    return [_env_sys(proj), _user(instruction), _call("todowrite", todos),
            _result("c1", "todo list updated")]


def _by_title(board_):
    return {t["title"]: t for t in board_.tasks()}


# --------------------------------------------------------------------------- #
# the three CLI shapes (unit)
# --------------------------------------------------------------------------- #

def test_opencode_todowrite_creates_a_goal_and_one_task_per_item(board, proj):
    msgs = _opencode(proj, {"todos": [
        {"content": "Read the CSV module", "status": "completed", "priority": "high"},
        {"content": "Write the exporter", "status": "in_progress", "priority": "high"},
        {"content": "Add tests", "status": "pending", "priority": "low"},
        {"content": "Port to Excel", "status": "cancelled", "priority": "medium"},
    ]})
    _sync(msgs)
    goals = board.goals(project_dir=proj)
    assert len(goals) == 1
    assert goals[0]["title"] == "[CLI] build the invoice exporter"
    tasks = _by_title(board)
    assert {k: v["status"] for k, v in tasks.items()} == {
        "Read the CSV module": "done", "Write the exporter": "doing",
        "Add tests": "todo", "Port to Excel": "blocked"}
    assert all(t["goal_id"] == goals[0]["id"] for t in tasks.values())
    assert tasks["Write the exporter"]["priority"] == 2
    assert tasks["Add tests"]["priority"] == 0
    notes = [h["note"] for h in tasks["Port to Excel"]["history"]]
    assert "cancelled in the CLI" in notes
    # the goal brief now names what remains
    brief = board.goal_brief(project_dir=proj)
    assert "Write the exporter [doing]" in brief and "Add tests" in brief
    assert "Read the CSV module" not in brief


def test_claude_code_todowrite_with_dict_arguments(board, proj):
    msgs = [_env_sys(proj), _user("refactor the parser"),
            _call("TodoWrite", {"todos": [
                {"content": "Split the lexer", "status": "completed",
                 "activeForm": "Splitting the lexer"},
                {"content": "Rename tokens", "status": "in_progress",
                 "activeForm": "Renaming tokens"}]}, as_string=False)]
    _sync(msgs, headers={"X-Claude-Code-Session-Id": "cc-session-1"})
    assert {k: v["status"] for k, v in _by_title(board).items()} == {
        "Split the lexer": "done", "Rename tokens": "doing"}


def test_claude_code_task_tools_are_replayed_like_claude_code_does(board, proj):
    msgs = [_env_sys(proj), _user("make the release"),
            _call("TaskCreate", {"subject": "Bump the version", "description": "x"}, cid="a"),
            _result("a", "Task #1 created successfully: Bump the version"),
            _call("TaskCreate", {"subject": "Write the changelog"}, cid="b"),
            _result("b", "Task #2 created successfully: Write the changelog"),
            _call("TaskCreate", {"subject": "Tweet about it"}, cid="c"),
            _result("c", "Task #3 created successfully: Tweet about it"),
            _call("TaskUpdate", {"taskId": "1", "status": "completed"}, cid="d"),
            _result("d", "Updated task #1 status"),
            _call("TaskUpdate", {"taskId": "2", "status": "in_progress"}, cid="e"),
            _call("TaskUpdate", {"taskId": "3", "status": "deleted"}, cid="f"),
            _call("TaskUpdate", {"taskId": "99", "status": "completed"}, cid="g"),
            _call("TaskCreate", {"subject": "Tag the commit"}, cid="h")]   # no result yet
    _sync(msgs, headers={"X-Claude-Code-Session-Id": "cc-session-2"})
    assert {k: v["status"] for k, v in _by_title(board).items()} == {
        "Bump the version": "done", "Write the changelog": "doing",
        "Tag the commit": "todo"}


def test_codex_update_plan_with_environment_context(board, proj):
    env = ("<environment_context>\n  <cwd>%s</cwd>\n  <approval_policy>never"
           "</approval_policy>\n  <shell>powershell</shell>\n</environment_context>" % proj)
    msgs = [{"role": "system", "content": "You are Codex."},
            _user(env),
            _user("add pagination to the API"),
            _call("update_plan", {"explanation": "three steps", "plan": [
                {"step": "Read the handlers", "status": "completed"},
                {"step": "Add the cursor", "status": "in_progress"},
                {"step": "Update the docs", "status": "pending"}]}),
            _result("c1", "Plan updated")]
    _sync(msgs, headers={}, body={"prompt_cache_key": "thread-abc"})
    goals = board.goals(project_dir=proj)
    assert len(goals) == 1 and goals[0]["title"] == "[CLI] add pagination to the API"
    assert {k: v["status"] for k, v in _by_title(board).items()} == {
        "Read the handlers": "done", "Add the cursor": "doing", "Update the docs": "todo"}


@pytest.mark.parametrize("name,want", [
    ("todowrite", "todowrite"), ("TodoWrite", "todowrite"),
    ("mcp__hub__TodoWrite", "todowrite"), ("functions.update_plan", "update_plan"),
    ("ns/update_plan", "update_plan"), ("server:todowrite", "todowrite"),
    ("TASKCREATE", "taskcreate")])
def test_tool_names_match_case_insensitively_and_without_prefixes(name, want):
    assert A._ctd_tool_name(name) == want


def test_a_prefixed_name_syncs_too(board, proj):
    msgs = [_env_sys(proj), _user("ship it"),
            _call("mcp__cli__TodoWrite", _todos(("Check CI", "pending")))]
    _sync(msgs)
    assert list(_by_title(board)) == ["Check CI"]


# --------------------------------------------------------------------------- #
# re-sync: update, never duplicate
# --------------------------------------------------------------------------- #

def test_a_resync_updates_instead_of_duplicating(board, proj):
    v1 = _opencode(proj, _todos(("Parse input", "pending"), ("Emit output", "pending")))
    _sync(v1)
    assert len(board.tasks()) == 2
    v2 = v1 + [_call("todowrite", _todos(("Parse input", "completed"),
                                         ("Emit output", "in_progress"),
                                         ("Write docs", "pending")), cid="c2")]
    _sync(v2)
    tasks = _by_title(board)
    assert len(board.tasks()) == 3
    assert tasks["Parse input"]["status"] == "done"
    assert tasks["Emit output"]["status"] == "doing"
    assert tasks["Write docs"]["status"] == "todo"
    assert len(board.goals(project_dir=proj)) == 1
    # the same list again: nothing changes; after a restart (memo gone) the
    # persistent links still find the tasks
    _sync(v2)
    A._CTD_MEMO.clear()
    _sync(v2)
    assert len(board.tasks()) == 3


def test_links_survive_a_reload_of_the_board_file(board, proj, tmp_path):
    msgs = _opencode(proj, _todos(("Alpha", "pending")))
    _sync(msgs)
    taskboard.configure(board.path)             # the file read back from disk
    A._CTD_MEMO.clear()
    msgs2 = msgs + [_call("todowrite", _todos(("Alpha", "completed")), cid="c2")]
    _sync(msgs2)
    tasks = taskboard.default.tasks()
    assert len(tasks) == 1 and tasks[0]["status"] == "done"
    assert tasks[0]["ext"]["key"].startswith("cli-todo:")


def test_a_board_edit_stands_while_the_cli_has_not_changed_that_item(board, proj):
    v1 = _opencode(proj, _todos(("Alpha", "pending"), ("Beta", "pending")))
    _sync(v1)
    alpha = _by_title(board)["Alpha"]
    board.update(alpha["id"], status="done", note="done by hand")
    v2 = v1 + [_call("todowrite", _todos(("Alpha", "pending"), ("Beta", "completed")),
                     cid="c2")]
    _sync(v2)
    tasks = _by_title(board)
    assert tasks["Alpha"]["status"] == "done"        # the CLI did not touch Alpha
    assert tasks["Beta"]["status"] == "done"


def test_cancelled_later_is_blocked_with_the_note(board, proj):
    v1 = _opencode(proj, _todos(("Gamma", "in_progress")))
    _sync(v1)
    v2 = v1 + [_call("todowrite", _todos(("Gamma", "cancelled")), cid="c2")]
    _sync(v2)
    t = _by_title(board)["Gamma"]
    assert t["status"] == "blocked"
    assert t["history"][-1]["note"] == "cancelled in the CLI"


def test_items_that_vanish_from_the_list_are_left_as_they_are(board, proj):
    v1 = _opencode(proj, _todos(("Keep me", "in_progress"), ("Other", "pending")))
    _sync(v1)
    v2 = v1 + [_call("todowrite", _todos(("Other", "completed")), cid="c2")]
    _sync(v2)
    tasks = _by_title(board)
    assert tasks["Keep me"]["status"] == "doing"
    assert tasks["Other"]["status"] == "done"


def test_the_folders_open_goal_is_used(board, proj):
    gid = board.add_goal("Ship v2 of the exporter", project_dir=proj)
    _sync(_opencode(proj, _todos(("One", "pending"))))
    assert len(board.goals(project_dir=proj)) == 1
    assert board.tasks()[0]["goal_id"] == gid


def test_two_conversations_keep_their_own_links(board, proj):
    _sync(_opencode(proj, _todos(("Same text", "pending"))), headers={"X-Session-Id": "s1"})
    _sync(_opencode(proj, _todos(("Same text", "completed"))), headers={"X-Session-Id": "s2"})
    assert sorted(t["status"] for t in board.tasks()) == ["done", "todo"]


# --------------------------------------------------------------------------- #
# arguments, bounds
# --------------------------------------------------------------------------- #

def test_json_string_arguments_even_double_encoded(board, proj):
    inner = json.dumps(_todos(("Encoded twice", "in_progress")))
    msgs = [_env_sys(proj), _user("go"),
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "todowrite", "arguments": json.dumps(inner)}}]}]
    _sync(msgs)
    assert _by_title(board)["Encoded twice"]["status"] == "doing"


def test_the_latest_call_wins_and_broken_arguments_are_skipped(board, proj):
    msgs = [_env_sys(proj), _user("go"),
            _call("todowrite", _todos(("Old list item", "pending")), cid="a"),
            _call("todowrite", _todos(("New list item", "pending")), cid="b"),
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "z", "type": "function",
                "function": {"name": "todowrite", "arguments": "{not json at all"}}]}]
    _sync(msgs)
    assert list(_by_title(board)) == ["New list item"]


def test_at_most_fifty_items_and_titles_are_clipped(board, proj):
    long = "x" * 900
    todos = {"todos": [{"content": "item %02d" % i, "status": "pending"} for i in range(80)]
             + [{"content": long, "status": "pending"}]}
    _sync(_opencode(proj, todos))
    tasks = board.tasks()
    assert len(tasks) == 50
    assert all(len(t["title"]) <= 200 for t in tasks)


def test_duplicate_items_in_one_list_make_one_task(board, proj):
    _sync(_opencode(proj, _todos(("Same", "pending"), ("same.", "completed"))))
    tasks = board.tasks()
    assert len(tasks) == 1 and tasks[0]["status"] == "todo"


# --------------------------------------------------------------------------- #
# nothing happens when it must not
# --------------------------------------------------------------------------- #

def test_no_todo_call_means_nothing_at_all(board, proj):
    _sync([_env_sys(proj), _user("hello"), _call("bash", {"command": "ls"})])
    assert board.goals(include_closed=True) == [] and board.tasks() == []


def test_a_folder_named_only_in_user_text_is_not_trusted(board, proj):
    msgs = [{"role": "system", "content": "You are a helpful agent."},
            _user("Working directory: %s\nplease plan" % proj),
            _call("todowrite", _todos(("Untrusted", "pending")))]
    _sync(msgs)
    assert board.tasks() == [] and board.goals(include_closed=True) == []


def test_the_hubs_own_repo_is_never_synced(board):
    hub = os.path.dirname(os.path.abspath(A.__file__))
    _sync(_opencode(hub, _todos(("Hub work", "pending"))))
    assert board.tasks() == [] and board.goals(include_closed=True) == []


def test_a_too_broad_folder_is_never_synced(board):
    home = os.path.expanduser("~")
    assert os.path.isdir(home)
    _sync(_opencode(home, _todos(("Home work", "pending"))))
    assert board.tasks() == [] and board.goals(include_closed=True) == []


def test_flag_off_means_nothing(board, proj, monkeypatch):
    real = config.get_flag

    def fake(name, default=None):
        if name == "cli_todo_sync":
            return False
        return real(name, default)

    monkeypatch.setattr(config, "get_flag", fake)
    _sync(_opencode(proj, _todos(("Off", "pending"))))
    assert board.tasks() == [] and board.goals(include_closed=True) == []
    assert not os.path.exists(board.path)
    assert len(A._CTD_MEMO) == 0


def test_a_multi_helper_session_is_skipped(board, proj, monkeypatch):
    monkeypatch.setattr(A, "_build_sid", lambda: "worker-1")
    monkeypatch.setattr(A.swarm_windows, "worker_info",
                        lambda sid: {"run_id": "swarm-1", "index": 0} if sid == "worker-1" else None)
    _sync(_opencode(proj, _todos(("Phase item", "pending"))))
    assert board.tasks() == []


def test_never_raises_and_logs_counts_only(board, proj, monkeypatch, caplog):
    secret = "Deploy to the secret staging host 10.9.8.7"
    with caplog.at_level(logging.DEBUG):
        _sync(_opencode(proj, _todos((secret, "pending")), instruction="private plan"))
    assert len(board.tasks()) == 1
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "1 added" in logged
    assert secret not in logged and "private plan" not in logged
    # a broken board never reaches the caller
    monkeypatch.setattr(taskboard.default, "task_by_ext",
                        lambda key: (_ for _ in ()).throw(RuntimeError("boom")))
    A._CTD_MEMO.clear()
    _sync(_opencode(proj, _todos(("Another", "pending"))))
    for junk in (None, [], [None, 3, "x"], [{"role": "assistant", "tool_calls": "bad"}]):
        _sync(junk)


# --------------------------------------------------------------------------- #
# taskboard: the optional link field and old files
# --------------------------------------------------------------------------- #

def test_an_old_format_taskboard_file_still_loads(tmp_path, proj):
    path = tmp_path / "old" / "taskboard.json"
    path.parent.mkdir()
    old = {
        "version": 1, "saved_at": 200.0,
        "goals": [{"id": "g-old1", "title": "Old goal", "detail": "", "project_dir": proj,
                   "status": "open", "created_at": 100.0, "closed_at": None}],
        "tasks": [
            {"id": "t-old1", "goal_id": "g-old1", "title": "Old task", "detail": "d",
             "needs": [], "files": ["a.py"], "priority": 1, "status": "doing",
             "owner": "helper-1", "run_id": "swarm-x", "phase": 2, "created_at": 101.0,
             "updated_at": 102.0,
             "history": [{"at": 102.0, "status": "doing", "owner": "helper-1",
                          "run_id": "swarm-x", "note": ""}]},
            {"id": "t-old2", "goal_id": "g-old1", "title": "Very old task",
             "status": "todo", "created_at": 103.0},
        ]}
    path.write_text(json.dumps(old), encoding="utf-8")
    before = taskboard.Board(str(path)).tasks()
    assert [t["id"] for t in before] == ["t-old1", "t-old2"]
    assert all("ext" not in t for t in before)
    taskboard.configure(str(path))
    A._CTD_MEMO.clear()
    _sync(_opencode(proj, _todos(("New CLI item", "pending"))))
    after = taskboard.Board(str(path))
    assert after.goals(project_dir=proj)[0]["id"] == "g-old1"   # the open goal is reused
    by_id = {t["id"]: t for t in after.tasks()}
    assert by_id["t-old1"] == before[0] and by_id["t-old2"] == before[1]
    assert "ext" not in by_id["t-old1"]
    new = [t for t in by_id.values() if t["id"] not in ("t-old1", "t-old2")]
    assert len(new) == 1 and new[0]["ext"]["status"] == "pending"


def test_task_by_ext_and_a_bad_link_is_dropped(board):
    gid = board.add_goal("G")
    tid = board.add_task(gid, "linked", ext={"key": "k1", "status": "pending"})
    board.add_task(gid, "plain")
    board.add_task(gid, "bad link", ext={"status": "x"})
    assert board.task_by_ext("k1")["id"] == tid
    assert board.task_by_ext("nope") is None
    plain = [t for t in board.tasks() if t["title"] in ("plain", "bad link")]
    assert all("ext" not in t for t in plain)
    board.update(tid, ext={"key": "k1", "status": "completed"})
    assert board.task_by_ext("k1")["ext"]["status"] == "completed"
    assert board.task_by_ext("k1")["history"] == []       # a link change is no history entry


def test_deferred_writes_the_file_once(board, monkeypatch):
    writes = []
    real = taskboard.os.replace
    monkeypatch.setattr(taskboard.os, "replace", lambda a, b: (writes.append(b), real(a, b)))
    with board.deferred():
        gid = board.add_goal("G")
        for i in range(10):
            board.add_task(gid, "t%d" % i)
        assert writes == []
    assert len(writes) == 1
    assert len(taskboard.Board(board.path).tasks()) == 10


# --------------------------------------------------------------------------- #
# end to end: the real /v1 handlers, a fake upstream
# --------------------------------------------------------------------------- #

class _Resp:
    status_code = 200
    text = ""

    def __init__(self):
        self.headers = {}

    def json(self):
        return {"id": "chatcmpl-x", "object": "chat.completion", "created": 1,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant",
                                         "content": "Done with this step; the exporter "
                                                    "now writes every invoice row."}}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 12, "total_tokens": 52}}

    def close(self):
        pass


@pytest.fixture
def world(monkeypatch):
    for name in ("_record_chat_usage", "_save_perf_stats", "_act_pick", "_note_ttft",
                 "_note_provider_timeout", "_throttle_failed_hop", "_v1_observe"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)
    monkeypatch.setattr(A, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(A, "_model_block_reason", lambda pid, m: None)
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    monkeypatch.setattr(A, "_is_trivial_turn", lambda *a, **k: False)
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: 20)
    monkeypatch.setattr(A, "_stream_peek_timeout", lambda *a, **k: 1.0)
    monkeypatch.setattr(A, "_record_outcome", lambda *a, **k: None)
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("p1", "m1", "hard"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("p1", "m1")])
    monkeypatch.setattr(A, "_dispatch_chat", lambda pid, payload, stream: _Resp())
    yield


SCHEMA = {"type": "object", "properties": {"todos": {"type": "array"}}}


def test_end_to_end_chat_completions_opencode(board, proj, world):
    body = {"model": "auto", "stream": False,
            "messages": [_env_sys(proj), _user("build the exporter"),
                         _call("todowrite", _todos(("Write rows", "in_progress"),
                                                   ("Add header", "completed"))),
                         _result("c1", "2 todos")],
            "tools": [{"type": "function", "function": {
                "name": "todowrite", "description": "todos", "parameters": SCHEMA}}]}
    r = A.app.test_client().post("/v1/chat/completions", json=body,
                                 headers={"X-Session-Id": "ses_e2e_chat"})
    assert r.status_code == 200, r.get_data()[:300]
    assert "every invoice row" in r.get_data(as_text=True)     # the reply is untouched
    assert {k: v["status"] for k, v in _by_title(board).items()} == {
        "Write rows": "doing", "Add header": "done"}
    assert board.goals(project_dir=proj)[0]["title"] == "[CLI] build the exporter"


def test_end_to_end_responses_codex(board, proj, world):
    env = ("<environment_context>\n  <cwd>%s</cwd>\n  <shell>bash</shell>\n"
           "</environment_context>" % proj)
    body = {"model": "auto", "stream": False, "prompt_cache_key": "thread-e2e",
            "instructions": "You are Codex.",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": env}]},
                {"role": "user", "content": [{"type": "input_text", "text": "add retries"}]},
                {"type": "function_call", "call_id": "p1", "name": "update_plan",
                 "arguments": json.dumps({"plan": [
                     {"step": "Find the client", "status": "completed"},
                     {"step": "Wrap calls", "status": "pending"}]})},
                {"type": "function_call_output", "call_id": "p1", "output": "Plan updated"}],
            "tools": [{"type": "function", "name": "update_plan", "description": "plan",
                       "parameters": {"type": "object", "properties": {}}}]}
    r = A.app.test_client().post("/v1/responses", json=body)
    assert r.status_code == 200, r.get_data()[:300]
    assert {k: v["status"] for k, v in _by_title(board).items()} == {
        "Find the client": "done", "Wrap calls": "todo"}


def test_end_to_end_messages_claude_code(board, proj, world):
    body = {"model": "auto", "max_tokens": 400, "stream": False,
            "system": "You are Claude Code.\n<env>\nWorking directory: %s\n</env>" % proj,
            "messages": [
                {"role": "user", "content": "fix the flaky test"},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "tu1", "name": "TodoWrite",
                     "input": {"todos": [
                         {"content": "Reproduce the flake", "status": "completed",
                          "activeForm": "Reproducing"},
                         {"content": "Pin the clock", "status": "in_progress",
                          "activeForm": "Pinning"}]}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "tu1", "content": "ok"}]}],
            "tools": [{"name": "TodoWrite", "description": "todos", "input_schema": SCHEMA}]}
    r = A.app.test_client().post("/v1/messages", json=body,
                                 headers={"X-Claude-Code-Session-Id": "cc-e2e"})
    assert r.status_code == 200, r.get_data()[:300]
    assert {k: v["status"] for k, v in _by_title(board).items()} == {
        "Reproduce the flake": "done", "Pin the clock": "doing"}


def test_end_to_end_flag_off_leaves_the_board_alone(board, proj, world, monkeypatch):
    monkeypatch.setattr(A, "_ctd_on", lambda: False)
    body = {"model": "auto", "stream": False,
            "messages": [_env_sys(proj), _user("x"),
                         _call("todowrite", _todos(("Nope", "pending")))]}
    r = A.app.test_client().post("/v1/chat/completions", json=body,
                                 headers={"X-Session-Id": "ses_off"})
    assert r.status_code == 200
    assert board.tasks() == [] and board.goals(include_closed=True) == []
