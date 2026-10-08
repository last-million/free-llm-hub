# Calvoun Free LLM Hub — one local endpoint for every free LLM provider

**A self-hosted LLM gateway that turns 60 providers' free tiers into a single
endpoint speaking OpenAI, Anthropic, Ollama and Gemini — and wires Claude Code,
Codex, opencode, aider and 9 more CLIs onto them in one click.**

Runs on `127.0.0.1`. No account, no telemetry, no cloud. Your keys stay on your
machine, encrypted at rest.

```
http://127.0.0.1:8787
```

> **License:** source-available under **PolyForm Noncommercial 1.0.0** —
> personal, hobby, research and study use only. You may not sell it, host it as
> a paid service, or use it in a commercial product. See [`LICENSE`](LICENSE).

---

## What it actually does

- **One endpoint, four wire protocols.** OpenAI Chat Completions, Anthropic
  Messages, the Ollama API and Google's Gemini `v1beta` surface, all on the same
  port — plus an MCP server. A tool that speaks any of them just works.
- **Free tiers, pooled and rotated.** 60 providers in the registry; 8 are marked
  paid so orchestrated routing can never pick them. Several need no signup at
  all. A provider whose allowance is spent goes on cooldown rather than being
  retried into the same refusal.
- **It learns which models actually answer.** A reliability and latency ledger
  survives restarts, so routing stops choosing the free model that keeps timing
  out on you.
- **One-click CLI wiring, reversibly.** 13 CLIs are recognised; 9 have a Connect
  button that edits their real config file, keeps a backup, and can put it back.
- **It keeps working when a provider doesn't.** Requests fall through a chain of
  models across providers, the context is compacted per hop so a small-window
  model can still serve a long conversation, and malformed tool calls from weak
  models are repaired rather than dropped.

---

## Install — download, then run one file

**1. Download**

| How | What to do |
| --- | --- |
| **ZIP** (no tools needed) | **[Download free-llm-hub (.zip)](https://github.com/last-million/free-llm-hub/archive/refs/heads/main.zip)**, unzip anywhere |
| **git** | `git clone https://github.com/last-million/free-llm-hub.git` |

**2. Run the file for your system**

| Your system | Run this file | How |
| --- | --- | --- |
| **Windows** | **`run.bat`** | Double-click, or type `run.bat` |
| **Linux / macOS** | **`run.sh`** | `chmod +x run.sh` once, then `./run.sh` |

That is the whole install. The project root holds exactly **one** `.bat` and
**one** `.sh` on purpose, so there is never a question of which to click.

**No Python? It installs it.** The script checks first, and if Python is missing
it installs it silently — winget or the official per-user installer on Windows,
your package manager on Linux and macOS. No administrator rights needed on
Windows. Then it creates a virtualenv, installs the dependencies from
`requirements.txt`, and starts the gateway.

**3. Open `http://127.0.0.1:8787`.** The terminal prints a control token on
first start; paste it into the dashboard once, then add provider keys.

<sub>Do not run `run.sh` on Windows or `run.bat` on Linux — each only does the
right thing on the system it belongs to.</sub>

**Dependencies** (all pinned in `requirements.txt`): Flask, requests, psutil,
Pillow, cryptography, PyYAML, tzdata. Python 3.9+. None is optional —
`cryptography` encrypts your keys at rest, and a missing one has cost an install
its whole keyring before; PyYAML lets Connect/Disconnect edit Hermes'
`config.yaml`; tzdata gives Windows the time-zone table Google's daily quota
resets on (midnight Pacific).

---

## Connect your tools

Every CLI below is detected on the Providers page. The nine with **Connect**
write the tool's own config file for you, with a backup you can restore.

| Tool | Protocol it speaks | Wiring |
| --- | --- | --- |
| **Claude Code** | Anthropic Messages | Connect |
| **Codex** | OpenAI Responses | Connect |
| **opencode** | OpenAI Chat | Connect |
| **aider**, **Kimi**, **Qwen**, **Pi**, **OpenClaw**, **Hermes** | OpenAI Chat | Connect |
| **Gemini CLI** | Gemini `v1beta` | point it at the hub |
| Anything OpenAI-compatible | OpenAI Chat | `OPENAI_BASE_URL=http://127.0.0.1:8787/v1` |
| Anything Ollama-compatible | Ollama API | point it at `http://127.0.0.1:8787` |

```bash
# any OpenAI-compatible tool
export OPENAI_BASE_URL=http://127.0.0.1:8787/v1
export OPENAI_API_KEY=whatever      # the hub's own key, if you set one

# Claude Code
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
```

### Choosing how it answers, from inside your CLI

The hub exposes its routing as **model ids**, so any CLI can pick one with
`/model` or `--model` without needing to understand the hub:

| Family | Ids | What it changes |
| --- | --- | --- |
| **Effort** | `auto`, `best`, `swarm` | how hard to try |
| **Mode** | `coding`, `reasoning`, `context`, `vision`, `fast`, `specialist` … | which kind of model |

Codex gets both as its native two-screen picker (model, then reasoning level). The full, live list of modes — with how many models each one currently has — is on the **Settings** page, and at `GET /api/model-mode`.

**Choosing the orchestrator** — the model that answers — works the same way
everywhere:

```text
/orchestrator kimi-k3        this conversation starts every turn on that model
/orchestrator auto           this conversation: the best model for each task
/orchestrator all kimi-k3    every conversation
/orchestrator reset          this conversation follows the global choice again
/orchestrator                show the current choice and strong models right now
```

Type it in any CLI connected to the hub (opencode lists it as a command after
Connect) or in the Build page's message box; the hub answers it itself, no model
is called. The Build page also has an **Orchestrator** picker per conversation,
and the **Orchestrator model** page sets it for everyone. It is a preference,
not a cage: if the chosen model is busy, failing, too small for the
conversation or cannot call the tools a turn needs, that turn falls back to
normal routing. Simple small turns (a one-line question, a quick lookup) go to a
medium model whichever orchestrator is chosen; real work opens on it.

On **auto**, a conversation opens on the strongest model available right now
(within 2 points of the top score, so equal copies on different providers share
the load), and the fallbacks behind it follow strength too — a proven but
weaker family is never queued ahead of a stronger model. In a **Multi** run,
each new worker opens on a strong model the run's earlier workers have not used
yet (never more than 4 points under the best), so the one that reviews the work
is a different model from the one that wrote it. While it runs, the
conversation shows a **Helpers** panel: each helper's step, model, time and what
it is doing right now, with **Open ↗** to watch any one in its own window. The
**Running now** popup does the same for everything open: each conversation, its
helpers listed under it, and each running preview has **Open ↗** (new window).

**Benchmarks, refreshed daily and free.** Besides Artificial Analysis, the hub
reads the LMArena text leaderboard every 24 hours from LMArena's own public
Hugging Face dataset (no key). A brand-new model none of the hub's tables know
gets its score from its Arena rating instead of starting at the bottom — capped
under the models you ranked yourself — and every model's Arena rating and rank
show in `/api/tracking`; `GET /api/arena` shows the board.

---

## What makes it different

Each of these is a thing the hub does that a plain proxy does not.

**Per-key free-tier accounting.** Gateways meter *spend*. This meters *free
allowance*: several accounts per provider, per-key windows, and a key that is
spent is cooled down instead of hammered. Provider headers are read back to
correct the estimate.

**A reliability ledger that survives restarts.** Laplace-smoothed,
penalty-only, neutral when unknown, decaying over a week. Routing learns which
free models actually answer here, on your keys, rather than trusting a
benchmark.

**Per-hop context compaction.** A 32K-window free model can still serve a long
agentic conversation: the original brief is pinned, the oldest turns go first,
and an oversized single message is trimmed inside itself rather than dropped.

**Tool-call repair.** Weak free models emit malformed tool calls in half a dozen
dialects. Those are parsed back into real `tool_calls` — restricted to the names
the client actually offered — which is what makes them usable inside an agent
loop at all.

**Reversible CLI wiring.** Connect edits a real config file, keeps a per-file
backup and a checksummed snapshot, and refuses to clobber a file you edited
after connecting. Disconnect puts it back.

**Multi-agent work, two ways.** `swarm` and the crews fan one prompt across
several models and synthesise an answer. **Multi sessions** is the other
thing: several *real* agent sessions in parallel, each with its own CLI process
and context window, dispatched as a phased todo list where a phase waits for
what it depends on. On the Build page it is a quality tier next to Normal, Max
and Swarm: pick it and every message you send is split into phases across real
sessions in the project folder, with the report coming back as the reply, in
the same conversation. Drive it from any CLI over MCP, or from a shell:

```bash
python scripts/swarm.py start "build the landing page" --dir ./site --watch
```

The last wave is always a review: one agent that reads what every other phase
produced, opens the actual files, and fixes what does not line up between them.
Runs are written to disk as they go, so a swarm that finished while the hub was
restarting is still there afterwards.

**Conversations that remember.** A running summary, the decisions worth not
re-deciding, and a durable turn count, one small JSON file per conversation.
Reloading the Build page mid-turn shows the running turn from its beginning and
follows it live; a conversation keeps its own effort tier, its own mode, and
its own edits to which models belong to a mode (Settings, scoped to that
conversation, or the *Models...* button beside its mode). It is continuity
memory, not a truth store: a remembered fact is stamped with the project files
it names, and recall marks the ones that have since moved ("gone since",
"changed since", "now on disk") rather than stating the fact as if nothing had.
It survives the restart the context window does not: when compaction drops old
turns, what they established rides back in on the next turn instead of being
gone.

**Skills you control.** Short, specific briefs (web design, landing pages,
e-commerce, engineering, run & deploy, security, SEO, images) are added to the
opening turn of a request that calls for them, in every CLI, the dashboard chat
and `/agent`. Settings -> Skills switches each one off, or adds your own named
skills that apply on your keywords or on every request.

**A health check in one click.** Settings -> Run health check tests every
enabled provider and key, sends real requests through the hub's own routing
(plain and tool-calling, streamed and not), and says what to do: add a key,
replace a dead one, or turn off a provider that refuses outside apps.

**Low-memory mode.** On a machine under 8 GB of RAM or 4 cores (Auto), multi
sessions run 1-2 agents at a time instead of 4; on any machine, one at a time
while free memory is under 1.5 GB. Auto, On or Off in Settings.

**Honest failures.** When no free model can serve an agent turn, the turn
stops and names the providers and why ("nvidia: too slow; dahl:
rate-limited") instead of retrying in silence, and a relay's billing notice is
never passed off as an answer. Settings -> What changed lists every update in
plain words, grouped as New, Fixed and Improved.

**Local-first with a real threat model.** AES-256-GCM at rest, a control token
on every `/api/*`, bound to `127.0.0.1`, and no telemetry of any kind.

---

## How routing works

```
your CLI ──► 127.0.0.1:8787 ──► classify difficulty ──► pick a model
                                        │                     │
                                        │              build a fallback chain
                                        │              (interleaved across providers)
                                        ▼                     │
                              mode / whitelist filter          ▼
                                                        try, hop on failure
                                                     429 · 5xx · empty · timeout
                                                              │
                                                     compact to fit each hop
                                                              │
                                                        repair tool calls
                                                              ▼
                                                     stream back to your CLI
```

1. The request is classified, and a model is chosen by benchmark score, measured
   reliability, and remaining free quota.
2. A chain of alternates is built **interleaved across providers**, so a
   per-account limit is skipped before a per-model one.
3. Each hop is compacted to what that model can actually take.
4. Failures rotate: rate limits, empty 200s, dead models and providers that only
   ever time out are all sidelined with their own cooldowns.
5. What comes back is repaired if it needs it, and streamed on.

---

## Free providers, honestly

The dashboard lists all 61 with signup links. A few need no account at all
(pollinations, aihorde, uncloseai, llm7, kilocode). The well-known ones —
Groq, Cerebras, Google AI Studio, OpenRouter — all have real free tiers with
no card.

**Limits change constantly, and some of the numbers the hub carries are marked
unverified in `quota.py`.** Treat every quota figure as indicative and check the
provider's own page. The dashboard's **Health check** button tells you what your
keys can actually reach right now, which is the only number that matters.

---

## Security

- **Localhost only.** Binds `127.0.0.1`. Do not port-forward it.
- **Keys encrypted at rest.** AES-256-GCM, key in `~/.free-llm-hub/secret.key`.
  `config.json` is `0600` on POSIX.
- **Control token** on every `/api/*`, plus an anti-CSRF header on writes.
- **Optional gateway key** clients must present on `/v1/*`.
- **No telemetry.** No analytics SDK of any kind is present in the source.
- **Honest nuance:** the hub does make outbound calls — to the providers you
  enable, to `artificialanalysis.ai` if you supply your own key for rankings,
  and `git pull` for its own updates.

---

## Endpoints

| Protocol | Endpoints |
| --- | --- |
| **OpenAI** | `/v1/chat/completions`, `/v1/completions`, `/v1/responses`, `/v1/embeddings`, `/v1/images/generations`, `/v1/models` |
| **Anthropic** | `/v1/messages`, `/v1/messages/count_tokens` |
| **Ollama** | `/api/tags`, `/api/show`, `/api/chat`, `/api/generate`, `/api/ps`, `/api/embed`, `/api/version` |
| **Gemini** | `/v1beta/models/<model>:generateContent`, `:streamGenerateContent`, `:countTokens` |
| **MCP** | `/mcp` — `crew_run`, `crew_start`, `crew_result`, `swarm_windows_*` |
| **Dashboard** | `/api/*` (control-token gated) |
| **Probes** | `/health`, `/healthz` (liveness), `/ready`, `/readyz` (readiness, 503 + `reason` when not ready) — no token, loopback only, no version or provider data |

168 routes in total; the dashboard is the documentation for the rest.

---

## How it compares

Framed as *different jobs*, not better and worse.

| | What it is | Where it is stronger | Where this one is |
| --- | --- | --- | --- |
| **LiteLLM** | The default OSS proxy, 100+ providers | Breadth, ecosystem, production deployment | Free-tier accounting, CLI wiring, native Ollama/Gemini serving, compaction |
| **OpenRouter** | Hosted marketplace with a free tier | Zero setup, one bill | Local, self-hosted, multi-account free rotation, no middleman |
| **Cloudflare AI Gateway** | Edge gateway: caching, analytics | Scale, edge | Local-first, free-tier focus, agent workspace |
| **Bifrost** | Very fast Go gateway | Throughput | Free-tier semantics, agent orchestration, tool repair |
| **gpt4free** | Reverse-engineers provider web endpoints | Needs no keys | Different category — this uses *your own keys* on official free tiers |
| **9router** | Free-tier routing for coding CLIs | Same headline idea, larger community | Four protocols, quota ledger, compaction, crews and multi-agent runs |

If you want a production proxy for paid keys, use LiteLLM. This is for getting
the most out of free tiers on your own machine.

---

## Tests

```bash
python -m pytest -q
```

**4474 tests, 278 files.** They are written as evidence: most carry a docstring
recording the measurement or the live failure that produced them.

---

## Links

▶️ **[YouTube — @QuantumSEO-AR](https://www.youtube.com/@QuantumSEO-AR)** ·
🎥 **[Video tutorial (Arabic)](https://www.youtube.com/watch?v=T_HS_Yl77SA)** ·
💬 **[Discord](https://discord.gg/HK9qKNKGY)**

*Made by **Last-Million** from **Calvoun**.*

---

## Disclaimer

Not affiliated with, endorsed by, or sponsored by Anthropic, OpenAI, Google,
Groq, Cerebras, OpenRouter or any other provider named here. All trademarks
belong to their owners. You are responsible for complying with the terms of
every provider whose key you add.

## License

**PolyForm Noncommercial License 1.0.0** — see [`LICENSE`](LICENSE).
Noncommercial use only. For commercial use, contact the author.
