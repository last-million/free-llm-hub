# Vendored: last30days (MODIFIED)

- Upstream: https://github.com/mvanhorn/last30days-skill
- Version: 3.18.4 (shallow clone, 2026-07-30)
- License: MIT — Copyright (c) 2026 Matt Van Horn (see `LICENSE` in this directory)

This is a **modified** vendor for Kimi Code running against the local
free-llm-hub (127.0.0.1:8787). Upstream history is not imported; the clone was
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

Everything below the adaptations banner in `SKILL.md`, and all of `scripts/`,
is byte-identical to upstream v3.18.4.
