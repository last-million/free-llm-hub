"""Review findings on memory, context and CLI routing, each pinned.

Memory: the fact harvest kept credentials, one-off task requests and passing
notes as lasting PROJECT facts; its "verified commands" collected non-commands
and split compound ones; a rewind left the undone turns' project facts; a
losing second send deleted the running turn's in-flight marker; and the
marker's pid check skipped every marker left across an os.execv restart.

Context: compaction charged every kept message the 400-token request
overhead; parallel Codex calls were split from their results; a CLI's own
compaction request and a request whose bigger hops only 429'd got the native
overflow error; a rewound or deleted /agent session kept its rolling recap;
the overflow body could state a request smaller than its window; the mode
failed open per speed tier.

CLIs: the provider-error heuristic flagged ordinary short answers, and its
kind was lost across hedge legs; a codex Connect did not rewrite the /model
catalog. No network: every upstream is a fake.
"""
import json
import os
import shutil
import tempfile
import time

import pytest

import agentic_chat as AC
import app as A
import ctxwin
import memory


@pytest.fixture
def mem_dir(monkeypatch):
    d = tempfile.mkdtemp(prefix="hubmem-")
    monkeypatch.setenv(memory._ROOT_ENV, d)
    monkeypatch.setattr(memory, "_FACT_EXTRACTOR", None)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Memory: what the harvest keeps
# --------------------------------------------------------------------------- #

def test_secrets_are_never_harvested(mem_dir):
    proj = os.path.join(mem_dir, "p")
    os.makedirs(proj)
    memory.harvest_facts(
        "s1", request="Always use my OpenAI key sk-proj-AbC123xyz456 for the widget.",
        reply="Note: the admin password is hunter2secret\nDecision: use pnpm, not npm.\n"
              "All tests passed.",
        project_dir=proj, tools=["bash: OPENAI_API_KEY=sk-live-999999 pytest -q"])
    facts = memory.project_facts(proj)
    blob = "\n".join(facts)
    assert "sk-proj" not in blob and "hunter2" not in blob and "sk-live" not in blob
    assert "Decision: use pnpm, not npm." in facts
    cmds = [f for f in facts if f.startswith(memory.COMMANDS_FACT_PREFIX)]
    assert cmds and "`pytest -q`" in cmds[0]


def test_the_extractor_path_drops_secrets_too(mem_dir):
    # A fake token, assembled at runtime so the push secret-scan never sees a
    # token-shaped literal in the source.
    memory._run_extractor(lambda q, r: ["The deploy token is " + "gh" + "p_abcdefghijkl123456",
                                        "The deploy target is Fly.io."],
                          "s2", "x", "y", None)
    facts = memory.get("s2").get("facts") or []
    assert facts == ["The deploy target is Fly.io."]


@pytest.mark.parametrize("text", [
    "I want you to build a landing page for my bakery.",
    "Use my OpenAI key for the chat widget.",
    "Use the following layout for the page.",
    "Use React to build the dashboard.",
])
def test_a_one_off_task_request_is_not_a_preference(text):
    assert memory.harvest_preferences(text) == []


@pytest.mark.parametrize("text, want", [
    ("Always use tabs.", "User preference: Always use tabs"),
    ("From now on, reply in French.", "User preference: From now on, reply in French"),
    ("Use pnpm, not npm.", "User preference: Use pnpm, not npm"),
])
def test_standing_rules_are_still_preferences(text, want):
    assert memory.harvest_preferences(text) == [want]


def test_a_status_note_is_not_a_decision():
    got = memory.harvest_decisions("Note: I could not find the file yet.\n"
                                   "Note: the API is versioned under /v2.")
    assert got == ["Note: the API is versioned under /v2."]


def test_only_real_verify_commands_are_verified():
    tools = ["Read: pytest.ini", "bash: cat ruff.toml", "Grep: jest in src",
             "bash: rm -rf dist && tsc", "bash: cd web && npm run build",
             "python -m pytest -q"]
    assert memory.harvest_commands(tools, "All tests passed.") == [
        "python -m pytest -q", "cd web && npm run build"]


def test_a_compound_command_survives_the_rolling_merge(mem_dir):
    proj = os.path.join(mem_dir, "p2")
    os.makedirs(proj)
    memory.harvest_facts("s1", reply="All tests passed.", project_dir=proj,
                         tools=["bash: cd web; npm run build"])
    memory.harvest_facts("s1", reply="All tests passed.", project_dir=proj,
                         tools=["bash: pytest -q"])
    cmds = [f for f in memory.project_facts(proj)
            if f.startswith(memory.COMMANDS_FACT_PREFIX)]
    assert cmds == [memory.COMMANDS_FACT_PREFIX + "`pytest -q`; `cd web; npm run build`"]


# --------------------------------------------------------------------------- #
# Memory: rewind, in-flight markers
# --------------------------------------------------------------------------- #

def test_rewind_takes_back_this_conversations_project_facts(mem_dir):
    proj = os.path.join(mem_dir, "p3")
    os.makedirs(proj)
    memory.harvest_facts("sA", reply="Decision: vanilla JS only.", project_dir=proj)
    time.sleep(0.05)
    cut = time.time()
    time.sleep(0.05)
    memory.harvest_facts("sA", reply="Decision: add a newsletter page.", project_dir=proj,
                         tools=["write newpage.html"])
    memory.harvest_facts("sB", reply="Decision: keep port 8080.", project_dir=proj)
    memory.rewind("sA", cutoff=cut, project_dir=proj)
    facts = memory.project_facts(proj)
    assert "Decision: vanilla JS only." in facts           # before the cut
    assert "Decision: keep port 8080." in facts            # another conversation's
    assert "Decision: add a newsletter page." not in facts
    assert not any(f.startswith(memory.FILES_FACT_PREFIX) for f in facts)


def test_a_losing_second_send_leaves_the_running_turns_marker(mem_dir, monkeypatch):
    memory.begin_inflight("r1", "the live turn")

    def stream(session_id, text):
        yield {"event": "error", "status": 409,
               "detail": "A turn is already running for this session."}
    monkeypatch.setattr(AC, "send_message_stream", stream)
    monkeypatch.setattr(AC, "get_session", lambda sid: None)
    monkeypatch.setattr(AC, "turn_busy", lambda sid: False)       # lost the race
    list(AC.send_message_stream_durable("r1", "a second tab"))
    rec = memory.inflight("r1")
    assert rec is not None and rec["request"] == "the live turn"
    assert memory.touch_inflight("r1", doing=["x"], force=True) is True


def test_a_real_turn_still_leaves_and_clears_its_marker(mem_dir, monkeypatch):
    seen = []

    def stream(session_id, text):
        yield {"event": "notice", "text": "starting"}
        seen.append(memory.inflight("r2"))
        yield {"event": "done", "text": "All done."}
    monkeypatch.setattr(AC, "send_message_stream", stream)
    monkeypatch.setattr(AC, "get_session", lambda sid: None)
    list(AC.send_message_stream_durable("r2", "build it"))
    assert seen and seen[0] and seen[0]["request"] == "build it"
    assert memory.inflight("r2") is None


def test_a_marker_from_before_an_execv_is_recovered(mem_dir):
    """Same pid (os.execv on POSIX), different process image."""
    memory.note_turn("x1")
    memory.begin_inflight("x1", "build the shop")
    rec = memory.inflight("x1")
    rec["boot"] = "an-earlier-image"
    with open(memory._inflight_path("x1"), "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
    assert rec["pid"] == os.getpid()
    assert memory.recover_inflight() == ["x1"]


# --------------------------------------------------------------------------- #
# Context
# --------------------------------------------------------------------------- #

def _tool_pairs(n):
    out = []
    for i in range(n):
        out.append({"role": "assistant", "content": None, "tool_calls": [{
            "id": "c%d" % i, "type": "function",
            "function": {"name": "read", "arguments": "{\"p\": \"f%d\"}" % i}}]})
        out.append({"role": "tool", "tool_call_id": "c%d" % i,
                    "content": "line %d of the file\n" % i * 6})
    return out


def test_a_request_slightly_over_target_keeps_most_of_its_history():
    msgs = ([{"role": "system", "content": "s" * 20000},
             {"role": "user", "content": "refactor the parser"}]
            + _tool_pairs(150))
    est = A._est_tokens(msgs)
    budget = int(est * 0.95 / 0.85)            # target ~5% under the request
    st = {}
    out, did = A._compact_to_budget(msgs, None, budget, stats=st, abort_frac=0.30)
    assert not st.get("overflow"), st
    assert st.get("dropped_frac", 0) < 0.30, st


def test_parallel_calls_and_their_results_are_one_unit():
    big = "BIGFILE " * 5000
    rest = [{"role": "user", "content": "look at both"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "b", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "a", "content": big},
            {"role": "tool", "tool_call_id": "b", "content": "small"}]
    units = A._message_units(rest)
    assert [len(u) for u in units] == [1, 4]
    out, _did = A._compact_to_budget([{"role": "system", "content": "sys"}] + rest,
                                     None, 8000)
    out = A._sanitize_tool_messages(out)
    calls = {tc["id"] for m in out if m.get("role") == "assistant"
             for tc in m.get("tool_calls") or []}
    results = {m.get("tool_call_id") for m in out if m.get("role") == "tool"}
    assert results and results <= calls
    assert "a" in results                       # the big result kept, trimmed


class _Resp:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text


def test_a_cli_compaction_request_never_gets_the_overflow_error():
    msgs = [{"role": "user", "content": "x " * 100},
            {"role": "user", "content": "Your task is to create a detailed summary of "
                                        "the conversation so far."}]
    with A.app.test_request_context("/v1/messages"):
        A._ctx_begin({}, msgs, 90000)
        A._ctx_note_tried("p", "m")
        A._ctx_note_overflow_resp(_Resp(400, "This model's maximum context length is "
                                             "32768 tokens."), "p", "m")
        assert A._ctx_overflow_reply("anthropic") is None


def test_no_overflow_error_when_a_bigger_hop_only_rate_limited():
    with A.app.test_request_context("/v1/chat/completions"):
        A._ctx_begin({}, [], 60000)
        A._ctx_note_tried("big", "m200k")          # answered 429
        A._ctx_note_tried("small", "m32k")
        A._ctx_note_overflow(32768, pid="small", model="m32k")
        A._ctx_note_overflow(32768, pid="small", model="m32k")   # counted once
        assert A._ctx_g("_ctx_overflow")["hops"] == 1
        assert A._ctx_overflow_reply("openai") is None
        A._ctx_note_overflow(200000, pid="big", model="m200k")
        assert A._ctx_overflow_reply("openai") is not None


def test_the_overflow_body_always_states_a_request_over_its_limit():
    msg = ctxwin.anthropic_overflow_body(90000, 100000)["error"]["message"]
    assert msg == "prompt is too long: 100001 tokens > 100000 maximum"
    msg = ctxwin.openai_overflow_body(18911, 21124)["error"]["message"]
    assert "maximum context length is 21124" in msg and "resulted in 21125" in msg
    assert "150000 tokens > 100000" in ctxwin.anthropic_overflow_body(
        150000, 100000)["error"]["message"]


def test_an_agent_sessions_recap_goes_with_delete_and_rewind(monkeypatch):
    gone = []

    class _Store:
        def delete(self, key):
            gone.append(key)
    monkeypatch.setattr(A, "_recap_store", _Store())
    monkeypatch.setattr(A.memory, "forget", lambda sid: None)
    A._forget_conversation_state("sid-1")
    assert gone == ["agent:sid-1"]
    src = open(A.__file__, encoding="utf-8").read()
    body = src[src.index("memory.rewind(session_id, cutoff="):]
    assert "_forget_agent_recap(session_id)" in body[:900]


# The mode is applied over BOTH speed tiers at once.
PIDS = ["pa", "pb"]
WORLD = {p: [p + "-coder", p + "-lyria"] for p in PIDS}
MSGS = [{"role": "user", "content": "fix the failing test"}]


@pytest.fixture
def fleet(monkeypatch):
    monkeypatch.setattr(A, "_available_providers", lambda *a, **k: list(PIDS))
    monkeypatch.setattr(A, "_prefetch_auto_models", lambda pids: dict(WORLD))
    monkeypatch.setattr(A, "_auto_models", lambda pid: list(WORLD[pid]))
    monkeypatch.setattr(A, "_provider_capable", lambda pid, est: True)
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: 134.0)
    monkeypatch.setattr(A, "_supports_tools", lambda pid, m: True)
    monkeypatch.setattr(A, "_session_pin_get", lambda key: None)
    monkeypatch.setattr(A, "_session_pin_set", lambda *a, **k: None)
    monkeypatch.setattr(A, "_weighted_pick", lambda pool, *a, **k: max(pool))
    monkeypatch.setattr(A.model_categories, "matches",
                        lambda key, p, m, i=None: m.endswith("-coder"))
    # Every in-mode model is SLOW, every out-of-mode one FAST.
    monkeypatch.setattr(A, "_is_fast", lambda p, m: m.endswith("-lyria"))
    yield


def test_the_chain_stays_in_mode_when_the_mode_is_all_slow(fleet, monkeypatch):
    monkeypatch.setattr(A, "_active_mode", lambda: "coding")
    chain = A._build_chain("pa", "pa-coder", 500, require_tools=True, messages=MSGS)
    assert chain and all(m.endswith("-coder") for _p, m in chain), chain


# --------------------------------------------------------------------------- #
# CLIs: provider-error heuristic, hedge kinds, codex catalog
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("prompt, text", [
    ("Is gpt-5 available on Azure?", "No, the model is not available on Azure yet."),
    ("summarise the log", "The job failed because the service is temporarily unavailable."),
    ("what happened?", "The deploy ran for 3 minutes. Then the registry returned 429 "
                       "Too Many Requests."),
])
def test_ordinary_short_answers_are_not_provider_errors(prompt, text):
    assert A._provider_error_kind(text, prompt) is None


def test_a_turn_answering_tool_results_is_never_judged():
    text = ("The deploy failed: the registry returned 429 Too Many Requests. "
            "Please try again later.")
    assert A._provider_error_kind(text, "run the deploy script") == "provider_quota"
    body = {"model": "auto", "messages": [
        {"role": "user", "content": "run the deploy script"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "bash",
                                           "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t",
                                      "content": "429 Too Many Requests"}]}]}
    with A.app.test_request_context("/v1/messages", method="POST", json=body):
        prompt = A._request_prompt_text()
        assert prompt == A._TOOL_RESULT_TURN
        assert A._is_upstream_nonanswer(text) is False
    body = {"model": "auto", "messages": [
        {"role": "user", "content": "go"},
        {"role": "tool", "tool_call_id": "c", "content": "service unavailable"}]}
    with A.app.test_request_context("/v1/chat/completions", method="POST", json=body):
        assert A._request_prompt_text() == A._TOOL_RESULT_TURN


class _JsonResp:
    status_code = 200

    def __init__(self, content):
        self._data = {"choices": [{"index": 0, "finish_reason": "stop",
                                   "message": {"role": "assistant", "content": content}}]}

    def json(self):
        return self._data

    def close(self):
        pass


def test_hedge_losers_each_keep_their_own_nonanswer_kind(monkeypatch):
    seen = {"throttle": [], "dead": []}
    monkeypatch.setattr(A, "_record_outcome", lambda *a, **k: None)
    monkeypatch.setattr(A, "_throttle_failed_hop",
                        lambda p, m, exc=None, secs=None: seen["throttle"].append(p))
    monkeypatch.setattr(A, "_mark_model_dead", lambda p, m, s: seen["dead"].append(p))
    quota_leg = (0, "json", _JsonResp("The API key used for this request has reached "
                                      "its usage limit."), None)
    relay_leg = (1, "json", _JsonResp("No cake credits. Bake proof-of-work cakes at "
                                      "g4f.dev/chat"), None)
    legs = {0: ("relay", "m0", {}, 0.0), 1: ("g4f", "m1", {}, 0.0)}
    verdicts = {0: A._hedge_leg_verdict(quota_leg, {}),
                1: A._hedge_leg_verdict(relay_leg, {})}
    assert verdicts == {0: "nonanswer", 1: "nonanswer"}
    A._ChainClock()._record_losers({0: quota_leg, 1: relay_leg}, legs, verdicts)
    assert seen["throttle"] == ["relay"]            # a quota hit: cooled down
    assert seen["dead"] == ["g4f"]                  # the relay page: dead-marked


def test_a_codex_connect_rewrites_the_model_catalog(monkeypatch):
    import config
    calls = []
    entry = A._get_cli_entry("codex")
    monkeypatch.setitem(A._AUTOFIXERS, entry["autofix"],
                        lambda *a, **k: {"ok": True})
    monkeypatch.setattr(A, "_first_free_model_id", lambda: "groq/m")
    monkeypatch.setattr(A, "_mark_hub_mode_unmanaged", lambda: None)
    monkeypatch.setattr(A, "_refresh_codex_catalog_soon", lambda: calls.append(1))
    A.app.config["TESTING"] = True
    with A.app.test_client() as c:
        r = c.post("/api/clis/codex/autofix",
                   headers={"X-Free-LLM-Hub-Token": config.ensure_control_token(),
                            "X-Free-LLM-Hub": "dashboard"})
    assert r.get_json().get("ok") is True
    assert calls == [1]
    src = open(A.__file__, encoding="utf-8").read()
    body = src[src.index("def _bulk_hub_on("):]
    body = body[:body.index("\ndef ")]
    assert '"codex" in changed_ids' in body and "_refresh_codex_catalog_soon()" in body
