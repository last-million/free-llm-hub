<!-- Part of the vendored last30days skill (upstream v3.18.4, MIT, (c) Matt Van Horn).
     Moved verbatim out of SKILL.md by the Calvoun Free LLM Hub: parse user intent, Step 0.45 query quality, Step 0.5 pre-flight resolution, agent mode. -->

## CRITICAL: Parse User Intent

Before doing anything, parse the user's input for:

1. **TOPIC**: What they want to learn about (e.g., "web app mockups", "Claude Code skills", "image generation")
2. **TARGET TOOL** (if specified): Where they'll use the prompts (e.g., "Nano Banana Pro", "ChatGPT", "Midjourney")
3. **QUERY TYPE**: What kind of research they want:
   - **PROMPTING** - "X prompts", "prompting for X", "X best practices" → User wants to learn techniques and get copy-paste prompts
   - **RECOMMENDATIONS** - "best X", "top X", "what X should I use", "recommended X" → User wants a LIST of specific things
   - **NEWS** - "what's happening with X", "X news", "latest on X" → User wants current events/updates
   - **COMPARISON** - "X vs Y", "X versus Y", "compare X and Y", "X or Y which is better" → User wants a side-by-side comparison
   - **GENERAL** - anything else → User wants broad understanding of the topic

Common patterns:
- `[topic] for [tool]` → "web mockups for Nano Banana Pro" → TOOL IS SPECIFIED
- `[topic] prompts for [tool]` → "UI design prompts for Midjourney" → TOOL IS SPECIFIED
- Just `[topic]` → "iOS design mockups" → TOOL NOT SPECIFIED, that's OK
- "best [topic]" or "top [topic]" → QUERY_TYPE = RECOMMENDATIONS
- "what are the best [topic]" → QUERY_TYPE = RECOMMENDATIONS
- "X vs Y" or "X versus Y" → QUERY_TYPE = COMPARISON, TOPIC_A = X, TOPIC_B = Y (split on ` vs ` or ` versus ` with spaces)

**IMPORTANT: Do NOT ask about target tool before research.**
- If tool is specified in the query, use it
- If tool is NOT specified, run research first, then ask AFTER showing results

**Store these variables:**
- `TOPIC = [extracted topic]`
- `TARGET_TOOL = [extracted tool, or "unknown" if not specified]`
- `QUERY_TYPE = [RECOMMENDATIONS | NEWS | HOW-TO | COMPARISON | GENERAL]`
- `REGISTER = [default | exec | dev | creator | eli5]` from an explicit `--register` argument, otherwise `LAST30DAYS_REGISTER`, otherwise `default`. A legacy `ELI5_MODE=true` config means `eli5` when no register was selected. Register words are controls, not part of TOPIC.
- `TOPIC_A = [first item]` (only if COMPARISON)
- `TOPIC_B = [second item]` (only if COMPARISON)

**Confirm the topic with a branded, truthful message. Build ACTIVE_SOURCES_LIST from the engine's own source diagnostic — do NOT infer availability by checking env vars or `.env`.** The engine resolves credentials at runtime from several places (process environment, `.env`, macOS Keychain, etc.), so a config-file check silently under-reports sources whenever a key is resolved at runtime rather than written literally in `.env`. Run the engine's `--diagnose` and read its result:

```bash
SKILL_DIR="<absolute path of the directory containing the SKILL.md you just Read>"
"${LAST30DAYS_PYTHON}" "${SKILL_DIR}/scripts/last30days.py" --diagnose
```

`--diagnose` prints JSON. `ACTIVE_SOURCES_LIST` is its `available_sources` array — the engine's authoritative source set, computed after credential resolution. Map the tokens to display names: `reddit`→Reddit, `hackernews`→Hacker News, `polymarket`→Polymarket, `github`→GitHub, `digg`→Digg, `x`→X, `youtube`→YouTube, `tiktok`→TikTok, `instagram`→Instagram, `threads`→Threads, `pinterest`→Pinterest, `linkedin`→LinkedIn, `bluesky`→Bluesky, `perplexity`→Perplexity, `grounding`→Web, `jobs`→Jobs, `corpus`→Your files, `dripstack`→DripStack.

- If EXCLUDE_SOURCES is set (comma-separated, case-insensitive): drop any matching source from ACTIVE_SOURCES_LIST before displaying

**Local corpus source:** If the user asks to include their own notes/documents, preserve each supplied directory as a repeatable `--corpus <dir>` engine flag. `LAST30DAYS_CORPUS_DIRS` activates persistent registered directories automatically. Do not WebSearch, upload, quote into a hosted request, or otherwise expose those paths or contents. Corpus retrieval is an offline source lane; its candidates also bypass remote reranker/fun-scoring prompts and use deterministic local scoring. The engine renders matches under the 🔒 **From your files** badge. The normal recency window uses file modification time; add `--corpus-all-time` only when the user explicitly asks to include older files. Corpus evidence is excluded from `--publish-html`, `library feed --publish`, and agent JSON by default. `LAST30DAYS_CORPUS_IN_EXPORT=1` is the explicit agent-JSON privacy opt-in; never enable it on the user's behalf. When a corpus is configured alongside `LAST30DAYS_API_KEY`/`LAST30DAYS_API_BASE`, the engine deliberately bypasses the hosted backend and runs locally.

**Perplexity source:** use it only when the user asks for Perplexity, Deep Research, or paid grounded synthesis, or when `perplexity` is already enabled in `INCLUDE_SOURCES` / `--search`. Direct `PERPLEXITY_API_KEY` supports Sonar synthesis, Search API rows, and async Deep Research. `OPENROUTER_API_KEY` is only a Sonar fallback. Normal runs default to `LAST30DAYS_PERPLEXITY_MODE=sonar`; use `search` for raw ranked web rows, `both` for synthesis plus rows, and `--deep-research` for `sonar-deep-research` with a 600s default wall timeout. A local Deep Research timeout is not a failed API key; inspect the raw artifact's async request id/status and resume by id if needed.

**Reddit backend pin:** Reddit defaults to the free keyless backend. When `SCRAPECREATORS_API_KEY` is available, ScrapeCreators Reddit **search** backfills only if that free path returns **no items** (empty-only — a thin but non-empty free scrape does not spend credits). If the user wants paid coverage on thin free runs, tell them to set `LAST30DAYS_REDDIT_SC_MIN_ITEMS=<N>` (backfill when free yield is below N). If they say public Reddit is shallow, bot-gated, or missing nested comments, tell them they can set `LAST30DAYS_REDDIT_BACKEND=scrapecreators` alongside `SCRAPECREATORS_API_KEY` to make ScrapeCreators primary and keep the free path as fallback. Do not set either automatically for normal runs.

**Doctor health check:** When the user asks for a health check ("is X working?", "why is a source missing?", "what's broken?", "did setup work?"), run `"${LAST30DAYS_PYTHON}" "${SKILL_DIR}/scripts/last30days.py" doctor` (append `--json` for the machine contract) and relay the audit and fix prescriptions. `doctor` renders a **four-state audit** - **WORKING** (verified this run/last run or keyless-always-on), **TURNED ON - UNVERIFIED** (configured/opted-in but no run evidence), **NOT WORKING** (configured but failing, or the last run errored), **COULD BE ON** (available, not yet configured) - one line per source, plus a **CLI-health** block for sources that need a downloaded binary and indented **backup/comment** sub-lanes. Two on-demand modes: `doctor --postmortem` reads the last run's `last-report.json` and reports what actually broke per source (Failed/Partial/Succeeded with fix hints) - reach for it right after a run that returned less than expected; `doctor --probe` runs a **bounded** live test (free HTTP + keyless CLI sources only; credit-gated sources are never probed) to verify WORKING instead of guessing, and the same bounded probe auto-fires on a plain `doctor` when there is no fresh run. Per-source probe deadline is `LAST30DAYS_DOCTOR_PROBE_TIMEOUT` (default 10s). **MANDATORY standing rule.** Before research that depends on login-backed sources (X via cookies, Reddit's ScrapeCreators backfill), consult `doctor --cached --json` — it serves the report cached at `~/.config/last30days/doctor-cache.json` within its TTL (`LAST30DAYS_DOCTOR_TTL` seconds, default 900) for the cost of one file read. Re-run live `doctor` only when the cache is stale or the previous run reported a degraded login-backed source. When X is in ACTIVE_SOURCES_LIST, announce its predicted backend from the report's `sources.x.active_backend` (e.g. "X will use: bird") in the pre-research status line.


Then display (use "and more" if 5+ sources, otherwise list all with Oxford comma):

For GENERAL / NEWS / RECOMMENDATIONS / PROMPTING queries:
```
/last30days - searching {ACTIVE_SOURCES_LIST} for what people are saying about {TOPIC}.
```

For COMPARISON queries:
```
/last30days - comparing {TOPIC_A} vs {TOPIC_B} across {ACTIVE_SOURCES_LIST}.
```

Do NOT show a multi-line "Parsed intent" block with TOPIC=, TARGET_TOOL=, QUERY_TYPE= variables. Do NOT promise a specific time. Do NOT list sources that aren't configured.

Then proceed immediately to Step 0.45.

---

## Step 0.45: Query Quality Pre-Flight (detect keyword-trap topics BEFORE running the engine)

**MANDATORY. Before Step 0.5, diagnose the topic for known failure classes. If the topic is a keyword trap, reframe or ask a clarifying question BEFORE calling the engine. Running the engine on a doomed query burns 5+ minutes and produces junk. Detecting the trap upfront costs one turn.**

Known keyword-trap classes and how to handle each:

**Class 1: Demographic shopping query**
- Pattern: `gift for {age} year old {gender}`, `what to buy for my {relationship}`, `present for {demographic}`, `birthday gift for {age} {gender}`.
- Why it fails: no human on Reddit posts "I bought a 42 year old man a gift." Real posts use relationship + hobbies + budget. The literal phrase is not the vocabulary of the actual discussions. The 2026-04-18 "Birthday gift for 42 year old man" run returned r/todayilearned, r/japannews crime posts, r/LivestreamFail drama - none about gifts.
- Action: **Ask ONE clarifying question upfront**:
  > "Before I research, tell me a bit more - hobbies (cooks / runs / reads / gaming / outdoors / golf / music)? Relationship (husband / dad / friend / boss / brother)? Budget range? A 'gift for a 42 year old man' is a wide net; hobbies + relationship narrow it 10x."
- If the user declines to narrow ("just run it"), reframe to generic-demographic and scope to gift subreddits:
  - Drop the literal age (age 42 reads identically to 41 or 43 in social content; the number causes keyword collisions like Jackie Robinson #42)
  - Rewrite as `gifts for men in their 40s` or `gifts for men who [hobby]`
  - Scope `--subreddits=GiftIdeas,BuyItForLife,AskMen,malefashionadvice,Dads` (plus hobby-specific subs when known)
  - Note in the Resolved block: "Reframed demographic shopping query. Dropping literal age; scoping to gift communities."

**Class 2: Numeric / age keyword trap**
- Pattern: topic contains a specific number that collides with unrelated content (42 = Jackie Robinson + Hitchhiker's + a 42" quilt; 40 = 40th anniversary posts; 50 = state-count posts; 100 = bench-press posts).
- Why it fails: the number dominates retrieval and pulls in unrelated content. A search that prominently features "42" returns jersey-number posts; a search for "the 100" returns TV-show posts.
- Action: Strip the number from the engine search query unless changing or removing it would change the topic itself (e.g., "GPT-4" yes, "40 year old man" no, "Area 51" yes, "top 10 foods" no). Keep the number in the user's original framing for context; drop it from the engine query. Document in Resolved: "Dropping '{number}' from the search query - it is a keyword trap that pulls in unrelated content. Search will cover the concept generically."

**Class 3: Overly-literal concept phrase**
- Pattern: `how to use X`, `what is Y`, `tutorial for Z`, `explain A` — tutorial-shaped phrasing where social posts are in different vocabulary.
- Why it fails: social posts about Docker do not say "how to use Docker"; they say "my Docker setup", "nginx in Docker", "my dev loop", "tip for folks using Docker Compose". Tutorial phrasing matches blog titles, not social discussions.
- Action: Reframe from tutorial phrasing to discussion phrasing: "how to use Docker" becomes "Docker tips tricks workflows" or "Docker production setups". Document the reframe in the Resolved block.

**Class 4: Generic single-noun common word**
- Pattern: topic is a single common noun with no specific hook (`bread`, `sneakers`, `coffee`, `shoes`, `headphones`).
- Why it fails: single-noun queries have no anchor — the corpus is infinite and the signal is noise.
- Action: Ask for specificity before running:
  > "{TOPIC} is a huge category - are you asking about {specific-facet-A}, {specific-facet-B}, or {specific-facet-C}? Each is a different community. Pick one or tell me the angle."

**Class 5: Non-English / non-Latin-script topic (Hebrew, Arabic, Chinese, Japanese, etc.)**
- Pattern: topic contains non-Latin characters (Hebrew [\u0590-\u05FF], Arabic [\u0600-\u06FF], CJK [\u4E00-\u9FFF], etc.).
- Why it fails without intervention: Reddit, HackerNews, GitHub, and Polymarket are English-dominant platforms. A Hebrew brand like "קפה עלית" scores zero entity-matches across all four sources and returns only English-language noise as fallback padding.
- Action: **Mandatory pre-flight steps for non-English topics:**
  1. **Force `--web-backend brave`** in the engine command. Brave indexes non-English web (Ynet/Walla/Mako for Hebrew; Haber7/Hurriyet for Turkish; etc.) and is the only available source with real-language coverage.
  2. **Skip `--subreddits` targeting unless the topic has a known English-speaking community.** Generic subreddits (r/food, r/Israel) return English noise; omit them or scope tightly to known bilingual communities.
  3. **Note in the Resolved block:** "Non-English topic detected ([language]). Routing to `--web-backend brave`; Reddit/HN/GitHub will likely return zero on-topic results."
  4. **X/Twitter and YouTube are the highest-value missing sources for non-English topics.** Surface this clearly in the output so the user knows what would unlock deeper coverage.
- Do NOT skip this class check for mixed-script queries (e.g. "קפה עלית Elite Coffee") - if any non-Latin characters are present, Class 5 applies.

**Pre-Flight decision flow (do this BEFORE any WebSearch):**
1. Read the topic. Match against Classes 1-5 above.
2. If the topic matches a class, ALWAYS emit a visible pre-flight note before the Resolved block:
   - `Pre-Flight: topic matches {Class N} ({class name}). {Action: clarifying question / reframe / specificity ask}.`
3. If the action is a clarifying question, STOP after emitting it. Wait for the user response before any engine work.
4. If the topic does NOT match any class, emit a one-liner: `Pre-Flight: topic is a {named-entity / comparison / concept} - proceeding to Step 0.5.` Then proceed.

**One-turn gate rule:** do NOT run the engine on a keyword-trap topic without either (a) explicit user confirmation to "just run it anyway", or (b) a concrete reframed query. Burning 5 minutes on a doomed run is worse than a one-turn clarifying question.

**When the user provides context inline:** if a Class 1 query already contains hobbies/relationship/budget ("gift for my cooking-obsessed husband, $200"), SKIP the clarifying question and go straight to the reframe + scope action. The clarifying question exists to fill in the gaps; if the gaps are already filled, move on.

---

## Step 0.5: Pre-Flight Resolution (handles, repos, communities)

**Pre-Flight Checklist — do NOT stop after the first flag. Every applicable flag below is MANDATORY for its topic class.**

Before running the engine, determine which flags apply to this topic and resolve them. Reading only the "X handle" subsection and stopping there is the named failure mode of the Peter Steinberger disaster #2 (2026-04-18). The model admitted on debug: "I treated the 'X handle resolution' section as the full contract for pre-flight resolution and didn't --help the script to see what else existed." The checklist below IS the full contract.

| Flag | Resolved in | Applies when |
|------|-------------|--------------|
| `--x-handle={handle}` | Step 0.5 (Section A below) | Topic is a person, brand, product, or creator with an X presence |
| `--x-related={h1,h2,...}` | Step 0.5 (Section A below) | Topic has associated entities (founders, commentators, spouse, collaborators, media handles) |
| `--github-user={user}` | Step 0.5b | Topic is a person who ships code (developer, engineer, CEO-who-codes, researcher) |
| `--github-repo={owner/repo}` | Step 0.5c | Topic is a product / project / open-source tool |
| `--trustpilot-domain={domain}` | Step 0.5d | Topic is a company / brand / service with a Trustpilot presence (passing the flag also auto-activates the opt-in Trustpilot source for this run) |
| `--subreddits={sub1,sub2,...}` | Step 0.55 | Always — almost every topic has active Reddit communities |
| `--tiktok-hashtags={h1,h2,...}` | Step 0.55 | Always — inferred from topic |
| `--tiktok-creators={c1,c2,...}` | Step 0.55 | Creator / influencer / brand topics |
| `--ig-creators={c1,c2,...}` | Step 0.55 | Creator / brand topics |
| `--web-backend brave` | Step 0.45 Class 5 | **MANDATORY** for non-Latin-script topics (Hebrew, Arabic, CJK, etc.) — Brave is the only source that indexes non-English web |
| `--auto-resolve` | Fallback | WebSearch is available but Step 0.55 could not resolve everything cleanly — use as belt-and-suspenders |

**Checkpoint before running the engine:** your Bash command must include every flag from the checklist that applies to this topic. For a person who ships code (the Peter Steinberger class), that is MINIMUM `--x-handle` AND `--github-user` AND `--subreddits`, and typically `--x-related` too. A command with only `--x-handle` on a person topic is a pre-flight skip and a Step 0.5 regression.

---

### Section A: Resolve X Handles (if topic could have X accounts)

If TOPIC looks like it could have its own X/Twitter account - **people, creators, brands, products, tools, companies, communities** (e.g., "Dor Brothers", "Jason Calacanis", "Nano Banana Pro", "Seedance", "Midjourney"), do WebSearches to find handles in three categories:

**1. Primary handle** (the entity itself):
```
WebSearch("{TOPIC} X twitter handle site:x.com")
```

**2. Company/organization handle OR founder/creator handle** -- This mapping is bidirectional:
- If the topic is a **PERSON**, resolve their company's X handle. A CEO's story is inseparable from their company's story.
- If the topic is a **PRODUCT or COMPANY**, resolve the founder/creator's personal X handle. The creator's personal account often has the most candid, high-signal content.
```
WebSearch("{TOPIC} company CEO of site:x.com")
```
OR for products:
```
WebSearch("{TOPIC} creator founder X twitter site:x.com")
```
Examples: Sam Altman -> @OpenAI, Dario Amodei -> @AnthropicAI, OpenClaw -> @steipete (Peter Steinberger), Paperclip -> @dotta, Claude Code -> @alexalbert__.

**3. 1-2 related handles** -- People/entities closely associated with the topic (spouse, collaborator, band member), PLUS 1-2 prominent commentator/media handles that regularly cover this topic:
```
WebSearch("{RELATED_PERSON_OR_ENTITY} X twitter handle site:x.com")
```
For a music artist, find music commentary accounts (e.g., @PopBase, @HotFreestyle, @DailyRapFacts).
For a tech CEO, find tech media accounts (e.g., @TechCrunch, @TheInformation).
For a product, find reviewer accounts in that category.

From the results, extract their X/Twitter handles. Look for:
- **Verified profile URLs** like `x.com/{handle}` or `twitter.com/{handle}`
- Mentions like "@handle" in bios, articles, or social profiles
- "Follow @handle on X" patterns

**Verify accounts are real, not parody/fan accounts.** Check for:
- Verified/blue checkmark in the search results
- Official website linking to the X account
- Consistent naming (e.g., @thedorbrothers for "The Dor Brothers", not @DorBrosFan)
- If results only show fan/parody/news accounts (not the entity's own account), skip - the entity may not have an X presence

Pass handles to the CLI:
- Primary: `--x-handle={handle}` (without @)
- Related: `--x-related={handle1},{handle2},{company_handle},{commentator_handles}` (comma-separated, without @)

Example for "Kanye West":
- Primary: `--x-handle=kanyewest`
- Related: `--x-related=travisscott,PopBase,HotFreestyle`

Example for "Sam Altman":
- Primary: `--x-handle=sama`
- Related: `--x-related=OpenAI,TechCrunch`

Related handles are searched with lower weight (0.3) so they appear in results but don't dominate over the primary entity's content.

**Note about @grok:** Grok is Elon's AI on X (xAI). It often appears in search results with thoughtful, accurate analysis. When citing @grok in your synthesis, frame it as "per Grok's AI analysis of [article/topic]" rather than treating it as an independent human commentator.

**Skip this step if:**
- TOPIC is clearly a generic concept, not an entity (e.g., "best rap songs 2026", "how to use Docker", "AI ethics debate")
- TOPIC already contains @ (user provided the handle directly)
- Using `--quick` depth
- WebSearch shows no official X account exists for this entity

Store: `RESOLVED_HANDLE = {handle or empty}`, `RESOLVED_RELATED = {comma-separated handles or empty}`

### Step 0.5b: Resolve GitHub Username (if topic is a person) — MANDATORY FOR PERSON TOPICS

**MANDATORY when the topic is a person (developer, creator, CEO, founder, engineer, researcher) and WebSearch is available.** Resolving the X handle but NOT the GitHub handle is the documented Peter Steinberger failure mode (2026-04-18). Without `--github-user={handle}`, GitHub search becomes a keyword match across all of GitHub instead of person-mode scoped to `user:{handle}`. The result is typically 5-10 thin unrelated items instead of the person's actual commits, PRs, releases, and top-starred repos. Treat this as a peer step to Step 0.5 (X handle resolution), not an afterthought.

Do the WebSearch:

```
WebSearch("{TOPIC} github profile site:github.com")
```

From the results, extract their GitHub username from URLs like `github.com/{username}`.

**Verify the account is correct:** Check that the profile description or pinned repos match the person you're researching. Common names may return multiple profiles.

Pass to the CLI: `--github-user={username}` (without @)

Worked examples:
- For "Peter Steinberger", a WebSearch for `Peter Steinberger github profile site:github.com` returns @steipete. Pass `--github-user=steipete`.
- For "Matt Van Horn": `--github-user=mvanhorn`
- For "Garry Tan": `--github-user=garrytan`

**Person-mode GitHub tells a different story than keyword search.** Instead of "who mentioned this person in an issue body," it answers: "What are they shipping? Where are they getting merged? What do their own projects look like?" The engine fetches PR velocity, top repos with star counts, release notes, and README summaries.

**Skip this step if:**
- TOPIC is clearly NOT a person (products, concepts, events)
- TOPIC already has `--github-user` specified by the user
- Using `--quick` depth
- WebSearch shows no GitHub profile for this person (report "no GitHub handle found for this person" and proceed without `--github-user` rather than fabricating one)

Store: `RESOLVED_GITHUB_USER = {username or empty}`

**Checkpoint for person topics:** by the time you reach the Research Execution command, for a person topic you MUST have BOTH `RESOLVED_HANDLE` (from Step 0.5) AND `RESOLVED_GITHUB_USER` (from this step) OR an explicit "no X account" / "no GitHub profile" note. The Bash command that follows must include BOTH `--x-handle={handle}` AND `--github-user={handle}` when resolved. A person-topic run that shows only one of the two is a Step 0.5b regression.

### Step 0.5c: Resolve GitHub Repos (if topic is a product/project)

If TOPIC looks like a product, tool, or open source project (not a person), resolve its GitHub repo for project-mode search:

```
WebSearch("{TOPIC} github repo site:github.com")
```

From the results, extract `owner/repo` from URLs like `github.com/{owner}/{repo}`.

Pass to the CLI: `--github-repo={owner/repo}`

For comparisons ("X vs Y"), resolve repos for both topics: `--github-repo={repo_a},{repo_b}`

Example for "OpenClaw": `--github-repo=openclaw/openclaw`
Example for "OpenClaw vs Paperclip": `--github-repo=openclaw/openclaw,paperclipai/paperclip`

Project-mode GitHub fetches live star counts, README snippets, latest releases, and top issues directly from the API. This is always more accurate than blog posts or YouTube videos citing weeks-old numbers.

**Skip this step if:**
- TOPIC is a person (use `--github-user` instead)
- TOPIC has no GitHub presence (not a software project)
- WebSearch shows no GitHub repo for this topic

Store: `RESOLVED_GITHUB_REPOS = {comma-separated owner/repo or empty}`

### Step 0.5d: Resolve Trustpilot Domain (if topic is a company/brand)

When TOPIC is a company, brand, or service and you want Trustpilot review evidence, resolve its Trustpilot review-page domain. Trustpilot pages are keyed by domain (`www.thriftbooks.com`), not company name — a bare name 404s. Passing `--trustpilot-domain` (or a per-entity `trustpilot_domain` in `--competitors-plan`) auto-activates the opt-in Trustpilot source for that run — you do not also need `INCLUDE_SOURCES=trustpilot`.

**You usually already have it.** Step 0.55 item 6 (first-party positioning) fetches the official site — capture the bare hostname while you're there. When positioning wasn't fetched, one lookup covers it:

```
WebSearch("{TOPIC} official site")
```

Pass to the CLI: `--trustpilot-domain={domain}` (e.g., `--trustpilot-domain=www.thriftbooks.com`)

The flag is used verbatim, bypasses the engine's brand-shape gate, and auto-activates Trustpilot for the run, so it also unlocks Trustpilot for multi-word company names ("Stanley Steemer carpet cleaning"). For comparisons, put a per-entity `trustpilot_domain` in each PEER entity's `--competitors-plan` entry; the MAIN topic's domain must ride the outer `--trustpilot-domain` flag (the engine does not read a main-topic entry out of the plan).

**A miss is not fatal.** When the flag is absent, the engine resolves name → domain itself via the CLI's search **only when Trustpilot is already active** (`INCLUDE_SOURCES=trustpilot` or `--search` includes it); headless `--auto-resolve` fills a hint the engine verifies, but that hint alone does not activate the source. Resolve the flag when the domain is already in hand or the company name is ambiguous (lookalike or same-named companies) — an explicit domain is the only way to guarantee the right company *and* turn the source on.

**Skip this step if:**
- TOPIC is a person, event, or abstract concept (no company reviews to fetch)
- You intentionally want Trustpilot off for this run (`EXCLUDE_SOURCES=trustpilot`)

Store: `RESOLVED_TRUSTPILOT_DOMAIN = {domain or empty}`

---

## Agent Mode (--agent flag)

If `--agent` appears in ARGUMENTS (e.g., `/last30days plaud granola --agent`):

1. **Skip** the intro display block ("I'll research X across Reddit...")
2. **Skip** any `AskUserQuestion` calls - use `TARGET_TOOL = "unknown"` if not specified
3. **Run** the research script and WebSearch exactly as normal
4. **Skip** the "WAIT FOR USER RESPONSE" pause
5. **Skip** the follow-up invitation ("I'm now an expert on X...")
6. **Output** the complete research report and stop - do not wait for further input

Agent mode saves raw research data to `LAST30DAYS_MEMORY_DIR` (defaults to `~/Documents/Last30Days`) automatically via `--save-dir` (handled by the script, no extra tool calls). Use `--output <file>` only when a caller needs the rendered stdout artifact at an exact path, with the format controlled by `--emit`.

**Machine-readable JSON exception:** If the user explicitly asks for structured JSON for an agent, script, or workflow, replace the normal `--emit=compact` engine invocation with `--emit=json` and pass the engine's stdout through verbatim instead of synthesizing the report format below. The default `--json-profile=agent` is the stable, versioned flat contract; use `--json-profile=raw` only when the user explicitly requests the full internal `Report` dump. `--preflight --emit=json` is a separate permission-preflight contract and is not affected by `--json-profile`. Full field documentation and the versioning policy live in `docs/reference/json-export.md` in the repository.

Agent mode report format:

```
## Research Report: {TOPIC}
Generated: {date} | Sources: Reddit, X, Bluesky, YouTube, TikTok, HN, Polymarket, Web

### Key Findings
[3-5 bullet points, highest-signal insights with citations]

### What I learned
{The full "What I learned" synthesis from normal output}

### Stats
{The standard stats block}
```

---
