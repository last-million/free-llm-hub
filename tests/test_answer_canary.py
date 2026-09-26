"""Health probes must JUDGE THE TEXT, not just the HTTP status.

Every probe used to send "hi" and call a 200 healthy, so a hop answering 200
with template junk ("4568<|im_end|>user: ...") or a wrong answer kept its
place at the head of the chain. The answer canary asks "What is N plus 1?"
and checks the reply. Two quality failures in a row demote THAT (provider,
model) pair by a score penalty -- never an identity block, never a dead mark.
All fakes, no network.
"""
import re
import time
from unittest import mock

import pytest

import app
import config


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.setattr(app, "_canary_state", {})
    # Never write the real quota-state file from a test.
    monkeypatch.setattr(app.quota, "_persist_maybe", lambda: None)


def _resp(status, content=None):
    r = mock.Mock(status_code=status)
    r.json.return_value = ({"choices": [{"message": {"content": content}}]}
                           if status == 200 else {"error": {"message": "boom"}})
    r.headers = {}
    r.text = "{}"
    r.close = mock.Mock()
    return r


def _answer_from(payload, style):
    """What a fake model replies to the canary in `payload`."""
    n = int(re.search(r"What is (\d+) plus 1", payload["messages"][0]["content"]).group(1))
    return {"correct": str(n + 1),
            "junk": "%d<|im_end|>\nuser: and now?" % (n + 1),
            "wrong": str(n + 7),
            "empty": ""}[style]


# --------------------------------------------------------------------------- #
# The judge
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", ["4568", "  4568\n", "4568.", "**4568**", "`4568`",
                                  "<think>4567+1 is 4568</think>4568"])
def test_exact_answer_is_correct(text):
    assert app._judge_canary(text, "4568") == "correct"


@pytest.mark.parametrize("text", ["4568<|im_end|>user: hi", "4568\n\nuser: next question",
                                  "The answer is 4568."])
def test_right_number_with_anything_around_it_is_junk(text):
    assert app._judge_canary(text, "4568") == "junk"


@pytest.mark.parametrize("text", ["4567", "hello", "45689"])
def test_missing_answer_is_wrong(text):
    assert app._judge_canary(text, "4568") == "wrong"


@pytest.mark.parametrize("text", ["", "   ", None, "<think>still thinking about"])
def test_no_visible_answer_is_empty_not_a_failure(text):
    assert app._judge_canary(text, "4568") == "empty"


def test_question_has_a_fresh_known_answer():
    prompt, expected = app._canary_question(1234)
    assert "1234 plus 1" in prompt and expected == "1235"
    prompt2, _ = app._canary_question()
    assert re.search(r"What is \d{4} plus 1\? Answer with only the number\.", prompt2)


# --------------------------------------------------------------------------- #
# Quality state -> score penalty
# --------------------------------------------------------------------------- #
def test_two_strikes_demote_only_that_provider_model_pair():
    app._record_canary_verdict("p1", "m", "junk")
    assert app._answer_quality_penalty("p1", "m") == 0.0      # one strike is evidence
    app._record_canary_verdict("p1", "m", "wrong")
    assert app._answer_quality_penalty("p1", "m") == app._CANARY_PENALTY
    # The SAME model on another provider is a different deployment: untouched.
    assert app._answer_quality_penalty("p2", "m") == 0.0
    # And it is a score penalty, not a dead mark.
    assert not app._is_model_dead("p1", "m")


def test_penalty_is_applied_in_ranking():
    app._record_canary_verdict("p1", "m", "junk")
    app._record_canary_verdict("p1", "m", "junk")
    with mock.patch.object(app, "_sustain_penalty", return_value=0.0), \
            mock.patch.object(app, "_reliability_penalty", return_value=0.0), \
            mock.patch.object(app, "_latency_penalty", return_value=0.0):
        demoted = app._agentic_score((100.0, "p1", "m"))
        clean = app._agentic_score((100.0, "p2", "m"))
    assert clean - demoted == pytest.approx(app._CANARY_PENALTY)


def test_a_later_correct_answer_lifts_the_demotion():
    for _ in range(2):
        app._record_canary_verdict("p1", "m", "junk")
    app._record_canary_verdict("p1", "m", "correct")
    assert app._answer_quality_penalty("p1", "m") == 0.0
    # Strikes reset too: a single junk afterwards does not re-demote.
    app._record_canary_verdict("p1", "m", "junk")
    assert app._answer_quality_penalty("p1", "m") == 0.0


def test_penalty_expires_after_ttl(monkeypatch):
    for _ in range(2):
        app._record_canary_verdict("p1", "m", "junk")
    real = time.time()
    monkeypatch.setattr(app.time, "time", lambda: real + app._CANARY_PENALTY_TTL + 1)
    assert app._answer_quality_penalty("p1", "m") == 0.0


def test_inconclusive_verdicts_do_not_strike():
    app._record_canary_verdict("p1", "m", "junk")
    for v in ("empty", "http_error", "error"):
        app._record_canary_verdict("p1", "m", v)
    assert app._canary_state[("p1", "m")]["strikes"] == 1
    assert app._answer_quality_penalty("p1", "m") == 0.0


def test_state_survives_a_restart_through_the_quota_app_blob():
    for _ in range(2):
        app._record_canary_verdict("p1", "org/m", "junk")
    blob = app._dead_state_dump()
    assert "p1|org/m" in blob["answer_canary"]
    app._canary_state.clear()
    app._dead_state_load(blob)
    assert app._answer_quality_penalty("p1", "org/m") == app._CANARY_PENALTY


def test_loader_ignores_garbage():
    app._canary_state_load({"nobar": {}, "p|m": "x", "p|n": {"strikes": "zz"}})
    app._canary_state_load(None)
    assert app._canary_state == {}


# --------------------------------------------------------------------------- #
# The probe paths
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("style,ok", [("correct", True), ("junk", False),
                                      ("wrong", False), ("empty", True)])
def test_probe_pair_judges_the_text(style, ok):
    def fake_chat(pid, payload, stream):
        return _resp(200, _answer_from(payload, style))

    with mock.patch.object(app, "_upstream_chat", side_effect=fake_chat):
        res = app._probe_pair_verdict("p1", "m")
        legacy = app._probe_pair("p1", "m")
    assert res["verdict"] == style
    assert res["ok"] is ok and legacy[0] is ok
    assert app._canary_state[("p1", "m")]["verdict"] == style


def test_probe_pair_http_error_is_not_a_quality_verdict():
    with mock.patch.object(app, "_upstream_chat", return_value=_resp(503)):
        res = app._probe_pair_verdict("p1", "m")
    assert res["ok"] is False and res["verdict"] == "http_error"
    assert res["detail"].startswith("HTTP 503")
    assert app._canary_state[("p1", "m")]["strikes"] == 0


def _client():
    app.app.config["TESTING"] = True
    return app.app.test_client()


def _hdrs():
    return {"X-Free-LLM-Hub-Token": config.ensure_control_token(),
            "X-Free-LLM-Hub": "dashboard"}


def _provider_test(fake_chat):
    with mock.patch.object(config, "get_provider_config",
                           return_value={"api_key": "K", "api_keys": ["K"], "enabled": True}), \
            mock.patch.object(app, "_models_url_for", return_value=None), \
            mock.patch.object(app, "_upstream_chat", side_effect=fake_chat), \
            mock.patch.object(app, "_record_test_result", return_value=([], [])):
        return _client().post("/api/test/groq", headers=_hdrs()).get_json()


def test_provider_test_reports_answered_correctly():
    body = _provider_test(lambda pid, payload, stream: _resp(200, _answer_from(payload, "correct")))
    assert body["ok"] is True
    assert body["canary"]["verdict"] == "correct"
    assert "answered correctly" in body["detail"]
    assert body["keys"][0]["canary"]["verdict"] == "correct"


def test_provider_test_reports_junk_but_key_still_ok():
    body = _provider_test(lambda pid, payload, stream: _resp(200, _answer_from(payload, "junk")))
    # ok keeps meaning "the key generates" (backward compatible)...
    assert body["ok"] is True
    # ...and the new field + wording say the text was junk.
    assert body["canary"]["verdict"] == "junk"
    assert "answered with junk" in body["detail"]


def test_provider_test_http_error_has_no_canary():
    body = _provider_test(lambda pid, payload, stream: _resp(401))
    assert body["ok"] is False
    assert "canary" not in body
    assert "HTTP 401" in body["detail"]


# --------------------------------------------------------------------------- #
# Background loop: low rate, eligible pairs only
# --------------------------------------------------------------------------- #
def test_canary_tick_probes_only_top_eligible_pairs_once_per_interval(monkeypatch):
    ranked = [(200.0 - i, "sub-codex" if i == 0 else "paidp" if i == 1
               else "hot" if i == 2 else "free%d" % i, "m%d" % i) for i in range(14)]
    monkeypatch.setattr(app, "_ranked_free_pairs", lambda limit=6: ranked[:limit])
    monkeypatch.setattr(app, "_is_provider_dead", lambda pid: False)
    monkeypatch.setattr(app.prov, "get_provider",
                        lambda pid: {"paid": True} if pid == "paidp" else {})
    monkeypatch.setattr(app.quota, "status",
                        lambda pid: {"throttled": pid == "hot", "exhausted": False})
    monkeypatch.setattr(app.quota, "is_model_throttled",
                        lambda pid, m: m == "m3")
    calls = []

    def fake_chat(pid, payload, stream):
        calls.append((pid, payload["model"]))
        return _resp(200, _answer_from(payload, "correct"))

    monkeypatch.setattr(app, "_upstream_chat", fake_chat)
    done = app._answer_canary_tick(pause=0)
    pids = [p for p, _m in calls]
    assert len(calls) == app._CANARY_TOP_N == 8
    assert not {"sub-codex", "paidp", "hot"} & set(pids)
    assert ("free3", "m3") not in calls           # model-throttled
    assert all(v == "correct" for _p, _m, v in done)
    # Same pairs again immediately: nothing is due (one probe per pair per 6h).
    calls.clear()
    assert app._answer_canary_tick(pause=0) == []
    assert calls == []


def test_canary_tick_never_raises(monkeypatch):
    def boom(limit=6):
        raise RuntimeError("x")
    monkeypatch.setattr(app, "_ranked_free_pairs", boom)
    assert app._answer_canary_tick(pause=0) == []
