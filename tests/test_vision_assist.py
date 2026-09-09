r"""A model that cannot see, handed a screenshot.

REQUESTED 2026-09-09: "if he need screenshot and the used model dont have
vision so he should use vision model to help automaticly".

An agent driving a browser takes a screenshot and hands it back to itself. If
that agent is pinned to a model with no vision -- the NORMAL case, because a
CLI pins the model it was told to use -- the image reaches something that
cannot read it and the agent proceeds blind while believing it looked.

Routing already sends an UNPINNED image request to a vision model. This is the
pinned case, and the answer is deliberately not to override the pin: a pin is
the caller saying which model answers this turn. A model that CAN see does the
looking, and the pinned model is given words.

Never raises, and never invents: when nothing on the fleet can see, the turn
goes through untouched rather than carrying a description of an image nobody
looked at.
"""
import unittest.mock as mock

import pytest

import app as A


PNG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlE"
       "QVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _turn(n=1):
    parts = [{"type": "text", "text": "what is on screen?"}]
    for _ in range(n):
        parts.append({"type": "image_url", "image_url": {"url": PNG}})
    return [{"role": "user", "content": parts}]


# --------------------------------------------------------------------------- #
# Finding the images
# --------------------------------------------------------------------------- #

def test_images_are_found_in_a_turn():
    assert len(A._message_images(_turn(2))) == 2


def test_a_plain_text_turn_has_none():
    assert A._message_images([{"role": "user", "content": "hello"}]) == []


@pytest.mark.parametrize("messages", [None, [], [{}], [{"content": None}],
                                      [{"content": [{"type": "text"}]}],
                                      [{"content": [{"type": "image_url"}]}]])
def test_odd_shapes_do_not_raise(messages):
    assert A._message_images(messages) == []


# --------------------------------------------------------------------------- #
# Describing them
# --------------------------------------------------------------------------- #

def test_an_image_becomes_text():
    with mock.patch.object(A, "_describe_image", return_value="a login form"):
        out, n = A._vision_assist(_turn(), 100)
    assert n == 1
    part = out[0]["content"][1]
    assert part["type"] == "text"
    assert "a login form" in part["text"]


def test_the_replacement_says_where_it_came_from():
    """A model reading this must not mistake a description for something the
    user typed."""
    with mock.patch.object(A, "_describe_image", return_value="x"):
        out, _n = A._vision_assist(_turn(), 100)
    assert "described by a vision model" in out[0]["content"][1]["text"]


def test_the_caller_s_messages_are_not_mutated():
    """The handler keeps its own copy; editing in place would make a retry see
    a different request than the first attempt did."""
    original = _turn()
    with mock.patch.object(A, "_describe_image", return_value="x"):
        A._vision_assist(original, 100)
    assert original[0]["content"][1]["type"] == "image_url"


def test_nothing_that_can_see_means_nothing_changes():
    """Inventing a description of an image nobody looked at would be worse than
    passing the turn through."""
    msgs = _turn()
    with mock.patch.object(A, "_describe_image", return_value=""):
        out, n = A._vision_assist(msgs, 100)
    assert n == 0 and out is msgs


def test_a_describer_that_throws_is_survivable():
    msgs = _turn()
    with mock.patch.object(A, "_describe_image", side_effect=RuntimeError("boom")):
        out, n = A._vision_assist(msgs, 100)
    assert n == 0 and out is msgs


def test_the_number_of_images_described_is_capped():
    """A turn carrying twenty screenshots does not want twenty paragraphs."""
    with mock.patch.object(A, "_describe_image", return_value="x") as m:
        _out, n = A._vision_assist(_turn(20), 100)
    assert n == A._VISION_ASSIST_MAX_IMAGES
    assert m.call_count == A._VISION_ASSIST_MAX_IMAGES


def test_a_long_description_is_clipped():
    with mock.patch.object(A, "_describe_image",
                           return_value="y" * (A._VISION_ASSIST_CHARS * 3)):
        out, _n = A._vision_assist(_turn(), 100)
    assert len(out[0]["content"][1]["text"]) < A._VISION_ASSIST_CHARS * 2


# --------------------------------------------------------------------------- #
# Which model does the looking
# --------------------------------------------------------------------------- #

def test_the_describer_asks_for_a_vision_model():
    src = open("app.py", encoding="utf-8").read()
    body = src[src.index("def _describe_image("):]
    body = body[:body.index("\ndef ")]
    assert "require_vision=True" in body


def test_the_describer_goes_through_the_normal_chain():
    """So it gets the same fallback, key rotation and quota accounting as
    anything else, instead of a second private path to a provider."""
    src = open("app.py", encoding="utf-8").read()
    body = src[src.index("def _describe_image("):]
    body = body[:body.index("\ndef ")]
    assert "_build_chain(" in body and "_dispatch_chat(" in body


def test_it_asks_for_the_text_on_screen():
    """A screenshot's value to a blind model is mostly the words in it."""
    assert "transcribe the visible text" in A._VISION_ASSIST_PROMPT


# --------------------------------------------------------------------------- #
# When it runs
# --------------------------------------------------------------------------- #

def test_it_only_runs_for_a_pinned_model_that_cannot_see():
    src = open("app.py", encoding="utf-8").read()
    i = src.index("if has_images and _pin_kw:")
    block = src[i:i + 700]
    assert "_is_vision_model(" in block
    assert "_vision_assist(" in block


def test_an_unpinned_request_is_left_to_routing():
    """Routing already sends an image to a vision model; describing it first
    would spend a call to reach a worse answer."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index("if has_images and _pin_kw:")
    assert "_pin_kw" in src[i:i + 60]


def _assist_block():
    """The vision-assist branch of the chat handler, scoped to the BRANCH
    rather than to a byte count -- a window measured in characters fails the
    moment someone writes a longer comment inside it, which says nothing about
    whether the behaviour is still there."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index("if has_images and _pin_kw:")
    return src[i:src.index("\n    if not _pin_kw:", i)]


def test_the_pin_is_never_overridden():
    """A pin is the caller saying which model answers this turn. Only the
    MESSAGES are rewritten."""
    block = _assist_block()
    assert 'body["messages"]' in block
    after = block.split("_vision_assist")[1]
    assert 'body["model"]' not in after, "the pinned model must not be rewritten"


def test_the_size_estimate_is_recomputed():
    """An image is worth thousands of tokens and a paragraph a few hundred;
    routing the rewritten turn on the old estimate would size it for a request
    that no longer exists."""
    assert "est = _est_tokens(" in _assist_block()


def test_the_image_flag_is_recomputed_too():
    """Once the images are gone the turn is no longer an image turn, and every
    later decision reads that flag."""
    assert "has_images = bool(_message_images(" in _assist_block()
