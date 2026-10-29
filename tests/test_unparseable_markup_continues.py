"""A model that writes its tool call in a dialect the hub cannot read must NOT end the turn.

MEASURED 2026-10-29 (opencode, /v1/chat/completions): prose had already been relayed, then the
model typed a native tool-call block `tool_rescue.rescue_stream` could not parse. Its error frame
(type upstream_error, code unparseable_tool_markup) was relayed untouched; opencode does not retry
that type, so the turn ended, and nothing was filed against the model, so the next request could
pick it again. /v1/responses and /v1/messages already turned such a frame into a retryable broken
stream; the chat relay did not.

Hermetic: fake frames, no network. The markup below is synthetic.
"""
import json
import logging
import re

import pytest

import app as A
import config
import tool_rescue
from test_broken_stream_retry import _chunk, _words, _Resp, _data, filed  # noqa: F401

TOOLS = [{"type": "function", "function": {
    "name": "bash", "description": "run a command",
    "parameters": {"type": "object", "properties": {"command": {"type": "string"}},
                   "required": ["command"]}}}]

SENTINEL = "SENTINEL-VALUE-9f3c"
# A native-dialect block naming a tool the client never offered: the hub must refuse to
# promote it (SAFETY in tool_rescue), i.e. it is "unparseable" for this request.
BAD_MARKUP = (
    "<｜DSML｜function_calls>\n"
    "<｜DSML｜invoke name=\"not_an_offered_tool\">\n"
    "<｜DSML｜parameter name=\"x\" string=\"true\">%s</｜DSML｜parameter>\n"
    "</｜DSML｜invoke>\n</｜DSML｜function_calls>" % SENTINEL)


GOOD_MARKUP = (
    "<｜DSML｜function_calls>\n"
    "<｜DSML｜invoke name=\"bash\">\n"
    "<｜DSML｜parameter name=\"command\" string=\"true\">ls</｜DSML｜parameter>\n"
    "</｜DSML｜invoke>\n</｜DSML｜function_calls>")


def _stream(frames, tools=TOOLS):
    """The upstream chat SSE as the hub reads it: frames, then rescue_stream over them."""
    raw = [f + b"\n\n" for f in frames]
    return tool_rescue.rescue_stream(iter(raw), tools, "frames")


def _relay(it, **kw):
    return b"".join(A._proxy_sse(_Resp(), it, hop_pid="p1", hop_model="m1", **kw)).decode()


def _prose_then_bad_markup(finish="stop"):
    return _words()[:6] + [_chunk({"content": BAD_MARKUP}), _chunk({}, finish)]


def test_the_rescuer_alone_still_reports_unparseable_markup():
    """Positive control: the stream really does carry the hub's error frame."""
    body = b"".join(_stream(_prose_then_bad_markup())).decode()
    errs = [e for e in _data(body) if "error" in e]
    assert errs and errs[-1]["error"]["code"] == "unparseable_tool_markup"


def test_prose_then_unparseable_markup_ends_with_the_retryable_frame_not_the_raw_error(filed):
    body = _relay(_stream(_prose_then_bad_markup()))
    errs = [e for e in _data(body) if "error" in e]
    assert len(errs) == 1
    assert errs[0]["error"]["code"] == "ECONNRESET", "the code opencode / qwen-code retry on"
    assert errs[0]["error"]["type"] == "server_error"
    assert "unparseable_tool_markup" not in body
    assert "[DONE]" not in body, "[DONE] would tell the CLI the answer is complete"
    assert SENTINEL not in body, "the raw markup is never shown as text"
    assert "choices" not in errs[0]


def test_the_model_is_filed_as_failed_so_the_retry_goes_elsewhere(filed):
    _relay(_stream(_prose_then_bad_markup()))
    assert filed["outcome"].count(("p1", "m1", False)) == 1
    assert A._recent_hop_failure("p1", "m1") == "conn", \
        "the next chain must put this pair behind the others"
    assert filed["stream"] == [], "not judged as a finished answer"


def test_the_prose_before_the_failure_is_still_relayed(filed):
    body = _relay(_stream(_prose_then_bad_markup()))
    text = "".join(((e.get("choices") or [{}])[0].get("delta") or {}).get("content") or ""
                   for e in _data(body))
    assert text.startswith("A hash map stores each key")


def test_an_error_frame_glued_to_other_frames_in_one_chunk_is_split(filed):
    prose = b"".join(f + b"\n\n" for f in _words()[:3])
    err = b'data: ' + json.dumps({"error": {"message": "boom", "type": "upstream_error",
                                            "code": "x"}}).encode() + b"\n\n"
    body = _relay(iter([prose + err]))
    assert body.startswith("data: ")
    assert "ECONNRESET" in body and '"code": "x"' not in body
    assert "[DONE]" not in body
    assert filed["outcome"].count(("p1", "m1", False)) == 1


def test_an_error_after_the_finish_reason_keeps_the_answer(filed):
    frames = _words()[:4] + [_chunk({}, "stop")]
    err = b"data: " + json.dumps({"error": {"message": "late", "type": "t"}}).encode()
    body = _relay(iter([f + b"\n\n" for f in frames] + [err + b"\n\n"]))
    assert "ECONNRESET" not in body, "the answer had finished: no retryable ending"
    assert '"finish_reason": "stop"' in body
    assert ("p1", "m1", False) not in filed["outcome"]


def test_text_that_merely_mentions_an_error_is_not_an_error_frame(filed):
    frames = [_chunk({"content": 'the response was {"error": {"code": 1}} and we retried'}),
              _chunk({}, "stop")]
    body = _relay(iter([f + b"\n\n" for f in frames]))
    assert "ECONNRESET" not in body
    assert "and we retried" in body, "the text reaches the client"
    assert ("p1", "m1", False) not in filed["outcome"]


def test_a_clean_tool_call_in_a_dialect_the_hub_reads_is_still_rescued(filed):
    body = _relay(_stream(_words()[:3] + [_chunk({"content": GOOD_MARKUP}), _chunk({}, "stop")]))
    data = _data(body)
    assert any((e.get("choices") or [{}])[0].get("delta", {}).get("tool_calls") for e in data)
    assert "ECONNRESET" not in body and ("p1", "m1", False) not in filed["outcome"]


def test_flag_off_restores_the_old_raw_frame(filed, monkeypatch):
    real = config.get_flag
    monkeypatch.setattr(config, "get_flag",
                        lambda n, d=False: False if n == "stream_error_frame_retry" else real(n, d))
    body = _relay(_stream(_prose_then_bad_markup()))
    assert "unparseable_tool_markup" in body and "ECONNRESET" not in body


def test_before_anything_is_relayed_the_peek_hands_the_turn_to_the_next_model():
    """No prose first: the stream is still uncommitted, so the existing peek reports 'error'
    and the chain walks on. This must keep working."""
    status, _buf = A._peek_until_content(_stream([_chunk({"content": BAD_MARKUP}),
                                                  _chunk({}, "stop")]), 5.0)
    assert status == "error"


def test_the_diagnostic_log_has_no_reply_content(caplog):
    with caplog.at_level(logging.WARNING, logger="free-llm-hub"):
        b"".join(_stream(_prose_then_bad_markup()))
    lines = [r.getMessage() for r in caplog.records if "tool-rescue" in r.getMessage()]
    assert lines, "the failure must leave a trace"
    assert SENTINEL not in " ".join(lines)
    assert re.search(r"len=\d+", lines[0]) and "finish=" in lines[0]


def test_responses_and_messages_still_treat_the_frame_as_a_broken_stream():
    """Guard for the two protocols that were already right (their own tests are in
    test_broken_stream_retry.py); here only that the frame we emit is what they key on."""
    err = json.loads(tool_rescue._STREAM_ERROR["error"]["code"] and
                     json.dumps(tool_rescue._STREAM_ERROR))
    assert err.get("error") and "choices" not in err
