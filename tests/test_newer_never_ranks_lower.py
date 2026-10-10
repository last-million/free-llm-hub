"""A newer version of a good family must never rank BELOW the one it replaces.

USER RULE, stated 2026-08-31: "qwen 3.8 is goood tooo man so if available last
models swen use them of course always last ones".

This has now bitten three separate families in two days, each time the same
way -- a preference floor written against the version that was current when it
was written, so the NEXT version matched nothing and fell to the ordinary field:

    glm-5.3   tied with glm-5.2 at 134 (fixed 2026-08-30, ox alpha)
    hy4       scored 10.00, dead last  (fixed 2026-08-31)
    qwen4     scores 100 while qwen3.8 scores 134.08   <- this file

The failure is silent by construction: a model that ranks last is never routed
to, and a model that is never routed to never fails, so nothing ever reports it.
The only way it surfaces is somebody asking "is <new model> available?".

So this file is a STANDING GUARD, not a one-off fix. It walks the real floored
families and asserts the rule directly: within a family, a higher version never
scores lower than a version the hub already likes. Add a family to _FAMILIES
when a floor is added for it and the rule is enforced from then on.
"""
import app


# (label, ids in ascending version order). Every entry is a REAL id shape the
# catalog uses or would use for the next release of that family.
_FAMILIES = [
    # OWNER DECISION 2026-10-10: the "newer never lower" rule holds WITHIN one
    # size class. qwen's listed cut is 27B, so this line is all-27B (a 27B cut
    # sits a step below the full-size floor, but a newer 27B still beats an
    # older 27B); the full-size progression is covered by test_the_reported_case.
    ("qwen",     ["qwen/qwen3.6-27b", "qwen/qwen3.8-27b", "qwen/qwen3.9-27b",
                  "qwen/qwen4-27b", "qwen/qwen4.5-27b", "qwen/qwen5-27b"]),
    ("glm",      ["z-ai/glm-5.3", "z-ai/glm-5.4", "z-ai/glm-6", "z-ai/glm-7"]),
    ("hunyuan",  ["tencent/hy4", "tencent/hy5", "tencent/hy6"]),
    ("deepseek", ["deepseek/deepseek-v4", "deepseek/deepseek-v5",
                  "deepseek/deepseek-v6"]),
    ("minimax",  ["minimax/minimax-m3", "minimax/minimax-m4",
                  "minimax/minimax-m5"]),
    ("gemini",   ["google/gemini-3.6", "google/gemini-3.7", "google/gemini-4",
                  "google/gemini-5"]),
    ("kimi",     ["moonshotai/kimi-k3", "moonshotai/kimi-k4",
                  "moonshotai/kimi-k5"]),
]


def _score(mid):
    return app._benchmark_score("kilocode", mid)


def test_a_newer_version_never_scores_lower():
    """The whole rule, on every floored family at once."""
    problems = []
    for label, ids in _FAMILIES:
        for older, newer in zip(ids, ids[1:]):
            if _score(newer) < _score(older):
                problems.append("%s: %s (%.2f) ranks BELOW %s (%.2f)"
                                % (label, newer, _score(newer), older, _score(older)))
    assert not problems, "\n".join(problems)


def test_the_reported_case():
    """qwen4 must not fall off the cliff qwen3.9 sits on top of."""
    assert _score("qwen/qwen4") >= _score("qwen/qwen3.9")


def test_qwen_3_8_27b_is_a_size_variant_below_the_flagship():
    """OWNER DECISION 2026-10-10: the 27B cut is placed by its own board evidence,
    a documented step under the full-size qwen floor (not at it), and still above
    older 27B cuts. "qwen 3.8 is goood" still holds -- it leads its size class."""
    for mid in ("qwen/qwen3.8-27b", "Qwen/Qwen3.8-27B"):
        s = _score(mid)
        assert app._PREF_FLOORS[7] - 2.0 <= s < app._PREF_FLOORS[7], (mid, s)
        assert s > _score("qwen/qwen3.6-27b"), mid


def test_older_weak_versions_are_still_not_promoted():
    """The rule is "newer is not worse", NOT "everything is good". Versions
    below each family's pin keep their measured place."""
    assert _score("qwen/qwen2.5") < app._PREF_FLOORS[7]
    assert _score("z-ai/glm-4.6") < app._PREF_FLOORS[7]
    assert _score("tencent/hy2") < app._PREF_FLOORS[0]


def test_a_bare_family_name_is_not_a_version():
    """No version number means no version-based floor -- it must not read as
    "infinitely new"."""
    for mid in ("acme/qwen", "acme/glm", "acme/gemini"):
        assert _score(mid) < app._PREF_FLOORS[5], mid
