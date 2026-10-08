# HANDOFF — free-llm-hub (LLM Calvoun)

Snapshot for the next session or agent. Written 2026-10-08.

**Source of truth for HOW things work: `AGENTS.md`** (one section per feature, with
the test file that covers it). `README.md` is the user-facing manual. This file
is only: current state, how to operate, owner rules, open items.

## State right now

| | |
|---|---|
| Branch | `main` after the 2026-10-08 merge (see `git log`), in sync with `origin/main` (another session's landing-page edits are uncommitted: `app.py` `/`+`/hub` routes, `make_landing.py`, `templates/landing.html`, `static/*.webp|jpg` — LEAVE THEM, and never `git add -A`: use explicit paths) |
| Running hub | `fd3dc1e` on `127.0.0.1:8787` until the next restart: `0b98a88` and the 2026-10-08 batch are NOT live before it |
| Tests | 7094 passed, 1 skipped, 2 order-dependent self-update failures fixed after (`e75688a`, leaked stub run) — 2026-10-08 |
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
- Ranking choices made by the owner (do not "fix" them from benchmarks alone):
  Kimi K3 top free model (138.1, just above GLM 5.3's 138); Space Bunny 137.7;
  subscription scope `manager_only` (manager model: sonnet when re-enabled).

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
2. **Codex subscription** is not signed in inside the hub's isolated folder;
   the owner must run (PowerShell):
   `$env:CODEX_HOME = "$HOME\.free-llm-hub\isolated-clis\codex\config"; & "$HOME\.free-llm-hub\isolated-clis\codex\install\codex.CMD" login`
3. **Subscriptions are switched off** and the manager model setting is empty;
   pick `sonnet` when re-enabling (verified working 2026-09-30, 13.8 s per call).
4. **g4f relays** (the only route to Claude/GPT) are rate-limited by the
   service (HTTP 429); the hub re-probes every 30 min. Not a hub bug.
5. **Another local session is building a marketing landing page** (uncommitted:
   `app.py` `/`+`/hub` routes, `make_landing.py`, `templates/landing.html`,
   `static/*`). Its tests fail (route count in README, dashboard cache +
   security headers) — THAT session must finish and commit them. Do NOT commit
   them for it, and NEVER `git add -A` in this checkout (it sweeps them in — it
   happened once, `cee5200`, reverted in `90ada7d`). Stage explicit paths only.
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
9. **Next context ideas (research done 2026-10-03, owner to pick):** age-based
   clearing of old tool results (keep newest ~5, no model call; measured
   -52% cost, solve rate flat), cache-stable prefix + one provider per
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
