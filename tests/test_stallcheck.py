"""stallcheck.assess: the 'is this turn finished, or stalled?' decision for hub-owned runs."""
import stallcheck as sc


def test_progress_and_a_real_answer_is_finished():
    d = sc.assess(made_progress=True, answer_ok=True)
    assert d.retry is False and d.reason == sc.DONE


def test_a_prose_answer_with_no_tools_is_finished():
    # a question answered in prose (no tool calls, no file changes) is DONE, not a stall
    d = sc.assess(made_progress=False, answer_ok=True)
    assert d.retry is False and d.reason == sc.DONE_DIRECT


def test_no_progress_and_no_answer_retries_same_model_first():
    d = sc.assess(made_progress=False, answer_ok=False, attempts_used=0)
    assert d.retry is True and d.reason == sc.NO_PROGRESS
    assert d.switch_model is False and d.attempt == 1


def test_the_second_no_progress_retry_switches_model():
    d = sc.assess(made_progress=False, answer_ok=False, attempts_used=1)
    assert d.retry is True and d.switch_model is True and d.attempt == 2


def test_made_progress_but_stopped_short_continues_same_model():
    d = sc.assess(made_progress=True, answer_ok=False, attempts_used=0)
    assert d.retry is True and d.reason == sc.INCOMPLETE and d.switch_model is False


def test_an_error_switches_model_at_once():
    d = sc.assess(made_progress=False, answer_ok=False, errored=True, attempts_used=0)
    assert d.retry is True and d.reason == sc.ERROR and d.switch_model is True and d.attempt == 1


def test_an_error_still_switches_even_if_some_progress():
    d = sc.assess(made_progress=True, answer_ok=False, errored=True)
    assert d.retry is True and d.reason == sc.ERROR and d.switch_model is True


def test_it_never_retries_more_than_max():
    assert sc.assess(False, False, attempts_used=sc.MAX_RETRIES).retry is False
    assert sc.assess(False, False, attempts_used=sc.MAX_RETRIES).reason == sc.EXHAUSTED
    assert sc.assess(False, False, errored=True, attempts_used=sc.MAX_RETRIES).retry is False


def test_max_retries_is_a_hard_two():
    # from a fresh instruction: retry at 0 and 1, stop at 2 -- at most two extra turns
    assert sc.assess(False, False, attempts_used=0).retry is True
    assert sc.assess(False, False, attempts_used=1).retry is True
    assert sc.assess(False, False, attempts_used=2).retry is False


def test_a_custom_cap_of_zero_never_retries():
    assert sc.assess(False, False, attempts_used=0, max_retries=0).retry is False


def test_never_raises_on_junk_inputs():
    for bad in (None, "x", object()):
        d = sc.assess(made_progress=True, answer_ok=False, attempts_used=bad)
        assert isinstance(d, sc.Decision)       # falls through to a safe decision, never raises


def test_as_dict_round_trips():
    d = sc.assess(False, False, attempts_used=1)
    assert d.as_dict() == {"retry": True, "reason": sc.NO_PROGRESS,
                           "switch_model": True, "attempt": 2}
