"""Auto leads with the strongest model; a chosen orchestrator opens real turns.

MEASURED 2026-09-30, /agent session 47a25faa (opencode, coding): 22 of 24
turns were served by gemini-3.6/3.7-flash (134.1) while z-ai/glm-5.3 (138) and
stealth/space-bunny-alpha (137.7, the conversation's CHOSEN orchestrator) sat
unused. Three causes:

1. _weighted_pick drew the session's model from a pool of 14-55 candidates at
   temperature 5, so ~40 models at 130-134 outweighed the one at 138.
2. The tool chain put every _TOOL_PROVEN family (gemini-3) ahead of every
   stronger unproven model -- even a relay copy at 130 ahead of 138.
3. The chosen orchestrator carried a stale failure record (empty replies from
   before the reasoning-budget fix), so _build_chain never seeded it at hop 1
   and it could never earn a better record.
"""
import app as A


def test_auto_pick_stays_in_the_top_band(monkeypatch):
    monkeypatch.setattr(A, "_agentic_score", lambda c, *a, **k: c[0])
    pool = [(138.0, "nvidia", "z-ai/glm-5.3"),
            (137.9, "g4f", "srv_x:z-ai/glm-5.3")]
    pool += [(130.0 + i * 0.1, "p%d" % i, "m%d" % i) for i in range(40)]
    band = A._auto_top_band(pool)
    assert {c[2] for c in band} == {"z-ai/glm-5.3", "srv_x:z-ai/glm-5.3"}


def test_the_top_band_fails_open():
    assert A._auto_top_band([]) == []
    one = [(120.0, "p", "m")]
    assert A._auto_top_band(one) == one


def test_a_weaker_proven_family_never_leads_a_stronger_model():
    pool = [(134.1, "google", "models/gemini-3.7-flash"),
            (130.1, "g4f", "srv_y:models/gemini-3.8-flash"),
            (138.0, "nvidia", "z-ai/glm-5.3"),
            (137.7, "openrouter", "stealth/space-bunny-alpha"),
            (120.0, "acme", "acme/mid-tier")]
    lead = {c[2] for c in A._may_lead_pool(pool)}
    assert {"z-ai/glm-5.3", "stealth/space-bunny-alpha",
            "models/gemini-3.7-flash", "srv_y:models/gemini-3.8-flash"} <= lead
    assert "acme/mid-tier" not in lead          # unproven mid-tier still may not lead


def test_the_lead_pool_without_any_proven_model_is_the_old_gate():
    pool = [(138.0, "nvidia", "z-ai/glm-5.3"), (134.5, "acme", "acme/decent")]
    assert [c[2] for c in A._may_lead_pool(pool)] == ["z-ai/glm-5.3"]


def _fleet(monkeypatch, band=None):
    world = {"google": ["models/gemini-3.7-flash", "models/gemini-3.6-flash"],
             "g4f": ["srv_y:models/gemini-3.8-flash"],
             "nvidia": ["z-ai/glm-5.3"],
             "openrouter": ["stealth/space-bunny-alpha"],
             "acme": ["acme/mid-tier"]}
    scores = {"models/gemini-3.7-flash": 134.1, "models/gemini-3.6-flash": 134.1,
              "srv_y:models/gemini-3.8-flash": 130.1, "z-ai/glm-5.3": 138.0,
              "stealth/space-bunny-alpha": 137.7, "acme/mid-tier": 120.0}
    monkeypatch.setattr(A, "_available_providers", lambda *a, **k: list(world))
    monkeypatch.setattr(A, "_prefetch_auto_models", lambda pids: dict(world))
    monkeypatch.setattr(A, "_auto_models", lambda pid: list(world[pid]))
    monkeypatch.setattr(A, "_provider_capable", lambda pid, est: True)
    monkeypatch.setattr(A, "_supports_tools", lambda pid, m: True)
    monkeypatch.setattr(A, "_is_fast", lambda pid, m: True)
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: scores.get(m, 100.0))
    monkeypatch.setattr(A, "_active_mode", lambda: A.MODE_ALL)
    monkeypatch.setattr(A, "_is_model_dead", lambda pid, m: False)
    monkeypatch.setattr(A.quota, "is_model_throttled", lambda pid, m: False)
    monkeypatch.setattr(A.quota, "model_status", lambda pid, m: {"exhausted": False})
    monkeypatch.setattr(A, "_context_ok", lambda pid, m, est: True)
    monkeypatch.setattr(A, "_sub_available_providers", lambda: [])
    monkeypatch.setattr(A.config, "get_flag", lambda k, d=None: d)
    for name in ("_reliability_penalty", "_latency_penalty", "_answer_quality_penalty",
                 "_sustain_penalty"):
        monkeypatch.setattr(A, name, lambda *a, **k: 0.0)
    monkeypatch.setattr(A, "_chain_reliability_band", band or (lambda pid, m: 0))
    monkeypatch.setattr(A, "_below_declared_window", lambda pid, m: False)
    monkeypatch.setattr(A, "_is_low_quality", lambda m: False)
    return world


FIX = [{"role": "user", "content": "Read src/parse.py and fix the bug in the parser."}]


def test_the_chain_puts_stronger_models_ahead_of_gemini(monkeypatch):
    _fleet(monkeypatch)
    with A.app.test_request_context("/v1/chat/completions"):
        chain = A._build_chain("", "", 1000, require_tools=True, messages=FIX)
    order = [m for _, m in chain]
    first_gemini = min(order.index(m) for m in order if "gemini" in m)
    assert order.index("z-ai/glm-5.3") < first_gemini, order
    assert order.index("stealth/space-bunny-alpha") < first_gemini, order
    assert order.index("acme/mid-tier") > first_gemini, order   # proven beats weaker


def test_the_chosen_orchestrator_opens_the_turn_despite_an_old_record(monkeypatch):
    _fleet(monkeypatch, band=lambda p, m: 2 if m == "stealth/space-bunny-alpha" else 0)
    with A.app.test_request_context("/v1/chat/completions"):
        A.g.hub_orchestrator_pair = ("openrouter", "stealth/space-bunny-alpha")
        chain = A._build_chain("openrouter", "stealth/space-bunny-alpha", 1000,
                               require_tools=True, messages=FIX)
    assert chain[0] == ("openrouter", "stealth/space-bunny-alpha")


def test_a_router_pick_with_an_old_record_is_still_not_seeded(monkeypatch):
    _fleet(monkeypatch, band=lambda p, m: 2 if m == "stealth/space-bunny-alpha" else 0)
    with A.app.test_request_context("/v1/chat/completions"):
        chain = A._build_chain("openrouter", "stealth/space-bunny-alpha", 1000,
                               require_tools=True, messages=FIX)
    assert chain and chain[0] != ("openrouter", "stealth/space-bunny-alpha")


def _choose(monkeypatch, choice):
    monkeypatch.setattr(A, "_orch_effective", lambda key: (choice, "this conversation"))
    monkeypatch.setattr(A, "_orch_unusable", lambda *a, **k: None)


def test_a_simple_small_turn_goes_to_the_router_whatever_the_orchestrator(monkeypatch):
    _choose(monkeypatch, "openrouter/stealth/space-bunny-alpha")
    with A.app.test_request_context("/v1/chat/completions"):
        got = A._apply_orchestrator("groq", "qwen/qwen3.8-27b", [], 500, False, False,
                                    None, {}, diff="simple")
        assert got == ("groq", "qwen/qwen3.8-27b")
        assert getattr(A.g, "hub_orchestrator_pair", None) is None


def test_real_work_goes_to_the_chosen_orchestrator(monkeypatch):
    _choose(monkeypatch, "openrouter/stealth/space-bunny-alpha")
    for diff, est in (("medium", 500), ("hard", 500), ("simple", 60000)):
        with A.app.test_request_context("/v1/chat/completions"):
            got = A._apply_orchestrator("groq", "qwen/qwen3.8-27b", [], est, True, False,
                                        None, {}, diff=diff)
            assert got == ("openrouter", "stealth/space-bunny-alpha"), (diff, est)
            assert A.g.hub_orchestrator_pair == got
