<!-- Part of the vendored last30days skill (upstream v3.18.4, MIT, (c) Matt Van Horn).
     Moved verbatim out of SKILL.md by the Calvoun Free LLM Hub: Security & Permissions: what the skill reads, writes and sends. -->

## Security & Permissions

**What this skill does:**
- Sends search queries to ScrapeCreators API (`api.scrapecreators.com`) for TikTok and Instagram search, and as a Reddit search backup when the free Reddit path returns no items (requires SCRAPECREATORS_API_KEY; empty-only by default — see `LAST30DAYS_REDDIT_SC_MIN_ITEMS` / `LAST30DAYS_REDDIT_BACKEND`)
- Legacy: Sends search queries to OpenAI's Responses API (`api.openai.com`) for Reddit discovery (fallback if no SCRAPECREATORS_API_KEY)
- Sends search queries to X/Twitter via optional user-provided `AUTH_TOKEN`/`CT0` env vars, explicit browser-cookie opt-in (`FROM_BROWSER` or setup consent), xAI's API (`api.x.ai` by default), Xquik's API (`xquik.com` by default), or the official X API v2 via xurl CLI (OAuth2, auto-detected when installed and authenticated)
- Sends search queries to Algolia HN Search API (`hn.algolia.com`) for Hacker News story and comment discovery (free, no auth)
- Sends search queries to Polymarket Gamma API (`gamma-api.polymarket.com`) for prediction market discovery (free, no auth)
- Runs `yt-dlp` locally for YouTube search and transcript extraction (no API key, public data)
- Sends search queries to ScrapeCreators API (`api.scrapecreators.com`) for TikTok and Instagram search, transcript/caption extraction (10,000 free calls, then PAYG)
- Optionally sends search queries to Brave Search API, Parallel AI API, Perplexity API (`api.perplexity.ai`), or OpenRouter API for web search / synthesis
- Fetches public Reddit thread data from `reddit.com` for engagement metrics
- Stores research findings in local SQLite database (watchlist mode only)
- Saves research briefings as .md files to `LAST30DAYS_MEMORY_DIR` (defaults to `~/Documents/Last30Days`)
- Generates a local `index.html`, Atom `feed.xml`, and rendered brief pages from saved research when the user asks for the library feed
- Publishes the library, feed, and referenced briefs to `ht-ml.app` only after explicit opt-in; hosted pages are public by default unless the user chooses password protection
- Provides `--preflight` for a safe human-readable permission summary before research; it does not read browser-cookie values, write files, or run live research

**What this skill does NOT do:**
- Does not post, like, or modify content on any platform
- Does not access browser cookies unless explicitly configured or consented (`FROM_BROWSER`, manual X cookies, or setup with `--allow-browser-cookies`); `--preflight` and `--diagnose` do not read browser-cookie values
- Does not use Codex ChatGPT auth as an OpenAI provider credential
- Does not share API keys between providers
- Does not log, cache, or write API keys to output files
- Endpoint destinations follow configured provider base URLs; `--preflight` reports active and ignored endpoint overrides without printing secrets
- Hacker News and Polymarket sources are always available (no API key, no binary dependency)
- TikTok and Instagram sources require SCRAPECREATORS_API_KEY (10,000 free calls, then PAYG). Reddit uses ScrapeCreators search only as a backup when the free path returns no items (default), unless `LAST30DAYS_REDDIT_SC_MIN_ITEMS` or `LAST30DAYS_REDDIT_BACKEND=scrapecreators` is set.
- Agent hosts invoke the slash-command skill contract; if `--agent` appears in the user's slash-command arguments, treat it as skill-level mode guidance, not a Python CLI flag.

**Bundled scripts:** `scripts/last30days.py` (main research engine), `scripts/lib/` (search, enrichment, rendering modules), `scripts/lib/vendor/bird-search/` (vendored X search client, MIT licensed)

Review scripts before first use to verify behavior.
