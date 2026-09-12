r"""Five agents started together all pinned the same model.

A session pin keeps ONE conversation on ONE model, which is right: an agent
that changes model mid-job produces incoherent work, and that is exactly why
the pin exists.

But every session picks its pin out of the same ranked pool, so sessions
started together all pin the SAME top model. What was supposed to be five
agents working in parallel became one model doing five jobs in series, with the
rest of the fleet idle and no second opinion anywhere.

REPORTED: "why do they only ever use the same model? he should use at least 2
or 3 of the best different models available if available."

The fix is one line at the moment a session decides: drop candidates other live
conversations already hold, and fail open when they all are -- sharing a model
is slower, refusing to route is broken.
"""
import time

import pytest

import app as A


@pytest.fixture(autouse=True)
def _clean():
    with A._session_pin_lock:
        A._session_pins.clear()
    yield
    with A._session_pin_lock:
        A._session_pins.clear()


# (score, pid, model) -- the shape _weighted_pick and _spread_pool both take.
POOL = [
    (140.0, "tokenrouter", "z-ai/glm-5.3-free"),
    (138.0, "groq", "qwen/qwen3.8-27b"),
    (136.0, "cerebras", "gpt-oss-120b"),
]


# --------------------------------------------------------------------------- #
# Who else is on
# --------------------------------------------------------------------------- #

def test_nothing_pinned_means_nothing_taken():
    assert A._pinned_elsewhere() == set()


def test_a_pinned_model_is_taken():
    A._session_pin_set("sess-a", "groq", "qwen/qwen3.8-27b")
    assert A._normalize_model_identity("qwen/qwen3.8-27b") in A._pinned_elsewhere()


def test_a_session_does_not_take_its_own_model():
    """Otherwise a session re-picking would have to avoid the model it is
    already on, which is the opposite of what a pin is for."""
    A._session_pin_set("sess-a", "groq", "qwen/qwen3.8-27b")
    assert A._pinned_elsewhere("sess-a") == set()


def test_an_expired_pin_is_not_taken():
    with A._session_pin_lock:
        A._session_pins["old"] = ("groq", "qwen/qwen3.8-27b", time.time() - 1)
    assert A._pinned_elsewhere() == set()


def test_the_same_model_on_another_provider_is_still_taken():
    """Identities, not provider+model pairs: the same model served by four
    providers is one model, and spreading across four providers serving it is
    not spreading at all."""
    A._session_pin_set("sess-a", "groq", "openai/gpt-oss-120b")
    taken = A._pinned_elsewhere()
    assert A._normalize_model_identity("gpt-oss-120b") in taken


# --------------------------------------------------------------------------- #
# The spread
# --------------------------------------------------------------------------- #

def test_the_second_agent_gets_a_different_model():
    A._session_pin_set("sess-a", "tokenrouter", "z-ai/glm-5.3-free")
    left = A._spread_pool(POOL, "sess-b")
    assert POOL[0] not in left
    assert len(left) == 2


def test_a_third_agent_gets_the_third_model():
    A._session_pin_set("sess-a", "tokenrouter", "z-ai/glm-5.3-free")
    A._session_pin_set("sess-b", "groq", "qwen/qwen3.8-27b")
    left = A._spread_pool(POOL, "sess-c")
    assert [c[1] for c in left] == ["cerebras"]


def test_when_every_model_is_taken_it_shares_rather_than_refusing():
    """Fail-open, and this matters more than the spreading: one strong model
    and six agents must still route."""
    for i, (_s, pid, model) in enumerate(POOL):
        A._session_pin_set("sess-%d" % i, pid, model)
    assert A._spread_pool(POOL, "sess-new") == POOL


def test_it_never_invents_a_candidate():
    left = A._spread_pool(POOL, "sess-x")
    assert all(c in POOL for c in left)


def test_a_broken_pin_store_does_not_break_routing(monkeypatch):
    monkeypatch.setattr(A, "_pinned_elsewhere",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert A._spread_pool(POOL, "sess-x") == POOL


def test_an_empty_pool_stays_empty():
    assert A._spread_pool([], "sess-x") == []


# --------------------------------------------------------------------------- #
# Wired where the choice is actually made
# --------------------------------------------------------------------------- #

def test_it_runs_where_a_session_pins():
    """Only the FIRST turn of a session reaches the pick; every later turn
    takes the pin. So this is the one moment knowing about siblings changes
    anything."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index("_pool = _spread_pool(_pool, _skey)")
    after = src[i:i + 400]
    assert "_weighted_pick(_pool" in after
    assert "_session_pin_set(_skey, pid, model)" in after


def test_ordinary_chat_is_untouched():
    """The spread is applied on the agentic path only -- a chat conversation
    has its own pin and no siblings to spread against."""
    src = open("app.py", encoding="utf-8").read()
    assert src.count("_spread_pool(") == 2      # the definition, and one use


# --------------------------------------------------------------------------- #
# A finished conversation holds nothing; spreading stays in class
# --------------------------------------------------------------------------- #
# MEASURED 2026-09-12: the workers of two earlier multi-session runs still
# counted as "holding" the four strongest models an hour later (the pin TTL is
# 4h), so a new run's workers were spread AWAY from them onto the weakest of
# the fleet -- one 7B model invented the files it claimed to have written.

def test_a_pin_can_be_released():
    A._session_pin_set("sess-a", "groq", "qwen/qwen3.8-27b")
    assert A._session_pin_drop("sess-a") is True
    assert A._pinned_elsewhere() == set()
    assert A._session_pin_drop("sess-a") is False
    assert A._session_pin_drop(None) is False


def test_ending_a_conversation_releases_its_model():
    src = open("app.py", encoding="utf-8").read()
    body = src[src.index("def api_agent_end_session("):]
    body = body[:body.index("\n@app.route")]
    assert "_session_pin_drop(session_id)" in body


def test_a_finished_worker_releases_its_model():
    src = open("app.py", encoding="utf-8").read()
    body = src[src.index("def _swarm_windows_turn("):]
    body = body[:body.index("\ndef ", 10)]
    assert "finally:" in body and "_session_pin_drop(session_id)" in body


def test_the_pin_is_keyed_by_the_session_when_there_is_one():
    """The hub's own id survives the CLI compacting its history (which changed
    the hashed key, and the model, mid-job) and can be released."""
    src = open("app.py", encoding="utf-8").read()
    assert "_skey = _build_sid() or _session_key(messages)" in src


def test_spreading_does_not_reach_down_to_junk():
    """The strong model is held by a sibling; the only free ones are far
    weaker. Sharing the strong one beats a second opinion from a 7B."""
    A._session_pin_set("sess-a", "tokenrouter", "z-ai/glm-5.3-free")
    A._session_pin_set("sess-b", "groq", "qwen/qwen3.8-27b")
    A._session_pin_set("sess-c", "cerebras", "gpt-oss-120b")
    pool = POOL + [(90.0, "kilocode", "thinkingmachines/inkling-small:free")]
    assert A._spread_pool(pool, "sess-new") == pool


def test_spreading_still_takes_a_close_competitor():
    A._session_pin_set("sess-a", "tokenrouter", "z-ai/glm-5.3-free")
    left = A._spread_pool(POOL, "sess-new")
    assert [c[2] for c in left] == ["qwen/qwen3.8-27b", "gpt-oss-120b"]


def test_the_band_is_a_few_points_not_twenty():
    assert 5.0 <= A._SPREAD_MAX_DROP <= 15.0


def test_a_chat_pin_holds_nothing():
    """The swarm planner's own request pinned the strongest model for four
    hours, so the workers it had just planned for were spread away from it
    (MEASURED 2026-09-12: "held elsewhere 3" with one worker running). A chat
    pin keeps that conversation coherent; it does not hold the model."""
    A._session_pin_set("chat-1", "groq", "qwen/qwen3.8-27b", agentic=False)
    assert A._session_pin_get("chat-1") == ("groq", "qwen/qwen3.8-27b"), "still pinned for itself"
    assert A._pinned_elsewhere() == set()


def test_a_working_session_does():
    A._session_pin_set("work-1", "groq", "qwen/qwen3.8-27b")
    assert A._normalize_model_identity("qwen/qwen3.8-27b") in A._pinned_elsewhere()


def test_the_plain_chat_path_pins_as_a_chat():
    src = open("app.py", encoding="utf-8").read()
    assert "_session_pin_set(_ckey, pid, model, agentic=False)" in src
