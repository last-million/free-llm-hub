r"""Pick a model once, not once per provider -- and be able to say "only these".

REQUESTED 2026-09-09: "i want also use whitelist models that i want to use only
either by inputing there names or checkmark them, and even in blocklist ... he
should be smart to detect the same model in all providers so no need to select
same model in each provider".

WHAT EXISTED. Only a blocklist, and it stored provider-qualified ids:

    _BLOCKED_SETTING = "blocked_models"   ->  ["groq/openai/gpt-oss-120b", ...]

so switching off gpt-oss meant finding and ticking it under every provider that
serves it -- five entries for one model, and a sixth provider added tomorrow
serves it again. There was no whitelist at all.

WHAT THE HUB ALREADY HAD. _normalize_model_identity() is the hub's canonical
"same model" key and is already load-bearing in routing (penalty sharing,
same-host alternation, retry vetoes). It strips provider suffixes (':free',
'-free', ':beta'), g4f relay prefixes ('srv_xxx:'), and the vendor namespace,
because hosts rename the namespace and never the model:

    groq/openai/gpt-oss-120b   ->  gpt-oss-120b
    cerebras/gpt-oss-120b      ->  gpt-oss-120b
    tokenrouter/z-ai/glm-5.3-free -> glm-5.3

So the grouping key for "the same model everywhere" was already written,
measured and trusted. It simply had never been offered to the user.

ONE SEAM. _is_model_dead() is where a user-blocked model is already made
invisible to the pool, the chain, the model lists and the probes -- its comment
says so and says why. All three lists are enforced there, so none of those six
callers can forget one.

WHY THE WHITELIST IS STRICT WHILE A MODE IS NOT. _apply_mode returns
`kept or cands`: a mode is a preference, and a category whose models are all
rate-limited must degrade to "answer with something". A whitelist is not a
preference, it is an instruction with a list attached, and silently ignoring it
would route to exactly the models the user just excluded. It is enforced. The
protection against locking the hub out lives at the WRITE side instead, where
it can be explained.
"""
import app as A
import config
import pytest


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    p = tmp_path / "config.json"
    p.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(p))
    monkeypatch.setattr(config, "_config_path", lambda: str(p))
    config.invalidate_settings_cache()
    yield
    config.invalidate_settings_cache()


# The control gate wants this header on every /api/* write; the token is only
# demanded once one is configured, and the isolated config above has none.
_DASH = {"X-Free-LLM-Hub": "dashboard"}


@pytest.fixture
def client():
    A.app.config["TESTING"] = True
    with A.app.test_client() as c:
        yield c


# --------------------------------------------------------------------------- #
# One model, every provider
# --------------------------------------------------------------------------- #

SIBLINGS = [
    ("groq", "openai/gpt-oss-120b"),
    ("cerebras", "gpt-oss-120b"),
    ("nvidia", "openai/gpt-oss-120b"),
    ("openrouter", "openai/gpt-oss-120b:free"),
]


def test_the_siblings_really_are_one_identity():
    """The premise. If this ever stops holding, everything below is wrong."""
    idents = {A._normalize_model_identity(m) for _pid, m in SIBLINGS}
    assert len(idents) == 1, idents


def test_blocking_an_identity_blocks_it_everywhere():
    A._set_identity_blocked("gpt-oss-120b", True)
    for pid, model in SIBLINGS:
        assert A._is_model_blocked_by_user(pid, model), (pid, model)


def test_unblocking_an_identity_releases_every_provider():
    A._set_identity_blocked("gpt-oss-120b", True)
    A._set_identity_blocked("gpt-oss-120b", False)
    for pid, model in SIBLINGS:
        assert not A._is_model_blocked_by_user(pid, model)


def test_an_unrelated_model_is_untouched():
    A._set_identity_blocked("gpt-oss-120b", True)
    assert not A._is_model_blocked_by_user("groq", "qwen/qwen3.8-27b")


def test_a_provider_added_tomorrow_is_covered_too():
    """The reason to store the identity rather than five ids: the sixth host
    is blocked before anyone has heard of it."""
    A._set_identity_blocked("gpt-oss-120b", True)
    assert A._is_model_blocked_by_user("somenewhost", "openai/gpt-oss-120b")


# --------------------------------------------------------------------------- #
# The old provider-qualified list still works
# --------------------------------------------------------------------------- #

def test_the_legacy_blocklist_is_still_honoured():
    """Every existing install has one. Dropping it would silently re-enable
    every model the user had already switched off."""
    A._set_model_blocked("groq", "openai/gpt-oss-120b", True)
    assert A._is_model_blocked_by_user("groq", "openai/gpt-oss-120b")


def test_the_legacy_list_blocks_only_that_provider():
    A._set_model_blocked("groq", "openai/gpt-oss-120b", True)
    assert not A._is_model_blocked_by_user("cerebras", "gpt-oss-120b")


# --------------------------------------------------------------------------- #
# The whitelist
# --------------------------------------------------------------------------- #

def test_an_empty_whitelist_restricts_nothing():
    """Off is the default and must stay the default -- a whitelist that starts
    life empty and enforcing would take the hub down on first upgrade."""
    assert A._allowed_identities() == set()
    assert not A._is_model_blocked_by_user("groq", "qwen/qwen3.8-27b")


def test_a_whitelist_admits_what_is_on_it():
    A._set_identity_allowed("qwen3.8-27b", True)
    assert not A._is_model_blocked_by_user("groq", "qwen/qwen3.8-27b")


def test_a_whitelist_excludes_everything_else():
    A._set_identity_allowed("qwen3.8-27b", True)
    assert A._is_model_blocked_by_user("groq", "openai/gpt-oss-120b")


def test_a_whitelisted_identity_covers_every_provider():
    A._set_identity_allowed("gpt-oss-120b", True)
    for pid, model in SIBLINGS:
        assert not A._is_model_blocked_by_user(pid, model)


def test_the_blocklist_beats_the_whitelist():
    """Both lists naming the same model is a contradiction, and the safe
    reading of a contradiction is the restrictive one."""
    A._set_identity_allowed("gpt-oss-120b", True)
    A._set_identity_blocked("gpt-oss-120b", True)
    assert A._is_model_blocked_by_user("groq", "openai/gpt-oss-120b")


def test_emptying_the_whitelist_turns_it_off_again():
    A._set_identity_allowed("qwen3.8-27b", True)
    A._set_identity_allowed("qwen3.8-27b", False)
    assert not A._is_model_blocked_by_user("groq", "openai/gpt-oss-120b")


# --------------------------------------------------------------------------- #
# Enforcement reaches routing, not just the API
# --------------------------------------------------------------------------- #

def test_the_one_seam_carries_all_three_lists():
    """_is_model_dead is what the pool, the chain, the model lists and the
    probes all call. A list enforced anywhere else would be forgotten by five
    of them."""
    A._set_identity_blocked("gpt-oss-120b", True)
    assert A._is_model_dead("cerebras", "gpt-oss-120b")


def test_the_block_reason_names_the_whitelist():
    """"Model X is switched off in Settings" would be a lie, and a confusing
    one, when what actually happened is that X is not on a list."""
    A._set_identity_allowed("qwen3.8-27b", True)
    reason = A._model_block_reason("groq", "openai/gpt-oss-120b")
    assert reason and "whitelist" in reason.lower()


def test_the_block_reason_is_none_when_it_may_run():
    assert A._model_block_reason("groq", "qwen/qwen3.8-27b") is None


# --------------------------------------------------------------------------- #
# Editing what belongs to a mode
# --------------------------------------------------------------------------- #

def test_a_model_can_be_added_to_a_category():
    """CATEGORIES is a hardcoded tuple of substring patterns -- until now the
    only way to fix a miscategorised model was to edit the source and
    restart."""
    assert not A._mode_allows("coding", "groq", "some-obscure-model")
    A._set_category_member("coding", "some-obscure-model", True)
    assert A._mode_allows("coding", "groq", "some-obscure-model")


def test_a_model_can_be_removed_from_a_category():
    """codestral matches the built-in 'codestral' pattern, so this proves the
    override beats the source list rather than just adding to it."""
    assert A._mode_allows("coding", "llm7", "codestral-latest")
    A._set_category_member("coding", "codestral-latest", False)
    assert not A._mode_allows("coding", "llm7", "codestral-latest")


def test_an_override_follows_the_identity_across_providers():
    A._set_category_member("coding", "gpt-oss-120b", True)
    for pid, model in SIBLINGS:
        assert A._mode_allows("coding", pid, model), (pid, model)


def test_removal_beats_addition():
    A._set_category_member("coding", "x-model", True)
    A._set_category_member("coding", "x-model", False)
    assert not A._mode_allows("coding", "groq", "x-model")


def test_an_override_does_not_leak_into_another_category():
    A._set_category_member("coding", "some-obscure-model", True)
    assert not A._mode_allows("vision", "groq", "some-obscure-model")


def test_the_all_mode_still_allows_everything():
    A._set_category_member("coding", "codestral-latest", False)
    assert A._mode_allows(A.MODE_ALL, "llm7", "codestral-latest")


def test_overrides_survive_a_bad_value():
    """A hand-edited config must not take routing down."""
    config.set_setting(A._CATEGORY_OVERRIDE_SETTING, "not a dict")
    assert A._category_overrides() == {}
    assert A._mode_allows("coding", "llm7", "codestral-latest")


# --------------------------------------------------------------------------- #
# Vision, and the whitelist that hid it
# --------------------------------------------------------------------------- #

def test_a_vision_model_is_recognised_by_its_category_too():
    """MEASURED on this install: the hand-curated per-provider `vision_models`
    list is exact-match and had gone stale -- it recognised ONE model out of
    526 live ones. Every Gemini Flash was invisible, so an image request could
    only ever route to that single model, and when it was unavailable
    _build_chain(require_vision=True) came back EMPTY and the request 503'd
    with 32 perfectly capable models sitting there."""
    assert A._is_vision_model("google", "models/gemini-3.5-flash")


def test_the_curated_list_still_wins():
    """A provider naming a model has said something exact, and that is
    believed even if no pattern matches it."""
    import providers
    for pid, spec in providers.PROVIDERS.items():
        for model in (spec.get("vision_models") or []):
            assert A._is_vision_model(pid, model), "%s/%s" % (pid, model)
            return
    pytest.skip("no provider curates a vision list any more")


def test_a_text_model_is_still_not_a_vision_model():
    assert not A._is_vision_model("groq", "qwen/qwen3.8-27b")
    assert not A._is_vision_model("groq", "openai/gpt-oss-120b")


def test_a_broken_category_table_does_not_claim_vision(monkeypatch):
    """Fail CLOSED here, unlike _mode_allows: guessing that a model can see
    sends an image to something that cannot read it."""
    import model_categories
    monkeypatch.setattr(model_categories, "matches",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert not A._is_vision_model("google", "models/gemini-3.5-flash")


def test_a_whitelist_that_narrows_to_nothing_says_so(client=None):
    """The exact accident that hid every vision model on this install: one
    whitelist entry, added while testing, switched off 500+ models and the only
    symptom was "no vision model available"."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index('if action == "allow":')
    body = src[i:i + 1400]
    assert '"warning"' in body
    assert "uses NOTHING else" in body


# --------------------------------------------------------------------------- #
# Several at once
# --------------------------------------------------------------------------- #

def test_a_list_of_models_is_one_action(client, monkeypatch):
    """REQUESTED: "in blacklist and whitelist I want to be able to select which
    ones I want to add there or remove from there". Twelve ticked boxes as
    twelve requests means twelve rewrites of the same setting file and twelve
    chances to end up half-applied."""
    r = client.post("/api/model-identities",
                    json={"identities": ["aaa-one", "bbb-two", "ccc-three"],
                          "action": "block"}, headers=_DASH)
    assert r.status_code == 200
    blocked = r.get_json()["blocked"]
    for ident in ("aaa-one", "bbb-two", "ccc-three"):
        assert ident in blocked


def test_one_model_still_works_the_old_way(client):
    r = client.post("/api/model-identities",
                    json={"identity": "solo-model", "action": "block"},
                    headers=_DASH)
    assert r.status_code == 200
    assert "solo-model" in r.get_json()["blocked"]


def test_duplicates_in_the_list_are_harmless(client):
    r = client.post("/api/model-identities",
                    json={"identities": ["dup", "dup", "DUP"], "action": "block"},
                    headers=_DASH)
    assert r.status_code == 200
    assert r.get_json()["blocked"].count("dup") == 1


def test_an_empty_list_is_refused(client):
    r = client.post("/api/model-identities",
                    json={"identities": [], "action": "block"}, headers=_DASH)
    assert r.status_code == 400


def test_a_body_that_is_trying_to_be_an_attack_is_refused(client):
    import app as A
    r = client.post("/api/model-identities",
                    json={"identities": ["m%d" % i for i in range(A._MAX_BULK_IDENTITIES + 1)],
                          "action": "block"}, headers=_DASH)
    assert r.status_code == 400


def test_a_bulk_edit_can_target_one_conversation(client, monkeypatch):
    """"and also for each session and globally but we can customize for each
    session conversation"."""
    import agentic_chat
    monkeypatch.setattr(agentic_chat, "get_session",
                        lambda sid: {"session_id": sid, "mode": None})
    r = client.post("/api/model-identities",
                    json={"identities": ["x-one", "x-two"], "action": "block",
                          "session_id": "sess-bulk"}, headers=_DASH)
    assert r.status_code == 200
    body = r.get_json()
    assert body["scope"] == "sess-bulk"
    assert "x-one" in body["blocked"] and "x-two" in body["blocked"]


def test_removing_several_is_checked_against_what_would_be_left(client, monkeypatch):
    """Removing two models where either alone would be fine can still leave a
    whitelist that serves nothing -- so the guard runs against the state after
    ALL of them are gone, not one at a time."""
    import app as A
    monkeypatch.setattr(A, "_identity_rows",
                        lambda sid=None: [{"identity": "keep-me", "working": False},
                                          {"identity": "live-a", "working": True},
                                          {"identity": "live-b", "working": True}])
    for ident in ("keep-me", "live-a", "live-b"):
        A._set_identity_allowed(ident, True)
    r = client.post("/api/model-identities",
                    json={"identities": ["live-a", "live-b"], "action": "disallow"},
                    headers=_DASH)
    assert r.status_code == 400, "a whitelist with nothing working was allowed"
