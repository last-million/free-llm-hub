"""The hub's own one-line calls try a fast, capable model first (Groq's qwen3.8 when it fits).

ASKED 2026-11-09: "groq is almost never used, even though qwen 3.8 is better than glm 4.6/4.7".

WHY IT IS RARELY USED (measured, not a routing bug): Groq's free tier caps one request at
~7,000 input tokens (its own 413: "ITPM Limit 7000"; docs: 8K tokens/minute for
qwen/qwen3.8-27b) while every CLI agent turn is 15K-150K tokens. In the hub log Groq was tried on
those turns and refused 120 of the 234 requests that ended in a 503 (37K-150K tokens each). No
number of keys changes it: the limit is per organization. A request that FITS is served by it:
two tiny probes through `auto` and `best` both came back from groq/qwen/qwen3.8-27b.

WHAT CAN BE IMPROVED: the hub's own tiny calls. The skill picker and the Multi "work or chat"
verdict used the "medium" route (the strongest model, often a slow thinker) for a reply of one
word. They now try the router's SIMPLE pick first when it is a real model, with the old path
behind it. Everything here is hermetic: the router, the dispatch and the scores are faked.
"""
import app

# tests/conftest.py replaces app._multi_intent_by_model with "no verdict" for every test
# (autouse); the real one is taken here, at import time, before any fixture runs.
_REAL_INTENT = app._multi_intent_by_model


class _Resp:
    def __init__(self, status=200, text="NONE"):
        self.status_code = status
        self._text = text

    def json(self):
        return {"choices": [{"message": {"content": self._text}}]}

    def close(self):
        pass


GROQ = ("groq", "qwen/qwen3.8-27b")
MERCURY = ("inception", "mercury-3")
GLM = ("nvidia", "z-ai/glm-5.3")
KILO = ("kilocode", "recursive/flint-v1.0:free")
SCORES = {GROQ: 133.1, MERCURY: 26.0, GLM: 138.85, KILO: 90.0}


def _wire(monkeypatch, simple=GROQ, medium=GLM, replies=None, rescue=(MERCURY,),
          rescue_pids=("inception",), flag=True):
    """Fake the router, the chain, the scores and the dispatch; return the call log."""
    calls = []
    asked = []
    replies = replies or {}

    def route(messages, max_tokens=None, est=None, require_tools=False,
              force_difficulty=None, quality_mode=False):
        asked.append(force_difficulty)
        pair = simple if force_difficulty == "simple" else medium
        if pair is None:
            return None, None, force_difficulty
        return pair[0], pair[1], force_difficulty

    def dispatch(pid, payload, seconds):
        calls.append((pid, payload["model"]))
        return replies.get((pid, payload["model"]), _Resp(200, "NONE")), None

    monkeypatch.setattr(app, "_route_by_difficulty", route)
    monkeypatch.setattr(app, "_build_chain", lambda pid, model: [(pid, model), KILO])
    monkeypatch.setattr(app, "_rescue_pairs", lambda est=0, tools=False: list(rescue))
    monkeypatch.setattr(app, "_rescue_pids", lambda: frozenset(rescue_pids))
    monkeypatch.setattr(app, "_is_sub", lambda pid: pid.startswith("sub-"))
    monkeypatch.setattr(app, "_benchmark_score", lambda pid, model: SCORES.get((pid, model), 10.0))
    monkeypatch.setattr(app, "_dispatch_chat_with_deadline", dispatch)
    monkeypatch.setattr(app.config, "get_flag",
                        lambda name, default=True: flag if name == "helpers_fast_first" else default)
    return calls, asked


# ----------------------------------------------------------------- the helper itself
def test_it_returns_the_simple_pick_when_it_is_a_real_model(monkeypatch):
    _wire(monkeypatch)
    assert app._fast_helper_pairs([{"role": "user", "content": "x"}], 8) == [GROQ]


def test_it_asks_the_simple_route_not_the_strongest(monkeypatch):
    _calls, asked = _wire(monkeypatch)
    app._fast_helper_pairs([{"role": "user", "content": "x"}], 8)
    assert asked == ["simple"]


def test_a_weak_pick_is_not_trusted(monkeypatch):
    """A cheap pick under the bar (a 4096-token 7B, a tiny relay) is not given the job."""
    _wire(monkeypatch, simple=("groq", "allam-2-7b"))
    assert app._fast_helper_pairs([{"role": "user", "content": "x"}], 8) == []


def test_a_rescue_provider_is_never_the_fast_pick(monkeypatch):
    """Mercury only catches what failed; it must not enter through this door."""
    monkeypatch.setitem(SCORES, MERCURY, 120.0)
    _wire(monkeypatch, simple=MERCURY)
    assert app._fast_helper_pairs([{"role": "user", "content": "x"}], 8) == []


def test_a_subscription_is_never_the_fast_pick(monkeypatch):
    monkeypatch.setitem(SCORES, ("sub-claude", "sonnet"), 138.0)
    _wire(monkeypatch, simple=("sub-claude", "sonnet"))
    assert app._fast_helper_pairs([{"role": "user", "content": "x"}], 8) == []


def test_nothing_routable_gives_nothing(monkeypatch):
    _wire(monkeypatch, simple=None)
    assert app._fast_helper_pairs([{"role": "user", "content": "x"}], 8) == []


def test_a_router_that_raises_is_fail_open(monkeypatch):
    _wire(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("router down")

    monkeypatch.setattr(app, "_route_by_difficulty", boom)
    assert app._fast_helper_pairs([{"role": "user", "content": "x"}], 8) == []


def test_flag_off_gives_nothing_and_never_asks_the_router(monkeypatch):
    _calls, asked = _wire(monkeypatch, flag=False)
    assert app._fast_helper_pairs([{"role": "user", "content": "x"}], 8) == []
    assert asked == []


# ----------------------------------------------------------------- the skill picker
def _pick(monkeypatch, **kw):
    calls, asked = _wire(monkeypatch, **kw)
    with app.app.app_context():
        got = app._sk_call([{"role": "user", "content": "build me a landing page"}],
                           ("web_design",))
    return got, calls, asked


def test_the_picker_asks_the_fast_model_first_and_stops_there(monkeypatch):
    got, calls, _asked = _pick(monkeypatch)
    assert got == ()                       # NONE is a clear verdict
    assert calls == [GROQ]                 # one fast hop; the slow strong model was not spent


def test_the_picker_falls_through_when_the_fast_hop_refuses(monkeypatch):
    got, calls, _ = _pick(monkeypatch, replies={GROQ: _Resp(429)})
    assert got == ()
    assert calls[0] == GROQ
    assert calls[1] == MERCURY             # the rescue provider is still next, as before


def test_the_picker_falls_through_when_the_fast_hop_is_unreadable(monkeypatch):
    got, calls, _ = _pick(monkeypatch, replies={GROQ: _Resp(200, "")})
    assert got == ()
    assert calls[:2] == [GROQ, MERCURY]


def test_the_picker_flag_off_is_the_old_order(monkeypatch):
    got, calls, asked = _pick(monkeypatch, flag=False)
    assert got == ()
    assert calls == [MERCURY]              # rescue first, exactly as before
    assert "simple" not in asked


def test_the_picker_does_not_try_the_same_pair_twice(monkeypatch):
    """If the fast pick is also what the old path offers, it is asked once."""
    got, calls, _ = _pick(monkeypatch, medium=GROQ,
                          replies={GROQ: _Resp(200, ""), MERCURY: _Resp(200, ""),
                                   KILO: _Resp(200, "")})
    assert got is None                     # nobody gave a readable verdict
    assert calls.count(GROQ) == 1


# ----------------------------------------------------------------- the Multi verdict
def _intent(monkeypatch, text="fix the login bug", **kw):
    calls, asked = _wire(monkeypatch, **kw)
    return _REAL_INTENT(text), calls, asked


def test_the_multi_verdict_asks_the_fast_model_first(monkeypatch):
    verdict, calls, _ = _intent(monkeypatch, replies={GROQ: _Resp(200, "WORK")})
    assert verdict == "work"
    assert calls == [GROQ]


def test_the_multi_verdict_falls_through_on_a_refusal(monkeypatch):
    verdict, calls, _ = _intent(monkeypatch, replies={GROQ: _Resp(429),
                                                      GLM: _Resp(200, "chat")})
    assert verdict == "chat"
    assert calls[:2] == [GROQ, GLM]


def test_the_multi_verdict_flag_off_is_the_old_path(monkeypatch):
    verdict, calls, asked = _intent(monkeypatch, flag=False,
                                    replies={GLM: _Resp(200, "WORK")})
    assert verdict == "work"
    assert calls == [GLM]
    assert asked == ["medium"]


def test_the_multi_verdict_works_when_only_the_fast_pair_exists(monkeypatch):
    """The medium route found nothing (the old code returned None here); the fast pair
    can still answer."""
    verdict, calls, _ = _intent(monkeypatch, medium=None, replies={GROQ: _Resp(200, "WORK")})
    assert verdict == "work"
    assert calls == [GROQ]


def test_the_multi_verdict_with_no_model_at_all_is_still_none(monkeypatch):
    verdict, calls, _ = _intent(monkeypatch, simple=None, medium=None)
    assert verdict is None and calls == []
