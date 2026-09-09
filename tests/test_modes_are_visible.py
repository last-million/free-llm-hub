"""The modes existed and nobody could see them.

REPORTED 2026-09-07, two symptoms with two different causes:

  "in /settings when i select uncensored or other mode he dont check them"
  "inside CLI i dont see the modes but just max or swarm when i do /model"

SETTINGS. The click worked -- the mode was stored, and reading it back returned
"uncensored". What failed was the display: /api/model-mode swept every provider
catalog ONE AT A TIME and took 15.5 SECONDS. The dashboard loads it alongside
two other endpoints in a Promise.all, so the buttons never got their answer and
none of them lit up. Same shape as the 23s /v1/models that had opencode
reporting "Unable to connect. Is the computer able to access the url".

CLI. opencode's `/model` picker reads the models list in its OWN config file,
never /v1/models. The hub writes that list -- in two places, both hardcoded to
("auto","best","swarm"). The modes were served correctly the whole time and
simply never appeared in the picker.
"""
import pytest

import agentic_chat as AC
import app as A
import model_categories as MC


# --------------------------------------------------------------------------- #
# The catalog sweeps are concurrent
# --------------------------------------------------------------------------- #

def test_the_settings_endpoints_do_not_sweep_one_at_a_time():
    """Three endpoints the Settings page loads together. A sequential sweep in
    any of them is what stopped the mode buttons rendering."""
    src = open("app.py", encoding="utf-8").read()
    for fn in ("api_model_categories", "api_model_mode", "api_model_blocklist"):
        i = src.index("def %s(" % fn)
        body = src[i:src.index("@app.route", i + 10)]
        assert "_prefetch_free_models(" in body, fn
        assert "for pid in _available_providers():" not in body, fn


def test_the_free_prefetch_is_actually_concurrent():
    src = open("app.py", encoding="utf-8").read()
    i = src.index("def _prefetch_free_models(")
    body = src[i:i + 1800]
    assert "ThreadPoolExecutor" in body
    assert "provider_free_models" in body


def test_it_stays_free_only(monkeypatch):
    """_auto_models honours _auto_provider_mode() and can return PAID ids --
    which is why display code calls provider_free_models directly. Routing the
    Settings tables through the auto prefetch would quietly list paid models."""
    seen = []
    monkeypatch.setattr(A, "provider_free_models", lambda pid: seen.append(pid) or ["m1"])
    monkeypatch.setattr(A, "_auto_models",
                        lambda pid: pytest.fail("display path must not use _auto_models"))
    out = A._prefetch_free_models(["pa", "pb"])
    assert out == {"pa": ["m1"], "pb": ["m1"]}
    assert sorted(seen) == ["pa", "pb"]


def test_a_failing_provider_contributes_nothing(monkeypatch):
    """Exactly what calling it directly would have given -- the old loop caught
    per-provider and continued."""
    def flaky(pid):
        if pid == "bad":
            raise RuntimeError("down")
        return ["m1"]
    monkeypatch.setattr(A, "provider_free_models", flaky)
    assert A._prefetch_free_models(["ok", "bad"]) == {"ok": ["m1"], "bad": []}


def test_no_providers_is_not_a_thread_pool(monkeypatch):
    assert A._prefetch_free_models([]) == {}


# --------------------------------------------------------------------------- #
# The CLI picker sees every mode
# --------------------------------------------------------------------------- #

def test_the_isolated_opencode_seed_lists_the_modes():
    for key in MC.CATEGORY_KEYS:
        assert key in AC._OPENCODE_HUB_MODELS, key


def test_the_seed_still_lists_the_pipelines():
    for key in ("auto", "best", "swarm"):
        assert key in AC._OPENCODE_HUB_MODELS, key


def test_swarm_keeps_its_pipeline_meaning_in_the_seed():
    """"swarm" is both a category name and the swarm PIPELINE's id. opencode
    must send the pipeline, so the pre-existing entry wins."""
    assert "several models" in AC._OPENCODE_HUB_MODELS["swarm"]["name"]


def test_every_seed_entry_has_a_label():
    for k, v in AC._OPENCODE_HUB_MODELS.items():
        assert isinstance(v, dict) and v.get("name"), k


def test_the_seed_is_derived_not_hand_written():
    """A hand-written list is how the modes went missing -- twice. The modes
    must come from the same table the router filters on, so the two cannot
    drift."""
    src = open("agentic_chat.py", encoding="utf-8").read()
    # The WHOLE function, not a fixed number of characters: a window measured in
    # bytes fails the moment someone writes a longer docstring, which says
    # nothing about whether the list is still derived.
    body = src.split("def _opencode_hub_models(", 1)[1]
    body = body[:body.index("\ndef ")]
    assert "model_categories.labels()" in body


def test_both_opencode_writers_share_one_list():
    """There are TWO writers of an opencode model list -- this seed, and
    _autofix_opencode for a terminal opencode connected from the dashboard.
    They were separate hardcoded copies and fell behind twice. app imports
    agentic_chat and not the reverse, so the list lives there and app takes it
    from there."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index("def _autofix_opencode(")
    body = src[i:i + 2500]
    assert "agentic_chat._opencode_hub_models()" in body
    assert '("auto", "best", "swarm")' not in body


# --------------------------------------------------------------------------- #
# Effort and mode read as two groups in one picker
# --------------------------------------------------------------------------- #

def test_the_effort_tiers_are_labelled_as_effort():
    """Asked for as "/model to select which mode, and /effort for auto, best,
    swarm". opencode has no /effort command and the hub cannot add one -- the
    model list is the only channel it has -- so the split is made in the names,
    which is where it CAN be made."""
    m = AC._opencode_hub_models()
    for k in ("auto", "best", "swarm"):
        assert m[k]["name"].startswith("effort:"), k


def test_the_modes_are_labelled_as_modes():
    m = AC._opencode_hub_models()
    for k in MC.CATEGORY_KEYS:
        if k in ("auto", "best", "swarm"):
            continue
        assert m[k]["name"].startswith("mode:"), k


def test_the_pipelines_are_left_out_of_the_picker():
    """crew/team/plan are dashboard pipelines. Seven of them in a picker of ten
    is noise in front of the choices a CLI actually makes. The hub still answers
    to them, so typing one by hand still works."""
    m = AC._opencode_hub_models()
    for k in ("crew", "crew-code", "crew-research", "crew-write", "crew-design",
              "team", "plan"):
        assert k not in m, k
    assert k in A._virtual_model_ids(), "the hub must still SERVE them"


def test_the_picker_stays_small():
    assert len(AC._opencode_hub_models()) <= 12


def test_both_writers_cover_the_modes():
    """Two files write an opencode model list. A mode present in one and absent
    from the other is the bug in half of the installs."""
    for key in ("uncensored", "coding", "reasoning"):
        assert key in AC._OPENCODE_HUB_MODELS, key
        assert key in A._virtual_model_ids(), key
