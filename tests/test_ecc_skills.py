"""ECC skills: opt-in coding-agent rules vendored from github.com/affaan-m/ECC.

Covers the legal vendoring (LICENSE + VENDORED.md, markdown only, no
executables), ecc.py (catalog, default-off, frontmatter-strip, bounded render,
enabled+matched gating), craft.match wiring, the /api/skills routes (list,
toggle one, toggle all), injection into the Build brief (craft.system_message,
used verbatim by agentic_chat) and into every /v1 protocol (the shared
app._apply_craft_brief injector that chat/messages/responses all run through),
and the Settings markup. No network; config is isolated per the project's
tempdir convention (this machine's pytest basetemp is permission-denied).
"""
import os
import re
import shutil
import tempfile

import pytest

import app
import config
import craft
import ecc
import skills

H = {"X-Free-LLM-Hub": "dashboard"}
ECC_DIR = os.path.join(os.path.dirname(app.__file__), "skills_ecc")

EXPECTED_IDS = (
    "ecc:tdd-workflow", "ecc:verification-loop", "ecc:security-review",
    "ecc:coding-standards", "ecc:agent-introspection-debugging",
    "ecc:backend-patterns", "ecc:frontend-patterns", "ecc:api-design",
    "ecc:e2e-testing",
)


@pytest.fixture
def isolated_config(monkeypatch):
    root = tempfile.mkdtemp(prefix="hub-pytest-")
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", os.path.join(root, "state", "config.json"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def app_source():
    """Point craft at the real app source (reads config), restore afterwards."""
    craft.set_skill_source(app._skill_source)
    try:
        yield
    finally:
        craft.set_skill_source(app._skill_source)


# ------------------------------------------------------------ legal vendoring

def test_license_present_and_mit():
    text = open(os.path.join(ECC_DIR, "LICENSE"), encoding="utf-8").read()
    assert "MIT License" in text
    assert "Affaan Mustafa" in text
    assert "Permission is hereby granted" in text


def test_vendored_md_records_provenance():
    doc = open(os.path.join(ECC_DIR, "VENDORED.md"), encoding="utf-8").read()
    assert "github.com/affaan-m/ECC" in doc
    assert "ef648e01899ba3e8dc6371642deaaf64b4477775" in doc        # commit
    assert "MIT" in doc and "Affaan Mustafa" in doc
    # The one modification and the deliberate exclusions must be written down.
    assert "setup-package-manager" in doc                           # the trim
    assert "openai.yaml" in doc                                     # excluded
    assert "strategic-compact" in doc                               # excluded + why


def test_every_vendored_file_is_markdown_no_executable():
    assert os.path.isdir(ECC_DIR)
    bad = (".py", ".js", ".ts", ".sh", ".bat", ".ps1", ".exe", ".yaml",
           ".yml", ".json", ".toml")
    for fn in os.listdir(ECC_DIR):
        full = os.path.join(ECC_DIR, fn)
        if not os.path.isfile(full):
            continue
        assert fn == "LICENSE" or fn.endswith(".md"), fn
        assert not fn.lower().endswith(bad), fn
    # Every catalogued skill has its markdown file, and it is non-empty text.
    for _id, filename, _n, _d, _k in ecc.CATALOG:
        assert os.path.isfile(os.path.join(ECC_DIR, filename)), filename
        assert ecc.load_body(filename).strip(), filename


def test_trimmed_tdd_has_no_unvendored_script():
    body = open(os.path.join(ECC_DIR, "tdd-workflow.md"), encoding="utf-8").read()
    assert "node scripts/setup-package-manager.js" not in body
    assert "Detect the package manager" in body                     # the replacement


# ------------------------------------------------------------ ecc.py catalog

def test_catalog_ids_and_shape():
    assert ecc.IDS == EXPECTED_IDS
    assert all(i.startswith("ecc:") for i in ecc.IDS)
    for _id, _f, name, desc, kw in ecc.CATALOG:
        assert name and desc and kw
        assert len(set(k.lower() for k in kw)) == len(kw)           # no dup keyword


def test_default_is_off():
    # Nothing enabled in a fresh catalog view, and the stored default is empty.
    assert all(not row["enabled"] for row in ecc.view([]))
    assert ecc.hits("set up tdd with test coverage", []) == []


def test_render_strips_frontmatter_and_is_bounded():
    for sid in ecc.IDS:
        block = ecc.render(sid)
        assert block.startswith("ECC SKILL: ")
        assert "Affaan Mustafa" in block.splitlines()[0]            # attribution
        assert "\nname:" not in block and "license: MIT" not in block
        assert len(block) <= ecc.MAX_CHARS


def test_hits_gate_on_enabled_and_matched():
    one = ["ecc:tdd-workflow"]
    # enabled + matched
    hits = ecc.hits("please set up tdd for this module", one)
    assert [h[0] for h in hits] == ["ecc:tdd-workflow"]
    # matched but NOT enabled -> nothing
    assert ecc.hits("please set up tdd for this module", []) == []
    # enabled but NOT matched -> nothing
    assert ecc.hits("write me a poem about the sea", one) == []
    # keywords are whole words, not substrings/regex
    assert ecc.hits("contested the point", one) == []               # not "tdd"
    assert ecc.matches("ecc:security-review", "needs a security review")
    assert not ecc.matches("ecc:security-review", "insecurely")


def test_hits_capped_at_two_per_turn():
    text = ("tdd test coverage, a security review of authentication and secrets, "
            "e2e playwright tests, backend express patterns, api design with pagination")
    hits = ecc.hits(text, list(ecc.IDS))
    assert len(hits) == ecc.MAX_PER_TURN == 2


# ------------------------------------------------------------ craft wiring

def test_two_tuple_source_means_no_ecc(app_source):
    # The older 2-tuple source shape still works and enables no ECC skill.
    craft.set_skill_source(lambda: ([], []))
    try:
        assert not any(n.startswith("ecc:") for n in craft.names("set up tdd"))
    finally:
        craft.set_skill_source(app._skill_source)


def test_craft_match_injects_enabled_ecc():
    craft.set_skill_source(lambda: ([], [], ["ecc:tdd-workflow"]))
    try:
        assert "ecc:tdd-workflow" in craft.names("set up tdd with good coverage")
        msg = craft.system_message("set up tdd with good coverage", tools=True)["content"]
        assert "ECC SKILL: TDD workflow (ECC)" in msg
        # disabled ECC skill never rides along
        assert "ecc:security-review" not in craft.names(
            "set up tdd and a security review")
    finally:
        craft.set_skill_source(app._skill_source)


def test_ecc_only_tool_less_gets_no_verify_read():
    # An ECC skill carries no ANTI lines, so (like a user skill) it must not pull
    # the tool-less VERIFY_READ block that references "the ANTI lines above".
    craft.set_skill_source(lambda: ([], [], ["ecc:verification-loop"]))
    try:
        msg = craft.system_message("verify the work before claiming done",
                                   tools=False)["content"]
        assert "ECC SKILL: Verification loop (ECC)" in msg
        assert craft.VERIFY_READ not in msg
    finally:
        craft.set_skill_source(app._skill_source)


def test_worst_case_with_all_ecc_enabled_stays_under_ceiling():
    """Enabling every ECC skill must not break the existing brief ceiling
    (test_craft_briefs.test_worst_case_brief_cost: < 13.5% of the 32K floor)."""
    craft.set_skill_source(lambda: ([], [], list(ecc.IDS)))
    try:
        worst = max(len(craft.system_message(t, tools=True)["content"]) for t in (
            "build an online store and deploy it",
            "create a landing page for my saas",
            "build me a restaurant website",
            "build and deploy a secure rest api with tdd, a security review, "
            "e2e playwright tests, backend and frontend react patterns, api design",
            "set up tdd with test coverage and run a security review for auth"))
        assert worst / 4 < 32768 * 0.135, "ECC pushed briefs to ~%d tokens" % (worst // 4)
    finally:
        craft.set_skill_source(app._skill_source)


# ------------------------------------------------------------ /api/skills

def test_api_lists_ecc_all_off(isolated_config):
    v = app.app.test_client().get("/api/skills").get_json()
    assert [row["id"] for row in v["ecc"]] == list(EXPECTED_IDS)
    assert all(not row["enabled"] for row in v["ecc"])
    assert v["ecc_all"] is False
    assert "Affaan Mustafa" in v["ecc_credit"]
    # ECC ids are distinct from the built-in ids.
    assert not (set(EXPECTED_IDS) & set(skills.BUILTIN_IDS))


def test_api_toggle_one_and_all(isolated_config, app_source):
    c = app.app.test_client()
    v = c.post("/api/skills/toggle", json={"id": "ecc:tdd-workflow", "enabled": True},
               headers=H).get_json()
    assert {r["id"]: r["enabled"] for r in v["ecc"]}["ecc:tdd-workflow"] is True
    assert v["ecc_all"] is False
    assert "ecc:tdd-workflow" in craft.names("set up tdd coverage")

    v = c.post("/api/skills/toggle", json={"id": "ecc_all", "enabled": True},
               headers=H).get_json()
    assert all(r["enabled"] for r in v["ecc"]) and v["ecc_all"] is True

    v = c.post("/api/skills/toggle", json={"id": "ecc_all", "enabled": False},
               headers=H).get_json()
    assert all(not r["enabled"] for r in v["ecc"]) and v["ecc_all"] is False
    assert not any(n.startswith("ecc:") for n in craft.names("set up tdd coverage"))


# ------------------------------------------------------------ injection paths

def test_appears_in_build_brief(isolated_config, app_source):
    # agentic_chat.py builds the Build-page brief from craft.system_message().
    config.set_setting("ecc_enabled", ["ecc:e2e-testing"])
    msg = craft.system_message("write playwright e2e tests with a page object",
                               tools=True)
    assert "ECC SKILL: E2E testing (ECC)" in msg["content"]


def test_injected_into_v1_via_apply_craft_brief(isolated_config, app_source):
    # chat/messages/responses all dispatch through _apply_craft_brief -> _upstream_chat.
    config.set_setting("ecc_enabled", ["ecc:security-review"])
    msgs = [{"role": "user",
             "content": "add authentication and a security review for secrets"}]
    out = app._apply_craft_brief(list(msgs), agentic=True)
    joined = "\n".join(m.get("content", "") for m in out if m.get("role") == "system")
    assert "ECC SKILL: Security review (ECC)" in joined
    # Off by default: nothing enabled -> no ECC block.
    config.set_setting("ecc_enabled", [])
    out2 = app._apply_craft_brief(list(msgs), agentic=True)
    joined2 = "\n".join(m.get("content", "") for m in out2 if m.get("role") == "system")
    assert "ECC SKILL:" not in joined2


# ------------------------------------------------------------ Settings markup

def test_settings_markup():
    src = open(os.path.join(os.path.dirname(app.__file__), "templates", "index.html"),
               encoding="utf-8").read()
    for needle in ('id="skills-ecc"', 'data-skill="ecc_all"', "r.ecc", "ecc_credit",
                   "Affaan Mustafa"):
        assert needle in src, needle
    # The ECC group is collapsed by default: the <details> enclosing the list
    # has no `open` attribute.
    before = src[:src.index('id="skills-ecc"')]
    tag = src[before.rindex("<details"):].split(">", 1)[0]
    assert "open" not in tag, tag
