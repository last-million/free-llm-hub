"""One relay backend that keeps failing is not walked five times in one request.

MEASURED 2026-10-18 17:17 (hub.log RESPONSES-503, no tools, 12 hops): five
hops went to the same g4f backend srv_mr9dda21bf67b4c62086 (opus-5.5,
sonnet-5.5, fable-5.1, a community opus, opus-4.7); each answered 200 with the
backend's own "API key ... has reached its budget" notice. Any non-tool walk now
drops a relay server after two failed hops in that walk. Hermetic: the real
_ChainClock.walk, hops opened and failed by hand, no upstream call.
"""
import pytest

import app as A

SRV = "srv_mr9dda21bf67b4c62086"
CHAIN = [
    ("g4f", SRV + ":anthropic/claude-opus-5.5"),
    ("g4f", SRV + ":anthropic/claude-sonnet-5.5"),
    ("g4f", SRV + ":anthropic/claude-fable-5.1"),
    ("g4f", SRV + ":anthropic/claude-opus-4.7"),
    ("g4f", "srv_mp1v9cyha31b95fa8c9a:z-ai/glm-5.3"),
    ("glm", "glm-4.7-flash"),
]


def _walk_all_failing(chain, tools=False):
    """Every hop the walk yields fails; returns the hops in order."""
    taken = []
    with A.app.test_request_context():
        clock = A._ChainClock(tools=tools)
        for pid, model in clock.walk(list(chain)):
            taken.append((pid, model))
            clock._open_hop(pid, model, True)
    return taken


def test_a_failing_backend_gets_two_hops_then_the_walk_moves_on():
    taken = _walk_all_failing(CHAIN)
    same = [m for p, m in taken if m.startswith(SRV)]
    assert len(same) == 2
    assert taken[2:] == CHAIN[4:]          # another relay backend, then glm


def test_one_failure_does_not_drop_the_backend():
    chain = [CHAIN[0], ("glm", "glm-4.7-flash"), CHAIN[1]]
    assert _walk_all_failing(chain) == chain


def test_a_non_relay_provider_is_never_dropped():
    chain = [("nvidia", "a"), ("nvidia", "b"), ("nvidia", "c")]
    assert _walk_all_failing(chain) == chain


def test_when_only_that_backend_is_left_it_is_still_walked():
    assert _walk_all_failing(CHAIN[:4]) == CHAIN[:4]


def test_the_switch_turns_it_off(monkeypatch):
    real = A.config.get_flag
    monkeypatch.setattr(A.config, "get_flag",
                        lambda n, d=None: False if n == "relay_walk_skip" else real(n, d))
    assert _walk_all_failing(CHAIN) == CHAIN


def test_tool_walks_keep_their_own_one_strike_rule():
    taken = _walk_all_failing(CHAIN, tools=True)
    assert len([m for p, m in taken if m.startswith(SRV)]) == 1


def test_the_server_id_matches_the_incident_spellings():
    assert A._relay_server_id("g4f", SRV + ":anthropic/claude-opus-5.5") == "g4f|" + SRV
    assert A._relay_server_id("glm", "glm-4.7-flash") is None
