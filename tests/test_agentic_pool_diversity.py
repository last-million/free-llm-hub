"""The agentic pool COLLAPSED to one model family instead of merely narrowing.

USER 2026-08-07: "he use only gemini model in current task but he should always
dispatch and use best models in swarm agents and sub agents".

MEASURED that day, one funnel over the live fleet:

    alive + tool-capable        660 models / 21 providers
    ∩ _TOOL_PROVEN              132 models / 13 providers
    ∩ not _LOW_QUALITY_RE        31 models /  3 providers

_TOOL_PROVEN names gpt-oss and nemotron; _LOW_QUALITY_RE demotes exactly those.
The intersection is essentially "gemini-3" -- and 14 routes sampled across 14
DISTINCT conversations returned just two ids, both google/gemini-3.x. 20 other
providers' quota sat unused.

The pre-existing fail-open only fired when the filtered pool was EMPTY, and 31
models is not zero, so it never triggered. The fix widens on PROVIDER
DIVERSITY (_MIN_AGENTIC_PROVIDERS) rather than emptiness.

Why widening is safe now and would not have been in July: _TOOL_PROVEN is a
hand-typed allowlist standing in for a feedback signal that did not exist. It
does now -- a model that fails earns a lasting reliability penalty
(_record_outcome) and a 6h dead-model sideline on 402/403/404/410, both folded
into _agentic_score. A listing a provider cannot really serve costs ONE hop and
then sinks itself.
"""
import pytest

import app


@pytest.fixture(autouse=True)
def _clear_session_pins():
    """_session_pins is module-level and holds a pick for 4h keyed on the
    conversation. Both tests here drive the same prompt shapes, so without this
    the second test inherits the first one's pins and asserts against a
    decision that was never re-made -- it caught me once already."""
    app._session_pins.clear()
    yield
    app._session_pins.clear()


def _cand(score, pid, model):
    return (score, pid, model)


def _drive(monkeypatch, agentic, rounds=14):
    """Run the REAL _route_by_difficulty over a fixed fleet.

    Deliberately not a re-implementation of the selection logic: a test that
    copies the code under test still passes after the code is reverted, which
    is exactly the regression this file exists to catch. So patch the two
    inputs that build the candidate list and let the real function run.
    """
    by_pid = {}
    scores = {}
    for score, pid, model in agentic:
        by_pid.setdefault(pid, []).append(model)
        scores[(pid, model)] = score
    monkeypatch.setattr(app, "_available_providers", lambda: list(by_pid))
    monkeypatch.setattr(app, "_prefetch_auto_models", lambda pids: dict(by_pid))
    monkeypatch.setattr(app, "_benchmark_score", lambda pid, m: scores.get((pid, m), 10.0))
    monkeypatch.setattr(app, "_provider_capable", lambda pid, est: True)
    monkeypatch.setattr(app, "_supports_tools", lambda pid, m: True)
    monkeypatch.setattr(app, "_is_model_dead", lambda pid, m: False)
    monkeypatch.setattr(app, "_context_ok", lambda pid, m, est: True)
    monkeypatch.setattr(app.quota, "is_model_throttled", lambda pid, m: False)
    monkeypatch.setattr(app.quota, "model_status",
                        lambda pid, m: {"exhausted": False, "used": 0, "limit": None,
                                        "limit_known": False, "remaining": None,
                                        "throttled": False})
    out = []
    for i in range(rounds):
        # A DISTINCT conversation each round, so the 4h session pin never
        # masks a repeat and we see the real per-task spread.
        msgs = [{"role": "user",
                 "content": "Task %d: refactor module %d, add tests, debug, optimize. %s"
                            % (i, i, "y" * 2000)}]
        pid, model, _diff = app._route_by_difficulty(msgs, est=3000, require_tools=True)
        out.append((pid, model))
    return out


def test_a_collapsed_pool_widens_back_out(monkeypatch):
    """proven ∩ not-low-quality covers ONE provider here; the pool must widen
    rather than hand every request to that provider."""
    # gemini-3 is tool-proven and not low-quality -> survives every filter.
    # gpt-oss/nemotron are proven but low-quality -> demoted away.
    # glm/kimi are neither -> only reachable once the pool widens.
    agentic = [
        _cand(100.0, "google", "gemini-3.5-flash"),
        _cand(99.0, "google", "gemini-3.6-flash"),
        _cand(120.0, "cerebras", "gpt-oss-120b"),      # proven, low-quality
        _cand(118.0, "groq", "nemotron-3-super"),      # proven, low-quality
        _cand(130.0, "nvidia", "z-ai/glm-5.2"),        # strong, not proven
        _cand(128.0, "dahl", "moonshotai/kimi-k2.6"),  # strong, not proven
    ]
    picked = _drive(monkeypatch, agentic)
    providers = {p for p, _m in picked}
    assert len(providers) > 1, (
        "every route went to %r -- the pool collapsed instead of widening" % providers)
    assert providers & {"nvidia", "dahl"}, (
        "widening must actually reach the strong non-allowlisted models, got %r" % providers)


def test_a_diverse_proven_pool_is_left_alone(monkeypatch):
    """Proven-first still wins when it is genuinely diverse -- this must not
    become 'always widen', which would undo the allowlist entirely."""
    agentic = [
        _cand(100.0, "google", "gemini-3.5-flash"),
        _cand(99.0, "g4f", "gemini-3.6-flash"),
        _cand(98.0, "llm7", "gemini-3.1-flash-lite"),
        _cand(97.0, "openrouter", "gemini-3-flash-preview"),
        _cand(96.0, "kilocode", "gemini-3.5-flash-lite"),
        _cand(130.0, "nvidia", "z-ai/glm-5.2"),        # strong but NOT proven
    ]
    picked = _drive(monkeypatch, agentic, rounds=12)
    assert "nvidia" not in {p for p, _m in picked}, (
        "a proven pool spanning 5 providers is diverse enough; widening to an "
        "unproven model there would defeat the allowlist")


def test_the_threshold_is_a_low_bar_not_a_diversity_quota():
    """Set too high, this would widen on every request and _TOOL_PROVEN would
    stop meaning anything."""
    assert 2 <= app._MIN_AGENTIC_PROVIDERS <= 6
