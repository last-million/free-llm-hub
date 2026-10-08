"""A conversation no model can hold is refused at the door (2026-10-08).

MEASURED (hub.log 2026-10-08 UTC), Build page session c39a30c1, codex,
`coding-max`, protocol `responses`, ~504,000 estimated tokens:

    [ctx] responses request of ~503553 tokens overflowed every hop (largest window tried 262144)
    RESPONSES-503 ... est=504077 hops=5-6 tried=[g4f relays, openrouter/..., groq/...]
    [spread] ... -> uncloseai/turboderp/Qwen3.8-27B-exl3 (pool 1)      # a 65K model

The size barely moved between messages, so Codex never compacted. Relays whose
window is only the "default" guess fail open (a guess never raises
_ContextOverflow) and each took a ~2 MB upload, so most messages ended as a
503 after 85-265 s instead of the native "context too long" reply.

What these tests pin (all hermetic: a fake upstream, no network):

  1. FRONT DOOR on all three protocols (chat, responses, messages; stream and
     non-stream): a non-compaction request above 1.15x the largest window any
     alive candidate could hold gets the native overflow reply at once, with
     ZERO routing, chain building, dispatch or upload. A default-window relay
     counts as holding at most _REACH_WINDOW_CAP (400K); a CLI's own compaction
     request is never refused; everything at or under the bound routes as before.
  2. A session pin on a model whose KNOWN window cannot hold the request is
     dropped and re-picked.
  3. Pipeline tiers (swarm / crew / multi) report the ORIGINAL request size in
     usage on every protocol (they reported 0 on the streamed Responses shape).
  4. The Build page says "too long for any available model" with the sizes,
     and the Activity row reads "context too long".
"""
import json
import os
import time

import pytest

import agentic_chat as AC
import app as A
import ctxwin
from test_agent_early_server import clock_turn  # noqa: F401  (fixture)
from test_context_window_management import (_LONG_ANSWER, _Resp, _answer, _sse_chunks,
                                             _sse_events)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

H_RELAY = ("uncloseai", "fake-relay-unknown-window")     # window only a guess
H_SMALL = ("uncloseai", "fake-small-65k")
H_MID = ("uncloseai", "fake-mid-262k")

COMPACT_PROMPT = ("You are performing a CONTEXT CHECKPOINT COMPACTION. Create a "
                  "handoff summary for another LLM that will resume the task.")

_CHUNK = ("def handler_%d(event):\n    return process(event, retries=3)  # keep\n" * 120)

BASH_TOOL = {"type": "function", "function": {
    "name": "bash", "description": "Run a shell command.",
    "parameters": {"type": "object", "properties": {"command": {"type": "string"}},
                   "required": ["command"]}}}


# --------------------------------------------------------------------------- #
# The world: a quiet hub, a fleet, and a fake upstream that counts every call
# --------------------------------------------------------------------------- #

class _World:
    def __init__(self):
        self.posts = []          # (model, estimated tokens sent) per upstream POST
        self.dispatches = []     # _dispatch_chat calls
        self.builds = 0          # _build_chain calls
        self.routes = 0          # _route_by_difficulty calls
        self.chain = []

    def calls(self):
        return len(self.posts) + len(self.dispatches) + self.builds + self.routes


@pytest.fixture
def world(monkeypatch):
    for name in ("_record_chat_usage", "_record_outcome", "_act_pick", "_note_ttft",
                 "_record_stream_outcome", "_note_provider_timeout",
                 "_throttle_failed_hop", "_bandit_credit", "_v1_observe"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)
    monkeypatch.setattr(A, "_check_provider_ready", lambda pid: None)
    monkeypatch.setattr(A, "_model_block_reason", lambda pid, m: None)
    monkeypatch.setattr(A, "_is_provider_dead", lambda pid: False)
    monkeypatch.setattr(A, "_ctx_hop_wait_seconds", lambda pid, m: 0.0)
    monkeypatch.setattr(A, "_supports_tools", lambda pid, m: True)
    monkeypatch.setattr(A, "_is_model_blocked_by_user", lambda pid, m: False)
    # A recap thread would POST to the fake upstream and break "zero calls".
    monkeypatch.setattr(A, "_summarize_dropped", lambda dropped: None)
    monkeypatch.setattr(A, "_cached_catalogs", lambda: {})
    saved = [(d, dict(d)) for d in (A._MODEL_MAX_INPUT, A._MODEL_LEARNED_AT,
                                    A._MODEL_CATALOG_CTX, A._MODEL_MAX_OUTPUT)]
    A._front_door_cache.clear()
    A._session_pins.clear()
    w = _World()

    def fake_post(*a, **kw):
        payload = kw.get("json") or {}
        w.posts.append((payload.get("model"),
                        A._est_tokens(payload.get("messages"), payload.get("tools"))))
        if kw.get("stream"):
            return _Resp(200, chunks=_sse_chunks(usage={"prompt_tokens": 5000,
                                                        "completion_tokens": 20,
                                                        "total_tokens": 5020}))
        return _Resp(200, payload=_answer(text=_LONG_ANSWER, usage={"prompt_tokens": 5000,
                                                 "completion_tokens": 20,
                                                 "total_tokens": 5020}))

    monkeypatch.setattr(A.requests, "post", fake_post)

    real_dispatch = A._dispatch_chat

    def counted_dispatch(pid, payload, stream):
        w.dispatches.append((pid, payload.get("model")))
        return real_dispatch(pid, payload, stream)

    monkeypatch.setattr(A, "_dispatch_chat", counted_dispatch)

    def set_chain(chain):
        w.chain = list(chain)

        def build(*a, **k):
            w.builds += 1
            return list(w.chain)

        def route(*a, **k):
            w.routes += 1
            return w.chain[0][0], w.chain[0][1], "hard"

        monkeypatch.setattr(A, "_build_chain", build)
        monkeypatch.setattr(A, "_route_by_difficulty", route)

    def fleet(rows, blocked=(), out=(), real_usable=False):
        """rows: [(pid, model, window or None)] = the alive models. `out`: pairs
        _usable_now reports out; real_usable=True keeps the real _usable_now."""
        cats = {}
        for pid, m, win in rows:
            cats.setdefault(pid, []).append(m)
            if win:
                A._MODEL_MAX_INPUT[(pid, m)] = win
                A._MODEL_LEARNED_AT[(pid, m)] = 1e12     # "learned", never expires
        monkeypatch.setattr(A, "_cached_catalogs",
                            lambda: {p: list(ms) for p, ms in cats.items()})
        monkeypatch.setattr(A, "_is_model_blocked_by_user",
                            lambda pid, m: (pid, m) in set(blocked))
        if not real_usable:
            monkeypatch.setattr(A, "_usable_now", lambda pid, m: (pid, m) not in set(out))
        A._front_door_cache.clear()

    w.set_chain = set_chain
    w.fleet = fleet
    try:
        yield w
    finally:
        for d, snap in saved:
            d.clear()
            d.update(snap)
        A._front_door_cache.clear()
        A._session_pins.clear()


# The measured fleet: known windows top out at 262144; a relay's is a guess.
MEASURED = [("nvidia", "fake-big-262k", 262144), H_SMALL + (65536,), H_RELAY + (None,)]


def _conv(tokens, last="Now run the tests and fix what fails."):
    """A coding-agent conversation of ~`tokens` estimated tokens, as chat
    messages (system, then user/assistant pairs, then the last user turn)."""
    n = int(tokens * 4 / len(_CHUNK)) + 1
    msgs = [{"role": "system", "content": "You are a coding agent."}]
    for i in range(n // 2 + 1):
        msgs.append({"role": "user", "content": "step %d: %s" % (i, _CHUNK)})
        msgs.append({"role": "assistant", "content": "done %d: %s" % (i, _CHUNK)})
    msgs.append({"role": "user", "content": last})
    return msgs


BIG = _conv(500000)
BIG_EST = A._est_tokens(BIG, [BASH_TOOL])
COMPACT = _conv(500000, last=COMPACT_PROMPT)


def _post_chat(msgs, stream=False, model="auto", path="/v1/chat/completions"):
    return A.app.test_client().post(path, json={
        "model": model, "stream": stream, "messages": msgs, "tools": [BASH_TOOL]})


def _post_responses(msgs, stream=False, model="auto", path="/v1/responses", headers=None):
    return A.app.test_client().post(path, json={
        "model": model, "stream": stream, "instructions": msgs[0]["content"],
        "input": [{"role": m["role"], "content": m["content"]} for m in msgs[1:]],
        "tools": [{"type": "function", "name": "bash", "description": "Run a shell command.",
                   "parameters": BASH_TOOL["function"]["parameters"]}]},
        headers=headers or {})


def _post_messages(msgs, stream=False, model="auto"):
    return A.app.test_client().post("/v1/messages", json={
        "model": model, "stream": stream, "max_tokens": 4096,
        "system": msgs[0]["content"],
        "messages": [{"role": m["role"], "content": m["content"]} for m in msgs[1:]],
        "tools": [{"name": "bash", "description": "Run a shell command.",
                   "input_schema": BASH_TOOL["function"]["parameters"]}]})


def _failed_event(r):
    ev = _sse_events(r.get_data(as_text=True))
    return [d for n, d in ev if n == "response.failed"]


def _assert_native_overflow(r, proto, stream):
    if proto == "responses" and stream:
        assert r.status_code == 200
        failed = _failed_event(r)
        assert failed and failed[0]["response"]["error"]["code"] == "context_length_exceeded"
        return failed[0]["response"]["error"]["message"]
    assert r.status_code == 400, r.get_data(as_text=True)[:300]
    body = r.get_json()
    if proto == "messages":
        assert body["error"]["message"].startswith("prompt is too long")
        return body["error"]["message"]
    assert body["error"]["code"] == "context_length_exceeded"
    return body["error"]["message"]


PROTOCOLS = [("chat", False), ("chat", True), ("responses", False),
             ("responses", True), ("messages", False), ("messages", True)]


def _send(proto, msgs, stream):
    return {"chat": _post_chat, "responses": _post_responses,
            "messages": _post_messages}[proto](msgs, stream=stream)


# --------------------------------------------------------------------------- #
# 1. The front door
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("proto,stream", PROTOCOLS)
def test_a_500k_conversation_gets_the_native_reply_with_zero_upstream_calls(
        world, proto, stream):
    world.fleet(MEASURED)
    world.set_chain([H_RELAY, H_SMALL])
    assert BIG_EST > 1.15 * 400000                       # the measured ~504K shape
    r = _send(proto, BIG, stream)
    msg = _assert_native_overflow(r, proto, stream)
    assert world.calls() == 0, (world.posts, world.dispatches, world.builds, world.routes)
    # the bound it states: 400000, the most a window that is only a guess can count for
    assert "400000" in msg, msg


def test_the_reply_states_the_request_and_the_bound(world):
    world.fleet(MEASURED)
    world.set_chain([H_RELAY])
    r = _post_chat(BIG)
    err = r.get_json()["error"]
    assert "400000 tokens" in err["message"]                         # the bound
    assert r.headers["X-Free-LLM-Hub-Last-Error"] == "context"
    assert world.calls() == 0


def test_without_a_default_window_candidate_the_bound_is_the_largest_known(world):
    world.fleet([("nvidia", "fake-big-262k", 262144), H_SMALL + (65536,)])
    bound, n = A._front_door_bound(tools=True)
    assert (bound, n) == (262144, 2)
    est = 400000                                        # > 1.15 x 262144 = 301,465
    with A.app.test_request_context("/v1/chat/completions"):
        r = A._front_door_overflow("openai", [{"role": "user", "content": "x"}],
                                   [BASH_TOOL], est)
    assert r is not None and r[1] == 400
    assert world.calls() == 0


def test_a_default_window_relay_counts_at_most_the_reach_cap(world):
    # Alive: a known 262144 model and a relay whose window is only a guess.
    world.fleet([("nvidia", "fake-big-262k", 262144), H_RELAY + (None,)])
    assert A._front_door_bound(tools=True) == (A._REACH_WINDOW_CAP, 2)
    assert A._REACH_WINDOW_CAP == 400000
    # 1.15 x 400000 = 460000: a 450K request may still fit the relay -> not refused
    with A.app.test_request_context("/v1/chat/completions"):
        assert A._front_door_overflow("openai", [{"role": "user", "content": "x"}],
                                      [BASH_TOOL], 450000) is None
        assert A._front_door_overflow("openai", [{"role": "user", "content": "x"}],
                                      [BASH_TOOL], 460001) is not None


def test_a_known_big_window_counts_at_its_window(world):
    world.fleet([("nvidia", "fake-big-262k", 262144), ("uncloseai", "fake-1m", 1000000)])
    assert A._front_door_bound(tools=True) == (1000000, 2)
    # a 504K request fits the 1M model: nothing is refused, the chain walks as before
    world.set_chain([("uncloseai", "fake-1m")])
    r = _post_responses(BIG, stream=False)
    assert r.status_code == 200
    assert world.builds == 1 and world.posts


def test_the_edge_exactly_1_15x_passes_and_one_more_token_is_refused(world, monkeypatch):
    monkeypatch.setattr(A, "_front_door_bound", lambda tools=False, images=False: (100000, 3))
    msgs = [{"role": "user", "content": "x"}]
    with A.app.test_request_context("/v1/chat/completions"):
        assert A._front_door_overflow("openai", msgs, [BASH_TOOL], 115000) is None
        assert A._front_door_overflow("openai", msgs, [BASH_TOOL], 115001) is not None


def test_a_cli_compaction_request_is_never_refused(world):
    world.fleet(MEASURED)
    world.set_chain([H_SMALL])
    assert ctxwin.is_compaction_request(COMPACT)
    for proto, stream in (("chat", False), ("responses", False), ("responses", True),
                          ("messages", False)):
        world.posts.clear()
        r = _send(proto, COMPACT, stream)
        assert r.status_code == 200, (proto, stream, r.get_data(as_text=True)[:200])
        if proto == "responses" and stream:
            assert not _failed_event(r)
        assert world.posts, "the compaction request was never served (%s)" % proto
        model, sent = world.posts[-1]
        # trimmed to the hop's 65K window (0.85 headroom), not forwarded whole
        assert sent <= int(65536 * 0.85) + 500, (proto, sent)


def test_a_100k_request_still_routes_as_before(world):
    world.fleet(MEASURED)
    world.set_chain([H_RELAY])
    msgs = _conv(100000)
    for proto, stream in PROTOCOLS:
        world.posts.clear()
        before = world.builds
        r = _send(proto, msgs, stream)
        assert r.status_code == 200, (proto, stream, r.get_data(as_text=True)[:200])
        assert world.builds == before + 1, proto
        assert len(world.posts) == 1, proto


def test_the_switches_turn_the_guard_off(world, monkeypatch):
    world.fleet(MEASURED)
    world.set_chain([H_RELAY])
    real = A.config.get_flag
    for flag in ("context_overflow_signal", "context_front_door"):
        monkeypatch.setattr(A.config, "get_flag",
                            lambda k, d=None, flag=flag: False if k == flag else real(k, d))
        with A.app.test_request_context("/v1/chat/completions"):
            assert A._front_door_overflow("openai", [{"role": "user", "content": "x"}],
                                          [BASH_TOOL], 900000) is None
    monkeypatch.setattr(A.config, "get_flag", real)
    with A.app.test_request_context("/v1/chat/completions"):
        assert A._front_door_overflow("openai", [{"role": "user", "content": "x"}],
                                      [BASH_TOOL], 900000) is not None


def test_nothing_alive_means_the_guard_stays_out_of_the_way(world):
    world.fleet([])
    assert A._front_door_bound(tools=True) == (0, 0)
    with A.app.test_request_context("/v1/chat/completions"):
        assert A._front_door_overflow("openai", [{"role": "user", "content": "x"}],
                                      [BASH_TOOL], 900000) is None


def test_blocked_out_and_toolless_models_do_not_raise_the_bound(world, monkeypatch):
    rows = [("nvidia", "fake-big-262k", 262144), ("openrouter", "fake-blocked-1m", 1000000),
            ("kilocode", "fake-out-1m", 1000000), ("kilocode", "fake-chat-only-2m", 2000000)]
    # one model is switched off by the user, one is out for good (day quota spent),
    # one cannot call tools: only the 262144 model is a real candidate.
    world.fleet(rows, blocked=[("openrouter", "fake-blocked-1m")],
                out=[("kilocode", "fake-out-1m")])
    monkeypatch.setattr(A, "_supports_tools", lambda pid, m: m != "fake-chat-only-2m")
    assert A._front_door_bound(tools=True) == (262144, 1)
    # ...and on a request without tools the chat-only model counts
    A._front_door_cache.clear()
    assert A._front_door_bound(tools=False) == (2000000, 2)


def test_a_dead_model_and_a_long_quota_wait_are_out_but_a_short_one_is_not(world, monkeypatch):
    rows = [("nvidia", "fake-big-262k", 262144), ("uncloseai", "fake-dead-1m", 1000000),
            ("kilocode", "fake-day-quota-1m", 1000000), ("dahl", "fake-minute-429-1m", 1000000)]
    world.fleet(rows, real_usable=True)               # the REAL _usable_now
    monkeypatch.setattr(A, "_is_model_dead", lambda pid, m: m == "fake-dead-1m")
    monkeypatch.setattr(A, "_ctx_hop_wait_seconds", lambda pid, m: {
        "fake-day-quota-1m": 4 * 3600.0, "fake-minute-429-1m": 20.0}.get(m, 0.0))
    # dead and day-quota-spent are out; a 20 s rate limit could serve after a short wait
    assert A._front_door_bound(tools=True) == (1000000, 2)
    A._front_door_cache.clear()
    monkeypatch.setattr(A, "_ctx_hop_wait_seconds", lambda pid, m: {
        "fake-day-quota-1m": 4 * 3600.0, "fake-minute-429-1m": 4 * 3600.0}.get(m, 0.0))
    assert A._front_door_bound(tools=True) == (262144, 1)


def test_a_provider_per_request_cap_lowers_what_a_model_counts_for(world):
    # google's free tier spends 250K input tokens per minute on ONE request.
    world.fleet([("google", "fake-gemini-1m", 1048576)])
    assert A._front_door_bound(tools=True) == (250000, 1)
    with A.app.test_request_context("/v1/chat/completions"):
        assert A._front_door_overflow("openai", [{"role": "user", "content": "x"}],
                                      [BASH_TOOL], 300000) is not None       # > 1.15 x 250K


def test_a_system_prompt_that_fills_the_window_is_a_capacity_case_not_a_compaction_one(world):
    world.fleet([("nvidia", "fake-big-262k", 262144)])
    huge_system = [{"role": "system", "content": "s" * (4 * 280000)},
                   {"role": "user", "content": "hi"}]
    with A.app.test_request_context("/v1/chat/completions"):
        # compacting cannot shrink a system prompt that is ~100% of the window
        assert A._front_door_overflow("openai", huge_system, [BASH_TOOL], 280000) is None


def test_other_surfaces_with_no_native_overflow_contract_are_not_guarded(world):
    """/v1/completions (and Gemini / Ollama) share the chat router but have no
    OpenAI-shaped context-length contract: they still walk the chain."""
    world.fleet(MEASURED)
    world.set_chain([H_RELAY])
    r = A.app.test_client().post("/v1/completions", json={
        "model": "auto", "prompt": _CHUNK * 380})
    body = r.get_data(as_text=True)
    assert "context_length_exceeded" not in body
    assert world.builds == 1 and world.posts


# --------------------------------------------------------------------------- #
# 2. The activity row and the Build page's ledger
# --------------------------------------------------------------------------- #

def test_the_activity_row_reads_context_too_long_with_the_sizes(world):
    world.fleet(MEASURED)
    world.set_chain([H_RELAY])
    _post_chat(BIG)
    row = A._activity[0]
    assert row["status"] == "context" and row["http"] == 400
    assert row["ctx_window"] == 400000 and row["ctx_tokens"] >= BIG_EST - 1


def test_a_streamed_responses_overflow_also_ends_context_not_error(world):
    world.fleet(MEASURED)
    world.set_chain([H_RELAY])
    r = _post_responses(BIG, stream=True)
    r.get_data()                                   # consume: the row finalizes with the body
    row = A._activity[0]
    assert row["status"] == "context" and row["http"] == 200, row


def test_the_hop_exhaustion_path_marks_the_row_too(world, monkeypatch):
    """The older path (every hop overflowed) goes through the same marker."""
    world.fleet([("nvidia", "fake-big-262k", 262144)])
    world.set_chain([("nvidia", "fake-big-262k")])

    def overflow(pid, payload, stream):
        A._ctx_note_overflow(262144, pid=pid, model=payload.get("model"))
        raise A._ContextOverflow("would lose most of its history")

    monkeypatch.setattr(A, "_dispatch_chat", overflow)
    # 280K is under 1.15 x 262144, so the front door lets it through
    r = _post_chat(_conv(280000))
    assert r.status_code == 400
    row = A._activity[0]
    assert row["status"] == "context" and row["ctx_window"] == 262144


def test_the_build_ledger_remembers_the_last_overflow_until_something_succeeds():
    t0 = time.time() - 1
    A._AGENT_CONTEXT.clear()
    A._AGENT_UPSTREAM.pop("ctx-s1", None)
    act = {"session": "ctx-s1", "ctx_tokens": 504077, "ctx_window": 262144, "finished": None}
    A._activity_done(act, "context", 200)
    assert A._agent_context_probe("ctx-s1", t0) == {"tokens": 504077, "window": 262144}
    assert A._agent_context_probe("ctx-s1", t0 + 3600) is None       # not recent
    assert A._agent_context_probe("other", t0) is None
    A._activity_done({"session": "ctx-s1", "finished": None}, "ok", 200)
    assert A._agent_context_probe("ctx-s1", t0) is None              # a success clears it


def test_the_probe_is_registered_with_the_agent_runner(monkeypatch):
    # (a test elsewhere may reload agentic_chat, so the registration is read from
    # the source and then exercised, not compared by identity)
    src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    assert "agentic_chat.set_context_probe(_agent_context_probe)" in src
    monkeypatch.setattr(AC, "_context_probe", A._agent_context_probe)
    t0 = time.time() - 1
    A._AGENT_CONTEXT.clear()
    A._activity_done({"session": "ctx-s2", "ctx_tokens": 504077, "ctx_window": 262144,
                      "finished": None}, "context", 200)
    sess = type("S", (), {"id": "ctx-s2"})()
    assert AC.context_too_long(sess, t0, "whatever the CLI said") ==         {"tokens": 504077, "window": 262144}


def test_an_agent_sessions_overflow_reaches_the_ledger_end_to_end(world):
    world.fleet(MEASURED)
    world.set_chain([H_RELAY])
    t0 = time.time() - 1
    A._AGENT_CONTEXT.clear()
    sid = "ctxe2e" + "0" * 10
    r = _post_responses(BIG, stream=True, path="/build/%s/v1/responses" % sid,
                        headers={"User-Agent": "codex_cli_rs/0.154.0"})
    r.get_data()
    rep = A._agent_context_probe(sid, t0)
    assert rep and rep["window"] == 400000 and rep["tokens"] >= BIG_EST - 1
    assert world.calls() == 0


# --------------------------------------------------------------------------- #
# 3. The Build page says it plainly
# --------------------------------------------------------------------------- #

def test_the_failure_text_names_the_sizes_and_the_way_out():
    text = AC.context_detail({"tokens": 504077, "window": 262144})
    assert text == ("This conversation is too long for any available model "
                    "(~504,077 tokens; the largest holds 262,144). Press Continue to "
                    "compact it or start a new conversation.")
    assert AC.context_detail({"tokens": 0, "window": 0}) == (
        "This conversation is too long for any available model. Press Continue to "
        "compact it or start a new conversation.")


def test_the_cli_error_text_is_recognised_without_the_ledger(monkeypatch):
    monkeypatch.setattr(AC, "_context_probe", None)
    sess = type("S", (), {"id": "x"})()
    for text in ("Codex ran out of room in the model's context window. Start a new "
                 "thread or clear earlier history and retry.",
                 "prompt is too long: 504077 tokens > 262144 maximum",
                 "This model's maximum context length is 262144 tokens."):
        assert AC.context_too_long(sess, 0.0, text) == {"tokens": 0, "window": 0}
    assert AC.context_too_long(sess, 0.0, "unexpected status 401 Unauthorized") is None
    assert AC.context_too_long(sess, 0.0, "") is None


def _codex_failed(message):
    return json.dumps({"type": "turn.failed", "error": {"message": message}}) + "\n"


def test_a_codex_turn_that_hit_the_context_wall_shows_the_plain_message(clock_turn, monkeypatch):
    seen = []

    def probe(sid, since):
        seen.append((sid, since))
        return {"tokens": 504077, "window": 262144}

    monkeypatch.setattr(AC, "_context_probe", probe)
    events, rec, sess = clock_turn(
        [([_codex_failed("Codex ran out of room in the model's context window.")], None, None)],
        cli_id="codex")
    last = events[-1]
    assert last["event"] == "error" and last["code"] == "context_too_long"
    assert last["status"] == 413
    assert last["detail"] == AC.context_detail({"tokens": 504077, "window": 262144})
    assert seen and seen[0][0] == sess.id and isinstance(seen[0][1], float)


def test_the_cli_text_alone_is_enough_when_the_ledger_has_nothing(clock_turn, monkeypatch):
    monkeypatch.setattr(AC, "_context_probe", lambda sid, since: None)
    events, _rec, _sess = clock_turn(
        [([_codex_failed("Codex ran out of room in the model's context window.")], None, None)],
        cli_id="codex")
    last = events[-1]
    assert last["code"] == "context_too_long"
    assert last["detail"].startswith("This conversation is too long for any available model.")


def test_an_unrelated_failure_is_untouched(clock_turn, monkeypatch):
    monkeypatch.setattr(AC, "_context_probe", lambda sid, since: None)
    events, _rec, _sess = clock_turn(
        [([_codex_failed("unexpected status 500: upstream exploded")], None, None)],
        cli_id="codex")
    last = events[-1]
    assert last["event"] == "error" and last.get("code") != "context_too_long"
    assert "upstream exploded" in last["detail"]


def test_the_dashboard_labels_the_row_and_offers_continue():
    html = open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8").read()
    assert "st === 'context' ? 'context too long'" in html
    assert "ev.code === 'context_too_long'" in html
    assert "ctxBtn.textContent = 'Continue'" in html
    # 'context' is neutral (the CLI compacts), never the red error pill
    assert "st === 'cancelled' ? 'cancel' : (st === 'context' ? 'cancel' : 'err')" in html


# --------------------------------------------------------------------------- #
# 4. A session pin never outlives the window
# --------------------------------------------------------------------------- #

def _drive(monkeypatch, rows, scores):
    by_pid = {}
    for pid, model, win in rows:
        by_pid.setdefault(pid, []).append(model)
        if win:
            A._MODEL_MAX_INPUT[(pid, model)] = win
            A._MODEL_LEARNED_AT[(pid, model)] = 1e12
    monkeypatch.setattr(A, "_available_providers", lambda: list(by_pid))
    monkeypatch.setattr(A, "_prefetch_auto_models", lambda pids: dict(by_pid))
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: scores.get((pid, m), 10.0))
    monkeypatch.setattr(A, "_provider_capable", lambda pid, est: True)
    monkeypatch.setattr(A, "_supports_tools", lambda pid, m: True)
    monkeypatch.setattr(A, "_is_model_dead", lambda pid, m: False)
    monkeypatch.setattr(A, "_context_ok", lambda pid, m, est: True)
    monkeypatch.setattr(A.quota, "is_model_throttled", lambda pid, m: False)
    monkeypatch.setattr(A.quota, "model_status",
                        lambda pid, m: {"exhausted": False, "used": 0, "limit": None,
                                        "limit_known": False, "remaining": None,
                                        "throttled": False})


PIN_ROWS = [H_SMALL + (65536,), H_MID + (262144,)]
PIN_SCORES = {H_SMALL: 140.0, H_MID: 134.0}


def test_a_pin_on_a_model_that_cannot_hold_the_request_is_dropped(world, monkeypatch):
    _drive(monkeypatch, PIN_ROWS, PIN_SCORES)
    msgs = [{"role": "user", "content": "Refactor module A and add tests. " + "y" * 2000}]
    key = A._session_key(msgs)
    A._session_pin_set(key, *H_SMALL)
    # 200K tokens: the 65K model cannot hold it (known window), the 262K one can
    pid, model, _d = A._route_by_difficulty(msgs, est=200000, require_tools=True)
    assert (pid, model) == H_MID
    assert A._session_pin_get(key) == H_MID                  # re-pinned on the one that fits


def test_a_pin_that_still_fits_is_kept(world, monkeypatch):
    _drive(monkeypatch, PIN_ROWS, PIN_SCORES)
    msgs = [{"role": "user", "content": "Refactor module B and add tests. " + "y" * 2000}]
    key = A._session_key(msgs)
    A._session_pin_set(key, *H_MID)
    assert A._route_by_difficulty(msgs, est=20000, require_tools=True)[:2] == H_MID
    A._session_pin_set(key, *H_SMALL)
    assert A._route_by_difficulty(msgs, est=20000, require_tools=True)[:2] == H_SMALL


def test_when_nothing_can_hold_it_the_pool_is_left_alone(world, monkeypatch):
    """Fail-open: the front door / overflow reply handle a request no one holds;
    routing never returns nothing because every known window is small."""
    _drive(monkeypatch, PIN_ROWS, PIN_SCORES)
    msgs = [{"role": "user", "content": "Refactor module C and add tests. " + "y" * 2000}]
    A._session_pin_set(A._session_key(msgs), *H_SMALL)
    pid, model, _d = A._route_by_difficulty(msgs, est=900000, require_tools=True)
    assert (pid, model) in (H_SMALL, H_MID)


def test_the_plain_chat_pin_follows_the_same_rule(world, monkeypatch):
    _drive(monkeypatch, PIN_ROWS, PIN_SCORES)
    msgs = [{"role": "user", "content": "Explain how the parser module is organised. " + "z" * 2000}]
    key = A._session_key(msgs)
    A._session_pin_set(key, *H_SMALL, agentic=False)
    pid, model, _d = A._route_by_difficulty(msgs, est=200000, require_tools=False,
                                            force_difficulty="hard")
    assert (pid, model) == H_MID


# --------------------------------------------------------------------------- #
# 5. Pipeline tiers report the ORIGINAL request size (they reported 0 / the
#    compacted count)
# --------------------------------------------------------------------------- #

def _pipeline_answer():
    return {"id": "chatcmpl-x", "object": "chat.completion", "model": "%s/%s" % H_SMALL,
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "call_1", "type": "function", "function": {
                    "name": "bash", "arguments": "{\"command\": \"ls\"}"}}]}}],
            "usage": {"prompt_tokens": 40000, "completion_tokens": 50, "total_tokens": 40050}}


@pytest.fixture
def pipeline(world, monkeypatch):
    """A swarm tier whose actor saw a payload compacted 6x (300K -> 50K)."""
    world.fleet([("nvidia", "fake-big-262k", 262144), H_SMALL + (65536,)])

    def fake_tool_result(body):
        # the early _ctx_begin must not arm the overflow signal for a pipeline
        # dispatch (an actor hop would raise _ContextOverflow instead of being
        # served compacted), on any of the three protocols
        assert not A._ctx_g("_ctx_signal")
        assert A._ctx_g("_ctx_orig_est")
        A._ctx_note_hop(H_SMALL[0], H_SMALL[1], 300000, 50000)     # what _upstream_chat does
        return _pipeline_answer(), {"X-Free-LLM-Hub-Roles": "actor_hops=1"}

    monkeypatch.setattr(A, "_swarm_tool_result", fake_tool_result)
    monkeypatch.setattr(A, "_swarm_fast_path", lambda *a, **k: False)
    AC.set_window_provider(None)
    return world


MID = _conv(280000)         # under 1.15 x 262144 and a 6x compaction ratio at 40K -> 240K


def _responses_usage(r, stream):
    if stream:
        done = [d for n, d in _sse_events(r.get_data(as_text=True)) if n == "response.completed"]
        assert done, r.get_data(as_text=True)[:400]
        return done[-1]["response"]["usage"]
    return r.get_json()["usage"]


@pytest.mark.parametrize("stream", [False, True])
def test_swarm_on_responses_reports_the_original_size(pipeline, stream):
    r = _post_responses(MID, stream=stream, model="swarm")
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    # 40000 upstream tokens x (300000 / 50000) = 240000 (never 0, never 40000)
    assert _responses_usage(r, stream)["input_tokens"] == 240000
    assert pipeline.posts == []


@pytest.mark.parametrize("stream", [False, True])
def test_swarm_on_chat_reports_the_original_size(pipeline, stream):
    r = _post_chat(MID, stream=stream, model="swarm")
    assert r.status_code == 200
    if stream:
        frames = [json.loads(l[6:]) for l in r.get_data(as_text=True).splitlines()
                  if l.startswith("data: {")]
        usage = [f["usage"] for f in frames if f.get("usage")]
        assert usage and usage[-1]["prompt_tokens"] == 240000
    else:
        assert r.get_json()["usage"]["prompt_tokens"] == 240000


@pytest.mark.parametrize("stream", [False, True])
def test_swarm_on_messages_reports_the_original_size(pipeline, stream):
    r = _post_messages(MID, stream=stream, model="swarm")
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    if stream:
        deltas = [d for n, d in _sse_events(r.get_data(as_text=True)) if n == "message_delta"]
        assert deltas and deltas[-1]["usage"]["input_tokens"] == 240000
    else:
        assert r.get_json()["usage"]["input_tokens"] == 240000


def test_a_steered_pipeline_reply_is_scaled_once_not_twice(pipeline, monkeypatch):
    """Live window steering (declared / live) must apply exactly once on the
    streamed shapes, whose translator steers, and on the JSON ones."""
    monkeypatch.setattr(A, "_ctx_steer_pair", lambda: (400000, 200000))
    for stream in (False, True):
        r = _post_responses(MID, stream=stream, model="swarm")
        assert _responses_usage(r, stream)["input_tokens"] == 480000, stream     # 240000 x 2
    r = _post_chat(MID, stream=False, model="swarm")
    assert r.get_json()["usage"]["prompt_tokens"] == 480000


def test_the_replayed_stream_carries_usage_only_when_the_answer_has_it():
    data = _pipeline_answer()
    lines = list(A._swarm_sse_lines(data))
    assert any(b'"usage"' in l for l in lines) and lines[-1] == b"data: [DONE]"
    data.pop("usage")
    assert not any(b'"usage"' in l for l in A._swarm_sse_lines(data))
    # the chunk list the finish_reason tests index into is unchanged
    assert A._swarm_stream_chunks(data).__class__.__name__ == "generator"
    assert list(A._swarm_stream_chunks(data))[-1]["choices"][0]["finish_reason"] == "tool_calls"


# --------------------------------------------------------------------------- #
# 6. The live shape: codex /agent session, `coding-max`, function_call history
# --------------------------------------------------------------------------- #

SID = "c39a30c1502349c0870c41872040f370"
CODEX_UA = {"User-Agent": "codex_cli_rs/0.154.0"}


def _codex_items(tokens, tail="Please continue."):
    """A codex-shaped /v1/responses history: AGENTS.md, environment context, the
    task, then function_call / function_call_output pairs, then a new message."""
    def msg(text):
        return {"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": text}]}
    items = [msg("# AGENTS.md instructions for /repo\n\n<INSTRUCTIONS>\nUse 4 spaces.\n"
                 "</INSTRUCTIONS>"),
             msg("<environment_context>\n  <cwd>/repo</cwd>\n</environment_context>"),
             msg("Build a todo app called Tasko")]
    for i in range(int(tokens * 4 / len(_CHUNK)) + 1):
        items.append({"type": "function_call", "name": "bash", "call_id": "c%d" % i,
                      "arguments": json.dumps({"command": "cat m%d.py" % i})})
        items.append({"type": "function_call_output", "call_id": "c%d" % i,
                      "output": "m%d.py\n%s" % (i, _CHUNK)})
    items.append(msg(tail))
    return items


def _codex_body(items, model="coding-max", stream=True):
    return {"model": model, "stream": stream, "instructions": "You are Codex.",
            "input": items,
            "tools": [{"type": "function", "name": "bash", "description": "Run a command.",
                       "parameters": BASH_TOOL["function"]["parameters"]}]}


@pytest.fixture
def codex_session(world, monkeypatch):
    """A registered codex /agent session on the measured fleet, declared
    windows from the REAL provider (400000 reach cap, 272000 for the compound
    id `coding-max`), the two 1M models out -> live window 262144."""
    import types
    rows = [("nvidia", "fake-glm-250k", 250000, 138.0),
            ("nvidia", "fake-kimi-250k", 250000, 138.1),
            ("kilocode", "fake-qwen-262k", 262144, 134.1),
            ("google", "fake-gemini-1m", 1048576, 134.1),
            ("openrouter", "fake-space-1m", 1000000, 137.7),
            H_SMALL + (65536, 134.0)]
    out = {("google", "fake-gemini-1m"), ("openrouter", "fake-space-1m")}
    monkeypatch.setattr(A, "_declared_fleet", lambda: list(rows))
    monkeypatch.setattr(A, "_mode_allows",
                        lambda mode, pid, m, session_overrides=None: mode == "coding")
    real_keys = A._mode_keys()
    monkeypatch.setattr(A, "_mode_keys", lambda: tuple(set(real_keys) | {"coding"}))
    monkeypatch.setattr(A, "_usable_now", lambda p, m: (p, m) not in out)
    A._declared_published.clear()
    A._declared_raise_seen.clear()
    saved = AC._window_provider, AC._window_provider_takes_cli
    AC.set_window_provider(A._stable_declared_window_for)
    with AC._REGISTRY_LOCK:
        AC._REGISTRY[SID] = types.SimpleNamespace(cli_id="codex")
    try:
        yield world
    finally:
        with AC._REGISTRY_LOCK:
            AC._REGISTRY.pop(SID, None)
        AC._window_provider, AC._window_provider_takes_cli = saved
        A._declared_published.clear()
        A._declared_raise_seen.clear()


def test_what_codex_is_told_for_the_compound_id(codex_session):
    # config.toml / the catalog carry the reach cap and 75% of it ...
    assert AC.declared_window("auto", cli="codex") == 400000
    assert AC.declared_compact_limit("auto", cli="codex") == 300000
    # ... but `coding-max` is not a catalog slug: codex runs it on its fallback
    # metadata (272000), which is the window the hub steers against.
    assert "coding-max" not in A._mode_keys()
    with A.app.test_request_context("/build/%s/v1/responses" % SID,
                                    environ_base={"flh.build_session": SID},
                                    headers=CODEX_UA):
        A._ctx_begin(A._apply_category_effort({"model": "coding-max"}),
                     [{"role": "user", "content": "x"}], 1000)
        assert A._cli_declared_window("codex", "coding-max", agent=True) == 272000
        assert A._ctx_steer_pair() == (272000, 262144)


def test_a_codex_shaped_500k_history_is_refused_at_the_door(codex_session):
    world = codex_session
    world.fleet([("nvidia", "fake-glm-250k", 250000), ("kilocode", "fake-qwen-262k", 262144),
                 H_SMALL + (65536,), H_RELAY + (None,)])
    world.set_chain([H_RELAY, H_SMALL])
    items = _codex_items(500000)
    assert A._est_tokens(A._responses_to_chat({"input": items, "instructions": "x"})) > 460000
    for stream in (True, False):
        r = A.app.test_client().post("/build/%s/v1/responses" % SID,
                                     json=_codex_body(items, stream=stream), headers=CODEX_UA)
        _assert_native_overflow(r, "responses", stream)
    assert world.calls() == 0


def test_a_served_codex_turn_reports_at_least_its_real_size(codex_session, monkeypatch):
    """Served turns do not under-report: upstream count x the hop's compaction
    ratio x declared/live (272000/262144), from 100K to 400K."""
    world = codex_session
    world.fleet([H_RELAY + (None,)])
    world.set_chain([H_RELAY])

    def post(*a, **kw):
        payload = kw.get("json") or {}
        sent = A._est_tokens(payload.get("messages"), payload.get("tools"))
        world.posts.append((payload.get("model"), sent))
        return _Resp(200, chunks=_sse_chunks(usage={"prompt_tokens": int(sent * 0.8),
                                                    "completion_tokens": 20,
                                                    "total_tokens": int(sent * 0.8) + 20}))

    monkeypatch.setattr(A.requests, "post", post)
    for tokens in (100000, 200000, 300000, 400000):
        world.posts.clear()
        items = _codex_items(tokens)
        est = A._est_tokens(A._responses_to_chat({"input": items, "instructions": "You are Codex.",
                                                  "tools": [BASH_TOOL]}),
                            A._responses_tools_to_chat(_codex_body(items)["tools"]))
        r = A.app.test_client().post("/build/%s/v1/responses" % SID,
                                     json=_codex_body(items), headers=CODEX_UA)
        assert r.status_code == 200
        done = [d for n, d in _sse_events(r.get_data(as_text=True)) if n == "response.completed"]
        assert done, (tokens, r.get_data(as_text=True)[:300])
        reported = done[-1]["response"]["usage"]["input_tokens"]
        real = 0.8 * est                             # what the fake upstream counted
        steered = real * 272000 / 262144.0
        assert real <= reported <= steered * 1.03, (tokens, est, reported, real)
        assert reported >= 0.75 * est
