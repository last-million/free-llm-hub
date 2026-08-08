"""A 200 whose "answer" IS the relay backend's error page.

The user reported this FOUR times and four fixes changed nothing:

    The model does not exist in https://api.airforce
    discord.gg/airforce

REPRODUCED LIVE 2026-08-07 with the hub's own g4f key:

    POST https://g4f.space/v1/chat/completions
    {"model": "srv_mp3lmkuad07322459f47:claude-opus-4-7", ...}
    -> HTTP 200, finish_reason "stop", usage {"total_tokens": 399},
       content = "The model does not exist in https://api.airforce\\n
                  discord.gg/airforce"

That is a SUCCESS by every status-based check in app.py, which is exactly why
all four earlier fixes were unreachable -- _DEAD_STATUSES,
_maybe_mark_missing_model, the non-2xx _record_outcome(False) and the last_hard
relay all live in the non-2xx branch, and this request never enters it. The
chain does not exhaust; it succeeds on hop 1 and returns. Worse,
_record_chat_usage files every delivery as _record_outcome(..., True), so the
reliability signal was PROMOTING the id while it kept its 138 claude-family
score floor and therefore usually won hop 1.

Content is the only signal that exists for this failure mode.
"""
import pytest

import app

AIRFORCE = "The model does not exist in https://api.airforce\ndiscord.gg/airforce"


@pytest.fixture(autouse=True)
def clean_state():
    with app._outcome_lock:
        app._outcomes.clear()
    with app._dead_lock:
        app._dead_models.clear()
    yield
    with app._outcome_lock:
        app._outcomes.clear()
    with app._dead_lock:
        app._dead_models.clear()


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text", [
    AIRFORCE,
    "Global rate limit exceeded (1 requests per second). Or upgrade at api.airforce",
    "Model 'gpt-4o' requires an active subscription or a positive balance.",
    "No cake credits. Bake proof-of-work cakes at g4f.dev/chat",
    "Insufficient balance. This request costs ~0.0472 pollen.",
])
def test_upstream_error_pages_are_detected(text):
    assert app._is_upstream_nonanswer(text) is True


@pytest.mark.parametrize("text", [
    "OK",
    "Here is your refactored function:\n\ndef f():\n    return 1",
    "",
    None,
])
def test_real_answers_are_never_flagged(text):
    assert app._is_upstream_nonanswer(text) is False


def test_a_long_reply_quoting_the_phrase_is_not_flagged():
    """The length bound is the safety property: a real model explaining an
    error legitimately contains the words, but is never a <300-char COMPLETE
    answer. A false positive here would silently discard genuine work."""
    real = ("When you request a model the provider has retired, the API replies "
            "that the model does not exist. Handle it by catching the 404 and "
            "falling back to a supported id. " + "Here is a worked example. " * 12)
    assert len(real) > app._NONANSWER_MAX_CHARS
    assert app._is_upstream_nonanswer(real) is False


def test_a_real_tool_call_is_never_flagged():
    """An agentic turn whose answer IS a tool call must survive even if the
    text part happens to look like an error."""
    data = {"choices": [{"message": {"role": "assistant", "content": AIRFORCE,
                                     "tool_calls": [{"function": {"name": "read_file"}}]}}]}
    assert app._chat_json_nonanswer(data) is False


def test_the_exact_reported_payload_is_rejected():
    """The live-captured shape, verbatim: 200 + finish_reason stop + usage."""
    data = {"choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": AIRFORCE}}],
            "usage": {"prompt_tokens": 391, "completion_tokens": 8, "total_tokens": 399}}
    assert app._chat_json_is_empty(data) is False, \
        "it is NOT empty -- which is precisely why the existing gate let it through"
    assert app._chat_json_nonanswer(data) is True


# --------------------------------------------------------------------------- #
# Consequences: the id must actually LEAVE the chain, not just rank lower.
# --------------------------------------------------------------------------- #

def test_note_nonanswer_both_records_failure_and_sidelines():
    pid, model = "g4f", "srv_mp3lmkuad07322459f47:claude-opus-4-7"
    assert app._is_model_dead(pid, model) is False
    app._note_nonanswer(pid, model)
    assert app._is_model_dead(pid, model) is True, (
        "dead-marking is the ONLY mechanism that REMOVES an id from _build_chain; "
        "a reliability penalty alone only re-orders it, which is why four "
        "previous fixes failed")
    assert app._reliability(pid, model) < 0.5, "the delivery failure must be filed too"


def test_the_hop_falls_through_and_the_next_model_answers(monkeypatch):
    """End to end: the bad hop returns the airforce 200, the user gets the NEXT
    model's real answer instead of the error page."""
    class R:
        status_code = 200
        headers = {}
        text = ""

        def __init__(self, content):
            self._c = content

        def json(self):
            return {"choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": self._c}}],
                    "usage": {"total_tokens": 399}}

        def close(self):
            pass

    def fake_dispatch(pid, payload, stream):
        return R(AIRFORCE) if pid == "g4f" else R("REAL ANSWER")

    monkeypatch.setattr(app, "_dispatch_chat", fake_dispatch)
    monkeypatch.setattr(app, "_build_chain", lambda *a, **k: [
        ("g4f", "srv_mp3lmkuad07322459f47:claude-opus-4-7"),
        ("groq", "llama-3.3-70b-versatile")])
    monkeypatch.setattr(app, "_check_provider_ready", lambda pid: None)
    # "auto" resolves via _best_free_pair() -> real enabled+keyed providers,
    # which an isolated test config has none of. _build_chain is already
    # mocked and ignores whatever _resolve_model returns, so any well-shaped
    # pair that doesn't itself error out is enough to clear the gate.
    monkeypatch.setattr(app, "_resolve_model",
                        lambda m: ("g4f", "srv_mp3lmkuad07322459f47:claude-opus-4-7"))

    r = app.app.test_client().post("/v1/chat/completions", json={
        "model": "auto", "max_tokens": 64, "stream": False,
        "messages": [{"role": "user", "content": "hi"}]})

    assert r.status_code == 200
    content = r.get_json()["choices"][0]["message"]["content"]
    assert content == "REAL ANSWER", (
        "the user received the relay's error page as the model's answer: %r" % content)
    assert app._is_model_dead("g4f", "srv_mp3lmkuad07322459f47:claude-opus-4-7") is True
