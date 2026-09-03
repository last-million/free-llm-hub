"""Named fallback chains: an order the user asserts.

Gap 9 from the freellmapi comparison. The audit called ours PARTIAL, and that
was the right word for the wrong reason: the hub had category BUTTONS, which are
a different thing entirely. A category is a SET, used to filter what routing may
consider, with the benchmark still deciding the order inside it. A chain is an
ORDER -- try this exact model, then that one -- which is the only way to say "I
know this pairing works for my project".

The design decision that matters is that a chain is a preference, not a cage.
The ordinary chain still follows the named entries, so a chain whose models are
all rate-limited degrades to normal routing instead of failing. A chain that
could dead-end would be worse than no chain at all, because it would fail
exactly when everything is busiest.

Entries are also filtered against the live catalog rather than trusted: a chain
saved months ago naming a model its provider has since withdrawn must not put a
dead hop at the front of every request.
"""
from unittest import mock

import pytest

import app as A
import config


@pytest.fixture
def client():
    return A.app.test_client()


def _ctl():
    return {"X-Free-LLM-Hub-Token": config.ensure_control_token(),
            "X-Free-LLM-Hub": "dashboard"}


@pytest.fixture
def chains(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_LLM_HUB_CONFIG", str(tmp_path / "config.json"))
    yield


CATALOG = [{"id": "alpha/one", "provider": "alpha", "model": "one"},
           {"id": "beta/two", "provider": "beta", "model": "two"},
           {"id": "gamma/three", "provider": "gamma", "model": "three"}]


@pytest.fixture
def catalog():
    with mock.patch.object(A, "aggregated_models", return_value=list(CATALOG)), \
         mock.patch.object(A, "_is_model_dead", return_value=False):
        yield


# --------------------------------------------------------------------------- #
# Storing them
# --------------------------------------------------------------------------- #

def test_a_chain_can_be_saved_and_read_back(client, chains, catalog):
    r = client.post("/api/chains", headers=_ctl(),
                    json={"name": "coding", "models": ["beta/two", "alpha/one"]})
    assert r.status_code == 200
    got = client.get("/api/chains", headers=_ctl()).get_json()["chains"]
    assert got[0]["name"] == "coding"
    assert got[0]["models"] == ["beta/two", "alpha/one"]


def test_the_order_is_preserved(client, chains, catalog):
    """The order IS the feature; sorting it would erase the whole point."""
    client.post("/api/chains", headers=_ctl(),
                json={"name": "c", "models": ["gamma/three", "alpha/one", "beta/two"]})
    assert A._named_chains()["c"] == ["gamma/three", "alpha/one", "beta/two"]


def test_a_chain_can_be_deleted(client, chains, catalog):
    client.post("/api/chains", headers=_ctl(), json={"name": "c", "models": ["alpha/one"]})
    client.delete("/api/chains", headers=_ctl(), json={"name": "c"})
    assert client.get("/api/chains", headers=_ctl()).get_json()["chains"] == []


def test_a_name_that_is_already_a_model_id_is_refused(client, chains, catalog):
    """A chain called "swarm" would silently change what an existing client
    asks for."""
    for name in ("auto", "best", "swarm", "crew"):
        r = client.post("/api/chains", headers=_ctl(),
                        json={"name": name, "models": ["alpha/one"]})
        assert r.status_code == 400, name


def test_a_name_with_a_slash_is_refused(client, chains, catalog):
    """'/' is what separates a provider from a model."""
    assert client.post("/api/chains", headers=_ctl(),
                       json={"name": "a/b", "models": ["alpha/one"]}).status_code == 400


def test_an_empty_chain_is_refused(client, chains, catalog):
    for models in ([], "alpha/one", [""], None):
        assert client.post("/api/chains", headers=_ctl(),
                           json={"name": "c", "models": models}).status_code == 400


def test_it_reports_which_entries_are_still_live(client, chains, catalog):
    """A chain outlives the catalog it was written against."""
    r = client.post("/api/chains", headers=_ctl(),
                    json={"name": "c", "models": ["alpha/one", "gone/model"]})
    assert r.get_json()["live"] == ["alpha/one"]


def test_the_routes_are_control_gated(client):
    """Deliberately NOT on the temp-config fixture: a fresh config has no
    control token, and the gate correctly does nothing when none is set."""
    assert client.get("/api/chains").status_code == 401


# --------------------------------------------------------------------------- #
# Resolving them
# --------------------------------------------------------------------------- #

def test_entries_resolve_to_provider_and_model(chains, catalog):
    config.set_json("chains", {"c": ["beta/two", "alpha/one"]})
    assert A._chain_entries("c") == [("beta", "two"), ("alpha", "one")]


def test_a_withdrawn_model_is_dropped_from_the_chain(chains, catalog):
    """Not merely skipped at request time: it must never head the chain."""
    config.set_json("chains", {"c": ["nope/gone", "alpha/one"]})
    assert A._chain_entries("c") == [("alpha", "one")]


def test_a_dead_model_is_dropped_too(chains):
    config.set_json("chains", {"c": ["alpha/one", "beta/two"]})
    with mock.patch.object(A, "aggregated_models", return_value=list(CATALOG)), \
         mock.patch.object(A, "_is_model_dead",
                           side_effect=lambda p, m: p == "alpha"):
        assert A._chain_entries("c") == [("beta", "two")]


def test_a_name_is_recognised_case_insensitively(chains, catalog):
    config.set_json("chains", {"coding": ["alpha/one"]})
    assert A._is_chain_name("CODING") and A._is_chain_name("coding")


def test_an_unknown_name_is_not_a_chain(chains, catalog):
    assert not A._is_chain_name("nope")
    assert not A._is_chain_name(None)


def test_a_malformed_stored_chain_does_not_break_anything(chains):
    """A hand-edited config must not stop the hub. ("5" survives on purpose --
    JSON turns the int key into a string, and "5" is a perfectly legal name.)"""
    config.set_json("chains", {"c": "not-a-list", "d": ["alpha/one", 7, ""],
                               "e": {"nope": 1}})
    assert A._named_chains() == {"d": ["alpha/one"]}


def test_a_chains_setting_of_the_wrong_type_is_ignored(chains):
    config.set_json("chains", ["not", "a", "dict"])
    assert A._named_chains() == {}


# --------------------------------------------------------------------------- #
# Using them
# --------------------------------------------------------------------------- #

def test_the_chain_entries_head_the_fallback_chain(catalog):
    """The whole point: the user's order runs first."""
    built = A._build_chain("zzz", "other", prefer=[("beta", "two"), ("alpha", "one")])
    assert built[:2] == [("beta", "two"), ("alpha", "one")]


def test_the_ordinary_chain_still_follows(catalog):
    """A preference, not a cage -- a chain whose models are all rate-limited
    must degrade to normal routing rather than fail."""
    built = A._build_chain("zzz", "other", prefer=[("beta", "two")])
    assert len(built) > 1


def test_a_preferred_entry_is_not_duplicated_further_down(catalog):
    built = A._build_chain("alpha", "one", prefer=[("alpha", "one")])
    assert built.count(("alpha", "one")) == 1


def test_a_vetoed_model_is_not_resurrected_by_a_chain(catalog):
    """"retry with a different model" must keep working against a named chain."""
    veto = {A._normalize_model_identity("two")}
    built = A._build_chain("zzz", "other", prefer=[("beta", "two"), ("alpha", "one")],
                           exclude_identities=veto)
    assert ("beta", "two") not in built


def test_no_preference_leaves_the_chain_exactly_as_it_was(catalog):
    assert A._build_chain("alpha", "one") == A._build_chain("alpha", "one", prefer=None)


# --------------------------------------------------------------------------- #
# As a model id on a real request
# --------------------------------------------------------------------------- #

def _completion():
    return {"choices": [{"message": {"role": "assistant", "content": "hi"},
                         "finish_reason": "stop"}], "usage": {}}


def test_a_chain_name_used_as_the_model_resolves_to_its_first_entry(client, chains, catalog):
    config.set_json("chains", {"coding": ["beta/two", "alpha/one"]})
    src = open("app.py", encoding="utf-8").read()
    assert "_is_chain_name(body.get(\"model\"))" in src
    assert "prefer=chain_prefer" in src


def test_a_chain_whose_models_are_all_gone_falls_back_to_auto(chains):
    """A saved preference must never be able to break a request outright."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index("chain_prefer = None")
    assert 'body["model"] = "auto"' in src[i:i + 1200]
