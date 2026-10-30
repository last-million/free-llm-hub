"""Delayed tool-call credit goes to the model that emitted the call.

The bandit remembers which (provider, model) emitted each tool call id and
rewards it when the call's result comes back. An id can come back from a
DIFFERENT model (the hub's own rescued calls were all "call_rescued_0";
providers can repeat ids too). The old record's "credited" flag was carried
over to the new owner, so the new call was never credited, and the first
result carrying the id was graded, so an earlier call's result could be filed
against the new model.

Pure module tests: a fixed clock, no file, no network.
"""
import pytest

from bandit import Bandit

KIND = "coding|hard|tools|m"


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def _tool(cid, text="ok"):
    return {"role": "tool", "tool_call_id": cid, "content": text}


def _grade(msg):
    return 0 if "Traceback" in (msg.get("content") or "") else 1


def test_an_id_reused_by_another_model_is_credited_to_that_model():
    b = Bandit(clock=Clock())
    b.remember_tool_calls(["call_x"], "p1", "m1", KIND)
    first = [_tool("call_x", "ok")]
    assert b.credit_from_messages(first, _grade) == 1
    # The same id, emitted later by another model.
    b.remember_tool_calls(["call_x"], "p2", "m2", KIND)
    later = first + [{"role": "assistant", "tool_calls": [{"id": "call_x"}]},
                     _tool("call_x", "Traceback (most recent call last): boom")]
    assert b.credit_from_messages(later, _grade) == 1
    note = b.propensity_note(KIND, "p2", "m2")
    # m2's own result (the failing one) is what was graded: alpha 1, beta 2.
    assert (note["alpha"], note["beta"]) == pytest.approx((1, 2))
    # m1 kept exactly its one success.
    m1 = b.propensity_note(KIND, "p1", "m1")
    assert (m1["alpha"], m1["beta"]) == pytest.approx((2, 1))


def test_one_conversations_old_result_is_not_filed_against_another_model():
    b = Bandit(clock=Clock())
    b.remember_tool_calls(["call_y"], "pA", "good", KIND)
    hist_a = [_tool("call_y", "Traceback (most recent call last): x")]
    assert b.credit_from_messages(hist_a, _grade) == 1
    # B's model emits the same id; B's history carries B's result.
    b.remember_tool_calls(["call_y"], "pB", "other", KIND)
    hist_b = [_tool("call_y", "all good")]
    assert b.credit_from_messages(hist_b, _grade) == 1
    other = b.propensity_note(KIND, "pB", "other")
    assert (other["alpha"], other["beta"]) == pytest.approx((2, 1))


def test_the_same_model_re_remembering_a_credited_id_stays_credited():
    b = Bandit(clock=Clock())
    b.remember_tool_calls(["c1"], "p", "m", KIND)
    msgs = [_tool("c1")]
    assert b.credit_from_messages(msgs, _grade) == 1
    b.remember_tool_calls(["c1"], "p", "m", KIND)
    assert b.credit_from_messages(msgs, _grade) == 0


def test_the_latest_result_for_an_id_is_the_one_graded():
    b = Bandit(clock=Clock())
    b.remember_tool_calls(["c9"], "p", "m", KIND)
    seen = []

    def grade(msg):
        seen.append(msg["content"])
        return 1

    assert b.credit_from_messages([_tool("c9", "old"), _tool("c9", "new")], grade) == 1
    assert seen == ["new"]


def test_several_ids_are_still_graded_in_history_order():
    b = Bandit(clock=Clock())
    b.remember_tool_calls(["a", "b", "c"], "p", "m", KIND)
    seen = []

    def grade(msg):
        seen.append(msg["tool_call_id"])
        return 1

    assert b.credit_from_messages([_tool("a"), _tool("b"), _tool("c")], grade) == 3
    assert seen == ["a", "b", "c"]
