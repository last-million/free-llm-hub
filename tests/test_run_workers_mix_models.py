"""The workers of one Multi run use different strong models.

MEASURED 2026-09-30, run swarm-4f2aab7204a4: its workers go one after another
(diagnose -> fix -> verify -> review), and _spread_pool only spreads workers
running at the SAME time, so every worker drew from the same top two models.
Owner: "mixing between top perfect models is a good idea" -- a reviewer on a
different model than the implementer is a real second opinion.
"""
import app as A
import swarm_windows as SW

from tests.test_best_model_leads import FIX, _fleet


def _run_with(monkeypatch, used):
    """Worker 'w-new' in a run whose other workers opened on `used`."""
    sibs = ["w-%d" % i for i in range(len(used))]
    monkeypatch.setattr(A, "_WORKER_MODEL", {})
    monkeypatch.setattr(A.swarm_windows, "sibling_sessions",
                        lambda sid: sibs if sid == "w-new" else [])
    for s, m in zip(sibs, used):
        A._note_worker_model(s, m)


def test_a_worker_avoids_the_models_its_run_used(monkeypatch):
    _run_with(monkeypatch, ["z-ai/glm-5.3"])
    pool = [(138.0, "nvidia", "z-ai/glm-5.3"),
            (137.7, "openrouter", "stealth/space-bunny-alpha"),
            (134.8, "nvidia", "moonshotai/kimi-k3")]
    got = [c[2] for c in A._rotate_within_run(pool, "w-new")]
    assert got == ["stealth/space-bunny-alpha", "moonshotai/kimi-k3"]


def test_it_never_rotates_onto_a_weak_model(monkeypatch):
    _run_with(monkeypatch, ["z-ai/glm-5.3", "stealth/space-bunny-alpha"])
    pool = [(138.0, "nvidia", "z-ai/glm-5.3"),
            (137.7, "openrouter", "stealth/space-bunny-alpha"),
            (120.0, "acme", "acme/mid-tier")]
    assert A._rotate_within_run(pool, "w-new") == pool          # all strong ones used


def test_a_session_outside_any_run_is_untouched(monkeypatch):
    monkeypatch.setattr(A.swarm_windows, "sibling_sessions", lambda sid: [])
    pool = [(138.0, "nvidia", "z-ai/glm-5.3")]
    assert A._rotate_within_run(pool, "lonely") == pool


def test_the_second_worker_opens_on_another_top_model(monkeypatch):
    _fleet(monkeypatch)
    monkeypatch.setattr(A, "_session_pin_get", lambda key: None)
    monkeypatch.setattr(A, "_pinned_elsewhere", lambda *a, **k: set())
    for first in ("z-ai/glm-5.3", "stealth/space-bunny-alpha"):
        _run_with(monkeypatch, [first])
        with A.app.test_request_context("/v1/chat/completions",
                                        environ_base={"flh.build_session": "w-new"}):
            pid, model, _d = A._route_by_difficulty(FIX, None, 1000, require_tools=True)
        assert model != first, (first, model)
        assert model in ("z-ai/glm-5.3", "stealth/space-bunny-alpha"), model


def test_siblings_are_found_from_a_worker_session():
    run = SW._Run("goal", ".", "opencode", SW.clean_phases({"phases": [
        {"title": "a", "task": "t"}, {"title": "b", "task": "t"}, {"title": "c", "task": "t"}]}))
    run.agents[0].session_id, run.agents[1].session_id = "s-a", "s-b"
    SW._remember(run)
    try:
        assert SW.sibling_sessions("s-b") == ["s-a"]
        assert SW.sibling_sessions("s-a") == ["s-b"]
        assert SW.sibling_sessions("nobody") == []
    finally:
        with SW._LOCK:
            SW._RUNS.pop(run.id, None)


def test_the_pick_rotates_before_it_spreads_and_remembers_the_model():
    src = open("app.py", encoding="utf-8").read()
    at = src.index("_pool = _rotate_within_run(_pool, _skey)")
    assert at < src.index("_pool = _spread_pool(_pool, _skey)", at)
    assert "_note_worker_model(_skey, model, pid)" in src[at:at + 600]
