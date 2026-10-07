"""Big tool turns that used to time out (2026-10-07).

Live evidence (hub.log, OpenCode coding-swarm, ~79K-token stream tool turns):
  * `[quality-fallback] best->auto -> groq/qwen3.8-27b` 25x -- a ~8K-TPM model
    picked for a 79K request, which can only _ContextOverflow.
  * roles actors "no answer in time" (nvidia/kimi-k3, glm-4.7-flash) -- the
    3-way actor split (~60 s) cut a healthy slow model that needed longer.
  * ConnectionError bursts (5 hops in ~4 s) -- suspected to be the hub's own
    clientgone shutdown poisoning a reused socket / a leaked cancel token.
  * zenmux 403 and "a tool call the CLI cannot run" re-dispatched every hop.
  * a 503 blaming "35 model(s) are switched OFF" when none could have fitted.

These tests pin each fix. The clientgone piece is a REAL reproduction (raw TCP
fake provider, real `requests`) that proves the ConnectionError bursts are NOT
the hub's clientgone work: the hub posts per call (a fresh pool each time) and
urllib3 2.x discards a dropped pooled socket, so nothing is ever reused and no
cancel token leaks into a later hop.
"""
import json
import socket
import threading
import time

import pytest

import app as A
import clientgone
import requests


# --------------------------------------------------------------------------- #
# 1. ConnectionError bursts are NOT the hub's clientgone work (real repro)
# --------------------------------------------------------------------------- #
class _RawHTTP:
    """Minimal keep-alive HTTP/1.1 server on a raw socket; counts accepted
    connections so a reused (vs fresh) socket is observable."""
    def __init__(self):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(16)
        self.port = self.srv.getsockname()[1]
        self.accepted = 0
        self._stop = False
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self._stop:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            self.accepted += 1
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        c.settimeout(10)
        try:
            while True:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = c.recv(65536)
                    if not chunk:
                        return
                    data += chunk
                hdr = data.split(b"\r\n\r\n", 1)[0].decode("latin1").lower()
                clen = 0
                for line in hdr.split("\r\n"):
                    if line.startswith("content-length:"):
                        clen = int(line.split(":", 1)[1].strip())
                body = data.split(b"\r\n\r\n", 1)[1]
                while len(body) < clen:
                    body += c.recv(65536)
                payload = b'{"ok":true}'
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                          b"Content-Length: %d\r\nConnection: keep-alive\r\n\r\n%s"
                          % (len(payload), payload))
        except OSError:
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass

    def stop(self):
        self._stop = True
        try:
            self.srv.close()
        except OSError:
            pass


@pytest.fixture
def raw_http():
    s = _RawHTTP()
    clientgone.install_urllib3_hook()
    try:
        yield s
    finally:
        clientgone.set_current(None)
        s.stop()


def _post(url, session=None):
    fn = session.post if session is not None else requests.post
    return fn(url, json={"x": "y"}, timeout=(5, 5))


def test_a_shutdown_connection_is_never_reused_per_call_pattern(raw_http):
    """The hub's pattern: requests.post per hop (a fresh pool each call).
    Cancelling a token shuts its sockets down; the NEXT hop must still succeed."""
    url = "http://127.0.0.1:%d/v1/chat/completions" % raw_http.port
    tok = clientgone.Token("req-1")
    clientgone.set_current(tok)
    r1 = _post(url); assert r1.status_code == 200; r1.close()
    tok.cancel("client gone")                      # shuts tok's tracked sockets
    tok2 = clientgone.Token("req-2")
    clientgone.set_current(tok2)
    r2 = _post(url)                                # must NOT ConnectionError
    assert r2.status_code == 200
    r2.close()


def test_a_shared_pool_discards_the_dropped_socket(raw_http):
    """Even in the worst case (a SHARED Session/pool, which the hub does not
    use), urllib3 2.x discards a socket we shut down and opens a fresh one --
    no poisoned reuse, so no instant ConnectionError."""
    url = "http://127.0.0.1:%d/v1/chat/completions" % raw_http.port
    sess = requests.Session()
    try:
        t1 = clientgone.Token("t1")
        clientgone.set_current(t1)
        _post(url, sess).close()                   # conn pooled under t1
        before = raw_http.accepted
        t1.cancel("client gone")                   # shuts the pooled socket
        t2 = clientgone.Token("t2")
        clientgone.set_current(t2)
        r = _post(url, sess)                        # must reopen, not reuse
        assert r.status_code == 200
        r.close()
        assert raw_http.accepted > before          # a FRESH socket was opened
    finally:
        sess.close()


def test_a_cancelled_token_does_not_leak_into_the_next_hop(raw_http):
    """A cancelled token refuses a NEW upstream call (correct); a fresh token
    after it connects fine -- the token never leaks across hops."""
    url = "http://127.0.0.1:%d/v1/chat/completions" % raw_http.port
    dead = clientgone.Token("cancelled")
    dead.cancel("client gone")
    clientgone.set_current(dead)
    with pytest.raises(clientgone.ClientGone):
        _post(url)
    clientgone.set_current(clientgone.Token("fresh"))
    r = _post(url); assert r.status_code == 200; r.close()


# --------------------------------------------------------------------------- #
# 2. A last-chance pick never gets a request it cannot hold
# --------------------------------------------------------------------------- #
@pytest.fixture
def _fit(monkeypatch):
    windows = {"small": (8000, "learned"), "big": (200000, "learned"),
               "mid": (200000, "learned")}
    monkeypatch.setattr(A, "_model_ctx_info",
                        lambda p, m: windows.get(m, (131072, "default")))
    monkeypatch.setattr(A, "_benchmark_score", lambda p, m: {"small": 130, "big": 140,
                                                             "mid": 135}.get(m, 120))
    for name in ("_is_fast", ):
        monkeypatch.setattr(A, name, lambda p, m: True)
    monkeypatch.setattr(A, "_recent_hop_stall", lambda p, m: False)
    monkeypatch.setattr(A, "_is_pair_benched", lambda p, m: False)
    monkeypatch.setattr(A, "_is_low_quality", lambda m: False)
    monkeypatch.setattr(A, "_chain_reliability_band", lambda p, m: 0)
    monkeypatch.setattr(A, "_recent_hop_failure", lambda p, m: False)
    monkeypatch.setattr(A, "_simple_speed_rank", lambda p, m: (False, False))
    monkeypatch.setattr(A, "_apply_mode", lambda pool: pool)
    return windows


def test_quality_fallback_skips_a_model_that_cannot_fit(_fit):
    entries = [("groq", "small"), ("nv", "big")]
    picks = A._quality_fallback_pick(entries, est=79000)
    assert ("groq", "small") not in picks
    assert ("nv", "big") in picks


def test_quality_fallback_is_empty_when_nothing_fits(_fit):
    # only a too-small model left: no pick at all, so the chain goes straight to
    # the native overflow reply instead of a dead groq hop.
    assert A._quality_fallback_pick([("groq", "small")], est=79000) == []


def test_quality_fallback_without_est_is_unchanged(_fit):
    # back-compat: no est means no fit filter (existing callers/tests).
    picks = A._quality_fallback_pick([("groq", "small"), ("nv", "big")])
    assert ("groq", "small") in picks and ("nv", "big") in picks


def test_window_fits_bar(monkeypatch):
    monkeypatch.setattr(A, "_model_ctx_info", lambda p, m: {
        "small": (8000, "learned"), "big": (262144, "learned"),
        "guess": (60000, "default"), "tablerow": (131072, "table")}[m])
    assert A._window_fits("groq", "small", 79000) is False
    assert A._window_fits("nv", "big", 79000) is True
    assert A._window_fits("x", "guess", 79000) is True        # "default" proves nothing
    assert A._window_fits("nv", "tablerow", 79000) is True    # plain table row, not a hard cap
    assert A._window_fits("groq", "small", 0) is True         # no size -> fits


def test_groq_hard_cap_cannot_hold_a_big_request():
    # groq's own window is capped at its per-request TPM (_PROVIDER_HARD_REQUEST_CAP),
    # so _window_fits must read it as too small even if the catalog says 131072.
    assert "groq" in A._PROVIDER_HARD_REQUEST_CAP


# --------------------------------------------------------------------------- #
# 3. Actor budget for a big request fits >= 2 attempts and does not cut short
# --------------------------------------------------------------------------- #
def test_role_hop_deadline_caps_the_split_on_a_big_request():
    clock = A._ChainClock(tools=True, est=79000)
    # 180 s turn, 3 nominal attempts: a small request gets room/3 (~60 s)...
    small = A._role_hop_deadline(clock, "nv", "kimi", 180.0, 3, 1000, None)
    assert 55.0 <= small <= 65.0
    # ...a BIG request caps the split at 2 so each actor gets ~room/2 (~90 s).
    big = A._role_hop_deadline(clock, "nv", "kimi", 180.0, 3, 79000, None)
    assert big >= 85.0
    assert big <= 180.0


def test_role_hop_deadline_lifts_to_a_measured_slow_model(monkeypatch):
    clock = A._ChainClock(tools=True, est=79000)
    # seed a measured long-context p90 of ~100 s for this pair (non-stalled)
    now = time.time()
    with A._outcome_lock:
        A._long_ctx_speed[("nv", "slow")] = [(now, 100000.0, False), (now, 100000.0, False)]
    try:
        d = A._role_hop_deadline(clock, "nv", "slow", 180.0, 3, 79000, None)
        # it gets at least its measured speed (capped at 0.6*room so a 2nd fits)
        assert d >= 100.0
        assert d <= 180.0 * 0.6 + 0.01
    finally:
        with A._outcome_lock:
            A._long_ctx_speed.pop(("nv", "slow"), None)


def test_role_hop_deadline_respects_an_explicit_budget():
    clock = A._ChainClock(tools=True, est=79000)
    assert A._role_hop_deadline(clock, "nv", "kimi", 180.0, 3, 79000, 30.0) == 30.0


# --------------------------------------------------------------------------- #
# 4. Roles failure handling: 403 rests the pair
# --------------------------------------------------------------------------- #
def test_a_403_rests_the_pair(monkeypatch):
    seen = {}
    monkeypatch.setattr(A, "_note_recent_hop_failure",
                        lambda p, m, kind: seen.__setitem__((p, m), kind))
    monkeypatch.setattr(A, "_note_swarm_member_fail", lambda *a, **k: None)
    race = set()
    A._swarm_note_member_status("zenmux", "glm-4.7-flash-free", 403, race_failed=race)
    assert seen.get(("zenmux", "glm-4.7-flash-free")) == "http-403"
    assert "zenmux" in race


def test_a_500_still_throttles_not_rests(monkeypatch):
    throttled = []
    monkeypatch.setattr(A, "_throttle_failed_hop", lambda p, m: throttled.append((p, m)))
    monkeypatch.setattr(A, "_note_recent_hop_failure",
                        lambda p, m, kind: pytest.fail("5xx must not rest-mark"))
    monkeypatch.setattr(A, "_note_swarm_member_fail", lambda *a, **k: None)
    A._swarm_note_member_status("pollinations", "openai-fast", 500)
    assert throttled == [("pollinations", "openai-fast")]


# --------------------------------------------------------------------------- #
# 5. Activity: a swarm/roles turn keeps the requested MODE next to the model
# --------------------------------------------------------------------------- #
def test_activity_row_keeps_the_requested_mode_and_the_resolved_model():
    with A.app.test_request_context(
            "/v1/chat/completions", method="POST",
            json={"model": "coding-swarm", "messages": [{"role": "user", "content": "hi"}]}):
        A.g.pop("act", None)
        A._activity_before()
        act = A.g.act
        assert act["model_req"] == "coding-swarm"
        # the roles path resolves to one actor model; model_req must survive so
        # the dashboard can show "coding-swarm -> nvidia/muse-glimmer-30b".
        A._act_pick("nvidia", "meta/muse-glimmer-30b")
        assert act["model_req"] == "coding-swarm"
        assert act["provider"] == "nvidia" and act["model"] == "meta/muse-glimmer-30b"


# --------------------------------------------------------------------------- #
# 6. A responses stream with a bare "error": null envelope is not "error"
# --------------------------------------------------------------------------- #
def test_error_null_envelope_is_not_a_failed_stream():
    assert not A._STREAM_ERROR_VALUE_RE.search(b'data: {"error": null}')
    assert not A._STREAM_ERROR_VALUE_RE.search(b'data: {"error": {}}')
    assert A._STREAM_ERROR_VALUE_RE.search(b'data: {"error": {"message": "boom"}}')
    assert A._STREAM_ERROR_VALUE_RE.search(b'event: error')


def test_a_completed_responses_stream_with_error_null_is_not_marked_error():
    frames = [b'data: {"type":"response.created","response":{}}\n\n',
              b'data: {"error": null}\n\n',
              b'data: {"type":"response.completed","response":{}}\n\n']
    with A.app.test_request_context("/v1/responses", method="POST"):
        act = {"status": "in_progress", "finished": None, "started": time.time()}
        A.g.act = act
        resp = A.Response(iter(frames), mimetype="text/event-stream", status=200)
        out = A._activity_after(resp)
        list(out.response)          # consume the wrapped body -> runs the finalizer
        assert act["status"] in ("empty", "ok")
        assert act["status"] != "error"


# --------------------------------------------------------------------------- #
# 7. Blocklist aliases: a cover-name is blocked when its alias identity is
# --------------------------------------------------------------------------- #
def test_an_aliased_model_is_blocked_when_its_alias_identity_is(monkeypatch):
    monkeypatch.setitem(A._CTX_ALIASES, ("pollinations", "openai-fast"),
                        ["openai", "gpt-oss", "gpt-oss-20b"])
    monkeypatch.setattr(A, "_blocked_models", lambda: set())
    monkeypatch.setattr(A, "_blocked_identities", lambda: {"gpt-oss-20b"})
    monkeypatch.setattr(A, "_allowed_identities", lambda: set())
    with A.app.test_request_context("/v1/chat/completions"):
        assert A._is_model_blocked_by_user("pollinations", "openai-fast") is True
        # a model whose aliases are not on the list stays allowed.
        monkeypatch.setitem(A._CTX_ALIASES, ("nv", "kimi-k3"), ["moonshotai/kimi-k3"])
        assert A._is_model_blocked_by_user("nv", "kimi-k3") is False


# --------------------------------------------------------------------------- #
# 8. The "models switched off" note only when a blocked model could have fitted
# --------------------------------------------------------------------------- #
def test_the_off_note_is_honest_about_a_big_request(monkeypatch):
    # three off-list ids, all too small for a 79K request -> the note must NOT
    # blame the Settings switch; it states the real constraint.
    monkeypatch.setattr(A, "_blocked_models",
                        lambda: {"groq/gpt-oss-20b", "nv/gemma-4-31b-it", "p/nemotron-3-nano"})
    monkeypatch.setattr(A, "_model_ctx_info", lambda p, m: (8000, "learned"))
    monkeypatch.setattr(A, "_supports_tools", lambda p, m: True)
    monkeypatch.setattr(A, "_is_low_quality", lambda m: False)
    note = A._no_candidates_hint(est=79000, tools=True)
    assert "switched OFF" not in note
    assert "~79000 tokens" in note


def test_the_off_note_fires_when_a_blocked_model_would_have_fitted(monkeypatch):
    monkeypatch.setattr(A, "_blocked_models", lambda: {"nv/big-model"})
    monkeypatch.setattr(A, "_model_ctx_info", lambda p, m: (262144, "learned"))
    monkeypatch.setattr(A, "_supports_tools", lambda p, m: True)
    monkeypatch.setattr(A, "_is_low_quality", lambda m: False)
    note = A._no_candidates_hint(est=79000, tools=True)
    assert "1 model(s) are switched OFF" in note


def test_the_off_note_without_a_size_counts_every_block(monkeypatch):
    monkeypatch.setattr(A, "_blocked_models", lambda: {"a/b", "c/d"})
    note = A._no_candidates_hint()
    assert "2 model(s) are switched OFF" in note
