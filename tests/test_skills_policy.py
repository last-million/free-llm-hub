"""Vendored agent skills + social web-search policy endpoint.

Covers:
  - GET/POST /api/web-search-policy reflects and toggles the persisted
    `social_web_search` flag (isolated config, no network).
  - .agents/skills/ vendors exist with valid frontmatter (name+description).
  - i-have-adhd keeps model auto-invocation ENABLED (always-on output style).
  - last30days SKILL.md documents the hub policy endpoint and the
    keyless-by-default rule.

NOTE: no pytest tmp_path here — this machine's basetemp is permission-denied;
tempfile.mkdtemp(prefix="hub-pytest-") works.
"""

import os
import re
import shutil
import tempfile

import pytest

import app
import config

REPO_ROOT = os.path.dirname(os.path.abspath(app.__file__))
SKILLS_DIR = os.path.join(REPO_ROOT, ".agents", "skills")


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


def _frontmatter(path):
    """Return the raw YAML frontmatter block of a SKILL.md (no yaml dep)."""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, "%s has no frontmatter block" % path
    return m.group(1)


def _fm_value(fm, key):
    m = re.search(r"^%s:\s*(.+)$" % re.escape(key), fm, re.MULTILINE)
    return m.group(1).strip().strip("'\"") if m else None


# ---------------------------------------------------------------- endpoint

def test_web_search_policy_defaults_off(isolated_config):
    assert config.get_social_web_search() is False
    r = _client().get("/api/web-search-policy")
    assert r.status_code == 200
    assert r.get_json() == {"social_search": False}


def test_web_search_policy_toggle_roundtrip(isolated_config):
    client = _client()
    r = client.post("/api/web-search-policy", json={"social_search": True},
                    headers={"X-Free-LLM-Hub": "dashboard"})
    assert r.status_code == 200
    assert r.get_json() == {"social_search": True}
    assert config.get_social_web_search() is True
    assert client.get("/api/web-search-policy").get_json() == {"social_search": True}
    # Persisted across reloads (same config file)
    assert config.get_flag("social_web_search", False) is True

    r = client.post("/api/web-search-policy", json={"social_search": False},
                    headers={"X-Free-LLM-Hub": "dashboard"})
    assert r.get_json() == {"social_search": False}
    assert config.get_social_web_search() is False


def test_web_search_policy_rejects_non_boolean(isolated_config):
    client = _client()
    for bad in ({"social_search": "yes"}, {"social_search": 1}, {}, None):
        r = client.post("/api/web-search-policy", json=bad,
                        headers={"X-Free-LLM-Hub": "dashboard"})
        assert r.status_code == 400, bad
    assert config.get_social_web_search() is False


def test_web_search_policy_post_requires_control_header(isolated_config, monkeypatch):
    monkeypatch.setattr(config, "get_control_token", lambda: "secret-token")
    client = _client()
    # GET is control-token exempt: a single non-sensitive boolean that
    # token-less local agents (last30days skill) must be able to read.
    r = client.get("/api/web-search-policy")
    assert r.status_code == 200
    assert r.get_json() == {"social_search": False}
    # POST keeps full protection: dashboard header AND control token.
    r = client.post("/api/web-search-policy", json={"social_search": True})
    assert r.status_code == 403
    r = client.post("/api/web-search-policy", json={"social_search": True},
                    headers={"X-Free-LLM-Hub": "dashboard"})
    assert r.status_code == 401
    r = client.post("/api/web-search-policy", json={"social_search": True},
                    headers={"X-Free-LLM-Hub": "dashboard",
                             "X-Free-LLM-Hub-Token": "secret-token"})
    assert r.status_code == 200
    assert r.get_json() == {"social_search": True}


# ---------------------------------------------------------------- vendored skills

def test_vendored_skills_exist_with_valid_frontmatter():
    for name in ("i-have-adhd", "last30days"):
        path = os.path.join(SKILLS_DIR, name, "SKILL.md")
        assert os.path.isfile(path), path
        fm = _frontmatter(path)
        assert _fm_value(fm, "name") == name
        assert _fm_value(fm, "description")


def test_i_have_adhd_auto_invocation_not_disabled():
    fm = _frontmatter(os.path.join(SKILLS_DIR, "i-have-adhd", "SKILL.md"))
    assert "disable-model-invocation" not in fm
    assert "disableModelInvocation" not in fm
    # Attribution kept
    assert os.path.isfile(os.path.join(SKILLS_DIR, "i-have-adhd", "LICENSE"))


def test_last30days_keyless_gating_documented():
    skill_dir = os.path.join(SKILLS_DIR, "last30days")
    with open(os.path.join(skill_dir, "SKILL.md"), encoding="utf-8") as f:
        body = f.read()
    assert "/api/web-search-policy" in body
    assert "social_search" in body
    assert "KEYLESS" in body.upper()
    assert "${KIMI_SKILL_DIR}" in body
    # Engine + attribution installed alongside
    assert os.path.isfile(os.path.join(skill_dir, "scripts", "last30days.py"))
    assert os.path.isfile(os.path.join(skill_dir, "LICENSE"))
    assert os.path.isfile(os.path.join(skill_dir, "VENDORED.md"))
