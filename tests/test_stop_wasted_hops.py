"""Fewer wasted hops (2026-10-08).

MEASURED (turn-roles.jsonl, 24 h, 514 swarm-mode tool turns): 1235 upstream
calls (2.40 per turn), 413 of them FAILED first hops. The single biggest source
was uncloseai/turboderp/Qwen3.8-27B-exl3: HTTP 400 x173 ("System message must be
at the beginning." -- a strict Qwen chat template that rejects any system
message that is not the very first one), opened on 184 turns although its
reliability sat at 0.026. These tests pin the three fixes:

1. a spread/rotation step never moves a session onto a measured-to-fail pair
   (sharing the best model beats a leftover that fails most turns);
2. a hop that a provider rejects for system-message order is retried once with
   the system messages merged at index 0, and remembered;
3. a pair that fails the SAME non-429 way 3 times in 15 minutes with no success
   rests for tool turns (30 min, doubling to 6 h), kept as the last resort.

Plus the cost made visible: `wasted_calls` on every roles row.
"""
import threading
import time

import pytest

import app as A

QWEN = ("uncloseai", "turboderp/Qwen3.8-27B-exl3")
GLM = ("nvidia", "z-ai/glm-5.3")
BUNNY = ("openrouter", "stealth/space-bunny-alpha")
KIMI = ("nvidia", "moonshotai/kimi-k3")

WORLD = {"nvidia": ["z-ai/glm-5.3", "moonshotai/kimi-k3"],
         "openrouter": ["stealth/space-bunny-alpha"],
         "uncloseai": ["turboderp/Qwen3.8-27B-exl3"]}
SCORES = {"z-ai/glm-5.3": 138.0, "stealth/space-bunny-alpha": 137.7,
          "moonshotai/kimi-k3": 135.0, "turboderp/Qwen3.8-27B-exl3": 134.0}
FIX = [{"role": "user", "content": "Read src/parse.py and fix the bug in the parser."}]


@pytest.fixture(autouse=True)
def _clean_state():
    def _wipe():
        with A._session_pin_lock:
            A._session_pins.clear()
        A._empty_200.clear()
        getattr(A, "_pair_rest", {}).clear()
        A._recent_hop_fail.clear()
        A._tool_outcomes.clear()
        A._WORKER_MODEL.clear()
    _wipe()
    yield
    _wipe()


def _fleet(monkeypatch, failing=(QWEN,), penalty=8.5):
    """A fake fleet. `failing` pairs carry the learned reliability penalty and
    the 'measured to fail' band, exactly as reliability 0.026 does live."""
    failing = set(failing)
    monkeypatch.setattr(A, "_available_providers", lambda *a, **k: list(WORLD))
    monkeypatch.setattr(A, "_prefetch_auto_models", lambda pids: dict(WORLD))
    monkeypatch.setattr(A, "_auto_models", lambda pid: list(WORLD[pid]))
    monkeypatch.setattr(A, "_provider_capable", lambda pid, est: True)
    monkeypatch.setattr(A, "_supports_tools", lambda pid, m: True)
    monkeypatch.setattr(A, "_is_fast", lambda pid, m: True)
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: SCORES.get(m, 100.0))
    monkeypatch.setattr(A, "_active_mode", lambda: A.MODE_ALL)
    monkeypatch.setattr(A, "_is_model_dead", lambda pid, m: False)
    monkeypatch.setattr(A.quota, "is_model_throttled", lambda pid, m: False)
    monkeypatch.setattr(A.quota, "model_status", lambda pid, m: {"exhausted": False})
    monkeypatch.setattr(A, "_context_ok", lambda pid, m, est: True)
    monkeypatch.setattr(A, "_window_fits", lambda pid, m, est: True)
    monkeypatch.setattr(A, "_sub_available_providers", lambda: [])
    monkeypatch.setattr(A.config, "get_flag", lambda k, d=None: d)
    for name in ("_latency_penalty", "_answer_quality_penalty", "_sustain_penalty"):
        monkeypatch.setattr(A, name, lambda *a, **k: 0.0)
    monkeypatch.setattr(A, "_reliability_penalty",
                        lambda pid, m: penalty if (pid, m) in failing else 0.0)
    monkeypatch.setattr(A, "_chain_reliability_band",
                        lambda pid, m: 2 if (pid, m) in failing else 0)
    monkeypatch.setattr(A, "_below_declared_window", lambda pid, m: False)
    monkeypatch.setattr(A, "_is_low_quality", lambda m: False)
    monkeypatch.setattr(A, "_is_pair_benched", lambda pid, m: False)
    # OWNER DECISION 2026-10-10 board evidence is orthogonal to the spread/walk
    # MECHANICS tested here (the pools use explicit scores); neutralise it.
    monkeypatch.setattr(A, "_ev_agentic_bonus", lambda m: 0.0)
    monkeypatch.setattr(A, "_ev_has_agentic_evidence", lambda m: False)


def _route(sid="new-session", est=1000, trace=None):
    """_route_by_difficulty as session `sid`, recording the pool size each
    narrowing stage hands on (so a 'pool 1' is traceable to a stage)."""
    with A.app.test_request_context("/v1/chat/completions",
                                    environ_base={"flh.build_session": sid}):
        pid, model, _d = A._route_by_difficulty(FIX, None, est, require_tools=True)
    return pid, model


def _hold(sid, pair):
    A._session_pin_set(sid, pair[0], pair[1])


# --------------------------------------------------------------------------- #
# 1. spread / rotation never leave the band for a measured-to-fail leftover
# --------------------------------------------------------------------------- #

def test_repro_every_strong_model_held_the_best_held_one_is_shared(monkeypatch):
    """glm and space-bunny are held by two other sessions; Qwen (134, reliability
    0.026) is the only UNHELD one in the 10-point spread window. The old spread
    handed the new session to it ('pool 1, held elsewhere 2')."""
    _fleet(monkeypatch)
    _hold("s-a", GLM)
    _hold("s-b", BUNNY)
    _hold("s-c", KIMI)
    for _ in range(25):                       # the pick is a weighted draw
        A._session_pins.pop("new-session", None)
        assert _route() != QWEN


def test_spread_pool_never_returns_a_measured_to_fail_leftover(monkeypatch):
    _fleet(monkeypatch)
    _hold("s-a", GLM)
    _hold("s-b", BUNNY)
    pool = [(138.0, *GLM), (137.7, *BUNNY), (134.0, *QWEN)]
    left = A._spread_pool(pool, "new-session")
    assert QWEN not in {(c[1], c[2]) for c in left}
    assert {(c[1], c[2]) for c in left} == {GLM, BUNNY}      # shared, not pushed down


def test_spread_still_takes_a_healthy_close_competitor(monkeypatch):
    """The owner rule stays: different models for parallel helpers WHEN they are
    in the band."""
    _fleet(monkeypatch)
    _hold("s-a", GLM)
    pool = [(138.0, *GLM), (137.7, *BUNNY), (134.0, *QWEN)]
    left = A._spread_pool(pool, "new-session")
    assert [(c[1], c[2]) for c in left] == [BUNNY]


def test_spread_measures_the_band_on_what_the_pair_really_delivers(monkeypatch):
    """A healthy-looking 134 whose learned penalty is 5 is a 129: outside the
    6-point spread window of a 138, so it is not a spread target."""
    _fleet(monkeypatch, failing=(), penalty=0.0)
    monkeypatch.setattr(A, "_reliability_penalty",
                        lambda pid, m: 5.0 if (pid, m) == QWEN else 0.0)
    _hold("s-a", GLM)
    _hold("s-b", BUNNY)
    pool = [(138.0, *GLM), (137.7, *BUNNY), (134.0, *QWEN)]
    left = A._spread_pool(pool, "new-session")
    # not narrowed ONTO it: the sessions share the best (full pool = fail-open),
    # and the band step that follows (learned penalty applied) drops it
    assert {GLM, BUNNY} <= {(c[1], c[2]) for c in left}
    assert {(c[1], c[2]) for c in A._auto_top_band(left)} == {GLM, BUNNY}


def test_rotation_never_picks_a_measured_to_fail_pair(monkeypatch):
    _fleet(monkeypatch)
    monkeypatch.setattr(A.swarm_windows, "sibling_sessions",
                        lambda sid: ["w-1", "w-2"] if sid == "w-new" else [])
    A._note_worker_model("w-1", GLM[1], GLM[0])
    A._note_worker_model("w-2", BUNNY[1], BUNNY[0])
    pool = [(138.0, *GLM), (137.7, *BUNNY), (134.0, *QWEN)]
    got = A._rotate_within_run(list(pool), "w-new")
    assert QWEN not in {(c[1], c[2]) for c in got}


def test_rotation_still_takes_a_healthy_unused_model(monkeypatch):
    _fleet(monkeypatch)
    monkeypatch.setattr(A.swarm_windows, "sibling_sessions",
                        lambda sid: ["w-1"] if sid == "w-new" else [])
    A._note_worker_model("w-1", GLM[1], GLM[0])
    pool = [(138.0, *GLM), (137.7, *BUNNY), (134.0, *QWEN)]
    got = A._rotate_within_run(list(pool), "w-new")
    assert [(c[1], c[2]) for c in got] == [BUNNY]


def test_a_recently_stalled_top_model_does_not_hand_the_turn_to_the_failing_pair(monkeypatch):
    """The other way a pool of one arises: glm and space-bunny stalled in the
    last 10 minutes (they leave the primary pick), and the pair that fails FAST
    (HTTP 400 is not a 'recent failure' kind) is what is left."""
    _fleet(monkeypatch)
    A._note_recent_hop_failure(*GLM, "deadline")
    A._note_recent_hop_failure(*BUNNY, "deadline")
    for _ in range(3):
        A._note_pair_failure(*QWEN, "http4xx")          # its streak: rested
    assert A._pair_resting(*QWEN)
    assert _route() != QWEN


def test_the_pick_fails_open_when_the_failing_pair_is_all_there_is(monkeypatch):
    _fleet(monkeypatch)
    WORLD_BACKUP = dict(WORLD)
    monkeypatch.setattr(A, "_available_providers", lambda *a, **k: ["uncloseai"])
    monkeypatch.setattr(A, "_prefetch_auto_models",
                        lambda pids: {"uncloseai": WORLD_BACKUP["uncloseai"]})
    assert _route() == QWEN              # last resort: reachable, never deleted


# --------------------------------------------------------------------------- #
# 2. one system message, first -- learned from the provider's own 400
# --------------------------------------------------------------------------- #

from tests.test_unsupported_param_drop import _Resp, isolated  # noqa: E402,F401

SYS_400 = ('{"error": {"message": "System message must be at the beginning.", '
           '"type": "BadRequestError", "param": null, "code": 400}}')


def _msgs():
    return [{"role": "system", "content": "client rules"},
            {"role": "system", "content": "MODEL GUIDE ..."},
            {"role": "user", "content": "fix the bug"},
            {"role": "assistant", "content": "ok"},
            {"role": "system", "content": "compaction notice"},
            {"role": "user", "content": "go on"}]


def test_merge_puts_every_system_message_first_in_order():
    out = A._merge_system_messages(_msgs())
    assert [m["role"] for m in out] == ["system", "user", "assistant", "user"]
    assert out[0]["content"] == "client rules\n\nMODEL GUIDE ...\n\ncompaction notice"


def test_merge_leaves_a_fine_conversation_alone():
    ok = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    assert A._merge_system_messages(ok) is ok
    none = [{"role": "user", "content": "u"}]
    assert A._merge_system_messages(none) is none


def test_merge_never_flattens_non_text_content():
    odd = [{"role": "system", "content": [{"type": "image_url", "image_url": {"url": "x"}}]},
           {"role": "user", "content": "u"}, {"role": "system", "content": "late"}]
    assert A._merge_system_messages(odd) is odd


def test_the_400_is_retried_in_the_same_hop_and_remembered(isolated, monkeypatch):
    monkeypatch.setattr(A, "_SYSTEM_FIRST", {})
    sent = []

    def fake_post(*a, **kw):
        body = kw.get("json") or {}
        sent.append([m["role"] for m in body.get("messages", [])])
        n_sys = sum(1 for r in sent[-1] if r == "system")
        if n_sys > 1 or (n_sys == 1 and sent[-1][0] != "system"):
            return _Resp(400, SYS_400)
        return _Resp(200)

    monkeypatch.setattr(A.requests, "post", fake_post)
    r = A._upstream_chat("uncloseai", {"model": "turboderp/Qwen3.8-27B-exl3",
                                       "messages": _msgs()}, False)
    assert r.status_code == 200                         # nothing for the walk to spend
    assert len(sent) == 2 and sent[-1] == ["system", "user", "assistant", "user"]
    assert A._system_first_known("uncloseai", "turboderp/Qwen3.8-27B-exl3")
    sent.clear()                                        # the next call is merged BEFORE sending
    r = A._upstream_chat("uncloseai", {"model": "turboderp/Qwen3.8-27B-exl3",
                                       "messages": _msgs()}, False)
    assert r.status_code == 200 and len(sent) == 1


def test_another_model_still_gets_its_messages_untouched(isolated, monkeypatch):
    monkeypatch.setattr(A, "_SYSTEM_FIRST",
                        {("uncloseai", "turboderp/Qwen3.8-27B-exl3"): time.time()})
    sent = []
    monkeypatch.setattr(A.requests, "post", lambda *a, **kw: sent.append(
        [m["role"] for m in kw["json"]["messages"]]) or _Resp(200))
    A._upstream_chat("uncloseai", {"model": "other-model", "messages": _msgs()}, False)
    assert sent[0].count("system") >= 3          # the hub's own briefs ride on top, unmerged


def test_an_unrelated_400_is_not_retried_as_a_system_problem(isolated, monkeypatch):
    monkeypatch.setattr(A, "_SYSTEM_FIRST", {})
    sent = []
    monkeypatch.setattr(A.requests, "post", lambda *a, **kw: sent.append(1) or _Resp(
        400, '{"error": {"message": "max_tokens is too large"}}'))
    r = A._upstream_chat("uncloseai", {"model": "m", "messages": _msgs()}, False)
    assert r.status_code == 400 and len(sent) == 1
    assert not A._system_first_known("uncloseai", "m")


@pytest.mark.parametrize("text", [
    "System message must be at the beginning.",
    "Only one system message is allowed",
    "system messages should come first",
])
def test_the_common_spellings_are_recognised(text):
    assert A._SYSTEM_ORDER_ERR_RE.search(text)


# --------------------------------------------------------------------------- #
# 3. same-way failure streaks rest the pair for tool turns
# --------------------------------------------------------------------------- #

P = ("pg", "mg")


def _fail(cls, n, pair=P):
    for _ in range(n):
        A._note_pair_failure(pair[0], pair[1], cls)


def _rest_secs(pair=P):
    return A._pair_rest[pair]["until"] - time.time()


def test_three_identical_failures_rest_the_pair():
    _fail("http4xx", 2)
    assert not A._pair_resting(*P)
    _fail("http4xx", 1)
    assert A._pair_resting(*P) and A._tool_turn_sick(*P)
    assert 1790 < _rest_secs() <= 1800                  # 30 min base


def test_mixed_classes_do_not_add_up():
    A._note_pair_failure(*P, "http4xx")
    A._note_pair_failure(*P, "exc")
    A._note_pair_failure(*P, "deadline")
    assert not A._pair_resting(*P)


def test_the_rest_doubles_per_repeat_and_is_capped():
    _fail("exc", 3)
    secs = []
    for _ in range(6):
        secs.append(round(_rest_secs() / 60))
        A._pair_rest[P]["until"] = time.time() - 1          # the rest ran out ...
        A._note_pair_failure(*P, "exc")                      # ... one probe failure: a repeat
    assert secs == [30, 60, 120, 240, 360, 360], secs


def test_one_success_clears_the_streak_and_the_level():
    _fail("http4xx", 3)
    assert A._pair_resting(*P)
    A._record_outcome(*P, True)
    assert not A._pair_resting(*P) and P not in A._pair_rest
    _fail("http4xx", 2)
    assert not A._pair_resting(*P)                      # the count restarted too


def test_a_failure_streak_older_than_the_window_does_not_count():
    old = time.time() - A._STREAK_WINDOW - 5
    with A._empty_200_lock:
        A._empty_200[P] = [(old, "http4xx"), (old, "http4xx")]
    A._note_pair_failure(*P, "http4xx")
    assert not A._pair_resting(*P)


def test_quota_billing_and_window_statuses_are_exempt():
    for code in (429, 402, 413):
        for _ in range(4):
            A._streak_note_response(P[0], {"tools": [1], "model": P[1]}, _Resp(code))
    assert not A._pair_resting(*P) and not A._empty_200.get(P)


def test_a_4xx_on_a_tool_turn_counts_and_a_chat_turn_does_not():
    for _ in range(3):
        A._streak_note_response(P[0], {"messages": [], "model": P[1]}, _Resp(400))
    assert not A._empty_200.get(P)
    for _ in range(3):
        A._streak_note_response(P[0], {"tools": [1], "model": P[1]}, _Resp(400))
    assert A._pair_resting(*P)


def test_a_local_network_failure_is_nobodys_streak(monkeypatch):
    monkeypatch.setattr(A, "_local_net_failed", lambda *a, **k: True)
    _fail("exc", 5)
    assert not A._pair_resting(*P) and not A._empty_200.get(P)


def test_a_client_that_left_files_nothing(monkeypatch):
    monkeypatch.setattr(A, "_client_gone", lambda: True)
    _fail("deadline", 5)
    assert not A._pair_resting(*P)


def test_no_answer_in_time_counts_but_a_429_kind_does_not():
    for _ in range(3):
        A._note_recent_hop_failure(*P, "429")
    assert not A._pair_resting(*P)
    for _ in range(3):
        A._note_recent_hop_failure(*P, "deadline")
    assert A._pair_resting(*P)


def test_empty_200s_still_rest_the_pair_the_old_way():
    A._note_empty_200(*P)
    A._note_empty_200(*P)
    assert not A._empty_resting(*P)
    A._note_empty_200(*P)
    assert A._empty_resting(*P) and A._empty_streak(*P) == 3
    A._clear_empty_200(*P)
    assert not A._empty_resting(*P)


def test_resting_reorders_it_never_deletes_it(monkeypatch):
    """Last resort: behind every healthy pair in the chain, still in the chain."""
    _fleet(monkeypatch, failing=())
    _fail("http4xx", 3, QWEN)
    with A.app.test_request_context("/v1/chat/completions"):
        chain = A._build_chain("", "", 1000, require_tools=True, messages=FIX)
    order = [(p, m) for p, m in chain]
    assert QWEN in order and order.index(QWEN) == len(order) - 1


def test_an_explicit_provider_model_request_is_never_demoted(monkeypatch):
    _fleet(monkeypatch, failing=())
    _fail("http4xx", 3, QWEN)
    with A.app.test_request_context("/v1/chat/completions"):
        chain = A._build_chain(QWEN[0], QWEN[1], 1000, require_tools=True, messages=FIX,
                               pinned=True)
    assert chain[0] == QWEN
    with A.app.test_request_context("/v1/chat/completions"):
        routed = A._build_chain(QWEN[0], QWEN[1], 1000, require_tools=True, messages=FIX)
    assert routed[0] != QWEN                              # a router pick is demoted


def test_a_resting_pair_does_not_open_the_pick_while_others_live(monkeypatch):
    _fleet(monkeypatch, failing=())
    _fail("exc", 3, QWEN)
    for _ in range(15):
        A._session_pins.pop("new-session", None)
        assert _route() != QWEN


# --------------------------------------------------------------------------- #
# 3b. a window that cannot hold the request is not walked ahead of one that can
# --------------------------------------------------------------------------- #

def test_the_first_pick_skips_a_window_that_can_only_overflow(monkeypatch):
    _fleet(monkeypatch, failing=())
    monkeypatch.setattr(A, "_window_fits",
                        lambda pid, m, est: not ((pid, m) == QWEN and est > 40000))
    backup = dict(SCORES)
    SCORES["turboderp/Qwen3.8-27B-exl3"] = 150.0         # the strongest, but too small
    try:
        for _ in range(15):
            A._session_pins.pop("new-session", None)
            assert _route(est=90000) != QWEN
        A._session_pins.pop("new-session", None)
        assert _route(est=1000) == QWEN                  # it fits a small turn
    finally:
        SCORES.clear()
        SCORES.update(backup)


def test_the_first_pick_fails_open_when_nothing_has_room(monkeypatch):
    _fleet(monkeypatch, failing=())
    monkeypatch.setattr(A, "_window_fits", lambda pid, m, est: False)
    assert _route(est=900000)[0] in WORLD


def test_the_generic_walk_puts_hopeless_windows_last_and_keeps_them(monkeypatch):
    monkeypatch.setattr(A, "_window_fits", lambda pid, m, est: pid != "small")
    chain = [("small", "a"), ("big", "b"), ("big2", "c")]
    with A.app.test_request_context("/v1/chat/completions"):
        clock = A._ChainClock(tools=True, est=100000)
        assert list(clock._roomy_first(chain)) == [("big", "b"), ("big2", "c"), ("small", "a")]
        pinned = A._ChainClock(tools=True, est=100000, pinned=True)
        assert list(pinned._roomy_first(chain))[0] == ("small", "a")      # the named head stays
        none = A._ChainClock(tools=True, est=0)
        assert list(none._roomy_first(chain)) == chain


# --------------------------------------------------------------------------- #
# 4. the cost made visible: wasted_calls
# --------------------------------------------------------------------------- #

from tests.test_tool_turn_stalls import (  # noqa: E402,F401
    CHAIN, _body, _call, _dispatcher, _fast_delay, roles)


@pytest.fixture
def role_rows(monkeypatch, tmp_path):
    monkeypatch.setattr(A.config, "state_dir", lambda: str(tmp_path))
    import json as _json

    def read():
        path = tmp_path / A._ROLE_LOG_NAME
        if not path.exists():
            return []
        return [_json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]
    return read


def _last_turn(role_rows):
    return [r for r in role_rows() if r.get("event") == "turn"][-1]


def test_a_failed_first_hop_is_one_wasted_call(roles, monkeypatch, role_rows):
    _fast_delay(monkeypatch, 5.0)
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, {"error": "bad"}, 400), "p2": (0.0, _call("b"), 200),
        "p3": (0.0, _call(), 200)}, []))
    data, _h = A._swarm_tool_result(_body())
    assert data["model"] == "p2/m2"
    row = _last_turn(role_rows)
    assert row["actor_calls"] == 2 and row["wasted_calls"] == 1


def test_a_clean_turn_wastes_nothing(roles, monkeypatch, role_rows):
    _fast_delay(monkeypatch, 5.0)
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, _call("a"), 200), "p2": (0.0, _call(), 200),
        "p3": (0.0, _call(), 200)}, []))
    A._swarm_tool_result(_body())
    row = _last_turn(role_rows)
    assert row["wasted_calls"] == 0 and "actor_ok" not in row


def test_a_turn_nobody_answered_wastes_every_call(roles, monkeypatch, role_rows):
    _fast_delay(monkeypatch, 5.0)
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        p: (0.0, {"error": "x"}, 400) for p in ("p1", "p2", "p3")}, []))
    A._swarm_tool_result(_body())
    row = _last_turn(role_rows)
    assert row["served"] is None and row["wasted_calls"] == row["actor_calls"] == 3


def test_a_hop_skipped_for_its_window_is_not_a_wasted_call(roles, monkeypatch, role_rows):
    """`failed` lists it, but no call went out: wasted_calls is counted at the
    leg starts, not from `failed`. (The walk already puts a hopeless window
    behind the pairs that fit, so it is reached last.)"""
    _fast_delay(monkeypatch, 5.0)
    monkeypatch.setattr(A, "_window_fits", lambda pid, m, est: pid != "p1")
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, _call(), 200), "p2": (0.0, {"e": 1}, 400),
        "p3": (0.0, {"e": 1}, 400)}, calls))
    A._swarm_tool_result(_body())
    row = _last_turn(role_rows)
    assert [c[0] for c in calls] == ["p2", "p3"]          # p1 never dispatched to
    assert any("window too small" in f["why"] for f in row["failed"])
    assert row["actor_calls"] == 2 and row["wasted_calls"] == 2


def test_a_resting_pair_is_walked_last_but_still_reached(roles, monkeypatch, role_rows):
    _fast_delay(monkeypatch, 5.0)
    _fail("http4xx", 3, ("p1", "m1"))
    calls = []
    monkeypatch.setattr(A, "_dispatch_chat_with_deadline", _dispatcher({
        "p1": (0.0, _call("a"), 200), "p2": (0.0, {"e": 1}, 400),
        "p3": (0.0, {"e": 1}, 400)}, calls))
    data, _h = A._swarm_tool_result(_body())
    assert [c[0] for c in calls] == ["p2", "p3", "p1"]
    assert data["model"] == "p1/m1"


def test_role_eval_reports_calls_per_turn_and_wasted_share():
    import importlib.util
    import os
    spec = importlib.util.spec_from_file_location(
        "role_eval", os.path.join(os.path.dirname(A.__file__), "scripts", "role_eval.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rows = [{"event": "turn", "turn": "tool", "calls": 3, "served": "a/b",
             "failed": [], "wasted_calls": 2},
            # an OLD row (no field): derived from `failed`, a skipped hop is free
            {"event": "turn", "turn": "tool", "calls": 2, "served": "a/b",
             "failed": [{"pair": "x/y", "why": "HTTP 400"},
                        {"pair": "x/z", "why": "window too small for ~9 tokens"}]}]
    out = mod.roles_stats(rows)
    assert out["calls_per_turn"] == 2.5
    assert out["wasted_calls"] == 3 and out["wasted_calls_pct"] == 60.0


# --------------------------------------------------------------------------- #
# 5. a verifier that never answers leaves the pool (never the last one)
# --------------------------------------------------------------------------- #

def _runs(pair, usable, total):
    now = time.time()
    with A._team_stats_lock:
        A._verifier_stats[pair] = [(now - i, i < usable) for i in range(total)]


@pytest.fixture
def verifier_stats(monkeypatch):
    monkeypatch.setattr(A, "_team_stats_seed", lambda: None)
    A._verifier_stats.clear()
    yield
    A._verifier_stats.clear()


def test_a_verifier_with_no_usable_verdicts_is_left_out(verifier_stats):
    _runs(("nvidia", "moonshotai/kimi-k3"), 3, 38)
    _runs(("groq", "qwen/qwen3.8-27b"), 16, 21)
    pool = [("nvidia", "moonshotai/kimi-k3", 135.0), ("groq", "qwen/qwen3.8-27b", 134.0)]
    out = A._rank_verifier_pool(pool)
    assert [(p, m) for p, m, _s in out] == [("groq", "qwen/qwen3.8-27b")]


def test_the_last_verifier_stays_and_a_young_record_is_not_judged(verifier_stats):
    _runs(("a", "m1"), 0, 10)
    assert len(A._rank_verifier_pool([("a", "m1", 130.0)])) == 1
    _runs(("a", "m1"), 0, 3)                              # under _VERIFIER_MIN_EVENTS
    _runs(("b", "m2"), 5, 5)
    assert len(A._rank_verifier_pool([("a", "m1", 130.0), ("b", "m2", 129.0)])) == 2


def test_an_unusable_verifier_gets_another_look_later(verifier_stats):
    long_ago = time.time() - A._VERIFIER_RETRY_AFTER - 60
    with A._team_stats_lock:
        A._verifier_stats[("a", "m1")] = [(long_ago, False)] * 10
    assert not A._verifier_unusable("a", "m1")


# --------------------------------------------------------------------------- #
# 6. the real mechanism: _dispatch_chat feeds the streak; consumers honour it
# --------------------------------------------------------------------------- #

def _dispatch(monkeypatch, upstream, payload):
    monkeypatch.setattr(A, "_upstream_chat", upstream)
    return A._dispatch_chat(P[0], payload, False)


TOOL_PAYLOAD = {"model": P[1], "tools": [{"type": "function"}],
                "messages": [{"role": "user", "content": "hi"}]}


def test_dispatch_chat_files_a_tool_turns_4xx_and_rests_the_pair(monkeypatch):
    for _ in range(3):
        _dispatch(monkeypatch, lambda *a, **k: _Resp(400, "bad"), dict(TOOL_PAYLOAD))
    assert A._pair_resting(*P) and A._tool_turn_sick(*P)


def test_dispatch_chat_streamed_4xx_counts_too(monkeypatch):
    monkeypatch.setattr(A, "_upstream_chat", lambda *a, **k: _Resp(403, "no"))
    for _ in range(3):
        A._dispatch_chat(P[0], dict(TOOL_PAYLOAD), True)
    assert A._pair_resting(*P)


def test_dispatch_chat_without_tools_files_nothing(monkeypatch):
    chat = {"model": P[1], "messages": [{"role": "user", "content": "hi"}]}
    for _ in range(4):
        _dispatch(monkeypatch, lambda *a, **k: _Resp(400, "bad"), dict(chat))
    assert not A._pair_resting(*P) and not A._empty_200.get(P)


def test_dispatch_chat_429_files_nothing(monkeypatch):
    for _ in range(4):
        _dispatch(monkeypatch, lambda *a, **k: _Resp(429, "slow down"), dict(TOOL_PAYLOAD))
    assert not A._pair_resting(*P)


def _boom(*a, **k):
    raise A.requests.ConnectionError("connection reset by peer")


def test_dispatch_chat_request_exceptions_rest_the_pair(monkeypatch):
    for _ in range(3):
        with pytest.raises(A.requests.ConnectionError):
            _dispatch(monkeypatch, _boom, dict(TOOL_PAYLOAD))
    assert A._pair_resting(*P)


def test_dispatch_chat_local_network_failures_rest_nobody(monkeypatch):
    monkeypatch.setattr(A, "_local_net_failed", lambda *a, **k: True)
    for _ in range(4):
        with pytest.raises(A.requests.ConnectionError):
            _dispatch(monkeypatch, _boom, dict(TOOL_PAYLOAD))
    assert not A._pair_resting(*P)


def test_a_delivering_dispatch_does_not_clear_but_a_recorded_success_does(monkeypatch):
    _fail("http4xx", 2)
    _dispatch(monkeypatch, lambda *a, **k: _Resp(200), dict(TOOL_PAYLOAD))
    assert len(A._empty_200[P]) == 2          # a 200 is not yet a DELIVERY (could be empty)
    A._record_outcome(*P, True)
    assert not A._empty_200.get(P)


def test_the_verifier_and_specialist_pool_skips_a_resting_pair(monkeypatch):
    monkeypatch.setattr(A, "_swarm_member_sick", lambda *a, **k: False)
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: 130.0)
    _fail("exc", 3, ("p2", "m2"))
    rows = A._role_candidates([("p1", "m1"), ("p2", "m2"), ("p3", "m3")], ("p1", "m1"))
    assert [(r[0], r[1]) for r in rows] == [("p3", "m3")]


def _orch_world(monkeypatch):
    monkeypatch.setattr(A, "_available_providers", lambda *a, **k: [P[0]])
    monkeypatch.setattr(A, "_model_block_reason", lambda *a, **k: None)
    monkeypatch.setattr(A, "_prefetch_free_models", lambda *a, **k: {P[0]: [P[1]]})
    monkeypatch.setattr(A, "_is_model_skipped", lambda *a, **k: False)
    monkeypatch.setattr(A.quota, "is_model_throttled", lambda *a, **k: False)
    monkeypatch.setattr(A, "_supports_tools", lambda *a, **k: True)


def test_the_chosen_orchestrator_is_not_skipped_for_the_long_rest(monkeypatch):
    """It is the user's pick (the chain already seeds it as pinned): an
    hours-long rest would keep it from ever earning the success that clears it."""
    _orch_world(monkeypatch)
    _fail("http4xx", 3)
    assert A._pair_resting(*P)
    assert A._orch_unusable(P[0], P[1], tools=True) is None
    A._clear_empty_200(*P)                    # ...but the short empties rest still applies
    for _ in range(3):
        A._note_empty_200(*P)
    assert "resting" in (A._orch_unusable(P[0], P[1], tools=True) or "")


def test_the_roles_tail_spares_the_orchestrator_lead_from_the_long_rest():
    _fail("http4xx", 3)
    with A.app.test_request_context("/v1/chat/completions"):
        assert A._resting_for_walk(*P)
        A.g.hub_orchestrator_pair = P
        assert not A._resting_for_walk(*P)
