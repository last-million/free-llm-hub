"""Max mode is offered by name, not only accepted when you already know it.

ASKED 2026-09-05: "but how inside cli's use swarm agents mode or max mode?"

The answer is "pick the model called swarm, or best" -- and checking that turned
up the reason the question needed asking. `best` WORKED: _is_orchestrate accepts
it and routes with quality_mode on, and a live call answered in 2.1s. It just
appeared in no model listing:

    /v1/models      -> auto, swarm, team, plan, crew, crew-*   (no best)
    /api/tags       -> same
    /v1beta/models  -> same

So every CLI's /model picker offered Normal (auto) and Swarm, and silently
dropped Max. A mode you can only use if you already know its name is not a mode
the CLIs have.

The cause was three listings each building their own
`[auto] + _SWARM_IDS + CREW_IDS`, none of which mentioned `best`. They now share
one list, so a fourth mode cannot be added to two surfaces out of three.
"""
import json
from unittest import mock

import pytest

import app as A


@pytest.fixture
def client():
    return A.app.test_client()


@pytest.fixture
def ollama_on():
    with mock.patch.object(A.config, "get_flag",
                           side_effect=lambda k, d=None: True if k == "ollama_api" else d):
        yield


# --------------------------------------------------------------------------- #
# One list, three surfaces
# --------------------------------------------------------------------------- #

def test_the_modes_are_named_in_one_place():
    ids = A._virtual_model_ids()
    assert ids[0] == "auto", "auto must stay first: a CLI taking row 1 as its default"
    assert "best" in ids and "swarm" in ids


def test_the_dashboard_switch_maps_onto_these_ids():
    """/agent's Normal / Max / Swarm are exactly auto / best / swarm, so the
    CLIs and the dashboard cannot mean different things by the same mode."""
    import agentic_chat as ac
    assert ac._hub_model_for("normal") == "auto"
    assert ac._hub_model_for("max") == "best"
    assert ac._hub_model_for("swarm") == "swarm"
    for mid in ("auto", "best", "swarm"):
        assert mid in A._virtual_model_ids()


def test_openai_listing_offers_max(client):
    ids = [m["id"] for m in client.get("/v1/models").get_json()["data"]]
    assert "best" in ids, "the OpenAI surface is what most CLIs read"
    assert "swarm" in ids and "auto" in ids


def test_the_second_listing_shape_offers_it_too(client):
    """/v1/models returns both `data` and `models`; codex reads the latter."""
    body = client.get("/v1/models").get_json()
    assert "best" in [m["id"] for m in body["models"]]


def test_gemini_listing_offers_max(client):
    names = [m["name"] for m in client.get("/v1beta/models").get_json()["models"]]
    assert "models/best" in names and "models/swarm" in names


def test_ollama_listing_offers_max(client, ollama_on):
    names = [m["name"] for m in client.get("/api/tags").get_json()["models"]]
    assert "best:latest" in names and "swarm:latest" in names


def test_no_listing_builds_its_own_copy():
    """Three hand-rolled lists is how `best` went missing from all of them.

    Exactly one occurrence: the definition inside _virtual_model_ids. A second
    means some surface is assembling the ids itself again, which is the shape
    that loses the next mode."""
    src = open("app.py", encoding="utf-8").read()
    assert src.count("_SWARM_IDS + tuple(crews.CREW_IDS)") == 1
    i = src.index("_SWARM_IDS + tuple(crews.CREW_IDS)")
    assert "def _virtual_model_ids" in src[max(0, i - 300):i]


def test_max_is_labelled_so_a_picker_explains_itself(client):
    row = [m for m in client.get("/v1/models").get_json()["data"]
           if m["id"] == "best"][0]
    assert "Max" in row["display_name"]


# --------------------------------------------------------------------------- #
# ...and it still routes
# --------------------------------------------------------------------------- #

def test_best_still_means_quality_routing():
    """Advertising it would be worse than useless if it stopped being the mode."""
    assert A._is_orchestrate("best")
    assert not A._is_swarm_model("best")


def test_swarm_still_means_the_fan_out():
    assert A._is_swarm_model("swarm")
