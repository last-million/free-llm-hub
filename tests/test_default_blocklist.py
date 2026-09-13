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
    assert added == len(A._DEFAULT_BLOCKED_IDENTITIES)
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
