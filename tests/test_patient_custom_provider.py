"""The hub waits for an endpoint the user added by hand, when it is asked for by name.

REPORTED 2026-11-21: a local bridge that drives a real browser answers in 20-75 s. "It works
even if it takes much time." The hub cut it at its fast-model budgets (25-45 s for a trivial
hop, 35-60 s for a stream's first content) and handed the turn to another model: a request
for `local-gemini-brdg/gemini-web` was answered by inception after 124 s.

Flag `patient_custom_provider` (default on). For a provider the user added, an EXPLICIT
`provider/model` request is bounded only by the request deadline: no trivial hop budget, no
per-provider share, no fallback mark, no hedge partner. Auto-routed turns and every built-in
provider are unchanged.
"""
import app as A

CUSTOM = "my-bridge"
MODEL = "gemini-web"


def _custom(monkeypatch, on=True):
    monkeypatch.setattr(A, "_is_custom_provider_pid", lambda pid: pid == CUSTOM)
    monkeypatch.setattr(A.config, "get_flag",
                        lambda name, default=False: on if name == "patient_custom_provider" else default)


def _clock(pinned, **kw):
    kw.setdefault("est", 200)
    kw.setdefault("trivial", True)
    return A._ChainClock(pinned=pinned, **kw)


def test_a_pinned_custom_hop_may_use_the_whole_deadline(monkeypatch):
    _custom(monkeypatch)
    with A.app.test_request_context("/v1/chat/completions"):
        c = _clock(True)
        b = c._budget_for(CUSTOM, MODEL, stream=False)
        assert b is not None and b > 150, b
        assert b <= c.left() + 0.01


def test_the_same_hop_not_pinned_keeps_the_trivial_budget(monkeypatch):
    _custom(monkeypatch)
    with A.app.test_request_context("/v1/chat/completions"):
        b = _clock(False)._budget_for(CUSTOM, MODEL, stream=False)
        assert b <= A._TRIVIAL_SLOW_HOP_BUDGET + 0.01, b


def test_a_pinned_built_in_provider_keeps_the_trivial_budget(monkeypatch):
    _custom(monkeypatch)
    with A.app.test_request_context("/v1/chat/completions"):
        b = _clock(True)._budget_for("nvidia", "some-model", stream=False)
        assert b <= A._TRIVIAL_SLOW_HOP_BUDGET + 0.01, b


def test_the_flag_turns_it_off(monkeypatch):
    _custom(monkeypatch, on=False)
    with A.app.test_request_context("/v1/chat/completions"):
        b = _clock(True)._budget_for(CUSTOM, MODEL, stream=False)
        assert b <= A._TRIVIAL_SLOW_HOP_BUDGET + 0.01, b


def test_the_provider_share_does_not_cut_it_while_others_wait(monkeypatch):
    _custom(monkeypatch)
    with A.app.test_request_context("/v1/chat/completions"):
        c = _clock(True, trivial=False, est=11000, stream=True)
        c._rest = [("inception", "mercury-3")]            # another provider waits
        b = c._budget_for(CUSTOM, MODEL, stream=True)
        cap = max(A._TOOL_PROVIDER_MIN_SECONDS, A._TOOL_PROVIDER_SHARE * float(c.limit))
        assert b is not None and b > cap, (b, cap)
        c2 = _clock(False, trivial=False, est=11000, stream=True)
        c2._rest = [("inception", "mercury-3")]
        assert c2._budget_for(CUSTOM, MODEL, stream=True) <= cap + 0.01


def test_a_streams_first_content_is_waited_for_as_long_as_the_deadline_allows(monkeypatch):
    _custom(monkeypatch)
    with A.app.test_request_context("/v1/chat/completions"):
        patient = _clock(True).peek_timeout(MODEL, 200, pid=CUSTOM)
        normal = _clock(False).peek_timeout(MODEL, 200, pid=CUSTOM)
        assert patient > 150, patient
        assert normal <= A.STREAM_SLOW_BIG_PEEK_TIMEOUT + A.STREAM_PREFILL_EXTRA_CAP
        assert patient > normal


def test_the_peek_is_still_bounded_by_the_request_deadline(monkeypatch):
    _custom(monkeypatch)
    with A.app.test_request_context("/v1/chat/completions"):
        c = _clock(True)
        assert c.peek_timeout(MODEL, 200, pid=CUSTOM) <= c.left() + 0.01


def test_no_hedge_partner_races_a_patient_hop(monkeypatch):
    _custom(monkeypatch)
    chain = [(CUSTOM, MODEL), ("inception", "mercury-3")]
    with A.app.test_request_context("/v1/chat/completions"):
        for pinned, want_partner in ((True, False), (False, True)):
            c = _clock(pinned)
            c._hedge_plan = {"chain": chain, "base": {}, "diff": "simple",
                             "lines": False, "output_budget": False}
            monkeypatch.setattr(A, "_recent_hop_failure", lambda p, m: None)
            monkeypatch.setattr(A, "_is_provider_dead", lambda p: False)
            assert (c._hedge_partner(CUSTOM, MODEL) is not None) is want_partner


def test_the_patient_check_never_raises(monkeypatch):
    def boom(pid):
        raise RuntimeError("registry unreadable")
    monkeypatch.setattr(A, "_is_custom_provider_pid", boom)
    assert A._patient_pid(CUSTOM) is False


def test_the_streaming_header_wait_is_long_for_a_custom_provider(monkeypatch):
    """A bridge sends its headers only when the whole answer is ready."""
    _custom(monkeypatch)
    seen = []

    class _R:
        status_code = 200
        text = ""
        headers = {}

        def close(self):
            pass

        def iter_lines(self, decode_unicode=False):
            return iter(())

    def fake(wait, fn, **kw):
        seen.append(wait)
        return _R()

    monkeypatch.setattr(A, "_post_with_header_deadline", fake)
    monkeypatch.setattr(A, "_needs_key", lambda pid: False)
    # a fake request must not be counted against a real provider's quota (other tests read it)
    monkeypatch.setattr(A.quota, "record", lambda *a, **k: None)
    monkeypatch.setattr(A.quota, "record_key", lambda *a, **k: None)
    payload = {"model": MODEL, "stream": True, "messages": [{"role": "user", "content": "hi"}]}
    # a real, keyless provider id stands in for the user's one (it needs a base_url to dispatch)
    monkeypatch.setattr(A, "_is_custom_provider_pid", lambda pid: pid == "llm7")
    A._upstream_chat("llm7", payload, True)
    A._upstream_chat("uncloseai", dict(payload), True)
    assert seen[0] >= A._STREAM_HEADER_BUDGET
    assert seen[1] < A._STREAM_HEADER_BUDGET
