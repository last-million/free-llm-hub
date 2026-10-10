# AGENTS.md — free-llm-hub

## Token economy: use the graphify graph before reading files

`app.py` is ~23k lines. Do NOT read it wholesale. A graphify knowledge graph
of this repo lives in `graphify-out/` — query it first:

- `graphify query "<topic>"` — returns the relevant symbols with `file:line`
  locations, self-capped to a ~2000-token budget. Raise with `--budget` or
  narrow with `get_node` for a specific symbol.
- `graphify-out/GRAPH_REPORT.md` — community hubs (navigation entry points).
- Freshness: the graph records the commit it was built from. A `post-commit`
  hook (installed via `graphify hook install`) rebuilds it automatically after
  each commit — keep it. To rebuild by hand: `graphify update .` (no API cost).

## "Caveman" mode (difficulty-aware routing)

The hub classifies every request simple/medium/hard (`_classify_difficulty`,
app.py:1213) and routes to the cheapest model that clears the tier floor
(`_route_by_difficulty`, app.py:2881); reasoning effort follows difficulty
(`_apply_reasoning_effort`, app.py:2588). Covered by
`tests/test_difficulty_routing.py` — keep those tests green when touching the
routing heuristics.

## Last-resort families & routing transparency

- `_LOW_QUALITY_RE` (nemotron ANY variant, gpt-oss, gemma) is a chain-ORDERING
  rule, not a score: AA scores (`_aa_score_for`) override the Tier-C demotion
  and `_TOOL_PROVEN` still names nemotron/gpt-oss, so scores alone always
  landed the chain on them. `_build_chain` and `_route_by_difficulty`
  partition them to the TAIL — after every other alive candidate (and after
  every tool-proven normal candidate for tool requests). Only `simple`
  difficulty may route to them while something stronger lives. Ordered last,
  never deleted. kimi-k2.6/k2.7 hold preference floor 133 (`_PREF_FLOORS[4]`,
  just under k3's 134), matching all id shapes (`@cf/moonshotai/…`,
  `moonshotai/…`, bare). Covered by `tests/test_last_resort_routing.py`.
- **Shipped default blocklist** (`_DEFAULT_BLOCKED_IDENTITIES`): blocks the
  gpt-oss and nemotron families (plus a handful of other weak ids) for EVERY
  install. `_seed_default_blocks` adds them once per install into the editable
  `blocked_identities` setting and records what it offered, so a family the
  user unticks is never re-added, while a family added to the shipped set in a
  later release still arrives on the next boot. Blocked means never routed;
  the last-resort TAIL ordering above still applies to every family that is
  NOT blocked (gemma, or a nemotron the user unticked). Covered by
  `tests/test_default_blocklist.py`.
- First-content peek is adaptive (`_stream_peek_timeout`): slow/reasoning
  models or >=12K-token requests get 60s (both: 90s) instead of the flat 35s —
  the flat budget was killing HEALTHY slow hops on Codex-sized prompts.
- One wall clock per request (`_ChainClock`, setting
  `request_deadline_seconds`, default 240, 0 = off) on all three chain loops:
  no hop starts past it, hops/peeks are cut to what is left, a chain that runs
  out returns a clean 504 (`X-Free-LLM-Hub-Last-Error: deadline`), and a
  committed stream past it continues only while it delivers visible content
  (`_deadline_guard`). Trivial small turns (<12K tokens, classifier "simple")
  get a 25s/45s (fast/slow) hop budget and a fast-first chain that leaves an
  all-slow category for fast models. Pipelines keep `swarm_max_seconds` inside
  an outer bound (`_pipeline_outer_bound`). Covered by
  `tests/test_request_deadline.py`.
- Trivial-turn speed (`tests/test_trivial_speed.py`): a (pid, model) that
  429'd or ran out its time in the last 10 min (`_recent_hop_fail`,
  `_RECENT_FAIL_TTL`) is out of the primary pick and at the TAIL of every
  chain (kept, never dropped; not for a pinned model). The 25s/45s budgets are
  now ceilings: a measured hop gets max(6s, 3.5x p90) (`_adaptive_hop_budget`).
  Trivial tool-free small turns HEDGE (`_ChainClock.plan_hedge`, flag
  `hedge_simple_turns`): after 1.5x p50 (min 3s) of silence the next candidate
  starts in parallel, first answer that passes answer_check wins, at most one
  extra call, nothing sent to the client before; the loops rebind their hop
  to `_clock.served(...)`. A short streamed answer whose `[DONE]` arrives as
  its own read is content, not "empty" (`_peek_until_content`), and a peek
  never cuts a hop already writing visible text (`content_grace`).
  `tests/conftest.py` clears the ledger between tests.
- `X-Free-LLM-Hub-Last-Error` response header (timeout/conn/413/429/http-N/
  empty/none) names the last hop-failure class; on `/v1/responses` it appears
  on chain-exhausted errors. Diagnosis is one `curl -i` away.


## Hidden run & sticky stop (hub lifecycle)

- `run-hidden.vbs` starts `run.bat` with no console window (WScript.Shell Run,
  window style 0), so closing any terminal can never kill the hub. Both
  autostart mechanisms (Startup-folder launcher and the 5-minute self-heal
  Scheduled Task, installed by `run.bat autostart`, which forwards to
  `scripts/autostart.bat`) call it as
  `run-hidden.vbs supervised`.
- Dashboard stop (`POST /api/runtime/stop`) writes the flag
  `state_dir()/intentional-stop` (`config.set_intentional_stop`). A user stop
  is STICKY: while the flag exists, `run.bat` under `HUB_SUPERVISED=1` refuses
  to start — self-heal and logon launches become no-ops and never resurrect a
  user-stopped hub. An explicit user action clears the flag and runs: the
  desktop shortcut, plain `run.bat`, or `python app.py`
  (`_mark_runtime_started` also clears it on boot).
- `POST /api/hub/desktop-shortcut` (control-token gated like every `/api/*`)
  creates a Desktop shortcut pointing at `run-hidden.vbs` (`.lnk` via
  PowerShell WScript.Shell COM, `.bat` fallback); returns `{ok, path}`.
  `GET /api/hub/stopped` returns `{stopped: bool}`.
- Covered by `tests/test_hub_lifecycle.py`.

## Liveness / readiness probes (2026-10-08)

Idea from PR #4, rewritten. `GET /health` + `/healthz` (liveness: always 200,
`{status, uptime_seconds}`, monotonic clock) and `GET /ready` + `/readyz`
(readiness, `_hub_readiness`: 503 `{status: not_ready, reason}` with reason
draining | stopped | no_provider | error; ready = /v1 accepted and an enabled,
usable, NON-paid provider exists). Outside `/api/*`, so no control token; the
loopback Host/Origin guard still applies. They carry NO version, release or
provider id (the hub shows those to token holders only). `Cache-Control:
no-store`; HEAD works. Covered by `tests/test_health_probes.py`.

## Puter zero-manual connect (dashboard)

The `puter` card (Recommended zone) has a "Connect with Puter" button —
browser-side, no backend endpoint and no credential handling. It replicates
puter.js v2's own sign-in contract (verified live against
https://js.puter.com/v2/ on 2026-07-31): popup to
`https://puter.com/action/sign-in?embedded_in_popup=true&msg_id=N`, then the
Puter GUI postMessages `{msg:"puter.token", msg_id:N, token, success:true}`
to the opener from origin `https://puter.com`. `connectPuter()` in
templates/index.html validates origin + msg_id, then saves the token via the
existing `POST /api/providers/puter/keys` (dedupe + auto-enable). The raw
key-paste field stays as manual fallback. An expired token self-heals via the
existing 401 → `_provider_authfail` sideline (app.py:1570). There is NO
server-side Puter login endpoint — `api.puter.com/login` and `/auth/login`
404 (probed 2026-07-30); only `/auth/get-user-app-token` exists and needs a
token already. Covered by `tests/test_puter_connect.py`.

Two live-verified gotchas, both fixed 2026-07-31 — do not "simplify" either
away:

- **The popup renders BLANK without an origin handshake.** The Puter GUI's
  `initgui()` needs the opener's origin before rendering. It reads
  `document.referrer`, which is ALWAYS empty here because the hub sends
  `Referrer-Policy: no-referrer` on every response (app.py:4160). Its fallback
  is to postMessage `{msg:"requestOrigin"}` to `window.opener` and wait 5s for
  a reply whose event `.origin` it adopts; with no reply it throws
  `Error: No referrer found` and nothing ever renders. puter.js answers from an
  always-on top-level listener — so index.html registers one too (at load, NOT
  inside `connectPuter()`), replying `{msg:"originResponse"}` to `e.source`.
- **Puter is NOT OpenAI-compatible for our tokens — it is driver-based.**
  `POST <base_url>/chat/completions` with a real popup token answers
  `403 "This endpoint is only available to user sessions"`: that surface wants a
  browser SESSION, and the popup hands out an APP token. Everything therefore
  goes through `POST https://api.puter.com/drivers/call`, which is what puter.js
  itself calls. `driver_api: "puter"` on the registry entry selects the adapter
  (`_puter_chat`, branched inside `_upstream_chat` so the key test and model
  probe take the working path too). Verified live: chat 200, `stream:true`
  returns `application/x-ndjson` (`{"type":"text","text":…}` per delta, final
  `{"type":"usage",…}`) which `_PuterStreamResponse` translates to OpenAI SSE.
  Tool requests are deliberately buffered, not live-streamed — the driver's
  streamed tool-call event shape is unverified and guessing it would silently
  drop `tool_calls`. `base_url` is kept for documentation only; nothing posts
  to it.
- **Text-to-image works; image-to-image does not.** Same driver endpoint,
  `interface: "puter-image-generation"`, `method: "generate"`, args `{prompt}`
  → `{"success":true,"result":"data:image/png;base64,…"}` (1024x1024, C2PA-
  signed). Gotcha: naming `driver: "ai-image"` makes `model` MANDATORY
  (400 "Missing `model`") and an unknown model is 400 "Model not found: X" —
  omitting BOTH is the only combination verified to return a PNG, so the
  registry row's id `ai-image` is a sentinel meaning "send neither". Puter
  publishes no image catalog (the chat catalog has zero image-output models and
  neither JS bundle names one). puter.js binds txt2img/txt2vid/img2txt/
  txt2speech/speech2txt/speech2speech — there is **no img2img**.
- **Text-to-video exists but is PAID.** `interface:
  "puter-video-generation"` (driver `ai-video`, args `{prompt, seconds}`)
  returns `402 {"code":"insufficient_funds"}` on a free account, even with
  `test_mode: true`. No `img2vid` / `vid2vid` interface exists in either
  bundle. Do not wire Puter video into a free-tier rotation.
- **`<base_url>/models` does not exist.** `models_url` is
  `https://api.puter.com/puterai/chat/models/details` (the route puter.js
  itself calls: public, 200, 563 models, `{"models":[{"id":..}]}` — a shape
  `_parse_model_ids` already accepts). `/puterai/openai/v1/models` returns 404
  `not_found` WITH a valid bearer too, and the key test aborts on any non-200
  from `models_url` (app.py ~4884), so every Puter Test failed
  "✗ HTTP 404: Not Found" before reaching the generation probe. The catalog
  route needs no auth, which is fine: the test ALWAYS follows the listing with
  a real generation call. `POST <base_url>/chat/completions` is real (401s a
  dummy bearer), so `base_url` itself was always correct.

## Kimi Code: one-click Connect / Disconnect

`_autofix_kimi` / `_disconnect_kimi` (registered under the `"kimi"` strategy in
`_AUTOFIXERS` / `_DISCONNECTERS`) replaced the manual-only TOML instructions on
2026-07-31. Kimi has NO shell-env fallback, so the whole wiring is
`~/.kimi/config.toml`: Connect writes `[providers.free-hub]` + `[models."auto"]`
and sets top-level `default_model = "auto"`; Disconnect strips exactly those and
restores the previous `default_model` (remembered in the `kimi_prev_default_model`
setting — normally Kimi's managed `kimi-code` OAuth service). Both reuse the
generic `_remove_toml_table` / `_backup_once` helpers, so unrelated tables the
user added after connecting survive a revert. `manual_note` is kept as fallback.
Covered by `tests/test_kimi_cli.py` (round-trip + idempotence + no-key-echo).

**Which file (2026-09-27, measured on kimi-code 0.39.1):** the `kimi` on PATH
is the Node Kimi Code (npm `@moonshot-ai/kimi-code`), which reads
`KIMI_CODE_HOME` else `~/.kimi-code/config.toml` — NOT `~/.kimi`. Writing only
`~/.kimi` left `kimi -p` at "No model configured" while the card said
Connected. `_p_kimi()` is now the Kimi Code file (legacy `~/.kimi` only when
it is the sole config); `_kimi_config_paths()` adds the legacy kimi-cli file
when it exists, and Connect/Disconnect act on all of them (per-file previous
default under `_kimi_prev_setting(path)`). Connect writes one alias per tier
(`_HUB_TIER_IDS`: auto/best/swarm); a user alias with a tier's name is kept.
A Connect-created file is deleted on Disconnect. Covered by
`tests/test_kimi_code_home.py`. MCP: `mcp_manager` writes kimi servers to
`<KIMI_CODE_HOME or ~/.kimi-code>/mcp.json` as `{"mcpServers": {name:
{"transport": "http"|"stdio", ...}}}` (Kimi Code's own zod schema); a machine
with only the legacy `~/.kimi/config.toml` keeps `[mcp_servers.*]`. Legacy TOML
entries stay listed and are removed too. Covered by `tests/test_protocol_misc.py`.

## Protocol leftovers (2026-09-27, `tests/test_protocol_misc.py`)

- Hermes Connect also writes `providers.free-llm-hub` (`api` = model.base_url,
  `models.<tier>.context_length`); Hermes matches it to the route by URL
  (`get_custom_provider_context_length`). Disconnect removes it.
- `/v1/responses` never emits a nameless `function_call`: named from its
  arguments when exactly one offered tool fits (`_infer_tool_name`), else
  dropped; a peek whose only tool call stays nameless is `empty` (next hop).
  Headers `X-Free-LLM-Hub-Provider/-Model` + `metadata.free_llm_hub_provider/
  _model`; `model` still echoes the client's id (verified with codex 0.154).
- 400 "not currently offered" (dahl) = `_NOT_OFFERED_TTL` (60 s) routing skip
  (`_is_model_skipped`), no reliability failure, no dead mark, never relayed.
- `_is_slow_model`: measured TTFT (>= 5 samples: p50 >= 17.5 s slow, p95 <
  17.5 s fast) before `_SLOW_MODEL_RE`; drives the peek and trivial budgets.

## Config-only CLIs: tiers + windows in the documented file (2026-09-27)

Covered by `tests/test_other_cli_formats.py`. qwen also gets the documented
`~/.qwen/settings.json` setup (`modelProviders.openai` one entry per tier with
`generationConfig.contextWindowSize`, `security.auth.selectedType = "openai"`,
`model.name = "auto"`), reverted key by key against its Connect-time backup.
openclaw lists every tier (+ allowlist) and a Connect-created file is removed.
aider uses `openai/auto` (never the concrete `model` argument) plus
`~/.aider.model.metadata.json` windows. Hub mode OFF byte-restores only
write_path, so `_revert_side_files` cleans these second files.

## AgentRouter: removed (2026-07-31)

Both halves are gone at user request: the `agentrouter` provider entry
(providers.py) and the `sub-agentrouter` isolated-CLI relay (`_SUB_PROVIDERS`),
plus `_agentrouter_backend`, `_agentrouter_review_and_fix` and its 4 call sites,
the "AGENTROUTER FIRST" pre-free-tier routing block in `_route_by_difficulty`,
and the `codex-agentrouter`/`claude-agentrouter` isolated-CLI ids.
`_PROVIDER_RELAY_SUB_PID` is now `{}` — the machinery is generic and stays for a
future relay. Re-probed on removal day: agentrouter.org answers 200 but
`/v1/models` still returns 401 "unauthorized client detected" to any generic
HTTP client (their WAF only accepts the official Claude Code CLI fingerprint),
so the direct provider never worked; the relay that worked around it had a probe
hang for 240s. Do not re-add without new evidence that policy changed.

## Opening-prompt enhancement + "best except trivial" routing

Both added 2026-07-31 at user request; covered by `tests/test_prompt_enhance.py`.

- **Routing**: `medium` now joins `hard` on the strongest-model branch of
  `_route_by_difficulty`; only `simple` still takes the cheap
  `_DIFFICULTY_FLOOR` pick. `simple` is one-word replies, classification and the
  hub's OWN probes, so leaving it cheap costs no quality and keeps strong
  providers alive for real work.
- **`_enhance_prompt(text, kind)`** rewrites the OPENING prompt only, and only
  for prompts typed in the dashboard — `/v1/*` traffic is never touched
  (rewriting a turn carrying `tool_calls` or a diff breaks the agent loop).
  `POST /api/enhance-prompt`; switch at `POST /api/prompt-enhance`, flag
  `prompt_enhance`, default ON.
- **It routes with `force_difficulty="medium"`, and that is load-bearing.**
  MEASURED: routed as the `simple` its text classifies as, the rewrite landed on
  groq/allam-2-7b, which ANSWERED "fix my python bug" ("Please provide the
  specific bug...") instead of rewriting it — silently replacing the user's
  question with an assistant reply. `_ENHANCE_ANSWERED_RE` is the second line of
  defence against the same failure; a hit skips that hop.
- **The image enhancer clarifies, it does not art-direct.** An earlier version
  turned "a fox" into "whimsical … warm orange tones, soft diffused lighting".
  It must never invent style, medium, mood, lighting, palette, camera or
  setting the user did not state — returning the prompt unchanged is the
  expected common case. Both system prompts also ban the usual slop
  ("masterpiece, 8k", "act as a world-class expert").
- Fail-open everywhere: any error, non-200, empty or runaway output returns the
  ORIGINAL text, and the UI always shows what was sent with a revert link.

## Agent skills (.agents/skills/)

Kimi Code scans `.agents/skills/` (directory form `<name>/SKILL.md`). One
vendored skill lives here:

- `last30days/` (MIT, github.com/mvanhorn/last30days-skill v3.18.4,
  MODIFIED — see its `VENDORED.md`) — last-30-days web research, keyless by
  default (web + YouTube + public Reddit). X/social/authenticated sources are
  gated: the skill curls `GET /api/web-search-policy` (open read — token-exempt;
  only the POST `{social_search: bool}` that sets it is control-token gated) and only uses
  social sources when the dashboard Settings switch "Social media web search"
  is on. The switch persists as the `social_web_search` flag in config.py
  (default false). Covered by `tests/test_skills_policy.py`.

As noted above, graphify's `post-commit` hook must stay installed so the
graph rebuilds after each commit.

## Crews (specialized swarm variants)

`crews.py` exposes five VIRTUAL model ids (`crews.CREW_IDS`), usable anywhere
`swarm` is usable (same one-shot stream behaviour, tool-carrying turns still
refused with 400): `crew` (auto-detects which crew from the request text via
`crews.detect_crew`), `crew-code` (planner splits by component, workers
implement, a hard-to-please senior reviewer checks correctness/edge cases),
`crew-research` (planner splits into sub-questions, workers answer from model
knowledge only — no web access exists — and the reviewer hunts invented
facts/figures), `crew-write` (structure/draft/polish split) and `crew-design`
(web/design work; worker system prompts get `craft.WEB_DESIGN` appended).

They reuse the swarm pipeline unchanged: `swarm.run(messages, dispatch,
profile=None)` takes an optional profile dict of stage system-prompt overrides,
extra worker system-prompt text and `max_revisions`. `max_revisions=1` runs ONE
bounded revision pass on a "revise" verdict — a worker is shown the draft plus
the reviewer's problems and fixes them, then synthesis runs (the Claude Code
style plan→do→review→fix loop); `0` reproduces the old behaviour exactly
(revise verdict only folded into synthesis), so `profile=None` is fully
backward compatible and `tests/test_swarm.py` stays green untouched.
`crews.run` returns the same result-dict shape as `swarm.run`;
`crews.format_answer(result)` renders it. The dashboard quick-chat picker lists
the five ids in a "crews (multi-agent)" optgroup (hardcoded in
`templates/index.html` — virtual ids never appear in `/api/models`). Covered by
`tests/test_crews.py`.

Hardening learned live 2026-08-06 (all in `_swarm_dispatch`, app.py):

- `_dispatch_chat_with_deadline` gives every stage hop an OVERALL deadline
  (`_SWARM_HOP_DEADLINE = 150s`). Non-streaming hops had only requests'
  per-recv timeout (CHAT_READ_TIMEOUT=300s), which a provider trickling
  keepalive bytes resets forever — tokenrouter/kimi-k3-free held a stage
  24+ min. Hung/failed hops feed `_record_outcome(..., False)`, so later
  stages route around them; successful hops now record usage + reliability
  via `_record_chat_usage` like every other endpoint.
- A hop answering `finish_reason: "length"` (provider completion cap —
  kilocode/hy3 cut a synthesis mid-attribute, shipping broken HTML) is
  skipped for the next hop; the longest partial is the fallback if every
  hop truncates.

Escalation & agent self-delegation (user request 2026-08-06, covered by
`tests/test_crew_auto_escalate.py` + `tests/test_chat_project_gate.py`):

- **Dashboard project gate**: an opening-turn Auto message that
  `looksLikeFullProject()` (index.html) flags gets an inline "🐝 Full crew /
  ⚡ Just answer" chooser before sending — once per conversation, Auto only.
- **API auto-escalation**: clients have no human to ask, so
  `/v1/chat/completions` routes a tool-free, image-free, single-user-turn
  `auto` request that `crews.looks_like_full_project()` flags straight to the
  crew pipeline (flag `crew_auto_escalate`, default on). Tool-carrying turns
  and explicit `<pid>/<model>` are never touched. The Python heuristic must
  stay in sync with its JS twin.
- **Agent hint**: `_apply_craft_brief(..., agentic=True)` (payload carries
  tools) appends `_CREW_AGENT_HINT` on the opening turn — tells Codex/Claude
  Code/hermes/openclaw that crews exist and to call them with a tool-free
  `model: "crew*"` request, so the agent itself decides per task (flag
  `crew_agent_hint`, default on).
- The prompt enhancer's hops got the same overall-deadline treatment
  (`_ENHANCE_HOP_DEADLINE = 45s`) plus a 20s client-side AbortController —
  an "enhance" that pended 150s+ behind a locked composer was the
  "clicked Just answer and nothing happened" bug.

Subscription manager in the prose swarm/crews (covered by
`tests/test_swarm_manager.py`): `swarm.run`/`crews.run` take `manager=None`;
app.py passes `_swarm_manager_kwargs()` (only when `_manager_enabled()`). The
manager PLANS, SUPERVISES, REVIEWS, gives each worker output a ~300-token
verdict (after free checks: empty, `answer_check.inspect`, quoted-literal /
word-count / JSON-HTML format tests) and FIXES; workers, gap repairs and
synthesis stay free. A failed phase retries once on another free provider
(exclude_pids), then the manager writes it. Manager prompts are clipped
summaries only; "" from it = that stage uses free dispatch. Result gains
`manager_tokens` (+ `review_warning` when the review stayed unreadable after
one re-ask); both land on the activity row. `manager=None` = old pipeline.

## MCP: the hub as a tool server (2026-08-06)

The hub itself speaks MCP so any MCP-capable agent CLI (Kimi Code, Codex,
Claude Code, OpenCode) can call the crews as NATIVE tools instead of
hand-rolling a `model: "crew*"` chat request (which the `_CREW_AGENT_HINT`
above only suggests).

- **`POST /mcp`** is a JSON-RPC 2.0 endpoint with three tools:
  - `crew_run` — SYNCHRONOUS: runs the whole crew pipeline and returns the
    formatted answer. A full run takes 5-20 min, so this only suits clients
    whose tool-call timeout is that generous.
  - `crew_start` / `crew_result` — the ASYNC pair for short-timeout clients:
    `crew_start` kicks the run off in the background and returns a job id
    immediately; the client polls `crew_result` with that id until done.
- **Per-CLI management routes** (control-token gated like every `/api/*`):
  - `GET /api/mcp` → `{<cli_id>: [{name, transport, command?, args?, url?}],
    errors: [...], hub_mcp: {url, name}}`. Per-CLI read errors land in
    `errors` and are NON-fatal — one unreadable config never hides the rest.
  - `POST /api/mcp` `{cli, name, spec:{command?,args?,env?} | {url}}` → add.
  - `POST /api/mcp/delete` `{cli, name}` → remove.
  - `POST /api/mcp/install-hub` `{cli}` → adds the hub's own `/mcp` endpoint
    to that CLI (the dashboard's "⚡ Enable hub crews in this CLI" button).
- **`mcp_manager` writes each CLI's native config**: kimi and codex get
  `[mcp_servers.<name>]` tables in their `config.toml`, claude gets
  `mcpServers` in `~/.claude.json`, opencode gets `mcp` in `opencode.json`.
  The Kimi Connect/Disconnect wiring (`_autofix_kimi` / `_disconnect_kimi`)
  strips only its own `[providers.free-hub]` / `[models."auto"]` tables, so
  `[mcp_servers.*]` survives a disconnect — installed crews stay installed.
- Dashboard: an "MCP servers" block lives in Hub controls
  (`#lifecycle-mcp`, next to the CLI cards in `templates/index.html`) —
  per-CLI server list with Remove, an inline add form (name + command OR
  url), and the install-hub button. It refreshes after every mutation.

## Categories x effort tiers (every CLI)

- **Codex has 4 effort levels** (`_CODEX_EFFORT_MODEL` / `_CODEX_LEVELS`):
  `low` = Normal (`auto`), `medium` = Max (`best`), `high` = Swarm (`swarm`),
  `xhigh` = Multi (`multi`). Codex picks the CATEGORY as its model id and the
  tier as its reasoning level; `_mode_and_effort` splits the pair.
- **Compound ids** `"<category>-<effort>"` (also `/`), e.g. `coding-swarm`,
  pick both in one model id — what the opencode picker offers, and usable from
  any protocol. `_split_category_effort` only splits when the head is a real
  category (`_mode_keys()`) and the tail a real tier, so the crew ids
  (`crew-code`) are never mistaken for one; `_apply_category_effort` sets
  `g.model_mode` and rewrites the model to the bare tier.
- A bare category id (`coding`) restricts the pool to that category on
  `/v1/chat/completions` AND `/v1/messages` (`ANTHROPIC_MODEL=coding`) —
  covered by `tests/test_messages_category_mode.py`.

## Disconnect leaves no hub trace (2026-09-26)

- The codex `/model` catalog (`~/.codex/model_catalog.json`) is written by
  `_refresh_codex_catalog` ONLY while `_codex_wired_to_hub()`; a disconnected
  codex gets nothing written and a hub-written catalog removed
  (`_codex_disconnect_catalog`: user's own backup restored, else file + a
  `model_catalog_json` key naming it deleted; a non-hub catalog is never touched).
- Every `_disconnect_*` also drops hub-only model ids the CLI's own picker saved
  (`_is_hub_virtual_model`: codex `model`, claude `model`/settings.local.json,
  opencode `model`/`small_model`/`agent.*.model`, qwen settings.json, pi
  settings.json defaults) and restores the pre-hub default from the
  Connect-time `.freehub-bak` (never from a hub-wired backup).
- `_repair_opencode_config` is a no-op without a `free-llm-hub` provider block.
- The hub MCP server entry (crews) is KEPT by design and reported as
  `mcp_kept` in the disconnect response. Covered by
  `tests/test_cli_disconnect_leaves_no_trace.py`.

## Shared env vars are not a CLI's connection (2026-09-27)

Covered by `tests/test_shared_env_vars.py`.

- The hub never writes the persistent environment; the only source of a hub
  URL in HKCU\Environment is the user running the `setx` block from
  `_env_commands` (`/api/clis/<cid>/instructions`), which records `cid` as an
  owner (`_env_record_owner`, config key `env_var_owners`). A var with no
  record is seeded with every CLI whose `env_check` lists it.
- `_CONFIG_WIRED_CLIS` (claude, pi, opencode, codex, qwen, openclaw, hermes,
  kimi) are connected ONLY by their config file; a shared var gives
  `env_vars` + `env_note` on the card. Env-only CLIs (aider, llm,
  cursor-agent) still count the var as their connection.
- Disconnect (`_env_release_on_disconnect`) removes a var only when no other
  installed, still-connected owner reads it; otherwise the response's
  `env.kept[].used_by` names them. `GET /api/env/hub-vars`,
  `POST /api/env/remove {name, confirm}` (no confirm = preview of the tools it
  reaches; refuses values not pointing at the hub and protected names).
- `userenv.py` is the only registry access (winreg + WM_SETTINGCHANGE, no child
  process). The root conftest swaps in `userenv.MemoryBackend` for every test
  and fingerprints the real vars in the tripwire.

## Context-window management (every CLI, every protocol)

Pure helpers live in `ctxwin.py`; the glue is in app.py. Covered by
`tests/test_context_window_management.py` (plus the older
`test_context_refit.py` / `test_compaction_actually_fits.py`).

- **Compaction** (`_compact_to_budget`): a tool call and its results are ONE
  unit (`_message_units`) — kept or dropped together; an oversized result is
  truncated head+tail with an "omitted by the hub" marker, never orphaned. The
  LATEST real instruction (`ctxwin.is_real_instruction` skips Claude Code
  `<system-reminder>` and Codex AGENTS.md/`<environment_context>` blocks) is
  pinned in full; the original request rides as a short excerpt. `reserve`
  (= `_output_reserve`: max_tokens, capped at 25% of the window) comes off the
  target, and `_upstream_chat` clamps max_tokens to what is left of a KNOWN
  window and to a learned output cap.
- **Windows** (`_model_ctx_info`): per-model (catalog or learned) beats the
  `_PROVIDER_TPM` row, except `_PROVIDER_HARD_REQUEST_CAP` (groq: a real
  per-request TPM cap); unknown = "default" guess, which never signals
  overflow nor clamps max_tokens. Learned limits expire after
  `_LEARNED_CTX_TTL` (7 days) back to the catalog figure. Output caps
  ("max_tokens must be <= N") go to `_MODEL_MAX_OUTPUT`, never the input table.
- **Usage to the CLI** is sized on the ORIGINAL request
  (`_reported_prompt_tokens`: upstream count x this hop's compaction ratio, or
  the estimate when upstream sent none) on all three protocols, stream and
  non-stream; translated streams ask upstream for `stream_options.include_usage`
  (a provider that rejects it is retried without and remembered). This is what
  lets codex (~96K) / opencode / Claude Code compact at the right time.
- **Overflow signal** (setting `context_overflow_signal`, default on, /v1 only):
  a hop that would drop >30% of the history raises `_ContextOverflow` (chain
  walks on); if nothing holds the request the CLI gets its native error —
  OpenAI 400 `context_length_exceeded`, Anthropic 400 "prompt is too long",
  Responses stream `response.failed` with that code. Never for a CLI's own
  compaction request, never on a guessed window.
  - A hop that failed otherwise blocks that reply only while a SHORT wait
    could let it serve (short 429, 5xx, timeout, conn, empty 200 on a model
    whose window could hold it). It does not block when its KNOWN window is
    < est x1.15+512, it is out >= `_CTX_OVERFLOW_LONG_WAIT` (300 s: day
    quota, parked, dead) or it refused with a non-retryable non-context 4xx
    (`_ctx_others_cannot_serve`; per-hop outcome via `_ctx_note_hop_result`
    in `_ChainClock.dispatch`). MEASURED 2026-10-03: a 326K OpenCode turn
    503'd forever while gemini (1M) sat on a spent day quota. Covered by
    `tests/test_overflow_when_big_models_are_out.py`.
  - `_model_ctx_info` applies `_PROVIDER_REQUEST_TOKEN_CAP` (google 250K:
    the free tier's input tokens per minute, spent by ONE bigger request) on
    every source, so routing, this reply and the declared/live windows agree
    a 300K request does not fit on gemini (it used to take a 30-250 s hop
    that 429'd and blocked this reply as a "short wait").
- **Rolling recap**: one per conversation, keyed by `ctxwin.conversation_key`
  (hub agent session, `X-Claude-Code-Session-Id` / Claude Code
  `metadata.user_id`, OpenCode `X-Session-Id`/`x-session-affinity`, Codex body
  `prompt_cache_key` — werkzeug DROPS underscore headers like codex's
  `session_id` — else a hash of system + first real instruction), extended
  incrementally, persisted in `state_dir()/compaction-recaps.json` (LRU 500,
  30-day TTL).
- Category modes are applied BEFORE the size re-admission
  (`_mode_first_size_split`); agentic chains put models known to hold much
  less than `HUB_CONTEXT_WINDOW` behind the others (`_below_declared_window`).
- **Window detection** (`_window_info` -> catalog | learned | inferred |
  reference | default; covered by `tests/test_model_window_detection.py`):
  `_catalog_row_ctx` reads every common field shape (vLLM `max_model_len`,
  Google `inputTokenLimit`, `top_provider`, `limit(s).context`, Cloudflare
  properties...); Google's native list is harvested via the registry's
  `ctx_models_url`. Unknown on one host -> the lower median of other
  catalogs + OpenRouter's public catalog for the same identity ("inferred";
  router aliases like `auto` never), else the `_CTX_REFERENCE` family table.
  Inferred/reference only LOWER a `_PROVIDER_TPM` row; a 400/413 still wins.
  `/api/model-windows` + `/api/tracking` (`ctx_window`/`ctx_source`) report it.
- **Declared windows** follow the fleet: `agentic_chat.declared_window(id)`
  (app registers `_declared_window_for` at startup: P25 of known windows of
  alive tool-capable candidates above the medium floor, hard floor for
  best/max, clamped 32K..1M; fixed 128000 when unregistered or too few
  known) sizes opencode `limit.context`, the codex catalog + fallback,
  Pi, openclaw and Kimi.
- **Declared = what 3 providers hold** (2026-10-03, MEASURED: a ~326K-token
  OpenCode session got 503s because coding-max/-multi were declared 500000 —
  Gemini flash variants + g4f copies, all 1M on ONE exhausted Google quota,
  outnumbered the rows). The P25 is now capped at the 3rd-largest window
  among DISTINCT NON-RELAY providers (`_declared_provider_windows`: one
  largest window per pid, `_is_relay_pid` never counts; fewer providers =
  the window every one holds; only relay windows known = None);
  `_DECLARED_MIN_PROVIDERS` = 3. Pinned `<pid>/<model>` unchanged.
  `_resync_declared_windows` rewrites ONLY the hub's own window fields of
  CLIs still wired to the hub (opencode real + isolated seed — a user's
  (context, output) pair that breaks the hub's shape stays —, codex catalog
  via `_refresh_codex_catalog`, claude settings env, pi, qwen, openclaw,
  aider metadata, hermes, kimi), no new `.freehub-bak`; boot pass after the
  warm-up, then `_declared_resync_loop` every 30 min when the figures
  changed. conftest stubs it. Covered by
  `tests/test_declared_window_providers.py`.
- **Live window steering** (2026-10-03, owner: "context-max should be
  automatic"; `tests/test_live_window_steering.py`). CLIs PROVEN from source
  to compact from the usage the hub reports (`_STEERED_CLIS`: opencode,
  codex, claude, qwen, kimi, pi, openclaw, hermes -- file + what it reads in
  the comment at the site; aider counts locally and is NOT steered) are
  declared the REACH window (`_reach_window_for`: largest known window among
  the tier's non-relay providers) via `agentic_chat.declared_window(mid,
  cli=...)`; Claude Code keeps the safe figure (it caps AUTO_COMPACT_WINDOW
  at its behavesAs model's 200K). Per request `_ctx_steer_pair` = (the
  window that CLI was declared for the id it SENT, `_cli_declared_window`;
  live = `_live_window_for`: biggest window usable now -- not dead/parked/
  out >= `_CTX_OVERFLOW_LONG_WAIT`, relays never). live < declared ->
  `_reported_prompt_tokens` reports `real * declared / live` (never below
  real; under 25% of live honest), all three protocols, stream + non-stream.
  Never a CLI compaction request; flag `context_live_steering`. CLI = /agent
  registry entry, else User-Agent; UA-less (pi/openclaw/hermes) only when
  the connected ones' figures agree (`_steer_connected_unidentifiable`,
  stubbed in conftest). google counts at most 250000 in reach/live
  (`_PROVIDER_REQUEST_TOKEN_CAP`: free-tier input TPM per model, every
  >=250K request 429'd in hub.log). Codex: catalog slug -> its entry, else
  its fallback metadata 272000. OWNER DECISION: the reach is capped at
  `_REACH_WINDOW_CAP` = 400000 (above ~262K only one daily-limited free model
  holds the conversation, each turn re-sends it, models degrade with length);
  the live window is not capped, so nothing is steered while that model is up.

- **A CLI's compaction is one summary, never a pipeline** (2026-10-04,
  `tests/test_cli_compaction_never_runs_a_pipeline.py`). MEASURED: opencode's
  prompt ("Create a new anchored summary ..." / "Construct a new summary that
  combines both") matched no `ctxwin._COMPACTION_REQUEST_RE` spelling, so on
  `coding-multi` each compaction ran plan + phases + review + synthesis
  (300-600 s; opencode stuck on "compaction"). The regex now also knows
  kimi-cli and gemini/qwen (system prompt) spellings; `is_compaction_request`
  checks the system prompt even when a user message exists;
  `_swarm_fast_path` returns True for any compaction (one strong model, flag
  ignored) and crew auto-escalation skips it.

## Pipelines keep the conversation (2026-09-27)

Covered by `tests/test_pipelines_keep_the_conversation.py`.

- **Swarm / crews**: `swarm.conversation_brief(messages, context)` = the last
  user message (unclipped) + a bounded block (<= 6000 chars): caller context,
  the previous answer when the request refers back, earlier requests. A
  one-message conversation with no context is byte-identical to before.
  `swarm.run(..., context=)` / `crews.run(..., context=)`; `_swarm_completion`
  passes `_pipeline_context()` = the per-conversation rolling recap
  (`ctxwin.conversation_key`, same key as compaction) + the /agent session's
  memory block. Manager plan/review see <= 1500 chars of it. Workers see the
  user brief (<= 6000). Synthesis max_tokens scales (cap `SYNTH_MAX_CAP`,
  lowered per hop to `_model_output_cap`).
- **Manager-less revision is directed** (`_free_revision`): free instruct call
  -> free apply on the FULL draft, or per phase when too long for one apply.
  Never a clipped rewrite.
- **Multi**: `swarm_windows.start(context=)` (planner, every worker, manager
  plan + verdicts; persisted). A short "continue" after a run with unfinished
  phases calls `swarm_windows.resume()` on THAT run (only non-DONE phases +
  review) instead of planning "continue". `_multi_context` = memory block +
  recap + previous run result.
- **Facts**: `memory.harvest_facts` after every finished /agent turn and multi
  run, no model call: Decision/Note/Constraint and Must/Never/Always lines,
  user preferences, ONE rolling files fact, ONE rolling verified-commands
  fact, into the project scope. Flag `memory_fact_extractor` (default off)
  adds a cheap free-model pass.
- **Quick chat** sends `X-Conversation-Id: quick-<chat id>`; its recap is
  dropped with the chat (`ctxwin.quick_chat_key`).

## Thinking models & truncated stubs (2026-09-27)

Covered by `tests/test_thinking_budget.py`. Live: gemini-3-flash at
max_tokens 40 answered "94" for 2994 (finish "length", completion_tokens 1 —
hidden reasoning ate the budget).

- **Who thinks**: `_thinks_by_default` = `_SLOW_MODEL_RE`, the docs table
  `_THINKING_DOC_RE` (Gemini 2.5/3.x non-lite, gpt-5, o-series, MiniMax-M),
  or runtime evidence (`_THINKING_LEARNED`: reasoning_tokens > 0, reasoning
  text, a starved/stub reply; 7-day TTL). `_can_think` adds catalog flags
  (`_catalog_row_thinks`: `reasoning`/`thinking: true`, capabilities,
  `supported_parameters`), harvested in `_learn_ctx_from_catalog` and
  OpenRouter's public catalog (identity-level).
- **`_apply_reasoning_effort(payload, model, diff, pid=)`**: a caller budget
  < 8192 gets `+_THINKING_ALLOWANCE[effort]` (low 1024 / medium 2048 / high
  4096); the caller's own budget rides in the private key
  `_hub_caller_max_tokens`, stripped in `_dispatch_chat`/`_upstream_chat`.
  Effort is SENT only to default thinkers (LOW on a < 512 ask or a simple
  turn); a catalog-only flag earns room, not effort (it would switch a
  hybrid's thinking on). A 400 naming the parameter is retried without it and
  remembered per (provider, model) (`_REASONING_REJECTED`).
- **Stubs**: `_starve_kind` / `_is_truncated_stub` — finish "length" with
  visible text under half the CALLER's budget. One same-pair retry with room
  on all three protocols: non-stream `_starve_retry`, stream via the peek
  status `starved` + `_stream_starve_retry` (before any byte is committed).
  Filed as a failure only when the retry starves too. A stub never wins a
  hedge race. The caller still gets ~its visible budget
  (`_fit_visible_to_caller`, the stream gate's `visible_cap`).
- **Pipeline fast path** (`_swarm_fast_path` / `_is_trivial_ask`): swarm /
  crew* / multi / compounds answer a TRIVIAL ask with one strong model
  ('best'), with or without tools (tool turns only on a fresh user
  instruction, never mid-loop). Trivial = one short question / one-line
  request: never a write/create/build ask, never enumerated parts
  ("Include:", lists, 3+ joined items, > 2 sentences).

## Complete pipeline answers & the hub's own chain errors (2026-09-27)

Covered by `tests/test_pipeline_complete.py`.

- **Enumerated parts** (`swarm.required_parts`: "1) … 2) …", bullets under a
  "…:" line, inline "Deliver:/Include: a, b and c"): each must be covered by a
  phase (one re-ask of a FREE planner, then phases are added — with a manager
  they are added directly), present in the draft before review (mechanical
  markers `_part_present`, then one manager-or-free verdict; gaps go to a
  repair worker) and present after synthesis (restored from its phase).
- **Budget sized to the plan**: `swarm_max_seconds` (default 300) is the BASE;
  + `swarm_seconds_per_phase` (60) per phase past two + the manager's MEASURED
  latency x (waves + 3/4 stages), capped by `swarm_max_seconds_ceiling` (1200,
  bounded under `_PIPELINE_OUTER_MAX`). Past the cap, missing phases are
  finished in parallel by `_swarm_fast_dispatch` (quickest of the chain's
  strongest 8, 90 s hops) within `swarm_grace_seconds` (90).
- **Never silently partial**: anything still missing is appended as a
  "**Not finished:** …" note, `result["unfinished"]`, `unfinished=` first in
  `X-Free-LLM-Hub-Pipeline`, and `pipeline_unfinished` on the activity row.
  `plan=` in the header is the PLAN (`result["planned"]`), with `done=k/n`.
- **Chain exhausted** on all three protocols: `_chain_exhausted_text` (hop
  failure classes + last hard status), never an upstream body/id; the raw
  body goes to the CHAT/RESPONSES/MESSAGES-503 log line only.
- **Trivial turns**: a trivial TOOL chain takes <= 2 hops per provider before
  the others (`_spread_by_provider`), and `_ChainClock.walk` moves a provider
  that stalled a hop (budget/timeout/silent peek) behind every other one.
- Brevity trims (`_fit_visible_to_caller`, the stream cap) never cut inside a
  token (`_cut_splits_token`): "50648" is never served as "5064".

## Stream gate & budget tuning (2026-09-27)

- **Early release**: `_StreamAnswerGate` still holds up to 400 chars / 2.5 s,
  but releases as soon as `answer_check.reads_as_answer` passes (>= 80 chars,
  >= 10 varied words, no glued run, no `<think>`/`<|`/tool markup, NOT a
  number/word/yes-no brevity ask). Measured at 40 tok/s: first visible text
  2.52 s -> 0.62 s. Passing `hold_chars=` pins the old window (tests). After
  release a delta ending in a possible split leak marker ("<thi") is kept
  back one delta (`_marker_open`) so half a `<think>` never leaks.
- **Structured tails** (`answer_check._line_is_structured`: list items,
  pipe rows, `[WARN]`/timestamp/PASSED log lines, indented output,
  `key: value`, `>>>`/`$` prompts): a run is a loop only at the token cap
  (3 copies), past 16 mid-stream/mid-text, or past 50 at a natural stop.
  Plain prose keeps 5/8. A `=== 12 passed ===` banner is not a separator run.
  Covered by `tests/test_stream_tuning.py`.
- **Window aliases**: `_window_info` also tries `_ctx_alias_candidates` —
  the `aliases` a provider's own catalog row names (pollinations
  `openai-fast` -> `gpt-oss-20b`), relay re-spellings `_CTX_ID_REWRITES`
  (morph-kimik3 -> kimi-k3, zai-org-glm-5-3-flash -> glm-5.3-flash, gpt-5-2,
  gemma4:31b -> gemma-4-31b-it) — inferred before reference. New sourced
  `_CTX_REFERENCE` rows: Claude, GPT-4o/4.1/5, Grok 3/4, mistral-tiny,
  yi-large, Jamba 1.5, DBRX, StarCoder2, DeepSeek-Coder. Live replay:
  "default" 60 -> 35 of 256. Covered by `tests/test_window_coverage.py`.
- **Budgets from data**: `_ADAPTIVE_HOP_MULT` 3.0 -> 3.5 (heaviest healthy
  p95/p50 = 3.33); every other adaptive constant re-checked against the
  measured snapshot in `tests/test_latency_constants.py` and kept.

## Manager pipeline speed (2026-09-27)

Covered by `tests/test_manager_speed.py`. Live: tally.py + tests + README with
manager sub-claude/sonnet took 772 s and shipped without the tests (the clock
ran out; each subscription call is 100-150 s).

- **One verdict per WAVE** (`_verify_set` / `_batch_verdict`), not per phase.
  Free checks first (answer_check, literals, word counts, and for code:
  `ast` syntax, test count, imports that exist in the module under test —
  `_code_problems`); their rejections retry FREE before the verdict. A phase
  whose acceptance the string tests fully decide (`_proven`) gets no verdict.
  The LAST wave's verdict also answers the supervisor's coverage question and
  the undecidable enumerated parts (`check_info`) — no separate supervise /
  parts call. When what ships passed its checks and the manager confirmed
  coverage, the review is skipped (never for a profile with its own
  `review_system` or `max_revisions`).
- **Pipelined waves**: wave k's verdict runs while wave k+1's free workers
  build on wave k's output; a changed output rebuilds its dependents.
  Speculative synthesis runs alongside the review. The plan retry goes to the
  FREE planner. Each manager call has its own deadline (`MANAGER_DEADLINES`,
  never past the wall clock) and falls back free.
- **Tests/docs see the code** (`wire_code_deps`): a tests or README phase is
  made to need the phase producing its .py file (moved after it when listed
  first) and gets `INTERFACE OF x.py` (signatures + argparse flags) plus test
  instructions. Retries see the rejected attempt; a manager that cannot fix
  leaves one free repair pass. A first attempt cut by the cap races the grace
  re-run.
- `claude -p` gets `--strict-mcp-config --no-session-persistence
  --disable-slash-commands` (+ `--tools ""` for a direct .exe) — only the flags
  its own cached `--help` lists (`_claude_fast_args`).
- Scaled simulation (manager 110 s, free phase 50 s, synthesis 60 s), tally
  plan: 6 manager calls / ~550 s before, 3 calls / ~440 s now.

## Tool-turn reliability (2026-09-27)

Covered by `tests/test_tool_turn_reliability.py`. Live on 8bf19be: a 16K
opencode tool turn spent all 240 s on nvidia (llama-3.2-90b-vision ReadTimeout,
then more nvidia); kimi saw 9x 504/503 with g4f walking 7 relay hops.

- **Chain** (`_build_chain`, tool branch): vision-specialised ids
  (`_is_vision_specialised`: `-vision`, `-vl-`, llava...) go behind every other
  candidate unless the turn has an image, and `_route_by_difficulty` never
  opens a tool turn on one; a pair measured on TOOL turns to mostly fail
  (`_tool_outcomes`, `_tool_turn_sick`) joins the measured-to-fail group; a
  pair measured slow to first content (`_tool_ttft`, else all ttft samples,
  p50 > `_TOOL_SLOW_TTFT_MS`) goes behind the quick ones in its group.
  Unmeasured moves nothing; a healthy chain keeps its strength order.
- **Walk** (`_ChainClock(tools=, est=)`): a provider that stalled
  (timeout/hop budget/silent peek) or had `_TOOL_PER_PROVIDER_HOPS` (2) failed
  hops goes behind the others; while another provider waits, one provider gets
  at most `_TOOL_PROVIDER_SHARE` (50%, 75% past 48K tokens) of the deadline.
- **Relays**: `_relay_server_id` = the g4f backend prefix (`srv_x`,
  `pa:hash`, `RelayRouter`). ConnectionError / non-answer on a tool turn is a
  strike; 2 in 15 min skips that server for tool requests. Tool chains carry
  at most `_TOOL_RELAY_MAX_HOPS` (3) relay hops; a server that failed in a
  walk is not retried in it.
- **Trims** (`_trim_largest_message`): middle-only, line-aligned; the marker
  states the exact chars (and lines) cut "from the MIDDLE"; a tool result keeps
  half as tail and always its whole last line. `<env>` /
  `<environment_context>` blocks and cwd lines in the cut are carried over
  verbatim. MEASURED live on old code: Kimi-shaped prompt trimmed for groq
  (8K) -> cwd lost 5/5; the new trim's payload -> cwd answered 4/4.
- Pipeline tool turns (fan-out + fast path) pass the CLI's messages
  unchanged to every member; the env loss was the per-model trim above.

## Tool-turn timeouts (2026-09-27)

Covered by `tests/test_tool_turn_timeouts.py`. Live on 7cb23ff: Claude Code
`multi` "What is N plus 1?" ran 1753 s (seven back-to-back 360 s fan-outs,
"0 used a tool" each, client retrying past its header timeout).

- **Claude Code's in-messages system block**: claude 2.1.x sends its
  `# Environment` block (cwd, agents, skills) as a role `system` entry INSIDE
  `messages`, after the question. `_anthropic_to_openai_messages` folds it
  into the leading system message; it used to become the LAST USER message,
  so the pipeline fast path, the difficulty classifier and upstream models
  read the environment instead of the question. (That shape appears when the
  model id is unrecognised; with the hub's picker settings the block sits in
  `system`.)
- **Fast path on an opening turn**: `_swarm_fast_path`'s 12K "small
  conversation" gate now applies only once the conversation has an answer in
  it (`_has_prior_turns`). Claude Code's opening turn with the hub's settings
  is ~13.5K tokens of system prompt + CLAUDE.md reminders, which sent
  "What is N plus 1?" on `multi`/`coding-swarm` to the fan-out every time.
- **Fan-out settles on text** (`_swarm_tool_result`): a member's CHECKED text
  answer (`_text_final`: answer_gate "ok", not refusal/announcement/typed
  call) starts the same `_SWARM_STRAGGLER_GRACE` a tool call does; once more
  than half of the members still in the race answered in text and none acted,
  the race ends at `_SWARM_TEXT_SETTLE` (25 s = trivial budget) from its start,
  or at once. A tool call inside the window still wins.
- **Streamed fan-out bound**: `_SWARM_TOOL_STREAM_DEADLINE` (180 s) and the
  request clock starts at the fan-out, so the fallback after an empty
  fan-out shares it (the turn stays under the ~300 s client header timeout).
- **Fan-out members & settle** (`tests/test_swarm_fanout_members.py`):
  `_swarm_tool_candidates` drops sick pairs (`_swarm_member_sick`: provider
  parked/throttled/exhausted, model throttled/dead/not offered/benched,
  `_recent_hop_fail`, `_relay_tool_sick`, the fan-out ledger
  `_swarm_member_fail`) BEFORE the identity de-dup; < 2 healthy = old
  selection. A member's 429 -> `_recent_hop_fail`, 5xx -> `_throttle_failed_hop`,
  any failure -> `_swarm_member_fail` (600 s). A VALID tool call
  (`_swarm_tool_calls_valid`) ends the race after `_swarm_tool_grace`
  (0.5x its latency, 15-25 s, <= `_SWARM_STRAGGLER_GRACE`); with an answer in
  hand, members of a provider that 429/5xx'd in the same race are not waited on.
- **Stalls vs 429s**: `_recent_hop_stall` (recent failure kind
  deadline/timeout). When every candidate failed recently the router still
  prefers a pair that 429'd over one that stalled (so a session pin re-picks
  instead of re-choosing the pair that just cost a hop budget), and the
  chain's recent-failure tail puts stalls behind 429s.

## Long conversations in real use (2026-09-27)

Verified live (160K-token histories on all 3 protocols; Claude Code
`-p --continue` and codex `exec resume --last`, 7 turns each, facts from
turn 1 recalled at turn 7). Covered by `tests/test_long_request_upload.py`,
`tests/test_unsupported_param_drop.py`,
`tests/test_recap_treats_transcript_as_data.py`.

- **Uploads**: urllib3 sends the BODY under the CONNECT timeout. This
  machine's uplink moved ~20 KB/s, so a flat 10s killed every body >~200 KB
  ("The write operation timed out" -> ConnectionError on every hop -> 503).
  `_send_timeout(_body_bytes(payload))` (floor `UPLOAD_FLOOR_BYTES_PER_S`,
  cap `UPLOAD_TIMEOUT_CAP`) on every chat/puter/embedding post; the streaming
  header wait is `_STREAM_HEADER_WAIT + _upload_allowance(...)`.
- **Prefill**: past `STREAM_HUGE_REQUEST_TOKENS` the first-content peek grows
  1s per `STREAM_PREFILL_TOKENS_PER_S` (cap `STREAM_PREFILL_EXTRA_CAP`).
- **Refused optional params** (`prompt_cache_key` -> nvidia 400 "Unsupported
  parameter(s)"): `_rejected_optional_params` drops a key a 400/422 names next
  to a refusal word, retries once, remembers it per provider
  (`_PARAM_REJECTED`); model/messages/tools/stream are never dropped.
- **Recap**: the summariser's input is fenced as data
  (`_summary_user_content`) and the recap carries a USER FACTS & RULES
  section — it once obeyed the conversation's "answer in French, start with
  OK:" rule and invented a goal.
- **Template junk**: answer_check `template_junk` rejects a reply that is only
  a `<|x|>` token (+ at most one glued word) and a punctuation run —
  nvidia/kimi-k3 shipped "<|close|>!!!!…" as 3 codex turns.
- At 220K real tokens one free hop can need 110-270s. The deadline now
  scales (see next section); the CLIs' declared windows (128K / codex 96K)
  still keep real sessions well below that size.

## Broken upstream streams (2026-10-03)

Covered by `tests/test_broken_stream_retry.py`. Live: a Multi worker (opencode,
session fe4bbd96) lost its provider's stream after 7 min ("Response ended
prematurely"); the passthrough appended `[DONE]`, opencode took the partial as
COMPLETE (exit 0, no message) and nothing was filed against the pair.

- When the upstream ITERATOR raises (`_upstream_reads`) or streams an
  `{"error": ...}` object BEFORE any finish_reason / [DONE], each protocol ends
  with what its clients RETRY on (source evidence at each site): chat =
  `_chat_broken_frame` (`{"error":{message,type:"server_error",code:"ECONNRESET"}}`,
  no `choices`, no [DONE]; opencode fromError -> isRetryable, qwen-code
  'transport'); responses = `response.failed` code `upstream_error` and NO
  `output_item.done` (codex CodexErr::Stream, stream_max_retries 5; a partial
  item would be replayed into history); messages = the body just ENDS, no
  stop/delta/error event (Claude Code docs: a cleanly ended body is a dropped
  connection, retried "even if some text had already started streaming"; an
  error event is retried only before any text; stopping a block completes it).
- Died AFTER its finish_reason = complete answer: today's clean end + judge.
  A fault in the hub's own frame handling keeps the old except path.
- `_note_broken_stream` files `_record_outcome(False)` + `_note_recent_hop_failure`
  ("timeout" for a read timeout, else "conn") -- the ledger `_build_chain`
  reads, so the next request puts the pair at the tail. Skipped when the answer
  gate cut (it filed already). `_sse_deltas` raises on the error frame, so
  /v1/completions, Ollama and Gemini surfaces end with their own error shape;
  a Puter NDJSON `error` event now raises instead of becoming finish "stop".
- Unchanged: the 150 s keepalive stall cut (`_STREAM_PROGRESS_DEADLINE`) still
  ends cleanly; kimi-cli shows an error frame as an error (no retry).

## Client disconnect stops the work (2026-10-04)

Covered by `tests/test_client_disconnect_stops_work.py` (real werkzeug
threaded server on an ephemeral port, a raw-TCP fake provider, clients that
leave). MEASURED 2026-10-03 23:13: `curl -N -m 3` on a streaming coding-max
request left after 3 s; the activity row stayed `in_progress` and the chain
walked g4f relay hops for 58+ s. werkzeug sees a gone client only when it
WRITES; while the hub waits (pre-commit peek, chain walk, buffered tool turn,
fan-out, pipelines, stream gate hold) nothing is written.

- **Detection** (`clientgone.py`, `MONITOR`): `_watch_client_before` (POST on
  `/v1/*`, `/v1beta/*`, Ollama paths; flag `client_disconnect_cancel`, default
  on) reads the body in full FIRST, then registers `environ["werkzeug.socket"]`
  with one shared daemon thread: every 0.5 s a zero-timeout `select()` (<= 500
  sockets per call, Windows' FD_SETSIZE is 512), and a read-ready socket is
  peeked with `recv(1, MSG_PEEK)`: `b""` or reset/abort = gone; pending bytes =
  alive; nothing is ever consumed. werkzeug answers `Connection: close` to
  every request, so there is no keep-alive read to confuse it. TLS sockets and
  the test client (no socket) are not watched. Unregistered when a plain reply
  leaves the handler, or when a stream body ends.
- **Cancel** (`Token.cancel`): flag first, then hooks (the activity row ends
  `cancelled` / 499 at the moment of detection; a subscription CLI process is
  killed), then every upstream connection the request opened is shut down
  with the BASE `socket.shutdown(SHUT_RDWR)`. A urllib3 hook
  (`HTTPConnectionPool._make_request`, `connect`) tracks connections made
  under the request's token (contextvar; plain threads get it through
  `clientgone.bind`) and refuses to START one after the cancel
  (`ClientGone`, a RuntimeError the chain loops already treat as "hop over").
  MEASURED on Windows: shutdown unblocks a blocked read on a timeout socket at
  once; `resp.close()` from another thread does NOT (it waits on the reader's
  buffer lock until the read returns) -- never close a response from the
  monitor. http.client drops `conn.sock` once a reply says Connection: close,
  so the socket is remembered at connect time (`_hub_raw_sock`).
- **Who checks the flag**: `_ChainClock.walk` (no next hop) and `dispatch`;
  the hedge race (0.5 s slices); `_call_with_wall_clock`,
  `_post_with_header_deadline`, `_dispatch_chat_with_deadline`,
  `_peek_until_content` (`clientgone.join`, status "cancelled"); the tool
  fan-out wait (0.5 s slices); `_swarm_dispatch` stage hops;
  `_manager_dispatch`; `_run_cli_cancellable` (sub-* / manager CLI); the
  three loops after the walk and `_chat_completions_uncached` / the swarm
  fall-throughs (`_client_gone_reply`, 499).
- **Nothing filed against the provider**: `_record_outcome(False)`,
  `_note_recent_hop_failure`, `_throttle_failed_hop`,
  `_note_provider_timeout`, `_note_provider_result(False)`,
  `_note_nonanswer`, `_note_relay_tool_fail`, `_note_tool_turn_outcome(False)`,
  `_note_quality_strike`, `_record_long_ctx_speed(stalled)`,
  `_record_stream_outcome`, `_note_broken_stream`, `_note_swarm_member_fail`
  return early while `_client_gone()`. Usage already spent is still recorded.
- **Activity**: `cancelled` (499) is a finished status; the dashboard shows it
  neutral ("client left"). A stream body closed before its end
  (GeneratorExit = a write failed) is `cancelled` and cancels the token too;
  the `call_on_close` backstop files `cancelled`, never "ok".
- **Stop**: /agent Stop kills the CLI tree -> its socket closes -> the
  request is cancelled (test kills a real client process). Multi:
  `swarm_windows._run_wave` now calls `stop(session)` for every worker still
  running when the run's stop flag is set (before, the flag only stopped NEW
  phases and running workers kept their CLIs going).
- Live probe (worktree app served on 8799 with sandboxed state, real
  `curl -N -m 3`, a relay that sends headers then nothing): row `cancelled`
  0.5 s after curl left, relay connection cut at +0.5 s, 1 hop total; with
  the flag off the row stayed `in_progress` past 20 s and the relay
  connection was never cut.
- **Abandoned hops close without blocking** (same test file). MEASURED on
  Windows: a `resp.close()` while a peek/pump thread is blocked reading that
  streamed body waits on the reader's buffer lock until the read returns --
  for a relay that sent headers then nothing, the 90 s read timeout. So every
  hop the hub gave up on (a peek with no content, a stall cut, a hedge loser,
  the deadline guard) held the request thread: on the 8799 sandbox (peek 8 s)
  hop 2 started at +90.0 s; now at +8.1 s. `_nb_close` (=
  `clientgone.nonblocking_close`, installed on every streamed response in
  `_dispatch_chat`, `_ChainClock._plain` and each hedge leg) makes `close()`
  shut the socket down first, then close on a daemon thread (waited <= 0.2 s);
  no live socket = the plain close. The closure holds the response WEAKLY
  (a cycle kept fully read responses' sockets open until the cyclic GC).
  In-flight calls the hub stops waiting for run under a per-hop token
  (`clientgone.child`, linked to the request's): `_call_with_wall_clock`
  (hop budget), `_post_with_header_deadline`, `_dispatch_chat_with_deadline`
  and hedge legs still pending at `_settle` are CUT (`hop.cancel`) instead of
  left open until the provider's read timeout.

## Long-context deadlines & exact facts (2026-09-27)

Covered by `tests/test_long_context.py`.

- **Scaled deadline** (`_scaled_request_deadline`): past
  `LONG_DEADLINE_FROM_TOKENS` (60K est) the request deadline gains
  `request_deadline_per_10k_seconds` (15) per started 10K tokens, capped at
  `request_deadline_max_seconds` (600): 220K -> 480s. A STREAM is capped at
  `LONG_DEADLINE_STREAM_MAX` (285, under the clients' ~300s header timeout);
  `_STREAM_HEADER_BUDGET`, the peeks and the post-deadline rules are
  unchanged. `_begin_request_deadline(tokens, stream)` only EXTENDS, from the
  clock's original start; `_ChainClock(est=, stream=)` on all three loops.
- **Fast long-context hops first**: `_long_ctx_speed` (in memory, 6h TTL)
  records time to first content / buffered duration of requests >= 60K est,
  and a hop that waited >= 60s for nothing as a stall. `_long_ctx_band`
  0 fast (median <= 60s) / 1 unknown / 2 slow (>= 120s or latest stalled);
  `_build_chain` stable-reorders by band (`_prefer_fast_long_context`) inside
  the last-resort and recent-failure partitions; a pinned hop one never moves.
- **Exact facts** (`ctxwin.exact_facts`, no model call): what each file /
  command printed (attributed only when unambiguous: one path, or `==> f <==`
  sections), what was written where (write tools, apply_patch Add File,
  heredoc, echo `>`/`>>`), the user's stated values and standing rules (own
  share, `EXACT_FACTS_MAX_RULES`), `Decision:` lines. A CLI summary's prose is
  never mined, its EXACT FACTS block is carried. Compaction adds it as its OWN
  system message after the notice (room reserved in the keep loop, ~5% of the
  target); the rolling recap entry stores `exact` and `_conversation_recap`
  appends it; a CLI's own compaction request (codex CONTEXT CHECKPOINT) gets
  the block appended to its last user message with "copy UNCHANGED"
  (`_with_cli_compaction_facts`). Flag `compact_exact_facts` (default on).

## Subscription scope (manager-only)

Setting `subscription_scope` = `"all"` (default when absent — the old
behaviour: manager + sub-* last-resort routing) | `"manager_only"`. With
`manager_only`, `_sub_routing_on()` is false, so `_sub_available_providers()`
returns [] (no sub-* candidate, primary, chain tail or /v1/models row), an
explicit `sub-*/<model>` pick and `_check_provider_ready` are refused, and the
`_subscription_chat` dispatch shim answers 403 without running the CLI.
`_manager_dispatch` still works (it gates on `_sub_master_on()` only), and
`/agent` sessions have their own switch. Exposed as `subscription_scope` in
GET/POST `/api/subscriptions` and the dashboard "What the subscription may be
used for" radio. Covered by `tests/test_subscription_scope.py`.

## Static ranking rebench (2026-09-27)

Covered by `tests/test_rebench_2026_09.py`. A table entry moves only when two
independent boards agree (Artificial Analysis II + Arena text), or one
authoritative narrow board for a category (LMArena Vision, tau-bench,
Terminal-Bench 4.0 on tbench.ai + AA). Each change carries its numbers in a
comment at the site.

- `_STRONG_ROOTS` versions must FOLLOW the root and are never a parameter
  count (`_root_versions`): `codellama-70b` had read as Llama v70 (104 live).
- Measured floors: GLM-5.3+ flash 133.6 (over dsv4-pro / minimax-m3, under
  dsv4 flash), MiMo-V2.6+ Pro 134.09 (qwen3.8 level, under kimi-k3),
  deepseek v4.x +0.01/minor, Gemini 3.5+ Flash-Lite 60. llama-4 /
  llama-3.3-70b (44) and mistral-large (52) left Tier A, now under
  mistral-medium (56).
- AA lookup: OpenRouter vendor namespaces (`deepseek/`, `qwen/`, `xiaomi/`,
  `anthropic/`, `x-ai/`, `minimax/` …) are stripped, and a pre-fix cache's
  vendor-joined keys still match exactly (`_AA_LEGACY_VENDOR_KEYS`).
- Unchanged because the owner set them or the boards disagree: the Claude floor,
  the gpt-5.x floors, gemini pro-over-flash, and the last-resort tail.
- SUPERSEDED 2026-10-10 (see "Ranking follows the boards" at the end of this
  file): the static kimi-k3 (138.1) and glm-5.3 (138) floors are GONE -- those
  two, plus qwen3.8-27b, are placed by board evidence now, and TOOL turns are
  ordered by Terminal-Bench 4.0. So MiMo-V2.6-Pro (134.09) vs kimi-k3 is now a
  genuine ~tie (kimi-k3 ~134.08), not kimi-on-top.

## /agent servers & hub self-protection (2026-09-27)

Covered by `tests/test_agent_servers.py`. Live: an opencode turn ran
`python app.py` in the foreground, the 420 s stall watchdog resumed blindly,
the model listed/killed python processes and blocked again ("produced nothing
for 420s twice").

- **Rules** (`agent_servers.brief_section` in every brief file, all CLIs and
  tiers; `worker_rules` in each swarm_windows worker prompt): servers start
  DETACHED with output to a log, a web app is left to the preview (it runs the
  project at turn end and adopts any printed `http://127.0.0.1:PORT`), only
  processes the agent started may be stopped, and the hub's PID(s) (venv
  launcher included) and port are named. Spellings MEASURED here under
  pipe-EOF semantics: bash `nohup CMD > log 2>&1 &` and PowerShell
  `Start-Process -WindowStyle Hidden -FilePath cmd -ArgumentList '/c','CMD >
  log 2>&1'` (cmd: the same via `powershell -Command`) return in < 1 s;
  `start /B`, `Start-Process -NoNewWindow`/`-Redirect*` and a bare `&` HANG
  (inherited pipe handles).
- **Smart resume**: at a stall, BEFORE the kill, `diagnose_stall` checks the
  CLI's process tree (a shell-spawned descendant listening, or running a
  server/watcher command) and, for codex/claude only, a server command that
  was the last line printed. opencode emits `tool_use` only on
  completed/error (read in its binary), so for it only the tree counts. A hit
  shows "Server running on port N ..." (no URL -- the preview would burn that
  port's one adopt try) and resumes with `resume_instruction` (what blocked,
  whether it listened, the detached spellings, never a kill suggestion). The
  first one does not spend the wedge retry; at most 3 attempts.
- **Early detection** (`agent_servers.early_server_diagnosis`, covered by
  `tests/test_agent_early_server.py`): after `AGENTIC_CHAT_SERVER_PROBE`
  (60 s, 0 = off, clamped 15..600) of silence the watchdog probes every 10 s.
  It fires only when a process the shell tool started is a server (listening
  on a non-hub port, or an unambiguous server command; `main.py`/`index.js`
  need a port), started AFTER the CLI's last line (so detached /
  run_in_background servers never count), up >= 20 s, and nothing else under
  the shell is real work (test runners, installers, builds = busy). ORPHANS:
  `start /B` / a bare `&` leave the server outside the CLI's tree (verified by
  a real-process test); every attempt's CLI env carries `CALVOUN_AGENT_TURN`,
  so `orphan_processes` finds them and `stop_processes` stops ONLY processes
  carrying this attempt's marker (never a hub PID, never the preview). The
  server is stopped, not left running: MEASURED, with its pipe reader gone
  `http.server`/print-logging servers answer RemoteDisconnected.
- **PID / port only** (`tests/test_server_safety.py`; live 2026-09-28, opencode
  run 1476d09d: `Get-Process | Where-Object {... -like "*app.py*"}` then
  `Stop-Process -Id` on the result — that filter matches the hub). Every
  detached spelling also writes `server.pid` (bash `& echo $! > server.pid`,
  PowerShell/cmd `Start-Process ... -PassThru | Select-Object -ExpandProperty
  Id | Set-Content server.pid`; measured to return in < 1 s like before). The
  brief (`stop_examples`) stops only by that PID (`taskkill /PID <pid> /T /F`
  — MEASURED: the recorded PID is cmd.exe's, `Stop-Process -Id` on it left
  the server listening; bash: its own `kill`, a `$!` is an MSYS PID) or by the
  port (`Get-NetTCPConnection -LocalPort N -State Listen`), and forbids any
  name / command-line search. `resume_instruction` (early and 420 s alike)
  carries the same guard and still names no kill command.
- **Brief pointer = absolute path**: same run, opencode's instance dir, session
  dir and PWD were the temp project folder (opencode log), but the model read
  a made-up `C:\Users\hamza\Desktop\Projects\opencode-evals\.calvoun-brief-*.md`
  — the pointer said "This folder contains <name>". It now names
  `<abs project dir>\<name>` (`_brief_pointer_path`); on the cmd.exe fallback
  with a message too close to the cap the bare name ships (`_pointer_dir`,
  keeps the ~8191 budget).
- **Limitation**: the hub cannot stop an agent shell from killing the hub
  (same user, no privilege to withhold, opencode reports a command only after
  it ran); prevention via the brief is the only defence.

## Provider status: no key, dead keys, echoed decimals (2026-09-28)

Covered by `tests/test_provider_status.py`.

- **no_key**: an ENABLED provider that needs a key and has none reports
  `status_reason: "no_key"`, detail "No API key saved — add one or switch it
  off" (`_NO_KEY_DETAIL`). Routing skipped it already (`_enabled_keyed`).
  The card labels it via `NOTE_REASONS` (not `OUT_REASONS`: no red cross).
- **Dead keys** (`quota.mark_key_dead`, fingerprints only, persisted as
  `key_dead`): the per-key Test marks a key whose real generation failed with
  an HTTP 4xx/5xx (not 429, not a network error) dead for 6 h (5xx: 30 min);
  live traffic marks it after 2 credential-shaped 401/403 in 10 min
  (`_note_key_live_status`; a model-scoped 401 like opencode-zen's "Model X
  is not supported" files nothing). A 2xx or a passing Test clears it.
  `usable_keys` skips dead keys and FAILS OPEN when every key is dead (a pool
  of all-dead keys is still tried). The card detail carries "dead keys: N of
  M"; `keys[]` rows carry `dead` / `dead_until` / `dead_why`.
- **Echoed decimal** (`answer_check.echoed_decimal`): "2768.2768" /
  "11991199.1199" under an "only the number" ask is a valid decimal shape, so
  it is NEVER cut; `inspect` adds the informational reason `echoed_decimal`
  (ok stays True) and `_answer_gate(..., hop=)` / `_record_stream_outcome`
  file `_note_quality_strike`: a junk-weighted failure + a junk-bench strike
  for that (provider, model) only.

## One-click health check (2026-09-28)

Settings drawer "Health check" group. `POST /api/health-check` starts ONE
background run (`_health_run`; a second POST while it runs = 409
`already_running`), `GET` returns `{running, progress, report}`,
`POST /api/health-check/cancel` stops it before the next step (a request in
flight finishes or times out first) and saves a `cancelled` partial report.
Steps: every ENABLED provider's `api_test_provider` in-process, sequentially,
skipping no_key / exhausted / throttled / no_free_tier (`_health_skip_reason`,
local state only); then 4 requests through the hub's own
`/v1/chat/completions` via the in-process test client (auto + tool turn, each
non-stream and stream; a tool turn passes only if it calls the tool); then the
`/api/model-windows` coverage. Summary: working, dead keys (per-key failures,
or an auth-shaped provider failure), failing (non-key), no key, skipped,
recommendations. Report at `state_dir()/health-check.json`. Never writes a
setting (the provider test still updates its own test cache). Usage is
labelled `health-check`. Covered by `tests/test_health_check.py`.
First live run (2026-09-28, 859 s, routing 4/4): each failed key gets a
`kind` (`_health_failure_kind`): `policy` (opencode-zen 403 "free tier can
only be used from within OpenCode" -> summary `blocked`, recommend disable;
never spoof the client), `server` (tokenrouter 503 "No available channel" ->
`failing`, not dead keys) or `key`. Only `key` failures are dead keys.

## Settings -> Skills (2026-09-28)

Covered by `tests/test_skills_settings.py`. "Skills" = the craft briefs
(craft.py) + the vendored last30days agent skill + the user's own named skills.

- `skills.py` (pure): `BUILTIN` catalog (id = craft brief name, plus
  `last30days`), `validate` (name <= 60, instructions <= 4000, trigger
  `keywords` | `always`, <= 20 keywords, <= 20 saved skills, unique names),
  `custom_hits` (keywords matched as escaped, word-bounded words, never a user
  regex; <= 3 user skills per request).
- Storage: settings `skills_disabled` (built-in ids) and `custom_skills`;
  master switch = existing flag `craft_briefs`. app.py registers
  `craft.set_skill_source(_skill_source)` at import, so `craft.match` (every
  protocol, /agent brief files) skips disabled built-ins and appends user
  skills as `USER SKILL: <name>` blocks; unregistered = old behaviour exactly.
  A tool-less request whose only hits are user skills gets no VERIFY_READ
  (it points at ANTI lines only built-ins carry). crew-design drops its
  WEB_DESIGN `worker_extra` when web_design is off (copy, CREWS not mutated).
- last30days off: GET `/api/web-search-policy` returns
  `{"social_search": false, "skill_enabled": false}` (unchanged shape when
  on) and SKILL.md stops on it.
- Routes (control-token gated): `GET /api/skills`, `POST /api/skills/toggle
  {id | "all", enabled}`, `POST /api/skills/custom` (no id = create, saved id
  = update), `POST /api/skills/custom/delete {id}`. UI: `#skills-group` in the
  Settings drawer.

## ECC skills (vendored, opt-in) (2026-10-08)

Covered by `tests/test_ecc_skills.py`. Nine coding-agent rule documents vendored
from github.com/affaan-m/ECC (commit `ef648e0`, repo VERSION 2.2.3), **MIT, (c)
Affaan Mustafa** -- the licence idea the "Model guides" section already credits,
now shipped as real toggleable skills. This is instructions only: no script,
hook, binary or `agents/openai.yaml` was vendored (supply-chain risk; the hub
only injects text).

- **The files** live in `skills_ecc/`: `LICENSE` (verbatim), `VENDORED.md`
  (source URL + commit + what was taken / modified / left out and why), and
  `skills_ecc/<name>.md` for each skill (the upstream `SKILL.md`, frontmatter
  kept for attribution). Skills: tdd-workflow, verification-loop,
  security-review, coding-standards, agent-introspection-debugging,
  backend-patterns, frontend-patterns, api-design, e2e-testing. Left out: the
  ~30 content/marketing/business skills; `strategic-compact` (built on the
  Claude-Code-only `/compact`, which does not work through the hub; the hub
  compacts itself in ctxwin). ONE modification: `tdd-workflow.md`'s "Step 0"
  dropped ECC's bundled package-manager detector script for a script-free
  instruction (an HTML comment marks the edit; see `VENDORED.md`).
- **`ecc.py`** (pure, stdlib, never raises): `CATALOG` (id `ecc:<name>`, display
  name, one-line description, trigger keywords drawn from each skill's own
  frontmatter), `matches`/`hits` (word-bounded keywords, never a user regex),
  and a mtime-cached reader (`load_body`) that strips the YAML frontmatter.
  `render(id)` = one bounded block `ECC SKILL: <name> (opt-in; MIT, (c) Affaan
  Mustafa -- ...)\n<body>`, each `<= MAX_CHARS` (1500); `hits(text, enabled_ids)`
  returns at most `MAX_PER_TURN` (2), ENABLED-and-matched only; a missing dir /
  unreadable file = no hits (the off state).
- **Default OFF.** Enabled ids live in setting `ecc_enabled` (default `[]`).
  `_skill_source()` now returns a THIRD element (enabled ECC ids); `craft.py`
  unpacks it length-tolerantly (a 2-tuple source = no ECC, the old shape some
  tests still use) and `craft.match` appends `ecc.hits` after the user skills.
  An ECC skill carries no ANTI lines, so like a user skill it never pulls the
  tool-less VERIFY_READ block. Rides every protocol, CLI, /agent brief file and
  crew through the same `craft.match` / `craft.system_message` path -- /v1 via
  `_apply_craft_brief`, the Build brief via `agentic_chat`.
- **Routes** (control-token gated, same `/api/skills/toggle`): id `ecc:<name>`
  toggles one, id `ecc_all` toggles every ECC skill; `GET /api/skills` gains
  `ecc` (`[{id,name,description,enabled}]`), `ecc_all` and `ecc_credit`. UI: a
  collapsed `<details id="skills-ecc-group">` inside `#skills-group` with the
  licence/credit line, "Enable all ECC", and per-skill switches.
- **Cost**: with EVERY ECC skill enabled, the heaviest request still lands at
  ~13.27% of the 32K floor (the saas landing page, which matches no ECC skill);
  the heaviest ECC-matching request is ~11.7%. The existing
  `test_craft_briefs.test_worst_case_brief_cost` (0.135) stays green because ECC
  is off by default.

## Low-resource mode (2026-09-28)

Covered by `tests/test_low_resource.py`. `lowres.py` (leaf: psutil + config,
fails open to "not weak"). Setting `low_resource_mode` auto (default) | on |
off; auto = active under 8 GB RAM or 4 logical cores. The one real RAM
multiplier is multi-session: each worker is a Node CLI (150-400 MB) and a run
started `swarm_windows.MAX_CONCURRENT` (4) at once. `swarm_windows._concurrency()`
= `lowres.workers(MAX_CONCURRENT)`: active -> 1 (< 6 GB) or 2; free RAM under
1.5 GB -> 1 on ANY machine unless mode is "off"; re-read before each queued
spawn (machine reading cached 5 s). Deliberately untouched: the shared
Playwright server (its browser launches only on use; the stdio fallback would
spawn one per CLI turn, which is worse) and HTTP fan-out threads (no real
RAM). `GET/POST /api/low-resource {mode}`; Settings `#lowres-group`.
`tests/conftest.py` pins a roomy machine for every test.

**Live RAM governor (2026-10-04, `tests/test_multi_parallel_models.py`)**:
the user's own programs come first. `lowres.Governor` (psutil only, fake-able
machine/clock/procs/cpu/priority) is ticked every 2 s by ONE daemon thread
while a Multi run walks (`acquire_monitor`/`release_monitor` around
`_run_phases`). Reserve = setting `multi_ram_reserve_gb` ("auto" = max(3 GB,
20% of total RAM)); new helpers allowed = (available - reserve) / per-helper
cost, where the cost is MEASURED (RSS of each worker process tree = processes
carrying `CALVOUN_AGENT_TURN`; p90 of the last 10 samples; 0.5 GB until
measured) and spawns younger than 15 s are subtracted (their RAM is not used
yet). Lowers at once, raises only after the higher value held 20 s. It ONLY
stops NEW starts (`swarm_windows.spawn_allowed`): never kills/suspends a
running helper, queued phases just wait ("Waiting for RAM to free up" - not an
error), and a run with nothing running always gets one helper. Free RAM < 1 GB
lowers the OS priority of the marker processes (BELOW_NORMAL on Windows, nice
+10 elsewhere) and restores it at >= 1.5 GB. Non-hub CPU > 90% for 10 s holds
new spawns the same way. Stale monitor (> 10 s), a failure, unreadable numbers
or `low_resource_mode` "off" = no live limit (the old behaviour). Measured
here (39.7 GB, 18.5 GB free, 8 cores): reserve 7.9 GB, cost 0.5 GB until
measured -> 21 new helpers by RAM, so the cap/fleet decide. `GET /api/low-
resource` gains `reserve_gb`, `per_helper_gb`, `allowed_now`, `limited_by`.

## /agent outage fixes (2026-09-28, session 47a25faa)

Live: an opencode Max + coding session (~54K tokens) failed after 25 min —
four 504s at the request deadline (nvidia stalling, dahl/openrouter 429,
tokenrouter 503, small models too short), opencode retrying silently, the
watchdog resuming blindly. Earlier a g4f relay (Pollinations backend) served a
billing notice as the answer.

- **Google on tool continuations** (`tests/test_gemini_tool_history.py`):
  Google used to be HARD-excluded from any request whose history had a tool
  call (Gemini 400s unsigned calls) — i.e. from every /agent turn after the
  first. The Google hop now carries Google's skip value
  `skip_thought_signature_validator` as `extra_content.google.thought_signature`
  on the first unsigned call of each assistant step
  (`_with_gemini_history_signatures`, in `_upstream_chat`, google only;
  MEASURED 200 on gemini-flash-latest). A thought_signature 400 on a payload
  that carried it (`_note_gemini_signature_rejection`) restores the exclusion
  for `_GEMINI_SIG_RETRY_SECONDS` (24 h).
- **Provider notices** (`tests/test_provider_notice.py`, RELAY hops only --
  `inspect(relay=)` / `reads_as_answer(relay=)`, set from `_is_relay_pid`
  by the three gates; a direct provider reports quota as an HTTP error, and a
  real answer about the user's OWN API quota must pass): answer_check
  `provider_notice` — a reply <= 1200 chars that is a quota/budget/credits/
  rate-limit notice addressed to the API caller (URL, "api key", "this
  request", "try again"...) is junk (cut at 0, no salvage), unless the user's
  own prompt is about those topics; `reads_as_answer` never releases one.
- **Outage stop** (`tests/test_agent_upstream_outage.py`): every activity row
  carries `session` (`_build_sid`); `_activity_done` feeds
  `_note_agent_upstream` (ok / 502-504 error per /agent session, ordered by a
  sequence number, not the ~15 ms Windows clock). `agentic_chat` asks
  `_agent_upstream_probe` at the 420 s stall: if the silence is explained by
  requests the hub could not serve (and none succeeded after), the turn ends
  503 with `outage_detail` (the providers and why, "send continue in a few
  minutes") instead of "looks wedged, resuming".
- **Mode + quality**: `_hub_model_for` sends the compound (`coding-max`,
  `coding-swarm`) instead of dropping one axis; it used to send bare `coding`
  (Normal tier) for Max + coding, and two turns were served by a 2.6B model.

## "What changed" window & release notes (2026-09-28)

Covered by `tests/test_whats_new_design.py` (+ the older
`tests/test_the_whats_new_popup.py`). `/api/release-notes` reads the last 24
non-merge commits and returns up to 16, each with `kind` (new | fix | improved
| change; `docs` is dropped), a plain-language `title` and a `scope` chip
(`_release_note_view`). Source order: `release_notes.json` (hash prefix ->
{kind, scope, title}, for commits older than the trailer), then the commit's
own trailer, then the subject split as "Scope: rest".

**Every user-visible commit gets a trailer** in its message's last paragraph:

    Release-note: fix: <one plain sentence a non-developer understands>

(kinds: new, fix, improved; docs-only commits: `Release-note: docs: ...` or a
`README:` subject). The window groups by kind with counts, marks what is new
since the last visit, and opens from Settings -> Update -> What changed. Its
CSS uses theme tokens only; the test measures every text/background pair at
>= 4.5:1 in both themes (the light `--ok-text` was darkened to #166534 for it).

## The orchestrator (2026-09-28)

Covered by `tests/test_orchestrator.py`. `orchestrator.py` (pure: command
parser, fuzzy `match_model`, `ConversationStore` at
`state_dir()/orchestrators.json`, LRU 1000 / 60 days).

- **Why "Set orchestrator" did nothing**: it saved `config.set_default()`,
  which only resolves BARE/unknown model names; every CLI sends auto / best /
  a category and those always went through `_route_by_difficulty`. The saved
  default on the owner's install was AUTO-picked (`_autoselect_default_if_unset`:
  groq/llama-3.3-70b), so it is deliberately NOT wired into auto traffic.
- **Two levels**: setting `orchestrator_preferred` ("pid/model", absent =
  auto) for all conversations; the store keyed by `ctxwin.conversation_key`
  (`agent:<sid>` for /agent, CLI session header/body id otherwise) for one
  conversation ("auto" = force auto, absent = follow global).
  `_apply_orchestrator` runs right after the router on all three protocols;
  the chosen pair opens the turn, the chain is built behind it, and
  `_orch_unusable` skips it for that turn (provider off, blocked, not offered,
  throttled/resting, no tools on a tool turn, no vision on an image turn,
  window < est) with `[orchestrator] ... skipped` logged.
- **`/orchestrator` command** (whole last user message, `<system-reminder>`
  blocks stripped): `_orch_command_response` answers it in the caller's
  protocol with no model call (provider header `hub`). Forms: `<name>`,
  `auto`, `all <name>`, `reset`, bare = status. The Build page send route
  answers it without starting the CLI. opencode Connect writes
  `command.orchestrator` (template `[free-llm-hub] /orchestrator $ARGUMENTS`),
  Disconnect removes it; a user's own command of that name is kept.
- **UI**: Orchestrator page (Auto option; Set and Auto-pick both set the
  global choice, a model also the bare-name default), Build page
  `#agent-orchestrator` per conversation. `GET/POST /api/orchestrator
  {model, session_id?}`.

## OpenRouter free models, Space Bunny, conclusive provider Test (2026-09-29)

Covered by `tests/test_openrouter_free_and_space_bunny.py`.

- OpenRouter stays `suffix_free` (Lyria bills per song at price 0) PLUS
  `free_zero_text`: discovery and the Test call `_zero_priced_text_ids`
  (every published price 0, `architecture.output_modalities == ["text"]`, not
  the `openrouter/free`/`auto` aliases) and `providers.note_extra_free`;
  `is_free_model` accepts those ids. MEASURED: stealth/space-bunny-alpha was
  the only real model this admitted.
- OWNER DIRECTIVE: Space Bunny floor `_PREF_FLOORS[12]` = 137.7
  (`_SPACE_BUNNY_RE`), above Pixel Canary, under Claude's 138. No public
  benchmark exists for it (anonymous stealth model).
- Provider Test: candidates sorted unblocked-first, strongest-first; a free
  probe gets `_TEST_PROBE_MAX_TOKENS` (512, metered probes keep 16); an empty
  200 moves on to the next candidate and is only the fallback verdict.
- AA scores ARE applied where the feed has them (nemotron-3 ultra/super,
  inkling, north-mini-code...); gemma-4, laguna, ling, dots, lfm-2.5 and
  inkling-small have no feed entry and score by family table.

## Only working keys & TokenRouter removed (2026-09-29)

- **Dead keys escalate** (`tests/test_dead_key_escalation.py`): an expired
  dead mark is KEPT (only `clear_key_dead` on a 2xx / passing Test removes
  it), so a key found dead again within `_KEY_DEAD_REPEAT_WINDOW` (3 days)
  doubles its time out, up to `_KEY_DEAD_MAX_TTL` (7 days). 5xx marks are
  `escalate=False` (an outage is the provider's, not the key's).
  `usable_keys` still fails open when EVERY key of a provider is out.
- **Provider Test time limits**: `_TEST_PROBE_CALL_SECONDS` (40) per call,
  `_TEST_PROBE_KEY_SECONDS` (90) per key, quick models first
  (`_is_slow_model`); when nothing answered in time the verdict is "Could not
  verify this key ... slow or overloaded", never "None of the keys work", and
  `_health_failure_kind` files timeouts as `server`.
- **TokenRouter: removed** at user request. Re-probed with the user's keys:
  kimi-k3-free 503 "No available channel", and the only other free-named id
  (nemotron-3-nano-omni ...:free) 403 "credit limit insufficient, remaining
  0" -- it bills a paid balance. Do not re-add without a real free 200.

## Categories by name AND by evidence (2026-09-29)

Covered by `tests/test_category_evidence.py`. Rechecked against the live
fleet: 34 of 117 alive models were in no category, including the #2 model
(stealth/space-bunny-alpha); qwen3.8-27b was only "uncensored", MiniMax-M2.7
in none. Named now: `space-bunny` (swarm, coding, context, vision, seo),
`qwen3.8` and `minimax-m2` (swarm, coding). `app._category_matches` is the ONE
membership test (routing `_mode_allows`, the Settings list, the mode counts):
the name patterns, else `_category_by_evidence` unless a "!" pattern rules
the model out (`model_categories.excluded`): context = KNOWN window >=
400K; swarm/coding/seo = tool-capable and score >= 130; reasoning = that and
`_thinks_by_default`. uncensored/specialist/fast/vision stay name-only.
`tests/conftest.py` stands evidence aside for tests that patch
`model_categories.matches` to define membership themselves.

## Best model leads; the orchestrator opens real turns (2026-09-30)

Covered by `tests/test_best_model_leads.py`. Live, session 47a25faa (opencode,
coding): 22 of 24 turns on gemini-3.6/3.7-flash (134.1) while glm-5.3 (138) and
the conversation's chosen orchestrator space-bunny (137.7) sat unused.

- **Auto pick**: `_auto_top_band` keeps entries within `_AUTO_TOP_BAND` (2.0)
  of the best `_agentic_score` before `_weighted_pick` (after `_spread_pool`).
  Alone, the softmax (T=5) over a pool of 14-55 let ~40 models at 130-134
  outweigh the one at 138.
- **Lead group**: `_may_lead_pool` = `_may_lead_agentic` (unchanged) plus any
  model at least as strong as the strongest `_TOOL_PROVEN` one present. Used by
  the router's agentic pool and `_build_chain`'s tool grouping, which put every
  gemini-3 (even a relay copy at 130.1) ahead of every stronger model.
- **Orchestrator**: `_apply_orchestrator` sets `g.hub_orchestrator_pair`;
  `_build_chain` treats that primary as `pinned` (opens hop 1 whatever its
  learned record; a stale 12-to-1 failure record from pre-fix empty replies
  kept space-bunny off hop 1, so it never earned a new one). A `simple` turn
  under `STREAM_BIG_REQUEST_TOKENS` keeps the router's pick (owner: medium
  models for easy turns whatever the orchestrator). An exception in it is now
  logged (`[orchestrator] not applied`) instead of silently dropped.

## Multi follows the selection, in any language (2026-09-30)

Covered by `tests/test_multi_french_work.py` and
`tests/test_multi_follows_the_selection.py`. Live, session 47a25faa (Multi +
coding): a French fix request was answered by one session on the Normal tier.

- **Is it work?** `_multi_wants_a_swarm`: > `_MULTI_DIRECT_MAX_CHARS` = work;
  else a quick model verdict `_multi_intent_by_model` ("WORK"/"CHAT", any
  language, medium route, 2 hops x 12 s, no subscription; MEASURED 2-7 s,
  correct on FR/AR/chat). No verdict = language-independent fallback: a
  question (ends ? / U+FF1F / U+061F) of <= 12 words or <= 3 words naming no
  work (`_MULTI_WORK_WORDS` + `_MULTI_WORK_WORDS_INTL`) is answered directly,
  everything else follows the tier. The classifier is no longer consulted
  (English-only hints). `tests/conftest.py` stubs the verdict to None.
- **Category**: `_multi_worker_modes(sess_info)` = `(session category,)` when
  one is selected, so the planner cannot pick another one, and
  `swarm_windows.start/resume(default_mode=)` (persisted on the run) is the
  category of a phase the plan names none for (`_run_agent_once`: `agent.mode
  or run.default_mode`). The planner's vision/coding choices used to win, and a
  phase with none ran under "all".
- **Effort**: `_hub_model_for("multi", mode)` = `"<mode>-max"` (was the bare
  mode = Normal tier).
- **One run, several strong models** (`tests/test_run_workers_mix_models.py`):
  a run's workers mostly go one after another, and `_spread_pool` only spreads
  CONCURRENT sessions, so each worker drew from the same top two. The agentic
  first pick now runs `_rotate_within_run` before `_spread_pool`: models the
  run's other workers opened on (`swarm_windows.sibling_sessions` + the
  `_WORKER_MODEL` ledger written by `_note_worker_model` at the pick) are
  skipped while a model within `_RUN_ROTATE_MAX_DROP` (4) of the best is left;
  else the full pool. Logged as `[rotate]`. A worker still keeps ONE model for
  its own turns (the session pin).
- **Task list** (`tests/test_multi_run_task_list.py`): `/api/agent/sessions/
  <sid>/plan` returns `_multi_run_plan` -- one line per phase of the
  conversation's run ("Phase N: title · model", done / doing / failed) --
  while the run is going and after it until PROGRESS.md is written again (it
  used to show the previous turn's "9/9 done"). Each worker prompt tells it to
  keep its own "- [ ] Phase N: title" line under "## <run id>" in PROGRESS.md.
- **Live work on the page** (`tests/test_multi_shows_live_work.py`): the
  conversation's turn used to get events only when a phase started or ended,
  so an hour-long phase looked blocked. `_multi_follow_events` now reads
  `status(run_id, with_events=True)` once per `_MULTI_POLL` and
  `_multi_activity` forwards the working phases' NEW tool/note events as
  "Phase N · …" tool lines (<= `_MULTI_ACTIVITY_PER_POLL` per look, clipped;
  a reloaded page sees the last few, not the history). New-ness comes from
  `_Agent.event_total` (monotonic; the ring buffer caps at EVENT_BUFFER).
- **Helpers panel**: during a run the Build page's plan strip becomes
  "Helpers · step N of M": `_multi_run_plan` rows also carry index, title,
  model, `url` (`/agent/<worker sid>`), started/ended, `last`
  (`_multi_last_action`); `helperRow()` in index.html renders state icon + a
  screen-reader word, model chip, elapsed, the last action and "Open ↗"
  (`target=_blank rel=noopener`). A live run refreshes it every 5 s even
  when the page is not in a busy turn (`_planLive`).
- **Where it stopped, next to the message box** (`tests/test_the_work_continues_
  where_it_stopped.py`): `#agent-plan-cut` moved from the plan strip to just
  above `.chat-input-row`; `renderResume()` (inside renderPlan) shows it for
  `mem.interrupted` -- "Interrupted: the hub restarted" for a turn
  `memory.recover_inflight` filed at boot -- with "Continue from there", a
  "type a new message below" hint and a dismiss ×; hidden while a turn runs and
  as soon as anything is sent. `_multi_run_plan` passes `interrupted` through
  when its run is not live, and flags `resumed` for a run the boot picked back
  up (`swarm_windows.resume_interrupted` -- no click needed for Multi).

## Working is always visible (2026-09-30)

Covered by `tests/test_opened_while_working.py`.

- **Opened while working**: opening a conversation from the History list
  (both branches: live session and /resume) now reattaches a running turn via
  `showReconnectedStillWorking` like a page load; it used to mount the
  transcript and say "send a message to continue". `agentic_chat.get_session`
  reads `currently_running` = CLI process alive OR `turn_busy` (the turn owns
  the session between two processes of one turn).
- **Planning shown as it happens**: `_multi_turn_events` yields
  `_MULTI_PLANNING_LINE` at once, runs `swarm_windows.start` (which plans
  before returning) on a thread inside `contextvars.copy_context()`, yields
  "Still planning (Ns)…" every `_MULTI_PLAN_HEARTBEAT`, and after the plan
  the notice is followed by `_multi_plan_lines` (one line per wave, helpers
  that run together named). While it plans, `_MULTI_PLANNING[sid]` makes
  `_multi_run_plan` show "Planning the Multi run (Ns so far)" in the strip.
- **Stop in the whole "working" span**: `stop_session` with no live process
  but the turn lock held sets `_Session.stop_pending`; both spawn sites check
  it under `proc_lock` right after assigning `sess.proc` and terminate that
  process (the turn then ends "stopped" as usual); both turn entries clear it.
  Before this, a Stop before the first process or between two was ignored
  (it also hung `test_send_message_after_stop_reports_interrupted` once the
  page -- and the test -- saw "working" from the lock).
- **A helper's own page** (`tests/test_helper_page_shows_its_run.py`):
  `_multi_run_plan` for a worker session (`swarm_windows.worker_info`) shows
  ITS run (never the project's shared PROGRESS.md), items flagged `this`,
  plus `helper`; the page draws `#agent-helper-bar` ("Helper N of M · title
  -- one step of a Multi run", "Open the conversation ↗") and marks the row
  "this window".

## Restart, Stop and the queue: the owner decides (2026-09-30)

- **Continue after a restart is a choice** (`tests/test_auto_resume_choice.py`):
  per conversation `agentic_history.set_auto_resume` / `auto_resume` (default
  OFF), checkbox `#agent-auto-resume` ("Continue by itself after a
  restart"), `POST /api/agent/sessions/<sid>/auto-resume {enabled}`; GET
  session and /resume rows carry `auto_resume`. Boot:
  `swarm_windows.resume_interrupted(should_resume=_multi_should_auto_resume)`
  resumes a conversation's run only when ticked (owner-less runs as before);
  the rest are filed by `_file_unresumed_runs` (why "hub restarted") so the
  page offers Continue. Turns cut by the restart continue by themselves only
  when ticked (`_auto_continue_turns`, sends `_CONTINUE_TEXT`). The Continue
  button's text counts as `_multi_is_continue` (it resumes the run's
  unfinished phases instead of planning a new run from the sentence).
- **Stop disconnects every CLI** (`tests/test_stop_disconnects_clis.py`):
  `api_runtime_stop` runs `_disconnect_all_clis` (every `_cli_connected` CLI
  through `api_cli_disconnect`) before the shutdown thread, stores them in
  setting `stop_disconnected_clis` and returns `clis_disconnected`; the next
  boot's `_reconnect_clis_after_stop` wires exactly those back (via
  `api_cli_autofix`) and forgets the list. `tests/conftest.py` stubs both --
  they write the owner's real CLI configs.
- **Queued messages after a reload** (`tests/test_queue_after_restart.py`): a
  restored queue starts paused (`_queueRestored`), is never sent by a
  finishing turn, and offers "Send them, in order" / "Clear all"; each item
  stays editable and removable; the interrupted bar says how many wait.
- **Old failures fade** (`_OUTCOME_IDLE_HALF_LIFE`, 8 h): `_reliability`
  halves an UNTRIED pair's lifetime counts per half-life toward neutral, so a
  pair demoted to the tail earns a fresh try in ~1.5 days (was: a week).
- **Any-language difficulty**: `_HARD_HINTS` gained FR/ES/PT/IT/DE stems; 5+
  words with no `_ENGLISH_FUNCTION_WORDS` classify at least medium
  (`tests/test_any_language_difficulty.py`).
- **Leftover agent CLIs stopped at boot** (`tests/test_boot_stops_leftover_clis.py`):
  a hard restart kills the hub, not its children (Windows). MEASURED: five
  opencode workers of an earlier run kept calling the new hub -- quota, pins
  holding the top models, edits in the same project. At boot, before any turn,
  `agent_servers.stop_stale_agent_clis()` stops every process carrying
  `TURN_MARKER` that is an agent CLI (`_is_agent_cli`: opencode/codex/claude/
  kimi/... or node/bun running one); agent-started servers, previews and the
  owner's own terminal CLIs are never touched.
- **Test hygiene**: conftest `_aa_scores_stay_put` (no benchmark refetch, no
  write to the real aa_scores.json, scores restored per test);
  `test_a_locked_run_file_is_retried_not_silently_lost` locks only its own
  file.

## Running now: open anything in a new window (2026-09-30)

Covered by `tests/test_running_now_opens_anything.py`. `runRow(title, sub,
action, onAction, open, nested)` renders an "Open ↗" link (`target=_blank
rel=noopener`, aria-label "… in a new window") for every conversation
(`/agent/<sid>`), helper and preview (its `url`). `GET /api/agent/sessions`
rows of a Multi worker carry `helper` = `swarm_windows.worker_info(sid)`
({run_id, index, title, owner, state}; retried workers via `past_sessions`),
so the popup lists each conversation (working first) with its helpers
indented under it; helpers whose conversation is not open come last.

## Multi plans run side by side (2026-09-30)

Covered by `tests/test_multi_runs_in_parallel.py`. Live run swarm-4f2aab7204a4
was waves [[1],[2],[3],[4]] on a machine allowing 4 at once -- the PLAN was
the bottleneck. `_PLAN_SYSTEM` (and the managed variant built from it) now
says a chain is the slowest plan, that finding and fixing a problem is ONE
phase, and to split by file/area. `clean_phases` ends in `merge_handoffs`: a
look-only phase (`_LOOK_ONLY_RE` title -- locate/diagnose/investigate... --
and no acting verb, `_ACTS_RE`) whose ONE follower needs it is folded into
that follower ("First -- … Then -- …"), needs renumbered. A look phase that
feeds several phases stays.

**No wave barrier** (`tests/test_multi_dependency_scheduling.py`): `_run_phases`
starts a phase once every phase it `needs` finished (a FAILED need does not block),
plan order, <= `_concurrency()`, `SPAWN_STAGGER` apart, review last; `run.waves` is
display only. Verdicts stay one per phase inside its worker (no batch exists here).
Since 2026-10-04 the cap is adaptive and `MAX_AGENTS` is 10 (see "Multi: up to
6 different models at once").

## Multi: up to 6 different models at once (2026-10-04)

Covered by `tests/test_multi_parallel_models.py`. Owner: "4 DIFFERENT models at
once, or 5-6 if needed, working TOGETHER".

- **Formula**: `swarm_windows._concurrency()` = min(`multi_parallel_max`
  (default 6, 1..8), by-machine, by-fleet, 429 back-off). By-machine =
  `min(lowres.workers(cap), free RAM / 0.5 GB, cores)` (weak machine 1-2, free
  RAM < 1.5 GB -> 1, mode "off" skips the RAM term, unreadable numbers -> 4).
  By-fleet = `app._multi_fleet_size()` registered via
  `set_fleet_counter`: distinct healthy tool-capable identities within 6
  points of the best (throttled/parked provider, low-quality, blocked do not
  count), floor 2, 0/unknown = no limit. Back-off = one helper fewer per 3
  `_recent_hop_fail` 429 pairs in 120 s, floor 2, logged `[multi] backing off
  to N (429s)`. This PC: min(6, 8 cores, RAM 37) = 6 unless the fleet is
  narrower. `GET/POST /api/multi-parallel {max, pair_phases, ram_reserve_gb}`.
- **Rotation** (`app._rotate_within_run`): workers of one run get distinct
  identities; among unused models the order is new provider AND family, new
  provider, new family, any (`verify.family`). Band = 4 points of the best; it
  widens to `_RUN_ROTATE_WIDE_DROP` 6 only when it holds fewer distinct models
  than the run needs. Low-quality and user-blocked models never. A worker
  keeps its model (session pin).
- **Planner**: `{helpers}` in `_PLAN_SYSTEM`; a sizeable goal (>= 280 chars or
  3+ listed parts) with >= 3 helpers also gets `_MICRO_ASK` (split big phases
  into file-owning micro-tasks, final integrate/review, `"parallel": true` for
  a big unsplittable phase). Plan check line ends "running up to N helpers at
  once".
- **PAIR mode** (`multi_pair_phases` auto | true | false): a phase with
  `parallel: true` or >= 4 owned files gets a co-pilot session (another model
  via `sibling_sessions`) ONLY when nothing else is pending for a slot. It
  reads/reviews freely, writes only files no phase owns, no installs/servers,
  coordinates via its PROGRESS.md line, is stopped when the lead ends; its
  notes are appended to the phase summary ("CO-PILOT HELPER NOTES").
- **Panel**: `_multi_run_plan` carries `parallel` ({line "N helpers at once
  (max M)", ram_line, waiting}) and per-helper `pair`.

## LMArena board, daily (2026-09-30)

Covered by `tests/test_arena_board.py`. `arena.py` (pure + fetch): LMArena's
own public HF dataset `lmarena-ai/leaderboard-dataset`, config
`text_style_control` (= arena.ai's default board; the plain `text` config is
~10 points off), split `latest`, category `overall`, read through the
datasets-server rows API (keyless, 100 rows/page; MEASURED 353 models, 3.6 s).
Best-rated variant per identity (`normalize`: vendor/relay prefixes, effort /
thinking / date suffixes stripped, "5.1" == "5-1"), >= `MIN_VOTES`. Cached in
`state_dir()/arena_scores.json`; `_arena_refresh_loop` refetches when a day
old (checks hourly; a failed/empty fetch keeps the last board).
`_benchmark_score`: ONLY a model still at the unknown-family 10 after AA, the
family table and the new-version heuristic takes `arena.hub_score(rating)`,
and the final score is capped at `arena.HUB_CAP` (134.5, under every owner
floor) after all bonuses. Known models never move. `/api/tracking` rows carry
`arena_rating` / `arena_rank`; `GET /api/arena` = the board.

## Preview setup commands always end (2026-10-04)

- `workspace.install` (npm install / python -m venv / pip install -r, run
  before a preview starts) goes through `_run_blocking(..., timeout=, label=)`:
  `INSTALL_TIMEOUT` 600 s for the two downloads, `SETUP_TIMEOUT` 120 s for venv
  creation (MEASURED 10.4 s here). Past it `_kill_tree` kills the WHOLE tree
  (Windows `taskkill /F /T /PID`, POSIX killpg of its own session; `stop()`
  uses the same helper), output up to the kill stays in the log, a half-written
  `node_modules`/`.venv` it created is removed, and the preview goes `failed`
  with "<step> did not finish in N s; the hub stopped it -- press Run to try
  again". Before, one hung install left it on "installing dependencies..."
  forever. A non-zero exit still returns None (the start reports it). Output is
  read on its own thread, so a grandchild holding the pipe cannot hang it
  either. Covered by `tests/test_install_timeout.py` (real child + grandchild).

## Observed evidence, not claims (2026-10-04)

Owner-approved items 1-4 of the evidence proposal; item 5 (the hub RE-RUNNING
tests) is NOT approved -- the hub runs no command in a project, it only reads
what the agent's CLI already ran. Covered by `tests/test_evidence.py` and
`tests/test_observed_results.py`.

- **Why** (read-only audit): without a manager any Multi phase that ended with
  a summary was DONE; with one, `verified=True` meant "the manager agreed with
  the summary"; `memory.harvest_commands` filed "Commands verified to work
  here" whenever the REPLY said "green" / "succeeded" / "no errors"
  (`_PASSED_RE`, now deleted). The parsers threw the real evidence away.
- **Parsers** (`agentic_chat`): beside their unchanged events each emits
  `{"event": "tool_result", command, exit_code, is_error, output_tail (<=4000),
  started_at, ended_at}`. Shapes read from the INSTALLED CLIs (comments at
  each parser): codex 0.154 `item.completed` `command_execution`
  {aggregated_output, exit_code, status} (exec_events.rs; command = argv
  shlex-joined, argv[0] = pwsh on Windows -> `evidence.inner_command`);
  Claude Code 2.1.288 `tool_result` "Exit code N" + `is_error` (command from
  the earlier `tool_use` by id, `_CLAUDE_TOOL_CALLS`; is_error false + no
  `returnCodeInterpretation` = exit 0); opencode 1.18.34 `tool_use`
  `state.metadata.exit` (status stays "completed" on a non-zero exit). The
  durable turn forwards only CHECK results (never to the reload buffer); the
  browser ignores the event kind.
- **`evidence.py`** (pure): `classify(command, exit_code, output, is_error)`
  -> PASS | FAIL | NO_TESTS | UNDETERMINED with adapters for pytest, unittest,
  jest, vitest, mocha, cargo test, go test, node --test, bun test, tsc, vite /
  next / webpack builds, and npm/pnpm/yarn/bun test|run build (delegated to
  the echoed script's tool, else the tool's own summary). PASS = exit 0 AND
  the tool's pass summary (builds: exit 0 AND none of its error lines);
  FAIL = non-zero AND a failure in the tool's format; a pipe / `;` / `|| true`
  after the check = exit unknown; never PASS from words.
- **Memory**: a verified command = the LAST observed result for it was PASS;
  the fact links its receipt (`(receipt: receipts/<scope>/<n>.json)`, only
  when it fits MAX_FACT_CHARS).
- **Multi** (with AND without a manager): `agent.evidence` (<= 30 rows,
  persisted). An outstanding observed FAIL (no later PASS covering it,
  `evidence.covers`) is a `_cheap_problems` problem -> the one revision, zero
  manager cost (no manager: only this check runs, so an empty phase still
  fails unretried as before). A summary claiming a pass with none observed =
  `claimed_not_observed` (not a failure). The manager's verdict brief lists the
  observed results. `verified` = observed PASS only; manager OK = `reviewed`
  (old run files: verified True -> reviewed on load).
- **Labels** (`swarm_windows.check_of`): "12 passed (observed)", "2 failed
  (observed)", "claimed, not checked", "reviewed"; on `_multi_run_plan` rows
  (`check`), the helpers panel chip (`.hp-check`), the swarm panel and
  `format_result` ("N verified by an observed test/build run, M only
  reviewed" when anything was checked; a plain run reads as before).
- **Receipts** (`receipts.py`): `state_dir()/receipts/<run id>/<phase>.json`
  and `<session>/<n>.json` per /agent turn that ran a check: command, argv,
  cwd, git HEAD (read from .git files; snapshots git helper with a 5 s timeout
  only as fallback), tool + version, started/ended, exit code, counts,
  verdict, source "observed", SHA-256 of the CHANGED files only (40 files /
  5 MB). json library, atomic replace, LRU-pruned to 500.

## Design, plan, dry run (2026-10-04)

Owner: "design, plan well in a perfect architecture, then go -- and prevent
problems with a DRY RUN in planning". Covered by
`tests/test_plan_design_and_dry_run.py`.

- **Design** (Multi): `_PLAN_SYSTEM` (and the managed variant) asks a build /
  feature plan for `"design": {components, interfaces, data_flow}` before its
  phases and `"files"` (owned paths) per phase; a small fix or a one-phase
  plan gets `{}` (`_plan_design`). `plan_check.normalize_design` reads any
  shape; `clean_phases` keeps `files` (`plan_check.norm_files`, merged phases
  union them). `run.design` and `agent.files` are persisted; every worker
  prompt gets `design_block` (<= `DESIGN_CHARS` 2500, its own files first);
  a plan with no design and no files keeps its old prompt byte for byte.
  `_multi_run_plan` carries `design` ({line "Design: 3 components, 4
  interfaces", text}) -> one collapsed `<details>` row (`designRow`).
- **Dry run** (`plan_check.check_plan`, pure: no model call, no command; reads
  only file NAMES of the project): parallel phases owning one file -> the later
  needs the earlier (fixed); a phase reading a file an earlier non-needed phase
  writes -> needs it (fixed); a file only a later phase writes, or one missing
  from the project and named by no phase -> warn; no concrete done_when /
  acceptance -> warn; `clean_phases(notes=)` reports dropped needs (cycle /
  dangling), dropped empty phases, merged look-only phases (fixed) and phases
  past `MAX_AGENTS` (warn); 3+ phases in one chain -> warn; an enumerated part
  (`swarm.required_parts` + `swarm._uncovered`) no phase covers -> ONE re-ask
  of the FREE planner with the findings and the plan (`dry_run`), the revised
  plan taken only if it covers more; still uncovered -> warn, run goes ahead.
  A check that raises never stops the run (report None).
- **Shown**: `run.plan_check` (persisted) -> one conversation line after the
  plan lines, fresh runs only: "Plan check: N phases, K start now[, planner
  re-asked once], X fixed (...), Y warnings (...)".
- **Single sessions**: `craft.PLAN_PHASES` gained DESIGN (skip for a small fix)
  and DRY-RUN steps: 626 -> 823 chars (+49 tokens), part-funded by folding
  NEEDS into the phases line; the brief ceiling moved 12.5% -> 13% once
  (note in `test_craft_briefs.test_worst_case_brief_cost`). The prose swarm
  already checks coverage (`swarm._uncovered`, `plan:coverage`); untouched.

## Learned model choice (bandit.py) (2026-10-04)

Covered by `tests/test_task_bandit.py`. Owner-approved, Sakana
Conductor/Trinity-style, no training: the hub learns which model is best per
KIND of task from its own measured outcomes. Pure module (stdlib only, no
import from app/swarm/verify, one lock, never raises).

- **Tie-breaker only** (owner rule 2026-10-04: best AVAILABLE models first):
  the benchmarks (AA / LMArena) and the owner floors set the order;
  `MAX_NUDGE` = 1.0, so +1 / -1 spans at most app.py's 2-point
  `_AUTO_TOP_BAND` and a learned favourite never overtakes a model more than
  2 points stronger.
- `task_kind(category, difficulty, tools, est_tokens)` -> e.g.
  `coding|hard|tools|m` (None/"all" -> `any`, unknown difficulty -> `any`,
  `notools`, size band s < 12K / m < 60K / l).
- `Bandit(path=None, clock=time.time, rng=None)`: a Beta posterior per
  (kind, pid, model) and per (pid, model). `nudge(kind, pid, model,
  base_score)` = base + (Thompson draw - 0.5) x 2 x `MAX_NUDGE` (1.0),
  drawn from the kind posterior once it holds >= 3 observations, else the
  model's global one, else the prior Beta(1,1) (0 on average).
  `propensity_note` -> `{alpha, beta, n, source: kind|model|prior}`.
  Evidence decays toward the prior with a 7-day half-life (`HALF_LIFE`).
- `reward(kind, pid, model, value)`: value in {0, 0.5, 1}, else ignored.
  QUALITY ONLY: a 429, an abandoned swarm/hedge member, a client that went
  away or a deadline cut is never rewarded (not even 0).
- Tool turns: `remember_tool_calls(ids, pid, model, kind)` (LRU 5000, TTL
  2 h) + `credit_from_messages(messages, grade)` rewards
  `grade(tool_message)` once per remembered role "tool" `tool_call_id`
  (None = skip, retried later); the repeating history never credits twice.
- `stats(limit)`, `save(force=False)` / `load()` (json, atomic replace,
  autosave <= 1 per 30 s; missing/corrupt file -> empty), `default` (in
  memory) and `configure(path)` to point it at a file.

## Verifier and corrector (verify.py)

Owner-approved design (Sakana Trinity-style ACCEPT/REVISE): an independent
VERIFIER from a DIFFERENT model family checks a producer's proposed next
message; on REVISE a CORRECTOR writes the fixed one. `verify.py` is the pure
half (stdlib + `ctxwin` only, no model call, no app import); app.py decides
when to verify and dispatches. Covered by `tests/test_verify.py`.

- `family(model_id)`: vendor family in any spelling (relay/host prefixes,
  `:free`/`-free`, Ollama tags, case): kimi, glm, qwen, deepseek, gemini,
  gemma, llama, mistral, claude, gpt, gpt-oss, minimax, mimo, nemotron, grok,
  cohere, phi, else `unknown:<identity>` (each unknown its own family).
  Ordered patterns resolve two-vendor names (nemotron before llama, claude
  before gemini for g4f's `gemini-claude-opus-*`, deepseek before a distill's
  base). `identity()` mirrors `app._normalize_model_identity` (test-pinned).
- `pick_verifier(producer, candidates)` -> `(pid, model)` or None: other
  family > other model > other provider > score; never the producer itself.
- `is_risky(tool_calls, difficulty, observed_pass=None)` (`tool_calls` may
  be the call list, the whole assistant message, or its text): hard turn +
  a writing/editing/patching/deleting/running call (name words AND argument
  shapes incl. opencode camelCase); a plainly read-only shell command (`ls`,
  `cat`, `git status`, no redirect / `$(...)`) and an editor `view` are NOT
  risky. Or, on any non-`simple` turn, text claiming done/fixed/tests pass
  while `observed_pass` is not True (hedged plans and "N failed" reports are
  not claims).
- `digest(messages, proposed)`: system contract + ONE user message (last real
  instruction via `ctxwin.is_real_instruction`, last 2 tool results, the
  proposal), head+tail clipped by weighted water-filling to
  `DIGEST_MAX_CHARS` (20000). Dispatch with `VERIFY_MAX_TOKENS` (300).
- `parse_verdict(text)`: fenced / prose-wrapped / lenient JSON,
  `"verdict": "ACCEPT"|"REVISE"`, bare `REVISE: ...`; unparseable = ACCEPT
  with `unparsed: True` (fail open).
- `corrector_messages(messages, proposed, verdict)`: original conversation +
  the proposal + a user note (`CORRECTOR_NOTE_HEADER`) listing the problems.
  A tool-call proposal stays a native `tool_calls` message, each call answered
  by a tool message `NOT_EXECUTED_TEXT` — providers 400 on calls left without
  results.

## Pipelines verify and search (2026-10-04)

Owner-approved (Sakana Trinity / Conductor / AB-MCTS adapted, no training).
Covered by `tests/test_pipeline_verify_and_search.py`. swarm.py, crews.py and
swarm_windows.py never import verify.py: every capability is an INJECTED
kwarg whose default is the old behaviour.

- **Reviewer family** (`swarm.run(family=)`, `crews.run(family=)`):
  `family(who) -> str`, called with the dispatch's `"pid/model"`; default
  `swarm.default_family` (last path segment's leading letters: kimi, llama,
  qwen). The free review gets `avoid_families=(<producer families>)` next to
  `exclude_pids` -- ONLY when `family` was injected or the dispatch NAMES the
  keyword (`_pipeline_bound`'s `**kw` does not count: it would forward it to a
  `_swarm_dispatch` that rejects it). **app.py must**, before passing
  `family=`: accept `avoid_families=()` in `_swarm_dispatch` (the review is
  the only caller; fast_dispatch never gets it) and PREFER candidates whose
  family is not in it (tail, never exclude, never fail for it). Result `review_family` = {producers, reviewer, distinct,
  hinted} when a free reviewer answered (not for a manager review).
- **Crews**: write/design carry `"revise_on": "high"` (max_revisions stays 0)
  and their reviewers add `"severity": "high"|"medium"|"low"` (`_SEVERITY_RULE`);
  `swarm.review_severity` reads a top-level field, per-problem objects or a
  "[HIGH] ..." tag. HIGH -> ONE revision (the same `_free_revision` /
  directed fix); medium/low/none -> synthesis only. code/research unchanged.
  `swarm.review_problems` turns object problems into text.
- **Multi free verdict** (`swarm_windows.start/resume/resume_interrupted(
  free_verdict=)`, not persisted, like the manager): no manager, a finished
  phase with NO observed PASS and no outstanding FAIL, and a risky outcome
  (`_risky_outcome`: claims checks pass with none observed, admits it is
  incomplete, or changed source files with no check run) -> ONE call
  `free_verdict(brief)`; `brief` is a dict (run_id, goal, phase, title, task,
  done_when, acceptance, summary, changed_files, observed, reason, `text` = the
  manager's verdict brief). Expected reply `{"ok", "problems", "severity"}`
  (verify.parse_verdict); not ok + high/critical -> the existing one revision;
  anything else, None or an exception -> unchanged. `agent.free_check` is
  persisted (row `free_check`).
- **Wider or deeper** (`search=` on swarm.run / crews.run / start / resume /
  resume_interrupted; None = off; True = a fresh `swarm.Search`; a Search
  instance for tests): only where a scorer exists. Swarm: a phase the free
  checks reject (`_cheap_problems` count, 0 = accepted) -- in `_verify_set`
  step A instead of the one retry, and after each wave without a manager.
  Multi: an outstanding observed FAIL (`_observed_score` = failed tests).
  Each extra attempt is WIDER (swarm: fresh phase prompt, every tried
  provider excluded; Multi: fresh session, and `sibling_sessions` names the
  worker's own past sessions while `agent.widen`, so app's existing rotation
  picks another model) or DEEPER (swarm: `_retry_msgs` on the best attempt,
  no exclusion; Multi: the SAME session continues), by Thompson sampling on
  Beta(1,1)-seeded per-run posteriors (success = strictly better score).
  Cap `SEARCH_MAX_EXTRA` = 2; past the first (today's one retry/revision) an
  attempt needs `SEARCH_MIN_SECONDS` (45) on the swarm clock / the phase's
  AGENT_TIMEOUT room (`_search_time_ok`). The best attempt is never replaced
  by a worse one; a Multi search attempt that produces nothing leaves the
  earlier one standing. Records (phase, attempt, choice, success,
  score_before/after, accepted, forced, at) -> swarm `result["search"]`,
  Multi run row `search` (persisted; a resume re-seeds from its log).
  A manager judgement is not a scorer: it keeps the single revision.

## Roles instead of racing (2026-10-04)

Covered by `tests/test_tool_turn_roles.py`. MEASURED hub.log 2026-09-26..10-04:
the swarm/crew*/multi tool-turn race sent one turn to 3.72 models, 4.02
upstream calls per served answer, 75% of member calls (2897/3857) served
nothing, each resending the whole prompt, and caused the hub's own 429s.
Owner: "each model must do something -- collaboration, not racing."

- **Switch**: flag `tool_turn_race` (default OFF). Off, `_swarm_tool_result`
  (every protocol: `_swarm_tool_turn`, `_swarm_for`) returns
  `_tool_turn_roles(body)`; on, the old race below it runs unchanged. Same
  contract: (data, headers) or None -> the caller falls back to `best`.
  `tests/conftest.py` `_RACE_TESTS` pins the race for the tests written
  against its member grace / labels / ranking.
- **Actor**: ONE model = the router's pick (`force_difficulty="hard"`), then
  the chain (`_build_chain`, subs dropped) walked with `_ChainClock.walk`
  (tool demotion, relay caps), <= `_ROLE_MAX_ACTOR_HOPS` (4), non-streamed
  (`_dispatch_chat_with_deadline`), each call on its own hop token
  (`_role_start_leg`). A failed hop = 429/5xx/exc/empty/junk, an INVALID tool
  call (`_swarm_tool_calls_valid`), a refusal / no-tools claim / typed call /
  announcement (`_role_judge`, the race's member checks). A clean text final
  answer is served. Budget: stream `_SWARM_TOOL_STREAM_DEADLINE` (180 s),
  else `_SWARM_TOOL_HOP_DEADLINE`, never past the request clock (started
  here, so the fallback shares it).
- **Stall backup** (`_ChainClock.plan_tool_hedge` / `tool_hedge_partner` /
  `fire_tool_hedge`, flag `hedge_tool_turns`): ONE extra actor per turn, on
  another provider first (the trivial hedge's partner rule minus
  `_swarm_member_sick`), after `_tool_hedge_delay` = max(6 s, 3.5 x the
  pair's `_tool_ttft` p50; 45 s unmeasured). Every successful role call adds
  its duration to `_tool_ttft`. First valid answer wins; the other leg's
  token is cancelled (its call is cut and files nothing).
- **Verifier** (`verify` module, lazy `_verify()`): only when
  `verify.is_risky(<whole assistant msg>, real difficulty, observed_pass=
  _observed_pass(messages))` (evidence.classify on command tool results).
  `verify.pick_verifier(producer, [(pid, model, score)])` over healthy chain
  entries (`_role_candidates`), one non-streamed call, `VERIFY_MAX_TOKENS`,
  `_no_craft`, <= `_VERIFY_DEADLINE` (25 s). Any error / timeout / unparsed
  verdict (`"unparsed": True`) = fail-open, ship the original, no reward.
- **Corrector**: only on ok=false + severity "high", ONE call to the best
  other actor (another identity) with `verify.corrector_messages` sent as-is;
  a tool step must pass `_swarm_tool_calls_valid`, prose only replaces prose,
  else the original ships. Needs `_CORRECT_MIN_SECONDS` left.
- **Max text** (`_max_text_review`, all three non-stream success points):
  model best/max, difficulty hard, no tools, non-streamed -> the same
  verifier/corrector. Never on streamed or Normal turns. Kill switch for both:
  flag `turn_verifier` (default on).
- **Bandit** (`bandit` module, lazy `_bandit()`; PyPI's `bandit` linter is
  rejected by attribute check): `_task_kind` = bandit.task_kind(mode,
  difficulty, tools, est). OWNER RULE: nudge <= `_NUDGE_CAP` (1.0) and ONLY
  among candidates within `_AUTO_TOP_BAND` (2.0) of the best available score
  (`_band_scores`: a nudged member never drops under the band floor):
  `_auto_top_band(kind=)` (auto + Multi worker pick, after
  `_rotate_within_run`), `_swarm_rank` (`_nudge_in_band`), verifier/corrector
  pools. Never for `_is_low_quality`; never touches floors or the blocklist.
  Rewards (quality only, never 429/5xx/timeout/hub-cut/client-gone): verifier
  ok 1, revise 0.5, junk/invalid/prose-instead-of-action 0. Every served tool
  call -> `remember_tool_calls`; each /v1 request (chat, responses, messages)
  starts with `_bandit_credit(messages)` -> `credit_from_messages(messages,
  _make_bandit_grade(messages))`: observed PASS 1.0, FAIL 0.5, else an error
  marker 0.5 / clean 1.0. `bandit.configure(state_dir()/task-bandit.json)` at
  BOOT only (`_bandit_boot`).
- **Log**: one row per role turn in `state_dir()/turn-roles.jsonl` (rolled to
  `.1` at 5 MB; kind, actor, nudge, hedge, verifier, verdict, severity,
  corrector, corrected, served, calls, input/sent tokens, latency, failed
  hops, invalid) + `{"event": "credit"}` rows. `scripts/role_eval.py` compares
  it with hub.log's `[swarm-tools]` race lines, offline.
- **Activity / headers**: crew "swarm (roles)" / "max (verified)", chips
  "actor", "actor: HTTP 429", "backup (stall)", "verifier: ok|revise",
  "corrector", "corrector: kept the original (...)"; header
  `X-Free-LLM-Hub-Roles: actor_hops=;backup=;verifier=;corrected=;calls=`.
- **Pipeline helpers** (wired by the pipelines after merge):
  `_swarm_dispatch(..., avoid_families=())` / `_swarm_fast_dispatch` put
  candidates whose `verify.family` is listed at the BACK of the stage chain
  (`_avoid_families_last`; never excluded, no-op without verify);
  `_verify_family()` -> `verify.family` or None; `_free_verdict(brief)` with
  `brief["text"]` (+ optional `producer`, `avoid_families`) -> {"ok",
  "problems", "severity"} or None (fail-open).
- **Stalls are remembered** (2026-10-04, `tests/test_tool_turn_stalls.py`).
  MEASURED live: nvidia/z-ai/glm-5.3 "silent 45 s -> backup" at 17:02 and
  again first at 17:04. An actor that loses to its backup (silent for the
  whole delay) gets a CENSORED `_tool_ttft` sample (>= the silence) plus
  `_note_recent_hop_failure("timeout")` + `clock._note_stall` (`_note_actor_stall`),
  so `_build_chain` puts it in the stall tail and `_tool_turn_slow` sees it.
  A backup that loses, or an actor that answers first, files nothing; a
  client that left files nothing; an actor win clears the mark.
- **Backup delay** (`_tool_hedge_delay(pid, model, est)`): unmeasured pair =
  fleet median of per-pair tool-turn p50 (pairs with >= 5 samples) x 2.5,
  clamped 12-30 s (30 s with no fleet data); 45 s only at >= 100K tokens.
- **Budget**: an actor hop gets room / attempts_left (3 -> 2 -> 1, floor 20 s,
  `_role_share`); the backup likewise; deadline constants unchanged.
- **Empty-200 streak**: 3 empty/junk 200s from one pair in 10 min rest it for
  tool turns (`_empty_resting`): walked last, and `_orch_unusable` skips the
  orchestrator pin (`[orchestrator] pinned <pair> resting (3 empties)`); a good
  answer or the TTL clears it.

## Team notes: parallel specialists for hard tool turns (2026-10-04)

Covered by `tests/test_tool_turn_specialists.py`. A CLI turn yields ONE next
action, but the thinking around it can be shared. Inside `_tool_turn_roles_run`
(before the actor walk), `_team_notes_for_turn` may run up to 3 read-only
SPECIALISTS in parallel and hand the actor their merged notes.

- **When** (`_specialists_wanted(body, kind)`): flag `tool_turn_specialists`
  (default ON), `lowres.active()` false, the conversation ENDS on a real user
  instruction (opening or fresh follow-up -- never a tool-result
  continuation), real difficulty `hard`, not `_is_trivial_ask`, not
  `ctxwin.is_compaction_request`, est <= `_TEAM_MAX_EST` (60K). Otherwise the
  roles turn is byte-for-byte as before. Pipeline tiers only (it lives in the
  roles path); Multi on /agent and prose swarm/crews are untouched.
- **Who**: `_team_pick` over `_role_candidates` (the routed actor pair, subs,
  sick pairs and last-resort families out; bandit nudge only inside the top
  band): DIFFERENT identities, preferring a new provider AND family
  (`verify.family`), then a new provider, then any new identity. Roles:
  SCOUT (files/symbols/commands visible in the digest, "unknown" over
  invented paths), CRITIC (risks, how to verify, what not to do), plus
  DESIGNER when `craft.is_web_ui(goal)` or the goal creates something new.
  Each: no tools, non-streamed, `_no_craft`, <= 700 tokens, <= 25 s, on its
  own `clientgone.child` token, input = `_team_digest` (last instruction,
  last 3 tool results, tool NAMES only; ~6K tokens max).
- **Orchestrator** (`_orchestrate_brief`, no model call): drops empty /
  "unknown" parts, dedupes lines across parts, orders scout -> design ->
  risks, labels, clips to 2500 chars (each part keeps a share). Injected as
  ONE system message after the leading system messages of the ACTOR's request
  only (`_with_team_notes`); verifier / corrector / specialists never see it.
  Failed specialist = omitted; none answered = plain roles.
- **Cost per hard fresh turn**: +2 or +3 calls of ~6K tokens in, <= 700 out;
  0 on every continuation. Cache `_team_cache` (key `ctxwin.conversation_key`,
  fallback first-instruction hash): same instruction within
  `_TEAM_CACHE_TURNS` (3) assistant turns reuses the brief on continuations
  and retries (no calls); a NEW instruction inside that window runs no team
  and gets no stale brief.
- **Nothing learned from specialist text**: no bandit reward/punishment. A
  specialist 429/5xx/timeout is filed like any hop failure; when the client
  left, tokens are cancelled and nothing is filed.
- **Visible**: activity chips `specialist: scout|designer|critic` (+ `: why`
  on failure) with the model, then `actor`; `X-Free-LLM-Hub-Roles` gains
  `specialists=N` (answered); `turn-roles.jsonl` rows carry `specialists`
  [{role, model, ok, ms, why}], `brief_chars`, `team` (ran | cached |
  skipped); `scripts/role_eval.py` reports `team_turns`,
  `team_specialist_calls`, `team_brief_chars_avg`. conftest turns the flag
  off for every test file except this one.

## Model guides (weak and specific models) (2026-10-04)

Owner: "make ANY model, even weak ones, work as well as possible". Covered by
`tests/test_model_guides.py`. `model_guides.py` is pure (stdlib, never imports
app, never raises); not wired into routing yet -- the caller passes
`verify.family` as `family`.

- **Files** `model_guides/<family>.md`, each <= 1200 chars of bullets ("Do X.
  Never Y."), `<!-- evidence: ... -->` comments first (stripped before use),
  optional `## any` / `## tools` / `## answer` sections picked by `task_kind`
  (a chat answer is never told how to call tools). A family gets a file ONLY
  with evidence. hub.log 2026-09-12..10-04 + measured code notes:
  deepseek (8 canary junk, 13 stream-gate cuts, 3 junk-benchings, 8 duplicate
  tool calls, DSML markup as text, invented apply_patch shapes), glm (ran on
  past the answer into other scripts and `</arg_value>`, 3 identical bash
  calls, junk-bench), kimi (4 canary misses narrating "The user is asking...",
  `<|close|>!!!` template junk, native call markup as text), minimax (narrated
  canary, tool JSON leaked into the text channel), gemini (5 cut-number canary
  misses: hidden reasoning ate the budget). No file for qwen / llama / gemma /
  mistral / claude / gpt / nemotron / gpt-oss: no behaviour evidence (their
  log failures are 429s, timeouts, relay empties, or hub-side context sizing).
- `weak.md` (generic scaffolding) and `strong.md` (two lines: do not
  over-instruct). `guide_for(model_id, family, score, task_kind)` = "MODEL
  GUIDE" + family guide + (weak.md if `is_weak(score)` else strong.md),
  trimmed to `GUIDE_MAX_CHARS` (1500) on whole lines, family lines first. The
  caller's family name is normalised (z-ai -> glm, moonshot -> kimi...); when
  it names nothing with a file, builtin id patterns try (gemma never reads as
  gemini). Reads cached per mtime.
- **`WEAK_SCORE` = 120**: every owner floor >= 133, Arena newcomers capped at
  134.5, category evidence from 130 -- the strong band; under it the family
  tiers (S 100, A 84, B 56, llama 44, C 26, unknown 10). arena.py maps 120 to
  Arena ~1445 (minimax-m3 "~120 by strength" and claude-haiku-4.5 under it).
  Unknown / unreadable score = weak (a missing guide costs a failed turn).
- `scaffold(score, task_kind)`: weak only -> `max_step_scope` "one file or one
  function", `always_verify`, `context_budget_tokens` 12000
  (`STREAM_BIG_REQUEST_TOKENS`) or 8000 under the medium floor 50 (8K-cap
  hosts), `temperature` None (the cards disagree: Gemini 3 loops below 1.0,
  Qwen3.8 instruct 0.7, GLM-4.7 agentic 0.7), a kind-specific `checklist`.
  `sampling_for(model_id, task_kind)` returns a model card's sampling ONLY for
  the version that card covers (DeepSeek-V4, GLM-4.7, Kimi-K2.6,
  MiniMax-M2.7, Gemini 3, Qwen3.8; fetched 2026-10-04), else {}.
- **ECC** (github.com/affaan-m/ecc, MIT, (c) Affaan Mustafa; ideas adapted in
  our own words, nothing vendored or fetched at runtime). Adopted into the
  guides: fix incrementally and verify each fix; cheapest check first, a
  failing build stops the loop; keep a weak model's working context well
  inside its window. Already in the hub: plan first (`craft.PLAN_PHASES`,
  plan_check.py), verify loop (`craft.VERIFY_RUN`), read code before changing
  it / never swallow errors (`craft.PROGRAMMING`), security brief, fresh-eyes
  reviewer (swarm / crews), session memory and learned facts
  (`memory.harvest_facts`), context compaction (ctxwin), cost-aware routing
  (difficulty tiers). Skipped: per-language rule packs (context tax on every
  turn, no hub evidence per language), TDD with an 80% coverage target and
  file/function size limits (over-instructs strong models, not evidenced
  here), repository-pattern / API-shape conventions (architecture taste, not
  model reliability), harness hooks and AgentShield (client-side, not
  something a model guide can do).

## No AI slop: prevented in planning, checked in output (2026-10-04)

Owner: "In web design: no AI slop, no AI watermark/tells -- perfect. And slop
must be prevented from the BEGINNING, in planning and in designing the
architecture, not only caught at the end." Covered by `tests/test_slopcheck.py`
(every rule both ways + the false positives each was tuned against).

- **`slopcheck.py`** (pure, stdlib, no model call). `check_design(text,
  request="")` before code, `check_files(paths_or_texts, kind=None)` on output
  (HTML/CSS/JSX/TSX/Vue/MD; node_modules, vendor and `*.min.*` skipped). A
  finding is `{rule, severity, where ("file:line" | "design"), why, fix}`.
  `contrast_ratio` is WCAG 2.x (translucent text composited first);
  `summary` scores each rule once (high 15 / medium 7 / low 2, +3/+1/0 per
  repeat, max 4).
- **Output rules**: placeholder (lorem; "Your Company" only as a NAME --
  copyright line, alone on a line, "... Name"; John Doe; @example.com; 555 and
  01 23 45 67 89; "Feature 1/2" needs two numbers), ai_credit (built/made with
  an AI tool, generated/written by, generator meta: AI tool high, Hugo-style
  low; "Powered by AI" and "made by Claude" are left alone), no_viewport (whole
  documents only), img_alt (missing high; empty alt medium only on a
  content-named image), contrast (colour + background in the SAME rule, var()
  from :root; gradients, translucent backgrounds, placeholder/disabled
  skipped), ai_gradient (a violet stop with blue/pink partners and no warm
  stop; Tailwind from/via/to; high on hero/body/header), outline_none (unless a
  :focus rule replaces it), no_focus_styles (low), ai_copy, fake_proof (round
  stat with no source marker; LLM-favourite testimonial names / fake
  companies, never "Sarah M."), emoji_icons (2+ at the start of nav / button /
  li / heading text or `icon:`; check, star, close glyphs excluded),
  dead_link (`href="#"`), fixed_width (>= 600px outside media queries without
  max-width), identical_cards (>= 3 card-grid sections, >= 60% of >= 4),
  centered_everything, generic_fonts (EVERY text family Inter/system),
  glassmorphism (blur on 3+ elements), reduced_motion (3+ animation signals or
  an animation library, no prefers-reduced-motion).
- **Design rules**: HIGH = default_gradient (not when the request names the
  colour), vague_style (2+ adjectives, no hex, no face), no_palette (< 2 colour
  values and no "existing palette / brand colours / stylesheet"),
  no_type_pairing (no face, no system stack). MEDIUM = one face only,
  slop_font (WEB_DESIGN's ANTI faces unless requested), stock_skeleton (hero +
  3 stock sections, no reason word), no_layout_concept, no_copy_source.
  Negations ("no purple gradient", "avoid Inter, Poppins") rule a mention out.
- **Planning** (`plan_check`): `normalize_design` keeps `design["visual"]`
  ("palette: ...", type, layout, motion, copy, look; from `{"visual": {...}}`
  or top-level keys; idempotent); `render_design` puts it FIRST so a clip never
  cuts it; `design_line` adds "visual decisions". `check_plan` step 6: a web
  BUILD (`craft.is_web_ui(goal)`, 2+ phases or a design, not a fix-only goal,
  web_design skill on) runs `check_design(design_text(design, phases),
  request=goal)`: high -> `design_slop` "replan" (the EXISTING one re-ask;
  `replan_ask` adds each fix and where the look goes), the rest -> warnings on
  the "Plan check:" line. Non-web goals are untouched.
- **Single sessions** (`craft`): `DESIGN_FIRST` (479 chars) ships on
  web_design turns after PLAN FIRST (tools) / before VERIFY_READ (tool-less):
  hex palette, a named pairing, layout + why, motion + reduced-motion fallback,
  real copy or [NEEDS INPUT]; a helper given a shared DESIGN uses it.
  WEB_DESIGN +353 chars (pairing replaces "one family", hex + 4.5:1, viewport /
  max-width / alt, focus-visible, placeholder and credit tells), part-funded by
  tightening three lines. Ceiling 0.13 -> 0.135 once (heaviest 13.27%).
- **Wired (`tests/test_slop_wiring.py`)**: (1) `swarm_windows.plan_system(goal,
  managed)` appends `_WEB_PLAN_ASK` (~110 tokens: the `design.visual` shape) for
  a web/UI goal only (`craft.is_web_ui`, web_design skill on); other goals get
  the base prompt byte-for-byte. (2) crew-design's `plan_system` / `phase_system`
  carry `craft.DESIGN_FIRST` (one text; stripped when the skill is off) and its
  profile has `slop_check: True`. (3) Output: `verify.slop_report` /
  `slop_problems` / `html_blocks` / `read_web_files` (lazy slopcheck, <= 40
  files, <= 1 MB each, never raises). Multi: `_verify_and_revise` scans the web
  files the phase CHANGED (`_slop_scan`); HIGH findings join the cheap problems
  (the ONE revision, no model call), the rest stay warnings in `agent.slop`
  (`{line: "Slop check: N high, M medium", warnings, problems, ...}`, in the row,
  the phase receipt and the saved run). A phase that wrote no web file is never
  scanned. Prose swarm / crews: HTML in the final draft's code fences (crew-design
  or a web brief) is scanned before the revision decision; HIGH findings are
  problems and earn the one bounded revision even on a "ship" verdict; the line
  rides on `result["slop_check"]`. Still open: app.py's verifier/corrector does
  not call `verify.slop_problems` yet (agent G).

## How the pieces are wired (2026-10-04)

Covered by `tests/test_wiring_roles_guides_pipelines.py`.

- **`tool_turn_race`** (default off): roles instead of racing, see above.
- **`pipeline_search`** (default on): `_pipeline_check_kwargs()` -> `family=
  _verify_family()` + `search=True` for `swarm.run` / `crews.run` (the
  `_swarm_completion` call and the MCP crew runner); `_multi_check_kwargs()` ->
  `free_verdict=_free_verdict` + `search=True` for `swarm_windows.start` /
  `resume` / `resume_interrupted` (Multi turn, boot resume, the REST and MCP
  starts). No `verify` module = no family / free_verdict; flag off = no search.
- **`model_guides`** (default on): `_with_model_guide` runs inside
  `_upstream_chat` (per HOP, after `_apply_craft_brief`, so it follows the model
  that answers; never for `_no_craft` pipeline-stage calls). `_model_guide_text`
  = `model_guides.guide_for(model, verify.family, _benchmark_score, "tools"|
  "answer")`; a weak model also gets the scaffold (step scope, context-budget
  hint, checklist), total <= 1500 chars. Multi workers are CLI sessions whose
  model is picked per hop, so scaffolding rides this hop path, not the phase
  prompt. A weak actor (`_model_is_weak`) is always verified in
  `_role_verify_and_correct`. Routing is untouched.
- **`turn_verifier`** (default on): kill switch for verifier + corrector.

## Swarm distinct models, team reliability (2026-10-04)

Covered by `tests/test_swarm_distinct_models.py` and
`tests/test_team_and_verifier_reliability.py`. Why (turn-roles.jsonl, 214 role
turns): team notes ran on 3.7%, 5 of 12 specialist calls answered (4 hit the 25 s
limit; answered p50 11.9 s / p90 19.4 s), the verifier gave a usable verdict on
21% (15 of 19 "no verdict"), and prose swarm/crew workers all clustered on the
top 1-2 models.

- **Distinct workers**: `swarm.run(ledger=True)` (crews forward it) makes one
  `swarm.RunLedger` per run and passes `ledger=` to `dispatch` on WORKER calls
  only (first attempt, retries, wider attempts). `_swarm_dispatch(ledger=)` ->
  `_distinct_first`: the chain is re-ordered so the worker opens on a model
  identity no other worker of the run holds (then unused family, then unused
  provider), inside `_AUTO_TOP_BAND`, widened to 4 points only when the narrow
  band has none; never a last-resort family, never an excluded provider, never
  dropped (only re-ordered); the pair is reserved under the ledger lock (a
  failed hop gives it back). Flag `swarm_distinct_models` (default on) via
  `_pipeline_check_kwargs()`. Result gains `workers_models`.
- **Phase verdict**: `swarm.run(free_verdict=)` (no manager): a code/format-bound
  phase (`swarm.verdict_bound`) whose free checks pass gets ONE `_free_verdict`;
  not-ok + HIGH -> the existing single retry on another provider (kept only if
  the free checks pass); `result["phase_verdicts"]`. Flag `swarm_phase_verdict`
  (default on). None / unreadable / raising = unchanged.
- **Specialists by measurement**: `_team_stats` (in memory, seeded once from the
  last 500 rows of turn-roles.jsonl) ranks the pool by answered rate (Beta(1,1))
  then p50, only inside the top band; 2 failures in the last 3 calls rest a
  pair for 30 min; `_TEAM_HOP_SECONDS` 25 -> 35. Verifier pool: `_rank_verifier_pool`
  puts measured usable-verdict rate before score inside the band (family
  diversity still first in `pick_verifier`).
- **Verifier contract**: `VERDICT: ACCEPT|REVISE` FIRST, then `PROBLEMS:` bullets
  and `SEVERITY:` (survives a reply cut by the token cap); JSON still accepted;
  the parser also reads the verdict mid-text, bare sentences ("looks correct",
  never high severity). Unreadable reply -> ONE retry on the next verifier with
  `verify.STRICT_VERIFIER_SYSTEM` (also in `_free_verdict`); `verifier_unparsed`
  / `verifier_retry` in turn-roles.jsonl and scripts/role_eval.py.
- **Medium gate**: `_specialists_wanted` also accepts a MEDIUM fresh build /
  implement / refactor instruction (`_medium_build_ask`, est <= 30K, not a
  question or one-line edit), scout + critic only; flag
  `tool_turn_specialists_medium` (default on). MEASURED: the log's 188 medium
  turns are agent-loop continuations (min 39K, p50 139K tokens), so this gate
  adds ~0 calls on that traffic; it fires on fresh opening turns only.

## Provider fairness (2026-10-07)

Covered by `tests/test_provider_fairness.py`. Owner: "it doesn't use all
providers equally; groq and other providers are almost never used." MEASURED
over 7 days of tool turns (`turn-roles.jsonl`, 2329 turns): nvidia served
**29.6%** of all served tool turns (tried 1076x, median 65.7 s, 113 timeouts)
while it piled up; uncloseai 13.2%, g4f 13.6%, kilocode 12.6%, dahl 12.1%,
openrouter 10.4%, google 7.1%; **groq tried 68x, served 0** (its free tier caps
one request at ~8K tokens and **97% of tool turns are >= 60K** -- only 28 of
2329 were < 12K -- so its instant `_ContextOverflow` is a real provider limit,
not a bug). Band coverage: small (<12K) 6 providers tried / 4 served, medium
(12-60K) 11 / 9, big (>=60K) 12 / 8. The band holds several equally-good
providers, but `_weighted_pick` + the session pin kept landing fresh sessions
on the same strongest host (the `[spread]` log showed pool size 1 on 863 of
1031 picks -- nothing to spread for a lone session).

- **The rule**: `_fair_spread_band(band, est)` runs between `_auto_top_band`
  and `_weighted_pick` in the agentic pick (`_route_by_difficulty`'s
  `_spread_pick_lock` block, and the trivial quick-turn pick). It NEVER crosses
  the band -- benchmarks + owner floors still set the order, a 134 never beats
  an available 138 -- it only NARROWS the band (every member already within
  `_AUTO_TOP_BAND` = 2.0 of the best, the owner's "equally good") to the
  provider carrying the least current load, and FAILS OPEN at every step (the
  full band comes back whenever a narrowing would empty it, and a cold fleet is
  left untouched so the weighted pick behaves exactly as before).
- **Load** = in-flight upstream requests + recent (2 min) hop failures + 2x the
  15-min routing share (`_provider_load`). Steps: (1) small request (est <
  `_FAIR_SMALL_EST` = 8000) prefers FAST providers in the band -- this is where
  groq/cerebras-class SHOULD be used often, and the band is already window-
  filtered (`_context_ok`) so a too-small window is never chosen; (2) a provider
  at/over the per-provider in-flight soft cap (setting
  `provider_inflight_soft_cap`, default 3) yields; (3) a provider carrying
  >= 50% of the last 15 min of picks yields; (4) the least-loaded provider(s)
  win. Flag `provider_fairness` (default on).
- **In-flight counter** (`_inflight_inc` / `_dec` / `_count`, thread-safe)
  wraps `_dispatch_chat`: a non-stream hop is counted for the whole call; a
  stream stays counted until its response object is finalized (whole stream
  duration), via `weakref.finalize`. **Routing picks** are logged per turn
  (`_note_route_pick`, incl. pinned turns) into `_ROUTE_LOG` for the 15-min
  share -- so a provider carrying real traffic (even via pins) raises its own
  share and fresh sessions spread away from it.
- **Visible**: `GET /api/provider-load` (control-token gated) -- per provider:
  `inflight`, `routed_15m`, `routed_24h`, `share_15m`, `recent_failures`,
  `load`; plus `total_inflight`, `soft_cap`, `fairness_on`.

## Big tool turns that used to time out (2026-10-07)

Covered by `tests/test_cli_big_turn_reliability.py`. MEASURED (hub.log, 3 h,
OpenCode `coding-swarm`, ~77-79K-token stream tool turns): 25x
`[quality-fallback] best->auto -> groq/qwen3.8-27b` (an ~8K-TPM model picked
for a 79K request), roles actors "no answer in time" on nvidia/kimi-k3 and
glm-4.7-flash, 4+ min turns ending 503, ConnectionError bursts (5 hops in ~4 s),
a 403 and "a tool call the CLI cannot run" re-dispatched every hop, and a 503
blaming "35 model(s) are switched OFF" when none could have held the request.

- **ConnectionError bursts are NOT the hub's clientgone work.** REPRODUCED
  (raw-TCP fake provider, real `requests`, the clientgone urllib3 hook
  installed): the hub posts per hop with `requests.post` (a FRESH pool each
  call -- there is no shared `requests.Session` anywhere), so a socket
  `Token.cancel`/`nonblocking_close` shuts down is never handed to a later
  call; and even in the worst case (a shared pool) urllib3 2.x discards a
  dropped pooled socket and opens a fresh one. The cancel token never leaks:
  the main thread keeps the request token (`set_current(None)` guards a reused
  thread), child hop tokens live only on their worker thread. So the bursts are
  genuine transient upstream resets (already noted at app.py ~10228). What
  changed: the `CHAT/RESPONSES/MESSAGES-503`/`-DEADLINE` log lines now carry
  `details=[...]` -- each hop's exception CLASS **and** sanitized message
  (`_note_hop_detail`/`_hop_details_log`, keys redacted, log-only, never shown
  to the client) -- and a roles `exc` failure records the message too, so the
  next burst is diagnosable instead of a bare "ConnectionError".
- **A last-chance pick never gets a request it cannot hold** (`_window_fits`):
  the KNOWN window must hold ~est*1.15 tokens (same bar as
  `_ctx_hop_cannot_serve`; a hard per-request cap like groq's 8K TPM counts, a
  "default"/plain "table" window does not). `_quality_fallback_pick(..., est=)`
  drops non-fitting picks OUTRIGHT (not fail-open) -> no pick when nothing fits,
  so the chain goes straight to `_ctx_overflow_reply`. The roles walk skips a
  non-fitting candidate WITHOUT spending an actor hop, so an all-too-small
  chain ends fast and falls back to the native overflow reply.
- **Actor budget for a big request** (`_role_hop_deadline`): past
  `LONG_CTX_SPEED_TOKENS` (60K) the actor split is capped to
  `_LONG_CTX_ROLE_ATTEMPTS` (2) so each actor gets ~room/2 (~90 s on a 180 s
  stream turn) instead of room/3 (~60 s), and a model MEASURED healthy-but-slow
  at long context (`_long_ctx_speed`/`_tool_ttft` p90) is given at least that,
  capped at `_LONG_CTX_ROLE_FLOOR_SHARE` (0.6) of the room so a 2nd attempt
  still fits. The fastest long-context model already opens the walk
  (`_prefer_fast_long_context` in `_build_chain`).
- **Roles failure handling**: a 401/403 (credential/policy -- zenmux free tier)
  rests the pair for the recent-failure TTL in `_swarm_note_member_status`; an
  "invalid"/"prose"/"junk" actor result (`a tool call the CLI cannot run`)
  `_note_recent_hop_failure`s the pair so `_build_chain` demotes it next turn;
  5xx still throttles as before.
- **Activity display**: `model_req` (the mode, e.g. `coding-swarm`) is kept on
  the row and is NOT overwritten by the resolved actor pick; the dashboard now
  shows `coding-swarm -> nvidia/muse-glimmer-30b` for a mode/tier request
  instead of only the single model that happened to answer (a pinned
  `pid/model` resolves to itself, unchanged).
- **Codex `/agent` "error - 200"**: `_finalizing_body` now tests the
  VALUE-carrying `_STREAM_ERROR_VALUE_RE`, so a bare `"error": null` / `{}`
  envelope (many OpenAI-compatible gateways send one every frame) on a cleanly
  completed stream reads as `ok`/`empty`, not `error` (MEASURED on
  glm/glm-4.7-flash, 816 ms).
- **Blocklist aliases**: `_is_model_blocked_by_user` also checks each
  `_ctx_alias_candidates` identity against the block lists, so blocking
  `gpt-oss-20b` also switches off pollinations `openai-fast` (its catalog
  alias).
- **Honest "switched OFF" note** (`_no_candidates_hint(est, tools)`): only
  counts an off-list model that could ACTUALLY have served THIS request (window
  fits, tool-capable on a tool turn, not low-quality); when none could, it
  states the real constraint ("this request is ~N tokens ... too small ...")
  instead of blaming the Settings switch.

## Task board and the goal behind every task (2026-10-08)

Owner (inspired by Paperclip's goal alignment + task board): "goal behind every
task" across runs and sessions, and a PERSISTENT task board agents pick work
from -- the SAME quality in any terminal CLI and in the Build page. Covered by
`tests/test_taskboard.py` and `tests/test_goal_everywhere.py`.

- **`taskboard.py`** (pure, stdlib only, never imports app/swarm/config; one
  RLock, atomic replace, every read fails open). A GOAL is one durable outcome
  for a project folder (newest OPEN goal wins, matched by abspath+normcase); a
  TASK belongs to a goal with `needs` (other task ids), owned `files`,
  `priority` and a status in `STATUSES` (todo/doing/done/blocked/failed).
  FIXED INTERFACE (agent H codes against it -- do not rename): `Board(path=None,
  clock=)`, `add_goal/goals/close_goal/goal_for`, `add_task/update(appends to
  history)/tasks/next_batch(deps done, priority then age)`, `goal_brief(goal_id
  =None, project_dir=None, max_chars=600) -> "GOAL: ...\nOPEN TASKS: ..."`.
  `default = Board()` is in memory until `configure(path)`; app calls
  `taskboard.configure(state_dir()/"taskboard.json")` at BOOT only.
- **Routes** (token-gated by the global `_local_control_guard` like every
  `/api/*`, no in-body check): GET/POST `/api/goals`, POST
  `/api/goals/<id>/close`, GET/POST `/api/tasks`, POST `/api/tasks/<id>`
  (status/note/owner/run_id). +4 `@app.route`, so README's route count line
  moved 156 -> 160 (`tests/test_readme_claims.py` counts `@app.route(`).
- **The goal brief is the SAME string everywhere** (`goal_brief()` already
  carries "GOAL: ... / OPEN TASKS: ..."), <= 600 chars, flag `goal_brief`
  (default on):
  - **Multi**: `swarm_windows.start/resume(goal_brief=<str|callable>)` stores
    `_Run.goal_brief` (persisted via `row()`/`from_row()`, cap
    `GOAL_BRIEF_CHARS` 700). The planner sees it ahead of the conversation
    context (`start` prepends it to the plan context only, so workers are not
    told twice); every worker's `_agent_prompt` carries a "THE GOAL BEHIND THIS
    WORK (keep every step serving it)" block ahead of the design. "" / no
    callable = the prompt is byte-for-byte as before.
  - **Build brief file**: `agentic_chat.set_goal_brief_source(fn)` (app
    registers `_goal_brief_for_project`); `write_task_brief` adds a "## The
    goal behind this work" section. Unregistered / no goal = the file is
    unchanged.
  - **Terminal CLI opening turn**: `_apply_goal_note(messages)` (called in
    `_upstream_chat` right after the craft brief, skipped for `_no_craft`
    stages) prepends ONE small system note when `_awaiting_new_instruction`
    (opening turn, never a tool-loop continuation), not a compaction request,
    and `_project_dir_from_messages` (reuses `_ENV_BLOCK_RE`/`_ENV_LINE_RE`)
    finds a real folder with an active goal. A dashboard request carries no
    env block, so it is never touched. Token cost: ~the brief (<= 600 chars,
    ~160 tokens) once per opening turn, 0 on continuations.
- **Multi integration** (`_multi_link_tasks` / `_multi_sync_task` /
  `_multi_progress_sync`, ledger `_MULTI_TASKS` run_id -> {phase index: task
  id}): a run of a project with an active goal passes `goal_brief=` to
  `start`/`resume` and links each phase to a board task (one created per phase
  the goal has none for, matched by title). The follow loop moves the task:
  phase RUNNING -> `doing` (owner = helper session), DONE -> `done` (noting
  verified/reviewed), FAILED -> `failed` with the reason; at the end worker
  PROGRESS.md `- [x] Phase N` ticks reconcile tasks to `done` (best effort,
  only promotes). The plan/dry-run, evidence and verdict logic are unchanged.
- **UI**: `#agent-tasks` on the Build page, theme tokens only. `loadTasks()`
  fetches `/api/goals` + `/api/tasks` for the session's `curProjectDir`.
  Since 2026-10-10 (owner: "the goal should appear in the TOP BAR section, not
  after the helpers") it is ONE row directly under the Build toolbar, first in
  `#agent-session` and so above the helper bar, the update banner and the
  Plan/Helpers strip (it used to sit inside the chat column after them):
  "Goal: <text>" (ellipsis; the whole goal in the tooltip and at the top of
  the opened body, for touch and keyboard), then an "N open tasks" toggle
  (`#agent-tasks-toggle`, `aria-expanded`) that opens `#agent-tasks-body` in
  place -- the open tasks with status chips + Done, the add-task form, the goal
  form (its button says "Save goal") and Close goal. No goal: "No goal yet" +
  "Set goal" (`#agent-goal-set`), which opens the body on the goal form. Folded
  by default; open/folded is the browser's own (`localStorage` key
  `flh.goalOpen`, guarded), never server state. 44 px targets on phones, 24
  rendered px on a desktop; the row wraps instead of scrolling sideways.
  Covered by `tests/test_goal_in_the_top_bar.py`.

## Heartbeats and budgets (2026-10-07)

Owner (inspired by Paperclip's heartbeats + budgets): (1) agents wake
themselves at times the owner picks and continue open tasks; (2) every run has
a spend budget and stops cleanly when it is spent. Covered by
`tests/test_heartbeat.py` + `tests/test_run_budgets.py`.

- **`heartbeat.py`** (pure, stdlib only; every side injected). cron-lite
  `parse_when`: `"every N min"` / `"every N hours"` -> `{kind:"every",
  seconds}`; `"daily HH:MM"`; `"weekdays|weekends HH:MM"`; `"mon,wed,fri
  HH:MM"` -> `{kind:"weekly", days, hh, mm}`; anything else None. `due` /
  `next_due` from the last beat (an `every` with no beat is due at once;
  daily/weekly fire once per slot, after its local HH:MM). `in_quiet_hours`
  ("HH:MM-HH:MM", wraps midnight). Schedule records are pure list transforms
  (`normalize_schedule` needs a readable `when` and a goal_id or project_dir;
  `add`/`update`/`remove`). Stored in settings `heartbeats`; runtime state
  (last beat, last skip, beats log) in `heartbeat_state`.
- **`Scheduler`**: ONE daemon thread, `tick(now)` fires every due schedule.
  Boot starts the thread only -- the loop WAITS a tick before its first beat
  (no beat the instant the hub comes up). A beat is SKIPPED (reason recorded in
  `last_skip`, surfaced in status, last_beat_at NOT advanced so it retries next
  tick -- never a tight loop) when, in order: quiet hours, the owner is busy
  (app `_hb_busy`: a live Multi run for the project, or any hub request in
  flight / finished in the last 60s), no RAM room (`_hb_ram_ok`: free -
  `lowres.reserve_gb` >= 0.6 GB), the providers are rate-limited (`_hb_providers_ok`:
  `swarm_windows.concurrency_info` backoff / limited_by 429s), or there are no
  tasks. Never two beats of one schedule at once (an in-flight guard). Else it
  takes `taskboard.default.next_batch(goal, max_tasks)`, starts ONE Multi run
  (app `_hb_start_run`: a per-schedule conversation is the `owner`, so the
  Build page shows it and Continue/Stop work), marks the tasks `doing`
  (`owner="heartbeat:<id>"`, the run id), and records the beat. Kill switch
  `heartbeats_enabled` (default OFF). `taskboard` is imported LAZILY (agent T
  ships it; a missing board reads as "no tasks").
- **Budgets** (`swarm_windows`): `start`/`resume(budget={tokens,seconds,calls},
  spent=fn)`, persisted on the run (`spent` re-attached on resume/boot, like
  the manager). `budget_check` compares the cap with the MEASURED spend --
  tokens/calls from `spent(run_id)` (app `_run_spent` sums the run's worker
  sessions via `worker_session_ids` from the per-build-session ledger
  `_SESSION_SPEND`, fed by `_record_chat_usage` / `_record_sse_usage`), seconds
  from the run clock (`budget_active_seconds`, accumulated across walks).
  `_run_phases_loop`: when the budget is reached it stops STARTING new phases;
  running ones finish their turn (no kill), the rest are marked STOPPED /
  `BUDGET_STOPPED_ERROR`, and `budget_note` ("Budget reached: X of Y tokens --
  N tasks left for next time") rides on `result`/`format_result`/`budget_view`
  and the run header. A resume respects the remaining budget. Heartbeat runs
  ALWAYS carry one (schedule's, else `heartbeat.DEFAULT_BUDGET` = 45 min / 2M
  tokens / 400 calls); a conversation's Multi run uses its own cap
  (`agentic_history.budget`) else `multi_default_budget` (None = unlimited, as
  today), wired via `_multi_budget_kwargs`. A plain /agent Build turn is
  refused cleanly (`_conversation_budget_block`, 429 + message) when its cap is
  reached; terminal CLIs get only the counter.
- **Routes** (control-token gated): `GET/POST /api/heartbeats` (status / flip
  the kill switch / add a schedule), `PUT|DELETE /api/heartbeats/<id>`,
  `GET /api/budgets` (spent vs cap per live run and capped conversation).
  Settings drawer `#heartbeats-group`; the Build run header shows "Budget:
  X / Y". app.py edits are a NEW section (hooks named `BUDGET:` / `HEARTBEAT:`).
  `tests/conftest.py` stubs `_start_heartbeat_scheduler` so no test launches
  the real thread.

## CLI and Build parity (2026-10-08)

The quality machinery Multi/swarm already had, extended to terminal CLIs on
`/v1/*` (opencode chat, codex responses, claude messages) and the Build
single-model turn. Each gap is flag-gated, fails open and is byte-identical old
behaviour with its flag off. Covered by `tests/test_cli_build_parity.py`.

- **Web slop -> verifier problems -> corrector** (flag `turn_slop_check`, under
  the `turn_verifier` kill switch): `_slop_problems_for_proposal` /
  `_web_writes_in_msg` read what a proposal WRITES -- a tool call whose args
  carry `.html/.css/.jsx/.tsx/.vue` content, or HTML code fences in the text --
  and `verify.slop_problems` scores it. Its HIGH findings are folded into
  `_role_verify_and_correct` as problems at severity high, which drives the
  existing ONE corrector call; they beat a verifier ACCEPT and, when nothing
  else would have verified, run no verifier model call. `_max_text_review`
  inherits it (it delegates to `_role_verify_and_correct`). Web output only.
- **Single-model team notes** (flag `tool_turn_specialists_single`, under
  `_team_flag_on`): `_single_turn_team_notes` runs `_team_notes_for_turn` for a
  HARD, fresh-instruction TOOL turn on auto/best (never pinned, never a
  pipeline tier), BEFORE the actor, and injects the brief into the messages
  every hop is built from (`_with_team_notes`) -- so stream and non-stream on
  all three protocols are covered with no buffering. Called in
  `_chat_completions_uncached`, the `/v1/responses` and `/v1/messages` handlers
  right after `_clock.plan_hedge`. Never changes routing or the chain.
- **Observed evidence + receipts for /v1** (flag `v1_observed_evidence`):
  `_v1_observe` (called next to `_bandit_credit` on all three routes) reads the
  history's command tool results (`_tool_call_commands` + `_exit_code_in`),
  classifies with `evidence.from_event`, dedupes per `tool_call_id` (bounded
  LRU `_V1_SEEN_IDS`) and writes a `receipts.write` receipt keyed by
  `ctxwin.conversation_key` for RECOGNISED test/build commands. The hub runs
  nothing.
- **Harvested facts for /v1** (flag `v1_memory_facts`): the same `_v1_observe`
  calls `memory.harvest_facts` into the project scope when the CLI's cwd is
  known (`_v1_project_cwd`: `<cwd>` / working-directory env lines, an existing
  dir that is never the hub's own repo). No model call; deduped; bounded.
- **PROGRESS/todo upkeep re-asked**: `swarm_windows._agent_prompt` re-injects a
  one-line "re-read PROGRESS.md and refresh your `- [ ] Phase N` line" reminder
  on a retry/revision/resume (`agent.revisions`/`run.resumes`/`run.restored`;
  first fresh attempt unchanged); `craft.PLAN_PHASES` tells single sessions to
  update PROGRESS.md after EACH step (ceiling in
  `tests/test_craft_briefs.py::test_worst_case_brief_cost` stays 0.135).

## Children never get the hub's port (2026-10-08)

run.bat/run.sh export `PORT=8787` for the hub, and every agent / subscription
CLI inherited it: an agent's `npm run dev` (vite.config `port: process.env.PORT`)
bound `[::]:8787` beside the hub, and the next restart refused to start (run.bat
saw the port taken). `agentic_chat.strip_hub_port_vars` drops `PORT`, `VITE_PORT`,
`FLASK_RUN_PORT` when they hold the hub's port, in `_agentic_env` and `app._sub_env`;
other values pass, the hub's own environment is untouched, previews still set
their own PORT (`workspace._env_for`). Covered by
`tests/test_children_never_get_the_hub_port.py`.

## Tests

Run with either python (the `.venv` has pytest too):

    python -m pytest tests/ -q
    .venv\Scripts\python.exe -m pytest tests/ -q

The hub itself RUNS from the `.venv`, so a dependency present only in the
system python (PyYAML, tzdata — both missing there until 2026-09-30) passes
the suite while failing live. Run the suite under the `.venv` after touching
imports or `requirements.txt`.

The full suite is green (5985 tests, 2026-10-03); a new failure is a real regression.

## Old tool results are cleared on big turns (2026-10-08)

Covered by `tests/test_old_tool_result_clearing.py`. MEASURED hub.log
2026-10-08 (OpenCode `coding-swarm`): ~85K-token tool turns timed out on free
models (`CHAT-DEADLINE est=85203 ... _HopBudgetExceeded no answer within
106s`; only a handful of models hold 85K and they are slow at that size) and,
on a slow uplink (~20 KB/s measured), an 85K-token body (~340 KB) is ~17 s of
UPLOAD per hop, re-sent on every hop. Most of those tokens are OLD tool outputs.

- **What**: `ctxwin.clear_old_tool_results(messages, keep_recent=8,
  min_chars=1500)` -> `(messages, stats)` replaces the CONTENT of an older tool
  result longer than `min_chars` with ONE line, `[tool output cleared by the hub
  to save context (was ~N chars): <first 160 chars, one line>. Re-run the command
  if you need it.]` (`ctxwin.cleared_stub`). No model call. No message is removed
  or reordered and no id is touched, so every tool call keeps its result by
  construction. Pure; handles all three wire shapes (OpenAI role `tool`,
  Anthropic `tool_result` blocks incl. `is_error`, Responses
  `function_call_output` items); the input is never mutated, a changed message
  is a copy, and when nothing changes the SAME list comes back.
- **Never touched**: the newest `keep_recent` message UNITS (a call + its
  results = one unit; `ctxwin.unit_spans`, which `app._message_units` now
  delegates to, so compaction and clearing agree), the leading system messages,
  the message carrying the latest real instruction, the results of the LAST 3
  failing steps (`ctxwin.looks_failed`: non-zero exit, traceback, failing tests,
  compiler errors, `is_error`; head+tail of the text only, liberal on purpose --
  a false positive only protects one more of three units), any message with an
  image, a CLI's own compaction request, a result already cleared, a request
  under `OLD_RESULT_CLEAR_FROM_TOKENS` (60000 estimated tokens, tools included).
- **Wired once**: `app._clear_old_results_for_hop`, called per hop in
  `_upstream_chat` right after the craft brief / model guide / goal note and
  BEFORE `_compact_to_budget`. Every protocol (chat, responses, messages; stream
  and non-stream; pipelines, roles, hedges) reaches a provider through
  `_clock.dispatch` -> `_dispatch_chat` -> `_upstream_chat` as OpenAI-shaped
  messages, so this is the one code path. Only what is SENT shrinks: handlers
  keep the original messages and estimate, so routing is unchanged.
- **Kill switch**: config flag `old_tool_result_clearing` (default on). Set it
  `false` in `config.json` (or `config.set_flag("old_tool_result_clearing",
  False)`) and every hop goes out byte-for-byte as before. Log: one line per
  request, `[ctx] cleared N old tool results (X -> Y est tokens)`.
- **Reported usage is unchanged** (CLIs compact on that number): a hop that
  cleared notes `_ctx_note_hop(pid, model, <size BEFORE clearing>, <size sent>)`
  whether or not compaction also ran (it used to be noted only when compaction
  ran, so a clear-only hop would have reported the small upstream count). The
  refit path composes with it (tested: a first-pass 400 that teaches
  the window, then the refit).
- **Compaction still sees what was said**: `_compact_to_budget(...,
  originals={id(cleared copy): original})` SIZES the cleared messages but feeds
  the exact facts, the model-written recap and its hashes the originals, as if
  nothing were cleared. `ctxwin._exact_facts` also skips a stub, so a stub is
  never mined as "what the file printed" (the refit path has no originals map).
- **Deterministic / cache-stable**: a stub is a function of that result's text
  alone (no counters, no clock). Clearing is monotonic in time: a result cleared
  at turn T is cleared to the same bytes at every later turn, and everything
  older than turn T's keep window is byte-identical across turns; only the one
  unit that crosses the window each turn changes. Idempotent (a stub is short
  and recognised by its prefix).
- **Measured** (synthetic but realistic 85K history: file reads 3-8K chars, test
  logs, grep output, tiny edits): 83,258 -> 15,107 est tokens (-82%),
  355,546 -> 75,543 bytes (-79%), 65 results cleared. That is the upper end:
  the saving is the share of the request that is OLD tool output (the test
  asserts only >= 35%). On a 20 KB/s uplink the body goes from ~17.8 s to ~3.8 s.
- **Left out on purpose**: routing / window filtering still use the ORIGINAL
  estimate (`est` in the handlers), so a model whose window is under ~85K is
  still skipped for such a turn even though the cleared body would fit it;
  re-sizing `est` needs the usage-reporting contract re-threaded through the
  three handlers. The long-context speed ledger (`_hub_est_tokens`) also still
  records the original size. Batching the clear boundary (a stride) to keep the
  provider cache valid for longer is the HANDOFF "cache-stable prefix" idea.

## Resolver resilience (2026-10-08)

Covered by `tests/test_dns_resilience.py`. What was seen: hub.log 2026-10-08
02:48:27 UTC, `CHAT-503` with `nvidia`, `glm` (api.z.ai) and `zenmux`
(zenmux.ai) all failing with `NameResolutionError` in the same second, so the
whole chain ended in milliseconds and the CLI got a 503. It came one second
after the hub finished uploading a ~2 MB (504K-token) body on a slow uplink; the
cause is NOT established (the resolver may have been starved by that upload, or
it was the local network or the router). A clean 72-lookup test minutes later
had no failure. Only two log lines carry `NameResolutionError` (the per-hop
`details=` logging is new), so no frequency is claimed here or anywhere else.

- **`netresolve.py`** (pure stdlib, Python 3.9+, no dependency, no thread, no
  file, no network of its own; nothing about the machine's DNS/network settings
  is read or changed). `install()` wraps `socket.getaddrinfo` once per process
  (idempotent; `_orig` keeps the original reachable, `uninstall()` puts it
  back only while the wrapper is still the installed one; `stats()`,
  `reset()`). A lookup that succeeds returns at once and is remembered in a
  bounded LRU (256 keys: host lowercased, port, family, type, proto, flags) with
  a wall-clock timestamp. A `socket.gaierror` (any errno, incl. EAI_AGAIN /
  EAI_NONAME and Windows WSA codes) is retried ONCE after 0.25 s; if it still
  fails and the same key succeeded less than 7 days ago (`MAX_STALE`) the last
  known list is returned and `[dns] resolver failed for <host> (<reason>); using
  the last known address from <N>s ago` is logged, at most once per host per 5
  minutes; otherwise the ORIGINAL error is re-raised unchanged. A failure is
  never cached, a name that never resolved never gets a stale answer, a
  successful lookup is never delayed, and no lock is held across the OS call.
  Left alone entirely: IP literals (incl. `%scope` and `127.1`), `localhost` /
  `*.localhost`, bare names without a dot, `host=None`, non-ASCII bytes and
  bind (`AI_PASSIVE`) calls. For HTTPS URLs TLS still verifies the certificate
  against the HOSTNAME of the URL (urllib3 passes `server_hostname` from the
  URL, not from the address), so a stale address can only reach a server that
  holds a valid certificate for that name; a moved address fails at
  connect/TLS as before. netresolve cannot see the URL scheme: a plain-http
  base URL has no certificate check with or without it, and a stale address
  there is only ever an address that name resolved to.
- **Switches**: flag `dns_stale_cache` (`config.get_flag`, default ON), read
  only after a lookup has already failed (a success never touches the config);
  off = plain passthrough (no retry, no stale). `install_at_boot()` is the one
  line app.py runs next to `clientgone.install_urllib3_hook()`; it never
  raises and honours the environment variable `FREE_LLM_HUB_NO_DNS_CACHE=1` and
  the module attribute `BOOT_INSTALL`. `tests/conftest.py` sets `BOOT_INSTALL =
  False` at import (before any test imports app) and a `pytest_runtest_teardown`
  hook wrapper puts the real `socket.getaddrinfo` and netresolve's state back
  after EVERY test (a hook wrapper, because monkeypatch undoes its own patches
  after any fixture finalizer of ours). Dedicated tests call `install()`.
- **A local network failure is nobody's failure** (app.py, section "This
  computer's DNS/network is not a provider outage"). `_is_local_network_error`
  walks `__cause__` / `__context__` / `.reason` / `args` for
  `urllib3.exceptions.NameResolutionError`, `socket.gaierror`, an
  `ENETUNREACH` / `EHOSTUNREACH` / `ENETDOWN` errno, or the texts "Failed to
  resolve", "Temporary failure in name resolution", "Name or service not
  known", "getaddrinfo failed", "nodename nor servname", "No address associated
  with hostname", "Network is unreachable", "No route to host". Resets,
  `RemoteDisconnected`, timeouts, refusals and TLS errors are provider-side and
  are NOT this. `_dispatch_chat` (the entry every CHAIN hop passes through,
  stream and non-stream, hedge legs and `_dispatch_chat_with_deadline`
  included; the other direct `_upstream_chat` callers -- the provider Test
  probe, the canary / probe-all pair probes and `_hub_serves_now` -- are not
  chain hops and were checked to file no provider ledger when the call raises)
  marks the hop on a local failure (`_mark_local_net_failure`: an
  8 s mark per (pid, model) and per pid, host kept) and clears the marks on any
  HTTP answer. The ledgers return early while `_local_net_failed(pid, model)`,
  the same list "Client disconnect stops the work" files nothing for:
  `_record_outcome(False)`, `_note_recent_hop_failure`, `_throttle_failed_hop`,
  `_note_provider_timeout` (by exception), `_note_provider_result(False)`,
  `_note_nonanswer`, `_note_relay_tool_fail`, `_note_tool_turn_outcome(False)`,
  `_note_quality_strike`, `_record_stream_outcome`, `_note_swarm_member_fail`,
  `_team_stats_note(ok=False)`, `_verifier_stats_note(usable=False)` (the roles
  actor judge, the fan-out and the swarm stage all reach them). Without that a
  DNS blip demoted nvidia / glm / zenmux for ~10 minutes after the network was
  back. `_throttle_failed_hop(exc=...)` called directly with a bare
  resolution error and no mark still throttles (`test_hop_breaker_scope.py`);
  in the real flow the mark is what stops it.
- **The walk waits instead of burning the chain** (`_ChainClock.walk`, so all
  three protocol loops and the roles actor walk get it with no change in the
  loops): when 2+ consecutive hops on DIFFERENT hosts (taken from the exception;
  the pid when absent) failed locally, the walk pauses before the next hop, 1 s
  and, the next time, 3 s (`_LOCAL_NET_PAUSES`; 0.5 s slices through
  `_LOCAL_NET_SLEEP`; bounded by `left() - 1` and `_client_gone()`). When the
  chain ends and EVERY hop that ran failed locally on 2+ hosts and a pause step
  is left, it pauses once more and walks the chain ONCE again (never a third
  time: at most 4 s extra per request). A hop that failed locally is not
  charged to its provider in the clock either (no hop count, seconds or relay
  strike). `/v1/responses` keeps its own single 6 s transient-storm retry
  untouched, so there a local-only failure is bounded by (walk + re-walk) x
  (pass + that retry).
- **What the client sees**: `_classify_hop_error` returns `dns` for a local
  error (header `X-Free-LLM-Hub-Last-Error: dns`; every other class is as
  before). When the whole walk ended on local failures on 2+ hosts and no hard
  upstream error exists, `_chain_exhausted_text` (call sites unchanged; it
  reads `g.hub_net_only` set by the clock) says: "All providers failed: this
  computer could not resolve provider hostnames (a DNS/network problem on this
  machine, not a provider outage); check the connection or DNS, then retry
  (<hop classes>)". Still a 503. A new clock clears that flag, so a later pass
  of the same request (the roles walk before its `best` fallback, the
  `/v1/responses` storm retry) never inherits an earlier pass's verdict. The
  Activity row gets `net: "network"` and the
  dashboard shows a small "network" word beside its status. A chain with no
  local failure is byte-identical to before.
- Not done on purpose: nothing was changed in the machine's DNS, no resolver
  list, DoH or hosts-file logic was added, and a single failing host never makes
  the hub claim a machine-wide problem (that needs 2+ hosts).
- Limits worth knowing: the no-filing rule is mark-based (a short process-wide
  mark per pair, not per exception), so a genuine failure of that same pair
  within the 8 s mark is also left unfiled; the roles actor-hop cap (4) is not
  refunded for local failures (the pause and re-walk live in `walk`, the cap
  still applies); `_note_actor_stall`, `_note_broken_stream` and
  `_record_long_ctx_speed` are not gated because those events are not
  resolution failures; with a single failing host the 503 reads as the old
  generic one.

## Oversized conversations (2026-10-08)

Covered by `tests/test_oversized_conversation.py` (hermetic: fake upstream, no
network). Live, hub.log 2026-10-08 UTC, Build page session c39a30c1 (codex,
`coding-max`, `/v1/responses`): the request sat at ~504K estimated tokens
(`[ctx] responses request of ~503553 / 504077 / 504558 tokens overflowed every
hop (largest window tried 262144)`), every message ended as the native
context-length reply (`response.failed` inside an HTTP 200, "error . 200") or as
`RESPONSES-503` after 85-265 s ("error . 503"): relays whose window is only the
"default" guess never raise `_ContextOverflow`, so they failed open, each took
the upload, and `_ctx_overflow_reply` was withheld ("some hop failed for a
reason a wait can fix"). `[spread]` had the session pinned to a 65K model
("pool 1").

- **What was measured (and what was only inferred).** Reproduced with a fake
  upstream on the REAL code path, in the live shape (a registered codex /agent
  session, model `coding-max`, unpatched `declared_window`, a fleet of
  ~250K / 262K / 65K / two 1M models with the 1M ones out):
  `coding-max` is a compound id, NOT a codex catalog slug (the catalog lists
  `auto` + the categories), so `_cli_declared_window("codex", "coding-max",
  agent=True)` takes the 272000 fallback branch, not the slug's
  (`declared_window("auto"/"coding", cli="codex")` = 400000 reach cap and
  `declared_compact_limit` = 300000 are what config.toml carries -- figures of
  the SYNTHETIC fleet used here, shaped after the log, not the live ones;
  codex caps the window at its fallback metadata (the notes above), and where
  it then compacts -- ~90% of 272000, ~245K reported -- is from memory of
  codex's source and UNVERIFIED here). Served turns report faithfully: `usage.input_tokens` = upstream count x the
  hop's compaction ratio x declared/live (272000/262144 = 1.04): with a fake
  upstream counting 0.8 x the sent estimate, 91K / 183K / 251K / 343K / 435K
  for requests of est 109K / 219K / 302K / 412K / 523K -- never below the real
  size, so a served turn that big makes codex compact. A CLI's OWN compaction request at 500K is SERVED (never
  refused, `_ctx_signal` off): `_compact_to_budget` trims it to the hop's window
  (55K sent to the 65K hop, 222K to a 262K hop, 84K to a default-window relay),
  so the summary is lossy (it sees the newest 10-42% of the history plus the
  structural notice / exact facts / recap) but it works. Overflow and 503 turns
  carry NO usage event, so codex's recorded usage stays frozen at its last
  successful turn. NOT reproduced, so the cause of the ~504K is UNPROVEN (the
  real hub.log was off limits; only the task's excerpt was seen, which shows no
  compaction request but is not the whole log): why codex never started a
  compaction during 00:23-02:48. The hub assumed that the `response.failed` /
  `context_length_exceeded` reply makes codex mark its window full and
  auto-compact on the next turn; the excerpt (three messages, +500 tokens each)
  suggests that does not hold in a long session. The only under-reporting
  REPRODUCED is the pipeline tiers below -- the incident session was
  `coding-max` (not a pipeline tier), so that bug does not by itself explain
  504K. If it does not, "Continue" re-sends the
  same oversized turn: the way out is codex's own compaction on a SUCCESSFUL
  turn that reports usage over its limit, or a new conversation. Open idea, needs
  the owner's call: answer such a request with a short successful reply whose
  usage is over codex's limit instead of the refusal.
- **The one under-reporting bug found and fixed: pipeline tiers.** swarm /
  crew* / multi / compound tool turns (`_swarm_tool_turn`, `_swarm_as_responses`,
  `_swarm_as_anthropic`) reported the actor's RAW count: streamed Responses
  `input_tokens = 0` (the replay had no usage frame and no `prompt_est`), JSON
  shapes the compacted payload's count (40000 for a 300K request compacted 6x).
  Codex/opencode/Claude Code on those tiers never saw the context fill.
  `_pipeline_usage(data, est, final)` now runs the same single reporting pass as
  the single-model path: the hop ratio (the actor is `data["model"]`) and live
  window steering, ONCE (`final=True` for replies no translator touches;
  `final=False` for the streamed Responses / Messages replays, whose translator
  steers -- `_reported_prompt_tokens(..., steer=)` / `_ctx_fix_chat_usage(...,
  steer=)`). `_swarm_sse_lines` and the chat `_one_shot` stream add one usage-only
  chunk when the answer has usage (`_swarm_stream_chunks` is unchanged: tests
  index its last chunk).
- **Front door** (`_front_door_overflow`, `_front_door_bound`): a NON-compaction
  request whose estimate exceeds 1.15x (`est * 100 > bound * 115`, integers) the
  LARGEST window any alive candidate could hold gets the protocol's native
  overflow reply at once -- zero routing, chain building, dispatch or upload --
  on chat (only the real `/v1/chat/completions` route: Gemini / Ollama /
  `/v1/completions` share the router but have no such contract), responses and
  messages, stream and non-stream. It runs right after `est` is known and BEFORE
  the pipeline dispatch (the roles path uploads to relays). Candidates are every
  alive (`_alive_models(_cached_catalogs())`), usable-now (`_usable_now`: not
  parked / dead / out >= `_CTX_OVERFLOW_LONG_WAIT`), not user-blocked model,
  tool-capable on a tool request, vision-capable on an image request -- NOT the
  mode-filtered pool (`_apply_mode` fails open). A window the hub KNOWS (learned /
  catalog / inferred / reference via `_model_ctx_info`, so google's 250K and
  groq's caps apply) counts at that window; a "default" guess or a provider-wide
  stand-in row counts as `_REACH_WINDOW_CAP` (400000), never more. Nothing
  alive = no guard; below `_FRONT_DOOR_MIN_EST` (2K, under any usable window)
  the scan is skipped; the bound is cached `_FRONT_DOOR_TTL` (20 s) per (tools,
  images). Futile
  compaction (system prompt + tools >= 75% of the bound) still gets the capacity
  reply. Flags: `context_overflow_signal` (existing semantics) and
  `context_front_door` (this guard only), both default on. The reply is shared
  with the hop-exhaustion path (`_native_overflow_reply`).
- **A pin never outlives the window** (`_route_by_difficulty`): a session pin
  (agentic and plain-chat) whose KNOWN window cannot hold the request
  (`_window_fits`) is dropped (`[spread] ... pin ... dropped`) and the pool
  narrowed to the candidates that can (fail-open: when none can, the pool is
  untouched). A first pick is unchanged -- the "strong model on a trimmed
  context" re-admission still stands.
- **Plain words on the Build page.** A turn whose failure is that reply ends
  `error` code `context_too_long` (status 413) with "This conversation is too
  long for any available model (~N tokens; the largest holds M). Press Continue
  to compact it or start a new conversation." (`agentic_chat.context_too_long` /
  `context_detail`; the sizes come from the per-session ledger `_AGENT_CONTEXT`
  through `set_context_probe`, the CLI's error text is the fallback without
  sizes) and the page adds a Continue button. The Activity row ends status
  `context` (label "context too long", neutral, with the sizes in its
  tooltip) -- set by `_note_context_reply`, read by `_activity_after`, on both
  the front door and the hop-exhaustion paths. Not an outage: `context` does not
  count as an upstream failure in `_AGENT_UPSTREAM`.
- **Left alone on purpose:** compaction requests still go to whichever hop the
  chain opens on (a largest-window-first order would upload ~2 MB to a 1M relay
  instead of 55K to a small one); the estimator's chars/4 over-count is the only
  slack (1.15x); default-window relays still fail open for requests under the
  bound (a request between the known max window and 460K can still reach them);
  a provider-wide "table" stand-in row (not a hard per-request cap) counts as an
  unknown window, i.e. 400K (the spec only names the "default" guess). The early
  `_ctx_begin(..., signal=False)` calls before the pipeline dispatch must stay
  `signal=False`: arming the overflow signal there would make a pipeline actor
  hop raise `_ContextOverflow` instead of being served compacted.

## Multi planning is time-bounded (2026-10-08)

Covered by `tests/test_multi_planner_time_bounds.py`. Flag `planner_time_bounds`
(default ON); off = the old unbounded walk, byte for byte. Adds no route.

**Measured** (Build conversation 2e3525ad, cli=codex, quality "multi"; hub.log is
UTC): a Multi run planned for **526 s** before one helper started. Activity rows
`hub / swarm / plan`: attempt 1 190 s, error 502, `planner attempt 1 was not a
plan (0 chars)`; attempt 2 198 s, a plan; then the dry run's OPTIONAL re-ask 138 s,
error 502, empty again (`Plan check: 8 phases ... planner re-asked once, 0 fixed,
7 warnings`). The goal was 12 073 chars + 4 078 chars of context.

**Why** (an inference that fits the timing and the missing allowance, NOT observed:
hub.log has no per-hop planner line, activity rows live in memory only, so which
models served the hops is not recoverable -- perf-stats `last` rows inside the
window are shared with other sessions' turns and prove nothing; the new `[plan]
hops` line below is the remedy): `_swarm_dispatch` sent the planner's
3000-token budget, strongest-first, to THINKING models (perf-stats non-stream
averages: nvidia kimi-k3 68 s, glm-5.3-flash 48 s, deepseek-v4.1-flash 198 s) with
NO reasoning allowance -- the chain loops give a thinker room
(`_apply_reasoning_effort`) and retry a starved hop (`_starve_retry`); this stage
did neither -- so hidden reasoning ate the budget: empty text, finish "length",
~60 s per hop, three hops per attempt (`_SWARM_STAGE_MAX_HOPS`), each allowed
`_SWARM_HOP_DEADLINE` = 300 s. An empty reply DID already fall through to the next
hop inside one call; what burned the attempt was the 3-hop cap, a non-plan reply
(prose) ending the walk, and nothing remembered between attempts, so attempt 2 and
the re-ask re-walked the same dead head.

**Old worst case**: 2 attempts x 3 hops x 300 s = 1800 s, plus the re-ask's
3 x 300 s = **2700 s (45 min)** before the first helper. The re-ask is polish (the
plan already exists), and in the incident it cost 138 s for nothing.

**Now** (`swarm_windows.py`, one window per planner call, thread-local):
`PLAN_HOP_SECONDS` 90 (a hop), `PLAN_ATTEMPT_SECONDS` 270 (an attempt = three
full-length hops, the old shape at 90 s), `DRY_RUN_SECONDS` 45 (ALL hops of the
re-ask), `DRY_RUN_SKIP_AFTER` 240 (no re-ask when the plan alone took longer;
`report["reask_skipped"]`, said on the "Plan check:" line). `_ask_planner` opens
the window; the planner reads `planning_seconds_left()` -- a window, not a new
argument, because every caller binds `planner` with `_pipeline_bound` and the fakes
take exactly `(system, user)`. `start()` times the plan (`_mono`, a test clock) and
passes `planned_in` to `dry_run`; it logs `[swarm] planning took ...`.
**New worst case: 2 x 270 = 540 s**, and no re-ask on a plan that long (a plan inside
4 min earns a re-ask of <= 45 s). There is deliberately NO total cap that fails a
slow-but-working plan: an attempt that produced a plan is always taken.

**The planner's hops** (`app.py`: `_PlannerStage`, `_planner_order`,
`_swarm_dispatch(plan=)`; only `_swarm_windows_planner` passes it -- workers,
reviewers and synthesis are untouched):
- hop deadline `min(90, window left)`; a hop with < 8 s left is not started;
  up to `_PLANNER_MAX_HOPS` = 5 hops inside the attempt (was 3);
- an EMPTY reply, a cut-off one (finish "length"), prose / JSON with no usable phase
  (`swarm_windows.plan_readable`), a timeout, an error or a non-200 hands over to
  the next model INSIDE the same attempt; the longest non-plan reply is still the
  fallback, so `plan()`'s nudged second attempt works as before;
- an empty finish-"length" hop is learned (`_note_thinking(..., "starved-empty")`)
  and filed (`_record_outcome(False)`); a thinker gets `reasoning_effort` low plus
  the usual allowance (`_apply_reasoning_effort(..., "simple")`: 3000 -> 4024) --
  a plan is JSON, not deep thought. A model that does not think is untouched;
- `_planner_order`: inside the top band (`_AUTO_TOP_BAND` of the best
  `_benchmark_score`, last-resort families never in it, like `_distinct_first`)
  tool-capable, then not slow (`_planner_slow`: measured non-stream duration >=
  60% of the hop deadline, else `_is_slow_model`), then not `_thinks_by_default`,
  then the chain's own order. RE-ORDER ONLY: nothing is dropped, nothing outside
  the band is promoted, the head never leaves the band;
- `_PLANNER_FAILED` (planner-local, 10 min, NOT the global recent-failure ledger,
  which would demote healthy tool-turn models): pairs that failed a planner hop go
  behind every other, so attempt 2 and the re-ask do not re-walk the same head;
- ONE log line per planner call (`[plan] hops, 190s in all: nvidia/... empty: out of
  tokens while thinking (62s) | ... answered (12s)`) and the activity row's chips
  (`planner: <why>` per failed hop, `planner` for the one that answered; the row's
  provider/model is the answering model via `_act_pick`).

**Trade-off**: 90 s per hop can clip a mid-speed model (a 2.5-3K-token plan at
30-40 tok/s is 75-100 s of generation); the walk then hands over to a faster one,
which is the point, but a slow-and-good planner that used to finish at 150 s now
loses its turn. Tune `swarm_windows.PLAN_HOP_SECONDS` if the log shows good plans
dying at 90 s. Also not byte-identical when attempt 1 works: a thinker is sent low
reasoning effort plus 1024 extra tokens, and hop 1 can differ because of the in-band
re-order. A pair that just failed the planner may sit behind out-of-band models --
a deliberate exception to "never leave the band" (it is what `_build_chain` does
for a recent failure), stated in `_planner_order`'s docstring.

**Open finding, NOT changed (declared windows)**: the log line `[ctx] declared windows
changed: all 65536/400000->32000/400000 ...` is `safe/reach`; only the SAFE figure
fell (to `_DECLARED_WINDOW_MIN`, the clamp floor, so the raw value was AT OR BELOW
it). The REACH figure (opencode, codex, kimi, pi, qwen, openclaw, hermes) stayed
400000 and no CLI file was rewritten (no `declared windows resynced` line); the
safe figure reaches only Claude Code, aider and `declared_window(None)`. It is the
minimum of the 25th percentile over ROWS (relay copies and 8K groq rows included)
and the window of the 3rd non-relay provider. Two mechanisms produce that exact
log line and the fleet snapshot is not logged, so they cannot be told apart: (a)
other providers' rows leave the alive set, the small rows' share crosses 25% and
the quantile lands in the small-window cluster (a sandboxed probe with a plausible
fleet reproduces the 65536 <-> 32000 ladder this way; the likelier of the two); (b)
a <= 32K provider becomes the 3rd-provider cap. Recommended follow-up (not done):
a log-only line with the percentile, the row count and the top-3 provider windows
on every change. `_stable_declared_window_for`
applies a decrease at once and a raise after 45 min, so a one-tick dip pins the safe
figure for 3 resync ticks = 90 min (the log shows three such cycles on 10-07,
06:00->07:30, 18:52->20:22 and 21:52->23:22, each exactly 90 min). Owner decision
needed before touching it (a decrease at once is the documented 503 protection);
the candidates are (1) leave rows whose window is under `_DECLARED_WINDOW_MIN` out
of the quantile -- they can never take a CLI turn -- and (2) keep a provider counted
for the 3rd-provider cap for ~45 min after it last had an alive row.


## Fewer wasted hops (2026-10-08)

Covered by `tests/test_stop_wasted_hops.py`. Owner: swarm modes "race to answer
first and waste tokens; they should collaborate, and a failed model should hand
over to the NEXT one". MEASURED (turn-roles.jsonl, 24 h, 514 swarm-mode tool
turns): there is NO race (`tool_turn_race` off, stall backup on 6% of turns),
but the turns cost 1235 upstream calls (2.40 per turn) and **413 (33%) were
failed first hops**: `uncloseai/turboderp/Qwen3.8-27B-exl3` HTTP 400 x173,
`groq/qwen/qwen3.8-27b` RequestException x49, `kilocode/dots-3-note-preview`
"200 with an empty message" x20, nvidia glm/kimi/deepseek "no answer in time"
x42; 86 turns (17%) ended with no server.

- **Why one weak pair kept opening chains** (`[spread] ... pool 1, held elsewhere
  N`; 185 of 1071 logged picks, 148 of them with >= 1 model held by a sibling --
  REPRODUCED with a fake fleet; the 37 with "held elsewhere 0" are INFERRED, see
  below): `_spread_pool` and `_rotate_within_run` measured "comparable" on the RAW
  benchmark score (a 10-point window) while `_auto_top_band` -- which runs AFTER
  them -- applies the learned reliability penalty. With the strong models held by
  sibling sessions (or just stalled: a stall is a `_recent_hop_failure` and drops
  a model from the primary pick, a HTTP 400 is not), the Qwen at 134 (reliability
  0.026, penalty 8.5) was the only unheld model in the window, so the next step
  had a pool of one to "choose" from. Now `_spread_pool` / `_rotate_within_run`
  measure on `_learned_score` (score minus reliability and answer-quality
  penalties), use a 6-point window (`_SPREAD_MAX_DROP`, the owner's widest
  rotation band), and never move a session ONTO a pair that is measured to fail
  (`_chain_reliability_band` 2), sick on tool turns or resting
  (`_spread_target_ok`). When nobody eligible is free the sessions SHARE the best
  (the pool minus measured-to-fail pairs; the full pool when that is all there
  is). Owner rule intact: parallel helpers still get different models WHEN they
  are in the band. The fresh pick also drops a model whose KNOWN window cannot
  hold the request even after the hub's allowed trim (`_roomy` in
  `_route_by_difficulty`, same bar as the overflow signal), fail-open. INFERRED
  (not reproduced before the fix): the "held elsewhere 0" picks and the repeated
  re-picks of one session after "pin dropped" -- the strong models stalled (a
  stall leaves the primary pick through `_recent_hop_failure`; a HTTP 400 is not
  such a kind) so the fast-failing pair was what was left; the streak rest and the
  system-message fix remove that cause, but no test failed on it before.
- **Why the HTTP 400 (all 7 logged bodies)**: `"System message must be at the
  beginning."` -- the Qwen chat template raises on ANY system message that is not
  the very first one, including a second leading one, and the hub puts its own
  (craft brief, model guide, goal note, team notes, compaction notice, exact
  facts) beside the client's. Not tools, `max_tokens` or private keys. Fix in
  `_upstream_chat`: the 400 is matched by `_SYSTEM_ORDER_ERR_RE`, the SAME hop is
  retried at once with every system message merged at index 0 in order
  (`_merge_system_messages`; text-only, otherwise untouched) and the pair is
  remembered (`_SYSTEM_FIRST`, 7 days, in memory) so later calls are merged before
  they go out. The walk never sees the 400, so nothing is spent or filed.
- **Failure streaks rest the pair** (extends the empty-200 ledger, no new walk):
  `_note_pair_failure(pid, model, cls)` files `http4xx` (any 4xx except 402/413/
  429), `exc` (RequestException) and `deadline` (no answer in time, via
  `_note_recent_hop_failure`) next to the old `empty`. 3 of ONE class inside 15
  min with no success rest the pair for tool turns: 30 min, doubling per repeat
  (a failure after a rest ran out with no success since is a repeat), cap 6 h
  (`_pair_rest`). One delivery (`_record_outcome(ok)`) clears events AND level.
  Exempt: 429/quota/billing/413, a client that left, a local-network failure
  (`_local_net_failed`), an explicit `provider/model` request (its chain is seeded
  before any of this). Resting rides `_tool_turn_sick` (primary pick, chain sick
  group, quality fallback) plus `_empty_resting` (roles walk tail),
  `_build_chain`'s primary seed and the verifier/specialist pool: always
  fail-open, the pair stays reachable as the LAST resort. The chosen
  ORCHESTRATOR (the user's pick) keeps the old short empties-only rest -- a
  skipped pair could never earn the success that clears an hours-long one. `_empty_200` entries
  may now be `(epoch, class)`; a bare epoch is the legacy "empty" event.
- **Known-too-small windows are not walked ahead of a pair that fits**:
  `_ChainClock._roomy_first` (every tool walk) moves a hop that would only raise
  `_ContextOverflow` behind the others -- reordered, never dropped, so the native
  overflow reply still sees them; the user-named head stays first. The roles walk
  and `_quality_fallback_pick` already skipped such a hop without a call.
- **Verifier pool**: a pair with >= 6 verifier runs of which under 20% gave a
  usable verdict leaves the pool while another candidate remains
  (`_verifier_unusable`, fresh look after 6 h). The verifier GATE is unchanged.
- **Cost visible**: every roles row now carries `actor_calls` and
  `wasted_calls` (actor-hop calls that served nothing: failed first hops, a
  backup that lost, every call of a turn nobody answered; counted at the leg
  starts, not from `failed`, which also lists hops skipped for a window).
  `scripts/role_eval.py` prints `summary: N calls/turn, P% of calls wasted` and
  derives the figure for old rows from `failed`, so before/after compares on the
  same log (24 h before: 2.40 calls/turn, ~32% wasted).

## Graceful updates: drain, restart, continue (2026-10-08)

Owner: "even after auto update the hub should let working jobs WAIT RETRYING
until the update finishes and they continue working after restarting with the
new system, if there are updates." Covered by `tests/test_graceful_update.py`;
the pure half is `graceful_update.py` (stdlib only, fake-clock testable).
Flag `graceful_update` (default on; off = the old behaviour exactly: the
snapshot wait capped at `_DEFER_RESTART_MAX`, no refusals, no marker, no Stop
check). Settings `update_drain_max_seconds` (default 600, clamped 1..86400)
and `resume_after_update` (default true).

- **What was wrong.** `_still_running` counted EVERY `/v1` request in flight
  next to the snapshot, so a hub that kept receiving work never reached zero
  and the restart waited up to the 4 h cap; and the jobs a restart finally cut
  continued only when that conversation's `auto_resume` box was ticked.
- **Drain** (`_UPDATE_DRAIN`, `_begin_update_drain`, called by
  `_reexec_when_idle`, i.e. only when an update is pulled AND something is
  busy; an idle hub restarts at once and never drains): NEW work gets 503 +
  `Retry-After: N` (N = what is left of the drain, clamped 5..30) + `x-should-retry:
  true`, message "The hub is updating to <short hash>; retry in N s." (a plain
  restart: "The hub is restarting; retry in N s."), in the caller's own shape
  (`_runtime_error(message, retry_after=)`): OpenAI `server_error` for
  `/v1/chat/completions`, `/v1/responses`, `/v1/completions`, embeddings and
  images; Anthropic `api_error` for `/v1/messages` (NOT `overloaded_error`:
  Claude Code counts those toward "Repeated 529 Overloaded errors" for
  opus/fable/mythos ids, and retries any 5xx anyway); Gemini `UNAVAILABLE`
  envelope; Ollama `{"error": ...}`; JSON-RPC error for the MCP start tools
  (`crew_run`, `crew_start`, `swarm_windows_start`); and for the dashboard doors
  `POST /api/agent/sessions/<sid>/message[/stream]` (`code: hub_updating`),
  `POST /api/swarm-windows`, `POST /api/enhance-prompt`. Stop's own drain reply
  is unchanged byte for byte. Heartbeats start nothing (`_hb_busy`), `/ready`
  says `draining`.
- **Who is served during the drain.** A request that belongs to running work:
  its `/build/<sid>` session (`_build_sid`) was busy when the drain began, or
  is a worker of a run that was running then (`swarm_windows.worker_info`, so a
  worker started later still counts). That is the running turn asking for its
  next model call; refusing it would stop the work the drain waits for. Everything
  else (terminal CLIs, new sessions) waits and retries. Not refused: GETs (model
  lists, dashboard pages, every read-only `/api` GET), `count_tokens`, MCP reads
  and Stop. Refused requests are never counted in-flight, so the counter only
  falls and the wait cannot starve.
- **Restart.** The moment `_still_running` reaches 0 (polled every second),
  or when the drain's deadline passes (`update_drain_max_seconds`; the resume
  marker makes that cut safe) -> `_reexec_soon`. A sticky Stop wins: with the
  intentional-stop flag (or runtime `desired: stopped`) `_reexec_soon` cancels
  the restart, ends the drain and clears `updating` (a stopped hub is never
  brought back by an update; the boot of a user relaunch still clears the flag).
  The re-exec is `_do_reexec()`; `tests/conftest.py` makes it a tripwire so no
  test can replace the process.
- **Resume marker** `state_dir()/update-resume.json` (written atomically,
  temp file + fsync + `os.replace`, in `_reexec_soon` just before `_do_reexec`):
  `{v, reason, from, to, written_at, state_dir, sessions: [conversation ids
  with a turn running], runs: [{run_id, owner}]}`. Left out of it: Multi
  worker sessions (their run carries them), a conversation that owns a listed
  run, anything with Stop pending (`last_interrupted` / `stop_pending`), runs
  that are STOPPED, and conversations deleted from history. Nothing running =
  no marker.
- **Boot** (after `memory.recover_inflight`, `swarm_windows.resume_interrupted`,
  `_auto_continue_turns`, `_file_unresumed_runs`, which run exactly as before):
  `_update_resume_plan()` reads a FRESH marker once (<= 15 min old, same state
  dir, well-formed, has work; a stale/corrupt/foreign one is ignored and
  removed). `_multi_should_auto_resume` then also accepts a listed run (by id
  or owner) and `_auto_continue_turns(back, plan)` continues a listed turn
  with `_CONTINUE_TEXT`, both regardless of the per-conversation `auto_resume`
  box (that box stays the owner's choice for crashes and manual stops).
  Deleted conversations and stopped runs are skipped. One notice line per
  continued conversation: "Continued automatically after the update to
  <short hash>." (a plain restart: "...after the restart."), via
  `agentic_chat.live_notice` for a turn and a leading `notice` event in the
  run's live feed. Each consumer calls `plan.finish("turns"|"runs")`; the file
  is deleted when both ran. `_auto_continue_turns(back, plan, parallel=True)`
  gives every continued conversation its own thread (a continued turn is
  drained to its end, so one long turn must not hold the next one back) and the
  turns half is filed once they are dispatched, not when the turns end.
- **Same path everywhere.** The periodic auto-update and the dashboard's
  update button (`POST /api/auto-update {check:true}`) both end in
  `_finish_update_apply` -> `_reexec_when_idle` / `_reexec_soon`. No restart
  route existed, so `POST /api/hub/restart {resume?: true, drain?: true}`
  (control token + dashboard header like every POST) was added: same drain,
  marker and re-exec, 202 `{restarting, waiting_for, resume, draining,
  max_wait_seconds}`; `resume:false` skips the marker, `drain:false` restarts
  at once (the marker still protects the work); 409 for a stopped hub or when
  the flag is off; a second call answers `already`. `GET /api/runtime` and
  `GET /api/auto-update` carry `updating` / `draining` ({to, retry_after,
  deadline_in, busy, ...}) for the page. README route count +1 (169).
- **Page.** One line, `role="status" aria-live="polite"`, in the dashboard
  header area (`#update-banner`) and on the Build page (`#agent-update-banner`):
  "The hub is updating. Running jobs finish or wait, then continue by
  themselves." (theme tokens only). `refreshUpdateStatus()` follows
  `/api/runtime` (15 s resync, every 5 s while updating). The Running popup says
  "working now · waiting for update", Activity rows still in progress say
  "in progress · waiting for update", and a refused send shows the hub's own
  sentence instead of "stream failed". Static tests only.
- **What each CLI does with 503 + Retry-After** (read from the GLOBAL installs
  on this machine, 2026-10-08 -- the hub-isolated builds under
  `~/.free-llm-hub/isolated-clis` (Claude Code 2.1.220, opencode 1.18.11) were
  NOT verified; the Claude Code 2.1.220 build has the same
  `tengu_api_retry_after_too_long` event but its cap value was not read; the
  value stays <= 30 s because
  Claude Code gives up on anything over 60 s and the OpenAI/Anthropic Python
  SDKs ignore anything over 60 s):
  - Claude Code 2.1.293: retries any status >= 500 (not with `x-should-retry:
    false`); Retry-After in whole seconds only, delay = max(RA, backoff), over
    60 s it stops at once; 10 retries (`CLAUDE_CODE_MAX_RETRIES`), backoff
    500 ms x2 capped 32 s; ~5 min tolerated at RA 30. The SDK itself has
    `maxRetries: 0`.
  - opencode 1.18.35: retries status >= 500 and messages matching
    `overloaded|service unavailable|server_error|503...`; honors
    `retry-after-ms`, then `retry-after` seconds, then HTTP-date, NO cap; 5
    retries (~2.5 min at RA 30); the AI SDK layer is `maxRetries: 0`.
  - kimi-code 0.39.1: retry list includes 503/529; Retry-After integer seconds
    only, no cap, replaces the backoff; 10 attempts per step (env
    `KIMI_LOOP_MAX_ATTEMPTS_PER_STEP`) ~5 min at RA 30. Legacy kimi-cli 1.6:
    retries 429/500/502/503 3 times at the step layer, its bundled SDKs add 2
    more and honor Retry-After only when 0 < x <= 60.
  - codex 0.154.0 (Rust binary): UNVERIFIED for HTTP 503. No Retry-After read
    for HTTP errors was found (only a "try again in Ns" regex on the message
    text); the log shows `stream_max_retries` 5 with backoff doubling from
    ~200 ms (~6 s in total). Expect codex to surface an update that outlasts a
    few seconds as an error; the one knob is `request_max_retries` /
    `stream_max_retries` in its provider config, which the hub does not write
    and this change deliberately does not touch.
  - aider, qwen-code, pi, hermes, openclaw, gemini-cli: not installed (or no
    runnable source) here -> no evidence, nothing claimed.
- **Left open.** codex's short retry budget (above); the drain keeps running
  turns of a Multi run going and still starting that run's later phases until
  the cap (it does not pause phase starts); a CLI that gave up before the hub
  came back is a CLI-side limit, not something the hub can resume.

## Publishing from the CLIs (2026-10-08)

Owner: when an agent has FINISHED building a web app and gives the user the
LOCAL url, it must ASK whether to also publish it online through a free
Cloudflare tunnel, and publish only on an explicit yes. Same behaviour in the
terminal CLIs (opencode/codex/claude via `/v1`) and in `/agent` Build sessions.
The engine (`publish.py`, tunnels + clock + the Build page's Publish panel/badge)
is a separate module; this section is the agent-facing workflow. Covered by
`tests/test_publish_cli_flow.py` (against a FAKE `publish` module; no tunnel is
started and nothing contacts Cloudflare in tests).

- **Workflow**: (1) the agent gives the local url and ASKS once: "Publish it
  online through a free Cloudflare tunnel (temporary public link)? yes/no" ->
  (2) nothing happens until the user says yes -> (3) the agent calls the hub MCP
  tool `publish_start {port, project_dir}` -> (4) it tells the user the public url
  and when it expires (`expires_in` / `expires_at` in the result, plus a `note`
  saying so) -> (5) "a new link" = `publish_renew {id}` (id from `publish_start` /
  `publish_status`), `publish_stop {id}` takes it down -> (6) expiry closes the
  tunnel (the engine's clock; nothing for the agent to do). A tunnel an agent
  started shows in the Build page's badge by itself (same registry, `source`
  "agent"); no UI code lives here.
- **The brief line** `craft.PUBLISH_ASK` (236 chars, ~59 tokens) ships inside
  `craft.system_message(text, tools=True, session_id=None)` ONLY for a web/UI
  deliverable (`craft.is_web_ui`) on a tool-carrying request, right after the
  domain briefs and BEFORE the loop (PLAN/ACT/VERIFY stay last: their "every
  brief above" back-references and `test_craft_briefs`). That one function feeds
  both the `/v1` opening turn (`_apply_craft_brief`, so a following "yes" turn
  carries it too via the project-opening-instruction fallback) and the Build
  brief file (`agentic_chat.write_task_brief` passes `session_id`). Everything
  else is byte-identical to before. It is deliberately minimal (the saas landing
  page had 232 chars of room under the 0.135 ceiling, which did NOT move; one
  sentence of IMAGES was tightened to fund the rest). What the line leaves out
  lives where the model meets it: the tool descriptions and each result's `note`.
  If the hub tools are not in the CLI the line says to use the Build page's
  Publish button (a CLI needs the hub MCP entry: Hub controls -> MCP servers -> "Enable hub
  crews in this CLI"; tools/list is dynamic, nothing is enumerated in the entry).
- **MCP tools** (`hub_mcp.py`, a third slot `_PUBLISH` wired by `init(publish=)`
  next to `_SWARM`; glue in app.py's `PUBLISH-CLI:` section, `_publish_cli_*`):
  `publish_start {port, project_dir?, ttl_minutes?}`, `publish_status
  {project_dir?}`, `publish_stop {id}`, `publish_renew {id, ttl_minutes?}`. The
  start/renew descriptions carry the consent wording ("CALL ONLY AFTER THE USER
  SAID YES ... Anyone with the link can open the app, and the link expires").
  Bad arguments are JSON-RPC -32602; every refusal is tool text with `isError`
  and `{error, code}`: the engine's `PublishError.code` (no_cloudflared,
  no_preview, forbidden_port, not_http, too_many, bad_ttl, install_failed,
  not_found, already_published, disabled) passes through (`no_cloudflared`
  says the user can install it from the Build page's Publish panel;
  `already_published` names the running tunnel), plus the glue's own `no_project`
  (project_dir missing, relative or not a folder: the hub guesses it from
  `workspace.running()` by port first) and `failed`. A tunnel that is still
  "starting" is polled (`_PUBLISH_WAIT_SECONDS`, 20 s) so the agent gets the link
  in the same call; never a link before it is live. `source="agent"` is always
  passed to the engine.
- **Safety**: consent lives in the brief AND in every tool description; the link
  lifetime is the engine's TTL; every agent-started tunnel is logged in hub.log
  as `[publish] agent started a tunnel for <project name> port <n>` WITHOUT the
  url (a refusal logs only its code); the setting flag `agent_publish` (default
  on, `config.get_flag`) is the kill switch: off, the four tools answer
  `{error, code: "disabled"}` and the brief line is not injected (the engine's
  own `publish_enabled` flag passes through as its `disabled` error). A Multi
  helper session never gets the line (`swarm_windows.worker_info`, via
  `craft.set_publish_source(_publish_brief_allowed)`): it has no user to ask. A
  hub without `publish.py` answers `disabled` too.
- **Which port an agent may publish** (security fix 2026-10-08; the server
  cannot see the chat, so "only after the user said yes" is a request to the
  model, not a control). `publish_start` calls
  `_publish_port_is_project_server(folder, port)` BEFORE the engine and refuses
  with `{error: "Only a server running from the project folder <name> can be
  published. Start the app from that folder, then ask again.", code:
  "not_project_server"}` (hub.log: `[publish] agent start refused
  (not_project_server) for <project> port <n>`, no url; the helper is
  `_publish_cli_fail`, never the HTTP routes' `_publish_fail`). Allowed ONLY
  when (a) the hub's OWN preview of that folder runs on that port
  (`workspace.running()`, `state` running, not `external`: an ADOPTED preview
  does not count, because `workspace.adopt` takes the agent's printed url and
  fails open when nothing contradicts it), or (b) EVERY process listening on
  that port has its working directory inside the folder, or a parent up to three
  levels up does (npm -> node). Compared by whole path components on
  `normcase(realpath(..))` (`proj` does not contain `proj-evil`). Fails closed:
  unreadable working directory, no listener, a listener psutil cannot name, psutil
  missing, any error = refused. Listeners count when bound to loopback OR a
  wildcard (`0.0.0.0`, `::`; Node's `listen(port)` binds `::`); all of them must
  pass because a specific bind can beat a wildcard one. Where the system-wide
  socket table needs root (macOS) each process is asked about its own sockets. A
  folder that is a filesystem root, the user's home or anything above it is
  refused outright (the folder is the agent's own argument). `publish_renew`,
  `publish_status`, `publish_stop` are unchanged, and the dashboard's Publish
  button (`/api/publish/start`, `confirm: true`) is a human click and does not
  pass through this rule. **Strict mode**: setting `agent_publish_requires_approval`
  (`config.get_flag`, default OFF; an unreadable config counts as ON): `publish_start`
  starts nothing, records the request in memory (one per project + port, kept 10
  minutes, asking again does not restart the clock; at most 20) and answers
  `{pending: true, note: "Waiting for the user to approve this in the Build
  page's Publish panel."}`; only a request that passed the port rule is recorded.
  Approval is the user pressing Publish for that project and port:
  `GET /api/publish` lists `pending_agent_requests` (`project_dir`, `port`,
  `requested_at`; honours `?project_dir=`), and an entry is dropped when its
  project + port has a live tunnel or when `/api/publish/start` starts one. A
  second `publish_start` for a port that is already live returns that link.
  Covered by `tests/test_publish_agent_port_rules.py` (fake psutil, no sockets).
  **Residual risk, in plain words**: after the user's yes, ANY process that runs
  from that project folder can be published (a dev server, an admin page the
  project itself ships); and an agent can pass any folder it likes, so a
  prompt-injected agent can still publish the app it was asked to build, or a
  service that happens to run from some other non-broad folder it names. It can
  no longer publish an arbitrary local service by port alone (a database admin
  page on 8080 that runs from its own folder is refused while the agent names the
  project's folder). Strict mode closes the rest: nothing goes online without the
  user's own click.

**publish_renew has the same gates (2026-10-08, security review of the push).** An agent may renew
only a tunnel that is still `live`/`starting` (`not_live` otherwise: the user's timer is the limit; after an
expiry the agent asks the user and calls `publish_start`), the app must still be a server running from the
project folder (`not_project_server`), strict mode (`agent_publish_requires_approval`) holds a renew for the
user's click like a start, and at most `_PUBLISH_AGENT_RENEWS_PER_DAY` (3) agent renewals per app per rolling
day (`too_many_renewals`: ask the user to renew from the Publish panel; the user's own panel click is never
limited). Covered by `tests/test_publish_agent_port_rules.py`.

## Publish button in the Build page (2026-10-08)

Owner request: put the running preview on the internet from the Build page,
through a FREE Cloudflare quick tunnel, with a countdown until the link
closes. This is the page's half only; the backend (`/api/publish*`) is a
separate piece. Covered by `tests/test_publish_ui.py` (static checks, the pure
helpers and the whole state machine run under node against a fake DOM, and
both-theme contrast measured from the template's own tokens).

- **Contract the page codes against** (and the ONLY routes it calls, through
  the dashboard's `api()` helper, which adds the control token and the
  `X-Free-LLM-Hub: dashboard` header on POSTs): `GET /api/publish?project_dir=`
  -> `{server_time, cloudflared:{available, path, version, platform,
  installable, installing, install_error}, tunnels:[{id, project_dir, port,
  url, state: starting|live|expired|stopped|failed, error, source, started_at,
  expires_at, ttl_seconds, remaining_seconds}], limits:{default_ttl_minutes,
  ttl_choices, max_tunnels}}`; `POST /api/publish/start {project_dir, port?,
  ttl_minutes?, confirm:true}`; `/stop {id}`; `/renew {id, ttl_minutes?}`
  (new URL, fresh timer); `/install {confirm:true}`. Errors are 4xx
  `{error, code}`; all ten codes (`no_cloudflared`, `no_preview`,
  `forbidden_port`, `not_http`, `too_many`, `bad_ttl`, `install_failed`,
  `not_found`, `already_published`, `disabled`) have plain-English text in
  `PUB_ERRORS`; an unknown code shows the server's message. A 404 with no code
  (backend missing) or `disabled` turns the button off with a plain reason.
- **Where**: `#preview-publish` sits in `#preview-bar` after Run / Stop. It
  uses `aria-disabled` (not `disabled`) so it stays focusable and
  `aria-describedby="preview-publish-why"` says "Start the preview first"; it
  stays usable while a tunnel exists for the folder even if the preview
  stopped, otherwise a live link could not be stopped. While a link is live
  the button itself reads "Published · 42:10" (the ticking time is
  `aria-hidden`, so the button's name does not change every second); under
  5:00 it reads "Closing soon · 4:12" (words, not only colour).
- **A native modal dialog** (2026-10-10, owner: "open in a POPUP, responsive,
  clean CSS -- it looks tight in that place"; it was an in-flow panel under the
  tab row). `<dialog id="publish-dialog">`, opened with `showModal()` from the
  button in every state (Publish / Starting / Published / Closing soon / Link
  expired): the top layer is never clipped by the preview column, and Esc, the
  focus trap and the inert page are the browser's own. The script moves it
  under `<body>` once: `showModal()` on a dialog with a hidden ancestor leaves
  an invisible modal over an inert page. Header "Publish online" (`h2`,
  `tabindex=-1`, takes focus on open; `aria-labelledby` it,
  `aria-describedby` the warning) + an SVG X; a body that scrolls; a footer
  with one row per state, the primary action last (right). Esc first backs out
  of the "new link?" question (`cancel` prevented), then closes; a press that
  starts AND ends on the backdrop closes (a drag out of the link text does
  not); the X, Esc and the backdrop all hand focus back to the button. A
  control that disappears while focused (Publish once the tunnel starts, Stop,
  the question row) moves focus to the title, never out of the dialog. The
  Files tab closes it (`publish.paneVisible(false)`, no focus return to the
  hidden button). Always visible inside: the warning "Anyone with the
  link can open this app. Don't publish apps that show private data. The link
  closes by itself when the timer reaches 0:00."; a TTL `<select>` from
  `limits.ttl_choices` (default preselected); the required tick box "I
  understand anyone with the link can open this app" which keeps Publish
  closed (and is cleared after each publication); when `cloudflared.available`
  is false an explicit "Install cloudflared" button (says it downloads the
  official release and verifies its checksum; progress from
  `installing` / `install_error`) plus the manual command for the platform
  (`darwin` is tested before `win`, since "darwin" contains "win").
- **States**: `starting` -> "Starting tunnel..." and a 1.5 s poll that stops as
  soon as nothing is settling, with "Stop publishing" in the footer (it also
  cancels a tunnel that never comes up); `live` -> https link (`target=_blank
  rel="noopener noreferrer"`), countdown, footer "Stop publishing" (left) /
  "New link" / "Copy link" (primary, right); New link asks "Make
  a new link? The old link stops working right away." first; focus starts on
  the safe "Keep this link"; the question replaces the action row; renew keeps
  the previous length; `expired` ->
  "Link expired" + "Generate new link"; `failed` -> the error in words + "Try
  again" (back to the form, which asks for the tick again; the dismissed
  record is remembered locally because the contract has no delete); `stopped`
  hides. The status for a folder is fetched on `preview.attach` (page load and
  project switch); `preview.detach` clears every timer; a late answer for a
  project the user already left is dropped (a sequence counter, no path
  comparison, so Windows path spellings cannot matter).
- **Countdown**: on each status answer the page stores `remaining_seconds` (else
  `expires_at - server_time`, else `started_at + ttl_seconds - server_time`:
  only server-clock numbers are compared with each other) and anchors it to
  `performance.now()` taken BEFORE the request (never later than the server's
  answer, so the clock can only run slightly early). Every tick derives from
  the anchor, so a throttled background tab cannot drift; the display is
  rounded UP, so it reads 0:00 exactly when the link closes. `m:ss`, `h:mm:ss`
  from one hour. `performance.now()` can stand still while a laptop sleeps, so
  the page re-reads the status when the tab becomes visible again and every
  30 s while live; at 0:00 it asks the server once, but shows "Link expired"
  at once.
- **Screen readers**: the countdown is `role="timer" aria-live="off"`. Two
  polite status regions: `#publish-announce` outside the dialog (and outside
  the bar the Files tab hides) while it is closed, `#publish-say` inside it
  while it is open -- everything behind a modal is inert, live regions
  included. They announce only: published, 5:00 left ("less than 5 minutes"),
  1:00 left, expired, failed, stopped, copied. A page opened at 0:40 says the
  one-minute line and skips the stale five-minute one. The badge on the
  button keeps ticking with the dialog closed.
- **Safety in the DOM**: the markup is written once and the script only toggles
  `hidden` and sets `textContent` (so the select, the tick box and focus
  survive the poll and the tick); backend strings never go through
  `innerHTML`; a link is only ever an `https://` URL with no spaces or quotes
  (`pubSafeUrl`; anything else is never made a link); the URL is never written
  to `localStorage` / `sessionStorage` / cookies.
- **Look**: theme tokens only (no raw colours in the `publish-css` block),
  existing `.btn` / `.btn.primary` / `.btn.ghost` / `.btn.danger`, inline SVG
  icons, `--scrim` backdrop. Desktop: a centred window
  `min(560px, 100% - 2 x 16 px)`, `max-height:85%`, the body scrolls; phones
  (<= 640 px): a full-width bottom sheet (`max-height:90%`, top corners
  rounded, footer padded by `env(safe-area-inset-bottom)`, buttons share the
  row, the select at 16 rendered px so iOS does not zoom). The root is zoomed
  (`html{zoom:.8}`) and viewport units resolve unzoomed and then shrink, so
  screen-relative sizes are PERCENTAGES of the dialog's fixed containing block,
  never vw/vh/dvh; `--pub-tap` / `--pub-gutter` are 44 / 16 RENDERED px. The
  dialog's `display:flex` is on `[open]` only (on the bare class it would beat
  the browser's `dialog:not([open]){display:none}`). The bar button grows to
  44 on `(pointer:coarse), (max-width:640px)`. URL and command text wrap
  (`overflow-wrap:anywhere`). Motion: a 180 ms fade + 12 px rise on open, none
  on close, none at all under `prefers-reduced-motion` (which also drops the
  button transitions and press scale). The light theme's dashboard-wide
  `.btn.primary` (white on `--accent-dim`) is 3.3:1, so inside the dialog it
  takes `--accent-strong` (5.0:1; hover `--ok-text`, 7.1:1). Contrast pairs
  (dialog text, hints, install card, warning, countdown, "closing soon", link,
  command, error, Stop publishing, buttons, select, live badge) are measured
  >= 4.5:1 in both themes by the test. Verified with a static Playwright
  harness (no hub): every state open and modal, no horizontal overflow at
  1280x800 or 390x844, every target 44 rendered px.
- Not decided here, backend side: whether a tunnel is closed when its preview
  stops (the page shows whatever the status says), and whether `renew` accepts
  a `failed` record (the page uses start for that, never renew).

## Newest and biggest first, inside a family (2026-10-08)

Covered by `tests/test_model_version_ranking.py`. Owner: "he should be smart to
know higher models by version number, and for Claude: Opus is better than Sonnet
and Sonnet better than Haiku, and Haiku is a short/small model." MEASURED the
same day: the g4f rows of claude-opus-5.5, claude-sonnet-5.5, claude-sonnet-4-5,
claude-sonnet-4, claude-haiku-4-5 and gemini-claude-opus-4-6 all scored the same
134.0 (the owner floor is family-wide, minus the relay discount 4), so Haiku 4.5
tied Opus 5.5; GPT-6.x vs 5.6 differed by ~1 point, inside `_AUTO_TOP_BAND`.

- **`modelrank.py`** (pure, stdlib, never raises). `parse(id)` -> (family, tier,
  (major, minor)) for any spelling: relay prefixes (`srv_x:`, `GithubCopilot:`,
  `Antigravity:`), `anthropic/`, `models/`, `:free`, `-thinking`, 8-digit date
  suffixes, `claude-sonnet-4-5` == 4.5, `claude-sonnet-4` == 4.0, `claude-4.5-haiku`,
  glued `claude40sonnet`, `gemini-claude-opus-4-6` (Claude, not Gemini),
  `grok-4.20` == 4.2. Families: claude, gpt, gemini, grok, qwen, glm, deepseek,
  kimi, minimax, mimo, hy. Tiers: claude opus/sonnet/haiku/fable/instant; gemini
  pro/flash/flash-lite; flash/lite/mini/nano/air/small everywhere; pro for
  gemini/deepseek/mimo. Unknown family or version -> None -> left alone
  (`llama`, `mistral`, `gpt-oss`, distills, `gemini-flash-latest`).
- **The rule** (`_benchmark_score`, after every floor, before the relay
  discount and `_shared_budget_penalty`): `score = min(score, cap + provider
  bias)`, `cap = newest reachable score of its family+tier - tier offset - gap`.
  It only LOWERS, only OLDER members of ONE family+tier, only models scoring
  >= `modelrank.STRONG` (120, the strong band; a strong newest release is the
  only anchor, so a 0.5B qwen4 cannot drag qwen3.8). The newest member keeps
  exactly what it had, so the owner's order BETWEEN families (kimi-k3 138.1 >
  glm-5.3 138 >= Opus 138 > Space Bunny 137.7 > Pixel Canary 137.6) does not
  move, nothing exceeds its owner ceiling, and nothing leaves the chain. ONE
  deliberate exception: rule (b) "Opus over Sonnet" puts the newest Sonnet 5.5
  (137.4) and Fable (137.7 / 137.6) under the old family-wide 138, i.e. Sonnet 5.5
  now sits under Space Bunny 137.7 and Pixel Canary 137.6 -- the owner's later,
  more specific Opus > Sonnet beats the 2026-07-31 family-wide floor (pinned in
  `test_the_owners_cross_family_order_is_unchanged`).
- **`gap(newest, version)`**: a newer minor = `MINOR_SPAN` 0.5 x m/(m+4), always
  < 0.5 (inside the band, the weighted pick still spreads); a major generation =
  `MAJOR_STEP` 3.0 + the minor swing, so always > 2.5 (an older generation leaves
  the 2.0 band), plus `LEGACY_EXTRA` 1.5 for each generation past the first
  (Claude 3.x is not "one release behind"; the boards put it in the D tier).
  One generation is kept deliberately small: Arena has Opus 4.6/4.7 (1505/1501)
  level with Opus 5.5 (1504).
- **Claude tiers** (`tier_offset`): Opus 0; Sonnet 0.6 under Opus (only when an
  Opus is reachable); Fable 0.3 under Opus (pinned halfway: AA 53 < Sonnet 56,
  Arena level with Opus); Haiku/instant 6.0 under the Sonnet of the SAME
  generation (measured against the Sonnet line, so it holds at 4.5 and 5.5;
  an untiered old `claude-2.1` is measured against Sonnet too). A small NEW
  model can still beat an OLD big one: Haiku 5.5 outranks Sonnet 3.x (two
  generations) but not Sonnet 4.5 (one) -- AA says Haiku 5.5 (43) > Sonnet 4.5,
  the one-generation step is deliberately small, so that stays an open owner
  call. Resulting scores (non-relay; g4f = minus 4): Opus 5.5 138, Fable 5.1
  137.7, Sonnet 5.5 137.4, Opus 4.6 135.0, Sonnet 4.5 134.4, Sonnet 4 134.1,
  Haiku 5.5 131.4, Haiku 4.5 128.4, Claude 3.x ~130.
- **GPT/Gemini/others**: gpt-6.1 137.2 > gpt-6-astra = gpt-6-luna 137.0 (the
  codenames keep their present relative order -- AA has astra 53, sol-6.1 52,
  luna 38; whether luna is a "small tier" like Haiku is an open owner call) >
  gpt-5.6 134.4 (> 2.5 behind). gemini-3.8-pro > 3.8-flash > 3.5-flash >
  3.5-flash-lite. Unplaceable ids (gpt-6.1-luna, gemini-3.8-pro, claude-fable-5.5,
  grok-5) are ordered by version only; no score was invented.
- **Newest reachable is computed, not hard-coded**: `_rank_anchors()` builds
  {(family, tier): (version, score)} from `_rank_fleet_ids()` = alive models of
  enabled, keyed providers (`_alive_models(_cached_catalogs())`, never
  `_declared_fleet`, which calls `_benchmark_score`), minus user-blocked models,
  minus listings `_chain_reliability_band` files as measured to fail (a bogus
  higher-version id on one relay must not hold the real family down; an id nobody
  has tried yet is still trusted -- rule (e) says "listed"), and minus a provider
  the quota state has parked >= `_CTX_OVERFLOW_LONG_WAIT`
  (g4f held 21 h is not where Claude 5.5 can be reached; a 60 s burst 429 does
  not flap the anchors). Cached `_RANK_TTL` 30 s, built under a non-blocking lock,
  scoring with `_benchmark_score("", id, _rank=False)` so a rebuild never
  re-enters itself. A newly listed claude-6 leads on the next rebuild and
  Sonnet 5.5 becomes the fallback. Empty or broken fleet = no adjustment.
  `tests/conftest.py` clears the cache around every test (`_rank_reset`).
- **Relay slots go to the best-ranked relays** (`_relay_keep_set`, used by the
  tool branch of `_build_chain`): `_TOOL_RELAY_MAX_HOPS` (3) relay hops used to go to
  the FIRST relays in `ordered`. `_lead_first` puts every relay copy of a
  tool-proven id (gemini-3.x) in the lead group while relay Claude sits 0.07
  under the gate (134.0 vs 134.07, the relay discount), so with
  google/gemini-3.8-flash alive three weaker gemini-3 copies (~130) took all
  three slots and relay Opus 5.5 / Sonnet 5.5 / gpt-6.1 never entered the chain
  (reproduced in the test). Now the slots go to the best by (healthy,
  not measured-slow, `_agentic_score`, position); the kept relays keep the
  position `ordered` gave them. Fail-open to the positional rule.
- **One failure is not "measured to fail"** (`_chain_reliability_band`,
  `_CHAIN_MIN_SAMPLES` 2): one failure and no success is Laplace 1/3, under
  `_CHAIN_UNRELIABLE` 0.35, so the pair sat in the sick tail for ~1.8 h (the lifetime
  counts halve every 8 h). The relay pairs claude-sonnet-5.5 and gpt-6-astra were
  there on exactly one failure each, both from ~505K-token requests no model
  could hold. A junk answer still counts double; two failures still band 2.
- **Measured, and deliberately NOT changed** (24 h of `turn-roles.jsonl`, 516
  tool turns): 0 first picks, 0 failed hops and 0 served turns on any claude-5.5
  or gpt-6.x pair. claude-sonnet-4-5 served 40 turns, and the router did NOT pick
  it: 34 of them were `routed` to the g4f space-bunny-free copy (an orchestrator
  choice), but the first actor of 39 was sonnet-4-5 in ONE hop -- the first entry
  of the chain walk that fit (85-126K tokens), i.e. the chain-order mechanism the
  two fixes above address, not a pin (g4f fresh picks since 10-07: 15, 9 sessions; Opus 5, Opus 4.8, gpt-6-luna,
  gpt-6.1-sol and gpt-6-astra WERE picked and failed: luna HTTP 504 and 403,
  astra an empty 200, one failure each in perf-stats for the rest). Every g4f
  row carries `_sustain_penalty` 29.0 (quota.py lists g4f as 5 per MINUTE and
  `_sustain_penalty` compares the 5 against a per-day yardstick, (150-5)/5), so
  relay Claude sits ~105 agentic against 137 first-party and leads only when the
  first-party hosts are out (38 of 516 turns started on g4f). A window-aware fix
  would also lift llm7/navy/nararouter, whose binding limits are hourly or
  token-based, and g4f's real budget is ~500K tokens/day (two ~505K-token
  requests ran at 02:40 and 02:48 on 10-08; by 03:31 g4f held `throttled_until`
  +21.7 h with count 1 = the gateway's own Retry-After, which `quota.mark_throttled`
  documents seeing before at 81486 s -- not a stale flag), so the demotion is
  right in effect and wrong in mechanism -- an owner decision. relay Opus 5.5 still sits 0.07
  under the lead gate (`_may_lead_pool`) by the owner's relay-discount design.
  SUPERSEDED 2026-10-10: the owner chose to fix the MECHANISM -- see "Relay
  quota units (2026-10-10)" at the very end of this file; the per-minute relays
  (g4f, llm7, navy, nararouter) no longer eat the ~26-29 point daily-scarcity
  penalty, while the relay discount, lead gate, throttles and measured failure
  still keep them behind a healthy first-party model.

## Publish online (free Cloudflare tunnel) (2026-10-08)

Owner: put a project's running preview on the internet with a free Cloudflare
Quick Tunnel (`cloudflared tunnel --url`, no account), show the link with a
countdown, expire it, and let the user make a new one. Covered by
`tests/test_publish_engine.py` (fake cloudflared script, fake clock, fake
downloader; nothing real is started, contacted or downloaded).

- **`publish.py`** (stdlib, no Flask, never imported by workspace at module
  level): `PublishError(code, message)`, `Manager` (`status(project_dir=None)`,
  `start`, `stop`, `renew`, `install`, `tick`, `sweep_leftovers`, `shutdown`,
  `project_stopped`, `has_active`), module `default`. Every outside effect is
  a constructor argument (`locate`, `launcher`, `clock`, `wall`, `spawn`,
  `kill`, `probe`, `preview_port`, `hub_ports`, `fetch`, `system`, `procs`,
  `flag`, `timer`); `tests/conftest.py::_no_real_tunnel` makes the module-level
  defaults (spawn, PATH lookup, download, probe, timer thread) fail loudly.
- **Tunnel dict**: `{id, project_dir, port, url (None unless live), state:
  starting|live|expired|stopped|failed, error, source: build|agent, started_at,
  expires_at, ttl_seconds, remaining_seconds}`. `status()` adds `server_time`,
  `enabled`, `cloudflared {available, path, version, platform, installable,
  installing, install_error, install_stage, install_progress}`, `limits`.
  `status(project_dir)` returns ONLY that project's tunnels (normcase+abspath).
- **Start** records a `starting` row under the lock (counted for dedupe and the
  limit of 3 BEFORE spawning), spawns outside it and returns at once. Command:
  `cloudflared tunnel --no-autoupdate --url http://127.0.0.1:<port>
  --http-host-header 127.0.0.1:<port>` (+ `--protocol http2` on the retry),
  `CREATE_NO_WINDOW` on Windows, own session on POSIX, stderr merged into
  stdout, env `CALVOUN_TUNNEL=<id>`, every `TUNNEL_*` var and the hub's port
  vars dropped. A reader thread per process takes the FIRST valid
  `https://<label>.trycloudflare.com` (host re-validated with urllib, a trailing
  boundary so `x.trycloudflare.com.evil.net` and `...com@evil.net` fail,
  reserved labels `api`/`www` skipped: cloudflared prints
  `https://api.trycloudflare.com/tunnel` in its failure messages).
- **Same project + port already starting/live => `start` returns THAT tunnel**
  (no second process, TTL of the duplicate ignored); `already_published` exists
  as a code but `start` never raises it. Dead rows of the same project+port are
  superseded by a new start; at most 10 dead rows are kept.
- **Time** lives in `tick()` only, driven by ONE daemon timer thread
  (`publish-timer`, started lazily); tests call `tick()` after advancing the
  fake clock. No address within 25 s of an attempt => kill and retry once with
  `--protocol http2`; a second miss, or a process that exits before an address,
  => `failed` with a fixed plain sentence (never a log line). Expiry =
  the EARLIER of the monotonic deadline and the wall `expires_at` (monotonic
  time stops during suspend on Linux/macOS). The TTL starts when the address
  goes live (`expires_at` is provisional while `starting`). Expired / stopped /
  failed rows keep their `error` but `url` is None; `stop` on a live row =
  `stopped`, on a finished row = dismiss it; `renew` replaces the old row.
- **Never kill by a stale pid**: `_kill_tree` does nothing on Windows for a
  process that already exited (measured: a late reader-thread cleanup killed the
  NEXT tunnel's launcher after pid reuse), and never closes the pipe from
  another thread (a buffered read in the reader holds its lock: 46-58 s hang);
  the reader closes its own stream. A process whose pipe is held open by a
  child is still noticed: `tick()` checks `poll()`; and `_on_exit` acts only when
  `poll()` shows the process really ended (an EOF with the process alive is left
  to `tick()`). Test pitfall: two fakes appending to one log file overwrite each
  other on Windows (one file per launch), and `launches()[0]` is not "the first
  tunnel started" (python start-up order) -- map by the `CALVOUN_TUNNEL` marker.
- **The URL is a capability.** Returned only by the token-gated API
  (`Cache-Control: no-store`); every log line, tail and error goes through
  `scrub()`; hub.log gets the tunnel id and the project's folder name only.
- **Ports**: hub ports (`PORT` env, 8787, `agentic_chat._port()`), anything
  outside 1024-65535 and `DENY_PORTS` (databases, brokers, docker/k8s, VNC/RDP,
  SMB, SSH) => `forbidden_port`; the port must answer an HTTP status line on
  127.0.0.1 (any status, 5xx included: a compiling dev server). The preview
  port is `workspace.status(project_dir)` only when `running` AND `port`.
- **Hooks**: `workspace.stop` -> `publish.default.project_stopped` (lazy import,
  outside `_lock`); `workspace.reap_idle` skips a previewed project that has an
  active tunnel (otherwise a 60 min link died at the 30 min idle reap with the
  dashboard closed); app boot -> `publish.default.sweep_leftovers()` right after
  the stale-agent-CLI sweep (stops processes carrying `CALVOUN_TUNNEL` that are
  cloudflared, skipping the registry's own subtree, hub pids and anything else,
  and any tunnel stamped `CALVOUN_TUNNEL_HOME` = ANOTHER hub's normalised state
  dir -- a sandboxed test hub booting must not kill the real hub's live links;
  no stamp = ours);
  `atexit` -> `default.shutdown()`. A hard kill of the hub leaves cloudflared
  running until the next boot sweep.
- **Install** (explicit `POST /api/publish/install {confirm:true}`, or by itself
  after boot -- see "cloudflared installs itself" below): official
  asset for `platform.system()/machine()` (windows-amd64.exe, linux-amd64|arm64,
  darwin-amd64|arm64.tgz), release metadata from `api.github.com` (metadata
  host only), file from `github.com` / `objects.githubusercontent.com` /
  `release-assets.githubusercontent.com` with https + allowlist re-checked on
  EVERY redirect hop (`_AllowlistRedirect`). The SHA-256 must come from the
  asset's `digest` or a line of the release notes naming exactly that asset; two
  sources that disagree, none, or a mismatch => nothing installed (the partial
  file is deleted). tgz: no `extract*`; any absolute / `..` / backslash member
  refuses the whole archive; only the single regular `cloudflared` member is
  streamed out. Written to `state_dir()/bin/` via `os.replace`, mode 0o755,
  `cloudflared.version` sidecar = the release tag (the version shown for an
  installed binary; nothing downloaded is ever run by the hub except at
  Publish; a PATH binary is asked `--version` once and cached). Runs in a
  background thread; failures show in `status().cloudflared.install_error`,
  only an unsupported platform or `disabled` raises synchronously.
- **Routes** (`# PUBLISH:` section of app.py): `GET /api/publish[?project_dir=]`,
  `POST /api/publish/start|stop|renew|install`. start and install need the body
  `confirm` to be literally `true` (`confirm_required`, 400). Errors are
  `{error, code}` with 400 bad_ttl/bad_request/confirm_required, 403
  forbidden_port/disabled, 404 not_found, 409 the rest. Flag `publish_enabled`
  (default true): off refuses start/renew/install, never stop/status.
- Known limits: an existing `~/.cloudflared/config.yml` can stop a quick tunnel
  from starting (not worked around: the command is the contract); the
  checksum-in-release-notes parser is lenient but unverified against a live
  release; macOS/Windows/Linux asset names are the documented ones.

## cloudflared installs itself (2026-10-10)

Owner: the installer must install cloudflared automatically, not only on the
Publish panel's click. Covered by `tests/test_cloudflared_auto_install.py`
(fake downloader, fake timer, fake wall clock, state file under tmp_path; no
network, no real timer thread, nothing left running).

- **What**: `publish.AutoInstaller` (module object `publish.auto`, attached to
  `publish.default`). About 60 s after boot (`AUTO_DELAY_SECONDS`), when flag
  `cloudflared_auto_install` (config.get_flag, default True; an unreadable
  config counts as OFF) and `publish_enabled` are on and no cloudflared is
  found, it calls the SAME `Manager.install()` the button calls: same release
  lookup, host allowlist on every redirect hop, SHA-256 check. The auto path
  downloads, verifies and writes nothing itself; a missing or mismatching
  checksum leaves nothing installed and is reported (`auto_error` = the
  engine's sentence).
- **Never** at import (the constructor stores settings: no thread, no file
  read), never on the boot thread (`start()` arms one daemon `threading.Timer`
  named `cloudflared-auto` and returns), never while `blocked()` names a
  reason. app.py's `_cfi_blocked`: `_UPDATE_DRAIN.active()` or
  `_auto_update_state["updating"]` -> "draining"; `_restart_is_vetoed_by_stop()`
  (sticky Stop, runtime drain) -> "stopped". It then looks again
  `AUTO_RECHECK_SECONDS` (60) later; a `blocked()` that raises also waits.
- **At most once per 24 h after a failure** (`AUTO_RETRY_SECONDS`), across
  restarts: `state_dir()/cloudflared-auto.json` = `{v, result
  (installing|installed|failed|unsupported|interrupted), last_attempt, error,
  platform, interrupted, updated_at}`, written with mkstemp in the same folder
  + fsync + `os.replace`. After a failure the next look is armed for the rest
  of the 24 h (again at each boot). An attempt the hub stopped in the middle of
  (`installing` found at boot) is tried once more; a second one in a row is
  `failed`. The Install button works any time and never touches this record.
- **Unsupported platform** (no `_ASSETS` entry): recorded once (`unsupported`,
  the platform, the manual-install sentence) and never tried automatically.
- **Never two downloads**: `Manager.install(on_done=None)` now re-checks
  `running` inside the lock that starts the worker (the button and the timer
  could both pass the first check before); `install_started_by(on_done)` tells
  the AutoInstaller whether ITS call started the download. A download the
  button started is left alone (`busy`, nothing recorded); the button pressed
  during an automatic download joins it. `on_done(error|None)` runs once on the
  worker thread, outside the Manager lock.
- **Locks**: the AutoInstaller's `_lock` may be held while calling the Manager
  (order: auto -> manager, never the reverse); `view()` (called by
  `Manager._cloudflared_info`) takes only the leaf `_io` lock, so a status read
  never waits on an install decision and can never close a lock cycle.
- **Status**: `GET /api/publish` -> `cloudflared` gains `auto_install` (the
  flag), `auto_state` (off | installing | installed | idle | unsupported |
  failed | scheduled, in that precedence; off = this flag or publishing off),
  `auto_last_attempt`, `auto_error` (failed/unsupported) and
  `auto_next_attempt`. A Manager with no AutoInstaller keeps its old fields.
- **Setting**: no generic flag route fits, so `POST /api/publish/install
  {auto: true|false}` (`_cfi_set_auto` -> `AutoInstaller.set_enabled`) saves
  the flag and arms a look `AUTO_REARM_SECONDS` (5) out (on, after boot) or
  cancels the pending one (off); it never downloads in that call. Non-bool ->
  400 `bad_request`, a save error -> 500 `save_failed`. Without `auto` the route
  is the old confirmed install, unchanged. No route added.
- **Boot**: `_cfi_start()`, one line right after
  `publish.default.sweep_leftovers()` in `__main__` ->
  `publish.auto.start(blocked=_cfi_blocked)`. Flag off = nothing armed at all,
  i.e. the explicit-click behaviour byte for byte. atexit stops the timer.
- **Panel** (`#publish-install`): checkbox `#publish-auto` "Install cloudflared
  automatically" (a `.publish-consent` row, 44 rendered px; shown when the hub
  sends `auto_install` and the platform is installable); "Installing
  cloudflared automatically…" while the automatic download runs; "The automatic
  install failed: <error> ..." with the existing Install button and manual
  command when it failed; "will be installed automatically in a moment" while
  scheduled (the open panel polls then). The Install button stays an explicit
  click.
- `tests/conftest.py::_no_real_tunnel` also fences `publish._auto_timer` (fails
  loudly) and stops `publish.auto` after every test.
- Left as is: unticking the box does not cancel a download already running (the
  engine has no cancel; it finishes); README's Security list of outbound calls
  was not extended (the change is described in the Publish section).

## Multi from the terminal CLIs (2026-10-10)

Covered by `tests/test_cli_multi_sessions.py`. Owner (2026-10-10): "inside the
CLI the multi mode works as crew, but from the frontend (Build page) Multi works
well in parallel." It was true -- `_crew_name_for` mapped "multi" to the crew
pipeline for a /v1 turn because "a /v1 call carries no project folder to run in",
which stopped being true once `_v1_project_cwd` / `_project_dir_from_messages`
read the CLI's `<cwd>`/env block. Now a terminal CLI that selects the Multi tier
gets the SAME real `swarm_windows` run the Build page does (planner + up to N
helper CLI sessions on different models + review), not the crew phase pipeline.

- **Flag** `cli_multi_sessions` (`config.get_flag`, default on). Off = today's
  behaviour byte for byte (every gate returns None, the request takes the crew /
  roles path). No new `@app.route`.
- **The gate** (`app._cm_multi_cli_intercept`, called in all three /v1 handlers
  right before the `_is_swarm_model` dispatch -- chat only on the real
  `/v1/chat/completions` path, since Gemini/Ollama/`/v1/completions` share that
  router). ALL must hold, else None -> today's path: the id resolves to the Multi
  tier after `_apply_category_effort` / `_mode_and_effort` (bare `multi`,
  `<category>-multi`/`/multi`, codex xhigh/max/ultra); NOT a hub-driven session
  (`_build_sid()` is set only for `/build/<sid>` -- a Build conversation OR a
  Multi worker, both reach the hub at that prefix, so a real terminal CLI has it
  unset; `swarm_windows.worker_info` would name a worker); not the dashboard quick
  chat (`X-Free-LLM-Hub: dashboard`); tools present; not a compaction request
  (`ctxwin.is_compaction_request`); a fresh user instruction
  (`_awaiting_new_instruction`); a known safe folder (`_cm_project_dir` =
  `_cm_trusted_cwd` -- ONLY the CLI's own environment block, see security fix 3
  -- an existing dir, never the hub repo (`_cm_is_hub_repo`), plus the publish
  broad-folder rule `_publish_folder_too_broad`, so never a root / home / above
  home); and `_multi_wants_a_swarm(text)` says WORK (the same any-language gate
  the Build page uses). The goal is the last user message with a CLI's
  `<system-reminder>` blocks removed.
- **The run** is started EXACTLY like the Build page: `_cm_multi_cli_intercept`
  opens a hub /agent conversation as the OWNER (like `_hb_start_run`:
  `agentic_chat.start_session(helper_cli, folder, quality="multi", mode=...)`,
  titled from the goal, `set_quality("multi")`, `set_auto_resume(True)` so
  boot/graceful-update resume cover it), then reuses `_multi_turn_events(owner,
  sess_info, text)` (plan + helpers + review + its own on_done recording) made
  durable by `agentic_chat.live_run` with a copy of the request context -- the
  shared helper both paths use. So the run shows in the Build page, the Running
  popup and Activity with Stop/Continue. The helper CLI = the caller's own CLI
  (from the User-Agent, like live window steering) when the hub has a working
  `agentic_chat._isolated_bin` of it, else the Build default (opencode). The
  pair (CLI conversation `ctxwin.conversation_key`, folder `_cm_folder_key` =
  `normcase(realpath(folder))`) -> {owner sid, run id, folder, consent} is
  persisted in `state_dir()/cli-multi-runs.json` (`_cm_map_key`: conversation
  key + a hash of the folder; atomic write, LRU 200, 7-day TTL, `_cm_map_put`
  MERGES so a later write never drops the folder or consent), so later turns of
  the same CLI conversation in the same folder find the owner.
- **The CLI turn is answered with TEXT ONLY** (never a tool call), streamed in
  its own protocol (`_cm_emit`: chat chunks; Responses via `_responses_stream` on
  a live chat-SSE line generator through `_ReplayUpstream`; Anthropic via
  `_anthropic_stream` the same way -- the swarm replay's trick): a first line with
  the watch link `http://127.0.0.1:<port>/agent/<owner sid>`, the events the
  Build page shows, a keepalive "· still working (Nm Ns)" at least every 20 s
  (`_cm_keepalive_wrap` injects a tick when the source is silent), then the
  report + link. Usage is the request's own estimate (`_reported_prompt_tokens`),
  so the CLI's context accounting is unchanged. Non-stream returns the same text
  as one message.
- **Safe end before the CLI's cap** (`_cm_safe_end_seconds`, default 240 s): past
  it the turn ends with "The Multi run keeps working in the background (k/n phases
  done). Send any message to follow it, or 'stop multi' to stop it. Watch it:
  <link>". The run NEVER depends on the CLI staying connected: a client
  disconnect or the turn ending does not stop it (`_cm_keepalive_wrap` drains the
  producer on its own daemon thread into an unbounded queue; nothing calls
  `swarm_windows.stop` on disconnect). Only its own Stop does: Build page, Running
  popup, or a CLI message that is EXACTLY "stop multi" (handled in `_cm_attach`,
  no model call; see the security bullet below).
- **Later turns** of the same CLI conversation in the same folder while its run is
  live (any fresh message, "status", "continue") re-attach and stream progress
  again with NO model call and NO second run (`_cm_attach` via
  `_multi_follow_events`); "stop multi" stops it. When the run has ended, a new
  instruction starts a new run and a short "continue" resumes the unfinished
  phases -- both through `_multi_turn_events`, which already decides resume vs
  fresh (`_multi_is_continue`) exactly as the Build page does.
- **Security fix 1 -- no cross-conversation control** (review of 8f647b8). A
  conversation key alone is not an identity: its fallback (no session id) hashes
  the system prompt + first instruction, so two different conversations, even in
  different folders, can share one -- and the first version looked the map up and
  re-attached BEFORE the folder was known, and stopped on a bare "stop" or any
  message containing "stop multi". Now the folder is resolved FIRST (no folder ->
  today's path), every lookup is scoped to (conversation key, folder), and
  re-attach / stop happen ONLY when the row's stored folder equals this request's
  folder AND the live run's own `project_dir` (from `swarm_windows.status`) equals
  it too (`_cm_live_run`; a missing `project_dir` fails closed). Stop happens only
  when the WHOLE last user message -- a CLI's `<system-reminder>` blocks removed,
  trimmed, case-insensitive, trailing punctuation (any script) ignored
  (`_cm_last_user_command` / `_cm_norm_cmd`) -- is exactly "stop multi": never a
  bare "stop", never a sentence that merely contains the words.
- **Security fix 2 -- WHO may start helper agents: setting `cli_multi_approval`**
  (second review, of main 608dfd6; replaces the boolean flag `cli_multi_confirm`
  of the first fix). A "go multi" chat reply is an UNAUTHENTICATED /v1 message:
  /v1 is open on localhost when no local API key is set, and 127.0.0.1 is shared
  by every OS user of the machine, so any local process could make the hub start
  helper agents that run commands AS THE HUB OWNER. `_cm_approval_mode()` reads
  `config.get_setting("cli_multi_approval")`; anything unreadable or unknown =
  "dashboard".
  - **"dashboard" (default).** The first eligible turn starts NOTHING: it records
    a SINGLE-USE request {random id, conversation key, folder, goal, helper cli,
    caller cli, created, expires in 15 min} (`_CM_APPROVALS`, in memory, bounded
    to 20, oldest dropped first) and answers in text: "Multi wants to start up
    to N helper agents that edit files and run commands in <folder>. Approve it
    in the hub dashboard: http://127.0.0.1:<port>/?approve=<id> (or the banner on
    any hub page). Waiting...". The SAME turn then looks every 2 s
    (`_cm_approval_stream`, keepalive every 20 s): approved -> the request is
    consumed and the run starts and streams in that turn exactly as before;
    denied / expired -> it says so; past the CLI's safe end -> "Approve it in the
    dashboard, then send any message here", and a later turn of the same
    conversation + folder (any message, even one that is not work) starts an
    approved-but-not-started request with its STORED goal (consumed); a still
    pending one is waited on again; a resend of the same goal waits on the same
    request, while new work replaces it (new id). Approval adds 15 minutes for
    the pickup. A "go multi" reply never bypasses this mode. Approval ONLY
    through two new token-gated routes (under /api/, so `_local_control_guard`
    applies: the control token, plus the dashboard header on the POST): `GET
    /api/cli-multi/pending` (id, folder, the FULL `goal`, `goal_display`,
    `goal_sha256`, `goal_chars`, `hidden_chars`, caller, source, created,
    expires; pending ones only) and `POST /api/cli-multi/decide {id, approve:
    true|false, goal_sha256}` (200; 400 bad body; 404 gone; 409 already decided,
    or an approval whose `goal_sha256` is missing / differs -- codes
    `hash_required` / `hash_mismatch`; a denial needs no hash). README route
    count +2 (176). The goal is logged 80 chars at most.
  - **What is approved is EXACTLY what runs** (third review, of 15d1051: the
    banner used to show a 300-character excerpt while the FULL goal ran, so a
    benign first part could hide the real instructions after it). The goal is
    stored whole, at most `_CM_GOAL_MAX_CHARS` (20000); a longer request is
    REFUSED at creation with a plain message (`_cm_goal_problem`), never cut.
    Line endings are canonical (CR / CRLF -> LF, `_cm_canonical_goal`) before
    anything else; that canonical text is what is shown, hashed and run.
    `goal_sha256` (kept as the field name) is the SHA-256 of the canonical JSON
    of EVERYTHING an approved run receives -- {folder, goal, helper_cli}
    (`_cm_request_sha256`) -- and an approval is accepted only when it sends
    that hash back (`_cm_approval_decide`, `hmac.compare_digest`).
  - **What is SHOWN is an ALLOW-list** (fourth review, of af7fc30: the
    deny-list missed classes). `_cm_goal_display` shows as plain text ONLY the
    ASCII space, newline and tab; letters, numbers, punctuation and symbols
    (L*, N*, P*, S*) that draw a glyph; and combining marks (Mn/Mc/Me) that
    follow a base LETTER, at most `_CM_MAX_MARKS_PER_BASE` (4) on one letter, so
    French / Arabic / Vietnamese / Devanagari / Hebrew diacritics stay readable.
    EVERYTHING else is shown as a visible `[U+XXXX]` marker and counted: every
    Cf / Cc / Co / Cs / Cn character, every space other than U+0020 (Zs, Zl
    U+2028, Zp U+2029), variation selectors (U+FE00-U+FE0F, U+E0100-U+E01EF,
    the Mongolian free variation selectors), glyphless letters and symbols
    (`_CM_GLYPHLESS`: Hangul fillers U+115F U+1160 U+3164 U+FFA0, U+2800, U+180E,
    U+2061-U+2064, U+034F, U+17B4/U+17B5, U+FFFC) and any isolated or
    over-stacked combining mark. The same rules apply to the folder and helper
    CLI names (`folder_display`, `helper_cli_display`; both are covered by the
    hash, so both are shown). REFUSED outright, with a plain message and
    nothing pending (`_cm_request_problem`): any bidi control or mark (U+202A-
    U+202E, U+2066-U+2069, U+061C, U+200E, U+200F); ANY Unicode tag character
    (U+E0000-U+E007F, "ASCII smuggling": invisible to people, read as text by
    models); more than `_CM_MAX_MARKED` (20) markers over goal + folder + CLI.
    The CLI path trims leading/trailing whitespace (NBSP included) before
    storing, as it always did -- the trimmed text is what is shown and run. The
    MCP pending path applies the same rules (`_cm_mcp_start`). The test file
    builds every special character with `chr(0x...)`: it contains no invisible
    or direction-changing character itself.
  - **An approved run receives ONLY what was approved -- option (a)** (fourth
    review). A dashboard-approved CLI request starts through
    `_multi_turn_events(..., bare=True)`: the run gets the stored goal, the
    stored folder and the stored helper CLI and NOTHING from the conversation
    -- no `context=` (owner memory, conversation recap, previous run's result),
    no board `goal_brief`, and never a resume of an earlier run (a "continue"
    is approved and run as a fresh goal). Option (b) (showing and hashing the
    context) was not needed: the workers work on the folder's files, which hold
    the state. The MCP approved path already passed no context or goal brief
    (`_cm_mcp_swarm_start_now`). The "chat" and "off" modes (unauthenticated by
    the owner's choice) keep today's behaviour: conversation context and the
    "continue" resume. Residual, in plain words: the helpers still read the
    project's own files, and every hub-run CLI session still gets the hub's
    usual per-session briefs (craft briefs, model guides, and the board's goal
    note / brief-file section for that folder, which is owner-managed board
    data) -- none of that comes from the terminal CLI's request.
  - **"chat".** The owner's explicit choice to accept the risk: today's "go
    multi" flow (pending request 10 min in `_CM_PENDING`, consent remembered for
    the (conversation, folder) for the 7-day map TTL, expired / mismatched "go
    multi" answers that nothing is waiting).
  - **"off".** Direct start, no approval.
  - **Dashboard banner** (`templates/index.html`, the one template every hub
    page renders): `#cm-approve-banner` lists each pending request as "A
    terminal CLI asks to start Multi in <folder>: <start of the goal>" (an MCP
    request reads "An MCP client asks ...", hidden characters counted), then
    "Show the full request (N characters)", which reveals ALL of
    `goal_display` in a scrollable monospace `<pre>` (textContent only), then
    [Approve] [Deny]. Approve stays DISABLED until that full text has been
    opened once (`cmApproveSeen`, kept across polls), and it sends the listed
    `goal_sha256`. Theme tokens only; polled every 10 s and only while the page
    is visible, redrawn only when the set of requests changes (an open box
    keeps its scroll); `?approve=<id>` scrolls to that request and focuses its
    "Show the full request" button.
  - **The MCP tool `swarm_windows_start`** (POST /mcp is not token-gated) follows
    the same setting. "dashboard": the call starts nothing and returns
    `{pending: true, id, approve_url, note}`; after the owner approves, the client
    calls `swarm_windows_start` AGAIN with the same arguments plus that `id`, which
    starts the request's STORED goal, folder and cli (single use;
    `_cm_mcp_start_approved`; still pending -> the same pending answer; denied /
    unknown / expired / used -> a tool error). "chat" and "off": the direct start
    as before (an MCP client has no chat to say "go multi" in). hub_mcp calls the
    optional `start_approved` callable only when wired, so a fake `_SWARM` without
    it behaves exactly as before.
  The existing local API key guard (`_guard_v1`, a `before_request` on every /v1
  path) still runs before all of this, unchanged.
- **Security fix 3 -- the folder comes ONLY from the CLI's own environment
  block** (second review). `_v1_project_cwd` takes the FIRST `<cwd>` tag or
  "working directory" line in system, USER or developer messages, so pasted text,
  a CLAUDE.md / file content Claude Code places in a user-role
  `<system-reminder>`, or any injected block could choose where helper agents
  run. `_cm_trusted_cwd` accepts a folder only from (a) the `_ENV_BLOCK_RE` spans
  and `_ENV_LINE_RE` lines of the LEADING system/developer message(s), with
  `<system-reminder>` blocks removed first (opencode / Claude Code
  `<env>Working directory: X</env>`, Claude Code's "Primary working directory:
  X", Kimi Code's "The current working directory is `X`"), or (b) codex's
  `<environment_context>...<cwd>X</cwd>...</environment_context>` when it is the
  WHOLE content of a user message (string, or its only non-empty part; exactly
  one block -- a crafted "block, user text, block" sandwich never matches).
  Never a line in ordinary user text, a `<system-reminder>`, a tool result, an
  assistant message or a system message that is not leading. Two or more
  DIFFERENT folders from trusted places = ambiguous = no run (fall through);
  the same folder spelled twice is one. The hub-repo and broad-folder refusals
  still apply after it. `_v1_project_cwd` and its other callers are unchanged.
- **Residual risk, in plain words:** in the "chat" and "off" modes any local
  client -- including another OS user's process on the same machine -- can
  start helper agents that edit files and run commands as the hub owner: "chat"
  needs only an unauthenticated "go multi" message, "off" needs nothing. That
  applies to the CLI Multi tier on /v1 AND to the MCP tool
  `swarm_windows_start`. Only "dashboard" (the default) puts a token-gated human
  approval in front of every start. Separately, in every mode the folder the
  owner approves is whatever the CLI's own environment block names: a CLI whose
  system prompt carries injected instruction text with a fake environment block
  could still name a folder -- the approval banner shows it, and two different
  folders mean no run.

**Per-CLI streaming-timeout evidence (requirement 4).** The question is whether a
stream that keeps sending content deltas survives long (keepalives every 20 s
defeat an IDLE timeout) or hits an OVERALL request/stream cap (keepalives do NOT
defeat that). Read from the installed CLIs on this machine on 2026-10-10 (global
npm + `~/.free-llm-hub/isolated-clis`, read-only); compiled binaries give string
fragments, not source, so some of this is inferred and marked:

- **opencode 1.18.35** (global) / 1.18.11 (isolated), compiled `opencode.exe`:
  strings show `idleTimeout` and `requestTimeout:0` (disabled) on the streaming
  fetch path plus a `DEFAULT_TIMEOUT=600000` ms on the AI-SDK client -- i.e. the
  cap that matters is IDLE-based (reset by each delta), with no fixed overall
  stream-duration limit found under ~10 min. A stream that keeps sending content
  survives well past 30 min. Safe end raised to 540 s (`_CM_SAFE_END`), under the
  observed 600000 ms, to keep watching longer while staying conservative.
- **codex 0.154** (the task's version; the isolated launcher here is a 421-byte
  script, the Rust binary was not locatable for strings). From the graceful-update
  read + codex's config: `stream_idle_timeout_ms` is an IDLE timeout (default
  ~300 s), reset by streamed content; no overall request cap is documented.
  INFERRED (binary not re-read here); safe end kept at the conservative default
  240 s.
- **Claude Code 2.1.220** (isolated) / 2.1.293 (global), compiled `claude.exe`:
  timeout constants are not plain strings in the Bun-compiled binary. From the
  graceful-update read: Claude Code retries dropped streams and uses an idle, not
  an overall, timeout; keepalive content keeps a stream alive. INFERRED for the
  overall-duration question; safe end 240 s.
- **kimi-code 0.39.1**: not installed as readable JS here (the isolated `kimi`
  dir holds only config). From the graceful-update read: step-level attempts, no
  overall stream-duration cap known. INFERRED; safe end 240 s.

Left out on purpose: the persisted map stores the run id best-effort (refreshed on
each re-attach via `_cm_map_put`); in-process liveness uses `_multi_run_for`
(cross-restart continuity is the owner conversation + auto_resume, not this map).
The exact overall caps for codex/claude/kimi are not firmly verified from their
compiled binaries, so their safe end is the conservative 240 s rather than a tuned
value.

## Roles: fewer turns with no answer (2026-10-10)

Owner: "crew / swarm roles is shitty: some don't answer." MEASURED by me via
`scripts/role_eval.py --since 2026-10-08T05:30` over turn-roles.jsonl (2026-10-08
05:30 UTC -> 2026-10-10): **334 roles turns, 0.141 no-answer rate, 2.03
calls/turn, 23.4% of calls wasted, 0.589 verifier usable rate** (the 82
`single:true` team-notes rows of Normal/Max turns are NOT roles turns and are
now excluded from the roles stats). Of the failed actor hops: `no answer in
time` 36 (nvidia z-ai/glm-5.3 15, moonshotai/kimi-k3 12, deepseek-v4.1-flash 3,
glm-5.3-flash 4), `window too small for ~N tokens` ~45 (uncloseai Qwen3.8-27B
65K, groq qwen3.8 8K cap, nvidia palmyra-med-70b-32k), `RequestException: tool
schema alone (...) exceeds ...` 6 (groq qwen3.8), HTTP 400 13 (uncloseai
Qwen3.8-27B 12 -- the system-message 400, learned per boot by `[system-first]`;
glm/glm-4.5-flash 1), HTTP 404 6 (six niche nvidia ids, each once). Verifier:
123 runs, 50 no-verdict (41%), of which 28 unparsed and ~22 timeouts on
default-THINKING models -- the one usable verifier was
nvidia/.../ising-calibration-1.5-31b (69/81), while kimi-k3 0/6,
deepseek-v4.1-flash 0/6, glm-5.3-flash 0/3 burned the whole `_VERIFY_DEADLINE`.
Covered by `tests/test_roles_reliability.py` (hermetic: fake fleets/clock/
upstream, no network). All new app.py names are `_rr_`. Each change is
flag-gated and byte-identical to before when its flag is off or it does not
apply (the request does not clear, no request clock, etc.).

- **Fix #1 -- every fit decision is sized on what the hop will SEND**
  (`_rr_compute_sent_est` / `_rr_set_request_fit` / `_rr_fit`, flag
  `rr_fit_on_cleared`, default on). The post-clear estimate is computed with the
  SAME deterministic path as `_clear_old_results_for_hop` (same flag
  `old_tool_result_clearing`, same `ctxwin.OLD_RESULT_CLEAR_FROM_TOKENS`, same
  `ctxwin.clear_old_tool_results`, same `_est_tokens`), stashed once per request
  on `g.rr_sent_est` by the three `/v1` handlers (and the roles run), and used
  for: the roles actor skip, the `_route_by_difficulty` pin-drop + fresh-pick
  roomy filter, `_ChainClock._roomy_first`, `_quality_fallback_pick`, the front
  door (`_front_door_overflow` refuses only when even the CLEARED size cannot
  fit), `_ctx_others_cannot_serve`'s `need`, and the long-context band
  (`_prefer_fast_long_context`'s threshold). WHY those two choices: the
  long-context band decides hop ORDERING by the work a hop actually does, so it
  reads the SENT size; the **scaled request deadline keeps the ORIGINAL est** --
  it is a time CEILING, a larger value only grants more time (never used up once
  the body is cleared), and sizing it on the smaller cleared estimate could cut
  a genuinely slow-but-healthy hop. **Reported usage to the CLI stays on the
  ORIGINAL est** (the `_reported_prompt_tokens` contract; `_rr_fit` is never in
  that path). The overflow/compaction signal is unaffected: clearing shrinks a
  message's CONTENT, it never drops a unit, so it is never counted as "dropped
  history" (and `_compact_to_budget` still sizes the stubs but reads the
  originals for the recap/facts). `_rr_fit` caps at the caller's est and falls
  back to it, so a missing app context (the tests) or a mismatched est is safe.
- **Fix #2 -- a hop the hub already knows it cannot make never spends an actor
  hop** (`_rr_prefilter_skip`, flag `rr_prefilter_hops`, default on). In the
  roles walk, BEFORE dispatch: a model that is dead / not offered
  (`_is_model_skipped`) or whose tool schema alone exceeds its window
  (`_rr_tools_exceed`, the same test `_upstream_chat` makes before it raises) is
  skipped with NO call -- no actor hop, no attempt, no `wasted_call`
  (`actor_calls` is not incremented), no provider failure. A roles hop that
  comes back 404 (or a 400 naming the model missing) is remembered with the
  EXISTING dead-model machinery (`_rr_remember_dead_model` ->
  `_mark_model_dead`/`_maybe_mark_missing_model`, 6 h then re-probe) so the
  prefilter and `_build_chain` stop picking it next turn.
- **Fix #3 -- the `best` fallback gets time** (`_rr_roles_turn_end`, flag
  `rr_reserve_fallback`, default on). The roles stage stops at the stage
  deadline capped by the request clock MINUS a reserved slice
  (`_RR_FALLBACK_FRAC` 0.30, clamped 60..120 s), so the single-model `best`
  fallback (which shares this request's clock after roles returns None) can
  serve one strong model on the already-cleared context. The reserve is skipped
  when there is no request clock or it would leave the roles stage under
  `_RR_ROLES_MIN` (60 s). Checked with the numbers: a big STREAMED turn
  (request clock capped at `LONG_DEADLINE_STREAM_MAX` 285 s) gives roles 180 s
  and ~105 s to the fallback; a big NON-stream turn (scaled deadline ~330 s)
  now gives roles 231 s and 99 s to the fallback instead of 330/0.
- **Fix #4 -- a verifier that never answers is not a second opinion**
  (`_rr_verifier_worth` / `_rr_verifier_pool`, flag `rr_verifier_prefilter`,
  default on, under the `turn_verifier` kill switch; keeps W's
  `_verifier_unusable` pool filter). After `_rank_verifier_pool`, a candidate
  that is a default-thinker / measured-slow model with NO usable-verdict record
  (`_verifier_rate` at/under its Beta(1,1) prior) is dropped -- it reliably
  times out on the 25 s `_VERIFY_DEADLINE`; a thinker WITH a real usable record
  is kept. When NONE is worth a call the verifier is SKIPPED (ship the original,
  `verdict = "skipped: no usable verifier"`) instead of burning a call + the
  deadline. Read-only steps already skip the verifier (`verify.is_risky`), so
  that case is untouched. Left out on purpose: shortening the digest for big
  turns (verify.py is another concern's module, and the data shows the failures
  were thinking models that time out regardless of input size -- the prefilter
  removes them).
- **Fix #5 -- `scripts/role_eval.py --since`** prints, for a time cutoff (a unix
  timestamp, an ISO datetime/date in UTC, or a relative age like `36h`/`7d`/
  `2w`), the five numbers the owner compares before/after: roles turns,
  no-answer rate, calls/turn, wasted %, verifier usable rate. Rows that predate
  the `ts` field are dropped when `--since` is set; `single:true` team-notes
  rows are counted separately (`single_team_turns`) and excluded from the roles
  stats. Old rows (no new fields) still parse.

## Ranking follows the boards (2026-10-10)

OWNER DECISION 2026-10-10 (replaces 2026-07-31's "Kimi K3 top free model, 138.1
just above GLM 5.3's 138"): "benchmark them in our hub like the public boards,
and in future a NEW version must of course rank higher than them automatically."
Covered by `tests/test_evidence_ranking.py` (hermetic: fake fleets, no network).
`benchmarks.py` (pure, stdlib, never raises) + an optional `benchmarks.json`
override hold a dated, sourced snapshot per model: `tb4` (Terminal-Bench 4.0 %),
`automation` (AutomationBench %), `aa` (AA Intelligence Index), `arena` (LMArena
rating), with SOURCE/DATE. All app.py names added for this are `_ev_*`.

- **Two evidence kinds, two paths.** AGENTIC evidence (TB4.0 first,
  AutomationBench as a sub-point tiebreak) orders TOOL turns: `_agentic_score`
  adds `benchmarks.agentic_delta` (`_ev_agentic_bonus`) -- monotone, bounded
  (<= ~11), 10 pts per 100% TB4.0. GENERAL evidence (AA + LMArena) orders
  TOOL-FREE (chat) turns: `_benchmark_score` places the named models in a narrow
  strong-band window (`_ev_GEN_LO/HI` 133.0..134.3, capped 134.45 under hy3) by
  `benchmarks.general_rank` (`_ev_general_floor`). The bonus is 0 and the floor
  None for any model the boards do not cover, so everything else keeps exactly
  today's score.
- **Today's orders** (the six free models the owner benchmarked 2026-10-10):
  - TOOL: GLM 5.3 (TB 42) > GLM 5.3 Flash (33) > DeepSeek V4.1 Flash (27) >
    Gemini 3.8 Flash (20) > Kimi K3 (13) > Qwen 3.8 27B (6). GLM 5.3 lands ~2.9
    clear of Kimi K3 -- OUTSIDE `_AUTO_TOP_BAND` (2.0), a 29-pt TB4.0 gap is never
    a coin flip -- while GLM 5.3 vs GLM 5.3 Flash (~1.1) stays inside. A model with
    agentic evidence may lead a tool turn even under the 138 floor
    (`_may_lead_agentic` + `_ev_has_agentic_evidence`).
  - CHAT: Kimi K3 ~ Gemini 3.8 Flash ~ GLM 5.3 close at the top (~134.0-134.1),
    then GLM 5.3 Flash, DeepSeek V4.1 Flash, Qwen 3.8 27B below -- all under Claude
    (138), Space Bunny (137.7), Pixel Canary (137.6), the gpt ladder and hy3
    (134.5). Those are UNCHANGED (Claude has the top AA Index; the other two are
    owner floors with no public board the owner said to keep).
- **The old floors are replaced, not re-indexed.** The kimi-k3 and glm-5.3 floor
  CODE in `_benchmark_score` is gone; `_PREF_FLOORS[1]` (138.1) stays in the tuple
  (other sites read it by index). glm-5.0..5.2 keep `_PREF_FLOORS[7]`.
- **A new unlisted version auto-ranks above its predecessor.** benchmarks matches
  by `modelrank.parse` (family, tier) + explicit param size; a version higher than
  any listed one inherits the newest row + a small version bump (`floor_bump` <=
  0.30), so glm-5.4 > glm-5.3, kimi-k4 > kimi-k3, deepseek-v4.2-flash > v4.1-flash,
  qwen3.9-27b > qwen3.8-27b, all still under the owner floors. Once a board lists
  the new version its own row wins; older versions keep being lowered by modelrank.
- **Size variants never inherit the full-size flagship floor.** A cut naming a
  small param count (27b/32b/8b, `_ev_SMALL_PARAM_B` 70) or `-mini`/`-small`
  (`_ev_small_param_variant`) sits a documented step (`_ev_SIZE_STEP` 1.0) under
  the flagship floor, newest-first; WITH board evidence it is placed by the boards
  (Qwen 3.8 27B ~133.08, below GLM 5.3 Flash and DeepSeek V4.1 Flash). qwen3.8-max
  (not listed) keeps the full qwen floor.
- **Keyless auto-refresh where a source has the field.** General AA is already
  refreshed 6-hourly keylessly from OpenRouter's catalog
  (`_fetch_aa_scores_keyless`). `benchmarks.parse_openrouter_row` is a
  forward-compatible hook that reads AA agentic/coding sub-indexes from the same
  catalog-row shape IF they ever appear -- none are published today (verified from
  the code and the cached response shape: only `intelligence_index` exists), so the
  dated table drives the agentic numbers. The day OpenRouter publishes
  Terminal-Bench / AutomationBench, the hook picks them up with no code change.
- **Visible.** `/api/tracking` rows gain `agentic_evidence` {tb4, automation,
  source, date, inherited_from?} and `general_evidence` {aa, arena,
  inherited_from?}.
- **Caveat -- production vs the pure board order.** The TOOL order above is the
  BOARD order; `_agentic_score` still subtracts the existing learned/dialect
  penalties, so e.g. DeepSeek V4.1 Flash's documented 25-pt tool-dialect penalty
  (`_TOOL_DIALECT_MISMATCH`, malformed tool calls) still demotes it on LIVE tool
  turns. Board evidence sets the order among equally-reliable candidates; it does
  not override a measured failure. The relay discount and sustain penalty are
  untouched (relay Claude stays well below first-party GLM 5.3 on tool turns).

## Apps deploy by themselves (2026-10-10)

Owner: "why can't this conversation deploy the app on the machine? ... help our
hub to deploy apps perfectly next time." Covered by `tests/test_deploy_perfect.py`
(hermetic: temp folders of fake package.json / .env.example / marker files, a
recording `_run_blocking` stub, an injected HTTP probe + clock; no real npm /
pip / docker / network / server).

**DIAGNOSIS of the incident** (Build conversation `2e3525ad`, codex / quality
multi, project `project-20261008-025847` -- a role-based "home repair / Fixli"
marketplace). Read-only, from the hub's own APIs + `~/.free-llm-hub` + the
project folder:
1. It is a **two-part monorepo**: root `package.json` start `node
   backend/server.js` (Express + **Postgres `pg`** + Stripe) AND a SEPARATE
   Vite/React/TS frontend in `frontend/` (Supabase). `workspace.detect()`/
   `install()` handle ONE package.json and ONE `npm install` in the ROOT only --
   the frontend's own install and its **`vite build`** step are never run by the
   hub, so "the app" works only after an agent hand-builds the frontend and
   patches the server to serve it, redone every run.
2. The hub **never started it** -- `workspace.status` was `external:true,
   "started by the agent"`: it only ADOPTED a server the agent hand-started on
   port 3000. hub.log has NO `[preview]`/`install`/`detect`/`adopt` line for the
   project; the install/start pipeline never drove it (only `[swarm] resumed ...`
   and `[publish] ... tunnel ...` lines).
3. The app expects **PostgreSQL** (`createdb home_repair_db` in the README) that
   is not on the machine; the backend degrades gracefully ("Database
   unavailable; continuing"), but the hub had no DB awareness so a "deploy"
   looked unverified and agents kept re-addressing it.
4. **No auto `.env`**: `.env.example` needs JWT/Stripe/SMTP; the hub never seeded
   `.env`, so each run an agent hand-created it.
5. **Circular re-deploy**: nothing persisted a canonical "start `node
   backend/server.js` on 3000, frontend already built" across runs/restarts, so
   after the 2026-10-10 01:42 update restart the adopted server was lost and
   turn 14 (`swarm-e0ef19ed87d1`) was **rebuilding the frontend shell from
   scratch AGAIN** -- the 5th deploy attempt. A killed `backend/
   node_modules.partial-20261010` (a manual `npm install` cut by the restart;
   NOT the hub's -- `workspace._remove_partial` rmtree's, it does not rename) was
   left behind.

**THE FIX (generic, any project, any OS)** -- the hub now does the extra
install/build/env work a multi-part app needs before its single start command,
and surfaces WHY an app is or is not running.

- **`deploy_perfect.py`** (stdlib only, never raises, starts no process;
  `realpath` / `is_link_fn`, the HTTP probe and the clock are injectable):
  - `analyze(project_dir)` -> a plan: `layout` (single | monorepo | workspaces |
    compose), the `server_dir`/`server_kind` to launch, the EXTRA `steps` after
    the root install (a DECLARED sub-project's install with lifecycle scripts
    off, then each unbuilt frontend's build: `{kind, pm, dir, label, timeout}`
    records the caller turns into argv via `pm_args`; the package manager comes
    from the lockfile -- npm, pnpm, yarn, yarn berry, bun), `members`,
    `frontends`, whether the server likely `serves_frontend`,
    an `env` seed directive, a `db` plan (engine from drivers in any
    package.json/requirements + `.env.example`; `required`/`local_ok`; a
    Postgres/MySQL/Mongo note naming `createdb <DB_NAME>` or "point DATABASE_URL
    at a hosted database"; SQLite = no external DB), `notes`, and a one-line
    `summary`.
  - `env_seed_plan(example_text)` -> a `.env` body with SAFE LOCAL defaults:
    keys are tokenised on non-alphanumerics and matched on WHOLE tokens (`\b` is
    no boundary across `_`, and a substring test reads "ses" inside "SESSION"),
    so a random value is generated ONLY for the project's own internal signing
    secrets (jwt/session/cookie/signing/... + a secret noun; bare `SECRET` is
    enough), every EXTERNAL service key (stripe/smtp/github/...) keeps the
    example's obvious placeholder (never fabricated), a bare DB password gets a
    local default, PORT/HOST/localhost coordinates are kept verbatim.
  - `deploy_check(url, probe=...)` -> poll the running preview over HTTP until it
    answers (ANY status line = alive, the same rule a bound port uses) or a
    deadline; returns `{ok, status, url, waited, error}`.
- **`workspace.py` wiring** (reuses the preview's own `_run_blocking` timeouts
  and PID/port kill rules; nothing here starts a server or binds a port):
  - `start()`'s worker calls `_deploy_prepare(run_dir, project_dir, proc)` on
    every explicit start, AFTER the root install (a workspaces build needs the
    root's hoisted packages; a root `node_modules` does NOT mean the frontend is
    built): it seeds `.env` from `.env.example` when absent (exclusive create,
    see the security model below), then runs the plan's declared sub-project
    installs (scripts off, `INSTALL_TIMEOUT`) and the frontend build
    (`BUILD_TIMEOUT`, new = 600 s) via `_run_blocking`; each step's folder is
    re-checked (no link, inside the project) when it runs. A step that runs out
    of its time is reported as the preview's error ("... the hub stopped it --
    press Run to try again") and a half-written `node_modules` it created is
    removed so the next Run retries, mirroring `install()`. A step that merely
    EXITS non-zero returns None (the start that follows reports the problem in
    the project's own words). A no-op for a plain single project.
  - `detect()` disambiguation: a bare conventional pair (`frontend/`+`backend/`,
    `client/`+`server/`, `web/`+`api/`) with no root start script used to 400
    "ambiguous"; it now launches the backend when `deploy_perfect` names a
    genuine server among the runnable candidates (undeclared folders and two
    static sites stay ambiguous -- regression-guarded).
  - `deploy_check(project_dir, probe=None)` -> runs `deploy_perfect.deploy_check`
    against the live `status()['url']` with a stdlib HTTP probe; None when
    nothing runs.
- **`app.py` wiring** (`_dp_`-prefixed, additive, flag-gated, degrades to the old
  behaviour): with `?deploy_check=1`, `_dp_augment_status(project_dir, st,
  report=True)` adds a plain `deploy` block (summary / layout / `on_start` steps /
  notes / db / `last_error`) to the `GET /api/workspace/status` response so the
  Build page shows WHY an app is or is not running instead of a bare "not
  running". REPORT ONLY: no subprocess, no HTTP request, no file write (the
  analysis is cached `_dp_analysis_ttl` = 30 s). Without the parameter the
  response is byte-identical to before; the block is added only when there is
  something to say. No new route (the status route carries it, like
  `?discover=1`).
- **Flag** `deploy_perfect` (`config.get_flag`, default on): off = every hop is
  the old single-package behaviour exactly (no prepare, no detect override, no
  status block).

**Security model** (2026-10-10 review of the first commit; covered by the
link / realpath / O_EXCL / declared-only / no-subprocess tests in
`tests/test_deploy_perfect.py`). The preview runs the project's OWN code BY
DESIGN -- its start command, its root install with that install's scripts, its
frontend build -- and only on an explicit start (the Run button, the turn-end
preview start, the run-end deploy check). What deploy-perfect adds is limited:
- **No links, nothing outside**: every path it reads or writes is refused when
  it is a symlink, a junction / mount point or an app-exec alias
  (`deploy_perfect.is_link`: `S_ISLNK`, `os.path.isjunction`, the reparse TAG --
  cloud-placeholder reparse points are real files) or when its realpath is not
  inside the project's realpath (component-wise, normcase:
  `deploy_perfect.inside`). Reads (`safe_read_text`) take regular files only,
  re-compare the opened file's identity with the path's (a link swapped in
  between check and open is refused) and are capped (`.env.example` 64 KB,
  package.json 512 KB).
- **`.env` is created, never overwritten or written through**:
  `workspace._create_exclusive`. POSIX: `O_CREAT|O_EXCL|O_NOFOLLOW`, mode 0600.
  WINDOWS: `CreateFile(CREATE_NEW, FILE_FLAG_OPEN_REPARSE_POINT)` -- MEASURED on
  this machine, `os.open(O_CREAT|O_EXCL)` through a DANGLING symlink succeeds and
  creates the link's TARGET, so plain O_EXCL is not enough there. Only
  well-formed KEY=VALUE lines are copied (comments, junk, control characters,
  over-long lines dropped; <= 300 keys).
- **Declared sub-projects only**: package.json `workspaces`,
  `pnpm-workspace.yaml` (a line-based read of `packages:`, "dir" and "dir/*"
  only, negations / `**` ignored), or the exact pairs `frontend/`+`backend/`,
  `client/`+`server/`, `web/`+`api/`. Nothing found by scanning; hidden,
  linked, node_modules, vendor, examples, fixtures, tests, docs folders are
  never entered.
- **Sub-installs run with lifecycle scripts OFF** (`npm|pnpm|yarn|bun install
  --ignore-scripts`, yarn berry `--mode=skip-build`). Workspace members are not
  sub-installed at all (the root's install covers them); a pair's backend the
  ROOT already covers is skipped (a scripts-off sub-install would shadow the
  root's properly built native packages, e.g. bcrypt); python sub-projects are
  never installed here (pip runs build code). The root install is unchanged.
- **A status read only reports** (see the app.py wiring above).
Residual, in plain words: a process already running in the project (an agent)
can still swap a folder for a link in the instant between the run-time re-check
and npm starting there; that process could run the same command itself, so the
check closes the "trick the hub" path, not that one.

**Machine probe, run-end deploy check, remembered start** (same day; covered by
`tests/test_deploy_after_run.py`, hermetic: fake which / run / port_open in
Windows, Linux and macOS output shapes, fake spawn / run_turn, a faked
workspace and clock).
- **`envprobe.py`** (stdlib; every side injectable; module switch `ENABLED`,
  which `tests/conftest.py` turns off for every test). `probe()` = `shutil.which`
  + ONE `--version` per tool under 4 s, all on threads (node, npm, pnpm, yarn,
  bun, python -- python3 / python / py, so a Windows Store alias that prints
  "Python was not found" falls through to the launcher --, pip, docker, psql,
  mysql incl. MariaDB's "Distrib" version, redis-server / redis-cli), one
  `docker info` only when the CLI exists (daemon answering or not), loopback
  connects to 5432 / 3306 / 6379 and the common dev ports (never a listen, never
  a kill), `CREATE_NO_WINDOW` on Windows. `snapshot(wait=)` caches it `TTL` (10
  min) and refreshes in the background; a request path never waits.
  `block(snap, who)` = ONE short block: "THIS MACHINE: node 22, npm 10, python
  3.12, docker: no, PostgreSQL: not installed (port 5432 closed), MySQL, Redis: not
  installed; busy ports: 3000." + "The app MUST run here with one command. Prefer
  storage that needs no install (SQLite / a JSON file) unless the user explicitly
  asked for PostgreSQL/MySQL/Redis; if they did and it is not installed, add an
  explicit setup phase (planner; a worker / session: say exactly what the user
  must install), and still provide a working local fallback." `wants_block(text)`
  = a request to BUILD an app (a server, an API, a store, a platform...; EN/FR/
  ES/PT/DE words) -- not a static page, a question or a fix.
- **Where it goes** (flag `planning_env_probe`, default on; `app._dp_env_block`):
  the Multi planner's system prompt (`swarm_windows.plan_system`, which may wait
  up to 8 s for the first probe), every worker's prompt (`_Run.env_block`,
  persisted, refreshed on a resume) and a single session's brief
  (`craft.system_message`, tool-carrying turns, before the loop). The brief stays
  under the cost ceiling BY CONSTRUCTION: `craft.BRIEF_CEILING_CHARS` = the
  0.135 x 32768 x 4 of `test_craft_briefs`, and the block is dropped whenever it
  would cross it (the heaviest bundle, a saas landing page, has ~44 chars of room
  and is not an app build anyway). Unregistered source / flag off / not an app
  build = every prompt byte-identical to before.
- **The run-end deploy check** (flag `deploy_check_after_run`, default on;
  `app._dp_run_deploy_check`, injected into EVERY run entry point --
  conversation start/continue, boot resume, REST, MCP, heartbeat -- through
  `_multi_check_kwargs()` as `deploy_check=`, re-attached on a resume like the
  free verdict). In `swarm_windows._walk`, after the phases and the review, for a
  run whose goal is an app or web build: `_dp_start_and_check` starts the project
  through the preview (`workspace.start`: the deploy-perfect prep + detect, or the
  remembered start), waits up to `_dp_deploy_wait` (120 s) for it to run and for
  an HTTP answer (`workspace._http_probe`: loopback URLs only, no proxy, no
  redirect followed -- a 3xx is an answer), and returns the URL or the exact
  error with the last 15 log lines (sanitized). The run's result (`format_result`,
  i.e. the conversation's reply) ends "Deployed: http://127.0.0.1:<port>" or
  "Deploy failed: <error>" + its last lines; `run.deploy` is persisted (a check a
  restart cut reads "interrupted" and runs again on the next walk) and the Build
  page's helpers panel shows it (`_multi_run_plan` `deploy` ->
  `deployRow`, words not only colour, "Open" only for a loopback URL). On a
  failure with the budget left, ONE phase labelled `deploy_fix` ("Fix the app so
  it starts", the error and log lines as its task, the usual helper rules) is
  queued, then the check runs once more. `run.deploy_fix_used` is persisted:
  never a second fix phase in a run -- not after a restart, not after a
  "continue". `_is_review` skips the trailing fix phase, so the review keeps its
  role.
- **The remembered start** (`workspace.remember_start`, after a check passed):
  `{kind, run_dir (relative), port}` in `state_dir()/preview-starts.json`
  (`workspace.CANON_PATH` in tests), keyed by the project's realpath. `start()`
  uses it first: the command is still DERIVED from the files in that folder
  (`_detect_at`) and only when it yields the same kind; the remembered port is
  asked for when free (`_preferred_port`, never the hub's). A record whose folder
  is gone, became a link, leaves the project or yields another kind is dropped
  (`canonical(..., drop_stale=True)`), and the project is detected as always.
  So a later Run, or the first start after a restart, starts it the same way --
  no relying on an adopted, hand-started server.

**Left out on purpose**: the hub still does not PATCH a server to serve a
separate frontend's build output -- it builds the frontend and, when the server
is a distinct app, emits a clear note ("serve the build output from the API, or
run the frontend dev server separately") rather than editing the project's
source; it provisions no database (a Postgres-only app gets the `createdb`
instruction and the planner is told to use storage that needs no install); a
remembered start is used on an explicit start only (Run, the turn-end preview,
the run-end check) -- nothing is started at boot; and the status route never
checks over HTTP.

## Oversized conversations say how to compact (2026-10-10)

Covered by `tests/test_compact_hint.py` (hermetic: the message is built from
constants + the request's User-Agent; no network). Owner: a Codex conversation
seen at ~504K tokens grew past every model's window, the hub refused it with the
native context-length error, and Codex kept re-sending the SAME oversized turn
instead of compacting. The refusal now TELLS the client how to shrink it.

- **What**: the two refusal paths -- the front door (`_front_door_overflow`) and
  the hop-exhaustion overflow (`_ctx_overflow_reply`) -- both go through the one
  choke point `_native_overflow_reply`, which now appends an actionable sentence
  to the native message: "This conversation is too long for every available model
  (~N tokens; the largest holds M). Type /compact to shrink it, or start a new
  conversation." The protocol's native error SHAPE and CODE are unchanged (OpenAI
  `context_length_exceeded` with `param: messages`; Anthropic message still STARTS
  with `prompt is too long:`; streamed Responses still `response.failed` with
  `error.code` `context_length_exceeded`), so every client still treats it as a
  context error and compacts -- only the human-readable text is made actionable.
- **Which command, per CLI** (`_ch_compact_hint`, `_ch_overflow_client`, map
  `_CH_COMPACT_CMD`): the client is the /agent session's CLI, else the
  User-Agent (`_steer_cli_from_ua`). codex, claude and opencode are each told
  `/compact` -- VERIFIED READ-ONLY from each installed binary's own strings on
  2026-10-10: `codex.exe` (`@openai/codex` win32 vendor bin) prints
  `/compact when the conversation gets long to summ[arize]`; `claude.exe`
  (isolated `@anthropic-ai/claude-code` 2.1.220) prints `/compact now to control
  what gets kept` and `/compact mid-task, /clear when switching...`; `opencode.exe`
  (`opencode-ai` bin) prints `/compact to reduce context`. Any other client
  (qwen/kimi/aider/hermes/openclaw, an SDK UA, or none) gets the generic "Start a
  new conversation or shorten the history." -- no command is named unless it was
  verified.
- **No fake numbers**: the sizes in the sentence are the SAME `orig`/`window`
  already in the reply (formatted with thousands separators, matching the Build
  page's `context_detail`); the hint is a message-string append ONLY. No usage
  field is added or changed, and no token count is altered to steer the client
  (the tests assert every structured field, size numbers included, is identical
  to the no-hint body). `ctxwin.openai_overflow_body` / `anthropic_overflow_body`
  / `responses_overflow_events` gained an optional `hint=""` (empty = byte-
  identical to before).
- **Flag** `context_compact_hint` (`config.get_flag`, default on). Off = the
  message is byte-identical to before (hint `""`, no extra log line). One log
  line per refusal: `[ctx] overflow refusal: cli=<id> compact=<cmd|none> ~N
  tokens, largest window M` (sizes only, no content).
- **Build page**: the browser's "context too long" message
  (`agentic_chat.context_detail`) already ends "Press Continue to compact it or
  start a new conversation." -- the Continue button runs the hub's own
  compaction, so that text is left as the page's equivalent guidance.

## Automatic migrations (2026-10-10)

Owner: every install auto-updates (`git pull` + graceful restart), so a release
that renames/retires a persisted setting must migrate old state BY ITSELF, and
must NEVER drop or corrupt an API key -- including on users' installs that get
the same update. Covered by `tests/test_safe_migrations.py` (hermetic: temp
config via `FREE_LLM_HUB_CONFIG`, fake `enc.v1:` key strings, real atomic I/O).

- **`migrations.py`** (pure stdlib, never imports app; imports `config` lazily so
  there is no cycle -- config never imports it). `apply(raw) -> (new, applied)`
  is pure and IDEMPOTENT and runs on the RAW, encrypted-at-rest config dict: a
  stored key string (ciphertext or legacy plaintext) is MOVED VERBATIM, never
  decrypted, re-encrypted or rewritten (AES-GCM would re-nonce; `encrypt()` is
  idempotent anyway). `LATEST = 3`. Steps are version-gated `(target, id, fn)`;
  `_effective_start` treats a config still carrying the unambiguous pre-v3 key
  `cli_multi_confirm` as `< 3` whatever its stamp says, so a premature
  `schema_version` save between boot and the step cannot skip a pending rename
  (provider presence is NOT such a signal, so a future release may re-add a
  retired name without it being re-retired).
- **The v3 step** (`_to_v3`): (1) `cli_multi_confirm` (bool) ->
  `cli_multi_approval` (str): `false` -> `"off"`, `true`/anything-else ->
  `"dashboard"` (the new default and the hub's safe side; never weaker than the
  owner's prior "require consent"); an existing `cli_multi_approval` is never
  clobbered and the old key is dropped. (2) rows of removed providers
  (`RETIRED_PROVIDERS = tokenrouter, agentrouter`) move from `providers` into a
  top-level `retired_providers` block, keys and all (`retired_at` stamped);
  `retired_providers` is outside the encrypt/decrypt pass (which only touches
  `providers`), so those ciphertext strings round-trip byte-for-byte through
  every ordinary save and `load_config`/`save_config` carry the block
  unchanged.
- **`run_migrations(path=None)`** (the boot step): under `config._LOCK` + the
  cross-process lock, read the raw JSON (no decryption); if `apply` changes
  nothing, write nothing (idempotent -- no backup, no churn). Otherwise take an
  atomic 0600 backup to `state_dir()/backups/config-<UTC>-premigrate.json`
  (byte-for-byte, so `list_backups` sees it) BEFORE the write, run the
  KEY-SAFETY check, then atomically write the new raw dict (0600, fsync,
  replace-with-retry, like `save_config` but WITHOUT its encrypt pass) and
  invalidate the settings cache. A change that would REDUCE the stored keys is
  ABORTED (live config untouched, backup kept). A missing file (fresh install)
  or a corrupt one is left alone. It NEVER raises: any failure leaves the config
  untouched, logs one line, and boot continues (fail-safe).
- **Key safety** is a count + per-key SHA-256 multiset (`key_fingerprint`,
  `_keys_preserved`) over every stored key string across `providers`,
  `retired_providers` and `_unreadable_api_keys` (and a legacy single
  `api_key`). The digest is of the STORED string, so it never exposes a secret
  and is invariant under moving a key between maps -- the one thing a safe
  migration does. Counts and a backup path may be logged; a key value never is.
- **Wiring**: `config.SCHEMA_VERSION` is `3`; `_stamp_schema_version` records it
  as a FLOOR (never downgrades a config written by a newer hub, lifts an older
  one to the baseline) in `load_config` and `_cas_update`. app.py's `__main__`
  calls `migrations.run_migrations()` as its FIRST action, before
  `_recover_interrupted_hub_transition` / `_mark_runtime_started` (so nothing
  stamps the version before pending moves are done) and before
  `_seed_default_blocks` / `encrypt_existing_secrets`. The auto-update path
  reaches this same boot -- no separate code path. `GET /api/version` (already
  control-token gated) gains a `migrations` object
  `{schema_version, applied, last_backup, keys_count}` via `migrations.status()`
  -- NO new route, so the README route count is unchanged.
- **State files are not migrated**: `update-resume.json`, `cli-multi-runs.json`,
  `preview-starts.json`, `cloudflared-auto.json`, `taskboard.json`, the
  benchmarks and the quota/perf ledgers hold no keys and already fail open
  (a missing file = fresh state; an old-shaped one is read defensively), so they
  need nothing here. New boolean flags of recent releases (`deploy_perfect`,
  `cloudflared_auto_install`, `agent_publish`, `graceful_update`, ...) read
  through `get_flag(name, default)`: absent on an old config = their default, so
  they self-migrate and are deliberately not touched.

## Relay quota units (2026-10-10)

OWNER DECISION 2026-10-10: fix the relay penalty MECHANISM. Covered by
`tests/test_relay_sustain_units.py` (hermetic: `quota.status` faked, no network).
Flag `relay_sustain_units` (`config.get_flag`, default ON); OFF = the old penalty
byte for byte. New app.py names are `_rs_*`.

**The bug.** `_sustain_penalty(pid)` demotes a provider whose REQUEST budget is
scarce PER DAY (a 50/day free tier drains in an hour under a coding CLI, so it
must not out-rank a sustainable large provider on agentic ordering). It read
`quota.status(pid)["limit"]` as if it were always per-day, but `quota.FREE_LIMITS`
stores each limit in its OWN window (`minute` / `day` / `month`). A relay listed
as "5 requests per MINUTE" (g4f) was read as "5 per DAY" and demoted `(150-5)/5`
= ~29 points. A units bug: right in effect (g4f's real budget is tiny and
token-based), wrong in mechanism.

**The fix.** `_rs_per_day_requests(limit, window)` converts the limit to a
per-day equivalent FIRST, so like compares with like: per-minute x1440, per-hour
x24, per-day as-is (`_RS_WINDOW_PER_DAY`). An unlisted window (`month`, unknown)
is left as-is -- the conservative choice; no month-window provider currently
lands in the penalty band (cohere's 1000/month stays >= 150 as-is, penalty 0).
When several windows are known the TIGHTEST converted one would bind, but
`quota.status` exposes one window per provider, so a single conversion is
trivially the tightest. The 150/day yardstick and the `/5` divisor are unchanged.

**Who changes** (all per-minute windows; penalty before -> after, flag ON):
g4f 5/min 29.0 -> 0.0, llm7 20/min 26.0 -> 0.0, navy 20/min 26.0 -> 0.0,
nararouter 10/min 28.0 -> 0.0. Everything else is byte-identical: day-window
scarcity is untouched (openrouter 50/day keeps 20.0, siliconflow 100/day keeps
10.0, sambanova 20/day keeps 26.0), abundant day tiers stay 0 (groq 1000/day,
google 200/day, github-models 150/day, ...), `limit: 0` providers stay 0, an
unknown budget (nvidia) stays 0, and cohere's month window stays 0.

**What still keeps a failing/relay pair behind** (proven in the test, unchanged
by this fix): the relay discount (`_RELAY_DISCOUNT["g4f"]` = 4.0 in
`_benchmark_score` -- `_benchmark_score` never used `_sustain_penalty`, so chat
ordering and the discount are byte-identical on/off); `_TOOL_RELAY_MAX_HOPS` (3
relay hops per tool chain); the lead gate (`_may_lead_pool` / `_may_lead_agentic`:
a relay copy of claude-opus-5.5 is NOT in the lead pool while GLM 5.3 -- agentic
evidence -- is alive, even with the relay un-penalised); quota throttles (a
`throttled`/`exhausted` provider is gated by `quota.status`, a layer the scoring
penalty never reads -- g4f's long Retry-After keeps it out at penalty 0,
`_canary_provider_eligible` False); and measured failure
(`_chain_reliability_band` 2 after >= 2 failures, `_reliability_penalty`,
`_pair_rest`, `_recent_hop_fail`). Fail-open everywhere (`quota.status` raising
-> penalty 0, as before).

**Before/after on a fleet shaped like the live one** (g4f claude-opus-5.5 /
gpt-6.1 / claude-sonnet-4-5, llm7, navy, nararouter vs nvidia glm-5.3, kimi-k3,
groq qwen3.8-27b): `_benchmark_score` is identical on/off for every row. The
tool turn still OPENS on nvidia/glm-5.3 (agentic score ~138.85); g4f
claude-opus-5.5's agentic score rises 105.00 -> 134.00 (the 29 points back) but
stays below GLM and out of the lead pool, so relays are only tried sooner in the
FALLBACK chain -- the downside the owner accepted. The chat turn opener is
unchanged (nvidia/kimi-k3, by `_benchmark_score`). The owner accepted that relay
copies (g4f "Claude Opus 5.5", "GPT-6.x", llm7, navy, nararouter) are tried more
often before falling back.
