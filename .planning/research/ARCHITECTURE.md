# Architecture Research

**Domain:** MCP server -- games market research tool (API + scraping hybrid)
**Researched:** 2026-02-05
**Confidence:** MEDIUM-HIGH (MCP structure: HIGH from official docs; data-layer layout: MEDIUM from multiple community sources + official Steam docs; scraping resilience: MEDIUM from consensus across 5+ sources)

---

## Standard Architecture

### System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                     MCP Host (Claude Desktop /                   │
│                     claude.ai / Cursor / custom)                 │
└───────────────────────────┬─────────────────────────────────────┘
                            │  JSON-RPC 2.0 over stdio (or HTTP)
┌───────────────────────────▼─────────────────────────────────────┐
│                      MCP Server (this project)                   │
│                                                                   │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │
│  │  Tool: lookup│  │  Tool: market│  │  Tool: engagement       │  │
│  │  (metadata)  │  │  (commercial)│  │  (CCU / reviews)        │  │
│  └──────┬──────┘  └──────┬──────┘  └──────────┬──────────────┘  │
│         │                │                     │                  │
│  ┌──────▼──────┐  ┌──────▼──────┐  ┌──────────▼──────────────┐  │
│  │ Steam Web   │  │ Store Page  │  │ SteamSpy API /           │  │
│  │ API Client  │  │ Scraper     │  │ Steam Reviews Client     │  │
│  │ (stable)    │  │ (fragile)   │  │ (mixed)                  │  │
│  └──────┬──────┘  └──────┬──────┘  └──────────┬──────────────┘  │
│         │                │                     │                  │
│  ┌──────▼────────────────▼─────────────────────▼──────────────┐  │
│  │                   HTTP Transport Layer                       │  │
│  │  (exponential backoff, retry logic, rate-limit awareness)    │  │
│  └─────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
┌──────────────┐  ┌──────────────────┐  ┌────────────────┐
│ Steam Web API│  │ Steam Store Pages│  │ SteamSpy API   │
│ (Valve,      │  │ (store.steam-    │  │ (steamspy.com, │
│  stable)     │  │  community.com,  │  │  stable,       │
│              │  │  Cloudflare-     │  │  free,         │
│              │  │  protected)      │  │  no auth)      │
└──────────────┘  └──────────────────┘  └────────────────┘
```

### Component Responsibilities

| Component | Responsibility | Typical Implementation |
|-----------|----------------|------------------------|
| MCP Server | Declares tools, handles JSON-RPC lifecycle, routes tool calls | `McpServer` from `@modelcontextprotocol/sdk` or `fastmcp` wrapper |
| Tool Definitions | Expose named, schema-validated entry points to the LLM | One tool per logical query (lookup, market, engagement). Zod schemas for input validation. |
| Steam API Client | Fetch metadata: app lists, tags, categories via official endpoints | Thin HTTP wrapper around `api.steampowered.com`. Auth via API key in query param. |
| Store Page Scraper | Fetch commercial data: base price, discount history, review summaries | HTTP fetch of store JSON endpoint; HTML parse as fallback. Cloudflare is the main obstacle. |
| SteamSpy Client | Fetch engagement proxy data: peak CCU, playtime, ownership estimates | Simple GET to `steamspy.com/api.php`. No auth required. Rate-limited but generous for single-tool use. |
| HTTP Transport Layer | Shared retry/backoff/timeout logic used by all outbound HTTP calls | Single `fetchWithRetry` utility. Exponential backoff. Per-host rate-limit tracking. |

---

## Recommended Project Structure

```
src/
├── server.ts              # MCP server bootstrap: create McpServer, register tools, connect transport
├── tools/                 # One file per MCP tool (maps 1:1 to what the LLM sees)
│   ├── lookup.ts          # Metadata tool: resolve AppID, fetch tags/categories/name
│   ├── market.ts          # Commercial tool: pricing, discount state, revenue estimates
│   └── engagement.ts      # Engagement tool: CCU, review count/sentiment
├── clients/               # HTTP clients for each external data source
│   ├── steamApi.ts        # Steam Web API wrapper (ISteamApps, IStoreService, etc.)
│   ├── steamStore.ts      # Steam Store page fetcher (JSON endpoint + HTML fallback)
│   └── steamSpy.ts        # SteamSpy API wrapper
├── http/                  # Shared HTTP infrastructure
│   ├── fetch.ts           # fetchWithRetry: exponential backoff, timeout, status handling
│   └── rateLimiter.ts     # Per-host token-bucket or delay tracker
├── types/                 # Shared TypeScript interfaces
│   ├── steam.ts           # Steam API response shapes
│   └── tools.ts           # Tool input/output schemas (Zod)
└── index.ts               # Entry point: instantiate server, call server.ts
```

### Structure Rationale

- **tools/:** Each file is one MCP tool. This keeps the LLM-facing interface flat and discoverable. A tool file orchestrates across multiple clients as needed (e.g., `engagement.ts` may call both `steamSpy` and `steamStore`).
- **clients/:** Isolated per data source. Each client owns its own base URL, auth, and response parsing. Swap one client without touching tools or shared infra.
- **http/:** The retry and rate-limit logic is the single most important shared piece. Every outbound call goes through here. Built once, reused everywhere.
- **types/:** Centralized schemas prevent the same Steam response shape from being defined in three places.

---

## Architectural Patterns

### Pattern 1: Tool-Per-Query-Intent

**What:** Each MCP tool corresponds to one user intent, not one data source. `lookup` answers "what is this game?", `market` answers "how is it selling?", `engagement` answers "how popular is it right now?". Each tool internally fans out to whichever clients it needs.

**When to use:** Always, for MCP tools. The LLM uses tool names and descriptions to decide what to call. Tool names should be intent-shaped, not data-source-shaped.

**Trade-offs:** A single tool call may touch 2-3 external services. This is fine -- it is the expected pattern for data-fetching MCP servers. The alternative (one tool per API endpoint) produces dozens of tools and overwhelms the LLM's selection logic.

**Example:**
```typescript
// tools/engagement.ts
server.registerTool("get_engagement", {
  description: "Get current player engagement for a Steam game: CCU, review volume, sentiment",
  inputSchema: {
    appId: z.number().describe("Steam AppID"),
  },
}, async ({ appId }) => {
  // Fan out to multiple sources concurrently
  const [ccu, reviews] = await Promise.all([
    steamSpyClient.getCCU(appId),
    steamStoreClient.getReviews(appId),
  ]);
  return { content: [{ type: "text", text: formatEngagement(ccu, reviews) }] };
});
```

### Pattern 2: Stable-API-First, Scrape-as-Fallback

**What:** For any data point, prefer the stable API source. Only fall back to scraping if the API does not provide the field. Within the scraper itself, prefer the JSON API endpoint over HTML parsing.

**When to use:** Whenever a field is available from both an official API and a scraped page. Pricing is the key example: Steam's store JSON endpoint (`/api/appdetails?appids=X`) returns base price reliably. Only scrape HTML if that endpoint fails.

**Trade-offs:** Adds a small amount of branching logic in client code. Pays for itself in reliability -- the API path will succeed 95%+ of the time; the scrape path handles the remaining cases.

**Example:**
```typescript
// clients/steamStore.ts
async function getAppDetails(appId: number): Promise<AppDetails | null> {
  // Tier 1: Steam JSON API (stable, no Cloudflare)
  const jsonResult = await fetchWithRetry(
    `https://store.steampowered.com/api/appdetails?appids=${appId}`
  );
  if (jsonResult?.ok) return parseJsonDetails(jsonResult);

  // Tier 2: HTML scrape (fragile, Cloudflare-gated)
  const htmlResult = await fetchWithRetry(
    `https://store.steampowered.com/app/${appId}/`
  );
  if (htmlResult?.ok) return parseHtmlDetails(htmlResult);

  return null; // Both tiers failed -- return null, tool reports partial data
}
```

### Pattern 3: Shared Exponential-Backoff Transport

**What:** All HTTP calls in the server go through a single `fetchWithRetry` function that handles: timeout (default 10s), exponential backoff on 429/503/5xx, max retries (3), and per-host cooldown after repeated failures.

**When to use:** Every outbound HTTP call. No client should call `fetch` directly.

**Trade-offs:** Centralizes retry logic (good). Makes debugging slightly less obvious -- errors surface as "max retries exceeded" rather than the original status code. Mitigate by logging the full retry chain to stderr.

**Example:**
```typescript
// http/fetch.ts
export async function fetchWithRetry(
  url: string,
  options: RequestInit = {},
  { maxRetries = 3, baseDelayMs = 1000 }: RetryOptions = {}
): Promise<Response | null> {
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      const response = await fetch(url, { ...options, signal: AbortSignal.timeout(10_000) });
      if (response.ok) return response;
      if ([429, 503].includes(response.status)) {
        const delay = baseDelayMs * Math.pow(2, attempt);
        console.error(`[retry] ${response.status} from ${url} -- waiting ${delay}ms`); // stderr only
        await new Promise(r => setTimeout(r, delay));
        continue;
      }
      return response; // 4xx other than 429 -- don't retry
    } catch (err) {
      if (attempt === maxRetries) return null;
      console.error(`[retry] network error on ${url} -- attempt ${attempt + 1}`);
    }
  }
  return null;
}
```

---

## Data Flow

### Request Flow (typical: user asks about a game)

```
LLM decides to call "get_engagement" with { appId: 1234 }
    │
    ▼
MCP Host sends tools/call over stdio (JSON-RPC)
    │
    ▼
server.ts routes to tools/engagement.ts handler
    │
    ▼  (concurrent fan-out)
    ├── steamSpy.ts  →  fetchWithRetry("steamspy.com/api.php?request=appdetails&appid=1234")
    │       ↓
    │   parse JSON → { ccu: 4200, playtime_avg: 180 }
    │
    └── steamStore.ts  →  fetchWithRetry("store.steampowered.com/api/appdetails?appids=1234")
            ↓
        parse JSON → { reviews_total: 12000, reviews_positive_pct: 87 }
    │
    ▼  (merge)
tools/engagement.ts combines results into a single text block
    │
    ▼
MCP Host receives tools/call result { content: [{ type: "text", text: "..." }] }
    │
    ▼
LLM incorporates result into its response to the user
```

### Key Data Flows

1. **AppID resolution:** User provides a game name. `lookup` tool calls Steam Web API `ISteamApps/GetAppList` to resolve name to AppID. All subsequent tools use that AppID as the universal key.
2. **Pricing fetch:** `market` tool calls `store.steampowered.com/api/appdetails` (JSON tier). If that fails, falls back to HTML scrape of the store page. SteamDB is NOT the primary source here -- it is Cloudflare-protected and unreliable for automated access.
3. **CCU fetch:** `engagement` tool calls SteamSpy API. SteamSpy returns peak CCU from yesterday -- this is the only freely available CCU source that does not require scraping. If real-time CCU is needed, that requires the Steam API `ISteamApps/GetCurrentPlayerCount` endpoint (requires API key).
4. **Review fetch:** Steam's own `/api/appdetails` response includes review summary fields (total reviews, percentage positive). Detailed review text requires scraping individual review pages -- defer this to a later phase.

---

## Scaling Considerations

| Scale | Architecture Adjustments |
|-------|--------------------------|
| Single user, local desktop | stdio transport. No caching needed. All calls are on-demand. This is the starting point. |
| Multiple users, shared instance | Switch to Streamable HTTP transport. Add in-memory cache (TTL 5 min) for SteamSpy and Steam API responses. These APIs have implicit rate limits. |
| High-volume research workflows | Add a request queue per external host. Consider a local SQLite cache for AppID lookups (the app list is 140K+ entries and changes slowly). |

### Scaling Priorities

1. **First bottleneck: SteamSpy rate limiting.** SteamSpy does not publish hard limits but throttles aggressively at high volume. Cache responses with a 5-minute TTL before adding concurrency.
2. **Second bottleneck: Cloudflare on store pages.** If scraping store HTML at scale, Cloudflare will block you. The JSON API endpoint (`/api/appdetails`) is not Cloudflare-gated and should be the only store.steampowered.com endpoint you hit in production.

---

## Anti-Patterns

### Anti-Pattern 1: One Tool Per API Endpoint

**What people do:** Create `get_app_list`, `get_app_details`, `get_steamspy_data`, `get_store_page`, etc. -- one tool per HTTP endpoint.

**Why it's wrong:** The LLM sees all these tools and must decide which to call and in what order. With 8-12 tools, selection accuracy degrades. The LLM does not know that `get_app_details` must be called before `get_store_page`. Tool orchestration is your job, not the LLM's.

**Do this instead:** Three tools maximum: `lookup`, `market`, `engagement`. Each tool internally calls whatever endpoints it needs.

### Anti-Pattern 2: Writing to stdout in stdio Mode

**What people do:** Use `console.log()` for debug output during development, leave it in production.

**Why it's wrong:** stdio transport uses stdout for JSON-RPC messages. Any non-JSON output on stdout corrupts the protocol and silently breaks the connection. The MCP host will not report a useful error.

**Do this instead:** All logging goes to stderr. `console.error()` in TypeScript. Configure any logging library to target stderr explicitly.

### Anti-Pattern 3: Scraping SteamDB as the Primary Commercial Data Source

**What people do:** Target `steamdb.info` for revenue estimates, pricing history, and player counts because it has the richest data.

**Why it's wrong:** SteamDB runs behind Cloudflare with active anti-bot protection. As of 2025, Cloudflare blocks AI-originated scraping by default. SteamDB's FAQ explicitly states it is a third party and not owned by Valve. Relying on it as a primary source means your tool breaks whenever Cloudflare tightens detection.

**Do this instead:** Use `store.steampowered.com/api/appdetails` for pricing (official, JSON, no Cloudflare). Use SteamSpy for CCU and ownership estimates. Reserve SteamDB scraping as a stretch goal with explicit resilience requirements and a clear fallback path.

### Anti-Pattern 4: No Separation Between Stable and Fragile Data Sources

**What people do:** Put all HTTP calls in one generic client with identical retry logic.

**Why it's wrong:** Steam Web API and SteamSpy are stable REST APIs that return clean JSON. Store page scraping is fragile -- it breaks on layout changes, Cloudflare challenges, and rate limits. Treating them identically means a scraping failure degrades the reliability of stable calls, and retry budgets get wasted on scrape attempts that will keep failing.

**Do this instead:** Separate client modules per source (see project structure above). The `fetchWithRetry` utility is shared, but each client can tune its retry count, timeout, and backoff independently.

---

## Integration Points

### External Services

| Service | Integration Pattern | Notes |
|---------|---------------------|-------|
| Steam Web API (`api.steampowered.com`) | REST GET with `key=` query param | Stable. Self-documenting via `GetSupportedAPIList`. Rate-limited but generous. Requires API key from `steamcommunity.com/dev`. |
| Steam Store JSON API (`store.steampowered.com/api/appdetails`) | REST GET, no auth | Returns base price, review summary, app metadata. Not behind Cloudflare. Single-app-at-a-time (one `appids` param per request). |
| Steam Store HTML (`store.steampowered.com/app/{id}/`) | HTML scrape, parse with cheerio or similar | Behind Cloudflare. Use only as fallback. Do not hit in production loops. |
| SteamSpy (`steamspy.com/api.php`) | REST GET, no auth | Returns peak CCU, playtime, ownership estimates. Free. Throttles at high volume. Paginated via `page` param. |
| SteamDB (`steamdb.info`) | HTML scrape | Cloudflare-protected with anti-bot. HIGH RISK for automated access. Do not use as a primary source. |

### Internal Boundaries

| Boundary | Communication | Notes |
|----------|---------------|-------|
| tool handler -> client module | Direct async function call | Each tool imports the clients it needs. No message bus or queue needed at this scale. |
| client module -> HTTP transport | Calls `fetchWithRetry` from `http/fetch.ts` | All HTTP goes through this. Clients never call `fetch` directly. |
| tool handler -> MCP response | Returns `{ content: [{ type: "text", text }] }` | Text-only responses. The MCP protocol supports structured content (`structuredContent` + `outputSchema`) but text is simpler for LLM consumption and sufficient for this use case. |

---

## Build Order Implications

The architecture has a clear dependency chain that dictates build order:

```
Phase 1: MCP Server Skeleton
  - server.ts, index.ts, stdio transport
  - One stub tool that returns static text
  - Proves the end-to-end pipe: LLM -> host -> server -> response
  - No external HTTP calls yet

Phase 2: Metadata Layer (lookup tool)
  - steamApi.ts client + fetchWithRetry
  - lookup tool: AppID resolution, tags, basic metadata
  - Stable API. This is the foundation -- everything else uses AppIDs from here

Phase 3: Engagement Layer (engagement tool)
  - steamSpy.ts client
  - engagement tool: CCU, playtime, ownership
  - Stable API. Cheap to add once fetchWithRetry exists

Phase 4: Commercial Layer (market tool)
  - steamStore.ts client (JSON tier first, HTML fallback second)
  - market tool: pricing, review summary
  - Most fragile layer. Builds on retry infra from Phase 2.
  - HTML scraping is the last thing to ship -- add it only after JSON tier is proven insufficient

Phase 5: Polish
  - rateLimiter.ts per-host tracking
  - Error reporting quality (partial results vs hard failure)
  - SteamDB scraping (if needed) as an explicitly optional, high-risk addition
```

---

## Sources

- MCP Tools specification (Protocol Revision 2025-06-18): https://modelcontextprotocol.io/docs/concepts/tools -- HIGH confidence
- MCP TypeScript SDK GitHub: https://github.com/modelcontextprotocol/typescript-sdk -- HIGH confidence
- MCP Build Server tutorial (official): https://modelcontextprotocol.io/docs/develop/build-server -- HIGH confidence
- FastMCP framework: https://github.com/punkpeye/fastmcp -- MEDIUM confidence (third-party, actively maintained)
- Steam Web API overview (Valve): https://partner.steamgames.com/doc/webapi_overview -- HIGH confidence
- ISteamApps interface (Valve): https://partner.steamgames.com/doc/webapi/isteamapps -- HIGH confidence
- SteamSpy API documentation: https://steamspy.com/api.php -- MEDIUM confidence (third-party, long-running)
- SteamDB FAQ (anti-bot details): https://steamdb.info/faq/ -- MEDIUM confidence
- Web scraping resilience patterns: https://blog.apify.com/web-scraping-infrastructure/ (Feb 2026) -- MEDIUM confidence
- Cloudflare AI scraping block (2025): referenced in https://www.scraperapi.com/blog/top-bot-blockers/ -- MEDIUM confidence

---
*Architecture research for: Games market research MCP tool (TypeScript)*
*Researched: 2026-02-05*
