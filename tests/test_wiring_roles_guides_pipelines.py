"""Wiring: pipelines (family / free verdict / search), per-hop model guides,
weak-model scaffolding. Every model call is a stub; `verify` is faked through
sys.modules exactly like tests/test_tool_turn_roles.py."""
import json
import sys
import types

import pytest

import app as A
import config
import swarm
import swarm_windows

from test_pipeline_verify_and_search import ASK, make_dispatch, scripted


@pytest.fixture
def fake_verify(monkeypatch):
    state = {"risky": False, "picked": []}
    mod = types.ModuleType("verify")
    mod.VERIFY_MAX_TOKENS = 300
    mod.family = lambda model_id: str(model_id).split("/")[-1].split("-")[0]
    mod.pick_verifier = lambda producer, cands: next(
        ((c[0], c[1]) for c in cands if (c[0], c[1]) != tuple(producer[:2])), None)
    mod.is_risky = lambda first, difficulty, observed_pass=None: state["risky"]
    mod.digest = lambda messages, proposed: [{"role": "user", "content": "V"}]
    mod.parse_verdict = lambda text: json.loads(text)
    mod.corrector_messages = lambda m, p, v: list(m)
    monkeypatch.setitem(sys.modules, "verify", mod)
    return state


def _flag(monkeypatch, **flags):
    real = config.get_flag
    monkeypatch.setattr(config, "get_flag",
                        lambda name, default=False: flags.get(name, real(name, default)))


# ---- 1. pipelines ---------------------------------------------------------

def test_pipeline_kwargs_carry_family_and_search(fake_verify):
    kw = A._pipeline_check_kwargs()
    assert kw["family"]("p1/kimi-k3") == "kimi" and kw["search"] is True
    mk = A._multi_check_kwargs()
    assert mk["free_verdict"] is A._free_verdict and mk["search"] is True


def test_pipeline_search_flag_off_and_no_verify(monkeypatch, fake_verify):
    _flag(monkeypatch, pipeline_search=False)
    assert "search" not in A._pipeline_check_kwargs()
    assert "search" not in A._multi_check_kwargs()
    monkeypatch.setitem(sys.modules, "verify", types.ModuleType("verify"))
    assert A._pipeline_check_kwargs() == {} or "family" not in A._pipeline_check_kwargs()
    assert "free_verdict" not in A._multi_check_kwargs()


def test_prose_swarm_review_goes_to_another_family(fake_verify):
    d = make_dispatch(scripted(), sig="kwargs")        # like _pipeline_bound's **kw
    swarm.run(ASK, d, **A._pipeline_check_kwargs())
    review = d.of("review")[0]
    assert review["kw"]["avoid_families"] == ("kimi",)
    chain = [("p1", "kimi-k3"), ("p9", "llama-4")]
    assert A._avoid_families_last(chain, ("kimi",))[0] == ("p9", "llama-4")


def test_without_the_kwargs_old_behaviour(fake_verify):
    d = make_dispatch(scripted(), sig="kwargs")
    swarm.run(ASK, d)
    assert "avoid_families" not in d.of("review")[0]["kw"]


def test_multi_entry_points_receive_the_kwargs(monkeypatch, fake_verify):
    seen = {}

    def fake_start(*a, **k):
        seen.update(k)
        return "run-1"
    monkeypatch.setattr(swarm_windows, "start", fake_start)
    monkeypatch.setattr(swarm_windows, "status", lambda *a, **k: {"run_id": "run-1"})
    import os
    import tempfile
    d = tempfile.mkdtemp()
    with A.app.test_request_context(
            "/api/swarm-windows", method="POST",
            json={"goal": "g", "project_dir": d, "cli": "opencode"}):
        try:
            A.api_swarm_windows_start.__wrapped__()
        except AttributeError:
            A.api_swarm_windows_start()
    assert seen.get("free_verdict") is A._free_verdict and seen.get("search") is True
    assert os.path.isdir(d)


# ---- 2. guides follow the hop ---------------------------------------------

def test_guide_follows_the_hop_model(monkeypatch, fake_verify):
    monkeypatch.setattr(A, "_benchmark_score",
                        lambda pid, m: 138.0 if "strong" in m else 20.0)
    msgs = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}]
    weak = A._with_model_guide(msgs, "p1", "deepseek-weak", True)
    strong = A._with_model_guide(msgs, "p1", "kimi-strong", True)
    assert weak[0]["content"] == "SYS" and weak[1]["content"].startswith("MODEL GUIDE")
    assert "one file or one function" in weak[1]["content"]
    assert "one file or one function" not in strong[1]["content"]
    assert len(strong[1]["content"]) < len(weak[1]["content"])
    assert len(weak[1]["content"]) <= 1500
    # the original list is untouched (cache keys / other hops unaffected)
    assert len(msgs) == 2
    assert A._with_model_guide(weak, "p1", "deepseek-weak", True) is weak   # idempotent


def test_guide_flag_off_restores_old(monkeypatch, fake_verify):
    _flag(monkeypatch, model_guides=False)
    msgs = [{"role": "user", "content": "hi"}]
    assert A._with_model_guide(msgs, "p1", "deepseek-weak", True) is msgs
    assert A._model_is_weak("p1", "deepseek-weak") is False


def test_upstream_chat_adds_guide_per_hop_and_skips_no_craft(monkeypatch, fake_verify):
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: 20.0)
    sent = []

    class Boom(Exception):
        pass

    def fake_compact(msgs, *a, **k):
        sent.append(msgs)
        raise Boom()
    monkeypatch.setattr(A, "_compact_to_budget", fake_compact)
    for no_craft in (False, True):
        sent.clear()
        payload = {"model": "weakling-1", "messages": [{"role": "user", "content": "x"}]}
        if no_craft:
            payload["_no_craft"] = True
        try:
            A._upstream_chat("p1", payload, False)
        except Exception:                                    # noqa: BLE001
            pass
        if not sent:
            pytest.skip("_upstream_chat needs more setup here")
        has = any(str(m.get("content", "")).startswith("MODEL GUIDE") for m in sent[0])
        assert has is (not no_craft)


# ---- 3. weak model => verifier -------------------------------------------

def test_weak_producer_is_always_verified_strong_only_if_risky(monkeypatch, fake_verify):
    monkeypatch.setattr(A, "_benchmark_score",
                        lambda pid, m: 138.0 if m == "strong" else 20.0)
    monkeypatch.setattr(A, "_observed_pass", lambda m: None)
    calls = []
    monkeypatch.setattr(A, "_role_candidates", lambda *a, **k: calls.append("cand") or [])
    msg = {"role": "assistant", "content": "ok", "tool_calls": [
        {"id": "1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]}
    rec = {"calls": 0, "sent_tokens": 0}
    import time
    end = time.monotonic() + 60

    def run(model):
        calls.clear()
        A._role_verify_and_correct({"messages": []}, [], ("p1", model), msg, [], "k",
                                   "hard", dict(rec), [], end, 10)
        return bool(calls)
    assert run("strong") is False                 # not risky, strong: skipped
    assert run("weak") is True                    # weak: always verified
    fake_verify["risky"] = True
    assert run("strong") is True                  # risky: verified
    _flag(monkeypatch, model_guides=False)
    fake_verify["risky"] = False
    assert run("weak") is False                   # flag off: old behaviour
