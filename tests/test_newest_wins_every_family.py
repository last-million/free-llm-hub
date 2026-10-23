"""A new release of ANY family ranks just above the one it replaces.

Owner (2026-10-23): "the hub should detect new models automatically and always
use the newest ones, because of course they are better than the previous ones".
The known strong families already did (`_STRONG_ROOTS`, `modelrank`,
`benchmarks.py`). Every other family did not: Inception's new `mercury-3` met the
"mercury" Tier-C substring (26) beside its predecessor's Artificial Analysis 61.5
and would have been the worst Mercury in the hub until a board listed it.

The rule (`_generic_predecessor_score`): a model with NO data of its own -- no AA
row, only a family-table default or the unknown 10 -- inherits the score of the
newest LOWER version of the same stem and tail that is reachable in the fleet and
has an AA row, plus a small bump. Boards always win over inheritance.
"""
import pytest

import app as A


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(A.config, "get_flag", lambda k, d=None: d)

    def setup(aa, fleet):
        monkeypatch.setattr(A, "_aa_scores", dict(aa))
        monkeypatch.setattr(A, "_rank_fleet_ids", lambda: list(fleet))
        A._gv_reset()
        A._rank_reset()
    yield setup
    A._gv_reset()
    A._rank_reset()


def s(model, pid="x"):
    return A._benchmark_score(pid, model)


MERCURY_FLEET = ["mercury-2", "mercury-2.5", "mercury-3", "mercury-decide"]


def test_mercury_3_ranks_just_above_mercury_2_5(world):
    world({"mercury2": 60.0, "mercury25": 61.5}, MERCURY_FLEET)
    m2, m25, m3 = s("mercury-2"), s("mercury-2.5"), s("mercury-3")
    assert m2 < m25 < m3
    assert m3 - m25 <= A._GV_BUMP_CAP + 1e-9          # a bump, never a leap
    assert m3 > 55                                    # not the legacy Tier-C 26


def test_the_vendor_joined_cache_keys_still_find_the_plain_ids(world):
    # The cache on disk keys Mercury as 'inceptionmercury25' (OpenRouter's
    # 'inception/mercury-2.5'); Inception's own API says 'mercury-2.5'.
    world({"inceptionmercury2": 60.0, "inceptionmercury25": 61.5}, MERCURY_FLEET)
    assert A._aa_score_for("mercury-2") == 60.0
    assert A._aa_score_for("mercury-2.5") == 61.5
    assert A._aa_score_for("inception/mercury-2.5") == 61.5
    assert s("mercury-3") > s("mercury-2.5") > s("mercury-2")


def test_a_family_nobody_has_heard_of_follows_the_same_rule(world):
    world({"zorblax1": 40.0}, ["zorblax-1", "zorblax-2"])
    old, new = s("zorblax-1"), s("zorblax-2")
    assert old > 30                                   # its own AA row
    assert 0 < new - old <= A._GV_BUMP_CAP + 1e-9


def test_the_bump_grows_with_the_version_step_but_is_capped(world):
    world({"zorblax1": 40.0}, ["zorblax-1", "zorblax-1.2", "zorblax-9"])
    base = s("zorblax-1")
    small, big = s("zorblax-1.2") - base, s("zorblax-9") - base
    assert 0 < small < big <= A._GV_BUMP_CAP + 1e-9


def test_a_different_tail_is_a_different_product(world):
    # coder / flash / a size are other products: never inherit across them.
    world({"zorblax1coder": 40.0}, ["zorblax-1-coder", "zorblax-2"])
    assert s("zorblax-2") == 10


def test_no_predecessor_in_the_fleet_means_no_inheritance(world):
    world({"zorblax1": 40.0}, ["zorblax-2"])           # zorblax-1 is not reachable
    assert s("zorblax-2") == 10


def test_a_listed_model_keeps_its_own_board_number(world):
    # AA lists the newer one LOWER than its predecessor: the board wins.
    world({"zorblax1": 40.0, "zorblax2": 30.0}, ["zorblax-1", "zorblax-2"])
    assert s("zorblax-2") < s("zorblax-1")


def test_an_older_version_is_never_raised_by_a_newer_one(world):
    world({"zorblax2": 40.0}, ["zorblax-1", "zorblax-2"])
    assert s("zorblax-1") == 10


def test_the_rank_free_scoring_pass_is_left_alone(world):
    # modelrank's anchor build scores the fleet with version ordering OFF.
    world({"zorblax1": 40.0}, ["zorblax-1", "zorblax-2"])
    assert A._benchmark_score("", "zorblax-2", _rank=False) == 10


def test_relay_prefixes_and_vendor_paths_do_not_hide_the_family(world):
    world({"zorblax1": 40.0}, ["zorblax-1"])
    for mid in ("zorblax-2", "acme/zorblax-2", "srv_ab12:acme/zorblax-2", "zorblax-2:free"):
        assert s(mid) > s("zorblax-1"), mid


def test_the_switch_off_restores_the_old_scores(world, monkeypatch):
    world({"mercury2": 60.0, "mercury25": 61.5}, MERCURY_FLEET)
    monkeypatch.setattr(A.config, "get_flag",
                        lambda k, d=None: False if k == "newest_wins" else d)
    A._gv_reset()
    assert s("mercury-3") == 26                        # the Tier-C table default


def test_strong_known_families_are_untouched(world):
    world({"mercury2": 60.0}, MERCURY_FLEET + ["glm-5.3-flash", "glm-5.2-flash"])
    before = {m: s(m) for m in ("glm-5.3-flash", "qwen3.8-27b", "claude-opus-5.5")}
    world({}, [])
    assert {m: s(m) for m in before} == before


def test_garbage_never_raises(world):
    world({"zorblax1": 40.0}, ["zorblax-1", None, 7, "", "///"])
    assert A._generic_predecessor_score(None) is None
    assert A._generic_predecessor_score("") is None
    assert A._generic_predecessor_score("3.5") is None
