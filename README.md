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

### Images / vision

Images work through OpenAI Chat Completions, OpenAI Responses, and Anthropic
Messages. The hub preserves image blocks and restricts the entire fallback chain
to an exact list of vision models verified in the provider registry. Choose a
preferred vision model in **Hub controls**, or leave priority on Auto. The chat
playground can attach PNG, JPEG, WebP, or GIF images (up to 8 images / 8 MiB
decoded data per request). Audio and video are rejected explicitly rather than
being silently dropped.

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
| `GET/POST /api/hub-mode` | Revision-checked bulk CLI connect/disconnect state |
| `GET/POST /api/media` | Verified vision models and persisted priority |
| `GET /api/runtime` | Runtime/draining state and active request count |
| `POST /api/runtime/stop` | Mark an intentional stop and drain before exit |
| `GET/POST /api/*` | Dashboard configuration API (localhost only) |

For non-browser calls to a state-changing `/api/*` endpoint, send
`X-Free-LLM-Hub: dashboard`. The dashboard adds it automatically; the header
prevents a remote website from issuing a browser "simple" request to the local
control plane.

**Control token.** Every `/api/*` route (including read-only ones like the key
"reveal" toggle) also requires `X-Free-LLM-Hub-Token: <token>` once a token has
been generated. The hub generates one on first startup, prints it to the
console the process is running in, and stores it in `~/.free-llm-hub/config.json`
(`0600` on POSIX). The dashboard prompts for it once (a plain browser prompt,
Jupyter-style) and remembers it in that browser's `localStorage`. This exists
because the loopback port itself is not isolated per OS user — Host/Origin
checks alone stop a cross-site browser request, not a different local account
that can also reach `127.0.0.1:<port>`. Scripting against `/api/*` directly?
Read the token from the same config file or the process's startup output.

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
