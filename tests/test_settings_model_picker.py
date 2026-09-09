r"""The Settings model picker: one row per model, and what it can do to routing.

REQUESTED 2026-09-09: a whitelist ("models that I want to use only"), a
blocklist with live suggestions of "models available connected working", the
same model detected across every provider so it is chosen once, running
sessions visible with a per-conversation mode, and editable category membership.

WHAT THE PANEL WAS. Two lists with two mental models. The table was
provider-qualified -- 504 rows for 281 models on this machine -- so switching
one model off meant finding it under each provider in turn; and because that
was unusable for a model served five ways, a second "Blocked models" panel sat
underneath it with a blind substring box that blocked a whole family by name
and showed you what it had done only afterwards.

Grouping by identity removes the reason for both. One row is the model
everywhere, Block on that row is what the pattern box was approximating, and
the search box above shows you exactly which models a word matches before you
touch anything.

MEASURED live: 504 provider+model rows collapse to 281 model rows, 262 of them
answering. The grouping key is _normalize_model_identity, which routing already
uses, so the table and the router cannot disagree about what "the same model"
means.
"""
import json

import pytest

import app as A
import config


@pytest.fixture()
def client(tmp_path, monkeypatch):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"control_token": "t"}), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(p))
    monkeypatch.setattr(config, "_config_path", lambda: str(p))
    config.invalidate_settings_cache()
    A.app.config["TESTING"] = True
    yield A.app.test_client()
    config.invalidate_settings_cache()


H = {"X-Free-LLM-Hub-Token": "t", "X-Free-LLM-Hub": "dashboard"}


def _post(client, path, body):
    return client.post(path, headers=H, json=body)


# --------------------------------------------------------------------------- #
# Grouping
# --------------------------------------------------------------------------- #

def test_the_rows_carry_what_the_picker_needs(client, monkeypatch):
    monkeypatch.setattr(A, "_identity_rows", lambda: [{
        "identity": "gpt-oss-120b", "providers": ["groq", "cerebras"],
        "ids": ["groq/openai/gpt-oss-120b", "cerebras/gpt-oss-120b"],
        "count": 2, "working": 2, "score": 71.0, "tool_capable": True,
        "state": "ok", "blocked": False, "allowed": False, "categories": ["coding"],
    }])
    d = client.get("/api/model-identities", headers=H).get_json()
    row = d["models"][0]
    for field in ("identity", "providers", "ids", "count", "working", "score",
                  "state", "blocked", "allowed", "categories"):
        assert field in row, field
    assert d["working"] == 1
    assert d["whitelist_active"] is False


def test_the_modes_are_listed_for_the_row_chips(client):
    d = client.get("/api/model-identities", headers=H).get_json()
    keys = [m["key"] for m in d["modes"]]
    assert keys and set(keys) <= set(A._mode_keys())


# --------------------------------------------------------------------------- #
# Blocking and allowing
# --------------------------------------------------------------------------- #

def test_block_then_unblock(client):
    r = _post(client, "/api/model-identities", {"identity": "x-model", "action": "block"})
    assert r.status_code == 200 and "x-model" in r.get_json()["blocked"]
    r = _post(client, "/api/model-identities", {"identity": "x-model", "action": "unblock"})
    assert "x-model" not in r.get_json()["blocked"]


def test_allow_turns_the_whitelist_on(client):
    r = _post(client, "/api/model-identities", {"identity": "x-model", "action": "allow"})
    body = r.get_json()
    assert body["allowed"] == ["x-model"]
    assert body["whitelist_active"] is True


def test_clearing_the_whitelist_turns_it_off(client):
    _post(client, "/api/model-identities", {"identity": "x-model", "action": "allow"})
    r = client.delete("/api/model-identities/whitelist", headers=H)
    assert r.status_code == 200
    assert r.get_json() == {"allowed": [], "whitelist_active": False}


def test_a_whitelist_cannot_be_narrowed_to_nothing_working(client, monkeypatch):
    """Enforced, not fail-open -- so a whitelist with no live model on it is a
    hub that answers nothing. The refusal belongs where it can be explained."""
    _post(client, "/api/model-identities", {"identity": "a", "action": "allow"})
    _post(client, "/api/model-identities", {"identity": "b", "action": "allow"})
    monkeypatch.setattr(A, "_identity_rows", lambda: [
        {"identity": "a", "working": 1}, {"identity": "b", "working": 0}])
    r = _post(client, "/api/model-identities", {"identity": "a", "action": "disallow"})
    assert r.status_code == 400
    assert "no working model" in r.get_json()["error"]["message"]
    assert "a" in A._allowed_identities(), "the refused change must not be applied"


def test_removing_the_last_entry_is_still_allowed_when_others_work(client, monkeypatch):
    _post(client, "/api/model-identities", {"identity": "a", "action": "allow"})
    _post(client, "/api/model-identities", {"identity": "b", "action": "allow"})
    monkeypatch.setattr(A, "_identity_rows", lambda: [
        {"identity": "a", "working": 1}, {"identity": "b", "working": 1}])
    assert _post(client, "/api/model-identities",
                 {"identity": "b", "action": "disallow"}).status_code == 200


@pytest.mark.parametrize("body,frag", [
    ({"action": "block"}, "identity is required"),
    ({"identity": "x", "action": "wat"}, "action must be"),
    ({"identity": "x"}, "action must be"),
])
def test_bad_input_says_what_is_wrong(client, body, frag):
    r = _post(client, "/api/model-identities", body)
    assert r.status_code == 400
    assert frag in r.get_json()["error"]["message"]


# --------------------------------------------------------------------------- #
# Editing a category
# --------------------------------------------------------------------------- #

def test_a_category_edit_round_trips(client):
    r = _post(client, "/api/model-category",
              {"key": "coding", "identity": "x-model", "member": True})
    assert r.get_json()["overrides"]["coding"]["add"] == ["x-model"]
    r = _post(client, "/api/model-category",
              {"key": "coding", "identity": "x-model", "member": False})
    ov = r.get_json()["overrides"]["coding"]
    assert ov["remove"] == ["x-model"] and ov["add"] == []


def test_an_unknown_mode_is_refused(client):
    r = _post(client, "/api/model-category",
              {"key": "not-a-mode", "identity": "x", "member": True})
    assert r.status_code == 400 and "unknown mode" in r.get_json()["error"]["message"]


def test_a_category_edit_changes_routing(client):
    _post(client, "/api/model-category",
          {"key": "coding", "identity": "x-model", "member": True})
    assert A._mode_allows("coding", "groq", "x-model")


# --------------------------------------------------------------------------- #
# The panel itself
# --------------------------------------------------------------------------- #

SRC = open("templates/index.html", encoding="utf-8").read()


def test_the_two_old_panels_are_now_one():
    """The blind substring box is gone -- its job is the Block button on a row
    that first shows you what the word matches."""
    assert 'id="sd-block-pattern"' not in SRC
    assert 'id="sd-blocked"' not in SRC
    assert "loadBlocked" not in SRC


def test_the_search_box_is_labelled_not_just_placeholdered():
    """A placeholder disappears the moment someone types."""
    assert '<label class="sd-label" for="sd-models-filter">' in SRC


def test_the_result_count_is_announced():
    assert 'id="sd-models-count"' in SRC and 'aria-live="polite"' in SRC


def test_the_row_actions_report_their_state():
    """aria-pressed, so the control says what it is, not just what it looks
    like -- colour alone is not a state."""
    assert 'data-act="allow"' in SRC and 'data-act="block"' in SRC
    assert "aria-pressed" in SRC


def test_the_whitelist_strip_can_be_undone():
    assert 'id="sd-wl-clear"' in SRC
    assert "Use every model again" in SRC


def test_the_sessions_panel_exists_with_a_per_session_mode():
    assert 'id="sd-sessions"' in SRC
    assert "loadSdSessions" in SRC
    assert "session_id: sel.getAttribute('data-sid')" in SRC


def test_the_list_is_fetched_from_the_grouped_endpoint():
    assert "api('/api/model-identities')" in SRC


def test_reduced_motion_is_respected():
    assert "@media (prefers-reduced-motion:reduce)" in SRC


def test_no_emoji_are_used_as_controls():
    """Icons are SVG or text in this dashboard; an emoji is font-dependent and
    cannot be themed."""
    for frag in ("sd-pill", "sd-chip"):
        i = SRC.index(frag)
        window = SRC[i:i + 400]
        assert not any(ord(ch) > 0x2500 for ch in window), window[:120]
