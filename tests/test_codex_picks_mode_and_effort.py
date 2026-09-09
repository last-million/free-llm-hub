r"""Codex gets the same two choices opencode has: which models, and how hard.

REQUESTED 2026-09-09: "i want in codex too to use command /model to select if
normal max or swarm and also if using uncensored or code or all models ... so
we want 2 choices, the models type and the effort".

CODEX ALREADY HAS A TWO-SCREEN PICKER. Strings lifted from the shipped binary
(codex-cli 0.147.0, tui/src/chatwidget/model_popups.rs):

    Select Model and Effort
    Select Model / Pick a quick auto mode or browse all models.
    Select Reasoning Level for <model>

So the two axes map straight onto it: screen one is the MODE, screen two is the
EFFORT. Nothing is crammed into one id.

WHERE ITS LIST COMES FROM -- and it is not /v1/models. The only `/v1/models`
strings in the binary sit in the Ollama client (`--oss` / --local-provider).
A `[model_providers.*]` provider is never enumerated. The picker is built from
a local CATALOG, resolved highest-first:

    model_catalog_json  ->  ~/.codex/models_cache.json  ->  compiled-in

Proven on this machine: with `model_catalog_json` set, `codex debug models`
returned exactly that file's 10 entries in file order and models_cache.json's
mtime never moved. It replaces the catalog; it does not merge.

WHY THE TEMPLATE COMES FROM THE INSTALLED BINARY. An entry has 34-36 fields,
including a ~13KB `base_instructions` and a `model_messages` object, and codex
refuses to start when the file misses a field it wants -- which is the reason
this file was NOT written before (the note in agentic_chat says so). Hand-
writing a schema that changes between codex releases hands the user a codex
that breaks on the next update. So the template is dumped from the binary that
is going to read it, with `codex debug models --bundled`, and re-dumped on
every hub start: after a codex upgrade the catalog is rewritten in whatever
schema the NEW binary just handed us.

AND THE MODE WAS BEING IGNORED ON THIS PROTOCOL ANYWAY. /v1/chat/completions
sets g.model_mode from a mode id; /v1/responses -- the one codex actually
speaks -- never did. So `model: "coding"` restricted nothing for codex even
before any picker existed.
"""
import json

import pytest

import app as A


# --------------------------------------------------------------------------- #
# Screen 1 + screen 2 -> the hub's two families
# --------------------------------------------------------------------------- #

def test_a_mode_plus_a_reasoning_level_becomes_mode_plus_effort():
    with A.app.test_request_context("/v1/responses"):
        body = A._mode_and_effort({"model": "uncensored",
                                   "reasoning": {"effort": "high"}})
        assert body["model"] == "best"          # the EFFORT the hub routes on
        assert A._active_mode() == "uncensored"  # the MODE the pool is cut to


@pytest.mark.parametrize("effort,expected", [
    ("minimal", "auto"), ("low", "auto"), ("medium", "auto"),
    ("high", "best"),
    ("xhigh", "swarm"), ("max", "swarm"), ("ultra", "swarm"),
])
def test_every_reasoning_level_maps_to_an_effort(effort, expected):
    with A.app.test_request_context("/v1/responses"):
        assert A._mode_and_effort({"model": "coding",
                                   "reasoning": {"effort": effort}})["model"] == expected


def test_no_reasoning_block_means_normal_effort():
    with A.app.test_request_context("/v1/responses"):
        assert A._mode_and_effort({"model": "coding"})["model"] == "auto"


def test_an_unknown_level_is_not_an_error():
    """A codex release adding a level must not 500 the request."""
    with A.app.test_request_context("/v1/responses"):
        assert A._mode_and_effort({"model": "coding",
                                   "reasoning": {"effort": "banana"}})["model"] == "auto"


def test_the_all_mode_restricts_nothing():
    with A.app.test_request_context("/v1/responses"):
        body = A._mode_and_effort({"model": "all", "reasoning": {"effort": "xhigh"}})
        assert body["model"] == "swarm"
        assert A._active_mode() == A.MODE_ALL


# --------------------------------------------------------------------------- #
# ...without touching anything that is not a mode
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("mid", ["auto", "best", "swarm"])
def test_an_explicit_effort_id_is_left_alone(mid):
    """`swarm` is BOTH a category name and the fan-out pipeline's id, and
    _valid_mode() answers 'swarm' for it while _mode_keys() deliberately does
    not offer it. Reading it as a mode here would silently turn every swarm
    request into a plain one."""
    with A.app.test_request_context("/v1/responses"):
        assert A._mode_and_effort({"model": mid,
                                   "reasoning": {"effort": "high"}})["model"] == mid


def test_a_pinned_model_is_left_alone():
    with A.app.test_request_context("/v1/responses"):
        body = {"model": "groq/qwen/qwen3.8-27b", "reasoning": {"effort": "high"}}
        assert A._mode_and_effort(body)["model"] == "groq/qwen/qwen3.8-27b"


def test_the_original_body_is_not_mutated():
    """The handler keeps its own copy; a normalizer that edits in place makes a
    retry pass see a different request than the first one did."""
    with A.app.test_request_context("/v1/responses"):
        body = {"model": "coding", "reasoning": {"effort": "high"}}
        A._mode_and_effort(body)
        assert body["model"] == "coding"


def test_a_malformed_reasoning_value_does_not_raise():
    with A.app.test_request_context("/v1/responses"):
        assert A._mode_and_effort({"model": "coding", "reasoning": "high"})["model"] == "auto"


def test_it_runs_before_the_swarm_dispatch():
    """model "coding" + xhigh has to reach the fan-out, and the fan-out is
    dispatched on the model id -- so the translation must already have
    happened."""
    src = open("app.py", encoding="utf-8").read()
    body = src.split("def v1_responses(", 1)[1]
    assert body.index("_mode_and_effort(") < body.index("_is_swarm_model(")


# --------------------------------------------------------------------------- #
# The catalog the picker reads
# --------------------------------------------------------------------------- #

TEMPLATE = {
    "slug": "gpt-5.6-sol", "display_name": "GPT-5.6 Sol", "description": "x",
    "priority": 1, "visibility": "list", "context_window": 400000,
    "max_context_window": 400000, "default_reasoning_level": "medium",
    "supported_reasoning_levels": [{"effort": "low", "description": "d"}],
    "base_instructions": "you are codex", "model_messages": {"a": 1},
    "shell_type": "shell_command", "supported_in_api": True,
}


def _entries():
    return A._codex_catalog_models({"models": [TEMPLATE]})


def test_there_is_one_entry_per_mode():
    slugs = [m["slug"] for m in _entries()]
    for mode in (A.MODE_ALL,) + tuple(A._mode_keys()):
        assert mode in slugs, mode


def test_the_bundled_models_are_kept():
    """A user who signs codex into their own OpenAI account must still find the
    real models -- the catalog REPLACES, so dropping them deletes them."""
    assert "gpt-5.6-sol" in [m["slug"] for m in _entries()]


def test_every_hub_entry_keeps_every_template_field():
    """The reason the template is dumped from the installed binary: a missing
    field is a codex that refuses to start."""
    hub = [m for m in _entries() if m["slug"] == "coding"][0]
    assert set(TEMPLATE).issubset(set(hub))


def test_the_effort_axis_is_the_second_screen():
    hub = [m for m in _entries() if m["slug"] == "coding"][0]
    efforts = [lvl["effort"] for lvl in hub["supported_reasoning_levels"]]
    assert efforts == ["medium", "high", "xhigh"]


def test_the_levels_say_what_they_actually_do():
    hub = [m for m in _entries() if m["slug"] == "coding"][0]
    text = " ".join(lvl["description"].lower()
                    for lvl in hub["supported_reasoning_levels"])
    assert "swarm" in text and "max" in text


def test_the_default_level_is_one_of_the_offered_ones():
    """codex renders the default; naming one that is not in the list is a
    picker with nothing selected."""
    ours = (A.MODE_ALL,) + tuple(A._mode_keys())
    for hub in [m for m in _entries() if m["slug"] in ours]:
        levels = [lvl["effort"] for lvl in hub["supported_reasoning_levels"]]
        assert hub["default_reasoning_level"] in levels, hub["slug"]


def test_the_hub_entries_sort_above_the_built_ins():
    ents = _entries()
    hub = [m for m in ents if m["slug"] == "coding"][0]
    built_in = [m for m in ents if m["slug"] == "gpt-5.6-sol"][0]
    assert hub["priority"] < built_in["priority"]


def test_the_hub_entries_are_visible():
    for m in _entries():
        if m["slug"] in (A.MODE_ALL,) + tuple(A._mode_keys()):
            assert m["visibility"] == "list", m["slug"]


def test_the_context_window_is_the_one_the_hub_states_everywhere_else():
    import agentic_chat
    hub = [m for m in _entries() if m["slug"] == "coding"][0]
    assert hub["context_window"] == agentic_chat._CODEX_CONTEXT_WINDOW


def test_the_names_read_like_a_picker():
    hub = {m["slug"]: m for m in _entries()}
    assert "uncensored" in hub["uncensored"]["display_name"].lower()
    assert hub["uncensored"]["display_name"] != "uncensored"   # a label, not an id


def test_an_empty_or_broken_dump_produces_nothing():
    """Fail-open. No template means no catalog, and codex keeps working exactly
    as it does today rather than being handed a file it may reject."""
    assert A._codex_catalog_models({}) is None
    assert A._codex_catalog_models({"models": []}) is None
    assert A._codex_catalog_models(None) is None


def test_a_template_is_chosen_from_a_visible_entry():
    """A hidden built-in can be a deprecated model with a stripped-down record."""
    dump = {"models": [dict(TEMPLATE, slug="old", visibility="hide", priority=0),
                       dict(TEMPLATE, slug="new", visibility="list", priority=5)]}
    hub = [m for m in A._codex_catalog_models(dump) if m["slug"] == "coding"][0]
    assert hub["base_instructions"] == TEMPLATE["base_instructions"]


def test_the_entries_are_deep_copies():
    ents = _entries()
    a = [m for m in ents if m["slug"] == "coding"][0]
    b = [m for m in ents if m["slug"] == "reasoning"][0]
    a["supported_reasoning_levels"][0]["effort"] = "MUTATED"
    assert b["supported_reasoning_levels"][0]["effort"] == "medium"


def test_the_result_is_serialisable():
    json.dumps({"models": _entries()})
