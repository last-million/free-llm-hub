"""Live window steering: the owner never picks context-max by hand.

REQUESTED 2026-10-03: "the context-max switch should be done automatically by
the hub, in all CLIs that use it". Each CLI compacts against the window its
config was told at Connect. The safe figure (what 3 providers hold, ~262K)
stops a conversation at ~250K even when a 1M model is free; the reach figure
alone would let it grow past what is usable when the big provider is out. So:

  1. CLIs PROVEN (from their source) to compact from the usage the hub reports
     are declared the REACH window of their tier; every other CLI keeps SAFE;
  2. per request the LIVE window = the biggest window among the tier's models
     usable now (not dead / parked / out for >= _CTX_OVERFLOW_LONG_WAIT);
  3. when live < declared, reported prompt tokens = real * declared / live
     (never below real), on all three protocols, stream and non-stream -- and
     never on a CLI's own compaction request.

All fakes and temp homes, no network, never the owner's real files.
"""
import json
import os
import types

import pytest

import agentic_chat as AC
import app as A
import ctxwin
import test_declared_window_providers as TD
from test_context_window_management import (  # noqa: F401  (fixtures)
    _Resp, _answer, _sse_chunks, _sse_events, isolated_windows, quiet)
from test_declared_window_providers import _fresh_provider, home  # noqa: F401

_REAL_CONNECTED = A._steer_connected_unidentifiable     # before conftest stubs it
PINNED = "cerebras/zai-glm-4.7"
BIG = "x" * 140000                                       # ~35K tokens: past the gate


def _provider(reach, safe):
    """A declared-window provider: `reach` for the steered CLIs, `safe` else."""
    def fn(mid, cli=None):
        return reach if cli in A._REACH_CLIS else safe
    return fn


def _begin(model, ua=None, messages=None, est=1000, path="/v1/chat/completions",
           build=None):
    """A request context positioned where a /v1 handler calls _ctx_begin."""
    env = {"flh.build_session": build} if build else None
    ctx = A.app.test_request_context(
        path, headers={"User-Agent": ua} if ua else {}, environ_base=env)
    ctx.push()
    body = A._apply_category_effort({"model": model})
    A._ctx_begin(body, messages or [{"role": "user", "content": "go on"}], est)
    return ctx


# --------------------------------------------------------------------------- #
# 1. The formula
# --------------------------------------------------------------------------- #

def test_reported_tokens_scale_by_declared_over_live(monkeypatch):
    monkeypatch.setattr(A, "_ctx_steer_pair", lambda: (400000, 100000))
    assert A._steer_reported(30000) == 120000
    assert A._steer_reported(25000) == 100000                # at the gate
    assert A._steer_reported(24999) == 24999                 # a small turn reads true
    assert A._steer_reported(0) == 0
    monkeypatch.setattr(A, "_ctx_steer_pair", lambda: (262145, 262144))
    assert A._steer_reported(100001) == 100001               # rounding, never below real
    monkeypatch.setattr(A, "_ctx_steer_pair", lambda: None)
    assert A._steer_reported(30000) == 30000


def test_the_cli_compacts_at_the_same_fraction_of_the_live_window(monkeypatch):
    # A CLI told 1M that compacts at 90% (codex) must fire at 90% of a 262K live window.
    declared, live, frac = 1000000, 262144, 0.9
    monkeypatch.setattr(A, "_ctx_steer_pair", lambda: (declared, live))
    assert A._steer_reported(int(frac * live) + 1) >= frac * declared
    assert A._steer_reported(int(frac * live) - 200) < frac * declared


def test_steering_rides_on_top_of_the_hop_compaction_ratio(monkeypatch):
    AC.set_window_provider(_provider(400000, 262144))
    monkeypatch.setattr(A, "_live_window_for", lambda mid: 100000)
    ctx = _begin("auto", ua="opencode/1.4.2 ai-sdk/5")
    try:
        A._ctx_note_hop("p", "m", 90000, 30000)                # hop compacted x3
        assert A._reported_prompt_tokens(10000, 0, "p", "m") == 120000
        assert A._reported_prompt_tokens(None, 30000) == 120000
    finally:
        ctx.pop()


@pytest.mark.parametrize("live", [400000, 1000000, None])
def test_live_at_or_above_declared_or_unknown_leaves_usage_alone(monkeypatch, live):
    AC.set_window_provider(_provider(400000, 262144))
    monkeypatch.setattr(A, "_live_window_for", lambda mid: live)
    ctx = _begin("auto", ua="opencode/1.4.2")
    try:
        assert A._ctx_steer_pair() is None
        assert A._reported_prompt_tokens(150000, 0) == 150000
    finally:
        ctx.pop()


def test_a_cli_compaction_request_is_never_steered(monkeypatch):
    AC.set_window_provider(_provider(400000, 262144))
    monkeypatch.setattr(A, "_live_window_for", lambda mid: 100000)
    msgs = [{"role": "user", "content": "lots of work"},
            {"role": "user", "content": "Your task is to create a detailed summary of the "
                                        "conversation so far."}]
    assert ctxwin.is_compaction_request(msgs)
    ctx = _begin("auto", ua="claude-cli/2.1.288 (external, cli)", messages=msgs)
    try:
        assert A._ctx_steer_pair() is None
        assert A._reported_prompt_tokens(80000, 0) == 80000
    finally:
        ctx.pop()


def test_the_switch_turns_it_off(monkeypatch):
    AC.set_window_provider(_provider(400000, 262144))
    monkeypatch.setattr(A, "_live_window_for", lambda mid: 100000)
    real_flag = A.config.get_flag
    monkeypatch.setattr(A.config, "get_flag", lambda k, d=None: False
                        if k == "context_live_steering" else real_flag(k, d))
    ctx = _begin("auto", ua="opencode/1.4.2")
    try:
        assert A._reported_prompt_tokens(80000, 0) == 80000
    finally:
        ctx.pop()


def test_outside_a_v1_handler_nothing_is_steered(monkeypatch):
    monkeypatch.setattr(A, "_live_window_for", lambda mid: 100000)
    assert A._reported_prompt_tokens(80000, 0) == 80000         # no request at all
    with A.app.test_request_context("/v1/chat/completions",
                                    headers={"User-Agent": "opencode/1"}):
        assert A._reported_prompt_tokens(80000, 0) == 80000     # _ctx_begin never ran


# --------------------------------------------------------------------------- #
# 2. Reach (declared to steered CLIs) vs safe (everyone else)
# --------------------------------------------------------------------------- #

def test_steered_clis_are_declared_the_reach_window(monkeypatch):
    TD._fleet(monkeypatch, TD._gemini_flood(7))
    for mid in ("auto", "coding", "coding-max", "best"):
        assert A._declared_window_for(mid) == 262144, mid            # SAFE unchanged
        for cli in (None, "aider", "claude", "cursor-agent"):
            assert A._declared_window_for(mid, cli=cli) == 262144, (mid, cli)
        for cli in sorted(A._REACH_CLIS):
            # space-bunny on openrouter holds 1M (google is capped per request);
            # the owner caps the reach at _REACH_WINDOW_CAP (400K).
            assert A._declared_window_for(mid, cli=cli) == A._REACH_WINDOW_CAP, (mid, cli)
    AC.set_window_provider(A._declared_window_for)
    assert AC.declared_window("coding-max", cli="opencode") == A._REACH_WINDOW_CAP
    assert AC.declared_window("coding-max", cli="aider") == 262144
    assert AC.declared_window("coding-max") == 262144
    assert AC.declared_compact_limit("auto", cli="codex") == A._REACH_WINDOW_CAP * 96000 // 128000


def test_google_counts_with_its_per_request_input_cap(monkeypatch):
    # google's free tier spends its 250K input TPM in ONE bigger request: with no
    # other 1M provider, the reach is what the next provider holds.
    rows = [r for r in TD._gemini_flood(0) if r[0] != "openrouter"]
    TD._fleet(monkeypatch, rows)
    assert A._PROVIDER_REQUEST_TOKEN_CAP["google"] == 250000
    assert A._reach_window_for("coding-max") == 262144
    assert A._declared_window_for("coding-max", cli="opencode") == 262144


def test_relays_never_make_the_reach(monkeypatch):
    rows = [("kilocode", "qwen/qwen3.8-27b:free", 262144, 134.1),
            ("nvidia", "z-ai/glm-5.3", 250000, 138.0),
            ("dahl", "deepseek-ai/DeepSeek-V4-Flash", 163840, 134.0)]
    rows += [("g4f", "srv_%d:space-bunny" % i, 1000000, 134.0) for i in range(6)]
    TD._fleet(monkeypatch, rows)
    assert A._reach_window_for("auto") == 262144


def test_too_little_known_falls_back_to_the_safe_rules(monkeypatch):
    rows = [(p, "m-%s" % p, 262144, 134.0) for p in ("google", "nvidia", "dahl", "kilocode")]
    rows += [("openrouter", "u%d" % i, None, 134.0) for i in range(20)]
    TD._fleet(monkeypatch, rows)
    assert A._reach_window_for("auto") is None
    assert A._declared_window_for("auto", cli="opencode") is None
    AC.set_window_provider(A._declared_window_for)
    assert AC.declared_window("auto", cli="opencode") == AC._CODEX_CONTEXT_WINDOW


def test_a_pinned_id_keeps_its_own_window_for_every_cli(monkeypatch):
    TD._fleet(monkeypatch, TD._gemini_flood(2))
    assert A._declared_window_for("nvidia/z-ai/glm-5.3", cli="opencode") == 250000
    assert A._live_window_for("nvidia/z-ai/glm-5.3") is None        # pinned: never steered


def test_a_one_argument_provider_still_works():
    AC.set_window_provider(lambda mid: 300000)
    assert AC.declared_window("auto", cli="opencode") == 300000
    AC.set_window_provider(lambda mid, cli=None: 500000 if cli else 300000)
    assert AC.declared_window("auto", cli="pi") == 500000
    assert AC.declared_window("auto") == 300000


# --------------------------------------------------------------------------- #
# 3. The live window
# --------------------------------------------------------------------------- #

def _quota(monkeypatch, waits=None, parked=(), dead=()):
    waits = dict(waits or {})
    monkeypatch.setattr(A, "_ctx_hop_wait_seconds", lambda p, m: waits.get(p, 0.0))
    monkeypatch.setattr(A, "_is_provider_dead", lambda p: p in parked)
    monkeypatch.setattr(A, "_is_model_dead", lambda p, m: (p, m) in dead)


def test_live_is_the_biggest_window_usable_now(monkeypatch):
    TD._fleet(monkeypatch, TD._gemini_flood(7))
    _quota(monkeypatch)
    assert A._live_window_for("coding-max") == 1000000
    # a SHORT wait (a per-minute 429) does not take a model out
    _quota(monkeypatch, waits={"openrouter": A._CTX_OVERFLOW_LONG_WAIT - 1})
    assert A._live_window_for("coding-max") == 1000000


@pytest.mark.parametrize("out", ["day quota", "parked", "dead"])
def test_live_drops_when_the_big_provider_is_out_for_long(monkeypatch, out):
    TD._fleet(monkeypatch, TD._gemini_flood(7))
    if out == "day quota":
        _quota(monkeypatch, waits={"openrouter": A._CTX_OVERFLOW_LONG_WAIT})
    elif out == "parked":
        _quota(monkeypatch, parked={"openrouter"})
    else:
        _quota(monkeypatch, dead={("openrouter", "stealth/space-bunny-alpha")})
    # google capped at 250K, nvidia 250K, kilocode 262144; the g4f relays never count
    assert A._live_window_for("coding-max") == 262144
    _quota(monkeypatch, waits={"openrouter": 99999, "kilocode": 99999})
    assert A._live_window_for("coding-max") == 250000
    _quota(monkeypatch, parked={"openrouter", "kilocode", "nvidia", "google", "dahl"})
    assert A._live_window_for("coding-max") is None                  # nothing: no steering


def test_end_to_end_the_cli_is_steered_once_the_1m_provider_runs_dry(monkeypatch):
    TD._fleet(monkeypatch, TD._gemini_flood(7))
    AC.set_window_provider(A._declared_window_for)
    _quota(monkeypatch)
    ctx = _begin("coding-max", ua="opencode/1.4.2")
    try:
        assert A._ctx_steer_pair() is None                           # 1M usable: no steering
        assert A._reported_prompt_tokens(200000, 0) == 200000
    finally:
        ctx.pop()
    _quota(monkeypatch, waits={"openrouter": 6 * 3600})              # day quota spent
    ctx = _begin("coding-max", ua="opencode/1.4.2")
    try:
        cap = A._REACH_WINDOW_CAP                                     # declared reach (400K)
        assert A._ctx_steer_pair() == (cap, 262144)
        assert A._reported_prompt_tokens(200000, 0) == round(200000 * cap / 262144)
    finally:
        ctx.pop()


# --------------------------------------------------------------------------- #
# 4. Which CLIs, and how each one's declared figure is found
# --------------------------------------------------------------------------- #

def test_the_steered_set_is_the_verified_one():
    assert A._STEERED_CLIS == {"opencode", "codex", "claude", "qwen", "kimi", "pi",
                               "openclaw", "hermes"}
    assert "aider" not in A._STEERED_CLIS
    assert A._REACH_CLIS == A._STEERED_CLIS - {"claude"}
    src = open("app.py", encoding="utf-8").read()
    block = src[src.index("# LIVE WINDOW STEERING"):src.index("_STEERED_CLIS = frozenset")]
    for evidence in ("overflow.ts", "context_window.rs", "model_info.rs",
                     "chatCompressionService.ts", "main.mjs", "agent-session.ts",
                     "agent-session-compaction.ts", "context_compressor.py",
                     "aider/history.py"):
        assert evidence in block, evidence


def test_each_cli_compacts_against_its_own_declared_figure():
    AC.set_window_provider(_provider(1000000, 262144))
    for cli in ("opencode", "qwen", "kimi", "pi", "openclaw", "hermes"):
        assert A._cli_declared_window(cli, "coding-max") == 1000000, cli
        assert A._cli_declared_window(cli, "auto") == 1000000, cli
        assert A._cli_declared_window(cli, "gpt-4o") is None, cli    # an id never declared
    assert A._cli_declared_window("aider", "auto") is None           # not steered
    assert A._cli_declared_window("claude", "max") == A._CLAUDE_MODEL_WINDOW
    AC.set_window_provider(_provider(1000000, 128000))
    assert A._cli_declared_window("claude", "auto") == 128000        # min(safe, 200K)
    AC.set_window_provider(_provider(1000000, 262144))
    # codex: a hub catalog slug, codex's fallback metadata, the /agent override
    assert A._cli_declared_window("codex", "coding") == 1000000
    assert A._cli_declared_window("codex", A.MODE_ALL) == 1000000
    assert A._cli_declared_window("codex", "auto") == A._CODEX_FALLBACK_WINDOW == 272000
    assert A._cli_declared_window("codex", "auto", agent=True) == 272000
    assert A._cli_declared_window("codex", "coding", agent=True) == 1000000


@pytest.mark.parametrize("ua,cli", [
    ("opencode/1.4.2 ai-sdk/provider-utils/3", "opencode"),
    ("codex_cli_rs/0.154.0 (Windows 10.0.26300; x86_64) WindowsTerminal", "codex"),
    ("claude-cli/2.1.288 (external, cli)", "claude"),
    ("QwenCode/0.9.1 (win32; x64)", "qwen"),
    ("KimiCLI/0.39.1", "kimi"),
    ("aider/0.86.1", "aider"),
    ("OpenAI/JS 5.20.0", None),
    ("OpenAI/Python 1.109.1", None),
])
def test_the_user_agent_names_the_cli(ua, cli):
    with A.app.test_request_context("/v1/chat/completions", headers={"User-Agent": ua}):
        assert A._steer_cli_from_ua() == cli


def _steer_for(monkeypatch, ua, model="auto", connected=(), build=None, provider=None):
    AC.set_window_provider(provider or _provider(1000000, 262144))
    monkeypatch.setattr(A, "_live_window_for", lambda mid: 262144)
    monkeypatch.setattr(A, "_steer_connected_unidentifiable", lambda: tuple(connected))
    ctx = _begin(model, ua=ua, build=build)
    try:
        return A._ctx_steer_pair()
    finally:
        ctx.pop()


def test_aider_is_never_steered(monkeypatch):
    assert _steer_for(monkeypatch, "aider/0.86.1") is None


def test_identified_steered_clis_are(monkeypatch):
    assert _steer_for(monkeypatch, "opencode/1.4.2") == (1000000, 262144)
    assert _steer_for(monkeypatch, "KimiCLI/0.39.1") == (1000000, 262144)
    assert _steer_for(monkeypatch, "codex_cli_rs/0.154.0") == (272000, 262144)
    assert _steer_for(monkeypatch, "claude-cli/2.1.288") is None     # 200000 < live
    assert _steer_for(monkeypatch, "opencode/1.4.2", model=PINNED) is None


def test_an_unidentified_client_is_steered_only_when_the_figure_is_unambiguous(monkeypatch):
    sdk = "OpenAI/JS 5.20.0"
    assert _steer_for(monkeypatch, sdk) is None                      # nothing connected
    assert _steer_for(monkeypatch, sdk, connected=("pi", "hermes")) == (1000000, 262144)

    def split(mid, cli=None):
        return {"pi": 1000000, "hermes": 500000}.get(cli, 262144)
    assert _steer_for(monkeypatch, sdk, connected=("pi", "hermes"), provider=split) is None
    assert _steer_for(monkeypatch, sdk, connected=("hermes",), provider=split) == \
        (500000, 262144)


def test_an_agent_session_is_identified_by_its_registry_entry(monkeypatch):
    sid = "steer-test-session"
    with AC._REGISTRY_LOCK:
        AC._REGISTRY[sid] = types.SimpleNamespace(cli_id="codex")
    try:
        # whatever its User-Agent says, the /agent session runs codex -- whose
        # isolated config carries model_context_window, capped by the slug's max
        assert _steer_for(monkeypatch, "OpenAI/JS 5", build=sid) == (272000, 262144)
        assert A._agent_session_cli(sid) == "codex"
    finally:
        with AC._REGISTRY_LOCK:
            AC._REGISTRY.pop(sid, None)
    assert A._agent_session_cli("no-such-session") is None


def test_the_id_the_client_sent_and_the_id_it_routes_on(monkeypatch):
    with A.app.test_request_context("/v1/chat/completions"):
        body = A._apply_category_effort({"model": "coding-max"})
        assert body["model"] == "max"
        assert A.g._ctx_client_model == "coding-max"
        assert A._ctx_route_id(body) == "coding-max"
    with A.app.test_request_context("/v1/responses"):
        body = A._mode_and_effort({"model": "coding", "reasoning": {"effort": "medium"}})
        assert A.g._ctx_client_model == "coding"
        assert A._ctx_route_id(body) == "coding-best"
    with A.app.test_request_context("/v1/chat/completions"):
        A._note_client_model("auto")
        A._note_client_model("max")                                  # first one wins
        assert A.g._ctx_client_model == "auto"
        assert A._ctx_route_id({"model": PINNED}) == PINNED


def test_the_connected_check_reads_temp_configs(home, monkeypatch):
    # The registry's config_paths are resolved at import (the real home): point
    # them at the temp home's files.
    real_entry = A._get_cli_entry
    temp_paths = {"pi": [A._p_pi_models()], "hermes": [A._p_hermes()],
                  "openclaw": [A._p_openclaw()], "kimi": [A._p_kimi()]}
    monkeypatch.setattr(A, "_get_cli_entry", lambda cid: dict(
        real_entry(cid), config_paths=temp_paths.get(cid, [])))
    A._steer_connected_cache[:] = [0.0, ()]
    try:
        assert _REAL_CONNECTED() == ()
        for cid in ("pi", "hermes"):
            e = dict(A._get_cli_entry(cid))
            assert A._AUTOFIXERS[e["autofix"]](e, TD.KEY, TD.ROOT, TD.V1, "auto")["ok"]
        assert _REAL_CONNECTED() == ()                               # cached a minute
        A._steer_connected_cache[:] = [0.0, ()]
        assert _REAL_CONNECTED() == ("pi", "hermes")
    finally:
        A._steer_connected_cache[:] = [0.0, ()]


def test_conftest_keeps_tests_off_the_owners_cli_configs():
    conf = open(os.path.join("tests", "conftest.py"), encoding="utf-8").read()
    assert '"_steer_connected_unidentifiable"' in conf


# --------------------------------------------------------------------------- #
# 5. All three protocols, stream and non-stream
# --------------------------------------------------------------------------- #

@pytest.fixture
def steering(quiet, monkeypatch):
    """declared: opencode 400000, codex 272000, claude 200000; live 100000."""
    monkeypatch.setattr(A, "_live_window_for", lambda mid: 100000)
    figures = {"opencode": 400000, "codex": 272000, "claude": 200000}
    monkeypatch.setattr(A, "_cli_declared_window",
                        lambda cli, mid, agent=False: figures.get(cli))
    yield figures


UA = {"chat": "opencode/1.4.2", "responses": "codex_cli_rs/0.154.0",
      "messages": "claude-cli/2.1.288 (external, cli)"}
USAGE = {"prompt_tokens": 30000, "completion_tokens": 7, "total_tokens": 30007}


def _post(path, body, ua):
    return A.app.test_client().post(path, json=body, headers={"User-Agent": ua})


def test_chat_non_stream(steering, monkeypatch):
    monkeypatch.setattr(A, "_dispatch_chat", lambda pid, p, s: _Resp(200, _answer(usage=USAGE)))
    r = _post("/v1/chat/completions", {"model": PINNED, "messages": [
        {"role": "user", "content": BIG}]}, UA["chat"])
    u = r.get_json()["usage"]
    assert u["prompt_tokens"] == 120000 and u["total_tokens"] == 120007


@pytest.mark.parametrize("include_usage", [True, False])
def test_chat_stream(steering, monkeypatch, include_usage):
    monkeypatch.setattr(A, "_dispatch_chat",
                        lambda pid, p, s: _Resp(200, chunks=_sse_chunks(usage=USAGE)))
    body = {"model": PINNED, "stream": True, "messages": [{"role": "user", "content": BIG}]}
    if include_usage:
        body["stream_options"] = {"include_usage": True}
    r = _post("/v1/chat/completions", body, UA["chat"])
    usages = [o["usage"] for _n, o in _sse_events(r.get_data(as_text=True))
              if isinstance(o, dict) and o.get("usage")]
    assert usages and usages[-1]["prompt_tokens"] == 120000
    assert usages[-1]["total_tokens"] == 120007


def test_responses_non_stream(steering, monkeypatch):
    monkeypatch.setattr(A, "_dispatch_chat", lambda pid, p, s: _Resp(200, _answer(usage=USAGE)))
    r = _post("/v1/responses", {"model": PINNED, "input": BIG}, UA["responses"])
    assert r.get_json()["usage"]["input_tokens"] == round(30000 * 272000 / 100000)


def test_responses_stream(steering, monkeypatch):
    monkeypatch.setattr(A, "_dispatch_chat", lambda pid, p, s: _Resp(
        200, chunks=_sse_chunks(usage=USAGE, newline=False)))
    r = _post("/v1/responses", {"model": PINNED, "stream": True, "input": BIG}, UA["responses"])
    done = [o for n, o in _sse_events(r.get_data(as_text=True)) if n == "response.completed"]
    u = done[0]["response"]["usage"]
    assert u["input_tokens"] == round(30000 * 272000 / 100000)
    assert u["total_tokens"] == u["input_tokens"] + u["output_tokens"]


def test_messages_non_stream(steering, monkeypatch):
    monkeypatch.setattr(A, "_dispatch_chat", lambda pid, p, s: _Resp(200, _answer(usage=USAGE)))
    r = _post("/v1/messages", {"model": PINNED, "max_tokens": 1000, "messages": [
        {"role": "user", "content": BIG}]}, UA["messages"])
    assert r.get_json()["usage"]["input_tokens"] == 60000


def test_messages_stream(steering, monkeypatch):
    monkeypatch.setattr(A, "_dispatch_chat", lambda pid, p, s: _Resp(
        200, chunks=_sse_chunks(usage=USAGE, newline=False)))
    body = {"model": PINNED, "max_tokens": 1000, "stream": True,
            "messages": [{"role": "user", "content": BIG}]}
    r = _post("/v1/messages", body, UA["messages"])
    events = _sse_events(r.get_data(as_text=True))
    start = next(o for n, o in events if n == "message_start")
    delta = next(o for n, o in events if n == "message_delta")
    assert start["message"]["usage"]["input_tokens"] == 2 * A._estimate_input_tokens(body)
    assert delta["usage"]["input_tokens"] == 60000


# (the CLI's instruction ends its last message: ctxwin reads the tail)
_COMPACT = BIG + "\n\nYour task is to create a detailed summary of the conversation so far."


@pytest.mark.parametrize("proto", ["chat", "responses", "messages"])
@pytest.mark.parametrize("stream", [False, True])
def test_compaction_requests_report_the_real_size(steering, monkeypatch, proto, stream):
    if stream:
        # chat relays the upstream frames as they are; the translated
        # protocols read them line by line (as the tests above do)
        monkeypatch.setattr(A, "_dispatch_chat", lambda pid, p, s: _Resp(
            200, chunks=_sse_chunks(usage=USAGE, newline=proto == "chat")))
    else:
        monkeypatch.setattr(A, "_dispatch_chat",
                            lambda pid, p, s: _Resp(200, _answer(usage=USAGE)))
    if proto == "chat":
        body = {"model": PINNED, "messages": [{"role": "user", "content": _COMPACT}]}
        if stream:
            body.update(stream=True, stream_options={"include_usage": True})
        r = _post("/v1/chat/completions", body, UA[proto])
        if stream:
            got = [o["usage"] for _n, o in _sse_events(r.get_data(as_text=True))
                   if isinstance(o, dict) and o.get("usage")][-1]["prompt_tokens"]
        else:
            got = r.get_json()["usage"]["prompt_tokens"]
    elif proto == "responses":
        body = {"model": PINNED, "input": _COMPACT, "stream": stream}
        r = _post("/v1/responses", body, UA[proto])
        if stream:
            got = [o for n, o in _sse_events(r.get_data(as_text=True))
                   if n == "response.completed"][0]["response"]["usage"]["input_tokens"]
        else:
            got = r.get_json()["usage"]["input_tokens"]
    else:
        body = {"model": PINNED, "max_tokens": 1000, "stream": stream,
                "messages": [{"role": "user", "content": _COMPACT}]}
        r = _post("/v1/messages", body, UA[proto])
        if stream:
            got = next(o for n, o in _sse_events(r.get_data(as_text=True))
                       if n == "message_delta")["usage"]["input_tokens"]
        else:
            got = r.get_json()["usage"]["input_tokens"]
    assert got == 30000


# --------------------------------------------------------------------------- #
# 6. Connect and the resync write the reach figure only for steered CLIs
# --------------------------------------------------------------------------- #

def _fake_codex_catalog(home_dir, monkeypatch):
    """TD's fake hub catalog, written the way _refresh_codex_catalog does now:
    from codex's OWN declared figures."""
    cat = os.path.join(home_dir, ".codex", "model_catalog.json")
    calls = []

    def write():
        ents = [{"slug": mid, "display_name": "%s (Calvoun hub)" % mid,
                 "context_window": AC.declared_window(mid, cli="codex"),
                 "max_context_window": AC.declared_window(mid, cli="codex")}
                for mid in (A.MODE_ALL, "coding")]
        A._cli_write_text(cat, json.dumps({"models": ents}, indent=2) + "\n")

    def refresh(config_path=None):
        calls.append(config_path)
        write()
    write()
    monkeypatch.setattr(A, "_refresh_codex_catalog", refresh)
    return cat, calls


def _figures(win):
    return {k: set(v.values()) if isinstance(v, dict) else set(v) for k, v in win.items()}


def test_connect_and_resync_write_reach_only_for_steered_clis(home, monkeypatch):
    monkeypatch.setattr(TD, "_fake_codex_catalog", _fake_codex_catalog)
    AC.set_window_provider(_provider(1000000, 262144))
    paths, calls = TD._connect_everything(home, monkeypatch)
    got = _figures(TD._windows(paths))
    for cli in ("opencode", "opencode-isolated", "pi", "openclaw", "qwen", "hermes",
                "kimi", "codex"):
        assert got[cli] == {1000000}, (cli, got[cli])
    assert got["aider"] == {262144}
    assert got["claude"] == {"262144"}
    lim = TD._load(paths["opencode"])["provider"]["free-llm-hub"]["models"]["auto"]["limit"]
    assert lim == {"context": 1000000, "output": AC._HUB_MAX_OUTPUT}
    assert AC._opencode_limit_is_hubs(lim)                    # the shape rule still holds

    before = {k: TD._load(p) for k, p in paths.items()}
    AC.set_window_provider(_provider(800000, 250000))         # the fleet moved
    done = TD._REAL_RESYNC()
    assert {c for c, _p in done} == set(TD._CLIS)
    got = _figures(TD._windows(paths))
    for cli in ("opencode", "opencode-isolated", "pi", "openclaw", "qwen", "hermes",
                "kimi", "codex"):
        assert got[cli] == {800000}, (cli, got[cli])
    assert got["aider"] == {250000} and got["claude"] == {"250000"}
    for k, p in paths.items():                                # nothing else moved
        after = TD._load(p)
        if p.endswith(".toml"):
            assert after == before[k], k
        else:
            assert TD._strip(after) == TD._strip(before[k]), k
    assert len(calls) == 1
    assert TD._REAL_RESYNC() == []                            # unchanged: a no-op


def test_a_reach_only_change_triggers_the_periodic_resync(monkeypatch):
    runs = []
    monkeypatch.setattr(A, "_resync_declared_windows", lambda: runs.append(1) or [])
    monkeypatch.setattr(A, "_declared_resync_last", [None])
    AC.set_window_provider(_provider(1000000, 262144))
    A._resync_declared_windows_if_changed()
    A._resync_declared_windows_if_changed()
    assert len(runs) == 1
    AC.set_window_provider(_provider(800000, 262144))         # only the reach moved
    A._resync_declared_windows_if_changed()
    assert len(runs) == 2


def test_model_windows_api_shows_reach_and_live(monkeypatch):
    TD._fleet(monkeypatch, TD._gemini_flood(2))
    _quota(monkeypatch, waits={"openrouter": 99999})
    monkeypatch.setattr(A, "_enabled_keyed", lambda: ["nvidia"])
    monkeypatch.setattr(A, "_prefetch_auto_models", lambda pids: {"nvidia": ["z-ai/glm-5.3"]})
    with A.app.test_request_context("/api/model-windows"):
        data = A.api_model_windows().get_json()
    assert data["declared_reach"]["auto"] == A._REACH_WINDOW_CAP
    assert data["live"]["auto"] == 262144
    assert "aider" not in data["steered_clis"] and "opencode" in data["steered_clis"]


def test_the_reach_is_capped_at_400k_by_the_owner(monkeypatch):
    """OWNER DECISION 2026-10-03: a 1M reach let one conversation grow to ~4 MB
    per turn on a single daily-limited model; the CLI compacts at 400K instead.
    The live window still reports the 1M model, so nothing is steered while it
    is usable (live >= declared)."""
    assert A._REACH_WINDOW_CAP == 400000
    TD._fleet(monkeypatch, TD._gemini_flood(7))
    assert A._reach_window_for("coding-max") == 400000
    assert A._live_window_for("coding-max") == 1000000
