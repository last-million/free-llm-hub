"""hy4 lands at the top the day a provider lists it, not at the bottom.

ASKED 2026-08-31: "check if hy4 available if yes he should be also used from top
best models cause he is gooooooooooood".

AVAILABILITY, measured against the live catalog: NOT yet. Searching all 416
discovered ids for tencent/hunyuan/hy-<n>/770b returned three entries, all hy3:

    g4f/srv_msjekdik2f3768a4ee42:tencent/hy3:free
    g4f/srv_msoxsh206cb0d89eca32:tencent/hy3-free
    kilocode/tencent/hy3:free

The model itself is real -- Tencent open-sourced Hy4 preview on 2026-08-28
(Apache 2.0, 770B MoE, 49B active, 1M context), three days before this was
asked. Terminal Bench 2.1 85.4 (past DeepSeek V4 Pro), DeepSWE 28.0 -> 64.3,
and in Tencent's own 163-expert blind eval 2.99/4.00, slightly ahead of GLM-5.3
(2.92) and Kimi K3 (2.94). No provider in this hub carries it yet.

THE BUG THIS FIXES. hy3's floor was a plain substring test --
`"hy3" in low or "hunyuan-3" in low or "tencent-hy" in low` -- with no notion
of a version. So a future hy4 matched nothing and scored 10.00: DEAD LAST, out
of ~1300 listings, never routed to. The strongest model of the family would
have been the worst-ranked thing in the hub on the day it arrived, and nobody
would have noticed, because a model that is never picked never fails either.

Same defect class as glm-5.3 tying with glm-5.2 (see test_glm53_ox_alpha.py),
and the same fix: read the version.

Nothing here asserts hy4 is REACHABLE -- it is not, today. A floor is only a
preference among models that exist; if hy4 shows up dead, the measured
machinery demotes it exactly like anything else (_note_nonanswer, the
reliability ledger, and the swarm's provider prior).
"""
import app


TOP = app._PREF_FLOORS[5]          # the claude / glm-5.3 top band


def test_hy4_would_rank_with_the_best():
    """The headline. Before this it scored 10.0."""
    assert app._benchmark_score("kilocode", "tencent/hy4") >= TOP


def test_every_spelling_of_it_is_recognised():
    for mid in ("tencent/hy4", "hy4", "hunyuan-4", "tencent/hunyuan-4",
                "tencent/hy4:free", "tencent/hy4-free", "Tencent/HY4"):
        assert app._benchmark_score("kilocode", mid) >= TOP, mid


def test_a_relayed_g4f_id_is_recognised_too():
    """g4f puts its backend in front of the real id; the floor must still see
    the model. It keeps the relay discount, which is the point of applying that
    discount after the floors."""
    relayed = "srv_msjekdik2f3768a4ee42:tencent/hy4:free"
    assert app._benchmark_score("g4f", relayed) > 100
    assert (app._benchmark_score("g4f", relayed)
            < app._benchmark_score("kilocode", "tencent/hy4"))


def test_later_versions_stay_at_the_top():
    """The whole point of reading the version instead of matching a literal."""
    assert app._benchmark_score("kilocode", "tencent/hy5") >= TOP
    assert app._benchmark_score("kilocode", "hunyuan-7") >= TOP


def test_hy3_is_left_exactly_where_it_was():
    """An explicit earlier preference put hy3 at its own floor, second only to
    the puter gpt-5.6 class. Promoting hy4 must not move it."""
    assert app._benchmark_score("kilocode", "tencent/hy3:free") == app._PREF_FLOORS[0]
    assert app._PREF_FLOORS[0] < TOP


def test_older_hunyuan_is_not_promoted():
    """hy2 and below were never in the preference list and must not arrive
    through the back door of a version-aware regex."""
    assert app._benchmark_score("kilocode", "tencent/hy2") < app._PREF_FLOORS[0]
    assert app._benchmark_score("kilocode", "hunyuan-1") < app._PREF_FLOORS[0]


def test_unrelated_models_containing_hy_are_untouched():
    """The regex must want a version number straight after the family name."""
    for mid in ("acme/hypernova-3", "acme/alchemy-7b", "openai/gpt-oss-120b"):
        assert app._benchmark_score("groq", mid) < TOP, mid


def test_the_identity_still_collapses_the_spellings():
    """Load sharing and the swarm's dedupe read this: three listings of one
    model must not read as three models."""
    ident = app._normalize_model_identity
    assert ident("tencent/hy4:free") == ident("tencent/hy4") == "hy4"
    assert ident("srv_msjekdik2f3768a4ee42:tencent/hy4:free") == "hy4"
    assert ident("tencent/hy4-free") == "hy4"
    assert ident("tencent/hy4") != ident("tencent/hy3")
