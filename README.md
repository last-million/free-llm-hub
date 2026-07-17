# Calvoun Free LLM Hub

*Made by **Last-Million** from **Calvoun**.*

▶️ **[Subscribe on YouTube — @QuantumSEO-AR](https://www.youtube.com/@QuantumSEO-AR)** for updates & news.

**A tiny local gateway that gives every CLI on your machine — including Claude Code — access to free LLM providers.**

> **License notice:** This is **not** free/MIT software. It is source-available under the **PolyForm Noncommercial License 1.0.0** — personal, hobby, research and study use only. **You may not sell it, host it as a paid service, or use it in a commercial product/web app.** See [`LICENSE`](LICENSE). For commercial use, contact the author.

Drop this folder anywhere, run one command, open the dashboard, paste a couple of free API keys, and every OpenAI-compatible tool *and* Claude Code can talk to Groq, Cerebras, Google AI Studio, OpenRouter and more — through a single `http://localhost:8787` endpoint with automatic failover between providers.

- **Zero cloud, zero accounts, zero telemetry.** Runs entirely on `127.0.0.1`. Your keys never leave your machine except in your own direct calls to the providers you enable.
- **Two protocols, one port.** Speaks the OpenAI Chat Completions API *and* the Anthropic Messages API (translated on the fly), so both ecosystems just work.
- **Automatic failover.** If a free provider rate-limits (429) or errors out, requests rotate to the next enabled provider that has a comparable free model.
- **Just two dependencies.** Flask + requests. Python 3.9+. Nothing else.

---

## Quickstart (one command)

**Windows**

```bat
run.bat
```

**Linux / macOS**

```bash
./run.sh
```

That's it. The script creates a virtualenv on first run, installs the two dependencies, and starts the gateway. Then open the dashboard:

```
http://127.0.0.1:8787
```

> Want a different port? Set the `PORT` environment variable before launching.

> Running it twice is safe: the launcher refuses to start a second copy on a
> port that's already served, so you can't end up with two hubs fighting over
> the same port. (`HUB_FORCE=1` overrides.)

### Keep it running (optional but recommended)

The hub is a normal foreground process — close the window, log out, or reboot
and it's gone, and any tool pointed at it quietly loses the free fleet. To make
it permanent:

```bat
autostart.bat            REM Windows  — install (no admin needed)
autostart.bat remove     REM          — uninstall
```

```bash
./autostart.sh           # Linux (systemd --user) / macOS (launchd)
./autostart.sh remove
```

It starts at logon **and** re-checks every 5 minutes, so a crash recovers by
itself instead of going unnoticed. Everything is per-user — no root, no admin,
and your keys in `~/.free-llm-hub` stay readable by you alone.

---

## Step 1 — Add free provider keys

Open **http://127.0.0.1:8787** in your browser. For each provider you want:

1. Click **Get a free key** on the provider's card (it links straight to the provider's signup/key page).
2. Paste the key into the card and hit **Save**.
3. Flip the **Enabled** toggle, then hit **Test** — you'll see a live list of the free models your key unlocks.
4. Pick a **Default model** at the top (used whenever a client sends a bare or unknown model name).

Keys are stored locally in `~/.free-llm-hub/config.json` (created with `0600` permissions on Linux/macOS) and are gitignored by design.

---

## Step 2 — Connect your tools

The dashboard's **Connect** panel shows these snippets pre-filled with your live port and gateway key — with copy buttons. Here is the shape:

### Claude Code (Anthropic Messages protocol)

```bash
export ANTHROPIC_BASE_URL=http://localhost:8787
export ANTHROPIC_AUTH_TOKEN=<your-local-gateway-key>
export ANTHROPIC_MODEL=groq/llama-3.3-70b-versatile   # any <provider>/<model> from the dashboard
claude
```

On Windows (PowerShell):

```powershell
$env:ANTHROPIC_BASE_URL = "http://localhost:8787"
$env:ANTHROPIC_AUTH_TOKEN = "<your-local-gateway-key>"
$env:ANTHROPIC_MODEL = "groq/llama-3.3-70b-versatile"
claude
```

Claude Code sends Anthropic Messages API requests; the hub translates them to OpenAI Chat Completions for the free provider and translates the response (including streaming) back into the Anthropic event stream. Claude Code never knows the difference.

### OpenAI-compatible CLIs (aider, opencode, llm, continue, …)

```bash
export OPENAI_BASE_URL=http://localhost:8787/v1
export OPENAI_API_KEY=<your-local-gateway-key>
# then run your tool, e.g.:
aider --model openai/groq/llama-3.3-70b-versatile
```

Model names are `<provider>/<model>` (e.g. `cerebras/llama-4-scout`). A bare model name falls back to the default you set in the dashboard. `GET /v1/models` lists everything currently available.

---

### Hub lifecycle controls

The dashboard's **Hub mode** switch connects or disconnects every installed,
compatible CLI as one revision-checked transaction. Before it writes anything,
the hub stores an exact snapshot under `~/.free-llm-hub/snapshots/`. Switching
off restores only files that still match the hub-managed checksum; a file edited
afterward is reported as a conflict and left untouched. Individual per-CLI
Connect/Disconnect buttons remain available and intentionally return the hub to
`unmanaged` mode.

**Stop hub** drains active inference streams, rejects new `/v1/*` work, records
an intentional-stop marker, then exits. The included systemd, launchd, Startup,
and Scheduled Task installers honor that marker, so an intentional stop is not
mistaken for a crash. Run `run.sh` or `run.bat` manually to clear the marker and
start again.

### Local subscription providers (Claude Code / Codex, opt-in)

If you already pay for Claude Code or ChatGPT/Codex and they're already signed
in on this machine, the dashboard's **Subscriptions** panel can add them as two
extra *virtual* providers (`sub-claude`, `sub-codex`) — a **last-resort** hop
used only when no free provider can take the request, never in place of the
free fleet. Off by default (master switch + a per-provider switch both have to
be turned on), and only usable for non-streaming, one-shot completions (no
tool access, no permission bypass) — never on a streaming request, and never
when that CLI is already pointed back at this hub.

**Isolated install (opt-in, per provider, default off).** Normally a `sub-*`
hop runs the exact same `claude`/`codex` binary and config your own terminal
uses. Flipping a provider's **Isolated** toggle instead gives the hub its own
private copy: **Install isolated copy** runs a real, non-interactive
`npm install -g --prefix ~/.free-llm-hub/isolated-clis/<claude|codex>` for
you and reports success/failure directly — no shell script to run yourself for
that part. What the hub genuinely *cannot* automate is signing that isolated
copy in: both vendors require a human to complete an OAuth/device-code step in
a browser, so after installing, the panel hands you a ready-to-paste command
(with `CLAUDE_CONFIG_DIR`/`CODEX_HOME` pre-set to the isolated config
directory) to run **in your own terminal**. The hub never attempts to drive
that login itself. Once signed in, the isolated copy shares nothing —
config, credentials, session state — with your everyday CLI install.

### Agentic chat (opt-in, full permissions)

This is a **separate feature from the "Local subscription providers" hop
above** — that one is a one-shot, no-tool-access text completion used as a
last resort inside normal chat routing. Agentic chat instead runs your own
**Claude Code** subscription as a **real coding agent** directly against a
project folder you pick: it can read/write/edit files and run shell commands
in that folder, with **no per-action confirmation prompt**, exactly as if you
had typed `claude --dangerously-skip-permissions` yourself in that folder.

**Off by default.** Nothing under `/api/agent/*` will start a session until
you flip it on with `POST /api/agent/settings {"enabled": true}` (or the
dashboard's own toggle) — every session/message route 403s until then. Two
routes are deliberately exempt from that gate: **Stop** and **End session**
always work, even with the master switch off, so a kill switch can still
kill a session that's already running.

**What "full permissions" means, concretely:** every turn is invoked with
`--dangerously-skip-permissions`, so the CLI will not ask before editing a
file or running a command — it just does it, using **your own real Claude
Code subscription** (the same login/billing as your everyday terminal use).
Only enable this against a folder you're comfortable letting an AI modify
unattended, and only after reading what that flag does in Claude Code's own
docs.

**Which CLI(s) this actually supports today:**

| CLI | Supported | Why |
|---|---|---|
| **Claude Code** (`claude`) | ✅ Yes | `-p "<message>" --resume <id> --output-format json --dangerously-skip-permissions` is a documented, confirmed-working invocation shape. |
| **Codex** (`codex`) | ❌ No — refuses cleanly with a 400 | Official docs list only `--last/--all/--image/PROMPT/SESSION_ID` as accepted `codex exec resume` flags (no approval/sandbox override), and open upstream reports (openai/codex #9144, #5322) describe `--dangerously-bypass-approvals-and-sandbox` being silently ignored after `codex exec resume`. Since this feature is fully non-interactive (no human available to click through a silently-reverted approval prompt) and every turn past the first needs resume+bypass together, Codex is scoped out rather than run on an unverified combination. `GET /api/agent/settings` reports this per-CLI so the dashboard can show it honestly instead of claiming both work. |

**Session model:** one agentic session = one `(cli, project_dir)` pair you
pick explicitly when starting it (there is no default folder). Each user
message is exactly one subprocess call — turn 1 has no `--resume` yet; turn 2
onward passes `--resume <native-session-id>` captured from turn 1's JSON
response, so the CLI keeps its own conversational memory across turns.
Sessions live in memory only and do not survive a hub restart.

**Stopping mid-turn:** the Stop button interrupts the in-flight subprocess by
signaling its **entire process tree** (not just the top PID — a Claude Code
turn can spawn Bash/MCP child processes that a plain `kill` on the parent
would orphan), escalating from a soft terminate to a hard kill after a short
grace period if the process doesn't exit on its own.

### Images / vision

Images work through OpenAI Chat Completions, OpenAI Responses, and Anthropic
Messages. The hub preserves image blocks and restricts the entire fallback chain
to an exact list of vision models verified in the provider registry. Choose a
preferred vision model in **Hub controls**, or leave priority on Auto. The chat
playground can attach PNG, JPEG, WebP, or GIF images (up to 8 images / 8 MiB
decoded data per request). Audio and video are rejected explicitly rather than
being silently dropped.

This is about *reading* images (vision input to chat). For *creating* images,
see the next section.

### Image generation

`POST /v1/images/generations` is OpenAI's Images API shape: `prompt`, `model`,
`n`, `size`. Free providers today are Cloudflare Workers AI (FLUX/SDXL family),
ModelScope (Qwen-Image/Z-Image/FLUX — the best free option for legible text
rendered inside the image), and Pollinations (anonymous, no key, FLUX/Turbo).

`response_format` is always answered as `b64_json`, regardless of what's
requested. There is no image hosting in this local-only tool, so a `url` field
would be a lie — a `data:` URI stuffed into `url` breaks real OpenAI SDK
clients that try to fetch it.

Same auto-detect/fallback pattern as chat and vision: `model: "auto"` (or omit
it) picks the best available free image provider and falls through to the next
one on failure; pin `"<provider>/<model-id>"` to force one. Priority is
configurable via `GET/POST /api/images`, which mirrors `/api/media`'s vision-
priority shape (revision-checked, manual or auto).

No new dependency. This endpoint deliberately does not add Pillow/webp
re-encoding — the hub stays "Flask + requests only." Generated bytes pass
through as-is, base64-encoded for the JSON response.

**Paid image providers (opt-in, explicit pin only).** OpenAI (GPT Image),
Google (Gemini image), OpenRouter (Seedream), and Higgsfield are also wired up
for image generation, but every one of their models is registered `free:
false` in the provider registry. That keeps them **out of Auto and out of the
fallback chain entirely** — `model: "auto"` (or a failed free hop) can never
land on a paid model. The only way to use one is to pin it explicitly by
`<provider>/<model-id>` (e.g. `"openai/gpt-image-1.5"`), same as this hub's
existing paid *chat* providers (DeepSeek, Kimi, MiniMax, …): add the key on
its dashboard card, then name the model directly in your request. Higgsfield's
credential is a composite `KEY_ID:KEY_SECRET` pasted into the single key
field. You are billed by that provider directly — the hub does no metering or
markup.

### Daily usage

The dashboard's **Daily usage** panel (and `GET /api/usage?date=YYYY-MM-DD`,
default today, UTC calendar day) shows token consumption per day, broken down
by `<provider>/<model>`: prompt tokens, completion tokens, total, request
count, and `available_days` (which past days have any data, newest first).
History is kept for 90 days and pruned automatically on write.

Counts come straight from the upstream provider's own `usage` object whenever
one is returned — this is the common case for non-streaming calls. **For a
streaming response, most providers don't send token counts inline**, so the
hub falls back to a `chars ÷ 4` estimate for whichever side (prompt and/or
completion) wasn't reported, and marks that row's `estimated_requests`
accordingly. Treat streamed-session numbers as an approximation, not a metered
bill — this is a local convenience tracker, not a billing system, and it never
talks to any provider's own usage/billing API.

## Notable free providers

A few of the providers in the built-in registry (the dashboard shows the full list with signup links and per-provider notes):

| Provider | Free tier (typical) | Notable free models | Get a key |
|---|---|---|---|
| **Groq** | Generous free rate limits per model, no card required | Llama 3.3 70B, Llama 4, Qwen, Whisper | console.groq.com |
| **Cerebras** | ~1M free tokens/day, extremely fast inference | Llama 3.3 70B, Llama 4 Scout, Qwen 3 | cloud.cerebras.ai |
| **Google AI Studio** | Free daily request quota, no card required | Gemini Flash family | aistudio.google.com |
| **OpenRouter** | `:free`-suffixed models, ~50 req/day free (more with a small top-up) | DeepSeek, Llama, Qwen, Mistral free variants | openrouter.ai |

Free tiers change often — the numbers above are indicative. Always check the provider's own pricing/limits page; the dashboard's **Test** button tells you exactly which models your key can use right now.

---

## How it works

```
┌────────────────┐   Anthropic Messages     ┌──────────────────────┐   OpenAI Chat        ┌────────────────┐
│  Claude Code   │ ───────────────────────► │                      │ ───────────────────► │  Groq (free)   │
└────────────────┘   POST /v1/messages      │    Calvoun Free LLM Hub      │                      ├────────────────┤
                                            │  127.0.0.1:8787      │   429/5xx? rotate ─► │ Cerebras (free)│
┌────────────────┐   OpenAI Chat            │                      │                      ├────────────────┤
│ aider/opencode │ ───────────────────────► │  · protocol xlate    │                      │ Google (free)  │
│ llm / continue │   POST /v1/chat/…        │  · key vault (local) │                      ├────────────────┤
└────────────────┘                          │  · failover rotation │                      │ OpenRouter …   │
                                            └──────────┬───────────┘                      └────────────────┘
┌────────────────┐                                     │
│    Browser     │ ◄── dashboard (config UI) ──────────┘
└────────────────┘        GET /
```

1. Your CLI sends a normal OpenAI or Anthropic request to `localhost:8787`.
2. The hub resolves the model (`<provider>/<model>`, or your default), attaches the provider's real key from your local config, and forwards the call.
3. Streaming is passed through (OpenAI clients) or translated chunk-by-chunk into Anthropic SSE events (Claude Code).
4. On rate limits or provider errors, the hub retries against the next enabled provider with a comparable free model.

---

## Security notes

- **Localhost only.** The server binds to `127.0.0.1` — it is not reachable from your network, let alone the internet. Don't put it behind a port-forward.
- **Local gateway key.** You can set a local API key that clients must present on `/v1/*` (as `Authorization: Bearer …` or `x-api-key`). This protects the gateway from other local processes; the dashboard itself stays localhost-open for configuration.
- **Keys at rest.** Provider keys live in `~/.free-llm-hub/config.json`, written with `0600` permissions on POSIX systems. The repo's `.gitignore` excludes every config/secret path — never commit that file.
- **Safety filter.** Models flagged as uncensored/NSFW-oriented are blocked at the gateway regardless of provider.
- **No secrets in code.** The codebase contains zero keys; everything sensitive is runtime config.
- **Safe lifecycle writes.** CLI files and hub state are replaced atomically. Bulk mode uses checksummed snapshots and never overwrites a config changed by the user after connection.
- **Media is not fetched locally.** Remote image URLs are passed to the selected provider; the hub never dereferences them, and local/file URL schemes are rejected.

---

## Endpoints (for the curious)

| Endpoint | Purpose |
|---|---|
| `GET /` | Dashboard UI |
| `GET /v1/models` | OpenAI-shaped list of all available free models |
| `POST /v1/chat/completions` | OpenAI Chat Completions (streaming supported) |
| `POST /v1/messages` | Anthropic Messages API (streaming supported) — Claude Code entry point |
| `POST /v1/images/generations` | OpenAI Images API shape — free text-to-image generation with fallback |
| `GET/POST /api/hub-mode` | Revision-checked bulk CLI connect/disconnect state |
| `GET/POST /api/media` | Verified vision models and persisted priority |
| `GET/POST /api/images` | Image-generation models and persisted priority |
| `GET /api/usage` | Daily token usage, per `<provider>/<model>` (`?date=YYYY-MM-DD`, default today) |
| `GET /api/runtime` | Runtime/draining state and active request count |
| `POST /api/runtime/stop` | Mark an intentional stop and drain before exit |
| `GET/POST /api/subscriptions` | Local subscription (Claude/Codex) provider state + isolated-profile toggle |
| `POST /api/subscriptions/<pid>/install-isolated` | Install an isolated npm copy of that provider's CLI |
| `GET/POST /api/agent/settings` | Agentic chat master on/off + per-CLI support (never gated) |
| `POST /api/agent/sessions` | Start an agentic session: `{"cli": "claude", "project_dir": "..."}` |
| `GET /api/agent/sessions` | List active agentic sessions |
| `GET /api/agent/sessions/<id>` | Status of one agentic session |
| `POST /api/agent/sessions/<id>/message` | Send one turn: `{"text": "..."}` (full tool access, full permissions) |
| `POST /api/agent/sessions/<id>/stop` | Interrupt the in-flight turn (works even with the master flag off) |
| `DELETE /api/agent/sessions/<id>` | Stop (if running) + drop the session entirely (works even with the master flag off) |
| `GET/POST /api/*` | Dashboard configuration API (localhost only) |

For non-browser calls to a state-changing `/api/*` endpoint, send
`X-Free-LLM-Hub: dashboard`. The dashboard adds it automatically; the header
prevents a remote website from issuing a browser "simple" request to the local
control plane.

**Control token.** Every `/api/*` route (including read-only ones like the key
"reveal" toggle) also requires `X-Free-LLM-Hub-Token: <token>` once a token has
been generated. The hub generates one on first startup, prints it to the
console the process is running in, and stores it in `~/.free-llm-hub/config.json`
(`0600` on POSIX). The dashboard page embeds it automatically (same trust
boundary as the CSP nonce), so there is nothing to copy/paste in normal use.
This exists because the loopback port itself is not isolated per OS user —
Host/Origin checks alone stop a cross-site browser request, not a different
local account that can also reach `127.0.0.1:<port>`; that residual case is
accepted for a single-user local dev tool (auto-embedding trades it away for
zero friction — a co-resident OS user who loads the same dashboard URL gets
the token too). Scripting against `/api/*` directly? Read the token from the
same config file or the process's startup output.

---

## Disclaimer

This project is **not affiliated with, endorsed by, or sponsored by** Anthropic, OpenAI, Groq, Cerebras, Google, OpenRouter, or any other provider mentioned. All product names and trademarks belong to their respective owners.

Free tiers are a gift from these providers — **respect each provider's Terms of Service and rate limits**. This tool uses your own personal API keys for your own personal use; it does not circumvent quotas, share keys, or resell access. Free-tier availability, model lists and limits can change or disappear at any time.

## License

**[PolyForm Noncommercial License 1.0.0](LICENSE)** © 2026 Last-Million

This is **not** an MIT / free-for-all project. It is source-available for
**noncommercial use only** — personal, hobby, research, and study. You may
**not** sell it, offer it as a paid or hosted service, or use it to build or
operate a commercial product or web app. For commercial use, contact the
author for a separate license.
