"""A provider can dodge STREAM_IDLE_TIMEOUT forever by sending blank/keepalive
SSE lines without ever delivering real content -- observed LIVE, same
provider+model, twice: 2026-08-06 held a swarm hop hostage 24+ minutes
(_SWARM_HOP_DEADLINE, the non-streaming fix), 2026-08-08 held a live Codex
/v1/responses stream 600+s -- ending only when the ACTIVITY-FEED janitor
relabelled the row 'stalled' for the DASHBOARD (_ACTIVITY_STALL_SECS), which
never touches the real connection, so Codex just kept waiting the whole time.

_STREAM_PROGRESS_DEADLINE closes that gap for all three streaming relays
(_proxy_sse for /v1/chat/completions, _responses_stream for Codex,
_anthropic_stream for Claude): time since the LAST real chunk, not total
stream duration, so a genuinely slow-but-progressing generation is never cut
off -- only a stream gone quiet except for keepalives is.
"""
import time

import pytest

import app


# --------------------------------------------------------------------------- #
# _sse_chunk_is_progress: the raw signal everything else is built on
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,expected", [
    (b'data: {"choices":[{"delta":{"content":"hi"}}]}', True),
    (b'data: {"choices":[{"delta":{"reasoning_content":"thinking..."}}]}', True),
    (b"", False),
    (b"\n", False),
    (b"data:", False),
    (b"data: ", False),
    (b"data: [DONE]", False),
    (b": keepalive", False),
    (b"\n\ndata: [DONE]\n\n", False),          # multi-line chunk, only [DONE] in it
    (b'\n\ndata: {"choices":[{"delta":{"content":"x"}}]}\n\n', True),  # real line among blanks
])
def test_sse_chunk_is_progress(raw, expected):
    assert app._sse_chunk_is_progress(raw) is expected


def test_sse_chunk_is_progress_accepts_str_too():
    assert app._sse_chunk_is_progress('data: {"choices":[{"delta":{"content":"hi"}}]}') is True


# --------------------------------------------------------------------------- #
# _proxy_sse (/v1/chat/completions passthrough)
# --------------------------------------------------------------------------- #

class _Resp:
    def close(self):
        pass


def _trickle(n, gap, chunk=b"\n\n"):
    """Yields a blank/keepalive chunk `n` times, `gap` seconds apart -- real
    content never arrives."""
    for _ in range(n):
        time.sleep(gap)
        yield chunk


def test_proxy_sse_cuts_off_a_trickling_stream(monkeypatch):
    monkeypatch.setattr(app, "_STREAM_PROGRESS_DEADLINE", 0.05)
    calls = []
    monkeypatch.setattr(app, "_record_outcome", lambda pid, model, ok: calls.append((pid, model, ok)))

    out = list(app._proxy_sse(_Resp(), _trickle(8, 0.02),
                              hop_pid="tokenrouter", hop_model="kimi-k3-free"))
    assert calls == [("tokenrouter", "kimi-k3-free", False)]
    assert out[-1] == b"data: [DONE]\n\n"


def test_proxy_sse_never_fires_while_real_content_keeps_arriving(monkeypatch):
    """The deadline is since the LAST real chunk, not total duration -- a slow
    but genuinely progressing stream must not be cut off."""
    monkeypatch.setattr(app, "_STREAM_PROGRESS_DEADLINE", 0.05)
    calls = []
    monkeypatch.setattr(app, "_record_outcome", lambda pid, model, ok: calls.append((pid, model, ok)))

    def real_progress():
        for _ in range(5):
            time.sleep(0.03)   # each gap alone is under the deadline
            yield b'data: {"choices":[{"delta":{"content":"x"}}]}'
        yield b"data: [DONE]"

    out = list(app._proxy_sse(_Resp(), real_progress(),
                              hop_pid="tokenrouter", hop_model="kimi-k3-free"))
    assert calls == []
    assert len(out) == 6


# --------------------------------------------------------------------------- #
# _responses_stream (Codex /v1/responses)
# --------------------------------------------------------------------------- #

def _decode(events):
    return b"".join(events)


def test_responses_stream_cuts_off_a_trickling_stream_and_still_completes(monkeypatch):
    monkeypatch.setattr(app, "_STREAM_PROGRESS_DEADLINE", 0.05)
    calls = []
    monkeypatch.setattr(app, "_record_outcome", lambda pid, model, ok: calls.append((pid, model, ok)))

    events = list(app._responses_stream(
        _Resp(), "auto", line_iter=_trickle(8, 0.02, chunk=b""),
        hop_pid="tokenrouter", hop_model="kimi-k3-free"))

    assert calls == [("tokenrouter", "kimi-k3-free", False)]
    blob = _decode(events)
    assert b'"type": "response.completed"' in blob, (
        "codex must still get a terminal event, never be left hanging: %r" % blob)


# --------------------------------------------------------------------------- #
# _anthropic_stream (Claude /v1/messages)
# --------------------------------------------------------------------------- #

def test_anthropic_stream_cuts_off_a_trickling_stream_and_still_completes(monkeypatch):
    monkeypatch.setattr(app, "_STREAM_PROGRESS_DEADLINE", 0.05)
    calls = []
    monkeypatch.setattr(app, "_record_outcome", lambda pid, model, ok: calls.append((pid, model, ok)))

    events = list(app._anthropic_stream(
        _Resp(), "claude-opus-5", 100, line_iter=_trickle(8, 0.02, chunk=b""),
        hop_pid="tokenrouter", hop_model="kimi-k3-free"))

    assert calls == [("tokenrouter", "kimi-k3-free", False)]
    blob = _decode(events)
    assert b'"type": "message_stop"' in blob, (
        "claude code must still get a terminal event, never be left hanging: %r" % blob)
