"""DIRECTED fixes: the manager instructs, a free model applies, the manager
confirms.

REPORTED: with a manager, the final revision (and a failed phase's fix) was
the manager REWRITING a view clipped to ~6000 chars -- so a long draft lost
its trimmed middle in the "fixed" version, and the subscription paid to
re-type text it could not see. These tests pin the redesign: the manager only
ever sees compact inputs and writes short instructions/verdicts, the free
apply gets the FULL text, a lossy apply is rejected before any verdict is paid
for, and manager=None keeps the old revision call byte-for-byte. No network.
"""
import json
import re
import threading

import swarm

ASK = [{"role": "user", "content": "Build a landing page: hero, pricing, FAQ."}]


def _long(tag, n=900):
    # Varied words so answer_check's loop detector has nothing to flag.
    return " ".join("%s%d" % (tag, i) for i in range(n))


HERO = "Start free. " + _long("hero")          # ~6K chars each
PRICING = "Pricing tiers. " + _long("price")

PLAN = json.dumps({"goal": "a landing page", "phases": [
    {"title": "Hero", "task": "write the hero", "needs": []},
    {"title": "Pricing", "task": "write the pricing table", "needs": []}]})

REVISE = '{"verdict": "revise", "problems": ["FAQ section missing"]}'


def _stage(msgs):
    sys_ = msgs[0]["content"]
    for name, prompt in (("plan", swarm._PLAN_SYSTEM), ("phase", swarm._PHASE_SYSTEM),
                         ("supervise", swarm._SUPERVISE_SYSTEM),
                         ("review", swarm._REVIEW_SYSTEM), ("synth", swarm._SYNTH_SYSTEM),
                         ("verify", swarm._VERDICT_SYSTEM), ("fix", swarm._FIX_SYSTEM),
                         ("instruct", swarm._INSTRUCT_SYSTEM),
                         ("apply", swarm._APPLY_SYSTEM),
                         ("confirm", swarm._CONFIRM_SYSTEM)):
        if sys_.startswith(prompt):
            if name == "phase" and "A PREVIOUS ATTEMPT" in msgs[-1]["content"]:
                return "retry"
            if name == "phase" and "DRAFT\n" in msgs[-1]["content"]:
                return "revision"
            return name
    return "?"


def _work_of(user):
    return re.search(r"THE WORK\n(.*)\n\nFIX INSTRUCTIONS\n", user, re.S).group(1)


def _free(phase=None, apply=None, review=REVISE, synth="FINAL"):
    calls, lock = [], threading.Lock()

    def dispatch(messages, max_tokens, exclude_pids=()):
        st = _stage(messages)
        user = messages[-1]["content"]
        with lock:
            n = len(calls)
            calls.append({"stage": st, "user": user, "max_tokens": max_tokens,
                          "exclude": tuple(exclude_pids)})
        if st in ("phase", "retry"):
            text = phase(user, st) if phase else (HERO if "Hero" in user else PRICING)
        elif st == "apply":
            k = sum(1 for c in calls if c["stage"] == "apply")
            text = apply(_work_of(user), k) if apply else \
                _work_of(user) + "\n\n## FAQ\nQ: is it free? A: yes."
        else:
            text = {"plan": PLAN, "supervise": '{"missing": []}', "review": review,
                    "synth": synth, "revision": "REWRITTEN"}.get(st, "")
        return text, ("free%d/model" % n if text else None)

    dispatch.calls = calls
    dispatch.of = lambda st: [c for c in calls if c["stage"] == st]
    return dispatch


def _manager(answers):
    answers = dict(answers)
    calls, lock = [], threading.Lock()

    def manager(messages, max_tokens, purpose):
        with lock:
            calls.append({"purpose": purpose, "max_tokens": max_tokens,
                          "stage": _stage(messages),
                          "chars": sum(len(m["content"]) for m in messages),
                          "user": messages[-1]["content"]})
            a = answers.get(purpose, "")
            if isinstance(a, list):
                a = a.pop(0) if a else ""
        return a, ("sub-claude/sonnet" if a else None), (7 if a else 0)

    manager.calls = calls
    manager.of = lambda st: [c for c in calls if c["stage"] == st]
    return manager


GOOD = {"plan": PLAN, "supervise": '{"missing": []}', "review": REVISE,
        "verify": '{"ok": true, "problems": []}',
        "fix": "1. After the pricing section add a FAQ section with one Q/A."}

REV = {"max_revisions": 1}


# ---- the final revision --------------------------------------------------

def test_revision_applies_manager_instructions_to_the_full_draft():
    d, m = _free(), _manager(GOOD)
    out = swarm.run(ASK, d, profile=REV, manager=m)
    applies = d.of("apply")
    assert len(applies) == 1
    work = _work_of(applies[0]["user"])
    # The FULL draft, both phases end to end -- nothing trimmed.
    assert HERO in work and PRICING in work and "[... trimmed ...]" not in work
    assert "After the pricing section add a FAQ" in applies[0]["user"]
    assert applies[0]["max_tokens"] >= swarm.SYNTH_MAX_TOKENS
    # The manager only instructed and confirmed -- short caps, compact views.
    instruct = m.of("instruct")
    assert len(instruct) == 1 and instruct[0]["max_tokens"] == swarm.MANAGER_INSTRUCT_TOKENS
    assert "FAQ section missing" in instruct[0]["user"]
    confirm = m.of("confirm")
    assert len(confirm) == 1 and confirm[0]["max_tokens"] == swarm.MANAGER_VERDICT_TOKENS
    assert "FAQ" in confirm[0]["user"]                    # it saw the diff
    for c in m.calls:
        assert c["chars"] < 12000, (c["stage"], c["chars"])
    # The manager never rewrote the draft itself.
    assert m.of("phase") == [] and d.of("revision") == []
    # Synthesis got the revised draft and no stale problem list.
    synth = d.of("synth")[0]["user"]
    assert "## FAQ" in synth and "REVIEWER PROBLEMS" not in synth
    assert out["text"] == "FINAL"
    assert ("revision", "free%d/model" % d.calls.index(applies[0])) in out["models"]
    assert ("fix-plan:revision", "sub-claude/sonnet") in out["models"]


def test_lossy_apply_is_rejected_free_and_retried_on_another_provider():
    def apply(work, k):
        if k == 1:
            return work[:len(work) // 3]                  # truncated by an output cap
        return work + "\n\n## FAQ\nQ: A."
    d, m = _free(apply=apply), _manager(GOOD)
    swarm.run(ASK, d, profile=REV, manager=m)
    applies = d.of("apply")
    assert len(applies) == 2
    first_pid = "free%d" % d.calls.index(applies[0])
    assert first_pid in applies[1]["exclude"]
    assert "dropped content" in applies[1]["user"]
    # Only the good apply was worth a manager verdict.
    assert len(m.of("confirm")) == 1
    assert "## FAQ" in d.of("synth")[0]["user"]


def test_confirm_rejected_twice_keeps_the_draft_and_hands_problems_to_synthesis():
    answers = dict(GOOD, verify=['{"ok": true}', '{"ok": true}',
                                 '{"ok": false, "problems": ["FAQ still missing"]}',
                                 '{"ok": false, "problems": ["FAQ still missing"]}'])
    d, m = _free(), _manager(answers)
    out = swarm.run(ASK, d, profile=REV, manager=m)
    assert len(d.of("apply")) == 2
    assert "FAQ still missing" in d.of("apply")[1]["user"]
    synth = d.of("synth")[0]["user"]
    assert "REVIEWER PROBLEMS TO FIX" in synth and "## FAQ" not in synth
    assert out["text"] == "FINAL"


def test_no_instructions_from_the_manager_applies_the_reviewer_problems():
    d, m = _free(), _manager(dict(GOOD, fix=""))
    swarm.run(ASK, d, profile=REV, manager=m)
    applies = d.of("apply")
    assert len(applies) == 1
    assert "1. Fix: FAQ section missing" in applies[0]["user"]
    assert HERO in _work_of(applies[0]["user"])


def test_without_a_manager_the_revision_is_directed_too():
    """CHANGED: the plain pipeline used to hand one free model the draft
    CLIPPED to DEP_CONTEXT_CHARS and let its rewrite replace the draft, so a
    long draft lost its trimmed middle. Now a free call writes instructions
    and a free apply edits the FULL draft -- no clipped rewrite anywhere."""
    d = _free()
    swarm.run(ASK, d, profile=REV)
    assert d.of("revision") == [], "a clipped whole-draft rewrite still ran"
    instruct = d.of("instruct")
    assert len(instruct) == 1 and "FAQ section missing" in instruct[0]["user"]
    applies = d.of("apply")
    assert len(applies) == 1
    work = _work_of(applies[0]["user"])
    assert HERO in work and PRICING in work and "[... trimmed ...]" not in work
    # The free instructions were empty, so the reviewer's problems stood in.
    assert "1. Fix: FAQ section missing" in applies[0]["user"]
    assert "## FAQ" in d.of("synth")[0]["user"]


# ---- a long phase that failed twice --------------------------------------

LONG_PLAN = json.dumps({"goal": "g", "phases": [
    {"title": "Hero", "task": "write the hero", "needs": [],
     "acceptance": ['includes "Start free"']},
    {"title": "Pricing", "task": "write the pricing table", "needs": []}]})


def test_long_failed_phase_is_fixed_by_a_free_apply_not_a_manager_rewrite():
    bad = "No call to action. " + _long("h")          # ~5K chars, misses the quote

    def phase(user, st):
        return bad if "Hero" in user else "Pricing: Free, Pro, Team."

    def apply(work, k):
        return "Start free today. " + work

    d = _free(phase=phase, apply=apply, review='{"verdict": "ship", "problems": []}')
    m = _manager(dict(GOOD, plan=LONG_PLAN, review='{"verdict": "ship", "problems": []}'))
    out = swarm.run(ASK, d, manager=m)
    applies = d.of("apply")
    assert len(applies) == 1
    assert bad in _work_of(applies[0]["user"])            # the FULL failed attempt
    failed = {"free%d" % d.calls.index(c) for c in d.of("phase") + d.of("retry")
              if "Hero" in c["user"]}
    assert failed and failed <= set(applies[0]["exclude"])
    assert m.of("fix") == []                              # no rewrite from an excerpt
    assert len(m.of("instruct")) == 1 and m.of("instruct")[0]["chars"] < 12000
    hero = [p for p in out["phases"] if p["title"] == "Hero"][0]
    assert hero["output"] == "Start free today. " + bad
    assert any(r == "phase-fix:Hero" for r, _ in out["models"])


def test_long_failed_phase_without_instructions_ships_the_last_attempt():
    bad = "No call to action. " + _long("h")
    d = _free(phase=lambda u, st: bad if "Hero" in u else "Pricing.",
              review='{"verdict": "ship", "problems": []}')
    m = _manager(dict(GOOD, plan=LONG_PLAN, fix="",
                      review='{"verdict": "ship", "problems": []}'))
    out = swarm.run(ASK, d, manager=m)
    assert d.of("apply") == []                            # no third free attempt
    hero = [p for p in out["phases"] if p["title"] == "Hero"][0]
    assert hero["output"] == bad


# ---- helpers ---------------------------------------------------------------

def test_changes_shows_only_what_moved():
    before = "\n".join("line %d" % i for i in range(200))
    after = before.replace("line 100", "line 100 EDITED")
    diff = swarm._changes(before, after)
    assert "+line 100 EDITED" in diff and "-line 100" in diff
    assert "line 5\n" not in diff and len(diff) < 200
    assert swarm._changes(before, before) == ""


def test_apply_tokens_scale_with_the_work_within_bounds():
    assert swarm._apply_tokens("x" * 100, 4000) == 4000
    assert swarm._apply_tokens("x" * 21000, 4000) == 21000 // 3 + 500
    assert swarm._apply_tokens("x" * 10 ** 6, 4000) == swarm.APPLY_MAX_TOKENS


def test_a_draft_too_long_for_any_apply_pays_for_no_instructions():
    """No apply can return enough of a ~58K-char draft (8000-token ceiling vs
    the 70% keep check), so the manager is not paid to instruct and no free
    apply runs; synthesis still gets the reviewer's problems."""
    big = {"Hero": "Start free. " + _long("hero", 3200),
           "Pricing": "Pricing tiers. " + _long("price", 3200)}
    d = _free(phase=lambda user, st: big["Hero"] if "Hero" in user else big["Pricing"])
    m = _manager(GOOD)
    swarm.run(ASK, d, profile=REV, manager=m)
    assert m.of("instruct") == [] and d.of("apply") == []
    assert "fix" not in [c["purpose"] for c in m.calls]
    assert "REVIEWER PROBLEMS TO FIX" in d.of("synth")[0]["user"]
