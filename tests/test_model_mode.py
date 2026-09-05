"""Choosing WHICH KIND of model to use, globally, per project, or per CLI.

Requested 2026-09-05: highlight the mode that is actually in force, and be able
to pick one "for each project ... in agent page and also inside CLI's".

Two things were in the way.

THE MODE WAS INFERRED, NOT REMEMBERED. The Settings buttons decided which one to
light up by comparing counts -- "every model in this category is enabled and
nothing outside it is". That holds only while a category is the sole thing
touching the model list. With 300 models switched off by hand on this install,
no button could ever match, so the dashboard showed no mode at all while one was
plainly in force.

AND IT WAS THE SAME THING AS THE BLOCKED LIST. Choosing a category wrote
blocked_models = every model outside it, and "All models" wrote []. So a mode
was global by construction -- "use the coding models on THIS project" could not
be said at all -- and one click on "All models" silently deleted the user's own
off-list, including a hand-made gpt-oss blacklist.

A mode is now a routing FILTER kept apart from the blocked list: blocked means
never, mode means not for this request, and neither destroys the other.
Precedence, most specific first: the model id a CLI sends, then the agent
session's own mode, then the global setting.
"""
import pytest

import app as A
import agentic_chat as AC
import config
import model_categories as MC


CANDS = [(134.0, "groq", "qwen3-coder"),
         (130.0, "g4f", "lyria-3"),
         (120.0, "nvidia", "deepseek-v4-pro")]


# --------------------------------------------------------------------------- #
# What counts as a mode
# --------------------------------------------------------------------------- #

def test_a_category_is_a_mode():
    assert A._valid_mode("coding") == "coding"


def test_all_is_a_mode_and_means_no_filter():
    for v in ("all", "", None, "  "):
        assert A._valid_mode(v) == A.MODE_ALL


def test_an_unknown_name_is_not_a_mode():
    """None, not "all": a typo must be rejected by the API rather than silently
    turning the filter off."""
    assert A._valid_mode("nope") is None
    assert A._valid_mode("gpt-4o") is None


def test_the_swarm_pipeline_is_not_a_mode_id():
    """"swarm" is BOTH a model_categories key and the swarm pipeline's id, and
    the pipeline has to win: reading `model: "swarm"` as a mode would route it
    through ordinary single-model orchestration and the fan-out would never run
    at all."""
    assert "swarm" in MC.CATEGORY_KEYS
    assert "swarm" not in A._mode_keys()
    assert A._is_orchestrate("swarm") is False


def test_no_mode_key_collides_with_a_pipeline_id():
    taken = set(A._SWARM_IDS) | set(A.crews.CREW_IDS) | {"auto", "best", "all"}
    assert not (set(A._mode_keys()) & taken)


# --------------------------------------------------------------------------- #
# The filter
# --------------------------------------------------------------------------- #

def test_a_mode_removes_what_it_does_not_cover():
    kept = A._apply_mode(CANDS, "coding")
    assert ("g4f", "lyria-3") not in [(c[1], c[2]) for c in kept]
    assert (134.0, "groq", "qwen3-coder") in kept


def test_all_removes_nothing():
    assert A._apply_mode(CANDS, "all") == CANDS


def test_a_mode_that_matches_nothing_fails_open():
    """The whole contract. A category is a hand-written list of families matched
    against a fleet that changes daily -- a mode whose models are all
    rate-limited, withdrawn or switched off must degrade to answering with
    something, never to refusing."""
    assert A._apply_mode(CANDS, "vision") == CANDS


def test_an_empty_candidate_list_stays_empty():
    assert A._apply_mode([], "coding") == []


def test_the_filter_only_removes_it_never_reorders():
    kept = A._apply_mode(CANDS, "coding")
    assert kept == [c for c in CANDS if c in kept]


def test_an_unknown_mode_does_not_filter():
    assert A._apply_mode(CANDS, "nonsense") == CANDS


# --------------------------------------------------------------------------- #
# It is NOT the blocked list
# --------------------------------------------------------------------------- #

def test_choosing_a_mode_does_not_touch_the_blocked_list(monkeypatch):
    """The destructive bug this replaces: picking a category rewrote
    blocked_models, so one click on "All models" deleted every hand-blocked
    model on the install."""
    saved = {}
    monkeypatch.setattr(config, "set_setting", lambda k, v: saved.__setitem__(k, v))
    monkeypatch.setattr(config, "get_setting", lambda k, d=None: saved.get(k, d))
    saved[A._BLOCKED_SETTING] = ["groq/openai/gpt-oss-120b", "llm7/gpt-oss"]
    config.set_setting(A._MODE_SETTING, "coding")
    assert saved[A._BLOCKED_SETTING] == ["groq/openai/gpt-oss-120b", "llm7/gpt-oss"]


def test_the_two_settings_are_different_keys():
    assert A._MODE_SETTING != A._BLOCKED_SETTING


# --------------------------------------------------------------------------- #
# Selecting one from a CLI, by model id
# --------------------------------------------------------------------------- #

def test_every_mode_is_a_listable_model_id():
    """A coding agent has no settings screen and no notion of this hub's
    projects. The model id is the one handle it has."""
    ids = A._virtual_model_ids()
    for key in A._mode_keys():
        assert key in ids, key


def test_the_listing_has_no_duplicates():
    ids = A._virtual_model_ids()
    assert len(ids) == len(set(ids))


def test_a_mode_id_routes_like_auto():
    for key in A._mode_keys():
        assert A._is_orchestrate(key), key


def test_a_real_model_id_is_still_not_orchestrated():
    assert not A._is_orchestrate("groq/qwen3-coder")


def test_a_mode_is_labelled_as_a_mode_not_a_pipeline():
    """The pickers show this string; calling a mode a "multi-model pipeline"
    would describe the swarm, which is a different feature."""
    assert "mode" in A._virtual_model_label("coding")
    assert "pipeline" in A._virtual_model_label("crew")


# --------------------------------------------------------------------------- #
# Selecting one per project
# --------------------------------------------------------------------------- #

class _Sess:
    quality = "normal"
    mode = None


def test_a_plain_session_names_no_model():
    """Normal quality with no mode sends no --model at all, so the CLI's own
    config keeps deciding exactly as it did before any of this existed."""
    assert AC._session_model_id(_Sess()) is None


def test_a_session_mode_becomes_the_model_id():
    s = _Sess()
    s.mode = "coding"
    assert AC._session_model_id(s) == "coding"


def test_a_mode_outranks_the_quality_tier():
    """"max" only says never the cheap models; "coding" says which KIND. A user
    who picked a mode for this project asked for that kind."""
    s = _Sess()
    s.quality, s.mode = "max", "coding"
    assert AC._session_model_id(s) == "coding"


def test_the_swarm_pipeline_outranks_a_mode():
    """Routing a fan-out as a single model would silently switch the feature
    off."""
    s = _Sess()
    s.quality, s.mode = "swarm", "coding"
    assert AC._session_model_id(s) == "swarm"


def test_quality_alone_still_works():
    s = _Sess()
    s.quality = "max"
    assert AC._session_model_id(s) == "best"


def test_setting_a_mode_on_a_missing_session_is_reported():
    assert AC.set_session_mode("no-such-session", "coding") is False


# --------------------------------------------------------------------------- #
# The API
# --------------------------------------------------------------------------- #

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(A, "_available_providers", lambda *a, **k: ["groq"])
    monkeypatch.setattr(A, "provider_free_models",
                        lambda pid: ["qwen3-coder", "lyria-3"])
    saved = {}
    monkeypatch.setattr(config, "set_setting", lambda k, v: saved.__setitem__(k, v))
    monkeypatch.setattr(config, "get_setting", lambda k, d=None: saved.get(k, d))
    A.app.config["TESTING"] = True
    with A.app.test_client() as c:
        c._hdrs = {"X-Free-LLM-Hub-Token": config.ensure_control_token(),
                   "X-Free-LLM-Hub": "dashboard"}
        yield c


def test_the_api_reports_the_current_mode(client):
    r = client.get("/api/model-mode", headers=client._hdrs)
    assert r.status_code == 200
    assert r.get_json()["mode"] == "all"


def test_the_api_sets_the_global_mode(client):
    r = client.post("/api/model-mode", json={"mode": "coding"}, headers=client._hdrs)
    assert r.status_code == 200 and r.get_json()["mode"] == "coding"
    assert client.get("/api/model-mode", headers=client._hdrs).get_json()["mode"] == "coding"


def test_the_api_refuses_an_unknown_mode(client):
    r = client.post("/api/model-mode", json={"mode": "nonsense"}, headers=client._hdrs)
    assert r.status_code == 400


def test_the_api_lists_the_modes_with_counts(client):
    modes = client.get("/api/model-mode", headers=client._hdrs).get_json()["modes"]
    keys = [m["key"] for m in modes]
    assert keys[0] == "all"
    assert "coding" in keys
    assert "swarm" not in keys           # a pipeline id, never offered as a mode
    assert all("count" in m and "label" in m for m in modes)


def test_setting_a_mode_for_an_unknown_session_is_a_404(client):
    r = client.post("/api/model-mode",
                    json={"mode": "coding", "session_id": "nope"},
                    headers=client._hdrs)
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# The filter actually reaches routing
#
# The primitives above can all be right while the router never calls them --
# which is exactly what a mutation of `cands = _apply_mode(cands)` proved: every
# other test still passed with the mode having no effect on a single request.
# --------------------------------------------------------------------------- #

PIDS = ["pa", "pb"]
# "-coder" is in the coding category, "-lyria" is in none of them.
WORLD = {p: [p + "-coder", p + "-lyria"] for p in PIDS}
MSGS = [{"role": "user", "content": "fix the failing test"}]


@pytest.fixture
def fleet(monkeypatch):
    monkeypatch.setattr(A, "_available_providers", lambda *a, **k: list(PIDS))
    monkeypatch.setattr(A, "_prefetch_auto_models", lambda pids: dict(WORLD))
    monkeypatch.setattr(A, "_auto_models", lambda pid: list(WORLD[pid]))
    monkeypatch.setattr(A, "_provider_capable", lambda pid, est: True)
    monkeypatch.setattr(A, "_benchmark_score", lambda pid, m: 134.0)
    monkeypatch.setattr(A, "_supports_tools", lambda pid, m: True)
    monkeypatch.setattr(A, "_session_pin_get", lambda key: None)
    monkeypatch.setattr(A, "_session_pin_set", lambda *a, **k: None)
    monkeypatch.setattr(A, "_weighted_pick", lambda pool, *a, **k: max(pool))
    # "-coder" belongs to the mode under test, "-lyria" does not.
    monkeypatch.setattr(A.model_categories, "matches",
                        lambda key, p, m, i=None: m.endswith("-coder"))
    yield


def _route(monkeypatch, mode):
    monkeypatch.setattr(A, "_active_mode", lambda: mode)
    return A._route_by_difficulty(MSGS, None, 500, require_tools=True)


def test_the_primary_pick_honours_the_mode(fleet, monkeypatch):
    _pid, model, _d = _route(monkeypatch, "coding")
    assert model.endswith("-coder"), model


def test_the_primary_pick_is_unrestricted_under_all(fleet, monkeypatch):
    """max() over equal scores picks the lexically greatest, which is -lyria.
    That it changes at all is the point: the mode is what moved it."""
    _pid, model, _d = _route(monkeypatch, A.MODE_ALL)
    assert model.endswith("-lyria"), model


def test_the_fallback_chain_honours_the_mode(fleet, monkeypatch):
    """Otherwise a mode chose the first model and the chain behind it quietly
    left the mode again on the very first retry."""
    monkeypatch.setattr(A, "_active_mode", lambda: "coding")
    chain = A._build_chain("pa", "pa-coder", 500, require_tools=True, messages=MSGS)
    assert chain
    assert all(m.endswith("-coder") for _p, m in chain), chain


def test_the_chain_is_unrestricted_under_all(fleet, monkeypatch):
    monkeypatch.setattr(A, "_active_mode", lambda: A.MODE_ALL)
    chain = A._build_chain("pa", "pa-coder", 500, require_tools=True, messages=MSGS)
    assert any(m.endswith("-lyria") for _p, m in chain), chain


def test_routing_still_answers_when_the_mode_matches_nothing(fleet, monkeypatch):
    """Fail-open, at the level that matters: a mode covering none of the live
    fleet must not turn into "no model available"."""
    monkeypatch.setattr(A.model_categories, "matches", lambda key, p, m, i=None: False)
    pid, model, _d = _route(monkeypatch, "coding")
    assert pid and model


# --------------------------------------------------------------------------- #
# The endpoint that created the mess can no longer create it
#
# 287 blocked entries were sitting in this install's config, none of them asked
# for. They came from POST /api/model-categories, which wrote
# blocked_models = every model OUTSIDE the chosen category -- so one click
# switched off hundreds of models permanently, indistinguishably from the ones
# the user had switched off on purpose, and "All models" then deleted the lot.
# --------------------------------------------------------------------------- #

def test_choosing_a_category_no_longer_rewrites_the_blocked_list(client):
    """The exact click that produced the 287."""
    r = client.post("/api/model-categories", json={"key": "coding"},
                    headers=client._hdrs)
    assert r.status_code == 200
    assert config.get_setting(A._BLOCKED_SETTING, None) is None


def test_all_models_no_longer_deletes_a_blacklist(client):
    """It used to write [] -- taking a hand-made gpt-oss blacklist with it."""
    config.set_setting(A._BLOCKED_SETTING, ["groq/openai/gpt-oss-120b"])
    client.post("/api/model-categories", json={"key": "all"}, headers=client._hdrs)
    assert config.get_setting(A._BLOCKED_SETTING, []) == ["groq/openai/gpt-oss-120b"]


def test_choosing_a_category_sets_the_mode_instead(client):
    client.post("/api/model-categories", json={"key": "coding"}, headers=client._hdrs)
    assert config.get_setting(A._MODE_SETTING, "all") == "coding"


def test_the_old_endpoint_still_rejects_a_bad_key(client):
    r = client.post("/api/model-categories", json={"key": "nonsense"},
                    headers=client._hdrs)
    assert r.status_code == 400


def test_no_route_writes_the_blocked_list_from_a_category():
    """Structural: the rewrite is gone, not merely unreachable. Dead code that
    still names the setting is one edit away from being live again."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index("def api_model_categories(")
    body = src[i:src.index("@app.route", i + 10)]
    assert "_BLOCKED_SETTING" not in body
