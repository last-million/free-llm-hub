# -*- coding: utf-8 -*-
"""A conversation keeps its skills, in every terminal CLI.

Owner, 2026-11-07: "are skills also used inside terminal CLIs? I don't think so." MEASURED
on his terminal OpenCode session (one conversation since 10-27, 265 user messages, 13
compactions): its opening request matched App & API testing + Security, and every request of
the last hour got NO skill. The hub chose skills from the text of the CURRENT request only;
a long terminal session sends short follow-ups ("check the next one"), and the CLI's
compaction had taken the opening request out of what it sends. A restart also forgot the
picker's verdicts (memory only).

Now (app._sk_inherit, flag `skill_conversation_memory`): the skills a conversation's task
named are remembered per conversation, on disk; a request whose own text names no task keeps
them; a request that names a new task replaces them.

Hermetic: no network, no model. The request texts are invented.
"""
import json
import os

import pytest

import app as A
import craft

TOOLS = [{"type": "function", "function": {"name": n}} for n in ("read", "write", "bash")]
SYS = {"role": "system", "content": "You are a coding agent."}
TASK = "find IDOR on the hackerone program api.example.com"
FOLLOW = "check the next one"
CALL = {"role": "assistant", "content": None, "tool_calls": [
    {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{\"command\":\"ls\"}"}}]}
RESULT = {"role": "tool", "tool_call_id": "c1", "content": "done"}
TESTING = craft.TARGET_TESTING[:40]


@pytest.fixture
def mem(monkeypatch, tmp_path):
    """Memory on, a fresh table and file, one conversation key that tests can change."""
    path = str(tmp_path / "conversation-skills.json")
    monkeypatch.setattr(A, "_sk_conv_path", lambda: path)
    monkeypatch.setattr(A, "_SK_CONV", None)
    key = {"conv": "hdr:ses_one"}
    monkeypatch.setattr(A, "_cmem_conv", lambda: key["conv"])
    monkeypatch.setattr(A, "_brief_every_turn_on", lambda: True)
    key["path"] = path
    return key


def _user(text):
    return {"role": "user", "content": text}


def _added(before, after):
    return "\n\n".join(str(m.get("content")) for m in after
                       if m.get("role") == "system" and m not in before)


def _brief(messages):
    return _added(messages, A._apply_craft_brief(messages, agentic=True, tools=TOOLS))


# --------------------------------------------------------------------------- #
# What settles a task
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("ids,expected", [
    (["app_testing"], True), (["programming"], True), (["web_design", "seo"], True),
    (["ship"], False), (["ecommerce"], False), (["security"], False), (["seo", "images"], False),
    (["custom:x"], False), (["ecc:tdd-workflow"], False), ([], False), (None, False)])
def test_what_names_a_task_of_its_own(ids, expected):
    assert craft.settles(ids) is expected


def test_extra_ids_ship_like_picked_ones_and_need_no_text():
    assert "app_testing" in craft.names("", extra=("app_testing",))
    assert craft.names("", extra=()) == []
    assert craft.loop_message("", extra=("app_testing",))["content"].count(TESTING) == 1


def test_a_switched_off_skill_never_ships_even_when_remembered(monkeypatch):
    monkeypatch.setattr(craft, "_skill_state", lambda: (frozenset({"app_testing"}), [], frozenset()))
    assert craft.names(FOLLOW, extra=("app_testing",)) == []


# --------------------------------------------------------------------------- #
# The memory
# --------------------------------------------------------------------------- #

def test_a_follow_up_keeps_the_task_the_conversation_opened_with(mem):
    assert TESTING in _brief([SYS, _user(TASK)])
    # The CLI compacted: the opening request is gone from what it sends.
    assert TESTING in _brief([SYS, _user(FOLLOW)])


def test_a_hop_inside_the_loop_keeps_it_too(mem):
    _brief([SYS, _user(TASK)])
    assert TESTING in _brief([SYS, _user(FOLLOW), CALL, RESULT])


def test_it_survives_a_restart(mem, monkeypatch):
    _brief([SYS, _user(TASK)])
    monkeypatch.setattr(A, "_SK_CONV", None)        # the process started again
    assert TESTING in _brief([SYS, _user(FOLLOW)])


def test_a_new_task_replaces_the_old_one(mem):
    _brief([SYS, _user(TASK)])
    _brief([SYS, _user("build a website for my gym")])
    body = _brief([SYS, _user(FOLLOW)])
    assert craft.WEB_DESIGN[:40] in body and TESTING not in body


def test_a_ride_along_skill_does_not_replace_the_task(mem):
    _brief([SYS, _user(TASK)])
    body = _brief([SYS, _user("now improve the seo of the report")])
    assert TESTING in body


def test_another_conversation_does_not_inherit_it(mem):
    _brief([SYS, _user(TASK)])
    mem["conv"] = "hdr:ses_two"
    assert TESTING not in _brief([SYS, _user(FOLLOW)])


def test_no_conversation_key_means_nothing_is_kept(mem):
    mem["conv"] = None
    _brief([SYS, _user(TASK)])
    assert TESTING not in _brief([SYS, _user(FOLLOW)])
    assert not os.path.exists(mem["path"])


def test_it_expires(mem, monkeypatch):
    _brief([SYS, _user(TASK)])
    real = A.time.time
    monkeypatch.setattr(A.time, "time", lambda: real() + A._SK_CONV_TTL + 10)
    assert TESTING not in _brief([SYS, _user(FOLLOW)])


def test_the_flag_off_keeps_and_writes_nothing(mem, monkeypatch):
    monkeypatch.setattr(A, "_sk_conv_on", lambda: False)
    _brief([SYS, _user(TASK)])
    assert TESTING not in _brief([SYS, _user(FOLLOW)])
    assert not os.path.exists(mem["path"])


def test_only_skill_ids_reach_the_disk(mem):
    _brief([SYS, _user(TASK)])
    raw = open(mem["path"], encoding="utf-8").read()
    assert "hackerone" not in raw and "ses_one" not in raw
    rows = json.loads(raw)["rows"]
    assert [r["ids"] for r in rows.values()] == [["app_testing"]]


def test_a_broken_file_is_an_empty_memory(mem):
    with open(mem["path"], "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert TESTING not in _brief([SYS, _user(FOLLOW)])


def test_the_table_stays_bounded(mem, monkeypatch):
    monkeypatch.setattr(A, "_SK_CONV_MAX", 3)
    for n in range(6):
        mem["conv"] = "hdr:s%d" % n
        _brief([SYS, _user(TASK)])
    assert len(json.load(open(mem["path"], encoding="utf-8"))["rows"]) == 3


def test_an_unchanged_task_is_not_rewritten_every_hop(mem, monkeypatch):
    _brief([SYS, _user(TASK)])
    writes = []
    monkeypatch.setattr(A, "_sk_conv_save", lambda rows: writes.append(1))
    for _ in range(5):
        _brief([SYS, _user(TASK), CALL, RESULT])
    assert writes == []


# --------------------------------------------------------------------------- #
# A session that began before this existed
# --------------------------------------------------------------------------- #

def test_an_earlier_instruction_in_the_request_names_the_task(mem):
    # Further back than the recent follow-ups read together (_SK_CONV_CTX).
    msgs = [SYS, _user("ok"), {"role": "assistant", "content": "Ready."}, _user(TASK)]
    for n in range(A._SK_CONV_CTX + 2):
        msgs += [{"role": "assistant", "content": "Done %d." % n}, _user("next %d please" % n)]
    assert TESTING in _brief(msgs)
    assert TESTING in _brief([SYS, _user(FOLLOW)])          # and it is kept from now on


def test_recent_follow_ups_are_read_together_by_the_picker(mem, monkeypatch):
    """MEASURED: 33 follow-ups since the last compaction, none names the task alone."""
    verdicts, asked = {}, []
    lines = ["look at the login endpoint", "try the second account", FOLLOW]

    def picker(text, wait=0.0):
        if wait < 0:
            asked.append(text)
            if text == "\n".join(lines):                  # read together, it is clear
                verdicts[text] = ("app_testing",)
            return ()
        return verdicts.get(text, ())
    monkeypatch.setattr(craft, "_SKILL_PICKER", picker)
    msgs = [SYS]
    for line in lines:
        msgs += [_user(line), {"role": "assistant", "content": "Done."}]
    msgs = msgs[:-1]
    assert TESTING not in _brief(msgs)                     # the verdict is not in yet
    assert "\n".join(lines) in asked                        # asked once, together
    assert TESTING in _brief(msgs + [CALL, RESULT])        # the next hop has it
    assert TESTING in _brief([SYS, _user("go on")])        # and it is kept


def test_activity_shows_the_kept_skills(mem):
    _brief([SYS, _user(TASK)])
    with A.app.test_request_context("/v1/chat/completions"):
        A.g.act = {}
        _brief([SYS, _user(FOLLOW)])
        assert "App & API testing" in A.g.act.get("skills", [])


# --------------------------------------------------------------------------- #
# The real handlers: OpenCode (chat), Codex (responses), Claude Code (messages)
# --------------------------------------------------------------------------- #

class _Resp:
    status_code = 200
    text = ""
    headers = {}

    def json(self):
        return {"id": "x", "object": "chat.completion", "created": 1,
                "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": "All done, the check ran and passed."}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 4, "total_tokens": 8}}

    def close(self):
        pass


@pytest.fixture
def world(monkeypatch, tmp_path):
    path = str(tmp_path / "conversation-skills.json")
    monkeypatch.setattr(A, "_sk_conv_path", lambda: path)
    monkeypatch.setattr(A, "_SK_CONV", None)
    monkeypatch.setattr(A, "_brief_every_turn_on", lambda: True)
    for name in ("_record_chat_usage", "_save_perf_stats", "_act_pick", "_note_ttft",
                 "_note_provider_timeout", "_throttle_failed_hop", "_record_outcome"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)
    monkeypatch.setattr(A, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(A, "_model_block_reason", lambda pid, m: None)
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    monkeypatch.setattr(A, "_is_trivial_turn", lambda *a, **k: False)
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: 20)
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("p1", "m1", "hard"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("p1", "m1")])
    sent = []

    def dispatch(pid, payload, stream=False, *a, **k):
        sent.append(A._apply_craft_brief(payload["messages"], agentic=bool(payload.get("tools")),
                                         tools=payload.get("tools")))
        return _Resp()
    monkeypatch.setattr(A, "_dispatch_chat", dispatch)
    return sent


BASH = {"type": "function", "function": {"name": "bash", "description": "run",
                                         "parameters": {"type": "object", "properties": {}}}}


def _chat(text, sid):
    return ("/v1/chat/completions", {"X-Session-Id": sid},
            {"model": "coding-max", "messages": [SYS, _user(text)], "tools": [BASH]})


def _responses(text, sid):
    return ("/v1/responses", {},
            {"model": "auto", "stream": False, "prompt_cache_key": sid,
             "input": [{"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": text}]}],
             "tools": [{"type": "function", "name": "bash", "parameters": {}}]})


def _messages(text, sid):
    return ("/v1/messages", {"X-Claude-Code-Session-Id": sid},
            {"model": "auto", "max_tokens": 100, "messages": [{"role": "user", "content": text}],
             "tools": [{"name": "bash", "input_schema": {"type": "object"}}]})


CLIS = [_chat, _responses, _messages]


def _post(shape, text, sid):
    path, headers, body = shape(text, sid)
    return A.app.test_client().post(path, json=body, headers=headers)


@pytest.mark.parametrize("shape", CLIS, ids=["opencode", "codex", "claude-code"])
def test_e2e_a_terminal_follow_up_keeps_the_conversations_skills(world, shape):
    assert _post(shape, TASK, "sess-1").status_code == 200
    assert any(TESTING in str(m.get("content")) for m in world[-1])
    assert _post(shape, FOLLOW, "sess-1").status_code == 200
    assert any(TESTING in str(m.get("content")) for m in world[-1])


@pytest.mark.parametrize("shape", CLIS, ids=["opencode", "codex", "claude-code"])
def test_e2e_another_session_starts_clean(world, shape):
    _post(shape, TASK, "sess-1")
    _post(shape, FOLLOW, "sess-2")
    assert not any(TESTING in str(m.get("content")) for m in world[-1])


@pytest.mark.parametrize("shape", CLIS, ids=["opencode", "codex", "claude-code"])
def test_e2e_what_it_was_before_the_fix(world, shape, monkeypatch):
    """Negative control: the measured bug, reproduced with the memory off."""
    monkeypatch.setattr(A, "_sk_conv_on", lambda: False)
    _post(shape, TASK, "sess-1")
    _post(shape, FOLLOW, "sess-1")
    assert not any(TESTING in str(m.get("content")) for m in world[-1])
