"""Two fixes for one report: "Failed 12m 3s claude produced no reply."

1. Claude Code's own CLI flag ALWAYS wins over the ANTHROPIC_MODEL=auto env
   var the hub's isolated-mode hub-fallback sets -- agentic_chat.py passes
   `--model opus` (its _MODEL_ALIAS) on every invocation. _is_orchestrate
   only recognised "auto"/"orchestrate"/"default"/"claude*", so a bare
   "opus" was treated as a literal model id, resolved to a nonexistent
   ('groq', 'opus'), and /v1/messages hung with no reply for the entire
   turn. Fixed by teaching _is_orchestrate Claude Code's own short aliases.

2. Separately, chasing the SAME report's cold-cache first-routing-decision
   delay: _route_by_difficulty called _auto_models(pid) once per provider,
   SEQUENTIALLY, and each call can be a live network fetch on a cold/expired
   60s cache -- measured ~20-30s across ~20 providers. _prefetch_auto_models
   runs those fetches concurrently instead, bounding the cold-cache cost by
   the SLOWEST single provider rather than their sum.
"""
import time

import app


def test_claude_code_short_aliases_are_recognized_as_orchestrate():
    """--model opus/sonnet/haiku/opusplan is Claude Code's OWN alias for
    "let the CLI's configured router decide", not a literal hub model id."""
    for alias in ("opus", "sonnet", "haiku", "opusplan"):
        assert app._is_orchestrate(alias) is True
        assert app._is_orchestrate(alias.upper()) is True
        assert app._is_orchestrate(f"  {alias}  ") is True


def test_provider_qualified_model_is_never_orchestrate_even_if_alias_like():
    """A real "pid/model" selection must never be reinterpreted as
    orchestration just because the model half looks like an alias."""
    assert app._is_orchestrate("openrouter/opus") is False
    assert app._is_orchestrate("groq/sonnet") is False


def test_unrelated_literal_model_ids_are_still_not_orchestrate():
    assert app._is_orchestrate("llama-3.1-70b-versatile") is False
    assert app._is_orchestrate("gpt-oss-120b") is False


def test_existing_orchestrate_triggers_still_work():
    assert app._is_orchestrate("") is True
    assert app._is_orchestrate(None) is True
    assert app._is_orchestrate("auto") is True
    assert app._is_orchestrate("orchestrate") is True
    assert app._is_orchestrate("default") is True
    assert app._is_orchestrate("claude-3-5-sonnet") is True


def test_prefetch_matches_shape_of_sequential_auto_models(monkeypatch):
    calls = []

    def fake_auto_models(pid):
        calls.append(pid)
        return [f"{pid}-model-a", f"{pid}-model-b"]

    monkeypatch.setattr(app, "_auto_models", fake_auto_models)
    providers = ["groq", "openrouter", "cerebras"]

    result = app._prefetch_auto_models(providers)

    assert result == {
        "groq": ["groq-model-a", "groq-model-b"],
        "openrouter": ["openrouter-model-a", "openrouter-model-b"],
        "cerebras": ["cerebras-model-a", "cerebras-model-b"],
    }
    assert sorted(calls) == sorted(providers)


def test_prefetch_runs_concurrently_not_sequentially(monkeypatch):
    """The whole point of the fix: N providers each costing ~0.2s must finish
    in close to 0.2s total, not N * 0.2s -- proves they overlap in time."""
    SLEEP = 0.2
    providers = ["p1", "p2", "p3", "p4", "p5", "p6", "p7", "p8"]

    def slow_auto_models(pid):
        time.sleep(SLEEP)
        return [pid]

    monkeypatch.setattr(app, "_auto_models", slow_auto_models)

    start = time.monotonic()
    result = app._prefetch_auto_models(providers)
    elapsed = time.monotonic() - start

    assert all(result[pid] == [pid] for pid in providers)
    # Sequential would take len(providers) * SLEEP (1.6s here). A generous
    # cutoff well under that, comfortably above SLEEP alone, absorbs CI/
    # thread-pool scheduling jitter without masking a regression to serial.
    assert elapsed < SLEEP * len(providers) / 2, (
        f"prefetch took {elapsed:.2f}s for {len(providers)} providers at "
        f"{SLEEP}s each -- looks sequential, not concurrent")


def test_prefetch_empty_providers_returns_empty_dict():
    assert app._prefetch_auto_models([]) == {}


def test_prefetch_a_provider_that_raises_contributes_empty_list_not_a_crash(monkeypatch):
    def flaky_auto_models(pid):
        if pid == "broken":
            raise RuntimeError("simulated provider /models fetch failure")
        return [pid + "-ok"]

    monkeypatch.setattr(app, "_auto_models", flaky_auto_models)

    result = app._prefetch_auto_models(["good", "broken"])

    assert result["good"] == ["good-ok"]
    assert result["broken"] == []


def test_route_by_difficulty_uses_the_prefetch_helper():
    import inspect
    src = inspect.getsource(app._route_by_difficulty)
    assert "_prefetch_auto_models" in src
