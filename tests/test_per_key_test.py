"""Test button: report EVERY saved key, not just the first one.

A provider can hold several keys and the router rotates across them. The test
endpoint only ever exercised the primary, so a pool with one good key and one
dead key reported a single green verdict -- while routing kept rotating onto
the dead one and burning an attempt on every request. Worse, the models-listing
step returned early on the primary's failure, so key 2 was never reached at all.

That is the exact shape of the live case this fixes: two Together keys, both
reported as one opaque failure, no way to tell which (or whether either) worked.
"""
from unittest import mock

import app
import config


def _client():
    app.app.config["TESTING"] = True
    return app.app.test_client()


def _hdrs():
    return {"X-Free-LLM-Hub-Token": config.ensure_control_token(),
            "X-Free-LLM-Hub": "dashboard"}


def _resp(status, body=None):
    r = mock.Mock(status_code=status)
    r.json.return_value = body if body is not None else {"choices": [{"message": {"content": "hi"}}]}
    r.headers = {}
    r.text = "{}"
    r.close = mock.Mock()
    return r


def test_each_key_is_tested_separately_and_reported():
    """The headline: one good key, one dead key -> the response names both."""
    seen = []

    def fake_chat(pid, payload, stream, only_key=app._NO_KEY_PIN):
        seen.append(only_key)
        return _resp(200 if only_key == "GOOD" else 401)

    with mock.patch.object(config, "get_provider_config",
                           return_value={"api_key": "GOOD", "api_keys": ["GOOD", "DEAD"], "enabled": True}), \
            mock.patch.object(app, "_models_url_for", return_value=None), \
            mock.patch.object(app, "_upstream_chat", side_effect=fake_chat), \
            mock.patch.object(app, "_record_test_result", return_value=([], [])):
        r = _client().post("/api/test/groq", headers=_hdrs())
    body = r.get_json()

    # Both keys were actually exercised, pinned, one each -- and the dead one
    # stopped at its first 401 instead of re-asking every candidate model.
    assert seen == ["GOOD", "DEAD"], seen
    rows = {k["index"]: k for k in body["keys"]}
    assert len(rows) == 2
    assert rows[0]["ok"] is True
    assert rows[1]["ok"] is False
    # The provider is usable (a key works), and the summary names the bad one.
    assert body["ok"] is True
    assert "NOT working: #2" in body["detail"], body["detail"]


def test_no_key_is_ever_echoed_back():
    """The rows identify a key so the user can delete the right one -- they must
    never carry the secret itself."""
    def fake_chat(pid, payload, stream, only_key=app._NO_KEY_PIN):
        return _resp(200)

    secrets = ["sk-super-secret-alpha", "sk-super-secret-beta"]
    with mock.patch.object(config, "get_provider_config",
                           return_value={"api_key": secrets[0], "api_keys": list(secrets), "enabled": True}), \
            mock.patch.object(app, "_models_url_for", return_value=None), \
            mock.patch.object(app, "_upstream_chat", side_effect=fake_chat), \
            mock.patch.object(app, "_record_test_result", return_value=([], [])):
        r = _client().post("/api/test/groq", headers=_hdrs())
    raw = r.get_data(as_text=True)
    for s in secrets:
        assert s not in raw
    assert len(r.get_json()["keys"]) == 2


def test_all_keys_working_says_so():
    def fake_chat(pid, payload, stream, only_key=app._NO_KEY_PIN):
        return _resp(200)

    with mock.patch.object(config, "get_provider_config",
                           return_value={"api_key": "A", "api_keys": ["A", "B", "C"], "enabled": True}), \
            mock.patch.object(app, "_models_url_for", return_value=None), \
            mock.patch.object(app, "_upstream_chat", side_effect=fake_chat), \
            mock.patch.object(app, "_record_test_result", return_value=([], [])):
        body = _client().post("/api/test/groq", headers=_hdrs()).get_json()
    assert body["ok"] is True
    assert "All 3 keys work" in body["detail"]
    assert all(k["ok"] for k in body["keys"])


def test_every_key_dead_reports_failure_not_a_false_green():
    def fake_chat(pid, payload, stream, only_key=app._NO_KEY_PIN):
        return _resp(401)

    with mock.patch.object(config, "get_provider_config",
                           return_value={"api_key": "X", "api_keys": ["X", "Y"], "enabled": True}), \
            mock.patch.object(app, "_models_url_for", return_value=None), \
            mock.patch.object(app, "_upstream_chat", side_effect=fake_chat), \
            mock.patch.object(app, "_record_test_result", return_value=([], [])):
        body = _client().post("/api/test/groq", headers=_hdrs()).get_json()
    assert body["ok"] is False
    assert "None of the 2 keys work" in body["detail"]
    assert [k["ok"] for k in body["keys"]] == [False, False]


def test_a_dead_primary_no_longer_hides_a_working_second_key():
    """The models-listing step used the PRIMARY key and returned early when it
    failed -- so a working key sitting behind a dead one was never reached."""
    def fake_get(url, **kw):
        return _resp(401, {})

    def fake_chat(pid, payload, stream, only_key=app._NO_KEY_PIN):
        return _resp(200 if only_key == "LIVE" else 401)

    with mock.patch.object(config, "get_provider_config",
                           return_value={"api_key": "DEAD", "api_keys": ["DEAD", "LIVE"], "enabled": True}), \
            mock.patch.object(app, "_models_url_for", return_value="https://x/models"), \
            mock.patch.object(app.requests, "get", side_effect=fake_get), \
            mock.patch.object(app, "_upstream_chat", side_effect=fake_chat), \
            mock.patch.object(app, "_record_test_result", return_value=([], [])):
        body = _client().post("/api/test/groq", headers=_hdrs()).get_json()
    assert body["ok"] is True, body["detail"]
    assert [k["ok"] for k in body["keys"]] == [False, True]


def test_single_key_provider_keeps_the_old_unpinned_call_shape():
    """One key must still go through the normal pool path, so a stand-in for
    _upstream_chat that predates only_key keeps working."""
    calls = []

    def old_style_chat(pid, payload, stream):      # NOTE: no only_key parameter
        calls.append(pid)
        return _resp(200)

    with mock.patch.object(config, "get_provider_config",
                           return_value={"api_key": "ONLY", "api_keys": ["ONLY"], "enabled": True}), \
            mock.patch.object(app, "_models_url_for", return_value=None), \
            mock.patch.object(app, "_upstream_chat", side_effect=old_style_chat), \
            mock.patch.object(app, "_record_test_result", return_value=([], [])):
        body = _client().post("/api/test/groq", headers=_hdrs()).get_json()
    assert calls, "the single-key path must still call through"
    assert body["ok"] is True
    assert len(body["keys"]) == 1 and body["keys"][0]["ok"] is True


def test_upstream_chat_pins_to_exactly_the_requested_key():
    """The mechanism itself: pinning must bypass pool rotation entirely."""
    sent = []

    def fake_post(url, **kw):
        sent.append((kw.get("headers") or {}).get("Authorization"))
        return _resp(200)

    with mock.patch.object(config, "get_provider_config",
                           return_value={"api_key": "one", "api_keys": ["one", "two", "three"]}), \
            mock.patch.object(app, "_resolve_base_url", return_value="https://x/v1"), \
            mock.patch.object(app.requests, "post", side_effect=fake_post), \
            mock.patch.object(app.quota, "record"), \
            mock.patch.object(app.quota, "observe_headers"):
        app._upstream_chat("groq", {"model": "m", "messages": []}, False, only_key="two")
    assert sent == ["Bearer two"], sent
