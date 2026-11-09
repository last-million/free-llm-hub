"""A strong model that failed FAST is asked again before a weak one; a stall is not.

ASKED 2026-11-09, looking at the Activity page: "he used glm 4.5 -- it is old, and there are
newer, better models available".

MEASURED on the live hub (OpenCode `coding-max`, 20:44-20:57 UTC): the router opened turns on
glm/glm-4.5-flash (hub score 10), glm/glm-4.7-flash and zenmux glm-4.6v, while nvidia
deepseek-v4.1-flash (134.1) and nvidia / dahl glm-5.3-flash (133.6) were state "ok". A pair that
429'd or ran out its time in the last ten minutes is dropped from the opening pick, so once every
strong model had a recent failure only the never-tried weak models were left.

FIRST VERSION (3269e2e): every strong recent failure stayed ahead of the weak models. MEASURED
live the same evening (21:53-22:12, six turns): every turn opened on a strong model and then
waited on it -- nvidia "_HopBudgetExceeded", 143-210 s per turn -- before a weak model served
anyway. The same turns had taken 12-45 s. So the rule was narrowed:

  * a strong pair that failed FAST (a 429, an HTTP error: milliseconds) stays ahead of the weak
    ones, in the opening pick and in the chain behind it;
  * a strong pair that STALLED, or that the hub measured to keep failing (resting, never chains,
    benched), keeps the place it always had: retried once its ten-minute mark runs out.

Hermetic: fake providers, scores and failure ledger.
"""
import pytest

import app as A


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
    monkeypatch.setattr(A, "_provider_stalling", lambda pid: False)
    return world


PIDS = {"nv-ds": "nv", "nv-glm": "nv", "glm-45": "zl", "glm-47": "zl", "dh-glm": "dh"}
TOOLS = [{"role": "user", "content": "Refactor the auth module and run the tests."}]
HARD = [{"role": "user", "content": "Explain in depth how a B-tree rebalances."}]
STRONG = {"nv-ds", "nv-glm", "dh-glm"}
WEAK = {"glm-45", "glm-47"}


def _route(msgs, **kw):
    with A.app.test_request_context():
        return A._route_by_difficulty(msgs, None, 60000, **kw)


def _fail(model_ids, kind):
    for m in model_ids:
        A._note_recent_hop_failure(PIDS[m], m, kind)


def _chain_names(msgs, **kw):
    _p, model, _d = _route(msgs, **kw)
    with A.app.test_request_context():
        chain = A._build_chain(_p, model, 60000, messages=msgs,
                               require_tools=kw.get("require_tools", False))
    return [m for _pid, m in chain]


# ------------------------------------------------------------------- the opening pick
def test_strong_models_that_only_429d_still_open_a_tool_turn(fleet):
    """Every strong pair was refused in milliseconds (dahl's "at capacity"); the weak ones
    have no record. Asking a strong one again is free, so it opens."""
    _fail(STRONG, "429")
    _p, model, _d = _route(TOOLS, require_tools=True)
    assert model in STRONG, model


def test_strong_models_that_all_stalled_do_not_block_the_turn(fleet):
    """THE LIVE LESSON: a stall costs a whole hop budget. With every strong pair stalled the
    turn opens as it always did (a model with no failure), and the stalled ones are retried
    once their ten-minute mark runs out."""
    _fail(STRONG, "deadline")
    _p, model, _d = _route(TOOLS, require_tools=True)
    assert model in WEAK, model


def test_a_429_opens_and_a_stall_does_not(fleet):
    _fail({"nv-ds", "nv-glm"}, "deadline")
    _fail({"dh-glm"}, "429")
    _p, model, _d = _route(TOOLS, require_tools=True)
    assert model == "dh-glm"


def test_every_stall_kind_is_left_in_its_old_place(fleet):
    for kind in ("deadline", "timeout", "reasoning_stall"):
        A._recent_hop_fail.clear()
        _fail(STRONG, kind)
        _p, model, _d = _route(TOOLS, require_tools=True)
        assert model in WEAK, (kind, model)


def test_a_healthy_strong_model_is_still_preferred(fleet):
    """Unchanged: while one strong model is healthy it opens, whatever failed."""
    _fail({"nv-ds", "nv-glm"}, "429")
    _p, model, _d = _route(TOOLS, require_tools=True)
    assert model == "dh-glm"


def test_with_no_failures_nothing_changes(fleet):
    _p, model, _d = _route(TOOLS, require_tools=True)
    assert model in STRONG


def test_a_hard_chat_turn_gets_the_same_rule(fleet):
    """The tier floor of a hard chat turn is 78: a 10 or 30 must not open it while a strong
    model that merely 429'd is there."""
    _fail(STRONG, "429")
    _p, model, _d = _route(HARD)
    assert model in STRONG, model


def test_a_resting_strong_pair_is_not_promoted(fleet, monkeypatch):
    """The hub already decided to stop asking it for a while; this rule must not undo that."""
    _fail(STRONG, "429")
    monkeypatch.setattr(A, "_pair_resting", lambda pid, m: m in STRONG)
    _p, model, _d = _route(TOOLS, require_tools=True)
    assert model in WEAK, model


def test_a_pair_that_never_chains_is_not_promoted(fleet, monkeypatch):
    _fail(STRONG, "429")
    monkeypatch.setattr(A, "_never_chains", lambda pid, m: m in STRONG)
    _p, model, _d = _route(TOOLS, require_tools=True)
    assert model in WEAK, model


def test_every_candidate_weak_is_unchanged(fleet, monkeypatch):
    scores = {"nv-ds": 20.0, "nv-glm": 25.0, "glm-45": 10.0, "glm-47": 30.0, "dh-glm": 22.0}
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: scores[m])
    _fail({"glm-47"}, "429")
    _p, model, _d = _route(TOOLS, require_tools=True)
    assert model is not None


def test_the_flag_off_is_the_old_behaviour(fleet, monkeypatch):
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=None: False if k == "recent_fail_keeps_strong" else d)
    _fail(STRONG, "429")
    _p, model, _d = _route(TOOLS, require_tools=True)
    assert model in WEAK, model


# ------------------------------------------------------------------- the chain behind the pick
def test_the_chain_asks_fast_failed_strong_models_before_weak_ones(fleet):
    _fail(STRONG, "429")
    names = _chain_names(TOOLS, require_tools=True)
    assert names[0] in STRONG, names
    last_strong = max(i for i, m in enumerate(names) if m in STRONG)
    first_weak = min(i for i, m in enumerate(names) if m in WEAK)
    assert last_strong < first_weak, names


def test_the_chain_keeps_stalled_strong_models_in_the_tail(fleet):
    """dahl 429'd, nvidia stalled: dahl is asked first, then the weak healthy models, and the
    stalled nvidia pairs stay the last resort (never dropped)."""
    _fail({"dh-glm"}, "429")
    _fail({"nv-ds", "nv-glm"}, "deadline")
    names = _chain_names(TOOLS, require_tools=True)
    assert names[0] == "dh-glm", names
    assert set(names[1:3]) == WEAK, names
    assert set(names[3:]) == {"nv-ds", "nv-glm"}, names


def test_a_healthy_strong_model_leads_then_fast_failed_strong_then_weak(fleet):
    _fail({"nv-ds", "nv-glm"}, "429")
    names = _chain_names(TOOLS, require_tools=True)
    assert names[0] == "dh-glm", names
    assert set(names[1:3]) == {"nv-ds", "nv-glm"}, names
    assert set(names[3:]) == WEAK, names


def test_the_chain_flag_off_is_the_old_order(fleet, monkeypatch):
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=None: False if k == "recent_fail_keeps_strong" else d)
    _fail(STRONG, "429")
    with A.app.test_request_context():
        names = [m for _p, m in A._build_chain("nv", "nv-ds", 60000, messages=TOOLS,
                                               require_tools=True)]
    assert names[0] in WEAK, names


def test_a_hard_chat_chain_gets_the_same_order(fleet):
    _fail(STRONG, "429")
    names = _chain_names(HARD)
    assert names[0] in STRONG, names
    assert max(i for i, m in enumerate(names) if m in STRONG) < min(
        i for i, m in enumerate(names) if m in WEAK), names


def test_a_small_hop_cap_does_not_squeeze_out_the_promoted_models(fleet, monkeypatch):
    """The old tail kept only _RECENT_FAIL_TAIL (2) recent failures past the cap; promoted
    ones bring their slots with them."""
    monkeypatch.setattr(A, "TOOLS_MAX_HOPS", 2)
    _fail(STRONG, "429")
    names = _chain_names(TOOLS, require_tools=True)
    assert STRONG <= set(names), names


def test_the_same_models_are_in_the_chain_with_and_without_the_rule(fleet, monkeypatch):
    """A reorder only: nothing is dropped and nothing is added."""
    _fail({"nv-ds", "nv-glm"}, "429")
    on = _chain_names(TOOLS, require_tools=True)
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=None: False if k == "recent_fail_keeps_strong" else d)
    with A.app.test_request_context():
        off = [m for _p, m in A._build_chain("dh", "dh-glm", 60000, messages=TOOLS,
                                             require_tools=True)]
    assert sorted(on) == sorted(off), (on, off)
    assert len(on) == len(set(on))


def test_a_simple_small_turn_is_left_alone(fleet):
    _fail(STRONG, "429")
    with A.app.test_request_context():
        A._mark_turn_shape("simple", 400)
        names = [m for _p, m in A._build_chain("", "", 400, messages=HARD)]
    assert names[0] in WEAK, names                 # the old order: a one-liner needs no floor


# ------------------------------------------------------------------- the helpers
def _c(score, model):
    return (score, PIDS[model], model)


def test_helper_returns_the_old_result_when_a_strong_healthy_one_exists(fleet):
    cs = [_c(134.1, "nv-ds"), _c(133.6, "dh-glm"), _c(10.0, "glm-45")]
    _fail({"nv-ds"}, "429")
    assert A._drop_recent_failures(cs, 90.0) == [_c(133.6, "dh-glm"), _c(10.0, "glm-45")]


def test_helper_keeps_only_the_fast_failed_strong_ones(fleet):
    cs = [_c(134.1, "nv-ds"), _c(133.6, "nv-glm"), _c(10.0, "glm-45")]
    _fail({"nv-ds"}, "429")
    _fail({"nv-glm"}, "deadline")
    assert A._drop_recent_failures(cs, 90.0) == [_c(134.1, "nv-ds")]


def test_helper_all_stalled_strong_gives_the_healthy_weak_ones_as_before(fleet):
    cs = [_c(134.1, "nv-ds"), _c(133.6, "nv-glm"), _c(10.0, "glm-45")]
    _fail({"nv-ds", "nv-glm"}, "deadline")
    assert A._drop_recent_failures(cs, 90.0) == [_c(10.0, "glm-45")]


def test_helper_when_everything_failed_it_is_the_old_rule(fleet):
    cs = [_c(134.1, "nv-ds"), _c(10.0, "glm-45")]
    _fail({"nv-ds"}, "deadline")
    _fail({"glm-45"}, "429")
    assert A._drop_recent_failures(cs, 90.0) == [_c(10.0, "glm-45")]


def test_helper_without_a_floor_is_the_old_rule(fleet):
    cs = [_c(134.1, "nv-ds"), _c(10.0, "glm-45")]
    _fail({"nv-ds"}, "429")
    assert A._drop_recent_failures(cs, 0) == [_c(10.0, "glm-45")]


def test_helper_empty_in_empty_out_and_never_raises(fleet, monkeypatch):
    assert A._drop_recent_failures([], 90.0) == []
    monkeypatch.setattr(A, "_recent_hop_failure",
                        lambda p, m: (_ for _ in ()).throw(RuntimeError("x")))
    cs = [_c(134.1, "nv-ds")]
    assert A._drop_recent_failures(cs, 90.0) == cs
    assert A._fast_failed_strong(cs[0], 90.0) is False


def test_fast_failed_strong_truth_table(fleet, monkeypatch):
    c = _c(134.1, "nv-ds")
    assert A._fast_failed_strong(c, 90.0) is False            # no recent failure at all
    _fail({"nv-ds"}, "429")
    assert A._fast_failed_strong(c, 90.0) is True
    assert A._fast_failed_strong(c, 0) is False               # no floor, no promotion
    assert A._fast_failed_strong(_c(10.0, "glm-45"), 90.0) is False   # weak
    monkeypatch.setattr(A, "_is_pair_benched", lambda pid, m: True)
    assert A._fast_failed_strong(c, 90.0) is False            # benched for junk answers
