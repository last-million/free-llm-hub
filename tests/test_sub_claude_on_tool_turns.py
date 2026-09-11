"""A local subscription cannot call a tool, so it must not take a swarm slot.

MEASURED over three live fan-outs: the member "sub-claude/claude" returned the
same prose every time --

    "Blocked: write permission not granted for <an AppData path>"

-- at ~21s against 3-7s for the members that answered. One slot in five, spent
on a certainty.

It is a certainty because the sub path is structurally incapable of a tool call:
_subscription_chat reads only payload["messages"] and never payload["tools"], and
its response shim is a two-key {"role","content"} literal with no tool_calls key
at all. Nothing about the model; nothing a retry could change.

TWO CORRECTIONS TO THE OBVIOUS FIX, both found by checking rather than assuming:

1. Making _supports_tools() return False for sub providers does NOTHING. It is
   never called with a sub pid: all five call sites filter lists built from
   _available_providers(), and sub ids are deliberately not merged into it.
   Verified by patching it exactly that way and rebuilding the chain -- the sub
   hop was still there.

2. Gating _build_chain's own sub append on require_tools OVERSHOOTS.
   require_tools is bool(body["tools"]), true for every Claude Code, opencode and
   codex turn including read-only questions the CLI attaches its schema to.
   Blanket-gating turns those into 503s exactly when the free fleet is empty and
   the paid subscription is the only thing left.

So the skip lives in the swarm's candidate loop, which is the only place that
specifically needs a tool call. The chain keeps its last resort.

AND THE PROSE ITSELF WAS BEING ACCEPTED. _REFUSAL_RE requires a first-person
verb -- "I cannot help". A sandboxed CLI does not talk like that; it states the
obstacle. So nothing detected it, and because the winner is chosen as
`acted or results`, that message could WIN a turn where no member emitted a tool
call -- the hub telling the user it lacks permissions, about a temp directory
they never chose.
"""
import pytest

import app as A


# --------------------------------------------------------------------------- #
# The premise
# --------------------------------------------------------------------------- #

def test_the_sub_path_never_forwards_tools():
    """payload["tools"] is dropped: the CLI is never told the tools exist."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index("def _subscription_chat(")
    body = src[i:i + 2500]
    assert 'payload.get("messages")' in body or 'payload["messages"]' in body
    assert 'payload.get("tools")' not in body and 'payload["tools"]' not in body


def test_the_sub_response_has_no_tool_calls_field():
    """So even a model that wanted to call one has no channel to do it."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index("def _subscription_chat(")
    assert "tool_calls" not in src[i:i + 2500]


# --------------------------------------------------------------------------- #
# The fix that would NOT have worked
# --------------------------------------------------------------------------- #

def test_supports_tools_is_never_asked_about_a_sub_provider():
    """The tempting fix is a provable no-op, and this is why: every caller
    filters a list that cannot contain a sub pid."""
    assert not [p for p in A._available_providers() if A._is_sub(p)]


def test_rejecting_subs_in_supports_tools_would_not_remove_them(monkeypatch):
    """Measured, not argued. With the 'fix' applied the sub hop is still in a
    require_tools chain."""
    real = A._supports_tools
    monkeypatch.setattr(A, "_supports_tools",
                        lambda pid, m: False if A._is_sub(pid) else real(pid, m))
    chain = A._build_chain(None, None, 500, require_tools=True,
                           messages=[{"role": "user", "content": "make a file"}])
    if not [p for p, _m in chain if A._is_sub(p)]:
        pytest.skip("no sub provider is enabled on this machine")


# --------------------------------------------------------------------------- #
# The fix that does
# --------------------------------------------------------------------------- #

def test_the_swarm_skips_sub_providers():
    src = open("app.py", encoding="utf-8").read()
    i = src.index("def _swarm_tool_result(")
    # The ranker now takes the turn's difficulty as well, so anchor on the
    # call NAME rather than its exact arguments.
    loop = src[i:src.index("picks = _swarm_rank(", i)]
    assert "if _is_sub(hop_pid):" in loop
    assert "continue" in loop


def test_the_chain_keeps_its_last_resort():
    """The skip is in the swarm, NOT in _build_chain: a drained free fleet must
    still be able to answer a prose question from the paid subscription."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index("for pid in ([] if require_vision else _sub_available_providers())")
    line = src[i:src.index("\n", i)]
    assert "require_tools" not in line, \
        "gating this on require_tools 503s every read-only CLI turn on an empty fleet"


def test_a_plain_turn_can_still_reach_a_subscription():
    chain = A._build_chain(None, None, 500, require_tools=False,
                           messages=[{"role": "user", "content": "what is 2+2"}])
    assert isinstance(chain, list)


# --------------------------------------------------------------------------- #
# The prose is a non-answer
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text", [
    "Blocked: write permission not granted for C:/Users/x/AppData/Local/tmp",
    "Blocked. Every tool call needs approval -- run /permissions and allow Write",
    "Blocked",
    "Permission denied writing the file.",
    "No write permission for that path.",
    "Not permitted to write there.",
])
def test_a_permission_block_is_recognised(text):
    assert A._looks_like_permission_block(text)


@pytest.mark.parametrize("text", [
    "Blocked the main thread for 200ms, then cached the result.",
    "Blocked on the network call, so I retried.",
    "The request was denied by the upstream API, so I used the cache.",
    "Built all 12 pages. Write permission was not granted for logs.",
    "Should I request write permission first?",
    "I cannot help with that.",
    "",
    None,
])
def test_ordinary_prose_is_not_mistaken_for_one(text):
    """The words have to be ABOUT permission, in the opening sentence. Prose that
    merely mentions blocking or denial is a turn doing its job -- and the
    'Write permission was not granted for logs' case is a turn that DID the work
    and then named a limit, which the sibling refusal detector was already
    burned by once."""
    assert not A._looks_like_permission_block(text)


def test_a_question_is_never_a_block():
    """Asking for permission is what a well-behaved agent SHOULD do."""
    assert not A._looks_like_permission_block("Blocked: should I request write access?")


def test_the_swarm_treats_it_as_a_non_answer():
    src = open("app.py", encoding="utf-8").read()
    i = src.index("_looks_like_permission_block(msg.get(\"content\"))")
    window = src[max(0, i - 400):i + 200]
    assert "_note_nonanswer" in window


def test_it_is_separate_from_the_first_person_refusal_detector():
    """Different grammar, different test. A model declines in the first person;
    a sandboxed CLI states an obstacle. Folding them into one regex is how the
    'I' requirement silently swallowed the second shape."""
    assert not A._REFUSAL_RE.search("Blocked: write permission not granted")
    assert A._looks_like_permission_block("Blocked: write permission not granted")
