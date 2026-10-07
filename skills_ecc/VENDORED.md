# Vendored: ECC skills (opt-in)

- Upstream: https://github.com/affaan-m/ECC
- Commit: `ef648e01899ba3e8dc6371642deaaf64b4477775` (repo VERSION 2.2.3)
- Fetched: 2026-10-08 (shallow clone, discarded after copying the files below)
- License: **MIT** — Copyright (c) 2026 Affaan Mustafa (see `LICENSE` in this
  directory, copied verbatim from the upstream root `LICENSE`).

The MIT license permits use, copy, modify and redistribution provided the
copyright notice and the permission notice are included — both are kept in
`LICENSE` here. Nothing else in the repo was vendored.

## What was taken

Nine skill documents, each the `SKILL.md` of an upstream
`.agents/skills/<name>/` directory, copied as `skills_ecc/<name>.md`. Only the
plain-markdown instruction documents useful to a coding agent were taken:

| file | upstream name | topic |
|------|---------------|-------|
| `tdd-workflow.md` | tdd-workflow | test-driven development |
| `verification-loop.md` | verification-loop | verify work before claiming done |
| `security-review.md` | security-review | security checklist & patterns |
| `coding-standards.md` | coding-standards | cross-project coding conventions |
| `agent-introspection-debugging.md` | agent-introspection-debugging | self-debugging failed runs |
| `backend-patterns.md` | backend-patterns | Node/Express/Next.js server patterns |
| `frontend-patterns.md` | frontend-patterns | React/Next.js UI patterns |
| `api-design.md` | api-design | REST API design conventions |
| `e2e-testing.md` | e2e-testing | Playwright E2E testing patterns |

Each file keeps its upstream YAML frontmatter (`name` / `description` /
`license`) for attribution. The hub strips the frontmatter before injecting the
body (see `ecc.py`).

## What was modified

- **`tdd-workflow.md` only** — one trim. Upstream "Step 0" told the agent to run
  a bundled helper script, `node scripts/setup-package-manager.js --detect`
  (reading `CLAUDE_PACKAGE_MANAGER` / `.claude/package-manager.json`). That
  script is ECC infrastructure and was **not** vendored (see below), so the step
  was replaced with a script-free instruction to detect the package manager from
  the `package.json` `packageManager` field and the lockfile. An HTML comment at
  the edit site points back here. Nothing else in that file, and no byte of the
  other eight files, was changed.

## What was left out, and why

- **`agents/openai.yaml`** — every upstream skill ships one alongside its
  `SKILL.md`. It is a machine config, not markdown, and not needed for the text
  injection; excluded so the vendored set is markdown only.
- **All executable content** — the repo's `scripts/`, hooks, binaries,
  installers, `node scripts/*.js`, package/CI config and the bundled
  package-manager detector. None are vendored (supply-chain risk; the hub only
  injects text).
- **The other ~30 skills** (article-writing, brand-voice, investor-outreach,
  market-research, video-editing, x-api, exa-search, deep-research, etc.) —
  content/marketing/business-ops skills, not coding-agent rules.
- **`strategic-compact`** — a useful idea, but its instruction is built around
  the Claude-Code-only `/compact` slash command, which does not work through the
  hub across other CLIs; left out rather than vendor an instruction that would
  misfire. (The hub already does context compaction itself — see `ctxwin.py`.)

## How it is wired

`ecc.py` holds the catalog (id `ecc:<name>`, display name, one-line description,
trigger keywords drawn from each skill's own frontmatter description) and reads
the markdown here on demand (cached by mtime, fail-open). The skills are
**off by default**; the owner enables them, all at once or one by one, in
Settings -> Skills -> "ECC skills". When enabled and matched, a skill's body is
injected as one bounded system block (<= 1500 chars each, at most 2 ECC skills
per request) exactly like the hub's existing USER SKILL blocks — see the
"ECC skills (vendored, opt-in)" section of the repo `AGENTS.md`.

Credit: these documents are the work of Affaan Mustafa and the ECC project,
redistributed here under the MIT license.
