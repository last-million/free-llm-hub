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

MAX_RETRIES = 2                  # automatic retries per user instruction; then stop and tell the user

# Why a turn is judged unfinished (for logs and the activity row).
NO_PROGRESS = "no_progress"      # nothing run, nothing changed, no real answer
INCOMPLETE = "incomplete"        # did work but the last message is not a finished answer
ERROR = "error"                  # the turn ended in an error (provider/model failure)
# Why a turn is judged done.
DONE = "done"                    # progress + a real answer
DONE_DIRECT = "done_direct"      # a real answer with no tools (a question answered in prose)
EXHAUSTED = "exhausted"          # already retried MAX_RETRIES times; hand back to the user


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


def assess(made_progress, answer_ok, errored=False, attempts_used=0, max_retries=MAX_RETRIES):
    """One ended turn -> Decision. Inputs are what the caller already knows:

    - made_progress: the turn ran a tool or changed a file (it did SOMETHING).
    - answer_ok: its final message reads as a real, finished answer (the caller judges this
      with the hub's answer_check -- NOT blank-after-thinking, not an idle greeting, not a
      'blocked/gave up' line, not an announcement of a next step it never took).
    - errored: the turn ended on an error event (a provider/model failure, not an answer).
    - attempts_used: how many automatic retries this user instruction already had (0 on the
      first natural end).

    Never raises; any surprise falls through to 'finished, do not retry' -- the safe side (the
    user still has the turn; nothing is spent)."""
    try:
        attempts_used = max(0, int(attempts_used))
        if attempts_used >= max(0, int(max_retries)):
            return Decision(False, EXHAUSTED)
        nxt = attempts_used + 1                       # the retry this would be (1-based)
        if errored:
            # the model/provider failed to produce a turn at all -> try another model
            return Decision(True, ERROR, switch_model=True, attempt=nxt)
        if answer_ok:
            # a real, finished answer -- with or without tools -- is DONE
            return Decision(False, DONE if made_progress else DONE_DIRECT)
        # no finished answer:
        if made_progress:
            # it did work but stopped short / announced a next step: let the SAME model
            # continue once, then switch if it stalls again
            return Decision(True, INCOMPLETE, switch_model=attempts_used >= 1, attempt=nxt)
        # nothing done and no answer: one more go on the same model, then switch
        return Decision(True, NO_PROGRESS, switch_model=attempts_used >= 1, attempt=nxt)
    except Exception:                                 # noqa: BLE001
        return Decision(False, DONE)
