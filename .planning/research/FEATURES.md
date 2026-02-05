# Feature Research

**Domain:** MCP data-fetching tool — games market research
**Researched:** 2026-02-05
**Confidence:** MEDIUM-HIGH (MCP patterns verified via official docs and multiple sources; game market research features verified via tool ecosystem survey; triangulation specifics are domain-inference from PROJECT.md)

---

## Feature Landscape

### Table Stakes (Users Expect These)

Features that make or break an MCP data tool. Missing any of these and the tool either crashes, returns garbage, or floods the model's context window into uselessness.

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| Typed tool schemas with defined input/output | MCP protocol requires it; without it the LLM cannot call the tool correctly | LOW | Use `zod` for schema validation in TypeScript. Every tool input and output must be typed. This is non-negotiable at the protocol level. |
| Structured error responses | LLM needs to understand failures to retry or surface them. Raw stack traces are useless to a model. | LOW | Return `{ isError: true, message: "..." }`. Classify errors: validation (400), not-found (404), rate-limit (429), upstream-failure (502). Do not crash the server on external API failures. |
| Rate limit handling and backoff | Steam API enforces 200 req/5 min. SteamDB will block aggressive scrapers. Without this, the tool breaks within minutes of real use. | MEDIUM | Implement per-source rate limiting with exponential backoff + jitter. The tool must wait and retry, not fail and blame the user. |
| Input validation | Malformed inputs (bad AppID, invalid tag string) must be caught before hitting external APIs. | LOW | Validate at the tool boundary using zod schemas. Reject early with a clear message, never let garbage propagate to Steam or SteamDB. |
| Fetch games by tag/genre | This is the primary query pattern for genre analysis. Without it, the entire research workflow has no entry point. | MEDIUM | Steam API supports tag-based search. This is the foundational tool — everything else builds on the AppID list it returns. |
| Return structured typed data, not raw HTML | The LLM consumes this data for analysis. Raw HTML wastes context window and is unparseable for reasoning. | LOW | Every scraper/fetcher must parse and return clean structured objects. This is the contract between the tool and the model. |
| Logging to stderr only | MCP protocol requirement. stdout is reserved for JSON-RPC messages. Mixing them breaks the transport. | LOW | Route all debug/info/error logs to stderr. This is a protocol constraint, not a design choice. |
| Expose the three data layers distinctly | PROJECT.md defines Metadata, Commercial, Engagement as separate concerns with separate sources. Collapsing them into one blob makes the data uninterpretable. | MEDIUM | Tools should surface layer boundaries explicitly. A consumer (the LLM) needs to know which data came from which source and what confidence level it carries. |

### Differentiators (Competitive Advantage)

Features that make this tool genuinely useful for the specific research workflow, beyond what a generic MCP data fetcher provides.

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| TTL-based caching per data type | Game metadata (tags, name, developer) changes rarely. Pricing and CCU change daily. Caching with type-appropriate TTLs avoids redundant API calls AND keeps commercial/engagement data fresh. Critical for a personal tool where you run the same genre query multiple times in a session. | MEDIUM | Metadata TTL: 24-72h. Commercial (pricing, revenue estimates): 6-12h. Engagement (CCU): 1-4h. Cache locally on disk or in-memory. Check cache before hitting external sources. |
| Multi-source triangulation | The core value proposition from PROJECT.md: revenue estimates from SteamDB alone are approximations. Cross-referencing with Gamalytic improves confidence. This is what separates a research tool from a data dump. | HIGH | This requires fetching from multiple sources for the same AppID, comparing results, and surfacing a confidence-weighted estimate. Not just data fetching — it is data synthesis. |
| Concurrent fetching from independent sources | When querying Metadata + Commercial + Engagement for the same game set, these can run in parallel. Serial fetching multiplies latency by 3x for no reason. | MEDIUM | Use Promise.all or equivalent for independent source fetches. Only serialize when there is an actual dependency (e.g., need AppIDs from Metadata before querying Commercial). |
| Report-generation tooling | The end product is not raw data — it is a structured report (Executive Summary, Market Structure, Opportunity Matrix, etc.). A tool that only dumps data forces the user to manually assemble the report. | HIGH | Expose a report-generation tool that takes structured data as input and returns a report skeleton. The LLM fills in analysis; the tool provides the structure and data formatting. |
| Dependency-aware fetch ordering | Metadata (AppIDs) must resolve before Commercial or Engagement can run. The tool should encode this dependency so the LLM does not have to orchestrate fetch order manually. | MEDIUM | Either expose a single high-level "research genre" tool that handles ordering internally, or document the dependency clearly so the LLM can chain correctly. The former is better UX per MCP best practices (high-level tools, not micro-endpoints). |
| Scrape health signaling | SteamDB scraping is fragile — selectors break, rate limits trigger, pages change. The tool should tell the LLM when a scrape degraded so it can fall back or flag the data as low-confidence. | LOW | Return a `confidence` or `freshness` field alongside scraped data. "This came from SteamDB scrape, last successful: 2h ago, selectors may be stale." |

### Anti-Features (Commonly Requested, Often Problematic)

Things that look useful on paper but create disproportionate complexity or directly contradict the project constraints.

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| One MCP tool per API endpoint (micro-tool explosion) | Feels thorough. Maps 1:1 to the underlying APIs. | Floods the model's context window with tool definitions. A model with 50+ tools performs worse, not better — it wastes tokens on tool catalogs and makes worse selection decisions. This is a documented MCP anti-pattern. | Group related operations into 3-5 high-level tools: `search_genre`, `fetch_commercial_data`, `fetch_engagement_data`, `generate_report`. Let the tool handle internal API calls. |
| Real-time dashboards / live monitoring | Feels like a "pro" feature. | PROJECT.md explicitly out of scope. This is a batch research tool. Real-time monitoring requires WebSocket infrastructure, persistent connections, and frontend rendering — none of which exist in an MCP-to-Claude workflow. | Use caching with short TTLs for time-sensitive data (CCU). Run research sessions when you need fresh data, not continuously. |
| Web UI | Feels like it makes the tool "complete." | The interface IS Claude/Cursor. Building a web UI is a separate product. It adds frontend framework, routing, auth, hosting — none of which serve the research workflow. | The MCP tool returns structured data and report skeletons. Claude renders them as markdown in conversation. That IS the UI. |
| Multi-user support | Feels like good engineering practice. | This is explicitly a personal research tool. Multi-user adds auth, permissions, data isolation, billing, and operational complexity that is 10x the core tool effort. | Build for single user. If shared research becomes a need later, the data layer can be networked — but that is a v2 decision, not a v0 concern. |
| Reddit/Twitch/YouTube social sentiment integration | Rounds out the "full picture" of a game's market. | These sources add 3 separate scraping surfaces, each with their own fragility, rate limits, and data models. Per PROJECT.md, defer until core market data works. Social sentiment is a Phase 4+ concern. | Start with Steam ecosystem data. The three data layers (Metadata, Commercial, Engagement) already cover market sizing, revenue, and player interest. Social data is a differentiator layer, not table stakes. |
| Automatic report writing (LLM does all analysis) | Seems like full automation. | The tool should provide data and structure. Having the MCP tool itself call an LLM to write the report creates a recursive dependency (tool calls model calls tool) and loses the user's ability to guide analysis interactively. | Return structured data + report skeleton. Let Claude (the client) do the analysis and writing in-conversation. This is the MCP sampling pattern done right: the tool provides data, the model provides reasoning. |

---

## Feature Dependencies

```
[search_genre] (Steam API: games by tag)
    └──requires──> returns AppID list
                       └──requires──> [fetch_commercial_data] (SteamDB + Gamalytic)
                       └──requires──> [fetch_engagement_data] (SteamDB: CCU, reviews)
                       └──requires──> [fetch_metadata] (Steam API: full game details)

[fetch_commercial_data] ──triangulates with──> [fetch_engagement_data]
    (Revenue estimates improve when cross-referenced with engagement signals)

[fetch_commercial_data] + [fetch_engagement_data] + [fetch_metadata]
    └──feeds──> [generate_report] (assembles structured report from all layers)

[TTL-based caching] ──enhances──> [search_genre], [fetch_commercial_data], [fetch_engagement_data]
    (Reduces redundant calls; each data type has its own TTL)

[scrape_health_signaling] ──enhances──> [fetch_commercial_data]
    (SteamDB scraping is the fragile link; health signals prevent silent data degradation)

[structured error responses] ──required by──> ALL tools
    (Protocol-level requirement; no tool is exempt)
```

### Dependency Notes

- **search_genre must resolve before Commercial or Engagement fetches:** AppIDs are the key. You cannot query SteamDB or Gamalytic for a game you do not have an AppID for. This is the single hard dependency in the system.
- **Commercial and Engagement fetches are independent of each other:** They both need AppIDs, but they hit different sources and return different data. Run them concurrently.
- **Triangulation is a synthesis step, not a fetch step:** Do not model it as a separate "fetch." It is logic that runs after both Commercial and Engagement data arrive. It compares and confidence-weights the results.
- **Report generation depends on all three layers:** It is the terminal node. Nothing depends on it. It is the final output.
- **Caching is orthogonal to all data tools:** Every fetcher should check cache first. This is a cross-cutting concern, not a feature dependency.

---

## MVP Definition

### Launch With (v1)

Minimum viable product — what you need to answer "Is this genre a gold mine or a graveyard?" with data.

- [ ] `search_genre` tool — fetch AppIDs by Steam tag. Without this, nothing else works.
- [ ] `fetch_metadata` tool — get game names, descriptions, tags, developer info from Steam API. Provides context for everything downstream.
- [ ] `fetch_commercial_data` tool — SteamDB scraping for revenue estimates, pricing, peak CCU. This is the "is it commercially viable" signal.
- [ ] Structured error responses on all tools — the LLM needs to handle failures gracefully from day one.
- [ ] Input validation on all tools — bad inputs must be rejected before they hit external APIs.
- [ ] Basic TTL caching for metadata — prevents hammering Steam API during a research session.

### Add After Validation (v1.x)

Features to add once the core fetch-and-analyze loop is working and you have real usage patterns to optimize against.

- [ ] `fetch_engagement_data` tool — Peak CCU, review-to-sales multipliers. Adds the engagement dimension. Trigger: you have run 3+ genre analyses and want richer signals.
- [ ] Gamalytic integration for revenue triangulation — adds confidence to commercial estimates. Trigger: you notice revenue estimates feel unreliable in v1 analyses.
- [ ] Concurrent fetching for independent sources — performance optimization. Trigger: a single genre research session takes more than 30 seconds.
- [ ] Scrape health signaling — surfaces data freshness and scraper reliability. Trigger: you get a stale or wrong result and did not know it.

### Future Consideration (v2+)

Features to defer until the research workflow is proven and the pain points are real, not hypothetical.

- [ ] `generate_report` tool — structured report skeleton generation. Defer because: Claude can assemble reports from structured data today. The tool version adds formatting logic that may not match how you actually want reports structured. Build it when you know what the report looks like from actual use.
- [ ] Social sentiment layer (Reddit, Twitch, YouTube) — adds qualitative market signal. Defer because: three separate scraping surfaces, each fragile. Core market data must be stable first.
- [ ] Dependency-aware orchestration tool (single "research this genre" mega-tool) — combines search + fetch + report into one call. Defer because: building it requires knowing the stable shape of each underlying tool first. Premature orchestration locks you into an API you have not validated.

---

## Feature Prioritization Matrix

| Feature | User Value | Implementation Cost | Priority |
|---------|------------|---------------------|----------|
| search_genre (tag-based game lookup) | HIGH | MEDIUM | P1 |
| fetch_metadata (Steam game details) | HIGH | LOW | P1 |
| fetch_commercial_data (SteamDB scrape) | HIGH | HIGH | P1 |
| Structured error responses | HIGH | LOW | P1 |
| Input validation (zod schemas) | MEDIUM | LOW | P1 |
| Basic TTL caching (metadata) | MEDIUM | MEDIUM | P1 |
| fetch_engagement_data (CCU, reviews) | HIGH | MEDIUM | P2 |
| Gamalytic triangulation | MEDIUM | HIGH | P2 |
| Concurrent source fetching | MEDIUM | LOW | P2 |
| Scrape health signaling | MEDIUM | LOW | P2 |
| Report generation tooling | HIGH | HIGH | P3 |
| Social sentiment integration | MEDIUM | HIGH | P3 |
| Single orchestration mega-tool | LOW | MEDIUM | P3 |

**Priority key:**
- P1: Must have for launch — the tool is useless without these
- P2: Should have, add when possible — materially improves research quality
- P3: Nice to have, future consideration — adds polish or scope, not core function

---

## Competitor Feature Analysis

"Competitors" here means: other MCP data-fetching tools (pattern comparison) and existing game market research tools (feature comparison).

| Feature | Existing Game Research Tools (SteamDB, GameRefinery, Sensor Tower) | Generic MCP Data Tools (Firecrawl, Bright Data, Fetch) | Our Approach |
|---------|-------------------------------------------------------------------|--------------------------------------------------------|--------------|
| Data source access | Each tool is one source. SteamDB = SteamDB. GameRefinery = GameRefinery. No cross-source triangulation in a single workflow. | Source-agnostic. They fetch any URL. No domain knowledge. | Domain-specific: Steam + SteamDB + Gamalytic in one tool set. Triangulation is built in, not bolted on. |
| Output format | Web dashboards, CSV exports, proprietary UIs. Not LLM-native. | Clean markdown / structured JSON for LLM consumption. Good at this. | Structured typed objects for LLM consumption. Markdown report skeletons. LLM-native by design. |
| Error handling | Varies. Most game tools silently return partial data or show error pages. | Mature. Firecrawl and Bright Data handle retries, timeouts, rate limits. | Borrow MCP best practices: isError flag, error classification, retry with backoff. Do not invent. |
| Caching | GameRefinery caches internally. Users see snapshots, not live data. | Minimal. Most generic fetchers are stateless. | TTL-based per data type. Metadata cached long, pricing/engagement cached short. Tuned to Steam update cadence. |
| Rate limit handling | Mostly handled server-side (you pay for API access). | Varies. Some have built-in throttling. | Explicit: Steam API 200 req/5 min limit is baked into the tool. SteamDB scraping rate is configurable. |
| Genre/tag analysis | SteamDB supports tag filtering. GameRefinery has genre frameworks. Neither gives you "is this genre worth entering" in one query. | Not applicable — these are generic fetchers. | search_genre is the entry point. One query returns the foundation for genre analysis. |

---

## Sources

- MCP Best Practices: https://modelcontextprotocol.info/docs/best-practices/
- MCP Error Handling Guide: https://mcpcat.io/guides/error-handling-custom-mcp-servers/
- MCP Server Best Practices 2026: https://www.cdata.com/blog/mcp-server-best-practices-2026
- MCP Advanced Caching Strategies: https://medium.com/@parichay2406/advanced-caching-strategies-for-mcp-servers-from-theory-to-production-1ff82a594177
- MCP API Gateway (caching, rate limiting): https://www.gravitee.io/blog/mcp-api-gateway-explained-protocols-caching-and-remote-server-integration
- MCP External API Integration Patterns: https://www.stainless.com/mcp/from-rest-api-to-mcp-server
- Game Market Research Tools Overview: https://impress.games/blog/free-tools-for-video-game-market-competitor-analysis
- Game Market Research Workflow: https://www.bryter-global.com/market-research-for-video-game-developers
- Local Research MCP Server (privacy-first pattern): https://mcpservers.org/servers/Unlock-MCP/local-research-server
- Firecrawl MCP Server (scraping patterns): https://github.com/firecrawl/firecrawl-mcp-server
- PROJECT.md (project constraints, data layers, research framework): C:\Users\shiyu\.planning\PROJECT.md

---
*Feature research for: Games Market Research MCP Tool*
*Researched: 2026-02-05*
