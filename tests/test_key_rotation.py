"""Multi-key rotation, for EVERY provider.

USER 2026-08-07 asked whether the hub can "handle multi api rotation for all
providers". It can, and always could -- but nothing pinned it, so this file
exists to make that answer verifiable instead of a claim.

The mechanism is one shared policy, not a per-provider special case:
_KEY_ROTATE_STATUSES = (401, 402, 403, 429) advances to the NEXT key in the
pool, and it is applied at all three places a provider credential is used:

  * _upstream_chat        -> every chat provider (all 3 protocols route here)
  * _call_image_generator -> every image provider
  * the Puter driver      -> its own non-REST path

_next_key_start also round-robins the STARTING key per provider, so a pool is
spread across requests rather than always hammering key[0] first.

The distinction that matters: a rotatable status (bad/exhausted key) tries the
next key, while a real upstream error (500, or a 200) must NOT burn the rest of
the pool -- it is returned immediately.
"""
import os
import shutil
import tempfile

import pytest

import app
import config


@pytest.fixture
def isolated_config(monkeypatch):
    # tempfile.mkdtemp, not pytest's tmp_path: tmp_path cannot read
    # %TEMP%\pytest-of-<user> in this environment (pre-existing PermissionError,
    # unrelated to anything under test here).
    d = tempfile.mkdtemp(prefix="hub-pytest-keyrot-")
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", os.path.join(d, "state", "config.json"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


class _Resp:
    def __init__(self, status):
        self.status_code = status
        self.headers = {}
        self.text = ""

    def json(self):
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    def close(self):
        pass


def _capture_keys(monkeypatch, statuses):
    """Record the bearer used per attempt; return `statuses` in order."""
    seen = []
    seq = list(statuses)

    def fake_post(url, **kw):
        auth = (kw.get("headers") or {}).get("Authorization")
        seen.append(auth.replace("Bearer ", "") if auth else None)
        return _Resp(seq[len(seen) - 1] if len(seen) <= len(seq) else seq[-1])

    monkeypatch.setattr(app.requests, "post", fake_post)
    return seen


@pytest.mark.parametrize("status", [401, 402, 403, 429])
def test_every_rotatable_status_advances_to_the_next_key(isolated_config, monkeypatch, status):
    for k in ("k1", "k2", "k3"):
        config.add_provider_key("groq", k)
    monkeypatch.setattr(app, "_next_key_start", lambda pid, n: 0)
    seen = _capture_keys(monkeypatch, [status, status, 200])
    resp = app._upstream_chat("groq", {"model": "m", "messages": []}, False)
    assert resp.status_code == 200
    assert seen == ["k1", "k2", "k3"], (
        "a %d must try the NEXT key, not give up on the provider: %r" % (status, seen))


def test_a_working_key_stops_the_rotation_immediately(isolated_config, monkeypatch):
    """A pool must not be walked for no reason -- every extra call spends real
    free quota on a provider that already answered."""
    for k in ("k1", "k2", "k3"):
        config.add_provider_key("groq", k)
    monkeypatch.setattr(app, "_next_key_start", lambda pid, n: 0)
    seen = _capture_keys(monkeypatch, [200])
    app._upstream_chat("groq", {"model": "m", "messages": []}, False)
    assert seen == ["k1"]


def test_a_real_upstream_error_does_not_burn_the_whole_pool(isolated_config, monkeypatch):
    """500 is the provider being broken, not the key being bad. Rotating would
    spend every key in the pool to collect the same 500 N times."""
    for k in ("k1", "k2", "k3"):
        config.add_provider_key("groq", k)
    monkeypatch.setattr(app, "_next_key_start", lambda pid, n: 0)
    seen = _capture_keys(monkeypatch, [500])
    resp = app._upstream_chat("groq", {"model": "m", "messages": []}, False)
    assert resp.status_code == 500
    assert seen == ["k1"]


def test_the_last_key_returns_its_own_error_rather_than_vanishing(isolated_config, monkeypatch):
    """Pool exhausted: the caller must still get a real response so the chain
    can fall through to the next provider."""
    for k in ("k1", "k2"):
        config.add_provider_key("groq", k)
    monkeypatch.setattr(app, "_next_key_start", lambda pid, n: 0)
    seen = _capture_keys(monkeypatch, [429, 429])
    resp = app._upstream_chat("groq", {"model": "m", "messages": []}, False)
    assert resp.status_code == 429
    assert seen == ["k1", "k2"]


def test_the_starting_key_round_robins_across_requests():
    """Without this a 3-key pool would always lead with key[0], so keys 2 and 3
    only ever serve as failover instead of sharing the load."""
    starts = {app._next_key_start("some-provider", 3) for _ in range(6)}
    assert len(starts) > 1, "start index never moved: the pool is not being spread"
    assert starts <= {0, 1, 2}


def test_rotation_policy_is_shared_by_the_image_path_too():
    """One policy, not a per-surface reimplementation -- the image generators
    read the same constant (see _call_image_generator)."""
    assert app._KEY_ROTATE_STATUSES == (401, 402, 403, 429)
    import inspect
    src = inspect.getsource(app._call_image_generator)
    assert "_next_key_start" in src and "_KEY_ROTATE_STATUSES" in src
