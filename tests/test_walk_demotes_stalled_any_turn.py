"""A stalled provider is stepped over on EVERY turn, not only trivial/tool ones.

MEASURED 2026-10-25 (hub.log): a tool-free codex "continue" (est 6853) walked
nvidia -> nvidia (a sibling model behind the same slow gateway), spent the whole
240 s deadline on two ReadTimeouts and 504'd -- never reaching inception/mercury-3
which was answering in the same minutes. The stall-demotion in `_ChainClock.walk`
only fired on `trivial or tools` turns, so a non-trivial tool-free turn kept
re-picking the stalled provider's siblings.

The fix (flag `walk_demote_stalled_any`, default on) makes the walk step to a
DIFFERENT provider's model once any provider has stalled THIS walk, on every turn.
It only REORDERS (the stalled pair still runs after the others) and only after a
real stall, so before any stall the walk is byte-for-byte as built. It lives in
the shared `_ChainClock.walk`, so codex (/v1/responses), opencode
(/v1/chat/completions) and Claude Code (/v1/messages) all get it.
"""
import app as A


def _drive_after_stall(clock, chain):
    """Yield hop 1, mark its provider stalled, return hop 2 (what the walk
    picks next). None if the walk ended."""
    it = clock.walk(list(chain))
    first = next(it)
    clock._note_stall(first[0])      # the provider stalled this hop (see walk)
    try:
        second = next(it)
    except StopIteration:
        second = None
    return first, second


def test_tool_free_nontrivial_turn_steps_over_the_stalled_provider():
    # The live shape: a non-trivial, tool-free request (codex "continue"),
    # nvidia twice then a working provider.
    chain = [("nvidia", "a"), ("nvidia", "b"), ("inception", "mercury-3")]
    with A.app.test_request_context("/v1/responses"):
        clock = A._ChainClock(tools=False, est=6853)  # trivial defaults False
        assert clock._demote_stalled_any is True
        first, second = _drive_after_stall(clock, chain)
        assert first == ("nvidia", "a")
        # The nvidia SIBLING is skipped; the working provider is reached next.
        assert second == ("inception", "mercury-3")


def test_flag_off_is_the_old_behaviour(monkeypatch):
    # Negative control: with the flag off the tool-free walk keeps re-picking
    # the stalled provider's sibling, exactly as before this change.
    monkeypatch.setattr(A.config, "get_flag",
                        lambda name, default=None:
                        False if name == "walk_demote_stalled_any" else default)
    chain = [("nvidia", "a"), ("nvidia", "b"), ("inception", "mercury-3")]
    with A.app.test_request_context("/v1/responses"):
        clock = A._ChainClock(tools=False, est=6853)
        assert clock._demote_stalled_any is False
        first, second = _drive_after_stall(clock, chain)
        assert first == ("nvidia", "a")
        assert second == ("nvidia", "b")      # the old gate: no demotion


def test_no_stall_means_the_chain_is_untouched():
    # Before any stall, a tool-free non-trivial walk yields the chain in order.
    chain = [("nvidia", "a"), ("inception", "mercury-3"), ("dahl", "c")]
    with A.app.test_request_context("/v1/responses"):
        clock = A._ChainClock(tools=False, est=6853)
        assert list(clock.walk(list(chain))) == chain


def test_trivial_and_tool_turns_still_demote():
    # The paths that already demoted keep doing so (the fix only ADDS the
    # tool-free non-trivial case).
    chain = [("nvidia", "a"), ("nvidia", "b"), ("inception", "mercury-3")]
    for kw in ({"trivial": True, "tools": False, "est": 500},
               {"trivial": False, "tools": True, "est": 6853}):
        with A.app.test_request_context("/v1/responses"):
            clock = A._ChainClock(**kw)
            first, second = _drive_after_stall(clock, chain)
            assert first == ("nvidia", "a")
            assert second == ("inception", "mercury-3")
