"""Tool turns that time out behind text answers and slow hops.

MEASURED 2026-09-27 on 7cb23ff (Claude Code 2.1.283 with a throwaway
CLAUDE_CONFIG_DIR; opencode with a temp XDG_CONFIG_HOME):
  * `claude -p "What is N plus 1?" --model multi` ran 1753 s: the [swarm-tools]
    fan-out logged seven back-to-back runs, each "N/5 answered, 0 used a tool"
    after the full 360 s. The members had answered the number in TEXT (the
    right answer) and the race only ever ended on a tool call, so each run
    outlived the client's ~300 s stream header timeout and the client retried.
  * the pipeline fast path meant for exactly that ask never fired: Claude Code
    sends its environment block as a role "system" entry INSIDE `messages`
    (after the question), the Anthropic translator turned it into the LAST
    USER MESSAGE, and the fast path judged "# Environment ..." instead of the
    question.
  * opencode tool sessions opened turn after turn on an nvidia pair that had
    just cost ~100-120 s of _HopBudgetExceeded: with the whole fleet 429'd or
    stalling, the recent-failure filter failed open and the session pin
    re-chose the stalled pair.

Pinned here with fakes (no network, no real config).
"""
import json
import time

import pytest
import requests

import app as A


TOOLS = [{"type": "function", "function": {"name": "read", "parameters": {}}}]
BODY = {"model": "multi", "tools": TOOLS,
        "messages": [{"role": "user", "content": "What is 4417 plus 1?"}]}


class _Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload
        self.headers = {}
        self.text = ""

    def json(self):
        return self._payload

    def close(self):
        pass


def _tool_call():
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "read", "arguments": "{}"}}]}}]}


def _text(text="4418"):
    return {"choices": [{"finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}]}


# --------------------------------------------------------------------------- #
# A. The fan-out settles on text answers
# --------------------------------------------------------------------------- #

def _fanout(monkeypatch, picks):
    monkeypatch.setattr(A, "_route_by_difficulty",
                        lambda *a, **k: (picks[0][0], picks[0][1], "hard"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: list(picks))
    monkeypatch.setattr(A, "_swarm_rank", lambda cands, difficulty=None: list(picks))
    monkeypatch.setattr(A, "_record_outcome", lambda *a, **k: None)
    monkeypatch.setattr(A, "_record_chat_usage", lambda *a, **k: None)
    monkeypatch.setattr(A, "_note_nonanswer", lambda *a, **k: None)
    monkeypatch.setattr(A, "_normalize_model_identity", lambda m: m)
    monkeypatch.setattr(A, "_est_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: 100.0)
    # Long enough that only the new rules can end the wait early.
    monkeypatch.setattr(A, "_SWARM_TOOL_HOP_DEADLINE", 20)
    monkeypatch.setattr(A, "_SWARM_TOOL_STREAM_DEADLINE", 20)


def _members(timings):
    """timings: {provider: (seconds, payload or None)} -- a None payload is a
    member still running at its deadline."""
    def go(pid, payload, deadline):
        delay, out = timings[pid]
        time.sleep(delay)
        if out is None:
            return None, None
        return _Resp(out), None
    return go


def test_a_text_answer_starts_the_grace_like_a_tool_call(monkeypatch):
    """The Claude Code multi case: the answer is the number, in text. It used
    to wait the whole deadline for a tool call that was never coming."""
    _fanout(monkeypatch, [("fast", "m1"), ("slow", "m2"), ("never", "m3")])
    monkeypatch.setattr(A, "_SWARM_STRAGGLER_GRACE", 1)
    monkeypatch.setattr(A, "_SWARM_TEXT_SETTLE", 10)
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _members({
        "fast": (0.0, _text()), "slow": (8.0, None), "never": (15.0, None)}))
    t0 = time.monotonic()
    data, _h = A._swarm_tool_result(dict(BODY))
    took = time.monotonic() - t0
    assert took < 3.0, "waited %.1fs after a valid text answer" % took
    assert data["choices"][0]["message"]["content"] == "4418"


def test_a_tool_call_inside_the_grace_still_beats_an_earlier_text(monkeypatch):
    _fanout(monkeypatch, [("fast", "m1"), ("slow", "m2"), ("never", "m3")])
    monkeypatch.setattr(A, "_SWARM_STRAGGLER_GRACE", 2)
    monkeypatch.setattr(A, "_SWARM_TEXT_SETTLE", 10)
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _members({
        "fast": (0.0, _text("It is probably in big.txt somewhere.")),
        "slow": (0.4, _tool_call()), "never": (15.0, None)}))
    data, _h = A._swarm_tool_result(dict(BODY))
    assert data["choices"][0]["message"].get("tool_calls"), "acted must beat answered"


def test_mostly_text_settles_within_the_trivial_budget(monkeypatch):
    """Three of five members answered in text, none called a tool: the turn is
    a question. The stragglers get the trivial budget from the fan-out's
    start, not the 150 s tool grace."""
    picks = [("a", "m1"), ("b", "m2"), ("c", "m3"), ("d", "m4"), ("e", "m5")]
    _fanout(monkeypatch, picks)
    monkeypatch.setattr(A, "_SWARM_STRAGGLER_GRACE", 15)
    monkeypatch.setattr(A, "_SWARM_TEXT_SETTLE", 1)
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _members({
        "a": (0.0, _text()), "b": (0.1, _text()), "c": (0.2, _text()),
        "d": (12.0, _tool_call()), "e": (12.0, None)}))
    t0 = time.monotonic()
    data, _h = A._swarm_tool_result(dict(BODY))
    took = time.monotonic() - t0
    assert took < 3.0, "a mostly-text fan-out waited %.1fs" % took
    assert data["choices"][0]["message"]["content"] == "4418"


def test_failed_members_do_not_count_against_the_text_majority(monkeypatch):
    """Two of five failed outright; two answered in text: that is most of the
    members still in the race."""
    picks = [("a", "m1"), ("b", "m2"), ("x", "m3"), ("y", "m4"), ("e", "m5")]
    _fanout(monkeypatch, picks)
    monkeypatch.setattr(A, "_SWARM_STRAGGLER_GRACE", 15)
    monkeypatch.setattr(A, "_SWARM_TEXT_SETTLE", 1)

    def go(pid, payload, deadline):
        if pid in ("x", "y"):
            return None, requests.RequestException("429")
        if pid == "e":
            time.sleep(12)
            return None, None
        return _Resp(_text()), None
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", go)
    t0 = time.monotonic()
    A._swarm_tool_result(dict(BODY))
    assert time.monotonic() - t0 < 3.0


def test_a_minority_text_answer_keeps_the_tool_grace(monkeypatch):
    """One text answer of five is not "mostly text": the others get the full
    grace, and a tool call inside it wins."""
    picks = [("a", "m1"), ("b", "m2"), ("c", "m3"), ("d", "m4"), ("e", "m5")]
    _fanout(monkeypatch, picks)
    monkeypatch.setattr(A, "_SWARM_STRAGGLER_GRACE", 3)
    monkeypatch.setattr(A, "_SWARM_TEXT_SETTLE", 0.2)
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _members({
        "a": (0.0, _text()), "b": (1.0, _tool_call()), "c": (12.0, None),
        "d": (12.0, None), "e": (12.0, None)}))
    data, _h = A._swarm_tool_result(dict(BODY))
    assert data["choices"][0]["message"].get("tool_calls")


def test_an_announcement_does_not_end_the_race(monkeypatch):
    """Only a VALID final answer counts: "Let me check the file first." is an
    announcement (_run drops it), so the tool-caller behind it still wins."""
    _fanout(monkeypatch, [("fast", "m1"), ("slow", "m2"), ("never", "m3")])
    monkeypatch.setattr(A, "_SWARM_STRAGGLER_GRACE", 0.5)
    monkeypatch.setattr(A, "_SWARM_TEXT_SETTLE", 0.1)
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _members({
        "fast": (0.0, _text("Let me check the file first and verify it.")),
        "slow": (1.5, _tool_call()), "never": (15.0, None)}))
    data, _h = A._swarm_tool_result(dict(BODY))
    assert data["choices"][0]["message"].get("tool_calls")


def test_a_streamed_fan_out_ends_under_the_clients_header_timeout(monkeypatch):
    """A stream's fan-out holds the client's headers; past ~300 s the client
    gives up and RETRIES the whole fan-out. Bounded by
    _SWARM_TOOL_STREAM_DEADLINE; a buffered one keeps the long deadline."""
    picks = [("a", "m1"), ("b", "m2")]
    _fanout(monkeypatch, picks)
    monkeypatch.setattr(A, "_SWARM_TOOL_HOP_DEADLINE", 3)
    monkeypatch.setattr(A, "_SWARM_TOOL_STREAM_DEADLINE", 0.5)
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _members({
        "a": (8.0, None), "b": (8.0, None)}))
    t0 = time.monotonic()
    with A.app.test_request_context():
        assert A._swarm_tool_result(dict(BODY, stream=True)) is None
        # ...and the fallback shares the request clock started at the fan-out
        assert getattr(A.g, "hub_deadline_started", None) is not None
    assert time.monotonic() - t0 < 2.0
    t0 = time.monotonic()
    assert A._swarm_tool_result(dict(BODY, stream=False)) is None
    assert time.monotonic() - t0 >= 2.5


def test_the_stream_bound_stays_under_the_client_limit():
    assert A._SWARM_TOOL_STREAM_DEADLINE < A._STREAM_HEADER_BUDGET
    assert A._SWARM_TOOL_STREAM_DEADLINE < A.LONG_DEADLINE_STREAM_MAX
    assert A._SWARM_TEXT_SETTLE == A._TRIVIAL_HOP_BUDGET


# --------------------------------------------------------------------------- #
# A. Claude Code's in-messages system block
# --------------------------------------------------------------------------- #

ENV = ("# Environment\nYou have been invoked in the following environment: \n"
       " - Primary working directory: C:\\work\\proj\n - Is a git repository: false\n"
       " - Platform: win32\n\nYou are powered by the model multi.")
CLAUDE_BODY = {
    "model": "multi", "max_tokens": 32000, "stream": False,
    "system": [{"type": "text", "text": "You are Claude Code."}],
    "tools": [{"name": "Read", "description": "read a file",
               "input_schema": {"type": "object", "properties": {}}}],
    "messages": [
        {"role": "user", "content": [
            {"type": "text", "text": "<system-reminder>\nCLAUDE.md says hi\n</system-reminder>"},
            {"type": "text", "text": "What is 1234 plus 1? Reply with only the number."}]},
        {"role": "system", "content": [{"type": "text", "text": ENV}]},
    ],
}


def test_an_in_messages_system_block_is_system_context():
    out = A._anthropic_to_openai_messages(CLAUDE_BODY)
    assert [m["role"] for m in out] == ["system", "user"], out
    assert out[0]["content"].startswith("You are Claude Code.")
    assert "Primary working directory: C:\\work\\proj" in out[0]["content"]
    assert out[-1]["content"].endswith("Reply with only the number.")


def test_the_question_not_the_environment_is_the_latest_instruction():
    out = A._anthropic_to_openai_messages(CLAUDE_BODY)
    assert A._trivial_ask_text(out).strip() == \
        "What is 1234 plus 1? Reply with only the number."
    assert A._swarm_fast_path({"tools": TOOLS}, out) is True


def test_a_body_without_in_messages_system_is_unchanged():
    body = {"system": "sys", "messages": [{"role": "user", "content": "hi"},
                                          {"role": "assistant", "content": "yo"},
                                          {"role": "user", "content": "again"}]}
    assert A._anthropic_to_openai_messages(body) == [
        {"role": "system", "content": "sys"}, {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "yo"}, {"role": "user", "content": "again"}]


BIG_SYSTEM = "You are Claude Code. " + ("Follow the tool rules carefully. " * 2400)


def test_a_big_cli_system_prompt_does_not_block_an_opening_trivial_ask():
    """MEASURED live on the merged fix: with the hub's own Claude Code settings
    the opening turn has NO in-messages system block, but it estimates 13.5K
    tokens (system ~6.8K + CLAUDE.md reminders) -- over the 12K "small
    conversation" gate, so `multi` still fanned "What is N plus 1?" out. An
    opening turn has no conversation to be big."""
    msgs = [{"role": "system", "content": BIG_SYSTEM},
            {"role": "user", "content": "What is 5555 plus 1? Reply with only the number."}]
    assert A._est_tokens(msgs) >= A.STREAM_BIG_REQUEST_TOKENS
    assert A._swarm_fast_path({"tools": TOOLS}, msgs) is True


def test_a_big_conversation_still_keeps_the_pipeline_for_a_short_follow_up():
    msgs = [{"role": "system", "content": BIG_SYSTEM},
            {"role": "user", "content": "Refactor the parser."},
            {"role": "assistant", "content": "Done: parser split into two modules."},
            {"role": "user", "content": "What is 5555 plus 1?"}]
    assert A._est_tokens(msgs) >= A.STREAM_BIG_REQUEST_TOKENS
    assert A._swarm_fast_path({"tools": TOOLS}, msgs) is False


def test_claude_code_multi_trivial_ask_takes_the_fast_path(monkeypatch):
    """End to end through /v1/messages: no fan-out, one strong model."""
    for name in ("_record_chat_usage", "_record_outcome", "_save_perf_stats",
                 "_act_pick", "_note_ttft", "_record_stream_outcome",
                 "_note_provider_timeout", "_throttle_failed_hop"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)
    monkeypatch.setattr(A, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(A, "_model_block_reason", lambda pid, m: None)
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    monkeypatch.setattr(A.config, "get_flag", lambda k, d=None: d)

    def no_fanout(*a, **k):
        raise AssertionError("the fan-out must not run for a trivial ask")
    monkeypatch.setattr(A, "_swarm_as_anthropic", no_fanout)
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("groq", "q", "medium"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: [("groq", "q")])
    monkeypatch.setattr(A, "_dispatch_chat",
                        lambda pid, payload, stream: _Resp(_text("1235")))
    r = A.app.test_client().post("/v1/messages", json=CLAUDE_BODY)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert "fast-path" in r.headers.get("X-Free-LLM-Hub-Pipeline", "")
    assert r.get_json()["content"][0]["text"] == "1235"


# --------------------------------------------------------------------------- #
# B. A stalled pair is not re-chosen by the next turn
# --------------------------------------------------------------------------- #

@pytest.fixture
def fleet(monkeypatch):
    world = {"nvidia": ["dsv4-flash"], "groq": ["qwen-q"], "cerebras": ["glm-c"]}
    scores = {"dsv4-flash": 140.0, "qwen-q": 120.0, "glm-c": 119.0}
    monkeypatch.setattr(A, "_available_providers", lambda *a, **k: list(world))
    monkeypatch.setattr(A, "_prefetch_auto_models", lambda pids: dict(world))
    monkeypatch.setattr(A, "_auto_models", lambda pid: list(world[pid]))
    monkeypatch.setattr(A, "_provider_capable", lambda pid, est: True)
    monkeypatch.setattr(A, "_supports_tools", lambda pid, m: True)
    monkeypatch.setattr(A, "_is_fast", lambda pid, m: True)
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: scores.get(m, 100.0))
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
    monkeypatch.setattr(A, "_is_low_quality", lambda m: False)
    monkeypatch.setattr(A, "_is_tool_proven", lambda m: True)
    monkeypatch.setattr(A, "_weighted_pick", lambda pool, *a, **k: max(pool))
    monkeypatch.setattr(A, "_build_sid", lambda: None)
    return world


SESSION = [{"role": "system", "content": "<env>\nWorking directory: /w/stall-test\n</env>"},
           {"role": "user", "content": "Read big.txt and reply with its last line, exactly."}]


@pytest.fixture
def pinned_session(fleet):
    key = A._session_key(SESSION)
    A._session_pin_set(key, "nvidia", "dsv4-flash")
    yield key
    A._session_pin_drop(key)


def _route():
    with A.app.test_request_context():
        pid, model, _d = A._route_by_difficulty(SESSION, None, 16000, require_tools=True,
                                                force_difficulty="hard")
    return pid, model


def test_the_pin_holds_when_nothing_is_wrong(pinned_session):
    assert _route() == ("nvidia", "dsv4-flash")


def test_a_stalled_pinned_pair_is_not_re_chosen_while_a_429d_one_exists(pinned_session):
    """The degraded-fleet case: EVERY candidate failed recently, so the
    recent-failure filter fails open -- but the pair that stalled a whole hop
    budget must not open the next turn over one that merely 429'd."""
    A._note_recent_hop_failure("nvidia", "dsv4-flash", "deadline")
    A._note_recent_hop_failure("groq", "qwen-q", "429")
    A._note_recent_hop_failure("cerebras", "glm-c", "429")
    assert _route() != ("nvidia", "dsv4-flash")


def test_a_read_timeout_counts_as_a_stall(pinned_session):
    A._note_recent_hop_failure("nvidia", "dsv4-flash", "timeout")
    A._note_recent_hop_failure("groq", "qwen-q", "429")
    A._note_recent_hop_failure("cerebras", "glm-c", "429")
    assert _route() != ("nvidia", "dsv4-flash")


def test_when_everything_stalled_the_pin_still_serves(pinned_session):
    """Fail-open: nothing better exists, so the pin keeps its model."""
    for p, m in (("nvidia", "dsv4-flash"), ("groq", "qwen-q"), ("cerebras", "glm-c")):
        A._note_recent_hop_failure(p, m, "deadline")
    assert _route() == ("nvidia", "dsv4-flash")


def test_in_the_recent_failure_tail_a_stall_goes_behind_a_429(fleet):
    A._note_recent_hop_failure("nvidia", "dsv4-flash", "deadline")
    A._note_recent_hop_failure("groq", "qwen-q", "429")
    A._note_recent_hop_failure("cerebras", "glm-c", "429")
    with A.app.test_request_context():
        A._mark_turn_shape("hard", 16000)
        chain = A._build_chain("nvidia", "dsv4-flash", 16000, require_tools=True,
                               messages=SESSION)
    stalled = chain.index(("nvidia", "dsv4-flash"))
    assert stalled > chain.index(("groq", "qwen-q")), chain
    assert stalled > chain.index(("cerebras", "glm-c")), chain
    assert chain[0] == ("groq", "qwen-q"), chain


# --------------------------------------------------------------------------- #
# B. The per-provider walk caps apply to STREAMED tool turns
#    (opencode: /v1/chat/completions, Claude Code: /v1/messages,
#     codex: /v1/responses)
# --------------------------------------------------------------------------- #

class _Stream:
    status_code = 200
    headers = {}
    text = ""

    def __init__(self, text):
        chunk = {"choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                              "finish_reason": None}]}
        end = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        self._lines = [b"data: " + json.dumps(chunk).encode(), b"",
                       b"data: " + json.dumps(end).encode(), b"", b"data: [DONE]", b""]

    def iter_lines(self, decode_unicode=False):
        return iter(self._lines)

    def iter_content(self, chunk_size=None):
        return iter([ln + b"\n" for ln in self._lines])

    def close(self):
        pass


@pytest.fixture
def walk(monkeypatch):
    for name in ("_record_chat_usage", "_record_outcome", "_save_perf_stats",
                 "_act_pick", "_note_ttft", "_record_stream_outcome",
                 "_note_provider_timeout", "_throttle_failed_hop"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)
    monkeypatch.setattr(A, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(A, "_model_block_reason", lambda pid, m: None)
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    monkeypatch.setattr(A.config, "get_flag", lambda k, d=None: d)
    monkeypatch.setattr(A, "_swarm_fast_path", lambda *a, **k: False)
    monkeypatch.setattr(A, "_request_deadline_seconds", lambda: 6.0)
    monkeypatch.setattr(A, "_TOOL_PROVIDER_MIN_SECONDS", 1.0)
    monkeypatch.setattr(A, "_TOOL_PROVIDER_MIN_HOP", 0.5)
    monkeypatch.setattr(A, "_ADAPTIVE_HOP_FLOOR", 0.1)
    hops = [("nvidia", "a"), ("nvidia", "b"), ("groq", "q")]
    monkeypatch.setattr(A, "_route_by_difficulty", lambda *a, **k: ("nvidia", "a", "hard"))
    monkeypatch.setattr(A, "_build_chain", lambda *a, **k: list(hops))
    calls = []

    def dispatch(pid, payload, stream):
        calls.append((pid, payload["model"]))
        assert stream, "this test drives the STREAMED path"
        if pid == "nvidia":
            time.sleep(8)                      # never sends headers in time
        return _Stream("The last line of big.txt is THE-LAST-LINE-424242.")
    monkeypatch.setattr(A, "_dispatch_chat", dispatch)
    return calls


def _check_walk(calls, took):
    assert calls == [("nvidia", "a"), ("groq", "q")], calls
    assert took < 4.5, took              # nvidia's share of 6 s, not all of it
    assert A._recent_hop_stall("nvidia", "a"), "the stall must reach the next turn"


def test_walk_caps_apply_to_a_streamed_messages_tool_turn(walk):
    t0 = time.monotonic()
    r = A.app.test_client().post("/v1/messages", json={
        "model": "auto", "stream": True, "max_tokens": 500,
        "system": "<env>\nWorking directory: /w\n</env>",
        "tools": [{"name": "Read", "input_schema": {"type": "object", "properties": {}}}],
        "messages": [{"role": "user", "content": "Read big.txt and reply with its last line."}]})
    body = r.get_data(as_text=True)
    _check_walk(walk, time.monotonic() - t0)
    assert r.status_code == 200 and "THE-LAST-LINE-424242" in body, body[:300]


def test_walk_caps_apply_to_a_streamed_chat_tool_turn(walk):
    t0 = time.monotonic()
    r = A.app.test_client().post("/v1/chat/completions", json={
        "model": "auto", "stream": True, "tools": TOOLS,
        "messages": [{"role": "system", "content": "<env>\nWorking directory: /w\n</env>"},
                     {"role": "user", "content": "Read big.txt and reply with its last line."}]})
    body = r.get_data(as_text=True)
    _check_walk(walk, time.monotonic() - t0)
    assert r.status_code == 200 and "THE-LAST-LINE-424242" in body, body[:300]


def test_walk_caps_apply_to_a_streamed_responses_tool_turn(walk):
    t0 = time.monotonic()
    r = A.app.test_client().post("/v1/responses", json={
        "model": "auto", "stream": True,
        "tools": [{"type": "function", "name": "read", "parameters": {}}],
        "input": [{"role": "user", "content": "Read big.txt and reply with its last line."}]})
    body = r.get_data(as_text=True)
    _check_walk(walk, time.monotonic() - t0)
    assert r.status_code == 200 and "THE-LAST-LINE-424242" in body, body[:300]
