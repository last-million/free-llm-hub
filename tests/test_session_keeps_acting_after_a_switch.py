"""Switching category keeps the memory AND the session keeps working, in any CLI.

Owner, 2026-10-28: "even switching between model categories he should keep the same memory and
be able to continue normally, in any CLI -- without starting a new conversation."

MEASURED on the owner's real opencode session (structural counts only; the content is private):
on `context-max` 149 of 162 assistant turns made a tool call; the moment the picker went to
`coding-max` the next 6 made NONE -- long text answers to short "carry on" messages, five of them
from ONE g4f relay pair, all held by the 4 h session pin. A category is a different POOL: the pair
that was doing the work (mercury-3 is in `context` by its 1M window and never in `coding`) may be
out of reach, and the new pool's pairs may answer in prose. The never-chains ledger could not see
it (it counts continuations only; these were fresh user messages).

What these tests pin:
  * MEMORY  the conversation key and the pin key do not depend on the model/category, for the
            request shape of each CLI; and the second request after a switch still carries the
            earlier turns (the history the CLI re-sends) through the real handler.
  * WATCH   a pair that has NEVER made a tool call in a session that clearly works with tools,
            and answers `_QUIET_STREAK` turns in a row with text, is released: pin dropped, pair
            demoted for THAT session only (reorder, never a drop), global never-chains nudged.
            A pair that HAS acted in the session is never judged; a tool call clears the streak.
  * RETURN  the pair that last made a tool call in the session is put back in front through the
            orchestrator seam, even outside the new category -- unless the owner chose an
            orchestrator himself.
  * END TO END  the three real handlers (chat = opencode, responses = codex, messages = Claude
            Code), stream and non-stream.
"""
import json
import os
import shutil
import tempfile
import time

import pytest

import app as A
import ctxwin
import config

# the fake fleet and the CLI request builders of the parallel-agents harness
from test_parallel_agents_diversity import (   # noqa: F401  (world is a fixture)
    FLEET, build_request, world, _Resp, _reset_state)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    root = tempfile.mkdtemp(prefix="hub-pytest-quiet-")
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", os.path.join(root, "state", "config.json"))
    with A._quiet_lock:
        A._quiet_text.clear()
        A._quiet_ban.clear()
        A._session_acted.clear()
        A._session_return.clear()
    A._chain_seen.clear()
    try:
        yield
    finally:
        with A._quiet_lock:
            A._quiet_text.clear()
            A._quiet_ban.clear()
            A._session_acted.clear()
            A._session_return.clear()
        A._chain_seen.clear()
        shutil.rmtree(root, ignore_errors=True)


P = ("g4f", "GLM:GLM-5.3")          # the pair that goes quiet
ACTOR = ("mistral", "mistral-large-3")   # the pair that was acting (outside any top band)
TEXT = "Here is where the work stands: the billing module is wired and the tests are green."


def _file(key, pid_model, called, text=TEXT, results=5):
    A._file_session_reply(key, results, pid_model[0], pid_model[1], called, text)


# --------------------------------------------------------------------------- #
# 1. THE WATCH -- the rule, both ways
# --------------------------------------------------------------------------- #

def test_a_pair_that_never_acted_goes_quiet_after_two_text_replies():
    A._session_pin_set("k1", P[0], P[1])
    _file("k1", P, called=False)
    assert ("k1",) + P not in A._quiet_ban, "one text reply is not a streak"
    assert A._session_pin_get("k1") is not None
    _file("k1", P, called=False)
    assert A._quiet_ban.get(("k1",) + P, 0) > time.time(), "two in a row = quiet"
    assert A._session_pin_get("k1") is None, "the pin that held the pair is released"


def test_the_global_never_chains_ledger_is_nudged_by_a_quiet_session():
    _file("k1", P, called=False)
    _file("k1", P, called=False)
    assert A._chain_seen[P]["text"] == 1       # +1 per quiet session; _never_chain_min() sessions demote it everywhere


def test_a_session_without_tool_use_is_never_judged():
    for _ in range(6):
        _file("k1", P, called=False, results=A._QUIET_MIN_TOOL_RESULTS - 1)
    assert not A._quiet_ban and not A._quiet_text


def test_an_empty_reply_is_not_a_quiet_reply():
    for _ in range(6):
        _file("k1", P, called=False, text="   ")
    assert not A._quiet_ban


def test_a_pair_that_has_acted_in_the_session_is_never_judged():
    _file("k1", P, called=True)
    for _ in range(6):
        _file("k1", P, called=False)           # its text is an answer, e.g. a question being answered
    assert not A._quiet_ban


def test_a_tool_call_clears_the_streak_and_the_ban():
    _file("k1", P, called=False)
    _file("k1", P, called=True)
    _file("k1", P, called=False)
    assert not A._quiet_ban, "the streak restarted at the tool call"
    _file("k2", P, called=False)
    _file("k2", P, called=False)
    assert A._quiet_ban
    _file("k2", P, called=True)
    assert not A._quiet_ban, "a pair that proves it can act here is forgiven"


def test_the_ban_belongs_to_one_session_only():
    _file("k1", P, called=False)
    _file("k1", P, called=False)
    with A.app.test_request_context("/v1/chat/completions", method="POST"):
        A.g.hub_quiet_key = "k1"
        assert A._quiet_in_session(*P) is True
        assert A._tool_turn_sick(*P) is True, "every pick / chain order goes through this test"
        A.g.hub_quiet_key = "k2"
        assert A._quiet_in_session(*P) is False
        A.g.hub_quiet_key = None
        assert A._quiet_in_session(*P) is False


def test_outside_a_request_the_ban_test_is_false_not_an_error():
    _file("k1", P, called=False)
    _file("k1", P, called=False)
    assert A._quiet_in_session(*P) is False      # a worker thread has no request context


def test_the_watch_can_be_switched_off(monkeypatch):
    real = A.config.get_flag
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=None: False if k == "session_quiet_watch" else real(k, d))
    _file("k1", P, called=False)
    _file("k1", P, called=False)
    assert not A._quiet_ban and not A._quiet_text


def test_the_tables_stay_bounded():
    now = time.time()
    with A._quiet_lock:
        for i in range(A._QUIET_MAX_SESSIONS + 50):
            A._session_acted["s%d" % i] = {("p", "m"): now - i}
        A._quiet_prune(now)
    assert len(A._session_acted) <= A._QUIET_MAX_SESSIONS


# --------------------------------------------------------------------------- #
# 2. THE RETURN -- back to whoever was acting, through the orchestrator seam
# --------------------------------------------------------------------------- #

def test_the_pair_that_was_acting_is_remembered_and_returned_to():
    _file("k1", ACTOR, called=True)
    _file("k1", P, called=False)
    assert A._session_return_pair("k1") is None, "not yet quiet"
    _file("k1", P, called=False)
    assert A._session_return_pair("k1") == ACTOR


def test_no_actor_means_no_return_just_a_ban():
    _file("k1", P, called=False)
    _file("k1", P, called=False)
    assert A._session_return_pair("k1") is None
    assert A._quiet_ban


def test_the_most_recent_actor_is_the_one_returned_to():
    _file("k1", ("a", "old"), called=True)
    time.sleep(0.01)
    _file("k1", ("b", "recent"), called=True)
    _file("k1", P, called=False)
    _file("k1", P, called=False)
    assert A._session_return_pair("k1") == ("b", "recent")


def test_the_return_can_be_switched_off(monkeypatch):
    real = A.config.get_flag
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=None: False if k == "session_return_to_actor" else real(k, d))
    _file("k1", ACTOR, called=True)
    _file("k1", P, called=False)
    _file("k1", P, called=False)
    assert A._session_return_pair("k1") is None and A._quiet_ban


def _apply(monkeypatch, usable=True, explicit=None, key="k1", diff="simple"):
    monkeypatch.setattr(A, "_agent_pin_key", lambda messages: key)
    monkeypatch.setattr(A, "_orch_unusable",
                        lambda pid, model, est, tools, images, veto: None if usable else "throttled")
    monkeypatch.setattr(A, "_orch_effective", lambda k: (explicit or "auto", "global"))
    with A.app.test_request_context("/v1/chat/completions", method="POST"):
        out = A._apply_orchestrator("nvidia", "z-ai/glm-5.3", [], est=1000, tools=True,
                                    body={}, diff=diff)
        return out, getattr(A.g, "hub_orchestrator_pair", None)


def test_apply_orchestrator_puts_the_acting_pair_in_front(monkeypatch):
    _file("k1", ACTOR, called=True)
    _file("k1", P, called=False)
    _file("k1", P, called=False)
    out, led = _apply(monkeypatch)
    assert out == ACTOR and led == ACTOR
    # a "continue" nudge is classed `simple`: the recovery still applies to it (done above, diff=simple)


def test_an_unusable_acting_pair_leaves_the_routers_pick_alone(monkeypatch):
    _file("k1", ACTOR, called=True)
    _file("k1", P, called=False)
    _file("k1", P, called=False)
    out, led = _apply(monkeypatch, usable=False)
    assert out == ("nvidia", "z-ai/glm-5.3") and led is None


def test_the_owners_own_orchestrator_choice_wins(monkeypatch):
    _file("k1", ACTOR, called=True)
    _file("k1", P, called=False)
    _file("k1", P, called=False)
    # a normal-size, non-simple turn (a simple small turn keeps the router's pick by the owner's
    # earlier rule, orchestrator or not)
    out, led = _apply(monkeypatch, explicit="zenmux/mimo-v2.6-pro", diff="hard")
    assert out == ("zenmux", "mimo-v2.6-pro")
    assert led == ("zenmux", "mimo-v2.6-pro"), "and not the pair the session would return to"


def test_another_session_is_not_sent_back(monkeypatch):
    _file("k1", ACTOR, called=True)
    _file("k1", P, called=False)
    _file("k1", P, called=False)
    out, led = _apply(monkeypatch, key="k2")
    assert out == ("nvidia", "z-ai/glm-5.3") and led is None


# --------------------------------------------------------------------------- #
# 3. MEMORY ACROSS A SWITCH -- the keys, per CLI request shape
# --------------------------------------------------------------------------- #

SWITCH_MODELS = ["context-max", "coding-max", "coding-swarm", "auto", "best", "reasoning-max",
                 "vision", "multi", "swarm", "nvidia/z-ai/glm-5.3"]
CLIS_FOR_KEYS = ("opencode", "codex", "claude")


@pytest.mark.parametrize("cli", CLIS_FOR_KEYS)
def test_the_conversation_and_pin_keys_ignore_the_model_for_every_cli(cli):
    convs, pins = set(), set()
    for model in SWITCH_MODELS:
        _path, kw = build_request(cli, 3, model, turn=2)
        body, headers = kw["json"], kw.get("headers") or {}
        conv = ctxwin.conversation_key(headers=headers, body=body)
        convs.add(conv)
        pins.add(ctxwin.pin_key(conv, headers=headers))
    assert len(convs) == 1 and len(pins) == 1, (cli, convs, pins)


# --------------------------------------------------------------------------- #
# 4. END TO END through the real handlers
# --------------------------------------------------------------------------- #

CLIS = ("opencode", "codex", "claude")


class Recorder:
    """A fake upstream: the pairs in `quiet` answer in PROSE even though tools are offered, every
    other pair makes a tool call. Remembers what each hop was sent."""

    def __init__(self):
        self.quiet = set()
        self.arm_first = False    # True: the first pair served becomes the quiet one
        self.served = []          # (pid, model)
        self.payloads = []        # (pid, model, payload)

    def arm(self):
        """Whichever pair OPENS the session answers in prose from now on. (The router's first
        pick is a weighted random choice among equally good pairs: it cannot be named.)"""
        self.quiet = set()
        self.arm_first = True

    @property
    def down(self):
        return next(iter(self.quiet), None)

    def dispatch(self, pid, payload, stream):
        pair = (pid, payload.get("model"))
        if self.arm_first and not self.quiet:
            self.quiet = {pair}
        self.served.append(pair)
        self.payloads.append((pid, payload.get("model"), payload))
        return _Resp(tool=pair not in self.quiet)


@pytest.fixture
def rec(world, monkeypatch):
    r = Recorder()
    monkeypatch.setattr(A, "_dispatch_chat", r.dispatch)
    # the request builder's second turn carries ONE tool round; the production floor is 3
    monkeypatch.setattr(A, "_QUIET_MIN_TOOL_RESULTS", 1)
    # _orch_unusable asks the LIVE catalog whether a provider "offers" the model; the fake fleet is
    # not in it (the check has its own tests -- test_orchestrator.py)
    monkeypatch.setattr(A, "_orch_unusable", lambda *a, **k: None)
    # DETERMINISTIC pick: always the best-scored candidate. The production pick is a weighted
    # random choice among equally good pairs, which would hide a router that ignores the ban
    # (a released pin re-rolls and usually lands elsewhere by luck). With this, the quiet pair
    # would be chosen again every single time unless the ban really keeps it out.
    monkeypatch.setattr(A, "_weighted_pick",
                        lambda pool, sustain_override=None: max(pool, key=lambda c: c[0]))
    return r


def _post(cli, model, stream=False, turn=2):
    path, kw = build_request(cli, 0, model, turn=turn, stream=stream)
    resp = A.app.test_client().post(path, **kw)
    resp.get_data()
    assert resp.status_code == 200, (cli, model, resp.status_code)
    return resp


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("cli", CLIS)
def test_a_quiet_pair_is_left_after_two_turns_in_every_cli(rec, cli, stream):
    _reset_state()
    rec.arm()
    _post(cli, "auto", stream)
    down = rec.down
    assert rec.served[-1] == down, "the pair that opens the session answers in prose"
    _post(cli, "auto", stream)                          # second quiet turn -> the watch fires
    assert rec.served[-1] == down, "the pin still holds it until the streak completes"
    assert any(k[1:] == down for k in A._quiet_ban), "two prose turns in a tool session = quiet"
    _post(cli, "auto", stream)
    assert rec.served[-1] != down, "the third turn must not go back to the pair that went quiet"


@pytest.mark.parametrize("cli", CLIS)
def test_the_session_returns_to_the_pair_that_was_acting_even_outside_the_pool(rec, cli):
    _reset_state()
    rec.arm()
    _post(cli, "auto")                                  # the first pick answers in prose (streak 1)
    down = rec.down
    assert down != ACTOR, "the test needs the acting pair to be a DIFFERENT, weaker one"
    (key,) = {k[0] for k in A._quiet_text}              # the session key the hub itself used
    with A._quiet_lock:                                 # ...and a weak pair had been acting in it
        A._session_acted[key] = {ACTOR: time.time()}
    _post(cli, "auto")                                  # streak 2 -> quiet, released, return set
    assert A._session_return_pair(key) == ACTOR
    _post(cli, "auto")
    assert rec.served[-1] == ACTOR, "continue with the pair that was doing the work"


@pytest.mark.parametrize("cli", CLIS)
def test_after_a_category_switch_the_next_request_still_carries_the_earlier_turns(rec, cli):
    """MEMORY: the history the CLI re-sends reaches the new category's model unchanged."""
    _reset_state()
    _post(cli, "context-max")
    _post(cli, "coding-max")
    assert len(rec.payloads) >= 2
    for _pid, _model, payload in rec.payloads[-2:]:
        blob = json.dumps(payload.get("messages") or [], default=str)
        assert "a.py b.py" in blob, "the earlier tool result is still in what the model reads"
        assert "TASK-0" in blob, "and so is the user's first instruction"


def test_a_switch_does_not_change_which_session_the_ban_belongs_to(rec):
    _reset_state()
    rec.arm()
    _post("opencode", "context-max")
    _post("opencode", "coding-max")                     # the picker moved; same session id
    keys = {k[0] for k in A._quiet_text} | {k[0] for k in A._quiet_ban}
    assert len(keys) == 1, "one conversation, one key, whatever category it names"


def test_with_the_watch_off_a_quiet_pair_keeps_the_session(rec, monkeypatch):
    real = A.config.get_flag
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=None: False if k == "session_quiet_watch" else real(k, d))
    _reset_state()
    rec.arm()
    for _ in range(4):
        _post("opencode", "auto")
    assert {s for s in rec.served} == {rec.down}, "the old behaviour: the pin holds it"
    assert not A._quiet_ban
