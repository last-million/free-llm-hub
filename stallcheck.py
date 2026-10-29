"""Did a hub-owned agent turn actually FINISH, or did it stall -- and if it stalled, retry on
the same model or switch to another?

A human watching an agent asks a simple question when a turn ends: did it do anything, and does
its last message actually answer -- or did it give up, announce a next step it never took, error
out, or say nothing? This module is that judgment, as a pure function. It decides ONLY from
booleans the caller computes (so it never imports app/answer_check and is fully testable); the
caller owns reading the turn's events and judging its final text with the hub's own helpers.

HARD BOUNDS, so an auto-retry can never loop or run up a bill:
  * at most MAX_RETRIES (2) automatic retries per user instruction;
  * a turn that made real progress AND gave a real answer is FINISHED -- never retried;
  * a turn with no progress whose last message IS a real answer (a prose question answered
    with no tools) is FINISHED too;
  * the first retry is the SAME model (a transient blip deserves one more go); the second, and
    any retry after an error, SWITCHES to another healthy model.
Nothing here spends a token or starts anything -- it returns a decision the caller acts on.
"""
from __future__ import annotations

MAX_RETRIES = 2                  # consecutive no-progress retries; then stop and tell the user
MAX_TOTAL_RETRIES = 25           # total auto-continues WHILE a chain keeps making new progress

# Why a turn is judged unfinished (for logs and the activity row).
NO_PROGRESS = "no_progress"      # nothing run, nothing changed, no real answer
INCOMPLETE = "incomplete"        # did work but the last message is not a finished answer
ERROR = "error"                  # the turn ended in an error (provider/model failure)
# Why a turn is judged done.
DONE = "done"                    # progress + a real answer
DONE_DIRECT = "done_direct"      # a real answer with no tools (a question answered in prose)
EXHAUSTED = "exhausted"          # stopped making new progress (the tight cap); hand back
COUNT = "count"                  # hit the total auto-continue cap; hand back to the user


class Decision:
    __slots__ = ("retry", "reason", "switch_model", "attempt")

    def __init__(self, retry, reason, switch_model=False, attempt=0):
        self.retry = bool(retry)
        self.reason = reason
        self.switch_model = bool(switch_model)
        self.attempt = int(attempt)          # 1-based index of the retry this would be

    def __repr__(self):
        return "Decision(retry=%r, reason=%r, switch_model=%r, attempt=%d)" % (
            self.retry, self.reason, self.switch_model, self.attempt)

    def as_dict(self):
        return {"retry": self.retry, "reason": self.reason,
                "switch_model": self.switch_model, "attempt": self.attempt}


def assess(made_progress, answer_ok, errored=False, attempts_used=0,
           unproductive_used=None, max_retries=MAX_RETRIES, max_total=MAX_TOTAL_RETRIES,
           progress_mode=True):
    """One ended turn -> Decision. Inputs are what the caller already knows:

    - made_progress: the turn did NEW, changing work (progress_mode: a tool-call signature not
      seen before in the chain, or a changed reply with a real tool/observed result -- the
      caller computes this against the chain so a repeated read-only `ls` is NOT progress; in
      the legacy mode, simply 'ran a tool / observed something').
    - answer_ok: its final message reads as a real, finished answer (the caller judges this
      with the hub's answer_check -- NOT blank-after-thinking, not an idle greeting, not a
      'blocked/gave up' line, not an announcement of a next step it never took).
    - errored: the turn ended on an error event (a provider/model failure, not an answer).
    - attempts_used: total automatic retries this user instruction already had (0 on the first
      natural end).
    - unproductive_used: CONSECUTIVE no-progress retries so far (the caller resets it to 0 on a
      productive continue); None falls back to attempts_used (the legacy single counter).

    progress_mode=False reproduces the original flat behaviour exactly (one cap of max_retries
    for every kind of unfinished turn). progress_mode=True (the default) splits the cap: a turn
    that made new progress keeps the SAME model and is bounded only by max_total (and, in the
    caller, a wall-clock budget); a turn that made no new progress keeps the tight max_retries
    cap and switches model after the first try, so a stuck turn gives up fast while a working
    one keeps going.

    Never raises; any surprise falls through to 'finished, do not retry' -- the safe side (the
    user still has the turn; nothing is spent)."""
    try:
        total = max(0, int(attempts_used))
        unprod = total if unproductive_used is None else max(0, int(unproductive_used))
        cap2 = max(0, int(max_retries))
        capN = max(cap2, int(max_total))
        nxt = total + 1                               # the retry this would be (1-based)
        if not progress_mode:
            if total >= cap2:
                return Decision(False, EXHAUSTED)
            if errored:
                return Decision(True, ERROR, switch_model=True, attempt=nxt)
            if answer_ok:
                return Decision(False, DONE if made_progress else DONE_DIRECT)
            if made_progress:
                return Decision(True, INCOMPLETE, switch_model=total >= 1, attempt=nxt)
            return Decision(True, NO_PROGRESS, switch_model=total >= 1, attempt=nxt)
        # progress_mode: two caps.
        if errored:
            # an error is an unproductive end -> tight cap, switch to another model
            if unprod >= cap2:
                return Decision(False, EXHAUSTED)
            if total >= capN:
                return Decision(False, COUNT)
            return Decision(True, ERROR, switch_model=True, attempt=nxt)
        if answer_ok:
            # a real, finished answer -- with or without tools -- is DONE
            return Decision(False, DONE if made_progress else DONE_DIRECT)
        if total >= capN:
            return Decision(False, COUNT)
        if made_progress:
            # real, changing work: keep the SAME model going (don't fight the session pin)
            return Decision(True, INCOMPLETE, switch_model=False, attempt=nxt)
        # no new work: tight cap, switch to another model after the first try
        if unprod >= cap2:
            return Decision(False, EXHAUSTED)
        return Decision(True, NO_PROGRESS, switch_model=unprod >= 1, attempt=nxt)
    except Exception:                                 # noqa: BLE001
        return Decision(False, DONE)
