# Pitfalls Research

**Domain:** MCP server + Steam API + web scraping (game market research tool)
**Researched:** 2026-02-05
**Confidence:** HIGH (MCP pitfalls verified via official docs + Nearform); MEDIUM (Steam rate limits -- community-reported, undocumented by Valve); HIGH (SteamDB scraping -- SteamDB FAQ is explicit)

---

## Critical Pitfalls

### Pitfall 1: stdout Pollution Kills the MCP Server Silently

**What goes wrong:**
Any `console.log()` call in a stdio-based MCP server writes to stdout, which is the same channel used for JSON-RPC protocol messages. A single stray log statement corrupts the message stream. The server does not crash with an error -- it silently returns malformed responses, and the client either hangs or drops the connection with no useful diagnostic.

**Why it happens:**
TypeScript developers reach for `console.log()` by habit during development. Logging libraries default to stdout. The MCP SDK does not intercept or warn about stdout writes -- the corruption is invisible until something breaks.

**How to avoid:**
Route all logging to stderr exclusively. Use `console.error()` for quick debugging. For anything more structured, use a logging library (e.g., `winston`, `pino`) configured with a stderr transport. Add an ESLint rule that flags `console.log` in server source files. This is the single most common MCP server bug across all language ecosystems.

**Warning signs:**
- Client hangs after tool invocation with no response
- MCP inspector shows connection drops without error codes
- Server appears to start fine but tool calls return nothing

**Phase to address:**
Phase 1 (MCP skeleton). This is a foundation constraint -- get it right at project start or pay for it in every subsequent debug session.

---

### Pitfall 2: Building on the Deprecated SSE Transport

**What goes wrong:**
The MCP SSE transport (spec version 2024-11-05) was deprecated in spec version 2025-03-26 and replaced by Streamable HTTP. Many tutorials, blog posts, and GitHub examples still use the old SSE pattern (separate GET endpoint for SSE stream + POST endpoint for messages). Building on this pattern means the server will stop working as clients drop legacy support.

**Why it happens:**
The majority of MCP tutorials online were written before or during the deprecation window. Copy-pasting from these examples produces code that works today but is on a deprecation clock.

**How to avoid:**
Use the Streamable HTTP transport from the start. The pattern is a single unified endpoint (`/mcp`) that accepts both POST and GET. The TypeScript SDK supports this natively. Verify your transport choice against the current spec at `modelcontextprotocol.io/specification/2025-03-26/basic/transports` before writing any server scaffolding.

**Warning signs:**
- Tutorial code uses two separate endpoint registrations (one for SSE GET, one for POST)
- SDK examples reference `SseServerTransport` as the primary transport
- No single endpoint path in your server config

**Phase to address:**
Phase 1 (MCP skeleton). Transport is the lowest-level architectural choice. Wrong here and everything built on top needs rework.

---

### Pitfall 3: SteamDB Scraping Without a Plan for Getting Blocked

**What goes wrong:**
SteamDB explicitly states in its FAQ: "No, there's a chance you'll get automatically banned for doing so" regarding scraping. They run Cloudflare anti-bot protection, log IPs and user-agents, and actively detect automated access. A scraper that works on day one will be blocked within days to weeks. This is the single highest-risk dependency in the entire project.

**Why it happens:**
Developers test scraping locally, it works, they ship it, and treat it as stable infrastructure. The PROJECT.md correctly flags this as "fragile" but the specific failure mode (automatic ban, not just rate limiting) is often underestimated.

**How to avoid:**
Design the Commercial Layer (SteamDB data) as an explicitly degradable component from day one. This means:
1. Never treat SteamDB scraping as always-available. Every tool that depends on it must return a clear "commercial data unavailable, using cached/estimated values" response.
2. Cache aggressively. Once you have data, store it locally. Re-scrape only what is stale and only on demand, not on every request.
3. Rotate User-Agent strings and add realistic request delays (minimum 5-10 seconds between requests). Do not scrape in bulk.
4. Have a fallback path: Gamalytic has an actual API. If SteamDB blocks you, Gamalytic becomes your primary commercial data source, not your secondary one.
5. Consider the Algolia search API that powers SteamDB's instant search as an alternative data entry point -- it returns JSON directly without page parsing, though it caps results at 1,000 per query.

**Warning signs:**
- HTTP 403 responses from SteamDB with no body
- Cloudflare challenge pages returned instead of game data
- Parsed HTML suddenly returns empty or unexpected structure (page layout changed or you are seeing a CAPTCHA page)

**Phase to address:**
Phase 2 (Commercial Layer). But the *architecture* for degradation must be decided in Phase 1.

---

### Pitfall 4: Steam API Rate Limits Are Not What You Think

**What goes wrong:**
Steam's rate limits are undocumented, inconsistent, and silently enforced. The commonly cited limit is 200 requests per 5 minutes, but developers regularly hit 429 errors at rates far below this threshold. Some endpoints (inventory, community profile) throttle as aggressively as 4 requests before blocking. Valve can and does tighten limits without notice (they did so for inventory endpoints in late 2023 with no announcement). The penalty for exceeding limits ranges from a temporary cooldown to a permanent IP block.

**Why it happens:**
Developers read "200 requests per 5 minutes" somewhere and code to that number. They do not account for: (a) per-IP throttling independent of API key, (b) endpoint-specific limits that are stricter than the global limit, (c) Valve changing limits at any time, (d) 429 responses that sometimes come back as HTTP 200 with an empty body.

**How to avoid:**
- Implement exponential backoff on every API call. Do not retry immediately on 429.
- Default to 1.5 seconds between requests (40 requests/minute) regardless of which endpoint you are hitting. This is well under any reported threshold.
- Check for empty response bodies on HTTP 200 -- Steam does this instead of returning 429 on some endpoints.
- Cache Steam API responses. The Metadata Layer (AppIDs, tags, basic metadata) changes slowly. A 24-hour cache on GetAppList results is reasonable.
- The `ISteamApps/GetAppList/v2` endpoint is deprecated. Use `IStoreService/GetAppList` instead.

**Warning signs:**
- HTTP 429 at request rates that seem low
- HTTP 200 with empty JSON body
- Intermittent failures that disappear after waiting 5-10 minutes
- All failures coinciding with bulk fetches

**Phase to address:**
Phase 1 (Steam API integration). Rate limiting is the first thing you will hit when testing against the live API.

---

### Pitfall 5: Revenue Estimate Accuracy Eaten by Triangulation Assumptions

**What goes wrong:**
The PROJECT.md specifies triangulation between SteamDB and Gamalytic for revenue confidence. But both sources use estimation methodologies, not ground truth. Gamalytic's own documentation states 77% of individual game estimates fall within a 30% margin of error, and 98% within 50%. SteamDB uses similar heuristics (review-count multipliers like 30x-50x). Triangulation only improves confidence if the two sources use *independent* estimation methods. In practice, both rely on the same underlying Steam data (review counts, player counts, top-seller rankings), so their errors are correlated. Averaging two correlated estimates does not halve the error -- it barely moves it.

**Why it happens:**
"Triangulation improves accuracy" is true in statistics when sources are independent. In the game revenue estimation ecosystem, sources are not independent -- they are all downstream of the same Steam data. Developers assume triangulation works like textbook cross-validation.

**How to avoid:**
- Label all revenue figures in reports as estimates with explicit error ranges. Never present them as facts. A report that says "$2M revenue" is misleading. A report that says "Estimated $1.4M-$2.6M revenue (30% confidence interval)" is useful.
- Use triangulation to detect *outliers* (when two sources disagree by more than 50%, flag the data point for manual review), not to produce a single averaged number.
- Include the estimation methodology as metadata in every revenue data point so downstream consumers know what they are looking at.

**Warning signs:**
- Two sources producing very similar estimates for every game (confirms correlation, not independence)
- Reports citing revenue as exact numbers without ranges
- Users treating estimates as ground truth for investment decisions

**Phase to address:**
Phase 2 (Commercial Layer) for data collection. Phase 4 (Report Generation) for how numbers are presented.

---

### Pitfall 6: Tool Interface Explosion Confuses the LLM

**What goes wrong:**
MCP servers that expose too many tools, or tools with overly complex parameter schemas, cause the calling LLM to make wrong tool selections or pass malformed arguments. This is a well-documented MCP anti-pattern. A game research tool tempts developers to expose one tool per data type: `get_revenue`, `get_player_count`, `get_review_count`, `get_price_history`, `get_tags`, `get_competitors`, etc. At 15+ tools with overlapping semantics, the LLM starts hallucinating parameter values or calling the wrong tool.

**Why it happens:**
The natural mapping is "one API endpoint = one MCP tool." But LLMs are not API clients -- they reason about intent, not endpoints. A tool surface area designed for programmatic consumption is a poor tool surface area for LLM consumption.

**How to avoid:**
Design tools around *research questions*, not data sources. Instead of `get_revenue` + `get_player_count` + `get_reviews`, expose `analyze_genre(genre: string)` which internally fetches and assembles all relevant data. Keep the total tool count under 10. Each tool description must be a plain-English sentence describing what question it answers, not what data it returns. Use Zod schemas with `describe()` on every parameter.

**Warning signs:**
- Tool count exceeds 10
- Two tools have descriptions that could apply to the same user question
- Tool parameters include internal IDs (like AppID) that the user would never know

**Phase to address:**
Phase 1 (MCP skeleton -- tool design). This shapes the entire API surface. Retrofitting tool consolidation after implementation is painful.

---

## Technical Debt Patterns

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|----------|-------------------|----------------|-----------------|
| Skip input validation on MCP tools (no Zod schemas) | Faster initial implementation | LLM passes bad data, errors are cryptic and hard to trace | Never -- Zod is a peer dependency of the SDK anyway |
| Fetch fresh from Steam API on every tool call (no cache) | Always-current data | Hits rate limits within minutes of real usage; tool becomes unusable | Only acceptable during initial smoke testing of a single endpoint |
| Hardcode SteamDB selectors to current page structure | Works today | Breaks silently when SteamDB updates HTML; data corruption goes unnoticed | Never for production; acceptable for a one-off manual scrape |
| Use a single MCP tool that returns raw JSON blobs | Simple to implement | LLM cannot reason over large unstructured data; wastes context window | Never -- format data as readable text before returning |
| Treat Gamalytic as a backup-only source | Simplifies initial architecture | When SteamDB blocks you (likely), you have no production path to commercial data | Only if Gamalytic API access is genuinely not available |

---

## Integration Gotchas

| Integration | Common Mistake | Correct Approach |
|-------------|----------------|------------------|
| Steam Web API | Using `ISteamApps/GetAppList/v2` | This endpoint is deprecated. Use `IStoreService/GetAppList` |
| Steam Web API | Assuming 429 means rate-limited | Check for HTTP 200 with empty body -- Steam does this too |
| Steam Web API | Firing requests as fast as possible | Add 1.5s delay between requests minimum; implement exponential backoff on any error |
| SteamDB scraping | Scraping on every request | Cache results locally. Re-scrape only on explicit refresh, with delays |
| SteamDB scraping | Using a single static User-Agent | Rotate User-Agents. Add realistic delays. Expect to get blocked anyway |
| Gamalytic API | Treating it as equivalent to SteamDB in data coverage | Gamalytic has an actual API with documented endpoints. It covers different data than SteamDB. Map which data comes from where explicitly |
| MCP stdio transport | Using `console.log()` anywhere in server code | All logging must go to stderr. No exceptions |
| MCP tool definitions | Writing technical descriptions | Write plain-English descriptions of what question the tool answers |

---

## Performance Traps

| Trap | Symptoms | Prevention | When It Breaks |
|------|----------|------------|----------------|
| No request queuing for Steam API | Burst of concurrent requests hits rate limit; subsequent requests all fail | Implement a request queue with configurable concurrency (start at 1 req/1.5s) | Immediately on any tool call that triggers multiple API requests |
| Returning full game datasets in tool responses | LLM context window fills up; responses become slow or truncated | Summarize and paginate. Return top-N results with a "get more" parameter | As soon as a genre query returns more than ~20 games |
| Scraping SteamDB synchronously in the request path | Tool call hangs for 10-30 seconds while scraping completes | Scrape to a background cache; serve from cache in the request path | Every single user-facing request |
| Re-parsing HTML on every cache miss | Selector changes corrupt data silently; no baseline to diff against | Store raw HTML alongside parsed data. On parse failure, compare raw HTML to last successful parse to detect layout changes | First time SteamDB updates their page structure |

---

## Security Mistakes

| Mistake | Risk | Prevention |
|---------|------|------------|
| Committing Steam API key to git | Key is public; Valve may revoke it or you may be charged for others' usage | Store in environment variable. Add `.env` to `.gitignore` before first commit |
| Storing Gamalytic API credentials in source code | Same as above | Environment variables only. Document required env vars in README |
| SteamDB scraping from a personal IP | Your IP gets permanently blocked; affects all Steam access from that network | Use a dedicated or rotated IP for scraping. Accept that scraping will eventually be blocked regardless |

---

## UX Pitfalls

| Pitfall | User Impact | Better Approach |
|---------|-------------|-----------------|
| Revenue numbers presented without error ranges | User treats estimates as facts; makes bad business decisions | Always present revenue as a range with confidence level (e.g., "$1.2M-$2.1M, est.") |
| Tool returns "error" when SteamDB is blocked | User thinks the tool is broken | Return partial data with a clear note: "Commercial data unavailable for this query. Using cached estimates from [date]." |
| Genre report includes games with no commercial data | Report looks incomplete or confusing | Segment the report: "Full data available for N games. Metadata-only for M games." Make this explicit, not hidden |
| Tool silently returns stale cached data | User makes decisions on outdated numbers | Include a "data freshness" timestamp in every response. Flag data older than 7 days |

---

## "Looks Done But Isn't" Checklist

- [ ] **MCP server startup:** Verify no stdout writes exist anywhere in the codebase. Run `grep -r "console.log" src/` before considering the server "done."
- [ ] **Steam API integration:** Verify the rate limiter is active, not just coded. Make 50 rapid requests and confirm backoff kicks in before a 429 is returned.
- [ ] **SteamDB scraping:** Verify the scraper returns a structured error (not a crash) when it receives a Cloudflare challenge page. Test by temporarily pointing at a known-blocked URL.
- [ ] **Gamalytic integration:** Verify the API key is read from an environment variable, not hardcoded. Verify the key is not in any committed file.
- [ ] **Revenue triangulation:** Verify reports display error ranges, not point estimates. Search the output for bare dollar amounts without confidence intervals.
- [ ] **Tool responses:** Verify no tool returns more than ~2000 tokens of raw data. Check by calling each tool with a broad query and measuring response length.
- [ ] **Cache behavior:** Verify each data source has a cache with a documented TTL. Verify stale data is labeled as such in responses.

---

## Recovery Strategies

| Pitfall | Recovery Cost | Recovery Steps |
|---------|---------------|----------------|
| stdout pollution discovered after server is built | LOW | Find and fix all `console.log` calls. Replace with `console.error` or a stderr logger. No architectural changes needed. |
| Built on SSE transport, need to switch to Streamable HTTP | MEDIUM | Transport is swapped at the server setup level. Tool logic does not change. Requires updating SDK usage and any client config. Estimate: 2-4 hours. |
| SteamDB scraper gets permanently blocked | MEDIUM | Shift commercial data sourcing to Gamalytic API (which has an actual API). Update all data-fetching code paths. Invalidate cached data that came from SteamDB and re-fetch via Gamalytic. |
| Steam API key revoked or IP blocked | MEDIUM | Request a new API key. If IP is blocked, wait or use a different outbound IP. Implement stricter rate limiting to prevent recurrence. |
| Revenue reports have been presenting point estimates without ranges | LOW | Update the report template. No data re-collection needed -- just change how existing data is formatted. |

---

## Pitfall-to-Phase Mapping

| Pitfall | Prevention Phase | Verification |
|---------|------------------|--------------|
| stdout pollution | Phase 1 (MCP skeleton) | ESLint rule active; `grep console.log src/` returns zero results |
| SSE transport (deprecated) | Phase 1 (MCP skeleton) | Server config uses Streamable HTTP transport; no SSE endpoint registered |
| SteamDB scraping fragility | Phase 1 (architecture decision) + Phase 2 (implementation) | Scraper returns structured degraded response when blocked; cache layer exists |
| Steam API rate limits | Phase 1 (Steam API integration) | Request queue is active; 50-request burst test passes without 429 |
| Revenue estimate accuracy | Phase 2 (Commercial Layer) + Phase 4 (Reports) | All revenue figures in test reports include error ranges |
| Tool interface explosion | Phase 1 (MCP skeleton -- tool design) | Tool count is under 10; each tool maps to a research question |

---

## Sources

- [MCP Official Docs: Build a Server](https://modelcontextprotocol.io/docs/develop/build-server) -- stdout/stderr logging rules (HIGH confidence, official)
- [MCP Spec: Transports (2025-03-26)](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports) -- SSE deprecation, Streamable HTTP (HIGH confidence, official spec)
- [Nearform: Implementing MCP -- Tips, Tricks and Pitfalls](https://nearform.com/digital-community/implementing-model-context-protocol-mcp-tips-tricks-and-pitfalls/) -- tool design anti-patterns, state management (MEDIUM confidence, practitioner guide)
- [SteamDB FAQ](https://steamdb.info/faq/) -- explicit scraping prohibition and Cloudflare protection (HIGH confidence, first-party)
- [Steam Web API Terms of Use](https://steamcommunity.com/dev/apiterms) -- rate limit terms, right to terminate (HIGH confidence, official)
- [ISteamApps Interface (Steamworks)](https://partner.steamgames.com/doc/webapi/ISteamApps) -- GetAppList deprecation notice (HIGH confidence, official)
- [McKay Development Forums: Steam Rate Limits](https://dev.doctormckay.com/topic/973-steam-rate-limits/) -- practical rate limit observations (MEDIUM confidence, community-verified)
- [Gamalytic About](https://gamalytic.com/about) -- estimation methodology and accuracy claims (MEDIUM confidence, first-party but self-reported)
- [Gamalytic API Docs](https://api.gamalytic.com/reference/) -- API exists and is documented (LOW confidence -- page did not render fully; verify during implementation)
- [Why MCP Deprecated SSE](https://blog.fka.dev/blog/2025-06-06-why-mcp-deprecated-sse-and-go-with-streamable-http/) -- SSE deprecation rationale (MEDIUM confidence, practitioner analysis)
- [ScrapingBee: Web Scraping Challenges 2025](https://www.scrapingbee.com/blog/web-scraping-challenges/) -- selector fragility, retry storms (MEDIUM confidence, industry analysis)

---
*Pitfalls research for: Games Market Research MCP Tool (TypeScript)*
*Researched: 2026-02-05*
