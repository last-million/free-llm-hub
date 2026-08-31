"""The newest top model ranks highest AND is allowed to lead a build.

ASKED 2026-08-31: "glm 5.3 is good man but of course if new verison of it should
be used more of ocurse and have higher priority".

Two separate things were wrong, and neither was the ranking table being wrong
about glm-5.3 -- it already scored 138, joint top.

1. NEWER DID NOT MEAN HIGHER. glm-5.3, 5.4, 6 and 7 all scored exactly 138.00,
   and hy4/hy5/hy6 likewise. The top-band floor was flat, so once a family was
   in the band, its next release ranked no higher than the one it replaced.
   Below the band the hub already scales by version (qwen 3.8 -> 134.08, 4.0 ->
   134.10; kimi k3 -> 134.8, k4 -> 134.9); the band itself did not.

2. IT COULD NOT LEAD A BUILD ANYWAY. Agentic routing did
       _proven = [c for c in agentic if _is_tool_proven(c[2])]
       _pool   = _proven or agentic
   so whenever ANY allowlisted model was available the pool was ONLY those --
   and the allowlist is ('gpt-oss', 'nemotron', 'gemini-3', 'mistral-medium'),
   written from July 2026 evidence. Measured 2026-08-31: a hard build routed to
   qwen3.8 at 134.1 while glm-5.3 at 138 was not in the chain at all.

   That gate was right when it was written -- three separate runs had produced
   three different silent tool-dialect failures, zero files each time, and
   nothing downstream caught them. Since then the hub learned to: reject a tool
   call typed out as prose, reject a turn that only announced work, mark a
   non-answering id dead, and demote by measured delivery. The failure the
   allowlist guards is now caught after the fact, so the gate can stop
   excluding the models the user actually wants.

   Widened, not removed: a model still has to be allowlisted OR in the top
   band. An unproven mid-tier model is still kept out of build work.
"""
import app


TOP = app._PREF_FLOORS[5]


def _s(model, pid="tokenrouter"):
    return app._benchmark_score(pid, model)


# --------------------------------------------------------------------------- #
# 1. newer means higher, inside the band too
# --------------------------------------------------------------------------- #

def test_a_newer_glm_outranks_the_one_it_replaces():
    assert _s("z-ai/glm-5.4") > _s("z-ai/glm-5.3")
    assert _s("z-ai/glm-6") > _s("z-ai/glm-5.4")
    assert _s("z-ai/glm-7") > _s("z-ai/glm-6")


def test_a_newer_hunyuan_outranks_the_one_it_replaces():
    assert _s("tencent/hy5") > _s("tencent/hy4")
    assert _s("tencent/hy6") > _s("tencent/hy5")


def test_they_all_stay_in_the_top_band():
    """Ordering within the band, not a new tier above everything."""
    for m in ("z-ai/glm-5.3", "z-ai/glm-6", "tencent/hy4", "tencent/hy6"):
        assert TOP <= _s(m) < TOP + 1.0, (m, _s(m))


def test_the_flash_variant_scales_the_same():
    """Ox Alpha was glm-5.3-flash specifically."""
    assert _s("z-ai/glm-5.4-flash") > _s("z-ai/glm-5.3-flash")


def test_nothing_below_the_band_moved():
    assert _s("z-ai/glm-5.2") == app._PREF_FLOORS[7]
    assert _s("tencent/hy3:free") == app._PREF_FLOORS[0]
    assert _s("z-ai/glm-4.6v-flash") < 100


def test_the_standing_newer_is_never_worse_rule_still_holds():
    """The guard added when qwen4 and kimi-k4 fell off a cliff."""
    fams = [["z-ai/glm-5.3", "z-ai/glm-5.4", "z-ai/glm-6"],
            ["tencent/hy4", "tencent/hy5", "tencent/hy6"],
            ["qwen/qwen3.9", "qwen/qwen4", "qwen/qwen5"],
            ["moonshotai/kimi-k3", "moonshotai/kimi-k4"]]
    for ids in fams:
        for older, newer in zip(ids, ids[1:]):
            assert _s(newer) >= _s(older), (older, newer)


# --------------------------------------------------------------------------- #
# 2. a top-band model may lead a build
# --------------------------------------------------------------------------- #

def test_a_top_band_model_may_lead_even_though_it_is_not_allowlisted():
    assert app._is_tool_proven("z-ai/glm-5.3-free") is False
    assert app._may_lead_agentic(_s("z-ai/glm-5.3-free"), "z-ai/glm-5.3-free") is True


def test_the_allowlist_still_lets_its_own_through():
    """Unchanged for everything that was already allowed."""
    for m in ("openai/gpt-oss-120b", "nvidia/nemotron-3-ultra", "models/gemini-3.7-flash"):
        assert app._may_lead_agentic(120.0, m) is True, m


def test_an_unproven_mid_tier_model_still_may_not_lead():
    """The gate is widened, not removed -- this is the case it exists for."""
    assert app._may_lead_agentic(110.0, "acme/some-unproven-thing") is False
    assert app._may_lead_agentic(134.5, "acme/decent-but-not-top") is False


def test_the_bar_is_the_top_band():
    assert app._may_lead_agentic(TOP, "acme/brand-new-flagship") is True
    assert app._may_lead_agentic(TOP - 0.01, "acme/nearly") is False


def test_a_hard_build_now_reaches_the_top_band():
    """The end-to-end symptom: a build routed to 134.1 while 138 sat unused."""
    from unittest import mock
    msgs = [{"role": "user", "content": "build a complete multi-page website"}]
    pool = [(138.0, "tokenrouter", "z-ai/glm-5.3-free"),
            (134.1, "groq", "qwen/qwen3.8-27b"),
            (134.1, "google", "models/gemini-3.7-flash")]
    leads = [c for c in pool if app._may_lead_agentic(c[0], c[2])]
    assert ("tokenrouter" in [c[1] for c in leads]), leads
