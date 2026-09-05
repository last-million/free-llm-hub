"""Blocking a model FAMILY by name, and seeing what is blocked.

Requested 2026-09-05: "blacklist model gpt oss and add it in section for
blacklist models in settings page".

The per-model switches already write this list one model at a time. That is
unusable for retiring a family: gpt-oss is served by groq, nvidia, llm7,
cerebras and several g4f relays under slightly different spellings, so switching
it off means finding the same name over and over in a table of hundreds. One
substring covers every spelling.

The list is also worth SHOWING. Until now a blocked model simply vanished from
the table it was blocked in -- the only evidence was a model that had quietly
stopped being used, which is indistinguishable from a provider going down.
"""
import json

import pytest

import app as A
import config


HDRS = None


@pytest.fixture
def client(monkeypatch):
    global HDRS
    # Writes also need the anti-CSRF header the dashboard sends -- see
    # _local_control_guard: a custom header forces a CORS preflight, which is
    # what stops an arbitrary website reconfiguring a localhost hub.
    HDRS = {"X-Free-LLM-Hub-Token": config.ensure_control_token(),
            "X-Free-LLM-Hub": "dashboard"}
    monkeypatch.setattr(A, "_available_providers", lambda *a, **k: ["groq", "llm7"])
    monkeypatch.setattr(A, "provider_free_models",
                        lambda pid: {"groq": ["openai/gpt-oss-120b", "qwen/qwen3.8-27b"],
                                     "llm7": ["gpt-oss", "minimax-m2.7"]}.get(pid, []))
    saved = {}
    monkeypatch.setattr(config, "set_setting",
                        lambda k, v: saved.__setitem__(k, v))
    monkeypatch.setattr(config, "get_setting",
                        lambda k, d=None: saved.get(k, d))
    A.app.config["TESTING"] = True
    with A.app.test_client() as c:
        yield c


def _post(client, **body):
    r = client.post("/api/model-blocklist", json=body, headers=HDRS)
    return r.status_code, r.get_json()


def _delete(client, **body):
    r = client.delete("/api/model-blocklist", json=body, headers=HDRS)
    return r.status_code, r.get_json()


def _ids(payload):
    return sorted(row["id"] for row in payload["blocked"])


# --------------------------------------------------------------------------- #
# Blocking a family
# --------------------------------------------------------------------------- #

def test_one_name_blocks_every_spelling_across_providers(client):
    """The whole point. 'gpt-oss' is 'openai/gpt-oss-120b' on groq and a bare
    'gpt-oss' on llm7, and both must go."""
    status, out = _post(client, pattern="gpt-oss")
    assert status == 200
    assert _ids(out) == ["groq/openai/gpt-oss-120b", "llm7/gpt-oss"]


def test_it_does_not_touch_anything_else(client):
    _post(client, pattern="gpt-oss")
    status, out = _post(client, pattern="gpt-oss")     # idempotent
    assert "groq/qwen/qwen3.8-27b" not in _ids(out)
    assert "llm7/minimax-m2.7" not in _ids(out)


def test_blocking_twice_changes_nothing(client):
    _post(client, pattern="gpt-oss")
    _, first = _post(client, pattern="gpt-oss")
    _, second = _post(client, pattern="gpt-oss")
    assert _ids(first) == _ids(second)


def test_the_match_is_case_insensitive(client):
    _, out = _post(client, pattern="GPT-OSS")
    assert len(out["blocked"]) == 2


def test_a_pattern_matching_nothing_blocks_nothing(client):
    _, out = _post(client, pattern="no-such-model")
    assert out["blocked"] == []


def test_an_empty_request_is_refused(client):
    """Refusing beats guessing: an empty pattern would match every model and
    switch the whole fleet off in one click."""
    status, _out = _post(client)
    assert status == 400
    status, _out = _post(client, pattern="   ")
    assert status == 400


# --------------------------------------------------------------------------- #
# Unblocking
# --------------------------------------------------------------------------- #

def test_one_model_can_be_unblocked_by_id(client):
    _post(client, pattern="gpt-oss")
    _, out = _delete(client, id="llm7/gpt-oss")
    assert _ids(out) == ["groq/openai/gpt-oss-120b"]


def test_a_family_can_be_unblocked_by_name(client):
    _post(client, pattern="gpt-oss")
    _, out = _delete(client, pattern="gpt-oss")
    assert out["blocked"] == []


def test_a_single_id_can_be_blocked_directly(client):
    _, out = _post(client, id="groq/qwen/qwen3.8-27b")
    assert _ids(out) == ["groq/qwen/qwen3.8-27b"]


# --------------------------------------------------------------------------- #
# A withdrawn model keeps its entry
# --------------------------------------------------------------------------- #

def test_a_model_no_longer_listed_stays_blocked(client, monkeypatch):
    """Providers withdraw and restore models constantly. Dropping the entry
    would silently re-enable a model the user had switched off, the moment it
    came back."""
    _post(client, pattern="gpt-oss")
    monkeypatch.setattr(A, "provider_free_models", lambda pid: [])
    r = client.get("/api/model-blocklist", headers=HDRS)
    out = r.get_json()
    assert len(out["blocked"]) == 2
    assert all(row["live"] is False for row in out["blocked"])


def test_live_says_which_ones_the_hub_can_see(client):
    _post(client, pattern="gpt-oss")
    out = client.get("/api/model-blocklist", headers=HDRS).get_json()
    assert all(row["live"] is True for row in out["blocked"])


def test_a_withdrawn_entry_can_still_be_unblocked(client, monkeypatch):
    """It is exactly the entry the per-model table cannot reach, because that
    table only lists what is live."""
    _post(client, pattern="gpt-oss")
    monkeypatch.setattr(A, "provider_free_models", lambda pid: [])
    _, out = _delete(client, pattern="gpt-oss")
    assert out["blocked"] == []


# --------------------------------------------------------------------------- #
# The shape the UI reads
# --------------------------------------------------------------------------- #

def test_each_row_carries_what_the_ui_shows(client):
    _post(client, pattern="gpt-oss")
    row = client.get("/api/model-blocklist", headers=HDRS).get_json()["blocked"][0]
    assert set(row) == {"id", "provider", "model", "live"}
    assert row["id"] == row["provider"] + "/" + row["model"]


def test_a_provider_scoped_model_id_splits_on_the_first_slash_only(client):
    """'groq/openai/gpt-oss-120b' is provider 'groq', model
    'openai/gpt-oss-120b' -- splitting on the last slash would invent a
    provider called 'openai'."""
    _post(client, pattern="gpt-oss")
    rows = {r["id"]: r for r in client.get("/api/model-blocklist", headers=HDRS).get_json()["blocked"]}
    r = rows["groq/openai/gpt-oss-120b"]
    assert r["provider"] == "groq"
    assert r["model"] == "openai/gpt-oss-120b"


def test_the_count_matches_the_rows(client):
    _post(client, pattern="gpt-oss")
    out = client.get("/api/model-blocklist", headers=HDRS).get_json()
    assert out["count"] == len(out["blocked"])


def test_reading_the_list_does_not_change_it(client):
    _post(client, pattern="gpt-oss")
    before = _ids(client.get("/api/model-blocklist", headers=HDRS).get_json())
    after = _ids(client.get("/api/model-blocklist", headers=HDRS).get_json())
    assert before == after


# --------------------------------------------------------------------------- #
# It reaches routing through the one seam
# --------------------------------------------------------------------------- #

def test_a_blocked_model_is_dead_to_the_router(client):
    """_is_model_dead is the single seam the candidate pool, _build_chain, the
    model lists and the probes all call, so blocking here switches the model off
    in orchestration, the fallback chain and the swarm at once."""
    _post(client, pattern="gpt-oss")
    assert A._is_model_blocked_by_user("llm7", "gpt-oss")
    assert A._is_model_dead("llm7", "gpt-oss")
    assert not A._is_model_dead("groq", "qwen/qwen3.8-27b")
