"""The Calvoun Free LLM Hub harness switch (Settings, default on, recommended).

Off makes every flag in config.HARNESS_FLAGS read False through config.get_flag;
the features that keep requests working (routing, fallback, answer checks, scope
guard) are not in the set. Hermetic: a temp config file per test.
"""
import os
import re

import pytest

import app
import config


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(tmp_path / "state" / "config.json"))
    return tmp_path


def test_the_harness_is_on_by_default_and_every_feature_keeps_its_own_default(cfg):
    assert config.get_flag(config.HARNESS_FLAG, True) is True
    for name in config.HARNESS_FLAGS:
        assert config.get_flag(name, True) is True
        assert config.get_flag(name, False) is False


def test_off_turns_every_harness_feature_off_and_on_brings_them_back(cfg):
    config.set_flag("model_guides", True)
    config.set_flag(config.HARNESS_FLAG, False)
    for name in config.HARNESS_FLAGS:
        assert config.get_flag(name, True) is False, name
    config.set_flag(config.HARNESS_FLAG, True)
    for name in config.HARNESS_FLAGS:
        assert config.get_flag(name, True) is True, name
    # a feature the owner switched off on its own stays off
    config.set_flag("model_guides", False)
    assert config.get_flag("model_guides", True) is False


def test_what_keeps_requests_working_is_not_part_of_the_harness(cfg):
    keep = ("scope_guard", "scope_card", "content_filter_handover", "max_reasoning",
            "old_tool_result_clearing", "conversation_memory", "relay_text_hold",
            "auto_block_no_answer", "provider_fairness", "image_rank_by_quality")
    assert not set(keep) & config.HARNESS_FLAGS
    config.set_flag(config.HARNESS_FLAG, False)
    for name in keep:
        assert config.get_flag(name, True) is True, name


def test_every_harness_flag_is_a_real_flag_the_hub_reads():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = "".join(open(os.path.join(here, f), encoding="utf-8").read()
                  for f in ("app.py", "craft.py", "memory.py"))
    for name in config.HARNESS_FLAGS:
        assert '"%s"' % name in src, name


def test_the_route_reads_and_writes_the_switch(cfg):
    client = app.app.test_client()
    assert client.get("/api/harness").get_json() == {"enabled": True}
    hdr = {"X-Free-LLM-Hub": "dashboard"}
    r = client.post("/api/harness", json={"enabled": False}, headers=hdr)
    assert r.status_code == 200 and r.get_json() == {"enabled": False}
    assert config.get_flag("craft_briefs", True) is False
    r = client.post("/api/harness", json={"enabled": True}, headers=hdr)
    assert r.get_json() == {"enabled": True}
    assert config.get_flag("craft_briefs", True) is True


def test_the_settings_page_shows_the_switch_recommended_and_on_top():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html = open(os.path.join(here, "templates", "index.html"), encoding="utf-8").read()
    grid = html.index('<div class="sd-grid">', html.index('id="sec-settings"'))
    first_group = html.index('<div class="sd-group"', grid)
    assert html.startswith('<div class="sd-group" id="harness-group">', first_group)
    assert 'id="drawer-harness-switch"' in html
    assert '<span class="lbl">Calvoun Free LLM Hub harness ' in html
    assert re.search(r"badge recommended\">Recommended: keep on<", html)
    assert "api('/api/harness'" in html
    assert "initHarness();" in html and "loadHarness(true)" in html
