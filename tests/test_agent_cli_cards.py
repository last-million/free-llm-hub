"""Choosing a CLI: cards that save themselves, and a start panel you can read.

  * "dropdown cli's should be replaced with cards" -- a <select> shows the
    state of exactly one entry, the selected one, and every fact here matters
    BEFORE choosing: installed, isolated or your own copy, recommended.
  * "system should auto save by clicking the choice of the user" -- there was
    no Save button and no memory; every reload went back to the built-in
    default.
  * "as default should have the choice of new project not use existing folder".
  * "buttons session and history should look real buttons".
  * "remove image-providers from the sidebar menu".
  * "name this version llm calvoun V2.8".
"""
import os
import re

import pytest

import agentic_chat
import app
import config


HTML = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "templates", "index.html"), encoding="utf-8").read()


@pytest.fixture
def client():
    app.app.config["TESTING"] = True
    with app.app.test_client() as c:
        yield c


def _dash(client, method, url, **kw):
    headers = {"X-Free-LLM-Hub": "dashboard",
               "X-Free-LLM-Hub-Token": config.ensure_control_token()}
    return getattr(client, method)(url, headers=headers, **kw)


# --------------------------------------------------------------------------- #
# Cards instead of a dropdown
# --------------------------------------------------------------------------- #

def test_the_cli_dropdown_is_gone():
    assert '<select id="agent-cli"' not in HTML
    assert 'id="agent-cli-cards"' in HTML


def test_the_cards_do_not_reuse_the_connect_panels_class():
    """`.cli-card` already belonged to the Connect panel -- 7 of them. Reusing
    it restyled all of those and made the picker's own JS count 10 cards."""
    assert ".agent-cli-card{" in HTML
    picker = HTML[HTML.index("function renderCliPicker()"):]
    picker = picker[:picker.index("function chooseCli")]
    assert "'cli-card'" not in picker


def test_every_card_shows_the_state_a_dropdown_could_not():
    picker = HTML[HTML.index("function renderCliPicker()"):]
    picker = picker[:picker.index("function chooseCli")]
    for fact in ("recommended", "not installed", "isolated copy", "your install"):
        assert fact in picker, fact


# --------------------------------------------------------------------------- #
# The choice saves itself
# --------------------------------------------------------------------------- #

def test_the_choice_is_saved_on_the_click(client):
    original = agentic_chat.default_cli()
    try:
        r = _dash(client, "post", "/api/agent/settings", json={"default_cli": "opencode"})
        assert r.status_code == 200
        assert r.get_json()["default_cli"] == "opencode"
        assert agentic_chat.default_cli() == "opencode", "did not survive the call"
    finally:
        agentic_chat.set_default_cli(original)


def test_the_saved_choice_is_what_the_page_preselects(client):
    original = agentic_chat.default_cli()
    try:
        agentic_chat.set_default_cli("claude")
        assert _dash(client, "get", "/api/agent/settings").get_json()["default_cli"] == "claude"
    finally:
        agentic_chat.set_default_cli(original)


def test_a_cli_this_build_cannot_drive_is_refused(client):
    original = agentic_chat.default_cli()
    try:
        assert _dash(client, "post", "/api/agent/settings",
                     json={"default_cli": "notacli"}).status_code == 400
        assert agentic_chat.default_cli() == original, "a bad value was stored anyway"
    finally:
        agentic_chat.set_default_cli(original)


def test_a_stored_choice_that_stops_being_drivable_is_ignored(monkeypatch):
    """A CLI can be dropped between versions. Obeying a stale name would start
    every session with an error instead of falling back."""
    monkeypatch.setattr(agentic_chat.config, "get_value", lambda k, d=None: "removed-cli")
    assert agentic_chat.default_cli() in agentic_chat._SUPPORT


def test_turning_the_master_switch_still_works_on_its_own(client):
    """The route grew a second field; the original one must not have become
    mandatory."""
    was = agentic_chat.master_enabled()
    try:
        assert _dash(client, "post", "/api/agent/settings",
                     json={"enabled": True}).status_code == 200
    finally:
        agentic_chat.set_master_enabled(was)


def test_an_empty_body_is_still_a_mistake(client):
    assert _dash(client, "post", "/api/agent/settings", json={}).status_code == 400


# --------------------------------------------------------------------------- #
# The rest of the start panel
# --------------------------------------------------------------------------- #

def test_create_new_project_is_the_default_choice():
    """Starting a fresh project is the common case; defaulting to a folder path
    the user has to go and find made the harder of the two lead."""
    mode = HTML[HTML.index('id="agent-dir-mode"'):]
    mode = mode[:mode.index("</div>")]
    first = re.search(r'id="agent-mode-(\w+)"', mode).group(1)
    assert first == "new", "the existing-folder button still comes first"
    assert 'id="agent-mode-new" role="tab" aria-selected="true"' in mode
    assert "var dirMode = 'new';" in HTML, "the code still starts in existing-folder mode"


def test_session_and_history_look_like_buttons():
    """The generic pill is background:none/border:0 over a faint colour, which
    reads as a caption, not a control."""
    assert "#agent-view-toggle .seg-btn{" in HTML
    block = HTML[HTML.index("#agent-view-toggle .seg-btn{"):]
    block = block[:block.index("#agent-dir-mode{")]
    assert "border:1px solid var(--border)" in block
    assert "background:var(--surface-2)" in block


def test_the_setup_column_is_centred():
    assert ".agent-setup{" in HTML
    block = HTML[HTML.index(".agent-setup{"):]
    block = block[:block.index("}")]
    assert "margin:0 auto" in block
    assert "max-width" in block


def test_the_cards_are_a_responsive_grid():
    block = HTML[HTML.index(".agent-cli-cards{"):]
    block = block[:block.index("}")]
    assert "grid-template-columns:repeat(auto-fit" in block, "fixed columns are not responsive"


def test_image_providers_is_not_in_the_sidebar_but_its_page_still_works(client):
    assert 'data-view="sec-image-providers"' not in HTML, "still in the menu"
    assert 'id="sec-image-providers"' in HTML, "the page itself should not be deleted"
    assert client.get("/image-providers").status_code == 200, "existing links must not break"


# --------------------------------------------------------------------------- #
# The release name
# --------------------------------------------------------------------------- #

def test_the_release_is_named(client):
    assert app.HUB_RELEASE == "LLM Calvoun V2.8"
    assert "LLM Calvoun V2.8" in HTML, "the dashboard does not show which version this is"
    assert _dash(client, "get", "/api/version").get_json()["release"] == "LLM Calvoun V2.8"


def test_the_git_stamp_is_still_reported_separately(client):
    """The what's-new popup compares commits; a hand-set release name changes
    only when someone decides it does."""
    body = _dash(client, "get", "/api/version").get_json()
    assert "version" in body and body["version"] != body["release"]
