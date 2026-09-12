r"""A conversation can say which models belong to ITS coding (vision, seo...) mode.

REQUESTED: "we can edit and add more or remove models from each category --
coding, uncensored, etc. -- and for each working session we can edit the mode
and even customize the mode's models for each session conversation".

The first half existed: the Settings chips add or remove a model from a mode,
stored globally. The second half did not. The chips ignored the scope
selector and always wrote the global list, so "customize the coding models
for this project" silently changed every project's coding mode; and routing
had nowhere to read a per-conversation edit from anyway.

Three layers now, narrowest first, each able to add or remove:

    this conversation's edit  >  the global edit  >  the built-in pattern
"""
import pytest

import app as A


APP = open("app.py", encoding="utf-8").read()
SRC = open("templates/index.html", encoding="utf-8").read()


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    import config
    p = tmp_path / "config.json"
    p.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(p))
    monkeypatch.setattr(config, "_config_path", lambda: str(p))
    config.invalidate_settings_cache()
    yield
    config.invalidate_settings_cache()


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #

def test_a_conversation_can_add_a_model_to_a_mode(isolated):
    ov = A._set_session_category("s1", "coding", "some-model", True)
    assert ov == {"coding": {"add": {"some-model"}, "remove": set()}}
    assert A._session_category_overrides("s1") == ov


def test_and_remove_one(isolated):
    ov = A._set_session_category("s1", "coding", "qwen3.6-coder", False)
    assert ov["coding"]["remove"] == {"qwen3.6-coder"}


def test_adding_undoes_removing_and_back(isolated):
    A._set_session_category("s1", "coding", "m", False)
    ov = A._set_session_category("s1", "coding", "m", True)
    assert ov["coding"] == {"add": {"m"}, "remove": set()}
    ov = A._set_session_category("s1", "coding", "m", False)
    assert ov["coding"] == {"add": set(), "remove": {"m"}}


def test_one_conversations_edit_is_not_anothers(isolated):
    A._set_session_category("s1", "coding", "m", True)
    assert A._session_category_overrides("s2") == {}
    assert A._category_overrides() == {}, "and it is not the global edit either"


def test_the_global_edit_is_untouched_by_a_conversations(isolated):
    A._set_category_member("coding", "g", True)
    A._set_session_category("s1", "coding", "m", True)
    assert A._category_overrides()["coding"]["add"] == {"g"}
    assert A._session_category_overrides("s1")["coding"]["add"] == {"m"}


def test_a_mode_can_be_put_back_to_the_default(isolated):
    A._set_session_category("s1", "coding", "m", True)
    A._set_session_category("s1", "vision", "v", False)
    ov = A._reset_session_category("s1", "coding")
    assert "coding" not in ov and ov["vision"]["remove"] == {"v"}


def test_an_empty_row_is_not_stored(isolated):
    """The same discipline as the allow/block lists: a config that grows one
    empty row per conversation ever opened is a config describing nothing."""
    import config
    A._set_session_category("s1", "coding", "m", True)
    A._reset_session_category("s1", "coding")
    assert "s1" not in (config.get_setting(A._SESSION_MODELS_SETTING, {}) or {})


def test_it_lives_beside_the_allow_and_block_lists(isolated):
    """One row per conversation, so ending the conversation drops all of it."""
    import config
    A._set_session_model("s1", "m", block=True)
    A._set_session_category("s1", "coding", "m2", True)
    row = config.get_setting(A._SESSION_MODELS_SETTING, {})["s1"]
    assert row["block"] == ["m"] and row["categories"]["coding"]["add"] == ["m2"]
    A._forget_session_models("s1")
    assert A._session_category_overrides("s1") == {}


def test_clearing_the_block_list_keeps_the_categories(isolated):
    """The allow/block writer used to drop a row with no lists left -- which
    would now throw away the categories stored on the same row."""
    A._set_session_category("s1", "coding", "m2", True)
    A._set_session_model("s1", "m", block=True)
    A._set_session_model("s1", "m", block=False)
    assert A._session_category_overrides("s1")["coding"]["add"] == {"m2"}


# --------------------------------------------------------------------------- #
# Routing reads it
# --------------------------------------------------------------------------- #

def test_the_conversations_edit_wins_over_the_global_one(isolated, monkeypatch):
    monkeypatch.setattr(A, "_normalize_model_identity", lambda m: m)
    monkeypatch.setattr(A.model_categories, "matches", lambda *a: False)
    A._set_category_member("coding", "m", False)          # globally: out
    sov = {"coding": {"add": {"m"}, "remove": set()}}    # here: in
    assert A._mode_allows("coding", "p", "m", session_overrides=sov) is True
    assert A._mode_allows("coding", "p", "m", session_overrides={}) is False


def test_and_over_the_built_in_pattern(isolated, monkeypatch):
    monkeypatch.setattr(A, "_normalize_model_identity", lambda m: m)
    monkeypatch.setattr(A.model_categories, "matches", lambda *a: True)
    sov = {"coding": {"add": set(), "remove": {"m"}}}
    assert A._mode_allows("coding", "p", "m", session_overrides=sov) is False


def test_no_session_layer_means_the_old_answer(isolated, monkeypatch):
    monkeypatch.setattr(A, "_normalize_model_identity", lambda m: m)
    monkeypatch.setattr(A.model_categories, "matches", lambda *a: True)
    assert A._mode_allows("coding", "p", "m", session_overrides={}) is True


def test_the_request_path_resolves_the_session_once(isolated, monkeypatch):
    """_mode_allows runs per candidate in every chain build; the session's
    edits are read from the setting once per request, like the block list."""
    calls = []
    real = A._session_category_overrides

    def counting(sid=None):
        calls.append(sid)
        return real(sid)
    monkeypatch.setattr(A, "_session_category_overrides", counting)
    monkeypatch.setattr(A, "_build_sid", lambda: "s1")
    A._set_session_category("s1", "coding", "m", True)
    calls.clear()
    with A.app.test_request_context("/"):
        for _ in range(5):
            A._request_category_overrides()
    assert len(calls) == 1


def test_routing_reads_the_session_of_the_request_by_default(isolated, monkeypatch):
    monkeypatch.setattr(A, "_normalize_model_identity", lambda m: m)
    monkeypatch.setattr(A.model_categories, "matches", lambda *a: False)
    monkeypatch.setattr(A, "_build_sid", lambda: "s1")
    A._set_session_category("s1", "coding", "m", True)
    with A.app.test_request_context("/"):
        assert A._mode_allows("coding", "p", "m") is True
    monkeypatch.setattr(A, "_build_sid", lambda: "other")
    with A.app.test_request_context("/"):
        assert A._mode_allows("coding", "p", "m") is False


# --------------------------------------------------------------------------- #
# The routes
# --------------------------------------------------------------------------- #

def test_the_category_route_takes_a_session(isolated, monkeypatch):
    monkeypatch.setattr(A.agentic_chat, "get_session", lambda sid: {"session_id": sid})
    monkeypatch.setattr(A, "_mode_keys", lambda: ("coding", "vision"))
    monkeypatch.setattr(A, "_has_control_token", lambda: True)
    c = A.app.test_client()
    r = c.post("/api/model-category", json={"key": "coding", "identity": "M",
                                            "member": True, "session_id": "s1"},
               headers={"X-Free-LLM-Hub": "dashboard"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["scope"] == "s1"
    assert A._session_category_overrides("s1")["coding"]["add"] == {"m"}
    assert A._category_overrides() == {}


def test_a_session_that_does_not_exist_is_refused(isolated, monkeypatch):
    monkeypatch.setattr(A.agentic_chat, "get_session", lambda sid: None)
    monkeypatch.setattr(A, "_mode_keys", lambda: ("coding",))
    monkeypatch.setattr(A, "_has_control_token", lambda: True)
    c = A.app.test_client()
    r = c.post("/api/model-category", json={"key": "coding", "identity": "m",
                                            "member": True, "session_id": "ghost"},
               headers={"X-Free-LLM-Hub": "dashboard"})
    assert r.status_code == 404


def test_the_route_can_reset_a_mode_for_a_session(isolated, monkeypatch):
    monkeypatch.setattr(A, "_mode_keys", lambda: ("coding",))
    monkeypatch.setattr(A, "_has_control_token", lambda: True)
    A._set_session_category("s1", "coding", "m", True)
    c = A.app.test_client()
    r = c.post("/api/model-category", json={"key": "coding", "session_id": "s1",
                                            "reset": True},
               headers={"X-Free-LLM-Hub": "dashboard"})
    assert r.status_code == 200
    assert A._session_category_overrides("s1") == {}


def test_without_a_session_it_is_still_the_global_edit(isolated, monkeypatch):
    monkeypatch.setattr(A, "_mode_keys", lambda: ("coding",))
    monkeypatch.setattr(A, "_has_control_token", lambda: True)
    c = A.app.test_client()
    r = c.post("/api/model-category", json={"key": "coding", "identity": "m", "member": True},
               headers={"X-Free-LLM-Hub": "dashboard"})
    assert r.status_code == 200 and r.get_json()["scope"] == "global"
    assert A._category_overrides()["coding"]["add"] == {"m"}


def test_the_identity_list_reports_the_scoped_categories(isolated, monkeypatch):
    """With a conversation in scope, a row's categories are THAT
    conversation's, and its own edits are marked as such."""
    fake = {"models": [{"id": "p/m", "provider": "p", "model": "m", "state": "ok",
                        "score": 1.0, "tool_capable": True}]}

    class _R:
        def get_json(self):
            return fake
    monkeypatch.setattr(A, "api_tracking", lambda: _R())
    monkeypatch.setattr(A, "_normalize_model_identity", lambda m: m)
    monkeypatch.setattr(A, "_mode_keys", lambda: ("coding", "vision"))
    monkeypatch.setattr(A.model_categories, "matches", lambda *a: False)
    A._set_session_category("s1", "coding", "m", True)
    rows = {r["identity"]: r for r in A._identity_rows("s1")}
    assert rows["m"]["categories"] == ["coding"]
    assert rows["m"]["custom_categories"] == ["coding"]
    rows = {r["identity"]: r for r in A._identity_rows(None)}
    assert rows["m"]["categories"] == [], "the global view shows the default"
    assert rows["m"]["custom_categories"] == []


# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #

def test_the_chips_send_the_scope():
    body = SRC[SRC.index("host.querySelectorAll('.sd-chip')"):]
    body = body[:body.index("function sdPickedList")]
    assert "if (_sdScope) payload.session_id = _sdScope;" in body


def test_an_own_edit_looks_like_one():
    assert "custom_categories" in SRC
    assert ".sd-chip.own{" in SRC


def test_the_conversation_has_a_way_to_its_own_list():
    """Beside the mode it already chooses: one click to Settings with this
    conversation in scope, not "find the selector and pick it"."""
    assert 'id="agent-models"' in SRC
    assert "window.cxSdScope = function(sid)" in SRC
    body = SRC[SRC.index("function initAgentModelsLink()"):]
    body = body[:body.index("function initAgentMode()")]
    # In its OWN tab, on this conversation: the first version switched this
    # page to Settings, and the report was "he opened settings in the same
    # page where I was working".
    assert "'/settings?scope=' + encodeURIComponent(sessionId)" in body
    assert "window.open(" in body
    assert "initAgentModelsLink();" in SRC
