"""Swarm that actually works inside a CLI agent loop.

The multi-phase pipeline (planner -> workers -> reviewer) emits finished prose
and never tool calls, so a coding agent driven by it writes no files at all --
which is why tool-carrying turns used to be refused outright with a 400.

Refusing was correct but useless. A tool turn now gets the form of "several
models work on it together" that survives an agent loop: the SAME request, with
the real tools, runs on several distinct strong models at once, and the best
single response is returned. Whatever wins is an ordinary complete response the
CLI can execute, tool calls intact.
"""
from unittest import mock

import app


def _resp(status=200, msg=None):
    r = mock.Mock(status_code=status)
    r.json.return_value = {"id": "x", "created": 1, "choices": [
        {"index": 0, "finish_reason": "tool_calls" if (msg or {}).get("tool_calls") else "stop",
         "message": msg or {"role": "assistant", "content": "hi"}}]}
    r.close = mock.Mock()
    return r


TOOLCALL = {"role": "assistant", "tool_calls": [
    {"id": "c1", "type": "function",
     "function": {"name": "read_file", "arguments": "{}"}}]}
PROSE = {"role": "assistant", "content": "I would read the file."}


def _fan(monkey_results, scores=None, picks=None):
    """Drive _swarm_tool_turn with a fixed set of candidates and replies."""
    picks = picks or [("groq", "a"), ("cerebras", "b"), ("nvidia", "c")]
    scores = scores or {"a": 10, "b": 20, "c": 30}

    def fake_chain(pid, model, est=0, **kw):
        return picks

    def fake_dispatch(pid, payload, deadline=None):
        return monkey_results.get(payload["model"]), None

    return (mock.patch.object(app, "_route_by_difficulty",
                              return_value=(picks[0][0], picks[0][1], "hard")),
            mock.patch.object(app, "_build_chain", side_effect=fake_chain),
            mock.patch.object(app, "_dispatch_chat_with_deadline", side_effect=fake_dispatch),
            mock.patch.object(app, "_benchmark_score", side_effect=lambda p, m: scores[m]),
            mock.patch.object(app, "_record_chat_usage"),
            mock.patch.object(app, "_record_outcome"),
            mock.patch.object(app, "_routing_headers", return_value={}))


def _run(body, results, scores=None, picks=None):
    ctxs = _fan(results, scores, picks)
    with ctxs[0], ctxs[1], ctxs[2], ctxs[3], ctxs[4], ctxs[5], ctxs[6]:
        with app.app.test_request_context("/v1/chat/completions"):
            return app._swarm_tool_turn(body)


BODY = {"messages": [{"role": "user", "content": "build it"}],
        "tools": [{"type": "function", "function": {"name": "read_file"}}]}


def test_a_tool_carrying_turn_is_no_longer_refused():
    out = _run(BODY, {"a": _resp(msg=TOOLCALL), "b": _resp(msg=TOOLCALL),
                      "c": _resp(msg=TOOLCALL)})
    assert out is not None, "swarm must serve the turn, not refuse it"


def test_the_winning_response_keeps_its_tool_calls():
    """The whole point: a CLI that gets prose instead of tool calls does nothing."""
    out = _run(BODY, {"a": _resp(msg=TOOLCALL), "b": _resp(msg=TOOLCALL),
                      "c": _resp(msg=TOOLCALL)})
    data = out[0].get_json()
    assert data["choices"][0]["message"]["tool_calls"], data


def test_a_model_that_acted_beats_a_better_ranked_one_that_only_talked():
    """A model answering in prose where others reached for a tool has done
    strictly less of the job, however well it benchmarks."""
    out = _run(BODY,
               {"a": _resp(msg=TOOLCALL), "b": _resp(msg=PROSE), "c": _resp(msg=PROSE)},
               scores={"a": 10, "b": 20, "c": 30})   # 'a' is the WORST ranked
    data = out[0].get_json()
    assert data["choices"][0]["message"].get("tool_calls"), "the acting model must win"
    assert data["model"].endswith("/a")


def test_among_equals_the_stronger_model_wins():
    out = _run(BODY, {"a": _resp(msg=TOOLCALL), "b": _resp(msg=TOOLCALL),
                      "c": _resp(msg=TOOLCALL)}, scores={"a": 10, "b": 20, "c": 30})
    assert out[0].get_json()["model"].endswith("/c")


def test_dead_candidates_are_ignored_not_fatal():
    out = _run(BODY, {"a": None, "b": _resp(status=500), "c": _resp(msg=TOOLCALL)})
    assert out is not None
    assert out[0].get_json()["model"].endswith("/c")


def test_it_returns_None_when_nothing_answers_so_the_caller_can_fall_through():
    """A swarm that cannot answer must not take the turn down with it."""
    assert _run(BODY, {"a": None, "b": None, "c": None}) is None


def test_the_same_model_is_not_asked_three_times():
    """Three copies of one model relayed by one provider is the same opinion
    three times at three times the cost, not a second opinion."""
    picks = [("g4f", "srv_1:kimi-k3"), ("g4f", "srv_2:kimi-k3"), ("nvidia", "moonshotai/kimi-k3")]
    seen = []

    def fake_dispatch(pid, payload, deadline=None):
        seen.append(payload["model"])
        return _resp(msg=TOOLCALL), None

    with mock.patch.object(app, "_route_by_difficulty", return_value=("g4f", picks[0][1], "hard")), \
            mock.patch.object(app, "_build_chain", side_effect=lambda *a, **k: picks), \
            mock.patch.object(app, "_dispatch_chat_with_deadline", side_effect=fake_dispatch), \
            mock.patch.object(app, "_benchmark_score", return_value=100), \
            mock.patch.object(app, "_record_chat_usage"), \
            mock.patch.object(app, "_record_outcome"), \
            mock.patch.object(app, "_routing_headers", return_value={}), \
            app.app.test_request_context("/v1/chat/completions"):
        app._swarm_tool_turn(BODY)
    idents = {app._normalize_model_identity(m) for m in seen}
    assert len(seen) == len(idents) == 1, seen


def test_streaming_replays_the_winner_including_tool_calls():
    body = dict(BODY, stream=True)
    out = _run(body, {"a": _resp(msg=TOOLCALL), "b": _resp(msg=TOOLCALL),
                      "c": _resp(msg=TOOLCALL)})
    chunks = "".join(x.decode() if isinstance(x, bytes) else x
                     for x in out.response)
    assert "tool_calls" in chunks, "a streamed turn must not lose the tool calls"
    assert "[DONE]" in chunks
