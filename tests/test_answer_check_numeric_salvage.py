"""A salvage must never turn one number into another.

Checked after a live sweep answered "5000" to "What is N plus 1? Answer with
only the number." The /v1/messages stream relays text verbatim (no salvage on
that path), so that reply was the model's own. But the glued-loop detector
could split a digit run: its periodic scan walks back over every char that
fits the loop, so "2826" followed by a "26" loop was salvaged to "28".
"""
import pytest

import answer_check

Q = "What is 2825 plus 1? Answer with only the number."


@pytest.mark.parametrize("reply", [
    "28" + "26" * 20,              # "2826" + a loop of its own last two digits
    "5001" + "01" * 20,
    "50005001" + "5001" * 6,       # an echo glued to the answer, then a loop
])
@pytest.mark.parametrize("fin", ["length", "stop"])
def test_a_cut_inside_a_digit_run_is_never_served(reply, fin):
    r = answer_check.inspect(reply, prompt_text=Q, finish_reason=fin)
    salvage = r.get("salvage")
    if salvage is not None:
        nxt = reply[len(salvage):len(salvage) + 1]
        assert not (salvage[-1:].isdigit() and nxt.isdigit()), (reply, salvage)


def test_an_echo_glued_to_the_answer_is_not_served_as_the_echo():
    """N=5000: "5000" (the echo) + "5001" looping. Before the fix the capped
    reply was salvaged to "5000" -- the reported wrong number."""
    r = answer_check.inspect("5000" + "5001" * 7, prompt_text="What is 5000 plus 1? "
                             "Answer with only the number.", finish_reason="length")
    assert r["salvage"] != "5000"


def test_the_first_copy_of_a_whole_number_loop_is_still_kept():
    r = answer_check.inspect("2826" * 9 + "28", prompt_text=Q, finish_reason="length")
    assert r["ok"] is False and r["salvage"] == "2826"


def test_a_decimal_is_never_cut_at_its_point():
    assert answer_check._splits_a_number("3.14159", "3.") is True


@pytest.mark.parametrize("text,clean,split", [
    ("2826262626", "28", True),
    ("28262826", "2826", False),
    ("2826 junk", "2826", False),
    ("2826HAMSTER-2826", "2826", False),
])
def test_the_split_rule(text, clean, split):
    assert answer_check._splits_a_number(text, clean) is split


def test_a_short_streamed_answer_with_its_own_done_line_is_content():
    """The peek used to call this "empty" (the [DONE] item hit the terminal
    check before anyone judged the content already seen), so every short
    correct answer on the line-framed /v1/responses and /v1/messages streams
    was discarded and only hops that never send [DONE] could answer."""
    import app
    lines = [b'data: {"choices":[{"delta":{"content":"5768"}}]}',
             b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
             b"data: [DONE]"]
    status, buf = app._peek_until_content(iter(lines), 5)
    assert status == "content" and buf == lines


def test_the_reported_shape_is_a_plain_wrong_answer():
    """'5000' for N=4998 is not junk: no detector fires, nothing is cut."""
    r = answer_check.inspect("5000", prompt_text="What is 4998 plus 1? Answer with only "
                             "the number.", finish_reason="stop")
    assert r["ok"] is True and r["salvage"] is None
