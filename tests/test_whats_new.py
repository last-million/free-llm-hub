"""Tests for the "what's new" update popup plumbing and the recommended
providers zone:

- GET /api/version exposes the running-version stamp (short git HEAD with a
  fallback constant) so the dashboard can detect an update since last visit.
- The puter registry entry carries the recommended flag (pinned zone).
- templates/index.html contains the frontend markers the popup and the
  recommended zone rely on (localStorage key, zone hook, first-run guard).

NOTE: no pytest tmp_path here — this machine's basetemp is permission-denied;
tempfile.mkdtemp(prefix="hub-pytest-") works.
"""

import os
import shutil
import tempfile

import pytest

import app
import config
import providers

REPO_ROOT = os.path.dirname(os.path.abspath(app.__file__))
TEMPLATE = os.path.join(REPO_ROOT, "templates", "index.html")


@pytest.fixture
def isolated_config(monkeypatch):
    root = tempfile.mkdtemp(prefix="hub-pytest-")
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", os.path.join(root, "state", "config.json"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _client():
    return app.app.test_client()


def _template_text():
    with open(TEMPLATE, "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------- /api/version

def test_version_endpoint_shape(isolated_config, monkeypatch):
    monkeypatch.setattr(config, "get_control_token", lambda: "secret-token")
    client = _client()
    # Gated like every other /api/* read: no token -> 401.
    r = client.get("/api/version")
    assert r.status_code == 401
    r = client.get("/api/version", headers={"X-Free-LLM-Hub-Token": "secret-token"})
    assert r.status_code == 200
    payload = r.get_json()
    assert isinstance(payload.get("version"), str)
    assert payload["version"]


def test_version_detect_is_stable_string():
    # Whatever the checkout state, the detector always yields a non-empty
    # string (short HEAD here, "unknown" only without git/the repo).
    assert isinstance(app._HUB_VERSION, str)
    assert app._HUB_VERSION
    assert app._detect_hub_version() == app._HUB_VERSION


# ---------------------------------------------------------------- puter flag

def test_puter_is_recommended():
    puter = providers.PROVIDERS.get("puter")
    assert puter is not None
    assert puter.get("recommended") is True


# ---------------------------------------------------------------- frontend markers

def test_template_has_seen_version_key():
    text = _template_text()
    assert "flh.seenVersion" in text
    # Never stack with the first-run welcome modal: the popup is skipped
    # until the welcome flag exists.
    assert "cx_welcome_seen_v1" in text
    assert "checkWhatsNew" in text


def test_template_has_recommended_zone():
    text = _template_text()
    # Pinned zone marker (CSS class + section title) and no duplication:
    # recommended cards are excluded from the category groups via `unpinned`.
    assert "prov-cat.rec" in text
    assert "catSection('rec', 'Recommended'" in text
    assert "var unpinned = shown.filter(function(p){ return !p.recommended; });" in text
    # Puter one-liner on its pinned card.
    assert "Latest GPT-5.6 + 500 models with one free token" in text
