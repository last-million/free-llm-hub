"""A dead model that never gets marked dead poisons the WHOLE chain, not just
its own hop.

THE BUG (found live): nvidia/qwen3-next-80b-a3b-instruct is still listed in
/models but 410s ("Gone") on every real generation. _DEAD_STATUSES was
(402, 403, 404) -- 410 wasn't in it, so _mark_model_dead(pid, model, 410) was
a silent no-op every time, and the model kept winning a chain slot on request
after request.

That alone would just waste one hop per request. The real damage: _retryable()
correctly refuses to retry a 410 (it's not 429/5xx), which sets last_hard for
the WHOLE request -- and the chain-exhaustion handler only offers its bonus
whole-chain retry when NOTHING hard failed. So the one dead hop turned "every
OTHER failure this round was a survivable 429" into a hard 503 on every
request that happened to draw it. Measured: two consecutive Codex turns 503'd
this way, each burning all 10 configured hops first.

410 is a STRONGER signal than 404 (which was already in _DEAD_STATUSES) --
"used to exist, now permanently gone" vs. plain "not found" -- so there is no
reason it should have been treated as harder-to-classify than 404.
"""
import time

import app


def test_410_is_now_recognised_as_a_dead_model_status():
    assert 410 in app._DEAD_STATUSES
    # 400 stays excluded on purpose (see the comment above _DEAD_STATUSES): a
    # bad payload is not the same claim as a permanently gone model, and
    # blocklisting a good model off one malformed request would be worse.
    assert 400 not in app._DEAD_STATUSES


def test_a_410_actually_marks_the_model_dead_and_it_self_heals(monkeypatch):
    pid, model = "nvidia", "qwen/qwen3-next-80b-a3b-instruct"
    with app._dead_lock:
        app._dead_models.pop((pid, model), None)
    try:
        assert not app._is_model_dead(pid, model)
        app._mark_model_dead(pid, model, 410)
        assert app._is_model_dead(pid, model), \
            "a 410 must sideline the model exactly like a 404 already does"

        # Self-heals after the TTL, same as every other entry in this table --
        # simulate expiry rather than sleeping 6h.
        with app._dead_lock:
            app._dead_models[(pid, model)] = time.time() - 1
        assert not app._is_model_dead(pid, model)
    finally:
        with app._dead_lock:
            app._dead_models.pop((pid, model), None)
