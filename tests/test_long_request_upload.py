"""A long conversation's body must be allowed to finish UPLOADING.

MEASURED live 2026-09-27: requests/urllib3 send the request body under the
CONNECT timeout (the read timeout only starts once the body is written). With a
flat CONNECT_TIMEOUT of 10s and this machine's ~20 KB/s uplink:

    POST 100 KB -> 404 in 6.6s     (dummy key, reached the server)
    POST 400 KB -> ConnectionError('The write operation timed out') at 10.6s
    POST 700 KB -> same at 10.3s;  with a 60s connect timeout: 404 in 33s

So every 160K-token Codex / Claude Code / OpenCode history (~650 KB of JSON)
failed on EVERY hop as a "connection error" and the client got a 503. The
connect-phase budget now grows with the body, and so does the streaming
header wait (which also covers the upload).
"""
import os
import shutil
import tempfile

import pytest

import app


@pytest.fixture
def isolated_config(monkeypatch):
    d = tempfile.mkdtemp(prefix="hub-pytest-upload-")
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", os.path.join(d, "state", "config.json"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": "OK"}}]}
        self.headers = {}
        self.text = ""

    def json(self):
        return self._payload

    def close(self):
        pass

    def iter_lines(self, decode_unicode=False):
        return iter(())


def _big_payload(n_chars):
    return {"model": "open", "messages": [
        {"role": "user", "content": "x" * n_chars}]}


def test_a_small_body_keeps_the_plain_connect_timeout():
    assert app._send_timeout(0) == app.CONNECT_TIMEOUT
    assert app._send_timeout(2000) < app.CONNECT_TIMEOUT + 1


def test_the_measured_failing_sizes_now_get_enough_time():
    """400 KB took ~17s and 700 KB ~33s on the wire: both must fit."""
    assert app._send_timeout(400_000) >= 17 + 10
    assert app._send_timeout(700_000) >= 33 + 10


def test_the_upload_allowance_is_bounded():
    assert app._send_timeout(50_000_000) == app.UPLOAD_TIMEOUT_CAP


def test_it_never_raises_on_nonsense():
    assert app._upload_allowance(None) == 0.0
    assert app._upload_allowance("junk") == 0.0
    assert app._body_bytes({"x": object()}) == 0


def test_a_long_history_hop_posts_with_an_upload_sized_timeout(isolated_config, monkeypatch):
    seen = []

    def fake_post(*a, **kw):
        seen.append(kw.get("timeout"))
        return _Resp()

    monkeypatch.setattr(app.requests, "post", fake_post)
    app._upstream_chat("uncloseai", _big_payload(700_000), False)
    assert seen, "the hop never posted"
    connect, _read = seen[0]
    assert connect >= 43, "a 700 KB body got only %ss to upload" % connect


def test_a_small_hop_is_unchanged(isolated_config, monkeypatch):
    seen = []
    monkeypatch.setattr(app.requests, "post",
                        lambda *a, **kw: seen.append(kw.get("timeout")) or _Resp())
    app._upstream_chat("uncloseai", _big_payload(100), False)
    assert seen and seen[0][0] < app.CONNECT_TIMEOUT + 1


def test_the_streaming_header_wait_covers_the_upload(isolated_config, monkeypatch):
    waits = []

    def fake_deadline(deadline, post, **kw):
        waits.append(deadline)
        return _Resp()

    monkeypatch.setattr(app, "_post_with_header_deadline", fake_deadline)
    app._upstream_chat("uncloseai", _big_payload(700_000), True)
    assert waits and waits[0] >= app._STREAM_HEADER_WAIT + 33


def test_every_chat_post_site_sizes_its_connect_timeout():
    """No upstream CHAT post may go back to the flat connect timeout."""
    src = open(os.path.join(os.path.dirname(app.__file__), "app.py"), encoding="utf-8").read()
    body = src[src.index("def _upstream_chat("):src.index("def _upstream_chat(") + 30000]
    assert "timeout=(CONNECT_TIMEOUT" not in body.split("\ndef ")[0]
    puter = src[src.index("def _puter_post("):src.index("def _puter_post(") + 900]
    assert "_send_timeout(" in puter


# --------------------------------------------------------------------------- #
# The first-content peek grows with a HUGE prompt (prefill time), bounded.
# --------------------------------------------------------------------------- #

def test_ordinary_big_requests_keep_their_peek():
    assert app._stream_peek_timeout("llama-3.3-70b-versatile", 30000) == app.STREAM_SLOW_PEEK_TIMEOUT
    assert app._stream_peek_timeout("deepseek-r1", 30000) == app.STREAM_SLOW_BIG_PEEK_TIMEOUT
    assert app._stream_peek_timeout("llama-3.3-70b-versatile", 400) == app.STREAM_CONTENT_PEEK_TIMEOUT


def test_a_160k_history_gets_prefill_time():
    """glm-5.3 needed 113-163s on this history; the flat 90s cut it."""
    assert app._stream_peek_timeout("glm-5.3", 164000) >= 110
    assert app._stream_peek_timeout("deepseek-r1", 164000) >= 140


def test_the_prefill_allowance_is_capped():
    assert app._stream_peek_timeout("deepseek-r1", 10_000_000) == (
        app.STREAM_SLOW_BIG_PEEK_TIMEOUT + app.STREAM_PREFILL_EXTRA_CAP)
    assert app._huge_prefill_allowance("junk") == 0
