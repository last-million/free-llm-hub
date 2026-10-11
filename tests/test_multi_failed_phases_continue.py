r"""Failed Multi phases continue by themselves (2026-10-11).

Owner: "sometimes some phases fail and it does not let them continue
automatically -- that is an issue, fix it." MEASURED on the owner's last 20 real
runs: 4 had failed phases -- a worker prompt over agentic_chat's per-turn cap
("Message is 27973 chars; capped at 24000 per turn."), "no output for 900s",
"opencode produced no reply.", and a phase still failing its tests after its one
revision. None was retried.

The fix (swarm_windows, flag `multi_auto_retry_failed`, default ON): a phase
that ends FAILED for a retryable reason is tried again in a FRESH session on a
DIFFERENT model (the existing rotation), up to PHASE_RETRIES extra attempts,
each told what the previous one failed on; a STOPPED phase (user stop, budget)
is never retried; dependents wait for the retry; the attempt count persists
across a resume; and a worker prompt over the cap no longer fails -- its bulky
context is moved to a hub-written brief file.

HERMETIC: `spawn` and `run_turn` are fakes; no network, no CLI, no real hub
state (its own FREE_LLM_HUB_SWARM_DIR per test). This is the ONE file where the
conftest leaves `multi_auto_retry_failed` ON (see
_multi_auto_retry_off_unless_tested)."""
import threading
import time

import pytest

import swarm_windows as SW


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setenv(SW._STORE_ENV, str(tmp_path / "runs"))
    # Keep the retry backoff tiny so these tests are fast (correctness, not
    # timing, is under test here).
    monkeypatch.setattr(SW, "RETRY_BACKOFF", 0.05)
    SW._RUNS.clear()
    yield
    for run in list(SW._RUNS.values()):
        run.stop_flag.set()
    SW._RUNS.clear()


def test_the_flag_is_on_in_this_file():
    """The conftest fixture leaves the feature ON here and OFF everywhere else."""
    assert SW._auto_retry_on() is True


_ids = [0]


def _spawn(cli, project):
    _ids[0] += 1
    return "sess-%d" % _ids[0]


def _wait(run_id, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        st = SW.status(run_id)
        if st and st["state"] in (SW.DONE, SW.FAILED, SW.STOPPED):
            return st
        time.sleep(0.02)
    return SW.status(run_id)


def _one(title="a", task="do the thing", needs=None):
    return {"title": title, "task": task, "needs": needs or []}


# --------------------------------------------------------------------------- #
# A retryable failure is tried again on a different model, and succeeds
# --------------------------------------------------------------------------- #

def test_a_no_reply_phase_is_retried_on_a_different_model_and_succeeds():
    tries = [0]
    siblings = []

    def run_turn(session_id, prompt):
        tries[0] += 1
        siblings.append((session_id, list(SW.sibling_sessions(session_id))))
        if tries[0] == 1:
            yield {"event": "error", "status": 502, "detail": "opencode produced no reply."}
        else:
            yield {"event": "message", "text": "built it on the retry"}

    rid = SW.start("g", ".", "opencode", _spawn, run_turn, phases=[_one()], review=False)
    st = _wait(rid)
    assert st["state"] == SW.DONE
    assert st["agents"][0]["summary"] == "built it on the retry"
    assert st["agents"][0]["attempts"] == 1            # one EXTRA attempt used
    assert tries[0] == 2
    # The retry ran in a different session, and the rotation was told about the
    # first one -- so app's _rotate_within_run picks another model for it.
    first_sid, _ = siblings[0]
    retry_sid, retry_sibs = siblings[1]
    assert retry_sid != first_sid
    assert first_sid in retry_sibs


def test_a_provider_error_phase_is_retried_and_the_next_attempt_is_told_why():
    seen_prompts = []
    tries = [0]

    def run_turn(session_id, prompt):
        tries[0] += 1
        seen_prompts.append(prompt)
        if tries[0] == 1:
            yield {"event": "error", "status": 503, "detail": "upstream exploded"}
        else:
            yield {"event": "message", "text": "ok now"}

    rid = SW.start("g", ".", "opencode", _spawn, run_turn, phases=[_one()], review=False)
    st = _wait(rid)
    assert st["state"] == SW.DONE
    # The second attempt's prompt names what the first one failed on.
    assert "A PREVIOUS ATTEMPT AT THIS PHASE FAILED" in seen_prompts[1]
    assert "upstream exploded" in seen_prompts[1]


def test_a_verification_failure_is_retried_after_its_one_revision():
    """A phase whose observed test keeps failing after its one revision is a
    retryable FAILED -- a fresh model gets a go."""
    tries = [0]

    def run_turn(session_id, prompt):
        tries[0] += 1
        # Attempts 1 and 2 (the first phase attempt + its one revision) run the
        # test and it FAILS; the third whole attempt (fresh model) passes.
        if tries[0] <= 2:
            yield {"event": "tool_result", "command": "pytest",
                   "exit_code": 1, "is_error": True,
                   "output_tail": "===== 1 failed in 0.10s =====",
                   "started_at": 0, "ended_at": 1}
            yield {"event": "message", "text": "tried, tests still red"}
        else:
            yield {"event": "tool_result", "command": "pytest",
                   "exit_code": 0, "is_error": False,
                   "output_tail": "===== 3 passed in 0.10s =====",
                   "started_at": 0, "ended_at": 1}
            yield {"event": "message", "text": "green now"}

    rid = SW.start("g", ".", "opencode", _spawn, run_turn, phases=[_one()], review=False)
    st = _wait(rid)
    assert st["state"] == SW.DONE
    assert st["agents"][0]["attempts"] >= 1
    assert st["agents"][0]["summary"] == "green now"


def test_a_silent_worker_is_unblocked_and_retried_on_a_different_model(monkeypatch):
    """A wedged turn (no output) is UNBLOCKED (stop ends the stuck CLI) and the
    worker thread retries on a fresh model, rather than being written off."""
    monkeypatch.setattr(SW, "AGENT_TIMEOUT", 3600.0)
    monkeypatch.setattr(SW, "AGENT_IDLE_TIMEOUT", 0.4)
    tries = [0]
    releases = {}
    stopped = []

    def stop(sid):
        stopped.append(sid)
        ev = releases.get(sid)
        if ev:
            ev.set()

    def run_turn(session_id, prompt):
        tries[0] += 1
        yield {"event": "tool", "text": "starting"}
        if tries[0] == 1:
            ev = threading.Event()
            releases[session_id] = ev
            ev.wait(10)                 # wedged until the scheduler stops us
            return                      # ...then end with no reply
        yield {"event": "message", "text": "finished on the retry"}

    rid = SW.start("g", ".", "opencode", _spawn, run_turn, phases=[_one()],
                   review=False, stop=stop)
    st = _wait(rid)
    assert st["state"] == SW.DONE
    assert st["agents"][0]["summary"] == "finished on the retry"
    assert st["agents"][0]["attempts"] == 1
    assert len(stopped) >= 1            # the stuck CLI was stopped to unblock it


# --------------------------------------------------------------------------- #
# Bounded, and transient store locks are NOT phase-retried
# --------------------------------------------------------------------------- #

def test_a_persistent_failure_stops_after_the_bounded_attempts():
    tries = [0]
    sessions = set()

    def run_turn(session_id, prompt):
        tries[0] += 1
        sessions.add(session_id)
        yield {"event": "error", "status": 502, "detail": "still broken"}

    rid = SW.start("g", ".", "opencode", _spawn, run_turn, phases=[_one()], review=False)
    st = _wait(rid)
    assert st["state"] == SW.FAILED
    assert tries[0] == 1 + SW.PHASE_RETRIES         # initial + the extra attempts
    assert st["agents"][0]["attempts"] == SW.PHASE_RETRIES
    assert len(sessions) == 1 + SW.PHASE_RETRIES    # a fresh session each time


def test_a_transient_store_lock_is_not_phase_retried():
    """A database lock is retried inside _attempts (AGENT_ATTEMPTS, same CLI);
    a phase-level retry would meet the same shared store, so it does not fire."""
    tries = [0]

    def run_turn(session_id, prompt):
        tries[0] += 1
        yield {"event": "error", "detail": "database is locked"}

    rid = SW.start("g", ".", "opencode", _spawn, run_turn, phases=[_one()], review=False)
    st = _wait(rid, timeout=60)
    assert st["state"] == SW.FAILED
    assert tries[0] == SW.AGENT_ATTEMPTS            # not multiplied by PHASE_RETRIES
    assert st["agents"][0]["attempts"] == 0


# --------------------------------------------------------------------------- #
# STOPPED is never retried; budget and Stop are honoured
# --------------------------------------------------------------------------- #

def test_a_stopped_phase_is_never_retried():
    tries = [0]

    def run_turn(session_id, prompt):
        tries[0] += 1
        for r in list(SW._RUNS.values()):          # a user Stop lands mid-turn
            r.stop_flag.set()
        yield {"event": "tool", "text": "cut off"}

    rid = SW.start("g", ".", "opencode", _spawn, run_turn, phases=[_one()], review=False)
    st = _wait(rid)
    assert st["state"] == SW.STOPPED
    assert st["agents"][0]["state"] == SW.STOPPED
    assert tries[0] == 1                            # STOPPED is never retried
    assert st["agents"][0]["attempts"] == 0


def test_a_spent_budget_prevents_a_retry():
    tries = [0]

    def run_turn(session_id, prompt):
        tries[0] += 1
        yield {"event": "error", "status": 502, "detail": "nope"}

    # The accountant reports a call as soon as one attempt ran; the cap is one
    # call, so after the first FAILED attempt the budget is reached -> no retry.
    def spent(run_id):
        return {"tokens": 0, "calls": tries[0]}

    rid = SW.start("g", ".", "opencode", _spawn, run_turn, phases=[_one()],
                   review=False, budget={"calls": 1}, spent=spent)
    st = _wait(rid)
    assert tries[0] == 1                            # ran once, budget blocked the retry
    assert st["agents"][0]["attempts"] == 0


# --------------------------------------------------------------------------- #
# Dependents wait for the retry
# --------------------------------------------------------------------------- #

def test_a_dependent_waits_for_the_retried_phase_to_succeed():
    phase1_tries = [0]
    phase1_state_when_2_ran = []

    def run_turn(session_id, prompt):
        # Phase 1's task carries a unique marker (a dependent's prompt quotes a
        # dep's TITLE and SUMMARY, never its task, so this never misfires).
        if "ALPHAMARKER" in prompt:
            phase1_tries[0] += 1
            if phase1_tries[0] == 1:
                yield {"event": "error", "status": 502, "detail": "produced no reply"}
            else:
                yield {"event": "message", "text": "ONE DONE"}
        else:
            # Phase 2: record what state phase 1 was in when phase 2 started.
            phase1_state_when_2_ran.append(list(SW._RUNS.values())[0].agents[0].state)
            yield {"event": "message", "text": "TWO DONE"}

    phases = [{"title": "Phase one", "task": "do ALPHAMARKER work", "needs": []},
              {"title": "Phase two", "task": "do beta work", "needs": [1]}]
    rid = SW.start("g", ".", "opencode", _spawn, run_turn, phases=phases, review=False)
    st = _wait(rid)
    assert st["state"] == SW.DONE
    assert st["agents"][0]["attempts"] == 1
    # Phase 2 never started while phase 1 was still being retried: it saw phase
    # 1 already DONE (dependents wait for the retry, not the first failure).
    assert phase1_state_when_2_ran == [SW.DONE]


# --------------------------------------------------------------------------- #
# The attempt count survives a resume ("continue")
# --------------------------------------------------------------------------- #

def test_the_attempt_count_persists_through_row_and_from_row():
    run = SW._Run("g", ".", "opencode", [_one()])
    run.agents[0].attempts = 2
    run.agents[0].last_failure = "no output for 900s"
    rebuilt = SW._Run.from_row(run.row(with_events=True))
    assert rebuilt.agents[0].attempts == 2
    assert rebuilt.agents[0].last_failure == "no output for 900s"


def test_a_resume_keeps_the_attempt_count_so_it_does_not_get_a_fresh_batch():
    """A phase that exhausted its auto-retries and failed, then a 'continue':
    the count is kept (bounded across restarts), so it gets the one resume run
    but no fresh batch of auto-retries."""
    mode = ["fail"]
    tries = [0]

    def run_turn(session_id, prompt):
        tries[0] += 1
        if mode[0] == "fail":
            yield {"event": "error", "status": 502, "detail": "broken"}
        else:
            yield {"event": "message", "text": "ok after continue"}

    rid = SW.start("g", ".", "opencode", _spawn, run_turn, phases=[_one()], review=False)
    st = _wait(rid)
    assert st["state"] == SW.FAILED
    assert st["agents"][0]["attempts"] == SW.PHASE_RETRIES
    before = tries[0]
    # "continue": the next attempt succeeds. The count is NOT reset.
    mode[0] = "ok"
    SW.resume(rid, _spawn, run_turn)
    st = _wait(rid)
    assert st["state"] == SW.DONE
    assert st["agents"][0]["attempts"] == SW.PHASE_RETRIES     # kept, not reset
    # The resume ran the phase once more; it did not open a fresh retry batch.
    assert tries[0] == before + 1


# --------------------------------------------------------------------------- #
# The over-long worker prompt now fits (the 24000-char bug)
# --------------------------------------------------------------------------- #

def test_an_over_cap_prompt_is_fitted_into_a_brief_file(tmp_path, monkeypatch):
    monkeypatch.setattr(SW, "_prompt_cap", lambda run: 2000)
    goal = "BUILD THE REAL THING: " + ("a very specific unique goal sentence. " * 400)
    run = SW._Run(goal, str(tmp_path), "opencode",
                  [{"title": "Big", "task": "implement the whole feature", "needs": []}])
    run.context = "earlier in the conversation we decided lots of things. " * 200
    prompt = SW._agent_prompt(run, run.agents[0])
    assert len(prompt) <= 2000, len(prompt)
    # The TASK (the instruction) stays inline; the folder rule is still there.
    assert "implement the whole feature" in prompt
    assert "THE PROJECT FOLDER IS:" in prompt
    # The bulky context went to a hub-written brief file the worker is pointed
    # at -- and the FULL goal is in it, never silently cut.
    assert "READ FIRST" in prompt
    briefs = list(tmp_path.glob(".calvoun-brief-ctx-*.md"))
    assert briefs, "a context brief file was written"
    body = briefs[0].read_text(encoding="utf-8")
    assert goal.strip()[:60] in body
    assert goal.strip()[-60:] in body


def test_an_over_cap_prompt_with_no_writable_folder_still_fits_and_keeps_the_goal(monkeypatch):
    monkeypatch.setattr(SW, "_prompt_cap", lambda run: 1500)
    goal = "SHIP IT: " + ("one unmistakable goal clause. " * 300)
    run = SW._Run(goal, "/no/such/folder/exists/here", "opencode",
                  [{"title": "Big", "task": "do the work", "needs": []}])
    prompt = SW._agent_prompt(run, run.agents[0])
    assert len(prompt) <= 1500
    assert "do the work" in prompt
    # The goal is bounded, not dropped: its opening words are visible inline.
    assert "SHIP IT:" in prompt


def test_a_prompt_under_the_cap_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(SW, "_prompt_cap", lambda run: 100000)
    run = SW._Run("small goal", str(tmp_path), "opencode", [_one()])
    assert SW._agent_prompt(run, run.agents[0]) == SW._full_agent_prompt(run, run.agents[0])


# --------------------------------------------------------------------------- #
# Flag OFF restores the old behaviour exactly
# --------------------------------------------------------------------------- #

def test_flag_off_keeps_the_old_one_shot_no_reply_retry(monkeypatch):
    monkeypatch.setattr(SW, "_auto_retry_on", lambda: False)
    tries = [0]

    def run_turn(session_id, prompt):
        tries[0] += 1
        yield {"event": "error", "status": 502, "detail": "opencode produced no reply."}

    rid = SW.start("g", ".", "opencode", _spawn, run_turn, phases=[_one()], review=False)
    st = _wait(rid)
    assert st["state"] == SW.FAILED
    # Old behaviour: the single in-_attempts no-reply retry, and no more.
    assert tries[0] == 1 + SW.NO_REPLY_RETRIES
    assert st["agents"][0]["attempts"] == 0


def test_flag_off_does_not_rewrite_an_over_cap_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(SW, "_auto_retry_on", lambda: False)
    monkeypatch.setattr(SW, "_prompt_cap", lambda run: 500)
    run = SW._Run("g " * 1000, str(tmp_path), "opencode", [_one()])
    # Flag off: the full prompt ships as-is (today's behaviour -- the cap bug and
    # all), and no brief file is written.
    assert SW._agent_prompt(run, run.agents[0]) == SW._full_agent_prompt(run, run.agents[0])
    assert not list(tmp_path.glob(".calvoun-brief-ctx-*.md"))


# --------------------------------------------------------------------------- #
# What the run reports
# --------------------------------------------------------------------------- #

def test_the_report_names_a_phase_that_failed_after_every_attempt():
    def run_turn(session_id, prompt):
        yield {"event": "error", "status": 502, "detail": "cannot do it"}

    rid = SW.start("g", ".", "opencode", _spawn, run_turn,
                   phases=[{"title": "Hard phase", "task": "t", "needs": []}], review=False)
    _wait(rid)
    text = SW.format_result(rid)
    assert "Phases still failing after %d attempts" % (1 + SW.PHASE_RETRIES) in text
    assert "Hard phase" in text


def test_attempt_view_words_the_retry():
    run = SW._Run("g", ".", "opencode", [_one()])
    a = run.agents[0]
    assert SW.attempt_view(a) is None               # first attempt: nothing to say
    a.attempts = 1
    a.state = SW.RUNNING
    v = SW.attempt_view(a)
    assert v["text"] == "retrying, attempt 2 of %d" % (1 + SW.PHASE_RETRIES)
    a.state = SW.FAILED
    assert SW.attempt_view(a)["text"] == "failed after 2 attempts"


# --------------------------------------------------------------------------- #
# A run resumed after a restart re-runs a phase that had FAILED before it
# (checked live 2026-10-11 06:22-06:26 on swarm-85890b169900: phase 1, failed
# "opencode produced no reply" before the restart, ran again after the resume
# and was then auto-continued on a different model, attempt 2 of 3).
# --------------------------------------------------------------------------- #

def test_a_resumed_run_runs_a_phase_that_failed_before_the_restart(monkeypatch):
    monkeypatch.setattr(SW, "SPAWN_STAGGER", 0)
    phases = [{"title": "A", "task": "do a", "needs": []},
              {"title": "B", "task": "do b", "needs": [1]},
              {"title": "C", "task": "do c", "needs": [1, 2]}]
    run = SW._Run("build it", ".", "opencode", phases)
    run.agents[0].state = SW.FAILED
    run.agents[0].error = "opencode produced no reply."
    run.agents[1].state = SW.RUNNING
    run.agents[2].state = SW.PENDING
    run.state = SW.RUNNING
    SW._persist(run)
    SW._RUNS.clear()
    assert SW.load() == 1
    ran = []

    def run_turn(session_id, prompt):
        ran.append("do " + "abc"[SW.worker_info(session_id)["index"] - 1])
        yield {"event": "message", "text": "redone"}
    assert SW.resume_interrupted(_spawn, run_turn) == [run.id]
    st = _wait(run.id)
    assert st["state"] == SW.DONE
    assert sorted(ran) == ["do a", "do b", "do c"]
