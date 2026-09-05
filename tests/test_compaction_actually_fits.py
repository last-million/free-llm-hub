"""Compaction must not report success while handing back an oversized payload.

REPORTED 2026-09-05: a run of 503s out of opencode. Every one carried the same
shape -- est around 160,000 tokens -- and the hub's own log named the hops:

    CHAT-503 est=161612 errors=[nvidia: HTTP 502; sub-claude: HTTP 413;
    groq: HTTP 413; dahl: HTTP 429; opencode-zen: HTTP 400; uncloseai: HTTP 404]

A 413 from groq should be impossible. _upstream_chat compacts every payload to
the model's own window before sending, and groq's budget is 8000 tokens, so what
left the hub should have been about 6800.

MEASURED against the real request shape:

    newest message is the huge one   before=150431  after=150493  did=True
    many medium turns                before=120417  after=11489   did=True

The first row came back BIGGER than it went in -- the truncation notice was
added and nothing was actually removed -- and both claimed success.

Root cause: the keep-loop admits the NEWEST message whatever its size, so that
"at least one turn survives". That is right. What was missing is the step after
it: nothing trims the survivor. And because turns HAD been dropped,
len(kept) < len(rest), so _trim_largest_message -- the code written for exactly
this, an overflow living inside one message -- was never reached. It only ran
when no turn could be dropped at all.

So the guarantee this function owes its callers is stated as a test: whatever it
returns either fits the target or is honestly reported as unchanged. Every
caller treats `did=True` as "this now fits".
"""
import pytest

import app as A


TOOLS = [{"type": "function", "function": {"name": "edit", "parameters": {}}}]

GROQ_BUDGET = 8000                      # the real one, from _provider_tpm
TARGET = int(GROQ_BUDGET * 0.85)        # what _compact_to_budget aims for

BIG = "x" * 600000                      # ~150K tokens, a repo in one message
MEDIUM = "y" * 40000                    # ~10K tokens, one file


def _size(msgs):
    return A._est_tokens(msgs, TOOLS)


def _compact(msgs, budget=GROQ_BUDGET):
    return A._compact_to_budget(msgs, TOOLS, budget)


# --------------------------------------------------------------------------- #
# The reported failure
# --------------------------------------------------------------------------- #

def test_a_huge_newest_message_is_actually_cut_down():
    """The exact opencode shape: a few turns of conversation, then the whole
    repository pasted into the latest one."""
    msgs = [{"role": "system", "content": "you are a coding agent"},
            {"role": "user", "content": "build the app"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "here is the repo:\n" + BIG}]
    out, did = _compact(msgs)
    assert did
    assert _size(out) <= TARGET, "left the hub at %d tokens for an %d budget" % (
        _size(out), GROQ_BUDGET)


def test_compaction_never_returns_something_bigger():
    """It came back 62 tokens LARGER than it went in -- the notice was added and
    nothing was removed."""
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "build the app"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": BIG}]
    before = _size(msgs)
    out, _did = _compact(msgs)
    assert _size(out) <= before


def test_many_medium_turns_also_fit():
    """No single message is over target here; the survivor still is."""
    msgs = [{"role": "user", "content": MEDIUM} for _ in range(12)]
    out, did = _compact(msgs)
    assert did
    assert _size(out) <= TARGET, "%d tokens" % _size(out)


def test_success_is_never_reported_for_an_oversized_result():
    """The contract every caller relies on. _upstream_chat sends whatever comes
    back without re-checking, so `did=True` on an oversized payload is what
    turned into the 413."""
    for label, msgs in [
        ("huge newest", [{"role": "user", "content": "hi"},
                         {"role": "assistant", "content": "ok"},
                         {"role": "user", "content": BIG}]),
        ("many medium", [{"role": "user", "content": MEDIUM} for _ in range(12)]),
        ("huge middle", [{"role": "user", "content": BIG},
                         {"role": "assistant", "content": "ok"},
                         {"role": "user", "content": "add dark mode"}]),
    ]:
        out, did = _compact(msgs)
        if did:
            assert _size(out) <= TARGET, "%s: claimed to fit at %d tokens" % (label, _size(out))


# --------------------------------------------------------------------------- #
# ...without breaking what already worked
# --------------------------------------------------------------------------- #

def test_a_single_oversized_turn_still_trims():
    """The path _trim_largest_message was written for, and the one case that
    always worked -- no turn could be dropped, so it was reached."""
    out, did = _compact([{"role": "user", "content": BIG}])
    assert did and _size(out) <= TARGET


def test_a_huge_middle_turn_is_still_dropped_wholesale():
    """Dropping the turn is better than trimming it when the newest turns fit:
    the result should be far UNDER target, not trimmed to sit just below it."""
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "here is the repo:\n" + BIG},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "now add dark mode"}]
    out, did = _compact(msgs)
    assert did and _size(out) <= TARGET
    assert _size(out) < TARGET // 2, "trimmed when it could simply have dropped"


def test_a_payload_that_already_fits_is_untouched():
    msgs = [{"role": "user", "content": "hello"}]
    out, did = _compact(msgs)
    assert did is False and out is msgs


def test_an_unknown_budget_is_still_a_no_op():
    msgs = [{"role": "user", "content": BIG}]
    assert A._compact_to_budget(msgs, TOOLS, 0) == (msgs, False)
    assert A._compact_to_budget(msgs, TOOLS, None) == (msgs, False)


def test_leading_system_messages_survive():
    msgs = [{"role": "system", "content": "system one"},
            {"role": "system", "content": "system two"},
            {"role": "user", "content": "build the app"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": BIG}]
    out, _did = _compact(msgs)
    assert [m["content"] for m in out[:2]] == ["system one", "system two"]


def test_the_newest_turn_is_never_dropped_entirely():
    """Trimming must not become deletion -- the message the user just sent has
    to reach the model in some form, or the turn is answered blind."""
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "build the app"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "UNIQUEMARKERHEAD " + BIG}]
    out, _did = _compact(msgs)
    assert any(isinstance(m.get("content"), str) and "UNIQUEMARKERHEAD" in m["content"]
               for m in out), "the latest user turn vanished"


def test_the_cut_is_marked_so_the_model_knows_material_is_missing():
    msgs = [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": BIG}]
    out, _did = _compact(msgs)
    joined = " ".join(m["content"] for m in out if isinstance(m.get("content"), str))
    assert "omitted" in joined or "truncated" in joined


def test_a_real_groq_sized_payload_would_not_413():
    """End to end against the numbers from the incident: 161,612 tokens in,
    groq's 8000-token budget, and a result that actually fits it."""
    msgs = [{"role": "system", "content": "you are opencode"},
            {"role": "user", "content": "build it"},
            {"role": "assistant", "content": "working"},
            {"role": "user", "content": "x" * 640000}]
    assert _size(msgs) > 150000
    out, did = A._compact_to_budget(msgs, TOOLS,
                                    A._model_ctx_budget("groq", "qwen/qwen3.8-27b"))
    assert did and _size(out) <= TARGET


def test_two_large_survivors_both_get_cut():
    """One trim pass is not enough, and this shape is not exotic: a coding CLI
    ships a large agent system prompt, which is always kept, alongside the newest
    user turn, which is kept unconditionally. Both survive the drop loop, and
    _trim_largest_message only shrinks the BIGGEST message per call.

    MEASURED: pass 1 leaves 75,550 tokens -- eleven times over an 8000-token
    budget, and still a 413. Pass 2 lands it at 6,728."""
    msgs = [{"role": "system", "content": "S" * 300000},
            {"role": "user", "content": "build the app"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "U" * 300000}]
    out, did = _compact(msgs)
    assert did
    assert _size(out) <= TARGET, "%d tokens after compaction" % _size(out)


def test_the_trim_loop_is_bounded():
    """It must converge, not spin: a payload nothing can usefully cut has to
    return rather than loop until the request times out."""
    msgs = [{"role": "system", "content": "s"},
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"}]
    out, did = A._compact_to_budget(msgs, TOOLS, 1)      # target smaller than the notice
    assert isinstance(out, list) and isinstance(did, bool)
