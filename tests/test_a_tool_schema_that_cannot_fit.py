"""A model that cannot hold its own tool schema is a guaranteed 413.

REPORTED 2026-09-05 as "error 200" in /activity. The row underneath it:

    nvidia/deepseek-v4-flash-0731 ! nvidia: ReadTimeout
    sub-claude/claude             ! sub-claude: HTTP 413
    groq/qwen/qwen3.8-27b         ! groq: HTTP 413
    dahl/DeepSeek-V4-Flash-0731   ! dahl: HTTP 429
    opencode-zen/deepseek-v4-flash-free ! opencode-zen: HTTP 400
    llm7/gpt-oss

groq 413ing should have been impossible: _upstream_chat compacts every payload
to the model's own window first, and that fix was verified live the same day
(160,000 tokens in, prompt_tokens=2803 out).

_compact_to_budget drops old turns and trims long messages. The TOOLS array is
neither: it is a fixed floor under every payload, and the CLI needs those exact
tools or the model cannot call them. MEASURED against a 600,000-character
conversation and groq's 8000-token window, compacting to a 6800 target:

    tools_alone   compacted to   fits target
    3,883            6,729          yes
    7,366            7,501          NO -- the floor is already above target
    12,012          12,147          NO

Above the target, compaction returns whatever it could not shrink and the
request goes out oversized. That is the `groq: HTTP 413` above, on the same day
compaction itself was verified working.

Nothing can fix it at send time, so the hop is not attempted -- it costs a
network round trip to learn what arithmetic already knows, and the chain has
better hops waiting.

The bar is deliberately PROVABLE, not merely likely: tools alone against the
FULL window, not against compaction's 0.85 target. A toolset between the two
(7,546 against groq's 8000 -- see test_the_borderline_toolset_is_still_attempted)
might still fit once the messages are compacted, so the provider gets to decide
and a 413 there costs one hop. _est_tokens also runs about 1.14x conservative,
and that margin is left in on purpose: a hop wrongly skipped loses a working
model, a hop wrongly attempted costs a single request.
"""
import pytest

import app as A


def _tool(n, props=("path", "content", "pattern")):
    return {"type": "function", "function": {
        "name": "tool_%d" % n,
        "description": "Does something useful. " * 40,
        "parameters": {"type": "object", "properties": {
            p: {"type": "string", "description": "A parameter. " * 30} for p in props},
            "required": [props[0]]}}}


# MEASURED against groq's 8000-token window:
#   2 tools, 2 props ->  1,371   fits with room
#  12 tools, 3 props ->  7,546   over compaction's 6800 target, UNDER the window
#  16 tools, 3 props ->  9,929   over the window itself -- provably impossible
SMALL_TOOLSET = [_tool(i, ("path", "content")) for i in range(2)]
BORDERLINE_TOOLSET = [_tool(i) for i in range(12)]
CLI_TOOLSET = [_tool(i) for i in range(16)]


def _payload(tools, text="hello"):
    return {"model": "m", "tools": tools,
            "messages": [{"role": "user", "content": text}]}


# --------------------------------------------------------------------------- #
# The arithmetic
# --------------------------------------------------------------------------- #

def test_the_cli_toolset_really_does_not_fit_groq():
    """The premise, measured rather than asserted: these tools are larger than
    groq's whole 8000-token window before a single message is attached."""
    assert A._est_tokens([], CLI_TOOLSET) > 8000


def test_a_small_toolset_fits_comfortably():
    assert A._est_tokens([], SMALL_TOOLSET) < int(8000 * 0.85)


def test_the_borderline_toolset_is_still_attempted():
    """7,546 tokens is over compaction's 6800 target but UNDER groq's 8000
    window, so whether it fits depends on how far the messages compact. That is
    not provably impossible, so the guard must not refuse it -- the provider
    gets to decide, and a 413 there costs one hop. Refusing it would be the
    guard overreaching into a judgement it cannot make."""
    assert 6800 < A._est_tokens([], BORDERLINE_TOOLSET) < 8000
    assert not A._tools_exceed_budget(_payload(BORDERLINE_TOOLSET), 8000)


# --------------------------------------------------------------------------- #
# The guard
# --------------------------------------------------------------------------- #

def test_a_toolset_over_the_whole_budget_is_refused():
    assert A._tools_exceed_budget(_payload(CLI_TOOLSET), 8000)


def test_a_toolset_that_fits_is_allowed():
    assert not A._tools_exceed_budget(_payload(SMALL_TOOLSET), 8000)


def test_a_big_window_takes_the_cli_toolset_easily():
    """The guard must not fire on the providers that can actually do this work."""
    for budget in (100000, 128000, 250000, 900000):
        assert not A._tools_exceed_budget(_payload(CLI_TOOLSET), budget), budget


def test_a_request_with_no_tools_is_never_refused():
    """Only the tool floor is unshrinkable; plain messages are compaction's job."""
    assert not A._tools_exceed_budget({"messages": [{"role": "user", "content": "x" * 900000}]}, 8000)
    assert not A._tools_exceed_budget(_payload([]), 8000)


def test_an_unknown_budget_never_refuses():
    """No budget means no evidence. Fail open -- refusing on a guess would drop
    a working provider."""
    for budget in (0, None, -1):
        assert not A._tools_exceed_budget(_payload(CLI_TOOLSET), budget), budget


def test_the_message_content_does_not_change_the_verdict():
    """It is a statement about the TOOLS, not about this turn. A huge
    conversation is compaction's problem and must not make the guard fire, and
    an empty one must not make it stop firing."""
    assert A._tools_exceed_budget(_payload(CLI_TOOLSET, ""), 8000)
    assert A._tools_exceed_budget(_payload(CLI_TOOLSET, "x" * 900000), 8000)


def test_the_margin_is_left_in_deliberately():
    """Tools alone are compared against the FULL budget, not the 0.85 compaction
    target. A model that fits its tools with nothing to spare is still tried:
    a hop wrongly skipped loses a working model, a hop wrongly attempted costs
    one 413."""
    budget = A._est_tokens([], CLI_TOOLSET) + 10
    assert not A._tools_exceed_budget(_payload(CLI_TOOLSET), budget)


def test_a_malformed_payload_fails_open():
    for bad in (None, {}, {"tools": "not-a-list"}, {"tools": [None, 3]}):
        assert not A._tools_exceed_budget(bad, 8000), bad


# --------------------------------------------------------------------------- #
# Wired into the send path
# --------------------------------------------------------------------------- #

def test_the_guard_runs_before_the_request_is_sent():
    """The whole point is not paying for the round trip."""
    src = open("app.py", encoding="utf-8").read()
    guard = src.index("_tools_exceed_budget(payload, _model_ctx_budget(")
    post = src.index("_post_with_header_deadline(_STREAM_HEADER_WAIT")
    assert guard < post


def test_it_raises_so_the_chain_falls_through():
    """_upstream_chat's callers walk on from a RequestException exactly as they
    do for a real 413 -- this just gets there without the round trip."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index("_tools_exceed_budget(payload, _model_ctx_budget(")
    window = src[i:i + 700]
    assert "raise" in window
    assert "RequestException" in window or "ConnectionError" in window \
        or "requests.exceptions" in window
