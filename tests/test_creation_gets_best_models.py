"""Anything CREATED by the hub gets the best models, whatever the classifier says.

USER DIRECTIVE 2026-08-30: "when it's about website creation or any creation
thing, of course he should always use best models, no matter our rules... even
motion design creation, or anything that will be created with our hub."

The difficulty classifier judges SHAPE, not intent, so a creation ask phrased
shortly fell to the cheap tier. MEASURED: "Write a python function to reverse a
string" classified `simple` and routed 40/40 to deepinfra/Qwen2.5-72B -- score
45.9 out of a 358-model pool, an old weak model writing code while kimi-k3
(134.8) sat idle.

The cheap tier still exists, and still serves what it was for: one-word replies,
lookups, translation, and the hub's own internal probes.
"""
from unittest import mock

import app


CREATION = [
    "Write a python function to reverse a string",
    "make a motion design animation for a hero section",
    "design a logo concept",
    "animate this svg",
    "build me a landing page for a bakery",
    "create a react dashboard",
    "refactor this module",
    "generate a color palette",
    "implement a rate limiter",
    "rewrite this in typescript",
]

NOT_CREATION = [
    "hi",
    "what is the capital of France",
    "summarize this paragraph",
    "translate hello to french",
    "is python faster than go",
    "who is Ada Lovelace",
]


def _difficulty(text):
    msgs = [{"role": "user", "content": text}]
    with mock.patch.object(app, "_available_providers", return_value=[]):
        # No providers -> returns (None, None, difficulty), which is all we need:
        # the tier decision happens before any candidate is looked at.
        _pid, _m, diff = app._route_by_difficulty(msgs, 2048, 50)
    return diff


def test_every_creation_ask_routes_at_the_hard_tier():
    for text in CREATION:
        assert _difficulty(text) == "hard", text


def test_trivial_asks_still_take_the_cheap_tier():
    """The cheap tier is for one-word replies, lookups and the hub's own probes.
    Lifting those would drain top-tier quota on housekeeping."""
    for text in NOT_CREATION:
        assert _difficulty(text) == "simple", text


def test_the_regex_matches_on_whole_words_only():
    """Word boundaries matter: 'codebase' or 'remake' must not read as a fresh
    creation ask. (They were silently lost once to shell escaping, which turned
    \b into a literal backspace and made the pattern match NOTHING -- caught by
    the routing test, not by eyeballing the source.)"""
    assert app._CREATION_INTENT_RE.search("build a site")
    assert app._CREATION_INTENT_RE.search("BUILD A SITE")      # case-insensitive
    assert not app._CREATION_INTENT_RE.search("rebuilding")    # not a bare verb
    assert not app._CREATION_INTENT_RE.search("makeshift")


def test_an_explicit_force_difficulty_is_never_overridden():
    """The hub's own probes pass force_difficulty precisely to STAY on the cheap
    providers; lifting them would spend strong quota on housekeeping."""
    msgs = [{"role": "user", "content": "write a landing page"}]
    with mock.patch.object(app, "_available_providers", return_value=[]):
        _p, _m, diff = app._route_by_difficulty(msgs, 2048, 50,
                                                force_difficulty="simple")
    assert diff == "simple"


def test_hard_stays_hard():
    assert _difficulty("Design and implement a distributed rate limiter with "
                       "Redis, handling clock skew, hot keys and graceful "
                       "degradation, with tests") == "hard"
