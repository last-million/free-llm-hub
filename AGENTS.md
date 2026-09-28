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
- Unchanged because the owner set them or the boards disagree: the Claude floor
  (Sonnet 5 trails k3 / glm-5.3 / qwen3.8 on both boards), the gpt-5.x and
  kimi floors, gemini pro-over-flash, and the last-resort tail.

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

## Tests

Run with the SYSTEM python (the `.venv` has no pytest):

    python -m pytest tests/ -q

The full suite is green (3770 passed); a new failure is a real regression.
