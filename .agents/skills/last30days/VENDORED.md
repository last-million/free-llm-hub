# Vendored: last30days (MODIFIED)

- Upstream: https://github.com/mvanhorn/last30days-skill
- Version: 3.18.4 (shallow clone, 2026-07-30)
- License: MIT — Copyright (c) 2026 Matt Van Horn (see `LICENSE` in this directory)

This is a **modified** vendor for Kimi Code running against the local
Calvoun Free LLM Hub (127.0.0.1:8787). Upstream history is not imported; the clone was
discarded after copying `SKILL.md` + `scripts/` + `LICENSE`.

## Modifications vs upstream

1. **Frontmatter** trimmed to Kimi Code's skill schema (kept
   `name`/`version`/`description`/`homepage`/`repository`/`author`/`license`;
   dropped `argument-hint`, `allowed-tools`, `user-invocable`,
   `metadata.openclaw`). `description` extended with the keyless-by-default note.
2. **Kimi Code invocation**: a new top section in `SKILL.md` instructs running
   `python "${KIMI_SKILL_DIR}/scripts/last30days.py" $ARGUMENTS --emit=compact`,
   keeping the upstream `SKILL_DIR` resolution as fallback.
3. **KEYLESS-BY-DEFAULT gating**: the same top section mandates a
   `curl -s -m 3 http://127.0.0.1:8787/api/web-search-policy` check before any
   X/social/authenticated source. Social sources are used only when the hub's
   "Social media web search" switch is on (`{"social_search": true}`);
   otherwise the engine is restricted to keyless sources
   (`--search reddit,youtube,hackernews,digg,arxiv,github,web` or the equivalent
   `EXCLUDE_SOURCES=...`). Gating is instruction-level only — the engine script
   itself already skips sources whose credentials are absent, so no script
   patches were needed.
4. **Proactive-suggestion rule** added (SEO / social trends / marketing /
   "what are people saying about X").

5. **Split into SKILL.md + `references/`** (2026-11-20). NVIDIA SkillEvaluator
   measured the old SKILL.md at 2266 lines / 226,851 bytes / ~56K tokens
   (limits 500 lines, 51,200 bytes, ~5K tokens) and OpenCode stops reading a
   SKILL.md at 50 KiB, so agents there never saw the research and synthesis
   steps. SKILL.md (now ~46 KB) keeps what every answer needs: the hub section,
   the SKILL CONTRACT, the whole OUTPUT CONTRACT (badge and LAWs 1-11) and HOW
   TO INVOKE (all of which sat inside the old first 50 KiB, so nothing the
   model could see before is now behind a Read), plus an index naming the
   `references/` file that holds every other upstream section: the LAW 6
   worked example, the trending/discovery protocol (stubbed in HOW TO INVOKE),
   setup, intent and pre-flight, comparison modes, research, synthesis,
   follow-up, and Security & Permissions. The upstream text was moved line for
   line (checked: no line lost); each reference file starts with a two-line
   comment saying so, and the SKILL CONTRACT carries a one-line hub note that
   "this file" now means SKILL.md plus `references/`. The `description` gained
   a leading "Use when ..." trigger.

Apart from those changes, the upstream sections (in `SKILL.md` below the
adaptations banner and in `references/`) and all of `scripts/` are
byte-identical to upstream v3.18.4.
