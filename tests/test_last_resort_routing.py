"""Last-resort chain ordering (nemotron/gpt-oss/gemma tail), kimi-k2.6/k2.7
preference floors, adaptive first-content peek timeout, and Codex-sized
request capability of the strong providers' TPM rows.

Root cause these pin down (measured in the 2026-07-27 RESPONSES-503 logs): a
real Artificial Analysis score re-inflates the Tier-C-demoted families
(nemotron-3-ultra ~104, gpt-oss-120b ~99) and _TOOL_PROVEN still names them,
so SCORES alone walked every agentic chain straight onto nemotron/gpt-oss
while glm-4.7 / kimi-k2.6 / kimi-k2.7-code sat alive. The fix is an ORDERING
rule (_LOW_QUALITY_RE partition), not a score change — these tests therefore
also cover the case where a demoted family out-scores every strong model.
"""
import shutil
import tempfile

import pytest

import app
import config  # noqa: F401  (env isolation fixture touches its path convention)
import quota


@pytest.fixture
def state_dir():
    # NB: not pytest's tmp_path — this machine's default pytest basetemp is
    # permission-denied (the suite's known environmental errors), so tests here
    # allocate their own temp dirs like the hub-pytest-* runs did.
    d = tempfile.mkdtemp(prefix="hub-pytest-lastresort-")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def fresh_quota():
    """Run a test against EMPTY quota state with persistence detached, then put
    the module's real state back exactly as it was."""
    saved = (dict(quota._STATE), dict(quota._MODEL_STATE),
             dict(quota._MODEL_THROTTLE), dict(quota._DYNAMIC),
             quota._PERSIST_PATH, quota._persist_last)
    quota._STATE.clear()
    quota._MODEL_STATE.clear()
    quota._MODEL_THROTTLE.clear()
    quota._DYNAMIC.clear()
    quota._PERSIST_PATH = None
    quota._persist_last = 0.0
    try:
        yield
    finally:
        quota._STATE.clear()
        quota._STATE.update(saved[0])
        quota._MODEL_STATE.clear()
        quota._MODEL_STATE.update(saved[1])
        quota._MODEL_THROTTLE.clear()
        quota._MODEL_THROTTLE.update(saved[2])
        quota._DYNAMIC.clear()
        quota._DYNAMIC.update(saved[3])
        quota._PERSIST_PATH = saved[4]
        quota._persist_last = saved[5]


# The live 2026-07-31 availability picture (morph exhausted; strong models on
# cerebras / g4f-nvidia / cloudflare / opencode-zen; nemotron on five
# providers, gpt-oss on four).
LIVE_MODELS = {
    "cerebras": ["zai-glm-4.7"],
    "g4f-nvidia": ["moonshotai/kimi-k2.6", "nvidia/nemotron-3-ultra-550b-a55b"],
    "cloudflare": ["@cf/moonshotai/kimi-k2.6", "@cf/moonshotai/kimi-k2.7-code",
                   "@cf/openai/gpt-oss-120b"],
    "opencode-zen": ["mimo-v2.5-free"],
}


@pytest.fixture
def live_providers(fresh_quota, monkeypatch):
    """Route against the live-facts provider set with all learned/dead/throttle
    state neutralized and NO Artificial Analysis override (the AA-inflated case
    gets its own test with an explicit score map)."""
    monkeypatch.setattr(app, "_available_providers", lambda: list(LIVE_MODELS))
    monkeypatch.setattr(app, "_auto_models", lambda pid: list(LIVE_MODELS.get(pid, ())))
    monkeypatch.setattr(app, "_sub_available_providers", lambda: [])
    monkeypatch.setattr(app, "_is_model_dead", lambda pid, m: False)
    monkeypatch.setattr(app, "_context_ok", lambda pid, m, est: True)
    monkeypatch.setattr(app.prov, "is_model_allowed", lambda m: True)
    monkeypatch.setattr(app, "_aa_scores", {})
    app._session_pins.clear()
    yield
    app._session_pins.clear()


def _msgs(text):
    return [{"role": "user", "content": text}]


_HARD_TEXT = (
    "refactor the whole routing chain, then write code for comprehensive "
    "tests, debug any failures, and optimize performance " + "x" * 2000
)
_MEDIUM_TEXT = (
    "Here is a function:\n```\ndef rotate(keys):\n"
    "    return keys[1:] + keys[:1]\n```\n"
    "Can you explain what it does and whether it handles empty input? "
    + "detail " * 60
)


def _lowq_positions(chain):
    return [i for i, (_pid, m) in enumerate(chain) if app._is_low_quality(m)]


def _assert_lowq_tail(chain):
    """Every last-resort hop sits AFTER every normal hop, and the chain's own
    tail is a last-resort family (they are ordered last, never deleted)."""
    lowq = _lowq_positions(chain)
    assert lowq, "last-resort families must stay IN the chain (tail), not be deleted"
    normal = [i for i in range(len(chain)) if i not in lowq]
    assert max(normal) < min(lowq), "a demoted family outranks a normal candidate: %r" % (chain,)
    assert app._is_low_quality(chain[-1][1])


# --------------------------------------------------------------------------- #
# FIX 1 — chain partition
# --------------------------------------------------------------------------- #

def test_tool_chain_puts_nemotron_gptoss_behind_strong_models(live_providers):
    """The exact live incident: glm-4.7 + kimi-k2.6/k2.7-code + mimo alive, a
    tool (Codex) request — nemotron/gpt-oss must not appear before ANY of them
    even though _TOOL_PROVEN still names nemotron/gpt-oss."""
    chain = app._build_chain("cerebras", "zai-glm-4.7", est=30000,
                             require_tools=True, messages=_msgs("build it"))
    ids = [m for _pid, m in chain]
    assert any("kimi-k2.6" in m or "kimi-k2.7" in m for m in ids)
    _assert_lowq_tail(chain)
    first_lowq = min(_lowq_positions(chain))
    for strong in ("kimi-k2.6", "kimi-k2.7-code", "glm-4.7", "mimo"):
        hits = [i for i, m in enumerate(ids) if strong in m]
        assert hits and max(hits) < first_lowq, strong


def test_non_tool_chain_also_partitions(live_providers):
    chain = app._build_chain("cerebras", "zai-glm-4.7", est=30000,
                             require_tools=False, messages=_msgs("hello"))
    _assert_lowq_tail(chain)


def test_partition_wins_even_when_low_quality_outscores(live_providers, monkeypatch):
    """The AA-override case: nemotron/gpt-oss re-inflated ABOVE every strong
    model (measured ~104/~99 live). Ordering must still hold — this is why the
    fix is a partition, not a score edit."""
    inflated = {
        ("g4f-nvidia", "nvidia/nemotron-3-ultra-550b-a55b"): 200.0,
        ("cloudflare", "@cf/openai/gpt-oss-120b"): 195.0,
        ("g4f-nvidia", "moonshotai/kimi-k2.6"): 100.0,
        ("cloudflare", "@cf/moonshotai/kimi-k2.7-code"): 100.0,
        ("cerebras", "zai-glm-4.7"): 90.0,
        ("opencode-zen", "mimo-v2.5-free"): 90.0,
    }
    monkeypatch.setattr(app, "_benchmark_score",
                        lambda pid, m: inflated.get((pid, m), 10.0))
    chain = app._build_chain("cerebras", "zai-glm-4.7", est=30000,
                             require_tools=True, messages=_msgs("build it"))
    _assert_lowq_tail(chain)


def test_last_resort_serves_when_nothing_else_alive(fresh_quota, monkeypatch):
    """Fail-open: with ONLY demoted families keyed, they still serve (and a
    tool request still gets a chain) — demoted, never deleted."""
    only_low = {"nvidia": ["nvidia/nemotron-3-ultra-550b-a55b"],
                "cloudflare": ["@cf/openai/gpt-oss-120b"]}
    monkeypatch.setattr(app, "_available_providers", lambda: list(only_low))
    monkeypatch.setattr(app, "_auto_models", lambda pid: list(only_low.get(pid, ())))
    monkeypatch.setattr(app, "_sub_available_providers", lambda: [])
    monkeypatch.setattr(app, "_is_model_dead", lambda pid, m: False)
    monkeypatch.setattr(app, "_context_ok", lambda pid, m, est: True)
    monkeypatch.setattr(app.prov, "is_model_allowed", lambda m: True)
    monkeypatch.setattr(app, "_aa_scores", {})
    app._session_pins.clear()
    chain = app._build_chain("nvidia", "nvidia/nemotron-3-ultra-550b-a55b",
                             est=30000, require_tools=True, messages=_msgs("hi"))
    assert len(chain) >= 2
    pid, model, _diff = app._route_by_difficulty(_msgs("hi"), est=30000,
                                                 require_tools=True)
    assert pid is not None and app._is_low_quality(model)


def test_tool_primary_prefers_unproven_strong_over_proven_low_quality(
        live_providers, monkeypatch):
    """A tool request's PRIMARY must be a strong model even when the only
    tool-PROVEN candidates are nemotron/gpt-oss (the pre-fix cascade: proven-
    first made the demoted family the primary while glm-4.7 sat alive)."""
    inflated = {
        ("g4f-nvidia", "nvidia/nemotron-3-ultra-550b-a55b"): 120.0,  # proven
        ("cloudflare", "@cf/openai/gpt-oss-120b"): 119.0,            # proven
        ("cerebras", "zai-glm-4.7"): 110.0,                          # unproven
    }
    monkeypatch.setattr(app, "_benchmark_score",
                        lambda pid, m: inflated.get((pid, m), 10.0))
    pid, model, _diff = app._route_by_difficulty(_msgs("build the feature"), est=30000,
                                                 require_tools=True)
    assert (pid, model) == ("cerebras", "zai-glm-4.7")


# --------------------------------------------------------------------------- #
# FIX 1 — difficulty routing: simple MAY use them, medium/hard may not
# --------------------------------------------------------------------------- #

def test_simple_may_route_to_last_resort(live_providers, monkeypatch):
    monkeypatch.setattr(app, "_is_fast", lambda pid, m: True)
    pid, model, diff = app._route_by_difficulty(_msgs("hi"))
    assert diff == "simple"
    # cheapest model above the simple floor — the demoted family qualifies.
    assert app._is_low_quality(model)


def test_medium_prefers_strong_even_when_low_quality_is_cheaper(
        live_providers, monkeypatch):
    monkeypatch.setattr(app, "_is_fast", lambda pid, m: True)
    scores = {
        ("g4f-nvidia", "nvidia/nemotron-3-ultra-550b-a55b"): 80.0,  # cheapest >= floor
        ("cerebras", "zai-glm-4.7"): 100.0,
    }
    monkeypatch.setattr(app, "_benchmark_score",
                        lambda pid, m: scores.get((pid, m), 10.0))
    pid, model, diff = app._route_by_difficulty(_msgs(_MEDIUM_TEXT))
    assert diff == "medium"
    assert (pid, model) == ("cerebras", "zai-glm-4.7")


def test_hard_prefers_strong_even_when_low_quality_outscores(
        live_providers, monkeypatch):
    monkeypatch.setattr(app, "_is_fast", lambda pid, m: True)
    scores = {
        ("g4f-nvidia", "nvidia/nemotron-3-ultra-550b-a55b"): 200.0,
        ("cerebras", "zai-glm-4.7"): 100.0,
    }
    monkeypatch.setattr(app, "_benchmark_score",
                        lambda pid, m: scores.get((pid, m), 10.0))
    pid, model, diff = app._route_by_difficulty(_msgs(_HARD_TEXT))
    assert diff == "hard"
    assert (pid, model) == ("cerebras", "zai-glm-4.7")


# --------------------------------------------------------------------------- #
# FIX 2 — kimi-k2.6 / kimi-k2.7 preference floor across provider id shapes
# --------------------------------------------------------------------------- #

def test_kimi_k26_k27_floor_across_id_shapes(fresh_quota, monkeypatch):
    monkeypatch.setattr(app, "_aa_scores", {})
    floor = app._PREF_FLOORS[4]
    assert floor == 133
    # g4f-nvidia 'moonshotai/<id>' shape and the bare id: full floor.
    assert app._benchmark_score("g4f-nvidia", "moonshotai/kimi-k2.6") == floor
    assert app._benchmark_score("cerebras", "kimi-k2.6") == floor
    # cloudflare '@cf/moonshotai/<id>' shape: floor minus the cloudflare
    # shared-budget penalty (its 10k neurons/day allowance) — still top-band.
    cf = app._benchmark_score("cloudflare", "@cf/moonshotai/kimi-k2.6")
    assert cf == floor - 12
    assert app._benchmark_score("cloudflare", "@cf/moonshotai/kimi-k2.7-code") == floor - 12
    # The floor sits just under kimi-k3 and above every natural strong score.
    assert app._benchmark_score("morph", "morph-kimik3") == app._PREF_FLOORS[1] > floor
    assert floor > app._benchmark_score("cerebras", "zai-glm-4.7")


# --------------------------------------------------------------------------- #
# FIX 3a — adaptive first-content peek timeout
# --------------------------------------------------------------------------- #

def test_peek_timeout_fast_model_small_request_stays_short():
    assert app._stream_peek_timeout("llama-3.3-70b-versatile", 400) \
        == app.STREAM_CONTENT_PEEK_TIMEOUT


def test_peek_timeout_slow_model_gets_long_budget():
    assert app._stream_peek_timeout("gpt-oss-120b", 400) == app.STREAM_SLOW_PEEK_TIMEOUT
    assert app._stream_peek_timeout("deepseek-r1", 400) == app.STREAM_SLOW_PEEK_TIMEOUT


def test_peek_timeout_big_request_gets_long_budget():
    assert app._stream_peek_timeout("llama-3.3-70b-versatile", 30000) \
        == app.STREAM_SLOW_PEEK_TIMEOUT


def test_peek_timeout_slow_model_and_big_request_gets_longest():
    assert app._stream_peek_timeout("gpt-oss-120b", 30000) \
        == app.STREAM_SLOW_BIG_PEEK_TIMEOUT
    assert app._stream_peek_timeout("deepseek-r1", 30000) \
        == app.STREAM_SLOW_BIG_PEEK_TIMEOUT


# --------------------------------------------------------------------------- #
# FIX 3c — a Codex-sized request fits the strong providers' TPM rows
# --------------------------------------------------------------------------- #

def test_codex_sized_request_admitted_on_strong_providers():
    """~30K tokens + tool schemas (a real Codex turn) must NOT be prefiltered
    off cerebras / g4f-nvidia / cloudflare by _provider_capable."""
    big_system = "you are codex. " * 8000              # ~104K chars
    tools = [{"type": "function", "function": {
        "name": "apply_patch",
        "description": "apply a patch " * 400,
        "parameters": {"type": "object",
                       "properties": {"input": {"type": "string",
                                                "description": "x " * 4000}}},
    }}]
    est = app._est_tokens([{"role": "system", "content": big_system},
                           {"role": "user", "content": "fix the bug"}], tools)
    assert 25000 <= est <= 60000, est
    for pid in ("cerebras", "g4f-nvidia", "cloudflare"):
        assert app._provider_capable(pid, est), pid
