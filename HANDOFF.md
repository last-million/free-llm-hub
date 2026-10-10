# HANDOFF — free-llm-hub (LLM Calvoun)

Snapshot for the next session or agent. Written 2026-10-08.

**Source of truth for HOW things work: `AGENTS.md`** (one section per feature, with
the test file that covers it). `README.md` is the user-facing manual. This file
is only: current state, how to operate, owner rules, open items.

## State right now

| | |
|---|---|
| Branch | `main`, in sync with `origin/main`; the working tree is clean except the owner's own untracked `MODELS_BENCHMARK.md` (still stage explicit paths, never `git add -A`) |
| Running hub | `1bb8c92` on `127.0.0.1:8787` until the next restart (restart through `POST /api/hub/restart {resume:true, drain:true}`: running work waits and continues). NOT live yet: the 2026-10-10 batch below |
| Tests | 7812 passed, 2 skipped, 0 failed on the final merge (2026-10-08 night, 11.5 min; pytest exit code 0) |
| Keys | 42 provider keys in `config.load_config()` (check after every restart, never print values) |
| Open PR | #4 by `osumtr-web`: an improved version landed on `main` (`74c3fde`); the PR is NOT approved/closed yet, see "Open items" |

## Operate

```bash
# tests (either python; run the .venv one after touching imports/requirements)
python -m pytest tests/ -q
.venv\Scripts\python.exe -m pytest tests/ -q          # ~6 min, never two at once

# version actually running (token required, else you get the Ollama emulation)
TOK=$(python -c "import config;print(config.get_control_token())")
curl -s -H "X-Free-LLM-Hub-Token: $TOK" http://127.0.0.1:8787/api/version
```

**Restart** (`memory: restarting-the-calvoun-hub`): kill EVERY `app.py` python
process, then `wscript.exe //B run-hidden.vbs`; it answers after ~30 s. Verify
`/api/version` = HEAD and 42 keys. The owner allows a restart mid-job; since
`f254e9f` the boot also stops agent CLIs the previous hub left running (Windows
does not kill children), and a conversation's Multi run waits for **Continue**
unless its "Continue by itself after a restart" box is ticked.

## Owner rules (standing)

- Commits: author `last-million`, **no `Co-Authored-By`**, last paragraph
  `Release-note: <new|fix|improved|docs>: <one plain sentence>`. Secret-scan the
  diff before every push. Push to `main` and restart the hub when work is done.
- Never print API keys/tokens. No config change on the owner's machine without asking.
- Token economy: grep/graphify before reading `app.py` (~37k lines), short
  answers, no extra markdown files unless asked.
- No parallel live provider sweeps (rate limits fake regressions). Small UI
  changes: static tests, no screenshots. Big UI work: run the `ui-ux-pro-max` skill.
- Ranking follows the public boards (OWNER DECISION 2026-10-10, replaces "Kimi K3
  top free model 138.1"; see AGENTS.md "Ranking follows the boards"): TOOL turns
  ordered by Terminal-Bench 4.0 (GLM 5.3 leads, Kimi K3 near the bottom of the
  six benchmarked free models), CHAT by AA + LMArena (Kimi K3 / GLM 5.3 / Gemini
  3.8 Flash close at the top). Space Bunny 137.7 and Pixel Canary 137.6 keep their
  owner floors (no public board). Subscription scope `manager_only` (manager
  model: sonnet when re-enabled).

## What changed 2026-10-10

- **Batch 2 (same day):** Publish opens a `<dialog>` popup (desktop window / phone bottom sheet);
  the project goal is one compact row under the Build toolbar; **apps deploy by themselves**
  (`deploy_perfect.py`, `envprobe.py`): diagnosis of conversation 2e3525ad = a frontend+backend
  monorepo (Express + Vite) needing PostgreSQL, which is NOT installed here, never installed/
  built/seeded by the preview, never verified, so each run redid the deploy (5th attempt). Now:
  the preview installs declared sub-projects (`--ignore-scripts`) and builds the frontend on an
  explicit start, seeds `.env` from `.env.example` (no symlinks, never overwrite), the planner
  sees "THIS MACHINE" (prefers SQLite when no DB server), every Multi run ends with a deploy
  check ("Deployed: <url>" or the exact error + ONE `deploy_fix` phase), and a passing start is
  remembered (`preview-starts.json`). Flags `deploy_perfect`, `planning_env_probe`,
  `deploy_check_after_run`. The owner's current app still needs PostgreSQL or a switch to SQLite.
- **Multi from a terminal CLI = the Build page's Multi** (`_cm_*`, flag `cli_multi_sessions`):
  a CLI on `*-multi` with a real task in a known, non-broad project folder starts a real
  swarm_windows run (planner + up to 6 helpers) owned by a hub conversation (Build page link,
  auto_resume on); the CLI turn streams progress text, ends safely before the CLI's stream cap
  (opencode 540 s; codex/claude/kimi 240 s, their caps are INFERRED from compiled binaries) and
  later messages re-attach; "stop multi" (exact) stops it. Security (review of the commit):
  runs are keyed by conversation + folder, and the first eligible turn only ASKS -- the user
  must reply exactly "go multi" (flag `cli_multi_confirm`, default on). Residual: the MCP tool
  `swarm_windows_start` still starts a run for any local client without that consent.
- **Roles: fewer turns with no answer** (`_rr_*`): before (2026-10-08 05:30 -> 10-10) 334 roles
  turns, 14.1% no answer, 23.4% of calls wasted, verifier usable 0.59. Fit decisions now use the
  size the hop SENDS after clearing old tool results (the ~45 false "window too small" skips),
  impossible hops (tool schema too big, dead/404 models) cost no attempt, the roles stage reserves
  a slice for the `best` fallback, thinking/slow verifiers with no usable record are skipped.
  `python scripts/role_eval.py --since <ISO time>` prints the 5 numbers to compare after.
- **cloudflared installs itself** (`publish.AutoInstaller`, flag `cloudflared_auto_install`):
  ~60 s after boot, the same SHA-256-verified official download, at most once per 24 h after a
  failure, never during a drain/Stop; Publish-panel checkbox to turn it off. README now lists
  every outbound call (OpenRouter catalog, LMArena dataset, Cloudflare's GitHub releases).
- **Why GLM 5.3 leads (owner asked)**: by the owner's ranking kimi-k3 138.1 and glm-5.3 138.0 are
  the two best USABLE models (both nvidia); Claude Opus 5.5 / GPT-6 exist only on g4f relays
  (134.0 after the relay discount, plus the sustain penalty), so they are backups. Owner can pin
  one with `/orchestrator <name>` (open decision, see item 1b).

## What changed 2026-10-08 night

Details in `AGENTS.md` (one section each, appended at the end). Headlines:

- **Publish online** (free Cloudflare quick tunnel; `publish.py`, 5 routes `/api/publish*`,
  Build-page button with countdown, MCP tools `publish_*` + a one-line ask in the briefs):
  explicit click or an explicit YES in the CLI, TTL 15 min-24 h (default 1 h) enforced by the hub,
  new link any time, the URL is never logged, only a project's own preview (never the hub port),
  at most 3 tunnels. `cloudflared` is NOT installed on this PC: the Publish panel's Install button
  downloads the official release only after a click and refuses without a matching SHA-256.
  NEVER run against the real Cloudflare yet: every test used a fake. First real run = owner's call.
  Hardened after the automated push review (`ec277a6`): an AGENT may publish only a server running from
  the project's own folder (`not_project_server` otherwise); optional strict mode
  `agent_publish_requires_approval` (default off) makes `publish_start` wait for the user's click in
  the Publish panel. Residual risk: an agent can name another service's folder as `project_dir`.
- **Newest and biggest first inside a family** (`modelrank.py`): higher version first, Opus > Sonnet >
  Haiku (Haiku a small model), older generations stay as fallbacks. Two real bugs fixed: weaker relay
  copies took all 3 relay slots; one failure marked a pair "measured to fail" for ~1.8 h. The g4f
  relay (the only route to Claude 5 / GPT-6) is PARKED by its own gateway until ~21 h out, so they
  cannot serve until it unparks. OPEN DECISION: every g4f row loses 29 points in the agentic score
  (`_sustain_penalty` reads "5 per minute" as per day); fixing it also lifts llm7/navy/nararouter.
- **Fewer wasted hops**: the 173 "HTTP 400" on uncloseai Qwen3.8 were the hub sending several system
  messages (the template wants one, first) -> merged and retried; spread/rotation no longer leave the
  band for a weak leftover; 3 identical failures rest a pair (doubling, cap 6 h, still a last resort);
  `wasted_calls` + `scripts/role_eval.py` summary. Before: 2.40 calls/turn, ~32% wasted.
- **Graceful updates**: an update drains new work (503 + Retry-After), lets running work finish (max 10
  min), writes `update-resume.json`, restarts, and every cut conversation continues by itself with a
  notice. `POST /api/hub/restart {resume, drain}` is the safe way to restart. CLI retry evidence:
  Claude Code/opencode/kimi-code retry 503 (~2.5-5 min); Codex is UNVERIFIED (a long update may show an
  error in a TERMINAL Codex; Build conversations resume through the marker either way).
- **Merge lessons**: two agents defined `_publish_fail` in app.py (later def won, 21 failures); strict
  one-argument test stubs broke when the brief gained `session_id`; an agent's test assumed its
  AGENTS.md section was last. Give each parallel agent unique helper names (prefix) and never assert
  "last section".

## What changed 2026-10-08 evening (errors seen in /agent and the CLIs)

Trigger: the owner saw `error · 200`, `error · 503` and `error · 504` in /agent and
the CLIs. Evidence-first; headlines, details in `AGENTS.md`.

- **Build `error · 200`/`503`** = ONE Codex conversation (session `c39a30c1`) stuck at
  ~504K estimated tokens for hours (no model holds more than 262K): the hub's native
  context-length reply shows as 200 (a `response.failed` inside a 200 stream), and the
  chain walk over default-window relays (each got a ~2 MB upload) ended in 503 after
  85-265 s. WHY it never compacted is NOT proven (served turns report usage correctly;
  Codex's own compaction request is served trimmed). `e019d96`: the front door refuses
  such a request with zero hops and no upload on all 3 protocols (`_front_door_overflow`),
  a session pin on a too-small model is dropped, Build says "context too long" with a
  Continue button, and swarm/crew/multi tool tiers now report usage (they reported 0/40000).
- **CLI `error · 503`**: one burst on 2026-10-08 02:48 UTC was `NameResolutionError` on three
  hosts at once, one second after the hub uploaded a ~2 MB body. The DNS cause is a
  HYPOTHESIS from one event (only 2 log lines carry it); a clean 72-lookup test minutes later
  had 0 failures. `f5da506`: `netresolve.py` reuses the last good address when the resolver
  fails (flag `dns_stale_cache`), a local-network failure is filed against no provider, the
  chain pauses and re-walks once, and the 503 says it is this computer's DNS.
- **CLI `error · 504`** on ~85K-token tool turns (nvidia kimi-k3 slower than the 106 s hop
  budget): `36e42bb` clears OLD tool outputs above 60K tokens (flag `old_tool_result_clearing`,
  default on; synthetic 85K history -82% tokens; reported usage stays sized on the original).
  KNOWN GAP: the `window too small` exclusions still use the pre-clear estimate.
- **Multi planning took 9 minutes** (two empty planner replies after 190 s / 138 s):
  `10b49a3`: planner hops 90 s, an empty reply hands over inside the attempt, thinking models
  get low effort + 1024 tokens, the optional dry-run re-ask is capped (flag
  `planner_time_bounds`). The reason for the empties was NOT observable (no per-hop planner
  log existed); a `[plan] hops` log line now records it.
- **Hub port inherited by agent CLIs** (`7b953de`): an agent's `npm run dev` took 8787 and
  `run.bat` then refused to start the hub. `agentic_chat.strip_hub_port_vars`.

## What changed 2026-10-07 → 2026-10-08

All in `AGENTS.md` with tests; headlines only.

- **Owner blocklist ships as the default** (`0b98a88`): `_DEFAULT_BLOCKED_IDENTITIES`
  + `_DEFAULT_BLOCKED_MODELS` (one exact `pid/model`), offered once per install.
- **Provider fairness** (`d05e702`, `a05fd8a`): load-aware tie-break INSIDE the
  2-point top band (in-flight soft cap, 15-min share, recent fails); small
  requests let groq/cerebras-class compete; a share counts only after 6 picks;
  `GET /api/provider-load`. Groq stays rare on big turns for a real reason: 97%
  of tool turns are >= 60K tokens and its free tier caps one request ~8K.
- **Big CLI turns** (`2416e5b`): fallbacks only pick a model whose window holds
  the request; actor budget sized for big turns; honest "switched OFF" note;
  model_req shows `mode -> model`; Codex "error · 200" was a false match on
  `"error": null`. The nvidia/glm ConnectionError bursts were reproduced: NOT a
  hub socket/pool leak (no shared Session); 503 lines now carry per-hop details.
- **ECC skills** (`360f974`): 9 vendored MIT skills (Affaan Mustafa), OFF by
  default, per-skill + "enable all" in Settings -> Skills (`ecc.py`).
- **Probes** (`74c3fde`): token-free `/health` `/healthz` `/ready` `/readyz`, no
  version or provider data (PR #4's idea, rewritten).
- **Task board + the goal behind every task** (`a371798`, `2198e0c`):
  `taskboard.py`; goal brief (<= 600 chars) in Multi planner/workers, the Build
  brief file and the opening terminal-CLI turn; Multi phases move their tasks.
- **Heartbeats + budgets** (`8c6a76c`): `heartbeat.py` schedules (OFF by default,
  `heartbeats_enabled`), skip when the owner is busy / no RAM / 429s; per-run
  token/call/time budgets stop a run cleanly (`multi_default_budget`).
- **CLI and Build parity** (`d4e05bb`): terminal CLIs and Build single sessions
  get the web-slop check, specialists on hard fresh tool turns (auto/best),
  observed test/build receipts from the conversation, project facts, PROGRESS
  upkeep on every step. Flags `turn_slop_check`, `tool_turn_specialists_single`,
  `v1_observed_evidence`, `v1_memory_facts`.

## What changed 2026-10-03 → 2026-10-04 (through 90ada7d)

All in `AGENTS.md` with tests; headlines only. These sit ON TOP of the
2026-09-30 batch further down.

- **Orchestration (the "beat Sakana Fugu" set, `cee5200`)**: tool turns no
  longer race the SAME prompt on 3-5 models (measured 4 calls/answer, 75%
  wasted). Now ROLES: one actor, a stall-only backup, an independent verifier
  from another model FAMILY on risky steps, one corrector (`app._tool_turn_roles`,
  `verify.py`, flag `tool_turn_race` default OFF = roles). A learned tie-breaker
  (`bandit.py`, +-1 pt, only inside the 2-pt top band, NEVER over owner floors /
  blocklist / last-resort; rewards = observed PASS / verifier OK, never 429s).
  Swarm/crews reviewer from another family; Multi free verdict without a manager;
  wider-or-deeper retries where a scorer exists (`swarm.py`/`crews.py`/
  `swarm_windows.py`). Weak models (score < 120) get evidence-backed per-family
  guides (`model_guides/*.md`), smaller steps, always-verify. AI slop decided out
  at design time + checked in finished web files (`slopcheck.py`, feeds the one
  revision). Flags: `tool_turn_race`, `pipeline_search`, `model_guides`,
  `turn_verifier`. Eval: `scripts/role_eval.py` + `state_dir()/turn-roles.jsonl`.
- **Multi up to 6 different models at once (`f0a452d`)**: `_concurrency()` =
  min(`multi_parallel_max` (default 6), by-machine, by-fleet, 429 back-off). A LIVE
  RAM governor (2 s tick while a run walks) keeps `reserve` = max(3 GB, 20% of
  RAM) free for the user's own programs, learns the real per-helper cost (p90 of
  the last 10 RSS samples), lowers at once / raises after 20 s, and only STOPS
  STARTING helpers (never kills one); below 1 GB free it lowers the helpers'
  process priority. Helpers of a run get distinct model/provider/family; big phases
  can get a pair helper (`multi_pair_phases`); `MAX_AGENTS` 10. Status:
  `GET/POST /api/multi-parallel`, `/api/low-resource` (`reserve_gb`, `allowed_now`).
  Verified live 2026-10-04: reserve 7.9 GB, 19 GB free, `now` 6. NOT yet verified
  with a real run: the RSS sampling and the priority change (tests use fakes).
- **Swarm/crews distinct models + reliability (`f0a452d`)**: workers of one run take
  distinct identities (`RunLedger`, `swarm_distinct_models`); per-phase free verdict
  (`swarm_phase_verdict`); specialists/verifiers ranked by measured success and
  speed (limit 35 s); the verifier asks for a one-line `VERDICT:` with one retry;
  medium build asks get scout+critic (`tool_turn_specialists_medium`). Tests pin
  the random bandit nudge to 0 unless a file sets `USES_REAL_BANDIT = True`.
- **Stalls are remembered (`fd3dc1e`)**: an actor replaced by the stall backup is
  demoted for tool turns (it was picked first again within 2 min, 45 s lost each
  time); unmeasured backup delay = fleet median x2.5 (12-30 s); 3 attempts fit in
  180 s; 3 empty-200s rest a pair 10 min (orchestrator pin included). Open: if the
  stalled pair IS the orchestrator pin, the pinned-head rule may still open on it.
- **Team notes (`117579d`)**: on a HARD, fresh-instruction tool turn (not a loop
  continuation, not trivial, <= 60K tokens, not a CLI compaction) the hub runs 2-3
  DIFFERENT models in parallel on read-only jobs (scout, critic, designer for
  build/web work), merges their notes with NO model call (<= 2500 chars, cached
  per conversation) and gives the brief to the actor. Flag `tool_turn_specialists`
  (default on). Activity shows `specialist: <role>` chips; header
  `X-Free-LLM-Hub-Roles` has `specialists=N`; `turn-roles.jsonl` rows carry them.
  FIRST LIVE PROBE (2026-10-04, coding-multi): scout + designer timed out (25 s,
  free models slow/429), critic answered, 5 calls total (design estimate was 3-4).
  Tune the specialist timeout / pick only models with fast measured TTFT if most
  specialists keep timing out.
- **Tests**: conftest pins the bandit tie-breaker to 0 outside files that mention
  `bandit` (it made `test_model_mode` flaky, 4/15 runs).
- **Context**: OpenCode 503 loop fixed (declared windows capped at what 3
  non-relay providers hold, reach window per-CLI, google 250K per-request cap);
  native "context too long" when only out-for-long big models fit; a CLI's own
  compaction never runs the Multi/swarm pipeline; declared windows published
  with hysteresis (no 30-min flip). Reach capped at 400K (owner).
- **Reliability**: Stop really stops (client-disconnect cancel within ~0.5 s,
  `clientgone.py`); a hop the hub gives up on no longer blocks the next for 90 s
  (now ~8 s); broken upstream streams are retryable, not a silent "done"; Multi
  phase with no reply gets one retry; roles race model shown honestly in Activity.
- **Quality**: observed test/build evidence replaces "tests pass" claims
  (`evidence.py`, receipts in `state_dir()/receipts/`); design + plan dry-run
  before building (`plan_check.py`); Multi phases start on their own deps (not
  wave barriers); install commands time out (`workspace.py`); `nul`-named file
  no longer 500s the dashboard tree.

## What changed 2026-09-30 → f254e9f

All in `AGENTS.md` with tests; headlines only:

- Routing: Auto opens on the strongest model (top band 2 pts), a weaker
  allow-listed family never queues ahead of a stronger model, a chosen
  orchestrator opens real turns; easy turns still go to medium models.
- Multi sessions: work detected in any language; follows the selected
  category and effort; helpers run side by side when they touch different
  files; each helper gets a different strong model; planning shown live;
  helpers panel + helper pages; live helper actions in the conversation.
- Restarts: "Continue from there" bar, per-conversation auto-resume (off by
  default), queued messages wait for the owner, Stop also works between two
  CLI processes, leftover CLIs stopped at boot.
- Settings Stop disconnects every CLI and reconnects them on the next start.
- Benchmarks: LMArena text board fetched daily (free HF dataset) for unknown
  models only; Artificial Analysis as before. Old failure records fade (8 h
  half-life while untried). PyYAML + tzdata pinned (the .venv lacked them).

## Open items

1. **PR #4**: the improved probes are on `main` (`74c3fde`). The approval and a
   "landed as 74c3fde, closing" comment were NOT posted: this machine has no
   GitHub auth for `last-million` (`gh` not installed, `GH_TOKEN` rejected, the
   Chrome profile is signed out, the GitHub MCP plugin fails). Owner decision:
   approve only, never merge (sole contributor). Close it after the comment.
   SECURITY: the Windows Credential Manager entry for `git:https://github.com`
   belongs to ANOTHER account (`jaddireda4-design`, admin scopes); it was not
   used. The owner was told to remove it if it is not theirs.
1b. **Owner decisions pending (2026-10-08):** (a) the "compact trick": answer an oversized
   request with a SHORT SUCCESSFUL reply whose usage is over Codex's compact limit so Codex
   compacts by itself (works around an unverified Codex behaviour and adds a hub-written
   line to the conversation); (b) the CLI-declared window dips to 32000 for 90 min after a
   one-tick provider dip (a decrease applies at once by design, a raise waits
   `_DECLARED_RAISE_AFTER`); a log-only `[ctx] declared window inputs` line now says why;
   decide on a policy change after reading it.
1c. **PARKED IDEA, owner 2026-10-08 (do NOT build yet; remind the owner next conversation):**
   push a Build project to GitHub from the Build page, choosing private or public at repo
   creation (default private), then push/sync whenever asked. Design notes are in the
   assistant's memory (`idea-github-push-for-build-projects`): explicit click per push, a
   token stored encrypted like provider keys, secret scan + .gitignore before the first
   push, never force-push, hermetic tests with a fake GitHub API. This PC has no usable
   GitHub API auth today (see item 1).
1d. **Smaller follow-ups from the night batch:** the `window too small` exclusions still use the
   pre-clear token estimate (a 65K model could hold a cleared 85K turn); `.btn.primary` is white on
   `#16A34A` = 3.3:1 in the light theme (below 4.5:1, pre-existing, now also on Publish); the
   `[ctx] declared window inputs:` log line now says why a CLI-declared window dips to 32000 (read it
   before changing that policy); groq `RequestException` (49 in 24 h) is not root-caused; the
   verifier gave no usable verdict on 72% of its runs (kimi-k3 and muse-glimmer left its pool).
2. **Codex subscription** is not signed in inside the hub's isolated folder;
   the owner must run (PowerShell):
   `$env:CODEX_HOME = "$HOME\.free-llm-hub\isolated-clis\codex\config"; & "$HOME\.free-llm-hub\isolated-clis\codex\install\codex.CMD" login`
3. **Subscriptions are switched off** and the manager model setting is empty;
   pick `sonnet` when re-enabling (verified working 2026-09-30, 13.8 s per call).
4. **g4f relays** (the only route to Claude/GPT) are rate-limited by the
   service (HTTP 429); the hub re-probes every 30 min. Not a hub bug.
5. **Landing page removed (owner, 2026-10-10: "we don't need it")**: the other session's
   uncommitted marketing page (`/` landing + `/hub` route in app.py, `make_landing.py`,
   `templates/landing.html`, 7 images in `static/`) was never pushed; it was moved to
   `~/.free-llm-hub/backups/landing-removed-20261010/` and app.py restored to the committed code.
   `/` is the dashboard again. Do not rebuild it unless the owner asks.
6. **Freebuff / tmux multi-account (declined, do not build):** Freebuff's ToS
   (freebuff.com/terms-of-service) forbids multiple accounts, bot/script/tmux
   control, and proxying its models — so `fbuff.sh` and any hub integration are
   out. Use Freebuff by hand only.
7. **Verify the orchestration live, then measure** (specialists: 12 live calls, 42%
   answered before the ranking fix; verifier usable-verdict rate was 21%; re-measure
   with `scripts/role_eval.py` after a day of traffic): run real tool turns and
   `python scripts/role_eval.py` to compare roles vs the old race (calls/turn,
   input tokens, zero-answer rate) from `turn-roles.jsonl` vs hub.log. The
   token-saving and verifier fix/break claims are DESIGN estimates, not yet
   measured on live traffic.
8. **Small tech debt**: two helpers read the project folder from a CLI's env
   block (`_project_dir_from_messages` from the task board, `_v1_project_cwd`
   from the parity work); merge them when either is touched next.
9. **Next context ideas (research done 2026-10-03, owner to pick):** ~~age-based
   clearing of old tool results (keep newest ~5, no model call; measured
   -52% cost, solve rate flat)~~ (done 2026-10-08: AGENTS.md "Old tool
   results are cleared on big turns"), cache-stable prefix + one provider per
   conversation, function signatures of files read in the recap.

## Gotchas

- Bash heredocs on this machine mangle backslashes: write patch scripts with
  the Write tool. `git` LF→CRLF warnings are normal.
- `run.bat` must stay CRLF. Tests must never touch the owner's real CLI
  configs, env vars or `~/.free-llm-hub/aa_scores.json` (conftest stubs them).
- Agent CLIs keep running when the hub is killed; find them by env
  `CALVOUN_AGENT_TURN` (value starts with the session id), never kill the
  current run's worker.
- `gh` is not installed and the GitHub MCP connector fails auth; PR actions
  go through the browser (or install `gh` and `gh auth login`).
