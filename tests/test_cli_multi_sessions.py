r"""Multi from the terminal CLIs (2026-10-10, flag `cli_multi_sessions`).

Owner complaint: "inside the CLI the multi mode works as crew, but from the
frontend (Build page) Multi works well in parallel." The fix: a terminal CLI
that selects the Multi tier (and carries tools, in a known project folder, on a
fresh work instruction) now gets the SAME real swarm_windows run the Build page
does -- planner, up to N helper CLI sessions, review -- under a hub /agent
conversation that OWNS it (auto_resume ticked). The CLI turn is answered with
TEXT ONLY, streamed in its own protocol, with a watch link and a safe
background hand-off before the client's timeout. A disconnect never stops the
run; only "stop multi" (or the Build page / Running popup) does.

Hermetic: `_multi_turn_events` / `_multi_follow_events` / `live_run` /
swarm_windows and the owner-session machinery are faked; the clock is faked
where elapsed time matters. No real CLI, no network, no real sleeps.
"""
import json
import os
import time

import pytest

import app as A
import config


# --------------------------------------------------------------------------- #
# fixture: a fully faked terminal-CLI Multi world
# --------------------------------------------------------------------------- #

def _scripted_events(report="DONE: built the thing (2/2 phases)"):
    """What a faked _multi_turn_events / _multi_follow_events yields."""
    yield {"event": "notice", "text": "Multi sessions: 2 phases across real codex sessions, in this folder (run run-x)."}
    yield {"event": "tool", "text": "Plan · start now: 1 · Build | 2 · Test"}
    yield {"event": "tool", "text": "Phase 1 of 2 · Build"}
    yield {"event": "output", "text": "Phase 1 of 2 · Build -- done: built"}
    yield {"event": "message", "text": report}
    yield {"event": "done", "text": report}


@pytest.fixture
def cm(tmp_path, monkeypatch):
    h = {"start_session": [], "auto_resume": [], "titles": [], "stop": [],
         "sessions": {}, "turn_events": [], "follow": [], "report": "DONE: built"}

    monkeypatch.setattr(config, "state_dir", lambda: str(tmp_path))
    monkeypatch.setattr(A, "_cm_on", lambda: True)
    # The tests below that start a run directly do it with `cli_multi_confirm`
    # OFF; the consent tests switch it back on.
    monkeypatch.setattr(A, "_cm_confirm_on", lambda: False)
    h["tmp"] = tmp_path
    h["proj"] = str(tmp_path / "proj")

    # Terminal CLI: no /build prefix, caller opencode, isolated copy exists.
    monkeypatch.setattr(A, "_build_sid", lambda: None)
    monkeypatch.setattr(A, "_steer_cli_from_ua", lambda: "opencode")
    monkeypatch.setattr(A.agentic_chat, "_isolated_bin", lambda c: "/iso/%s" % c)

    # A fixed conversation key, so the mapping is deterministic across turns.
    monkeypatch.setattr(A.ctxwin, "conversation_key", lambda **kw: "conv-1")
    monkeypatch.setattr(A.ctxwin, "is_compaction_request", lambda msgs: False)

    monkeypatch.setattr(A, "_multi_wants_a_swarm", lambda t: True)
    (tmp_path / "proj").mkdir()
    monkeypatch.setattr(A, "_v1_project_cwd", lambda msgs: str(tmp_path / "proj"))
    monkeypatch.setattr(A, "_active_mode", lambda: "all")

    def _start_session(cli, project, quality=None, mode=None):
        sid = "owner-%d" % (len(h["start_session"]) + 1)
        h["start_session"].append({"cli": cli, "project": project,
                                    "quality": quality, "mode": mode, "sid": sid})
        h["sessions"][sid] = {"session_id": sid, "cli": cli, "project_dir": project,
                              "quality": quality, "mode": mode}
        return sid
    monkeypatch.setattr(A.agentic_chat, "start_session", _start_session)
    monkeypatch.setattr(A.agentic_chat, "get_session", lambda sid: h["sessions"].get(sid))
    monkeypatch.setattr(A.agentic_history, "get_conversation", lambda sid: h["sessions"].get(sid))
    monkeypatch.setattr(A.agentic_history, "set_title",
                        lambda sid, t: h["titles"].append((sid, t)))
    monkeypatch.setattr(A.agentic_history, "set_quality", lambda sid, q: None)
    monkeypatch.setattr(A.agentic_history, "set_mode", lambda sid, m: None)
    monkeypatch.setattr(A.agentic_history, "set_auto_resume",
                        lambda sid, en: h["auto_resume"].append((sid, en)))

    def _turn_events(sid, info, text):
        h["turn_events"].append({"sid": sid, "text": text})
        return _scripted_events(h["report"])
    monkeypatch.setattr(A, "_multi_turn_events", _turn_events)

    def _follow(run_id, cli):
        h["follow"].append({"run": run_id, "cli": cli})
        return _scripted_events(h["report"])
    monkeypatch.setattr(A, "_multi_follow_events", _follow)
    monkeypatch.setattr(A.agentic_chat, "live_run",
                        lambda sid, producer, context=None: producer)

    monkeypatch.setattr(A.swarm_windows, "stop", lambda rid: h["stop"].append(rid) or True)
    monkeypatch.setattr(A.swarm_windows, "format_result", lambda rid: "FALLBACK REPORT")
    monkeypatch.setattr(A.swarm_windows, "_concurrency", lambda: 6)
    # _multi_run_for reads swarm_windows.status(): a simulated run is RUNNING in
    # h["run_dir"] (the project folder unless a test moves it).
    h["run_dir"] = str(tmp_path / "proj")
    monkeypatch.setattr(A.swarm_windows, "status",
                        lambda rid, with_events=False: {"run_id": rid,
                                                        "state": A.swarm_windows.RUNNING,
                                                        "project_dir": h["run_dir"]})

    A._MULTI_RUNS.clear()
    A._CM_PENDING.clear()
    yield h
    A._MULTI_RUNS.clear()
    A._CM_PENDING.clear()


USER = [{"role": "user", "content": "build me a dashboard with a chart and a table"}]
TOOLS = [{"type": "function", "function": {"name": "write", "parameters": {}}}]


def _call(protocol="openai", stream=False, model="multi", messages=None,
          tools=None, headers=None):
    """Call the intercept inside a terminal-CLI request context AND read the
    whole body there -- a streamed response must be consumed inside the context
    that created it (stream_with_context). Returns (resp_or_None, raw)."""
    body = {"model": model, "stream": stream}
    msgs = USER if messages is None else messages
    tls = TOOLS if tools is None else tools
    hdrs = {"User-Agent": "opencode/1.0"}
    if headers:
        hdrs.update(headers)
    with A.app.test_request_context("/v1/chat/completions", method="POST", headers=hdrs):
        ret = A._cm_multi_cli_intercept(body, protocol, msgs, tls, 1234)
        if ret is None:
            return None, ""
        resp = ret[0] if isinstance(ret, tuple) else ret
        return resp, resp.get_data(as_text=True)


# --------------------------------------------------------------------------- #
# GATE -- each condition, both ways
# --------------------------------------------------------------------------- #

def test_all_conditions_hold_starts_a_run(cm):
    resp, raw = _call()
    assert resp is not None
    assert cm["turn_events"], "the run was started via _multi_turn_events"
    assert "Watch it live: http://127.0.0.1:" in raw
    assert "/agent/owner-1" in raw


def test_flag_off_is_byte_identical_fall_through(cm, monkeypatch):
    monkeypatch.setattr(A, "_cm_on", lambda: False)
    resp, _ = _call()
    assert resp is None
    assert not cm["turn_events"] and not cm["start_session"]


def test_not_the_multi_tier_falls_through(cm):
    assert _call(model="best")[0] is None
    assert _call(model="swarm")[0] is None
    assert _call(model="auto")[0] is None
    assert not cm["turn_events"]


def test_a_build_request_is_never_turned_into_a_run(cm, monkeypatch):
    monkeypatch.setattr(A, "_build_sid", lambda: "build-sid")
    assert _call()[0] is None
    assert not cm["turn_events"] and not cm["start_session"]


def test_a_worker_session_is_never_turned_into_a_run(cm, monkeypatch):
    # A Multi worker reaches the hub at /build/<worker sid> too, so _build_sid()
    # is set; worker_info would name it.
    monkeypatch.setattr(A, "_build_sid", lambda: "worker-sid")
    monkeypatch.setattr(A.swarm_windows, "worker_info",
                        lambda sid: {"run_id": "r", "owner": "o"})
    assert _call()[0] is None
    assert not cm["turn_events"]


def test_the_dashboard_quick_chat_falls_through(cm):
    assert _call(headers={"X-Free-LLM-Hub": "dashboard"})[0] is None
    assert not cm["turn_events"]


def test_a_tool_free_turn_falls_through(cm):
    assert _call(tools=[])[0] is None
    assert not cm["turn_events"]


def test_a_compaction_request_falls_through(cm, monkeypatch):
    monkeypatch.setattr(A.ctxwin, "is_compaction_request", lambda msgs: True)
    assert _call()[0] is None
    assert not cm["turn_events"]


def test_a_tool_continuation_is_not_a_fresh_instruction(cm):
    cont = USER + [{"role": "assistant", "content": "",
                    "tool_calls": [{"id": "c1", "type": "function",
                                    "function": {"name": "write", "arguments": "{}"}}]},
                   {"role": "tool", "tool_call_id": "c1", "content": "ok"}]
    assert _call(messages=cont)[0] is None
    assert not cm["turn_events"]


def test_an_unknown_folder_falls_through(cm, monkeypatch):
    monkeypatch.setattr(A, "_v1_project_cwd", lambda msgs: None)
    assert _call()[0] is None
    assert not cm["turn_events"]


def test_a_broad_or_hub_folder_is_refused(cm, monkeypatch):
    monkeypatch.setattr(A, "_v1_project_cwd", lambda msgs: os.path.expanduser("~"))
    assert _call()[0] is None
    monkeypatch.setattr(A, "_v1_project_cwd", lambda msgs: os.path.abspath(os.sep))
    assert _call()[0] is None
    assert not cm["turn_events"]


def test_a_chat_turn_that_is_not_work_falls_through(cm, monkeypatch):
    monkeypatch.setattr(A, "_multi_wants_a_swarm", lambda t: False)
    assert _call()[0] is None
    assert not cm["turn_events"]


# --------------------------------------------------------------------------- #
# The owner conversation
# --------------------------------------------------------------------------- #

def test_owner_conversation_is_created_with_auto_resume(cm):
    _call()
    assert cm["start_session"], "a hub /agent owner conversation was opened"
    row = cm["start_session"][0]
    assert row["quality"] == "multi"
    assert ("owner-1", True) in cm["auto_resume"], "auto_resume ticked"
    assert cm["titles"] and cm["titles"][0][1].startswith("Multi (CLI)")


def test_helper_cli_is_the_caller_when_isolated_else_opencode(cm, monkeypatch):
    assert A._cm_helper_cli("opencode") == "opencode"
    monkeypatch.setattr(A.agentic_chat, "_isolated_bin", lambda c: None)
    assert A._cm_helper_cli("codex") == "opencode"


# --------------------------------------------------------------------------- #
# The mapping: persisted and found by the next turn
# --------------------------------------------------------------------------- #

def _mapkey(cm, folder=None, conv="conv-1"):
    return A._cm_map_key(conv, A._cm_folder_key(folder or cm["proj"]))


def test_mapping_is_persisted_and_found_by_the_next_turn(cm):
    _call()                                        # turn 1 opens owner-1
    row = A._cm_map_get(_mapkey(cm))
    assert row and row["owner"] == "owner-1"
    assert row["folder"] == A._cm_folder_key(cm["proj"]), "the folder is stored in the row"
    assert A._cm_map_get("conv-1") is None, "never keyed by the conversation key alone"
    assert os.path.exists(A._cm_map_path())
    # Turn 2 (run not live): the SAME owner is reused, no second session opened.
    A._MULTI_RUNS.clear()
    _call()
    assert len(cm["start_session"]) == 1, "owner conversation reused across turns"


def test_map_store_round_trip_lru_and_ttl(cm):
    A._cm_map_put("k1", "o1", "r1")
    got = A._cm_map_get("k1")
    assert got["owner"] == "o1" and got["run"] == "r1" and "at" in got
    # TTL: an entry older than 7 days is pruned.
    stale = {"old": {"owner": "o", "run": "r", "at": time.time() - A._CM_MAP_TTL - 10}}
    assert A._cm_map_prune(stale) == {}
    # LRU: only the newest _CM_MAP_LRU survive (all within TTL via +i).
    base = time.time()
    many = {"k%d" % i: {"owner": "o", "run": "r", "at": base + i}
            for i in range(A._CM_MAP_LRU + 25)}
    pruned = A._cm_map_prune(many)
    assert len(pruned) == A._CM_MAP_LRU
    assert "k0" not in pruned and ("k%d" % (A._CM_MAP_LRU + 24)) in pruned


# --------------------------------------------------------------------------- #
# Later turns while the run is live: re-attach / stop, no model call, no 2nd run
# --------------------------------------------------------------------------- #

def test_live_run_reattaches_without_a_second_run(cm):
    _call()                                        # start -> owner-1
    A._MULTI_RUNS["owner-1"] = "run-1"             # simulate the run still live
    before = len(cm["turn_events"])
    resp, raw = _call()
    assert resp is not None
    assert len(cm["turn_events"]) == before, "no second run started"
    assert cm["follow"] and cm["follow"][-1]["run"] == "run-1", "re-attached"
    assert "still working" in raw


def test_stop_multi_stops_the_run_and_starts_nothing(cm):
    _call()
    A._MULTI_RUNS["owner-1"] = "run-1"
    before = len(cm["turn_events"])
    resp, raw = _call(messages=[{"role": "user", "content": "stop multi"}])
    assert resp is not None
    assert cm["stop"] == ["run-1"], "the run was stopped"
    assert len(cm["turn_events"]) == before and not cm["follow"]
    assert "Stopping the Multi run" in raw


@pytest.mark.parametrize("msg", ["stop", "please don't stop multi now",
                                 "stop multi please", "stop multi, then go"])
def test_only_the_whole_message_stop_multi_stops(cm, msg):
    """A bare "stop", or a sentence that merely CONTAINS "stop multi", never
    stops the run -- it re-attaches instead."""
    _call()
    A._MULTI_RUNS["owner-1"] = "run-1"
    resp, raw = _call(messages=[{"role": "user", "content": msg}])
    assert resp is not None
    assert cm["stop"] == [], "%r must not stop the run" % msg
    assert cm["follow"], "it re-attached instead"


@pytest.mark.parametrize("msg", ["Stop Multi", "  stop   multi  ", "STOP MULTI!",
                                 "stop multi."])
def test_stop_multi_is_matched_after_normalisation(cm, msg):
    _call()
    A._MULTI_RUNS["owner-1"] = "run-1"
    _call(messages=[{"role": "user", "content": msg}])
    assert cm["stop"] == ["run-1"]


def test_stop_multi_with_a_claude_code_system_reminder_still_matches(cm):
    _call()
    A._MULTI_RUNS["owner-1"] = "run-1"
    _call(messages=[{"role": "user", "content": [
        {"type": "text", "text": "<system-reminder>be careful</system-reminder>"},
        {"type": "text", "text": "stop multi"}]}])
    assert cm["stop"] == ["run-1"]


# --------------------------------------------------------------------------- #
# Protocols: stream + non-stream, text only, with the report
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("protocol", ["openai", "responses", "anthropic"])
def test_non_stream_each_protocol_is_text_only_with_the_report(cm, protocol):
    resp, raw = _call(protocol=protocol, stream=False)
    assert resp is not None
    assert "DONE: built" in raw
    assert "Watch it" in raw
    if protocol == "openai":
        data = json.loads(raw)
        msg = data["choices"][0]["message"]
        assert msg["role"] == "assistant" and msg.get("content")
        assert not msg.get("tool_calls")
    if protocol == "responses":
        assert "response" in raw and "output_text" in raw
    if protocol == "anthropic":
        assert "content" in raw and "text" in raw


@pytest.mark.parametrize("protocol", ["openai", "responses", "anthropic"])
def test_stream_each_protocol_is_text_only_with_the_report(cm, protocol):
    resp, raw = _call(protocol=protocol, stream=True)
    assert resp is not None
    assert "DONE: built" in raw
    assert "Watch it" in raw
    assert '"tool_calls"' not in raw, "a Multi CLI turn is answered with TEXT only"
    if protocol == "openai":
        assert "chat.completion.chunk" in raw and "[DONE]" in raw
    if protocol == "responses":
        assert "response.output_text.delta" in raw
    if protocol == "anthropic":
        assert "content_block_delta" in raw


def test_usage_reports_the_requests_own_estimate(cm):
    resp, raw = _call(protocol="openai", stream=False)
    data = json.loads(raw)
    assert data["usage"]["prompt_tokens"] == A._reported_prompt_tokens(None, 1234)


# --------------------------------------------------------------------------- #
# Safe end before the CLI's cap; a disconnect never stops the run
# --------------------------------------------------------------------------- #

def test_safe_end_hands_off_to_the_background_before_the_cap(cm, monkeypatch):
    # A clock that jumps past the safe window after the stream starts, so the
    # run has not finished when the safe end fires.
    ticks = iter([0.0] + [10_000.0] * 60)
    monkeypatch.setattr(A.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(A, "_multi_turn_events",
                        lambda s, i, t: iter([{"event": "tool", "text": "Phase 1 of 9"}] * 20))
    resp, raw = _call(protocol="openai", stream=True)
    assert "keeps working in the background" in raw
    assert "stop multi" in raw
    assert not cm["stop"], "the safe hand-off never stops the run"


def test_a_disconnect_does_not_stop_the_run(cm):
    body = {"model": "multi", "stream": True}
    with A.app.test_request_context("/v1/chat/completions", method="POST",
                                    headers={"User-Agent": "opencode/1.0"}):
        ret = A._cm_multi_cli_intercept(body, "openai", USER, TOOLS, 1234)
        assert ret is not None
        gen = iter(ret.response)
        first = next(gen)               # consume one chunk, then abandon (client left)
        gen.close()
    assert "data:" in first
    assert not cm["stop"], "a disconnect never stops the run"


def test_safe_end_seconds_per_cli():
    assert A._cm_safe_end_seconds("opencode") == 540
    assert A._cm_safe_end_seconds("codex") == A._CM_SAFE_END_DEFAULT
    assert A._cm_safe_end_seconds(None) == A._CM_SAFE_END_DEFAULT


# --------------------------------------------------------------------------- #
# The keepalive line
# --------------------------------------------------------------------------- #

def test_a_keepalive_line_is_emitted_when_the_run_is_quiet(cm, monkeypatch):
    ticks = iter([0.0, 25.0, 25.0, 25.0, 25.0, 25.0])
    monkeypatch.setattr(A.time, "monotonic", lambda: next(ticks))

    def _wrap(producer, interval):
        yield None                       # a silent-gap keepalive tick
        for ev in producer:
            yield ev
    monkeypatch.setattr(A, "_cm_keepalive_wrap", _wrap)
    pieces = list(A._cm_text_stream(
        "owner-1", "run-1", iter([{"event": "message", "text": "R"}]),
        "http://127.0.0.1:8787/agent/owner-1", 240, 6, "HEADER\n"))
    joined = "".join(pieces)
    assert joined.startswith("HEADER\n")
    assert "still working" in joined
    assert "R" in joined


def _content(raw):
    """The assistant text of a non-stream chat.completions answer."""
    return json.loads(raw)["choices"][0]["message"]["content"]


# --------------------------------------------------------------------------- #
# SECURITY 1 -- cross-conversation control: everything is scoped to the folder
# --------------------------------------------------------------------------- #

def _other_folder(cm, monkeypatch):
    other = cm["tmp"] / "other"
    other.mkdir(exist_ok=True)
    monkeypatch.setattr(A, "_v1_project_cwd", lambda msgs: str(other))
    return str(other)


def test_same_key_in_another_folder_does_not_reattach_or_stop(cm, monkeypatch):
    """Two conversations can share a conversation key (its fallback hashes the
    system prompt + first instruction). A request from ANOTHER folder must
    never re-attach to, or stop, this folder's run."""
    _call()                                        # folder A: owner-1
    A._MULTI_RUNS["owner-1"] = "run-1"             # A's run is live
    _other_folder(cm, monkeypatch)
    monkeypatch.setattr(A, "_multi_wants_a_swarm", lambda t: False)
    resp, _ = _call(messages=[{"role": "user", "content": "stop multi"}])
    assert resp is None, "no folder-B run to talk to: today's path"
    assert cm["stop"] == [] and cm["follow"] == []


def test_same_key_in_another_folder_starts_its_own_run(cm, monkeypatch):
    _call()                                        # folder A: owner-1
    A._MULTI_RUNS["owner-1"] = "run-1"
    other = _other_folder(cm, monkeypatch)
    resp, raw = _call()                            # work, in folder B
    assert resp is not None and cm["follow"] == [] and cm["stop"] == []
    assert [r["project"] for r in cm["start_session"]] == [cm["proj"], other]
    assert "/agent/owner-2" in raw, "folder B got its own owner conversation"


def test_a_live_run_working_in_another_folder_is_not_reattached(cm, monkeypatch):
    """The row's folder matches, but the LIVE run's own project_dir does not:
    fail closed -- no re-attach, no stop."""
    _call()
    A._MULTI_RUNS["owner-1"] = "run-1"
    cm["run_dir"] = str(cm["tmp"])                 # the run works elsewhere
    monkeypatch.setattr(A, "_multi_wants_a_swarm", lambda t: False)
    _call(messages=[{"role": "user", "content": "stop multi"}])
    assert cm["stop"] == [] and cm["follow"] == []


# --------------------------------------------------------------------------- #
# SECURITY 2 -- server-enforced consent (flag cli_multi_confirm, default on)
# --------------------------------------------------------------------------- #

def _with_go(goal_msgs=None, go="go multi"):
    """The CLI's next request after the consent prompt: the whole history,
    ending on the user's reply."""
    return (goal_msgs or USER) + [{"role": "assistant", "content": "Multi starts ..."},
                                  {"role": "user", "content": go}]


@pytest.fixture
def confirm(cm, monkeypatch):
    monkeypatch.setattr(A, "_cm_confirm_on", lambda: True)
    return cm


def test_confirm_flag_reads_the_setting_default_on(monkeypatch):
    monkeypatch.setattr(config, "get_flag", lambda name, default=None: default)
    assert A._cm_confirm_on() is True
    monkeypatch.setattr(config, "get_flag",
                        lambda name, default=None:
                        False if name == "cli_multi_confirm" else default)
    assert A._cm_confirm_on() is False


def test_first_turn_starts_nothing_and_asks_for_go_multi(confirm):
    resp, raw = _call()
    assert resp is not None
    assert confirm["turn_events"] == [] and confirm["start_session"] == [], \
        "nothing starts before the user says go"
    text = _content(raw)
    assert "without asking you" in text and "reply exactly: go multi" in text
    assert confirm["proj"] in text, "names the folder"
    assert "up to 6 helper agents" in text


@pytest.mark.parametrize("protocol", ["openai", "responses", "anthropic"])
def test_the_consent_prompt_is_text_only_in_each_protocol(confirm, protocol):
    resp, raw = _call(protocol=protocol, stream=True)
    assert "go multi" in raw and '"tool_calls"' not in raw
    assert confirm["turn_events"] == []


def test_go_multi_starts_the_stored_goal(confirm):
    _call()                                        # the work -> pending
    resp, raw = _call(messages=_with_go())
    assert resp is not None
    assert len(confirm["turn_events"]) == 1, "the run started on 'go multi'"
    assert confirm["turn_events"][0]["text"] == USER[0]["content"], "with the STORED goal"
    row = A._cm_map_get(_mapkey(confirm))
    assert row["consent"] is True and row["folder"] == A._cm_folder_key(confirm["proj"])
    assert A._CM_PENDING == {}, "the pending request was used up"


@pytest.mark.parametrize("go", ["Go Multi", "go multi!", "  go   multi "])
def test_go_multi_is_matched_after_normalisation(confirm, go):
    _call()
    _call(messages=_with_go(go=go))
    assert len(confirm["turn_events"]) == 1


@pytest.mark.parametrize("go", ["go", "ok go multi", "go multi and also add tests"])
def test_anything_but_exactly_go_multi_does_not_start(confirm, go):
    _call()
    _call(messages=_with_go(go=go))
    assert confirm["turn_events"] == [], "%r is not consent" % go


def test_go_multi_twice_does_not_start_twice(confirm):
    _call()
    _call(messages=_with_go())
    A._MULTI_RUNS.clear()                          # (run ended)
    _resp, raw = _call(messages=_with_go())
    assert len(confirm["turn_events"]) == 1
    assert "Nothing is waiting" in _content(raw)


def test_an_expired_go_multi_answers_nothing_is_waiting(confirm, monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(A, "_cm_now", lambda: now[0])
    _call()                                        # pending, waits 10 minutes
    now[0] += A._CM_PENDING_TTL + 1
    _resp, raw = _call(messages=_with_go())
    assert confirm["turn_events"] == []
    assert "Nothing is waiting" in _content(raw)


def test_a_go_multi_from_another_folder_or_conversation_starts_nothing(confirm, monkeypatch):
    _call()                                        # pending: conv-1, folder A
    other = confirm["tmp"] / "other"
    other.mkdir()
    monkeypatch.setattr(A, "_v1_project_cwd", lambda msgs: str(other))
    _r, raw = _call(messages=_with_go())           # same key, another folder
    assert "Nothing is waiting" in _content(raw)
    monkeypatch.setattr(A, "_v1_project_cwd", lambda msgs: confirm["proj"])
    monkeypatch.setattr(A.ctxwin, "conversation_key", lambda **kw: "conv-2")
    _r, raw = _call(messages=_with_go())           # same folder, another conversation
    assert "Nothing is waiting" in _content(raw)
    assert confirm["turn_events"] == [], "neither mismatched go started a run"
    monkeypatch.setattr(A.ctxwin, "conversation_key", lambda **kw: "conv-1")
    _call(messages=_with_go())                     # the real owner still can
    assert len(confirm["turn_events"]) == 1


def test_consent_is_remembered_for_the_conversation_and_folder(confirm):
    _call()
    _call(messages=_with_go())                     # consent given, run 1
    A._MULTI_RUNS.clear()                          # run 1 ended
    _resp, raw = _call(messages=[{"role": "user",
                                  "content": "now add a footer and a contact form"}])
    assert len(confirm["turn_events"]) == 2, "run 2 started directly, no second prompt"
    assert "without asking you" not in raw


def test_consent_does_not_carry_to_another_folder(confirm, monkeypatch):
    _call()
    _call(messages=_with_go())                     # consent for folder A
    A._MULTI_RUNS.clear()
    _other_folder(confirm, monkeypatch)
    _r, raw = _call()
    assert len(confirm["turn_events"]) == 1, "folder B asks again"
    assert "without asking you" in _content(raw)


def test_confirm_flag_off_starts_directly(cm):
    _resp, raw = _call()                           # fixture default: confirm OFF
    assert len(cm["turn_events"]) == 1
    assert "without asking you" not in raw


def test_go_multi_with_the_flag_off_is_just_a_message(cm, monkeypatch):
    monkeypatch.setattr(A, "_multi_wants_a_swarm", lambda t: False)
    resp, _ = _call(messages=[{"role": "user", "content": "go multi"}])
    assert resp is None and cm["turn_events"] == []


def test_the_pending_store_is_bounded(confirm):
    for i in range(A._CM_PENDING_MAX + 30):
        A._cm_pending_put("k%d" % i, "c", "f", "g")
    assert len(A._CM_PENDING) <= A._CM_PENDING_MAX
