"""A provider the user added can be ranked by hand, and marked chat-only.

REQUESTED 2026-11-21: "when I add a provider, in its card and in Settings, let me rank it among the
benchmark models by hand, like putting it at the top -- and make my local Gemini bridge the most
intelligent." The bridge has no benchmark row and no family the hub knows, so it scored as "unknown" (10).

Rank (auto | top | high | medium | low) replaces the score of every model of that provider. `no_tools`
marks a chat-only endpoint: it is never offered a tool turn, so a top rank cannot send an agent's tool
call to a provider that refuses it. Flag `manual_provider_rank` (default on) turns the effect off.
Hermetic: FREE_LLM_HUB_CONFIG points at a temp file per test; no network.
"""
import io
import json
import re
import tempfile
from pathlib import Path

import pytest

import app
import config

_DASH = {"X-Free-LLM-Hub": "dashboard"}
MODEL = "gemini-web"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    path = Path(tempfile.mkdtemp()) / "state" / "config.json"
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(path))
    with app._model_cache_lock:
        app._model_cache.clear()
        app._paid_model_cache.clear()
    app._custom_meta_reset()
    yield
    app._custom_meta_reset()


def _auth():
    return dict(_DASH, **{"X-Free-LLM-Hub-Token": config.ensure_control_token()})


def _create(**extra):
    body = {"name": "Gemini bridge", "base_url": "http://127.0.0.1:8765/v1"}
    body.update(extra)
    r = app.app.test_client().post("/api/custom-providers", json=body, headers=_auth())
    assert r.status_code == 201, r.get_data(as_text=True)
    return r.get_json()["id"]


def _put(pid, **body):
    return app.app.test_client().put("/api/custom-providers/" + pid, json=body, headers=_auth())


def _defn(pid):
    r = app.app.test_client().get("/api/custom-providers", headers=_auth())
    return next(p for p in r.get_json()["providers"] if p["id"] == pid)


# --------------------------------------------------------------------------- #
# The scores
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("rank,score", [("top", 200.0), ("high", 134.0), ("medium", 90.0), ("low", 40.0)])
def test_each_rank_sets_the_score_of_every_model_of_the_provider(rank, score):
    pid = _create(rank=rank)
    assert app._benchmark_score(pid, MODEL) == score
    assert app._benchmark_score(pid, "any-other-model") == score


def test_top_is_above_every_model_the_hub_knows():
    pid = _create(rank="top")
    top = app._benchmark_score(pid, MODEL)
    others = [app._benchmark_score("anthropic", "claude-opus-5.5"),
              app._benchmark_score("nvidia", "moonshotai/kimi-k3.1-pro"),
              app._benchmark_score("g4f", "claude-opus-5.5"),
              app._benchmark_score("nvidia", "z-ai/glm-5.3")]
    assert top > max(others) + 10, (top, others)


def test_automatic_is_the_default_and_changes_nothing():
    pid = _create()
    assert _defn(pid)["rank"] == "auto"
    assert app._manual_rank_score(pid) is None
    assert app._benchmark_score(pid, MODEL) == app._benchmark_score("", MODEL)


def test_an_edit_takes_effect_at_once_not_after_the_cache_expires():
    pid = _create(rank="top")
    assert app._benchmark_score(pid, MODEL) == 200.0
    assert _put(pid, rank="low").status_code == 200
    assert app._benchmark_score(pid, MODEL) == 40.0
    assert _put(pid, rank="auto").status_code == 200
    assert app._manual_rank_score(pid) is None


def test_another_provider_is_not_affected():
    before = app._benchmark_score("groq", "qwen/qwen3.8-27b")
    _create(rank="top")
    assert app._benchmark_score("groq", "qwen/qwen3.8-27b") == before
    assert before < 200.0


def test_a_built_in_provider_with_the_same_id_wins(monkeypatch):
    monkeypatch.setattr(config, "get_custom_providers", lambda: {"groq": {"rank": "top"}})
    app._custom_meta_reset()
    assert app._manual_rank_score("groq") is None


def test_the_flag_turns_the_effect_off(monkeypatch):
    pid = _create(rank="top", no_tools=True)
    real = config.get_flag
    monkeypatch.setattr(config, "get_flag",
                        lambda name, default=False: False if name == "manual_provider_rank" else real(name, default))
    app._custom_meta_reset()
    assert app._manual_rank_score(pid) is None
    assert app._supports_tools(pid, MODEL) is True


def test_scoring_never_raises_when_the_config_is_unreadable(monkeypatch):
    def boom():
        raise OSError("config unreadable")
    monkeypatch.setattr(config, "get_custom_providers", boom)
    app._custom_meta_reset()
    assert app._manual_rank_score("anything") is None
    assert app._custom_no_tools("anything") is False


# --------------------------------------------------------------------------- #
# Chat only
# --------------------------------------------------------------------------- #

def test_a_chat_only_provider_is_never_offered_a_tool_turn():
    pid = _create(rank="top", no_tools=True)
    assert app._supports_tools(pid, MODEL) is False
    assert _defn(pid)["no_tools"] is True


def test_by_default_a_provider_is_assumed_tool_capable():
    pid = _create()
    assert app._supports_tools(pid, MODEL) is True
    assert _defn(pid)["no_tools"] is False


def test_the_mark_can_be_removed():
    pid = _create(no_tools=True)
    assert _put(pid, no_tools=False).status_code == 200
    assert app._supports_tools(pid, MODEL) is True


def test_a_chat_only_provider_is_dropped_from_a_tool_request():
    """The router's tool-capability filter (_supports_tools) is what keeps a top-ranked bridge
    away from an agent's tool call."""
    pid = _create(rank="top", no_tools=True)
    cands = [(pid, MODEL), ("groq", "qwen/qwen3.8-27b")]
    kept = [c for c in cands if app._supports_tools(*c)]
    assert kept == [("groq", "qwen/qwen3.8-27b")]


# --------------------------------------------------------------------------- #
# The API
# --------------------------------------------------------------------------- #

def test_the_definition_and_the_provider_card_carry_the_rank():
    pid = _create(rank="top", no_tools=True)
    d = _defn(pid)
    assert d["rank"] == "top" and d["no_tools"] is True
    row = next(p for p in app.app.test_client().get("/api/providers", headers=_auth()).get_json()
               if p["id"] == pid)
    assert row["rank"] == "top" and row["no_tools"] is True and row["custom"] is True


def test_a_built_in_card_has_no_rank():
    row = next(p for p in app.app.test_client().get("/api/providers", headers=_auth()).get_json()
               if p["id"] == "groq")
    assert row["rank"] is None and row["no_tools"] is None


def test_an_unknown_rank_is_refused():
    r = app.app.test_client().post("/api/custom-providers", headers=_auth(), json={
        "name": "X bridge", "base_url": "http://127.0.0.1:1/v1", "rank": "best-ever"})
    assert r.status_code == 400 and "rank" in r.get_json()["error"]
    pid = _create()
    assert _put(pid, rank="supreme").status_code == 400
    assert _put(pid, rank=5).status_code == 400


def test_no_tools_must_be_a_boolean():
    pid = _create()
    assert _put(pid, no_tools="yes").status_code == 400
    assert _put(pid, no_tools=1).status_code == 400


def test_an_edit_that_omits_the_fields_leaves_them_alone():
    pid = _create(rank="top", no_tools=True)
    assert _put(pid, notes="hello").status_code == 200
    d = _defn(pid)
    assert d["rank"] == "top" and d["no_tools"] is True


def test_rank_is_case_and_space_tolerant_and_auto_clears_it():
    pid = _create(rank=" TOP ")
    assert _defn(pid)["rank"] == "top"
    _put(pid, rank="Auto")
    assert _defn(pid)["rank"] == "auto"
    assert "rank" not in (config.get_custom_provider(pid) or {}) or config.get_custom_provider(pid)["rank"] is None


# --------------------------------------------------------------------------- #
# Export / import
# --------------------------------------------------------------------------- #

def test_the_export_carries_the_rank_and_the_import_keeps_it():
    pid = _create(rank="top", no_tools=True)
    sec = config.get_custom_providers()
    assert sec[pid]["rank"] == "top" and sec[pid]["no_tools"] is True
    out, err = app._validate_settings_import({"custom_providers": {pid + "-copy": dict(sec[pid])}})
    assert err is None, err
    row = out["custom_providers"][pid + "-copy"]
    assert row["rank"] == "top" and row["no_tools"] is True


def test_the_import_drops_a_bad_rank_instead_of_trusting_it():
    out, err = app._validate_settings_import({"custom_providers": {"my-llm": {
        "name": "My LLM", "base_url": "https://api.example.com/v1", "rank": "god", "no_tools": "yes"}}})
    assert err is None, err
    row = out["custom_providers"]["my-llm"]
    assert "rank" not in row and "no_tools" not in row


# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #

HTML = io.open("templates/index.html", encoding="utf-8").read()


def test_the_add_dialog_has_the_rank_and_chat_only_inputs():
    dlg = HTML[HTML.index('id="add-provider-dialog"'):]
    dlg = dlg[:dlg.index("</dialog>")]
    assert 'id="pa-rank"' in dlg and 'id="pa-no-tools"' in dlg
    for v in ("auto", "top", "high", "medium", "low"):
        assert 'value="%s"' % v in dlg.split('id="pa-rank"')[1].split("</select>")[0]


def test_the_settings_form_has_them_too():
    form = HTML[HTML.index('id="cp-form"'):]
    form = form[:form.index("</form>")]
    assert 'id="cp-rank"' in form and 'id="cp-no-tools"' in form


def test_the_provider_card_has_a_rank_selector_for_user_added_providers_only():
    assert "(p.custom ? rankRowHTML(p) : '')" in HTML
    assert "function rankRowHTML(" in HTML
    assert "/api/custom-providers/' + encodeURIComponent(p.id), { method: 'PUT', body: { rank: want } }" in HTML


def test_the_body_builder_sends_rank_and_chat_only_only_when_given():
    fn = HTML[HTML.index("function cpBuildBody("):]
    fn = fn[:fn.index("function cpFormReset(")]
    assert "if (v.rank != null) body.rank" in fn and "if (v.no_tools != null) body.no_tools" in fn


def test_every_form_passes_them_to_the_builder():
    assert HTML.count("rank: $('#cp-rank').value, no_tools: $('#cp-no-tools').checked") == 1
    assert "rank: val('pa-rank') || 'auto'" in HTML


def test_the_new_css_uses_theme_tokens_only():
    css = HTML[HTML.index(".pa-check{"):HTML.index("/* add-prov-css:end */")]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", css), css
