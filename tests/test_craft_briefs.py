"""Domain craft briefs, and the rules that keep them from doing harm.

Injected guidance is only worth its tokens if it fires on the right requests and
NEVER on the wrong ones — an unwanted brief costs context on a small free window
AND adds instructions the user did not ask for. It is also injected on the
OPENING turn only: adding instructions to a turn that carries tool_calls is what
breaks Codex/Claude Code agent loops.
"""
import pytest

import app
import craft


@pytest.mark.parametrize("text,expected", [
    ("Build me an online store landing page for solar panels", ["landing", "ecommerce"]),
    ("write the copy for our sales page", ["landing"]),
    ("add a shopping cart and stripe checkout", ["ecommerce"]),
    ("write a blog post and optimise it for SEO", ["seo"]),
    ("redesign the hero section with tailwind", ["web_design"]),
    ("optimise the query in src/db.py", ["programming"]),
    ("refactor the auth module and write tests", ["programming"]),
])
def test_briefs_fire_on_their_own_domain(text, expected):
    assert craft.names(text) == expected


@pytest.mark.parametrize("text", [
    "what is the capital of France",
    "explain how a mutex works",
    "design a database schema",          # 'design' alone must not mean web design
    "translate this to French",
    "",
])
def test_nothing_fires_on_unrelated_requests(text):
    """A brief on an unrelated ask is pure token cost plus unwanted instructions."""
    assert craft.names(text) == []


def test_at_most_two_briefs():
    """'online store landing page with SEO and tests' could match five; more than
    two is context tax, not help."""
    hits = craft.names("build an ecommerce landing page with SEO, refactor and write tests")
    assert len(hits) <= craft.MAX_BRIEFS


def test_every_brief_names_what_NOT_to_do():
    """The ANTI sections are the point: models over-produce exactly the generic
    output these forbid."""
    for _name, _rx, body in craft._BRIEFS:
        assert "ANTI" in body, _name


def test_briefs_forbid_inventing_facts():
    """The failure that matters most in this domain: fabricated testimonials,
    metrics, prices and reviews."""
    for name in ("landing", "ecommerce", "seo"):
        body = dict((n, b) for n, _r, b in craft._BRIEFS)[name].lower()
        assert "invent" in body or "fabricat" in body, name


def test_briefs_stay_small():
    """Injected into a small free context window on every matching opening turn."""
    for name, _rx, body in craft._BRIEFS:
        assert len(body) < 1400, "%s brief is too long (%d chars)" % (name, len(body))


# --------------------------------------------------------------------------- #
# Injection safety
# --------------------------------------------------------------------------- #

def test_injected_on_the_opening_turn():
    msgs = [{"role": "system", "content": "You are Codex."},
            {"role": "user", "content": "build a landing page for my solar store"}]
    out = app._apply_craft_brief(msgs)
    assert len(out) == 3
    assert "LANDING PAGE" in out[1]["content"]


def test_the_callers_own_system_prompt_still_comes_first():
    """Codex's agent prompt must win on any conflict, so the brief goes AFTER it."""
    msgs = [{"role": "system", "content": "You are Codex."},
            {"role": "user", "content": "build a landing page"}]
    assert app._apply_craft_brief(msgs)[0]["content"] == "You are Codex."


def test_never_injected_mid_conversation():
    msgs = [{"role": "user", "content": "build a landing page"},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "make it better"}]
    assert app._apply_craft_brief(msgs) is msgs


def test_never_injected_into_a_running_tool_loop():
    """The failure mode that made the prompt enhancer dashboard-only."""
    msgs = [{"role": "user", "content": "build a landing page"},
            {"role": "assistant", "tool_calls": [{"function": {"name": "shell"}}]}]
    assert app._apply_craft_brief(msgs) is msgs


def test_unmatched_requests_are_returned_untouched():
    msgs = [{"role": "user", "content": "what is the capital of France"}]
    assert app._apply_craft_brief(msgs) is msgs


def test_multimodal_opening_turn_is_read_for_text():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "redesign this hero section"},
                                         {"type": "image_url", "image_url": {"url": "x"}}]}]
    assert len(app._apply_craft_brief(msgs)) == 2


def test_injection_never_raises_on_junk():
    for bad in (None, [], "nope", [None], [{"role": "user"}]):
        app._apply_craft_brief(bad)
