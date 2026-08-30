"""The phased todo list ships in EVERY mode, not only swarm.

REPORTED 2026-08-31: "why he dont always continue and finish the task till the
end ... i dont see the todolists and phases in work ... i'm talking about usage
inside cli's and also in our frontend /build".

Cause, found in one grep: craft.plan_message() had exactly ONE call site --
app._swarm_tool_result -- so the phased plan existed only when swarm mode was
on. Normal and Max, which is what most sessions actually run, got no planning
instruction at all. The block even announced itself as "(swarm mode is on)".

Nothing about working in phases is swarm-specific. Naming the phases, saying
which can run together, and keeping the list updated as each lands is how any
agent finishes a big task instead of stopping halfway to ask what is next --
which is the other half of the same report.

It now ships from craft.system_message() for any TOOL-CARRYING opening turn.
That is the single injector every protocol funnels through
(app._apply_craft_brief, called from _upstream_chat with
agentic=bool(payload["tools"])), so one change covers codex on /v1/responses,
claude on /v1/messages, opencode on /v1/chat/completions, every quality mode,
and /build alike.

Tool-less chat is deliberately excluded: a plan is a thing you EXECUTE, and a
client with no tools cannot work one.
"""
import craft


AGENTIC = "build me a restaurant website with a menu page and a booking form"
PLAIN = "explain how a mutex works"


def _content(text, tools=True):
    msg = craft.system_message(text, tools=tools)
    return msg["content"] if msg else ""


def test_a_tool_carrying_turn_is_told_to_plan_in_phases():
    body = _content(AGENTIC)
    assert "PLAN FIRST" in body
    assert "phase" in body.lower()


def test_it_ships_even_with_no_domain_brief_matched():
    """The narrate-then-stop turn that started all of this matched ZERO domain
    briefs. A hits-gated block would never have fired on it -- the same reason
    ACT/VERIFY already ship unconditionally for a tool-carrying caller."""
    body = _content("go on then, keep working on it")
    assert "PLAN FIRST" in body


def test_it_says_which_phases_can_run_together():
    """Asked for explicitly, back when this was swarm-only: "he should know
    paralal pahases that can be launched as same time and the ones that should
    wait till other one finish"."""
    body = _content(AGENTIC).lower()
    assert "same time" in body
    assert "waits for it" in body or "wait" in body


def test_it_no_longer_claims_to_be_a_swarm_feature():
    """It announced "(swarm mode is on)", which was both wrong in the other two
    modes and a hint to the model that it could ignore it."""
    assert "swarm mode is on" not in craft.PLAN_PHASES.lower()


def test_plain_chat_is_not_told_to_plan():
    """A plan is a thing you execute. A client with no tools cannot work one,
    and the tokens would buy nothing."""
    assert "PLAN FIRST" not in _content(PLAIN, tools=False)
    assert "PLAN FIRST" not in _content(AGENTIC, tools=False)


def test_the_plan_comes_before_the_act_and_verify_blocks():
    """Reading order matters: plan the work, do the work, check the work. ACT
    and VERIFY both say "applies to every brief above" and dangle otherwise."""
    body = _content(AGENTIC)
    assert body.index("PLAN FIRST") < body.index("ACT ")
    assert body.index("ACT ") < body.index("VERIFY")


def test_the_swarm_does_not_inject_it_a_second_time():
    """It used to add its own copy. Every swarm tool turn now goes through the
    same shared injector (it carries `tools` and does not set _no_craft), so a
    second copy would be pure duplication in the one mode that can least afford
    the tokens -- swarm pays for the context three times over."""
    import app
    assert "craft.plan_message()" not in _app_source()
    body = _content(AGENTIC)
    assert body.count("PLAN FIRST") == 1


def _app_source():
    with open("app.py", encoding="utf-8") as f:
        return f.read()


def test_the_budget_still_holds():
    """The ceiling this file must not break: the heaviest request stays under
    12.5% of the smallest context window the hub routes to (~32K). The ceiling
    moved from 11.5% once, for this block and the component lines together --
    see the note in test_craft_briefs.test_worst_case_brief_cost."""
    worst = max(len(_content(t)) for t in
                ("build an online store and deploy it",
                 "create a landing page for my saas",
                 "build me a restaurant website"))
    assert worst / 4 < 32768 * 0.125, "briefs cost ~%d tokens" % (worst // 4)
