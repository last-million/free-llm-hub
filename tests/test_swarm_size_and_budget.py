"""A bigger swarm, drawn from the best models without draining them.

ASKED 2026-08-31: "the orchestrator should orchestrate the best model that can
orchestrate requests ... but always in the range of best models to dont exaust
good ones quickly specialy in swarm agents too and he can use more then 3 agents
in the swarm ... and orchestrator communicate between them and assembly perfect
result from them".

Three parts, and only two of them were missing.

MORE THAN 3. _SWARM_TOOL_FANOUT was a hardcoded 3. It is now a setting with a
higher default, so a swarm turn buys more opinions when the quota is there.

WITHOUT EXHAUSTING THE GOOD ONES. This is the part that makes a bigger swarm
safe rather than reckless. _swarm_rank ordered by delivery and spread providers,
but never looked at how much daily budget a provider had left -- so five slots
could all land on the same nearly-drained provider and finish it off. It now
skips a provider with almost nothing left whenever a healthy alternative exists,
which is the same reasoning the single-model router already applies through
_quota_headroom.

ASSEMBLY was already there and is untouched: the PROSE swarm runs planner ->
phases -> reviewer -> "Assemble the final deliverable from the phase outputs"
(see swarm.py). The TOOL swarm is deliberately best-of-N instead, because
merging tool calls from several models means several models editing the same
files from different assumptions -- that is not assembly, it is a conflict.
"""
from unittest import mock

import app


def _rel(default=0.8):
    return mock.patch.object(app, "_swarm_reliability", return_value=default)


def _score(mapping, default=130.0):
    return mock.patch.object(app, "_benchmark_score",
                             side_effect=lambda pid, m: mapping.get((pid, m), default))


def _headroom(mapping, default=1.0):
    return mock.patch.object(app, "_quota_headroom",
                             side_effect=lambda pid: mapping.get(pid, default))


# --------------------------------------------------------------------------- #
# More than three
# --------------------------------------------------------------------------- #

def test_the_swarm_can_field_more_than_three():
    assert app._SWARM_TOOL_FANOUT > 3


def test_the_size_is_configurable():
    """So it can be turned down on a tight quota without a code change."""
    import config
    assert config.get_flag is not None
    with mock.patch.object(app.config, "get_setting",
                           side_effect=lambda n, d=None: 6 if n == "swarm_fanout" else d):
        assert app._swarm_fanout() == 6


def test_a_silly_size_is_clamped():
    for value, expected_max in ((999, app._SWARM_FANOUT_MAX), (0, 1), (-3, 1)):
        with mock.patch.object(app.config, "get_setting",
                               side_effect=lambda n, d=None, v=value: v if n == "swarm_fanout" else d):
            got = app._swarm_fanout()
        assert 1 <= got <= app._SWARM_FANOUT_MAX, (value, got)


def test_the_candidate_pool_is_still_deeper_than_the_fanout():
    """There has to be something to choose between."""
    assert app._SWARM_TOOL_CANDIDATES > app._SWARM_TOOL_FANOUT


def test_it_really_fields_that_many():
    cands = [("p%d" % i, "m%d" % i) for i in range(10)]
    with _rel(), _score({}), _headroom({}):
        assert len(app._swarm_rank(cands)) == app._swarm_fanout()


# --------------------------------------------------------------------------- #
# ...without draining the good providers
# --------------------------------------------------------------------------- #

def test_a_nearly_drained_provider_loses_its_slot():
    """The whole point of a bigger swarm being safe: five slots must not finish
    off the one provider that still has the best models."""
    # enough healthy alternatives to fill every slot without it -- with fewer,
    # using the drained provider for the last slot is the right trade, and the
    # test below pins that case
    cands = [("drained", "top-a"), ("drained", "top-b")] + [
        ("fresh%d" % i, "m%d" % i) for i in range(1, app._SWARM_FANOUT_MAX + 2)]
    with _rel(), _score({("drained", "top-a"): 138.0, ("drained", "top-b"): 137.0}), \
            _headroom({"drained": 0.02}):
        picks = app._swarm_rank(cands)
    assert not any(p == "drained" for p, _ in picks), picks


def test_it_is_still_used_when_nothing_else_is_left():
    """Refusing to answer is worse than spending the last of a budget."""
    cands = [("drained", "a"), ("drained", "b"), ("drained", "c")]
    with _rel(), _score({}), _headroom({"drained": 0.01}):
        assert len(app._swarm_rank(cands)) == 3


def test_plenty_of_budget_changes_nothing():
    cands = [("a", "m1"), ("b", "m2"), ("c", "m3"), ("d", "m4"), ("e", "m5")]
    with _rel(), _score({("a", "m1"): 138.0}), _headroom({}):
        picks = app._swarm_rank(cands)
    assert picks[0] == ("a", "m1")


def test_strength_and_delivery_still_decide_among_healthy_ones():
    cands = [("a", "weak"), ("b", "strong")]
    with _rel(), _score({("a", "weak"): 60.0, ("b", "strong"): 138.0}), _headroom({}):
        assert app._swarm_rank(cands)[0] == ("b", "strong")


def test_providers_are_still_spread_across_the_slots():
    cands = [("nvidia", "m1"), ("nvidia", "m2"), ("nvidia", "m3"),
             ("google", "g1"), ("glm", "z1"), ("groq", "q1")]
    with _rel(), _score({}), _headroom({}):
        picks = app._swarm_rank(cands)
    assert len({p for p, _ in picks}) >= 4, picks


# --------------------------------------------------------------------------- #
# Assembly: already real, and only in the pipeline where it makes sense
# --------------------------------------------------------------------------- #

def test_the_prose_swarm_assembles_rather_than_picking_one():
    import swarm
    src = open("swarm.py", encoding="utf-8").read()
    assert "Assemble the final deliverable from the phase outputs" in src


def test_the_tool_swarm_is_best_of_n_on_purpose():
    """Merging tool calls from several models is several models editing the
    same files from different assumptions -- a conflict, not an assembly."""
    src = open("app.py", encoding="utf-8").read()
    i = src.find("def _swarm_tool_result")
    assert "best single response" in src[i:i + 900]
