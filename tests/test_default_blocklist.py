r"""The hub ships a blocklist so a fresh install is not stuck routing to
families the owner already found broken across every provider.

REQUESTED 2026-09-13: "check my blacklist models and blacklist them even in the
repo please, like this all users will have them in blacklist too".

Shipped by IDENTITY, never by 'pid/model'. The owner's own off-list also held
ephemeral per-provider ids the g4f relay minted for one session
("g4f/srv_mkom688d57c76d8a3542:..."); those mean nothing on another machine and
are deliberately not shipped. An identity holds for every provider that serves
the model, which is what routing matches on.

Delivered by SEEDING each install's blocked_identities setting once per
version, not by unioning at read: seeding keeps every entry editable, so the
blacklist UI can untick one and it stays off. That untickability -- and the
promise that re-seeding never undoes a user's untick -- is what these tests
pin.
"""
import pytest

import app as A


@pytest.fixture
def store(monkeypatch):
    """An in-memory config so a seed test never writes the real settings file."""
    data = {}

    def get_setting(key, default=None):
        return data.get(key, default)

    def set_setting(key, value):
        data[key] = value

    monkeypatch.setattr(A.config, "get_setting", get_setting)
    monkeypatch.setattr(A.config, "set_setting", set_setting)
    return data


# --------------------------------------------------------------------------- #
# The shipped list is identities, and never the ephemeral per-provider ids
# --------------------------------------------------------------------------- #

def test_the_defaults_are_bare_identities_not_provider_ids():
    for ident in A._DEFAULT_BLOCKED_IDENTITIES:
        assert "/" not in ident, ident          # not 'pid/model'
        assert ident == ident.strip().lower()   # already normalised
    # The families the owner named are there.
    assert {"gpt-oss", "gpt-oss-120b", "nemotron-3-super",
            "ling-3.0-tiny", "mimo-v2.5"} <= A._DEFAULT_BLOCKED_IDENTITIES


# --------------------------------------------------------------------------- #
# A fresh install inherits them
# --------------------------------------------------------------------------- #

def test_a_fresh_install_gets_every_default(store):
    added = A._seed_default_blocks()
    assert added == len(A._DEFAULT_BLOCKED_IDENTITIES) + len(A._DEFAULT_BLOCKED_MODELS)
    assert A._DEFAULT_BLOCKED_IDENTITIES <= A._blocked_identities()
    # The whole shipped set is recorded as offered.
    assert A._DEFAULT_BLOCKED_IDENTITIES <= set(store[A._DEFAULT_BLOCKS_SEEDED_SETTING])


def test_a_shipped_default_actually_blocks_routing(store):
    A._seed_default_blocks()
    with A.app.test_request_context("/v1/chat/completions"):
        # reads the identity; any provider serving gpt-oss is off.
        assert A._is_model_blocked_by_user("groq", "openai/gpt-oss-120b")


# --------------------------------------------------------------------------- #
# ...once. And it never fights the user.
# --------------------------------------------------------------------------- #

def test_seeding_is_idempotent(store):
    assert A._seed_default_blocks() > 0
    assert A._seed_default_blocks() == 0          # version already recorded
    assert A._seed_default_blocks() == 0


def test_a_user_untick_is_not_undone(store):
    """A user removes one shipped family; the seed has already run, so it must
    not come back."""
    A._seed_default_blocks()
    A._set_identity_blocked("gpt-oss", False)     # untick it in the UI
    assert "gpt-oss" not in A._blocked_identities()
    A._seed_default_blocks()                      # a later boot
    assert "gpt-oss" not in A._blocked_identities(), "untick must survive re-seed"


def test_a_new_release_delivers_new_defaults_without_reblocking_old_unticks(store, monkeypatch):
    A._seed_default_blocks()
    A._set_identity_blocked("gpt-oss", False)     # user does not want this one
    # A future release ADDS a family to the shipped set.
    monkeypatch.setattr(A, "_DEFAULT_BLOCKED_IDENTITIES",
                        A._DEFAULT_BLOCKED_IDENTITIES | {"brand-new-broken"})
    A._seed_default_blocks()
    assert "brand-new-broken" in A._blocked_identities()   # the new one arrives
    assert "gpt-oss" not in A._blocked_identities()        # the untick still holds


def test_a_users_own_block_is_preserved(store):
    A._set_identity_blocked("something-i-hate", True)
    A._seed_default_blocks()
    got = A._blocked_identities()
    assert "something-i-hate" in got
    assert A._DEFAULT_BLOCKED_IDENTITIES <= got


# --------------------------------------------------------------------------- #
# The owner's own blocklist ships as the default (2026-10-07)
# --------------------------------------------------------------------------- #

def test_the_owners_blocklist_is_shipped():
    assert {"apodex-1.1-mini", "gemma-4-26b-a4b-it", "gemma-4-31b-it",
            "granite-3.0-3b-a800m-instruct", "granite-3.0-8b-instruct",
            "granite-34b-code-instruct", "granite-8b-code-instruct",
            "lfm-2.5-2.6b", "poolside-laguna-s-2.1",
            "step-3.7-flash"} <= A._DEFAULT_BLOCKED_IDENTITIES
    assert "dahl/MiniMaxAI/MiniMax-M2.7" in A._DEFAULT_BLOCKED_MODELS


def test_shipped_model_blocks_are_offered_once(store):
    A._seed_default_blocks()
    assert A._DEFAULT_BLOCKED_MODELS <= A._blocked_models()
    # The owner unticks one: the next boot does not put it back.
    store[A._BLOCKED_SETTING] = []
    assert A._seed_default_blocks() == 0
    assert "dahl/MiniMaxAI/MiniMax-M2.7" not in A._blocked_models()


def test_model_blocks_keep_the_users_own_entries(store):
    store[A._BLOCKED_SETTING] = ["groq/some/model"]
    A._seed_default_model_blocks()
    assert {"groq/some/model"} | set(A._DEFAULT_BLOCKED_MODELS) == A._blocked_models()


# --------------------------------------------------------------------------- #
# The shipped list vs the owner's live blocklist (checked 2026-10-23)
# --------------------------------------------------------------------------- #

def test_the_owners_newer_blocks_ship_and_the_current_nemotron_does_not():
    # generic identities the owner blocked that the list did not hold yet
    assert {"codellama-70b", "nemotron-3-super-120b-a12b"} <= A._DEFAULT_BLOCKED_IDENTITIES
    # the 3.5 chat variants were un-shipped; the safety classifier stays off
    assert "nemotron-3.5-lightning-30b" not in A._DEFAULT_BLOCKED_IDENTITIES
    assert "nemotron-3.5-lightning-30b-a3b" not in A._DEFAULT_BLOCKED_IDENTITIES
    assert "nemotron-3.5-content-safety" in A._DEFAULT_BLOCKED_IDENTITIES


def test_nothing_install_specific_is_shipped():
    for ident in A._DEFAULT_BLOCKED_IDENTITIES:
        assert not ident.startswith(("srv_", "pa:")), ident     # g4f backend ids
        assert ":" not in ident, ident


# --------------------------------------------------------------------------- #
# Retiring a shipped default (nemotron 3.5 chat variants, 2026-10-23)
# --------------------------------------------------------------------------- #

RETIRED_A = "nemotron-3.5-lightning-30b"
RETIRED_B = "nemotron-3.5-lightning-30b-a3b"
ENC_KEY = "enc.v1:AAAA-not-a-real-key"


@pytest.fixture
def writes(store, monkeypatch):
    """`store`, plus a log of every setting name written to it."""
    log = []
    real = A.config.set_setting

    def spy(key, value):
        log.append(key)
        real(key, value)

    monkeypatch.setattr(A.config, "set_setting", spy)
    return log


def _existing_install(store, *, extra_blocked=(), seeded_extra=()):
    """An install that booted the PREVIOUS release: it was offered the old
    shipped set (retired ids included), holds all of it in blocked_identities,
    and has an API key in its providers block."""
    old_shipped = set(A._DEFAULT_BLOCKED_IDENTITIES) | A._RETIRED_DEFAULT_BLOCKS
    store[A._BLOCKED_IDENTITY_SETTING] = sorted(old_shipped | set(extra_blocked))
    store[A._DEFAULT_BLOCKS_SEEDED_SETTING] = sorted(old_shipped | set(seeded_extra))
    store["providers"] = {"nvidia": {"api_key": ENC_KEY, "enabled": True}}
    return old_shipped


def test_an_existing_install_loses_exactly_the_retired_seeded_entries(store):
    _existing_install(store, extra_blocked=["something-i-hate"])
    before = A._blocked_identities()
    A._seed_default_blocks()
    got = A._blocked_identities()
    assert RETIRED_A not in got and RETIRED_B not in got
    assert before - got == A._RETIRED_DEFAULT_BLOCKS            # nothing else left
    assert "something-i-hate" in got                            # the user's own stays
    assert "nemotron-3.5-content-safety" in got                 # the classifier stays
    assert "nemotron-3-super-120b-a12b" in got                  # older gen stays
    assert A._RETIRED_DEFAULT_BLOCKS <= set(store[A._RETIRED_BLOCKS_DONE_SETTING])


def test_an_entry_the_user_added_themselves_is_kept(store):
    """Not in the seeded snapshot = the hub never offered it = the user's."""
    _existing_install(store)
    store[A._DEFAULT_BLOCKS_SEEDED_SETTING] = sorted(
        set(store[A._DEFAULT_BLOCKS_SEEDED_SETTING]) - {RETIRED_B})
    A._seed_default_blocks()
    got = A._blocked_identities()
    assert RETIRED_B in got                                     # user-added: kept
    assert RETIRED_A not in got                                 # hub-seeded: gone


def test_the_owners_own_3_5_blocks_are_not_touched(store):
    """nemotron-3.5-lightning / -super-vl-120b-a12b are hand-ticked, never
    shipped: the step must leave them (the owner unticks them in the UI)."""
    _existing_install(store, extra_blocked=["nemotron-3.5-lightning",
                                            "nemotron-3.5-super-vl-120b-a12b"])
    A._seed_default_blocks()
    got = A._blocked_identities()
    assert {"nemotron-3.5-lightning", "nemotron-3.5-super-vl-120b-a12b"} <= got


def test_an_untick_is_not_undone_by_the_step_or_the_seed(store):
    _existing_install(store)
    A._set_identity_blocked(RETIRED_A, False)                   # already unticked
    A._seed_default_blocks()
    A._seed_default_blocks()
    assert RETIRED_A not in A._blocked_identities()
    assert RETIRED_A not in A._DEFAULT_BLOCKED_IDENTITIES       # nothing re-adds it


def test_a_tick_made_after_the_step_ran_survives_later_boots(store):
    _existing_install(store)
    A._seed_default_blocks()                                    # the step runs
    A._set_identity_blocked(RETIRED_B, True)                    # user blocks it again
    A._seed_default_blocks()
    A._seed_default_blocks()
    assert RETIRED_B in A._blocked_identities()


def test_the_step_is_idempotent_and_writes_nothing_the_second_time(writes, store):
    _existing_install(store)
    assert A._retire_default_blocks() == 2
    writes.clear()
    assert A._retire_default_blocks() == 0
    assert writes == []
    A._seed_default_blocks()                                    # settles the other seeds
    snapshot = {k: (list(v) if isinstance(v, list) else v) for k, v in store.items()}
    A._seed_default_blocks()                                    # nothing new to ship
    A._seed_default_blocks()
    assert {k: (list(v) if isinstance(v, list) else v)
            for k, v in store.items()} == snapshot


def test_the_step_writes_only_the_two_blocklist_settings_and_never_keys(writes, store):
    _existing_install(store)
    keys_before = dict(store["providers"])
    assert A._retire_default_blocks() == 2
    assert set(writes) == {A._BLOCKED_IDENTITY_SETTING, A._RETIRED_BLOCKS_DONE_SETTING}
    assert store["providers"] == keys_before                    # API keys untouched
    assert store["providers"]["nvidia"]["api_key"] == ENC_KEY
    # and the full boot step still only touches blocklist-related settings
    A._seed_default_blocks()
    assert set(writes) <= {
        A._BLOCKED_IDENTITY_SETTING, A._RETIRED_BLOCKS_DONE_SETTING,
        A._DEFAULT_BLOCKS_SEEDED_SETTING, A._BLOCKED_SETTING,
        A._DEFAULT_MODEL_BLOCKS_SEEDED_SETTING}
    assert store["providers"]["nvidia"]["api_key"] == ENC_KEY


def test_a_fresh_install_records_the_step_and_blocks_no_3_5_chat_model(store):
    A._seed_default_blocks()
    assert not (A._RETIRED_DEFAULT_BLOCKS & A._blocked_identities())
    assert A._RETIRED_DEFAULT_BLOCKS <= set(store[A._RETIRED_BLOCKS_DONE_SETTING])


def test_the_step_runs_even_when_the_boot_has_no_new_shipped_ids(store):
    """It sits above _seed_default_blocks' early return: an install whose seeded
    snapshot already covers every shipped id still gets the take-back."""
    _existing_install(store)
    assert set(store[A._DEFAULT_BLOCKS_SEEDED_SETTING]) >= A._DEFAULT_BLOCKED_IDENTITIES
    A._seed_default_blocks()
    assert RETIRED_A not in A._blocked_identities()


def test_a_retired_id_that_is_shipped_again_is_neither_removed_nor_marked(store, monkeypatch):
    _existing_install(store)
    monkeypatch.setattr(A, "_DEFAULT_BLOCKED_IDENTITIES",
                        A._DEFAULT_BLOCKED_IDENTITIES | {RETIRED_A})
    assert A._retire_default_blocks() == 1                      # only B goes
    assert RETIRED_A in A._blocked_identities()
    assert RETIRED_A not in set(store[A._RETIRED_BLOCKS_DONE_SETTING])


def test_the_step_never_raises(monkeypatch):
    sunk = []

    def boom(*a, **k):
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(A.config, "get_setting", boom)
    monkeypatch.setattr(A.config, "set_setting", lambda k, v: sunk.append(k))
    assert A._retire_default_blocks() == 0
    A._seed_default_blocks()                                    # must not raise either
