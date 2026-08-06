# AGENTS.md — free-llm-hub

## Token economy: use the graphify graph before reading files

`app.py` is ~10.5k lines. Do NOT read it wholesale. A graphify knowledge graph
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
- First-content peek is adaptive (`_stream_peek_timeout`): slow/reasoning
  models or >=12K-token requests get 60s (both: 90s) instead of the flat 35s —
  the flat budget was killing HEALTHY slow hops on Codex-sized prompts.
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

Kimi Code scans `.agents/skills/` (directory form `<name>/SKILL.md`). Two
vendored skills live here:

- `i-have-adhd/` (MIT, github.com/ayghri/i-have-adhd) — always-on output
  style; `disable-model-invocation` was dropped on vendoring so it
  auto-applies. Off switch: "stop adhd mode".
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

## Tests

Run with the SYSTEM python (the `.venv` has no pytest):

    python -m pytest tests/ -q

Known pre-existing noise on this machine: ~238 errors from a pytest tmp-dir
`PermissionError` (environment issue, not the code) and one failure in
`tests/test_benchmark_scoring.py::test_gemini_ids_do_not_collide_with_bare_mini_pattern`.
