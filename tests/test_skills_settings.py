"""Settings -> Skills: switch built-in briefs off, add named user skills.

Covers skills.py validation/matching, craft.match honouring the registered
source, the /api/skills routes (isolated config, no network), the last30days
policy gate and the Settings markup.

NOTE: no pytest tmp_path here -- this machine's basetemp is permission-denied.
"""
import os
import shutil
import tempfile

import pytest

import app
import config
import craft
import crews
import skills

H = {"X-Free-LLM-Hub": "dashboard"}


@pytest.fixture
def isolated_config(monkeypatch):
    root = tempfile.mkdtemp(prefix="hub-pytest-")
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", os.path.join(root, "state", "config.json"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def source():
    """Point craft at a plain in-memory source; restore app's afterwards."""
    state = {"disabled": [], "custom": []}
    craft.set_skill_source(lambda: (state["disabled"], state["custom"]))
    try:
        yield state
    finally:
        craft.set_skill_source(app._skill_source)


def _skill(**kw):
    body = {"name": "Brand voice", "trigger": "keywords", "keywords": "newsletter, blog post",
            "instructions": "Warm, direct tone."}
    body.update(kw)
    return skills.validate(body, [])


# ---------------------------------------------------------------- skills.py

def test_validate_cleans_and_rejects():
    s = _skill(keywords=" newsletter ,, Newsletter, blog   post ")
    assert s["keywords"] == ["newsletter", "blog post"] and s["enabled"] is True
    assert len(s["id"]) == 12
    for bad, msg in [({"name": ""}, "name"), ({"instructions": "  "}, "instructions"),
                     ({"trigger": "sometimes"}, "trigger"), ({"keywords": ""}, "keyword"),
                     ({"name": "x" * 61}, "longer"), ({"instructions": "x" * 4001}, "longer"),
                     ({"enabled": "yes"}, "enabled")]:
        with pytest.raises(skills.SkillError, match=msg):
            _skill(**bad)
    assert _skill(trigger="always", keywords="")["keywords"] == []


def test_validate_update_keeps_id_and_blocks_duplicate_names():
    a = _skill()
    b = skills.validate({"id": a["id"], "name": "Brand voice", "trigger": "always",
                         "instructions": "v2"}, [a])
    assert b["id"] == a["id"]
    with pytest.raises(skills.SkillError, match="already exists"):
        skills.validate({"name": "brand VOICE", "trigger": "always", "instructions": "x"}, [a])
    # An unknown id is a NEW skill, not an update of someone else's.
    c = skills.validate({"id": "nope", "name": "Other", "trigger": "always",
                         "instructions": "x"}, [a])
    assert c["id"] not in ("nope", a["id"])


def test_limit_on_saved_skills():
    saved = [dict(_skill(name="s%d" % i)) for i in range(skills.MAX_CUSTOM)]
    with pytest.raises(skills.SkillError, match="at most"):
        skills.validate({"name": "one more", "trigger": "always", "instructions": "x"}, saved)


def test_keywords_are_words_not_regex():
    s = _skill(keywords="c++, .*")
    assert skills.matches(s, "port this to C++ please")
    assert not skills.matches(s, "anything at all")          # ".*" is literal
    assert not skills.matches(_skill(keywords="blog"), "the blogger said")  # word-bounded
    assert not skills.matches(dict(_skill(), enabled=False), "a newsletter")
    assert skills.matches(_skill(trigger="always", keywords=""), "hi")


# ---------------------------------------------------------------- craft

def test_unregistered_source_is_the_old_behaviour():
    craft.set_skill_source(None)
    try:
        assert "web_design" in craft.names("build me a website")
    finally:
        craft.set_skill_source(app._skill_source)


def test_disabled_builtin_is_not_injected(source):
    assert "web_design" in craft.names("redesign my website")
    source["disabled"] = ["web_design", "seo"]
    names = craft.names("redesign my website")
    assert "web_design" not in names and "seo" not in names
    assert craft.skill_enabled("images") and not craft.skill_enabled("seo")


def test_custom_skill_rides_along_and_is_capped(source):
    source["custom"] = [_skill(name="k%d" % i, keywords="newsletter") for i in range(5)]
    hits = [n for n in craft.names("write my newsletter") if n.startswith("custom:")]
    assert len(hits) == skills.MAX_CUSTOM_PER_TURN
    msg = craft.system_message("write my newsletter", tools=True)["content"]
    assert "USER SKILL: k0" in msg and "Warm, direct tone." in msg


def test_custom_only_tool_less_gets_no_anti_verify(source):
    source["custom"] = [_skill(trigger="always", keywords="")]
    msg = craft.system_message("hello there", tools=False)["content"]
    assert "USER SKILL: Brand voice" in msg
    assert craft.VERIFY_READ not in msg


def test_design_crew_drops_web_design_when_switched_off(source, monkeypatch):
    seen = {}

    def fake_run(messages, dispatch, profile=None, **kw):
        seen["profile"] = profile
        return {"answer": "x"}
    monkeypatch.setattr(crews.swarm, "run", fake_run)
    crews.run([{"role": "user", "content": "a landing page"}], None, "crew-design")
    assert seen["profile"]["worker_extra"] == craft.WEB_DESIGN
    source["disabled"] = ["web_design"]
    crews.run([{"role": "user", "content": "a landing page"}], None, "crew-design")
    assert seen["profile"]["worker_extra"] == ""
    assert any(p.get("worker_extra") == craft.WEB_DESIGN for p in crews.CREWS.values())  # not mutated


# ---------------------------------------------------------------- API

def test_api_lists_toggles_and_crud(isolated_config):
    c = app.app.test_client()
    v = c.get("/api/skills").get_json()
    assert v["enabled"] is True and v["custom"] == []
    assert [b["id"] for b in v["builtin"]] == list(skills.BUILTIN_IDS)
    assert all(b["enabled"] for b in v["builtin"])

    v = c.post("/api/skills/toggle", json={"id": "seo", "enabled": False}, headers=H).get_json()
    assert {b["id"]: b["enabled"] for b in v["builtin"]}["seo"] is False
    assert "seo" not in craft.names("improve my website")
    c.post("/api/skills/toggle", json={"id": "seo", "enabled": True}, headers=H)
    assert "seo" in craft.names("improve my website")

    r = c.post("/api/skills/custom", headers=H, json={
        "name": "Brand voice", "trigger": "keywords", "keywords": "newsletter",
        "instructions": "Warm, direct tone."})
    assert r.status_code == 200
    sid = r.get_json()["saved"]
    assert any(n == "custom:" + sid for n in craft.names("draft the newsletter"))

    r = c.post("/api/skills/custom", headers=H, json={"name": "", "instructions": "x"})
    assert r.status_code == 400 and "name" in r.get_json()["error"]

    v = c.post("/api/skills/toggle", json={"id": sid, "enabled": False}, headers=H).get_json()
    assert v["custom"][0]["enabled"] is False
    assert not any(n.startswith("custom:") for n in craft.names("draft the newsletter"))

    r = c.post("/api/skills/custom", headers=H, json={
        "id": sid, "name": "Brand voice 2", "trigger": "always", "instructions": "v2",
        "enabled": True})
    assert r.get_json()["custom"][0]["name"] == "Brand voice 2"
    assert len(r.get_json()["custom"]) == 1

    assert c.post("/api/skills/toggle", json={"id": "all", "enabled": False},
                  headers=H).get_json()["enabled"] is False
    assert config.get_flag("craft_briefs", True) is False

    assert c.post("/api/skills/custom/delete", json={"id": sid}, headers=H).get_json()["custom"] == []
    assert c.post("/api/skills/custom/delete", json={"id": sid}, headers=H).status_code == 404
    assert c.post("/api/skills/toggle", json={"id": "zzz", "enabled": True}, headers=H).status_code == 404
    assert c.post("/api/skills/toggle", json={"id": "seo"}, headers=H).status_code == 400


def test_api_is_control_gated(isolated_config, monkeypatch):
    monkeypatch.setattr(config, "get_control_token", lambda: "secret-token")
    c = app.app.test_client()
    assert c.get("/api/skills").status_code == 401
    assert c.post("/api/skills/toggle", json={"id": "seo", "enabled": False}).status_code == 403


def test_last30days_switch_reaches_the_policy_endpoint(isolated_config):
    c = app.app.test_client()
    assert c.get("/api/web-search-policy").get_json() == {"social_search": False}
    config.set_social_web_search(True)
    c.post("/api/skills/toggle", json={"id": "last30days", "enabled": False}, headers=H)
    assert c.get("/api/web-search-policy").get_json() == {
        "social_search": False, "skill_enabled": False}
    md = open(os.path.join(os.path.dirname(app.__file__), ".agents", "skills",
                           "last30days", "SKILL.md"), encoding="utf-8").read()
    assert '"skill_enabled": false' in md


def test_settings_markup_and_escaping():
    src = open(os.path.join(os.path.dirname(app.__file__), "templates", "index.html"),
               encoding="utf-8").read()
    for needle in ('id="skills-group"', 'id="skills-master-switch"', 'id="skills-form"',
                   'id="sk-name"', 'id="sk-trigger"', 'id="sk-keywords"', 'id="sk-text"',
                   "/api/skills/toggle", "/api/skills/custom/delete", "initSkills();",
                   "loadSkills(silent)"):
        assert needle in src, needle
    js = src[src.index("function renderSkills"):src.index("function loadSkills")]
    # Every user-controlled value is escaped before it lands in innerHTML.
    for field in ("k.name", "k.description", "k.id", "when"):
        assert "esc(%s)" % field in js, field
