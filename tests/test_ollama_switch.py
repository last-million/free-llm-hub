"""The Ollama surface gets a switch, not a config flag.

ASKED 2026-09-03, after I wrote "turn on the ollama_api flag if you want Open
WebUI or Continue pointed at the hub": "and fix this too".

Fair. The surface is off by default for a real reason -- it lives under /api/,
where the dashboard's control API also lives, and no Ollama client can send the
control token, so it is an extra auth-less-by-default shape that should exist
only once someone asks for it. But "off by default" and "hidden behind a JSON
file nobody can find" are different things, and only the first was intended.

So it is a toggle on the Connect page, next to the Antigravity card, that also
hands over the connection values -- including the one detail that breaks most
setups: these apps want a HOST, not a /v1 base URL, because they append
/api/... themselves.
"""
from unittest import mock

import pytest

import app as A
import config


@pytest.fixture
def client():
    return A.app.test_client()


def _ctl():
    return {"X-Free-LLM-Hub-Token": config.ensure_control_token(),
            "X-Free-LLM-Hub": "dashboard"}


@pytest.fixture
def flag():
    """A real, writable flag, restored afterwards."""
    before = config.get_flag("ollama_api", False)
    yield
    config.set_flag("ollama_api", before)


# --------------------------------------------------------------------------- #
# The route
# --------------------------------------------------------------------------- #

def test_it_reports_the_current_state(client, flag):
    config.set_flag("ollama_api", False)
    assert client.get("/api/ollama", headers=_ctl()).get_json()["enabled"] is False
    config.set_flag("ollama_api", True)
    assert client.get("/api/ollama", headers=_ctl()).get_json()["enabled"] is True


def test_it_can_be_switched_on_and_off(client, flag):
    on = client.post("/api/ollama", headers=_ctl(), json={"enabled": True}).get_json()
    assert on["enabled"] is True and config.get_flag("ollama_api") is True
    off = client.post("/api/ollama", headers=_ctl(), json={"enabled": False}).get_json()
    assert off["enabled"] is False and config.get_flag("ollama_api") is False


def test_switching_it_on_really_opens_the_surface(client, flag):
    """The point of the switch: /api/tags answers afterwards, with no restart."""
    config.set_flag("ollama_api", False)
    assert client.get("/api/tags").status_code == 404
    client.post("/api/ollama", headers=_ctl(), json={"enabled": True})
    assert client.get("/api/tags").status_code == 200


def test_a_missing_or_wrong_enabled_is_refused(client, flag):
    for body in ({}, {"enabled": "yes"}, {"enabled": 1}):
        assert client.post("/api/ollama", headers=_ctl(), json=body).status_code == 400


def test_the_switch_is_a_control_endpoint(client):
    """It changes what the hub serves, so it is gated like every other control
    endpoint -- unlike the Ollama surface itself, which cannot be."""
    assert client.get("/api/ollama").status_code == 401
    assert client.post("/api/ollama", json={"enabled": True}).status_code == 403


# --------------------------------------------------------------------------- #
# The values it hands over
# --------------------------------------------------------------------------- #

def test_it_gives_a_host_url_not_a_v1_base(client, flag):
    """These apps append /api/... themselves. Handing them the /v1 URL is the
    usual cause of "connection refused" on an otherwise correct setup."""
    d = client.get("/api/ollama", headers=_ctl()).get_json()
    assert d["base_url"].endswith(str(A.PORT))
    assert not d["base_url"].endswith("/v1")
    assert d["openai_base_url"].endswith("/v1")


def test_it_says_whether_a_key_is_needed(client, flag):
    with mock.patch.object(A.config, "get_local_api_key", return_value=None):
        assert client.get("/api/ollama", headers=_ctl()).get_json()["key_required"] is False
    with mock.patch.object(A.config, "get_local_api_key", return_value="secret"):
        d = client.get("/api/ollama", headers=_ctl()).get_json()
        assert d["key_required"] is True and d["api_key"] == "secret"


def test_it_lists_the_paths_it_serves(client, flag):
    paths = client.get("/api/ollama", headers=_ctl()).get_json()["paths"]
    for p in ("/api/tags", "/api/chat", "/api/embed"):
        assert p in paths


def test_it_offers_the_virtual_models(client, flag):
    assert client.get("/api/ollama", headers=_ctl()).get_json()["models"] == [
        "auto", "best", "swarm"]


# --------------------------------------------------------------------------- #
# The card
# --------------------------------------------------------------------------- #

def _template():
    with open("templates/index.html", encoding="utf-8") as f:
        return f.read()


def test_the_card_is_rendered_and_refreshed():
    html = _template()
    assert 'id="ollama-card"' in html
    assert "function loadOllama()" in html
    assert "loadAntigravity(); loadOllama(); loadMcp();" in html


def test_the_card_has_a_real_switch():
    html = _template()
    body = html[html.index("function renderOllama("):][:4000]
    assert 'id="ollama-on"' in body
    assert "setOllama(sw.checked" in body


def test_the_card_warns_about_the_v1_url():
    """The mistake that breaks most setups, said where it is made."""
    body = _template()
    i = body.index("function renderOllama(")
    assert "not a /v1 address" in body[i:i + 4000]


def test_the_card_explains_why_it_is_off_by_default():
    body = _template()
    i = body.index("function renderOllama(")
    assert "control port" in body[i:i + 4000]
