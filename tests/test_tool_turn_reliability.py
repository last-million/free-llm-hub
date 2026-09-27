"""Tool turns: fast, diverse chains; honest truncation; the environment survives.

MEASURED on 8bf19be (live CLI runs through the hub):
  * opencode "read big.txt (~600 KB), reply with its last line": 0/4. One
    ~16K-token tool request spent the whole 240 s deadline on nvidia --
    nvidia/meta/llama-3.2-90b-vision-instruct ReadTimeout, _HopBudgetExceeded,
    then more nvidia. A vision model on a text tool turn; one provider eating
    the deadline.
  * kimi auto: 9 requests in a row 504/503, g4f walking up to 7 relay hops
    (ConnectionError / non-answer).
  * auto answered a last line that is not in the file; coding-swarm / multi
    guessed nonexistent cwd paths (C:\\Users\\...\\Downloads\\big.txt).

Pinned here with fakes (no network, no real config):
  1. tool chains: vision-specialised ids last, measured tool-turn failures
     behind, measured-slow behind quick, <= _TOOL_SPREAD_PER_PROVIDER per
     provider up front; the walk moves on from a provider after a stall, after
     _TOOL_PER_PROVIDER_HOPS failed hops, or past its share of the deadline;
  2. relay (g4f) hops: capped, a failed relay server skipped for the rest of
     the walk and -- after repeated ConnectionError / non-answer -- for tool
     requests altogether;
  3. a trimmed tool result keeps its TRUE last line and says exactly how many
     characters were cut from the MIDDLE;
  4. pipeline members (fan-out and fast path) get the CLI's own system prompt,
     and a trim never drops the environment block (cwd) from it.
"""
import time

import pytest
import requests

import app as A


TOOLS = [{"type": "function", "function": {"name": "read", "parameters": {}}}]


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = {}
        self.text = ""

    def json(self):
        return self._payload

    def close(self):
        pass

    def iter_content(self, chunk_size=None):
        return iter(())

    def iter_lines(self, decode_unicode=False):
        return iter(())


def _tool_call_answer():
    return {"choices": [{"index": 0, "finish_reason": "tool_calls",
                         "message": {"role": "assistant", "content": None,
                                     "tool_calls": [{"id": "c1", "type": "function",
                                                     "function": {"name": "read",
                                                                  "arguments": "{}"}}]}}]}


# --------------------------------------------------------------------------- #
# Fleet for _build_chain
# --------------------------------------------------------------------------- #

@pytest.fixture
def fleet(monkeypatch):
    world = {
        "nvidia": ["meta/llama-3.2-90b-vision-instruct", "gpt-oss-120b-a",
                   "gpt-oss-120b-b", "gpt-oss-120b-c", "gpt-oss-120b-d"],
        "groq": ["qwen3.8-27b"],
        "cerebras": ["glm-5.3"],
    }
    scores = {"meta/llama-3.2-90b-vision-instruct": 150.0,
              "gpt-oss-120b-a": 140.0, "gpt-oss-120b-b": 139.0,
              "gpt-oss-120b-c": 138.0, "gpt-oss-120b-d": 137.0,
              "qwen3.8-27b": 120.0, "glm-5.3": 119.0}
    monkeypatch.setattr(A, "_available_providers", lambda *a, **k: list(world))
    monkeypatch.setattr(A, "_prefetch_auto_models", lambda pids: dict(world))
    monkeypatch.setattr(A, "_auto_models", lambda pid: list(world[pid]))
    monkeypatch.setattr(A, "_provider_capable", lambda pid, est: True)
    monkeypatch.setattr(A, "_supports_tools", lambda pid, m: True)
    monkeypatch.setattr(A, "_is_fast", lambda pid, m: True)
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: scores.get(m, 130.0))
    monkeypatch.setattr(A, "_active_mode", lambda: A.MODE_ALL)
    monkeypatch.setattr(A, "_is_model_dead", lambda pid, m: False)
    monkeypatch.setattr(A.quota, "is_model_throttled", lambda pid, m: False)
    monkeypatch.setattr(A.quota, "model_status", lambda pid, m: {"exhausted": False})
    monkeypatch.setattr(A, "_context_ok", lambda pid, m, est: True)
    monkeypatch.setattr(A, "_sub_available_providers", lambda: [])
    monkeypatch.setattr(A.config, "get_flag", lambda k, d=None: d)
    for name in ("_reliability_penalty", "_latency_penalty", "_answer_quality_penalty",
                 "_sustain_penalty"):
        monkeypatch.setattr(A, name, lambda *a, **k: 0.0)
    monkeypatch.setattr(A, "_chain_reliability_band", lambda pid, m: 0)
    monkeypatch.setattr(A, "_below_declared_window", lambda pid, m: False)
    # gpt-oss is on the shipped last-resort list; this test is about the
    # tool-chain rules, not that list.
    monkeypatch.setattr(A, "_is_low_quality", lambda m: False)
    monkeypatch.setattr(A, "_is_tool_proven", lambda m: "gpt-oss" in m)
    return world


HARD = [{"role": "user", "content": "Read big.txt and refactor the parser in src/parse.py."}]


def _tool_chain(primary=("", "")):
    with A.app.test_request_context():
        A._mark_turn_shape("hard", 16000)
        return A._build_chain(primary[0], primary[1], 16000, require_tools=True,
                              messages=HARD)


def test_vision_specialised_ids_are_recognised():
    for m in ("meta/llama-3.2-90b-vision-instruct", "qwen/qwen2.5-vl-72b-instruct",
              "llava-hf/llava-1.6", "microsoft/phi-3.5-vision-instruct",
              "nvidia/nemotron-nano-12b-v2-vl"):
        assert A._is_vision_specialised(m), m
    for m in ("google/gemini-3-flash", "meta/llama-4-maverick", "z-ai/glm-5.3",
              "gpt-oss-120b", "qwen3.8-27b", "revision-model"):
        assert not A._is_vision_specialised(m), m


def test_a_vision_model_goes_behind_every_other_candidate_on_a_tool_turn(fleet):
    chain = _tool_chain()
    assert chain[-1] == ("nvidia", "meta/llama-3.2-90b-vision-instruct"), chain
    assert len(chain) == 7


def test_the_router_never_opens_a_tool_turn_on_a_vision_model(fleet, monkeypatch):
    monkeypatch.setattr(A, "_session_pin_get", lambda key: None)
    monkeypatch.setattr(A, "_session_pin_set", lambda *a, **k: None)
    monkeypatch.setattr(A, "_weighted_pick", lambda pool, *a, **k: max(pool))
    with A.app.test_request_context():
        pid, model, _d = A._route_by_difficulty(HARD, None, 16000, require_tools=True,
                                                force_difficulty="hard")
    assert not A._is_vision_specialised(model), model


def test_a_real_tool_chain_keeps_its_strength_order(fleet):
    """The per-provider caps act in the WALK on hops that failed (see the
    end-to-end tests below), not on the ranking of a healthy chain."""
    chain = _tool_chain()
    assert chain[:4] == [("nvidia", "gpt-oss-120b-a"), ("nvidia", "gpt-oss-120b-b"),
                         ("nvidia", "gpt-oss-120b-c"), ("nvidia", "gpt-oss-120b-d")], chain


def test_measured_tool_turn_failures_go_behind(fleet):
    with A.app.test_request_context():
        A.g.hub_tool_turn = True
        for _ in range(4):
            A._note_tool_turn_outcome("nvidia", "gpt-oss-120b-a", False)
    assert A._tool_turn_sick("nvidia", "gpt-oss-120b-a")
    chain = _tool_chain()
    healthy = [e for e in chain if e != ("nvidia", "gpt-oss-120b-a")
               and not A._is_vision_specialised(e[1])]
    assert chain.index(("nvidia", "gpt-oss-120b-a")) > max(chain.index(e) for e in healthy)


def test_outcomes_outside_a_tool_turn_do_not_count(fleet):
    with A.app.test_request_context():
        for _ in range(4):
            A._note_tool_turn_outcome("nvidia", "gpt-oss-120b-a", False)
    assert A._tool_turn_reliability("nvidia", "gpt-oss-120b-a") is None


def test_measured_slow_first_content_goes_behind_quick_ones(fleet):
    with A._outcome_lock:
        A._tool_ttft[("nvidia", "gpt-oss-120b-a")] = [60000.0] * 4
    assert A._tool_turn_slow("nvidia", "gpt-oss-120b-a")
    assert not A._tool_turn_slow("nvidia", "gpt-oss-120b-b"), "unmeasured is not slow"
    chain = _tool_chain()
    assert chain.index(("nvidia", "gpt-oss-120b-b")) < chain.index(("nvidia", "gpt-oss-120b-a"))


def test_tool_turn_ttft_is_recorded_in_its_own_bucket():
    with A.app.test_request_context():
        A.g.hub_tool_turn = True
        A._record_ttft("p", "m", 1234.0)
    assert A._tool_ttft.get(("p", "m")) == [1234.0]
    with A.app.test_request_context():
        A._record_ttft("p", "m2", 99.0)
    assert ("p", "m2") not in A._tool_ttft


# --------------------------------------------------------------------------- #
# Relay servers
# --------------------------------------------------------------------------- #

def test_relay_server_ids():
    sid = A._relay_server_id
    assert sid("g4f", "srv_mkom688d:openai/gpt-oss-120b") == "g4f|srv_mkom688d"
    assert sid("g4f", "pa:657cce02:auto") == "g4f|pa:657cce02"
    assert sid("g4f", "G4FSpace:srv_x:z-ai/glm-5.3") == "g4f|G4FSpace:srv_x"
    assert sid("g4f", "RelayRouter:gemini-3.7-flash-free") == "g4f|RelayRouter"
    assert sid("g4f", "srv_mqjxnj9i:gemma4:latest") == "g4f|srv_mqjxnj9i"
    assert sid("g4f", "gpt-4o") == "g4f|gpt-4o"
    assert sid("nvidia", "z-ai/glm-5.3") is None


def test_a_relay_server_that_keeps_failing_is_skipped_then_forgotten(monkeypatch):
    A._note_relay_tool_fail("g4f", "srv_a:m1")
    assert not A._relay_tool_sick("g4f", "srv_a:m2"), "one failure is not a pattern"
    A._note_relay_tool_fail("g4f", "srv_a:m2")
    assert A._relay_tool_sick("g4f", "srv_a:other-model"), "per SERVER, not per model"
    assert not A._relay_tool_sick("g4f", "srv_b:m1")
    later = time.monotonic() + A._RELAY_TOOL_FAIL_TTL + 1
    monkeypatch.setattr(A.time, "monotonic", lambda: later)
    assert not A._relay_tool_sick("g4f", "srv_a:m1")


def test_cap_relay_hops_drops_sick_servers_and_caps_the_rest():
    A._note_relay_tool_fail("g4f", "srv_a:m1")
    A._note_relay_tool_fail("g4f", "srv_a:m1")
    chain = [("g4f", "srv_a:m1"), ("nvidia", "x"), ("g4f", "srv_b:m"), ("g4f", "srv_c:m"),
             ("g4f", "srv_d:m"), ("g4f", "srv_e:m"), ("groq", "y")]
    out = A._cap_relay_hops(chain)
    assert ("g4f", "srv_a:m1") not in out
    assert sum(1 for p, _ in out if p == "g4f") == A._TOOL_RELAY_MAX_HOPS
    assert out[0] == ("nvidia", "x") and out[-1] == ("groq", "y")
    only_sick = [("g4f", "srv_a:m1")]
    assert A._cap_relay_hops(only_sick) == only_sick, "fail-open"


def test_a_tool_chain_carries_at_most_the_relay_cap(fleet):
    fleet["g4f"] = ["srv_%d:model-%d" % (i, i) for i in range(7)]
    chain = _tool_chain()
    assert sum(1 for p, _ in chain if p == "g4f") <= A._TOOL_RELAY_MAX_HOPS, chain


def test_a_non_answer_on_a_tool_turn_is_a_relay_strike(monkeypatch):
    monkeypatch.setattr(A, "_mark_model_dead", lambda *a, **k: None)
    monkeypatch.setattr(A, "_record_outcome", lambda *a, **k: None)
    with A.app.test_request_context():
        A.g.hub_tool_turn = True
        A._note_nonanswer("g4f", "srv_z:m", kind=None)
        A._note_nonanswer("g4f", "srv_z:m2", kind=None)
    assert A._relay_tool_sick("g4f", "srv_z:m3")


# --------------------------------------------------------------------------- #
# The walk, end to end through /v1/chat/completions
# --------------------------------------------------------------------------- #

@pytest.fixture
def quiet(monkeypatch):
    for name in ("_record_chat_usage", "_record_outcome", "_save_perf_stats",
                 "_act_pick", "_note_ttft", "_record_stream_outcome",
                 "_note_provider_timeout", "_throttle_failed_hop"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)
    monkeypatch.setattr(A, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(A, "_model_block_reason", lambda pid, m: None)
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    monkeypatch.setattr(A.config, "get_flag", lambda k, d=None: d)
    monkeypatch.setattr(A, "_swarm_fast_path", lambda *a, **k: False)
    yield


def _route_and_chain(monkeypatch, *hops):
    monkeypatch.setattr(A, "_route_by_difficulty",
                        lambda *a, **k: (hops[0][0], hops[0][1], "hard"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: list(hops))


def _post_tool_turn():
    return A.app.test_client().post("/v1/chat/completions", json={
        "model": "auto", "stream": False, "tools": TOOLS,
        "messages": [{"role": "system", "content": "<env>\nWorking directory: /w\n</env>"},
                     {"role": "user", "content": "Read big.txt and reply with its last line."}]})


def test_a_provider_that_timed_out_goes_behind_the_others(quiet, monkeypatch):
    _route_and_chain(monkeypatch, ("nvidia", "a"), ("nvidia", "b"), ("nvidia", "c"),
                     ("groq", "q"))
    calls = []

    def dispatch(pid, payload, stream):
        calls.append((pid, payload["model"]))
        if pid == "nvidia":
            raise requests.exceptions.ReadTimeout("slow")
        return _Resp(200, _tool_call_answer())
    monkeypatch.setattr(A, "_dispatch_chat", dispatch)
    r = _post_tool_turn()
    assert r.status_code == 200, r.get_data(as_text=True)
    assert calls == [("nvidia", "a"), ("groq", "q")], calls


def test_a_provider_gets_at_most_two_failed_hops_before_the_others(quiet, monkeypatch):
    _route_and_chain(monkeypatch, ("nvidia", "a"), ("nvidia", "b"), ("nvidia", "c"),
                     ("nvidia", "d"), ("groq", "q"))
    calls = []

    def dispatch(pid, payload, stream):
        calls.append((pid, payload["model"]))
        if pid == "nvidia":
            return _Resp(429)
        return _Resp(200, _tool_call_answer())
    monkeypatch.setattr(A, "_dispatch_chat", dispatch)
    r = _post_tool_turn()
    assert r.status_code == 200
    assert calls == [("nvidia", "a"), ("nvidia", "b"), ("groq", "q")], calls


def test_one_provider_cannot_eat_the_whole_deadline(quiet, monkeypatch):
    """A hanging hop is cut at the provider's share of the deadline while
    another provider still waits -- not at the deadline itself."""
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: 6.0)
    monkeypatch.setattr(A, "_TOOL_PROVIDER_MIN_SECONDS", 1.0)
    monkeypatch.setattr(A, "_TOOL_PROVIDER_MIN_HOP", 0.5)
    monkeypatch.setattr(A, "_ADAPTIVE_HOP_FLOOR", 0.1)
    _route_and_chain(monkeypatch, ("nvidia", "a"), ("nvidia", "b"), ("groq", "q"))
    calls = []

    def dispatch(pid, payload, stream):
        calls.append(pid)
        if pid == "nvidia":
            time.sleep(8)
        return _Resp(200, _tool_call_answer())
    monkeypatch.setattr(A, "_dispatch_chat", dispatch)
    t0 = time.monotonic()
    r = _post_tool_turn()
    took = time.monotonic() - t0
    assert r.status_code == 200, r.get_data(as_text=True)
    assert calls == ["nvidia", "groq"], calls
    assert took < 4.5, took          # 50% of 6 s, not all of it


def test_relay_hops_are_capped_and_a_failed_server_is_not_retried(quiet, monkeypatch):
    _route_and_chain(monkeypatch, ("g4f", "srv_a:m1"), ("g4f", "srv_a:m2"),
                     ("g4f", "srv_b:m1"), ("g4f", "srv_c:m1"), ("g4f", "srv_d:m1"),
                     ("groq", "q"))
    monkeypatch.setattr(A, "_TOOL_PER_PROVIDER_HOPS", 99)   # isolate the relay rules
    calls = []

    def dispatch(pid, payload, stream):
        calls.append((pid, payload["model"]))
        if pid == "g4f":
            raise requests.exceptions.ConnectionError("relay down")
        return _Resp(200, _tool_call_answer())
    monkeypatch.setattr(A, "_dispatch_chat", dispatch)
    r = _post_tool_turn()
    assert r.status_code == 200
    relay = [c for c in calls if c[0] == "g4f"]
    assert ("g4f", "srv_a:m2") not in calls, "same relay server, same failure"
    assert len(relay) == A._TOOL_RELAY_MAX_HOPS, calls
    assert calls[-1] == ("groq", "q")
    # ...and those ConnectionErrors are strikes against each server.
    A._note_relay_tool_fail("g4f", "srv_b:m1")
    assert A._relay_tool_sick("g4f", "srv_b:anything")


def test_a_plain_chat_turn_keeps_the_chain_as_built(quiet, monkeypatch):
    _route_and_chain(monkeypatch, ("nvidia", "a"), ("nvidia", "b"), ("nvidia", "c"),
                     ("groq", "q"))
    calls = []

    def dispatch(pid, payload, stream):
        calls.append(payload["model"])
        if payload["model"] != "c":
            return _Resp(429)
        return _Resp(200, {"choices": [{"index": 0, "finish_reason": "stop",
                                        "message": {"role": "assistant",
                                                    "content": "The parser is fine."}}]})
    monkeypatch.setattr(A, "_dispatch_chat", dispatch)
    r = A.app.test_client().post("/v1/chat/completions", json={
        "model": "auto", "stream": False,
        "messages": [{"role": "user", "content": "Explain in depth how a B-tree rebalances."}]})
    assert r.status_code == 200
    assert calls == ["a", "b", "c"]


# --------------------------------------------------------------------------- #
# Truncation keeps the true tail and says what it cut
# --------------------------------------------------------------------------- #

LAST = "THE-LAST-LINE-112225"
BIG_TXT = "".join("line %05d filler text to make this file large enough\n" % i
                  for i in range(12000)) + LAST + "\n"


def _read_loop(content):
    return [
        {"role": "system", "content": "You are an agent."},
        {"role": "user", "content": "Read big.txt and reply with ONLY its last line."},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "read", "arguments": "{\"filePath\": \"big.txt\"}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": content},
    ]


@pytest.mark.parametrize("budget", [8000, 32768])
def test_a_trimmed_tool_result_keeps_its_true_last_line(budget):
    out, did = A._compact_to_budget(_read_loop(BIG_TXT), TOOLS, budget)
    assert did
    content = [m for m in out if m.get("role") == "tool"][0]["content"]
    assert content.rstrip("\n").endswith(LAST), content[-200:]
    assert "from the MIDDLE" in content and "true last line" in content
    marker_at = content.index("[... ")
    tail = content[content.index("]\n\n", marker_at) + 3:]
    head = content[:marker_at].rstrip("\n")
    # The count in the marker is exactly what is missing.
    n = int(content[marker_at + 5:].split(" ", 1)[0])
    assert len(head) + 1 + n + len(tail) == len(BIG_TXT) or \
        len(head) + n + len(tail) == len(BIG_TXT)
    assert BIG_TXT.endswith(tail), "the tail is verbatim"
    assert tail.startswith("line "), "the tail starts on a line boundary"
    assert A._est_tokens(out, TOOLS) <= int(budget * 0.85)


def test_a_long_last_line_is_kept_whole():
    text = "short\n" * 20000 + "Z" * 3000
    out, did = A._compact_to_budget(_read_loop(text), TOOLS, 8000)
    content = [m for m in out if m.get("role") == "tool"][0]["content"]
    assert content.endswith("Z" * 3000)


def test_tool_content_in_text_parts_is_trimmed_too():
    parts = [{"type": "text", "text": BIG_TXT}]
    out, did = A._compact_to_budget(_read_loop(parts), TOOLS, 8000)
    assert did
    content = [m for m in out if m.get("role") == "tool"][0]["content"]
    assert isinstance(content, str) and content.rstrip("\n").endswith(LAST)
    assert A._est_tokens(out, TOOLS) <= int(8000 * 0.85)


def test_a_non_tool_message_says_the_middle_was_cut():
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "A" * 400000}]
    out, did = A._compact_to_budget(msgs, None, 8000)
    assert did and "from the MIDDLE" in out[1]["content"]


# --------------------------------------------------------------------------- #
# The environment survives trimming; pipeline members see the CLI's prompt
# --------------------------------------------------------------------------- #

CWD = "C:\\Users\\hamza\\work\\proj"


def _kimi_system():
    """Kimi Code's shape (measured): the cwd sits at ~61% of ~20K chars."""
    before = ("You are Kimi Code CLI. Rules about tools and style.\n" * 240)
    after = ("More guidance about context management and dates.\n" * 150)
    return (before + "\n## Working Directory\n\nThe current working directory is `"
            + CWD + "`.\n\n" + after)


def _opencode_system():
    provider = "You are opencode, an interactive CLI agent.\n" * 400
    env = ("Here is some useful information about the environment you are running in:\n"
           "<env>\n  Working directory: " + CWD + "\n  Is directory a git repo: no\n"
           "  Platform: win32\n  Today's date: Sun Sep 27 2026\n</env>\n")
    instructions = "Instructions from: C:\\Users\\hamza\\.claude\\CLAUDE.md\n" + (
        "Use caveman mode. Keep responses short.\n" * 500)
    return provider + env + instructions


@pytest.mark.parametrize("system,budget", [(_kimi_system(), 4000),
                                           (_opencode_system(), 8000)],
                         ids=["kimi-code", "opencode"])
def test_a_trimmed_system_prompt_keeps_the_working_directory(system, budget):
    msgs = [{"role": "system", "content": system},
            {"role": "user", "content": "Read big.txt in the current folder and reply "
                                        "with ONLY its last line."}]
    assert A._est_tokens(msgs, TOOLS) > budget * 0.85
    out, did = A._compact_to_budget(msgs, TOOLS, budget)
    assert did
    sys_text = out[0]["content"]
    assert "omitted by the hub" in sys_text, "the prompt was really trimmed"
    assert CWD in sys_text, "the cwd must survive the trim"
    if "<env>" in system:
        env = system[system.index("<env>"):system.index("</env>") + 6]
        assert env in sys_text, "the <env> block is carried verbatim"
    assert A._est_tokens(out, TOOLS) <= int(budget * 0.85)


def test_a_rule_mentioning_the_working_directory_is_not_mistaken_for_a_fact():
    kept = A._protected_spans("Never touch files outside the working directory.\n"
                              "The current working directory is `/srv/app`.\n")
    assert "/srv/app" in kept and "Never touch" not in kept


def test_fan_out_members_see_the_clis_system_prompt(monkeypatch):
    body = {"model": "swarm", "tools": TOOLS, "stream": False,
            "messages": [{"role": "system", "content": _opencode_system()},
                         {"role": "user", "content": "Read big.txt and reply with its "
                                                     "last line."}]}
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("p1", "m1", "hard"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("p1", "m1"), ("p2", "m2"),
                                                            ("p3", "m3")])
    monkeypatch.setattr(A, "_swarm_rank", lambda cands, diff: list(cands))
    for name in ("_record_outcome", "_record_chat_usage", "_save_perf_stats"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)
    seen = []

    def dispatch(pid, payload, deadline):
        seen.append(payload)
        return _Resp(200, _tool_call_answer()), None
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", dispatch)
    with A.app.test_request_context():
        out = A._swarm_tool_result(body)
    assert out is not None and len(seen) == 3
    for p in seen:
        assert p["messages"] == body["messages"], "a member lost part of the prompt"
        assert "<env>" in p["messages"][0]["content"]


def test_the_fast_path_hands_the_whole_prompt_to_the_strong_model(monkeypatch):
    body = {"model": "coding-swarm", "tools": TOOLS, "stream": False,
            "messages": [{"role": "system", "content": _opencode_system()},
                         {"role": "user", "content": "What is in big.txt?"}]}
    monkeypatch.setattr(A, "_swarm_fast_path", lambda *a, **k: True)
    got = {}

    def uncached(b):
        got.update(b)
        return A.jsonify(_tool_call_answer())
    monkeypatch.setattr(A, "_chat_completions_uncached", uncached)
    with A.app.test_request_context():
        A._swarm_completion(dict(body))
    assert got["model"] == "best"
    assert got["messages"] == body["messages"]
    assert got["tools"] == TOOLS
