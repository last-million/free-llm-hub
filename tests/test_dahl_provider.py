"""Dahl Inference (Gonka): registry shape + the one-click free-key button.

The vendor documents anonymous instant keys verbatim ("Keys are free and each
includes 100 million tokens. There is no payment UI yet — create another key when
a key's allowance is spent."), so the card offers a button instead of a link to a
signup page. Everything asserted here was verified against the live API on
2026-07-31; the network is mocked so the suite stays offline and deterministic.
"""
import json
from unittest import mock

import pytest

import config
import providers as prov


P = prov.PROVIDERS["dahl"]

_DASH = {"X-Free-LLM-Hub": "dashboard"}


def _auth():
    """Control routes are token-guarded; a dashboard header alone gets a 403."""
    return dict(_DASH, **{"X-Free-LLM-Hub-Token": config.ensure_control_token()})


def test_registered_with_the_verified_endpoints():
    assert P["base_url"] == "https://inference.dahl.global/v1"
    assert P["models_url"] == "https://inference.dahl.global/v1/models"
    assert P["key_mint_url"] == "https://inference.dahl.global/tokens"


def test_only_models_that_actually_answer_are_listed():
    """GLM-5.2 is advertised as coming soon but 400s 'unsupported model' today."""
    assert P["default_free_models"] == ["moonshotai/Kimi-K2.6",
                                        "MiniMaxAI/MiniMax-M2.7"]
    assert not any("glm" in m.lower() for m in P["default_free_models"])


def test_kimi_id_is_k2_6_not_k2_7():
    """/v1/models returns moonshotai/Kimi-K2.6. A wrong id 400s on every call."""
    assert "moonshotai/Kimi-K2.6" in P["default_free_models"]


def test_no_vision_models_even_though_the_site_badges_vision():
    """Image parts are SILENTLY DROPPED: 200 OK, prompt_tokens counts text only,
    and the model answers "I cannot see an image". Routing vision here would fail
    unsafely — the caller gets a confident answer about an image never seen."""
    assert not P.get("vision_models")


def test_not_no_key_and_not_static_key():
    """Inference 401s "Missing API token", and each key owns a PRIVATE 100M pool
    — a key shipped in this repo would burn one allowance for every user."""
    assert not P.get("no_key")
    assert not P.get("static_key")


@pytest.fixture
def client():
    import app
    app.app.config["TESTING"] = True
    with app.app.test_client() as c:
        yield c


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def test_mint_key_saves_the_key_and_enables_the_provider(client):
    import app
    minted = {"available_tokens": 100000000, "token": "dahl_TESTKEY123"}
    with mock.patch.object(app.requests, "post",
                           return_value=_Resp(201, minted)) as post, \
         mock.patch.object(app.config, "add_provider_key") as add, \
         mock.patch.object(app.config, "get_provider_config",
                           return_value={"enabled": False}), \
         mock.patch.object(app.config, "set_provider_config"), \
         mock.patch.object(app, "_provider_row", return_value={"id": "dahl"}):
        r = client.post("/api/providers/dahl/mint-key", json={}, headers=_auth())
    assert r.status_code == 200
    post.assert_called_once()
    assert post.call_args[0][0] == "https://inference.dahl.global/tokens"
    add.assert_called_once_with("dahl", "dahl_TESTKEY123")
    assert "100,000,000" in r.get_json()["note"]


def test_mint_key_refused_for_providers_that_do_not_offer_it(client):
    """The URL comes from OUR registry, never the request — so this endpoint can
    never be pointed at an arbitrary host."""
    r = client.post("/api/providers/groq/mint-key", json={}, headers=_auth())
    assert r.status_code == 400
    assert "instant free keys" in r.get_json()["error"]


def test_mint_key_surfaces_an_upstream_failure(client):
    import app
    with mock.patch.object(app.requests, "post",
                           return_value=_Resp(429, {"error": "too many signup attempts"})):
        r = client.post("/api/providers/dahl/mint-key", json={}, headers=_auth())
    assert r.status_code == 502
    assert "429" in r.get_json()["error"]
