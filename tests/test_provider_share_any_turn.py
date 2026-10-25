"""One provider may not eat the whole deadline on a tool-FREE turn either.

MEASURED 2026-10-25 (hub.log, RESPONSES-DEADLINE, codex): tool-free streamed
turns 504'd with ONE hop holding almost the whole 240 s clock --
"g4f: _HopBudgetExceeded no answer within 233s" then inception/mercury-3 (a
fast, working provider) got the last seconds or none. `_ChainClock._provider_cap`
(one provider may spend at most _TOOL_PROVIDER_SHARE of the deadline while
another provider still waits) only applied to TOOL turns.

Flag `provider_share_any_turn` (default on) applies the same share to every
turn. Off = the old tool-only cap, byte for byte.
"""
import app as A


def _first_hop_budget(clock, chain):
    it = clock.walk(list(chain))
    pid, model = next(it)[:2]
    return pid, clock._budget_for(pid, model, stream=True)


def test_tool_free_turn_caps_one_provider_while_another_waits():
    chain = [("g4f", "srv_x:z-ai/glm-5.3"), ("inception", "mercury-3")]
    with A.app.test_request_context("/v1/responses"):
        clock = A._ChainClock(tools=False, est=11361, stream=True)
        assert clock.limit, "the test needs a request deadline"
        pid, budget = _first_hop_budget(clock, chain)
        assert pid == "g4f"
        cap = max(A._TOOL_PROVIDER_MIN_SECONDS,
                  A._TOOL_PROVIDER_SHARE * float(clock.limit))
        assert budget is not None and budget <= cap + 0.01
        # ... and so the next provider still has real time left.
        assert clock.left() - budget >= A._TOOL_PROVIDER_MIN_HOP


def test_the_last_provider_gets_what_is_left():
    # Nobody else waits: no cap, the hop may use the rest of the clock.
    chain = [("inception", "mercury-3")]
    with A.app.test_request_context("/v1/responses"):
        clock = A._ChainClock(tools=False, est=11361, stream=True)
        _, budget = _first_hop_budget(clock, chain)
        assert budget >= clock.left() - 1.0


def test_flag_off_is_the_old_tool_only_cap(monkeypatch):
    monkeypatch.setattr(A.config, "get_flag",
                        lambda name, default=None:
                        False if name == "provider_share_any_turn" else default)
    chain = [("g4f", "srv_x:z-ai/glm-5.3"), ("inception", "mercury-3")]
    with A.app.test_request_context("/v1/responses"):
        clock = A._ChainClock(tools=False, est=11361, stream=True)
        _, budget = _first_hop_budget(clock, chain)
        assert budget >= clock.left() - 1.0      # whole clock, as before


def test_tool_turns_keep_their_cap():
    chain = [("g4f", "srv_x:z-ai/glm-5.3"), ("inception", "mercury-3")]
    with A.app.test_request_context("/v1/responses"):
        clock = A._ChainClock(tools=True, est=11361, stream=True)
        _, budget = _first_hop_budget(clock, chain)
        cap = max(A._TOOL_PROVIDER_MIN_SECONDS,
                  A._TOOL_PROVIDER_SHARE * float(clock.limit))
        assert budget <= cap + 0.01
