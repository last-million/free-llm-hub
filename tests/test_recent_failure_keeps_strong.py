"""A recent failure does not hand the turn to a weak model.

ASKED 2026-11-09, looking at the Activity page: "he used glm 4.5 -- it is old, and there are
newer, better models available".

MEASURED on the live hub (OpenCode `coding-max`, 20:44-20:57 UTC): the router opened turns on
glm/glm-4.5-flash (hub score 10), glm/glm-4.7-flash, zenmux glm-4.6v and kilocode's
nemotron-3.5-super-vl, "[spread] ... -> glm/glm-4.5-flash (pool 1, held elsewhere 4)", while
nvidia deepseek-v4.1-flash (134.1) and nvidia / dahl glm-5.3-flash (133.6) were state "ok". The
weak pick spent its hop ("only reasoning for 58s ... handed to the next model") and the
best->auto fallback served a strong model after 109-146 s.

CAUSE: a pair that 429'd or ran out its time in the last ten minutes is dropped from the primary
pick. When every strong model has stalled on a huge request, that leaves only the models with no
failure record -- the never-tried weak tail -- and `agentic` then fails open onto them. The
fail-open has to be judged INSIDE the strong tier: while a healthy candidate at or above the tier
floor remains the filter is exactly what it was; when none does but strong ones exist they stay
(the ones that did not stall first) and the weak healthy ones leave the primary pick.

Hermetic: fake providers, scores and failure ledger.
"""
import pytest

import app as A


class _Fleet:
    pass


@pytest.fixture
def fleet(monkeypatch):
    world = {"nv": ["nv-ds", "nv-glm"], "zl": ["glm-45", "glm-47"], "dh": ["dh-glm"]}
    scores = {"nv-ds": 134.1, "nv-glm": 133.6, "glm-45": 10.0, "glm-47": 30.0, "dh-glm": 133.6}
    monkeypatch.setattr(A, "_available_providers", lambda *a, **k: list(world))
    monkeypatch.setattr(A, "_prefetch_auto_models", lambda pids: dict(world))
    monkeypatch.setattr(A, "_auto_models", lambda pid: list(world[pid]))
    monkeypatch.setattr(A, "_provider_capable", lambda pid, est: True)
    monkeypatch.setattr(A, "_supports_tools", lambda pid, m: True)
    monkeypatch.setattr(A, "_session_pin_get", lambda key: None)
    monkeypatch.setattr(A, "_session_pin_set", lambda *a, **k: None)
    monkeypatch.setattr(A, "_is_fast", lambda pid, m: True)
    monkeypatch.setattr(A, "_measured_latency_ms", lambda pid, m: None)
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: scores[m])
    monkeypatch.setattr(A, "_active_mode", lambda: A.MODE_ALL)
    monkeypatch.setattr(A, "_is_model_dead", lambda pid, m: False)
    monkeypatch.setattr(A.quota, "is_model_throttled", lambda pid, m: False)
    monkeypatch.setattr(A.quota, "model_status", lambda pid, m: {"exhausted": False})
    monkeypatch.setattr(A, "_context_ok", lambda pid, m, est: True)
    monkeypatch.setattr(A, "_sub_available_providers", lambda: [])
    monkeypatch.setattr(A, "_rescue_pids", lambda: frozenset())
    monkeypatch.setattr(A.config, "get_flag", lambda k, d=None: d)
    for name in ("_reliability_penalty", "_latency_penalty", "_answer_quality_penalty"):
        monkeypatch.setattr(A, name, lambda pid, m: 0.0)
    monkeypatch.setattr(A, "_chain_reliability_band", lambda pid, m: 0)
    monkeypatch.setattr(A, "_tool_turn_sick", lambda pid, m: False)
    monkeypatch.setattr(A, "_relay_tool_sick", lambda pid, m: False)
    return world


TOOLS = [{"role": "user", "content": "Refactor the auth module and run the tests."}]
HARD = [{"role": "user", "content": "Explain in depth how a B-tree rebalances."}]
STRONG = {"nv-ds", "nv-glm", "dh-glm"}
WEAK = {"glm-45", "glm-47"}


def _route(msgs, **kw):
    with A.app.test_request_context():
        return A._route_by_difficulty(msgs, None, 60000, **kw)


def _fail(model_ids, kind="deadline"):
    for pid, models in {"nv": ["nv-ds", "nv-glm"], "zl": ["glm-45", "glm-47"], "dh": ["dh-glm"]}.items():
        for m in models:
            if m in model_ids:
                A._note_recent_hop_failure(pid, m, kind)


# ------------------------------------------------------------------- the bug
def test_strong_models_that_all_stalled_still_open_a_tool_turn(fleet):
    """THE LIVE CASE: every strong pair stalled in the last ten minutes; the weak ones have no
    record. The turn must open on a strong model, not on glm-4.5."""
    _fail(STRONG, "deadline")
    _p, model, _d = _route(TOOLS, require_tools=True)
    assert model in STRONG, model


def test_a_429_opens_before_a_stall(fleet):
    """A 429 fails in milliseconds; a stall costs a whole hop budget."""
    _fail({"nv-ds", "nv-glm"}, "deadline")
    _fail({"dh-glm"}, "429")
    _p, model, _d = _route(TOOLS, require_tools=True)
    assert model == "dh-glm"


def test_a_healthy_strong_model_is_still_preferred(fleet):
    """Unchanged: while one strong model is healthy it opens, whatever failed."""
    _fail({"nv-ds", "nv-glm"}, "deadline")
    _p, model, _d = _route(TOOLS, require_tools=True)
    assert model == "dh-glm"


def test_with_no_failures_nothing_changes(fleet):
    _p, model, _d = _route(TOOLS, require_tools=True)
    assert model in STRONG


def test_a_hard_chat_turn_gets_the_same_protection(fleet):
    """The tier floor of a hard chat turn is 78: a 10 or 30 must not open it either."""
    _fail(STRONG, "deadline")
    _p, model, _d = _route(HARD)
    assert model in STRONG, model


def test_every_candidate_weak_is_unchanged(fleet, monkeypatch):
    """Nothing clears the floor at all: the weak tail is the pool, exactly as before."""
    scores = {"nv-ds": 20.0, "nv-glm": 25.0, "glm-45": 10.0, "glm-47": 30.0, "dh-glm": 22.0}
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: scores[m])
    _fail({"glm-47"}, "429")
    _p, model, _d = _route(TOOLS, require_tools=True)
    assert model is not None


def test_the_flag_off_is_the_old_behaviour(fleet, monkeypatch):
    """Documents the bug it fixes: with the flag off a weak, never-failed model opens."""
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=None: False if k == "recent_fail_keeps_strong" else d)
    _fail(STRONG, "deadline")
    _p, model, _d = _route(TOOLS, require_tools=True)
    assert model in WEAK, model


# ------------------------------------------------------------------- the chain behind the pick
def test_the_chain_behind_a_strong_opener_tries_the_other_strong_models_before_weak_ones(fleet):
    """If the strong opener fails again, the walk goes to the OTHER strong models (which only
    failed recently) before it reaches the weak ones that have no record."""
    _fail(STRONG, "deadline")
    _p, model, _d = _route(TOOLS, require_tools=True)
    with A.app.test_request_context():
        chain = A._build_chain(_p, model, 60000, messages=TOOLS)
    names = [m for _pid, m in chain]
    assert names[0] in STRONG, names
    last_strong = max(i for i, m in enumerate(names) if m in STRONG)
    first_weak = min((i for i, m in enumerate(names) if m in WEAK), default=len(names))
    assert last_strong < first_weak, names


def _chain_names(msgs, **kw):
    _p, model, _d = _route(msgs, **kw)
    with A.app.test_request_context():
        chain = A._build_chain(_p, model, 60000, messages=msgs, require_tools=kw.get("require_tools", False))
    return [m for _pid, m in chain]


def test_a_healthy_strong_model_still_leads_then_the_failed_strong_then_the_weak(fleet):
    _fail({"nv-ds", "nv-glm"}, "deadline")
    names = _chain_names(TOOLS, require_tools=True)
    assert names[0] == "dh-glm", names                         # the healthy strong one opens
    assert set(names[1:3]) == {"nv-ds", "nv-glm"}, names       # then the strong ones that stalled
    assert set(names[3:]) == WEAK, names                       # the weak ones are the last resort


def test_the_chain_flag_off_is_the_old_order(fleet, monkeypatch):
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=None: False if k == "recent_fail_keeps_strong" else d)
    _fail(STRONG, "deadline")
    with A.app.test_request_context():
        names = [m for _p, m in A._build_chain("nv", "nv-ds", 60000, messages=TOOLS,
                                               require_tools=True)]
    assert names[0] in WEAK, names                             # weak first, strong in the tail


def test_a_hard_chat_chain_gets_the_same_order(fleet):
    _fail(STRONG, "deadline")
    names = _chain_names(HARD)
    assert names[0] in STRONG, names
    assert max(i for i, m in enumerate(names) if m in STRONG) < min(
        i for i, m in enumerate(names) if m in WEAK), names


def test_the_failed_strong_ones_keep_a_place_past_the_hop_cap(fleet, monkeypatch):
    """A small hop cap does not squeeze the failed strong models out: the old recent-failure
    tail kept only _RECENT_FAIL_TAIL (2) of them past the cap, so with three of them the third
    was lost; moved ahead of the weak hops they now bring their slots with them."""
    monkeypatch.setattr(A, "TOOLS_MAX_HOPS", 2)
    _fail(STRONG, "deadline")
    names = _chain_names(TOOLS, require_tools=True)
    assert STRONG <= set(names), names


def test_the_same_models_are_in_the_chain_with_and_without_the_rule(fleet, monkeypatch):
    """A reorder only: nothing is dropped and nothing is added."""
    _fail({"nv-ds", "nv-glm"}, "deadline")
    on = _chain_names(TOOLS, require_tools=True)
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=None: False if k == "recent_fail_keeps_strong" else d)
    with A.app.test_request_context():
        off = [m for _p, m in A._build_chain("dh", "dh-glm", 60000, messages=TOOLS,
                                             require_tools=True)]
    assert sorted(on) == sorted(off), (on, off)
    assert len(on) == len(set(on))


def test_a_simple_small_turn_is_left_alone(fleet):
    """A one-liner may be answered by a weak fast model: the rule needs a floor."""
    _fail(STRONG, "deadline")
    with A.app.test_request_context():
        A._mark_turn_shape("simple", 400)
        names = [m for _p, m in A._build_chain("", "", 400, messages=HARD)]
    assert names, names                                        # still builds; order is the old one


# ------------------------------------------------------------------- the helper itself
def _c(score, pid, model):
    return (score, pid, model)


def test_helper_returns_the_old_result_when_a_strong_healthy_one_exists(fleet):
    cs = [_c(134.1, "nv", "nv-ds"), _c(133.6, "dh", "dh-glm"), _c(10.0, "zl", "glm-45")]
    _fail({"nv-ds"}, "429")
    got = A._drop_recent_failures(cs, 90.0)
    assert got == [_c(133.6, "dh", "dh-glm"), _c(10.0, "zl", "glm-45")]


def test_helper_keeps_strong_and_drops_weak_when_no_strong_is_healthy(fleet):
    cs = [_c(134.1, "nv", "nv-ds"), _c(133.6, "nv", "nv-glm"), _c(10.0, "zl", "glm-45")]
    _fail({"nv-ds"}, "429")
    _fail({"nv-glm"}, "deadline")
    assert A._drop_recent_failures(cs, 90.0) == [_c(134.1, "nv", "nv-ds")]


def test_helper_all_stalled_strong_stay_in_order(fleet):
    cs = [_c(134.1, "nv", "nv-ds"), _c(133.6, "nv", "nv-glm"), _c(10.0, "zl", "glm-45")]
    _fail({"nv-ds", "nv-glm"}, "deadline")
    assert A._drop_recent_failures(cs, 90.0) == [_c(134.1, "nv", "nv-ds"), _c(133.6, "nv", "nv-glm")]


def test_helper_when_everything_failed_it_is_the_old_rule(fleet):
    cs = [_c(134.1, "nv", "nv-ds"), _c(10.0, "zl", "glm-45")]
    _fail({"nv-ds"}, "deadline")
    _fail({"glm-45"}, "429")
    # nothing healthy -> the ones that did not stall (the 429'd weak one), as always
    assert A._drop_recent_failures(cs, 90.0) == [_c(10.0, "zl", "glm-45")]


def test_helper_without_a_floor_is_the_old_rule(fleet):
    cs = [_c(134.1, "nv", "nv-ds"), _c(10.0, "zl", "glm-45")]
    _fail({"nv-ds"}, "deadline")
    assert A._drop_recent_failures(cs, 0) == [_c(10.0, "zl", "glm-45")]


def test_helper_empty_in_empty_out_and_never_raises(fleet, monkeypatch):
    assert A._drop_recent_failures([], 90.0) == []
    monkeypatch.setattr(A, "_recent_hop_failure", lambda p, m: (_ for _ in ()).throw(RuntimeError("x")))
    cs = [_c(134.1, "nv", "nv-ds")]
    assert A._drop_recent_failures(cs, 90.0) == cs
