"""A provider's several API keys carry the load TOGETHER, at the same time.

Owner, 2026-10-27: "even for multi API keys I want them to be used equally at the same time, not till
the end of one of them and then switch to the next one."

What already held: _next_key_start is a per-request round-robin cursor, so N keys get 1/N of the
requests each and a key is only left on a failure (a 429 / dead mark). What it ignored is WHO IS BUSY:
a key holding a slow 60 s stream still got its turn, so under concurrency one key could be carrying
several requests while its siblings sat idle -- which is where per-key parallel limits bite
(dahl documents `parallel_key_limit`). Now each request starts on the key with the fewest requests in
flight (counted around the real send), ties broken by the same round-robin cursor, so an idle key is
always preferred and equal load still rotates. Flag key_least_loaded (default on; off = the plain cursor).
"""
import json
import threading
import time

import pytest

import app
import quota

KEYS = ["key-A", "key-B", "key-C", "key-D"]


class _Ok:
    status_code = 200
    headers = {}

    def __init__(self):
        self.text = json.dumps({"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    def json(self):
        return json.loads(self.text)

    def close(self):
        pass


@pytest.fixture
def pool(monkeypatch):
    seen = []
    gates = {}                       # key -> Event the fake send waits on
    inside = {}                      # key -> Event set once the send has started
    held = set()

    def fake_post(url, **kw):
        key = kw["headers"]["Authorization"].split()[-1]
        seen.append(key)
        if key in inside:
            inside[key].set()
        if key in gates and key not in held:      # only the FIRST request on a gated key is held
            held.add(key)
            gates[key].wait(10)
        return _Ok()

    monkeypatch.setattr(app.config, "get_provider_config",
                        lambda pid: {"api_keys": list(KEYS), "enabled": True})
    monkeypatch.setattr(app.requests, "post", fake_post)
    monkeypatch.setattr(quota, "usable_keys", lambda pid, ks: list(ks))
    app._provider_key_cursor.clear()
    app._KEY_INFLIGHT.clear()
    yield type("P", (), {"seen": seen, "gates": gates, "inside": inside})
    for ev in gates.values():
        ev.set()
    app._provider_key_cursor.clear()
    app._KEY_INFLIGHT.clear()


def _call():
    return app._upstream_chat("mistral", {"model": "m", "messages": []}, False)


def _flag(monkeypatch, on):
    real = app.config.get_flag
    monkeypatch.setattr(app.config, "get_flag",
                        lambda k, d=None: on if k == "key_least_loaded" else real(k, d))


def test_requests_spread_evenly_over_every_key(pool):
    for _ in range(12):
        _call()
    assert sorted(pool.seen.count(k) for k in KEYS) == [3, 3, 3, 3], pool.seen


def test_the_keys_rotate_rather_than_run_dry_in_turn(pool):
    for _ in range(8):
        _call()
    # no key serves two requests in a row while the others are idle
    assert all(a != b for a, b in zip(pool.seen, pool.seen[1:])), pool.seen


def test_a_busy_key_is_skipped_while_an_idle_one_exists(pool):
    pool.gates["key-A"] = threading.Event()
    pool.inside["key-A"] = threading.Event()
    t = threading.Thread(target=_call)            # takes key-A and holds it
    t.start()
    assert pool.inside["key-A"].wait(5)
    try:
        for _ in range(7):                        # the cursor comes round to key-A more than once
            _call()
        assert pool.seen.count("key-A") == 1, pool.seen
        assert {k for k in pool.seen if k != "key-A"} == {"key-B", "key-C", "key-D"}
    finally:
        pool.gates["key-A"].set()
        t.join(5)


def test_the_busy_key_is_used_again_once_it_is_free(pool):
    pool.gates["key-A"] = threading.Event()
    pool.inside["key-A"] = threading.Event()
    t = threading.Thread(target=_call)
    t.start()
    assert pool.inside["key-A"].wait(5)
    pool.gates["key-A"].set()
    t.join(5)
    for _ in range(8):
        _call()
    assert pool.seen.count("key-A") >= 2, pool.seen


def test_with_the_flag_off_it_is_the_plain_cursor(pool, monkeypatch):
    _flag(monkeypatch, False)
    pool.gates["key-A"] = threading.Event()
    pool.inside["key-A"] = threading.Event()
    t = threading.Thread(target=_call)
    t.start()
    assert pool.inside["key-A"].wait(5)
    try:
        for _ in range(4):
            _call()
        assert pool.seen.count("key-A") >= 2, "the busy key still gets its turn without the flag"
    finally:
        pool.gates["key-A"].set()
        t.join(5)


def test_the_in_flight_count_returns_to_zero(pool):
    for _ in range(6):
        _call()
    assert all(v == 0 for v in app._KEY_INFLIGHT.values()), app._KEY_INFLIGHT


def test_the_count_also_returns_to_zero_when_the_send_raises(monkeypatch):
    monkeypatch.setattr(app.config, "get_provider_config",
                        lambda pid: {"api_keys": list(KEYS), "enabled": True})
    monkeypatch.setattr(quota, "usable_keys", lambda pid, ks: list(ks))
    app._KEY_INFLIGHT.clear()

    def boom(url, **kw):
        raise app.requests.exceptions.ConnectionError("down")
    monkeypatch.setattr(app.requests, "post", boom)
    with pytest.raises(Exception):
        _call()
    assert all(v == 0 for v in app._KEY_INFLIGHT.values()), app._KEY_INFLIGHT


def test_a_single_key_provider_is_untouched(monkeypatch):
    monkeypatch.setattr(app.config, "get_provider_config",
                        lambda pid: {"api_keys": ["only"], "enabled": True})
    monkeypatch.setattr(quota, "usable_keys", lambda pid, ks: list(ks))
    seen = []
    monkeypatch.setattr(app.requests, "post",
                        lambda url, **kw: (seen.append(kw["headers"]["Authorization"]), _Ok())[1])
    for _ in range(3):
        _call()
    assert seen == ["Bearer only"] * 3


def test_the_key_is_never_stored_only_its_fingerprint(pool):
    _call()
    for (pid, fp) in list(app._KEY_INFLIGHT):
        assert fp not in KEYS and len(fp) == 16
