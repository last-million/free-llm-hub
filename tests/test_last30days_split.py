"""The vendored last30days skill stays readable by every harness.

MEASURED 2026-11-20 with NVIDIA SkillEvaluator: the old SKILL.md was 2266 lines /
226,851 bytes / ~56K tokens, and OpenCode stops reading a SKILL.md at 50 KiB, so the
research and synthesis steps never reached the model there. It was split into a SKILL.md
that keeps the output contract and HOW TO INVOKE (both sat inside the old first 50 KiB)
plus references/ (upstream text moved line for line). These tests keep it so.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(ROOT, ".agents", "skills", "last30days")
REFS = ("output-example.md", "discovery.md", "setup.md", "intent-and-preflight.md",
        "comparison-modes.md", "research.md", "synthesis.md", "follow-up.md", "security.md")
OPENCODE_CUT = 50 * 1024
MARGIN = 3 * 1024            # room for the next edit before a harness starts cutting


def _read(*parts):
    with open(os.path.join(SKILL, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_skill_md_fits_every_harness_with_room_to_spare():
    md = _read("SKILL.md")
    assert len(md.encode("utf-8")) < OPENCODE_CUT - MARGIN
    assert md.count("\n") < 500


def test_every_reference_file_exists_and_fits():
    for name in REFS:
        body = _read("references", name)
        assert len(body.encode("utf-8")) < OPENCODE_CUT, name
        assert body.startswith("<!-- Part of the vendored last30days skill"), name


def test_index_names_every_reference_file():
    md = _read("SKILL.md")
    idx = md[md.index("## WHERE THE REST OF THIS SKILL LIVES"):md.index("# SKILL CONTRACT")]
    for name in REFS:
        assert "references/%s" % name in idx, name


def test_the_output_contract_and_invoke_stay_inline():
    # They were inside the old first 50 KiB, so an OpenCode agent could see them: they must
    # not now need an extra Read. The badge, every LAW and the first-run gate stay here.
    md = _read("SKILL.md")
    for needle in ("# OUTPUT CONTRACT", "**BADGE (MANDATORY, FIRST LINE OF OUTPUT)", "### VOICE CONTRACT LAW",
                   "# HOW TO INVOKE THIS SKILL", "FIRST-RUN GATE", "STEP 1 - RUN THE ENGINE"):
        assert needle in md, needle
    for n in range(1, 12):
        assert "**LAW %d " % n in md or "**LAW %d -" % n in md, "LAW %d" % n
    assert md.index("# OUTPUT CONTRACT") < md.index("# HOW TO INVOKE THIS SKILL")


def test_the_two_stubs_point_at_their_reference():
    md = _read("SKILL.md")
    assert "references/output-example.md" in md[md.index("**LAW 6"):md.index("**LAW 7")]
    inv = md[md.index("# HOW TO INVOKE THIS SKILL"):]
    assert "references/discovery.md" in inv[:inv.index("If the user provided a topic")]


def test_each_upstream_section_lives_in_exactly_one_place():
    heads = ["# OUTPUT CONTRACT", "# STEP 0: STALE-CLONE SELF-CHECK", "## Runtime Preflight",
             "## Step 0: First-Run Setup Wizard", "## CRITICAL: Parse User Intent",
             "## Step 0.45: Query Quality Pre-Flight", "## Step 0.5: Pre-Flight Resolution",
             "## Step 0.55: Pre-Research Intelligence", "## Step 0.75: Generate Query Plan",
             "## Research Execution", "## STEP 2: DO WEBSEARCH AFTER SCRIPT COMPLETES",
             "## Judge Agent: Synthesize All Sources", "## THEN: Show Summary + Invite Vision",
             "## WAIT FOR USER'S RESPONSE", "# SKILL CONTRACT", "# HOW TO INVOKE THIS SKILL",
             "## Security & Permissions", "**Worked example (LAW 6 transformation).**",
             "- **If the user asks what is trending — globally"]      # the original bullet, em dash and all
    texts = [_read("SKILL.md")] + [_read("references", n) for n in REFS]
    for head in heads:
        pat = re.compile(r"^" + re.escape(head), re.M)
        assert sum(len(pat.findall(t)) for t in texts) == 1, head


def test_hub_gating_and_triggers_stay_in_skill_md():
    md = _read("SKILL.md")
    assert md.index("# KIMI CODE / CALVOUN FREE LLM HUB ADAPTATIONS") < md.index("# SKILL CONTRACT")
    assert "/api/web-search-policy" in md and '"skill_enabled": false' in md
    desc = re.search(r'^description: "(.*)"$', md, re.M).group(1)
    assert desc.startswith("Use when")
