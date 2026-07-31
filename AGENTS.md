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
  Scheduled Task, installed by `autostart.bat`) call it as
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

## Tests

Run with the SYSTEM python (the `.venv` has no pytest):

    python -m pytest tests/ -q

Known pre-existing noise on this machine: ~238 errors from a pytest tmp-dir
`PermissionError` (environment issue, not the code) and one failure in
`tests/test_benchmark_scoring.py::test_gemini_ids_do_not_collide_with_bare_mini_pattern`.
