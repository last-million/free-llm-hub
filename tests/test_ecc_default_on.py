"""ECC skills are ON by default; a user can switch each one, or all of them, off.

Owner, 2026-11-20: "skills should be always enabled by default in settings and user can
disable them of course". Until the user saves a choice every ECC skill is on; a saved list
(an empty one included) is followed exactly, so nothing a user switched off ever comes back.
"""
import os
import shutil
import tempfile

import pytest

import app
import config
import craft
import ecc
import skills

H = {"X-Free-LLM-Hub": "dashboard"}


@pytest.fixture
def fresh(monkeypatch):
    root = tempfile.mkdtemp(prefix="hub-pytest-")
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", os.path.join(root, "state", "config.json"))
    craft.set_skill_source(app._skill_source)
    try:
        yield
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _on(c):
    return {r["id"]: r["enabled"] for r in c.get("/api/skills").get_json()["ecc"]}


def test_a_fresh_install_has_every_ecc_skill_on(fresh):
    assert config.get_setting("ecc_enabled", None) is None          # nothing saved
    assert app._ecc_enabled() == list(ecc.IDS)
    c = app.app.test_client()
    v = c.get("/api/skills").get_json()
    assert all(r["enabled"] for r in v["ecc"]) and v["ecc_all"] is True
    assert "ecc:tdd-workflow" in craft.names("set up tdd with test coverage")


def test_every_built_in_skill_is_on_by_default_too(fresh):
    v = app.app.test_client().get("/api/skills").get_json()
    assert v["enabled"] is True                                      # the master switch
    assert [b["id"] for b in v["builtin"]] == [b[0] for b in skills.BUILTIN]
    assert all(b["enabled"] for b in v["builtin"])


def test_switching_one_off_leaves_the_other_eight_on(fresh):
    c = app.app.test_client()
    c.post("/api/skills/toggle", json={"id": "ecc:tdd-workflow", "enabled": False}, headers=H)
    on = _on(c)
    assert on["ecc:tdd-workflow"] is False
    assert sum(on.values()) == len(ecc.IDS) - 1
    assert "ecc:tdd-workflow" not in craft.names("set up tdd with test coverage")
    # ...and it stays off after the next toggle of another skill.
    c.post("/api/skills/toggle", json={"id": "ecc:api-design", "enabled": False}, headers=H)
    on = _on(c)
    assert on["ecc:tdd-workflow"] is False and on["ecc:api-design"] is False
    assert sum(on.values()) == len(ecc.IDS) - 2


def test_switching_all_off_stays_off_and_on_brings_them_back(fresh):
    c = app.app.test_client()
    c.post("/api/skills/toggle", json={"id": "ecc_all", "enabled": False}, headers=H)
    assert config.get_setting("ecc_enabled", None) == []             # a saved choice
    assert app._ecc_enabled() == []
    assert not any(n.startswith("ecc:") for n in craft.names("set up tdd with test coverage"))
    v = c.post("/api/skills/toggle", json={"id": "ecc_all", "enabled": True}, headers=H).get_json()
    assert v["ecc_all"] is True and all(r["enabled"] for r in v["ecc"])


def test_a_choice_the_user_saved_is_followed_exactly(fresh):
    config.set_setting("ecc_enabled", ["ecc:e2e-testing"])
    assert app._ecc_enabled() == ["ecc:e2e-testing"]
    config.set_setting("ecc_enabled", ["ecc:e2e-testing", "ecc:not-a-skill"])
    assert app._ecc_enabled() == ["ecc:e2e-testing"]                 # unknown ids are dropped


def test_a_stored_value_that_is_not_a_list_reads_as_never_saved(fresh):
    for junk in ("yes", 3, {"a": 1}, None):
        config.set_setting("ecc_enabled", junk)
        assert app._ecc_enabled() == list(ecc.IDS), junk


def test_the_settings_page_no_longer_calls_them_opt_in():
    src = open(os.path.join(os.path.dirname(app.__file__), "templates", "index.html"),
               encoding="utf-8").read()
    assert "ECC skills (opt-in)" not in src
    assert "Off by default: switch on the ones you want" not in src
    assert "On by default" in src
    assert "opt-in" not in ecc.CREDIT and "Affaan Mustafa" in ecc.CREDIT
